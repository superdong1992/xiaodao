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
  "tests/deterministic/journey/test_rpc_timeout.py::test_rpc_timeout_methods_v2_is_one_durable_same_job_path",
  "tests/deterministic/integration/test_methods_v2_runtime_journey.py::test_runtime_submission_reviewer_and_public_projection_are_one_v2_journey",
  "tests/deterministic/contracts/test_methods_v2_public_schemas.py::test_all_ten_public_schema_roots_accept_production_objects",
  "tests/deterministic/contracts/test_methods_v2_public_schemas.py::test_each_public_root_rejects_one_invalid_field_in_schema_and_pydantic",
  "tests/deterministic/contracts/test_methods_v2_public_schemas.py::test_existing_composite_public_schemas_accept_the_production_methods_branch",
  "tests/deterministic/contracts/test_methods_v2_public_schemas.py::test_composite_methods_branch_rejects_one_invalid_nested_field",
  "tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_specialist_restart_rebuilds_missing_plan_from_recorded_graph",
  "tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_specialist_pending_state_wins_over_later_asset_drift",
  "tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_specialist_restart_reuses_graph_and_runs_only_repair",
  "tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_reviewer_restart_runs_only_repair_and_reads_source_state_after_model",
  "tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_specialist_replacement_resumes_old_repair_without_rescan",
  "tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_reviewer_replacement_inherits_old_rejection_and_interrupted_state",
  "tests/deterministic/unit/runtime/test_methods_evidence_v2.py::test_shared_literal_emits_one_method_qualified_hit_per_method",
  "tests/deterministic/unit/runtime/test_methods_evidence_v2.py::test_scan_casefolds_but_preserves_declared_marker_and_frozen_line",
  "tests/deterministic/unit/runtime/test_methods_evidence_v2.py::test_complete_plan_has_every_loaded_method_and_all_of_its_hits",
  "tests/deterministic/unit/runtime/test_methods_evidence_v2.py::test_plan_consumes_production_graph_refs_without_rescanning_logs",
  "tests/deterministic/unit/runtime/test_methods_evidence_v2.py::test_plan_rejects_rehashed_hit_bound_to_another_methods_marker_index",
  "tests/deterministic/unit/runtime/test_methods_evidence_v2.py::test_request_identity_tokens_create_distinct_events_without_losing_hits",
  "tests/deterministic/unit/runtime/test_methods_evidence_v2.py::test_plan_graph_validation_rejects_incomplete_or_cross_method_refs",
  "tests/deterministic/unit/runtime/test_meta_skill_source_identity.py::test_validator_rejects_marker_from_another_method_reference",
  "tests/deterministic/unit/integrations/test_lan_logparse_meta_skill.py::test_validator_rejects_marker_from_another_method_reference",
  "tests/deterministic/unit/runtime/test_methods_evaluation_v2.py::test_response_rejects_order_coverage_and_extra_fields",
  "tests/deterministic/unit/runtime/test_methods_evaluation_v2.py::test_blind_consensus_ignores_reason_and_resolves_complete_agreement",
  "tests/deterministic/unit/domain/test_methods_state_v2.py::test_consensus_truth_table_drives_terminal_state",
  "tests/deterministic/unit/domain/test_methods_state_v2.py::test_each_role_gets_one_protocol_repair_then_exhausts",
  "tests/deterministic/unit/domain/test_methods_state_v2.py::test_semantic_and_model_failures_are_unresolved_not_failed",
  "tests/deterministic/unit/domain/test_methods_state_v2.py::test_only_infrastructure_categories_enter_failed",
  "tests/deterministic/unit/domain/test_methods_state_v2.py::test_interrupt_preserves_role_and_resume_returns_to_its_pending_state",
  "tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_specialist_scans_once_hard_cuts_logs_and_publishes_handoff",
  "tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_specialist_uses_one_repair_then_stops",
  "tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_specialist_private_reason_is_not_published_in_handoff",
  "tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_reviewer_is_blind_and_reads_pending_state_after_model",
  "tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_reviewer_disagreement_and_unknown_are_unresolved",
  "tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_reviewer_uses_at_most_one_repair",
  "tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_reviewer_terminal_state_wins_over_later_asset_drift",
  "tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_reviewer_rebuilds_missing_limitations_record_from_graph",
  "tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_terminal_contract_is_validated_before_state_commit",
  "tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_cancellation_persists_interrupted_state_without_terminal_projection",
  "tests/deterministic/unit/runtime/test_methods_outcome_v2.py::test_outcome_mapping_does_not_rescan_or_read_marker_line",
  "tests/deterministic/unit/runtime/test_methods_replay_v2.py::test_real_store_replays_specialist_primary_rejection_without_rescanning",
  "tests/deterministic/unit/runtime/test_methods_replay_v2.py::test_real_store_replays_reviewer_repair_from_legal_rejection_sequence",
  "tests/deterministic/unit/runtime/test_methods_workspace_context_v2.py::test_role_workspaces_hard_cut_to_one_graph_plan_and_real_cards",
  "tests/deterministic/unit/runtime/test_methods_workspace_context_v2.py::test_review_context_policy_declares_candidate_free_methods_v2",
  "tests/deterministic/unit/storage/test_resource_files.py::test_reader_file_materialization_never_attempts_a_hardlink",
  "tests/deterministic/integration/test_methods_v2_terminal_submission.py::test_consensus_terminal_projection_survives_submission_mcp_and_rest",
  "tests/deterministic/integration/test_methods_v2_terminal_submission.py::test_each_failed_terminal_reason_reaches_case_mcp_and_rest",
  "tests/deterministic/integration/test_methods_v2_terminal_submission.py::test_each_role_failure_reason_reaches_case_mcp_and_rest",
  "tests/deterministic/integration/test_methods_v2_terminal_submission.py::test_methods_reviewer_private_reasons_never_enter_public_journey",
  "tests/deterministic/integration/test_methods_v2_pre_evaluation_failures.py::test_pre_evaluation_failure_reaches_case_mcp_and_rest_without_fake_graph",
  "tests/deterministic/unit/interfaces/test_replay_cli.py::test_production_cli_replays_real_rejection_without_scanner_model_or_writes",
  "tests/deterministic/unit/test_journey_renderer.py::test_methods_v2_terminal_projection_is_rendered_from_production_outcome",
  "tests/deterministic/unit/interfaces/test_web_api.py::test_nonterminal_methods_result_is_optional_in_rest_and_serialization",
  "tests/deterministic/unit/interfaces/test_client_access_skill.py::test_skill_creates_case_before_requesting_missing_details",
  "tests/deterministic/unit/interfaces/test_client_access_skill.py::test_skill_presents_methods_v2_without_waiting_for_an_artifact",
  "tests/deterministic/integration/test_evidence_v2_source_mutations.py::test_source_overlay_mutant_is_killed_by_exact_regression_test",
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
    "the Evidence V2 Core suite must pass without failures, errors, or skips",
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
    "Core selectors do not match the frozen Evidence V2 suite",
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
