import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  E2E_GOAL,
  METHODS_GOAL,
  REQUIRED_EVIDENCE,
  buildPlan,
  defaults,
  deterministicGateRoot,
  executeSuite,
  materializeDeterministicGateEvidence,
  parseArguments,
  sealGate,
} from "../run.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("standalone entry exposes only the two Claude/DeepSeek Fast E2E goals and the nine-scenario suite", () => {
  assert.equal(parseArguments(["--goal", METHODS_GOAL]).goal, METHODS_GOAL);
  assert.equal(parseArguments(["--goal", E2E_GOAL, "--scenario", "api-execution-overrun"]).goal, E2E_GOAL);
  assert.equal(parseArguments(["--goal", E2E_GOAL, "--all-scenarios"])["all-scenarios"], true);
  assert.throws(() => parseArguments(["--goal", E2E_GOAL, "--all-scenarios", "--scenario", "api-execution-overrun"]), (error) => error.code === "CLAUDE_DEEPSEEK_SCENARIO_SELECTION_CONFLICT");
  assert.throws(() => parseArguments(["--goal", "release.full"]), (error) => error.code === "CLAUDE_DEEPSEEK_GOAL_INVALID");
  assert.throws(() => parseArguments(["--goal", E2E_GOAL, "--client", "linux"]), (error) => error.code === "CLAUDE_DEEPSEEK_CLIENT_INVALID");
  assert.throws(() => parseArguments(["--goal", E2E_GOAL, "--docker-context", "colima"]), (error) => error.code === "CLAUDE_DEEPSEEK_ARGUMENT_UNKNOWN");
});

test("Claude suite plan freezes nine scenarios, 44 processes, and aggregate limits before admission", () => {
  const options = defaults(parseArguments(["--goal", E2E_GOAL, "--all-scenarios", "--plan-only"]));
  const plan = buildPlan(options);
  assert.equal(plan.mode, "e2e-suite");
  assert.equal(plan.scenarios.length, 9);
  assert.equal(plan.execution.expected_model_processes, 44);
  assert.equal(plan.execution.token_cap, 18_000_000);
  assert.equal(plan.execution.usd_cap, 36);
  assert.equal(plan.execution.stage_wall_seconds, 16_200);
  assert.equal(plan.execution.per_scenario.find((item) => item.scenario_id === "insufficient-evidence").expected_model_processes, 4);
});

test("blocked Claude suite writes one aggregate verdict and nine NOT_RUN results without a model", async () => {
  const runsRoot = fs.mkdtempSync(path.join(os.tmpdir(), "claude-suite-blocked-"));
  const options = { ...defaults(parseArguments(["--goal", E2E_GOAL, "--all-scenarios"])), runsRoot };
  const result = await executeSuite(options, buildPlan(options));
  assert.equal(result.verdict.status, "BLOCKED");
  assert.equal(result.verdict.model_processes.actual, 0);
  assert.equal(result.verdict.scenarios.filter((item) => item.status === "NOT_RUN").length, 9);
});

function fakeClaudeExecutor({ failureAt = null, failureCode = null, seen }) {
  return async (options, childPlan, { runRoot }) => {
    seen.push({ scenario: options.scenario, runRoot, usageRoot: path.join(runRoot, "usage"), evidenceRoot: path.join(runRoot, "evidence") });
    fs.mkdirSync(runRoot, { recursive: true });
    const failed = options.scenario === failureAt;
    const actual = childPlan.execution.expected_model_processes;
    const verdict = {
      status: failed ? "FAIL" : "PASS",
      model_processes: { expected: actual, actual, retry_count: 0 },
      usage: { input_tokens: actual * 10, cost_usd: actual / 100 },
      failure: failed ? { code: failureCode, message: "closed fixture failure" } : null,
      failure_domain: null,
    };
    fs.writeFileSync(path.join(runRoot, "verdict.json"), JSON.stringify(verdict));
    return { verdict, runRoot, exitCode: failed ? 1 : 0 };
  };
}

function claudePreflightFixture(_goal, evidenceRoot) {
  fs.writeFileSync(path.join(evidenceRoot, "quick-claude-e2e-contracts.tap"), "fixture preflight\n");
}

test("Claude suite continues after a contract failure and aggregates all nine child verdicts", async () => {
  const runsRoot = fs.mkdtempSync(path.join(os.tmpdir(), "claude-suite-contract-"));
  const options = { ...defaults(parseArguments(["--goal", E2E_GOAL, "--all-scenarios"])), runsRoot, allowRealModel: true };
  const plan = { ...buildPlan(options), admission: { status: "READY", blockers: [] } };
  const seen = [];
  const result = await executeSuite(options, plan, {
    executeOneImpl: fakeClaudeExecutor({ failureAt: plan.scenarios[0], failureCode: "MACOS_CODEX_LUNA_EXPECTED_TERM_MISSING", seen }),
    runDeterministicGatesImpl: claudePreflightFixture,
  });
  assert.equal(result.verdict.status, "FAIL");
  assert.equal(result.verdict.stop_reason, null);
  assert.deepEqual(seen.map((item) => item.scenario), plan.scenarios);
  assert.equal(new Set(seen.map((item) => item.runRoot)).size, 9);
  assert.equal(result.verdict.scenarios[0].failure_domain, "CONTRACT");
  assert.equal(result.verdict.model_processes.actual, result.verdict.scenarios.reduce((sum, item) => sum + item.model_processes.actual, 0));
  assert.equal(result.verdict.usage.input_tokens, result.verdict.scenarios.reduce((sum, item) => sum + item.usage.input_tokens, 0));
  assert.equal(result.verdict.summary.completed, 9);
  assert.equal(result.verdict.summary.not_run, 0);
});

test("Claude suite stops immediately after an engineering failure and marks the remainder NOT_RUN", async () => {
  const runsRoot = fs.mkdtempSync(path.join(os.tmpdir(), "claude-suite-engineering-"));
  const options = { ...defaults(parseArguments(["--goal", E2E_GOAL, "--all-scenarios"])), runsRoot, allowRealModel: true };
  const plan = { ...buildPlan(options), admission: { status: "READY", blockers: [] } };
  const seen = [];
  const passExecutor = fakeClaudeExecutor({ seen });
  const result = await executeSuite(options, plan, {
    async executeOneImpl(childOptions, childPlan, executionOptions) {
      if (childOptions.scenario === plan.scenarios[1]) {
        seen.push({ scenario: childOptions.scenario, runRoot: executionOptions.runRoot });
        throw Object.assign(new Error("closed fixture runner failure"), { code: "CLAUDE_DEEPSEEK_E2E_RUNNER_FAILED" });
      }
      return passExecutor(childOptions, childPlan, executionOptions);
    },
    runDeterministicGatesImpl: claudePreflightFixture,
  });
  assert.equal(result.verdict.status, "ERROR");
  assert.deepEqual(seen.map((item) => item.scenario), plan.scenarios.slice(0, 2));
  assert.equal(result.verdict.summary.completed, 1);
  assert.equal(result.verdict.summary.attempted, 2);
  assert.equal(result.verdict.summary.not_run, 7);
  assert.equal(result.verdict.scenarios[1].status, "ERROR");
  assert.equal(result.verdict.scenarios[2].status, "NOT_RUN");
  assert.equal(result.verdict.stop_reason.domain, "ENGINEERING");
  assert.equal(result.verdict.stop_reason.failure.scenario_id, plan.scenarios[1]);
  assert.equal(fs.existsSync(path.join(result.runRoot, "verdict.json")), true);
});

test("standalone entry does not import old CrossJob, Docker, browser, restart, or old Test Flow finalization", () => {
  const source = fs.readFileSync(path.join(ROOT, "run.mjs"), "utf8");
  for (const forbidden of ["cross-job-core.mjs", "lib/engine.mjs", "source-snapshot.mjs", "Dockerfile", "browser.mjs", "release.full"]) assert.equal(source.includes(forbidden), false, forbidden);
  assert.match(source, /old_cross_job: false/);
  assert.match(source, /old_test_flow_orchestrator: false/);
  assert.match(source, /automatic_model_retry: false/);
  assert.match(source, /docker: false/);
  assert.match(source, /browser: false/);
  assert.match(source, /restart: false/);
});

test("Methods contract stage includes migrated Codex Gate before Claude Bootstrap", () => {
  const source = fs.readFileSync(path.join(ROOT, "run.mjs"), "utf8");
  assert.match(source, /quick\.codex-luna\.contracts/);
  assert.match(source, /quick-codex-luna-contracts\.tap/);
  assert.match(source, /runDeterministicGates\(plan\.goal, deterministicRoot\)/);
  assert.ok(REQUIRED_EVIDENCE[METHODS_GOAL].includes("quick-codex-luna-contracts.tap"));
  assert.ok(REQUIRED_EVIDENCE[E2E_GOAL].includes("quick-claude-e2e-contracts.tap"));
});

test("provider deterministic Gates stay outside empty Methods and E2E runner roots until materialization", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-methods-gates-"));
  const scratchRunRoot = path.join(root, "scratch");
  const evidenceRoot = path.join(root, "evidence");
  fs.mkdirSync(scratchRunRoot);
  fs.mkdirSync(evidenceRoot);
  const stagingRoot = deterministicGateRoot({ goal: METHODS_GOAL, evidenceRoot, scratchRunRoot });
  fs.mkdirSync(stagingRoot);
  fs.writeFileSync(path.join(stagingRoot, "quick-codex-luna-contracts.tap"), "codex pass\n");
  fs.writeFileSync(path.join(stagingRoot, "quick-claude-methods-contracts.tap"), "claude pass\n");
  assert.deepEqual(fs.readdirSync(evidenceRoot), []);
  assert.deepEqual(materializeDeterministicGateEvidence({ stagingRoot, evidenceRoot }), {
    moved: ["quick-claude-methods-contracts.tap", "quick-codex-luna-contracts.tap"],
    status: "PASS",
  });
  assert.deepEqual(fs.readdirSync(evidenceRoot).sort(), ["quick-claude-methods-contracts.tap", "quick-codex-luna-contracts.tap"]);
  assert.equal(fs.readFileSync(path.join(evidenceRoot, "quick-codex-luna-contracts.tap"), "utf8"), "codex pass\n");
  assert.equal(fs.readFileSync(path.join(evidenceRoot, "quick-claude-methods-contracts.tap"), "utf8"), "claude pass\n");
  assert.equal(fs.readdirSync(evidenceRoot).some((name) => name.endsWith(".tmp")), false);
  assert.equal(fs.existsSync(stagingRoot), false);
  assert.equal(deterministicGateRoot({ goal: E2E_GOAL, evidenceRoot, scratchRunRoot }), path.join(path.resolve(scratchRunRoot), "deterministic-gates"));
});

test("central engine marks only Claude Quick deterministic contract Gates as zero-model usage complete", () => {
  const source = fs.readFileSync(path.join(ROOT, "..", "..", "lib", "engine.mjs"), "utf8");
  assert.match(source, /const claudeQuickContractGate = gate\.kind === "node-test"/);
  assert.match(source, /if \(claudeQuickContractGate\) \{\s*actionResult = \{ \.\.\.actionResult, usage_complete: true, invocations: \[\] \};/s);
  assert.match(source, /\["real\.macos-claude-deepseek-methods", "real\.macos-claude-deepseek-e2e"\]\.includes\(stage\.id\)/);
});

test("light Gate rejects missing evidence and wrong model-process cardinality", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-deepseek-gate-"));
  for (const name of REQUIRED_EVIDENCE[METHODS_GOAL]) {
    if (name === "model-invocations.json") fs.writeFileSync(path.join(root, name), '{"invocations":[]}\n');
    else if (name === "model-usage.json") fs.writeFileSync(path.join(root, name), '{"aggregate":{}}\n');
    else if (name === "adapter-receipt.json") fs.writeFileSync(path.join(root, name), '{"status":"PASS"}\n');
    else fs.writeFileSync(path.join(root, name), "{}\n");
  }
  assert.equal(sealGate({ goal: METHODS_GOAL, mode: "cache-verification", evidenceRoot: root, expectedCalls: 0 }).status, "PASS");
  const other = fs.mkdtempSync(path.join(os.tmpdir(), "claude-deepseek-gate-other-"));
  for (const name of REQUIRED_EVIDENCE[METHODS_GOAL]) fs.writeFileSync(path.join(other, name), name === "adapter-receipt.json" ? '{"status":"PASS"}\n' : name === "model-invocations.json" ? '{"invocations":[]}\n' : "{}\n");
  assert.equal(sealGate({ goal: METHODS_GOAL, mode: "bootstrap", evidenceRoot: other, expectedCalls: 1 }).status, "FAIL");
});
