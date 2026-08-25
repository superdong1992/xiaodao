import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  auditMcpRecoveries,
  materializeClientSettings,
  parseArguments,
  safeE2EError,
} from "../runtime/claude-deepseek-e2e-runner.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("E2E runner accepts only repository-owned Claude inputs and rejects Docker, MCP source, and adapters", () => {
  const names = ["source-root", "claude-entry", "claude-settings", "python-entry", "logparse-root", "cache-root", "scenario", "work-root", "private-root", "evidence-root", "usage-root", "run-id"];
  const argv = names.flatMap((name) => [`--${name}`, `/${name}`]);
  assert.equal(parseArguments(argv).scenario, "/scenario");
  for (const [name, value] of [["docker-context", "colima"], ["mcp-source", "/tmp/mcp"], ["adapter", "/tmp/adapter"], ["codex-auth", "/tmp/auth"]]) {
    assert.throws(() => parseArguments([...argv, `--${name}`, value]), (error) => error.code === "CLAUDE_DEEPSEEK_E2E_ARGUMENT_UNKNOWN");
  }
});

test("E2E source freezes one client plus four ordered server receipts and no retry path", () => {
  const source = fs.readFileSync(path.join(ROOT, "runtime", "claude-deepseek-e2e-runner.mjs"), "utf8");
  assert.match(source, /\["route", "logparse", "diagnose", "review"\]/);
  assert.match(source, /const invocations = \[client\.receipt, \.\.\.serverInvocations\]/);
  assert.match(source, /auditClaudeInvocations\(invocations, \{ workflow: "e2e" \}\)/);
  assert.equal(source.includes("automaticRetry"), false);
  assert.equal(source.includes("retryProcess"), false);
  assert.match(source, /auditMcpRecoveries\(client\.mcp\)/);
});

test("recoverable MCP errors reuse the original request ID exactly once in the same Client ledger", () => {
  const failed = { ordinal: 1, tool: "problem_locator_submit_supplement", arguments: { request_id: "req-submit", expected_case_revision: 2 }, result: { ok: false, data: null, error: { code: "REVISION_CONFLICT" } } };
  const corrected = { ordinal: 3, tool: "problem_locator_submit_supplement", arguments: { request_id: "req-submit", expected_case_revision: 3 }, result: { ok: true, data: { case_revision: 4 }, error: null } };
  assert.deepEqual(auditMcpRecoveries([failed, corrected]).recoveries, [{ tool: failed.tool, code: "REVISION_CONFLICT", request_id: "req-submit", failed_ordinal: 1, corrected_ordinal: 3 }]);
  assert.throws(() => auditMcpRecoveries([failed, { ...corrected, arguments: { ...corrected.arguments, request_id: "new-request" } }]), (error) => error.code === "CLAUDE_DEEPSEEK_RECOVERY_REQUEST_ID_INVALID");
  assert.throws(() => auditMcpRecoveries([failed, corrected, { ...corrected, ordinal: 4 }]), (error) => error.code === "CLAUDE_DEEPSEEK_RECOVERY_REQUEST_ID_INVALID");
});

test("client uses strict MCP, production Skill, exact Bash programs, and one fresh data root", () => {
  const source = fs.readFileSync(path.join(ROOT, "runtime", "claude-deepseek-e2e-runner.mjs"), "utf8");
  assert.match(source, /\.claude", "skills", "problem-locator-client/);
  assert.match(source, /mcpConfig/);
  assert.match(source, /Bash\(\/usr\/bin\/openssl:\*\)/);
  assert.match(source, /Bash\(\/usr\/bin\/stat:\*\)/);
  assert.match(source, /Bash\(\/usr\/bin\/curl:\*\)/);
  assert.match(source, /auditClientBash/);
  assert.match(source, /const CLIENT_DISALLOWED_TOOLS = Object\.freeze\(\["Read", "Glob", "Grep", "Edit", "Write"\]\)/);
  assert.match(source, /disallowedTools: CLIENT_DISALLOWED_TOOLS, auditOnlyAllowedTools: CLIENT_DISALLOWED_TOOLS, allowToolErrors: true/);
  assert.match(source, /!CLIENT_DISALLOWED_TOOLS\.includes\(record\.name\) \|\| record\.is_error === true/);
  assert.match(source, /denied_tool_attempts: client\.denied/);
  assert.match(source, /claude-deepseek-bash-policy\.mjs/);
  assert.match(source, /一条物理命令行/);
  assert.match(source, /const dataRoot = path\.join\(workRoot, "data-root"\)/);
  assert.match(source, /treeBytes\(serviceEvidence\) \+ treeBytes\(serviceUsage\)/);
  assert.match(source, /TEST_FLOW_PROGRESS stage\.progress claude-deepseek/);
  assert.equal(source.includes("Chrome"), false);
  assert.equal(source.includes("docker"), false);
  assert.equal(source.includes("restart"), false);
  assert.equal(source.includes("cross-job"), false);
});

test("client-only settings overlay adds three Bash rules and one test-owned PreToolUse policy without copying provider Hooks", () => {
  const root = fs.mkdtempSync(path.join("/private/tmp", "claude-client-settings-"));
  const source = path.join(root, "provider.json");
  const target = path.join(root, "client.json");
  const hookScript = path.join(root, "hook.mjs");
  const policyPath = path.join(root, "policy.json");
  fs.writeFileSync(source, '{"env":{"ANTHROPIC_AUTH_TOKEN":"token","ANTHROPIC_BASE_URL":"https://example.test"}}\n');
  fs.writeFileSync(hookScript, "// hook\n");
  fs.writeFileSync(policyPath, "{}\n");
  const receipt = materializeClientSettings(source, target, { hookScript, policyPath });
  const value = JSON.parse(fs.readFileSync(target, "utf8"));
  assert.equal(receipt.provider_env_unchanged, true);
  assert.deepEqual(value.env, JSON.parse(fs.readFileSync(source, "utf8")).env);
  assert.deepEqual(value.permissions.allow, ["Bash(/usr/bin/openssl:*)", "Bash(/usr/bin/stat:*)", "Bash(/usr/bin/curl:*)"]);
  assert.equal(receipt.hooks_copied, false);
  assert.equal(receipt.test_owned_pre_tool_use, true);
  assert.equal(value.hooks.PreToolUse[0].matcher, "Bash");
});

test("E2E evidence closes MCP, attachment, terminal Case, Artifact, oracle, DFX, budgets, and secrets", () => {
  const source = fs.readFileSync(path.join(ROOT, "runtime", "claude-deepseek-e2e-runner.mjs"), "utf8");
  for (const evidence of ["mcp-tool-calls.json", "attachment.json", "final-case.json", "artifact-index.json", "scenario-oracle.json", "server-events.ndjson", "model-usage.json", "security-audit.json", "adapter-receipt.json"]) assert.ok(source.includes(evidence), evidence);
  for (const audit of ["auditMcpToolCalls", "auditUploadedAttachment", "artifactConsistency", "auditOracle", "combineServerEvents", "auditClaudeInvocations", "secretScan"]) assert.ok(source.includes(audit), audit);
});

test("safe E2E error exposes only closed code and message", () => {
  assert.deepEqual(safeE2EError({ code: "CLOSED", message: "safe", details: { token: "secret" } }), { schema_version: 1, status: "FAIL", code: "CLOSED", message: "safe" });
});
