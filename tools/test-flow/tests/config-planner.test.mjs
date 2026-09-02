import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { loadConfiguration, topologicalStages } from "../lib/config.mjs";
import { findReusableStages } from "../lib/history.mjs";
import { buildRunPlan, builtInAdapter, freshStageIdsForTrack, providerCertificationClient, releaseImageValidationMode, resolveClient, retryRequirement, supportedCodexLunaOrchestrator, supportedHostClientTopology } from "../lib/planner.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

function buildIsolatedRunPlan(options) {
  const evidenceRoot = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-planner-evidence-"));
  try {
    return buildRunPlan(REPO_ROOT, { ...options, evidenceRoot });
  } finally {
    fs.rmSync(evidenceRoot, { recursive: true, force: true });
  }
}

test("the v2 bundle resolves Goal to Proof to Stage to Gate in DAG order", () => {
  const config = loadConfiguration(REPO_ROOT);
  assert.equal(config.proofs.schema_version, 2);
  assert.equal(config.stages.schema_version, 2);
  assert.equal(config.gates.schema_version, 2);
  assert.ok(Object.hasOwn(config.gates.gates, "det.journey.same-job"));
  assert.equal(config.policy.tracks.dev.requires_source_snapshot, true);
  assert.equal(config.policy.tracks.release.requires_source_snapshot, true);
  const ordered = topologicalStages(config.stages.stages, ["journey.cross-job.publish-restart"]);
  assert.deepEqual(ordered.slice(-2).map((stage) => stage.id), [
    "journey.cross-job.diagnose",
    "journey.cross-job.publish-restart",
  ]);
});

test("Dev default selects the complete cheap deterministic closure and no model budget", () => {
  const built = buildIsolatedRunPlan({ track: "dev", planOnly: true });
  assert.equal(built.plan.admission.status, "ADMITTED");
  assert.deepEqual(built.plan.stages.map((stage) => stage.id), [
    "framework.self-test",
    "repository.static",
    "deterministic.affected",
    "deterministic.full",
  ]);
  assert.deepEqual(built.plan.budget, {
    estimated_tokens: 0,
    sum_of_per_invocation_caps_usd: 0,
    hard_cap_tokens: 0,
    hard_cap_usd: 0,
    normal_model_calls: 0,
    repair_model_calls_max: 0,
    hard_max_model_calls: 0,
    cumulative_spending_cap: null,
    per_invocation_hard_enforced: true,
  });
  assert.ok(built.plan.stages.every((stage) => stage.invocation_caps.length === 0));
  assert.equal(built.plan.stages[0].timeout_seconds, 300);
});

test("Dev quick selects only the affected deterministic closure", () => {
  const built = buildIsolatedRunPlan({ track: "dev", goal: "dev.quick", planOnly: true });
  assert.equal(built.plan.admission.status, "ADMITTED");
  assert.deepEqual(built.plan.proofs.map((proof) => proof.id), [
    "proof.framework",
    "proof.repository-static",
    "proof.deterministic-affected",
  ]);
  assert.deepEqual(built.plan.stages.map((stage) => stage.id), [
    "framework.self-test",
    "repository.static",
    "deterministic.affected",
  ]);
  assert.equal(built.plan.stages.some((stage) => stage.id === "deterministic.full"), false);
  assert.equal(built.plan.budget.normal_model_calls, 0);
});

test("Dev real requires one selected proof, explicit opt-in and a reason", () => {
  const built = buildIsolatedRunPlan({ track: "dev", goal: "dev.real", stage: "real.route", planOnly: true });
  assert.equal(built.plan.admission.status, "BLOCKED");
  const codes = built.plan.admission.blockers.map((blocker) => blocker.code);
  assert.ok(codes.includes("DEV_REAL_OPT_IN_REQUIRED"));
  assert.ok(codes.includes("DEV_REAL_REASON_REQUIRED"));
  assert.ok(built.plan.stages.some((stage) => stage.id === "real.route"));
  assert.equal(built.plan.stages.filter((stage) => stage.kind === "isolated-real").length, 2);
});

test("Release is fresh, binds an immutable source snapshot and exposes exact per-invocation caps", () => {
  const built = buildIsolatedRunPlan({ track: "release", client: "macos", planOnly: true });
  assert.equal(built.plan.admission.status, "BLOCKED");
  const codes = built.plan.admission.blockers.map((blocker) => blocker.code);
  assert.equal(codes.includes("EVIDENCE_V2_REAL_DIAGNOSIS_ADAPTER_UNMIGRATED"), false);
  assert.ok(codes.includes("CLAUDE_ENTRY_REQUIRED"));
  assert.ok(codes.includes("CLAUDE_SETTINGS_REQUIRED"));
  assert.equal(codes.includes("RELEASE_SOURCE_DIRTY"), false);
  assert.match(built.plan.source.snapshot.digest, /^[a-f0-9]{64}$/);
  assert.equal(built.plan.source.snapshot.status, "PRESENT");
  assert.equal(typeof built.plan.source.worktree_clean, "boolean");
  assert.equal(built.plan.resume, "fresh");
  assert.equal(built.options.crossJobAdapter, path.join(REPO_ROOT, "tools", "test-flow", "adapters", "macos-linux-release.mjs"));
  assert.deepEqual(built.plan.budget, {
    estimated_tokens: 6100000,
    sum_of_per_invocation_caps_usd: 28,
    hard_cap_tokens: 8500000,
    hard_cap_usd: 31,
    normal_model_calls: 7,
    repair_model_calls_max: 1,
    hard_max_model_calls: 8,
    cumulative_spending_cap: null,
    per_invocation_hard_enforced: true,
  });

  const codex = built.plan.stages.find((stage) => stage.id === "real.codex-luna-methods");
  assert.equal(codex, undefined);
  assert.deepEqual(built.plan.release_inputs.codex, { status: "NOT_REQUIRED" });
  assert.deepEqual(built.plan.release_inputs.codex_logparse_runtime, { status: "NOT_REQUIRED" });
  assert.equal(codes.includes("CODEX_RUNTIME_INVALID"), false);
  assert.equal(codes.includes("CODEX_POSTHOC_BUDGET_ACK_REQUIRED"), false);

  const route = built.plan.stages.find((stage) => stage.id === "journey.cross-job.route");
  const diagnose = built.plan.stages.find((stage) => stage.id === "journey.cross-job.diagnose");
  const publish = built.plan.stages.find((stage) => stage.id === "journey.cross-job.publish-restart");
  assert.deepEqual(route.invocation_caps.map((entry) => [entry.class, entry.min_count, entry.max_count, entry.caps.max_total_tokens, entry.caps.max_budget_usd]), [
    ["host-client", 2, 2, 400000, 3],
    ["server-agent", 1, 1, 2000000, 3],
  ]);
  assert.deepEqual(diagnose.invocation_caps.map((entry) => [entry.class, entry.min_count, entry.max_count, entry.caps.max_total_tokens, entry.caps.max_budget_usd]), [
    ["host-client", 1, 1, 600000, 5],
    ["server-agent", 1, 2, 2000000, 3],
  ]);
  assert.deepEqual([diagnose.normal_model_calls, diagnose.repair_model_calls_max, diagnose.hard_max_model_calls], [2, 1, 3]);
  assert.deepEqual(diagnose.normal_budget, { tokens: 2600000, cost_usd: 8 });
  assert.deepEqual(diagnose.hard_budget, { tokens: 4600000, cost_usd: 11 });
  assert.deepEqual(publish.invocation_caps.map((entry) => [entry.class, entry.min_count, entry.max_count, entry.caps.max_budget_usd]), [
    ["host-client", 1, 1, 1],
  ]);
});

test("formal Evidence V2 certification defaults both providers to one Specialist call and one repair", () => {
  const built = buildIsolatedRunPlan({
    track: "release",
    goal: "release.evidence-v2-certification",
    client: process.platform === "linux" ? "linux" : "macos",
    planOnly: true,
    allowCodexPosthocBudget: true,
  });
  const blockerCodes = built.plan.admission.blockers.map((entry) => entry.code);
  const warningCodes = built.plan.admission.warnings.map((entry) => entry.code);
  assert.equal(blockerCodes.includes("CODEX_POSTHOC_BUDGET_ACK_REQUIRED"), false);
  assert.ok(blockerCodes.includes("CODEX_RUNTIME_INVALID"));
  assert.deepEqual(warningCodes.filter((code) => code === "CODEX_POSTHOC_BUDGET_EXCEPTION"), ["CODEX_POSTHOC_BUDGET_EXCEPTION"]);
  assert.equal(built.plan.budget.posthoc_aggregate_limits.acknowledged, true);
  assert.equal(built.plan.budget.per_invocation_hard_enforced, false);
  assert.deepEqual(
    built.plan.stages
      .filter((stage) => ["real.macos-codex-luna-e2e", "real.macos-claude-deepseek-e2e"].includes(stage.id))
      .map((stage) => [stage.id, stage.normal_model_calls, stage.repair_model_calls_max, stage.hard_max_model_calls]),
    [
      ["real.macos-claude-deepseek-e2e", 1, 1, 2],
      ["real.macos-codex-luna-e2e", 1, 1, 2],
    ],
  );
  assert.deepEqual(built.plan.budget.posthoc_aggregate_limits, {
    exception_id: "PSE-CODEX-LUNA-POSTHOC-001",
    normal_calls: 1,
    repair_calls_max: 1,
    calls: 2,
    tokens: 2000000,
    equivalent_usd: 3,
    enforcement: "posthoc-terminal-aggregate",
    acknowledged: true,
  });
  assert.deepEqual({
    estimated_tokens: built.plan.budget.estimated_tokens,
    estimated_cost_usd: built.plan.budget.sum_of_per_invocation_caps_usd,
    hard_cap_tokens: built.plan.budget.hard_cap_tokens,
    hard_cap_usd: built.plan.budget.hard_cap_usd,
    normal_model_calls: built.plan.budget.normal_model_calls,
    repair_model_calls_max: built.plan.budget.repair_model_calls_max,
    hard_max_model_calls: built.plan.budget.hard_max_model_calls,
  }, {
    estimated_tokens: 4_600_000,
    estimated_cost_usd: 17,
    hard_cap_tokens: 5_000_000,
    hard_cap_usd: 17,
    normal_model_calls: 3,
    repair_model_calls_max: 2,
    hard_max_model_calls: 5,
  });
  assert.ok(blockerCodes.includes("CLAUDE_ENTRY_REQUIRED"));
  assert.ok(blockerCodes.includes("CLAUDE_SETTINGS_REQUIRED"));
  assert.equal(blockerCodes.includes("MCP_SOURCE_INVALID"), false);
  assert.equal(blockerCodes.includes("DOCKER_CONTEXT_REQUIRED"), false);
  assert.equal(blockerCodes.includes("CODEX_CLIENT_LABEL_INVALID"), false);
  assert.ok(["darwin-local-claude-deepseek-quick-validation", "sealed-ubuntu2204-container-claude-deepseek-quick-validation"].includes(built.plan.release_inputs.topology));
  assert.equal(built.plan.release_inputs.network_policy, "provider-plus-local-evidence-v2-runtime");
  assert.equal(built.plan.release_inputs.cross_job_adapter, null);
});

test("optional blind-review certification preserves the two-role two-to-four call topology", () => {
  const built = buildIsolatedRunPlan({
    track: "release",
    goal: "release.evidence-v2-blind-review-certification",
    client: process.platform === "linux" ? "linux" : "macos",
    planOnly: true,
    allowCodexPosthocBudget: true,
  });
  assert.deepEqual(
    built.plan.stages
      .filter((stage) => stage.id.endsWith("blind-review-e2e"))
      .map((stage) => [stage.id, stage.invocation_caps[0].phases, stage.normal_model_calls, stage.repair_model_calls_max, stage.hard_max_model_calls]),
    [
      ["real.macos-claude-deepseek-blind-review-e2e", ["SPECIALIST:PRIMARY", "SPECIALIST:REPAIR?", "REVIEWER:PRIMARY", "REVIEWER:REPAIR?"], 2, 2, 4],
      ["real.macos-codex-luna-blind-review-e2e", ["SPECIALIST:PRIMARY", "SPECIALIST:REPAIR?", "REVIEWER:PRIMARY", "REVIEWER:REPAIR?"], 2, 2, 4],
    ],
  );
  assert.deepEqual(
    [built.plan.budget.posthoc_aggregate_limits.normal_calls, built.plan.budget.posthoc_aggregate_limits.repair_calls_max, built.plan.budget.posthoc_aggregate_limits.calls],
    [2, 2, 4],
  );
});

test("a reusable historical PASS cannot replace the current-attempt Release Core", () => {
  const config = loadConfiguration(REPO_ROOT);
  const fresh = freshStageIdsForTrack(config.stages.stages, "release", { requireCurrentAttemptCore: true });
  assert.equal(fresh.has("deterministic.full"), true);
  const identity = { producer_identity: "same-producer", proof_identity: "same-proof" };
  const reusable = findReusableStages([{
    attempt_root: path.join(os.tmpdir(), "historical-core-pass"),
    verdict: {
      run_id: "run-historical-core",
      committed_at_utc: "2026-08-01T00:00:00.000Z",
      evidence_reusable: true,
      stages: [{ id: "deterministic.full", kind: "deterministic", status: "PASS", result_source: "EXECUTED", ...identity }],
    },
  }], { "deterministic.full": identity }, { track: "release", freshStageIds: fresh });
  assert.equal(reusable.has("deterministic.full"), false);

  const built = buildIsolatedRunPlan({
    track: "release",
    goal: "release.evidence-v2-certification",
    client: process.platform === "linux" ? "linux" : "macos",
    planOnly: true,
    allowCodexPosthocBudget: true,
  });
  const core = built.plan.stages.find((stage) => stage.id === "deterministic.full");
  assert.equal(core.decision, "RUN");
  assert.equal(core.reuse, null);
  assert.ok(core.gates.some((gate) => gate.id === "det.evidence-v2-core"));
});

test("provider certification uses Linux inside sealed Ubuntu and macOS on native Darwin", () => {
  assert.equal(providerCertificationClient("linux"), "linux");
  assert.equal(providerCertificationClient("darwin"), "macos");
  const config = loadConfiguration(REPO_ROOT);
  for (const stageId of ["real.macos-codex-luna-e2e", "real.macos-claude-deepseek-e2e", "evidence-v2.release-verdict"]) {
    const stage = config.stages.stages.find((item) => item.id === stageId);
    assert.deepEqual(stage.platforms, ["macos", "linux"]);
  }
});

test("Luna package generation stays independent while P2 uses Core plus the shared production registration", () => {
  const methods = buildIsolatedRunPlan({
    track: "dev",
    goal: "dev.macos-codex-luna-methods",
    client: "macos",
    planOnly: true,
    allowRealModel: true,
    allowCodexPosthocBudget: true,
    reason: "plan",
  }).plan;
  assert.deepEqual(methods.stages.map((stage) => stage.id), ["real.macos-codex-luna-methods"]);
  assert.equal(methods.stages[0].no_progress_seconds, 180);
  assert.deepEqual(methods.stages[0].invocation_caps.map((item) => [item.class, item.min_count, item.max_count]), [["codex-luna-methods-bootstrap", 1, 1]]);
  assert.equal(methods.stages[0].invocation_caps[0].per_call_hard_timeout_seconds, 600);
  assert.equal(methods.budget.posthoc_aggregate_limits.calls, 1);
  assert.equal(methods.budget.posthoc_aggregate_limits.tokens, 1_000_000);
  assert.equal(methods.budget.posthoc_aggregate_limits.equivalent_usd, 2);
  assert.equal(methods.admission.blockers.some((item) => item.code === "EVIDENCE_V2_REAL_DIAGNOSIS_ADAPTER_UNMIGRATED"), false);

  const e2e = buildIsolatedRunPlan({
    track: "dev",
    goal: "dev.macos-codex-luna-e2e",
    client: "macos",
    scenario: "multiple-rpc-timeouts",
    planOnly: true,
    allowRealModel: true,
    allowCodexPosthocBudget: true,
    reason: "plan",
  }).plan;
  assert.deepEqual(e2e.stages.map((stage) => stage.id), [
    "framework.self-test",
    "repository.static",
    "deterministic.affected",
    "deterministic.full",
    "real.skill-generation",
    "real.macos-codex-luna-e2e",
  ]);
  assert.equal(e2e.scenario, "multiple-rpc-timeouts");
  const e2eStage = e2e.stages.find((stage) => stage.id === "real.macos-codex-luna-e2e");
  assert.deepEqual(e2eStage.invocation_caps[0].phases, ["SPECIALIST:PRIMARY", "SPECIALIST:REPAIR?"]);
  assert.deepEqual([e2eStage.invocation_caps[0].min_count, e2eStage.invocation_caps[0].max_count], [1, 2]);
  assert.deepEqual([e2eStage.normal_model_calls, e2eStage.repair_model_calls_max, e2eStage.hard_max_model_calls], [1, 1, 2]);
  assert.equal(e2eStage.invocation_caps[0].per_call_hard_timeout_seconds, 600);
  assert.ok(e2eStage.timeout_seconds > e2eStage.invocation_caps[0].max_count * e2eStage.invocation_caps[0].per_call_hard_timeout_seconds);
  assert.equal(e2e.budget.posthoc_aggregate_limits.normal_calls, 1);
  assert.equal(e2e.budget.posthoc_aggregate_limits.repair_calls_max, 1);
  assert.equal(e2e.budget.posthoc_aggregate_limits.calls, 2);
  assert.equal(e2e.budget.posthoc_aggregate_limits.tokens, 2_000_000);
  assert.equal(e2e.budget.posthoc_aggregate_limits.equivalent_usd, 3);
  assert.equal(e2e.admission.blockers.some((item) => item.code === "EVIDENCE_V2_REAL_DIAGNOSIS_ADAPTER_UNMIGRATED"), false);
  assert.equal(e2e.admission.blockers.some((item) => item.code === "MACOS_CODEX_LUNA_METHODS_CACHE_REQUIRED"), false);
  assert.deepEqual(e2eStage.gates.map((gate) => gate.id), ["quick.codex-luna.contracts", "quick.codex-luna.model-cert-driver", "real.macos-codex-luna-e2e"]);

  const invalid = buildIsolatedRunPlan({
    track: "dev",
    goal: "dev.macos-codex-luna-e2e",
    client: "macos",
    scenario: "../untrusted",
    planOnly: true,
  }).plan;
  assert.ok(invalid.admission.blockers.some((item) => item.code === "MACOS_CODEX_LUNA_SCENARIO_INVALID"));
});

test("central P1 plan fixes the scenario and declares the Specialist-only topology", () => {
  const build = () => buildIsolatedRunPlan({
    track: "dev",
    goal: "dev.macos-claude-deepseek-e2e",
    client: "macos",
    scenario: "multiple-rpc-timeouts",
    planOnly: true,
    allowRealModel: true,
    reason: "plan",
  }).plan;
  const normal = build();
  assert.equal(normal.admission.blockers.some((item) => item.code === "EVIDENCE_V2_REAL_DIAGNOSIS_ADAPTER_UNMIGRATED"), false);
  const normalStage = normal.stages.find((stage) => stage.id === "real.macos-claude-deepseek-e2e");
  const declaration = normalStage.invocation_caps[0];
  assert.deepEqual(declaration.phases, ["SPECIALIST:PRIMARY", "SPECIALIST:REPAIR?"]);
  assert.deepEqual([declaration.min_count, declaration.max_count, declaration.normal_count, declaration.repair_max_count], [1, 2, 1, 1]);
  assert.deepEqual([normalStage.normal_model_calls, normalStage.repair_model_calls_max, normalStage.hard_max_model_calls], [1, 1, 2]);
  assert.ok(normalStage.timeout_seconds > declaration.max_count * declaration.per_call_hard_timeout_seconds);
  assert.equal(normalStage.gates.find((gate) => gate.id === "quick.claude-deepseek-e2e.contracts").required_evidence[0], "node-test.tap");
});

test("Dev CrossJob diagnosis uses the Evidence V2 Specialist-only service-call contract", () => {
  const built = buildIsolatedRunPlan({
    track: "dev",
    goal: "dev.real",
    stage: "journey.cross-job.diagnose",
    client: "windows",
    planOnly: true,
    allowRealModel: true,
    reason: "验证 Evidence V2 正式定位路径",
  });
  assert.equal(built.plan.admission.blockers.some((item) => item.code === "EVIDENCE_V2_REAL_DIAGNOSIS_ADAPTER_UNMIGRATED"), false);
  const declaration = built.plan.stages.find((stage) => stage.id === "journey.cross-job.diagnose").invocation_caps.find((item) => item.class === "server-agent");
  assert.deepEqual([declaration.min_count, declaration.max_count, declaration.normal_count, declaration.repair_max_count], [1, 2, 1, 1]);
  assert.equal(built.plan.admission.status, "BLOCKED");
});

test("all supported Clients resolve to repository-owned first-party adapters", () => {
  for (const client of ["windows", "macos", "linux"]) {
    const built = buildIsolatedRunPlan({ track: "release", client, planOnly: true });
    const adapterName = process.platform === "darwin" && client === "linux"
      ? "macos-linux-linux-release.mjs"
      : `${client}-linux-release.mjs`;
    assert.equal(
      built.options.crossJobAdapter,
      path.join(REPO_ROOT, "tools", "test-flow", "adapters", adapterName),
    );
    assert.deepEqual(
      built.plan.stages.filter((stage) => stage.kind === "isolated-real").map((stage) => stage.id),
      ["real.skill-generation"],
    );
    assert.ok(built.plan.stages.filter((stage) => stage.id.startsWith("journey.cross-job.")).every((stage) => stage.decision === "RUN"));
  }
});

test("plans expose only observable progress and exact serial model deadlines", () => {
  const built = buildIsolatedRunPlan({ track: "dev", goal: "dev.real", stage: "real.skill-generation", planOnly: true });
  const stage = built.plan.stages.find((candidate) => candidate.id === "real.skill-generation");
  assert.equal(stage.no_progress_seconds, null);
  assert.equal(stage.timeout_seconds, 2100);
  assert.deepEqual(stage.hard_caps, {
    max_turns: 16,
    max_total_tokens: 1000000,
    max_output_tokens: 64000,
    max_budget_usd: 10,
    hard_timeout_seconds: 1800,
  });
  assert.equal(stage.estimated_tokens, 600000);
  assert.deepEqual(stage.invocation_caps.map((entry) => [entry.class, entry.min_count, entry.max_count]), [
    ["isolated-agent", 1, 1],
  ]);
  assert.ok(stage.hard_caps.hard_timeout_seconds + 60 < stage.timeout_seconds);

  const serverBuilt = buildIsolatedRunPlan({ track: "release", client: "linux", planOnly: true });
  const host = serverBuilt.plan.stages.find((candidate) => candidate.id === "platform.host-capability");
  const server = serverBuilt.plan.stages.find((candidate) => candidate.id === "platform.server-linux-capability");
  const journey = serverBuilt.plan.stages.find((candidate) => candidate.id === "journey.cross-job.environment");
  assert.equal(host.no_progress_seconds, null);
  assert.equal(server.no_progress_seconds, 360);
  assert.equal(journey.no_progress_seconds, 360);
});

test("retired rollout goals and caller-supplied adapters cannot become an execution path", () => {
  assert.throws(
    () => buildIsolatedRunPlan({ track: "release", goal: "release.rollout-parity", client: "macos", planOnly: true }),
    (error) => error.code === "GOAL_UNKNOWN",
  );
  const built = buildIsolatedRunPlan({
    track: "release",
    client: "macos",
    crossJobAdapter: "/tmp/untrusted-adapter",
    planOnly: true,
  });
  assert.equal(built.options.crossJobAdapter, path.join(REPO_ROOT, "tools", "test-flow", "adapters", "macos-linux-release.mjs"));
  assert.throws(
    () => buildIsolatedRunPlan({ track: "dev", goal: "dev.real", stage: "real.diagnose", planOnly: true }),
    (error) => error.code === "REAL_STAGE_INVALID",
  );
});

test("client auto follows Windows/macOS and Linux remains explicit", () => {
  assert.equal(resolveClient("auto", "win32"), "windows");
  assert.equal(resolveClient("auto", "darwin"), "macos");
  assert.equal(resolveClient("auto", "linux"), null);
  assert.equal(resolveClient("linux", "darwin"), "linux");
});

test("Darwin explicit Linux selects only the dual-container adapter and rejects unsupported host-client pairs", () => {
  assert.equal(
    builtInAdapter(REPO_ROOT, "linux", "darwin"),
    path.join(REPO_ROOT, "tools", "test-flow", "adapters", "macos-linux-linux-release.mjs"),
  );
  assert.equal(builtInAdapter(REPO_ROOT, "linux", "linux"), path.join(REPO_ROOT, "tools", "test-flow", "adapters", "linux-linux-release.mjs"));
  assert.equal(supportedHostClientTopology("linux", "darwin"), true);
  assert.equal(supportedHostClientTopology("macos", "darwin"), true);
  assert.equal(supportedHostClientTopology("windows", "darwin"), false);
  assert.equal(supportedHostClientTopology("linux", "win32"), false);
});

test("the exact Mach-O Codex Luna flow is bound to a Darwin arm64 orchestrator", () => {
  assert.equal(supportedCodexLunaOrchestrator("darwin", "arm64"), true);
  assert.equal(supportedCodexLunaOrchestrator("darwin", "x64"), false);
  assert.equal(supportedCodexLunaOrchestrator("linux", "arm64"), false);
  assert.equal(supportedCodexLunaOrchestrator("win32", "x64"), false);
});

test("every formal orchestrator freezes the Linux Server image by exact identity", () => {
  assert.equal(releaseImageValidationMode("darwin", true), "sealed-darwin-cache");
  assert.equal(releaseImageValidationMode("win32", true), "portable-exact-server-image");
  assert.equal(releaseImageValidationMode("linux", true), "portable-exact-server-image");
  assert.equal(releaseImageValidationMode("linux", false), "not-required");
});

test("Darwin explicit Linux declares the Client runtime by frozen image instead of the orchestrator Node", (context) => {
  if (process.platform !== "darwin") {
    context.skip("Darwin orchestrator regression");
    return;
  }
  const built = buildIsolatedRunPlan({ track: "release", client: "linux", planOnly: true });
  const declared = built.plan.release_inputs.claude.selected_client_runtime;
  const source = built.plan.release_inputs.claude.orchestrator_distribution_source;
  assert.equal(declared.execution_topology, "darwin-orchestrated-linux-container");
  assert.equal(declared.platform, "linux/amd64");
  assert.equal(declared.node_identity_boundary, "client-image-id");
  assert.equal(declared.node.version, null);
  assert.equal(declared.node.sha256, null);
  assert.deepEqual(Object.keys(declared.claude).sort(), ["cli_sha256", "version"]);
  assert.equal(Object.hasOwn(declared.claude, "tarball_sha256"), false);
  assert.equal(source.node.version, process.version);
  assert.notEqual(source.node.sha256, null);
  for (const stageId of ["journey.cross-job.route", "journey.cross-job.diagnose", "journey.cross-job.publish-restart"]) {
    const declaration = built.plan.stages.find((stage) => stage.id === stageId).invocation_caps[0];
    assert.equal(declaration.class, "linux-client-container");
    assert.equal(declaration.execution_topology, "darwin-orchestrated-linux-container");
  }
});

test("the planner emits a hard blocker for an unsupported current host-client topology", (context) => {
  if (process.platform !== "darwin") {
    context.skip("Darwin orchestrator regression");
    return;
  }
  const built = buildIsolatedRunPlan({ track: "release", client: "windows", planOnly: true });
  assert.ok(built.plan.admission.blockers.some((entry) => entry.code === "HOST_CLIENT_TOPOLOGY_UNSUPPORTED"));
  assert.notEqual(built.plan.admission.status, "ADMITTED");
});

test("a later same-identity PASS resolves an earlier retry stop", () => {
  const identity = { producer_identity: "producer-a", proof_identity: "proof-a" };
  const history = [
    { verdict: { run_id: "run-failed", stages: [{ id: "deterministic.full", status: "FAIL", code: "PYTEST_FAILED", ...identity }] } },
    { verdict: { run_id: "run-passed", stages: [{ id: "deterministic.full", status: "PASS", ...identity }] } },
  ];
  assert.deepEqual(retryRequirement(history, { "deterministic.full": identity }), {
    recommendation: "RUN",
    reason: null,
    previous_run_id: null,
    stage_id: null,
    previous_code: null,
  });
});

test("a full deterministic plan supersedes the quick scope escalation", () => {
  const identity = { producer_identity: "producer-affected", proof_identity: "proof-affected" };
  const history = [{ verdict: {
    run_id: "run-quick-needs-full",
    stages: [{
      id: "deterministic.affected",
      status: "INCONCLUSIVE",
      code: "AFFECTED_SCOPE_REQUIRES_FULL",
      ...identity,
    }],
  } }];

  assert.equal(
    retryRequirement(history, { "deterministic.affected": identity }).recommendation,
    "STOP",
  );
  assert.deepEqual(retryRequirement(
    history,
    { "deterministic.affected": identity },
    { supersededFailureCodes: ["AFFECTED_SCOPE_REQUIRES_FULL"] },
  ), {
    recommendation: "RUN",
    reason: null,
    previous_run_id: null,
    stage_id: null,
    previous_code: null,
  });
});

test("dependency-skipped stages do not hide the unchanged root failure", () => {
  const framework = { producer_identity: "producer-framework", proof_identity: "proof-framework" };
  const full = { producer_identity: "producer-full", proof_identity: "proof-full" };
  const history = [{ verdict: {
    run_id: "run-root-failed",
    stages: [
      { id: "deterministic.full", status: "NOT_RUN", code: "PRIOR_STAGE_NOT_PASSING", ...full },
      { id: "framework.self-test", status: "FAIL", code: "NODE_TEST_FAILED", ...framework },
    ],
  } }];
  assert.deepEqual(retryRequirement(history, {
    "framework.self-test": framework,
    "deterministic.full": full,
  }), {
    recommendation: "STOP",
    reason: "UNCHANGED_FAILED_IDENTITY",
    previous_run_id: "run-root-failed",
    stage_id: "framework.self-test",
    previous_code: "NODE_TEST_FAILED",
  });
});
