import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { loadConfiguration, resolveGoalClosure } from "../lib/config.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

function withConfigMutation(fileName, mutate, action) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-config-v2-"));
  try {
    fs.cpSync(path.join(REPO_ROOT, "tools", "test-flow", "config"), root, { recursive: true });
    const filePath = path.join(root, fileName);
    const value = JSON.parse(fs.readFileSync(filePath, "utf8"));
    mutate(value);
    fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`);
    return action(root);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

test("v2 is the only loaded Test Flow configuration bundle", () => {
  const config = loadConfiguration(REPO_ROOT);
  assert.equal(config.proofs.schema_version, 2);
  assert.equal(config.stages.schema_version, 2);
  assert.equal(config.gates.schema_version, 2);
  assert.equal(config.identities.schema_version, 2);
  assert.equal(config.policy.schema_version, 2);
  assert.equal(config.runtimeProfiles.schema_version, 2);
  assert.ok(Object.values(config.files).every((filePath) => filePath.endsWith(".v2.json")));
  assert.match(config.bundle_digest, /^[a-f0-9]{64}$/);
  for (const retired of ["flow.v1.json", "proofs.v1.json", "gates.v1.json", "identities.v1.json"]) {
    assert.equal(fs.existsSync(path.join(REPO_ROOT, "tools", "test-flow", "config", retired)), false);
  }
});

test("unknown configuration fields fail closed", () => {
  assert.throws(() => withConfigMutation("gates.v2.json", (value) => {
    value.gates["det.unit"].unused_patch_field = true;
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_GATE_FIELDS");
});

test("orphan Gates fail before admission", () => {
  assert.throws(() => withConfigMutation("gates.v2.json", (value) => {
    value.gates["det.orphan"] = {
      kind: "pytest",
      selectors: ["tests/deterministic/unit"],
      min_passed: 1,
      skip_policy: "forbid",
      runtime_profile: "python-test",
      evidence: ["pytest.xml", "pytest-summary.json"],
    };
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_ORPHAN_GATE");
});

test("release.full has one fresh six-stage CrossJob closure on every Client platform", () => {
  const config = loadConfiguration(REPO_ROOT);
  const expected = [
    "journey.cross-job.environment",
    "journey.cross-job.route",
    "journey.cross-job.upload",
    "journey.cross-job.diagnose",
    "journey.cross-job.review",
    "journey.cross-job.publish-restart",
  ];
  for (const client of ["windows", "macos", "linux"]) {
    const closure = resolveGoalClosure(config, { goalId: "release.full", track: "release", client });
    assert.deepEqual(closure.stages.filter((stage) => stage.id.startsWith("journey.cross-job.")).map((stage) => stage.id), expected);
    assert.equal(closure.stages.filter((stage) => stage.kind === "isolated-real").length, 0);
    assert.ok(closure.stages.filter((stage) => stage.id.startsWith("journey.cross-job.")).every((stage) => stage.reuse.release === "never"));
  }
});

test("finalization and rollout migration scaffolding are not schedulable Stages", () => {
  const config = loadConfiguration(REPO_ROOT);
  const ids = config.stages.stages.map((stage) => stage.id);
  assert.equal(ids.includes("evidence.finalize"), false);
  assert.equal(ids.includes("rollout.parity"), false);
  assert.equal(Object.hasOwn(config.proofs.goals, "release.rollout-parity"), false);
});

test("every public platform has a repository-owned adapter and no harness identity input", () => {
  const config = loadConfiguration(REPO_ROOT);
  for (const client of ["windows", "macos", "linux"]) {
    assert.equal(fs.existsSync(path.join(REPO_ROOT, "tools", "test-flow", "adapters", `${client}-linux-release.mjs`)), true);
  }
  const identityPaths = Object.values(config.identities.components)
    .filter((component) => component.kind === "paths")
    .flatMap((component) => component.paths);
  assert.equal(identityPaths.some((entry) => entry.includes("tools/test-flow/harness")), false);
  assert.equal(fs.existsSync(path.join(REPO_ROOT, "tools", "test-flow", "harness")), false);
});

test("every repository identity path exists and the two diagnosis skills are distinct", () => {
  const config = loadConfiguration(REPO_ROOT);
  for (const [componentId, component] of Object.entries(config.identities.components)) {
    if (component.kind !== "paths") continue;
    for (const relative of component.paths) {
      assert.equal(fs.existsSync(path.join(REPO_ROOT, relative)), true, `${componentId} is missing ${relative}`);
    }
  }
  assert.deepEqual(config.identities.components["skill.diagnose"].paths, [
    "tests/fixtures/components/diagnosis-generator/diagnose-service-takeover",
  ]);
  assert.deepEqual(config.identities.components["skill.logparse"].paths, [
    ".claude/skills/logparse-diagnose",
  ]);
});

test("unsupported policy versions and dead fields fail closed", () => {
  assert.throws(() => withConfigMutation("policy.v2.json", (value) => {
    value.process.progress_allowlist_version = "test-flow-progress-v1";
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_PROCESS_VERSION");
  assert.throws(() => withConfigMutation("proofs.v2.json", (value) => {
    value.proofs["proof.framework"].fresh = true;
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_PROOF_FIELDS");
});
