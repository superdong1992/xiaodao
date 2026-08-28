import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  attachEvidenceV2ModelCert,
  executeGate,
  materializeEvidenceV2CoreVerdict,
  materializeEvidenceV2ModelCert,
  materializeEvidenceV2ReleaseVerdict,
} from "../lib/actions.mjs";
import { loadConfiguration } from "../lib/config.mjs";
import { sumUsage } from "../lib/usage.mjs";
import {
  canonicalJson,
  resolvePythonTestRuntime,
  sha256Bytes,
  sha256File,
  writeJsonSync,
} from "../lib/util.mjs";
import {
  buildEvidenceV2ReleaseVerdict,
  EVIDENCE_V2_CORE_VERDICT_PATH,
  EVIDENCE_V2_MODEL_CERT_FILENAME,
  EVIDENCE_V2_MODEL_CERT_INPUT_FILENAME,
  EVIDENCE_V2_MODEL_CERT_RECEIPT,
  EVIDENCE_V2_RELEASE_VERDICT_FILENAME,
  validateEvidenceV2ModelCert,
  validateEvidenceV2ModelCertInput,
  validateEvidenceV2ModelCertInputSchema,
  validateEvidenceV2ModelCertSchema,
  validateEvidenceV2ReleaseVerdict,
  validateEvidenceV2ReleaseVerdictSchema,
} from "../../validation/evidence-v2-certification.mjs";
import {
  EVIDENCE_V2_SCENARIO_ORACLE_RECEIPT_FILENAME,
  buildEvidenceV2ScenarioOracleReceipt,
} from "../../validation/evidence-v2-scenario-oracle.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const SOURCE_DIGEST = "a".repeat(64);

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function digest(label) {
  return sha256Bytes(`${label}\n`);
}

function usage(seed) {
  return {
    schema_version: 1,
    input_tokens: 100 + seed,
    output_tokens: 10 + seed,
    cache_creation_input_tokens: seed,
    cache_read_input_tokens: seed * 2,
    total_tokens: 110 + seed * 5,
    cost_usd: seed / 100,
  };
}

function invocation(target, ordinal, role, attempt) {
  return {
    invocation_id: `${target.toLowerCase()}-${ordinal}-${role.toLowerCase()}-${attempt.toLowerCase()}`,
    ordinal,
    role,
    attempt,
    prompt: {
      sha256: digest(`${target}-${ordinal}-prompt`),
      size: 200 + ordinal,
    },
    usage: usage(ordinal),
  };
}

function methodsResult(runtimeReceipt) {
  const methods = runtimeReceipt.methods_result_identity;
  return {
    canonical_sha256: methods.sha256,
    canonical_size: methods.size,
    case_id: methods.case_id,
    source_job_id: methods.source_job_id,
    result_ref: methods.result_ref,
    evaluation_id: methods.evaluation_id,
    status: methods.status,
    plan_ref: methods.plan_ref,
    evidence_graph_ref: methods.evidence_graph_ref,
    diagnostic_id: methods.diagnostic_id,
  };
}

function modelInput({ target, manifestSha256, coreSha256, runtimeReceipt, scenarioOracleSha256 }) {
  const invocations = [
    invocation(target, 1, "SPECIALIST", "PRIMARY"),
    invocation(target, 2, "REVIEWER", "PRIMARY"),
  ];
  return {
    schema_version: 1,
    receipt_type: "evidence-v2-model-cert-input",
    status: "PASS",
    certification_target: target,
    source_snapshot_digest: SOURCE_DIGEST,
    contract_manifest: {
      path: "schemas/v2/contract-manifest.json",
      sha256: manifestSha256,
    },
    core_verdict: {
      path: EVIDENCE_V2_CORE_VERDICT_PATH,
      sha256: coreSha256,
    },
    scenario_oracle: { path: EVIDENCE_V2_SCENARIO_ORACLE_RECEIPT_FILENAME, sha256: scenarioOracleSha256 },
    scenario: runtimeReceipt.scenario,
    provider: target === "P1"
      ? { id: "deepseek", transport: "claude-code-compatible-api" }
      : { id: "openai", transport: "codex-app-server" },
    model: target === "P1"
      ? { id: "deepseek-v4-flash[1m]", revision: digest("p1-settings"), revision_source: "settings-fingerprint" }
      : { id: "gpt-5.6-luna", revision: digest("p2-codex-runtime"), revision_source: "frozen-codex-cli-and-app-server-runtime-identity" },
    execution_identity: {
      runtime: { id: `${target.toLowerCase()}-diagnosis-runtime-v2`, sha256: digest(`${target}-runtime`) },
      prompt_policy: { id: "evidence-v2-role-prompts", sha256: digest(`${target}-prompt-policy`) },
      profile: { id: `${target.toLowerCase()}-profile`, sha256: digest(`${target}-profile`) },
      tool_policy: { id: "evidence-v2-read-write", sha256: digest(`${target}-tool-policy`) },
    },
    invocations,
    call_counts: {
      total_calls: 2,
      specialist_calls: 1,
      reviewer_calls: 1,
      specialist_repairs: 0,
      reviewer_repairs: 0,
      model_retries: 0,
    },
    usage: sumUsage(invocations.map((value) => value.usage)),
    methods_result: methodsResult(runtimeReceipt),
  };
}

function certificationRoot(artifactRoot, target) {
  const stage = target === "P1"
    ? "real.macos-claude-deepseek-e2e"
    : "real.macos-codex-luna-e2e";
  return path.join(artifactRoot, "payload", "stages", stage, "gates", stage);
}

let productionTemplate = null;

function pythonRuntime() {
  const configuredEnvironment = process.env.TEST_FLOW_QUICK_PYTHON
    ? { ...process.env, TEST_FLOW_PYTHON: process.env.TEST_FLOW_QUICK_PYTHON }
    : process.env;
  const resolved = resolvePythonTestRuntime(REPO_ROOT, configuredEnvironment);
  if (resolved !== null) return { command: resolved.command, prefix: resolved.interpreterPrefix };
  const bundled = process.platform === "win32"
    ? path.join(os.homedir(), ".cache", "codex-runtimes", "codex-primary-runtime", "dependencies", "python", "python.exe")
    : null;
  assert.ok(bundled !== null && fs.existsSync(bundled), "production semantic-oracle test requires a Python 3.12 runtime");
  return { command: bundled, prefix: [] };
}

function writeReleaseRegistration(root) {
  const caseRoot = path.join(REPO_ROOT, "tests", "cases", "release", "rpc-timeout-anonymized");
  const registration = JSON.parse(fs.readFileSync(path.join(caseRoot, "registration", "rpc-timeout-methods-v1", "registration-template.json"), "utf8"));
  const packageRoot = path.join(root, "package", "diagnose-rpc-timeout");
  const references = path.join(packageRoot, "references");
  fs.mkdirSync(references, { recursive: true });
  fs.writeFileSync(path.join(root, "registration-template.json"), `${JSON.stringify(registration, null, 2)}\n`);
  const expected = JSON.parse(fs.readFileSync(path.join(caseRoot, "oracle.json"), "utf8")).expected_package;
  const methods = {
    schema_version: 1,
    skill_name: expected.skill_name,
    source_wiki_sha256: expected.source_wiki_sha256,
    required_user_inputs: expected.required_user_inputs,
    required_artifacts: expected.required_artifacts,
    log_derived_fields: expected.required_log_derived_fields,
    shared_references: ["references/shared-boundaries.md"],
    methods: [
      { id: "api-execution-slow", title: "API 执行时间过长", reference: "references/api-execution-slow.md", priority: 1, evidence_markers: expected.method_marker_sets[0].all_markers },
      { id: "server-queueing", title: "服务端收包排队", reference: "references/server-queueing.md", priority: 2, evidence_markers: expected.method_marker_sets[1].all_markers },
      { id: "client-receive-blocked", title: "客户端收包线程阻塞", reference: "references/client-receive-blocked.md", priority: 3, evidence_markers: expected.method_marker_sets[2].all_markers },
    ],
  };
  fs.writeFileSync(path.join(packageRoot, "methods.json"), `${JSON.stringify(methods, null, 2)}\n`);
  fs.writeFileSync(path.join(packageRoot, "SKILL.md"), "---\nname: diagnose-rpc-timeout\ndescription: Test-owned production Runtime fixture.\n---\n\nRead request.json, method-evidence-graph.json, and method-evaluation-plan.json. Return only evaluation_ref, verdict, and reason; UNKNOWN is allowed.\n");
  const card = ["## 适用条件\n固定用例。", "## 所需证据\n使用方法 marker。", "## 计算与判断\n按冻结 Evidence Graph 判断。", "## 确认条件\n存在正向证据。", "## 未知边界\n证据不足时 UNKNOWN。", "## 输出含义\n输出 evaluation verdict。"].join("\n\n");
  for (const name of ["api-execution-slow.md", "server-queueing.md", "client-receive-blocked.md"]) fs.writeFileSync(path.join(references, name), `${card}\n`);
  fs.writeFileSync(path.join(references, "shared-boundaries.md"), "RPC 超时不等于取消。\n");
}

function productionBundle() {
  if (productionTemplate !== null) return productionTemplate;
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "evidence-v2-production-cert-"));
  const registrationRoot = path.join(root, "registration");
  const evidenceRoot = path.join(root, "evidence");
  writeReleaseRegistration(registrationRoot);
  const script = path.join(REPO_ROOT, "tools", "test-flow", "quick-validation", "codex-luna", "runtime", "macos_codex_luna_model_cert_driver.py");
  const receiptPath = path.join(evidenceRoot, "runtime-receipt.json");
  const bootstrap = "import runpy,sys,types; mark=types.SimpleNamespace(parametrize=lambda *a,**k:(lambda f:f)); sys.modules['pytest']=types.SimpleNamespace(fixture=lambda f:f,mark=mark); script=sys.argv[1]; sys.argv=sys.argv[1:]; runpy.run_path(script,run_name='__main__')";
  const python = pythonRuntime();
  const result = spawnSync(python.command, [
    ...python.prefix, "-c", bootstrap,
    script,
    "--mode", "fake",
    "--source-root", REPO_ROOT,
    "--registration-root", registrationRoot,
    "--work-root", path.join(root, "work"),
    "--receipt-path", receiptPath,
    "--evidence-root", evidenceRoot,
    "--fake-rejected-method-id", "server-queueing",
  ], { cwd: REPO_ROOT, env: process.env, encoding: "utf8", timeout: 120_000 });
  assert.equal(result.status, 0, result.stderr);
  productionTemplate = { root, evidenceRoot, runtimeReceipt: JSON.parse(fs.readFileSync(receiptPath, "utf8")) };
  return productionTemplate;
}

test.after(() => {
  if (productionTemplate !== null) fs.rmSync(productionTemplate.root, { recursive: true, force: true });
});

function fixture() {
  const production = productionBundle();
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "evidence-v2-certification-"));
  const sourceRoot = REPO_ROOT;
  const artifactRoot = path.join(root, "artifact");
  const coreRoot = path.join(artifactRoot, ...path.dirname(EVIDENCE_V2_CORE_VERDICT_PATH).split("/"));
  fs.mkdirSync(coreRoot, { recursive: true });
  const manifestPath = path.join(sourceRoot, "schemas", "v2", "contract-manifest.json");
  fs.writeFileSync(path.join(coreRoot, "pytest-summary.json"), canonicalJson({
    schema_version: 2,
    tests: 106,
    passed: 106,
    failures: 0,
    errors: 0,
    skipped: 0,
    executed: 106,
  }));
  fs.writeFileSync(path.join(coreRoot, "pytest.xml"), "<?xml version=\"1.0\"?><testsuites tests=\"106\" failures=\"0\" errors=\"0\" skipped=\"0\"/>\n");
  const core = materializeEvidenceV2CoreVerdict({
    sourceSnapshotDigest: SOURCE_DIGEST,
    sourceSnapshotRoot: sourceRoot,
    gateRoot: coreRoot,
  });
  const coreVerdictPath = path.join(coreRoot, "core-verdict.json");
  const certs = {};
  for (const target of ["P1", "P2"]) {
    const certRoot = certificationRoot(artifactRoot, target);
    fs.mkdirSync(certRoot, { recursive: true });
    for (const name of fs.readdirSync(production.evidenceRoot)) {
      if (["runtime-receipt.json"].includes(name)) continue;
      fs.copyFileSync(path.join(production.evidenceRoot, name), path.join(certRoot, name));
    }
    const provisional = modelInput({
      target,
      manifestSha256: core.contract_manifest.sha256,
      coreSha256: sha256File(coreVerdictPath),
      runtimeReceipt: production.runtimeReceipt,
      scenarioOracleSha256: "0".repeat(64),
    });
    const scenarioOracle = buildEvidenceV2ScenarioOracleReceipt({
      sourceRoot,
      certRoot,
      scenario: provisional.scenario,
      providerInvocations: provisional.invocations,
      modelId: provisional.model.id,
    });
    writeJsonSync(path.join(certRoot, EVIDENCE_V2_SCENARIO_ORACLE_RECEIPT_FILENAME), scenarioOracle);
    writeJsonSync(path.join(certRoot, EVIDENCE_V2_MODEL_CERT_INPUT_FILENAME), {
      ...provisional,
      scenario_oracle: {
        path: EVIDENCE_V2_SCENARIO_ORACLE_RECEIPT_FILENAME,
        sha256: sha256File(path.join(certRoot, EVIDENCE_V2_SCENARIO_ORACLE_RECEIPT_FILENAME)),
      },
    });
    const cert = materializeEvidenceV2ModelCert({
      certificationTarget: target,
      sourceSnapshotDigest: SOURCE_DIGEST,
      sourceSnapshotRoot: sourceRoot,
      attemptRoot: artifactRoot,
      gateRoot: certRoot,
    });
    certs[target] = {
      cert,
      certRoot,
      certPath: path.join(certRoot, EVIDENCE_V2_MODEL_CERT_FILENAME),
      inputPath: path.join(certRoot, EVIDENCE_V2_MODEL_CERT_INPUT_FILENAME),
    };
  }
  return {
    root,
    sourceRoot,
    artifactRoot,
    manifestPath,
    coreRoot,
    coreVerdictPath,
    certs,
  };
}

function withConfigMutation(fileName, mutate, action) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "evidence-v2-cert-config-"));
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

test("P1 and P2 Gates require the replayable scenario oracle after Skill generation", () => {
  const config = loadConfiguration(REPO_ROOT);
  const expected = [
    ["P1", "real.macos-claude-deepseek-e2e"],
    ["P2", "real.macos-codex-luna-e2e"],
  ];
  for (const [target, id] of expected) {
    const gate = config.gates.gates[id];
    const stage = config.stages.stages.find((value) => value.id === id);
    assert.equal(gate.result_receipt, EVIDENCE_V2_MODEL_CERT_RECEIPT);
    assert.equal(gate.certification_target, target);
    assert.ok(gate.evidence.includes(EVIDENCE_V2_MODEL_CERT_INPUT_FILENAME));
    assert.ok(gate.evidence.includes(EVIDENCE_V2_MODEL_CERT_FILENAME));
    assert.ok(gate.evidence.includes(EVIDENCE_V2_SCENARIO_ORACLE_RECEIPT_FILENAME));
    assert.ok(gate.evidence.includes("methods.json"));
    assert.ok(gate.evidence.includes("methods-result-v2.json"));
    assert.ok(stage.depends_on.includes("deterministic.full"));
    assert.ok(stage.depends_on.includes("real.skill-generation"));
    assert.equal(Object.hasOwn(stage, "admission_blocker"), false);
  }
});

test("configuration rejects model certification evidence or Core dependency drift", () => {
  assert.throws(() => withConfigMutation("gates.v2.json", (value) => {
    value.gates["real.macos-codex-luna-e2e"].evidence.pop();
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_MODEL_CERT_EVIDENCE");
  assert.throws(() => withConfigMutation("stages.v2.json", (value) => {
    value.stages.find((stage) => stage.id === "real.macos-claude-deepseek-e2e").depends_on = [];
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_MODEL_CERT_CORE_DEPENDENCY");
});

test("shared Test Flow builders materialize P1, P2, and the final release verdict without a model", () => {
  const value = fixture();
  try {
    for (const target of ["P1", "P2"]) {
      const { cert, certPath, certRoot } = value.certs[target];
      assert.equal(fs.readFileSync(certPath, "utf8"), canonicalJson(cert));
      assert.equal(cert.certification_target, target);
      assert.equal(cert.source_snapshot_digest, SOURCE_DIGEST);
      assert.equal(cert.core_verdict.sha256, sha256File(value.coreVerdictPath));
      assert.equal(validateEvidenceV2ModelCert(cert, {
        certificationTarget: target,
        sourceSnapshotDigest: SOURCE_DIGEST,
        sourceRoot: value.sourceRoot,
        coreVerdictPath: value.coreVerdictPath,
        certRoot,
      }), cert);
    }
    const verdict = materializeEvidenceV2ReleaseVerdict({
      sourceSnapshotDigest: SOURCE_DIGEST,
      sourceSnapshotRoot: value.sourceRoot,
      artifactRoot: value.artifactRoot,
      coreVerdictPath: value.coreVerdictPath,
      p1ModelCertPath: value.certs.P1.certPath,
      p2ModelCertPath: value.certs.P2.certPath,
    });
    const verdictPath = path.join(value.artifactRoot, EVIDENCE_V2_RELEASE_VERDICT_FILENAME);
    assert.equal(fs.readFileSync(verdictPath, "utf8"), canonicalJson(verdict));
    assert.deepEqual(verdict.model_certs.map((cert) => cert.certification_target), ["P1", "P2"]);
    assert.equal(verdict.core_verdict.sha256, value.certs.P1.cert.core_verdict.sha256);
    assert.equal(verdict.core_verdict.sha256, value.certs.P2.cert.core_verdict.sha256);
    assert.deepEqual(verdict.scenario, value.certs.P1.cert.scenario);
    assert.deepEqual(verdict.scenario, value.certs.P2.cert.scenario);
    assert.equal(validateEvidenceV2ReleaseVerdict(verdict, {
      sourceSnapshotDigest: SOURCE_DIGEST,
      sourceRoot: value.sourceRoot,
      artifactRoot: value.artifactRoot,
    }), verdict);
  } finally {
    fs.rmSync(value.root, { recursive: true, force: true });
  }
});

test("central attach validates the provider-owned model cert without rewriting it", () => {
  const value = fixture();
  try {
    for (const target of ["P1", "P2"]) {
      const cert = value.certs[target];
      const before = fs.readFileSync(cert.certPath);
      const result = attachEvidenceV2ModelCert({ status: "PASS" }, {
        context: {
          attemptRoot: value.artifactRoot,
          sourceSnapshotDigest: SOURCE_DIGEST,
          sourceSnapshotRoot: value.sourceRoot,
        },
        gate: { result_receipt: EVIDENCE_V2_MODEL_CERT_RECEIPT, certification_target: target },
        gateRoot: cert.certRoot,
      });
      assert.equal(result.status, "PASS");
      assert.equal(result.model_cert.certification_target, target);
      assert.deepEqual(fs.readFileSync(cert.certPath), before);
    }
  } finally {
    fs.rmSync(value.root, { recursive: true, force: true });
  }
});

test("central attach rejects a missing or changed provider-owned model cert", () => {
  for (const mutation of ["missing", "changed"]) {
    const value = fixture();
    try {
      const cert = value.certs.P1;
      if (mutation === "missing") fs.rmSync(cert.certPath);
      else {
        const changed = readJson(cert.certPath);
        changed.model.revision = digest("changed-provider-cert");
        fs.writeFileSync(cert.certPath, canonicalJson(changed));
      }
      const result = attachEvidenceV2ModelCert({ status: "PASS" }, {
        context: {
          attemptRoot: value.artifactRoot,
          sourceSnapshotDigest: SOURCE_DIGEST,
          sourceSnapshotRoot: value.sourceRoot,
        },
        gate: { result_receipt: EVIDENCE_V2_MODEL_CERT_RECEIPT, certification_target: "P1" },
        gateRoot: cert.certRoot,
      });
      assert.equal(result.status, "ERROR");
      assert.equal(result.failure_domain, "HARNESS");
      if (mutation === "missing") assert.equal(result.code, "EVIDENCE_V2_MODEL_CERT_MISSING");
      else assert.equal(result.code, "MODEL_CERT_ADAPTER_INPUT_MISMATCH");
    } finally {
      fs.rmSync(value.root, { recursive: true, force: true });
    }
  }
});

test("central release-verdict Gate routes the same-attempt Core, P1, and P2 receipts", async () => {
  const value = fixture();
  try {
    const stage = { id: "evidence-v2.release-verdict" };
    const gateId = "evidence-v2.release-verdict";
    const result = await executeGate({
      attemptRoot: value.artifactRoot,
      sourceSnapshotDigest: SOURCE_DIGEST,
      sourceSnapshotRoot: value.sourceRoot,
    }, stage, gateId, {
      kind: "capability-adapter",
      adapter: "evidence-v2-release-verdict",
    });
    assert.equal(result.status, "PASS");
    assert.deepEqual(result.adapter_receipt.model_cert_targets, ["P1", "P2"]);
    const verdictPath = path.join(
      value.artifactRoot,
      "payload", "stages", stage.id, "gates", gateId,
      EVIDENCE_V2_RELEASE_VERDICT_FILENAME,
    );
    assert.equal(fs.existsSync(verdictPath), true);
    assert.equal(validateEvidenceV2ReleaseVerdict(readJson(verdictPath), {
      sourceSnapshotDigest: SOURCE_DIGEST,
      sourceRoot: value.sourceRoot,
      artifactRoot: value.artifactRoot,
    }).status, "PASS");
  } finally {
    fs.rmSync(value.root, { recursive: true, force: true });
  }
});

test("model certification rejects one-field identity, topology, usage, and result mutations", () => {
  const value = fixture();
  try {
    const baseline = readJson(value.certs.P1.inputPath);
    const mutations = [
      (item) => { item.provider.id = ""; },
      (item) => { item.provider.id = "openai"; },
      (item) => { item.model.revision = ""; },
      (item) => { item.execution_identity.prompt_policy.sha256 = "b".repeat(63); },
      (item) => { item.execution_identity.profile.extra = true; },
      (item) => { item.execution_identity.tool_policy.id = ""; },
      (item) => { item.invocations[1].attempt = "REPAIR"; },
      (item) => { item.invocations[0].prompt.size = 0; },
      (item) => { item.invocations[0].usage.total_tokens += 1; },
      (item) => { item.call_counts.specialist_repairs = 1; },
      (item) => { item.usage.output_tokens += 1; item.usage.total_tokens += 1; },
      (item) => { item.scenario.scenario_id = "another-scenario"; },
      (item) => { item.scenario.source_wiki_sha256 = "b".repeat(63); },
      (item) => { item.scenario.sources[1].source_id = item.scenario.sources[0].source_id; },
      (item) => { item.scenario.evidence_graph.ref = `graph-${"b".repeat(64)}`; },
      (item) => { item.methods_result.plan_ref = `plan-${"b".repeat(64)}`; },
      (item) => { item.methods_result.result_ref = `result-${"b".repeat(63)}`; },
      (item) => { item.methods_result.canonical_sha256 = "b".repeat(63); },
      (item) => { item.unexpected = true; },
    ];
    for (const mutate of mutations) {
      const changed = clone(baseline);
      mutate(changed);
      assert.throws(() => validateEvidenceV2ModelCertInputSchema(changed, { certificationTarget: "P1" }));
    }
    const productOpaqueIds = clone(baseline);
    productOpaqueIds.methods_result.case_id = "00000000-0000-0000-0000-000000000001";
    productOpaqueIds.methods_result.source_job_id = "00000000-0000-0000-0000-000000000002";
    productOpaqueIds.methods_result.evaluation_id = "00000000-0000-0000-0000-000000000003";
    assert.equal(
      validateEvidenceV2ModelCertInputSchema(productOpaqueIds, { certificationTarget: "P1" }),
      productOpaqueIds,
    );
    const bindingMutations = [
      (item) => { item.source_snapshot_digest = "b".repeat(64); },
      (item) => { item.contract_manifest.sha256 = "b".repeat(64); },
      (item) => { item.core_verdict.sha256 = "b".repeat(64); },
      (item) => { item.scenario_oracle.sha256 = "b".repeat(64); },
    ];
    for (const mutate of bindingMutations) {
      const changed = clone(baseline);
      mutate(changed);
      assert.throws(() => validateEvidenceV2ModelCertInput(changed, {
        certificationTarget: "P1",
        sourceSnapshotDigest: SOURCE_DIGEST,
        sourceRoot: value.sourceRoot,
        coreVerdictPath: value.coreVerdictPath,
        certRoot: value.certs.P1.certRoot,
      }));
    }
    const changedCert = clone(value.certs.P1.cert);
    changedCert.model.revision = digest("another-valid-revision");
    assert.equal(validateEvidenceV2ModelCertSchema(changedCert, {
      certificationTarget: "P1",
    }), changedCert);
    assert.throws(() => validateEvidenceV2ModelCert(changedCert, {
      certificationTarget: "P1",
      sourceSnapshotDigest: SOURCE_DIGEST,
      sourceRoot: value.sourceRoot,
      coreVerdictPath: value.coreVerdictPath,
      certRoot: value.certs.P1.certRoot,
    }), (error) => error.code === "MODEL_CERT_ADAPTER_INPUT_MISMATCH");
  } finally {
    fs.rmSync(value.root, { recursive: true, force: true });
  }
});

test("model certification replay rejects scenario identity drift before release aggregation", () => {
  const mutations = [
    (item) => { item.scenario.skill_content_sha256 = digest("another-registered-skill"); },
    (item) => { item.scenario.sources[0].content_sha256 = digest("another-client.log"); },
  ];
  for (const mutate of mutations) {
    const value = fixture();
    try {
      const changedInput = readJson(value.certs.P2.inputPath);
      mutate(changedInput);
      fs.writeFileSync(value.certs.P2.inputPath, canonicalJson(changedInput));
      fs.rmSync(value.certs.P2.certPath);
      assert.throws(() => materializeEvidenceV2ModelCert({
        certificationTarget: "P2",
        sourceSnapshotDigest: SOURCE_DIGEST,
        sourceSnapshotRoot: value.sourceRoot,
        attemptRoot: value.artifactRoot,
        gateRoot: value.certs.P2.certRoot,
      }));
    } finally {
      fs.rmSync(value.root, { recursive: true, force: true });
    }
  }
});

test("release verdict exists only for PASS Core plus one exact P1 and P2 certification", () => {
  const value = fixture();
  try {
    const verdict = buildEvidenceV2ReleaseVerdict({
      sourceSnapshotDigest: SOURCE_DIGEST,
      sourceRoot: value.sourceRoot,
      artifactRoot: value.artifactRoot,
      coreVerdictPath: value.coreVerdictPath,
      p1ModelCertPath: value.certs.P1.certPath,
      p2ModelCertPath: value.certs.P2.certPath,
    });
    const schemaMutations = [
      (item) => { item.status = "FAIL"; },
      (item) => { item.model_certs.pop(); },
      (item) => { item.model_certs[1].certification_target = "P1"; },
      (item) => { item.unexpected = true; },
    ];
    for (const mutate of schemaMutations) {
      const changed = clone(verdict);
      mutate(changed);
      assert.throws(() => validateEvidenceV2ReleaseVerdictSchema(changed));
    }
    const boundMutations = [
      (item) => { item.source_snapshot_digest = "b".repeat(64); },
      (item) => { item.contract_manifest.sha256 = "b".repeat(64); },
      (item) => { item.core_verdict.sha256 = "b".repeat(64); },
      (item) => { item.scenario.skill_content_sha256 = "b".repeat(64); },
      (item) => { item.model_certs[0].sha256 = "b".repeat(64); },
      (item) => { item.model_certs[0].provider.id = "other"; },
      (item) => { item.model_certs[0].scenario_oracle.sha256 = "b".repeat(64); },
      (item) => { item.model_certs[1].model.revision = "other"; },
      (item) => { item.model_certs[1].methods_result.diagnostic_id = `diag-${"b".repeat(64)}`; },
    ];
    for (const mutate of boundMutations) {
      const changed = clone(verdict);
      mutate(changed);
      assert.throws(() => validateEvidenceV2ReleaseVerdict(changed, {
        sourceSnapshotDigest: SOURCE_DIGEST,
        sourceRoot: value.sourceRoot,
        artifactRoot: value.artifactRoot,
      }));
    }
    const invalidP2 = clone(value.certs.P2.cert);
    invalidP2.status = "FAIL";
    fs.writeFileSync(value.certs.P2.certPath, canonicalJson(invalidP2));
    assert.throws(() => buildEvidenceV2ReleaseVerdict({
      sourceSnapshotDigest: SOURCE_DIGEST,
      sourceRoot: value.sourceRoot,
      artifactRoot: value.artifactRoot,
      coreVerdictPath: value.coreVerdictPath,
      p1ModelCertPath: value.certs.P1.certPath,
      p2ModelCertPath: value.certs.P2.certPath,
    }), (error) => error.code === "MODEL_CERT_STATUS");
  } finally {
    fs.rmSync(value.root, { recursive: true, force: true });
  }
});

test("JSON Schemas close every shared certification receipt root", () => {
  const schemas = Object.fromEntries([
    "model-cert-input.schema.json",
    "model-cert.schema.json",
    "release-verdict.schema.json",
  ].map((name) => [name, JSON.parse(fs.readFileSync(path.join(REPO_ROOT, "tools", "validation", name), "utf8"))]));
  assert.equal(schemas["model-cert-input.schema.json"].additionalProperties, false);
  assert.equal(schemas["model-cert.schema.json"].additionalProperties, false);
  assert.equal(schemas["release-verdict.schema.json"].additionalProperties, false);
  assert.equal(schemas["model-cert-input.schema.json"].properties.status.const, "PASS");
  assert.equal(schemas["model-cert.schema.json"].properties.status.const, "PASS");
  assert.equal(schemas["release-verdict.schema.json"].properties.status.const, "PASS");
  assert.equal(schemas["release-verdict.schema.json"].properties.model_certs.minItems, 2);
  assert.equal(schemas["release-verdict.schema.json"].properties.model_certs.maxItems, 2);
  assert.equal(schemas["model-cert-input.schema.json"].$defs.scenario.additionalProperties, false);
  assert.equal(schemas["model-cert-input.schema.json"].$defs.scenarioSource.additionalProperties, false);
  assert.ok(schemas["model-cert-input.schema.json"].required.includes("scenario"));
  assert.ok(schemas["model-cert-input.schema.json"].required.includes("scenario_oracle"));
  assert.ok(schemas["model-cert.schema.json"].required.includes("scenario"));
  assert.ok(schemas["model-cert.schema.json"].required.includes("scenario_oracle"));
  assert.ok(schemas["release-verdict.schema.json"].required.includes("scenario"));
});

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}
