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

test("declarative flow validates and every public stage action is trusted", () => {
  const config = loadConfiguration(REPO_ROOT);
  assert.equal(config.flow.schema_version, 1);
  assert.ok(config.flow.stages.length >= 10);
  assert.ok(Object.hasOwn(config.gates.gates, "det.journey.same-job"));
  const ordered = topologicalStages(config.flow.stages, ["journey.cross-job.publish-restart"]);
  assert.deepEqual(ordered.slice(-2).map((stage) => stage.id), ["journey.cross-job.review", "journey.cross-job.publish-restart"]);
});

test("Dev default selects only cheap deterministic stages and no real model", () => {
  const built = buildIsolatedRunPlan({ track: "dev", planOnly: true });
  assert.equal(built.plan.admission.status, "ADMITTED");
  assert.deepEqual(built.plan.stages.map((stage) => stage.id), [
    "framework.self-test",
    "deterministic.affected",
    "deterministic.full",
  ]);
  assert.equal(built.plan.budget_advisory.estimated_tokens, 0);
  assert.equal(built.plan.budget_advisory.hard_enforced, false);
});

test("Dev real proof fails admission without explicit opt-in and reason", () => {
  const built = buildIsolatedRunPlan({ track: "dev", goal: "dev.real", stage: "real.route", planOnly: true });
  assert.equal(built.plan.admission.status, "BLOCKED");
  const codes = built.plan.admission.blockers.map((blocker) => blocker.code);
  assert.ok(codes.includes("DEV_REAL_OPT_IN_REQUIRED"));
  assert.ok(codes.includes("DEV_REAL_REASON_REQUIRED"));
});

test("Release plan blocks missing explicit formal inputs before any resource or model call", () => {
  const built = buildIsolatedRunPlan({ track: "release", planOnly: true });
  assert.equal(built.plan.admission.status, "BLOCKED");
  const codes = built.plan.admission.blockers.map((blocker) => blocker.code);
  assert.ok(codes.includes("CLAUDE_ENTRY_REQUIRED"));
  assert.ok(codes.includes("CLAUDE_SETTINGS_REQUIRED"));
  assert.equal(built.plan.resume, "fresh");
  assert.ok(built.plan.stages.some((stage) => stage.id === "journey.cross-job.publish-restart"));
  if (built.plan.client === "macos") {
    assert.equal(built.plan.budget_advisory.estimated_tokens, 35000);
    assert.equal(built.plan.budget_advisory.estimated_cost_usd, 18);
  }
});

test("Release never treats a global-style Claude 2.1.201 executable as cli.js", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-global-claude-"));
  try {
    const globalClaude = path.join(root, "claude");
    fs.writeFileSync(globalClaude, "#!/bin/sh\nprintf '%s\\n' '2.1.201 (Claude Code)'\n", { encoding: "utf8", mode: 0o700 });
    const built = buildIsolatedRunPlan({ track: "release", client: "macos", claudeEntry: globalClaude, planOnly: true });
    const codes = built.plan.admission.blockers.map((blocker) => blocker.code);
    assert.ok(codes.includes("CLAUDE_DISTRIBUTION_INVALID"));
    assert.equal(built.plan.release_inputs.claude.status, "INVALID");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("rollout parity cannot silently pass without an executable pair specification", () => {
  const built = buildIsolatedRunPlan({ track: "release", goal: "release.rollout-parity", planOnly: true });
  assert.ok(built.plan.stages.some((stage) => stage.id === "rollout.parity"));
  assert.ok(built.plan.admission.blockers.some((blocker) => blocker.code === "ROLLOUT_PARITY_SPEC_REQUIRED"));
});

test("client auto follows Windows/macOS and is unresolved on Linux", () => {
  assert.equal(resolveClient("auto", "win32"), "windows");
  assert.equal(resolveClient("auto", "darwin"), "macos");
  assert.equal(resolveClient("auto", "linux"), null);
  assert.equal(resolveClient("linux", "darwin"), "linux");
});

test("a later same-identity PASS resolves an earlier retry stop", () => {
  const identity = { producer_identity: "producer-a", proof_identity: "proof-a" };
  const history = [
    {
      verdict: {
        run_id: "run-failed",
        stages: [{ id: "deterministic.full", status: "BLOCKED", code: "LOOPBACK_BIND_PERMISSION_DENIED", ...identity }],
      },
    },
    {
      verdict: {
        run_id: "run-passed",
        stages: [{ id: "deterministic.full", status: "PASS", ...identity }],
      },
    },
  ];

  assert.deepEqual(retryRequirement(history, { "deterministic.full": identity }), {
    recommendation: "RUN",
    reason: null,
    previous_run_id: null,
    stage_id: null,
    previous_code: null,
  });
});

test("an unrelated later PASS does not erase an unresolved selected-stage failure", () => {
  const frameworkIdentity = { producer_identity: "producer-framework", proof_identity: "proof-framework" };
  const fullIdentity = { producer_identity: "producer-full", proof_identity: "proof-full" };
  const history = [
    {
      verdict: {
        run_id: "run-failed",
        stages: [{ id: "deterministic.full", status: "FAIL", code: "PYTEST_FAILED", ...fullIdentity }],
      },
    },
    {
      verdict: {
        run_id: "run-framework-only",
        stages: [{ id: "framework.self-test", status: "PASS", ...frameworkIdentity }],
      },
    },
  ];

  assert.deepEqual(retryRequirement(history, {
    "framework.self-test": frameworkIdentity,
    "deterministic.full": fullIdentity,
  }), {
    recommendation: "STOP",
    reason: "UNCHANGED_FAILED_IDENTITY",
    previous_run_id: "run-failed",
    stage_id: "deterministic.full",
    previous_code: "PYTEST_FAILED",
  });
});

test("a later NOT_REQUIRED result resolves an obsolete failure for that stage", () => {
  const identity = { producer_identity: "producer-affected", proof_identity: "proof-affected" };
  const history = [
    {
      verdict: {
        run_id: "run-failed",
        stages: [{ id: "deterministic.affected", status: "FAIL", code: "PYTEST_FAILED", ...identity }],
      },
    },
    {
      verdict: {
        run_id: "run-not-required",
        stages: [{ id: "deterministic.affected", status: "NOT_REQUIRED", ...identity }],
      },
    },
  ];

  assert.equal(retryRequirement(history, { "deterministic.affected": identity }).recommendation, "RUN");
});

test("dependency-skipped stages never replace the root retry failure", () => {
  const frameworkIdentity = { producer_identity: "producer-framework", proof_identity: "proof-framework" };
  const fullIdentity = { producer_identity: "producer-full", proof_identity: "proof-full" };
  const history = [{
    verdict: {
      run_id: "run-root-failed",
      stages: [
        { id: "deterministic.full", status: "NOT_RUN", code: "PRIOR_STAGE_NOT_PASSING", ...fullIdentity },
        { id: "framework.self-test", status: "FAIL", code: "NODE_TEST_FAILED", ...frameworkIdentity },
      ],
    },
  }];

  const retry = retryRequirement(history, {
    "framework.self-test": frameworkIdentity,
    "deterministic.full": fullIdentity,
  });
  assert.equal(retry.stage_id, "framework.self-test");
  assert.equal(retry.previous_code, "NODE_TEST_FAILED");
});
