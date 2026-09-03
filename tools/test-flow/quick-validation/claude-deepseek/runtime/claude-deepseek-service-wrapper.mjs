#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { canonicalJson, sha256Bytes } from "../../../lib/util.mjs";
import {
  aggregateClaudeUsage,
  CLAUDE_DEEPSEEK_CALL_WALL_SECONDS,
  CLAUDE_DEEPSEEK_E2E_MAX_TURNS,
  CLAUDE_DEEPSEEK_E2E_USD_LIMIT,
  CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS,
  CLAUDE_DEEPSEEK_MODEL_CERT_BUDGET_ENFORCEMENT,
  CLAUDE_DEEPSEEK_MODEL_CERT_PLAN_CAPS,
  CLAUDE_DEEPSEEK_MODEL_CERT_ROLE_POOL_USD,
  CLAUDE_DEEPSEEK_MODEL,
  CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
  validateClaudeDeepseekRoleReceipt,
} from "./claude-deepseek-contract.mjs";
import { runClaudeProcess } from "./claude-deepseek-process.mjs";

const MODULE_PATH = fileURLToPath(import.meta.url);
const MAX_PROMPT_BYTES = 4 * 1024 * 1024;
const ROLE_MARKER = "<<<METHODS_EVIDENCE_V2_ROLE>>>";
const ROLE_END_MARKER = "<<<END METHODS_EVIDENCE_V2_ROLE>>>";
const ROLE_CONTEXT_START = /<<<SECTION \d+ (EVIDENCE|REVIEW_TARGET)>>>/gu;
const ROLE_CONTEXT_SECTION = /<<<SECTION \d+ (EVIDENCE|REVIEW_TARGET)>>>\n([\s\S]*?)\n<<<END SECTION>>>/gu;
const LEGACY_ROLE_INPUTS = Object.freeze([
  "inputs/method-evidence-graph.json",
  "inputs/method-evaluation-plan.json",
]);
const ROLE_SPEC = Object.freeze({
  SPECIALIST: Object.freeze({
    promptRole: "Specialist",
    output: "output/method-diagnosis.draft.json",
  }),
  REVIEWER: Object.freeze({
    promptRole: "Reviewer",
    output: "output/method-review.draft.json",
  }),
});

class ServiceWrapperError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "ServiceWrapperError";
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) {
  throw new ServiceWrapperError(code, message, details);
}

function requireWrapper(condition, code, message, details = {}) {
  if (!condition) fail(code, message, details);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function positiveInteger(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function relativePosixPath(value) {
  if (typeof value !== "string" || value.length === 0 || value.startsWith("/") || value.includes("\\")) return false;
  if (/^[A-Za-z]:/u.test(value)) return false;
  return value.split("/").every((part) => part !== "" && part !== "." && part !== "..");
}

function compareUnicodeText(left, right) {
  const leftPoints = [...left].map((item) => item.codePointAt(0));
  const rightPoints = [...right].map((item) => item.codePointAt(0));
  for (let index = 0; index < Math.min(leftPoints.length, rightPoints.length); index += 1) {
    if (leftPoints[index] !== rightPoints[index]) return leftPoints[index] - rightPoints[index];
  }
  return leftPoints.length - rightPoints.length;
}

function compareDeterministicTuple(left, right) {
  for (let index = 0; index < left.length; index += 1) {
    const comparison = typeof left[index] === "number"
      ? left[index] - right[index]
      : compareUnicodeText(left[index], right[index]);
    if (comparison !== 0) return comparison;
  }
  return 0;
}

function uuid(value) {
  return typeof value === "string"
    && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(value);
}

function validVersionedRef(value) {
  return exactKeys(value, ["id", "version", "content_hash"])
    && typeof value.id === "string"
    && value.id.length > 0
    && typeof value.version === "string"
    && value.version.length > 0
    && /^[0-9a-f]{64}$/u.test(value.content_hash);
}

function validReviewTarget(value) {
  return exactKeys(value, [
    "schema_version",
    "evaluation_id",
    "source_job_id",
    "graph_ref",
    "plan_ref",
    "skill_ref",
    "reviewed_state_revision",
  ])
    && value.schema_version === 2
    && uuid(value.evaluation_id)
    && uuid(value.source_job_id)
    && /^graph-[0-9a-f]{64}$/u.test(value.graph_ref)
    && /^plan-[0-9a-f]{64}$/u.test(value.plan_ref)
    && validVersionedRef(value.skill_ref)
    && positiveInteger(value.reviewed_state_revision);
}

function validateCompactEvaluationInput(value) {
  requireWrapper(
    isPlainObject(value)
      && exactKeys(value, ["schema_version", "evidence_graph_ref", "plan_ref", "limitations", "sources", "observations", "markers", "evaluations"])
      && value.schema_version === 2
      && /^graph-[0-9a-f]{64}$/u.test(value.evidence_graph_ref)
      && /^plan-[0-9a-f]{64}$/u.test(value.plan_ref)
      && Array.isArray(value.limitations)
      && Array.isArray(value.sources)
      && value.sources.length > 0
      && Array.isArray(value.observations)
      && value.observations.length > 0
      && Array.isArray(value.markers)
      && value.markers.length > 0
      && Array.isArray(value.evaluations)
      && value.evaluations.length > 0,
    "CLAUDE_DEEPSEEK_EVALUATION_INPUT_INVALID",
    "Production role prompt has an invalid compact evaluation_input",
  );
  requireWrapper(
    value.limitations.every((item) => typeof item === "string" && item.length > 0)
      && new Set(value.limitations).size === value.limitations.length,
    "CLAUDE_DEEPSEEK_EVALUATION_INPUT_INVALID",
    "Compact evaluation limitations are invalid",
  );

  const sourceNames = new Set();
  let previousSourceKey = null;
  for (const [index, item] of value.sources.entries()) {
    const sourceKey = [item?.source_id, item?.relative_path];
    requireWrapper(
      exactKeys(item, ["id", "source_id", "relative_path"])
        && item.id === index + 1
        && /^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*$/u.test(item.source_id)
        && item.source_id.length <= 256
        && relativePosixPath(item.relative_path),
      "CLAUDE_DEEPSEEK_EVALUATION_INPUT_INVALID",
      "Compact evaluation sources are invalid",
    );
    requireWrapper(
      previousSourceKey === null
        || compareDeterministicTuple(previousSourceKey, sourceKey) < 0,
      "CLAUDE_DEEPSEEK_EVALUATION_INPUT_INVALID",
      "Compact evaluation sources are not in deterministic order",
    );
    requireWrapper(
      !sourceNames.has(item.source_id),
      "CLAUDE_DEEPSEEK_EVALUATION_INPUT_INVALID",
      "Compact evaluation source ids are not unique",
    );
    sourceNames.add(item.source_id);
    previousSourceKey = sourceKey;
  }

  const observationIds = new Set();
  const physicalLines = new Set();
  let previousObservationKey = null;
  for (const [index, item] of value.observations.entries()) {
    const observationKey = [item?.source_id, item?.line_number, item?.line];
    requireWrapper(
      exactKeys(item, ["id", "source_id", "line_number", "line"])
        && item.id === index + 1
        && /^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*$/u.test(item.source_id)
        && sourceNames.has(item.source_id)
        && positiveInteger(item.line_number)
        && typeof item.line === "string"
        && item.line.length > 0
        && (
          previousObservationKey === null
          || compareDeterministicTuple(previousObservationKey, observationKey) < 0
        ),
      "CLAUDE_DEEPSEEK_EVALUATION_INPUT_INVALID",
      "Compact evaluation observations are invalid",
    );
    observationIds.add(item.id);
    physicalLines.add(`${item.source_id}\0${item.line_number}`);
    previousObservationKey = observationKey;
  }
  requireWrapper(
    physicalLines.size === value.observations.length,
    "CLAUDE_DEEPSEEK_EVALUATION_INPUT_INVALID",
    "Compact evaluation observations repeat one physical log line",
  );

  const markerIds = new Set();
  const markerLiterals = new Set();
  let previousMarker = null;
  for (const [index, item] of value.markers.entries()) {
    requireWrapper(
      exactKeys(item, ["id", "literal"])
        && item.id === index + 1
        && typeof item.literal === "string"
        && item.literal.length > 0
        && (previousMarker === null || compareUnicodeText(previousMarker, item.literal) < 0),
      "CLAUDE_DEEPSEEK_EVALUATION_INPUT_INVALID",
      "Compact evaluation markers are invalid",
    );
    markerIds.add(item.id);
    markerLiterals.add(item.literal);
    previousMarker = item.literal;
  }
  requireWrapper(
    markerLiterals.size === value.markers.length,
    "CLAUDE_DEEPSEEK_EVALUATION_INPUT_INVALID",
    "Compact evaluation marker literals are not unique",
  );

  const evaluationRefs = new Set();
  const methodIds = new Set();
  const eventRefs = new Set();
  const qualifiedMatches = new Set();
  const usedObservationIds = new Set();
  const usedMarkerIds = new Set();
  let previousEvaluation = null;
  for (const evaluation of value.evaluations) {
    requireWrapper(
      exactKeys(evaluation, ["evaluation_ref", "method_id", "method_priority", "events"])
        && /^eval-[0-9a-f]{64}$/u.test(evaluation.evaluation_ref)
        && /^[a-z0-9]+(?:-[a-z0-9]+)*$/u.test(evaluation.method_id)
        && positiveInteger(evaluation.method_priority)
        && Array.isArray(evaluation.events)
        && evaluation.events.length > 0,
      "CLAUDE_DEEPSEEK_EVALUATION_INPUT_INVALID",
      "Compact evaluation item is invalid",
    );
    requireWrapper(
      previousEvaluation === null
        || previousEvaluation.method_priority < evaluation.method_priority
        || (
          previousEvaluation.method_priority === evaluation.method_priority
          && previousEvaluation.method_id < evaluation.method_id
        ),
      "CLAUDE_DEEPSEEK_EVALUATION_INPUT_INVALID",
      "Compact evaluations are not in deterministic plan order",
    );
    previousEvaluation = evaluation;
    requireWrapper(
      !evaluationRefs.has(evaluation.evaluation_ref) && !methodIds.has(evaluation.method_id),
      "CLAUDE_DEEPSEEK_EVALUATION_INPUT_INVALID",
      "Compact evaluations are not unique",
    );
    evaluationRefs.add(evaluation.evaluation_ref);
    methodIds.add(evaluation.method_id);
    for (const event of evaluation.events) {
      requireWrapper(
        exactKeys(event, ["event_ref", "identity_tokens", "matches"])
          && /^event-[0-9a-f]{64}$/u.test(event.event_ref)
          && Array.isArray(event.identity_tokens)
          && event.identity_tokens.every((item) => typeof item === "string" && /^[a-z][a-z0-9]*(?:_[a-z0-9]+)*=[^\s,;]+$/u.test(item))
          && new Set(event.identity_tokens).size === event.identity_tokens.length
          && new Set(event.identity_tokens.map((item) => item.split("=", 1)[0])).size === event.identity_tokens.length
          && Array.isArray(event.matches)
          && event.matches.length > 0
          && !eventRefs.has(event.event_ref),
        "CLAUDE_DEEPSEEK_EVALUATION_INPUT_INVALID",
        "Compact evaluation event is invalid",
      );
      eventRefs.add(event.event_ref);
      for (const match of event.matches) {
        const qualified = `${evaluation.method_id}\0${match?.observation_id}\0${match?.marker_id}\0${match?.method_marker_index}`;
        requireWrapper(
          exactKeys(match, ["observation_id", "marker_id", "method_marker_index"])
            && observationIds.has(match.observation_id)
            && markerIds.has(match.marker_id)
            && positiveInteger(match.method_marker_index)
            && !qualifiedMatches.has(qualified),
          "CLAUDE_DEEPSEEK_EVALUATION_INPUT_INVALID",
          "Compact method-qualified match is invalid",
        );
        qualifiedMatches.add(qualified);
        usedObservationIds.add(match.observation_id);
        usedMarkerIds.add(match.marker_id);
      }
    }
  }
  requireWrapper(
    usedObservationIds.size === observationIds.size
      && [...observationIds].every((id) => usedObservationIds.has(id))
      && usedMarkerIds.size === markerIds.size
      && [...markerIds].every((id) => usedMarkerIds.has(id)),
    "CLAUDE_DEEPSEEK_EVALUATION_INPUT_INVALID",
    "Compact evaluation_input does not exactly cover its observations and markers",
  );
  return value;
}

function roleEvaluationInput(prompt, role) {
  requireWrapper(
    LEGACY_ROLE_INPUTS.every((relative) => !prompt.includes(relative)),
    "CLAUDE_DEEPSEEK_LEGACY_MODEL_INPUT_EXPOSED",
    "Production role prompt exposes a legacy Graph/Plan input path",
  );
  const expectedSection = role === "SPECIALIST" ? "EVIDENCE" : "REVIEW_TARGET";
  const starts = [...prompt.matchAll(new RegExp(ROLE_CONTEXT_START.source, "gu"))];
  const sections = [...prompt.matchAll(new RegExp(ROLE_CONTEXT_SECTION.source, "gu"))];
  requireWrapper(
    starts.length === 1
      && sections.length === 1
      && sections[0][1] === expectedSection,
    "CLAUDE_DEEPSEEK_EVALUATION_INPUT_MISSING",
    "Production role prompt must contain exactly one role data section",
  );
  let payload;
  try { payload = JSON.parse(sections[0][2]); }
  catch {
    fail(
      "CLAUDE_DEEPSEEK_EVALUATION_INPUT_INVALID",
      "Production role data section is not valid JSON",
    );
  }
  const validIdentity = role === "SPECIALIST"
    ? exactKeys(payload, ["schema_version", "role", "job_id", "case_id", "request_path", "evaluation_input"])
      && uuid(payload.job_id)
      && uuid(payload.case_id)
    : exactKeys(payload, ["schema_version", "role", "target", "request_path", "evaluation_input"])
      && validReviewTarget(payload.target);
  requireWrapper(
    validIdentity
      && payload.schema_version === 2
      && payload.role === role
      && payload.request_path === "inputs/request.json"
      && isPlainObject(payload.evaluation_input),
    "CLAUDE_DEEPSEEK_EVALUATION_INPUT_INVALID",
    "Production role data section has an invalid identity or shape",
  );
  const evaluationInput = validateCompactEvaluationInput(payload.evaluation_input);
  if (role === "REVIEWER") {
    requireWrapper(
      payload.target.graph_ref === evaluationInput.evidence_graph_ref
        && payload.target.plan_ref === evaluationInput.plan_ref,
      "CLAUDE_DEEPSEEK_EVALUATION_INPUT_INVALID",
      "Reviewer target does not match its compact evaluation_input",
    );
  }
  return evaluationInput;
}

export function parseArguments(argv) {
  const values = {};
  const names = new Set([
    "claude-entry",
    "settings",
    "config-root",
    "private-root",
    "evidence-root",
    "usage-root",
    "run-id",
  ]);
  for (let index = 0; index < argv.length; index += 2) {
    const argument = argv[index];
    if (!argument?.startsWith("--") || index + 1 >= argv.length || argv[index + 1].startsWith("--")) {
      fail("CLAUDE_DEEPSEEK_SERVICE_ARGUMENT_INVALID", "Model-cert wrapper arguments must use --name value pairs");
    }
    const name = argument.slice(2);
    if (!names.has(name)) fail("CLAUDE_DEEPSEEK_SERVICE_ARGUMENT_UNKNOWN", "Model-cert wrapper received an unsupported argument");
    if (Object.hasOwn(values, name)) fail("CLAUDE_DEEPSEEK_SERVICE_ARGUMENT_DUPLICATE", "Model-cert wrapper argument is duplicated");
    values[name] = argv[index + 1];
  }
  if (![...names].every((name) => typeof values[name] === "string" && values[name])) {
    fail("CLAUDE_DEEPSEEK_SERVICE_ARGUMENT_MISSING", "Model-cert wrapper arguments are incomplete");
  }
  return values;
}

async function readPrompt(stream) {
  const chunks = [];
  let size = 0;
  for await (const chunk of stream) {
    size += chunk.length;
    if (size > MAX_PROMPT_BYTES) fail("CLAUDE_DEEPSEEK_SERVICE_PROMPT_LIMIT", "Evidence V2 role prompt exceeds the wrapper byte cap");
    chunks.push(Buffer.from(chunk));
  }
  try {
    const prompt = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
    if (!prompt.trim()) fail("CLAUDE_DEEPSEEK_SERVICE_PROMPT_EMPTY", "Evidence V2 role prompt is empty");
    return prompt;
  } catch (error) {
    if (error instanceof ServiceWrapperError) throw error;
    fail("CLAUDE_DEEPSEEK_SERVICE_PROMPT_UTF8", "Evidence V2 role prompt must be UTF-8");
  }
}

export function parseMethodsRolePrompt(prompt) {
  requireWrapper(typeof prompt === "string" && prompt.length > 0, "CLAUDE_DEEPSEEK_ROLE_PROMPT_INVALID", "Evidence V2 role prompt is invalid");
  requireWrapper(prompt.split(ROLE_MARKER).length === 2 && prompt.split(ROLE_END_MARKER).length === 2, "CLAUDE_DEEPSEEK_ROLE_MARKER_INVALID", "Evidence V2 role marker is missing or duplicated");
  const roleMatch = /Role: (Specialist|Reviewer)\. Attempt: (primary evaluation|only repair)\./u.exec(prompt);
  requireWrapper(roleMatch !== null, "CLAUDE_DEEPSEEK_ROLE_IDENTITY_INVALID", "Evidence V2 role or attempt is not explicit");
  const role = roleMatch[1] === "Specialist" ? "SPECIALIST" : "REVIEWER";
  const attempt = roleMatch[2] === "primary evaluation" ? "PRIMARY" : "REPAIR";
  const expectedOutput = ROLE_SPEC[role].output;
  requireWrapper(prompt.includes(`Write only ${expectedOutput}.`), "CLAUDE_DEEPSEEK_ROLE_OUTPUT_INVALID", "Evidence V2 role prompt does not bind its one output draft");
  requireWrapper(prompt.includes("evaluation_ref, verdict, supporting_event_refs, and reason"), "CLAUDE_DEEPSEEK_ROLE_CONTRACT_INVALID", "Evidence V2 evaluation array contract is missing");
  roleEvaluationInput(prompt, role);
  return Object.freeze({ role, attempt, output: expectedOutput });
}

function writeJsonNew(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, canonicalJson(value), { encoding: "utf8", mode: 0o600, flag: "wx" });
}

function attemptKey(role, attempt) {
  return `${role.toLowerCase()}-${attempt.toLowerCase()}`;
}

function roundedUsd(value) {
  return Math.round(value * 1_000_000) / 1_000_000;
}

function exactKeys(value, keys) {
  return value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && canonicalJson(Object.keys(value).sort()) === canonicalJson([...keys].sort());
}

function normalizedUsageOrNull(value) {
  try { return aggregateClaudeUsage([{ usage: value }]); }
  catch { return null; }
}

function closedProviderTerminal(value) {
  if (!exactKeys(value, ["subtype", "is_error", "stop_reason", "exit_code", "signal"])) return null;
  if (typeof value.subtype !== "string"
    || value.subtype.length === 0
    || typeof value.is_error !== "boolean"
    || (value.stop_reason !== null && typeof value.stop_reason !== "string")
    || (value.exit_code !== null && (!Number.isSafeInteger(value.exit_code) || value.exit_code < 0))
    || (value.signal !== null && (typeof value.signal !== "string" || value.signal.length === 0))) return null;
  return Object.freeze({ ...value });
}

export function roleInvocationBudget(usageRoot, role, attempt) {
  requireWrapper(Object.hasOwn(ROLE_SPEC, role) && ["PRIMARY", "REPAIR"].includes(attempt), "CLAUDE_DEEPSEEK_ROLE_BUDGET_IDENTITY_INVALID", "Evidence V2 role budget identity is invalid");
  let priorCostUsd = 0;
  if (attempt === "REPAIR") {
    const primaryPath = path.join(path.resolve(usageRoot), `${attemptKey(role, "PRIMARY")}.json`);
    requireWrapper(fs.existsSync(primaryPath), "CLAUDE_DEEPSEEK_ROLE_BUDGET_PRIMARY_MISSING", `${role} repair has no archived primary usage`);
    const primary = JSON.parse(fs.readFileSync(primaryPath, "utf8"));
    requireWrapper(
      primary?.role === role
        && primary?.evaluation_attempt === "PRIMARY"
        && primary?.status === "PASS"
        && primary?.usage_complete === true,
      "CLAUDE_DEEPSEEK_ROLE_BUDGET_PRIMARY_INVALID",
      `${role} primary usage receipt is not eligible for repair budgeting`,
    );
    priorCostUsd = aggregateClaudeUsage([primary]).cost_usd;
  }
  const effectiveCallCapUsd = Math.max(0, roundedUsd(CLAUDE_DEEPSEEK_MODEL_CERT_ROLE_POOL_USD - priorCostUsd));
  return Object.freeze({
    schema_version: 1,
    stage_cap_usd: CLAUDE_DEEPSEEK_E2E_USD_LIMIT,
    role,
    role_pool_usd: CLAUDE_DEEPSEEK_MODEL_CERT_ROLE_POOL_USD,
    prior_cost_usd: priorCostUsd,
    effective_call_cap_usd: effectiveCallCapUsd,
    enforcement: CLAUDE_DEEPSEEK_MODEL_CERT_BUDGET_ENFORCEMENT,
  });
}

export function claimRoleAttempt(privateRoot, role, attempt) {
  requireWrapper(Object.hasOwn(ROLE_SPEC, role) && ["PRIMARY", "REPAIR"].includes(attempt), "CLAUDE_DEEPSEEK_ROLE_CLAIM_INVALID", "Evidence V2 role claim is invalid");
  const claimsRoot = path.join(path.resolve(privateRoot), "model-role-claims");
  fs.mkdirSync(claimsRoot, { recursive: true, mode: 0o700 });
  const existing = new Set(fs.readdirSync(claimsRoot, { withFileTypes: true }).filter((item) => item.isDirectory()).map((item) => item.name));
  const primary = attemptKey(role, "PRIMARY");
  const repair = attemptKey(role, "REPAIR");
  if (attempt === "PRIMARY") {
    requireWrapper(!existing.has(primary) && !existing.has(repair), "CLAUDE_DEEPSEEK_ROLE_RETRY_FORBIDDEN", `${role} already consumed its primary model call`);
  } else {
    requireWrapper(existing.has(primary) && !existing.has(repair), "CLAUDE_DEEPSEEK_ROLE_REPAIR_FORBIDDEN", `${role} repair requires exactly one rejected primary and cannot repeat`);
  }
  requireWrapper(existing.size < 4, "CLAUDE_DEEPSEEK_MODEL_CALL_CAP", "Evidence V2 model-cert exceeded its four-call hard cap");
  const claim = path.join(claimsRoot, attemptKey(role, attempt));
  try { fs.mkdirSync(claim, { mode: 0o700 }); }
  catch (error) {
    if (error?.code === "EEXIST") fail("CLAUDE_DEEPSEEK_ROLE_RETRY_FORBIDDEN", `${role} ${attempt} model call is already claimed`);
    throw error;
  }
  return claim;
}

function portablePermissionPath(value) {
  const resolved = path.resolve(value);
  if (/[\r\n*?]/u.test(resolved)) fail("CLAUDE_DEEPSEEK_SERVICE_WORKSPACE_PATH_INVALID", "Job workspace path cannot be represented in a Claude permission rule");
  const drive = /^([A-Za-z]):[\\/](.*)$/u.exec(resolved);
  const portable = drive ? `${drive[1]}/${drive[2].replaceAll("\\", "/")}` : resolved.split(path.sep).join("/").replace(/^\/+/, "");
  return `//${portable.replaceAll("\\", "\\\\").replaceAll("(", "\\(").replaceAll(")", "\\)")}`;
}

export function roleToolPolicy({ workspaceRoot, output }) {
  const requestFile = portablePermissionPath(path.join(workspaceRoot, "inputs", "request.json"));
  const outputFile = portablePermissionPath(path.join(workspaceRoot, ...output.split("/")));
  const policy = {
    schema_version: 1,
    tools: ["Read", "Write"],
    // Claude Code emits file creation as Write, while its permission matcher
    // authorizes that tool with the Edit(path) permission category.
    allowed_tools: [`Read(${requestFile})`, `Edit(${outputFile})`],
    readable_scope: "job-request-only",
    writable_scope: output,
    network: false,
    shell: false,
    skill_loading: false,
  };
  return Object.freeze({ ...policy, sha256: sha256Bytes(canonicalJson(policy)) });
}

function inside(root, candidate) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return relative !== "" && relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}

function resolveToolPath(workspaceRoot, value) {
  requireWrapper(typeof value === "string" && value.length > 0 && !/[\r\n\0]/u.test(value), "CLAUDE_DEEPSEEK_ROLE_TOOL_PATH_INVALID", "Evidence V2 file-tool path is invalid");
  const portable = value.replaceAll("\\", "/");
  if (/^\/(?:inputs|output)\//u.test(portable)) return path.join(workspaceRoot, ...portable.slice(1).split("/"));
  return path.isAbsolute(value) ? path.resolve(value) : path.resolve(workspaceRoot, value);
}

export function auditRoleWorkspace({ workspaceRoot, roleSpec, processResult }) {
  const inputs = path.join(workspaceRoot, "inputs");
  validateRoleWorkspaceInputs(workspaceRoot);
  const request = path.join(inputs, "request.json");
  const forbidden = [
    path.join(inputs, "target_logs.json"),
    path.join(inputs, "logparse-receipt.json"),
    path.join(inputs, "attachments"),
    path.join(inputs, "target-logs"),
  ];
  requireWrapper(forbidden.every((target) => !fs.existsSync(target)), "CLAUDE_DEEPSEEK_ROLE_INPUT_LEAK", "Evidence V2 role workspace contains raw Logparse or attachment inputs");
  requireWrapper(Array.isArray(processResult?.records) && processResult.records.length > 0, "CLAUDE_DEEPSEEK_ROLE_TRACE_INVALID", "Evidence V2 role must produce a bounded file-tool trace");
  const outputRoot = path.join(workspaceRoot, "output");
  const expectedOutput = path.join(workspaceRoot, ...roleSpec.output.split("/"));
  let reads = 0;
  const writes = [];
  for (const record of processResult.records) {
    requireWrapper(record?.is_error !== true, "CLAUDE_DEEPSEEK_ROLE_TOOL_FAILED", "Evidence V2 role file tool failed or was denied");
    const target = resolveToolPath(workspaceRoot, record?.input?.file_path);
    if (record.name === "Read" && target === request) reads += 1;
    else if (record.name === "Write" && inside(outputRoot, target) && target === expectedOutput && typeof record.input?.content === "string") writes.push(record);
    else fail("CLAUDE_DEEPSEEK_ROLE_TOOL_SCOPE_INVALID", "Evidence V2 role used a tool or path outside its frozen Read/Write policy");
  }
  requireWrapper(writes.length === 1, "CLAUDE_DEEPSEEK_ROLE_WRITE_COUNT_INVALID", "Evidence V2 role must write its draft exactly once");
  requireWrapper(fs.existsSync(expectedOutput) && fs.statSync(expectedOutput).isFile(), "CLAUDE_DEEPSEEK_ROLE_DRAFT_MISSING", "Evidence V2 role output draft is missing");
  const disk = fs.readFileSync(expectedOutput);
  requireWrapper(disk.equals(Buffer.from(writes[0].input.content, "utf8")), "CLAUDE_DEEPSEEK_ROLE_DRAFT_MISMATCH", "Evidence V2 role draft differs from the successful Write input");
  return Object.freeze({
    schema_version: 1,
    status: "PASS",
    role: roleSpec.role,
    attempt: roleSpec.attempt,
    reads,
    writes: 1,
    output_path: roleSpec.output,
    output_size: disk.length,
    output_sha256: sha256Bytes(disk),
    harness_normalized: false,
  });
}

function validateRoleWorkspaceInputs(workspaceRoot) {
  const inputs = path.join(workspaceRoot, "inputs");
  const request = path.join(inputs, "request.json");
  requireWrapper(
    LEGACY_ROLE_INPUTS.every((relative) => !fs.existsSync(path.join(workspaceRoot, ...relative.split("/")))),
    "CLAUDE_DEEPSEEK_LEGACY_MODEL_INPUT_EXPOSED",
    "Evidence V2 role workspace exposes legacy Graph/Plan model inputs",
  );
  requireWrapper(
    !fs.existsSync(path.join(workspaceRoot, "runtime", "context.txt")),
    "CLAUDE_DEEPSEEK_DUPLICATE_CONTEXT_EXPOSED",
    "Evidence V2 role workspace exposes a duplicate runtime context",
  );
  let inputNames;
  try { inputNames = fs.readdirSync(inputs).sort(); }
  catch { fail("CLAUDE_DEEPSEEK_ROLE_INPUT_MISSING", "Evidence V2 role workspace inputs are unavailable"); }
  requireWrapper(
    canonicalJson(inputNames) === canonicalJson(["manifest.json", "request.json"]),
    "CLAUDE_DEEPSEEK_ROLE_INPUT_LEAK",
    "Evidence V2 role workspace inputs are not model-minimal",
  );
  for (const name of inputNames) {
    const metadata = fs.lstatSync(path.join(inputs, name));
    requireWrapper(
      metadata.isFile() && !metadata.isSymbolicLink(),
      "CLAUDE_DEEPSEEK_ROLE_INPUT_INVALID",
      "Evidence V2 role inputs must be ordinary files",
    );
  }
  const runtime = path.join(workspaceRoot, "runtime");
  let runtimeNames;
  try { runtimeNames = fs.readdirSync(runtime).sort(); }
  catch { fail("CLAUDE_DEEPSEEK_ROLE_RUNTIME_INVALID", "Evidence V2 role runtime directory is unavailable"); }
  requireWrapper(
    canonicalJson(runtimeNames) === canonicalJson(["tool-state"]),
    "CLAUDE_DEEPSEEK_ROLE_RUNTIME_LEAK",
    "Evidence V2 role runtime directory contains an unexpected entry",
  );
  const toolState = fs.lstatSync(path.join(runtime, "tool-state"));
  requireWrapper(
    toolState.isDirectory() && !toolState.isSymbolicLink(),
    "CLAUDE_DEEPSEEK_ROLE_RUNTIME_INVALID",
    "Evidence V2 role tool-state must be an ordinary directory",
  );
}

function privateEnvironment(claim) {
  const home = path.join(claim, "home");
  const temporary = path.join(claim, "tmp");
  fs.mkdirSync(home, { recursive: true, mode: 0o700 });
  fs.mkdirSync(temporary, { recursive: true, mode: 0o700 });
  return { home, temporary };
}

export async function runServiceInvocation(values, {
  stdin = process.stdin,
  stdout = process.stdout,
  ambient = process.env,
  runClaude = runClaudeProcess,
} = {}) {
  const workspaceRoot = process.cwd();
  const prompt = await readPrompt(stdin);
  const parsed = parseMethodsRolePrompt(prompt);
  const roleSpec = { ...parsed, role: parsed.role };
  const policy = roleToolPolicy({ workspaceRoot, output: parsed.output });
  const key = attemptKey(parsed.role, parsed.attempt);
  const traceRoot = path.join(path.resolve(values["evidence-root"]), "model-role-invocations");
  fs.mkdirSync(traceRoot, { recursive: true, mode: 0o700 });
  const progressPath = path.join(traceRoot, `${key}.progress`);
  const startedAtUtc = new Date().toISOString();
  let result = null;
  let budget = null;
  let claim = null;
  try {
    claim = claimRoleAttempt(values["private-root"], parsed.role, parsed.attempt);
    validateRoleWorkspaceInputs(workspaceRoot);
    budget = roleInvocationBudget(values["usage-root"], parsed.role, parsed.attempt);
    requireWrapper(budget.effective_call_cap_usd > 0, "CLAUDE_DEEPSEEK_ROLE_BUDGET_EXHAUSTED", `${parsed.role} exhausted its model-cert role budget`);
    try { fs.writeFileSync(progressPath, "", { encoding: "utf8", mode: 0o600, flag: "wx" }); }
    catch (error) {
      if (error?.code === "EEXIST") fail("CLAUDE_DEEPSEEK_ROLE_PROGRESS_EXISTS", `${parsed.role} ${parsed.attempt} progress receipt already exists`);
      throw error;
    }
    result = await runClaude({
      claudeEntry: path.resolve(values["claude-entry"]),
      settings: path.resolve(values.settings),
      cwd: workspaceRoot,
      prompt,
      phase: parsed.role,
      invocationId: `${values["run-id"]}:${key}`,
      tools: policy.tools,
      allowedTools: policy.allowed_tools,
      disallowedTools: ["Bash", "Glob", "Grep", "Skill"],
      auditOnlyAllowedTools: ["Bash", "Glob", "Grep", "Skill"],
      maxTurns: CLAUDE_DEEPSEEK_E2E_MAX_TURNS,
      maxBudgetUsd: budget.effective_call_cap_usd,
      wallTimeoutSeconds: CLAUDE_DEEPSEEK_CALL_WALL_SECONDS,
      noProgressSeconds: CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
      tracePath: path.join(traceRoot, `${key}.stream-json.ndjson`),
      stderrPath: path.join(traceRoot, `${key}.stderr.txt`),
      environment: {
        configRoot: path.resolve(values["config-root"]),
        ...privateEnvironment(claim),
      },
    }, { ambient, onProgress: () => {
      fs.appendFileSync(progressPath, ".\n", { encoding: "utf8" });
      stdout.write(`TEST_FLOW_PROGRESS stage.progress claude-deepseek ${key}\n`);
    } });
    const terminalUsage = normalizedUsageOrNull(result?.receipt?.usage);
    requireWrapper(terminalUsage !== null, "CLAUDE_DEEPSEEK_ROLE_USAGE_INVALID", "Evidence V2 role terminal usage is incomplete");
    requireWrapper(
      terminalUsage.cost_usd <= budget.effective_call_cap_usd,
      "CLAUDE_DEEPSEEK_CALL_BUDGET_EXCEEDED",
      "Evidence V2 role terminal cost exceeded its effective Claude CLI threshold",
    );
    const providerTerminal = closedProviderTerminal(result?.receipt?.provider_terminal);
    requireWrapper(
      providerTerminal?.subtype === "success"
        && providerTerminal.is_error === false
        && providerTerminal.exit_code === 0
        && providerTerminal.signal === null,
      "CLAUDE_DEEPSEEK_ROLE_TERMINAL_INVALID",
      "Evidence V2 role provider terminal is not one closed successful process result",
    );
    requireWrapper(result.skills.length === 0 && result.bash.length === 0 && result.mcp.length === 0, "CLAUDE_DEEPSEEK_ROLE_NON_FILE_TOOL", "Evidence V2 role attempted a Skill, shell, or MCP call");
    requireWrapper(result.denied.every((item) => item.executed === false), "CLAUDE_DEEPSEEK_ROLE_DENIED_EXECUTION", "A denied Evidence V2 role tool executed");
    const workspaceAudit = auditRoleWorkspace({ workspaceRoot, roleSpec, processResult: result });
    const receipt = Object.freeze({
      ...result.receipt,
      workflow: `${parsed.role}:${parsed.attempt}`,
      role: parsed.role,
      evaluation_attempt: parsed.attempt,
      role_call_ordinal: parsed.attempt === "PRIMARY" ? 1 : 2,
      max_budget_usd: budget.effective_call_cap_usd,
      budget,
      provider_terminal: providerTerminal,
      usage: terminalUsage,
      prompt: {
        sha256: sha256Bytes(prompt),
        utf8_size: Buffer.byteLength(prompt, "utf8"),
      },
      tool_policy: policy,
      workspace_audit: workspaceAudit,
      usage_complete: true,
      failure_code: null,
    });
    validateClaudeDeepseekRoleReceipt(receipt, {
      planCaps: CLAUDE_DEEPSEEK_MODEL_CERT_PLAN_CAPS,
      expectedRole: parsed.role,
      expectedAttempt: parsed.attempt,
      priorCostUsd: budget.prior_cost_usd,
    });
    writeJsonNew(path.join(path.resolve(values["usage-root"]), `${key}.json`), receipt);
    writeJsonNew(path.join(traceRoot, `${key}.receipt.json`), receipt);
    const terminal = result.events.at(-1);
    stdout.write(`${String(terminal?.result ?? "")}\n`);
    return receipt;
  } catch (error) {
    if (claim === null) throw error;
    const observed = error?.details?.terminal ?? null;
    const usage = normalizedUsageOrNull(result?.receipt?.usage ?? observed?.usage ?? null);
    const hasProcessExit = Object.hasOwn(error?.details ?? {}, "exit_code") || Object.hasOwn(error?.details ?? {}, "signal");
    const providerTerminal = closedProviderTerminal(result?.receipt?.provider_terminal) ?? (
      observed === null && !hasProcessExit ? null : closedProviderTerminal({
        subtype: observed?.subtype ?? null,
        is_error: observed?.is_error ?? null,
        stop_reason: observed?.stop_reason ?? null,
        exit_code: error?.details?.exit_code ?? null,
        signal: error?.details?.signal ?? null,
      })
    );
    const receipt = Object.freeze({
      schema_version: 1,
      invocation_id: `${values["run-id"]}:${key}`,
      phase: parsed.role,
      model: result?.receipt?.model ?? CLAUDE_DEEPSEEK_MODEL,
      attempt: 1,
      retry: 0,
      status: "FAIL",
      terminal: true,
      started_at_utc: result?.receipt?.started_at_utc ?? startedAtUtc,
      finished_at_utc: new Date().toISOString(),
      turns: result?.receipt?.turns ?? observed?.turns ?? 0,
      wall_timeout_seconds: CLAUDE_DEEPSEEK_CALL_WALL_SECONDS,
      max_turns: CLAUDE_DEEPSEEK_E2E_MAX_TURNS,
      max_output_tokens: CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS,
      appended_system_prompt: null,
      workflow: `${parsed.role}:${parsed.attempt}`,
      role: parsed.role,
      evaluation_attempt: parsed.attempt,
      role_call_ordinal: parsed.attempt === "PRIMARY" ? 1 : 2,
      max_budget_usd: budget?.effective_call_cap_usd ?? null,
      budget,
      disallowed_tools: ["Bash", "Glob", "Grep", "Skill"],
      prompt: {
        sha256: sha256Bytes(prompt),
        utf8_size: Buffer.byteLength(prompt, "utf8"),
      },
      tool_policy: policy,
      workspace_audit: null,
      environment_policy: result?.receipt?.environment_policy ?? null,
      provider_terminal: providerTerminal,
      usage_complete: usage !== null,
      usage,
      failure_code: typeof error?.code === "string" ? error.code : "CLAUDE_DEEPSEEK_MODEL_CALL_FAILED",
    });
    writeJsonNew(path.join(path.resolve(values["usage-root"]), `${key}.json`), receipt);
    writeJsonNew(path.join(traceRoot, `${key}.receipt.json`), receipt);
    throw error;
  }
}

export function readRoleInvocationReceipts(usageRoot) {
  const root = path.resolve(usageRoot);
  if (!fs.existsSync(root)) return [];
  const order = ["specialist-primary", "specialist-repair", "reviewer-primary", "reviewer-repair"];
  return order
    .map((key) => path.join(root, `${key}.json`))
    .filter((target) => fs.existsSync(target))
    .map((target) => JSON.parse(fs.readFileSync(target, "utf8")));
}

export function safeServiceError(error) {
  return {
    schema_version: 1,
    status: "FAIL",
    code: error?.code ?? "CLAUDE_DEEPSEEK_SERVICE_WRAPPER_FAILED",
    message: error?.message ?? String(error),
  };
}

async function main() {
  try { await runServiceInvocation(parseArguments(process.argv.slice(2))); }
  catch (error) {
    process.stderr.write(canonicalJson(safeServiceError(error)));
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === MODULE_PATH) await main();
