import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  compareReleaseCaseEntries,
  diagnosisSkillRuntimeRefId,
  discoverReleaseCaseRoot,
  loadReleaseCase,
  loadReleaseCaseInputs,
  loadReleaseCaseOracle,
  methodsSkillRuntimeRefId,
  releaseCaseDigests,
  releaseCasePartition,
  verifyReleaseCaseManifest,
} from "../lib/release-case.mjs";
import { canonicalJson } from "../lib/util.mjs";

const REPOSITORY_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const RELEASE_CASES_ROOT = path.join(REPOSITORY_ROOT, "tests", "cases", "release");
const CASE_ROOT = discoverReleaseCaseRoot(RELEASE_CASES_ROOT);

function filesBelow(root) {
  const result = [];
  if (!fs.existsSync(root)) return result;
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

function refreshManifest(root) {
  const manifestPath = path.join(root, "fixture-manifest.json");
  const previous = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  const previousByPath = new Map(previous.files.map((entry) => [entry.path, entry]));
  const files = filesBelow(root).map((absolute) => {
    const relative = path.relative(root, absolute).split(path.sep).join("/");
    const bytes = fs.readFileSync(absolute);
    return {
      path: relative,
      purpose: previousByPath.get(relative)?.purpose ?? `Reviewed Methods fixture ${relative}.`,
      schema_ref: null,
      sha256: crypto.createHash("sha256").update(bytes).digest("hex"),
      size: bytes.length,
    };
  });
  fs.writeFileSync(manifestPath, canonicalJson({
    schema_version: 2,
    owner_spec: "METHODS_SKILL_RELEASE_CASE",
    root: previous.root,
    files,
  }), "utf8");
}

function cloneCase(prefix) {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  const root = path.join(parent, "case");
  fs.cpSync(CASE_ROOT, root, { recursive: true });
  return root;
}

test("Methods registration ids map to the frozen diagnosis-skill namespace", () => {
  assert.equal(diagnosisSkillRuntimeRefId("rpc-timeout-methods-v1"), "diagnosis-skill/rpc-timeout-methods-v1");
  assert.equal(methodsSkillRuntimeRefId("rpc-timeout-methods-v1"), "diagnosis-skill/rpc-timeout-methods-v1");
  for (const invalid of ["diagnosis-skill/x", "A", "has_underscore", "-leading", "trailing-"]) {
    assert.throws(() => diagnosisSkillRuntimeRefId(invalid), /Methods registration id is invalid/);
  }
});

test("release case directory ordering is ordinal and independent of host collation", () => {
  const entries = [{ name: "registration-template.json" }, { name: "SKILL.md" }];
  assert.deepEqual(entries.sort(compareReleaseCaseEntries).map((entry) => entry.name), ["SKILL.md", "registration-template.json"]);
});

test("v2 loader exposes only Wiki, registration, driver, and frozen attachments", () => {
  const verified = verifyReleaseCaseManifest(CASE_ROOT);
  const loaded = loadReleaseCase(CASE_ROOT);
  const inputs = loadReleaseCaseInputs(CASE_ROOT);
  const oracle = loadReleaseCaseOracle(CASE_ROOT);

  assert.equal(verified.manifest.schema_version, 2);
  assert.equal(loaded.journey_scenario, "multiple-rpc-timeouts");
  assert.equal(inputs.registration_template.deployment_scope, "PRODUCTION");
  assert.equal(inputs.product_registration.registration_id, "rpc-timeout-methods-v1");
  assert.equal(inputs.product_registration.runtime_ref_id, "diagnosis-skill/rpc-timeout-methods-v1");
  assert.equal(inputs.product_registration.skill_name, "diagnose-rpc-timeout");
  assert.equal(inputs.product_registration.logparse_product, "rpc-skill-feasibility");
  assert.equal(inputs.product_registration.attachment_requirement, "log_archive");
  assert.equal(inputs.registration_template.package.source_wiki_sha256, crypto.createHash("sha256").update(inputs.wiki).digest("hex"));
  assert.equal(Object.hasOwn(inputs, "semantic_oracle"), false);
  assert.equal(Object.hasOwn(inputs.scenarios[0], "oracle"), false);
  assert.equal(oracle.semantic_oracle.oracle_visibility, "GATE_ONLY");
  assert.deepEqual(
    oracle.semantic_oracle.expected_package.method_marker_sets.map((item) => item.activation_markers),
    [
      ["LATE_RESPONSE service=", "API_COMPLETE service=", "DEADLOOP_DETECTED service="],
      ["LATE_RESPONSE service=", "QUEUE_HISTORY print_time_ms="],
      ["LATE_RESPONSE service="],
    ],
  );
  assert.equal(
    oracle.semantic_oracle.expected_package.method_marker_sets
      .flatMap((item) => item.activation_markers)
      .some((marker) => marker === "rpc call" || marker === "call unsuccess, reqid("),
    false,
  );
  assert.equal(oracle.scenarios[0].oracle.oracle_visibility, "GATE_ONLY");
  assert.equal(oracle.scenarios[0].oracle.expected_status, "RESOLVED");
  assert.deepEqual(oracle.scenarios[0].oracle.expected_method_verdicts.map((item) => item.verdict), ["REJECTED", "CONFIRMED", "REJECTED"]);
  assert.deepEqual(oracle.scenarios[0].oracle.required_request_timeout, {
    marker: "call unsuccess, reqid(",
    request_id: "501",
    timeout_ms: 3000,
    unlinked_marker: "rpc call",
    unlinked_timeout_ms: 3000,
    decoy_service: "svc_catalog",
    decoy_api: "Refresh",
    decoy_request_id: "502",
    decoy_timeout_ms: 5000,
  });
  const repeatedMethodEvents = oracle.scenarios[0].oracle.required_evidence_identities
    .filter((identity) => identity.marker === "API_COMPLETE service=");
  assert.equal(repeatedMethodEvents.length, 2);
  assert.notDeepEqual(repeatedMethodEvents[0].identity_tokens, repeatedMethodEvents[1].identity_tokens);
  assert.match(inputs.wiki, /LATE_RESPONSE service=/);
  assert.deepEqual(inputs.scenarios[0].driver.initial_user_fact_names, ["problem_time", "client_process", "server_process", "service", "api"]);
  assert.deepEqual(inputs.scenarios[0].driver.supplement_input_names, []);
  assert.deepEqual(inputs.scenarios[0].attachment_paths.map((value) => path.basename(value)), ["client.log", "server.log"]);
  assert.equal(inputs.scenarios[0].driver.problem.completion_criteria.some((item) => item.includes("超时不等于取消")), false);
  assert.equal(Object.hasOwn(oracle.scenarios[0].oracle, "required_safety_phrases"), false);
});

test("registration and semantic oracle bind the same Wiki and Methods package", () => {
  const inputs = loadReleaseCaseInputs(CASE_ROOT);
  const expected = loadReleaseCaseOracle(CASE_ROOT).semantic_oracle.expected_package;
  assert.equal(expected.skill_name, inputs.registration_template.package.skill_name);
  assert.equal(expected.source_wiki_sha256, inputs.registration_template.package.source_wiki_sha256);
  assert.deepEqual(expected.required_user_inputs, inputs.scenarios[0].driver.initial_user_fact_names);
  assert.deepEqual(expected.required_artifacts, ["log_archive"]);
  assert.equal(expected.method_marker_sets.length, 3);
  for (const item of expected.method_marker_sets) {
    for (const marker of item.all_markers) assert.equal(inputs.wiki.includes(marker), true);
    let cursor = 0;
    for (const marker of item.activation_markers) {
      const index = item.all_markers.indexOf(marker, cursor);
      assert.notEqual(index, -1);
      cursor = index + 1;
    }
  }
});

test("release semantic oracle requires ordered per-method activation marker subsequences", async (context) => {
  const cases = [
    ["missing", (item) => { delete item.activation_markers; }, /expected method marker set fields are invalid/i],
    ["empty", (item) => { item.activation_markers = []; }, /activation markers must contain valid strings/i],
    ["duplicate", (item) => { item.activation_markers = ["LATE_RESPONSE service=", "LATE_RESPONSE service="]; }, /activation markers must contain valid strings/i],
    ["non-member", (item) => { item.activation_markers = ["NOT_A_METHOD_MARKER"]; }, /ordered subsequence/i],
    ["reordered", (item) => { item.activation_markers = ["API_COMPLETE service=", "LATE_RESPONSE service="]; }, /ordered subsequence/i],
  ];
  for (const [name, mutate, expectedError] of cases) {
    await context.test(name, () => {
      const root = cloneCase("methods-release-activation-");
      try {
        const oraclePath = path.join(root, "oracle.json");
        const oracle = JSON.parse(fs.readFileSync(oraclePath, "utf8"));
        mutate(oracle.expected_package.method_marker_sets[0]);
        fs.writeFileSync(oraclePath, canonicalJson(oracle), "utf8");
        refreshManifest(root);
        assert.throws(() => loadReleaseCaseOracle(root), expectedError);
      } finally {
        fs.rmSync(path.dirname(root), { recursive: true, force: true });
      }
    });
  }
});

test("release partitions bind Wiki, product registration, gate-only oracle, and journey independently", () => {
  assert.deepEqual(releaseCasePartition(CASE_ROOT, "wiki").records.map((entry) => entry.path), ["input/wiki.md"]);
  assert.deepEqual(releaseCasePartition(CASE_ROOT, "registration").records.map((entry) => entry.path), ["registration/rpc-timeout-methods-v1/registration-template.json"]);
  assert.deepEqual(releaseCasePartition(CASE_ROOT, "oracle").records.map((entry) => entry.path), ["oracle.json", "scenarios/multiple-rpc-timeouts/oracle.json"]);
  const journey = releaseCasePartition(CASE_ROOT, "journey").records.map((entry) => entry.path);
  assert.equal(journey.includes("input/wiki.md"), true);
  assert.equal(journey.includes("scenarios/multiple-rpc-timeouts/driver.json"), true);
  assert.equal(journey.some((entry) => entry.endsWith("oracle.json")), false);
  assert.throws(() => releaseCasePartition(CASE_ROOT, "approved"), /Unknown Release case partition/);
});

test("oracle-only changes invalidate only the proof digest", () => {
  const root = cloneCase("methods-release-oracle-");
  const before = releaseCaseDigests(root);
  const oraclePath = path.join(root, "oracle.json");
  const oracle = JSON.parse(fs.readFileSync(oraclePath, "utf8"));
  oracle.business_canaries.push("TEMPORARY_ORACLE_MUTATION");
  fs.writeFileSync(oraclePath, canonicalJson(oracle), "utf8");
  refreshManifest(root);
  const after = releaseCaseDigests(root);
  assert.equal(after.input_digest, before.input_digest);
  assert.notEqual(after.oracle_digest, before.oracle_digest);
  assert.notEqual(after.all_digest, before.all_digest);
});

test("legacy descriptors, unknown actions, traversal, and manifest drift fail closed", () => {
  const root = cloneCase("methods-release-negative-");
  const descriptorPath = path.join(root, "case.json");
  const descriptor = JSON.parse(fs.readFileSync(descriptorPath, "utf8"));
  descriptor.schema_version = 1;
  descriptor.generation_spec = "input/generation-spec.json";
  fs.writeFileSync(descriptorPath, canonicalJson(descriptor), "utf8");
  refreshManifest(root);
  assert.throws(() => loadReleaseCase(root), /descriptor fields are invalid/);

  delete descriptor.generation_spec;
  descriptor.schema_version = 2;
  descriptor.allowed_actions = ["skill_generation"];
  fs.writeFileSync(descriptorPath, canonicalJson(descriptor), "utf8");
  refreshManifest(root);
  assert.throws(() => loadReleaseCase(root), /actions are invalid/);

  descriptor.allowed_actions = ["methods_skill_generation"];
  descriptor.input_wiki = "../escape.md";
  fs.writeFileSync(descriptorPath, canonicalJson(descriptor), "utf8");
  refreshManifest(root);
  assert.throws(() => loadReleaseCase(root), /path is unsafe/);

  descriptor.input_wiki = "input/wiki.md";
  fs.writeFileSync(descriptorPath, canonicalJson(descriptor), "utf8");
  refreshManifest(root);
  fs.appendFileSync(path.join(root, "input", "wiki.md"), "drift\n", "utf8");
  assert.throws(() => verifyReleaseCaseManifest(root), /Release case (size|hash) drift/);
});

test("registration identity, Wiki digest, and file roles fail closed on drift or alias", () => {
  const drift = cloneCase("methods-release-registration-");
  const registrationPath = path.join(drift, "registration", "rpc-timeout-methods-v1", "registration-template.json");
  const registration = JSON.parse(fs.readFileSync(registrationPath, "utf8"));
  registration.package.source_wiki_sha256 = "0".repeat(64);
  fs.writeFileSync(registrationPath, canonicalJson(registration), "utf8");
  refreshManifest(drift);
  assert.throws(() => loadReleaseCase(drift), /Wiki digest differs/);

  const alias = cloneCase("methods-release-alias-");
  const descriptorPath = path.join(alias, "case.json");
  const descriptor = JSON.parse(fs.readFileSync(descriptorPath, "utf8"));
  descriptor.registration_template = descriptor.input_wiki;
  fs.writeFileSync(descriptorPath, canonicalJson(descriptor), "utf8");
  refreshManifest(alias);
  assert.throws(() => loadReleaseCase(alias), /file roles must be mutually exclusive/);

  const attachmentAlias = cloneCase("methods-release-attachment-alias-");
  const driverPath = path.join(attachmentAlias, "scenarios", "multiple-rpc-timeouts", "driver.json");
  const driver = JSON.parse(fs.readFileSync(driverPath, "utf8"));
  driver.attachment_files = ["oracle.json"];
  driver.attachment_anchor_names = ["client"];
  fs.writeFileSync(driverPath, canonicalJson(driver), "utf8");
  refreshManifest(attachmentAlias);
  assert.throws(() => loadReleaseCaseInputs(attachmentAlias), /attachment aliases an input or oracle role/);
});

test("scenario evidence identities must resolve to distinct frozen log events", () => {
  const root = cloneCase("methods-release-event-identity-");
  const oraclePath = path.join(root, "scenarios", "multiple-rpc-timeouts", "oracle.json");
  const oracle = JSON.parse(fs.readFileSync(oraclePath, "utf8"));
  const apiIdentities = oracle.required_evidence_identities.filter((identity) => identity.marker === "API_COMPLETE service=");
  assert.equal(apiIdentities.length, 2);
  apiIdentities[1].identity_tokens = [...apiIdentities[0].identity_tokens];
  fs.writeFileSync(oraclePath, canonicalJson(oracle), "utf8");
  refreshManifest(root);
  assert.throws(
    () => loadReleaseCaseOracle(root),
    /evidence identities must name distinct frozen log events/,
  );
});

test("Evidence V2 scenario oracle hard-cuts RESOLVED, exact semantic verdicts, and canonical markers", () => {
  const mutations = [
    {
      change: (oracle) => { oracle.expected_status = "CONFIRMED"; },
      expected: /must expect RESOLVED/,
    },
    {
      change: (oracle) => { oracle.expected_method_verdicts[0].verdict = "UNKNOWN"; },
      expected: /method verdict is invalid/,
    },
    {
      change: (oracle) => { oracle.required_evidence_identities[0].marker = "LATE_RESPONSE"; },
      expected: /not an exact method marker/,
    },
    {
      change: (oracle) => { oracle.required_safety_phrases = ["超时不等于取消"]; },
      expected: /fields are invalid/,
    },
  ];
  for (const { change, expected } of mutations) {
    const root = cloneCase("methods-release-v2-oracle-");
    const oraclePath = path.join(root, "scenarios", "multiple-rpc-timeouts", "oracle.json");
    const oracle = JSON.parse(fs.readFileSync(oraclePath, "utf8"));
    change(oracle);
    fs.writeFileSync(oraclePath, canonicalJson(oracle), "utf8");
    refreshManifest(root);
    assert.throws(() => loadReleaseCaseOracle(root), expected);
  }
});

test("gate-only business canaries do not leak into product, Skills, adapters, or non-case tests", () => {
  const canaries = loadReleaseCaseOracle(CASE_ROOT).semantic_oracle.business_canaries;
  const roots = [
    path.join(REPOSITORY_ROOT, "src"),
    path.join(REPOSITORY_ROOT, ".agents", "skills"),
    path.join(REPOSITORY_ROOT, "tools", "test-flow", "adapters"),
    path.join(REPOSITORY_ROOT, "tools", "test-flow", "lib"),
    path.join(REPOSITORY_ROOT, "tools", "test-flow", "runtime-support"),
    path.join(REPOSITORY_ROOT, "tests", "deterministic"),
    path.join(REPOSITORY_ROOT, "tests", "real"),
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
