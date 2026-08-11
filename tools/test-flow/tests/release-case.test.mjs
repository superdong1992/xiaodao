import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  loadReleaseCase,
  loadReleaseCaseInputs,
  loadReleaseCaseOracle,
  releaseCaseDigests,
  verifyReleaseCaseManifest,
} from "../lib/release-case.mjs";
import { canonicalJson } from "../lib/util.mjs";


const REPOSITORY_ROOT = path.resolve(path.dirname(new URL(import.meta.url).pathname.replace(/^\/(?:([A-Za-z]:))/, "$1")), "..", "..", "..");
const CASE_ROOT = path.join(REPOSITORY_ROOT, "tests", "cases", "release", "rpc-timeout-anonymized");

function filesBelow(root) {
  const result = [];
  const visit = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name);
      if (entry.isDirectory()) visit(absolute);
      else if (entry.isFile() && entry.name !== "fixture-manifest.json") result.push(absolute);
    }
  };
  visit(root);
  return result.sort();
}

function refreshManifest(root, owner = "WIKI_DIAGNOSIS_GENERALIZATION") {
  const oldPath = path.join(root, "fixture-manifest.json");
  const previous = fs.existsSync(oldPath) ? JSON.parse(fs.readFileSync(oldPath, "utf8")) : { files: [] };
  const oldByPath = new Map(previous.files.map((entry) => [entry.path, entry]));
  const files = filesBelow(root).map((absolute) => {
    const relative = path.relative(root, absolute).split(path.sep).join("/");
    const bytes = fs.readFileSync(absolute);
    return {
      path: relative,
      purpose: oldByPath.get(relative)?.purpose ?? `Reviewed neutral fixture ${relative}.`,
      schema_ref: null,
      sha256: crypto.createHash("sha256").update(bytes).digest("hex"),
      size: bytes.length,
    };
  });
  fs.writeFileSync(oldPath, canonicalJson({
    schema_version: 1,
    owner_spec: owner,
    root: "tests/cases/release/neutral",
    files,
  }), "utf8");
}

function writeNeutralCase(root, { withAttachment, fieldName }) {
  fs.mkdirSync(path.join(root, "input"), { recursive: true });
  fs.mkdirSync(path.join(root, "approved"), { recursive: true });
  fs.mkdirSync(path.join(root, "scenarios", "one"), { recursive: true });
  fs.writeFileSync(path.join(root, "input", "wiki.md"), `# ${fieldName}\n`, "utf8");
  fs.writeFileSync(path.join(root, "input", "clarifications.md"), "# none\n", "utf8");
  fs.writeFileSync(path.join(root, "input", "spec.json"), canonicalJson({ field_name: fieldName }), "utf8");
  fs.writeFileSync(path.join(root, "approved", "SKILL.md"), "# approved\n", "utf8");
  fs.writeFileSync(path.join(root, "oracle.json"), canonicalJson({ expected_field: fieldName }), "utf8");
  fs.writeFileSync(path.join(root, "scenarios", "one", "driver.json"), canonicalJson({
    scenario_id: "one",
    problem: {
      raw_problem_text: "neutral problem",
      statement: "neutral statement",
      expected_behavior: "expected",
      actual_behavior: "observed",
      scope: "scope",
      goals: ["goal"],
      non_goals: [],
      constraints: ["constraint"],
      completion_criteria: ["criterion"],
    },
    initial_user_fact_names: [fieldName],
    initial_user_fact_values: ["value"],
    supplement_input_names: [],
    supplement_input_values: [],
    attachment_files: withAttachment ? ["archive.zip"] : [],
    attachment_anchor_names: withAttachment ? ["source_a"] : [],
  }), "utf8");
  if (withAttachment) fs.writeFileSync(path.join(root, "scenarios", "one", "archive.zip"), "neutral", "utf8");
  fs.writeFileSync(path.join(root, "scenarios", "one", "oracle.json"), canonicalJson({ status: "COMPLETE" }), "utf8");
  fs.writeFileSync(path.join(root, "case.json"), canonicalJson({
    schema_version: 1,
    case_id: `neutral-${fieldName.replaceAll("_", "-")}`,
    input_wiki: "input/wiki.md",
    clarifications: "input/clarifications.md",
    generation_spec: "input/spec.json",
    approved_skill_dir: "approved",
    semantic_oracle: "oracle.json",
    journey_scenario: "one",
    scenarios: [{ scenario_id: "one", driver: "scenarios/one/driver.json", oracle: "scenarios/one/oracle.json" }],
    allowed_actions: ["skill_generation", "specialized_diagnosis"],
  }), "utf8");
  refreshManifest(root);
}

test("the release case loader verifies hashes and keeps every oracle on an explicit gate-only path", () => {
  const verified = verifyReleaseCaseManifest(CASE_ROOT);
  const loaded = loadReleaseCase(CASE_ROOT);
  const inputs = loadReleaseCaseInputs(CASE_ROOT);
  const oracle = loadReleaseCaseOracle(CASE_ROOT);
  const digests = releaseCaseDigests(CASE_ROOT);

  assert.equal(verified.manifest.root, "tests/cases/release/rpc-timeout-anonymized");
  assert.deepEqual(loaded.scenarios.map((item) => item.scenario_id), ["complete", "partial"]);
  assert.equal(Object.hasOwn(inputs, "semantic_oracle"), false);
  assert.equal(Object.hasOwn(inputs.scenarios[0], "oracle"), false);
  assert.equal(oracle.semantic_oracle.oracle_visibility, "GATE_ONLY");
  assert.match(inputs.wiki, /\(#|（#/);
  assert.doesNotMatch(fs.readFileSync(path.join(inputs.approved_skill_dir, "SKILL.md"), "utf8"), /\(#|（#|#\)|#）/);
  assert.equal(digests.input_records.some((item) => item.path.endsWith("oracle.json")), false);
  assert.equal(digests.oracle_records.length, 3);
});

test("two heterogeneous neutral cases drive different scalar inputs and attachment shapes without framework business fields", () => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "release-case-neutral-"));
  const noAttachment = path.join(parent, "no-attachment");
  const withAttachment = path.join(parent, "with-attachment");
  writeNeutralCase(noAttachment, { withAttachment: false, fieldName: "value_x" });
  writeNeutralCase(withAttachment, { withAttachment: true, fieldName: "metric_y" });
  const first = loadReleaseCaseInputs(noAttachment);
  const second = loadReleaseCaseInputs(withAttachment);
  assert.deepEqual(first.scenarios[0].driver.initial_user_fact_names, ["value_x"]);
  assert.deepEqual(first.scenarios[0].driver.attachment_files, []);
  assert.deepEqual(second.scenarios[0].driver.initial_user_fact_names, ["metric_y"]);
  assert.deepEqual(second.scenarios[0].driver.attachment_files, ["archive.zip"]);
});

test("unknown actions, traversal, hash drift, and oracle-only changes fail closed or invalidate only the proof digest", () => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "release-case-negative-"));
  const root = path.join(parent, "case");
  fs.cpSync(CASE_ROOT, root, { recursive: true });

  const before = releaseCaseDigests(root);
  const semanticOracle = path.join(root, "oracle.json");
  const oracleValue = JSON.parse(fs.readFileSync(semanticOracle, "utf8"));
  oracleValue.test_only_note = "proof changed";
  fs.writeFileSync(semanticOracle, canonicalJson(oracleValue), "utf8");
  refreshManifest(root);
  const afterOracle = releaseCaseDigests(root);
  assert.equal(afterOracle.input_digest, before.input_digest);
  assert.notEqual(afterOracle.oracle_digest, before.oracle_digest);

  const descriptorPath = path.join(root, "case.json");
  const descriptor = JSON.parse(fs.readFileSync(descriptorPath, "utf8"));
  descriptor.allowed_actions = ["arbitrary_command"];
  fs.writeFileSync(descriptorPath, canonicalJson(descriptor), "utf8");
  refreshManifest(root);
  assert.throws(() => loadReleaseCase(root), /Release case actions are invalid/);

  descriptor.allowed_actions = ["skill_generation"];
  descriptor.input_wiki = "../escape.md";
  fs.writeFileSync(descriptorPath, canonicalJson(descriptor), "utf8");
  refreshManifest(root);
  assert.throws(() => loadReleaseCase(root), /Release case path is unsafe/);

  descriptor.input_wiki = "input/wiki.md";
  fs.writeFileSync(descriptorPath, canonicalJson(descriptor), "utf8");
  refreshManifest(root);
  fs.appendFileSync(path.join(root, "input", "wiki.md"), "drift\n", "utf8");
  assert.throws(() => verifyReleaseCaseManifest(root), /Release case (size|hash) drift/);
});

test("case business canaries do not leak into framework, runtime, or non-case tests", () => {
  const canaries = loadReleaseCaseOracle(CASE_ROOT).semantic_oracle.business_canaries;
  const roots = [
    path.join(REPOSITORY_ROOT, "src"),
    path.join(REPOSITORY_ROOT, "tools", "test-flow", "adapters"),
    path.join(REPOSITORY_ROOT, "tools", "test-flow", "config"),
    path.join(REPOSITORY_ROOT, "tools", "test-flow", "lib"),
    path.join(REPOSITORY_ROOT, "tools", "test-flow", "runtime-support"),
    path.join(REPOSITORY_ROOT, "tools", "test-flow", "tests"),
    path.join(REPOSITORY_ROOT, ".claude", "skills", "wiki-to-diagnosis-skill"),
    path.join(REPOSITORY_ROOT, "tests", "deterministic"),
    path.join(REPOSITORY_ROOT, "tests", "real"),
    path.join(REPOSITORY_ROOT, "tests", "platform"),
  ];
  for (const root of roots) {
    for (const file of filesBelow(root)) {
      const bytes = fs.readFileSync(file);
      if (bytes.includes(0)) continue;
      const text = bytes.toString("utf8");
      for (const canary of canaries) assert.equal(text.includes(canary), false, `${canary} leaked into ${file}`);
    }
  }
});
