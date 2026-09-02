export const EVIDENCE_V2_EVALUATION_MODES = Object.freeze([
  "SPECIALIST_ONLY",
  "BLIND_CONSENSUS",
]);

export const EVIDENCE_V2_DEFAULT_EVALUATION_MODE = "SPECIALIST_ONLY";

export function isEvidenceV2EvaluationMode(value) {
  return EVIDENCE_V2_EVALUATION_MODES.includes(value);
}
