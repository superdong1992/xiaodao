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

function appendStructuredOutput(events, name = "StructuredOutput") {
  events.push(
    { type: "assistant", message: { content: [{ type: "tool_use", name, id: "output-1",
      input: { protocol: "xiaodao.followup.v1", request_key: "test-request", action: "ANSWER", answer_markdown: "依据日志回答。" } }] } },
    { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "output-1", content: "accepted" }] } },
  );
}

function partialEvent(event, wrapped = true) {
  return wrapped ? { type: "stream_event", event } : event;
}

function partialTool(name, id, wrapped = true) {
  return partialEvent({ type: "content_block_start", index: 0,
    content_block: { type: "tool_use", name, id, input: {} } }, wrapped);
}

for (const wrapped of [false, true]) {
  test(`follow-up audit rejects forbidden partial-only tools with wrapped=${wrapped}`, (context) => {
    const { root, events, before } = fixture(context);
    events.push(partialTool("Bash", "partial-only", wrapped));
    assert.throws(() => auditReportFollowupTrace({ events, workspaceRoot: root, before }),
      (error) => error.code === "FOLLOWUP_TRACE_TOOL_NOT_ALLOWED");
  });
  for (const name of ["Read", "Grep", "StructuredOutput", "structuredoutput"]) {
    test(`follow-up audit counts ${name} partial and complete frames once with wrapped=${wrapped}`, (context) => {
      const { root, events, before } = fixture(context);
      if (name === "StructuredOutput" || name === "structuredoutput") appendStructuredOutput(events, name);
      const offset = events.findIndex((event) => event.message?.content[0]?.name === name);
      const id = events[offset].message.content[0].id;
      events.splice(offset, 0, partialTool(name, id, wrapped));
      const audit = auditReportFollowupTrace({ events, workspaceRoot: root, before });
      assert.deepEqual([audit.reads, audit.searches, audit.log_reads, audit.log_searches], [1, 1, 1, 1]);
      assert.equal(validReportFollowupTraceReceipt(audit), true);
    });
  }
}

for (const [name, append, code] of [
  ["output without complete frame", () => [partialTool("StructuredOutput", "partial-only")], "FOLLOWUP_TRACE_TOOL_PARTIAL_INVALID"],
  ["read without complete frame", () => [partialTool("Read", "partial-only")], "FOLLOWUP_TRACE_TOOL_PARTIAL_INVALID"],
  ["orphan input delta", () => [partialEvent({ type: "content_block_delta", index: 0,
    delta: { type: "input_json_delta", partial_json: "{}" } })], "FOLLOWUP_TRACE_TOOL_PARTIAL_INVALID"],
  ["partial tool after completed use", () => [partialTool("Read", "read-1")], "FOLLOWUP_TRACE_TOOL_ID_INVALID"],
  ["duplicate partial declaration", () => [partialTool("Read", "partial-only"), partialTool("Read", "partial-only")], "FOLLOWUP_TRACE_TOOL_ID_INVALID"],
  ["malformed partial identity", () => [partialTool("Read", "")], "FOLLOWUP_TRACE_TOOL_ID_INVALID"],
]) test(`follow-up audit rejects ${name}`, (context) => {
  const { root, events, before } = fixture(context);
  events.push(...append());
  assert.throws(() => auditReportFollowupTrace({ events, workspaceRoot: root, before }), (error) => error.code === code);
});

test("follow-up audit rejects changed tool identity between partial and complete frames", (context) => {
  const { root, events, before } = fixture(context);
  events.splice(1, 0, partialTool("Read", "search-1"));
  assert.throws(() => auditReportFollowupTrace({ events, workspaceRoot: root, before }),
    (error) => error.code === "FOLLOWUP_TRACE_TOOL_ID_INVALID");
});

test("follow-up audit accepts known input deltas without retaining or trusting partial arguments", (context) => {
  const { root, events, before } = fixture(context);
  events.splice(1, 0, partialTool("Grep", "search-1"),
    partialEvent({ type: "content_block_delta", index: 0, delta: { type: "input_json_delta", partial_json: '{"pattern":"private"}' } }),
    partialEvent({ type: "content_block_stop", index: 0 }));
  const audit = auditReportFollowupTrace({ events, workspaceRoot: root, before });
  assert.deepEqual([audit.reads, audit.searches], [1, 1]);
  assert.doesNotMatch(JSON.stringify(audit), /private|pattern/);
  events[6].message.content[0].input.file_path = "../outside.log";
  assert.throws(() => auditReportFollowupTrace({ events, workspaceRoot: root, before }), /FOLLOWUP_TRACE_PATH_ESCAPE/);
});

for (const name of ["StructuredOutput", "structuredoutput"]) {
  for (const advertised of [false, true]) test(`follow-up trace accepts ${name} with advertised=${advertised} without counting file access`, (context) => {
    const { root, events, before } = fixture(context);
    if (advertised) events[0].tools.push(name);
    appendStructuredOutput(events, name);
    const audit = auditReportFollowupTrace({ events, workspaceRoot: root, before });
    assert.deepEqual([audit.reads, audit.searches, audit.log_reads, audit.log_searches], [1, 1, 1, 1]);
    assert.equal(validReportFollowupTraceReceipt(audit), true);
    assert.doesNotMatch(JSON.stringify(audit), /依据日志|answer_markdown|test-request/);
  });
  test(`report-only ${name} leaves all retrieval counts at zero`, (context) => {
    const { root, events, before } = fixture(context);
    events.splice(1);
    appendStructuredOutput(events, name);
    const audit = auditReportFollowupTrace({ events, workspaceRoot: root, before });
    assert.deepEqual([audit.reads, audit.searches, audit.log_reads, audit.log_searches], [0, 0, 0, 0]);
  });
}

for (const [name, mutate, code] of [
  ["lookalike output tools", (events) => { events[5].message.content[0].name = "StructuredOutputExtra"; }, "FOLLOWUP_TRACE_TOOL_NOT_ALLOWED"],
  ["case-folded output tools", (events) => { events[5].message.content[0].name = "STRUCTUREDOUTPUT"; }, "FOLLOWUP_TRACE_TOOL_NOT_ALLOWED"],
  ["container name used as tool", (events) => { events[5].message.content[0].name = "structured_output"; }, "FOLLOWUP_TRACE_TOOL_NOT_ALLOWED"],
  ["MCP output tools", (events) => { events[5].message.content[0].name = "mcp__remote__StructuredOutput"; }, "FOLLOWUP_TRACE_TOOL_NOT_ALLOWED"],
  ["array output input", (events) => { events[5].message.content[0].input = []; }, "FOLLOWUP_TRACE_TOOL_INPUT_INVALID"],
  ["string output input", (events) => { events[5].message.content[0].input = "{}"; }, "FOLLOWUP_TRACE_TOOL_INPUT_INVALID"],
  ["null output input", (events) => { events[5].message.content[0].input = null; }, "FOLLOWUP_TRACE_TOOL_INPUT_INVALID"],
  ["missing output result", (events) => { events.pop(); }, "FOLLOWUP_TRACE_TOOL_RESULT_MISSING"],
  ["failed output result", (events) => { events[6].message.content[0].is_error = true; }, "FOLLOWUP_TRACE_TOOL_RESULT_INVALID"],
  ["duplicate output result", (events) => { events.push(events[6]); }, "FOLLOWUP_TRACE_TOOL_RESULT_INVALID"],
  ["output result before use", (events) => { [events[5], events[6]] = [events[6], events[5]]; }, "FOLLOWUP_TRACE_TOOL_RESULT_INVALID"],
  ["duplicate output ID", (events) => { events[5].message.content[0].id = "read-1"; }, "FOLLOWUP_TRACE_TOOL_ID_INVALID"],
  ["wrong output role", (events) => { events[5].type = "user"; }, "FOLLOWUP_TRACE_TOOL_NOT_ALLOWED"],
  ["both output aliases advertised", (events) => { events[0].tools.push("StructuredOutput", "structuredoutput"); }, "FOLLOWUP_TRACE_INIT_INVALID"],
  ["duplicate output inventory", (events) => { events[0].tools.push("StructuredOutput", "StructuredOutput"); }, "FOLLOWUP_TRACE_INIT_INVALID"],
  ["missing read inventory", (events) => { events[0].tools = ["Grep", "StructuredOutput"]; }, "FOLLOWUP_TRACE_INIT_INVALID"],
  ["non-string inventory entry", (events) => { events[0].tools = [["Grep"], "Read", "StructuredOutput"]; }, "FOLLOWUP_TRACE_INIT_INVALID"],
  ["output with relaxed permissions", (events) => { events[0].tools.push("StructuredOutput"); events[0].permissionMode = "bypassPermissions"; }, "FOLLOWUP_TRACE_INIT_INVALID"],
  ["output with MCP servers", (events) => { events[0].tools.push("StructuredOutput"); events[0].mcp_servers.push({ name: "ambient" }); }, "FOLLOWUP_TRACE_INIT_INVALID"],
  ["output masking escaped reads", (events) => { events[3].message.content[0].input.file_path = "../outside.log"; }, "FOLLOWUP_TRACE_PATH_ESCAPE"],
]) test(`follow-up structured output audit rejects ${name}`, (context) => {
  const { root, events, before } = fixture(context);
  appendStructuredOutput(events);
  mutate(events);
  assert.throws(() => auditReportFollowupTrace({ events, workspaceRoot: root, before }), (error) => error.code === code);
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

function runStructuredFollowupWrapper(context, { name = "StructuredOutput", mutate = () => {}, exitCode = 0 } = {}) {
  const { root, events } = fixture(context);
  events[0].tools.push(name);
  events[0].model = "test-model";
  appendStructuredOutput(events, name);
  const structured = events.at(-2).message.content[0].input;
  events.push({ type: "result", subtype: "success", is_error: false, num_turns: 3,
    total_cost_usd: 0.01, structured_output: structured,
    usage: { input_tokens: 20, output_tokens: 10, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 } });
  mutate(events);
  const settings = path.join(root, "followup-policy.json");
  const cli = path.join(root, "fake-cli.mjs");
  const usageRoot = path.join(root, "usage");
  fs.writeFileSync(settings, '{"env":{},"hooks":{"PreToolUse":[]}}\n');
  fs.writeFileSync(cli, `for(const event of ${JSON.stringify(events)}) console.log(JSON.stringify(event));\nprocess.exitCode=${exitCode};\n`);
  const result = spawnSync(process.execPath, [path.join(ROOT, "tools/test-flow/runtime-support/isolated-agent-wrapper.mjs"),
    "--claude-entry", cli, "--settings", settings, "--model", "test-model", "--usage-root", usageRoot,
    "--max-turns", "8", "--max-total-tokens", "120000", "--max-budget-usd", "1", "--hard-timeout-seconds", "30",
    "--workflow", "report-followup"], {
    cwd: root, input: "", encoding: "utf8",
    env: { ...buildIsolatedAgentEnvironment({ ambient: process.env }),
      PROBLEM_LOCATOR_AGENT_FILE_ACCESS: "read-search", PROBLEM_LOCATOR_AGENT_PHASE: "REPORT_FOLLOWUP",
      PROBLEM_LOCATOR_FOLLOWUP_SETTINGS: settings },
  });
  const receipts = fs.existsSync(usageRoot)
    ? fs.readdirSync(usageRoot).map((file) => JSON.parse(fs.readFileSync(path.join(usageRoot, file), "utf8"))) : [];
  return { result, receipts, structured };
}

for (const name of ["StructuredOutput", "structuredoutput"]) test(`isolated follow-up wrapper preserves a successful ${name} terminal object`, (context) => {
  const { result, receipts, structured } = runStructuredFollowupWrapper(context, { name });
  assert.equal(result.status, 0, result.stderr);
  const output = result.stdout.trim().split(/\r?\n/).map((line) => JSON.parse(line));
  assert.deepEqual(output.at(-1).structured_output, structured);
  assert.equal(Object.hasOwn(output.at(-1), "result"), false);
  assert.equal(receipts.length, 1);
  assert.deepEqual(receipts[0].wrapper_outcome, { schema_version: 1, status: "PASS", code: null });
  assert.equal(validReportFollowupTraceReceipt(receipts[0].tool_trace_audit), true);
  assert.deepEqual([receipts[0].tool_trace_audit.reads, receipts[0].tool_trace_audit.searches], [1, 1]);
});

for (const [name, options, code] of [
  ["forbidden partial-only tool", { mutate: (events) => { events.splice(1, 0, partialTool("Bash", "partial-only")); } }, "WRAPPER_FOLLOWUP_TRACE_INVALID"],
  ["incomplete partial output declaration", { mutate: (events) => { events.splice(1, 0, partialTool("StructuredOutput", "partial-only")); } }, "WRAPPER_FOLLOWUP_TRACE_INVALID"],
  ["failed output tool", { mutate: (events) => { events[6].message.content[0].is_error = true; } }, "WRAPPER_FOLLOWUP_TRACE_INVALID"],
  ["missing output tool result", { mutate: (events) => { events.splice(6, 1); } }, "WRAPPER_FOLLOWUP_TRACE_INVALID"],
  ["failed terminal", { mutate: (events) => { events.at(-1).is_error = true; } }, "WRAPPER_MODEL_TERMINAL_INVALID"],
  ["error terminal subtype", { mutate: (events) => { events.at(-1).subtype = "error_max_turns"; } }, "WRAPPER_MODEL_TERMINAL_INVALID"],
  ["duplicate terminal", { mutate: (events) => { events.push(events.at(-1)); } }, "WRAPPER_MODEL_STREAM_INVALID"],
  ["events after terminal", { mutate: (events) => { events.push({ type: "assistant", message: { content: [] } }); } }, "WRAPPER_MODEL_STREAM_INVALID"],
  ["failed process", { exitCode: 9 }, "WRAPPER_CHILD_PROCESS_FAILED"],
]) test(`isolated follow-up wrapper rejects structured output with ${name}`, (context) => {
  const { result } = runStructuredFollowupWrapper(context, options);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, new RegExp(code));
});
