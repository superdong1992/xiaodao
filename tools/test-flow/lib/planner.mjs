import path from "node:path";
import fs from "node:fs";
import { canonicalJson, commandExists, runSync, sha256Bytes } from "./util.mjs";
import { changedIdentityGroups, loadConfiguration, stagesForGoal } from "./config.mjs";
import { changedFiles, computeIdentityGroups, gitState, resolveChangeBaseline, stageIdentity } from "./identity.mjs";
import { findReusableStages, lastSuccessfulDevCommit, loadHistory } from "./history.mjs";

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
    client_hash: producer.client?.sha256 ?? null,
    model_context: producer.model_context?.fingerprint ?? null,
    environment: producer.environment ?? null,
  }));
}

function retryRequirement(history, stageIdentities) {
  for (const entry of [...history].reverse()) {
    const failed = (entry.verdict.stages ?? []).find((stage) => !["PASS", "NOT_REQUIRED"].includes(stage.status));
    if (!failed) continue;
    const desired = stageIdentities[failed.id];
    if (desired && desired.producer_identity === failed.producer_identity && desired.proof_identity === failed.proof_identity) {
      return {
        recommendation: "STOP",
        reason: "UNCHANGED_FAILED_IDENTITY",
        previous_run_id: entry.verdict.run_id,
        stage_id: failed.id,
        previous_code: failed.code ?? null,
      };
    }
    break;
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
      cross_job_adapter: options.crossJobAdapter,
      server_model_probe: process.env.TEST_FLOW_SERVER_MODEL_PROBE,
    },
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
  const selected = stagesForGoal(config, {
    goalId: goal,
    track,
    requestedStage: options.stage,
    changedGroups,
    reusableStages: new Set(reusable.keys()),
  });
  const client = resolveClient(options.client);
  const blockers = [];
  const warnings = [];
  const containsReal = selected.some((stage) => ["isolated-real", "real-journey", "capability", "rollout"].includes(stage.kind));
  if (!source.available) blockers.push({ code: "GIT_REQUIRED", detail: "A Git worktree is required for input identity." });
  if (trackConfig.requires_clean_commit && !source.clean) blockers.push({ code: "RELEASE_SOURCE_DIRTY", detail: "Release requires a clean committed source tree." });
  if (track === "release" && !client) blockers.push({ code: "RELEASE_CLIENT_UNRESOLVED", detail: "Linux hosts require an explicit --client linux; Windows/macOS follow the host." });
  if (client && !["windows", "macos", "linux"].includes(client)) blockers.push({ code: "CLIENT_UNKNOWN", detail: `Unsupported client ${client}.` });
  if (track === "dev" && containsReal && !options.allowRealModel) blockers.push({ code: "DEV_REAL_OPT_IN_REQUIRED", detail: "Dev real proofs require --allow-real-model." });
  if (track === "dev" && containsReal && !options.reason) blockers.push({ code: "DEV_REAL_REASON_REQUIRED", detail: "Dev real proofs require --reason." });
  if (track === "release" && options.resume && options.resume !== "fresh" && options.resume !== "auto") blockers.push({ code: "RELEASE_RESUME_FORBIDDEN", detail: "Release must start from GENESIS and an empty DATA_ROOT." });
  if (track === "release" && options.resume === "auto") warnings.push({ code: "RELEASE_RESUME_FORCED_FRESH", detail: "Release ignores checkpoint auto-resume and starts fresh." });
  if (selected.some((stage) => stage.id.startsWith("journey.cross-job.")) && !options.crossJobAdapter) {
    blockers.push({ code: "CROSS_JOB_ADAPTER_REQUIRED", detail: "CrossJob stages require --cross-job-adapter before any model call can start." });
  }
  if (options.crossJobAdapter && (!path.isAbsolute(options.crossJobAdapter) || !fs.existsSync(options.crossJobAdapter) || !fs.statSync(options.crossJobAdapter).isFile())) {
    blockers.push({ code: "CROSS_JOB_ADAPTER_INVALID", detail: "The CrossJob adapter must be an existing absolute file." });
  }
  if (selected.some((stage) => stage.id === "platform.server-linux-capability")) {
    const probe = process.env.TEST_FLOW_SERVER_MODEL_PROBE;
    if (!probe || !path.isAbsolute(probe) || !fs.existsSync(probe) || !fs.statSync(probe).isFile()) {
      blockers.push({ code: "SERVER_MODEL_PROBE_REQUIRED", detail: "A frozen absolute TEST_FLOW_SERVER_MODEL_PROBE is required for the Linux server capability." });
    }
    if (!commandExists("docker")) blockers.push({ code: "DOCKER_REQUIRED", detail: "A Docker client and Linux server are required." });
    else {
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

  const retry = retryRequirement(history, stageIdentities);
  if (retry.recommendation === "STOP") {
    const structured = options.reason && options.hypothesis && options.expectedEvidence;
    if (!structured) blockers.push({ code: "UNCHANGED_RETRY_INTENT_REQUIRED", detail: "The same failed identity may run again only with --reason, --hypothesis, and --expected-evidence.", retry });
    else warnings.push({ code: "UNCHANGED_RETRY_OVERRIDE", detail: "Agent supplied a structured new hypothesis for an unchanged failed identity.", retry });
  }

  const stagePlan = selected.map((stage) => {
    const reuse = reusable.get(stage.id);
    const decision = reuse ? "REUSE" : "RUN";
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
      estimated_tokens: stage.estimated_tokens ?? (["isolated-real", "real-journey", "capability"].includes(stage.kind) ? 5000 : 0),
      estimated_cost_usd: stage.estimated_cost_usd ?? (["isolated-real", "real-journey", "capability"].includes(stage.kind) ? 3 : 0),
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
  };
}
