import assert from "node:assert/strict";
import crypto from "node:crypto";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { allowedEmptyEventFiles, auditExecutedStageUsage, createAttempt, finalizeAttempt, recoverStageAuditProgress, requiredEventFiles, verifyVerdict } from "../lib/evidence.mjs";
import { EventWriter } from "../lib/events.mjs";
import { TOKEN_USAGE_FORMULA, zeroUsage } from "../lib/usage.mjs";
import { canonicalJson, removeTreeWritable, sha256Bytes, sha256File, writeJsonSync } from "../lib/util.mjs";
import {
  environmentKeySummary,
  ISOLATED_AGENT_ENV_POLICY_VERSION,
  ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_ENFORCEMENT,
  ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY,
} from "../runtime-support/isolated-agent-env.mjs";
import {
  buildSkillGenerationIncompleteAuditRejectedReceipt,
  SKILL_GENERATION_TRACE_CODES,
  SKILL_GENERATION_TOOL_ATTEMPT_POLICY,
  SKILL_GENERATION_TRACE_SCHEMA_VERSION,
} from "../runtime-support/isolated-agent-tool-audit.mjs";
import { SKILL_GENERATION_RULE_IR } from "../runtime-support/skill-generation-rule-ir.mjs";

const TOOL_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const STATUS_POLICY = { pass: 0, pass_with_warnings: 0, fail: 1, blocked: 2, error: 3 };
const ZERO_USAGE = zeroUsage();

function passingSkillTraceAudit() {
  const requiredReads = [
    "workspace/inputs/wiki.md",
    "workspace/inputs/clarifications.md",
    "skill/references/generation-spec-v6-reference.md",
    "skill/references/verification-contract-v2-reference.md",
    "skill/references/checkpoints/01-begin-repeated-families-and-paths.md",
    "skill/references/checkpoints/02-begin-9-1-inventory.md",
    "skill/references/checkpoints/03-begin-9-2-witnesses.md",
    "skill/references/checkpoints/04-write-now.md",
  ];
  const outputOrdinal = requiredReads.length + 1;
  return {
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "PASS",
    workflow: "skill-generation",
    skill: "wiki-to-diagnosis-skill",
    tool_inventory: ["Skill", "Read", "StructuredOutput"],
    permission_mode: "dontAsk",
    permission_policy_sha256: "a".repeat(64),
    attempt_policy: SKILL_GENERATION_TOOL_ATTEMPT_POLICY,
    attempt_policy_sha256: crypto.createHash("sha256").update(JSON.stringify(SKILL_GENERATION_TOOL_ATTEMPT_POLICY)).digest("hex"),
    tool_sequence: [
      { ordinal: 0, tool: "Skill", outcome: "SUCCESS" },
      ...requiredReads.map((readPath, index) => ({ ordinal: index + 1, tool: "Read", outcome: "SUCCESS", path: readPath })),
      { ordinal: outputOrdinal, tool: "StructuredOutput", outcome: "SUCCESS" },
    ],
    accepted_validation_rejections: [],
    required_reads: requiredReads,
    observed_reads: requiredReads.map((readPath, index) => ({ ordinal: index + 1, path: readPath })),
    linked_references: [
      "skill/references/checkpoints/01-begin-repeated-families-and-paths.md",
      "skill/references/checkpoints/02-begin-9-1-inventory.md",
      "skill/references/checkpoints/03-begin-9-2-witnesses.md",
      "skill/references/checkpoints/04-write-now.md",
      "skill/references/generation-spec-v6-reference.md",
      "skill/references/verification-contract-v2-reference.md",
    ],
    ir_input: {
      ordinal: outputOrdinal,
      size_bytes: 3,
      sha256: "c".repeat(64),
    },
    compiler: {
      id: SKILL_GENERATION_RULE_IR.compiler_id,
      version: SKILL_GENERATION_RULE_IR.compiler_version,
      blueprint_schema_version: SKILL_GENERATION_RULE_IR.blueprint_schema_version,
      family_kind: SKILL_GENERATION_RULE_IR.family_kind,
      family_version: SKILL_GENERATION_RULE_IR.family_version,
    },
    output: {
      ordinal: outputOrdinal,
      path: "workspace/output/generation-spec.json",
      size_bytes: 3,
      sha256: "b".repeat(64),
    },
    terminal: { subtype: "success", is_error: false },
  };
}

function failedPartialSkillTraceAudit({ subtype = "error_max_turns", isError = true } = {}) {
  return {
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "FAIL",
    workflow: "skill-generation",
    code: "SKILL_TRACE_RESULT_NOT_SUCCESS",
    tool_sequence: [
      { ordinal: 0, tool: "Skill", outcome: "SUCCESS" },
      { ordinal: 1, tool: "Read", outcome: "SUCCESS", path: "workspace/inputs/wiki.md" },
      {
        ordinal: 2,
        tool: "StructuredOutput",
        outcome: "ERROR",
        size_bytes: 3,
        sha256: "c".repeat(64),
        diagnostic: { schema_version: 1, status: "INVALID_IR" },
      },
    ],
    terminal: { subtype, is_error: isError },
  };
}

function closeMinimalStream(attemptRoot, runId) {
  const writer = new EventWriter({ attemptRoot, runId, producerId: "orchestrator", producerType: "orchestrator" });
  writer.write("run.created", { data: { track: "dev" } });
  writer.close();
}

function writeExecutedStage(attemptRoot) {
  const stageId = "deterministic.full";
  const gateId = "det.unit";
  const gateRoot = path.join(attemptRoot, "payload", "stages", stageId, "gates", gateId);
  fs.mkdirSync(gateRoot, { recursive: true });
  const receiptPath = path.join(gateRoot, "gate-receipt.json");
  writeJsonSync(receiptPath, {
    schema_version: 2,
    stage_id: stageId,
    gate_id: gateId,
    gate_kind: "pytest",
    gate_identity: "gate-identity-a",
    definition_digest: "gate-definition-a",
    evidence_contract: null,
    runtime_profile: "python-test",
    runtime_profile_digest: "runtime-python-a",
    result_source: "EXECUTED",
    status: "PASS",
    code: null,
    failure_domain: null,
    elapsed_seconds: 1,
    usage: ZERO_USAGE,
    usage_complete: true,
    effective_caps: null,
    model_invocations: [],
    fresh_admission: null,
    evidence: [],
    execution: { exit_code: 0, signal: null, termination: null, stdout_path: null, stderr_path: null },
    assertions: { pytest: { executed: 1, passed: 1, skipped: 0 }, node_test: null, selection: null, adapter: null },
  });
  const gate = {
    id: gateId,
    kind: "pytest",
    status: "PASS",
    code: null,
    failure_domain: null,
    gate_identity: "gate-identity-a",
    definition_digest: "gate-definition-a",
    evidence_contract: null,
    runtime_profile: "python-test",
    runtime_profile_digest: "runtime-python-a",
    receipt_path: path.relative(attemptRoot, receiptPath).split(path.sep).join("/"),
    receipt_digest: sha256File(receiptPath),
    elapsed_seconds: 1,
    usage: ZERO_USAGE,
    usage_complete: true,
    effective_caps: null,
    model_invocations: [],
    fresh_admission: null,
    evidence: [],
  };
  const stageReceipt = {
    schema_version: 2,
    id: stageId,
    kind: "deterministic",
    status: "PASS",
    code: null,
    failure_domain: null,
    operation_failure: null,
    result_source: "EXECUTED",
    producer_identity: "producer-a",
    proof_identity: "proof-a",
    performance_identity: "performance-a",
    performance_status: "PASS",
    performance_reason: null,
    performance_baseline: null,
    consecutive_significant_regressions: 0,
    elapsed_seconds: 1,
    usage: ZERO_USAGE,
    gates: [gate],
    checkpoint: null,
    restored_checkpoint: null,
  };
  const stagePath = path.join(attemptRoot, "payload", "stages", stageId, "stage-receipt.json");
  writeJsonSync(stagePath, stageReceipt);
  return {
    ...stageReceipt,
    stage_receipt_path: path.relative(attemptRoot, stagePath).split(path.sep).join("/"),
    stage_receipt_digest: sha256File(stagePath),
  };
}

function writeReusedStage(attemptRoot, sourceRunId, sourceStageDigest) {
  const stageReceipt = {
    schema_version: 2,
    id: "deterministic.full",
    kind: "deterministic",
    status: "PASS",
    code: null,
    failure_domain: null,
    result_source: "REUSED",
    reused_from: { run_id: sourceRunId, source_stage_receipt_digest: sourceStageDigest },
    producer_identity: "producer-a",
    proof_identity: "proof-a",
    performance_identity: "performance-a",
    performance_status: "NOT_MEASURED",
    performance_baseline: null,
    elapsed_seconds: null,
    usage: ZERO_USAGE,
    gates: [],
    checkpoint: null,
  };
  const stagePath = path.join(attemptRoot, "payload", "stages", stageReceipt.id, "stage-receipt.json");
  fs.mkdirSync(path.dirname(stagePath), { recursive: true });
  writeJsonSync(stagePath, stageReceipt);
  return {
    ...stageReceipt,
    stage_receipt_path: path.relative(attemptRoot, stagePath).split(path.sep).join("/"),
    stage_receipt_digest: sha256File(stagePath),
  };
}

function writePlanAndCandidate(attemptRoot, runId, stage) {
  const proof = {
    id: "proof.deterministic-full",
    acceptance: "all",
    stages: [{ id: stage.id, status: stage.status }],
    proof_definition_digest: "proof-definition-a",
    status: "PASS",
  };
  const admission = { status: "ADMITTED", blockers: [], warnings: [] };
  const configDigests = {
    proofs: "config-proofs", stages: "config-stages", gates: "config-gates",
    identities: "config-identities", policy: "config-policy", runtimeProfiles: "config-runtime",
  };
  const sourceManifest = {
    schema_version: 1,
    algorithm: "git-visible-worktree-v1",
    digest: sha256Bytes(canonicalJson([])),
    file_count: 0,
    records: [],
  };
  const sourceSnapshot = {
    schema_version: 1,
    algorithm: sourceManifest.algorithm,
    status: "PRESENT",
    digest: sourceManifest.digest,
    file_count: 0,
  };
  const sourceVerification = {
    schema_version: 1,
    status: "PASS",
    worktree: { status: "PASS", expected_digest: sourceManifest.digest, observed_digest: sourceManifest.digest },
    materialized: { status: "PASS", expected_digest: sourceManifest.digest, observed_digest: sourceManifest.digest },
  };
  const planCore = {
    schema_version: 2,
    track: "dev",
    goal: "dev.default",
    client: "macos",
    runtime_profile: "release",
    runtime_profile_digest: "runtime-release-a",
    config_digests: configDigests,
    config_bundle_digest: "config-bundle-a",
    resume: "auto",
    source: { available: true, base_commit: "a".repeat(40), branch: "codex/test", worktree_clean: false, snapshot: sourceSnapshot, baseline: { source: "explicit", commit: "b".repeat(40) }, changed_files: [] },
    release_inputs: null,
    lineage: { root: "AUTO", initial_data_root: "TRACK_POLICY", checkpoint_reuse: "CONFIGURED_PER_STAGE" },
    proofs: [{ id: proof.id, acceptance: proof.acceptance, stages: [stage.id], proof_definition_digest: proof.proof_definition_digest }],
    stages: [{
      id: stage.id,
      producer_identity: stage.producer_identity,
      proof_identity: stage.proof_identity,
      performance_identity: stage.performance_identity,
      decision: stage.result_source === "REUSED" ? "REUSE" : "RUN",
      gates: [{ id: "det.unit", gate_identity: "gate-identity-a", definition_digest: "gate-definition-a", evidence_contract: null, required_evidence: [], runtime_profile: "python-test", runtime_profile_digest: "runtime-python-a" }],
    }],
    admission,
    retry: { recommendation: "RUN", reason: null, previous_run_id: null, stage_id: null, previous_code: null },
    intent: { reason: null, hypothesis: null, expected_evidence: null },
    budget: { estimated_tokens: 0, sum_of_per_invocation_caps_usd: 0, cumulative_spending_cap: null, per_invocation_hard_enforced: true },
    policies: { status: STATUS_POLICY },
  };
  const planFingerprint = sha256Bytes(canonicalJson(planCore));
  writeJsonSync(path.join(attemptRoot, "payload", "run-plan.json"), {
    ...planCore,
    plan_fingerprint: planFingerprint,
    run_id: runId,
    created_at_utc: "2026-08-10T00:00:00.000Z",
  });
  writeJsonSync(path.join(attemptRoot, "payload", "source", "source-snapshot.json"), sourceManifest);
  writeJsonSync(path.join(attemptRoot, "payload", "source", "source-snapshot-verification.json"), sourceVerification);
  const gates = stage.gates.map((gate) => ({ stage_id: stage.id, ...gate }));
  const candidate = {
    schema_version: 2,
    run_id: runId,
    track: "dev",
    goal: "dev.default",
    functional_status: "PASS",
    performance_status: stage.result_source === "REUSED" ? "NOT_RUN" : "PASS",
    operation_status: "PASS",
    failure_domain: null,
    failure_fingerprint: null,
    proofs: [proof],
    stages: [stage],
    gates,
    source: { base_commit: "a".repeat(40), branch: "codex/test", worktree_clean_at_start: false, snapshot: sourceSnapshot, baseline: { source: "explicit", commit: "b".repeat(40) }, verification: sourceVerification },
    config_digests: configDigests,
    config_bundle_digest: "config-bundle-a",
    runtime_profile: "release",
    runtime_profile_digest: "runtime-release-a",
    plan_fingerprint: planFingerprint,
    policy_digest: "config-policy",
    status_policy: STATUS_POLICY,
    lineage: { root: "AUTO" },
    admission,
    pre_finalization_resource_receipt: null,
    usage: ZERO_USAGE,
    candidate_input_digest: "pending",
  };
  candidate.candidate_input_digest = sha256Bytes(canonicalJson({
    run_id: runId,
    plan_fingerprint: planFingerprint,
    proofs: candidate.proofs,
    stages: [{ id: stage.id, digest: stage.stage_receipt_digest }],
  }));
  return candidate;
}

async function createFinalized(evidenceRoot, runId, { reusedFrom = null, resourceStatus = "PASS", mutateBeforeFinalize = null } = {}) {
  const attemptRoot = createAttempt({ evidenceRoot, runId });
  closeMinimalStream(attemptRoot, runId);
  const stage = reusedFrom
    ? writeReusedStage(attemptRoot, reusedFrom.runId, reusedFrom.stageDigest)
    : writeExecutedStage(attemptRoot);
  const candidate = writePlanAndCandidate(attemptRoot, runId, stage);
  mutateBeforeFinalize?.({ attemptRoot, candidate, stage });
  const verdict = await finalizeAttempt({
    attemptRoot,
    candidate,
    policy: { evidence: { scanner_version: "test-flow-secret-scan-v2", event_visibility_seconds: 0 } },
    resourcePolicy: async ({ preserve }) => ({
      schema_version: 2,
      status: resourceStatus,
      policy: preserve ? "PRESERVE" : "DELETE",
      ...(resourceStatus === "PASS" ? {} : { code: "CLEANUP_PARTIAL" }),
      inspected: [],
      remaining: resourceStatus === "PASS" ? [] : [{ kind: "volume", name: "kept" }],
    }),
  });
  return { attemptRoot, verdict, stage };
}

test("a fully sealed v2 candidate verifies and binds plan, Proof, Stage and Gate receipts", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-evidence-valid-"));
  try {
    const result = await createFinalized(root, "run-20260810T000000Z-aaaaaaaa");
    assert.equal(result.verdict.overall, "PASS");
    assert.equal(result.verdict.verification_status, "PASS");
    assert.equal(result.verdict.evidence_reusable, true);
    assert.equal(result.verdict.proofs[0].status, "PASS");
    assert.equal(verifyVerdict(result.attemptRoot).status, "PASS");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("a failed planned invocation requires a sealed audit for a parse-complete terminal-less Skill timeout", () => {
  const caps = {
    max_turns: 12,
    max_total_tokens: 1000000,
    max_budget_usd: 10,
    hard_timeout_seconds: 1800,
  };
  const invocation = {
    schema_version: 3,
    invocation_id: "isolated-agent:timeout",
    class: "isolated-agent",
    workflow: "skill-generation",
    environment_policy: {
      schema_version: 1,
      version: ISOLATED_AGENT_ENV_POLICY_VERSION,
      provider_auth_source: "audited-settings-file",
      session_credentials: "NONE",
      inbound: environmentKeySummary({ PATH: "/bin" }),
      claude_process: environmentKeySummary({ PATH: "/bin", [ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY]: "2" }),
    },
    tool_trace_audit: null,
    effective_model: "test-model",
    effective_caps: caps,
    usage_complete: false,
    usage: null,
    terminal: null,
    turns: null,
    stream: {
      schema_version: 1,
      event_count: 22,
      parsed_event_count: 22,
      init_count: 1,
      result_count: 0,
      last_event_type: "assistant",
      complete: false,
    },
    wrapper_outcome: { schema_version: 1, status: "FAIL", code: "WRAPPER_MODEL_TIMEOUT" },
    hard_cap_enforcement: {
      turns: "claude-cli",
      cost_usd: "claude-cli",
      hard_timeout_seconds: "wrapper-process-watchdog",
      total_tokens: `terminal-usage-postcondition:${TOKEN_USAGE_FORMULA}`,
      structured_output_retries: ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_ENFORCEMENT,
    },
    timed_out: true,
    process: { exit_code: null, signal: "SIGTERM", wrapper_exit_code: 124 },
  };
  const planned = { invocation_caps: [{ class: "isolated-agent", min_count: 1, max_count: 1, caps }] };
  const plan = { release_inputs: { settings: { model: "test-model" } } };
  const failedStage = {
    id: "real.skill-generation",
    status: "FAIL",
    usage: ZERO_USAGE,
    gates: [{
      id: "real.agent.skill-generation",
      status: "FAIL",
      usage: ZERO_USAGE,
      usage_complete: false,
      model_invocations: [invocation],
    }],
  };
  const missingAuditFailures = [];
  auditExecutedStageUsage(plan, planned, failedStage, missingAuditFailures);
  assert.deepEqual(missingAuditFailures, [{ code: "MODEL_USAGE_INVALID", stage_id: "real.skill-generation" }]);

  invocation.tool_trace_audit = buildSkillGenerationIncompleteAuditRejectedReceipt(
    SKILL_GENERATION_TRACE_CODES.PHASE_SEQUENCE_INVALID,
    structuredClone(invocation.stream),
  );
  const failures = [];
  auditExecutedStageUsage(plan, planned, failedStage, failures);
  assert.deepEqual(failures, []);
  assert.doesNotMatch(JSON.stringify(invocation.tool_trace_audit), /content|thinking|raw|file_path|message|details/iu);

  const streamInvalidStage = structuredClone(failedStage);
  const streamInvalidInvocation = streamInvalidStage.gates[0].model_invocations[0];
  streamInvalidInvocation.wrapper_outcome.code = "WRAPPER_MODEL_STREAM_INVALID";
  streamInvalidInvocation.timed_out = false;
  streamInvalidInvocation.process = { exit_code: 1, signal: null, wrapper_exit_code: 1 };
  const streamInvalidFailures = [];
  auditExecutedStageUsage(plan, planned, streamInvalidStage, streamInvalidFailures);
  assert.deepEqual(streamInvalidFailures, []);

  const nonDiagnosableStage = structuredClone(streamInvalidStage);
  const nonDiagnosableInvocation = nonDiagnosableStage.gates[0].model_invocations[0];
  nonDiagnosableInvocation.tool_trace_audit = null;
  nonDiagnosableInvocation.stream.parsed_event_count -= 1;
  const nonDiagnosableFailures = [];
  auditExecutedStageUsage(plan, planned, nonDiagnosableStage, nonDiagnosableFailures);
  assert.deepEqual(nonDiagnosableFailures, []);

  const unknownEventStage = structuredClone(streamInvalidStage);
  const unknownEventInvocation = unknownEventStage.gates[0].model_invocations[0];
  unknownEventInvocation.tool_trace_audit = null;
  unknownEventInvocation.stream.last_event_type = null;
  const unknownEventFailures = [];
  auditExecutedStageUsage(plan, planned, unknownEventStage, unknownEventFailures);
  assert.deepEqual(unknownEventFailures, [{ code: "MODEL_USAGE_INVALID", stage_id: "real.skill-generation" }]);

  for (const [name, mutate] of [
    ["rejection raw field injected", (candidate) => { candidate.tool_trace_audit.raw = "must-not-pass"; }],
    ["rejection stream changed", (candidate) => { candidate.tool_trace_audit.stream.event_count += 1; }],
    ["terminal-less streams cannot end in result", (candidate) => {
      candidate.stream.last_event_type = "result";
      candidate.tool_trace_audit.stream.last_event_type = "result";
    }],
    ["rejection audit code unknown", (candidate) => { candidate.tool_trace_audit.audit_code = "SKILL_TRACE_UNKNOWN"; }],
  ]) {
    const tamperedStage = structuredClone(failedStage);
    mutate(tamperedStage.gates[0].model_invocations[0]);
    const tamperedFailures = [];
    auditExecutedStageUsage(plan, planned, tamperedStage, tamperedFailures);
    assert.deepEqual(tamperedFailures, [{ code: "MODEL_USAGE_INVALID", stage_id: "real.skill-generation" }], name);
  }

  const prefixStage = structuredClone(failedStage);
  const prefixInvocation = prefixStage.gates[0].model_invocations[0];
  prefixInvocation.tool_trace_audit = {
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "FAIL",
    workflow: "skill-generation",
    code: SKILL_GENERATION_TRACE_CODES.INCOMPLETE_PREFIX,
    stream_state: "TERMINAL_MISSING",
    tool_sequence: [{ ordinal: 0, tool: "Skill", outcome: "PENDING" }],
    stream: structuredClone(prefixInvocation.stream),
  };
  const prefixFailures = [];
  auditExecutedStageUsage(plan, planned, prefixStage, prefixFailures);
  assert.deepEqual(prefixFailures, []);
  assert.doesNotMatch(JSON.stringify(prefixInvocation.tool_trace_audit), /content|thinking|raw|file_path/iu);

  for (const [name, mutate] of [
    ["prefix stream count changed", (candidate) => { candidate.tool_trace_audit.stream.event_count += 1; }],
    ["prefix raw field injected", (candidate) => { candidate.tool_trace_audit.raw = "must-not-pass"; }],
    ["prefix attached to usage failure", (candidate) => {
      candidate.wrapper_outcome.code = "WRAPPER_MODEL_USAGE_INVALID";
      candidate.timed_out = false;
      candidate.process = { exit_code: 1, signal: null, wrapper_exit_code: 1 };
      candidate.stream.complete = true;
      candidate.tool_trace_audit.stream.complete = true;
    }],
  ]) {
    const tamperedStage = structuredClone(prefixStage);
    mutate(tamperedStage.gates[0].model_invocations[0]);
    const tamperedFailures = [];
    auditExecutedStageUsage(plan, planned, tamperedStage, tamperedFailures);
    assert.deepEqual(tamperedFailures, [{ code: "MODEL_USAGE_INVALID", stage_id: "real.skill-generation" }], name);
  }

  const overCountFailures = [];
  auditExecutedStageUsage(plan, planned, {
    ...failedStage,
    gates: failedStage.gates.map((gate) => ({
      ...gate,
      model_invocations: [
        invocation,
        { ...invocation, invocation_id: "isolated-agent:timeout-duplicate" },
      ],
    })),
  }, overCountFailures);
  assert.deepEqual(overCountFailures, [{
    code: "MODEL_INVOCATION_COUNT_MISMATCH",
    stage_id: "real.skill-generation",
    class: "isolated-agent",
  }]);

  const passingFailures = [];
  auditExecutedStageUsage(plan, planned, {
    ...failedStage,
    status: "PASS",
    gates: failedStage.gates.map((gate) => ({ ...gate, status: "PASS" })),
  }, passingFailures);
  assert.deepEqual(passingFailures, [{
    code: "MODEL_USAGE_INCOMPLETE",
    stage_id: "real.skill-generation",
    gate_id: "real.agent.skill-generation",
  }]);

  const fabricatedFailures = [];
  auditExecutedStageUsage(plan, planned, {
    ...failedStage,
    gates: failedStage.gates.map((gate) => ({
      ...gate,
      model_invocations: [{ ...invocation, usage: ZERO_USAGE }],
    })),
  }, fabricatedFailures);
  assert.deepEqual(fabricatedFailures, [{ code: "MODEL_USAGE_INVALID", stage_id: "real.skill-generation" }]);

  for (const [name, mutate] of [
    ["timeout flag cleared", (candidate) => { candidate.timed_out = false; }],
    ["timeout wrapper exit changed", (candidate) => { candidate.process.wrapper_exit_code = 1; }],
    ["timeout stream marked complete", (candidate) => { candidate.stream.complete = true; }],
    ["timeout terminal fabricated", (candidate) => {
      candidate.terminal = { subtype: "error_max_turns", is_error: true };
    }],
    ["timeout partial trace fabricated", (candidate) => {
      candidate.tool_trace_audit = failedPartialSkillTraceAudit();
    }],
    ["timeout process exit and signal conflict", (candidate) => {
      candidate.process.exit_code = 1;
    }],
    ["timeout exit zero without a termination signal", (candidate) => {
      candidate.process = { exit_code: 0, signal: null, wrapper_exit_code: 124 };
    }],
    ["timeout untrusted termination signal", (candidate) => {
      candidate.process = { exit_code: null, signal: "SIGUSR1", wrapper_exit_code: 124 };
    }],
  ]) {
    const invalidStage = structuredClone(failedStage);
    mutate(invalidStage.gates[0].model_invocations[0]);
    const invalidFailures = [];
    auditExecutedStageUsage(plan, planned, invalidStage, invalidFailures);
    assert.deepEqual(
      invalidFailures,
      [{ code: "MODEL_USAGE_INVALID", stage_id: "real.skill-generation" }],
      name,
    );
  }

  const completeUsage = {
    schema_version: 1,
    input_tokens: 10,
    output_tokens: 20,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    total_tokens: 30,
    cost_usd: 0.01,
  };
  const partialInvocation = {
    ...invocation,
    invocation_id: "isolated-agent:max-turns",
    tool_trace_audit: failedPartialSkillTraceAudit(),
    usage_complete: true,
    usage: completeUsage,
    terminal: { subtype: "error_max_turns", is_error: true },
    turns: 13,
    stream: {
      schema_version: 1,
      event_count: 22,
      parsed_event_count: 22,
      init_count: 1,
      result_count: 1,
      last_event_type: "result",
      complete: true,
    },
    wrapper_outcome: { schema_version: 1, status: "FAIL", code: "WRAPPER_MODEL_CAP_EXCEEDED" },
    timed_out: false,
    process: { exit_code: 1, signal: null, wrapper_exit_code: 1 },
  };
  const partialStage = {
    ...failedStage,
    usage: completeUsage,
    gates: [{
      ...failedStage.gates[0],
      usage: completeUsage,
      usage_complete: true,
      model_invocations: [partialInvocation],
    }],
  };
  const partialFailures = [];
  auditExecutedStageUsage(plan, planned, partialStage, partialFailures);
  assert.deepEqual(partialFailures, []);
  assert.doesNotMatch(JSON.stringify(partialInvocation.tool_trace_audit), /content|thinking|raw|file_path/iu);

  const injectedPartial = structuredClone(partialStage);
  injectedPartial.gates[0].model_invocations[0].tool_trace_audit.tool_sequence[0].content = "must-not-pass";
  const injectedFailures = [];
  auditExecutedStageUsage(plan, planned, injectedPartial, injectedFailures);
  assert.deepEqual(injectedFailures, [{
    code: "MODEL_TOOL_TRACE_AUDIT_INVALID",
    stage_id: "real.skill-generation",
    invocation_id: "isolated-agent:max-turns",
  }]);

  const diagnosticTamper = structuredClone(partialStage);
  diagnosticTamper.gates[0].model_invocations[0].tool_trace_audit.tool_sequence.at(-1).diagnostic.raw = "must-not-pass";
  const diagnosticFailures = [];
  auditExecutedStageUsage(plan, planned, diagnosticTamper, diagnosticFailures);
  assert.deepEqual(diagnosticFailures, [{
    code: "MODEL_TOOL_TRACE_AUDIT_INVALID",
    stage_id: "real.skill-generation",
    invocation_id: "isolated-agent:max-turns",
  }]);

  const unknownCompletedAudit = structuredClone(partialStage);
  const unknownInvocation = unknownCompletedAudit.gates[0].model_invocations[0];
  unknownInvocation.tool_trace_audit = {
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "FAIL",
    workflow: "skill-generation",
    code: "SKILL_TRACE_UNKNOWN",
  };
  unknownInvocation.wrapper_outcome.code = "WRAPPER_SKILL_TRACE_INVALID";
  unknownInvocation.terminal = { subtype: "success", is_error: false };
  unknownInvocation.turns = 1;
  unknownInvocation.process = { exit_code: 0, signal: null, wrapper_exit_code: 1 };
  const unknownFailures = [];
  auditExecutedStageUsage(plan, planned, unknownCompletedAudit, unknownFailures);
  assert.deepEqual(unknownFailures, [{
    code: "MODEL_TOOL_TRACE_AUDIT_INVALID",
    stage_id: "real.skill-generation",
    invocation_id: "isolated-agent:max-turns",
  }]);

  const mismatchedTerminal = structuredClone(partialStage);
  mismatchedTerminal.gates[0].model_invocations[0].tool_trace_audit.terminal.subtype = "error_max_budget_usd";
  const mismatchFailures = [];
  auditExecutedStageUsage(plan, planned, mismatchedTerminal, mismatchFailures);
  assert.deepEqual(mismatchFailures, [{
    code: "MODEL_TOOL_TRACE_AUDIT_INVALID",
    stage_id: "real.skill-generation",
    invocation_id: "isolated-agent:max-turns",
  }]);

  for (const [name, mutate] of [
    ["complete failure marked timed out", (candidate) => { candidate.timed_out = true; }],
    ["complete failure stream truncated", (candidate) => { candidate.stream.complete = false; }],
    ["complete failure stream count mismatch", (candidate) => { candidate.stream.parsed_event_count -= 1; }],
    ["complete failure wrapper exit mismatch", (candidate) => { candidate.process.wrapper_exit_code = 0; }],
    ["complete failure process exit and signal conflict", (candidate) => { candidate.process.signal = "SIGTERM"; }],
    ["complete failure wrapper code mismatch", (candidate) => {
      candidate.wrapper_outcome.code = "WRAPPER_SKILL_TRACE_INVALID";
    }],
    ["complete failure terminal no longer explains failure", (candidate) => {
      candidate.terminal = { subtype: "success", is_error: false };
      candidate.turns = 12;
    }],
    ["complete failure model removed", (candidate) => { candidate.effective_model = null; }],
  ]) {
    const invalidStage = structuredClone(partialStage);
    mutate(invalidStage.gates[0].model_invocations[0]);
    const invalidFailures = [];
    auditExecutedStageUsage(plan, planned, invalidStage, invalidFailures);
    assert.deepEqual(invalidFailures, [{
      code: "MODEL_HARD_CAP_RECEIPT_MISMATCH",
      stage_id: "real.skill-generation",
      invocation_id: "isolated-agent:max-turns",
    }], name);
  }
});

test("a passing skill-generation invocation must retain a valid trace audit receipt", () => {
  const caps = {
    max_turns: 12,
    max_total_tokens: 1000000,
    max_budget_usd: 10,
    hard_timeout_seconds: 1800,
  };
  const usage = {
    schema_version: 1,
    input_tokens: 10,
    output_tokens: 20,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    total_tokens: 30,
    cost_usd: 0.01,
  };
  const invocation = {
    schema_version: 3,
    invocation_id: "isolated-agent:skill-generation",
    class: "isolated-agent",
    workflow: "skill-generation",
    environment_policy: {
      schema_version: 1,
      version: ISOLATED_AGENT_ENV_POLICY_VERSION,
      provider_auth_source: "audited-settings-file",
      session_credentials: "NONE",
      inbound: environmentKeySummary({ PATH: "/bin" }),
      claude_process: environmentKeySummary({ PATH: "/bin", [ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY]: "2" }),
    },
    tool_trace_audit: passingSkillTraceAudit(),
    effective_model: "test-model",
    effective_caps: caps,
    usage_complete: true,
    usage,
    terminal: { subtype: "success", is_error: false },
    turns: 1,
    stream: {
      schema_version: 1,
      event_count: 10,
      parsed_event_count: 10,
      init_count: 1,
      result_count: 1,
      last_event_type: "result",
      complete: true,
    },
    wrapper_outcome: { schema_version: 1, status: "PASS", code: null },
    hard_cap_enforcement: {
      turns: "claude-cli",
      cost_usd: "claude-cli",
      hard_timeout_seconds: "wrapper-process-watchdog",
      total_tokens: `terminal-usage-postcondition:${TOKEN_USAGE_FORMULA}`,
      structured_output_retries: ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_ENFORCEMENT,
    },
    timed_out: false,
    process: { exit_code: 0, signal: null, wrapper_exit_code: 0 },
  };
  const planned = { invocation_caps: [{ class: "isolated-agent", min_count: 1, max_count: 1, caps }] };
  const plan = { release_inputs: { settings: { model: "test-model" } } };
  const stage = {
    id: "real.skill-generation",
    status: "PASS",
    usage,
    gates: [{
      id: "real.agent.skill-generation",
      status: "PASS",
      usage,
      usage_complete: true,
      model_invocations: [invocation],
    }],
  };
  const failures = [];
  auditExecutedStageUsage(plan, planned, stage, failures);
  assert.deepEqual(failures, []);

  for (const [name, mutate] of [
    ["timed out", (candidate) => { candidate.timed_out = true; }],
    ["stream incomplete", (candidate) => { candidate.stream.complete = false; }],
    ["stream event count mismatch", (candidate) => { candidate.stream.event_count += 1; }],
    ["stream init count mismatch", (candidate) => { candidate.stream.init_count = 0; }],
    ["stream result count mismatch", (candidate) => { candidate.stream.result_count = 0; }],
    ["stream last event mismatch", (candidate) => { candidate.stream.last_event_type = "assistant"; }],
    ["child exit nonzero", (candidate) => { candidate.process.exit_code = 1; }],
    ["child exit and signal conflict", (candidate) => { candidate.process.signal = "SIGTERM"; }],
    ["wrapper exit nonzero", (candidate) => { candidate.process.wrapper_exit_code = 1; }],
    ["terminal subtype failed", (candidate) => { candidate.terminal.subtype = "error_max_turns"; }],
    ["terminal is_error failed", (candidate) => { candidate.terminal.is_error = true; }],
    ["wrapper outcome failed", (candidate) => {
      candidate.wrapper_outcome = {
        schema_version: 1,
        status: "FAIL",
        code: "WRAPPER_MODEL_TERMINAL_INVALID",
      };
    }],
    ["structured retry seal changed", (candidate) => {
      candidate.hard_cap_enforcement.structured_output_retries = "unsealed";
    }],
    ["structured retry child key missing", (candidate) => {
      candidate.environment_policy.claude_process = environmentKeySummary({ PATH: "/bin" });
    }],
    ["unexpected raw field", (candidate) => { candidate.raw = "/private/absolute/path"; }],
  ]) {
    const invalidStage = structuredClone(stage);
    mutate(invalidStage.gates[0].model_invocations[0]);
    const invalidFailures = [];
    auditExecutedStageUsage(plan, planned, invalidStage, invalidFailures);
    assert.deepEqual(invalidFailures, [{
      code: "MODEL_HARD_CAP_RECEIPT_MISMATCH",
      stage_id: "real.skill-generation",
      invocation_id: "isolated-agent:skill-generation",
    }], name);
  }

  const zeroTokenStage = structuredClone(stage);
  zeroTokenStage.usage = structuredClone(ZERO_USAGE);
  zeroTokenStage.gates[0].usage = structuredClone(ZERO_USAGE);
  zeroTokenStage.gates[0].model_invocations[0].usage = structuredClone(ZERO_USAGE);
  const zeroTokenFailures = [];
  auditExecutedStageUsage(plan, planned, zeroTokenStage, zeroTokenFailures);
  assert.deepEqual(zeroTokenFailures, [{
    code: "MODEL_HARD_CAP_RECEIPT_MISMATCH",
    stage_id: "real.skill-generation",
    invocation_id: "isolated-agent:skill-generation",
  }]);

  const tampered = structuredClone(stage);
  tampered.gates[0].model_invocations[0].tool_trace_audit.output.sha256 = "not-a-digest";
  const tamperedFailures = [];
  auditExecutedStageUsage(plan, planned, tampered, tamperedFailures);
  assert.deepEqual(tamperedFailures, [{
    code: "MODEL_TOOL_TRACE_AUDIT_INVALID",
    stage_id: "real.skill-generation",
    invocation_id: "isolated-agent:skill-generation",
  }]);

  const disguisedAsJob = structuredClone(stage);
  disguisedAsJob.gates[0].model_invocations[0].workflow = "job";
  disguisedAsJob.gates[0].model_invocations[0].tool_trace_audit = null;
  const disguisedFailures = [];
  auditExecutedStageUsage(plan, planned, disguisedAsJob, disguisedFailures);
  assert.deepEqual(disguisedFailures, [{
    code: "MODEL_TOOL_TRACE_AUDIT_INVALID",
    stage_id: "real.skill-generation",
    invocation_id: "isolated-agent:skill-generation",
  }]);
});

test("cleanup failure commits overall ERROR and cannot remain reusable", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-evidence-cleanup-"));
  try {
    const result = await createFinalized(root, "run-20260810T000001Z-bbbbbbbb", { resourceStatus: "ERROR" });
    assert.equal(result.verdict.functional_status, "PASS");
    assert.equal(result.verdict.operation_status, "ERROR");
    assert.equal(result.verdict.overall, "ERROR");
    assert.equal(result.verdict.evidence_reusable, false);
    assert.equal(verifyVerdict(result.attemptRoot).status, "PASS");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("secret evidence is preserved but never reusable", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-secret-"));
  try {
    let preserved = false;
    const attemptRoot = createAttempt({ evidenceRoot: root, runId: "run-20260810T000002Z-cccccccc" });
    closeMinimalStream(attemptRoot, path.basename(attemptRoot));
    const stage = writeExecutedStage(attemptRoot);
    const candidate = writePlanAndCandidate(attemptRoot, path.basename(attemptRoot), stage);
    fs.writeFileSync(path.join(attemptRoot, "payload", "logs", "leak.log"), "sk-ant-abcdefghijklmnopqrstuv\n");
    const verdict = await finalizeAttempt({
      attemptRoot,
      candidate,
      resourcePolicy: async ({ preserve }) => {
        preserved = preserve;
        return { schema_version: 2, status: "PASS", policy: "PRESERVE", inspected: [], remaining: [] };
      },
    });
    assert.equal(preserved, true);
    assert.equal(verdict.overall, "ERROR");
    assert.equal(verdict.failure_domain, "SECURITY");
    assert.equal(verdict.evidence_reusable, false);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("payload and verdict-only tampering are both detected", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-tamper-"));
  try {
    const first = await createFinalized(root, "run-20260810T000003Z-dddddddd");
    fs.appendFileSync(path.join(first.attemptRoot, "payload", "candidate-result.json"), " ");
    assert.equal(verifyVerdict(first.attemptRoot).status, "INVALID");

    for (const [index, mutate] of [
      (value) => { value.overall = "FAIL"; },
      (value) => { value.evidence_reusable = false; },
      (value) => { value.stages[0].producer_identity = "tampered"; },
      (value) => { value.source.snapshot.digest = "f".repeat(64); },
    ].entries()) {
      const item = await createFinalized(root, `run-20260810T00000${4 + index}Z-${String(index + 1).repeat(8)}`);
      const verdictPath = path.join(item.attemptRoot, "verdict.json");
      const value = JSON.parse(fs.readFileSync(verdictPath, "utf8"));
      mutate(value);
      fs.writeFileSync(verdictPath, canonicalJson(value), "utf8");
      assert.equal(verifyVerdict(item.attemptRoot).status, "INVALID");
    }
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("an incomplete event stream produces a verifiable ERROR verdict instead of crashing finalization", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-event-error-"));
  try {
    const result = await createFinalized(root, "run-20260810T000010Z-eeeeeeee", {
      mutateBeforeFinalize: ({ attemptRoot }) => fs.appendFileSync(path.join(attemptRoot, "payload", "events", "orchestrator.ndjson"), "{"),
    });
    assert.equal(result.verdict.overall, "ERROR");
    assert.equal(result.verdict.failure_domain, "HARNESS");
    assert.equal(result.verdict.verification_status, "FAIL");
    assert.equal(verifyVerdict(result.attemptRoot).status, "PASS");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("missing verdict is UNFINALIZED, never a successful attempt", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-unfinalized-"));
  try {
    const attemptRoot = createAttempt({ evidenceRoot: root, runId: "run-20260810T000011Z-ffffffff" });
    assert.deepEqual(verifyVerdict(attemptRoot), { status: "UNFINALIZED", verdict: null });
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("reuse points only to the original executed receipt and breaks if the source disappears", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-reuse-source-"));
  try {
    const source = await createFinalized(root, "run-20260810T000012Z-aaaabbbb");
    const derived = await createFinalized(root, "run-20260810T000013Z-ccccdddd", {
      reusedFrom: { runId: source.verdict.run_id, stageDigest: source.stage.stage_receipt_digest },
    });
    assert.equal(verifyVerdict(derived.attemptRoot).status, "PASS");

    const chained = await createFinalized(root, "run-20260810T000014Z-eeeeffff", {
      reusedFrom: { runId: derived.verdict.run_id, stageDigest: derived.stage.stage_receipt_digest },
    });
    assert.equal(verifyVerdict(chained.attemptRoot).reason, "REUSE_SOURCE_STAGE_INVALID");

    fs.rmSync(source.attemptRoot, { recursive: true, force: true });
    assert.equal(verifyVerdict(derived.attemptRoot).reason, "REUSE_SOURCE_INVALID");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("prune refuses to delete a source run that a valid derived verdict references", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-prune-source-"));
  try {
    const source = await createFinalized(root, "run-20260810T000015Z-1234abcd");
    await createFinalized(root, "run-20260810T000016Z-5678efab", {
      reusedFrom: { runId: source.verdict.run_id, stageDigest: source.stage.stage_receipt_digest },
    });
    const command = spawnSync(process.execPath, [path.join(TOOL_ROOT, "evidence.mjs"), "prune", "--evidence-root", root, "--run-id", source.verdict.run_id, "--execute"], { encoding: "utf8" });
    assert.equal(command.status, 3);
    assert.match(command.stderr, /PRUNE_REFERENCED_SOURCE/);
    assert.equal(fs.existsSync(source.attemptRoot), true);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("evidence report filters exact run ids and never labels an invalid verdict reusable", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-report-validity-"));
  try {
    await createFinalized(root, "run-20260810T000017Z-1111aaaa");
    const invalid = await createFinalized(root, "run-20260810T000018Z-2222bbbb");
    const verdictPath = path.join(invalid.attemptRoot, "verdict.json");
    const verdict = JSON.parse(fs.readFileSync(verdictPath, "utf8"));
    verdict.overall = "FAIL";
    fs.writeFileSync(verdictPath, canonicalJson(verdict), "utf8");

    const command = spawnSync(process.execPath, [path.join(TOOL_ROOT, "evidence.mjs"), "report", "--evidence-root", root, "--run-id", verdict.run_id], { encoding: "utf8" });
    assert.equal(command.status, 0, command.stderr);
    const report = JSON.parse(command.stdout);
    assert.equal(report.attempt_count, 1);
    assert.deepEqual(report.attempts.map((item) => item.run_id), [verdict.run_id]);
    assert.equal(report.attempts[0].verification_status, "INVALID");
    assert.equal(report.attempts[0].evidence_reusable, false);
    assert.equal(report.attempts[0].retention, "MANUAL_REVIEW");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("event requirements are derived from executed Gate evidence contracts", () => {
  assert.deepEqual(requiredEventFiles([{ id: "det.unit", status: "PASS", result_source: "EXECUTED" }]), ["orchestrator.ndjson"]);
  const routeContract = { id: "cross-job-tool-call-v2", event_stream: { instance: "route", pass_requires: ["diagnostics", "journey"], pass_allows_empty: [], failure_allows_empty: ["journey"] } };
  const routeGate = { stage_id: "journey.cross-job.route", id: "journey.route", evidence_contract: routeContract, status: "PASS", result_source: "EXECUTED" };
  assert.deepEqual(requiredEventFiles([routeGate]), [
    "orchestrator.ndjson",
    "parts/service-linux.route.diagnostics.ndjson",
    "parts/service-linux.route.journey.ndjson",
  ]);
  assert.deepEqual(allowedEmptyEventFiles([{ ...routeGate, status: "FAIL" }]), ["parts/service-linux.route.journey.ndjson"]);
  assert.deepEqual(requiredEventFiles([{ ...routeGate, status: "FAIL" }]), ["orchestrator.ndjson"]);
  const environmentGate = { stage_id: "journey.cross-job.environment", id: "journey.environment", evidence_contract: { id: "cross-job-environment-v2", event_stream: { ...routeContract.event_stream, pass_allows_empty: ["journey"] } }, status: "PASS", result_source: "EXECUTED" };
  assert.deepEqual(allowedEmptyEventFiles([environmentGate]), ["parts/service-linux.route.journey.ndjson"]);
  assert.deepEqual(allowedEmptyEventFiles([environmentGate, routeGate]), []);
});

test("failed adapter progress recovers completed calls and authoritative usage", () => {
  const attemptRoot = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-stage-progress-"));
  try {
    const stageId = "journey.cross-job.route";
    const stageRoot = path.join(attemptRoot, "payload", "stages", stageId);
    fs.mkdirSync(stageRoot, { recursive: true });
    writeJsonSync(path.join(stageRoot, "phase1.authoritative.json"), {
      records: [{ tool_name: "problem_locator_create_case" }, { tool_name: "problem_locator_get_case" }],
      usage: {
        schema_version: 1,
        input_tokens: 100,
        output_tokens: 20,
        cache_creation_input_tokens: 30,
        cache_read_input_tokens: 40,
        total_tokens: 190,
        cost_usd: 0.125,
      },
    });
    writeJsonSync(path.join(attemptRoot, "payload", "service-route-supervisor.json"), { status: "PASS" });
    const writer = new EventWriter({ attemptRoot, runId: "run-progress", producerId: "service-linux-route-diagnostics", producerType: "service" });
    writer.write("mcp.tool.completed", { data: { tool: "problem_locator_create_case", ok: true } });
    writer.write("mcp.tool.completed", { data: { tool: "problem_locator_get_case", ok: true } });
    writer.close();
    const parts = path.join(attemptRoot, "payload", "events", "parts");
    fs.mkdirSync(parts, { recursive: true });
    fs.renameSync(writer.filePath, path.join(parts, "service-linux.route.diagnostics.ndjson"));
    assert.deepEqual(recoverStageAuditProgress({ attemptRoot, stageRoot, stageId }), {
      client_tool_calls: 2,
      server_tool_calls: 2,
      usage: {
        schema_version: 1,
        input_tokens: 100,
        output_tokens: 20,
        cache_creation_input_tokens: 30,
        cache_read_input_tokens: 40,
        total_tokens: 190,
        cost_usd: 0.125,
      },
    });
  } finally { fs.rmSync(attemptRoot, { recursive: true, force: true }); }
});

test("attempt-scoped cleanup removes nested read-only scratch trees", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-cleanup-"));
  try {
    const scratch = path.join(root, "scratch");
    const nested = path.join(scratch, "workspace", "inputs", "tree");
    fs.mkdirSync(nested, { recursive: true });
    fs.writeFileSync(path.join(nested, "payload.txt"), "immutable\n");
    fs.chmodSync(nested, 0o500);
    fs.chmodSync(path.dirname(nested), 0o500);
    removeTreeWritable(scratch, root);
    assert.equal(fs.existsSync(scratch), false);
  } finally {
    if (fs.existsSync(root)) {
      fs.chmodSync(root, 0o700);
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
});
