#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
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
  macosCodexLunaE2ECallCount,
  MACOS_CODEX_LUNA_METHODS_CALLS,
  MACOS_CODEX_LUNA_METHODS_TOKEN_LIMIT,
  MACOS_CODEX_LUNA_METHODS_USD_LIMIT,
  MACOS_CODEX_LUNA_STAGE_WALL_SECONDS,
  STANDALONE_CODEX_LUNA_SCENARIOS,
  methodsCachePath,
  scenarioPaths,
  validateMethodsCache,
} from "./runtime/macos-codex-luna-e2e-contract.mjs";
import { runE2E } from "./runtime/macos-codex-luna-e2e-runner.mjs";
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

const SCRIPT_ROOT = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(SCRIPT_ROOT, "..", "..", "..", "..");
const FRAMEWORK_ID = "macos-codex-luna-fast-e2e";
const FRAMEWORK_VERSION = 1;
const GOALS = new Set(["methods", "e2e"]);
const FLAGS = new Set(["plan-only", "allow-real-model", "all-scenarios", "help"]);
const EVIDENCE_V2_REAL_DIAGNOSIS_BLOCKER = Object.freeze({
  code: "EVIDENCE_V2_REAL_DIAGNOSIS_ADAPTER_UNMIGRATED",
  detail: "Standalone Codex/Luna E2E 仍消费旧版 Methods V1 定位产物，迁移完成前禁止调用真实模型。",
});

const REQUIRED_EVIDENCE = Object.freeze({
  methods: ["codex-identity.json", "model-invocations.json", "model-usage.json", "methods-package.json", "adapter-receipt.json"],
  e2e: ["scenario-input.json", "scenario-oracle.json", "methods-package.json", "codex-identity.json", "model-invocations.json", "model-usage.json", "client-events.jsonl", "mcp-tool-calls.json", "attachment.json", "server-events.ndjson", "final-case.json", "artifact-index.json", "http-boundary-audit.json", "adapter-receipt.json"],
});

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

function safeFailure(error) {
  const failure = {
    code: typeof error?.code === "string" ? error.code : "MACOS_CODEX_LUNA_UNEXPECTED",
    message: typeof error?.message === "string" ? error.message : String(error),
  };
  if (error?.details !== null && typeof error?.details === "object" && !Array.isArray(error.details)) {
    const details = {};
    for (const key of ["id", "method", "field", "marker", "term", "function_name", "response_code", "response_message", "codex_error_info", "http_status_code", "will_retry"]) {
      const value = error.details[key];
      if (typeof value === "string" || Number.isSafeInteger(value) || value === null) details[key] = value;
    }
    if (Object.keys(details).length > 0) failure.details = details;
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
  if (!GOALS.has(values.goal)) fail("LUNA_GOAL_INVALID", "--goal must be methods or e2e");
  if (values.scenario !== undefined && !STANDALONE_CODEX_LUNA_SCENARIOS.includes(values.scenario)) fail("LUNA_SCENARIO_INVALID", "--scenario is not in the repository-owned standalone matrix");
  if (values["all-scenarios"] === true && values.scenario !== undefined) fail("LUNA_SCENARIO_SELECTION_CONFLICT", "--all-scenarios and --scenario are mutually exclusive");
  if (values["all-scenarios"] === true && values.goal !== "e2e") fail("LUNA_SUITE_GOAL_INVALID", "--all-scenarios requires --goal e2e");
  return values;
}

function defaults(values, environment = process.env) {
  return {
    goal: values.goal,
    scenario: values.scenario ?? "api-execution-overrun",
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

  const metaSkillRoot = path.join(REPO_ROOT, ".agents", "skills", "wiki-to-diagnosis-skill");
  const releaseCaseRoot = path.join(REPO_ROOT, "tests", "cases", "release", "rpc-timeout-anonymized");
  const wiki = path.join(releaseCaseRoot, "input", "wiki.md");
  const registrationTemplate = path.join(releaseCaseRoot, "registration", "rpc-timeout-methods-v1", "registration-template.json");
  let codexIdentity = null;
  let producer = null;
  let cache = { status: "UNKNOWN", code: null, path: null, package_tree_sha256: null };
  if (blockers.length === 0) {
    try {
      codexIdentity = validateCodexLunaIdentity(options.codexEntry, options.authSource);
      producer = buildMethodsProducerIdentity({ wiki, metaSkillRoot, registrationTemplate, codexIdentity });
      const cachePath = methodsCachePath(options.cacheRoot, producer.producer_identity);
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
    } catch (error) {
      blockers.push({ code: error?.code ?? "LUNA_IDENTITY_INVALID", detail: error?.message ?? "Codex or Methods identity is invalid" });
    }
  }

  const scenarios = options.goal === "e2e"
    ? options.allScenarios ? [...STANDALONE_CODEX_LUNA_SCENARIOS] : [options.scenario]
    : [];
  if (options.goal === "e2e") {
    blockers.push(EVIDENCE_V2_REAL_DIAGNOSIS_BLOCKER);
    requiredDirectory(options.logparseRoot, "LUNA_LOGPARSE_MISSING", "Logparse source", blockers);
    for (const scenario of scenarios) {
      try { scenarioPaths(REPO_ROOT, scenario); } catch (error) { blockers.push({ code: error?.code ?? "LUNA_SCENARIO_INVALID", detail: error?.message ?? `Scenario inputs are invalid: ${scenario}` }); }
    }
    if (cache.status !== "PRESENT") blockers.push({ code: "LUNA_METHODS_CACHE_REQUIRED", detail: `E2E requires the exact Methods cache (${cache.code ?? cache.status})` });
  }
  if (options.goal === "methods" && cache.status === "INVALID") blockers.push({ code: "LUNA_METHODS_CACHE_INVALID", detail: `Exact cache exists but is invalid (${cache.code})` });

  const mode = options.goal === "methods" && cache.status === "PRESENT" ? "cache-verification" : options.goal === "methods" ? "bootstrap" : options.allScenarios ? "e2e-suite" : "e2e";
  const expectedCalls = mode === "cache-verification" ? 0 : options.goal === "methods" ? MACOS_CODEX_LUNA_METHODS_CALLS : expectedSuiteCalls(scenarios, macosCodexLunaE2ECallCount);
  const tokenCap = options.goal === "methods" ? MACOS_CODEX_LUNA_METHODS_TOKEN_LIMIT : MACOS_CODEX_LUNA_E2E_TOKEN_LIMIT * scenarios.length;
  const costCap = options.goal === "methods" ? MACOS_CODEX_LUNA_METHODS_USD_LIMIT : MACOS_CODEX_LUNA_E2E_USD_LIMIT * scenarios.length;
  const planCore = {
    schema_version: 1,
    framework: FRAMEWORK_ID,
    framework_version: FRAMEWORK_VERSION,
    goal: options.goal,
    mode,
    scenario: options.goal === "e2e" && !options.allScenarios ? options.scenario : null,
    scenarios,
    execution: {
      entry: "tools/test-flow/quick-validation/codex-luna/run.mjs",
      old_test_flow_orchestrator: false,
      source_snapshot: false,
      history_reuse: false,
      automatic_retry: false,
      security_and_permission_proof: false,
      platform,
      expected_model_calls: expectedCalls,
      per_scenario: scenarios.map((scenario) => ({ scenario_id: scenario, expected_model_calls: macosCodexLunaE2ECallCount(scenario), token_cap: MACOS_CODEX_LUNA_E2E_TOKEN_LIMIT, equivalent_usd_cap: MACOS_CODEX_LUNA_E2E_USD_LIMIT })),
      model: "gpt-5.6-luna",
      reasoning_effort: "medium",
      token_cap: tokenCap,
      equivalent_usd_cap: costCap,
      wall_timeout_seconds: options.goal === "e2e" ? MACOS_CODEX_LUNA_STAGE_WALL_SECONDS * scenarios.length : MACOS_CODEX_LUNA_STAGE_WALL_SECONDS,
      per_call_timeout_seconds: 600,
      no_progress_seconds: 180,
    },
    inputs: {
      repository_root: REPO_ROOT,
      scratch_root: options.scratchRoot,
      codex: codexIdentity,
      producer,
      methods_cache: cache,
      python_entry: options.pythonEntry,
      logparse_root: options.goal === "e2e" ? options.logparseRoot : null,
      retry_context: options.retryContext,
    },
    evidence: REQUIRED_EVIDENCE[options.goal],
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

export function sealLightGate({ goal, mode, evidenceRoot, expectedCalls, failure = null }) {
  const manifestBefore = evidenceManifest(evidenceRoot);
  const names = new Set(manifestBefore.map((entry) => entry.name));
  const missing = REQUIRED_EVIDENCE[goal].filter((name) => !names.has(name));
  let invocationCount = null;
  let usage = null;
  if (names.has("model-invocations.json")) {
    const ledger = JSON.parse(fs.readFileSync(path.join(evidenceRoot, "model-invocations.json"), "utf8"));
    invocationCount = Array.isArray(ledger.invocations) ? ledger.invocations.length : null;
  }
  if (names.has("model-usage.json")) usage = JSON.parse(fs.readFileSync(path.join(evidenceRoot, "model-usage.json"), "utf8")).aggregate ?? null;
  const adapterPass = names.has("adapter-receipt.json") && JSON.parse(fs.readFileSync(path.join(evidenceRoot, "adapter-receipt.json"), "utf8")).status === "PASS";
  const status = failure === null && missing.length === 0 && invocationCount === expectedCalls && adapterPass ? "PASS" : "FAIL";
  const receipt = {
    schema_version: 1,
    framework: FRAMEWORK_ID,
    goal,
    mode,
    status,
    expected_model_calls: expectedCalls,
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

function runSuiteContracts(destination) {
  const files = [
    "tools/test-flow/quick-validation/standalone-suite.test.mjs",
    ...fs.readdirSync(path.join(REPO_ROOT, "tools", "test-flow", "quick-validation", "codex-luna", "tests"))
      .filter((name) => name.endsWith(".test.mjs"))
      .map((name) => path.join("tools", "test-flow", "quick-validation", "codex-luna", "tests", name)),
  ];
  const result = spawnSync(process.execPath, ["--test", "--test-reporter=tap", ...files], { cwd: REPO_ROOT, env: process.env, encoding: "utf8", timeout: 180_000, stdio: ["ignore", "pipe", "pipe"] });
  fs.writeFileSync(destination, `${result.stdout}${result.stderr}`, { encoding: "utf8", mode: 0o600, flag: "wx" });
  if (result.status !== 0 || result.signal !== null || result.error) fail("LUNA_SUITE_CONTRACTS_FAILED", "Codex standalone contract preflight failed");
}

function scenarioPlan(plan, scenario) {
  const core = {
    ...plan,
    mode: "e2e",
    scenario,
    scenarios: [scenario],
    execution: {
      ...plan.execution,
      expected_model_calls: macosCodexLunaE2ECallCount(scenario),
      token_cap: MACOS_CODEX_LUNA_E2E_TOKEN_LIMIT,
      equivalent_usd_cap: MACOS_CODEX_LUNA_E2E_USD_LIMIT,
      per_scenario: [{ scenario_id: scenario, expected_model_calls: macosCodexLunaE2ECallCount(scenario), token_cap: MACOS_CODEX_LUNA_E2E_TOKEN_LIMIT, equivalent_usd_cap: MACOS_CODEX_LUNA_E2E_USD_LIMIT }],
      wall_timeout_seconds: MACOS_CODEX_LUNA_STAGE_WALL_SECONDS,
    },
  };
  delete core.plan_sha256;
  return { ...core, plan_sha256: sha256Bytes(canonicalJson(core)) };
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
    const gate = sealLightGate({ goal: plan.goal, mode: plan.mode, evidenceRoot, expectedCalls, failure: { code: "LUNA_PLAN_BLOCKED", blockers: plan.admission.blockers } });
    const verdict = lightVerdict({ runId: id, plan, gate, startedAt, finishedAt: new Date().toISOString(), runRoot, statusOverride: "BLOCKED" });
    writeJsonExclusive(path.join(runRoot, "verdict.json"), verdict);
    return { verdict, runRoot, exitCode: 2 };
  }
  if (expectedCalls > 0 && options.allowRealModel !== true) {
    const gate = sealLightGate({ goal: plan.goal, mode: plan.mode, evidenceRoot, expectedCalls, failure: { code: "LUNA_REAL_MODEL_OPT_IN_REQUIRED", message: "Execution with model calls requires --allow-real-model" } });
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
    } else {
      await runE2E({
        ...common,
        sourceRoot: REPO_ROOT,
        logparseRoot: options.logparseRoot,
        scenario: options.scenario,
        clientSkill: path.join(REPO_ROOT, "tools", "test-flow", "quick-validation", "codex-luna", "fixtures", "client-skill", "problem-locator-client", "SKILL.md"),
        serviceSkill: path.join(REPO_ROOT, "tools", "test-flow", "quick-validation", "codex-luna", "fixtures", "service-skill", "problem-locator-service-agent", "SKILL.md"),
      }, { onProgress: progressReporter() });
    }
  } catch (error) {
    failure = safeFailure(error);
  }
  let gate;
  try {
    gate = sealLightGate({ goal: plan.goal, mode: plan.mode, evidenceRoot, expectedCalls, failure });
  } catch (error) {
    failure = safeFailure(error);
    gate = sealLightGate({ goal: plan.goal, mode: plan.mode, evidenceRoot, expectedCalls, failure });
  }
  const verdict = lightVerdict({ runId: id, plan, gate, startedAt, finishedAt: new Date().toISOString(), runRoot });
  writeJsonExclusive(path.join(runRoot, "verdict.json"), verdict);
  return { verdict, runRoot, exitCode: verdict.status === "PASS" ? 0 : 1 };
}

export async function executeSuite(options, plan, {
  executeOneImpl = executeOne,
  runSuiteContractsImpl = runSuiteContracts,
} = {}) {
  const id = runId().replace(/^luna-/, "luna-suite-");
  const runRoot = path.join(options.runsRoot, id);
  const preflightRoot = path.join(runRoot, "evidence", "preflight");
  fs.mkdirSync(runRoot, { recursive: false, mode: 0o700 });
  fs.mkdirSync(preflightRoot, { recursive: true, mode: 0o700 });
  writeJsonExclusive(path.join(runRoot, "plan.json"), plan);
  const startedAt = new Date().toISOString();
  const references = [];
  let engineeringFailure = null;
  let blockedFailure = null;
  let unsealedScenario = null;
  if (plan.admission.status !== "READY") blockedFailure = { code: "LUNA_PLAN_BLOCKED", blockers: plan.admission.blockers };
  else if (options.allowRealModel !== true) blockedFailure = { code: "LUNA_REAL_MODEL_OPT_IN_REQUIRED", message: "Execution with model calls requires --allow-real-model" };
  else {
    try { runSuiteContractsImpl(path.join(preflightRoot, "quick-codex-contracts.tap")); }
    catch (error) { engineeringFailure = safeFailure(error); }
  }

  if (blockedFailure === null && engineeringFailure === null) {
    fs.mkdirSync(path.join(runRoot, "scenarios"), { mode: 0o700 });
    for (const scenario of plan.scenarios) {
      const childRoot = path.join(runRoot, "scenarios", scenario);
      let child;
      let reference;
      try {
        child = await executeOneImpl({ ...options, scenario, allScenarios: false }, scenarioPlan(plan, scenario), {
          id: `${id}-${scenario}`,
          runRoot: childRoot,
        });
        reference = scenarioVerdictReference({ suiteRoot: runRoot, scenario, verdict: child.verdict, sha256File });
      } catch (error) {
        const failure = safeFailure(error);
        engineeringFailure = { scenario_id: scenario, ...failure };
        unsealedScenario = { scenario_id: scenario, status: "ERROR", failure_domain: "ENGINEERING", model_calls: null, usage: null, failure, verdict: null };
        break;
      }
      references.push(reference);
      if (child.verdict.status === "PASS") continue;
      const decision = scenarioDecision(child.verdict);
      references.at(-1).failure_domain = decision.failure_domain;
      if (decision.stop) {
        engineeringFailure = { scenario_id: scenario, ...(child.verdict.failure ?? { code: "LUNA_SCENARIO_ENGINEERING_FAILURE" }) };
        break;
      }
    }
  }

  const completed = new Set(references.map((item) => item.scenario_id));
  const referencesByScenario = new Map(references.map((item) => [item.scenario_id, item]));
  const scenarioResults = plan.scenarios.map((scenario) => referencesByScenario.get(scenario)
    ?? (unsealedScenario?.scenario_id === scenario
      ? unsealedScenario
      : { scenario_id: scenario, status: "NOT_RUN", failure_domain: null, model_calls: null, usage: null, failure: null, verdict: null }));
  const attemptedCount = completed.size + (unsealedScenario === null ? 0 : 1);
  const status = suiteStatus({ blocked: blockedFailure !== null, engineeringFailure, references, expectedCount: plan.scenarios.length });
  const actualCalls = references.reduce((sum, item) => sum + (Number.isSafeInteger(item.model_calls?.actual) ? item.model_calls.actual : 0), 0);
  const finishedAt = new Date().toISOString();
  const verdict = {
    schema_version: 1,
    framework: FRAMEWORK_ID,
    framework_version: FRAMEWORK_VERSION,
    run_id: id,
    goal: plan.goal,
    mode: "e2e-suite",
    scenario: null,
    scenario_order: plan.scenarios,
    status,
    started_at_utc: startedAt,
    finished_at_utc: finishedAt,
    elapsed_seconds: Math.max(0, (Date.parse(finishedAt) - Date.parse(startedAt)) / 1000),
    plan_sha256: plan.plan_sha256,
    source_snapshot: false,
    old_test_flow_finalization: false,
    model_calls: { expected: plan.execution.expected_model_calls, actual: actualCalls, retry_count: 0 },
    usage: aggregateUsage(references.map((item) => item.usage)),
    preflight_evidence: evidenceManifest(preflightRoot),
    summary: {
      expected: plan.scenarios.length,
      completed: references.length,
      attempted: attemptedCount,
      passed: references.filter((item) => item.status === "PASS").length,
      failed: references.filter((item) => item.status === "FAIL").length,
      errored: unsealedScenario === null ? 0 : 1,
      not_run: plan.scenarios.length - attemptedCount,
    },
    scenarios: scenarioResults,
    failure: blockedFailure ?? engineeringFailure,
    failure_domain: blockedFailure !== null ? "ADMISSION" : engineeringFailure !== null ? "ENGINEERING" : null,
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

function usage() {
  return `Usage:\n  ./tools/test-flow/quick-validation/codex-luna/run.sh --goal methods [--plan-only] [--allow-real-model]\n  ./tools/test-flow/quick-validation/codex-luna/run.sh --goal e2e (--scenario <repository-id> | --all-scenarios) [--plan-only] [--allow-real-model]\n\nThis entry has its own planner and lightweight verdict writer.\n`;
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

export { defaults, evidenceManifest, lightVerdict };
