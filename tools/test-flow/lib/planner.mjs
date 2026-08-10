import fs from "node:fs";
import path from "node:path";
import { canonicalJson, sha256Bytes } from "./util.mjs";
import { canonicalStageDefinition, loadConfiguration, resolveGoalClosure } from "./config.mjs";
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
  claudeSettingsIdentity,
  dockerServerIdentity,
  externalGitIdentity,
  releaseDockerContextForClient,
  releaseCachePaths,
  validateClaudeDistribution,
  validateReleaseImage,
  validateUvCache,
} from "./release-inputs.mjs";
import { captureSourceSnapshot, publicSourceSnapshot } from "./source-snapshot.mjs";

const BUILT_IN_ADAPTERS = Object.freeze({
  macos: "macos-linux-release.mjs",
  windows: "windows-linux-release.mjs",
  linux: "linux-linux-release.mjs",
});

export function resolveClient(requested, platform = process.platform) {
  if (requested && requested !== "auto") return requested;
  if (platform === "win32") return "windows";
  if (platform === "darwin") return "macos";
  return null;
}

function builtInAdapter(repoRoot, client) {
  const name = BUILT_IN_ADAPTERS[client];
  return name ? path.join(repoRoot, "tools", "test-flow", "adapters", name) : null;
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

function releaseRuntimeIdentity({ profile, clientDistribution, settingsIdentity, dockerIdentity, uvIdentity, imageIdentity }) {
  return {
    schema_version: 2,
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
      client_distribution: clientDistribution,
      settings: settingsIdentity,
      docker: dockerIdentity,
      uv_cache: uvIdentity,
      image: imageIdentity,
    },
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
  if (stage.kind === "isolated-real" && stage.id !== "real.logparse") return profile.real_caps.isolated;
  if (stage.id === "journey.cross-job.route") return profile.real_caps["journey.route"];
  if (stage.id === "journey.cross-job.diagnose") return profile.real_caps["journey.diagnose"];
  if (stage.id === "journey.cross-job.publish-restart") return profile.real_caps["journey.publish-restart"];
  return null;
}

function invocationCapsForStage(stage, profile) {
  const cap = capForStage(stage, profile);
  if (stage.id === "real.logparse") return [];
  if (stage.kind === "isolated-real") {
    return [{ class: "isolated-agent", min_count: stage.id === "real.diagnose" ? 2 : 1, max_count: stage.id === "real.diagnose" ? 2 : 1, caps: cap }];
  }
  if (stage.id === "journey.cross-job.route") return [
    { class: "host-client", min_count: 1, max_count: 1, caps: cap },
    { class: "server-agent", min_count: 3, max_count: 3, caps: profile.real_caps.service_agent },
  ];
  if (stage.id === "journey.cross-job.diagnose") return [
    { class: "host-client", min_count: 1, max_count: 1, caps: cap },
    { class: "server-agent", min_count: 3, max_count: 3, caps: profile.real_caps.service_agent },
  ];
  if (stage.id === "journey.cross-job.publish-restart") return [{ class: "host-client", min_count: 1, max_count: 1, caps: cap }];
  return [];
}

export function buildRunPlan(repoRoot, options = {}) {
  const config = loadConfiguration(repoRoot);
  const defaults = config.policy.defaults;
  const track = options.track ?? defaults.track;
  const trackConfig = config.policy.tracks[track];
  if (!trackConfig) throw new Error(`TRACK_UNKNOWN:${track}`);
  const goal = options.goal ?? trackConfig.default_goal;
  const client = resolveClient(options.client);
  const planningClient = client ?? "linux";
  const closure = resolveGoalClosure(config, { goalId: goal, track, requestedStage: options.stage ?? null, client: planningClient });
  const crossJobSelected = closure.stages.some((stage) => stage.kind === "real-journey" || stage.id === "journey.cross-job.review");
  const effectiveAdapter = crossJobSelected ? builtInAdapter(repoRoot, client) : null;
  const effectiveOptions = { ...options, crossJobAdapter: effectiveAdapter };
  const runtimeProfileId = options.runtimeProfile ?? defaults.runtime_profile;
  const runtimeProfile = config.runtimeProfiles.profiles[runtimeProfileId];
  if (!runtimeProfile) throw new Error(`RUNTIME_PROFILE_UNKNOWN:${runtimeProfileId}`);
  const formalRuntime = formalRuntimeRequired(closure.stages);
  const requiresLinuxServer = closure.stages.some((stage) => stage.id === "platform.server-linux-capability" || stage.id.startsWith("journey.cross-job."));
  const expectedDockerContext = client ? releaseDockerContextForClient(client) : null;
  if (requiresLinuxServer && expectedDockerContext && !effectiveOptions.dockerContext) effectiveOptions.dockerContext = expectedDockerContext;
  const cachePaths = releaseCachePaths(repoRoot, effectiveOptions.cacheRoot);
  const clientDistribution = formalRuntime
    ? validateClaudeDistribution(effectiveOptions.claudeEntry)
    : { status: effectiveOptions.claudeEntry ? validateClaudeDistribution(effectiveOptions.claudeEntry).status : "NOT_REQUIRED" };
  const settingsIdentity = formalRuntime
    ? claudeSettingsIdentity(effectiveOptions.claudeSettings)
    : { status: effectiveOptions.claudeSettings ? claudeSettingsIdentity(effectiveOptions.claudeSettings).status : "NOT_REQUIRED" };
  const dockerIdentity = requiresLinuxServer && client
    ? dockerServerIdentity(effectiveOptions.dockerContext, client)
    : { status: "NOT_REQUIRED", context: effectiveOptions.dockerContext ?? null };
  const uvIdentity = requiresLinuxServer ? validateUvCache(cachePaths) : { status: "NOT_REQUIRED" };
  const imageIdentity = requiresLinuxServer
    ? validateReleaseImage(cachePaths, dockerIdentity)
    : { status: "NOT_REQUIRED", image: cachePaths.baseImage };
  const logparseIdentity = formalRuntime
    ? externalGitIdentity(effectiveOptions.logparseSource, runtimeProfile.external_sources.logparse)
    : { status: "NOT_REQUIRED", root: effectiveOptions.logparseSource ?? null };
  const mcpIdentity = formalRuntime
    ? externalGitIdentity(effectiveOptions.mcpSource, runtimeProfile.external_sources.mcp)
    : { status: "NOT_REQUIRED", root: effectiveOptions.mcpSource ?? null };
  const releaseRuntime = releaseRuntimeIdentity({ profile: runtimeProfile, clientDistribution, settingsIdentity, dockerIdentity, uvIdentity, imageIdentity });

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
    externalTrees: { logparse: logparseIdentity, mcp: mcpIdentity },
    claudeEntry: effectiveOptions.claudeEntry,
    claudeSettings: effectiveOptions.claudeSettings,
    releaseRuntime,
  });

  const stageIdentities = {};
  const performanceIdentities = {};
  for (const stage of config.stages.stages) {
    const dependencyProofIdentities = stage.depends_on.map((stageId) => stageIdentities[stageId]?.proof_identity).filter(Boolean);
    const journeyDependency = stage.depends_on.find((stageId) => stageId.startsWith("journey.cross-job."));
    const definitionDigest = sha256Bytes(canonicalJson(canonicalStageDefinition(config, stage)));
    const identity = stageIdentity(stage, identities.sets, {
      parent_checkpoint: journeyDependency ? stageIdentities[journeyDependency].producer_identity : "GENESIS",
      scenario: "CrossJob",
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
  const containsReal = closure.stages.some((stage) => ["isolated-real", "real-journey"].includes(stage.kind));
  if (!source.available) blockers.push({ code: "GIT_REQUIRED", detail: "A Git worktree is required to enumerate the source snapshot." });
  if (trackConfig.requires_source_snapshot && !sourceSnapshot.digest) blockers.push({ code: "SOURCE_SNAPSHOT_REQUIRED", detail: "The Git-visible worktree could not be frozen into an exact source snapshot." });
  if (track === "release" && !client) blockers.push({ code: "RELEASE_CLIENT_UNRESOLVED", detail: "Linux hosts require an explicit --client linux; Windows/macOS follow the host." });
  if (client && !Object.hasOwn(BUILT_IN_ADAPTERS, client)) blockers.push({ code: "CLIENT_UNKNOWN", detail: `Unsupported client ${client}.` });
  if (track === "dev" && containsReal && trackConfig.real_requires_opt_in && !options.allowRealModel) blockers.push({ code: "DEV_REAL_OPT_IN_REQUIRED", detail: "Dev real proofs require --allow-real-model." });
  if (track === "dev" && containsReal && trackConfig.real_requires_intent && !options.reason) blockers.push({ code: "DEV_REAL_REASON_REQUIRED", detail: "Dev real proofs require --reason." });
  if (track === "release" && options.resume && !["fresh", "auto"].includes(options.resume)) blockers.push({ code: "RELEASE_RESUME_FORBIDDEN", detail: "Release must start from GENESIS and an empty DATA_ROOT." });
  if (track === "release" && options.resume === "auto") warnings.push({ code: "RELEASE_RESUME_FORCED_FRESH", detail: "Release starts from GENESIS; Release checkpoint reuse is forbidden." });

  if (formalRuntime && !effectiveOptions.claudeEntry) blockers.push({ code: "CLAUDE_ENTRY_REQUIRED", detail: "Formal proofs require the runtime-profile-bound official npm cli.js." });
  else if (formalRuntime && clientDistribution.status !== "PRESENT") blockers.push({ code: "CLAUDE_DISTRIBUTION_INVALID", detail: `Claude distribution does not match runtime profile ${runtimeProfileId}: ${clientDistribution.code ?? "invalid"}.` });
  if (track === "release" && formalRuntime && effectiveOptions.claudeEntry && path.resolve(effectiveOptions.claudeEntry) !== path.resolve(cachePaths.claudeEntry)) blockers.push({ code: "CLAUDE_ENTRY_CACHE_MISMATCH", detail: "Release proofs must use cli.js from the explicit frozen cache root." });
  if (formalRuntime && !effectiveOptions.claudeSettings) blockers.push({ code: "CLAUDE_SETTINGS_REQUIRED", detail: "Formal proofs require an env-only Claude settings source." });
  else if (formalRuntime && settingsIdentity.status !== "PRESENT") blockers.push({ code: "CLAUDE_SETTINGS_INVALID", detail: `Claude settings violate the runtime profile: ${settingsIdentity.code ?? "invalid"}.` });

  if (crossJobSelected && !effectiveAdapter) blockers.push({ code: "CROSS_JOB_ADAPTER_REQUIRED", detail: `No first-party ${client ?? "unresolved"}→Linux CrossJob adapter was selected.` });
  if (effectiveAdapter && (!path.isAbsolute(effectiveAdapter) || !fs.existsSync(effectiveAdapter) || !fs.statSync(effectiveAdapter).isFile())) blockers.push({ code: "CROSS_JOB_ADAPTER_INVALID", detail: "The selected CrossJob adapter is not an existing repository-owned or explicit file." });
  if (crossJobSelected && client && effectiveAdapter !== builtInAdapter(repoRoot, client)) blockers.push({ code: "BUILTIN_ADAPTER_IDENTITY_INVALID", detail: `The ${client} first-party adapter did not resolve exactly.` });

  if (requiresLinuxServer) {
    if (logparseIdentity.status !== "PRESENT") blockers.push({ code: "LOGPARSE_SOURCE_INVALID", detail: `Logparse must be a clean repository containing frozen commit ${runtimeProfile.external_sources.logparse}: ${logparseIdentity.code ?? "invalid"}.` });
    if (mcpIdentity.status !== "PRESENT") blockers.push({ code: "MCP_SOURCE_INVALID", detail: `problem-locator-mcp must be a clean repository containing frozen commit ${runtimeProfile.external_sources.mcp}: ${mcpIdentity.code ?? "invalid"}.` });
  }
  if (closure.stages.some((stage) => stage.id === "platform.server-linux-capability")) {
    if (expectedDockerContext !== "default" && !options.dockerContext) blockers.push({ code: "DOCKER_CONTEXT_REQUIRED", detail: `${client} Release requires --docker-context ${expectedDockerContext}.` });
    if (options.dockerContext && options.dockerContext !== expectedDockerContext) blockers.push({ code: "DOCKER_CONTEXT_MISMATCH", detail: `The frozen ${client} runtime profile is bound to logical Docker context ${expectedDockerContext}.` });
    if (dockerIdentity.status !== "PRESENT") blockers.push({ code: "DOCKER_SERVER_IDENTITY_INVALID", detail: `Docker must report the profile-bound Linux amd64 Server: ${dockerIdentity.code ?? "invalid"}.` });
    if (uvIdentity.status !== "PRESENT") blockers.push({ code: "UV_RELEASE_CACHE_INVALID", detail: `The explicit uv cache is invalid: ${uvIdentity.code ?? "invalid"}.` });
    if (imageIdentity.status !== "PRESENT") blockers.push({ code: "RELEASE_BASE_IMAGE_INVALID", detail: `The offline Release image is invalid: ${imageIdentity.code ?? "invalid"}.` });
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
    const invocationCaps = invocationCapsForStage(stage, runtimeProfile);
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
      no_progress_seconds: ["isolated-real", "real-journey", "capability"].includes(stage.kind) ? config.policy.process.real_no_progress_seconds : null,
      performance_mode: (config.policy.performance.stages[stage.id] ?? config.policy.performance.stages["*"]).mode,
      hard_caps: cap,
      invocation_caps: invocationCaps,
      estimated_tokens: invocationCaps.reduce((sum, item) => sum + item.max_count * item.caps.max_turns * 1000, 0),
      estimated_cost_usd: invocationCaps.reduce((sum, item) => sum + item.max_count * item.caps.max_budget_usd, 0),
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
        status: clientDistribution.status,
        version: clientDistribution.version ?? null,
        cli_sha256: clientDistribution.cli_sha256 ?? null,
        package_name: clientDistribution.package_name ?? null,
        package_version: clientDistribution.package_version ?? null,
        package_manifest_sha256: clientDistribution.package_manifest_sha256 ?? null,
        package_tree_digest: clientDistribution.package_tree_digest ?? null,
        tarball_sha256: clientDistribution.tarball_sha256 ?? null,
        node_version: clientDistribution.node?.version ?? null,
        node_sha256: clientDistribution.node?.sha256 ?? null,
      },
      settings: {
        status: settingsIdentity.status,
        endpoint: settingsIdentity.endpoint ?? null,
        model: settingsIdentity.model ?? null,
        fingerprint: settingsIdentity.fingerprint ?? null,
        policy: "env-allowlist-only-no-hooks-v1",
      },
      docker: dockerIdentity,
      image: imageIdentity,
      uv_cache: uvIdentity,
      external_sources: {
        logparse: { status: logparseIdentity.status, root: logparseIdentity.root, head: logparseIdentity.head, clean: logparseIdentity.clean, pinned_commit: logparseIdentity.pinned_commit, tree_oid: logparseIdentity.tree_oid, tree_manifest_sha256: logparseIdentity.tree_manifest_sha256 },
        problem_locator_mcp: { status: mcpIdentity.status, root: mcpIdentity.root, head: mcpIdentity.head, clean: mcpIdentity.clean, pinned_commit: mcpIdentity.pinned_commit, tree_oid: mcpIdentity.tree_oid, tree_manifest_sha256: mcpIdentity.tree_manifest_sha256 },
      },
      cross_job_adapter: effectiveAdapter,
      network_policy: runtimeProfile.network_policy,
    } : null,
    lineage: {
      root: track === "release" ? "GENESIS" : options.resume === "fresh" ? "GENESIS" : "AUTO",
      initial_data_root: track === "release" ? "EMPTY_REQUIRED" : "TRACK_POLICY",
      checkpoint_reuse: track === "release" ? "FORBIDDEN" : "CONFIGURED_PER_STAGE",
    },
    proofs: proofPlan,
    stages: stagePlan,
    admission: { status: blockers.length === 0 ? "ADMITTED" : "BLOCKED", blockers, warnings },
    retry,
    intent: { reason: options.reason ?? null, hypothesis: options.hypothesis ?? null, expected_evidence: options.expectedEvidence ?? null },
    budget: {
      estimated_tokens: stagePlan.reduce((sum, stage) => sum + stage.estimated_tokens, 0),
      sum_of_per_invocation_caps_usd: stagePlan.reduce((sum, stage) => sum + stage.estimated_cost_usd, 0),
      cumulative_spending_cap: null,
      per_invocation_hard_enforced: stagePlan.flatMap((stage) => stage.invocation_caps).every((item) => item.caps.max_turns > 0 && item.caps.max_total_tokens > 0 && item.caps.max_budget_usd > 0 && item.caps.hard_timeout_seconds > 0),
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
