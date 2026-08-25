import { canonicalJson, sha256Bytes } from "./util.mjs";

const SHA256 = /^[a-f0-9]{64}$/;
const METHODS_STATUSES = new Set(["CONFIRMED", "PARTIAL", "INSUFFICIENT"]);
const AUDIT_FIELDS = Object.freeze([
  "schema_version",
  "registration_id",
  "registration_sha256",
  "package_tree_sha256",
  "combined_sha256",
  "logparse_receipt_sha256",
  "status",
  "confirmed_methods",
  "evidence_count",
  "checked_source_count",
  "skill_load",
]);
const SKILL_LOAD_FIELDS = Object.freeze([
  "package_tree_sha256",
  "scanned_source_ids",
  "marker_hits",
  "loaded_method_ids",
]);

export class MethodsOracleError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "MethodsOracleError";
    this.code = code;
  }
}

function fail(code, message) {
  throw new MethodsOracleError(code, message);
}

function requireOracle(condition, code, message) {
  if (!condition) fail(code, message);
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function exactKeys(value, expected) {
  return isPlainObject(value)
    && canonicalJson(Object.keys(value).sort()) === canonicalJson([...expected].sort());
}

function uniqueStrings(value) {
  return Array.isArray(value)
    && value.every((item) => typeof item === "string" && item.length > 0)
    && value.length === new Set(value).size;
}

function bytes(value, label) {
  if (Buffer.isBuffer(value)) return value;
  if (value instanceof Uint8Array) return Buffer.from(value);
  fail("METHODS_ORACLE_BYTES_INVALID", `${label} must be bytes`);
}

function parseObject(value, label) {
  const payload = bytes(value, label);
  let parsed;
  try {
    parsed = JSON.parse(payload.toString("utf8"));
  } catch {
    fail("METHODS_ORACLE_JSON_INVALID", `${label} is not valid JSON`);
  }
  requireOracle(isPlainObject(parsed), "METHODS_ORACLE_JSON_INVALID", `${label} must be a JSON object`);
  requireOracle(
    payload.equals(Buffer.from(canonicalJson(parsed), "utf8")),
    "METHODS_ORACLE_JSON_NON_CANONICAL",
    `${label} must use the canonical execution-record JSON encoding`,
  );
  return { payload, parsed };
}

function validateExpectedGrounding(expected) {
  requireOracle(isPlainObject(expected), "METHODS_ORACLE_EXPECTATION_INVALID", "Methods grounding expectation must be an object");
  requireOracle(typeof expected.diagnosis_job_id === "string" && expected.diagnosis_job_id.length > 0, "METHODS_ORACLE_EXPECTATION_INVALID", "Expected DIAGNOSE job ID is invalid");
  requireOracle(typeof expected.case_id === "string" && expected.case_id.length > 0, "METHODS_ORACLE_EXPECTATION_INVALID", "Expected Case ID is invalid");
  requireOracle(exactKeys(expected.skill_ref, ["id", "version", "content_hash"])
    && typeof expected.skill_ref.id === "string" && expected.skill_ref.id.length > 0
    && typeof expected.skill_ref.version === "string" && expected.skill_ref.version.length > 0
    && SHA256.test(expected.skill_ref.content_hash ?? ""), "METHODS_ORACLE_EXPECTATION_INVALID", "Expected Skill reference is invalid");
  requireOracle(typeof expected.logparse_product === "string" && expected.logparse_product.length > 0, "METHODS_ORACLE_EXPECTATION_INVALID", "Expected Logparse product is invalid");
  requireOracle(typeof expected.registration_id === "string" && expected.registration_id.length > 0, "METHODS_ORACLE_EXPECTATION_INVALID", "Expected Methods registration is invalid");
  requireOracle([expected.registration_sha256, expected.package_tree_sha256, expected.combined_sha256].every((value) => SHA256.test(value ?? "")), "METHODS_ORACLE_EXPECTATION_INVALID", "Expected Methods hashes are invalid");
  requireOracle(METHODS_STATUSES.has(expected.status), "METHODS_ORACLE_EXPECTATION_INVALID", "Expected Methods status is invalid");
  requireOracle(uniqueStrings(expected.source_ids) && expected.source_ids.length > 0, "METHODS_ORACLE_EXPECTATION_INVALID", "Expected Methods source IDs are invalid");
  requireOracle(Number.isSafeInteger(expected.evidence_count) && expected.evidence_count >= 0, "METHODS_ORACLE_EXPECTATION_INVALID", "Expected Methods evidence count is invalid");
  if (expected.confirmed_methods !== undefined) requireOracle(uniqueStrings(expected.confirmed_methods), "METHODS_ORACLE_EXPECTATION_INVALID", "Expected confirmed Methods are invalid");
  if (expected.known_method_ids !== undefined) requireOracle(uniqueStrings(expected.known_method_ids) && expected.known_method_ids.length > 0, "METHODS_ORACLE_EXPECTATION_INVALID", "Known Methods IDs are invalid");
}

export function validateMethodsGroundingExecutionRecord({
  jobBytes,
  auditBytes,
  logparseReceiptBytes,
  expected,
}) {
  validateExpectedGrounding(expected);
  const jobDocument = parseObject(jobBytes, "DIAGNOSE job.json");
  const auditDocument = parseObject(auditBytes, "method-grounding-audit.json");
  const receiptPayload = bytes(logparseReceiptBytes, "methods_logparse_receipt.json");
  const job = jobDocument.parsed;
  const audit = auditDocument.parsed;

  requireOracle(
    job.job_id === expected.diagnosis_job_id
      && job.case_id === expected.case_id
      && job.job_type === "DIAGNOSE"
      && job.diagnosis_mode === "SPECIALIZED"
      && job.logparse_product === expected.logparse_product,
    "METHODS_ORACLE_JOB_IDENTITY_MISMATCH",
    "The exact DIAGNOSE execution record does not match the expected Case and runtime mode",
  );
  requireOracle(
    exactKeys(job.skill_ref, ["id", "version", "content_hash"])
      && canonicalJson(job.skill_ref) === canonicalJson(expected.skill_ref),
    "METHODS_ORACLE_SKILL_REF_MISMATCH",
    "The DIAGNOSE execution record does not bind the expected Skill reference",
  );

  requireOracle(exactKeys(audit, AUDIT_FIELDS), "METHODS_ORACLE_AUDIT_FIELDS_INVALID", "Methods grounding audit fields are invalid");
  requireOracle(exactKeys(audit.skill_load, SKILL_LOAD_FIELDS), "METHODS_ORACLE_SKILL_LOAD_FIELDS_INVALID", "Methods skill-load receipt fields are invalid");
  requireOracle(
    audit.schema_version === 1
      && audit.registration_id === expected.registration_id
      && audit.registration_sha256 === expected.registration_sha256
      && audit.package_tree_sha256 === expected.package_tree_sha256
      && audit.combined_sha256 === expected.combined_sha256
      && audit.skill_load.package_tree_sha256 === expected.package_tree_sha256,
    "METHODS_ORACLE_PACKAGE_IDENTITY_MISMATCH",
    "Methods grounding audit does not bind the expected registration and package bytes",
  );

  const receiptSha256 = sha256Bytes(receiptPayload);
  requireOracle(
    audit.logparse_receipt_sha256 === receiptSha256,
    "METHODS_ORACLE_LOGPARSE_RECEIPT_MISMATCH",
    "Methods grounding audit does not bind the exact Logparse receipt bytes",
  );
  requireOracle(
    METHODS_STATUSES.has(audit.status) && audit.status === expected.status,
    "METHODS_ORACLE_STATUS_MISMATCH",
    "Actual Methods status differs from the scenario oracle",
  );
  requireOracle(
    uniqueStrings(audit.confirmed_methods)
      && (expected.confirmed_methods === undefined
        || canonicalJson(audit.confirmed_methods) === canonicalJson(expected.confirmed_methods)),
    "METHODS_ORACLE_CONFIRMED_METHODS_MISMATCH",
    "Grounded confirmed Methods differ from the scenario oracle",
  );
  requireOracle(
    Number.isSafeInteger(audit.evidence_count)
      && audit.evidence_count === expected.evidence_count
      && Number.isSafeInteger(audit.checked_source_count)
      && audit.checked_source_count === expected.source_ids.length,
    "METHODS_ORACLE_COUNT_MISMATCH",
    "Methods grounding counters differ from the scenario oracle",
  );
  requireOracle(
    uniqueStrings(audit.skill_load.scanned_source_ids)
      && canonicalJson(audit.skill_load.scanned_source_ids) === canonicalJson(expected.source_ids)
      && uniqueStrings(audit.skill_load.loaded_method_ids),
    "METHODS_ORACLE_SKILL_LOAD_MISMATCH",
    "Methods skill-load sources are invalid",
  );
  if (expected.known_method_ids !== undefined) {
    const known = new Set(expected.known_method_ids);
    requireOracle(
      audit.confirmed_methods.every((methodId) => known.has(methodId))
        && audit.skill_load.loaded_method_ids.every((methodId) => known.has(methodId)),
      "METHODS_ORACLE_UNKNOWN_METHOD",
      "Methods grounding audit names a method outside the pinned package",
    );
  }
  requireOracle(
    Array.isArray(audit.skill_load.marker_hits)
      && audit.skill_load.marker_hits.every((item) => Array.isArray(item)
        && item.length === 3
        && expected.source_ids.includes(item[0])
        && typeof item[1] === "string" && item[1].length > 0
        && Number.isSafeInteger(item[2]) && item[2] > 0),
    "METHODS_ORACLE_MARKER_HITS_INVALID",
    "Methods marker-hit receipt is invalid",
  );

  return {
    schema_version: 1,
    status: "PASS",
    diagnosis_job_id: expected.diagnosis_job_id,
    case_id: expected.case_id,
    skill_ref: expected.skill_ref,
    registration_id: audit.registration_id,
    registration_sha256: audit.registration_sha256,
    package_tree_sha256: audit.package_tree_sha256,
    combined_sha256: audit.combined_sha256,
    expected_methods_status: expected.status,
    actual_methods_status: audit.status,
    confirmed_methods: audit.confirmed_methods,
    evidence_count: audit.evidence_count,
    checked_source_count: audit.checked_source_count,
    job_sha256: sha256Bytes(jobDocument.payload),
    audit_sha256: sha256Bytes(auditDocument.payload),
    logparse_receipt_sha256: receiptSha256,
  };
}

function factorIds(report, field) {
  return (report[field] ?? []).map((item) => item.factor_id);
}

function validateEvidenceIdentityPartition(report, identities) {
  const rules = new Map();
  for (const rule of report.verification_rules ?? []) {
    requireOracle(isPlainObject(rule) && typeof rule.rule_id === "string" && !rules.has(rule.rule_id), "RELEASE_RESULT_RULE_SET_INVALID", "Public verification rules must have unique IDs");
    rules.set(rule.rule_id, rule);
  }
  const factors = [
    ...(report.causal_factors ?? []),
    ...(report.candidate_factors ?? []),
    ...(report.excluded_factors ?? []),
  ];
  const byFactor = new Map();
  for (const identity of identities) {
    const values = byFactor.get(identity.factor_id) ?? [];
    values.push(identity);
    byFactor.set(identity.factor_id, values);
  }
  for (const [factorId, expectedIdentities] of byFactor) {
    const factor = factors.find((item) => item.factor_id === factorId);
    requireOracle(factor && uniqueStrings(factor.required_rule_ids), "RELEASE_RESULT_EVIDENCE_IDENTITY", "Expected evidence factor or rule IDs are absent");
    requireOracle(factor.required_rule_ids.length === expectedIdentities.length, "RELEASE_RESULT_EVIDENCE_EVENT_COUNT", "A method's independent evidence events were merged or multiplied");
    const matchedIdentityIndexes = new Set();
    for (const ruleId of factor.required_rule_ids) {
      const rule = rules.get(ruleId);
      requireOracle(rule, "RELEASE_RESULT_EVIDENCE_RULE_MISSING", "A factor references an absent verification rule");
      const serializedRule = canonicalJson(rule);
      const matches = expectedIdentities
        .map((identity, index) => ({ identity, index }))
        .filter(({ identity }) => serializedRule.includes(identity.marker)
          && identity.identity_tokens.every((token) => serializedRule.includes(token)));
      requireOracle(matches.length === 1 && !matchedIdentityIndexes.has(matches[0].index), "RELEASE_RESULT_EVIDENCE_EVENT_MERGED", "One verification rule does not map to exactly one expected event identity");
      matchedIdentityIndexes.add(matches[0].index);
    }
    requireOracle(matchedIdentityIndexes.size === expectedIdentities.length, "RELEASE_RESULT_EVIDENCE_IDENTITY", "Expected event identities are not covered one-to-one");
  }
}

export function validateReleaseDiagnosisReport({
  report,
  expectation,
  completionCriteria,
  requiredSafetyPhrases,
}) {
  requireOracle(isPlainObject(report), "RELEASE_RESULT_INVALID", "Public diagnosis result must be an object");
  requireOracle(
    report.schema_version === 3 && report.status === expectation.report_status,
    "RESTART_RESULT_STATUS",
    "Public diagnosis status differs from its translated product expectation",
  );
  for (const [field, expected] of [
    ["causal_factors", expectation.causal_factor_ids],
    ["candidate_factors", expectation.candidate_factor_ids],
    ["excluded_factors", expectation.excluded_factor_ids],
  ]) {
    requireOracle(canonicalJson(factorIds(report, field)) === canonicalJson(expected), `RESTART_RESULT_${field.toUpperCase()}`, `Public ${field} differ from the scenario oracle`);
  }
  validateEvidenceIdentityPartition(report, expectation.required_evidence_identities);
  const criteria = report.completion_criteria_mapping ?? [];
  requireOracle(
    criteria.length === completionCriteria.length
      && criteria.every((item, index) => item.criterion_index === index && item.criterion === completionCriteria[index])
      && (expectation.resolution_status === "COMPLETE"
        ? criteria.every((item) => item.status === "SATISFIED")
        : criteria.some((item) => ["SATISFIED", "PARTIALLY_SATISFIED"].includes(item.status)) && criteria.some((item) => item.status !== "SATISFIED")),
    "RESTART_RESULT_CRITERIA",
    "Public completion criteria differ from the scenario expectation",
  );
  const serialized = canonicalJson(report);
  requireOracle(expectation.forbidden_evidence_terms.every((term) => !serialized.includes(term)), "RESTART_RESULT_FORBIDDEN_EVIDENCE", "Public result contains forbidden scenario evidence");
  requireOracle(
    uniqueStrings(report.safety_notes)
      && requiredSafetyPhrases.every((phrase) => report.safety_notes.some((note) => note.includes(phrase))),
    "RESTART_RESULT_SAFETY_NOTES",
    "Required safety phrases must appear specifically in safety_notes",
  );
  return true;
}
