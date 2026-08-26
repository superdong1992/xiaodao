import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

import { canonicalJson, sha256Bytes, sha256File } from "../../../lib/util.mjs";
import {
  CLAUDE_DEEPSEEK_CALL_WALL_SECONDS,
  CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS,
  CLAUDE_DEEPSEEK_METHODS_MAX_TURNS,
  CLAUDE_DEEPSEEK_METHODS_TOKEN_LIMIT,
  CLAUDE_DEEPSEEK_METHODS_USD_LIMIT,
  CLAUDE_DEEPSEEK_METHODS_WALL_SECONDS,
  CLAUDE_DEEPSEEK_MODEL,
  CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
  aggregateClaudeUsage,
  treeDigest,
  validateClaudeDeepseekIdentity,
} from "../../claude-deepseek/runtime/claude-deepseek-contract.mjs";

export const FRAMEWORK_ID = "claude-deepseek-lan-logparse-fast-e2e";
export const FRAMEWORK_VERSION = 1;
export const GENERATION_GOAL = "generation";
export const DIAGNOSIS_GOAL = "diagnosis";
export const GOALS = Object.freeze([GENERATION_GOAL, DIAGNOSIS_GOAL]);
export const DIAGNOSIS_SCENARIOS = Object.freeze(["missing-slots", "complete"]);
export const META_SKILL_NAME = "wiki-to-logparse-diagnosis-skill";
export const GENERATED_SKILL_NAME = "diagnose-rpc-timeout-lan";
export const FIXED_MODULE = "rpc";
export const GENERATION_CALLS = 1;
export const GENERATION_TOKEN_LIMIT = CLAUDE_DEEPSEEK_METHODS_TOKEN_LIMIT;
export const GENERATION_USD_LIMIT = CLAUDE_DEEPSEEK_METHODS_USD_LIMIT;
export const GENERATION_MAX_TURNS = CLAUDE_DEEPSEEK_METHODS_MAX_TURNS;
export const GENERATION_WALL_SECONDS = CLAUDE_DEEPSEEK_METHODS_WALL_SECONDS;
export const DIAGNOSIS_LIMITS = Object.freeze({
  "missing-slots": Object.freeze({ token_limit: 100_000, usd_limit: 1 }),
  complete: Object.freeze({ token_limit: 900_000, usd_limit: 7 }),
});
export const DIAGNOSIS_TOKEN_LIMIT = 1_000_000;
export const DIAGNOSIS_USD_LIMIT = 8;
export const DIAGNOSIS_MAX_TURNS = 40;
export const DIAGNOSIS_WALL_SECONDS = CLAUDE_DEEPSEEK_CALL_WALL_SECONDS;
export { CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS, CLAUDE_DEEPSEEK_MODEL, CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS };
export { treeDigest };

const REQUIRED_INPUT_PREFIX = Object.freeze([
  "problem_time",
  "client_slot",
  "client_process_name",
  "server_slot",
  "server_process_name",
]);
const EXPECTED_ROLES = Object.freeze([
  Object.freeze({ label: "client", required: true, slot_input: "client_slot", process_name_input: "client_process_name", pid_input: "client_pid" }),
  Object.freeze({ label: "server", required: true, slot_input: "server_slot", process_name_input: "server_process_name", pid_input: "server_pid" }),
]);

export class LanSkillContractError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "LanSkillContractError";
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) {
  throw new LanSkillContractError(code, message, details);
}

export function requireContract(condition, code, message, details = {}) {
  if (!condition) fail(code, message, details);
}

export function writeJsonExclusive(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, canonicalJson(value), { encoding: "utf8", mode: 0o600, flag: "wx" });
}

export function copyTree(source, destination) {
  requireContract(path.isAbsolute(source) && path.isAbsolute(destination), "LAN_COPY_PATH_INVALID", "Copied trees must use absolute paths");
  requireContract(!fs.existsSync(destination), "LAN_COPY_COLLISION", "Copy destination already exists");
  fs.mkdirSync(destination, { recursive: true, mode: 0o700 });
  for (const entry of fs.readdirSync(source, { withFileTypes: true })) {
    const from = path.join(source, entry.name);
    const to = path.join(destination, entry.name);
    const metadata = fs.lstatSync(from);
    requireContract(!metadata.isSymbolicLink(), "LAN_COPY_SYMLINK", "Copied trees cannot contain symlinks");
    if (entry.isDirectory()) copyTree(from, to);
    else if (entry.isFile() && metadata.nlink === 1) fs.copyFileSync(from, to, fs.constants.COPYFILE_EXCL);
    else fail("LAN_COPY_NODE_INVALID", "Copied trees may contain ordinary files only");
  }
}

export function createEmptyRoot(root, label) {
  const resolved = path.resolve(root);
  if (fs.existsSync(resolved)) requireContract(fs.statSync(resolved).isDirectory() && fs.readdirSync(resolved).length === 0, "LAN_ROOT_NOT_EMPTY", `${label} must be empty`);
  else fs.mkdirSync(resolved, { recursive: true, mode: 0o700 });
  return resolved;
}

export function buildSourceWikiIdentity(wikiBytes) {
  const text = new TextDecoder("utf-8", { fatal: true }).decode(wikiBytes);
  const logTemplates = [];
  let inTextFence = false;
  for (const rawLine of text.split(/\r\n|[\n\v\f\r\x1c-\x1e\x85\u2028\u2029]/u)) {
    const stripped = rawLine.trim();
    if (stripped === "```text") { inTextFence = true; continue; }
    if (stripped === "```" && inTextFence) { inTextFence = false; continue; }
    if (inTextFence && stripped && /\{[A-Za-z_][A-Za-z0-9_]*\}|%[A-Za-z]/u.test(stripped)) logTemplates.push(stripped);
  }
  return {
    schema_version: 2,
    algorithm: "sha256",
    source_path: "inputs/wiki.md",
    sha256: sha256Bytes(wikiBytes),
    log_template_extraction_version: 1,
    log_templates: logTemplates,
    log_template_inventory_sha256: sha256Bytes(canonicalJson({ version: 1, templates: logTemplates }).trimEnd()),
  };
}

export function buildProducerIdentity({ wiki, metaSkillRoot, module = FIXED_MODULE, claudeIdentity, runnerFiles = [] }) {
  const validator = path.join(metaSkillRoot, "scripts", "validate_generated_skill.py");
  const packer = path.join(metaSkillRoot, "assets", "pack_result_zip.py");
  for (const target of [wiki, validator, packer, ...runnerFiles]) requireContract(fs.existsSync(target) && fs.statSync(target).isFile(), "LAN_PRODUCER_INPUT_MISSING", "Producer input is unavailable", { path: target });
  requireContract(typeof module === "string" && module === module.trim() && module.length > 0 && module.length <= 128 && /^[\x20-\x7e]+$/u.test(module), "LAN_MODULE_INVALID", "Fixed module is invalid");
  const value = {
    schema_version: 1,
    workflow: FRAMEWORK_ID,
    meta_skill_tree_sha256: treeDigest(metaSkillRoot),
    wiki_sha256: sha256File(wiki),
    module,
    validator_sha256: sha256File(validator),
    packer_sha256: sha256File(packer),
    runner_sha256: sha256Bytes(canonicalJson(runnerFiles.map((target) => ({ path: path.basename(target), sha256: sha256File(target) }))).trimEnd()),
    claude: claudeIdentity,
  };
  return { ...value, producer_identity: sha256Bytes(canonicalJson(value).trimEnd()) };
}

export function generationCachePath(cacheRoot, producerIdentity) {
  requireContract(path.isAbsolute(cacheRoot) && /^[0-9a-f]{64}$/u.test(producerIdentity), "LAN_CACHE_PATH_INVALID", "Generation cache path is invalid");
  return path.join(cacheRoot, "claude-deepseek-lan-logparse", producerIdentity);
}

function expectedCacheManifest(producer, packageRoot) {
  return {
    schema_version: 1,
    producer,
    package: {
      skill_name: GENERATED_SKILL_NAME,
      tree_sha256: treeDigest(packageRoot),
    },
  };
}

export function validateGenerationCache({ cacheRoot, producer }) {
  const root = generationCachePath(cacheRoot, producer.producer_identity);
  const manifestPath = path.join(root, "manifest.json");
  const packageRoot = path.join(root, "package", GENERATED_SKILL_NAME);
  requireContract(fs.existsSync(manifestPath) && fs.statSync(manifestPath).isFile(), "LAN_CACHE_MANIFEST_MISSING", "Generation cache manifest is missing");
  requireContract(fs.existsSync(packageRoot) && fs.statSync(packageRoot).isDirectory(), "LAN_CACHE_PACKAGE_MISSING", "Generated Skill cache is missing");
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  const expected = expectedCacheManifest(producer, packageRoot);
  requireContract(canonicalJson(manifest) === canonicalJson(expected), "LAN_CACHE_IDENTITY_MISMATCH", "Generation cache identity or package bytes drifted");
  return { status: "PASS", root, package_root: packageRoot, manifest };
}

export function publishGenerationCache({ cacheRoot, producer, packageRoot, stagingRoot }) {
  const destination = generationCachePath(cacheRoot, producer.producer_identity);
  if (fs.existsSync(destination)) return { ...validateGenerationCache({ cacheRoot, producer }), published: false };
  requireContract(!fs.existsSync(stagingRoot), "LAN_CACHE_STAGING_COLLISION", "Generation cache staging path already exists");
  fs.mkdirSync(path.join(stagingRoot, "package"), { recursive: true, mode: 0o700 });
  copyTree(packageRoot, path.join(stagingRoot, "package", GENERATED_SKILL_NAME));
  const stagedPackage = path.join(stagingRoot, "package", GENERATED_SKILL_NAME);
  writeJsonExclusive(path.join(stagingRoot, "manifest.json"), expectedCacheManifest(producer, stagedPackage));
  fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 });
  fs.renameSync(stagingRoot, destination);
  return { ...validateGenerationCache({ cacheRoot, producer }), published: true };
}

export function validateGeneratedPackage({ pythonEntry, validator, packageRoot, wiki, module = FIXED_MODULE }) {
  const result = spawnSync(pythonEntry, ["-I", "-B", validator, "--skill-dir", packageRoot, "--wiki", wiki, "--module", module, "--json"], {
    cwd: path.dirname(validator),
    env: { PATH: "/usr/bin:/bin:/usr/sbin:/sbin", LANG: "C.UTF-8", PYTHONDONTWRITEBYTECODE: "1", PYTHONNOUSERSITE: "1" },
    encoding: "utf8",
    timeout: 120_000,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let receipt = null;
  try { receipt = JSON.parse(result.stdout); } catch {}
  requireContract(result.status === 0 && result.signal === null && !result.error && receipt?.ok === true, "LAN_GENERATED_SKILL_INVALID", "Generated LAN Logparse Skill failed its canonical validator", { receipt, stderr: String(result.stderr ?? "").slice(0, 1_000) });
  return { schema_version: 1, status: "PASS", validator_sha256: sha256File(validator), result: receipt };
}

export function auditGeneratedPackage({ packageRoot, oraclePath, module = FIXED_MODULE }) {
  const methods = JSON.parse(fs.readFileSync(path.join(packageRoot, "methods.json"), "utf8"));
  const logparse = JSON.parse(fs.readFileSync(path.join(packageRoot, "logparse.json"), "utf8"));
  const skill = fs.readFileSync(path.join(packageRoot, "SKILL.md"), "utf8");
  const oracle = JSON.parse(fs.readFileSync(oraclePath, "utf8"));
  const mismatches = [];
  const equal = (name, actual, expected) => { if (canonicalJson(actual) !== canonicalJson(expected)) mismatches.push(name); };
  const fixedInputAliases = new Set(["problem_time", "client_slot", "client_process", "client_process_name", "server_slot", "server_process", "server_process_name"]);
  const additionalInputs = [...new Set((oracle.expected_package.required_user_inputs ?? []).filter((input) => !fixedInputAliases.has(input)).map((input) => input === "service_name" ? "service" : input === "api_name" ? "api" : input))];
  equal("required_user_inputs", methods.required_user_inputs, [...REQUIRED_INPUT_PREFIX, ...additionalInputs]);
  if (methods.required_artifacts?.[0] !== "log_archive") mismatches.push("log_archive");
  equal("logparse_roles", logparse.roles, EXPECTED_ROLES);
  if (logparse.helper_skill !== "logparse-diagnose") mismatches.push("helper_skill");
  if (logparse.module !== module) mismatches.push("module");
  for (const phrase of ["Skill(logparse-diagnose)", "target_logs[*].log_path", "result.txt", "result.zip"]) if (!skill.includes(phrase)) mismatches.push(`skill_phrase:${phrase}`);
  if (/^[ \t]*(?:\$[ \t]*)?`?problem-locator-logparse(?:[ \t]|`|$)/mu.test(skill)) mismatches.push("direct_problem_locator_logparse_command");
  if (/^[^\r\n]*\bcli\.py[ \t]+(?:parse|mech-target-logs)\b/mu.test(skill)) mismatches.push("direct_legacy_cli_command");
  if (/\bSKILL_FIXED\b/u.test(skill)) mismatches.push("skill_fixed_binding");
  const markerSets = (methods.methods ?? []).map((method) => new Set(method.evidence_markers ?? []));
  for (const required of oracle.expected_package.method_marker_sets) if (!markerSets.some((actual) => required.all_markers.every((marker) => actual.has(marker)))) mismatches.push(`method_marker_set:${required.semantic_id}`);
  const sharedText = (methods.shared_references ?? []).map((relative) => fs.readFileSync(path.join(packageRoot, ...relative.split("/")), "utf8")).join("\n");
  if (oracle.expected_package.required_shared_markers.some((marker) => !sharedText.includes(marker))) mismatches.push("required_shared_markers");
  requireContract(mismatches.length === 0, "LAN_GENERATION_ORACLE_MISMATCH", "Generated LAN Logparse Skill failed its semantic oracle", { mismatches });
  return { schema_version: 1, status: "PASS", oracle_sha256: sha256File(oraclePath), mismatch_count: 0 };
}

export function auditGenerationTools(processResult) {
  requireContract(processResult.skills.length === 1 && processResult.skills[0].skill === META_SKILL_NAME, "LAN_GENERATION_SKILL_CALL_INVALID", "Generation must load exactly the LAN Logparse meta Skill");
  requireContract(processResult.records[0]?.name === "Skill" && processResult.records[0]?.input?.skill === META_SKILL_NAME, "LAN_GENERATION_FIRST_TOOL_INVALID", "Generation must load the meta Skill first");
  requireContract(processResult.bash.length === 0 && processResult.mcp.length === 0 && processResult.denied.length === 0, "LAN_GENERATION_TOOL_SCOPE_INVALID", "Generation may use only Skill, Read and Write");
  return { schema_version: 1, status: "PASS", tool_count: processResult.records.length, skill_calls: processResult.skills };
}

export function auditInvocationUsage(receipts, { tokenLimit, usdLimit, phases }) {
  requireContract(Array.isArray(receipts) && receipts.length === phases.length, "LAN_INVOCATION_COUNT_INVALID", "Claude invocation count drifted");
  for (const [index, receipt] of receipts.entries()) {
    requireContract(receipt.phase === phases[index] && receipt.model === CLAUDE_DEEPSEEK_MODEL && receipt.attempt === 1 && receipt.retry === 0 && receipt.status === "PASS" && receipt.terminal === true, "LAN_INVOCATION_IDENTITY_INVALID", "Claude invocation identity drifted", { phase: receipt?.phase });
  }
  const aggregate = aggregateClaudeUsage(receipts);
  requireContract(aggregate.total_tokens <= tokenLimit && aggregate.cost_usd <= usdLimit, "LAN_INVOCATION_BUDGET_EXCEEDED", "Claude invocation usage exceeded the standalone cap", { token_limit: tokenLimit, usd_limit: usdLimit, aggregate });
  return { schema_version: 1, status: "PASS", expected_phases: phases, retry_count: 0, aggregate };
}

export function finalText(events) {
  const result = [...events].reverse().find((event) => event?.type === "result");
  return typeof result?.result === "string" ? result.result : "";
}

export function currentIdentity(claudeEntry, claudeSettings) {
  return validateClaudeDeepseekIdentity(claudeEntry, claudeSettings);
}

export function safeFailure(error) {
  return { code: error?.code ?? "LAN_FAST_E2E_FAILED", message: error?.message ?? String(error), details: error?.details ?? {} };
}
