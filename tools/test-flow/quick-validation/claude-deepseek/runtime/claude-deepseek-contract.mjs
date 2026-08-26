import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import {
  CLAUDE_SETTINGS_ENV_KEYS,
  RELEASE_CLAUDE_CLI_SHA256,
  RELEASE_CLAUDE_VERSION,
  RELEASE_CLAUDE_VERSION_OUTPUT,
  RELEASE_MODEL,
  claudeSettingsIdentity,
  packageTreeIdentity,
  validateClaudeDistribution,
} from "../../../lib/release-inputs.mjs";
import { canonicalJson, sha256Bytes, sha256File } from "../../../lib/util.mjs";
import {
  auditFlatMcpInputSchema,
  auditHttpBoundary,
  auditListedMcpTools,
  auditMcpToolCalls,
  auditOracle,
  auditUploadedAttachment,
  buildDeterministicLogsZip,
  loadScenarioFacts,
  loadScenarioOracle,
  STANDALONE_CODEX_LUNA_SCENARIOS,
  macosCodexLunaE2EPhases,
  mapScenarioToCreateCase,
  scenarioPaths,
  writeDeterministicLogsZip,
} from "../../codex-luna/runtime/macos-codex-luna-e2e-contract.mjs";

export const CLAUDE_DEEPSEEK_CONTRACT_VERSION = 1;
export const CLAUDE_DEEPSEEK_MODEL = RELEASE_MODEL;
export const CLAUDE_DEEPSEEK_VERSION = RELEASE_CLAUDE_VERSION;
export const CLAUDE_DEEPSEEK_VERSION_OUTPUT = RELEASE_CLAUDE_VERSION_OUTPUT;
export const CLAUDE_DEEPSEEK_CLI_SHA256 = RELEASE_CLAUDE_CLI_SHA256;
export const CLAUDE_DEEPSEEK_METHODS_PROMPT_VERSION = 1;
export const CLAUDE_DEEPSEEK_CLIENT_PROMPT_VERSION = 1;
export const CLAUDE_DEEPSEEK_METHODS_CALLS = 1;
export const CLAUDE_DEEPSEEK_E2E_CALLS = 5;
export const CLAUDE_DEEPSEEK_METHODS_TOKEN_LIMIT = 1_000_000;
export const CLAUDE_DEEPSEEK_METHODS_USD_LIMIT = 10;
export const CLAUDE_DEEPSEEK_E2E_TOKEN_LIMIT = 2_000_000;
export const CLAUDE_DEEPSEEK_E2E_USD_LIMIT = 4;
export const CLAUDE_DEEPSEEK_METHODS_MAX_TURNS = 16;
export const CLAUDE_DEEPSEEK_E2E_MAX_TURNS = 50;
export const CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS = 64_000;
export const CLAUDE_DEEPSEEK_CALL_WALL_SECONDS = 600;
export const CLAUDE_DEEPSEEK_METHODS_WALL_SECONDS = 1_800;
export const CLAUDE_DEEPSEEK_STAGE_WALL_SECONDS = 1_800;
export const CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS = 300;
export const CLAUDE_DEEPSEEK_SCENARIOS = Object.freeze([...STANDALONE_CODEX_LUNA_SCENARIOS]);
export const CLAUDE_DEEPSEEK_REGISTRATION_ID = "rpc-timeout-methods-v1";
export const CLAUDE_DEEPSEEK_SKILL_NAME = "diagnose-rpc-timeout";
export const CLAUDE_DEEPSEEK_PUBLIC_TOOLS = Object.freeze([
  "problem_locator_create_case",
  "problem_locator_prepare_attachment",
  "problem_locator_submit_supplement",
  "problem_locator_get_case",
  "problem_locator_resume_case",
  "problem_locator_cancel_case",
  "problem_locator_list_artifacts",
]);
export const CLAUDE_DEEPSEEK_E2E_PHASES = Object.freeze([
  "CLIENT", "ROUTE", "LOGPARSE", "DIAGNOSE", "REVIEW",
]);
export const CLAUDE_DEEPSEEK_BASH_PROGRAMS = Object.freeze([
  "/usr/bin/openssl", "/usr/bin/stat", "/usr/bin/curl",
]);

export class ClaudeDeepseekContractError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "ClaudeDeepseekContractError";
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) {
  throw new ClaudeDeepseekContractError(code, message, details);
}

function requireContract(condition, code, message, details = {}) {
  if (!condition) fail(code, message, details);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function ordinaryFile(filePath, label) {
  let metadata;
  try { metadata = fs.lstatSync(filePath); } catch { fail("CLAUDE_DEEPSEEK_FILE_MISSING", `${label} is unavailable`); }
  requireContract(metadata.isFile() && !metadata.isSymbolicLink() && metadata.nlink === 1, "CLAUDE_DEEPSEEK_FILE_INVALID", `${label} must be one ordinary file`);
  return metadata;
}

function readJson(filePath, label) {
  ordinaryFile(filePath, label);
  let value;
  try { value = JSON.parse(fs.readFileSync(filePath, "utf8")); } catch { fail("CLAUDE_DEEPSEEK_JSON_INVALID", `${label} must be valid JSON`); }
  requireContract(isPlainObject(value), "CLAUDE_DEEPSEEK_JSON_ROOT_INVALID", `${label} must have an object root`);
  return value;
}

function treeRecords(root, current = root, output = []) {
  requireContract(path.isAbsolute(root) && fs.existsSync(root) && fs.statSync(root).isDirectory(), "CLAUDE_DEEPSEEK_TREE_ROOT_INVALID", "Tree root must be an existing absolute directory");
  for (const entry of fs.readdirSync(current, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    const absolute = path.join(current, entry.name);
    const relative = path.relative(root, absolute).split(path.sep).join("/");
    const metadata = fs.lstatSync(absolute);
    requireContract(!metadata.isSymbolicLink(), "CLAUDE_DEEPSEEK_TREE_LINK_FORBIDDEN", "Identity trees cannot contain links", { path: relative });
    if (entry.isDirectory()) {
      output.push({ path: `${relative}/`, kind: "directory", mode: metadata.mode & 0o777 });
      treeRecords(root, absolute, output);
    } else {
      requireContract(entry.isFile() && metadata.nlink === 1, "CLAUDE_DEEPSEEK_TREE_NODE_INVALID", "Identity trees may contain ordinary files only", { path: relative });
      output.push({ path: relative, kind: "file", mode: metadata.mode & 0o777, size: metadata.size, sha256: sha256File(absolute) });
    }
  }
  return output;
}

export function treeManifest(root, { directoryMode = null } = {}) {
  const records = treeRecords(path.resolve(root));
  return directoryMode === null ? records : records.map((record) => record.kind === "directory" ? { ...record, mode: directoryMode } : record);
}

export function treeDigest(root, options = {}) {
  return sha256Bytes(canonicalJson(treeManifest(root, options)));
}

export function validateClaudeDeepseekIdentity(claudeEntry, claudeSettings) {
  const distribution = validateClaudeDistribution(path.resolve(claudeEntry));
  requireContract(distribution.status === "PRESENT", "CLAUDE_DEEPSEEK_CLI_IDENTITY_INVALID", "Claude Code distribution does not match the pinned 2.1.89 cache", { code: distribution.code });
  const settings = claudeSettingsIdentity(path.resolve(claudeSettings));
  requireContract(settings.status === "PRESENT", "CLAUDE_DEEPSEEK_SETTINGS_IDENTITY_INVALID", "Claude settings do not match the audited DeepSeek allowlist", { code: settings.code });
  requireContract(settings.model === CLAUDE_DEEPSEEK_MODEL, "CLAUDE_DEEPSEEK_MODEL_IDENTITY_INVALID", "Claude settings do not bind the pinned DeepSeek model");
  return Object.freeze({
    schema_version: 1,
    status: "PASS",
    cli: {
      version: distribution.version,
      package_version: distribution.package_version,
      cli_sha256: distribution.cli_sha256,
      package_manifest_sha256: distribution.package_manifest_sha256,
      package_tree_digest: distribution.package_tree_digest,
      tarball_sha256: distribution.tarball_sha256,
      platform: process.platform,
      architecture: process.arch,
    },
    settings: {
      fingerprint: settings.fingerprint,
      endpoint: settings.endpoint,
      model: settings.model,
      copied_env_key_count: settings.copied_env_key_count,
      env_key_names: [...CLAUDE_SETTINGS_ENV_KEYS].sort(),
      hooks_copied: false,
      secrets_persisted: false,
    },
    model: CLAUDE_DEEPSEEK_MODEL,
    max_output_tokens: CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS,
  });
}

export function buildMethodsProducerIdentity({ wiki, metaSkillRoot, registrationTemplate, claudeIdentity }) {
  ordinaryFile(wiki, "canonical Wiki");
  ordinaryFile(registrationTemplate, "registration template");
  const outputContract = path.join(metaSkillRoot, "references", "output-contract.md");
  const validator = path.join(metaSkillRoot, "scripts", "validate_generated_skill.py");
  ordinaryFile(outputContract, "Methods output contract");
  ordinaryFile(validator, "Methods validator");
  requireContract(claudeIdentity?.status === "PASS", "CLAUDE_DEEPSEEK_IDENTITY_INVALID", "Claude identity must be validated before building a producer identity");
  const inputs = {
    schema_version: 1,
    contract_version: CLAUDE_DEEPSEEK_CONTRACT_VERSION,
    wiki: { sha256: sha256File(wiki), size: fs.statSync(wiki).size },
    meta_skill: { tree_sha256: treeDigest(metaSkillRoot, { directoryMode: 0o700 }) },
    output_contract: { sha256: sha256File(outputContract), size: fs.statSync(outputContract).size },
    validator: { sha256: sha256File(validator), size: fs.statSync(validator).size },
    registration_template: { sha256: sha256File(registrationTemplate), size: fs.statSync(registrationTemplate).size },
    claude: {
      version: claudeIdentity.cli.version,
      cli_sha256: claudeIdentity.cli.cli_sha256,
      package_tree_digest: claudeIdentity.cli.package_tree_digest,
      settings_fingerprint: claudeIdentity.settings.fingerprint,
      platform: claudeIdentity.cli.platform,
      architecture: claudeIdentity.cli.architecture,
      model: CLAUDE_DEEPSEEK_MODEL,
      max_output_tokens: CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS,
    },
    generation_prompt_version: CLAUDE_DEEPSEEK_METHODS_PROMPT_VERSION,
    runner_contract: "claude-deepseek-methods-bootstrap-v1",
  };
  return Object.freeze({ schema_version: 1, producer_identity: sha256Bytes(canonicalJson(inputs)), inputs });
}

export function methodsCachePath(cacheRoot, producerIdentity) {
  requireContract(path.isAbsolute(cacheRoot), "CLAUDE_DEEPSEEK_CACHE_ROOT_INVALID", "Methods cache root must be absolute");
  requireContract(/^[a-f0-9]{64}$/.test(producerIdentity), "CLAUDE_DEEPSEEK_PRODUCER_IDENTITY_INVALID", "Producer identity must be SHA-256");
  return path.join(cacheRoot, "claude-deepseek-methods", producerIdentity);
}

export function buildMethodsCacheManifest({ producer, packageRoot, registrationTemplate }) {
  requireContract(/^[a-f0-9]{64}$/.test(producer?.producer_identity ?? ""), "CLAUDE_DEEPSEEK_PRODUCER_IDENTITY_INVALID", "Producer identity is invalid");
  const registration = readJson(registrationTemplate, "registration template");
  requireContract(registration.registration_id === CLAUDE_DEEPSEEK_REGISTRATION_ID && registration.package?.skill_name === CLAUDE_DEEPSEEK_SKILL_NAME, "CLAUDE_DEEPSEEK_REGISTRATION_INVALID", "Registration does not bind the expected Methods package");
  const files = treeManifest(packageRoot);
  requireContract(files.length > 0, "CLAUDE_DEEPSEEK_METHODS_PACKAGE_EMPTY", "Methods package is empty");
  return {
    schema_version: 1,
    producer,
    package: { skill_name: CLAUDE_DEEPSEEK_SKILL_NAME, tree_sha256: sha256Bytes(canonicalJson(files)), files },
    registration: { registration_id: CLAUDE_DEEPSEEK_REGISTRATION_ID, template_sha256: sha256File(registrationTemplate) },
    publish: { strategy: "staging-directory-atomic-rename", collision: "byte-identical-only" },
  };
}

export function validateMethodsCache({ cacheRoot, producer, registrationTemplate }) {
  const root = methodsCachePath(cacheRoot, producer.producer_identity);
  const manifest = readJson(path.join(root, "manifest.json"), "Methods cache manifest");
  const packageRoot = path.join(root, "package", CLAUDE_DEEPSEEK_SKILL_NAME);
  const expected = buildMethodsCacheManifest({ producer, packageRoot, registrationTemplate });
  requireContract(canonicalJson(manifest) === canonicalJson(expected), "CLAUDE_DEEPSEEK_METHODS_CACHE_IDENTITY_MISMATCH", "Methods cache, package bytes, or producer identity drifted");
  return Object.freeze({ schema_version: 1, status: "PASS", root, package_root: packageRoot, manifest });
}

export function assertMethodsPackageUnchanged(cacheReceipt) {
  requireContract(cacheReceipt?.status === "PASS", "CLAUDE_DEEPSEEK_METHODS_CACHE_RECEIPT_INVALID", "Methods cache receipt is invalid");
  const current = treeDigest(cacheReceipt.package_root);
  requireContract(current === cacheReceipt.manifest.package.tree_sha256, "CLAUDE_DEEPSEEK_METHODS_PACKAGE_DRIFT", "Frozen Methods package changed");
  return { schema_version: 1, status: "PASS", tree_sha256: current };
}

function normalizedUsage(value) {
  const fields = ["input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"];
  requireContract(isPlainObject(value) && fields.every((key) => Number.isSafeInteger(value[key]) && value[key] >= 0), "CLAUDE_DEEPSEEK_TERMINAL_USAGE_INVALID", "Claude terminal usage is incomplete");
  requireContract(Number.isFinite(value.cost_usd) && value.cost_usd >= 0, "CLAUDE_DEEPSEEK_TERMINAL_USAGE_INVALID", "Claude terminal cost is incomplete");
  return { schema_version: 1, ...Object.fromEntries(fields.map((key) => [key, value[key]])), total_tokens: fields.reduce((sum, key) => sum + value[key], 0), cost_usd: Math.round(value.cost_usd * 1_000_000) / 1_000_000 };
}

export function aggregateClaudeUsage(invocations) {
  requireContract(Array.isArray(invocations), "CLAUDE_DEEPSEEK_INVOCATIONS_INVALID", "Invocation ledger must be an array");
  const aggregate = { input_tokens: 0, output_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, total_tokens: 0, cost_usd: 0 };
  for (const invocation of invocations) {
    const usage = normalizedUsage(invocation?.usage);
    for (const key of ["input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "total_tokens"]) aggregate[key] += usage[key];
    aggregate.cost_usd += usage.cost_usd;
  }
  aggregate.cost_usd = Math.round(aggregate.cost_usd * 1_000_000) / 1_000_000;
  return aggregate;
}

export function claudeDeepseekE2EPhases(scenarioId) {
  requireContract(CLAUDE_DEEPSEEK_SCENARIOS.includes(scenarioId), "CLAUDE_DEEPSEEK_SCENARIO_INVALID", "Scenario is outside the repository-owned matrix", { scenario_id: scenarioId });
  return macosCodexLunaE2EPhases(scenarioId);
}

export function claudeDeepseekE2ECallCount(scenarioId) {
  return claudeDeepseekE2EPhases(scenarioId).length;
}

export function auditClaudeInvocations(invocations, { workflow, scenarioId = null }) {
  const phases = workflow === "methods" ? ["METHODS_BOOTSTRAP"] : claudeDeepseekE2EPhases(scenarioId);
  const tokenLimit = workflow === "methods" ? CLAUDE_DEEPSEEK_METHODS_TOKEN_LIMIT : CLAUDE_DEEPSEEK_E2E_TOKEN_LIMIT;
  const costLimit = workflow === "methods" ? CLAUDE_DEEPSEEK_METHODS_USD_LIMIT : CLAUDE_DEEPSEEK_E2E_USD_LIMIT;
  const turnLimit = workflow === "methods" ? CLAUDE_DEEPSEEK_METHODS_MAX_TURNS : CLAUDE_DEEPSEEK_E2E_MAX_TURNS;
  requireContract(Array.isArray(invocations) && invocations.length === phases.length, "CLAUDE_DEEPSEEK_INVOCATION_COUNT_INVALID", "Claude process cardinality drifted", { expected: phases.length, actual: invocations?.length ?? null });
  for (const [index, item] of invocations.entries()) {
    requireContract(
      item.phase === phases[index]
      && item.model === CLAUDE_DEEPSEEK_MODEL
      && item.attempt === 1
      && item.retry === 0
      && item.status === "PASS"
      && item.terminal === true
      && Number.isSafeInteger(item.turns)
      && item.turns > 0
      && item.turns <= turnLimit
      && item.wall_timeout_seconds === (workflow === "methods" ? CLAUDE_DEEPSEEK_METHODS_WALL_SECONDS : CLAUDE_DEEPSEEK_CALL_WALL_SECONDS)
      && Date.parse(item.finished_at_utc) >= Date.parse(item.started_at_utc),
      "CLAUDE_DEEPSEEK_INVOCATION_IDENTITY_INVALID",
      "Claude phase, identity, retry, terminal, turn, timeout, or timestamp contract drifted",
      { phase: item?.phase ?? null },
    );
    normalizedUsage(item.usage);
  }
  const aggregate = aggregateClaudeUsage(invocations);
  requireContract(aggregate.total_tokens <= tokenLimit && aggregate.cost_usd <= costLimit, "CLAUDE_DEEPSEEK_BUDGET_EXCEEDED", "Aggregate Claude usage exceeded the Goal cap", { token_limit: tokenLimit, cost_limit: costLimit });
  return { schema_version: 1, status: "PASS", workflow, expected_phases: phases, retry_count: 0, token_formula: "input_tokens+output_tokens+cache_creation_input_tokens+cache_read_input_tokens", aggregate };
}

function streamToolUses(events) {
  const uses = [];
  const visit = (value) => {
    if (Array.isArray(value)) value.forEach(visit);
    else if (isPlainObject(value)) {
      if (value.type === "tool_use" && typeof value.name === "string") uses.push({ id: value.id ?? null, name: value.name, input: value.input ?? null });
      Object.values(value).forEach(visit);
    }
  };
  events.forEach(visit);
  return uses;
}

export function auditClaudeStream(events, { phase, allowedTools, maxTurns, wallTimeoutSeconds }) {
  requireContract(Array.isArray(events) && events.length >= 2, "CLAUDE_DEEPSEEK_STREAM_INVALID", "Claude stream must be a non-empty JSONL event list");
  const init = events.filter((item) => item?.type === "system" && item?.subtype === "init");
  const terminal = events.filter((item) => item?.type === "result");
  requireContract(init.length === 1 && terminal.length === 1 && events.at(-1) === terminal[0], "CLAUDE_DEEPSEEK_STREAM_TERMINAL_INVALID", "Claude stream must contain one init and one terminal result");
  const result = terminal[0];
  requireContract(init[0].model === CLAUDE_DEEPSEEK_MODEL && result.subtype === "success" && result.is_error === false, "CLAUDE_DEEPSEEK_STREAM_MODEL_INVALID", "Claude stream model or terminal status drifted");
  requireContract(Number.isSafeInteger(result.num_turns) && result.num_turns > 0 && result.num_turns <= maxTurns, "CLAUDE_DEEPSEEK_STREAM_TURNS_INVALID", "Claude terminal turn count exceeded the cap");
  const tools = streamToolUses(events);
  const allowed = new Set(allowedTools);
  const deniedToolIds = new Set();
  const visitDenied = (value) => {
    if (Array.isArray(value)) value.forEach(visitDenied);
    else if (isPlainObject(value)) {
      if (value.type === "tool_result" && value.is_error === true && typeof value.tool_use_id === "string") deniedToolIds.add(value.tool_use_id);
      Object.values(value).forEach(visitDenied);
    }
  };
  events.forEach(visitDenied);
  requireContract(tools.every((item) => allowed.has(item.name) || deniedToolIds.has(item.id)), "CLAUDE_DEEPSEEK_STREAM_TOOL_FORBIDDEN", "Claude used a tool outside the phase allowlist");
  const usage = normalizedUsage({
    input_tokens: result.usage?.input_tokens,
    output_tokens: result.usage?.output_tokens,
    cache_creation_input_tokens: result.usage?.cache_creation_input_tokens,
    cache_read_input_tokens: result.usage?.cache_read_input_tokens,
    cost_usd: result.total_cost_usd ?? result.cost_usd,
  });
  return { schema_version: 1, status: "PASS", phase, model: init[0].model, turns: result.num_turns, wall_timeout_seconds: wallTimeoutSeconds, usage, tools };
}

function shellWords(command) {
  requireContract(typeof command === "string" && command.length > 0 && !/[\n\r\0;&|`]/.test(command), "CLAUDE_DEEPSEEK_BASH_SYNTAX_FORBIDDEN", "Bash command contains chaining, substitution, or multiple lines");
  const words = command.match(/(?:[^\s'\"]+|'[^']*'|\"[^\"]*\")+/g) ?? [];
  return words.map((word) => ((word.startsWith("'") && word.endsWith("'")) || (word.startsWith('"') && word.endsWith('"'))) ? word.slice(1, -1) : word);
}

export function auditClientBash(commands, { archivePath, archive, descriptor }) {
  requireContract(Array.isArray(commands), "CLAUDE_DEEPSEEK_BASH_LEDGER_INVALID", "Bash ledger must be an array");
  requireContract(commands.length === 3, "CLAUDE_DEEPSEEK_BASH_CARDINALITY_INVALID", "Client must run exactly openssl, stat, and one curl PUT");
  const parsed = commands.map((entry) => ({ ...entry, words: shellWords(entry.command) }));
  requireContract(parsed.every((entry) => entry.status === "completed" && entry.exit_code === 0), "CLAUDE_DEEPSEEK_BASH_RESULT_INVALID", "Every allowed Bash command must complete successfully");
  requireContract(parsed[0].words[0] === "/usr/bin/openssl" && parsed[0].words[1] === "dgst" && parsed[0].words[2] === "-sha256" && parsed[0].words[3] === archivePath && parsed[0].words.length === 4, "CLAUDE_DEEPSEEK_OPENSSL_COMMAND_INVALID", "openssl command is not the exact digest command");
  requireContract(parsed[1].words[0] === "/usr/bin/stat" && parsed[1].words[1] === "-f" && parsed[1].words[2] === "%z" && parsed[1].words[3] === archivePath && parsed[1].words.length === 4, "CLAUDE_DEEPSEEK_STAT_COMMAND_INVALID", "stat command is not the exact size command");
  const curl = parsed[2];
  requireContract(curl.words[0] === "/usr/bin/curl" && curl.words.includes("PUT") && curl.words.includes(descriptor.url) && curl.words.includes(archivePath), "CLAUDE_DEEPSEEK_CURL_COMMAND_INVALID", "curl command is not bound to the UploadDescriptor and archive");
  requireContract((curl.words.filter((word) => word === "-H" || word === "--header").length === Object.keys(descriptor.required_headers).length), "CLAUDE_DEEPSEEK_CURL_HEADERS_INVALID", "curl must carry each and only each required upload header");
  requireContract(String(parsed[0].stdout ?? "").toLowerCase().includes(archive.sha256) && String(parsed[1].stdout ?? "").trim() === String(archive.size), "CLAUDE_DEEPSEEK_ATTACHMENT_PRECHECK_INVALID", "openssl/stat receipts do not match the deterministic ZIP");
  return { schema_version: 1, status: "PASS", command_count: 3, programs: parsed.map((entry) => entry.words[0]), upload_count: 1 };
}

export function publishMethodsCacheAtomically({ cacheRoot, producer, packageRoot, registrationTemplate, stagingRoot }) {
  const destination = methodsCachePath(cacheRoot, producer.producer_identity);
  const manifest = buildMethodsCacheManifest({ producer, packageRoot, registrationTemplate });
  const stage = path.resolve(stagingRoot);
  requireContract(!fs.existsSync(stage), "CLAUDE_DEEPSEEK_CACHE_STAGING_EXISTS", "Cache staging directory already exists");
  fs.mkdirSync(path.join(stage, "package"), { recursive: true, mode: 0o700 });
  fs.cpSync(packageRoot, path.join(stage, "package", CLAUDE_DEEPSEEK_SKILL_NAME), { recursive: true, errorOnExist: true, force: false });
  fs.writeFileSync(path.join(stage, "manifest.json"), canonicalJson(manifest), { encoding: "utf8", mode: 0o600, flag: "wx" });
  fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 });
  try { fs.renameSync(stage, destination); } catch (error) {
    if (error.code !== "EEXIST" && error.code !== "ENOTEMPTY") throw error;
    const existing = validateMethodsCache({ cacheRoot, producer, registrationTemplate });
    requireContract(canonicalJson(existing.manifest) === canonicalJson(manifest), "CLAUDE_DEEPSEEK_CACHE_COLLISION", "Existing producer identity contains different bytes");
    fs.rmSync(stage, { recursive: true });
    return { ...existing, published: false, collision: "byte-identical" };
  }
  const receipt = validateMethodsCache({ cacheRoot, producer, registrationTemplate });
  return { ...receipt, published: true, collision: null };
}

export {
  auditFlatMcpInputSchema,
  auditHttpBoundary,
  auditListedMcpTools,
  auditMcpToolCalls,
  auditOracle,
  auditUploadedAttachment,
  buildDeterministicLogsZip,
  loadScenarioFacts,
  loadScenarioOracle,
  mapScenarioToCreateCase,
  packageTreeIdentity,
  scenarioPaths,
  writeDeterministicLogsZip,
};
