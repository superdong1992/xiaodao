#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { canonicalJson, sha256Bytes, sha256File } from "../../lib/util.mjs";
import {
  CLAUDE_DEEPSEEK_E2E_TOKEN_LIMIT,
  CLAUDE_DEEPSEEK_E2E_USD_LIMIT,
  CLAUDE_DEEPSEEK_MODULE,
  CLAUDE_DEEPSEEK_STAGE_WALL_SECONDS,
  CLAUDE_DEEPSEEK_METHODS_CALLS,
  CLAUDE_DEEPSEEK_METHODS_TOKEN_LIMIT,
  CLAUDE_DEEPSEEK_METHODS_USD_LIMIT,
  CLAUDE_DEEPSEEK_MODEL,
  CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
  CLAUDE_DEEPSEEK_SCENARIOS,
  assertRegistrationUnchanged,
  buildRegistrationProducerIdentity,
  claudeDeepseekE2ECallCount,
  registrationCachePath,
  scenarioPaths,
  treeDigest,
  validateClaudeDeepseekIdentity,
  validateRegistrationCache,
} from "./runtime/claude-deepseek-contract.mjs";
import { clientToolInputPolicyIdentity, runE2E } from "./runtime/claude-deepseek-e2e-runner.mjs";
import { runMethodsBootstrap, verifyMethodsCacheOnly } from "./runtime/claude-deepseek-methods-runner.mjs";
import {
  aggregateUsage,
  expectedSuiteCalls,
  failureDomain,
  scenarioDecision,
  scenarioVerdictReference,
  standaloneScenarioRoots,
  standalonePlatform,
  suiteStatus,
} from "../standalone-suite.mjs";

const SCRIPT_ROOT = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(SCRIPT_ROOT, "..", "..", "..", "..");
const FRAMEWORK_ID = "macos-claude-deepseek-quick-validation";
const FRAMEWORK_VERSION = 1;
const METHODS_GOAL = "dev.macos-claude-deepseek-methods";
const E2E_GOAL = "dev.macos-claude-deepseek-e2e";
const GOALS = new Set([METHODS_GOAL, E2E_GOAL]);
const FLAGS = new Set(["plan-only", "allow-real-model", "all-scenarios", "help"]);
const VALUE_ARGUMENTS = new Set(["goal", "client", "claude-entry", "claude-settings", "cache-root", "runs-root", "python-entry", "logparse-source", "scenario", "reason", "hypothesis", "expected-evidence"]);
const EVIDENCE_V2_REAL_DIAGNOSIS_BLOCKER = Object.freeze({
  code: "EVIDENCE_V2_REAL_DIAGNOSIS_ADAPTER_UNMIGRATED",
  detail: "Standalone Claude/DeepSeek E2E 仍消费旧版 Methods V1 定位和 result.zip 产物，迁移完成前禁止调用真实模型。",
});

const REQUIRED_EVIDENCE = Object.freeze({
  [METHODS_GOAL]: ["quick-codex-luna-contracts.tap", "quick-claude-methods-contracts.tap", "claude-identity.json", "model-invocations.json", "model-usage.json", "methods-package.json", "scenario-evaluation-audit.json", "security-audit.json", "adapter-receipt.json"],
  [E2E_GOAL]: ["quick-claude-e2e-contracts.tap", "scenario-input.json", "scenario-oracle.json", "methods-package.json", "claude-identity.json", "model-invocations.json", "model-usage.json", "client-events.jsonl", "client-skill.json", "mcp-tool-calls.json", "attachment.json", "server-events.ndjson", "server-lifecycle.json", "server-sealed-diagnosis.json", "final-case.json", "artifact-index.json", "artifact-download.json", "specialized-runtime.json", "logparse-config.json", "http-boundary-audit.json", "security-audit.json", "adapter-receipt.json"],
});

class QuickValidationError extends Error {
  constructor(code, message) { super(message); this.name = "QuickValidationError"; this.code = code; }
}
function fail(code, message) { throw new QuickValidationError(code, message); }

export function parseArguments(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (!argument.startsWith("--")) fail("CLAUDE_DEEPSEEK_ARGUMENT_INVALID", "Arguments must use --name value syntax");
    const name = argument.slice(2);
    if (!FLAGS.has(name) && !VALUE_ARGUMENTS.has(name)) fail("CLAUDE_DEEPSEEK_ARGUMENT_UNKNOWN", `Unsupported argument --${name}`);
    if (Object.hasOwn(values, name)) fail("CLAUDE_DEEPSEEK_ARGUMENT_DUPLICATE", `Argument --${name} is duplicated`);
    if (FLAGS.has(name)) values[name] = true;
    else {
      if (index + 1 >= argv.length || argv[index + 1].startsWith("--")) fail("CLAUDE_DEEPSEEK_ARGUMENT_MISSING", `Argument --${name} requires a value`);
      values[name] = argv[++index];
    }
  }
  if (values.help === true) return values;
  if (!GOALS.has(values.goal)) fail("CLAUDE_DEEPSEEK_GOAL_INVALID", `--goal must be ${METHODS_GOAL} or ${E2E_GOAL}`);
  if ((values.client ?? "macos") !== "macos") fail("CLAUDE_DEEPSEEK_CLIENT_INVALID", "Claude/DeepSeek Quick Validation supports --client macos only");
  if (values.scenario !== undefined && !CLAUDE_DEEPSEEK_SCENARIOS.includes(values.scenario)) fail("CLAUDE_DEEPSEEK_SCENARIO_INVALID", "--scenario is outside the repository-owned matrix");
  if (values["all-scenarios"] === true && values.scenario !== undefined) fail("CLAUDE_DEEPSEEK_SCENARIO_SELECTION_CONFLICT", "--all-scenarios and --scenario are mutually exclusive");
  if (values["all-scenarios"] === true && values.goal !== E2E_GOAL) fail("CLAUDE_DEEPSEEK_SUITE_GOAL_INVALID", `--all-scenarios requires --goal ${E2E_GOAL}`);
  return values;
}

export function defaults(values, environment = process.env) {
  return {
    goal: values.goal,
    client: values.client ?? "macos",
    planOnly: values["plan-only"] === true,
    allowRealModel: values["allow-real-model"] === true,
    allScenarios: values["all-scenarios"] === true,
    claudeEntry: path.resolve(values["claude-entry"] ?? path.join(REPO_ROOT, ".tmp", "test-flow-cache", "claude", "2.1.89", "package", "cli.js")),
    claudeSettings: path.resolve(values["claude-settings"] ?? path.join(REPO_ROOT, ".tmp", "test-flow-release", "settings.json")),
    cacheRoot: path.resolve(values["cache-root"] ?? path.join(REPO_ROOT, ".tmp", "quick-validation", "claude-deepseek", "cache")),
    runsRoot: path.resolve(values["runs-root"] ?? path.join(REPO_ROOT, ".tmp", "quick-validation", "claude-deepseek", "runs")),
    scratchRoot: environment.TEST_FLOW_QUICK_SCRATCH_ROOT ? path.resolve(environment.TEST_FLOW_QUICK_SCRATCH_ROOT) : null,
    pythonEntry: path.resolve(values["python-entry"] ?? path.join(REPO_ROOT, ".venv", "bin", "python")),
    logparseRoot: path.resolve(values["logparse-source"] ?? path.join(environment.HOME ?? "", "Documents", "Codex", "2026-06-29-github-issue-locator-logparse", "logparse")),
    scenario: values.scenario ?? "api-execution-overrun",
    retryContext: { reason: values.reason ?? null, hypothesis: values.hypothesis ?? null, expected_evidence: values["expected-evidence"] ?? null },
  };
}

function requiredFile(filePath, code, label, blockers) {
  try { if (!fs.statSync(filePath).isFile()) throw new Error("not-file"); } catch { blockers.push({ code, detail: `${label} is unavailable` }); }
}
function requiredDirectory(directory, code, label, blockers) {
  try { if (!fs.statSync(directory).isDirectory()) throw new Error("not-directory"); } catch { blockers.push({ code, detail: `${label} is unavailable` }); }
}

export function buildPlan(options) {
  const blockers = [];
  const platform = standalonePlatform();
  if (platform.status !== "SUPPORTED") blockers.push({ code: "CLAUDE_DEEPSEEK_HOST_UNSUPPORTED", detail: "Fast E2E requires native macOS arm64 or the explicitly sealed Ubuntu 22.04 Linux/x64 container" });
  if (platform.sealed && options.scratchRoot === null) blockers.push({ code: "CLAUDE_DEEPSEEK_SCRATCH_ROOT_REQUIRED", detail: "Sealed Linux Fast E2E requires the wrapper-owned tmpfs scratch root" });
  if (options.scratchRoot !== null) {
    requiredDirectory(options.scratchRoot, "CLAUDE_DEEPSEEK_SCRATCH_ROOT_MISSING", "standalone scratch root", blockers);
    const scratch = path.resolve(options.scratchRoot);
    const runs = path.resolve(options.runsRoot);
    if (scratch === runs || scratch.startsWith(`${runs}${path.sep}`) || runs.startsWith(`${scratch}${path.sep}`)) {
      blockers.push({ code: "CLAUDE_DEEPSEEK_SCRATCH_ROOT_OVERLAP", detail: "Standalone scratch and persisted runs roots must not overlap" });
    }
  }
  requiredFile(options.claudeEntry, "CLAUDE_DEEPSEEK_CLI_MISSING", "Claude Code cli.js", blockers);
  requiredFile(options.claudeSettings, "CLAUDE_DEEPSEEK_SETTINGS_MISSING", "audited Claude settings", blockers);
  requiredFile(options.pythonEntry, "CLAUDE_DEEPSEEK_PYTHON_MISSING", "validator/service Python", blockers);
  const caseRoot = path.join(REPO_ROOT, "tests", "cases", "release", "rpc-timeout-anonymized");
  const metaSkillRoot = path.join(REPO_ROOT, ".claude", "skills", "wiki-to-logparse-diagnosis-skill");
  const wiki = path.join(caseRoot, "input", "wiki.md");
  const sourceLogparseConfig = path.join(REPO_ROOT, "experiments", "rpc-skill-feasibility", "logparse-config.json");
  const helperRoot = path.join(REPO_ROOT, ".claude", "skills", "logparse-diagnose");
  const brokerEntry = path.join(path.dirname(options.pythonEntry), "problem-locator-logparse");
  let identity = null;
  let producer = null;
  let cache = { status: "UNKNOWN", code: null, path: null, registration_tree_sha256: null, runtime_ref: null };
  if (blockers.length === 0) {
    try {
      identity = validateClaudeDeepseekIdentity(options.claudeEntry, options.claudeSettings);
      producer = buildRegistrationProducerIdentity({ wiki, metaSkillRoot, claudeIdentity: identity, module: CLAUDE_DEEPSEEK_MODULE });
      const cachePath = registrationCachePath(options.cacheRoot, producer.producer_identity);
      try {
        const receipt = validateRegistrationCache({ cacheRoot: options.cacheRoot, producer });
        assertRegistrationUnchanged(receipt);
        cache = { status: "PRESENT", code: null, path: cachePath, registration_tree_sha256: receipt.manifest.registration.tree_sha256, runtime_ref: receipt.manifest.registration.runtime_ref };
      } catch (error) {
        cache = { status: fs.existsSync(cachePath) ? "INVALID" : "MISSING", code: error?.code ?? "CLAUDE_DEEPSEEK_CACHE_INVALID", path: cachePath, registration_tree_sha256: null, runtime_ref: null };
      }
    } catch (error) { blockers.push({ code: error?.code ?? "CLAUDE_DEEPSEEK_IDENTITY_INVALID", detail: error?.message ?? "Claude identity is invalid" }); }
  }
  const scenarios = options.goal === E2E_GOAL
    ? options.allScenarios ? [...CLAUDE_DEEPSEEK_SCENARIOS] : [options.scenario]
    : [];
  if (options.goal === E2E_GOAL) {
    blockers.push(EVIDENCE_V2_REAL_DIAGNOSIS_BLOCKER);
    requiredDirectory(options.logparseRoot, "CLAUDE_DEEPSEEK_LOGPARSE_MISSING", "Logparse source", blockers);
    requiredFile(sourceLogparseConfig, "CLAUDE_DEEPSEEK_LOGPARSE_CONFIG_MISSING", "repository Logparse config", blockers);
    requiredDirectory(helperRoot, "CLAUDE_DEEPSEEK_HELPER_MISSING", "Server logparse-diagnose Helper", blockers);
    requiredFile(brokerEntry, "CLAUDE_DEEPSEEK_BROKER_ENTRY_MISSING", "job-scoped problem-locator-logparse entry", blockers);
    for (const scenario of scenarios) {
      try { scenarioPaths(REPO_ROOT, scenario); } catch (error) { blockers.push({ code: error?.code ?? "CLAUDE_DEEPSEEK_SCENARIO_INVALID", detail: error?.message ?? `Scenario is invalid: ${scenario}` }); }
    }
    if (cache.status !== "PRESENT") blockers.push({ code: "CLAUDE_DEEPSEEK_REGISTRATION_CACHE_REQUIRED", detail: `E2E requires the exact generated registration cache (${cache.code ?? cache.status})` });
  }
  const helperIdentity = options.goal === E2E_GOAL && fs.existsSync(helperRoot) ? { name: "logparse-diagnose", tree_sha256: treeDigest(helperRoot, { directoryMode: 0o700 }) } : null;
  const providerRuntimeIdentity = { tree_sha256: treeDigest(path.join(SCRIPT_ROOT, "runtime"), { directoryMode: 0o700 }) };
  let logparseConfig = null;
  if (options.goal === E2E_GOAL && fs.existsSync(sourceLogparseConfig)) {
    try {
      const config = JSON.parse(fs.readFileSync(sourceLogparseConfig, "utf8"));
      const product = config.products?.["rpc-skill-feasibility"];
      if (config.schema_version !== 2 || product === null || typeof product !== "object" || Array.isArray(product)) throw new Error("invalid-config");
      logparseConfig = { materialization_version: 1, source_sha256: sha256File(sourceLogparseConfig), product: "default", module: CLAUDE_DEEPSEEK_MODULE, product_bytes_sha256: sha256Bytes(canonicalJson(product)) };
    } catch { blockers.push({ code: "CLAUDE_DEEPSEEK_LOGPARSE_CONFIG_INVALID", detail: "Repository Logparse config cannot materialize the default product" }); }
  }
  if (options.goal === METHODS_GOAL && cache.status === "INVALID") blockers.push({ code: "CLAUDE_DEEPSEEK_REGISTRATION_CACHE_INVALID", detail: `Exact producer path exists but is invalid (${cache.code})` });
  const mode = options.goal === METHODS_GOAL ? (cache.status === "PRESENT" ? "cache-verification" : "generation") : options.allScenarios ? "e2e-suite" : "e2e";
  const expectedCalls = mode === "cache-verification" ? 0 : options.goal === METHODS_GOAL ? CLAUDE_DEEPSEEK_METHODS_CALLS : expectedSuiteCalls(scenarios, claudeDeepseekE2ECallCount);
  const core = {
    schema_version: 1, framework: FRAMEWORK_ID, framework_version: FRAMEWORK_VERSION, goal: options.goal, mode, scenario: options.goal === E2E_GOAL && !options.allScenarios ? options.scenario : null, scenarios,
    execution: {
      entry: "tools/test-flow/quick-validation/claude-deepseek/run.mjs", old_cross_job: false, old_test_flow_orchestrator: false, source_snapshot: false, history_reuse: false, automatic_model_retry: false,
      platform, expected_model_processes: expectedCalls, model: CLAUDE_DEEPSEEK_MODEL, token_cap: options.goal === METHODS_GOAL ? CLAUDE_DEEPSEEK_METHODS_TOKEN_LIMIT : CLAUDE_DEEPSEEK_E2E_TOKEN_LIMIT * scenarios.length, usd_cap: options.goal === METHODS_GOAL ? CLAUDE_DEEPSEEK_METHODS_USD_LIMIT : CLAUDE_DEEPSEEK_E2E_USD_LIMIT * scenarios.length,
      per_scenario: scenarios.map((scenario) => ({ scenario_id: scenario, expected_model_processes: claudeDeepseekE2ECallCount(scenario), token_cap: CLAUDE_DEEPSEEK_E2E_TOKEN_LIMIT, usd_cap: CLAUDE_DEEPSEEK_E2E_USD_LIMIT })),
      stage_wall_seconds: options.goal === E2E_GOAL ? CLAUDE_DEEPSEEK_STAGE_WALL_SECONDS * scenarios.length : CLAUDE_DEEPSEEK_STAGE_WALL_SECONDS, per_process_wall_seconds: options.goal === METHODS_GOAL ? 1800 : 600, no_progress_seconds: CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS, docker: false, browser: false, restart: false,
    },
    inputs: { repository_root: REPO_ROOT, scratch_root: options.scratchRoot, provider_runtime: providerRuntimeIdentity, client: options.client, client_prompt: options.goal === E2E_GOAL ? clientToolInputPolicyIdentity() : null, claude: identity, producer, registration_cache: cache, helper: helperIdentity, module: CLAUDE_DEEPSEEK_MODULE, python_entry: options.pythonEntry, broker_entry: options.goal === E2E_GOAL && fs.existsSync(brokerEntry) ? { path: brokerEntry, sha256: sha256File(brokerEntry) } : null, logparse_root: options.goal === E2E_GOAL ? options.logparseRoot : null, logparse_config: logparseConfig, retry_context: options.retryContext },
    contracts: options.goal === METHODS_GOAL ? ["quick.codex-luna.contracts", "quick.claude-deepseek.methods.contracts"] : ["quick.claude-deepseek.e2e.contracts"],
    evidence: REQUIRED_EVIDENCE[options.goal],
    admission: { status: blockers.length === 0 ? "READY" : "BLOCKED", blockers },
  };
  return { ...core, plan_sha256: sha256Bytes(canonicalJson(core)) };
}

function runId() {
  const timestamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
  return `claude-deepseek-${timestamp}-${crypto.randomBytes(4).toString("hex")}`;
}

function runContracts(files, destination) {
  const result = spawnSync(process.execPath, ["--test", "--test-reporter=tap", ...files], { cwd: REPO_ROOT, env: process.env, encoding: "utf8", timeout: 180_000, stdio: ["ignore", "pipe", "pipe"] });
  fs.writeFileSync(destination, `${result.stdout}${result.stderr}`, { encoding: "utf8", mode: 0o600, flag: "wx" });
  if (result.status !== 0 || result.signal !== null || result.error) fail("CLAUDE_DEEPSEEK_CONTRACTS_FAILED", `Quick contract Gate failed: ${path.basename(destination)}`);
}

function runDeterministicGates(goal, evidenceRoot) {
  if (goal === METHODS_GOAL) {
    const codexTests = fs.readdirSync(path.join(REPO_ROOT, "tools", "test-flow", "quick-validation", "codex-luna", "tests")).filter((name) => name.endsWith(".test.mjs")).map((name) => path.join("tools", "test-flow", "quick-validation", "codex-luna", "tests", name));
    runContracts(codexTests, path.join(evidenceRoot, "quick-codex-luna-contracts.tap"));
    runContracts(["tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-contract.test.mjs", "tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-process.test.mjs", "tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-methods-runner.test.mjs"], path.join(evidenceRoot, "quick-claude-methods-contracts.tap"));
  } else runContracts(["tools/test-flow/quick-validation/standalone-suite.test.mjs", "tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-contract.test.mjs", "tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-process.test.mjs", "tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-bash-policy.test.mjs", "tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-service-wrapper.test.mjs", "tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-e2e-runner.test.mjs"], path.join(evidenceRoot, "quick-claude-e2e-contracts.tap"));
}

export function deterministicGateRoot({ goal, evidenceRoot, scratchRunRoot }) {
  if (!GOALS.has(goal) || typeof evidenceRoot !== "string" || typeof scratchRunRoot !== "string") fail("CLAUDE_DEEPSEEK_CONTRACT_EVIDENCE_ROOT_INVALID", "Deterministic Gate roots are invalid");
  return path.join(path.resolve(scratchRunRoot), "deterministic-gates");
}

export function materializeDeterministicGateEvidence({ stagingRoot, evidenceRoot }) {
  const source = path.resolve(stagingRoot);
  const destination = path.resolve(evidenceRoot);
  if (source === destination) return { moved: [], status: "SKIP" };
  const entries = fs.readdirSync(source, { withFileTypes: true });
  const moved = [];
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.endsWith(".tap")) fail("CLAUDE_DEEPSEEK_CONTRACT_EVIDENCE_INVALID", "Deterministic Gate staging contains an unexpected node");
    const from = path.join(source, entry.name);
    const to = path.join(destination, entry.name);
    const temporary = path.join(destination, `.${entry.name}.${process.pid}.tmp`);
    if (fs.existsSync(to)) fail("CLAUDE_DEEPSEEK_CONTRACT_EVIDENCE_COLLISION", "Deterministic Gate evidence destination already exists");
    const expectedSize = fs.statSync(from).size;
    const expectedSha256 = sha256File(from);
    try {
      fs.copyFileSync(from, temporary, fs.constants.COPYFILE_EXCL);
      fs.chmodSync(temporary, 0o600);
      const descriptor = fs.openSync(temporary, "r+");
      try { fs.fsyncSync(descriptor); } finally { fs.closeSync(descriptor); }
      if (fs.statSync(temporary).size !== expectedSize || sha256File(temporary) !== expectedSha256) {
        fail("CLAUDE_DEEPSEEK_CONTRACT_EVIDENCE_COPY_MISMATCH", "Copied deterministic Gate evidence differs from staging bytes");
      }
      fs.renameSync(temporary, to);
      fs.unlinkSync(from);
    } catch (error) {
      if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
      throw error;
    }
    moved.push(entry.name);
  }
  fs.rmdirSync(source);
  return { moved: moved.sort(), status: "PASS" };
}

function evidenceManifest(root) {
  return fs.existsSync(root) ? fs.readdirSync(root, { withFileTypes: true }).filter((entry) => entry.isFile()).map((entry) => ({ name: entry.name, size: fs.statSync(path.join(root, entry.name)).size, sha256: sha256File(path.join(root, entry.name)) })).sort((a, b) => a.name.localeCompare(b.name)) : [];
}

export function sealGate({ goal, mode, evidenceRoot, expectedCalls, failure = null, requiredEvidence = REQUIRED_EVIDENCE[goal] }) {
  const manifest = evidenceManifest(evidenceRoot);
  const names = new Set(manifest.map((item) => item.name));
  const missing = requiredEvidence.filter((name) => !names.has(name));
  let actualCalls = null;
  let usage = null;
  if (names.has("model-invocations.json")) actualCalls = JSON.parse(fs.readFileSync(path.join(evidenceRoot, "model-invocations.json"), "utf8")).invocations?.length ?? null;
  if (names.has("model-usage.json")) usage = JSON.parse(fs.readFileSync(path.join(evidenceRoot, "model-usage.json"), "utf8")).aggregate ?? null;
  const adapterPass = names.has("adapter-receipt.json") && JSON.parse(fs.readFileSync(path.join(evidenceRoot, "adapter-receipt.json"), "utf8")).status === "PASS";
  const receipt = { schema_version: 1, framework: FRAMEWORK_ID, goal, mode, status: failure === null && missing.length === 0 && actualCalls === expectedCalls && adapterPass ? "PASS" : "FAIL", expected_model_processes: expectedCalls, actual_model_processes: actualCalls, retry_count: 0, missing_evidence: missing, usage, failure, evidence: manifest };
  writeJsonExclusive(path.join(evidenceRoot, "gate-receipt.json"), receipt);
  return receipt;
}

function writeJsonExclusive(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, canonicalJson(value), { encoding: "utf8", mode: 0o600, flag: "wx" });
}

function safeFailure(error) { return { code: error?.code ?? "CLAUDE_DEEPSEEK_UNEXPECTED", message: error?.message ?? String(error) }; }

function scenarioPlan(plan, scenario) {
  const core = {
    ...plan,
    mode: "e2e",
    scenario,
    scenarios: [scenario],
    execution: {
      ...plan.execution,
      expected_model_processes: claudeDeepseekE2ECallCount(scenario),
      token_cap: CLAUDE_DEEPSEEK_E2E_TOKEN_LIMIT,
      usd_cap: CLAUDE_DEEPSEEK_E2E_USD_LIMIT,
      per_scenario: [{ scenario_id: scenario, expected_model_processes: claudeDeepseekE2ECallCount(scenario), token_cap: CLAUDE_DEEPSEEK_E2E_TOKEN_LIMIT, usd_cap: CLAUDE_DEEPSEEK_E2E_USD_LIMIT }],
      stage_wall_seconds: CLAUDE_DEEPSEEK_STAGE_WALL_SECONDS,
    },
  };
  delete core.plan_sha256;
  return { ...core, plan_sha256: sha256Bytes(canonicalJson(core)) };
}

async function executeOne(options, plan, {
  id = runId(),
  runRoot = path.join(options.runsRoot, id),
  runContractsFirst = true,
  requiredEvidence = REQUIRED_EVIDENCE[plan.goal],
} = {}) {
  const roots = standaloneScenarioRoots({ runRoot, runId: id, scratchRoot: options.scratchRoot });
  const { scratch_run_root: scratchRunRoot, work_root: workRoot, private_root: privateRoot, evidence_root: evidenceRoot, usage_root: usageRoot } = roots;
  fs.mkdirSync(runRoot, { recursive: false, mode: 0o700 });
  if (scratchRunRoot !== runRoot) fs.mkdirSync(scratchRunRoot, { recursive: false, mode: 0o700 });
  for (const directory of [workRoot, privateRoot, evidenceRoot, usageRoot]) fs.mkdirSync(directory, { mode: 0o700 });
  writeJsonExclusive(path.join(runRoot, "plan.json"), plan);
  const startedAt = new Date().toISOString();
  const expectedCalls = plan.execution.expected_model_processes;
  let deterministicRoot = null;
  let statusOverride = null;
  let failure = null;
  if (plan.admission.status !== "READY") { failure = { code: "CLAUDE_DEEPSEEK_PLAN_BLOCKED", blockers: plan.admission.blockers }; statusOverride = "BLOCKED"; }
  else if (expectedCalls > 0 && !options.allowRealModel) { failure = { code: "CLAUDE_DEEPSEEK_REAL_MODEL_OPT_IN_REQUIRED", message: "Execution with Claude processes requires --allow-real-model" }; statusOverride = "BLOCKED"; }
  else {
    try {
      if (runContractsFirst) {
        deterministicRoot = deterministicGateRoot({ goal: plan.goal, evidenceRoot, scratchRunRoot });
        if (deterministicRoot !== evidenceRoot) fs.mkdirSync(deterministicRoot, { mode: 0o700 });
        runDeterministicGates(plan.goal, deterministicRoot);
      }
      const caseRoot = path.join(REPO_ROOT, "tests", "cases", "release", "rpc-timeout-anonymized");
      const common = { runId: id, sourceRoot: REPO_ROOT, claudeEntry: options.claudeEntry, claudeSettings: options.claudeSettings, pythonEntry: options.pythonEntry, cacheRoot: options.cacheRoot, workRoot, privateRoot, evidenceRoot, usageRoot };
      if (plan.goal === METHODS_GOAL) {
        const methodOptions = { ...common, metaSkillRoot: path.join(REPO_ROOT, ".claude", "skills", "wiki-to-logparse-diagnosis-skill"), wiki: path.join(caseRoot, "input", "wiki.md"), oracle: path.join(caseRoot, "oracle.json"), module: CLAUDE_DEEPSEEK_MODULE };
        if (plan.mode === "cache-verification") verifyMethodsCacheOnly(methodOptions); else await runMethodsBootstrap(methodOptions);
      } else await runE2E({ ...common, logparseRoot: options.logparseRoot, scenario: options.scenario });
    } catch (error) { failure = safeFailure(error); }
  }
  if (deterministicRoot !== null && deterministicRoot !== evidenceRoot) {
    try { materializeDeterministicGateEvidence({ stagingRoot: deterministicRoot, evidenceRoot }); }
    catch (error) { failure = safeFailure(error); }
  }
  const gate = sealGate({ goal: plan.goal, mode: plan.mode, evidenceRoot, expectedCalls, failure, requiredEvidence });
  const finishedAt = new Date().toISOString();
  const verdict = { schema_version: 1, framework: FRAMEWORK_ID, framework_version: FRAMEWORK_VERSION, run_id: id, goal: plan.goal, mode: plan.mode, scenario: plan.scenario, status: statusOverride ?? gate.status, started_at_utc: startedAt, finished_at_utc: finishedAt, elapsed_seconds: (Date.parse(finishedAt) - Date.parse(startedAt)) / 1000, plan_sha256: plan.plan_sha256, source_snapshot: false, old_cross_job_finalization: false, gate_receipt: { path: "evidence/gate-receipt.json", sha256: sha256File(path.join(evidenceRoot, "gate-receipt.json")) }, model_processes: { expected: expectedCalls, actual: gate.actual_model_processes, retry_count: 0 }, usage: gate.usage, failure: gate.failure, failure_domain: gate.failure ? failureDomain(gate.failure) : null };
  writeJsonExclusive(path.join(runRoot, "verdict.json"), verdict);
  return { verdict, runRoot, exitCode: verdict.status === "PASS" ? 0 : verdict.status === "BLOCKED" ? 2 : 1 };
}

export async function executeSuite(options, plan, {
  executeOneImpl = executeOne,
  runDeterministicGatesImpl = runDeterministicGates,
} = {}) {
  const id = runId().replace(/^claude-deepseek-/, "claude-deepseek-suite-");
  const runRoot = path.join(options.runsRoot, id);
  const preflightRoot = path.join(runRoot, "evidence", "preflight");
  fs.mkdirSync(preflightRoot, { recursive: true, mode: 0o700 });
  writeJsonExclusive(path.join(runRoot, "plan.json"), plan);
  const startedAt = new Date().toISOString();
  const references = [];
  let engineeringFailure = null;
  let blockedFailure = null;
  let unsealedScenario = null;
  if (plan.admission.status !== "READY") blockedFailure = { code: "CLAUDE_DEEPSEEK_PLAN_BLOCKED", blockers: plan.admission.blockers };
  else if (!options.allowRealModel) blockedFailure = { code: "CLAUDE_DEEPSEEK_REAL_MODEL_OPT_IN_REQUIRED", message: "Execution with Claude processes requires --allow-real-model" };
  else {
    try { runDeterministicGatesImpl(E2E_GOAL, preflightRoot); }
    catch (error) { engineeringFailure = safeFailure(error); }
  }

  if (blockedFailure === null && engineeringFailure === null) {
    fs.mkdirSync(path.join(runRoot, "scenarios"), { mode: 0o700 });
    const childEvidence = REQUIRED_EVIDENCE[E2E_GOAL].filter((name) => !name.endsWith(".tap"));
    for (const scenario of plan.scenarios) {
      let child;
      let reference;
      try {
        child = await executeOneImpl({ ...options, scenario, allScenarios: false }, scenarioPlan(plan, scenario), {
          id: `${id}-${scenario}`,
          runRoot: path.join(runRoot, "scenarios", scenario),
          runContractsFirst: false,
          requiredEvidence: childEvidence,
        });
        reference = scenarioVerdictReference({ suiteRoot: runRoot, scenario, verdict: child.verdict, sha256File, modelField: "model_processes" });
      } catch (error) {
        const failure = safeFailure(error);
        engineeringFailure = { scenario_id: scenario, ...failure };
        unsealedScenario = { scenario_id: scenario, status: "ERROR", failure_domain: "ENGINEERING", model_processes: null, usage: null, failure, verdict: null };
        break;
      }
      references.push(reference);
      if (child.verdict.status === "PASS") continue;
      const decision = scenarioDecision(child.verdict);
      references.at(-1).failure_domain = decision.failure_domain;
      if (decision.stop) {
        engineeringFailure = { scenario_id: scenario, ...(child.verdict.failure ?? { code: "CLAUDE_DEEPSEEK_SCENARIO_ENGINEERING_FAILURE" }) };
        break;
      }
    }
  }

  const completed = new Set(references.map((item) => item.scenario_id));
  const referencesByScenario = new Map(references.map((item) => [item.scenario_id, item]));
  const scenarioResults = plan.scenarios.map((scenario) => referencesByScenario.get(scenario)
    ?? (unsealedScenario?.scenario_id === scenario
      ? unsealedScenario
      : { scenario_id: scenario, status: "NOT_RUN", failure_domain: null, model_processes: null, usage: null, failure: null, verdict: null }));
  const attemptedCount = completed.size + (unsealedScenario === null ? 0 : 1);
  const status = suiteStatus({ blocked: blockedFailure !== null, engineeringFailure, references, expectedCount: plan.scenarios.length });
  const actualCalls = references.reduce((sum, item) => sum + (Number.isSafeInteger(item.model_processes?.actual) ? item.model_processes.actual : 0), 0);
  const finishedAt = new Date().toISOString();
  const verdict = {
    schema_version: 1, framework: FRAMEWORK_ID, framework_version: FRAMEWORK_VERSION, run_id: id, goal: plan.goal, mode: "e2e-suite", scenario: null,
    scenario_order: plan.scenarios, status, started_at_utc: startedAt, finished_at_utc: finishedAt, elapsed_seconds: (Date.parse(finishedAt) - Date.parse(startedAt)) / 1000,
    plan_sha256: plan.plan_sha256, source_snapshot: false, old_cross_job_finalization: false,
    model_processes: { expected: plan.execution.expected_model_processes, actual: actualCalls, retry_count: 0 }, usage: aggregateUsage(references.map((item) => item.usage)),
    preflight_evidence: evidenceManifest(preflightRoot),
    summary: { expected: plan.scenarios.length, completed: references.length, attempted: attemptedCount, passed: references.filter((item) => item.status === "PASS").length, failed: references.filter((item) => item.status === "FAIL").length, errored: unsealedScenario === null ? 0 : 1, not_run: plan.scenarios.length - attemptedCount },
    scenarios: scenarioResults, failure: blockedFailure ?? engineeringFailure, failure_domain: blockedFailure !== null ? "ADMISSION" : engineeringFailure !== null ? "ENGINEERING" : null,
    stop_reason: blockedFailure !== null
      ? { domain: "ADMISSION", failure: blockedFailure }
      : engineeringFailure !== null
        ? { domain: "ENGINEERING", failure: engineeringFailure }
        : null,
  };
  writeJsonExclusive(path.join(runRoot, "verdict.json"), verdict);
  return { verdict, runRoot, exitCode: status === "PASS" ? 0 : status === "BLOCKED" ? 2 : 1 };
}

export async function execute(options, plan) {
  return options.allScenarios ? executeSuite(options, plan) : executeOne(options, plan);
}

function usage() { return `Usage:\n  ./tools/test-flow/quick-validation/claude-deepseek/run.sh --goal ${METHODS_GOAL} [--plan-only] [--allow-real-model]\n  ./tools/test-flow/quick-validation/claude-deepseek/run.sh --goal ${E2E_GOAL} --logparse-source <path> (--scenario <repository-id> | --all-scenarios) [--plan-only] [--allow-real-model]\n`; }

async function main() {
  try {
    const parsed = parseArguments(process.argv.slice(2));
    if (parsed.help) { process.stdout.write(usage()); return; }
    const options = defaults(parsed);
    const plan = buildPlan(options);
    if (options.planOnly) { process.stdout.write(canonicalJson(plan)); return; }
    fs.mkdirSync(options.runsRoot, { recursive: true, mode: 0o700 });
    const result = await execute(options, plan);
    process.stdout.write(canonicalJson({ run_id: result.verdict.run_id, status: result.verdict.status, verdict: path.join(result.runRoot, "verdict.json") }));
    process.exitCode = result.exitCode;
  } catch (error) { process.stderr.write(canonicalJson({ status: "ERROR", failure: safeFailure(error) })); process.exitCode = 2; }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();

export { E2E_GOAL, METHODS_GOAL, REQUIRED_EVIDENCE };
