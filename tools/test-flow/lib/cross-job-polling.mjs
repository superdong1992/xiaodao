export function fixedGetCasePollInput(caseId) {
  if (typeof caseId !== "string" || caseId.length === 0) {
    throw new TypeError("caseId must be a non-empty string");
  }
  return {
    case_id: caseId,
    wait_for_job_id: null,
    wait_seconds: 30,
  };
}

export function fixedGetCasePollingInvariant(caseId) {
  const input = JSON.stringify(fixedGetCasePollInput(caseId));
  return `Polling invariant: every problem_locator_get_case call MUST copy this complete non-empty root input exactly: ${input}. Keep wait_for_job_id null for every poll, including RUNNING, WAITING_INPUT, and REVIEWING; null follows the Case's current active Job without changing the tool input. Never call it, or any other Problem Locator tool, with {}. Empty or missing input returning VALIDATION_ERROR does not change the template: make at most one immediate corrected call from the same literal object; never repeat the malformed input.`;
}
