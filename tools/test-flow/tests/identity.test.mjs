import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { chromeIdentity } from "../lib/browser.mjs";
import { hashConfiguredPaths, performanceIdentity, pythonImportPathIdentity, stageIdentity } from "../lib/identity.mjs";

test("Chrome identity freezes the version and executable bytes", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-chrome-identity-"));
  try {
    const executable = path.join(root, process.platform === "win32" ? "chrome.exe" : "google-chrome");
    fs.writeFileSync(executable, "frozen chrome launcher\n");
    const identity = chromeIdentity(
      { TEST_FLOW_CHROME: executable },
      process.platform,
      () => ({ status: 0, stdout: "Google Chrome 140.0.7339.1\n", stderr: "" }),
    );
    assert.equal(identity.status, "PRESENT");
    assert.equal(identity.version, "Google Chrome 140.0.7339.1");
    assert.match(identity.executable_sha256, /^[a-f0-9]{64}$/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Chrome identity accepts the pinned Chrome for Testing version format", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-cft-identity-"));
  try {
    const executable = path.join(root, "chrome");
    fs.writeFileSync(executable, "frozen cft launcher\n");
    const identity = chromeIdentity(
      { TEST_FLOW_CHROME: executable },
      "linux",
      () => ({ status: 0, stdout: "Google Chrome for Testing 152.0.7977.54\n", stderr: "" }),
    );
    assert.equal(identity.status, "PRESENT");
    assert.equal(identity.version, "Google Chrome for Testing 152.0.7977.54");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("Python import identity changes when external PYTHONPATH content changes", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-python-path-"));
  try {
    const repository = path.join(root, "repo");
    const external = path.join(root, "external");
    fs.mkdirSync(repository);
    fs.mkdirSync(external);
    const module = path.join(external, "dependency.py");
    fs.writeFileSync(module, "VALUE = 1\n");
    const first = pythonImportPathIdentity(repository, { sys_path: [repository, external] });
    assert.deepEqual(first[0], { index: 0, kind: "repository", path: "." });
    fs.writeFileSync(module, "VALUE = 2\n");
    const second = pythonImportPathIdentity(repository, { sys_path: [repository, external] });
    assert.notEqual(first[1].digest, second[1].digest);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("configured identity paths fail visibly when an input is absent", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-identity-missing-"));
  try {
    const identity = hashConfiguredPaths(root, ["present.txt", "missing.txt"]);
    assert.equal(identity.records.find((record) => record.path === "missing.txt").kind, "missing");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("producer and proof identity are separate but proof remains producer-bound", () => {
  const stage = { id: "deterministic.full", identity_set: "deterministic" };
  const policy = {
    parent_checkpoint: "GENESIS",
    scenario: "CrossJob",
    stage_definition_digest: "stage-v1",
    dependency_proof_identities: ["dependency-a"],
    config_bundle_digest: "bundle-a",
    evidence_contract_version: "events-v2",
  };
  const base = stageIdentity(stage, { deterministic: { producer_digest: "product-a", proof_digest: "tests-a" } }, policy);
  const proofOnly = stageIdentity(stage, { deterministic: { producer_digest: "product-a", proof_digest: "tests-b" } }, policy);
  assert.equal(base.producer_identity, proofOnly.producer_identity);
  assert.notEqual(base.proof_identity, proofOnly.proof_identity);

  const productChanged = stageIdentity(stage, { deterministic: { producer_digest: "product-b", proof_digest: "tests-a" } }, policy);
  assert.notEqual(base.producer_identity, productChanged.producer_identity);
  assert.notEqual(base.proof_identity, productChanged.proof_identity);
  const registrationChanged = stageIdentity(stage, { deterministic: { producer_digest: "product-a", proof_digest: "tests-a" } }, { ...policy, registration_tree_digest: "registration-a" });
  assert.notEqual(base.producer_identity, registrationChanged.producer_identity);
});

test("performance identity changes with producer, policy version or stage policy", () => {
  const stage = { id: "deterministic.full", progress_class: "local" };
  const policy = { policy_version: "robust-mad-v2", stages: { "deterministic.full": { mode: "gate", hard_cap_seconds: 300 }, "*": { mode: "warn", hard_cap_seconds: null } } };
  const baseline = performanceIdentity(stage, "producer-a", policy);
  assert.notEqual(baseline, performanceIdentity(stage, "producer-b", policy));
  assert.notEqual(baseline, performanceIdentity(stage, "producer-a", { ...policy, policy_version: "robust-mad-v3" }));
  assert.notEqual(baseline, performanceIdentity(stage, "producer-a", { ...policy, stages: { ...policy.stages, "deterministic.full": { mode: "gate", hard_cap_seconds: 301 } } }));
});
