import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import zlib from "node:zlib";
import { fileURLToPath } from "node:url";

import {
  auditMcpRecoveries,
  auditSpecializedRuntime,
  auditDownloadedResultArchive,
  auditClientSkillLoad,
  claudeClientPrompt,
  clientToolInputPolicyIdentity,
  materializeDefaultLogparseConfig,
  serviceSourceEnvironment,
  materializeClientSettings,
  parseArguments,
  safeE2EError,
  serviceLauncherArguments,
} from "../runtime/claude-deepseek-e2e-runner.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("E2E service uses isolated Python without writing bytecode into the materialized source", () => {
  const sourceRoot = path.resolve("sealed-source");
  assert.deepEqual(serviceLauncherArguments(sourceRoot), [
    "-I",
    "-B",
    path.join(sourceRoot, "tools", "test-flow", "runtime-support", "test_service_launcher.py"),
    "serve",
  ]);
});

test("E2E runner accepts only repository-owned Claude inputs and rejects Docker, MCP source, and adapters", () => {
  const names = ["source-root", "claude-entry", "claude-settings", "python-entry", "logparse-root", "cache-root", "scenario", "work-root", "private-root", "evidence-root", "usage-root", "run-id"];
  const argv = names.flatMap((name) => [`--${name}`, `/${name}`]);
  assert.equal(parseArguments(argv).scenario, "/scenario");
  for (const [name, value] of [["docker-context", "colima"], ["mcp-source", "/tmp/mcp"], ["adapter", "/tmp/adapter"], ["codex-auth", "/tmp/auth"]]) {
    assert.throws(() => parseArguments([...argv, `--${name}`, value]), (error) => error.code === "CLAUDE_DEEPSEEK_E2E_ARGUMENT_UNKNOWN");
  }
});

test("E2E source freezes one client plus scenario-required ordered server receipts and no retry path", () => {
  const source = fs.readFileSync(path.join(ROOT, "runtime", "claude-deepseek-e2e-runner.mjs"), "utf8");
  assert.match(source, /expectedPhases\.slice\(1\)/);
  assert.match(source, /const invocations = \[client\.receipt, \.\.\.serverInvocations\]/);
  assert.match(source, /auditClaudeInvocations\(invocations, \{ workflow: "e2e", scenarioId: options\.scenario \}\)/);
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

test("one empty get_case validation attempt permits only the immediate complete successful correction", () => {
  const failed = { ordinal: 1, tool: "problem_locator_get_case", arguments: {}, result: { ok: false, data: null, error: { code: "VALIDATION_ERROR" } } };
  const corrected = { ordinal: 2, tool: "problem_locator_get_case", arguments: { case_id: "case-id", wait_for_job_id: null, wait_seconds: 30 }, result: { ok: true, data: { case_view: {} }, error: null } };
  assert.deepEqual(auditMcpRecoveries([failed, corrected]).recoveries, [{ tool: failed.tool, code: "EMPTY_GET_CASE_VALIDATION", request_id: null, failed_ordinal: 1, corrected_ordinal: 2 }]);
  assert.equal(auditMcpRecoveries([failed, { ...corrected, arguments: { ...corrected.arguments, wait_seconds: 0 } }]).status, "PASS");
  assert.throws(() => auditMcpRecoveries([failed, { ...corrected, ordinal: 3 }, { ordinal: 2, tool: "problem_locator_list_artifacts", arguments: { case_id: "case-id" }, result: { ok: true, data: { artifacts: [] }, error: null } }]), (error) => error.code === "CLAUDE_DEEPSEEK_RECOVERY_REQUEST_ID_INVALID");
  assert.throws(() => auditMcpRecoveries([failed, { ...corrected, arguments: { ...corrected.arguments, wait_seconds: 15 } }]), (error) => error.code === "CLAUDE_DEEPSEEK_RECOVERY_REQUEST_ID_INVALID");
  assert.throws(() => auditMcpRecoveries([failed, { ...corrected, arguments: { ...corrected.arguments, wait_for_job_id: "not-a-job-id" } }]), (error) => error.code === "CLAUDE_DEEPSEEK_RECOVERY_REQUEST_ID_INVALID");
});

test("one string-null get_case validation attempt permits only the immediate same-poll correction", () => {
  const failed = {
    ordinal: 1,
    tool: "problem_locator_get_case",
    arguments: { case_id: "case-id", wait_for_job_id: "null", wait_seconds: 0 },
    result: { ok: false, data: null, error: { code: "VALIDATION_ERROR", retryable: false, details: [{ field: "wait_for_job_id", actual: "null" }] } },
  };
  const corrected = { ordinal: 2, tool: "problem_locator_get_case", arguments: { case_id: "case-id", wait_seconds: 0 }, result: { ok: true, data: { case_view: {} }, error: null } };
  assert.deepEqual(auditMcpRecoveries([failed, corrected]).recoveries, [{ tool: failed.tool, code: "STRING_NULL_GET_CASE_VALIDATION", request_id: null, failed_ordinal: 1, corrected_ordinal: 2 }]);
  assert.equal(auditMcpRecoveries([failed, { ...corrected, arguments: { ...corrected.arguments, wait_for_job_id: null } }]).status, "PASS");
  assert.throws(() => auditMcpRecoveries([failed, { ...corrected, arguments: { ...corrected.arguments, wait_seconds: 30 } }]), (error) => error.code === "CLAUDE_DEEPSEEK_RECOVERY_REQUEST_ID_INVALID");
  assert.throws(() => auditMcpRecoveries([{ ...failed, result: { ok: false, data: null, error: { code: "VALIDATION_ERROR", retryable: false, details: [{ field: "case_id", actual: "null" }] } } }, corrected]), (error) => error.code === "CLAUDE_DEEPSEEK_RECOVERY_ERROR_INVALID");
  assert.throws(() => auditMcpRecoveries([failed, { ordinal: 2, tool: "problem_locator_list_artifacts", arguments: { case_id: "case-id" }, result: { ok: true, data: { artifacts: [] }, error: null } }, { ...corrected, ordinal: 3 }]), (error) => error.code === "CLAUDE_DEEPSEEK_RECOVERY_REQUEST_ID_INVALID");
});

test("client uses strict MCP, production Skill, exact Bash programs, and one fresh data root", () => {
  const source = fs.readFileSync(path.join(ROOT, "runtime", "claude-deepseek-e2e-runner.mjs"), "utf8");
  assert.match(source, /\.claude", "skills", "problem-locator-client/);
  assert.match(source, /mcpConfig/);
  assert.match(source, /Bash\(\/usr\/bin\/openssl:\*\)/);
  assert.match(source, /Bash\(\/usr\/bin\/stat:\*\)/);
  assert.match(source, /Bash\(\/usr\/bin\/curl:\*\)/);
  assert.match(source, /auditClientBash/);
  assert.match(source, /kind=USER_RESULT_ARCHIVE/);
  assert.match(source, /download_url/);
  assert.match(source, /auditDownloadedResultArchive/);
  assert.match(source, /const CLIENT_DISALLOWED_TOOLS = Object\.freeze\(\["Read", "Glob", "Grep", "Edit", "Write"\]\)/);
  assert.match(source, /disallowedTools: CLIENT_DISALLOWED_TOOLS, auditOnlyAllowedTools: CLIENT_DISALLOWED_TOOLS, allowToolErrors: true/);
  assert.match(source, /!CLIENT_DISALLOWED_TOOLS\.includes\(record\.name\) \|\| record\.is_error === true/);
  assert.match(source, /denied_tool_attempts: client\.denied/);
  assert.match(source, /claude-deepseek-bash-policy\.mjs/);
  assert.match(source, /一条物理命令行/);
  assert.match(source, /--max-time 60/);
  assert.match(source, /不得再运行 stat -c、ls 或其他 Bash 探测/);
  assert.match(source, /declared_size 必须是整数/);
  assert.match(source, /这两个字段禁止传 null/);
  assert.match(source, /不要先发送工具名再补参数/);
  assert.match(source, /appendSystemPrompt: CLIENT_TOOL_INPUT_SYSTEM_PROMPT/);
  assert.match(source, /全流程最多纠正一次/);
  assert.match(source, /紧接着用当前 case_id/);
  assert.match(source, /不传 Job UUID/);
  assert.match(source, /严禁传字符串 "null"/);
  assert.match(source, /CLAUDE_DEEPSEEK_SERVICE_JOB_FAILED/);
  assert.match(source, /const dataRoot = path\.join\(workRoot, "data-root"\)/);
  assert.match(source, /\.\.\.serviceSourceEnvironment\(sourceRoot\)/);
  assert.match(source, /treeBytes\(serviceEvidence\) \+ treeBytes\(serviceUsage\)/);
  assert.match(source, /TEST_FLOW_PROGRESS stage\.progress claude-deepseek/);
  assert.match(source, /problem-locator-seal-outcome-draft/);
  assert.match(source, /problem-locator-logparse/);
  assert.match(source, /copyTree\(cache\.registration_root, registrationRoot\)/);
  assert.equal(source.includes("registrationTemplate"), false);
  assert.equal(source.includes("Chrome"), false);
  assert.equal(source.includes("docker"), false);
  assert.equal(source.includes("restart"), false);
  assert.equal(source.includes("cross-job"), false);
});

test("service launcher receives the exact current mounted source root", () => {
  const sourceRoot = path.resolve("mounted-source");
  assert.deepEqual(serviceSourceEnvironment(sourceRoot), { TEST_FLOW_SOURCE_ROOT: sourceRoot });
  assert.throws(() => serviceSourceEnvironment("relative-source"), (error) => error.code === "CLAUDE_DEEPSEEK_SERVICE_SOURCE_ROOT_INVALID");
  const codexRunner = fs.readFileSync(path.join(ROOT, "..", "codex-luna", "runtime", "macos-codex-luna-e2e-runner.mjs"), "utf8");
  assert.match(codexRunner, /TEST_FLOW_SOURCE_ROOT: sourceRoot/);
});

test("Client tool-input policy identity binds the one bounded empty-call correction", () => {
  const identity = clientToolInputPolicyIdentity();
  assert.equal(identity.version, 3);
  assert.match(identity.sha256, /^[0-9a-f]{64}$/u);
  assert.ok(identity.utf8_size > 0);
});

test("Claude Client prompt removes the shared Job-UUID polling choice", () => {
  const prompt = claudeClientPrompt({ mapped: {}, archivePath: "/tmp/logs.zip", archive: { size: 1, sha256: "a".repeat(64) }, runId: "run", scenarioId: "scenario" });
  assert.doesNotMatch(prompt, /已知 job_id 时用 wait_for_job_id/u);
  assert.match(prompt, /wait_for_job_id 都必须是原生 JSON null/u);
  assert.match(prompt, /两次 revision refresh/u);
});

test("Client trace must load problem-locator-client exactly once as its first tool", () => {
  const skillRecord = { ordinal: 0, name: "Skill", input: { skill: "problem-locator-client" }, is_error: false };
  const valid = { records: [skillRecord, { ordinal: 1, name: "mcp__problem-locator__problem_locator_create_case", input: {}, is_error: false }], skills: [{ ordinal: 0, skill: "problem-locator-client" }] };
  assert.equal(auditClientSkillLoad(valid).status, "PASS");
  assert.throws(() => auditClientSkillLoad({ records: valid.records.slice(1), skills: [] }), (error) => error.code === "CLAUDE_DEEPSEEK_CLIENT_SKILL_LOAD_INVALID");
  assert.throws(() => auditClientSkillLoad({ records: [skillRecord, { ...skillRecord, ordinal: 1 }], skills: [{ ordinal: 0, skill: "problem-locator-client" }, { ordinal: 1, skill: "problem-locator-client" }] }), (error) => error.code === "CLAUDE_DEEPSEEK_CLIENT_SKILL_LOAD_INVALID");
  assert.throws(() => auditClientSkillLoad({ records: [{ ...skillRecord, input: { skill: "another-skill" } }], skills: [{ ordinal: 0, skill: "another-skill" }] }), (error) => error.code === "CLAUDE_DEEPSEEK_CLIENT_SKILL_LOAD_INVALID");
  assert.throws(() => auditClientSkillLoad({ records: [{ ...skillRecord, is_error: true }], skills: [] }), (error) => error.code === "CLAUDE_DEEPSEEK_CLIENT_SKILL_LOAD_INVALID");
});

test("E2E materializes one equivalent default Logparse product and binds its digest", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-logparse-config-"));
  try {
    const source = path.join(root, "source.json");
    const destination = path.join(root, "default.json");
    fs.writeFileSync(source, JSON.stringify({ schema_version: 2, pipeline: { keep_workspace: false }, products: { "rpc-skill-feasibility": { mechanisms: { rpc: { enabled: true } } } } }));
    const receipt = materializeDefaultLogparseConfig(source, destination);
    assert.equal(receipt.product, "default");
    assert.deepEqual(Object.keys(JSON.parse(fs.readFileSync(destination, "utf8")).products), ["default"]);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

function digest(bytes) { return crypto.createHash("sha256").update(bytes).digest("hex"); }
function localZip(entries) {
  const chunks = [];
  for (const [name, content] of entries) {
    const nameBytes = Buffer.from(name);
    const compressed = zlib.deflateRawSync(content);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(8, 8);
    local.writeUInt32LE(compressed.length, 18);
    local.writeUInt32LE(content.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    chunks.push(local, nameBytes, compressed);
  }
  const central = Buffer.alloc(4);
  central.writeUInt32LE(0x02014b50, 0);
  return Buffer.concat([...chunks, central]);
}

test("runner independently verifies downloaded Server v3 ZIP size, SHA, manifest, and target bytes", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-result-"));
  try {
    const result = Buffer.from("诊断结果\n", "utf8");
    const target = Buffer.from("target log\n", "utf8");
    const userResult = { sha256: "c".repeat(64) };
    const manifest = Buffer.from(JSON.stringify({ schema_version: 3, format_id: "problem-locator-result-archive-v3", problem_time: "2026-08-23T02:00:05.500Z", diagnosis_result_sha256: userResult.sha256, result_txt_sha256: digest(result), target_log_count: 1, target_logs: [{ ordinal: 1, archive_name: "client__rpc__slot_1__rpc_client.log", label: "client", requested_module: "rpc", slot: "1", process_name: "rpc_client", pid: null, size: target.length, sha256: digest(target) }] }), "utf8");
    const bytes = localZip([["result.txt", result], ["archive-manifest.json", manifest], ["client__rpc__slot_1__rpc_client.log", target]]);
    const filePath = path.join(root, "result.zip");
    fs.writeFileSync(filePath, bytes);
    const artifact = { size: bytes.length, sha256: digest(bytes) };
    const receipt = auditDownloadedResultArchive({ filePath, artifact, userResultArtifact: userResult, problemTime: "2026-08-23T02:00:05.500Z", targetLogs: { target_logs: [{ label: "client", size: target.length, content_sha256: digest(target) }] } });
    assert.equal(receipt.format_id, "problem-locator-result-archive-v3");
    fs.appendFileSync(filePath, "tamper");
    assert.throws(() => auditDownloadedResultArchive({ filePath, artifact, userResultArtifact: userResult, problemTime: "2026-08-23T02:00:05.500Z", targetLogs: { target_logs: [] } }), (error) => error.code === "CLAUDE_DEEPSEEK_RESULT_DOWNLOAD_IDENTITY_MISMATCH");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("specialized runtime audit distinguishes attachment preflight from the actual DIAGNOSE", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-specialized-"));
  try {
    const ref = { id: "diagnosis-skill/rpc-timeout-methods-v1", version: "1.0.0", content_hash: "d".repeat(64) };
    for (const id of ["route", "diagnose-preflight", "diagnose"]) fs.mkdirSync(path.join(root, "jobs", id), { recursive: true });
    fs.writeFileSync(path.join(root, "jobs", "route", "job_outcome.json"), JSON.stringify({ payload: { kind: "MATCHED", skill_ref: ref } }));
    const anchors = [{ label: "client", module: "rpc", slot: "1", process_name: "rpc_client", pid: null }, { label: "server", module: "rpc", slot: "1", process_name: "rpc_server", pid: null }];
    const brokerTargets = [{ label: "client", module_key: "rpc", module_name: "rpc", slot: "1", process_name: "rpc_client", pid: null }, { label: "server", module_key: "rpc", module_name: "rpc", slot: "1", process_name: "rpc_server", pid: null }];
    fs.writeFileSync(path.join(root, "jobs", "diagnose", "logparse_broker_audit.json"), JSON.stringify({ operations: [{ operation: "parse-targets", http_status: 200, request: { anchors }, result: { target_logs: brokerTargets } }] }));
    const server = { aggregate: { case: { generic_result: null, generic_result_v2: null } }, jobs: [{ job_id: "route", job_type: "ROUTE" }, { job_id: "diagnose-preflight", job_type: "DIAGNOSE", diagnosis_mode: "SPECIALIZED", skill_ref: ref }, { job_id: "diagnose", job_type: "DIAGNOSE", diagnosis_mode: "SPECIALIZED", skill_ref: ref }, { job_id: "review", job_type: "REVIEW", diagnosis_mode: null, skill_ref: ref }], outcome: { job_id: "diagnose" }, targetLogs: { target_logs: [{ label: "client" }, { label: "server" }] } };
    const helper = { status: "PASS", mode: "SERVER_PREPROCESS", helper_calls: 1, broker_calls: 1, helper_tool_ordinal: 0, broker_tool_ordinal: 1, operation: "parse-targets", direct_fallback: false, retry_count: 0, broker_entry_sha256: "e".repeat(64), stream_trace_sha256: "f".repeat(64), tool_sequence_sha256: "a".repeat(64) };
    const receipt = auditSpecializedRuntime({ dataRoot: root, server, cache: { manifest: { registration: { runtime_ref: ref } } }, facts: { client_slot: "1", client_process: "rpc_client", server_slot: "1", server_process: "rpc_server" }, finalCase: { selected_skill_ref: ref }, serverInvocations: [{ phase: "LOGPARSE", helper_audit: helper }, { phase: "REVIEW" }] });
    assert.equal(receipt.diagnosis_mode, "SPECIALIZED");
    assert.equal(receipt.diagnose_job_count, 2);
    assert.equal(receipt.attachment_preflight_job_count, 1);
    const bad = structuredClone(server);
    bad.jobs[1].diagnosis_mode = "GENERIC";
    assert.throws(() => auditSpecializedRuntime({ dataRoot: root, server: bad, cache: { manifest: { registration: { runtime_ref: ref } } }, facts: { client_slot: "1", client_process: "rpc_client", server_slot: "1", server_process: "rpc_server" }, finalCase: { selected_skill_ref: ref }, serverInvocations: [{ phase: "LOGPARSE", helper_audit: helper }, { phase: "REVIEW" }] }), (error) => error.code === "CLAUDE_DEEPSEEK_SPECIALIZED_LIFECYCLE_INVALID");
    const generic = structuredClone(server);
    generic.aggregate.case.generic_result = { status: "COMPLETED" };
    assert.throws(() => auditSpecializedRuntime({ dataRoot: root, server: generic, cache: { manifest: { registration: { runtime_ref: ref } } }, facts: { client_slot: "1", client_process: "rpc_client", server_slot: "1", server_process: "rpc_server" }, finalCase: { selected_skill_ref: ref }, serverInvocations: [{ phase: "LOGPARSE", helper_audit: helper }, { phase: "REVIEW" }] }), (error) => error.code === "CLAUDE_DEEPSEEK_GENERIC_RESULT_PRESENT");
    const missingPreflight = structuredClone(server);
    missingPreflight.jobs = missingPreflight.jobs.filter((job) => job.job_id !== "diagnose-preflight");
    assert.throws(() => auditSpecializedRuntime({ dataRoot: root, server: missingPreflight, cache: { manifest: { registration: { runtime_ref: ref } } }, facts: { client_slot: "1", client_process: "rpc_client", server_slot: "1", server_process: "rpc_server" }, finalCase: { selected_skill_ref: ref }, serverInvocations: [{ phase: "LOGPARSE", helper_audit: helper }, { phase: "REVIEW" }] }), (error) => error.code === "CLAUDE_DEEPSEEK_SPECIALIZED_LIFECYCLE_INVALID");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("client-only settings overlay adds three Bash rules and one test-owned PreToolUse policy without copying provider Hooks", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-client-settings-"));
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
  for (const evidence of ["client-skill.json", "mcp-tool-calls.json", "attachment.json", "final-case.json", "artifact-index.json", "artifact-download.json", "specialized-runtime.json", "logparse-config.json", "scenario-oracle.json", "server-events.ndjson", "model-usage.json", "security-audit.json", "adapter-receipt.json"]) assert.ok(source.includes(evidence), evidence);
  for (const audit of ["auditMcpToolCalls", "auditUploadedAttachment", "artifactConsistency", "auditDownloadedResultArchive", "auditSpecializedRuntime", "auditOracle", "combineServerEvents", "auditClaudeInvocations", "secretScan"]) assert.ok(source.includes(audit), audit);
});

test("safe E2E error exposes only closed code and message", () => {
  assert.deepEqual(safeE2EError({ code: "CLOSED", message: "safe", details: { token: "secret" } }), { schema_version: 1, status: "FAIL", code: "CLOSED", message: "safe" });
});
