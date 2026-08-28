import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Readable, Writable } from "node:stream";
import test from "node:test";

import {
  CODEX_LUNA_MODEL_CERT_MAX_CALLS,
  CODEX_LUNA_MODEL_CERT_NORMAL_CALLS,
  modelRoleDeveloperInstructions,
  parseEvidenceV2RolePrompt,
  parseModelCertWrapperArguments,
  runModelRoleInvocation,
} from "../runtime/macos-codex-luna-model-cert-wrapper.mjs";

const SPECIALIST_PROMPT = `bounded production context\n\n<<<METHODS_EVIDENCE_V2_ROLE>>>\nRole: Specialist. Attempt: primary evaluation.\nUse only frozen inputs.\nWrite only output/method-diagnosis.draft.json.\n<<<END METHODS_EVIDENCE_V2_ROLE>>>\n`;
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

function fakeTrace(workspace, response) {
  fs.writeFileSync(path.join(workspace, "output", "method-diagnosis.draft.json"), JSON.stringify(response));
  return {
    thread_id: "thread-1",
    turn_id: "turn-1",
    command_receipts: [{ item_id: "command-1", status: "completed", exit_code: 0 }],
    usage: { input_tokens: 10, cached_input_tokens: 0, cache_write_input_tokens: 0, output_tokens: 5, reasoning_output_tokens: 0, total_tokens: 15 },
    app_server: {
      permission_profile_id: "test-flow-app-server-external-auth-v1-service",
      invocation_mode: "service",
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

test("model cert wrapper freezes two normal calls and a four-call hard cap", () => {
  assert.equal(CODEX_LUNA_MODEL_CERT_NORMAL_CALLS, 2);
  assert.equal(CODEX_LUNA_MODEL_CERT_MAX_CALLS, 4);
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
  const response = [{ evaluation_ref: "evaluation-1", verdict: "CONFIRMED", reason: "Frozen Evidence satisfies the method." }];
  const output = [];
  const receipt = await runModelRoleInvocation(values, {
    stdin: Readable.from([SPECIALIST_PROMPT]),
    stdout: new Writable({ write(chunk, encoding, callback) { output.push(chunk.toString()); callback(); } }),
    ambient: {},
    runAppServerCall: async () => fakeTrace(workspace, response),
  });
  assert.equal(receipt.role, "SPECIALIST");
  assert.equal(receipt.attempt, "PRIMARY");
  assert.equal(receipt.output.json_root, "ARRAY");
  assert.equal(receipt.output.normalized, false);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(workspace, "output", "method-diagnosis.draft.json"), "utf8")), response);
  assert.equal(JSON.parse(fs.readFileSync(path.join(values["usage-root"], "specialist-primary.json"), "utf8")).usage.total_tokens, 15);
  assert.deepEqual(JSON.parse(output.at(-1)), { attempt: "PRIMARY", role: "SPECIALIST", status: "PASS" });
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
