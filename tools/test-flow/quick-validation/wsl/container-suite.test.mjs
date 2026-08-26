import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  WSL_CONTAINER_SUITE_SCENARIOS,
  aggregateContainerSuite,
  buildContainerSuitePlan,
  materializeContainerSuite,
} from "./container-suite.mjs";

const IMAGE_SEAL = Object.freeze({
  schema_version: 1,
  image_id: `sha256:${"a".repeat(64)}`,
  platform: "linux/amd64",
  profile: "ubuntu22.04-central-v1",
  status: "PASS",
});

function providerPlan(provider, admission = { status: "READY", blockers: [] }) {
  const codex = provider === "codex-luna";
  return {
    schema_version: 1,
    framework: codex ? "macos-codex-luna-fast-e2e" : "macos-claude-deepseek-quick-validation",
    goal: codex ? "e2e" : "dev.macos-claude-deepseek-e2e",
    mode: "e2e-suite",
    scenario: null,
    scenarios: [...WSL_CONTAINER_SUITE_SCENARIOS],
    execution: codex
      ? { entry: "tools/test-flow/quick-validation/codex-luna/run.mjs", expected_model_calls: 44, token_cap: 18_000_000, equivalent_usd_cap: 27, wall_timeout_seconds: 16_200 }
      : { entry: "tools/test-flow/quick-validation/claude-deepseek/run.mjs", expected_model_processes: 44, token_cap: 18_000_000, usd_cap: 36, stage_wall_seconds: 16_200 },
    admission,
    plan_sha256: "b".repeat(64),
  };
}

function createSuite(provider, admission = { status: "READY", blockers: [] }) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `wsl-${provider}-containers-`));
  const plan = buildContainerSuitePlan({ provider, providerPlan: providerPlan(provider, admission), imageSeal: IMAGE_SEAL });
  fs.writeFileSync(path.join(root, "plan.json"), `${JSON.stringify(plan)}\n`);
  fs.writeFileSync(path.join(root, "run-id.txt"), `fixture-${provider}-suite-20260826T010203Z\n`);
  fs.writeFileSync(path.join(root, "started-at.txt"), "2026-08-26T00:00:00.000Z\n");
  return { root, plan };
}

function writeChild({ root, provider, scenario, status = "PASS", failure = null, exitCode = status === "PASS" ? 0 : 1, actualOverride = undefined }) {
  const scenarioRoot = path.join(root, "scenarios", scenario);
  const runtimeRoot = path.join(root, "evidence", "container-runtime", scenario);
  fs.mkdirSync(scenarioRoot, { recursive: true });
  fs.mkdirSync(runtimeRoot, { recursive: true });
  const countField = provider === "codex-luna" ? "model_calls" : "model_processes";
  const actual = scenario === "insufficient-evidence" ? 4 : 5;
  fs.writeFileSync(path.join(scenarioRoot, "verdict.json"), JSON.stringify({
    schema_version: 1,
    run_id: `fixture-${scenario}`,
    scenario,
    status,
    [countField]: { expected: actual, actual: actualOverride === undefined ? actual : actualOverride, retry_count: 0 },
    usage: { input_tokens: actual * 10, equivalent_usd: actual / 100 },
    failure,
  }));
  fs.writeFileSync(path.join(runtimeRoot, "container-name.txt"), `fixture-${scenario}\n`);
  fs.writeFileSync(path.join(runtimeRoot, "exit-code.txt"), `${exitCode}\n`);
  fs.writeFileSync(path.join(runtimeRoot, "stdout.txt"), "fixture stdout\n");
  fs.writeFileSync(path.join(runtimeRoot, "stderr.txt"), "");
}

test("nine-container plan freezes one scenario per container and a parallel wall limit", () => {
  for (const provider of ["codex-luna", "claude-deepseek"]) {
    const plan = buildContainerSuitePlan({ provider, providerPlan: providerPlan(provider), imageSeal: IMAGE_SEAL });
    assert.equal(plan.mode, "e2e-container-suite");
    assert.deepEqual(plan.scenarios, WSL_CONTAINER_SUITE_SCENARIOS);
    assert.equal(plan.execution.topology, "NINE_ISOLATED_CONTAINERS");
    assert.equal(plan.execution.container_count, 9);
    assert.equal(plan.execution.max_concurrency, 9);
    assert.equal(plan.execution.scenarios_per_container, 1);
    assert.equal(plan.execution.expected_model_activity, 44);
    assert.equal(plan.execution.per_container_wall_seconds, 1_800);
    assert.equal(plan.execution.suite_wall_seconds, 1_800);
    assert.equal(plan.execution.engineering_failure_policy, "FINISH_ALL_STARTED_CONTAINERS");
    assert.match(plan.plan_sha256, /^[a-f0-9]{64}$/u);
  }
});

test("root utility materializes one root-owned child run per scenario without copying evidence", () => {
  const { root } = createSuite("codex-luna");
  for (const scenario of WSL_CONTAINER_SUITE_SCENARIOS) {
    const child = path.join(root, ".children", scenario, `run-${scenario}`);
    fs.mkdirSync(child, { recursive: true });
    fs.writeFileSync(path.join(child, "verdict.json"), "{}\n");
  }
  const receipt = materializeContainerSuite({ suiteRoot: root, write: false });
  assert.equal(receipt.status, "PASS");
  for (const scenario of WSL_CONTAINER_SUITE_SCENARIOS) {
    assert.equal(fs.existsSync(path.join(root, "scenarios", scenario, "verdict.json")), true);
    assert.deepEqual(fs.readdirSync(path.join(root, ".children", scenario)), []);
  }
});

test("container aggregate preserves fixed order and recomputes calls and usage from nine child verdicts", () => {
  for (const provider of ["codex-luna", "claude-deepseek"]) {
    const { root } = createSuite(provider);
    for (const scenario of [...WSL_CONTAINER_SUITE_SCENARIOS].reverse()) writeChild({ root, provider, scenario });
    const verdict = aggregateContainerSuite({ provider, suiteRoot: root, write: false });
    assert.equal(verdict.status, "PASS");
    assert.equal(verdict.run_id, `fixture-${provider}-suite-20260826T010203Z`);
    assert.deepEqual(verdict.scenarios.map((item) => item.scenario_id), WSL_CONTAINER_SUITE_SCENARIOS);
    assert.equal(verdict.summary.completed, 9);
    assert.equal(verdict.summary.attempted, 9);
    assert.equal(verdict.summary.passed, 9);
    assert.equal(verdict.summary.not_run, 0);
    assert.equal(verdict[provider === "codex-luna" ? "model_calls" : "model_processes"].actual, 44);
    assert.equal(verdict.usage.input_tokens, 440);
    assert.equal(new Set(verdict.container_receipts.map((item) => item.container_name)).size, 9);
  }
});

test("contract failure does not hide sibling verdicts while engineering loss makes the suite ERROR", () => {
  const contractSuite = createSuite("codex-luna");
  for (const scenario of WSL_CONTAINER_SUITE_SCENARIOS) writeChild({
    root: contractSuite.root,
    provider: "codex-luna",
    scenario,
    status: scenario === WSL_CONTAINER_SUITE_SCENARIOS[2] ? "FAIL" : "PASS",
    failure: scenario === WSL_CONTAINER_SUITE_SCENARIOS[2] ? { code: "MACOS_CODEX_LUNA_EXPECTED_TERM_MISSING" } : null,
    actualOverride: scenario === WSL_CONTAINER_SUITE_SCENARIOS[2] ? null : undefined,
  });
  const contractVerdict = aggregateContainerSuite({ provider: "codex-luna", suiteRoot: contractSuite.root, write: false });
  assert.equal(contractVerdict.status, "FAIL");
  assert.equal(contractVerdict.summary.completed, 9);
  assert.equal(contractVerdict.engineering_failures.length, 0);
  assert.equal(contractVerdict.scenarios[2].model_calls.actual, null);

  const engineeringSuite = createSuite("codex-luna");
  for (const scenario of WSL_CONTAINER_SUITE_SCENARIOS.slice(1)) writeChild({ root: engineeringSuite.root, provider: "codex-luna", scenario });
  const runtimeRoot = path.join(engineeringSuite.root, "evidence", "container-runtime", WSL_CONTAINER_SUITE_SCENARIOS[0]);
  fs.mkdirSync(runtimeRoot, { recursive: true });
  fs.writeFileSync(path.join(runtimeRoot, "exit-code.txt"), "137\n");
  const engineeringVerdict = aggregateContainerSuite({ provider: "codex-luna", suiteRoot: engineeringSuite.root, write: false });
  assert.equal(engineeringVerdict.status, "ERROR");
  assert.equal(engineeringVerdict.summary.attempted, 9);
  assert.equal(engineeringVerdict.summary.completed, 8);
  assert.equal(engineeringVerdict.scenarios[0].status, "ERROR");
  assert.equal(engineeringVerdict.stop_reason.completion_policy, "FINISH_ALL_STARTED_CONTAINERS");
});

test("blocked provider plan starts no scenario and yields nine NOT_RUN entries", () => {
  const { root } = createSuite("claude-deepseek", { status: "BLOCKED", blockers: [{ code: "CACHE_MISSING" }] });
  const verdict = aggregateContainerSuite({ provider: "claude-deepseek", suiteRoot: root, write: false });
  assert.equal(verdict.status, "BLOCKED");
  assert.equal(verdict.summary.attempted, 0);
  assert.equal(verdict.summary.not_run, 9);
  assert.equal(verdict.model_processes.actual, 0);
});
