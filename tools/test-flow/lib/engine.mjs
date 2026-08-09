import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { executeAction } from "./actions.mjs";
import { createCheckpoint, restoreCheckpoint } from "./checkpoint.mjs";
import { createAttempt, finalizeAttempt } from "./evidence.mjs";
import { EventWriter } from "./events.mjs";
import { failureFingerprint, performanceSamples } from "./history.mjs";
import { buildRunPlan } from "./planner.mjs";
import { ResourceRegistry } from "./resources.mjs";
import { assessPerformance } from "./status.mjs";
import {
  readJson,
  redactError,
  removeTreeWritable,
  timestampForPath,
  writeJsonSync,
} from "./util.mjs";

const CHECKPOINT_NEXT_STAGE = new Map([
  ["journey.cross-job.route", "journey.cross-job.upload"],
  ["journey.cross-job.upload", "journey.cross-job.diagnose"],
  ["journey.cross-job.review", "journey.cross-job.publish-restart"],
  ["journey.cross-job.publish-restart", null],
]);

function runIdentifier() {
  return `run-${timestampForPath()}-${crypto.randomBytes(4).toString("hex")}`;
}

function knownSecrets(environment) {
  return [
    environment.ANTHROPIC_AUTH_TOKEN,
    environment.ANTHROPIC_API_KEY,
    environment.PROBLEM_LOCATOR_LOGPARSE_TOKEN,
    environment.TEST_FLOW_PROVIDER_TOKEN,
  ].filter(Boolean);
}

function priorConsecutiveSlow(history, stageId, performanceIdentity) {
  let count = 0;
  for (const entry of [...history].reverse()) {
    const stage = (entry.verdict.stages ?? []).find((candidate) => candidate.id === stageId && candidate.performance_identity === performanceIdentity);
    if (!stage || stage.status !== "PASS") continue;
    if (stage.performance_status !== "SLOW") break;
    count += 1;
  }
  return count;
}

function applyPerformance({ result, stage, planStage, history, track, policies }) {
  if (result.status !== "PASS" || stage.kind === "finalizer") return { status: "NOT_RUN", baseline: null };
  const samples = performanceSamples(history, stage.id, planStage.performance_identity);
  const external = ["isolated-real", "real-journey", "capability"].includes(stage.kind);
  let assessment = assessPerformance(result.elapsed_seconds, samples, { external });
  const devSlo = stage.id === "deterministic.affected" ? 60 : stage.id === "deterministic.full" ? 300 : null;
  if (track === "dev" && devSlo !== null && result.elapsed_seconds > devSlo) {
    assessment = { ...assessment, status: "SLOW", dev_slo_seconds: devSlo };
  }
  if (track === "release" && assessment.status === "SLOW") {
    const consecutive = priorConsecutiveSlow(history, stage.id, planStage.performance_identity) + 1;
    assessment = { ...assessment, consecutive_significant_regressions: consecutive };
    if (consecutive >= policies.performance_consecutive_failures) assessment.status = "FAIL";
  }
  return assessment;
}

function functionalStatus(results) {
  if (results.some((stage) => stage.status === "FAIL")) return "FAIL";
  if (results.some((stage) => ["BLOCKED", "INCONCLUSIVE", "ERROR", "NOT_RUN"].includes(stage.status))) return "INCONCLUSIVE";
  return "PASS";
}

function performanceStatus(results) {
  const values = results.map((stage) => stage.performance_status).filter(Boolean);
  if (values.includes("FAIL")) return "FAIL";
  if (values.includes("SLOW")) return "SLOW";
  if (values.includes("NOT_CALIBRATED")) return "NOT_CALIBRATED";
  return "PASS";
}

function firstFailure(results) {
  return results.find((stage) => !["PASS", "NOT_REQUIRED", "REUSED"].includes(stage.status)) ?? null;
}

function sealBoundaryCheckpoint({ attemptRoot, runId, track, stage, planStage, parentCheckpointId }) {
  if (!CHECKPOINT_NEXT_STAGE.has(stage.id)) return { checkpoint: null, parentCheckpointId };
  const sourcePath = path.join(attemptRoot, "payload", "stages", stage.id, "checkpoint-source.json");
  if (!fs.existsSync(sourcePath)) throw new Error(`CHECKPOINT_SOURCE_MISSING:${stage.id}`);
  const source = readJson(sourcePath);
  if (source.schema_version !== 1 || !path.isAbsolute(source.state_root)) throw new Error(`CHECKPOINT_SOURCE_INVALID:${stage.id}`);
  const continuation = {
    ...source.continuation,
    schema_version: 1,
    next_stage: CHECKPOINT_NEXT_STAGE.get(stage.id),
    origin_run_id: runId,
    origin_track: track,
    release_eligible: false,
  };
  const checkpoint = createCheckpoint({
    stateRoot: source.state_root,
    checkpointsRoot: path.join(attemptRoot, "payload", "checkpoints"),
    stageId: stage.id,
    continuation,
    identity: {
      schema_version: 1,
      producer_identity: planStage.producer_identity,
      identity_group: planStage.identity_group,
    },
    parentCheckpointId,
    quiescenceReceipt: source.quiescence_receipt,
    knownSecrets: knownSecrets(process.env),
  });
  fs.rmSync(sourcePath, { force: true });
  return {
    checkpoint: {
      ...checkpoint,
      path: path.relative(attemptRoot, checkpoint.path).split(path.sep).join("/"),
    },
    parentCheckpointId: checkpoint.checkpoint_id,
  };
}

function restoreReusableCheckpoint({ attemptRoot, pending, currentStageId }) {
  const relative = pending.checkpoint.path;
  const sourceRoot = path.resolve(pending.attemptRoot);
  const checkpointRoot = path.isAbsolute(relative) ? path.resolve(relative) : path.resolve(sourceRoot, relative);
  const sourcePrefix = `${sourceRoot}${path.sep}`;
  if (!checkpointRoot.startsWith(sourcePrefix)) throw new Error("CHECKPOINT_REUSE_PATH_OUTSIDE_ATTEMPT");
  const targetRoot = path.join(attemptRoot, "scratch", "restored", `${currentStageId}-${pending.checkpoint.checkpoint_id.slice(0, 12)}`);
  const identity = {
    schema_version: 1,
    producer_identity: pending.planStage.producer_identity,
    identity_group: pending.planStage.identity_group,
  };
  const receipt = restoreCheckpoint({
    checkpointRoot,
    targetRoot,
    currentIdentity: identity,
    knownSecrets: knownSecrets(process.env),
  });
  const continuationPath = path.join(checkpointRoot, "continuation.json");
  const stageRoot = path.join(attemptRoot, "payload", "stages", currentStageId);
  fs.mkdirSync(stageRoot, { recursive: true, mode: 0o700 });
  writeJsonSync(path.join(stageRoot, "restore-receipt.json"), {
    schema_version: 1,
    status: receipt.status,
    checkpoint_id: receipt.checkpoint_id,
    portable_digest: receipt.portable_digest,
    restored_for_stage: currentStageId,
    source_run_id: path.basename(sourceRoot),
  });
  return {
    state_root: targetRoot,
    continuation_path: continuationPath,
    checkpoint_id: receipt.checkpoint_id,
  };
}

export async function runFlow(repoRoot, options) {
  const built = buildRunPlan(repoRoot, options);
  if (options.planOnly) return { plan: built.plan, verdict: null, attemptRoot: null, exitCode: built.plan.admission.status === "ADMITTED" ? 0 : 2 };

  const runId = runIdentifier();
  const attemptRoot = createAttempt({ evidenceRoot: built.evidenceRoot, runId });
  const plan = { ...built.plan, run_id: runId, created_at_utc: new Date().toISOString() };
  writeJsonSync(path.join(attemptRoot, "payload", "run-plan.json"), plan);
  const eventWriter = new EventWriter({
    attemptRoot,
    runId,
    producerId: "orchestrator",
    producerType: "orchestrator",
    limitBytes: plan.policies.event_file_limit_bytes,
  });
  const resources = new ResourceRegistry(attemptRoot, runId);
  const stageResults = [];
  let operationStatus = "PASS";
  let stopped = false;
  let parentCheckpointId = "GENESIS";
  let pendingCheckpoint = null;

  try {
    eventWriter.write("run.created", { data: { track: plan.track, goal: plan.goal } });
    if (plan.admission.status !== "ADMITTED") {
      eventWriter.write("run.blocked", { data: { blocker_count: plan.admission.blockers.length } });
      stopped = true;
    } else {
      eventWriter.write("run.admitted", { data: { stage_count: plan.stages.length } });
    }

    for (const planStage of plan.stages) {
      if (planStage.kind === "finalizer") continue;
      const stage = built.config.flow.stages.find((entry) => entry.id === planStage.id);
      if (stopped) {
        stageResults.push({
          id: stage.id,
          kind: stage.kind,
          status: "NOT_RUN",
          code: "PRIOR_STAGE_NOT_PASSING",
          failure_domain: null,
          producer_identity: planStage.producer_identity,
          proof_identity: planStage.proof_identity,
          performance_identity: planStage.performance_identity,
          performance_status: "NOT_RUN",
          elapsed_seconds: 0,
        });
        continue;
      }
      if (planStage.decision === "REUSE") {
        const result = {
          id: stage.id,
          kind: stage.kind,
          status: "PASS",
          result_source: "REUSED",
          reused_from: planStage.reuse,
          producer_identity: planStage.producer_identity,
          proof_identity: planStage.proof_identity,
          performance_identity: planStage.performance_identity,
          performance_status: "PASS",
          elapsed_seconds: 0,
          usage: { input_tokens: 0, output_tokens: 0, cost_usd: 0 },
          checkpoint: planStage.reuse.stage?.checkpoint ?? null,
        };
        stageResults.push(result);
        writeJsonSync(path.join(attemptRoot, "payload", "stages", `${stage.id}.json`), result);
        eventWriter.write("stage.reused", { stageId: stage.id, data: { source_run_id: planStage.reuse.run_id } });
        if (planStage.reuse.stage?.checkpoint?.checkpoint_id) {
          pendingCheckpoint = {
            checkpoint: planStage.reuse.stage.checkpoint,
            attemptRoot: planStage.reuse.attempt_root,
            planStage,
          };
        }
        continue;
      }

      let actionResult;
      let restoredCheckpoint = null;
      try {
        if (pendingCheckpoint && stage.id.startsWith("journey.cross-job.")) {
          restoredCheckpoint = restoreReusableCheckpoint({ attemptRoot, pending: pendingCheckpoint, currentStageId: stage.id });
          parentCheckpointId = restoredCheckpoint.checkpoint_id;
          pendingCheckpoint = null;
          eventWriter.write("checkpoint.restored", { stageId: stage.id, data: { checkpoint_id: restoredCheckpoint.checkpoint_id } });
        }
        actionResult = await executeAction({
          repoRoot,
          attemptRoot,
          options,
          client: built.client,
          policies: plan.policies,
          changedFiles: built.changedFiles,
          identityGroups: built.identityGroups,
          eventWriter,
          resources,
          sourceHead: plan.source.head,
          planStage,
          restoredCheckpoint,
        }, stage);
      } catch (error) {
        actionResult = { status: "ERROR", failure_domain: "HARNESS", code: "ACTION_EXCEPTION", elapsed_seconds: 0, error: redactError(error) };
      }
      let checkpoint = null;
      if (actionResult.status === "PASS") {
        try {
          const sealed = sealBoundaryCheckpoint({ attemptRoot, runId, track: plan.track, stage, planStage, parentCheckpointId });
          checkpoint = sealed.checkpoint;
          parentCheckpointId = sealed.parentCheckpointId;
        } catch (error) {
          actionResult = { status: "ERROR", failure_domain: "HARNESS", code: "CHECKPOINT_SEAL_FAILED", elapsed_seconds: actionResult.elapsed_seconds ?? 0, error: redactError(error) };
        }
      }
      const performance = applyPerformance({ result: actionResult, stage, planStage, history: built.history, track: plan.track, policies: plan.policies });
      const result = {
        id: stage.id,
        kind: stage.kind,
        ...actionResult,
        producer_identity: planStage.producer_identity,
        proof_identity: planStage.proof_identity,
        performance_identity: planStage.performance_identity,
        performance_status: performance.status,
        performance_baseline: performance.baseline,
        consecutive_significant_regressions: performance.consecutive_significant_regressions ?? 0,
        checkpoint,
      };
      stageResults.push(result);
      writeJsonSync(path.join(attemptRoot, "payload", "stages", `${stage.id}.json`), result);
      if (!["PASS", "NOT_REQUIRED"].includes(result.status) || result.performance_status === "FAIL") {
        stopped = true;
        if (result.status === "ERROR") operationStatus = "ERROR";
      }
    }
  } catch (error) {
    operationStatus = "ERROR";
    stageResults.push({
      id: "orchestrator",
      kind: "framework",
      status: "ERROR",
      code: "ORCHESTRATOR_EXCEPTION",
      failure_domain: "HARNESS",
      error: redactError(error),
      elapsed_seconds: 0,
      performance_status: "NOT_RUN",
    });
    try { eventWriter.write("run.failed", { data: { code: "ORCHESTRATOR_EXCEPTION" } }); } catch {}
  } finally {
    try { eventWriter.close(); } catch (error) {
      operationStatus = "ERROR";
      stageResults.push({
        id: "evidence.events",
        kind: "framework",
        status: "ERROR",
        code: "EVENT_STREAM_CLOSE_FAILED",
        failure_domain: "HARNESS",
        error: redactError(error),
        elapsed_seconds: 0,
        performance_status: "NOT_RUN",
      });
    }
    const scratch = path.join(attemptRoot, "scratch");
    try { removeTreeWritable(scratch, attemptRoot); } catch (error) {
      operationStatus = "ERROR";
      stageResults.push({
        id: "evidence.scratch-cleanup",
        kind: "framework",
        status: "ERROR",
        code: "SCRATCH_CLEANUP_FAILED",
        failure_domain: "HARNESS",
        error: redactError(error),
        elapsed_seconds: 0,
        performance_status: "NOT_RUN",
      });
    }
  }

  const failure = firstFailure(stageResults);
  const functional = plan.admission.status === "ADMITTED" ? functionalStatus(stageResults) : "INCONCLUSIVE";
  const performance = performanceStatus(stageResults);
  const candidate = {
    schema_version: 1,
    run_id: runId,
    track: plan.track,
    goal: plan.goal,
    functional_status: functional,
    performance_status: performance,
    operation_status: operationStatus === "ERROR" ? "ERROR" : plan.admission.status === "ADMITTED" ? "PASS" : "BLOCKED",
    failure_domain: failure?.failure_domain ?? (plan.admission.status === "ADMITTED" ? null : "INFRA"),
    failure_fingerprint: failure ? failureFingerprint({ stageId: failure.id, identity: failure, failureDomain: failure.failure_domain, code: failure.code }) : null,
    stages: stageResults,
    source: { head: plan.source.head, clean: plan.source.clean, baseline: plan.source.baseline },
    admission: plan.admission,
    usage: stageResults.reduce((usage, stage) => ({
      input_tokens: usage.input_tokens + (stage.usage?.input_tokens ?? 0),
      output_tokens: usage.output_tokens + (stage.usage?.output_tokens ?? 0),
      cost_usd: Math.round((usage.cost_usd + (stage.usage?.cost_usd ?? 0)) * 1_000_000) / 1_000_000,
    }), { input_tokens: 0, output_tokens: 0, cost_usd: 0 }),
  };
  const verdict = await finalizeAttempt({
    attemptRoot,
    candidate,
    knownSecrets: knownSecrets(process.env),
    resourcePolicy: (policy) => resources.apply(policy),
  });
  return { plan, verdict, attemptRoot, exitCode: verdict.exit_code };
}
