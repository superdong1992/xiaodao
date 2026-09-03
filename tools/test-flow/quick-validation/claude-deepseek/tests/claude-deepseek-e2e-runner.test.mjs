import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { canonicalJson } from "../../../lib/util.mjs";

import {
  auditRuntimeAndInvocations,
  auditScenarioIdentity,
  buildModelCertInput,
  materializeStandaloneModelCert,
  materializeProviderTerminalFailure,
  parseArguments,
  productionRuntimeArguments,
  safeE2EError,
} from "../runtime/claude-deepseek-e2e-runner.mjs";
import { validateEvidenceV2ModelCertInputSchema } from "../../../../validation/evidence-v2-certification.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..", "..");
const RUNTIME = path.join(ROOT, "tools", "test-flow", "quick-validation", "claude-deepseek", "runtime", "claude_deepseek_model_cert_runtime.py");

function usage(input = 1, output = 1) {
  return { schema_version: 1, input_tokens: input, output_tokens: output, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, total_tokens: input + output, cost_usd: 0 };
}

function invocation(role, attempt, ordinal) {
  return {
    schema_version: 1,
    invocation_id: `run:${role.toLowerCase()}:${attempt.toLowerCase()}`,
    phase: role,
    role,
    evaluation_attempt: attempt,
    role_call_ordinal: attempt === "PRIMARY" ? 1 : 2,
    model: "deepseek-v4-flash[1m]",
    attempt: 1,
    retry: 0,
    status: "PASS",
    terminal: true,
    turns: 1,
    wall_timeout_seconds: 600,
    prompt: { sha256: String(ordinal).repeat(64), utf8_size: 100 + ordinal },
    usage: usage(),
    tool_policy: { tools: ["Read", "Write"], readable_scope: "job-request-only", writable_scope: role === "SPECIALIST" ? "output/method-diagnosis.draft.json" : "output/method-review.draft.json", network: false, shell: false, skill_loading: false },
    workspace_audit: { status: "PASS", harness_normalized: false },
  };
}

function scenarioIdentity() {
  return {
    scenario_id: "multiple-rpc-timeouts",
    source_wiki_sha256: "5".repeat(64),
    registration_id: "rpc-timeout-methods-v1",
    skill_content_sha256: "6".repeat(64),
    user_inputs_sha256: "7".repeat(64),
    sources: [
      { source_id: "client", content_sha256: "8".repeat(64) },
      { source_id: "server", content_sha256: "9".repeat(64) },
    ],
    evidence_graph: { ref: `graph-${"3".repeat(64)}`, canonical_sha256: "a".repeat(64), canonical_size: 200 },
    evaluation_plan: { ref: `plan-${"2".repeat(64)}`, canonical_sha256: "b".repeat(64), canonical_size: 100 },
  };
}

function runtimeReceipt(invocations, evaluationMode = invocations.some((item) => item.role === "REVIEWER") ? "BLIND_CONSENSUS" : "SPECIALIST_ONLY") {
  return {
    schema_version: 1,
    status: "PASS",
    execution_mode: "real-model",
    evaluation_mode: evaluationMode,
    production_runtime: "problem_locator.runtime.diagnosis_runtime.DiagnosisRuntime",
    model_invocations: invocations.length,
    scenario: scenarioIdentity(),
    role_attempts: invocations.map((item) => ({ role: item.role, attempt: item.evaluation_attempt, prompt: { sha256: item.prompt.sha256, size: item.prompt.utf8_size } })),
    repair_counts: { specialist: invocations.some((item) => item.role === "SPECIALIST" && item.evaluation_attempt === "REPAIR") ? 1 : 0, reviewer: invocations.some((item) => item.role === "REVIEWER" && item.evaluation_attempt === "REPAIR") ? 1 : 0 },
    methods_result_identity: { sha256: "e".repeat(64), size: 400, case_id: "00000000-0000-4000-8000-000000000001", source_job_id: "00000000-0000-4000-8000-000000000002", result_ref: `result-${"1".repeat(64)}`, evaluation_id: "00000000-0000-4000-8000-000000000003", status: "RESOLVED", plan_ref: `plan-${"2".repeat(64)}`, evidence_graph_ref: `graph-${"3".repeat(64)}`, diagnostic_id: `diag-${"4".repeat(64)}` },
    records: {},
  };
}

test("model-cert CLI requires source/Core bindings and rejects the former scenario suite", () => {
  const args = ["--source-root", "/source", "--claude-entry", "/cli", "--claude-settings", "/settings", "--python-entry", "/python", "--cache-root", "/cache", "--work-root", "/work", "--private-root", "/private", "--evidence-root", "/evidence", "--usage-root", "/usage", "--run-id", "run", "--source-snapshot-digest", "a".repeat(64), "--core-verdict", "/core/core-verdict.json", "--scenario", "multiple-rpc-timeouts"];
  assert.equal(parseArguments(args)["source-snapshot-digest"], "a".repeat(64));
  assert.equal(parseArguments(args)["evaluation-mode"], "SPECIALIST_ONLY");
  assert.equal(parseArguments([...args, "--evaluation-mode", "BLIND_CONSENSUS"])["evaluation-mode"], "BLIND_CONSENSUS");
  assert.throws(() => parseArguments([...args, "--evaluation-mode", "invalid"]), (error) => error.code === "CLAUDE_DEEPSEEK_MODEL_CERT_EVALUATION_MODE_INVALID");
  const wrongScenario = [...args];
  wrongScenario[wrongScenario.length - 1] = "api-execution-overrun";
  assert.throws(() => parseArguments(wrongScenario), (error) => error.code === "CLAUDE_DEEPSEEK_MODEL_CERT_SCENARIO_INVALID");
  const coreIndex = args.indexOf("--core-verdict");
  const missingCore = [...args.slice(0, coreIndex), ...args.slice(coreIndex + 2)];
  assert.throws(() => parseArguments(missingCore), (error) => error.code === "CLAUDE_DEEPSEEK_MODEL_CERT_ARGUMENT_MISSING");
});

test("provider calls bind the exact production prompt sequence", () => {
  const invocations = [invocation("SPECIALIST", "PRIMARY", 1), invocation("REVIEWER", "PRIMARY", 2)];
  const receipt = runtimeReceipt(invocations);
  assert.equal(auditRuntimeAndInvocations(receipt, invocations, { evaluationMode: "BLIND_CONSENSUS" }).prompt_count, 2);
  const drifted = structuredClone(invocations);
  drifted[1].prompt.sha256 = "f".repeat(64);
  assert.throws(() => auditRuntimeAndInvocations(receipt, drifted, { evaluationMode: "BLIND_CONSENSUS" }), (error) => error.code === "CLAUDE_DEEPSEEK_RUNTIME_INVOCATION_IDENTITY_MISMATCH");
});

test("P1 model-cert input binds provider revision, calls, usage, Core, and methods_result", () => {
  const invocations = [invocation("SPECIALIST", "PRIMARY", 1)];
  const sourceRoot = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-cert-source-"));
  try {
    const runtime = path.join(sourceRoot, "src", "problem_locator", "runtime");
    fs.mkdirSync(runtime, { recursive: true });
    fs.writeFileSync(path.join(runtime, "diagnosis_runtime.py"), "# production runtime\n");
    const receipt = buildModelCertInput({
      sourceSnapshotDigest: "a".repeat(64), contractManifestSha256: "b".repeat(64), coreVerdictSha256: "c".repeat(64), scenarioOracleSha256: "f".repeat(64),
      identity: { settings: { fingerprint: "d".repeat(64) }, cli: { version: "2.1.89" }, model: "deepseek-v4-flash[1m]", max_output_tokens: 64000 },
      invocations, usage: { input_tokens: 1, output_tokens: 1, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, total_tokens: 2, cost_usd: 0 },
      runtimeReceipt: runtimeReceipt(invocations), sourceRoot,
    });
    assert.deepEqual(Object.keys(receipt).sort(), ["call_counts", "certification_target", "contract_manifest", "core_verdict", "evaluation_mode", "scenario_oracle", "execution_identity", "invocations", "methods_result", "model", "provider", "receipt_type", "scenario", "schema_version", "source_snapshot_digest", "status", "usage"].sort());
    assert.equal(receipt.schema_version, 2);
    assert.equal(receipt.certification_target, "P1");
    assert.equal(receipt.evaluation_mode, "SPECIALIST_ONLY");
    assert.equal(receipt.model.revision_source, "settings-fingerprint");
    assert.equal(receipt.call_counts.total_calls, 1);
    assert.equal(receipt.call_counts.reviewer_calls, 0);
    assert.equal(receipt.methods_result.status, "RESOLVED");
    assert.equal(validateEvidenceV2ModelCertInputSchema(receipt, { certificationTarget: "P1" }).scenario.registration_id, "rpc-timeout-methods-v1");
  } finally { fs.rmSync(sourceRoot, { recursive: true, force: true }); }
});

test("standalone writes and rereads the exact shared model-cert bytes once", () => {
  const certRoot = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-final-cert-"));
  try {
    const options = { certificationTarget: "P1", sourceSnapshotDigest: "a".repeat(64), sourceRoot: "/source", coreVerdictPath: "/core", certRoot };
    const built = { schema_version: 1, receipt_type: "evidence-v2-model-cert", status: "PASS", deterministic: true };
    let validated = 0;
    const result = materializeStandaloneModelCert(options, {
      build(actual) { assert.deepEqual(actual, options); return built; },
      validate(actual, actualOptions) { assert.deepEqual(actual, built); assert.deepEqual(actualOptions, options); validated += 1; return actual; },
    });
    assert.deepEqual(result, built);
    assert.equal(validated, 1);
    assert.equal(fs.readFileSync(path.join(certRoot, "model-cert.json"), "utf8"), canonicalJson(built));
    assert.throws(() => materializeStandaloneModelCert(options, { build: () => built, validate: () => built }), (error) => error.code === "EEXIST");
  } finally { fs.rmSync(certRoot, { recursive: true, force: true }); }
});

test("E2E writes adapter input before materializing the shared final cert", () => {
  const source = fs.readFileSync(path.join(ROOT, "tools", "test-flow", "quick-validation", "claude-deepseek", "runtime", "claude-deepseek-e2e-runner.mjs"), "utf8");
  const inputWrite = source.indexOf('writeJsonNew(path.join(evidenceRoot, "model-cert-input.json")');
  const finalBuild = source.indexOf("const modelCert = materializeStandaloneModelCert", inputWrite);
  assert.ok(inputWrite > 0 && finalBuild > inputWrite);
  assert.match(source, /buildEvidenceV2ModelCert/u);
  assert.match(source, /validateEvidenceV2ModelCert/u);
});

test("real Runtime invocation receives the validated registration and frozen release scenario", () => {
  const sourceRoot = path.resolve("/source-snapshot");
  const options = {
    sourceRoot,
    sourceWiki: path.join(sourceRoot, "tests/cases/release/rpc-timeout-anonymized/input/wiki.md"),
    scenarioRoot: path.join(sourceRoot, "tests/cases/release/rpc-timeout-anonymized/scenarios/multiple-rpc-timeouts"),
    registrationRoot: "/cache/registration/rpc-timeout-methods-v1",
    workRoot: "/work", evidenceRoot: "/evidence", claudeEntry: "/claude/cli.js", stagedSettings: "/private/settings.json",
    configRoot: "/private/config", privateRoot: "/private", usageRoot: "/usage", runId: "run",
  };
  const args = productionRuntimeArguments(options);
  const value = (name) => args[args.indexOf(name) + 1];
  assert.equal(value("--registration-root"), options.registrationRoot);
  assert.equal(value("--source-wiki"), options.sourceWiki);
  assert.equal(value("--scenario-root"), options.scenarioRoot);
  assert.equal(value("--evaluation-mode"), "SPECIALIST_ONLY");
  assert.match(value("--scenario-root"), /multiple-rpc-timeouts$/u);
});

test("real scenario audit binds frozen Wiki, registration, driver sources, and production Graph/Plan", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-scenario-audit-"));
  try {
    const sourceWiki = path.join(root, "wiki.md");
    const scenarioRoot = path.join(root, "multiple-rpc-timeouts");
    fs.mkdirSync(scenarioRoot);
    fs.writeFileSync(sourceWiki, "# Wiki\n");
    fs.writeFileSync(path.join(scenarioRoot, "client.log"), "client\n");
    fs.writeFileSync(path.join(scenarioRoot, "server.log"), "server\n");
    const driver = { scenario_id: "multiple-rpc-timeouts", initial_user_fact_names: ["problem_time"], initial_user_fact_values: ["2026-08-29T00:00:00Z"], attachment_anchor_names: ["client", "server"], attachment_files: ["client.log", "server.log"] };
    fs.writeFileSync(path.join(scenarioRoot, "driver.json"), JSON.stringify(driver));
    const wikiSha = crypto.createHash("sha256").update(fs.readFileSync(sourceWiki)).digest("hex");
    const receipt = runtimeReceipt([invocation("SPECIALIST", "PRIMARY", 1), invocation("REVIEWER", "PRIMARY", 2)]);
    receipt.scenario = {
      ...scenarioIdentity(), source_wiki_sha256: wikiSha,
      user_inputs_sha256: crypto.createHash("sha256").update(canonicalJson({ initial_user_fact_names: driver.initial_user_fact_names, initial_user_fact_values: driver.initial_user_fact_values })).digest("hex"),
      sources: driver.attachment_anchor_names.map((sourceId, index) => ({ source_id: sourceId, content_sha256: crypto.createHash("sha256").update(fs.readFileSync(path.join(scenarioRoot, driver.attachment_files[index]))).digest("hex") })),
    };
    receipt.records = { graph: { sha256: receipt.scenario.evidence_graph.canonical_sha256, size: receipt.scenario.evidence_graph.canonical_size }, plan: { sha256: receipt.scenario.evaluation_plan.canonical_sha256, size: receipt.scenario.evaluation_plan.canonical_size } };
    const producer = { inputs: { wiki: { sha256: wikiSha } } };
    const cache = { manifest: { registration: { registration_id: receipt.scenario.registration_id, runtime_ref: { content_hash: receipt.scenario.skill_content_sha256 } } } };
    assert.equal(auditScenarioIdentity({ sourceWiki, scenarioRoot, producer, cache, runtimeReceipt: receipt }).status, "PASS");
    receipt.scenario.sources.reverse();
    assert.throws(() => auditScenarioIdentity({ sourceWiki, scenarioRoot, producer, cache, runtimeReceipt: receipt }), (error) => error.code === "CLAUDE_DEEPSEEK_SCENARIO_IDENTITY_MISMATCH");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("provider-local zero-model driver defaults to one role and preserves blind two-role execution", { skip: !process.env.TEST_FLOW_QUICK_PYTHON }, () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "dcm-"));
  try {
    for (const evaluationMode of ["SPECIALIST_ONLY", "BLIND_CONSENSUS"]) for (const repair of [false, true]) {
      const caseRoot = path.join(root, `${evaluationMode === "SPECIALIST_ONLY" ? "s" : "b"}${repair ? "1" : "0"}`);
      fs.mkdirSync(caseRoot);
      const evidenceRoot = path.join(caseRoot, "evidence");
      const receipt = path.join(evidenceRoot, "runtime-receipt.json");
      const args = [RUNTIME, "--mode", "fake", ...(evaluationMode === "BLIND_CONSENSUS" ? ["--evaluation-mode", evaluationMode] : []), ...(repair ? ["--fake-repair"] : []), "--source-root", ROOT, "--work-root", path.join(caseRoot, "work"), "--evidence-root", evidenceRoot, "--receipt-path", receipt];
      const bootstrap = "import runpy,sys,types; mark=types.SimpleNamespace(parametrize=lambda *a,**k:(lambda f:f)); sys.modules['pytest']=types.SimpleNamespace(fixture=lambda f:f,mark=mark); script=sys.argv[1]; sys.argv=sys.argv[1:]; runpy.run_path(script,run_name='__main__')";
      const result = spawnSync(process.env.TEST_FLOW_QUICK_PYTHON, ["-c", bootstrap, ...args], { cwd: ROOT, env: process.env, encoding: "utf8", timeout: 120_000 });
      assert.equal(result.status, 0, result.stderr);
      const value = JSON.parse(fs.readFileSync(receipt, "utf8"));
      assert.equal(value.production_runtime, "problem_locator.runtime.diagnosis_runtime.DiagnosisRuntime");
      assert.equal(value.evaluation_mode, evaluationMode);
      assert.equal(value.model_invocations, 0);
      assert.deepEqual(value.repair_counts, repair
        ? { reviewer: evaluationMode === "BLIND_CONSENSUS" ? 1 : 0, specialist: 1 }
        : { reviewer: 0, specialist: 0 });
      const specialistAttempts = repair ? ["SPECIALIST:PRIMARY", "SPECIALIST:REPAIR"] : ["SPECIALIST:PRIMARY"];
      const reviewerAttempts = evaluationMode === "BLIND_CONSENSUS"
        ? (repair ? ["REVIEWER:PRIMARY", "REVIEWER:REPAIR"] : ["REVIEWER:PRIMARY"])
        : [];
      assert.deepEqual(value.role_attempts.map((item) => `${item.role}:${item.attempt}`), [...specialistAttempts, ...reviewerAttempts]);
      assert.equal(value.methods_result.status, "RESOLVED");
      assert.equal(value.scenario.scenario_id, "multiple-rpc-timeouts");
      const releaseRoot = path.join(ROOT, "tests", "cases", "release", "rpc-timeout-anonymized");
      const loadedMethods = JSON.parse(fs.readFileSync(path.join(evidenceRoot, "methods.json"), "utf8"));
      const driver = JSON.parse(fs.readFileSync(path.join(releaseRoot, "scenarios", "multiple-rpc-timeouts", "driver.json"), "utf8"));
      const inputProjection = { initial_user_fact_names: driver.initial_user_fact_names, initial_user_fact_values: driver.initial_user_fact_values };
      assert.equal(value.scenario.source_wiki_sha256, loadedMethods.source_wiki_sha256);
      assert.equal(value.scenario.user_inputs_sha256, crypto.createHash("sha256").update(canonicalJson(inputProjection)).digest("hex"));
      assert.equal(value.scenario.evidence_graph.ref, value.methods_result.evidence_graph_ref);
      assert.equal(value.scenario.evaluation_plan.ref, value.methods_result.plan_ref);
      assert.deepEqual(value.scenario.sources.map((item) => item.source_id), ["client", "server"]);
      assert.equal(Object.hasOwn(value, "hard_cut"), false);
      for (const name of [
        "methods-source-job.json",
        "methods-evidence-graph-v2.json", "methods-evaluation-plan-v2.json",
        "methods-limitations-v2.json", "methods-source-state-v2.json",
        "methods-source-outcome-v2.json", "methods-result-v2.json", "methods.json",
      ]) assert.equal(fs.existsSync(path.join(evidenceRoot, name)), true, name);
      for (const name of ["methods-reviewer-job.json", "methods-terminal-state-v2.json", "methods-reviewer-outcome-v2.json"]) {
        assert.equal(fs.existsSync(path.join(evidenceRoot, name)), evaluationMode === "BLIND_CONSENSUS", name);
      }
    }
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("provider-local production Runtime archives a disagreement as UNRESOLVED", { skip: !process.env.TEST_FLOW_QUICK_PYTHON }, () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-cert-disagreement-"));
  try {
    const evidenceRoot = path.join(root, "evidence");
    const receiptPath = path.join(evidenceRoot, "runtime-receipt.json");
    const args = [
      RUNTIME,
      "--mode", "fake",
      "--evaluation-mode", "BLIND_CONSENSUS",
      "--fake-reviewer-rejected-method-id", "rpc-call-timeout",
      "--source-root", ROOT,
      "--work-root", path.join(root, "work"),
      "--evidence-root", evidenceRoot,
      "--receipt-path", receiptPath,
    ];
    const bootstrap = "import runpy,sys,types; mark=types.SimpleNamespace(parametrize=lambda *a,**k:(lambda f:f)); sys.modules['pytest']=types.SimpleNamespace(fixture=lambda f:f,mark=mark); script=sys.argv[1]; sys.argv=sys.argv[1:]; runpy.run_path(script,run_name='__main__')";
    const result = spawnSync(process.env.TEST_FLOW_QUICK_PYTHON, ["-c", bootstrap, ...args], { cwd: ROOT, env: process.env, encoding: "utf8", timeout: 120_000 });
    assert.equal(result.status, 0, result.stderr);
    const receipt = JSON.parse(fs.readFileSync(receiptPath, "utf8"));
    assert.equal(receipt.status, "PASS");
    assert.equal(receipt.methods_result.status, "UNRESOLVED");
    assert.equal(receipt.methods_result.reason_code, "SPECIALIST_REVIEWER_DISAGREEMENT");
    assert.match(receipt.methods_result.diagnostic_id, /^diag-[a-f0-9]{64}$/u);
    assert.match(receipt.methods_result.diagnostic_evaluation_ref, /^eval-[a-f0-9]{64}$/u);
    assert.equal(receipt.methods_result.reasons.length, 1);
    assert.match(receipt.methods_result.reasons[0], /Specialist.*Reviewer/u);
    for (const name of [
      "runtime-receipt.json", "methods-evidence-graph-v2.json", "methods-evaluation-plan-v2.json",
      "methods-source-state-v2.json", "methods-source-outcome-v2.json",
      "methods-terminal-state-v2.json", "methods-reviewer-outcome-v2.json", "methods-result-v2.json",
    ]) assert.equal(fs.existsSync(path.join(evidenceRoot, name)), true, name);
    assert.equal(fs.existsSync(path.join(evidenceRoot, "model-cert.json")), false);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("provider-local production Runtime archives every legal early terminal", { skip: !process.env.TEST_FLOW_QUICK_PYTHON }, () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "dce-"));
  try {
    const fixtures = [
      { name: "specialist-protocol", args: ["--fake-protocol-exhausted-role", "SPECIALIST"], status: "UNRESOLVED", reason: "SPECIALIST_PROTOCOL_REPAIR_EXHAUSTED", calls: 2 },
      { name: "specialist-model", args: ["--fake-model-failure-role", "SPECIALIST"], status: "UNRESOLVED", reason: "SPECIALIST_MODEL_EXECUTION_FAILED", calls: 1 },
      { name: "no-evidence", args: ["--fake-no-matching-evidence"], status: "UNRESOLVED", reason: "NO_MATCHING_METHOD_EVIDENCE", calls: 0 },
      { name: "reviewer-model", args: ["--evaluation-mode", "BLIND_CONSENSUS", "--fake-model-failure-role", "REVIEWER"], status: "UNRESOLVED", reason: "REVIEWER_MODEL_EXECUTION_FAILED", calls: 2 },
      { name: "specialist-failed", args: ["--fake-server-invariant-role", "SPECIALIST"], status: "FAILED", reason: "SERVER_INVARIANT_VIOLATION", calls: 1 },
    ];
    const bootstrap = "import runpy,sys,types; mark=types.SimpleNamespace(parametrize=lambda *a,**k:(lambda f:f)); sys.modules['pytest']=types.SimpleNamespace(fixture=lambda f:f,mark=mark); script=sys.argv[1]; sys.argv=sys.argv[1:]; runpy.run_path(script,run_name='__main__')";
    for (const [index, fixture] of fixtures.entries()) {
      const caseRoot = path.join(root, String(index));
      const evidenceRoot = path.join(caseRoot, "evidence");
      const receiptPath = path.join(evidenceRoot, "runtime-receipt.json");
      fs.mkdirSync(caseRoot);
      const args = [RUNTIME, "--mode", "fake", ...fixture.args, "--source-root", ROOT, "--work-root", path.join(caseRoot, "work"), "--evidence-root", evidenceRoot, "--receipt-path", receiptPath];
      const result = spawnSync(process.env.TEST_FLOW_QUICK_PYTHON, ["-c", bootstrap, ...args], { cwd: ROOT, env: process.env, encoding: "utf8", timeout: 120_000 });
      assert.equal(result.status, 0, `${fixture.name}: ${result.stderr}`);
      const receipt = JSON.parse(fs.readFileSync(receiptPath, "utf8"));
      assert.equal(receipt.status, "PASS", fixture.name);
      assert.equal(receipt.methods_result.status, fixture.status, fixture.name);
      assert.equal(receipt.methods_result.reason_code, fixture.reason, fixture.name);
      assert.match(receipt.methods_result.diagnostic_id, /^diag-[a-f0-9]{64}$/u, fixture.name);
      assert.equal(receipt.role_attempts.length, fixture.calls, fixture.name);
      assert.equal(fs.existsSync(path.join(evidenceRoot, "methods-evidence-graph-v2.json")), true, fixture.name);
      assert.equal(fs.existsSync(path.join(evidenceRoot, "methods-evaluation-plan-v2.json")), true, fixture.name);
      assert.equal(fs.existsSync(path.join(evidenceRoot, "methods-source-state-v2.json")), true, fixture.name);
      assert.equal(fs.existsSync(path.join(evidenceRoot, "methods-source-outcome-v2.json")), true, fixture.name);
      assert.equal(fs.existsSync(path.join(evidenceRoot, "methods-result-v2.json")), true, fixture.name);
      assert.equal(fs.existsSync(path.join(evidenceRoot, "model-cert.json")), false, fixture.name);
      if (fixture.name !== "reviewer-model") {
        assert.equal(Object.hasOwn(receipt.records, "reviewer_job"), false, fixture.name);
      }
    }
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("P1 failure adapter exposes the production reason and diagnostic without minting a cert", (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-terminal-failure-"));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const methodsResult = {
    status: "UNRESOLVED",
    reason_code: "SPECIALIST_REVIEWER_DISAGREEMENT",
    reasons: ["Specialist 与 Reviewer 的判定不一致。"],
    diagnostic_id: `diag-${"a".repeat(64)}`,
    diagnostic_evaluation_ref: `eval-${"b".repeat(64)}`,
  };
  const receipt = materializeProviderTerminalFailure({ methods_result: methodsResult }, root, {
    modelCalls: 2,
    repairs: { specialist: 0, reviewer: 0 },
  });
  assert.equal(receipt.code, methodsResult.reason_code);
  assert.equal(receipt.reason, methodsResult.reasons[0]);
  assert.equal(receipt.diagnostic_id, methodsResult.diagnostic_id);
  assert.equal(receipt.evaluation_ref, methodsResult.diagnostic_evaluation_ref);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(root, "adapter-receipt.json"), "utf8")), receipt);
  assert.equal(fs.existsSync(path.join(root, "model-cert.json")), false);
  assert.equal(safeE2EError({ code: receipt.code, message: receipt.reason }).code, "SPECIALIST_REVIEWER_DISAGREEMENT");
});

test("safe model-cert error exposes only a closed code and message", () => {
  assert.deepEqual(safeE2EError({ code: "CLOSED", message: "safe", details: { token: "secret" } }), { schema_version: 1, status: "FAIL", code: "CLOSED", message: "safe" });
});
