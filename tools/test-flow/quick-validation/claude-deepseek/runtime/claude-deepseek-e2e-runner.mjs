#!/usr/bin/env node
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import zlib from "node:zlib";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { canonicalJson, sha256Bytes, sha256File } from "../../../lib/util.mjs";
import { materializeClaudeSettings } from "../../../lib/release-inputs.mjs";
import {
  clientPrompt,
  createStandaloneGitBoundary,
  extractCommandHttpEntries,
  partitionMcpCalls,
  stateEvidence,
  structuredMcpData,
  validDescriptorUploadCommand,
} from "../../codex-luna/runtime/macos-codex-luna-e2e-runner.mjs";
import {
  CLAUDE_DEEPSEEK_CALL_WALL_SECONDS,
  CLAUDE_DEEPSEEK_CLIENT_PROMPT_VERSION,
  CLAUDE_DEEPSEEK_E2E_MAX_TURNS,
  CLAUDE_DEEPSEEK_E2E_USD_LIMIT,
  CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
  CLAUDE_DEEPSEEK_PUBLIC_TOOLS,
  CLAUDE_DEEPSEEK_REGISTRATION_ID,
  claudeDeepseekE2EPhases,
  assertRegistrationUnchanged,
  auditClaudeInvocations,
  auditClientBash,
  auditHttpBoundary,
  auditListedMcpTools,
  auditMcpToolCalls,
  auditOracle,
  auditUploadedAttachment,
  buildRegistrationProducerIdentity,
  loadScenarioFacts,
  loadScenarioOracle,
  mapScenarioToCreateCase,
  scenarioPaths,
  treeDigest,
  treeManifest,
  validateClaudeDeepseekIdentity,
  validateRegistrationCache,
  writeDeterministicLogsZip,
} from "./claude-deepseek-contract.mjs";
import { runClaudeProcess } from "./claude-deepseek-process.mjs";

const MODULE_PATH = fileURLToPath(import.meta.url);
const TERMINAL_CASE_STATUSES = new Set(["RESOLVED", "PARTIALLY_RESOLVED", "UNRESOLVED", "FAILED", "CANCELLED"]);
const FULL_MCP_TOOLS = Object.freeze(CLAUDE_DEEPSEEK_PUBLIC_TOOLS.map((name) => `mcp__problem-locator__${name}`));
const CLIENT_DISALLOWED_TOOLS = Object.freeze(["Read", "Glob", "Grep", "Edit", "Write"]);
const CLIENT_TOOL_INPUT_SYSTEM_PROMPT = "Standalone Fast E2E 硬约束：每次调用 problem_locator_get_case，必须在同一个 tool_use.input 中一次性传入 case_id、wait_for_job_id、wait_seconds。本场景的 wait_for_job_id 始终使用原生 JSON null，不传 Job UUID；等待 Job 或 terminal 时 wait_seconds=30，只在流程明确要求刷新 revision 时使用 0。禁止主动发送空 {}，也禁止先发工具名再补参数。若 Host 仍意外产出一次空 {} 且 Server 以零副作用 VALIDATION_ERROR 拒绝，必须在紧接着的下一次 tool_use 中完整补发当前 case_id、wait_for_job_id:null 和原本要用的 wait_seconds（只能是 0 或 30）；中间不得调用其他工具。全流程最多纠正一次，第二次空输入或纠正失败必须立即停止。字符串 null 始终无效。";
const SHARED_POLLING_CLAUSE = "每次 get_case 的 wait_seconds 必须是 0 到 30；已知 job_id 时用 wait_for_job_id 等待同一 Job。";
const CLAUDE_POLLING_CLAUSE = "本场景每次 get_case 的 wait_for_job_id 都必须是原生 JSON null；等待 Job 或 terminal 时 wait_seconds=30，只在上述两次 revision refresh 时使用 0。";

export function clientToolInputPolicyIdentity() {
  return {
    schema_version: 1,
    version: CLAUDE_DEEPSEEK_CLIENT_PROMPT_VERSION,
    sha256: sha256Bytes(CLIENT_TOOL_INPUT_SYSTEM_PROMPT),
    utf8_size: Buffer.byteLength(CLIENT_TOOL_INPUT_SYSTEM_PROMPT, "utf8"),
  };
}

export function claudeClientPrompt(options) {
  const shared = clientPrompt(options);
  requireE2E(shared.includes(SHARED_POLLING_CLAUSE), "CLAUDE_DEEPSEEK_SHARED_CLIENT_PROMPT_DRIFT", "Shared Client prompt no longer exposes the polling clause that Claude must specialize");
  return shared.replace(SHARED_POLLING_CLAUSE, CLAUDE_POLLING_CLAUSE);
}

class E2ERunnerError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "E2ERunnerError";
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) { throw new E2ERunnerError(code, message, details); }
function requireE2E(condition, code, message, details = {}) { if (!condition) fail(code, message, details); }
function isPlainObject(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }

function createEmptyRoot(root, label) {
  const resolved = path.resolve(root);
  if (fs.existsSync(resolved)) requireE2E(fs.statSync(resolved).isDirectory() && fs.readdirSync(resolved).length === 0, "CLAUDE_DEEPSEEK_E2E_ROOT_NOT_EMPTY", `${label} must be empty`);
  else fs.mkdirSync(resolved, { recursive: true, mode: 0o700 });
  return resolved;
}

function writeJson(filePath, value, { exclusive = true } = {}) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, canonicalJson(value), { encoding: "utf8", mode: 0o600, flag: exclusive ? "wx" : "w" });
}

export function materializeDefaultLogparseConfig(source, destination) {
  const value = JSON.parse(fs.readFileSync(source, "utf8"));
  const product = value.products?.["rpc-skill-feasibility"];
  requireE2E(value.schema_version === 2 && isPlainObject(value.pipeline) && isPlainObject(product), "CLAUDE_DEEPSEEK_LOGPARSE_CONFIG_INVALID", "Repository Logparse config cannot materialize the default product");
  const materialized = { schema_version: value.schema_version, pipeline: value.pipeline, products: { default: product } };
  writeJson(destination, materialized);
  return { schema_version: 1, status: "PASS", materialization_version: 1, source_sha256: sha256File(source), materialized_sha256: sha256File(destination), product: "default", module: "rpc", product_count: 1, product_bytes_sha256: sha256Bytes(canonicalJson(product)) };
}

export function serviceSourceEnvironment(sourceRoot) {
  requireE2E(typeof sourceRoot === "string" && path.isAbsolute(sourceRoot), "CLAUDE_DEEPSEEK_SERVICE_SOURCE_ROOT_INVALID", "Service launcher source root must be absolute");
  return { TEST_FLOW_SOURCE_ROOT: path.resolve(sourceRoot) };
}

export function materializeClientSettings(stagedProviderSettings, destination, { hookScript, policyPath } = {}) {
  const provider = JSON.parse(fs.readFileSync(stagedProviderSettings, "utf8"));
  const allow = ["Bash(/usr/bin/openssl:*)", "Bash(/usr/bin/stat:*)", "Bash(/usr/bin/curl:*)"];
  requireE2E(typeof hookScript === "string" && path.isAbsolute(hookScript) && fs.statSync(hookScript).isFile() && typeof policyPath === "string" && path.isAbsolute(policyPath) && fs.statSync(policyPath).isFile(), "CLAUDE_DEEPSEEK_BASH_POLICY_INPUT_INVALID", "Client Bash policy inputs must be existing absolute files");
  const command = [process.execPath, hookScript, "--policy", policyPath].map(shellQuote).join(" ");
  const hooks = { PreToolUse: [{ matcher: "Bash", hooks: [{ type: "command", command, timeout: 5 }] }] };
  writeJson(destination, { env: provider.env, permissions: { allow }, hooks });
  return { schema_version: 1, status: "PASS", allow, hooks_copied: false, test_owned_pre_tool_use: true, provider_env_unchanged: true, policy_sha256: sha256File(policyPath), sha256: sha256File(destination) };
}

function copyTree(source, destination) {
  requireE2E(!fs.existsSync(destination), "CLAUDE_DEEPSEEK_E2E_COPY_COLLISION", "Copy destination already exists");
  fs.mkdirSync(destination, { recursive: true, mode: 0o700 });
  for (const entry of fs.readdirSync(source, { withFileTypes: true })) {
    const from = path.join(source, entry.name);
    const to = path.join(destination, entry.name);
    const metadata = fs.lstatSync(from);
    requireE2E(!metadata.isSymbolicLink(), "CLAUDE_DEEPSEEK_E2E_COPY_LINK", "E2E inputs cannot contain links");
    if (entry.isDirectory()) copyTree(from, to);
    else if (entry.isFile() && metadata.nlink === 1) fs.copyFileSync(from, to, fs.constants.COPYFILE_EXCL);
    else fail("CLAUDE_DEEPSEEK_E2E_COPY_NODE", "E2E inputs may contain ordinary files only");
  }
}

function materializeRegistration({ skillDir, cache }) {
  const registrationRoot = path.join(skillDir, CLAUDE_DEEPSEEK_REGISTRATION_ID);
  copyTree(cache.registration_root, registrationRoot);
  const inventory = (records) => records.filter((item) => item.kind === "file").map(({ path: relative, size, sha256 }) => ({ path: relative, size, sha256 }));
  requireE2E(canonicalJson(inventory(treeManifest(registrationRoot))) === canonicalJson(inventory(cache.manifest.registration.files)), "CLAUDE_DEEPSEEK_REGISTRATION_COPY_MISMATCH", "SKILL_DIR registration bytes differ from the exact generation cache");
  return registrationRoot;
}

function shellQuote(value) {
  requireE2E(typeof value === "string" && value && !value.includes("\0"), "CLAUDE_DEEPSEEK_COMMAND_VALUE_INVALID", "Service wrapper value is invalid");
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

async function reservePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => { server.once("error", reject); server.listen({ host: "127.0.0.1", port: 0, exclusive: true }, resolve); });
  const address = server.address();
  requireE2E(isPlainObject(address) && Number.isSafeInteger(address.port), "CLAUDE_DEEPSEEK_PORT_INVALID", "Could not reserve one IPv4 loopback port");
  await new Promise((resolve) => server.close(resolve));
  return address.port;
}

function wait(milliseconds) { return new Promise((resolve) => setTimeout(resolve, milliseconds)); }
function processClosed(child) { return new Promise((resolve) => child.once("close", (code, signal) => resolve({ code, signal }))); }
function terminateOwnedProcess(child) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  try { process.kill(-child.pid, "SIGTERM"); } catch {}
  setTimeout(() => { if (child.exitCode === null && child.signalCode === null) try { process.kill(-child.pid, "SIGKILL"); } catch {} }, 5_000).unref();
}

function probeMcp({ pythonEntry, script, mcpUrl, output }) {
  return spawnSync(pythonEntry, ["-I", "-B", script, "--url", mcpUrl, "--output", output], {
    cwd: path.dirname(script),
    env: { PATH: "/usr/bin:/bin:/usr/sbin:/sbin", LANG: "C.UTF-8", PYTHONDONTWRITEBYTECODE: "1", PYTHONNOUSERSITE: "1" },
    encoding: "utf8", timeout: 35_000, stdio: ["ignore", "pipe", "pipe"],
  });
}

async function waitForMcp(options, child, timeoutMs = 30_000) {
  const started = Date.now();
  let attempt = 0;
  while (Date.now() - started < timeoutMs) {
    requireE2E(child.exitCode === null && child.signalCode === null, "CLAUDE_DEEPSEEK_SERVICE_EARLY_EXIT", "Local service exited before MCP readiness");
    const output = `${options.output}.${attempt += 1}`;
    const result = probeMcp({ ...options, output });
    if (result.status === 0 && result.signal === null && !result.error) { fs.renameSync(output, options.output); return JSON.parse(fs.readFileSync(options.output, "utf8")); }
    if (fs.existsSync(output)) fs.unlinkSync(output);
    await wait(300);
  }
  fail("CLAUDE_DEEPSEEK_MCP_READINESS_TIMEOUT", "MCP initialize/tools list did not become ready");
}

function artifactConsistency(finalCase, artifactData, { expectArchive }) {
  const artifacts = artifactData.artifacts;
  requireE2E(Array.isArray(artifacts) && artifacts.length > 0, "CLAUDE_DEEPSEEK_ARTIFACT_INDEX_EMPTY", "MCP list_artifacts returned no artifacts");
  const summaries = finalCase.artifacts ?? [];
  requireE2E(artifacts.every((artifact) => summaries.some((summary) => summary.artifact_id === artifact.artifact_id && summary.kind === artifact.kind && summary.size === artifact.size && summary.sha256 === artifact.sha256)), "CLAUDE_DEEPSEEK_ARTIFACT_INDEX_MISMATCH", "Artifact index differs from the terminal Case projection");
  requireE2E(artifacts.filter((artifact) => artifact.kind === "USER_RESULT").length === 1, "CLAUDE_DEEPSEEK_USER_RESULT_CARDINALITY_INVALID", "Terminal Case must expose exactly one USER_RESULT");
  requireE2E(artifacts.filter((artifact) => artifact.kind === "USER_RESULT_ARCHIVE").length === (expectArchive ? 1 : 0), "CLAUDE_DEEPSEEK_USER_RESULT_ARCHIVE_CARDINALITY_INVALID", "Artifact index archive cardinality differs from the terminal scenario path");
  const archive = artifacts.find((artifact) => artifact.kind === "USER_RESULT_ARCHIVE") ?? null;
  requireE2E(!expectArchive || (archive.name === "result.zip" && archive.content_type === "application/zip" && Number.isSafeInteger(archive.size) && archive.size > 0 && /^[a-f0-9]{64}$/u.test(archive.sha256 ?? "") && typeof archive.download_url === "string" && archive.download_url.length > 0), "CLAUDE_DEEPSEEK_USER_RESULT_ARCHIVE_INVALID", "Published USER_RESULT_ARCHIVE is not the downloadable result.zip");
  return { schema_version: 1, status: "PASS", artifact_count: artifacts.length, user_result_count: 1, user_result_archive_count: expectArchive ? 1 : 0 };
}

export function auditClientSkillLoad(client) {
  requireE2E(Array.isArray(client?.records) && Array.isArray(client?.skills), "CLAUDE_DEEPSEEK_CLIENT_SKILL_LEDGER_INVALID", "Client Skill audit requires the complete model tool trace");
  const records = client.records.filter((item) => item.name === "Skill");
  requireE2E(records.length === 1 && client.skills.length === 1 && records[0].ordinal === 0 && records[0].is_error === false && records[0].input?.skill === "problem-locator-client" && client.skills[0].ordinal === 0 && client.skills[0].skill === "problem-locator-client", "CLAUDE_DEEPSEEK_CLIENT_SKILL_LOAD_INVALID", "Client must successfully load problem-locator-client exactly once as its first tool use");
  return { schema_version: 1, status: "PASS", skill: "problem-locator-client", call_count: 1, tool_ordinal: 0 };
}

function readZipEntries(bytes) {
  const entries = [];
  let offset = 0;
  while (offset + 4 <= bytes.length && bytes.readUInt32LE(offset) === 0x04034b50) {
    requireE2E(offset + 30 <= bytes.length, "CLAUDE_DEEPSEEK_RESULT_ZIP_INVALID", "result.zip has a truncated local header");
    const flags = bytes.readUInt16LE(offset + 6);
    const method = bytes.readUInt16LE(offset + 8);
    const compressedSize = bytes.readUInt32LE(offset + 18);
    const size = bytes.readUInt32LE(offset + 22);
    const nameSize = bytes.readUInt16LE(offset + 26);
    const extraSize = bytes.readUInt16LE(offset + 28);
    requireE2E((flags & 0x08) === 0 && method === 8 && extraSize === 0, "CLAUDE_DEEPSEEK_RESULT_ZIP_INVALID", "result.zip local entry flags, compression, or extras are invalid");
    const nameStart = offset + 30;
    const dataStart = nameStart + nameSize + extraSize;
    const dataEnd = dataStart + compressedSize;
    requireE2E(dataEnd <= bytes.length, "CLAUDE_DEEPSEEK_RESULT_ZIP_INVALID", "result.zip entry exceeds the archive bytes");
    const name = bytes.subarray(nameStart, nameStart + nameSize).toString("utf8");
    let content;
    try { content = zlib.inflateRawSync(bytes.subarray(dataStart, dataEnd)); } catch { fail("CLAUDE_DEEPSEEK_RESULT_ZIP_INVALID", "result.zip entry cannot be inflated"); }
    requireE2E(content.length === size && name && !name.includes("/") && !name.includes("\\"), "CLAUDE_DEEPSEEK_RESULT_ZIP_INVALID", "result.zip entry name or size is invalid");
    entries.push({ name, content });
    offset = dataEnd;
  }
  requireE2E(entries.length >= 2 && offset + 4 <= bytes.length && bytes.readUInt32LE(offset) === 0x02014b50 && new Set(entries.map((item) => item.name.toLowerCase())).size === entries.length, "CLAUDE_DEEPSEEK_RESULT_ZIP_INVALID", "result.zip central directory or entry uniqueness is invalid");
  return entries;
}

export function auditDownloadedResultArchive({ filePath, artifact, userResultArtifact, problemTime, targetLogs }) {
  requireE2E(fs.existsSync(filePath) && fs.statSync(filePath).isFile(), "CLAUDE_DEEPSEEK_RESULT_DOWNLOAD_MISSING", "Downloaded result.zip is missing");
  const bytes = fs.readFileSync(filePath);
  requireE2E(bytes.length === artifact.size && sha256File(filePath) === artifact.sha256, "CLAUDE_DEEPSEEK_RESULT_DOWNLOAD_IDENTITY_MISMATCH", "Downloaded result.zip differs from list_artifacts metadata");
  const entries = readZipEntries(bytes);
  requireE2E(entries[0].name === "result.txt" && entries[0].content.length > 0 && entries[1].name === "archive-manifest.json" && entries.slice(2).every((item) => item.name.endsWith(".log")), "CLAUDE_DEEPSEEK_RESULT_ZIP_SHAPE_INVALID", "Server v3 result.zip entry order or names are invalid");
  let manifest;
  try { manifest = JSON.parse(entries[1].content.toString("utf8")); } catch { fail("CLAUDE_DEEPSEEK_RESULT_ZIP_MANIFEST_INVALID", "Server v3 result.zip manifest is invalid JSON"); }
  requireE2E(manifest.schema_version === 3 && manifest.format_id === "problem-locator-result-archive-v3" && manifest.problem_time === problemTime && manifest.diagnosis_result_sha256 === userResultArtifact.sha256 && manifest.result_txt_sha256 === sha256Bytes(entries[0].content) && manifest.target_log_count === entries.length - 2 && Array.isArray(manifest.target_logs) && manifest.target_logs.length === manifest.target_log_count, "CLAUDE_DEEPSEEK_RESULT_ZIP_MANIFEST_INVALID", "Server v3 result.zip manifest identity is invalid");
  requireE2E(Array.isArray(targetLogs?.target_logs) && targetLogs.target_logs.length === manifest.target_log_count, "CLAUDE_DEEPSEEK_RESULT_ZIP_TARGET_COUNT_INVALID", "Server v3 result.zip target count differs from frozen target_logs");
  for (const [index, item] of manifest.target_logs.entries()) {
    const entry = entries[index + 2];
    const frozen = targetLogs.target_logs[index];
    requireE2E(item.ordinal === index + 1 && item.archive_name === entry.name && item.size === entry.content.length && item.sha256 === sha256Bytes(entry.content) && item.label === frozen.label && item.size === frozen.size && item.sha256 === frozen.content_sha256, "CLAUDE_DEEPSEEK_RESULT_ZIP_TARGET_INVALID", "Server v3 result.zip target entry differs from frozen target_logs");
  }
  return { schema_version: 3, status: "PASS", size: bytes.length, sha256: sha256File(filePath), entries: entries.map((item) => item.name), target_log_count: manifest.target_log_count, format_id: manifest.format_id };
}

function businessEnvelope(value, candidates = []) {
  if (typeof value === "string") {
    try { businessEnvelope(JSON.parse(value), candidates); } catch {}
  } else if (Array.isArray(value)) value.forEach((item) => businessEnvelope(item, candidates));
  else if (isPlainObject(value)) {
    if (typeof value.ok === "boolean" && Object.hasOwn(value, "data") && Object.hasOwn(value, "error")) candidates.push(value);
    Object.values(value).forEach((item) => businessEnvelope(item, candidates));
  }
  return candidates[0] ?? null;
}

export function auditMcpRecoveries(calls) {
  requireE2E(Array.isArray(calls), "CLAUDE_DEEPSEEK_RECOVERY_LEDGER_INVALID", "Recovery audit requires the complete Client MCP ledger");
  const failures = calls.filter((call) => businessEnvelope(call.result)?.ok === false);
  requireE2E(failures.length <= 2, "CLAUDE_DEEPSEEK_RECOVERY_CARDINALITY_INVALID", "Client exceeded the bounded business-error correction count");
  const recoveries = failures.map((failure) => {
    const envelope = businessEnvelope(failure.result);
    const code = envelope?.error?.code ?? null;
    const validationDetails = Array.isArray(envelope?.error?.details) ? envelope.error.details : [];
    const emptyGetCase = failure.tool === "problem_locator_get_case"
      && code === "VALIDATION_ERROR"
      && isPlainObject(failure.arguments)
      && Object.keys(failure.arguments).length === 0;
    const stringNullGetCase = failure.tool === "problem_locator_get_case"
      && code === "VALIDATION_ERROR"
      && envelope?.error?.retryable === false
      && isPlainObject(failure.arguments)
      && canonicalJson(Object.keys(failure.arguments).sort()) === canonicalJson(["case_id", "wait_for_job_id", "wait_seconds"])
      && typeof failure.arguments.case_id === "string"
      && failure.arguments.wait_for_job_id === "null"
      && Number.isSafeInteger(failure.arguments.wait_seconds)
      && failure.arguments.wait_seconds >= 0
      && failure.arguments.wait_seconds <= 30
      && validationDetails.length === 1
      && validationDetails[0]?.field === "wait_for_job_id"
      && validationDetails[0]?.actual === "null";
    if (emptyGetCase || stringNullGetCase) {
      const next = calls.filter((candidate) => candidate.ordinal > failure.ordinal).sort((left, right) => left.ordinal - right.ordinal)[0];
      const completeEmptyCorrection = emptyGetCase
        && typeof next?.arguments?.case_id === "string"
        && next.arguments.wait_for_job_id === null
        && [0, 30].includes(next.arguments.wait_seconds);
      const correctedKeys = isPlainObject(next?.arguments) ? Object.keys(next.arguments).sort() : [];
      const correctedStringNull = stringNullGetCase
        && next?.arguments?.case_id === failure.arguments.case_id
        && next.arguments.wait_seconds === failure.arguments.wait_seconds
        && (canonicalJson(correctedKeys) === canonicalJson(["case_id", "wait_seconds"])
          || (canonicalJson(correctedKeys) === canonicalJson(["case_id", "wait_for_job_id", "wait_seconds"])
            && next.arguments.wait_for_job_id === null));
      requireE2E(next?.tool === failure.tool
        && (completeEmptyCorrection || correctedStringNull)
        && businessEnvelope(next.result)?.ok === true,
      "CLAUDE_DEEPSEEK_RECOVERY_REQUEST_ID_INVALID", "A get_case syntax validation error must have one immediate bounded successful correction");
      return { tool: failure.tool, code: emptyGetCase ? "EMPTY_GET_CASE_VALIDATION" : "STRING_NULL_GET_CASE_VALIDATION", request_id: null, failed_ordinal: failure.ordinal, corrected_ordinal: next.ordinal };
    }
    const requestId = failure.arguments?.request_id;
    requireE2E(["REVISION_CONFLICT", "ATTACHMENT_NOT_READY"].includes(code) && typeof requestId === "string" && requestId.length > 0, "CLAUDE_DEEPSEEK_RECOVERY_ERROR_INVALID", "Client encountered a non-recoverable MCP business error");
    const corrections = calls.filter((candidate) => candidate.ordinal > failure.ordinal
      && candidate.tool === failure.tool
      && candidate.arguments?.request_id === requestId
      && businessEnvelope(candidate.result)?.ok === true);
    requireE2E(corrections.length === 1, "CLAUDE_DEEPSEEK_RECOVERY_REQUEST_ID_INVALID", "A recoverable MCP error must have exactly one later successful correction with the original request ID");
    return { tool: failure.tool, code, request_id: requestId, failed_ordinal: failure.ordinal, corrected_ordinal: corrections[0].ordinal };
  });
  requireE2E(recoveries.filter((item) => ["EMPTY_GET_CASE_VALIDATION", "STRING_NULL_GET_CASE_VALIDATION"].includes(item.code)).length <= 1,
    "CLAUDE_DEEPSEEK_RECOVERY_REPEATED", "Client may correct at most one get_case syntax validation error");
  requireE2E(new Set(recoveries.map((item) => item.request_id === null ? `${item.tool}\0ordinal:${item.failed_ordinal}` : `${item.tool}\0${item.request_id}`)).size === recoveries.length, "CLAUDE_DEEPSEEK_RECOVERY_REPEATED", "The same logical write cannot be corrected more than once");
  return { schema_version: 1, status: "PASS", recoveries };
}

function combineServerEvents(dfxRoot, destination) {
  const inputs = [path.join(dfxRoot, "debug.jsonl"), path.join(dfxRoot, "journey.jsonl")];
  const chunks = inputs.filter((input) => fs.existsSync(input)).map((input) => fs.readFileSync(input));
  requireE2E(chunks.length === 2 && chunks.every((chunk) => chunk.length > 0), "CLAUDE_DEEPSEEK_SERVER_DFX_MISSING", "Server DFX streams are incomplete");
  fs.writeFileSync(destination, Buffer.concat(chunks.map((chunk) => chunk.at(-1) === 0x0a ? chunk : Buffer.concat([chunk, Buffer.from("\n")]))), { mode: 0o600, flag: "wx" });
}

function auditBashPolicyClaims(claimRoot, { expectDownload }) {
  const claims = fs.existsSync(claimRoot) ? fs.readdirSync(claimRoot).sort() : [];
  const order = expectDownload ? ["upload-openssl", "upload-stat", "upload-curl", "download-curl", "download-stat", "download-openssl"] : ["upload-openssl", "upload-stat", "upload-curl"];
  requireE2E(canonicalJson(claims) === canonicalJson([...order].sort()), "CLAUDE_DEEPSEEK_BASH_POLICY_CLAIMS_INVALID", "Bash policy claims do not match the frozen upload/download workflow");
  return { schema_version: 2, status: "PASS", claims, order };
}

function secretScan({ roots, settings, brokerToken = null }) {
  const parsed = JSON.parse(fs.readFileSync(settings, "utf8"));
  const canaries = [parsed.env?.ANTHROPIC_AUTH_TOKEN, brokerToken].filter((item) => typeof item === "string" && item.length >= 8);
  let scanned = 0;
  const visit = (root) => fs.readdirSync(root, { withFileTypes: true }).forEach((entry) => {
    const target = path.join(root, entry.name);
    if (entry.isDirectory()) visit(target);
    else if (entry.isFile()) {
      scanned += 1;
      const bytes = fs.readFileSync(target);
      for (const canary of canaries) requireE2E(!bytes.includes(Buffer.from(canary)), "CLAUDE_DEEPSEEK_SECRET_LEAK", "E2E evidence contains a provider or broker credential");
    }
  });
  roots.filter((root) => fs.existsSync(root)).forEach(visit);
  return { schema_version: 1, status: "PASS", scanned_files: scanned, canary_count: canaries.length, secret_values_persisted: false };
}

function treeBytes(root) {
  if (!fs.existsSync(root)) return 0;
  let total = 0;
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const target = path.join(root, entry.name);
    if (entry.isDirectory()) total += treeBytes(target);
    else if (entry.isFile()) total += fs.statSync(target).size;
  }
  return total;
}

export function auditSpecializedRuntime({ dataRoot, server, cache, facts, finalCase, serverInvocations }) {
  const expectedRef = cache.manifest.registration.runtime_ref;
  requireE2E(canonicalJson(finalCase.selected_skill_ref) === canonicalJson(expectedRef), "CLAUDE_DEEPSEEK_SELECTED_SKILL_MISMATCH", "ROUTE did not select the exact generated registration reference");
  requireE2E(server.aggregate.case?.generic_result == null && server.aggregate.case?.generic_result_v2 == null, "CLAUDE_DEEPSEEK_GENERIC_RESULT_PRESENT", "Specialized E2E unexpectedly produced a generic result");
  const routeJobs = server.jobs.filter((job) => job.job_type === "ROUTE");
  const diagnoseJobs = server.jobs.filter((job) => job.job_type === "DIAGNOSE");
  const reviewJobs = server.jobs.filter((job) => job.job_type === "REVIEW");
  const expectReview = serverInvocations.some((item) => item.phase === "REVIEW");
  const diagnosisJob = diagnoseJobs.find((job) => job.job_id === server.outcome?.job_id);
  requireE2E(routeJobs.length === 1 && diagnoseJobs.length === 2 && diagnosisJob !== undefined && diagnoseJobs.every((job) => job.diagnosis_mode === "SPECIALIZED" && canonicalJson(job.skill_ref) === canonicalJson(expectedRef)) && reviewJobs.length === (expectReview ? 1 : 0) && reviewJobs.every((job) => canonicalJson(job.skill_ref) === canonicalJson(expectedRef)), "CLAUDE_DEEPSEEK_SPECIALIZED_LIFECYCLE_INVALID", "The attachment preflight, actual DIAGNOSE, or REVIEW did not remain bound to the exact specialized registration");
  const routeOutcome = JSON.parse(fs.readFileSync(path.join(dataRoot, "jobs", routeJobs[0].job_id, "job_outcome.json"), "utf8"));
  requireE2E(routeOutcome.payload?.kind === "MATCHED" && canonicalJson(routeOutcome.payload.skill_ref) === canonicalJson(expectedRef), "CLAUDE_DEEPSEEK_ROUTE_NOT_MATCHED", "ROUTE outcome is not MATCHED to the generated registration");
  const logparseInvocation = serverInvocations.find((item) => item.phase === "LOGPARSE");
  requireE2E(logparseInvocation?.helper_audit?.status === "PASS" && logparseInvocation.helper_audit.mode === "SERVER_PREPROCESS" && logparseInvocation.helper_audit.helper_calls === 1 && logparseInvocation.helper_audit.broker_calls === 1 && logparseInvocation.helper_audit.helper_tool_ordinal < logparseInvocation.helper_audit.broker_tool_ordinal && logparseInvocation.helper_audit.direct_fallback === false && logparseInvocation.helper_audit.retry_count === 0 && [logparseInvocation.helper_audit.broker_entry_sha256, logparseInvocation.helper_audit.stream_trace_sha256, logparseInvocation.helper_audit.tool_sequence_sha256].every((value) => /^[a-f0-9]{64}$/u.test(value ?? "")), "CLAUDE_DEEPSEEK_HELPER_TRACE_INVALID", "LOGPARSE trace does not prove one Helper load before one broker call");
  const brokerAuditPath = path.join(dataRoot, "jobs", diagnosisJob.job_id, "logparse_broker_audit.json");
  const brokerAudit = JSON.parse(fs.readFileSync(brokerAuditPath, "utf8"));
  requireE2E(Array.isArray(brokerAudit.operations) && brokerAudit.operations.length === 1 && brokerAudit.operations[0].http_status === 200 && brokerAudit.operations[0].operation === logparseInvocation.helper_audit.operation && ["parse-targets", "target-logs"].includes(brokerAudit.operations[0].operation), "CLAUDE_DEEPSEEK_BROKER_AUDIT_INVALID", "Job-scoped broker audit must contain exactly the one successful operation observed in the model trace");
  const anchors = brokerAudit.operations[0].request?.anchors;
  const expectedAnchors = [
    { label: "client", module: "rpc", slot: facts.client_slot, process_name: facts.client_process, pid: null },
    { label: "server", module: "rpc", slot: facts.server_slot, process_name: facts.server_process, pid: null },
  ];
  requireE2E(canonicalJson(anchors) === canonicalJson(expectedAnchors), "CLAUDE_DEEPSEEK_DYNAMIC_ANCHORS_INVALID", "Actual Logparse request anchors differ from the mapped client/server facts");
  const brokerTargets = brokerAudit.operations[0].result?.target_logs;
  requireE2E(Array.isArray(brokerTargets) && brokerTargets.length === 2 && brokerTargets.every((target, index) => target.label === expectedAnchors[index].label && [target.module_key, target.module_name].includes("rpc") && String(target.slot).replace(/^slot_/iu, "") === expectedAnchors[index].slot && target.process_name === expectedAnchors[index].process_name && (target.pid ?? null) === null), "CLAUDE_DEEPSEEK_TARGET_LOG_ANCHORS_INVALID", "Actual Logparse target receipt does not preserve the requested anchor bindings");
  requireE2E(server.targetLogs.target_logs.length === 2 && server.targetLogs.target_logs.every((target, index) => target.label === expectedAnchors[index].label), "CLAUDE_DEEPSEEK_FROZEN_TARGETS_INVALID", "Server did not freeze both Logparse targets in request order");
  return { schema_version: 1, status: "PASS", route: "MATCHED", diagnosis_mode: "SPECIALIZED", diagnose_job_count: 2, attachment_preflight_job_count: 1, selected_skill_ref: expectedRef, helper: logparseInvocation.helper_audit, broker: { operation: brokerAudit.operations[0].operation, operation_count: 1, audit_sha256: sha256File(brokerAuditPath) }, anchors: expectedAnchors, generic_result: false };
}

export function parseArguments(argv) {
  const values = {};
  const names = new Set(["source-root", "runtime-root", "claude-entry", "claude-settings", "python-entry", "logparse-root", "cache-root", "scenario", "work-root", "private-root", "evidence-root", "usage-root", "run-id"]);
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    requireE2E(argument.startsWith("--"), "CLAUDE_DEEPSEEK_E2E_ARGUMENT_INVALID", "Arguments must use --name value syntax");
    const name = argument.slice(2);
    requireE2E(names.has(name), "CLAUDE_DEEPSEEK_E2E_ARGUMENT_UNKNOWN", "E2E runner received an unsupported argument");
    requireE2E(!Object.hasOwn(values, name), "CLAUDE_DEEPSEEK_E2E_ARGUMENT_DUPLICATE", "Argument is duplicated");
    requireE2E(index + 1 < argv.length && !argv[index + 1].startsWith("--"), "CLAUDE_DEEPSEEK_E2E_ARGUMENT_MISSING", "Argument value is missing");
    values[name] = argv[++index];
  }
  requireE2E([...names].filter((name) => name !== "runtime-root").every((name) => typeof values[name] === "string" && values[name]), "CLAUDE_DEEPSEEK_E2E_ARGUMENT_MISSING", "E2E runner arguments are incomplete");
  return values;
}

export async function runE2E(options, { ambient = process.env, onProgress = null } = {}) {
  const sourceRoot = path.resolve(options.sourceRoot);
  const runtimeRoot = path.resolve(options.runtimeRoot ?? sourceRoot);
  const workRoot = createEmptyRoot(options.workRoot, "E2E work root");
  const privateRoot = createEmptyRoot(options.privateRoot, "E2E private root");
  const evidenceRoot = createEmptyRoot(options.evidenceRoot, "E2E evidence root");
  const usageRoot = createEmptyRoot(options.usageRoot, "E2E usage root");
  const rawPaths = scenarioPaths(sourceRoot, options.scenario);
  const facts = loadScenarioFacts(rawPaths.case, options.scenario);
  const oracle = loadScenarioOracle(rawPaths.case, options.scenario);
  const expectedPhases = claudeDeepseekE2EPhases(options.scenario);
  const expectArchive = expectedPhases.includes("REVIEW");
  const expectedDiagnosisResultType = {
    CONFIRMED: "COMPLETED",
    PARTIAL: "PARTIAL",
    INSUFFICIENT: "INCONCLUSIVE",
  }[oracle.expected_status];
  const mapped = mapScenarioToCreateCase(facts);
  const identity = validateClaudeDeepseekIdentity(options.claudeEntry, options.claudeSettings);
  const helperIdentity = { name: "logparse-diagnose", tree_sha256: treeDigest(path.join(sourceRoot, ".claude", "skills", "logparse-diagnose"), { directoryMode: 0o700 }) };
  const releaseCaseRoot = path.join(sourceRoot, "tests", "cases", "release", "rpc-timeout-anonymized");
  const metaSkillRoot = path.join(sourceRoot, ".claude", "skills", "wiki-to-logparse-diagnosis-skill");
  const wiki = path.join(releaseCaseRoot, "input", "wiki.md");
  const producer = buildRegistrationProducerIdentity({ wiki, metaSkillRoot, claudeIdentity: identity, module: "rpc" });
  const cache = validateRegistrationCache({ cacheRoot: options.cacheRoot, producer });
  const skillDir = path.join(workRoot, "server-skill-dir");
  fs.mkdirSync(skillDir, { mode: 0o700 });
  materializeRegistration({ skillDir, cache });
  const dataRoot = path.join(workRoot, "data-root");
  const dfxRoot = path.join(workRoot, "server-dfx");
  fs.mkdirSync(dataRoot, { mode: 0o700 });
  fs.mkdirSync(dfxRoot, { mode: 0o700 });
  const clientWorkspace = path.join(workRoot, "client");
  fs.mkdirSync(clientWorkspace, { mode: 0o700 });
  createStandaloneGitBoundary(clientWorkspace);
  const clientConfig = path.join(privateRoot, "client-config");
  const serverConfig = path.join(privateRoot, "server-config");
  fs.mkdirSync(path.join(clientConfig, "skills"), { recursive: true, mode: 0o700 });
  fs.mkdirSync(path.join(serverConfig, "skills"), { recursive: true, mode: 0o700 });
  copyTree(path.join(sourceRoot, ".claude", "skills", "problem-locator-client"), path.join(clientConfig, "skills", "problem-locator-client"));
  copyTree(path.join(sourceRoot, ".claude", "skills", "logparse-diagnose"), path.join(serverConfig, "skills", "logparse-diagnose"));
  copyTree(cache.package_root, path.join(serverConfig, "skills", path.basename(cache.package_root)));
  const archivePath = path.join(clientWorkspace, "input", "logs.zip");
  const archive = writeDeterministicLogsZip({ clientLog: rawPaths.client_log, serverLog: rawPaths.server_log, destination: archivePath });
  const downloadPath = path.join(clientWorkspace, "output", "result.zip");
  fs.mkdirSync(path.dirname(downloadPath), { recursive: true, mode: 0o700 });
  writeJson(path.join(evidenceRoot, "scenario-input.json"), { schema_version: 1, scenario_id: options.scenario, source: { case_sha256: sha256File(rawPaths.case), client_log_sha256: sha256File(rawPaths.client_log), server_log_sha256: sha256File(rawPaths.server_log) }, mapper: mapped, archive });
  const stagedSettings = path.join(privateRoot, "claude-settings.json");
  materializeClaudeSettings(options.claudeSettings, stagedSettings);
  const port = await reservePort();
  const mcpUrl = `http://127.0.0.1:${port}/mcp`;
  const bashPolicyScript = path.join(sourceRoot, "tools", "test-flow", "quick-validation", "claude-deepseek", "runtime", "claude-deepseek-bash-policy.mjs");
  const bashPolicyPath = path.join(privateRoot, "client-bash-policy.json");
  const bashClaimRoot = path.join(privateRoot, "client-bash-claims");
  writeJson(bashPolicyPath, { schema_version: 2, archive_path: archivePath, archive_size: archive.size, archive_sha256: archive.sha256, upload_origin: `http://127.0.0.1:${port}`, expect_download: expectArchive, download_path: downloadPath, claim_root: bashClaimRoot });
  const clientSettings = path.join(privateRoot, "client-settings.json");
  const clientPermissionProfile = materializeClientSettings(stagedSettings, clientSettings, { hookScript: bashPolicyScript, policyPath: bashPolicyPath });
  const mcpConfig = path.join(privateRoot, "client-mcp.json");
  writeJson(mcpConfig, { mcpServers: { "problem-locator": { type: "http", url: mcpUrl, alwaysLoad: true } } });
  const servicePrivate = path.join(privateRoot, "service");
  const serviceEvidence = path.join(evidenceRoot, "service-runtime");
  const serviceUsage = path.join(usageRoot, "server");
  for (const directory of [servicePrivate, serviceEvidence, serviceUsage]) fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const sourceLogparseConfig = path.join(sourceRoot, "experiments", "rpc-skill-feasibility", "logparse-config.json");
  const defaultLogparseConfig = path.join(privateRoot, "logparse-config.default.json");
  const logparseConfigReceipt = materializeDefaultLogparseConfig(sourceLogparseConfig, defaultLogparseConfig);
  writeJson(path.join(evidenceRoot, "logparse-config.json"), logparseConfigReceipt);
  const wrapper = path.join(sourceRoot, "tools", "test-flow", "quick-validation", "claude-deepseek", "runtime", "claude-deepseek-service-wrapper.mjs");
  const serviceCommand = [process.execPath, wrapper, "--source-root", sourceRoot, "--runtime-root", runtimeRoot, "--claude-entry", options.claudeEntry, "--settings", stagedSettings, "--config-root", serverConfig, "--finalizer-entry", path.join(path.dirname(options.pythonEntry), "problem-locator-seal-outcome-draft"), "--logparse-entry", path.join(path.dirname(options.pythonEntry), "problem-locator-logparse"), "--private-root", servicePrivate, "--evidence-root", serviceEvidence, "--usage-root", serviceUsage, "--run-id", options.runId].map(shellQuote).join(" ");
  const serviceLog = path.join(privateRoot, "service.log");
  const serviceLogStream = fs.createWriteStream(serviceLog, { flags: "wx", mode: 0o600 });
  const serviceEnvironment = {
    PATH: `${path.dirname(options.pythonEntry)}:/usr/bin:/bin:/usr/sbin:/sbin`, LANG: "C.UTF-8", PYTHONDONTWRITEBYTECODE: "1", PYTHONNOUSERSITE: "1",
    ...serviceSourceEnvironment(sourceRoot),
    DATA_ROOT: dataRoot, DFX_LOG_DIR: dfxRoot, PUBLIC_BASE_URL: `http://127.0.0.1:${port}`, BIND_HOST: "127.0.0.1", PORT: String(port), SKILL_DIR: skillDir,
    GENERIC_SKILL_NAME: "generic-problem-locator-smoke", LOGPARSE_REPO: options.logparseRoot, LOGPARSE_CONFIG_PATH: defaultLogparseConfig, LOGPARSE_PYTHON: path.join(options.logparseRoot, ".venv", "bin", "python"), CLAUDE_COMMAND: serviceCommand,
  };
  const service = spawn(options.pythonEntry, serviceLauncherArguments(sourceRoot), { cwd: sourceRoot, env: serviceEnvironment, stdio: ["ignore", "pipe", "pipe"], detached: true });
  service.stdout.pipe(serviceLogStream, { end: false });
  service.stderr.pipe(serviceLogStream, { end: false });
  const serviceClosed = processClosed(service);
  let observedServiceBytes = treeBytes(serviceEvidence) + treeBytes(serviceUsage);
  const serviceProgress = setInterval(() => {
    const current = treeBytes(serviceEvidence) + treeBytes(serviceUsage);
    if (current > observedServiceBytes) { observedServiceBytes = current; onProgress?.("server-stream"); }
  }, 1_000);
  serviceProgress.unref();
  service.stdout.on("data", (chunk) => { if (String(chunk).includes("QUICK_VALIDATION_PROGRESS")) onProgress?.("server"); });
  service.stderr.on("data", (chunk) => { if (String(chunk).includes("QUICK_VALIDATION_PROGRESS")) onProgress?.("server"); });
  let readiness = null;
  let client = null;
  try {
    readiness = await waitForMcp({ pythonEntry: options.pythonEntry, script: path.join(sourceRoot, "tools", "test-flow", "quick-validation", "claude-deepseek", "runtime", "claude_deepseek_mcp_probe.py"), mcpUrl, output: path.join(privateRoot, "mcp-readiness.json") }, service);
    writeJson(path.join(evidenceRoot, "mcp-tools.json"), auditListedMcpTools(readiness.tools));
    const clientHome = path.join(privateRoot, "client-home");
    const clientTmp = path.join(privateRoot, "client-tmp");
    for (const directory of [clientHome, clientTmp]) fs.mkdirSync(directory, { mode: 0o700 });
    const baseClientPrompt = claudeClientPrompt({ mapped, archivePath, archive, runId: options.runId, scenarioId: options.scenario });
    const artifactInstruction = expectArchive
      ? `list_artifacts 成功后，必须从它返回的 artifacts 中选择 kind=USER_RESULT_ARCHIVE 且 name=result.zip 的唯一条目。逐字使用该条目的 download_url，执行且只执行一次 /usr/bin/curl --silent --show-error --fail-with-body --max-time 60 --request GET --output '${downloadPath}' '<download_url>'。GET 返回 completed/exit 0 后，依次执行 /usr/bin/stat -f %z '${downloadPath}' 与 /usr/bin/openssl dgst -sha256 '${downloadPath}'，并逐字核对同一条目的 size 和 sha256。完成这三条命令前不得结束。`
      : "本场景是 INCONCLUSIVE，不会公开 USER_RESULT_ARCHIVE；list_artifacts 后不得尝试下载。";
    client = await runClaudeProcess({
      claudeEntry: options.claudeEntry, settings: clientSettings, cwd: clientWorkspace, prompt: `${baseClientPrompt.replace("禁止下载 artifact。", "禁止下载非 USER_RESULT_ARCHIVE artifact。")}\n\n${artifactInstruction}\n\n每一次 problem_locator_get_case 都必须在同一个 tool_use.input 中一次性显式传入 case_id、wait_for_job_id、wait_seconds；不要先发送工具名再补参数。本场景的 wait_for_job_id 始终传原生 JSON null，不传 Job UUID；等待 Job 或 terminal 时 wait_seconds=30，只在上述两次 revision refresh 时使用 0。禁止主动发送空 {}；若 Host 仍意外产出一次空输入并收到 Server 的零副作用 VALIDATION_ERROR，必须紧接着用当前 case_id、wait_for_job_id:null 和原本要用的 wait_seconds（只能是 0 或 30）完整纠正一次，中间不得调用其他工具；第二次空输入或纠正失败立即停止。wait_for_job_id 严禁传字符串 "null"；如果本次只因字符串 "null" 被 VALIDATION_ERROR 拒绝，必须保留相同 case_id 和 wait_seconds，立即改成原生 null；如果 Host 仍无法表达原生 null，只允许省略这个可选字段更正一次。调用 prepare_attachment 时，declared_size 必须是整数 ${archive.size}，declared_sha256 必须逐字使用 ${archive.sha256}；这两个字段禁止传 null。curl PUT 和 curl GET 都必须各写成一条物理命令行，不得使用反斜杠续行、换行、分号、管道或命令替换；PUT 使用 --request PUT、--max-time 60、恰好四个 descriptor header 和 --upload-file 指向上述 ZIP。密封镜像已经为 /usr/bin/stat -f %z 提供兼容实现；该命令成功后不得再运行 stat -c、ls 或其他 Bash 探测。`, phase: "CLIENT", invocationId: `${options.runId}:client`,
      appendSystemPrompt: CLIENT_TOOL_INPUT_SYSTEM_PROMPT,
      tools: ["Bash", "Skill"], allowedTools: ["Skill(problem-locator-client)", ...FULL_MCP_TOOLS, "Bash(/usr/bin/openssl:*)", "Bash(/usr/bin/stat:*)", "Bash(/usr/bin/curl:*)"],
      disallowedTools: CLIENT_DISALLOWED_TOOLS, auditOnlyAllowedTools: CLIENT_DISALLOWED_TOOLS, allowToolErrors: true,
      maxTurns: CLAUDE_DEEPSEEK_E2E_MAX_TURNS, maxBudgetUsd: CLAUDE_DEEPSEEK_E2E_USD_LIMIT, wallTimeoutSeconds: CLAUDE_DEEPSEEK_CALL_WALL_SECONDS, noProgressSeconds: CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
      mcpConfig, tracePath: path.join(evidenceRoot, "client-events.jsonl"), stderrPath: path.join(evidenceRoot, "client.stderr.txt"), receiptPath: path.join(usageRoot, "client.json"), environment: { configRoot: clientConfig, home: clientHome, temporary: clientTmp },
    }, { ambient, onProgress: () => onProgress?.("client") });
  } finally {
    clearInterval(serviceProgress);
    terminateOwnedProcess(service);
    const closed = await Promise.race([serviceClosed, wait(12_000).then(() => ({ signal: "TIMEOUT" }))]);
    serviceLogStream.end();
    requireE2E(closed.signal !== "TIMEOUT", "CLAUDE_DEEPSEEK_SERVICE_STOP_TIMEOUT", "Owned local service did not stop");
  }
  requireE2E(client !== null, "CLAUDE_DEEPSEEK_CLIENT_INCOMPLETE", "Claude MCP Client did not complete");
  const clientSkillAudit = auditClientSkillLoad(client);
  requireE2E(client.records.every((record) => !CLIENT_DISALLOWED_TOOLS.includes(record.name) || record.is_error === true), "CLAUDE_DEEPSEEK_DISALLOWED_TOOL_EXECUTED", "A Client tool removed by disallowedTools executed successfully");
  requireE2E(client.denied.every((item) => item.executed === false && (item.name === "Bash" || CLIENT_DISALLOWED_TOOLS.includes(item.name))), "CLAUDE_DEEPSEEK_DENIED_TOOL_AUDIT_INVALID", "Client denied-tool evidence contains an unexpected tool or execution");
  const partitioned = partitionMcpCalls(client.mcp);
  const recoveryAudit = auditMcpRecoveries(client.mcp);
  const bashPolicyAudit = auditBashPolicyClaims(bashClaimRoot, { expectDownload: expectArchive });
  requireE2E(recoveryAudit.recoveries.length === partitioned.recoveries.length, "CLAUDE_DEEPSEEK_RECOVERY_PROJECTION_MISMATCH", "Recovery audit differs from the MCP business-envelope partition");
  const mcpCalls = partitioned.successful;
  const prepareCall = mcpCalls.find((call) => call.tool === "problem_locator_prepare_attachment");
  const prepareData = structuredMcpData(prepareCall);
  const upload = prepareData.upload;
  requireE2E(upload?.method === "PUT" && upload.attachment_id && upload.url && isPlainObject(upload.required_headers), "CLAUDE_DEEPSEEK_UPLOAD_DESCRIPTOR_INVALID", "prepare_attachment did not return one UploadDescriptor");
  requireE2E(prepareCall.arguments.declared_size === archive.size && prepareCall.arguments.declared_sha256 === archive.sha256, "CLAUDE_DEEPSEEK_ATTACHMENT_DECLARATION_MISMATCH", "Client declaration differs from deterministic ZIP");
  requireE2E(validDescriptorUploadCommand({ commands: client.bash, upload, archivePath }), "CLAUDE_DEEPSEEK_UPLOAD_COMMAND_INVALID", "Attachment PUT command is not uniquely bound to the descriptor");
  const getCalls = mcpCalls.filter((call) => call.tool === "problem_locator_get_case");
  const finalData = structuredMcpData(getCalls.at(-1));
  const finalCase = finalData?.case_view ?? finalData;
  requireE2E(TERMINAL_CASE_STATUSES.has(finalCase.status) && finalCase.active_job === null, "CLAUDE_DEEPSEEK_FINAL_CASE_INVALID", "Final Case is not terminal or retains an active Job");
  requireE2E(finalCase.status !== "FAILED", "CLAUDE_DEEPSEEK_SERVICE_JOB_FAILED", "Service Job failed before the scenario workflow completed");
  const artifactData = structuredMcpData(mcpCalls.filter((call) => call.tool === "problem_locator_list_artifacts").at(-1));
  const listArtifactsCall = mcpCalls.filter((call) => call.tool === "problem_locator_list_artifacts").at(-1);
  const artifactAudit = artifactConsistency(finalCase, artifactData, { expectArchive });
  const userResultArtifact = artifactData.artifacts.find((artifact) => artifact.kind === "USER_RESULT");
  const archiveArtifact = artifactData.artifacts.find((artifact) => artifact.kind === "USER_RESULT_ARCHIVE") ?? null;
  const download = archiveArtifact === null ? null : { path: downloadPath, artifact: archiveArtifact };
  const bashAudit = auditClientBash(client.bash, { archivePath, archive, descriptor: upload, download });
  requireE2E(!expectArchive || client.bash[3].ordinal > listArtifactsCall.ordinal, "CLAUDE_DEEPSEEK_DOWNLOAD_ORDER_INVALID", "Artifact GET must occur after list_artifacts in the same Client model trace");
  const httpAudit = auditHttpBoundary([{ method: "POST", url: mcpUrl, source: "claude-strict-mcp" }, ...extractCommandHttpEntries(client.bash)], { mcpUrl, uploadUrl: upload.url, downloadUrl: archiveArtifact?.download_url ?? null });
  const server = stateEvidence(dataRoot, rawPaths, expectedDiagnosisResultType);
  const downloadedArchiveAudit = archiveArtifact === null ? { schema_version: 3, status: "SKIP", target_log_count: 0 } : auditDownloadedResultArchive({ filePath: downloadPath, artifact: archiveArtifact, userResultArtifact, problemTime: mapped.initial_user_fact_values[0], targetLogs: server.targetLogs });
  const submit = mcpCalls.find((call) => call.tool === "problem_locator_submit_supplement");
  const attachmentAudit = auditUploadedAttachment({ attachment: server.attachment, uploadReceipt: server.uploadReceipt, descriptor: upload, archive, submitArguments: submit?.arguments });
  const mcpAudit = auditMcpToolCalls(mcpCalls, { attachmentId: upload.attachment_id, uploadRevision: server.uploadReceipt.case_revision });
  mcpAudit.recovery_audit = recoveryAudit;
  const oracleAudit = auditOracle({ oracle, publicCase: { status: server.outcome.result_type }, sealedDiagnosis: server.enriched, evidenceSources: server.evidenceSources });
  const serverInvocations = expectedPhases.slice(1).map((phase) => JSON.parse(fs.readFileSync(path.join(serviceUsage, `${phase.toLowerCase()}.json`), "utf8")));
  const specializedRuntime = { ...auditSpecializedRuntime({ dataRoot, server, cache, facts, finalCase, serverInvocations }), helper_installation: helperIdentity };
  const invocations = [client.receipt, ...serverInvocations];
  const modelAudit = auditClaudeInvocations(invocations, { workflow: "e2e", scenarioId: options.scenario });
  assertRegistrationUnchanged(cache);
  combineServerEvents(dfxRoot, path.join(evidenceRoot, "server-events.ndjson"));
  const lifecycle = { schema_version: 1, status: "PASS", case_id: finalCase.case_id, public_case_status: finalCase.status, jobs: server.jobs.map((job) => ({ job_id: job.job_id, job_type: job.job_type, status: job.status })), wrapper_phases: serverInvocations.map((item) => item.phase), active_jobs: 0, logparse_target_count: server.targetLogs.target_logs.length };
  const attachmentReceipt = { schema_version: 1, status: "PASS", archive, descriptor: { attachment_id: upload.attachment_id, method: upload.method, url_sha256: sha256Bytes(upload.url), required_header_names: Object.keys(upload.required_headers).sort(), max_bytes: upload.max_bytes }, upload: attachmentAudit, bash: bashAudit, submitted: true };
  const security = { ...secretScan({ roots: [evidenceRoot], settings: options.claudeSettings }), client_permission_profile: clientPermissionProfile, bash_policy: bashPolicyAudit, denied_tool_attempts: client.denied };
  writeJson(path.join(evidenceRoot, "scenario-oracle.json"), oracle);
  writeJson(path.join(evidenceRoot, "methods-package.json"), { schema_version: 2, status: "PASS", producer_identity: producer.producer_identity, registration_tree_sha256: cache.manifest.registration.tree_sha256, registration_identity: cache.manifest.registration });
  writeJson(path.join(evidenceRoot, "claude-identity.json"), { schema_version: 1, status: "PASS", claude: identity, helper: helperIdentity });
  writeJson(path.join(evidenceRoot, "model-invocations.json"), { schema_version: 1, status: "PASS", retry_policy: "NONE", invocations });
  writeJson(path.join(evidenceRoot, "model-usage.json"), modelAudit);
  writeJson(path.join(evidenceRoot, "mcp-tool-calls.json"), { ...mcpAudit, tools_listing: readiness.tools, calls: mcpCalls });
  writeJson(path.join(evidenceRoot, "client-skill.json"), clientSkillAudit);
  writeJson(path.join(evidenceRoot, "attachment.json"), attachmentReceipt);
  writeJson(path.join(evidenceRoot, "server-lifecycle.json"), lifecycle);
  writeJson(path.join(evidenceRoot, "specialized-runtime.json"), specializedRuntime);
  writeJson(path.join(evidenceRoot, "server-sealed-diagnosis.json"), { schema_version: 1, diagnosis: server.diagnosis, grounding_audit: server.grounding, outcome_result_type: server.outcome.result_type, target_logs: server.targetLogs });
  writeJson(path.join(evidenceRoot, "final-case.json"), finalCase);
  writeJson(path.join(evidenceRoot, "artifact-index.json"), artifactData);
  writeJson(path.join(evidenceRoot, "artifact-download.json"), downloadedArchiveAudit);
  writeJson(path.join(evidenceRoot, "http-boundary-audit.json"), httpAudit);
  writeJson(path.join(evidenceRoot, "security-audit.json"), security);
  const gate = { schema_version: 1, status: "PASS", scenario_id: options.scenario, checks: { client_skill: clientSkillAudit.status, mcp: mcpAudit.status, bash: bashAudit.status, attachment: attachmentReceipt.status, lifecycle: lifecycle.status, artifacts: artifactAudit.status, artifact_download: downloadedArchiveAudit.status, http_boundary: httpAudit.status, oracle: oracleAudit.status, model_usage: modelAudit.status, security: security.status } };
  writeJson(path.join(evidenceRoot, "adapter-receipt.json"), gate);
  return gate;
}

export function safeE2EError(error) { return { schema_version: 1, status: "FAIL", code: error?.code ?? "CLAUDE_DEEPSEEK_E2E_RUNNER_FAILED", message: error?.message ?? String(error) }; }

export function serviceLauncherArguments(sourceRoot) {
  return ["-I", "-B", path.join(path.resolve(sourceRoot), "tools", "test-flow", "runtime-support", "test_service_launcher.py"), "serve"];
}

async function main() {
  try {
    const values = parseArguments(process.argv.slice(2));
    const options = Object.fromEntries(Object.entries({ runId: values["run-id"], sourceRoot: values["source-root"], runtimeRoot: values["runtime-root"] ?? values["source-root"], claudeEntry: values["claude-entry"], claudeSettings: values["claude-settings"], pythonEntry: values["python-entry"], logparseRoot: values["logparse-root"], cacheRoot: values["cache-root"], scenario: values.scenario, workRoot: values["work-root"], privateRoot: values["private-root"], evidenceRoot: values["evidence-root"], usageRoot: values["usage-root"] }).map(([key, value]) => [key, ["runId", "scenario"].includes(key) ? value : path.resolve(value)]));
    process.stdout.write(canonicalJson(await runE2E(options, { onProgress: (phase) => process.stdout.write(`TEST_FLOW_PROGRESS stage.progress claude-deepseek ${phase}\n`) })));
  } catch (error) { process.stderr.write(canonicalJson(safeE2EError(error))); process.exitCode = 1; }
}

if (process.argv[1] && path.resolve(process.argv[1]) === MODULE_PATH) await main();
