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
  sealLightGate,
} from "../run.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("standalone CLI keeps Methods generation and exposes one P2 model-cert scenario", () => {
  assert.equal(parseArguments(["--goal", "methods"]).goal, "methods");
  assert.equal(parseArguments(["--goal", "e2e", "--scenario", "multiple-rpc-timeouts"]).scenario, "multiple-rpc-timeouts");
  assert.throws(() => parseArguments(["--goal", "e2e", "--all-scenarios"]), { code: "LUNA_MODEL_CERT_SUITE_FORBIDDEN" });
  assert.throws(() => parseArguments(["--goal", "e2e", "--scenario", "api-execution-overrun"]), { code: "LUNA_SCENARIO_INVALID" });
  assert.throws(() => parseArguments(["--goal", "release.full"]), { code: "LUNA_GOAL_INVALID" });
});

test("P2 plan freezes source/Core bindings, normal two calls and a four-call hard cap", () => {
  const options = defaults(parseArguments(["--goal", "e2e", "--scenario", "multiple-rpc-timeouts", "--plan-only"]));
  const plan = buildPlan(options);
  assert.equal(plan.mode, "model-cert");
  assert.deepEqual(plan.scenarios, ["multiple-rpc-timeouts"]);
  assert.equal(plan.execution.expected_model_calls, 2);
  assert.equal(plan.execution.model_call_hard_cap, 4);
  assert.equal(plan.execution.wall_timeout_seconds, 2700);
  assert.equal(plan.execution.per_scenario[0].model_call_hard_cap, 4);
  assert.equal(plan.execution.source_snapshot, true);
  assert.ok(plan.admission.blockers.some((item) => item.code === "LUNA_SOURCE_SNAPSHOT_REQUIRED"));
  assert.ok(plan.admission.blockers.some((item) => item.code === "LUNA_CORE_VERDICT_REQUIRED"));
  assert.equal(plan.admission.blockers.some((item) => item.code === "EVIDENCE_V2_REAL_DIAGNOSIS_ADAPTER_UNMIGRATED"), false);
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

test("light Gate accepts P2 normal/repair counts and rejects a fifth call", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-light-gate-"));
  const required = REQUIRED_EVIDENCE.e2e;
  const normal = path.join(root, "normal");
  writeEvidence(normal, required, 2);
  assert.equal(sealLightGate({ goal: "e2e", mode: "model-cert", evidenceRoot: normal, expectedCalls: 2 }).status, "PASS");
  const repaired = path.join(root, "repaired");
  writeEvidence(repaired, required, 4);
  assert.equal(sealLightGate({ goal: "e2e", mode: "model-cert", evidenceRoot: repaired, expectedCalls: 2 }).status, "PASS");
  const fifth = path.join(root, "fifth");
  writeEvidence(fifth, required, 5);
  assert.equal(sealLightGate({ goal: "e2e", mode: "model-cert", evidenceRoot: fifth, expectedCalls: 2 }).status, "FAIL");
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
