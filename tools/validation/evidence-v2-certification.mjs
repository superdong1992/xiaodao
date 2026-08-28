import fs from "node:fs";
import path from "node:path";

import {
  assertFlow,
  canonicalJson,
  readJson,
  sha256File,
} from "../test-flow/lib/util.mjs";
import { isCompleteUsage, sumUsage } from "../test-flow/lib/usage.mjs";
import {
  EVIDENCE_V2_CORE_MANIFEST_PATH,
  validateEvidenceV2CoreVerdict,
} from "./evidence-v2-core.mjs";

const SHA256 = /^[a-f0-9]{64}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const RESULT_REF = /^result-[a-f0-9]{64}$/;
const PLAN_REF = /^plan-[a-f0-9]{64}$/;
const GRAPH_REF = /^graph-[a-f0-9]{64}$/;
const DIAGNOSTIC_ID = /^diag-[a-f0-9]{64}$/;
const TARGETS = Object.freeze(["P1", "P2"]);
const TERMINAL_STATUSES = new Set(["RESOLVED", "UNRESOLVED", "FAILED"]);
const TARGET_IDENTITIES = Object.freeze({
  P1: Object.freeze({
    provider: "deepseek",
    transport: "claude-code-compatible-api",
    model: "deepseek-v4-flash[1m]",
    revisionSource: "settings-fingerprint",
  }),
  P2: Object.freeze({
    provider: "openai",
    transport: "codex-app-server",
    model: "gpt-5.6-luna",
    revisionSource: "frozen-codex-cli-and-app-server-runtime-identity",
  }),
});

export const EVIDENCE_V2_MODEL_CERT_INPUT_RECEIPT = "evidence-v2-model-cert-input";
export const EVIDENCE_V2_MODEL_CERT_RECEIPT = "evidence-v2-model-cert";
export const EVIDENCE_V2_RELEASE_VERDICT_RECEIPT = "evidence-v2-release-verdict";
export const EVIDENCE_V2_CERTIFICATION_SCHEMA_VERSION = 1;
export const EVIDENCE_V2_CERTIFICATION_SCENARIO_ID = "multiple-rpc-timeouts";
export const EVIDENCE_V2_MODEL_CERT_INPUT_FILENAME = "model-cert-input.json";
export const EVIDENCE_V2_MODEL_CERT_FILENAME = "model-cert.json";
export const EVIDENCE_V2_RELEASE_VERDICT_FILENAME = "release-verdict.json";
export const EVIDENCE_V2_CORE_VERDICT_PATH = "payload/stages/deterministic.full/gates/det.evidence-v2-core/core-verdict.json";

function exactKeys(value, expected, code, label) {
  assertFlow(value !== null && typeof value === "object" && !Array.isArray(value), code, `${label} must be an object`);
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  assertFlow(canonicalJson(actual) === canonicalJson(wanted), code, `${label} fields do not match the contract`);
}

function nonEmptyString(value, code, label) {
  assertFlow(typeof value === "string" && value.length > 0, code, `${label} must be a non-empty string`);
}

function sha256(value, code, label) {
  assertFlow(typeof value === "string" && SHA256.test(value), code, `${label} must be a lowercase SHA-256`);
}

function safeCount(value, code, label, { positive = false } = {}) {
  assertFlow(Number.isSafeInteger(value) && value >= (positive ? 1 : 0), code, `${label} has an invalid count`);
}

function validateFileBinding(value, { expectedPath, code, label }) {
  exactKeys(value, ["path", "sha256"], `${code}_FIELDS`, label);
  assertFlow(value.path === expectedPath, `${code}_PATH`, `${label} path is not pinned`);
  sha256(value.sha256, `${code}_DIGEST`, `${label} digest`);
}

function validateIdentityBinding(value, code, label) {
  exactKeys(value, ["id", "sha256"], `${code}_FIELDS`, label);
  nonEmptyString(value.id, `${code}_ID`, `${label} id`);
  sha256(value.sha256, `${code}_DIGEST`, `${label} digest`);
}

function validateUsage(value, code, label, { positive = false } = {}) {
  exactKeys(
    value,
    [
      "schema_version",
      "input_tokens",
      "output_tokens",
      "cache_creation_input_tokens",
      "cache_read_input_tokens",
      "total_tokens",
      "cost_usd",
    ],
    `${code}_FIELDS`,
    label,
  );
  assertFlow(isCompleteUsage(value), code, `${label} is incomplete or internally inconsistent`);
  assertFlow(!positive || value.total_tokens > 0, code, `${label} must record actual model usage`);
}

function validateProvider(value) {
  exactKeys(value, ["id", "transport"], "MODEL_CERT_PROVIDER_FIELDS", "provider identity");
  nonEmptyString(value.id, "MODEL_CERT_PROVIDER_ID", "provider id");
  nonEmptyString(value.transport, "MODEL_CERT_PROVIDER_TRANSPORT", "provider transport");
}

function validateModel(value) {
  exactKeys(value, ["id", "revision", "revision_source"], "MODEL_CERT_MODEL_FIELDS", "model identity");
  nonEmptyString(value.id, "MODEL_CERT_MODEL_ID", "model id");
  nonEmptyString(value.revision, "MODEL_CERT_MODEL_REVISION", "model revision");
  nonEmptyString(value.revision_source, "MODEL_CERT_MODEL_REVISION_SOURCE", "model revision source");
}

function validateExecutionIdentity(value) {
  exactKeys(
    value,
    ["runtime", "prompt_policy", "profile", "tool_policy"],
    "MODEL_CERT_EXECUTION_IDENTITY_FIELDS",
    "execution identity",
  );
  validateIdentityBinding(value.runtime, "MODEL_CERT_RUNTIME", "runtime identity");
  validateIdentityBinding(value.prompt_policy, "MODEL_CERT_PROMPT_POLICY", "prompt policy");
  validateIdentityBinding(value.profile, "MODEL_CERT_PROFILE", "profile identity");
  validateIdentityBinding(value.tool_policy, "MODEL_CERT_TOOL_POLICY", "tool policy");
}

function validateTargetIdentity(value) {
  const expected = TARGET_IDENTITIES[value.certification_target];
  assertFlow(value.provider.id === expected.provider, "MODEL_CERT_TARGET_PROVIDER", "model certification provider does not match its target");
  assertFlow(value.provider.transport === expected.transport, "MODEL_CERT_TARGET_TRANSPORT", "model certification transport does not match its target");
  assertFlow(value.model.id === expected.model, "MODEL_CERT_TARGET_MODEL", "model certification model does not match its target");
  assertFlow(value.model.revision_source === expected.revisionSource, "MODEL_CERT_TARGET_REVISION_SOURCE", "model certification revision source does not match its target");
  sha256(value.model.revision, "MODEL_CERT_TARGET_REVISION", "model certification frozen revision fingerprint");
}

function validateInvocation(value, index) {
  exactKeys(
    value,
    ["invocation_id", "ordinal", "role", "attempt", "prompt", "usage"],
    "MODEL_CERT_INVOCATION_FIELDS",
    `model invocation ${index + 1}`,
  );
  nonEmptyString(value.invocation_id, "MODEL_CERT_INVOCATION_ID", "model invocation id");
  assertFlow(value.ordinal === index + 1, "MODEL_CERT_INVOCATION_ORDINAL", "model invocation ordinals must be contiguous and source ordered");
  assertFlow(["SPECIALIST", "REVIEWER"].includes(value.role), "MODEL_CERT_INVOCATION_ROLE", "model invocation role is invalid");
  assertFlow(["PRIMARY", "REPAIR"].includes(value.attempt), "MODEL_CERT_INVOCATION_ATTEMPT", "model invocation attempt is invalid");
  exactKeys(value.prompt, ["sha256", "size"], "MODEL_CERT_PROMPT_FIELDS", "exact prompt binding");
  sha256(value.prompt.sha256, "MODEL_CERT_PROMPT_DIGEST", "exact prompt digest");
  safeCount(value.prompt.size, "MODEL_CERT_PROMPT_SIZE", "exact prompt size", { positive: true });
  validateUsage(value.usage, "MODEL_CERT_INVOCATION_USAGE", "model invocation usage", { positive: true });
}

function invocationTopology(invocations) {
  return invocations.map((value) => `${value.role}:${value.attempt}`).join(",");
}

function expectedCallCounts(invocations) {
  const specialist = invocations.filter((value) => value.role === "SPECIALIST");
  const reviewer = invocations.filter((value) => value.role === "REVIEWER");
  return {
    total_calls: invocations.length,
    specialist_calls: specialist.length,
    reviewer_calls: reviewer.length,
    specialist_repairs: specialist.filter((value) => value.attempt === "REPAIR").length,
    reviewer_repairs: reviewer.filter((value) => value.attempt === "REPAIR").length,
    model_retries: 0,
  };
}

function validateCalls(invocations, counts, aggregateUsage) {
  assertFlow(Array.isArray(invocations), "MODEL_CERT_INVOCATIONS", "model invocations must be an array");
  assertFlow(invocations.length >= 2 && invocations.length <= 4, "MODEL_CERT_INVOCATION_COUNT", "model certification requires two primary calls and at most one repair per role");
  invocations.forEach(validateInvocation);
  assertFlow(new Set(invocations.map((value) => value.invocation_id)).size === invocations.length, "MODEL_CERT_INVOCATION_ID", "model invocation ids must be unique");
  assertFlow(
    new Set([
      "SPECIALIST:PRIMARY,REVIEWER:PRIMARY",
      "SPECIALIST:PRIMARY,SPECIALIST:REPAIR,REVIEWER:PRIMARY",
      "SPECIALIST:PRIMARY,REVIEWER:PRIMARY,REVIEWER:REPAIR",
      "SPECIALIST:PRIMARY,SPECIALIST:REPAIR,REVIEWER:PRIMARY,REVIEWER:REPAIR",
    ]).has(invocationTopology(invocations)),
    "MODEL_CERT_INVOCATION_TOPOLOGY",
    "model invocations must contain one primary call and at most one immediate repair for each isolated role",
  );
  exactKeys(
    counts,
    ["total_calls", "specialist_calls", "reviewer_calls", "specialist_repairs", "reviewer_repairs", "model_retries"],
    "MODEL_CERT_CALL_COUNTS_FIELDS",
    "model call counts",
  );
  for (const [name, value] of Object.entries(counts)) safeCount(value, "MODEL_CERT_CALL_COUNTS", `model call count ${name}`);
  assertFlow(canonicalJson(counts) === canonicalJson(expectedCallCounts(invocations)), "MODEL_CERT_CALL_COUNTS", "model call counts do not match the exact invocation sequence");
  validateUsage(aggregateUsage, "MODEL_CERT_USAGE", "aggregate model usage", { positive: true });
  assertFlow(canonicalJson(aggregateUsage) === canonicalJson(sumUsage(invocations.map((value) => value.usage))), "MODEL_CERT_USAGE_MISMATCH", "aggregate model usage does not equal the invocation sum");
}

function validateMethodsResult(value) {
  exactKeys(
    value,
    [
      "canonical_sha256",
      "canonical_size",
      "case_id",
      "source_job_id",
      "result_ref",
      "evaluation_id",
      "status",
      "plan_ref",
      "evidence_graph_ref",
      "diagnostic_id",
    ],
    "MODEL_CERT_METHODS_RESULT_FIELDS",
    "final methods_result identity",
  );
  sha256(value.canonical_sha256, "MODEL_CERT_METHODS_RESULT_DIGEST", "final methods_result digest");
  safeCount(value.canonical_size, "MODEL_CERT_METHODS_RESULT_SIZE", "final methods_result size", { positive: true });
  for (const field of ["case_id", "source_job_id", "evaluation_id"]) {
    assertFlow(typeof value[field] === "string" && UUID.test(value[field]), "MODEL_CERT_METHODS_RESULT_UUID", `final methods_result ${field} is invalid`);
  }
  assertFlow(typeof value.result_ref === "string" && RESULT_REF.test(value.result_ref), "MODEL_CERT_METHODS_RESULT_REF", "final methods_result result_ref is invalid");
  assertFlow(typeof value.plan_ref === "string" && PLAN_REF.test(value.plan_ref), "MODEL_CERT_METHODS_PLAN_REF", "final methods_result plan_ref is invalid");
  assertFlow(typeof value.evidence_graph_ref === "string" && GRAPH_REF.test(value.evidence_graph_ref), "MODEL_CERT_METHODS_GRAPH_REF", "final methods_result evidence_graph_ref is invalid");
  assertFlow(typeof value.diagnostic_id === "string" && DIAGNOSTIC_ID.test(value.diagnostic_id), "MODEL_CERT_METHODS_DIAGNOSTIC_ID", "final methods_result diagnostic_id is invalid");
  assertFlow(TERMINAL_STATUSES.has(value.status), "MODEL_CERT_METHODS_STATUS", "final methods_result status is invalid");
}

function validateScenario(value) {
  exactKeys(
    value,
    [
      "scenario_id",
      "source_wiki_sha256",
      "registration_id",
      "skill_content_sha256",
      "user_inputs_sha256",
      "sources",
      "evidence_graph",
      "evaluation_plan",
    ],
    "MODEL_CERT_SCENARIO_FIELDS",
    "model certification scenario identity",
  );
  assertFlow(
    value.scenario_id === EVIDENCE_V2_CERTIFICATION_SCENARIO_ID,
    "MODEL_CERT_SCENARIO_ID",
    `model certification scenario must be ${EVIDENCE_V2_CERTIFICATION_SCENARIO_ID}`,
  );
  sha256(value.source_wiki_sha256, "MODEL_CERT_SCENARIO_WIKI_DIGEST", "scenario source Wiki digest");
  nonEmptyString(value.registration_id, "MODEL_CERT_SCENARIO_REGISTRATION_ID", "scenario registration id");
  sha256(value.skill_content_sha256, "MODEL_CERT_SCENARIO_SKILL_DIGEST", "scenario Skill content digest");
  sha256(value.user_inputs_sha256, "MODEL_CERT_SCENARIO_USER_INPUTS_DIGEST", "scenario user inputs digest");
  assertFlow(Array.isArray(value.sources) && value.sources.length > 0, "MODEL_CERT_SCENARIO_SOURCES", "scenario sources must be a non-empty ordered array");
  value.sources.forEach((source, index) => {
    exactKeys(source, ["source_id", "content_sha256"], "MODEL_CERT_SCENARIO_SOURCE_FIELDS", `scenario source ${index + 1}`);
    nonEmptyString(source.source_id, "MODEL_CERT_SCENARIO_SOURCE_ID", `scenario source ${index + 1} id`);
    sha256(source.content_sha256, "MODEL_CERT_SCENARIO_SOURCE_DIGEST", `scenario source ${index + 1} digest`);
  });
  assertFlow(
    new Set(value.sources.map((source) => source.source_id)).size === value.sources.length,
    "MODEL_CERT_SCENARIO_SOURCE_ID",
    "scenario source ids must be unique",
  );
  exactKeys(value.evidence_graph, ["ref", "canonical_sha256", "canonical_size"], "MODEL_CERT_SCENARIO_GRAPH_FIELDS", "scenario Evidence Graph identity");
  assertFlow(typeof value.evidence_graph.ref === "string" && GRAPH_REF.test(value.evidence_graph.ref), "MODEL_CERT_SCENARIO_GRAPH_REF", "scenario Evidence Graph ref is invalid");
  sha256(value.evidence_graph.canonical_sha256, "MODEL_CERT_SCENARIO_GRAPH_DIGEST", "scenario Evidence Graph digest");
  safeCount(value.evidence_graph.canonical_size, "MODEL_CERT_SCENARIO_GRAPH_SIZE", "scenario Evidence Graph size", { positive: true });
  exactKeys(value.evaluation_plan, ["ref", "canonical_sha256", "canonical_size"], "MODEL_CERT_SCENARIO_PLAN_FIELDS", "scenario Evaluation Plan identity");
  assertFlow(typeof value.evaluation_plan.ref === "string" && PLAN_REF.test(value.evaluation_plan.ref), "MODEL_CERT_SCENARIO_PLAN_REF", "scenario Evaluation Plan ref is invalid");
  sha256(value.evaluation_plan.canonical_sha256, "MODEL_CERT_SCENARIO_PLAN_DIGEST", "scenario Evaluation Plan digest");
  safeCount(value.evaluation_plan.canonical_size, "MODEL_CERT_SCENARIO_PLAN_SIZE", "scenario Evaluation Plan size", { positive: true });
}

function validateScenarioResultBinding(scenario, methodsResult) {
  assertFlow(
    methodsResult.evidence_graph_ref === scenario.evidence_graph.ref,
    "MODEL_CERT_SCENARIO_GRAPH_RESULT_MISMATCH",
    "final methods_result binds another Evidence Graph",
  );
  assertFlow(
    methodsResult.plan_ref === scenario.evaluation_plan.ref,
    "MODEL_CERT_SCENARIO_PLAN_RESULT_MISMATCH",
    "final methods_result binds another Evaluation Plan",
  );
}

function modelCertBodyFields(receiptType) {
  const fields = [
    "schema_version",
    "receipt_type",
    "status",
    "certification_target",
    "source_snapshot_digest",
    "contract_manifest",
    "core_verdict",
    "scenario",
    "provider",
    "model",
    "execution_identity",
    "invocations",
    "call_counts",
    "usage",
    "methods_result",
  ];
  if (receiptType === EVIDENCE_V2_MODEL_CERT_RECEIPT) fields.push("adapter_input");
  return fields;
}

function validateModelCertBody(value, { receiptType, certificationTarget } = {}) {
  exactKeys(value, modelCertBodyFields(receiptType), "MODEL_CERT_FIELDS", "model certification receipt");
  assertFlow(value.schema_version === EVIDENCE_V2_CERTIFICATION_SCHEMA_VERSION, "MODEL_CERT_VERSION", "unsupported model certification schema version");
  assertFlow(value.receipt_type === receiptType, "MODEL_CERT_TYPE", "model certification receipt type is invalid");
  assertFlow(value.status === "PASS", "MODEL_CERT_STATUS", "model certification status must be PASS");
  assertFlow(TARGETS.includes(value.certification_target), "MODEL_CERT_TARGET", "model certification target is invalid");
  if (certificationTarget !== undefined) assertFlow(value.certification_target === certificationTarget, "MODEL_CERT_TARGET_MISMATCH", "model certification target does not match its Gate");
  sha256(value.source_snapshot_digest, "MODEL_CERT_SOURCE_DIGEST", "source snapshot digest");
  validateFileBinding(value.contract_manifest, {
    expectedPath: EVIDENCE_V2_CORE_MANIFEST_PATH,
    code: "MODEL_CERT_MANIFEST",
    label: "V8 contract manifest binding",
  });
  validateFileBinding(value.core_verdict, {
    expectedPath: EVIDENCE_V2_CORE_VERDICT_PATH,
    code: "MODEL_CERT_CORE",
    label: "Core verdict binding",
  });
  validateScenario(value.scenario);
  validateProvider(value.provider);
  validateModel(value.model);
  validateTargetIdentity(value);
  validateExecutionIdentity(value.execution_identity);
  validateCalls(value.invocations, value.call_counts, value.usage);
  validateMethodsResult(value.methods_result);
  validateScenarioResultBinding(value.scenario, value.methods_result);
  if (receiptType === EVIDENCE_V2_MODEL_CERT_RECEIPT) {
    validateFileBinding(value.adapter_input, {
      expectedPath: EVIDENCE_V2_MODEL_CERT_INPUT_FILENAME,
      code: "MODEL_CERT_ADAPTER_INPUT",
      label: "model certification adapter input",
    });
  }
  return value;
}

function inputProjection(cert) {
  const value = { ...cert, receipt_type: EVIDENCE_V2_MODEL_CERT_INPUT_RECEIPT };
  delete value.adapter_input;
  return value;
}

function requireFile(filePath, code, label) {
  assertFlow(typeof filePath === "string" && path.isAbsolute(filePath), code, `${label} path must be absolute`);
  assertFlow(fs.existsSync(filePath) && fs.statSync(filePath).isFile(), code, `${label} is missing`);
}

function validateCoreAndSource(value, {
  sourceSnapshotDigest,
  sourceRoot,
  coreVerdictPath,
}) {
  sha256(sourceSnapshotDigest, "MODEL_CERT_EXPECTED_SOURCE_DIGEST", "expected source snapshot digest");
  assertFlow(value.source_snapshot_digest === sourceSnapshotDigest, "MODEL_CERT_SOURCE_MISMATCH", "model certification does not bind the active source snapshot");
  assertFlow(typeof sourceRoot === "string" && path.isAbsolute(sourceRoot), "MODEL_CERT_SOURCE_ROOT", "source root must be absolute");
  requireFile(coreVerdictPath, "MODEL_CERT_CORE_MISSING", "Core verdict");
  const manifestPath = path.join(sourceRoot, ...EVIDENCE_V2_CORE_MANIFEST_PATH.split("/"));
  requireFile(manifestPath, "MODEL_CERT_MANIFEST_MISSING", "V8 contract manifest");
  assertFlow(value.contract_manifest.sha256 === sha256File(manifestPath), "MODEL_CERT_MANIFEST_MISMATCH", "model certification contract manifest digest differs from the frozen source");
  assertFlow(value.core_verdict.sha256 === sha256File(coreVerdictPath), "MODEL_CERT_CORE_MISMATCH", "model certification Core verdict digest differs from its receipt");
  const core = readJson(coreVerdictPath);
  validateEvidenceV2CoreVerdict(core, {
    sourceSnapshotDigest,
    sourceRoot,
    gateRoot: path.dirname(coreVerdictPath),
  });
  assertFlow(core.contract_manifest.sha256 === value.contract_manifest.sha256, "MODEL_CERT_CORE_MANIFEST_MISMATCH", "Core and model certification bind different V8 contract manifests");
}

export function validateEvidenceV2ModelCertInputSchema(value, options = {}) {
  return validateModelCertBody(value, {
    ...options,
    receiptType: EVIDENCE_V2_MODEL_CERT_INPUT_RECEIPT,
  });
}

export function validateEvidenceV2ModelCertSchema(value, options = {}) {
  return validateModelCertBody(value, {
    ...options,
    receiptType: EVIDENCE_V2_MODEL_CERT_RECEIPT,
  });
}

export function validateEvidenceV2ModelCertInput(value, {
  certificationTarget,
  sourceSnapshotDigest,
  sourceRoot,
  coreVerdictPath,
} = {}) {
  validateEvidenceV2ModelCertInputSchema(value, { certificationTarget });
  validateCoreAndSource(value, { sourceSnapshotDigest, sourceRoot, coreVerdictPath });
  return value;
}

export function validateEvidenceV2ModelCert(value, {
  certificationTarget,
  sourceSnapshotDigest,
  sourceRoot,
  coreVerdictPath,
  certRoot,
} = {}) {
  validateEvidenceV2ModelCertSchema(value, { certificationTarget });
  assertFlow(typeof certRoot === "string" && path.isAbsolute(certRoot), "MODEL_CERT_ROOT", "model certification root must be absolute");
  const inputPath = path.join(certRoot, EVIDENCE_V2_MODEL_CERT_INPUT_FILENAME);
  requireFile(inputPath, "MODEL_CERT_ADAPTER_INPUT_MISSING", "model certification adapter input");
  assertFlow(value.adapter_input.sha256 === sha256File(inputPath), "MODEL_CERT_ADAPTER_INPUT_MISMATCH", "model certification input digest differs from its receipt");
  const input = readJson(inputPath);
  validateEvidenceV2ModelCertInput(input, {
    certificationTarget,
    sourceSnapshotDigest,
    sourceRoot,
    coreVerdictPath,
  });
  assertFlow(canonicalJson(inputProjection(value)) === canonicalJson(input), "MODEL_CERT_ADAPTER_INPUT_MISMATCH", "model certification differs from its exact adapter input");
  return value;
}

export function buildEvidenceV2ModelCert({
  certificationTarget,
  sourceSnapshotDigest,
  sourceRoot,
  coreVerdictPath,
  certRoot,
}) {
  assertFlow(typeof certRoot === "string" && path.isAbsolute(certRoot), "MODEL_CERT_ROOT", "model certification root must be absolute");
  const inputPath = path.join(certRoot, EVIDENCE_V2_MODEL_CERT_INPUT_FILENAME);
  requireFile(inputPath, "MODEL_CERT_ADAPTER_INPUT_MISSING", "model certification adapter input");
  const input = readJson(inputPath);
  validateEvidenceV2ModelCertInput(input, {
    certificationTarget,
    sourceSnapshotDigest,
    sourceRoot,
    coreVerdictPath,
  });
  const cert = {
    ...input,
    receipt_type: EVIDENCE_V2_MODEL_CERT_RECEIPT,
    adapter_input: {
      path: EVIDENCE_V2_MODEL_CERT_INPUT_FILENAME,
      sha256: sha256File(inputPath),
    },
  };
  return validateEvidenceV2ModelCert(cert, {
    certificationTarget,
    sourceSnapshotDigest,
    sourceRoot,
    coreVerdictPath,
    certRoot,
  });
}

function relativeArtifactPath(artifactRoot, filePath, code) {
  assertFlow(typeof artifactRoot === "string" && path.isAbsolute(artifactRoot), code, "artifact root must be absolute");
  requireFile(filePath, code, "release evidence file");
  const relative = path.relative(artifactRoot, filePath);
  assertFlow(relative.length > 0 && relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative), code, "release evidence file must belong to the artifact root");
  return relative.split(path.sep).join("/");
}

function validateModelCertBinding(value, target) {
  exactKeys(
    value,
    ["certification_target", "path", "sha256", "provider", "model", "methods_result"],
    "RELEASE_VERDICT_MODEL_CERT_FIELDS",
    `release ${target} model certification binding`,
  );
  assertFlow(value.certification_target === target, "RELEASE_VERDICT_MODEL_CERT_TARGET", `release model certification target must be ${target}`);
  nonEmptyString(value.path, "RELEASE_VERDICT_MODEL_CERT_PATH", "release model certification path");
  sha256(value.sha256, "RELEASE_VERDICT_MODEL_CERT_DIGEST", "release model certification digest");
  validateProvider(value.provider);
  validateModel(value.model);
  validateTargetIdentity({
    certification_target: target,
    provider: value.provider,
    model: value.model,
  });
  validateMethodsResult(value.methods_result);
}

export function validateEvidenceV2ReleaseVerdictSchema(value) {
  exactKeys(
    value,
    ["schema_version", "receipt_type", "status", "source_snapshot_digest", "contract_manifest", "core_verdict", "scenario", "model_certs"],
    "RELEASE_VERDICT_FIELDS",
    "Evidence V2 release verdict",
  );
  assertFlow(value.schema_version === EVIDENCE_V2_CERTIFICATION_SCHEMA_VERSION, "RELEASE_VERDICT_VERSION", "unsupported Evidence V2 release verdict version");
  assertFlow(value.receipt_type === EVIDENCE_V2_RELEASE_VERDICT_RECEIPT, "RELEASE_VERDICT_TYPE", "Evidence V2 release verdict type is invalid");
  assertFlow(value.status === "PASS", "RELEASE_VERDICT_STATUS", "Evidence V2 release verdict status must be PASS");
  sha256(value.source_snapshot_digest, "RELEASE_VERDICT_SOURCE_DIGEST", "release source snapshot digest");
  validateFileBinding(value.contract_manifest, {
    expectedPath: EVIDENCE_V2_CORE_MANIFEST_PATH,
    code: "RELEASE_VERDICT_MANIFEST",
    label: "release V8 contract manifest binding",
  });
  validateFileBinding(value.core_verdict, {
    expectedPath: EVIDENCE_V2_CORE_VERDICT_PATH,
    code: "RELEASE_VERDICT_CORE",
    label: "release Core verdict binding",
  });
  validateScenario(value.scenario);
  assertFlow(Array.isArray(value.model_certs) && value.model_certs.length === 2, "RELEASE_VERDICT_MODEL_CERTS", "release verdict requires exactly P1 and P2 model certifications");
  validateModelCertBinding(value.model_certs[0], "P1");
  validateModelCertBinding(value.model_certs[1], "P2");
  return value;
}

export function validateEvidenceV2ReleaseVerdict(value, {
  sourceSnapshotDigest,
  sourceRoot,
  artifactRoot,
} = {}) {
  validateEvidenceV2ReleaseVerdictSchema(value);
  sha256(sourceSnapshotDigest, "RELEASE_VERDICT_EXPECTED_SOURCE_DIGEST", "expected release source snapshot digest");
  assertFlow(value.source_snapshot_digest === sourceSnapshotDigest, "RELEASE_VERDICT_SOURCE_MISMATCH", "release verdict does not bind the active source snapshot");
  const coreVerdictPath = path.join(artifactRoot, ...value.core_verdict.path.split("/"));
  validateCoreAndSource(value, { sourceSnapshotDigest, sourceRoot, coreVerdictPath });
  for (const [index, target] of TARGETS.entries()) {
    const binding = value.model_certs[index];
    const certPath = path.join(artifactRoot, ...binding.path.split("/"));
    requireFile(certPath, "RELEASE_VERDICT_MODEL_CERT_MISSING", `${target} model certification`);
    assertFlow(binding.sha256 === sha256File(certPath), "RELEASE_VERDICT_MODEL_CERT_MISMATCH", `${target} model certification digest differs from its receipt`);
    const cert = readJson(certPath);
    validateEvidenceV2ModelCert(cert, {
      certificationTarget: target,
      sourceSnapshotDigest,
      sourceRoot,
      coreVerdictPath,
      certRoot: path.dirname(certPath),
    });
    assertFlow(cert.core_verdict.sha256 === value.core_verdict.sha256, "RELEASE_VERDICT_CORE_MISMATCH", `${target} model certification binds another Core verdict`);
    assertFlow(cert.contract_manifest.sha256 === value.contract_manifest.sha256, "RELEASE_VERDICT_MANIFEST_MISMATCH", `${target} model certification binds another V8 contract manifest`);
    assertFlow(canonicalJson(cert.scenario) === canonicalJson(value.scenario), "RELEASE_VERDICT_SCENARIO_MISMATCH", `${target} model certification binds another scenario`);
    assertFlow(canonicalJson(binding.provider) === canonicalJson(cert.provider), "RELEASE_VERDICT_PROVIDER_MISMATCH", `${target} provider identity differs from its model certification`);
    assertFlow(canonicalJson(binding.model) === canonicalJson(cert.model), "RELEASE_VERDICT_MODEL_MISMATCH", `${target} model identity differs from its model certification`);
    assertFlow(canonicalJson(binding.methods_result) === canonicalJson(cert.methods_result), "RELEASE_VERDICT_METHODS_RESULT_MISMATCH", `${target} methods_result identity differs from its model certification`);
  }
  return value;
}

export function buildEvidenceV2ReleaseVerdict({
  sourceSnapshotDigest,
  sourceRoot,
  artifactRoot,
  coreVerdictPath,
  p1ModelCertPath,
  p2ModelCertPath,
}) {
  const core = readJson(coreVerdictPath);
  validateEvidenceV2CoreVerdict(core, {
    sourceSnapshotDigest,
    sourceRoot,
    gateRoot: path.dirname(coreVerdictPath),
  });
  const coreRelativePath = relativeArtifactPath(artifactRoot, coreVerdictPath, "RELEASE_VERDICT_CORE_PATH");
  assertFlow(coreRelativePath === EVIDENCE_V2_CORE_VERDICT_PATH, "RELEASE_VERDICT_CORE_PATH", "release Core verdict path is not pinned");
  const certPaths = [p1ModelCertPath, p2ModelCertPath];
  let scenario;
  const modelCerts = certPaths.map((certPath, index) => {
    const target = TARGETS[index];
    const cert = readJson(certPath);
    validateEvidenceV2ModelCert(cert, {
      certificationTarget: target,
      sourceSnapshotDigest,
      sourceRoot,
      coreVerdictPath,
      certRoot: path.dirname(certPath),
    });
    if (scenario === undefined) scenario = cert.scenario;
    else assertFlow(canonicalJson(cert.scenario) === canonicalJson(scenario), "RELEASE_VERDICT_SCENARIO_MISMATCH", "P1 and P2 model certifications bind different scenarios");
    return {
      certification_target: target,
      path: relativeArtifactPath(artifactRoot, certPath, "RELEASE_VERDICT_MODEL_CERT_PATH"),
      sha256: sha256File(certPath),
      provider: cert.provider,
      model: cert.model,
      methods_result: cert.methods_result,
    };
  });
  const verdict = {
    schema_version: EVIDENCE_V2_CERTIFICATION_SCHEMA_VERSION,
    receipt_type: EVIDENCE_V2_RELEASE_VERDICT_RECEIPT,
    status: "PASS",
    source_snapshot_digest: sourceSnapshotDigest,
    contract_manifest: core.contract_manifest,
    core_verdict: {
      path: EVIDENCE_V2_CORE_VERDICT_PATH,
      sha256: sha256File(coreVerdictPath),
    },
    scenario,
    model_certs: modelCerts,
  };
  return validateEvidenceV2ReleaseVerdict(verdict, {
    sourceSnapshotDigest,
    sourceRoot,
    artifactRoot,
  });
}
