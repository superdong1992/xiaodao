import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { loadConfiguration, topologicalStages } from "../lib/config.mjs";
import { buildRunPlan, builtInAdapter, releaseImageValidationMode, resolveClient, retryRequirement, supportedCodexLunaOrchestrator, supportedHostClientTopology } from "../lib/planner.mjs";

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
    "journey.cross-job.review",
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
    cumulative_spending_cap: null,
    per_invocation_hard_enforced: true,
  });
  assert.ok(built.plan.stages.every((stage) => stage.invocation_caps.length === 0));
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
  assert.ok(codes.includes("CLAUDE_ENTRY_REQUIRED"));
  assert.ok(codes.includes("CLAUDE_SETTINGS_REQUIRED"));
  assert.equal(codes.includes("RELEASE_SOURCE_DIRTY"), false);
  assert.match(built.plan.source.snapshot.digest, /^[a-f0-9]{64}$/);
  assert.equal(built.plan.source.snapshot.status, "PRESENT");
  assert.equal(typeof built.plan.source.worktree_clean, "boolean");
  assert.equal(built.plan.resume, "fresh");
  assert.equal(built.options.crossJobAdapter, path.join(REPO_ROOT, "tools", "test-flow", "adapters", "macos-linux-release.mjs"));
  assert.deepEqual(built.plan.budget, {
    estimated_tokens: 910000,
    sum_of_per_invocation_caps_usd: 31,
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
    ["host-client", 1, 1, 400000, 3],
    ["server-agent", 1, 1, 2000000, 3],
  ]);
  assert.deepEqual(diagnose.invocation_caps.map((entry) => [entry.class, entry.min_count, entry.max_count, entry.caps.max_total_tokens, entry.caps.max_budget_usd]), [
    ["host-client", 1, 1, 600000, 5],
    ["server-agent", 3, 3, 2000000, 3],
  ]);
  assert.deepEqual(publish.invocation_caps.map((entry) => [entry.class, entry.min_count, entry.max_count, entry.caps.max_budget_usd]), [
    ["host-client", 1, 1, 1],
  ]);
});

test("Codex posthoc aggregate budget requires an explicit acknowledgement and remains visible after acknowledgement", () => {
  const built = buildIsolatedRunPlan({
    track: "release",
    goal: "release.codex-luna-methods",
    client: "macos",
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
  assert.deepEqual(built.plan.budget, {
    estimated_tokens: 5000000,
    sum_of_per_invocation_caps_usd: 10,
    cumulative_spending_cap: null,
    posthoc_aggregate_limits: {
      exception_id: "PSE-CODEX-LUNA-POSTHOC-001",
      calls: 10,
      tokens: 5000000,
      equivalent_usd: 10,
      enforcement: "posthoc-terminal-aggregate",
      acknowledged: true,
    },
    per_invocation_hard_enforced: false,
  });
  assert.equal(blockerCodes.includes("CLAUDE_ENTRY_REQUIRED"), false);
  assert.equal(blockerCodes.includes("CLAUDE_SETTINGS_REQUIRED"), false);
  assert.equal(blockerCodes.includes("MCP_SOURCE_INVALID"), false);
  assert.equal(blockerCodes.includes("DOCKER_CONTEXT_REQUIRED"), false);
  assert.equal(blockerCodes.includes("CODEX_CLIENT_LABEL_INVALID"), false);
  assert.equal(built.plan.release_inputs.topology, "darwin-local-codex");
  assert.equal(built.plan.release_inputs.network_policy, "codex-app-server-provider-only-command-network-denied");
  assert.equal(built.plan.release_inputs.cross_job_adapter, null);
});

test("Codex Luna goal cannot be mislabeled as a Linux Client proof", () => {
  const built = buildIsolatedRunPlan({
    track: "release",
    goal: "release.codex-luna-methods",
    client: "linux",
    planOnly: true,
    allowCodexPosthocBudget: true,
  });
  assert.ok(built.plan.admission.blockers.some((entry) => entry.code === "CODEX_CLIENT_LABEL_INVALID"));
  assert.equal(built.plan.release_inputs.topology, "darwin-local-codex");
});

test("macOS Luna bootstrap and E2E are independent one-stage Dev goals with exact budgets and cache admission", () => {
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

  const e2e = buildIsolatedRunPlan({
    track: "dev",
    goal: "dev.macos-codex-luna-e2e",
    client: "macos",
    scenario: "api-execution-overrun",
    planOnly: true,
    allowRealModel: true,
    allowCodexPosthocBudget: true,
    reason: "plan",
  }).plan;
  assert.deepEqual(e2e.stages.map((stage) => stage.id), ["real.macos-codex-luna-e2e"]);
  assert.equal(e2e.scenario, "api-execution-overrun");
  assert.deepEqual(e2e.stages[0].invocation_caps[0].phases, ["CLIENT", "ROUTE", "LOGPARSE", "DIAGNOSE", "REVIEW"]);
  assert.equal(e2e.stages[0].invocation_caps[0].per_call_hard_timeout_seconds, 600);
  assert.equal(e2e.budget.posthoc_aggregate_limits.calls, 5);
  assert.equal(e2e.budget.posthoc_aggregate_limits.tokens, 2_000_000);
  assert.equal(e2e.budget.posthoc_aggregate_limits.equivalent_usd, 3);
  assert.ok(e2e.admission.blockers.some((item) => item.code === "MACOS_CODEX_LUNA_METHODS_CACHE_REQUIRED"));

  const invalid = buildIsolatedRunPlan({
    track: "dev",
    goal: "dev.macos-codex-luna-e2e",
    client: "macos",
    scenario: "../untrusted",
    planOnly: true,
  }).plan;
  assert.ok(invalid.admission.blockers.some((item) => item.code === "MACOS_CODEX_LUNA_SCENARIO_INVALID"));
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
