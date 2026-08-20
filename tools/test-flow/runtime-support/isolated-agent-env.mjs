import crypto from "node:crypto";


export const ISOLATED_AGENT_ENV_POLICY_VERSION = "isolated-agent-env-allowlist-v3";
export const ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY = "CLAUDE_CODE_MAX_OUTPUT_TOKENS";
export const ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT = "identity-bound-wrapper-arg+child-only-env+pinned-cli-upper-limit+sealed-runtime-implementation";
// Frozen Claude CLI 2.1.89 reads this exact key with parseInt after each
// StructuredOutput tool-result and emits error_max_structured_output_retries
// when the observed call count reaches the configured value.
export const ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY = "MAX_STRUCTURED_OUTPUT_RETRIES";
export const ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_LIMIT = 2;
export const ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_ENFORCEMENT = "identity-bound-child-only-env+frozen-claude-cli-2.1.89-counter+limit-2";

export const ISOLATED_AGENT_AMBIENT_KEYS = Object.freeze([
  "PATH",
  "HOME",
  "USERPROFILE",
  "SystemRoot",
  "TEMP",
  "TMP",
  "TMPDIR",
  "LANG",
  "LC_ALL",
  "LC_CTYPE",
]);

const ISOLATED_AGENT_INBOUND_ONLY_KEYS = Object.freeze([
  "PYTEST_CURRENT_TEST",
  "PYTEST_VERSION",
  "HOMEDRIVE",
  "HOMEPATH",
  "SystemDrive",
  "USERDOMAIN",
  "USERNAME",
]);

const ISOLATED_AGENT_CLAUDE_CHILD_ONLY_KEYS = Object.freeze([
  ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY,
  ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY,
]);

export const ISOLATED_AGENT_EXPLICIT_KEYS = Object.freeze([
  "PYTHONNOUSERSITE",
  "PYTHONPYCACHEPREFIX",
  "CLAUDE_CONFIG_DIR",
  "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
  "TEST_FLOW_AGENT_BACKEND_WALL_TIME_SECONDS",
  "S08_REAL_AGENT_COMMAND",
  "S08_REAL_GENERIC_LOCATOR_AGENT_COMMAND",
  "S08_REAL_SKILL_GENERATION_AGENT_COMMAND",
  "S08_REAL_SKILL_GENERATION_AUDIT_PATH",
  "S08_REAL_ROUTE_AGENT_COMMAND",
  "S08_REAL_DIAGNOSE_AGENT_COMMAND",
  "S08_REAL_REVIEW_AGENT_COMMAND",
  "S08_REAL_AGENT_GATE",
  "S08_REAL_GENERIC_LOCATOR_GATE",
  "S08_REAL_SKILL_GENERATION_GATE",
  "S08_REAL_ROUTE_AGENT_GATE",
  "S08_REAL_DIAGNOSE_AGENT_V3_MATRIX_GATE",
  "S08_REAL_FIRST_LOG_AGENT_GATE",
  "S08_REAL_REVIEW_AGENT_GATE",
  "S08_RELEASE_CASES_ROOT",
  "S08_REAL_DIAGNOSE_SKILL_PATH",
  "LOGPARSE_REPO",
  "LOGPARSE_CONFIG_PATH",
  "LOGPARSE_PYTHON",
]);

const SESSION_CREDENTIAL_KEYS = Object.freeze([
  "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT",
  "PROBLEM_LOCATOR_LOGPARSE_TOKEN",
]);
const EXPLICIT_KEY_SET = new Set(ISOLATED_AGENT_EXPLICIT_KEYS);
const CLAUDE_CHILD_ONLY_KEY_SET = new Set(ISOLATED_AGENT_CLAUDE_CHILD_ONLY_KEYS);

function requireEnvironment(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label}_INVALID`);
  for (const [name, item] of Object.entries(value)) {
    if (!name || name.includes("\0") || name.includes("=") || typeof item !== "string" || item.includes("\0")) {
      throw new Error(`${label}_INVALID`);
    }
  }
  return value;
}

function ambientValue(environment, canonicalName, platform) {
  if (Object.hasOwn(environment, canonicalName)) return environment[canonicalName];
  if (platform !== "win32") return undefined;
  const match = Object.keys(environment).find((name) => name.toLowerCase() === canonicalName.toLowerCase());
  return match === undefined ? undefined : environment[match];
}

function sessionCredentials(environment, allowSessionCredentials) {
  const present = SESSION_CREDENTIAL_KEYS.filter((name) => Object.hasOwn(environment, name));
  if (!allowSessionCredentials) return {};
  if (present.length !== 0 && present.length !== SESSION_CREDENTIAL_KEYS.length) {
    throw new Error("ISOLATED_AGENT_SESSION_CREDENTIALS_INCOMPLETE");
  }
  return Object.fromEntries(present.map((name) => [name, environment[name]]));
}

export function buildIsolatedAgentEnvironment({
  ambient,
  explicit = {},
  allowSessionCredentials = false,
  allowClaudeChildControls = false,
  platform = process.platform,
} = {}) {
  requireEnvironment(ambient, "ISOLATED_AGENT_AMBIENT_ENVIRONMENT");
  requireEnvironment(explicit, "ISOLATED_AGENT_EXPLICIT_ENVIRONMENT");
  for (const name of Object.keys(explicit)) {
    if (!EXPLICIT_KEY_SET.has(name) && !(allowClaudeChildControls && CLAUDE_CHILD_ONLY_KEY_SET.has(name))) {
      throw new Error(`ISOLATED_AGENT_EXPLICIT_KEY_FORBIDDEN:${name}`);
    }
  }
  const environment = {};
  for (const name of ISOLATED_AGENT_AMBIENT_KEYS) {
    const value = ambientValue(ambient, name, platform);
    if (value !== undefined) environment[name] = value;
  }
  Object.assign(environment, explicit, sessionCredentials(ambient, allowSessionCredentials));
  return environment;
}

export function assertIsolatedAgentInboundEnvironment(environment, { allowSessionCredentials = true, platform = process.platform } = {}) {
  requireEnvironment(environment, "ISOLATED_AGENT_INBOUND_ENVIRONMENT");
  const allowed = new Set([
    ...ISOLATED_AGENT_AMBIENT_KEYS,
    ...ISOLATED_AGENT_EXPLICIT_KEYS,
    ...ISOLATED_AGENT_INBOUND_ONLY_KEYS,
  ]);
  if (allowSessionCredentials) SESSION_CREDENTIAL_KEYS.forEach((name) => allowed.add(name));
  const allowedNames = platform === "win32"
    ? new Set([...allowed].map((name) => name.toLowerCase()))
    : allowed;
  const forbidden = Object.keys(environment).filter((name) => (
    platform === "win32" ? !allowedNames.has(name.toLowerCase()) : !allowedNames.has(name)
  )).sort();
  if (forbidden.length > 0) throw new Error(`ISOLATED_AGENT_INBOUND_KEY_FORBIDDEN:${forbidden.join(",")}`);
  sessionCredentials(environment, allowSessionCredentials);
  return environmentKeySummary(environment);
}

export function environmentKeySummary(environment) {
  requireEnvironment(environment, "ISOLATED_AGENT_ENVIRONMENT_SUMMARY_SOURCE");
  const keyNames = Object.keys(environment).sort();
  return {
    schema_version: 1,
    key_count: keyNames.length,
    key_names: keyNames,
    key_names_sha256: crypto.createHash("sha256").update(`${JSON.stringify(keyNames)}\n`).digest("hex"),
  };
}

export function validEnvironmentKeySummary(summary) {
  if (!summary || summary.schema_version !== 1 || !Number.isSafeInteger(summary.key_count) || !Array.isArray(summary.key_names)) return false;
  if (summary.key_names.some((name) => typeof name !== "string") || summary.key_names.join("\0") !== [...summary.key_names].sort().join("\0")) return false;
  const expected = environmentKeySummary(Object.fromEntries(summary.key_names.map((name) => [name, ""])));
  return summary.key_count === expected.key_count && summary.key_names_sha256 === expected.key_names_sha256;
}

export function explicitEnvironmentFrom(environment, names) {
  requireEnvironment(environment, "ISOLATED_AGENT_EXPLICIT_SOURCE");
  if (!Array.isArray(names) || names.some((name) => !EXPLICIT_KEY_SET.has(name))) throw new Error("ISOLATED_AGENT_EXPLICIT_SELECTION_INVALID");
  return Object.fromEntries(names.filter((name) => Object.hasOwn(environment, name)).map((name) => [name, environment[name]]));
}
