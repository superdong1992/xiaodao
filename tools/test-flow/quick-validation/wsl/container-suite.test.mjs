import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  WSL_CONTAINER_SUITE_CALL_HARD_CAP,
  WSL_CONTAINER_SUITE_EXPECTED_CALLS,
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

function expectedForScenario(scenario) {
  return scenario === "insufficient-evidence" ? 0 : 2;
}

function hardCapForScenario(scenario) {
  return scenario === "insufficient-evidence" ? 0 : 4;
}

function providerPlan(provider, admission = { status: "READY", blockers: [] }) {
  const codex = provider === "codex-luna";
  const expectedField = codex ? "expected_model_calls" : "expected_model_processes";
  const hardCapField = codex ? "model_call_hard_cap" : "model_process_hard_cap";
  return {
    schema_version: 1,
    framework: codex
      ? "macos-codex-luna-fast-e2e"
      : "macos-claude-deepseek-quick-validation",
    goal: "fast-e2e",
    mode: "fast-e2e-suite",
    scenario: null,
    scenarios: [...WSL_CONTAINER_SUITE_SCENARIOS],
    execution: {
      entry: codex
        ? "tools/test-flow/quick-validation/codex-luna/run.mjs"
        : "tools/test-flow/quick-validation/claude-deepseek/run.mjs",
      [expectedField]: WSL_CONTAINER_SUITE_EXPECTED_CALLS,
      [hardCapField]: WSL_CONTAINER_SUITE_CALL_HARD_CAP,
      per_scenario: WSL_CONTAINER_SUITE_SCENARIOS.map((scenario) => ({
        scenario_id: scenario,
        [expectedField]: expectedForScenario(scenario),
        [hardCapField]: hardCapForScenario(scenario),
      })),
      ...(codex
        ? { wall_timeout_seconds: 16_200 }
        : { stage_wall_seconds: 16_200 }),
    },
    admission,
    plan_sha256: "b".repeat(64),
  };
}

function createSuite(provider, admission = { status: "READY", blockers: [] }) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), `wsl-${provider}-containers-`));
  const plan = buildContainerSuitePlan({
    provider,
    providerPlan: providerPlan(provider, admission),
    imageSeal: IMAGE_SEAL,
  });
  fs.writeFileSync(path.join(root, "plan.json"), `${JSON.stringify(plan)}\n`);
  fs.writeFileSync(
    path.join(root, "run-id.txt"),
    `fixture-${provider}-suite-20260829T010203Z\n`,
  );
  fs.writeFileSync(path.join(root, "started-at.txt"), "2026-08-29T00:00:00.000Z\n");
  return { root, plan };
}

function writeChild({
  root,
  provider,
  scenario,
  status = "PASS",
  failure = null,
  failureDomain = status === "PASS" ? null : "CONTRACT",
  exitCode = status === "PASS" ? 0 : status === "BLOCKED" ? 2 : 1,
  actualOverride = undefined,
}) {
  const scenarioRoot = path.join(root, "scenarios", scenario);
  const runtimeRoot = path.join(root, "evidence", "container-runtime", scenario);
  fs.mkdirSync(scenarioRoot, { recursive: true });
  fs.mkdirSync(runtimeRoot, { recursive: true });
  const countField = provider === "codex-luna" ? "model_calls" : "model_processes";
  const expected = expectedForScenario(scenario);
  fs.writeFileSync(path.join(scenarioRoot, "verdict.json"), JSON.stringify({
    schema_version: 1,
    run_id: `fixture-${scenario}`,
    goal: "fast-e2e",
    mode: "fast-e2e",
    scenario,
    source_snapshot: false,
    status,
    [countField]: {
      expected,
      actual: actualOverride === undefined ? expected : actualOverride,
      retry_count: 0,
    },
    usage: {
      input_tokens: expected * 10,
      equivalent_usd: expected / 100,
    },
    failure,
    failure_domain: failureDomain,
  }));
  fs.writeFileSync(path.join(runtimeRoot, "container-name.txt"), `fixture-${scenario}\n`);
  fs.writeFileSync(path.join(runtimeRoot, "exit-code.txt"), `${exitCode}\n`);
  fs.writeFileSync(path.join(runtimeRoot, "stdout.txt"), "fixture stdout\n");
  fs.writeFileSync(path.join(runtimeRoot, "stderr.txt"), "");
}

test("nine-container plan freezes the 0/2 call boundary and the 16/32 totals", () => {
  for (const provider of ["codex-luna", "claude-deepseek"]) {
    const plan = buildContainerSuitePlan({
      provider,
      providerPlan: providerPlan(provider),
      imageSeal: IMAGE_SEAL,
    });
    assert.equal(plan.goal, "fast-e2e");
    assert.equal(plan.mode, "fast-e2e-container-suite");
    assert.equal(plan.source_snapshot, false);
    assert.equal(plan.release_verdict, false);
    assert.deepEqual(plan.scenarios, WSL_CONTAINER_SUITE_SCENARIOS);
    assert.equal(plan.execution.topology, "NINE_ISOLATED_CONTAINERS");
    assert.equal(plan.execution.container_count, 9);
    assert.equal(plan.execution.max_concurrency, 9);
    assert.equal(plan.execution.scenarios_per_container, 1);
    assert.equal(plan.execution.expected_model_activity, 16);
    assert.equal(plan.execution.model_activity_hard_cap, 32);
    assert.deepEqual(
      plan.execution.per_scenario.map((item) => [
        item.scenario_id,
        item.expected_model_activity,
        item.model_activity_hard_cap,
      ]),
      WSL_CONTAINER_SUITE_SCENARIOS.map((scenario) => [
        scenario,
        expectedForScenario(scenario),
        hardCapForScenario(scenario),
      ]),
    );
    assert.equal(plan.execution.per_container_wall_seconds, 1_800);
    assert.equal(plan.execution.suite_wall_seconds, 1_800);
    assert.match(plan.plan_sha256, /^[a-f0-9]{64}$/u);
  }
});

test("suite plan rejects model calls in insufficient-evidence and stale 18/36 totals", () => {
  const calls = providerPlan("codex-luna");
  const insufficient = calls.execution.per_scenario.find(
    (item) => item.scenario_id === "insufficient-evidence",
  );
  insufficient.expected_model_calls = 2;
  insufficient.model_call_hard_cap = 4;
  assert.throws(
    () => buildContainerSuitePlan({
      provider: "codex-luna",
      providerPlan: calls,
      imageSeal: IMAGE_SEAL,
    }),
    { code: "WSL_CONTAINER_SUITE_PER_SCENARIO_PLAN_INVALID" },
  );

  const stale = providerPlan("claude-deepseek");
  stale.execution.expected_model_processes = 18;
  stale.execution.model_process_hard_cap = 36;
  assert.throws(
    () => buildContainerSuitePlan({
      provider: "claude-deepseek",
      providerPlan: stale,
      imageSeal: IMAGE_SEAL,
    }),
    { code: "WSL_CONTAINER_SUITE_MODEL_COUNT_INVALID" },
  );
});

test("root utility materializes one child run per scenario without copying evidence", () => {
  const { root } = createSuite("codex-luna");
  for (const scenario of WSL_CONTAINER_SUITE_SCENARIOS) {
    const child = path.join(root, ".children", scenario, `run-${scenario}`);
    fs.mkdirSync(child, { recursive: true });
    fs.writeFileSync(path.join(child, "verdict.json"), "{}\n");
  }
  const receipt = materializeContainerSuite({ suiteRoot: root, write: false });
  assert.equal(receipt.status, "PASS");
  for (const scenario of WSL_CONTAINER_SUITE_SCENARIOS) {
    assert.equal(
      fs.existsSync(path.join(root, "scenarios", scenario, "verdict.json")),
      true,
    );
    assert.deepEqual(fs.readdirSync(path.join(root, ".children", scenario)), []);
  }
});

test("aggregate preserves order and recomputes 16 calls from nine standalone verdicts", () => {
  for (const provider of ["codex-luna", "claude-deepseek"]) {
    const { root } = createSuite(provider);
    for (const scenario of [...WSL_CONTAINER_SUITE_SCENARIOS].reverse()) {
      writeChild({ root, provider, scenario });
    }
    const verdict = aggregateContainerSuite({ provider, suiteRoot: root, write: false });
    const count = verdict[provider === "codex-luna" ? "model_calls" : "model_processes"];
    assert.equal(verdict.status, "PASS");
    assert.equal(verdict.release_verdict, false);
    assert.deepEqual(
      verdict.scenarios.map((item) => item.scenario_id),
      WSL_CONTAINER_SUITE_SCENARIOS,
    );
    assert.deepEqual(count, {
      expected: 16,
      hard_cap: 32,
      actual: 16,
      actual_complete: true,
      retry_count: 0,
    });
    assert.equal(verdict.summary.completed, 9);
    assert.equal(verdict.summary.attempted, 9);
    assert.equal(verdict.summary.passed, 9);
    assert.equal(verdict.usage.input_tokens, 160);
    assert.equal(new Set(verdict.container_receipts.map((item) => item.container_name)).size, 9);
  }
});

test("aggregate rejects any model call in the insufficient-evidence child", () => {
  const { root } = createSuite("codex-luna");
  for (const scenario of WSL_CONTAINER_SUITE_SCENARIOS) {
    writeChild({
      root,
      provider: "codex-luna",
      scenario,
      actualOverride: scenario === "insufficient-evidence" ? 1 : undefined,
    });
  }
  const verdict = aggregateContainerSuite({
    provider: "codex-luna",
    suiteRoot: root,
    write: false,
  });
  assert.equal(verdict.status, "ERROR");
  assert.equal(verdict.summary.errored, 1);
  assert.equal(
    verdict.scenarios.find((item) => item.scenario_id === "insufficient-evidence")
      .failure.code,
    "WSL_CONTAINER_CHILD_RECEIPT_MISMATCH",
  );
});

test("contract failure preserves siblings while a missing child is an engineering error", () => {
  const contractSuite = createSuite("codex-luna");
  for (const scenario of WSL_CONTAINER_SUITE_SCENARIOS) {
    const failed = scenario === "deadloop-detected";
    writeChild({
      root: contractSuite.root,
      provider: "codex-luna",
      scenario,
      status: failed ? "FAIL" : "PASS",
      failure: failed ? { code: "MACOS_CODEX_LUNA_PUBLIC_STATUS_MISMATCH" } : null,
      actualOverride: failed ? null : undefined,
    });
  }
  const contractVerdict = aggregateContainerSuite({
    provider: "codex-luna",
    suiteRoot: contractSuite.root,
    write: false,
  });
  assert.equal(contractVerdict.status, "FAIL");
  assert.equal(contractVerdict.summary.completed, 9);
  assert.equal(contractVerdict.engineering_failures.length, 0);

  const engineeringSuite = createSuite("claude-deepseek");
  for (const scenario of WSL_CONTAINER_SUITE_SCENARIOS.slice(1)) {
    writeChild({ root: engineeringSuite.root, provider: "claude-deepseek", scenario });
  }
  const runtimeRoot = path.join(
    engineeringSuite.root,
    "evidence",
    "container-runtime",
    WSL_CONTAINER_SUITE_SCENARIOS[0],
  );
  fs.mkdirSync(runtimeRoot, { recursive: true });
  fs.writeFileSync(path.join(runtimeRoot, "exit-code.txt"), "137\n");
  const engineeringVerdict = aggregateContainerSuite({
    provider: "claude-deepseek",
    suiteRoot: engineeringSuite.root,
    write: false,
  });
  assert.equal(engineeringVerdict.status, "ERROR");
  assert.equal(engineeringVerdict.summary.attempted, 9);
  assert.equal(engineeringVerdict.summary.completed, 8);
  assert.equal(engineeringVerdict.scenarios[0].status, "ERROR");
});

test("blocked provider plan starts no scenario and yields nine NOT_RUN entries", () => {
  const { root } = createSuite("claude-deepseek", {
    status: "BLOCKED",
    blockers: [{ code: "CACHE_MISSING" }],
  });
  const verdict = aggregateContainerSuite({
    provider: "claude-deepseek",
    suiteRoot: root,
    write: false,
  });
  assert.equal(verdict.status, "BLOCKED");
  assert.equal(verdict.summary.attempted, 0);
  assert.equal(verdict.summary.not_run, 9);
  assert.equal(verdict.model_processes.actual, 0);
});
