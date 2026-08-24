#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { codexLogparseRuntimeIdentity } from "../lib/release-inputs.mjs";

import {
  auditDiagnosisCommands,
  auditGenerationCommands,
  buildCodexLunaSourceWikiIdentity,
  buildPosthocBudgetReceipt,
  canonicalJson,
  CODEX_LUNA_CALL_WALL_SECONDS,
  CODEX_LUNA_CONTRACT_VERSION,
  CODEX_LUNA_EQUIVALENT_USD_LIMIT,
  CODEX_LUNA_EXPECTED_CLI_SHA256,
  CODEX_LUNA_EXPECTED_CLI_VERSION,
  CODEX_LUNA_MAX_CALLS,
  CODEX_LUNA_MODEL,
  CODEX_LUNA_NO_PROGRESS_SECONDS,
  CODEX_LUNA_NORMAL_CALLS,
  CODEX_LUNA_PERMISSION_PROFILE_VERSION,
  CODEX_LUNA_POSTHOC_EXCEPTION_ID,
  CODEX_LUNA_REASONING_EFFORT,
  CODEX_LUNA_SCENARIO_COUNT,
  CODEX_LUNA_STAGE_WALL_SECONDS,
  CODEX_LUNA_TOKEN_LIMIT,
  normalizeCodexUsage,
  ordinaryDirectory,
  ordinaryFile,
  sha256Bytes,
  sha256File,
  treeDigest,
  treeManifest,
  validateCodexLunaSourceWikiIdentity,
  validateCodexLunaIdentity,
  verifyMethodsV1Package,
} from "./codex-luna-contract.mjs";
import {
  auditCodexLunaRuntimeSecrets,
  generateCodexLunaProtocolSchemaReceipt,
  readCodexLunaExternalAuth,
  runCodexLunaAppServerCall,
} from "./codex-luna-app-server-runtime.mjs";

const MODULE_ROOT = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_DIAGNOSIS_SCHEMA = path.join(MODULE_ROOT, "codex-luna-diagnosis.schema.json");
const GENERATED_SKILL_NAME = "diagnose-rpc-timeout";
const META_SKILL_NAME = "wiki-to-diagnosis-skill";
const INVOCATION_CLASS = "codex-luna-agent";
const RESULT_SCHEMA_VERSION = 1;
const USAGE_RECEIPT_SCHEMA_VERSION = 1;
const LEDGER_SCHEMA_VERSION = 1;

const CASE_REQUIRED_KEYS = Object.freeze([
  "scenario_id",
  "problem_time",
  "client_process",
  "server_process",
  "service",
  "api",
  "expected_status",
  "expected_branch_markers",
  "expected_terms",
  "expected_evidence_identities",
  "forbidden_evidence_terms",
]);

const RESULT_KEYS = Object.freeze([
  "schema_version",
  "scenario_id",
  "status",
  "confirmed_methods",
  "candidate_methods",
  "evidence",
  "limitations",
  "safety_notes",
  "logparse_receipt_sha256",
]);

const EVIDENCE_KEYS = Object.freeze(["method_id", "summary", "identity_tokens", "sources"]);
const SOURCE_KEYS = Object.freeze(["source_id", "line_number", "marker", "line"]);

class CodexLunaRunnerError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "CodexLunaRunnerError";
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) {
  throw new CodexLunaRunnerError(code, message, details);
}

function requireRunner(condition, code, message, details = {}) {
  if (!condition) fail(code, message, details);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function exactKeys(value, expected) {
  return isPlainObject(value) && Object.keys(value).sort().join("\0") === [...expected].sort().join("\0");
}

function readJson(filePath, label) {
  ordinaryFile(filePath, label);
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (error) {
    fail("CODEX_LUNA_JSON_INVALID", `${label} is not valid JSON`, { path: filePath, cause: error.message });
  }
}

function writeJson(filePath, value, { exclusive = false } = {}) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  const payload = `${canonicalJson(value)}\n`;
  if (exclusive) {
    fs.writeFileSync(filePath, payload, { encoding: "utf8", mode: 0o600, flag: "wx" });
    return;
  }
  const temporary = `${filePath}.tmp-${process.pid}`;
  fs.writeFileSync(temporary, payload, { encoding: "utf8", mode: 0o600, flag: "wx" });
  fs.renameSync(temporary, filePath);
}

function createEmptyRoot(root, label) {
  const resolved = path.resolve(root);
  if (fs.existsSync(resolved)) {
    ordinaryDirectory(resolved, label);
    requireRunner(fs.readdirSync(resolved).length === 0, "CODEX_LUNA_ROOT_NOT_EMPTY", `${label} must be empty`, { path: resolved });
  } else {
    fs.mkdirSync(resolved, { recursive: true, mode: 0o700 });
  }
  return resolved;
}

function pathInside(root, candidate) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function assertDisjointRoots(roots) {
  const entries = Object.entries(roots).map(([label, value]) => [label, path.resolve(value)]);
  for (let left = 0; left < entries.length; left += 1) {
    for (let right = left + 1; right < entries.length; right += 1) {
      const [leftLabel, leftRoot] = entries[left];
      const [rightLabel, rightRoot] = entries[right];
      requireRunner(!pathInside(leftRoot, rightRoot) && !pathInside(rightRoot, leftRoot), "CODEX_LUNA_ROOTS_OVERLAP", "Work, private, evidence, and usage roots must be disjoint", { left: leftLabel, right: rightLabel });
    }
  }
}

function copyOrdinaryTree(source, destination) {
  ordinaryDirectory(source, "copy source");
  requireRunner(!fs.existsSync(destination), "CODEX_LUNA_COPY_DESTINATION_EXISTS", "Copy destination already exists", { destination });
  fs.mkdirSync(destination, { recursive: true, mode: 0o700 });
  const visit = (from, to) => {
    for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
      if (["__pycache__", ".DS_Store"].includes(entry.name) || entry.name.endsWith(".pyc")) continue;
      const sourcePath = path.join(from, entry.name);
      const destinationPath = path.join(to, entry.name);
      const metadata = fs.lstatSync(sourcePath);
      requireRunner(!metadata.isSymbolicLink(), "CODEX_LUNA_COPY_SYMLINK", "Isolated inputs cannot contain symlinks", { path: sourcePath });
      if (entry.isDirectory()) {
        fs.mkdirSync(destinationPath, { mode: 0o700 });
        visit(sourcePath, destinationPath);
      } else {
        requireRunner(entry.isFile() && metadata.nlink === 1, "CODEX_LUNA_COPY_NODE_INVALID", "Isolated inputs must contain only ordinary files and directories", { path: sourcePath });
        fs.copyFileSync(sourcePath, destinationPath, fs.constants.COPYFILE_EXCL);
        fs.chmodSync(destinationPath, metadata.mode & 0o777);
      }
    }
  };
  visit(source, destination);
}

function safeEnvironment(ambient, { codexHome, home, temporary }) {
  const isolatedTemporary = temporary ?? path.join(home, "tmp");
  const allowed = [
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SHELL",
    "USER",
    "LOGNAME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
  ];
  const environment = {};
  for (const key of allowed) {
    if (typeof ambient[key] === "string" && ambient[key].length > 0) environment[key] = ambient[key];
  }
  environment.LANG = "C.UTF-8";
  environment.HOME = home;
  environment.CODEX_HOME = codexHome;
  environment.PATH = "/usr/bin:/bin:/usr/sbin:/sbin";
  environment.TMPDIR = isolatedTemporary;
  environment.TMP = isolatedTemporary;
  environment.TEMP = isolatedTemporary;
  environment.NO_COLOR = "1";
  environment.PYTHONDONTWRITEBYTECODE = "1";
  environment.PYTHONNOUSERSITE = "1";
  environment.PYTHONUTF8 = "1";
  return environment;
}

function environmentAudit(ambient, childEnvironment) {
  const sensitiveInbound = Object.keys(ambient).filter((key) => /(?:TOKEN|KEY|SECRET|PASSWORD|AUTH|CREDENTIAL)/i.test(key)).sort();
  const sensitiveForwarded = Object.keys(childEnvironment).filter((key) => /(?:TOKEN|KEY|SECRET|PASSWORD|AUTH|CREDENTIAL)/i.test(key)).sort();
  requireRunner(sensitiveForwarded.length === 0, "CODEX_LUNA_ENV_SECRET_FORWARDED", "Sensitive ambient variables cannot be forwarded to Codex", { keys: sensitiveForwarded });
  return {
    schema_version: 1,
    policy: "explicit-safe-environment-v1",
    inherited_keys: Object.keys(childEnvironment).filter((key) => !["HOME", "CODEX_HOME"].includes(key)).sort(),
    stripped_sensitive_key_names: sensitiveInbound,
    sensitive_values_forwarded: 0,
    home_isolated: true,
    codex_home_isolated: true,
    user_config_ignored: true,
    user_rules_ignored: true,
  };
}

async function captureProcess(executable, argumentsList, { cwd, environment, wallSeconds = 120, noProgressSeconds = 60, stdoutPath = null, stderrPath = null, onProgress = null }) {
  if (stdoutPath) fs.mkdirSync(path.dirname(stdoutPath), { recursive: true, mode: 0o700 });
  if (stderrPath) fs.mkdirSync(path.dirname(stderrPath), { recursive: true, mode: 0o700 });
  const stdoutChunks = [];
  const stderrChunks = [];
  const stdout = stdoutPath ? fs.createWriteStream(stdoutPath, { flags: "wx", mode: 0o600 }) : null;
  const stderr = stderrPath ? fs.createWriteStream(stderrPath, { flags: "wx", mode: 0o600 }) : null;
  const child = spawn(executable, argumentsList, {
    cwd,
    env: environment,
    stdio: ["ignore", "pipe", "pipe"],
    detached: process.platform !== "win32",
  });
  let timedOut = false;
  let noProgressTimedOut = false;
  let noProgressTimer;
  const terminate = () => {
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
  };
  const armNoProgress = () => {
    clearTimeout(noProgressTimer);
    noProgressTimer = setTimeout(() => {
      noProgressTimedOut = true;
      terminate();
    }, noProgressSeconds * 1_000);
  };
  const wallTimer = setTimeout(() => {
    timedOut = true;
    terminate();
  }, wallSeconds * 1_000);
  armNoProgress();
  child.stdout.on("data", (chunk) => {
    stdoutChunks.push(chunk);
    stdout?.write(chunk);
    armNoProgress();
    onProgress?.("stdout", chunk.length);
  });
  child.stderr.on("data", (chunk) => {
    stderrChunks.push(chunk);
    stderr?.write(chunk);
    armNoProgress();
    onProgress?.("stderr", chunk.length);
  });
  const outcome = await new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    child.once("error", (error) => finish({ code: null, signal: null, spawn_error: error?.code ?? "UNKNOWN" }));
    child.once("close", (code, signal) => finish({ code, signal, spawn_error: null }));
  });
  clearTimeout(wallTimer);
  clearTimeout(noProgressTimer);
  await Promise.all([
    stdout ? new Promise((resolve) => stdout.end(resolve)) : Promise.resolve(),
    stderr ? new Promise((resolve) => stderr.end(resolve)) : Promise.resolve(),
  ]);
  return {
    ...outcome,
    timed_out: timedOut,
    no_progress_timed_out: noProgressTimedOut,
    stdout: Buffer.concat(stdoutChunks).toString("utf8"),
    stderr: Buffer.concat(stderrChunks).toString("utf8"),
  };
}

async function probeCodexIdentity(codexEntry, environment) {
  const metadata = ordinaryFile(codexEntry, "Codex executable");
  requireRunner((metadata.mode & 0o111) !== 0, "CODEX_LUNA_CLI_NOT_EXECUTABLE", "Codex entry is not executable", { path: codexEntry });
  const digest = sha256File(codexEntry);
  requireRunner(digest === CODEX_LUNA_EXPECTED_CLI_SHA256, "CODEX_LUNA_CLI_SHA256_MISMATCH", "Codex executable does not match the frozen local identity", { expected: CODEX_LUNA_EXPECTED_CLI_SHA256, actual: digest });
  const probe = await captureProcess(codexEntry, ["--version"], { cwd: path.dirname(codexEntry), environment, wallSeconds: 30, noProgressSeconds: 15 });
  requireRunner(probe.code === 0 && probe.signal === null && !probe.timed_out && !probe.no_progress_timed_out, "CODEX_LUNA_CLI_VERSION_PROBE_FAILED", "Codex version probe failed", { code: probe.code, signal: probe.signal });
  const version = probe.stdout.split(/\r?\n/).map((line) => line.trim()).find((line) => line.startsWith("codex-cli "));
  requireRunner(version === CODEX_LUNA_EXPECTED_CLI_VERSION, "CODEX_LUNA_CLI_VERSION_MISMATCH", "Codex CLI version does not match the frozen identity", { expected: CODEX_LUNA_EXPECTED_CLI_VERSION, actual: version ?? null });
  return {
    schema_version: 1,
    version,
    sha256: digest,
    size: metadata.size,
    platform: process.platform,
    architecture: process.arch,
    entry_path_sha256: sha256Bytes(path.resolve(codexEntry)),
    exact_match: true,
  };
}

function frontmatterName(skillRoot) {
  const document = fs.readFileSync(path.join(skillRoot, "SKILL.md"), "utf8");
  const match = /^---\r?\n([\s\S]*?)\r?\n---/.exec(document);
  requireRunner(match, "CODEX_LUNA_META_FRONTMATTER_INVALID", "Meta Skill must have YAML frontmatter");
  const nameLine = match[1].split(/\r?\n/).find((line) => line.startsWith("name:"));
  const name = nameLine?.slice("name:".length).trim().replace(/^['"]|['"]$/g, "");
  requireRunner(name === META_SKILL_NAME, "CODEX_LUNA_META_SKILL_NAME_INVALID", `Meta Skill must be promoted as ${META_SKILL_NAME}`, { actual: name ?? null });
  return name;
}

function loadCases(caseRoot) {
  ordinaryDirectory(caseRoot, "Codex scenario root");
  const descriptors = fs.readdirSync(caseRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => path.join(caseRoot, entry.name, "case.json"))
    .filter((descriptor) => fs.existsSync(descriptor))
    .sort();
  requireRunner(descriptors.length === CODEX_LUNA_SCENARIO_COUNT, "CODEX_LUNA_SCENARIO_COUNT_INVALID", `Codex exploration closure requires exactly ${CODEX_LUNA_SCENARIO_COUNT} scenarios`, { count: descriptors.length });
  const cases = descriptors.map((descriptor) => {
    const data = readJson(descriptor, "Codex scenario descriptor");
    requireRunner(isPlainObject(data) && CASE_REQUIRED_KEYS.every((key) => Object.hasOwn(data, key)), "CODEX_LUNA_CASE_CONTRACT_INVALID", "Codex scenario descriptor is missing a required field", { descriptor });
    const scenarioId = path.basename(path.dirname(descriptor));
    requireRunner(data.scenario_id === scenarioId && /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(scenarioId), "CODEX_LUNA_CASE_ID_INVALID", "Codex scenario ID must match its directory", { descriptor });
    for (const key of ["problem_time", "client_process", "server_process", "service", "api"]) requireRunner(typeof data[key] === "string" && data[key].trim().length > 0, "CODEX_LUNA_CASE_INPUT_INVALID", `Codex scenario ${key} must be a non-empty string`, { scenario_id: scenarioId });
    requireRunner(["CONFIRMED", "PARTIAL", "INSUFFICIENT"].includes(data.expected_status), "CODEX_LUNA_CASE_STATUS_INVALID", "Codex scenario expected status is invalid", { scenario_id: scenarioId });
    for (const key of ["expected_branch_markers", "expected_terms", "expected_evidence_identities", "forbidden_evidence_terms"]) requireRunner(Array.isArray(data[key]), "CODEX_LUNA_CASE_ORACLE_INVALID", `Codex scenario ${key} must be an array`, { scenario_id: scenarioId });
    return { root: path.dirname(descriptor), descriptor, data };
  });
  requireRunner(new Set(cases.map((item) => item.data.scenario_id)).size === CODEX_LUNA_SCENARIO_COUNT, "CODEX_LUNA_CASE_DUPLICATE", "Codex scenario IDs must be unique");
  return cases;
}

function validatePreprocessedCase(caseItem, preprocessedRoot) {
  const root = path.join(preprocessedRoot, caseItem.data.scenario_id);
  ordinaryDirectory(root, `preprocessed ${caseItem.data.scenario_id}`);
  const receiptPath = path.join(root, "receipt.json");
  const receipt = readJson(receiptPath, `Logparse receipt for ${caseItem.data.scenario_id}`);
  requireRunner(isPlainObject(receipt) && receipt.schema_version === 1 && receipt.status === "PASS" && receipt.scenario_id === caseItem.data.scenario_id, "CODEX_LUNA_LOGPARSE_RECEIPT_INVALID", "Preprocessed Logparse receipt identity is invalid", { scenario_id: caseItem.data.scenario_id });
  requireRunner(receipt.parse_invocations === 1 && receipt.target_query_invocations === 2 && receipt.logparse_processes_during_diagnosis === 0, "CODEX_LUNA_LOGPARSE_COUNT_INVALID", "Each scenario must be preprocessed exactly once and declare zero diagnosis-time Logparse calls", { scenario_id: caseItem.data.scenario_id });
  requireRunner(isPlainObject(receipt.archive) && typeof receipt.archive.name === "string" && path.basename(receipt.archive.name) === receipt.archive.name && /^[0-9a-f]{64}$/.test(receipt.archive.sha256), "CODEX_LUNA_ARCHIVE_RECEIPT_INVALID", "Preprocessed archive identity is invalid", { scenario_id: caseItem.data.scenario_id });
  const archivePath = path.join(root, receipt.archive.name);
  ordinaryFile(archivePath, `preprocessed archive for ${caseItem.data.scenario_id}`);
  requireRunner(sha256File(archivePath) === receipt.archive.sha256, "CODEX_LUNA_ARCHIVE_DRIFT", "Preprocessed archive differs from its receipt", { scenario_id: caseItem.data.scenario_id });
  requireRunner(Array.isArray(receipt.frozen_target_logs) && receipt.frozen_target_logs.length === 2, "CODEX_LUNA_FROZEN_LOGS_INVALID", "Each scenario must have exactly two frozen target logs", { scenario_id: caseItem.data.scenario_id });
  const sources = [];
  for (const item of receipt.frozen_target_logs) {
    requireRunner(isPlainObject(item) && ["client", "server"].includes(item.label) && typeof item.file === "string" && /^[0-9a-f]{64}$/.test(item.sha256) && item.match_status === "exact", "CODEX_LUNA_FROZEN_LOG_INVALID", "Frozen target log receipt is invalid", { scenario_id: caseItem.data.scenario_id });
    const expectedProcess = item.label === "client" ? caseItem.data.client_process : caseItem.data.server_process;
    requireRunner(item.process_name === expectedProcess, "CODEX_LUNA_FROZEN_PROCESS_MISMATCH", "Frozen target process differs from the scenario request", { scenario_id: caseItem.data.scenario_id, source_id: item.label });
    const sourcePath = path.resolve(root, ...item.file.split("/"));
    requireRunner(pathInside(root, sourcePath), "CODEX_LUNA_FROZEN_LOG_ESCAPE", "Frozen target log path escapes its preprocessing root", { scenario_id: caseItem.data.scenario_id });
    ordinaryFile(sourcePath, `frozen ${item.label} log`);
    requireRunner(sha256File(sourcePath) === item.sha256, "CODEX_LUNA_FROZEN_LOG_DRIFT", "Frozen target log differs from its receipt", { scenario_id: caseItem.data.scenario_id, source_id: item.label });
    sources.push({ ...item, source_path: sourcePath });
  }
  requireRunner(new Set(sources.map((item) => item.label)).size === sources.length, "CODEX_LUNA_FROZEN_SOURCE_DUPLICATE", "Frozen target source IDs must be unique", { scenario_id: caseItem.data.scenario_id });
  requireRunner(new Set(sources.map((item) => item.label)).size === 2, "CODEX_LUNA_FROZEN_SOURCE_SET_INVALID", "Frozen target sources must be exactly client and server", { scenario_id: caseItem.data.scenario_id });
  return { root, receipt, receipt_path: receiptPath, receipt_sha256: sha256File(receiptPath), sources };
}

function diagnosisRequest(caseItem, preprocessing) {
  const archive = preprocessing.receipt.archive;
  requireRunner(isPlainObject(archive) && typeof archive.name === "string" && /^[0-9a-f]{64}$/.test(archive.sha256), "CODEX_LUNA_ARCHIVE_RECEIPT_INVALID", "Logparse receipt archive identity is invalid", { scenario_id: caseItem.data.scenario_id });
  return {
    schema_version: 1,
    scenario_id: caseItem.data.scenario_id,
    problem_time: caseItem.data.problem_time,
    client_process: caseItem.data.client_process,
    server_process: caseItem.data.server_process,
    service: caseItem.data.service,
    api: caseItem.data.api,
    log_archive: { name: archive.name, sha256: archive.sha256, status: "consumed_by_logparse" },
  };
}

function boundDiagnosisSchema(schemaPath, { scenarioId, receiptSha256 }) {
  const schema = readJson(schemaPath, "Codex diagnosis output schema");
  requireRunner(isPlainObject(schema) && isPlainObject(schema.properties), "CODEX_LUNA_OUTPUT_SCHEMA_INVALID", "Codex diagnosis output schema is invalid");
  schema.properties.scenario_id.const = scenarioId;
  delete schema.properties.scenario_id.minLength;
  schema.properties.logparse_receipt_sha256.const = receiptSha256;
  delete schema.properties.logparse_receipt_sha256.pattern;
  return schema;
}

function createStandaloneGitBoundary(workspace) {
  const gitRoot = path.join(workspace, ".git");
  fs.mkdirSync(path.join(gitRoot, "objects"), { recursive: true, mode: 0o700 });
  fs.mkdirSync(path.join(gitRoot, "refs", "heads"), { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(gitRoot, "HEAD"), "ref: refs/heads/main\n", { flag: "wx", mode: 0o600 });
  fs.writeFileSync(path.join(gitRoot, "config"), "[core]\n\trepositoryformatversion = 0\n\tbare = false\n", { flag: "wx", mode: 0o600 });
}

function buildGenerationWorkspace({ attemptRoot, metaSkillRoot, wiki }) {
  fs.mkdirSync(attemptRoot, { recursive: true, mode: 0o700 });
  createStandaloneGitBoundary(attemptRoot);
  const installedMeta = path.join(attemptRoot, ".agents", "skills", META_SKILL_NAME);
  copyOrdinaryTree(metaSkillRoot, installedMeta);
  fs.mkdirSync(path.join(attemptRoot, "input"), { recursive: true, mode: 0o700 });
  fs.mkdirSync(path.join(attemptRoot, "runtime"), { recursive: true, mode: 0o700 });
  fs.mkdirSync(path.join(attemptRoot, "generated"), { recursive: true, mode: 0o700 });
  const wikiBytes = fs.readFileSync(wiki);
  fs.copyFileSync(wiki, path.join(attemptRoot, "input", "wiki.md"), fs.constants.COPYFILE_EXCL);
  const sourceWikiIdentity = buildCodexLunaSourceWikiIdentity(wikiBytes);
  const sourceWikiIdentityPath = path.join(attemptRoot, "runtime", "source-wiki-identity.json");
  writeJson(sourceWikiIdentityPath, sourceWikiIdentity, { exclusive: true });
  return { workspace: attemptRoot, installedMeta, sourceWikiIdentity, sourceWikiIdentityPath };
}

function buildDiagnosisWorkspace({ attemptRoot, generatedSkill, caseItem, preprocessing, schemaPath }) {
  fs.mkdirSync(attemptRoot, { recursive: true, mode: 0o700 });
  createStandaloneGitBoundary(attemptRoot);
  const installedSkill = path.join(attemptRoot, ".agents", "skills", GENERATED_SKILL_NAME);
  copyOrdinaryTree(generatedSkill, installedSkill);
  const inputRoot = path.join(attemptRoot, "input");
  const evidenceRoot = path.join(attemptRoot, "evidence");
  fs.mkdirSync(inputRoot, { recursive: true, mode: 0o700 });
  fs.mkdirSync(evidenceRoot, { recursive: true, mode: 0o700 });
  fs.copyFileSync(preprocessing.receipt_path, path.join(inputRoot, "logparse-receipt.json"), fs.constants.COPYFILE_EXCL);
  const targetLogs = [];
  for (const source of preprocessing.sources) {
    const destination = path.join(evidenceRoot, `${source.label}.log`);
    fs.copyFileSync(source.source_path, destination, fs.constants.COPYFILE_EXCL);
    targetLogs.push({
      source_id: source.label,
      process_name: source.process_name,
      match_status: source.match_status,
      log_path: `evidence/${source.label}.log`,
      sha256: source.sha256,
    });
  }
  writeJson(path.join(inputRoot, "target_logs.json"), { schema_version: 2, target_logs: targetLogs }, { exclusive: true });
  writeJson(path.join(inputRoot, "request.json"), diagnosisRequest(caseItem, preprocessing), { exclusive: true });
  const boundSchema = boundDiagnosisSchema(schemaPath, { scenarioId: caseItem.data.scenario_id, receiptSha256: preprocessing.receipt_sha256 });
  const outputSchemaPath = path.join(inputRoot, "diagnosis-result.schema.json");
  writeJson(outputSchemaPath, boundSchema, { exclusive: true });
  return { workspace: attemptRoot, installedSkill, outputSchema: boundSchema, outputSchemaPath };
}

function generationPrompt() {
  return `使用 $${META_SKILL_NAME}，把 input/wiki.md 转换成一个名为 ${GENERATED_SKILL_NAME} 的定位 Skill，并写入 generated/${GENERATED_SKILL_NAME}。

要求：
- 人工 Wiki 是唯一业务事实源，不得修改。
- 开始生成前，先完整读取 input/wiki.md 和 runtime/source-wiki-identity.json；identity 是 input/wiki.md 的闭合 schema-v2 投影，不得修改、重算或猜测其中任何值。
- 将 identity.sha256 原样写入 methods.json 的 source_wiki_sha256。
- identity.log_templates 是完整性清单：固定文件 references/source-log-templates.md 必须依次且仅包含标题行 # Source log templates、一个空行、起始 text 代码围栏、按数组顺序逐字写入且每项一行的全部模板、结束代码围栏和最终换行；不得重排、去重或添加其他内容。
- methods.json 的 shared_references[0] 必须是 references/source-log-templates.md；清单只用于完整性核对，不得作为 Wiki 以外的业务事实源。
- 只生成 methods-v1 输出合同允许的 SKILL.md、methods.json 和 references/*.md；不生成旧版 manifest、GenerationSpec、README 或测试框架。
- 完整保留 Wiki 声明的用户参数、日志附件和日志派生字段；不得把日志字段改成用户参数。
- 生成物消费 request.json、冻结的 target_logs 与 receipt，诊断时不能再次调用 Logparse。
- 检查全部正向证据；每个原因、每次独立事件分别输出 evidence，并保留来源整行、精确行号和同源 identity_tokens。
- Wiki 明确列出的原因决定方法边界；同一原因的不同日志是证据分支，不得另拆方法。
- 完成后执行元 Skill 自带的 validate_generated_skill.py；只有 PASS 才结束。`;
}

function diagnosisPrompt(caseItem, receiptSha256) {
  return `使用 $${GENERATED_SKILL_NAME} 定位 input/request.json 中的问题。

输入边界：
- Logparse 已完成；只读取 input/request.json、input/target_logs.json 列出的 evidence 日志和 input/logparse-receipt.json。
- 不调用 Logparse，不读取工作区以外路径，不查找 raw、case.json、oracle 或预期答案。
- 检查 service/API 范围内所有相关调用和全部正向证据，不能在第一条命中后停止。
- 每个原因、每次独立调用分别输出一条 evidence；证据不足以证明同一次调用时不得合并。
- sources 必须给出 source_id、从 1 开始的精确 line_number、该行 marker 和完整冻结日志原文 line。
- identity_tokens 必须原样来自本条 evidence 的 sources；候选方法没有正向日志时不得编造 evidence。
- 最终只输出符合 input/diagnosis-result.schema.json 的 JSON，文字字段使用自然中文。
- scenario_id 必须是 ${caseItem.data.scenario_id}。
- logparse_receipt_sha256 必须是 ${receiptSha256}。`;
}

function stringArray(value, label, { nonEmpty = false } = {}) {
  requireRunner(Array.isArray(value) && (!nonEmpty || value.length > 0) && value.every((item) => typeof item === "string" && item.trim().length > 0) && new Set(value).size === value.length, "CODEX_LUNA_RESULT_ARRAY_INVALID", `${label} must contain unique non-empty strings`);
  return value;
}

function rpcBranchMapping(manifest) {
  const markerSet = (method) => new Set(method.evidence_markers);
  const includes = (method, marker) => [...markerSet(method)].some((value) => value === marker || value.startsWith(`${marker} `));
  const api = manifest.methods.filter((method) => includes(method, "API_COMPLETE") && includes(method, "DEADLOOP_DETECTED"));
  const queue = manifest.methods.filter((method) => includes(method, "QUEUE_HISTORY"));
  const client = manifest.methods.filter((method) => includes(method, "LATE_RESPONSE") && !["API_COMPLETE", "DEADLOOP_DETECTED", "QUEUE_HISTORY"].some((marker) => includes(method, marker)));
  requireRunner(api.length === 1 && queue.length === 1 && client.length === 1, "CODEX_LUNA_BRANCH_MAPPING_INVALID", "Generated methods cannot be mapped to the three RPC Wiki causes");
  return {
    API_COMPLETE: api[0].id,
    DEADLOOP_DETECTED: api[0].id,
    QUEUE_HISTORY: queue[0].id,
    LATE_RESPONSE: client[0].id,
  };
}

function validateDiagnosis({ result, caseItem, preprocessing, manifest, branchMapping, workspace }) {
  const scenarioId = caseItem.data.scenario_id;
  requireRunner(exactKeys(result, RESULT_KEYS), "CODEX_LUNA_RESULT_KEYS_INVALID", "Diagnosis result keys do not match the contract", { scenario_id: scenarioId });
  requireRunner(result.schema_version === 2 && result.scenario_id === scenarioId && result.logparse_receipt_sha256 === preprocessing.receipt_sha256, "CODEX_LUNA_RESULT_IDENTITY_INVALID", "Diagnosis result identity is invalid", { scenario_id: scenarioId });
  requireRunner(["CONFIRMED", "PARTIAL", "INSUFFICIENT"].includes(result.status) && result.status === caseItem.data.expected_status, "CODEX_LUNA_RESULT_STATUS_INVALID", "Diagnosis status differs from the scenario oracle", { scenario_id: scenarioId, expected: caseItem.data.expected_status, actual: result.status });
  const confirmed = stringArray(result.confirmed_methods, "confirmed_methods");
  const candidates = stringArray(result.candidate_methods, "candidate_methods");
  requireRunner(!confirmed.some((methodId) => candidates.includes(methodId)), "CODEX_LUNA_RESULT_METHOD_OVERLAP", "Confirmed and candidate methods must be disjoint", { scenario_id: scenarioId });
  const knownMethods = new Map(manifest.methods.map((method) => [method.id, method]));
  requireRunner([...confirmed, ...candidates].every((methodId) => knownMethods.has(methodId)), "CODEX_LUNA_RESULT_METHOD_UNKNOWN", "Diagnosis selected an unknown method", { scenario_id: scenarioId });
  stringArray(result.limitations, "limitations");
  stringArray(result.safety_notes, "safety_notes", { nonEmpty: true });
  requireRunner(Array.isArray(result.evidence), "CODEX_LUNA_RESULT_EVIDENCE_INVALID", "Diagnosis evidence must be an array", { scenario_id: scenarioId });
  const sourceLines = Object.fromEntries(preprocessing.sources.map((source) => [source.label, fs.readFileSync(path.join(workspace, "evidence", `${source.label}.log`), "utf8").split(/\r?\n/)]));
  const confirmedWithEvidence = new Set();
  const evidenceIdentities = new Set();
  for (const [index, evidence] of result.evidence.entries()) {
    requireRunner(exactKeys(evidence, EVIDENCE_KEYS) && knownMethods.has(evidence.method_id) && typeof evidence.summary === "string" && evidence.summary.trim().length > 0, "CODEX_LUNA_EVIDENCE_CONTRACT_INVALID", "Diagnosis evidence item is invalid", { scenario_id: scenarioId, index });
    requireRunner(confirmed.includes(evidence.method_id), "CODEX_LUNA_CANDIDATE_EVIDENCE_FORBIDDEN", "Only confirmed methods may carry positive evidence", { scenario_id: scenarioId, index });
    const identityTokens = stringArray(evidence.identity_tokens, `evidence[${index}].identity_tokens`, { nonEmpty: true });
    const identity = `${evidence.method_id}\0${[...identityTokens].sort().join("\0")}`;
    requireRunner(!evidenceIdentities.has(identity), "CODEX_LUNA_EVIDENCE_IDENTITY_DUPLICATE", "Diagnosis duplicated one method occurrence", { scenario_id: scenarioId, index });
    evidenceIdentities.add(identity);
    requireRunner(Array.isArray(evidence.sources) && evidence.sources.length > 0, "CODEX_LUNA_EVIDENCE_SOURCES_INVALID", "Diagnosis evidence must cite sources", { scenario_id: scenarioId, index });
    const methodMarkers = new Set(knownMethods.get(evidence.method_id).evidence_markers);
    let positiveMarker = false;
    const citedLines = [];
    const sourceSignatures = new Set();
    for (const [sourceIndex, source] of evidence.sources.entries()) {
      requireRunner(exactKeys(source, SOURCE_KEYS) && Object.hasOwn(sourceLines, source.source_id) && Number.isSafeInteger(source.line_number) && source.line_number > 0 && typeof source.marker === "string" && source.marker.length > 0 && typeof source.line === "string" && source.line.length > 0, "CODEX_LUNA_EVIDENCE_SOURCE_INVALID", "Diagnosis evidence source is invalid", { scenario_id: scenarioId, index, source_index: sourceIndex });
      const actualLine = sourceLines[source.source_id][source.line_number - 1];
      requireRunner(actualLine === source.line && source.line.includes(source.marker), "CODEX_LUNA_EVIDENCE_SOURCE_UNGROUNDED", "Diagnosis source line/number/marker is not grounded in the frozen log", { scenario_id: scenarioId, index, source_index: sourceIndex });
      const signature = `${source.source_id}\0${source.line_number}\0${source.marker}\0${source.line}`;
      requireRunner(!sourceSignatures.has(signature), "CODEX_LUNA_EVIDENCE_SOURCE_DUPLICATE", "Diagnosis evidence repeated one source", { scenario_id: scenarioId, index, source_index: sourceIndex });
      sourceSignatures.add(signature);
      citedLines.push(source.line);
      if (methodMarkers.has(source.marker)) positiveMarker = true;
    }
    requireRunner(positiveMarker, "CODEX_LUNA_EVIDENCE_MARKER_UNINDEXED", "Diagnosis evidence has no methods-v1 positive marker", { scenario_id: scenarioId, index });
    requireRunner(identityTokens.every((token) => citedLines.some((line) => line.includes(token))), "CODEX_LUNA_EVIDENCE_IDENTITY_UNGROUNDED", "Diagnosis identity token is absent from its cited sources", { scenario_id: scenarioId, index });
    if (confirmed.includes(evidence.method_id)) confirmedWithEvidence.add(evidence.method_id);
  }
  requireRunner([...confirmed].every((methodId) => confirmedWithEvidence.has(methodId)), "CODEX_LUNA_CONFIRMED_WITHOUT_EVIDENCE", "Every confirmed method must have grounded evidence", { scenario_id: scenarioId });
  const safetyText = result.safety_notes.join(" ").toLowerCase();
  const chineseCancellationBoundary = safetyText.includes("超时") && safetyText.includes("取消") && ["不", "未", "并非"].some((term) => safetyText.includes(term));
  const englishCancellationBoundary = safetyText.includes("not") && (safetyText.includes("cancel") || safetyText.includes("cancellation"));
  requireRunner(chineseCancellationBoundary || englishCancellationBoundary, "CODEX_LUNA_CANCELLATION_BOUNDARY_MISSING", "Diagnosis must preserve the Wiki boundary that timeout is not cancellation", { scenario_id: scenarioId });
  const expectedMethods = new Set(caseItem.data.expected_branch_markers.map((marker) => branchMapping[marker]));
  requireRunner(!caseItem.data.expected_branch_markers.some((marker) => !branchMapping[marker]), "CODEX_LUNA_ORACLE_MARKER_UNKNOWN", "Scenario oracle references an unmapped branch marker", { scenario_id: scenarioId });
  requireRunner(confirmed.length === expectedMethods.size && confirmed.every((methodId) => expectedMethods.has(methodId)), "CODEX_LUNA_BRANCH_ORACLE_MISMATCH", "Confirmed methods differ from the scenario branch oracle", { scenario_id: scenarioId });
  if (expectedMethods.size === 0) {
    const limitationText = result.limitations.join(" ").toLowerCase();
    requireRunner(result.evidence.length === 0 && ["抑制", "限流", "suppression", "rate limit"].some((term) => limitationText.includes(term)), "CODEX_LUNA_INSUFFICIENT_BOUNDARY_MISSING", "Insufficient diagnosis must not invent evidence and must preserve suppression/rate-limit uncertainty", { scenario_id: scenarioId });
  }
  const matchedEvidenceIndexes = new Set();
  for (const expectation of caseItem.data.expected_evidence_identities) {
    requireRunner(isPlainObject(expectation) && typeof expectation.branch_marker === "string" && Array.isArray(expectation.identity_tokens), "CODEX_LUNA_IDENTITY_ORACLE_INVALID", "Scenario evidence identity oracle is invalid", { scenario_id: scenarioId });
    const methodId = branchMapping[expectation.branch_marker];
    const matches = result.evidence
      .map((evidence, index) => ({ evidence, index }))
      .filter(({ evidence }) => evidence.method_id === methodId
        && expectation.identity_tokens.every((token) => evidence.identity_tokens.includes(token))
        && evidence.sources.some((source) => source.marker === expectation.branch_marker));
    requireRunner(matches.length === 1, "CODEX_LUNA_IDENTITY_ORACLE_MISMATCH", "Diagnosis did not preserve exactly one expected event identity", { scenario_id: scenarioId, marker: expectation.branch_marker });
    requireRunner(!matchedEvidenceIndexes.has(matches[0].index), "CODEX_LUNA_IDENTITY_ORACLE_MERGED", "One evidence item merged multiple expected event identities", { scenario_id: scenarioId, marker: expectation.branch_marker });
    matchedEvidenceIndexes.add(matches[0].index);
  }
  const rendered = JSON.stringify(result);
  requireRunner(caseItem.data.expected_terms.every((term) => rendered.includes(term)), "CODEX_LUNA_EXPECTED_TERM_MISSING", "Diagnosis omitted a required scenario term", { scenario_id: scenarioId });
  const evidenceRendered = JSON.stringify(result.evidence);
  requireRunner(caseItem.data.forbidden_evidence_terms.every((term) => !evidenceRendered.includes(term)), "CODEX_LUNA_FORBIDDEN_EVIDENCE_PRESENT", "Diagnosis associated forbidden evidence with the scenario", { scenario_id: scenarioId });
  return { scenario_id: scenarioId, status: result.status, confirmed_methods: confirmed, evidence_count: result.evidence.length, receipt_sha256: preprocessing.receipt_sha256 };
}

async function validateGeneratedSkill({ validator, skillRoot, wiki, environment, validatorPython, validatorRuntimeRoot, validatorRuntimeIdentity }) {
  ordinaryFile(validator, "methods-v1 validator");
  const expectedRuntime = readJson(validatorRuntimeIdentity, "methods-v1 validator runtime identity");
  requireRunner(
    path.resolve(validatorPython) === path.join(path.resolve(validatorRuntimeRoot), ".venv", "bin", "python"),
    "CODEX_LUNA_VALIDATOR_PYTHON_ENTRY_INVALID",
    "Validator Python must use the planned virtual-environment entry",
  );
  const beforeRuntime = codexLogparseRuntimeIdentity(validatorRuntimeRoot);
  requireRunner(canonicalJson(beforeRuntime) === canonicalJson(expectedRuntime), "CODEX_LUNA_VALIDATOR_RUNTIME_DRIFT", "Validator Python runtime differs from the planned Logparse runtime before validation");
  const result = await captureProcess(validatorPython, ["-I", "-B", validator, "--skill-dir", skillRoot, "--wiki", wiki, "--json"], { cwd: path.dirname(skillRoot), environment, wallSeconds: 120, noProgressSeconds: 30 });
  const afterRuntime = codexLogparseRuntimeIdentity(validatorRuntimeRoot);
  requireRunner(canonicalJson(afterRuntime) === canonicalJson(expectedRuntime), "CODEX_LUNA_VALIDATOR_RUNTIME_DRIFT", "Validator Python runtime differs from the planned Logparse runtime after validation");
  requireRunner(result.code === 0 && result.signal === null && !result.timed_out && !result.no_progress_timed_out, "CODEX_LUNA_VALIDATOR_PROCESS_FAILED", "methods-v1 validator process failed", { code: result.code, stderr: result.stderr.slice(-2_000) });
  let receipt;
  try {
    receipt = JSON.parse(result.stdout);
  } catch (error) {
    fail("CODEX_LUNA_VALIDATOR_OUTPUT_INVALID", "methods-v1 validator did not return JSON", { cause: error.message });
  }
  requireRunner(isPlainObject(receipt) && receipt.ok === true, "CODEX_LUNA_VALIDATOR_REJECTED", "methods-v1 validator rejected the generated Skill", { receipt });
  return {
    ...receipt,
    runtime_identity_sha256: sha256Bytes(canonicalJson(expectedRuntime)),
    runtime_policy: "exact-planned-logparse-python-isolated-pre-and-post-v1",
  };
}

function buildInvocationUsageReceipt({ invocationId, phase, logicalId, trace, passed, failureCode, processReceipt }) {
  return {
    schema_version: USAGE_RECEIPT_SCHEMA_VERSION,
    invocation_id: invocationId,
    class: INVOCATION_CLASS,
    workflow: phase,
    logical_id: logicalId,
    effective_model: CODEX_LUNA_MODEL,
    effective_reasoning_effort: CODEX_LUNA_REASONING_EFFORT,
    effective_caps: {
      max_calls: CODEX_LUNA_MAX_CALLS,
      call_wall_seconds: CODEX_LUNA_CALL_WALL_SECONDS,
      no_progress_seconds: CODEX_LUNA_NO_PROGRESS_SECONDS,
      stage_wall_seconds: CODEX_LUNA_STAGE_WALL_SECONDS,
      max_total_tokens_posthoc: CODEX_LUNA_TOKEN_LIMIT,
      max_equivalent_usd_posthoc: CODEX_LUNA_EQUIVALENT_USD_LIMIT,
    },
    usage_complete: trace !== null,
    usage: trace === null ? null : normalizeCodexUsage(trace.usage),
    turns: trace?.turn_count ?? null,
    turns_source: trace === null ? null : "app-server-one-ephemeral-thread-one-terminal-turn-with-raw-response-usage",
    terminal: trace === null ? null : { event: "turn.completed", thread_id: trace.thread_id, turn_id: trace.turn_id },
    wrapper_outcome: { schema_version: 1, status: passed ? "PASS" : "FAIL", code: passed ? null : failureCode },
    posthoc_enforcement: {
      schema_version: 1,
      exception_id: CODEX_LUNA_POSTHOC_EXCEPTION_ID,
      calls: "runner-precondition-exactly-ten-no-retry",
      wall: "wrapper-process-watchdog",
      no_progress: "wrapper-stream-watchdog",
      total_tokens: "terminal-usage-postcondition-only",
      equivalent_usd: "terminal-usage-postcondition-only",
    },
    process: processReceipt,
  };
}

class InvocationLedger {
  constructor({ runId, evidenceRoot, usageRoot, startedAt, privateRoot, sourceSnapshotRoot, auth, environment }) {
    this.runId = runId;
    this.evidenceRoot = evidenceRoot;
    this.usageRoot = usageRoot;
    this.startedAt = startedAt;
    this.privateRoot = privateRoot;
    this.sourceSnapshotRoot = sourceSnapshotRoot;
    this.auth = auth;
    this.environment = environment;
    this.callsRoot = path.join(privateRoot, "calls");
    fs.mkdirSync(this.callsRoot, { mode: 0o700 });
    this.calls = [];
  }

  flush() {
    writeJson(path.join(this.evidenceRoot, "codex-luna-invocations.json"), {
      schema_version: LEDGER_SCHEMA_VERSION,
      run_id: this.runId,
      invocation_class: INVOCATION_CLASS,
      expected_calls: CODEX_LUNA_MAX_CALLS,
      retry_policy: "NONE",
      calls: this.calls,
    });
  }

  async invoke({ codexEntry, phase, logicalId, workspace, skillPath, finalPath, outputSchema, prompt, tracePath, stderrPath }) {
    requireRunner(this.calls.length < CODEX_LUNA_MAX_CALLS, "CODEX_LUNA_CALL_LIMIT_EXCEEDED", "Codex exploration closure cannot exceed ten model calls");
    const elapsedSeconds = Math.ceil((Date.now() - this.startedAt) / 1_000);
    const remainingStageSeconds = CODEX_LUNA_STAGE_WALL_SECONDS - elapsedSeconds;
    requireRunner(remainingStageSeconds > 0, "CODEX_LUNA_STAGE_TIMEOUT", "Codex exploration closure exceeded its stage wall limit");
    const number = this.calls.length + 1;
    const invocationId = `${this.runId}:codex-luna:${String(number).padStart(2, "0")}`;
    const call = {
      schema_version: 1,
      invocation_id: invocationId,
      class: INVOCATION_CLASS,
      workflow: phase,
      logical_id: logicalId,
      ordinal: number,
      attempt: 1,
      retry_allowed: false,
      status: "RUNNING",
      started_at: new Date().toISOString(),
      trace: path.relative(this.evidenceRoot, tracePath).split(path.sep).join("/"),
    };
    this.calls.push(call);
    this.flush();
    const wallSeconds = Math.min(CODEX_LUNA_CALL_WALL_SECONDS, remainingStageSeconds);
    const forbiddenReadPaths = [
      path.join(this.sourceSnapshotRoot, "AGENTS.md"),
      path.join(this.sourceSnapshotRoot, "experiments", "rpc-skill-feasibility", "cases", "api-execution-overrun", "raw", "client.log"),
      this.auth.source_path,
    ];
    let lastOuterProgress = 0;
    const emitOuterProgress = (reason) => {
      const now = Date.now();
      if (reason === "stream" && now - lastOuterProgress < 30_000) return;
      lastOuterProgress = now;
      process.stdout.write(`TEST_FLOW_PROGRESS stage.progress codex-luna ${number} ${phase} ${logicalId} ${reason}\n`);
    };
    emitOuterProgress("started");
    let trace = null;
    let traceError = null;
    try {
      trace = await runCodexLunaAppServerCall({
        codexEntry,
        auth: this.auth,
        environment: this.environment,
        workspaceRoot: workspace,
        skillPath,
        mode: phase === "methods-generation" ? "generation" : "diagnosis",
        prompt,
        outputSchema,
        callRoot: path.join(this.callsRoot, String(number).padStart(2, "0")),
        privateRoot: this.privateRoot,
        tracePath,
        stderrPath,
        finalPath,
        forbiddenReadPaths,
        wallSeconds,
        noProgressSeconds: CODEX_LUNA_NO_PROGRESS_SECONDS,
        onProgress: () => emitOuterProgress("stream"),
      });
    } catch (error) {
      traceError = error;
    }
    emitOuterProgress("completed");
    const passed = trace !== null
      && trace.process.exit_code === 0
      && trace.process.signal === null
      && trace.process.spawn_error === null
      && trace.process.timed_out === false
      && trace.process.no_progress_timed_out === false;
    const processReceipt = trace === null ? {
      exit_code: null,
      signal: null,
      spawn_error: null,
      timed_out: traceError?.code === "CODEX_LUNA_APP_SERVER_WALL_TIMEOUT",
      no_progress_timed_out: traceError?.code === "CODEX_LUNA_APP_SERVER_NO_PROGRESS_TIMEOUT",
      app_server: null,
    } : {
      ...trace.process,
      app_server: trace.app_server,
    };
    Object.assign(call, {
      status: passed ? "PASS" : "FAIL",
      completed_at: new Date().toISOString(),
      process: processReceipt,
      thread_id: trace?.thread_id ?? null,
      turn_id: trace?.turn_id ?? null,
      usage_complete: trace !== null,
      usage: trace === null ? null : normalizeCodexUsage(trace.usage),
      terminal: trace === null ? null : { event: "turn.completed", thread_id: trace.thread_id, turn_id: trace.turn_id },
      failure: passed ? null : { code: traceError?.code ?? "CODEX_LUNA_PROCESS_FAILED", message: traceError?.message ?? "Codex process did not complete successfully" },
    });
    this.flush();
    const usageReceipt = buildInvocationUsageReceipt({
      invocationId,
      phase,
      logicalId,
      trace,
      passed,
      failureCode: call.failure?.code ?? null,
      processReceipt: call.process,
    });
    writeJson(path.join(this.usageRoot, `${invocationId.replaceAll(":", "-")}.json`), usageReceipt, { exclusive: true });
    requireRunner(passed, call.failure.code, call.failure.message, { logical_id: logicalId, process: call.process, details: traceError?.details ?? {} });
    return trace;
  }
}

function parseArguments(argv) {
  const values = {};
  const flags = new Set(["allow-posthoc-budget"]);
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    requireRunner(argument.startsWith("--"), "CODEX_LUNA_ARGUMENT_INVALID", "Runner arguments must use --name value syntax", { argument });
    const name = argument.slice(2);
    requireRunner(!Object.hasOwn(values, name), "CODEX_LUNA_ARGUMENT_DUPLICATE", "Runner arguments must be unique", { name });
    if (flags.has(name)) values[name] = true;
    else {
      requireRunner(index + 1 < argv.length && !argv[index + 1].startsWith("--"), "CODEX_LUNA_ARGUMENT_VALUE_MISSING", "Runner argument is missing its value", { name });
      values[name] = argv[index + 1];
      index += 1;
    }
  }
  const required = ["codex-entry", "auth-source", "meta-skill-root", "wiki", "case-root", "preprocessed-root", "validator-python", "validator-runtime-root", "validator-runtime-identity", "work-root", "private-root", "evidence-root", "usage-root", "run-id"];
  requireRunner(required.every((name) => typeof values[name] === "string" && values[name].length > 0) && values["allow-posthoc-budget"] === true, "CODEX_LUNA_REQUIRED_ARGUMENT_MISSING", "Runner requires all identity/input/output roots and explicit --allow-posthoc-budget", { required });
  requireRunner(/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(values["run-id"]), "CODEX_LUNA_RUN_ID_INVALID", "Run ID is invalid");
  return values;
}

export async function runCodexLunaExploration(options, { ambientEnvironment = process.env } = {}) {
  const startedAt = Date.now();
  const workRoot = createEmptyRoot(options.workRoot, "Codex work root");
  const privateRoot = createEmptyRoot(options.privateRoot, "Codex private root");
  const evidenceRoot = createEmptyRoot(options.evidenceRoot, "Codex evidence root");
  const usageRoot = path.resolve(options.usageRoot);
  if (fs.existsSync(usageRoot)) ordinaryDirectory(usageRoot, "Codex usage root");
  else fs.mkdirSync(usageRoot, { recursive: true, mode: 0o700 });
  assertDisjointRoots({ workRoot, privateRoot, evidenceRoot, usageRoot });
  const tracesRoot = path.join(evidenceRoot, "traces");
  fs.mkdirSync(tracesRoot, { recursive: true, mode: 0o700 });
  const sourceSnapshotRoot = path.resolve(options.metaSkillRoot, "..", "..", "..");
  const bootstrapRoot = path.join(privateRoot, "bootstrap");
  const bootstrapCodexHome = path.join(bootstrapRoot, "codex-home");
  const bootstrapHome = path.join(bootstrapRoot, "home");
  const bootstrapTemporary = path.join(bootstrapRoot, "tmp");
  for (const directory of [bootstrapRoot, bootstrapCodexHome, bootstrapHome, bootstrapTemporary]) {
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  }
  const environment = safeEnvironment(ambientEnvironment, {
    codexHome: bootstrapCodexHome,
    home: bootstrapHome,
    temporary: bootstrapTemporary,
  });
  const environmentReceipt = environmentAudit(ambientEnvironment, environment);
  const externalAuth = readCodexLunaExternalAuth(options.authSource, ambientEnvironment);
  const runtimeAuth = { ...externalAuth, source_path: path.resolve(options.authSource) };
  const sharedIdentity = validateCodexLunaIdentity(options.codexEntry, options.authSource);
  const ledger = new InvocationLedger({
    runId: options.runId,
    evidenceRoot,
    usageRoot,
    startedAt,
    privateRoot,
    sourceSnapshotRoot,
    auth: runtimeAuth,
    environment,
  });
  const immutableInputRoots = [...new Set([path.dirname(options.codexEntry), path.dirname(options.authSource), options.validatorRuntimeRoot].map((entry) => path.resolve(entry)))];
  let identity = null;
  let protocolSchemaReceipt = null;
  let generatedSkillReceipt = null;
  let diagnosisResults = [];
  let failure = null;
  try {
    ordinaryFile(options.wiki, "source Wiki");
    ordinaryDirectory(options.metaSkillRoot, "promoted methods-v1 meta Skill");
    ordinaryDirectory(options.preprocessedRoot, "Codex preprocessed scenario root");
    ordinaryFile(options.diagnosisSchema, "Codex diagnosis schema");
    const metaName = frontmatterName(options.metaSkillRoot);
    requireRunner(metaName === META_SKILL_NAME, "CODEX_LUNA_META_SKILL_INVALID", "Promoted meta Skill identity is invalid");
    const cases = loadCases(options.caseRoot);
    const preprocessing = new Map(cases.map((caseItem) => [caseItem.data.scenario_id, validatePreprocessedCase(caseItem, options.preprocessedRoot)]));
    protocolSchemaReceipt = generateCodexLunaProtocolSchemaReceipt({
      codexEntry: options.codexEntry,
      schemaRoot: path.join(privateRoot, "protocol-schema"),
      environment,
    });
    identity = await probeCodexIdentity(options.codexEntry, environment);
    const expectedValidatorRuntime = readJson(options.validatorRuntimeIdentity, "methods-v1 validator runtime identity");
    const initialValidatorRuntime = codexLogparseRuntimeIdentity(options.validatorRuntimeRoot);
    requireRunner(canonicalJson(initialValidatorRuntime) === canonicalJson(expectedValidatorRuntime), "CODEX_LUNA_VALIDATOR_RUNTIME_DRIFT", "Validator Python runtime differs from the planned Logparse runtime before generation");
    requireRunner(sharedIdentity.cli.sha256 === identity.sha256 && sharedIdentity.cli.version === identity.version, "CODEX_LUNA_IDENTITY_RECHECK_MISMATCH", "Planner/action identity validation and isolated runtime probe differ");
    requireRunner(
      sharedIdentity.auth.sha256 === externalAuth.receipt.source_sha256
        && sharedIdentity.auth.size === externalAuth.receipt.byte_count
        && sharedIdentity.auth.account_id_sha256 === externalAuth.receipt.account_id_sha256
        && sharedIdentity.auth.transfer === externalAuth.receipt.transfer,
      "CODEX_LUNA_AUTH_IDENTITY_RECHECK_MISMATCH",
      "Validated auth identity and external in-memory runtime auth differ",
    );
    const identityReceipt = {
      schema_version: 1,
      contract_version: CODEX_LUNA_CONTRACT_VERSION,
      run_id: options.runId,
      invocation_class: INVOCATION_CLASS,
      cli: identity,
      model: CODEX_LUNA_MODEL,
      reasoning_effort: CODEX_LUNA_REASONING_EFFORT,
      auth: { ...externalAuth.receipt, auth_json_files: 0 },
      filesystem_sandbox: sharedIdentity.filesystem_sandbox,
      environment: environmentReceipt,
      model_shell_environment: {
        inherit: "none",
        set_keys: ["HOME", "LANG", "PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE"],
        auth_environment_available: false,
        home_is_workspace_local: true,
      },
      meta_skill: { name: META_SKILL_NAME, tree_sha256: treeDigest(options.metaSkillRoot) },
      wiki: { sha256: sha256File(options.wiki), size: fs.statSync(options.wiki).size },
      scenarios: cases.map((caseItem) => caseItem.data.scenario_id),
      protocol_schema: protocolSchemaReceipt,
      validator_runtime: {
        policy: "exact-planned-logparse-python-isolated-pre-and-post-v1",
        identity_sha256: sha256Bytes(canonicalJson(expectedValidatorRuntime)),
        python_entry_path_sha256: sha256Bytes(path.resolve(options.validatorPython)),
      },
    };
    writeJson(path.join(evidenceRoot, "codex-luna-identity.json"), identityReceipt, { exclusive: true });

    const generationWorkspace = path.join(workRoot, "generation");
    const preparedGeneration = buildGenerationWorkspace({ attemptRoot: generationWorkspace, metaSkillRoot: options.metaSkillRoot, wiki: options.wiki });
    const generationFinal = path.join(tracesRoot, "01-generation.final.txt");
    const generationTrace = await ledger.invoke({
      codexEntry: options.codexEntry,
      phase: "methods-generation",
      logicalId: "generate",
      workspace: generationWorkspace,
      skillPath: path.join(generationWorkspace, ".agents", "skills", META_SKILL_NAME, "SKILL.md"),
      finalPath: generationFinal,
      outputSchema: null,
      prompt: generationPrompt(),
      tracePath: path.join(tracesRoot, "01-generation.jsonl"),
      stderrPath: path.join(tracesRoot, "01-generation.stderr.txt"),
    });
    requireRunner(ordinaryFile(generationFinal, "Codex generation final message").size > 0, "CODEX_LUNA_GENERATION_FINAL_EMPTY", "Codex generation final message is empty");
    const generatedSkill = path.join(generationWorkspace, "generated", GENERATED_SKILL_NAME);
    const generationScopeAudit = auditGenerationCommands(generationTrace.commands, {
      workspaceRoot: generationWorkspace,
      forbiddenRoots: [sourceSnapshotRoot, options.preprocessedRoot, privateRoot, ...immutableInputRoots],
    });
    const observedSourceWikiIdentity = readJson(preparedGeneration.sourceWikiIdentityPath, "Codex source Wiki identity");
    validateCodexLunaSourceWikiIdentity(observedSourceWikiIdentity, fs.readFileSync(options.wiki));
    requireRunner(
      fs.readFileSync(preparedGeneration.sourceWikiIdentityPath, "utf8") === `${canonicalJson(preparedGeneration.sourceWikiIdentity)}\n`,
      "CODEX_LUNA_SOURCE_WIKI_IDENTITY_DRIFT",
      "Codex generation changed the canonical source Wiki identity bytes",
    );
    const packageContract = verifyMethodsV1Package(generatedSkill, preparedGeneration.sourceWikiIdentity);
    const validator = path.join(options.metaSkillRoot, "scripts", "validate_generated_skill.py");
    const validatorReceipt = await validateGeneratedSkill({
      validator,
      skillRoot: generatedSkill,
      wiki: options.wiki,
      environment,
      validatorPython: options.validatorPython,
      validatorRuntimeRoot: options.validatorRuntimeRoot,
      validatorRuntimeIdentity: options.validatorRuntimeIdentity,
    });
    const branchMapping = rpcBranchMapping(packageContract.manifest);
    const durableGeneratedSkill = path.join(evidenceRoot, "generated-skill");
    copyOrdinaryTree(generatedSkill, durableGeneratedSkill);
    requireRunner(
      treeDigest(durableGeneratedSkill) === packageContract.tree_sha256,
      "CODEX_LUNA_DURABLE_SKILL_DRIFT",
      "Durable generated Skill evidence differs from the validated package",
    );
    generatedSkillReceipt = {
      schema_version: 1,
      skill_name: GENERATED_SKILL_NAME,
      methods_schema_version: 1,
      package_tree_sha256: packageContract.tree_sha256,
      source_wiki_sha256: sha256File(options.wiki),
      generation_thread_id: generationTrace.thread_id,
      generation_final_sha256: sha256File(generationFinal),
      generation_scope_audit: generationScopeAudit,
      validator: validatorReceipt,
      method_ids: packageContract.method_ids,
      branch_mapping: branchMapping,
      durable_package: {
        path: "generated-skill",
        tree_sha256: treeDigest(durableGeneratedSkill),
        manifest: treeManifest(durableGeneratedSkill),
      },
    };
    writeJson(path.join(evidenceRoot, "codex-luna-skill.json"), generatedSkillReceipt, { exclusive: true });

    for (const [caseIndex, caseItem] of cases.entries()) {
      requireRunner(treeDigest(generatedSkill) === packageContract.tree_sha256, "CODEX_LUNA_GENERATED_SKILL_DRIFT", "Generated Skill changed between diagnosis scenarios", { scenario_id: caseItem.data.scenario_id });
      const casePreprocessing = preprocessing.get(caseItem.data.scenario_id);
      const diagnosisWorkspace = path.join(workRoot, "diagnoses", caseItem.data.scenario_id);
      const prepared = buildDiagnosisWorkspace({ attemptRoot: diagnosisWorkspace, generatedSkill, caseItem, preprocessing: casePreprocessing, schemaPath: options.diagnosisSchema });
      requireRunner(treeDigest(prepared.installedSkill) === packageContract.tree_sha256, "CODEX_LUNA_INSTALLED_SKILL_DRIFT", "Diagnosis did not receive the exact generated Skill package", { scenario_id: caseItem.data.scenario_id });
      const ordinal = String(caseIndex + 2).padStart(2, "0");
      const finalPath = path.join(tracesRoot, `${ordinal}-${caseItem.data.scenario_id}.final.json`);
      const trace = await ledger.invoke({
        codexEntry: options.codexEntry,
        phase: "methods-diagnosis",
        logicalId: caseItem.data.scenario_id,
        workspace: diagnosisWorkspace,
        skillPath: path.join(prepared.installedSkill, "SKILL.md"),
        finalPath,
        outputSchema: prepared.outputSchema,
        prompt: diagnosisPrompt(caseItem, casePreprocessing.receipt_sha256),
        tracePath: path.join(tracesRoot, `${ordinal}-${caseItem.data.scenario_id}.jsonl`),
        stderrPath: path.join(tracesRoot, `${ordinal}-${caseItem.data.scenario_id}.stderr.txt`),
      });
      const scopeAudit = auditDiagnosisCommands(trace.commands, {
        workspaceRoot: diagnosisWorkspace,
        forbiddenRoots: [sourceSnapshotRoot, options.preprocessedRoot, privateRoot, ...immutableInputRoots],
      });
      requireRunner(treeDigest(prepared.installedSkill) === packageContract.tree_sha256, "CODEX_LUNA_INSTALLED_SKILL_MUTATED", "Read-only diagnosis changed its installed Skill", { scenario_id: caseItem.data.scenario_id });
      const result = readJson(finalPath, `Codex diagnosis result for ${caseItem.data.scenario_id}`);
      const validated = validateDiagnosis({ result, caseItem, preprocessing: casePreprocessing, manifest: packageContract.manifest, branchMapping, workspace: diagnosisWorkspace });
      diagnosisResults.push({ ...validated, package_tree_sha256: packageContract.tree_sha256, thread_id: trace.thread_id, turn_id: trace.turn_id, scope_audit: scopeAudit, result_sha256: sha256File(finalPath) });
    }
    requireRunner(ledger.calls.length === CODEX_LUNA_MAX_CALLS, "CODEX_LUNA_CALL_COUNT_INVALID", "Codex exploration closure must execute exactly one generation and nine diagnosis calls", { count: ledger.calls.length });
    requireRunner(ledger.calls.filter((call) => call.workflow === "methods-generation").length === 1 && ledger.calls.filter((call) => call.workflow === "methods-diagnosis").length === CODEX_LUNA_SCENARIO_COUNT, "CODEX_LUNA_CALL_SHAPE_INVALID", "Codex invocation ledger does not contain the required 1+9 shape");
  } catch (error) {
    failure = {
      code: error?.code ?? "CODEX_LUNA_UNEXPECTED_ERROR",
      message: error?.message ?? String(error),
      details: error?.details ?? {},
    };
  }

  ledger.flush();
  const budget = buildPosthocBudgetReceipt({ calls: ledger.calls, usageComplete: ledger.calls.every((call) => call.usage_complete === true) });
  writeJson(path.join(evidenceRoot, "codex-luna-usage.json"), budget, { exclusive: true });
  let securityAudit;
  try {
    const appServerCalls = ledger.calls.filter((call) => call.process?.app_server?.status === "PASS");
    requireRunner(
      ledger.calls.length === CODEX_LUNA_NORMAL_CALLS && appServerCalls.length === CODEX_LUNA_NORMAL_CALLS,
      "CODEX_LUNA_APP_SERVER_EVIDENCE_INCOMPLETE",
      "Every Codex call must complete through one validated app-server session",
    );
    const authJsonFiles = appServerCalls.reduce((sum, call) => sum + call.process.app_server.codex_home.auth_json_files, 0);
    requireRunner(
      authJsonFiles === 0,
      "CODEX_LUNA_APP_SERVER_AUTH_PERSISTED",
      "External ChatGPT credentials must not create auth.json in any isolated Codex home",
    );
    requireRunner(
      appServerCalls.every((call) => call.process.app_server.preflight?.status === "PASS"
        && call.process.app_server.cleanup?.status === "PASS"),
      "CODEX_LUNA_PERMISSION_PROFILE_INCOMPLETE",
      "Every Codex call must pass its permission-profile preflight and app-server cleanup",
    );
    requireRunner(
      new Set(appServerCalls.map((call) => call.thread_id)).size === CODEX_LUNA_NORMAL_CALLS
        && new Set(appServerCalls.map((call) => call.turn_id)).size === CODEX_LUNA_NORMAL_CALLS,
      "CODEX_LUNA_EPHEMERAL_CALL_IDENTITY_REUSED",
      "Each Codex call must use one unique ephemeral thread and turn",
    );
    requireRunner(
      protocolSchemaReceipt?.status === "PASS",
      "CODEX_LUNA_PROTOCOL_SCHEMA_EVIDENCE_MISSING",
      "Pinned app-server protocol schema evidence is unavailable",
    );
    const artifactSecretScan = auditCodexLunaRuntimeSecrets({
      roots: [privateRoot, workRoot, evidenceRoot, usageRoot],
      auth: externalAuth,
    });
    securityAudit = {
      schema_version: 1,
      status: "PASS",
      auth_isolation: { ...externalAuth.receipt, auth_json_files: authJsonFiles },
      environment: environmentReceipt,
      protocol_schema: {
        schema_version: 1,
        status: protocolSchemaReceipt.status,
        file_count: protocolSchemaReceipt.file_count,
        tree_sha256: protocolSchemaReceipt.tree_sha256,
      },
      permission_profiles: {
        schema_version: 1,
        status: "PASS",
        call_count: appServerCalls.length,
        profile_version: CODEX_LUNA_PERMISSION_PROFILE_VERSION,
        enforcement: "single-layer-codex-command-sandbox",
        call_receipts: appServerCalls.map((call) => ({
          invocation_id: call.invocation_id,
          receipt_sha256: sha256Bytes(canonicalJson(call.process.app_server.permission_profile)),
        })),
      },
      artifact_secret_scan: artifactSecretScan,
      oracle_and_logparse_scope: {
        scenario_count: diagnosisResults.length,
        all_passed: diagnosisResults.every((result) => result.scope_audit.status === "PASS"),
        logparse_invocations_during_diagnosis: diagnosisResults.reduce((sum, result) => sum + result.scope_audit.logparse_invocations, 0),
        oracle_accesses: diagnosisResults.reduce((sum, result) => sum + result.scope_audit.oracle_accesses, 0),
        raw_input_accesses: diagnosisResults.reduce((sum, result) => sum + result.scope_audit.raw_input_accesses, 0),
      },
    };
  } catch (error) {
    securityAudit = { schema_version: 1, status: "FAIL", code: error?.code ?? "CODEX_LUNA_SECURITY_AUDIT_FAILED", message: error?.message ?? String(error) };
    failure ??= { code: securityAudit.code, message: securityAudit.message, details: {} };
  }
  writeJson(path.join(evidenceRoot, "codex-luna-security-audit.json"), securityAudit, { exclusive: true });
  if (budget.status === "FAIL" && failure === null) failure = { code: "CODEX_LUNA_POSTHOC_BUDGET_FAILED", message: "Codex usage is incomplete or exceeds the post-hoc token/API-equivalent USD policy", details: { checks: budget.checks } };
  const completed = failure === null
    && diagnosisResults.length === CODEX_LUNA_SCENARIO_COUNT
    && securityAudit.status === "PASS"
    && budget.status === "PASS_WITH_WARNINGS";
  const result = {
    schema_version: RESULT_SCHEMA_VERSION,
    run_id: options.runId,
    status: completed ? "PASS_WITH_WARNINGS" : "FAIL",
    warning: completed ? "Codex token and API-equivalent USD limits are post-hoc only." : null,
    invocation_class: INVOCATION_CLASS,
    model: CODEX_LUNA_MODEL,
    reasoning_effort: CODEX_LUNA_REASONING_EFFORT,
    cli_identity: identity,
    call_contract: { expected: CODEX_LUNA_MAX_CALLS, actual: ledger.calls.length, generation: ledger.calls.filter((call) => call.workflow === "methods-generation").length, diagnosis: ledger.calls.filter((call) => call.workflow === "methods-diagnosis").length, retries: 0 },
    generated_skill: generatedSkillReceipt,
    diagnoses: diagnosisResults,
    posthoc_budget: { exception_id: CODEX_LUNA_POSTHOC_EXCEPTION_ID, status: budget.status, aggregate: budget.aggregate, checks: budget.checks },
    security_audit: { status: securityAudit.status },
    elapsed_seconds: Math.ceil((Date.now() - startedAt) / 1_000),
    failure,
  };
  writeJson(path.join(evidenceRoot, "codex-luna-result.json"), result, { exclusive: true });
  return result;
}

async function main() {
  let values;
  try {
    values = parseArguments(process.argv.slice(2));
    const result = await runCodexLunaExploration({
      runId: values["run-id"],
      codexEntry: path.resolve(values["codex-entry"]),
      authSource: path.resolve(values["auth-source"]),
      metaSkillRoot: path.resolve(values["meta-skill-root"]),
      wiki: path.resolve(values.wiki),
      caseRoot: path.resolve(values["case-root"]),
      preprocessedRoot: path.resolve(values["preprocessed-root"]),
      validatorPython: path.resolve(values["validator-python"]),
      validatorRuntimeRoot: path.resolve(values["validator-runtime-root"]),
      validatorRuntimeIdentity: path.resolve(values["validator-runtime-identity"]),
      workRoot: path.resolve(values["work-root"]),
      privateRoot: path.resolve(values["private-root"]),
      evidenceRoot: path.resolve(values["evidence-root"]),
      usageRoot: path.resolve(values["usage-root"]),
      diagnosisSchema: DEFAULT_DIAGNOSIS_SCHEMA,
    });
    process.stdout.write(`${canonicalJson(result)}\n`);
    if (result.status !== "PASS_WITH_WARNINGS") process.exitCode = 1;
  } catch (error) {
    const failure = {
      schema_version: RESULT_SCHEMA_VERSION,
      status: "FAIL",
      code: error?.code ?? "CODEX_LUNA_RUNNER_CRASHED",
      message: error?.message ?? String(error),
    };
    process.stderr.write(`${canonicalJson(failure)}\n`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();

export {
  INVOCATION_CLASS,
  boundDiagnosisSchema,
  buildDiagnosisWorkspace,
  buildGenerationWorkspace,
  buildInvocationUsageReceipt,
  environmentAudit,
  loadCases,
  parseArguments,
  safeEnvironment,
  generationPrompt,
  validateDiagnosis,
  validatePreprocessedCase,
};
