import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  E2E_GOAL,
  FAST_E2E_GOAL,
  METHODS_GOAL,
  REQUIRED_EVIDENCE,
  buildPlan,
  defaults,
  deterministicGateRoot,
  executeFastSuite,
  materializeDeterministicGateEvidence,
  parseArguments,
  safeFailure,
  sealGate,
} from "../run.mjs";
import { FAST_E2E_SCENARIOS } from "../../fast-e2e-scenarios.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("standalone entry separates the historical Fast E2E matrix from the fixed model-cert scenario", () => {
  assert.equal(parseArguments(["--goal", METHODS_GOAL]).goal, METHODS_GOAL);
  assert.equal(parseArguments(["--goal", E2E_GOAL, "--scenario", "multiple-rpc-timeouts"]).goal, E2E_GOAL);
  assert.equal(parseArguments(["--goal", FAST_E2E_GOAL, "--scenario", "api-execution-overrun"]).goal, FAST_E2E_GOAL);
  assert.equal(parseArguments(["--goal", FAST_E2E_GOAL, "--all-scenarios"])["all-scenarios"], true);
  assert.throws(() => parseArguments(["--goal", E2E_GOAL, "--all-scenarios"]), (error) => error.code === "CLAUDE_DEEPSEEK_MODEL_CERT_SUITE_FORBIDDEN");
  assert.throws(() => parseArguments(["--goal", E2E_GOAL, "--scenario", "api-execution-overrun"]), (error) => error.code === "CLAUDE_DEEPSEEK_SCENARIO_INVALID");
  assert.throws(() => parseArguments(["--goal", FAST_E2E_GOAL, "--scenario", "invented-scenario"]), (error) => error.code === "CLAUDE_DEEPSEEK_SCENARIO_INVALID");
  assert.throws(() => parseArguments(["--goal", FAST_E2E_GOAL, "--all-scenarios", "--scenario", "api-execution-overrun"]), (error) => error.code === "CLAUDE_DEEPSEEK_SCENARIO_SELECTION_CONFLICT");
  assert.throws(() => parseArguments(["--goal", "release.full"]), (error) => error.code === "CLAUDE_DEEPSEEK_GOAL_INVALID");
  assert.throws(() => parseArguments(["--goal", E2E_GOAL, "--client", "linux"]), (error) => error.code === "CLAUDE_DEEPSEEK_CLIENT_INVALID");
  assert.throws(() => parseArguments(["--goal", E2E_GOAL, "--docker-context", "colima"]), (error) => error.code === "CLAUDE_DEEPSEEK_ARGUMENT_UNKNOWN");
});

test("Fast failure receipts retain Runtime stderr and oracle comparison details", () => {
  const error = new Error("runtime failed");
  error.code = "CLAUDE_DEEPSEEK_PRODUCTION_RUNTIME_FAILED";
  error.reason_code = "SERVER_INVARIANT_VIOLATION";
  error.diagnostic_id = "diag-example";
  error.details = { stderr: "trace", expected: ["601"], actual: ["999"] };
  assert.deepEqual(safeFailure(error), {
    code: error.code,
    message: error.message,
    reason_code: error.reason_code,
    diagnostic_id: error.diagnostic_id,
    details: error.details,
  });
});

test("Fast E2E plan freezes nine historical scenarios with a 16/32 model-process boundary", () => {
  const plan = buildPlan(defaults(parseArguments(["--goal", FAST_E2E_GOAL, "--all-scenarios", "--plan-only"])));
  assert.equal(plan.mode, "fast-e2e-suite");
  assert.deepEqual(plan.scenarios, FAST_E2E_SCENARIOS);
  assert.equal(plan.execution.expected_model_processes, 16);
  assert.equal(plan.execution.model_process_hard_cap, 32);
  assert.equal(plan.execution.token_cap, 16_000_000);
  assert.equal(plan.execution.usd_cap, 32);
  assert.equal(plan.execution.source_snapshot, false);
  assert.equal(plan.inputs.source_snapshot_digest, null);
  assert.equal(plan.inputs.core_verdict, null);
  const insufficient = plan.execution.per_scenario.find((item) => item.scenario_id === "insufficient-evidence");
  assert.equal(insufficient.expected_model_processes, 0);
  assert.equal(insufficient.model_process_hard_cap, 0);
  assert.equal(insufficient.token_cap, 0);
  assert.equal(insufficient.usd_cap, 0);
  assert.equal(plan.execution.per_scenario.filter((item) => item.expected_model_processes === 2).length, 8);
  assert.equal(plan.admission.blockers.some((item) => item.code === "CLAUDE_DEEPSEEK_SOURCE_SNAPSHOT_REQUIRED"), false);
  assert.equal(plan.admission.blockers.some((item) => item.code === "CLAUDE_DEEPSEEK_CORE_VERDICT_REQUIRED"), false);
  assert.equal(REQUIRED_EVIDENCE[FAST_E2E_GOAL].includes("model-cert.json"), false);
  assert.equal(REQUIRED_EVIDENCE[FAST_E2E_GOAL].includes("fast-e2e-oracle.json"), true);
});

test("Fast E2E suite continues after an oracle failure and stops after an engineering failure", async (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-fast-suite-"));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const plan = {
    schema_version: 1,
    framework: "macos-claude-deepseek-quick-validation",
    framework_version: 1,
    goal: FAST_E2E_GOAL,
    mode: "fast-e2e-suite",
    scenario: null,
    scenarios: [...FAST_E2E_SCENARIOS],
    execution: {
      expected_model_processes: 16,
      model_process_hard_cap: 32,
      per_scenario: [],
    },
    inputs: {},
    evidence: [],
    admission: { status: "READY", blockers: [] },
    plan_sha256: "a".repeat(64),
  };
  const options = { runsRoot: root, allowRealModel: true };
  const child = (failureCode) => async (_options, childPlan, { runRoot }) => {
    fs.mkdirSync(runRoot, { recursive: true });
    const failed = childPlan.scenario === FAST_E2E_SCENARIOS[0];
    const verdict = {
      status: failed ? "FAIL" : "PASS",
      failure: failed ? { code: failureCode } : null,
      model_processes: { expected: 2, actual: 2, retry_count: 0 },
      usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2, cost_usd: 0 },
    };
    fs.writeFileSync(path.join(runRoot, "verdict.json"), JSON.stringify(verdict));
    return { verdict, runRoot, exitCode: failed ? 1 : 0 };
  };
  const contract = await executeFastSuite(options, plan, {
    executeOneImpl: child("CLAUDE_DEEPSEEK_PUBLIC_STATUS_MISMATCH"),
    runDeterministicGatesImpl() {},
  });
  assert.equal(contract.verdict.status, "FAIL");
  assert.equal(contract.verdict.summary.completed, 9);
  assert.equal(contract.verdict.summary.not_run, 0);

  const engineeringRoot = fs.mkdtempSync(path.join(os.tmpdir(), "claude-fast-engineering-"));
  const engineering = await executeFastSuite({ ...options, runsRoot: engineeringRoot }, plan, {
    executeOneImpl: child("CLAUDE_DEEPSEEK_FAST_E2E_RUNTIME_FAILED"),
    runDeterministicGatesImpl() {},
  });
  fs.rmSync(engineeringRoot, { recursive: true, force: true });
  assert.equal(engineering.verdict.status, "ERROR");
  assert.equal(engineering.verdict.summary.completed, 1);
  assert.equal(engineering.verdict.summary.not_run, 8);
});

test("Claude model-cert plan freezes normal two calls, one repair per role, and Core bindings", () => {
  const options = defaults(parseArguments(["--goal", E2E_GOAL, "--scenario", "multiple-rpc-timeouts", "--plan-only"]));
  const plan = buildPlan(options);
  assert.equal(plan.mode, "model-cert");
  assert.deepEqual(plan.scenarios, ["multiple-rpc-timeouts"]);
  assert.equal(plan.execution.expected_model_processes, 2);
  assert.equal(plan.execution.model_process_hard_cap, 4);
  assert.equal(plan.execution.stage_wall_seconds, 2700);
  assert.equal(plan.execution.per_process_wall_seconds, 600);
  assert.equal(plan.execution.per_scenario[0].model_process_hard_cap, 4);
  assert.equal(plan.execution.source_snapshot, true);
  assert.match(plan.inputs.provider_runtime.tree_sha256, /^[0-9a-f]{64}$/u);
  assert.equal(Object.hasOwn(plan.inputs.registration_cache, "path"), true);
  assert.equal(Object.hasOwn(plan.inputs.registration_cache, "registration_root"), true);
  assert.ok(plan.admission.blockers.some((item) => item.code === "CLAUDE_DEEPSEEK_SOURCE_SNAPSHOT_REQUIRED"));
  assert.ok(plan.admission.blockers.some((item) => item.code === "CLAUDE_DEEPSEEK_CORE_VERDICT_REQUIRED"));
  assert.equal(plan.admission.blockers.some((item) => item.code === "EVIDENCE_V2_REAL_DIAGNOSIS_ADAPTER_UNMIGRATED"), false);
});

test("P1 plan preserves a deferred shared registration root instead of falling back to cache", () => {
  const registrationRoot = path.join(os.tmpdir(), "not-yet-generated-evidence-v2-registration");
  const options = defaults(parseArguments(["--goal", E2E_GOAL, "--registration-root", registrationRoot, "--plan-only"]));
  const plan = buildPlan(options);
  assert.equal(plan.inputs.production_registration.source, "explicit-deferred");
  assert.equal(plan.inputs.production_registration.registration_root, path.resolve(registrationRoot));
  assert.equal(plan.inputs.production_registration.tree_sha256, null);
  assert.ok(plan.admission.blockers.some((item) => item.code === "CLAUDE_DEEPSEEK_REGISTRATION_ROOT_MISSING"));
  assert.equal(plan.admission.blockers.some((item) => item.code === "CLAUDE_DEEPSEEK_REGISTRATION_CACHE_REQUIRED"), false);
});

test("Claude Methods generation and cache verification do not inherit the E2E migration blocker", () => {
  const plan = buildPlan(defaults(parseArguments(["--goal", METHODS_GOAL, "--plan-only"])));
  assert.equal(plan.admission.blockers.some((item) => item.code === "EVIDENCE_V2_REAL_DIAGNOSIS_ADAPTER_UNMIGRATED"), false);
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
  assert.ok(REQUIRED_EVIDENCE[E2E_GOAL].includes("model-cert.json"));
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

test("central Claude generation keeps its meta Skill while model cert consumes the shared registration", () => {
  const actions = fs.readFileSync(path.join(ROOT, "..", "..", "lib", "actions.mjs"), "utf8");
  const actionStart = actions.indexOf("async function runMacosClaudeDeepseekGate");
  const actionEnd = actions.indexOf("\nasync function crossJob", actionStart);
  const claudeAction = actions.slice(actionStart, actionEnd);
  assert.match(claudeAction, /\.claude", "skills", "wiki-to-logparse-diagnosis-skill/);
  assert.match(claudeAction, /"--module", "rpc"/);
  assert.equal(claudeAction.includes("--registration-template"), false);
  const planner = fs.readFileSync(path.join(ROOT, "..", "..", "lib", "planner.mjs"), "utf8");
  const cacheStart = planner.indexOf("const claudeMethodsSelected");
  const cacheEnd = planner.indexOf("\n  const stageIdentities", cacheStart);
  const claudeCache = planner.slice(cacheStart, cacheEnd);
  assert.match(claudeCache, /wiki-to-logparse-diagnosis-skill/);
  assert.match(claudeCache, /registration_tree_sha256/);
  assert.match(claudeCache, /runtime_ref/);
  assert.equal(claudeCache.includes("registrationTemplate"), false);
  assert.match(claudeAction, /evidenceV2ProviderRuntimeInputs\(context\)/);
  assert.match(claudeAction, /"--registration-root", providerInputs\.registrationRoot/);
  const stages = JSON.parse(fs.readFileSync(path.join(ROOT, "..", "..", "config", "stages.v2.json"), "utf8"));
  const modelCertStage = stages.stages.find((stage) => stage.id === "real.macos-claude-deepseek-e2e");
  assert.ok(modelCertStage.depends_on.includes("real.skill-generation"));
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
  assert.equal(sealGate({ goal: METHODS_GOAL, mode: "generation", evidenceRoot: other, expectedCalls: 1 }).status, "FAIL");

  const missingFinalCert = fs.mkdtempSync(path.join(os.tmpdir(), "claude-deepseek-gate-no-final-cert-"));
  for (const name of REQUIRED_EVIDENCE[E2E_GOAL].filter((item) => item !== "model-cert.json")) {
    const content = name === "adapter-receipt.json"
      ? '{"status":"PASS"}\n'
      : name === "model-invocations.json"
        ? '{"invocations":[{},{}]}\n'
        : name === "model-usage.json"
          ? '{"aggregate":{}}\n'
          : "{}\n";
    fs.writeFileSync(path.join(missingFinalCert, name), content);
  }
  const missingReceipt = sealGate({ goal: E2E_GOAL, mode: "model-cert", evidenceRoot: missingFinalCert, expectedCalls: 2 });
  assert.equal(missingReceipt.status, "FAIL");
  assert.deepEqual(missingReceipt.missing_evidence, ["model-cert.json"]);
});
