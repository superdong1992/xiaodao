import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { materializeEvidenceV2CoreVerdict } from "../lib/actions.mjs";
import { loadConfiguration } from "../lib/config.mjs";
import { canonicalJson, sha256Bytes } from "../lib/util.mjs";
import {
  EVIDENCE_V2_CORE_RECEIPT,
  EVIDENCE_V2_CORE_SELECTORS,
  evidenceV2CoreCasesDigest,
  validateEvidenceV2CoreVerdict,
  validateEvidenceV2CoreVerdictSchema,
} from "../../validation/evidence-v2-core.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const SOURCE_DIGEST = "a".repeat(64);

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "evidence-v2-core-"));
  const sourceRoot = path.join(root, "source");
  const gateRoot = path.join(root, "gate");
  fs.mkdirSync(path.join(sourceRoot, "schemas", "v2"), { recursive: true });
  fs.mkdirSync(gateRoot, { recursive: true });
  fs.writeFileSync(path.join(sourceRoot, "schemas", "v2", "contract-manifest.json"), canonicalJson({ schema_version: 9 }));
  fs.writeFileSync(path.join(gateRoot, "pytest-summary.json"), canonicalJson({
    schema_version: 2,
    tests: 106,
    passed: 106,
    failures: 0,
    errors: 0,
    skipped: 0,
    executed: 106,
  }));
  fs.writeFileSync(path.join(gateRoot, "pytest.xml"), "<?xml version=\"1.0\"?><testsuites tests=\"106\" failures=\"0\" errors=\"0\" skipped=\"0\"/>\n");
  return { root, sourceRoot, gateRoot };
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function withConfigMutation(fileName, mutate, action) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "evidence-v2-core-config-"));
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

test("det.evidence-v2-core is a zero-model pytest Gate in deterministic.full", () => {
  const config = loadConfiguration(REPO_ROOT);
  const gate = config.gates.gates["det.evidence-v2-core"];
  const stage = config.stages.stages.find((candidate) => candidate.id === "deterministic.full");
  assert.equal(gate.kind, "pytest");
  assert.equal(gate.result_receipt, EVIDENCE_V2_CORE_RECEIPT);
  assert.equal(gate.environment_profile, undefined);
  assert.equal(gate.pytest_args, undefined);
  assert.equal(gate.skip_policy, "forbid");
  assert.deepEqual(gate.selectors, EVIDENCE_V2_CORE_SELECTORS);
  assert.deepEqual(gate.evidence, ["pytest.xml", "pytest-summary.json", "core-verdict.json"]);
  assert.ok(stage.gates.includes("det.evidence-v2-core"));
});

test("the Core selector list is fixed, unique, and points at existing production tests", () => {
  assert.equal(new Set(EVIDENCE_V2_CORE_SELECTORS).size, EVIDENCE_V2_CORE_SELECTORS.length);
  assert.equal(EVIDENCE_V2_CORE_SELECTORS.length, 22);
  assert.match(evidenceV2CoreCasesDigest(), /^[a-f0-9]{64}$/);
  for (const selector of EVIDENCE_V2_CORE_SELECTORS) {
    const file = selector.split("::", 1)[0];
    assert.equal(fs.existsSync(path.join(REPO_ROOT, file)), true, selector);
  }
});

test("the Core suite pins the real SameJob entrance and direct report publication", () => {
  assert.ok(EVIDENCE_V2_CORE_SELECTORS.includes(
    "tests/deterministic/journey/test_rpc_timeout.py::test_r01_r14_rpc_timeout_is_one_durable_cross_module_path",
  ));
  assert.ok(EVIDENCE_V2_CORE_SELECTORS.includes(
    "tests/deterministic/unit/application/test_outcome_submission.py::test_candidate_outcome_without_review_atomically_publishes_json_and_zip",
  ));
});

test("configuration rejects Core selector or receipt drift", () => {
  assert.throws(() => withConfigMutation("gates.v2.json", (value) => {
    value.gates["det.evidence-v2-core"].selectors.pop();
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_EVIDENCE_V2_CORE_SELECTORS");
  assert.throws(() => withConfigMutation("gates.v2.json", (value) => {
    value.gates["det.evidence-v2-core"].result_receipt = "plain-pytest";
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_PYTEST_RECEIPT");
});

test("the JSON Schema closes every Core verdict object", () => {
  const schema = JSON.parse(fs.readFileSync(path.join(REPO_ROOT, "tools", "validation", "core-verdict.schema.json"), "utf8"));
  assert.equal(schema.additionalProperties, false);
  assert.equal(schema.properties.contract_manifest.additionalProperties, false);
  assert.equal(schema.properties.core_cases.additionalProperties, false);
  assert.equal(schema.properties.pytest.additionalProperties, false);
  assert.equal(schema.properties.pytest.properties.counts.additionalProperties, false);
  assert.equal(schema.properties.model_invocations.const, 0);
  assert.equal(schema.properties.status.const, "PASS");
});

test("the Test Flow action writes one canonical Core sub-receipt bound to its evidence", () => {
  const { root, sourceRoot, gateRoot } = fixture();
  try {
    const verdict = materializeEvidenceV2CoreVerdict({
      sourceSnapshotDigest: SOURCE_DIGEST,
      sourceSnapshotRoot: sourceRoot,
      gateRoot,
    });
    const verdictPath = path.join(gateRoot, "core-verdict.json");
    assert.equal(fs.readFileSync(verdictPath, "utf8"), canonicalJson(verdict));
    assert.equal(verdict.source_snapshot_digest, SOURCE_DIGEST);
    assert.equal(verdict.contract_manifest.sha256, sha256Bytes(canonicalJson({ schema_version: 9 })));
    assert.equal(verdict.core_cases.sha256, evidenceV2CoreCasesDigest());
    assert.deepEqual(verdict.pytest.counts, {
      tests: 106,
      passed: 106,
      failures: 0,
      errors: 0,
      skipped: 0,
      executed: 106,
    });
    assert.equal(validateEvidenceV2CoreVerdict(verdict, {
      sourceSnapshotDigest: SOURCE_DIGEST,
      sourceRoot,
      gateRoot,
    }), verdict);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("changing one bound Core verdict field makes validation fail", () => {
  const { root, sourceRoot, gateRoot } = fixture();
  try {
    const verdict = materializeEvidenceV2CoreVerdict({
      sourceSnapshotDigest: SOURCE_DIGEST,
      sourceSnapshotRoot: sourceRoot,
      gateRoot,
    });
    const mutations = [
      (value) => { value.source_snapshot_digest = "b".repeat(64); },
      (value) => { value.contract_manifest.sha256 = "b".repeat(64); },
      (value) => { value.core_cases.selectors[0] = `${value.core_cases.selectors[0]}-changed`; },
      (value) => { value.core_cases.sha256 = "b".repeat(64); },
      (value) => { value.pytest.summary_sha256 = "b".repeat(64); },
      (value) => { value.pytest.junit_sha256 = "b".repeat(64); },
      (value) => { value.pytest.counts.passed -= 1; },
      (value) => { value.unexpected = true; },
    ];
    for (const mutate of mutations) {
      const changed = clone(verdict);
      mutate(changed);
      assert.throws(() => validateEvidenceV2CoreVerdict(changed, {
        sourceSnapshotDigest: SOURCE_DIGEST,
        sourceRoot,
        gateRoot,
      }));
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("a receipt cannot claim PASS when pytest has a failure or skip", () => {
  const { root, sourceRoot, gateRoot } = fixture();
  try {
    const invalid = JSON.parse(fs.readFileSync(path.join(gateRoot, "pytest-summary.json"), "utf8"));
    invalid.passed = 105;
    invalid.skipped = 1;
    invalid.executed = 105;
    fs.writeFileSync(path.join(gateRoot, "pytest-summary.json"), canonicalJson(invalid));
    assert.throws(() => materializeEvidenceV2CoreVerdict({
      sourceSnapshotDigest: SOURCE_DIGEST,
      sourceSnapshotRoot: sourceRoot,
      gateRoot,
    }), (error) => error.code === "CORE_VERDICT_PYTEST_NOT_PASS");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("structural validation rejects a valid-looking extra field without filesystem context", () => {
  const { root, sourceRoot, gateRoot } = fixture();
  try {
    const verdict = materializeEvidenceV2CoreVerdict({
      sourceSnapshotDigest: SOURCE_DIGEST,
      sourceSnapshotRoot: sourceRoot,
      gateRoot,
    });
    const changed = clone(verdict);
    changed.pytest.extra = "ignored";
    assert.throws(() => validateEvidenceV2CoreVerdictSchema(changed), (error) => error.code === "CORE_VERDICT_PYTEST_FIELDS");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
