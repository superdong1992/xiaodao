import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { loadConfiguration, resolveGoalClosure } from "../lib/config.mjs";
import { auditReportFollowupTrace, reportFollowupInputManifest, reportFollowupPermissionArguments,
  validReportFollowupTraceReceipt } from "../runtime-support/report-followup-tool-audit.mjs";
import { buildIsolatedAgentEnvironment } from "../runtime-support/isolated-agent-env.mjs";

const ROOT = path.resolve(import.meta.dirname, "../../..");

function fixture(context) {
  const root = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), "followup-audit-")));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, "inputs", "logs"), { recursive: true });
  fs.writeFileSync(path.join(root, "inputs", "context.json"), '{"question":"原始用户输入"}\n');
  fs.writeFileSync(path.join(root, "inputs", "logs", "probe.log"), "private-log-marker\n");
  const tool = (name, id, input) => ({ type: "assistant", message: { content: [{ type: "tool_use", name, id, input }] } });
  const result = (id) => ({ type: "user", message: { content: [{ type: "tool_result", tool_use_id: id, content: "private-log-marker" }] } });
  const events = [
    { type: "system", subtype: "init", cwd: root, permissionMode: "dontAsk", tools: ["Read", "Grep"], mcp_servers: [] },
    tool("Grep", "search-1", { path: "inputs/logs", pattern: "private" }), result("search-1"),
    tool("Read", "read-1", { file_path: "inputs/logs/probe.log" }), result("read-1"),
  ];
  return { root, events, before: reportFollowupInputManifest(root) };
}

test("report follow-up selects only the deterministic closure and two independent model invocations", () => {
  const config = loadConfiguration(ROOT);
  const closure = resolveGoalClosure(config, { goalId: "dev.real", track: "dev", requestedStage: "real.report-followup", client: "windows" });
  assert.deepEqual(closure.stages.filter((stage) => stage.kind === "isolated-real").map((stage) => stage.id), ["real.report-followup"]);
  const gate = config.gates.gates["real.agent.report-followup"];
  assert.equal(gate.isolated_agent_invocations, 2);
  assert.equal(gate.min_passed, 2);
  assert.equal(gate.skip_policy, "forbid");
  assert.equal(gate.environment_profile, "real-report-followup");
  assert.deepEqual(gate.selectors, ["tests/real/agent/test_real_report_followup.py"]);
  assert.ok(gate.evidence.includes("report-followup-audit.json"));
  const release = resolveGoalClosure(config, { goalId: "release.full", track: "release", client: "windows" });
  assert.equal(release.stages.some((stage) => stage.id === "real.report-followup"), false);
});

test("follow-up trace proves successful scoped searches and reads without retaining user content", (context) => {
  const { root, events, before } = fixture(context);
  const audit = auditReportFollowupTrace({ events, workspaceRoot: root, before });
  assert.deepEqual([audit.reads, audit.searches, audit.log_reads, audit.log_searches], [1, 1, 1, 1]);
  assert.equal(validReportFollowupTraceReceipt(audit), true);
  assert.doesNotMatch(JSON.stringify(audit), /private|原始|question|probe\.log/);
  assert.deepEqual(reportFollowupPermissionArguments(), ["--tools", "Read,Grep", "--permission-mode", "dontAsk"]);
  assert.equal(validReportFollowupTraceReceipt({ ...audit, log_reads: 2 }), false);
});

for (const [name, mutate, code] of [
  ["outside input paths", (events) => { events[3].message.content[0].input.file_path = "../outside.log"; }, "FOLLOWUP_TRACE_PATH_ESCAPE"],
  ["write tools", (events) => { events[3].message.content[0].name = "Write"; }, "FOLLOWUP_TRACE_TOOL_NOT_ALLOWED"],
  ["missing search scope", (events) => { delete events[1].message.content[0].input.path; }, "FOLLOWUP_TRACE_PATH_INVALID"],
  ["failed tool results", (events) => { events[2].message.content[0].is_error = true; }, "FOLLOWUP_TRACE_TOOL_RESULT_INVALID"],
  ["unpaired tool results", (events) => { events.pop(); }, "FOLLOWUP_TRACE_TOOL_RESULT_MISSING"],
  ["duplicate results", (events) => { events.push(events[4]); }, "FOLLOWUP_TRACE_TOOL_RESULT_INVALID"],
  ["broader tool inventory", (events) => { events[0].tools.push("Bash"); }, "FOLLOWUP_TRACE_INIT_INVALID"],
  ["ambient MCP servers", (events) => { events[0].mcp_servers.push({ name: "ambient", status: "connected" }); }, "FOLLOWUP_TRACE_INIT_INVALID"],
]) test(`follow-up audit rejects ${name}`, (context) => {
  const { root, events, before } = fixture(context);
  mutate(events);
  assert.throws(() => auditReportFollowupTrace({ events, workspaceRoot: root, before }), (error) => error.code === code);
});

test("input changes and hard links fail the follow-up input audit", (context) => {
  const { root, events, before } = fixture(context);
  const input = path.join(root, "inputs", "logs", "probe.log");
  fs.writeFileSync(input, "changed\n");
  assert.throws(() => auditReportFollowupTrace({ events, workspaceRoot: root, before }), /FOLLOWUP_TRACE_INPUT_CHANGED/);
  fs.linkSync(input, path.join(root, "shared.log"));
  assert.throws(() => reportFollowupInputManifest(root), /FOLLOWUP_TRACE_PATH_UNSAFE/);
});

test("the isolated wrapper enforces the production follow-up policy and writes its audit", (context) => {
  const { root, events } = fixture(context);
  const settings = path.join(root, "settings.json");
  const policySettings = path.join(root, "followup-policy.json");
  const cli = path.join(root, "fake-cli.mjs");
  fs.writeFileSync(settings, JSON.stringify({ permissions: { allow: ["Read", "Grep"] }, hooks: { SessionStart: [] }, enabledPlugins: { inherited: true } }));
  fs.writeFileSync(policySettings, '{"env":{},"hooks":{"PreToolUse":[]}}\n');
  const terminal = { type: "result", subtype: "success", is_error: false, num_turns: 3,
    total_cost_usd: 0.01, result: "依据日志回答。", usage: { input_tokens: 20, output_tokens: 10, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 } };
  const stream = [{ ...events[0], model: "test-model" }, ...events.slice(1), terminal];
  fs.writeFileSync(cli, `import assert from 'node:assert/strict';\nconst args=process.argv.slice(2);\nassert.deepEqual(args.slice(args.indexOf('--tools')),${JSON.stringify(reportFollowupPermissionArguments())});\nassert.equal(args[args.indexOf('--setting-sources')+1],'');\nassert.equal(args[args.indexOf('--settings')+1],${JSON.stringify(policySettings)});\nassert.equal(args[args.indexOf('--mcp-config')+1],'{"mcpServers":{}}');\nfor(const flag of ['--strict-mcp-config','--disable-slash-commands','--no-chrome']) assert.ok(args.includes(flag));\nassert.equal(Object.hasOwn(process.env,'PROBLEM_LOCATOR_FOLLOWUP_SETTINGS'),false);\nfor(const event of ${JSON.stringify(stream)}) console.log(JSON.stringify(event));\n`);
  const args = [path.join(ROOT, "tools/test-flow/runtime-support/isolated-agent-wrapper.mjs"),
    "--claude-entry", cli, "--settings", settings, "--model", "test-model",
    "--usage-root", path.join(root, "usage"), "--max-turns", "8", "--max-total-tokens", "120000",
    "--max-budget-usd", "1", "--hard-timeout-seconds", "30", "--workflow", "report-followup"];
  const env = buildIsolatedAgentEnvironment({ ambient: process.env });
  const rejected = spawnSync(process.execPath, args, { cwd: root, env, input: "", encoding: "utf8" });
  assert.notEqual(rejected.status, 0);
  assert.match(rejected.stderr, /WRAPPER_FOLLOWUP_POLICY_REQUIRED/);
  const missingGuard = spawnSync(process.execPath, args, { cwd: root,
    env: { ...env, PROBLEM_LOCATOR_AGENT_FILE_ACCESS: "read-search", PROBLEM_LOCATOR_AGENT_PHASE: "REPORT_FOLLOWUP" },
    input: "", encoding: "utf8" });
  assert.notEqual(missingGuard.status, 0);
  assert.match(missingGuard.stderr, /WRAPPER_FOLLOWUP_SETTINGS_REQUIRED/);
  const accepted = spawnSync(process.execPath, args, { cwd: root,
    env: { ...env, PROBLEM_LOCATOR_AGENT_FILE_ACCESS: "read-search", PROBLEM_LOCATOR_AGENT_PHASE: "REPORT_FOLLOWUP",
      PROBLEM_LOCATOR_FOLLOWUP_SETTINGS: policySettings },
    input: "", encoding: "utf8" });
  assert.equal(accepted.status, 0, accepted.stderr);
  const [name] = fs.readdirSync(path.join(root, "usage"));
  const receipt = JSON.parse(fs.readFileSync(path.join(root, "usage", name), "utf8"));
  assert.equal(receipt.workflow, "report-followup");
  assert.equal(validReportFollowupTraceReceipt(receipt.tool_trace_audit), true);
  assert.equal(receipt.environment_policy.claude_process.key_names.includes("PROBLEM_LOCATOR_AGENT_PHASE"), false);
  assert.equal(receipt.environment_policy.claude_process.key_names.includes("PROBLEM_LOCATOR_FOLLOWUP_SETTINGS"), false);
});
