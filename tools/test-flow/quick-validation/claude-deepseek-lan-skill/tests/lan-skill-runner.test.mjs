import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { buildPlan, defaults, parseArguments } from "../run.mjs";


test("runner accepts only the two independent goals and explicit diagnosis scenarios", () => {
  assert.deepEqual(parseArguments(["--goal", "generation", "--plan-only"]), { goal: "generation", "plan-only": true });
  assert.deepEqual(parseArguments(["--goal", "diagnosis", "--scenario", "missing-slots"]), { goal: "diagnosis", scenario: "missing-slots" });
  assert.throws(() => parseArguments(["--goal", "diagnosis"]), /requires --scenario/u);
  assert.throws(() => parseArguments(["--goal", "generation", "--all-scenarios"]), /does not accept/u);
  assert.throws(() => parseArguments(["--goal", "other"]), /--goal must be/u);
});


test("retry context is all-or-nothing and never enables automatic retry", () => {
  assert.throws(() => parseArguments(["--goal", "generation", "--reason", "provider failed"]), /requires reason, hypothesis/u);
  const values = parseArguments([
    "--goal", "generation",
    "--reason", "contract failure",
    "--hypothesis", "slot prefix drifted",
    "--expected-evidence", "validator reports the prefix",
  ]);
  assert.equal(values.reason, "contract failure");
});


test("generation planning is standalone and diagnosis fails closed without its cache", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lan-runner-plan-"));
  const claude = path.join(root, "cli.js");
  const settings = path.join(root, "settings.json");
  const python = path.join(root, "python");
  for (const target of [claude, settings, python]) fs.writeFileSync(target, "fixture\n");
  const identity = {
    schema_version: 1,
    version: "2.1.89",
    cli_sha256: "a".repeat(64),
    settings_fingerprint: "b".repeat(64),
    model: "deepseek-v4-flash[1m]",
  };
  const generationOptions = defaults({ goal: "generation", "claude-entry": claude, "claude-settings": settings, "python-entry": python, "cache-root": path.join(root, "cache"), "runs-root": path.join(root, "runs") }, {});
  const generation = buildPlan(generationOptions, { platform: { status: "SUPPORTED", topology: "test", sealed: false }, claudeIdentity: identity });
  assert.equal(generation.admission.status, "READY");
  assert.equal(generation.execution.central_test_flow, false);
  assert.equal(generation.execution.release_claim, false);
  assert.equal(generation.execution.retry_policy, "NONE");
  assert.equal(generation.execution.expected_model_processes, 1);

  const diagnosisOptions = { ...generationOptions, goal: "diagnosis", scenario: "complete", allScenarios: false };
  const diagnosis = buildPlan(diagnosisOptions, { platform: { status: "SUPPORTED", topology: "test", sealed: false }, claudeIdentity: identity });
  assert.equal(diagnosis.admission.status, "BLOCKED");
  assert.equal(diagnosis.execution.token_cap, 900_000);
  assert.equal(diagnosis.execution.usd_cap, 7);
  assert.ok(diagnosis.admission.blockers.some((item) => item.code === "LAN_GENERATION_CACHE_REQUIRED"));
});
