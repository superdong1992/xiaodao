import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { executeGate } from "./actions.mjs";
import { createCheckpoint, restoreCheckpoint } from "./checkpoint.mjs";
import { createAttempt, finalizeAttempt } from "./evidence.mjs";
import { EventWriter } from "./events.mjs";
import { projectCandidateFailureDiagnostic } from "./failure-diagnostic.mjs";
import { failureFingerprint, performanceSamples } from "./history.mjs";
import { buildRunPlan } from "./planner.mjs";
import { ResourceRegistry } from "./resources.mjs";
import {
  materializeSourceSnapshot,
  verifyMaterializedSourceSnapshot,
  verifySourceSnapshot,
} from "./source-snapshot.mjs";
import { adjudicateStagePerformance } from "./status.mjs";
import { isCompleteUsage, normalizeUsage, sumUsage, TOKEN_USAGE_FORMULA, zeroUsage } from "./usage.mjs";
import {
  ISOLATED_AGENT_ENV_POLICY_VERSION,
  ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT,
  validEnvironmentKeySummary,
} from "../runtime-support/isolated-agent-env.mjs";
import {
  assertFlow,
  canonicalJson,
  readJson,
  redactError,
  removeTreeWritable,
  sha256Bytes,
  sha256File,
  timestampForPath,
  writeJsonSync,
} from "./util.mjs";

export function validEvidenceV2CurrentAttemptCorePlan(plan) {
  const requiresCurrentCore = plan?.stages?.some((stage) => [
    "real.macos-codex-luna-e2e",
    "real.macos-claude-deepseek-e2e",
    "evidence-v2.release-verdict",
  ].includes(stage.id));
  if (!requiresCurrentCore) return true;
  const core = plan.stages.find((stage) => stage.id === "deterministic.full");
  return core?.decision === "RUN"
    && core.reuse === null
    && core.gates?.some((gate) => gate.id === "det.evidence-v2-core");
}

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
    if (!stage || stage.result_source !== "EXECUTED" || stage.status !== "PASS") continue;
    if (![
      "SLOW",
      "FAIL",
    ].includes(stage.performance_status) || (stage.performance_status === "FAIL" && stage.performance_reason !== "CONSECUTIVE_SIGNIFICANT_REGRESSION")) break;
    count += 1;
  }
  return count;
}

function functionalStageStatus(gates) {
  if (gates.some((gate) => gate.status === "ERROR")) return "ERROR";
  if (gates.some((gate) => gate.status === "FAIL")) return "FAIL";
  if (gates.some((gate) => gate.status === "BLOCKED")) return "BLOCKED";
  if (gates.some((gate) => gate.status === "INCONCLUSIVE")) return "INCONCLUSIVE";
  if (gates.every((gate) => gate.status === "NOT_REQUIRED")) return "NOT_REQUIRED";
  return "PASS";
}

function performanceStatus(stages) {
  const values = stages.map((stage) => stage.performance_status).filter(Boolean);
  if (values.includes("FAIL")) return "FAIL";
  if (values.includes("SLOW")) return "SLOW";
  if (values.includes("NOT_CALIBRATED")) return "NOT_CALIBRATED";
  if (values.includes("PASS")) return "PASS";
  return "NOT_RUN";
}

function proofResults(plan, stages) {
  const stageById = new Map(stages.map((stage) => [stage.id, stage]));
  return plan.proofs.map((proof) => {
    const members = proof.stages.map((stageId) => stageById.get(stageId)).filter(Boolean);
    let status = "PASS";
    if (members.some((stage) => stage.status === "ERROR")) status = "ERROR";
    else if (members.some((stage) => stage.status === "FAIL")) status = "FAIL";
    else if (members.some((stage) => ["BLOCKED", "INCONCLUSIVE", "NOT_RUN"].includes(stage.status))) status = "INCONCLUSIVE";
    else if (members.length !== proof.stages.length) status = "INCONCLUSIVE";
    return {
      id: proof.id,
      acceptance: "all",
      status,
      stages: proof.stages.map((stageId) => ({ id: stageId, status: stageById.get(stageId)?.status ?? "MISSING" })),
      proof_definition_digest: proof.proof_definition_digest,
    };
  });
}

function functionalProofStatus(proofs) {
  if (proofs.some((proof) => proof.status === "FAIL")) return "FAIL";
  if (proofs.some((proof) => proof.status === "ERROR")) return "INCONCLUSIVE";
  if (proofs.some((proof) => proof.status !== "PASS")) return "INCONCLUSIVE";
  return "PASS";
}

function firstFailure(stages) {
  for (const stage of stages) {
    const gate = stage.gates?.find((candidate) => !["PASS", "NOT_REQUIRED"].includes(candidate.status));
    if (gate) return { ...gate, id: stage.id, gate_id: gate.id };
    if (!["PASS", "NOT_REQUIRED"].includes(stage.status)) return stage;
    if (stage.performance_status === "FAIL") return { ...stage, failure_domain: "PERFORMANCE", code: stage.performance_reason ?? "PERFORMANCE_FAIL" };
  }
  return null;
}

export function buildRunCandidate({
  attemptRoot,
  runId,
  plan,
  stageResults,
  operationStatus,
  sourceSnapshotVerification,
  preFinalizationResourceReceipt,
}) {
  const proofs = proofResults(plan, stageResults);
  const failure = firstFailure(stageResults);
  const failureDiagnostic = projectCandidateFailureDiagnostic({ attemptRoot, stages: stageResults });
  const functional = plan.admission.status === "ADMITTED" ? functionalProofStatus(proofs) : "INCONCLUSIVE";
  return {
    schema_version: 2,
    run_id: runId,
    track: plan.track,
    goal: plan.goal,
    functional_status: functional,
    performance_status: performanceStatus(stageResults),
    operation_status: operationStatus === "ERROR" ? "ERROR" : plan.admission.status === "ADMITTED" ? "PASS" : "BLOCKED",
    failure_domain: failure?.failure_domain ?? (plan.admission.status === "ADMITTED" ? null : "INFRA"),
    failure_fingerprint: failure ? failureFingerprint({ stageId: failure.id, identity: failure, failureDomain: failure.failure_domain, code: failure.code }) : null,
    failure_diagnostic: functional === "PASS" ? null : failureDiagnostic,
    proofs,
    stages: stageResults,
    gates: stageResults.flatMap((stage) => (stage.gates ?? []).map((gate) => ({ stage_id: stage.id, ...gate }))),
    source: {
      base_commit: plan.source.base_commit,
      branch: plan.source.branch,
      worktree_clean_at_start: plan.source.worktree_clean,
      snapshot: plan.source.snapshot,
      baseline: plan.source.baseline,
      verification: sourceSnapshotVerification,
    },
    config_digests: plan.config_digests,
    config_bundle_digest: plan.config_bundle_digest,
    runtime_profile: plan.runtime_profile,
    runtime_profile_digest: plan.runtime_profile_digest,
    plan_fingerprint: plan.plan_fingerprint,
    policy_digest: plan.config_digests.policy,
    status_policy: plan.policies.status,
    lineage: { ...plan.lineage, fresh_admission: stageResults.find((stage) => stage.id === "journey.cross-job.environment")?.gates?.[0]?.fresh_admission ?? null },
    admission: plan.admission,
    pre_finalization_resource_receipt: preFinalizationResourceReceipt,
    usage: sumUsage(stageResults.map((stage) => stage.usage)),
    candidate_input_digest: sha256Bytes(canonicalJson({ run_id: runId, plan_fingerprint: plan.plan_fingerprint, proofs, stages: stageResults.map((stage) => ({ id: stage.id, digest: stage.stage_receipt_digest })) })),
  };
}

function evidenceRecord(filePath, attemptRoot) {
  const metadata = fs.statSync(filePath);
  return {
    path: path.relative(attemptRoot, filePath).split(path.sep).join("/"),
    size: metadata.size,
    sha256: sha256File(filePath),
  };
}

function locateGateEvidence(attemptRoot, stageId, gateId, name) {
  const candidates = [
    path.join(attemptRoot, "payload", "stages", stageId, "gates", gateId, name),
    path.join(attemptRoot, "payload", "stages", stageId, name),
  ];
  return candidates.find((candidate) => fs.existsSync(candidate) && fs.statSync(candidate).isFile()) ?? null;
}

export function applyGateEvidenceContract({ actionResult, gate, gatePlan, stage, attemptRoot }) {
  const evidence = [];
  for (const name of gate.evidence) {
    const filePath = locateGateEvidence(attemptRoot, stage.id, gatePlan.id, name);
    if (!filePath) {
      if (actionResult.status !== "PASS") continue;
      return {
        result: { ...actionResult, status: "ERROR", failure_domain: "HARNESS", code: "GATE_REQUIRED_EVIDENCE_MISSING", missing_evidence: name },
        evidence,
      };
    }
    evidence.push(evidenceRecord(filePath, attemptRoot));
  }
  return { result: actionResult, evidence };
}

export function validOutputTokenCapEvidence(invocation, caps) {
  if (Object.hasOwn(invocation, "observed_request_limits")) return false;
  const declaredCap = caps?.max_output_tokens;
  const marker = invocation.hard_cap_enforcement?.max_output_tokens;
  if (declaredCap === undefined) {
    if (marker !== undefined) return false;
    if (invocation.class !== "isolated-agent") return true;
    return invocation.environment_policy?.schema_version === 1
      && invocation.environment_policy?.version === ISOLATED_AGENT_ENV_POLICY_VERSION
      && validEnvironmentKeySummary(invocation.environment_policy?.inbound)
      && validEnvironmentKeySummary(invocation.environment_policy?.claude_process)
      && !(invocation.environment_policy.inbound.key_names ?? []).includes("CLAUDE_CODE_MAX_OUTPUT_TOKENS")
      && !(invocation.environment_policy.claude_process.key_names ?? []).includes("CLAUDE_CODE_MAX_OUTPUT_TOKENS");
  }
  return Number.isSafeInteger(declaredCap)
    && declaredCap > 0
    && marker === ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT
    && invocation.environment_policy?.schema_version === 1
    && invocation.environment_policy?.version === ISOLATED_AGENT_ENV_POLICY_VERSION
    && validEnvironmentKeySummary(invocation.environment_policy?.inbound)
    && validEnvironmentKeySummary(invocation.environment_policy?.claude_process)
    && !(invocation.environment_policy?.inbound?.key_names ?? []).includes("CLAUDE_CODE_MAX_OUTPUT_TOKENS")
    && (invocation.environment_policy?.claude_process?.key_names ?? []).includes("CLAUDE_CODE_MAX_OUTPUT_TOKENS");
}

export function applyHardCaps({ result, planStage, expectedModel }) {
  if (result.status !== "PASS") return result;
  const expected = planStage.invocation_caps ?? [];
  const actual = result.invocations ?? [];
  if (!Array.isArray(actual)) return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "MODEL_INVOCATION_RECEIPT_INVALID" };
  const ids = actual.map((invocation) => invocation?.invocation_id);
  if (ids.some((id) => typeof id !== "string" || !id) || new Set(ids).size !== ids.length) {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "MODEL_INVOCATION_ID_INVALID" };
  }
  const expectedClasses = new Set(expected.map((item) => item.class));
  if (actual.some((invocation) => !expectedClasses.has(invocation.class))) {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "MODEL_INVOCATION_CLASS_UNEXPECTED" };
  }
  for (const declaration of expected) {
    const members = actual.filter((invocation) => invocation.class === declaration.class);
    if (members.length < declaration.min_count || members.length > declaration.max_count) {
      return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "MODEL_INVOCATION_COUNT_MISMATCH" };
    }
    const expectedInvocationModel = declaration.model ?? expectedModel;
    for (const invocation of members) {
      const totalTokenEnforcement = declaration.aggregate
        ? invocation.hard_cap_enforcement?.total_tokens === `posthoc-terminal-aggregate:${TOKEN_USAGE_FORMULA}`
        : invocation.hard_cap_enforcement?.total_tokens === `terminal-usage-postcondition:${TOKEN_USAGE_FORMULA}`;
      if (
        invocation.schema_version !== 3
        || (declaration.execution_topology !== undefined && invocation.execution_topology !== declaration.execution_topology)
        || !isCompleteUsage(invocation.usage)
        || invocation.usage_complete !== true
        || invocation.effective_model !== expectedInvocationModel
        || (declaration.reasoning_effort !== undefined && invocation.effective_reasoning_effort !== declaration.reasoning_effort)
        || canonicalJson(invocation.effective_caps) !== canonicalJson(declaration.caps)
        || invocation.terminal?.subtype !== "success"
        || invocation.terminal?.is_error !== false
        || invocation.wrapper_outcome?.schema_version !== 1
        || invocation.wrapper_outcome?.status !== "PASS"
        || invocation.wrapper_outcome?.code !== null
        || !totalTokenEnforcement
        || !validOutputTokenCapEvidence(invocation, declaration.caps)
      ) {
        return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "MODEL_HARD_CAP_RECEIPT_MISMATCH" };
      }
      if (!Number.isSafeInteger(invocation.turns) || invocation.turns <= 0 || invocation.turns > declaration.caps.max_turns) {
        return { ...result, status: "FAIL", failure_domain: "CONTRACT", code: "MODEL_TURN_CAP_EXCEEDED" };
      }
      const usage = invocation.usage ?? {};
      if (!Number.isFinite(usage.cost_usd) || usage.cost_usd < 0 || usage.cost_usd > declaration.caps.max_budget_usd) {
        return { ...result, status: "FAIL", failure_domain: "CONTRACT", code: "MODEL_BUDGET_CAP_EXCEEDED" };
      }
      const totalTokens = usage.total_tokens;
      if (!Number.isSafeInteger(totalTokens) || totalTokens < 0 || totalTokens > declaration.caps.max_total_tokens) {
        return { ...result, status: "FAIL", failure_domain: "CONTRACT", code: "MODEL_TOKEN_CAP_EXCEEDED" };
      }
    }
    if (declaration.aggregate) {
      const aggregate = sumUsage(members.map((invocation) => invocation.usage));
      if (aggregate.total_tokens > declaration.caps.max_total_tokens) {
        return { ...result, status: "FAIL", failure_domain: "CONTRACT", code: "MODEL_TOKEN_CAP_EXCEEDED" };
      }
      if (aggregate.cost_usd > declaration.caps.max_budget_usd) {
        return { ...result, status: "FAIL", failure_domain: "CONTRACT", code: "MODEL_BUDGET_CAP_EXCEEDED" };
      }
    }
  }
  return { ...result, usage_complete: true, effective_caps: null, invocations: actual };
}

export function writeGateReceipt({ attemptRoot, stage, gatePlan, actionResult, evidence, planStage }) {
  const gateRoot = path.join(attemptRoot, "payload", "stages", stage.id, "gates", gatePlan.id);
  const receiptPath = path.join(gateRoot, "gate-receipt.json");
  const receipt = {
    schema_version: 2,
    stage_id: stage.id,
    gate_id: gatePlan.id,
    gate_kind: gatePlan.kind,
    gate_identity: gatePlan.gate_identity,
    definition_digest: gatePlan.definition_digest,
    evidence_contract: gatePlan.evidence_contract,
    runtime_profile: gatePlan.runtime_profile,
    runtime_profile_digest: gatePlan.runtime_profile_digest,
    result_source: "EXECUTED",
    status: actionResult.status,
    code: actionResult.code ?? null,
    failure_domain: actionResult.failure_domain ?? null,
    elapsed_seconds: Number(actionResult.elapsed_seconds ?? 0),
    usage: normalizeUsage(actionResult.usage),
    usage_complete: actionResult.usage_complete ?? (planStage.invocation_caps?.length ? false : true),
    effective_caps: actionResult.effective_caps ?? null,
    model_invocations: actionResult.invocations ?? [],
    fresh_admission: actionResult.fresh_admission ?? null,
    evidence,
    execution: {
      exit_code: actionResult.exit_code ?? null,
      signal: actionResult.signal ?? null,
      termination: actionResult.termination ?? null,
      stdout_path: actionResult.stdout_path ?? null,
      stderr_path: actionResult.stderr_path ?? null,
    },
    assertions: {
      pytest: actionResult.pytest ?? null,
      node_test: actionResult.node_test ?? null,
      selection: actionResult.selection ?? null,
      adapter: actionResult.adapter_receipt ?? null,
    },
  };
  writeJsonSync(receiptPath, receipt);
  return {
    ...receipt,
    id: gatePlan.id,
    receipt_path: path.relative(attemptRoot, receiptPath).split(path.sep).join("/"),
    receipt_digest: sha256File(receiptPath),
  };
}

function sealBoundaryCheckpoint({ attemptRoot, runId, track, stage, planStage, parentCheckpointId }) {
  if (!stage.checkpoint) return { checkpoint: null, parentCheckpointId };
  const sourcePath = path.join(attemptRoot, "payload", "stages", stage.id, "checkpoint-source.json");
  if (!fs.existsSync(sourcePath)) throw new Error(`CHECKPOINT_SOURCE_MISSING:${stage.id}`);
  const source = readJson(sourcePath);
  if (source.schema_version !== 1 || !path.isAbsolute(source.state_root)) throw new Error(`CHECKPOINT_SOURCE_INVALID:${stage.id}`);
  const continuation = {
    ...source.continuation,
    schema_version: 1,
    next_stage: stage.checkpoint.next_stage,
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
      schema_version: 2,
      producer_identity: planStage.producer_identity,
      identity_set: planStage.identity_set,
    },
    parentCheckpointId,
    quiescenceReceipt: source.quiescence_receipt,
    knownSecrets: knownSecrets(process.env),
  });
  fs.rmSync(sourcePath, { force: true });
  return {
    checkpoint: { ...checkpoint, path: path.relative(attemptRoot, checkpoint.path).split(path.sep).join("/") },
    parentCheckpointId: checkpoint.checkpoint_id,
  };
}

function restoreReusableCheckpoint({ attemptRoot, pending, currentStageId }) {
  const relative = pending.checkpoint.path;
  const sourceRoot = path.resolve(pending.attemptRoot);
  const checkpointRoot = path.isAbsolute(relative) ? path.resolve(relative) : path.resolve(sourceRoot, relative);
  if (!checkpointRoot.startsWith(`${sourceRoot}${path.sep}`)) throw new Error("CHECKPOINT_REUSE_PATH_OUTSIDE_ATTEMPT");
  const targetRoot = path.join(attemptRoot, "scratch", "restored", `${currentStageId}-${pending.checkpoint.checkpoint_id.slice(0, 12)}`);
  const identity = { schema_version: 2, producer_identity: pending.planStage.producer_identity, identity_set: pending.planStage.identity_set };
  const receipt = restoreCheckpoint({ checkpointRoot, targetRoot, currentIdentity: identity, knownSecrets: knownSecrets(process.env) });
  const continuationPath = path.join(checkpointRoot, "continuation.json");
  const stageRoot = path.join(attemptRoot, "payload", "stages", currentStageId);
  fs.mkdirSync(stageRoot, { recursive: true, mode: 0o700 });
  const restoreReceiptPath = path.join(stageRoot, "restore-receipt.json");
  writeJsonSync(restoreReceiptPath, {
    schema_version: 2,
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
    source_run_id: path.basename(sourceRoot),
    receipt_path: path.relative(attemptRoot, restoreReceiptPath).split(path.sep).join("/"),
    receipt_digest: sha256File(restoreReceiptPath),
  };
}

export function writeStageReceipt(attemptRoot, result) {
  const receiptPath = path.join(attemptRoot, "payload", "stages", result.id, "stage-receipt.json");
  writeJsonSync(receiptPath, result);
  return {
    ...result,
    stage_receipt_path: path.relative(attemptRoot, receiptPath).split(path.sep).join("/"),
    stage_receipt_digest: sha256File(receiptPath),
  };
}

function publicGateSummary(gate) {
  return {
    id: gate.id,
    kind: gate.gate_kind,
    status: gate.status,
    code: gate.code,
    failure_domain: gate.failure_domain,
    gate_identity: gate.gate_identity,
    definition_digest: gate.definition_digest,
    evidence_contract: gate.evidence_contract,
    runtime_profile: gate.runtime_profile,
    runtime_profile_digest: gate.runtime_profile_digest,
    receipt_path: gate.receipt_path,
    receipt_digest: gate.receipt_digest,
    elapsed_seconds: gate.elapsed_seconds,
    usage: gate.usage,
    usage_complete: gate.usage_complete,
    effective_caps: gate.effective_caps,
    model_invocations: gate.model_invocations,
    fresh_admission: gate.fresh_admission,
    evidence: gate.evidence,
  };
}

export function writeExecutedStageReceipt({
  attemptRoot,
  stage,
  planStage,
  gateResults,
  status = functionalStageStatus(gateResults),
  stageOperationFailure = null,
  performance = { status: "NOT_MEASURED", reason: null, baseline: null, consecutive_significant_regressions: 0 },
  checkpoint = null,
  restoredCheckpoint = null,
}) {
  const firstGateFailure = gateResults.find((gate) => !["PASS", "NOT_REQUIRED"].includes(gate.status));
  return writeStageReceipt(attemptRoot, {
    schema_version: 2,
    id: stage.id,
    kind: stage.kind,
    status,
    code: stageOperationFailure?.code ?? firstGateFailure?.code ?? null,
    failure_domain: stageOperationFailure?.failure_domain ?? firstGateFailure?.failure_domain ?? null,
    operation_failure: stageOperationFailure,
    result_source: "EXECUTED",
    producer_identity: planStage.producer_identity,
    proof_identity: planStage.proof_identity,
    performance_identity: planStage.performance_identity,
    performance_status: performance.status,
    performance_reason: performance.reason,
    performance_baseline: performance.baseline,
    consecutive_significant_regressions: performance.consecutive_significant_regressions,
    elapsed_seconds: Math.round(gateResults.reduce((sum, gate) => sum + Number(gate.elapsed_seconds ?? 0), 0) * 1000) / 1000,
    usage: sumUsage(gateResults.map((gate) => gate.usage)),
    gates: gateResults.map(publicGateSummary),
    checkpoint,
    restored_checkpoint: restoredCheckpoint ? {
      checkpoint_id: restoredCheckpoint.checkpoint_id,
      source_run_id: restoredCheckpoint.source_run_id,
      receipt_path: restoredCheckpoint.receipt_path,
      receipt_digest: restoredCheckpoint.receipt_digest,
    } : null,
  });
}

function notRunStage(stage, planStage) {
  return {
    schema_version: 2,
    id: stage.id,
    kind: stage.kind,
    status: "NOT_RUN",
    code: "PRIOR_STAGE_NOT_PASSING",
    failure_domain: null,
    result_source: "NOT_EXECUTED",
    producer_identity: planStage.producer_identity,
    proof_identity: planStage.proof_identity,
    performance_identity: planStage.performance_identity,
    performance_status: "NOT_MEASURED",
    performance_baseline: null,
    elapsed_seconds: 0,
    usage: zeroUsage(),
    gates: [],
    checkpoint: null,
  };
}

export async function runFlow(repoRoot, options) {
  const built = buildRunPlan(repoRoot, options);
  assertFlow(
    validEvidenceV2CurrentAttemptCorePlan(built.plan),
    "EVIDENCE_V2_CURRENT_ATTEMPT_CORE_REQUIRED",
    "Evidence V2 provider certification requires Core evidence from the current attempt",
  );
  if (options.planOnly) return { plan: built.plan, verdict: null, attemptRoot: null, exitCode: built.plan.admission.status === "ADMITTED" ? 0 : 2 };

  const runId = runIdentifier();
  const attemptRoot = createAttempt({ evidenceRoot: built.evidenceRoot, runId });
  const plan = { ...built.plan, run_id: runId, created_at_utc: new Date().toISOString() };
  writeJsonSync(path.join(attemptRoot, "payload", "run-plan.json"), plan);
  const sourceManifestPath = path.join(attemptRoot, "payload", "source", "source-snapshot.json");
  const sourceSnapshotRoot = path.join(attemptRoot, "scratch", "source-snapshot", "repository");
  writeJsonSync(sourceManifestPath, built.sourceSnapshot);
  let sourceSnapshotSetupError = null;
  try {
    materializeSourceSnapshot(repoRoot, sourceSnapshotRoot, built.sourceSnapshot);
  } catch (error) {
    sourceSnapshotSetupError = redactError(error);
  }
  const executionOptions = { ...built.options };
  if (executionOptions.crossJobAdapter && sourceSnapshotSetupError === null) {
    const relativeAdapter = path.relative(repoRoot, executionOptions.crossJobAdapter);
    if (relativeAdapter !== ".." && !relativeAdapter.startsWith(`..${path.sep}`) && !path.isAbsolute(relativeAdapter)) {
      executionOptions.crossJobAdapter = path.join(sourceSnapshotRoot, relativeAdapter);
    }
  }
  const policies = { ...plan.policies.process, ...plan.policies.evidence };
  const eventWriter = new EventWriter({
    attemptRoot,
    runId,
    producerId: "orchestrator",
    producerType: "orchestrator",
    limitBytes: policies.event_file_limit_bytes,
  });
  const resources = new ResourceRegistry(attemptRoot, runId, {
    dockerContext: built.options.dockerContext ?? null,
    expectedDockerIdentity: plan.release_inputs?.docker?.status === "PRESENT"
      ? plan.release_inputs.docker
      : null,
  });
  const stageResults = [];
  let operationStatus = "PASS";
  let stopped = false;
  let parentCheckpointId = "GENESIS";
  let pendingCheckpoint = null;
  let preFinalizationResourceReceipt = null;
  let sourceSnapshotVerification = null;

  try {
    eventWriter.write("run.created", { data: { track: plan.track, goal: plan.goal } });
    if (sourceSnapshotSetupError) {
      eventWriter.write("run.failed", { data: { code: "SOURCE_SNAPSHOT_MATERIALIZATION_FAILED" } });
      stageResults.push({
        schema_version: 2,
        id: "source.snapshot",
        kind: "framework",
        status: "ERROR",
        code: "SOURCE_SNAPSHOT_MATERIALIZATION_FAILED",
        failure_domain: "HARNESS",
        error: sourceSnapshotSetupError,
        elapsed_seconds: 0,
        performance_status: "NOT_MEASURED",
        gates: [],
      });
      operationStatus = "ERROR";
      stopped = true;
    } else if (plan.admission.status !== "ADMITTED") {
      eventWriter.write("run.blocked", { data: { blocker_count: plan.admission.blockers.length } });
      stopped = true;
    } else {
      eventWriter.write("run.admitted", { data: { stage_count: plan.stages.length, proof_count: plan.proofs.length } });
    }

    for (const planStage of plan.stages) {
      const stage = built.config.stages.stages.find((entry) => entry.id === planStage.id);
      if (stopped) {
        stageResults.push(writeStageReceipt(attemptRoot, notRunStage(stage, planStage)));
        continue;
      }
      if (planStage.decision === "REUSE") {
        const result = writeStageReceipt(attemptRoot, {
          schema_version: 2,
          id: stage.id,
          kind: stage.kind,
          status: "PASS",
          code: null,
          failure_domain: null,
          result_source: "REUSED",
          reused_from: planStage.reuse,
          producer_identity: planStage.producer_identity,
          proof_identity: planStage.proof_identity,
          performance_identity: planStage.performance_identity,
          performance_status: "NOT_MEASURED",
          performance_baseline: null,
          elapsed_seconds: null,
          usage: zeroUsage(),
          gates: [],
          checkpoint: planStage.reuse.source_checkpoint ?? null,
        });
        stageResults.push(result);
        eventWriter.write("stage.reused", { stageId: stage.id, data: { source_run_id: planStage.reuse.run_id } });
        if (planStage.reuse.source_checkpoint?.checkpoint_id) pendingCheckpoint = { checkpoint: planStage.reuse.source_checkpoint, attemptRoot: planStage.reuse.attempt_root, planStage };
        continue;
      }

      let restoredCheckpoint = null;
      try {
        if (pendingCheckpoint && stage.id.startsWith("journey.cross-job.")) {
          restoredCheckpoint = restoreReusableCheckpoint({ attemptRoot, pending: pendingCheckpoint, currentStageId: stage.id });
          parentCheckpointId = restoredCheckpoint.checkpoint_id;
          pendingCheckpoint = null;
          eventWriter.write("checkpoint.restored", { stageId: stage.id, data: { checkpoint_id: restoredCheckpoint.checkpoint_id } });
        }
      } catch (error) {
        const failed = writeStageReceipt(attemptRoot, {
          ...notRunStage(stage, planStage),
          status: "ERROR",
          code: "CHECKPOINT_RESTORE_FAILED",
          failure_domain: "HARNESS",
          error: redactError(error),
        });
        stageResults.push(failed);
        operationStatus = "ERROR";
        stopped = true;
        continue;
      }

      const gateResults = [];
      for (const gatePlan of planStage.gates) {
        const gate = built.config.gates.gates[gatePlan.id];
        const gateRuntimeProfile = gatePlan.runtime_profile
          ? built.config.runtimeProfiles.profiles[gatePlan.runtime_profile]
          : null;
        let actionResult;
        try {
          const before = verifySourceSnapshot(repoRoot, built.sourceSnapshot);
          if (before.status !== "PASS") {
            actionResult = { status: "ERROR", failure_domain: "HARNESS", code: "SOURCE_SNAPSHOT_DRIFT", elapsed_seconds: 0, source_snapshot_verification: before };
          } else actionResult = await executeGate({
            repoRoot,
            sourceRepoRoot: repoRoot,
            sourceSnapshotRoot,
            sourceSnapshotManifestPath: sourceManifestPath,
            sourceSnapshotDigest: built.sourceSnapshot.digest,
            attemptRoot,
            options: executionOptions,
            client: built.client,
            track: plan.track,
            policies,
            changedFiles: built.changedFiles,
            identities: built.identities,
            eventWriter,
            resources,
            plan,
            planStage,
            runtimeProfile: gateRuntimeProfile,
            restoredCheckpoint,
          }, stage, gatePlan.id, gate);
          const after = verifySourceSnapshot(repoRoot, built.sourceSnapshot);
          if (after.status !== "PASS") actionResult = { ...actionResult, status: "ERROR", failure_domain: "HARNESS", code: "SOURCE_SNAPSHOT_DRIFT", source_snapshot_verification: after };
        } catch (error) {
          actionResult = { status: "ERROR", failure_domain: "HARNESS", code: "GATE_EXCEPTION", elapsed_seconds: 0, error: redactError(error) };
        }
        const claudeQuickContractGate = gate.kind === "node-test"
          && ["real.macos-claude-deepseek-methods", "real.macos-claude-deepseek-e2e"].includes(stage.id);
        if (claudeQuickContractGate) {
          actionResult = { ...actionResult, usage_complete: true, invocations: [] };
        } else {
          actionResult = applyHardCaps({
            result: actionResult,
            planStage,
            expectedModel: gateRuntimeProfile?.claude?.model ?? null,
          });
        }
        const contracted = applyGateEvidenceContract({ actionResult, gate, gatePlan, stage, attemptRoot });
        gateResults.push(writeGateReceipt({ attemptRoot, stage, gatePlan, actionResult: contracted.result, evidence: contracted.evidence, planStage }));
      }

      let status = functionalStageStatus(gateResults);
      let stageOperationFailure = null;
      let checkpoint = null;
      if (["PASS", "NOT_REQUIRED"].includes(status)) {
        try {
          const sealed = sealBoundaryCheckpoint({ attemptRoot, runId, track: plan.track, stage, planStage, parentCheckpointId });
          checkpoint = sealed.checkpoint;
          parentCheckpointId = sealed.parentCheckpointId;
        } catch (error) {
          status = "ERROR";
          stageOperationFailure = { code: "CHECKPOINT_SEAL_FAILED", failure_domain: "HARNESS", error: redactError(error) };
        }
      }
      let performance = { status: "NOT_MEASURED", baseline: null, consecutive_significant_regressions: 0, reason: null };
      if (status === "PASS") {
        performance = adjudicateStagePerformance({
          elapsedSeconds,
          samples: performanceSamples(built.history, stage.id, planStage.performance_identity, built.config.policy.performance.window),
          stage,
          effect: built.config.policy.tracks[plan.track].performance_effect,
          policy: built.config.policy.performance,
          priorConsecutiveSlow: priorConsecutiveSlow(built.history, stage.id, planStage.performance_identity),
        });
      }
      const result = writeExecutedStageReceipt({
        attemptRoot,
        stage,
        planStage,
        gateResults,
        status,
        stageOperationFailure,
        performance,
        checkpoint,
        restoredCheckpoint,
      });
      stageResults.push(result);
      if (!["PASS", "NOT_REQUIRED"].includes(result.status) || result.performance_status === "FAIL") {
        stopped = true;
        if (result.status === "ERROR") operationStatus = "ERROR";
      }
    }
  } catch (error) {
    operationStatus = "ERROR";
    stageResults.push({
      schema_version: 2,
      id: "orchestrator",
      kind: "framework",
      status: "ERROR",
      code: "ORCHESTRATOR_EXCEPTION",
      failure_domain: "HARNESS",
      error: redactError(error),
      elapsed_seconds: 0,
      performance_status: "NOT_MEASURED",
      gates: [],
    });
    try { eventWriter.write("run.failed", { data: { code: "ORCHESTRATOR_EXCEPTION" } }); } catch {}
  } finally {
    try {
      preFinalizationResourceReceipt = await resources.apply({ preserve: true });
      if (preFinalizationResourceReceipt.status !== "PASS") operationStatus = "ERROR";
      try {
        eventWriter.write("resources.quiesced", { data: { status: preFinalizationResourceReceipt.status, inspected_count: preFinalizationResourceReceipt.inspected?.length ?? 0, remaining_count: preFinalizationResourceReceipt.remaining?.length ?? 0 } });
      } catch {}
    } catch (error) {
      operationStatus = "ERROR";
      preFinalizationResourceReceipt = { schema_version: 2, status: "ERROR", policy: "PRESERVE", code: "PRE_FINALIZATION_RESOURCE_QUIESCENCE_FAILED", error: redactError(error), inspected: [], remaining: [] };
    }
    try { eventWriter.close(); } catch (error) {
      operationStatus = "ERROR";
      stageResults.push({ schema_version: 2, id: "evidence.events", kind: "framework", status: "ERROR", code: "EVENT_STREAM_CLOSE_FAILED", failure_domain: "HARNESS", error: redactError(error), elapsed_seconds: 0, performance_status: "NOT_MEASURED", gates: [] });
    }
    const worktreeVerification = verifySourceSnapshot(repoRoot, built.sourceSnapshot);
    const materializedVerification = sourceSnapshotSetupError
      ? { schema_version: 1, status: "ERROR", expected_digest: built.sourceSnapshot.digest, observed_digest: null, code: "SOURCE_SNAPSHOT_MATERIALIZATION_FAILED" }
      : verifyMaterializedSourceSnapshot(sourceSnapshotRoot, built.sourceSnapshot);
    sourceSnapshotVerification = {
      schema_version: 1,
      status: worktreeVerification.status === "PASS" && materializedVerification.status === "PASS" ? "PASS" : "FAIL",
      worktree: worktreeVerification,
      materialized: materializedVerification,
    };
    writeJsonSync(path.join(attemptRoot, "payload", "source", "source-snapshot-verification.json"), sourceSnapshotVerification);
    if (sourceSnapshotVerification.status !== "PASS") operationStatus = "ERROR";
    const scratch = path.join(attemptRoot, "scratch");
    try { removeTreeWritable(scratch, attemptRoot); } catch (error) {
      operationStatus = "ERROR";
      stageResults.push({ schema_version: 2, id: "evidence.scratch-cleanup", kind: "framework", status: "ERROR", code: "SCRATCH_CLEANUP_FAILED", failure_domain: "HARNESS", error: redactError(error), elapsed_seconds: 0, performance_status: "NOT_MEASURED", gates: [] });
    }
  }

  const candidate = buildRunCandidate({
    attemptRoot,
    runId,
    plan,
    stageResults,
    operationStatus,
    sourceSnapshotVerification,
    preFinalizationResourceReceipt,
  });
  const verdict = await finalizeAttempt({
    attemptRoot,
    candidate,
    policy: built.config.policy,
    config: built.config,
    knownSecrets: knownSecrets(process.env),
    resourcePolicy: (policy) => resources.apply(policy),
  });
  return { plan, verdict, attemptRoot, exitCode: verdict.exit_code };
}
