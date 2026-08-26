#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import {
  auditNoSecretLeak,
  canonicalJson,
  CODEX_LUNA_MODEL,
  CODEX_LUNA_REASONING_EFFORT,
  sha256Bytes,
} from "../../../runtime-support/codex-luna-contract.mjs";
import {
  readCodexLunaExternalAuth,
  runCodexLunaAppServerCall,
} from "../../../runtime-support/codex-luna-app-server-runtime.mjs";
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
const LINUX_SERVICE_PROJECT_DIRECTORY = "test-flow-codex-project";
const SERVICE_DRAFT_BY_PHASE = Object.freeze({
  ROUTE: "output/job_outcome.draft.json",
  DIAGNOSE: "output/method-diagnosis.draft.json",
  REVIEW: "output/method-review.draft.json",
});
const MAX_SERVICE_DRAFT_BYTES = 2_000_000;
export function serviceDeveloperInstructions(workspaceRoot) {
  if (typeof workspaceRoot !== "string" || !path.isAbsolute(workspaceRoot) || /[\0\r\n]/u.test(workspaceRoot)) fail("MACOS_CODEX_LUNA_SERVICE_WORKSPACE_INVALID", "Service developer instructions require one absolute workspace");
  const root = path.resolve(workspaceRoot);
  return `Standalone Fast E2E 硬约束：唯一允许的命令工作目录是 ${JSON.stringify(root)}，每条 commandExecution.cwd 都必须逐字等于该路径；禁止 cd、chdir 或切到其他目录。附加的 Skill 绝对路径仅用于 app-server 配置，绝不能作为命令 cwd。写 diagnosis、review 或 outcome draft 前，必须先在上述 workspace 读取 inputs/manifest.json；所有结论、evidence ref、Case/Job/Outcome 身份都只能逐字绑定 manifest 与冻结输入，不能凭记忆补写或省略来源。每条 evidence 的 line_number 必须在其 source_id 对应的单个文件内从 1 计数，并逐字复制该文件整行，禁止使用跨文件或全局行号。遇到 LATE_RESPONSE 时，若存在相同 request_id 的 timeout 记录，必须优先使用该精确预算，禁止改用旁边的通用 timeout limit；必须枚举所有已加载 method 与 marker occurrence 后再结束诊断。`;
}

class ServiceWrapperError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "ServiceWrapperError";
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) {
  throw new ServiceWrapperError(code, message, details);
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
  const required = ["codex-entry", "auth-source", "skill-source", "finalizer-entry", "logparse-entry", "expected-cli-version", "private-root", "evidence-root", "usage-root", "run-id"];
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

function copyPlainTree(sourceRoot, destinationRoot, { skippedRoot = null } = {}) {
  const source = path.resolve(sourceRoot);
  if (skippedRoot !== null && source === path.resolve(skippedRoot)) return;
  let metadata;
  try { metadata = fs.lstatSync(source); } catch { fail("MACOS_CODEX_LUNA_SERVICE_PROJECT_INPUT_MISSING", "Service project input is unavailable"); }
  if (!metadata.isDirectory() || metadata.isSymbolicLink()) fail("MACOS_CODEX_LUNA_SERVICE_PROJECT_INPUT_INVALID", "Service project input must be a plain directory");
  if (fs.existsSync(destinationRoot)) fail("MACOS_CODEX_LUNA_SERVICE_PROJECT_DESTINATION_EXISTS", "Service project destination already exists");
  fs.mkdirSync(destinationRoot, { mode: 0o700 });
  for (const entry of fs.readdirSync(source, { withFileTypes: true }).sort((left, right) => left.name.localeCompare(right.name))) {
    const sourcePath = path.join(source, entry.name);
    if (skippedRoot !== null && path.resolve(sourcePath) === path.resolve(skippedRoot)) continue;
    const destinationPath = path.join(destinationRoot, entry.name);
    const entryMetadata = fs.lstatSync(sourcePath);
    if (entryMetadata.isSymbolicLink()) fail("MACOS_CODEX_LUNA_SERVICE_PROJECT_INPUT_INVALID", "Service project input contains a symbolic link");
    if (entryMetadata.isDirectory()) {
      copyPlainTree(sourcePath, destinationPath, { skippedRoot });
      continue;
    }
    if (!entryMetadata.isFile()) fail("MACOS_CODEX_LUNA_SERVICE_PROJECT_INPUT_INVALID", "Service project input contains a non-ordinary file");
    fs.copyFileSync(sourcePath, destinationPath, fs.constants.COPYFILE_EXCL);
    fs.chmodSync(destinationPath, entryMetadata.mode & 0o777);
  }
  fs.chmodSync(destinationRoot, metadata.mode & 0o777);
}

export function stageLinuxServiceProject(workspaceRoot) {
  const root = path.resolve(workspaceRoot);
  const runtimeRoot = path.join(root, "runtime");
  const projectRoot = path.join(runtimeRoot, LINUX_SERVICE_PROJECT_DIRECTORY);
  if (fs.existsSync(projectRoot)) fail("MACOS_CODEX_LUNA_SERVICE_PROJECT_EXISTS", "Linux service project workspace already exists");
  fs.mkdirSync(projectRoot, { mode: 0o700 });
  try {
    copyPlainTree(path.join(root, "inputs"), path.join(projectRoot, "inputs"));
    copyPlainTree(path.join(root, "output"), path.join(projectRoot, "output"));
    copyPlainTree(runtimeRoot, path.join(projectRoot, "runtime"), { skippedRoot: projectRoot });
  } catch (error) {
    fs.rmSync(projectRoot, { recursive: true, force: true });
    throw error;
  }
  return projectRoot;
}

export function publishLinuxServiceDraft({ phase, workspaceRoot, projectRoot }) {
  const relativePath = SERVICE_DRAFT_BY_PHASE[phase] ?? null;
  if (relativePath === null) return { required: false, published: false, status: "SKIP" };
  const source = path.join(projectRoot, ...relativePath.split("/"));
  const destination = path.join(workspaceRoot, ...relativePath.split("/"));
  let metadata;
  try { metadata = fs.lstatSync(source); } catch { fail("MACOS_CODEX_LUNA_SERVICE_DRAFT_MISSING", "Service project draft is missing"); }
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.nlink !== 1 || metadata.size > MAX_SERVICE_DRAFT_BYTES) fail("MACOS_CODEX_LUNA_SERVICE_DRAFT_INVALID", "Service project draft is not a bounded ordinary file");
  if (fs.existsSync(destination)) fail("MACOS_CODEX_LUNA_SERVICE_DRAFT_EXISTS", "Product Workspace draft already exists");
  fs.copyFileSync(source, destination, fs.constants.COPYFILE_EXCL);
  fs.chmodSync(destination, 0o600);
  return {
    required: true,
    published: true,
    status: "PASS",
    relative_path: relativePath,
    sha256: sha256Bytes(fs.readFileSync(destination)),
    size: metadata.size,
  };
}

export function removeLinuxServiceProject({ workspaceRoot, projectRoot }) {
  const expected = path.join(path.resolve(workspaceRoot), "runtime", LINUX_SERVICE_PROJECT_DIRECTORY);
  if (path.resolve(projectRoot) !== expected) fail("MACOS_CODEX_LUNA_SERVICE_PROJECT_PATH_INVALID", "Linux service project path is outside the fixed runtime location");
  fs.rmSync(expected, { recursive: true, force: true });
}

export function safeServiceError(error) {
  const safeDetails = {};
  for (const key of ["id", "response_code", "response_message", "codex_error_info", "http_status_code", "will_retry", "method", "field", "item_type", "function_name", "line", "cwd_matches", "errors_count", "skills_errors"]) {
    const value = error?.details?.[key];
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean" || value === null) safeDetails[key] = value;
  }
  return {
    schema_version: 1,
    status: "FAIL",
    code: error?.code ?? "MACOS_CODEX_LUNA_SERVICE_WRAPPER_FAILED",
    message: error?.message ?? String(error),
    ...(Object.keys(safeDetails).length > 0 ? { details: safeDetails } : {}),
  };
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

export function repositorySkillPaths(sourceRoot) {
  const skillsRoot = path.join(path.resolve(sourceRoot), ".agents", "skills");
  if (!fs.existsSync(skillsRoot)) return [];
  return fs.readdirSync(skillsRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => path.join(skillsRoot, entry.name, "SKILL.md"))
    .filter((candidate) => {
      try { return fs.statSync(candidate).isFile(); } catch { return false; }
    })
    .sort();
}

export function sealServiceOutcomeDraft({ phase, workspaceRoot, sourceRoot = null, finalizerEntry = null }) {
  if (phase !== "ROUTE") return { required: false, invoked: false, status: "SKIP" };
  const finalizer = finalizerEntry === null
    ? path.join(path.resolve(sourceRoot), ".venv", "bin", "problem-locator-seal-outcome-draft")
    : path.resolve(finalizerEntry);
  let metadata;
  try { metadata = fs.statSync(finalizer); } catch { fail("MACOS_CODEX_LUNA_SERVICE_FINALIZER_MISSING", "Repository outcome finalizer command is missing"); }
  if (!metadata.isFile() || (metadata.mode & 0o111) === 0) fail("MACOS_CODEX_LUNA_SERVICE_FINALIZER_INVALID", "Repository outcome finalizer command is not executable");
  const marker = path.join(workspaceRoot, "runtime", "tool-state", "agent-job-outcome-draft.finalized");
  if (fs.existsSync(marker)) return { required: true, invoked: false, status: "PASS", marker_sha256: sha256Bytes(fs.readFileSync(marker)) };
  const draft = path.join(workspaceRoot, "output", "job_outcome.draft.json");
  let draftMetadata;
  try { draftMetadata = fs.lstatSync(draft); } catch { fail("MACOS_CODEX_LUNA_SERVICE_DRAFT_MISSING", "ROUTE Agent draft is missing"); }
  if (!draftMetadata.isFile() || draftMetadata.isSymbolicLink() || draftMetadata.nlink !== 1 || draftMetadata.size > MAX_SERVICE_DRAFT_BYTES) {
    fail("MACOS_CODEX_LUNA_SERVICE_DRAFT_INVALID", "ROUTE Agent draft is not a bounded ordinary file");
  }
  const result = spawnSync(finalizer, [], {
    cwd: workspaceRoot,
    env: {
      PATH: `${path.dirname(finalizer)}:/usr/bin:/bin:/usr/sbin:/sbin`,
      LANG: "C.UTF-8",
      PYTHONDONTWRITEBYTECODE: "1",
      PYTHONNOUSERSITE: "1",
    },
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 30_000,
  });
  if (result.status === 2 && result.signal === null && !result.error && !fs.existsSync(marker)) {
    fail("MACOS_CODEX_LUNA_SERVICE_DRAFT_REJECTED", "Product finalizer rejected the ROUTE Agent draft", { field: "output/job_outcome.draft.json" });
  }
  if (result.status !== 0 || result.signal !== null || result.error || !fs.existsSync(marker)) fail("MACOS_CODEX_LUNA_SERVICE_FINALIZER_FAILED", "Repository outcome finalizer command failed");
  return { required: true, invoked: true, status: "PASS", marker_sha256: sha256Bytes(fs.readFileSync(marker)) };
}

export function persistRejectedServiceDraft({ phase, workspaceRoot, evidenceRoot, canaries }) {
  if (phase !== "ROUTE") return { required: false, persisted: false, status: "SKIP" };
  const relativePath = "output/job_outcome.draft.json";
  const source = path.join(workspaceRoot, "output", "job_outcome.draft.json");
  let metadata;
  try { metadata = fs.lstatSync(source); } catch { fail("MACOS_CODEX_LUNA_SERVICE_DRAFT_MISSING", "Rejected ROUTE draft is unavailable"); }
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.nlink !== 1 || metadata.size > MAX_SERVICE_DRAFT_BYTES) {
    fail("MACOS_CODEX_LUNA_SERVICE_DRAFT_INVALID", "Rejected ROUTE draft is not a bounded ordinary file");
  }
  const secretScan = auditNoSecretLeak({ roots: [source], canaries });
  const destinationRoot = path.join(evidenceRoot, "server-invocations");
  fs.mkdirSync(destinationRoot, { recursive: true, mode: 0o700 });
  const destination = path.join(destinationRoot, "route.rejected-draft.json");
  fs.copyFileSync(source, destination, fs.constants.COPYFILE_EXCL);
  fs.chmodSync(destination, 0o600);
  writeJsonExclusive(path.join(destinationRoot, "route.rejected-draft-secret-scan.json"), secretScan);
  return { required: true, persisted: true, status: "PASS", relative_path: relativePath, size: metadata.size, sha256: sha256Bytes(fs.readFileSync(source)) };
}

export function runServiceLogparseCommand({ phase, prompt, workspaceRoot, sourceRoot = null, logparseEntry = null, environment }) {
  if (phase !== "LOGPARSE") return { required: false, invoked: false, status: "SKIP" };
  const match = prompt.match(/^problem-locator-logparse (parse-targets|target-logs) --request ([a-zA-Z0-9._/-]+\.json) --result ([a-zA-Z0-9._/-]+\.json)$/m);
  if (!match || [match[2], match[3]].some((value) => value.startsWith("/") || value.split("/").includes("..") || !value.startsWith("output/proposals/"))) fail("MACOS_CODEX_LUNA_SERVICE_LOGPARSE_COMMAND_INVALID", "Product Logparse prompt does not contain one safe fixed command");
  const [, operation, requestPath, resultPath] = match;
  const resultFile = path.join(workspaceRoot, ...resultPath.split("/"));
  if (fs.existsSync(resultFile)) return { required: true, invoked: false, status: "PASS", operation, request_path_sha256: sha256Bytes(requestPath), result_path_sha256: sha256Bytes(resultPath) };
  const command = logparseEntry === null
    ? path.join(path.resolve(sourceRoot), ".venv", "bin", "problem-locator-logparse")
    : path.resolve(logparseEntry);
  let metadata;
  try { metadata = fs.statSync(command); } catch { fail("MACOS_CODEX_LUNA_SERVICE_LOGPARSE_MISSING", "Repository Logparse command is missing"); }
  if (!metadata.isFile() || (metadata.mode & 0o111) === 0) fail("MACOS_CODEX_LUNA_SERVICE_LOGPARSE_INVALID", "Repository Logparse command is not executable");
  const result = spawnSync(command, [operation, "--request", requestPath, "--result", resultPath], {
    cwd: workspaceRoot,
    env: {
      PATH: `${path.dirname(command)}:/usr/bin:/bin:/usr/sbin:/sbin`,
      LANG: "C.UTF-8",
      PYTHONDONTWRITEBYTECODE: "1",
      PYTHONNOUSERSITE: "1",
      PROBLEM_LOCATOR_LOGPARSE_ENDPOINT: environment.PROBLEM_LOCATOR_LOGPARSE_ENDPOINT,
      PROBLEM_LOCATOR_LOGPARSE_TOKEN: environment.PROBLEM_LOCATOR_LOGPARSE_TOKEN,
    },
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 120_000,
  });
  if (result.status !== 0 || result.signal !== null || result.error || !fs.existsSync(resultFile)) fail("MACOS_CODEX_LUNA_SERVICE_LOGPARSE_FAILED", "Repository Logparse command failed");
  return { required: true, invoked: true, status: "PASS", operation, request_path_sha256: sha256Bytes(requestPath), result_path_sha256: sha256Bytes(resultPath) };
}

export function canonicalizeMethodsDraft({ phase, workspaceRoot }) {
  const relativePath = phase === "DIAGNOSE"
    ? "output/method-diagnosis.draft.json"
    : phase === "REVIEW"
      ? "output/method-review.draft.json"
      : null;
  if (relativePath === null) return { required: false, invoked: false, status: "SKIP" };
  const draftPath = path.join(workspaceRoot, ...relativePath.split("/"));
  let metadata;
  try { metadata = fs.lstatSync(draftPath); } catch { fail("MACOS_CODEX_LUNA_METHODS_DRAFT_MISSING", "Methods draft is missing"); }
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size > 2_000_000) fail("MACOS_CODEX_LUNA_METHODS_DRAFT_INVALID", "Methods draft is not a bounded ordinary file");
  let value;
  try { value = JSON.parse(fs.readFileSync(draftPath, "utf8")); } catch { fail("MACOS_CODEX_LUNA_METHODS_DRAFT_INVALID", "Methods draft is not valid JSON"); }
  if (value === null || typeof value !== "object" || Array.isArray(value)) fail("MACOS_CODEX_LUNA_METHODS_DRAFT_INVALID", "Methods draft must be one JSON object");
  const before = sha256Bytes(fs.readFileSync(draftPath));
  fs.writeFileSync(draftPath, `${canonicalJson(value)}\n`, { encoding: "utf8", mode: 0o600 });
  return { required: true, invoked: true, status: "PASS", relative_path: relativePath, before_sha256: before, after_sha256: sha256Bytes(fs.readFileSync(draftPath)) };
}

export async function runServiceInvocation(values, { stdin = process.stdin, stdout = process.stdout, ambient = process.env } = {}) {
  const workspaceRoot = process.cwd();
  const prompt = await readPrompt(stdin);
  const phase = serverInvocationPhase(prompt, workspaceRoot);
  const privateRoot = path.resolve(values["private-root"]);
  const evidenceRoot = path.resolve(values["evidence-root"]);
  const usageRoot = path.resolve(values["usage-root"]);
  const claim = claimInvocation(privateRoot, phase);
  const skillPath = path.resolve(values["skill-source"]);
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
  const isolatedProject = process.platform === "linux";
  const projectRoot = isolatedProject
    ? stageLinuxServiceProject(workspaceRoot)
    : workspaceRoot;
  let trace;
  let draftPublication = { required: false, published: false, status: "SKIP" };
  try {
    trace = await runCodexLunaAppServerCall({
      codexEntry: path.resolve(values["codex-entry"]),
      auth: runtimeAuth,
      environment: controlled.environment,
      workspaceRoot: projectRoot,
      skillPath,
      mode: "service",
      developerInstructions: serviceDeveloperInstructions(projectRoot),
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
      shellHome: path.join(claim, "shell-home"),
      disabledSkillPaths: repositorySkillPaths(ambient.TEST_FLOW_SOURCE_ROOT),
      expectedCliVersion: values["expected-cli-version"],
      onProgress: () => stdout.write(`TEST_FLOW_PROGRESS service-agent ${phase}\n`),
    });
    if (isolatedProject) {
      draftPublication = publishLinuxServiceDraft({ phase, workspaceRoot, projectRoot });
    }
  } finally {
    if (isolatedProject) removeLinuxServiceProject({ workspaceRoot, projectRoot });
  }
  const methodsDraft = canonicalizeMethodsDraft({ phase, workspaceRoot });
  const logparseRunner = runServiceLogparseCommand({ phase, prompt, workspaceRoot, logparseEntry: values["logparse-entry"], environment: controlled.environment });
  let outcomeSealer;
  try {
    outcomeSealer = sealServiceOutcomeDraft({ phase, workspaceRoot, finalizerEntry: values["finalizer-entry"] });
  } catch (error) {
    if (error?.code === "MACOS_CODEX_LUNA_SERVICE_DRAFT_REJECTED") {
      persistRejectedServiceDraft({ phase, workspaceRoot, evidenceRoot, canaries: runtimeAuth.canaries });
    }
    throw error;
  }
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
    service_project: {
      isolated: isolatedProject,
      product_workspace_sha256: sha256Bytes(workspaceRoot),
      project_workspace_sha256: sha256Bytes(projectRoot),
      draft_publication: draftPublication,
    },
    methods_draft: methodsDraft,
    logparse_runner: logparseRunner,
    outcome_sealer: outcomeSealer,
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
    process.stderr.write(`${canonicalJson(safeServiceError(error))}\n`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === MODULE_PATH) await main();

export { controlledEnvironment, parseArguments };
