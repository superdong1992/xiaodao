import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { loadConfiguration, topologicalStages } from "../lib/config.mjs";
import { buildRunPlan, resolveClient } from "../lib/planner.mjs";

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
