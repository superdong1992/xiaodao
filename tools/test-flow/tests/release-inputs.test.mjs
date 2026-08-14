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
import { NEGATIVE_PROBE_VALIDATION_FIELDS } from "../lib/events.mjs";

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
    "initialize-container.sh", "isolated-agent-env.mjs", "isolated-agent-tool-audit.mjs", "isolated-agent-wrapper.mjs", "prepare_claude_settings.py",
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
  assert.match(releaseCase, /logparse-current-loose-diagnostic-v2/);
  assert.match(releaseCase, /build_logparse_projection/);
  assert.match(releaseCase, /smoke_test_logparse/);
  assert.match(releaseCase, /release case Logparse process projection is invalid/);
  const releaseRoot = path.join(TOOL_ROOT, "..", "..", "tests", "cases", "release");
  const cases = fs.readdirSync(releaseRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && fs.existsSync(path.join(releaseRoot, entry.name, "case.json")));
  assert.equal(cases.length, 1);
  const descriptor = JSON.parse(fs.readFileSync(path.join(releaseRoot, cases[0].name, "case.json"), "utf8"));
  const approved = JSON.parse(fs.readFileSync(path.join(releaseRoot, cases[0].name, descriptor.approved_skill_dir, "diagnosis-skill.json"), "utf8"));
  assert.equal(releaseCase.includes(descriptor.case_id), false);
  assert.equal(releaseCase.includes(approved.logparse_product), false);
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
  assert.match(core, /runtime_ref_id: diagnosisSkillRuntimeRefId\(skillManifest\.id\)/);
  for (const code of ["PHASE1_SELECTED_SKILL", "PHASE3_SELECTED_SKILL", "RESTART_SELECTED_SKILL"]) {
    assert.match(core, new RegExp(`selected_skill_ref\\?\\.id === releaseCase\\.skill\\.runtime_ref_id[^\\n]+${code}`));
  }
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
  assert.match(core, /receipt\.invocations\.every\(validSuccessfulInvocationReceipt\)/);
  assert.match(core, /Array\.isArray\(invocations\) && invocations\.every\(validSuccessfulInvocationReceipt\)/);
  assert.match(core, /jobTypes\.filter\(\(item\) => item === "ROUTE"\)\.length === 1/);
  assert.match(core, /jobTypes\.includes\("DIAGNOSE"\) && jobTypes\.every\(\(item\) => \["DIAGNOSE", "ROUTE"\]\.includes\(item\)\)/);
  assert.match(core, /jobTypes\.includes\("DIAGNOSE"\) && jobTypes\.includes\("REVIEW"\)/);
  assert.match(core, /jobTypes\.every\(\(item\) => \["DIAGNOSE", "REVIEW"\]\.includes\(item\)\)/);
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

test("Skill generation grants one exact file-write permission without exposing Edit or Bash", () => {
  const audit = fs.readFileSync(path.join(SUPPORT_ROOT, "isolated-agent-tool-audit.mjs"), "utf8");
  const wrapper = fs.readFileSync(path.join(SUPPORT_ROOT, "isolated-agent-wrapper.mjs"), "utf8");
  const actions = fs.readFileSync(path.join(TOOL_ROOT, "lib", "actions.mjs"), "utf8");
  const realGate = fs.readFileSync(path.join(TOOL_ROOT, "..", "..", "tests", "real", "agent", "test_real_wiki_skill_generation_gate.py"), "utf8");
  assert.match(audit, /const ALLOWED_TOOLS = Object\.freeze\(\["Skill", "Read", "Write"\]\)/);
  assert.match(audit, /"Edit\(\/output\/generation-spec\.json\)"/);
  assert.doesNotMatch(audit, /"Write\(\/output\/generation-spec\.json\)"/);
  assert.match(audit, /successfulWrites\.length === 1/);
  assert.match(audit, /max_empty_write_rejections: 1/);
  assert.match(audit, /exactKeys\(record\.input, \[\]\)/);
  assert.match(audit, /record\.result\.explicit_error === true/);
  assert.match(audit, /rejectedWrite\.ordinal === successfulWrite\.ordinal - 1/);
  assert.match(audit, /rejectedWrite\.result_event_index < successfulWrite\.use_event_index/);
  assert.match(audit, /validSkillGenerationTraceAuditReceipt/);
  assert.match(audit, /exactKeys\(record\.input, \["file_path", "content"\]\)/);
  assert.match(audit, /record\.input\.content\.trim\(\)\.length > 0/);
  assert.match(wrapper, /"--tools", "Read,Write,Skill"/);
  assert.match(wrapper, /"--permission-mode", "dontAsk"/);
  assert.match(wrapper, /schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION/);
  assert.match(actions, /validSkillGenerationTraceAuditReceipt\(audit\)/);
  assert.match(realGate, /call Write exactly once with both `file_path` and non-empty `content`/);
  assert.match(realGate, /do not use Bash or another file-writing tool/);
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
