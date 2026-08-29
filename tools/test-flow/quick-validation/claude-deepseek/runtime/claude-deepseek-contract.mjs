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
export const CLAUDE_DEEPSEEK_MODEL_CERT_NORMAL_CALLS = 2;
export const CLAUDE_DEEPSEEK_MODEL_CERT_MAX_CALLS = 4;
export const CLAUDE_DEEPSEEK_MODEL_CERT_SCENARIO = "multiple-rpc-timeouts";
export const CLAUDE_DEEPSEEK_MODEL_CERT_PHASES = Object.freeze(["SPECIALIST", "REVIEWER"]);
export const CLAUDE_DEEPSEEK_METHODS_TOKEN_LIMIT = 1_000_000;
export const CLAUDE_DEEPSEEK_METHODS_USD_LIMIT = 10;
export const CLAUDE_DEEPSEEK_E2E_TOKEN_LIMIT = 2_000_000;
export const CLAUDE_DEEPSEEK_E2E_USD_LIMIT = 4;
export const CLAUDE_DEEPSEEK_MODEL_CERT_ROLE_POOL_USD = CLAUDE_DEEPSEEK_E2E_USD_LIMIT / CLAUDE_DEEPSEEK_MODEL_CERT_NORMAL_CALLS;
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

function normalizedUsage(value) {
  const fields = ["input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"];
  requireContract(isPlainObject(value) && fields.every((key) => Number.isSafeInteger(value[key]) && value[key] >= 0), "CLAUDE_DEEPSEEK_TERMINAL_USAGE_INVALID", "Claude terminal usage is incomplete");
  requireContract(Number.isFinite(value.cost_usd) && value.cost_usd >= 0, "CLAUDE_DEEPSEEK_TERMINAL_USAGE_INVALID", "Claude terminal cost is incomplete");
  return { schema_version: 1, ...Object.fromEntries(fields.map((key) => [key, value[key]])), total_tokens: fields.reduce((sum, key) => sum + value[key], 0), cost_usd: Math.round(value.cost_usd * 1_000_000) / 1_000_000 };
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

export function claudeDeepseekE2EPhases(scenarioId) {
  requireContract(CLAUDE_DEEPSEEK_SCENARIOS.includes(scenarioId), "CLAUDE_DEEPSEEK_SCENARIO_INVALID", "Scenario is outside the repository-owned matrix", { scenario_id: scenarioId });
  return macosCodexLunaE2EPhases(scenarioId);
}

export function claudeDeepseekE2ECallCount(scenarioId) {
  return claudeDeepseekE2EPhases(scenarioId).length;
}

export function mapScenarioToCreateCase(facts) {
  const base = mapBaseScenarioToCreateCase(facts);
  return Object.freeze({
    ...base,
    initial_user_fact_names: ["problem_time", "client_slot", "client_process_name", "server_slot", "server_process_name", "service", "api"],
    initial_user_fact_values: [base.initial_user_fact_values[0], facts.client_slot, facts.client_process, facts.server_slot, facts.server_process, facts.service, facts.api],
  });
}

export function auditClaudeInvocations(invocations, { workflow, scenarioId = null }) {
  const generation = workflow === "generation";
  const phases = generation ? ["REGISTRATION_GENERATION"] : claudeDeepseekE2EPhases(scenarioId);
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

export function auditClaudeModelCertInvocations(invocations) {
  requireContract(
    Array.isArray(invocations)
      && invocations.length >= CLAUDE_DEEPSEEK_MODEL_CERT_NORMAL_CALLS
      && invocations.length <= CLAUDE_DEEPSEEK_MODEL_CERT_MAX_CALLS,
    "CLAUDE_DEEPSEEK_MODEL_CERT_CALL_COUNT_INVALID",
    "Evidence V2 model-cert must use two normal role calls and at most one repair per role",
    { actual: invocations?.length ?? null },
  );
  const attempts = invocations.map((item) => `${item?.role}:${item?.evaluation_attempt}`);
  const legal = [
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
    requireContract(
      item.phase === item.role
        && ["SPECIALIST", "REVIEWER"].includes(item.role)
        && ["PRIMARY", "REPAIR"].includes(item.evaluation_attempt)
        && item.role_call_ordinal === (item.evaluation_attempt === "PRIMARY" ? 1 : 2)
        && item.model === CLAUDE_DEEPSEEK_MODEL
        && item.attempt === 1
        && item.retry === 0
        && item.status === "PASS"
        && item.terminal === true
        && Number.isSafeInteger(item.turns)
        && item.turns > 0
        && item.turns <= CLAUDE_DEEPSEEK_E2E_MAX_TURNS
        && item.wall_timeout_seconds === CLAUDE_DEEPSEEK_CALL_WALL_SECONDS
        && item.workspace_audit?.status === "PASS"
        && item.workspace_audit?.harness_normalized === false
        && item.tool_policy?.shell === false
        && item.tool_policy?.network === false,
      "CLAUDE_DEEPSEEK_MODEL_CERT_INVOCATION_INVALID",
      "Evidence V2 model-cert invocation identity or role boundary drifted",
      { role: item?.role ?? null, evaluation_attempt: item?.evaluation_attempt ?? null },
    );
    const usage = normalizedUsage(item.usage);
    const priorCostUsd = item.evaluation_attempt === "PRIMARY" ? 0 : primaryCosts[item.role];
    const effectiveCallCapUsd = Number.isFinite(priorCostUsd)
      ? Math.max(0, Math.round((CLAUDE_DEEPSEEK_MODEL_CERT_ROLE_POOL_USD - priorCostUsd) * 1_000_000) / 1_000_000)
      : null;
    requireContract(
      exactKeys(item.budget, [
        "schema_version", "stage_cap_usd", "role", "role_pool_usd", "prior_cost_usd",
        "effective_call_cap_usd", "enforcement",
      ])
        && item.budget.schema_version === 1
        && item.budget.stage_cap_usd === CLAUDE_DEEPSEEK_E2E_USD_LIMIT
        && item.budget.role === item.role
        && item.budget.role_pool_usd === CLAUDE_DEEPSEEK_MODEL_CERT_ROLE_POOL_USD
        && item.budget.prior_cost_usd === priorCostUsd
        && item.budget.effective_call_cap_usd === effectiveCallCapUsd
        && item.budget.enforcement === CLAUDE_DEEPSEEK_MODEL_CERT_BUDGET_ENFORCEMENT
        && item.max_budget_usd === effectiveCallCapUsd
        && effectiveCallCapUsd > 0
        && usage.cost_usd <= effectiveCallCapUsd,
      "CLAUDE_DEEPSEEK_MODEL_CERT_BUDGET_RECEIPT_INVALID",
      "Evidence V2 model-cert invocation budget receipt is invalid or terminal cost exceeded its effective call cap",
      { role: item?.role ?? null, evaluation_attempt: item?.evaluation_attempt ?? null },
    );
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
    normal_call_count: CLAUDE_DEEPSEEK_MODEL_CERT_NORMAL_CALLS,
    hard_call_cap: CLAUDE_DEEPSEEK_MODEL_CERT_MAX_CALLS,
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
    requireContract(get.words[0] === "/usr/bin/curl" && get.words.includes("GET") && get.words.includes("--output") && get.words.includes(download.path) && get.words.includes(download.artifact.download_url), "CLAUDE_DEEPSEEK_DOWNLOAD_COMMAND_INVALID", "curl GET is not bound to list_artifacts download_url and the frozen result path");
    requireContract(parsed[4].words[0] === "/usr/bin/stat" && parsed[4].words[1] === "-f" && parsed[4].words[2] === "%z" && parsed[4].words[3] === download.path && parsed[4].words.length === 4, "CLAUDE_DEEPSEEK_DOWNLOAD_STAT_INVALID", "Downloaded result stat command is invalid");
    requireContract(parsed[5].words[0] === "/usr/bin/openssl" && parsed[5].words[1] === "dgst" && parsed[5].words[2] === "-sha256" && parsed[5].words[3] === download.path && parsed[5].words.length === 4, "CLAUDE_DEEPSEEK_DOWNLOAD_OPENSSL_INVALID", "Downloaded result digest command is invalid");
    requireContract(String(parsed[4].stdout ?? "").trim() === String(download.artifact.size) && String(parsed[5].stdout ?? "").toLowerCase().includes(download.artifact.sha256), "CLAUDE_DEEPSEEK_DOWNLOAD_CHECK_INVALID", "Downloaded result size or SHA-256 receipt differs from list_artifacts");
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
