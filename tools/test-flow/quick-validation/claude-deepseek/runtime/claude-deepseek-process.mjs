import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";

import { canonicalJson, sha256Bytes } from "../../../lib/util.mjs";
import {
  ISOLATED_AGENT_ENV_POLICY_VERSION,
  environmentKeySummary,
} from "../../../runtime-support/isolated-agent-env.mjs";
import {
  CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS,
  CLAUDE_DEEPSEEK_MODEL,
  auditClaudeStream,
} from "./claude-deepseek-contract.mjs";

export class ClaudeDeepseekProcessError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "ClaudeDeepseekProcessError";
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) {
  throw new ClaudeDeepseekProcessError(code, message, details);
}

function requireProcess(condition, code, message, details = {}) {
  if (!condition) fail(code, message, details);
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function writeNew(filePath, bytes) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, bytes, { mode: 0o600, flag: "wx" });
}

export function controlledClaudeEnvironment(ambient, { configRoot, home, temporary, brokerEnvironment = null, pathEntries = [] } = {}) {
  for (const value of [configRoot, home, temporary]) requireProcess(typeof value === "string" && path.isAbsolute(value), "CLAUDE_DEEPSEEK_ENV_ROOT_INVALID", "Claude environment roots must be absolute");
  requireProcess(Array.isArray(pathEntries) && pathEntries.every((value) => typeof value === "string" && path.isAbsolute(value) && !value.includes(path.delimiter)), "CLAUDE_DEEPSEEK_ENV_PATH_INVALID", "Claude PATH additions must be absolute directories");
  const environment = {
    PATH: [...pathEntries, "/usr/bin", "/bin", "/usr/sbin", "/sbin"].join(path.delimiter),
    HOME: home,
    TMPDIR: temporary,
    LANG: ambient.LANG ?? "C.UTF-8",
    CLAUDE_CONFIG_DIR: configRoot,
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
    CLAUDE_CODE_MAX_OUTPUT_TOKENS: String(CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS),
    NO_PROXY: "127.0.0.1,localhost",
    no_proxy: "127.0.0.1,localhost",
  };
  for (const key of ["SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS"]) if (typeof ambient[key] === "string" && ambient[key]) environment[key] = ambient[key];
  if (brokerEnvironment !== null) {
    const keys = ["PROBLEM_LOCATOR_LOGPARSE_ENDPOINT", "PROBLEM_LOCATOR_LOGPARSE_TOKEN"];
    requireProcess(isPlainObject(brokerEnvironment) && keys.every((key) => typeof brokerEnvironment[key] === "string" && brokerEnvironment[key]), "CLAUDE_DEEPSEEK_BROKER_ENV_INVALID", "Logparse broker environment must be complete");
    for (const key of keys) environment[key] = brokerEnvironment[key];
  }
  return environment;
}

function parseJsonLines(bytes) {
  try { return bytes.toString("utf8").split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line)); }
  catch { fail("CLAUDE_DEEPSEEK_STREAM_JSON_INVALID", "Claude stdout is not valid stream-json JSONL"); }
}

function walkToolBlocks(events, { allowToolErrors = false } = {}) {
  const uses = new Map();
  const ordered = [];
  for (const event of events) {
    const content = event?.message?.content;
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      if (block?.type === "tool_use") {
        requireProcess(typeof block.id === "string" && !uses.has(block.id), "CLAUDE_DEEPSEEK_TOOL_USE_INVALID", "Tool use IDs must be unique");
        const record = { ordinal: ordered.length, id: block.id, name: block.name, input: block.input ?? null, result: null, is_error: null };
        uses.set(block.id, record);
        ordered.push(record);
      } else if (block?.type === "tool_result") {
        const record = uses.get(block.tool_use_id);
        requireProcess(record && record.result === null, "CLAUDE_DEEPSEEK_TOOL_RESULT_INVALID", "Tool result must match one preceding tool use");
        record.result = event.tool_use_result ?? block.content ?? null;
        record.is_error = block.is_error === true;
      }
    }
  }
  requireProcess(ordered.every((record) => record.result !== null), "CLAUDE_DEEPSEEK_TOOL_RESULT_MISSING", "Every Claude tool use must have one result");
  requireProcess(allowToolErrors || ordered.every((record) => record.is_error === false), "CLAUDE_DEEPSEEK_TOOL_RESULT_ERROR", "Claude tool errors are forbidden in this phase");
  return ordered;
}

function normalizedMcpName(name) {
  if (typeof name !== "string") return null;
  const marker = "problem_locator_";
  const index = name.lastIndexOf(marker);
  return index < 0 ? null : name.slice(index);
}

function resultEnvelope(result) {
  if (isPlainObject(result?.structuredContent)) return result.structuredContent;
  if (isPlainObject(result)) return result;
  if (typeof result === "string") {
    try { return JSON.parse(result); } catch { return { raw: result }; }
  }
  return { raw: result };
}

export function projectClaudeTools(events, options = {}) {
  const records = walkToolBlocks(events, options);
  const mcp = [];
  const bash = [];
  const skills = [];
  const denied = [];
  for (const record of records) {
    if (record.is_error === true) {
      const command = record.name === "Bash" && typeof record.input?.command === "string" ? record.input.command.trim() : "";
      denied.push({
        ordinal: record.ordinal,
        name: record.name,
        program: command ? command.split(/\s+/u)[0] : null,
        input_sha256: sha256Bytes(canonicalJson(record.input)),
        executed: false,
      });
      continue;
    }
    const tool = normalizedMcpName(record.name);
    if (tool !== null) {
      mcp.push({ ordinal: record.ordinal, server: "problem-locator", tool, full_name: record.name, status: "completed", error: null, arguments: record.input, result: resultEnvelope(record.result) });
    } else if (record.name === "Bash") {
      const result = resultEnvelope(record.result);
      bash.push({ ordinal: record.ordinal, item_id: record.id, command: record.input?.command ?? null, status: "completed", exit_code: Number(result.exitCode ?? result.exit_code ?? 0), stdout: result.stdout ?? "", stderr: result.stderr ?? "" });
    } else if (record.name === "Skill") skills.push({ ordinal: record.ordinal, skill: record.input?.skill ?? null });
  }
  return { records, mcp, bash, skills, denied };
}

export async function runClaudeProcess(options, { ambient = process.env, onProgress = null } = {}) {
  const requiredFiles = [options.claudeEntry, options.settings];
  requireProcess(requiredFiles.every((filePath) => path.isAbsolute(filePath) && fs.existsSync(filePath) && fs.statSync(filePath).isFile()), "CLAUDE_DEEPSEEK_PROCESS_INPUT_MISSING", "Claude entry and settings must be existing absolute files");
  requireProcess(path.isAbsolute(options.cwd) && fs.existsSync(options.cwd) && fs.statSync(options.cwd).isDirectory(), "CLAUDE_DEEPSEEK_PROCESS_CWD_INVALID", "Claude cwd must be an existing absolute directory");
  requireProcess(Array.isArray(options.tools) && options.tools.length > 0 && options.tools.every((item) => typeof item === "string" && item), "CLAUDE_DEEPSEEK_PROCESS_TOOLS_INVALID", "Claude tool inventory is invalid");
  requireProcess(Array.isArray(options.allowedTools) && options.allowedTools.every((item) => typeof item === "string" && item), "CLAUDE_DEEPSEEK_PROCESS_ALLOWLIST_INVALID", "Claude allowedTools is invalid");
  requireProcess(options.disallowedTools === undefined || (Array.isArray(options.disallowedTools) && options.disallowedTools.every((item) => typeof item === "string" && item)), "CLAUDE_DEEPSEEK_PROCESS_DENYLIST_INVALID", "Claude disallowedTools is invalid");
  requireProcess(options.appendSystemPrompt === undefined || (typeof options.appendSystemPrompt === "string" && options.appendSystemPrompt.length > 0 && Buffer.byteLength(options.appendSystemPrompt, "utf8") <= 4_096), "CLAUDE_DEEPSEEK_PROCESS_SYSTEM_PROMPT_INVALID", "Claude appended system prompt must be a bounded non-empty string");
  requireProcess(Number.isSafeInteger(options.maxTurns) && options.maxTurns > 0 && Number.isFinite(options.maxBudgetUsd) && options.maxBudgetUsd > 0 && Number.isSafeInteger(options.wallTimeoutSeconds) && options.wallTimeoutSeconds > 0, "CLAUDE_DEEPSEEK_PROCESS_CAPS_INVALID", "Claude process caps are invalid");
  const environment = controlledClaudeEnvironment(ambient, options.environment);
  const args = [
    options.claudeEntry,
    "-p",
    "--output-format", "stream-json",
    "--verbose",
    "--no-chrome",
    "--no-session-persistence",
    "--setting-sources", "user",
    "--settings", options.settings,
    "--model", CLAUDE_DEEPSEEK_MODEL,
    ...(options.appendSystemPrompt ? ["--append-system-prompt", options.appendSystemPrompt] : []),
    "--max-turns", String(options.maxTurns),
    "--max-budget-usd", String(options.maxBudgetUsd),
    "--tools", options.tools.join(","),
    ...(options.allowedTools.length > 0 ? ["--allowedTools", ...options.allowedTools] : []),
    ...((options.disallowedTools?.length ?? 0) > 0 ? ["--disallowedTools", ...options.disallowedTools] : []),
    "--permission-mode", "dontAsk",
    ...(options.mcpConfig ? ["--mcp-config", options.mcpConfig, "--strict-mcp-config"] : []),
  ];
  const startedAtUtc = new Date().toISOString();
  const stdout = [];
  const stderr = [];
  let timedOut = false;
  let noProgressTimedOut = false;
  let lastProgress = Date.now();
  const exit = await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, args, { cwd: options.cwd, env: environment, stdio: ["pipe", "pipe", "pipe"] });
    const hard = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
      setTimeout(() => child.exitCode === null && child.kill("SIGKILL"), 5_000).unref();
    }, options.wallTimeoutSeconds * 1_000);
    const progress = setInterval(() => {
      if (Date.now() - lastProgress <= options.noProgressSeconds * 1_000) return;
      noProgressTimedOut = true;
      child.kill("SIGTERM");
      setTimeout(() => child.exitCode === null && child.kill("SIGKILL"), 5_000).unref();
    }, 1_000);
    hard.unref();
    progress.unref();
    child.stdout.on("data", (chunk) => { stdout.push(chunk); lastProgress = Date.now(); onProgress?.(options.phase); });
    child.stderr.on("data", (chunk) => { stderr.push(chunk); });
    child.once("error", (error) => { clearTimeout(hard); clearInterval(progress); reject(error); });
    child.once("exit", (code, signal) => { clearTimeout(hard); clearInterval(progress); resolve({ code, signal }); });
    child.stdin.end(options.prompt);
  });
  const stdoutBytes = Buffer.concat(stdout);
  const stderrBytes = Buffer.concat(stderr);
  if (options.tracePath) writeNew(options.tracePath, stdoutBytes);
  if (options.stderrPath) writeNew(options.stderrPath, stderrBytes);
  requireProcess(!timedOut, "CLAUDE_DEEPSEEK_PROCESS_TIMEOUT", "Claude process exceeded its wall timeout");
  requireProcess(!noProgressTimedOut, "CLAUDE_DEEPSEEK_PROCESS_NO_PROGRESS", `Claude process made no semantic stream progress for ${options.noProgressSeconds} seconds`);
  requireProcess(exit.code === 0 && exit.signal === null, "CLAUDE_DEEPSEEK_PROCESS_FAILED", "Claude process exited unsuccessfully", { exit_code: exit.code, signal: exit.signal });
  const events = parseJsonLines(stdoutBytes);
  const stream = auditClaudeStream(events, { phase: options.phase, allowedTools: [...options.tools, ...options.allowedTools, ...(options.auditOnlyAllowedTools ?? [])], maxTurns: options.maxTurns, wallTimeoutSeconds: options.wallTimeoutSeconds });
  const projected = projectClaudeTools(events, { allowToolErrors: options.allowToolErrors === true });
  const receipt = {
    schema_version: 1,
    invocation_id: options.invocationId,
    phase: options.phase,
    model: CLAUDE_DEEPSEEK_MODEL,
    attempt: 1,
    retry: 0,
    status: "PASS",
    terminal: true,
    turns: stream.turns,
    started_at_utc: startedAtUtc,
    finished_at_utc: new Date().toISOString(),
    wall_timeout_seconds: options.wallTimeoutSeconds,
    max_turns: options.maxTurns,
    max_budget_usd: options.maxBudgetUsd,
    max_output_tokens: CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS,
    appended_system_prompt: options.appendSystemPrompt ? { sha256: sha256Bytes(options.appendSystemPrompt), utf8_size: Buffer.byteLength(options.appendSystemPrompt, "utf8") } : null,
    environment_policy: {
      schema_version: 1,
      version: ISOLATED_AGENT_ENV_POLICY_VERSION,
      provider_auth_source: "audited-settings-file",
      inbound: environmentKeySummary(ambient),
      claude_process: environmentKeySummary(environment),
    },
    usage: stream.usage,
    tool_count: projected.records.length,
    denied_tool_attempt_count: projected.denied.length,
    disallowed_tools: options.disallowedTools ?? [],
    mcp_call_count: projected.mcp.length,
    bash_call_count: projected.bash.length,
  };
  if (options.receiptPath) writeNew(options.receiptPath, Buffer.from(canonicalJson(receipt), "utf8"));
  return { receipt, events, ...projected, stdout: stdoutBytes, stderr: stderrBytes };
}
