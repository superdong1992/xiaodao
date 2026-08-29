import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { treeDigest } from "../../../runtime-support/codex-luna-contract.mjs";
import { validateEvidenceV2ModelCertInputSchema } from "../../../../validation/evidence-v2-certification.mjs";
import {
  auditRuntimeAndInvocations,
  buildModelCertInput,
  materializeProviderTerminalFailure,
  parseArguments,
  runE2E,
  safeE2EError,
} from "../runtime/macos-codex-luna-e2e-runner.mjs";
import { materializeProviderTerminalFailure as materializeP1TerminalFailure } from "../../claude-deepseek/runtime/claude-deepseek-e2e-runner.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..", "..");
const SHA = (character) => character.repeat(64);
const GRAPH_REF = `graph-${SHA("1")}`;
const PLAN_REF = `plan-${SHA("2")}`;

function usage(input = 10, output = 5) {
  return { input_tokens: input, cached_input_tokens: 0, cache_write_input_tokens: 0, output_tokens: output, reasoning_output_tokens: 0, total_tokens: input + output };
}

function invocation(role, attempt, ordinal) {
  return {
    schema_version: 1,
    invocation_id: `run:codex-luna:${role.toLowerCase()}-${attempt.toLowerCase()}`,
    provider: "openai-codex-app-server",
    model: "gpt-5.6-luna",
    model_revision: "gpt-5.6-luna",
    reasoning_effort: "medium",
    role,
    attempt,
    repair: attempt === "REPAIR",
    status: "PASS",
    terminal: true,
    prompt: { sha256: SHA(String(ordinal)), size: 100 + ordinal, production_role_marker: true },
    profile: { permission_profile_id: "profile", config_sha256: SHA("a"), developer_instructions_sha256: SHA("b") },
    tool_policy: { invocation_mode: "service", mcp_tool_call_count: 0, command_count: 1, output_normalized: false },
    usage: usage(),
    thread_id: `thread-${ordinal}`,
    turn_id: `turn-${ordinal}`,
  };
}

function runtimeReceipt(invocations) {
  return {
    schema_version: 1,
    receipt_type: "codex-luna-evidence-v2-runtime-result",
    status: "PASS",
    execution_mode: "real-model",
    production_runtime: "problem_locator.runtime.diagnosis_runtime.DiagnosisRuntime",
    runtime_driver: "codex-luna-model-cert-v1",
    scenario_id: "multiple-rpc-timeouts",
    registration_id: "rpc-timeout-methods-v1",
    logparse_mode: "deterministic-fixture",
    model_invocations: invocations.length,
    role_attempts: invocations.map((item) => ({ role: item.role, attempt: item.attempt, prompt: item.prompt })),
    repair_counts: {
      specialist: invocations.some((item) => item.role === "SPECIALIST" && item.attempt === "REPAIR") ? 1 : 0,
      reviewer: invocations.some((item) => item.role === "REVIEWER" && item.attempt === "REPAIR") ? 1 : 0,
    },
    records: {
      graph: { filename: "methods-evidence-graph-v2.json", sha256: SHA("3"), size: 300 },
      plan: { filename: "methods-evaluation-plan-v2.json", sha256: SHA("4"), size: 200 },
      source_state: { filename: "methods-state-v2.json", sha256: SHA("5"), size: 100 },
      terminal_state: { filename: "methods-state-v2.json", sha256: SHA("6"), size: 100 },
      specialist_outcome: { filename: "job_outcome.json", sha256: SHA("7"), size: 100 },
      reviewer_outcome: { filename: "job_outcome.json", sha256: SHA("8"), size: 100 },
    },
    scenario: {
      scenario_id: "multiple-rpc-timeouts",
      source_wiki_sha256: SHA("9"),
      registration_id: "rpc-timeout-methods-v1",
      skill_content_sha256: SHA("a"),
      user_inputs_sha256: SHA("b"),
      sources: [
        { source_id: "client", content_sha256: SHA("c") },
        { source_id: "server", content_sha256: SHA("d") },
      ],
      evidence_graph: { ref: GRAPH_REF, canonical_sha256: SHA("e"), canonical_size: 800 },
      evaluation_plan: { ref: PLAN_REF, canonical_sha256: SHA("f"), canonical_size: 600 },
    },
    methods_result_identity: {
      sha256: SHA("0"),
      size: 900,
      case_id: "00000000-0000-4000-8000-000000000001",
      source_job_id: "00000000-0000-4000-8000-000000000002",
      result_ref: `result-${SHA("3")}`,
      evaluation_id: "00000000-0000-4000-8000-000000000003",
      status: "RESOLVED",
      plan_ref: PLAN_REF,
      evidence_graph_ref: GRAPH_REF,
      diagnostic_id: `diag-${SHA("4")}`,
    },
    methods_result: { status: "RESOLVED" },
  };
}

function identity() {
  return {
    schema_version: 1,
    status: "PASS",
    cli: { version: "codex-cli 0.149.0-alpha.4.1", sha256: SHA("1"), size: 1, platform: "darwin", architecture: "arm64" },
    auth: { kind: "chatgpt-external-tokens" },
    filesystem_sandbox: { kind: "codex-permission-profile" },
    model: "gpt-5.6-luna",
    reasoning_effort: "medium",
  };
}

test("model-cert CLI requires source/Core/registration binding and one fixed scenario", () => {
  const args = [
    "--source-root", "/source", "--codex-entry", "/codex", "--auth-source", "/auth",
    "--python-entry", "/python", "--registration-root", "/registration", "--work-root", "/work",
    "--private-root", "/private", "--evidence-root", "/evidence", "--usage-root", "/usage",
    "--run-id", "run", "--source-snapshot-digest", SHA("a"), "--core-verdict", "/core/core-verdict.json",
    "--scenario", "multiple-rpc-timeouts",
  ];
  assert.equal(parseArguments(args)["registration-root"], "/registration");
  const wrong = [...args];
  wrong[wrong.length - 1] = "api-execution-overrun";
  assert.throws(() => parseArguments(wrong), { code: "CODEX_LUNA_MODEL_CERT_SCENARIO_INVALID" });
  const registrationIndex = args.indexOf("--registration-root");
  assert.throws(() => parseArguments([...args.slice(0, registrationIndex), ...args.slice(registrationIndex + 2)]), { code: "CODEX_LUNA_MODEL_CERT_REGISTRATION_INPUT_MISSING" });
});

test("provider calls bind exact production prompts and reject scenario mutations", () => {
  const invocations = [invocation("SPECIALIST", "PRIMARY", 1), invocation("REVIEWER", "PRIMARY", 2)];
  const receipt = runtimeReceipt(invocations);
  assert.equal(auditRuntimeAndInvocations(receipt, invocations).prompt_count, 2);
  const promptDrift = structuredClone(invocations);
  promptDrift[1].prompt.sha256 = SHA("7");
  assert.throws(() => auditRuntimeAndInvocations(receipt, promptDrift), { code: "CODEX_LUNA_MODEL_CERT_RUNTIME_INVOCATION_IDENTITY_MISMATCH" });
  const wrongScenario = structuredClone(receipt);
  wrongScenario.scenario.scenario_id = "other";
  assert.throws(() => auditRuntimeAndInvocations(wrongScenario, invocations), { code: "CODEX_LUNA_MODEL_CERT_SCENARIO_IDENTITY_INVALID" });
});

test("P2 model-cert input binds provider revision, scenario, calls, usage, Core and methods_result", () => {
  const invocations = [invocation("SPECIALIST", "PRIMARY", 1), invocation("REVIEWER", "PRIMARY", 2)];
  const receipt = buildModelCertInput({
    sourceSnapshotDigest: SHA("a"),
    contractManifestSha256: SHA("b"),
    coreVerdictSha256: SHA("c"),
    scenarioOracleSha256: SHA("d"),
    identity: identity(),
    invocations,
    runtimeReceipt: runtimeReceipt(invocations),
    sourceRoot: REPO_ROOT,
  });
  assert.deepEqual(Object.keys(receipt).sort(), ["call_counts", "certification_target", "contract_manifest", "core_verdict", "scenario_oracle", "execution_identity", "invocations", "methods_result", "model", "provider", "receipt_type", "scenario", "schema_version", "source_snapshot_digest", "status", "usage"].sort());
  assert.equal(receipt.certification_target, "P2");
  assert.deepEqual(receipt.provider, { id: "openai", transport: "codex-app-server" });
  assert.equal(receipt.model.id, "gpt-5.6-luna");
  assert.match(receipt.model.revision, /^[a-f0-9]{64}$/u);
  assert.deepEqual(receipt.call_counts, { total_calls: 2, specialist_calls: 1, reviewer_calls: 1, specialist_repairs: 0, reviewer_repairs: 0, model_retries: 0 });
  assert.equal(receipt.usage.total_tokens, 30);
  assert.equal(receipt.scenario.evidence_graph.ref, receipt.methods_result.evidence_graph_ref);
  assert.equal(receipt.scenario.evaluation_plan.ref, receipt.methods_result.plan_ref);
  assert.equal(validateEvidenceV2ModelCertInputSchema(receipt, { certificationTarget: "P2" }), receipt);
});

test("an injected provider receipt cannot mint a cert without production execution originals", async (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "codex-luna-model-cert-entry-"));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const core = path.join(root, "core-verdict.json");
  fs.writeFileSync(core, "{}\n");
  const invocations = [invocation("SPECIALIST", "PRIMARY", 1), invocation("REVIEWER", "PRIMARY", 2)];
  const options = {
    sourceRoot: REPO_ROOT,
    codexEntry: path.join(root, "codex"),
    authSource: path.join(root, "auth.json"),
    pythonEntry: path.join(root, "python"),
    cacheRoot: null,
    registrationRoot: path.join(root, "registration"),
    workRoot: path.join(root, "work"),
    privateRoot: path.join(root, "private"),
    evidenceRoot: path.join(root, "evidence"),
    usageRoot: path.join(root, "usage"),
    runId: "run",
    sourceSnapshotDigest: SHA("a"),
    coreVerdict: core,
  };
  fs.mkdirSync(options.registrationRoot);
  const registration = { root: options.registrationRoot, source: "fake-app-server-registration", tree_sha256: treeDigest(options.registrationRoot) };
  await assert.rejects(() => runE2E(options, {
    validateIdentity: () => identity(),
    validateCore: () => ({ status: "PASS" }),
    registrationInput: () => ({ registration, producer: null, cache: null }),
    runRuntime: async () => runtimeReceipt(invocations),
    readInvocations: () => invocations,
    materializeModelCert: () => { throw new Error("must not materialize"); },
  }), (error) => error.code === "SCENARIO_ORACLE_METHODS_MISSING");
  assert.equal(fs.existsSync(path.join(options.evidenceRoot, "model-cert-input.json")), false);
  assert.equal(fs.existsSync(path.join(options.evidenceRoot, "model-cert.json")), false);
});

test("P1 and P2 failure adapters expose the same production terminal diagnostic", (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "provider-terminal-alignment-"));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const methodsResult = {
    status: "UNRESOLVED",
    reason_code: "SPECIALIST_REVIEWER_DISAGREEMENT",
    reasons: ["Specialist 与 Reviewer 的判定不一致。"],
    diagnostic_id: `diag-${SHA("a")}`,
    diagnostic_evaluation_ref: `eval-${SHA("b")}`,
  };
  const options = { modelCalls: 2, repairs: { specialist: 0, reviewer: 0 } };
  const p1 = materializeP1TerminalFailure({ methods_result: methodsResult }, path.join(root, "p1"), options);
  const p2 = materializeProviderTerminalFailure({ methods_result: methodsResult }, path.join(root, "p2"), options);
  const withoutTarget = ({ certification_target: _target, ...value }) => value;
  assert.deepEqual(withoutTarget(p1), withoutTarget(p2));
  assert.equal(p1.code, methodsResult.reason_code);
  assert.equal(p2.diagnostic_id, methodsResult.diagnostic_id);
  assert.equal(p2.evaluation_ref, methodsResult.diagnostic_evaluation_ref);
});

test("P2 writes failure evidence and preserves the production reason without minting a cert", async (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "codex-luna-terminal-failure-"));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const core = path.join(root, "core-verdict.json");
  fs.writeFileSync(core, "{}\n");
  const invocations = [invocation("SPECIALIST", "PRIMARY", 1), invocation("REVIEWER", "PRIMARY", 2)];
  const runtime = runtimeReceipt(invocations);
  runtime.methods_result_identity.status = "UNRESOLVED";
  runtime.methods_result = {
    status: "UNRESOLVED",
    reason_code: "SPECIALIST_REVIEWER_DISAGREEMENT",
    reasons: ["Specialist 与 Reviewer 的判定不一致。"],
    diagnostic_id: `diag-${SHA("a")}`,
    diagnostic_evaluation_ref: `eval-${SHA("b")}`,
  };
  const options = {
    sourceRoot: REPO_ROOT,
    codexEntry: path.join(root, "codex"),
    authSource: path.join(root, "auth.json"),
    pythonEntry: path.join(root, "python"),
    cacheRoot: null,
    registrationRoot: path.join(root, "registration"),
    workRoot: path.join(root, "work"),
    privateRoot: path.join(root, "private"),
    evidenceRoot: path.join(root, "evidence"),
    usageRoot: path.join(root, "usage"),
    runId: "run",
    sourceSnapshotDigest: SHA("a"),
    coreVerdict: core,
  };
  fs.mkdirSync(options.registrationRoot);
  const registration = { root: options.registrationRoot, source: "fake-app-server-registration", tree_sha256: treeDigest(options.registrationRoot) };
  await assert.rejects(() => runE2E(options, {
    validateIdentity: () => identity(),
    validateCore: () => ({ status: "PASS" }),
    registrationInput: () => ({ registration, producer: null, cache: null }),
    runRuntime: async (runtimeOptions) => {
      fs.writeFileSync(path.join(runtimeOptions.evidenceRoot, "runtime-receipt.json"), JSON.stringify(runtime));
      return runtime;
    },
    readInvocations: () => invocations,
    materializeModelCert: () => { throw new Error("must not materialize"); },
  }), (error) => error.code === "SPECIALIST_REVIEWER_DISAGREEMENT");
  const adapter = JSON.parse(fs.readFileSync(path.join(options.evidenceRoot, "adapter-receipt.json"), "utf8"));
  assert.equal(adapter.code, "SPECIALIST_REVIEWER_DISAGREEMENT");
  assert.equal(adapter.reason, runtime.methods_result.reasons[0]);
  assert.equal(adapter.diagnostic_id, runtime.methods_result.diagnostic_id);
  assert.equal(adapter.evaluation_ref, runtime.methods_result.diagnostic_evaluation_ref);
  for (const name of ["runtime-receipt.json", "model-invocations.json", "model-usage.json", "adapter-receipt.json"]) {
    assert.equal(fs.existsSync(path.join(options.evidenceRoot, name)), true, name);
  }
  assert.equal(fs.existsSync(path.join(options.evidenceRoot, "model-cert-input.json")), false);
  assert.equal(fs.existsSync(path.join(options.evidenceRoot, "model-cert.json")), false);
  assert.equal(safeE2EError({ code: adapter.code, message: adapter.reason }).code, "SPECIALIST_REVIEWER_DISAGREEMENT");
});

test("safe model-cert error exposes only one closed code and message", () => {
  assert.deepEqual(safeE2EError({ code: "CLOSED", message: "safe", details: { token: "secret" } }), { schema_version: 1, status: "FAIL", code: "CLOSED", message: "safe" });
});
