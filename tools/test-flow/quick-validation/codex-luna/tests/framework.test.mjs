import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  buildPlan,
  defaults,
  executeSuite,
  parseArguments,
  lightVerdict,
  sealLightGate,
} from "../run.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("standalone CLI accepts only the two closed goals and scenario matrix", () => {
  assert.equal(parseArguments(["--goal", "methods"]).goal, "methods");
  assert.equal(parseArguments(["--goal", "e2e", "--scenario", "api-execution-overrun"]).scenario, "api-execution-overrun");
  assert.equal(parseArguments(["--goal", "e2e", "--scenario", "unrelated-log-noise"]).scenario, "unrelated-log-noise");
  assert.equal(parseArguments(["--goal", "e2e", "--all-scenarios"])["all-scenarios"], true);
  assert.throws(() => parseArguments(["--goal", "e2e", "--all-scenarios", "--scenario", "api-execution-overrun"]), (error) => error.code === "LUNA_SCENARIO_SELECTION_CONFLICT");
  assert.throws(() => parseArguments(["--goal", "release.full"]), (error) => error.code === "LUNA_GOAL_INVALID");
  assert.throws(() => parseArguments(["--goal", "e2e", "--scenario", "../raw"]), (error) => error.code === "LUNA_SCENARIO_INVALID");
});

test("Codex suite plan freezes nine scenarios, 44 calls, and aggregate limits before admission", () => {
  const options = defaults(parseArguments(["--goal", "e2e", "--all-scenarios", "--plan-only"]));
  const plan = buildPlan(options);
  assert.equal(plan.mode, "e2e-suite");
  assert.equal(plan.scenarios.length, 9);
  assert.equal(plan.execution.expected_model_calls, 44);
  assert.equal(plan.execution.token_cap, 18_000_000);
  assert.equal(plan.execution.equivalent_usd_cap, 27);
  assert.equal(plan.execution.wall_timeout_seconds, 16_200);
  assert.equal(plan.execution.per_scenario.find((item) => item.scenario_id === "insufficient-evidence").expected_model_calls, 4);
});

test("blocked Codex suite writes one aggregate verdict and nine NOT_RUN results without a model", async () => {
  const runsRoot = fs.mkdtempSync(path.join(os.tmpdir(), "codex-suite-blocked-"));
  const options = { ...defaults(parseArguments(["--goal", "e2e", "--all-scenarios"])), runsRoot };
  const result = await executeSuite(options, buildPlan(options));
  assert.equal(result.verdict.status, "BLOCKED");
  assert.equal(result.verdict.model_calls.actual, 0);
  assert.equal(result.verdict.scenarios.filter((item) => item.status === "NOT_RUN").length, 9);
});

function fakeCodexExecutor({ failureAt = null, failureCode = null, seen }) {
  return async (options, childPlan, { runRoot }) => {
    seen.push({ scenario: options.scenario, runRoot, usageRoot: path.join(runRoot, "usage"), evidenceRoot: path.join(runRoot, "evidence") });
    fs.mkdirSync(runRoot, { recursive: true });
    const failed = options.scenario === failureAt;
    const actual = childPlan.execution.expected_model_calls;
    const verdict = {
      status: failed ? "FAIL" : "PASS",
      model_calls: { expected: actual, actual, retry_count: 0 },
      usage: { input_tokens: actual * 10, cost_usd: actual / 100 },
      failure: failed ? { code: failureCode, message: "closed fixture failure" } : null,
      failure_domain: null,
    };
    fs.writeFileSync(path.join(runRoot, "verdict.json"), JSON.stringify(verdict));
    return { verdict, runRoot, exitCode: failed ? 1 : 0 };
  };
}

test("Codex suite continues after a contract failure and keeps nine child roots fully isolated", async () => {
  const runsRoot = fs.mkdtempSync(path.join(os.tmpdir(), "codex-suite-contract-"));
  const options = { ...defaults(parseArguments(["--goal", "e2e", "--all-scenarios"])), runsRoot, allowRealModel: true };
  const plan = { ...buildPlan(options), admission: { status: "READY", blockers: [] } };
  const seen = [];
  const result = await executeSuite(options, plan, {
    executeOneImpl: fakeCodexExecutor({ failureAt: plan.scenarios[0], failureCode: "MACOS_CODEX_LUNA_EXPECTED_TERM_MISSING", seen }),
    runSuiteContractsImpl(destination) { fs.writeFileSync(destination, "fixture preflight\n"); },
  });
  assert.equal(result.verdict.status, "FAIL");
  assert.equal(result.verdict.stop_reason, null);
  assert.deepEqual(seen.map((item) => item.scenario), plan.scenarios);
  assert.equal(new Set(seen.map((item) => item.runRoot)).size, 9);
  assert.equal(new Set(seen.map((item) => item.usageRoot)).size, 9);
  assert.equal(new Set(seen.map((item) => item.evidenceRoot)).size, 9);
  assert.equal(result.verdict.scenarios[0].failure_domain, "CONTRACT");
  assert.equal(result.verdict.model_calls.actual, result.verdict.scenarios.reduce((sum, item) => sum + item.model_calls.actual, 0));
  assert.equal(result.verdict.usage.input_tokens, result.verdict.scenarios.reduce((sum, item) => sum + item.usage.input_tokens, 0));
  assert.equal(result.verdict.summary.completed, 9);
  assert.equal(result.verdict.summary.not_run, 0);
});

test("Codex suite stops immediately after an engineering failure", async () => {
  const runsRoot = fs.mkdtempSync(path.join(os.tmpdir(), "codex-suite-engineering-"));
  const options = { ...defaults(parseArguments(["--goal", "e2e", "--all-scenarios"])), runsRoot, allowRealModel: true };
  const plan = { ...buildPlan(options), admission: { status: "READY", blockers: [] } };
  const seen = [];
  const result = await executeSuite(options, plan, {
    executeOneImpl: fakeCodexExecutor({ failureAt: plan.scenarios[1], failureCode: "MACOS_CODEX_LUNA_MCP_READINESS_TIMEOUT", seen }),
    runSuiteContractsImpl(destination) { fs.writeFileSync(destination, "fixture preflight\n"); },
  });
  assert.equal(result.verdict.status, "ERROR");
  assert.deepEqual(seen.map((item) => item.scenario), plan.scenarios.slice(0, 2));
  assert.equal(result.verdict.summary.completed, 2);
  assert.equal(result.verdict.summary.attempted, 2);
  assert.equal(result.verdict.summary.not_run, 7);
  assert.equal(result.verdict.scenarios[2].status, "NOT_RUN");
  assert.equal(result.verdict.stop_reason.domain, "ENGINEERING");
  assert.equal(result.verdict.stop_reason.failure.scenario_id, plan.scenarios[1]);
});

test("standalone entry does not import or invoke the old orchestrator stack", () => {
  const source = fs.readFileSync(path.join(ROOT, "run.mjs"), "utf8");
  for (const forbidden of [
    "tools/test-flow/run.sh",
    "lib/planner.mjs",
    "lib/engine.mjs",
    "lib/source-snapshot.mjs",
    "lib/evidence.mjs",
  ]) assert.equal(source.includes(forbidden), false, forbidden);
  assert.match(source, /old_test_flow_orchestrator: false/);
  assert.match(source, /source_snapshot: false/);
  assert.match(source, /automatic_retry: false/);
  assert.match(source, /security_and_permission_proof: false/);
});

test("light Gate seals exact evidence and rejects call-count drift", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-light-gate-"));
  const evidenceRoot = path.join(root, "evidence");
  fs.mkdirSync(evidenceRoot);
  const required = ["codex-identity.json", "model-usage.json", "methods-package.json"];
  for (const name of required) fs.writeFileSync(path.join(evidenceRoot, name), "{}\n");
  fs.writeFileSync(path.join(evidenceRoot, "model-invocations.json"), '{"invocations":[]}\n');
  fs.writeFileSync(path.join(evidenceRoot, "adapter-receipt.json"), '{"status":"PASS"}\n');
  const pass = sealLightGate({ goal: "methods", mode: "cache-verification", evidenceRoot, expectedCalls: 0 });
  assert.equal(pass.status, "PASS");
  assert.equal(pass.actual_model_calls, 0);

  const otherRoot = path.join(root, "other");
  fs.mkdirSync(otherRoot);
  for (const name of required) fs.writeFileSync(path.join(otherRoot, name), "{}\n");
  fs.writeFileSync(path.join(otherRoot, "model-invocations.json"), '{"invocations":[]}\n');
  fs.writeFileSync(path.join(otherRoot, "adapter-receipt.json"), '{"status":"PASS"}\n');
  assert.equal(sealLightGate({ goal: "methods", mode: "bootstrap", evidenceRoot: otherRoot, expectedCalls: 1 }).status, "FAIL");
});

test("light verdict retains only the closed failure projection", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-light-verdict-"));
  const evidenceRoot = path.join(root, "evidence");
  fs.mkdirSync(evidenceRoot);
  fs.writeFileSync(path.join(evidenceRoot, "gate-receipt.json"), "{}\n");
  const verdict = lightVerdict({
    runId: "run",
    plan: { goal: "e2e", mode: "e2e", scenario: "api-execution-overrun", plan_sha256: "0".repeat(64) },
    gate: { status: "FAIL", expected_model_calls: 5, actual_model_calls: null, retry_count: 0, usage: null, failure: { code: "CODE", details: { id: 5, response_code: -1, response_message: "closed" } } },
    startedAt: "2026-08-24T00:00:00.000Z",
    finishedAt: "2026-08-24T00:00:01.000Z",
    runRoot: root,
  });
  assert.deepEqual(verdict.failure.details, { id: 5, response_code: -1, response_message: "closed" });
});
