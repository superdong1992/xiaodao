import fs from "node:fs";
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

function pythonRuntime(repoRoot) {
  return resolvePythonTestRuntime(repoRoot);
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

async function pytestAction(context, stage, selectors, { extra = [], env = {}, real = false } = {}) {
  const runtime = pythonRuntime(context.repoRoot);
  if (!runtime) {
    return { status: "BLOCKED", failure_domain: "INFRA", code: "PYTHON_312_TEST_RUNTIME_MISSING", elapsed_seconds: 0 };
  }
  if (selectors.length === 0) return { status: "NOT_REQUIRED", failure_domain: null, elapsed_seconds: 0 };
  const scratch = path.join(context.attemptRoot, "scratch", stage.id);
  const stageEvidence = path.join(context.attemptRoot, "payload", "stages", stage.id);
  ensureDirectory(scratch);
  ensureDirectory(stageEvidence);
  const loopback = probeLoopbackCapability(runtime, context.repoRoot);
  writeJsonSync(path.join(stageEvidence, "loopback-capability.json"), loopback);
  if (loopback.status !== "PASS") {
    removeTreeWritable(scratch, context.attemptRoot);
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
    `--basetemp=${path.join(scratch, "pytest")}`,
    `--junitxml=${path.join(stageEvidence, "pytest.xml")}`,
    ...extra,
  ];
  const result = await runProcess({
    repoRoot: context.repoRoot,
    attemptRoot: context.attemptRoot,
    stage,
    command: runtime.command,
    args,
    cwd: context.repoRoot,
    env: {
      PYTHONNOUSERSITE: "1",
      PYTHONPYCACHEPREFIX: path.join(scratch, "pycache"),
      ...env,
    },
    hardTimeoutSeconds: stage.timeout_seconds,
    noProgressSeconds: real ? context.policies.real_no_progress_seconds : null,
    rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
    eventWriter: context.eventWriter,
  });
  removeTreeWritable(scratch, context.attemptRoot);
  if (result.status === "ERROR") return { ...result, failure_domain: "HARNESS", code: result.termination?.trigger ?? "PROCESS_EVIDENCE_ERROR" };
  if (result.status === "INCONCLUSIVE") return { ...result, failure_domain: "EXTERNAL", code: result.termination.trigger };
  if (result.status !== "PASS") return { ...result, failure_domain: real ? "CONTRACT" : "PRODUCT", code: "PYTEST_FAILED" };
  return { ...result, failure_domain: null };
}

function agentCommand() {
  if (process.env.TEST_FLOW_AGENT_COMMAND) return process.env.TEST_FLOW_AGENT_COMMAND;
  const executable = resolveCommand("claude") ?? resolveCommand("claude.exe");
  if (!executable) return null;
  const quoted = /\s/.test(executable) ? `"${executable.replaceAll('"', '\\"')}"` : executable;
  return `${quoted} -p --no-session-persistence --dangerously-skip-permissions --tools Read,Write`;
}

async function hostCapability(context, stage) {
  const outputRoot = path.join(context.attemptRoot, "payload", "stages", stage.id);
  ensureDirectory(outputRoot);
  const result = await runProcess({
    repoRoot: context.repoRoot,
    attemptRoot: context.attemptRoot,
    stage,
    command: process.execPath,
    args: [path.join(context.repoRoot, "tools", "test-flow", "adapters", "host-capability.mjs"), "--repo-root", context.repoRoot, "--output-root", outputRoot],
    cwd: context.repoRoot,
    hardTimeoutSeconds: stage.timeout_seconds,
    noProgressSeconds: null,
    rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
    eventWriter: context.eventWriter,
  });
  if (result.status === "ERROR") return { ...result, failure_domain: "HARNESS", code: result.termination?.trigger ?? "HOST_EVIDENCE_ERROR" };
  if (result.status !== "PASS") return { ...result, status: result.status === "INCONCLUSIVE" ? "INCONCLUSIVE" : "BLOCKED", failure_domain: "EXTERNAL", code: "HOST_CAPABILITY_FAILED" };
  return result;
}

async function serverLinuxCapability(context, stage) {
  const outputRoot = path.join(context.attemptRoot, "payload", "stages", stage.id);
  ensureDirectory(outputRoot);
  const result = await runProcess({
    repoRoot: context.repoRoot,
    attemptRoot: context.attemptRoot,
    stage,
    command: process.execPath,
    args: [path.join(context.repoRoot, "tools", "test-flow", "adapters", "server-linux-capability.mjs"), "--output-root", outputRoot],
    cwd: context.repoRoot,
    hardTimeoutSeconds: stage.timeout_seconds,
    noProgressSeconds: context.policies.real_no_progress_seconds,
    rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
    eventWriter: context.eventWriter,
  });
  if (result.status === "ERROR") return { ...result, failure_domain: "HARNESS", code: result.termination?.trigger ?? "SERVER_EVIDENCE_ERROR" };
  if (result.exit_code === 2) return { ...result, status: "BLOCKED", failure_domain: "INFRA", code: "SERVER_MODEL_CAPABILITY_UNAVAILABLE" };
  if (result.status !== "PASS") return { ...result, failure_domain: "EXTERNAL", code: "SERVER_MODEL_CAPABILITY_FAILED" };
  return result;
}

async function isolatedAgent(context, stage, selector, switchName) {
  const command = agentCommand();
  if (!command) return { status: "BLOCKED", failure_domain: "INFRA", code: "CLAUDE_COMMAND_MISSING", elapsed_seconds: 0 };
  return pytestAction(context, stage, [selector], {
    real: true,
    env: { [switchName]: "1", S08_REAL_AGENT_COMMAND: command, S08_REAL_ROUTE_AGENT_COMMAND: command, S08_REAL_DIAGNOSE_AGENT_COMMAND: command, S08_REAL_REVIEW_AGENT_COMMAND: command },
  });
}

async function realDiagnose(context, stage) {
  const command = agentCommand();
  if (!command) return { status: "BLOCKED", failure_domain: "INFRA", code: "CLAUDE_COMMAND_MISSING", elapsed_seconds: 0 };
  const skillPath = path.join(context.repoRoot, ".claude", "skills", "diagnose-service-takeover");
  if (!fs.existsSync(skillPath)) return { status: "BLOCKED", failure_domain: "INFRA", code: "DIAGNOSE_SKILL_MISSING", elapsed_seconds: 0 };
  return pytestAction(context, stage, [
    "tests/real/agent/test_real_diagnose_agent_contract_gate.py::test_real_agent_v3_requirement_isolation_gate",
    "tests/real/agent/test_real_diagnose_agent_contract_gate.py::test_real_first_log_diagnose_agent_produces_valid_continuation",
  ], {
    real: true,
    env: {
      S08_REAL_DIAGNOSE_AGENT_V3_MATRIX_GATE: "1",
      S08_REAL_FIRST_LOG_AGENT_GATE: "1",
      S08_REAL_DIAGNOSE_AGENT_COMMAND: command,
      S08_REAL_DIAGNOSE_SKILL_PATH: skillPath,
      LOGPARSE_REPO: context.options.logparseSource ?? "",
      LOGPARSE_CONFIG_PATH: process.env.TEST_FLOW_LOGPARSE_CONFIG ?? "",
      LOGPARSE_PYTHON: process.env.TEST_FLOW_LOGPARSE_PYTHON ?? "",
    },
  });
}

async function realLogparse(context, stage) {
  const source = context.options.logparseSource;
  const python = process.env.TEST_FLOW_LOGPARSE_PYTHON;
  if (!source || !python) return { status: "BLOCKED", failure_domain: "INFRA", code: "LOGPARSE_RUNTIME_MISSING", elapsed_seconds: 0 };
  return pytestAction(context, stage, ["tests/real/logparse/test_logparse_real_e2e.py"], {
    real: true,
    extra: ["--run-real-logparse"],
    env: { LOGPARSE_REPO: source, LOGPARSE_CONFIG_PATH: process.env.TEST_FLOW_LOGPARSE_CONFIG ?? path.join(source, "config.yaml"), LOGPARSE_PYTHON: python },
  });
}

async function crossJob(context, stage) {
  const adapter = context.options.crossJobAdapter;
  if (!adapter) return { status: "BLOCKED", failure_domain: "INFRA", code: "CROSS_JOB_ADAPTER_MISSING", elapsed_seconds: 0 };
  if (!path.isAbsolute(adapter) || !fs.existsSync(adapter)) return { status: "BLOCKED", failure_domain: "INFRA", code: "CROSS_JOB_ADAPTER_INVALID", elapsed_seconds: 0 };
  const adapterArguments = [
    "--stage", stage.id,
    "--attempt-root", context.attemptRoot,
    "--client", context.client,
    "--resource-registry", context.resources.filePath,
    "--resource-label", `problem-locator.test-flow.run=${path.basename(context.attemptRoot)}`,
  ];
  if (stage.id === "journey.cross-job.environment") adapterArguments.push("--fresh-data-root");
  if (context.restoredCheckpoint) {
    adapterArguments.push(
      "--restored-data-root", context.restoredCheckpoint.state_root,
      "--restored-continuation", context.restoredCheckpoint.continuation_path,
      "--restored-checkpoint-id", context.restoredCheckpoint.checkpoint_id,
    );
  }
  const checkpointStage = stage.id === "journey.cross-job.diagnose" ? "journey.cross-job.review" : stage.id;
  if (["journey.cross-job.route", "journey.cross-job.upload", "journey.cross-job.review", "journey.cross-job.publish-restart"].includes(checkpointStage)) {
    adapterArguments.push(
      "--checkpoint-output-source",
      path.join(context.attemptRoot, "payload", "stages", checkpointStage, "checkpoint-source.json"),
    );
  }
  const result = await runProcess({
    repoRoot: context.repoRoot,
    attemptRoot: context.attemptRoot,
    stage,
    command: adapter,
    args: adapterArguments,
    cwd: context.repoRoot,
    hardTimeoutSeconds: stage.timeout_seconds,
    noProgressSeconds: context.policies.real_no_progress_seconds,
    rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
    eventWriter: context.eventWriter,
  });
  if (result.status === "ERROR") return { ...result, failure_domain: "HARNESS", code: result.termination?.trigger ?? "CROSS_JOB_EVIDENCE_ERROR" };
  if (result.status === "INCONCLUSIVE") return { ...result, failure_domain: "EXTERNAL", code: result.termination?.trigger ?? "EXTERNAL_INCONCLUSIVE" };
  if (result.status !== "PASS") return { ...result, failure_domain: "PRODUCT", code: "CROSS_JOB_STAGE_FAILED" };
  return result;
}

async function rolloutParity(context, stage) {
  const spec = context.options.rolloutParitySpec;
  if (!spec) return { status: "BLOCKED", failure_domain: "INFRA", code: "ROLLOUT_PARITY_SPEC_REQUIRED", elapsed_seconds: 0 };
  if (!path.isAbsolute(spec) || !fs.existsSync(spec)) {
    return { status: "BLOCKED", failure_domain: "INFRA", code: "ROLLOUT_PARITY_SPEC_INVALID", elapsed_seconds: 0 };
  }
  const outputRoot = path.join(context.attemptRoot, "payload", "stages", stage.id);
  ensureDirectory(outputRoot);
  const result = await runProcess({
    repoRoot: context.repoRoot,
    attemptRoot: context.attemptRoot,
    stage,
    command: process.execPath,
    args: [
      path.join(context.repoRoot, "tools", "test-flow", "adapters", "rollout-parity.mjs"),
      "--repo-root", context.repoRoot,
      "--attempt-root", context.attemptRoot,
      "--output-root", outputRoot,
      "--spec", spec,
      "--expected-source-commit", context.sourceHead,
      "--expected-producer-identity", context.planStage.producer_identity,
    ],
    cwd: context.repoRoot,
    hardTimeoutSeconds: stage.timeout_seconds,
    // Each child release owns its semantic 360-second no-progress guard.  The
    // outer migration pair must not invent heartbeat progress on their behalf.
    noProgressSeconds: null,
    rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
    eventWriter: context.eventWriter,
  });
  const receiptPath = path.join(outputRoot, "rollout-parity-receipt.json");
  if (!fs.existsSync(receiptPath)) {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "ROLLOUT_PARITY_RECEIPT_MISSING" };
  }
  let receipt;
  try { receipt = JSON.parse(fs.readFileSync(receiptPath, "utf8")); } catch {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "ROLLOUT_PARITY_RECEIPT_INVALID" };
  }
  const valid = receipt.schema_version === 1
    && receipt.source_commit === context.sourceHead
    && receipt.producer_identity === context.planStage.producer_identity
    && Array.isArray(receipt.runs)
    && receipt.runs.length === 2
    && receipt.runs[0]?.label === "legacy"
    && receipt.runs[1]?.label === "candidate";
  if (!valid) return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "ROLLOUT_PARITY_RECEIPT_INVALID" };
  if (result.status !== "PASS" || receipt.status !== "PASS" || receipt.runs.some((run) => run.exit_code !== 0)) {
    return { ...result, status: "FAIL", failure_domain: "CONTRACT", code: "ROLLOUT_PARITY_FAILED", parity_receipt: receipt };
  }
  return { ...result, parity_receipt: receipt };
}

function reviewObservation(context) {
  const stream = path.join(context.attemptRoot, "payload", "events", "service-linux.journey.ndjson");
  if (!fs.existsSync(stream)) return { status: "ERROR", failure_domain: "HARNESS", code: "REVIEW_EVENT_STREAM_MISSING", elapsed_seconds: 0 };
  const events = fs.readFileSync(stream, "utf8").split("\n").filter(Boolean).map((line) => JSON.parse(line));
  const reviewEvents = events.filter((event) => event?.data?.job_type === "REVIEW" && typeof event.job_id === "string");
  const jobIds = [...new Set(reviewEvents.map((event) => event.job_id))];
  if (jobIds.length !== 1) return { status: "FAIL", failure_domain: "CONTRACT", code: "REVIEW_JOB_IDENTITY_AMBIGUOUS", elapsed_seconds: 0 };
  const reviewJob = reviewEvents.filter((event) => event.job_id === jobIds[0]);
  const required = ["job.pending_persisted", "job.claimed", "job.outcome.produced", "job.outcome.applied"];
  const ordinals = required.map((type) => reviewJob.find((event) => event.event_type === type)?.seq ?? null);
  const failure = reviewJob.some((event) => ["job.claim.failed", "job.outcome.rejected", "job.outcome.stale"].includes(event.event_type));
  const queued = reviewJob.some((event) => ["job.queued", "job.queue.duplicate"].includes(event.event_type));
  const ordered = ordinals.every(Number.isInteger) && ordinals.every((value, index) => index === 0 || value > ordinals[index - 1]);
  if (!queued || !ordered || failure) return { status: "FAIL", failure_domain: "CONTRACT", code: "REVIEW_OBSERVATION_INCOMPLETE", elapsed_seconds: 0 };
  return { status: "PASS", failure_domain: null, code: null, elapsed_seconds: 0, review_job_id: jobIds[0], observed_review_events: reviewJob.length };
}

export async function executeAction(context, stage) {
  switch (stage.action) {
    case "framework_self_test": {
      const testRoot = path.join(context.repoRoot, "tools", "test-flow", "tests");
      const tests = fs.readdirSync(testRoot).filter((name) => name.endsWith(".test.mjs")).sort().map((name) => path.join(testRoot, name));
      return runProcess({ repoRoot: context.repoRoot, attemptRoot: context.attemptRoot, stage, command: process.execPath, args: ["--test", ...tests], cwd: context.repoRoot, hardTimeoutSeconds: stage.timeout_seconds, noProgressSeconds: null, rawLogLimitBytes: context.policies.raw_log_file_limit_bytes, eventWriter: context.eventWriter });
    }
    case "deterministic_affected": {
      const selection = planAffectedSelection(context.repoRoot, context.changedFiles);
      if (selection.defer_to_full) {
        return {
          status: "NOT_REQUIRED",
          failure_domain: null,
          code: "AFFECTED_SCOPE_DEFERRED_TO_FULL",
          elapsed_seconds: 0,
          selection,
        };
      }
      const result = await pytestAction(context, stage, selection.selectors);
      return { ...result, selection };
    }
    case "deterministic_full":
      return pytestAction(context, stage, ["tests/deterministic"]);
    case "host_capability":
      return hostCapability(context, stage);
    case "server_linux_capability":
      return serverLinuxCapability(context, stage);
    case "real_logparse":
      return realLogparse(context, stage);
    case "real_agent_backend":
      return isolatedAgent(context, stage, "tests/real/agent/test_real_agent_backend_gate.py", "S08_REAL_AGENT_GATE");
    case "real_route":
      return isolatedAgent(context, stage, "tests/real/agent/test_real_route_agent_contract_gate.py", "S08_REAL_ROUTE_AGENT_GATE");
    case "real_diagnose":
      return realDiagnose(context, stage);
    case "real_review":
      return isolatedAgent(context, stage, "tests/real/agent/test_real_review_agent_contract_gate.py", "S08_REAL_REVIEW_AGENT_GATE");
    case "journey_environment":
    case "journey_route":
    case "journey_upload":
    case "journey_diagnose":
    case "journey_publish_restart":
      return crossJob(context, stage);
    case "journey_review_audit":
      return reviewObservation(context);
    case "rollout_parity_once":
      return rolloutParity(context, stage);
    case "finalize":
      return { status: "PASS", elapsed_seconds: 0, failure_domain: null };
    default:
      return { status: "ERROR", failure_domain: "HARNESS", code: "ACTION_NOT_IMPLEMENTED", elapsed_seconds: 0 };
  }
}

export { affectedSelectors, pythonRuntime };
