import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { loadConfiguration, topologicalStages } from "../lib/config.mjs";
import { buildRunPlan, resolveClient } from "../lib/planner.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

test("declarative flow validates and every public stage action is trusted", () => {
  const config = loadConfiguration(REPO_ROOT);
  assert.equal(config.flow.schema_version, 1);
  assert.ok(config.flow.stages.length >= 10);
  assert.ok(Object.hasOwn(config.gates.gates, "det.journey.same-job"));
  const ordered = topologicalStages(config.flow.stages, ["journey.cross-job.publish-restart"]);
  assert.deepEqual(ordered.slice(-2).map((stage) => stage.id), ["journey.cross-job.review", "journey.cross-job.publish-restart"]);
});

test("Dev default selects only cheap deterministic stages and no real model", () => {
  const built = buildRunPlan(REPO_ROOT, { track: "dev", planOnly: true });
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
  const built = buildRunPlan(REPO_ROOT, { track: "dev", goal: "dev.real", stage: "real.route", planOnly: true });
  assert.equal(built.plan.admission.status, "BLOCKED");
  const codes = built.plan.admission.blockers.map((blocker) => blocker.code);
  assert.ok(codes.includes("DEV_REAL_OPT_IN_REQUIRED"));
  assert.ok(codes.includes("DEV_REAL_REASON_REQUIRED"));
});

test("Release plan is blocked by dirty source before any resource or model call", () => {
  const built = buildRunPlan(REPO_ROOT, { track: "release", planOnly: true });
  assert.equal(built.plan.admission.status, "BLOCKED");
  assert.ok(built.plan.admission.blockers.some((blocker) => blocker.code === "RELEASE_SOURCE_DIRTY"));
  assert.equal(built.plan.resume, "fresh");
  assert.ok(built.plan.stages.some((stage) => stage.id === "journey.cross-job.publish-restart"));
});

test("rollout parity cannot silently pass without an executable pair specification", () => {
  const built = buildRunPlan(REPO_ROOT, { track: "release", goal: "release.rollout-parity", planOnly: true });
  assert.ok(built.plan.stages.some((stage) => stage.id === "rollout.parity"));
  assert.ok(built.plan.admission.blockers.some((blocker) => blocker.code === "ROLLOUT_PARITY_SPEC_REQUIRED"));
});

test("client auto follows Windows/macOS and is unresolved on Linux", () => {
  assert.equal(resolveClient("auto", "win32"), "windows");
  assert.equal(resolveClient("auto", "darwin"), "macos");
  assert.equal(resolveClient("auto", "linux"), null);
  assert.equal(resolveClient("linux", "darwin"), "linux");
});
