#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  canonicalJson,
  sha256Bytes,
  sha256File,
  validateCodexLunaIdentity,
} from "../../runtime-support/codex-luna-contract.mjs";
import {
  assertMethodsPackageUnchanged,
  buildMethodsProducerIdentity,
  MACOS_CODEX_LUNA_E2E_TOKEN_LIMIT,
  MACOS_CODEX_LUNA_E2E_USD_LIMIT,
  MACOS_CODEX_LUNA_BLIND_REVIEW_E2E_MAX_CALLS,
  macosCodexLunaE2ECallCount,
  MACOS_CODEX_LUNA_METHODS_CALLS,
  MACOS_CODEX_LUNA_METHODS_TOKEN_LIMIT,
  MACOS_CODEX_LUNA_METHODS_USD_LIMIT,
  MACOS_CODEX_LUNA_STAGE_WALL_SECONDS,
  STANDALONE_CODEX_LUNA_SCENARIOS,
  methodsCachePath,
  scenarioPaths as releaseScenarioPaths,
  validateMethodsCache,
} from "./runtime/macos-codex-luna-e2e-contract.mjs";
import { runE2E } from "./runtime/macos-codex-luna-e2e-runner.mjs";
import { runFastE2E } from "./runtime/macos-codex-luna-fast-e2e-runner.mjs";
import {
  runMethodsBootstrap,
  verifyMethodsCacheOnly,
} from "./runtime/macos-codex-luna-methods-runner.mjs";
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
const FRAMEWORK_ID = "macos-codex-luna-fast-e2e";
const FRAMEWORK_VERSION = 1;
const GOALS = new Set(["methods", "fast-e2e", "e2e"]);
const FLAGS = new Set(["plan-only", "allow-real-model", "all-scenarios", "help"]);
const EVALUATION_MODES = new Set(["SPECIALIST_ONLY", "BLIND_CONSENSUS"]);

const REQUIRED_EVIDENCE = Object.freeze({
  methods: ["codex-identity.json", "model-invocations.json", "model-usage.json", "methods-package.json", "adapter-receipt.json"],
  "fast-e2e": ["runtime-receipt.json", "methods-package.json", "codex-identity.json", "model-invocations.json", "model-usage.json", "methods-source-job.json", "methods-evidence-graph-v2.json", "methods-evaluation-plan-v2.json", "methods-limitations-v2.json", "methods-source-state-v2.json", "methods-source-outcome-v2.json", "methods-terminal-state-v2.json", "methods-result-v2.json", "methods.json", "fast-e2e-oracle.json", "adapter-receipt.json"],
  e2e: ["runtime-receipt.json", "methods-package.json", "codex-identity.json", "model-invocations.json", "model-usage.json", "methods-source-job.json", "methods-reviewer-job.json", "methods-evidence-graph-v2.json", "methods-evaluation-plan-v2.json", "methods-limitations-v2.json", "methods-source-state-v2.json", "methods-source-outcome-v2.json", "methods-terminal-state-v2.json", "methods-reviewer-outcome-v2.json", "methods-result-v2.json", "methods.json", "scenario-oracle-receipt.json", "model-cert-input.json", "model-cert.json", "adapter-receipt.json"],
});

const FAST_E2E_REVIEW_EVIDENCE = Object.freeze([
  ...REQUIRED_EVIDENCE["fast-e2e"],
  "methods-reviewer-job.json",
  "methods-reviewer-outcome-v2.json",
]);

const MODEL_CERT_REVIEW_EVIDENCE = new Set([
  "methods-reviewer-job.json",
  "methods-terminal-state-v2.json",
  "methods-reviewer-outcome-v2.json",
]);

class LunaFlowError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "LunaFlowError";
    this.code = code;
  }
}

function fail(code, message) {
  throw new LunaFlowError(code, message);
}

function writeJsonExclusive(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, `${canonicalJson(value)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
}

export function safeFailure(error) {
  const failure = {
    code: typeof error?.code === "string" ? error.code : "MACOS_CODEX_LUNA_UNEXPECTED",
    message: typeof error?.message === "string" ? error.message : String(error),
  };
  if (error?.details !== null && typeof error?.details === "object" && !Array.isArray(error.details)) {
    failure.details = { ...error.details };
  }
  for (const key of ["reason_code", "diagnostic_id"]) {
    if (typeof error?.[key] === "string") failure[key] = error[key];
  }
  return failure;
}

export function parseArguments(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (!argument.startsWith("--")) fail("LUNA_ARGUMENT_INVALID", "Arguments must use --name value syntax");
    const name = argument.slice(2);
    if (Object.hasOwn(values, name)) fail("LUNA_ARGUMENT_DUPLICATE", `Argument --${name} is duplicated`);
    if (FLAGS.has(name)) values[name] = true;
    else {
      if (index + 1 >= argv.length || argv[index + 1].startsWith("--")) fail("LUNA_ARGUMENT_MISSING", `Argument --${name} requires a value`);
      values[name] = argv[++index];
    }
  }
  if (values.help === true) return values;
  if (!GOALS.has(values.goal)) fail("LUNA_GOAL_INVALID", "--goal must be methods, fast-e2e, or e2e");
  if (values.goal === "fast-e2e" && values.scenario !== undefined && !FAST_E2E_SCENARIOS.includes(values.scenario)) fail("LUNA_SCENARIO_INVALID", "--scenario is not in the historical Fast E2E matrix");
  if (values.goal !== "fast-e2e" && values["all-scenarios"] === true) fail("LUNA_MODEL_CERT_SUITE_FORBIDDEN", "--all-scenarios belongs only to standalone Fast E2E");
  if (values["all-scenarios"] === true && values.scenario !== undefined) fail("LUNA_SCENARIO_SELECTION_CONFLICT", "--all-scenarios and --scenario are mutually exclusive");
  if (values.goal === "e2e" && values.scenario !== undefined && !STANDALONE_CODEX_LUNA_SCENARIOS.includes(values.scenario)) fail("LUNA_SCENARIO_INVALID", "Evidence V2 model-cert uses only its fixed Release scenario");
  if (values.goal === "e2e" && (values.scenario ?? "multiple-rpc-timeouts") !== "multiple-rpc-timeouts") fail("LUNA_SCENARIO_INVALID", "Evidence V2 model-cert uses only multiple-rpc-timeouts");
  if (values["evaluation-mode"] !== undefined && values.goal !== "e2e") fail("LUNA_EVALUATION_MODE_FORBIDDEN", "--evaluation-mode belongs only to Evidence V2 model-cert");
  if (values["evaluation-mode"] !== undefined && !EVALUATION_MODES.has(values["evaluation-mode"])) fail("LUNA_EVALUATION_MODE_INVALID", "--evaluation-mode must be SPECIALIST_ONLY or BLIND_CONSENSUS");
  return values;
}

function defaults(values, environment = process.env) {
  return {
    goal: values.goal,
    scenario: values.scenario ?? (values.goal === "fast-e2e" ? "api-execution-overrun" : "multiple-rpc-timeouts"),
    allScenarios: values["all-scenarios"] === true,
    planOnly: values["plan-only"] === true,
    allowRealModel: values["allow-real-model"] === true,
    codexEntry: path.resolve(values["codex-entry"] ?? "/Applications/ChatGPT.app/Contents/Resources/codex"),
    authSource: path.resolve(values["codex-auth"] ?? path.join(environment.HOME ?? "", ".codex", "auth.json")),
    pythonEntry: path.resolve(values["python-entry"] ?? path.join(REPO_ROOT, ".venv", "bin", "python")),
    cacheRoot: path.resolve(values["cache-root"] ?? path.join(REPO_ROOT, ".tmp", "quick-validation", "codex-luna", "cache")),
    runsRoot: path.resolve(values["runs-root"] ?? path.join(REPO_ROOT, ".tmp", "quick-validation", "codex-luna", "runs")),
    scratchRoot: environment.TEST_FLOW_QUICK_SCRATCH_ROOT ? path.resolve(environment.TEST_FLOW_QUICK_SCRATCH_ROOT) : null,
    logparseRoot: path.resolve(values["logparse-root"] ?? path.join(environment.HOME ?? "", "Documents", "Codex", "2026-06-29-github-issue-locator-logparse", "logparse")),
    registrationRoot: values["registration-root"] ? path.resolve(values["registration-root"]) : null,
    sourceSnapshotDigest: values["source-snapshot-digest"] ?? null,
    coreVerdict: values["core-verdict"] ? path.resolve(values["core-verdict"]) : null,
    evaluationMode: values["evaluation-mode"] ?? "SPECIALIST_ONLY",
    retryContext: { reason: values.reason ?? null, hypothesis: values.hypothesis ?? null, expected_evidence: values["expected-evidence"] ?? null },
  };
}

function requiredFile(filePath, code, label, blockers) {
  try {
    const metadata = fs.statSync(filePath);
    if (!metadata.isFile()) throw new Error("not-file");
  } catch {
    blockers.push({ code, detail: `${label} is unavailable` });
  }
}

function requiredDirectory(directory, code, label, blockers) {
  try {
    const metadata = fs.statSync(directory);
    if (!metadata.isDirectory()) throw new Error("not-directory");
  } catch {
    blockers.push({ code, detail: `${label} is unavailable` });
  }
}

function scenarioCallCount(goal, scenario, evaluationMode = "SPECIALIST_ONLY") {
  return goal === "fast-e2e"
    ? fastE2ENormalCallCount(scenario)
    : macosCodexLunaE2ECallCount(scenario, evaluationMode);
}

function scenarioCallHardCap(goal, scenario, evaluationMode = "SPECIALIST_ONLY") {
  return goal === "fast-e2e"
    ? fastE2ECallHardCap(scenario)
    : evaluationMode === "SPECIALIST_ONLY"
      ? 2
      : MACOS_CODEX_LUNA_BLIND_REVIEW_E2E_MAX_CALLS;
}

function scenarioTokenCap(goal, scenario) {
  return goal === "fast-e2e" && fastE2ENormalCallCount(scenario) === 0
    ? 0
    : MACOS_CODEX_LUNA_E2E_TOKEN_LIMIT;
}

function scenarioCostCap(goal, scenario) {
  return goal === "fast-e2e" && fastE2ENormalCallCount(scenario) === 0
    ? 0
    : MACOS_CODEX_LUNA_E2E_USD_LIMIT;
}

function fastRequiredEvidence(scenario) {
  return scenario === "insufficient-evidence"
    ? REQUIRED_EVIDENCE["fast-e2e"]
    : FAST_E2E_REVIEW_EVIDENCE;
}

function modelCertRequiredEvidence(evaluationMode) {
  return evaluationMode === "BLIND_CONSENSUS"
    ? REQUIRED_EVIDENCE.e2e
    : REQUIRED_EVIDENCE.e2e.filter((name) => !MODEL_CERT_REVIEW_EVIDENCE.has(name));
}

export function buildPlan(options) {
  const blockers = [];
  const platform = standalonePlatform();
  if (platform.status !== "SUPPORTED") blockers.push({ code: "LUNA_HOST_UNSUPPORTED", detail: "Fast E2E requires native macOS arm64 or the explicitly sealed Ubuntu 22.04 Linux/x64 container" });
  if (platform.sealed && options.scratchRoot === null) blockers.push({ code: "LUNA_SCRATCH_ROOT_REQUIRED", detail: "Sealed Linux Fast E2E requires the wrapper-owned tmpfs scratch root" });
  if (options.scratchRoot !== null) {
    requiredDirectory(options.scratchRoot, "LUNA_SCRATCH_ROOT_MISSING", "standalone scratch root", blockers);
    const scratch = path.resolve(options.scratchRoot);
    const runs = path.resolve(options.runsRoot);
    if (scratch === runs || scratch.startsWith(`${runs}${path.sep}`) || runs.startsWith(`${scratch}${path.sep}`)) {
      blockers.push({ code: "LUNA_SCRATCH_ROOT_OVERLAP", detail: "Standalone scratch and persisted runs roots must not overlap" });
    }
  }
  requiredFile(options.codexEntry, "LUNA_CODEX_MISSING", "Codex CLI", blockers);
  requiredFile(options.authSource, "LUNA_AUTH_MISSING", "Codex auth source", blockers);
  requiredFile(options.pythonEntry, "LUNA_PYTHON_MISSING", "validator/service Python", blockers);

  let codexIdentity = null;
  let producer = null;
  let cache = options.goal === "fast-e2e"
    ? { status: "NOT_REQUIRED", code: null, path: null, package_tree_sha256: null }
    : { status: "UNKNOWN", code: null, path: null, package_tree_sha256: null };
  if (blockers.length === 0) {
    try {
      codexIdentity = validateCodexLunaIdentity(options.codexEntry, options.authSource);
      if (options.goal !== "fast-e2e") {
        const metaSkillRoot = path.join(REPO_ROOT, ".agents", "skills", "wiki-to-diagnosis-skill");
        const releaseCaseRoot = path.join(REPO_ROOT, "tests", "cases", "release", "rpc-timeout-anonymized");
        const wiki = path.join(releaseCaseRoot, "input", "wiki.md");
        const registrationTemplate = path.join(releaseCaseRoot, "registration", "rpc-timeout-methods-v1", "registration-template.json");
        producer = buildMethodsProducerIdentity({ wiki, metaSkillRoot, registrationTemplate, codexIdentity });
      }
      if (options.goal === "methods" || (options.goal === "e2e" && options.registrationRoot === null)) {
        const cachePath = methodsCachePath(options.cacheRoot, producer.producer_identity);
        const releaseCaseRoot = path.join(REPO_ROOT, "tests", "cases", "release", "rpc-timeout-anonymized");
        const registrationTemplate = path.join(releaseCaseRoot, "registration", "rpc-timeout-methods-v1", "registration-template.json");
        try {
          const receipt = validateMethodsCache({ cacheRoot: options.cacheRoot, producer, registrationTemplate });
          assertMethodsPackageUnchanged(receipt);
          cache = { status: "PRESENT", code: null, path: cachePath, package_tree_sha256: receipt.manifest.package.tree_sha256 };
        } catch (error) {
          cache = {
            status: !fs.existsSync(cachePath) ? "MISSING" : "INVALID",
            code: error?.code ?? "LUNA_CACHE_INVALID",
            path: cachePath,
            package_tree_sha256: null,
          };
        }
      } else {
        cache = { status: "NOT_REQUIRED", code: null, path: null, package_tree_sha256: null };
      }
    } catch (error) {
      blockers.push({ code: error?.code ?? "LUNA_IDENTITY_INVALID", detail: error?.message ?? "Codex or Methods identity is invalid" });
    }
  }

  const scenarios = options.goal === "fast-e2e"
    ? (options.allScenarios ? [...FAST_E2E_SCENARIOS] : [options.scenario])
    : options.goal === "e2e" ? ["multiple-rpc-timeouts"] : [];
  if (options.goal === "e2e") {
    if (!/^[a-f0-9]{64}$/u.test(options.sourceSnapshotDigest ?? "")) blockers.push({ code: "LUNA_SOURCE_SNAPSHOT_REQUIRED", detail: "Evidence V2 model-cert requires the active source snapshot digest" });
    if (options.coreVerdict === null) blockers.push({ code: "LUNA_CORE_VERDICT_REQUIRED", detail: "Evidence V2 model-cert requires the matching Core verdict" });
    else requiredFile(options.coreVerdict, "LUNA_CORE_VERDICT_MISSING", "Evidence V2 Core verdict", blockers);
    if (options.registrationRoot !== null) requiredDirectory(options.registrationRoot, "LUNA_REGISTRATION_ROOT_MISSING", "validated production registration", blockers);
    for (const scenario of scenarios) {
      try { releaseScenarioPaths(REPO_ROOT, scenario); } catch (error) { blockers.push({ code: error?.code ?? "LUNA_SCENARIO_INVALID", detail: error?.message ?? `Scenario inputs are invalid: ${scenario}` }); }
    }
    if (options.registrationRoot === null && cache.status !== "PRESENT") blockers.push({ code: "LUNA_REGISTRATION_INPUT_REQUIRED", detail: `Model-cert requires --registration-root or the exact Methods cache (${cache.code ?? cache.status})` });
  }
  if (options.goal === "fast-e2e") {
    if (options.registrationRoot !== null) requiredDirectory(options.registrationRoot, "LUNA_REGISTRATION_ROOT_MISSING", "validated production registration", blockers);
    for (const scenario of scenarios) {
      try {
        const paths = fastScenarioPaths(REPO_ROOT, scenario);
        requiredFile(paths.case, "LUNA_FAST_E2E_CASE_MISSING", `Fast E2E case ${scenario}`, blockers);
        requiredFile(paths.client_log, "LUNA_FAST_E2E_CLIENT_LOG_MISSING", `Fast E2E client log ${scenario}`, blockers);
        requiredFile(paths.server_log, "LUNA_FAST_E2E_SERVER_LOG_MISSING", `Fast E2E server log ${scenario}`, blockers);
      } catch (error) {
        blockers.push({ code: "LUNA_SCENARIO_INVALID", detail: error?.message ?? `Fast E2E scenario is invalid: ${scenario}` });
      }
    }
    if (options.registrationRoot === null) blockers.push({ code: "LUNA_FAST_E2E_REGISTRATION_ROOT_REQUIRED", detail: "Fast E2E requires --registration-root for one validated production registration" });
  }
  if (options.goal === "methods" && cache.status === "INVALID") blockers.push({ code: "LUNA_METHODS_CACHE_INVALID", detail: `Exact cache exists but is invalid (${cache.code})` });

  const mode = options.goal === "methods" && cache.status === "PRESENT"
    ? "cache-verification"
    : options.goal === "methods"
      ? "bootstrap"
      : options.goal === "fast-e2e"
        ? (options.allScenarios ? "fast-e2e-suite" : "fast-e2e")
        : "model-cert";
  const expectedCalls = mode === "cache-verification"
    ? 0
    : options.goal === "methods"
      ? MACOS_CODEX_LUNA_METHODS_CALLS
      : expectedSuiteCalls(scenarios, (scenario) => scenarioCallCount(options.goal, scenario, options.evaluationMode));
  const tokenCap = options.goal === "methods"
    ? MACOS_CODEX_LUNA_METHODS_TOKEN_LIMIT
    : scenarios.reduce((sum, scenario) => sum + scenarioTokenCap(options.goal, scenario), 0);
  const costCap = options.goal === "methods"
    ? MACOS_CODEX_LUNA_METHODS_USD_LIMIT
    : scenarios.reduce((sum, scenario) => sum + scenarioCostCap(options.goal, scenario), 0);
  const planCore = {
    schema_version: 1,
    framework: FRAMEWORK_ID,
    framework_version: FRAMEWORK_VERSION,
    goal: options.goal,
    mode,
    evaluation_mode: options.goal === "e2e" ? options.evaluationMode : null,
    scenario: scenarios.length === 1 ? scenarios[0] : null,
    scenarios,
    execution: {
      entry: "tools/test-flow/quick-validation/codex-luna/run.mjs",
      old_test_flow_orchestrator: false,
      source_snapshot: options.goal === "e2e",
      history_reuse: false,
      automatic_retry: false,
      security_and_permission_proof: false,
      platform,
      expected_model_calls: expectedCalls,
      model_call_hard_cap: ["fast-e2e", "e2e"].includes(options.goal)
        ? scenarios.reduce((sum, scenario) => sum + scenarioCallHardCap(options.goal, scenario, options.evaluationMode), 0)
        : expectedCalls,
      per_scenario: scenarios.map((scenario) => ({ scenario_id: scenario, expected_model_calls: scenarioCallCount(options.goal, scenario, options.evaluationMode), model_call_hard_cap: scenarioCallHardCap(options.goal, scenario, options.evaluationMode), token_cap: scenarioTokenCap(options.goal, scenario), equivalent_usd_cap: scenarioCostCap(options.goal, scenario) })),
      model: "gpt-5.6-luna",
      reasoning_effort: "medium",
      token_cap: tokenCap,
      equivalent_usd_cap: costCap,
      wall_timeout_seconds: ["fast-e2e", "e2e"].includes(options.goal)
        ? MACOS_CODEX_LUNA_STAGE_WALL_SECONDS * scenarios.length
        : MACOS_CODEX_LUNA_STAGE_WALL_SECONDS,
      per_call_timeout_seconds: 600,
      no_progress_seconds: 180,
    },
    inputs: {
      repository_root: REPO_ROOT,
      scratch_root: options.scratchRoot,
      codex: codexIdentity,
      producer,
      methods_cache: cache,
      registration_root: ["fast-e2e", "e2e"].includes(options.goal) ? options.registrationRoot : null,
      source_snapshot_digest: options.goal === "e2e" ? options.sourceSnapshotDigest : null,
      core_verdict: options.goal === "e2e" ? options.coreVerdict : null,
      python_entry: options.pythonEntry,
      retry_context: options.retryContext,
    },
    evidence: options.goal === "fast-e2e" && scenarios.length === 1
      ? fastRequiredEvidence(scenarios[0])
      : options.goal === "e2e" ? modelCertRequiredEvidence(options.evaluationMode) : REQUIRED_EVIDENCE[options.goal],
    admission: { status: blockers.length === 0 ? "READY" : "BLOCKED", blockers },
  };
  return { ...planCore, plan_sha256: sha256Bytes(canonicalJson(planCore)) };
}

function runId() {
  const timestamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
  return `luna-${timestamp}-${crypto.randomBytes(4).toString("hex")}`;
}

function evidenceManifest(evidenceRoot) {
  if (!fs.existsSync(evidenceRoot)) return [];
  return fs.readdirSync(evidenceRoot, { withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => {
      const filePath = path.join(evidenceRoot, entry.name);
      return { name: entry.name, size: fs.statSync(filePath).size, sha256: sha256File(filePath) };
    })
    .sort((left, right) => left.name.localeCompare(right.name));
}

export function sealLightGate({ goal, mode, evidenceRoot, expectedCalls, modelCallHardCap = null, failure = null, requiredEvidence = REQUIRED_EVIDENCE[goal] }) {
  const manifestBefore = evidenceManifest(evidenceRoot);
  const names = new Set(manifestBefore.map((entry) => entry.name));
  const missing = requiredEvidence.filter((name) => !names.has(name));
  let invocationCount = null;
  let usage = null;
  if (names.has("model-invocations.json")) {
    const ledger = JSON.parse(fs.readFileSync(path.join(evidenceRoot, "model-invocations.json"), "utf8"));
    invocationCount = Array.isArray(ledger.invocations) ? ledger.invocations.length : null;
  }
  if (names.has("model-usage.json")) usage = JSON.parse(fs.readFileSync(path.join(evidenceRoot, "model-usage.json"), "utf8")).aggregate ?? null;
  const adapterPass = names.has("adapter-receipt.json") && JSON.parse(fs.readFileSync(path.join(evidenceRoot, "adapter-receipt.json"), "utf8")).status === "PASS";
  const roleEvaluationGoal = goal === "e2e" || goal === "fast-e2e";
  const hardCap = modelCallHardCap
    ?? (roleEvaluationGoal ? MACOS_CODEX_LUNA_BLIND_REVIEW_E2E_MAX_CALLS : expectedCalls);
  const callCountPass = roleEvaluationGoal
    ? Number.isSafeInteger(invocationCount) && invocationCount >= expectedCalls && invocationCount <= hardCap
    : invocationCount === expectedCalls;
  const status = failure === null && missing.length === 0 && callCountPass && adapterPass ? "PASS" : "FAIL";
  const receipt = {
    schema_version: 1,
    framework: FRAMEWORK_ID,
    goal,
    mode,
    status,
    expected_model_calls: expectedCalls,
    model_call_hard_cap: hardCap,
    actual_model_calls: invocationCount,
    retry_count: 0,
    missing_evidence: missing,
    usage,
    failure,
    evidence: manifestBefore,
  };
  writeJsonExclusive(path.join(evidenceRoot, "gate-receipt.json"), receipt);
  return receipt;
}

function lightVerdict({ runId: id, plan, gate, startedAt, finishedAt, runRoot, statusOverride = null }) {
  const status = statusOverride ?? gate.status;
  return {
    schema_version: 1,
    framework: FRAMEWORK_ID,
    framework_version: FRAMEWORK_VERSION,
    run_id: id,
    goal: plan.goal,
    mode: plan.mode,
    scenario: plan.scenario,
    status,
    started_at_utc: startedAt,
    finished_at_utc: finishedAt,
    elapsed_seconds: Math.max(0, (Date.parse(finishedAt) - Date.parse(startedAt)) / 1000),
    plan_sha256: plan.plan_sha256,
    source_snapshot: false,
    old_test_flow_finalization: false,
    gate_receipt: gate ? { path: "evidence/gate-receipt.json", sha256: sha256File(path.join(runRoot, "evidence", "gate-receipt.json")) } : null,
    model_calls: gate ? { expected: gate.expected_model_calls, actual: gate.actual_model_calls, retry_count: gate.retry_count } : null,
    usage: gate?.usage ?? null,
    failure: gate?.failure ?? (status === "BLOCKED" ? { code: "LUNA_PLAN_BLOCKED", blockers: plan.admission.blockers } : null),
    failure_domain: gate?.failure ? failureDomain(gate.failure) : null,
  };
}

function progressReporter() {
  let last = 0;
  return (activity) => {
    const now = Date.now();
    if (now - last < 5000) return;
    last = now;
    process.stderr.write(`LUNA_PROGRESS ${activity}\n`);
  };
}

async function executeOne(options, plan, { id = runId(), runRoot = path.join(options.runsRoot, id) } = {}) {
  const roots = standaloneScenarioRoots({ runRoot, runId: id, scratchRoot: options.scratchRoot });
  const { scratch_run_root: scratchRunRoot, work_root: workRoot, private_root: privateRoot, evidence_root: evidenceRoot, usage_root: usageRoot } = roots;
  fs.mkdirSync(runRoot, { recursive: false, mode: 0o700 });
  if (scratchRunRoot !== runRoot) fs.mkdirSync(scratchRunRoot, { recursive: false, mode: 0o700 });
  for (const directory of [workRoot, privateRoot, evidenceRoot, usageRoot]) fs.mkdirSync(directory, { mode: 0o700 });
  writeJsonExclusive(path.join(runRoot, "plan.json"), plan);
  const startedAt = new Date().toISOString();
  const expectedCalls = plan.execution.expected_model_calls;
  if (plan.admission.status !== "READY") {
    const gate = sealLightGate({ goal: plan.goal, mode: plan.mode, evidenceRoot, expectedCalls, modelCallHardCap: plan.execution.model_call_hard_cap, failure: { code: "LUNA_PLAN_BLOCKED", blockers: plan.admission.blockers }, requiredEvidence: plan.evidence });
    const verdict = lightVerdict({ runId: id, plan, gate, startedAt, finishedAt: new Date().toISOString(), runRoot, statusOverride: "BLOCKED" });
    writeJsonExclusive(path.join(runRoot, "verdict.json"), verdict);
    return { verdict, runRoot, exitCode: 2 };
  }
  if ((expectedCalls > 0 || plan.goal === "fast-e2e") && options.allowRealModel !== true) {
    const gate = sealLightGate({ goal: plan.goal, mode: plan.mode, evidenceRoot, expectedCalls, modelCallHardCap: plan.execution.model_call_hard_cap, failure: { code: "LUNA_REAL_MODEL_OPT_IN_REQUIRED", message: "Execution with model calls requires --allow-real-model" }, requiredEvidence: plan.evidence });
    const verdict = lightVerdict({ runId: id, plan, gate, startedAt, finishedAt: new Date().toISOString(), runRoot, statusOverride: "BLOCKED" });
    writeJsonExclusive(path.join(runRoot, "verdict.json"), verdict);
    return { verdict, runRoot, exitCode: 2 };
  }

  const common = {
    runId: id,
    codexEntry: options.codexEntry,
    authSource: options.authSource,
    pythonEntry: options.pythonEntry,
    cacheRoot: options.cacheRoot,
    registrationRoot: options.registrationRoot,
    sourceSnapshotDigest: options.sourceSnapshotDigest,
    coreVerdict: options.coreVerdict,
    evaluationMode: options.evaluationMode,
    workRoot,
    privateRoot,
    evidenceRoot,
    usageRoot,
  };
  let failure = null;
  try {
    if (plan.goal === "methods") {
      const releaseCaseRoot = path.join(REPO_ROOT, "tests", "cases", "release", "rpc-timeout-anonymized");
      const methodsOptions = {
        ...common,
        metaSkillRoot: path.join(REPO_ROOT, ".agents", "skills", "wiki-to-diagnosis-skill"),
        wiki: path.join(releaseCaseRoot, "input", "wiki.md"),
        registrationTemplate: path.join(releaseCaseRoot, "registration", "rpc-timeout-methods-v1", "registration-template.json"),
      };
      if (plan.mode === "cache-verification") verifyMethodsCacheOnly(methodsOptions);
      else await runMethodsBootstrap(methodsOptions, { onProgress: progressReporter() });
    } else if (plan.goal === "fast-e2e") {
      await runFastE2E({
        ...common,
        sourceRoot: REPO_ROOT,
        scenario: plan.scenario,
      }, { onProgress: progressReporter() });
    } else {
      await runE2E({
        ...common,
        sourceRoot: REPO_ROOT,
        scenario: "multiple-rpc-timeouts",
        evaluationMode: options.evaluationMode,
      }, { onProgress: progressReporter() });
    }
  } catch (error) {
    failure = safeFailure(error);
  }
  let gate;
  try {
    gate = sealLightGate({ goal: plan.goal, mode: plan.mode, evidenceRoot, expectedCalls, modelCallHardCap: plan.execution.model_call_hard_cap, failure, requiredEvidence: plan.evidence });
  } catch (error) {
    failure = safeFailure(error);
    gate = sealLightGate({ goal: plan.goal, mode: plan.mode, evidenceRoot, expectedCalls, modelCallHardCap: plan.execution.model_call_hard_cap, failure, requiredEvidence: plan.evidence });
  }
  const verdict = lightVerdict({ runId: id, plan, gate, startedAt, finishedAt: new Date().toISOString(), runRoot });
  writeJsonExclusive(path.join(runRoot, "verdict.json"), verdict);
  return { verdict, runRoot, exitCode: verdict.status === "PASS" ? 0 : 1 };
}

function fastScenarioPlan(plan, scenario) {
  const core = {
    ...plan,
    mode: "fast-e2e",
    scenario,
    scenarios: [scenario],
    evidence: fastRequiredEvidence(scenario),
    execution: {
      ...plan.execution,
      expected_model_calls: fastE2ENormalCallCount(scenario),
      model_call_hard_cap: fastE2ECallHardCap(scenario),
      per_scenario: [{
        scenario_id: scenario,
        expected_model_calls: fastE2ENormalCallCount(scenario),
        model_call_hard_cap: fastE2ECallHardCap(scenario),
        token_cap: scenarioTokenCap("fast-e2e", scenario),
        equivalent_usd_cap: scenarioCostCap("fast-e2e", scenario),
      }],
      token_cap: scenarioTokenCap("fast-e2e", scenario),
      equivalent_usd_cap: scenarioCostCap("fast-e2e", scenario),
      wall_timeout_seconds: MACOS_CODEX_LUNA_STAGE_WALL_SECONDS,
    },
  };
  delete core.plan_sha256;
  return { ...core, plan_sha256: sha256Bytes(canonicalJson(core)) };
}

async function executeFastSuite(options, plan) {
  const id = `luna-suite-${new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z")}-${crypto.randomBytes(4).toString("hex")}`;
  const suiteRoot = path.join(options.runsRoot, id);
  fs.mkdirSync(suiteRoot, { recursive: false, mode: 0o700 });
  fs.mkdirSync(path.join(suiteRoot, "scenarios"), { mode: 0o700 });
  writeJsonExclusive(path.join(suiteRoot, "plan.json"), plan);
  const startedAt = new Date().toISOString();
  const references = [];
  let engineeringFailure = null;

  if (plan.admission.status === "READY" && options.allowRealModel === true) {
    for (const scenario of plan.scenarios) {
      const result = await executeOne(
        { ...options, scenario, allScenarios: false },
        fastScenarioPlan(plan, scenario),
        {
          id: `${id}-${scenario}`,
          runRoot: path.join(suiteRoot, "scenarios", scenario),
        },
      );
      const reference = scenarioVerdictReference({
        suiteRoot,
        scenario,
        verdict: result.verdict,
        sha256File,
      });
      references.push(reference);
      const decision = scenarioDecision(result.verdict);
      if (decision.stop) {
        engineeringFailure = result.verdict.failure ?? {
          code: "LUNA_FAST_E2E_ENGINEERING_FAILURE",
          scenario,
        };
        break;
      }
    }
  }

  const blocked = plan.admission.status !== "READY" || options.allowRealModel !== true;
  const status = suiteStatus({
    blocked,
    engineeringFailure,
    references,
    expectedCount: plan.scenarios.length,
  });
  const actualCalls = references.reduce(
    (sum, item) => sum + (item.model_calls?.actual ?? 0),
    0,
  );
  const verdict = {
    schema_version: 1,
    framework: FRAMEWORK_ID,
    framework_version: FRAMEWORK_VERSION,
    run_id: id,
    goal: "fast-e2e",
    mode: "fast-e2e-suite",
    scenario: null,
    scenarios: references,
    status,
    started_at_utc: startedAt,
    finished_at_utc: new Date().toISOString(),
    plan_sha256: plan.plan_sha256,
    source_snapshot: false,
    old_test_flow_finalization: false,
    model_calls: {
      expected: plan.execution.expected_model_calls,
      hard_cap: plan.execution.model_call_hard_cap,
      actual: actualCalls,
      retry_count: 0,
    },
    usage: aggregateUsage(references.map((item) => item.usage)),
    failure: blocked
      ? {
          code: plan.admission.status !== "READY"
            ? "LUNA_PLAN_BLOCKED"
            : "LUNA_REAL_MODEL_OPT_IN_REQUIRED",
          blockers: plan.admission.blockers,
        }
      : engineeringFailure,
    failure_domain: engineeringFailure === null ? null : "ENGINEERING",
  };
  writeJsonExclusive(path.join(suiteRoot, "verdict.json"), verdict);
  return {
    verdict,
    runRoot: suiteRoot,
    exitCode: status === "PASS" ? 0 : status === "BLOCKED" ? 2 : 1,
  };
}

export async function execute(options, plan) {
  return plan.mode === "fast-e2e-suite"
    ? executeFastSuite(options, plan)
    : executeOne(options, plan);
}

function usage() {
  return `Usage:\n  ./tools/test-flow/quick-validation/codex-luna/run.sh --goal methods [--plan-only] [--allow-real-model]\n  ./tools/test-flow/quick-validation/codex-luna/run.sh --goal fast-e2e (--scenario <historical-scenario> | --all-scenarios) --registration-root <path> [--plan-only] [--allow-real-model]\n  ./tools/test-flow/quick-validation/codex-luna/run.sh --goal e2e --scenario multiple-rpc-timeouts --source-snapshot-digest <sha256> --core-verdict <path> (--registration-root <path> | --cache-root <path>) [--evaluation-mode SPECIALIST_ONLY|BLIND_CONSENSUS] [--plan-only] [--allow-real-model]\n\nFast E2E writes lightweight standalone verdicts. The e2e goal remains the formal P2 model-cert probe.\n`;
}

async function main() {
  let parsed;
  try { parsed = parseArguments(process.argv.slice(2)); } catch (error) {
    process.stderr.write(`${canonicalJson({ status: "ERROR", failure: safeFailure(error) })}\n`);
    process.exitCode = 2;
    return;
  }
  if (parsed.help === true) {
    process.stdout.write(usage());
    return;
  }
  const options = defaults(parsed);
  const plan = buildPlan(options);
  if (options.planOnly) {
    process.stdout.write(`${canonicalJson(plan)}\n`);
    return;
  }
  fs.mkdirSync(options.runsRoot, { recursive: true, mode: 0o700 });
  const result = await execute(options, plan);
  process.stdout.write(`${canonicalJson({ run_id: result.verdict.run_id, status: result.verdict.status, verdict: path.join(result.runRoot, "verdict.json") })}\n`);
  process.exitCode = result.exitCode;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();

export { defaults, evidenceManifest, lightVerdict, REQUIRED_EVIDENCE };
