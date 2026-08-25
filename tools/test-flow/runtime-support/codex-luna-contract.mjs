import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

export const CODEX_LUNA_CONTRACT_VERSION = 1;
export const CODEX_LUNA_MODEL = "gpt-5.6-luna";
export const CODEX_LUNA_REASONING_EFFORT = "medium";
export const CODEX_LUNA_CLI_VERSION = "0.149.0-alpha.4.1";
export const CODEX_LUNA_EXPECTED_CLI_VERSION = `codex-cli ${CODEX_LUNA_CLI_VERSION}`;
export const CODEX_LUNA_EXPECTED_CLI_SHA256 = "09db9560f6f9dec139d3324254fb3c8fdbad5ecce1d8c794113dc15294f6aefd";
export const CODEX_LUNA_LINUX_CLI_VERSION = "0.149.1";
export const CODEX_LUNA_LINUX_EXPECTED_CLI_VERSION = `codex-cli ${CODEX_LUNA_LINUX_CLI_VERSION}`;
export const CODEX_LUNA_LINUX_EXPECTED_CLI_SHA256 = "73dc5888888f411c1f0fa7b81d866e721dcc86b527ce8e3b2cf4708661e823ba";
export const CODEX_LUNA_LINUX_CODE_MODE_HOST_SHA256 = "48f3a0d48033039cc7caccd209edb0ee350b81f82ca851a7b129e146e4bec6fb";
export const CODEX_LUNA_NORMAL_CALLS = 10;
export const CODEX_LUNA_MAX_CALLS = 10;
export const CODEX_LUNA_SCENARIO_COUNT = 9;
export const CODEX_LUNA_CALL_WALL_SECONDS = 1_200;
export const CODEX_LUNA_NO_PROGRESS_SECONDS = 360;
export const CODEX_LUNA_STAGE_WALL_SECONDS = 7_200;
export const CODEX_LUNA_TOKEN_LIMIT = 5_000_000;
export const CODEX_LUNA_EQUIVALENT_USD_LIMIT = 10;
export const CODEX_LUNA_POSTHOC_EXCEPTION_ID = "PSE-CODEX-LUNA-POSTHOC-001";
export const CODEX_LUNA_PERMISSION_PROFILE_VERSION = "test-flow-app-server-external-auth-v1";
export const CODEX_LUNA_APP_SERVER_SCHEMA_TREE_SHA256 = "fc8e053c99055754e184c14fb2555dd35f878a1c0e8ca641047d216d507387c0";
export const CODEX_LUNA_SOURCE_WIKI_IDENTITY_SCHEMA_VERSION = 2;
export const CODEX_LUNA_LOG_TEMPLATE_EXTRACTION_VERSION = 1;
export const CODEX_LUNA_SOURCE_WIKI_PATH = "input/wiki.md";
export const CODEX_LUNA_SOURCE_LOG_TEMPLATES_REFERENCE = "references/source-log-templates.md";

// A request may cross the model page's long-context threshold. The runner
// cannot reconstruct that boundary from Codex's terminal aggregate, so the
// upper bound intentionally charges every input/output token at the more
// expensive long-context rate and gives no cache discount.
export const CODEX_LUNA_PRICE_SNAPSHOT = Object.freeze({
  schema_version: 1,
  model: CODEX_LUNA_MODEL,
  captured_on: "2026-08-24",
  source: "https://developers.openai.com/api/docs/models/gpt-5.6-luna",
  currency: "USD",
  unit: "million_tokens",
  published_base_rates: Object.freeze({
    input: 0.20,
    cached_input: 0.02,
    output: 1.20,
  }),
  conservative_rates: Object.freeze({
    input: 0.40,
    output: 1.80,
  }),
  conservative_assumptions: Object.freeze([
    "all input tokens are charged as uncached input",
    "all calls are charged at the long-context rate",
    "cached_input_tokens, cache_write_input_tokens, and reasoning_output_tokens are reported but never double-counted",
  ]),
});

export const CODEX_LUNA_USAGE_FIELDS = Object.freeze([
  "input_tokens",
  "cached_input_tokens",
  "cache_write_input_tokens",
  "output_tokens",
  "reasoning_output_tokens",
]);

export const CODEX_LUNA_USAGE_FORMULA = "input_tokens+output_tokens";
export const CODEX_LUNA_COST_FORMULA = "input_tokens*0.40/1000000+output_tokens*1.80/1000000";

export class CodexLunaContractError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "CodexLunaContractError";
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) {
  throw new CodexLunaContractError(code, message, details);
}

function requireContract(condition, code, message, details = {}) {
  if (!condition) fail(code, message, details);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function safeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function roundMoney(value) {
  return Math.ceil((value - Number.EPSILON) * 1_000_000) / 1_000_000;
}

export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  if (isPlainObject(value)) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

export function sha256Bytes(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

export function sha256File(filePath) {
  return sha256Bytes(fs.readFileSync(filePath));
}

export function extractCodexLunaWikiLogTemplates(wikiText) {
  requireContract(typeof wikiText === "string", "CODEX_LUNA_WIKI_TEXT_INVALID", "Source Wiki text must be a string");
  const templates = [];
  let inTextFence = false;
  for (const rawLine of wikiText.split(/\r\n|[\n\v\f\r\x1c-\x1e\x85\u2028\u2029]/u)) {
    const stripped = rawLine.trim();
    if (stripped === "```text") {
      inTextFence = true;
      continue;
    }
    if (stripped === "```" && inTextFence) {
      inTextFence = false;
      continue;
    }
    if (!inTextFence || stripped.length === 0) continue;
    if (/\{[A-Za-z_][A-Za-z0-9_]*\}|%[A-Za-z]/.test(stripped)) templates.push(stripped);
  }
  return templates;
}

function sourceWikiBytes(value) {
  if (typeof value === "string") return Buffer.from(value, "utf8");
  requireContract(Buffer.isBuffer(value) || value instanceof Uint8Array, "CODEX_LUNA_WIKI_BYTES_INVALID", "Source Wiki must be UTF-8 bytes");
  return Buffer.from(value);
}

export function buildCodexLunaSourceWikiIdentity(wikiBytes) {
  const bytes = sourceWikiBytes(wikiBytes);
  let wikiText;
  try {
    wikiText = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch (error) {
    fail("CODEX_LUNA_WIKI_UTF8_INVALID", "Source Wiki must be valid UTF-8", { cause: error.message });
  }
  const logTemplates = extractCodexLunaWikiLogTemplates(wikiText);
  const inventory = { version: CODEX_LUNA_LOG_TEMPLATE_EXTRACTION_VERSION, templates: logTemplates };
  return {
    algorithm: "sha256",
    schema_version: CODEX_LUNA_SOURCE_WIKI_IDENTITY_SCHEMA_VERSION,
    sha256: sha256Bytes(bytes),
    source_path: CODEX_LUNA_SOURCE_WIKI_PATH,
    log_template_extraction_version: CODEX_LUNA_LOG_TEMPLATE_EXTRACTION_VERSION,
    log_templates: logTemplates,
    log_template_inventory_sha256: sha256Bytes(canonicalJson(inventory)),
  };
}

export function buildCodexLunaSourceLogTemplatesBytes(logTemplates) {
  requireContract(
    Array.isArray(logTemplates) && logTemplates.every((template) => typeof template === "string" && template.length > 0),
    "CODEX_LUNA_LOG_TEMPLATES_INVALID",
    "Source log templates must be an array of non-empty strings",
  );
  return Buffer.from(`# Source log templates\n\n\`\`\`text\n${logTemplates.join("\n")}\n\`\`\`\n`, "utf8");
}

export function validateCodexLunaSourceWikiIdentity(identity, wikiBytes) {
  const expected = buildCodexLunaSourceWikiIdentity(wikiBytes);
  requireContract(
    isPlainObject(identity)
      && Object.keys(identity).sort().join("\0") === Object.keys(expected).sort().join("\0")
      && canonicalJson(identity) === canonicalJson(expected),
    "CODEX_LUNA_SOURCE_WIKI_IDENTITY_INVALID",
    "Source Wiki identity must exactly match the closed schema-v2 projection of input/wiki.md",
  );
  return expected;
}

export function codexLunaAppServerCliVersion({ platform = process.platform, architecture = process.arch, environment = process.env } = {}) {
  return platform === "linux" && architecture === "x64" && environment.TEST_FLOW_QUICK_UBUNTU2204_CONTAINER === "1"
    ? CODEX_LUNA_LINUX_CLI_VERSION
    : CODEX_LUNA_CLI_VERSION;
}

export function codexLunaExecutableIdentity({ platform = process.platform, architecture = process.arch, environment = process.env } = {}) {
  if (platform === "darwin" && architecture === "arm64") {
    return Object.freeze({ version: CODEX_LUNA_EXPECTED_CLI_VERSION, cli_sha256: CODEX_LUNA_EXPECTED_CLI_SHA256, code_mode_host_sha256: null, linux_sandbox_sha256: null });
  }
  if (platform === "linux" && architecture === "x64" && environment.TEST_FLOW_QUICK_UBUNTU2204_CONTAINER === "1") {
    return Object.freeze({ version: CODEX_LUNA_LINUX_EXPECTED_CLI_VERSION, cli_sha256: CODEX_LUNA_LINUX_EXPECTED_CLI_SHA256, code_mode_host_sha256: CODEX_LUNA_LINUX_CODE_MODE_HOST_SHA256, linux_sandbox_sha256: CODEX_LUNA_LINUX_EXPECTED_CLI_SHA256 });
  }
  fail("CODEX_LUNA_PLATFORM_UNSUPPORTED", "Codex/Luna supports native macOS arm64 or the sealed Ubuntu 22.04 container wrapper");
}

export function codexLunaHelperDirectory(
  entry,
  { platform = process.platform, architecture = process.arch, environment = process.env } = {},
) {
  return platform === "linux"
    && architecture === "x64"
    && environment.TEST_FLOW_QUICK_UBUNTU2204_CONTAINER === "1"
    ? "/usr/bin"
    : path.dirname(path.resolve(entry));
}

export function validateCodexLunaIdentity(entry, authFile) {
  const expectedExecutable = codexLunaExecutableIdentity();
  const metadata = ordinaryFile(entry, "Codex executable");
  requireContract((metadata.mode & 0o111) !== 0, "CODEX_LUNA_CLI_NOT_EXECUTABLE", "Codex entry must be executable");
  const cliSha256 = sha256File(entry);
  const helperRoot = codexLunaHelperDirectory(entry);
  const codeModeHostPath = path.join(helperRoot, "codex-code-mode-host");
  const codeModeHostMetadata = ordinaryFile(codeModeHostPath, "Codex code-mode host");
  requireContract((codeModeHostMetadata.mode & 0o111) !== 0, "CODEX_LUNA_CODE_MODE_HOST_NOT_EXECUTABLE", "Codex code-mode host must be executable");
  const codeModeHostSha256 = sha256File(codeModeHostPath);
  requireContract(cliSha256 === expectedExecutable.cli_sha256, "CODEX_LUNA_CLI_SHA256_MISMATCH", "Codex executable does not match the frozen platform identity", { expected: expectedExecutable.cli_sha256, actual: cliSha256 });
  if (expectedExecutable.code_mode_host_sha256 !== null) requireContract(codeModeHostSha256 === expectedExecutable.code_mode_host_sha256, "CODEX_LUNA_CODE_MODE_HOST_SHA256_MISMATCH", "Codex code-mode host does not match the frozen Linux identity");
  let linuxSandbox = null;
  if (expectedExecutable.linux_sandbox_sha256 !== null) {
    const linuxSandboxPath = path.join(helperRoot, "codex-linux-sandbox");
    const linuxSandboxMetadata = ordinaryFile(linuxSandboxPath, "Codex Linux sandbox helper");
    const linuxSandboxSha256 = sha256File(linuxSandboxPath);
    requireContract((linuxSandboxMetadata.mode & 0o111) !== 0 && linuxSandboxSha256 === expectedExecutable.linux_sandbox_sha256, "CODEX_LUNA_LINUX_SANDBOX_IDENTITY_INVALID", "Codex Linux sandbox helper does not match the frozen CLI bytes");
    linuxSandbox = { sha256: linuxSandboxSha256, size: linuxSandboxMetadata.size, path_sha256: sha256Bytes(linuxSandboxPath) };
  }
  const versionProbe = spawnSync(entry, ["--version"], {
    cwd: path.dirname(path.resolve(entry)),
    env: Object.fromEntries(["PATH", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE"].filter((key) => typeof process.env[key] === "string").map((key) => [key, process.env[key]])),
    encoding: "utf8",
    timeout: 30_000,
    stdio: ["ignore", "pipe", "pipe"],
  });
  requireContract(versionProbe.status === 0 && versionProbe.signal === null && !versionProbe.error, "CODEX_LUNA_CLI_VERSION_PROBE_FAILED", "Codex version probe failed", { status: versionProbe.status, signal: versionProbe.signal, cause: versionProbe.error?.code ?? null });
  const version = String(versionProbe.stdout ?? "").split(/\r?\n/).map((line) => line.trim()).find((line) => line.startsWith("codex-cli "));
  requireContract(version === expectedExecutable.version, "CODEX_LUNA_CLI_VERSION_MISMATCH", "Codex CLI version does not match the frozen identity", { expected: expectedExecutable.version, actual: version ?? null });
  const appServerProbe = spawnSync(entry, ["app-server", "--help"], {
    cwd: path.dirname(path.resolve(entry)),
    env: Object.fromEntries(["PATH", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE"].filter((key) => typeof process.env[key] === "string").map((key) => [key, process.env[key]])),
    encoding: "utf8",
    timeout: 30_000,
    stdio: ["ignore", "pipe", "pipe"],
  });
  requireContract(
    appServerProbe.status === 0
      && appServerProbe.signal === null
      && !appServerProbe.error
      && String(appServerProbe.stdout ?? "").includes("--stdio"),
    "CODEX_LUNA_APP_SERVER_PROBE_FAILED",
    "Codex app-server stdio support is unavailable",
  );
  const permissionProbe = spawnSync(entry, ["sandbox", "--help"], {
    cwd: path.dirname(path.resolve(entry)),
    env: Object.fromEntries(["PATH", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE"].filter((key) => typeof process.env[key] === "string").map((key) => [key, process.env[key]])),
    encoding: "utf8",
    timeout: 30_000,
    stdio: ["ignore", "pipe", "pipe"],
  });
  requireContract(
    permissionProbe.status === 0
      && permissionProbe.signal === null
      && !permissionProbe.error
      && String(permissionProbe.stdout ?? "").includes("--permission-profile"),
    "CODEX_LUNA_PERMISSION_PROFILE_PROBE_FAILED",
    "Codex permission-profile support is unavailable",
  );

  ordinaryFile(authFile, "Codex auth source");
  let auth;
  try {
    auth = JSON.parse(fs.readFileSync(authFile, "utf8"));
  } catch (error) {
    fail("CODEX_LUNA_AUTH_INVALID", "Codex auth source must be valid JSON", { cause: error.message });
  }
  requireContract(
    isPlainObject(auth)
      && auth.auth_mode === "chatgpt"
      && (auth.OPENAI_API_KEY === null || auth.OPENAI_API_KEY === undefined)
      && isPlainObject(auth.tokens)
      && ["access_token", "refresh_token", "id_token", "account_id"].every((key) => typeof auth.tokens[key] === "string" && auth.tokens[key].length > 0),
    "CODEX_LUNA_AUTH_NOT_CHATGPT",
    "Codex exploration closure requires auth_mode=chatgpt with a complete tokens object and no API key",
  );
  const authSha256 = sha256File(authFile);
  return {
    schema_version: 1,
    status: "PASS",
    cli: {
      version,
      sha256: cliSha256,
      size: metadata.size,
      platform: process.platform,
      architecture: process.arch,
      entry_path_sha256: sha256Bytes(path.resolve(entry)),
      code_mode_host: {
        sha256: codeModeHostSha256,
        size: codeModeHostMetadata.size,
        path_sha256: sha256Bytes(codeModeHostPath),
      },
      ...(linuxSandbox === null ? {} : { linux_sandbox: linuxSandbox }),
    },
    auth: {
      kind: "chatgpt-external-tokens",
      auth_mode: "chatgpt",
      sha256: authSha256,
      size: fs.statSync(authFile).size,
      account_id_sha256: sha256Bytes(auth.tokens.account_id),
      transfer: "app-server-account-login-start-memory-only",
    },
    filesystem_sandbox: {
      kind: "codex-permission-profile",
      profile_version: CODEX_LUNA_PERMISSION_PROFILE_VERSION,
      enforcement: "single-layer-codex-command-sandbox",
      command_network: "disabled",
      auth_storage: "external-memory-no-auth-file",
      app_server_transport: "stdio-json-rpc",
    },
    model: CODEX_LUNA_MODEL,
    reasoning_effort: CODEX_LUNA_REASONING_EFFORT,
  };
}

export function ordinaryFile(filePath, label = "file") {
  let metadata;
  try {
    metadata = fs.lstatSync(filePath);
  } catch (error) {
    fail("CODEX_LUNA_FILE_MISSING", `${label} is unavailable`, { path: filePath, cause: error?.code ?? "UNKNOWN" });
  }
  requireContract(metadata.isFile() && !metadata.isSymbolicLink() && metadata.nlink === 1, "CODEX_LUNA_FILE_NOT_ORDINARY", `${label} must be one ordinary file`, { path: filePath });
  return metadata;
}

export function ordinaryDirectory(directory, label = "directory") {
  let metadata;
  try {
    metadata = fs.lstatSync(directory);
  } catch (error) {
    fail("CODEX_LUNA_DIRECTORY_MISSING", `${label} is unavailable`, { path: directory, cause: error?.code ?? "UNKNOWN" });
  }
  requireContract(metadata.isDirectory() && !metadata.isSymbolicLink(), "CODEX_LUNA_DIRECTORY_INVALID", `${label} must be one real directory`, { path: directory });
  const resolved = path.resolve(directory);
  const real = fs.realpathSync.native(directory);
  requireContract(process.platform === "win32" ? resolved.toLowerCase() === real.toLowerCase() : resolved === real, "CODEX_LUNA_DIRECTORY_SYMLINKED", `${label} must not contain symlinked path components`, { path: directory });
  return metadata;
}

export function treeManifest(root) {
  ordinaryDirectory(root, "tree root");
  const records = [];
  const visit = (directory, relativeRoot = "") => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true }).sort((left, right) => left.name.localeCompare(right.name))) {
      const absolute = path.join(directory, entry.name);
      const relative = path.posix.join(relativeRoot, entry.name);
      const metadata = fs.lstatSync(absolute);
      requireContract(!metadata.isSymbolicLink(), "CODEX_LUNA_TREE_SYMLINK", "Audited trees cannot contain symlinks", { path: relative });
      if (entry.isDirectory()) visit(absolute, relative);
      else {
        requireContract(entry.isFile() && metadata.nlink === 1, "CODEX_LUNA_TREE_NODE_INVALID", "Audited trees may contain only ordinary files and directories", { path: relative });
        records.push({ path: relative, size: metadata.size, sha256: sha256File(absolute) });
      }
    }
  };
  visit(root);
  return records;
}

export function treeDigest(root) {
  return sha256Bytes(`${canonicalJson(treeManifest(root))}\n`);
}

export function normalizeCodexUsage(value) {
  requireContract(isPlainObject(value), "CODEX_LUNA_USAGE_INVALID", "Codex usage must be one object");
  const normalized = {};
  for (const field of CODEX_LUNA_USAGE_FIELDS) {
    const fieldValue = value[field];
    requireContract(safeInteger(fieldValue), "CODEX_LUNA_USAGE_INVALID", `Codex usage ${field} must be a non-negative safe integer`, { field, value: fieldValue });
    normalized[field] = fieldValue;
  }
  requireContract(normalized.cached_input_tokens <= normalized.input_tokens, "CODEX_LUNA_USAGE_CACHE_INVALID", "cached_input_tokens cannot exceed input_tokens");
  requireContract(normalized.reasoning_output_tokens <= normalized.output_tokens, "CODEX_LUNA_USAGE_REASONING_INVALID", "reasoning_output_tokens cannot exceed output_tokens");
  const totalTokens = normalized.input_tokens + normalized.output_tokens;
  const equivalentUsdUpperBound = roundMoney(
    (normalized.input_tokens * CODEX_LUNA_PRICE_SNAPSHOT.conservative_rates.input
      + normalized.output_tokens * CODEX_LUNA_PRICE_SNAPSHOT.conservative_rates.output) / 1_000_000,
  );
  return {
    schema_version: 1,
    ...normalized,
    total_tokens: totalTokens,
    equivalent_usd_upper_bound: equivalentUsdUpperBound,
  };
}

export function sumCodexUsage(values) {
  const aggregate = Object.fromEntries(CODEX_LUNA_USAGE_FIELDS.map((field) => [field, 0]));
  let equivalentUsdUpperBound = 0;
  for (const value of values) {
    const usage = value?.schema_version === 1 && safeInteger(value?.total_tokens)
      ? value
      : normalizeCodexUsage(value);
    for (const field of CODEX_LUNA_USAGE_FIELDS) aggregate[field] += usage[field];
    equivalentUsdUpperBound += usage.equivalent_usd_upper_bound;
  }
  return {
    schema_version: 1,
    ...aggregate,
    total_tokens: aggregate.input_tokens + aggregate.output_tokens,
    equivalent_usd_upper_bound: roundMoney(equivalentUsdUpperBound),
  };
}

export function buildPosthocBudgetReceipt({ calls, usageComplete }) {
  requireContract(Array.isArray(calls), "CODEX_LUNA_BUDGET_CALLS_INVALID", "Budget calls must be an array");
  const completeUsage = calls
    .map((call) => call?.usage)
    .filter((usage) => usage !== null && usage !== undefined);
  const aggregate = sumCodexUsage(completeUsage);
  const callCountValid = calls.length === CODEX_LUNA_NORMAL_CALLS;
  const withinTokenLimit = aggregate.total_tokens <= CODEX_LUNA_TOKEN_LIMIT;
  const withinCostLimit = aggregate.equivalent_usd_upper_bound <= CODEX_LUNA_EQUIVALENT_USD_LIMIT;
  const complete = Boolean(usageComplete) && completeUsage.length === calls.length;
  return {
    schema_version: 1,
    exception_id: CODEX_LUNA_POSTHOC_EXCEPTION_ID,
    enforcement: "posthoc-only",
    warning: "Token and API-equivalent USD limits are audited after calls and do not prevent spend.",
    usage_formula: CODEX_LUNA_USAGE_FORMULA,
    equivalent_cost_formula: CODEX_LUNA_COST_FORMULA,
    price_snapshot: CODEX_LUNA_PRICE_SNAPSHOT,
    limits: {
      calls: CODEX_LUNA_MAX_CALLS,
      tokens: CODEX_LUNA_TOKEN_LIMIT,
      equivalent_usd: CODEX_LUNA_EQUIVALENT_USD_LIMIT,
    },
    aggregate,
    checks: {
      call_count_valid: callCountValid,
      usage_complete: complete,
      within_token_limit: withinTokenLimit,
      within_equivalent_usd_limit: withinCostLimit,
    },
    status: callCountValid && complete && withinTokenLimit && withinCostLimit ? "PASS_WITH_WARNINGS" : "FAIL",
  };
}

export function auditDiagnosisCommands(commands, { workspaceRoot, forbiddenRoots = [] }) {
  requireContract(Array.isArray(commands), "CODEX_LUNA_COMMANDS_INVALID", "Diagnosis commands must be an array");
  const workspace = path.resolve(workspaceRoot);
  const forbiddenTerms = [
    "mech-target-logs",
    "logparse-config",
    "case.json",
    "oracle",
    "expected_branch",
    "/raw/",
    "\\raw\\",
    "auth.json",
    "$CODEX_HOME",
    "${CODEX_HOME}",
    ".codex/",
    ".codex\\",
  ];
  const violations = [];
  for (const command of commands) {
    requireContract(typeof command === "string", "CODEX_LUNA_COMMAND_INVALID", "Diagnosis commands must be strings");
    const lowered = command.toLowerCase();
    if (forbiddenTerms.some((term) => lowered.includes(term.toLowerCase()))) violations.push({ command, reason: "forbidden-term" });
    if (/\b(?:find|rg\s+--files)\s+\//i.test(command)) violations.push({ command, reason: "absolute-filesystem-scan" });
    if (/(?:^|[\s"'=:(])\.\.(?:[\\/]|$)/.test(command)) violations.push({ command, reason: "parent-traversal" });
    for (const root of forbiddenRoots) {
      const absolute = path.resolve(root);
      if (absolute !== workspace && command.includes(absolute)) violations.push({ command, reason: "forbidden-root" });
    }
  }
  requireContract(violations.length === 0, "CODEX_LUNA_DIAGNOSIS_SCOPE_VIOLATION", "Diagnosis trace accessed Logparse, oracle, raw, or out-of-scope inputs", { violations });
  return {
    schema_version: 1,
    status: "PASS",
    command_count: commands.length,
    logparse_invocations: 0,
    oracle_accesses: 0,
    raw_input_accesses: 0,
    workspace_root_sha256: sha256Bytes(workspace),
  };
}

export function auditGenerationCommands(commands, { workspaceRoot, forbiddenRoots = [] }) {
  requireContract(Array.isArray(commands), "CODEX_LUNA_COMMANDS_INVALID", "Generation commands must be an array");
  const workspace = path.resolve(workspaceRoot);
  const forbiddenTerms = [
    "diagnosis-skill.json",
    "generationspec",
    "generation-spec",
    "verification_contract",
    "verification-contract",
    "case.json",
    "oracle",
    "/raw/",
    "\\raw\\",
    "auth.json",
    "$CODEX_HOME",
    "${CODEX_HOME}",
    ".codex/",
    ".codex\\",
  ];
  const violations = [];
  for (const command of commands) {
    requireContract(typeof command === "string", "CODEX_LUNA_COMMAND_INVALID", "Generation commands must be strings");
    const lowered = command.toLowerCase();
    if (forbiddenTerms.some((term) => lowered.includes(term.toLowerCase()))) violations.push({ command, reason: "forbidden-contract-or-input" });
    if (/\b(?:find|rg\s+--files)\s+\//i.test(command)) violations.push({ command, reason: "absolute-filesystem-scan" });
    if (/(?:^|[\s"'=:(])\.\.(?:[\\/]|$)/.test(command)) violations.push({ command, reason: "parent-traversal" });
    for (const root of forbiddenRoots) {
      const absolute = path.resolve(root);
      if (absolute !== workspace && command.includes(absolute)) violations.push({ command, reason: "forbidden-root" });
    }
  }
  requireContract(violations.length === 0, "CODEX_LUNA_GENERATION_SCOPE_VIOLATION", "Generation trace accessed legacy contracts, oracle/raw inputs, or out-of-scope roots", { violations });
  return {
    schema_version: 1,
    status: "PASS",
    command_count: commands.length,
    legacy_contract_accesses: 0,
    oracle_accesses: 0,
    raw_input_accesses: 0,
    workspace_root_sha256: sha256Bytes(workspace),
  };
}

export function collectSecretCanaries(authFile, environment = {}) {
  ordinaryFile(authFile, "Codex auth source");
  const canaries = new Set();
  const addValue = (value) => {
    if (typeof value === "string" && value.length >= 12) canaries.add(value);
    else if (Array.isArray(value)) value.forEach(addValue);
    else if (isPlainObject(value)) Object.values(value).forEach(addValue);
  };
  const authText = fs.readFileSync(authFile, "utf8");
  try {
    addValue(JSON.parse(authText));
  } catch {
    if (authText.trim().length >= 12) canaries.add(authText.trim());
  }
  for (const [key, value] of Object.entries(environment)) {
    if (/(?:TOKEN|KEY|SECRET|PASSWORD|AUTH|CREDENTIAL)/i.test(key)) addValue(value);
  }
  return [...canaries].sort((left, right) => right.length - left.length);
}

export function auditNoSecretLeak({ roots, canaries }) {
  requireContract(Array.isArray(roots) && Array.isArray(canaries), "CODEX_LUNA_SECRET_AUDIT_INPUT_INVALID", "Secret audit roots and canaries must be arrays");
  const scanned = [];
  for (const root of roots) {
    if (!fs.existsSync(root)) continue;
    const paths = fs.statSync(root).isDirectory() ? treeManifest(root).map((item) => path.join(root, ...item.path.split("/"))) : [root];
    for (const filePath of paths) {
      const payload = fs.readFileSync(filePath);
      scanned.push({ path_sha256: sha256Bytes(path.resolve(filePath)), size: payload.length, sha256: sha256Bytes(payload) });
      const text = payload.toString("utf8");
      for (const canary of canaries) {
        requireContract(!text.includes(canary), "CODEX_LUNA_SECRET_LEAK", "A Codex evidence artifact contains an authentication or environment secret", { path_sha256: sha256Bytes(path.resolve(filePath)), canary_sha256: sha256Bytes(canary) });
      }
    }
  }
  return { schema_version: 1, status: "PASS", scanned_files: scanned.length, scanned };
}

export function verifyMethodsV1Package(skillRoot, sourceWikiIdentity) {
  requireContract(
    isPlainObject(sourceWikiIdentity)
      && sourceWikiIdentity.schema_version === CODEX_LUNA_SOURCE_WIKI_IDENTITY_SCHEMA_VERSION
      && sourceWikiIdentity.algorithm === "sha256"
      && typeof sourceWikiIdentity.sha256 === "string"
      && /^[a-f0-9]{64}$/.test(sourceWikiIdentity.sha256)
      && sourceWikiIdentity.source_path === CODEX_LUNA_SOURCE_WIKI_PATH
      && sourceWikiIdentity.log_template_extraction_version === CODEX_LUNA_LOG_TEMPLATE_EXTRACTION_VERSION
      && Array.isArray(sourceWikiIdentity.log_templates)
      && sourceWikiIdentity.log_templates.every((template) => (
        typeof template === "string"
          && template.length > 0
          && template === template.trim()
          && /\{[A-Za-z_][A-Za-z0-9_]*\}|%[A-Za-z]/.test(template)
      ))
      && typeof sourceWikiIdentity.log_template_inventory_sha256 === "string"
      && /^[a-f0-9]{64}$/.test(sourceWikiIdentity.log_template_inventory_sha256)
      && Object.keys(sourceWikiIdentity).sort().join("\0") === [
        "algorithm",
        "schema_version",
        "sha256",
        "source_path",
        "log_template_extraction_version",
        "log_templates",
        "log_template_inventory_sha256",
      ].sort().join("\0")
      && sourceWikiIdentity.log_template_inventory_sha256 === sha256Bytes(canonicalJson({
        version: CODEX_LUNA_LOG_TEMPLATE_EXTRACTION_VERSION,
        templates: sourceWikiIdentity.log_templates,
      })),
    "CODEX_LUNA_SOURCE_WIKI_IDENTITY_INVALID",
    "Methods-v1 package verification requires one valid closed source Wiki identity v2",
  );
  const rootEntries = fs.readdirSync(skillRoot).sort();
  requireContract(rootEntries.join("\0") === ["SKILL.md", "methods.json", "references"].sort().join("\0"), "CODEX_LUNA_SKILL_FILESET_INVALID", "Generated methods-v1 package root must contain exactly SKILL.md, methods.json, and references");
  ordinaryFile(path.join(skillRoot, "SKILL.md"), "generated SKILL.md");
  ordinaryDirectory(path.join(skillRoot, "references"), "generated references");
  const methodsPath = path.join(skillRoot, "methods.json");
  ordinaryFile(methodsPath, "generated methods.json");
  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(methodsPath, "utf8"));
  } catch (error) {
    fail("CODEX_LUNA_METHODS_JSON_INVALID", "Generated methods.json is invalid JSON", { cause: error.message });
  }
  const exactRootKeys = ["schema_version", "skill_name", "source_wiki_sha256", "required_user_inputs", "required_artifacts", "log_derived_fields", "shared_references", "methods"];
  requireContract(isPlainObject(manifest) && Object.keys(manifest).sort().join("\0") === exactRootKeys.sort().join("\0"), "CODEX_LUNA_METHODS_CONTRACT_INVALID", "Generated methods.json must use the exact methods-v1 root contract");
  requireContract(manifest.schema_version === 1 && manifest.source_wiki_sha256 === sourceWikiIdentity.sha256, "CODEX_LUNA_METHODS_IDENTITY_INVALID", "Generated methods.json identity does not match methods-v1 and the source Wiki");
  requireContract(typeof manifest.skill_name === "string" && manifest.skill_name === path.basename(skillRoot), "CODEX_LUNA_SKILL_NAME_INVALID", "Generated skill name must match its directory");
  requireContract(
    Array.isArray(manifest.shared_references)
      && manifest.shared_references[0] === CODEX_LUNA_SOURCE_LOG_TEMPLATES_REFERENCE,
    "CODEX_LUNA_TEMPLATE_REFERENCE_INVALID",
    `Generated methods.json shared_references[0] must be ${CODEX_LUNA_SOURCE_LOG_TEMPLATES_REFERENCE}`,
  );
  const templateReferencePath = path.join(skillRoot, ...CODEX_LUNA_SOURCE_LOG_TEMPLATES_REFERENCE.split("/"));
  ordinaryFile(templateReferencePath, "source log templates reference");
  requireContract(
    fs.readFileSync(templateReferencePath).equals(buildCodexLunaSourceLogTemplatesBytes(sourceWikiIdentity.log_templates)),
    "CODEX_LUNA_TEMPLATE_REFERENCE_INVALID",
    "Source log templates reference must use the exact frozen Markdown wrapper and contain every inventoried template verbatim, in order, one per line",
  );
  requireContract(Array.isArray(manifest.methods) && manifest.methods.length > 0, "CODEX_LUNA_METHODS_EMPTY", "Generated methods-v1 package must contain at least one method");
  const methodIds = new Set();
  for (const method of manifest.methods) {
    requireContract(isPlainObject(method) && Object.keys(method).sort().join("\0") === ["id", "title", "reference", "priority", "evidence_markers"].sort().join("\0"), "CODEX_LUNA_METHOD_INVALID", "Generated method does not match methods-v1");
    requireContract(typeof method.id === "string" && !methodIds.has(method.id), "CODEX_LUNA_METHOD_ID_INVALID", "Generated method IDs must be non-empty and unique");
    requireContract(method.reference !== CODEX_LUNA_SOURCE_LOG_TEMPLATES_REFERENCE, "CODEX_LUNA_TEMPLATE_REFERENCE_INVALID", "Source log templates reference must remain shared-only and cannot be a method reference");
    methodIds.add(method.id);
    requireContract(Array.isArray(method.evidence_markers) && method.evidence_markers.length > 0, "CODEX_LUNA_METHOD_MARKERS_INVALID", "Every generated method must expose positive evidence markers");
    ordinaryFile(path.join(skillRoot, ...String(method.reference).split("/")), `method reference ${method.reference}`);
  }
  return { manifest, method_ids: [...methodIds], tree_sha256: treeDigest(skillRoot) };
}
