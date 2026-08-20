import fs from "node:fs";
import path from "node:path";
import {
  atomicCreateJson,
  canonicalJson,
  ensureDirectory,
  readJson,
  sha256Bytes,
  sha256File,
  writeJsonSync,
} from "./util.mjs";
import { buildWaterfallSummary, readRelayedEventPart, validateEventFile } from "./events.mjs";
import { classifyRun } from "./status.mjs";
import {
  ISOLATED_AGENT_ENV_POLICY_VERSION,
  ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT,
  ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_ENFORCEMENT,
  ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY,
  validEnvironmentKeySummary,
} from "../runtime-support/isolated-agent-env.mjs";
import {
  validSkillGenerationFailedTraceAuditReceipt,
  validSkillGenerationIncompleteAuditRejectedReceipt,
  validSkillGenerationIncompleteTraceAuditReceipt,
  validSkillGenerationPartialTraceAuditReceipt,
  validSkillGenerationTraceAuditReceipt,
  validIsolatedAgentStreamEventType,
} from "../runtime-support/isolated-agent-tool-audit.mjs";
import {
  isCompleteUsage,
  normalizeUsage as normalizeTokenUsage,
  sumUsage as sumTokenUsage,
  TOKEN_USAGE_FORMULA,
  zeroUsage,
} from "./usage.mjs";

const SECRET_PATTERNS = [
  { code: "ANTHROPIC_KEY", expression: /sk-ant-[A-Za-z0-9_-]{16,}/g },
  { code: "BEARER_TOKEN", expression: /Bearer\s+[A-Za-z0-9._~+/=-]{16,}/gi },
  { code: "PRIVATE_KEY", expression: /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/g },
  { code: "GENERIC_SECRET", expression: /(?:api[_-]?key|auth[_-]?token|password|client[_-]?secret)\s*[:=]\s*["']?[A-Za-z0-9._~+/=-]{16,}/gi },
];

function listFiles(root, relative = "", output = [], { exclude = new Set() } = {}) {
  if (!fs.existsSync(root)) return output;
  const entries = fs.readdirSync(path.join(root, relative), { withFileTypes: true }).sort((left, right) => left.name.localeCompare(right.name));
  for (const entry of entries) {
    const child = relative ? `${relative}/${entry.name}` : entry.name;
    if (exclude.has(child)) continue;
    if (entry.isSymbolicLink()) output.push({ path: child, kind: "symlink", target: fs.readlinkSync(path.join(root, child)) });
    else if (entry.isDirectory()) listFiles(root, child, output, { exclude });
    else if (entry.isFile()) {
      const filePath = path.join(root, child);
      output.push({ path: child, kind: "file", size: fs.statSync(filePath).size, sha256: sha256File(filePath) });
    } else output.push({ path: child, kind: "unsupported" });
  }
  return output;
}

function scanFile(filePath, relativePath, knownSecrets) {
  const buffer = fs.readFileSync(filePath);
  const text = buffer.toString("utf8");
  const hits = [];
  for (const pattern of SECRET_PATTERNS) {
    pattern.expression.lastIndex = 0;
    if (pattern.expression.test(text)) hits.push({ path: relativePath, code: pattern.code });
  }
  for (const secret of knownSecrets.filter((value) => typeof value === "string" && value.length >= 8)) {
    if (buffer.includes(Buffer.from(secret))) hits.push({ path: relativePath, code: "KNOWN_SECRET" });
  }
  return hits;
}

export function scanPayload(payloadRoot, {
  knownSecrets = [],
  scannerVersion = "test-flow-secret-scan-v2",
  exclude = [],
} = {}) {
  const files = listFiles(payloadRoot, "", [], { exclude: new Set(exclude) });
  const hits = [];
  for (const entry of files) if (entry.kind === "file") hits.push(...scanFile(path.join(payloadRoot, entry.path), entry.path, knownSecrets));
  const manifest = files.map(({ path: filePath, kind, size, sha256, target }) => ({ path: filePath, kind, size, sha256, target }));
  return {
    schema_version: 2,
    status: hits.length === 0 ? "PASS" : "FAIL",
    scanner: scannerVersion,
    scanned_root_digest: sha256Bytes(canonicalJson(manifest)),
    files_scanned: files.filter((entry) => entry.kind === "file").length,
    sensitive_value_occurrences: hits.length,
    hits,
  };
}

export function sealPayload(payloadRoot) {
  const files = listFiles(payloadRoot);
  const invalid = files.filter((entry) => entry.kind !== "file");
  return {
    schema_version: 2,
    status: invalid.length === 0 ? "PASS" : "FAIL",
    root_digest: sha256Bytes(canonicalJson(files)),
    files,
    invalid_entries: invalid.map((entry) => entry.path),
  };
}

export function verifyPayloadSeal(payloadRoot, seal) {
  const current = sealPayload(payloadRoot);
  return {
    status: current.status === "PASS" && seal.schema_version === 2 && seal.status === "PASS" && current.root_digest === seal.root_digest ? "PASS" : "FAIL",
    expected_digest: seal.root_digest,
    actual_digest: current.root_digest,
  };
}

export function createAttempt({ evidenceRoot, runId }) {
  ensureDirectory(evidenceRoot);
  const attemptRoot = path.join(evidenceRoot, runId);
  fs.mkdirSync(attemptRoot, { recursive: false, mode: 0o700 });
  for (const child of ["payload", "payload/events", "payload/logs", "payload/stages", "payload/checkpoints", "finalization"]) ensureDirectory(path.join(attemptRoot, child));
  writeJsonSync(path.join(attemptRoot, "attempt.json"), {
    schema_version: 2,
    kind: "immutable-attempt-manifest",
    run_id: runId,
    created_at_utc: new Date().toISOString(),
  });
  return attemptRoot;
}

function passedExecutedGate(gate) {
  return gate.status === "PASS" && gate.result_source !== "REUSED";
}

function crossJobGate(gate) {
  const contract = gate.evidence_contract;
  return contract !== null
    && typeof contract === "object"
    && !Array.isArray(contract)
    && typeof contract.id === "string"
    && contract.id.startsWith("cross-job-")
    && contract.event_stream !== null
    && typeof contract.event_stream === "object";
}

function eventFile(contract, mode) {
  return `parts/service-linux.${contract.event_stream.instance}.${mode}.ndjson`;
}

export function requiredEventFiles(gatesOrStages = []) {
  const gates = gatesOrStages.flatMap((entry) => entry.gates ? entry.gates.map((gate) => ({ stage_id: entry.id, ...gate })) : [entry]);
  const required = new Set(["orchestrator.ndjson"]);
  const passedJourney = gates.filter((gate) => passedExecutedGate(gate) && crossJobGate(gate));
  for (const gate of passedJourney) {
    for (const mode of gate.evidence_contract.event_stream.pass_requires) required.add(eventFile(gate.evidence_contract, mode));
  }
  return [...required].sort();
}

export function allowedEmptyEventFiles(gatesOrStages = []) {
  const gates = gatesOrStages.flatMap((entry) => entry.gates ? entry.gates.map((gate) => ({ stage_id: entry.id, ...gate })) : [entry]);
  const allowed = new Set();
  const requiredNonempty = new Set();
  const attemptedCrossJob = gates.filter((gate) => crossJobGate(gate) && gate.result_source !== "REUSED");
  for (const gate of attemptedCrossJob) {
    const stream = gate.evidence_contract.event_stream;
    if (passedExecutedGate(gate)) {
      for (const mode of stream.pass_allows_empty) allowed.add(eventFile(gate.evidence_contract, mode));
      for (const mode of stream.pass_requires.filter((candidate) => !stream.pass_allows_empty.includes(candidate))) requiredNonempty.add(eventFile(gate.evidence_contract, mode));
    } else {
      for (const mode of stream.failure_allows_empty) allowed.add(eventFile(gate.evidence_contract, mode));
    }
  }
  for (const name of requiredNonempty) allowed.delete(name);
  return [...allowed].sort();
}

function listEventFiles(root, relative = "", output = []) {
  if (!fs.existsSync(root)) return output;
  for (const entry of fs.readdirSync(path.join(root, relative), { withFileTypes: true }).sort((left, right) => left.name.localeCompare(right.name))) {
    const child = relative ? `${relative}/${entry.name}` : entry.name;
    if (entry.isDirectory()) listEventFiles(root, child, output);
    else if (entry.isFile() && child.endsWith(".ndjson")) output.push(child);
  }
  return output;
}

export function validateEvidenceStreams(attemptRoot, { requiredFiles = ["orchestrator.ndjson"], allowedEmptyFiles = [] } = {}) {
  const eventsRoot = path.join(attemptRoot, "payload", "events");
  const results = [];
  let status = "PASS";
  const allowedEmpty = new Set(allowedEmptyFiles);
  for (const name of listEventFiles(eventsRoot)) {
    try {
      const filePath = path.join(eventsRoot, name);
      const part = /^parts\/service-linux\.([a-z]+)\.(journey|diagnostics)\.ndjson$/.exec(name);
      if (part) {
        const [, instance, mode] = part;
        const receiptPath = path.join(attemptRoot, "payload", `service-${instance}-${mode}-relay.json`);
        const relayed = readRelayedEventPart({
          filePath,
          receiptPath,
          expectedProducerId: mode === "journey" ? `service-linux-${instance}` : `service-linux-diagnostics-${instance}`,
          expectedRunId: path.basename(attemptRoot),
          allowEmpty: allowedEmpty.has(name),
        });
        results.push({ file: name, status: "PASS", event_count: relayed.event_count, producer_id: relayed.receipt.producer_id, producer_type: "service", run_id: path.basename(attemptRoot), clock_domain: relayed.receipt.clock_domain, raw_sha256: relayed.receipt.raw_sha256, events_sha256: relayed.receipt.events_sha256 });
      } else {
        results.push({ file: name, ...validateEventFile(filePath) });
      }
    }
      catch (error) {
        status = "FAIL";
        results.push({ file: name, status: "FAIL", code: error?.code ?? "EVENT_STREAM_INVALID" });
      }
  }
  const present = new Set(results.filter((result) => result.status === "PASS").map((result) => result.file));
  const missing = requiredFiles.filter((name) => !present.has(name));
  if (missing.length > 0) status = "FAIL";
  return { schema_version: 2, status, required_files: requiredFiles, allowed_empty_files: [...allowedEmpty].sort(), missing_files: missing, streams: results };
}

async function awaitRequiredEventVisibility(attemptRoot, requiredFiles, seconds) {
  const root = path.join(attemptRoot, "payload", "events");
  const deadline = Date.now() + Math.max(0, seconds) * 1000;
  for (;;) {
    const missing = requiredFiles.filter((relative) => !fs.existsSync(path.join(root, relative)));
    if (missing.length === 0 || Date.now() >= deadline) return missing;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

export function recoverStageAuditProgress({ attemptRoot, stageRoot, stageId }) {
  const progress = { client_tool_calls: 0, server_tool_calls: 0, usage: zeroUsage() };
  const resolvedAttempt = path.resolve(attemptRoot);
  const resolvedStage = path.resolve(stageRoot);
  if (!resolvedStage.startsWith(`${resolvedAttempt}${path.sep}`) || !fs.existsSync(resolvedStage)) return progress;
  for (const name of fs.readdirSync(resolvedStage).filter((entry) => entry.endsWith(".authoritative.json")).sort()) {
    try {
      const audit = readJson(path.join(resolvedStage, name));
      if (!Array.isArray(audit.records)) continue;
      progress.client_tool_calls += audit.records.length;
      progress.usage = sumTokenUsage([progress.usage, audit.usage]);
    } catch {}
  }
  const instance = new Map([
    ["journey.cross-job.route", "route"],
    ["journey.cross-job.upload", "upload"],
    ["journey.cross-job.diagnose", "diagnose"],
    ["journey.cross-job.publish-restart", "restart"],
  ]).get(stageId);
  if (!instance) return progress;
  try {
    const supervisor = readJson(path.join(resolvedAttempt, "payload", `service-${instance}-supervisor.json`));
    if (supervisor.status !== "PASS") return progress;
    const eventsPath = path.join(resolvedAttempt, "payload", "events", "parts", `service-linux.${instance}.diagnostics.ndjson`);
    validateEventFile(eventsPath);
    const events = fs.readFileSync(eventsPath, "utf8").split("\n").filter(Boolean).map((line) => JSON.parse(line));
    progress.server_tool_calls = events.filter((event) => event.event_type === "mcp.tool.completed").length;
  } catch {}
  return progress;
}

function comparableStage(stage) {
  const { stage_receipt_path, stage_receipt_digest, ...receipt } = stage;
  return receipt;
}

function comparableGate(gate) {
  return {
    id: gate.gate_id,
    kind: gate.gate_kind,
    status: gate.status,
    code: gate.code,
    failure_domain: gate.failure_domain,
    gate_identity: gate.gate_identity,
    definition_digest: gate.definition_digest,
    evidence_contract: gate.evidence_contract,
    runtime_profile: gate.runtime_profile,
    runtime_profile_digest: gate.runtime_profile_digest,
    receipt_path: gate.receipt_path,
    receipt_digest: gate.receipt_digest,
    elapsed_seconds: gate.elapsed_seconds,
    usage: gate.usage,
    usage_complete: gate.usage_complete,
    effective_caps: gate.effective_caps,
    model_invocations: gate.model_invocations,
    fresh_admission: gate.fresh_admission,
    evidence: gate.evidence,
  };
}

const CANDIDATE_FIELDS = Object.freeze([
  "schema_version", "run_id", "track", "goal", "functional_status", "performance_status",
  "operation_status", "failure_domain", "failure_fingerprint", "proofs", "stages", "gates",
  "source", "config_digests", "config_bundle_digest", "runtime_profile",
  "runtime_profile_digest", "plan_fingerprint", "policy_digest", "status_policy", "lineage",
  "admission", "pre_finalization_resource_receipt", "usage", "candidate_input_digest",
]);

function normalizedUsage(value) {
  return normalizeTokenUsage(value);
}

function sumUsage(values) {
  return sumTokenUsage(values);
}

function validUsage(value) {
  return isCompleteUsage(value);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function exactKeys(value, expected) {
  return isPlainObject(value)
    && Object.keys(value).sort().join("\0") === [...expected].sort().join("\0");
}

const ISOLATED_INVOCATION_FIELDS = Object.freeze([
  "schema_version", "invocation_id", "class", "workflow", "environment_policy",
  "tool_trace_audit", "effective_model", "effective_caps", "usage_complete", "usage",
  "terminal", "turns", "stream", "wrapper_outcome", "hard_cap_enforcement",
  "timed_out", "process",
]);

function validIsolatedEnvironmentPolicy(value) {
  return exactKeys(value, [
    "schema_version", "version", "provider_auth_source", "session_credentials",
    "inbound", "claude_process",
  ])
    && value.schema_version === 1
    && value.version === ISOLATED_AGENT_ENV_POLICY_VERSION
    && value.provider_auth_source === "audited-settings-file"
    && ["NONE", "explicit-logparse-broker"].includes(value.session_credentials)
    && validEnvironmentKeySummary(value.inbound)
    && validEnvironmentKeySummary(value.claude_process);
}

function validIsolatedProcess(value) {
  if (!exactKeys(value, ["exit_code", "signal", "wrapper_exit_code"])
    || !Number.isSafeInteger(value.wrapper_exit_code)) return false;
  const exited = Number.isSafeInteger(value.exit_code) && value.signal === null;
  const signaled = value.exit_code === null
    && typeof value.signal === "string"
    && /^[A-Z][A-Z0-9_]{1,31}$/.test(value.signal);
  return exited || signaled;
}

function validIsolatedStreamShape(value) {
  return exactKeys(value, [
    "schema_version", "event_count", "parsed_event_count", "init_count",
    "result_count", "last_event_type", "complete",
  ])
    && value.schema_version === 1
    && Number.isSafeInteger(value.event_count)
    && value.event_count >= 0
    && Number.isSafeInteger(value.parsed_event_count)
    && value.parsed_event_count >= 0
    && value.parsed_event_count <= value.event_count
    && Number.isSafeInteger(value.init_count)
    && value.init_count >= 0
    && Number.isSafeInteger(value.result_count)
    && value.result_count >= 0
    && (value.last_event_type === null
      ? value.event_count === 0 && value.parsed_event_count === 0
      : validIsolatedAgentStreamEventType(value.last_event_type))
    && typeof value.complete === "boolean";
}

function validCompleteIsolatedStream(value) {
  return validIsolatedStreamShape(value)
    && value.complete === true
    && value.event_count === value.parsed_event_count
    && value.event_count >= 2
    && value.init_count === 1
    && value.result_count === 1
    && value.last_event_type === "result";
}

function validIsolatedTerminal(value) {
  return exactKeys(value, ["subtype", "is_error"])
    && typeof value.subtype === "string"
    && /^[a-z][a-z0-9_]{0,63}$/.test(value.subtype)
    && typeof value.is_error === "boolean";
}

function validIsolatedWrapperOutcome(value) {
  return exactKeys(value, ["schema_version", "status", "code"])
    && value.schema_version === 1
    && (
      (value.status === "PASS" && value.code === null)
      || (value.status === "FAIL" && /^WRAPPER_[A-Z0-9_]+$/.test(value.code ?? ""))
    );
}

function validIsolatedCaps(value) {
  return isPlainObject(value)
    && Object.keys(value).every((key) => [
      "max_turns", "max_total_tokens", "max_output_tokens", "max_budget_usd",
      "hard_timeout_seconds",
    ].includes(key))
    && Number.isSafeInteger(value.max_turns)
    && value.max_turns > 0
    && Number.isSafeInteger(value.max_total_tokens)
    && value.max_total_tokens > 0
    && (value.max_output_tokens === undefined || (
      Number.isSafeInteger(value.max_output_tokens)
      && value.max_output_tokens > 0
      && value.max_output_tokens <= value.max_total_tokens
    ))
    && Number.isFinite(value.max_budget_usd)
    && value.max_budget_usd > 0
    && Number.isSafeInteger(value.hard_timeout_seconds)
    && value.hard_timeout_seconds > 0;
}

function validIsolatedHardCapEvidence(invocation, caps) {
  if (!validIsolatedCaps(caps)) return false;
  const enforcement = invocation.hard_cap_enforcement;
  if (!isPlainObject(enforcement)
    || !Object.keys(enforcement).every((key) => [
      "turns", "cost_usd", "hard_timeout_seconds", "total_tokens", "max_output_tokens",
      "structured_output_retries",
    ].includes(key))
    || enforcement.turns !== "claude-cli"
    || enforcement.cost_usd !== "claude-cli"
    || enforcement.hard_timeout_seconds !== "wrapper-process-watchdog"
    || enforcement.total_tokens !== `terminal-usage-postcondition:${TOKEN_USAGE_FORMULA}`) return false;
  return validOutputTokenCapEvidence(invocation, caps)
    && validStructuredOutputRetryEvidence(invocation);
}

function validCompleteIsolatedInvocation(invocation, caps) {
  if (!exactKeys(invocation, ISOLATED_INVOCATION_FIELDS)
    || invocation.schema_version !== 3
    || invocation.class !== "isolated-agent"
    || !["job", "skill-generation"].includes(invocation.workflow)
    || !validIsolatedEnvironmentPolicy(invocation.environment_policy)
    || typeof invocation.effective_model !== "string"
    || invocation.effective_model.length === 0
    || invocation.usage_complete !== true
    || !validUsage(invocation.usage)
    || invocation.usage.total_tokens <= 0
    || !validIsolatedTerminal(invocation.terminal)
    || !(invocation.turns === null || Number.isSafeInteger(invocation.turns))
    || invocation.timed_out !== false
    || !validCompleteIsolatedStream(invocation.stream)
    || !validIsolatedWrapperOutcome(invocation.wrapper_outcome)
    || !validIsolatedProcess(invocation.process)
    || !validIsolatedHardCapEvidence(invocation, caps)) return false;

  const terminalSucceeded = invocation.terminal.subtype === "success"
    && invocation.terminal.is_error === false;
  const turnsShapeValid = Number.isSafeInteger(invocation.turns) && invocation.turns > 0;
  const turnsWithinCaps = turnsShapeValid && invocation.turns <= caps.max_turns;
  const usageWithinCaps = invocation.usage.total_tokens <= caps.max_total_tokens
    && invocation.usage.cost_usd <= caps.max_budget_usd;
  const childSucceeded = invocation.process.exit_code === 0 && invocation.process.signal === null;
  const wrapper = invocation.wrapper_outcome;
  if (wrapper.status === "PASS") {
    return wrapper.code === null
      && invocation.process.wrapper_exit_code === 0
      && terminalSucceeded
      && turnsWithinCaps
      && usageWithinCaps
      && childSucceeded;
  }
  if (invocation.process.wrapper_exit_code !== 1) return false;
  if (wrapper.code === "WRAPPER_MODEL_TERMINAL_INVALID") {
    return usageWithinCaps && (
      !turnsShapeValid
      || (!terminalSucceeded && turnsWithinCaps)
    );
  }
  if (wrapper.code === "WRAPPER_MODEL_CAP_EXCEEDED") {
    return !usageWithinCaps || (turnsShapeValid && !turnsWithinCaps);
  }
  if (wrapper.code === "WRAPPER_CHILD_PROCESS_FAILED") {
    return terminalSucceeded && turnsWithinCaps && usageWithinCaps && !childSucceeded;
  }
  if (wrapper.code === "WRAPPER_SKILL_TRACE_INVALID") {
    return invocation.workflow === "skill-generation"
      && terminalSucceeded
      && turnsWithinCaps
      && usageWithinCaps
      && childSucceeded;
  }
  return false;
}

export function validOutputTokenCapEvidence(invocation, caps) {
  if (Object.hasOwn(invocation, "observed_request_limits")) return false;
  const declaredCap = caps?.max_output_tokens;
  const marker = invocation.hard_cap_enforcement?.max_output_tokens;
  if (declaredCap === undefined) {
    if (marker !== undefined) return false;
    if (invocation.class !== "isolated-agent") return true;
    return invocation.environment_policy?.schema_version === 1
      && invocation.environment_policy?.version === ISOLATED_AGENT_ENV_POLICY_VERSION
      && validEnvironmentKeySummary(invocation.environment_policy?.inbound)
      && validEnvironmentKeySummary(invocation.environment_policy?.claude_process)
      && !(invocation.environment_policy.inbound.key_names ?? []).includes("CLAUDE_CODE_MAX_OUTPUT_TOKENS")
      && !(invocation.environment_policy.claude_process.key_names ?? []).includes("CLAUDE_CODE_MAX_OUTPUT_TOKENS");
  }
  return Number.isSafeInteger(declaredCap)
    && declaredCap > 0
    && marker === ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT
    && invocation.environment_policy?.schema_version === 1
    && invocation.environment_policy?.version === ISOLATED_AGENT_ENV_POLICY_VERSION
    && validEnvironmentKeySummary(invocation.environment_policy?.inbound)
    && validEnvironmentKeySummary(invocation.environment_policy?.claude_process)
    && !(invocation.environment_policy?.inbound?.key_names ?? []).includes("CLAUDE_CODE_MAX_OUTPUT_TOKENS")
    && (invocation.environment_policy?.claude_process?.key_names ?? []).includes("CLAUDE_CODE_MAX_OUTPUT_TOKENS");
}

export function validStructuredOutputRetryEvidence(invocation) {
  const inboundKeys = invocation.environment_policy?.inbound?.key_names ?? [];
  const claudeKeys = invocation.environment_policy?.claude_process?.key_names ?? [];
  const marker = invocation.hard_cap_enforcement?.structured_output_retries;
  if (inboundKeys.includes(ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY)) return false;
  if (invocation.workflow === "skill-generation") {
    return claudeKeys.includes(ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY)
      && marker === ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_ENFORCEMENT;
  }
  return !claudeKeys.includes(ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY)
    && marker === undefined;
}

function validIncompleteFailedInvocation(invocation) {
  const code = invocation?.wrapper_outcome?.code;
  const stream = invocation?.stream;
  const partialAudit = validSkillGenerationPartialTraceAuditReceipt(invocation?.tool_trace_audit);
  const incompleteTraceAudit = validSkillGenerationIncompleteTraceAuditReceipt(invocation?.tool_trace_audit);
  const incompleteAuditRejected = validSkillGenerationIncompleteAuditRejectedReceipt(invocation?.tool_trace_audit);
  const incompleteAuditValid = (invocation?.tool_trace_audit === null
      && !terminalLessSkillTraceAuditRequired(invocation))
    || (code === "WRAPPER_MODEL_USAGE_INVALID" && partialAudit)
    || (["WRAPPER_MODEL_STREAM_INVALID", "WRAPPER_MODEL_TIMEOUT"].includes(code)
      && (incompleteTraceAuditMatchesInvocation(invocation, invocation.tool_trace_audit)
        || (incompleteAuditRejected
          && incompleteAuditRejectedMatchesInvocation(invocation, invocation.tool_trace_audit))));
  const commonValid = exactKeys(invocation, ISOLATED_INVOCATION_FIELDS)
    && invocation.schema_version === 3
    && invocation.class === "isolated-agent"
    && ["job", "skill-generation"].includes(invocation.workflow)
    && (invocation.tool_trace_audit === null || invocation.workflow === "skill-generation")
    && validIsolatedEnvironmentPolicy(invocation.environment_policy)
    && (invocation.effective_model === null || (
      typeof invocation.effective_model === "string" && invocation.effective_model.length > 0
    ))
    && validIsolatedCaps(invocation.effective_caps)
    && validIsolatedWrapperOutcome(invocation.wrapper_outcome)
    && invocation.wrapper_outcome.status === "FAIL"
    && ["WRAPPER_MODEL_STREAM_INVALID", "WRAPPER_MODEL_TIMEOUT", "WRAPPER_MODEL_USAGE_INVALID"].includes(code)
    && invocation.usage_complete === false
    && invocation.usage === null
    && invocation.terminal === null
    && invocation.turns === null
    && incompleteAuditValid
    && validIsolatedProcess(invocation.process)
    && validIsolatedStreamShape(stream)
    && validIsolatedHardCapEvidence(invocation, invocation.effective_caps);
  if (!commonValid) return false;
  if (code === "WRAPPER_MODEL_TIMEOUT") {
    return invocation.timed_out === true
      && invocation.process.exit_code === null
      && ["SIGTERM", "SIGKILL"].includes(invocation.process.signal)
      && invocation.process.wrapper_exit_code === 124
      && stream.complete === false
      && (invocation.tool_trace_audit === null || incompleteTraceAudit || incompleteAuditRejected);
  }
  if (code === "WRAPPER_MODEL_STREAM_INVALID") {
    const childExited = Number.isSafeInteger(invocation.process.exit_code)
      && invocation.process.signal === null;
    const childSignaled = invocation.process.exit_code === null
      && typeof invocation.process.signal === "string"
      && /^[A-Z][A-Z0-9_]{1,31}$/.test(invocation.process.signal);
    return invocation.timed_out === false
      && (childExited || childSignaled)
      && invocation.process.wrapper_exit_code === 1
      && stream.complete === false
      && (invocation.tool_trace_audit === null || incompleteTraceAudit || incompleteAuditRejected);
  }
  return invocation.timed_out === false
    && invocation.process.wrapper_exit_code === 1
    && validCompleteIsolatedStream(stream);
}

function terminalLessSkillTraceAuditRequired(invocation) {
  const stream = invocation?.stream;
  return invocation?.workflow === "skill-generation"
    && ["WRAPPER_MODEL_STREAM_INVALID", "WRAPPER_MODEL_TIMEOUT"].includes(invocation.wrapper_outcome?.code)
    && stream?.complete === false
    && Number.isSafeInteger(stream.event_count)
    && stream.event_count > 0
    && stream.parsed_event_count === stream.event_count
    && stream.init_count === 1
    && stream.result_count === 0;
}

function incompleteTraceAuditMatchesInvocation(invocation, audit) {
  return validSkillGenerationIncompleteTraceAuditReceipt(audit)
    && invocation.workflow === "skill-generation"
    && invocation.wrapper_outcome?.status === "FAIL"
    && ["WRAPPER_MODEL_STREAM_INVALID", "WRAPPER_MODEL_TIMEOUT"].includes(invocation.wrapper_outcome.code)
    && invocation.usage_complete === false
    && invocation.usage === null
    && invocation.terminal === null
    && invocation.turns === null
    && invocation.stream?.complete === false
    && invocation.stream?.result_count === 0
    && canonicalJson(audit.stream) === canonicalJson(invocation.stream);
}

function incompleteAuditRejectedMatchesInvocation(invocation, audit) {
  return validSkillGenerationIncompleteAuditRejectedReceipt(audit)
    && invocation.workflow === "skill-generation"
    && invocation.wrapper_outcome?.status === "FAIL"
    && ["WRAPPER_MODEL_STREAM_INVALID", "WRAPPER_MODEL_TIMEOUT"].includes(invocation.wrapper_outcome.code)
    && invocation.usage_complete === false
    && invocation.usage === null
    && invocation.terminal === null
    && invocation.turns === null
    && invocation.stream?.complete === false
    && invocation.stream?.result_count === 0
    && canonicalJson(audit.stream) === canonicalJson(invocation.stream);
}

function partialTraceMatchesInvocation(invocation, audit) {
  if (!validSkillGenerationPartialTraceAuditReceipt(audit)
    || invocation.wrapper_outcome?.status !== "FAIL") return false;
  if (invocation.terminal !== null) {
    return ["WRAPPER_MODEL_TERMINAL_INVALID", "WRAPPER_MODEL_CAP_EXCEEDED"].includes(invocation.wrapper_outcome.code)
      && audit.terminal.subtype === invocation.terminal?.subtype
      && audit.terminal.is_error === invocation.terminal?.is_error;
  }
  return invocation.usage_complete === false
    && invocation.wrapper_outcome.code === "WRAPPER_MODEL_USAGE_INVALID"
    && invocation.stream?.complete === true;
}

function validFailedStageSkillTrace(stage, planned, invocation) {
  const requiresSkillTrace = stage.id === "real.skill-generation"
    || planned.identity_set === "real-skill-generation"
    || invocation.workflow === "skill-generation";
  if (!requiresSkillTrace) return invocation.tool_trace_audit === null;
  if (invocation.workflow !== "skill-generation") return false;
  if (invocation.wrapper_outcome?.status === "PASS") {
    return validSkillGenerationTraceAuditReceipt(invocation.tool_trace_audit);
  }
  if (invocation.wrapper_outcome?.status !== "FAIL") return false;
  const wrapperCode = invocation.wrapper_outcome.code;
  if (invocation.tool_trace_audit === null) {
    if (terminalLessSkillTraceAuditRequired(invocation)) return false;
    return [
      "WRAPPER_MODEL_TERMINAL_INVALID",
      "WRAPPER_MODEL_CAP_EXCEEDED",
      "WRAPPER_MODEL_STREAM_INVALID",
      "WRAPPER_MODEL_TIMEOUT",
      "WRAPPER_MODEL_USAGE_INVALID",
    ].includes(wrapperCode);
  }
  if (validSkillGenerationTraceAuditReceipt(invocation.tool_trace_audit)) {
    return [
      "WRAPPER_MODEL_TERMINAL_INVALID",
      "WRAPPER_MODEL_CAP_EXCEEDED",
      "WRAPPER_CHILD_PROCESS_FAILED",
    ].includes(wrapperCode);
  }
  const incompleteTraceAudit = validSkillGenerationIncompleteTraceAuditReceipt(invocation.tool_trace_audit);
  if (incompleteTraceAudit) return incompleteTraceAuditMatchesInvocation(invocation, invocation.tool_trace_audit);
  const incompleteAuditRejected = validSkillGenerationIncompleteAuditRejectedReceipt(invocation.tool_trace_audit);
  if (incompleteAuditRejected) return incompleteAuditRejectedMatchesInvocation(invocation, invocation.tool_trace_audit);
  const partialAudit = validSkillGenerationPartialTraceAuditReceipt(invocation.tool_trace_audit);
  if (partialAudit) return partialTraceMatchesInvocation(invocation, invocation.tool_trace_audit);
  return wrapperCode === "WRAPPER_SKILL_TRACE_INVALID"
    && validSkillGenerationFailedTraceAuditReceipt(invocation.tool_trace_audit);
}

export function auditExecutedStageUsage(plan, planned, stage, failures) {
  const gates = stage.gates ?? [];
  if (!validUsage(stage.usage) || gates.some((gate) => !validUsage(gate.usage))) {
    failures.push({ code: "MODEL_USAGE_INVALID", stage_id: stage.id });
    return;
  }
  if (canonicalJson(normalizedUsage(stage.usage)) !== canonicalJson(sumUsage(gates.map((gate) => gate.usage)))) {
    failures.push({ code: "STAGE_GATE_USAGE_MISMATCH", stage_id: stage.id });
  }
  const declarations = planned.invocation_caps ?? [];
  const invocations = gates.flatMap((gate) => gate.model_invocations ?? []);
  const incompleteInvocations = new Set(
    invocations.filter((invocation) => validIncompleteFailedInvocation(invocation)),
  );
  if (invocations.some((invocation) => (
    invocation?.schema_version !== 3
    || ((invocation.usage_complete !== true || !validUsage(invocation.usage)) && !incompleteInvocations.has(invocation))
  ))) {
    failures.push({ code: "MODEL_USAGE_INVALID", stage_id: stage.id });
    return;
  }
  for (const gate of gates) {
    const members = gate.model_invocations ?? [];
    const incomplete = members.some((invocation) => incompleteInvocations.has(invocation));
    if (incomplete && (
      stage.status === "PASS"
      || gate.status === "PASS"
      || gate.usage_complete !== false
      || canonicalJson(normalizedUsage(gate.usage)) !== canonicalJson(zeroUsage())
    )) {
      failures.push({ code: "MODEL_USAGE_INCOMPLETE", stage_id: stage.id, gate_id: gate.id });
    } else if (members.length > 0 && !incomplete && canonicalJson(normalizedUsage(gate.usage)) !== canonicalJson(sumUsage(members.map((invocation) => invocation.usage)))) {
      failures.push({ code: "GATE_MODEL_USAGE_MISMATCH", stage_id: stage.id, gate_id: gate.id });
    }
  }
  if (declarations.length === 0) {
    if (invocations.length > 0) failures.push({ code: "MODEL_INVOCATION_UNPLANNED", stage_id: stage.id });
    return;
  }
  const ids = invocations.map((invocation) => invocation?.invocation_id);
  if (ids.some((id) => typeof id !== "string" || id.length === 0) || new Set(ids).size !== ids.length) {
    failures.push({ code: "MODEL_INVOCATION_ID_INVALID", stage_id: stage.id });
    return;
  }
  const declaredClasses = new Set(declarations.map((declaration) => declaration.class));
  if (invocations.some((invocation) => !declaredClasses.has(invocation?.class))) failures.push({ code: "MODEL_INVOCATION_CLASS_UNEXPECTED", stage_id: stage.id });
  for (const declaration of declarations) {
    const members = invocations.filter((invocation) => invocation?.class === declaration.class);
    if (members.length > declaration.max_count) {
      failures.push({ code: "MODEL_INVOCATION_COUNT_MISMATCH", stage_id: stage.id, class: declaration.class });
    }
  }
  if (stage.status === "PASS" && incompleteInvocations.size > 0) return;
  if (stage.status !== "PASS") {
    for (const invocation of invocations) {
      const declaration = declarations.find((item) => item.class === invocation.class);
      if (!declaration) continue;
      const isolatedStateValid = invocation.class !== "isolated-agent"
        || incompleteInvocations.has(invocation)
        || validCompleteIsolatedInvocation(invocation, declaration.caps);
      const capsMatch = canonicalJson(invocation.effective_caps) === canonicalJson(declaration.caps);
      const completeIsolated = invocation.class === "isolated-agent"
        && invocation.usage_complete === true;
      const modelMatches = completeIsolated
        ? typeof plan.release_inputs?.settings?.model === "string"
          && invocation.effective_model === plan.release_inputs.settings.model
        : invocation.effective_model === null
          || (typeof plan.release_inputs?.settings?.model === "string" && invocation.effective_model === plan.release_inputs.settings.model);
      const hardCapsValid = invocation.class === "isolated-agent"
        ? validIsolatedHardCapEvidence(invocation, declaration.caps)
        : validOutputTokenCapEvidence(invocation, declaration.caps);
      if (!isolatedStateValid || !capsMatch || !modelMatches || !hardCapsValid) {
        failures.push({ code: "MODEL_HARD_CAP_RECEIPT_MISMATCH", stage_id: stage.id, invocation_id: invocation.invocation_id ?? null });
        continue;
      }
      if (!validFailedStageSkillTrace(stage, planned, invocation)) {
        failures.push({ code: "MODEL_TOOL_TRACE_AUDIT_INVALID", stage_id: stage.id, invocation_id: invocation.invocation_id ?? null });
      }
    }
    return;
  }
  if (gates.some((gate) => gate.usage_complete !== true)) failures.push({ code: "MODEL_USAGE_INCOMPLETE", stage_id: stage.id });
  for (const declaration of declarations) {
    const members = invocations.filter((invocation) => invocation?.class === declaration.class);
    if (members.length < declaration.min_count) {
      failures.push({ code: "MODEL_INVOCATION_COUNT_MISMATCH", stage_id: stage.id, class: declaration.class });
      continue;
    }
    for (const invocation of members) {
      const usage = normalizedUsage(invocation.usage);
      const capsMatch = canonicalJson(invocation.effective_caps) === canonicalJson(declaration.caps);
      const terminalSuccess = invocation.terminal?.subtype === "success" && invocation.terminal?.is_error === false;
      const wrapperSuccess = invocation.wrapper_outcome?.schema_version === 1
        && invocation.wrapper_outcome?.status === "PASS"
        && invocation.wrapper_outcome?.code === null;
      const requiresSkillTraceAudit = stage.id === "real.skill-generation"
        || planned.identity_set === "real-skill-generation"
        || invocation.workflow === "skill-generation";
      const toolTraceAuditValid = !requiresSkillTraceAudit || (
        invocation.workflow === "skill-generation"
        && validSkillGenerationTraceAuditReceipt(invocation.tool_trace_audit)
      );
      const modelMatches = typeof plan.release_inputs?.settings?.model === "string" && invocation.effective_model === plan.release_inputs.settings.model;
      const isolatedStateValid = invocation.class !== "isolated-agent"
        || validCompleteIsolatedInvocation(invocation, declaration.caps);
      const withinCaps = Number.isSafeInteger(invocation.turns) && invocation.turns > 0 && invocation.turns <= declaration.caps.max_turns
        && validUsage(usage)
        && usage.cost_usd <= declaration.caps.max_budget_usd
        && usage.total_tokens <= declaration.caps.max_total_tokens
        && invocation.hard_cap_enforcement?.total_tokens === `terminal-usage-postcondition:${TOKEN_USAGE_FORMULA}`
        && validOutputTokenCapEvidence(invocation, declaration.caps);
      if (!toolTraceAuditValid) {
        failures.push({ code: "MODEL_TOOL_TRACE_AUDIT_INVALID", stage_id: stage.id, invocation_id: invocation.invocation_id ?? null });
      } else if (!isolatedStateValid || invocation.usage_complete !== true || !capsMatch || !terminalSuccess || !wrapperSuccess || !modelMatches || !withinCaps) {
        failures.push({ code: "MODEL_HARD_CAP_RECEIPT_MISMATCH", stage_id: stage.id, invocation_id: invocation.invocation_id ?? null });
      }
    }
  }
}

function auditCandidateAgainstPlan(attemptRoot, candidate, failures) {
  const planPath = path.join(attemptRoot, "payload", "run-plan.json");
  if (!fs.existsSync(planPath)) {
    failures.push({ code: "RUN_PLAN_MISSING" });
    return;
  }
  let plan;
  try { plan = readJson(planPath); } catch {
    failures.push({ code: "RUN_PLAN_INVALID" });
    return;
  }
  const { plan_fingerprint: storedFingerprint, run_id: planRunId, created_at_utc: _created, ...planCore } = plan;
  if (storedFingerprint !== sha256Bytes(canonicalJson(planCore))) failures.push({ code: "RUN_PLAN_FINGERPRINT_INVALID" });
  if (
    planRunId !== candidate.run_id
    || plan.track !== candidate.track
    || plan.goal !== candidate.goal
    || plan.plan_fingerprint !== candidate.plan_fingerprint
    || canonicalJson(plan.config_digests) !== canonicalJson(candidate.config_digests)
    || plan.config_bundle_digest !== candidate.config_bundle_digest
    || plan.runtime_profile !== candidate.runtime_profile
    || plan.runtime_profile_digest !== candidate.runtime_profile_digest
    || plan.config_digests?.policy !== candidate.policy_digest
    || canonicalJson(plan.policies?.status) !== canonicalJson(candidate.status_policy)
    || canonicalJson(plan.admission) !== canonicalJson(candidate.admission)
  ) failures.push({ code: "CANDIDATE_PLAN_HEADER_MISMATCH" });
  const expectedCandidateSource = {
    base_commit: plan.source?.base_commit ?? null,
    branch: plan.source?.branch ?? null,
    worktree_clean_at_start: plan.source?.worktree_clean ?? false,
    snapshot: plan.source?.snapshot ?? null,
    baseline: plan.source?.baseline ?? null,
    verification: candidate.source?.verification ?? null,
  };
  if (canonicalJson(candidate.source) !== canonicalJson(expectedCandidateSource)) failures.push({ code: "CANDIDATE_PLAN_SOURCE_MISMATCH" });
  const sourceManifestPath = path.join(attemptRoot, "payload", "source", "source-snapshot.json");
  const sourceVerificationPath = path.join(attemptRoot, "payload", "source", "source-snapshot-verification.json");
  if (!fs.existsSync(sourceManifestPath) || !fs.existsSync(sourceVerificationPath)) {
    failures.push({ code: "SOURCE_SNAPSHOT_EVIDENCE_MISSING" });
  } else {
    try {
      const sourceManifest = readJson(sourceManifestPath);
      const sourceVerification = readJson(sourceVerificationPath);
      const manifestDigest = Array.isArray(sourceManifest.records) ? sha256Bytes(canonicalJson([...sourceManifest.records].sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0))) : null;
      const publicManifest = {
        schema_version: sourceManifest.schema_version,
        algorithm: sourceManifest.algorithm,
        status: sourceManifest.digest ? "PRESENT" : "MISSING",
        digest: sourceManifest.digest,
        file_count: sourceManifest.file_count,
      };
      if (
        sourceManifest.schema_version !== 1
        || sourceManifest.algorithm !== "git-visible-worktree-v1"
        || manifestDigest !== sourceManifest.digest
        || sourceManifest.file_count !== sourceManifest.records.length
        || canonicalJson(publicManifest) !== canonicalJson(plan.source?.snapshot)
        || canonicalJson(sourceVerification) !== canonicalJson(candidate.source?.verification)
      ) failures.push({ code: "SOURCE_SNAPSHOT_EVIDENCE_INVALID" });
      if (sourceVerification.status !== "PASS" && candidate.operation_status !== "ERROR") failures.push({ code: "SOURCE_SNAPSHOT_DRIFT_NOT_FATAL" });
    } catch {
      failures.push({ code: "SOURCE_SNAPSHOT_EVIDENCE_INVALID" });
    }
  }
  const planProofs = (plan.proofs ?? []).map((proof) => ({
    id: proof.id,
    acceptance: proof.acceptance,
    stages: proof.stages,
    proof_definition_digest: proof.proof_definition_digest,
  }));
  const candidateProofs = (candidate.proofs ?? []).map((proof) => ({
    id: proof.id,
    acceptance: proof.acceptance,
    stages: proof.stages.map((stage) => stage.id),
    proof_definition_digest: proof.proof_definition_digest,
  }));
  if (canonicalJson(planProofs) !== canonicalJson(candidateProofs)) failures.push({ code: "CANDIDATE_PLAN_PROOF_MISMATCH" });
  const plannedStages = plan.stages ?? [];
  if (canonicalJson(plannedStages.map((stage) => stage.id)) !== canonicalJson((candidate.stages ?? []).map((stage) => stage.id))) failures.push({ code: "CANDIDATE_PLAN_STAGE_ORDER_MISMATCH" });
  for (const stage of candidate.stages ?? []) {
    const planned = plannedStages.find((item) => item.id === stage.id);
    if (!planned) continue;
    if (
      planned.producer_identity !== stage.producer_identity
      || planned.proof_identity !== stage.proof_identity
      || planned.performance_identity !== stage.performance_identity
      || (planned.decision === "REUSE") !== (stage.result_source === "REUSED")
    ) failures.push({ code: "CANDIDATE_PLAN_STAGE_IDENTITY_MISMATCH", stage_id: stage.id });
    if (stage.result_source === "EXECUTED") {
      const summaries = stage.gates ?? [];
      if (canonicalJson(planned.gates.map((gate) => gate.id)) !== canonicalJson(summaries.map((gate) => gate.id))) {
        failures.push({ code: "CANDIDATE_PLAN_GATE_MISMATCH", stage_id: stage.id });
      }
      for (const summary of summaries) {
        const plannedGate = planned.gates.find((gate) => gate.id === summary.id);
        if (!plannedGate || plannedGate.gate_identity !== summary.gate_identity || plannedGate.definition_digest !== summary.definition_digest || canonicalJson(plannedGate.evidence_contract) !== canonicalJson(summary.evidence_contract) || plannedGate.runtime_profile !== summary.runtime_profile || plannedGate.runtime_profile_digest !== summary.runtime_profile_digest) {
          failures.push({ code: "CANDIDATE_PLAN_GATE_IDENTITY_MISMATCH", stage_id: stage.id, gate_id: summary.id });
          continue;
        }
        const evidenceNames = Array.isArray(summary.evidence) ? summary.evidence.map((record) => path.basename(record.path)).sort() : [];
        if (summary.status === "PASS" && canonicalJson(evidenceNames) !== canonicalJson([...plannedGate.required_evidence].sort())) failures.push({ code: "CANDIDATE_PLAN_GATE_EVIDENCE_MISMATCH", stage_id: stage.id, gate_id: summary.id });
      }
      auditExecutedStageUsage(plan, planned, stage, failures);
    }
  }
  if (!validUsage(candidate.usage) || canonicalJson(normalizedUsage(candidate.usage)) !== canonicalJson(sumUsage((candidate.stages ?? []).map((stage) => stage.usage)))) failures.push({ code: "CANDIDATE_STAGE_USAGE_MISMATCH" });
  const expectedInputDigest = sha256Bytes(canonicalJson({
    run_id: candidate.run_id,
    plan_fingerprint: candidate.plan_fingerprint,
    proofs: candidate.proofs,
    stages: (candidate.stages ?? []).map((stage) => ({ id: stage.id, digest: stage.stage_receipt_digest })),
  }));
  if (candidate.candidate_input_digest !== expectedInputDigest) failures.push({ code: "CANDIDATE_INPUT_DIGEST_INVALID" });
}

function auditCandidateReceipts(attemptRoot, candidate) {
  const failures = [];
  if (canonicalJson(Object.keys(candidate ?? {}).sort()) !== canonicalJson([...CANDIDATE_FIELDS].sort())) failures.push({ code: "CANDIDATE_FIELDS_INVALID" });
  if (candidate?.schema_version !== 2 || candidate.run_id !== path.basename(attemptRoot) || !Array.isArray(candidate.proofs) || candidate.proofs.length === 0 || !Array.isArray(candidate.stages) || candidate.stages.length === 0 || !Array.isArray(candidate.gates)) {
    failures.push({ code: "CANDIDATE_SHAPE_INVALID" });
  }
  auditCandidateAgainstPlan(attemptRoot, candidate, failures);
  const stageById = new Map();
  for (const stage of candidate.stages ?? []) {
    if (!stage.stage_receipt_path || !stage.stage_receipt_digest) {
      failures.push({ code: "STAGE_RECEIPT_REFERENCE_MISSING", stage_id: stage.id });
      continue;
    }
    const receiptPath = path.resolve(attemptRoot, stage.stage_receipt_path);
    if (!receiptPath.startsWith(`${path.resolve(attemptRoot)}${path.sep}`) || !fs.existsSync(receiptPath) || sha256File(receiptPath) !== stage.stage_receipt_digest) {
      failures.push({ code: "STAGE_RECEIPT_DIGEST_INVALID", stage_id: stage.id });
      continue;
    }
    const receipt = readJson(receiptPath);
    if (canonicalJson(receipt) !== canonicalJson(comparableStage(stage))) failures.push({ code: "STAGE_RECEIPT_CONTENT_MISMATCH", stage_id: stage.id });
    stageById.set(stage.id, stage);
    for (const gateSummary of stage.gates ?? []) {
      const expectedReceiptPath = `payload/stages/${stage.id}/gates/${gateSummary.id}/gate-receipt.json`;
      if (gateSummary.receipt_path !== expectedReceiptPath) {
        failures.push({ code: "GATE_RECEIPT_PATH_INVALID", stage_id: stage.id, gate_id: gateSummary.id });
        continue;
      }
      const gatePath = path.resolve(attemptRoot, gateSummary.receipt_path ?? "");
      if (!gatePath.startsWith(`${path.resolve(attemptRoot)}${path.sep}`) || !fs.existsSync(gatePath) || sha256File(gatePath) !== gateSummary.receipt_digest) {
        failures.push({ code: "GATE_RECEIPT_DIGEST_INVALID", stage_id: stage.id, gate_id: gateSummary.id });
        continue;
      }
      const rawGate = readJson(gatePath);
      if (rawGate.schema_version !== 2 || rawGate.stage_id !== stage.id || rawGate.gate_id !== gateSummary.id || rawGate.result_source !== "EXECUTED") failures.push({ code: "GATE_RECEIPT_SHAPE_INVALID", stage_id: stage.id, gate_id: gateSummary.id });
      const receiptGate = { ...rawGate, id: gateSummary.id, receipt_path: gateSummary.receipt_path, receipt_digest: gateSummary.receipt_digest };
      if (canonicalJson(comparableGate(receiptGate)) !== canonicalJson(gateSummary)) failures.push({ code: "GATE_RECEIPT_CONTENT_MISMATCH", stage_id: stage.id, gate_id: gateSummary.id });
      const evidencePaths = new Set();
      for (const record of gateSummary.evidence ?? []) {
        const evidencePath = path.resolve(attemptRoot, record.path ?? "");
        const stageEvidenceRoot = path.resolve(attemptRoot, "payload", "stages", stage.id);
        const recordShapeValid = canonicalJson(Object.keys(record ?? {}).sort()) === canonicalJson(["path", "sha256", "size"])
          && typeof record.path === "string" && !evidencePaths.has(record.path)
          && Number.isSafeInteger(record.size) && record.size >= 0 && /^[a-f0-9]{64}$/.test(record.sha256 ?? "");
        evidencePaths.add(record.path);
        if (!recordShapeValid || !evidencePath.startsWith(`${stageEvidenceRoot}${path.sep}`) || !fs.existsSync(evidencePath) || !fs.statSync(evidencePath).isFile() || fs.statSync(evidencePath).size !== record.size || sha256File(evidencePath) !== record.sha256) {
          failures.push({ code: "GATE_EVIDENCE_DIGEST_INVALID", stage_id: stage.id, gate_id: gateSummary.id, path: record.path ?? null });
        }
      }
    }
  }
  for (const proof of candidate.proofs ?? []) {
    const expected = proof.stages.map((member) => ({ id: member.id, status: stageById.get(member.id)?.status ?? "MISSING" }));
    let status = "PASS";
    if (expected.some((member) => member.status === "ERROR")) status = "ERROR";
    else if (expected.some((member) => member.status === "FAIL")) status = "FAIL";
    else if (expected.some((member) => !["PASS", "NOT_REQUIRED"].includes(member.status))) status = "INCONCLUSIVE";
    if (proof.acceptance !== "all" || proof.status !== status || canonicalJson(proof.stages) !== canonicalJson(expected)) failures.push({ code: "PROOF_AGGREGATION_INVALID", proof_id: proof.id });
  }
  const flattened = (candidate.stages ?? []).flatMap((stage) => (stage.gates ?? []).map((gate) => ({ stage_id: stage.id, ...gate })));
  if (canonicalJson(flattened) !== canonicalJson(candidate.gates ?? [])) failures.push({ code: "GATE_INDEX_INVALID" });
  const functional = (candidate.proofs ?? []).some((proof) => proof.status === "FAIL")
    ? "FAIL"
    : (candidate.proofs ?? []).every((proof) => proof.status === "PASS") ? "PASS" : "INCONCLUSIVE";
  if (functional !== candidate.functional_status) failures.push({ code: "FUNCTIONAL_AGGREGATION_INVALID" });
  const performanceValues = (candidate.stages ?? []).map((stage) => stage.performance_status);
  const performance = performanceValues.includes("FAIL") ? "FAIL"
    : performanceValues.includes("SLOW") ? "SLOW"
      : performanceValues.includes("NOT_CALIBRATED") ? "NOT_CALIBRATED"
        : performanceValues.includes("PASS") ? "PASS" : "NOT_RUN";
  if (performance !== candidate.performance_status) failures.push({ code: "PERFORMANCE_AGGREGATION_INVALID" });
  return { schema_version: 2, status: failures.length === 0 ? "PASS" : "FAIL", failures };
}

function decisionInputs({ candidate, payloadSeal, streams, waterfall, scan, resourceReceipt, metaScan, receiptAudit }) {
  return {
    schema_version: 2,
    candidate_digest: sha256Bytes(canonicalJson(candidate)),
    candidate_input_digest: candidate.candidate_input_digest,
    payload_seal_digest: payloadSeal.root_digest,
    event_audit_digest: sha256Bytes(canonicalJson(streams)),
    waterfall_digest: sha256Bytes(canonicalJson(waterfall)),
    secret_scan_digest: sha256Bytes(canonicalJson(scan)),
    resource_receipt_digest: sha256Bytes(canonicalJson(resourceReceipt)),
    meta_secret_scan_digest: sha256Bytes(canonicalJson(metaScan)),
    receipt_audit_digest: sha256Bytes(canonicalJson(receiptAudit)),
    config_bundle_digest: candidate.config_bundle_digest,
    policy_digest: candidate.policy_digest,
    runtime_profile_digest: candidate.runtime_profile_digest,
  };
}

function buildVerdict({ candidate, payloadSeal, streams, waterfall, scan, resourceReceipt, metaScan, receiptAudit, finalizationDigest, committedAtUtc }) {
  let operationStatus = candidate.operation_status;
  let failureDomain = candidate.failure_domain ?? null;
  if (scan.status !== "PASS" || metaScan.status !== "PASS") {
    operationStatus = "ERROR";
    failureDomain = "SECURITY";
  } else if (payloadSeal.status !== "PASS" || streams.status !== "PASS" || waterfall.status !== "PASS" || receiptAudit.status !== "PASS") {
    operationStatus = "ERROR";
    failureDomain = "HARNESS";
  } else if (resourceReceipt.status !== "PASS") {
    operationStatus = "ERROR";
    failureDomain = failureDomain ?? "INFRA";
  }
  const verificationStatus = scan.status === "PASS" && metaScan.status === "PASS" && payloadSeal.status === "PASS" && streams.status === "PASS" && waterfall.status === "PASS" && receiptAudit.status === "PASS" && resourceReceipt.status === "PASS" ? "PASS" : "FAIL";
  const classification = classifyRun({ functional: candidate.functional_status, performance: candidate.performance_status, operation: operationStatus }, candidate.status_policy);
  const inputs = decisionInputs({ candidate, payloadSeal, streams, waterfall, scan, resourceReceipt, metaScan, receiptAudit });
  const core = {
    schema_version: 2,
    run_id: candidate.run_id,
    track: candidate.track,
    goal: candidate.goal,
    functional_status: candidate.functional_status,
    performance_status: candidate.performance_status,
    operation_status: operationStatus,
    verification_status: verificationStatus,
    overall: classification.overall,
    exit_code: classification.exit_code,
    failure_domain: failureDomain,
    failure_fingerprint: candidate.failure_fingerprint ?? null,
    evidence_reusable: scan.status === "PASS" && metaScan.status === "PASS" && payloadSeal.status === "PASS" && streams.status === "PASS" && waterfall.status === "PASS" && receiptAudit.status === "PASS" && resourceReceipt.status === "PASS",
    proofs: candidate.proofs,
    stages: candidate.stages,
    gates: candidate.gates,
    source: candidate.source,
    lineage: candidate.lineage ?? null,
    config_digests: candidate.config_digests,
    config_bundle_digest: candidate.config_bundle_digest,
    runtime_profile: candidate.runtime_profile,
    runtime_profile_digest: candidate.runtime_profile_digest,
    policy_digest: candidate.policy_digest,
    status_policy: candidate.status_policy,
    plan_fingerprint: candidate.plan_fingerprint,
    usage: validUsage(candidate.usage) ? normalizedUsage(candidate.usage) : zeroUsage(),
    secret_scan: { status: scan.status, sensitive_value_occurrences: scan.sensitive_value_occurrences },
    candidate_input_digest: candidate.candidate_input_digest,
    payload_seal_digest: payloadSeal.root_digest,
    event_audit_digest: inputs.event_audit_digest,
    waterfall_digest: inputs.waterfall_digest,
    dfx_summary: waterfall.totals,
    secret_scan_digest: inputs.secret_scan_digest,
    resource_receipt_digest: inputs.resource_receipt_digest,
    meta_secret_scan_digest: inputs.meta_secret_scan_digest,
    receipt_audit_digest: inputs.receipt_audit_digest,
    finalization_digest: finalizationDigest,
    decision_input_digest: sha256Bytes(canonicalJson(inputs)),
    committed_at_utc: committedAtUtc,
  };
  return { ...core, verdict_digest: sha256Bytes(canonicalJson(core)) };
}

function safeAudit(action, fallback) {
  try { return action(); } catch { return fallback; }
}

function waterfallFailure() {
  return {
    schema_version: 2,
    status: "ERROR",
    authority: "indexed-from-sealed-gate-and-producer-evidence",
    cross_clock_subtraction_forbidden: true,
    stages: [],
    producers: [],
    host_spans: [],
    server_job_spans: [],
    totals: {
      event_bytes: 0,
      transfer_bytes: 0,
      retry_events: 0,
      timeout_events: 0,
      server_operation_duration_ms: 0,
      correlation_ids: 0,
      request_ids: 0,
      case_ids: 0,
      job_ids: 0,
    },
    code: "WATERFALL_BUILD_ERROR",
  };
}

export async function finalizeAttempt({ attemptRoot, candidate, resourcePolicy, knownSecrets = [], policy = null }) {
  const verdictPath = path.join(attemptRoot, "verdict.json");
  if (fs.existsSync(verdictPath)) return readJson(verdictPath);
  if (candidate.schema_version !== 2) throw new Error("CANDIDATE_SCHEMA_UNSUPPORTED");
  const scannerVersion = policy?.evidence?.scanner_version ?? "test-flow-secret-scan-v2";
  const requiredFiles = requiredEventFiles(candidate.gates);
  await awaitRequiredEventVisibility(attemptRoot, requiredFiles, policy?.evidence?.event_visibility_seconds ?? 0);
  const waterfall = safeAudit(() => buildWaterfallSummary(attemptRoot, candidate), waterfallFailure());
  writeJsonSync(path.join(attemptRoot, "payload", "waterfall-summary.json"), waterfall);
  const streams = validateEvidenceStreams(attemptRoot, {
    requiredFiles,
    allowedEmptyFiles: allowedEmptyEventFiles(candidate.gates),
  });
  writeJsonSync(path.join(attemptRoot, "payload", "candidate-result.json"), candidate);
  writeJsonSync(path.join(attemptRoot, "payload", "event-audit.json"), streams);
  const receiptAudit = auditCandidateReceipts(attemptRoot, candidate);
  writeJsonSync(path.join(attemptRoot, "payload", "receipt-audit.json"), receiptAudit);
  const payloadSeal = safeAudit(() => sealPayload(path.join(attemptRoot, "payload")), { schema_version: 2, status: "ERROR", root_digest: null, files: [], invalid_entries: [], code: "PAYLOAD_SEAL_ERROR" });
  writeJsonSync(path.join(attemptRoot, "finalization", "payload-seal.json"), payloadSeal);
  const scan = safeAudit(() => scanPayload(path.join(attemptRoot, "payload"), { knownSecrets, scannerVersion }), { schema_version: 2, status: "ERROR", scanner: scannerVersion, scanned_root_digest: null, files_scanned: 0, sensitive_value_occurrences: 0, hits: [], code: "SECRET_SCAN_ERROR" });
  writeJsonSync(path.join(attemptRoot, "finalization", "secret-scan.json"), scan);
  const preserve = candidate.functional_status !== "PASS" || candidate.performance_status === "FAIL" || candidate.operation_status !== "PASS" || streams.status !== "PASS" || waterfall.status !== "PASS" || scan.status !== "PASS" || payloadSeal.status !== "PASS" || receiptAudit.status !== "PASS";
  let resourceReceipt;
  try { resourceReceipt = await resourcePolicy({ preserve, runId: candidate.run_id }); }
  catch (error) { resourceReceipt = { schema_version: 2, status: "ERROR", preserve, code: "RESOURCE_POLICY_FAILED", remaining: [], error: String(error?.message ?? error) }; }
  writeJsonSync(path.join(attemptRoot, "finalization", "resource-receipt.json"), resourceReceipt);
  const metaScan = safeAudit(
    () => scanPayload(path.join(attemptRoot, "finalization"), { knownSecrets, scannerVersion, exclude: ["meta-secret-scan.json"] }),
    { schema_version: 2, status: "ERROR", scanner: scannerVersion, scanned_root_digest: null, files_scanned: 0, sensitive_value_occurrences: 0, hits: [], code: "META_SECRET_SCAN_ERROR" },
  );
  writeJsonSync(path.join(attemptRoot, "finalization", "meta-secret-scan.json"), metaScan);
  const finalizationDigest = sha256Bytes(canonicalJson(listFiles(path.join(attemptRoot, "finalization"))));
  const verdict = buildVerdict({ candidate, payloadSeal, streams, waterfall, scan, resourceReceipt, metaScan, receiptAudit, finalizationDigest, committedAtUtc: new Date().toISOString() });
  atomicCreateJson(verdictPath, verdict);
  return verdict;
}

function invalid(verdict, reason, details = null) {
  return { status: "INVALID", verdict, reason, details };
}

export function verifyVerdict(attemptRoot, { knownSecrets = [], visited = new Set() } = {}) {
  const verdictPath = path.join(attemptRoot, "verdict.json");
  if (!fs.existsSync(verdictPath)) return { status: "UNFINALIZED", verdict: null };
  let verdict;
  try { verdict = readJson(verdictPath); } catch { return invalid(null, "VERDICT_JSON_INVALID"); }
  if (verdict.schema_version !== 2) return invalid(verdict, "VERDICT_SCHEMA_UNSUPPORTED");
  const resolved = path.resolve(attemptRoot);
  if (visited.has(resolved)) return invalid(verdict, "REUSE_REFERENCE_CYCLE");
  const nextVisited = new Set(visited).add(resolved);
  const required = [
    ["payload/candidate-result.json", "CANDIDATE_MISSING"],
    ["payload/event-audit.json", "EVENT_AUDIT_MISSING"],
    ["payload/receipt-audit.json", "RECEIPT_AUDIT_MISSING"],
    ["payload/waterfall-summary.json", "WATERFALL_SUMMARY_MISSING"],
    ["finalization/payload-seal.json", "PAYLOAD_SEAL_MISSING"],
    ["finalization/secret-scan.json", "SECRET_SCAN_MISSING"],
    ["finalization/resource-receipt.json", "RESOURCE_RECEIPT_MISSING"],
    ["finalization/meta-secret-scan.json", "META_SECRET_SCAN_MISSING"],
  ];
  for (const [relative, reason] of required) if (!fs.existsSync(path.join(attemptRoot, relative))) return invalid(verdict, reason);
  let candidate;
  let storedStreams;
  let storedReceiptAudit;
  let storedWaterfall;
  let payloadSeal;
  let storedScan;
  let resourceReceipt;
  let storedMetaScan;
  try {
    candidate = readJson(path.join(attemptRoot, "payload", "candidate-result.json"));
    storedStreams = readJson(path.join(attemptRoot, "payload", "event-audit.json"));
    storedReceiptAudit = readJson(path.join(attemptRoot, "payload", "receipt-audit.json"));
    storedWaterfall = readJson(path.join(attemptRoot, "payload", "waterfall-summary.json"));
    payloadSeal = readJson(path.join(attemptRoot, "finalization", "payload-seal.json"));
    storedScan = readJson(path.join(attemptRoot, "finalization", "secret-scan.json"));
    resourceReceipt = readJson(path.join(attemptRoot, "finalization", "resource-receipt.json"));
    storedMetaScan = readJson(path.join(attemptRoot, "finalization", "meta-secret-scan.json"));
  } catch { return invalid(verdict, "FINALIZATION_JSON_INVALID"); }
  const sealVerification = verifyPayloadSeal(path.join(attemptRoot, "payload"), payloadSeal);
  if (sealVerification.status !== "PASS") return invalid(verdict, "PAYLOAD_SEAL_INVALID", sealVerification);
  const receiptAudit = auditCandidateReceipts(attemptRoot, candidate);
  if (receiptAudit.status !== "PASS" || canonicalJson(receiptAudit) !== canonicalJson(storedReceiptAudit)) return invalid(verdict, "RECEIPT_AUDIT_INVALID", receiptAudit);
  const streams = validateEvidenceStreams(attemptRoot, {
    requiredFiles: requiredEventFiles(candidate.gates),
    allowedEmptyFiles: allowedEmptyEventFiles(candidate.gates),
  });
  if (canonicalJson(streams) !== canonicalJson(storedStreams)) return invalid(verdict, "EVENT_AUDIT_INVALID", streams);
  const waterfall = safeAudit(() => buildWaterfallSummary(attemptRoot, candidate), waterfallFailure());
  if (canonicalJson(waterfall) !== canonicalJson(storedWaterfall)) return invalid(verdict, "WATERFALL_SUMMARY_INVALID", waterfall);
  const scannerVersion = storedScan.scanner;
  const scan = scanPayload(path.join(attemptRoot, "payload"), { knownSecrets, scannerVersion });
  if (canonicalJson(scan) !== canonicalJson(storedScan)) return invalid(verdict, "SECRET_SCAN_INVALID", scan);
  const metaScan = scanPayload(path.join(attemptRoot, "finalization"), { knownSecrets, scannerVersion, exclude: ["meta-secret-scan.json"] });
  if (canonicalJson(metaScan) !== canonicalJson(storedMetaScan)) return invalid(verdict, "META_SECRET_SCAN_INVALID", metaScan);
  const finalizationDigest = sha256Bytes(canonicalJson(listFiles(path.join(attemptRoot, "finalization"))));
  const expected = buildVerdict({ candidate, payloadSeal, streams, waterfall, scan, resourceReceipt, metaScan, receiptAudit, finalizationDigest, committedAtUtc: verdict.committed_at_utc });
  if (canonicalJson(expected) !== canonicalJson(verdict)) return invalid(verdict, "VERDICT_DECISION_MISMATCH", { expected_digest: expected.verdict_digest, actual_digest: verdict.verdict_digest });
  for (const stage of candidate.stages ?? []) {
    if (stage.result_source !== "REUSED") continue;
    const sourceRunId = stage.reused_from?.run_id;
    const sourceDigest = stage.reused_from?.source_stage_receipt_digest;
    if (typeof sourceRunId !== "string" || typeof sourceDigest !== "string") return invalid(verdict, "REUSE_SOURCE_REFERENCE_INVALID", { stage_id: stage.id });
    const sourceRoot = path.join(path.dirname(resolved), sourceRunId);
    const sourceVerification = verifyVerdict(sourceRoot, { knownSecrets, visited: nextVisited });
    if (sourceVerification.status !== "PASS") return invalid(verdict, "REUSE_SOURCE_INVALID", { stage_id: stage.id, source_run_id: sourceRunId });
    const sourceStage = (sourceVerification.verdict.stages ?? []).find((candidateStage) => candidateStage.id === stage.id);
    if (!sourceStage || sourceStage.result_source !== "EXECUTED" || sourceStage.stage_receipt_digest !== sourceDigest || sourceStage.status !== "PASS" || sourceStage.producer_identity !== stage.producer_identity || sourceStage.proof_identity !== stage.proof_identity) {
      return invalid(verdict, "REUSE_SOURCE_STAGE_INVALID", { stage_id: stage.id, source_run_id: sourceRunId });
    }
  }
  return {
    status: "PASS",
    verdict,
    verification: sealVerification,
    finalization_verification: { status: "PASS", expected_digest: verdict.finalization_digest, actual_digest: finalizationDigest },
  };
}
