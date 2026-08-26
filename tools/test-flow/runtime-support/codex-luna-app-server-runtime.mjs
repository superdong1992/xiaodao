import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";

import {
  auditNoSecretLeak,
  canonicalJson,
  CODEX_LUNA_APP_SERVER_SCHEMA_TREE_SHA256,
  codexLunaAppServerCliVersion,
  codexLunaHelperDirectory,
  collectSecretCanaries,
  ordinaryFile,
  sha256Bytes,
  sha256File,
  treeDigest,
  treeManifest,
} from "./codex-luna-contract.mjs";
import {
  buildCodexLunaAccountReadRequest,
  buildCodexLunaAppServerArguments,
  buildCodexLunaAppServerEvidenceSummary,
  buildCodexLunaInitializeRequest,
  buildCodexLunaInitializedNotification,
  buildCodexLunaIsolatedConfig,
  buildCodexLunaPermissionProfileListRequest,
  buildCodexLunaSkillsListRequest,
  buildCodexLunaThreadStartRequest,
  buildCodexLunaTurnStartRequest,
  CODEX_LUNA_APP_SERVER_REQUEST_IDS,
  CODEX_LUNA_DISABLED_FEATURES,
  parseCodexLunaAppServerTranscript,
  writeExternalChatgptAuthLoginRequest,
} from "./codex-luna-app-server.mjs";

const CLEANUP_REQUEST_ID = 7;
const MAX_STDOUT_BYTES = 64 * 1024 * 1024;
const MAX_STDERR_BYTES = 16 * 1024 * 1024;
const AUTH_KEYS = Object.freeze(["access_token", "refresh_token", "id_token", "account_id"]);

export class CodexLunaAppServerRuntimeError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "CodexLunaAppServerRuntimeError";
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) {
  throw new CodexLunaAppServerRuntimeError(code, message, details);
}

function requireRuntime(condition, code, message, details = {}) {
  if (!condition) fail(code, message, details);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function writeFileExclusive(filePath, payload, mode = 0o600) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, payload, { encoding: "utf8", flag: "wx", mode });
}

function writeJsonExclusive(filePath, value) {
  writeFileExclusive(filePath, `${canonicalJson(value)}\n`);
}

function createDirectoryExclusive(directory) {
  fs.mkdirSync(directory, { recursive: false, mode: 0o700 });
  return directory;
}

function uniqueCanaries(values) {
  return [...new Set(values.filter((value) => typeof value === "string" && value.length >= 8))]
    .sort((left, right) => right.length - left.length);
}

function jwtStringClaims(token) {
  if (typeof token !== "string") return [];
  const payload = token.split(".")[1];
  if (!payload) return [];
  try {
    const decoded = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    const values = [];
    const visit = (value) => {
      if (typeof value === "string" && value.length >= 8) values.push(value);
      else if (Array.isArray(value)) value.forEach(visit);
      else if (isPlainObject(value)) Object.values(value).forEach(visit);
    };
    visit(decoded);
    return values;
  } catch {
    return [];
  }
}

export function readCodexLunaExternalAuth(authSource, ambientEnvironment = {}) {
  const metadata = ordinaryFile(authSource, "Codex external auth source");
  let auth;
  try {
    auth = JSON.parse(fs.readFileSync(authSource, "utf8"));
  } catch (error) {
    fail("CODEX_LUNA_EXTERNAL_AUTH_INVALID", "Codex external auth source is not valid JSON", { cause: error.message });
  }
  requireRuntime(
    isPlainObject(auth)
      && auth.auth_mode === "chatgpt"
      && (auth.OPENAI_API_KEY === null || auth.OPENAI_API_KEY === undefined)
      && isPlainObject(auth.tokens)
      && AUTH_KEYS.every((key) => typeof auth.tokens[key] === "string" && auth.tokens[key].length > 0),
    "CODEX_LUNA_EXTERNAL_AUTH_INVALID",
    "Codex external auth requires complete ChatGPT tokens and no API key",
  );
  const canaries = uniqueCanaries(collectSecretCanaries(authSource, ambientEnvironment));
  const redactCanaries = uniqueCanaries([
    ...jwtStringClaims(auth.tokens.access_token),
    ...jwtStringClaims(auth.tokens.id_token),
  ]).filter((value) => !canaries.includes(value));
  return {
    access_token: auth.tokens.access_token,
    account_id: auth.tokens.account_id,
    canaries,
    redact_canaries: redactCanaries,
    receipt: {
      schema_version: 1,
      mode: "chatgpt-external-tokens",
      source_sha256: sha256File(authSource),
      byte_count: metadata.size,
      account_id_sha256: sha256Bytes(auth.tokens.account_id),
      access_token_sha256: sha256Bytes(auth.tokens.access_token),
      access_token_length: auth.tokens.access_token.length,
      transfer: "app-server-account-login-start-memory-only",
      transmitted_fields: ["access_token", "account_id"],
      withheld_fields: ["refresh_token", "id_token"],
      credential_persisted: false,
      refresh_policy: "fail-closed-no-refresh-replay",
    },
  };
}

function hasCanary(text, canaries) {
  return canaries.find((canary) => text.includes(canary)) ?? null;
}

function redactExactValues(value, canaries) {
  if (typeof value === "string") {
    const matched = canaries.find((canary) => value.includes(canary));
    if (!matched) return value;
    return { redacted_sha256: sha256Bytes(value), byte_count: Buffer.byteLength(value) };
  }
  if (Array.isArray(value)) return value.map((item) => redactExactValues(item, canaries));
  if (!isPlainObject(value)) return value;
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, redactExactValues(item, canaries)]));
}

function digestValue(value) {
  const payload = canonicalJson(value);
  return { redacted_sha256: sha256Bytes(payload), byte_count: Buffer.byteLength(payload) };
}

const CODEX_ERROR_INFO_SCALARS = new Set([
  "contextWindowExceeded",
  "sessionBudgetExceeded",
  "usageLimitExceeded",
  "serverOverloaded",
  "cyberPolicy",
  "misalignmentPolicyViolation",
  "internalServerError",
  "unauthorized",
  "badRequest",
  "threadRollbackFailed",
  "sandboxError",
  "other",
]);
const CODEX_ERROR_INFO_VARIANTS = [
  "httpConnectionFailed",
  "responseStreamConnectionFailed",
  "responseStreamDisconnected",
  "responseTooManyFailedAttempts",
  "activeTurnNotSteerable",
];

function closedErrorNotificationDetails(params) {
  const source = isPlainObject(params) ? params : {};
  const rawInfo = isPlainObject(source.error) ? source.error.codexErrorInfo : null;
  const scalarInfo = typeof rawInfo === "string" && CODEX_ERROR_INFO_SCALARS.has(rawInfo) ? rawInfo : null;
  const variantInfo = isPlainObject(rawInfo)
    ? CODEX_ERROR_INFO_VARIANTS.find((name) => Object.hasOwn(rawInfo, name)) ?? null
    : null;
  const status = variantInfo === null ? null : rawInfo[variantInfo]?.httpStatusCode;
  return {
    codex_error_info: scalarInfo ?? variantInfo,
    http_status_code: Number.isSafeInteger(status) ? status : null,
    will_retry: typeof source.willRetry === "boolean" ? source.willRetry : null,
  };
}

function sanitizedInboundMessage(message, { redactCanaries }) {
  let safe = redactExactValues(JSON.parse(JSON.stringify(message)), redactCanaries);
  if (safe?.id === CODEX_LUNA_APP_SERVER_REQUEST_IDS.accountRead && isPlainObject(safe.result?.account)) {
    safe.result = {
      account: {
        type: safe.result.account.type,
        planType: safe.result.account.planType,
      },
      requiresOpenaiAuth: safe.result.requiresOpenaiAuth,
    };
  } else if (safe?.method === "account/updated") {
    safe.params = { authMode: safe.params?.authMode ?? null };
  } else if (safe?.method === "rawResponseItem/completed") {
    const rawItem = safe.params?.item;
    const item = { type: rawItem?.type ?? null };
    if (rawItem?.type === "message") item.role = rawItem.role ?? null;
    else if (rawItem?.type === "custom_tool_call") {
      item.name = rawItem.name ?? null;
      item.namespace = rawItem.namespace ?? null;
      item.call_id = rawItem.call_id ?? null;
      item.status = rawItem.status ?? null;
    } else if (rawItem?.type === "custom_tool_call_output") {
      item.name = rawItem.name ?? null;
      item.call_id = rawItem.call_id ?? null;
    }
    else if (rawItem?.type === "function_call") {
      item.name = rawItem.name ?? null;
      item.namespace = rawItem.namespace ?? null;
      item.call_id = rawItem.call_id ?? null;
    } else if (rawItem?.type === "local_shell_call") {
      item.call_id = rawItem.call_id ?? null;
      item.status = rawItem.status ?? null;
      item.action = { type: rawItem.action?.type ?? null };
    } else if (rawItem?.type === "function_call_output") item.call_id = rawItem.call_id ?? null;
    item.content_receipt = digestValue(rawItem ?? null);
    safe.params = {
      threadId: safe.params?.threadId,
      turnId: safe.params?.turnId,
      item,
    };
  } else if (safe?.method === "turn/diff/updated") {
    safe.params = {
      threadId: safe.params?.threadId,
      turnId: safe.params?.turnId,
      diff_receipt: digestValue(safe.params ?? null),
    };
  } else if (safe?.method === "account/rateLimits/updated") {
    safe.params = { state_receipt: digestValue(safe.params ?? null) };
  } else if (safe?.method === "warning") {
    safe.params = {
      threadId: safe.params?.threadId ?? null,
      message_receipt: digestValue(safe.params?.message ?? null),
    };
  } else if (safe?.method === "error") {
    const params = safe.params ?? {};
    const details = closedErrorNotificationDetails(params);
    safe.params = {
      threadId: typeof params.threadId === "string" ? params.threadId : null,
      turnId: typeof params.turnId === "string" ? params.turnId : null,
      ...details,
      error_receipt: digestValue(params),
    };
  } else if (/\/(?:delta|outputDelta)$/.test(safe?.method ?? "")) {
    safe.params = {
      threadId: safe.params?.threadId,
      turnId: safe.params?.turnId,
      itemId: safe.params?.itemId ?? null,
      delta: digestValue(safe.params ?? null),
    };
  } else if (["item/started", "item/completed"].includes(safe?.method) && safe.params?.item?.type === "fileChange") {
    const rawItem = safe.params.item;
    safe.params.item = {
      type: rawItem.type,
      id: rawItem.id,
      status: rawItem.status,
      changes: Array.isArray(rawItem.changes) ? rawItem.changes.map((change) => ({
        path: change?.path ?? null,
        kind: {
          type: change?.kind?.type ?? null,
          move_path: change?.kind?.move_path ?? null,
        },
        diff_receipt: digestValue(change?.diff ?? null),
      })) : null,
    };
  } else if (["item/started", "item/completed"].includes(safe?.method) && safe.params?.item?.type === "commandExecution") {
    if (safe.params.item.aggregatedOutput !== null && safe.params.item.aggregatedOutput !== undefined) {
      safe.params.item.aggregatedOutput = digestValue(safe.params.item.aggregatedOutput);
    }
  } else if (["item/started", "item/completed"].includes(safe?.method) && ["reasoning", "plan"].includes(safe.params?.item?.type)) {
    const identity = { type: safe.params.item.type, id: safe.params.item.id };
    safe.params.item = { ...identity, content_receipt: digestValue(safe.params.item) };
  }
  return safe;
}

function safeOutboundReceipt(message) {
  const params = message.params ?? null;
  return {
    schema_version: 1,
    method: message.method,
    id: Object.hasOwn(message, "id") ? message.id : null,
    params_sha256: sha256Bytes(canonicalJson(params)),
  };
}

function terminateProcessGroup(child) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  try {
    if (process.platform !== "win32" && child.pid) process.kill(-child.pid, "SIGTERM");
    else child.kill("SIGTERM");
  } catch {}
  setTimeout(() => {
    if (child.exitCode !== null || child.signalCode !== null) return;
    try {
      if (process.platform !== "win32" && child.pid) process.kill(-child.pid, "SIGKILL");
      else child.kill("SIGKILL");
    } catch {}
  }, 5_000).unref();
}

function spawnProbe(codexEntry, profileId, workspaceRoot, environment, command) {
  return spawnSync(codexEntry, ["sandbox", "-P", profileId, "-C", workspaceRoot, "--", ...command], {
    cwd: workspaceRoot,
    env: environment,
    encoding: "utf8",
    timeout: 30_000,
    stdio: ["ignore", "pipe", "pipe"],
  });
}

async function loopbackProbe(codexEntry, profileId, workspaceRoot, environment) {
  const server = net.createServer(() => {});
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen({ host: "127.0.0.1", port: 0 }, resolve);
  });
  const address = server.address();
  try {
    requireRuntime(isPlainObject(address) && Number.isSafeInteger(address.port), "CODEX_LUNA_PERMISSION_NETWORK_PROBE_INVALID", "Loopback probe did not obtain a port");
    const result = spawnProbe(codexEntry, profileId, workspaceRoot, environment, ["/usr/bin/nc", "-z", "-w", "1", "127.0.0.1", String(address.port)]);
    return { status: result.status === 0 && result.signal === null && !result.error ? "ALLOWED" : "DENIED", endpoint: "ipv4-loopback-listener", exit_code: result.status };
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

export async function probeCodexLunaPermissionProfile({
  codexEntry,
  profile,
  workspaceRoot,
  skillPath,
  forbiddenReadPaths,
  environment,
}) {
  const readProbe = spawnProbe(codexEntry, profile.profile_id, workspaceRoot, environment, ["/bin/cat", skillPath]);
  requireRuntime(readProbe.status === 0 && readProbe.signal === null && !readProbe.error && String(readProbe.stdout).length > 0, "CODEX_LUNA_PERMISSION_WORKSPACE_READ_FAILED", "Permission profile could not read the intended Skill");
  const readDenials = forbiddenReadPaths.map((forbiddenPath) => {
    ordinaryFile(forbiddenPath, "permission negative-probe target");
    const denial = spawnProbe(codexEntry, profile.profile_id, workspaceRoot, environment, ["/bin/cat", forbiddenPath]);
    requireRuntime(denial.status !== 0 && denial.signal === null && !denial.error && String(denial.stdout ?? "").length === 0, "CODEX_LUNA_PERMISSION_READ_NOT_DENIED", "Permission profile allowed a forbidden read", { path_sha256: sha256Bytes(path.resolve(forbiddenPath)) });
    return { status: "DENIED", path_sha256: sha256Bytes(path.resolve(forbiddenPath)), exit_code: denial.status };
  });
  const writeProbePath = path.join(workspaceRoot, ".test-flow-permission-write-probe");
  requireRuntime(!fs.existsSync(writeProbePath), "CODEX_LUNA_PERMISSION_PROBE_COLLISION", "Permission write probe path already exists");
  const writeProbe = spawnProbe(codexEntry, profile.profile_id, workspaceRoot, environment, ["/usr/bin/touch", writeProbePath]);
  const expectedWrite = profile.invocation_mode === "generation";
  if (expectedWrite) {
    requireRuntime(writeProbe.status === 0 && fs.existsSync(writeProbePath), "CODEX_LUNA_PERMISSION_WORKSPACE_WRITE_FAILED", "Generation profile could not write its workspace");
    fs.unlinkSync(writeProbePath);
  } else {
    requireRuntime(writeProbe.status !== 0 && !fs.existsSync(writeProbePath), "CODEX_LUNA_PERMISSION_WORKSPACE_WRITE_NOT_DENIED", "Diagnosis profile allowed a workspace write");
  }
  const networkProbe = await loopbackProbe(codexEntry, profile.profile_id, workspaceRoot, environment);
  const expectedNetworkStatus = profile.network_enabled ? "ALLOWED" : "DENIED";
  requireRuntime(networkProbe.status === expectedNetworkStatus, profile.network_enabled ? "CODEX_LUNA_PERMISSION_NETWORK_NOT_ALLOWED" : "CODEX_LUNA_PERMISSION_NETWORK_NOT_DENIED", `Permission profile command network did not match ${expectedNetworkStatus}`);
  return {
    schema_version: 1,
    status: "PASS",
    profile_id: profile.profile_id,
    profile_sha256: profile.config_sha256,
    workspace_path_sha256: sha256Bytes(path.resolve(workspaceRoot)),
    workspace_read: "PASS",
    workspace_write: expectedWrite ? "ALLOWED" : "DENIED",
    command_network: networkProbe,
    forbidden_reads: readDenials,
  };
}

export function generateCodexLunaProtocolSchemaReceipt({ codexEntry, schemaRoot, environment }) {
  requireRuntime(!fs.existsSync(schemaRoot), "CODEX_LUNA_PROTOCOL_SCHEMA_ROOT_EXISTS", "Protocol schema output root must not exist");
  const result = spawnSync(codexEntry, ["app-server", "generate-json-schema", "--experimental", "--out", schemaRoot], {
    cwd: path.dirname(schemaRoot),
    env: environment,
    encoding: "utf8",
    timeout: 120_000,
    stdio: ["ignore", "pipe", "pipe"],
  });
  requireRuntime(result.status === 0 && result.signal === null && !result.error, "CODEX_LUNA_PROTOCOL_SCHEMA_GENERATION_FAILED", "Pinned Codex app-server schema generation failed", { exit_code: result.status });
  const manifest = treeManifest(schemaRoot);
  const digest = treeDigest(schemaRoot);
  requireRuntime(manifest.length === 401 && digest === CODEX_LUNA_APP_SERVER_SCHEMA_TREE_SHA256, "CODEX_LUNA_PROTOCOL_SCHEMA_IDENTITY_MISMATCH", "Pinned app-server protocol schema tree differs from the frozen identity", { file_count: manifest.length, expected: CODEX_LUNA_APP_SERVER_SCHEMA_TREE_SHA256, actual: digest });
  return {
    schema_version: 1,
    status: "PASS",
    experimental: true,
    file_count: manifest.length,
    tree_sha256: digest,
    manifest,
  };
}

function notificationMatches(messages, method, predicate) {
  return messages.some((message) => message?.method === method && predicate(message.params));
}

function persistTranscript(tracePath, messages) {
  const payload = messages.map((message, index) => canonicalJson({
    schema_version: 1,
    seq: index + 1,
    direction: "server_to_client",
    message,
  })).join("\n");
  writeFileExclusive(tracePath, `${payload}\n`);
  return { path_sha256: sha256Bytes(path.resolve(tracePath)), sha256: sha256File(tracePath), size: fs.statSync(tracePath).size };
}

function authJsonPaths(root) {
  return treeManifest(root).filter((entry) => path.posix.basename(entry.path) === "auth.json");
}

export function installServiceSkillInCodexHome(codexHome, sourceSkillPath) {
  ordinaryFile(sourceSkillPath, "service Skill");
  const source = fs.readFileSync(sourceSkillPath, "utf8");
  const declaredName = source.match(/^name:\s*([a-z0-9][a-z0-9-]{0,63})\s*$/m)?.[1] ?? null;
  const skillName = declaredName;
  requireRuntime(/^[a-z0-9][a-z0-9-]{0,63}$/.test(skillName), "CODEX_LUNA_SERVICE_SKILL_NAME_INVALID", "Service Skill directory has an invalid name");
  const destination = path.join(codexHome, "skills", skillName, "SKILL.md");
  fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 });
  fs.writeFileSync(destination, source, { encoding: "utf8", flag: "wx", mode: 0o400 });
  fs.chmodSync(destination, 0o400);
  return destination;
}

export async function runCodexLunaAppServerCall({
  codexEntry,
  auth,
  environment,
  workspaceRoot,
  skillPath,
  mode,
  mcpServer = null,
  disabledSkillPaths = [],
  prompt,
  developerInstructions = null,
  maxMcpToolCalls = null,
  outputSchema,
  callRoot,
  privateRoot,
  tracePath,
  stderrPath,
  finalPath,
  forbiddenReadPaths,
  wallSeconds,
  noProgressSeconds,
  shellHome: requestedShellHome = null,
  shellPath: requestedShellPath = "/usr/bin:/bin:/usr/sbin:/sbin",
  expectedCliVersion = codexLunaAppServerCliVersion(),
  onProgress = null,
}) {
  requireRuntime(developerInstructions === null || (typeof developerInstructions === "string" && developerInstructions.length > 0 && Buffer.byteLength(developerInstructions, "utf8") <= 4_096), "CODEX_LUNA_APP_SERVER_DEVELOPER_INSTRUCTIONS_INVALID", "Developer instructions must be null or a bounded non-empty string");
  requireRuntime(maxMcpToolCalls === null || (Number.isSafeInteger(maxMcpToolCalls) && maxMcpToolCalls > 0 && maxMcpToolCalls <= 1_000), "CODEX_LUNA_APP_SERVER_MCP_CALL_LIMIT_INVALID", "MCP call limit must be null or a bounded positive integer");
  const codeModeHostPath = path.join(codexLunaHelperDirectory(codexEntry), "codex-code-mode-host");
  const codeModeHostMetadata = ordinaryFile(codeModeHostPath, "Codex code-mode host");
  requireRuntime((codeModeHostMetadata.mode & 0o111) !== 0, "CODEX_LUNA_CODE_MODE_HOST_NOT_EXECUTABLE", "Codex code-mode host must be executable");
  createDirectoryExclusive(callRoot);
  const codexHome = createDirectoryExclusive(path.join(callRoot, "codex-home"));
  const home = createDirectoryExclusive(path.join(callRoot, "home"));
  const temporary = createDirectoryExclusive(path.join(callRoot, "tmp"));
  const configuredSkillPath = mode === "service" ? installServiceSkillInCodexHome(codexHome, skillPath) : skillPath;
  const shellHome = path.resolve(requestedShellHome ?? path.join(workspaceRoot, ".shell-home"));
  if (!fs.existsSync(shellHome)) fs.mkdirSync(shellHome, { recursive: true, mode: 0o700 });
  const childEnvironment = {
    ...environment,
    HOME: home,
    CODEX_HOME: codexHome,
    TMPDIR: temporary,
    TMP: temporary,
    TEMP: temporary,
    LANG: "C.UTF-8",
  };
  childEnvironment.PATH = `${path.dirname(codeModeHostPath)}:${environment.PATH ?? "/usr/bin:/bin:/usr/sbin:/sbin"}`;
  const profile = buildCodexLunaIsolatedConfig({
    workspaceRoot,
    skillPath: configuredSkillPath,
    codexHome,
    shellHome,
    shellPath: requestedShellPath,
    shellLang: "C.UTF-8",
    mode,
    mcpServer,
    disabledSkillPaths,
  });
  writeFileExclusive(path.join(codexHome, "config.toml"), profile.config_toml);
  const preflight = mode === "service"
    ? { schema_version: 1, status: "SKIP", reason: "standalone-lightweight-service" }
    : await probeCodexLunaPermissionProfile({ codexEntry, profile, workspaceRoot, skillPath, forbiddenReadPaths, environment: childEnvironment });

  const appServerArguments = buildCodexLunaAppServerArguments({ workspaceRoot });
  const child = spawn(codexEntry, appServerArguments, {
    cwd: workspaceRoot,
    env: childEnvironment,
    stdio: ["pipe", "pipe", "pipe"],
    detached: process.platform !== "win32",
  });
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  const inbound = [];
  const persistedInbound = [];
  const outbound = [];
  const pendingResponses = new Map();
  const notificationWaiters = [];
  const stderrChunks = [];
  let stdoutPending = "";
  let stdoutBytes = 0;
  let stderrBytes = 0;
  let fatalError = null;
  let timedOut = false;
  let noProgressTimedOut = false;
  let completedTurn = false;
  let cleanupStarted = false;
  let mcpToolCallsStarted = 0;
  let noProgressTimer;

  const rejectWaiters = (error) => {
    for (const pending of pendingResponses.values()) pending.reject(error);
    pendingResponses.clear();
    for (const waiter of notificationWaiters.splice(0)) waiter.reject(error);
  };
  const abort = (error) => {
    if (fatalError) return;
    fatalError = error;
    rejectWaiters(error);
    terminateProcessGroup(child);
  };
  const armNoProgress = () => {
    clearTimeout(noProgressTimer);
    noProgressTimer = setTimeout(() => {
      noProgressTimedOut = true;
      abort(new CodexLunaAppServerRuntimeError("CODEX_LUNA_APP_SERVER_NO_PROGRESS_TIMEOUT", "Codex app-server invocation made no progress"));
    }, noProgressSeconds * 1_000);
  };
  const wallTimer = setTimeout(() => {
    timedOut = true;
    abort(new CodexLunaAppServerRuntimeError("CODEX_LUNA_APP_SERVER_WALL_TIMEOUT", "Codex app-server invocation exceeded its wall limit"));
  }, wallSeconds * 1_000);
  armNoProgress();

  const closePromise = new Promise((resolve) => {
    child.once("error", (error) => {
      abort(new CodexLunaAppServerRuntimeError("CODEX_LUNA_APP_SERVER_SPAWN_FAILED", "Codex app-server could not start", { cause: error?.code ?? "UNKNOWN" }));
    });
    child.once("close", (code, signal) => {
      if ((!cleanupStarted || pendingResponses.size > 0) && !fatalError) {
        abort(new CodexLunaAppServerRuntimeError("CODEX_LUNA_APP_SERVER_EARLY_EXIT", "Codex app-server exited before validated cleanup", { code, signal }));
      }
      resolve({ code, signal });
    });
  });
  child.stdin.once("error", (error) => {
    abort(new CodexLunaAppServerRuntimeError("CODEX_LUNA_APP_SERVER_STDIN_FAILED", "Codex app-server stdin failed", { cause: error?.code ?? "UNKNOWN" }));
  });

  const resolveNotificationWaiters = (message) => {
    for (let index = notificationWaiters.length - 1; index >= 0; index -= 1) {
      const waiter = notificationWaiters[index];
      if (message.method === waiter.method && waiter.predicate(message.params)) {
        notificationWaiters.splice(index, 1);
        waiter.resolve(message.params);
      }
    }
  };
  const handleLine = (line) => {
    const canary = hasCanary(line, auth.canaries);
    if (canary) {
      abort(new CodexLunaAppServerRuntimeError("CODEX_LUNA_APP_SERVER_SECRET_OUTPUT", "Codex app-server emitted a credential canary", { canary_sha256: sha256Bytes(canary) }));
      return;
    }
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      abort(new CodexLunaAppServerRuntimeError("CODEX_LUNA_APP_SERVER_JSONL_INVALID", "Codex app-server stdout contains invalid JSONL"));
      return;
    }
    if (!isPlainObject(message)) {
      abort(new CodexLunaAppServerRuntimeError("CODEX_LUNA_APP_SERVER_MESSAGE_INVALID", "Codex app-server emitted a non-object message"));
      return;
    }
    armNoProgress();
    onProgress?.("app-server-message", 1);
    if (cleanupStarted) {
      const cleanupResponse = message.id === CLEANUP_REQUEST_ID && typeof message.method !== "string";
      const cleanupNotification = (message.method === "account/updated" || (mode === "client" && String(message.method ?? "").startsWith("mcpServer/")))
        && !Object.hasOwn(message, "id");
      if (!cleanupResponse && !cleanupNotification) {
        abort(new CodexLunaAppServerRuntimeError("CODEX_LUNA_APP_SERVER_LATE_MESSAGE_REJECTED", "Codex app-server emitted an unexpected message after the validated turn", { method: message.method ?? null, id: message.id ?? null }));
        return;
      }
    }
    if (typeof message.method === "string" && Object.hasOwn(message, "id")) {
      try {
        child.stdin.write(`${canonicalJson({ id: message.id, error: { code: -32601, message: "Test Flow denies all server requests" } })}\n`);
      } catch {}
      abort(new CodexLunaAppServerRuntimeError("CODEX_LUNA_APP_SERVER_SERVER_REQUEST_REJECTED", "Codex app-server requested an interactive or privileged client action", { method: message.method }));
      return;
    }
    inbound.push(message);
    if (!cleanupStarted) persistedInbound.push(sanitizedInboundMessage(message, { redactCanaries: auth.redact_canaries }));
    if (message.method === "item/started" && message.params?.item?.type === "mcpToolCall") {
      mcpToolCallsStarted += 1;
      if (maxMcpToolCalls !== null && mcpToolCallsStarted > maxMcpToolCalls) {
        abort(new CodexLunaAppServerRuntimeError("CODEX_LUNA_APP_SERVER_MCP_CALL_LIMIT", "Codex app-server exceeded the bounded MCP call count", { limit: maxMcpToolCalls, observed: mcpToolCallsStarted }));
        return;
      }
    }
    if (Object.hasOwn(message, "id") && typeof message.method !== "string") {
      const pending = pendingResponses.get(`${typeof message.id}:${String(message.id)}`);
      if (!pending) {
        abort(new CodexLunaAppServerRuntimeError("CODEX_LUNA_APP_SERVER_UNEXPECTED_RESPONSE", "Codex app-server emitted a response for an unknown request", { id: message.id }));
        return;
      }
      pendingResponses.delete(`${typeof message.id}:${String(message.id)}`);
      if (Object.hasOwn(message, "error")) pending.reject(new CodexLunaAppServerRuntimeError("CODEX_LUNA_APP_SERVER_RESPONSE_ERROR", "Codex app-server request failed", {
        id: message.id,
        response_code: Number.isSafeInteger(message.error?.code) ? message.error.code : null,
        response_message: typeof message.error?.message === "string" ? message.error.message.slice(0, 1024) : null,
      }));
      else pending.resolve(message.result);
    } else if (typeof message.method === "string") {
      if (message.method === "error") {
        const details = closedErrorNotificationDetails(message.params);
        if (details.will_retry === true) {
          resolveNotificationWaiters(message);
          return;
        }
        abort(new CodexLunaAppServerRuntimeError(
          details.will_retry === false ? "CODEX_LUNA_APP_SERVER_ERROR_NOTIFICATION" : "CODEX_LUNA_APP_SERVER_ERROR_NOTIFICATION_INVALID",
          details.will_retry === false ? "Codex app-server emitted a terminal error notification" : "Codex app-server error notification omitted its retry decision",
          details,
        ));
        return;
      }
      resolveNotificationWaiters(message);
    } else {
      abort(new CodexLunaAppServerRuntimeError("CODEX_LUNA_APP_SERVER_MESSAGE_INVALID", "Codex app-server emitted an invalid JSON-RPC message"));
    }
  };
  child.stdout.on("data", (chunk) => {
    stdoutBytes += Buffer.byteLength(chunk, "utf8");
    if (stdoutBytes > MAX_STDOUT_BYTES) {
      abort(new CodexLunaAppServerRuntimeError("CODEX_LUNA_APP_SERVER_STDOUT_LIMIT", "Codex app-server stdout exceeded its memory boundary"));
      return;
    }
    stdoutPending += chunk;
    for (;;) {
      const newline = stdoutPending.indexOf("\n");
      if (newline < 0) break;
      const line = stdoutPending.slice(0, newline).trim();
      stdoutPending = stdoutPending.slice(newline + 1);
      if (line) handleLine(line);
    }
  });
  child.stderr.on("data", (chunk) => {
    stderrBytes += Buffer.byteLength(chunk, "utf8");
    if (stderrBytes > MAX_STDERR_BYTES) {
      abort(new CodexLunaAppServerRuntimeError("CODEX_LUNA_APP_SERVER_STDERR_LIMIT", "Codex app-server stderr exceeded its memory boundary"));
      return;
    }
    const text = chunk;
    const canary = hasCanary(text, auth.canaries);
    if (canary) {
      abort(new CodexLunaAppServerRuntimeError("CODEX_LUNA_APP_SERVER_SECRET_OUTPUT", "Codex app-server stderr emitted a credential canary", { canary_sha256: sha256Bytes(canary) }));
      return;
    }
    stderrChunks.push(text);
  });

  const send = (message) => {
    requireRuntime(!fatalError, fatalError?.code ?? "CODEX_LUNA_APP_SERVER_ABORTED", fatalError?.message ?? "Codex app-server is unavailable");
    outbound.push(safeOutboundReceipt(message));
    child.stdin.write(`${canonicalJson(message)}\n`);
  };
  const request = (message) => new Promise((resolve, reject) => {
    const key = `${typeof message.id}:${String(message.id)}`;
    requireRuntime(!pendingResponses.has(key), "CODEX_LUNA_APP_SERVER_REQUEST_DUPLICATE", "Duplicate app-server request id");
    pendingResponses.set(key, { resolve, reject });
    try { send(message); } catch (error) { pendingResponses.delete(key); reject(error); }
  });
  const waitNotification = (method, predicate) => {
    const existing = inbound.find((message) => message?.method === method && predicate(message.params));
    if (existing) return Promise.resolve(existing.params);
    return new Promise((resolve, reject) => notificationWaiters.push({ method, predicate, resolve, reject }));
  };

  let parsed;
  let baseEvidence;
  let loginReceipt;
  let cleanupResponse = null;
  try {
    await request(buildCodexLunaInitializeRequest());
    send(buildCodexLunaInitializedNotification());
    const loginPromise = new Promise((resolve, reject) => {
      const id = CODEX_LUNA_APP_SERVER_REQUEST_IDS.login;
      pendingResponses.set(`${typeof id}:${String(id)}`, { resolve, reject });
    });
    loginReceipt = writeExternalChatgptAuthLoginRequest(child.stdin, {
      accessToken: auth.access_token,
      chatgptAccountId: auth.account_id,
    });
    outbound.push({
      schema_version: 1,
      method: loginReceipt.method,
      id: loginReceipt.id,
      params_sha256: null,
      auth: {
        type: loginReceipt.auth_type,
        account_id_sha256: loginReceipt.account_id_sha256,
        access_token_sha256: auth.receipt.access_token_sha256,
        access_token_length: auth.receipt.access_token_length,
        credential_returned: false,
      },
    });
    await loginPromise;
    await Promise.all([
      waitNotification("account/login/completed", (params) => params?.success === true && params?.error === null),
      waitNotification("account/updated", (params) => params?.authMode === "chatgptAuthTokens"),
    ]);
    await request(buildCodexLunaAccountReadRequest());
    await request(buildCodexLunaPermissionProfileListRequest({ workspaceRoot }));
    await request(buildCodexLunaSkillsListRequest({ workspaceRoot }));
    const thread = await request(buildCodexLunaThreadStartRequest({ workspaceRoot, mode, developerInstructions }));
    const threadId = thread?.thread?.id;
    requireRuntime(typeof threadId === "string" && threadId.length > 0, "CODEX_LUNA_APP_SERVER_THREAD_START_INVALID", "thread/start did not return a thread id");
    await request(buildCodexLunaTurnStartRequest({ threadId, prompt, workspaceRoot, skillPath: configuredSkillPath, codexHome, mode, outputSchema }));
    await waitNotification("turn/completed", (params) => params?.threadId === threadId && params?.turn?.status === "completed");
    completedTurn = true;
    parsed = parseCodexLunaAppServerTranscript(persistedInbound, {
      workspaceRoot,
      skillPath: configuredSkillPath,
      codexHome,
      mode,
      expectedCliVersion,
      secretValues: auth.canaries,
    });
    baseEvidence = buildCodexLunaAppServerEvidenceSummary({ profile, transcript: parsed, secretValues: auth.canaries });
    cleanupStarted = true;
    cleanupResponse = await request({ id: CLEANUP_REQUEST_ID, method: "account/logout" });
    child.stdin.end();
  } catch (error) {
    abort(error);
  }

  let closed = await closePromise;
  clearTimeout(wallTimer);
  clearTimeout(noProgressTimer);
  if (stdoutPending.trim().length > 0 && !fatalError) fatalError = new CodexLunaAppServerRuntimeError("CODEX_LUNA_APP_SERVER_JSONL_TRUNCATED", "Codex app-server ended with a partial JSONL frame");
  if (fatalError) {
    if (["CODEX_LUNA_APP_SERVER_WALL_TIMEOUT", "CODEX_LUNA_APP_SERVER_NO_PROGRESS_TIMEOUT", "CODEX_LUNA_APP_SERVER_ERROR_NOTIFICATION", "CODEX_LUNA_APP_SERVER_RAW_SHELL_FUNCTION_REJECTED", "CODEX_LUNA_APP_SERVER_COMMAND_WORKSPACE_INVALID", "CODEX_LUNA_APP_SERVER_MCP_CALL_LIMIT"].includes(fatalError.code) && persistedInbound.length > 0) {
      persistTranscript(tracePath, persistedInbound);
    }
    writeFileExclusive(stderrPath, "[Test Flow withheld app-server stderr after a failed secret/protocol boundary.]\n");
    throw fatalError;
  }
  requireRuntime(completedTurn && parsed && baseEvidence, "CODEX_LUNA_APP_SERVER_TURN_INCOMPLETE", "Codex app-server did not complete one validated turn");
  requireRuntime(closed.code === 0 && closed.signal === null && !timedOut && !noProgressTimedOut, "CODEX_LUNA_APP_SERVER_PROCESS_FAILED", "Codex app-server did not exit cleanly", closed);

  const traceReceipt = persistTranscript(tracePath, persistedInbound);
  writeFileExclusive(stderrPath, stderrChunks.join(""));
  writeFileExclusive(finalPath, `${parsed.final_agent_message}\n`);
  const homeManifest = treeManifest(codexHome);
  const authFiles = authJsonPaths(codexHome);
  requireRuntime(authFiles.length === 0, "CODEX_LUNA_APP_SERVER_AUTH_PERSISTED", "Codex app-server persisted auth.json despite external memory-only auth");
  const appServer = {
    ...baseEvidence,
    outbound,
    feature_disables: [...CODEX_LUNA_DISABLED_FEATURES],
    protocol_schema_tree_sha256: CODEX_LUNA_APP_SERVER_SCHEMA_TREE_SHA256,
    arguments: buildCodexLunaAppServerArguments(),
    mcp_tool_call_limit: maxMcpToolCalls,
    developer_instructions: developerInstructions === null ? null : { sha256: sha256Bytes(developerInstructions), utf8_size: Buffer.byteLength(developerInstructions, "utf8") },
    preflight,
    cleanup: {
      schema_version: 1,
      status: cleanupResponse !== null && cleanupResponse !== undefined ? "PASS" : "FAIL",
      logout_request_id: CLEANUP_REQUEST_ID,
      process_exit_code: closed.code,
      process_signal: closed.signal,
      timed_out: timedOut,
      no_progress_timed_out: noProgressTimedOut,
      stdin_closed: true,
    },
    codex_home: {
      schema_version: 1,
      status: "PASS",
      relative_path: path.relative(privateRoot, codexHome).split(path.sep).join("/"),
      path_sha256: sha256Bytes(path.resolve(codexHome)),
      config_sha256: sha256File(path.join(codexHome, "config.toml")),
      tree_sha256: treeDigest(codexHome),
      manifest: homeManifest,
      auth_json_files: 0,
    },
    code_mode_host: {
      schema_version: 1,
      status: "PASS",
      sha256: sha256File(codeModeHostPath),
      size: codeModeHostMetadata.size,
      path_sha256: sha256Bytes(codeModeHostPath),
    },
    trace_sha256: traceReceipt.sha256,
    final_sha256: sha256File(finalPath),
    login: loginReceipt,
  };
  return {
    schema_version: 1,
    thread_id: parsed.thread_id,
    turn_id: parsed.turn_id,
    turn_count: 1,
    commands: parsed.commands.map((item) => item.command),
    command_receipts: parsed.commands,
    final_text: parsed.final_agent_message,
    usage: parsed.usage,
    app_server: appServer,
    process: {
      exit_code: closed.code,
      signal: closed.signal,
      spawn_error: null,
      timed_out: timedOut,
      no_progress_timed_out: noProgressTimedOut,
    },
  };
}

export function auditCodexLunaRuntimeSecrets({ roots, auth }) {
  const receipt = auditNoSecretLeak({ roots, canaries: auth.canaries });
  requireRuntime(receipt.scanned_files > 0, "CODEX_LUNA_SECRET_SCAN_EMPTY", "Codex Luna secret scan did not examine any files");
  return receipt;
}
