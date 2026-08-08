from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

import pytest
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import TypeAdapter, ValidationError

from problem_locator.contracts import SCHEMA_MODELS
from problem_locator.contracts.enums import (
    CandidateStatus,
    ContextSectionKind,
    ErrorCode,
    ExecutionStage,
)
from problem_locator.contracts.errors import (
    DETERMINISTIC_OUTCOME_FAILURE_SPECS,
    deterministic_outcome_failure,
)
from problem_locator.contracts.limits import SPECIALIST_CONTEXT_BYTES
from problem_locator.contracts.models import (
    ApplicationErrorDetail,
    BoundedContext,
    CandidateConclusion,
    ContextSection,
    ExecutionFailure,
    Job,
    JobInstructionPayload,
    LogparseParseClaim,
    UserResultPayload,
    WorkspaceInputManifest,
    validate_job_instruction_for_job,
    validate_workspace_manifest_for_job,
)
from problem_locator.contracts.serialization import (
    canonical_json_bytes,
    canonical_json_sha256,
    parse_canonical_json_bytes,
)

from tests.contracts._support import FIXTURE_ROOT, load_json, schema_validator
from tests.contracts.fakes import CountingLogparseAdapter


def _claim_compatible_inputs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return a valid first-parse seam with no pre-existing LOGPARSE_RUN."""

    job = load_json(FIXTURE_ROOT / "positive" / "job-diagnose.json")
    manifest = load_json(
        FIXTURE_ROOT / "positive" / "workspace-input-manifest.json"
    )
    claim = load_json(FIXTURE_ROOT / "positive" / "logparse-parse-claim.json")
    job["artifact_refs"] = []
    manifest["entries"] = [
        entry
        for entry in manifest["entries"]
        if entry["input_kind"] != "ARTIFACT"
    ]
    manifest["resolved_logparse_plan"].update(
        attachment_id=job["attachment_refs"][0],
        artifact_id=None,
    )
    return job, manifest, claim


def _validate_manifest_for_job(
    job_payload: dict[str, Any], manifest_payload: dict[str, Any]
) -> tuple[Job, WorkspaceInputManifest]:
    job = Job.model_validate(job_payload)
    manifest = WorkspaceInputManifest.model_validate(manifest_payload)
    validate_workspace_manifest_for_job(manifest, job)
    return job, manifest


def _validate_claim_for_first_parse(
    job_payload: dict[str, Any],
    manifest_payload: dict[str, Any],
    claim_payload: dict[str, Any],
) -> LogparseParseClaim:
    job, manifest = _validate_manifest_for_job(job_payload, manifest_payload)
    claim = LogparseParseClaim.model_validate(claim_payload)
    if any(
        entry.input_kind == "ARTIFACT" and entry.artifact_kind == "LOGPARSE_RUN"
        for entry in manifest.entries
    ):
        raise ValueError("a first-parse claim forbids a pre-existing LOGPARSE_RUN")
    attachments = {
        entry.resource_id: entry
        for entry in manifest.entries
        if entry.input_kind == "ATTACHMENT"
    }
    attachment = attachments.get(claim.attachment_id)
    if claim.job_id != job.job_id:
        raise ValueError("parse claim job_id does not match the Job")
    if attachment is None or attachment.sha256 != claim.attachment_sha256:
        raise ValueError("parse claim attachment does not match the manifest")
    if claim.logparse_tool_ref != manifest.logparse_tool_ref:
        raise ValueError("parse claim tool ref does not match the manifest")
    return claim


def test_workspace_manifest_and_parse_claim_accept_the_first_parse_seam() -> None:
    job, manifest, claim = _claim_compatible_inputs()
    parsed_claim = _validate_claim_for_first_parse(job, manifest, claim)

    assert manifest["logparse_product"] == job["logparse_product"]
    assert parsed_claim.attachment_id == job["attachment_refs"][0]
    schema_validator("workspace-input-manifest.schema.json").validate(manifest)
    schema_validator("logparse-parse-claim.schema.json").validate(claim)
    assert parse_canonical_json_bytes(
        canonical_json_bytes(manifest), model_type=WorkspaceInputManifest
    ).job_id == job["job_id"]
    assert parse_canonical_json_bytes(
        canonical_json_bytes(claim), model_type=LogparseParseClaim
    ) == parsed_claim


@pytest.mark.parametrize(
    ("schema_name", "target"),
    [
        ("workspace-input-manifest.schema.json", "manifest"),
        ("workspace-input-manifest.schema.json", "entry"),
        ("logparse-parse-claim.schema.json", "claim"),
    ],
)
def test_workspace_manifest_and_parse_claim_forbid_extra_fields(
    schema_name: str, target: str
) -> None:
    _, manifest, claim = _claim_compatible_inputs()
    payload = claim if target == "claim" else manifest
    if target == "entry":
        payload["entries"][0]["unexpected"] = True
    else:
        payload["unexpected"] = True

    model_type = SCHEMA_MODELS[schema_name]
    with pytest.raises(ValidationError):
        TypeAdapter(model_type).validate_python(payload)
    with pytest.raises(JsonSchemaValidationError):
        schema_validator(schema_name).validate(payload)


def test_manifest_preserves_within_group_order_and_fixed_logparse_product() -> None:
    job, manifest, _ = _claim_compatible_inputs()
    second_evidence = copy.deepcopy(
        next(entry for entry in manifest["entries"] if entry["input_kind"] == "EVIDENCE")
    )
    second_evidence["resource_id"] = "00000000-0000-0000-0000-000000000041"
    second_evidence["source_ref"] = "00000000-0000-0000-0000-000000000071"
    evidence_end = next(
        index
        for index, entry in enumerate(manifest["entries"])
        if entry["input_kind"] == "PREVIOUS_OUTCOME"
    )
    manifest["entries"].insert(evidence_end, second_evidence)
    job["evidence_refs"].append(second_evidence["resource_id"])
    job["context_snapshot"]["evidence_refs"].append(second_evidence["resource_id"])
    _validate_manifest_for_job(job, manifest)

    reordered = copy.deepcopy(manifest)
    evidence_indices = [
        index
        for index, entry in enumerate(reordered["entries"])
        if entry["input_kind"] == "EVIDENCE"
    ]
    left, right = evidence_indices
    reordered["entries"][left], reordered["entries"][right] = (
        reordered["entries"][right],
        reordered["entries"][left],
    )
    # The public DTO preserves a same-group sequence; the Job seam freezes it.
    WorkspaceInputManifest.model_validate(reordered)
    with pytest.raises(ValueError, match="EVIDENCE order"):
        _validate_manifest_for_job(job, reordered)

    wrong_product = copy.deepcopy(manifest)
    wrong_product["logparse_product"] = "inventory-service"
    WorkspaceInputManifest.model_validate(wrong_product)
    with pytest.raises(ValueError, match="logparse_product"):
        _validate_manifest_for_job(job, wrong_product)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("job_id", "00000000-0000-0000-0000-000000000099"),
        ("attachment_id", "00000000-0000-0000-0000-000000000099"),
        ("attachment_sha256", "9" * 64),
        (
            "logparse_tool_ref",
            {
                "id": "logparse",
                "version": "9.9.9",
                "content_hash": "9" * 64,
            },
        ),
    ],
)
def test_parse_claim_rejects_cross_seam_drift(field: str, replacement: Any) -> None:
    job, manifest, claim = _claim_compatible_inputs()
    claim[field] = replacement
    LogparseParseClaim.model_validate(claim)
    with pytest.raises(ValueError):
        _validate_claim_for_first_parse(job, manifest, claim)


def test_parse_claim_is_forbidden_when_manifest_already_has_logparse_run() -> None:
    job = load_json(FIXTURE_ROOT / "positive" / "job-diagnose.json")
    manifest = load_json(
        FIXTURE_ROOT / "positive" / "workspace-input-manifest.json"
    )
    claim = load_json(FIXTURE_ROOT / "positive" / "logparse-parse-claim.json")
    with pytest.raises(ValueError, match="pre-existing LOGPARSE_RUN"):
        _validate_claim_for_first_parse(job, manifest, claim)


def test_parse_then_hang_is_a_backend_timeout_without_a_second_parse() -> None:
    job, manifest, claim = _claim_compatible_inputs()
    parsed_claim = _validate_claim_for_first_parse(job, manifest, claim)
    adapter = CountingLogparseAdapter(
        parse_results=[{"parse_manifest": "parse_manifest.json"}],
        target_log_results=[TimeoutError("deterministic post-parse hang")],
    )

    assert adapter.parse("archive", product=manifest["logparse_product"]) == {
        "parse_manifest": "parse_manifest.json"
    }
    with pytest.raises(TimeoutError, match="post-parse hang"):
        adapter.target_logs("parsed-run", order_id="order-1")

    failure = ExecutionFailure(
        stage=ExecutionStage.BACKEND_EXECUTE,
        code=ErrorCode.BACKEND_TIMEOUT,
        message="The backend exceeded the fixed wall time.",
        retryable=True,
        details=[],
    )
    assert adapter.parse_count == 1
    assert parsed_claim == LogparseParseClaim.model_validate(claim)
    assert failure.code is ErrorCode.BACKEND_TIMEOUT
    assert failure.stage is ExecutionStage.BACKEND_EXECUTE
    assert failure.details == []
    assert canonical_json_bytes(failure) == (
        b'{"code":"BACKEND_TIMEOUT","details":[],"message":'
        b'"The backend exceeded the fixed wall time.","retryable":true,'
        b'"stage":"BACKEND_EXECUTE"}\n'
    )


def _evidence_manifest_entry(ordinal: int) -> dict[str, Any]:
    return {
        "input_kind": "EVIDENCE",
        "resource_id": f"00000000-0000-0000-0000-{ordinal:012d}",
        "relative_path": None,
        "resource_kind": None,
        "size": None,
        "sha256": None,
        "source_type": "USER_FACT",
        "source_ref": f"10000000-0000-0000-0000-{ordinal:012d}",
        "locator": {"kind": "USER_FACT", "input_name": f"input_{ordinal}"},
        "summary": "x",
        "content_hash": None,
    }


def _job_and_manifest_for_exact_required_bytes(
    target_manifest_bytes: int,
) -> tuple[Job, WorkspaceInputManifest]:
    job_payload = load_json(FIXTURE_ROOT / "positive" / "job-diagnose.json")
    entries = [_evidence_manifest_entry(ordinal) for ordinal in range(1, 5)]
    evidence_ids = [entry["resource_id"] for entry in entries]
    job_payload["attachment_refs"] = []
    job_payload["artifact_refs"] = []
    job_payload["previous_outcome_refs"] = []
    job_payload["evidence_refs"] = evidence_ids
    job_payload["logparse_tool_ref"] = None
    job_payload["logparse_product"] = None
    job_payload["context_snapshot"]["evidence_refs"] = evidence_ids
    manifest_payload = {
        "schema_version": 2,
        "job_id": job_payload["job_id"],
        "case_id": job_payload["case_id"],
        "job_type": job_payload["job_type"],
        "logparse_tool_ref": job_payload["logparse_tool_ref"],
        "logparse_product": job_payload["logparse_product"],
        "entries": entries,
        "resolved_logparse_plan": None,
        "review_subject": None,
    }

    remaining = target_manifest_bytes - len(canonical_json_bytes(manifest_payload))
    if remaining < 0:
        raise AssertionError("target is smaller than the minimal manifest")
    for entry in entries:
        extra = min(remaining, 65_535)
        entry["summary"] += "x" * extra
        remaining -= extra
    if remaining:
        raise AssertionError("target exceeds the four-field user-text capacity")

    job, manifest = _validate_manifest_for_job(job_payload, manifest_payload)
    assert len(canonical_json_bytes(manifest)) == target_manifest_bytes
    return job, manifest


def _bounded_required_context(
    job: Job, manifest: WorkspaceInputManifest, *, limit_bytes: int
) -> BoundedContext:
    instruction = JobInstructionPayload(
        job_id=job.job_id,
        job_type=job.job_type,
        goal=job.goal,
        base_state_revision=job.base_state_revision,
    )
    instruction_bytes = canonical_json_bytes(instruction)
    manifest_bytes = canonical_json_bytes(manifest)
    body_bytes = instruction_bytes + manifest_bytes
    sections = [
        ContextSection(
            ordinal=0,
            kind=ContextSectionKind.JOB_INSTRUCTION,
            source_refs=[job.job_id],
            required=True,
            utf8_bytes=len(instruction_bytes),
            content_sha256=hashlib.sha256(instruction_bytes).hexdigest(),
        ),
        ContextSection(
            ordinal=1,
            kind=ContextSectionKind.RESOURCE_MANIFEST,
            source_refs=[job.job_id],
            required=True,
            utf8_bytes=len(manifest_bytes),
            content_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        ),
    ]
    return BoundedContext(
        job_id=job.job_id,
        job_type=job.job_type,
        body=body_bytes.decode("utf-8"),
        sections=sections,
        utf8_bytes=len(body_bytes),
        limit_bytes=limit_bytes,
        body_sha256=hashlib.sha256(body_bytes).hexdigest(),
    )


def test_job_instruction_and_resource_manifest_fit_exactly_200_kib() -> None:
    seed_job = Job.model_validate(
        load_json(FIXTURE_ROOT / "positive" / "job-diagnose.json")
    )
    instruction_bytes = canonical_json_bytes(
        JobInstructionPayload(
            job_id=seed_job.job_id,
            job_type=seed_job.job_type,
            goal=seed_job.goal,
            base_state_revision=seed_job.base_state_revision,
        )
    )
    job, manifest = _job_and_manifest_for_exact_required_bytes(
        SPECIALIST_CONTEXT_BYTES - len(instruction_bytes)
    )
    context = _bounded_required_context(
        job, manifest, limit_bytes=SPECIALIST_CONTEXT_BYTES
    )

    assert context.utf8_bytes == SPECIALIST_CONTEXT_BYTES == 204_800
    assert [section.kind for section in context.sections] == [
        ContextSectionKind.JOB_INSTRUCTION,
        ContextSectionKind.RESOURCE_MANIFEST,
    ]
    assert all(section.required for section in context.sections)
    expected_instruction = canonical_json_bytes(
        {
            "job_id": job.job_id,
            "job_type": job.job_type.value,
            "goal": job.goal,
            "base_state_revision": job.base_state_revision,
        }
    )
    assert context.body.encode("utf-8").startswith(expected_instruction)
    assert context.body.encode("utf-8").endswith(canonical_json_bytes(manifest))

    with pytest.raises(ValidationError):
        JobInstructionPayload.model_validate(
            {
                **JobInstructionPayload(
                    job_id=job.job_id,
                    job_type=job.job_type,
                    goal=job.goal,
                    base_state_revision=job.base_state_revision,
                ).model_dump(mode="json"),
                "unexpected": True,
            }
        )


def test_job_instruction_must_match_all_four_current_job_fields() -> None:
    job = Job.model_validate(
        load_json(FIXTURE_ROOT / "positive" / "job-diagnose.json")
    )
    instruction = JobInstructionPayload(
        job_id=job.job_id,
        job_type=job.job_type,
        goal=job.goal,
        base_state_revision=job.base_state_revision,
    )

    assert validate_job_instruction_for_job(instruction, job) is instruction
    with pytest.raises(ValueError, match="exactly match"):
        validate_job_instruction_for_job(
            instruction.model_copy(update={"goal": f"{job.goal}!"}),
            job,
        )


def test_required_context_rejects_exactly_one_byte_over_200_kib() -> None:
    seed_job = Job.model_validate(
        load_json(FIXTURE_ROOT / "positive" / "job-diagnose.json")
    )
    instruction_size = len(
        canonical_json_bytes(
            JobInstructionPayload(
                job_id=seed_job.job_id,
                job_type=seed_job.job_type,
                goal=seed_job.goal,
                base_state_revision=seed_job.base_state_revision,
            )
        )
    )
    job, manifest = _job_and_manifest_for_exact_required_bytes(
        SPECIALIST_CONTEXT_BYTES - instruction_size + 1
    )

    with pytest.raises(ValidationError, match="exceeds limit_bytes"):
        _bounded_required_context(job, manifest, limit_bytes=SPECIALIST_CONTEXT_BYTES)


def _validate_user_result_semantics(
    job_payload: dict[str, Any],
    outcome_payload: dict[str, Any],
    result_bytes: bytes,
) -> UserResultPayload:
    job = Job.model_validate(job_payload)
    result = parse_canonical_json_bytes(result_bytes, model_type=UserResultPayload)
    candidate = outcome_payload["payload"]["candidate_conclusion_draft"]
    expected = {
        "problem_statement": job.context_snapshot.problem_spec.statement,
        "candidate_statement": candidate["statement"],
        "supporting_evidence_bindings": candidate["supporting_evidence_bindings"],
        "completion_criteria_mapping": candidate["completion_criteria_mapping"],
    }
    actual = result.model_dump(mode="json")
    for field, value in expected.items():
        if actual[field] != value:
            raise ValueError(f"USER_RESULT {field} does not match its candidate seam")
    return result


def test_user_result_fixture_is_canonical_and_matches_its_candidate() -> None:
    job = load_json(FIXTURE_ROOT / "positive" / "job-diagnose.json")
    outcome = load_json(
        FIXTURE_ROOT / "positive" / "agent-job-outcome-diagnosis.json"
    )
    result_bytes = (FIXTURE_ROOT / "positive" / "user-result.json").read_bytes()
    result = _validate_user_result_semantics(job, outcome, result_bytes)
    assert canonical_json_bytes(result) == result_bytes


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("problem_statement", "A different problem statement."),
        ("candidate_statement", "A different candidate statement."),
        (
            "supporting_evidence_bindings",
            [
                {
                    "existing_evidence_id": "00000000-0000-0000-0000-000000000041",
                    "evidence_proposal_key": None,
                }
            ],
        ),
        (
            "completion_criteria_mapping",
            [
                {
                    "criterion_index": 0,
                    "criterion": "Identify the timed-out request.",
                    "satisfied": True,
                    "evidence_bindings": [
                        {
                            "existing_evidence_id": "00000000-0000-0000-0000-000000000040",
                            "evidence_proposal_key": None,
                        }
                    ],
                    "explanation": "A semantically different mapping.",
                }
            ],
        ),
    ],
)
def test_user_result_rejects_each_semantic_mismatch(
    field: str, replacement: Any
) -> None:
    job = load_json(FIXTURE_ROOT / "positive" / "job-diagnose.json")
    outcome = load_json(
        FIXTURE_ROOT / "positive" / "agent-job-outcome-diagnosis.json"
    )
    result = load_json(FIXTURE_ROOT / "positive" / "user-result.json")
    result[field] = replacement
    result_bytes = canonical_json_bytes(UserResultPayload.model_validate(result))

    with pytest.raises(ValueError, match=field):
        _validate_user_result_semantics(job, outcome, result_bytes)


def _candidate_preimage(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "statement": candidate["statement"],
        "supporting_evidence_refs": candidate["supporting_evidence_refs"],
        "completion_criteria_mapping": candidate["completion_criteria_mapping"],
    }


def test_candidate_hash_is_status_stable_but_semantically_sensitive() -> None:
    candidate = load_json(FIXTURE_ROOT / "positive" / "job-review.json")[
        "context_snapshot"
    ]["candidate_conclusion"]
    second_evidence = "00000000-0000-0000-0000-000000000041"
    candidate["supporting_evidence_refs"].append(second_evidence)
    candidate["completion_criteria_mapping"][0]["evidence_refs"].append(
        second_evidence
    )
    candidate["content_hash"] = canonical_json_sha256(_candidate_preimage(candidate))
    baseline_hash = candidate["content_hash"]
    CandidateConclusion.model_validate(candidate)

    for status in CandidateStatus:
        changed = copy.deepcopy(candidate)
        changed.update(
            {
                "conclusion_id": "00000000-0000-0000-0000-000000000081",
                "revision": 9,
                "proposed_by_job_id": "00000000-0000-0000-0000-000000000013",
                "status": status.value,
            }
        )
        assert CandidateConclusion.model_validate(changed).content_hash == baseline_hash

    semantic_mutations = []
    statement_change = copy.deepcopy(candidate)
    statement_change["statement"] += " New content."
    semantic_mutations.append(statement_change)
    supporting_order_change = copy.deepcopy(candidate)
    supporting_order_change["supporting_evidence_refs"].reverse()
    semantic_mutations.append(supporting_order_change)
    mapping_change = copy.deepcopy(candidate)
    mapping_change["completion_criteria_mapping"][0]["explanation"] += " New mapping."
    semantic_mutations.append(mapping_change)

    for changed in semantic_mutations:
        with pytest.raises(ValidationError, match="content_hash"):
            CandidateConclusion.model_validate(changed)
        changed_hash = canonical_json_sha256(_candidate_preimage(changed))
        assert changed_hash != baseline_hash
        changed["content_hash"] = changed_hash
        assert CandidateConclusion.model_validate(changed).content_hash == changed_hash


_DETERMINISTIC_FAILURE_CASES = [
    (
        ErrorCode.OUTCOME_MISSING,
        ExecutionStage.OUTCOME_VALIDATE,
        "Job outcome validation failed.",
    ),
    (
        ErrorCode.OUTCOME_INVALID,
        ExecutionStage.OUTCOME_VALIDATE,
        "Job outcome validation failed.",
    ),
    (
        ErrorCode.EXECUTION_RECORD_FAILED,
        ExecutionStage.EXECUTION_RECORD,
        "Execution record validation failed.",
    ),
    (
        ErrorCode.RESOURCE_LIMIT_EXCEEDED,
        ExecutionStage.RESOURCE_STAGE,
        "Case resource capacity exceeded.",
    ),
    (
        ErrorCode.RESOURCE_HASH_MISMATCH,
        ExecutionStage.RESOURCE_STAGE,
        "Resource publication validation failed.",
    ),
]


def _failure_details(code: ErrorCode) -> list[ApplicationErrorDetail]:
    common = [
        ApplicationErrorDetail(
            field="zeta",
            resource_type="OUTCOME",
            resource_id="00000000-0000-0000-0000-000000000022",
            resource_ref=None,
            expected="expected-z",
            actual="actual-z",
            limit=None,
            observed=None,
        ),
        ApplicationErrorDetail(
            field="alpha",
            resource_type="JOB",
            resource_id="00000000-0000-0000-0000-000000000011",
            resource_ref=None,
            expected="expected-a",
            actual="actual-a",
            limit=None,
            observed=None,
        ),
    ]
    if code is ErrorCode.RESOURCE_LIMIT_EXCEEDED:
        common.append(
            ApplicationErrorDetail(
                field="case_resource_bytes",
                resource_type="CASE",
                resource_id="00000000-0000-0000-0000-000000000001",
                resource_ref=None,
                expected=None,
                actual=None,
                limit=5_368_709_120,
                observed=5_368_709_121,
            )
        )
    return common


@pytest.mark.parametrize(
    ("code", "stage", "message"), _DETERMINISTIC_FAILURE_CASES
)
def test_five_deterministic_rejections_have_frozen_canonical_bytes(
    code: ErrorCode, stage: ExecutionStage, message: str
) -> None:
    details = _failure_details(code)
    failure = deterministic_outcome_failure(code, reversed(details))
    expected_details = sorted(
        details,
        key=lambda item: (
            item.field or "",
            item.resource_type or "",
            item.resource_id or "",
        ),
    )
    expected_payload = {
        "stage": stage.value,
        "code": code.value,
        "message": message,
        "retryable": False,
        "details": [item.model_dump(mode="json") for item in expected_details],
    }
    independently_encoded = (
        json.dumps(
            expected_payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )

    assert set(DETERMINISTIC_OUTCOME_FAILURE_SPECS) == {
        item[0] for item in _DETERMINISTIC_FAILURE_CASES
    }
    assert failure.stage is stage
    assert failure.code is code
    assert failure.message == message
    assert failure.retryable is False
    assert failure.details == expected_details
    assert canonical_json_bytes(failure) == independently_encoded
    assert parse_canonical_json_bytes(
        independently_encoded, model_type=ExecutionFailure
    ) == failure
    assert canonical_json_bytes(
        deterministic_outcome_failure(code, details)
    ) == independently_encoded


def test_capacity_rejection_requires_frozen_limit_and_observed_detail() -> None:
    with pytest.raises(ValueError, match="limit=5368709120"):
        deterministic_outcome_failure(ErrorCode.RESOURCE_LIMIT_EXCEEDED, [])


def test_non_deterministic_code_is_rejected_by_the_frozen_failure_helper() -> None:
    with pytest.raises(ValueError, match="not a deterministic Outcome rejection"):
        deterministic_outcome_failure(ErrorCode.BACKEND_TIMEOUT, [])
