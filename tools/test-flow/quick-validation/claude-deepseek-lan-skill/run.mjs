#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { canonicalJson, sha256File } from "../../lib/util.mjs";
import {
  aggregateUsage,
  standalonePlatform,
  standaloneScenarioRoots,
} from "../standalone-suite.mjs";
import {
  CLAUDE_DEEPSEEK_MODEL,
  DIAGNOSIS_GOAL,
  DIAGNOSIS_LIMITS,
  DIAGNOSIS_SCENARIOS,
  FIXED_MODULE,
  FRAMEWORK_ID,
  FRAMEWORK_VERSION,
  GENERATION_CALLS,
  GENERATION_GOAL,
  GENERATION_TOKEN_LIMIT,
  GENERATION_USD_LIMIT,
  GOALS,
  auditGeneratedPackage,
  buildProducerIdentity,
  createEmptyRoot,
  currentIdentity,
  generationCachePath,
  safeFailure,
  treeDigest,
  validateGeneratedPackage,
  validateGenerationCache,
  writeJsonExclusive,
} from "./runtime/lan-skill-contract.mjs";
import { runGeneration } from "./runtime/lan-skill-generation.mjs";
import { runDiagnosis } from "./runtime/lan-skill-diagnosis.mjs";


const SCRIPT_ROOT = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(SCRIPT_ROOT, "..", "..", "..", "..");
const FLAGS = new Set(["plan-only", "allow-real-model", "all-scenarios", "help"]);
const VALUES = new Set(["goal", "scenario", "claude-entry", "claude-settings", "python-entry", "cache-root", "runs-root", "reason", "hypothesis", "expected-evidence"]);
const RUNNER_FILES = Object.freeze([
  path.join(SCRIPT_ROOT, "run.mjs"),
  path.join(SCRIPT_ROOT, "runtime", "lan-skill-contract.mjs"),
  path.join(SCRIPT_ROOT, "runtime", "lan-skill-generation.mjs"),
  path.join(SCRIPT_ROOT, "runtime", "lan-skill-diagnosis.mjs"),
  path.join(SCRIPT_ROOT, "runtime", "problem-locator-logparse"),
]);
const GENERATION_EVIDENCE = Object.freeze([
  "claude-identity.json",
  "model-invocations.json",
  "model-usage.json",
  "package-validation-audit.json",
  "generated-skill.json",
  "tool-trace-audit.json",
  "scenario-evaluation-audit.json",
  "security-audit.json",
  "adapter-receipt.json",
]);
const DIAGNOSIS_EVIDENCE = Object.freeze([
  "claude-identity.json",
  "generated-skill-cache.json",
  "scenario-input.json",
  "model-invocations.json",
  "model-usage.json",
  "tool-trace-audit.json",
  "scenario-evaluation-audit.json",
  "adapter-receipt.json",
]);


class RunnerError extends Error {
  constructor(code, message, details = {}) { super(message); this.name = "RunnerError"; this.code = code; this.details = details; }
}
function fail(code, message, details = {}) { throw new RunnerError(code, message, details); }


export function parseArguments(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (!argument.startsWith("--")) fail("LAN_ARGUMENT_INVALID", "Arguments must use --name value syntax");
    const name = argument.slice(2);
    if (!FLAGS.has(name) && !VALUES.has(name)) fail("LAN_ARGUMENT_UNKNOWN", `Unsupported argument --${name}`);
    if (Object.hasOwn(values, name)) fail("LAN_ARGUMENT_DUPLICATE", `Argument --${name} is duplicated`);
    if (FLAGS.has(name)) values[name] = true;
    else {
      if (index + 1 >= argv.length || argv[index + 1].startsWith("--")) fail("LAN_ARGUMENT_MISSING", `Argument --${name} requires a value`);
      values[name] = argv[++index];
    }
  }
  if (values.help === true) return values;
  if (!GOALS.includes(values.goal)) fail("LAN_GOAL_INVALID", `--goal must be ${GENERATION_GOAL} or ${DIAGNOSIS_GOAL}`);
  if (values.scenario !== undefined && !DIAGNOSIS_SCENARIOS.includes(values.scenario)) fail("LAN_SCENARIO_INVALID", "--scenario is outside the LAN diagnosis matrix");
  if (values["all-scenarios"] === true && values.scenario !== undefined) fail("LAN_SCENARIO_CONFLICT", "--scenario and --all-scenarios are mutually exclusive");
  if (values.goal === GENERATION_GOAL && (values.scenario !== undefined || values["all-scenarios"] === true)) fail("LAN_GENERATION_SCENARIO_INVALID", "Generation does not accept diagnosis scenarios");
  if (values.goal === DIAGNOSIS_GOAL && values.scenario === undefined && values["all-scenarios"] !== true) fail("LAN_DIAGNOSIS_SCENARIO_REQUIRED", "Diagnosis requires --scenario or --all-scenarios");
  const retryValues = [values.reason, values.hypothesis, values["expected-evidence"]];
  if (retryValues.some((item) => item !== undefined) && !retryValues.every((item) => typeof item === "string" && item.length > 0)) fail("LAN_RETRY_CONTEXT_INCOMPLETE", "Retry context requires reason, hypothesis, and expected-evidence together");
  return values;
}


export function defaults(values, environment = process.env) {
  return {
    goal: values.goal,
    scenario: values.scenario ?? null,
    allScenarios: values["all-scenarios"] === true,
    planOnly: values["plan-only"] === true,
    allowRealModel: values["allow-real-model"] === true,
    claudeEntry: path.resolve(values["claude-entry"] ?? path.join(REPO_ROOT, ".tmp", "test-flow-cache", "claude", "2.1.89", "package", "cli.js")),
    claudeSettings: path.resolve(values["claude-settings"] ?? path.join(REPO_ROOT, ".tmp", "test-flow-release", "settings.json")),
    pythonEntry: path.resolve(values["python-entry"] ?? path.join(REPO_ROOT, ".venv", "bin", "python")),
    cacheRoot: path.resolve(values["cache-root"] ?? path.join(REPO_ROOT, ".tmp", "quick-validation", "claude-deepseek-lan-skill", "cache")),
    runsRoot: path.resolve(values["runs-root"] ?? path.join(REPO_ROOT, ".tmp", "quick-validation", "claude-deepseek-lan-skill", "runs")),
    scratchRoot: environment.TEST_FLOW_QUICK_SCRATCH_ROOT ? path.resolve(environment.TEST_FLOW_QUICK_SCRATCH_ROOT) : null,
    retryContext: { reason: values.reason ?? null, hypothesis: values.hypothesis ?? null, expected_evidence: values["expected-evidence"] ?? null },
  };
}


function requiredFile(filePath, code, label, blockers) {
  try { if (!fs.statSync(filePath).isFile()) throw new Error("not-file"); } catch { blockers.push({ code, detail: `${label} is unavailable` }); }
}
function requiredDirectory(directory, code, label, blockers) {
  try { if (!fs.statSync(directory).isDirectory()) throw new Error("not-directory"); } catch { blockers.push({ code, detail: `${label} is unavailable` }); }
}


export function buildPlan(options, dependencies = {}) {
  const blockers = [];
  const platform = dependencies.platform ?? standalonePlatform();
  if (platform.status !== "SUPPORTED") blockers.push({ code: "LAN_HOST_UNSUPPORTED", detail: "Standalone Fast E2E requires native macOS arm64 or the sealed Ubuntu 22.04 Linux/x64 container" });
  if (platform.sealed && options.scratchRoot === null) blockers.push({ code: "LAN_SCRATCH_ROOT_REQUIRED", detail: "Sealed Linux requires the wrapper-owned tmpfs scratch root" });
  if (options.scratchRoot !== null) requiredDirectory(options.scratchRoot, "LAN_SCRATCH_ROOT_MISSING", "standalone scratch root", blockers);

  const metaSkillRoot = path.join(REPO_ROOT, ".claude", "skills", "wiki-to-logparse-diagnosis-skill");
  const helperSkillRoot = path.join(REPO_ROOT, ".claude", "skills", "logparse-diagnose");
  const caseRoot = path.join(REPO_ROOT, "tests", "cases", "release", "rpc-timeout-anonymized");
  const wiki = path.join(caseRoot, "input", "wiki.md");
  const oracle = path.join(caseRoot, "oracle.json");
  const clientLog = path.join(caseRoot, "scenarios", "multiple-rpc-timeouts", "client.log");
  const serverLog = path.join(caseRoot, "scenarios", "multiple-rpc-timeouts", "server.log");
  const brokerStub = path.join(SCRIPT_ROOT, "runtime", "problem-locator-logparse");
  requiredFile(options.claudeEntry, "LAN_CLAUDE_ENTRY_MISSING", "Claude Code entry", blockers);
  requiredFile(options.claudeSettings, "LAN_CLAUDE_SETTINGS_MISSING", "Claude settings", blockers);
  requiredFile(options.pythonEntry, "LAN_PYTHON_MISSING", "Python entry", blockers);
  requiredDirectory(metaSkillRoot, "LAN_META_SKILL_MISSING", "LAN Logparse meta Skill", blockers);
  requiredDirectory(helperSkillRoot, "LAN_HELPER_SKILL_MISSING", "installed logparse-diagnose Skill", blockers);
  for (const [target, code, label] of [[wiki, "LAN_WIKI_MISSING", "RPC Wiki"], [oracle, "LAN_ORACLE_MISSING", "RPC oracle"], [clientLog, "LAN_CLIENT_LOG_MISSING", "client fixture log"], [serverLog, "LAN_SERVER_LOG_MISSING", "server fixture log"], [brokerStub, "LAN_BROKER_STUB_MISSING", "broker contract stub"]]) requiredFile(target, code, label, blockers);

  let claudeIdentity = null;
  let producer = null;
  let cache = { status: "UNKNOWN", code: null, path: null, package_tree_sha256: null };
  if (blockers.every((item) => !["LAN_CLAUDE_ENTRY_MISSING", "LAN_CLAUDE_SETTINGS_MISSING", "LAN_META_SKILL_MISSING", "LAN_WIKI_MISSING", "LAN_BROKER_STUB_MISSING"].includes(item.code))) {
    try {
      claudeIdentity = dependencies.claudeIdentity ?? currentIdentity(options.claudeEntry, options.claudeSettings);
      producer = buildProducerIdentity({ wiki, metaSkillRoot, module: FIXED_MODULE, claudeIdentity, runnerFiles: RUNNER_FILES });
      const cachePath = generationCachePath(options.cacheRoot, producer.producer_identity);
      try {
        const receipt = validateGenerationCache({ cacheRoot: options.cacheRoot, producer });
        cache = { status: "PRESENT", code: null, path: cachePath, package_tree_sha256: receipt.manifest.package.tree_sha256 };
      } catch (error) {
        cache = { status: fs.existsSync(cachePath) ? "INVALID" : "MISSING", code: error?.code ?? "LAN_CACHE_INVALID", path: cachePath, package_tree_sha256: null };
      }
    } catch (error) {
      blockers.push({ code: error?.code ?? "LAN_IDENTITY_INVALID", detail: error?.message ?? "Claude or producer identity is invalid" });
    }
  }
  if (options.goal === DIAGNOSIS_GOAL && cache.status !== "PRESENT") blockers.push({ code: "LAN_GENERATION_CACHE_REQUIRED", detail: `Diagnosis requires the exact generation cache (${cache.code ?? cache.status})` });
  if (options.goal === GENERATION_GOAL && cache.status === "INVALID") blockers.push({ code: "LAN_GENERATION_CACHE_INVALID", detail: `Exact producer cache exists but is invalid (${cache.code})` });

  const scenarios = options.goal === DIAGNOSIS_GOAL ? (options.allScenarios ? [...DIAGNOSIS_SCENARIOS] : [options.scenario]) : [];
  const diagnosisBudget = scenarios.reduce((total, selected) => ({ token_limit: total.token_limit + DIAGNOSIS_LIMITS[selected].token_limit, usd_limit: total.usd_limit + DIAGNOSIS_LIMITS[selected].usd_limit }), { token_limit: 0, usd_limit: 0 });
  const mode = options.goal === GENERATION_GOAL ? (cache.status === "PRESENT" ? "cache-verification" : "bootstrap") : options.allScenarios ? "diagnosis-suite" : "diagnosis";
  const expectedCalls = options.goal === GENERATION_GOAL ? (mode === "bootstrap" ? GENERATION_CALLS : 0) : scenarios.length;
  const plan = {
    schema_version: 1,
    framework: FRAMEWORK_ID,
    framework_version: FRAMEWORK_VERSION,
    goal: options.goal,
    mode,
    scenario: options.scenario,
    scenarios,
    platform,
    execution: {
      entry: "tools/test-flow/quick-validation/claude-deepseek-lan-skill/run.sh",
      central_test_flow: false,
      release_claim: false,
      model: CLAUDE_DEEPSEEK_MODEL,
      expected_model_processes: expectedCalls,
      retry_policy: "NONE",
      retry_count: 0,
      token_cap: options.goal === GENERATION_GOAL ? GENERATION_TOKEN_LIMIT : diagnosisBudget.token_limit,
      usd_cap: options.goal === GENERATION_GOAL ? GENERATION_USD_LIMIT : diagnosisBudget.usd_limit,
    },
    inputs: {
      meta_skill_root: metaSkillRoot,
      helper_skill_root: helperSkillRoot,
      helper_skill_tree_sha256: fs.existsSync(helperSkillRoot) && fs.statSync(helperSkillRoot).isDirectory() ? treeDigest(helperSkillRoot) : null,
      wiki,
      oracle,
      module: FIXED_MODULE,
      broker_backend: "repository-contract-stub",
      broker_stub_sha256: fs.existsSync(brokerStub) && fs.statSync(brokerStub).isFile() ? sha256File(brokerStub) : null,
      claude: claudeIdentity,
      producer,
      generation_cache: cache,
      retry_context: options.retryContext,
    },
    admission: { status: blockers.length === 0 ? "READY" : "BLOCKED", blockers },
  };
  return { ...plan, plan_sha256: crypto.createHash("sha256").update(canonicalJson(plan).trimEnd()).digest("hex"), paths: { metaSkillRoot, helperSkillRoot, wiki, oracle, clientLog, serverLog, brokerStub }, claudeIdentity, producer, cache };
}


function runId(prefix) {
  return `${prefix}-${new Date().toISOString().replace(/[-:]/gu, "").replace(/\.\d{3}Z$/u, "Z")}-${crypto.randomBytes(4).toString("hex")}`;
}


function writePlan(runRoot, plan) {
  fs.mkdirSync(runRoot, { recursive: true, mode: 0o700 });
  writeJsonExclusive(path.join(runRoot, "plan.json"), Object.fromEntries(Object.entries(plan).filter(([key]) => !["paths", "claudeIdentity", "producer", "cache"].includes(key))));
}


function runDeterministicPreflight(options, outputPath) {
  const nodeTests = [
    path.join(SCRIPT_ROOT, "tests", "lan-skill-contract.test.mjs"),
    path.join(SCRIPT_ROOT, "tests", "lan-skill-runner.test.mjs"),
    path.join(REPO_ROOT, "tools", "test-flow", "quick-validation", "claude-deepseek", "tests", "claude-deepseek-process.test.mjs"),
  ];
  const node = spawnSync(process.execPath, ["--test", ...nodeTests], { cwd: REPO_ROOT, encoding: "utf8", timeout: 120_000 });
  const python = spawnSync(options.pythonEntry, ["-I", "-B", "-m", "pytest", "-q", "tests/deterministic/unit/integrations/test_lan_logparse_meta_skill.py"], { cwd: REPO_ROOT, encoding: "utf8", timeout: 120_000, env: { PATH: "/usr/bin:/bin:/usr/sbin:/sbin", LANG: "C.UTF-8", PYTHONDONTWRITEBYTECODE: "1", PYTHONNOUSERSITE: "1" } });
  fs.mkdirSync(path.dirname(outputPath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(outputPath, `NODE\n${node.stdout}\n${node.stderr}\nPYTHON\n${python.stdout}\n${python.stderr}`, { encoding: "utf8", mode: 0o600, flag: "wx" });
  if (node.status !== 0 || node.signal !== null || node.error || python.status !== 0 || python.signal !== null || python.error) fail("LAN_DETERMINISTIC_PREFLIGHT_FAILED", "Standalone deterministic preflight failed", { node_status: node.status, python_status: python.status });
}


function writeGenerationCacheVerification({ plan, options, roots }) {
  const evidenceRoot = createEmptyRoot(roots.evidence_root, "cache verification evidence root");
  createEmptyRoot(roots.work_root, "cache verification work root");
  createEmptyRoot(roots.private_root, "cache verification private root");
  createEmptyRoot(roots.usage_root, "cache verification usage root");
  const cache = validateGenerationCache({ cacheRoot: options.cacheRoot, producer: plan.producer });
  const validator = validateGeneratedPackage({ pythonEntry: options.pythonEntry, validator: path.join(plan.paths.metaSkillRoot, "scripts", "validate_generated_skill.py"), packageRoot: cache.package_root, wiki: plan.paths.wiki, module: FIXED_MODULE });
  const oracle = auditGeneratedPackage({ packageRoot: cache.package_root, oraclePath: plan.paths.oracle, module: FIXED_MODULE });
  const usage = { schema_version: 1, status: "PASS", expected_phases: [], retry_count: 0, aggregate: { input_tokens: 0, output_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, total_tokens: 0, cost_usd: 0 } };
  writeJsonExclusive(path.join(evidenceRoot, "claude-identity.json"), { schema_version: 1, status: "PASS", claude: plan.claudeIdentity, producer: plan.producer });
  writeJsonExclusive(path.join(evidenceRoot, "model-invocations.json"), { schema_version: 1, status: "PASS", retry_policy: "NONE", invocations: [] });
  writeJsonExclusive(path.join(evidenceRoot, "model-usage.json"), usage);
  writeJsonExclusive(path.join(evidenceRoot, "package-validation-audit.json"), validator);
  writeJsonExclusive(path.join(evidenceRoot, "generated-skill.json"), { schema_version: 1, status: "PASS", producer_identity: plan.producer.producer_identity, package_tree_sha256: cache.manifest.package.tree_sha256, validator, cache: cache.manifest, published: false });
  writeJsonExclusive(path.join(evidenceRoot, "tool-trace-audit.json"), { schema_version: 1, status: "PASS", tool_count: 0, skill_calls: [] });
  writeJsonExclusive(path.join(evidenceRoot, "scenario-evaluation-audit.json"), oracle);
  writeJsonExclusive(path.join(evidenceRoot, "security-audit.json"), { schema_version: 1, status: "PASS", cache_only: true, secret_values_persisted: false });
  const gate = { schema_version: 1, status: "PASS", goal: "generation", mode: "cache-verification", retry_count: 0 };
  writeJsonExclusive(path.join(evidenceRoot, "adapter-receipt.json"), gate);
  return { gate, usage, cache };
}


function evidenceManifest(evidenceRoot, names) {
  const missing = names.filter((name) => !fs.existsSync(path.join(evidenceRoot, name)));
  const files = names.filter((name) => !missing.includes(name)).map((name) => ({ path: `evidence/${name}`, sha256: sha256File(path.join(evidenceRoot, name)), size: fs.statSync(path.join(evidenceRoot, name)).size }));
  return { missing, files };
}


function failureDomain(failure) {
  const code = failure?.code ?? "LAN_UNKNOWN_FAILURE";
  return /(?:ORACLE|SLOT|HELPER|BROKER|ZIP|FINAL|SKILL_CALL|LOG_SCOPE|PACKER|GENERATED_SKILL_INVALID)/u.test(code) ? "CONTRACT" : "ENGINEERING";
}


async function executeGeneration({ plan, options, runRoot, id }) {
  const roots = standaloneScenarioRoots({ runRoot, runId: id, scratchRoot: options.scratchRoot });
  runDeterministicPreflight(options, path.join(runRoot, "deterministic-preflight.txt"));
  let result = null;
  let failure = null;
  try {
    result = plan.mode === "cache-verification"
      ? writeGenerationCacheVerification({ plan, options, roots })
      : await runGeneration({ runId: id, sourceRoot: REPO_ROOT, claudeEntry: options.claudeEntry, claudeSettings: options.claudeSettings, pythonEntry: options.pythonEntry, cacheRoot: options.cacheRoot, workRoot: roots.work_root, privateRoot: roots.private_root, evidenceRoot: roots.evidence_root, usageRoot: roots.usage_root, metaSkillRoot: plan.paths.metaSkillRoot, wiki: plan.paths.wiki, oracle: plan.paths.oracle, runnerFiles: RUNNER_FILES, claudeIdentity: plan.claudeIdentity });
  } catch (error) { failure = safeFailure(error); }
  const manifest = evidenceManifest(roots.evidence_root, GENERATION_EVIDENCE);
  const status = failure === null && manifest.missing.length === 0 ? "PASS" : "FAIL";
  const invocationPath = path.join(roots.evidence_root, "model-invocations.json");
  const usagePath = path.join(roots.evidence_root, "model-usage.json");
  const observedInvocations = fs.existsSync(invocationPath) ? JSON.parse(fs.readFileSync(invocationPath, "utf8")).invocations ?? [] : [];
  const observedUsage = result?.usage ?? (fs.existsSync(usagePath) ? JSON.parse(fs.readFileSync(usagePath, "utf8")) : null);
  const verdict = { schema_version: 1, framework: FRAMEWORK_ID, framework_version: FRAMEWORK_VERSION, run_id: id, goal: GENERATION_GOAL, mode: plan.mode, status, failure_domain: failure ? failureDomain(failure) : null, plan_sha256: plan.plan_sha256, central_test_flow: false, release_claim: false, deterministic_preflight: { path: "deterministic-preflight.txt", sha256: sha256File(path.join(runRoot, "deterministic-preflight.txt")) }, model_processes: { expected: plan.execution.expected_model_processes, actual: observedInvocations.length, retry_count: 0 }, usage: observedUsage, failure, missing_evidence: manifest.missing, evidence: manifest.files };
  writeJsonExclusive(path.join(runRoot, "verdict.json"), verdict);
  return verdict;
}


async function executeDiagnosisScenario({ plan, options, suiteRoot, scenario, id }) {
  const runRoot = options.allScenarios ? path.join(suiteRoot, "scenarios", scenario) : suiteRoot;
  fs.mkdirSync(runRoot, { recursive: true, mode: 0o700 });
  const roots = standaloneScenarioRoots({ runRoot, runId: `${id}-${scenario}`, scratchRoot: options.scratchRoot });
  const cache = validateGenerationCache({ cacheRoot: options.cacheRoot, producer: plan.producer });
  let result = null;
  let failure = null;
  try {
      result = await runDiagnosis({ runId: id, scenario, claudeEntry: options.claudeEntry, claudeSettings: options.claudeSettings, pythonEntry: options.pythonEntry, workRoot: roots.work_root, privateRoot: roots.private_root, evidenceRoot: roots.evidence_root, usageRoot: roots.usage_root, generatedSkillRoot: cache.package_root, helperSkillRoot: plan.paths.helperSkillRoot, brokerStub: plan.paths.brokerStub, clientLog: plan.paths.clientLog, serverLog: plan.paths.serverLog });
  } catch (error) { failure = safeFailure(error); }
  fs.mkdirSync(roots.evidence_root, { recursive: true, mode: 0o700 });
  writeJsonExclusive(path.join(roots.evidence_root, "claude-identity.json"), { schema_version: 1, status: "PASS", claude: plan.claudeIdentity, helper_skill_tree_sha256: treeDigest(plan.paths.helperSkillRoot), producer: plan.producer });
  writeJsonExclusive(path.join(roots.evidence_root, "generated-skill-cache.json"), { schema_version: 1, status: "PASS", manifest: cache.manifest });
  writeJsonExclusive(path.join(roots.evidence_root, "scenario-input.json"), { schema_version: 1, scenario, problem_time: "2026-08-23T10:00:05.300Z", module: FIXED_MODULE, supplied_slots: scenario === "complete" ? { client_slot: "1", server_slot: "2" } : {} });
  const manifest = evidenceManifest(roots.evidence_root, DIAGNOSIS_EVIDENCE);
  const status = failure === null && manifest.missing.length === 0 ? "PASS" : "FAIL";
  const invocationPath = path.join(roots.evidence_root, "model-invocations.json");
  const usagePath = path.join(roots.evidence_root, "model-usage.json");
  const observedInvocations = fs.existsSync(invocationPath) ? JSON.parse(fs.readFileSync(invocationPath, "utf8")).invocations ?? [] : [];
  const observedUsage = result?.usage ?? (fs.existsSync(usagePath) ? JSON.parse(fs.readFileSync(usagePath, "utf8")) : null);
  const verdict = { schema_version: 1, framework: FRAMEWORK_ID, framework_version: FRAMEWORK_VERSION, run_id: `${id}-${scenario}`, goal: DIAGNOSIS_GOAL, mode: "diagnosis", scenario, status, failure_domain: failure ? failureDomain(failure) : null, plan_sha256: plan.plan_sha256, central_test_flow: false, release_claim: false, model_processes: { expected: 1, actual: observedInvocations.length, retry_count: 0 }, usage: observedUsage, failure, missing_evidence: manifest.missing, evidence: manifest.files };
  writeJsonExclusive(path.join(runRoot, "verdict.json"), verdict);
  return verdict;
}


async function executeDiagnosis({ plan, options, runRoot, id }) {
  runDeterministicPreflight(options, path.join(runRoot, "deterministic-preflight.txt"));
  if (!options.allScenarios) return executeDiagnosisScenario({ plan, options, suiteRoot: runRoot, scenario: options.scenario, id });
  const children = [];
  let engineeringFailure = null;
  for (const scenario of DIAGNOSIS_SCENARIOS) {
    if (engineeringFailure !== null) break;
    const verdict = await executeDiagnosisScenario({ plan, options, suiteRoot: runRoot, scenario, id });
    children.push({ scenario, verdict });
    if (verdict.status !== "PASS" && verdict.failure_domain === "ENGINEERING") engineeringFailure = { scenario, failure: verdict.failure };
  }
  const status = engineeringFailure ? "ERROR" : children.length === DIAGNOSIS_SCENARIOS.length && children.every((item) => item.verdict.status === "PASS") ? "PASS" : "FAIL";
  const verdict = { schema_version: 1, framework: FRAMEWORK_ID, framework_version: FRAMEWORK_VERSION, run_id: id, goal: DIAGNOSIS_GOAL, mode: "diagnosis-suite", scenario: null, status, failure_domain: engineeringFailure ? "ENGINEERING" : status === "FAIL" ? "CONTRACT" : null, plan_sha256: plan.plan_sha256, central_test_flow: false, release_claim: false, deterministic_preflight: { path: "deterministic-preflight.txt", sha256: sha256File(path.join(runRoot, "deterministic-preflight.txt")) }, model_processes: { expected: DIAGNOSIS_SCENARIOS.length, actual: children.reduce((sum, item) => sum + item.verdict.model_processes.actual, 0), retry_count: 0 }, usage: aggregateUsage(children.map((item) => item.verdict.usage?.aggregate ?? item.verdict.usage)), failure: engineeringFailure, scenarios: children.map((item) => ({ scenario_id: item.scenario, status: item.verdict.status, failure_domain: item.verdict.failure_domain, verdict: { path: `scenarios/${item.scenario}/verdict.json`, sha256: sha256File(path.join(runRoot, "scenarios", item.scenario, "verdict.json")) } })) };
  writeJsonExclusive(path.join(runRoot, "verdict.json"), verdict);
  return verdict;
}


async function execute(options, plan) {
  const id = runId(options.goal === GENERATION_GOAL ? "lan-generation" : "lan-diagnosis");
  const runRoot = path.join(options.runsRoot, id);
  writePlan(runRoot, plan);
  if (plan.admission.status !== "READY" || (plan.execution.expected_model_processes > 0 && !options.allowRealModel)) {
    const failure = plan.admission.status !== "READY" ? { code: "LAN_ADMISSION_BLOCKED", blockers: plan.admission.blockers } : { code: "LAN_REAL_MODEL_OPT_IN_REQUIRED", message: "Real Claude execution requires --allow-real-model" };
    const verdict = { schema_version: 1, framework: FRAMEWORK_ID, framework_version: FRAMEWORK_VERSION, run_id: id, goal: options.goal, mode: plan.mode, scenario: options.scenario, status: "BLOCKED", failure_domain: "ENGINEERING", plan_sha256: plan.plan_sha256, central_test_flow: false, release_claim: false, model_processes: { expected: plan.execution.expected_model_processes, actual: 0, retry_count: 0 }, usage: null, failure };
    writeJsonExclusive(path.join(runRoot, "verdict.json"), verdict);
    return { verdict, runRoot };
  }
  let verdict;
  try {
    verdict = options.goal === GENERATION_GOAL
      ? await executeGeneration({ plan, options, runRoot, id })
      : await executeDiagnosis({ plan, options, runRoot, id });
  } catch (error) {
    const existing = path.join(runRoot, "verdict.json");
    if (fs.existsSync(existing)) verdict = JSON.parse(fs.readFileSync(existing, "utf8"));
    else {
      const failure = safeFailure(error);
      verdict = { schema_version: 1, framework: FRAMEWORK_ID, framework_version: FRAMEWORK_VERSION, run_id: id, goal: options.goal, mode: plan.mode, scenario: options.scenario, status: "ERROR", failure_domain: "ENGINEERING", plan_sha256: plan.plan_sha256, central_test_flow: false, release_claim: false, model_processes: { expected: plan.execution.expected_model_processes, actual: 0, retry_count: 0 }, usage: null, failure };
      writeJsonExclusive(existing, verdict);
    }
  }
  return { verdict, runRoot };
}


function usage() {
  return `Usage:\n  ./tools/test-flow/quick-validation/claude-deepseek-lan-skill/run.sh --goal generation [--plan-only] [--allow-real-model]\n  ./tools/test-flow/quick-validation/claude-deepseek-lan-skill/run.sh --goal diagnosis (--scenario missing-slots|complete | --all-scenarios) [--plan-only] [--allow-real-model]\n`;
}


async function main() {
  try {
    const values = parseArguments(process.argv.slice(2));
    if (values.help === true) { process.stdout.write(usage()); return; }
    const options = defaults(values);
    const plan = buildPlan(options);
    if (options.planOnly) {
      const publicPlan = Object.fromEntries(
        Object.entries(plan).filter(([key]) => !["paths", "claudeIdentity", "producer", "cache"].includes(key)),
      );
      process.stdout.write(canonicalJson(publicPlan));
      return;
    }
    const result = await execute(options, plan);
    process.stdout.write(canonicalJson({ run_id: result.verdict.run_id, status: result.verdict.status, verdict: path.join(result.runRoot, "verdict.json") }));
    process.exitCode = result.verdict.status === "PASS" ? 0 : result.verdict.status === "BLOCKED" ? 2 : 1;
  } catch (error) {
    process.stderr.write(canonicalJson(safeFailure(error)));
    process.exitCode = 1;
  }
}


if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
