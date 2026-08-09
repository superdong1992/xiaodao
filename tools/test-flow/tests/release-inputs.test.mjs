import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  CLAUDE_SETTINGS_ENV_KEYS,
  RELEASE_BASE_IMAGE,
  RELEASE_CLAUDE_CLI_SHA256,
  RELEASE_CLAUDE_TARBALL_SHA256,
  RELEASE_CLAUDE_VERSION,
  RELEASE_HATCHLING_VERSION,
  RELEASE_UV_ARCHIVE_SHA256,
  claudeSettingsIdentity,
  materializeAttemptClaudeSettings,
  materializeClaudeSettings,
  validateClaudeDistribution,
} from "../lib/release-inputs.mjs";

const TOOL_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function settingsPayload(overrides = {}) {
  return {
    env: {
      ANTHROPIC_AUTH_TOKEN: "unit-test-high-entropy-auth-value",
      ANTHROPIC_BASE_URL: "https://provider.example.test/v1",
      ANTHROPIC_DEFAULT_HAIKU_MODEL: "deepseek-v4-flash[1m]",
      ANTHROPIC_DEFAULT_OPUS_MODEL: "deepseek-v4-flash[1m]",
      ANTHROPIC_DEFAULT_SONNET_MODEL: "deepseek-v4-flash[1m]",
      API_TIMEOUT_MS: "600000",
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
      ...overrides,
    },
    hooks: { PreToolUse: [{ matcher: "*", hooks: [{ type: "command", command: "forbidden-hook" }] }] },
    permissions: { allow: ["Bash(*)"] },
  };
}

test("Release settings materialization copies only the exact env allowlist and no Hooks", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-release-settings-"));
  try {
    const source = path.join(root, "source-settings.json");
    const target = path.join(root, "isolated", "settings.json");
    fs.writeFileSync(source, JSON.stringify(settingsPayload()), { encoding: "utf8", mode: 0o600 });
    const identity = claudeSettingsIdentity(source);
    assert.equal(identity.status, "PRESENT");
    assert.equal(identity.model, "deepseek-v4-flash[1m]");
    assert.equal(identity.endpoint, "https://provider.example.test");
    assert.equal(identity.hooks_copied, false);
    assert.equal(JSON.stringify(identity).includes("unit-test-high-entropy-auth-value"), false);

    materializeClaudeSettings(source, target);
    const copied = JSON.parse(fs.readFileSync(target, "utf8"));
    assert.deepEqual(Object.keys(copied), ["env"]);
    assert.deepEqual(Object.keys(copied.env).sort(), [...CLAUDE_SETTINGS_ENV_KEYS].sort());
    assert.equal(Object.hasOwn(copied, "hooks"), false);
    assert.equal(Object.hasOwn(copied, "permissions"), false);
    assert.equal(fs.statSync(target).mode & 0o777, 0o600);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("container settings are staged inside attempt scratch instead of binding the source path", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-release-settings-stage-"));
  try {
    const sourceRoot = path.join(root, "daemon-invisible-source");
    const attemptRoot = path.join(root, "shared-attempt");
    fs.mkdirSync(sourceRoot);
    fs.mkdirSync(attemptRoot);
    const source = path.join(sourceRoot, "settings.json");
    fs.writeFileSync(source, JSON.stringify(settingsPayload()), { encoding: "utf8", mode: 0o600 });
    const sourceIdentity = claudeSettingsIdentity(source);

    const staged = materializeAttemptClaudeSettings(source, attemptRoot, sourceIdentity.fingerprint);
    assert.equal(staged.path.startsWith(`${path.resolve(attemptRoot)}${path.sep}`), true);
    assert.notEqual(staged.path, source);
    assert.equal(staged.identity.fingerprint, sourceIdentity.fingerprint);
    assert.equal(fs.statSync(staged.path).mode & 0o777, 0o600);
    const copied = JSON.parse(fs.readFileSync(staged.path, "utf8"));
    assert.deepEqual(Object.keys(copied), ["env"]);
    assert.equal(Object.hasOwn(copied, "hooks"), false);

    const adapter = fs.readFileSync(path.join(TOOL_ROOT, "adapters", "macos-linux-release.mjs"), "utf8");
    assert.match(adapter, /src=\$\{configuration\.containerClaudeSettings\},dst=\/run\/host-claude-settings\.json,readonly/);
    assert.doesNotMatch(adapter, /src=\$\{configuration\.claudeSettings\},dst=\/run\/host-claude-settings\.json/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("macOS container initialization trusts only the three exact read-only source repositories", () => {
  const initializer = fs.readFileSync(path.join(TOOL_ROOT, "harness", "macos-initialize-container.sh"), "utf8");
  const trusted = [
    "/source/xiaodao/.git",
    "/source/logparse/.git",
    "/source/problem-locator-mcp/.git",
  ];
  for (const source of trusted) {
    assert.equal(initializer.includes(`git config --file "$source_git_config" --add safe.directory ${source}`), true);
    assert.equal(initializer.includes(`GIT_CONFIG_GLOBAL="$source_git_config" git -c core.autocrlf=false clone --no-hardlinks ${source.slice(0, -5)} `), true);
  }
  assert.match(initializer, /git config --file "\$source_git_config" --add safe\.directory ''/);
  assert.match(initializer, /chmod 0600 "\$source_git_config"/);
  assert.doesNotMatch(initializer, /safe\.directory\s+['\"]?\*['\"]?/);
  assert.doesNotMatch(initializer, /safe\.directory\s+\/source(?:\s|$)/);
  assert.match(initializer, /rm -f "\$source_git_config"/);
});

test("macOS adapter permits only the environment diagnostic outside Release", () => {
  const adapter = fs.readFileSync(path.join(TOOL_ROOT, "adapters", "macos-linux-release.mjs"), "utf8");
  assert.match(adapter, /configuration\.track === "dev" && configuration\.stage === "journey\.cross-job\.environment"/);
  assert.match(adapter, /configuration\.track === "release" \|\| devEnvironmentDiagnostic/);
  assert.match(adapter, /devEnvironmentDiagnostic \? await stopEnvironmentDiagnostic\(configuration, state\) : null/);
  assert.match(adapter, /return mergeEventParts\(configuration\.attemptRoot, state\.run_id, "diagnostics"\)/);
  assert.doesNotMatch(adapter, /configuration\.track === "dev" \|\| configuration\.track === "release"/);
});

test("offline container installation uses the build backend sealed into the v2 image", () => {
  const dockerfile = fs.readFileSync(path.join(TOOL_ROOT, "Dockerfile"), "utf8");
  const initializer = fs.readFileSync(path.join(TOOL_ROOT, "harness", "macos-initialize-container.sh"), "utf8");
  const cachePreparer = fs.readFileSync(path.join(TOOL_ROOT, "prepare-release-cache.mjs"), "utf8");
  assert.equal(RELEASE_HATCHLING_VERSION, "1.28.0");
  assert.equal(RELEASE_BASE_IMAGE.endsWith("-v2"), true);
  assert.match(dockerfile, /ARG HATCHLING_VERSION=1\.28\.0/);
  assert.match(dockerfile, /hatchling==\$\{HATCHLING_VERSION\}/);
  assert.match(dockerfile, /problem-locator\.e2e\.hatchling="\$\{HATCHLING_VERSION\}"/);
  assert.match(initializer, /--offline --no-deps --no-build-isolation --reinstall/);
  assert.match(cachePreparer, /HATCHLING_VERSION=\$\{RELEASE_HATCHLING_VERSION\}/);
  assert.match(cachePreparer, /hatchling_version: RELEASE_HATCHLING_VERSION/);
});

test("offline container installation copies runtime assets and rejects filesystem links", () => {
  const initializer = fs.readFileSync(path.join(TOOL_ROOT, "harness", "macos-initialize-container.sh"), "utf8");
  assert.match(initializer, /UV_LINK_MODE=copy UV_NO_PROGRESS=1 uv pip install/);
  assert.match(initializer, /installed_assets=\/opt\/venvs\/xiaodao\/lib\/python3\.12\/site-packages\/problem_locator\/runtime\/assets/);
  assert.match(initializer, /find "\$installed_assets" -xdev -type l -print -quit/);
  assert.match(initializer, /find "\$installed_assets" -xdev -type f -links \+1 -print -quit/);
  assert.match(initializer, /"uv_link_mode":"copy","installed_asset_hardlinks":0/);
});

test("non-root service trees are readable and traversable but never writable", () => {
  const initializer = fs.readFileSync(path.join(TOOL_ROOT, "harness", "macos-initialize-container.sh"), "utf8");
  const trees = "/opt/src /opt/e2e-skills /opt/venvs /opt/uv-python";
  assert.equal(initializer.includes(`chmod -R a+rX ${trees}`), true);
  assert.equal(initializer.includes(`chmod -R go-w ${trees}`), true);
  assert.match(initializer, /find "\$tree" -xdev ! -readable -print -quit/);
  assert.match(initializer, /find "\$tree" -xdev -type d ! -executable -print -quit/);
  assert.match(initializer, /find "\$tree" -xdev -writable -print -quit/);
});

test("service logs stream directly into attempt evidence on every failure path", () => {
  const supervisor = fs.readFileSync(path.join(TOOL_ROOT, "harness", "macos-service-supervisor.sh"), "utf8");
  const relay = fs.readFileSync(path.join(TOOL_ROOT, "harness", "relay_service_journey.py"), "utf8");
  const adapter = fs.readFileSync(path.join(TOOL_ROOT, "adapters", "macos-linux-release.mjs"), "utf8");
  assert.match(supervisor, /service_log="\$logs\/service-\$instance\.log"/);
  assert.match(supervisor, /test ! -e "\$service_log"/);
  assert.match(supervisor, /PYTHONUNBUFFERED=1/);
  assert.match(supervisor, /install -d -m 0700 -o 0 -g 0 "\$runtime"\nmkdir -p "\$parts" "\$logs"/);
  assert.doesNotMatch(supervisor, /install -d[^\n]*"\$parts"/);
  assert.doesNotMatch(supervisor, /install -d[^\n]*"\$logs"/);
  assert.doesNotMatch(supervisor, /service_log="\$runtime\/service\.log"/);
  assert.doesNotMatch(supervisor, /install .*"\$service_log".*service-\$instance\.log/);
  assert.match(adapter, /bootstrapLog = `\/evidence\/stages\/\$\{configuration\.stage\}\/supervisor-\$\{instance\}\.log`/);
  assert.match(adapter, /exec sh \/harness\/macos-service-supervisor\.sh "\$1" "\$2" >"\$3" 2>&1/);
  assert.match(adapter, /allowEmptyJourney: configuration\.track === "dev"/);
  assert.match(supervisor, /allow-empty\) journey_empty_arg=--allow-empty/);
  assert.match(relay, /parser\.add_argument\("--allow-empty", action="store_true"\)/);
  assert.match(relay, /if arguments\.allow_empty:\n\s+_receipt\(arguments, status="PASS", code=None, count=0\)/);
  assert.match(adapter, /readiness-\$\{instance\}\.json/);
  assert.match(adapter, /service_pid_running: pidProbe\.status === 0/);
  assert.match(adapter, /internal_tcp_8000: tcpProbe\.status === 0/);
  assert.match(adapter, /launcher_process_present:/);
  assert.match(adapter, /journey_relay_present:/);
});

test("macOS adapter delegates client-server MCP correspondence to the evidence reader", () => {
  const adapter = fs.readFileSync(path.join(TOOL_ROOT, "adapters", "macos-linux-release.mjs"), "utf8");
  const start = adapter.indexOf("function verifyCorrespondence");
  const end = adapter.indexOf("async function startService", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const body = adapter.slice(start, end);
  assert.match(adapter, /import \{ readServerMcpCorrespondence \} from "\.\.\/lib\/events\.mjs"/);
  assert.match(body, /readServerMcpCorrespondence\(attemptRoot, client\)/);
  assert.match(body, /correspondence\.started_exact/);
  assert.match(body, /correspondence\.completed_exact/);
});

test("macOS adapter failure receipts recover authoritative usage instead of reporting zero", () => {
  const adapter = fs.readFileSync(path.join(TOOL_ROOT, "adapters", "macos-linux-release.mjs"), "utf8");
  assert.match(adapter, /import \{ recoverStageAuditProgress \} from "\.\.\/lib\/evidence\.mjs"/);
  assert.match(adapter, /const progress = recoverStageAuditProgress\(/);
  assert.match(adapter, /client_tool_calls: progress\.client_tool_calls/);
  assert.match(adapter, /server_tool_calls: progress\.server_tool_calls/);
  assert.match(adapter, /usage: progress\.usage/);
});

test("Release settings reject any additional environment key", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-release-settings-"));
  try {
    const source = path.join(root, "settings.json");
    fs.writeFileSync(source, JSON.stringify(settingsPayload({ EXTRA_UNFROZEN_INPUT: "forbidden" })), { encoding: "utf8", mode: 0o600 });
    const identity = claudeSettingsIdentity(source);
    assert.equal(identity.status, "INVALID");
    assert.equal(identity.code, "CLAUDE_SETTINGS_ENV_ALLOWLIST_MISMATCH");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("settings preparation leaves global aliases untouched and creates the frozen isolated mapping", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-release-settings-prep-"));
  try {
    const output = path.join(root, "release-settings.json");
    const environment = {
      ...process.env,
      ANTHROPIC_AUTH_TOKEN: "unit-test-high-entropy-auth-value",
      ANTHROPIC_BASE_URL: "https://provider.example.test/v1",
      ANTHROPIC_DEFAULT_HAIKU_MODEL: "unfrozen-global-haiku",
      ANTHROPIC_DEFAULT_OPUS_MODEL: "unfrozen-global-opus",
      ANTHROPIC_DEFAULT_SONNET_MODEL: "unfrozen-global-sonnet",
    };
    const prepared = spawnSync(process.execPath, [path.join(TOOL_ROOT, "prepare-release-settings.mjs"), "--output", output], {
      env: environment,
      encoding: "utf8",
    });
    assert.equal(prepared.status, 0, prepared.stderr);
    assert.equal(prepared.stdout.includes(environment.ANTHROPIC_AUTH_TOKEN), false);
    const copied = JSON.parse(fs.readFileSync(output, "utf8"));
    assert.equal(copied.env.ANTHROPIC_DEFAULT_HAIKU_MODEL, "deepseek-v4-flash[1m]");
    assert.equal(copied.env.ANTHROPIC_DEFAULT_OPUS_MODEL, "deepseek-v4-flash[1m]");
    assert.equal(copied.env.ANTHROPIC_DEFAULT_SONNET_MODEL, "deepseek-v4-flash[1m]");
    assert.equal(Object.hasOwn(copied, "hooks"), false);
    assert.equal(environment.ANTHROPIC_DEFAULT_HAIKU_MODEL, "unfrozen-global-haiku");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("a global-style Claude 2.1.201 executable and a tampered cli.js are rejected", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-release-claude-"));
  try {
    const globalClaude = path.join(root, "claude");
    fs.writeFileSync(globalClaude, "#!/bin/sh\nprintf '%s\\n' '2.1.201 (Claude Code)'\n", { encoding: "utf8", mode: 0o700 });
    assert.equal(validateClaudeDistribution(globalClaude).code, "CLAUDE_ENTRY_INVALID");

    const packageRoot = path.join(root, "package");
    fs.mkdirSync(packageRoot);
    const tamperedEntry = path.join(packageRoot, "cli.js");
    fs.writeFileSync(tamperedEntry, "console.log('2.1.89 (Claude Code)')\n", { encoding: "utf8", mode: 0o600 });
    assert.equal(validateClaudeDistribution(tamperedEntry).code, "CLAUDE_CLI_HASH_MISMATCH");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("the executable Release baseline exposes the frozen official artifact hashes", () => {
  assert.equal(RELEASE_CLAUDE_VERSION, "2.1.89");
  assert.equal(RELEASE_CLAUDE_TARBALL_SHA256, "680e35001b24b604f58958e3a324bb758be3c069c0a3f89585156256f17a9c87");
  assert.equal(RELEASE_CLAUDE_CLI_SHA256, "a9950ef6407fdc750bddb673852485500387e524a99d42385cb81e7d17128e01");
  assert.equal(RELEASE_UV_ARCHIVE_SHA256, "aab924fd522efd06f1c5f3b93a243864fc453132c94b2dc49f1371b528a4b967");
});
