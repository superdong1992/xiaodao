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

const GRAPH_REF = `graph-${"a".repeat(64)}`;
const PLAN_REF = `plan-${"b".repeat(64)}`;
const CASE_ID = "10000000-0000-4000-8000-000000000001";
const SOURCE_JOB_ID = "20000000-0000-4000-8000-000000000001";
const REVIEW_JOB_ID = "30000000-0000-4000-8000-000000000001";
const EVALUATION_ID = "40000000-0000-4000-8000-000000000001";
const SKILL_REF = Object.freeze({
  id: "diagnosis-skill/rpc-timeout",
  version: "1.0.0",
  content_hash: "e".repeat(64),
});
const EVALUATION_INPUT = Object.freeze({
  schema_version: 2,
  evidence_graph_ref: GRAPH_REF,
  plan_ref: PLAN_REF,
  limitations: [],
  sources: [
    { id: 1, source_id: "client", relative_path: "logs/client.log" },
    { id: 2, source_id: "server", relative_path: "logs/server.log" },
  ],
  observations: [{ id: 1, source_id: "server", line_number: 7, line: "RPC_TIMEOUT request=42" }],
  markers: [{ id: 1, literal: "RPC_TIMEOUT" }],
  evaluations: [{
    evaluation_ref: `eval-${"c".repeat(64)}`,
    method_id: "rpc-timeout",
    method_priority: 1,
    events: [{
      event_ref: `event-${"d".repeat(64)}`,
      identity_tokens: ["request=42"],
      matches: [{ observation_id: 1, marker_id: 1, method_marker_index: 1 }],
    }],
  }],
});
const REVIEW_TARGET = Object.freeze({
  schema_version: 2,
  evaluation_id: EVALUATION_ID,
  source_job_id: SOURCE_JOB_ID,
  graph_ref: GRAPH_REF,
  plan_ref: PLAN_REF,
  skill_ref: SKILL_REF,
  reviewed_state_revision: 3,
});

function rolePrompt({
  role = "SPECIALIST",
  attempt = "PRIMARY",
  evaluationInput = EVALUATION_INPUT,
  rolePayload = null,
} = {}) {
  const specialist = role === "SPECIALIST";
  const roleLabel = specialist ? "Specialist" : "Reviewer";
  const attemptLabel = attempt === "PRIMARY" ? "primary evaluation" : "only repair";
  const output = specialist ? "output/method-diagnosis.draft.json" : "output/method-review.draft.json";
  const section = specialist ? "EVIDENCE" : "REVIEW_TARGET";
  const roleInput = JSON.stringify(rolePayload ?? (specialist
    ? {
        schema_version: 2,
        role,
        job_id: SOURCE_JOB_ID,
        case_id: CASE_ID,
        request_path: "inputs/request.json",
        evaluation_input: evaluationInput,
      }
    : {
        schema_version: 2,
        role,
        target: REVIEW_TARGET,
        request_path: "inputs/request.json",
        evaluation_input: evaluationInput,
      }));
  return `bounded production context\n<<<SECTION 6 ${section}>>>\n${roleInput}\n<<<END SECTION>>>\n\n<<<METHODS_EVIDENCE_V2_ROLE>>>\nRole: ${roleLabel}. Attempt: ${attemptLabel}.\nUse only frozen inputs.\nWrite only ${output}.\n${attempt === "REPAIR" ? "The previous response failed.\n" : ""}<<<END METHODS_EVIDENCE_V2_ROLE>>>\n`;
}

const SPECIALIST_PROMPT = rolePrompt();
const SPECIALIST_REPAIR_PROMPT = rolePrompt({ attempt: "REPAIR" });
const REVIEWER_PROMPT = rolePrompt({ role: "REVIEWER" });
const REVIEWER_REPAIR_PROMPT = rolePrompt({ role: "REVIEWER", attempt: "REPAIR" });

function withAdditionalRoleSection(prompt, kind, payload) {
  return prompt.replace(
    "\n<<<METHODS_EVIDENCE_V2_ROLE>>>",
    `\n<<<SECTION 7 ${kind}>>>\n${JSON.stringify(payload)}\n<<<END SECTION>>>\n\n<<<METHODS_EVIDENCE_V2_ROLE>>>`,
  );
}

function fixture({ role = "SPECIALIST" } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "codex-luna-model-cert-"));
  const workspace = path.join(root, "workspace");
  for (const directory of [workspace, "inputs", "output", "runtime"].map((entry) => path.isAbsolute(entry) ? entry : path.join(workspace, entry))) fs.mkdirSync(directory, { recursive: true });
  const specialist = role === "SPECIALIST";
  const jobId = specialist ? SOURCE_JOB_ID : REVIEW_JOB_ID;
  const jobType = specialist ? "DIAGNOSE" : "REVIEW";
  fs.writeFileSync(path.join(workspace, "inputs", "request.json"), JSON.stringify({
    schema_version: 2,
    job: {
      job_id: jobId,
      case_id: CASE_ID,
      job_type: jobType,
      goal: "Evaluate the compact Evidence V2 input.",
      base_state_revision: 3,
    },
    user_facts: [],
  }));
  fs.writeFileSync(path.join(workspace, "inputs", "manifest.json"), JSON.stringify({
    schema_version: 2,
    job_id: jobId,
    case_id: CASE_ID,
    job_type: jobType,
    logparse_tool_ref: null,
    logparse_product: null,
    entries: [],
    resolved_logparse_plan: null,
    review_subject: null,
    ...(specialist ? {} : {
      methods_reviewer_input: {
        schema_version: 2,
        review_job_id: REVIEW_JOB_ID,
        case_id: CASE_ID,
        target: REVIEW_TARGET,
        method_ids: EVALUATION_INPUT.evaluations.map((item) => item.method_id),
      },
    }),
  }));
  fs.mkdirSync(path.join(workspace, "runtime", "tool-state"));
  fs.writeFileSync(path.join(workspace, "runtime", "tool-state", "role.state"), "ready\n");
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

function fakeTrace(workspace, response, { writeOutput = true, role = "SPECIALIST" } = {}) {
  if (process.platform === "linux") {
    for (const name of [".agents", ".codex", ".git"]) {
      fs.mkdirSync(path.join(workspace, name));
    }
  }
  if (writeOutput) {
    const output = role === "SPECIALIST"
      ? "method-diagnosis.draft.json"
      : "method-review.draft.json";
    fs.writeFileSync(path.join(workspace, "output", output), JSON.stringify(response));
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
  const specialist = parseEvidenceV2RolePrompt(SPECIALIST_PROMPT);
  assert.deepEqual(
    { role: specialist.role, attempt: specialist.attempt, output: specialist.output },
    { role: "SPECIALIST", attempt: "PRIMARY", output: "output/method-diagnosis.draft.json" },
  );
  assert.deepEqual(
    Object.keys(specialist.rolePayload).sort(),
    ["case_id", "evaluation_input", "job_id", "request_path", "role", "schema_version"],
  );
  assert.equal(specialist.rolePayload.job_id, SOURCE_JOB_ID);
  assert.equal(specialist.rolePayload.case_id, CASE_ID);

  const reviewer = parseEvidenceV2RolePrompt(REVIEWER_REPAIR_PROMPT);
  assert.deepEqual(
    { role: reviewer.role, attempt: reviewer.attempt, output: reviewer.output },
    { role: "REVIEWER", attempt: "REPAIR", output: "output/method-review.draft.json" },
  );
  assert.deepEqual(
    Object.keys(reviewer.rolePayload).sort(),
    ["evaluation_input", "request_path", "role", "schema_version", "target"],
  );
  assert.deepEqual(reviewer.rolePayload.target, REVIEW_TARGET);
  assert.throws(() => parseEvidenceV2RolePrompt(SPECIALIST_PROMPT.replace("diagnosis", "review")), { code: "CODEX_LUNA_MODEL_CERT_OUTPUT_PATH_MISMATCH" });
  assert.throws(() => parseEvidenceV2RolePrompt(`${SPECIALIST_PROMPT}trailing`), { code: "CODEX_LUNA_MODEL_CERT_ROLE_MARKER_INVALID" });
});

test("role payload exact keys and closed identities reject cross-role contamination", () => {
  const specialistPayload = parseEvidenceV2RolePrompt(SPECIALIST_PROMPT).rolePayload;
  const reviewerPayload = parseEvidenceV2RolePrompt(REVIEWER_PROMPT).rolePayload;
  const specialistMissingCase = { ...specialistPayload };
  delete specialistMissingCase.case_id;
  const reviewerMissingTarget = { ...reviewerPayload };
  delete reviewerMissingTarget.target;
  const invalid = [
    [rolePrompt({ rolePayload: specialistMissingCase }), "CODEX_LUNA_MODEL_CERT_ROLE_PAYLOAD_INVALID"],
    [rolePrompt({ role: "REVIEWER", rolePayload: reviewerMissingTarget }), "CODEX_LUNA_MODEL_CERT_ROLE_PAYLOAD_INVALID"],
    [
      rolePrompt({ rolePayload: { ...specialistPayload, target: REVIEW_TARGET } }),
      "CODEX_LUNA_MODEL_CERT_ROLE_PAYLOAD_INVALID",
    ],
    [
      rolePrompt({ rolePayload: { ...specialistPayload, job_id: "not-a-job-id" } }),
      "CODEX_LUNA_MODEL_CERT_ROLE_IDENTITY_INVALID",
    ],
    [
      rolePrompt({ role: "REVIEWER", rolePayload: { ...reviewerPayload, case_id: CASE_ID } }),
      "CODEX_LUNA_MODEL_CERT_ROLE_PAYLOAD_INVALID",
    ],
    [
      rolePrompt({
        role: "REVIEWER",
        rolePayload: {
          ...reviewerPayload,
          target: { ...REVIEW_TARGET, graph_ref: `graph-${"0".repeat(64)}` },
        },
      }),
      "CODEX_LUNA_MODEL_CERT_ROLE_IDENTITY_INVALID",
    ],
    [
      rolePrompt({
        role: "REVIEWER",
        rolePayload: {
          ...reviewerPayload,
          target: { ...REVIEW_TARGET, candidate: { conclusion_id: "forbidden" } },
        },
      }),
      "CODEX_LUNA_MODEL_CERT_ROLE_IDENTITY_INVALID",
    ],
  ];
  for (const [prompt, code] of invalid) {
    assert.throws(() => parseEvidenceV2RolePrompt(prompt), { code });
  }
});

test("prompt accepts exactly one Methods role data section", () => {
  const specialistPayload = parseEvidenceV2RolePrompt(SPECIALIST_PROMPT).rolePayload;
  const reviewerPayload = parseEvidenceV2RolePrompt(REVIEWER_PROMPT).rolePayload;
  for (const prompt of [
    withAdditionalRoleSection(SPECIALIST_PROMPT, "EVIDENCE", specialistPayload),
    withAdditionalRoleSection(SPECIALIST_PROMPT, "REVIEW_TARGET", reviewerPayload),
    withAdditionalRoleSection(REVIEWER_PROMPT, "EVIDENCE", specialistPayload),
    withAdditionalRoleSection(REVIEWER_PROMPT, "REVIEW_TARGET", { candidate: "forbidden" }),
  ]) {
    assert.throws(
      () => parseEvidenceV2RolePrompt(prompt),
      { code: "CODEX_LUNA_MODEL_CERT_ROLE_DATA_SECTION_INVALID" },
    );
  }
});

test("compact source catalog is exact, ordered, and may include a zero-hit source", () => {
  assert.doesNotThrow(() => parseEvidenceV2RolePrompt(SPECIALIST_PROMPT));
  assert.equal(
    EVALUATION_INPUT.observations.some((item) => item.source_id === "client"),
    false,
  );
  const withoutSources = JSON.parse(JSON.stringify(EVALUATION_INPUT));
  delete withoutSources.sources;
  const invalidInputs = [
    withoutSources,
    {
      ...EVALUATION_INPUT,
      extra: true,
    },
    {
      ...EVALUATION_INPUT,
      sources: EVALUATION_INPUT.sources.map((item) => ({ ...item, id: item.id + 1 })),
    },
    {
      ...EVALUATION_INPUT,
      sources: [
        { id: 1, source_id: "server", relative_path: "logs/server.log" },
        { id: 2, source_id: "client", relative_path: "logs/client.log" },
      ],
    },
    {
      ...EVALUATION_INPUT,
      sources: [
        EVALUATION_INPUT.sources[0],
        { id: 2, source_id: "client", relative_path: "logs/server.log" },
      ],
    },
    {
      ...EVALUATION_INPUT,
      sources: [
        { ...EVALUATION_INPUT.sources[0], extra: true },
        EVALUATION_INPUT.sources[1],
      ],
    },
    {
      ...EVALUATION_INPUT,
      observations: [{ ...EVALUATION_INPUT.observations[0], source_id: "unknown" }],
    },
  ];
  for (const identityTokens of [
    ["Request=42"],
    ["request=42 extra"],
    ["request=42,43"],
    ["request=42", "request=43"],
  ]) {
    const invalidIdentity = JSON.parse(JSON.stringify(EVALUATION_INPUT));
    invalidIdentity.evaluations[0].events[0].identity_tokens = identityTokens;
    invalidInputs.push(invalidIdentity);
  }
  for (const evaluationInput of invalidInputs) {
    assert.throws(
      () => parseEvidenceV2RolePrompt(rolePrompt({ evaluationInput })),
      { code: "CODEX_LUNA_MODEL_CERT_EVALUATION_INPUT_INVALID" },
    );
  }
});

test("model cert wrapper freezes default and blind-review call caps", () => {
  assert.equal(CODEX_LUNA_MODEL_CERT_NORMAL_CALLS, 1);
  assert.equal(CODEX_LUNA_MODEL_CERT_MAX_CALLS, 2);
  assert.equal(CODEX_LUNA_MODEL_CERT_BLIND_REVIEW_NORMAL_CALLS, 2);
  assert.equal(CODEX_LUNA_MODEL_CERT_BLIND_REVIEW_MAX_CALLS, 4);
  const instructions = modelRoleDeveloperInstructions("/tmp/evidence-v2-role", parseEvidenceV2RolePrompt(SPECIALIST_PROMPT));
  assert.match(instructions, /只写 output\/method-diagnosis\.draft\.json/);
  assert.match(instructions, /prompt 上下文中的紧凑 evaluation_input/);
  assert.match(instructions, /不得读取 inputs\/method-evidence-graph\.json/);
  assert.match(instructions, /不得生成 Evidence、Candidate、Artifact、grounding、PARTIAL/);
  assert.throws(
    () => parseEvidenceV2RolePrompt(SPECIALIST_PROMPT.replace("bounded production context", "read inputs/method-evidence-graph.json")),
    { code: "CODEX_LUNA_MODEL_CERT_LEGACY_INPUT_EXPOSED" },
  );
  assert.throws(
    () => parseEvidenceV2RolePrompt(SPECIALIST_PROMPT.replace("bounded production context", "read runtime/context.txt")),
    { code: "CODEX_LUNA_MODEL_CERT_CONTEXT_FILE_EXPOSED" },
  );
  assert.throws(
    () => parseEvidenceV2RolePrompt(SPECIALIST_PROMPT.replace('"evaluations":[', '"evaluations_missing":[')),
    { code: "CODEX_LUNA_MODEL_CERT_EVALUATION_INPUT_INVALID" },
  );
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
    runAppServerCall: async ({ workspaceRoot, developerInstructions }) => {
      assert.deepEqual(fs.readdirSync(path.join(workspaceRoot, "inputs")).sort(), ["manifest.json", "request.json"]);
      assert.equal(fs.existsSync(path.join(workspaceRoot, "inputs", "method-evidence-graph.json")), false);
      assert.equal(fs.existsSync(path.join(workspaceRoot, "inputs", "method-evaluation-plan.json")), false);
      assert.equal(fs.existsSync(path.join(workspaceRoot, "runtime", "context.txt")), false);
      assert.deepEqual(fs.readdirSync(path.join(workspaceRoot, "runtime")), ["tool-state"]);
      assert.equal(fs.readFileSync(path.join(workspaceRoot, "runtime", "tool-state", "role.state"), "utf8"), "ready\n");
      assert.match(developerInstructions, /评估证据只能使用.*prompt 上下文中的紧凑 evaluation_input/);
      return fakeTrace(workspaceRoot, response);
    },
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

test("blind Reviewer receives the same compact input without Specialist or Graph/Plan files", async (context) => {
  const { root, workspace, values } = fixture({ role: "REVIEWER" });
  const previous = process.cwd();
  process.chdir(workspace);
  context.after(() => process.chdir(previous));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const response = [{ evaluation_ref: EVALUATION_INPUT.evaluations[0].evaluation_ref, verdict: "UNKNOWN", supporting_event_refs: [], reason: "Blind evidence is insufficient." }];
  const receipt = await runModelRoleInvocation(values, {
    stdin: Readable.from([REVIEWER_PROMPT]),
    stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
    ambient: {},
    runAppServerCall: async ({ workspaceRoot }) => {
      assert.equal(fs.existsSync(path.join(workspaceRoot, "inputs", "method-diagnosis.json")), false);
      assert.equal(fs.existsSync(path.join(workspaceRoot, "inputs", "method-evidence-graph.json")), false);
      assert.equal(fs.existsSync(path.join(workspaceRoot, "inputs", "method-evaluation-plan.json")), false);
      return fakeTrace(workspaceRoot, response, { role: "REVIEWER" });
    },
  });
  assert.equal(receipt.role, "REVIEWER");
  assert.deepEqual(
    JSON.parse(fs.readFileSync(path.join(workspace, "output", "method-review.draft.json"), "utf8")),
    response,
  );
  assertWorkspaceRestored(workspace);
});

test("wrapper binds Specialist payload identity to request and manifest", async (context) => {
  const { root, workspace, values } = fixture();
  const payload = parseEvidenceV2RolePrompt(SPECIALIST_PROMPT).rolePayload;
  const mismatchedPrompt = rolePrompt({
    rolePayload: {
      ...payload,
      job_id: "20000000-0000-4000-8000-000000000099",
    },
  });
  const previous = process.cwd();
  process.chdir(workspace);
  context.after(() => process.chdir(previous));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  await assert.rejects(
    runModelRoleInvocation(values, {
      stdin: Readable.from([mismatchedPrompt]),
      stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
      ambient: {},
      runAppServerCall: async () => { throw new Error("must not call app-server"); },
    }),
    { code: "CODEX_LUNA_MODEL_CERT_ROLE_IDENTITY_INVALID" },
  );
  assertWorkspaceRestored(workspace);
});

test("Specialist manifest omits rather than nulls methods_reviewer_input", async (context) => {
  const { root, workspace, values } = fixture();
  const manifestPath = path.join(workspace, "inputs", "manifest.json");
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  manifest.methods_reviewer_input = null;
  fs.writeFileSync(manifestPath, JSON.stringify(manifest));
  const previous = process.cwd();
  process.chdir(workspace);
  context.after(() => process.chdir(previous));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  await assert.rejects(
    runModelRoleInvocation(values, {
      stdin: Readable.from([SPECIALIST_PROMPT]),
      stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
      ambient: {},
      runAppServerCall: async () => { throw new Error("must not call app-server"); },
    }),
    { code: "CODEX_LUNA_MODEL_CERT_MANIFEST_INVALID" },
  );
  assert.equal(JSON.parse(fs.readFileSync(manifestPath, "utf8")).methods_reviewer_input, null);
  assertWorkspaceRestored(workspace);
});

test("wrapper binds Reviewer target identity to its manifest", async (context) => {
  const { root, workspace, values } = fixture({ role: "REVIEWER" });
  const manifestPath = path.join(workspace, "inputs", "manifest.json");
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  manifest.methods_reviewer_input.target.reviewed_state_revision += 1;
  fs.writeFileSync(manifestPath, JSON.stringify(manifest));
  const previous = process.cwd();
  process.chdir(workspace);
  context.after(() => process.chdir(previous));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  await assert.rejects(
    runModelRoleInvocation(values, {
      stdin: Readable.from([REVIEWER_PROMPT]),
      stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
      ambient: {},
      runAppServerCall: async () => { throw new Error("must not call app-server"); },
    }),
    { code: "CODEX_LUNA_MODEL_CERT_ROLE_IDENTITY_INVALID" },
  );
  assertWorkspaceRestored(workspace);
});

for (const legacyInput of ["method-evidence-graph.json", "method-evaluation-plan.json"]) {
  test(`wrapper fails closed when production workspace exposes ${legacyInput}`, async (context) => {
    const { root, workspace, values } = fixture();
    const legacyPath = path.join(workspace, "inputs", legacyInput);
    fs.writeFileSync(legacyPath, "authoritative server record\n");
    const previous = process.cwd();
    process.chdir(workspace);
    context.after(() => process.chdir(previous));
    context.after(() => fs.rmSync(root, { recursive: true, force: true }));
    await assert.rejects(
      runModelRoleInvocation(values, {
        stdin: Readable.from([SPECIALIST_PROMPT]),
        stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
        ambient: {},
        runAppServerCall: async () => { throw new Error("must not call app-server"); },
      }),
      { code: "CODEX_LUNA_MODEL_CERT_LEGACY_INPUT_EXPOSED" },
    );
    assert.equal(fs.readFileSync(legacyPath, "utf8"), "authoritative server record\n");
    assertWorkspaceRestored(workspace);
  });
}

for (const unexpectedInput of ["evidence", "artifacts", "outcomes", "unexpected.json"]) {
  test(`wrapper preserves and rejects unexpected inputs/${unexpectedInput}`, async (context) => {
    const { root, workspace, values } = fixture();
    const unexpectedPath = path.join(workspace, "inputs", unexpectedInput);
    if (unexpectedInput.endsWith(".json")) {
      fs.writeFileSync(unexpectedPath, "unexpected input\n");
    } else {
      fs.mkdirSync(unexpectedPath);
      fs.writeFileSync(path.join(unexpectedPath, "private.txt"), "unexpected input\n");
    }
    const previous = process.cwd();
    process.chdir(workspace);
    context.after(() => process.chdir(previous));
    context.after(() => fs.rmSync(root, { recursive: true, force: true }));
    let providerCalled = false;
    await assert.rejects(
      runModelRoleInvocation(values, {
        stdin: Readable.from([SPECIALIST_PROMPT]),
        stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
        ambient: {},
        runAppServerCall: async () => {
          providerCalled = true;
          throw new Error("must not call app-server");
        },
      }),
      { code: "CODEX_LUNA_MODEL_CERT_INPUT_SURFACE_INVALID" },
    );
    assert.equal(providerCalled, false);
    assert.equal(fs.existsSync(unexpectedPath), true);
    assertWorkspaceRestored(workspace);
  });
}

test("wrapper preserves and rejects an unknown runtime entry", async (context) => {
  const { root, workspace, values } = fixture();
  const unexpectedPath = path.join(workspace, "runtime", "debug.json");
  fs.writeFileSync(unexpectedPath, "unexpected runtime input\n");
  const previous = process.cwd();
  process.chdir(workspace);
  context.after(() => process.chdir(previous));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  let providerCalled = false;
  await assert.rejects(
    runModelRoleInvocation(values, {
      stdin: Readable.from([SPECIALIST_PROMPT]),
      stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
      ambient: {},
      runAppServerCall: async () => {
        providerCalled = true;
        throw new Error("must not call app-server");
      },
    }),
    { code: "CODEX_LUNA_MODEL_CERT_RUNTIME_SURFACE_INVALID" },
  );
  assert.equal(providerCalled, false);
  assert.equal(fs.readFileSync(unexpectedPath, "utf8"), "unexpected runtime input\n");
  assertWorkspaceRestored(workspace);
});

test("wrapper fails closed without deleting a production runtime/context.txt", async (context) => {
  const { root, workspace, values } = fixture();
  const contextPath = path.join(workspace, "runtime", "context.txt");
  fs.writeFileSync(contextPath, "duplicate model context\n");
  const previous = process.cwd();
  process.chdir(workspace);
  context.after(() => process.chdir(previous));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  let providerCalled = false;
  await assert.rejects(
    runModelRoleInvocation(values, {
      stdin: Readable.from([SPECIALIST_PROMPT]),
      stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
      ambient: {},
      runAppServerCall: async () => {
        providerCalled = true;
        throw new Error("must not call app-server");
      },
    }),
    { code: "CODEX_LUNA_MODEL_CERT_CONTEXT_FILE_EXPOSED" },
  );
  assert.equal(providerCalled, false);
  assert.equal(fs.readFileSync(contextPath, "utf8"), "duplicate model context\n");
  assertWorkspaceRestored(workspace);
});

test("repair cannot reuse the inherited primary output as a new model response", async (context) => {
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

test("repair publishes a new response even when the restored workspace has no primary draft", async (context) => {
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
  const { root, workspace, values } = fixture({ role: "REVIEWER" });
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
