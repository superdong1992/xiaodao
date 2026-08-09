#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { spawn } from "node:child_process";
import {
  RELEASE_CLAUDE_VERSION_OUTPUT,
  RELEASE_LOGPARSE_COMMIT,
  RELEASE_MCP_COMMIT,
  RELEASE_MODEL,
  claudeSettingsIdentity,
  materializeAttemptClaudeSettings,
  materializeClaudeSettings,
  packageTreeIdentity,
  validateClaudeDistribution,
} from "../lib/release-inputs.mjs";
import { readServerMcpCorrespondence } from "../lib/events.mjs";
import { recoverStageAuditProgress } from "../lib/evidence.mjs";
import { canonicalJson, ensureDirectory, sha256Bytes, sha256File } from "../lib/util.mjs";

const ZIP_NAME = "synthetic-rpc-service-takeover.zip";
const ZIP_SIZE = 2367;
const ZIP_SHA256 = "194f69fecd8dc8d40d1aedeb6fc25d2b7b4922b176be2b15be73ffe386cc5064";
const MAX_ATTACHMENT_BYTES = 2684354560;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const TOOL_NAMES = Object.freeze([
  "problem_locator_create_case",
  "problem_locator_prepare_attachment",
  "problem_locator_submit_supplement",
  "problem_locator_get_case",
  "problem_locator_resume_case",
  "problem_locator_cancel_case",
  "problem_locator_list_artifacts",
]);
const FULL_TOOL_NAMES = Object.freeze(TOOL_NAMES.map((name) => `mcp__problem-locator__${name}`));
const INSTANCE_ORDER = Object.freeze(["route", "upload", "diagnose", "restart"]);

class StageError extends Error {
  constructor(code, status = "ERROR", domain = "HARNESS") {
    super(code);
    this.code = code;
    this.status = status;
    this.domain = domain;
  }
}

function requireCondition(condition, code, status = "ERROR", domain = "HARNESS") {
  if (!condition) throw new StageError(code, status, domain);
}

function parseArguments(argv) {
  const values = {};
  const flags = new Set();
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index];
    if (["--fresh-data-root"].includes(name)) {
      flags.add(name);
      continue;
    }
    requireCondition(name.startsWith("--") && index + 1 < argv.length, "ADAPTER_ARGUMENT_INVALID");
    values[name.slice(2).replaceAll("-", "_")] = argv[++index];
  }
  return { values, flags };
}

function writeNew(filePath, value) {
  ensureDirectory(path.dirname(filePath));
  const descriptor = fs.openSync(filePath, "wx", 0o600);
  try {
    fs.writeFileSync(descriptor, canonicalJson(value), "utf8");
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

function writeTextNew(filePath, value) {
  ensureDirectory(path.dirname(filePath));
  const descriptor = fs.openSync(filePath, "wx", 0o600);
  try {
    fs.writeFileSync(descriptor, value, "utf8");
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

function atomicState(filePath, value) {
  ensureDirectory(path.dirname(filePath));
  const temporary = `${filePath}.${process.pid}.${crypto.randomUUID()}.tmp`;
  fs.writeFileSync(temporary, canonicalJson(value), { encoding: "utf8", mode: 0o600, flag: "wx" });
  fs.renameSync(temporary, filePath);
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function safeName(prefix, runId, suffix = "") {
  const digest = sha256Bytes(`${runId}:${suffix}`).slice(0, 16);
  return `${prefix}-${digest}${suffix ? `-${suffix}` : ""}`;
}

function currentReleaseRuntimeIdentity(configuration) {
  const distribution = validateClaudeDistribution(configuration.claudeEntry);
  requireCondition(distribution.status === "PRESENT", `CLAUDE_DISTRIBUTION_${distribution.code ?? "INVALID"}`, "BLOCKED", "INFRA");
  const settings = claudeSettingsIdentity(configuration.claudeSettings);
  requireCondition(settings.status === "PRESENT", `CLAUDE_SETTINGS_${settings.code ?? "INVALID"}`, "BLOCKED", "INFRA");
  return {
    schema_version: 1,
    claude: {
      entry: distribution.entry,
      version: distribution.version,
      cli_sha256: distribution.cli_sha256,
      package_manifest_sha256: distribution.package_manifest_sha256,
      package_tree_digest: distribution.package_tree_digest,
      tarball_sha256: distribution.tarball_sha256,
      node_version: distribution.node?.version ?? null,
      node_sha256: distribution.node?.sha256 ?? null,
    },
    settings: {
      endpoint: settings.endpoint,
      model: settings.model,
      fingerprint: settings.fingerprint,
      hooks_copied: settings.hooks_copied,
    },
  };
}

async function verifyRepositoryIdentity(configuration) {
  const head = await run("git", ["-C", configuration.repoRoot, "rev-parse", "HEAD"], { forward: false });
  const status = await run("git", ["-C", configuration.repoRoot, "status", "--porcelain=v1", "--untracked-files=all"], { forward: false });
  requireCondition(head.status === 0 && head.stdout.trim() === configuration.sourceCommit, "RELEASE_SOURCE_COMMIT_DRIFT", "BLOCKED", "INFRA");
  requireCondition(status.status === 0 && status.stdout.length === 0, "RELEASE_SOURCE_TREE_DRIFT", "BLOCKED", "INFRA");
}

async function run(command, args, { cwd = undefined, env = process.env, forward = true, maximumBytes = 64 * 1024 * 1024 } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, env, stdio: ["ignore", "pipe", "pipe"] });
    const stdout = [];
    const stderr = [];
    let bytes = 0;
    const consume = (collection, target, chunk) => {
      bytes += chunk.length;
      if (bytes > maximumBytes) {
        child.kill("SIGKILL");
        reject(new StageError("ADAPTER_COMMAND_OUTPUT_LIMIT"));
        return;
      }
      collection.push(chunk);
      if (forward) target.write(chunk);
    };
    child.stdout.on("data", (chunk) => consume(stdout, process.stdout, chunk));
    child.stderr.on("data", (chunk) => consume(stderr, process.stderr, chunk));
    child.once("error", reject);
    child.once("exit", (code, signal) => resolve({
      status: code,
      signal,
      stdout: Buffer.concat(stdout).toString("utf8"),
      stderr: Buffer.concat(stderr).toString("utf8"),
    }));
  });
}

function dockerArgs(context, args) {
  return ["--context", context, ...args];
}

async function docker(context, args, options = {}) {
  const result = await run("docker", dockerArgs(context, args), options);
  if (result.status !== 0) throw new StageError(`DOCKER_COMMAND_FAILED:${args[0]}`, "BLOCKED", "INFRA");
  return result;
}

function appendResource(registryPath, attemptRoot, kind, name, label) {
  const registry = path.resolve(registryPath);
  const root = path.resolve(attemptRoot);
  requireCondition(registry.startsWith(`${root}${path.sep}`), "RESOURCE_REGISTRY_OUTSIDE_ATTEMPT");
  requireCondition(["container", "volume"].includes(kind) && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(name), "RESOURCE_RECORD_INVALID");
  const existing = fs.existsSync(registry)
    ? fs.readFileSync(registry, "utf8").split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line))
    : [];
  requireCondition(!existing.some((entry) => entry.kind === kind && entry.name === name), "RESOURCE_RECORD_DUPLICATE");
  ensureDirectory(path.dirname(registry));
  fs.appendFileSync(registry, `${JSON.stringify({ schema_version: 1, kind, name, label })}\n`, { encoding: "utf8", mode: 0o600 });
}

async function availablePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once("error", reject);
    server.listen({ host: "127.0.0.1", port: 0 }, () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : null;
      server.close((error) => error ? reject(error) : resolve(port));
    });
  });
}

async function waitReady(publicBaseUrl) {
  const deadline = Date.now() + 90_000;
  let probes = 0;
  while (Date.now() < deadline) {
    probes += 1;
    try {
      const [live, ready] = await Promise.all([
        fetch(`${publicBaseUrl}/live`, { signal: AbortSignal.timeout(3000) }),
        fetch(`${publicBaseUrl}/ready`, { signal: AbortSignal.timeout(3000) }),
      ]);
      if (live.status === 200 && ready.status === 200) {
        const liveBody = await live.json();
        const readyBody = await ready.json();
        requireCondition(liveBody?.ok === true && readyBody?.ok === true && readyBody?.data?.ready === true, "SERVICE_READINESS_BODY", "FAIL", "PRODUCT");
        process.stdout.write("TEST_FLOW_PROGRESS request.completed\n");
        return { probes, live: true, ready: true };
      }
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new StageError("SERVICE_READINESS_TIMEOUT", "BLOCKED", "INFRA");
}

async function captureReadinessDiagnostic(configuration, state, instance, code) {
  const pidProbe = await run("docker", dockerArgs(configuration.dockerContext, [
    "exec", state.active_container,
    "sh", "-c",
    'pid_file="/tmp/test-flow-service-$1/service.pid"; test -s "$pid_file"; pid=$(cat "$pid_file"); kill -0 "$pid"',
    "test-flow-readiness", instance,
  ]), { forward: false });
  const tcpProbe = await run("docker", dockerArgs(configuration.dockerContext, [
    "exec", state.active_container,
    "/opt/venvs/xiaodao/bin/python", "-I", "-c",
    'import socket; s=socket.socket(); s.settimeout(2); print("OPEN" if s.connect_ex(("127.0.0.1", 8000)) == 0 else "CLOSED"); s.close()',
  ]), { forward: false });
  const topProbe = await run("docker", dockerArgs(configuration.dockerContext, [
    "top", state.active_container, "-eo", "args",
  ]), { forward: false });
  const supervisorLog = path.join(configuration.stageRoot, `supervisor-${instance}.log`);
  const serviceLog = path.join(configuration.attemptRoot, "payload", "logs", `service-${instance}.log`);
  writeNew(path.join(configuration.stageRoot, `readiness-${instance}.json`), {
    schema_version: 1,
    status: "FAIL",
    code,
    service_pid_running: pidProbe.status === 0,
    internal_tcp_8000: tcpProbe.status === 0 && tcpProbe.stdout.trim() === "OPEN",
    launcher_process_present: topProbe.status === 0 && topProbe.stdout.includes("/harness/test_service_launcher.py"),
    journey_relay_present: topProbe.status === 0 && topProbe.stdout.includes("relay_service_journey.py"),
    supervisor_log_bytes: fs.existsSync(supervisorLog) ? fs.statSync(supervisorLog).size : null,
    service_log_bytes: fs.existsSync(serviceLog) ? fs.statSync(serviceLog).size : null,
  });
}

function copyTreeNoSymlinks(source, destination) {
  const metadata = fs.lstatSync(source);
  requireCondition(!metadata.isSymbolicLink(), "CLIENT_SKILL_SYMLINK_FORBIDDEN");
  if (metadata.isDirectory()) {
    fs.mkdirSync(destination, { recursive: false, mode: 0o700 });
    for (const name of fs.readdirSync(source).sort()) copyTreeNoSymlinks(path.join(source, name), path.join(destination, name));
    return;
  }
  requireCondition(metadata.isFile() && metadata.nlink === 1, "CLIENT_SKILL_FILE_INVALID");
  fs.copyFileSync(source, destination, fs.constants.COPYFILE_EXCL);
  fs.chmodSync(destination, 0o600);
}

function ensureClientRuntime(configuration, state) {
  const runtimeRoot = path.join(configuration.attemptRoot, "scratch", "macos-client");
  const configRoot = path.join(runtimeRoot, "config");
  const settingsPath = path.join(runtimeRoot, "settings.json");
  const mcpPath = path.join(runtimeRoot, "mcp.json");
  const home = path.join(runtimeRoot, "home");
  ensureDirectory(runtimeRoot);
  ensureDirectory(home);
  if (!fs.existsSync(configRoot)) {
    fs.mkdirSync(configRoot, { mode: 0o700 });
    fs.mkdirSync(path.join(configRoot, "skills"), { mode: 0o700 });
    copyTreeNoSymlinks(
      path.join(configuration.repoRoot, ".claude", "skills", "problem-locator-client"),
      path.join(configRoot, "skills", "problem-locator-client"),
    );
  }
  if (!fs.existsSync(settingsPath)) materializeClaudeSettings(configuration.containerClaudeSettings, settingsPath);
  const frozenSettings = claudeSettingsIdentity(settingsPath);
  requireCondition(frozenSettings.status === "PRESENT" && frozenSettings.fingerprint === state.runtime_identity.settings.fingerprint, "CLIENT_SETTINGS_IDENTITY_DRIFT", "BLOCKED", "INFRA");
  const clientSkill = packageTreeIdentity(path.join(configRoot, "skills", "problem-locator-client"));
  requireCondition(clientSkill.status === "PRESENT", "CLIENT_SKILL_COPY_INVALID");
  if (state.client_skill_digest) requireCondition(state.client_skill_digest === clientSkill.digest, "CLIENT_SKILL_COPY_DRIFT");
  else state.client_skill_digest = clientSkill.digest;
  const expectedMcp = { mcpServers: { "problem-locator": { type: "http", url: `${state.public_base_url}/mcp`, alwaysLoad: true } } };
  if (!fs.existsSync(mcpPath)) writeNew(mcpPath, expectedMcp);
  else requireCondition(canonicalJson(readJson(mcpPath)) === canonicalJson(expectedMcp), "CLIENT_MCP_CONFIG_DRIFT");
  return { runtimeRoot, configRoot, settingsPath, mcpPath, home };
}

function normalizedToolName(fullName) {
  if (TOOL_NAMES.includes(fullName)) return fullName;
  return TOOL_NAMES.find((name) => fullName.endsWith(name)) ?? null;
}

function assertFlatInput(input) {
  requireCondition(input && typeof input === "object" && !Array.isArray(input), "CLIENT_TOOL_INPUT_NOT_OBJECT", "FAIL", "CONTRACT");
  for (const value of Object.values(input)) {
    if ([null, "string", "number", "boolean"].includes(value === null ? null : typeof value)) continue;
    requireCondition(Array.isArray(value) && value.every((item) => item === null || ["string", "number", "boolean"].includes(typeof item)), "CLIENT_TOOL_INPUT_NOT_FLAT", "FAIL", "CONTRACT");
  }
}

function parseClaudeStream(text, expectedCwd) {
  const events = text.split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
  const initEvents = events.filter((event) => event.type === "system" && event.subtype === "init");
  const results = events.filter((event) => event.type === "result");
  requireCondition(initEvents.length === 1 && results.length === 1 && events.at(-1)?.type === "result", "CLIENT_STREAM_TERMINAL_INVALID");
  const terminal = results[0];
  requireCondition(terminal.subtype === "success" && terminal.is_error === false, "CLIENT_RESULT_NOT_SUCCESS", "INCONCLUSIVE", "EXTERNAL");
  const init = initEvents[0];
  requireCondition(path.resolve(init.cwd) === path.resolve(expectedCwd), "CLIENT_CWD_MISMATCH");
  requireCondition(init.model === RELEASE_MODEL, "CLIENT_EFFECTIVE_MODEL_MISMATCH", "BLOCKED", "INFRA");
  requireCondition(init.permissionMode === "dontAsk", "CLIENT_PERMISSION_MODE_MISMATCH");
  const reported = new Set((init.tools ?? []).map(normalizedToolName).filter(Boolean));
  requireCondition((init.tools ?? []).includes("Skill") && TOOL_NAMES.every((name) => reported.has(name)), "CLIENT_TOOL_INVENTORY_MISMATCH");
  const servers = init.mcp_servers ?? [];
  requireCondition(servers.length === 1 && (servers[0].name ?? servers[0].serverName) === "problem-locator" && (!servers[0].status || servers[0].status === "connected"), "CLIENT_STRICT_MCP_NOT_CONNECTED", "BLOCKED", "INFRA");

  const byId = new Map();
  const records = [];
  for (const event of events) {
    const content = event.message?.content;
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      if (block?.type === "tool_use") {
        requireCondition(event.type === "assistant" && event.message?.role === "assistant", "CLIENT_TOOL_USE_ROLE");
        requireCondition(typeof block.id === "string" && !byId.has(block.id), "CLIENT_TOOL_USE_ID");
        const toolName = block.name === "Skill" ? "Skill" : normalizedToolName(block.name);
        requireCondition(toolName !== null, "CLIENT_UNEXPECTED_TOOL", "FAIL", "CONTRACT");
        if (toolName !== "Skill") assertFlatInput(block.input);
        const record = { ordinal: records.length, tool_use_id: block.id, full_name: block.name, tool_name: toolName, input: block.input, result: null };
        records.push(record);
        byId.set(block.id, record);
      }
      if (block?.type === "tool_result") {
        requireCondition(event.type === "user" && event.message?.role === "user", "CLIENT_TOOL_RESULT_ROLE");
        const record = byId.get(block.tool_use_id);
        requireCondition(record && record.result === null && block.is_error !== true, "CLIENT_TOOL_RESULT_ID");
        if (record.tool_name === "Skill") record.result = { skill_loaded: true };
        else {
          const raw = event.tool_use_result;
          requireCondition(raw && typeof raw === "object" && !event.toolUseResult && raw.isError !== true && raw.is_error !== true, "CLIENT_TOOL_RESULT_ENVELOPE");
          requireCondition(raw.structuredContent && typeof raw.structuredContent === "object" && !Array.isArray(raw.structuredContent), "CLIENT_TOOL_RESULT_STRUCTURED_CONTENT");
          record.result = raw.structuredContent;
        }
      }
    }
  }
  requireCondition(records.every((record) => record.result !== null), "CLIENT_TOOL_RESULT_MISSING");
  const skills = records.filter((record) => record.tool_name === "Skill");
  requireCondition(skills.length === 1 && skills[0].ordinal === 0 && skills[0].input?.skill === "problem-locator-client", "CLIENT_SKILL_INVOCATION_INVALID", "FAIL", "CONTRACT");
  const mcpRecords = records.filter((record) => record.tool_name !== "Skill");
  requireCondition(mcpRecords.length > 0, "CLIENT_MCP_CALL_MISSING", "FAIL", "CONTRACT");
  return {
    schema_version: 1,
    init: { cwd: init.cwd, effective_model: init.model, permission_mode: init.permissionMode, tools: init.tools, mcp_servers: servers },
    records: mcpRecords,
    usage: {
      input_tokens: Number(terminal.usage?.input_tokens ?? 0),
      output_tokens: Number(terminal.usage?.output_tokens ?? 0),
      cost_usd: Number(terminal.total_cost_usd ?? terminal.cost_usd ?? 0),
    },
    terminal: { subtype: terminal.subtype, is_error: terminal.is_error },
  };
}

async function runClaude(configuration, state, stageRoot, phase, prompt, maxTurns, maxBudgetUsd) {
  const runtime = ensureClientRuntime(configuration, state);
  const promptPath = path.join(stageRoot, `${phase}.prompt.txt`);
  const stdoutPath = path.join(stageRoot, `${phase}.stream-json.stdout.ndjson`);
  const stderrPath = path.join(stageRoot, `${phase}.stderr.txt`);
  const auditPath = path.join(stageRoot, `${phase}.authoritative.json`);
  writeTextNew(promptPath, `${prompt}\n`);
  const stdoutDescriptor = fs.openSync(stdoutPath, "wx", 0o600);
  const stderrDescriptor = fs.openSync(stderrPath, "wx", 0o600);
  const environment = { ...process.env };
  for (const name of Object.keys(environment)) {
    if (/^(?:ANTHROPIC_|CLAUDE_MODEL$|HTTP_PROXY$|HTTPS_PROXY$|ALL_PROXY$|http_proxy$|https_proxy$|all_proxy$)/.test(name)) delete environment[name];
  }
  environment.HOME = runtime.home;
  environment.CLAUDE_CONFIG_DIR = runtime.configRoot;
  environment.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1";
  environment.NO_PROXY = "127.0.0.1,localhost";
  environment.no_proxy = environment.NO_PROXY;
  const args = [
    configuration.claudeEntry,
    "--print",
    "--output-format", "stream-json",
    "--verbose",
    "--model", "sonnet",
    "--max-turns", String(maxTurns),
    "--max-budget-usd", String(maxBudgetUsd),
    "--setting-sources", "user",
    "--settings", runtime.settingsPath,
    "--mcp-config", runtime.mcpPath,
    "--strict-mcp-config",
    "--tools=Skill",
    "--allowedTools", "Skill(problem-locator-client)",
    ...FULL_TOOL_NAMES,
    "--permission-mode", "dontAsk",
    "--no-chrome",
    "--no-session-persistence",
    prompt,
  ];
  const chunks = [];
  const stderrChunks = [];
  const exit = await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, args, { cwd: configuration.repoRoot, env: environment, stdio: ["ignore", "pipe", "pipe"] });
    child.stdout.on("data", (chunk) => {
      chunks.push(chunk);
      fs.writeSync(stdoutDescriptor, chunk);
      process.stdout.write(chunk);
    });
    child.stderr.on("data", (chunk) => {
      stderrChunks.push(chunk);
      fs.writeSync(stderrDescriptor, chunk);
      process.stderr.write(chunk);
    });
    child.once("error", reject);
    child.once("exit", (code, signal) => resolve({ code, signal }));
  });
  fs.fsyncSync(stdoutDescriptor);
  fs.fsyncSync(stderrDescriptor);
  fs.closeSync(stdoutDescriptor);
  fs.closeSync(stderrDescriptor);
  if (exit.code !== 0) throw new StageError(`CLAUDE_${phase.toUpperCase()}_EXIT_${exit.code ?? exit.signal}`, "INCONCLUSIVE", "EXTERNAL");
  const audit = parseClaudeStream(Buffer.concat(chunks).toString("utf8"), configuration.repoRoot);
  writeNew(auditPath, audit);
  const forbidden = [
    path.join(runtime.runtimeRoot, "client-dfx.jsonl"),
    path.join(runtime.runtimeRoot, ".problem-locator", "client-dfx.jsonl"),
    path.join(stageRoot, `${phase}.client-dfx.jsonl`),
  ];
  requireCondition(forbidden.every((filePath) => !fs.existsSync(filePath)), "CLIENT_DFX_FORBIDDEN");
  return audit;
}

function successData(record) {
  requireCondition(record.result && record.result.ok === true && record.result.error === null && record.result.data && typeof record.result.data === "object", `MCP_${record.tool_name}_FAILED`, "FAIL", "CONTRACT");
  return record.result.data;
}

function applicationResponse(record) {
  const data = successData(record);
  return record.tool_name === "problem_locator_prepare_attachment" ? data.application_response : data;
}

function caseView(record) {
  return applicationResponse(record)?.case_view ?? null;
}

function exactKeys(value, expected, code) {
  requireCondition(value && typeof value === "object" && !Array.isArray(value), code, "FAIL", "CONTRACT");
  requireCondition(canonicalJson(Object.keys(value).sort()) === canonicalJson([...expected].sort()), code, "FAIL", "CONTRACT");
}

function phaseOnePrompt(requestIds) {
  return `Perform phase 1 of the controlled Problem Locator acceptance journey. Use only the Skill tool and the seven problem_locator Remote MCP tools. Treat MCP tool_result structured payloads as authoritative; do not infer state from prose.

0. Your first action MUST call the Skill tool with skill=problem-locator-client (exact input {"skill":"problem-locator-client"}). Until that Skill tool_result is received successfully, do not call any problem_locator MCP tool.
1. Call problem_locator_create_case exactly once with request_id "${requestIds.create}". Put statement "A checkout-to-inventory ReserveStock RPC times out during a service takeover.", expected_behavior "The checkout operation completes after inventory reservation.", actual_behavior "During an active service takeover, the ReserveStock RPC times out and checkout does not complete.", scope "checkout-to-inventory service-takeover RPC diagnosis", goals ["Locate the service-takeover timeout cause using the supplied logs."], non_goals ["Modify production systems."], constraints ["Use only evidence persisted in this diagnosis case."], completion_criteria ["Identify the timed-out request and an evidence-backed root cause."] directly at the root. Do not send problem_spec. Set initial_user_fact_names [], initial_user_fact_values [], and wait_seconds 0.
2. Poll problem_locator_get_case with non-empty case_id input until WAITING_INPUT has exactly OPEN INPUT requirements caller_service, server_service, rpc_method, problem_time.
3. Call problem_locator_submit_supplement once with request_id "${requestIds.submit_a}", the latest case_revision, input_names ["caller_service","server_service","rpc_method","problem_time"], input_values ["checkout-synthetic","inventory-synthetic","ReserveStock","2026-07-31T00:00:03.000Z"], attachment_ids [], wait_seconds 0.
4. Poll with non-empty case_id input until WAITING_ATTACHMENT has the OPEN ATTACHMENT requirement log_archive.
5. Call problem_locator_prepare_attachment exactly once with request_id "${requestIds.prepare}", the latest revision, name "${ZIP_NAME}", content_type "application/zip", declared_size ${ZIP_SIZE}, and declared_sha256 "${ZIP_SHA256}". The call must contain exactly those seven required root properties and must never send nested input.
6. Stop immediately after the successful prepare result. Do not upload, submit the attachment, or call another tool.`;
}

function validatePhaseOne(audit, requestIds, publicBaseUrl) {
  const records = audit.records;
  const successful = records.filter((record) => record.result?.ok === true);
  const create = successful.filter((record) => record.tool_name === "problem_locator_create_case");
  const submit = successful.filter((record) => record.tool_name === "problem_locator_submit_supplement");
  const prepare = successful.filter((record) => record.tool_name === "problem_locator_prepare_attachment");
  requireCondition(create.length === 1 && submit.length === 1 && prepare.length === 1, "PHASE1_CALL_CARDINALITY", "FAIL", "CONTRACT");
  requireCondition(records.at(-1) === prepare[0], "PHASE1_PREPARE_NOT_TERMINAL", "FAIL", "CONTRACT");
  exactKeys(create[0].input, ["request_id", "statement", "expected_behavior", "actual_behavior", "scope", "goals", "non_goals", "constraints", "completion_criteria", "initial_user_fact_names", "initial_user_fact_values", "wait_seconds"], "PHASE1_CREATE_INPUT_SHAPE");
  requireCondition(create[0].input.request_id === requestIds.create && create[0].input.non_goals?.[0] === "Modify production systems." && !Object.hasOwn(create[0].input, "problem_spec"), "PHASE1_CREATE_INPUT", "FAIL", "CONTRACT");
  exactKeys(submit[0].input, ["request_id", "case_id", "expected_case_revision", "input_names", "input_values", "attachment_ids", "wait_seconds"], "PHASE1_SUBMIT_INPUT_SHAPE");
  requireCondition(submit[0].input.request_id === requestIds.submit_a && canonicalJson(submit[0].input.input_names) === canonicalJson(["caller_service", "server_service", "rpc_method", "problem_time"]), "PHASE1_SUBMIT_INPUT", "FAIL", "CONTRACT");
  exactKeys(prepare[0].input, ["request_id", "case_id", "expected_case_revision", "name", "content_type", "declared_size", "declared_sha256"], "PHASE1_PREPARE_INPUT_SHAPE");
  requireCondition(prepare[0].input.request_id === requestIds.prepare && prepare[0].input.name === ZIP_NAME && prepare[0].input.declared_size === ZIP_SIZE && prepare[0].input.declared_sha256 === ZIP_SHA256, "PHASE1_PREPARE_INPUT", "FAIL", "CONTRACT");
  const prepareData = successData(prepare[0]);
  const response = prepareData.application_response;
  const view = response?.case_view;
  const descriptor = prepareData.upload;
  requireCondition(view?.status === "WAITING_ATTACHMENT" && UUID.test(view.case_id) && Number.isInteger(view.case_revision), "PHASE1_CASE_VIEW", "FAIL", "CONTRACT");
  exactKeys(descriptor, ["attachment_id", "method", "url", "required_headers", "max_bytes", "expires_at"], "PHASE1_UPLOAD_DESCRIPTOR_SHAPE");
  requireCondition(UUID.test(descriptor.attachment_id) && descriptor.method === "PUT" && descriptor.url === `${publicBaseUrl}/api/v1/attachments/${descriptor.attachment_id}/content` && descriptor.max_bytes === MAX_ATTACHMENT_BYTES && descriptor.expires_at === null, "PHASE1_UPLOAD_DESCRIPTOR", "FAIL", "CONTRACT");
  exactKeys(descriptor.required_headers, ["Content-Length", "Content-Type", "Idempotency-Key", "X-Content-SHA256"], "PHASE1_UPLOAD_HEADERS_SHAPE");
  requireCondition(descriptor.required_headers["Content-Length"] === String(ZIP_SIZE) && descriptor.required_headers["Content-Type"] === "application/zip" && descriptor.required_headers["Idempotency-Key"] === descriptor.attachment_id && descriptor.required_headers["X-Content-SHA256"] === ZIP_SHA256, "PHASE1_UPLOAD_HEADERS", "FAIL", "CONTRACT");
  requireCondition(successful.filter((record) => record.tool_name === "problem_locator_get_case").some((record) => caseView(record)?.status === "WAITING_INPUT"), "PHASE1_WAITING_INPUT_NOT_OBSERVED", "FAIL", "CONTRACT");
  return {
    case_id: view.case_id,
    attachment_id: descriptor.attachment_id,
    prepared_case_revision: view.case_revision,
    selected_skill_ref: view.selected_skill_ref,
    upload_descriptor: descriptor,
  };
}

async function uploadAttachment(configuration, state, stageRoot) {
  const descriptor = state.upload_descriptor;
  const archive = path.join(configuration.attemptRoot, "payload", ZIP_NAME);
  requireCondition(fs.existsSync(archive) && fs.statSync(archive).size === ZIP_SIZE && sha256File(archive) === ZIP_SHA256, "UPLOAD_FIXTURE_INVALID");
  requireCondition(descriptor.url === `${state.public_base_url}/api/v1/attachments/${state.attachment_id}/content`, "UPLOAD_URL_INVALID");
  const response = await fetch(descriptor.url, {
    method: "PUT",
    headers: descriptor.required_headers,
    body: fs.readFileSync(archive),
    signal: AbortSignal.timeout(135_000),
  });
  const text = await response.text();
  writeTextNew(path.join(stageRoot, "upload.response.json"), `${text.trim()}\n`);
  requireCondition(response.status === 200, `UPLOAD_HTTP_${response.status}`, "FAIL", "PRODUCT");
  const envelope = JSON.parse(text);
  const data = envelope?.data;
  requireCondition(envelope?.ok === true && envelope.error === null && data?.case_id === state.case_id && data?.attachment_id === state.attachment_id && data?.status === "READY" && Number.isInteger(data.case_revision) && data.case_revision > state.prepared_case_revision, "UPLOAD_RESPONSE_INVALID", "FAIL", "CONTRACT");
  process.stdout.write("TEST_FLOW_PROGRESS attachment.upload.completed\n");
  return { status: "READY", case_revision: data.case_revision };
}

function phaseThreePrompt(state) {
  return `Perform phase 3 of the controlled Problem Locator journey. Use only the Skill tool and the seven problem_locator Remote MCP tools. Treat structured MCP results as authoritative. Case ${state.case_id}, attachment ${state.attachment_id}, and case_revision ${state.case_revision} are authoritative.

0. First call Skill with exact input {"skill":"problem-locator-client"}; do not call MCP before it succeeds.
1. Call problem_locator_submit_supplement exactly once with request_id "${state.request_ids.submit_attachment}", case_id "${state.case_id}", expected_case_revision ${state.case_revision}, input_names [], input_values [], attachment_ids ["${state.attachment_id}"], wait_seconds 0.
2. Poll problem_locator_get_case with non-empty case_id until WAITING_INPUT has exactly one OPEN INPUT requirement order_id.
3. Call problem_locator_submit_supplement exactly once with request_id "${state.request_ids.submit_order}", latest revision, input_names ["order_id"], input_values ["synthetic-order-0001"], attachment_ids [], wait_seconds 0.
4. Poll promptly. Observe REVIEWING, then continue with the authoritative active review job id until RESOLVED with final_result.status ACCEPTED. Do not skip REVIEWING.
5. Call problem_locator_list_artifacts exactly once for this Case and stop. Do not call another tool.`;
}

function validatePhaseThree(audit, state) {
  const records = audit.records;
  const successful = records.filter((record) => record.result?.ok === true);
  const submits = successful.filter((record) => record.tool_name === "problem_locator_submit_supplement");
  const gets = successful.filter((record) => record.tool_name === "problem_locator_get_case");
  const lists = successful.filter((record) => record.tool_name === "problem_locator_list_artifacts");
  requireCondition(submits.length === 2 && gets.length >= 3 && lists.length === 1, "PHASE3_CALL_CARDINALITY", "FAIL", "CONTRACT");
  requireCondition(records[0] === submits[0] && records.at(-1) === lists[0], "PHASE3_CALL_ORDER", "FAIL", "CONTRACT");
  exactKeys(submits[0].input, ["request_id", "case_id", "expected_case_revision", "input_names", "input_values", "attachment_ids", "wait_seconds"], "PHASE3_ATTACHMENT_INPUT_SHAPE");
  requireCondition(submits[0].input.request_id === state.request_ids.submit_attachment && submits[0].input.case_id === state.case_id && submits[0].input.expected_case_revision === state.case_revision && canonicalJson(submits[0].input.attachment_ids) === canonicalJson([state.attachment_id]), "PHASE3_ATTACHMENT_INPUT", "FAIL", "CONTRACT");
  const views = gets.map((record) => ({ ordinal: record.ordinal, view: caseView(record) })).filter((entry) => entry.view?.case_id === state.case_id);
  const orderView = views.find((entry) => entry.view.status === "WAITING_INPUT" && entry.view.requirements?.filter((item) => item.status === "OPEN").map((item) => item.name).includes("order_id"));
  requireCondition(orderView, "PHASE3_ORDER_REQUIREMENT_NOT_OBSERVED", "FAIL", "CONTRACT");
  exactKeys(submits[1].input, ["request_id", "case_id", "expected_case_revision", "input_names", "input_values", "attachment_ids", "wait_seconds"], "PHASE3_ORDER_INPUT_SHAPE");
  requireCondition(submits[1].input.request_id === state.request_ids.submit_order && submits[1].input.case_id === state.case_id && submits[1].input.expected_case_revision === orderView.view.case_revision && canonicalJson(submits[1].input.input_names) === canonicalJson(["order_id"]) && canonicalJson(submits[1].input.input_values) === canonicalJson(["synthetic-order-0001"]), "PHASE3_ORDER_INPUT", "FAIL", "CONTRACT");
  const reviewing = views.find((entry) => entry.ordinal > submits[1].ordinal && entry.view.status === "REVIEWING");
  const resolved = [...views].reverse().find((entry) => entry.ordinal > (reviewing?.ordinal ?? Infinity) && entry.view.status === "RESOLVED");
  requireCondition(reviewing && resolved && resolved.view.final_result?.status === "ACCEPTED", "PHASE3_REVIEW_RESOLUTION", "FAIL", "CONTRACT");
  const listData = successData(lists[0]);
  const artifacts = listData.artifacts;
  requireCondition(Array.isArray(artifacts) && artifacts.length === 2, "PHASE3_ARTIFACT_COUNT", "FAIL", "CONTRACT");
  const publicArtifact = artifacts.find((artifact) => artifact.kind === "USER_RESULT" && artifact.name === "diagnosis-result.json");
  const publicArchive = artifacts.find((artifact) => artifact.kind === "USER_RESULT_ARCHIVE" && artifact.name === "result.zip");
  for (const artifact of [publicArtifact, publicArchive]) {
    requireCondition(UUID.test(artifact?.artifact_id ?? "") && Number.isInteger(artifact?.size) && artifact.size > 0 && SHA256.test(artifact?.sha256 ?? "") && artifact.download_url === `${state.public_base_url}/api/v1/artifacts/${artifact.artifact_id}/content?case_id=${state.case_id}`, "PHASE3_ARTIFACT_INVALID", "FAIL", "CONTRACT");
  }
  return {
    case_id: state.case_id,
    attachment_id: state.attachment_id,
    resolved_case_revision: resolved.view.case_revision,
    diagnosis_state_revision: resolved.view.diagnosis_state_revision,
    selected_skill_ref: resolved.view.selected_skill_ref,
    final_result: resolved.view.final_result,
    observed_statuses: views.map((entry) => entry.view.status),
    public_artifact: publicArtifact,
    public_result_archive: publicArchive,
  };
}

function restartPrompt(state) {
  return `Perform one read-only post-restart persistence verification for Case ${state.case_id}. Treat only Remote MCP structured results as authoritative.
0. First call Skill exactly once with {"skill":"problem-locator-client"}.
1. Call problem_locator_get_case exactly once with case_id "${state.case_id}", wait_for_job_id null, wait_seconds 0.
2. Call problem_locator_list_artifacts exactly once with case_id "${state.case_id}".
3. Stop. Do not create, prepare, submit, resume, cancel, upload, or call another tool.`;
}

function validateRestart(audit, state) {
  const records = audit.records;
  requireCondition(records.length === 2 && records[0].tool_name === "problem_locator_get_case" && records[1].tool_name === "problem_locator_list_artifacts", "RESTART_CALL_SEQUENCE", "FAIL", "CONTRACT");
  const view = caseView(records[0]);
  const artifacts = successData(records[1]).artifacts;
  requireCondition(view?.case_id === state.case_id && view.status === "RESOLVED" && view.case_revision === state.resolved_case_revision && view.final_result?.status === "ACCEPTED", "RESTART_CASE_MISMATCH", "FAIL", "CONTRACT");
  requireCondition(Array.isArray(artifacts) && artifacts.length === 2, "RESTART_ARTIFACT_COUNT", "FAIL", "CONTRACT");
  for (const expected of [state.public_artifact, state.public_result_archive]) {
    const actual = artifacts.find((artifact) => artifact.artifact_id === expected.artifact_id);
    requireCondition(actual && actual.sha256 === expected.sha256 && actual.size === expected.size && actual.kind === expected.kind, "RESTART_ARTIFACT_MISMATCH", "FAIL", "CONTRACT");
  }
  return { case_view: view, artifacts };
}

async function downloadArtifacts(state, stageRoot) {
  for (const [label, artifact] of [["diagnosis-result", state.public_artifact], ["result-archive", state.public_result_archive]]) {
    const response = await fetch(artifact.download_url, { signal: AbortSignal.timeout(60_000) });
    const bytes = Buffer.from(await response.arrayBuffer());
    requireCondition(response.status === 200 && bytes.length === artifact.size && sha256Bytes(bytes) === artifact.sha256, `RESTART_DOWNLOAD_${label.toUpperCase().replaceAll("-", "_")}`, "FAIL", "PRODUCT");
    if (artifact.content_type === "application/json") JSON.parse(bytes.toString("utf8"));
    if (artifact.content_type === "application/zip") requireCondition(bytes.subarray(0, 2).toString("binary") === "PK", "RESTART_ARCHIVE_FORMAT", "FAIL", "CONTRACT");
    fs.writeFileSync(path.join(stageRoot, `restart-${label}.${artifact.content_type === "application/json" ? "json" : "zip"}`), bytes, { flag: "wx", mode: 0o600 });
    process.stdout.write("TEST_FLOW_PROGRESS request.completed\n");
  }
}

function mergeEventParts(attemptRoot, runId, mode) {
  const partsRoot = path.join(attemptRoot, "payload", "events", "parts");
  const suffix = mode === "journey" ? "journey.ndjson" : "diagnostics.ndjson";
  const destination = path.join(attemptRoot, "payload", "events", mode === "journey" ? "service-linux.journey.ndjson" : "service-linux.diagnostics.ndjson");
  const lines = [];
  let sequence = 0;
  for (const instance of INSTANCE_ORDER) {
    const part = path.join(partsRoot, `service-linux.${instance}.${suffix}`);
    if (!fs.existsSync(part)) continue;
    const source = fs.readFileSync(part, "utf8");
    requireCondition(source.endsWith("\n"), "SERVICE_EVENT_PARTIAL_TAIL");
    for (const line of source.split("\n").filter(Boolean)) {
      const event = JSON.parse(line);
      sequence += 1;
      lines.push(JSON.stringify({
        ...event,
        seq: sequence,
        run_id: runId,
        producer_id: mode === "journey" ? "service-linux" : "service-linux-diagnostics",
      }));
    }
  }
  requireCondition(sequence > 0, `SERVICE_${mode.toUpperCase()}_EVENTS_EMPTY`);
  const temporary = `${destination}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, `${lines.join("\n")}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
  fs.renameSync(temporary, destination);
  return sequence;
}

function verifyCorrespondence(state, attemptRoot) {
  const client = state.client_calls.map((record) => record.tool_name);
  const correspondence = readServerMcpCorrespondence(attemptRoot, client);
  requireCondition(correspondence.started_exact, "CLIENT_SERVER_STARTED_CORRESPONDENCE", "FAIL", "CONTRACT");
  requireCondition(correspondence.completed_exact, "CLIENT_SERVER_COMPLETED_CORRESPONDENCE", "FAIL", "CONTRACT");
  return { status: "PASS", client_tool_calls: client.length, server_started: correspondence.started_tool_names.length, server_completed: correspondence.completed_tool_names.length, client_dfx_absent: true };
}

async function startService(configuration, state, instance, { allowEmptyJourney = false } = {}) {
  requireCondition(!state.current_instance, "SERVICE_ALREADY_RUNNING");
  const bootstrapLog = `/evidence/stages/${configuration.stage}/supervisor-${instance}.log`;
  const journeyPolicy = allowEmptyJourney ? "allow-empty" : "require-events";
  await docker(configuration.dockerContext, [
    "exec", "--detach", state.active_container,
    "sh", "-c", 'exec sh /harness/macos-service-supervisor.sh "$1" "$2" >"$3" 2>&1',
    "test-flow-supervisor", instance, journeyPolicy, bootstrapLog,
  ]);
  state.current_instance = instance;
  atomicState(configuration.statePath, state);
  try {
    return await waitReady(state.public_base_url);
  } catch (error) {
    try { await captureReadinessDiagnostic(configuration, state, instance, error?.code ?? "SERVICE_READINESS_ERROR"); } catch {}
    throw error;
  }
}

async function quiesceService(configuration, state) {
  requireCondition(typeof state.current_instance === "string", "SERVICE_NOT_RUNNING");
  const instance = state.current_instance;
  await docker(configuration.dockerContext, ["exec", state.active_container, "sh", "/harness/macos-stop-service.sh", instance]);
  state.current_instance = null;
  atomicState(configuration.statePath, state);
}

async function stopEnvironmentDiagnostic(configuration, state) {
  await quiesceService(configuration, state);
  return mergeEventParts(configuration.attemptRoot, state.run_id, "diagnostics");
}

async function stopService(configuration, state) {
  await quiesceService(configuration, state);
  mergeEventParts(configuration.attemptRoot, state.run_id, "journey");
  mergeEventParts(configuration.attemptRoot, state.run_id, "diagnostics");
  return verifyCorrespondence(state, configuration.attemptRoot);
}

async function initializeContainer(configuration, state, containerName, mode, stageId) {
  const receipt = `/evidence/stages/${stageId}/container-init.json`;
  await docker(configuration.dockerContext, [
    "exec", containerName,
    "sh", "/harness/macos-initialize-container.sh",
    mode,
    configuration.sourceCommit,
    RELEASE_LOGPARSE_COMMIT,
    RELEASE_MCP_COMMIT,
    receipt,
  ]);
  const hostReceipt = path.join(configuration.attemptRoot, "payload", "stages", stageId, "container-init.json");
  requireCondition(readJson(hostReceipt).status === "PASS", "CONTAINER_INIT_RECEIPT_INVALID");
}

async function createContainer(configuration, state, containerName, mode, stageId, register = true) {
  if (register) appendResource(configuration.resourceRegistry, configuration.attemptRoot, "container", containerName, configuration.resourceLabel);
  await docker(configuration.dockerContext, [
    "run", "--detach", "--init",
    "--name", containerName,
    "--label", configuration.resourceLabel,
    "--pull", "never",
    "--platform", "linux/amd64",
    "--network", "bridge",
    "--publish", `127.0.0.1:${state.port}:8000/tcp`,
    "--env", `E2E_RUN_ID=${state.run_id}`,
    "--env", `E2E_PUBLIC_BASE_URL=${state.public_base_url}`,
    "--tmpfs", "/root/.claude:rw,noexec,nosuid,nodev,mode=0700,size=536870912",
    "--tmpfs", "/run/plagent-claude:rw,noexec,nosuid,nodev,mode=0700,size=536870912",
    "--mount", `type=bind,src=${configuration.repoRoot},dst=/source/xiaodao,readonly`,
    "--mount", `type=bind,src=${configuration.logparseSource},dst=/source/logparse,readonly`,
    "--mount", `type=bind,src=${configuration.mcpSource},dst=/source/problem-locator-mcp,readonly`,
    "--mount", `type=bind,src=${configuration.containerClaudeSettings},dst=/run/host-claude-settings.json,readonly`,
    "--mount", `type=bind,src=${path.join(configuration.repoRoot, ".claude", "skills", "logparse-diagnose")},dst=/run/plagent-claude/.claude/skills/logparse-diagnose,readonly`,
    "--mount", `type=bind,src=${path.join(configuration.repoRoot, "tools", "test-flow", "harness")},dst=/harness,readonly`,
    "--mount", `type=bind,src=${path.join(configuration.attemptRoot, "payload")},dst=/evidence`,
    "--mount", `type=volume,src=${state.volume},dst=/var/lib/problem-locator`,
    state.image_id,
    "sleep", "infinity",
  ]);
  state.active_container = containerName;
  atomicState(configuration.statePath, state);
}

async function createFreshEnvironment(configuration, stageRoot, runtimeIdentity) {
  requireCondition(configuration.freshDataRoot, "FRESH_DATA_ROOT_FLAG_REQUIRED", "BLOCKED", "INFRA");
  requireCondition(!fs.existsSync(configuration.statePath), "ADAPTER_STATE_ALREADY_EXISTS");
  const runId = path.basename(configuration.attemptRoot);
  const port = await availablePort();
  const imageInspect = await docker(configuration.dockerContext, ["image", "inspect", configuration.baseImage], { forward: false });
  const imageMetadata = JSON.parse(imageInspect.stdout)[0];
  requireCondition(imageMetadata?.Os === "linux" && imageMetadata?.Architecture === "amd64" && typeof imageMetadata?.Id === "string", "RELEASE_IMAGE_IDENTITY_INVALID", "BLOCKED", "INFRA");
  const state = {
    schema_version: 1,
    run_id: runId,
    volume: safeName("pltf-data", runId),
    initial_container: safeName("pltf-server", runId, "initial"),
    restart_container: safeName("pltf-server", runId, "restart"),
    active_container: null,
    current_instance: null,
    port,
    public_base_url: `http://127.0.0.1:${port}`,
    image_id: imageMetadata.Id,
    runtime_identity: runtimeIdentity,
    request_ids: {
      create: `mac-${sha256Bytes(runId).slice(0, 12)}-create-v1`,
      submit_a: `mac-${sha256Bytes(runId).slice(0, 12)}-submit-a-v1`,
      prepare: `mac-${sha256Bytes(runId).slice(0, 12)}-prepare-v1`,
      submit_attachment: `mac-${sha256Bytes(runId).slice(0, 12)}-submit-attachment-v1`,
      submit_order: `mac-${sha256Bytes(runId).slice(0, 12)}-submit-order-v1`,
    },
    client_calls: [],
    usage: { input_tokens: 0, output_tokens: 0, cost_usd: 0 },
  };
  appendResource(configuration.resourceRegistry, configuration.attemptRoot, "volume", state.volume, configuration.resourceLabel);
  await docker(configuration.dockerContext, ["volume", "create", "--label", configuration.resourceLabel, state.volume]);
  await createContainer(configuration, state, state.initial_container, "fresh", configuration.stage, true);
  await docker(configuration.dockerContext, ["exec", state.active_container, "sh", "-eu", "-c", "test -z \"$(find /var/lib/problem-locator -mindepth 1 -print -quit)\""]);
  const volumeInspect = await docker(configuration.dockerContext, ["volume", "inspect", state.volume], { forward: false });
  const volumeMetadata = JSON.parse(volumeInspect.stdout)[0];
  requireCondition(volumeMetadata.Labels?.["problem-locator.test-flow.run"] === runId, "FRESH_VOLUME_LABEL_MISMATCH");
  const freshAdmission = {
    schema_version: 1,
    status: "PASS",
    lineage_root: "GENESIS",
    volume: state.volume,
    initial_data_root: "EMPTY",
    docker_context: configuration.dockerContext,
    server_os: "linux",
    server_architecture: "x86_64",
    platform: "linux/amd64",
  };
  writeNew(path.join(stageRoot, "fresh-admission.json"), freshAdmission);
  await initializeContainer(configuration, state, state.initial_container, "fresh", configuration.stage);
  ensureClientRuntime(configuration, state);
  atomicState(configuration.statePath, state);
  await startService(configuration, state, "route", { allowEmptyJourney: configuration.track === "dev" });
  return { state, freshAdmission };
}

function jobCounts(exported) {
  const jobs = Object.values(exported?.state?.cases ?? {}).flatMap((aggregate) => Object.values(aggregate.jobs ?? {}));
  return {
    running: jobs.filter((job) => job.status === "RUNNING").length,
    queued: jobs.filter((job) => job.status === "PENDING").length,
  };
}

async function createCheckpointSource(configuration, state, continuation) {
  const checkpointPath = configuration.checkpointOutputSource;
  requireCondition(checkpointPath && path.isAbsolute(checkpointPath) && path.resolve(checkpointPath).startsWith(`${path.resolve(configuration.attemptRoot)}${path.sep}`), "CHECKPOINT_OUTPUT_INVALID");
  requireCondition(!state.current_instance, "CHECKPOINT_SERVICE_RUNNING");
  const stageRoot = path.dirname(checkpointPath);
  ensureDirectory(stageRoot);
  const validation = await docker(configuration.dockerContext, [
    "exec", state.active_container,
    "runuser", "-u", "plagent", "--",
    "/usr/bin/env", "-i",
    "HOME=/run/plagent-claude", "LANG=C.UTF-8", "PATH=/opt/venvs/xiaodao/bin:/usr/local/bin:/usr/bin:/bin", "PYTHONNOUSERSITE=1",
    "/opt/venvs/xiaodao/bin/python", "-I", "-m", "problem_locator", "validate-state", "--data-root", "/var/lib/problem-locator",
  ], { forward: false });
  const validationReport = JSON.parse(validation.stdout);
  requireCondition(validationReport.valid === true && Array.isArray(validationReport.errors) && validationReport.errors.length === 0, "CHECKPOINT_STATE_INVALID", "FAIL", "PRODUCT");
  writeNew(path.join(stageRoot, "state-validation.json"), validationReport);
  const exportContainerPath = `/tmp/test-flow-state-export-${configuration.stage.replaceAll(".", "-")}.json`;
  await docker(configuration.dockerContext, [
    "exec", state.active_container,
    "runuser", "-u", "plagent", "--",
    "/usr/bin/env", "-i",
    "HOME=/run/plagent-claude", "LANG=C.UTF-8", "PATH=/opt/venvs/xiaodao/bin:/usr/local/bin:/usr/bin:/bin", "PYTHONNOUSERSITE=1",
    "/opt/venvs/xiaodao/bin/python", "-I", "-m", "problem_locator", "export-state", "--data-root", "/var/lib/problem-locator", "--output", exportContainerPath,
  ], { forward: false });
  const exportHostPath = path.join(stageRoot, "state-export.json");
  await docker(configuration.dockerContext, ["cp", `${state.active_container}:${exportContainerPath}`, exportHostPath], { forward: false });
  await docker(configuration.dockerContext, ["exec", state.active_container, "rm", "-f", exportContainerPath], { forward: false });
  const exported = readJson(exportHostPath);
  const counts = jobCounts(exported);
  const stateRoot = path.join(configuration.attemptRoot, "scratch", "checkpoint-sources", configuration.stage);
  ensureDirectory(path.dirname(stateRoot));
  fs.mkdirSync(stateRoot, { recursive: false, mode: 0o700 });
  await docker(configuration.dockerContext, ["cp", `${state.active_container}:/var/lib/problem-locator/.`, stateRoot], { forward: false });
  const workspaces = path.join(stateRoot, "tmp", "workspaces");
  const temporaryWorkspaces = fs.existsSync(workspaces) ? fs.readdirSync(workspaces).length : 0;
  const top = await docker(configuration.dockerContext, ["top", state.active_container, "-eo", "args"], { forward: false });
  const activeWorkers = top.stdout.split(/\r?\n/).filter((line) => /test_service_launcher\.py|\/usr\/local\/bin\/claude|macos-service-supervisor/.test(line)).length;
  const receipt = {
    schema_version: 1,
    status: counts.running === 0 && counts.queued === 0 && activeWorkers === 0 && temporaryWorkspaces === 0 ? "PASS" : "FAIL",
    service_stopped: true,
    running_jobs: counts.running,
    queued_jobs: counts.queued,
    active_workers: activeWorkers,
    temporary_workspaces: temporaryWorkspaces,
    state_validation: "PASS",
  };
  requireCondition(receipt.status === "PASS", "CHECKPOINT_NOT_QUIESCENT", "ERROR", "HARNESS");
  writeNew(checkpointPath, {
    schema_version: 1,
    state_root: stateRoot,
    continuation,
    quiescence_receipt: receipt,
  });
  return receipt;
}

function addUsage(state, usage) {
  for (const name of ["input_tokens", "output_tokens", "cost_usd"]) state.usage[name] = Math.round((state.usage[name] + Number(usage[name] ?? 0)) * 1_000_000) / 1_000_000;
}

function stageReceipt(configuration, value) {
  const receiptPath = path.join(configuration.stageRoot, "adapter-result.json");
  writeNew(receiptPath, { schema_version: 1, stage_id: configuration.stage, ...value });
}

async function execute(configuration) {
  requireCondition(process.platform === "darwin" && configuration.client === "macos", "MACOS_ADAPTER_HOST_REQUIRED", "BLOCKED", "INFRA");
  const devEnvironmentDiagnostic = configuration.track === "dev" && configuration.stage === "journey.cross-job.environment";
  requireCondition(configuration.track === "release" || devEnvironmentDiagnostic, "MACOS_ADAPTER_TRACK_UNSUPPORTED", "BLOCKED", "INFRA");
  requireCondition(configuration.dockerContext === "colima", "MACOS_ADAPTER_DOCKER_CONTEXT", "BLOCKED", "INFRA");
  requireCondition(configuration.sourceCommit && configuration.logparseSource && configuration.mcpSource && configuration.claudeEntry && configuration.claudeSettings, "MACOS_ADAPTER_INPUT_MISSING", "BLOCKED", "INFRA");
  requireCondition(!configuration.restoredDataRoot && !configuration.restoredCheckpointId, "RELEASE_CHECKPOINT_RESTORE_FORBIDDEN", "BLOCKED", "INFRA");
  await verifyRepositoryIdentity(configuration);
  const runtimeIdentity = currentReleaseRuntimeIdentity(configuration);
  try {
    configuration.containerClaudeSettings = materializeAttemptClaudeSettings(
      configuration.claudeSettings,
      configuration.attemptRoot,
      runtimeIdentity.settings.fingerprint,
    ).path;
  } catch (error) {
    throw new StageError(`CLAUDE_SETTINGS_STAGING_${String(error?.message ?? error)}`, "BLOCKED", "INFRA");
  }

  if (configuration.stage === "journey.cross-job.environment") {
    const { state, freshAdmission } = await createFreshEnvironment(configuration, configuration.stageRoot, runtimeIdentity);
    const diagnosticEvents = devEnvironmentDiagnostic ? await stopEnvironmentDiagnostic(configuration, state) : null;
    stageReceipt(configuration, { status: "PASS", fresh_admission: freshAdmission, client_tool_calls: 0, server_tool_calls: 0, service_diagnostic_events: diagnosticEvents, checkpoint_ready: false, usage: { input_tokens: 0, output_tokens: 0, cost_usd: 0 } });
    return;
  }

  requireCondition(fs.existsSync(configuration.statePath), "ADAPTER_STATE_MISSING");
  const state = readJson(configuration.statePath);
  requireCondition(state.schema_version === 1 && state.run_id === path.basename(configuration.attemptRoot), "ADAPTER_STATE_INVALID");
  requireCondition(canonicalJson(state.runtime_identity) === canonicalJson(runtimeIdentity), "RELEASE_RUNTIME_IDENTITY_DRIFT", "BLOCKED", "INFRA");

  if (configuration.stage === "journey.cross-job.route") {
    const audit = await runClaude(configuration, state, configuration.stageRoot, "phase1", phaseOnePrompt(state.request_ids), 20, 3);
    const summary = validatePhaseOne(audit, state.request_ids, state.public_base_url);
    Object.assign(state, summary);
    state.client_calls.push(...audit.records.map((record, index) => ({ phase: "phase1", ordinal: state.client_calls.length + index, tool_name: record.tool_name, input: record.input })));
    addUsage(state, audit.usage);
    atomicState(configuration.statePath, state);
    const correspondence = await stopService(configuration, state);
    const checkpoint = await createCheckpointSource(configuration, state, {
      case_id: state.case_id,
      attachment_id: state.attachment_id,
      prepared_case_revision: state.prepared_case_revision,
      request_ids: Object.values(state.request_ids),
    });
    await startService(configuration, state, "upload");
    stageReceipt(configuration, { status: "PASS", client_tool_calls: audit.records.length, server_tool_calls: correspondence.server_completed, checkpoint_ready: checkpoint.status === "PASS", usage: audit.usage });
    return;
  }

  if (configuration.stage === "journey.cross-job.upload") {
    const upload = await uploadAttachment(configuration, state, configuration.stageRoot);
    Object.assign(state, upload);
    atomicState(configuration.statePath, state);
    const correspondence = await stopService(configuration, state);
    const checkpoint = await createCheckpointSource(configuration, state, {
      case_id: state.case_id,
      attachment_id: state.attachment_id,
      case_revision: state.case_revision,
      attachment_status: state.status,
      request_ids: Object.values(state.request_ids),
    });
    await startService(configuration, state, "diagnose");
    stageReceipt(configuration, { status: "PASS", client_tool_calls: 0, server_tool_calls: correspondence.server_completed, checkpoint_ready: checkpoint.status === "PASS", usage: { input_tokens: 0, output_tokens: 0, cost_usd: 0 } });
    return;
  }

  if (configuration.stage === "journey.cross-job.diagnose") {
    const audit = await runClaude(configuration, state, configuration.stageRoot, "phase3", phaseThreePrompt(state), 30, 5);
    const summary = validatePhaseThree(audit, state);
    Object.assign(state, summary);
    state.client_calls.push(...audit.records.map((record, index) => ({ phase: "phase3", ordinal: state.client_calls.length + index, tool_name: record.tool_name, input: record.input })));
    addUsage(state, audit.usage);
    atomicState(configuration.statePath, state);
    const correspondence = await stopService(configuration, state);
    const checkpoint = await createCheckpointSource(configuration, state, {
      case_id: state.case_id,
      attachment_id: state.attachment_id,
      resolved_case_revision: state.resolved_case_revision,
      public_artifact_id: state.public_artifact.artifact_id,
      public_archive_id: state.public_result_archive.artifact_id,
      observed_statuses: state.observed_statuses,
    });
    stageReceipt(configuration, { status: "PASS", client_tool_calls: audit.records.length, server_tool_calls: correspondence.server_completed, checkpoint_ready: checkpoint.status === "PASS", usage: audit.usage });
    return;
  }

  if (configuration.stage === "journey.cross-job.publish-restart") {
    requireCondition(!state.current_instance, "PUBLISH_RESTART_SERVICE_STILL_RUNNING");
    await docker(configuration.dockerContext, ["container", "stop", "--time", "10", state.initial_container]);
    await createContainer(configuration, state, state.restart_container, "restart", configuration.stage, true);
    await initializeContainer(configuration, state, state.restart_container, "restart", configuration.stage);
    await startService(configuration, state, "restart");
    const audit = await runClaude(configuration, state, configuration.stageRoot, "restart", restartPrompt(state), 10, 1);
    validateRestart(audit, state);
    state.client_calls.push(...audit.records.map((record, index) => ({ phase: "restart", ordinal: state.client_calls.length + index, tool_name: record.tool_name, input: record.input })));
    addUsage(state, audit.usage);
    atomicState(configuration.statePath, state);
    await downloadArtifacts(state, configuration.stageRoot);
    const correspondence = await stopService(configuration, state);
    const checkpoint = await createCheckpointSource(configuration, state, {
      case_id: state.case_id,
      resolved_case_revision: state.resolved_case_revision,
      public_artifact_id: state.public_artifact.artifact_id,
      public_archive_id: state.public_result_archive.artifact_id,
      restart_verified: true,
    });
    writeNew(path.join(configuration.stageRoot, "client-server-correspondence.json"), { schema_version: 1, ...correspondence });
    stageReceipt(configuration, { status: "PASS", client_tool_calls: audit.records.length, server_tool_calls: correspondence.server_completed, checkpoint_ready: checkpoint.status === "PASS", restart_verified: true, usage: audit.usage });
    return;
  }

  throw new StageError("MACOS_ADAPTER_STAGE_UNKNOWN");
}

const parsed = parseArguments(process.argv.slice(2));
const values = parsed.values;
const configuration = {
  stage: values.stage,
  repoRoot: values.repo_root && path.resolve(values.repo_root),
  attemptRoot: values.attempt_root && path.resolve(values.attempt_root),
  client: values.client,
  track: values.track,
  sourceCommit: values.source_commit,
  claudeEntry: values.claude_entry && path.resolve(values.claude_entry),
  claudeSettings: values.claude_settings && path.resolve(values.claude_settings),
  dockerContext: values.docker_context,
  cacheRoot: values.cache_root && path.resolve(values.cache_root),
  logparseSource: values.logparse_source && path.resolve(values.logparse_source),
  mcpSource: values.mcp_source && path.resolve(values.mcp_source),
  baseImage: values.base_image,
  resourceRegistry: values.resource_registry && path.resolve(values.resource_registry),
  resourceLabel: values.resource_label,
  checkpointOutputSource: values.checkpoint_output_source && path.resolve(values.checkpoint_output_source),
  restoredDataRoot: values.restored_data_root && path.resolve(values.restored_data_root),
  restoredCheckpointId: values.restored_checkpoint_id,
  freshDataRoot: parsed.flags.has("--fresh-data-root"),
};
configuration.statePath = configuration.attemptRoot && path.join(configuration.attemptRoot, "scratch", "macos-cross-job", "state.json");
configuration.stageRoot = configuration.attemptRoot && configuration.stage && path.join(configuration.attemptRoot, "payload", "stages", configuration.stage);

let receiptWritten = false;
try {
  for (const [name, value] of Object.entries({ stage: configuration.stage, repoRoot: configuration.repoRoot, attemptRoot: configuration.attemptRoot, resourceRegistry: configuration.resourceRegistry, resourceLabel: configuration.resourceLabel, baseImage: configuration.baseImage })) {
    requireCondition(Boolean(value), `ADAPTER_REQUIRED_${name.toUpperCase()}`);
  }
  ensureDirectory(configuration.stageRoot);
  await execute(configuration);
  receiptWritten = true;
  process.stdout.write("TEST_FLOW_PROGRESS stage.completed\n");
} catch (error) {
  const failure = error instanceof StageError ? error : new StageError(`ADAPTER_UNEXPECTED:${String(error?.message ?? error)}`);
  try {
    if (configuration.stageRoot) {
      ensureDirectory(configuration.stageRoot);
      const receiptPath = path.join(configuration.stageRoot, "adapter-result.json");
      if (!fs.existsSync(receiptPath)) {
        const progress = recoverStageAuditProgress({
          attemptRoot: configuration.attemptRoot,
          stageRoot: configuration.stageRoot,
          stageId: configuration.stage,
        });
        writeNew(receiptPath, {
          schema_version: 1,
          stage_id: configuration.stage ?? "unknown",
          status: failure.status,
          failure_domain: failure.domain,
          code: failure.code,
          client_tool_calls: progress.client_tool_calls,
          server_tool_calls: progress.server_tool_calls,
          checkpoint_ready: false,
          usage: progress.usage,
        });
        receiptWritten = true;
      }
    }
  } catch {}
  process.stderr.write(`${failure.code}\n`);
  process.exitCode = failure.status === "INCONCLUSIVE" || failure.status === "BLOCKED" ? 2 : failure.status === "FAIL" ? 1 : 3;
}

void receiptWritten;
