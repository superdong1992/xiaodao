import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { loadConfiguration, topologicalStages } from "../lib/config.mjs";
import { buildRunPlan, resolveClient, retryRequirement } from "../lib/planner.mjs";

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
    estimated_tokens: 410000,
    sum_of_per_invocation_caps_usd: 27,
    cumulative_spending_cap: null,
    per_invocation_hard_enforced: true,
  });

  const route = built.plan.stages.find((stage) => stage.id === "journey.cross-job.route");
  const diagnose = built.plan.stages.find((stage) => stage.id === "journey.cross-job.diagnose");
  const publish = built.plan.stages.find((stage) => stage.id === "journey.cross-job.publish-restart");
  assert.deepEqual(route.invocation_caps.map((entry) => [entry.class, entry.min_count, entry.max_count, entry.caps.max_budget_usd]), [
    ["host-client", 1, 1, 3],
    ["server-agent", 3, 3, 3],
  ]);
  assert.deepEqual(diagnose.invocation_caps.map((entry) => [entry.class, entry.min_count, entry.max_count, entry.caps.max_budget_usd]), [
    ["host-client", 1, 1, 5],
    ["server-agent", 3, 3, 3],
  ]);
  assert.deepEqual(publish.invocation_caps.map((entry) => [entry.class, entry.min_count, entry.max_count, entry.caps.max_budget_usd]), [
    ["host-client", 1, 1, 1],
  ]);
});

test("all supported Clients resolve to repository-owned first-party adapters", () => {
  for (const client of ["windows", "macos", "linux"]) {
    const built = buildIsolatedRunPlan({ track: "release", client, planOnly: true });
    assert.equal(
      built.options.crossJobAdapter,
      path.join(REPO_ROOT, "tools", "test-flow", "adapters", `${client}-linux-release.mjs`),
    );
    assert.equal(built.plan.stages.filter((stage) => stage.kind === "isolated-real").length, 0);
    assert.ok(built.plan.stages.filter((stage) => stage.id.startsWith("journey.cross-job.")).every((stage) => stage.decision === "RUN"));
  }
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
});

test("client auto follows Windows/macOS and Linux remains explicit", () => {
  assert.equal(resolveClient("auto", "win32"), "windows");
  assert.equal(resolveClient("auto", "darwin"), "macos");
  assert.equal(resolveClient("auto", "linux"), null);
  assert.equal(resolveClient("linux", "darwin"), "linux");
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
