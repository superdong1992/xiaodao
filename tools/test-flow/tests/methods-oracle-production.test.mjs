import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  METHODS_V2_BLIND_CONSENSUS_CAPTURED_FILES,
  METHODS_V2_SPECIALIST_ONLY_CAPTURED_FILES,
  validateMethodsV2ExecutionRecords,
  validateMethodsV2RestartSnapshot,
} from "../lib/methods-oracle.mjs";
import { canonicalJson, resolvePythonTestRuntime, sha256Bytes } from "../lib/util.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");

function pythonRuntime() {
  const configuredEnvironment = process.env.TEST_FLOW_QUICK_PYTHON
    ? { ...process.env, TEST_FLOW_PYTHON: process.env.TEST_FLOW_QUICK_PYTHON }
    : process.env;
  const resolved = resolvePythonTestRuntime(REPO_ROOT, configuredEnvironment);
  if (resolved !== null) return { command: resolved.command, prefix: resolved.interpreterPrefix };
  const bundled = process.platform === "win32"
    ? path.join(os.homedir(), ".cache", "codex-runtimes", "codex-primary-runtime", "dependencies", "python", "python.exe")
    : null;
  assert.ok(bundled !== null && fs.existsSync(bundled), "production Methods oracle test requires a Python 3.12 runtime");
  return { command: bundled, prefix: [] };
}

let productionTemplate = null;

function productionBundle() {
  if (productionTemplate !== null) return productionTemplate;
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "methods-v2-casefold-production-"));
  const outputRoot = path.join(root, "bundle");
  const helper = path.join(REPO_ROOT, "tools", "test-flow", "tests", "helpers", "methods_v2_casefold_bundle.py");
  const python = pythonRuntime();
  const completed = spawnSync(python.command, [
    ...python.prefix,
    helper,
    "--output-root", outputRoot,
  ], {
    cwd: REPO_ROOT,
    env: process.env,
    encoding: "utf8",
    timeout: 120_000,
  });
  assert.equal(completed.status, 0, completed.stderr);
  const manifest = JSON.parse(fs.readFileSync(path.join(outputRoot, "bundle-manifest.json"), "utf8"));
  const files = Object.fromEntries(Object.entries(METHODS_V2_BLIND_CONSENSUS_CAPTURED_FILES).map(([key, filename]) => [
    key,
    fs.readFileSync(path.join(manifest.evidence_root, filename)),
  ]));
  productionTemplate = {
    root,
    status: manifest.status,
    inputProvenance: manifest.input_provenance,
    productionRuntime: manifest.production_runtime,
    preprocessingCalls: manifest.preprocessing_calls,
    fixture: {
      files,
      expected: manifest.expected,
      invocations: manifest.invocations,
      publicMethodsResult: manifest.public_methods_result,
      evaluationMode: "BLIND_CONSENSUS",
    },
  };
  return productionTemplate;
}

function copyFixture(fixture) {
  return {
    files: Object.fromEntries(Object.entries(fixture.files).map(([key, value]) => [key, Buffer.from(value)])),
    expected: structuredClone(fixture.expected),
    invocations: structuredClone(fixture.invocations),
    publicMethodsResult: structuredClone(fixture.publicMethodsResult),
    evaluationMode: fixture.evaluationMode,
  };
}

function prefixedRef(prefix, kind, value) {
  return `${prefix}-${sha256Bytes(canonicalJson({ kind, ...value }))}`;
}

function specialistOnlyFixture(blindFixture) {
  const fixture = copyFixture(blindFixture);
  const sourceJob = JSON.parse(fixture.files.source_job.toString("utf8"));
  const terminalState = JSON.parse(fixture.files.terminal_state.toString("utf8"));
  const graph = JSON.parse(fixture.files.evidence_graph.toString("utf8"));
  const plan = JSON.parse(fixture.files.evaluation_plan.toString("utf8"));
  const limitations = JSON.parse(fixture.files.limitations.toString("utf8"));
  const sourceOutcome = JSON.parse(fixture.files.source_outcome.toString("utf8"));
  const sourceState = {
    ...terminalState,
    consensus: null,
    reviewer_evaluation: null,
    reviewer_protocol_failures: 0,
  };
  sourceState.state_ref = prefixedRef("state", "method-state-v2", {
    case_id: sourceState.case_id,
    source_job_id: sourceState.source_job_id,
    evaluation_id: sourceState.evaluation_id,
    plan_ref: sourceState.plan_ref,
    evaluation_refs: sourceState.evaluation_refs,
    status: sourceState.status,
    current_role: sourceState.current_role,
    specialist_protocol_failures: sourceState.specialist_protocol_failures,
    reviewer_protocol_failures: sourceState.reviewer_protocol_failures,
    specialist_evaluation: sourceState.specialist_evaluation,
    reviewer_evaluation: sourceState.reviewer_evaluation,
    consensus: sourceState.consensus,
    reason_code: sourceState.reason_code,
    diagnostic_id: sourceState.diagnostic_id,
    diagnostic_evaluation_ref: sourceState.diagnostic_evaluation_ref,
    reasons: sourceState.reasons,
  });
  const confirmed = sourceState.specialist_evaluation.evaluations.filter((item) => item.verdict === "CONFIRMED");
  const confirmedRefs = confirmed.map((item) => item.evaluation_ref);
  const byEvaluationRef = new Map(plan.evaluations.map((item) => [item.evaluation_ref, item]));
  const confirmedMethods = confirmedRefs.map((ref) => byEvaluationRef.get(ref).method_id);
  const confirmedEventRefs = confirmed.flatMap((item) => item.supporting_event_refs);
  const eventByRef = new Map(graph.events.map((item) => [item.event_ref, item]));
  const confirmedHitRefs = [...new Set(confirmedEventRefs.flatMap((ref) => eventByRef.get(ref).evidence_hit_refs))];
  const evaluations = confirmed.map((item) => {
    const planned = byEvaluationRef.get(item.evaluation_ref);
    return {
      evaluation_ref: item.evaluation_ref,
      method_id: planned.method_id,
      evidence_event_refs: item.supporting_event_refs,
      evidence_hit_refs: [...new Set(item.supporting_event_refs.flatMap((ref) => eventByRef.get(ref).evidence_hit_refs))],
      verdict: "CONFIRMED",
    };
  });
  const resultRef = prefixedRef("result", "method-terminal-result-v2", {
    case_id: sourceJob.case_id,
    source_job_id: sourceJob.job_id,
    terminal_job_id: sourceJob.job_id,
    evaluation_id: sourceState.evaluation_id,
    status: "RESOLVED",
    plan_ref: plan.plan_ref,
    evidence_graph_ref: graph.graph_ref,
    reason_code: null,
    diagnostic_id: sourceState.diagnostic_id,
    diagnostic_evaluation_ref: null,
    evaluations,
    confirmed_evaluation_refs: confirmedRefs,
    confirmed_method_ids: confirmedMethods,
    confirmed_event_refs: confirmedEventRefs,
    confirmed_hit_refs: confirmedHitRefs,
    limitations: limitations.limitations,
    reasons: [],
  });
  const projection = {
    ...fixture.publicMethodsResult,
    source_job_id: sourceJob.job_id,
    result_ref: resultRef,
  };
  delete sourceOutcome.methods_review_target;
  sourceOutcome.methods_terminal_projection = projection;
  fixture.files.source_state = Buffer.from(canonicalJson(sourceState), "utf8");
  fixture.files.source_outcome = Buffer.from(canonicalJson(sourceOutcome), "utf8");
  fixture.files = Object.fromEntries(Object.keys(METHODS_V2_SPECIALIST_ONLY_CAPTURED_FILES).map((key) => [key, fixture.files[key]]));
  fixture.expected.reviewer_job_id = null;
  fixture.invocations = fixture.invocations.filter((item) => item.job_type === "DIAGNOSE");
  fixture.publicMethodsResult = projection;
  fixture.evaluationMode = "SPECIALIST_ONLY";
  return fixture;
}

function mutateRecord(fixture, key, mutate) {
  const value = JSON.parse(fixture.files[key].toString("utf8"));
  mutate(value);
  fixture.files[key] = Buffer.from(canonicalJson(value), "utf8");
}

test.after(() => {
  if (productionTemplate !== null) fs.rmSync(productionTemplate.root, { recursive: true, force: true });
});

test("Methods V2 replay accepts the production scanner's Straße to STRASSE casefold Graph", () => {
  const production = productionBundle();
  assert.equal(production.status, "PASS");
  assert.equal(production.inputProvenance, "hand-authored-untrusted-package-and-log");
  assert.equal(
    production.productionRuntime,
    "problem_locator.runtime.diagnosis_runtime.DiagnosisRuntime",
  );
  assert.equal(production.preprocessingCalls, 0);
  const fixture = production.fixture;
  const graph = JSON.parse(fixture.files.evidence_graph.toString("utf8"));
  assert.equal(graph.hits.length, 1);
  assert.equal(graph.hits[0].marker, "Straße request_id=");
  assert.equal(graph.hits[0].line, "STRASSE request_id=42");
  assert.notEqual(graph.hits[0].marker.toLowerCase(), "strasse request_id=");
  assert.equal(graph.hits[0].method_id, "casefold-method");
  assert.equal(graph.hits[0].marker_index, 1);

  const summary = validateMethodsV2ExecutionRecords(fixture);
  assert.equal(summary.status, "PASS");
  assert.deepEqual(summary.confirmed_method_ids, ["casefold-method"]);
  assert.equal(summary.evidence_hit_count, 1);
  assert.equal(summary.evaluation_count, 1);
  assert.equal(
    summary.method_activation_markers_sha256,
    sha256Bytes(canonicalJson([{ method_id: "casefold-method", activation_markers: ["Straße request_id="] }])),
  );
  assert.deepEqual(Object.keys(summary.record_sha256).sort(), Object.keys(METHODS_V2_BLIND_CONSENSUS_CAPTURED_FILES).sort());

  const caseView = {
    case_id: summary.case_id,
    status: "RESOLVED",
    final_result: null,
    unresolved_result: null,
    generic_result: null,
    generic_result_v2: null,
    methods_result: fixture.publicMethodsResult,
    artifacts: [],
  };
  assert.equal(validateMethodsV2RestartSnapshot({
    caseView,
    artifacts: [],
    methodsSummary: summary,
    restartedFiles: fixture.files,
    evaluationMode: "BLIND_CONSENSUS",
  }), true);
});

test("Methods V2 replay accepts the Specialist-only terminal source records and rejects Reviewer evidence", () => {
  const fixture = specialistOnlyFixture(productionBundle().fixture);
  const summary = validateMethodsV2ExecutionRecords(fixture);
  assert.equal(summary.status, "PASS");
  assert.equal(summary.evaluation_mode, "SPECIALIST_ONLY");
  assert.equal(summary.reviewer_job_id, null);
  assert.equal(summary.reviewer_repair_used, false);
  assert.equal(summary.service_model_calls, 1);
  assert.deepEqual(Object.keys(summary.record_sha256).sort(), Object.keys(METHODS_V2_SPECIALIST_ONLY_CAPTURED_FILES).sort());

  const caseView = {
    case_id: summary.case_id,
    status: "RESOLVED",
    final_result: null,
    unresolved_result: null,
    generic_result: null,
    generic_result_v2: null,
    methods_result: fixture.publicMethodsResult,
    artifacts: [],
  };
  assert.equal(validateMethodsV2RestartSnapshot({
    caseView,
    artifacts: [],
    methodsSummary: summary,
    restartedFiles: fixture.files,
  }), true);

  const reviewerInvocation = copyFixture(fixture);
  reviewerInvocation.invocations.push({
    job_id: "00000000-0000-0000-0000-000000000099",
    job_type: "REVIEW",
    effective_model: "zero-model-role-double",
  });
  assert.throws(
    () => validateMethodsV2ExecutionRecords(reviewerInvocation),
    (error) => error.code === "METHODS_V2_INVOCATION_CARDINALITY",
  );

  const reviewerFile = copyFixture(fixture);
  reviewerFile.files.reviewer_job = productionBundle().fixture.files.reviewer_job;
  assert.throws(
    () => validateMethodsV2ExecutionRecords(reviewerFile),
    (error) => error.code === "METHODS_V2_FILES_INVALID",
  );
});

test("Methods V2 replay rejects one-field mutations of a production-generated bundle", () => {
  const production = productionBundle();
  assert.equal(
    production.productionRuntime,
    "problem_locator.runtime.diagnosis_runtime.DiagnosisRuntime",
  );
  const baseline = production.fixture;

  const wrongMarkerIndex = copyFixture(baseline);
  mutateRecord(wrongMarkerIndex, "evidence_graph", (graph) => { graph.hits[0].marker_index = 2; });
  assert.throws(
    () => validateMethodsV2ExecutionRecords(wrongMarkerIndex),
    (error) => error.code === "METHODS_V2_GRAPH_HIT_INVALID",
  );

  const wrongMethod = copyFixture(baseline);
  mutateRecord(wrongMethod, "evidence_graph", (graph) => { graph.hits[0].method_id = "other-method"; });
  assert.throws(
    () => validateMethodsV2ExecutionRecords(wrongMethod),
    (error) => error.code === "METHODS_V2_GRAPH_HIT_INVALID",
  );

  for (const mutate of [
    (card) => { delete card.activation_markers; },
    (card) => { card.activation_markers = []; },
    (card) => { card.activation_markers = [card.evidence_markers[0], card.evidence_markers[0]]; },
    (card) => { card.activation_markers = ["NOT_A_MARKER"]; },
    (card) => {
      card.evidence_markers.push("SECOND_MARKER");
      card.activation_markers = ["SECOND_MARKER", card.evidence_markers[0]];
    },
  ]) {
    const invalidActivation = copyFixture(baseline);
    mutate(invalidActivation.expected.method_cards[0]);
    assert.throws(
      () => validateMethodsV2ExecutionRecords(invalidActivation),
      (error) => error.code === "METHODS_V2_EXPECTATION_INVALID",
    );
  }

  const noActivationHit = copyFixture(baseline);
  noActivationHit.expected.method_cards[0].evidence_markers.push("UNSEEN_ACTIVATION");
  noActivationHit.expected.method_cards[0].activation_markers = ["UNSEEN_ACTIVATION"];
  assert.throws(
    () => validateMethodsV2ExecutionRecords(noActivationHit),
    (error) => error.code === "METHODS_V2_GRAPH_METHOD_ACTIVATION_MISSING",
  );

  const wrongPlan = copyFixture(baseline);
  mutateRecord(wrongPlan, "evaluation_plan", (plan) => { plan.evidence_graph_ref = `graph-${"0".repeat(64)}`; });
  assert.throws(
    () => validateMethodsV2ExecutionRecords(wrongPlan),
    (error) => error.code === "METHODS_V2_PLAN_IDENTITY_MISMATCH",
  );

  const wrongSourceOutcome = copyFixture(baseline);
  mutateRecord(wrongSourceOutcome, "source_outcome", (outcome) => { outcome.methods_review_target.plan_ref = `plan-${"0".repeat(64)}`; });
  assert.throws(() => validateMethodsV2ExecutionRecords(wrongSourceOutcome));

  const wrongReviewerOutcome = copyFixture(baseline);
  mutateRecord(wrongReviewerOutcome, "reviewer_outcome", (outcome) => { outcome.methods_terminal_projection.result_ref = `result-${"0".repeat(64)}`; });
  assert.throws(() => validateMethodsV2ExecutionRecords(wrongReviewerOutcome));

  const extraRoleField = copyFixture(baseline);
  mutateRecord(extraRoleField, "terminal_state", (state) => { state.reviewer_evaluation.evaluations[0].marker = "Straße"; });
  assert.throws(() => validateMethodsV2ExecutionRecords(extraRoleField));

  const summary = validateMethodsV2ExecutionRecords(baseline);
  const changedCase = {
    case_id: summary.case_id,
    status: "RESOLVED",
    final_result: null,
    unresolved_result: null,
    generic_result: null,
    generic_result_v2: null,
    methods_result: { ...baseline.publicMethodsResult, result_ref: `result-${"f".repeat(64)}` },
    artifacts: [],
  };
  assert.throws(
    () => validateMethodsV2RestartSnapshot({
      caseView: changedCase,
      artifacts: [],
      methodsSummary: summary,
      restartedFiles: baseline.files,
      evaluationMode: "BLIND_CONSENSUS",
    }),
    (error) => error.code === "METHODS_V2_RESTART_CASE_MISMATCH",
  );
});
