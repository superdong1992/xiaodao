#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { canonicalJson, sha256Bytes } from "../../../lib/util.mjs";
import {
  canonicalizeMethodsDraft,
  runServiceLogparseCommand,
  sealServiceOutcomeDraft,
  serverInvocationPhase,
} from "../../codex-luna/runtime/macos-codex-luna-service-wrapper.mjs";
import {
  CLAUDE_DEEPSEEK_CALL_WALL_SECONDS,
  CLAUDE_DEEPSEEK_E2E_MAX_TURNS,
  CLAUDE_DEEPSEEK_E2E_USD_LIMIT,
  CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
} from "./claude-deepseek-contract.mjs";
import { runClaudeProcess } from "./claude-deepseek-process.mjs";

const MODULE_PATH = fileURLToPath(import.meta.url);
const MAX_PROMPT_BYTES = 4 * 1024 * 1024;

class ServiceWrapperError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "ServiceWrapperError";
    this.code = code;
  }
}

function fail(code, message) {
  throw new ServiceWrapperError(code, message);
}

export function parseArguments(argv) {
  const values = {};
  const names = new Set(["source-root", "runtime-root", "claude-entry", "settings", "config-root", "finalizer-entry", "logparse-entry", "private-root", "evidence-root", "usage-root", "run-id"]);
  for (let index = 0; index < argv.length; index += 2) {
    const argument = argv[index];
    if (!argument?.startsWith("--") || index + 1 >= argv.length || argv[index + 1].startsWith("--")) fail("CLAUDE_DEEPSEEK_SERVICE_ARGUMENT_INVALID", "Service wrapper arguments must use --name value pairs");
    const name = argument.slice(2);
    if (!names.has(name)) fail("CLAUDE_DEEPSEEK_SERVICE_ARGUMENT_UNKNOWN", "Service wrapper received an unsupported argument");
    if (Object.hasOwn(values, name)) fail("CLAUDE_DEEPSEEK_SERVICE_ARGUMENT_DUPLICATE", "Service wrapper argument is duplicated");
    values[name] = argv[index + 1];
  }
  if (![...names].every((name) => typeof values[name] === "string" && values[name])) fail("CLAUDE_DEEPSEEK_SERVICE_ARGUMENT_MISSING", "Service wrapper arguments are incomplete");
  return values;
}

async function readPrompt(stream) {
  const chunks = [];
  let size = 0;
  for await (const chunk of stream) {
    size += chunk.length;
    if (size > MAX_PROMPT_BYTES) fail("CLAUDE_DEEPSEEK_SERVICE_PROMPT_LIMIT", "Server Agent prompt exceeds the wrapper byte cap");
    chunks.push(Buffer.from(chunk));
  }
  let prompt;
  try { prompt = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)); } catch { fail("CLAUDE_DEEPSEEK_SERVICE_PROMPT_UTF8", "Server Agent prompt must be UTF-8"); }
  if (!prompt.trim()) fail("CLAUDE_DEEPSEEK_SERVICE_PROMPT_EMPTY", "Server Agent prompt is empty");
  return prompt;
}

function writeJsonNew(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, canonicalJson(value), { encoding: "utf8", mode: 0o600, flag: "wx" });
}

function claimPhase(privateRoot, phase) {
  const claim = path.join(privateRoot, "server-claims", phase.toLowerCase());
  fs.mkdirSync(path.dirname(claim), { recursive: true, mode: 0o700 });
  try { fs.mkdirSync(claim, { mode: 0o700 }); } catch (error) {
    if (error.code === "EEXIST") fail("CLAUDE_DEEPSEEK_SERVICE_RETRY_FORBIDDEN", `Server phase ${phase} already has one Claude process`);
    throw error;
  }
  return claim;
}

function brokerEnvironment(ambient) {
  const keys = ["PROBLEM_LOCATOR_LOGPARSE_ENDPOINT", "PROBLEM_LOCATOR_LOGPARSE_TOKEN"];
  const present = keys.filter((key) => typeof ambient[key] === "string" && ambient[key]);
  if (![0, 2].includes(present.length)) fail("CLAUDE_DEEPSEEK_SERVICE_BROKER_ENV_INVALID", "Logparse broker environment must be absent or complete");
  return present.length === 2 ? Object.fromEntries(keys.map((key) => [key, ambient[key]])) : null;
}

export function boundedServicePrompt(phase, prompt) {
  const reviewBoundary = phase === "REVIEW"
    ? " For REVIEW, copy every existing candidate limitation sentence verbatim into the review limitations; do not translate, paraphrase, summarize, or drop it."
    : "";
  return `Controlled ${phase} Job boundary: the current working directory is the only readable workspace. Do not read repository, source, test-flow, AGENTS, settings, or paths outside this Job workspace. Do not attempt Bash, Glob, Grep, or another discovery tool; use only explicit Read paths supplied by the frozen prompt and resource manifest. Follow the frozen product prompt directly, read only files it identifies, and write only the required output/* draft. Write every user-visible statement, reason, limitation, safety note, and recommendation in natural Simplified Chinese. When a method confirms queuing from a single target history record but cannot identify a specific prior contributor, state 无法确认具体贡献者 explicitly.${reviewBoundary} The harness runs any fixed product finalizer or Logparse command after the model process.\n\n${prompt}`;
}

export async function runServiceInvocation(values, { stdin = process.stdin, stdout = process.stdout, ambient = process.env } = {}) {
  const workspaceRoot = process.cwd();
  const prompt = await readPrompt(stdin);
  const phase = serverInvocationPhase(prompt, workspaceRoot);
  const privateRoot = path.resolve(values["private-root"]);
  const evidenceRoot = path.resolve(values["evidence-root"]);
  const usageRoot = path.resolve(values["usage-root"]);
  const claim = claimPhase(privateRoot, phase);
  const broker = brokerEnvironment(ambient);
  const preLogparse = runServiceLogparseCommand({ phase, prompt, workspaceRoot, logparseEntry: path.resolve(values["logparse-entry"]), environment: broker ?? {} });
  const home = path.join(claim, "home");
  const temporary = path.join(claim, "tmp");
  for (const directory of [home, temporary]) fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const traceRoot = path.join(evidenceRoot, "server-invocations");
  const allowedTools = ["Read(/**)", "Edit(/output/**)", "Skill(logparse-diagnose)", "Skill(diagnose-rpc-timeout)"];
  const result = await runClaudeProcess({
    claudeEntry: path.resolve(values["claude-entry"]),
    settings: path.resolve(values.settings),
    cwd: workspaceRoot,
    prompt: boundedServicePrompt(phase, prompt),
    phase,
    invocationId: `${values["run-id"]}:server:${phase.toLowerCase()}`,
    tools: ["Read", "Write", "Skill"],
    allowedTools,
    allowToolErrors: true,
    auditOnlyAllowedTools: ["Bash", "Glob"],
    maxTurns: CLAUDE_DEEPSEEK_E2E_MAX_TURNS,
    maxBudgetUsd: CLAUDE_DEEPSEEK_E2E_USD_LIMIT,
    wallTimeoutSeconds: CLAUDE_DEEPSEEK_CALL_WALL_SECONDS,
    noProgressSeconds: CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
    tracePath: path.join(traceRoot, `${phase.toLowerCase()}.stream-json.ndjson`),
    stderrPath: path.join(traceRoot, `${phase.toLowerCase()}.stderr.txt`),
    receiptPath: path.join(usageRoot, `${phase.toLowerCase()}.json`),
    environment: { configRoot: path.resolve(values["config-root"]), home, temporary, brokerEnvironment: broker },
  }, { ambient, onProgress: () => stdout.write(`QUICK_VALIDATION_PROGRESS service-agent ${phase}\n`) });
  if (result.records.some((record) => record.name === "Bash" && record.is_error !== true)) fail("CLAUDE_DEEPSEEK_SERVICE_BASH_EXECUTED", "Service Claude process executed forbidden Bash");
  const methodsDraft = canonicalizeMethodsDraft({ phase, workspaceRoot });
  const outcomeSealer = sealServiceOutcomeDraft({ phase, workspaceRoot, finalizerEntry: path.resolve(values["finalizer-entry"]) });
  const receipt = {
    ...result.receipt,
    pre_logparse: preLogparse,
    methods_draft: methodsDraft,
    outcome_sealer: outcomeSealer,
    broker_environment: { present: broker !== null, key_names: broker === null ? [] : Object.keys(broker).sort(), values_persisted: false },
    workspace_sha256: sha256Bytes(workspaceRoot),
  };
  writeJsonNew(path.join(traceRoot, `${phase.toLowerCase()}.receipt.json`), receipt);
  const terminal = result.events.at(-1);
  stdout.write(`${String(terminal?.result ?? "")}\n`);
  return receipt;
}

export function safeServiceError(error) {
  return { schema_version: 1, status: "FAIL", code: error?.code ?? "CLAUDE_DEEPSEEK_SERVICE_WRAPPER_FAILED", message: error?.message ?? String(error) };
}

async function main() {
  try { await runServiceInvocation(parseArguments(process.argv.slice(2))); }
  catch (error) { process.stderr.write(canonicalJson(safeServiceError(error))); process.exitCode = 1; }
}

if (process.argv[1] && path.resolve(process.argv[1]) === MODULE_PATH) await main();
