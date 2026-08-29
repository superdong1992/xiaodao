import fs from "node:fs";
import path from "node:path";

import { canonicalJson, sha256File } from "./util.mjs";

const PROVIDER_TERMINAL_RECEIPT = "evidence-v2-provider-terminal-failure";
const PRODUCTION_RUNTIME = "problem_locator.runtime.diagnosis_runtime.DiagnosisRuntime";
const DIAGNOSTIC_ID = /^diag-[a-f0-9]{64}$/u;
const EVALUATION_REF = /^eval-[a-f0-9]{64}$/u;
const REASON_CODE = /^[A-Z][A-Z0-9_]*$/u;

export const FAILURE_DIAGNOSTIC_SCHEMA_VERSION = 1;
export const FAILURE_DIAGNOSTIC_FIELDS = Object.freeze([
  "schema_version",
  "certification_target",
  "code",
  "reason_code",
  "reason",
  "diagnostic_id",
  "evaluation_ref",
  "provider_code",
  "provider_subtype",
]);

const TARGET_GATE = Object.freeze({
  P1: "real.macos-claude-deepseek-e2e",
  P2: "real.macos-codex-luna-e2e",
});

function exactKeys(value, keys) {
  return value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && canonicalJson(Object.keys(value).sort()) === canonicalJson([...keys].sort());
}

export function validFailureDiagnostic(value) {
  return exactKeys(value, FAILURE_DIAGNOSTIC_FIELDS)
    && value.schema_version === FAILURE_DIAGNOSTIC_SCHEMA_VERSION
    && ["P1", "P2"].includes(value.certification_target)
    && typeof value.code === "string"
    && REASON_CODE.test(value.code)
    && value.reason_code === value.code
    && typeof value.reason === "string"
    && value.reason.length > 0
    && typeof value.diagnostic_id === "string"
    && DIAGNOSTIC_ID.test(value.diagnostic_id)
    && (value.evaluation_ref === null
      || (typeof value.evaluation_ref === "string" && EVALUATION_REF.test(value.evaluation_ref)))
    && ((value.provider_code === null && value.provider_subtype === null)
      || (typeof value.provider_code === "string"
        && REASON_CODE.test(value.provider_code)
        && typeof value.provider_subtype === "string"
        && value.provider_subtype.length > 0));
}

function providerFailure(invocations) {
  if (!Array.isArray(invocations) || invocations.length === 0) return null;
  for (let index = invocations.length - 1; index >= 0; index -= 1) {
    const invocation = invocations[index];
    const outcome = invocation?.wrapper_outcome;
    const terminal = invocation?.terminal;
    if (invocation?.schema_version === 3
      && outcome?.schema_version === 1
      && outcome.status === "FAIL"
      && typeof outcome.code === "string"
      && REASON_CODE.test(outcome.code)
      && terminal !== null
      && typeof terminal === "object"
      && !Array.isArray(terminal)
      && terminal.is_error === true
      && typeof terminal.subtype === "string"
      && terminal.subtype.length > 0) return { code: outcome.code, subtype: terminal.subtype };
  }
  return null;
}

function validRepairs(value) {
  return exactKeys(value, ["reviewer", "specialist"])
    && Number.isSafeInteger(value.reviewer)
    && value.reviewer >= 0
    && Number.isSafeInteger(value.specialist)
    && value.specialist >= 0;
}

function validEvidenceFile({ attemptRoot, gateReceipt, expectedPath }) {
  const record = Array.isArray(gateReceipt?.evidence)
    ? gateReceipt.evidence.find((item) => item?.path === expectedPath)
    : null;
  const target = path.resolve(attemptRoot, ...expectedPath.split("/"));
  const root = path.resolve(attemptRoot);
  return exactKeys(record, ["path", "sha256", "size"])
    && target.startsWith(`${root}${path.sep}`)
    && fs.existsSync(target)
    && fs.statSync(target).isFile()
    && record.size === fs.statSync(target).size
    && record.sha256 === sha256File(target)
    ? target
    : null;
}

function readJsonOrNull(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return null;
  }
}

function projectGateFailureDiagnostic({ attemptRoot, stageId, gateSummary }) {
  if (gateSummary?.status !== "FAIL"
    || gateSummary.failure_domain !== "CONTRACT"
    || typeof gateSummary.receipt_path !== "string"
    || typeof gateSummary.receipt_digest !== "string") return null;
  const expectedGatePath = `payload/stages/${stageId}/gates/${gateSummary.id}/gate-receipt.json`;
  if (gateSummary.receipt_path !== expectedGatePath) return null;
  const gatePath = path.resolve(attemptRoot, ...expectedGatePath.split("/"));
  if (!fs.existsSync(gatePath) || sha256File(gatePath) !== gateSummary.receipt_digest) return null;
  const gateReceipt = readJsonOrNull(gatePath);
  if (gateReceipt?.schema_version !== 2
    || gateReceipt.stage_id !== stageId
    || gateReceipt.gate_id !== gateSummary.id
    || gateReceipt.status !== "FAIL"
    || gateReceipt.failure_domain !== "CONTRACT"
    || gateReceipt.code !== gateSummary.code) return null;

  const assertion = gateReceipt.assertions?.adapter;
  const targetGate = TARGET_GATE[assertion?.certification_target];
  if (targetGate === undefined || stageId !== targetGate || gateSummary.id !== targetGate) return null;
  const adapterPath = `payload/stages/${stageId}/gates/${gateSummary.id}/adapter-receipt.json`;
  const runtimePath = `payload/stages/${stageId}/gates/${gateSummary.id}/runtime-receipt.json`;
  const adapterFile = validEvidenceFile({ attemptRoot, gateReceipt, expectedPath: adapterPath });
  const runtimeFile = validEvidenceFile({ attemptRoot, gateReceipt, expectedPath: runtimePath });
  if (adapterFile === null || runtimeFile === null) return null;
  const adapter = readJsonOrNull(adapterFile);
  const runtime = readJsonOrNull(runtimeFile);
  const runtimeReference = assertion?.runtime_receipt;
  const assertionAdapter = assertion === null || assertion === undefined
    ? null
    : Object.fromEntries(Object.entries(assertion).filter(([key]) => key !== "runtime_receipt"));
  if (canonicalJson(assertionAdapter) !== canonicalJson(adapter)
    || !exactKeys(runtimeReference, ["path", "sha256", "status", "production_runtime", "methods_status"])
    || runtimeReference.path !== "runtime-receipt.json"
    || runtimeReference.sha256 !== sha256File(runtimeFile)
    || runtimeReference.status !== "PASS"
    || runtimeReference.production_runtime !== PRODUCTION_RUNTIME) return null;

  const expectedAdapterKeys = [
    "schema_version", "receipt_type", "status", "certification_target", "code",
    "methods_status", "reason_code", "reason", "diagnostic_id", "evaluation_ref",
    "model_calls", "repairs",
  ];
  const methods = runtime?.methods_result;
  const provider = providerFailure(gateReceipt.model_invocations);
  const diagnostic = {
    schema_version: FAILURE_DIAGNOSTIC_SCHEMA_VERSION,
    certification_target: adapter?.certification_target,
    code: adapter?.code,
    reason_code: adapter?.reason_code,
    reason: adapter?.reason,
    diagnostic_id: adapter?.diagnostic_id,
    evaluation_ref: adapter?.evaluation_ref,
    provider_code: provider?.code ?? null,
    provider_subtype: provider?.subtype ?? null,
  };
  if (!exactKeys(adapter, expectedAdapterKeys)
    || adapter.schema_version !== 1
    || adapter.receipt_type !== PROVIDER_TERMINAL_RECEIPT
    || adapter.status !== "FAIL"
    || adapter.code !== gateReceipt.code
    || !["UNRESOLVED", "FAILED"].includes(adapter.methods_status)
    || !Number.isSafeInteger(adapter.model_calls)
    || adapter.model_calls < 0
    || !validRepairs(adapter.repairs)
    || !validFailureDiagnostic(diagnostic)
    || runtime?.status !== "PASS"
    || runtime.execution_mode !== "real-model"
    || runtime.production_runtime !== PRODUCTION_RUNTIME
    || runtime.model_invocations !== adapter.model_calls
    || canonicalJson(runtime.repair_counts) !== canonicalJson(adapter.repairs)
    || methods?.status !== adapter.methods_status
    || methods.reason_code !== adapter.reason_code
    || !Array.isArray(methods.reasons)
    || methods.reasons.length !== 1
    || methods.reasons[0] !== adapter.reason
    || methods.diagnostic_id !== adapter.diagnostic_id
    || (methods.diagnostic_evaluation_ref ?? null) !== adapter.evaluation_ref
    || runtimeReference.methods_status !== adapter.methods_status) return null;
  return Object.freeze(diagnostic);
}

export function projectCandidateFailureDiagnostic({ attemptRoot, stages }) {
  if (!Array.isArray(stages)) return null;
  for (const stage of stages) {
    const gate = stage?.gates?.find((candidate) => !["PASS", "NOT_REQUIRED"].includes(candidate?.status));
    if (gate) return projectGateFailureDiagnostic({ attemptRoot, stageId: stage.id, gateSummary: gate });
    if (!["PASS", "NOT_REQUIRED"].includes(stage?.status) || stage?.performance_status === "FAIL") return null;
  }
  return null;
}
