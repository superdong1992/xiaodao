import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Readable, Writable } from "node:stream";
import test from "node:test";

import {
  CODEX_LUNA_MODEL_CERT_MAX_CALLS,
  CODEX_LUNA_MODEL_CERT_BLIND_REVIEW_MAX_CALLS,
  CODEX_LUNA_MODEL_CERT_BLIND_REVIEW_NORMAL_CALLS,
  CODEX_LUNA_MODEL_CERT_NORMAL_CALLS,
  modelRoleDeveloperInstructions,
  parseEvidenceV2RolePrompt,
  parseModelCertWrapperArguments,
  readModelCertInvocationReceipts,
  runModelRoleInvocation,
} from "../runtime/macos-codex-luna-model-cert-wrapper.mjs";

const SPECIALIST_PROMPT = `bounded production context\n\n<<<METHODS_EVIDENCE_V2_ROLE>>>\nRole: Specialist. Attempt: primary evaluation.\nUse only frozen inputs.\nWrite only output/method-diagnosis.draft.json.\n<<<END METHODS_EVIDENCE_V2_ROLE>>>\n`;
const SPECIALIST_REPAIR_PROMPT = `bounded production context\n\n<<<METHODS_EVIDENCE_V2_ROLE>>>\nRole: Specialist. Attempt: only repair.\nUse only frozen inputs.\nWrite only output/method-diagnosis.draft.json.\nThe previous response failed.\n<<<END METHODS_EVIDENCE_V2_ROLE>>>\n`;
const REVIEWER_REPAIR_PROMPT = `bounded blind context\n\n<<<METHODS_EVIDENCE_V2_ROLE>>>\nRole: Reviewer. Attempt: only repair.\nUse only frozen inputs.\nWrite only output/method-review.draft.json.\nThe previous response failed.\n<<<END METHODS_EVIDENCE_V2_ROLE>>>\n`;

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "codex-luna-model-cert-"));
  const workspace = path.join(root, "workspace");
  for (const directory of [workspace, "inputs", "output", "runtime"].map((entry) => path.isAbsolute(entry) ? entry : path.join(workspace, entry))) fs.mkdirSync(directory, { recursive: true });
  for (const name of ["request.json", "method-evidence-graph.json", "method-evaluation-plan.json"]) fs.writeFileSync(path.join(workspace, "inputs", name), "{}\n");
  const auth = path.join(root, "auth.json");
  fs.writeFileSync(auth, JSON.stringify({
    auth_mode: "chatgpt",
    tokens: {
      access_token: "access-token-canary",
      refresh_token: "refresh-token-canary",
      id_token: "id-token-canary",
      account_id: "account-1",
    },
  }));
  const values = {
    "codex-entry": path.join(root, "codex"),
    "auth-source": auth,
    "skill-source": path.join(root, "SKILL.md"),
    "expected-cli-version": "0.149.0-alpha.4.1",
    "private-root": path.join(root, "private"),
    "evidence-root": path.join(root, "evidence"),
    "usage-root": path.join(root, "usage"),
    "run-id": "test-run",
  };
  fs.writeFileSync(values["skill-source"], "---\nname: codex-luna-evidence-v2-evaluator\n---\n");
  return { root, workspace, values };
}

function assertWorkspaceRestored(workspace) {
  assert.deepEqual(fs.readdirSync(workspace).sort(), ["inputs", "output", "runtime"]);
  assert.equal(
    fs.existsSync(path.join(workspace, "runtime", "test-flow-codex-project")),
    false,
  );
}

function fakeTrace(workspace, response, { writeOutput = true } = {}) {
  if (process.platform === "linux") {
    for (const name of [".agents", ".codex", ".git"]) {
      fs.mkdirSync(path.join(workspace, name));
    }
  }
  if (writeOutput) {
    fs.writeFileSync(path.join(workspace, "output", "method-diagnosis.draft.json"), JSON.stringify(response));
  }
  return {
    thread_id: "thread-1",
    turn_id: "turn-1",
    command_receipts: [{ item_id: "command-1", status: "completed", exit_code: 0 }],
    usage: { input_tokens: 10, cached_input_tokens: 0, cache_write_input_tokens: 0, output_tokens: 5, reasoning_output_tokens: 0, total_tokens: 15 },
    app_server: {
      permission_profile: {
        id: "test-flow-codex-luna-service",
        invocation_mode: "service",
      },
      developer_instructions: { sha256: "1".repeat(64) },
      codex_home: { config_sha256: "2".repeat(64) },
      turn: { mcp_tool_call_count: 0 },
    },
  };
}

test("Evidence V2 role parser accepts only the production role/attempt/output marker", () => {
  assert.deepEqual(parseEvidenceV2RolePrompt(SPECIALIST_PROMPT), {
    role: "SPECIALIST",
    attempt: "PRIMARY",
    output: "output/method-diagnosis.draft.json",
  });
  assert.deepEqual(parseEvidenceV2RolePrompt(REVIEWER_REPAIR_PROMPT), {
    role: "REVIEWER",
    attempt: "REPAIR",
    output: "output/method-review.draft.json",
  });
  assert.throws(() => parseEvidenceV2RolePrompt(SPECIALIST_PROMPT.replace("diagnosis", "review")), { code: "CODEX_LUNA_MODEL_CERT_OUTPUT_PATH_MISMATCH" });
  assert.throws(() => parseEvidenceV2RolePrompt(`${SPECIALIST_PROMPT}trailing`), { code: "CODEX_LUNA_MODEL_CERT_ROLE_MARKER_INVALID" });
});

test("model cert wrapper freezes default and blind-review call caps", () => {
  assert.equal(CODEX_LUNA_MODEL_CERT_NORMAL_CALLS, 1);
  assert.equal(CODEX_LUNA_MODEL_CERT_MAX_CALLS, 2);
  assert.equal(CODEX_LUNA_MODEL_CERT_BLIND_REVIEW_NORMAL_CALLS, 2);
  assert.equal(CODEX_LUNA_MODEL_CERT_BLIND_REVIEW_MAX_CALLS, 4);
  const instructions = modelRoleDeveloperInstructions("/tmp/evidence-v2-role", parseEvidenceV2RolePrompt(SPECIALIST_PROMPT));
  assert.match(instructions, /只写 output\/method-diagnosis\.draft\.json/);
  assert.match(instructions, /不得生成 Evidence、Candidate、Artifact、grounding、PARTIAL/);
});

test("wrapper arguments are closed and complete", () => {
  const args = [
    "--codex-entry", "/codex",
    "--auth-source", "/auth",
    "--skill-source", "/skill",
    "--expected-cli-version", "version",
    "--private-root", "/private",
    "--evidence-root", "/evidence",
    "--usage-root", "/usage",
    "--run-id", "run",
  ];
  assert.equal(parseModelCertWrapperArguments(args)["run-id"], "run");
  assert.throws(() => parseModelCertWrapperArguments([...args, "--run-id", "twice"]), { code: "CODEX_LUNA_MODEL_CERT_ARGUMENT_DUPLICATE" });
});

test("fake app-server follows the same wrapper path and leaves validation to production Runtime", async (context) => {
  const { root, workspace, values } = fixture();
  const previous = process.cwd();
  process.chdir(workspace);
  context.after(() => process.chdir(previous));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const response = [{ evaluation_ref: "evaluation-1", verdict: "CONFIRMED", supporting_event_refs: ["event-1"], reason: "Frozen Evidence satisfies the method." }];
  const output = [];
  const receipt = await runModelRoleInvocation(values, {
    stdin: Readable.from([SPECIALIST_PROMPT]),
    stdout: new Writable({ write(chunk, encoding, callback) { output.push(chunk.toString()); callback(); } }),
    ambient: {},
    runAppServerCall: async ({ workspaceRoot }) => fakeTrace(workspaceRoot, response),
  });
  assert.equal(receipt.role, "SPECIALIST");
  assert.equal(receipt.attempt, "PRIMARY");
  assert.equal(receipt.output.json_root, "ARRAY");
  assert.equal(receipt.output.normalized, false);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(workspace, "output", "method-diagnosis.draft.json"), "utf8")), response);
  assert.equal(JSON.parse(fs.readFileSync(path.join(values["usage-root"], "specialist-primary.json"), "utf8")).usage.total_tokens, 15);
  assert.deepEqual(JSON.parse(output.at(-1)), { attempt: "PRIMARY", role: "SPECIALIST", status: "PASS" });
  assertWorkspaceRestored(workspace);
});

test("Linux repair cannot reuse the inherited primary output as a new model response", {
  skip: process.platform !== "linux",
}, async (context) => {
  const { root, workspace, values } = fixture();
  const previous = process.cwd();
  process.chdir(workspace);
  context.after(() => process.chdir(previous));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const primary = [{ evaluation_ref: "evaluation-primary", verdict: "UNKNOWN", supporting_event_refs: [], reason: "primary" }];
  await runModelRoleInvocation(values, {
    stdin: Readable.from([SPECIALIST_PROMPT]),
    stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
    ambient: {},
    runAppServerCall: async ({ workspaceRoot }) => fakeTrace(workspaceRoot, primary),
  });
  await assert.rejects(
    runModelRoleInvocation(values, {
      stdin: Readable.from([SPECIALIST_REPAIR_PROMPT]),
      stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
      ambient: {},
      runAppServerCall: async ({ workspaceRoot }) => {
        assert.equal(fs.existsSync(path.join(workspaceRoot, "output", "method-diagnosis.draft.json")), false);
        return fakeTrace(workspaceRoot, [], { writeOutput: false });
      },
    }),
    { code: "CODEX_LUNA_MODEL_CERT_OUTPUT_MISSING" },
  );
  assert.deepEqual(
    JSON.parse(fs.readFileSync(path.join(workspace, "output", "method-diagnosis.draft.json"), "utf8")),
    primary,
  );
  assertWorkspaceRestored(workspace);
});

test("Linux repair publishes a new response even when the restored workspace has no primary draft", {
  skip: process.platform !== "linux",
}, async (context) => {
  const { root, workspace, values } = fixture();
  const previous = process.cwd();
  process.chdir(workspace);
  context.after(() => process.chdir(previous));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  await runModelRoleInvocation(values, {
    stdin: Readable.from([SPECIALIST_PROMPT]),
    stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
    ambient: {},
    runAppServerCall: async ({ workspaceRoot }) => fakeTrace(workspaceRoot, []),
  });
  fs.rmSync(path.join(workspace, "output", "method-diagnosis.draft.json"));
  const repair = [{ evaluation_ref: "evaluation-repair", verdict: "CONFIRMED", supporting_event_refs: ["event-repair"], reason: "repair" }];
  await runModelRoleInvocation(values, {
    stdin: Readable.from([SPECIALIST_REPAIR_PROMPT]),
    stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
    ambient: {},
    runAppServerCall: async ({ workspaceRoot }) => fakeTrace(workspaceRoot, repair),
  });
  assert.deepEqual(
    JSON.parse(fs.readFileSync(path.join(workspace, "output", "method-diagnosis.draft.json"), "utf8")),
    repair,
  );
  assertWorkspaceRestored(workspace);
});

test("completed app-server trace remains complete when role output audit fails", async (context) => {
  const { root, workspace, values } = fixture();
  const previous = process.cwd();
  process.chdir(workspace);
  context.after(() => process.chdir(previous));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  await assert.rejects(runModelRoleInvocation(values, {
    stdin: Readable.from([SPECIALIST_PROMPT]),
    stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
    ambient: {},
    runAppServerCall: async ({ workspaceRoot }) => {
      const trace = fakeTrace(workspaceRoot, []);
      fs.rmSync(path.join(workspaceRoot, "output", "method-diagnosis.draft.json"));
      return trace;
    },
  }), { code: "CODEX_LUNA_MODEL_CERT_OUTPUT_MISSING" });
  const [receipt] = readModelCertInvocationReceipts(values["usage-root"], { allowFailurePrefix: true });
  assert.equal(receipt.status, "FAIL");
  assert.equal(receipt.failure_code, "CODEX_LUNA_MODEL_CERT_OUTPUT_MISSING");
  assert.equal(receipt.usage_complete, true);
  assert.equal(receipt.usage.total_tokens, 15);
  assert.deepEqual(receipt.profile, {
    permission_profile_id: "test-flow-codex-luna-service",
    config_sha256: "2".repeat(64),
    developer_instructions_sha256: "1".repeat(64),
  });
  assert.deepEqual(receipt.tool_policy, {
    invocation_mode: "service",
    mcp_tool_call_count: 0,
    command_count: 1,
    output_normalized: false,
  });
  assert.equal(receipt.thread_id, "thread-1");
  assert.equal(receipt.turn_id, "turn-1");
  assert.equal(receipt.output, null);
  assertWorkspaceRestored(workspace);
});

test("completed app-server trace cannot pass with an incomplete profile or tool policy", async () => {
  const mutations = [
    ["command receipts", (trace) => { delete trace.command_receipts; }],
    ["permission profile", (trace) => { delete trace.app_server.permission_profile; }],
    ["Codex home identity", (trace) => { delete trace.app_server.codex_home; }],
    ["developer instructions identity", (trace) => { delete trace.app_server.developer_instructions; }],
  ];
  for (const [label, mutate] of mutations) {
    const { root, workspace, values } = fixture();
    const previous = process.cwd();
    process.chdir(workspace);
    try {
      await assert.rejects(
        runModelRoleInvocation(values, {
          stdin: Readable.from([SPECIALIST_PROMPT]),
          stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
          ambient: {},
          runAppServerCall: async ({ workspaceRoot }) => {
            const trace = fakeTrace(workspaceRoot, []);
            mutate(trace);
            return trace;
          },
        }),
        { code: "CODEX_LUNA_MODEL_CERT_TRACE_INVALID" },
        label,
      );
      const [receipt] = readModelCertInvocationReceipts(values["usage-root"], { allowFailurePrefix: true });
      assert.equal(receipt.status, "FAIL", label);
      assert.equal(receipt.failure_code, "CODEX_LUNA_MODEL_CERT_TRACE_INVALID", label);
    } finally {
      process.chdir(previous);
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
});

test("Reviewer repair requires a claimed primary attempt", async (context) => {
  const { root, workspace, values } = fixture();
  const previous = process.cwd();
  process.chdir(workspace);
  context.after(() => process.chdir(previous));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  await assert.rejects(
    runModelRoleInvocation(values, {
      stdin: Readable.from([REVIEWER_REPAIR_PROMPT]),
      stdout: new Writable({ write(chunk, encoding, callback) { callback(); } }),
      ambient: {},
      runAppServerCall: async () => { throw new Error("must not call app-server"); },
    }),
    { code: "CODEX_LUNA_MODEL_CERT_REPAIR_WITHOUT_PRIMARY" },
  );
});

test("failed app-server invocation writes one closed role receipt with terminal usage", async (context) => {
  const { root, workspace, values } = fixture();
  const previous = process.cwd();
  process.chdir(workspace);
  context.after(() => process.chdir(previous));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const error = new Error("provider failed");
  error.code = "CODEX_LUNA_APP_SERVER_ERROR_NOTIFICATION";
  error.details = {
    usage: { input_tokens: 12, cached_input_tokens: 2, cache_write_input_tokens: 0, output_tokens: 4, reasoning_output_tokens: 1, total_tokens: 16 },
    thread_id: "thread-failed",
    turn_id: "turn-failed",
  };
  await assert.rejects(runModelRoleInvocation(values, {
    stdin: Readable.from([SPECIALIST_PROMPT]),
    stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
    ambient: {},
    runAppServerCall: async () => { throw error; },
  }), { code: "CODEX_LUNA_APP_SERVER_ERROR_NOTIFICATION" });
  const [receipt] = readModelCertInvocationReceipts(values["usage-root"], { allowFailurePrefix: true });
  assert.equal(receipt.status, "FAIL");
  assert.equal(receipt.workflow, "SPECIALIST:PRIMARY");
  assert.equal(receipt.role, "SPECIALIST");
  assert.equal(receipt.attempt, "PRIMARY");
  assert.equal(receipt.failure_code, "CODEX_LUNA_APP_SERVER_ERROR_NOTIFICATION");
  assert.equal(receipt.usage_complete, true);
  assert.equal(receipt.usage.total_tokens, 16);
  assert.deepEqual(receipt, JSON.parse(fs.readFileSync(path.join(values["evidence-root"], "model-invocations", "specialist-primary.receipt.json"), "utf8")));
});

test("invocation collector preserves the only legal role order and rejects extra receipts", (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "codex-luna-model-receipts-"));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const base = {
    schema_version: 1,
    invocation_id: "run:specialist-primary",
    provider: "openai-codex-app-server",
    model: "gpt-5.6-luna",
    reasoning_effort: "medium",
    status: "PASS",
    terminal: true,
  };
  const write = (name, role, attempt) => fs.writeFileSync(path.join(root, name), JSON.stringify({ ...base, invocation_id: `run:${role}:${attempt}`, role, attempt, repair: attempt === "REPAIR" }));
  write("specialist-primary.json", "SPECIALIST", "PRIMARY");
  assert.deepEqual(readModelCertInvocationReceipts(root).map((item) => `${item.role}:${item.attempt}`), ["SPECIALIST:PRIMARY"]);
  assert.throws(
    () => readModelCertInvocationReceipts(root, { evaluationMode: "BLIND_CONSENSUS" }),
    { code: "CODEX_LUNA_MODEL_CERT_USAGE_RECEIPT_MISSING" },
  );
  write("reviewer-primary.json", "REVIEWER", "PRIMARY");
  assert.throws(
    () => readModelCertInvocationReceipts(root),
    { code: "CODEX_LUNA_MODEL_CERT_USAGE_FILE_UNEXPECTED" },
  );
  assert.deepEqual(readModelCertInvocationReceipts(root, { evaluationMode: "BLIND_CONSENSUS" }).map((item) => `${item.role}:${item.attempt}`), ["SPECIALIST:PRIMARY", "REVIEWER:PRIMARY"]);
  write("specialist-repair.json", "SPECIALIST", "REPAIR");
  assert.deepEqual(readModelCertInvocationReceipts(root, { evaluationMode: "BLIND_CONSENSUS" }).map((item) => `${item.role}:${item.attempt}`), ["SPECIALIST:PRIMARY", "SPECIALIST:REPAIR", "REVIEWER:PRIMARY"]);
  fs.writeFileSync(path.join(root, "fifth.json"), "{}\n");
  assert.throws(() => readModelCertInvocationReceipts(root, { evaluationMode: "BLIND_CONSENSUS" }), { code: "CODEX_LUNA_MODEL_CERT_USAGE_FILE_UNEXPECTED" });
  assert.throws(() => readModelCertInvocationReceipts(root, { evaluationMode: "UNKNOWN" }), { code: "CODEX_LUNA_MODEL_CERT_EVALUATION_MODE_INVALID" });
});
