import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import {
  RELEASE_CLAUDE_CLI_SHA256,
  RELEASE_CLAUDE_VERSION,
  RELEASE_CLAUDE_VERSION_OUTPUT,
} from "../lib/release-inputs.mjs";
import { runSync, sha256File } from "../lib/util.mjs";

const ADAPTER_ROOT = path.dirname(fileURLToPath(import.meta.url));

function argument(name) {
  const index = process.argv.indexOf(name);
  return index === -1 ? null : process.argv[index + 1];
}

function fail(code, message) {
  process.stderr.write(`${code}: ${message}\n`);
  process.exitCode = 1;
}

function readLines(filePath) {
  return fs.readFileSync(filePath, "utf8").split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
}

async function waitFor(filePath, child, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (fs.existsSync(filePath)) return JSON.parse(fs.readFileSync(filePath, "utf8"));
    if (child.exitCode !== null) throw new Error("probe server exited before readiness");
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error("probe server readiness timeout");
}

async function main() {
  const repoRoot = path.resolve(argument("--repo-root") ?? path.join(ADAPTER_ROOT, "..", "..", ".."));
  const outputRoot = path.resolve(argument("--output-root") ?? fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-host-probe-")));
  fs.mkdirSync(outputRoot, { recursive: true, mode: 0o700 });
  const claude = argument("--claude-entry");
  const runtimeProfileDigest = argument("--runtime-profile-digest");
  if (!runtimeProfileDigest || !/^[a-f0-9]{64}$/.test(runtimeProfileDigest)) throw new Error("--runtime-profile-digest is required");
  if (!claude || !path.isAbsolute(claude) || path.basename(claude) !== "cli.js" || !fs.existsSync(claude) || !fs.statSync(claude).isFile()) {
    throw new Error("--claude-entry must identify an existing absolute official npm cli.js");
  }
  if (sha256File(claude) !== RELEASE_CLAUDE_CLI_SHA256) throw new Error("Claude cli.js SHA-256 is not the frozen 2.1.89 baseline");
  const packageManifest = JSON.parse(fs.readFileSync(path.join(path.dirname(claude), "package.json"), "utf8"));
  if (packageManifest.name !== "@anthropic-ai/claude-code" || packageManifest.version !== RELEASE_CLAUDE_VERSION) {
    throw new Error("Claude npm package identity is not @anthropic-ai/claude-code@2.1.89");
  }
  const version = runSync(process.execPath, [claude, "--version"]);
  if (version.status !== 0 || version.stdout.trim() !== RELEASE_CLAUDE_VERSION_OUTPUT) {
    throw new Error(`unsupported Claude Code version output: ${version.stdout.trim()}`);
  }

  const fixture = path.join(repoRoot, "tools", "test-flow", "adapters", "fixtures", "claude-flat-probe.mjs");
  const server = spawn(process.execPath, [fixture, outputRoot], { stdio: ["ignore", "pipe", "pipe"], detached: false });
  const serverStderr = [];
  server.stderr.on("data", (chunk) => serverStderr.push(chunk));
  try {
    const ready = await waitFor(path.join(outputRoot, "servers-ready.json"), server, 10_000);
    const settingsPath = path.join(outputRoot, "settings.json");
    const mcpPath = path.join(outputRoot, "mcp.json");
    const configRoot = path.join(outputRoot, "claude-config");
    fs.mkdirSync(configRoot, { mode: 0o700 });
    fs.writeFileSync(settingsPath, "{}\n", { encoding: "utf8", mode: 0o600 });
    fs.writeFileSync(mcpPath, `${JSON.stringify({ mcpServers: { "problem-locator": { type: "http", url: `http://127.0.0.1:${ready.mcp}/mcp` } } })}\n`, { encoding: "utf8", mode: 0o600 });
    const environment = { ...process.env };
    environment.ANTHROPIC_AUTH_TOKEN = "flat-probe-token";
    environment.ANTHROPIC_BASE_URL = `http://127.0.0.1:${ready.api}`;
    environment.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1";
    environment.CLAUDE_CONFIG_DIR = configRoot;
    for (const name of ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]) delete environment[name];
    environment.NO_PROXY = "127.0.0.1,localhost";
    environment.no_proxy = environment.NO_PROXY;
    const args = [
      "-p",
      "--output-format", "stream-json",
      "--verbose",
      "--dangerously-skip-permissions",
      "--no-session-persistence",
      "--mcp-config", mcpPath,
      "--strict-mcp-config",
      "--settings", settingsPath,
      "--setting-sources", "user",
      "Call the Problem Locator create_case tool exactly once.",
    ];
    const child = spawn(process.execPath, [claude, ...args], { cwd: outputRoot, env: environment, stdio: ["ignore", "pipe", "pipe"] });
    const stdout = [];
    const stderr = [];
    child.stdout.on("data", (chunk) => { stdout.push(chunk); process.stdout.write(chunk); });
    child.stderr.on("data", (chunk) => { stderr.push(chunk); process.stderr.write(chunk); });
    const exitCode = await new Promise((resolve, reject) => {
      child.once("error", reject);
      child.once("exit", resolve);
    });
    if (exitCode !== 0) throw new Error(`Claude probe exited ${exitCode}: ${Buffer.concat(stderr).toString("utf8").slice(-2000)}`);
    if (!Buffer.concat(stdout).toString("utf8").includes("DONE")) throw new Error("Claude probe did not reach DONE");

    const apiEvents = readLines(path.join(outputRoot, "api-requests.jsonl"));
    const tools = apiEvents.flatMap((event) => Array.isArray(event.body?.tools) ? event.body.tools : []);
    const advertised = [...tools].reverse().find((tool) => String(tool?.name ?? "").endsWith("problem_locator_create_case"));
    const schema = advertised?.input_schema;
    if (!schema || schema.$defs || schema.properties?.problem_spec || schema.properties?.goals?.items?.type !== "string") {
      throw new Error("Claude Host did not receive the flat public MCP schema");
    }
    const mcpEvents = readLines(path.join(outputRoot, "mcp-requests.jsonl"));
    const call = [...mcpEvents].reverse().find((event) => event.body?.method === "tools/call" && event.body?.params?.arguments?.request_id === ready.request_id);
    const input = call?.body?.params?.arguments;
    if (!input || input.problem_spec || input.initial_user_facts || input.statement !== "连接失败") {
      throw new Error("Claude Host emitted a non-flat tool input");
    }
    if (fs.existsSync(path.join(outputRoot, ".problem-locator", "client-dfx.jsonl")) || fs.existsSync(path.join(outputRoot, "client-dfx.jsonl"))) {
      throw new Error("forbidden client DFX was created");
    }
    const bypass = await fetch(`http://127.0.0.1:${ready.mcp}/mcp`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        jsonrpc: "2.0", id: 99, method: "tools/call",
        params: { name: "problem_locator_create_case", arguments: { request_id: "bypass-composite", statement: "still flat otherwise", goals: ["reject removed field"], initial_user_fact_names: [], initial_user_fact_values: [], problem_spec: { statement: "removed" } } },
      }),
    });
    const rejected = await bypass.json();
    if (rejected.result?.structuredContent?.error?.code !== "VALIDATION_ERROR") throw new Error("server did not reject a removed composite field");
    fs.writeFileSync(path.join(outputRoot, "host-capability-result.json"), `${JSON.stringify({ schema_version: 2, status: "PASS", runtime_profile_digest: runtimeProfileDigest, client: process.platform === "darwin" ? "macos" : process.platform === "win32" ? "windows" : "linux", claude_version: version.stdout.trim(), claude_cli_sha256: RELEASE_CLAUDE_CLI_SHA256, distribution: "official-npm", flat_schema: true, flat_call: true, client_dfx_absent: true })}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
    process.stdout.write("TEST_FLOW_PROGRESS request.completed\n");
  } finally {
    server.kill("SIGTERM");
    await new Promise((resolve) => {
      if (server.exitCode !== null) resolve();
      else {
        server.once("exit", resolve);
        setTimeout(() => { if (server.exitCode === null) server.kill("SIGKILL"); }, 5000).unref();
      }
    });
    if (server.exitCode && server.exitCode !== 0 && server.exitCode !== 143) {
      process.stderr.write(Buffer.concat(serverStderr).toString("utf8"));
    }
  }
}

main().catch((error) => fail("HOST_CAPABILITY_FAILED", error.message));
