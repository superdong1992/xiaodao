import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";

import {
  canonicalJson,
  CODEX_LUNA_MODEL,
  CODEX_LUNA_REASONING_EFFORT,
  sha256Bytes,
  sha256File,
  treeDigest,
  treeManifest,
} from "../../../runtime-support/codex-luna-contract.mjs";

export const MACOS_CODEX_LUNA_E2E_CONTRACT_VERSION = 1;
export const MACOS_CODEX_LUNA_METHODS_PROMPT_VERSION = 1;
export const MACOS_CODEX_LUNA_CLIENT_PROMPT_VERSION = 2;
export const MACOS_CODEX_LUNA_SCENARIOS = Object.freeze(["multiple-rpc-timeouts"]);
export const STANDALONE_CODEX_LUNA_SCENARIOS = MACOS_CODEX_LUNA_SCENARIOS;
export const MACOS_CODEX_LUNA_METHODS_CALLS = 1;
export const MACOS_CODEX_LUNA_E2E_CALLS = 1;
export const MACOS_CODEX_LUNA_E2E_MAX_CALLS = 2;
export const MACOS_CODEX_LUNA_BLIND_REVIEW_E2E_CALLS = 2;
export const MACOS_CODEX_LUNA_BLIND_REVIEW_E2E_MAX_CALLS = 4;
export const MACOS_CODEX_LUNA_METHODS_TOKEN_LIMIT = 1_000_000;
export const MACOS_CODEX_LUNA_METHODS_USD_LIMIT = 2;
export const MACOS_CODEX_LUNA_E2E_TOKEN_LIMIT = 2_000_000;
export const MACOS_CODEX_LUNA_E2E_USD_LIMIT = 3;
export const MACOS_CODEX_LUNA_CALL_WALL_SECONDS = 600;
export const MACOS_CODEX_LUNA_NO_PROGRESS_SECONDS = 180;
export const MACOS_CODEX_LUNA_STAGE_WALL_SECONDS = 2_700;
export const MACOS_CODEX_LUNA_REGISTRATION_ID = "rpc-timeout-methods-v1";
export const MACOS_CODEX_LUNA_SKILL_NAME = "diagnose-rpc-timeout";
export const MACOS_CODEX_LUNA_PUBLIC_TOOLS = Object.freeze([
  "problem_locator_create_case",
  "problem_locator_prepare_attachment",
  "problem_locator_submit_supplement",
  "problem_locator_get_case",
  "problem_locator_resume_case",
  "problem_locator_cancel_case",
  "problem_locator_list_artifacts",
]);
export const MACOS_CODEX_LUNA_SUCCESS_INVOCATIONS = Object.freeze([
  "SPECIALIST",
]);
export const MACOS_CODEX_LUNA_BLIND_REVIEW_SUCCESS_INVOCATIONS = Object.freeze([
  "SPECIALIST",
  "REVIEWER",
]);
export const MACOS_CODEX_LUNA_PRICE_SNAPSHOT = Object.freeze({
  schema_version: 1,
  model: CODEX_LUNA_MODEL,
  captured_on: "2026-08-24",
  source: "https://developers.openai.com/api/docs/models/gpt-5.6-luna",
  currency: "USD",
  unit: "million_tokens",
  rates: Object.freeze({ input: 0.20, cached_input: 0.02, output: 1.20 }),
});

const CASE_FACT_KEYS = Object.freeze([
  "scenario_id",
  "problem_time",
  "client_slot",
  "client_process",
  "server_slot",
  "server_process",
  "service",
  "api",
]);
const CASE_ORACLE_KEYS = Object.freeze([
  "expected_status",
  "expected_branch_markers",
  "expected_terms",
  "expected_evidence_identities",
  "forbidden_evidence_terms",
]);
const SCALAR_SCHEMA_TYPES = new Set(["string", "integer", "number", "boolean"]);
const WRITE_TOOLS = new Set([
  "problem_locator_create_case",
  "problem_locator_prepare_attachment",
  "problem_locator_submit_supplement",
  "problem_locator_resume_case",
  "problem_locator_cancel_case",
]);

export class MacosCodexLunaE2EError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "MacosCodexLunaE2EError";
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) {
  throw new MacosCodexLunaE2EError(code, message, details);
}

function requireE2E(condition, code, message, details = {}) {
  if (!condition) fail(code, message, details);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function isNonEmptyString(value) {
  return typeof value === "string" && value.length > 0;
}

function exactKeys(value, keys) {
  return isPlainObject(value)
    && Object.keys(value).sort().join("\0") === [...keys].sort().join("\0");
}

function readJson(filePath, label) {
  let value;
  try {
    value = JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (error) {
    fail("MACOS_CODEX_LUNA_JSON_INVALID", `${label} must be valid JSON`, { cause: error.message });
  }
  requireE2E(isPlainObject(value), "MACOS_CODEX_LUNA_JSON_ROOT_INVALID", `${label} must have an object root`);
  return value;
}

function ordinaryFile(filePath, label) {
  let metadata;
  try {
    metadata = fs.lstatSync(filePath);
  } catch (error) {
    fail("MACOS_CODEX_LUNA_FILE_MISSING", `${label} is unavailable`, { cause: error.code ?? null });
  }
  requireE2E(metadata.isFile() && !metadata.isSymbolicLink(), "MACOS_CODEX_LUNA_FILE_INVALID", `${label} must be an ordinary file`);
  return metadata;
}

function normalizedScenarioId(value) {
  requireE2E(STANDALONE_CODEX_LUNA_SCENARIOS.includes(value), "MACOS_CODEX_LUNA_SCENARIO_UNSUPPORTED", "Scenario is not in the repository-owned standalone matrix", { scenario_id: value });
  return value;
}

export function macosCodexLunaE2EPhases(
  scenarioId,
  evaluationMode = "SPECIALIST_ONLY",
) {
  normalizedScenarioId(scenarioId);
  requireE2E(
    ["SPECIALIST_ONLY", "BLIND_CONSENSUS"].includes(evaluationMode),
    "MACOS_CODEX_LUNA_EVALUATION_MODE_INVALID",
    "Evidence V2 evaluation mode is invalid",
    { evaluation_mode: evaluationMode },
  );
  return evaluationMode === "BLIND_CONSENSUS"
    ? MACOS_CODEX_LUNA_BLIND_REVIEW_SUCCESS_INVOCATIONS
    : MACOS_CODEX_LUNA_SUCCESS_INVOCATIONS;
}

export function macosCodexLunaE2ECallCount(
  scenarioId,
  evaluationMode = "SPECIALIST_ONLY",
) {
  return macosCodexLunaE2EPhases(scenarioId, evaluationMode).length;
}

export function scenarioPaths(sourceRoot, scenarioId) {
  const id = normalizedScenarioId(scenarioId);
  const scenarioRoot = path.join(path.resolve(sourceRoot), "tests", "cases", "release", "rpc-timeout-anonymized", "scenarios", id);
  const result = {
    root: scenarioRoot,
    case: path.join(scenarioRoot, "driver.json"),
    client_log: path.join(scenarioRoot, "client.log"),
    server_log: path.join(scenarioRoot, "server.log"),
  };
  ordinaryFile(result.case, "scenario driver.json");
  ordinaryFile(result.client_log, "scenario client.log");
  ordinaryFile(result.server_log, "scenario server.log");
  return Object.freeze(result);
}

export function loadScenarioFacts(casePath, expectedScenarioId = null) {
  ordinaryFile(casePath, "scenario case.json");
  const value = readJson(casePath, "scenario case.json");
  const allowed = new Set([...CASE_FACT_KEYS, ...CASE_ORACLE_KEYS]);
  requireE2E(Object.keys(value).every((key) => allowed.has(key)), "MACOS_CODEX_LUNA_CASE_FIELDS_INVALID", "Scenario case.json contains an unsupported field");
  requireE2E(CASE_FACT_KEYS.every((key) => isNonEmptyString(value[key])), "MACOS_CODEX_LUNA_CASE_FACT_INVALID", "Scenario facts are incomplete");
  if (expectedScenarioId !== null) requireE2E(value.scenario_id === expectedScenarioId, "MACOS_CODEX_LUNA_CASE_ID_MISMATCH", "Scenario directory and case.json differ");
  return Object.freeze(Object.fromEntries(CASE_FACT_KEYS.map((key) => [key, value[key]])));
}

export function loadScenarioOracle(casePath, expectedScenarioId = null) {
  ordinaryFile(casePath, "scenario case.json");
  const value = readJson(casePath, "scenario case.json");
  if (expectedScenarioId !== null) requireE2E(value.scenario_id === expectedScenarioId, "MACOS_CODEX_LUNA_CASE_ID_MISMATCH", "Scenario directory and case.json differ");
  requireE2E(["CONFIRMED", "PARTIAL", "INSUFFICIENT"].includes(value.expected_status), "MACOS_CODEX_LUNA_ORACLE_STATUS_INVALID", "Scenario oracle status is invalid");
  for (const key of ["expected_branch_markers", "expected_terms", "forbidden_evidence_terms"]) {
    requireE2E(Array.isArray(value[key]) && value[key].every(isNonEmptyString), "MACOS_CODEX_LUNA_ORACLE_FIELD_INVALID", `Scenario oracle ${key} is invalid`);
  }
  requireE2E(
    Array.isArray(value.expected_evidence_identities)
      && value.expected_evidence_identities.every((identity) => (
        exactKeys(identity, ["branch_marker", "identity_tokens"])
        && isNonEmptyString(identity.branch_marker)
        && Array.isArray(identity.identity_tokens)
        && identity.identity_tokens.length > 0
        && identity.identity_tokens.every(isNonEmptyString)
      )),
    "MACOS_CODEX_LUNA_ORACLE_FIELD_INVALID",
    "Scenario oracle expected_evidence_identities is invalid",
  );
  return Object.freeze({
    schema_version: 1,
    scenario_id: value.scenario_id,
    source_sha256: sha256File(casePath),
    ...Object.fromEntries(CASE_ORACLE_KEYS.map((key) => [key, value[key]])),
  });
}

export function mapScenarioToCreateCase(facts) {
  requireE2E(exactKeys(facts, CASE_FACT_KEYS), "MACOS_CODEX_LUNA_MAPPER_INPUT_INVALID", "Scenario mapper accepts facts only; oracle fields are forbidden");
  for (const key of CASE_FACT_KEYS) requireE2E(isNonEmptyString(facts[key]), "MACOS_CODEX_LUNA_MAPPER_INPUT_INVALID", `Scenario fact ${key} is invalid`);
  requireE2E(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(facts.problem_time), "MACOS_CODEX_LUNA_MAPPER_INPUT_INVALID", "Scenario problem_time must be an ISO-8601 timestamp with a timezone");
  const problemTimeMs = Date.parse(facts.problem_time);
  requireE2E(Number.isFinite(problemTimeMs), "MACOS_CODEX_LUNA_MAPPER_INPUT_INVALID", "Scenario problem_time is not a real timestamp");
  const problemTime = new Date(problemTimeMs).toISOString();
  const subject = `${facts.service}.${facts.api}`;
  return Object.freeze({
    raw_problem_text: `问题时间 ${problemTime}；客户端 slot ${facts.client_slot}、进程 ${facts.client_process}；服务端 slot ${facts.server_slot}、进程 ${facts.server_process}；服务 ${facts.service}；API ${facts.api}；客户端观察到 RPC timeout。`,
    statement: `${subject} 在 ${problemTime} 附近发生 RPC timeout`,
    expected_behavior: "RPC 请求在超时预算内完成并被客户端正常接收",
    actual_behavior: "客户端观察到 RPC timeout",
    scope: "仅定位给定 client/server 日志中的超时原因",
    goals: ["定位根因并给出原始日志证据"],
    non_goals: ["不修改服务，不执行恢复动作"],
    constraints: ["只使用给定事实和日志，不补造证据"],
    completion_criteria: ["给出状态、根因或证据缺口，并绑定可核验日志证据"],
    initial_user_fact_names: ["problem_time", "client_slot", "client_process", "server_slot", "server_process", "service", "api"],
    initial_user_fact_values: [problemTime, facts.client_slot, facts.client_process, facts.server_slot, facts.server_process, facts.service, facts.api],
  });
}

let crcTable = null;
function crc32(bytes) {
  if (crcTable === null) {
    crcTable = Array.from({ length: 256 }, (_, index) => {
      let value = index;
      for (let bit = 0; bit < 8; bit += 1) value = (value & 1) ? (0xedb88320 ^ (value >>> 1)) : (value >>> 1);
      return value >>> 0;
    });
  }
  let value = 0xffffffff;
  for (const byte of bytes) value = crcTable[(value ^ byte) & 0xff] ^ (value >>> 8);
  return (value ^ 0xffffffff) >>> 0;
}

function zipMember(name, bytes, offset) {
  const nameBytes = Buffer.from(name, "utf8");
  const compressed = zlib.deflateRawSync(bytes, { level: 9 });
  const crc = crc32(bytes);
  const local = Buffer.alloc(30);
  local.writeUInt32LE(0x04034b50, 0);
  local.writeUInt16LE(20, 4);
  local.writeUInt16LE(0x0800, 6);
  local.writeUInt16LE(8, 8);
  local.writeUInt16LE(0, 10);
  local.writeUInt16LE(0x0021, 12);
  local.writeUInt32LE(crc, 14);
  local.writeUInt32LE(compressed.length, 18);
  local.writeUInt32LE(bytes.length, 22);
  local.writeUInt16LE(nameBytes.length, 26);
  local.writeUInt16LE(0, 28);
  const central = Buffer.alloc(46);
  central.writeUInt32LE(0x02014b50, 0);
  central.writeUInt16LE(0x0314, 4);
  central.writeUInt16LE(20, 6);
  central.writeUInt16LE(0x0800, 8);
  central.writeUInt16LE(8, 10);
  central.writeUInt16LE(0, 12);
  central.writeUInt16LE(0x0021, 14);
  central.writeUInt32LE(crc, 16);
  central.writeUInt32LE(compressed.length, 20);
  central.writeUInt32LE(bytes.length, 24);
  central.writeUInt16LE(nameBytes.length, 28);
  central.writeUInt16LE(0, 30);
  central.writeUInt16LE(0, 32);
  central.writeUInt16LE(0, 34);
  central.writeUInt16LE(0, 36);
  central.writeUInt32LE((0o100644 << 16) >>> 0, 38);
  central.writeUInt32LE(offset, 42);
  return {
    local: Buffer.concat([local, nameBytes, compressed]),
    central: Buffer.concat([central, nameBytes]),
    receipt: { name, size: bytes.length, sha256: sha256Bytes(bytes), crc32: crc.toString(16).padStart(8, "0") },
  };
}

export function buildDeterministicLogsZip({ clientLog, serverLog }) {
  ordinaryFile(clientLog, "client.log");
  ordinaryFile(serverLog, "server.log");
  const inputs = [
    ["client.log", fs.readFileSync(clientLog)],
    ["server.log", fs.readFileSync(serverLog)],
  ];
  const members = [];
  let offset = 0;
  for (const [name, bytes] of inputs) {
    const member = zipMember(name, bytes, offset);
    members.push(member);
    offset += member.local.length;
  }
  const centralOffset = offset;
  const central = Buffer.concat(members.map((member) => member.central));
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(0, 4);
  end.writeUInt16LE(0, 6);
  end.writeUInt16LE(members.length, 8);
  end.writeUInt16LE(members.length, 10);
  end.writeUInt32LE(central.length, 12);
  end.writeUInt32LE(centralOffset, 16);
  end.writeUInt16LE(0, 20);
  const bytes = Buffer.concat([...members.map((member) => member.local), central, end]);
  return Object.freeze({
    bytes,
    receipt: Object.freeze({
      schema_version: 1,
      name: "logs.zip",
      content_type: "application/zip",
      size: bytes.length,
      sha256: sha256Bytes(bytes),
      compression: "deflate-raw-level-9",
      timestamp: "1980-01-01T00:00:00Z",
      mode: "0644",
      newline_policy: "preserve-source-bytes",
      members: members.map((member) => member.receipt),
    }),
  });
}

export function writeDeterministicLogsZip({ clientLog, serverLog, destination }) {
  requireE2E(path.isAbsolute(destination), "MACOS_CODEX_LUNA_ZIP_PATH_INVALID", "ZIP destination must be absolute");
  requireE2E(!fs.existsSync(destination), "MACOS_CODEX_LUNA_ZIP_EXISTS", "ZIP destination already exists");
  const archive = buildDeterministicLogsZip({ clientLog, serverLog });
  fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 });
  fs.writeFileSync(destination, archive.bytes, { mode: 0o600, flag: "wx" });
  return archive.receipt;
}

function schemaTypeIsScalar(schema) {
  if (!isPlainObject(schema)) return false;
  if (SCALAR_SCHEMA_TYPES.has(schema.type)) return true;
  const typeArray = Array.isArray(schema.type)
    && schema.type.includes("null")
    && schema.type.filter((item) => item !== "null").length === 1
    && SCALAR_SCHEMA_TYPES.has(schema.type.find((item) => item !== "null"));
  if (typeArray) return true;
  return Array.isArray(schema.anyOf)
    && schema.anyOf.length === 2
    && schema.anyOf.some((item) => isPlainObject(item) && item.type === "null")
    && schema.anyOf.some((item) => isPlainObject(item) && SCALAR_SCHEMA_TYPES.has(item.type));
}

export function auditFlatMcpInputSchema(schema) {
  const forbidden = [];
  const visit = (value, pointer) => {
    if (Array.isArray(value)) value.forEach((item, index) => visit(item, `${pointer}/${index}`));
    else if (isPlainObject(value)) {
      for (const [key, item] of Object.entries(value)) {
        if (["$ref", "$defs", "definitions", "patternProperties", "additionalProperties"].includes(key) && item !== false) forbidden.push(`${pointer}/${key}`);
        visit(item, `${pointer}/${key}`);
      }
    }
  };
  visit(schema, "");
  requireE2E(isPlainObject(schema) && schema.type === "object" && isPlainObject(schema.properties), "MACOS_CODEX_LUNA_MCP_SCHEMA_ROOT_INVALID", "MCP input schema must have an object root");
  requireE2E(forbidden.length === 0, "MACOS_CODEX_LUNA_MCP_SCHEMA_FORBIDDEN", "MCP input schema contains nested/dynamic constructs", { pointers: forbidden });
  for (const [name, property] of Object.entries(schema.properties)) {
    const validArray = isPlainObject(property)
      && property.type === "array"
      && schemaTypeIsScalar(property.items);
    requireE2E(schemaTypeIsScalar(property) || validArray, "MACOS_CODEX_LUNA_MCP_SCHEMA_NOT_FLAT", "MCP root property is not a scalar, nullable scalar, or scalar array", { property: name });
  }
  return { schema_version: 1, status: "PASS", property_count: Object.keys(schema.properties).length };
}

export function auditListedMcpTools(listedTools) {
  requireE2E(Array.isArray(listedTools), "MACOS_CODEX_LUNA_MCP_TOOLS_INVALID", "MCP tool listing must be an array");
  const names = listedTools.map((tool) => tool?.name);
  requireE2E(names.length === MACOS_CODEX_LUNA_PUBLIC_TOOLS.length && new Set(names).size === names.length && [...names].sort().join("\0") === [...MACOS_CODEX_LUNA_PUBLIC_TOOLS].sort().join("\0"), "MACOS_CODEX_LUNA_MCP_TOOL_SET_INVALID", "MCP must expose exactly the seven public tools", { names });
  const schemas = listedTools.map((tool) => ({ name: tool.name, ...auditFlatMcpInputSchema(tool.inputSchema ?? tool.input_schema) }));
  return { schema_version: 1, status: "PASS", names, schemas };
}

function flatArguments(value) {
  if (!isPlainObject(value)) return false;
  return Object.values(value).every((item) => (
    item === null
    || ["string", "number", "boolean"].includes(typeof item)
    || (Array.isArray(item) && item.every((entry) => entry === null || ["string", "number", "boolean"].includes(typeof entry)))
  ));
}

export function auditMcpToolCalls(calls, { attachmentId = null, uploadRevision = null } = {}) {
  requireE2E(Array.isArray(calls) && calls.length >= 6, "MACOS_CODEX_LUNA_MCP_CALLS_INVALID", "MCP call ledger is incomplete");
  const names = calls.map((call) => call.tool);
  requireE2E(calls.every((call) => MACOS_CODEX_LUNA_PUBLIC_TOOLS.includes(call.tool) && call.server === "problem-locator" && call.status === "completed" && call.error == null && flatArguments(call.arguments)), "MACOS_CODEX_LUNA_MCP_CALL_INVALID", "MCP calls must be completed, flat, and confined to the public server/tool allowlist");
  requireE2E(names.filter((name) => name === "problem_locator_create_case").length === 1 && names[0] === "problem_locator_create_case", "MACOS_CODEX_LUNA_CREATE_CASE_CARDINALITY_INVALID", "Client must create exactly one Case first");
  requireE2E(names.filter((name) => name === "problem_locator_prepare_attachment").length === 1, "MACOS_CODEX_LUNA_PREPARE_CARDINALITY_INVALID", "Client must prepare exactly one attachment");
  const prepareIndex = names.indexOf("problem_locator_prepare_attachment");
  const submitIndex = names.indexOf("problem_locator_submit_supplement");
  const artifactIndex = names.lastIndexOf("problem_locator_list_artifacts");
  requireE2E(prepareIndex > names.indexOf("problem_locator_get_case") && submitIndex > prepareIndex && artifactIndex > submitIndex, "MACOS_CODEX_LUNA_MCP_ORDER_INVALID", "MCP call order violates the attachment workflow");
  const requestIds = calls.filter((call) => WRITE_TOOLS.has(call.tool)).map((call) => call.arguments.request_id);
  requireE2E(requestIds.every(isNonEmptyString) && new Set(requestIds).size === requestIds.length, "MACOS_CODEX_LUNA_REQUEST_ID_INVALID", "Each logical write must use a distinct stable request ID");
  const revisions = calls.filter((call) => WRITE_TOOLS.has(call.tool) && call.tool !== "problem_locator_create_case").map((call) => call.arguments.expected_case_revision);
  requireE2E(revisions.every((value) => Number.isSafeInteger(value) && value > 0) && revisions.every((value, index) => index === 0 || value >= revisions[index - 1]), "MACOS_CODEX_LUNA_REVISION_INVALID", "Write revisions must be positive and monotonic");
  if (attachmentId !== null) {
    const submit = calls[submitIndex];
    requireE2E(Array.isArray(submit.arguments.attachment_ids) && submit.arguments.attachment_ids.includes(attachmentId), "MACOS_CODEX_LUNA_ATTACHMENT_NOT_SUBMITTED", "READY attachment was not submitted after upload");
  }
  if (uploadRevision !== null) requireE2E(calls[submitIndex].arguments.expected_case_revision === uploadRevision, "MACOS_CODEX_LUNA_UPLOAD_REVISION_STALE", "Supplement did not use the upload receipt revision");
  return { schema_version: 1, status: "PASS", call_count: calls.length, sequence: names, write_request_ids: requestIds, revisions };
}

export function auditHttpBoundary(entries, { mcpUrl, uploadUrl, downloadUrl = null }) {
  requireE2E(Array.isArray(entries) && isNonEmptyString(mcpUrl) && isNonEmptyString(uploadUrl), "MACOS_CODEX_LUNA_HTTP_AUDIT_INPUT_INVALID", "HTTP boundary audit inputs are invalid");
  const mcp = new URL(mcpUrl);
  const upload = new URL(uploadUrl);
  const download = downloadUrl === null ? null : new URL(downloadUrl);
  const normalized = entries.map((entry) => {
    requireE2E(isPlainObject(entry) && isNonEmptyString(entry.method) && isNonEmptyString(entry.url) && isNonEmptyString(entry.source), "MACOS_CODEX_LUNA_HTTP_ENTRY_INVALID", "HTTP audit entry is invalid");
    const target = new URL(entry.url);
    const method = entry.method.toUpperCase();
    const isMcp = target.href === mcp.href && ["GET", "POST", "DELETE"].includes(method);
    const isUpload = target.href === upload.href && method === "PUT";
    const isDownload = download !== null && target.href === download.href && method === "GET";
    requireE2E(isMcp || isUpload || isDownload, "MACOS_CODEX_LUNA_HTTP_BOUNDARY_VIOLATION", "Observed HTTP call is outside /mcp transport, the UploadDescriptor PUT, and the selected Artifact GET", { method, url: target.href, source: entry.source });
    return { method, url: target.href, source: entry.source, category: isMcp ? "MCP_TRANSPORT" : isUpload ? "ATTACHMENT_PUT" : "ARTIFACT_GET" };
  });
  requireE2E(normalized.filter((entry) => entry.category === "ATTACHMENT_PUT").length === 1, "MACOS_CODEX_LUNA_UPLOAD_HTTP_CARDINALITY_INVALID", "UploadDescriptor must be used for exactly one PUT");
  requireE2E(download === null || normalized.filter((entry) => entry.category === "ARTIFACT_GET").length === 1, "MACOS_CODEX_LUNA_DOWNLOAD_HTTP_CARDINALITY_INVALID", "Selected Artifact must be downloaded exactly once");
  return { schema_version: 1, status: "PASS", entries: normalized };
}

export function auditUploadedAttachment({ attachment, uploadReceipt, descriptor, archive, submitArguments }) {
  requireE2E(
    isPlainObject(attachment)
      && isPlainObject(uploadReceipt)
      && isPlainObject(descriptor)
      && isPlainObject(archive)
      && isPlainObject(submitArguments),
    "MACOS_CODEX_LUNA_ATTACHMENT_AUDIT_INPUT_INVALID",
    "Attachment audit inputs are incomplete",
  );
  const expectedHeaders = {
    "Content-Length": String(archive.size),
    "Content-Type": "application/zip",
    "Idempotency-Key": descriptor.attachment_id,
    "X-Content-SHA256": archive.sha256,
  };
  requireE2E(
    descriptor.method === "PUT"
      && exactKeys(descriptor.required_headers, Object.keys(expectedHeaders))
      && Object.entries(expectedHeaders).every(([name, value]) => descriptor.required_headers[name] === value),
    "MACOS_CODEX_LUNA_UPLOAD_DESCRIPTOR_INVALID",
    "UploadDescriptor does not bind the deterministic ZIP with exactly four headers",
  );
  requireE2E(
    attachment.attachment_id === descriptor.attachment_id
      && attachment.status === "READY"
      && attachment.name === "logs.zip"
      && attachment.content_type === "application/zip"
      && attachment.declared_size === archive.size
      && attachment.size === archive.size
      && attachment.declared_sha256 === archive.sha256
      && attachment.sha256 === archive.sha256,
    "MACOS_CODEX_LUNA_ATTACHMENT_BYTES_MISMATCH",
    "READY attachment does not match the declared and uploaded deterministic ZIP bytes",
  );
  requireE2E(
    uploadReceipt.operation === "UploadAttachmentContent"
      && uploadReceipt.primary_resource_id === descriptor.attachment_id
      && uploadReceipt.status === "READY"
      && Number.isSafeInteger(uploadReceipt.case_revision)
      && uploadReceipt.case_revision > 0,
    "MACOS_CODEX_LUNA_UPLOAD_RECEIPT_INVALID",
    "Server upload receipt is missing or does not identify the READY attachment",
  );
  requireE2E(
    submitArguments.expected_case_revision === uploadReceipt.case_revision
      && Array.isArray(submitArguments.attachment_ids)
      && submitArguments.attachment_ids.length === 1
      && submitArguments.attachment_ids[0] === descriptor.attachment_id,
    "MACOS_CODEX_LUNA_UPLOAD_REVISION_STALE",
    "Supplement did not use the upload receipt revision and exact READY attachment",
  );
  return {
    schema_version: 1,
    status: "PASS",
    attachment_id: descriptor.attachment_id,
    case_revision: uploadReceipt.case_revision,
    size: archive.size,
    sha256: archive.sha256,
  };
}

export function aggregateCodexUsage(invocations) {
  requireE2E(Array.isArray(invocations), "MACOS_CODEX_LUNA_USAGE_INVALID", "Invocation usage must be an array");
  const aggregate = { input_tokens: 0, cached_input_tokens: 0, output_tokens: 0, total_tokens: 0, equivalent_usd: 0 };
  for (const invocation of invocations) {
    const usage = invocation.usage;
    requireE2E(isPlainObject(usage) && [usage.input_tokens, usage.cached_input_tokens, usage.output_tokens].every((value) => Number.isSafeInteger(value) && value >= 0) && usage.cached_input_tokens <= usage.input_tokens, "MACOS_CODEX_LUNA_TERMINAL_USAGE_INVALID", "Every invocation needs complete non-negative terminal usage");
    aggregate.input_tokens += usage.input_tokens;
    aggregate.cached_input_tokens += usage.cached_input_tokens;
    aggregate.output_tokens += usage.output_tokens;
  }
  aggregate.total_tokens = aggregate.input_tokens + aggregate.output_tokens;
  aggregate.equivalent_usd = Math.ceil(((aggregate.input_tokens - aggregate.cached_input_tokens) * 0.20 / 1_000_000 + aggregate.cached_input_tokens * 0.02 / 1_000_000 + aggregate.output_tokens * 1.20 / 1_000_000) * 1_000_000) / 1_000_000;
  return aggregate;
}

export function auditModelInvocations(invocations, {
  workflow,
  scenarioId = null,
  evaluationMode = "SPECIALIST_ONLY",
}) {
  const expectedPhases = workflow === "methods"
    ? ["METHODS_BOOTSTRAP"]
    : macosCodexLunaE2EPhases(scenarioId, evaluationMode);
  const tokenLimit = workflow === "methods" ? MACOS_CODEX_LUNA_METHODS_TOKEN_LIMIT : MACOS_CODEX_LUNA_E2E_TOKEN_LIMIT;
  const costLimit = workflow === "methods" ? MACOS_CODEX_LUNA_METHODS_USD_LIMIT : MACOS_CODEX_LUNA_E2E_USD_LIMIT;
  requireE2E(Array.isArray(invocations) && invocations.length === expectedPhases.length, "MACOS_CODEX_LUNA_INVOCATION_COUNT_INVALID", "Model invocation count drifted", { workflow, expected: expectedPhases.length, actual: invocations?.length ?? null });
  requireE2E(invocations.every((item, index) => {
    const started = Date.parse(item.started_at_utc);
    const finished = Date.parse(item.finished_at_utc);
    return item.phase === expectedPhases[index]
      && item.model === CODEX_LUNA_MODEL
      && item.reasoning_effort === CODEX_LUNA_REASONING_EFFORT
      && item.attempt === 1
      && item.retry === 0
      && item.status === "PASS"
      && item.terminal === true
      && item.wall_timeout_seconds === MACOS_CODEX_LUNA_CALL_WALL_SECONDS
      && Number.isFinite(started)
      && Number.isFinite(finished)
      && finished >= started;
  }), "MACOS_CODEX_LUNA_INVOCATION_IDENTITY_INVALID", "Model invocation order, identity, retry, timeout, timestamps, status, or terminal receipt is invalid");
  const aggregate = aggregateCodexUsage(invocations);
  requireE2E(aggregate.total_tokens <= tokenLimit && aggregate.equivalent_usd <= costLimit, "MACOS_CODEX_LUNA_BUDGET_EXCEEDED", "Terminal aggregate usage exceeded the Goal hard cap", { aggregate, token_limit: tokenLimit, equivalent_usd_limit: costLimit });
  return { schema_version: 1, status: "PASS", workflow, expected_phases: expectedPhases, retry_count: 0, aggregate, price_snapshot: MACOS_CODEX_LUNA_PRICE_SNAPSHOT };
}

export function buildMethodsProducerIdentity({ wiki, metaSkillRoot, registrationTemplate, codexIdentity }) {
  ordinaryFile(wiki, "canonical Wiki");
  ordinaryFile(registrationTemplate, "registration template");
  const outputContract = path.join(metaSkillRoot, "references", "output-contract.md");
  const validator = path.join(metaSkillRoot, "scripts", "validate_generated_skill.py");
  ordinaryFile(outputContract, "Methods output contract");
  ordinaryFile(validator, "Methods validator");
  requireE2E(isPlainObject(codexIdentity) && codexIdentity.status === "PASS", "MACOS_CODEX_LUNA_CODEX_IDENTITY_INVALID", "Codex identity must be planner-validated");
  const inputs = {
    schema_version: 1,
    contract_version: MACOS_CODEX_LUNA_E2E_CONTRACT_VERSION,
    wiki: { path_class: "canonical-wiki", sha256: sha256File(wiki), size: ordinaryFile(wiki, "canonical Wiki").size },
    meta_skill: { tree_sha256: treeDigest(metaSkillRoot), manifest_sha256: sha256Bytes(canonicalJson(treeManifest(metaSkillRoot))) },
    output_contract: { sha256: sha256File(outputContract), size: ordinaryFile(outputContract, "Methods output contract").size },
    validator: { sha256: sha256File(validator), size: ordinaryFile(validator, "Methods validator").size },
    registration_template: { sha256: sha256File(registrationTemplate), size: ordinaryFile(registrationTemplate, "registration template").size },
    model: {
      model: CODEX_LUNA_MODEL,
      reasoning_effort: CODEX_LUNA_REASONING_EFFORT,
    },
    generation_prompt_version: MACOS_CODEX_LUNA_METHODS_PROMPT_VERSION,
    runner_contract: "macos-codex-luna-methods-bootstrap-v1",
  };
  requireE2E(Object.values(inputs.model).every(isNonEmptyString), "MACOS_CODEX_LUNA_MODEL_IDENTITY_INVALID", "Methods model identity is incomplete");
  return Object.freeze({ schema_version: 1, producer_identity: sha256Bytes(canonicalJson(inputs)), inputs });
}

export function methodsCachePath(cacheRoot, producerIdentity) {
  requireE2E(path.isAbsolute(cacheRoot), "MACOS_CODEX_LUNA_CACHE_ROOT_INVALID", "Methods cache root must be absolute");
  requireE2E(/^[a-f0-9]{64}$/.test(producerIdentity), "MACOS_CODEX_LUNA_PRODUCER_IDENTITY_INVALID", "Methods producer identity must be SHA-256");
  return path.join(cacheRoot, "codex-luna-methods", producerIdentity);
}

export function buildMethodsCacheManifest({ producer, packageRoot, registrationTemplate }) {
  requireE2E(isPlainObject(producer) && /^[a-f0-9]{64}$/.test(producer.producer_identity), "MACOS_CODEX_LUNA_PRODUCER_IDENTITY_INVALID", "Methods producer identity is invalid");
  const registration = readJson(registrationTemplate, "registration template");
  requireE2E(registration.registration_id === MACOS_CODEX_LUNA_REGISTRATION_ID && registration.package?.skill_name === MACOS_CODEX_LUNA_SKILL_NAME, "MACOS_CODEX_LUNA_REGISTRATION_INVALID", "Registration template does not bind the expected Methods package");
  const manifest = treeManifest(packageRoot);
  requireE2E(manifest.length > 0, "MACOS_CODEX_LUNA_METHODS_PACKAGE_EMPTY", "Generated Methods package is empty");
  return {
    schema_version: 1,
    producer,
    package: { skill_name: MACOS_CODEX_LUNA_SKILL_NAME, tree_sha256: treeDigest(packageRoot), files: manifest },
    registration: { registration_id: MACOS_CODEX_LUNA_REGISTRATION_ID, template_sha256: sha256File(registrationTemplate) },
  };
}

export function validateMethodsCache({ cacheRoot, producer, registrationTemplate }) {
  const root = methodsCachePath(cacheRoot, producer.producer_identity);
  ordinaryFile(path.join(root, "manifest.json"), "Methods cache manifest");
  const manifest = readJson(path.join(root, "manifest.json"), "Methods cache manifest");
  const packageRoot = path.join(root, "package", MACOS_CODEX_LUNA_SKILL_NAME);
  const expected = buildMethodsCacheManifest({ producer, packageRoot, registrationTemplate });
  requireE2E(canonicalJson(manifest) === canonicalJson(expected), "MACOS_CODEX_LUNA_METHODS_CACHE_IDENTITY_MISMATCH", "Methods cache bytes or producer identity drifted");
  return Object.freeze({ schema_version: 1, status: "PASS", root, package_root: packageRoot, manifest });
}

export function assertMethodsPackageUnchanged(cacheReceipt) {
  requireE2E(cacheReceipt?.status === "PASS", "MACOS_CODEX_LUNA_METHODS_CACHE_RECEIPT_INVALID", "Methods cache receipt is invalid");
  const current = treeDigest(cacheReceipt.package_root);
  requireE2E(current === cacheReceipt.manifest.package.tree_sha256, "MACOS_CODEX_LUNA_METHODS_PACKAGE_DRIFT", "Frozen Methods package changed during E2E");
  return { schema_version: 1, status: "PASS", tree_sha256: current };
}

export function auditOracle({ oracle, publicCase, sealedDiagnosis, evidenceSources }) {
  requireE2E(isPlainObject(oracle) && isPlainObject(publicCase) && isPlainObject(sealedDiagnosis) && Array.isArray(evidenceSources), "MACOS_CODEX_LUNA_ORACLE_INPUT_INVALID", "Oracle audit inputs are invalid");
  const expectedPublic = {
    CONFIRMED: "COMPLETED",
    PARTIAL: "PARTIAL",
    INSUFFICIENT: "INCONCLUSIVE",
  }[oracle.expected_status];
  requireE2E(publicCase.status === expectedPublic, "MACOS_CODEX_LUNA_PUBLIC_STATUS_MISMATCH", "Final public Case status does not satisfy the scenario oracle", { expected: expectedPublic, actual: publicCase.status });
  requireE2E(sealedDiagnosis.status === oracle.expected_status, "MACOS_CODEX_LUNA_DIAGNOSIS_STATUS_MISMATCH", "Server-sealed Methods status does not satisfy the scenario oracle");
  const confirmed = new Set(sealedDiagnosis.confirmed_methods ?? []);
  const evidence = sealedDiagnosis.evidence ?? [];
  requireE2E(Array.isArray(sealedDiagnosis.confirmed_methods) && Array.isArray(evidence), "MACOS_CODEX_LUNA_DIAGNOSIS_SHAPE_INVALID", "Server-sealed Methods diagnosis shape is invalid");
  const markerMatches = (actual, expected) => typeof actual === "string"
    && actual.startsWith(expected)
    && (actual.length === expected.length || /[^A-Za-z0-9_]/.test(actual[expected.length]));
  for (const marker of oracle.expected_branch_markers) {
    const matches = evidence.filter((item) => confirmed.has(item?.method_id) && (item.sources ?? []).some((source) => markerMatches(source?.marker, marker)));
    requireE2E(matches.length > 0, "MACOS_CODEX_LUNA_BRANCH_MARKER_MISSING", "Expected branch marker is absent from confirmed grounded evidence", { marker });
  }
  const rendered = canonicalJson(sealedDiagnosis);
  for (const term of oracle.expected_terms) requireE2E(rendered.includes(term), "MACOS_CODEX_LUNA_EXPECTED_TERM_MISSING", "Expected diagnosis term is absent", { term });
  const renderedEvidence = canonicalJson(evidence);
  for (const term of oracle.forbidden_evidence_terms) requireE2E(!renderedEvidence.includes(term), "MACOS_CODEX_LUNA_FORBIDDEN_TERM_PRESENT", "Forbidden evidence term is present", { term });
  const sourceById = new Map(evidenceSources.map((source) => [source.source_id, source]));
  for (const item of evidence) {
    for (const source of item.sources ?? []) {
      const frozen = sourceById.get(source.source_id);
      requireE2E(frozen && frozen.raw_sha256 === source.raw_sha256 && frozen.file_name === source.file_name && frozen.lines?.[source.line_number - 1] === source.line, "MACOS_CODEX_LUNA_EVIDENCE_IDENTITY_INVALID", "Diagnosis evidence cannot be traced to this run's ZIP member", { source_id: source.source_id, line_number: source.line_number });
    }
  }
  const matchedSourceIdentities = new Set();
  const matchedEvidencePartitions = [];
  for (const expectation of oracle.expected_evidence_identities) {
    const matchesBySource = new Map();
    for (const [index, item] of evidence.entries()) {
      if (!confirmed.has(item?.method_id)
        || !expectation.identity_tokens.every((token) => (item.identity_tokens ?? []).includes(token))) continue;
      for (const source of item.sources ?? []) {
        if (!markerMatches(source?.marker, expectation.branch_marker)
          || !expectation.identity_tokens.every((token) => source.line.includes(token))) continue;
        const sourceIdentity = canonicalJson({
          file_name: source.file_name,
          line: source.line,
          line_number: source.line_number,
          raw_sha256: source.raw_sha256,
          source_id: source.source_id,
        });
        const indexes = matchesBySource.get(sourceIdentity) ?? new Set();
        indexes.add(index);
        matchesBySource.set(sourceIdentity, indexes);
      }
    }
    requireE2E(matchesBySource.size === 1, "MACOS_CODEX_LUNA_EXPECTED_EVIDENCE_IDENTITY_MISMATCH", "Diagnosis did not preserve exactly one frozen source identity", { branch_marker: expectation.branch_marker });
    const [sourceIdentity, evidenceIndexes] = matchesBySource.entries().next().value;
    requireE2E(!matchedSourceIdentities.has(sourceIdentity), "MACOS_CODEX_LUNA_EXPECTED_EVIDENCE_IDENTITY_MERGED", "One frozen source identity satisfied multiple expected identities", { branch_marker: expectation.branch_marker });
    requireE2E(
      matchedEvidencePartitions.every((indexes) => [...evidenceIndexes].every((index) => !indexes.has(index))),
      "MACOS_CODEX_LUNA_EXPECTED_EVIDENCE_IDENTITY_MERGED",
      "One diagnosis evidence item merged multiple expected identities",
      { branch_marker: expectation.branch_marker },
    );
    matchedSourceIdentities.add(sourceIdentity);
    matchedEvidencePartitions.push(evidenceIndexes);
  }
  return { schema_version: 1, status: "PASS", scenario_id: oracle.scenario_id, expected_public_status: expectedPublic };
}
