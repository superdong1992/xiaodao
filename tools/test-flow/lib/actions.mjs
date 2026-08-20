import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  ensureDirectory,
  removeTreeWritable,
  resolveCommand,
  resolvePythonTestRuntime,
  runSync,
  writeJsonSync,
} from "./util.mjs";
import { runProcess } from "./process.mjs";
import {
  RELEASE_BASE_IMAGE,
  materializeClaudeSettings,
} from "./release-inputs.mjs";
import { isCompleteUsage, sumUsage, TOKEN_USAGE_FORMULA } from "./usage.mjs";
import {
  buildIsolatedAgentEnvironment,
  ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY,
  ISOLATED_AGENT_ENV_POLICY_VERSION,
  ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT,
  validEnvironmentKeySummary,
} from "../runtime-support/isolated-agent-env.mjs";
import {
  SKILL_GENERATION_TRACE_SCHEMA_VERSION,
  validSkillGenerationTraceAuditReceipt,
} from "../runtime-support/isolated-agent-tool-audit.mjs";

function pythonRuntime(repoRoot) {
  return resolvePythonTestRuntime(repoRoot);
}

function gateExecutionId(stage, gateId) {
  return `${stage.id}--${gateId}`;
}

export function pytestScratchBoundary({
  platform = process.platform,
  temporaryDirectory = os.tmpdir(),
  repoRoot = null,
  isolatedAgent = false,
  configuredWindowsDirectory = process.env.TEST_FLOW_WINDOWS_SCRATCH_ROOT ?? null,
  attemptRoot,
} = {}) {
  if (platform === "win32") {
    if (configuredWindowsDirectory !== null) {
      if (!path.isAbsolute(configuredWindowsDirectory)) {
        throw new Error("PYTEST_WINDOWS_SCRATCH_ROOT_ABSOLUTE_REQUIRED");
      }
      return path.resolve(configuredWindowsDirectory);
    }
    const systemTemporary = path.resolve(temporaryDirectory);
    if (isolatedAgent || repoRoot === null) return systemTemporary;
    const repositoryTemporary = path.resolve(repoRoot, ".tmp", "p");
    return repositoryTemporary.length <= systemTemporary.length
      ? repositoryTemporary
      : systemTemporary;
  }
  if (!attemptRoot) throw new Error("PYTEST_ATTEMPT_ROOT_REQUIRED");
  return path.resolve(attemptRoot);
}

export function pytestBaseTempPath(scratch, platform = process.platform) {
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  const absolute = pathApi.resolve(scratch);
  if (platform !== "win32" || absolute.startsWith("\\\\?\\")) return absolute;
  if (absolute.startsWith("\\\\")) return `\\\\?\\UNC\\${absolute.slice(2)}`;
  return `\\\\?\\${absolute}`;
}

function gateRoot(context, stage) {
  return context.gateRoot ?? path.join(context.attemptRoot, "payload", "stages", stage.id);
}

function xmlInteger(attributes, name) {
  const match = new RegExp(`(?:^|\\s)${name}="(\\d+)"`).exec(attributes);
  return match ? Number(match[1]) : 0;
}

export function parseJUnitSummary(filePath) {
  if (!fs.existsSync(filePath)) throw new Error("JUNIT_MISSING");
  const text = fs.readFileSync(filePath, "utf8");
  const aggregateRoot = /<testsuites\b([^>]*)>/.exec(text);
  const suites = [...text.matchAll(/<testsuite\b([^>]*)>/g)].map((match) => match[1]);
  const attributes = aggregateRoot && /(?:^|\s)tests="\d+"/.test(aggregateRoot[1])
    ? [aggregateRoot[1]]
    : suites;
  if (attributes.length === 0) throw new Error("JUNIT_ROOT_INVALID");
  const tests = attributes.reduce((total, value) => total + xmlInteger(value, "tests"), 0);
  const failures = attributes.reduce((total, value) => total + xmlInteger(value, "failures"), 0);
  const errors = attributes.reduce((total, value) => total + xmlInteger(value, "errors"), 0);
  const skipped = attributes.reduce((total, value) => total + xmlInteger(value, "skipped"), 0);
  if (![tests, failures, errors, skipped].every(Number.isSafeInteger) || failures + errors + skipped > tests) throw new Error("JUNIT_COUNTS_INVALID");
  return {
    schema_version: 2,
    tests,
    passed: tests - failures - errors - skipped,
    failures,
    errors,
    skipped,
    executed: tests - skipped,
  };
}

export function materializePytestSummary(stageEvidence) {
  const junitPath = path.join(stageEvidence, "pytest.xml");
  if (!fs.existsSync(junitPath)) return null;
  const summary = parseJUnitSummary(junitPath);
  writeJsonSync(path.join(stageEvidence, "pytest-summary.json"), summary);
  return summary;
}

export function evaluatePytestSummary(summary, { minPassed = 1, skipPolicy = "forbid-all-skipped" } = {}) {
  if (summary.executed === 0) return { status: "FAIL", failure_domain: "CONTRACT", code: "PYTEST_NO_EXECUTED_TESTS" };
  if (summary.passed < minPassed) return { status: "FAIL", failure_domain: "CONTRACT", code: "PYTEST_MIN_PASSED_NOT_MET" };
  if (skipPolicy === "forbid" && summary.skipped > 0) return { status: "FAIL", failure_domain: "CONTRACT", code: "PYTEST_SKIP_FORBIDDEN" };
  return { status: "PASS", failure_domain: null, code: null };
}

function listTestFiles(root) {
  if (!fs.existsSync(root)) return [];
  const metadata = fs.statSync(root);
  if (metadata.isFile()) return /^test_.*\.py$/.test(path.basename(root)) ? [root] : [];
  const files = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const nested = path.join(root, entry.name);
    if (entry.isDirectory()) files.push(...listTestFiles(nested));
    else if (entry.isFile() && /^test_.*\.py$/.test(entry.name)) files.push(nested);
  }
  return files;
}

function collapseSelectors(selectors) {
  const ordered = [...selectors].sort((left, right) => left.length - right.length || left.localeCompare(right));
  const kept = [];
  for (const selector of ordered) {
    if (kept.some((parent) => selector === parent || selector.startsWith(`${parent}/`))) continue;
    kept.push(selector);
  }
  return kept.sort();
}

function affectedSelectors(changedFiles) {
  const selectors = new Set();
  const add = (...values) => values.forEach((value) => selectors.add(value));
  for (const file of changedFiles) {
    if (file.startsWith("tests/deterministic/") && file.endsWith(".py")) {
      add(path.posix.basename(file).startsWith("test_") ? file : path.posix.dirname(file));
      continue;
    }
    if (file === "docs/browser-rest-api.md" || file === "schemas/v2/web-api.openapi.snapshot.json") {
      add("tests/deterministic/unit/interfaces/test_web_api.py");
    }
    if (/^(schemas|src\/problem_locator\/contracts)\//.test(file)) add("tests/deterministic/contracts");
    if (/^src\/problem_locator\/domain\//.test(file)) add("tests/deterministic/unit/domain", "tests/deterministic/integration/test_s01_contract_domain_seam.py");
    if (/^src\/problem_locator\/storage\//.test(file)) add("tests/deterministic/unit/storage", "tests/deterministic/integration");
    if (/^src\/problem_locator\/application\//.test(file)) add("tests/deterministic/unit/application", "tests/deterministic/integration");
    if (/^src\/problem_locator\/dispatch\//.test(file)) add("tests/deterministic/unit/dispatch", "tests/deterministic/integration");
    if (/^src\/problem_locator\/(interfaces|entrypoints)\//.test(file)) add("tests/deterministic/unit/interfaces", "tests/deterministic/integration");
    if (/^src\/problem_locator\/runtime\//.test(file)) add("tests/deterministic/unit/runtime", "tests/deterministic/integration");
    if (/^src\/problem_locator\/integrations\//.test(file)) add("tests/deterministic/unit/integrations", "tests/deterministic/integration");
    if (/^src\/problem_locator\/(bootstrap|__init__|__main__)\.py$/.test(file)) add("tests/deterministic/integration");
    if (/^(pyproject\.toml|uv\.lock)$/.test(file)) add("tests/deterministic/contracts", "tests/deterministic/unit", "tests/deterministic/integration");
    if (/^tests\/fixtures\//.test(file)) add("tests/deterministic/contracts", "tests/deterministic/journey");
  }
  return collapseSelectors(selectors);
}

export function planAffectedSelection(repoRoot, changedFiles) {
  const selectors = affectedSelectors(changedFiles);
  const fullRoot = path.join(repoRoot, "tests", "deterministic");
  const allTests = new Set(listTestFiles(fullRoot).map((file) => path.resolve(file)));
  const covered = new Set();
  for (const selector of selectors) {
    for (const file of listTestFiles(path.resolve(repoRoot, selector))) covered.add(path.resolve(file));
  }
  const coverage = allTests.size === 0 ? 0 : covered.size / allTests.size;
  return {
    selectors,
    covered_test_files: covered.size,
    total_test_files: allTests.size,
    coverage,
    defer_to_full: coverage >= 0.5,
  };
}

export function probeLoopbackCapability(runtime, repoRoot, environment = process.env, invoke = runSync) {
  const probe = invoke(runtime.command, [
    ...(runtime.interpreterPrefix ?? []),
    "-c",
    "import socket; server = socket.socket(socket.AF_INET, socket.SOCK_STREAM); server.bind(('127.0.0.1', 0)); server.close()",
  ], {
    cwd: repoRoot,
    env: { ...environment, PYTHONNOUSERSITE: "1" },
  });
  const output = `${probe.stderr ?? ""}\n${probe.stdout ?? ""}`;
  return {
    schema_version: 1,
    status: probe.status === 0 ? "PASS" : "BLOCKED",
    capability: "ipv4-loopback-bind",
    exit_code: probe.status,
    signal: probe.signal ?? null,
    error_code: probe.error?.code ?? null,
    failure_code: probe.status === 0
      ? null
      : /PermissionError|operation not permitted|permission denied/i.test(output)
        ? "LOOPBACK_BIND_PERMISSION_DENIED"
        : "LOOPBACK_BIND_UNAVAILABLE",
  };
}

async function pytestAction(context, stage, selectors, {
  extra = [],
  env = {},
  real = false,
  minPassed = 1,
  skipPolicy = "forbid-all-skipped",
  selection = null,
  isolatedAgent = false,
} = {}) {
  const runtime = pythonRuntime(context.repoRoot);
  if (!runtime) {
    return { status: "BLOCKED", failure_domain: "INFRA", code: "PYTHON_312_TEST_RUNTIME_MISSING", elapsed_seconds: 0 };
  }
  const expectedPython = context.runtimeProfile?.version ?? context.runtimeProfile?.python ?? null;
  if (typeof expectedPython !== "string" || !runtime.details.python_version.startsWith(`${expectedPython.split(".").slice(0, 2).join(".")}.`)) {
    return { status: "BLOCKED", failure_domain: "INFRA", code: "PYTHON_RUNTIME_PROFILE_MISMATCH", elapsed_seconds: 0 };
  }
  const stageEvidence = gateRoot(context, stage);
  ensureDirectory(stageEvidence);
  if (selectors.length === 0) {
    const summary = { schema_version: 2, tests: 0, passed: 0, failures: 0, errors: 0, skipped: 0, executed: 0, not_required: true };
    writeJsonSync(path.join(stageEvidence, "pytest-summary.json"), summary);
    return { status: "NOT_REQUIRED", failure_domain: null, code: "AFFECTED_SCOPE_EMPTY", elapsed_seconds: 0, pytest: summary, selection };
  }
  const externalScratch = process.platform === "win32";
  const scratchBoundary = pytestScratchBoundary({
    attemptRoot: context.attemptRoot,
    repoRoot: context.repoRoot,
    isolatedAgent,
  });
  if (externalScratch) ensureDirectory(scratchBoundary);
  const scratch = externalScratch
    ? fs.mkdtempSync(path.join(scratchBoundary, "p-"))
    : path.join(context.attemptRoot, "scratch", gateExecutionId(stage, context.gateId ?? stage.id));
  const pytestScratch = externalScratch ? scratch : path.join(scratch, "pytest");
  ensureDirectory(scratch);
  const processEnvironment = {
    PYTHONNOUSERSITE: "1",
    PYTHONPYCACHEPREFIX: path.join(scratch, "pycache"),
    ...env,
  };
  const loopbackEnvironment = isolatedAgent
    ? buildIsolatedAgentEnvironment({ ambient: process.env, explicit: processEnvironment })
    : process.env;
  const loopback = probeLoopbackCapability(runtime, context.repoRoot, loopbackEnvironment);
  writeJsonSync(path.join(stageEvidence, "loopback-capability.json"), loopback);
  if (loopback.status !== "PASS") {
    removeTreeWritable(scratch, scratchBoundary);
    return {
      status: "BLOCKED",
      failure_domain: "INFRA",
      code: loopback.failure_code,
      elapsed_seconds: 0,
      capability_receipt: "loopback-capability.json",
    };
  }
  const args = [
    ...runtime.prefix,
    ...selectors,
    "-q",
    "-p", "no:cacheprovider",
    `--basetemp=${pytestBaseTempPath(pytestScratch)}`,
    `--junitxml=${path.join(stageEvidence, "pytest.xml")}`,
    ...extra,
  ];
  let result;
  try {
    result = await runProcess({
      repoRoot: context.repoRoot,
      attemptRoot: context.attemptRoot,
      stage,
      command: runtime.command,
      args,
      cwd: context.repoRoot,
      env: processEnvironment,
      hardTimeoutSeconds: stage.timeout_seconds,
      noProgressSeconds: null,
      rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
      eventWriter: context.eventWriter,
      executionId: gateExecutionId(stage, context.gateId ?? stage.id),
      pollMilliseconds: context.policies.poll_milliseconds,
      progressAllowlistVersion: context.policies.progress_allowlist_version,
      environmentPolicy: isolatedAgent ? ISOLATED_AGENT_ENV_POLICY_VERSION : null,
    });
  } finally {
    removeTreeWritable(scratch, scratchBoundary);
  }
  let summary = null;
  let summaryError = null;
  try {
    summary = materializePytestSummary(stageEvidence);
  } catch (error) {
    summaryError = error;
  }
  if (result.status === "ERROR") return { ...result, failure_domain: "HARNESS", code: result.termination?.trigger ?? "PROCESS_EVIDENCE_ERROR", pytest: summary };
  if (result.status === "INCONCLUSIVE") return { ...result, failure_domain: "EXTERNAL", code: result.termination.trigger, pytest: summary };
  if (result.status !== "PASS") return { ...result, failure_domain: real ? "CONTRACT" : "PRODUCT", code: "PYTEST_FAILED", pytest: summary };
  if (summaryError !== null || summary === null) {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: summaryError?.message ?? "JUNIT_MISSING", pytest: null };
  }
  const evaluation = evaluatePytestSummary(summary, { minPassed, skipPolicy });
  return { ...result, ...evaluation, pytest: summary, selection };
}

function quoteShell(value) {
  return `'${String(value).replaceAll("'", `'"'"'`)}'`;
}

function nodeTestFiles(repoRoot, gate) {
  if (gate.test_files) return gate.test_files.map((entry) => path.join(repoRoot, entry));
  const directory = path.join(repoRoot, path.dirname(gate.test_glob));
  const excluded = new Set((gate.exclude ?? []).map((entry) => path.resolve(repoRoot, entry)));
  return fs.readdirSync(directory)
    .filter((name) => name.endsWith(".test.mjs"))
    .map((name) => path.join(directory, name))
    .filter((filePath) => !excluded.has(path.resolve(filePath)))
    .sort();
}

function parseTapSummary(filePath) {
  const text = fs.readFileSync(filePath, "utf8");
  const number = (label) => {
    const match = new RegExp(`^# ${label} (\\d+)$`, "m").exec(text);
    return match ? Number(match[1]) : null;
  };
  const tests = number("tests");
  const passed = number("pass");
  const failed = number("fail");
  const skipped = number("skipped") ?? 0;
  if (![tests, passed, failed, skipped].every(Number.isSafeInteger)) throw new Error("NODE_TEST_TAP_SUMMARY_INVALID");
  return { schema_version: 2, tests, passed, failed, skipped };
}

async function nodeTestAction(context, stage, gate) {
  const files = nodeTestFiles(context.repoRoot, gate);
  if (files.length === 0) return { status: "ERROR", failure_domain: "HARNESS", code: "NODE_TEST_SELECTION_EMPTY", elapsed_seconds: 0 };
  const result = await runProcess({
    repoRoot: context.repoRoot,
    attemptRoot: context.attemptRoot,
    stage,
    command: process.execPath,
    args: ["--test", "--test-reporter=tap", ...files],
    cwd: context.repoRoot,
    hardTimeoutSeconds: stage.timeout_seconds,
    noProgressSeconds: null,
    rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
    eventWriter: context.eventWriter,
    executionId: gateExecutionId(stage, context.gateId),
    pollMilliseconds: context.policies.poll_milliseconds,
    progressAllowlistVersion: context.policies.progress_allowlist_version,
  });
  const outputPath = path.join(context.attemptRoot, result.stdout_path);
  const tapPath = path.join(gateRoot(context, stage), "node-test.tap");
  if (fs.existsSync(outputPath)) fs.copyFileSync(outputPath, tapPath, fs.constants.COPYFILE_EXCL);
  if (result.status !== "PASS") return { ...result, failure_domain: "HARNESS", code: "NODE_TEST_FAILED" };
  let summary;
  try { summary = parseTapSummary(tapPath); } catch (error) {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: error.message };
  }
  if (summary.passed < gate.min_passed || summary.failed > 0) return { ...result, status: "FAIL", failure_domain: "HARNESS", code: "NODE_TEST_MIN_PASSED_NOT_MET", node_test: summary };
  return { ...result, failure_domain: null, node_test: summary };
}

async function repositoryCheck(context, stage, gate) {
  let command;
  let args;
  const scratch = path.join(context.attemptRoot, "scratch", gateExecutionId(stage, context.gateId));
  ensureDirectory(scratch);
  let externalPycacheRoot = null;
  let pycacheRoot = path.join(scratch, "pycache");
  if (gate.check === "python-compileall") {
    const runtime = pythonRuntime(context.repoRoot);
    if (!runtime) return { status: "BLOCKED", failure_domain: "INFRA", code: "PYTHON_312_TEST_RUNTIME_MISSING", elapsed_seconds: 0 };
    if (process.platform === "win32") {
      externalPycacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-compileall-"));
      pycacheRoot = externalPycacheRoot;
    }
    command = runtime.command;
    args = [...runtime.interpreterPrefix, "-m", "compileall", "-q", ...gate.paths];
  } else if (gate.check === "uv-lock") {
    command = resolveCommand(process.env.UV ?? "uv");
    if (!command) return { status: "BLOCKED", failure_domain: "INFRA", code: "UV_REQUIRED", elapsed_seconds: 0 };
    args = ["lock", "--check", "--offline"];
  } else if (gate.check === "git-diff-check") {
    command = resolveCommand("git");
    if (!command) return { status: "BLOCKED", failure_domain: "INFRA", code: "GIT_REQUIRED", elapsed_seconds: 0 };
    args = ["diff", "--check", "HEAD"];
  } else {
    return { status: "ERROR", failure_domain: "HARNESS", code: "REPOSITORY_CHECK_UNSUPPORTED", elapsed_seconds: 0 };
  }
  let result;
  try {
    result = await runProcess({
      repoRoot: gate.check === "git-diff-check" ? context.sourceRepoRoot : context.repoRoot,
      attemptRoot: context.attemptRoot,
      stage,
      command,
      args,
      cwd: gate.check === "git-diff-check" ? context.sourceRepoRoot : context.repoRoot,
      env: {
        PYTHONNOUSERSITE: "1",
        PYTHONPYCACHEPREFIX: pycacheRoot,
      },
      hardTimeoutSeconds: stage.timeout_seconds,
      noProgressSeconds: null,
      rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
      eventWriter: context.eventWriter,
      executionId: gateExecutionId(stage, context.gateId),
      pollMilliseconds: context.policies.poll_milliseconds,
      progressAllowlistVersion: context.policies.progress_allowlist_version,
    });
  } finally {
    removeTreeWritable(scratch, context.attemptRoot);
    if (externalPycacheRoot !== null) removeTreeWritable(externalPycacheRoot, os.tmpdir());
  }
  const receipt = {
    schema_version: 2,
    check: gate.check,
    status: result.status,
    exit_code: result.exit_code,
    elapsed_seconds: result.elapsed_seconds,
    stdout_path: result.stdout_path,
    stderr_path: result.stderr_path,
  };
  writeJsonSync(path.join(gateRoot(context, stage), "repository-check.json"), receipt);
  if (result.status !== "PASS") return { ...result, failure_domain: gate.check === "git-diff-check" ? "CONTRACT" : "PRODUCT", code: `REPOSITORY_${gate.check.toUpperCase().replaceAll("-", "_")}_FAILED` };
  return { ...result, failure_domain: null, repository_check: receipt };
}

function preparedClaudeRuntime(context) {
  const entry = context.options.claudeEntry;
  const sourceSettings = context.options.claudeSettings;
  if (!entry || !path.isAbsolute(entry) || !fs.existsSync(entry)) return null;
  if (!sourceSettings || !path.isAbsolute(sourceSettings) || !fs.existsSync(sourceSettings)) return null;
  const root = path.join(context.attemptRoot, "scratch", "claude-runtime");
  const settings = path.join(root, "settings.json");
  const config = path.join(root, "config");
  ensureDirectory(config);
  if (!fs.existsSync(settings)) materializeClaudeSettings(sourceSettings, settings);
  return {
    entry,
    settings,
    config,
    environment: {
      CLAUDE_CONFIG_DIR: config,
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
    },
  };
}

function agentCommand(context, workflow = "job") {
  const runtime = preparedClaudeRuntime(context);
  if (!runtime) return null;
  const caps = context.planStage.hard_caps;
  if (!caps?.max_turns || !caps?.max_budget_usd || !caps?.hard_timeout_seconds) return null;
  const usageRoot = path.join(context.gateRoot, "model-usage");
  ensureDirectory(usageRoot);
  const skillRootArgument = workflow === "skill-generation"
    ? ` --skill-root ${quoteShell(path.join(runtime.config, "skills", "wiki-to-diagnosis-skill"))} --source-root ${quoteShell(context.repoRoot)}`
    : "";
  const maxOutputTokensArgument = caps.max_output_tokens === undefined
    ? ""
    : ` --max-output-tokens ${caps.max_output_tokens} --max-output-tokens-upper-limit ${context.runtimeProfile.claude.max_output_tokens_upper_limit}`;
  return `${quoteShell(process.execPath)} ${quoteShell(path.join(context.repoRoot, "tools", "test-flow", "runtime-support", "isolated-agent-wrapper.mjs"))} --claude-entry ${quoteShell(runtime.entry)} --settings ${quoteShell(runtime.settings)} --model ${quoteShell(context.runtimeProfile.claude.model)} --usage-root ${quoteShell(usageRoot)} --max-turns ${caps.max_turns} --max-total-tokens ${caps.max_total_tokens}${maxOutputTokensArgument} --max-budget-usd ${caps.max_budget_usd} --hard-timeout-seconds ${caps.hard_timeout_seconds} --workflow ${quoteShell(workflow)}${skillRootArgument}`;
}

function validIsolatedOutputCapReceipt(invocation) {
  const cap = invocation.effective_caps?.max_output_tokens;
  const inboundKeys = invocation.environment_policy?.inbound?.key_names ?? [];
  const claudeKeys = invocation.environment_policy?.claude_process?.key_names ?? [];
  const enforcement = invocation.hard_cap_enforcement?.max_output_tokens;
  if (inboundKeys.includes(ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY)) return false;
  if (cap === undefined) {
    return !claudeKeys.includes(ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY)
      && enforcement === undefined;
  }
  return Number.isSafeInteger(cap)
    && cap > 0
    && cap <= invocation.effective_caps.max_total_tokens
    && claudeKeys.includes(ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY)
    && enforcement === ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT;
}

export function collectIsolatedModelUsage(context, profile) {
  const usageRoot = path.join(context.gateRoot, "model-usage");
  const files = fs.existsSync(usageRoot) ? fs.readdirSync(usageRoot).filter((name) => name.endsWith(".json")).sort() : [];
  const invocations = files.map((name) => JSON.parse(fs.readFileSync(path.join(usageRoot, name), "utf8")));
  if (invocations.length === 0) throw new Error("ISOLATED_MODEL_USAGE_RECEIPT_MISSING");
  if (invocations.some((invocation) => (
    invocation?.schema_version !== 3
    || invocation.class !== "isolated-agent"
    || invocation.usage_complete !== true
    || !isCompleteUsage(invocation.usage)
    || Object.hasOwn(invocation, "observed_request_limits")
  ))) throw new Error("ISOLATED_MODEL_USAGE_RECEIPT_INVALID");
  if (invocations.some((invocation) => (
    invocation.environment_policy?.schema_version !== 1
    || invocation.environment_policy?.version !== ISOLATED_AGENT_ENV_POLICY_VERSION
    || invocation.environment_policy?.provider_auth_source !== "audited-settings-file"
    || !validEnvironmentKeySummary(invocation.environment_policy?.inbound)
    || !validEnvironmentKeySummary(invocation.environment_policy?.claude_process)
    || !validIsolatedOutputCapReceipt(invocation)
  ))) throw new Error("ISOLATED_MODEL_ENVIRONMENT_POLICY_RECEIPT_INVALID");
  const expectedWorkflow = profile === "real-skill-generation" ? "skill-generation" : "job";
  if (invocations.some((invocation) => invocation.workflow !== expectedWorkflow)) throw new Error("ISOLATED_MODEL_WORKFLOW_RECEIPT_INVALID");
  if (invocations.some((invocation) => (
    typeof invocation.terminal?.subtype !== "string"
    || typeof invocation.terminal?.is_error !== "boolean"
    || invocation.wrapper_outcome?.schema_version !== 1
    || !["PASS", "FAIL"].includes(invocation.wrapper_outcome?.status)
    || (invocation.wrapper_outcome.status === "PASS" && invocation.wrapper_outcome.code !== null)
    || (invocation.wrapper_outcome.status === "FAIL" && !/^WRAPPER_[A-Z0-9_]+$/.test(invocation.wrapper_outcome.code ?? ""))
  ))) throw new Error("ISOLATED_MODEL_TERMINAL_RECEIPT_INVALID");
  if (expectedWorkflow === "skill-generation" && invocations.some((invocation) => {
    const audit = invocation.tool_trace_audit;
    if (audit === null) return invocation.wrapper_outcome.status === "PASS";
    const passedAudit = validSkillGenerationTraceAuditReceipt(audit);
    const failedAudit = audit?.schema_version === SKILL_GENERATION_TRACE_SCHEMA_VERSION
      && audit.status === "FAIL"
      && audit.workflow === "skill-generation"
      && /^SKILL_TRACE_[A-Z0-9_]+$/.test(audit.code ?? "")
      && Object.keys(audit).sort().join("\0") === ["code", "schema_version", "status", "workflow"].join("\0");
    return invocation.wrapper_outcome.status === "PASS" ? !passedAudit : !(passedAudit || failedAudit);
  })) throw new Error("ISOLATED_MODEL_TOOL_TRACE_AUDIT_INVALID");
  if (expectedWorkflow === "job" && invocations.some((invocation) => invocation.tool_trace_audit !== null)) throw new Error("ISOLATED_MODEL_TOOL_TRACE_AUDIT_UNEXPECTED");
  const usage = sumUsage(invocations.map((invocation) => invocation.usage));
  const summary = {
    schema_version: 3,
    status: "PASS",
    usage_complete: true,
    token_formula: TOKEN_USAGE_FORMULA,
    invocations,
    usage,
  };
  writeJsonSync(path.join(context.gateRoot, "model-usage.json"), summary);
  return summary;
}

async function hostCapability(context, stage) {
  const outputRoot = gateRoot(context, stage);
  ensureDirectory(outputRoot);
  const result = await runProcess({
    repoRoot: context.repoRoot,
    attemptRoot: context.attemptRoot,
    stage,
    command: process.execPath,
    args: [
      path.join(context.sourceSnapshotRoot, "tools", "test-flow", "adapters", "host-capability.mjs"),
      "--repo-root", context.sourceSnapshotRoot,
      "--output-root", outputRoot,
      "--claude-entry", context.options.claudeEntry,
      "--runtime-profile-digest", context.plan.runtime_profile_digest,
    ],
    cwd: context.repoRoot,
    hardTimeoutSeconds: stage.timeout_seconds,
    noProgressSeconds: null,
    rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
    eventWriter: context.eventWriter,
    executionId: gateExecutionId(stage, context.gateId),
    pollMilliseconds: context.policies.poll_milliseconds,
    progressAllowlistVersion: context.policies.progress_allowlist_version,
  });
  if (result.status === "ERROR") return { ...result, failure_domain: "HARNESS", code: result.termination?.trigger ?? "HOST_EVIDENCE_ERROR" };
  if (result.status !== "PASS") return { ...result, status: result.status === "INCONCLUSIVE" ? "INCONCLUSIVE" : "BLOCKED", failure_domain: "EXTERNAL", code: "HOST_CAPABILITY_FAILED" };
  let receipt;
  try { receipt = JSON.parse(fs.readFileSync(path.join(outputRoot, "host-capability-result.json"), "utf8")); } catch {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "HOST_CAPABILITY_RECEIPT_INVALID" };
  }
  if (receipt?.schema_version !== 2 || receipt.status !== "PASS" || receipt.runtime_profile_digest !== context.plan.runtime_profile_digest || receipt.client !== context.client || receipt.flat_schema !== true || receipt.flat_call !== true || receipt.client_dfx_absent !== true) {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "HOST_CAPABILITY_RECEIPT_INVALID" };
  }
  return { ...result, adapter_receipt: receipt };
}

async function serverLinuxCapability(context, stage, gate) {
  const outputRoot = gateRoot(context, stage);
  ensureDirectory(outputRoot);
  const runId = path.basename(context.attemptRoot);
  const containerName = `pltf-cap-${runId.slice(-17)}`.replace(/[^a-zA-Z0-9_.-]/g, "-");
  const resourceLabel = `problem-locator.test-flow.run=${runId}`;
  context.resources.register("container", containerName, resourceLabel);
  const result = await runProcess({
    repoRoot: context.repoRoot,
    attemptRoot: context.attemptRoot,
    stage,
    command: process.execPath,
    args: [
      path.join(context.sourceSnapshotRoot, "tools", "test-flow", "adapters", "server-linux-capability.mjs"),
      "--output-root", outputRoot,
      "--docker-context", context.options.dockerContext ?? "default",
      "--image", RELEASE_BASE_IMAGE,
      "--repo-root", context.sourceSnapshotRoot,
      "--logparse-source", context.options.logparseSource,
      "--runtime-profile-digest", context.plan.runtime_profile_digest,
      "--model", context.runtimeProfile.claude.model,
      "--service-agent-max-turns", context.runtimeProfile.real_caps.service_agent.max_turns,
      "--service-agent-max-total-tokens", context.runtimeProfile.real_caps.service_agent.max_total_tokens,
      "--service-agent-max-budget-usd", context.runtimeProfile.real_caps.service_agent.max_budget_usd,
      "--service-agent-hard-timeout-seconds", context.runtimeProfile.real_caps.service_agent.hard_timeout_seconds,
      "--container-name", containerName,
      "--resource-label", resourceLabel,
    ],
    cwd: context.repoRoot,
    hardTimeoutSeconds: stage.timeout_seconds,
    noProgressSeconds: context.policies.real_no_progress_seconds,
    rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
    eventWriter: context.eventWriter,
    executionId: gateExecutionId(stage, context.gateId),
    pollMilliseconds: context.policies.poll_milliseconds,
    progressAllowlistVersion: context.policies.progress_allowlist_version,
  });
  if (result.status === "ERROR") return { ...result, failure_domain: "HARNESS", code: result.termination?.trigger ?? "SERVER_EVIDENCE_ERROR" };
  if (result.exit_code === 2) return { ...result, status: "BLOCKED", failure_domain: "INFRA", code: "SERVER_MODEL_CAPABILITY_UNAVAILABLE" };
  if (result.status !== "PASS") return { ...result, failure_domain: "EXTERNAL", code: "SERVER_MODEL_CAPABILITY_FAILED" };
  let receipt;
  let junit;
  try {
    receipt = JSON.parse(fs.readFileSync(path.join(outputRoot, "server-linux-capability-result.json"), "utf8"));
    junit = parseJUnitSummary(path.join(outputRoot, "platform-server.xml"));
  } catch {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "SERVER_CAPABILITY_RECEIPT_INVALID" };
  }
  const claims = gate.required_claims ?? [];
  if (receipt?.schema_version !== 2 || receipt.status !== "PASS" || receipt.runtime_profile_digest !== context.plan.runtime_profile_digest || claims.some((claim) => receipt.claims?.[claim] !== "PASS") || Object.keys(receipt.claims ?? {}).some((claim) => !claims.includes(claim)) || junit.executed !== 2 || junit.passed !== 2 || junit.skipped !== 0) {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "SERVER_CAPABILITY_RECEIPT_INVALID" };
  }
  return { ...result, adapter_receipt: receipt, pytest: junit };
}

async function crossJob(context, stage) {
  const adapter = context.options.crossJobAdapter;
  if (!adapter) return { status: "BLOCKED", failure_domain: "INFRA", code: "CROSS_JOB_ADAPTER_MISSING", elapsed_seconds: 0 };
  if (!path.isAbsolute(adapter) || !fs.existsSync(adapter)) return { status: "BLOCKED", failure_domain: "INFRA", code: "CROSS_JOB_ADAPTER_INVALID", elapsed_seconds: 0 };
  const adapterArguments = [];
  const add = (name, value) => {
    if (value !== undefined && value !== null && value !== "") adapterArguments.push(name, String(value));
  };
  add("--stage", stage.id);
  add("--repo-root", context.sourceSnapshotRoot);
  add("--attempt-root", context.attemptRoot);
  add("--client", context.client);
  add("--track", context.track);
  add("--source-snapshot-digest", context.sourceSnapshotDigest);
  add("--source-snapshot-manifest", context.sourceSnapshotManifestPath);
  add("--claude-entry", context.options.claudeEntry);
  add("--claude-settings", context.options.claudeSettings);
  add("--docker-context", context.options.dockerContext ?? "default");
  add("--cache-root", context.options.cacheRoot ?? path.join(context.repoRoot, ".tmp", "test-flow-cache"));
  add("--logparse-source", context.options.logparseSource);
  add("--mcp-source", context.options.mcpSource);
  add("--base-image", RELEASE_BASE_IMAGE);
  add("--runtime-profile-digest", context.plan.runtime_profile_digest);
  add("--chrome-version", context.plan.release_inputs?.browser?.version);
  add("--chrome-sha256", context.plan.release_inputs?.browser?.executable_sha256);
  add("--gate-id", context.gateId);
  add("--resource-registry", context.resources.filePath);
  add("--resource-label", `problem-locator.test-flow.run=${path.basename(context.attemptRoot)}`);
  if (context.planStage.hard_caps) {
    add("--max-turns", context.planStage.hard_caps.max_turns);
    add("--max-total-tokens", context.planStage.hard_caps.max_total_tokens);
    add("--max-budget-usd", context.planStage.hard_caps.max_budget_usd);
    add("--hard-timeout-seconds", context.planStage.hard_caps.hard_timeout_seconds);
  }
  const serviceCaps = context.runtimeProfile.real_caps.service_agent;
  add("--service-agent-max-turns", serviceCaps.max_turns);
  add("--service-agent-max-total-tokens", serviceCaps.max_total_tokens);
  add("--service-agent-max-budget-usd", serviceCaps.max_budget_usd);
  add("--service-agent-hard-timeout-seconds", serviceCaps.hard_timeout_seconds);
  if (stage.id === "journey.cross-job.environment") adapterArguments.push("--fresh-data-root");
  if (context.restoredCheckpoint) {
    adapterArguments.push(
      "--restored-data-root", context.restoredCheckpoint.state_root,
      "--restored-continuation", context.restoredCheckpoint.continuation_path,
      "--restored-checkpoint-id", context.restoredCheckpoint.checkpoint_id,
    );
  }
  const stageIndex = context.plan.stages.findIndex((candidate) => candidate.id === stage.id);
  const laterExecutedJourney = context.plan.stages.slice(stageIndex + 1).some((candidate) =>
    candidate.decision === "RUN" && candidate.id.startsWith("journey.cross-job."));
  if (!laterExecutedJourney) adapterArguments.push("--terminal-after-stage");
  const checkpointStage = stage.id === "journey.cross-job.diagnose" ? "journey.cross-job.review" : stage.id;
  if (["journey.cross-job.route", "journey.cross-job.upload", "journey.cross-job.review", "journey.cross-job.publish-restart"].includes(checkpointStage)) {
    adapterArguments.push(
      "--checkpoint-output-source",
      path.join(context.attemptRoot, "payload", "stages", checkpointStage, "checkpoint-source.json"),
    );
  }
  const nodeAdapter = adapter.endsWith(".mjs");
  const result = await runProcess({
    repoRoot: context.repoRoot,
    attemptRoot: context.attemptRoot,
    stage,
    command: nodeAdapter ? process.execPath : adapter,
    args: nodeAdapter ? [adapter, ...adapterArguments] : adapterArguments,
    cwd: context.repoRoot,
    hardTimeoutSeconds: stage.timeout_seconds,
    noProgressSeconds: context.policies.real_no_progress_seconds,
    rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
    eventWriter: context.eventWriter,
    executionId: gateExecutionId(stage, context.gateId),
    pollMilliseconds: context.policies.poll_milliseconds,
    progressAllowlistVersion: context.policies.progress_allowlist_version,
  });
  const receiptPath = path.join(context.attemptRoot, "payload", "stages", stage.id, "adapter-result.json");
  if (!fs.existsSync(receiptPath)) {
    if (result.status === "ERROR") return { ...result, failure_domain: "HARNESS", code: result.termination?.trigger ?? "CROSS_JOB_EVIDENCE_ERROR" };
    if (result.status === "INCONCLUSIVE") return { ...result, failure_domain: "EXTERNAL", code: result.termination?.trigger ?? "EXTERNAL_INCONCLUSIVE" };
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CROSS_JOB_RECEIPT_MISSING" };
  }
  let receipt;
  try { receipt = JSON.parse(fs.readFileSync(receiptPath, "utf8")); } catch {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CROSS_JOB_RECEIPT_INVALID" };
  }
  const receiptStatuses = new Set(["PASS", "FAIL", "INCONCLUSIVE", "BLOCKED", "ERROR"]);
  if (receipt.schema_version !== 3 || !receiptStatuses.has(receipt.status) || receipt.stage_id !== stage.id || receipt.gate_id !== context.gateId || receipt.runtime_profile_digest !== context.plan.runtime_profile_digest) {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CROSS_JOB_RECEIPT_INVALID" };
  }
  if (receipt.status === "PASS" && stage.id === "journey.cross-job.upload" && receipt.browser_upload?.status !== "PASS") {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CROSS_JOB_BROWSER_UPLOAD_RECEIPT_INVALID" };
  }
  if (receipt.status === "PASS" && stage.id === "journey.cross-job.diagnose" && receipt.browser_api?.status !== "PASS") {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CROSS_JOB_BROWSER_API_RECEIPT_INVALID" };
  }
  if (result.status === "ERROR") return { ...result, failure_domain: "HARNESS", code: result.termination?.trigger ?? "CROSS_JOB_EVIDENCE_ERROR" };
  if (result.status === "INCONCLUSIVE") return { ...result, failure_domain: "EXTERNAL", code: result.termination?.trigger ?? "EXTERNAL_INCONCLUSIVE" };
  if (receipt.status !== "PASS") {
    return {
      ...result,
      status: receipt.status,
      failure_domain: receipt.failure_domain ?? "HARNESS",
      code: receipt.code ?? "CROSS_JOB_STAGE_FAILED",
      usage: receipt.usage ?? result.usage,
      usage_complete: receipt.usage_complete === true,
      effective_caps: receipt.effective_caps ?? null,
      invocations: receipt.invocations ?? [],
      adapter_receipt: {
        stage_id: receipt.stage_id,
        client_tool_calls: receipt.client_tool_calls ?? 0,
        server_tool_calls: receipt.server_tool_calls ?? 0,
        checkpoint_ready: receipt.checkpoint_ready ?? false,
        browser_upload: receipt.browser_upload ?? null,
        browser_api: receipt.browser_api ?? null,
      },
    };
  }
  if (result.status !== "PASS") return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CROSS_JOB_PROCESS_RECEIPT_MISMATCH" };
  return {
    ...result,
    usage: receipt.usage ?? result.usage,
    usage_complete: receipt.usage_complete === true,
    effective_caps: receipt.effective_caps ?? null,
    invocations: receipt.invocations ?? [],
    fresh_admission: receipt.fresh_admission ?? null,
    adapter_receipt: {
      stage_id: receipt.stage_id,
      client_tool_calls: receipt.client_tool_calls ?? 0,
      server_tool_calls: receipt.server_tool_calls ?? 0,
      checkpoint_ready: receipt.checkpoint_ready ?? false,
      restart_verified: receipt.restart_verified ?? false,
      browser_upload: receipt.browser_upload ?? null,
      browser_api: receipt.browser_api ?? null,
    },
  };
}

function reviewObservation(context) {
  const partsRoot = path.join(context.attemptRoot, "payload", "events", "parts");
  const streams = fs.existsSync(partsRoot)
    ? fs.readdirSync(partsRoot).filter((name) => name.endsWith(".journey.ndjson")).sort()
    : [];
  if (streams.length === 0) return { status: "ERROR", failure_domain: "HARNESS", code: "REVIEW_EVENT_STREAM_MISSING", elapsed_seconds: 0 };
  const events = streams.flatMap((name) => fs.readFileSync(path.join(partsRoot, name), "utf8").split("\n").filter(Boolean).map((line) => JSON.parse(line)));
  const reviewEvents = events.filter((event) => event?.data?.job_type === "REVIEW" && typeof event.job_id === "string");
  const jobIds = [...new Set(reviewEvents.map((event) => event.job_id))];
  if (jobIds.length !== 1) return { status: "FAIL", failure_domain: "CONTRACT", code: "REVIEW_JOB_IDENTITY_AMBIGUOUS", elapsed_seconds: 0 };
  const reviewJob = reviewEvents.filter((event) => event.job_id === jobIds[0]);
  const required = ["job.pending_persisted", "job.claimed", "job.outcome.produced", "job.outcome.applied"];
  const producerIds = [...new Set(reviewJob.map((event) => event.producer_id))];
  const ordinals = required.map((type) => reviewJob.find((event) => event.event_type === type)?.seq ?? null);
  const failure = reviewJob.some((event) => ["job.claim.failed", "job.outcome.rejected", "job.outcome.stale"].includes(event.event_type));
  const queued = reviewJob.some((event) => ["job.queued", "job.queue.duplicate"].includes(event.event_type));
  const ordered = producerIds.length === 1 && ordinals.every(Number.isInteger) && ordinals.every((value, index) => index === 0 || value > ordinals[index - 1]);
  const receipt = {
    schema_version: 2,
    status: queued && ordered && !failure ? "PASS" : "FAIL",
    review_job_id: jobIds[0],
    observed_review_events: reviewJob.length,
    producer_id: producerIds.length === 1 ? producerIds[0] : null,
    queued,
    ordered,
    failure_observed: failure,
  };
  writeJsonSync(path.join(gateRoot(context, { id: "journey.cross-job.review" }), "review-observation.json"), receipt);
  if (receipt.status !== "PASS") return { status: "FAIL", failure_domain: "CONTRACT", code: "REVIEW_OBSERVATION_INCOMPLETE", elapsed_seconds: 0 };
  return { status: "PASS", failure_domain: null, code: null, elapsed_seconds: 0, review_job_id: jobIds[0], observed_review_events: reviewJob.length };
}

function realEnvironment(context, profile) {
  if (profile === "real-logparse") {
    const source = context.options.logparseSource;
    const python = process.env.TEST_FLOW_LOGPARSE_PYTHON;
    if (!source || !python) return { error: "LOGPARSE_RUNTIME_MISSING" };
    return {
      env: {
        LOGPARSE_REPO: source,
        LOGPARSE_CONFIG_PATH: process.env.TEST_FLOW_LOGPARSE_CONFIG ?? path.join(source, "config.yaml"),
        LOGPARSE_PYTHON: python,
      },
    };
  }
  const command = agentCommand(
    context,
    profile === "real-skill-generation" ? "skill-generation" : "job",
  );
  if (!command) return { error: "CLAUDE_COMMAND_OR_HARD_CAP_MISSING" };
  const runtime = preparedClaudeRuntime(context);
  if (!runtime) return { error: "CLAUDE_RUNTIME_MISSING" };
  const common = {
    ...runtime.environment,
    TEST_FLOW_AGENT_BACKEND_WALL_TIME_SECONDS: String(Math.min(
      context.planStage.timeout_seconds - 30,
      context.planStage.hard_caps.hard_timeout_seconds + 30,
    )),
    S08_REAL_AGENT_COMMAND: command,
    S08_REAL_GENERIC_LOCATOR_AGENT_COMMAND: command,
    S08_REAL_SKILL_GENERATION_AGENT_COMMAND: command,
    S08_REAL_ROUTE_AGENT_COMMAND: command,
    S08_REAL_DIAGNOSE_AGENT_COMMAND: command,
    S08_REAL_REVIEW_AGENT_COMMAND: command,
  };
  if (profile === "real-agent-backend") return { env: { ...common, S08_REAL_AGENT_GATE: "1" } };
  if (profile === "real-generic-locator") {
    const skillName = "generic-problem-locator-dual-mode";
    const skillPath = path.join(
      context.repoRoot,
      "tests",
      "fixtures",
      "components",
      skillName,
    );
    if (!fs.existsSync(path.join(skillPath, "SKILL.md"))) return { error: "GENERIC_LOCATOR_SKILL_MISSING" };
    const skillRoot = path.join(runtime.config, "skills");
    const installed = path.join(skillRoot, skillName);
    ensureDirectory(skillRoot);
    if (!fs.existsSync(installed)) fs.cpSync(skillPath, installed, { recursive: true, errorOnExist: true, force: false });
    return { env: { ...common, S08_REAL_GENERIC_LOCATOR_GATE: "1" } };
  }
  if (profile === "real-skill-generation") {
    const skillName = "wiki-to-diagnosis-skill";
    const skillPath = path.join(context.repoRoot, ".claude", "skills", skillName);
    if (!fs.existsSync(path.join(skillPath, "SKILL.md"))) return { error: "WIKI_SKILL_GENERATOR_MISSING" };
    const skillRoot = path.join(runtime.config, "skills");
    const installed = path.join(skillRoot, skillName);
    ensureDirectory(skillRoot);
    if (!fs.existsSync(installed)) fs.cpSync(skillPath, installed, { recursive: true, errorOnExist: true, force: false });
    return {
      env: {
        ...common,
        S08_REAL_SKILL_GENERATION_GATE: "1",
        S08_REAL_SKILL_GENERATION_AUDIT_PATH: path.join(
          context.gateRoot,
          "scenario-evaluation-audit.json",
        ),
        S08_RELEASE_CASES_ROOT: path.join(context.repoRoot, "tests", "cases", "release"),
      },
    };
  }
  if (profile === "real-route") return { env: { ...common, S08_REAL_ROUTE_AGENT_GATE: "1" } };
  if (profile === "real-review") return { env: { ...common, S08_REAL_REVIEW_AGENT_GATE: "1" } };
  if (profile === "real-diagnose") {
    const skillPath = path.join(
      context.repoRoot,
      "tests",
      "fixtures",
      "components",
      "diagnosis-generator",
      "diagnose-service-takeover",
    );
    if (!fs.existsSync(skillPath)) return { error: "DIAGNOSE_SKILL_MISSING" };
    return {
      env: {
        ...common,
        S08_REAL_DIAGNOSE_AGENT_V3_MATRIX_GATE: "1",
        S08_REAL_FIRST_LOG_AGENT_GATE: "1",
        S08_REAL_DIAGNOSE_SKILL_PATH: skillPath,
        LOGPARSE_REPO: context.options.logparseSource ?? "",
        LOGPARSE_CONFIG_PATH: process.env.TEST_FLOW_LOGPARSE_CONFIG ?? "",
        LOGPARSE_PYTHON: process.env.TEST_FLOW_LOGPARSE_PYTHON ?? "",
      },
    };
  }
  return { error: "REAL_ENVIRONMENT_PROFILE_UNSUPPORTED" };
}

export async function executeGate(context, stage, gateId, gate) {
  const root = path.join(context.attemptRoot, "payload", "stages", stage.id, "gates", gateId);
  ensureDirectory(root);
  const scoped = { ...context, gateId, gateRoot: root };
  if (gate.kind === "node-test") return nodeTestAction(scoped, stage, gate);
  if (gate.kind === "repository-check") return repositoryCheck(scoped, stage, gate);
  if (gate.kind === "pytest") {
    let selectors = gate.selectors ?? [];
    let selection = null;
    if (gate.selector_mode === "affected") {
      selection = planAffectedSelection(context.repoRoot, context.changedFiles);
      if (selection.defer_to_full) {
        const summary = { schema_version: 2, tests: 0, passed: 0, failures: 0, errors: 0, skipped: 0, executed: 0, not_required: true };
        writeJsonSync(path.join(root, "pytest-summary.json"), summary);
        return { status: "NOT_REQUIRED", failure_domain: null, code: "AFFECTED_SCOPE_DEFERRED_TO_FULL", elapsed_seconds: 0, selection, pytest: summary };
      }
      selectors = selection.selectors;
    }
    let environment = {};
    if (gate.environment_profile) {
      const prepared = realEnvironment(scoped, gate.environment_profile);
      if (prepared.error) return { status: "BLOCKED", failure_domain: "INFRA", code: prepared.error, elapsed_seconds: 0 };
      environment = prepared.env;
    }
    const result = await pytestAction(scoped, stage, selectors, {
      extra: gate.pytest_args ?? [],
      env: environment,
      real: Boolean(gate.environment_profile),
      minPassed: gate.min_passed,
      skipPolicy: gate.skip_policy,
      selection,
      isolatedAgent: Boolean(gate.environment_profile && gate.environment_profile !== "real-logparse"),
    });
    if (gate.environment_profile && gate.environment_profile !== "real-logparse") {
      try {
        const modelUsage = collectIsolatedModelUsage(scoped, gate.environment_profile);
        return { ...result, invocations: modelUsage.invocations, usage: modelUsage.usage, usage_complete: true };
      } catch (error) {
        if (result.status !== "PASS" && error?.message === "ISOLATED_MODEL_USAGE_RECEIPT_MISSING") {
          return { ...result, invocations: [], usage_complete: false };
        }
        return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "ISOLATED_MODEL_USAGE_RECEIPT_INVALID" };
      }
    }
    return { ...result, invocations: [] };
  }
  if (gate.kind === "capability-adapter") {
    if (gate.adapter === "host-capability") return hostCapability(scoped, stage);
    if (gate.adapter === "server-linux-capability") return serverLinuxCapability(scoped, stage, gate);
  }
  if (gate.kind === "cross-job-adapter") return crossJob(scoped, stage);
  if (gate.kind === "observation" && gate.observation === "review-state-transition") return reviewObservation(scoped);
  return { status: "ERROR", failure_domain: "HARNESS", code: "GATE_EXECUTOR_NOT_IMPLEMENTED", elapsed_seconds: 0 };
}

export { affectedSelectors, pythonRuntime };
