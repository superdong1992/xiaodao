import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

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
  ISOLATED_AGENT_ENV_POLICY_VERSION,
  validEnvironmentKeySummary,
} from "../../../runtime-support/isolated-agent-env.mjs";
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
  macosCodexLunaE2EPhases,
  mapScenarioToCreateCase as mapBaseScenarioToCreateCase,
  scenarioPaths,
  writeDeterministicLogsZip,
} from "../../codex-luna/runtime/macos-codex-luna-e2e-contract.mjs";

const RUNTIME_ROOT = path.dirname(fileURLToPath(import.meta.url));

export const CLAUDE_DEEPSEEK_CONTRACT_VERSION = 3;
export const CLAUDE_DEEPSEEK_MODEL = RELEASE_MODEL;
export const CLAUDE_DEEPSEEK_VERSION = RELEASE_CLAUDE_VERSION;
export const CLAUDE_DEEPSEEK_VERSION_OUTPUT = RELEASE_CLAUDE_VERSION_OUTPUT;
export const CLAUDE_DEEPSEEK_CLI_SHA256 = RELEASE_CLAUDE_CLI_SHA256;
export const CLAUDE_DEEPSEEK_METHODS_PROMPT_VERSION = 3;
export const CLAUDE_DEEPSEEK_CLIENT_PROMPT_VERSION = 3;
export const CLAUDE_DEEPSEEK_METHODS_CALLS = 1;
export const CLAUDE_DEEPSEEK_E2E_CALLS = 5;
export const CLAUDE_DEEPSEEK_MODEL_CERT_NORMAL_CALLS = 1;
export const CLAUDE_DEEPSEEK_MODEL_CERT_MAX_CALLS = 2;
export const CLAUDE_DEEPSEEK_BLIND_REVIEW_MODEL_CERT_NORMAL_CALLS = 2;
export const CLAUDE_DEEPSEEK_BLIND_REVIEW_MODEL_CERT_MAX_CALLS = 4;
export const CLAUDE_DEEPSEEK_MODEL_CERT_SCENARIO = "multiple-rpc-timeouts";
export const CLAUDE_DEEPSEEK_MODEL_CERT_PHASES = Object.freeze(["SPECIALIST"]);
export const CLAUDE_DEEPSEEK_BLIND_REVIEW_MODEL_CERT_PHASES = Object.freeze([
  "SPECIALIST",
  "REVIEWER",
]);
export const CLAUDE_DEEPSEEK_METHODS_TOKEN_LIMIT = 1_000_000;
export const CLAUDE_DEEPSEEK_METHODS_USD_LIMIT = 10;
export const CLAUDE_DEEPSEEK_E2E_TOKEN_LIMIT = 2_000_000;
export const CLAUDE_DEEPSEEK_E2E_USD_LIMIT = 4;
export const CLAUDE_DEEPSEEK_MODEL_CERT_ROLE_POOL_USD = CLAUDE_DEEPSEEK_E2E_USD_LIMIT / CLAUDE_DEEPSEEK_BLIND_REVIEW_MODEL_CERT_NORMAL_CALLS;
export const CLAUDE_DEEPSEEK_MODEL_CERT_BUDGET_ENFORCEMENT = "claude-cli-threshold+terminal-posthoc-release-cap";
export const CLAUDE_DEEPSEEK_METHODS_MAX_TURNS = 16;
export const CLAUDE_DEEPSEEK_E2E_MAX_TURNS = 50;
export const CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS = 64_000;
export const CLAUDE_DEEPSEEK_CALL_WALL_SECONDS = 600;
export const CLAUDE_DEEPSEEK_METHODS_WALL_SECONDS = 1_800;
export const CLAUDE_DEEPSEEK_STAGE_WALL_SECONDS = 2_700;
export const CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS = 300;
export const CLAUDE_DEEPSEEK_SCENARIOS = Object.freeze([CLAUDE_DEEPSEEK_MODEL_CERT_SCENARIO]);
export const CLAUDE_DEEPSEEK_REGISTRATION_ID = "rpc-timeout-methods-v1";
export const CLAUDE_DEEPSEEK_SKILL_NAME = "diagnose-rpc-timeout";
export const CLAUDE_DEEPSEEK_MODULE = "rpc";
export const CLAUDE_DEEPSEEK_PUBLIC_TOOLS = Object.freeze([]);
export const CLAUDE_DEEPSEEK_E2E_PHASES = Object.freeze([
  "CLIENT", "ROUTE", "LOGPARSE", "DIAGNOSE", "REVIEW",
]);
export const CLAUDE_DEEPSEEK_BASH_PROGRAMS = Object.freeze([]);

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

function exactKeys(value, keys) {
  return isPlainObject(value)
    && canonicalJson(Object.keys(value).sort()) === canonicalJson([...keys].sort());
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

function productTreeDigest(packageRoot) {
  const entries = treeManifest(packageRoot)
    .filter((item) => item.kind === "file" && item.path !== ".DS_Store" && !item.path.endsWith(".pyc") && !item.path.split("/").includes("__pycache__") && !item.path.split("/").includes(".pytest_cache") && item.path !== ".managed" && !path.posix.basename(item.path).startsWith(".managed.") && item.path !== ".codex-managed")
    .map(({ path: relative, size, sha256 }) => ({ path: relative, size, sha256 }))
    .sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  return sha256Bytes(canonicalJson({ version: 1, entries }));
}

function registrationRuntimeRef(registrationRoot, registration) {
  const registrationSha256 = sha256File(path.join(registrationRoot, "registration-template.json"));
  const packageRoot = path.join(registrationRoot, "package", registration.package.skill_name);
  const packageTreeSha256 = productTreeDigest(packageRoot);
  return {
    id: `diagnosis-skill/${registration.registration_id}`,
    version: registration.version,
    content_hash: sha256Bytes(canonicalJson({
      schema_version: 1,
      registration_id: registration.registration_id,
      registration_sha256: registrationSha256,
      package_tree_sha256: packageTreeSha256,
    })),
  };
}

export function validateRegistrationRoot(registrationRoot, { module = CLAUDE_DEEPSEEK_MODULE } = {}) {
  const root = path.resolve(registrationRoot);
  requireContract(fs.existsSync(root) && fs.statSync(root).isDirectory(), "CLAUDE_DEEPSEEK_REGISTRATION_ROOT_INVALID", "Generated registration root is unavailable");
  const rootEntries = fs.readdirSync(root, { withFileTypes: true });
  requireContract(rootEntries.length === 2 && rootEntries.some((item) => item.name === "registration-template.json" && item.isFile()) && rootEntries.some((item) => item.name === "package" && item.isDirectory()), "CLAUDE_DEEPSEEK_REGISTRATION_TREE_INVALID", "Registration root must contain exactly registration-template.json and package");
  const registration = readJson(path.join(root, "registration-template.json"), "generated registration template");
  requireContract(registration.schema_version === 1 && registration.registration_id === path.basename(root) && registration.registration_id === CLAUDE_DEEPSEEK_REGISTRATION_ID && registration.version === "1.0.0" && registration.deployment_scope === "PRODUCTION", "CLAUDE_DEEPSEEK_REGISTRATION_INVALID", "Generated registration identity is invalid");
  requireContract(registration.package?.relative_path === `package/${CLAUDE_DEEPSEEK_SKILL_NAME}` && registration.package?.skill_name === CLAUDE_DEEPSEEK_SKILL_NAME, "CLAUDE_DEEPSEEK_REGISTRATION_PACKAGE_INVALID", "Generated registration package binding is invalid");
  const packageParent = path.join(root, "package");
  const packageChildren = fs.readdirSync(packageParent, { withFileTypes: true });
  requireContract(packageChildren.length === 1 && packageChildren[0].isDirectory() && packageChildren[0].name === CLAUDE_DEEPSEEK_SKILL_NAME, "CLAUDE_DEEPSEEK_REGISTRATION_PACKAGE_INVALID", "Registration package must contain the generated Skill exactly once");
  const packageRoot = path.join(packageParent, CLAUDE_DEEPSEEK_SKILL_NAME);
  const methods = readJson(path.join(packageRoot, "methods.json"), "generated methods manifest");
  const requiredPrefix = ["problem_time", "client_slot", "client_process_name", "server_slot", "server_process_name", "client_pid", "server_pid"];
  requireContract(requiredPrefix.every((name, index) => methods.required_user_inputs?.[index] === name) && methods.required_artifacts?.includes("log_archive"), "CLAUDE_DEEPSEEK_METHODS_INPUTS_INVALID", "Generated Methods inputs do not bind the fixed dual-end anchors");
  const preprocessing = registration.runtime?.preprocessing;
  const anchors = preprocessing?.logparse_plan?.anchors;
  const expectedAnchors = [
    { label: "client", slot: "client_slot", process: "client_process_name", pid: "client_pid" },
    { label: "server", slot: "server_slot", process: "server_process_name", pid: "server_pid" },
  ];
  requireContract(preprocessing?.requires_logparse === true && preprocessing?.logparse_product === "default" && preprocessing?.logparse_plan?.attachment_requirement === "log_archive" && preprocessing?.logparse_plan?.problem_time_binding?.source === "USER_FACT" && preprocessing.logparse_plan.problem_time_binding.name === "problem_time" && Array.isArray(anchors) && anchors.length === 2, "CLAUDE_DEEPSEEK_PREPROCESSING_INVALID", "Generated registration Logparse preprocessing contract is invalid");
  for (const [index, expected] of expectedAnchors.entries()) {
    const anchor = anchors[index];
    requireContract(anchor?.label === expected.label && anchor.module?.source === "SKILL_FIXED" && anchor.module.value === module && anchor.slot?.source === "USER_FACT" && anchor.slot.name === expected.slot && anchor.process_name?.source === "USER_FACT" && anchor.process_name.name === expected.process && anchor.pid?.source === "USER_FACT" && anchor.pid.name === expected.pid, "CLAUDE_DEEPSEEK_ANCHOR_BINDING_INVALID", "Generated registration anchor binding drifted", { label: expected.label });
  }
  const skillText = fs.readFileSync(path.join(packageRoot, "SKILL.md"), "utf8");
  const invokesHelper = skillText.split(/\r?\n/u).some((line) => /logparse-diagnose/iu.test(line) && /(?:调用|加载|使用|执行|invoke|load|use|call|run)/iu.test(line) && !/(?:不|不得|禁止|不要|无需|do not|must not|never)/iu.test(line));
  const directBroker = skillText.split(/\r?\n/u).some((line) => /^\s*`?problem-locator-logparse(?:\s|`|$)/u.test(line));
  requireContract(!invokesHelper && !directBroker, "CLAUDE_DEEPSEEK_GENERATED_SKILL_PREPROCESSING_FORBIDDEN", "Generated Methods Skill must consume frozen inputs and must not invoke the Helper or broker");
  return Object.freeze({ root, registration, package_root: packageRoot, runtime_ref: registrationRuntimeRef(root, registration) });
}

export function buildRegistrationProducerIdentity({ wiki, metaSkillRoot, claudeIdentity, module = CLAUDE_DEEPSEEK_MODULE }) {
  ordinaryFile(wiki, "canonical Wiki");
  const outputContract = path.join(metaSkillRoot, "references", "output-contract.md");
  const validator = path.join(metaSkillRoot, "scripts", "validate_generated_skill.py");
  const runner = path.join(RUNTIME_ROOT, "claude-deepseek-methods-runner.mjs");
  ordinaryFile(outputContract, "Methods output contract");
  ordinaryFile(validator, "Methods validator");
  ordinaryFile(runner, "registration generation runner");
  requireContract(module === CLAUDE_DEEPSEEK_MODULE, "CLAUDE_DEEPSEEK_MODULE_INVALID", "Registration generation module must be rpc");
  requireContract(claudeIdentity?.status === "PASS", "CLAUDE_DEEPSEEK_IDENTITY_INVALID", "Claude identity must be validated before building a producer identity");
  const inputs = {
    schema_version: 1,
    contract_version: CLAUDE_DEEPSEEK_CONTRACT_VERSION,
    registration_id: CLAUDE_DEEPSEEK_REGISTRATION_ID,
    skill_name: CLAUDE_DEEPSEEK_SKILL_NAME,
    module,
    wiki: { sha256: sha256File(wiki), size: fs.statSync(wiki).size },
    meta_skill: { tree_sha256: treeDigest(metaSkillRoot, { directoryMode: 0o700 }) },
    output_contract: { sha256: sha256File(outputContract), size: fs.statSync(outputContract).size },
    validator: { sha256: sha256File(validator), size: fs.statSync(validator).size },
    source_identity: { schema_version: 2, log_template_extraction_version: 2 },
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
    runner: { contract: "claude-deepseek-registration-generation-v2", sha256: sha256File(runner), size: fs.statSync(runner).size },
  };
  return Object.freeze({ schema_version: 1, producer_identity: sha256Bytes(canonicalJson(inputs)), inputs });
}

export function registrationCachePath(cacheRoot, producerIdentity) {
  requireContract(path.isAbsolute(cacheRoot), "CLAUDE_DEEPSEEK_CACHE_ROOT_INVALID", "Registration cache root must be absolute");
  requireContract(/^[a-f0-9]{64}$/.test(producerIdentity), "CLAUDE_DEEPSEEK_PRODUCER_IDENTITY_INVALID", "Producer identity must be SHA-256");
  return path.join(cacheRoot, "claude-deepseek-registration", producerIdentity);
}

export function buildRegistrationCacheManifest({ producer, registrationRoot }) {
  requireContract(/^[a-f0-9]{64}$/.test(producer?.producer_identity ?? ""), "CLAUDE_DEEPSEEK_PRODUCER_IDENTITY_INVALID", "Producer identity is invalid");
  const validated = validateRegistrationRoot(registrationRoot, { module: producer.inputs.module });
  requireContract(validated.registration.package.source_wiki_sha256 === producer.inputs.wiki.sha256, "CLAUDE_DEEPSEEK_REGISTRATION_SOURCE_MISMATCH", "Generated registration does not bind the producer Wiki bytes");
  const files = treeManifest(validated.root);
  requireContract(files.length > 0, "CLAUDE_DEEPSEEK_REGISTRATION_EMPTY", "Generated registration is empty");
  return {
    schema_version: 1,
    producer,
    registration: {
      registration_id: CLAUDE_DEEPSEEK_REGISTRATION_ID,
      skill_name: CLAUDE_DEEPSEEK_SKILL_NAME,
      tree_sha256: sha256Bytes(canonicalJson(files)),
      files,
      template_sha256: sha256File(path.join(validated.root, "registration-template.json")),
      package_tree_sha256: productTreeDigest(validated.package_root),
      runtime_ref: validated.runtime_ref,
    },
    publish: { strategy: "staging-directory-atomic-rename", collision: "byte-identical-only" },
  };
}

export function validateRegistrationCache({ cacheRoot, producer }) {
  const root = registrationCachePath(cacheRoot, producer.producer_identity);
  const manifest = readJson(path.join(root, "manifest.json"), "registration cache manifest");
  const registrationRoot = path.join(root, "registration", CLAUDE_DEEPSEEK_REGISTRATION_ID);
  const expected = buildRegistrationCacheManifest({ producer, registrationRoot });
  requireContract(canonicalJson(manifest) === canonicalJson(expected), "CLAUDE_DEEPSEEK_REGISTRATION_CACHE_IDENTITY_MISMATCH", "Registration cache, generated bytes, or producer identity drifted");
  return Object.freeze({ schema_version: 1, status: "PASS", root, registration_root: registrationRoot, package_root: path.join(registrationRoot, "package", CLAUDE_DEEPSEEK_SKILL_NAME), manifest });
}

export function assertRegistrationUnchanged(cacheReceipt) {
  requireContract(cacheReceipt?.status === "PASS", "CLAUDE_DEEPSEEK_REGISTRATION_CACHE_RECEIPT_INVALID", "Registration cache receipt is invalid");
  const current = treeDigest(cacheReceipt.registration_root);
  requireContract(current === cacheReceipt.manifest.registration.tree_sha256, "CLAUDE_DEEPSEEK_REGISTRATION_DRIFT", "Frozen generated registration changed");
  return { schema_version: 1, status: "PASS", tree_sha256: current };
}

const CLAUDE_TERMINAL_USAGE_FIELDS = ["input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"];

function terminalUsageCandidate(values, costUsd) {
  if (!CLAUDE_TERMINAL_USAGE_FIELDS.every((field) => Number.isSafeInteger(values[field]) && values[field] >= 0)
    || !Number.isFinite(costUsd)
    || costUsd < 0) return null;
  return {
    schema_version: 1,
    ...Object.fromEntries(CLAUDE_TERMINAL_USAGE_FIELDS.map((field) => [field, values[field]])),
    total_tokens: CLAUDE_TERMINAL_USAGE_FIELDS.reduce((sum, field) => sum + values[field], 0),
    cost_usd: Math.round(costUsd * 1_000_000) / 1_000_000,
  };
}

function topLevelTerminalCost(terminal) {
  const values = ["total_cost_usd", "cost_usd"]
    .filter((field) => Object.hasOwn(terminal, field))
    .map((field) => terminal[field]);
  if (values.length === 0) return { present: false, cost_usd: null };
  if (values.some((value) => !Number.isFinite(value) || value < 0)) return { present: true, cost_usd: null };
  const costs = values.map((value) => Math.round(value * 1_000_000) / 1_000_000);
  if (new Set(costs).size !== 1) return { present: true, cost_usd: null };
  return { present: true, cost_usd: costs[0] };
}

function modelTerminalUsage(terminal) {
  if (terminal.modelUsage === undefined) return { present: false, usage: null };
  if (!isPlainObject(terminal.modelUsage)) return { present: true, usage: null };
  const entries = Object.entries(terminal.modelUsage);
  if (entries.length !== 1 || entries[0][0] !== CLAUDE_DEEPSEEK_MODEL || !isPlainObject(entries[0][1])) return { present: true, usage: null };
  const value = entries[0][1];
  return {
    present: true,
    usage: terminalUsageCandidate({
      input_tokens: value.inputTokens,
      output_tokens: value.outputTokens,
      cache_creation_input_tokens: value.cacheCreationInputTokens,
      cache_read_input_tokens: value.cacheReadInputTokens,
    }, value.costUSD),
  };
}

export function resolvedTerminalUsage(terminal) {
  if (!isPlainObject(terminal)) return null;
  const topLevelCost = topLevelTerminalCost(terminal);
  const topLevel = terminalUsageCandidate(
    Object.fromEntries(CLAUDE_TERMINAL_USAGE_FIELDS.map((field) => [field, terminal.usage?.[field]])),
    topLevelCost.cost_usd,
  );
  const model = modelTerminalUsage(terminal);
  if (!model.present) return topLevel;
  if (model.usage === null) return null;
  if (topLevelCost.present && (topLevelCost.cost_usd === null || topLevelCost.cost_usd !== model.usage.cost_usd)) return null;
  if (topLevel === null || canonicalJson(topLevel) === canonicalJson(model.usage)) return model.usage;
  const costsMatch = topLevel.cost_usd === model.usage.cost_usd;
  if (costsMatch && topLevel.total_tokens === 0 && model.usage.total_tokens > 0) return model.usage;
  if (costsMatch && model.usage.total_tokens === 0 && topLevel.total_tokens > 0) return topLevel;
  return null;
}

function normalizedUsage(value) {
  const fields = CLAUDE_TERMINAL_USAGE_FIELDS;
  requireContract(isPlainObject(value) && fields.every((key) => Number.isSafeInteger(value[key]) && value[key] >= 0), "CLAUDE_DEEPSEEK_TERMINAL_USAGE_INVALID", "Claude terminal usage is incomplete");
  requireContract(Number.isFinite(value.cost_usd) && value.cost_usd >= 0, "CLAUDE_DEEPSEEK_TERMINAL_USAGE_INVALID", "Claude terminal cost is incomplete");
  return { schema_version: 1, ...Object.fromEntries(fields.map((key) => [key, value[key]])), total_tokens: fields.reduce((sum, key) => sum + value[key], 0), cost_usd: Math.round(value.cost_usd * 1_000_000) / 1_000_000 };
}

const ROLE_RECEIPT_COMMON_FIELDS = Object.freeze([
  "schema_version", "invocation_id", "phase", "model", "attempt", "retry", "status", "terminal",
  "started_at_utc", "finished_at_utc", "turns", "wall_timeout_seconds", "max_turns", "max_budget_usd",
  "max_output_tokens", "appended_system_prompt", "workflow", "role", "evaluation_attempt", "role_call_ordinal",
  "budget", "prompt", "tool_policy", "workspace_audit", "environment_policy", "provider_terminal",
  "usage_complete", "usage", "failure_code", "disallowed_tools",
]);
const ROLE_RECEIPT_SUCCESS_FIELDS = Object.freeze([
  ...ROLE_RECEIPT_COMMON_FIELDS,
  "tool_count", "denied_tool_attempt_count", "mcp_call_count", "bash_call_count",
]);
const ROLE_BUDGET_FIELDS = Object.freeze([
  "schema_version", "stage_cap_usd", "role", "role_pool_usd", "prior_cost_usd",
  "effective_call_cap_usd", "enforcement",
]);
const ROLE_PROVIDER_TERMINAL_FIELDS = Object.freeze(["subtype", "is_error", "stop_reason", "exit_code", "signal"]);
const ROLE_TOOL_POLICY_FIELDS = Object.freeze([
  "schema_version", "tools", "allowed_tools", "readable_scope", "writable_scope", "network", "shell", "skill_loading", "sha256",
]);
const ROLE_WORKSPACE_AUDIT_FIELDS = Object.freeze([
  "schema_version", "status", "role", "attempt", "reads", "writes", "output_path", "output_size", "output_sha256", "harness_normalized",
]);

export const CLAUDE_DEEPSEEK_MODEL_CERT_PLAN_CAPS = Object.freeze({
  max_turns: CLAUDE_DEEPSEEK_E2E_MAX_TURNS,
  max_total_tokens: CLAUDE_DEEPSEEK_E2E_TOKEN_LIMIT,
  max_output_tokens: CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS,
  max_budget_usd: CLAUDE_DEEPSEEK_E2E_USD_LIMIT,
  hard_timeout_seconds: CLAUDE_DEEPSEEK_CALL_WALL_SECONDS,
});

function validRoleProviderTerminal(value) {
  return exactKeys(value, ROLE_PROVIDER_TERMINAL_FIELDS)
    && typeof value.subtype === "string"
    && value.subtype.length > 0
    && typeof value.is_error === "boolean"
    && (value.stop_reason === null || typeof value.stop_reason === "string")
    && (value.exit_code === null || (Number.isSafeInteger(value.exit_code) && value.exit_code >= 0))
    && (value.signal === null || (typeof value.signal === "string" && value.signal.length > 0))
    && (value.is_error ? value.subtype !== "success" : value.subtype === "success")
    && (value.is_error ? (value.exit_code !== 0 || value.signal !== null) : value.exit_code === 0 && value.signal === null);
}

function validRoleEnvironmentPolicy(value) {
  return exactKeys(value, ["schema_version", "version", "provider_auth_source", "inbound", "claude_process"])
    && value.schema_version === 1
    && value.version === ISOLATED_AGENT_ENV_POLICY_VERSION
    && value.provider_auth_source === "audited-settings-file"
    && validEnvironmentKeySummary(value.inbound)
    && validEnvironmentKeySummary(value.claude_process);
}

function validRoleToolPolicy(value, role) {
  if (!exactKeys(value, ROLE_TOOL_POLICY_FIELDS)) return false;
  const { sha256, ...core } = value;
  const output = role === "SPECIALIST" ? "output/method-diagnosis.draft.json" : "output/method-review.draft.json";
  return value.schema_version === 1
    && canonicalJson(value.tools) === canonicalJson(["Read", "Write"])
    && Array.isArray(value.allowed_tools)
    && value.allowed_tools.length === 2
    && value.allowed_tools.every((item) => typeof item === "string" && item.length > 0)
    && value.allowed_tools[0].startsWith("Read(")
    && value.allowed_tools[1].startsWith("Edit(")
    && value.readable_scope === "job-request-only"
    && value.writable_scope === output
    && value.network === false
    && value.shell === false
    && value.skill_loading === false
    && /^[a-f0-9]{64}$/u.test(sha256 ?? "")
    && sha256 === sha256Bytes(canonicalJson(core));
}

function validRoleWorkspaceAudit(value, role, evaluationAttempt) {
  const output = role === "SPECIALIST" ? "output/method-diagnosis.draft.json" : "output/method-review.draft.json";
  return exactKeys(value, ROLE_WORKSPACE_AUDIT_FIELDS)
    && value.schema_version === 1
    && value.status === "PASS"
    && value.role === role
    && value.attempt === evaluationAttempt
    && Number.isSafeInteger(value.reads)
    && value.reads >= 0
    && value.writes === 1
    && value.output_path === output
    && Number.isSafeInteger(value.output_size)
    && value.output_size >= 0
    && /^[a-f0-9]{64}$/u.test(value.output_sha256 ?? "")
    && value.harness_normalized === false;
}

export function validateClaudeDeepseekRoleReceipt(receipt, {
  planCaps = CLAUDE_DEEPSEEK_MODEL_CERT_PLAN_CAPS,
  expectedRole = null,
  expectedAttempt = null,
  priorCostUsd = null,
} = {}) {
  const success = receipt?.status === "PASS";
  requireContract(
    exactKeys(receipt, success ? ROLE_RECEIPT_SUCCESS_FIELDS : ROLE_RECEIPT_COMMON_FIELDS),
    "CLAUDE_DEEPSEEK_ROLE_RECEIPT_FIELDS_INVALID",
    "Evidence V2 role receipt fields are not closed",
  );
  const role = receipt.role;
  const evaluationAttempt = receipt.evaluation_attempt;
  requireContract(
    receipt.schema_version === 1
      && ["PASS", "FAIL"].includes(receipt.status)
      && ["SPECIALIST", "REVIEWER"].includes(role)
      && ["PRIMARY", "REPAIR"].includes(evaluationAttempt)
      && (expectedRole === null || role === expectedRole)
      && (expectedAttempt === null || evaluationAttempt === expectedAttempt)
      && receipt.phase === role
      && receipt.workflow === `${role}:${evaluationAttempt}`
      && receipt.role_call_ordinal === (evaluationAttempt === "PRIMARY" ? 1 : 2)
      && receipt.model === CLAUDE_DEEPSEEK_MODEL
      && receipt.attempt === 1
      && receipt.retry === 0
      && receipt.terminal === true
      && Number.isSafeInteger(receipt.turns)
      && receipt.turns >= (success ? 1 : 0)
      && receipt.turns <= planCaps.max_turns
      && receipt.max_turns === planCaps.max_turns
      && receipt.wall_timeout_seconds === planCaps.hard_timeout_seconds
      && receipt.max_output_tokens === planCaps.max_output_tokens
      && receipt.appended_system_prompt === null
      && Array.isArray(receipt.disallowed_tools)
      && canonicalJson(receipt.disallowed_tools) === canonicalJson(["Bash", "Glob", "Grep", "Skill"])
      && typeof receipt.invocation_id === "string"
      && receipt.invocation_id.length > 0,
    "CLAUDE_DEEPSEEK_ROLE_RECEIPT_IDENTITY_INVALID",
    "Evidence V2 role receipt identity, model, turn, or cap fields are invalid",
  );
  const started = Date.parse(receipt.started_at_utc);
  const finished = Date.parse(receipt.finished_at_utc);
  requireContract(Number.isFinite(started) && Number.isFinite(finished) && finished >= started, "CLAUDE_DEEPSEEK_ROLE_RECEIPT_TIME_INVALID", "Evidence V2 role receipt timestamps are invalid");
  requireContract(
    exactKeys(receipt.prompt, ["sha256", "utf8_size"])
      && /^[a-f0-9]{64}$/u.test(receipt.prompt.sha256 ?? "")
      && Number.isSafeInteger(receipt.prompt.utf8_size)
      && receipt.prompt.utf8_size > 0,
    "CLAUDE_DEEPSEEK_ROLE_RECEIPT_PROMPT_INVALID",
    "Evidence V2 role prompt receipt is invalid",
  );
  requireContract(validRoleToolPolicy(receipt.tool_policy, role), "CLAUDE_DEEPSEEK_ROLE_RECEIPT_TOOL_POLICY_INVALID", "Evidence V2 role tool policy receipt is invalid");
  const budgetPrior = priorCostUsd === null ? receipt.budget?.prior_cost_usd : priorCostUsd;
  const expectedEffective = Number.isFinite(budgetPrior)
    ? Math.max(0, Math.round((CLAUDE_DEEPSEEK_MODEL_CERT_ROLE_POOL_USD - budgetPrior) * 1_000_000) / 1_000_000)
    : null;
  requireContract(
    exactKeys(receipt.budget, ROLE_BUDGET_FIELDS)
      && receipt.budget.schema_version === 1
      && receipt.budget.stage_cap_usd === planCaps.max_budget_usd
      && receipt.budget.role === role
      && receipt.budget.role_pool_usd === CLAUDE_DEEPSEEK_MODEL_CERT_ROLE_POOL_USD
      && receipt.budget.prior_cost_usd === budgetPrior
      && receipt.budget.effective_call_cap_usd === expectedEffective
      && receipt.budget.enforcement === CLAUDE_DEEPSEEK_MODEL_CERT_BUDGET_ENFORCEMENT
      && receipt.max_budget_usd === expectedEffective,
    "CLAUDE_DEEPSEEK_ROLE_RECEIPT_BUDGET_INVALID",
    "Evidence V2 role budget receipt is invalid",
  );
  const usage = receipt.usage === null ? null : normalizedUsage(receipt.usage);
  requireContract(receipt.usage === null || (
    exactKeys(receipt.usage, [
      "schema_version", "input_tokens", "output_tokens", "cache_creation_input_tokens",
      "cache_read_input_tokens", "total_tokens", "cost_usd",
    ])
      && receipt.usage.schema_version === 1
      && canonicalJson(receipt.usage) === canonicalJson(usage)
  ), "CLAUDE_DEEPSEEK_ROLE_RECEIPT_USAGE_INVALID", "Evidence V2 role usage fields are not closed");
  requireContract(receipt.usage_complete === (usage !== null), "CLAUDE_DEEPSEEK_ROLE_RECEIPT_USAGE_INVALID", "Evidence V2 role usage completeness is inconsistent");
  requireContract(receipt.provider_terminal === null || validRoleProviderTerminal(receipt.provider_terminal), "CLAUDE_DEEPSEEK_ROLE_RECEIPT_TERMINAL_INVALID", "Evidence V2 role provider terminal is invalid");
  if (success) {
    requireContract(
      usage !== null && usage.cost_usd <= expectedEffective,
      "CLAUDE_DEEPSEEK_ROLE_RECEIPT_BUDGET_INVALID",
      "Evidence V2 successful role usage exceeded its effective call cap",
    );
    requireContract(receipt.failure_code === null
      && usage.total_tokens <= planCaps.max_total_tokens
      && receipt.provider_terminal?.subtype === "success"
      && receipt.provider_terminal.is_error === false
      && validRoleEnvironmentPolicy(receipt.environment_policy)
      && validRoleWorkspaceAudit(receipt.workspace_audit, role, evaluationAttempt)
      && Number.isSafeInteger(receipt.tool_count)
      && receipt.tool_count > 0
      && Number.isSafeInteger(receipt.denied_tool_attempt_count)
      && receipt.denied_tool_attempt_count >= 0
      && receipt.mcp_call_count === 0
      && receipt.bash_call_count === 0,
    "CLAUDE_DEEPSEEK_ROLE_RECEIPT_SUCCESS_INVALID", "Evidence V2 successful role receipt is incomplete or exceeded a cap");
  } else {
    requireContract(/^[A-Z][A-Z0-9_]*$/u.test(receipt.failure_code ?? "")
      && receipt.workspace_audit === null
      && (receipt.environment_policy === null || validRoleEnvironmentPolicy(receipt.environment_policy))
      && (receipt.provider_terminal !== null || usage === null),
    "CLAUDE_DEEPSEEK_ROLE_RECEIPT_FAILURE_INVALID", "Evidence V2 failed role receipt is inconsistent");
  }
  return receipt;
}

export function aggregateClaudeUsage(invocations) {
  requireContract(Array.isArray(invocations), "CLAUDE_DEEPSEEK_INVOCATIONS_INVALID", "Invocation ledger must be an array");
  const aggregate = { schema_version: 1, input_tokens: 0, output_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, total_tokens: 0, cost_usd: 0 };
  for (const invocation of invocations) {
    const usage = normalizedUsage(invocation?.usage);
    for (const key of ["input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "total_tokens"]) aggregate[key] += usage[key];
    aggregate.cost_usd += usage.cost_usd;
  }
  aggregate.cost_usd = Math.round(aggregate.cost_usd * 1_000_000) / 1_000_000;
  return aggregate;
}

export function claudeDeepseekE2EPhases(
  scenarioId,
  evaluationMode = "SPECIALIST_ONLY",
) {
  requireContract(CLAUDE_DEEPSEEK_SCENARIOS.includes(scenarioId), "CLAUDE_DEEPSEEK_SCENARIO_INVALID", "Scenario is outside the repository-owned matrix", { scenario_id: scenarioId });
  return macosCodexLunaE2EPhases(scenarioId, evaluationMode);
}

export function claudeDeepseekE2ECallCount(
  scenarioId,
  evaluationMode = "SPECIALIST_ONLY",
) {
  return claudeDeepseekE2EPhases(scenarioId, evaluationMode).length;
}

export function mapScenarioToCreateCase(facts) {
  const base = mapBaseScenarioToCreateCase(facts);
  return Object.freeze({
    ...base,
    initial_user_fact_names: ["problem_time", "client_slot", "client_process_name", "server_slot", "server_process_name", "service", "api"],
    initial_user_fact_values: [base.initial_user_fact_values[0], facts.client_slot, facts.client_process, facts.server_slot, facts.server_process, facts.service, facts.api],
  });
}

export function auditClaudeInvocations(invocations, {
  workflow,
  scenarioId = null,
  evaluationMode = "SPECIALIST_ONLY",
}) {
  const generation = workflow === "generation";
  const phases = generation
    ? ["REGISTRATION_GENERATION"]
    : claudeDeepseekE2EPhases(scenarioId, evaluationMode);
  const tokenLimit = generation ? CLAUDE_DEEPSEEK_METHODS_TOKEN_LIMIT : CLAUDE_DEEPSEEK_E2E_TOKEN_LIMIT;
  const costLimit = generation ? CLAUDE_DEEPSEEK_METHODS_USD_LIMIT : CLAUDE_DEEPSEEK_E2E_USD_LIMIT;
  const turnLimit = generation ? CLAUDE_DEEPSEEK_METHODS_MAX_TURNS : CLAUDE_DEEPSEEK_E2E_MAX_TURNS;
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
      && item.wall_timeout_seconds === (generation ? CLAUDE_DEEPSEEK_METHODS_WALL_SECONDS : CLAUDE_DEEPSEEK_CALL_WALL_SECONDS)
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

export function auditClaudeModelCertInvocations(invocations, {
  evaluationMode = "SPECIALIST_ONLY",
} = {}) {
  requireContract(
    ["SPECIALIST_ONLY", "BLIND_CONSENSUS"].includes(evaluationMode),
    "CLAUDE_DEEPSEEK_MODEL_CERT_EVALUATION_MODE_INVALID",
    "Evidence V2 model-cert evaluation mode is invalid",
    { evaluation_mode: evaluationMode },
  );
  const normalCallCount = evaluationMode === "SPECIALIST_ONLY"
    ? CLAUDE_DEEPSEEK_MODEL_CERT_NORMAL_CALLS
    : CLAUDE_DEEPSEEK_BLIND_REVIEW_MODEL_CERT_NORMAL_CALLS;
  const hardCallCap = evaluationMode === "SPECIALIST_ONLY"
    ? CLAUDE_DEEPSEEK_MODEL_CERT_MAX_CALLS
    : CLAUDE_DEEPSEEK_BLIND_REVIEW_MODEL_CERT_MAX_CALLS;
  requireContract(
    Array.isArray(invocations)
      && invocations.length >= normalCallCount
      && invocations.length <= hardCallCap,
    "CLAUDE_DEEPSEEK_MODEL_CERT_CALL_COUNT_INVALID",
    "Evidence V2 model-cert call count differs from its evaluation mode",
    { actual: invocations?.length ?? null, evaluation_mode: evaluationMode },
  );
  const attempts = invocations.map((item) => `${item?.role}:${item?.evaluation_attempt}`);
  const legal = evaluationMode === "SPECIALIST_ONLY"
    ? [
      ["SPECIALIST:PRIMARY"],
      ["SPECIALIST:PRIMARY", "SPECIALIST:REPAIR"],
    ]
    : [
      ["SPECIALIST:PRIMARY", "REVIEWER:PRIMARY"],
      ["SPECIALIST:PRIMARY", "SPECIALIST:REPAIR", "REVIEWER:PRIMARY"],
      ["SPECIALIST:PRIMARY", "REVIEWER:PRIMARY", "REVIEWER:REPAIR"],
      ["SPECIALIST:PRIMARY", "SPECIALIST:REPAIR", "REVIEWER:PRIMARY", "REVIEWER:REPAIR"],
    ];
  requireContract(
    legal.some((sequence) => canonicalJson(sequence) === canonicalJson(attempts)),
    "CLAUDE_DEEPSEEK_MODEL_CERT_SEQUENCE_INVALID",
    "Evidence V2 model-cert role calls are out of order or exceed the repair allowance",
    { attempts },
  );
  const roleCosts = { SPECIALIST: 0, REVIEWER: 0 };
  const primaryCosts = {};
  for (const item of invocations) {
    const priorCostUsd = item.evaluation_attempt === "PRIMARY" ? 0 : primaryCosts[item.role];
    try {
      validateClaudeDeepseekRoleReceipt(item, {
        planCaps: CLAUDE_DEEPSEEK_MODEL_CERT_PLAN_CAPS,
        expectedRole: item.role,
        expectedAttempt: item.evaluation_attempt,
        priorCostUsd,
      });
    } catch (error) {
      if (error?.code === "CLAUDE_DEEPSEEK_ROLE_RECEIPT_BUDGET_INVALID") {
        fail("CLAUDE_DEEPSEEK_MODEL_CERT_BUDGET_RECEIPT_INVALID", "Evidence V2 model-cert role budget receipt is invalid", { role: item?.role ?? null });
      }
      throw error;
    }
    const usage = normalizedUsage(item.usage);
    if (item.evaluation_attempt === "PRIMARY") primaryCosts[item.role] = usage.cost_usd;
    roleCosts[item.role] = Math.round((roleCosts[item.role] + usage.cost_usd) * 1_000_000) / 1_000_000;
    requireContract(
      roleCosts[item.role] <= CLAUDE_DEEPSEEK_MODEL_CERT_ROLE_POOL_USD,
      "CLAUDE_DEEPSEEK_MODEL_CERT_ROLE_BUDGET_EXCEEDED",
      "Evidence V2 model-cert role usage exceeded its two-dollar pool",
      { role: item.role },
    );
  }
  const aggregate = aggregateClaudeUsage(invocations);
  requireContract(
    aggregate.total_tokens <= CLAUDE_DEEPSEEK_E2E_TOKEN_LIMIT
      && aggregate.cost_usd <= CLAUDE_DEEPSEEK_E2E_USD_LIMIT,
    "CLAUDE_DEEPSEEK_BUDGET_EXCEEDED",
    "Evidence V2 model-cert usage exceeded its aggregate cap",
    { token_limit: CLAUDE_DEEPSEEK_E2E_TOKEN_LIMIT, cost_limit: CLAUDE_DEEPSEEK_E2E_USD_LIMIT },
  );
  return Object.freeze({
    schema_version: 1,
    status: "PASS",
    workflow: "evidence-v2-model-cert",
    evaluation_mode: evaluationMode,
    normal_call_count: normalCallCount,
    hard_call_cap: hardCallCap,
    actual_call_count: invocations.length,
    repair_counts: {
      specialist: attempts.includes("SPECIALIST:REPAIR") ? 1 : 0,
      reviewer: attempts.includes("REVIEWER:REPAIR") ? 1 : 0,
    },
    retry_count: 0,
    token_formula: "input_tokens+output_tokens+cache_creation_input_tokens+cache_read_input_tokens",
    aggregate,
  });
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
  const usage = resolvedTerminalUsage(result);
  requireContract(usage !== null, "CLAUDE_DEEPSEEK_TERMINAL_USAGE_INVALID", "Claude terminal usage sources are incomplete or inconsistent");
  return { schema_version: 1, status: "PASS", phase, model: init[0].model, turns: result.num_turns, wall_timeout_seconds: wallTimeoutSeconds, usage, tools };
}

function shellWords(command) {
  requireContract(typeof command === "string" && command.length > 0 && !/[\n\r\0;&|`]/.test(command), "CLAUDE_DEEPSEEK_BASH_SYNTAX_FORBIDDEN", "Bash command contains chaining, substitution, or multiple lines");
  const words = command.match(/(?:[^\s'\"]+|'[^']*'|\"[^\"]*\")+/g) ?? [];
  return words.map((word) => ((word.startsWith("'") && word.endsWith("'")) || (word.startsWith('"') && word.endsWith('"'))) ? word.slice(1, -1) : word);
}

export function auditClientBash(commands, { archivePath, archive, descriptor, download = null }) {
  requireContract(Array.isArray(commands), "CLAUDE_DEEPSEEK_BASH_LEDGER_INVALID", "Bash ledger must be an array");
  requireContract(commands.length === (download === null ? 3 : 6), "CLAUDE_DEEPSEEK_BASH_CARDINALITY_INVALID", "Client Bash command cardinality does not match the upload/download workflow");
  const parsed = commands.map((entry) => ({ ...entry, words: shellWords(entry.command) }));
  requireContract(parsed.every((entry) => entry.status === "completed" && entry.exit_code === 0), "CLAUDE_DEEPSEEK_BASH_RESULT_INVALID", "Every allowed Bash command must complete successfully");
  requireContract(parsed[0].words[0] === "/usr/bin/openssl" && parsed[0].words[1] === "dgst" && parsed[0].words[2] === "-sha256" && parsed[0].words[3] === archivePath && parsed[0].words.length === 4, "CLAUDE_DEEPSEEK_OPENSSL_COMMAND_INVALID", "openssl command is not the exact digest command");
  requireContract(parsed[1].words[0] === "/usr/bin/stat" && parsed[1].words[1] === "-f" && parsed[1].words[2] === "%z" && parsed[1].words[3] === archivePath && parsed[1].words.length === 4, "CLAUDE_DEEPSEEK_STAT_COMMAND_INVALID", "stat command is not the exact size command");
  const curl = parsed[2];
  requireContract(curl.words[0] === "/usr/bin/curl" && curl.words.includes("PUT") && curl.words.includes(descriptor.url) && curl.words.includes(archivePath), "CLAUDE_DEEPSEEK_CURL_COMMAND_INVALID", "curl command is not bound to the UploadDescriptor and archive");
  requireContract((curl.words.filter((word) => word === "-H" || word === "--header").length === Object.keys(descriptor.required_headers).length), "CLAUDE_DEEPSEEK_CURL_HEADERS_INVALID", "curl must carry each and only each required upload header");
  requireContract(String(parsed[0].stdout ?? "").toLowerCase().includes(archive.sha256) && String(parsed[1].stdout ?? "").trim() === String(archive.size), "CLAUDE_DEEPSEEK_ATTACHMENT_PRECHECK_INVALID", "openssl/stat receipts do not match the deterministic ZIP");
  if (download !== null) {
    requireContract(typeof download.path === "string" && path.isAbsolute(download.path) && Number.isSafeInteger(download.artifact?.size) && /^[a-f0-9]{64}$/u.test(download.artifact?.sha256 ?? "") && typeof download.artifact?.download_url === "string", "CLAUDE_DEEPSEEK_DOWNLOAD_DESCRIPTOR_INVALID", "Artifact download audit input is invalid");
    const get = parsed[3];
    requireContract(get.words[0] === "/usr/bin/curl" && get.words.includes("GET") && get.words.includes("--output") && get.words.includes(download.path) && get.words.includes(download.artifact.download_url), "CLAUDE_DEEPSEEK_DOWNLOAD_COMMAND_INVALID", "curl GET is not bound to the Artifact download_url and the frozen result path");
    requireContract(parsed[4].words[0] === "/usr/bin/stat" && parsed[4].words[1] === "-f" && parsed[4].words[2] === "%z" && parsed[4].words[3] === download.path && parsed[4].words.length === 4, "CLAUDE_DEEPSEEK_DOWNLOAD_STAT_INVALID", "Downloaded result stat command is invalid");
    requireContract(parsed[5].words[0] === "/usr/bin/openssl" && parsed[5].words[1] === "dgst" && parsed[5].words[2] === "-sha256" && parsed[5].words[3] === download.path && parsed[5].words.length === 4, "CLAUDE_DEEPSEEK_DOWNLOAD_OPENSSL_INVALID", "Downloaded result digest command is invalid");
    requireContract(String(parsed[4].stdout ?? "").trim() === String(download.artifact.size) && String(parsed[5].stdout ?? "").toLowerCase().includes(download.artifact.sha256), "CLAUDE_DEEPSEEK_DOWNLOAD_CHECK_INVALID", "Downloaded result size or SHA-256 receipt differs from the Artifact descriptor");
  }
  return { schema_version: 2, status: "PASS", command_count: parsed.length, programs: parsed.map((entry) => entry.words[0]), upload_count: 1, download_count: download === null ? 0 : 1 };
}

export function publishRegistrationCacheAtomically({ cacheRoot, producer, registrationRoot, stagingRoot }) {
  const destination = registrationCachePath(cacheRoot, producer.producer_identity);
  const manifest = buildRegistrationCacheManifest({ producer, registrationRoot });
  const stage = path.resolve(stagingRoot);
  requireContract(!fs.existsSync(stage), "CLAUDE_DEEPSEEK_CACHE_STAGING_EXISTS", "Cache staging directory already exists");
  fs.mkdirSync(path.join(stage, "registration"), { recursive: true, mode: 0o700 });
  fs.cpSync(registrationRoot, path.join(stage, "registration", CLAUDE_DEEPSEEK_REGISTRATION_ID), { recursive: true, errorOnExist: true, force: false });
  fs.writeFileSync(path.join(stage, "manifest.json"), canonicalJson(manifest), { encoding: "utf8", mode: 0o600, flag: "wx" });
  fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 });
  try { fs.renameSync(stage, destination); } catch (error) {
    if (!["EEXIST", "ENOTEMPTY", "EPERM"].includes(error.code) || !fs.existsSync(destination)) throw error;
    const existing = validateRegistrationCache({ cacheRoot, producer });
    requireContract(canonicalJson(existing.manifest) === canonicalJson(manifest), "CLAUDE_DEEPSEEK_CACHE_COLLISION", "Existing producer identity contains different bytes");
    fs.rmSync(stage, { recursive: true });
    return { ...existing, published: false, collision: "byte-identical" };
  }
  const receipt = validateRegistrationCache({ cacheRoot, producer });
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
  packageTreeIdentity,
  scenarioPaths,
  writeDeterministicLogsZip,
};
