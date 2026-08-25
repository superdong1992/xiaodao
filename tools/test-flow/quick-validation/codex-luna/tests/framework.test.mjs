import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  parseArguments,
  lightVerdict,
  sealLightGate,
} from "../run.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("standalone CLI accepts only the two closed goals and scenario matrix", () => {
  assert.equal(parseArguments(["--goal", "methods"]).goal, "methods");
  assert.equal(parseArguments(["--goal", "e2e", "--scenario", "api-execution-overrun"]).scenario, "api-execution-overrun");
  assert.throws(() => parseArguments(["--goal", "release.full"]), (error) => error.code === "LUNA_GOAL_INVALID");
  assert.throws(() => parseArguments(["--goal", "e2e", "--scenario", "../raw"]), (error) => error.code === "LUNA_SCENARIO_INVALID");
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
