#!/usr/bin/env node
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import {
  auditNoSecretLeak,
  canonicalJson,
  CODEX_LUNA_MODEL,
  CODEX_LUNA_REASONING_EFFORT,
  sha256Bytes,
  sha256File,
  treeDigest,
  validateCodexLunaIdentity,
} from "./codex-luna-contract.mjs";
import {
  readCodexLunaExternalAuth,
  runCodexLunaAppServerCall,
} from "./codex-luna-app-server-runtime.mjs";
import { safeEnvironment } from "./codex-luna-exploration-runner.mjs";
import {
  assertMethodsPackageUnchanged,
  auditHttpBoundary,
  auditListedMcpTools,
  auditMcpToolCalls,
  auditModelInvocations,
  auditOracle,
  auditUploadedAttachment,
  buildMethodsProducerIdentity,
  loadScenarioFacts,
  loadScenarioOracle,
  MACOS_CODEX_LUNA_CALL_WALL_SECONDS,
  MACOS_CODEX_LUNA_NO_PROGRESS_SECONDS,
  MACOS_CODEX_LUNA_PUBLIC_TOOLS,
  MACOS_CODEX_LUNA_REGISTRATION_ID,
  mapScenarioToCreateCase,
  scenarioPaths,
  validateMethodsCache,
  writeDeterministicLogsZip,
} from "./macos-codex-luna-e2e-contract.mjs";
const MODULE_PATH = fileURLToPath(import.meta.url);
const TERMINAL_CASE_STATUSES = new Set(["RESOLVED", "PARTIALLY_RESOLVED", "UNRESOLVED", "FAILED", "CANCELLED"]);

class E2ERunnerError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "E2ERunnerError";
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) {
  throw new E2ERunnerError(code, message, details);
}

function requireE2E(condition, code, message, details = {}) {
  if (!condition) fail(code, message, details);
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function parseArguments(argv) {
  const values = {};
  const flags = new Set(["allow-posthoc-budget"]);
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    requireE2E(argument.startsWith("--"), "MACOS_CODEX_LUNA_E2E_ARGUMENT_INVALID", "Arguments must use --name value syntax");
    const name = argument.slice(2);
    requireE2E(!Object.hasOwn(values, name), "MACOS_CODEX_LUNA_E2E_ARGUMENT_DUPLICATE", "Argument is duplicated", { name });
    if (flags.has(name)) values[name] = true;
    else {
      requireE2E(index + 1 < argv.length && !argv[index + 1].startsWith("--"), "MACOS_CODEX_LUNA_E2E_ARGUMENT_MISSING", "Argument value is missing", { name });
      values[name] = argv[++index];
    }
  }
  const required = ["source-root", "codex-entry", "auth-source", "python-entry", "logparse-root", "cache-root", "scenario", "client-skill", "service-skill", "work-root", "private-root", "evidence-root", "usage-root", "run-id"];
  requireE2E(required.every((name) => typeof values[name] === "string" && values[name].length > 0) && values["allow-posthoc-budget"] === true, "MACOS_CODEX_LUNA_E2E_ARGUMENT_MISSING", "E2E runner requires all frozen inputs/roots and explicit post-hoc budget acknowledgement");
  return values;
}

function createEmptyRoot(root, label) {
  const resolved = path.resolve(root);
  if (fs.existsSync(resolved)) requireE2E(fs.statSync(resolved).isDirectory() && fs.readdirSync(resolved).length === 0, "MACOS_CODEX_LUNA_E2E_ROOT_NOT_EMPTY", `${label} must be empty`);
  else fs.mkdirSync(resolved, { recursive: true, mode: 0o700 });
  return resolved;
}

function writeJson(filePath, value, { exclusive = true } = {}) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, `${canonicalJson(value)}\n`, { encoding: "utf8", mode: 0o600, flag: exclusive ? "wx" : "w" });
}

export function createStandaloneGitBoundary(workspace) {
  const gitRoot = path.join(workspace, ".git");
  requireE2E(!fs.existsSync(gitRoot), "MACOS_CODEX_LUNA_CLIENT_GIT_COLLISION", "Client standalone Git boundary already exists");
  fs.mkdirSync(path.join(gitRoot, "objects"), { recursive: true, mode: 0o700 });
  fs.mkdirSync(path.join(gitRoot, "refs", "heads"), { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(gitRoot, "HEAD"), "ref: refs/heads/main\n", { flag: "wx", mode: 0o600 });
  fs.writeFileSync(path.join(gitRoot, "config"), "[core]\n\trepositoryformatversion = 0\n\tbare = false\n", { flag: "wx", mode: 0o600 });
}

function copyTree(source, destination) {
  requireE2E(!fs.existsSync(destination), "MACOS_CODEX_LUNA_E2E_COPY_COLLISION", "Copy destination already exists");
  fs.mkdirSync(destination, { recursive: true, mode: 0o700 });
  for (const entry of fs.readdirSync(source, { withFileTypes: true })) {
    const from = path.join(source, entry.name);
    const to = path.join(destination, entry.name);
    const metadata = fs.lstatSync(from);
    requireE2E(!metadata.isSymbolicLink(), "MACOS_CODEX_LUNA_E2E_COPY_LINK", "E2E inputs cannot contain links");
    if (entry.isDirectory()) copyTree(from, to);
    else if (entry.isFile()) fs.copyFileSync(from, to, fs.constants.COPYFILE_EXCL);
    else fail("MACOS_CODEX_LUNA_E2E_COPY_NODE", "E2E inputs contain a non-file node");
  }
}

function shellQuote(value) {
  requireE2E(typeof value === "string" && value.length > 0 && !value.includes("\0"), "MACOS_CODEX_LUNA_E2E_COMMAND_VALUE_INVALID", "Service wrapper command value is invalid");
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

async function reservePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen({ host: "127.0.0.1", port: 0, exclusive: true }, resolve);
  });
  const address = server.address();
  requireE2E(isPlainObject(address) && Number.isSafeInteger(address.port), "MACOS_CODEX_LUNA_E2E_PORT_INVALID", "Could not reserve an IPv4 loopback port");
  const port = address.port;
  await new Promise((resolve) => server.close(resolve));
  return port;
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function processClosed(child) {
  return new Promise((resolve) => child.once("close", (code, signal) => resolve({ code, signal })));
}

function terminateOwnedProcess(child) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  try { process.kill(-child.pid, "SIGTERM"); } catch {}
  setTimeout(() => {
    if (child.exitCode !== null || child.signalCode !== null) return;
    try { process.kill(-child.pid, "SIGKILL"); } catch {}
  }, 5_000).unref();
}

function probeMcp({ pythonEntry, script, mcpUrl, output }) {
  return spawnSync(pythonEntry, ["-I", "-B", script, "--url", mcpUrl, "--output", output], {
    cwd: path.dirname(script),
    env: { PATH: "/usr/bin:/bin:/usr/sbin:/sbin", LANG: "C.UTF-8", PYTHONDONTWRITEBYTECODE: "1", PYTHONNOUSERSITE: "1" },
    encoding: "utf8",
    timeout: 35_000,
    stdio: ["ignore", "pipe", "pipe"],
  });
}

async function waitForMcp(options, child, timeoutMs = 30_000) {
  const started = Date.now();
  let attempt = 0;
  while (Date.now() - started < timeoutMs) {
    requireE2E(child.exitCode === null && child.signalCode === null, "MACOS_CODEX_LUNA_SERVICE_EARLY_EXIT", "Local test service exited before MCP readiness");
    const output = `${options.output}.${attempt += 1}`;
    const result = probeMcp({ ...options, output });
    if (result.status === 0 && result.signal === null && !result.error) {
      fs.renameSync(output, options.output);
      return JSON.parse(fs.readFileSync(options.output, "utf8"));
    }
    if (fs.existsSync(output)) fs.unlinkSync(output);
    await wait(300);
  }
  fail("MACOS_CODEX_LUNA_MCP_READINESS_TIMEOUT", "MCP initialize/tools list did not become ready");
}

function parsedJsonCandidates(value, results = []) {
  if (typeof value === "string") {
    try { parsedJsonCandidates(JSON.parse(value), results); } catch {}
  } else if (Array.isArray(value)) value.forEach((item) => parsedJsonCandidates(item, results));
  else if (isPlainObject(value)) {
    results.push(value);
    Object.values(value).forEach((item) => parsedJsonCandidates(item, results));
  }
  return results;
}

export function structuredMcpData(call) {
  const candidates = parsedJsonCandidates(call?.result);
  const envelope = candidates.find((item) => item.ok === true && Object.hasOwn(item, "data"));
  requireE2E(envelope !== undefined, "MACOS_CODEX_LUNA_MCP_RESULT_INVALID", "Completed MCP call lacks a structured success envelope", { tool: call?.tool ?? null });
  return envelope.data;
}

export function partitionMcpCalls(calls) {
  requireE2E(Array.isArray(calls), "MACOS_CODEX_LUNA_MCP_RESULT_INVALID", "MCP call ledger must be an array");
  const successful = [];
  const recoveries = [];
  for (const call of calls) {
    const envelope = parsedJsonCandidates(call?.result).find((item) => typeof item.ok === "boolean" && Object.hasOwn(item, "data") && Object.hasOwn(item, "error"));
    requireE2E(envelope !== undefined, "MACOS_CODEX_LUNA_MCP_RESULT_INVALID", "Completed MCP call lacks a structured business envelope", { tool: call?.tool ?? null });
    if (envelope.ok === true) successful.push(call);
    else recoveries.push({ tool: call.tool, code: envelope.error?.code ?? null });
  }
  return { successful, recoveries };
}

export function extractCommandHttpEntries(commands) {
  requireE2E(Array.isArray(commands), "MACOS_CODEX_LUNA_COMMAND_LEDGER_INVALID", "Command ledger must be an array");
  const entries = [];
  for (const command of commands) {
    const urls = String(command.command ?? "").match(/https?:\/\/[^\s'\"]+/g) ?? [];
    const rendered = String(command.command ?? "");
    const method = /(?:--request|-X)\s+(?:'|")?PUT\b/i.test(rendered) ? "PUT" : "GET";
    for (const url of urls) entries.push({ method, url, source: `client-command:${command.item_id}` });
  }
  return entries;
}

export function validDescriptorUploadCommand({ commands, upload, archivePath }) {
  if (!Array.isArray(commands) || !isPlainObject(upload) || typeof archivePath !== "string") return false;
  const matches = commands.filter((command) => String(command.command).includes(upload.url));
  if (matches.length !== 1) return false;
  const receipt = matches[0];
  const rendered = String(receipt.command ?? "");
  const headerArgumentCount = (rendered.match(/(?:^|\s)(?:-H|--header)(?=\s)/g) ?? []).length;
  const bodyArgumentCount = (rendered.match(/(?:^|\s)(?:--upload-file|--data-binary)(?=\s)/g) ?? []).length;
  return receipt.status === "completed"
    && receipt.exit_code === 0
    && headerArgumentCount === 4
    && bodyArgumentCount === 1
    && /(?:^|\s)(?:-X|--request)\s+(?:'|")?PUT\b/.test(rendered)
    && rendered.includes(archivePath)
    && Object.entries(upload.required_headers).every(([name, value]) => rendered.includes(name) && (value === null || rendered.includes(value)));
}

export function clientPrompt({ mapped, archivePath, archive, runId }) {
  const requestPrefix = sha256Bytes(`${runId}:api-execution-overrun`).slice(0, 24);
  return `使用 $problem-locator-client 完成一次无人值守的单 Case MCP 冒烟。\n\n创建字段（逐字使用，不得改写）：\n${canonicalJson(mapped)}\n\n附件：\n- 绝对路径：${archivePath}\n- name：logs.zip\n- content_type：application/zip\n- size：${archive.size}\n- sha256：${archive.sha256}\n\n调用 prepare_attachment 前必须执行 /usr/bin/openssl dgst -sha256 与 /usr/bin/stat -f %z，从文件重新读取并逐字核对上述值；SHA 必须恰好 64 位小写十六进制。禁止凭上下文手抄或截短 SHA。\n\n稳定 request_id 前缀：${requestPrefix}。每个逻辑写操作使用不同后缀；禁止重用其他操作的 ID。\n\n必须在一个 turn 内按顺序完成：create_case → get_case 等待附件要求 → prepare_attachment → 按 UploadDescriptor 执行一次系统 curl PUT 并取得 completed/exit 0 命令回执 → 立即 get_case(wait_seconds=0) 刷新 revision → 使用该 revision 与 descriptor attachment_id 调用 submit_supplement → get_case 有限轮询至 terminal → list_artifacts。文字声称“已 PUT”不算执行，必须有真实命令回执。公开 Case 投影不会展示 attachment 内部 READY；submit 前附件要求保持 OPEN 是正常现象，禁止为等待该要求变为 FULFILLED 而轮询。prepare 与 submit 前都必须额外执行一次 wait_seconds=0 的 get_case，并逐字复制该响应的最新 case_revision。若逻辑写返回 REVISION_CONFLICT 或 ATTACHMENT_NOT_READY，刷新同一 Case后使用同一 request_id 最多纠正一次。每次 get_case 的 wait_seconds 必须是 0 到 30；已知 job_id 时用 wait_for_job_id 等待同一 Job。轮询总时限 12 分钟；禁止创建第二个 Case 或第二个 attachment，禁止调用业务 REST API，禁止读取 case.json、scenario oracle 或工作区外路径，禁止下载 artifact。`;
}

function materializeRegistration({ skillDir, cache, registrationTemplate }) {
  const registrationRoot = path.join(skillDir, MACOS_CODEX_LUNA_REGISTRATION_ID);
  fs.mkdirSync(path.join(registrationRoot, "package"), { recursive: true, mode: 0o700 });
  fs.copyFileSync(registrationTemplate, path.join(registrationRoot, "registration-template.json"), fs.constants.COPYFILE_EXCL);
  copyTree(cache.package_root, path.join(registrationRoot, "package", path.basename(cache.package_root)));
  return registrationRoot;
}

function stateEvidence(dataRoot, rawPaths) {
  const state = JSON.parse(fs.readFileSync(path.join(dataRoot, "state.json"), "utf8"));
  const aggregates = Object.values(state.cases ?? {});
  requireE2E(aggregates.length === 1, "MACOS_CODEX_LUNA_STATE_CASE_COUNT_INVALID", "Fresh DATA_ROOT must contain exactly one Case");
  const aggregate = aggregates[0];
  requireE2E(aggregate.case?.active_job_id === null && TERMINAL_CASE_STATUSES.has(aggregate.case?.status), "MACOS_CODEX_LUNA_STATE_NOT_TERMINAL", "Server state retains an active or non-terminal Case");
  const jobs = Object.values(aggregate.jobs ?? {});
  const jobOutcomes = new Map(jobs.map((job) => [job.job_id, JSON.parse(fs.readFileSync(path.join(dataRoot, "jobs", job.job_id, "job_outcome.json"), "utf8"))]));
  const { diagnose } = selectScenarioJobs(jobs, jobOutcomes);
  const jobRoot = path.join(dataRoot, "jobs", diagnose.job_id);
  const diagnosis = JSON.parse(fs.readFileSync(path.join(jobRoot, "method-diagnosis.draft.json"), "utf8"));
  const grounding = JSON.parse(fs.readFileSync(path.join(jobRoot, "method-grounding-audit.json"), "utf8"));
  const targetLogs = JSON.parse(fs.readFileSync(path.join(jobRoot, "methods_target_logs.json"), "utf8"));
  const outcome = JSON.parse(fs.readFileSync(path.join(jobRoot, "job_outcome.json"), "utf8"));
  const attachments = Object.values(aggregate.attachments ?? {});
  requireE2E(attachments.length === 1, "MACOS_CODEX_LUNA_ATTACHMENT_CARDINALITY_INVALID", "Fresh DATA_ROOT must contain exactly one attachment");
  const uploadRecords = Object.values(state.idempotency_records ?? {}).filter((record) => record?.operation === "UploadAttachmentContent");
  requireE2E(uploadRecords.length === 1, "MACOS_CODEX_LUNA_UPLOAD_RECEIPT_CARDINALITY_INVALID", "Fresh DATA_ROOT must contain exactly one upload receipt");
  requireE2E(grounding.status === diagnosis.status && canonicalJson(grounding.confirmed_methods) === canonicalJson(diagnosis.confirmed_methods), "MACOS_CODEX_LUNA_GROUNDING_AUDIT_MISMATCH", "Server-sealed diagnosis and grounding audit disagree");
  const rawByLabel = new Map([
    ["client", { file_name: "client.log", path: rawPaths.client_log }],
    ["server", { file_name: "server.log", path: rawPaths.server_log }],
  ]);
  const evidenceSources = buildScenarioEvidenceSources({
    targetLogs,
    rawByLabel,
    workspaceRoot: path.join(dataRoot, "tmp", "workspaces", diagnose.job_id),
  });
  const sourceById = new Map(evidenceSources.map((source) => [source.source_id, source]));
  const enriched = JSON.parse(JSON.stringify(diagnosis));
  for (const evidence of enriched.evidence ?? []) for (const source of evidence.sources ?? []) Object.assign(source, { file_name: sourceById.get(source.source_id)?.file_name, raw_sha256: sourceById.get(source.source_id)?.raw_sha256 });
  return { state, aggregate, jobs, diagnosis, grounding, targetLogs, outcome, evidenceSources, enriched, attachment: attachments[0], uploadReceipt: uploadRecords[0].business_receipt };
}

function textLines(file) {
  const lines = fs.readFileSync(file, "utf8").split(/\r?\n/);
  if (lines.at(-1) === "") lines.pop();
  return lines;
}

export function buildScenarioEvidenceSources({ targetLogs, rawByLabel, workspaceRoot }) {
  return targetLogs.target_logs.map((target) => {
    const raw = rawByLabel.get(target.label);
    const targetPath = path.resolve(workspaceRoot, target.log_path);
    requireE2E(raw !== undefined && targetPath.startsWith(`${path.resolve(workspaceRoot)}${path.sep}`) && fs.existsSync(targetPath), "MACOS_CODEX_LUNA_LOGPARSE_SOURCE_MISMATCH", "Logparse target source is missing or escapes its Job workspace", { source_id: target.source_id });
    requireE2E(sha256File(targetPath) === target.content_sha256 && fs.statSync(targetPath).size === target.size, "MACOS_CODEX_LUNA_LOGPARSE_TARGET_MISMATCH", "Logparse target receipt does not match its staged bytes", { source_id: target.source_id });
    const rawLines = textLines(raw.path);
    const targetLines = textLines(targetPath);
    let rawCursor = 0;
    for (const targetLine of targetLines) {
      const rawLine = targetLine.replace(/^\[[^\]]+\]\s+\[[^\]]+\]\s+/, "");
      const rawIndex = rawLines.indexOf(rawLine, rawCursor);
      requireE2E(rawIndex >= 0, "MACOS_CODEX_LUNA_LOGPARSE_SOURCE_MISMATCH", "Logparse target line cannot be traced to this run's ZIP member", { source_id: target.source_id });
      rawCursor = rawIndex + 1;
    }
    return {
      source_id: target.source_id,
      file_name: raw.file_name,
      raw_sha256: sha256File(raw.path),
      target_sha256: target.content_sha256,
      lines: targetLines,
    };
  });
}

export function selectScenarioJobs(jobs, jobOutcomes) {
  const routeJobs = jobs.filter((job) => job.job_type === "ROUTE");
  const diagnoseJobs = jobs.filter((job) => job.job_type === "DIAGNOSE");
  const reviewJobs = jobs.filter((job) => job.job_type === "REVIEW");
  const diagnoseByResult = new Map(diagnoseJobs.map((job) => [jobOutcomes.get(job.job_id)?.result_type, job]));
  requireE2E(
    jobs.length === 4
      && jobs.every((job) => job.status === "SUCCEEDED")
      && routeJobs.length === 1
      && diagnoseJobs.length === 2
      && reviewJobs.length === 1
      && diagnoseByResult.size === 2
      && diagnoseByResult.has("NEED_ATTACHMENT")
      && diagnoseByResult.has("COMPLETED"),
    "MACOS_CODEX_LUNA_SERVER_JOB_LIFECYCLE_INVALID",
    "Server lifecycle must contain one ROUTE, one attachment-request DIAGNOSE, one completed DIAGNOSE, and one REVIEW",
  );
  return {
    route: routeJobs[0],
    attachmentRequestDiagnose: diagnoseByResult.get("NEED_ATTACHMENT"),
    diagnose: diagnoseByResult.get("COMPLETED"),
    review: reviewJobs[0],
  };
}

function artifactConsistency(finalCase, artifactData) {
  const artifacts = artifactData.artifacts;
  requireE2E(Array.isArray(artifacts) && artifacts.length > 0, "MACOS_CODEX_LUNA_ARTIFACT_INDEX_EMPTY", "MCP list_artifacts returned no artifacts");
  const summaries = finalCase.artifacts ?? [];
  requireE2E(artifacts.every((artifact) => summaries.some((summary) => summary.artifact_id === artifact.artifact_id && summary.kind === artifact.kind && summary.size === artifact.size && summary.sha256 === artifact.sha256)), "MACOS_CODEX_LUNA_ARTIFACT_INDEX_MISMATCH", "MCP artifact index differs from the terminal Case projection");
  requireE2E(artifacts.filter((artifact) => artifact.kind === "USER_RESULT").length === 1, "MACOS_CODEX_LUNA_USER_RESULT_CARDINALITY_INVALID", "Terminal Case must expose exactly one USER_RESULT artifact");
  return { schema_version: 1, status: "PASS", artifact_count: artifacts.length, user_result_count: 1 };
}

function combineServerEvents(dfxRoot, destination) {
  const inputs = [path.join(dfxRoot, "debug.jsonl"), path.join(dfxRoot, "journey.jsonl")];
  const chunks = inputs.filter((input) => fs.existsSync(input)).map((input) => fs.readFileSync(input));
  requireE2E(chunks.length === 2 && chunks.every((chunk) => chunk.length > 0), "MACOS_CODEX_LUNA_SERVER_DFX_MISSING", "Server DFX streams are incomplete");
  fs.writeFileSync(destination, Buffer.concat(chunks.map((chunk) => chunk.at(-1) === 0x0a ? chunk : Buffer.concat([chunk, Buffer.from("\n")]))), { mode: 0o600, flag: "wx" });
}

export async function runE2E(options, { ambient = process.env, onProgress = null } = {}) {
  const sourceRoot = path.resolve(options.sourceRoot);
  const workRoot = createEmptyRoot(options.workRoot, "E2E work root");
  const privateRoot = createEmptyRoot(options.privateRoot, "E2E private root");
  const evidenceRoot = createEmptyRoot(options.evidenceRoot, "E2E evidence root");
  const usageRoot = createEmptyRoot(options.usageRoot, "E2E usage root");
  const rawPaths = scenarioPaths(sourceRoot, options.scenario);
  const facts = loadScenarioFacts(rawPaths.case, options.scenario);
  const mapped = mapScenarioToCreateCase(facts);
  const codexIdentity = validateCodexLunaIdentity(options.codexEntry, options.authSource);
  const metaSkillRoot = path.join(sourceRoot, ".agents", "skills", "wiki-to-diagnosis-skill");
  const releaseCaseRoot = path.join(sourceRoot, "tests", "cases", "release", "rpc-timeout-anonymized");
  const wiki = path.join(releaseCaseRoot, "input", "wiki.md");
  const registrationTemplate = path.join(releaseCaseRoot, "registration", MACOS_CODEX_LUNA_REGISTRATION_ID, "registration-template.json");
  const producer = buildMethodsProducerIdentity({ wiki, metaSkillRoot, registrationTemplate, codexIdentity });
  const cache = validateMethodsCache({ cacheRoot: options.cacheRoot, producer, registrationTemplate });
  const skillDir = path.join(workRoot, "server-skill-dir");
  fs.mkdirSync(skillDir, { mode: 0o700 });
  materializeRegistration({ skillDir, cache, registrationTemplate });
  const dataRoot = path.join(workRoot, "data-root");
  const dfxRoot = path.join(workRoot, "server-dfx");
  fs.mkdirSync(dataRoot, { mode: 0o700 });
  fs.mkdirSync(dfxRoot, { mode: 0o700 });
  const clientWorkspace = path.join(workRoot, "client");
  fs.mkdirSync(clientWorkspace, { recursive: true, mode: 0o700 });
  createStandaloneGitBoundary(clientWorkspace);
  const clientSkillRoot = path.join(clientWorkspace, ".agents", "skills", "problem-locator-client");
  fs.mkdirSync(clientSkillRoot, { recursive: true, mode: 0o700 });
  fs.copyFileSync(options.clientSkill, path.join(clientSkillRoot, "SKILL.md"), fs.constants.COPYFILE_EXCL);
  const archivePath = path.join(clientWorkspace, "input", "logs.zip");
  const archive = writeDeterministicLogsZip({ clientLog: rawPaths.client_log, serverLog: rawPaths.server_log, destination: archivePath });
  const scenarioInput = { schema_version: 1, scenario_id: options.scenario, source: { case_sha256: sha256File(rawPaths.case), client_log_sha256: sha256File(rawPaths.client_log), server_log_sha256: sha256File(rawPaths.server_log) }, mapper: mapped, archive };
  writeJson(path.join(evidenceRoot, "scenario-input.json"), scenarioInput);
  const port = await reservePort();
  const mcpUrl = `http://127.0.0.1:${port}/mcp`;
  const serverPrivateRoot = path.join(privateRoot, "server");
  const serviceEvidenceRoot = path.join(evidenceRoot, "service-runtime");
  const serviceUsageRoot = path.join(usageRoot, "server");
  fs.mkdirSync(serverPrivateRoot, { recursive: true, mode: 0o700 });
  fs.mkdirSync(serviceEvidenceRoot, { recursive: true, mode: 0o700 });
  fs.mkdirSync(serviceUsageRoot, { recursive: true, mode: 0o700 });
  const wrapper = path.join(sourceRoot, "tools", "test-flow", "runtime-support", "macos-codex-luna-service-wrapper.mjs");
  const serviceCommand = [
    process.execPath,
    wrapper,
    "--codex-entry", options.codexEntry,
    "--auth-source", options.authSource,
    "--skill-source", options.serviceSkill,
    "--private-root", serverPrivateRoot,
    "--evidence-root", serviceEvidenceRoot,
    "--usage-root", serviceUsageRoot,
    "--run-id", options.runId,
  ].map(shellQuote).join(" ");
  const serviceLog = path.join(privateRoot, "service.log");
  const serviceLogStream = fs.createWriteStream(serviceLog, { flags: "wx", mode: 0o600 });
  const serviceEnvironment = {
    PATH: `${path.dirname(options.pythonEntry)}:/usr/bin:/bin:/usr/sbin:/sbin`,
    LANG: "C.UTF-8",
    PYTHONDONTWRITEBYTECODE: "1",
    PYTHONNOUSERSITE: "1",
    TEST_FLOW_SOURCE_ROOT: sourceRoot,
    DATA_ROOT: dataRoot,
    DFX_LOG_DIR: dfxRoot,
    PUBLIC_BASE_URL: `http://127.0.0.1:${port}`,
    BIND_HOST: "127.0.0.1",
    PORT: String(port),
    SKILL_DIR: skillDir,
    GENERIC_SKILL_NAME: "generic-problem-locator-smoke",
    LOGPARSE_REPO: options.logparseRoot,
    LOGPARSE_CONFIG_PATH: path.join(sourceRoot, "experiments", "rpc-skill-feasibility", "logparse-config.json"),
    LOGPARSE_PYTHON: path.join(options.logparseRoot, ".venv", "bin", "python"),
    CLAUDE_COMMAND: serviceCommand,
  };
  const service = spawn(options.pythonEntry, ["-I", path.join(sourceRoot, "tools", "test-flow", "runtime-support", "test_service_launcher.py"), "serve"], {
    cwd: sourceRoot,
    env: serviceEnvironment,
    stdio: ["ignore", "pipe", "pipe"],
    detached: true,
  });
  service.stdout.pipe(serviceLogStream, { end: false });
  service.stderr.pipe(serviceLogStream, { end: false });
  const serviceClosed = processClosed(service);
  const forwardProgress = (chunk) => {
    const match = String(chunk).match(/TEST_FLOW_PROGRESS service-agent (ROUTE|LOGPARSE|DIAGNOSE|REVIEW)/g);
    if (match) for (const entry of match) onProgress?.(entry.split(" ").at(-1).toLowerCase());
  };
  service.stdout.on("data", forwardProgress);
  service.stderr.on("data", forwardProgress);
  let clientTrace = null;
  let readiness = null;
  try {
    readiness = await waitForMcp({ pythonEntry: options.pythonEntry, script: path.join(sourceRoot, "tools", "test-flow", "runtime-support", "macos_codex_luna_mcp_probe.py"), mcpUrl, output: path.join(privateRoot, "mcp-readiness.json") }, service);
    const listedAudit = auditListedMcpTools(readiness.tools);
    writeJson(path.join(evidenceRoot, "mcp-tools.json"), listedAudit);
    const clientPrivate = path.join(privateRoot, "client");
    const codexHome = path.join(clientPrivate, "bootstrap-codex-home");
    const home = path.join(clientPrivate, "bootstrap-home");
    const temporary = path.join(clientPrivate, "bootstrap-tmp");
    for (const directory of [clientPrivate, codexHome, home, temporary]) fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    const environment = safeEnvironment(ambient, { codexHome, home, temporary });
    const auth = readCodexLunaExternalAuth(options.authSource, ambient);
    const clientEvidence = path.join(evidenceRoot, "client-runtime");
    fs.mkdirSync(clientEvidence, { recursive: true, mode: 0o700 });
    const clientStartedAtUtc = new Date().toISOString();
    clientTrace = await runCodexLunaAppServerCall({
      codexEntry: options.codexEntry,
      auth,
      environment,
      workspaceRoot: clientWorkspace,
      skillPath: path.join(clientSkillRoot, "SKILL.md"),
      mode: "client",
      mcpServer: { name: "problem-locator", url: mcpUrl, enabled_tools: MACOS_CODEX_LUNA_PUBLIC_TOOLS, startup_timeout_sec: 15, tool_timeout_sec: 600 },
      prompt: clientPrompt({ mapped, archivePath, archive, runId: options.runId }),
      outputSchema: null,
      callRoot: path.join(clientPrivate, "call"),
      privateRoot,
      tracePath: path.join(clientEvidence, "client-events.jsonl"),
      stderrPath: path.join(clientEvidence, "client.stderr.txt"),
      finalPath: path.join(clientEvidence, "client.final.txt"),
      forbiddenReadPaths: [options.authSource, rawPaths.case],
      wallSeconds: MACOS_CODEX_LUNA_CALL_WALL_SECONDS,
      noProgressSeconds: MACOS_CODEX_LUNA_NO_PROGRESS_SECONDS,
      onProgress: () => onProgress?.("client"),
    });
    clientTrace.started_at_utc = clientStartedAtUtc;
    clientTrace.finished_at_utc = new Date().toISOString();
  } finally {
    terminateOwnedProcess(service);
    const closed = await Promise.race([serviceClosed, wait(12_000).then(() => ({ code: null, signal: "TIMEOUT" }))]);
    serviceLogStream.end();
    requireE2E(closed.signal !== "TIMEOUT", "MACOS_CODEX_LUNA_SERVICE_STOP_TIMEOUT", "Owned local test service did not stop");
  }
  requireE2E(clientTrace !== null, "MACOS_CODEX_LUNA_CLIENT_INCOMPLETE", "Codex MCP Client did not complete");
  const partitionedCalls = partitionMcpCalls(clientTrace.app_server.turn.mcp_tool_calls);
  const mcpCalls = partitionedCalls.successful;
  requireE2E(
    partitionedCalls.recoveries.length <= 2
      && partitionedCalls.recoveries.every((item) => ["REVISION_CONFLICT", "ATTACHMENT_NOT_READY"].includes(item.code)),
    "MACOS_CODEX_LUNA_MCP_BUSINESS_ERROR",
    "Client encountered a non-recoverable MCP business error",
  );
  const prepareCall = mcpCalls.find((call) => call.tool === "problem_locator_prepare_attachment");
  const prepareData = structuredMcpData(prepareCall);
  const upload = prepareData.upload;
  requireE2E(upload?.method === "PUT" && upload.attachment_id && upload.url && isPlainObject(upload.required_headers), "MACOS_CODEX_LUNA_UPLOAD_DESCRIPTOR_INVALID", "prepare_attachment did not return a valid UploadDescriptor");
  requireE2E(prepareCall.arguments.declared_size === archive.size && prepareCall.arguments.declared_sha256 === archive.sha256, "MACOS_CODEX_LUNA_ATTACHMENT_DECLARATION_MISMATCH", "Client prepare declaration differs from deterministic ZIP");
  const commandEntries = extractCommandHttpEntries(clientTrace.command_receipts);
  const uploadCommands = clientTrace.command_receipts.filter((command) => String(command.command).includes(upload.url));
  if (uploadCommands.length > 0) requireE2E(
    validDescriptorUploadCommand({ commands: uploadCommands, upload, archivePath }),
    "MACOS_CODEX_LUNA_UPLOAD_COMMAND_INVALID",
    "Attachment PUT command did not bind exactly the descriptor headers and deterministic ZIP path",
  );
  const httpAudit = auditHttpBoundary([
    { method: "POST", url: mcpUrl, source: "codex-mcp-config" },
    ...commandEntries,
    ...(uploadCommands.length === 0 ? [{ method: "PUT", url: upload.url, source: "server-ready-upload-receipt" }] : []),
  ], { mcpUrl, uploadUrl: upload.url });
  const getCalls = mcpCalls.filter((call) => call.tool === "problem_locator_get_case");
  const finalCaseData = structuredMcpData(getCalls.at(-1));
  const finalCase = finalCaseData?.case_view ?? finalCaseData;
  requireE2E(TERMINAL_CASE_STATUSES.has(finalCase.status) && finalCase.active_job === null, "MACOS_CODEX_LUNA_FINAL_CASE_INVALID", "Final MCP Case is not terminal or retains an active Job");
  const artifactData = structuredMcpData(mcpCalls.filter((call) => call.tool === "problem_locator_list_artifacts").at(-1));
  const artifactAudit = artifactConsistency(finalCase, artifactData);
  const server = stateEvidence(dataRoot, rawPaths);
  const submitCall = mcpCalls.find((call) => call.tool === "problem_locator_submit_supplement");
  const attachmentAudit = auditUploadedAttachment({ attachment: server.attachment, uploadReceipt: server.uploadReceipt, descriptor: upload, archive, submitArguments: submitCall?.arguments });
  const mcpAudit = auditMcpToolCalls(mcpCalls, { attachmentId: upload.attachment_id, uploadRevision: server.uploadReceipt.case_revision });
  mcpAudit.recoveries = partitionedCalls.recoveries;
  const oracle = loadScenarioOracle(rawPaths.case, options.scenario);
  const oracleAudit = auditOracle({ oracle, publicCase: { status: server.outcome.result_type }, sealedDiagnosis: server.enriched, evidenceSources: server.evidenceSources });
  const clientInvocation = { schema_version: 1, invocation_id: `${options.runId}:client`, phase: "CLIENT", model: CODEX_LUNA_MODEL, reasoning_effort: CODEX_LUNA_REASONING_EFFORT, attempt: 1, retry: 0, status: "PASS", terminal: true, started_at_utc: clientTrace.started_at_utc, finished_at_utc: clientTrace.finished_at_utc, wall_timeout_seconds: MACOS_CODEX_LUNA_CALL_WALL_SECONDS, thread_id: clientTrace.thread_id, turn_id: clientTrace.turn_id, usage: clientTrace.usage };
  const serverInvocations = ["route", "logparse", "diagnose", "review"].map((phase) => JSON.parse(fs.readFileSync(path.join(serviceUsageRoot, `${phase}.json`), "utf8")));
  const invocations = [clientInvocation, ...serverInvocations];
  const modelAudit = auditModelInvocations(invocations, { workflow: "e2e" });
  assertMethodsPackageUnchanged(cache);
  combineServerEvents(dfxRoot, path.join(evidenceRoot, "server-events.ndjson"));
  const attachmentReceipt = { schema_version: 1, status: "PASS", archive, descriptor: { attachment_id: upload.attachment_id, method: upload.method, url_sha256: sha256Bytes(upload.url), required_header_names: Object.keys(upload.required_headers).sort(), max_bytes: upload.max_bytes }, upload: attachmentAudit, upload_command: { item_id: uploadCommands[0].item_id, status: uploadCommands[0].status, exit_code: uploadCommands[0].exit_code }, submitted: true };
  const methodsReceipt = { schema_version: 1, status: "PASS", producer_identity: producer.producer_identity, package_tree_sha256: cache.manifest.package.tree_sha256, registration_identity: cache.manifest.registration };
  const identityReceipt = { schema_version: 1, status: "PASS", codex: codexIdentity, model: CODEX_LUNA_MODEL, reasoning_effort: CODEX_LUNA_REASONING_EFFORT, auth_kind: "chatgpt-external-tokens" };
  const lifecycle = { schema_version: 1, status: "PASS", case_id: finalCase.case_id, public_case_status: finalCase.status, jobs: server.jobs.map((job) => ({ job_id: job.job_id, job_type: job.job_type, status: job.status })), wrapper_phases: serverInvocations.map((item) => item.phase), active_jobs: 0, logparse_target_count: server.targetLogs.target_logs.length };
  const serverSealed = { schema_version: 1, diagnosis: server.diagnosis, grounding_audit: server.grounding, outcome_result_type: server.outcome.result_type, target_logs: server.targetLogs };
  writeJson(path.join(evidenceRoot, "scenario-oracle.json"), oracle);
  writeJson(path.join(evidenceRoot, "methods-package.json"), methodsReceipt);
  writeJson(path.join(evidenceRoot, "codex-identity.json"), identityReceipt);
  writeJson(path.join(evidenceRoot, "model-invocations.json"), { schema_version: 1, status: "PASS", retry_policy: "NONE", invocations });
  writeJson(path.join(evidenceRoot, "model-usage.json"), modelAudit);
  fs.copyFileSync(path.join(evidenceRoot, "client-runtime", "client-events.jsonl"), path.join(evidenceRoot, "client-events.jsonl"), fs.constants.COPYFILE_EXCL);
  writeJson(path.join(evidenceRoot, "mcp-tool-calls.json"), { ...mcpAudit, tools_listing: readiness.tools, calls: mcpCalls });
  writeJson(path.join(evidenceRoot, "attachment.json"), attachmentReceipt);
  writeJson(path.join(evidenceRoot, "server-lifecycle.json"), lifecycle);
  writeJson(path.join(evidenceRoot, "server-sealed-diagnosis.json"), serverSealed);
  writeJson(path.join(evidenceRoot, "final-case.json"), finalCase);
  writeJson(path.join(evidenceRoot, "artifact-index.json"), artifactData);
  writeJson(path.join(evidenceRoot, "http-boundary-audit.json"), httpAudit);
  const auth = readCodexLunaExternalAuth(options.authSource, ambient);
  const secretScan = auditNoSecretLeak({ roots: [evidenceRoot], canaries: auth.canaries });
  const security = { schema_version: 1, status: "PASS", secret_scan: secretScan, auth_files_persisted: 0, oracle_visible_before_models: false, unexpected_network_targets: 0, package_drift: false };
  writeJson(path.join(evidenceRoot, "security-audit.json"), security);
  const gate = { schema_version: 1, status: "PASS", scenario_id: options.scenario, checks: { mcp: mcpAudit.status, attachment: attachmentReceipt.status, server_lifecycle: lifecycle.status, artifacts: artifactAudit.status, http_boundary: httpAudit.status, oracle: oracleAudit.status, model_usage: modelAudit.status, security: security.status }, evidence_sha256: Object.fromEntries(fs.readdirSync(evidenceRoot).filter((name) => fs.statSync(path.join(evidenceRoot, name)).isFile()).sort().map((name) => [name, sha256File(path.join(evidenceRoot, name))])) };
  writeJson(path.join(evidenceRoot, "adapter-receipt.json"), gate);
  return gate;
}

async function main() {
  try {
    const values = parseArguments(process.argv.slice(2));
    const result = await runE2E({
      runId: values["run-id"],
      sourceRoot: path.resolve(values["source-root"]),
      codexEntry: path.resolve(values["codex-entry"]),
      authSource: path.resolve(values["auth-source"]),
      pythonEntry: path.resolve(values["python-entry"]),
      logparseRoot: path.resolve(values["logparse-root"]),
      cacheRoot: path.resolve(values["cache-root"]),
      scenario: values.scenario,
      clientSkill: path.resolve(values["client-skill"]),
      serviceSkill: path.resolve(values["service-skill"]),
      workRoot: path.resolve(values["work-root"]),
      privateRoot: path.resolve(values["private-root"]),
      evidenceRoot: path.resolve(values["evidence-root"]),
      usageRoot: path.resolve(values["usage-root"]),
    });
    process.stdout.write(`${canonicalJson(result)}\n`);
  } catch (error) {
    process.stderr.write(`${canonicalJson({ schema_version: 1, status: "FAIL", code: error?.code ?? "MACOS_CODEX_LUNA_E2E_RUNNER_FAILED", message: error?.message ?? String(error) })}\n`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === MODULE_PATH) await main();

export { parseArguments, stateEvidence };
