#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  canonicalJson,
  CODEX_LUNA_MODEL,
  CODEX_LUNA_REASONING_EFFORT,
  sha256Bytes,
} from "../../../runtime-support/codex-luna-contract.mjs";
import {
  readCodexLunaExternalAuth,
  runCodexLunaAppServerCall,
} from "../../../runtime-support/codex-luna-app-server-runtime.mjs";
import {
  MACOS_CODEX_LUNA_CALL_WALL_SECONDS,
  MACOS_CODEX_LUNA_NO_PROGRESS_SECONDS,
} from "./macos-codex-luna-e2e-contract.mjs";
import {
  removeLinuxServiceProject,
  stageLinuxServiceProject,
} from "./macos-codex-luna-service-wrapper.mjs";

const MODULE_PATH = fileURLToPath(import.meta.url);
const MAX_PROMPT_BYTES = 4 * 1024 * 1024;
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
const ROLE_MARKER = /<<<METHODS_EVIDENCE_V2_ROLE>>>\nRole: (Specialist|Reviewer)\. Attempt: (primary evaluation|only repair)\.\n[\s\S]*?\n<<<END METHODS_EVIDENCE_V2_ROLE>>>\n?$/u;
const ROLE_DATA_SECTION_HEADER = /<<<SECTION \d+ (EVIDENCE|REVIEW_TARGET)>>>/gu;
const ROLE_CONTEXT_SECTION = /<<<SECTION \d+ (EVIDENCE|REVIEW_TARGET)>>>\n([^\n]+)\n<<<END SECTION>>>/gu;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u;
const IDENTITY_TOKEN = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)*=[^\s,;]+$/u;
const MODEL_INPUT_NAMES = Object.freeze(["manifest.json", "request.json"]);
const MODEL_RUNTIME_NAMES = Object.freeze(["tool-state"]);
const LEGACY_ROLE_INPUTS = Object.freeze([
  "inputs/method-evidence-graph.json",
  "inputs/method-evaluation-plan.json",
]);
const FORBIDDEN_ROLE_FILES = Object.freeze([
  ...LEGACY_ROLE_INPUTS,
  "runtime/context.txt",
]);
const ROLE_CONFIG = Object.freeze({
  SPECIALIST: Object.freeze({
    label: "Specialist",
    output: "output/method-diagnosis.draft.json",
  }),
  REVIEWER: Object.freeze({
    label: "Reviewer",
    output: "output/method-review.draft.json",
  }),
});

export const CODEX_LUNA_MODEL_CERT_WRAPPER_VERSION = 1;
export const CODEX_LUNA_MODEL_CERT_NORMAL_CALLS = 1;
export const CODEX_LUNA_MODEL_CERT_MAX_CALLS = 2;
export const CODEX_LUNA_MODEL_CERT_BLIND_REVIEW_NORMAL_CALLS = 2;
export const CODEX_LUNA_MODEL_CERT_BLIND_REVIEW_MAX_CALLS = 4;

class ModelCertWrapperError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "ModelCertWrapperError";
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) {
  throw new ModelCertWrapperError(code, message, details);
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

function hasExactKeys(value, expected) {
  return isPlainObject(value)
    && Object.keys(value).sort().join("\0") === [...expected].sort().join("\0");
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

function validateCompactEvaluationInput(value) {
  requireWrapper(
    hasExactKeys(value, [
      "schema_version",
      "evidence_graph_ref",
      "plan_ref",
      "limitations",
      "sources",
      "observations",
      "markers",
      "evaluations",
    ])
      && value.schema_version === 2
      && /^graph-[0-9a-f]{64}$/u.test(value.evidence_graph_ref)
      && /^plan-[0-9a-f]{64}$/u.test(value.plan_ref)
      && Array.isArray(value.limitations)
      && Array.isArray(value.sources)
      && Array.isArray(value.observations)
      && Array.isArray(value.markers)
      && Array.isArray(value.evaluations)
      && value.evaluations.length > 0,
    "CODEX_LUNA_MODEL_CERT_EVALUATION_INPUT_INVALID",
    "Production role prompt has an invalid compact evaluation_input",
  );
  requireWrapper(
    value.limitations.every((item) => typeof item === "string" && item.length > 0)
      && new Set(value.limitations).size === value.limitations.length,
    "CODEX_LUNA_MODEL_CERT_EVALUATION_INPUT_INVALID",
    "Compact evaluation limitations are invalid",
  );

  const sourceNames = new Set();
  let previousSourceKey = null;
  value.sources.forEach((item, index) => {
    const sourceKey = [item?.source_id, item?.relative_path];
    requireWrapper(
      hasExactKeys(item, ["id", "source_id", "relative_path"])
        && item.id === index + 1
        && /^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*$/u.test(item.source_id)
        && typeof item.relative_path === "string"
        && item.relative_path.length > 0
        && !item.relative_path.startsWith("/")
        && !item.relative_path.includes("\\")
        && item.relative_path.split("/").every((part) => part.length > 0 && part !== "." && part !== "..")
        && !sourceNames.has(item.source_id)
        && (previousSourceKey === null || compareDeterministicTuple(previousSourceKey, sourceKey) < 0),
      "CODEX_LUNA_MODEL_CERT_EVALUATION_INPUT_INVALID",
      "Compact evaluation sources are invalid or not in deterministic order",
    );
    sourceNames.add(item.source_id);
    previousSourceKey = sourceKey;
  });

  const observationIds = new Set();
  const physicalLines = new Set();
  let previousObservationKey = null;
  value.observations.forEach((item, index) => {
    const observationKey = [item?.source_id, item?.line_number, item?.line];
    requireWrapper(
      hasExactKeys(item, ["id", "source_id", "line_number", "line"])
        && item.id === index + 1
        && typeof item.source_id === "string"
        && sourceNames.has(item.source_id)
        && positiveInteger(item.line_number)
        && typeof item.line === "string"
        && item.line.length > 0
        && (previousObservationKey === null || compareDeterministicTuple(previousObservationKey, observationKey) < 0),
      "CODEX_LUNA_MODEL_CERT_EVALUATION_INPUT_INVALID",
      "Compact evaluation observations are invalid",
    );
    observationIds.add(item.id);
    physicalLines.add(`${item.source_id}\0${item.line_number}`);
    previousObservationKey = observationKey;
  });
  requireWrapper(
    physicalLines.size === value.observations.length,
    "CODEX_LUNA_MODEL_CERT_EVALUATION_INPUT_INVALID",
    "Compact evaluation observations repeat one physical log line",
  );

  const markerIds = new Set();
  const markerLiterals = new Set();
  let previousMarker = null;
  value.markers.forEach((item, index) => {
    requireWrapper(
      hasExactKeys(item, ["id", "literal"])
        && item.id === index + 1
        && typeof item.literal === "string"
        && item.literal.length > 0
        && (previousMarker === null || compareUnicodeText(previousMarker, item.literal) < 0),
      "CODEX_LUNA_MODEL_CERT_EVALUATION_INPUT_INVALID",
      "Compact evaluation markers are invalid",
    );
    markerIds.add(item.id);
    markerLiterals.add(item.literal);
    previousMarker = item.literal;
  });
  requireWrapper(
    markerLiterals.size === value.markers.length,
    "CODEX_LUNA_MODEL_CERT_EVALUATION_INPUT_INVALID",
    "Compact evaluation marker literals are not unique",
  );

  const evaluationRefs = new Set();
  const methodIds = new Set();
  const eventRefs = new Set();
  const qualifiedMatches = new Set();
  const usedObservationIds = new Set();
  const usedMarkerIds = new Set();
  let previousMethodKey = null;
  for (const evaluation of value.evaluations) {
    requireWrapper(
      hasExactKeys(evaluation, ["evaluation_ref", "method_id", "method_priority", "events"])
        && /^eval-[0-9a-f]{64}$/u.test(evaluation.evaluation_ref)
        && /^[a-z0-9]+(?:-[a-z0-9]+)*$/u.test(evaluation.method_id)
        && positiveInteger(evaluation.method_priority)
        && Array.isArray(evaluation.events)
        && evaluation.events.length > 0,
      "CODEX_LUNA_MODEL_CERT_EVALUATION_INPUT_INVALID",
      "Compact evaluation item is invalid",
    );
    const methodKey = [evaluation.method_priority, evaluation.method_id];
    requireWrapper(
      previousMethodKey === null || compareDeterministicTuple(previousMethodKey, methodKey) < 0,
      "CODEX_LUNA_MODEL_CERT_EVALUATION_INPUT_INVALID",
      "Compact evaluations are not in deterministic plan order",
    );
    previousMethodKey = methodKey;
    requireWrapper(
      !evaluationRefs.has(evaluation.evaluation_ref) && !methodIds.has(evaluation.method_id),
      "CODEX_LUNA_MODEL_CERT_EVALUATION_INPUT_INVALID",
      "Compact evaluations are not unique",
    );
    evaluationRefs.add(evaluation.evaluation_ref);
    methodIds.add(evaluation.method_id);
    for (const event of evaluation.events) {
      const identityNames = Array.isArray(event?.identity_tokens)
        ? event.identity_tokens.map((item) => typeof item === "string" ? item.split("=", 1)[0] : null)
        : [];
      requireWrapper(
        hasExactKeys(event, ["event_ref", "identity_tokens", "matches"])
          && /^event-[0-9a-f]{64}$/u.test(event.event_ref)
          && Array.isArray(event.identity_tokens)
          && event.identity_tokens.every((item) => typeof item === "string" && IDENTITY_TOKEN.test(item))
          && new Set(event.identity_tokens).size === event.identity_tokens.length
          && new Set(identityNames).size === identityNames.length
          && Array.isArray(event.matches)
          && event.matches.length > 0
          && !eventRefs.has(event.event_ref),
        "CODEX_LUNA_MODEL_CERT_EVALUATION_INPUT_INVALID",
        "Compact evaluation event is invalid",
      );
      eventRefs.add(event.event_ref);
      for (const match of event.matches) {
        const qualified = `${evaluation.method_id}\0${match?.observation_id}\0${match?.marker_id}\0${match?.method_marker_index}`;
        requireWrapper(
          hasExactKeys(match, ["observation_id", "marker_id", "method_marker_index"])
            && observationIds.has(match.observation_id)
            && markerIds.has(match.marker_id)
            && positiveInteger(match.method_marker_index)
            && !qualifiedMatches.has(qualified),
          "CODEX_LUNA_MODEL_CERT_EVALUATION_INPUT_INVALID",
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
    "CODEX_LUNA_MODEL_CERT_EVALUATION_INPUT_INVALID",
    "Compact evaluation_input does not exactly cover its observations and markers",
  );
  return value;
}

function validateReviewTarget(target, evaluationInput) {
  requireWrapper(
    hasExactKeys(target, [
      "schema_version",
      "evaluation_id",
      "source_job_id",
      "graph_ref",
      "plan_ref",
      "skill_ref",
      "reviewed_state_revision",
    ])
      && target.schema_version === 2
      && UUID.test(target.evaluation_id)
      && UUID.test(target.source_job_id)
      && target.graph_ref === evaluationInput.evidence_graph_ref
      && target.plan_ref === evaluationInput.plan_ref
      && hasExactKeys(target.skill_ref, ["id", "version", "content_hash"])
      && typeof target.skill_ref.id === "string"
      && target.skill_ref.id.length > 0
      && typeof target.skill_ref.version === "string"
      && target.skill_ref.version.length > 0
      && /^[0-9a-f]{64}$/u.test(target.skill_ref.content_hash)
      && positiveInteger(target.reviewed_state_revision),
    "CODEX_LUNA_MODEL_CERT_ROLE_IDENTITY_INVALID",
    "Reviewer role target is not one closed production identity",
  );
}

function validateRolePayload(payload, role) {
  const expectedKeys = role === "SPECIALIST"
    ? ["schema_version", "role", "job_id", "case_id", "request_path", "evaluation_input"]
    : ["schema_version", "role", "target", "request_path", "evaluation_input"];
  requireWrapper(
    hasExactKeys(payload, expectedKeys)
      && payload.schema_version === 2
      && payload.role === role
      && payload.request_path === "inputs/request.json"
      && isPlainObject(payload.evaluation_input),
    "CODEX_LUNA_MODEL_CERT_ROLE_PAYLOAD_INVALID",
    "Production role data section has an invalid exact shape",
  );
  const evaluationInput = validateCompactEvaluationInput(payload.evaluation_input);
  if (role === "SPECIALIST") {
    requireWrapper(
      UUID.test(payload.job_id) && UUID.test(payload.case_id),
      "CODEX_LUNA_MODEL_CERT_ROLE_IDENTITY_INVALID",
      "Specialist role identity is invalid",
    );
  } else {
    validateReviewTarget(payload.target, evaluationInput);
  }
  return payload;
}

function rolePayloadFromPrompt(prompt, role) {
  const exposedInput = FORBIDDEN_ROLE_FILES.find((relative) => prompt.includes(relative));
  requireWrapper(
    exposedInput === undefined,
    exposedInput === "runtime/context.txt"
      ? "CODEX_LUNA_MODEL_CERT_CONTEXT_FILE_EXPOSED"
      : "CODEX_LUNA_MODEL_CERT_LEGACY_INPUT_EXPOSED",
    "Production role prompt exposes a forbidden duplicate input path",
  );
  const expectedKind = role === "SPECIALIST" ? "EVIDENCE" : "REVIEW_TARGET";
  const headers = [...prompt.matchAll(new RegExp(ROLE_DATA_SECTION_HEADER.source, "gu"))];
  const sections = [...prompt.matchAll(new RegExp(ROLE_CONTEXT_SECTION.source, "gu"))];
  requireWrapper(
    headers.length === 1
      && sections.length === 1
      && headers[0][1] === expectedKind
      && sections[0][1] === expectedKind,
    "CODEX_LUNA_MODEL_CERT_ROLE_DATA_SECTION_INVALID",
    "Production role prompt must contain exactly one matching Methods role data section",
  );
  let payload;
  try { payload = JSON.parse(sections[0][2]); } catch {
    fail("CODEX_LUNA_MODEL_CERT_ROLE_PAYLOAD_INVALID", "Production role data section is not valid JSON");
  }
  return validateRolePayload(payload, role);
}

function writeJsonExclusive(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, `${canonicalJson(value)}\n`, {
    encoding: "utf8",
    mode: 0o600,
    flag: "wx",
  });
}

export function parseModelCertWrapperArguments(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    requireWrapper(
      typeof name === "string"
        && name.startsWith("--")
        && index + 1 < argv.length
        && !argv[index + 1].startsWith("--"),
      "CODEX_LUNA_MODEL_CERT_ARGUMENT_INVALID",
      "Model cert wrapper arguments must use --name value pairs",
    );
    const key = name.slice(2);
    requireWrapper(
      !Object.hasOwn(values, key),
      "CODEX_LUNA_MODEL_CERT_ARGUMENT_DUPLICATE",
      "Model cert wrapper argument is duplicated",
      { field: key },
    );
    values[key] = argv[index + 1];
  }
  const required = [
    "codex-entry",
    "auth-source",
    "skill-source",
    "expected-cli-version",
    "private-root",
    "evidence-root",
    "usage-root",
    "run-id",
  ];
  requireWrapper(
    required.every((name) => typeof values[name] === "string" && values[name].length > 0),
    "CODEX_LUNA_MODEL_CERT_ARGUMENT_MISSING",
    "Model cert wrapper arguments are incomplete",
  );
  return values;
}

async function readPrompt(stream) {
  const chunks = [];
  let total = 0;
  for await (const chunk of stream) {
    const bytes = Buffer.from(chunk);
    total += bytes.length;
    requireWrapper(
      total <= MAX_PROMPT_BYTES,
      "CODEX_LUNA_MODEL_CERT_PROMPT_LIMIT",
      "Production role prompt exceeds the wrapper byte cap",
    );
    chunks.push(bytes);
  }
  let prompt;
  try {
    prompt = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
  } catch {
    fail("CODEX_LUNA_MODEL_CERT_PROMPT_UTF8", "Production role prompt must be UTF-8");
  }
  requireWrapper(prompt.length > 0, "CODEX_LUNA_MODEL_CERT_PROMPT_EMPTY", "Production role prompt is empty");
  return prompt;
}

export function parseEvidenceV2RolePrompt(prompt) {
  requireWrapper(typeof prompt === "string" && prompt.length > 0, "CODEX_LUNA_MODEL_CERT_PROMPT_INVALID", "Production role prompt is invalid");
  const matches = [...prompt.matchAll(new RegExp(ROLE_MARKER.source, "gu"))];
  requireWrapper(
    matches.length === 1,
    "CODEX_LUNA_MODEL_CERT_ROLE_MARKER_INVALID",
    "Production prompt must contain exactly one terminal Evidence V2 role marker",
  );
  const [, roleLabel, attemptLabel] = matches[0];
  const role = roleLabel === "Specialist" ? "SPECIALIST" : "REVIEWER";
  const attempt = attemptLabel === "primary evaluation" ? "PRIMARY" : "REPAIR";
  const output = ROLE_CONFIG[role].output;
  requireWrapper(
    matches[0][0].includes(`Write only ${output}.`),
    "CODEX_LUNA_MODEL_CERT_OUTPUT_PATH_MISMATCH",
    "Production role marker does not name the fixed role output",
  );
  const rolePayload = rolePayloadFromPrompt(prompt, role);
  return Object.freeze({ role, attempt, output, rolePayload });
}

function claimInvocation(privateRoot, role, attempt) {
  const claimsRoot = path.join(path.resolve(privateRoot), "model-cert-claims");
  fs.mkdirSync(claimsRoot, { recursive: true, mode: 0o700 });
  const roleRoot = path.join(claimsRoot, role.toLowerCase());
  fs.mkdirSync(roleRoot, { recursive: true, mode: 0o700 });
  const primary = path.join(roleRoot, "primary");
  const repair = path.join(roleRoot, "repair");
  if (attempt === "REPAIR") {
    requireWrapper(
      fs.existsSync(primary),
      "CODEX_LUNA_MODEL_CERT_REPAIR_WITHOUT_PRIMARY",
      "A role repair cannot run before its primary invocation",
      { role },
    );
  }
  const claim = attempt === "PRIMARY" ? primary : repair;
  try {
    fs.mkdirSync(claim, { mode: 0o700 });
  } catch (error) {
    if (error?.code === "EEXIST") {
      fail(
        "CODEX_LUNA_MODEL_CERT_DUPLICATE_ATTEMPT",
        "A role attempt already has an invocation claim",
        { role, attempt },
      );
    }
    throw error;
  }
  const claims = fs.readdirSync(claimsRoot, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isDirectory() && ["primary", "repair"].includes(entry.name));
  requireWrapper(
    claims.length <= CODEX_LUNA_MODEL_CERT_BLIND_REVIEW_MAX_CALLS,
    "CODEX_LUNA_MODEL_CERT_CALL_LIMIT",
    "Evidence V2 model cert exceeded the four-call hard limit",
  );
  return claim;
}

export function readModelCertInvocationReceipts(
  usageRoot,
  {
    allowFailurePrefix = false,
    evaluationMode = "SPECIALIST_ONLY",
  } = {},
) {
  requireWrapper(
    ["SPECIALIST_ONLY", "BLIND_CONSENSUS"].includes(evaluationMode),
    "CODEX_LUNA_MODEL_CERT_EVALUATION_MODE_INVALID",
    "Model cert evaluation mode is invalid",
  );
  const blindConsensus = evaluationMode === "BLIND_CONSENSUS";
  const root = path.resolve(usageRoot);
  const order = [
    ["SPECIALIST", "PRIMARY", "specialist-primary.json", true],
    ["SPECIALIST", "REPAIR", "specialist-repair.json", false],
    ["REVIEWER", "PRIMARY", "reviewer-primary.json", blindConsensus],
    ["REVIEWER", "REPAIR", "reviewer-repair.json", false],
  ];
  let names;
  try {
    names = fs.readdirSync(root).filter((name) => name.endsWith(".json")).sort();
  } catch {
    fail("CODEX_LUNA_MODEL_CERT_USAGE_ROOT_INVALID", "Model cert usage root is unavailable");
  }
  const allowed = new Set(order
    .filter(([role]) => blindConsensus || role === "SPECIALIST")
    .map(([, , name]) => name));
  requireWrapper(
    names.every((name) => allowed.has(name)),
    "CODEX_LUNA_MODEL_CERT_USAGE_FILE_UNEXPECTED",
    "Model cert usage root contains an unexpected receipt",
  );
  const receipts = [];
  for (const [role, attempt, name, required] of order) {
    const receiptPath = path.join(root, name);
    if (!fs.existsSync(receiptPath)) {
      requireWrapper(allowFailurePrefix || !required, "CODEX_LUNA_MODEL_CERT_USAGE_RECEIPT_MISSING", "A required model role receipt is missing", { role, attempt });
      continue;
    }
    let receipt;
    try { receipt = JSON.parse(fs.readFileSync(receiptPath, "utf8")); } catch { fail("CODEX_LUNA_MODEL_CERT_USAGE_RECEIPT_INVALID", "Model role receipt is not valid JSON", { role, attempt }); }
    requireWrapper(
      ["PASS", "FAIL"].includes(receipt?.status)
        && receipt.provider === "openai-codex-app-server"
        && receipt.model === CODEX_LUNA_MODEL
        && receipt.reasoning_effort === CODEX_LUNA_REASONING_EFFORT
        && receipt.role === role
        && receipt.attempt === attempt
        && receipt.repair === (attempt === "REPAIR")
        && receipt.terminal === true,
      "CODEX_LUNA_MODEL_CERT_USAGE_RECEIPT_INVALID",
      "Model role receipt does not match its closed role identity",
      { role, attempt },
    );
    receipts.push(receipt);
  }
  const sequence = receipts.map((receipt) => `${receipt.role}:${receipt.attempt}`).join(",");
  const specialistSequences = [
    "SPECIALIST:PRIMARY",
    "SPECIALIST:PRIMARY,SPECIALIST:REPAIR",
  ];
  const blindSequences = [
    "SPECIALIST:PRIMARY,REVIEWER:PRIMARY",
    "SPECIALIST:PRIMARY,SPECIALIST:REPAIR,REVIEWER:PRIMARY",
    "SPECIALIST:PRIMARY,REVIEWER:PRIMARY,REVIEWER:REPAIR",
    "SPECIALIST:PRIMARY,SPECIALIST:REPAIR,REVIEWER:PRIMARY,REVIEWER:REPAIR",
  ];
  const legalPrefixes = new Set(
    blindConsensus
      ? [...specialistSequences, ...blindSequences]
      : specialistSequences,
  );
  if (allowFailurePrefix) {
    requireWrapper(
      receipts.length === 0
        || (legalPrefixes.has(sequence)
          && receipts.slice(0, -1).every((receipt) => receipt.status === "PASS")),
      "CODEX_LUNA_MODEL_CERT_CALL_SEQUENCE_INVALID",
      "Model role failure receipts are not one legal invocation prefix",
    );
    return receipts;
  }
  requireWrapper(
    new Set(blindConsensus ? blindSequences : specialistSequences).has(sequence),
    "CODEX_LUNA_MODEL_CERT_CALL_COUNT_INVALID",
    blindConsensus
      ? "Blind model cert must contain both required role calls and at most one repair per role"
      : "Specialist-only model cert must contain one required call and at most one repair",
  );
  requireWrapper(receipts.every((receipt) => receipt.status === "PASS"), "CODEX_LUNA_MODEL_CERT_USAGE_RECEIPT_INVALID", "Successful model certification requires only successful role calls");
  return receipts;
}

function exactDirectoryNames(root, expected, code, label) {
  let metadata;
  let names;
  try {
    metadata = fs.lstatSync(root);
    names = fs.readdirSync(root).sort();
  } catch {
    fail(code, `${label} is unavailable`);
  }
  requireWrapper(
    metadata.isDirectory()
      && !metadata.isSymbolicLink()
      && names.join("\0") === [...expected].sort().join("\0"),
    code,
    `${label} does not match its exact production allowlist`,
  );
  return names;
}

function validateModelWorkspaceSurface(root) {
  const inputsRoot = path.join(root, "inputs");
  exactDirectoryNames(
    inputsRoot,
    MODEL_INPUT_NAMES,
    "CODEX_LUNA_MODEL_CERT_INPUT_SURFACE_INVALID",
    "Evidence V2 role inputs root",
  );
  for (const name of MODEL_INPUT_NAMES) {
    const metadata = fs.lstatSync(path.join(inputsRoot, name));
    requireWrapper(
      metadata.isFile() && !metadata.isSymbolicLink(),
      "CODEX_LUNA_MODEL_CERT_INPUT_SURFACE_INVALID",
      "Evidence V2 role input must be an ordinary file",
      { field: `inputs/${name}` },
    );
  }
  const runtimeRoot = path.join(root, "runtime");
  exactDirectoryNames(
    runtimeRoot,
    MODEL_RUNTIME_NAMES,
    "CODEX_LUNA_MODEL_CERT_RUNTIME_SURFACE_INVALID",
    "Evidence V2 role runtime root",
  );
  const toolState = fs.lstatSync(path.join(runtimeRoot, "tool-state"));
  requireWrapper(
    toolState.isDirectory() && !toolState.isSymbolicLink(),
    "CODEX_LUNA_MODEL_CERT_RUNTIME_SURFACE_INVALID",
    "Evidence V2 role tool-state must be a plain directory",
  );
}

function readJsonFile(file, code, label) {
  try { return JSON.parse(fs.readFileSync(file, "utf8")); } catch {
    fail(code, `${label} is not valid JSON`);
  }
}

function validateRoleRequestAndManifest(root, parsed) {
  const request = readJsonFile(
    path.join(root, "inputs", "request.json"),
    "CODEX_LUNA_MODEL_CERT_REQUEST_INVALID",
    "Evidence V2 role request",
  );
  requireWrapper(
    hasExactKeys(request, ["schema_version", "job", "user_facts"])
      && request.schema_version === 2
      && hasExactKeys(request.job, ["job_id", "case_id", "job_type", "goal", "base_state_revision"])
      && UUID.test(request.job.job_id)
      && UUID.test(request.job.case_id)
      && typeof request.job.goal === "string"
      && request.job.goal.length > 0
      && positiveInteger(request.job.base_state_revision)
      && Array.isArray(request.user_facts),
    "CODEX_LUNA_MODEL_CERT_REQUEST_INVALID",
    "Evidence V2 role request has an invalid exact identity",
  );
  const manifest = readJsonFile(
    path.join(root, "inputs", "manifest.json"),
    "CODEX_LUNA_MODEL_CERT_MANIFEST_INVALID",
    "Evidence V2 role manifest",
  );
  const manifestKeys = [
    "schema_version",
    "job_id",
    "case_id",
    "job_type",
    "logparse_tool_ref",
    "logparse_product",
    "entries",
    "resolved_logparse_plan",
    "review_subject",
    ...(parsed.role === "REVIEWER" ? ["methods_reviewer_input"] : []),
  ];
  requireWrapper(
    hasExactKeys(manifest, manifestKeys)
      && manifest.schema_version === 2
      && manifest.job_id === request.job.job_id
      && manifest.case_id === request.job.case_id
      && manifest.job_type === request.job.job_type
      && Array.isArray(manifest.entries)
      && manifest.entries.length === 0
      && manifest.resolved_logparse_plan === null
      && manifest.review_subject === null,
    "CODEX_LUNA_MODEL_CERT_MANIFEST_INVALID",
    "Evidence V2 role manifest does not bind its minimal request",
  );
  if (parsed.role === "SPECIALIST") {
    requireWrapper(
      request.job.job_type === "DIAGNOSE"
        && parsed.rolePayload.job_id === request.job.job_id
        && parsed.rolePayload.case_id === request.job.case_id
        && manifest.methods_reviewer_input === undefined,
      "CODEX_LUNA_MODEL_CERT_ROLE_IDENTITY_INVALID",
      "Specialist prompt, request, and manifest identities differ",
    );
    return;
  }
  const reviewerInput = manifest.methods_reviewer_input;
  requireWrapper(
    request.job.job_type === "REVIEW"
      && hasExactKeys(reviewerInput, ["schema_version", "review_job_id", "case_id", "target", "method_ids"])
      && reviewerInput.schema_version === 2
      && reviewerInput.review_job_id === request.job.job_id
      && reviewerInput.case_id === request.job.case_id
      && canonicalJson(reviewerInput.target) === canonicalJson(parsed.rolePayload.target)
      && Array.isArray(reviewerInput.method_ids)
      && canonicalJson(reviewerInput.method_ids) === canonicalJson(
        parsed.rolePayload.evaluation_input.evaluations.map((item) => item.method_id),
      )
      && parsed.rolePayload.target.source_job_id !== request.job.job_id,
    "CODEX_LUNA_MODEL_CERT_ROLE_IDENTITY_INVALID",
    "Reviewer prompt, request, manifest, and target identities differ",
  );
}

function validateRoleWorkspace(workspaceRoot, parsed) {
  const root = path.resolve(workspaceRoot);
  for (const relative of LEGACY_ROLE_INPUTS) {
    requireWrapper(
      !fs.existsSync(path.join(root, ...relative.split("/"))),
      "CODEX_LUNA_MODEL_CERT_LEGACY_INPUT_EXPOSED",
      "Production role workspace exposes a legacy Graph/Plan model input",
      { field: relative },
    );
  }
  requireWrapper(
    !fs.existsSync(path.join(root, "runtime", "context.txt")),
    "CODEX_LUNA_MODEL_CERT_CONTEXT_FILE_EXPOSED",
    "Production role workspace exposes a duplicate runtime/context.txt",
  );
  validateModelWorkspaceSurface(root);
  validateRoleRequestAndManifest(root, parsed);
  const output = path.join(root, ...parsed.output.split("/"));
  if (parsed.attempt === "PRIMARY") {
    requireWrapper(!fs.existsSync(output), "CODEX_LUNA_MODEL_CERT_OUTPUT_EXISTS", "Evidence V2 primary role output already exists before the model invocation", { role: parsed.role, attempt: parsed.attempt });
  }
  if (parsed.role === "REVIEWER") {
    requireWrapper(
      !fs.existsSync(path.join(root, "inputs", "method-diagnosis.json")),
      "CODEX_LUNA_MODEL_CERT_REVIEW_NOT_BLIND",
      "Reviewer workspace exposes the Specialist response",
    );
  }
  return { root, output };
}

function stageModelRoleProject(workspaceRoot, parsed) {
  // Every host gets a model-only project. Production must omit the legacy
  // Graph/Plan inputs and context.txt; this verifies rather than repairs the boundary.
  const projectRoot = stageLinuxServiceProject(workspaceRoot, {
    runtimeEntries: ["tool-state"],
  });
  try {
    for (const relative of FORBIDDEN_ROLE_FILES) {
      requireWrapper(
        !fs.existsSync(path.join(projectRoot, ...relative.split("/"))),
        relative === "runtime/context.txt"
          ? "CODEX_LUNA_MODEL_CERT_CONTEXT_FILE_EXPOSED"
          : "CODEX_LUNA_MODEL_CERT_LEGACY_INPUT_EXPOSED",
        "Staged model project exposes a forbidden duplicate role input",
        { field: relative },
      );
    }
    validateModelWorkspaceSurface(projectRoot);
    validateRoleRequestAndManifest(projectRoot, parsed);
    return projectRoot;
  } catch (error) {
    removeLinuxServiceProject({ workspaceRoot, projectRoot });
    throw error;
  }
}

function auditRoleOutput(output, parsed) {
  let metadata;
  try { metadata = fs.lstatSync(output); } catch { fail("CODEX_LUNA_MODEL_CERT_OUTPUT_MISSING", "Model did not write the fixed Evidence V2 role output", { role: parsed.role, attempt: parsed.attempt }); }
  requireWrapper(
    metadata.isFile() && !metadata.isSymbolicLink() && metadata.size > 0 && metadata.size <= MAX_RESPONSE_BYTES,
    "CODEX_LUNA_MODEL_CERT_OUTPUT_INVALID",
    "Evidence V2 role output is not a bounded ordinary file",
    { role: parsed.role, attempt: parsed.attempt },
  );
  const raw = fs.readFileSync(output);
  let value;
  try { value = JSON.parse(raw.toString("utf8")); } catch { value = null; }
  return {
    relative_path: parsed.output,
    size: raw.length,
    sha256: sha256Bytes(raw),
    json_root: Array.isArray(value) ? "ARRAY" : "INVALID",
    item_count: Array.isArray(value) ? value.length : null,
    normalized: false,
  };
}

function publishLinuxRoleOutput({ workspaceRoot, projectRoot, parsed }) {
  const source = path.join(projectRoot, ...parsed.output.split("/"));
  const destination = path.join(workspaceRoot, ...parsed.output.split("/"));
  auditRoleOutput(source, parsed);
  if (parsed.attempt === "PRIMARY") {
    requireWrapper(
      !fs.existsSync(destination),
      "CODEX_LUNA_MODEL_CERT_OUTPUT_EXISTS",
      "Evidence V2 primary role output already exists before Linux project publication",
      { role: parsed.role, attempt: parsed.attempt },
    );
    fs.copyFileSync(source, destination, fs.constants.COPYFILE_EXCL);
  } else {
    if (fs.existsSync(destination)) {
      const metadata = fs.lstatSync(destination);
      requireWrapper(
        metadata.isFile() && !metadata.isSymbolicLink(),
        "CODEX_LUNA_MODEL_CERT_OUTPUT_INVALID",
        "Evidence V2 primary role output is not replaceable by its repair",
        { role: parsed.role, attempt: parsed.attempt },
      );
    }
    fs.copyFileSync(source, destination);
  }
  fs.chmodSync(destination, 0o600);
}

function clearLinuxProjectRoleOutput(projectRoot, parsed) {
  const output = path.join(projectRoot, ...parsed.output.split("/"));
  if (!fs.existsSync(output)) return;
  const metadata = fs.lstatSync(output);
  requireWrapper(
    metadata.isFile() && !metadata.isSymbolicLink(),
    "CODEX_LUNA_MODEL_CERT_OUTPUT_INVALID",
    "Linux role project contains an invalid inherited output",
    { role: parsed.role, attempt: parsed.attempt },
  );
  fs.rmSync(output);
}

function completedTraceProfile(trace) {
  if (
    !isPlainObject(trace?.app_server)
    || !isPlainObject(trace.app_server.permission_profile)
    || typeof trace.app_server.permission_profile.id !== "string"
    || typeof trace.app_server.codex_home?.config_sha256 !== "string"
    || typeof trace.app_server.developer_instructions?.sha256 !== "string"
  ) return null;
  return {
    permission_profile_id: trace.app_server.permission_profile.id,
    config_sha256: trace.app_server.codex_home.config_sha256,
    developer_instructions_sha256: trace.app_server.developer_instructions.sha256,
  };
}

function completedTraceToolPolicy(trace) {
  if (
    !isPlainObject(trace?.app_server)
    || !isPlainObject(trace.app_server.permission_profile)
    || typeof trace.app_server.permission_profile.invocation_mode !== "string"
    || !Number.isSafeInteger(trace.app_server.turn?.mcp_tool_call_count)
    || !Array.isArray(trace.command_receipts)
  ) return null;
  return {
    invocation_mode: trace.app_server.permission_profile.invocation_mode,
    mcp_tool_call_count: trace.app_server.turn.mcp_tool_call_count,
    command_count: trace.command_receipts.length,
    output_normalized: false,
  };
}

function controlledEnvironment(ambient) {
  const environment = {};
  for (const key of ["LANG", "LC_ALL", "LC_CTYPE", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS"]) {
    if (typeof ambient[key] === "string" && ambient[key].length > 0) environment[key] = ambient[key];
  }
  environment.PATH = "/usr/bin:/bin:/usr/sbin:/sbin";
  environment.LANG = "C.UTF-8";
  return environment;
}

export function modelRoleDeveloperInstructions(workspaceRoot, parsed) {
  const root = path.resolve(workspaceRoot);
  return `这是 Evidence V2 ${ROLE_CONFIG[parsed.role].label} 的生产评估调用。唯一工作目录是 ${JSON.stringify(root)}。读取 inputs/request.json；评估证据只能使用本次直接提供的 prompt 上下文中的紧凑 evaluation_input，并结合 prompt 中的方法卡，按 evaluations 原顺序评估全部 evaluation_ref。不得读取 inputs/method-evidence-graph.json、inputs/method-evaluation-plan.json 或 runtime/context.txt。只写 ${parsed.output}，根必须是 JSON 数组；每项只能包含 evaluation_ref、verdict、supporting_event_refs、reason。CONFIRMED 必须按当前 evaluation 的 event 原顺序选择非空 event ref 子集；REJECTED 或 UNKNOWN 必须使用空数组。不得生成 Evidence、Candidate、Artifact、grounding、PARTIAL 或权威 Outcome，也不得读取工作区外文件。`;
}

export async function runModelRoleInvocation(values, {
  stdin = process.stdin,
  stdout = process.stdout,
  ambient = process.env,
  runAppServerCall = runCodexLunaAppServerCall,
} = {}) {
  const prompt = await readPrompt(stdin);
  const parsed = parseEvidenceV2RolePrompt(prompt);
  const workspace = validateRoleWorkspace(process.cwd(), parsed);
  const claim = claimInvocation(values["private-root"], parsed.role, parsed.attempt);
  const auth = readCodexLunaExternalAuth(path.resolve(values["auth-source"]), ambient);
  const prefix = `${parsed.role.toLowerCase()}-${parsed.attempt.toLowerCase()}`;
  const traceRoot = path.join(path.resolve(values["evidence-root"]), "model-invocations");
  fs.mkdirSync(traceRoot, { recursive: true, mode: 0o700 });
  const startedAtUtc = new Date().toISOString();
  let trace = null;
  try {
    const projectRoot = stageModelRoleProject(workspace.root, parsed);
    try {
      clearLinuxProjectRoleOutput(projectRoot, parsed);
      trace = await runAppServerCall({
        codexEntry: path.resolve(values["codex-entry"]),
        auth,
        environment: controlledEnvironment(ambient),
        workspaceRoot: projectRoot,
        skillPath: path.resolve(values["skill-source"]),
        mode: "service",
        developerInstructions: modelRoleDeveloperInstructions(projectRoot, parsed),
        prompt,
        outputSchema: null,
        callRoot: path.join(claim, "app-server"),
        privateRoot: path.resolve(values["private-root"]),
        tracePath: path.join(traceRoot, `${prefix}.jsonl`),
        stderrPath: path.join(traceRoot, `${prefix}.stderr.txt`),
        finalPath: path.join(traceRoot, `${prefix}.final.txt`),
        forbiddenReadPaths: [path.resolve(values["auth-source"]), path.resolve(values["skill-source"])],
        wallSeconds: MACOS_CODEX_LUNA_CALL_WALL_SECONDS,
        noProgressSeconds: MACOS_CODEX_LUNA_NO_PROGRESS_SECONDS,
        shellHome: path.join(claim, "shell-home"),
        expectedCliVersion: values["expected-cli-version"],
        onProgress: () => stdout.write(`TEST_FLOW_PROGRESS model-cert ${parsed.role.toLowerCase()} ${parsed.attempt.toLowerCase()}\n`),
      });
      publishLinuxRoleOutput({
        workspaceRoot: workspace.root,
        projectRoot,
        parsed,
      });
    } finally {
      removeLinuxServiceProject({
        workspaceRoot: workspace.root,
        projectRoot,
      });
    }
    const output = auditRoleOutput(workspace.output, parsed);
    const profile = completedTraceProfile(trace);
    const toolPolicy = completedTraceToolPolicy(trace);
    const receipt = {
      schema_version: 1,
      wrapper_version: CODEX_LUNA_MODEL_CERT_WRAPPER_VERSION,
      invocation_id: `${values["run-id"]}:codex-luna:${prefix}`,
      provider: "openai-codex-app-server",
      model: CODEX_LUNA_MODEL,
      model_revision: CODEX_LUNA_MODEL,
      reasoning_effort: CODEX_LUNA_REASONING_EFFORT,
      workflow: `${parsed.role}:${parsed.attempt}`,
      role: parsed.role,
      attempt: parsed.attempt,
      repair: parsed.attempt === "REPAIR",
      status: "PASS",
      terminal: true,
      started_at_utc: startedAtUtc,
      finished_at_utc: new Date().toISOString(),
      prompt: {
        sha256: sha256Bytes(prompt),
        size: Buffer.byteLength(prompt, "utf8"),
        production_role_marker: true,
      },
      profile,
      tool_policy: toolPolicy,
      output,
      usage_complete: true,
      usage: trace.usage,
      failure_code: null,
      thread_id: trace.thread_id,
      turn_id: trace.turn_id,
    };
    requireWrapper(
      isPlainObject(receipt.usage)
        && isPlainObject(receipt.profile)
        && isPlainObject(receipt.tool_policy)
        && receipt.tool_policy.mcp_tool_call_count === 0
        && typeof receipt.thread_id === "string"
        && receipt.thread_id.length > 0
        && typeof receipt.turn_id === "string"
        && receipt.turn_id.length > 0,
      "CODEX_LUNA_MODEL_CERT_TRACE_INVALID",
      "Codex app-server did not return one closed Evidence V2 role receipt",
    );
    writeJsonExclusive(path.join(path.resolve(values["usage-root"]), `${prefix}.json`), receipt);
    writeJsonExclusive(path.join(traceRoot, `${prefix}.receipt.json`), receipt);
    stdout.write(`${canonicalJson({ status: "PASS", role: parsed.role, attempt: parsed.attempt })}\n`);
    return receipt;
  } catch (error) {
    const usage = isPlainObject(trace?.usage)
      ? trace.usage
      : (isPlainObject(error?.details?.usage) ? error.details.usage : null);
    const profile = completedTraceProfile(trace);
    const toolPolicy = completedTraceToolPolicy(trace);
    const receipt = {
      schema_version: 1,
      wrapper_version: CODEX_LUNA_MODEL_CERT_WRAPPER_VERSION,
      invocation_id: `${values["run-id"]}:codex-luna:${prefix}`,
      provider: "openai-codex-app-server",
      model: CODEX_LUNA_MODEL,
      model_revision: CODEX_LUNA_MODEL,
      reasoning_effort: CODEX_LUNA_REASONING_EFFORT,
      workflow: `${parsed.role}:${parsed.attempt}`,
      role: parsed.role,
      attempt: parsed.attempt,
      repair: parsed.attempt === "REPAIR",
      status: "FAIL",
      terminal: true,
      started_at_utc: startedAtUtc,
      finished_at_utc: new Date().toISOString(),
      prompt: {
        sha256: sha256Bytes(prompt),
        size: Buffer.byteLength(prompt, "utf8"),
        production_role_marker: true,
      },
      profile,
      tool_policy: toolPolicy,
      output: null,
      usage_complete: usage !== null,
      usage,
      failure_code: typeof error?.code === "string" ? error.code : "CODEX_LUNA_MODEL_CERT_CALL_FAILED",
      thread_id: trace?.thread_id ?? error?.details?.thread_id ?? null,
      turn_id: trace?.turn_id ?? error?.details?.turn_id ?? null,
    };
    writeJsonExclusive(path.join(path.resolve(values["usage-root"]), `${prefix}.json`), receipt);
    writeJsonExclusive(path.join(traceRoot, `${prefix}.receipt.json`), receipt);
    throw error;
  }
}

function safeError(error) {
  return {
    schema_version: 1,
    status: "FAIL",
    code: typeof error?.code === "string" ? error.code : "CODEX_LUNA_MODEL_CERT_WRAPPER_FAILED",
    message: typeof error?.message === "string" ? error.message : String(error),
  };
}

async function main() {
  try {
    const values = parseModelCertWrapperArguments(process.argv.slice(2));
    await runModelRoleInvocation(values);
  } catch (error) {
    process.stderr.write(`${canonicalJson(safeError(error))}\n`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === MODULE_PATH) await main();
