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
  CLAUDE_DEEPSEEK_REGISTRATION_ID,
  assertRegistrationUnchanged,
  buildRegistrationProducerIdentity,
  registrationCachePath,
  treeDigest,
  validateClaudeDeepseekIdentity,
  validateRegistrationCache,
} from "./runtime/claude-deepseek-contract.mjs";
import { runE2E, validateExplicitRegistrationInput } from "./runtime/claude-deepseek-e2e-runner.mjs";
import { runFastE2E, validateFastRegistrationInput } from "./runtime/claude-deepseek-fast-e2e-runner.mjs";
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
import {
  FAST_E2E_SCENARIOS,
  fastE2ECallHardCap,
  fastE2ENormalCallCount,
  scenarioPaths as fastScenarioPaths,
} from "../fast-e2e-scenarios.mjs";

const SCRIPT_ROOT = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(SCRIPT_ROOT, "..", "..", "..", "..");
const FRAMEWORK_ID = "macos-claude-deepseek-quick-validation";
const FRAMEWORK_VERSION = 1;
const METHODS_GOAL = "dev.macos-claude-deepseek-methods";
const E2E_GOAL = "dev.macos-claude-deepseek-e2e";
const FAST_E2E_GOAL = "fast-e2e";
const GOALS = new Set([METHODS_GOAL, E2E_GOAL, FAST_E2E_GOAL]);
const FLAGS = new Set(["plan-only", "allow-real-model", "all-scenarios", "help"]);
const VALUE_ARGUMENTS = new Set(["goal", "client", "claude-entry", "claude-settings", "cache-root", "registration-root", "runs-root", "python-entry", "scenario", "source-snapshot-digest", "core-verdict", "reason", "hypothesis", "expected-evidence"]);

const REQUIRED_EVIDENCE = Object.freeze({
  [METHODS_GOAL]: ["quick-codex-luna-contracts.tap", "quick-claude-methods-contracts.tap", "claude-identity.json", "model-invocations.json", "model-usage.json", "methods-package.json", "scenario-evaluation-audit.json", "security-audit.json", "adapter-receipt.json"],
  [E2E_GOAL]: ["quick-claude-e2e-contracts.tap", "runtime-receipt.json", "methods-package.json", "claude-identity.json", "model-invocations.json", "model-usage.json", "methods-source-job.json", "methods-reviewer-job.json", "methods-evidence-graph-v2.json", "methods-evaluation-plan-v2.json", "methods-limitations-v2.json", "methods-source-state-v2.json", "methods-source-outcome-v2.json", "methods-terminal-state-v2.json", "methods-reviewer-outcome-v2.json", "methods-result-v2.json", "methods.json", "scenario-oracle-receipt.json", "model-cert-input.json", "model-cert.json", "adapter-receipt.json"],
  [FAST_E2E_GOAL]: ["quick-claude-fast-e2e-contracts.tap", "runtime-receipt.json", "methods-package.json", "claude-identity.json", "model-invocations.json", "model-usage.json", "methods-source-job.json", "methods-evidence-graph-v2.json", "methods-evaluation-plan-v2.json", "methods-limitations-v2.json", "methods-source-state-v2.json", "methods-source-outcome-v2.json", "methods-result-v2.json", "methods.json", "fast-e2e-oracle.json", "adapter-receipt.json"],
});

const FAST_E2E_REVIEW_EVIDENCE = Object.freeze([
  ...REQUIRED_EVIDENCE[FAST_E2E_GOAL],
  "methods-reviewer-job.json",
  "methods-terminal-state-v2.json",
  "methods-reviewer-outcome-v2.json",
]);

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
  if (!GOALS.has(values.goal)) fail("CLAUDE_DEEPSEEK_GOAL_INVALID", `--goal must be ${METHODS_GOAL}, ${FAST_E2E_GOAL}, or ${E2E_GOAL}`);
  if ((values.client ?? "macos") !== "macos") fail("CLAUDE_DEEPSEEK_CLIENT_INVALID", "Claude/DeepSeek Quick Validation supports --client macos only");
  if (values["all-scenarios"] === true && values.goal !== FAST_E2E_GOAL) fail("CLAUDE_DEEPSEEK_MODEL_CERT_SUITE_FORBIDDEN", "--all-scenarios belongs only to standalone Fast E2E");
  if (values["all-scenarios"] === true && values.scenario !== undefined) fail("CLAUDE_DEEPSEEK_SCENARIO_SELECTION_CONFLICT", "--all-scenarios and --scenario are mutually exclusive");
  if (values.goal === FAST_E2E_GOAL && values.scenario !== undefined && !FAST_E2E_SCENARIOS.includes(values.scenario)) fail("CLAUDE_DEEPSEEK_SCENARIO_INVALID", "--scenario is not in the historical Fast E2E matrix");
  if (values.goal === E2E_GOAL && values.scenario !== undefined && values.scenario !== "multiple-rpc-timeouts") fail("CLAUDE_DEEPSEEK_SCENARIO_INVALID", "Evidence V2 model-cert uses only multiple-rpc-timeouts");
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
    registrationRoot: values["registration-root"] ? path.resolve(values["registration-root"]) : null,
    runsRoot: path.resolve(values["runs-root"] ?? path.join(REPO_ROOT, ".tmp", "quick-validation", "claude-deepseek", "runs")),
    scratchRoot: environment.TEST_FLOW_QUICK_SCRATCH_ROOT ? path.resolve(environment.TEST_FLOW_QUICK_SCRATCH_ROOT) : null,
    pythonEntry: path.resolve(values["python-entry"] ?? path.join(REPO_ROOT, ".venv", "bin", "python")),
    scenario: values.scenario ?? (values.goal === FAST_E2E_GOAL ? "api-execution-overrun" : "multiple-rpc-timeouts"),
    sourceSnapshotDigest: values["source-snapshot-digest"] ?? null,
    coreVerdict: values["core-verdict"] ? path.resolve(values["core-verdict"]) : null,
    retryContext: { reason: values.reason ?? null, hypothesis: values.hypothesis ?? null, expected_evidence: values["expected-evidence"] ?? null },
  };
}

function requiredFile(filePath, code, label, blockers) {
  try { if (!fs.statSync(filePath).isFile()) throw new Error("not-file"); } catch { blockers.push({ code, detail: `${label} is unavailable` }); }
}
function requiredDirectory(directory, code, label, blockers) {
  try { if (!fs.statSync(directory).isDirectory()) throw new Error("not-directory"); } catch { blockers.push({ code, detail: `${label} is unavailable` }); }
}

function scenarioCallCount(goal, scenario) {
  return goal === FAST_E2E_GOAL ? fastE2ENormalCallCount(scenario) : 2;
}

function scenarioCallHardCap(goal, scenario) {
  return goal === FAST_E2E_GOAL ? fastE2ECallHardCap(scenario) : 4;
}

function scenarioTokenCap(goal, scenario) {
  return goal === FAST_E2E_GOAL && fastE2ENormalCallCount(scenario) === 0
    ? 0
    : CLAUDE_DEEPSEEK_E2E_TOKEN_LIMIT;
}

function scenarioUsdCap(goal, scenario) {
  return goal === FAST_E2E_GOAL && fastE2ENormalCallCount(scenario) === 0
    ? 0
    : CLAUDE_DEEPSEEK_E2E_USD_LIMIT;
}

function fastRequiredEvidence(scenario) {
  return scenario === "insufficient-evidence"
    ? REQUIRED_EVIDENCE[FAST_E2E_GOAL]
    : FAST_E2E_REVIEW_EVIDENCE;
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
  let identity = null;
  let producer = null;
  let cache = options.goal === FAST_E2E_GOAL
    ? { status: "NOT_REQUIRED", code: null, path: null, registration_root: null, registration_tree_sha256: null, runtime_ref: null }
    : { status: "UNKNOWN", code: null, path: null, registration_root: null, registration_tree_sha256: null, runtime_ref: null };
  let explicitRegistration = null;
  if (blockers.length === 0) {
    try {
      identity = validateClaudeDeepseekIdentity(options.claudeEntry, options.claudeSettings);
      if (options.goal !== FAST_E2E_GOAL) {
        const caseRoot = path.join(REPO_ROOT, "tests", "cases", "release", "rpc-timeout-anonymized");
        const metaSkillRoot = path.join(REPO_ROOT, ".claude", "skills", "wiki-to-logparse-diagnosis-skill");
        const wiki = path.join(caseRoot, "input", "wiki.md");
        producer = buildRegistrationProducerIdentity({ wiki, metaSkillRoot, claudeIdentity: identity, module: CLAUDE_DEEPSEEK_MODULE });
        const cachePath = registrationCachePath(options.cacheRoot, producer.producer_identity);
        const registrationRoot = path.join(cachePath, "registration", CLAUDE_DEEPSEEK_REGISTRATION_ID);
        try {
          const receipt = validateRegistrationCache({ cacheRoot: options.cacheRoot, producer });
          assertRegistrationUnchanged(receipt);
          cache = { status: "PRESENT", code: null, path: cachePath, registration_root: receipt.registration_root, registration_tree_sha256: receipt.manifest.registration.tree_sha256, runtime_ref: receipt.manifest.registration.runtime_ref };
        } catch (error) {
          cache = { status: fs.existsSync(cachePath) ? "INVALID" : "MISSING", code: error?.code ?? "CLAUDE_DEEPSEEK_CACHE_INVALID", path: cachePath, registration_root: registrationRoot, registration_tree_sha256: null, runtime_ref: null };
        }
      }
    } catch (error) { blockers.push({ code: error?.code ?? "CLAUDE_DEEPSEEK_IDENTITY_INVALID", detail: error?.message ?? "Claude identity is invalid" }); }
  }
  if ([E2E_GOAL, FAST_E2E_GOAL].includes(options.goal) && options.registrationRoot !== null) {
    try {
      const receipt = options.goal === FAST_E2E_GOAL
        ? validateFastRegistrationInput(options.registrationRoot)
        : validateExplicitRegistrationInput(options.registrationRoot, REPO_ROOT);
      assertRegistrationUnchanged(receipt);
      explicitRegistration = {
        source: receipt.source,
        registration_root: receipt.registration_root,
        registration_id: receipt.manifest.registration.registration_id,
        skill_name: receipt.manifest.registration.skill_name,
        tree_sha256: receipt.manifest.registration.tree_sha256,
        template_sha256: receipt.manifest.registration.template_sha256,
        methods_sha256: receipt.manifest.registration.methods_sha256,
      };
    } catch (error) {
      const missing = !fs.existsSync(options.registrationRoot);
      explicitRegistration = {
        source: "explicit-deferred",
        registration_root: options.registrationRoot,
        registration_id: CLAUDE_DEEPSEEK_REGISTRATION_ID,
        skill_name: null,
        tree_sha256: null,
        template_sha256: null,
        methods_sha256: null,
      };
      blockers.push({ code: missing ? "CLAUDE_DEEPSEEK_REGISTRATION_ROOT_MISSING" : (error?.code ?? "CLAUDE_DEEPSEEK_REGISTRATION_ROOT_INVALID"), detail: error?.message ?? "Explicit production registration is invalid" });
    }
  }
  const scenarios = options.goal === FAST_E2E_GOAL
    ? (options.allScenarios ? [...FAST_E2E_SCENARIOS] : [options.scenario])
    : options.goal === E2E_GOAL ? ["multiple-rpc-timeouts"] : [];
  if (options.goal === E2E_GOAL) {
    if (!/^[a-f0-9]{64}$/u.test(options.sourceSnapshotDigest ?? "")) blockers.push({ code: "CLAUDE_DEEPSEEK_SOURCE_SNAPSHOT_REQUIRED", detail: "Evidence V2 model-cert requires the active source snapshot digest" });
    if (options.coreVerdict === null) blockers.push({ code: "CLAUDE_DEEPSEEK_CORE_VERDICT_REQUIRED", detail: "Evidence V2 model-cert requires the matching Core verdict" });
    else requiredFile(options.coreVerdict, "CLAUDE_DEEPSEEK_CORE_VERDICT_MISSING", "Evidence V2 Core verdict", blockers);
    if (options.registrationRoot === null && cache.status !== "PRESENT") blockers.push({ code: "CLAUDE_DEEPSEEK_REGISTRATION_CACHE_REQUIRED", detail: `E2E requires --registration-root or the exact generated registration cache (${cache.code ?? cache.status})` });
  }
  if (options.goal === FAST_E2E_GOAL) {
    for (const scenario of scenarios) {
      try {
        const paths = fastScenarioPaths(REPO_ROOT, scenario);
        requiredFile(paths.case, "CLAUDE_DEEPSEEK_FAST_E2E_CASE_MISSING", `Fast E2E case ${scenario}`, blockers);
        requiredFile(paths.client_log, "CLAUDE_DEEPSEEK_FAST_E2E_CLIENT_LOG_MISSING", `Fast E2E client log ${scenario}`, blockers);
        requiredFile(paths.server_log, "CLAUDE_DEEPSEEK_FAST_E2E_SERVER_LOG_MISSING", `Fast E2E server log ${scenario}`, blockers);
      } catch (error) {
        blockers.push({ code: "CLAUDE_DEEPSEEK_SCENARIO_INVALID", detail: error?.message ?? `Fast E2E scenario is invalid: ${scenario}` });
      }
    }
    if (options.registrationRoot === null) blockers.push({ code: "CLAUDE_DEEPSEEK_FAST_E2E_REGISTRATION_ROOT_REQUIRED", detail: "Fast E2E requires --registration-root for one validated production registration" });
  }
  const providerRuntimeIdentity = { tree_sha256: treeDigest(path.join(SCRIPT_ROOT, "runtime"), { directoryMode: 0o700 }) };
  if (options.goal === METHODS_GOAL && cache.status === "INVALID") blockers.push({ code: "CLAUDE_DEEPSEEK_REGISTRATION_CACHE_INVALID", detail: `Exact producer path exists but is invalid (${cache.code})` });
  const mode = options.goal === METHODS_GOAL
    ? (cache.status === "PRESENT" ? "cache-verification" : "generation")
    : options.goal === FAST_E2E_GOAL
      ? (options.allScenarios ? "fast-e2e-suite" : "fast-e2e")
      : "model-cert";
  const expectedCalls = mode === "cache-verification"
    ? 0
    : options.goal === METHODS_GOAL
      ? CLAUDE_DEEPSEEK_METHODS_CALLS
      : expectedSuiteCalls(scenarios, (scenario) => scenarioCallCount(options.goal, scenario));
  const hardCap = [E2E_GOAL, FAST_E2E_GOAL].includes(options.goal)
    ? scenarios.reduce((sum, scenario) => sum + scenarioCallHardCap(options.goal, scenario), 0)
    : expectedCalls;
  const tokenCap = options.goal === METHODS_GOAL
    ? CLAUDE_DEEPSEEK_METHODS_TOKEN_LIMIT
    : scenarios.reduce((sum, scenario) => sum + scenarioTokenCap(options.goal, scenario), 0);
  const usdCap = options.goal === METHODS_GOAL
    ? CLAUDE_DEEPSEEK_METHODS_USD_LIMIT
    : scenarios.reduce((sum, scenario) => sum + scenarioUsdCap(options.goal, scenario), 0);
  const core = {
    schema_version: 1, framework: FRAMEWORK_ID, framework_version: FRAMEWORK_VERSION, goal: options.goal, mode, scenario: scenarios.length === 1 ? scenarios[0] : null, scenarios,
    execution: {
      entry: "tools/test-flow/quick-validation/claude-deepseek/run.mjs", old_cross_job: false, old_test_flow_orchestrator: false, source_snapshot: options.goal === E2E_GOAL, history_reuse: false, automatic_model_retry: false,
      platform, expected_model_processes: expectedCalls, model: CLAUDE_DEEPSEEK_MODEL, token_cap: tokenCap, usd_cap: usdCap,
      model_process_hard_cap: hardCap,
      per_scenario: scenarios.map((scenario) => ({ scenario_id: scenario, expected_model_processes: scenarioCallCount(options.goal, scenario), model_process_hard_cap: scenarioCallHardCap(options.goal, scenario), token_cap: scenarioTokenCap(options.goal, scenario), usd_cap: scenarioUsdCap(options.goal, scenario) })),
      stage_wall_seconds: [E2E_GOAL, FAST_E2E_GOAL].includes(options.goal) ? CLAUDE_DEEPSEEK_STAGE_WALL_SECONDS * scenarios.length : CLAUDE_DEEPSEEK_STAGE_WALL_SECONDS, per_process_wall_seconds: options.goal === METHODS_GOAL ? 1800 : 600, no_progress_seconds: CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS, docker: false, browser: false, restart: false,
    },
    inputs: { repository_root: REPO_ROOT, source_snapshot_digest: options.goal === E2E_GOAL ? options.sourceSnapshotDigest : null, core_verdict: options.goal === E2E_GOAL ? options.coreVerdict : null, scratch_root: options.scratchRoot, provider_runtime: providerRuntimeIdentity, client: options.client, claude: identity, producer, production_registration: explicitRegistration ?? { source: options.goal === FAST_E2E_GOAL ? "explicit-required" : "standalone-cache", registration_root: cache.registration_root, registration_id: CLAUDE_DEEPSEEK_REGISTRATION_ID, skill_name: null, tree_sha256: cache.registration_tree_sha256, template_sha256: null, methods_sha256: null }, registration_cache: cache, module: CLAUDE_DEEPSEEK_MODULE, python_entry: options.pythonEntry, retry_context: options.retryContext },
    contracts: options.goal === METHODS_GOAL ? ["quick.codex-luna.contracts", "quick.claude-deepseek.methods.contracts"] : options.goal === FAST_E2E_GOAL ? ["quick.claude-deepseek.fast-e2e.contracts"] : ["quick.claude-deepseek.e2e.contracts"],
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
  } else if (goal === FAST_E2E_GOAL) {
    runContracts([
      "tools/test-flow/quick-validation/fast-e2e-scenarios.test.mjs",
      "tools/test-flow/quick-validation/standalone-suite.test.mjs",
      "tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-fast-e2e-runner.test.mjs",
    ], path.join(evidenceRoot, "quick-claude-fast-e2e-contracts.tap"));
  } else runContracts(["tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-contract.test.mjs", "tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-process.test.mjs", "tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-bash-policy.test.mjs", "tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-service-wrapper.test.mjs", "tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-e2e-runner.test.mjs"], path.join(evidenceRoot, "quick-claude-e2e-contracts.tap"));
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

export function sealGate({ goal, mode, evidenceRoot, expectedCalls, modelProcessHardCap = null, failure = null, requiredEvidence = REQUIRED_EVIDENCE[goal] }) {
  const manifest = evidenceManifest(evidenceRoot);
  const names = new Set(manifest.map((item) => item.name));
  const missing = requiredEvidence.filter((name) => !names.has(name));
  let actualCalls = null;
  let usage = null;
  if (names.has("model-invocations.json")) actualCalls = JSON.parse(fs.readFileSync(path.join(evidenceRoot, "model-invocations.json"), "utf8")).invocations?.length ?? null;
  if (names.has("model-usage.json")) usage = JSON.parse(fs.readFileSync(path.join(evidenceRoot, "model-usage.json"), "utf8")).aggregate ?? null;
  const adapterPass = names.has("adapter-receipt.json") && JSON.parse(fs.readFileSync(path.join(evidenceRoot, "adapter-receipt.json"), "utf8")).status === "PASS";
  const roleEvaluationGoal = [E2E_GOAL, FAST_E2E_GOAL].includes(goal);
  const hardCap = modelProcessHardCap ?? (roleEvaluationGoal ? 4 : expectedCalls);
  const callCountPass = roleEvaluationGoal
    ? Number.isSafeInteger(actualCalls) && actualCalls >= expectedCalls && actualCalls <= hardCap
    : actualCalls === expectedCalls;
  const receipt = { schema_version: 1, framework: FRAMEWORK_ID, goal, mode, status: failure === null && missing.length === 0 && callCountPass && adapterPass ? "PASS" : "FAIL", expected_model_processes: expectedCalls, model_process_hard_cap: hardCap, actual_model_processes: actualCalls, retry_count: 0, missing_evidence: missing, usage, failure, evidence: manifest };
  writeJsonExclusive(path.join(evidenceRoot, "gate-receipt.json"), receipt);
  return receipt;
}

function writeJsonExclusive(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, canonicalJson(value), { encoding: "utf8", mode: 0o600, flag: "wx" });
}

export function safeFailure(error) {
  const failure = {
    code: error?.code ?? "CLAUDE_DEEPSEEK_UNEXPECTED",
    message: error?.message ?? String(error),
  };
  if (error?.details !== null && typeof error?.details === "object" && !Array.isArray(error.details)) {
    failure.details = { ...error.details };
  }
  for (const key of ["reason_code", "diagnostic_id"]) {
    if (typeof error?.[key] === "string") failure[key] = error[key];
  }
  return failure;
}

async function executeOne(options, plan, {
  id = runId(),
  runRoot = path.join(options.runsRoot, id),
  runContractsFirst = true,
  requiredEvidence = plan.goal === FAST_E2E_GOAL
    ? fastRequiredEvidence(plan.scenario)
    : REQUIRED_EVIDENCE[plan.goal],
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
      const common = { runId: id, sourceRoot: REPO_ROOT, claudeEntry: options.claudeEntry, claudeSettings: options.claudeSettings, pythonEntry: options.pythonEntry, cacheRoot: options.cacheRoot, registrationRoot: options.registrationRoot, workRoot, privateRoot, evidenceRoot, usageRoot };
      if (plan.goal === METHODS_GOAL) {
        const methodOptions = { ...common, metaSkillRoot: path.join(REPO_ROOT, ".claude", "skills", "wiki-to-logparse-diagnosis-skill"), wiki: path.join(caseRoot, "input", "wiki.md"), oracle: path.join(caseRoot, "oracle.json"), module: CLAUDE_DEEPSEEK_MODULE };
        if (plan.mode === "cache-verification") verifyMethodsCacheOnly(methodOptions); else await runMethodsBootstrap(methodOptions);
      } else if (plan.goal === FAST_E2E_GOAL) {
        await runFastE2E({
          ...common,
          registrationRoot: plan.inputs.production_registration.registration_root,
          scenario: plan.scenario,
        });
      } else await runE2E({ ...common, sourceSnapshotDigest: options.sourceSnapshotDigest, coreVerdict: options.coreVerdict, scenario: options.scenario });
    } catch (error) { failure = safeFailure(error); }
  }
  if (deterministicRoot !== null && deterministicRoot !== evidenceRoot) {
    try { materializeDeterministicGateEvidence({ stagingRoot: deterministicRoot, evidenceRoot }); }
    catch (error) { failure = safeFailure(error); }
  }
  const gate = sealGate({ goal: plan.goal, mode: plan.mode, evidenceRoot, expectedCalls, modelProcessHardCap: plan.execution.model_process_hard_cap, failure, requiredEvidence });
  const finishedAt = new Date().toISOString();
  const verdict = { schema_version: 1, framework: FRAMEWORK_ID, framework_version: FRAMEWORK_VERSION, run_id: id, goal: plan.goal, mode: plan.mode, scenario: plan.scenario, status: statusOverride ?? gate.status, started_at_utc: startedAt, finished_at_utc: finishedAt, elapsed_seconds: (Date.parse(finishedAt) - Date.parse(startedAt)) / 1000, plan_sha256: plan.plan_sha256, source_snapshot: false, old_cross_job_finalization: false, gate_receipt: { path: "evidence/gate-receipt.json", sha256: sha256File(path.join(evidenceRoot, "gate-receipt.json")) }, model_processes: { expected: expectedCalls, actual: gate.actual_model_processes, retry_count: 0 }, usage: gate.usage, failure: gate.failure, failure_domain: gate.failure ? failureDomain(gate.failure) : null };
  writeJsonExclusive(path.join(runRoot, "verdict.json"), verdict);
  return { verdict, runRoot, exitCode: verdict.status === "PASS" ? 0 : verdict.status === "BLOCKED" ? 2 : 1 };
}

function fastScenarioPlan(plan, scenario) {
  const core = {
    ...plan,
    mode: "fast-e2e",
    scenario,
    scenarios: [scenario],
    execution: {
      ...plan.execution,
      expected_model_processes: fastE2ENormalCallCount(scenario),
      model_process_hard_cap: fastE2ECallHardCap(scenario),
      per_scenario: [{
        scenario_id: scenario,
        expected_model_processes: fastE2ENormalCallCount(scenario),
        model_process_hard_cap: fastE2ECallHardCap(scenario),
        token_cap: scenarioTokenCap(FAST_E2E_GOAL, scenario),
        usd_cap: scenarioUsdCap(FAST_E2E_GOAL, scenario),
      }],
      token_cap: scenarioTokenCap(FAST_E2E_GOAL, scenario),
      usd_cap: scenarioUsdCap(FAST_E2E_GOAL, scenario),
      stage_wall_seconds: CLAUDE_DEEPSEEK_STAGE_WALL_SECONDS,
    },
    evidence: fastRequiredEvidence(scenario),
  };
  delete core.plan_sha256;
  return { ...core, plan_sha256: sha256Bytes(canonicalJson(core)) };
}

async function executeFastSuite(options, plan, {
  executeOneImpl = executeOne,
  runDeterministicGatesImpl = runDeterministicGates,
} = {}) {
  const id = runId().replace(/^claude-deepseek-/, "claude-deepseek-suite-");
  const suiteRoot = path.join(options.runsRoot, id);
  const preflightRoot = path.join(suiteRoot, "evidence", "preflight");
  fs.mkdirSync(preflightRoot, { recursive: true, mode: 0o700 });
  fs.mkdirSync(path.join(suiteRoot, "scenarios"), { mode: 0o700 });
  writeJsonExclusive(path.join(suiteRoot, "plan.json"), plan);
  const startedAt = new Date().toISOString();
  const references = [];
  let engineeringFailure = null;
  let blockedFailure = null;
  if (plan.admission.status !== "READY") blockedFailure = { code: "CLAUDE_DEEPSEEK_PLAN_BLOCKED", blockers: plan.admission.blockers };
  else if (options.allowRealModel !== true) blockedFailure = { code: "CLAUDE_DEEPSEEK_REAL_MODEL_OPT_IN_REQUIRED", message: "Execution with Claude processes requires --allow-real-model" };
  else {
    try { runDeterministicGatesImpl(FAST_E2E_GOAL, preflightRoot); }
    catch (error) { engineeringFailure = safeFailure(error); }
  }

  if (blockedFailure === null && engineeringFailure === null) {
    for (const scenario of plan.scenarios) {
      const scenarioEvidence = fastRequiredEvidence(scenario).filter((name) => name !== "quick-claude-fast-e2e-contracts.tap");
      const result = await executeOneImpl(
        { ...options, scenario, allScenarios: false },
        fastScenarioPlan(plan, scenario),
        {
          id: `${id}-${scenario}`,
          runRoot: path.join(suiteRoot, "scenarios", scenario),
          runContractsFirst: false,
          requiredEvidence: scenarioEvidence,
        },
      );
      const reference = scenarioVerdictReference({
        suiteRoot,
        scenario,
        verdict: result.verdict,
        sha256File,
        modelField: "model_processes",
      });
      references.push(reference);
      const decision = scenarioDecision(result.verdict);
      references.at(-1).failure_domain = decision.failure_domain;
      if (decision.stop) {
        engineeringFailure = { scenario_id: scenario, ...(result.verdict.failure ?? { code: "CLAUDE_DEEPSEEK_FAST_E2E_ENGINEERING_FAILURE" }) };
        break;
      }
    }
  }

  const referenceByScenario = new Map(references.map((item) => [item.scenario_id, item]));
  const scenarios = plan.scenarios.map((scenario) => referenceByScenario.get(scenario) ?? {
    scenario_id: scenario,
    status: "NOT_RUN",
    failure_domain: null,
    model_processes: null,
    usage: null,
    failure: null,
    verdict: null,
  });
  const blocked = blockedFailure !== null;
  const status = suiteStatus({ blocked, engineeringFailure, references, expectedCount: plan.scenarios.length });
  const actualCalls = references.reduce((sum, item) => sum + (item.model_processes?.actual ?? 0), 0);
  const finishedAt = new Date().toISOString();
  const verdict = {
    schema_version: 1,
    framework: FRAMEWORK_ID,
    framework_version: FRAMEWORK_VERSION,
    run_id: id,
    goal: FAST_E2E_GOAL,
    mode: "fast-e2e-suite",
    scenario: null,
    scenario_order: [...plan.scenarios],
    status,
    started_at_utc: startedAt,
    finished_at_utc: finishedAt,
    elapsed_seconds: (Date.parse(finishedAt) - Date.parse(startedAt)) / 1000,
    plan_sha256: plan.plan_sha256,
    source_snapshot: false,
    old_cross_job_finalization: false,
    model_processes: { expected: plan.execution.expected_model_processes, hard_cap: plan.execution.model_process_hard_cap, actual: actualCalls, retry_count: 0 },
    usage: aggregateUsage(references.map((item) => item.usage)),
    summary: {
      expected: plan.scenarios.length,
      completed: references.length,
      passed: references.filter((item) => item.status === "PASS").length,
      failed: references.filter((item) => item.status === "FAIL").length,
      not_run: plan.scenarios.length - references.length,
    },
    scenarios,
    failure: blockedFailure ?? engineeringFailure,
    failure_domain: blocked ? "ADMISSION" : engineeringFailure === null ? null : "ENGINEERING",
    stop_reason: blockedFailure !== null
      ? { domain: "ADMISSION", failure: blockedFailure }
      : engineeringFailure !== null
        ? { domain: "ENGINEERING", failure: engineeringFailure }
        : null,
  };
  writeJsonExclusive(path.join(suiteRoot, "verdict.json"), verdict);
  return { verdict, runRoot: suiteRoot, exitCode: status === "PASS" ? 0 : status === "BLOCKED" ? 2 : 1 };
}

export async function execute(options, plan) {
  return plan.mode === "fast-e2e-suite" ? executeFastSuite(options, plan) : executeOne(options, plan);
}

function usage() { return `Usage:\n  ./tools/test-flow/quick-validation/claude-deepseek/run.sh --goal ${METHODS_GOAL} [--plan-only] [--allow-real-model]\n  ./tools/test-flow/quick-validation/claude-deepseek/run.sh --goal ${FAST_E2E_GOAL} (--scenario <historical-scenario> | --all-scenarios) --registration-root <path> [--plan-only] [--allow-real-model]\n  ./tools/test-flow/quick-validation/claude-deepseek/run.sh --goal ${E2E_GOAL} --registration-root <path> --scenario multiple-rpc-timeouts --source-snapshot-digest <sha256> --core-verdict <path> [--plan-only] [--allow-real-model]\n`; }

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

export { E2E_GOAL, FAST_E2E_GOAL, METHODS_GOAL, REQUIRED_EVIDENCE, executeFastSuite, fastScenarioPlan };
