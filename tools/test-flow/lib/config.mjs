import fs from "node:fs";
import path from "node:path";
import { assertFlow, canonicalJson, readJson, sha256Bytes, sha256File } from "./util.mjs";

const IDENTIFIER = /^[a-z0-9][a-z0-9.-]*$/;
const PLATFORMS = new Set(["windows", "macos", "linux"]);
const STAGE_KINDS = new Set(["deterministic", "capability", "isolated-real", "real-journey", "observation"]);
const GATE_KINDS = new Set(["node-test", "pytest", "repository-check", "capability-adapter", "cross-job-adapter", "observation"]);
const REUSE_POLICIES = new Set(["never", "identity", "checkpoint-chain"]);
const PROGRESS_CLASSES = new Set(["local", "external", "real"]);
const PYTEST_SKIP_POLICIES = new Set(["forbid", "forbid-all-skipped", "allow-explicit"]);
const REPOSITORY_CHECKS = new Set(["python-compileall", "uv-lock", "git-diff-check"]);
const CAPABILITY_ADAPTERS = new Set(["host-capability", "server-linux-capability"]);
const CROSS_JOB_PHASES = new Set(["environment", "route", "upload", "diagnose", "publish-restart"]);
const OBSERVATIONS = new Set(["review-state-transition"]);
const ENVIRONMENT_PROFILES = new Set(["real-logparse", "real-agent-backend", "real-route", "real-diagnose", "real-review"]);
const RELEASE_SETTINGS_ENVIRONMENT = Object.freeze([
  "ANTHROPIC_AUTH_TOKEN",
  "ANTHROPIC_BASE_URL",
  "ANTHROPIC_DEFAULT_HAIKU_MODEL",
  "ANTHROPIC_DEFAULT_OPUS_MODEL",
  "ANTHROPIC_DEFAULT_SONNET_MODEL",
  "API_TIMEOUT_MS",
  "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
]);

function object(value, code, label) {
  assertFlow(value !== null && typeof value === "object" && !Array.isArray(value), code, `${label} must be an object`);
  return value;
}

function exactKeys(value, allowed, code, label) {
  object(value, code, label);
  const unexpected = Object.keys(value).filter((key) => !allowed.includes(key));
  assertFlow(unexpected.length === 0, code, `${label} has unknown field(s): ${unexpected.join(", ")}`);
}

function identifier(value, code, label) {
  assertFlow(typeof value === "string" && IDENTIFIER.test(value), code, `Invalid ${label}: ${String(value)}`);
}

function stringArray(value, code, label, { nonEmpty = false, unique = true } = {}) {
  assertFlow(Array.isArray(value), code, `${label} must be an array`);
  assertFlow(!nonEmpty || value.length > 0, code, `${label} must be non-empty`);
  assertFlow(value.every((entry) => typeof entry === "string" && entry.length > 0), code, `${label} must contain non-empty strings`);
  assertFlow(!unique || new Set(value).size === value.length, code, `${label} must not contain duplicates`);
}

function relativePath(value, code, label) {
  assertFlow(typeof value === "string" && value.length > 0 && !path.isAbsolute(value), code, `${label} must be repo-relative`);
  const normalized = path.posix.normalize(value.replaceAll("\\", "/"));
  assertFlow(normalized === value && normalized !== ".." && !normalized.startsWith("../"), code, `${label} escapes the repository`);
}

function positiveInteger(value, code, label) {
  assertFlow(Number.isInteger(value) && value > 0, code, `${label} must be a positive integer`);
}

function nonNegativeNumber(value, code, label) {
  assertFlow(Number.isFinite(value) && value >= 0, code, `${label} must be a non-negative number`);
}

function nonEmptyString(value, code, label) {
  assertFlow(typeof value === "string" && value.length > 0, code, `${label} must be a non-empty string`);
}

function sha256(value, code, label) {
  assertFlow(typeof value === "string" && /^[a-f0-9]{64}$/.test(value), code, `${label} must be a lowercase SHA-256`);
}

function validateProofs(proofs) {
  exactKeys(proofs, ["schema_version", "goals", "proofs"], "CONFIG_PROOFS_FIELDS", "proof config");
  assertFlow(proofs.schema_version === 2, "CONFIG_PROOFS_VERSION", "Unsupported proof schema version");
  object(proofs.goals, "CONFIG_GOALS", "goals");
  object(proofs.proofs, "CONFIG_PROOFS", "proofs");
  assertFlow(Object.keys(proofs.goals).length > 0, "CONFIG_GOALS_EMPTY", "goals must be non-empty");
  assertFlow(Object.keys(proofs.proofs).length > 0, "CONFIG_PROOFS_EMPTY", "proofs must be non-empty");
  for (const [goalId, goal] of Object.entries(proofs.goals)) {
    identifier(goalId, "CONFIG_GOAL_ID", "goal id");
    exactKeys(goal, ["description", "tracks", "required_proofs", "selectable_proofs"], "CONFIG_GOAL_FIELDS", `goal ${goalId}`);
    assertFlow(typeof goal.description === "string" && goal.description.length > 0, "CONFIG_GOAL_DESCRIPTION", `${goalId} needs a description`);
    stringArray(goal.tracks, "CONFIG_GOAL_TRACKS", `${goalId}.tracks`, { nonEmpty: true });
    assertFlow(goal.tracks.every((track) => ["dev", "release"].includes(track)), "CONFIG_GOAL_TRACK", `${goalId} has an unsupported track`);
    stringArray(goal.required_proofs, "CONFIG_GOAL_PROOFS", `${goalId}.required_proofs`, { nonEmpty: true });
    stringArray(goal.selectable_proofs, "CONFIG_GOAL_SELECTABLE", `${goalId}.selectable_proofs`);
  }
  for (const [proofId, proof] of Object.entries(proofs.proofs)) {
    identifier(proofId, "CONFIG_PROOF_ID", "proof id");
    exactKeys(proof, ["description", "acceptance", "stages"], "CONFIG_PROOF_FIELDS", `proof ${proofId}`);
    assertFlow(typeof proof.description === "string" && proof.description.length > 0, "CONFIG_PROOF_DESCRIPTION", `${proofId} needs a description`);
    assertFlow(proof.acceptance === "all", "CONFIG_PROOF_ACCEPTANCE", `${proofId} must use acceptance=all`);
    stringArray(proof.stages, "CONFIG_PROOF_STAGES", `${proofId}.stages`, { nonEmpty: true });
  }
}

function validateStages(stages) {
  exactKeys(stages, ["schema_version", "stages"], "CONFIG_STAGES_FIELDS", "stage config");
  assertFlow(stages.schema_version === 2, "CONFIG_STAGES_VERSION", "Unsupported stage schema version");
  assertFlow(Array.isArray(stages.stages) && stages.stages.length > 0, "CONFIG_STAGES_EMPTY", "stages must be non-empty");
  const ids = new Set();
  for (const stage of stages.stages) {
    exactKeys(stage, ["id", "kind", "depends_on", "gates", "identity_set", "timeout_seconds", "progress_class", "reuse", "checkpoint", "platforms"], "CONFIG_STAGE_FIELDS", `stage ${stage?.id ?? "?"}`);
    identifier(stage.id, "CONFIG_STAGE_ID", "stage id");
    assertFlow(!ids.has(stage.id), "CONFIG_STAGE_DUPLICATE", `Duplicate stage ${stage.id}`);
    ids.add(stage.id);
    assertFlow(STAGE_KINDS.has(stage.kind), "CONFIG_STAGE_KIND", `Unsupported kind for ${stage.id}`);
    stringArray(stage.depends_on, "CONFIG_STAGE_DEPENDENCIES", `${stage.id}.depends_on`);
    stringArray(stage.gates, "CONFIG_STAGE_GATES", `${stage.id}.gates`, { nonEmpty: true });
    identifier(stage.identity_set, "CONFIG_STAGE_IDENTITY", `${stage.id} identity set`);
    positiveInteger(stage.timeout_seconds, "CONFIG_STAGE_TIMEOUT", `${stage.id}.timeout_seconds`);
    assertFlow(PROGRESS_CLASSES.has(stage.progress_class), "CONFIG_STAGE_PROGRESS", `${stage.id} has invalid progress_class`);
    exactKeys(stage.reuse, ["dev", "release"], "CONFIG_STAGE_REUSE_FIELDS", `${stage.id}.reuse`);
    assertFlow(REUSE_POLICIES.has(stage.reuse.dev) && REUSE_POLICIES.has(stage.reuse.release), "CONFIG_STAGE_REUSE", `${stage.id} has invalid reuse policy`);
    stringArray(stage.platforms, "CONFIG_STAGE_PLATFORMS", `${stage.id}.platforms`, { nonEmpty: true });
    assertFlow(stage.platforms.every((platform) => PLATFORMS.has(platform)), "CONFIG_STAGE_PLATFORM", `${stage.id} has invalid platform`);
    if (Object.hasOwn(stage, "checkpoint") && stage.checkpoint !== null) {
      exactKeys(stage.checkpoint, ["next_stage"], "CONFIG_CHECKPOINT_FIELDS", `${stage.id}.checkpoint`);
      assertFlow(stage.checkpoint.next_stage === null || (typeof stage.checkpoint.next_stage === "string" && IDENTIFIER.test(stage.checkpoint.next_stage)), "CONFIG_CHECKPOINT_NEXT", `${stage.id} has invalid checkpoint next_stage`);
      assertFlow(stage.reuse.dev === "checkpoint-chain", "CONFIG_CHECKPOINT_REUSE", `${stage.id} checkpoint requires Dev checkpoint-chain reuse`);
    }
    if (stage.kind === "real-journey") {
      assertFlow(stage.reuse.release === "never", "CONFIG_RELEASE_JOURNEY_REUSE", `${stage.id} must never reuse in Release`);
    }
  }
}

const GATE_FIELDS = {
  "node-test": ["kind", "test_files", "test_glob", "exclude", "min_passed", "evidence"],
  pytest: ["kind", "selectors", "selector_mode", "pytest_args", "environment_profile", "min_passed", "skip_policy", "runtime_profile", "evidence"],
  "repository-check": ["kind", "check", "paths", "evidence"],
  "capability-adapter": ["kind", "adapter", "runtime_profile", "required_claims", "evidence"],
  "cross-job-adapter": ["kind", "phase", "runtime_profile", "evidence_contract", "evidence"],
  observation: ["kind", "observation", "evidence_contract", "evidence"],
};

function validateGates(gates) {
  exactKeys(gates, ["schema_version", "gates"], "CONFIG_GATES_FIELDS", "gate config");
  assertFlow(gates.schema_version === 2, "CONFIG_GATES_VERSION", "Unsupported gate schema version");
  object(gates.gates, "CONFIG_GATES", "gates");
  assertFlow(Object.keys(gates.gates).length > 0, "CONFIG_GATES_EMPTY", "gates must be non-empty");
  for (const [gateId, gate] of Object.entries(gates.gates)) {
    identifier(gateId, "CONFIG_GATE_ID", "gate id");
    object(gate, "CONFIG_GATE_OBJECT", `gate ${gateId}`);
    assertFlow(GATE_KINDS.has(gate.kind), "CONFIG_GATE_KIND", `Unsupported gate kind for ${gateId}`);
    exactKeys(gate, GATE_FIELDS[gate.kind], "CONFIG_GATE_FIELDS", `gate ${gateId}`);
    stringArray(gate.evidence, "CONFIG_GATE_EVIDENCE", `${gateId}.evidence`, { nonEmpty: true });
    if (gate.kind === "node-test") {
      assertFlow(Boolean(gate.test_files) !== Boolean(gate.test_glob), "CONFIG_NODE_TEST_SELECTOR", `${gateId} must define exactly one node-test selector`);
      if (gate.test_files) {
        stringArray(gate.test_files, "CONFIG_NODE_TEST_FILES", `${gateId}.test_files`, { nonEmpty: true });
        gate.test_files.forEach((entry) => relativePath(entry, "CONFIG_NODE_TEST_PATH", `${gateId} test file`));
      }
      if (gate.test_glob) {
        relativePath(gate.test_glob, "CONFIG_NODE_TEST_GLOB", `${gateId}.test_glob`);
        assertFlow(gate.test_glob.endsWith("*.test.mjs"), "CONFIG_NODE_TEST_GLOB", `${gateId} test_glob is not allowlisted`);
      }
      if (gate.exclude) {
        stringArray(gate.exclude, "CONFIG_NODE_TEST_EXCLUDE", `${gateId}.exclude`);
        gate.exclude.forEach((entry) => relativePath(entry, "CONFIG_NODE_TEST_PATH", `${gateId} excluded file`));
      }
      positiveInteger(gate.min_passed, "CONFIG_NODE_TEST_MIN", `${gateId}.min_passed`);
    } else if (gate.kind === "pytest") {
      assertFlow(Boolean(gate.selectors) !== Boolean(gate.selector_mode), "CONFIG_PYTEST_SELECTOR", `${gateId} must define selectors or selector_mode`);
      if (gate.selectors) {
        stringArray(gate.selectors, "CONFIG_PYTEST_SELECTORS", `${gateId}.selectors`, { nonEmpty: true });
        gate.selectors.forEach((entry) => relativePath(entry.split("::", 1)[0], "CONFIG_PYTEST_PATH", `${gateId} selector`));
      }
      if (gate.selector_mode) assertFlow(gate.selector_mode === "affected", "CONFIG_PYTEST_SELECTOR_MODE", `${gateId} has unsupported selector_mode`);
      if (gate.pytest_args) stringArray(gate.pytest_args, "CONFIG_PYTEST_ARGS", `${gateId}.pytest_args`);
      if (gate.environment_profile) assertFlow(ENVIRONMENT_PROFILES.has(gate.environment_profile), "CONFIG_PYTEST_ENVIRONMENT", `${gateId} has unknown environment profile`);
      positiveInteger(gate.min_passed, "CONFIG_PYTEST_MIN", `${gateId}.min_passed`);
      assertFlow(PYTEST_SKIP_POLICIES.has(gate.skip_policy), "CONFIG_PYTEST_SKIP", `${gateId} has invalid skip policy`);
      identifier(gate.runtime_profile, "CONFIG_GATE_RUNTIME", `${gateId} runtime profile`);
    } else if (gate.kind === "repository-check") {
      assertFlow(REPOSITORY_CHECKS.has(gate.check), "CONFIG_REPOSITORY_CHECK", `${gateId} has untrusted repository check`);
      if (gate.paths) {
        stringArray(gate.paths, "CONFIG_REPOSITORY_PATHS", `${gateId}.paths`, { nonEmpty: true });
        gate.paths.forEach((entry) => relativePath(entry, "CONFIG_REPOSITORY_PATH", `${gateId} path`));
      }
      assertFlow((gate.check === "python-compileall") === Boolean(gate.paths), "CONFIG_REPOSITORY_PATHS", `${gateId} paths do not match check kind`);
    } else if (gate.kind === "capability-adapter") {
      assertFlow(CAPABILITY_ADAPTERS.has(gate.adapter), "CONFIG_CAPABILITY_ADAPTER", `${gateId} has untrusted adapter`);
      identifier(gate.runtime_profile, "CONFIG_GATE_RUNTIME", `${gateId} runtime profile`);
      if (gate.required_claims) stringArray(gate.required_claims, "CONFIG_CAPABILITY_CLAIMS", `${gateId}.required_claims`, { nonEmpty: true });
    } else if (gate.kind === "cross-job-adapter") {
      assertFlow(CROSS_JOB_PHASES.has(gate.phase), "CONFIG_CROSS_JOB_PHASE", `${gateId} has invalid phase`);
      identifier(gate.runtime_profile, "CONFIG_GATE_RUNTIME", `${gateId} runtime profile`);
      identifier(gate.evidence_contract, "CONFIG_EVIDENCE_CONTRACT", `${gateId} evidence contract`);
    } else if (gate.kind === "observation") {
      assertFlow(OBSERVATIONS.has(gate.observation), "CONFIG_OBSERVATION", `${gateId} has untrusted observation`);
      identifier(gate.evidence_contract, "CONFIG_EVIDENCE_CONTRACT", `${gateId} evidence contract`);
    }
  }
}

function validateIdentities(identities) {
  exactKeys(identities, ["schema_version", "components", "sets"], "CONFIG_IDENTITIES_FIELDS", "identity config");
  assertFlow(identities.schema_version === 2, "CONFIG_IDENTITIES_VERSION", "Unsupported identity schema version");
  object(identities.components, "CONFIG_IDENTITY_COMPONENTS", "identity components");
  object(identities.sets, "CONFIG_IDENTITY_SETS", "identity sets");
  const kinds = new Set(["paths", "external-tree", "client-distribution", "claude-settings", "release-runtime", "environment"]);
  for (const [componentId, component] of Object.entries(identities.components)) {
    identifier(componentId, "CONFIG_IDENTITY_COMPONENT_ID", "identity component id");
    object(component, "CONFIG_IDENTITY_COMPONENT", `identity component ${componentId}`);
    assertFlow(kinds.has(component.kind), "CONFIG_IDENTITY_COMPONENT_KIND", `${componentId} has invalid kind`);
    exactKeys(component, component.kind === "paths" ? ["kind", "paths"] : component.kind === "external-tree" ? ["kind", "name"] : ["kind"], "CONFIG_IDENTITY_COMPONENT_FIELDS", `identity component ${componentId}`);
    if (component.kind === "paths") {
      stringArray(component.paths, "CONFIG_IDENTITY_PATHS", `${componentId}.paths`, { nonEmpty: true });
      component.paths.forEach((entry) => relativePath(entry, "CONFIG_IDENTITY_PATH", `${componentId} path`));
    }
    if (component.kind === "external-tree") identifier(component.name, "CONFIG_IDENTITY_EXTERNAL", `${componentId} external name`);
  }
  for (const [setId, set] of Object.entries(identities.sets)) {
    identifier(setId, "CONFIG_IDENTITY_SET_ID", "identity set id");
    exactKeys(set, ["producer", "proof"], "CONFIG_IDENTITY_SET_FIELDS", `identity set ${setId}`);
    stringArray(set.producer, "CONFIG_IDENTITY_SET_PRODUCER", `${setId}.producer`, { nonEmpty: true });
    stringArray(set.proof, "CONFIG_IDENTITY_SET_PROOF", `${setId}.proof`, { nonEmpty: true });
  }
}

function validatePolicy(policy) {
  exactKeys(policy, ["schema_version", "defaults", "tracks", "process", "evidence", "performance", "retry", "status"], "CONFIG_POLICY_FIELDS", "policy config");
  assertFlow(policy.schema_version === 2, "CONFIG_POLICY_VERSION", "Unsupported policy schema version");
  exactKeys(policy.defaults, ["track", "goal", "client", "resume", "evidence_root", "runtime_profile"], "CONFIG_POLICY_DEFAULT_FIELDS", "policy defaults");
  assertFlow(["dev", "release"].includes(policy.defaults.track), "CONFIG_DEFAULT_TRACK", "Invalid default track");
  identifier(policy.defaults.goal, "CONFIG_DEFAULT_GOAL", "default goal");
  assertFlow(policy.defaults.client === "auto", "CONFIG_DEFAULT_CLIENT", "Default client must be auto");
  assertFlow(policy.defaults.resume === "auto", "CONFIG_DEFAULT_RESUME", "Default resume must be auto");
  relativePath(policy.defaults.evidence_root, "CONFIG_EVIDENCE_ROOT", "default evidence root");
  identifier(policy.defaults.runtime_profile, "CONFIG_DEFAULT_RUNTIME", "default runtime profile");
  exactKeys(policy.tracks, ["dev", "release"], "CONFIG_TRACKS_FIELDS", "tracks");
  for (const [trackId, track] of Object.entries(policy.tracks)) {
    exactKeys(track, ["default_goal", "requires_clean_commit", "real_requires_opt_in", "real_requires_intent", "performance_effect"], "CONFIG_TRACK_FIELDS", `track ${trackId}`);
    identifier(track.default_goal, "CONFIG_TRACK_GOAL", `${trackId} default goal`);
    assertFlow(typeof track.requires_clean_commit === "boolean" && typeof track.real_requires_opt_in === "boolean" && typeof track.real_requires_intent === "boolean", "CONFIG_TRACK_BOOLEAN", `${trackId} has invalid boolean policy`);
    assertFlow(["warn", "gate"].includes(track.performance_effect), "CONFIG_TRACK_PERFORMANCE", `${trackId} has invalid performance effect`);
  }
  exactKeys(policy.process, ["real_no_progress_seconds", "real_hard_timeout_seconds", "poll_milliseconds", "progress_allowlist_version"], "CONFIG_PROCESS_FIELDS", "process policy");
  positiveInteger(policy.process.real_no_progress_seconds, "CONFIG_PROCESS_NO_PROGRESS", "real_no_progress_seconds");
  positiveInteger(policy.process.real_hard_timeout_seconds, "CONFIG_PROCESS_HARD_TIMEOUT", "real_hard_timeout_seconds");
  positiveInteger(policy.process.poll_milliseconds, "CONFIG_PROCESS_POLL", "poll_milliseconds");
  assertFlow(policy.process.progress_allowlist_version === "test-flow-progress-v2", "CONFIG_PROCESS_VERSION", "Unsupported progress allowlist version");
  exactKeys(policy.evidence, ["event_visibility_seconds", "event_file_limit_bytes", "raw_log_file_limit_bytes", "scanner_version", "event_contract_version", "verdict_schema_version"], "CONFIG_EVIDENCE_FIELDS", "evidence policy");
  for (const name of ["event_visibility_seconds", "event_file_limit_bytes", "raw_log_file_limit_bytes", "verdict_schema_version"]) positiveInteger(policy.evidence[name], "CONFIG_EVIDENCE_LIMIT", name);
  assertFlow(policy.evidence.scanner_version === "test-flow-secret-scan-v2", "CONFIG_SCANNER_VERSION", "Unsupported scanner version");
  assertFlow(policy.evidence.event_contract_version === "server-dfx-v2", "CONFIG_EVENT_VERSION", "Unsupported event contract version");
  assertFlow(policy.evidence.verdict_schema_version === 2, "CONFIG_VERDICT_VERSION", "Unsupported verdict schema version");
  exactKeys(policy.performance, ["policy_version", "window", "min_samples", "consecutive_release_failures", "mad_multiplier", "relative_floor", "local_absolute_floor_seconds", "external_absolute_floor_seconds", "stages"], "CONFIG_PERFORMANCE_FIELDS", "performance policy");
  assertFlow(policy.performance.policy_version === "robust-mad-v2", "CONFIG_PERFORMANCE_VERSION", "Unsupported performance policy version");
  for (const name of ["window", "min_samples", "consecutive_release_failures"]) positiveInteger(policy.performance[name], "CONFIG_PERFORMANCE_INTEGER", name);
  for (const name of ["mad_multiplier", "relative_floor", "local_absolute_floor_seconds", "external_absolute_floor_seconds"]) nonNegativeNumber(policy.performance[name], "CONFIG_PERFORMANCE_NUMBER", name);
  assertFlow(policy.performance.min_samples <= policy.performance.window, "CONFIG_PERFORMANCE_WINDOW", "min_samples exceeds window");
  object(policy.performance.stages, "CONFIG_PERFORMANCE_STAGES", "performance stage policies");
  for (const [stageId, stagePolicy] of Object.entries(policy.performance.stages)) {
    assertFlow(stageId === "*" || IDENTIFIER.test(stageId), "CONFIG_PERFORMANCE_STAGE", `Invalid performance stage ${stageId}`);
    exactKeys(stagePolicy, ["mode", "hard_cap_seconds"], "CONFIG_PERFORMANCE_STAGE_FIELDS", `performance stage ${stageId}`);
    assertFlow(["observe", "warn", "gate"].includes(stagePolicy.mode), "CONFIG_PERFORMANCE_MODE", `${stageId} has invalid performance mode`);
    assertFlow(stagePolicy.hard_cap_seconds === null || (Number.isFinite(stagePolicy.hard_cap_seconds) && stagePolicy.hard_cap_seconds > 0), "CONFIG_PERFORMANCE_CAP", `${stageId} has invalid hard cap`);
  }
  exactKeys(policy.retry, ["same_identity_requires", "automatic_blind_retry"], "CONFIG_RETRY_FIELDS", "retry policy");
  stringArray(policy.retry.same_identity_requires, "CONFIG_RETRY_REQUIREMENTS", "retry requirements", { nonEmpty: true });
  assertFlow(canonicalJson([...policy.retry.same_identity_requires].sort()) === canonicalJson(["expected_evidence", "hypothesis", "reason"]), "CONFIG_RETRY_REQUIREMENTS", "Retry intent must require reason, hypothesis and expected evidence");
  assertFlow(policy.retry.automatic_blind_retry === false, "CONFIG_BLIND_RETRY", "automatic blind retry must remain disabled");
  exactKeys(policy.status, ["pass", "pass_with_warnings", "fail", "blocked", "error"], "CONFIG_STATUS_FIELDS", "status policy");
  assertFlow(canonicalJson(policy.status) === canonicalJson({ pass: 0, pass_with_warnings: 0, fail: 1, blocked: 2, error: 3 }), "CONFIG_STATUS_CODES", "Unsupported status exit mapping");
}

function validateRuntimeProfiles(runtimeProfiles) {
  exactKeys(runtimeProfiles, ["schema_version", "profiles"], "CONFIG_RUNTIME_FIELDS", "runtime profile config");
  assertFlow(runtimeProfiles.schema_version === 2, "CONFIG_RUNTIME_VERSION", "Unsupported runtime profile schema version");
  object(runtimeProfiles.profiles, "CONFIG_RUNTIME_PROFILES", "runtime profiles");
  for (const [profileId, profile] of Object.entries(runtimeProfiles.profiles)) {
    identifier(profileId, "CONFIG_RUNTIME_ID", "runtime profile id");
    if (profile.kind === "python") {
      exactKeys(profile, ["kind", "version", "environment_allowlist"], "CONFIG_RUNTIME_PROFILE_FIELDS", `runtime profile ${profileId}`);
      assertFlow(/^3\.12(?:\.|$)/.test(profile.version), "CONFIG_PYTHON_VERSION", `${profileId} requires Python 3.12`);
      stringArray(profile.environment_allowlist, "CONFIG_RUNTIME_ENV", `${profileId}.environment_allowlist`);
      continue;
    }
    assertFlow(profile.kind === "formal-release", "CONFIG_RUNTIME_KIND", `${profileId} has invalid runtime kind`);
    exactKeys(profile, ["kind", "claude", "uv", "python", "hatchling", "base_image", "external_sources", "settings_environment_allowlist", "real_caps", "network_policy"], "CONFIG_RUNTIME_PROFILE_FIELDS", `runtime profile ${profileId}`);
    exactKeys(profile.claude, ["package", "version", "version_output", "tarball_sha256", "cli_sha256", "model"], "CONFIG_RUNTIME_CLAUDE_FIELDS", `${profileId}.claude`);
    exactKeys(profile.uv, ["version", "archive_sha256", "uv_sha256", "uvx_sha256"], "CONFIG_RUNTIME_UV_FIELDS", `${profileId}.uv`);
    exactKeys(profile.base_image, ["name", "source", "os", "architecture", "macos_docker_context"], "CONFIG_RUNTIME_IMAGE_FIELDS", `${profileId}.base_image`);
    exactKeys(profile.external_sources, ["logparse", "mcp"], "CONFIG_RUNTIME_EXTERNAL_FIELDS", `${profileId}.external_sources`);
    assertFlow(profile.claude.package === "@anthropic-ai/claude-code", "CONFIG_RUNTIME_CLAUDE_PACKAGE", `${profileId} must use the official Claude Code package`);
    nonEmptyString(profile.claude.version, "CONFIG_RUNTIME_CLAUDE_VERSION", `${profileId}.claude.version`);
    assertFlow(profile.claude.version_output === `${profile.claude.version} (Claude Code)`, "CONFIG_RUNTIME_CLAUDE_VERSION_OUTPUT", `${profileId} has an invalid Claude version output`);
    sha256(profile.claude.tarball_sha256, "CONFIG_RUNTIME_CLAUDE_HASH", `${profileId}.claude.tarball_sha256`);
    sha256(profile.claude.cli_sha256, "CONFIG_RUNTIME_CLAUDE_HASH", `${profileId}.claude.cli_sha256`);
    assertFlow(/^[a-zA-Z0-9_.\[\]-]+$/.test(profile.claude.model), "CONFIG_RUNTIME_MODEL", `${profileId}.claude.model is invalid`);
    nonEmptyString(profile.uv.version, "CONFIG_RUNTIME_UV_VERSION", `${profileId}.uv.version`);
    for (const name of ["archive_sha256", "uv_sha256", "uvx_sha256"]) sha256(profile.uv[name], "CONFIG_RUNTIME_UV_HASH", `${profileId}.uv.${name}`);
    assertFlow(/^3\.12\.\d+$/.test(profile.python), "CONFIG_RUNTIME_PYTHON_VERSION", `${profileId}.python must pin Python 3.12`);
    nonEmptyString(profile.hatchling, "CONFIG_RUNTIME_HATCHLING", `${profileId}.hatchling`);
    assertFlow(/^[a-z0-9][a-z0-9_.:/@-]+$/.test(profile.base_image.name), "CONFIG_RUNTIME_IMAGE_NAME", `${profileId}.base_image.name is invalid`);
    assertFlow(/^.+@sha256:[a-f0-9]{64}$/.test(profile.base_image.source), "CONFIG_RUNTIME_IMAGE_SOURCE", `${profileId}.base_image.source must be digest-pinned`);
    assertFlow(profile.base_image.os === "linux" && profile.base_image.architecture === "amd64" && profile.base_image.macos_docker_context === "colima", "CONFIG_RUNTIME_IMAGE_PLATFORM", `${profileId}.base_image has an unsupported platform`);
    for (const [name, commit] of Object.entries(profile.external_sources)) assertFlow(/^[a-f0-9]{40}$/.test(commit), "CONFIG_RUNTIME_EXTERNAL_COMMIT", `${profileId}.external_sources.${name} must be a commit SHA`);
    stringArray(profile.settings_environment_allowlist, "CONFIG_RUNTIME_SETTINGS_ENV", `${profileId}.settings_environment_allowlist`, { nonEmpty: true });
    assertFlow(canonicalJson([...profile.settings_environment_allowlist].sort()) === canonicalJson([...RELEASE_SETTINGS_ENVIRONMENT].sort()), "CONFIG_RUNTIME_SETTINGS_ENV", `${profileId} has an unsupported settings environment allowlist`);
    exactKeys(profile.real_caps, ["isolated", "service_agent", "journey.route", "journey.diagnose", "journey.publish-restart"], "CONFIG_RUNTIME_CAPS_FIELDS", `${profileId}.real_caps`);
    for (const [capId, cap] of Object.entries(profile.real_caps)) {
      exactKeys(cap, ["max_turns", "max_total_tokens", "max_budget_usd", "hard_timeout_seconds"], "CONFIG_RUNTIME_CAP_FIELDS", `${profileId}.real_caps.${capId}`);
      positiveInteger(cap.max_turns, "CONFIG_RUNTIME_MAX_TURNS", `${capId}.max_turns`);
      positiveInteger(cap.max_total_tokens, "CONFIG_RUNTIME_MAX_TOKENS", `${capId}.max_total_tokens`);
      nonNegativeNumber(cap.max_budget_usd, "CONFIG_RUNTIME_MAX_BUDGET", `${capId}.max_budget_usd`);
      assertFlow(cap.max_budget_usd > 0, "CONFIG_RUNTIME_MAX_BUDGET", `${capId}.max_budget_usd must be positive`);
      positiveInteger(cap.hard_timeout_seconds, "CONFIG_RUNTIME_CAP_TIMEOUT", `${capId}.hard_timeout_seconds`);
    }
    assertFlow(profile.network_policy === "release-offline-pull-never", "CONFIG_RUNTIME_NETWORK_POLICY", `${profileId} has an unsupported network policy`);
  }
}

function crossValidate(config) {
  const stageIds = new Set(config.stages.stages.map((stage) => stage.id));
  const gateIds = new Set(Object.keys(config.gates.gates));
  const identitySetIds = new Set(Object.keys(config.identities.sets));
  const componentIds = new Set(Object.keys(config.identities.components));
  const profileIds = new Set(Object.keys(config.runtimeProfiles.profiles));
  const referencedStages = new Set();
  const referencedGates = new Set();
  const referencedProofs = new Set();
  const referencedComponents = new Set();

  assertFlow(config.policy.process.real_no_progress_seconds < config.policy.process.real_hard_timeout_seconds, "CONFIG_PROCESS_TIMEOUT_ORDER", "No-progress timeout must be below the hard timeout");
  for (const profile of Object.values(config.runtimeProfiles.profiles)) {
    if (profile.kind !== "formal-release") continue;
    for (const [capId, cap] of Object.entries(profile.real_caps)) {
      assertFlow(cap.hard_timeout_seconds <= config.policy.process.real_hard_timeout_seconds, "CONFIG_RUNTIME_CAP_TIMEOUT_POLICY", `${capId} exceeds the process hard-timeout policy`);
    }
  }

  for (const stage of config.stages.stages) {
    assertFlow(identitySetIds.has(stage.identity_set), "CONFIG_STAGE_IDENTITY_UNKNOWN", `${stage.id} references unknown identity set ${stage.identity_set}`);
    if (["capability", "isolated-real", "real-journey"].includes(stage.kind)) {
      assertFlow(stage.timeout_seconds <= config.policy.process.real_hard_timeout_seconds, "CONFIG_REAL_STAGE_TIMEOUT_POLICY", `${stage.id} exceeds the process hard-timeout policy`);
    }
    for (const dependency of stage.depends_on) {
      assertFlow(stageIds.has(dependency), "CONFIG_STAGE_DEPENDENCY_UNKNOWN", `${stage.id} depends on unknown ${dependency}`);
      assertFlow(dependency !== stage.id, "CONFIG_STAGE_SELF_DEPENDENCY", `${stage.id} depends on itself`);
    }
    for (const gateId of stage.gates) {
      assertFlow(gateIds.has(gateId), "CONFIG_STAGE_GATE_UNKNOWN", `${stage.id} references unknown gate ${gateId}`);
      referencedGates.add(gateId);
    }
    if (stage.checkpoint?.next_stage) {
      assertFlow(stageIds.has(stage.checkpoint.next_stage), "CONFIG_CHECKPOINT_STAGE_UNKNOWN", `${stage.id} checkpoint references unknown stage`);
      const next = config.stages.stages.find((candidate) => candidate.id === stage.checkpoint.next_stage);
      assertFlow(next.depends_on.includes(stage.id) || (stage.id === "journey.cross-job.review" && next.depends_on.includes(stage.id)), "CONFIG_CHECKPOINT_ORDER", `${stage.id} checkpoint next_stage is not its DAG successor`);
    }
  }
  topologicalStages(config.stages.stages, [...stageIds]);

  for (const [proofId, proof] of Object.entries(config.proofs.proofs)) {
    for (const stageId of proof.stages) {
      assertFlow(stageIds.has(stageId), "CONFIG_PROOF_STAGE_UNKNOWN", `${proofId} references unknown stage ${stageId}`);
      referencedStages.add(stageId);
    }
  }
  for (const [goalId, goal] of Object.entries(config.proofs.goals)) {
    for (const proofId of [...goal.required_proofs, ...goal.selectable_proofs]) {
      assertFlow(Object.hasOwn(config.proofs.proofs, proofId), "CONFIG_GOAL_PROOF_UNKNOWN", `${goalId} references unknown proof ${proofId}`);
      referencedProofs.add(proofId);
    }
    for (const track of goal.tracks) assertFlow(Object.hasOwn(config.policy.tracks, track), "CONFIG_GOAL_TRACK_UNKNOWN", `${goalId} references unknown track ${track}`);
  }
  assertFlow([...stageIds].every((id) => referencedStages.has(id)), "CONFIG_ORPHAN_STAGE", `Orphan stage(s): ${[...stageIds].filter((id) => !referencedStages.has(id)).join(", ")}`);
  assertFlow([...gateIds].every((id) => referencedGates.has(id)), "CONFIG_ORPHAN_GATE", `Orphan gate(s): ${[...gateIds].filter((id) => !referencedGates.has(id)).join(", ")}`);
  assertFlow(Object.keys(config.proofs.proofs).every((id) => referencedProofs.has(id)), "CONFIG_ORPHAN_PROOF", `Orphan proof(s): ${Object.keys(config.proofs.proofs).filter((id) => !referencedProofs.has(id)).join(", ")}`);

  for (const [setId, set] of Object.entries(config.identities.sets)) {
    for (const componentId of [...set.producer, ...set.proof]) {
      assertFlow(componentIds.has(componentId), "CONFIG_IDENTITY_COMPONENT_UNKNOWN", `${setId} references unknown component ${componentId}`);
      referencedComponents.add(componentId);
    }
  }
  assertFlow([...componentIds].every((id) => referencedComponents.has(id)), "CONFIG_ORPHAN_IDENTITY_COMPONENT", `Orphan identity component(s): ${[...componentIds].filter((id) => !referencedComponents.has(id)).join(", ")}`);

  for (const [gateId, gate] of Object.entries(config.gates.gates)) {
    if (gate.runtime_profile) assertFlow(profileIds.has(gate.runtime_profile), "CONFIG_GATE_RUNTIME_UNKNOWN", `${gateId} references unknown runtime profile ${gate.runtime_profile}`);
  }
  assertFlow(profileIds.has(config.policy.defaults.runtime_profile), "CONFIG_DEFAULT_RUNTIME_UNKNOWN", "Default runtime profile is unknown");
  for (const track of Object.values(config.policy.tracks)) assertFlow(Object.hasOwn(config.proofs.goals, track.default_goal), "CONFIG_TRACK_GOAL_UNKNOWN", `Unknown default goal ${track.default_goal}`);

  const release = config.proofs.goals["release.full"];
  assertFlow(release && release.tracks.length === 1 && release.tracks[0] === "release", "CONFIG_RELEASE_GOAL", "release.full must be Release-only");
  const releaseStages = new Set(release.required_proofs.flatMap((proofId) => config.proofs.proofs[proofId].stages));
  const journey = config.stages.stages.filter((stage) => stage.id.startsWith("journey.cross-job."));
  assertFlow(journey.every((stage) => releaseStages.has(stage.id)), "CONFIG_RELEASE_JOURNEY_CLOSURE", "release.full does not close every CrossJob stage");
  assertFlow(![...releaseStages].some((stageId) => config.stages.stages.find((stage) => stage.id === stageId)?.kind === "isolated-real"), "CONFIG_RELEASE_DUPLICATE_REAL", "release.full must not duplicate isolated real-model gates");
  assertFlow(journey.every((stage) => stage.platforms.length === 3 && [...PLATFORMS].every((platform) => stage.platforms.includes(platform))), "CONFIG_PLATFORM_CLOSURE", "Every CrossJob stage must define Windows/macOS/Linux applicability");
}

export function loadConfiguration(repoRoot, configRoot = path.join(repoRoot, "tools", "test-flow", "config")) {
  const files = {
    proofs: path.join(configRoot, "proofs.v2.json"),
    stages: path.join(configRoot, "stages.v2.json"),
    gates: path.join(configRoot, "gates.v2.json"),
    identities: path.join(configRoot, "identities.v2.json"),
    policy: path.join(configRoot, "policy.v2.json"),
    runtimeProfiles: path.join(configRoot, "runtime-profiles.v2.json"),
  };
  for (const [label, filePath] of Object.entries(files)) assertFlow(fs.existsSync(filePath), "CONFIG_FILE_MISSING", `Missing ${label} config: ${filePath}`);
  const config = {
    proofs: readJson(files.proofs),
    stages: readJson(files.stages),
    gates: readJson(files.gates),
    identities: readJson(files.identities),
    policy: readJson(files.policy),
    runtimeProfiles: readJson(files.runtimeProfiles),
    files,
  };
  validateProofs(config.proofs);
  validateStages(config.stages);
  validateGates(config.gates);
  validateIdentities(config.identities);
  validatePolicy(config.policy);
  validateRuntimeProfiles(config.runtimeProfiles);
  crossValidate(config);
  config.digests = Object.fromEntries(Object.entries(files).map(([name, filePath]) => [name, sha256File(filePath)]));
  config.bundle_digest = sha256Bytes(canonicalJson(config.digests));
  return config;
}

export function topologicalStages(stages, selectedIds) {
  const byId = new Map(stages.map((stage) => [stage.id, stage]));
  const selected = new Set(selectedIds);
  const visiting = new Set();
  const visited = new Set();
  const ordered = [];
  function visit(stageId) {
    if (visited.has(stageId)) return;
    assertFlow(!visiting.has(stageId), "CONFIG_STAGE_CYCLE", `Stage cycle at ${stageId}`);
    const stage = byId.get(stageId);
    assertFlow(stage, "CONFIG_STAGE_UNKNOWN", `Unknown stage ${stageId}`);
    visiting.add(stageId);
    for (const dependency of stage.depends_on) {
      selected.add(dependency);
      visit(dependency);
    }
    visiting.delete(stageId);
    visited.add(stageId);
    ordered.push(stage);
  }
  for (const stageId of [...selected]) visit(stageId);
  return ordered;
}

export function resolveGoalClosure(config, { goalId, track, requestedStage = null, client }) {
  const goal = config.proofs.goals[goalId];
  assertFlow(goal, "GOAL_UNKNOWN", `Unknown proof goal ${goalId}`);
  assertFlow(goal.tracks.includes(track), "GOAL_TRACK_MISMATCH", `${goalId} is not valid for ${track}`);
  const proofIds = [...goal.required_proofs];
  if (goal.selectable_proofs.length > 0) {
    assertFlow(requestedStage, "REAL_STAGE_REQUIRED", `${goalId} requires --stage`);
    const matches = goal.selectable_proofs.filter((proofId) => config.proofs.proofs[proofId].stages.includes(requestedStage));
    assertFlow(matches.length === 1, "REAL_STAGE_INVALID", `${requestedStage} is not an unambiguous selectable stage for ${goalId}`);
    proofIds.push(matches[0]);
  } else {
    assertFlow(!requestedStage, "STAGE_NOT_ALLOWED", `${goalId} does not accept --stage`);
  }
  const selectedStageIds = new Set(proofIds.flatMap((proofId) => config.proofs.proofs[proofId].stages));
  const stages = topologicalStages(config.stages.stages, [...selectedStageIds]);
  for (const stage of stages) assertFlow(stage.platforms.includes(client), "STAGE_PLATFORM_UNSUPPORTED", `${stage.id} does not support ${client}`);
  return {
    goal,
    proofIds,
    proofs: proofIds.map((proofId) => ({ id: proofId, ...config.proofs.proofs[proofId] })),
    stages,
  };
}

export function canonicalStageDefinition(config, stage) {
  return {
    stage,
    gates: stage.gates.map((gateId) => ({ id: gateId, ...config.gates.gates[gateId] })),
    policy: {
      process: config.policy.process,
      evidence: config.policy.evidence,
      performance: config.policy.performance,
    },
  };
}
