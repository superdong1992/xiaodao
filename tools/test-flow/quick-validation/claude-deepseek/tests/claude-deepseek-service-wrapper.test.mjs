import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Readable, Writable } from "node:stream";
import test from "node:test";

import {
  auditRoleWorkspace,
  claimRoleAttempt,
  parseArguments,
  parseMethodsRolePrompt,
  readRoleInvocationReceipts,
  roleToolPolicy,
  runServiceInvocation,
} from "../runtime/claude-deepseek-service-wrapper.mjs";

function prompt(role, attempt) {
  const output = role === "Specialist" ? "output/method-diagnosis.draft.json" : "output/method-review.draft.json";
  return `frozen context\n<<<METHODS_EVIDENCE_V2_ROLE>>>\nRole: ${role}. Attempt: ${attempt}.\nWrite one JSON root array whose items contain only evaluation_ref, verdict, and reason.\nWrite only ${output}.\n<<<END METHODS_EVIDENCE_V2_ROLE>>>\n`;
}

function workspace(root) {
  const inputs = path.join(root, "inputs");
  fs.mkdirSync(path.join(root, "output"), { recursive: true });
  fs.mkdirSync(inputs, { recursive: true });
  for (const name of ["request.json", "method-evidence-graph.json", "method-evaluation-plan.json"]) fs.writeFileSync(path.join(inputs, name), "{}\n");
}

test("model-cert wrapper accepts only its frozen provider inputs", () => {
  const argv = ["--claude-entry", "/cli.js", "--settings", "/settings.json", "--config-root", "/config", "--private-root", "/private", "--evidence-root", "/evidence", "--usage-root", "/usage", "--run-id", "run"];
  assert.equal(parseArguments(argv)["run-id"], "run");
  assert.throws(() => parseArguments([...argv, "--adapter", "/tmp/other"]), (error) => error.code === "CLAUDE_DEEPSEEK_SERVICE_ARGUMENT_UNKNOWN");
});

test("production role marker binds Specialist and Reviewer primary or only repair", () => {
  assert.deepEqual(parseMethodsRolePrompt(prompt("Specialist", "primary evaluation")), { role: "SPECIALIST", attempt: "PRIMARY", output: "output/method-diagnosis.draft.json" });
  assert.deepEqual(parseMethodsRolePrompt(prompt("Reviewer", "only repair")), { role: "REVIEWER", attempt: "REPAIR", output: "output/method-review.draft.json" });
  assert.throws(() => parseMethodsRolePrompt("Candidate diagnosis"), (error) => error.code === "CLAUDE_DEEPSEEK_ROLE_MARKER_INVALID");
});

test("each role receives one primary and at most one repair with a four-call total cap", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-role-claims-"));
  try {
    claimRoleAttempt(root, "SPECIALIST", "PRIMARY");
    assert.throws(() => claimRoleAttempt(root, "SPECIALIST", "PRIMARY"), (error) => error.code === "CLAUDE_DEEPSEEK_ROLE_RETRY_FORBIDDEN");
    claimRoleAttempt(root, "SPECIALIST", "REPAIR");
    assert.throws(() => claimRoleAttempt(root, "SPECIALIST", "REPAIR"), (error) => error.code === "CLAUDE_DEEPSEEK_ROLE_REPAIR_FORBIDDEN");
    claimRoleAttempt(root, "REVIEWER", "PRIMARY");
    claimRoleAttempt(root, "REVIEWER", "REPAIR");
    assert.equal(fs.readdirSync(path.join(root, "model-role-claims")).length, 4);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("role workspace contains Graph and Plan but no raw logs and accepts one exact Write", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-role-workspace-"));
  try {
    workspace(root);
    const output = path.join(root, "output", "method-diagnosis.draft.json");
    const content = '[{"evaluation_ref":"eval-a","verdict":"CONFIRMED","reason":"ok"}]';
    fs.writeFileSync(output, content);
    const receipt = auditRoleWorkspace({
      workspaceRoot: root,
      roleSpec: { role: "SPECIALIST", attempt: "PRIMARY", output: "output/method-diagnosis.draft.json" },
      processResult: { records: [
        { name: "Read", is_error: false, input: { file_path: path.join(root, "inputs", "method-evaluation-plan.json") } },
        { name: "Write", is_error: false, input: { file_path: output, content } },
      ] },
    });
    assert.equal(receipt.harness_normalized, false);
    fs.mkdirSync(path.join(root, "inputs", "target-logs"));
    assert.throws(() => auditRoleWorkspace({ workspaceRoot: root, roleSpec: { role: "SPECIALIST", attempt: "PRIMARY", output: "output/method-diagnosis.draft.json" }, processResult: { records: [] } }), (error) => error.code === "CLAUDE_DEEPSEEK_ROLE_INPUT_LEAK");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("tool policy exposes Read/Write only and binds one role draft", () => {
  const root = path.resolve("role-workspace");
  const policy = roleToolPolicy({ workspaceRoot: root, output: "output/method-review.draft.json" });
  assert.deepEqual(policy.tools, ["Read", "Write"]);
  assert.equal(policy.shell, false);
  assert.equal(policy.network, false);
  assert.equal(policy.writable_scope, "output/method-review.draft.json");
  assert.match(policy.sha256, /^[a-f0-9]{64}$/u);
});

test("wrapper preserves the raw evaluation array and records the exact production prompt", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-role-run-"));
  const previous = process.cwd();
  try {
    workspace(root);
    process.chdir(root);
    const values = {
      "claude-entry": path.join(root, "cli.js"), settings: path.join(root, "settings.json"), "config-root": path.join(root, "config"),
      "private-root": path.join(root, "private"), "evidence-root": path.join(root, "evidence"), "usage-root": path.join(root, "usage"), "run-id": "run",
    };
    for (const target of [values["claude-entry"], values.settings]) fs.writeFileSync(target, "fixture");
    fs.mkdirSync(values["config-root"]);
    const rawPrompt = prompt("Specialist", "primary evaluation");
    const output = path.join(root, "output", "method-diagnosis.draft.json");
    const content = '[{"evaluation_ref":"eval-a","verdict":"CONFIRMED","reason":"ok"}]';
    const chunks = [];
    const stdout = new Writable({ write(chunk, _encoding, callback) { chunks.push(Buffer.from(chunk)); callback(); } });
    const receipt = await runServiceInvocation(values, {
      stdin: Readable.from([rawPrompt]), stdout,
      runClaude: async (options, hooks) => {
        hooks.onProgress();
        fs.writeFileSync(output, content);
        return {
          receipt: { schema_version: 1, invocation_id: options.invocationId, phase: "SPECIALIST", model: "deepseek-v4-flash[1m]", attempt: 1, retry: 0, status: "PASS", terminal: true, turns: 1, wall_timeout_seconds: 600, usage: { input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, cost_usd: 0 } },
          records: [{ name: "Read", is_error: false, input: { file_path: path.join(root, "inputs", "method-evaluation-plan.json") } }, { name: "Write", is_error: false, input: { file_path: output, content } }],
          skills: [], bash: [], mcp: [], denied: [], events: [{ type: "result", result: "done" }],
        };
      },
    });
    assert.equal(receipt.workspace_audit.output_sha256, receipt.workspace_audit.output_sha256);
    assert.equal(receipt.prompt.utf8_size, Buffer.byteLength(rawPrompt));
    assert.equal(fs.readFileSync(output, "utf8"), content);
    assert.equal(fs.readFileSync(path.join(root, "evidence", "model-role-invocations", "specialist-primary.progress"), "utf8"), ".\n");
    assert.match(Buffer.concat(chunks).toString("utf8"), /done/u);
  } finally { process.chdir(previous); fs.rmSync(root, { recursive: true, force: true }); }
});

test("failed provider invocation writes one closed role receipt with terminal usage", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-role-failure-"));
  const previous = process.cwd();
  try {
    workspace(root);
    process.chdir(root);
    const values = {
      "claude-entry": path.join(root, "cli.js"), settings: path.join(root, "settings.json"), "config-root": path.join(root, "config"),
      "private-root": path.join(root, "private"), "evidence-root": path.join(root, "evidence"), "usage-root": path.join(root, "usage"), "run-id": "run",
    };
    for (const target of [values["claude-entry"], values.settings]) fs.writeFileSync(target, "fixture");
    fs.mkdirSync(values["config-root"]);
    const error = new Error("provider failed");
    error.code = "CLAUDE_DEEPSEEK_PROCESS_FAILED";
    error.details = {
      terminal: {
        turns: 1,
        usage: { schema_version: 1, input_tokens: 11, output_tokens: 3, cache_creation_input_tokens: 0, cache_read_input_tokens: 2, total_tokens: 16, cost_usd: 0.01 },
      },
    };
    await assert.rejects(runServiceInvocation(values, {
      stdin: Readable.from([prompt("Specialist", "primary evaluation")]),
      stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
      runClaude: async () => { throw error; },
    }), { code: "CLAUDE_DEEPSEEK_PROCESS_FAILED" });
    const [receipt] = readRoleInvocationReceipts(values["usage-root"]);
    assert.equal(receipt.status, "FAIL");
    assert.equal(receipt.workflow, "SPECIALIST:PRIMARY");
    assert.equal(receipt.role, "SPECIALIST");
    assert.equal(receipt.evaluation_attempt, "PRIMARY");
    assert.equal(receipt.failure_code, "CLAUDE_DEEPSEEK_PROCESS_FAILED");
    assert.equal(receipt.usage_complete, true);
    assert.equal(receipt.usage.total_tokens, 16);
    assert.equal(receipt.wall_timeout_seconds, 600);
    assert.ok(Date.parse(receipt.finished_at_utc) >= Date.parse(receipt.started_at_utc));
    assert.deepEqual(receipt, JSON.parse(fs.readFileSync(path.join(values["evidence-root"], "model-role-invocations", "specialist-primary.receipt.json"), "utf8")));
  } finally { process.chdir(previous); fs.rmSync(root, { recursive: true, force: true }); }
});
