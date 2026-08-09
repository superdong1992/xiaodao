import path from "node:path";
import fs from "node:fs";
import { canonicalJson, commandExists, runSync, sha256Bytes } from "./util.mjs";
import { changedIdentityGroups, loadConfiguration, stagesForGoal } from "./config.mjs";
import { changedFiles, computeIdentityGroups, gitState, resolveChangeBaseline, stageIdentity } from "./identity.mjs";
import { findReusableStages, lastSuccessfulDevCommit, loadHistory } from "./history.mjs";
import {
  RELEASE_DOCKER_CONTEXT,
  RELEASE_LOGPARSE_COMMIT,
  RELEASE_MCP_COMMIT,
  claudeSettingsIdentity,
  dockerServerIdentity,
  externalGitIdentity,
  releaseCachePaths,
  validateClaudeDistribution,
  validateReleaseImage,
  validateUvCache,
} from "./release-inputs.mjs";

const FRESH_RELEASE_STAGES = new Set([
  "framework.self-test",
  "platform.host-capability",
  "platform.server-linux-capability",
  "journey.cross-job.environment",
  "journey.cross-job.route",
  "journey.cross-job.upload",
  "journey.cross-job.diagnose",
  "journey.cross-job.review",
  "journey.cross-job.publish-restart",
  "rollout.parity",
  "evidence.finalize",
]);

const JOURNEY_PARENT_STAGE = new Map([
  ["journey.cross-job.route", "journey.cross-job.environment"],
  ["journey.cross-job.upload", "journey.cross-job.route"],
  ["journey.cross-job.diagnose", "journey.cross-job.upload"],
  ["journey.cross-job.review", "journey.cross-job.diagnose"],
  ["journey.cross-job.publish-restart", "journey.cross-job.review"],
]);

export function resolveClient(requested, platform = process.platform) {
  if (requested && requested !== "auto") return requested;
  if (platform === "win32") return "windows";
  if (platform === "darwin") return "macos";
  return null;
}

function performanceIdentity(stage, group) {
  const producer = group.producer;
  return sha256Bytes(canonicalJson({
    schema_version: 1,
    stage_id: stage.id,
    scenario: stage.kind === "real-journey" ? "CrossJob" : null,
    source_digest: producer.source_digest,
    external_trees: producer.external_trees,
    client_version: producer.client?.version ?? null,
    client_hash: producer.client?.cli_sha256 ?? null,
    model_context: producer.model_context?.fingerprint ?? null,
    environment: producer.environment ?? null,
  }));
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

export function buildRunPlan(repoRoot, options) {
  const config = loadConfiguration(repoRoot);
  const defaults = config.flow.defaults;
  const track = options.track ?? defaults.track;
  const trackConfig = config.flow.tracks[track];
  if (!trackConfig) throw new Error(`TRACK_UNKNOWN:${track}`);
  const goal = options.goal ?? trackConfig.default_goal;
  const client = resolveClient(options.client);
  const builtInMacAdapter = path.join(repoRoot, "tools", "test-flow", "adapters", "macos-linux-release.mjs");
  const effectiveOptions = {
    ...options,
    crossJobAdapter: options.crossJobAdapter ?? (client === "macos" ? builtInMacAdapter : null),
  };
  const cachePaths = releaseCachePaths(repoRoot, effectiveOptions.cacheRoot);
  const formalRuntime = track === "release" || goal === "dev.real";
  const clientDistribution = formalRuntime
    ? validateClaudeDistribution(effectiveOptions.claudeEntry)
    : { status: effectiveOptions.claudeEntry ? validateClaudeDistribution(effectiveOptions.claudeEntry).status : "NOT_REQUIRED" };
  const settingsIdentity = formalRuntime
    ? claudeSettingsIdentity(effectiveOptions.claudeSettings)
    : { status: effectiveOptions.claudeSettings ? claudeSettingsIdentity(effectiveOptions.claudeSettings).status : "NOT_REQUIRED" };
  const dockerIdentity = client === "macos" && formalRuntime
    ? dockerServerIdentity(effectiveOptions.dockerContext)
    : { status: "NOT_REQUIRED", context: effectiveOptions.dockerContext ?? null };
  const uvIdentity = client === "macos" && formalRuntime
    ? validateUvCache(cachePaths)
    : { status: "NOT_REQUIRED" };
  const imageIdentity = client === "macos" && formalRuntime
    ? validateReleaseImage(cachePaths, dockerIdentity)
    : { status: "NOT_REQUIRED", image: cachePaths.baseImage };
  const logparseIdentity = formalRuntime
    ? externalGitIdentity(effectiveOptions.logparseSource, RELEASE_LOGPARSE_COMMIT)
    : { status: "NOT_REQUIRED", root: effectiveOptions.logparseSource ?? null };
  const mcpIdentity = formalRuntime
    ? externalGitIdentity(effectiveOptions.mcpSource, RELEASE_MCP_COMMIT)
    : { status: "NOT_REQUIRED", root: effectiveOptions.mcpSource ?? null };
  const releaseRuntime = {
    schema_version: 1,
    client_distribution: clientDistribution,
    settings: settingsIdentity,
    docker: dockerIdentity,
    uv_cache: uvIdentity,
    image: imageIdentity,
    network_policy: "release-offline-pull-never",
  };
  const evidenceRoot = path.resolve(repoRoot, options.evidenceRoot ?? defaults.evidence_root);
  const history = loadHistory(evidenceRoot);
  const source = gitState(repoRoot);
  const baseline = options.base
    ? { source: "explicit", commit: options.base }
    : resolveChangeBaseline(repoRoot, lastSuccessfulDevCommit(history));
  const files = changedFiles(repoRoot, baseline, source);
  const changedGroups = changedIdentityGroups(config.identities, files);
  const identityGroups = computeIdentityGroups({
    repoRoot,
    identityConfig: config.identities,
    externalTrees: {
      logparse: options.logparseSource,
      mcp: options.mcpSource,
      cross_job_adapter: effectiveOptions.crossJobAdapter,
    },
    claudeEntry: effectiveOptions.claudeEntry,
    claudeSettings: effectiveOptions.claudeSettings,
    releaseRuntime,
  });
  const policies = {
    parent_checkpoint: track === "release" ? "GENESIS" : options.resume === "fresh" ? "GENESIS" : "AUTO",
    scenario: null,
    no_progress_seconds: defaults.real_no_progress_seconds,
    selection: goal,
  };
  const stageIdentities = {};
  const performanceIdentities = {};
  for (const stage of config.flow.stages) {
    const parentStage = JOURNEY_PARENT_STAGE.get(stage.id);
    const identity = stageIdentity(stage, identityGroups, {
      ...policies,
      parent_checkpoint: parentStage ? stageIdentities[parentStage]?.producer_identity ?? "GENESIS" : "GENESIS",
    });
    if (stage.id === "rollout.parity") {
      const specDigest = options.rolloutParitySpec && fs.existsSync(options.rolloutParitySpec)
        ? sha256Bytes(fs.readFileSync(options.rolloutParitySpec))
        : null;
      stageIdentities[stage.id] = {
        producer_identity: sha256Bytes(canonicalJson({ base: identity.producer_identity, parity_spec_digest: specDigest })),
        proof_identity: sha256Bytes(canonicalJson({ base: identity.proof_identity, parity_spec_digest: specDigest })),
      };
    } else {
      stageIdentities[stage.id] = identity;
    }
    performanceIdentities[stage.id] = performanceIdentity(stage, identityGroups[stage.identity_group]);
  }
  const fresh = track === "release"
    ? FRESH_RELEASE_STAGES
    : new Set(["framework.self-test", "journey.cross-job.environment", "evidence.finalize"]);
  const reusable = findReusableStages(history, stageIdentities, {
    track,
    freshStageIds: fresh,
    knownSecrets: [process.env.ANTHROPIC_AUTH_TOKEN, process.env.ANTHROPIC_API_KEY, process.env.PROBLEM_LOCATOR_LOGPARSE_TOKEN].filter(Boolean),
  });
  for (const stageId of ["journey.cross-job.route", "journey.cross-job.upload", "journey.cross-job.review", "journey.cross-job.publish-restart"]) {
    const checkpoint = reusable.get(stageId)?.stage?.checkpoint;
    if (reusable.has(stageId) && (!checkpoint?.checkpoint_id || !checkpoint?.parent_checkpoint_id || !checkpoint?.path)) reusable.delete(stageId);
  }
  const diagnoseReuse = reusable.get("journey.cross-job.diagnose");
  const reviewReuse = reusable.get("journey.cross-job.review");
  if (!diagnoseReuse || !reviewReuse || diagnoseReuse.run_id !== reviewReuse.run_id) {
    reusable.delete("journey.cross-job.diagnose");
    reusable.delete("journey.cross-job.review");
  }
  let journeyParent = "GENESIS";
  let journeyChainBroken = false;
  for (const stageId of ["journey.cross-job.route", "journey.cross-job.upload", "journey.cross-job.review", "journey.cross-job.publish-restart"]) {
    const reuse = reusable.get(stageId);
    const checkpoint = reuse?.stage?.checkpoint;
    if (journeyChainBroken || !reuse || checkpoint.parent_checkpoint_id !== journeyParent) {
      reusable.delete(stageId);
      if (stageId === "journey.cross-job.review") reusable.delete("journey.cross-job.diagnose");
      journeyChainBroken = true;
      continue;
    }
    journeyParent = checkpoint.checkpoint_id;
  }
  let selected = stagesForGoal(config, {
    goalId: goal,
    track,
    requestedStage: options.stage,
    changedGroups,
    reusableStages: new Set(reusable.keys()),
  });
  const macIsolatedStagesOmitted = track === "release" && client === "macos" && selected.some((stage) => stage.kind === "isolated-real");
  if (track === "release" && client === "macos") {
    selected = selected.filter((stage) => stage.kind !== "isolated-real");
  }
  const blockers = [];
  const warnings = [];
  if (macIsolatedStagesOmitted) {
    warnings.push({
      code: "MACOS_ISOLATED_SERVER_GATES_COVERED_BY_CROSS_JOB",
      detail: "macOS runs the native macOS Client to the Linux Server; Linux agent and Logparse proofs run inside that fresh CrossJob journey.",
    });
  }
  const containsReal = selected.some((stage) => ["isolated-real", "real-journey", "capability", "rollout"].includes(stage.kind));
  if (!source.available) blockers.push({ code: "GIT_REQUIRED", detail: "A Git worktree is required for input identity." });
  if (trackConfig.requires_clean_commit && !source.clean) blockers.push({ code: "RELEASE_SOURCE_DIRTY", detail: "Release requires a clean committed source tree." });
  if (track === "release" && !client) blockers.push({ code: "RELEASE_CLIENT_UNRESOLVED", detail: "Linux hosts require an explicit --client linux; Windows/macOS follow the host." });
  if (client && !["windows", "macos", "linux"].includes(client)) blockers.push({ code: "CLIENT_UNKNOWN", detail: `Unsupported client ${client}.` });
  if (track === "dev" && containsReal && !options.allowRealModel) blockers.push({ code: "DEV_REAL_OPT_IN_REQUIRED", detail: "Dev real proofs require --allow-real-model." });
  if (track === "dev" && containsReal && !options.reason) blockers.push({ code: "DEV_REAL_REASON_REQUIRED", detail: "Dev real proofs require --reason." });
  if (track === "release" && options.resume && options.resume !== "fresh" && options.resume !== "auto") blockers.push({ code: "RELEASE_RESUME_FORBIDDEN", detail: "Release must start from GENESIS and an empty DATA_ROOT." });
  if (track === "release" && options.resume === "auto") warnings.push({ code: "RELEASE_RESUME_FORCED_FRESH", detail: "Release ignores checkpoint auto-resume and starts fresh." });
  if (formalRuntime && !effectiveOptions.claudeEntry) {
    blockers.push({ code: "CLAUDE_ENTRY_REQUIRED", detail: "Formal real-model proofs require --claude-entry pointing at the isolated official npm cli.js; global claude is never a fallback." });
  } else if (formalRuntime && clientDistribution.status !== "PRESENT") {
    blockers.push({ code: "CLAUDE_DISTRIBUTION_INVALID", detail: `The Claude distribution is not exact official npm 2.1.89: ${clientDistribution.code ?? "invalid"}.` });
  }
  if (client === "macos" && formalRuntime && effectiveOptions.claudeEntry && path.resolve(effectiveOptions.claudeEntry) !== path.resolve(cachePaths.claudeEntry)) {
    blockers.push({ code: "CLAUDE_ENTRY_CACHE_MISMATCH", detail: "macOS Release must use cli.js from the explicitly prepared frozen cache root." });
  }
  if (formalRuntime && !effectiveOptions.claudeSettings) {
    blockers.push({ code: "CLAUDE_SETTINGS_REQUIRED", detail: "Formal real-model proofs require --claude-settings; only the seven allowlisted env values are materialized and Hooks are not copied." });
  } else if (formalRuntime && settingsIdentity.status !== "PRESENT") {
    blockers.push({ code: "CLAUDE_SETTINGS_INVALID", detail: `Claude settings failed the env-only materialization policy: ${settingsIdentity.code ?? "invalid"}.` });
  }
  const crossJobSelected = selected.some((stage) => stage.id.startsWith("journey.cross-job."));
  if (crossJobSelected && !effectiveOptions.crossJobAdapter) {
    blockers.push({ code: "CROSS_JOB_ADAPTER_REQUIRED", detail: "CrossJob stages require --cross-job-adapter before any model call can start." });
  }
  if (effectiveOptions.crossJobAdapter && (!path.isAbsolute(effectiveOptions.crossJobAdapter) || !fs.existsSync(effectiveOptions.crossJobAdapter) || !fs.statSync(effectiveOptions.crossJobAdapter).isFile())) {
    blockers.push({ code: "CROSS_JOB_ADAPTER_INVALID", detail: "The CrossJob adapter must be an existing absolute file." });
  }
  if (client === "macos" && crossJobSelected && effectiveOptions.crossJobAdapter !== builtInMacAdapter && !options.crossJobAdapter) {
    blockers.push({ code: "MACOS_BUILTIN_ADAPTER_INVALID", detail: "The repository-owned macOS CrossJob adapter could not be resolved." });
  }
  if (crossJobSelected || selected.some((stage) => stage.id === "platform.server-linux-capability")) {
    if (logparseIdentity.status !== "PRESENT") {
      blockers.push({ code: "LOGPARSE_SOURCE_INVALID", detail: `Logparse must be clean at ${RELEASE_LOGPARSE_COMMIT}: ${logparseIdentity.code ?? "invalid"}.` });
    }
    if (mcpIdentity.status !== "PRESENT") {
      blockers.push({ code: "MCP_SOURCE_INVALID", detail: `problem-locator-mcp must be clean at ${RELEASE_MCP_COMMIT}: ${mcpIdentity.code ?? "invalid"}.` });
    }
  }
  if (selected.some((stage) => stage.id === "platform.server-linux-capability")) {
    if (client === "macos") {
      if (!effectiveOptions.dockerContext) blockers.push({ code: "DOCKER_CONTEXT_REQUIRED", detail: "macOS Release requires --docker-context colima." });
      else if (effectiveOptions.dockerContext !== RELEASE_DOCKER_CONTEXT) blockers.push({ code: "DOCKER_CONTEXT_MISMATCH", detail: "macOS Release is bound to the colima Docker context." });
      if (dockerIdentity.status !== "PRESENT") blockers.push({ code: "DOCKER_SERVER_IDENTITY_INVALID", detail: `Docker/Colima must report a Linux amd64 Server: ${dockerIdentity.code ?? "invalid"}.` });
      if (uvIdentity.status !== "PRESENT") blockers.push({ code: "UV_RELEASE_CACHE_INVALID", detail: `The explicit uv 0.11.32 Linux x64 cache is invalid: ${uvIdentity.code ?? "invalid"}.` });
      if (imageIdentity.status !== "PRESENT") blockers.push({ code: "RELEASE_BASE_IMAGE_INVALID", detail: `The offline linux/amd64 Release image is invalid: ${imageIdentity.code ?? "invalid"}.` });
    } else if (!commandExists("docker")) {
      blockers.push({ code: "DOCKER_REQUIRED", detail: "A Docker client and Linux server are required." });
    } else {
      const docker = runSync("docker", ["version", "--format", "{{json .Server}}"]);
      if (docker.status !== 0) blockers.push({ code: "DOCKER_SERVER_UNAVAILABLE", detail: "The Docker server is unavailable." });
    }
  }
  if (selected.some((stage) => stage.id === "rollout.parity") && !options.rolloutParitySpec) {
    blockers.push({ code: "ROLLOUT_PARITY_SPEC_REQUIRED", detail: "The one-time parity goal requires --rollout-parity-spec; it can never pass without executing both commands." });
  }
  if (options.rolloutParitySpec && (!path.isAbsolute(options.rolloutParitySpec) || !fs.existsSync(options.rolloutParitySpec))) {
    blockers.push({ code: "ROLLOUT_PARITY_SPEC_INVALID", detail: "The parity specification must be an existing absolute JSON file." });
  }
  const identityBlockers = new Set();
  for (const stage of selected) {
    if (!["isolated-real", "real-journey", "rollout"].includes(stage.kind)) continue;
    const group = identityGroups[stage.identity_group];
    for (const missing of group.missing_inputs) identityBlockers.add(`${stage.identity_group}:${missing}`);
    if (stage.action !== "real_logparse" && group.producer.model_context?.status !== "PRESENT") {
      identityBlockers.add(`${stage.identity_group}:provider-context`);
    }
  }
  const clientIdentity = identityGroups.client.producer.client;
  if (selected.some((stage) => stage.id === "platform.host-capability") && clientIdentity?.status !== "PRESENT") {
    identityBlockers.add("client:binary");
  }
  if (selected.some((stage) => stage.id === "platform.server-linux-capability")) {
    for (const missing of identityGroups.server.missing_inputs) identityBlockers.add(`server:${missing}`);
    if (identityGroups.server.producer.model_context?.status !== "PRESENT") identityBlockers.add("server:provider-context");
  }
  if (selected.some((stage) => ["isolated-real", "real-journey", "rollout"].includes(stage.kind)) && clientIdentity?.status !== "PRESENT") {
    identityBlockers.add("client:binary");
  }
  for (const missing of [...identityBlockers].sort()) {
    blockers.push({ code: "IDENTITY_INPUT_MISSING", detail: `Required exact identity input is missing: ${missing}.` });
  }

  const selectedStageIdentities = Object.fromEntries(selected.map((stage) => [stage.id, stageIdentities[stage.id]]));
  const retry = retryRequirement(history, selectedStageIdentities);
  if (retry.recommendation === "STOP") {
    const structured = options.reason && options.hypothesis && options.expectedEvidence;
    if (!structured) blockers.push({ code: "UNCHANGED_RETRY_INTENT_REQUIRED", detail: "The same failed identity may run again only with --reason, --hypothesis, and --expected-evidence.", retry });
    else warnings.push({ code: "UNCHANGED_RETRY_OVERRIDE", detail: "Agent supplied a structured new hypothesis for an unchanged failed identity.", retry });
  }

  const stagePlan = selected.map((stage) => {
    const reuse = reusable.get(stage.id);
    const decision = reuse ? "REUSE" : "RUN";
    const modelCallingStage = stage.kind === "isolated-real" || [
      "journey.cross-job.route",
      "journey.cross-job.diagnose",
      "journey.cross-job.publish-restart",
    ].includes(stage.id);
    return {
      id: stage.id,
      kind: stage.kind,
      action: stage.action,
      depends_on: stage.depends_on,
      identity_group: stage.identity_group,
      producer_identity: stageIdentities[stage.id].producer_identity,
      proof_identity: stageIdentities[stage.id].proof_identity,
      performance_identity: performanceIdentities[stage.id],
      decision,
      reuse: reuse ? { run_id: reuse.run_id, committed_at_utc: reuse.committed_at_utc, attempt_root: reuse.attempt_root, current_reaudit: reuse.current_reaudit } : null,
      timeout_seconds: stage.timeout_seconds,
      no_progress_seconds: ["isolated-real", "real-journey", "capability"].includes(stage.kind) ? defaults.real_no_progress_seconds : null,
      estimated_tokens: stage.estimated_tokens ?? (modelCallingStage ? 5000 : 0),
      estimated_cost_usd: stage.estimated_cost_usd ?? (modelCallingStage ? 3 : 0),
    };
  });
  const planCore = {
    schema_version: 1,
    track,
    goal,
    client,
    resume: track === "release" ? "fresh" : options.resume ?? defaults.resume,
    source: {
      available: source.available,
      head: source.head,
      branch: source.branch,
      clean: source.clean,
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
      docker: {
        status: dockerIdentity.status,
        context: dockerIdentity.context ?? null,
        os: dockerIdentity.os ?? null,
        architecture: dockerIdentity.architecture ?? null,
        version: dockerIdentity.version ?? null,
        context_fingerprint: dockerIdentity.context_fingerprint ?? null,
        colima_version: dockerIdentity.colima_version ?? null,
        colima_status_fingerprint: dockerIdentity.colima_status_fingerprint ?? null,
      },
      image: imageIdentity,
      uv_cache: uvIdentity,
      external_sources: {
        logparse: { status: logparseIdentity.status, root: logparseIdentity.root, head: logparseIdentity.head, clean: logparseIdentity.clean },
        problem_locator_mcp: { status: mcpIdentity.status, root: mcpIdentity.root, head: mcpIdentity.head, clean: mcpIdentity.clean },
      },
      cross_job_adapter: effectiveOptions.crossJobAdapter,
      release_network_policy: "offline-pull-never",
    } : null,
    lineage: {
      root: track === "release" ? "GENESIS" : policies.parent_checkpoint,
      initial_data_root: track === "release" ? "EMPTY_REQUIRED" : "TRACK_POLICY",
      checkpoint_reuse: track === "release" ? "FORBIDDEN" : "IDENTITY_GATED",
    },
    changed_identity_groups: [...changedGroups].sort(),
    stages: stagePlan,
    admission: { status: blockers.length === 0 ? "ADMITTED" : "BLOCKED", blockers, warnings },
    retry,
    intent: {
      reason: options.reason ?? null,
      hypothesis: options.hypothesis ?? null,
      expected_evidence: options.expectedEvidence ?? null,
    },
    budget_advisory: {
      estimated_tokens: stagePlan.reduce((sum, stage) => sum + stage.estimated_tokens, 0),
      estimated_cost_usd: stagePlan.reduce((sum, stage) => sum + stage.estimated_cost_usd, 0),
      hard_enforced: false,
    },
    policies: {
      performance: trackConfig.performance_policy,
      performance_window: defaults.history_limit,
      performance_min_samples: defaults.performance_min_samples,
      performance_consecutive_failures: defaults.performance_consecutive_failures,
      real_no_progress_seconds: defaults.real_no_progress_seconds,
      real_hard_timeout_seconds: defaults.real_hard_timeout_seconds,
      event_visibility_seconds: defaults.event_visibility_seconds,
      event_file_limit_bytes: defaults.event_file_limit_bytes,
      raw_log_file_limit_bytes: defaults.raw_log_file_limit_bytes,
    },
  };
  return {
    plan: { ...planCore, plan_fingerprint: sha256Bytes(canonicalJson(planCore)) },
    config,
    history,
    identityGroups,
    stageIdentities,
    changedFiles: files,
    evidenceRoot,
    client,
    options: effectiveOptions,
  };
}
