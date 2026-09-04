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
  RELEASE_CHROME_HEADLESS_SHELL_ARCHIVE_SHA256,
  RELEASE_CHROME_HEADLESS_SHELL_EXECUTABLE_SHA256,
  RELEASE_CHROME_HEADLESS_SHELL_PLATFORM,
  RELEASE_CHROME_HEADLESS_SHELL_PRODUCT,
  RELEASE_CHROME_HEADLESS_SHELL_VERSION,
  RELEASE_CHROME_HEADLESS_SHELL_VERSION_OUTPUT,
  RELEASE_CLIENT_IMAGE,
  RELEASE_CLAUDE_CLI_SHA256,
  RELEASE_CLAUDE_TARBALL_SHA256,
  RELEASE_CLAUDE_VERSION,
  RELEASE_HATCHLING_VERSION,
  RELEASE_MODEL,
  RELEASE_PYTHON_VERSION,
  RELEASE_RUNTIME_PROFILE,
  RELEASE_UV_ARCHIVE_SHA256,
  RELEASE_UV_VERSION,
  RELEASE_UV_VERSION_OUTPUT,
  RELEASE_UVX_VERSION_OUTPUT,
  claudeSettingsIdentity,
  codexLogparseRuntimeIdentity,
  dockerContextArgs,
  dockerServerIdentity,
  materializeAttemptClaudeSettings,
  materializeClaudeSettings,
  probeReleaseClientHeadlessShell,
  releaseCachePaths,
  sameDockerRuntimeIdentity,
  validateChromeHeadlessShellCache,
  validateClaudeDistribution,
} from "../lib/release-inputs.mjs";
import { NEGATIVE_PROBE_VALIDATION_FIELDS } from "../lib/events.mjs";

const TOOL_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SUPPORT_ROOT = path.join(TOOL_ROOT, "runtime-support");

test("Docker context arguments use ambient semantics for explicit default and exact named contexts otherwise", () => {
  assert.deepEqual(dockerContextArgs(null, ["version"]), ["version"]);
  assert.deepEqual(dockerContextArgs("default", ["image", "inspect", "sha256:abc"]), ["image", "inspect", "sha256:abc"]);
  assert.deepEqual(dockerContextArgs("colima", ["version"]), ["--context", "colima", "version"]);
  assert.throws(() => dockerContextArgs("../escape", ["version"]), /DOCKER_CONTEXT_ARGUMENTS_INVALID/);
});

test("dual Linux planning executes one closed zero-network Headless Shell smoke before admission", () => {
  const calls = [];
  const clientImageId = `sha256:${"a".repeat(64)}`;
  const dockerIdentity = { status: "PRESENT", docker_cli: "/fixture/docker", context: "colima" };
  const commandRunner = (command, args) => {
    calls.push({ command, args: [...args] });
    const dataUrl = args.at(-1);
    assert.match(dataUrl, /^data:text\/html,/);
    return {
      status: 0,
      signal: null,
      stdout: `${decodeURIComponent(dataUrl.slice("data:text/html,".length))}\n`,
      stderr: "diagnostic text is not persisted",
    };
  };
  const receipt = probeReleaseClientHeadlessShell({
    dockerIdentity,
    clientImageId,
    commandRunner,
    uid: 501,
    gid: 20,
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].command, "/fixture/docker");
  assert.deepEqual(calls[0].args.slice(0, 3), ["--context", "colima", "run"]);
  assert.equal(calls[0].args.filter((value) => value === "--rm").length, 1);
  assert.equal(calls[0].args.includes("--init"), true);
  assert.equal(calls[0].args.includes("none"), true);
  assert.equal(calls[0].args.includes("501:20"), true);
  assert.equal(calls[0].args.includes(clientImageId), true);
  assert.equal(calls[0].args.includes("/opt/chrome-headless-shell/chrome-headless-shell"), true);
  assert.deepEqual(receipt, {
    schema_version: 1,
    status: "PASS",
    code: null,
    kind: "linux-client-headless-shell-plan-smoke",
    argument_profile: "chrome-headless-shell-plan-smoke-v1",
    execution_layer: "docker-run-client-image",
    network_scope: "none",
    image_id: clientImageId,
    execution_user: { uid: 501, gid: 20, root: false },
    executable: "/opt/chrome-headless-shell/chrome-headless-shell",
    challenge_sha256: "e845b0fcff00e98dcba8238c1153e673dcdcc602cc49dd0a91a577ae14c06854",
    stdout: {
      byte_count: 183,
      sha256: "b0cd909b40e3b72644fc9834ab6223de4715e00f10f7a25d6e2620c7da34ae7d",
      truncated: false,
    },
    launcher: { exit_code: 0, signal: null, retries: 0 },
  });

  assert.throws(() => probeReleaseClientHeadlessShell({
    dockerIdentity,
    clientImageId,
    commandRunner: () => ({ status: 124, signal: null, stdout: "", stderr: "timeout" }),
    uid: 501,
    gid: 20,
  }), /RELEASE_CLIENT_HEADLESS_SHELL_SMOKE_EXIT_124/);
  assert.throws(() => probeReleaseClientHeadlessShell({
    dockerIdentity,
    clientImageId,
    commandRunner: () => ({ status: 0, signal: null, stdout: "<html></html>", stderr: "" }),
    uid: 501,
    gid: 20,
  }), /RELEASE_CLIENT_HEADLESS_SHELL_SMOKE_RESULT_MISSING/);
  assert.throws(() => probeReleaseClientHeadlessShell({
    dockerIdentity,
    clientImageId,
    commandRunner,
    uid: 0,
    gid: 0,
  }), /RELEASE_CLIENT_HEADLESS_SHELL_SMOKE_NON_ROOT_REQUIRED/);
});

function observedDockerIdentity({
  context = "default",
  serverId = "daemon-identity-1",
  versionOs = "linux",
  infoOs = "linux",
  versionArchitecture = "amd64",
  infoArchitecture = "x86_64",
  version = "29.7.2",
  infoVersion = "29.7.2",
  colimaStatus = '{"status":"Running","generation":1}',
  environment = {},
} = {}) {
  const calls = [];
  const response = (stdout, status = 0) => ({ status, stdout: `${stdout}\n`, stderr: "" });
  const commandRunner = (command, args) => {
    calls.push({ command, args: [...args] });
    if (command === "/fixture/colima") {
      if (args[0] === "version") return response("colima version 0.9.1");
      if (args[0] === "status" && args[1] === "--json") return response(colimaStatus);
      throw new Error(`unexpected Colima fixture command: ${args.join(" ")}`);
    }
    const dockerArgs = context === "default" ? args : args.slice(2);
    if (dockerArgs[0] === "version") {
      return response(JSON.stringify({
        Platform: { Name: "Docker Engine" },
        Components: [],
        Version: version,
        ApiVersion: "1.53",
        MinAPIVersion: "1.44",
        GitCommit: "fixture",
        GoVersion: "go1.25",
        Os: versionOs,
        Arch: versionArchitecture,
        KernelVersion: "fixture",
        BuildTime: "fixture",
      }));
    }
    if (dockerArgs[0] === "info") {
      return response(JSON.stringify({
        ID: serverId,
        OSType: infoOs,
        Architecture: infoArchitecture,
        ServerVersion: infoVersion,
      }));
    }
    if (args[0] === "context" && args[1] === "show") return response("ambient-linux");
    if (args[0] === "context" && args[1] === "inspect") {
      return response(JSON.stringify([{ Name: context === "default" ? "ambient-linux" : context, Endpoints: { docker: { Host: "unix:///fixture.sock" } } }]));
    }
    throw new Error(`unexpected fixture command: ${command} ${args.join(" ")}`);
  };
  const identity = dockerServerIdentity(context, {
    commandResolver: (name) => `/fixture/${name}`,
    fileResolver: (filePath) => filePath,
    commandRunner,
    fileHasher: () => "a".repeat(64),
    environment,
  });
  return { identity, calls };
}

test("Docker daemon identity reads the nonempty ID from info while version keeps its real no-ID shape", () => {
  const ambient = observedDockerIdentity();
  assert.equal(ambient.identity.status, "PRESENT", ambient.identity.code);
  assert.equal(ambient.identity.server_id, "daemon-identity-1");
  assert.equal(ambient.identity.os, "linux");
  assert.equal(ambient.identity.architecture, "amd64");
  assert.equal(ambient.identity.version, "29.7.2");
  const ambientDockerCalls = ambient.calls.filter((call) => call.command === "/fixture/docker" && call.args[0] !== "context");
  assert.ok(ambientDockerCalls.some((call) => call.args[0] === "version"));
  assert.ok(ambientDockerCalls.some((call) => call.args[0] === "info"));
  assert.ok(ambientDockerCalls.every((call) => call.args[0] !== "--context"));

  const named = observedDockerIdentity({ context: "remote-linux" });
  assert.equal(named.identity.status, "PRESENT", named.identity.code);
  const namedDockerCalls = named.calls.filter((call) => call.command === "/fixture/docker" && call.args[0] === "--context");
  assert.ok(namedDockerCalls.some((call) => call.args[2] === "version"));
  assert.ok(namedDockerCalls.some((call) => call.args[2] === "info"));
  assert.ok(namedDockerCalls.every((call) => call.args[1] === "remote-linux"));
});

test("Docker daemon identity fails closed when info omits its ID or disagrees with version", () => {
  const missing = observedDockerIdentity({ serverId: "" }).identity;
  assert.equal(missing.status, "INVALID");
  assert.equal(missing.code, "DOCKER_SERVER_ID_MISSING");

  const disagreement = observedDockerIdentity({ infoArchitecture: "arm64" }).identity;
  assert.equal(disagreement.status, "INVALID");
  assert.equal(disagreement.code, "DOCKER_SERVER_METADATA_MISMATCH");
});

test("Docker runtime equality binds a nonempty daemon ID, OS, architecture and version", () => {
  const expected = observedDockerIdentity().identity;
  assert.equal(sameDockerRuntimeIdentity(expected, { ...expected }), true);
  for (const [field, value] of [
    ["server_id", "daemon-identity-2"],
    ["os", "windows"],
    ["architecture", "arm64"],
    ["version", "29.7.3"],
    ["context", "other-requested-context"],
    ["effective_context", "other-context"],
    ["context_fingerprint", "b".repeat(64)],
    ["docker_cli_sha256", "c".repeat(64)],
  ]) {
    assert.equal(sameDockerRuntimeIdentity(expected, { ...expected, [field]: value }), false, field);
  }
  assert.equal(sameDockerRuntimeIdentity({ ...expected, server_id: null }, { ...expected, server_id: null }), false);
  assert.equal(sameDockerRuntimeIdentity({ ...expected, version: null }, { ...expected, version: null }), false);
  assert.equal(sameDockerRuntimeIdentity({ ...expected, context_fingerprint: "invalid" }, { ...expected, context_fingerprint: "invalid" }), false);
  assert.equal(sameDockerRuntimeIdentity({ ...expected, docker_cli_sha256: "invalid" }, { ...expected, docker_cli_sha256: "invalid" }), false);
});

test("Docker context identity binds API negotiation and Colima runtime observations", () => {
  const apiDefault = observedDockerIdentity({ environment: {} }).identity;
  const apiPinned = observedDockerIdentity({ environment: { DOCKER_API_VERSION: "1.49" } }).identity;
  assert.notEqual(apiDefault.context_fingerprint, apiPinned.context_fingerprint);
  assert.equal(sameDockerRuntimeIdentity(apiDefault, apiPinned), false);

  const colima = observedDockerIdentity({ context: "colima" }).identity;
  const restarted = observedDockerIdentity({ context: "colima", colimaStatus: '{"status":"Running","generation":2}' }).identity;
  assert.equal(colima.status, "PRESENT", colima.code);
  assert.equal(restarted.status, "PRESENT", restarted.code);
  assert.notEqual(colima.context_fingerprint, restarted.context_fingerprint);
  assert.equal(sameDockerRuntimeIdentity(colima, restarted), false);
});

test("Codex Logparse runtime identity binds venv, interpreter and external Python import roots", {
  skip: process.platform === "win32",
}, (t) => {
  const root = fs.mkdtempSync(path.join(fs.existsSync("/private/tmp") ? "/private/tmp" : os.tmpdir(), "test-flow-codex-logparse-runtime-"));
  try {
    fs.writeFileSync(path.join(root, "cli.py"), "print('logparse')\n");
    const created = spawnSync("python3", ["-m", "venv", path.join(root, ".venv")], { encoding: "utf8", timeout: 120_000 });
    if (created.status !== 0) {
      t.skip(`python3 -m venv unavailable: ${created.stderr}`);
      return;
    }
    const marker = path.join(root, ".venv", "runtime-marker.txt");
    fs.writeFileSync(marker, "first\n");
    const first = codexLogparseRuntimeIdentity(root);
    assert.equal(first.status, "PRESENT", first.code);
    assert.match(first.venv.tree_sha256, /^[a-f0-9]{64}$/);
    assert.match(first.python.resolved_sha256, /^[a-f0-9]{64}$/);
    assert.ok(first.python.runtime.import_paths.some((entry) => entry.status === "PRESENT" && entry.kind === "directory"));
    fs.writeFileSync(marker, "second\n");
    const second = codexLogparseRuntimeIdentity(root);
    assert.equal(second.status, "PRESENT", second.code);
    assert.notEqual(second.venv.tree_sha256, first.venv.tree_sha256);
    assert.equal(second.python.resolved_sha256, first.python.resolved_sha256);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

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
    assert.equal(CLAUDE_SETTINGS_ENV_KEYS.includes("CLAUDE_CODE_MAX_OUTPUT_TOKENS"), false);
    assert.equal(Object.hasOwn(copied, "hooks"), false);
    assert.equal(Object.hasOwn(copied, "permissions"), false);
    const mode = fs.statSync(target).mode & 0o777;
    if (process.platform === "win32") assert.equal(mode & 0o600, 0o600);
    else assert.equal(mode, 0o600);
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
  assert.equal(RELEASE_RUNTIME_PROFILE.claude.max_output_tokens_upper_limit, 64000);
  assert.equal(RELEASE_UV_VERSION, RELEASE_RUNTIME_PROFILE.uv.version);
  assert.equal(RELEASE_UV_VERSION_OUTPUT, RELEASE_RUNTIME_PROFILE.uv.version_output);
  assert.equal(RELEASE_UVX_VERSION_OUTPUT, RELEASE_RUNTIME_PROFILE.uv.uvx_version_output);
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
  const clientDockerfile = fs.readFileSync(path.join(TOOL_ROOT, "Dockerfile.client"), "utf8");
  for (const name of ["BASE_IMAGE", "CHROME_HEADLESS_SHELL_VERSION", "CHROME_HEADLESS_SHELL_ARCHIVE_SHA256", "CHROME_HEADLESS_SHELL_EXECUTABLE_SHA256"]) {
    assert.match(clientDockerfile, new RegExp(`^ARG ${name}$`, "m"));
    assert.match(preparer, new RegExp(`--build-arg[\\s\\S]{0,100}${name}=\\$\\{RELEASE_`));
  }
  assert.match(clientDockerfile, /problem-locator\.e2e\.role="linux-client"/);
  assert.match(clientDockerfile, /Google Chrome for Testing \$\{CHROME_HEADLESS_SHELL_VERSION\}/);
  assert.match(clientDockerfile, /COPY --from=chromeheadlessshellcache \. \/opt\/chrome-headless-shell/);
  assert.match(clientDockerfile, /problem-locator\.e2e\.chrome-headless-shell-version=/);
  assert.match(clientDockerfile, /problem-locator\.e2e\.chrome-headless-shell-product="Chrome Headless Shell for Testing"/);
  assert.match(clientDockerfile, /problem-locator\.e2e\.chrome-headless-shell-archive-sha256=/);
  assert.match(clientDockerfile, /problem-locator\.e2e\.chrome-headless-shell-sha256=/);
  assert.match(preparer, /chrome-headless-shell-\$\{RELEASE_CHROME_HEADLESS_SHELL_PLATFORM\}\.zip/);
  assert.doesNotMatch(preparer, /const CHROME_URL =/);
  assert.match(RELEASE_CLIENT_IMAGE, /client-cft-headless-shell-152\.0\.7977\.54$/);
  assert.equal(RELEASE_CHROME_HEADLESS_SHELL_VERSION, "152.0.7977.54");
  assert.equal(RELEASE_CHROME_HEADLESS_SHELL_PRODUCT, "Chrome Headless Shell for Testing");
  assert.equal(RELEASE_CHROME_HEADLESS_SHELL_VERSION_OUTPUT, "Google Chrome for Testing 152.0.7977.54");
  assert.equal(RELEASE_CHROME_HEADLESS_SHELL_PLATFORM, "linux64");
  assert.equal(RELEASE_CHROME_HEADLESS_SHELL_ARCHIVE_SHA256, "11cedb5568cd374a76eb738e40bd434cd0c9956820fb406b8bd9edca53428d3e");
  assert.equal(RELEASE_CHROME_HEADLESS_SHELL_EXECUTABLE_SHA256, "8a3f72f9676736c45e94ae3279b4e2e6a1e323187f9a5e73c9a760e8cc1296ea");
  assert.doesNotMatch(clientDockerfile, /TEST_FLOW_CHROME=/);
  assert.doesNotMatch(clientDockerfile, /problem-locator\.e2e\.chrome-(?:version|sha256|archive-sha256)=/);
  assert.doesNotMatch(clientDockerfile, /\/opt\/chrome-for-testing\/chrome/);

  const cache = releaseCachePaths(TOOL_ROOT, path.join(os.tmpdir(), "release-cache-root"));
  assert.match(cache.chromeHeadlessShellRoot.replaceAll(path.sep, "/"), /chrome-headless-shell-for-testing\/152\.0\.7977\.54\/linux64$/);
  assert.match(cache.chromeHeadlessShellArchive.replaceAll(path.sep, "/"), /chrome-headless-shell-linux64-152\.0\.7977\.54\.zip$/);
  assert.match(cache.chromeHeadlessShellDistribution.replaceAll(path.sep, "/"), /chrome-headless-shell-linux64$/);
  assert.match(cache.chromeHeadlessShellExecutable.replaceAll(path.sep, "/"), /chrome-headless-shell-linux64\/chrome-headless-shell$/);
  assert.equal(Object.hasOwn(cache, "chromeRoot"), false);
  assert.equal(Object.hasOwn(cache, "chromeExecutable"), false);
  assert.equal(validateChromeHeadlessShellCache(cache).code, "CHROME_HEADLESS_SHELL_CACHE_FILE_MISSING");
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
    assert.match(wrapper, /TEST_FLOW_FIRST_PARTY_TOPOLOGY = "host-client"/);
  }
  const dual = fs.readFileSync(path.join(TOOL_ROOT, "adapters", "macos-linux-linux-release.mjs"), "utf8");
  assert.match(dual, /TEST_FLOW_FIRST_PARTY_CLIENT = "linux"/);
  assert.match(dual, /TEST_FLOW_FIRST_PARTY_HOST_PLATFORM = "darwin"/);
  assert.match(dual, /TEST_FLOW_FIRST_PARTY_DOCKER_CONTEXT = "colima"/);
  assert.match(dual, /TEST_FLOW_FIRST_PARTY_TOPOLOGY = "dual-linux-containers"/);
  const corePath = path.join(TOOL_ROOT, "adapters", "cross-job-core.mjs");
  const core = fs.readFileSync(corePath, "utf8");
  const syntax = spawnSync(process.execPath, ["--check", corePath], { encoding: "utf8" });
  assert.equal(syntax.status, 0, syntax.stderr);
  assert.match(core, /process\.platform === configuration\.expectedHostPlatform/);
  assert.match(core, /configuration\.client === configuration\.expectedClient/);
  assert.match(core, /runChromePage/);
  assert.match(core, /content_length_control: "user-agent"/);
  assert.match(core, /CHROME_ARTIFACT_LIST_INVALID/);
  assert.match(core, /CHROME_IDENTITY_DRIFT/);
  assert.match(core, /"--network-alias", "problem-locator-client"/);
  assert.match(core, /"--network-alias", "problem-locator-server"/);
  assert.match(core, /public_base_url: dualLinuxContainers \? "http:\/\/problem-locator-server:8000"/);
  assert.match(core, /Object\.keys\(serverPortBindings\)\.length === 0/);
  assert.match(core, /DUAL_LINUX_IMAGE_IDS_REQUIRED/);
  assert.match(core, /RELEASE_CLIENT_IMAGE_IDENTITY_INVALID/);
  assert.match(core, /GENERATED_SKILL_ROOT_REQUIRED/);
  assert.match(core, /dst=\/run\/generated-specialized-skill,readonly/);
  assert.match(core, /diagnosis-result\.json/);
  assert.match(core, /result\.zip/);
  assert.match(core, /methods-grounding-audit\.json/);
  assert.match(core, /public_result_archive/);
  const actions = fs.readFileSync(path.join(TOOL_ROOT, "lib", "actions.mjs"), "utf8");
  assert.match(actions, /--chrome-version/);
  assert.match(actions, /CROSS_JOB_BROWSER_UPLOAD_RECEIPT_INVALID/);
  assert.match(actions, /CROSS_JOB_BROWSER_API_RECEIPT_INVALID/);
  assert.match(actions, /validMethodsV1OracleEvidence/);
  assert.match(actions, /CROSS_JOB_METHODS_V1_ORACLE_EVIDENCE_INVALID/);
  assert.match(actions, /methods_grounding/);
});

test("the dual Linux adapter fails closed on traversal, mutable Skills, proxy leakage and runtime replacement", () => {
  const corePath = path.join(TOOL_ROOT, "adapters", "cross-job-core.mjs");
  const core = fs.readFileSync(corePath, "utf8");
  const methodsOracle = fs.readFileSync(path.join(TOOL_ROOT, "lib", "methods-oracle.mjs"), "utf8");
  const initializer = fs.readFileSync(path.join(SUPPORT_ROOT, "initialize-container.sh"), "utf8");
  assert.match(core, /ADAPTER_STAGE_IDS\.has\(configuration\.stage\)/);
  assert.match(core, /GENERATED_SKILL_ROOT_REALPATH_INVALID/);
  assert.match(core, /GENERATED_SKILL_GATE_RECEIPT_MISSING/);
  assert.match(core, /GENERATED_SKILL_GATE_RECEIPT_DRIFT/);
  assert.match(core, /RESOURCE_LABEL_INVALID/);
  assert.match(core, /"timeout", "--signal=TERM", "--kill-after=10s"/);
  assert.match(core, /env: containerClient \? process\.env : environment/);
  assert.match(core, /chown -R 0:0 "\$installed_root"/);
  assert.match(core, /find "\$installed_root" -type f -exec chmod 0444/);
  assert.match(core, /GENERATED_SKILL_INSTALLED_TREE_DRIFT/);
  assert.match(core, /DUAL_LINUX_RUNTIME_RESOURCE_DRIFT/);
  assert.match(core, /client\.Image === configuration\.expectedClientImageId/);
  assert.match(core, /validServerRuntimeInspection\(\{/);
  assert.match(core, /SERVER_RUNTIME_RESOURCE_DRIFT/);
  assert.match(core, /"--read-only"/);
  assert.match(core, /client\.HostConfig\?\.ReadonlyRootfs === true/);
  assert.match(core, /node_identity_boundary: "client-image-id"/);
  assert.match(core, /node_sha256:digest\(process\.execPath\)/);
  assert.match(core, /canonicalJson\(observedClientRuntime\) === canonicalJson\(state\.selected_client_runtime_observed\)/);
  assert.match(core, /runtimeIdentity\.claude_cli_sha256 === RELEASE_CLAUDE_CLI_SHA256/);
  assert.match(core, /runtimeIdentity\.headless_shell_sha256 === RELEASE_CHROME_HEADLESS_SHELL_EXECUTABLE_SHA256/);
  assert.match(core, /"NO_PROXY=problem-locator-server,problem-locator-client,127\.0\.0\.1,localhost"/);
  assert.match(core, /artifacts_verified: expectedArtifacts\.length/);
  assert.match(initializer, /find \/opt\/e2e-skills -xdev -perm \/022/);
  assert.match(initializer, /runuser -u plagent -- find "\$tree" -xdev -writable/);
  assert.match(core, /GENERATED_SKILL_REGISTRATION_BYTES_DRIFT/);
  assert.match(core, /validateMethodsGroundingExecutionRecord/);
  assert.match(methodsOracle, /METHODS_V2_GRAPH_HIT_INVALID/);
  assert.doesNotMatch(methodsOracle, /hit\.line\.toLowerCase\(\)|observed\.line\.toLowerCase\(\)/);
  assert.match(methodsOracle, /METHODS_V2_PLAN_COVERAGE/);
  assert.match(methodsOracle, /METHODS_V2_PUBLIC_RESULT_MISMATCH/);
  assert.match(methodsOracle, /METHODS_V2_RESTART_RECORD_DRIFT/);

  const attemptRoot = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-stage-traversal-"));
  try {
    const escaped = path.join(attemptRoot, "payload", "escape");
    const result = spawnSync(process.execPath, [
      corePath,
      "--stage", "../../escape",
      "--attempt-root", attemptRoot,
    ], { encoding: "utf8" });
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /ADAPTER_STAGE_INVALID/);
    assert.equal(fs.existsSync(escaped), false);
  } finally {
    fs.rmSync(attemptRoot, { recursive: true, force: true });
  }
});

test("Linux capability installs the immutable source snapshot from the sealed offline cache before testing", () => {
  const adapter = fs.readFileSync(path.join(TOOL_ROOT, "adapters", "server-linux-capability.mjs"), "utf8");
  const platformTests = [
    fs.readFileSync(path.join(TOOL_ROOT, "..", "..", "tests", "platform", "server_linux", "test_native_startup_gate.py"), "utf8"),
    fs.readFileSync(path.join(TOOL_ROOT, "..", "..", "tests", "platform", "distribution", "test_installed_distribution_gate.py"), "utf8"),
    fs.readFileSync(path.join(TOOL_ROOT, "..", "..", "tests", "platform", "compat", "test_macos_process_tree_gate.py"), "utf8"),
  ].join("\n");
  assert.match(adapter, /UV_CACHE_DIR=\/root\/\.cache\/uv/);
  assert.match(adapter, /UV_LINK_MODE=copy/);
  assert.doesNotMatch(adapter, /\/tmp\/uv-cache/);
  assert.match(adapter, /uv pip install --offline --no-deps --no-build-isolation --reinstall/);
  assert.match(adapter, /problem_locator\/runtime\/assets -xdev -type f -links \+1/);
  assert.ok(adapter.indexOf("uv pip install --offline") < adapter.indexOf("python -m pytest"));
  assert.match(adapter, /SERVER_CAPABILITY_OFFLINE_INSTALL/);
  assert.match(adapter, /SERVER_CAPABILITY_CONTRACT/);
  assert.match(adapter, /lines\[5\] !== RELEASE_UV_VERSION_OUTPUT/);
  assert.match(adapter, /lines\[6\] !== RELEASE_UVX_VERSION_OUTPUT/);
  assert.match(adapter, /server-capability-termination\.json/);
  assert.doesNotMatch(adapter, /SERVER_MODEL_CAPABILITY_UNAVAILABLE/);
  assert.match(adapter, /tests\/platform\/compat\/test_macos_process_tree_gate\.py::test_host_timeout_kills_the_real_child_tree_without_rerunning_agent/);
  assert.match(adapter, /"process-tree-cleanup": "PASS"/);
  assert.match(platformTests, /environ\["UV_LINK_MODE"\] = "copy"/);
  assert.match(platformTests, /shutil\.copytree\(sealed_runtime, venv, symlinks=True\)/);
  assert.doesNotMatch(platformTests, /"venv",\s*"--no-project"/);
  assert.doesNotMatch(`${adapter}\n${platformTests}`, /S08_/);
});

test("active runtime support is explicit and the historical harness closure is gone", () => {
  const expected = [
    "audit_service_agent_usage.py", "checkpoint-temporary.mjs", "export-checkpoint.sh",
    "codex-luna-app-server-runtime.mjs", "codex-luna-app-server.mjs", "codex-luna-contract.mjs", "codex-luna-diagnosis.schema.json", "codex-luna-exploration-runner.mjs", "codex-luna-prepare.py",
    "evidence-v2-provider-terminal.mjs", "initialize-container.sh", "isolated-agent-env.mjs", "isolated-agent-tool-audit.mjs", "isolated-agent-wrapper.mjs", "linux_client_browser_runner.py",
    "prepare_claude_settings.py",
    "prepare_nonroot_settings.py", "prepare_release_case.py", "relay_service_journey.py",
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

test("every POSIX Test Flow entrypoint is stored with LF-only bytes", () => {
  const scripts = [
    path.join(TOOL_ROOT, "run.sh"),
    ...fs.readdirSync(SUPPORT_ROOT)
      .filter((name) => name.endsWith(".sh"))
      .map((name) => path.join(SUPPORT_ROOT, name)),
  ];
  for (const script of scripts) {
    assert.doesNotMatch(fs.readFileSync(script, "utf8"), /\r/, script);
  }
});

test("container initialization verifies the exact product snapshot and external source commits", () => {
  const initializer = fs.readFileSync(path.join(SUPPORT_ROOT, "initialize-container.sh"), "utf8");
  const releaseCase = fs.readFileSync(path.join(SUPPORT_ROOT, "prepare_release_case.py"), "utf8");
  const supervisor = fs.readFileSync(path.join(SUPPORT_ROOT, "service-supervisor.sh"), "utf8");
  for (const source of ["/source/logparse/.git", "/source/problem-locator-mcp/.git"]) {
    assert.equal(initializer.includes(`git config --file "$source_git_config" --add safe.directory ${source}`), true);
    assert.equal(initializer.includes(`GIT_CONFIG_GLOBAL="$source_git_config" git -c core.autocrlf=false clone --no-hardlinks ${source.slice(0, -5)} `), true);
  }
  assert.doesNotMatch(initializer, /\/source\/xiaodao\/\.git/);
  assert.match(initializer, /cp -a \/source\/xiaodao\/\. \/opt\/src\/xiaodao\//);
  assert.match(initializer, /verify-source-snapshot\.mjs/);
  assert.match(initializer, /--materialize-file-modes/);
  assert.match(initializer, /xiaodao_snapshot_digest/);
  const verifier = fs.readFileSync(path.join(SUPPORT_ROOT, "verify-source-snapshot.mjs"), "utf8");
  assert.match(verifier, /useExpectedFileMode/);
  assert.match(verifier, /digest\(canonicalJson\(contentObserved\)\) !== expectedDigest/);
  assert.match(verifier, /fs\.chmodSync\(absolute, item\.mode === "100755" \? 0o755 : 0o644\)/);
  assert.ok(verifier.indexOf("contentObserved") < verifier.indexOf("fs.chmodSync"));
  assert.doesNotMatch(initializer, /safe\.directory\s+['"]?\*['"]?/);
  assert.match(initializer, /UV_LINK_MODE=copy UV_NO_PROGRESS=1 uv pip install/);
  assert.match(initializer, /--offline --no-deps --no-build-isolation --reinstall/);
  assert.match(initializer, /installed_assets=\/opt\/venvs\/xiaodao\/lib\/python3\.12\/site-packages\/problem_locator\/runtime\/assets/);
  assert.match(initializer, /-type l -print -quit/);
  assert.match(initializer, /-type f -links \+1 -print -quit/);
  assert.match(initializer, /--logparse-config \/opt\/e2e-logparse\/config\.yaml/);
  assert.match(initializer, /--logparse-python \/opt\/venvs\/logparse\/bin\/python/);
  assert.match(initializer, /--logparse-repo \/opt\/src\/logparse/);
  assert.match(initializer, /logparse_config_sha256=\$\(sha256sum/);
  assert.match(supervisor, /LOGPARSE_CONFIG_PATH=\/opt\/e2e-logparse\/config\.yaml/);
  assert.doesNotMatch(supervisor, /LOGPARSE_CONFIG_PATH=\/opt\/src\/logparse\/config\.yaml/);
  assert.match(releaseCase, /frozen-raw-log-v1/);
  assert.match(releaseCase, /build_logparse_projection/);
  assert.match(releaseCase, /smoke_test_logparse/);
  assert.match(releaseCase, /release case Logparse process projection is invalid/);
  assert.match(releaseCase, /load_specialized_skill_registration/);
  assert.doesNotMatch(releaseCase, /diagnosis-skill\.json|generation_spec|approved_skill_dir/);
  const releaseRoot = path.join(TOOL_ROOT, "..", "..", "tests", "cases", "release");
  const cases = fs.readdirSync(releaseRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && fs.existsSync(path.join(releaseRoot, entry.name, "case.json")));
  assert.equal(cases.length, 1);
  const descriptor = JSON.parse(fs.readFileSync(path.join(releaseRoot, cases[0].name, "case.json"), "utf8"));
  const registration = JSON.parse(fs.readFileSync(path.join(releaseRoot, cases[0].name, descriptor.registration_template), "utf8"));
  assert.equal(releaseCase.includes(descriptor.case_id), false);
  assert.equal(releaseCase.includes(registration.runtime.preprocessing.logparse_product), false);
});

test("CrossJob runtime uses pull-never, empty labeled storage and authoritative server DFX", () => {
  const core = fs.readFileSync(path.join(TOOL_ROOT, "adapters", "cross-job-core.mjs"), "utf8");
  const probe = fs.readFileSync(path.join(SUPPORT_ROOT, "server_dfx_probe.py"), "utf8");
  const declared = probe.match(/EXPECTED_VALIDATION_FIELDS = \{([\s\S]*?)\n\}/);
  assert.notEqual(declared, null);
  const probeFields = [...declared[1].matchAll(/"([a-z_]+)"/g)].map((item) => item[1]).sort();
  assert.deepEqual(probeFields, [...NEGATIVE_PROBE_VALIDATION_FIELDS].sort());
  assert.ok(probeFields.includes("raw_problem_text"));
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
  assert.doesNotMatch(core, /fixedGetCasePollingInvariant\("<authoritative-case-id>"\)/);
  assert.match(core, /assertPhaseOneCaseFirst\(audit\)/);
  assert.match(core, /phaseOnePrompt\(\)/);
  assert.match(core, /phaseTwoPrompt\(state, configuration\.releaseCase, state\.archive\)/);
  assert.match(core, /fixedGetCasePollingInvariant\(state\.case_id\)/);
  assert.match(core, /Poll with the same literal get-case input/);
  assert.match(core, /runtime_ref_id: product\.runtime_ref_id/);
  for (const code of ["PHASE1_SELECTED_SKILL", "PHASE2_SELECTED_SKILL", "PHASE3_SELECTED_SKILL", "RESTART_SELECTED_SKILL"]) assert.match(core, new RegExp(code));
  assert.doesNotMatch(core, /selected_skill_ref\?\.id === releaseCase\.skill\.id/);
});

test("model invocations preserve failed terminals while PASS still requires exact caps and complete usage", () => {
  const core = fs.readFileSync(path.join(TOOL_ROOT, "adapters", "cross-job-core.mjs"), "utf8");
  const actions = fs.readFileSync(path.join(TOOL_ROOT, "lib", "actions.mjs"), "utf8");
  const engine = fs.readFileSync(path.join(TOOL_ROOT, "lib", "engine.mjs"), "utf8");
  const evidence = fs.readFileSync(path.join(TOOL_ROOT, "lib", "evidence.mjs"), "utf8");
  const isolated = fs.readFileSync(path.join(SUPPORT_ROOT, "isolated-agent-wrapper.mjs"), "utf8");
  const isolatedEnvironment = fs.readFileSync(path.join(SUPPORT_ROOT, "isolated-agent-env.mjs"), "utf8");
  const serviceAudit = fs.readFileSync(path.join(SUPPORT_ROOT, "audit_service_agent_usage.py"), "utf8");
  assert.match(core, /--max-turns/);
  assert.match(core, /--max-budget-usd/);
  assert.match(core, /max_total_tokens: configuration\.hardCaps\.max_total_tokens/);
  assert.match(core, /terminal: audit\.terminal/);
  assert.match(core, /usage_complete: true/);
  assert.match(core, /cache_creation_input_tokens/);
  assert.match(core, /cache_read_input_tokens/);
  assert.match(core, /terminal-usage-postcondition:\$\{TOKEN_USAGE_FORMULA\}/);
  assert.match(core, /wrapper_outcome: \{ schema_version: 1, status: "PASS", code: null \}/);
  assert.match(core, /function validSuccessfulInvocationReceipt/);
  assert.match(core, /function validServiceAgentInvocationReceipt/);
  assert.match(core, /export function validServiceAgentUsageReceipt/);
  assert.match(core, /validServiceAgentUsageReceipt\(receipt\)/);
  assert.match(core, /canonicalJson\(receipt\.new_job_ids\) === canonicalJson\(expectedJobIds\)/);
  assert.match(core, /Array\.isArray\(invocations\) && invocations\.every\(validSuccessfulInvocationReceipt\)/);
  assert.match(core, /jobTypes\.length === 1 && jobTypes\[0\] === "ROUTE"/);
  assert.match(core, /validRouteMethodsPreflightEvidence\(correspondence\.service_no_model_jobs/);
  assert.match(core, /registrationId: configuration\.generatedSkill\.registration_id/);
  assert.match(core, /expectedJobId: state\.methods_preflight_job_id/);
  assert.match(core, /methods_preflight_job_id: requestedBy\[0\]/);
  assert.match(core, /receipt\.result_type === "NEED_ATTACHMENT"/);
  assert.match(core, /receipt\.model_invoked === false/);
  assert.match(core, /receipt\.log_pair === "ABSENT"/);
  assert.match(core, /receipt\.job_sha256/);
  assert.match(core, /receipt\.job_outcome_sha256/);
  assert.match(core, /receipt\.methods_preflight_sha256/);
  assert.match(core, /validDirectMethodsServiceInvocations\(correspondence\.service_invocations\)/);
  assert.match(core, /SPECIALIZED_REVIEWER_ENABLED=true/);
  assert.match(core, /DIAGNOSE_UNEXPECTED_PREFLIGHT_ACTIVITY/);
  assert.match(isolated, /WRAPPER_MODEL_CAP_EXCEEDED/);
  assert.match(isolated, /cache_creation_input_tokens/);
  assert.match(isolated, /cache_read_input_tokens/);
  assert.match(isolated, /usage\.total_tokens > caps\.max_total_tokens/);
  assert.match(isolated, /terminalSucceeded = final\.subtype === "success" && final\.is_error === false/);
  assert.match(isolated, /wrapper_outcome:/);
  assert.match(isolated, /WRAPPER_MODEL_TERMINAL_INVALID/);
  assert.match(actions, /--max-output-tokens/);
  assert.match(actions, /max_output_tokens_upper_limit/);
  assert.match(isolated, /ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY/);
  assert.match(isolated, /allowClaudeChildControls: true/);
  assert.match(isolated, /ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT/);
  assert.match(isolatedEnvironment, /identity-bound-wrapper-arg\+child-only-env\+pinned-cli-upper-limit\+sealed-runtime-implementation/);
  assert.doesNotMatch(isolated, /modelUsage\[effectiveModel\]|effectiveLimit === declaredCap/);
  assert.match(actions, /if \(gate\.environment_profile && gate\.environment_profile !== "real-logparse"\)/);
  assert.match(isolated, /auditSkillGenerationTrace/);
  assert.match(isolated, /skillGenerationPermissionRules/);
  assert.match(isolated, /assertIsolatedAgentInboundEnvironment/);
  assert.match(isolated, /buildIsolatedAgentEnvironment/);
  assert.match(isolated, /provider_auth_source: "audited-settings-file"/);
  assert.match(actions, /environmentPolicy: isolatedAgent \? ISOLATED_AGENT_ENV_POLICY_VERSION : null/);
  assert.match(actions, /buildIsolatedAgentEnvironment\(\{ ambient: process\.env, explicit: processEnvironment \}\)/);
  assert.match(isolated, /"--permission-mode", "dontAsk"/);
  assert.match(isolated, /\["--tools", "Read,Write", "--dangerously-skip-permissions"\]/);
  assert.doesNotMatch(isolated, /"--no-session-persistence",\s*"--dangerously-skip-permissions"/);
  assert.match(engine, /invocation\.wrapper_outcome\?\.schema_version !== 1/);
  assert.match(evidence, /invocation\.wrapper_outcome\?\.schema_version === 1/);
  assert.match(serviceAudit, /MODEL_TERMINAL_INVALID/);
  assert.match(serviceAudit, /max_total_tokens/);
  assert.match(serviceAudit, /cache_creation_input_tokens/);
  assert.match(serviceAudit, /cache_read_input_tokens/);
  assert.match(serviceAudit, /observed\["total_tokens"\] > arguments\.max_total_tokens/);
  assert.match(serviceAudit, /"wrapper_outcome": \{\s*"schema_version": 1,\s*"status": "PASS",\s*"code": None/);
});

test("Skill generation grants only an audited Methods package subtree without exposing Bash", () => {
  const audit = fs.readFileSync(path.join(SUPPORT_ROOT, "isolated-agent-tool-audit.mjs"), "utf8");
  const wrapper = fs.readFileSync(path.join(SUPPORT_ROOT, "isolated-agent-wrapper.mjs"), "utf8");
  const actions = fs.readFileSync(path.join(TOOL_ROOT, "lib", "actions.mjs"), "utf8");
  const realGate = fs.readFileSync(path.join(TOOL_ROOT, "..", "..", "tests", "real", "agent", "test_real_wiki_skill_generation_gate.py"), "utf8");
  assert.match(audit, /const ALLOWED_TOOLS = Object\.freeze\(\["Skill", "Read", "Write"\]\)/);
  assert.match(audit, /"Edit\(\/output\/\*\*\)"/);
  assert.doesNotMatch(audit, /generation-spec\.json/);
  assert.match(audit, /writeRecords\.length >= 3/);
  assert.match(audit, /Failed tool calls are forbidden in Methods generation/);
  assert.match(audit, /Every package file must be created by exactly one successful Write/);
  assert.match(audit, /output\/<skill>\/\{SKILL\.md,methods\.json,references\/\*\.md\}/);
  assert.match(audit, /validSkillGenerationTraceAuditReceipt/);
  assert.match(audit, /exactKeys\(record\.input, \["file_path", "content"\]\)/);
  assert.match(audit, /record\.input\.content\.length > 0/);
  assert.match(wrapper, /"--tools", "Read,Write,Skill"/);
  assert.match(wrapper, /"--permission-mode", "dontAsk"/);
  assert.match(wrapper, /schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION/);
  assert.match(actions, /validSkillGenerationTraceAuditReceipt\(audit\)/);
  assert.match(actions, /"\.agents", "skills", skillName/);
  assert.match(realGate, /Use exactly one successful Write call per final package file/);
  assert.match(realGate, /Do not call Bash, Edit, Glob, Grep/);
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
