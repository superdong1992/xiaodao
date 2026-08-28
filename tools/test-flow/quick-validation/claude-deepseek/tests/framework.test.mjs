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
  materializeDeterministicGateEvidence,
  parseArguments,
  sealGate,
} from "../run.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("standalone entry keeps Methods generation and exposes one Evidence V2 model-cert scenario", () => {
  assert.equal(parseArguments(["--goal", METHODS_GOAL]).goal, METHODS_GOAL);
  assert.equal(parseArguments(["--goal", E2E_GOAL, "--scenario", "multiple-rpc-timeouts"]).goal, E2E_GOAL);
  assert.throws(() => parseArguments(["--goal", E2E_GOAL, "--all-scenarios"]), (error) => error.code === "CLAUDE_DEEPSEEK_MODEL_CERT_SUITE_FORBIDDEN");
  assert.throws(() => parseArguments(["--goal", E2E_GOAL, "--scenario", "api-execution-overrun"]), (error) => error.code === "CLAUDE_DEEPSEEK_SCENARIO_INVALID");
  assert.throws(() => parseArguments(["--goal", "release.full"]), (error) => error.code === "CLAUDE_DEEPSEEK_GOAL_INVALID");
  assert.throws(() => parseArguments(["--goal", E2E_GOAL, "--client", "linux"]), (error) => error.code === "CLAUDE_DEEPSEEK_CLIENT_INVALID");
  assert.throws(() => parseArguments(["--goal", E2E_GOAL, "--docker-context", "colima"]), (error) => error.code === "CLAUDE_DEEPSEEK_ARGUMENT_UNKNOWN");
});

test("Claude model-cert plan freezes normal two calls, one repair per role, and Core bindings", () => {
  const options = defaults(parseArguments(["--goal", E2E_GOAL, "--scenario", "multiple-rpc-timeouts", "--plan-only"]));
  const plan = buildPlan(options);
  assert.equal(plan.mode, "model-cert");
  assert.deepEqual(plan.scenarios, ["multiple-rpc-timeouts"]);
  assert.equal(plan.execution.expected_model_processes, 2);
  assert.equal(plan.execution.model_process_hard_cap, 4);
  assert.equal(plan.execution.per_scenario[0].model_process_hard_cap, 4);
  assert.equal(plan.execution.source_snapshot, true);
  assert.match(plan.inputs.provider_runtime.tree_sha256, /^[0-9a-f]{64}$/u);
  assert.ok(plan.admission.blockers.some((item) => item.code === "CLAUDE_DEEPSEEK_SOURCE_SNAPSHOT_REQUIRED"));
  assert.ok(plan.admission.blockers.some((item) => item.code === "CLAUDE_DEEPSEEK_CORE_VERDICT_REQUIRED"));
  assert.equal(plan.admission.blockers.some((item) => item.code === "EVIDENCE_V2_REAL_DIAGNOSIS_ADAPTER_UNMIGRATED"), false);
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

test("central Claude adapter and planner use the new meta Skill and complete registration cache", () => {
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
  assert.match(planner, /registration_tree_digest: stage\.id === "real\.macos-claude-deepseek-e2e"/);
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
});
