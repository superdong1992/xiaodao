import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { allowedEmptyEventFiles, createAttempt, finalizeAttempt, recoverStageAuditProgress, requiredEventFiles, verifyVerdict } from "../lib/evidence.mjs";
import { EventWriter } from "../lib/events.mjs";
import { canonicalJson, removeTreeWritable, sha256Bytes, sha256File, writeJsonSync } from "../lib/util.mjs";

const TOOL_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const STATUS_POLICY = { pass: 0, pass_with_warnings: 0, fail: 1, blocked: 2, error: 3 };
const ZERO_USAGE = { input_tokens: 0, output_tokens: 0, cost_usd: 0 };

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
      usage: { input_tokens: 100, output_tokens: 20, cost_usd: 0.125 },
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
      usage: { input_tokens: 100, output_tokens: 20, cost_usd: 0.125 },
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
