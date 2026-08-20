#!/usr/bin/env node
// Shared first-party Windows/macOS/Linux Client to Linux Server CrossJob core.
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
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
import { extractCheckpointSourceArchive } from "../lib/checkpoint.mjs";
import { fixedGetCasePollingInvariant } from "../lib/cross-job-polling.mjs";
import {
  NEGATIVE_PROBE_VALIDATION_FIELDS,
  readRelayedEventPart,
  readServerMcpCorrespondence,
} from "../lib/events.mjs";
import { recoverStageAuditProgress } from "../lib/evidence.mjs";
import {
  compareReleaseCaseEntries,
  diagnosisSkillRuntimeRefId,
  discoverReleaseCaseRoot,
  loadReleaseCaseInputs,
  loadReleaseCaseOracle,
  releaseCaseInputCoverage,
  releaseCaseDigests,
} from "../lib/release-case.mjs";
import { verifyMaterializedSourceSnapshot } from "../lib/source-snapshot.mjs";
import {
  isCompleteUsage,
  normalizeUsage,
  sumUsage,
  TOKEN_USAGE_FORMULA,
  zeroUsage,
} from "../lib/usage.mjs";
import { canonicalJson, ensureDirectory, sha256Bytes, sha256File } from "../lib/util.mjs";
import { chromeIdentity, resolveChromeExecutable } from "../lib/browser.mjs";

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
    if (["--fresh-data-root", "--terminal-after-stage"].includes(name)) {
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

function scriptJson(value) {
  return JSON.stringify(value)
    .replaceAll("<", "\\u003c")
    .replaceAll("\u2028", "\\u2028")
    .replaceAll("\u2029", "\\u2029");
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

function selectedReleaseCase(repoRoot) {
  const root = discoverReleaseCaseRoot(path.join(repoRoot, "tests", "cases", "release"));
  const inputs = loadReleaseCaseInputs(root);
  const gateOracle = loadReleaseCaseOracle(root);
  const digests = releaseCaseDigests(root);
  const scenario = inputs.scenarios.find((item) => item.scenario_id === inputs.journey_scenario);
  const scenarioOracle = gateOracle.scenarios.find((item) => item.scenario_id === inputs.journey_scenario);
  requireCondition(scenario && scenarioOracle, "RELEASE_CASE_JOURNEY_SCENARIO_MISSING");
  const skillManifest = readJson(path.join(inputs.approved_skill_dir, "diagnosis-skill.json"));
  const inputCoverage = releaseCaseInputCoverage(skillManifest, scenario.driver);
  const attachments = skillManifest.requirements.filter((item) => item.kind === "ATTACHMENT" && item.stage === "INITIAL");
  requireCondition(inputCoverage.initialValid, "RELEASE_CASE_INITIAL_INPUT_DRIFT", "FAIL", "CONTRACT");
  requireCondition(inputCoverage.supplementValid, "RELEASE_CASE_SUPPLEMENT_INPUT_DRIFT", "FAIL", "CONTRACT");
  requireCondition(attachments.length === 1, "RELEASE_CASE_ATTACHMENT_REQUIREMENT", "FAIL", "CONTRACT");
  requireCondition(canonicalJson(skillManifest.logparse_plan.anchors.map((item) => item.label)) === canonicalJson(scenario.driver.attachment_anchor_names), "RELEASE_CASE_ANCHOR_DRIFT", "FAIL", "CONTRACT");
  const approvedProductRecords = fs.readdirSync(inputs.approved_skill_dir, { withFileTypes: true })
    .sort(compareReleaseCaseEntries)
    .map((entry) => {
      requireCondition(entry.isFile() && !entry.isSymbolicLink(), "RELEASE_CASE_APPROVED_SKILL_NODE", "FAIL", "CONTRACT");
      const absolute = path.join(inputs.approved_skill_dir, entry.name);
      const metadata = fs.statSync(absolute);
      requireCondition(metadata.nlink === 1, "RELEASE_CASE_APPROVED_SKILL_LINK", "FAIL", "CONTRACT");
      return { path: entry.name, size: metadata.size, sha256: sha256File(absolute) };
    });
  requireCondition(canonicalJson(approvedProductRecords.map((item) => item.path)) === canonicalJson(["SKILL.md", "diagnosis-skill.json"]), "RELEASE_CASE_APPROVED_SKILL_FILES", "FAIL", "CONTRACT");
  return {
    root,
    case_id: inputs.case_id,
    scenario_id: scenario.scenario_id,
    driver: scenario.driver,
    oracle: scenarioOracle.oracle,
    semantic_oracle: gateOracle.semantic_oracle,
    logparse_product: skillManifest.logparse_product,
    skill: {
      id: skillManifest.id,
      runtime_ref_id: diagnosisSkillRuntimeRefId(skillManifest.id),
      version: skillManifest.version,
      content_hash: packageTreeIdentity(inputs.approved_skill_dir).digest,
      product_digest: sha256Bytes(canonicalJson(approvedProductRecords)),
      attachment_requirement: attachments[0].name,
    },
    input_digest: digests.input_digest,
    oracle_digest: digests.oracle_digest,
  };
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
  requireCondition(configuration.repoRoot.startsWith(`${configuration.attemptRoot}${path.sep}`), "RELEASE_SOURCE_SNAPSHOT_OUTSIDE_ATTEMPT", "BLOCKED", "INFRA");
  requireCondition(configuration.sourceSnapshotManifest.startsWith(`${configuration.attemptRoot}${path.sep}`), "RELEASE_SOURCE_MANIFEST_OUTSIDE_ATTEMPT", "BLOCKED", "INFRA");
  const sourceManifest = readJson(configuration.sourceSnapshotManifest);
  requireCondition(sourceManifest?.digest === configuration.sourceSnapshotDigest, "RELEASE_SOURCE_SNAPSHOT_MANIFEST_DRIFT", "BLOCKED", "INFRA");
  const verification = verifyMaterializedSourceSnapshot(configuration.repoRoot, sourceManifest);
  requireCondition(verification.status === "PASS", "RELEASE_SOURCE_SNAPSHOT_DRIFT", "BLOCKED", "INFRA");
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

async function runChromePage(label, page, fixtureBytes = null) {
  const chrome = chromeIdentity();
  const chromeExecutable = resolveChromeExecutable();
  requireCondition(chrome.status === "PRESENT" && chromeExecutable, chrome.code ?? "CHROME_REQUIRED", "BLOCKED", "INFRA");
  const fixtureServer = http.createServer((request, response) => {
    if (request.url === "/fixture" && fixtureBytes !== null) {
      response.writeHead(200, {
        "Cache-Control": "no-store",
        "Content-Length": String(fixtureBytes.length),
        "Content-Type": "application/octet-stream",
      });
      response.end(fixtureBytes);
      return;
    }
    response.writeHead(200, {
      "Cache-Control": "no-store",
      "Content-Type": "text/html; charset=utf-8",
    });
    response.end(page);
  });
  await new Promise((resolve, reject) => {
    fixtureServer.once("error", reject);
    fixtureServer.listen({ host: "127.0.0.1", port: 0 }, resolve);
  });
  const fixtureAddress = fixtureServer.address();
  requireCondition(typeof fixtureAddress === "object" && fixtureAddress && Number.isInteger(fixtureAddress.port), "BROWSER_FIXTURE_ADDRESS_INVALID");
  const browserOrigin = `http://127.0.0.1:${fixtureAddress.port}`;
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), `test-flow-chrome-${label}-`));
  let chromeRun;
  try {
    const chromeArguments = [
      "--headless=new",
      "--disable-background-networking",
      "--disable-component-update",
      "--disable-default-apps",
      "--disable-gpu",
      "--disable-sync",
      "--metrics-recording-only",
      "--no-default-browser-check",
      "--no-first-run",
      "--no-proxy-server",
      `--user-data-dir=${profile}`,
      "--virtual-time-budget=30000",
      "--dump-dom",
      `${browserOrigin}/`,
    ];
    if (typeof process.getuid === "function" && process.getuid() === 0) chromeArguments.unshift("--no-sandbox");
    chromeRun = await run(chromeExecutable, chromeArguments, { forward: false, maximumBytes: 8 * 1024 * 1024 });
  } finally {
    await new Promise((resolve) => fixtureServer.close(resolve));
    fs.rmSync(profile, { recursive: true, force: true });
  }
  requireCondition(chromeRun.status === 0, `CHROME_${label.toUpperCase()}_EXIT_${chromeRun.status}`, "FAIL", "BROWSER");
  const match = chromeRun.stdout.match(/data-result="([A-Za-z0-9+/=]+)"/);
  requireCondition(match, `CHROME_${label.toUpperCase()}_RESULT_MISSING`, "FAIL", "BROWSER");
  let result;
  try {
    result = JSON.parse(Buffer.from(match[1], "base64").toString("utf8"));
  } catch {
    throw new StageError(`CHROME_${label.toUpperCase()}_RESULT_INVALID`, "FAIL", "BROWSER");
  }
  return { browserOrigin, chrome, chromeRun, result };
}

function dockerArgs(context, args) {
  return context && context !== "default" ? ["--context", context, ...args] : args;
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
  fs.appendFileSync(registry, `${JSON.stringify({ schema_version: 2, kind, name, label })}\n`, { encoding: "utf8", mode: 0o600 });
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
    launcher_process_present: topProbe.status === 0 && topProbe.stdout.includes("/test-flow-runtime/test_service_launcher.py"),
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
  const runtimeRoot = path.join(configuration.attemptRoot, "scratch", `${configuration.client}-client`);
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

function parseClaudeStream(text, expectedCwd, { allowErrorTerminal = false } = {}) {
  const events = text.split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
  const initEvents = events.filter((event) => event.type === "system" && event.subtype === "init");
  const results = events.filter((event) => event.type === "result");
  requireCondition(initEvents.length === 1 && results.length === 1 && events.at(-1)?.type === "result", "CLIENT_STREAM_TERMINAL_INVALID");
  const terminal = results[0];
  const terminalSucceeded = terminal.subtype === "success" && terminal.is_error === false;
  requireCondition(terminalSucceeded || (allowErrorTerminal && terminal.is_error === true), "CLIENT_RESULT_NOT_SUCCESS", "INCONCLUSIVE", "EXTERNAL");
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
  let usage;
  try {
    usage = normalizeUsage({
      input_tokens: Number(terminal.usage?.input_tokens ?? -1),
      output_tokens: Number(terminal.usage?.output_tokens ?? -1),
      cache_creation_input_tokens: Number(terminal.usage?.cache_creation_input_tokens ?? -1),
      cache_read_input_tokens: Number(terminal.usage?.cache_read_input_tokens ?? -1),
      cost_usd: Number(terminal.total_cost_usd ?? terminal.cost_usd ?? -1),
    });
  } catch {
    requireCondition(false, "CLIENT_MODEL_USAGE_INVALID");
  }
  return {
    schema_version: 1,
    init: { cwd: init.cwd, effective_model: init.model, permission_mode: init.permissionMode, tools: init.tools, mcp_servers: servers },
    records: mcpRecords,
    usage,
    terminal: { subtype: terminal.subtype, is_error: terminal.is_error },
    turns: Number(terminal.num_turns),
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
  const invocationStartedUtc = new Date().toISOString();
  const invocationStarted = process.hrtime.bigint();
  const exit = await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, args, { cwd: configuration.repoRoot, env: environment, stdio: ["ignore", "pipe", "pipe"] });
    let timedOut = false;
    const timeout = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
      setTimeout(() => child.exitCode === null && child.kill("SIGKILL"), 5000).unref();
    }, configuration.hardCaps.hard_timeout_seconds * 1000);
    timeout.unref();
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
    child.once("error", (error) => { clearTimeout(timeout); reject(error); });
    child.once("exit", (code, signal) => { clearTimeout(timeout); resolve({ code, signal, timedOut }); });
  });
  fs.fsyncSync(stdoutDescriptor);
  fs.fsyncSync(stderrDescriptor);
  fs.closeSync(stdoutDescriptor);
  fs.closeSync(stderrDescriptor);
  const audit = parseClaudeStream(Buffer.concat(chunks).toString("utf8"), configuration.repoRoot, { allowErrorTerminal: exit.code !== 0 });
  writeNew(auditPath, audit);
  writeNew(path.join(stageRoot, `${phase}.timing.json`), {
    schema_version: 2,
    span: "host.model-and-mcp-wait",
    phase,
    clock_domain: `${configuration.client}-host`,
    started_at_utc: invocationStartedUtc,
    finished_at_utc: new Date().toISOString(),
    duration_ms: Math.round(Number(process.hrtime.bigint() - invocationStarted) / 1_000_000),
    timed_out: exit.timedOut,
    max_turns: maxTurns,
    max_budget_usd: maxBudgetUsd,
    max_total_tokens: configuration.hardCaps.max_total_tokens,
    effective_model: audit.init.effective_model,
    usage: audit.usage,
  });
  const forbidden = [
    path.join(runtime.runtimeRoot, "client-dfx.jsonl"),
    path.join(runtime.runtimeRoot, ".problem-locator", "client-dfx.jsonl"),
    path.join(stageRoot, `${phase}.client-dfx.jsonl`),
  ];
  requireCondition(forbidden.every((filePath) => !fs.existsSync(filePath)), "CLIENT_DFX_FORBIDDEN");
  if (exit.timedOut) throw new StageError(`CLAUDE_${phase.toUpperCase()}_HARD_TIMEOUT`, "INCONCLUSIVE", "EXTERNAL");
  if (exit.code !== 0) throw new StageError(`CLAUDE_${phase.toUpperCase()}_EXIT_${exit.code ?? exit.signal}`, "INCONCLUSIVE", "EXTERNAL");
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

function openRequirementNames(view, kind) {
  return (view?.pending_requirements ?? [])
    .filter((item) => item.status === "OPEN" && item.kind === kind)
    .map((item) => item.name);
}

function expectedCreateInput(releaseCase, requestIds) {
  return {
    request_id: requestIds.create,
    ...releaseCase.driver.problem,
    initial_user_fact_names: releaseCase.driver.initial_user_fact_names,
    initial_user_fact_values: releaseCase.driver.initial_user_fact_values,
    wait_seconds: 0,
  };
}

function phaseOnePrompt(releaseCase, requestIds, archive) {
  const createInput = canonicalJson(expectedCreateInput(releaseCase, requestIds)).trimEnd();
  return `Perform phase 1 of the controlled Problem Locator acceptance journey. Use only the Skill tool and the seven problem_locator Remote MCP tools. Treat MCP tool_result structured payloads as authoritative; do not infer state from prose.

0. Your first action MUST call the Skill tool with skill=problem-locator-client (exact input {"skill":"problem-locator-client"}). Until that Skill tool_result is received successfully, do not call any problem_locator MCP tool.
1. Call problem_locator_create_case exactly once with this exact flat root input (do not send problem_spec or any nested object): ${createInput}
${fixedGetCasePollingInvariant("<authoritative-case-id>")}
2. Poll problem_locator_get_case with non-empty case_id input and wait_seconds 30 until status WAITING_ATTACHMENT has exactly the OPEN ATTACHMENT requirement ${JSON.stringify(releaseCase.skill.attachment_requirement)}. Use wait_seconds 30 on every poll; do not rapid-poll. The initial facts were already supplied to create_case, so do not resubmit them and do not accept WAITING_INPUT as the target state.
3. Call problem_locator_prepare_attachment exactly once with request_id "${requestIds.prepare}", the latest revision, name ${JSON.stringify(archive.name)}, content_type ${JSON.stringify(archive.content_type)}, declared_size ${archive.size}, and declared_sha256 "${archive.sha256}". The call must contain exactly those seven required root properties and must never send nested input.
4. Stop immediately after the successful prepare result. Do not upload, submit the attachment, or call another tool.`;
}

function validatePhaseOne(audit, releaseCase, requestIds, archive, publicBaseUrl) {
  const records = audit.records;
  const successful = records.filter((record) => record.result?.ok === true);
  const create = successful.filter((record) => record.tool_name === "problem_locator_create_case");
  const submit = successful.filter((record) => record.tool_name === "problem_locator_submit_supplement");
  const prepare = successful.filter((record) => record.tool_name === "problem_locator_prepare_attachment");
  requireCondition(create.length === 1 && submit.length === 0 && prepare.length === 1, "PHASE1_CALL_CARDINALITY", "FAIL", "CONTRACT");
  requireCondition(records.at(-1) === prepare[0], "PHASE1_PREPARE_NOT_TERMINAL", "FAIL", "CONTRACT");
  const createInput = expectedCreateInput(releaseCase, requestIds);
  exactKeys(create[0].input, Object.keys(createInput), "PHASE1_CREATE_INPUT_SHAPE");
  requireCondition(canonicalJson(create[0].input) === canonicalJson(createInput) && !Object.hasOwn(create[0].input, "problem_spec"), "PHASE1_CREATE_INPUT", "FAIL", "CONTRACT");
  exactKeys(prepare[0].input, ["request_id", "case_id", "expected_case_revision", "name", "content_type", "declared_size", "declared_sha256"], "PHASE1_PREPARE_INPUT_SHAPE");
  requireCondition(prepare[0].input.request_id === requestIds.prepare && prepare[0].input.name === archive.name && prepare[0].input.content_type === archive.content_type && prepare[0].input.declared_size === archive.size && prepare[0].input.declared_sha256 === archive.sha256, "PHASE1_PREPARE_INPUT", "FAIL", "CONTRACT");
  const prepareData = successData(prepare[0]);
  const response = prepareData.application_response;
  const view = response?.case_view;
  const descriptor = prepareData.upload;
  requireCondition(view?.status === "WAITING_ATTACHMENT" && UUID.test(view.case_id) && Number.isInteger(view.case_revision), "PHASE1_CASE_VIEW", "FAIL", "CONTRACT");
  requireCondition(canonicalJson(openRequirementNames(view, "ATTACHMENT")) === canonicalJson([releaseCase.skill.attachment_requirement]), "PHASE1_ATTACHMENT_REQUIREMENT", "FAIL", "CONTRACT");
  requireCondition(view.selected_skill_ref?.id === releaseCase.skill.runtime_ref_id && view.selected_skill_ref?.version === releaseCase.skill.version, "PHASE1_SELECTED_SKILL", "FAIL", "CONTRACT");
  exactKeys(descriptor, ["attachment_id", "method", "url", "required_headers", "max_bytes", "expires_at"], "PHASE1_UPLOAD_DESCRIPTOR_SHAPE");
  requireCondition(UUID.test(descriptor.attachment_id) && descriptor.method === "PUT" && descriptor.url === `${publicBaseUrl}/api/v1/attachments/${descriptor.attachment_id}/content` && descriptor.max_bytes === MAX_ATTACHMENT_BYTES && descriptor.expires_at === null, "PHASE1_UPLOAD_DESCRIPTOR", "FAIL", "CONTRACT");
  exactKeys(descriptor.required_headers, ["Content-Length", "Content-Type", "Idempotency-Key", "X-Content-SHA256"], "PHASE1_UPLOAD_HEADERS_SHAPE");
  requireCondition(descriptor.required_headers["Content-Length"] === String(archive.size) && descriptor.required_headers["Content-Type"] === archive.content_type && descriptor.required_headers["Idempotency-Key"] === descriptor.attachment_id && descriptor.required_headers["X-Content-SHA256"] === archive.sha256, "PHASE1_UPLOAD_HEADERS", "FAIL", "CONTRACT");
  requireCondition(!successful.some((record) => caseView(record)?.status === "WAITING_INPUT"), "PHASE1_INITIAL_FACT_REGRESSION", "FAIL", "CONTRACT");
  return {
    case_id: view.case_id,
    attachment_id: descriptor.attachment_id,
    prepared_case_revision: view.case_revision,
    prepare_expected_case_revision: prepare[0].input.expected_case_revision,
    selected_skill_ref: view.selected_skill_ref,
    upload_descriptor: descriptor,
  };
}

async function uploadAttachment(configuration, state, stageRoot) {
  const descriptor = state.upload_descriptor;
  const archive = path.join(configuration.attemptRoot, "payload", state.archive.name);
  requireCondition(fs.existsSync(archive) && fs.statSync(archive).size === state.archive.size && sha256File(archive) === state.archive.sha256, "UPLOAD_FIXTURE_INVALID");
  requireCondition(descriptor.url === `${state.public_base_url}/api/v1/attachments/${state.attachment_id}/content`, "UPLOAD_URL_INVALID");
  const flatCreate = expectedCreateInput(configuration.releaseCase, state.request_ids);
  const problemKeys = ["statement", "expected_behavior", "actual_behavior", "scope", "goals", "non_goals", "constraints", "completion_criteria"];
  const createBody = {
    request_id: flatCreate.request_id,
    raw_problem_text: flatCreate.raw_problem_text,
    problem_spec: Object.fromEntries(problemKeys.map((name) => [name, flatCreate[name]])),
    initial_user_facts: flatCreate.initial_user_fact_names.map((name, index) => ({ name, value: flatCreate.initial_user_fact_values[index] })),
    wait_seconds: flatCreate.wait_seconds,
  };
  const prepareBody = {
    request_id: state.request_ids.prepare,
    expected_case_revision: state.prepare_expected_case_revision,
    name: state.archive.name,
    content_type: state.archive.content_type,
    declared_size: state.archive.size,
    declared_sha256: state.archive.sha256,
  };

  const archiveBytes = fs.readFileSync(archive);
  const page = `<!doctype html><html><head><meta charset="utf-8"><title>PENDING</title></head><body><script>
const configuration = ${scriptJson({
    createUrl: `${state.public_base_url}/api/v1/cases`,
    caseUrl: `${state.public_base_url}/api/v1/cases/${state.case_id}`,
    prepareUrl: `${state.public_base_url}/api/v1/cases/${state.case_id}/attachments`,
    createBody,
    prepareBody,
  })};
function encoded(value) {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}
async function jsonRequest(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let envelope = null;
  try { envelope = JSON.parse(text); } catch {}
  return {
    status: response.status,
    envelope,
    correlation_id: response.headers.get("x-problem-locator-correlation-id"),
  };
}
async function upload(label, bytes, descriptor) {
  const response = await fetch(descriptor.url, {
    method: "PUT",
    headers: descriptor.required_headers,
    body: new Blob([bytes], { type: descriptor.required_headers["Content-Type"] }),
  });
  const text = await response.text();
  let envelope = null;
  try { envelope = JSON.parse(text); } catch {}
  return {
    label,
    status: response.status,
    error_code: envelope?.error?.code ?? null,
    data: envelope?.data ?? null,
    correlation_id: response.headers.get("x-problem-locator-correlation-id"),
    response_sha256: response.headers.get("x-content-sha256"),
  };
}
(async () => {
  const create = await jsonRequest(configuration.createUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(configuration.createBody),
  });
  const queryBefore = await jsonRequest(configuration.caseUrl + "?wait_seconds=0");
  const prepare = await jsonRequest(configuration.prepareUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(configuration.prepareBody),
  });
  const descriptor = prepare.envelope?.data?.upload;
  if (!descriptor) throw new Error("REST prepare did not return an upload descriptor");
  const fixture = await fetch("/fixture", { cache: "no-store" });
  if (!fixture.ok) throw new Error("fixture fetch failed");
  const bytes = await fixture.arrayBuffer();
  if (bytes.byteLength < 2) throw new Error("fixture is too small");
  const wrongHash = bytes.slice(0);
  new Uint8Array(wrongHash)[0] ^= 1;
  const results = [
    await upload("size-mismatch", bytes.slice(0, bytes.byteLength - 1), descriptor),
    await upload("hash-mismatch", wrongHash, descriptor),
    await upload("ready", bytes, descriptor),
  ];
  const queryAfter = await jsonRequest(configuration.caseUrl + "?wait_seconds=0");
  document.documentElement.dataset.result = encoded({ ok: true, body_kind: "Blob", byte_length: bytes.byteLength, create, query_before: queryBefore, prepare, descriptor, results, query_after: queryAfter });
  document.title = "DONE";
})().catch((error) => {
  document.documentElement.dataset.result = encoded({ ok: false, error: String(error?.stack ?? error) });
  document.title = "FAILED";
});
</script></body></html>`;

  const startedAtUtc = new Date().toISOString();
  const started = process.hrtime.bigint();
  const { browserOrigin, chrome, chromeRun, result: browserResult } = await runChromePage(
    "upload",
    page,
    archiveBytes,
  );
  requireCondition(browserResult.ok === true && browserResult.body_kind === "Blob" && browserResult.byte_length === archiveBytes.length, "CHROME_UPLOAD_EXECUTION_FAILED", "FAIL", "BROWSER");
  requireCondition(browserResult.create?.status === 200 && browserResult.create.envelope?.ok === true && browserResult.create.envelope?.data?.business_receipt?.case_id === state.case_id, "CHROME_CREATE_REPLAY_INVALID", "FAIL", "CONTRACT");
  requireCondition(browserResult.query_before?.status === 200 && browserResult.query_before.envelope?.data?.case_view?.status === "WAITING_ATTACHMENT", "CHROME_QUERY_BEFORE_UPLOAD_INVALID", "FAIL", "CONTRACT");
  requireCondition(browserResult.prepare?.status === 200 && browserResult.prepare.envelope?.ok === true && browserResult.prepare.envelope?.data?.application_response?.business_receipt?.primary_resource_id === state.attachment_id, "CHROME_PREPARE_REPLAY_INVALID", "FAIL", "CONTRACT");
  const webDescriptor = browserResult.descriptor;
  exactKeys(webDescriptor, ["attachment_id", "method", "url", "required_headers", "expected_content_length", "max_bytes", "expires_at"], "CHROME_UPLOAD_DESCRIPTOR_SHAPE");
  exactKeys(webDescriptor.required_headers, ["Content-Type", "Idempotency-Key", "X-Content-SHA256"], "CHROME_UPLOAD_HEADERS_SHAPE");
  requireCondition(webDescriptor.attachment_id === state.attachment_id && webDescriptor.method === "PUT" && webDescriptor.url === descriptor.url && webDescriptor.expected_content_length === state.archive.size && webDescriptor.required_headers["Idempotency-Key"] === state.attachment_id && webDescriptor.required_headers["Content-Type"] === state.archive.content_type && webDescriptor.required_headers["X-Content-SHA256"] === state.archive.sha256, "CHROME_UPLOAD_DESCRIPTOR_INVALID", "FAIL", "CONTRACT");
  const [sizeMismatch, hashMismatch, ready] = browserResult.results ?? [];
  requireCondition(sizeMismatch?.label === "size-mismatch" && sizeMismatch.status === 400 && sizeMismatch.error_code === "VALIDATION_ERROR", "CHROME_SIZE_MISMATCH_NOT_REJECTED", "FAIL", "CONTRACT");
  requireCondition(hashMismatch?.label === "hash-mismatch" && hashMismatch.status === 422 && hashMismatch.error_code === "RESOURCE_HASH_MISMATCH", "CHROME_HASH_MISMATCH_NOT_REJECTED", "FAIL", "CONTRACT");
  requireCondition(ready?.label === "ready" && ready.status === 200 && ready.data?.case_id === state.case_id && ready.data?.attachment_id === state.attachment_id && ready.data?.status === "READY" && Number.isInteger(ready.data.case_revision) && ready.data.case_revision > state.prepared_case_revision, "CHROME_UPLOAD_RESPONSE_INVALID", "FAIL", "CONTRACT");
  requireCondition(browserResult.query_after?.status === 200 && browserResult.query_after.envelope?.data?.case_view?.case_revision === ready.data.case_revision, "CHROME_QUERY_AFTER_UPLOAD_INVALID", "FAIL", "CONTRACT");
  requireCondition(typeof ready.correlation_id === "string" && ready.correlation_id.length > 0, "CHROME_CORRELATION_HEADER_NOT_EXPOSED", "FAIL", "CONTRACT");
  const browserHeaders = webDescriptor.required_headers;
  const browserReceipt = {
    schema_version: 1,
    status: "PASS",
    browser: chrome,
    origin: browserOrigin,
    target_origin: new URL(descriptor.url).origin,
    cross_origin: browserOrigin !== new URL(descriptor.url).origin,
    operations: ["create_case", "get_case", "prepare_attachment", "upload_attachment"],
    body_kind: browserResult.body_kind,
    content_length_control: "user-agent",
    scripted_headers: Object.keys(browserHeaders).sort(),
    size_mismatch_status: sizeMismatch.status,
    hash_mismatch_status: hashMismatch.status,
    ready_status: ready.status,
    correlation_header_exposed: true,
  };
  writeNew(path.join(stageRoot, "chrome-upload.json"), browserReceipt);
  writeNew(path.join(stageRoot, "upload.timing.json"), {
    schema_version: 2,
    span: "host.http-upload",
    clock_domain: `${configuration.client}-host`,
    started_at_utc: startedAtUtc,
    finished_at_utc: new Date().toISOString(),
    duration_ms: Math.round(Number(process.hrtime.bigint() - started) / 1_000_000),
    request_bytes: fs.statSync(archive).size,
    response_bytes: Buffer.byteLength(chromeRun.stdout),
    http_status: ready.status,
    retries: 0,
    timed_out: false,
  });
  writeTextNew(path.join(stageRoot, "upload.response.json"), `${canonicalJson(ready)}`);
  process.stdout.write("TEST_FLOW_PROGRESS attachment.upload.completed\n");
  return { status: "READY", case_revision: ready.data.case_revision, browser_upload: browserReceipt };
}

function phaseThreePrompt(state, releaseCase) {
  const supplement = releaseCase.driver.supplement_input_names.length > 0
    ? `2. Poll problem_locator_get_case with non-empty case_id and wait_seconds 30 until WAITING_INPUT has exactly these OPEN INPUT requirements: ${canonicalJson(releaseCase.driver.supplement_input_names).trimEnd()}. Use wait_seconds 30 on every poll; do not rapid-poll.\n3. Call problem_locator_submit_supplement exactly once with request_id "${state.request_ids.submit_inputs}", the latest revision, input_names ${canonicalJson(releaseCase.driver.supplement_input_names).trimEnd()}, input_values ${canonicalJson(releaseCase.driver.supplement_input_values).trimEnd()}, attachment_ids [], wait_seconds 0.\n4.`
    : "2. Do not submit any ordinary inputs because this scenario declares none.\n3.";
  return `Perform phase 3 of the controlled Problem Locator journey. Use only the Skill tool and the seven problem_locator Remote MCP tools. Treat structured MCP results as authoritative. Case ${state.case_id}, attachment ${state.attachment_id}, and case_revision ${state.case_revision} are authoritative.

0. First call Skill with exact input {"skill":"problem-locator-client"}; do not call MCP before it succeeds.
1. Call problem_locator_submit_supplement exactly once with request_id "${state.request_ids.submit_attachment}", case_id "${state.case_id}", expected_case_revision ${state.case_revision}, input_names [], input_values [], attachment_ids ["${state.attachment_id}"], wait_seconds 0.
${fixedGetCasePollingInvariant(state.case_id)}
${supplement} Poll with the same literal get-case input. Observe REVIEWING, then continue unchanged until a terminal case status with final_result.status ACCEPTED. Use wait_seconds 30 on every poll, do not rapid-poll, and do not skip REVIEWING.
5. Call problem_locator_list_artifacts exactly once for this Case and stop. Do not call another tool.`;
}

function validatePhaseThree(audit, state, releaseCase) {
  const records = audit.records;
  const successful = records.filter((record) => record.result?.ok === true);
  const submits = successful.filter((record) => record.tool_name === "problem_locator_submit_supplement");
  const gets = successful.filter((record) => record.tool_name === "problem_locator_get_case");
  const lists = successful.filter((record) => record.tool_name === "problem_locator_list_artifacts");
  const hasSupplement = releaseCase.driver.supplement_input_names.length > 0;
  requireCondition(submits.length === (hasSupplement ? 2 : 1) && gets.length >= (hasSupplement ? 3 : 2) && lists.length === 1, "PHASE3_CALL_CARDINALITY", "FAIL", "CONTRACT");
  requireCondition(records[0] === submits[0] && records.at(-1) === lists[0], "PHASE3_CALL_ORDER", "FAIL", "CONTRACT");
  exactKeys(submits[0].input, ["request_id", "case_id", "expected_case_revision", "input_names", "input_values", "attachment_ids", "wait_seconds"], "PHASE3_ATTACHMENT_INPUT_SHAPE");
  requireCondition(submits[0].input.request_id === state.request_ids.submit_attachment && submits[0].input.case_id === state.case_id && submits[0].input.expected_case_revision === state.case_revision && canonicalJson(submits[0].input.attachment_ids) === canonicalJson([state.attachment_id]), "PHASE3_ATTACHMENT_INPUT", "FAIL", "CONTRACT");
  const views = gets.map((record) => ({ ordinal: record.ordinal, view: caseView(record) })).filter((entry) => entry.view?.case_id === state.case_id);
  let terminalPredecessor = submits[0];
  if (hasSupplement) {
    const inputView = views.find((entry) => entry.view.status === "WAITING_INPUT" && canonicalJson(openRequirementNames(entry.view, "INPUT")) === canonicalJson(releaseCase.driver.supplement_input_names));
    requireCondition(inputView, "PHASE3_INPUT_REQUIREMENT_NOT_OBSERVED", "FAIL", "CONTRACT");
    exactKeys(submits[1].input, ["request_id", "case_id", "expected_case_revision", "input_names", "input_values", "attachment_ids", "wait_seconds"], "PHASE3_INPUT_SHAPE");
    requireCondition(submits[1].input.request_id === state.request_ids.submit_inputs && submits[1].input.case_id === state.case_id && submits[1].input.expected_case_revision === inputView.view.case_revision && canonicalJson(submits[1].input.input_names) === canonicalJson(releaseCase.driver.supplement_input_names) && canonicalJson(submits[1].input.input_values) === canonicalJson(releaseCase.driver.supplement_input_values) && canonicalJson(submits[1].input.attachment_ids) === canonicalJson([]), "PHASE3_INPUT", "FAIL", "CONTRACT");
    terminalPredecessor = submits[1];
  }
  const reviewing = views.find((entry) => entry.ordinal > terminalPredecessor.ordinal && entry.view.status === "REVIEWING");
  const resolved = [...views].reverse().find((entry) => entry.ordinal > (reviewing?.ordinal ?? Infinity) && entry.view.status === releaseCase.oracle.case_status);
  requireCondition(reviewing && resolved && resolved.view.final_result?.status === "ACCEPTED", "PHASE3_REVIEW_RESOLUTION", "FAIL", "CONTRACT");
  requireCondition(resolved.view.final_result?.resolution_status === releaseCase.oracle.resolution_status, "PHASE3_RESOLUTION_STATUS", "FAIL", "CONTRACT");
  requireCondition(resolved.view.selected_skill_ref?.id === releaseCase.skill.runtime_ref_id && resolved.view.selected_skill_ref?.version === releaseCase.skill.version, "PHASE3_SELECTED_SKILL", "FAIL", "CONTRACT");
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
    rest_supplements: submits.map((record) => ({
      request_id: record.input.request_id,
      expected_case_revision: record.input.expected_case_revision,
      inputs: record.input.input_names.map((name, index) => ({ name, value: record.input.input_values[index] })),
      attachment_ids: record.input.attachment_ids,
      wait_seconds: record.input.wait_seconds,
    })),
  };
}

async function verifyResolvedWebApi(configuration, state, summary, stageRoot) {
  const expectedArtifacts = [summary.public_artifact, summary.public_result_archive];
  const page = `<!doctype html><html><head><meta charset="utf-8"><title>PENDING</title></head><body><script>
const configuration = ${scriptJson({
    caseUrl: `${state.public_base_url}/api/v1/cases/${state.case_id}`,
    supplementsUrl: `${state.public_base_url}/api/v1/cases/${state.case_id}/supplements`,
    artifactsUrl: `${state.public_base_url}/api/v1/cases/${state.case_id}/artifacts`,
    supplements: summary.rest_supplements,
  })};
function encoded(value) {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}
async function jsonRequest(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let envelope = null;
  try { envelope = JSON.parse(text); } catch {}
  return { status: response.status, envelope, correlation_id: response.headers.get("x-problem-locator-correlation-id") };
}
async function digest(bytes) {
  const value = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(value)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}
(async () => {
  const supplements = [];
  for (const body of configuration.supplements) {
    supplements.push(await jsonRequest(configuration.supplementsUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }));
  }
  const query = await jsonRequest(configuration.caseUrl + "?wait_seconds=30");
  const artifacts = await jsonRequest(configuration.artifactsUrl);
  const downloads = [];
  for (const artifact of artifacts.envelope?.data?.artifacts ?? []) {
    const response = await fetch(artifact.download_url);
    const bytes = await response.arrayBuffer();
    downloads.push({
      artifact_id: artifact.artifact_id,
      status: response.status,
      size: bytes.byteLength,
      sha256: await digest(bytes),
      header_sha256: response.headers.get("x-content-sha256"),
      header_length: response.headers.get("content-length"),
      correlation_id: response.headers.get("x-problem-locator-correlation-id"),
    });
  }
  document.documentElement.dataset.result = encoded({ ok: true, supplements, query, artifacts, downloads });
  document.title = "DONE";
})().catch((error) => {
  document.documentElement.dataset.result = encoded({ ok: false, error: String(error?.stack ?? error) });
  document.title = "FAILED";
});
</script></body></html>`;
  const { browserOrigin, chrome, result } = await runChromePage("resolved-api", page);
  requireCondition(result.ok === true, "CHROME_RESOLVED_API_EXECUTION_FAILED", "FAIL", "BROWSER");
  requireCondition(Array.isArray(result.supplements) && result.supplements.length === summary.rest_supplements.length && result.supplements.every((item) => item.status === 200 && item.envelope?.ok === true && typeof item.correlation_id === "string"), "CHROME_SUPPLEMENT_REPLAY_INVALID", "FAIL", "CONTRACT");
  requireCondition(result.query?.status === 200 && result.query.envelope?.data?.case_view?.case_id === state.case_id && result.query.envelope?.data?.case_view?.case_revision === summary.resolved_case_revision && result.query.envelope?.data?.wait_timed_out === false, "CHROME_TERMINAL_QUERY_INVALID", "FAIL", "CONTRACT");
  const listed = result.artifacts?.envelope?.data?.artifacts;
  requireCondition(result.artifacts?.status === 200 && Array.isArray(listed) && listed.length === expectedArtifacts.length, "CHROME_ARTIFACT_LIST_INVALID", "FAIL", "CONTRACT");
  for (const expected of expectedArtifacts) {
    const listedArtifact = listed.find((item) => item.artifact_id === expected.artifact_id);
    const download = result.downloads?.find((item) => item.artifact_id === expected.artifact_id);
    requireCondition(listedArtifact?.size === expected.size && listedArtifact?.sha256 === expected.sha256 && listedArtifact?.download_url === expected.download_url, "CHROME_ARTIFACT_VIEW_MISMATCH", "FAIL", "CONTRACT");
    requireCondition(download?.status === 200 && download.size === expected.size && download.sha256 === expected.sha256 && download.header_sha256 === expected.sha256 && download.header_length === String(expected.size) && typeof download.correlation_id === "string", "CHROME_ARTIFACT_DOWNLOAD_MISMATCH", "FAIL", "CONTRACT");
  }
  const receipt = {
    schema_version: 1,
    status: "PASS",
    browser: chrome,
    origin: browserOrigin,
    target_origin: new URL(state.public_base_url).origin,
    cross_origin: browserOrigin !== new URL(state.public_base_url).origin,
    operations: ["submit_supplement", "get_case", "list_artifacts", "download_artifact"],
    supplement_replays: result.supplements.length,
    artifacts_verified: expectedArtifacts.length,
    correlation_header_exposed: true,
    content_headers_exposed: true,
  };
  writeNew(path.join(stageRoot, "chrome-resolved-api.json"), receipt);
  return receipt;
}

function restartPrompt(state) {
  return `Perform one read-only post-restart persistence verification for Case ${state.case_id}. Treat only Remote MCP structured results as authoritative.
0. First call Skill exactly once with {"skill":"problem-locator-client"}.
1. Call problem_locator_get_case exactly once with case_id "${state.case_id}", wait_for_job_id null, wait_seconds 0.
2. Call problem_locator_list_artifacts exactly once with case_id "${state.case_id}".
3. Stop. Do not create, prepare, submit, resume, cancel, upload, or call another tool.`;
}

function validateRestart(audit, state, releaseCase) {
  const records = audit.records;
  requireCondition(records.length === 2 && records[0].tool_name === "problem_locator_get_case" && records[1].tool_name === "problem_locator_list_artifacts", "RESTART_CALL_SEQUENCE", "FAIL", "CONTRACT");
  const view = caseView(records[0]);
  const artifacts = successData(records[1]).artifacts;
  requireCondition(view?.case_id === state.case_id && view.status === releaseCase.oracle.case_status && view.case_revision === state.resolved_case_revision && view.final_result?.status === "ACCEPTED" && view.final_result?.resolution_status === releaseCase.oracle.resolution_status, "RESTART_CASE_MISMATCH", "FAIL", "CONTRACT");
  requireCondition(view.selected_skill_ref?.id === releaseCase.skill.runtime_ref_id && view.selected_skill_ref?.version === releaseCase.skill.version, "RESTART_SELECTED_SKILL", "FAIL", "CONTRACT");
  requireCondition(Array.isArray(artifacts) && artifacts.length === 2, "RESTART_ARTIFACT_COUNT", "FAIL", "CONTRACT");
  for (const expected of [state.public_artifact, state.public_result_archive]) {
    const actual = artifacts.find((artifact) => artifact.artifact_id === expected.artifact_id);
    requireCondition(actual && actual.sha256 === expected.sha256 && actual.size === expected.size && actual.kind === expected.kind, "RESTART_ARTIFACT_MISMATCH", "FAIL", "CONTRACT");
  }
  return { case_view: view, artifacts };
}

async function downloadArtifacts(configuration, state, stageRoot) {
  for (const [label, artifact] of [["diagnosis-result", state.public_artifact], ["result-archive", state.public_result_archive]]) {
    const startedAtUtc = new Date().toISOString();
    const started = process.hrtime.bigint();
    const response = await fetch(artifact.download_url, { signal: AbortSignal.timeout(60_000) });
    const bytes = Buffer.from(await response.arrayBuffer());
    writeNew(path.join(stageRoot, `restart-${label}.timing.json`), {
      schema_version: 2,
      span: "host.http-download",
      clock_domain: `${configuration.client}-host`,
      started_at_utc: startedAtUtc,
      finished_at_utc: new Date().toISOString(),
      duration_ms: Math.round(Number(process.hrtime.bigint() - started) / 1_000_000),
      response_bytes: bytes.length,
      http_status: response.status,
      retries: 0,
      timed_out: false,
    });
    requireCondition(response.status === 200 && bytes.length === artifact.size && sha256Bytes(bytes) === artifact.sha256, `RESTART_DOWNLOAD_${label.toUpperCase().replaceAll("-", "_")}`, "FAIL", "PRODUCT");
    if (artifact.content_type === "application/json") {
      const report = JSON.parse(bytes.toString("utf8"));
      const expectedStatus = configuration.releaseCase.oracle.resolution_status === "COMPLETE" ? "COMPLETED" : "PARTIAL";
      requireCondition(report?.schema_version === 3 && report.status === expectedStatus, "RESTART_RESULT_STATUS", "FAIL", "CONTRACT");
      for (const [field, expected] of [
        ["causal_factors", configuration.releaseCase.oracle.causal_factor_ids],
        ["candidate_factors", configuration.releaseCase.oracle.candidate_factor_ids],
        ["excluded_factors", configuration.releaseCase.oracle.excluded_factor_ids],
      ]) {
        requireCondition(canonicalJson((report[field] ?? []).map((item) => item.factor_id)) === canonicalJson(expected), `RESTART_RESULT_${field.toUpperCase()}`, "FAIL", "CONTRACT");
      }
      requireCondition(canonicalJson((report.completion_criteria_mapping ?? []).map((item) => item.status)) === canonicalJson(configuration.releaseCase.oracle.criterion_statuses), "RESTART_RESULT_CRITERIA", "FAIL", "CONTRACT");
      const serialized = canonicalJson(report);
      requireCondition(configuration.releaseCase.oracle.required_safety_phrases.every((phrase) => serialized.includes(phrase)), "RESTART_RESULT_SAFETY_NOTES", "FAIL", "CONTRACT");
    }
    if (artifact.content_type === "application/zip") requireCondition(bytes.subarray(0, 2).toString("binary") === "PK", "RESTART_ARCHIVE_FORMAT", "FAIL", "CONTRACT");
    fs.writeFileSync(path.join(stageRoot, `restart-${label}.${artifact.content_type === "application/json" ? "json" : "zip"}`), bytes, { flag: "wx", mode: 0o600 });
    process.stdout.write("TEST_FLOW_PROGRESS request.completed\n");
  }
}

function indexEventParts(configuration, state, mode, indexLabel = null) {
  const attemptRoot = configuration.attemptRoot;
  const runId = state.run_id;
  const partsRoot = path.join(attemptRoot, "payload", "events", "parts");
  const suffix = mode === "journey" ? "journey.ndjson" : "diagnostics.ndjson";
  const streams = [];
  let eventCount = 0;
  for (const instance of INSTANCE_ORDER) {
    const part = path.join(partsRoot, `service-linux.${instance}.${suffix}`);
    const receipt = path.join(attemptRoot, "payload", `service-${instance}-${mode}-relay.json`);
    if (!fs.existsSync(part) && !fs.existsSync(receipt)) continue;
    let relayed;
    try {
      relayed = readRelayedEventPart({
        filePath: part,
        receiptPath: receipt,
        expectedProducerId: mode === "journey" ? `service-linux-${instance}` : `service-linux-diagnostics-${instance}`,
        expectedRunId: runId,
        allowEmpty: mode === "journey" && ["route", "restart"].includes(instance),
      });
    } catch (error) {
      const code = typeof error?.code === "string" && /^[A-Z0-9_]+$/.test(error.code)
        ? error.code
        : "EVENT_PART_INVALID";
      throw new StageError(`SERVICE_${code}`, "ERROR", "HARNESS");
    }
    eventCount += relayed.event_count;
    streams.push({
      instance,
      producer_id: relayed.receipt.producer_id,
      clock_domain: relayed.receipt.clock_domain,
      event_count: relayed.event_count,
      events_path: path.relative(attemptRoot, part).split(path.sep).join("/"),
      events_sha256: relayed.receipt.events_sha256,
      raw_path: path.relative(attemptRoot, part.replace(/\.ndjson$/, ".raw")).split(path.sep).join("/"),
      raw_sha256: relayed.receipt.raw_sha256,
      receipt_path: path.relative(attemptRoot, receipt).split(path.sep).join("/"),
    });
  }
  requireCondition(eventCount > 0 || (mode === "journey" && streams.some((stream) => ["route", "restart"].includes(stream.instance))), `SERVICE_${mode.toUpperCase()}_EVENTS_EMPTY`);
  const index = { schema_version: 2, authority: "producer-streams", aggregate_is_authoritative: false, mode, event_count: eventCount, streams };
  const suffixName = indexLabel ? `-${indexLabel}` : "";
  writeNew(path.join(configuration.stageRoot, `${mode}-event-index${suffixName}.json`), index);
  return index;
}

function verifyCorrespondence(state, attemptRoot) {
  const correspondence = readServerMcpCorrespondence(attemptRoot, state.client_calls, {
    validationProbeRequestId: state.validation_probe_request_id,
  });
  requireCondition(correspondence.started_exact, "CLIENT_SERVER_STARTED_CORRESPONDENCE", "FAIL", "CONTRACT");
  requireCondition(correspondence.completed_exact, "CLIENT_SERVER_COMPLETED_CORRESPONDENCE", "FAIL", "CONTRACT");
  requireCondition(correspondence.pair_exact && correspondence.request_exact, "CLIENT_SERVER_REQUEST_CORRESPONDENCE", "FAIL", "CONTRACT");
  requireCondition(correspondence.tools_listed_exact, "SERVER_TOOLS_LISTED_INCOMPLETE", "FAIL", "CONTRACT");
  requireCondition(correspondence.validation_probe_exact, "SERVER_VALIDATION_DFX_INCOMPLETE", "FAIL", "CONTRACT");
  return { status: "PASS", client_tool_calls: state.client_calls.length, server_started: correspondence.started_tool_names.length, server_completed: correspondence.completed_tool_names.length, request_exact: true, pair_exact: true, tools_listed_exact: true, validation_probe_exact: true, validation_probe_request_id: state.validation_probe_request_id, validation_fields: correspondence.validation_fields, client_dfx_absent: true };
}

async function runServerDfxProbe(configuration, state) {
  requireCondition(!state.validation_probe_request_id, "SERVER_DFX_PROBE_ALREADY_RUN");
  const requestId = `test-flow-${sha256Bytes(state.run_id).slice(0, 16)}-invalid-v2`;
  const containerOutput = `/evidence/stages/${configuration.stage}/server-dfx-probe.json`;
  await docker(configuration.dockerContext, [
    "exec", state.active_container,
    "/opt/venvs/xiaodao/bin/python", "-I", "/test-flow-runtime/server_dfx_probe.py",
    "--request-id", requestId,
    "--output", containerOutput,
  ]);
  const receipt = readJson(path.join(configuration.stageRoot, "server-dfx-probe.json"));
  requireCondition(
    receipt?.schema_version === 2
      && receipt.status === "PASS"
      && receipt.validation_probe_request_id === requestId
      && receipt.flat_schema === true
      && receipt.tool_count === 7
      && canonicalJson(receipt.tool_names) === canonicalJson(TOOL_NAMES)
      && canonicalJson(receipt.validation_fields) === canonicalJson(NEGATIVE_PROBE_VALIDATION_FIELDS),
    "SERVER_DFX_PROBE_RECEIPT_INVALID",
    "FAIL",
    "CONTRACT",
  );
  state.validation_probe_request_id = requestId;
  atomicState(configuration.statePath, state);
  return receipt;
}

async function startService(configuration, state, instance, { allowEmptyJourney = true } = {}) {
  requireCondition(!state.current_instance, "SERVICE_ALREADY_RUNNING");
  const bootstrapLog = `/evidence/stages/${configuration.stage}/supervisor-${instance}.log`;
  const journeyPolicy = allowEmptyJourney ? "allow-empty" : "require-events";
  await docker(configuration.dockerContext, [
    "exec", "--detach", state.active_container,
    "sh", "-c", 'exec sh /test-flow-runtime/service-supervisor.sh "$1" "$2" >"$3" 2>&1',
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

function relayFailureFromServiceReceipt(configuration, instance) {
  const payloadRoot = path.join(configuration.attemptRoot, "payload");
  const supervisorPath = path.join(payloadRoot, `service-${instance}-supervisor.json`);
  if (!fs.existsSync(supervisorPath)) return null;
  try {
    const supervisor = readJson(supervisorPath);
    if (supervisor?.schema_version !== 1 || supervisor.instance !== instance || supervisor.status !== "FAIL") return null;
    const relayKind = supervisor.code === "JOURNEY_RELAY"
      ? "journey"
      : supervisor.code === "DIAGNOSTIC_RELAY" ? "diagnostics" : null;
    if (!relayKind) return null;
    const relayPath = path.join(payloadRoot, `service-${instance}-${relayKind}-relay.json`);
    const relay = fs.existsSync(relayPath) ? readJson(relayPath) : null;
    const relayCode = relay?.status === "FAIL" && /^[A-Z0-9_]+$/.test(relay.code ?? "")
      ? `_${relay.code}`
      : "";
    return new StageError(`SERVICE_${instance.toUpperCase()}_${supervisor.code}${relayCode}`, "ERROR", "HARNESS");
  } catch {
    return null;
  }
}

async function quiesceService(configuration, state) {
  requireCondition(typeof state.current_instance === "string", "SERVICE_NOT_RUNNING");
  const instance = state.current_instance;
  try {
    await docker(configuration.dockerContext, ["exec", state.active_container, "sh", "/test-flow-runtime/stop-service.sh", instance]);
  } catch (error) {
    throw relayFailureFromServiceReceipt(configuration, instance) ?? error;
  }
  state.current_instance = null;
  atomicState(configuration.statePath, state);
}

async function stopEnvironmentDiagnostic(configuration, state) {
  await quiesceService(configuration, state);
  return indexEventParts(configuration, state, "diagnostics");
}

async function stopService(configuration, state, { indexLabel = null } = {}) {
  const instance = state.current_instance;
  await quiesceService(configuration, state);
  indexEventParts(configuration, state, "journey", indexLabel);
  indexEventParts(configuration, state, "diagnostics", indexLabel);
  const serviceInvocations = await auditServiceAgentUsage(configuration, state, instance);
  return { ...verifyCorrespondence(state, configuration.attemptRoot), service_invocations: serviceInvocations };
}

async function archiveFailureServiceEvidence(configuration) {
  if (!configuration.statePath || !fs.existsSync(configuration.statePath)) return;
  const state = readJson(configuration.statePath);
  if (state.schema_version !== 1 || state.run_id !== path.basename(configuration.attemptRoot)) return;
  if (typeof state.current_instance === "string") {
    try { await quiesceService(configuration, state); } catch {}
  }
  for (const mode of ["journey", "diagnostics"]) {
    try {
      const indexPath = path.join(configuration.stageRoot, `${mode}-event-index.json`);
      if (!fs.existsSync(indexPath)) indexEventParts(configuration, state, mode);
    } catch {}
  }
}

async function initializeContainer(configuration, state, containerName, mode, stageId) {
  const receipt = `/evidence/stages/${stageId}/container-init.json`;
  await docker(configuration.dockerContext, [
    "exec", containerName,
    "sh", "/test-flow-runtime/initialize-container.sh",
    mode,
    configuration.sourceSnapshotDigest,
    RELEASE_LOGPARSE_COMMIT,
    RELEASE_MCP_COMMIT,
    receipt,
  ]);
  const hostReceipt = path.join(configuration.attemptRoot, "payload", "stages", stageId, "container-init.json");
  const initialization = readJson(hostReceipt);
  const caseReceipt = readJson(
    path.join(configuration.attemptRoot, "payload", "stages", stageId, "release-case.json"),
  );
  requireCondition(
    initialization.status === "PASS"
      && initialization.xiaodao_snapshot_digest === configuration.sourceSnapshotDigest
      && initialization.case_source_redacted === true
      && SHA256.test(initialization.logparse_config_sha256),
    "CONTAINER_INIT_RECEIPT_INVALID",
  );
  requireCondition(
    caseReceipt?.schema_version === 1
      && caseReceipt.status === "PASS"
      && caseReceipt.case_id === configuration.releaseCase.case_id
      && caseReceipt.scenario_id === configuration.releaseCase.scenario_id
      && caseReceipt.skill_id === configuration.releaseCase.skill.id
      && caseReceipt.skill_product_digest === configuration.releaseCase.skill.product_digest
      && caseReceipt.logparse_product === configuration.releaseCase.logparse_product
      && caseReceipt.archive_projection === "logparse-current-loose-diagnostic-v2"
      && Number.isSafeInteger(caseReceipt.logparse_config_size)
      && caseReceipt.logparse_config_size > 0
      && SHA256.test(caseReceipt.logparse_config_sha256)
      && caseReceipt.logparse_config_sha256 === initialization.logparse_config_sha256
      && typeof caseReceipt.archive_name === "string"
      && Number.isSafeInteger(caseReceipt.archive_size)
      && caseReceipt.archive_size > 0
      && SHA256.test(caseReceipt.archive_sha256),
    "RELEASE_CASE_RECEIPT_INVALID",
  );
  state.release_case = {
    case_id: configuration.releaseCase.case_id,
    scenario_id: configuration.releaseCase.scenario_id,
    input_digest: configuration.releaseCase.input_digest,
    oracle_digest: configuration.releaseCase.oracle_digest,
    skill_id: configuration.releaseCase.skill.id,
    logparse_product: caseReceipt.logparse_product,
    logparse_config_sha256: caseReceipt.logparse_config_sha256,
  };
  state.archive = {
    name: caseReceipt.archive_name,
    content_type: caseReceipt.archive_content_type,
    size: caseReceipt.archive_size,
    sha256: caseReceipt.archive_sha256,
    member_count: caseReceipt.archive_member_count,
  };
  atomicState(configuration.statePath, state);
  return { initialization, caseReceipt };
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
    "--env", `TEST_FLOW_SERVICE_MODEL=${RELEASE_MODEL}`,
    "--env", `TEST_FLOW_SERVICE_MAX_TURNS=${configuration.serviceAgentCaps.max_turns}`,
    "--env", `TEST_FLOW_SERVICE_MAX_TOTAL_TOKENS=${configuration.serviceAgentCaps.max_total_tokens}`,
    "--env", `TEST_FLOW_SERVICE_MAX_BUDGET_USD=${configuration.serviceAgentCaps.max_budget_usd}`,
    "--env", `TEST_FLOW_SERVICE_HARD_TIMEOUT_SECONDS=${configuration.serviceAgentCaps.hard_timeout_seconds}`,
    "--tmpfs", "/root/.claude:rw,noexec,nosuid,nodev,mode=0700,size=536870912",
    "--tmpfs", "/run/plagent-claude:rw,noexec,nosuid,nodev,mode=0700,size=536870912",
    "--mount", `type=bind,src=${configuration.repoRoot},dst=/source/xiaodao,readonly`,
    "--mount", `type=bind,src=${configuration.logparseSource},dst=/source/logparse,readonly`,
    "--mount", `type=bind,src=${configuration.mcpSource},dst=/source/problem-locator-mcp,readonly`,
    "--mount", `type=bind,src=${configuration.containerClaudeSettings},dst=/run/host-claude-settings.json,readonly`,
    "--mount", `type=bind,src=${path.join(configuration.repoRoot, ".claude", "skills", "logparse-diagnose")},dst=/run/plagent-claude/.claude/skills/logparse-diagnose,readonly`,
    "--mount", `type=bind,src=${path.join(configuration.repoRoot, "tools", "test-flow", "runtime-support")},dst=/test-flow-runtime,readonly`,
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
      create: `${configuration.client}-${sha256Bytes(runId).slice(0, 12)}-create-v1`,
      prepare: `${configuration.client}-${sha256Bytes(runId).slice(0, 12)}-prepare-v1`,
      submit_attachment: `${configuration.client}-${sha256Bytes(runId).slice(0, 12)}-submit-attachment-v1`,
      submit_inputs: `${configuration.client}-${sha256Bytes(runId).slice(0, 12)}-submit-inputs-v1`,
    },
    client_calls: [],
    audited_service_job_ids: [],
    usage: zeroUsage(),
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
  await startService(configuration, state, "route", { allowEmptyJourney: true });
  const dfxProbe = await runServerDfxProbe(configuration, state);
  return { state, freshAdmission, dfxProbe };
}

function exportedJobs(exported) {
  return Object.values(exported?.state?.cases ?? {}).flatMap((aggregate) => Object.values(aggregate.jobs ?? {}));
}

function jobCounts(exported) {
  const jobs = exportedJobs(exported);
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
  const workspaceProbe = await docker(configuration.dockerContext, [
    "exec", state.active_container,
    "find", "/var/lib/problem-locator/tmp/workspaces", "-mindepth", "1", "-maxdepth", "1", "-printf", "%y %f\\n",
  ], { forward: false });
  const workspaceEntries = workspaceProbe.stdout.split(/\r?\n/).filter(Boolean)
    .map((line) => ({ kind: line.slice(0, 1), job_id: line.slice(2) }));
  const workspaceIds = workspaceEntries.map((entry) => entry.job_id).sort();
  const terminalJobs = new Set(exportedJobs(exported)
    .filter((job) => ["SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"].includes(job.status))
    .map((job) => job.job_id));
  const retainedWorkspacesAreTerminal = workspaceEntries.every((entry) => entry.kind === "d" && terminalJobs.has(entry.job_id));
  const processes = await docker(configuration.dockerContext, ["exec", state.active_container, "ps", "-ww", "-eo", "args"], { forward: false });
  const activeWorkers = processes.stdout.split(/\r?\n/).filter((line) => /test_service_launcher\.py|\/usr\/local\/bin\/claude|service-supervisor/.test(line)).length;
  const receipt = {
    schema_version: 1,
    status: counts.running === 0 && counts.queued === 0 && activeWorkers === 0 && retainedWorkspacesAreTerminal ? "PASS" : "FAIL",
    service_stopped: true,
    running_jobs: counts.running,
    queued_jobs: counts.queued,
    active_workers: activeWorkers,
    temporary_workspaces: 0,
    excluded_terminal_workspaces: workspaceIds.length,
    state_validation: "PASS",
  };
  requireCondition(receipt.status === "PASS", "CHECKPOINT_NOT_QUIESCENT", "ERROR", "HARNESS");
  const scratchRoot = path.join(configuration.attemptRoot, "scratch", "checkpoint-sources");
  ensureDirectory(scratchRoot);
  const stateRoot = path.join(scratchRoot, configuration.stage);
  const archiveHostPath = path.join(scratchRoot, `${configuration.stage}.stable-state.tar`);
  const archiveContainerPath = `/tmp/test-flow-stable-state-${configuration.stage.replaceAll(".", "-")}.tar`;
  const classificationContainerPath = `/tmp/test-flow-stable-state-${configuration.stage.replaceAll(".", "-")}-classification.json`;
  const classificationHostPath = path.join(stageRoot, "checkpoint-temporary-classification.json");
  requireCondition(!fs.existsSync(stateRoot) && !fs.existsSync(archiveHostPath), "CHECKPOINT_STAGING_EXISTS");
  const exportResult = await run("docker", dockerArgs(configuration.dockerContext, [
    "exec", state.active_container, "sh", "/test-flow-runtime/export-checkpoint.sh", archiveContainerPath, classificationContainerPath,
  ]), { forward: false });
  const classificationCopy = await run("docker", dockerArgs(configuration.dockerContext, [
    "cp", `${state.active_container}:${classificationContainerPath}`, classificationHostPath,
  ]), { forward: false });
  const classification = classificationCopy.status === 0 ? readJson(classificationHostPath) : null;
  if (exportResult.status !== 0) {
    const code = classification?.status === "FAIL" && /^CHECKPOINT_[A-Z0-9_]+$/.test(classification.code)
      ? classification.code
      : "CHECKPOINT_STABLE_EXPORT_FAILED";
    throw new StageError(code, "ERROR", "HARNESS");
  }
  requireCondition(classification?.schema_version === 1 && classification.status === "PASS" && classification.code === null && classification.outbox_clear === true, "CHECKPOINT_TEMPORARY_CLASSIFICATION_RECEIPT_INVALID");
  try {
    await docker(configuration.dockerContext, ["cp", `${state.active_container}:${archiveContainerPath}`, archiveHostPath], { forward: false });
    const extraction = extractCheckpointSourceArchive({ archivePath: archiveHostPath, targetRoot: stateRoot });
    const workspaces = path.join(stateRoot, "tmp", "workspaces");
    requireCondition(fs.existsSync(workspaces) && fs.readdirSync(workspaces).length === 0 && !fs.existsSync(path.join(stateRoot, ".instance.lock")), "CHECKPOINT_STABLE_LAYOUT_INVALID");
    writeNew(path.join(stageRoot, "checkpoint-stable-export.json"), {
      schema_version: 1,
      status: extraction.status,
      entry_count: extraction.entry_count,
      portable_digest: extraction.portable_digest,
      excluded_instance_lock: true,
      excluded_terminal_workspaces: workspaceIds.length,
      excluded_completed_uploads: classification.excluded_completed_uploads,
      excluded_processed_proposal_stages: classification.excluded_processed_proposal_stages,
      outbox_clear: true,
      temporary_layout_empty: true,
    });
  } finally {
    try { await docker(configuration.dockerContext, ["exec", state.active_container, "rm", "-f", archiveContainerPath, classificationContainerPath], { forward: false }); } catch {}
    try { fs.rmSync(archiveHostPath, { force: true }); } catch {}
  }
  const adapterContinuation = {
    adapter_state_schema_version: 4,
    adapter_case_input_digest: state.release_case?.input_digest ?? null,
    adapter_case_scenario_id: state.release_case?.scenario_id ?? null,
    adapter_case_skill_id: state.release_case?.skill_id ?? null,
    adapter_case_id: state.case_id ?? null,
    adapter_attachment_id: state.attachment_id ?? null,
    adapter_prepared_case_revision: state.prepared_case_revision ?? null,
    adapter_prepare_expected_case_revision: state.prepare_expected_case_revision ?? null,
    adapter_case_revision: state.case_revision ?? null,
    adapter_status: state.status ?? null,
    adapter_resolved_case_revision: state.resolved_case_revision ?? null,
    adapter_observed_statuses: state.observed_statuses ?? [],
    adapter_audited_service_job_ids: state.audited_service_job_ids ?? [],
    adapter_request_create: state.request_ids?.create ?? null,
    adapter_request_prepare: state.request_ids?.prepare ?? null,
    adapter_request_submit_attachment: state.request_ids?.submit_attachment ?? null,
    adapter_request_submit_inputs: state.request_ids?.submit_inputs ?? null,
    adapter_upload_method: state.upload_descriptor?.method ?? null,
    adapter_upload_max_bytes: state.upload_descriptor?.max_bytes ?? null,
    adapter_upload_expires_at: state.upload_descriptor?.expires_at ?? null,
    adapter_upload_content_length: state.upload_descriptor?.required_headers?.["Content-Length"] ?? null,
    adapter_upload_content_type: state.upload_descriptor?.required_headers?.["Content-Type"] ?? null,
    adapter_upload_idempotency_key: state.upload_descriptor?.required_headers?.["Idempotency-Key"] ?? null,
    adapter_upload_sha256: state.upload_descriptor?.required_headers?.["X-Content-SHA256"] ?? null,
    adapter_public_artifact_id: state.public_artifact?.artifact_id ?? null,
    adapter_public_artifact_kind: state.public_artifact?.kind ?? null,
    adapter_public_artifact_name: state.public_artifact?.name ?? null,
    adapter_public_artifact_content_type: state.public_artifact?.content_type ?? null,
    adapter_public_artifact_size: state.public_artifact?.size ?? null,
    adapter_public_artifact_sha256: state.public_artifact?.sha256 ?? null,
    adapter_public_artifact_created_at: state.public_artifact?.created_at ?? null,
    adapter_public_archive_id: state.public_result_archive?.artifact_id ?? null,
    adapter_public_archive_kind: state.public_result_archive?.kind ?? null,
    adapter_public_archive_name: state.public_result_archive?.name ?? null,
    adapter_public_archive_content_type: state.public_result_archive?.content_type ?? null,
    adapter_public_archive_size: state.public_result_archive?.size ?? null,
    adapter_public_archive_sha256: state.public_result_archive?.sha256 ?? null,
    adapter_public_archive_created_at: state.public_result_archive?.created_at ?? null,
  };
  requireCondition(Object.values(adapterContinuation).every((value) => value === null || ["string", "number", "boolean"].includes(typeof value) || (Array.isArray(value) && value.every((entry) => entry === null || ["string", "number", "boolean"].includes(typeof entry)))), "CHECKPOINT_CONTINUATION_NOT_FLAT");
  writeNew(checkpointPath, {
    schema_version: 1,
    state_root: stateRoot,
    continuation: { ...continuation, ...adapterContinuation },
    quiescence_receipt: receipt,
  });
  return receipt;
}

function addUsage(state, usage) {
  state.usage = sumUsage([state.usage, usage]);
}

function validSuccessfulInvocationReceipt(invocation) {
  return invocation?.schema_version === 3
    && invocation.usage_complete === true
    && isCompleteUsage(invocation.usage)
    && invocation.terminal?.subtype === "success"
    && invocation.terminal?.is_error === false
    && invocation.wrapper_outcome?.schema_version === 1
    && invocation.wrapper_outcome?.status === "PASS"
    && invocation.wrapper_outcome?.code === null;
}

function hostInvocation(phase, audit, caps) {
  return {
    schema_version: 3,
    invocation_id: `host-client:${phase}`,
    class: "host-client",
    effective_model: audit.init.effective_model,
    effective_caps: caps,
    usage_complete: true,
    usage: audit.usage,
    terminal: audit.terminal,
    turns: audit.turns,
    wrapper_outcome: { schema_version: 1, status: "PASS", code: null },
    hard_cap_enforcement: {
      turns: "claude-cli",
      cost_usd: "claude-cli",
      hard_timeout_seconds: "host-process-watchdog",
      total_tokens: `terminal-usage-postcondition:${TOKEN_USAGE_FORMULA}`,
    },
  };
}

async function auditServiceAgentUsage(configuration, state, instance) {
  const output = `/evidence/stages/${configuration.stage}/service-agent-usage-${instance}.json`;
  const args = [
    "exec", state.active_container,
    "/opt/venvs/xiaodao/bin/python", "-I", "/test-flow-runtime/audit_service_agent_usage.py",
    "--jobs-root", "/var/lib/problem-locator/jobs",
    "--output", output,
    "--model", RELEASE_MODEL,
    "--max-turns", String(configuration.serviceAgentCaps.max_turns),
    "--max-total-tokens", String(configuration.serviceAgentCaps.max_total_tokens),
    "--max-budget-usd", String(configuration.serviceAgentCaps.max_budget_usd),
    "--hard-timeout-seconds", String(configuration.serviceAgentCaps.hard_timeout_seconds),
  ];
  for (const jobId of state.audited_service_job_ids ?? []) args.push("--exclude-job-id", jobId);
  await docker(configuration.dockerContext, args);
  const receipt = readJson(path.join(configuration.stageRoot, `service-agent-usage-${instance}.json`));
  requireCondition(
    receipt?.schema_version === 3
      && receipt.status === "PASS"
      && receipt.usage_complete === true
      && receipt.token_formula === TOKEN_USAGE_FORMULA
      && Array.isArray(receipt.invocations)
      && receipt.invocations.every(validSuccessfulInvocationReceipt)
      && Array.isArray(receipt.new_job_ids),
    "SERVICE_AGENT_USAGE_RECEIPT_INVALID",
  );
  state.audited_service_job_ids = [...new Set([...(state.audited_service_job_ids ?? []), ...receipt.new_job_ids])].sort();
  atomicState(configuration.statePath, state);
  return receipt.invocations;
}

function stageReceipt(configuration, value) {
  const receiptPath = path.join(configuration.stageRoot, "adapter-result.json");
  const invocations = value.invocations ?? [];
  requireCondition(
    Array.isArray(invocations) && invocations.every(validSuccessfulInvocationReceipt),
    "MODEL_INVOCATION_RECEIPT_INVALID",
  );
  const usage = value.usage ? normalizeUsage(value.usage) : sumUsage(invocations.map((invocation) => invocation.usage));
  const usageComplete = isCompleteUsage(usage);
  writeNew(receiptPath, {
    schema_version: 3,
    stage_id: configuration.stage,
    gate_id: configuration.gateId,
    runtime_profile_digest: configuration.runtimeProfileDigest,
    effective_caps: null,
    usage_complete: usageComplete,
    ...value,
    invocations,
    usage,
  });
}

function restoredArtifact(continuation, prefix, publicBaseUrl, caseId) {
  const artifactId = continuation[`${prefix}_id`];
  if (!artifactId) return null;
  return {
    artifact_id: artifactId,
    kind: continuation[`${prefix}_kind`],
    name: continuation[`${prefix}_name`],
    content_type: continuation[`${prefix}_content_type`],
    size: continuation[`${prefix}_size`],
    sha256: continuation[`${prefix}_sha256`],
    created_at: continuation[`${prefix}_created_at`],
    download_url: `${publicBaseUrl}/api/v1/artifacts/${artifactId}/content?case_id=${caseId}`,
  };
}

async function applyRestoredCheckpoint(configuration, state) {
  requireCondition(configuration.track === "dev", "CHECKPOINT_RESTORE_RELEASE_FORBIDDEN", "BLOCKED", "INFRA");
  requireCondition(configuration.restoredDataRoot && configuration.restoredContinuation && configuration.restoredCheckpointId, "CHECKPOINT_RESTORE_INPUT_MISSING");
  const continuation = readJson(configuration.restoredContinuation);
  requireCondition(
    continuation?.schema_version === 1
      && continuation.release_eligible === false
      && continuation.next_stage === configuration.stage
      && continuation.adapter_state_schema_version === 4
      && continuation.adapter_case_input_digest === configuration.releaseCase.input_digest
      && continuation.adapter_case_scenario_id === configuration.releaseCase.scenario_id
      && continuation.adapter_case_skill_id === configuration.releaseCase.skill.id,
    "CHECKPOINT_CONTINUATION_INVALID",
  );
  if (state.current_instance) {
    const discarded = await stopService(configuration, state, { indexLabel: "restore-discarded-route" });
    requireCondition(discarded.service_invocations.length === 0, "CHECKPOINT_RESTORE_FRESH_ENVIRONMENT_MODEL_ACTIVITY", "FAIL", "CONTRACT");
  }
  await docker(configuration.dockerContext, [
    "exec", state.active_container,
    "sh", "-eu", "-c",
    'test "$1" = /var/lib/problem-locator; find "$1" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +',
    "test-flow-checkpoint-restore", "/var/lib/problem-locator",
  ]);
  await docker(configuration.dockerContext, ["cp", `${configuration.restoredDataRoot}/.`, `${state.active_container}:/var/lib/problem-locator`]);
  await docker(configuration.dockerContext, ["exec", state.active_container, "chown", "-R", "10001:10001", "/var/lib/problem-locator"]);
  const validation = await docker(configuration.dockerContext, [
    "exec", state.active_container,
    "runuser", "-u", "plagent", "--",
    "/usr/bin/env", "-i",
    "HOME=/run/plagent-claude", "LANG=C.UTF-8", "PATH=/opt/venvs/xiaodao/bin:/usr/local/bin:/usr/bin:/bin", "PYTHONNOUSERSITE=1",
    "/opt/venvs/xiaodao/bin/python", "-I", "-m", "problem_locator", "validate-state", "--data-root", "/var/lib/problem-locator",
  ], { forward: false });
  const validationReport = JSON.parse(validation.stdout);
  requireCondition(validationReport.valid === true && Array.isArray(validationReport.errors) && validationReport.errors.length === 0, "CHECKPOINT_RESTORED_STATE_INVALID", "FAIL", "PRODUCT");

  const caseId = continuation.adapter_case_id;
  Object.assign(state, {
    case_id: caseId,
    attachment_id: continuation.adapter_attachment_id,
    prepared_case_revision: continuation.adapter_prepared_case_revision,
    prepare_expected_case_revision: continuation.adapter_prepare_expected_case_revision,
    case_revision: continuation.adapter_case_revision,
    status: continuation.adapter_status,
    resolved_case_revision: continuation.adapter_resolved_case_revision,
    observed_statuses: continuation.adapter_observed_statuses,
    audited_service_job_ids: continuation.adapter_audited_service_job_ids,
    request_ids: {
      create: continuation.adapter_request_create,
      prepare: continuation.adapter_request_prepare,
      submit_attachment: continuation.adapter_request_submit_attachment,
      submit_inputs: continuation.adapter_request_submit_inputs,
    },
    client_calls: [],
    usage: zeroUsage(),
  });
  if (continuation.adapter_upload_method) {
    state.upload_descriptor = {
      attachment_id: state.attachment_id,
      method: continuation.adapter_upload_method,
      url: `${state.public_base_url}/api/v1/attachments/${state.attachment_id}/content`,
      required_headers: {
        "Content-Length": continuation.adapter_upload_content_length,
        "Content-Type": continuation.adapter_upload_content_type,
        "Idempotency-Key": continuation.adapter_upload_idempotency_key,
        "X-Content-SHA256": continuation.adapter_upload_sha256,
      },
      max_bytes: continuation.adapter_upload_max_bytes,
      expires_at: continuation.adapter_upload_expires_at,
    };
  }
  state.public_artifact = restoredArtifact(continuation, "adapter_public_artifact", state.public_base_url, caseId);
  state.public_result_archive = restoredArtifact(continuation, "adapter_public_archive", state.public_base_url, caseId);
  atomicState(configuration.statePath, state);
  if (configuration.stage === "journey.cross-job.upload") await startService(configuration, state, "upload");
  else if (configuration.stage === "journey.cross-job.diagnose") await startService(configuration, state, "diagnose");
  else requireCondition(configuration.stage === "journey.cross-job.publish-restart", "CHECKPOINT_RESTORE_STAGE_UNSUPPORTED");
  writeNew(path.join(configuration.stageRoot, "adapter-restore-applied.json"), {
    schema_version: 2,
    status: "PASS",
    checkpoint_id: configuration.restoredCheckpointId,
    next_stage: configuration.stage,
    restored_data_root: "VERIFIED",
  });
}

async function execute(configuration) {
  requireCondition(process.platform === configuration.expectedHostPlatform && configuration.client === configuration.expectedClient, "CROSS_JOB_ADAPTER_HOST_REQUIRED", "BLOCKED", "INFRA");
  requireCondition(["release", "dev"].includes(configuration.track), "CROSS_JOB_ADAPTER_TRACK_UNSUPPORTED", "BLOCKED", "INFRA");
  if (configuration.expectedDockerContext !== "default") requireCondition(configuration.dockerContext === configuration.expectedDockerContext, "CROSS_JOB_ADAPTER_DOCKER_CONTEXT", "BLOCKED", "INFRA");
  requireCondition(configuration.sourceSnapshotDigest && configuration.sourceSnapshotManifest && configuration.logparseSource && configuration.mcpSource && configuration.claudeEntry && configuration.claudeSettings, "CROSS_JOB_ADAPTER_INPUT_MISSING", "BLOCKED", "INFRA");
  if (configuration.track === "release") requireCondition(!configuration.restoredDataRoot && !configuration.restoredCheckpointId, "RELEASE_CHECKPOINT_RESTORE_FORBIDDEN", "BLOCKED", "INFRA");
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
    const { state, freshAdmission, dfxProbe } = await createFreshEnvironment(configuration, configuration.stageRoot, runtimeIdentity);
    const terminalCorrespondence = configuration.terminalAfterStage ? await stopService(configuration, state) : null;
    const invocations = terminalCorrespondence?.service_invocations ?? [];
    requireCondition(invocations.length === 0, "ENVIRONMENT_UNEXPECTED_MODEL_INVOCATION", "FAIL", "CONTRACT");
    stageReceipt(configuration, { status: "PASS", fresh_admission: freshAdmission, server_dfx_probe: dfxProbe, client_tool_calls: 0, server_tool_calls: 1, terminal_correspondence: terminalCorrespondence, checkpoint_ready: false, invocations });
    return;
  }

  requireCondition(fs.existsSync(configuration.statePath), "ADAPTER_STATE_MISSING");
  const state = readJson(configuration.statePath);
  requireCondition(state.schema_version === 1 && state.run_id === path.basename(configuration.attemptRoot), "ADAPTER_STATE_INVALID");
  requireCondition(canonicalJson(state.runtime_identity) === canonicalJson(runtimeIdentity), "RELEASE_RUNTIME_IDENTITY_DRIFT", "BLOCKED", "INFRA");
  if (configuration.restoredDataRoot) await applyRestoredCheckpoint(configuration, state);

  if (configuration.stage === "journey.cross-job.route") {
    requireCondition(configuration.hardCaps !== null, "ROUTE_HARD_CAPS_MISSING", "BLOCKED", "INFRA");
    const audit = await runClaude(configuration, state, configuration.stageRoot, "phase1", phaseOnePrompt(configuration.releaseCase, state.request_ids, state.archive), configuration.hardCaps.max_turns, configuration.hardCaps.max_budget_usd);
    const summary = validatePhaseOne(audit, configuration.releaseCase, state.request_ids, state.archive, state.public_base_url);
    Object.assign(state, summary);
    state.client_calls.push(...audit.records.map((record, index) => ({ phase: "phase1", ordinal: state.client_calls.length + index, tool_name: record.tool_name, input: record.input })));
    addUsage(state, audit.usage);
    atomicState(configuration.statePath, state);
    const correspondence = await stopService(configuration, state);
    const jobTypes = correspondence.service_invocations.map((invocation) => invocation.job_type).sort();
    requireCondition(jobTypes.filter((item) => item === "ROUTE").length === 1 && jobTypes.includes("DIAGNOSE") && jobTypes.every((item) => ["DIAGNOSE", "ROUTE"].includes(item)), "ROUTE_SERVICE_AGENT_INVOCATIONS", "FAIL", "CONTRACT");
    const checkpoint = await createCheckpointSource(configuration, state, {
      case_id: state.case_id,
      attachment_id: state.attachment_id,
      prepared_case_revision: state.prepared_case_revision,
      request_ids: Object.values(state.request_ids),
    });
    if (!configuration.terminalAfterStage) await startService(configuration, state, "upload");
    const invocations = [hostInvocation("route", audit, configuration.hardCaps), ...correspondence.service_invocations];
    stageReceipt(configuration, { status: "PASS", client_tool_calls: audit.records.length, server_tool_calls: correspondence.server_completed, checkpoint_ready: checkpoint.status === "PASS", invocations });
    return;
  }

  if (configuration.stage === "journey.cross-job.upload") {
    const upload = await uploadAttachment(configuration, state, configuration.stageRoot);
    Object.assign(state, upload);
    atomicState(configuration.statePath, state);
    const correspondence = await stopService(configuration, state);
    requireCondition(correspondence.service_invocations.length === 0, "UPLOAD_UNEXPECTED_MODEL_INVOCATION", "FAIL", "CONTRACT");
    const checkpoint = await createCheckpointSource(configuration, state, {
      case_id: state.case_id,
      attachment_id: state.attachment_id,
      case_revision: state.case_revision,
      attachment_status: state.status,
      request_ids: Object.values(state.request_ids),
    });
    if (!configuration.terminalAfterStage) await startService(configuration, state, "diagnose");
    stageReceipt(configuration, { status: "PASS", client_tool_calls: 0, server_tool_calls: correspondence.server_completed, checkpoint_ready: checkpoint.status === "PASS", browser_upload: upload.browser_upload, invocations: [] });
    return;
  }

  if (configuration.stage === "journey.cross-job.diagnose") {
    requireCondition(configuration.hardCaps !== null, "DIAGNOSE_HARD_CAPS_MISSING", "BLOCKED", "INFRA");
    const audit = await runClaude(configuration, state, configuration.stageRoot, "phase3", phaseThreePrompt(state, configuration.releaseCase), configuration.hardCaps.max_turns, configuration.hardCaps.max_budget_usd);
    const summary = validatePhaseThree(audit, state, configuration.releaseCase);
    const browserApi = await verifyResolvedWebApi(
      configuration,
      state,
      summary,
      configuration.stageRoot,
    );
    Object.assign(state, summary);
    state.client_calls.push(...audit.records.map((record, index) => ({ phase: "phase3", ordinal: state.client_calls.length + index, tool_name: record.tool_name, input: record.input })));
    addUsage(state, audit.usage);
    atomicState(configuration.statePath, state);
    const correspondence = await stopService(configuration, state);
    const jobTypes = correspondence.service_invocations.map((invocation) => invocation.job_type).sort();
    requireCondition(jobTypes.includes("DIAGNOSE") && jobTypes.includes("REVIEW") && jobTypes.every((item) => ["DIAGNOSE", "REVIEW"].includes(item)), "DIAGNOSE_SERVICE_AGENT_INVOCATIONS", "FAIL", "CONTRACT");
    const checkpoint = await createCheckpointSource(configuration, state, {
      case_id: state.case_id,
      attachment_id: state.attachment_id,
      resolved_case_revision: state.resolved_case_revision,
      public_artifact_id: state.public_artifact.artifact_id,
      public_archive_id: state.public_result_archive.artifact_id,
      observed_statuses: state.observed_statuses,
    });
    const invocations = [hostInvocation("diagnose", audit, configuration.hardCaps), ...correspondence.service_invocations];
    stageReceipt(configuration, { status: "PASS", client_tool_calls: audit.records.length, server_tool_calls: correspondence.server_completed, checkpoint_ready: checkpoint.status === "PASS", browser_api: browserApi, invocations });
    return;
  }

  if (configuration.stage === "journey.cross-job.publish-restart") {
    requireCondition(!state.current_instance, "PUBLISH_RESTART_SERVICE_STILL_RUNNING");
    await docker(configuration.dockerContext, ["container", "stop", "--time", "10", state.initial_container]);
    await createContainer(configuration, state, state.restart_container, "restart", configuration.stage, true);
    await initializeContainer(configuration, state, state.restart_container, "restart", configuration.stage);
    // This phase is deliberately read-only (get_case + list_artifacts), so the
    // business Journey writer has no state transition to emit. Diagnostics
    // remain mandatory and are still used for exact MCP correspondence.
    await startService(configuration, state, "restart", { allowEmptyJourney: true });
    requireCondition(configuration.hardCaps !== null, "PUBLISH_RESTART_HARD_CAPS_MISSING", "BLOCKED", "INFRA");
    const audit = await runClaude(configuration, state, configuration.stageRoot, "restart", restartPrompt(state), configuration.hardCaps.max_turns, configuration.hardCaps.max_budget_usd);
    validateRestart(audit, state, configuration.releaseCase);
    state.client_calls.push(...audit.records.map((record, index) => ({ phase: "restart", ordinal: state.client_calls.length + index, tool_name: record.tool_name, input: record.input })));
    addUsage(state, audit.usage);
    atomicState(configuration.statePath, state);
    await downloadArtifacts(configuration, state, configuration.stageRoot);
    const correspondence = await stopService(configuration, state);
    requireCondition(correspondence.service_invocations.length === 0, "RESTART_UNEXPECTED_MODEL_INVOCATION", "FAIL", "CONTRACT");
    const checkpoint = await createCheckpointSource(configuration, state, {
      case_id: state.case_id,
      resolved_case_revision: state.resolved_case_revision,
      public_artifact_id: state.public_artifact.artifact_id,
      public_archive_id: state.public_result_archive.artifact_id,
      restart_verified: true,
    });
    writeNew(path.join(configuration.stageRoot, "client-server-correspondence.json"), { schema_version: 1, ...correspondence });
    stageReceipt(configuration, { status: "PASS", client_tool_calls: audit.records.length, server_tool_calls: correspondence.server_completed, checkpoint_ready: checkpoint.status === "PASS", restart_verified: true, invocations: [hostInvocation("publish-restart", audit, configuration.hardCaps)] });
    return;
  }

  throw new StageError("CROSS_JOB_ADAPTER_STAGE_UNKNOWN");
}

const parsed = parseArguments(process.argv.slice(2));
const values = parsed.values;
const configuration = {
  stage: values.stage,
  repoRoot: values.repo_root && path.resolve(values.repo_root),
  attemptRoot: values.attempt_root && path.resolve(values.attempt_root),
  client: values.client,
  track: values.track,
  sourceSnapshotDigest: values.source_snapshot_digest,
  sourceSnapshotManifest: values.source_snapshot_manifest && path.resolve(values.source_snapshot_manifest),
  claudeEntry: values.claude_entry && path.resolve(values.claude_entry),
  claudeSettings: values.claude_settings && path.resolve(values.claude_settings),
  dockerContext: values.docker_context,
  cacheRoot: values.cache_root && path.resolve(values.cache_root),
  logparseSource: values.logparse_source && path.resolve(values.logparse_source),
  mcpSource: values.mcp_source && path.resolve(values.mcp_source),
  baseImage: values.base_image,
  resourceRegistry: values.resource_registry && path.resolve(values.resource_registry),
  resourceLabel: values.resource_label,
  gateId: values.gate_id,
  runtimeProfileDigest: values.runtime_profile_digest,
  expectedChromeVersion: values.chrome_version,
  expectedChromeSha256: values.chrome_sha256,
  checkpointOutputSource: values.checkpoint_output_source && path.resolve(values.checkpoint_output_source),
  restoredDataRoot: values.restored_data_root && path.resolve(values.restored_data_root),
  restoredCheckpointId: values.restored_checkpoint_id,
  restoredContinuation: values.restored_continuation && path.resolve(values.restored_continuation),
  freshDataRoot: parsed.flags.has("--fresh-data-root"),
  terminalAfterStage: parsed.flags.has("--terminal-after-stage"),
  expectedClient: process.env.TEST_FLOW_FIRST_PARTY_CLIENT,
  expectedHostPlatform: process.env.TEST_FLOW_FIRST_PARTY_HOST_PLATFORM,
  expectedDockerContext: process.env.TEST_FLOW_FIRST_PARTY_DOCKER_CONTEXT,
  hardCaps: values.max_turns ? {
    max_turns: Number(values.max_turns),
    max_total_tokens: Number(values.max_total_tokens),
    max_budget_usd: Number(values.max_budget_usd),
    hard_timeout_seconds: Number(values.hard_timeout_seconds),
  } : null,
  serviceAgentCaps: {
    max_turns: Number(values.service_agent_max_turns),
    max_total_tokens: Number(values.service_agent_max_total_tokens),
    max_budget_usd: Number(values.service_agent_max_budget_usd),
    hard_timeout_seconds: Number(values.service_agent_hard_timeout_seconds),
  },
};
configuration.statePath = configuration.attemptRoot && path.join(configuration.attemptRoot, "scratch", "cross-job", "state.json");
configuration.stageRoot = configuration.attemptRoot && configuration.stage && path.join(configuration.attemptRoot, "payload", "stages", configuration.stage);

let receiptWritten = false;
try {
  for (const [name, value] of Object.entries({ stage: configuration.stage, gateId: configuration.gateId, runtimeProfileDigest: configuration.runtimeProfileDigest, expectedClient: configuration.expectedClient, expectedHostPlatform: configuration.expectedHostPlatform, expectedChromeVersion: configuration.expectedChromeVersion, expectedChromeSha256: configuration.expectedChromeSha256, repoRoot: configuration.repoRoot, attemptRoot: configuration.attemptRoot, sourceSnapshotDigest: configuration.sourceSnapshotDigest, sourceSnapshotManifest: configuration.sourceSnapshotManifest, resourceRegistry: configuration.resourceRegistry, resourceLabel: configuration.resourceLabel, baseImage: configuration.baseImage })) {
    requireCondition(Boolean(value), `ADAPTER_REQUIRED_${name.toUpperCase()}`);
  }
  const observedChrome = chromeIdentity();
  requireCondition(observedChrome.status === "PRESENT" && observedChrome.version === configuration.expectedChromeVersion && observedChrome.executable_sha256 === configuration.expectedChromeSha256, "CHROME_IDENTITY_DRIFT", "BLOCKED", "INFRA");
  if (configuration.hardCaps) {
    requireCondition(Number.isSafeInteger(configuration.hardCaps.max_turns) && configuration.hardCaps.max_turns > 0, "ADAPTER_MAX_TURNS_INVALID");
    requireCondition(Number.isSafeInteger(configuration.hardCaps.max_total_tokens) && configuration.hardCaps.max_total_tokens > 0, "ADAPTER_MAX_TOKENS_INVALID");
    requireCondition(Number.isFinite(configuration.hardCaps.max_budget_usd) && configuration.hardCaps.max_budget_usd > 0, "ADAPTER_MAX_BUDGET_INVALID");
    requireCondition(Number.isSafeInteger(configuration.hardCaps.hard_timeout_seconds) && configuration.hardCaps.hard_timeout_seconds > 0, "ADAPTER_HARD_TIMEOUT_INVALID");
  }
  requireCondition(Number.isSafeInteger(configuration.serviceAgentCaps.max_turns) && configuration.serviceAgentCaps.max_turns > 0, "ADAPTER_SERVICE_MAX_TURNS_INVALID");
  requireCondition(Number.isSafeInteger(configuration.serviceAgentCaps.max_total_tokens) && configuration.serviceAgentCaps.max_total_tokens > 0, "ADAPTER_SERVICE_MAX_TOKENS_INVALID");
  requireCondition(Number.isFinite(configuration.serviceAgentCaps.max_budget_usd) && configuration.serviceAgentCaps.max_budget_usd > 0, "ADAPTER_SERVICE_MAX_BUDGET_INVALID");
  requireCondition(Number.isSafeInteger(configuration.serviceAgentCaps.hard_timeout_seconds) && configuration.serviceAgentCaps.hard_timeout_seconds > 0, "ADAPTER_SERVICE_HARD_TIMEOUT_INVALID");
  configuration.releaseCase = selectedReleaseCase(configuration.repoRoot);
  ensureDirectory(configuration.stageRoot);
  await execute(configuration);
  receiptWritten = true;
  process.stdout.write("TEST_FLOW_PROGRESS stage.completed\n");
} catch (error) {
  const failure = error instanceof StageError ? error : new StageError(`ADAPTER_UNEXPECTED:${String(error?.message ?? error)}`);
  try { await archiveFailureServiceEvidence(configuration); } catch {}
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
          schema_version: 3,
          stage_id: configuration.stage ?? "unknown",
          gate_id: configuration.gateId ?? null,
          runtime_profile_digest: configuration.runtimeProfileDigest ?? null,
          effective_caps: configuration.hardCaps,
          usage_complete: false,
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
