import fs from "node:fs";
import path from "node:path";

import {
  assertFlow,
  canonicalJson,
  readJson,
  sha256Bytes,
  sha256File,
} from "../test-flow/lib/util.mjs";

const SHA256 = /^[a-f0-9]{64}$/;

export const EVIDENCE_V2_CORE_RECEIPT = "evidence-v2-core";
export const EVIDENCE_V2_CORE_SCHEMA_VERSION = 1;
export const EVIDENCE_V2_CORE_MANIFEST_PATH = "schemas/v2/contract-manifest.json";

export const EVIDENCE_V2_CORE_SELECTORS = Object.freeze([
  "tests/deterministic/journey/test_rpc_timeout.py::test_r01_r14_rpc_timeout_is_one_durable_cross_module_path",
  "tests/deterministic/journey/test_rpc_timeout.py::test_same_job_uses_initial_order_fact_and_survives_restart",
  "tests/deterministic/unit/runtime/test_diagnosis_runtime.py::test_methods_v1_specialist_publishes_candidate_json_and_log_archive",
  "tests/deterministic/unit/runtime/test_diagnosis_runtime.py::test_methods_preflight_publishes_waiting_without_backend_or_broker",
  "tests/deterministic/unit/runtime/test_methods_output_pipeline.py::test_verify_method_diagnosis_classifies_marker_not_indexed",
  "tests/deterministic/unit/runtime/test_methods_output_pipeline.py::test_specialized_diagnosis_hard_cut_ignores_legacy_v6_envelope",
  "tests/deterministic/unit/runtime/test_methods_output_pipeline.py::test_specialized_diagnosis_normalizes_pretty_methods_draft",
  "tests/deterministic/unit/integrations/test_result_archive.py::test_result_archive_v3_is_deterministic_and_uses_plan_order",
  "tests/deterministic/unit/integrations/test_result_archive.py::test_result_text_uses_the_locked_nine_chinese_sections",
  "tests/deterministic/unit/integrations/test_result_archive.py::test_inconclusive_result_never_builds_result_zip",
  "tests/deterministic/contracts/test_user_result_v2.py::test_completed_candidate_server_final_requires_json_and_archive",
  "tests/deterministic/contracts/test_user_result_v2.py::test_inconclusive_server_final_requires_json_and_forbids_archive",
  "tests/deterministic/contracts/test_user_result_v2.py::test_non_pass_review_carries_json_but_pass_carries_no_new_result",
  "tests/deterministic/unit/domain/test_coordinator_diagnosis.py::test_specialized_candidate_is_accepted_and_published_when_review_is_disabled",
  "tests/deterministic/unit/application/test_outcome_submission.py::test_candidate_outcome_formalizes_user_result_and_creates_review_job",
  "tests/deterministic/unit/application/test_outcome_submission.py::test_candidate_outcome_without_review_atomically_publishes_json_and_zip",
  "tests/deterministic/unit/application/test_outcome_submission.py::test_candidate_result_retry_adopts_internal_first_file_before_state_commit",
  "tests/deterministic/unit/application/test_outcome_submission.py::test_finalized_candidate_replay_adopts_consumed_file_and_directory",
  "tests/deterministic/unit/interfaces/test_client_access_skill.py::test_skill_downloads_and_presents_the_specialized_user_report",
  "tests/deterministic/unit/interfaces/test_settings.py::test_legacy_evidence_v2_reviewer_switch_is_rejected",
  "tests/deterministic/unit/storage/test_state_repository.py::test_v1_through_v8_state_is_read_only_and_unsupported",
  "tests/deterministic/contracts/test_mcp_input_schema_flatness.py::test_all_public_mcp_inputs_are_flat_without_exceptions",
]);

function exactKeys(value, expected, code, label) {
  assertFlow(value !== null && typeof value === "object" && !Array.isArray(value), code, `${label} must be an object`);
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  assertFlow(canonicalJson(actual) === canonicalJson(wanted), code, `${label} fields do not match the contract`);
}

function sha256(value, code, label) {
  assertFlow(typeof value === "string" && SHA256.test(value), code, `${label} must be a lowercase SHA-256`);
}

function safeCount(value, code, label) {
  assertFlow(Number.isSafeInteger(value) && value >= 0, code, `${label} must be a non-negative safe integer`);
}

function validateSummary(summary) {
  exactKeys(
    summary,
    ["schema_version", "tests", "passed", "failures", "errors", "skipped", "executed"],
    "CORE_VERDICT_PYTEST_SUMMARY_FIELDS",
    "pytest summary",
  );
  assertFlow(summary.schema_version === 2, "CORE_VERDICT_PYTEST_SUMMARY_VERSION", "pytest summary schema version must be 2");
  for (const field of ["tests", "passed", "failures", "errors", "skipped", "executed"]) {
    safeCount(summary[field], "CORE_VERDICT_PYTEST_COUNT", `pytest summary ${field}`);
  }
  assertFlow(
    summary.tests === summary.passed + summary.failures + summary.errors + summary.skipped,
    "CORE_VERDICT_PYTEST_COUNT",
    "pytest totals do not balance",
  );
  assertFlow(
    summary.executed === summary.passed + summary.failures + summary.errors,
    "CORE_VERDICT_PYTEST_COUNT",
    "pytest executed count does not balance",
  );
  assertFlow(
    summary.tests > 0
      && summary.passed === summary.tests
      && summary.executed === summary.tests
      && summary.failures === 0
      && summary.errors === 0
      && summary.skipped === 0,
    "CORE_VERDICT_PYTEST_NOT_PASS",
    "the Methods V1 Core suite must pass without failures, errors, or skips",
  );
  return summary;
}

export function evidenceV2CoreCasesDigest(selectors = EVIDENCE_V2_CORE_SELECTORS) {
  return sha256Bytes(canonicalJson(selectors));
}

export function validateEvidenceV2CoreVerdictSchema(value) {
  exactKeys(
    value,
    [
      "schema_version",
      "receipt_type",
      "status",
      "execution_mode",
      "model_invocations",
      "source_snapshot_digest",
      "contract_manifest",
      "core_cases",
      "pytest",
    ],
    "CORE_VERDICT_FIELDS",
    "core verdict",
  );
  assertFlow(value.schema_version === EVIDENCE_V2_CORE_SCHEMA_VERSION, "CORE_VERDICT_VERSION", "unsupported core verdict schema version");
  assertFlow(value.receipt_type === EVIDENCE_V2_CORE_RECEIPT, "CORE_VERDICT_TYPE", "invalid core verdict receipt type");
  assertFlow(value.status === "PASS", "CORE_VERDICT_STATUS", "core verdict status must be PASS");
  assertFlow(value.execution_mode === "deterministic-zero-model", "CORE_VERDICT_MODE", "core verdict execution mode must be deterministic-zero-model");
  assertFlow(value.model_invocations === 0, "CORE_VERDICT_MODEL_INVOCATIONS", "core verdict must record zero model invocations");
  sha256(value.source_snapshot_digest, "CORE_VERDICT_SOURCE_DIGEST", "source snapshot digest");

  exactKeys(value.contract_manifest, ["path", "sha256"], "CORE_VERDICT_MANIFEST_FIELDS", "contract manifest binding");
  assertFlow(value.contract_manifest.path === EVIDENCE_V2_CORE_MANIFEST_PATH, "CORE_VERDICT_MANIFEST_PATH", "contract manifest path is not pinned");
  sha256(value.contract_manifest.sha256, "CORE_VERDICT_MANIFEST_DIGEST", "contract manifest digest");

  exactKeys(value.core_cases, ["selectors", "count", "sha256"], "CORE_VERDICT_CASES_FIELDS", "Core case binding");
  assertFlow(Array.isArray(value.core_cases.selectors), "CORE_VERDICT_CASES", "Core selectors must be an array");
  assertFlow(
    canonicalJson(value.core_cases.selectors) === canonicalJson(EVIDENCE_V2_CORE_SELECTORS),
    "CORE_VERDICT_CASES",
    "Core selectors do not match the frozen Methods V1 suite",
  );
  assertFlow(value.core_cases.count === EVIDENCE_V2_CORE_SELECTORS.length, "CORE_VERDICT_CASE_COUNT", "Core case count is invalid");
  assertFlow(value.core_cases.sha256 === evidenceV2CoreCasesDigest(), "CORE_VERDICT_CASE_DIGEST", "Core case digest is invalid");

  exactKeys(
    value.pytest,
    ["summary_path", "summary_sha256", "junit_path", "junit_sha256", "counts"],
    "CORE_VERDICT_PYTEST_FIELDS",
    "pytest binding",
  );
  assertFlow(value.pytest.summary_path === "pytest-summary.json", "CORE_VERDICT_PYTEST_PATH", "pytest summary path is invalid");
  assertFlow(value.pytest.junit_path === "pytest.xml", "CORE_VERDICT_JUNIT_PATH", "JUnit path is invalid");
  sha256(value.pytest.summary_sha256, "CORE_VERDICT_PYTEST_DIGEST", "pytest summary digest");
  sha256(value.pytest.junit_sha256, "CORE_VERDICT_JUNIT_DIGEST", "JUnit digest");
  validateSummary({ schema_version: 2, ...value.pytest.counts });
  return value;
}

export function validateEvidenceV2CoreVerdict(value, {
  sourceSnapshotDigest,
  sourceRoot,
  gateRoot,
} = {}) {
  validateEvidenceV2CoreVerdictSchema(value);
  sha256(sourceSnapshotDigest, "CORE_VERDICT_EXPECTED_SOURCE_DIGEST", "expected source snapshot digest");
  assertFlow(value.source_snapshot_digest === sourceSnapshotDigest, "CORE_VERDICT_SOURCE_MISMATCH", "core verdict does not bind the active source snapshot");
  assertFlow(typeof sourceRoot === "string" && path.isAbsolute(sourceRoot), "CORE_VERDICT_SOURCE_ROOT", "source root must be absolute");
  assertFlow(typeof gateRoot === "string" && path.isAbsolute(gateRoot), "CORE_VERDICT_GATE_ROOT", "gate root must be absolute");

  const manifestPath = path.join(sourceRoot, ...EVIDENCE_V2_CORE_MANIFEST_PATH.split("/"));
  const summaryPath = path.join(gateRoot, value.pytest.summary_path);
  const junitPath = path.join(gateRoot, value.pytest.junit_path);
  for (const [filePath, code] of [
    [manifestPath, "CORE_VERDICT_MANIFEST_MISSING"],
    [summaryPath, "CORE_VERDICT_PYTEST_SUMMARY_MISSING"],
    [junitPath, "CORE_VERDICT_JUNIT_MISSING"],
  ]) {
    assertFlow(fs.existsSync(filePath) && fs.statSync(filePath).isFile(), code, `required Core receipt input is missing: ${filePath}`);
  }
  assertFlow(value.contract_manifest.sha256 === sha256File(manifestPath), "CORE_VERDICT_MANIFEST_MISMATCH", "contract manifest digest does not match the frozen source");
  assertFlow(value.pytest.summary_sha256 === sha256File(summaryPath), "CORE_VERDICT_PYTEST_MISMATCH", "pytest summary digest does not match its evidence file");
  assertFlow(value.pytest.junit_sha256 === sha256File(junitPath), "CORE_VERDICT_JUNIT_MISMATCH", "JUnit digest does not match its evidence file");

  const summary = validateSummary(readJson(summaryPath));
  assertFlow(
    canonicalJson(value.pytest.counts) === canonicalJson({
      tests: summary.tests,
      passed: summary.passed,
      failures: summary.failures,
      errors: summary.errors,
      skipped: summary.skipped,
      executed: summary.executed,
    }),
    "CORE_VERDICT_PYTEST_COUNTS_MISMATCH",
    "Core verdict counts do not match pytest-summary.json",
  );
  return value;
}

export function buildEvidenceV2CoreVerdict({ sourceSnapshotDigest, sourceRoot, gateRoot }) {
  const manifestPath = path.join(sourceRoot, ...EVIDENCE_V2_CORE_MANIFEST_PATH.split("/"));
  const summaryPath = path.join(gateRoot, "pytest-summary.json");
  const junitPath = path.join(gateRoot, "pytest.xml");
  const summary = validateSummary(readJson(summaryPath));
  const verdict = {
    schema_version: EVIDENCE_V2_CORE_SCHEMA_VERSION,
    receipt_type: EVIDENCE_V2_CORE_RECEIPT,
    status: "PASS",
    execution_mode: "deterministic-zero-model",
    model_invocations: 0,
    source_snapshot_digest: sourceSnapshotDigest,
    contract_manifest: {
      path: EVIDENCE_V2_CORE_MANIFEST_PATH,
      sha256: sha256File(manifestPath),
    },
    core_cases: {
      selectors: [...EVIDENCE_V2_CORE_SELECTORS],
      count: EVIDENCE_V2_CORE_SELECTORS.length,
      sha256: evidenceV2CoreCasesDigest(),
    },
    pytest: {
      summary_path: "pytest-summary.json",
      summary_sha256: sha256File(summaryPath),
      junit_path: "pytest.xml",
      junit_sha256: sha256File(junitPath),
      counts: {
        tests: summary.tests,
        passed: summary.passed,
        failures: summary.failures,
        errors: summary.errors,
        skipped: summary.skipped,
        executed: summary.executed,
      },
    },
  };
  return validateEvidenceV2CoreVerdict(verdict, { sourceSnapshotDigest, sourceRoot, gateRoot });
}
