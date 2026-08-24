#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  canonicalJson,
  CODEX_LUNA_MODEL,
  CODEX_LUNA_REASONING_EFFORT,
  sha256Bytes,
} from "./codex-luna-contract.mjs";
import {
  readCodexLunaExternalAuth,
  runCodexLunaAppServerCall,
} from "./codex-luna-app-server-runtime.mjs";
import {
  MACOS_CODEX_LUNA_CALL_WALL_SECONDS,
  MACOS_CODEX_LUNA_NO_PROGRESS_SECONDS,
} from "./macos-codex-luna-e2e-contract.mjs";

const MODULE_PATH = fileURLToPath(import.meta.url);
const MAX_PROMPT_BYTES = 4 * 1024 * 1024;
const BROKER_KEYS = Object.freeze([
  "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT",
  "PROBLEM_LOCATOR_LOGPARSE_TOKEN",
]);

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

function parseArguments(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    if (!name?.startsWith("--") || index + 1 >= argv.length || argv[index + 1].startsWith("--")) fail("MACOS_CODEX_LUNA_SERVICE_ARGUMENT_INVALID", "Service wrapper arguments must use --name value pairs");
    const key = name.slice(2);
    if (Object.hasOwn(values, key)) fail("MACOS_CODEX_LUNA_SERVICE_ARGUMENT_DUPLICATE", "Service wrapper argument is duplicated");
    values[key] = argv[index + 1];
  }
  const required = ["codex-entry", "auth-source", "skill-source", "private-root", "evidence-root", "usage-root", "run-id"];
  if (!required.every((name) => typeof values[name] === "string" && values[name].length > 0)) fail("MACOS_CODEX_LUNA_SERVICE_ARGUMENT_MISSING", "Service wrapper arguments are incomplete");
  return values;
}

async function readPrompt(stream) {
  const chunks = [];
  let size = 0;
  for await (const chunk of stream) {
    const bytes = Buffer.from(chunk);
    size += bytes.length;
    if (size > MAX_PROMPT_BYTES) fail("MACOS_CODEX_LUNA_SERVICE_PROMPT_LIMIT", "Server Agent prompt exceeds the wrapper byte cap");
    chunks.push(bytes);
  }
  let prompt;
  try { prompt = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)); } catch { fail("MACOS_CODEX_LUNA_SERVICE_PROMPT_UTF8", "Server Agent prompt must be UTF-8"); }
  if (!prompt.trim()) fail("MACOS_CODEX_LUNA_SERVICE_PROMPT_EMPTY", "Server Agent prompt is empty");
  return prompt;
}

export function serverInvocationPhase(prompt, workspaceRoot) {
  if (path.basename(workspaceRoot).endsWith(".logparse-preprocess")) return "LOGPARSE";
  const match = prompt.match(/"job_type"\s*:\s*"(ROUTE|DIAGNOSE|REVIEW)"/);
  if (!match) fail("MACOS_CODEX_LUNA_SERVICE_PHASE_INVALID", "Server Agent prompt does not bind a supported Job type");
  return match[1];
}

function controlledEnvironment(ambient) {
  const allowed = ["LANG", "LC_ALL", "LC_CTYPE", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS"];
  const environment = {};
  for (const key of allowed) if (typeof ambient[key] === "string" && ambient[key].length > 0) environment[key] = ambient[key];
  environment.PATH = "/usr/bin:/bin:/usr/sbin:/sbin";
  environment.LANG = "C.UTF-8";
  for (const key of BROKER_KEYS) if (typeof ambient[key] === "string" && ambient[key].length > 0) environment[key] = ambient[key];
  const brokerKeys = BROKER_KEYS.filter((key) => Object.hasOwn(environment, key));
  if (![0, 2].includes(brokerKeys.length)) fail("MACOS_CODEX_LUNA_SERVICE_BROKER_ENV_INVALID", "Logparse broker environment must be absent or complete");
  return { environment, brokerKeys };
}

function writeJsonExclusive(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, `${canonicalJson(value)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
}

function claimInvocation(privateRoot, phase) {
  const claimsRoot = path.join(privateRoot, "server-claims");
  fs.mkdirSync(claimsRoot, { recursive: true, mode: 0o700 });
  const claim = path.join(claimsRoot, phase.toLowerCase());
  try { fs.mkdirSync(claim, { mode: 0o700 }); } catch (error) {
    if (error.code === "EEXIST") fail("MACOS_CODEX_LUNA_SERVICE_RETRY_FORBIDDEN", `Server phase ${phase} already has an invocation claim`);
    throw error;
  }
  return claim;
}

function installSkill(workspaceRoot, skillSource) {
  const root = path.join(workspaceRoot, "runtime", "tool-state", "codex-service-skill");
  if (fs.existsSync(root)) fail("MACOS_CODEX_LUNA_SERVICE_SKILL_COLLISION", "Server Agent Skill destination already exists");
  fs.mkdirSync(root, { recursive: true, mode: 0o700 });
  const destination = path.join(root, "SKILL.md");
  fs.copyFileSync(skillSource, destination, fs.constants.COPYFILE_EXCL);
  fs.chmodSync(destination, 0o400);
  return destination;
}

export async function runServiceInvocation(values, { stdin = process.stdin, stdout = process.stdout, ambient = process.env } = {}) {
  const workspaceRoot = process.cwd();
  const prompt = await readPrompt(stdin);
  const phase = serverInvocationPhase(prompt, workspaceRoot);
  const privateRoot = path.resolve(values["private-root"]);
  const evidenceRoot = path.resolve(values["evidence-root"]);
  const usageRoot = path.resolve(values["usage-root"]);
  const claim = claimInvocation(privateRoot, phase);
  const skillPath = installSkill(workspaceRoot, path.resolve(values["skill-source"]));
  const controlled = controlledEnvironment(ambient);
  const auth = readCodexLunaExternalAuth(path.resolve(values["auth-source"]), ambient);
  const brokerToken = controlled.brokerKeys.length === 2 ? controlled.environment.PROBLEM_LOCATOR_LOGPARSE_TOKEN : null;
  const runtimeAuth = {
    ...auth,
    canaries: [...auth.canaries, ...(brokerToken ? [brokerToken] : [])],
    redact_canaries: [...auth.redact_canaries],
  };
  const prefix = phase.toLowerCase();
  const traceRoot = path.join(evidenceRoot, "server-invocations");
  const startedAtUtc = new Date().toISOString();
  const trace = await runCodexLunaAppServerCall({
    codexEntry: path.resolve(values["codex-entry"]),
    auth: runtimeAuth,
    environment: controlled.environment,
    workspaceRoot,
    skillPath,
    mode: "service",
    prompt,
    outputSchema: null,
    callRoot: path.join(claim, "app-server"),
    privateRoot,
    tracePath: path.join(traceRoot, `${prefix}.jsonl`),
    stderrPath: path.join(traceRoot, `${prefix}.stderr.txt`),
    finalPath: path.join(traceRoot, `${prefix}.final.txt`),
    forbiddenReadPaths: [path.resolve(values["auth-source"]), path.resolve(values["skill-source"])],
    wallSeconds: MACOS_CODEX_LUNA_CALL_WALL_SECONDS,
    noProgressSeconds: MACOS_CODEX_LUNA_NO_PROGRESS_SECONDS,
    onProgress: () => stdout.write(`TEST_FLOW_PROGRESS service-agent ${phase}\n`),
  });
  const finishedAtUtc = new Date().toISOString();
  const invocationId = `${values["run-id"]}:server:${prefix}`;
  const receipt = {
    schema_version: 1,
    invocation_id: invocationId,
    phase,
    model: CODEX_LUNA_MODEL,
    reasoning_effort: CODEX_LUNA_REASONING_EFFORT,
    attempt: 1,
    retry: 0,
    status: "PASS",
    terminal: true,
    started_at_utc: startedAtUtc,
    finished_at_utc: finishedAtUtc,
    wall_timeout_seconds: MACOS_CODEX_LUNA_CALL_WALL_SECONDS,
    thread_id: trace.thread_id,
    turn_id: trace.turn_id,
    usage: trace.usage,
    command_count: trace.command_receipts.length,
    command_receipts: trace.command_receipts,
    broker_environment: { present: controlled.brokerKeys.length === 2, keys: controlled.brokerKeys, values_persisted: false },
    workspace_sha256: sha256Bytes(workspaceRoot),
  };
  writeJsonExclusive(path.join(usageRoot, `${prefix}.json`), receipt);
  writeJsonExclusive(path.join(traceRoot, `${prefix}.receipt.json`), receipt);
  stdout.write(`${trace.final_text}\n`);
  return receipt;
}

async function main() {
  try {
    const values = parseArguments(process.argv.slice(2));
    await runServiceInvocation(values);
  } catch (error) {
    process.stderr.write(`${canonicalJson({ schema_version: 1, status: "FAIL", code: error?.code ?? "MACOS_CODEX_LUNA_SERVICE_WRAPPER_FAILED", message: error?.message ?? String(error) })}\n`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === MODULE_PATH) await main();

export { controlledEnvironment, parseArguments };
