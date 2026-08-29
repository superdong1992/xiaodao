export const EVIDENCE_V2_PROVIDER_TERMINAL_FAILURE_RECEIPT = "evidence-v2-provider-terminal-failure";

export function projectEvidenceV2ProviderTerminalFailure({
  certificationTarget,
  methodsResult,
}) {
  if (methodsResult?.status === "RESOLVED") return null;
  if (!["UNRESOLVED", "FAILED"].includes(methodsResult?.status)) {
    throw new TypeError("Evidence V2 provider terminal result is missing");
  }
  if (
    typeof methodsResult.reason_code !== "string"
    || typeof methodsResult.diagnostic_id !== "string"
    || !Array.isArray(methodsResult.reasons)
    || methodsResult.reasons.length !== 1
    || typeof methodsResult.reasons[0] !== "string"
  ) {
    throw new TypeError("Evidence V2 provider terminal result has no public diagnostic");
  }
  return Object.freeze({
    schema_version: 1,
    receipt_type: EVIDENCE_V2_PROVIDER_TERMINAL_FAILURE_RECEIPT,
    status: "FAIL",
    certification_target: certificationTarget,
    code: methodsResult.reason_code,
    methods_status: methodsResult.status,
    reason_code: methodsResult.reason_code,
    reason: methodsResult.reasons[0],
    diagnostic_id: methodsResult.diagnostic_id,
    evaluation_ref: methodsResult.diagnostic_evaluation_ref ?? null,
  });
}
