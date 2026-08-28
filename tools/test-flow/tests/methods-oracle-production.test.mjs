import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  METHODS_V2_CAPTURED_FILES,
  validateMethodsV2ExecutionRecords,
  validateMethodsV2RestartSnapshot,
} from "../lib/methods-oracle.mjs";
import { canonicalJson, resolvePythonTestRuntime } from "../lib/util.mjs";

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
  const files = Object.fromEntries(Object.entries(METHODS_V2_CAPTURED_FILES).map(([key, filename]) => [
    key,
    fs.readFileSync(path.join(manifest.evidence_root, filename)),
  ]));
  productionTemplate = {
    root,
    fixture: {
      files,
      expected: manifest.expected,
      invocations: manifest.invocations,
      publicMethodsResult: manifest.public_methods_result,
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
  };
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
  const fixture = productionBundle().fixture;
  const graph = JSON.parse(fixture.files.evidence_graph.toString("utf8"));
  assert.equal(graph.hits.length, 1);
  assert.equal(graph.hits[0].marker, "Straße");
  assert.equal(graph.hits[0].line, "STRASSE request_id=42");
  assert.equal(graph.hits[0].method_id, "casefold-method");
  assert.equal(graph.hits[0].marker_index, 1);

  const summary = validateMethodsV2ExecutionRecords(fixture);
  assert.equal(summary.status, "PASS");
  assert.deepEqual(summary.confirmed_method_ids, ["casefold-method"]);
  assert.equal(summary.evidence_hit_count, 1);
  assert.equal(summary.evaluation_count, 1);
  assert.deepEqual(Object.keys(summary.record_sha256).sort(), Object.keys(METHODS_V2_CAPTURED_FILES).sort());

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
});

test("Methods V2 replay rejects one-field mutations of a production-generated bundle", () => {
  const baseline = productionBundle().fixture;

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
    () => validateMethodsV2RestartSnapshot({ caseView: changedCase, artifacts: [], methodsSummary: summary, restartedFiles: baseline.files }),
    (error) => error.code === "METHODS_V2_RESTART_CASE_MISMATCH",
  );
});
