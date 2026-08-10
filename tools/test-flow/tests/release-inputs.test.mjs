import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  CLAUDE_SETTINGS_ENV_KEYS,
  RELEASE_BASE_IMAGE,
  RELEASE_BASE_IMAGE_SOURCE,
  RELEASE_CLAUDE_CLI_SHA256,
  RELEASE_CLAUDE_TARBALL_SHA256,
  RELEASE_CLAUDE_VERSION,
  RELEASE_HATCHLING_VERSION,
  RELEASE_MODEL,
  RELEASE_PYTHON_VERSION,
  RELEASE_RUNTIME_PROFILE,
  RELEASE_UV_ARCHIVE_SHA256,
  RELEASE_UV_VERSION,
  claudeSettingsIdentity,
  materializeAttemptClaudeSettings,
  materializeClaudeSettings,
  validateClaudeDistribution,
} from "../lib/release-inputs.mjs";

const TOOL_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SUPPORT_ROOT = path.join(TOOL_ROOT, "runtime-support");

function settingsPayload(overrides = {}) {
  return {
    env: {
      ANTHROPIC_AUTH_TOKEN: "unit-test-high-entropy-auth-value",
      ANTHROPIC_BASE_URL: "https://provider.example.test/v1",
      ANTHROPIC_DEFAULT_HAIKU_MODEL: RELEASE_MODEL,
      ANTHROPIC_DEFAULT_OPUS_MODEL: RELEASE_MODEL,
      ANTHROPIC_DEFAULT_SONNET_MODEL: RELEASE_MODEL,
      API_TIMEOUT_MS: "600000",
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
      ...overrides,
    },
    hooks: { PreToolUse: [{ matcher: "*", hooks: [{ type: "command", command: "forbidden-hook" }] }] },
    permissions: { allow: ["Bash(*)"] },
  };
}

test("Release settings copy only the profile allowlist and never copy Hooks", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-release-settings-"));
  try {
    const source = path.join(root, "source-settings.json");
    const target = path.join(root, "isolated", "settings.json");
    fs.writeFileSync(source, JSON.stringify(settingsPayload()), { mode: 0o600 });
    const identity = claudeSettingsIdentity(source);
    assert.equal(identity.status, "PRESENT");
    assert.equal(identity.model, RELEASE_MODEL);
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
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("container settings are materialized inside attempt scratch and identity-bound", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-release-settings-stage-"));
  try {
    const sourceRoot = path.join(root, "source");
    const attemptRoot = path.join(root, "attempt");
    fs.mkdirSync(sourceRoot);
    fs.mkdirSync(attemptRoot);
    const source = path.join(sourceRoot, "settings.json");
    fs.writeFileSync(source, JSON.stringify(settingsPayload()), { mode: 0o600 });
    const sourceIdentity = claudeSettingsIdentity(source);
    const staged = materializeAttemptClaudeSettings(source, attemptRoot, sourceIdentity.fingerprint);
    assert.equal(staged.path.startsWith(`${path.resolve(attemptRoot)}${path.sep}`), true);
    assert.notEqual(staged.path, source);
    assert.equal(staged.identity.fingerprint, sourceIdentity.fingerprint);
    const core = fs.readFileSync(path.join(TOOL_ROOT, "adapters", "cross-job-core.mjs"), "utf8");
    assert.match(core, /configuration\.containerClaudeSettings = materializeAttemptClaudeSettings/);
    assert.match(core, /src=\$\{configuration\.containerClaudeSettings\},dst=\/run\/host-claude-settings\.json,readonly/);
    assert.doesNotMatch(core, /src=\$\{configuration\.claudeSettings\},dst=\/run\/host-claude-settings\.json/);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("settings reject extra keys and the preparation helper preserves global aliases", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-settings-prep-"));
  try {
    const invalid = path.join(root, "invalid.json");
    fs.writeFileSync(invalid, JSON.stringify(settingsPayload({ EXTRA_UNFROZEN_INPUT: "forbidden" })));
    assert.equal(claudeSettingsIdentity(invalid).code, "CLAUDE_SETTINGS_ENV_ALLOWLIST_MISMATCH");

    const output = path.join(root, "release-settings.json");
    const environment = {
      ...process.env,
      ANTHROPIC_AUTH_TOKEN: "unit-test-high-entropy-auth-value",
      ANTHROPIC_BASE_URL: "https://provider.example.test/v1",
      ANTHROPIC_DEFAULT_HAIKU_MODEL: "unfrozen-global-haiku",
      ANTHROPIC_DEFAULT_OPUS_MODEL: "unfrozen-global-opus",
      ANTHROPIC_DEFAULT_SONNET_MODEL: "unfrozen-global-sonnet",
    };
    const prepared = spawnSync(process.execPath, [path.join(TOOL_ROOT, "prepare-release-settings.mjs"), "--output", output], { env: environment, encoding: "utf8" });
    assert.equal(prepared.status, 0, prepared.stderr);
    const copied = JSON.parse(fs.readFileSync(output, "utf8"));
    assert.equal(copied.env.ANTHROPIC_DEFAULT_HAIKU_MODEL, RELEASE_MODEL);
    assert.equal(copied.env.ANTHROPIC_DEFAULT_OPUS_MODEL, RELEASE_MODEL);
    assert.equal(copied.env.ANTHROPIC_DEFAULT_SONNET_MODEL, RELEASE_MODEL);
    assert.equal(Object.hasOwn(copied, "hooks"), false);
    assert.equal(environment.ANTHROPIC_DEFAULT_HAIKU_MODEL, "unfrozen-global-haiku");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("runtime profile is the sole frozen version and artifact-pin source", () => {
  const releaseInputs = fs.readFileSync(path.join(TOOL_ROOT, "lib", "release-inputs.mjs"), "utf8");
  assert.match(releaseInputs, /runtime-profiles\.v2\.json/);
  assert.equal(RELEASE_CLAUDE_VERSION, RELEASE_RUNTIME_PROFILE.claude.version);
  assert.equal(RELEASE_UV_VERSION, RELEASE_RUNTIME_PROFILE.uv.version);
  assert.equal(RELEASE_PYTHON_VERSION, RELEASE_RUNTIME_PROFILE.python);
  assert.equal(RELEASE_HATCHLING_VERSION, RELEASE_RUNTIME_PROFILE.hatchling);
  assert.equal(RELEASE_BASE_IMAGE, RELEASE_RUNTIME_PROFILE.base_image.name);
  assert.equal(RELEASE_BASE_IMAGE_SOURCE, RELEASE_RUNTIME_PROFILE.base_image.source);
  assert.match(RELEASE_BASE_IMAGE_SOURCE, /@sha256:[a-f0-9]{64}$/);
});

test("the Dockerfile has no hidden version defaults and cache preparation supplies every build arg", () => {
  const dockerfile = fs.readFileSync(path.join(TOOL_ROOT, "Dockerfile"), "utf8");
  const preparer = fs.readFileSync(path.join(TOOL_ROOT, "prepare-release-cache.mjs"), "utf8");
  assert.doesNotMatch(dockerfile, /^ARG\s+[A-Z0-9_]+=/m);
  for (const name of ["BASE_IMAGE", "UV_SHA256", "UVX_SHA256", "UV_VERSION", "CLAUDE_CLI_SHA256", "CLAUDE_VERSION", "PYTHON_VERSION", "HATCHLING_VERSION"]) {
    assert.match(dockerfile, new RegExp(`^ARG ${name}$`, "m"));
    assert.match(preparer, new RegExp(`--build-arg[\\s\\S]{0,80}${name}=\\$\\{RELEASE_`));
  }
  assert.match(dockerfile, /hatchling==\$\{HATCHLING_VERSION\}/);
  assert.match(dockerfile, /problem-locator\.e2e\.claude="npm-\$\{CLAUDE_VERSION\}"/);
});

test("the first-party adapter matrix is thin, platform-bound and shares one core contract", () => {
  const expected = {
    macos: ["darwin", "colima"],
    windows: ["win32", "default"],
    linux: ["linux", "default"],
  };
  for (const [client, [host, context]] of Object.entries(expected)) {
    const wrapper = fs.readFileSync(path.join(TOOL_ROOT, "adapters", `${client}-linux-release.mjs`), "utf8");
    assert.match(wrapper, new RegExp(`TEST_FLOW_FIRST_PARTY_CLIENT = "${client}"`));
    assert.match(wrapper, new RegExp(`TEST_FLOW_FIRST_PARTY_HOST_PLATFORM = "${host}"`));
    assert.match(wrapper, new RegExp(`TEST_FLOW_FIRST_PARTY_DOCKER_CONTEXT = "${context}"`));
    assert.match(wrapper, /import\("\.\/cross-job-core\.mjs"\)/);
  }
  const core = fs.readFileSync(path.join(TOOL_ROOT, "adapters", "cross-job-core.mjs"), "utf8");
  assert.match(core, /process\.platform === configuration\.expectedHostPlatform/);
  assert.match(core, /configuration\.client === configuration\.expectedClient/);
});

test("Linux capability installs the immutable source snapshot from the sealed offline cache before testing", () => {
  const adapter = fs.readFileSync(path.join(TOOL_ROOT, "adapters", "server-linux-capability.mjs"), "utf8");
  const platformTests = [
    fs.readFileSync(path.join(TOOL_ROOT, "..", "..", "tests", "platform", "server_linux", "test_native_startup_gate.py"), "utf8"),
    fs.readFileSync(path.join(TOOL_ROOT, "..", "..", "tests", "platform", "distribution", "test_installed_distribution_gate.py"), "utf8"),
  ].join("\n");
  assert.match(adapter, /UV_CACHE_DIR=\/root\/\.cache\/uv/);
  assert.match(adapter, /UV_LINK_MODE=copy/);
  assert.doesNotMatch(adapter, /\/tmp\/uv-cache/);
  assert.match(adapter, /uv pip install --offline --no-deps --no-build-isolation --reinstall/);
  assert.match(adapter, /problem_locator\/runtime\/assets -xdev -type f -links \+1/);
  assert.ok(adapter.indexOf("uv pip install --offline") < adapter.indexOf("python -m pytest"));
  assert.match(adapter, /SERVER_CAPABILITY_OFFLINE_INSTALL/);
  assert.match(adapter, /SERVER_CAPABILITY_CONTRACT/);
  assert.match(platformTests, /environ\["UV_LINK_MODE"\] = "copy"/);
  assert.match(platformTests, /shutil\.copytree\(sealed_runtime, venv, symlinks=True\)/);
  assert.doesNotMatch(platformTests, /"venv",\s*"--no-project"/);
  assert.doesNotMatch(`${adapter}\n${platformTests}`, /S08_/);
});

test("active runtime support is explicit and the historical harness closure is gone", () => {
  const expected = [
    "audit_service_agent_usage.py", "checkpoint-temporary.mjs", "export-checkpoint.sh",
    "initialize-container.sh", "isolated-agent-wrapper.mjs", "prepare_claude_settings.py",
    "prepare_nonroot_settings.py", "prepare_real_zip.py", "relay_service_journey.py",
    "server_dfx_probe.py", "service-supervisor.sh", "stop-service.sh", "test_service_launcher.py",
    "verify-source-snapshot.mjs",
  ];
  assert.deepEqual(fs.readdirSync(SUPPORT_ROOT).sort(), expected.sort());
  assert.equal(fs.existsSync(path.join(TOOL_ROOT, "harness")), false);
  const activeText = [
    fs.readFileSync(path.join(TOOL_ROOT, "adapters", "cross-job-core.mjs"), "utf8"),
    ...fs.readdirSync(SUPPORT_ROOT).map((name) => fs.readFileSync(path.join(SUPPORT_ROOT, name), "utf8")),
  ].join("\n");
  assert.doesNotMatch(activeText, /\/harness\//);
  assert.match(activeText, /\/test-flow-runtime\//);
});

test("container initialization verifies the exact product snapshot and external source commits", () => {
  const initializer = fs.readFileSync(path.join(SUPPORT_ROOT, "initialize-container.sh"), "utf8");
  for (const source of ["/source/logparse/.git", "/source/problem-locator-mcp/.git"]) {
    assert.equal(initializer.includes(`git config --file "$source_git_config" --add safe.directory ${source}`), true);
    assert.equal(initializer.includes(`GIT_CONFIG_GLOBAL="$source_git_config" git -c core.autocrlf=false clone --no-hardlinks ${source.slice(0, -5)} `), true);
  }
  assert.doesNotMatch(initializer, /\/source\/xiaodao\/\.git/);
  assert.match(initializer, /cp -a \/source\/xiaodao\/\. \/opt\/src\/xiaodao\//);
  assert.match(initializer, /verify-source-snapshot\.mjs/);
  assert.match(initializer, /xiaodao_snapshot_digest/);
  assert.doesNotMatch(initializer, /safe\.directory\s+['"]?\*['"]?/);
  assert.match(initializer, /UV_LINK_MODE=copy UV_NO_PROGRESS=1 uv pip install/);
  assert.match(initializer, /--offline --no-deps --no-build-isolation --reinstall/);
  assert.match(initializer, /installed_assets=\/opt\/venvs\/xiaodao\/lib\/python3\.12\/site-packages\/problem_locator\/runtime\/assets/);
  assert.match(initializer, /-type l -print -quit/);
  assert.match(initializer, /-type f -links \+1 -print -quit/);
});

test("CrossJob runtime uses pull-never, empty labeled storage and authoritative server DFX", () => {
  const core = fs.readFileSync(path.join(TOOL_ROOT, "adapters", "cross-job-core.mjs"), "utf8");
  assert.match(core, /"--pull", "never"/);
  assert.match(core, /lineage_root: "GENESIS"/);
  assert.match(core, /initial_data_root: "EMPTY"/);
  assert.match(core, /server_dfx_probe\.py/);
  assert.match(core, /readServerMcpCorrespondence\(attemptRoot, state\.client_calls/);
  assert.match(core, /correspondence\.tools_listed_exact/);
  assert.match(core, /correspondence\.validation_probe_exact/);
  assert.match(core, /canonicalJson\(receipt\.validation_fields\) === canonicalJson\(NEGATIVE_PROBE_VALIDATION_FIELDS\)/);
  assert.match(core, /client_dfx_absent: true/);
  assert.match(core, /CLIENT_DFX_FORBIDDEN/);
});

test("model invocations require exact model, hard caps, terminal success and complete usage", () => {
  const core = fs.readFileSync(path.join(TOOL_ROOT, "adapters", "cross-job-core.mjs"), "utf8");
  const isolated = fs.readFileSync(path.join(SUPPORT_ROOT, "isolated-agent-wrapper.mjs"), "utf8");
  const serviceAudit = fs.readFileSync(path.join(SUPPORT_ROOT, "audit_service_agent_usage.py"), "utf8");
  assert.match(core, /--max-turns/);
  assert.match(core, /--max-budget-usd/);
  assert.match(core, /max_total_tokens: configuration\.hardCaps\.max_total_tokens/);
  assert.match(core, /terminal: audit\.terminal/);
  assert.match(core, /usage_complete: true/);
  assert.match(core, /total_tokens: "terminal-usage-postcondition"/);
  assert.match(core, /canonicalJson\(jobTypes\) === canonicalJson\(\["DIAGNOSE", "DIAGNOSE", "ROUTE"\]\)/);
  assert.match(core, /canonicalJson\(jobTypes\) === canonicalJson\(\["DIAGNOSE", "DIAGNOSE", "REVIEW"\]\)/);
  assert.match(isolated, /WRAPPER_MODEL_CAP_EXCEEDED/);
  assert.match(isolated, /final\.subtype !== "success" \|\| final\.is_error !== false/);
  assert.match(serviceAudit, /MODEL_TERMINAL_INVALID/);
  assert.match(serviceAudit, /max_total_tokens/);
});

test("checkpoints export stable state without symlinks, hardlinks or retained temporary workspaces", () => {
  const core = fs.readFileSync(path.join(TOOL_ROOT, "adapters", "cross-job-core.mjs"), "utf8");
  const exporter = fs.readFileSync(path.join(SUPPORT_ROOT, "export-checkpoint.sh"), "utf8");
  assert.match(core, /extractCheckpointSourceArchive\(\{ archivePath: archiveHostPath, targetRoot: stateRoot \}\)/);
  assert.match(core, /checkpoint-temporary-classification\.json/);
  assert.match(core, /classification\.outbox_clear === true/);
  assert.match(core, /"exec", state\.active_container, "ps", "-ww", "-eo", "args"/);
  assert.match(exporter, /for required in data-format\.json state\.json resources jobs/);
  assert.match(exporter, /tmp\/workspaces/);
  assert.match(exporter, /-type l -print -quit/);
  assert.match(exporter, /-type f -links \+1 -print -quit/);
  assert.match(exporter, /tar --format=ustar --sort=name --mtime=@0 --numeric-owner/);
  assert.doesNotMatch(exporter, /\.instance\.lock/);
});

test("service evidence streams directly to attempt logs and restart emptiness is explicit", () => {
  const core = fs.readFileSync(path.join(TOOL_ROOT, "adapters", "cross-job-core.mjs"), "utf8");
  const supervisor = fs.readFileSync(path.join(SUPPORT_ROOT, "service-supervisor.sh"), "utf8");
  const relay = fs.readFileSync(path.join(SUPPORT_ROOT, "relay_service_journey.py"), "utf8");
  assert.match(supervisor, /service_log="\$logs\/service-\$instance\.log"/);
  assert.match(supervisor, /PYTHONUNBUFFERED=1/);
  assert.match(supervisor, /allow-empty\) journey_empty_arg=--allow-empty/);
  assert.match(core, /\{ allowEmptyJourney = true \}/);
  assert.match(relay, /parser\.add_argument\("--allow-empty", action="store_true"\)/);
  assert.match(relay, /_validation_error_facts/);
  assert.doesNotMatch(relay, /"validation_errors": event\.get\("validation_errors"\)/);
  assert.match(core, /startService\(configuration, state, "route", \{ allowEmptyJourney: true \}\)/);
  assert.match(core, /startService\(configuration, state, "restart", \{ allowEmptyJourney: true \}\)/);
  assert.match(core, /allowEmpty: mode === "journey" && \["route", "restart"\]\.includes\(instance\)/);
  assert.match(core, /service-\$\{instance\}-\$\{relayKind\}-relay\.json/);
});

test("tampered distributions and the frozen artifact hashes fail closed", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-release-claude-"));
  try {
    const globalClaude = path.join(root, "claude");
    fs.writeFileSync(globalClaude, `#!/bin/sh\nprintf '%s\\n' '${RELEASE_CLAUDE_VERSION} (Claude Code)'\n`, { mode: 0o700 });
    assert.equal(validateClaudeDistribution(globalClaude).code, "CLAUDE_ENTRY_INVALID");
    const packageRoot = path.join(root, "package");
    fs.mkdirSync(packageRoot);
    const tamperedEntry = path.join(packageRoot, "cli.js");
    fs.writeFileSync(tamperedEntry, `console.log('${RELEASE_CLAUDE_VERSION} (Claude Code)')\n`);
    assert.equal(validateClaudeDistribution(tamperedEntry).code, "CLAUDE_CLI_HASH_MISMATCH");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
  assert.equal(RELEASE_CLAUDE_TARBALL_SHA256, "680e35001b24b604f58958e3a324bb758be3c069c0a3f89585156256f17a9c87");
  assert.equal(RELEASE_CLAUDE_CLI_SHA256, "a9950ef6407fdc750bddb673852485500387e524a99d42385cb81e7d17128e01");
  assert.equal(RELEASE_UV_ARCHIVE_SHA256, "aab924fd522efd06f1c5f3b93a243864fc453132c94b2dc49f1371b528a4b967");
});
