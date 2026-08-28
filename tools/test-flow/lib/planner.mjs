import fs from "node:fs";
import path from "node:path";
import { canonicalJson, commandExists, runSync, sha256Bytes } from "./util.mjs";
import { canonicalStageDefinition, loadConfiguration, realCapIdForStage, resolveGoalClosure } from "./config.mjs";
import {
  changedFiles,
  computeIdentitySets,
  gitState,
  performanceIdentity,
  resolveChangeBaseline,
  stageIdentity,
} from "./identity.mjs";
import { findReusableStages, lastSuccessfulDevBaseCommit, loadHistory } from "./history.mjs";
import {
  RELEASE_DOCKER_CONTEXT,
  RELEASE_CHROME_HEADLESS_SHELL_PRODUCT,
  claudeSettingsIdentity,
  codexLogparseRuntimeIdentity,
  dockerServerIdentity,
  externalGitIdentity,
  releaseCachePaths,
  validateClaudeDistribution,
  validateReleaseImage,
  validatePortableReleaseServerImage,
  validateUvCache,
} from "./release-inputs.mjs";
import { captureSourceSnapshot, publicSourceSnapshot } from "./source-snapshot.mjs";
import { chromeIdentity } from "./browser.mjs";
import {
  CODEX_LUNA_EQUIVALENT_USD_LIMIT,
  CODEX_LUNA_MODEL,
  CODEX_LUNA_NORMAL_CALLS,
  CODEX_LUNA_POSTHOC_EXCEPTION_ID,
  CODEX_LUNA_REASONING_EFFORT,
  CODEX_LUNA_TOKEN_LIMIT,
  validateCodexLunaIdentity,
} from "../runtime-support/codex-luna-contract.mjs";
import {
  buildMethodsProducerIdentity,
  MACOS_CODEX_LUNA_E2E_CALLS,
  MACOS_CODEX_LUNA_E2E_TOKEN_LIMIT,
  MACOS_CODEX_LUNA_E2E_USD_LIMIT,
  MACOS_CODEX_LUNA_CALL_WALL_SECONDS,
  MACOS_CODEX_LUNA_METHODS_CALLS,
  MACOS_CODEX_LUNA_METHODS_TOKEN_LIMIT,
  MACOS_CODEX_LUNA_METHODS_USD_LIMIT,
  MACOS_CODEX_LUNA_NO_PROGRESS_SECONDS,
  MACOS_CODEX_LUNA_SCENARIOS,
  methodsCachePath,
  validateMethodsCache,
} from "../quick-validation/codex-luna/runtime/macos-codex-luna-e2e-contract.mjs";
import {
  CLAUDE_DEEPSEEK_CALL_WALL_SECONDS,
  CLAUDE_DEEPSEEK_METHODS_CALLS,
  CLAUDE_DEEPSEEK_MODEL,
  CLAUDE_DEEPSEEK_MODULE,
  CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
  CLAUDE_DEEPSEEK_SCENARIOS,
  claudeDeepseekE2EPhases,
  buildRegistrationProducerIdentity as buildClaudeDeepseekProducerIdentity,
  registrationCachePath as claudeDeepseekCachePath,
  validateClaudeDeepseekIdentity,
  validateRegistrationCache as validateClaudeDeepseekCache,
} from "../quick-validation/claude-deepseek/runtime/claude-deepseek-contract.mjs";

const BUILT_IN_ADAPTERS = Object.freeze({
  macos: "macos-linux-release.mjs",
  windows: "windows-linux-release.mjs",
  linux: "linux-linux-release.mjs",
});
const DARWIN_LINUX_CONTAINER_ADAPTER = "macos-linux-linux-release.mjs";

export function resolveClient(requested, platform = process.platform) {
  if (requested && requested !== "auto") return requested;
  if (platform === "win32") return "windows";
  if (platform === "darwin") return "macos";
  return null;
}

export function builtInAdapter(repoRoot, client, platform = process.platform) {
  const name = platform === "darwin" && client === "linux"
    ? DARWIN_LINUX_CONTAINER_ADAPTER
    : BUILT_IN_ADAPTERS[client];
  return name ? path.join(repoRoot, "tools", "test-flow", "adapters", name) : null;
}

export function supportedHostClientTopology(client, platform = process.platform) {
  return (platform === "darwin" && ["macos", "linux"].includes(client))
    || (platform === "win32" && client === "windows")
    || (platform === "linux" && client === "linux");
}

export function supportedCodexLunaOrchestrator(platform = process.platform, architecture = process.arch) {
  return platform === "darwin" && architecture === "arm64";
}

export function supportedQuickValidationOrchestrator(
  platform = process.platform,
  architecture = process.arch,
  environment = process.env,
) {
  return supportedCodexLunaOrchestrator(platform, architecture)
    || (platform === "linux"
      && architecture === "x64"
      && environment.TEST_FLOW_QUICK_UBUNTU2204_CONTAINER === "1");
}

export function releaseImageValidationMode(platform = process.platform, formalRuntime = true) {
  if (!formalRuntime) return "not-required";
  return platform === "darwin" ? "sealed-darwin-cache" : "portable-exact-server-image";
}

export function retryRequirement(history, stageIdentities) {
  const unresolvedStageIds = new Set(Object.keys(stageIdentities));
  for (const entry of [...history].reverse()) {
    for (const stage of entry.verdict.stages ?? []) {
      if (!unresolvedStageIds.has(stage.id)) continue;
      const desired = stageIdentities[stage.id];
      if (desired.producer_identity !== stage.producer_identity || desired.proof_identity !== stage.proof_identity) continue;
      if (stage.status === "NOT_RUN" && stage.code === "PRIOR_STAGE_NOT_PASSING") continue;
      unresolvedStageIds.delete(stage.id);
      if (["PASS", "NOT_REQUIRED"].includes(stage.status)) continue;
      return {
        recommendation: "STOP",
        reason: "UNCHANGED_FAILED_IDENTITY",
        previous_run_id: entry.verdict.run_id,
        stage_id: stage.id,
        previous_code: stage.code ?? null,
      };
    }
    if (unresolvedStageIds.size === 0) break;
  }
  return { recommendation: "RUN", reason: null, previous_run_id: null, stage_id: null, previous_code: null };
}

function formalRuntimeRequired(stages) {
  return stages.some((stage) => ["capability", "isolated-real", "real-journey"].includes(stage.kind));
}

function claudeRuntimeRequired(stages) {
  return stages.some((stage) => (
    stage.kind === "isolated-real"
    || stage.kind === "real-journey"
    || claudeDeepseekStage(stage)
    || ["platform.host-capability", "platform.server-linux-capability"].includes(stage.id)
  ));
}

function serverRuntimeRequired(stages) {
  return stages.some((stage) => stage.id === "platform.server-linux-capability" || stage.kind === "real-journey");
}

function logparseRuntimeRequired(stages) {
  return stages.some((stage) => (
    stage.id === "real.logparse"
    || stage.id === "platform.server-linux-capability"
    || stage.id === "real.codex-luna-methods"
    || stage.id === "real.macos-codex-luna-e2e"
    || stage.id === "real.macos-claude-deepseek-e2e"
    || stage.kind === "real-journey"
  ));
}

function mcpRuntimeRequired(stages) {
  return stages.some((stage) => stage.id === "platform.server-linux-capability" || stage.kind === "real-journey");
}

function codexLogparseRuntimeRequired(stages) {
  return stages.some((stage) => ["real.codex-luna-methods", "real.macos-codex-luna-e2e"].includes(stage.id));
}

function macosCodexStage(stage) {
  return ["real.macos-codex-luna-methods", "real.macos-codex-luna-e2e"].includes(stage.id);
}

function claudeDeepseekStage(stage) {
  return ["real.macos-claude-deepseek-methods", "real.macos-claude-deepseek-e2e"].includes(stage.id);
}

function selectedClientRuntimeIdentity({ clientDistribution, imageIdentity, dualLinuxContainers, hostPlatform }) {
  const claude = {
    status: clientDistribution.status,
    version: clientDistribution.version ?? null,
    cli_sha256: clientDistribution.cli_sha256 ?? null,
    package_name: clientDistribution.package_name ?? null,
    package_version: clientDistribution.package_version ?? null,
    package_manifest_sha256: clientDistribution.package_manifest_sha256 ?? null,
    package_tree_digest: clientDistribution.package_tree_digest ?? null,
    tarball_sha256: clientDistribution.tarball_sha256 ?? null,
  };
  if (dualLinuxContainers) {
    return {
      status: clientDistribution.status === "PRESENT" && imageIdentity?.client?.image_id ? "PRESENT" : "INVALID",
      execution_topology: "darwin-orchestrated-linux-container",
      platform: "linux/amd64",
      image_id: imageIdentity?.client?.image_id ?? null,
      node_identity_boundary: "client-image-id",
      node: { version: null, architecture: "x64", executable: null, sha256: null, observation: "runtime-container-probe-required" },
      claude: { version: clientDistribution.version ?? null, cli_sha256: clientDistribution.cli_sha256 ?? null },
    };
  }
  const platform = hostPlatform === "darwin" ? "macos" : hostPlatform === "win32" ? "windows" : "linux";
  return {
    status: clientDistribution.status,
    execution_topology: "native-host",
    platform,
    image_id: null,
    node_identity_boundary: "node-binary-sha256",
    node: clientDistribution.node ?? null,
    claude,
  };
}

function releaseRuntimeIdentity({ profile, clientDistribution, selectedClientRuntime, settingsIdentity, dockerIdentity, uvIdentity, imageIdentity }) {
  return {
    schema_version: 5,
    profile: {
      claude: profile.claude,
      uv: profile.uv,
      python: profile.python,
      hatchling: profile.hatchling,
      base_image: profile.base_image,
      external_sources: profile.external_sources,
      real_caps: profile.real_caps,
      network_policy: profile.network_policy,
    },
    observed: {
      orchestrator_distribution_source: clientDistribution,
      selected_client_runtime: selectedClientRuntime,
      settings: settingsIdentity,
      docker: dockerIdentity,
      uv_cache: uvIdentity,
      image: imageIdentity,
    },
  };
}

function codexRuntimeIdentity({ profile, codexIdentity }) {
  return {
    schema_version: 1,
    profile: profile.codex,
    observed: codexIdentity,
  };
}

function filterReusableChain(selected, reusable, track) {
  const byId = new Map(selected.map((stage) => [stage.id, stage]));
  for (const stage of selected) {
    if (stage.reuse[track] === "never") reusable.delete(stage.id);
    if (stage.reuse[track] !== "checkpoint-chain") continue;
    const candidate = reusable.get(stage.id);
    if (!candidate) continue;
    if (candidate.stage.result_source !== "EXECUTED") {
      reusable.delete(stage.id);
      continue;
    }
    if (stage.checkpoint && (!candidate.stage.checkpoint?.checkpoint_id || !candidate.stage.checkpoint?.parent_checkpoint_id || !candidate.stage.checkpoint?.path)) {
      reusable.delete(stage.id);
    }
  }
  const diagnose = reusable.get("journey.cross-job.diagnose");
  const review = reusable.get("journey.cross-job.review");
  if (byId.has("journey.cross-job.diagnose") && byId.has("journey.cross-job.review") && (!diagnose || !review || diagnose.run_id !== review.run_id)) {
    reusable.delete("journey.cross-job.diagnose");
    reusable.delete("journey.cross-job.review");
  }
  let parent = "GENESIS";
  let broken = false;
  for (const stageId of ["journey.cross-job.route", "journey.cross-job.upload", "journey.cross-job.review", "journey.cross-job.publish-restart"]) {
    if (!byId.has(stageId)) continue;
    const candidate = reusable.get(stageId);
    const checkpoint = candidate?.stage?.checkpoint;
    if (broken || !candidate || checkpoint.parent_checkpoint_id !== parent) {
      reusable.delete(stageId);
      if (stageId === "journey.cross-job.review") reusable.delete("journey.cross-job.diagnose");
      broken = true;
      continue;
    }
    parent = checkpoint.checkpoint_id;
  }
}

function capForStage(stage, profile) {
  if (stage.id === "real.codex-luna-methods") return profile.real_caps["codex.methods"];
  if (stage.id === "real.macos-codex-luna-methods") return profile.real_caps["codex.macos-methods"];
  if (stage.id === "real.macos-codex-luna-e2e") return profile.real_caps["codex.macos-e2e"];
  if (stage.id === "real.macos-claude-deepseek-methods") return profile.real_caps["claude.macos-methods"];
  if (stage.id === "real.macos-claude-deepseek-e2e") return profile.real_caps["claude.macos-e2e"];
  const isolatedCapId = realCapIdForStage(stage);
  if (isolatedCapId) return profile.real_caps[isolatedCapId];
  if (stage.id === "journey.cross-job.route") return profile.real_caps["journey.route"];
  if (stage.id === "journey.cross-job.diagnose") return profile.real_caps["journey.diagnose"];
  if (stage.id === "journey.cross-job.publish-restart") return profile.real_caps["journey.publish-restart"];
  return null;
}

function invocationCapsForStage(stage, profile, gates, {
  clientInvocationClass = "host-client",
  clientExecutionTopology = "native-host-client",
  scenarioId = null,
} = {}) {
  const cap = capForStage(stage, profile);
  if (stage.id === "real.logparse") return [];
  if (stage.id === "real.codex-luna-methods") return [{
    class: "codex-luna-agent",
    min_count: CODEX_LUNA_NORMAL_CALLS,
    max_count: CODEX_LUNA_NORMAL_CALLS,
    aggregate: true,
    enforcement: "posthoc-terminal-aggregate",
    model: CODEX_LUNA_MODEL,
    reasoning_effort: CODEX_LUNA_REASONING_EFFORT,
    caps: cap,
  }];
  if (stage.id === "real.macos-codex-luna-methods") return [{
    class: "codex-luna-methods-bootstrap",
    min_count: MACOS_CODEX_LUNA_METHODS_CALLS,
    max_count: MACOS_CODEX_LUNA_METHODS_CALLS,
    aggregate: true,
    enforcement: "posthoc-terminal-aggregate",
    model: CODEX_LUNA_MODEL,
    reasoning_effort: CODEX_LUNA_REASONING_EFFORT,
    per_call_hard_timeout_seconds: MACOS_CODEX_LUNA_CALL_WALL_SECONDS,
    caps: cap,
  }];
  if (stage.id === "real.macos-codex-luna-e2e") return [{
    class: "codex-luna-macos-e2e",
    phases: ["CLIENT", "ROUTE", "LOGPARSE", "DIAGNOSE", "REVIEW"],
    min_count: MACOS_CODEX_LUNA_E2E_CALLS,
    max_count: MACOS_CODEX_LUNA_E2E_CALLS,
    aggregate: true,
    enforcement: "posthoc-terminal-aggregate",
    model: CODEX_LUNA_MODEL,
    reasoning_effort: CODEX_LUNA_REASONING_EFFORT,
    per_call_hard_timeout_seconds: MACOS_CODEX_LUNA_CALL_WALL_SECONDS,
    caps: cap,
  }];
  if (stage.id === "real.macos-claude-deepseek-methods") return [{
    class: "claude-deepseek-registration-generation",
    phases: ["REGISTRATION_GENERATION"],
    min_count: CLAUDE_DEEPSEEK_METHODS_CALLS,
    max_count: CLAUDE_DEEPSEEK_METHODS_CALLS,
    aggregate: true,
    enforcement: "claude-cli-hard-caps-plus-terminal-aggregate",
    model: CLAUDE_DEEPSEEK_MODEL,
    per_call_hard_timeout_seconds: 1800,
    caps: cap,
  }];
  if (stage.id === "real.macos-claude-deepseek-e2e") {
    const phases = claudeDeepseekE2EPhases(scenarioId ?? CLAUDE_DEEPSEEK_SCENARIOS[0]);
    return [{
    class: "claude-deepseek-macos-e2e",
    phases,
    min_count: phases.length,
    max_count: phases.length,
    aggregate: true,
    enforcement: "claude-cli-hard-caps-plus-terminal-aggregate",
    model: CLAUDE_DEEPSEEK_MODEL,
    per_call_hard_timeout_seconds: CLAUDE_DEEPSEEK_CALL_WALL_SECONDS,
    caps: cap,
  }];
  }
  if (stage.kind === "isolated-real") {
    const count = stage.gates.reduce((sum, gateId) => sum + gates[gateId].isolated_agent_invocations, 0);
    return [{ class: "isolated-agent", min_count: count, max_count: count, caps: cap }];
  }
  if (stage.id === "journey.cross-job.route") return [
    { class: clientInvocationClass, execution_topology: clientExecutionTopology, min_count: 1, max_count: 1, caps: cap },
    { class: "server-agent", min_count: 1, max_count: 1, caps: profile.real_caps.service_agent },
  ];
  if (stage.id === "journey.cross-job.diagnose") return [
    { class: clientInvocationClass, execution_topology: clientExecutionTopology, min_count: 1, max_count: 1, caps: cap },
    { class: "server-agent", min_count: 3, max_count: 3, caps: profile.real_caps.service_agent },
  ];
  if (stage.id === "journey.cross-job.publish-restart") return [{ class: clientInvocationClass, execution_topology: clientExecutionTopology, min_count: 1, max_count: 1, caps: cap }];
  return [];
}

function noProgressForStage(stage, gates, seconds) {
  const definitions = stage.gates.map((gateId) => gates[gateId]);
  const enabled = definitions.filter((gate) => (claudeDeepseekStage(stage) && gate.kind === "node-test") || gate.kind === "cross-job-adapter" || (gate.kind === "capability-adapter" && ["server-linux-capability", "codex-luna-methods", "macos-codex-luna-methods", "macos-codex-luna-e2e", "macos-claude-deepseek-methods", "macos-claude-deepseek-e2e"].includes(gate.adapter)));
  if (enabled.length === 0) return null;
  if (enabled.length !== definitions.length) throw new Error("MIXED_PROGRESS_POLICY_UNSUPPORTED");
  if (macosCodexStage(stage)) return MACOS_CODEX_LUNA_NO_PROGRESS_SECONDS;
  if (claudeDeepseekStage(stage)) return CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS;
  return seconds;
}

export function buildRunPlan(repoRoot, options = {}) {
  const config = loadConfiguration(repoRoot);
  const defaults = config.policy.defaults;
  const track = options.track ?? defaults.track;
  const trackConfig = config.policy.tracks[track];
  if (!trackConfig) throw new Error(`TRACK_UNKNOWN:${track}`);
  const goal = options.goal ?? trackConfig.default_goal;
  const client = resolveClient(options.client);
  const hostPlatform = process.platform;
  const planningClient = client ?? "linux";
  const closure = resolveGoalClosure(config, { goalId: goal, track, requestedStage: options.stage ?? null, client: planningClient });
  const crossJobSelected = closure.stages.some((stage) => stage.kind === "real-journey" || stage.id === "journey.cross-job.review");
  const codexRequired = closure.stages.some((stage) => stage.id === "real.codex-luna-methods" || macosCodexStage(stage));
  const claudeDeepseekSelected = closure.stages.some(claudeDeepseekStage);
  const quickValidationSelected = closure.stages.some((stage) => macosCodexStage(stage) || claudeDeepseekStage(stage));
  const quickValidationOrchestrator = quickValidationSelected
    && supportedQuickValidationOrchestrator(hostPlatform, process.arch, process.env);
  const dualLinuxContainers = crossJobSelected && hostPlatform === "darwin" && client === "linux";
  const effectiveAdapter = crossJobSelected ? builtInAdapter(repoRoot, client, hostPlatform) : null;
  const effectiveOptions = { ...options, crossJobAdapter: effectiveAdapter };
  const runtimeProfileId = options.runtimeProfile ?? defaults.runtime_profile;
  const runtimeProfile = config.runtimeProfiles.profiles[runtimeProfileId];
  if (!runtimeProfile) throw new Error(`RUNTIME_PROFILE_UNKNOWN:${runtimeProfileId}`);
  const formalRuntime = formalRuntimeRequired(closure.stages);
  const claudeRequired = claudeRuntimeRequired(closure.stages);
  const serverRequired = serverRuntimeRequired(closure.stages);
  const logparseRequired = logparseRuntimeRequired(closure.stages);
  const codexLogparseRequired = codexLogparseRuntimeRequired(closure.stages);
  const mcpRequired = mcpRuntimeRequired(closure.stages);
  const cachePaths = releaseCachePaths(repoRoot, effectiveOptions.cacheRoot);
  const clientDistribution = claudeRequired
    ? validateClaudeDistribution(effectiveOptions.claudeEntry)
    : { status: effectiveOptions.claudeEntry ? validateClaudeDistribution(effectiveOptions.claudeEntry).status : "NOT_REQUIRED" };
  const settingsIdentity = claudeRequired
    ? claudeSettingsIdentity(effectiveOptions.claudeSettings)
    : { status: effectiveOptions.claudeSettings ? claudeSettingsIdentity(effectiveOptions.claudeSettings).status : "NOT_REQUIRED" };
  let codexIdentity = { status: "NOT_REQUIRED" };
  if (codexRequired) {
    try {
      codexIdentity = validateCodexLunaIdentity(effectiveOptions.codexEntry, effectiveOptions.codexAuth);
    } catch (error) {
      codexIdentity = { status: "INVALID", code: error?.code ?? "CODEX_LUNA_IDENTITY_INVALID" };
    }
  }
  const dockerIdentity = serverRequired
    ? dockerServerIdentity(effectiveOptions.dockerContext ?? "default")
    : { status: "NOT_REQUIRED", context: effectiveOptions.dockerContext ?? null };
  const uvIdentity = hostPlatform === "darwin" && serverRequired ? validateUvCache(cachePaths) : { status: "NOT_REQUIRED" };
  const imageValidationMode = releaseImageValidationMode(hostPlatform, serverRequired);
  const imageIdentity = imageValidationMode !== "not-required"
    ? imageValidationMode === "sealed-darwin-cache"
      ? validateReleaseImage(cachePaths, dockerIdentity, { requireClientImage: dualLinuxContainers })
      : validatePortableReleaseServerImage(cachePaths, dockerIdentity)
    : { status: "NOT_REQUIRED", image: cachePaths.baseImage };
  const selectedClientRuntime = selectedClientRuntimeIdentity({
    clientDistribution,
    imageIdentity,
    dualLinuxContainers,
    hostPlatform,
  });
  const browserIdentity = !crossJobSelected
    ? { status: "NOT_REQUIRED", product: "Google Chrome", version: null, executable_sha256: null, code: null }
    : dualLinuxContainers
      ? imageIdentity.browser ?? { status: "INVALID", product: RELEASE_CHROME_HEADLESS_SHELL_PRODUCT, version: null, executable_sha256: null, code: imageIdentity.code ?? "CLIENT_IMAGE_BROWSER_INVALID" }
      : chromeIdentity();
  const logparseIdentity = logparseRequired
    ? externalGitIdentity(effectiveOptions.logparseSource, runtimeProfile.external_sources.logparse)
    : { status: "NOT_REQUIRED", root: effectiveOptions.logparseSource ?? null };
  const codexLogparseRuntime = codexLogparseRequired
    ? codexLogparseRuntimeIdentity(effectiveOptions.logparseSource)
    : { schema_version: 1, status: "NOT_REQUIRED", root: effectiveOptions.logparseSource ?? null };
  const mcpIdentity = mcpRequired
    ? externalGitIdentity(effectiveOptions.mcpSource, runtimeProfile.external_sources.mcp)
    : { status: "NOT_REQUIRED", root: effectiveOptions.mcpSource ?? null };
  const releaseRuntime = releaseRuntimeIdentity({ profile: runtimeProfile, clientDistribution, selectedClientRuntime, settingsIdentity, dockerIdentity, uvIdentity, imageIdentity });
  const codexRuntime = codexRuntimeIdentity({ profile: runtimeProfile, codexIdentity });

  const evidenceRoot = path.resolve(repoRoot, options.evidenceRoot ?? defaults.evidence_root);
  const history = loadHistory(evidenceRoot, {
    config,
    knownSecrets: [process.env.ANTHROPIC_AUTH_TOKEN, process.env.ANTHROPIC_API_KEY, process.env.PROBLEM_LOCATOR_LOGPARSE_TOKEN].filter(Boolean),
  });
  const source = gitState(repoRoot);
  let sourceSnapshot;
  try {
    sourceSnapshot = captureSourceSnapshot(repoRoot);
  } catch (error) {
    sourceSnapshot = { schema_version: 1, algorithm: "git-visible-worktree-v1", digest: null, file_count: 0, records: [], code: error?.code ?? "SOURCE_SNAPSHOT_UNAVAILABLE" };
  }
  const baseline = options.base
    ? { source: "explicit", commit: options.base }
    : resolveChangeBaseline(repoRoot, lastSuccessfulDevBaseCommit(history));
  const files = changedFiles(repoRoot, baseline, source);
  const identities = computeIdentitySets({
    repoRoot,
    identityConfig: config.identities,
    externalTrees: { logparse: effectiveOptions.logparseSource, mcp: effectiveOptions.mcpSource },
    claudeEntry: effectiveOptions.claudeEntry,
    claudeSettings: effectiveOptions.claudeSettings,
    releaseRuntime,
    codexRuntime,
    codexLogparseRuntime,
  });

  const selectedScenario = closure.stages.some((stage) => ["real.macos-codex-luna-e2e", "real.macos-claude-deepseek-e2e"].includes(stage.id))
    ? options.scenario ?? (claudeDeepseekSelected ? CLAUDE_DEEPSEEK_SCENARIOS[0] : MACOS_CODEX_LUNA_SCENARIOS[0])
    : null;
  const methodsBootstrapSelected = closure.stages.some((stage) => stage.id === "real.macos-codex-luna-methods");
  const macosE2ESelected = closure.stages.some((stage) => stage.id === "real.macos-codex-luna-e2e");
  let methodsCache = { status: "NOT_REQUIRED", package_tree_sha256: null, producer_identity: null, cache_path: null, code: null };
  if ((methodsBootstrapSelected || macosE2ESelected) && codexIdentity.status === "PASS") {
    let producer = null;
    try {
      const releaseCaseRoot = path.join(repoRoot, "tests", "cases", "release", "rpc-timeout-anonymized");
      const registrationTemplate = path.join(releaseCaseRoot, "registration", "rpc-timeout-methods-v1", "registration-template.json");
      producer = buildMethodsProducerIdentity({
        wiki: path.join(releaseCaseRoot, "input", "wiki.md"),
        metaSkillRoot: path.join(repoRoot, ".agents", "skills", "wiki-to-diagnosis-skill"),
        registrationTemplate,
        codexIdentity,
      });
      const cachePath = methodsCachePath(cachePaths.cacheRoot, producer.producer_identity);
      const receipt = validateMethodsCache({ cacheRoot: cachePaths.cacheRoot, producer, registrationTemplate });
      methodsCache = { status: "PRESENT", package_tree_sha256: receipt.manifest.package.tree_sha256, producer_identity: producer.producer_identity, cache_path: cachePath, code: null };
    } catch (error) {
      const code = error?.code ?? "MACOS_CODEX_LUNA_METHODS_CACHE_INVALID";
      const cachePath = producer ? methodsCachePath(cachePaths.cacheRoot, producer.producer_identity) : null;
      methodsCache = {
        status: code === "MACOS_CODEX_LUNA_FILE_MISSING" && cachePath !== null && !fs.existsSync(cachePath) ? "MISSING" : "INVALID",
        package_tree_sha256: null,
        producer_identity: producer?.producer_identity ?? null,
        cache_path: cachePath,
        code,
      };
    }
  }
  const claudeMethodsSelected = closure.stages.some((stage) => stage.id === "real.macos-claude-deepseek-methods");
  const claudeE2ESelected = closure.stages.some((stage) => stage.id === "real.macos-claude-deepseek-e2e");
  let claudeMethodsCache = { status: "NOT_REQUIRED", registration_tree_sha256: null, runtime_ref: null, producer_identity: null, cache_path: null, code: null };
  if ((claudeMethodsSelected || claudeE2ESelected) && clientDistribution.status === "PRESENT" && settingsIdentity.status === "PRESENT") {
    let producer = null;
    try {
      const claudeIdentity = validateClaudeDeepseekIdentity(effectiveOptions.claudeEntry, effectiveOptions.claudeSettings);
      const releaseCaseRoot = path.join(repoRoot, "tests", "cases", "release", "rpc-timeout-anonymized");
      producer = buildClaudeDeepseekProducerIdentity({
        wiki: path.join(releaseCaseRoot, "input", "wiki.md"),
        metaSkillRoot: path.join(repoRoot, ".claude", "skills", "wiki-to-logparse-diagnosis-skill"),
        claudeIdentity,
        module: CLAUDE_DEEPSEEK_MODULE,
      });
      const cachePath = claudeDeepseekCachePath(cachePaths.cacheRoot, producer.producer_identity);
      const receipt = validateClaudeDeepseekCache({ cacheRoot: cachePaths.cacheRoot, producer });
      claudeMethodsCache = { status: "PRESENT", registration_tree_sha256: receipt.manifest.registration.tree_sha256, runtime_ref: receipt.manifest.registration.runtime_ref, producer_identity: producer.producer_identity, cache_path: cachePath, code: null };
    } catch (error) {
      const cachePath = producer ? claudeDeepseekCachePath(cachePaths.cacheRoot, producer.producer_identity) : null;
      claudeMethodsCache = { status: cachePath !== null && !fs.existsSync(cachePath) ? "MISSING" : "INVALID", registration_tree_sha256: null, runtime_ref: null, producer_identity: producer?.producer_identity ?? null, cache_path: cachePath, code: error?.code ?? "CLAUDE_DEEPSEEK_REGISTRATION_CACHE_INVALID" };
    }
  }
  const stageIdentities = {};
  const performanceIdentities = {};
  for (const stage of config.stages.stages) {
    const dependencyProofIdentities = stage.depends_on.map((stageId) => stageIdentities[stageId]?.proof_identity).filter(Boolean);
    const journeyDependency = stage.depends_on.find((stageId) => stageId.startsWith("journey.cross-job."));
    const definitionDigest = sha256Bytes(canonicalJson(canonicalStageDefinition(config, stage)));
    const identity = stageIdentity(stage, identities.sets, {
      parent_checkpoint: journeyDependency ? stageIdentities[journeyDependency].producer_identity : "GENESIS",
      scenario: ["real.macos-codex-luna-e2e", "real.macos-claude-deepseek-e2e"].includes(stage.id) ? selectedScenario : stage.id === "real.macos-codex-luna-methods" ? "methods-bootstrap" : stage.id === "real.macos-claude-deepseek-methods" ? "registration-generation" : stage.id.startsWith("journey.cross-job.") ? "CrossJob" : null,
      methods_package_digest: stage.id === "real.macos-codex-luna-e2e" ? methodsCache.package_tree_sha256 ?? "MISSING" : null,
      registration_tree_digest: stage.id === "real.macos-claude-deepseek-e2e" ? claudeMethodsCache.registration_tree_sha256 ?? "MISSING" : null,
      stage_definition_digest: definitionDigest,
      dependency_proof_identities: dependencyProofIdentities,
      config_bundle_digest: config.bundle_digest,
      evidence_contract_version: config.policy.evidence.event_contract_version,
    });
    stageIdentities[stage.id] = identity;
    performanceIdentities[stage.id] = performanceIdentity(stage, identity.producer_identity, config.policy.performance);
  }

  const freshStageIds = new Set(config.stages.stages.filter((stage) => stage.reuse[track] === "never").map((stage) => stage.id));
  const reusable = findReusableStages(history, stageIdentities, {
    track,
    freshStageIds,
    knownSecrets: [process.env.ANTHROPIC_AUTH_TOKEN, process.env.ANTHROPIC_API_KEY, process.env.PROBLEM_LOCATOR_LOGPARSE_TOKEN].filter(Boolean),
    config,
  });
  filterReusableChain(closure.stages, reusable, track);

  const blockers = [];
  const warnings = [];
  for (const stage of closure.stages) {
    if (stage.admission_blocker !== undefined) {
      blockers.push({
        code: stage.admission_blocker.code,
        detail: stage.admission_blocker.detail,
        stage_id: stage.id,
      });
    }
  }
  const containsReal = closure.stages.some((stage) => ["isolated-real", "real-journey"].includes(stage.kind) || stage.id === "real.codex-luna-methods" || macosCodexStage(stage) || claudeDeepseekStage(stage));
  if (!source.available) blockers.push({ code: "GIT_REQUIRED", detail: "A Git worktree is required to enumerate the source snapshot." });
  if (trackConfig.requires_source_snapshot && !sourceSnapshot.digest) blockers.push({ code: "SOURCE_SNAPSHOT_REQUIRED", detail: "The Git-visible worktree could not be frozen into an exact source snapshot." });
  if (track === "release" && !client) blockers.push({ code: "RELEASE_CLIENT_UNRESOLVED", detail: "Linux hosts require an explicit --client linux; Windows/macOS follow the host." });
  if (client && !Object.hasOwn(BUILT_IN_ADAPTERS, client)) blockers.push({ code: "CLIENT_UNKNOWN", detail: `Unsupported client ${client}.` });
  if (client
    && Object.hasOwn(BUILT_IN_ADAPTERS, client)
    && !supportedHostClientTopology(client, hostPlatform)
    && !(quickValidationOrchestrator && client === "macos")) {
    blockers.push({ code: "HOST_CLIENT_TOPOLOGY_UNSUPPORTED", detail: `Host ${hostPlatform} cannot orchestrate first-party Client ${client}.` });
  }
  if (track === "dev" && containsReal && trackConfig.real_requires_opt_in && !options.allowRealModel) blockers.push({ code: "DEV_REAL_OPT_IN_REQUIRED", detail: "Dev real proofs require --allow-real-model." });
  if (track === "dev" && containsReal && trackConfig.real_requires_intent && !options.reason) blockers.push({ code: "DEV_REAL_REASON_REQUIRED", detail: "Dev real proofs require --reason." });
  if (track === "release" && options.resume && !["fresh", "auto"].includes(options.resume)) blockers.push({ code: "RELEASE_RESUME_FORBIDDEN", detail: "Release must start from GENESIS and an empty DATA_ROOT." });
  if (track === "release" && options.resume === "auto") warnings.push({ code: "RELEASE_RESUME_FORCED_FRESH", detail: "Release starts from GENESIS; Release checkpoint reuse is forbidden." });

  if (claudeRequired && !effectiveOptions.claudeEntry) blockers.push({ code: "CLAUDE_ENTRY_REQUIRED", detail: "Claude-based formal proofs require the runtime-profile-bound official npm cli.js." });
  else if (claudeRequired && clientDistribution.status !== "PRESENT") blockers.push({ code: "CLAUDE_DISTRIBUTION_INVALID", detail: `Claude distribution does not match runtime profile ${runtimeProfileId}: ${clientDistribution.code ?? "invalid"}.` });
  if (hostPlatform === "darwin" && claudeRequired && effectiveOptions.claudeEntry && path.resolve(effectiveOptions.claudeEntry) !== path.resolve(cachePaths.claudeEntry)) blockers.push({ code: "CLAUDE_ENTRY_CACHE_MISMATCH", detail: "Darwin-orchestrated Claude proofs must use cli.js from the explicit frozen cache root." });
  if (claudeRequired && !effectiveOptions.claudeSettings) blockers.push({ code: "CLAUDE_SETTINGS_REQUIRED", detail: "Claude-based formal proofs require an env-only Claude settings source." });
  else if (claudeRequired && settingsIdentity.status !== "PRESENT") blockers.push({ code: "CLAUDE_SETTINGS_INVALID", detail: `Claude settings violate the runtime profile: ${settingsIdentity.code ?? "invalid"}.` });
  if (codexRequired && codexIdentity.status !== "PASS") blockers.push({ code: "CODEX_RUNTIME_INVALID", detail: `Codex CLI and ChatGPT authentication must match the frozen Luna contract: ${codexIdentity.code ?? "invalid"}.` });
  if (codexLogparseRequired && codexLogparseRuntime.status !== "PRESENT") blockers.push({ code: "CODEX_LOGPARSE_RUNTIME_INVALID", detail: `Codex preprocessing requires the exact Logparse .venv, Python base runtime and CLI bytes: ${codexLogparseRuntime.code ?? "invalid"}.` });
  if (codexRequired && !(quickValidationSelected
    ? quickValidationOrchestrator
    : supportedCodexLunaOrchestrator(hostPlatform, process.arch))) {
    blockers.push({ code: "CODEX_ORCHESTRATOR_UNSUPPORTED", detail: "The pinned Codex CLI + gpt-5.6-luna exploration flow requires native Darwin arm64 or the sealed Ubuntu 22.04 Quick Validation container." });
  }
  if (codexRequired && client !== "macos") blockers.push({ code: "CODEX_CLIENT_LABEL_INVALID", detail: "The local Codex exploration goal must use --client macos; it does not execute in or validate a Linux Client." });
  if (claudeDeepseekSelected && !quickValidationOrchestrator) blockers.push({ code: "CLAUDE_DEEPSEEK_ORCHESTRATOR_UNSUPPORTED", detail: "Claude/DeepSeek Quick Validation requires native Darwin arm64 or the sealed Ubuntu 22.04 Quick Validation container." });
  if (claudeDeepseekSelected && client !== "macos") blockers.push({ code: "CLAUDE_DEEPSEEK_CLIENT_LABEL_INVALID", detail: "Claude/DeepSeek Quick Validation requires --client macos." });
  if (selectedScenario !== null && !MACOS_CODEX_LUNA_SCENARIOS.includes(selectedScenario)) blockers.push({ code: "MACOS_CODEX_LUNA_SCENARIO_INVALID", detail: `Scenario ${selectedScenario} is not in the repository-owned smoke matrix.` });
  if (methodsBootstrapSelected && methodsCache.status === "INVALID") blockers.push({ code: "MACOS_CODEX_LUNA_METHODS_CACHE_INVALID", detail: `The exact Methods cache path exists but is invalid: ${methodsCache.code}.` });
  if (macosE2ESelected && methodsCache.status !== "PRESENT") blockers.push({ code: "MACOS_CODEX_LUNA_METHODS_CACHE_REQUIRED", detail: `E2E requires an exact frozen Methods cache produced by dev.macos-codex-luna-methods: ${methodsCache.code ?? "missing"}.` });
  if (claudeMethodsSelected && claudeMethodsCache.status === "INVALID") blockers.push({ code: "CLAUDE_DEEPSEEK_REGISTRATION_CACHE_INVALID", detail: `The exact Claude/DeepSeek registration cache path exists but is invalid: ${claudeMethodsCache.code}.` });
  if (claudeE2ESelected && claudeMethodsCache.status !== "PRESENT") blockers.push({ code: "CLAUDE_DEEPSEEK_REGISTRATION_CACHE_REQUIRED", detail: `E2E requires the exact registration cache produced by dev.macos-claude-deepseek-methods: ${claudeMethodsCache.code ?? "missing"}.` });
  if (codexRequired && !effectiveOptions.allowCodexPosthocBudget) {
    blockers.push({ code: "CODEX_POSTHOC_BUDGET_ACK_REQUIRED", detail: `Acknowledge ${CODEX_LUNA_POSTHOC_EXCEPTION_ID}; Codex token and equivalent-USD limits are terminal audits, not spend prevention.` });
  } else if (codexRequired) {
    warnings.push({ code: "CODEX_POSTHOC_BUDGET_EXCEPTION", detail: `${CODEX_LUNA_POSTHOC_EXCEPTION_ID} accepted for this run; aggregate limits remain verdict-blocking terminal postconditions.` });
  }

  if (crossJobSelected && !effectiveAdapter) blockers.push({ code: "CROSS_JOB_ADAPTER_REQUIRED", detail: `No first-party ${client ?? "unresolved"}→Linux CrossJob adapter was selected.` });
  if (crossJobSelected && browserIdentity.status !== "PRESENT") blockers.push({ code: "CHROME_REQUIRED", detail: `CrossJob Web API proof requires Google Chrome: ${browserIdentity.code ?? "invalid"}.` });
  if (effectiveAdapter && (!path.isAbsolute(effectiveAdapter) || !fs.existsSync(effectiveAdapter) || !fs.statSync(effectiveAdapter).isFile())) blockers.push({ code: "CROSS_JOB_ADAPTER_INVALID", detail: "The selected CrossJob adapter is not an existing repository-owned or explicit file." });
  if (crossJobSelected && client && effectiveAdapter !== builtInAdapter(repoRoot, client, hostPlatform)) blockers.push({ code: "BUILTIN_ADAPTER_IDENTITY_INVALID", detail: `The ${client} first-party adapter did not resolve exactly.` });

  if (logparseRequired && logparseIdentity.status !== "PRESENT") blockers.push({ code: "LOGPARSE_SOURCE_INVALID", detail: `Logparse must be clean at ${runtimeProfile.external_sources.logparse}: ${logparseIdentity.code ?? "invalid"}.` });
  if (mcpRequired && mcpIdentity.status !== "PRESENT") blockers.push({ code: "MCP_SOURCE_INVALID", detail: `problem-locator-mcp must be clean at ${runtimeProfile.external_sources.mcp}: ${mcpIdentity.code ?? "invalid"}.` });
  if (closure.stages.some((stage) => stage.id === "platform.server-linux-capability")) {
    if (hostPlatform === "darwin") {
      if (!effectiveOptions.dockerContext) blockers.push({ code: "DOCKER_CONTEXT_REQUIRED", detail: "macOS Release requires --docker-context colima." });
      else if (effectiveOptions.dockerContext !== RELEASE_DOCKER_CONTEXT) blockers.push({ code: "DOCKER_CONTEXT_MISMATCH", detail: "The frozen macOS runtime profile is bound to Docker context colima." });
      if (dockerIdentity.status !== "PRESENT") blockers.push({ code: "DOCKER_SERVER_IDENTITY_INVALID", detail: `Docker/Colima must report a Linux amd64 Server: ${dockerIdentity.code ?? "invalid"}.` });
      if (uvIdentity.status !== "PRESENT") blockers.push({ code: "UV_RELEASE_CACHE_INVALID", detail: `The explicit uv cache is invalid: ${uvIdentity.code ?? "invalid"}.` });
      if (imageIdentity.status !== "PRESENT") blockers.push({ code: "RELEASE_BASE_IMAGE_INVALID", detail: `The offline Release image is invalid: ${imageIdentity.code ?? "invalid"}.` });
    } else if (!commandExists("docker")) {
      blockers.push({ code: "DOCKER_REQUIRED", detail: "A Docker client connected to a Linux server is required." });
    } else if (dockerIdentity.status !== "PRESENT") blockers.push({ code: "DOCKER_SERVER_UNAVAILABLE", detail: `The Docker server identity is unavailable: ${dockerIdentity.code ?? "invalid"}.` });
    else if (imageIdentity.status !== "PRESENT") blockers.push({ code: "RELEASE_BASE_IMAGE_INVALID", detail: `The Linux Release image cannot be frozen by exact ID: ${imageIdentity.code ?? "invalid"}.` });
  }

  for (const stage of closure.stages) {
    const identitySet = identities.sets[stage.identity_set];
    for (const missing of identitySet.missing_inputs) blockers.push({ code: "IDENTITY_INPUT_MISSING", detail: `${stage.id} requires ${missing.component_id} (${missing.status}).` });
  }

  const selectedStageIdentities = Object.fromEntries(closure.stages.map((stage) => [stage.id, stageIdentities[stage.id]]));
  const retry = retryRequirement(history, selectedStageIdentities);
  if (retry.recommendation === "STOP") {
    const fields = config.policy.retry.same_identity_requires;
    const structured = fields.every((field) => field === "expected_evidence" ? Boolean(options.expectedEvidence) : Boolean(options[field]));
    if (!structured) blockers.push({ code: "UNCHANGED_RETRY_INTENT_REQUIRED", detail: "The same failed identity requires reason, hypothesis and expected evidence.", retry });
    else warnings.push({ code: "UNCHANGED_RETRY_OVERRIDE", detail: "A structured new hypothesis authorizes this same-identity retry.", retry });
  }

  const stagePlan = closure.stages.map((stage) => {
    const reuse = reusable.get(stage.id);
    const cap = capForStage(stage, runtimeProfile);
    const gatePlans = stage.gates.map((gateId) => {
      const definition = config.gates.gates[gateId];
      const gateRuntimeProfile = definition.runtime_profile ?? null;
      return {
        id: gateId,
        kind: definition.kind,
        definition_digest: sha256Bytes(canonicalJson(definition)),
        gate_identity: sha256Bytes(canonicalJson({ stage_proof_identity: stageIdentities[stage.id].proof_identity, gate_id: gateId, definition })),
        evidence_contract: definition.evidence_contract ?? null,
        required_evidence: definition.evidence,
        runtime_profile: gateRuntimeProfile,
        runtime_profile_digest: gateRuntimeProfile
          ? sha256Bytes(canonicalJson(config.runtimeProfiles.profiles[gateRuntimeProfile]))
          : null,
      };
    });
    const verifyCodexMethodsCache = stage.id === "real.macos-codex-luna-methods" && methodsCache.status === "PRESENT";
    const verifyClaudeMethodsCache = stage.id === "real.macos-claude-deepseek-methods" && claudeMethodsCache.status === "PRESENT";
    const invocationCaps = verifyCodexMethodsCache || verifyClaudeMethodsCache ? [] : invocationCapsForStage(stage, runtimeProfile, config.gates.gates, {
      clientInvocationClass: dualLinuxContainers ? "linux-client-container" : "host-client",
      clientExecutionTopology: dualLinuxContainers ? "darwin-orchestrated-linux-container" : "native-host-client",
      scenarioId: selectedScenario,
    });
    return {
      id: stage.id,
      kind: stage.kind,
      depends_on: stage.depends_on,
      gates: gatePlans,
      identity_set: stage.identity_set,
      producer_identity: stageIdentities[stage.id].producer_identity,
      proof_identity: stageIdentities[stage.id].proof_identity,
      performance_identity: performanceIdentities[stage.id],
      decision: reuse ? "REUSE" : "RUN",
      reuse: reuse ? {
        run_id: reuse.run_id,
        committed_at_utc: reuse.committed_at_utc,
        attempt_root: reuse.attempt_root,
        source_stage_receipt_digest: reuse.stage.stage_receipt_digest,
        source_checkpoint: reuse.stage.checkpoint ?? null,
        current_reaudit: reuse.current_reaudit,
      } : null,
      timeout_seconds: stage.timeout_seconds,
      no_progress_seconds: noProgressForStage(stage, config.gates.gates, config.policy.process.real_no_progress_seconds),
      performance_mode: (config.policy.performance.stages[stage.id] ?? config.policy.performance.stages["*"]).mode,
      hard_caps: cap,
      invocation_caps: invocationCaps,
      estimated_tokens: stage.estimated_tokens ?? invocationCaps.reduce((sum, item) => sum + (item.aggregate ? item.caps.max_total_tokens : item.max_count * item.caps.max_turns * 1000), 0),
      estimated_cost_usd: invocationCaps.reduce((sum, item) => sum + (item.aggregate ? item.caps.max_budget_usd : item.max_count * item.caps.max_budget_usd), 0),
      checkpoint: stage.checkpoint ?? null,
    };
  });

  const proofPlan = closure.proofs.map((proof) => ({
    id: proof.id,
    acceptance: proof.acceptance,
    stages: proof.stages,
    proof_definition_digest: sha256Bytes(canonicalJson(proof)),
  }));
  const planCore = {
    schema_version: 2,
    track,
    goal,
    client,
    runtime_profile: runtimeProfileId,
    runtime_profile_digest: config.digests.runtimeProfiles,
    config_digests: config.digests,
    config_bundle_digest: config.bundle_digest,
    resume: track === "release" ? "fresh" : options.resume ?? defaults.resume,
    source: {
      available: source.available,
      base_commit: source.head,
      branch: source.branch,
      worktree_clean: source.clean,
      snapshot: publicSourceSnapshot(sourceSnapshot),
      baseline,
      changed_files: files,
    },
    release_inputs: formalRuntime ? {
      claude: {
        orchestrator_distribution_source: {
          status: clientDistribution.status,
          version: clientDistribution.version ?? null,
          cli_sha256: clientDistribution.cli_sha256 ?? null,
          package_name: clientDistribution.package_name ?? null,
          package_version: clientDistribution.package_version ?? null,
          package_manifest_sha256: clientDistribution.package_manifest_sha256 ?? null,
          package_tree_digest: clientDistribution.package_tree_digest ?? null,
          tarball_sha256: clientDistribution.tarball_sha256 ?? null,
          node: clientDistribution.node ?? null,
        },
        selected_client_runtime: selectedClientRuntime,
      },
      settings: {
        status: settingsIdentity.status,
        endpoint: settingsIdentity.endpoint ?? null,
        model: settingsIdentity.model ?? null,
        fingerprint: settingsIdentity.fingerprint ?? null,
        policy: "env-allowlist-only-no-hooks-v1",
      },
      codex: codexRequired ? codexIdentity : { status: "NOT_REQUIRED" },
      codex_logparse_runtime: codexLogparseRequired ? codexLogparseRuntime : { status: "NOT_REQUIRED" },
      codex_luna_methods_cache: methodsCache,
      claude_deepseek_methods_cache: claudeMethodsCache,
      browser: browserIdentity,
      docker: dockerIdentity,
      image: imageIdentity,
      image_validation_mode: imageValidationMode,
      uv_cache: uvIdentity,
      external_sources: {
        logparse: { status: logparseIdentity.status, root: logparseIdentity.root, head: logparseIdentity.head, clean: logparseIdentity.clean },
        problem_locator_mcp: { status: mcpIdentity.status, root: mcpIdentity.root, head: mcpIdentity.head, clean: mcpIdentity.clean },
      },
      cross_job_adapter: effectiveAdapter,
      topology: crossJobSelected
        ? dualLinuxContainers
          ? "darwin-orchestrated-dual-linux-containers"
          : "host-client-to-linux-server"
        : claudeDeepseekSelected
          ? quickValidationOrchestrator && hostPlatform === "linux"
            ? "sealed-ubuntu2204-container-claude-deepseek-quick-validation"
            : "darwin-local-claude-deepseek-quick-validation"
        : codexRequired
          ? quickValidationOrchestrator && hostPlatform === "linux"
            ? "sealed-ubuntu2204-container-codex-quick-validation"
            : "darwin-local-codex"
          : "not-applicable",
      orchestrator_platform: hostPlatform,
      network_policy: crossJobSelected ? runtimeProfile.network_policy : closure.stages.some((stage) => ["real.macos-codex-luna-e2e", "real.macos-claude-deepseek-e2e"].includes(stage.id)) ? "provider-plus-ipv4-loopback-mcp-upload-and-logparse-broker" : claudeDeepseekSelected ? "claude-provider-only" : codexRequired ? "codex-app-server-provider-only-command-network-denied" : "not-applicable",
    } : null,
    lineage: {
      root: track === "release" ? "GENESIS" : options.resume === "fresh" ? "GENESIS" : "AUTO",
      initial_data_root: track === "release" ? "EMPTY_REQUIRED" : "TRACK_POLICY",
      checkpoint_reuse: track === "release" ? "FORBIDDEN" : "CONFIGURED_PER_STAGE",
    },
    proofs: proofPlan,
    scenario: selectedScenario,
    stages: stagePlan,
    admission: { status: blockers.length === 0 ? "ADMITTED" : "BLOCKED", blockers, warnings },
    retry,
    intent: { reason: options.reason ?? null, hypothesis: options.hypothesis ?? null, expected_evidence: options.expectedEvidence ?? null },
    budget: {
      estimated_tokens: stagePlan.reduce((sum, stage) => sum + stage.estimated_tokens, 0),
      sum_of_per_invocation_caps_usd: stagePlan.reduce((sum, stage) => sum + stage.estimated_cost_usd, 0),
      cumulative_spending_cap: null,
      ...(codexRequired ? { posthoc_aggregate_limits: {
        exception_id: CODEX_LUNA_POSTHOC_EXCEPTION_ID,
        calls: closure.stages.some((stage) => stage.id === "real.macos-codex-luna-methods") ? MACOS_CODEX_LUNA_METHODS_CALLS : closure.stages.some((stage) => stage.id === "real.macos-codex-luna-e2e") ? MACOS_CODEX_LUNA_E2E_CALLS : CODEX_LUNA_NORMAL_CALLS,
        tokens: closure.stages.some((stage) => stage.id === "real.macos-codex-luna-methods") ? MACOS_CODEX_LUNA_METHODS_TOKEN_LIMIT : closure.stages.some((stage) => stage.id === "real.macos-codex-luna-e2e") ? MACOS_CODEX_LUNA_E2E_TOKEN_LIMIT : CODEX_LUNA_TOKEN_LIMIT,
        equivalent_usd: closure.stages.some((stage) => stage.id === "real.macos-codex-luna-methods") ? MACOS_CODEX_LUNA_METHODS_USD_LIMIT : closure.stages.some((stage) => stage.id === "real.macos-codex-luna-e2e") ? MACOS_CODEX_LUNA_E2E_USD_LIMIT : CODEX_LUNA_EQUIVALENT_USD_LIMIT,
        enforcement: "posthoc-terminal-aggregate",
        acknowledged: Boolean(effectiveOptions.allowCodexPosthocBudget),
      } } : {}),
      per_invocation_hard_enforced: stagePlan.flatMap((stage) => stage.invocation_caps).every((item) => (
        item.enforcement !== "posthoc-terminal-aggregate"
        &&
        item.caps.max_turns > 0
        && item.caps.max_total_tokens > 0
        && (item.caps.max_output_tokens === undefined || item.caps.max_output_tokens > 0)
        && item.caps.max_budget_usd > 0
        && item.caps.hard_timeout_seconds > 0
      )),
    },
    policies: {
      process: config.policy.process,
      evidence: config.policy.evidence,
      performance: config.policy.performance,
      track: trackConfig,
      status: config.policy.status,
    },
  };
  return {
    plan: { ...planCore, plan_fingerprint: sha256Bytes(canonicalJson(planCore)) },
    config,
    history,
    identities,
    stageIdentities,
    changedFiles: files,
    evidenceRoot,
    client,
    options: effectiveOptions,
    sourceSnapshot,
  };
}
