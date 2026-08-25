import crypto from "node:crypto";
import path from "node:path";

import {
  CODEX_LUNA_CLI_VERSION,
  CODEX_LUNA_MODEL,
  CODEX_LUNA_REASONING_EFFORT,
} from "./codex-luna-contract.mjs";
import { MACOS_CODEX_LUNA_PUBLIC_TOOLS } from "../quick-validation/codex-luna/runtime/macos-codex-luna-e2e-contract.mjs";

export const CODEX_LUNA_APP_SERVER_SCHEMA_VERSION = 1;
export const CODEX_LUNA_APP_SERVER_PROTOCOL_VERSION = "v2";
export const CODEX_LUNA_APP_SERVER_TRANSPORT = "jsonl-stdio";
export const CODEX_LUNA_APP_SERVER_SESSION_SOURCE = "vscode";
export const CODEX_LUNA_APP_SERVER_CLIENT = Object.freeze({
  name: "xiaodao_test_flow",
  title: "Xiaodao Test Flow",
  version: "1.0.0",
});
export const CODEX_LUNA_APP_SERVER_REQUEST_IDS = Object.freeze({
  initialize: 0,
  login: 1,
  accountRead: 2,
  permissionProfileList: 3,
  skillsList: 4,
  threadStart: 5,
  turnStart: 6,
});
export const CODEX_LUNA_SYSTEM_SKILL_NAMES = Object.freeze([
  "imagegen",
  "openai-docs",
  "plugin-creator",
  "review-agent",
  "skill-creator",
  "skill-installer",
]);
export const CODEX_LUNA_RAW_SHELL_FUNCTION_NAMES = Object.freeze(["shell_command"]);
export const CODEX_LUNA_RAW_CUSTOM_TOOL_NAMES = Object.freeze(["apply_patch", "exec", "wait"]);
export const CODEX_LUNA_RAW_RESPONSE_ITEM_TYPES_ALLOWED = Object.freeze([
  "message",
  "reasoning",
  "local_shell_call",
  "function_call",
  "function_call_output",
  "custom_tool_call",
  "custom_tool_call_output",
]);
export const CODEX_LUNA_RAW_RESPONSE_ITEM_SANITIZER_FIELDS = Object.freeze({
  message: Object.freeze(["type", "role"]),
  reasoning: Object.freeze(["type"]),
  local_shell_call: Object.freeze(["type", "call_id", "status", "action.type"]),
  function_call: Object.freeze(["type", "name", "namespace", "call_id"]),
  function_call_output: Object.freeze(["type", "call_id"]),
  custom_tool_call: Object.freeze(["type", "name", "namespace", "call_id", "status"]),
  custom_tool_call_output: Object.freeze(["type", "name", "call_id"]),
});
export const CODEX_LUNA_DISABLED_FEATURES = Object.freeze([
  "apps",
  "auth_elicitation",
  "browser_use",
  "browser_use_external",
  "browser_use_full_cdp_access",
  "computer_use",
  "current_time_reminder",
  "default_mode_request_user_input",
  "deferred_executor",
  "enable_mcp_apps",
  "executor_capability_discovery",
  "external_agent_memory_import",
  "goals",
  "guardian_approval",
  "hooks",
  "image_generation",
  "in_app_browser",
  "in_app_chat",
  "memories",
  "multi_agent",
  "multi_agent_v2",
  "network_proxy",
  "plugin_sharing",
  "plugins",
  "realtime_conversation",
  "recommended_plugins",
  "remote_compaction_v2",
  "remote_plugin",
  "request_permissions_tool",
  "shell_snapshot",
  "skill_mcp_dependency_install",
  "skill_search",
  "standalone_web_search",
  "tool_call_mcp_elicitation",
  "tool_suggest",
  "unbounded_connection_retries",
  "view_image",
  "workspace_dependencies",
]);

const PROFILE_PREFIX = "test-flow-codex-luna";
const RAW_RESPONSE_ITEM_TYPES_ALLOWED = new Set(CODEX_LUNA_RAW_RESPONSE_ITEM_TYPES_ALLOWED);
const RAW_SHELL_FUNCTION_NAMES = new Set(CODEX_LUNA_RAW_SHELL_FUNCTION_NAMES);
const RAW_CUSTOM_TOOL_NAMES = new Set(CODEX_LUNA_RAW_CUSTOM_TOOL_NAMES);
export const CODEX_LUNA_RAW_MESSAGE_ROLES_ALLOWED = Object.freeze(["assistant", "developer", "system", "user"]);
const RAW_MESSAGE_ROLES_ALLOWED = new Set(CODEX_LUNA_RAW_MESSAGE_ROLES_ALLOWED);
const MODE_POLICY = Object.freeze({
  generation: Object.freeze({ workspace_access: "write", network_enabled: false, mcp: false }),
  diagnosis: Object.freeze({ workspace_access: "read", network_enabled: false, mcp: false }),
  service: Object.freeze({ workspace_access: "write", network_enabled: true, mcp: false }),
  client: Object.freeze({ workspace_access: "read", network_enabled: true, mcp: true }),
});
const THREAD_ITEM_TYPES_ALLOWED = new Set([
  "userMessage",
  "agentMessage",
  "reasoning",
  "commandExecution",
]);
const NOTIFICATIONS_ALLOWED = new Set([
  "account/login/completed",
  "account/updated",
  "thread/started",
  "thread/status/changed",
  "thread/tokenUsage/updated",
  "turn/started",
  "turn/completed",
  "item/started",
  "item/completed",
  "item/agentMessage/delta",
  "item/reasoning/summaryTextDelta",
  "item/reasoning/summaryPartAdded",
  "item/reasoning/textDelta",
  "item/commandExecution/outputDelta",
  "item/commandExecution/terminalInteraction",
  "rawResponseItem/completed",
  "rawResponse/completed",
  "turn/diff/updated",
  "account/rateLimits/updated",
  "warning",
]);

function notificationAllowed(method, mode) {
  return NOTIFICATIONS_ALLOWED.has(method) || (mode === "client" && method.startsWith("mcpServer/"));
}
const TURN_SCOPED_DELTA_NOTIFICATIONS = new Set([
  "item/agentMessage/delta",
  "item/reasoning/summaryTextDelta",
  "item/reasoning/summaryPartAdded",
  "item/reasoning/textDelta",
  "item/commandExecution/outputDelta",
  "item/commandExecution/terminalInteraction",
  "turn/diff/updated",
]);

export class CodexLunaAppServerError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "CodexLunaAppServerError";
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) {
  throw new CodexLunaAppServerError(code, message, details);
}

function requireAppServer(condition, code, message, details = {}) {
  if (!condition) fail(code, message, details);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function isRequestId(value) {
  return (typeof value === "string" && value.length > 0)
    || (Number.isSafeInteger(value) && value >= 0);
}

function isNonEmptyString(value) {
  return typeof value === "string" && value.length > 0;
}

function sha256Bytes(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function safeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function cloneJson(value, label) {
  try {
    return JSON.parse(JSON.stringify(value));
  } catch {
    fail("CODEX_LUNA_APP_SERVER_JSON_VALUE_INVALID", `${label} must be JSON serializable`);
  }
}

function normalizedWorkspaceRoot(workspaceRoot) {
  requireAppServer(isNonEmptyString(workspaceRoot), "CODEX_LUNA_APP_SERVER_WORKSPACE_INVALID", "Workspace root must be a non-empty absolute path");
  requireAppServer(!workspaceRoot.includes("\0") && !/[\r\n]/.test(workspaceRoot), "CODEX_LUNA_APP_SERVER_WORKSPACE_INVALID", "Workspace root contains forbidden characters");
  requireAppServer(path.isAbsolute(workspaceRoot), "CODEX_LUNA_APP_SERVER_WORKSPACE_INVALID", "Workspace root must be absolute");
  const resolved = path.resolve(workspaceRoot);
  requireAppServer(resolved !== path.parse(resolved).root, "CODEX_LUNA_APP_SERVER_WORKSPACE_TOO_BROAD", "Filesystem root cannot be an invocation workspace");
  return resolved;
}

function normalizedSkillPath(skillPath, workspaceRoot, privateSkillRoot = null) {
  requireAppServer(isNonEmptyString(skillPath) && path.isAbsolute(skillPath), "CODEX_LUNA_APP_SERVER_SKILL_PATH_INVALID", "Skill path must be absolute");
  requireAppServer(!skillPath.includes("\0") && !/[\r\n]/.test(skillPath), "CODEX_LUNA_APP_SERVER_SKILL_PATH_INVALID", "Skill path contains forbidden characters");
  const resolved = path.resolve(skillPath);
  requireAppServer(path.basename(resolved) === "SKILL.md", "CODEX_LUNA_APP_SERVER_SKILL_PATH_INVALID", "Skill config path must point to SKILL.md itself");
  const insideWorkspace = pathIsInside(workspaceRoot, resolved) && resolved !== workspaceRoot;
  const insidePrivateSkillRoot = privateSkillRoot !== null && pathIsInside(privateSkillRoot, resolved) && resolved !== privateSkillRoot;
  requireAppServer(insideWorkspace || insidePrivateSkillRoot, "CODEX_LUNA_APP_SERVER_SKILL_OUTSIDE_WORKSPACE", "Skill path must stay inside the invocation workspace or the service invocation's isolated Codex home");
  return resolved;
}

function normalizedPrivateDirectory(directory, label) {
  requireAppServer(isNonEmptyString(directory) && path.isAbsolute(directory), "CODEX_LUNA_APP_SERVER_PRIVATE_PATH_INVALID", `${label} must be absolute`);
  requireAppServer(!directory.includes("\0") && !/[\r\n]/.test(directory), "CODEX_LUNA_APP_SERVER_PRIVATE_PATH_INVALID", `${label} contains forbidden characters`);
  const resolved = path.resolve(directory);
  requireAppServer(resolved !== path.parse(resolved).root, "CODEX_LUNA_APP_SERVER_PRIVATE_PATH_TOO_BROAD", `${label} cannot be the filesystem root`);
  return resolved;
}

function pathIsInside(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function systemSkillPaths(codexHome) {
  return CODEX_LUNA_SYSTEM_SKILL_NAMES.map((name) => path.join(codexHome, "skills", ".system", name, "SKILL.md"));
}

function skillName(skillPath) {
  const value = path.basename(path.dirname(skillPath));
  requireAppServer(/^[a-z0-9][a-z0-9-]{0,63}$/.test(value), "CODEX_LUNA_APP_SERVER_SKILL_NAME_INVALID", "Configured skill directory does not provide a safe skill name");
  return value;
}

function normalizedMode(mode) {
  requireAppServer(Object.hasOwn(MODE_POLICY, mode), "CODEX_LUNA_APP_SERVER_MODE_INVALID", "App-server invocation mode must be generation, diagnosis, service, or client");
  return mode;
}

function rawCustomToolAllowed(name, mode) {
  return (name === "apply_patch" && ["generation", "service"].includes(mode)) || name === "exec" || name === "wait";
}

function normalizedMcpServer(value, mode) {
  const policy = MODE_POLICY[mode];
  if (!policy.mcp) {
    requireAppServer(value === null, "CODEX_LUNA_APP_SERVER_MCP_MODE_INVALID", "Only client mode may configure an MCP server");
    return null;
  }
  requireAppServer(isPlainObject(value), "CODEX_LUNA_APP_SERVER_MCP_INVALID", "Client mode requires one MCP server configuration");
  requireAppServer(value.name === "problem-locator", "CODEX_LUNA_APP_SERVER_MCP_INVALID", "MCP server key must be problem-locator");
  let endpoint;
  try { endpoint = new URL(value.url); } catch { fail("CODEX_LUNA_APP_SERVER_MCP_INVALID", "MCP URL is invalid"); }
  requireAppServer(endpoint.protocol === "http:" && endpoint.hostname === "127.0.0.1" && endpoint.pathname === "/mcp" && endpoint.search === "" && endpoint.hash === "" && endpoint.username === "" && endpoint.password === "", "CODEX_LUNA_APP_SERVER_MCP_INVALID", "MCP URL must be an unauthenticated IPv4 loopback /mcp endpoint");
  const enabledTools = value.enabled_tools ?? MACOS_CODEX_LUNA_PUBLIC_TOOLS;
  requireAppServer(Array.isArray(enabledTools) && enabledTools.length === MACOS_CODEX_LUNA_PUBLIC_TOOLS.length && new Set(enabledTools).size === enabledTools.length && [...enabledTools].sort().join("\0") === [...MACOS_CODEX_LUNA_PUBLIC_TOOLS].sort().join("\0"), "CODEX_LUNA_APP_SERVER_MCP_TOOLS_INVALID", "MCP enabled_tools must be the exact seven-tool allowlist");
  const startupTimeout = value.startup_timeout_sec ?? 10;
  const toolTimeout = value.tool_timeout_sec ?? 600;
  requireAppServer(Number.isSafeInteger(startupTimeout) && startupTimeout > 0 && Number.isSafeInteger(toolTimeout) && toolTimeout >= 30, "CODEX_LUNA_APP_SERVER_MCP_TIMEOUT_INVALID", "MCP startup/tool timeouts are invalid");
  return Object.freeze({
    name: "problem-locator",
    url: endpoint.href,
    required: true,
    enabled: true,
    enabled_tools: Object.freeze([...enabledTools]),
    startup_timeout_sec: startupTimeout,
    tool_timeout_sec: toolTimeout,
    default_tools_approval_mode: "approve",
  });
}

function tomlString(value) {
  requireAppServer(typeof value === "string" && !value.includes("\0"), "CODEX_LUNA_APP_SERVER_TOML_STRING_INVALID", "TOML string contains a forbidden character");
  return JSON.stringify(value);
}

function exactObjectKeys(value, expected) {
  return isPlainObject(value)
    && Object.keys(value).sort().join("\0") === [...expected].sort().join("\0");
}

function assertNoSecret(value, secretValues) {
  const rendered = typeof value === "string" ? value : JSON.stringify(value);
  for (const secret of secretValues) {
    if (typeof secret === "string" && secret.length >= 8 && rendered.includes(secret)) {
      fail("CODEX_LUNA_APP_SERVER_SECRET_LEAK", "App-server transcript or evidence contains a credential canary");
    }
  }
}

export function codexLunaPermissionProfileId(mode) {
  return `${PROFILE_PREFIX}-${normalizedMode(mode)}`;
}

export function buildCodexLunaAppServerArguments() {
  return [
    "app-server",
    "--stdio",
    "--strict-config",
    ...CODEX_LUNA_DISABLED_FEATURES.flatMap((feature) => ["--disable", feature]),
  ];
}

/**
 * Build the complete config.toml bytes for one isolated app-server process.
 *
 * The config intentionally uses only beta permission profiles. In particular,
 * it contains no legacy sandbox_mode/sandbox_workspace_write setting. The
 * app-server itself may reach the provider. Generation/diagnosis commands have
 * no network; the test-owned service/client modes deliberately enable command
 * network for the loopback broker, MCP transport, and descriptor-authorized PUT.
 * Every mode can see only :minimal plus its one invocation workspace.
 */
export function buildCodexLunaIsolatedConfig({
  workspaceRoot,
  skillPath,
  codexHome,
  shellHome = null,
  shellPath = "/usr/bin:/bin:/usr/sbin:/sbin",
  shellLang = "C.UTF-8",
  mode,
  mcpServer = null,
  disabledSkillPaths = [],
}) {
  const normalizedModeValue = normalizedMode(mode);
  const modePolicy = MODE_POLICY[normalizedModeValue];
  const normalizedMcp = normalizedMcpServer(mcpServer, normalizedModeValue);
  const normalizedRoot = normalizedWorkspaceRoot(workspaceRoot);
  const normalizedCodexHome = normalizedPrivateDirectory(codexHome, "Codex home");
  const normalizedSkill = normalizedSkillPath(skillPath, normalizedRoot, normalizedModeValue === "service" ? normalizedCodexHome : null);
  const normalizedShellHome = normalizedPrivateDirectory(shellHome ?? path.join(normalizedRoot, ".shell-home"), "Shell home");
  requireAppServer(!pathIsInside(normalizedRoot, normalizedCodexHome) && !pathIsInside(normalizedCodexHome, normalizedRoot), "CODEX_LUNA_APP_SERVER_PRIVATE_PATH_OVERLAP", "Codex home and the invocation workspace must be disjoint");
  requireAppServer(normalizedModeValue === "service" || pathIsInside(normalizedRoot, normalizedShellHome), "CODEX_LUNA_APP_SERVER_SHELL_HOME_OUTSIDE_WORKSPACE", "Shell home must stay inside the invocation workspace except for the standalone service invocation's private home");
  requireAppServer(!pathIsInside(normalizedCodexHome, normalizedShellHome) && !pathIsInside(normalizedShellHome, normalizedCodexHome), "CODEX_LUNA_APP_SERVER_PRIVATE_PATH_OVERLAP", "Shell home and Codex home must be disjoint");
  requireAppServer(isNonEmptyString(shellPath) && !/[\0\r\n]/.test(shellPath), "CODEX_LUNA_APP_SERVER_SHELL_PATH_INVALID", "Shell PATH is invalid");
  requireAppServer(isNonEmptyString(shellLang) && !/[\0\r\n]/.test(shellLang), "CODEX_LUNA_APP_SERVER_SHELL_LANG_INVALID", "Shell LANG is invalid");
  const disabledSystemSkills = systemSkillPaths(normalizedCodexHome);
  requireAppServer(Array.isArray(disabledSkillPaths), "CODEX_LUNA_APP_SERVER_DISABLED_SKILLS_INVALID", "Additional disabled Skill paths must be an array");
  const additionalDisabledSkills = disabledSkillPaths.map((configuredSkill) => {
    requireAppServer(isNonEmptyString(configuredSkill) && path.isAbsolute(configuredSkill) && path.basename(configuredSkill) === "SKILL.md" && !configuredSkill.includes("\0") && !/[\r\n]/.test(configuredSkill), "CODEX_LUNA_APP_SERVER_DISABLED_SKILLS_INVALID", "Additional disabled Skill path is invalid");
    return path.resolve(configuredSkill);
  });
  requireAppServer(new Set(additionalDisabledSkills).size === additionalDisabledSkills.length && !additionalDisabledSkills.includes(normalizedSkill), "CODEX_LUNA_APP_SERVER_DISABLED_SKILLS_INVALID", "Additional disabled Skill paths are duplicated or disable the intended Skill");
  const allDisabledSkills = [...disabledSystemSkills, ...additionalDisabledSkills];
  const profileId = codexLunaPermissionProfileId(normalizedModeValue);
  const workspaceAccess = modePolicy.workspace_access;
  const brokerEnvironmentKeys = normalizedModeValue === "service"
    ? ["PROBLEM_LOCATOR_LOGPARSE_ENDPOINT", "PROBLEM_LOCATOR_LOGPARSE_TOKEN"]
    : [];
  const shellEnvironmentKeys = ["PATH", "LANG", "HOME", "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE", ...brokerEnvironmentKeys];
  const configToml = [
    `model = ${tomlString(CODEX_LUNA_MODEL)}`,
    `model_reasoning_effort = ${tomlString(CODEX_LUNA_REASONING_EFFORT)}`,
    "approval_policy = \"never\"",
    `default_permissions = ${tomlString(profileId)}`,
    "web_search = \"disabled\"",
    "allow_login_shell = false",
    "project_doc_max_bytes = 0",
    "",
    "[analytics]",
    "enabled = false",
    "",
    "[history]",
    "persistence = \"none\"",
    "",
    "[agents]",
    "enabled = false",
    "",
    "[apps._default]",
    "enabled = false",
    "",
    ...(normalizedMcp === null ? [] : [
      `[mcp_servers.${JSON.stringify(normalizedMcp.name)}]`,
      `url = ${tomlString(normalizedMcp.url)}`,
      `required = ${String(normalizedMcp.required)}`,
      `enabled = ${String(normalizedMcp.enabled)}`,
      `enabled_tools = [${normalizedMcp.enabled_tools.map((tool) => tomlString(tool)).join(", ")}]`,
      `startup_timeout_sec = ${normalizedMcp.startup_timeout_sec}`,
      `tool_timeout_sec = ${normalizedMcp.tool_timeout_sec}`,
      `default_tools_approval_mode = ${tomlString(normalizedMcp.default_tools_approval_mode)}`,
      "",
    ]),
    "[shell_environment_policy]",
    `inherit = ${tomlString(normalizedModeValue === "service" ? "all" : "none")}`,
    `ignore_default_excludes = ${String(normalizedModeValue === "service")}`,
    ...(normalizedModeValue === "service" ? [`include_only = [${shellEnvironmentKeys.map((key) => tomlString(key)).join(", ")}]`] : []),
    "",
    "[shell_environment_policy.set]",
    `PATH = ${tomlString(shellPath)}`,
    `LANG = ${tomlString(shellLang)}`,
    `HOME = ${tomlString(normalizedShellHome)}`,
    "PYTHONDONTWRITEBYTECODE = \"1\"",
    "PYTHONNOUSERSITE = \"1\"",
    "",
    ...allDisabledSkills.flatMap((configuredSkill) => [
      "[[skills.config]]",
      `path = ${tomlString(configuredSkill)}`,
      "enabled = false",
      "",
    ]),
    "[[skills.config]]",
    `path = ${tomlString(normalizedSkill)}`,
    "enabled = true",
    "",
    `[permissions.${profileId}]`,
    `description = ${tomlString(`Test Flow ${normalizedModeValue} least-privilege boundary.`)}`,
    "",
    `[permissions.${profileId}.workspace_roots]`,
    `${tomlString(normalizedRoot)} = true`,
    "",
    `[permissions.${profileId}.filesystem]`,
    "\":root\" = \"deny\"",
    "\":minimal\" = \"read\"",
    "",
    `[permissions.${profileId}.filesystem.\":workspace_roots\"]`,
    `"." = ${tomlString(workspaceAccess)}`,
    "",
    `[permissions.${profileId}.network]`,
    `enabled = ${String(modePolicy.network_enabled)}`,
    "",
  ].join("\n");
  requireAppServer(!/(?:^|\n)\s*sandbox_mode\s*=/.test(configToml), "CODEX_LUNA_APP_SERVER_LEGACY_SANDBOX_PRESENT", "Isolated config cannot contain sandbox_mode");
  requireAppServer(!/(?:^|\n)\s*\[sandbox_workspace_write\]/.test(configToml), "CODEX_LUNA_APP_SERVER_LEGACY_SANDBOX_PRESENT", "Isolated config cannot contain sandbox_workspace_write");
  return Object.freeze({
    schema_version: CODEX_LUNA_APP_SERVER_SCHEMA_VERSION,
    profile_id: profileId,
    invocation_mode: normalizedModeValue,
    workspace_root: normalizedRoot,
    skill_path: normalizedSkill,
    skill_name: skillName(normalizedSkill),
    codex_home: normalizedCodexHome,
    codex_home_sha256: sha256Bytes(normalizedCodexHome),
    disabled_system_skill_paths: Object.freeze([...disabledSystemSkills]),
    disabled_additional_skill_paths: Object.freeze([...additionalDisabledSkills]),
    shell_home: normalizedShellHome,
    shell_path: shellPath,
    shell_lang: shellLang,
    shell_environment: Object.freeze({
      inherit: normalizedModeValue === "service" ? "all" : "none",
      keys: Object.freeze(shellEnvironmentKeys),
      broker_keys: Object.freeze(brokerEnvironmentKeys),
      home_sha256: sha256Bytes(normalizedShellHome),
      path_sha256: sha256Bytes(shellPath),
      lang_sha256: sha256Bytes(shellLang),
      codex_home_forwarded: false,
    }),
    workspace_access: workspaceAccess,
    root_access: "deny",
    minimal_access: "read",
    network_enabled: modePolicy.network_enabled,
    mcp_server: normalizedMcp,
    config_toml: configToml,
    config_byte_count: Buffer.byteLength(configToml),
    config_sha256: sha256Bytes(configToml),
  });
}

export function buildCodexLunaInitializeRequest({
  id = CODEX_LUNA_APP_SERVER_REQUEST_IDS.initialize,
  client = CODEX_LUNA_APP_SERVER_CLIENT,
} = {}) {
  requireAppServer(isRequestId(id), "CODEX_LUNA_APP_SERVER_REQUEST_ID_INVALID", "Initialize request id is invalid");
  requireAppServer(
    isPlainObject(client)
      && isNonEmptyString(client.name)
      && (client.title === null || isNonEmptyString(client.title))
      && isNonEmptyString(client.version),
    "CODEX_LUNA_APP_SERVER_CLIENT_INVALID",
    "App-server client identity is invalid",
  );
  return {
    method: "initialize",
    id,
    params: {
      clientInfo: { name: client.name, title: client.title, version: client.version },
      capabilities: {
        experimentalApi: true,
        requestAttestation: false,
        mcpServerOpenaiFormElicitation: false,
        optOutNotificationMethods: ["remoteControl/status/changed"],
        extensions: {},
      },
    },
  };
}

export function buildCodexLunaInitializedNotification() {
  return { method: "initialized", params: {} };
}

export function buildCodexLunaAccountReadRequest({ id = CODEX_LUNA_APP_SERVER_REQUEST_IDS.accountRead } = {}) {
  requireAppServer(isRequestId(id), "CODEX_LUNA_APP_SERVER_REQUEST_ID_INVALID", "Account read request id is invalid");
  return { method: "account/read", id, params: { refreshToken: false } };
}

export function buildCodexLunaPermissionProfileListRequest({
  workspaceRoot,
  id = CODEX_LUNA_APP_SERVER_REQUEST_IDS.permissionProfileList,
} = {}) {
  requireAppServer(isRequestId(id), "CODEX_LUNA_APP_SERVER_REQUEST_ID_INVALID", "Permission profile list request id is invalid");
  return { method: "permissionProfile/list", id, params: { cwd: normalizedWorkspaceRoot(workspaceRoot), limit: 100 } };
}

export function buildCodexLunaSkillsListRequest({
  workspaceRoot,
  id = CODEX_LUNA_APP_SERVER_REQUEST_IDS.skillsList,
} = {}) {
  requireAppServer(isRequestId(id), "CODEX_LUNA_APP_SERVER_REQUEST_ID_INVALID", "Skills list request id is invalid");
  const root = normalizedWorkspaceRoot(workspaceRoot);
  return { method: "skills/list", id, params: { cwds: [root], forceReload: true } };
}

/**
 * Write the one credential-bearing request directly to app-server stdin.
 *
 * Deliberately no builder returns the credential-bearing envelope. The return
 * value is a safe audit stub, and failures never echo the request or the token.
 */
export function writeExternalChatgptAuthLoginRequest(writable, {
  id = CODEX_LUNA_APP_SERVER_REQUEST_IDS.login,
  accessToken,
  chatgptAccountId,
  chatgptPlanType = null,
}) {
  requireAppServer(writable && typeof writable.write === "function", "CODEX_LUNA_APP_SERVER_AUTH_WRITER_INVALID", "External auth requires a writable stdin sink");
  requireAppServer(isRequestId(id), "CODEX_LUNA_APP_SERVER_REQUEST_ID_INVALID", "Login request id is invalid");
  requireAppServer(isNonEmptyString(accessToken), "CODEX_LUNA_APP_SERVER_ACCESS_TOKEN_INVALID", "External ChatGPT access token is missing");
  requireAppServer(isNonEmptyString(chatgptAccountId), "CODEX_LUNA_APP_SERVER_ACCOUNT_INVALID", "External ChatGPT account id is missing");
  requireAppServer(chatgptPlanType === null || isNonEmptyString(chatgptPlanType), "CODEX_LUNA_APP_SERVER_PLAN_TYPE_INVALID", "External ChatGPT plan type is invalid");
  let accepted;
  try {
    accepted = writable.write(`${JSON.stringify({
      method: "account/login/start",
      id,
      params: {
        type: "chatgptAuthTokens",
        accessToken,
        chatgptAccountId,
        chatgptPlanType,
      },
    })}\n`);
  } catch {
    fail("CODEX_LUNA_APP_SERVER_AUTH_WRITE_FAILED", "External ChatGPT login request could not be written");
  }
  return Object.freeze({
    schema_version: CODEX_LUNA_APP_SERVER_SCHEMA_VERSION,
    method: "account/login/start",
    id,
    auth_type: "chatgptAuthTokens",
    account_id_sha256: sha256Bytes(chatgptAccountId),
    plan_type_present: chatgptPlanType !== null,
    write_accepted: accepted !== false,
    credential_returned: false,
  });
}

export function buildCodexLunaThreadStartRequest({
  workspaceRoot,
  mode,
  id = CODEX_LUNA_APP_SERVER_REQUEST_IDS.threadStart,
  developerInstructions = null,
}) {
  requireAppServer(isRequestId(id), "CODEX_LUNA_APP_SERVER_REQUEST_ID_INVALID", "Thread start request id is invalid");
  requireAppServer(developerInstructions === null || isNonEmptyString(developerInstructions), "CODEX_LUNA_APP_SERVER_INSTRUCTIONS_INVALID", "Developer instructions must be null or a non-empty string");
  const root = normalizedWorkspaceRoot(workspaceRoot);
  const profileId = codexLunaPermissionProfileId(mode);
  const params = {
    model: CODEX_LUNA_MODEL,
    allowProviderModelFallback: false,
    cwd: root,
    runtimeWorkspaceRoots: [],
    approvalPolicy: "never",
    approvalsReviewer: "user",
    permissions: profileId,
    ephemeral: true,
    dynamicTools: [],
    selectedCapabilityRoots: [],
    experimentalRawEvents: true,
  };
  if (developerInstructions !== null) params.developerInstructions = developerInstructions;
  return { method: "thread/start", id, params };
}

export function buildCodexLunaTurnStartRequest({
  threadId,
  prompt,
  workspaceRoot,
  skillPath,
  codexHome = null,
  mode,
  outputSchema = null,
  id = CODEX_LUNA_APP_SERVER_REQUEST_IDS.turnStart,
}) {
  requireAppServer(isRequestId(id), "CODEX_LUNA_APP_SERVER_REQUEST_ID_INVALID", "Turn start request id is invalid");
  requireAppServer(isNonEmptyString(threadId), "CODEX_LUNA_APP_SERVER_THREAD_ID_INVALID", "Turn start requires a thread id");
  requireAppServer(isNonEmptyString(prompt), "CODEX_LUNA_APP_SERVER_PROMPT_INVALID", "Turn prompt must be non-empty");
  requireAppServer(outputSchema === null || isPlainObject(outputSchema), "CODEX_LUNA_APP_SERVER_OUTPUT_SCHEMA_INVALID", "Output schema must be null or one JSON object");
  const root = normalizedWorkspaceRoot(workspaceRoot);
  const normalizedModeValue = normalizedMode(mode);
  const privateSkillRoot = normalizedModeValue === "service" ? normalizedPrivateDirectory(codexHome, "Codex home") : null;
  const intendedSkillPath = normalizedSkillPath(skillPath, root, privateSkillRoot);
  const params = {
    threadId,
    input: [
      { type: "skill", name: skillName(intendedSkillPath), path: intendedSkillPath },
      { type: "text", text: prompt, text_elements: [] },
    ],
    cwd: root,
    runtimeWorkspaceRoots: [],
    approvalPolicy: "never",
    approvalsReviewer: "user",
    permissions: codexLunaPermissionProfileId(normalizedModeValue),
    model: CODEX_LUNA_MODEL,
    effort: CODEX_LUNA_REASONING_EFFORT,
  };
  if (outputSchema !== null) params.outputSchema = cloneJson(outputSchema, "output schema");
  return { method: "turn/start", id, params };
}

function parseTranscriptMessages(transcript) {
  if (Array.isArray(transcript)) {
    requireAppServer(transcript.length > 0, "CODEX_LUNA_APP_SERVER_TRANSCRIPT_EMPTY", "App-server transcript is empty");
    return transcript.map((message, index) => {
      requireAppServer(isPlainObject(message), "CODEX_LUNA_APP_SERVER_MESSAGE_INVALID", "App-server transcript message is invalid", { line: index + 1 });
      return message;
    });
  }
  requireAppServer(typeof transcript === "string", "CODEX_LUNA_APP_SERVER_TRANSCRIPT_INVALID", "App-server transcript must be JSONL text or an array of messages");
  const lines = transcript.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  requireAppServer(lines.length > 0, "CODEX_LUNA_APP_SERVER_TRANSCRIPT_EMPTY", "App-server transcript is empty");
  return lines.map((line, index) => {
    try {
      const message = JSON.parse(line);
      requireAppServer(isPlainObject(message), "CODEX_LUNA_APP_SERVER_MESSAGE_INVALID", "App-server transcript message is invalid", { line: index + 1 });
      return message;
    } catch (error) {
      if (error instanceof CodexLunaAppServerError) throw error;
      fail("CODEX_LUNA_APP_SERVER_JSONL_INVALID", "App-server transcript contains invalid JSON", { line: index + 1 });
    }
  });
}

function normalizeUsageBreakdown(value, label) {
  const keys = [
    "totalTokens",
    "inputTokens",
    "cachedInputTokens",
    "cacheWriteInputTokens",
    "outputTokens",
    "reasoningOutputTokens",
  ];
  requireAppServer(exactObjectKeys(value, keys), "CODEX_LUNA_APP_SERVER_USAGE_INVALID", `${label} token usage is incomplete`);
  for (const key of keys) {
    requireAppServer(safeInteger(value[key]), "CODEX_LUNA_APP_SERVER_USAGE_INVALID", `${label} ${key} must be a non-negative safe integer`);
  }
  requireAppServer(value.totalTokens === value.inputTokens + value.outputTokens, "CODEX_LUNA_APP_SERVER_USAGE_TOTAL_INVALID", `${label} totalTokens does not equal input plus output`);
  requireAppServer(value.cachedInputTokens <= value.inputTokens, "CODEX_LUNA_APP_SERVER_USAGE_CACHE_INVALID", `${label} cached input exceeds input`);
  requireAppServer(value.reasoningOutputTokens <= value.outputTokens, "CODEX_LUNA_APP_SERVER_USAGE_REASONING_INVALID", `${label} reasoning output exceeds output`);
  return Object.fromEntries(keys.map((key) => [key, value[key]]));
}

function normalizeThreadTokenUsage(value) {
  requireAppServer(exactObjectKeys(value, ["total", "last", "modelContextWindow"]), "CODEX_LUNA_APP_SERVER_USAGE_INVALID", "ThreadTokenUsage is incomplete");
  requireAppServer(value.modelContextWindow === null || (safeInteger(value.modelContextWindow) && value.modelContextWindow > 0), "CODEX_LUNA_APP_SERVER_USAGE_CONTEXT_INVALID", "ThreadTokenUsage model context window is invalid");
  const total = normalizeUsageBreakdown(value.total, "total");
  const last = normalizeUsageBreakdown(value.last, "last");
  for (const key of Object.keys(last)) {
    requireAppServer(last[key] <= total[key], "CODEX_LUNA_APP_SERVER_USAGE_MONOTONICITY_INVALID", `ThreadTokenUsage last ${key} exceeds total`);
  }
  return { total, last, modelContextWindow: value.modelContextWindow };
}

function sumUsageBreakdowns(values) {
  const keys = [
    "totalTokens",
    "inputTokens",
    "cachedInputTokens",
    "cacheWriteInputTokens",
    "outputTokens",
    "reasoningOutputTokens",
  ];
  const aggregate = Object.fromEntries(keys.map((key) => [key, 0]));
  for (const value of values) {
    for (const key of keys) {
      aggregate[key] += value[key];
      requireAppServer(Number.isSafeInteger(aggregate[key]), "CODEX_LUNA_APP_SERVER_RAW_USAGE_OVERFLOW", "Raw response usage aggregate exceeds safe integer range");
    }
  }
  return aggregate;
}

function responseFor(responses, id, label) {
  const response = responses.get(`${typeof id}:${String(id)}`);
  requireAppServer(response !== undefined, "CODEX_LUNA_APP_SERVER_RESPONSE_MISSING", `${label} response is missing`, { id });
  requireAppServer(!Object.hasOwn(response, "error"), "CODEX_LUNA_APP_SERVER_RESPONSE_ERROR", `${label} response failed`, { id });
  requireAppServer(isPlainObject(response.result), "CODEX_LUNA_APP_SERVER_RESPONSE_INVALID", `${label} response result is invalid`, { id });
  return response.result;
}

function requireOne(values, code, message) {
  requireAppServer(values.length === 1, code, message, { count: values.length });
  return values[0];
}

function bindScope(threadIds, turnIds, threadId, turnId, label) {
  requireAppServer(isNonEmptyString(threadId), "CODEX_LUNA_APP_SERVER_THREAD_ID_INVALID", `${label} has no thread id`);
  threadIds.add(threadId);
  if (turnId !== undefined) {
    requireAppServer(isNonEmptyString(turnId), "CODEX_LUNA_APP_SERVER_TURN_ID_INVALID", `${label} has no turn id`);
    turnIds.add(turnId);
  }
}

/**
 * Parse and fail-close one complete app-server turn transcript.
 */
export function parseCodexLunaAppServerTranscript(transcript, {
  requestIds = CODEX_LUNA_APP_SERVER_REQUEST_IDS,
  workspaceRoot,
  skillPath,
  codexHome,
  mode,
  expectedCliVersion = CODEX_LUNA_CLI_VERSION,
  secretValues = [],
} = {}) {
  requireAppServer(isPlainObject(requestIds), "CODEX_LUNA_APP_SERVER_REQUEST_IDS_INVALID", "Request id map is invalid");
  for (const key of ["initialize", "login", "accountRead", "permissionProfileList", "skillsList", "threadStart", "turnStart"]) {
    requireAppServer(isRequestId(requestIds[key]), "CODEX_LUNA_APP_SERVER_REQUEST_IDS_INVALID", `Request id ${key} is invalid`);
  }
  requireAppServer(new Set(Object.values(requestIds).map((value) => `${typeof value}:${String(value)}`)).size === 7, "CODEX_LUNA_APP_SERVER_REQUEST_IDS_DUPLICATE", "Request ids must be distinct");
  requireAppServer(Array.isArray(secretValues), "CODEX_LUNA_APP_SERVER_SECRETS_INVALID", "Secret canaries must be an array");
  const expectedRoot = normalizedWorkspaceRoot(workspaceRoot);
  const expectedCodexHome = normalizedPrivateDirectory(codexHome, "Codex home");
  const expectedMode = normalizedMode(mode);
  const expectedSkillPath = normalizedSkillPath(skillPath, expectedRoot, expectedMode === "service" ? expectedCodexHome : null);
  const expectedSystemSkillPaths = systemSkillPaths(expectedCodexHome);
  const modePolicy = MODE_POLICY[expectedMode];
  const expectedProfileId = codexLunaPermissionProfileId(expectedMode);
  const messages = parseTranscriptMessages(transcript);
  assertNoSecret(messages, secretValues);

  const responses = new Map();
  const notifications = new Map();
  const threadIds = new Set();
  const turnIds = new Set();
  const completedItems = [];
  const commands = [];
  const commandIds = new Set();
  const usageUpdates = [];
  const rawResponseUsages = [];
  const rawResponseIds = new Set();
  const rawResponseItems = [];
  const rawShellCalls = new Map();
  const rawMcpCalls = new Map();
  const rawShellOutputCallIds = new Set();
  const rawCustomToolCalls = new Map();
  const rawCustomToolOutputCallIds = new Set();
  const mcpToolCalls = [];
  const fileChanges = [];
  const warningReceipts = [];

  for (const [index, message] of messages.entries()) {
    const hasMethod = isNonEmptyString(message.method);
    const hasId = Object.hasOwn(message, "id");
    if (hasMethod && hasId) {
      fail("CODEX_LUNA_APP_SERVER_SERVER_REQUEST_REJECTED", "App-server sent a server-initiated request that this non-interactive flow cannot grant", { method: message.method, line: index + 1 });
    }
    if (!hasMethod && hasId) {
      requireAppServer(isRequestId(message.id), "CODEX_LUNA_APP_SERVER_RESPONSE_ID_INVALID", "App-server response id is invalid", { line: index + 1 });
      const key = `${typeof message.id}:${String(message.id)}`;
      requireAppServer(!responses.has(key), "CODEX_LUNA_APP_SERVER_RESPONSE_DUPLICATE", "App-server response id was repeated", { id: message.id });
      responses.set(key, message);
      continue;
    }
    requireAppServer(hasMethod && !hasId, "CODEX_LUNA_APP_SERVER_MESSAGE_INVALID", "App-server message is neither a response nor a notification", { line: index + 1 });
    requireAppServer(notificationAllowed(message.method, expectedMode), "CODEX_LUNA_APP_SERVER_NOTIFICATION_REJECTED", "App-server emitted an unapproved notification", { method: message.method, line: index + 1 });
    const occurrences = notifications.get(message.method) ?? [];
    occurrences.push({ message, index });
    notifications.set(message.method, occurrences);

    if (["thread/started", "turn/started", "turn/completed", "thread/tokenUsage/updated", "item/started", "item/completed"].includes(message.method)) {
      requireAppServer(isPlainObject(message.params), "CODEX_LUNA_APP_SERVER_NOTIFICATION_INVALID", `${message.method} params are invalid`, { line: index + 1 });
    }
    if (message.method === "thread/started") {
      bindScope(threadIds, turnIds, message.params.thread?.id, undefined, "thread/started");
    } else if (message.method === "turn/started" || message.method === "turn/completed") {
      bindScope(threadIds, turnIds, message.params.threadId, message.params.turn?.id, message.method);
    } else if (message.method === "thread/tokenUsage/updated") {
      bindScope(threadIds, turnIds, message.params.threadId, message.params.turnId, message.method);
      usageUpdates.push({ index, usage: normalizeThreadTokenUsage(message.params.tokenUsage) });
    } else if (message.method === "rawResponse/completed") {
      requireAppServer(isPlainObject(message.params), "CODEX_LUNA_APP_SERVER_RAW_RESPONSE_INVALID", "rawResponse/completed params are invalid", { line: index + 1 });
      bindScope(threadIds, turnIds, message.params.threadId, message.params.turnId, message.method);
      requireAppServer(isNonEmptyString(message.params.responseId) && !rawResponseIds.has(message.params.responseId), "CODEX_LUNA_APP_SERVER_RAW_RESPONSE_INVALID", "Raw response id is missing or duplicated", { line: index + 1 });
      requireAppServer(message.params.usage !== null, "CODEX_LUNA_APP_SERVER_RAW_USAGE_MISSING", "Raw response completed without exact usage", { line: index + 1 });
      rawResponseIds.add(message.params.responseId);
      rawResponseUsages.push({ index, response_id: message.params.responseId, usage: normalizeUsageBreakdown(message.params.usage, "raw response") });
    } else if (message.method === "rawResponseItem/completed") {
      requireAppServer(isPlainObject(message.params), "CODEX_LUNA_APP_SERVER_RAW_RESPONSE_ITEM_INVALID", "rawResponseItem/completed params are invalid", { line: index + 1 });
      bindScope(threadIds, turnIds, message.params.threadId, message.params.turnId, message.method);
      const item = message.params.item;
      requireAppServer(isPlainObject(item) && isNonEmptyString(item.type), "CODEX_LUNA_APP_SERVER_RAW_RESPONSE_ITEM_INVALID", "rawResponseItem/completed item is invalid", { line: index + 1 });
      requireAppServer(RAW_RESPONSE_ITEM_TYPES_ALLOWED.has(item.type), "CODEX_LUNA_APP_SERVER_RAW_RESPONSE_ITEM_TYPE_REJECTED", "Raw Responses API item used a disallowed capability", { item_type: item.type, line: index + 1 });
      const receipt = { type: item.type };
      if (item.type === "message") {
        requireAppServer(RAW_MESSAGE_ROLES_ALLOWED.has(item.role), "CODEX_LUNA_APP_SERVER_RAW_MESSAGE_INVALID", "Raw response message role is outside the pinned allowlist", { line: index + 1, role: item.role ?? null });
        receipt.role = item.role;
      } else if (item.type === "custom_tool_call") {
        requireAppServer(
          RAW_CUSTOM_TOOL_NAMES.has(item.name)
            && rawCustomToolAllowed(item.name, expectedMode)
            && (item.namespace === undefined || item.namespace === null),
          "CODEX_LUNA_APP_SERVER_RAW_CUSTOM_TOOL_REJECTED",
          "Raw custom tool call is outside the mode-specific allowlist",
          { function_name: item.name ?? null, line: index + 1 },
        );
        requireAppServer(
          isNonEmptyString(item.call_id)
            && !rawShellCalls.has(item.call_id)
            && !rawMcpCalls.has(item.call_id)
            && !rawCustomToolCalls.has(item.call_id),
          "CODEX_LUNA_APP_SERVER_RAW_CUSTOM_TOOL_CALL_INVALID",
          "Raw custom tool call id is missing or duplicated",
          { line: index + 1 },
        );
        rawCustomToolCalls.set(item.call_id, { type: item.type, name: item.name });
        receipt.name = item.name;
        receipt.namespace = item.namespace ?? null;
        receipt.call_id = item.call_id;
        receipt.status = item.status ?? null;
      } else if (item.type === "custom_tool_call_output") {
        const call = rawCustomToolCalls.get(item.call_id);
        requireAppServer(
          call !== undefined
            && rawCustomToolAllowed(call.name, expectedMode)
            && (item.name === null || item.name === undefined || item.name === call.name)
            && !rawCustomToolOutputCallIds.has(item.call_id),
          "CODEX_LUNA_APP_SERVER_RAW_CUSTOM_TOOL_OUTPUT_INVALID",
          "Raw custom tool output has no unique preceding allowed custom tool call",
          { line: index + 1 },
        );
        rawCustomToolOutputCallIds.add(item.call_id);
        receipt.name = item.name ?? null;
        receipt.call_id = item.call_id;
      } else if (item.type === "function_call") {
        const shellCall = RAW_SHELL_FUNCTION_NAMES.has(item.name) && (item.namespace === undefined || item.namespace === null);
        const mcpCall = expectedMode === "client" && MACOS_CODEX_LUNA_PUBLIC_TOOLS.some((tool) => item.name === tool || item.name?.endsWith(`__${tool}`));
        requireAppServer(shellCall || mcpCall, "CODEX_LUNA_APP_SERVER_RAW_SHELL_FUNCTION_REJECTED", "Raw function call is neither the pinned Luna shell function nor an allowed Problem Locator MCP tool", { function_name: item.name ?? null, line: index + 1 });
        requireAppServer(isNonEmptyString(item.call_id) && !rawShellCalls.has(item.call_id) && !rawMcpCalls.has(item.call_id) && !rawCustomToolCalls.has(item.call_id), "CODEX_LUNA_APP_SERVER_RAW_SHELL_CALL_INVALID", "Raw function call id is missing or duplicated", { line: index + 1 });
        if (shellCall) rawShellCalls.set(item.call_id, { type: item.type, name: item.name });
        else rawMcpCalls.set(item.call_id, { type: item.type, name: item.name });
        receipt.name = item.name;
        receipt.namespace = item.namespace ?? null;
        receipt.call_id = item.call_id;
      } else if (item.type === "local_shell_call") {
        requireAppServer(isNonEmptyString(item.call_id) && !rawShellCalls.has(item.call_id) && !rawMcpCalls.has(item.call_id) && !rawCustomToolCalls.has(item.call_id), "CODEX_LUNA_APP_SERVER_RAW_SHELL_CALL_INVALID", "Raw local shell call id is missing or duplicated", { line: index + 1 });
        requireAppServer(["completed", "in_progress", "incomplete"].includes(item.status) && item.action?.type === "exec", "CODEX_LUNA_APP_SERVER_RAW_SHELL_CALL_INVALID", "Raw local shell call status or action is invalid", { line: index + 1 });
        rawShellCalls.set(item.call_id, { type: item.type, name: null });
        receipt.call_id = item.call_id;
        receipt.status = item.status;
        receipt.action_type = item.action.type;
      } else if (item.type === "function_call_output") {
        requireAppServer(isNonEmptyString(item.call_id) && (rawShellCalls.has(item.call_id) || rawMcpCalls.has(item.call_id)) && !rawShellOutputCallIds.has(item.call_id), "CODEX_LUNA_APP_SERVER_RAW_SHELL_OUTPUT_INVALID", "Raw tool output has no unique preceding allowed call", { line: index + 1 });
        rawShellOutputCallIds.add(item.call_id);
        receipt.call_id = item.call_id;
      }
      rawResponseItems.push(receipt);
    } else if (message.method === "item/started" || message.method === "item/completed") {
      bindScope(threadIds, turnIds, message.params.threadId, message.params.turnId, message.method);
      const item = message.params.item;
      requireAppServer(isPlainObject(item) && isNonEmptyString(item.type) && isNonEmptyString(item.id), "CODEX_LUNA_APP_SERVER_ITEM_INVALID", `${message.method} item is invalid`, { line: index + 1 });
      const allowedThreadItem = THREAD_ITEM_TYPES_ALLOWED.has(item.type)
        || (expectedMode === "client" && item.type === "mcpToolCall")
        || (["generation", "service"].includes(expectedMode) && item.type === "fileChange");
      requireAppServer(allowedThreadItem, "CODEX_LUNA_APP_SERVER_TOOL_REJECTED", "App-server turn used a disallowed tool or item type", { item_type: item.type, line: index + 1 });
      if (message.method === "item/completed") {
        completedItems.push({ item, index });
        if (item.type === "commandExecution") {
          requireAppServer(!commandIds.has(item.id), "CODEX_LUNA_APP_SERVER_COMMAND_DUPLICATE", "A command execution completed more than once", { item_id: item.id });
          requireAppServer(isNonEmptyString(item.command) && isNonEmptyString(item.cwd), "CODEX_LUNA_APP_SERVER_COMMAND_INVALID", "Completed command execution is missing command or cwd", { item_id: item.id });
          requireAppServer(path.isAbsolute(item.cwd) && pathIsInside(expectedRoot, path.resolve(item.cwd)), "CODEX_LUNA_APP_SERVER_COMMAND_WORKSPACE_INVALID", "Completed command execution cwd is outside the invocation workspace", { item_id: item.id });
          requireAppServer(["completed", "failed", "declined"].includes(item.status), "CODEX_LUNA_APP_SERVER_COMMAND_STATUS_INVALID", "Command execution has no terminal status", { item_id: item.id });
          requireAppServer(item.exitCode === null || Number.isSafeInteger(item.exitCode), "CODEX_LUNA_APP_SERVER_COMMAND_EXIT_INVALID", "Command execution exit code is invalid", { item_id: item.id });
          requireAppServer(item.durationMs === null || safeInteger(item.durationMs), "CODEX_LUNA_APP_SERVER_COMMAND_DURATION_INVALID", "Command execution duration is invalid", { item_id: item.id });
          commandIds.add(item.id);
          commands.push({
            item_id: item.id,
            command: item.command,
            cwd: item.cwd,
            status: item.status,
            exit_code: item.exitCode,
            duration_ms: item.durationMs,
          });
        } else if (item.type === "fileChange") {
          requireAppServer(
            exactObjectKeys(item, ["type", "id", "status", "changes"])
              && ["completed", "failed", "declined"].includes(item.status)
              && Array.isArray(item.changes),
            "CODEX_LUNA_APP_SERVER_FILE_CHANGE_INVALID",
            "Completed generation file change is not a closed terminal receipt",
            { item_id: item.id },
          );
          const changes = item.changes.map((change) => {
            requireAppServer(
              exactObjectKeys(change, ["path", "kind", "diff_receipt"])
                && isNonEmptyString(change.path)
                && exactObjectKeys(change.kind, ["type", "move_path"])
                && ["add", "delete", "update"].includes(change.kind.type)
                && (change.kind.move_path === null || isNonEmptyString(change.kind.move_path))
                && exactObjectKeys(change.diff_receipt, ["redacted_sha256", "byte_count"])
                && /^[a-f0-9]{64}$/.test(change.diff_receipt.redacted_sha256)
                && safeInteger(change.diff_receipt.byte_count),
              "CODEX_LUNA_APP_SERVER_FILE_CHANGE_INVALID",
              "Generation file change contains an invalid path, kind, or diff receipt",
              { item_id: item.id },
            );
            const resolvedPath = path.isAbsolute(change.path) ? path.resolve(change.path) : path.resolve(expectedRoot, change.path);
            requireAppServer(pathIsInside(expectedRoot, resolvedPath), "CODEX_LUNA_APP_SERVER_FILE_CHANGE_WORKSPACE_INVALID", "Generation file change targets outside the invocation workspace", { item_id: item.id });
            if (change.kind.move_path !== null) {
              const resolvedMovePath = path.isAbsolute(change.kind.move_path) ? path.resolve(change.kind.move_path) : path.resolve(expectedRoot, change.kind.move_path);
              requireAppServer(change.kind.type === "update" && pathIsInside(expectedRoot, resolvedMovePath), "CODEX_LUNA_APP_SERVER_FILE_CHANGE_WORKSPACE_INVALID", "Generation file move targets outside the invocation workspace", { item_id: item.id });
            }
            return cloneJson(change, "file change receipt");
          });
          fileChanges.push({ item_id: item.id, status: item.status, changes });
        } else if (item.type === "mcpToolCall") {
          requireAppServer(item.server === "problem-locator" && MACOS_CODEX_LUNA_PUBLIC_TOOLS.includes(item.tool) && item.status === "completed" && isPlainObject(item.arguments) && item.error == null, "CODEX_LUNA_APP_SERVER_MCP_TOOL_CALL_INVALID", "Completed MCP tool call violates the server/tool/status/argument boundary", { item_id: item.id });
          mcpToolCalls.push({
            item_id: item.id,
            server: item.server,
            tool: item.tool,
            status: item.status,
            arguments: cloneJson(item.arguments, "MCP arguments"),
            result: item.result === undefined ? null : cloneJson(item.result, "MCP result"),
            error: null,
          });
        }
      }
    } else if (message.method === "thread/status/changed") {
      requireAppServer(isPlainObject(message.params), "CODEX_LUNA_APP_SERVER_NOTIFICATION_INVALID", "thread/status/changed params are invalid", { line: index + 1 });
      bindScope(threadIds, turnIds, message.params.threadId, undefined, message.method);
    } else if (TURN_SCOPED_DELTA_NOTIFICATIONS.has(message.method)) {
      requireAppServer(isPlainObject(message.params), "CODEX_LUNA_APP_SERVER_NOTIFICATION_INVALID", `${message.method} params are invalid`, { line: index + 1 });
      bindScope(threadIds, turnIds, message.params.threadId, message.params.turnId, message.method);
    } else if (message.method === "account/rateLimits/updated") {
      requireAppServer(isPlainObject(message.params), "CODEX_LUNA_APP_SERVER_NOTIFICATION_INVALID", "account/rateLimits/updated params are invalid", { line: index + 1 });
    } else if (message.method === "warning") {
      const params = message.params;
      requireAppServer(
        exactObjectKeys(params, ["threadId", "message_receipt"])
          && (params.threadId === null || isNonEmptyString(params.threadId))
          && exactObjectKeys(params.message_receipt, ["redacted_sha256", "byte_count"])
          && /^[a-f0-9]{64}$/.test(params.message_receipt.redacted_sha256)
          && safeInteger(params.message_receipt.byte_count)
          && params.message_receipt.byte_count > 0,
        "CODEX_LUNA_APP_SERVER_WARNING_INVALID",
        "Warning notification must be a closed content-free receipt",
        { line: index + 1 },
      );
      warningReceipts.push({ index, thread_id: params.threadId, ...params.message_receipt });
    }
  }

  const initialize = responseFor(responses, requestIds.initialize, "initialize");
  requireAppServer(isNonEmptyString(initialize.userAgent) && isNonEmptyString(initialize.codexHome) && isNonEmptyString(initialize.platformFamily) && isNonEmptyString(initialize.platformOs), "CODEX_LUNA_APP_SERVER_INITIALIZE_INVALID", "Initialize response is incomplete");
  requireAppServer(path.resolve(initialize.codexHome) === expectedCodexHome, "CODEX_LUNA_APP_SERVER_CODEX_HOME_BINDING_INVALID", "Initialize response does not bind the isolated Codex home");
  const login = responseFor(responses, requestIds.login, "external auth login");
  requireAppServer(exactObjectKeys(login, ["type"]) && login.type === "chatgptAuthTokens", "CODEX_LUNA_APP_SERVER_LOGIN_INVALID", "App-server did not accept external ChatGPT tokens");
  const accountRead = responseFor(responses, requestIds.accountRead, "account/read");
  requireAppServer(
    isPlainObject(accountRead.account)
      && accountRead.account.type === "chatgpt"
      && isNonEmptyString(accountRead.account.planType)
      && accountRead.requiresOpenaiAuth === true,
    "CODEX_LUNA_APP_SERVER_ACCOUNT_PROOF_INVALID",
    "Account proof does not bind an authenticated ChatGPT account",
  );
  const permissionProfiles = responseFor(responses, requestIds.permissionProfileList, "permissionProfile/list");
  requireAppServer(Array.isArray(permissionProfiles.data), "CODEX_LUNA_APP_SERVER_PERMISSION_PROOF_INVALID", "Permission profile proof is invalid");
  const selectedPermissionProfiles = permissionProfiles.data.filter((entry) => entry?.id === expectedProfileId);
  requireAppServer(selectedPermissionProfiles.length === 1 && selectedPermissionProfiles[0].allowed === true, "CODEX_LUNA_APP_SERVER_PERMISSION_PROOF_INVALID", "The exact custom permission profile is not selectable");
  const skillsList = responseFor(responses, requestIds.skillsList, "skills/list");
  requireAppServer(Array.isArray(skillsList.data) && skillsList.data.length === 1, "CODEX_LUNA_APP_SERVER_SKILLS_PROOF_INVALID", "Skills proof must contain exactly one cwd entry");
  const skillsEntry = skillsList.data[0];
  requireAppServer(
    skillsEntry?.cwd === expectedRoot
      && Array.isArray(skillsEntry.skills)
      && Array.isArray(skillsEntry.errors)
      && skillsEntry.errors.length === 0,
    "CODEX_LUNA_APP_SERVER_SKILLS_PROOF_INVALID",
    "Skills proof cwd or errors are invalid",
    {
      field: "skills_list",
      cwd_matches: skillsEntry?.cwd === expectedRoot,
      errors_count: Array.isArray(skillsEntry?.errors) ? skillsEntry.errors.length : null,
      skills_errors: Array.isArray(skillsEntry?.errors) ? JSON.stringify(skillsEntry.errors) : null,
    },
  );
  const enabledSkills = skillsEntry.skills.filter((entry) => entry?.enabled === true);
  requireAppServer(enabledSkills.length === 1 && enabledSkills[0].path === expectedSkillPath && enabledSkills[0].name === skillName(expectedSkillPath), "CODEX_LUNA_APP_SERVER_SKILLS_PROOF_INVALID", "Exactly the intended skill must be enabled");
  for (const systemSkillPath of expectedSystemSkillPaths) {
    const entries = skillsEntry.skills.filter((entry) => entry?.path === systemSkillPath);
    requireAppServer(entries.length === 1 && entries[0].enabled === false, "CODEX_LUNA_APP_SERVER_SYSTEM_SKILL_PROOF_INVALID", "A system skill was not explicitly disabled", { skill_path_sha256: sha256Bytes(systemSkillPath) });
  }
  const threadStart = responseFor(responses, requestIds.threadStart, "thread/start");
  const turnStart = responseFor(responses, requestIds.turnStart, "turn/start");
  requireAppServer(responses.size === 7, "CODEX_LUNA_APP_SERVER_UNEXPECTED_RESPONSE", "App-server transcript contains an unexpected response", { response_count: responses.size });

  const loginCompletedEvent = requireOne(notifications.get("account/login/completed") ?? [], "CODEX_LUNA_APP_SERVER_LOGIN_COMPLETION_INVALID", "External auth must complete exactly once");
  const loginCompleted = loginCompletedEvent.message.params;
  requireAppServer(isPlainObject(loginCompleted) && loginCompleted.loginId === null && loginCompleted.success === true && loginCompleted.error === null, "CODEX_LUNA_APP_SERVER_LOGIN_COMPLETION_INVALID", "External auth completion was not successful");
  const accountUpdatedEvent = requireOne(notifications.get("account/updated") ?? [], "CODEX_LUNA_APP_SERVER_ACCOUNT_UPDATE_INVALID", "External auth must update the account exactly once");
  const accountUpdated = accountUpdatedEvent.message.params;
  requireAppServer(isPlainObject(accountUpdated) && accountUpdated.authMode === "chatgptAuthTokens", "CODEX_LUNA_APP_SERVER_ACCOUNT_UPDATE_INVALID", "External auth mode was not activated");
  const loginBoundaryIndex = Math.max(loginCompletedEvent.index, accountUpdatedEvent.index);
  requireAppServer(
    (notifications.get("account/rateLimits/updated") ?? []).every((entry) => entry.index > loginBoundaryIndex),
    "CODEX_LUNA_APP_SERVER_RATE_LIMIT_ORDER_INVALID",
    "Rate-limit state was emitted before external ChatGPT login completed",
  );

  const threadStarted = requireOne(notifications.get("thread/started") ?? [], "CODEX_LUNA_APP_SERVER_THREAD_CARDINALITY_INVALID", "Exactly one thread must start");
  const turnStarted = requireOne(notifications.get("turn/started") ?? [], "CODEX_LUNA_APP_SERVER_TURN_CARDINALITY_INVALID", "Exactly one turn must start");
  const turnCompleted = requireOne(notifications.get("turn/completed") ?? [], "CODEX_LUNA_APP_SERVER_TURN_COMPLETION_INVALID", "Exactly one turn must complete");
  requireAppServer(threadIds.size === 1, "CODEX_LUNA_APP_SERVER_THREAD_CARDINALITY_INVALID", "Transcript contains more than one thread", { count: threadIds.size });
  requireAppServer(turnIds.size === 1, "CODEX_LUNA_APP_SERVER_TURN_CARDINALITY_INVALID", "Transcript contains more than one turn", { count: turnIds.size });
  const threadId = [...threadIds][0];
  const turnId = [...turnIds][0];
  requireAppServer(warningReceipts.every((warning) => warning.thread_id === null || warning.thread_id === threadId), "CODEX_LUNA_APP_SERVER_WARNING_SCOPE_INVALID", "Warning notification targets another thread");

  requireAppServer(threadStart.thread?.id === threadId && threadStarted.message.params.thread?.id === threadId, "CODEX_LUNA_APP_SERVER_THREAD_BINDING_INVALID", "Thread response and notifications disagree");
  requireAppServer(threadStart.model === CODEX_LUNA_MODEL && threadStart.reasoningEffort === CODEX_LUNA_REASONING_EFFORT && threadStart.modelProvider === "openai", "CODEX_LUNA_APP_SERVER_MODEL_IDENTITY_INVALID", "Thread did not use the pinned OpenAI model and reasoning effort");
  requireAppServer(threadStart.cwd === expectedRoot, "CODEX_LUNA_APP_SERVER_WORKSPACE_BINDING_INVALID", "Thread cwd differs from the invocation workspace");
  requireAppServer(Array.isArray(threadStart.runtimeWorkspaceRoots) && threadStart.runtimeWorkspaceRoots.length === 0, "CODEX_LUNA_APP_SERVER_WORKSPACE_BINDING_INVALID", "Thread runtime workspace roots must remain empty; the custom profile owns the explicit root");
  requireAppServer(Array.isArray(threadStart.instructionSources), "CODEX_LUNA_APP_SERVER_INSTRUCTION_SOURCES_INVALID", "Thread instructionSources must be an array");
  const instructionSources = [...new Set(threadStart.instructionSources)];
  requireAppServer(instructionSources.length === threadStart.instructionSources.length && instructionSources.every((source) => isNonEmptyString(source) && path.isAbsolute(source) && pathIsInside(expectedRoot, path.resolve(source))), "CODEX_LUNA_APP_SERVER_INSTRUCTION_SOURCES_INVALID", "Thread instruction sources must be unique absolute paths inside the invocation workspace");
  requireAppServer(threadStart.approvalPolicy === "never", "CODEX_LUNA_APP_SERVER_APPROVAL_POLICY_INVALID", "Thread approval policy must be never");
  requireAppServer(threadStart.approvalsReviewer === "user", "CODEX_LUNA_APP_SERVER_APPROVAL_REVIEWER_INVALID", "Thread approval reviewer must remain user-only");
  requireAppServer(isPlainObject(threadStart.sandbox) && threadStart.sandbox.networkAccess === modePolicy.network_enabled, "CODEX_LUNA_APP_SERVER_SANDBOX_PROJECTION_INVALID", "Legacy sandbox projection must match the selected command-network policy");
  requireAppServer(threadStart.activePermissionProfile?.id === expectedProfileId && threadStart.activePermissionProfile?.extends === null, "CODEX_LUNA_APP_SERVER_PERMISSION_PROFILE_INVALID", "Thread did not activate the exact custom permission profile");
  requireAppServer(threadStart.multiAgentMode === "explicitRequestOnly", "CODEX_LUNA_APP_SERVER_MULTI_AGENT_BOUNDARY_INVALID", "Thread unexpectedly activated multi-agent behavior");
  const threadBoundaryChecks = [
    ["session_id", threadStart.thread.sessionId === threadId],
    ["forked_from_id", threadStart.thread.forkedFromId === null],
    ["parent_thread_id", threadStart.thread.parentThreadId === null],
    ["ephemeral", threadStart.thread.ephemeral === true],
    ["path", threadStart.thread.path === null],
    ["source", threadStart.thread.source === CODEX_LUNA_APP_SERVER_SESSION_SOURCE],
    ["model_provider", threadStart.thread.modelProvider === "openai"],
    ["cwd", threadStart.thread.cwd === expectedRoot],
    ["cli_version", threadStart.thread.cliVersion === expectedCliVersion],
    ["initial_turns", Array.isArray(threadStart.thread.turns) && threadStart.thread.turns.length === 0],
  ];
  const failedThreadBoundary = threadBoundaryChecks.find(([, passed]) => !passed);
  requireAppServer(
    failedThreadBoundary === undefined,
    "CODEX_LUNA_APP_SERVER_THREAD_BOUNDARY_INVALID",
    "Thread persistence, lineage, source, provider, or workspace boundary is invalid",
    { field: failedThreadBoundary?.[0] ?? null },
  );
  requireAppServer(turnStart.turn?.id === turnId && turnStart.turn?.status === "inProgress" && turnStart.turn?.error === null, "CODEX_LUNA_APP_SERVER_TURN_START_INVALID", "Turn start response is invalid");
  requireAppServer(turnStarted.message.params.turn?.id === turnId && turnStarted.message.params.turn?.status === "inProgress", "CODEX_LUNA_APP_SERVER_TURN_START_INVALID", "Turn started notification is invalid");
  requireAppServer(turnCompleted.message.params.turn?.id === turnId && turnCompleted.message.params.turn?.status === "completed" && turnCompleted.message.params.turn?.error === null, "CODEX_LUNA_APP_SERVER_TURN_COMPLETION_INVALID", "Turn did not complete successfully");
  requireAppServer(turnStarted.index < turnCompleted.index, "CODEX_LUNA_APP_SERVER_EVENT_ORDER_INVALID", "Turn completed before it started");

  requireAppServer(usageUpdates.length > 0, "CODEX_LUNA_APP_SERVER_USAGE_MISSING", "Turn has no complete ThreadTokenUsage update");
  const terminalUsage = usageUpdates.at(-1);
  requireAppServer(terminalUsage.index < turnCompleted.index, "CODEX_LUNA_APP_SERVER_EVENT_ORDER_INVALID", "Terminal token usage was not observed before turn completion");
  requireAppServer(rawResponseUsages.length > 0, "CODEX_LUNA_APP_SERVER_RAW_USAGE_MISSING", "Turn has no raw Responses API usage receipts");
  requireAppServer(
    rawCustomToolCalls.size === rawCustomToolOutputCallIds.size
      && [...rawCustomToolCalls.keys()].every((callId) => rawCustomToolOutputCallIds.has(callId)),
    "CODEX_LUNA_APP_SERVER_RAW_CUSTOM_TOOL_OUTPUT_MISSING",
    "Every allowed raw custom tool call must have exactly one output receipt",
  );
  requireAppServer(rawResponseUsages.every((entry) => entry.index < turnCompleted.index), "CODEX_LUNA_APP_SERVER_EVENT_ORDER_INVALID", "Raw response usage was emitted after turn completion");
  const rawResponseUsage = sumUsageBreakdowns(rawResponseUsages.map((entry) => entry.usage));
  const lastRawResponseUsage = rawResponseUsages.at(-1).usage;
  for (const key of Object.keys(rawResponseUsage)) {
    requireAppServer(
      rawResponseUsage[key] === terminalUsage.usage.total[key]
        && lastRawResponseUsage[key] === terminalUsage.usage.last[key],
      "CODEX_LUNA_APP_SERVER_USAGE_RECONCILIATION_FAILED",
      "Raw response aggregate/final usage does not equal terminal ThreadTokenUsage total/last",
      { field: key },
    );
  }
  requireAppServer(completedItems.length > 0, "CODEX_LUNA_APP_SERVER_FINAL_MESSAGE_MISSING", "Turn completed without item evidence");
  const finalCompletedItem = completedItems.at(-1);
  requireAppServer(finalCompletedItem.index < turnCompleted.index, "CODEX_LUNA_APP_SERVER_EVENT_ORDER_INVALID", "An item completed after the turn completed");
  requireAppServer(finalCompletedItem.item.type === "agentMessage", "CODEX_LUNA_APP_SERVER_FINAL_MESSAGE_NOT_LAST", "The last completed item is not an agent message", { item_type: finalCompletedItem.item.type });
  requireAppServer(isNonEmptyString(finalCompletedItem.item.text), "CODEX_LUNA_APP_SERVER_FINAL_MESSAGE_INVALID", "The final agent message is empty");
  requireAppServer(finalCompletedItem.item.phase === "final_answer", "CODEX_LUNA_APP_SERVER_FINAL_MESSAGE_INVALID", "The terminal agent message is not exact final_answer content");

  const summary = {
    schema_version: CODEX_LUNA_APP_SERVER_SCHEMA_VERSION,
    status: "PASS",
    protocol_version: CODEX_LUNA_APP_SERVER_PROTOCOL_VERSION,
    transport: CODEX_LUNA_APP_SERVER_TRANSPORT,
    pinned_cli_version: expectedCliVersion,
    server_user_agent: initialize.userAgent,
    server_platform_family: initialize.platformFamily,
    server_platform_os: initialize.platformOs,
    auth_mode: "chatgptAuthTokens",
    account_plan_type: accountRead.account.planType,
    permission_profile_id: expectedProfileId,
    invocation_mode: expectedMode,
    workspace_root_sha256: sha256Bytes(expectedRoot),
    intended_skill_name: skillName(expectedSkillPath),
    intended_skill_path_sha256: sha256Bytes(expectedSkillPath),
    codex_home_sha256: sha256Bytes(expectedCodexHome),
    disabled_system_skill_path_sha256s: expectedSystemSkillPaths.map((entry) => sha256Bytes(entry)),
    instruction_source_path_sha256s: instructionSources.map((entry) => sha256Bytes(path.resolve(entry))),
    thread_id: threadId,
    turn_id: turnId,
    model: threadStart.model,
    reasoning_effort: threadStart.reasoningEffort,
    final_agent_message: finalCompletedItem.item.text,
    commands,
    command_count: commands.length,
    mcp_tool_calls: mcpToolCalls,
    mcp_tool_call_count: mcpToolCalls.length,
    raw_response_count: rawResponseUsages.length,
    raw_response_ids: rawResponseUsages.map((entry) => entry.response_id),
    raw_response_usage: rawResponseUsage,
    raw_response_item_count: rawResponseItems.length,
    raw_response_item_type_counts: Object.fromEntries(CODEX_LUNA_RAW_RESPONSE_ITEM_TYPES_ALLOWED.map((type) => [type, rawResponseItems.filter((entry) => entry.type === type).length])),
    raw_response_message_role_counts: Object.fromEntries(CODEX_LUNA_RAW_MESSAGE_ROLES_ALLOWED.map((role) => [role, rawResponseItems.filter((entry) => entry.type === "message" && entry.role === role).length])),
    raw_shell_function_names: [...new Set(rawResponseItems.filter((entry) => entry.type === "function_call").map((entry) => entry.name))],
    raw_shell_call_ids: [...rawShellCalls.keys()],
    raw_mcp_call_ids: [...rawMcpCalls.keys()],
    raw_shell_output_call_ids: [...rawShellOutputCallIds],
    raw_custom_tool_names: [...new Set([...rawCustomToolCalls.values()].map((entry) => entry.name))],
    raw_custom_tool_call_ids: [...rawCustomToolCalls.keys()],
    raw_custom_tool_output_call_ids: [...rawCustomToolOutputCallIds],
    file_changes: fileChanges,
    file_change_count: fileChanges.length,
    warning_receipts: warningReceipts.map(({ index, ...warning }) => warning),
    thread_token_usage: terminalUsage.usage,
    usage: {
      input_tokens: terminalUsage.usage.total.inputTokens,
      cached_input_tokens: terminalUsage.usage.total.cachedInputTokens,
      cache_write_input_tokens: terminalUsage.usage.total.cacheWriteInputTokens,
      output_tokens: terminalUsage.usage.total.outputTokens,
      reasoning_output_tokens: terminalUsage.usage.total.reasoningOutputTokens,
      total_tokens: terminalUsage.usage.total.totalTokens,
    },
    inbound_message_count: messages.length,
  };
  assertNoSecret(summary, secretValues);
  return summary;
}

export function buildCodexLunaAppServerEvidenceSummary({ profile, transcript, secretValues = [] }) {
  requireAppServer(isPlainObject(profile), "CODEX_LUNA_APP_SERVER_PROFILE_INVALID", "Permission profile receipt is invalid");
  const rebuilt = buildCodexLunaIsolatedConfig({
    workspaceRoot: profile.workspace_root,
    skillPath: profile.skill_path,
    codexHome: profile.codex_home,
    shellHome: profile.shell_home,
    shellPath: profile.shell_path,
    shellLang: profile.shell_lang,
    mode: profile.invocation_mode,
    mcpServer: profile.mcp_server,
    disabledSkillPaths: profile.disabled_additional_skill_paths,
  });
  requireAppServer(
    profile.profile_id === rebuilt.profile_id
      && profile.workspace_access === rebuilt.workspace_access
      && profile.root_access === rebuilt.root_access
      && profile.minimal_access === rebuilt.minimal_access
      && profile.network_enabled === rebuilt.network_enabled
      && JSON.stringify(profile.mcp_server) === JSON.stringify(rebuilt.mcp_server)
      && profile.codex_home_sha256 === rebuilt.codex_home_sha256
      && JSON.stringify(profile.disabled_system_skill_paths) === JSON.stringify(rebuilt.disabled_system_skill_paths)
      && JSON.stringify(profile.disabled_additional_skill_paths) === JSON.stringify(rebuilt.disabled_additional_skill_paths)
      && JSON.stringify(profile.shell_environment) === JSON.stringify(rebuilt.shell_environment)
      && profile.config_toml === rebuilt.config_toml
      && profile.config_sha256 === rebuilt.config_sha256
      && profile.config_byte_count === rebuilt.config_byte_count,
    "CODEX_LUNA_APP_SERVER_PROFILE_BYTES_INVALID",
    "Permission profile bytes do not match their declared identity",
  );
  requireAppServer(
    isPlainObject(transcript)
      && transcript.status === "PASS"
      && transcript.protocol_version === CODEX_LUNA_APP_SERVER_PROTOCOL_VERSION
      && transcript.permission_profile_id === profile.profile_id
      && transcript.invocation_mode === profile.invocation_mode
      && transcript.workspace_root_sha256 === sha256Bytes(profile.workspace_root)
      && transcript.intended_skill_name === profile.skill_name
      && transcript.intended_skill_path_sha256 === sha256Bytes(profile.skill_path)
      && transcript.codex_home_sha256 === profile.codex_home_sha256
      && JSON.stringify(transcript.disabled_system_skill_path_sha256s) === JSON.stringify(profile.disabled_system_skill_paths.map((entry) => sha256Bytes(entry))),
    "CODEX_LUNA_APP_SERVER_TRANSCRIPT_SUMMARY_INVALID",
    "Transcript summary does not bind the permission profile",
  );
  const evidence = {
    schema_version: CODEX_LUNA_APP_SERVER_SCHEMA_VERSION,
    status: "PASS",
    protocol: {
      name: "codex-app-server",
      version: CODEX_LUNA_APP_SERVER_PROTOCOL_VERSION,
      transport: CODEX_LUNA_APP_SERVER_TRANSPORT,
      pinned_cli_version: transcript.pinned_cli_version,
      experimental_api: true,
      experimental_raw_events: true,
      session_source: CODEX_LUNA_APP_SERVER_SESSION_SOURCE,
      raw_response_item_types_allowed: [...CODEX_LUNA_RAW_RESPONSE_ITEM_TYPES_ALLOWED],
      raw_response_message_roles_allowed: [...CODEX_LUNA_RAW_MESSAGE_ROLES_ALLOWED],
      raw_shell_function_names_allowed: [...CODEX_LUNA_RAW_SHELL_FUNCTION_NAMES],
      raw_custom_tool_names_allowed: [...CODEX_LUNA_RAW_CUSTOM_TOOL_NAMES],
      raw_custom_tool_modes_allowed: {
        apply_patch: ["generation", "service"],
        exec: ["generation", "diagnosis", "service", "client"],
        wait: ["generation", "diagnosis", "service", "client"],
      },
      authentication: "external-chatgpt-tokens-in-memory",
      disabled_features: [...CODEX_LUNA_DISABLED_FEATURES],
      launch_arguments_sha256: sha256Bytes(JSON.stringify(buildCodexLunaAppServerArguments())),
    },
    permission_profile: {
      id: profile.profile_id,
      invocation_mode: profile.invocation_mode,
      workspace_access: profile.workspace_access,
      root_access: profile.root_access,
      minimal_access: profile.minimal_access,
      network_enabled: profile.network_enabled,
      mcp_server: cloneJson(profile.mcp_server, "MCP server receipt"),
      intended_skill_name: profile.skill_name,
      intended_skill_path_sha256: sha256Bytes(profile.skill_path),
      codex_home_sha256: profile.codex_home_sha256,
      disabled_system_skill_path_sha256s: profile.disabled_system_skill_paths.map((entry) => sha256Bytes(entry)),
      shell_environment: cloneJson(profile.shell_environment, "shell environment receipt"),
      bytes_utf8: profile.config_toml,
      byte_count: profile.config_byte_count,
      sha256: profile.config_sha256,
    },
    turn: cloneJson(transcript, "transcript summary"),
  };
  assertNoSecret(evidence, secretValues);
  return evidence;
}
