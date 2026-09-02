import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  buildPlan,
  defaults,
  lightVerdict,
  parseArguments,
  REQUIRED_EVIDENCE,
  safeFailure,
  sealLightGate,
} from "../run.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("standalone CLI keeps the historical Fast E2E matrix separate from P2", () => {
  assert.equal(parseArguments(["--goal", "methods"]).goal, "methods");
  assert.equal(parseArguments(["--goal", "fast-e2e", "--scenario", "api-execution-overrun"]).scenario, "api-execution-overrun");
  assert.equal(parseArguments(["--goal", "fast-e2e", "--all-scenarios"])["all-scenarios"], true);
  assert.throws(() => parseArguments(["--goal", "fast-e2e", "--scenario", "api-execution-overrun", "--all-scenarios"]), { code: "LUNA_SCENARIO_SELECTION_CONFLICT" });
  assert.throws(() => parseArguments(["--goal", "fast-e2e", "--scenario", "release-decoy"]), { code: "LUNA_SCENARIO_INVALID" });
  assert.equal(parseArguments(["--goal", "e2e", "--scenario", "multiple-rpc-timeouts"]).scenario, "multiple-rpc-timeouts");
  assert.equal(defaults(parseArguments(["--goal", "e2e"])).evaluationMode, "SPECIALIST_ONLY");
  assert.equal(defaults(parseArguments(["--goal", "e2e", "--evaluation-mode", "BLIND_CONSENSUS"])).evaluationMode, "BLIND_CONSENSUS");
  assert.throws(() => parseArguments(["--goal", "e2e", "--evaluation-mode", "invalid"]), { code: "LUNA_EVALUATION_MODE_INVALID" });
  assert.throws(() => parseArguments(["--goal", "fast-e2e", "--evaluation-mode", "BLIND_CONSENSUS"]), { code: "LUNA_EVALUATION_MODE_FORBIDDEN" });
  assert.throws(() => parseArguments(["--goal", "e2e", "--all-scenarios"]), { code: "LUNA_MODEL_CERT_SUITE_FORBIDDEN" });
  assert.throws(() => parseArguments(["--goal", "e2e", "--scenario", "api-execution-overrun"]), { code: "LUNA_SCENARIO_INVALID" });
  assert.throws(() => parseArguments(["--goal", "release.full"]), { code: "LUNA_GOAL_INVALID" });
});

test("Fast failure receipts retain the mechanical reason and comparison details", () => {
  const error = new Error("oracle mismatch");
  error.code = "MACOS_CODEX_LUNA_DIAGNOSIS_STATUS_MISMATCH";
  error.reason_code = "NO_MATCHING_METHOD_EVIDENCE";
  error.diagnostic_id = "diag-example";
  error.details = { expected: ["target"], actual: ["noise"], stderr: "driver failed" };
  assert.deepEqual(safeFailure(error), {
    code: error.code,
    message: error.message,
    reason_code: error.reason_code,
    diagnostic_id: error.diagnostic_id,
    details: error.details,
  });
});

test("Fast E2E plan uses the nine historical inputs without Core or source snapshot", () => {
  const options = defaults(parseArguments(["--goal", "fast-e2e", "--all-scenarios", "--plan-only"]));
  const plan = buildPlan(options);
  assert.equal(plan.mode, "fast-e2e-suite");
  assert.deepEqual(plan.scenarios, [
    "api-execution-overrun",
    "client-receive-blocked",
    "deadloop-detected",
    "insufficient-evidence",
    "multiple-rpc-timeouts",
    "server-queue-delay",
    "server-queue-five",
    "server-queue-single",
    "unrelated-log-noise",
  ]);
  assert.equal(plan.execution.expected_model_calls, 16);
  assert.equal(plan.execution.model_call_hard_cap, 32);
  const insufficient = plan.execution.per_scenario.find((item) => item.scenario_id === "insufficient-evidence");
  assert.equal(insufficient.expected_model_calls, 0);
  assert.equal(insufficient.model_call_hard_cap, 0);
  assert.equal(insufficient.token_cap, 0);
  assert.equal(insufficient.equivalent_usd_cap, 0);
  assert.equal(plan.execution.token_cap, 16_000_000);
  assert.equal(plan.execution.equivalent_usd_cap, 24);
  assert.equal(plan.execution.source_snapshot, false);
  assert.equal(plan.admission.blockers.some((item) => item.code === "LUNA_SOURCE_SNAPSHOT_REQUIRED"), false);
  assert.equal(plan.admission.blockers.some((item) => item.code === "LUNA_CORE_VERDICT_REQUIRED"), false);
  assert.equal(plan.inputs.source_snapshot_digest, null);
  assert.equal(plan.inputs.core_verdict, null);
  assert.equal(plan.inputs.methods_cache.status, "NOT_REQUIRED");
  assert.ok(plan.admission.blockers.some((item) => item.code === "LUNA_FAST_E2E_REGISTRATION_ROOT_REQUIRED"));
});

test("Fast evidence requires Reviewer records except on the zero-evaluation scenario", () => {
  const normal = buildPlan(defaults(parseArguments([
    "--goal", "fast-e2e", "--scenario", "api-execution-overrun", "--plan-only",
  ])));
  const insufficient = buildPlan(defaults(parseArguments([
    "--goal", "fast-e2e", "--scenario", "insufficient-evidence", "--plan-only",
  ])));
  assert.equal(normal.evidence.includes("methods-reviewer-job.json"), true);
  assert.equal(normal.evidence.includes("methods-reviewer-outcome-v2.json"), true);
  assert.equal(insufficient.evidence.includes("methods-reviewer-job.json"), false);
  assert.equal(insufficient.evidence.includes("methods-reviewer-outcome-v2.json"), false);
});

test("P2 plan defaults to Specialist-only and preserves explicit blind consensus", () => {
  const options = defaults(parseArguments(["--goal", "e2e", "--scenario", "multiple-rpc-timeouts", "--plan-only"]));
  const plan = buildPlan(options);
  assert.equal(plan.mode, "model-cert");
  assert.equal(plan.evaluation_mode, "SPECIALIST_ONLY");
  assert.deepEqual(plan.scenarios, ["multiple-rpc-timeouts"]);
  assert.equal(plan.execution.expected_model_calls, 1);
  assert.equal(plan.execution.model_call_hard_cap, 2);
  assert.equal(plan.execution.wall_timeout_seconds, 2700);
  assert.equal(plan.execution.per_scenario[0].model_call_hard_cap, 2);
  assert.equal(plan.evidence.includes("methods-reviewer-job.json"), false);
  assert.equal(plan.evidence.includes("methods-terminal-state-v2.json"), false);
  assert.equal(plan.evidence.includes("methods-reviewer-outcome-v2.json"), false);
  assert.equal(plan.execution.source_snapshot, true);
  assert.ok(plan.admission.blockers.some((item) => item.code === "LUNA_SOURCE_SNAPSHOT_REQUIRED"));
  assert.ok(plan.admission.blockers.some((item) => item.code === "LUNA_CORE_VERDICT_REQUIRED"));
  assert.equal(plan.admission.blockers.some((item) => item.code === "EVIDENCE_V2_REAL_DIAGNOSIS_ADAPTER_UNMIGRATED"), false);

  const blind = buildPlan(defaults(parseArguments(["--goal", "e2e", "--evaluation-mode", "BLIND_CONSENSUS", "--plan-only"])));
  assert.equal(blind.evaluation_mode, "BLIND_CONSENSUS");
  assert.equal(blind.execution.expected_model_calls, 2);
  assert.equal(blind.execution.model_call_hard_cap, 4);
  assert.equal(blind.evidence.includes("methods-reviewer-job.json"), true);
  assert.equal(blind.evidence.includes("methods-terminal-state-v2.json"), true);
  assert.equal(blind.evidence.includes("methods-reviewer-outcome-v2.json"), true);
});

test("Methods generation does not inherit model-cert source/Core requirements", () => {
  const plan = buildPlan(defaults(parseArguments(["--goal", "methods", "--plan-only"])));
  assert.equal(plan.admission.blockers.some((item) => item.code === "LUNA_SOURCE_SNAPSHOT_REQUIRED"), false);
  assert.equal(plan.admission.blockers.some((item) => item.code === "LUNA_CORE_VERDICT_REQUIRED"), false);
});

test("standalone entry does not import the central orchestrator or old CrossJob runner", () => {
  const source = fs.readFileSync(path.join(ROOT, "run.mjs"), "utf8");
  for (const forbidden of ["tools/test-flow/run.sh", "lib/planner.mjs", "lib/engine.mjs", "lib/source-snapshot.mjs", "cross-job-core.mjs"]) assert.equal(source.includes(forbidden), false, forbidden);
  assert.match(source, /old_test_flow_orchestrator: false/);
  assert.match(source, /source_snapshot: options\.goal === "e2e"/);
  assert.match(source, /automatic_retry: false/);
});

function writeEvidence(root, names, calls) {
  fs.mkdirSync(root);
  for (const name of names) {
    if (name === "model-invocations.json") fs.writeFileSync(path.join(root, name), JSON.stringify({ invocations: Array.from({ length: calls }, (_, index) => ({ invocation_id: `call-${index}` })) }));
    else if (name === "model-usage.json") fs.writeFileSync(path.join(root, name), '{"aggregate":{}}\n');
    else if (name === "adapter-receipt.json") fs.writeFileSync(path.join(root, name), '{"status":"PASS"}\n');
    else fs.writeFileSync(path.join(root, name), "{}\n");
  }
}

test("light Gate accepts default P2 one/two calls and optional blind two/four calls", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-light-gate-"));
  const reviewEvidence = new Set(["methods-reviewer-job.json", "methods-terminal-state-v2.json", "methods-reviewer-outcome-v2.json"]);
  const specialistRequired = REQUIRED_EVIDENCE.e2e.filter((name) => !reviewEvidence.has(name));
  for (const calls of [1, 2]) {
    const evidenceRoot = path.join(root, `specialist-${calls}`);
    writeEvidence(evidenceRoot, specialistRequired, calls);
    assert.equal(sealLightGate({ goal: "e2e", mode: "model-cert", evidenceRoot, expectedCalls: 1, modelCallHardCap: 2, requiredEvidence: specialistRequired }).status, "PASS");
  }
  for (const calls of [2, 4]) {
    const evidenceRoot = path.join(root, `blind-${calls}`);
    writeEvidence(evidenceRoot, REQUIRED_EVIDENCE.e2e, calls);
    assert.equal(sealLightGate({ goal: "e2e", mode: "model-cert", evidenceRoot, expectedCalls: 2, modelCallHardCap: 4 }).status, "PASS");
  }
  const fifth = path.join(root, "fifth");
  writeEvidence(fifth, REQUIRED_EVIDENCE.e2e, 5);
  assert.equal(sealLightGate({ goal: "e2e", mode: "model-cert", evidenceRoot: fifth, expectedCalls: 2, modelCallHardCap: 4 }).status, "FAIL");
});

test("light Gate applies the same two/four call bound to one Fast E2E scenario", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-fast-light-gate-"));
  for (const calls of [2, 4]) {
    const evidenceRoot = path.join(root, String(calls));
    writeEvidence(evidenceRoot, REQUIRED_EVIDENCE["fast-e2e"], calls);
    assert.equal(sealLightGate({ goal: "fast-e2e", mode: "fast-e2e", evidenceRoot, expectedCalls: 2 }).status, "PASS");
  }
  const fifth = path.join(root, "5");
  writeEvidence(fifth, REQUIRED_EVIDENCE["fast-e2e"], 5);
  assert.equal(sealLightGate({ goal: "fast-e2e", mode: "fast-e2e", evidenceRoot: fifth, expectedCalls: 2 }).status, "FAIL");
});

test("insufficient-evidence Fast E2E rejects any model call", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-insufficient-light-gate-"));
  const zero = path.join(root, "zero");
  writeEvidence(zero, REQUIRED_EVIDENCE["fast-e2e"], 0);
  assert.equal(sealLightGate({ goal: "fast-e2e", mode: "fast-e2e", evidenceRoot: zero, expectedCalls: 0, modelCallHardCap: 0 }).status, "PASS");
  const one = path.join(root, "one");
  writeEvidence(one, REQUIRED_EVIDENCE["fast-e2e"], 1);
  assert.equal(sealLightGate({ goal: "fast-e2e", mode: "fast-e2e", evidenceRoot: one, expectedCalls: 0, modelCallHardCap: 0 }).status, "FAIL");
});

test("light verdict retains only the closed failure projection", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-light-verdict-"));
  const evidenceRoot = path.join(root, "evidence");
  fs.mkdirSync(evidenceRoot);
  fs.writeFileSync(path.join(evidenceRoot, "gate-receipt.json"), "{}\n");
  const verdict = lightVerdict({
    runId: "run",
    plan: { goal: "e2e", mode: "model-cert", scenario: "multiple-rpc-timeouts", plan_sha256: "0".repeat(64) },
    gate: { status: "FAIL", expected_model_calls: 2, actual_model_calls: null, retry_count: 0, usage: null, failure: { code: "CODE", details: { id: 5, response_code: -1, response_message: "closed" } } },
    startedAt: "2026-08-29T00:00:00.000Z",
    finishedAt: "2026-08-29T00:00:01.000Z",
    runRoot: root,
  });
  assert.deepEqual(verdict.failure.details, { id: 5, response_code: -1, response_message: "closed" });
});
