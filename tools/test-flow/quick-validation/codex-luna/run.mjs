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
  MACOS_CODEX_LUNA_E2E_CALLS,
  MACOS_CODEX_LUNA_E2E_TOKEN_LIMIT,
  MACOS_CODEX_LUNA_E2E_USD_LIMIT,
  MACOS_CODEX_LUNA_METHODS_CALLS,
  MACOS_CODEX_LUNA_METHODS_TOKEN_LIMIT,
  MACOS_CODEX_LUNA_METHODS_USD_LIMIT,
  MACOS_CODEX_LUNA_SCENARIOS,
  methodsCachePath,
  scenarioPaths,
  validateMethodsCache,
} from "./runtime/macos-codex-luna-e2e-contract.mjs";
import { runE2E } from "./runtime/macos-codex-luna-e2e-runner.mjs";
import {
  runMethodsBootstrap,
  verifyMethodsCacheOnly,
} from "./runtime/macos-codex-luna-methods-runner.mjs";

const SCRIPT_ROOT = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(SCRIPT_ROOT, "..", "..", "..", "..");
const FRAMEWORK_ID = "macos-codex-luna-fast-e2e";
const FRAMEWORK_VERSION = 1;
const GOALS = new Set(["methods", "e2e"]);
const FLAGS = new Set(["plan-only", "allow-real-model", "help"]);

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
    for (const key of ["id", "method", "field", "response_code", "response_message"]) {
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
  if (values.scenario !== undefined && !MACOS_CODEX_LUNA_SCENARIOS.includes(values.scenario)) fail("LUNA_SCENARIO_INVALID", "--scenario is not in the repository-owned matrix");
  return values;
}

function defaults(values) {
  return {
    goal: values.goal,
    scenario: values.scenario ?? "api-execution-overrun",
    planOnly: values["plan-only"] === true,
    allowRealModel: values["allow-real-model"] === true,
    codexEntry: path.resolve(values["codex-entry"] ?? "/Applications/ChatGPT.app/Contents/Resources/codex"),
    authSource: path.resolve(values["codex-auth"] ?? path.join(process.env.HOME ?? "", ".codex", "auth.json")),
    pythonEntry: path.resolve(values["python-entry"] ?? path.join(REPO_ROOT, ".venv", "bin", "python")),
    cacheRoot: path.resolve(values["cache-root"] ?? path.join(REPO_ROOT, ".tmp", "quick-validation", "codex-luna", "cache")),
    runsRoot: path.resolve(values["runs-root"] ?? path.join(REPO_ROOT, ".tmp", "quick-validation", "codex-luna", "runs")),
    logparseRoot: path.resolve(values["logparse-root"] ?? path.join(process.env.HOME ?? "", "Documents", "Codex", "2026-06-29-github-issue-locator-logparse", "logparse")),
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
  if (process.platform !== "darwin" || process.arch !== "arm64") blockers.push({ code: "LUNA_HOST_UNSUPPORTED", detail: "The fast flow requires macOS arm64" });
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

  if (options.goal === "e2e") {
    requiredDirectory(options.logparseRoot, "LUNA_LOGPARSE_MISSING", "Logparse source", blockers);
    try { scenarioPaths(REPO_ROOT, options.scenario); } catch (error) { blockers.push({ code: error?.code ?? "LUNA_SCENARIO_INVALID", detail: error?.message ?? "Scenario inputs are invalid" }); }
    if (cache.status !== "PRESENT") blockers.push({ code: "LUNA_METHODS_CACHE_REQUIRED", detail: `E2E requires the exact Methods cache (${cache.code ?? cache.status})` });
  }
  if (options.goal === "methods" && cache.status === "INVALID") blockers.push({ code: "LUNA_METHODS_CACHE_INVALID", detail: `Exact cache exists but is invalid (${cache.code})` });

  const mode = options.goal === "methods" && cache.status === "PRESENT" ? "cache-verification" : options.goal === "methods" ? "bootstrap" : "e2e";
  const expectedCalls = mode === "cache-verification" ? 0 : options.goal === "methods" ? MACOS_CODEX_LUNA_METHODS_CALLS : MACOS_CODEX_LUNA_E2E_CALLS;
  const tokenCap = options.goal === "methods" ? MACOS_CODEX_LUNA_METHODS_TOKEN_LIMIT : MACOS_CODEX_LUNA_E2E_TOKEN_LIMIT;
  const costCap = options.goal === "methods" ? MACOS_CODEX_LUNA_METHODS_USD_LIMIT : MACOS_CODEX_LUNA_E2E_USD_LIMIT;
  const planCore = {
    schema_version: 1,
    framework: FRAMEWORK_ID,
    framework_version: FRAMEWORK_VERSION,
    goal: options.goal,
    mode,
    scenario: options.goal === "e2e" ? options.scenario : null,
    execution: {
      entry: "tools/test-flow/quick-validation/codex-luna/run.mjs",
      old_test_flow_orchestrator: false,
      source_snapshot: false,
      history_reuse: false,
      automatic_retry: false,
      security_and_permission_proof: false,
      expected_model_calls: expectedCalls,
      model: "gpt-5.6-luna",
      reasoning_effort: "medium",
      token_cap: tokenCap,
      equivalent_usd_cap: costCap,
      wall_timeout_seconds: 1800,
      per_call_timeout_seconds: 600,
      no_progress_seconds: 180,
    },
    inputs: {
      repository_root: REPO_ROOT,
      codex: codexIdentity,
      producer,
      methods_cache: cache,
      python_entry: options.pythonEntry,
      logparse_root: options.goal === "e2e" ? options.logparseRoot : null,
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

export async function execute(options, plan) {
  const id = runId();
  const runRoot = path.join(options.runsRoot, id);
  const workRoot = path.join(runRoot, "work");
  const privateRoot = path.join(runRoot, "private");
  const evidenceRoot = path.join(runRoot, "evidence");
  const usageRoot = path.join(runRoot, "usage");
  fs.mkdirSync(runRoot, { recursive: false, mode: 0o700 });
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

function usage() {
  return `Usage:\n  ./tools/test-flow/quick-validation/codex-luna/run.sh --goal methods [--plan-only] [--allow-real-model]\n  ./tools/test-flow/quick-validation/codex-luna/run.sh --goal e2e --scenario api-execution-overrun [--plan-only] [--allow-real-model]\n\nThis entry has its own planner and lightweight verdict writer.\n`;
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
