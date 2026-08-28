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
    tool_policy: { tools: ["Read", "Write"], readable_scope: "job-workspace-inputs", writable_scope: role === "SPECIALIST" ? "output/method-diagnosis.draft.json" : "output/method-review.draft.json", network: false, shell: false, skill_loading: false },
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

function runtimeReceipt(invocations) {
  return {
    schema_version: 1,
    status: "PASS",
    execution_mode: "real-model",
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
  assert.equal(auditRuntimeAndInvocations(receipt, invocations).prompt_count, 2);
  const drifted = structuredClone(invocations);
  drifted[1].prompt.sha256 = "f".repeat(64);
  assert.throws(() => auditRuntimeAndInvocations(receipt, drifted), (error) => error.code === "CLAUDE_DEEPSEEK_RUNTIME_INVOCATION_IDENTITY_MISMATCH");
});

test("P1 model-cert input binds provider revision, calls, usage, Core, and methods_result", () => {
  const invocations = [invocation("SPECIALIST", "PRIMARY", 1), invocation("REVIEWER", "PRIMARY", 2)];
  const sourceRoot = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-cert-source-"));
  try {
    const runtime = path.join(sourceRoot, "src", "problem_locator", "runtime");
    fs.mkdirSync(runtime, { recursive: true });
    fs.writeFileSync(path.join(runtime, "diagnosis_runtime.py"), "# production runtime\n");
    const receipt = buildModelCertInput({
      sourceSnapshotDigest: "a".repeat(64), contractManifestSha256: "b".repeat(64), coreVerdictSha256: "c".repeat(64), scenarioOracleSha256: "f".repeat(64),
      identity: { settings: { fingerprint: "d".repeat(64) }, cli: { version: "2.1.89" }, model: "deepseek-v4-flash[1m]", max_output_tokens: 64000 },
      invocations, usage: { input_tokens: 2, output_tokens: 2, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, total_tokens: 4, cost_usd: 0 },
      runtimeReceipt: runtimeReceipt(invocations), sourceRoot,
    });
    assert.deepEqual(Object.keys(receipt).sort(), ["call_counts", "certification_target", "contract_manifest", "core_verdict", "scenario_oracle", "execution_identity", "invocations", "methods_result", "model", "provider", "receipt_type", "scenario", "schema_version", "source_snapshot_digest", "status", "usage"].sort());
    assert.equal(receipt.certification_target, "P1");
    assert.equal(receipt.model.revision_source, "settings-fingerprint");
    assert.equal(receipt.call_counts.total_calls, 2);
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

test("provider-local zero-model driver proves the normal two calls and four-call repair cap", { skip: !process.env.TEST_FLOW_QUICK_PYTHON }, () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-cert-zero-model-"));
  try {
    for (const repair of [false, true]) {
      const caseRoot = path.join(root, repair ? "repair" : "normal");
      fs.mkdirSync(caseRoot);
      const evidenceRoot = path.join(caseRoot, "evidence");
      const receipt = path.join(evidenceRoot, "runtime-receipt.json");
      const args = [RUNTIME, "--mode", "fake", ...(repair ? ["--fake-repair"] : []), "--source-root", ROOT, "--work-root", path.join(caseRoot, "work"), "--evidence-root", evidenceRoot, "--receipt-path", receipt];
      const bootstrap = "import runpy,sys,types; mark=types.SimpleNamespace(parametrize=lambda *a,**k:(lambda f:f)); sys.modules['pytest']=types.SimpleNamespace(fixture=lambda f:f,mark=mark); script=sys.argv[1]; sys.argv=sys.argv[1:]; runpy.run_path(script,run_name='__main__')";
      const result = spawnSync(process.env.TEST_FLOW_QUICK_PYTHON, ["-c", bootstrap, ...args], { cwd: ROOT, env: process.env, encoding: "utf8", timeout: 120_000 });
      assert.equal(result.status, 0, result.stderr);
      const value = JSON.parse(fs.readFileSync(receipt, "utf8"));
      assert.equal(value.production_runtime, "problem_locator.runtime.diagnosis_runtime.DiagnosisRuntime");
      assert.equal(value.model_invocations, 0);
      assert.deepEqual(value.repair_counts, repair ? { reviewer: 1, specialist: 1 } : { reviewer: 0, specialist: 0 });
      assert.deepEqual(value.role_attempts.map((item) => `${item.role}:${item.attempt}`), repair
        ? ["SPECIALIST:PRIMARY", "SPECIALIST:REPAIR", "REVIEWER:PRIMARY", "REVIEWER:REPAIR"]
        : ["SPECIALIST:PRIMARY", "REVIEWER:PRIMARY"]);
      assert.equal(value.methods_result.status, "RESOLVED");
      assert.equal(value.scenario.scenario_id, "multiple-rpc-timeouts");
      const releaseRoot = path.join(ROOT, "tests", "cases", "release", "rpc-timeout-anonymized");
      const wikiBytes = fs.readFileSync(path.join(releaseRoot, "input", "wiki.md"));
      const driver = JSON.parse(fs.readFileSync(path.join(releaseRoot, "scenarios", "multiple-rpc-timeouts", "driver.json"), "utf8"));
      const inputProjection = { initial_user_fact_names: driver.initial_user_fact_names, initial_user_fact_values: driver.initial_user_fact_values };
      assert.equal(value.scenario.source_wiki_sha256, crypto.createHash("sha256").update(wikiBytes).digest("hex"));
      assert.equal(value.scenario.user_inputs_sha256, crypto.createHash("sha256").update(canonicalJson(inputProjection)).digest("hex"));
      assert.equal(value.scenario.evidence_graph.ref, value.methods_result.evidence_graph_ref);
      assert.equal(value.scenario.evaluation_plan.ref, value.methods_result.plan_ref);
      assert.deepEqual(value.scenario.sources.map((item) => item.source_id), ["client", "server"]);
      assert.equal(Object.hasOwn(value, "hard_cut"), false);
      for (const name of [
        "methods-source-job.json", "methods-reviewer-job.json",
        "methods-evidence-graph-v2.json", "methods-evaluation-plan-v2.json",
        "methods-limitations-v2.json", "methods-source-state-v2.json",
        "methods-source-outcome-v2.json", "methods-terminal-state-v2.json",
        "methods-reviewer-outcome-v2.json", "methods-result-v2.json", "methods.json",
      ]) assert.equal(fs.existsSync(path.join(evidenceRoot, name)), true, name);
    }
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("safe model-cert error exposes only a closed code and message", () => {
  assert.deepEqual(safeE2EError({ code: "CLOSED", message: "safe", details: { token: "secret" } }), { schema_version: 1, status: "FAIL", code: "CLOSED", message: "safe" });
});
