from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from problem_locator.contracts.enums import (
    ArtifactKind,
    ContextSectionKind,
    ErrorCode,
    EvidenceSourceType,
    ExecutionStage,
    JobType,
    OutcomeResultType,
    ResourceKind,
)
from problem_locator.contracts.models import (
    Evidence,
    ExecutionFailure,
    FixtureManifest,
    Job,
    JobInstructionPayload,
    JobOutcome,
    WorkspaceInputManifest,
)
from problem_locator.contracts.serialization import (
    canonical_json_bytes,
    canonical_json_sha256,
    schema_bundle_bytes,
)
from problem_locator.runtime.context_builder import (
    ContextBuilder,
    ContextLimitExceeded,
    ContextMaterials,
)
from tests.v2_helpers import blind_review_subject, resolved_logparse_plan


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
CONTRACT_FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "contracts" / "positive"
CONTEXT_FIXTURES = (
    REPOSITORY_ROOT / "tests" / "fixtures" / "components" / "runtime-context"
)
ASSET_ROOT = REPOSITORY_ROOT / "src" / "problem_locator" / "runtime" / "assets"
FIXED_TIME = "2026-01-02T03:04:05.000Z"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


MATERIAL_TEXT = _load_json(CONTEXT_FIXTURES / "materials.json")
EXPECTED_ORDER = _load_json(CONTEXT_FIXTURES / "expected-section-order.json")
UNICODE_SUMMARY = (CONTEXT_FIXTURES / "unicode-evidence.txt").read_text(
    encoding="utf-8"
).rstrip("\n")


def _base_job(job_type: JobType) -> Job:
    fixture_name = {
        JobType.ROUTE: "job-route.json",
        JobType.DIAGNOSE: "job-diagnose.json",
        JobType.REVIEW: "job-review.json",
    }[job_type]
    return Job.model_validate(_load_json(CONTRACT_FIXTURES / fixture_name))


def _minimal_job(job_type: JobType) -> Job:
    job = _base_job(job_type)
    if job_type is not JobType.DIAGNOSE:
        return job
    payload = job.model_dump(mode="json")
    payload.update(
        {
            "attachment_refs": [],
            "artifact_refs": [],
            "evidence_refs": [],
            "logparse_product": None,
            "logparse_tool_ref": None,
            "previous_outcome_refs": [],
        }
    )
    payload["context_snapshot"]["evidence_refs"] = []
    return Job.model_validate(payload)


def _previous_outcome(job: Job) -> JobOutcome | None:
    if not job.previous_outcome_refs:
        return None
    return JobOutcome(
        outcome_id=job.previous_outcome_refs[0],
        job_id="00000000-0000-0000-0000-000000000009",
        case_id=job.case_id,
        job_type=JobType.DIAGNOSE,
        base_state_revision=1,
        result_type=OutcomeResultType.FAILED,
        payload=None,
        consumed_evidence_refs=[],
        proposed_evidence=[],
        proposed_artifacts=[],
        error=ExecutionFailure(
            stage=ExecutionStage.CONTEXT_BUILD,
            code=ErrorCode.CONTEXT_LIMIT,
            message="Required context exceeds the fixed role budget.",
            retryable=False,
            details=[],
        ),
        produced_at=FIXED_TIME,
    )


def _evidence(job: Job, evidence_id: str, summary: str) -> Evidence:
    if not job.previous_outcome_refs:
        raise AssertionError("test Evidence requires a previous Outcome source")
    return Evidence.model_validate(
        {
            "evidence_id": evidence_id,
            "case_id": job.case_id,
            "source_type": EvidenceSourceType.PREVIOUS_OUTCOME.value,
            "source_ref": job.previous_outcome_refs[0],
            "locator": {"kind": "PREVIOUS_OUTCOME", "json_pointer": "/payload"},
            "summary": summary,
            "collected_at": FIXED_TIME,
            "content_hash": None,
            "resource_ref": None,
        }
    )


def _job_with_evidence(job: Job, evidence_ids: list[str]) -> Job:
    payload = job.model_dump(mode="json")
    payload["evidence_refs"] = evidence_ids
    snapshot_refs = payload["context_snapshot"]["evidence_refs"]
    for evidence_id in evidence_ids:
        if evidence_id not in snapshot_refs:
            snapshot_refs.append(evidence_id)
    return Job.model_validate(payload)


def _manifest(
    job: Job,
    evidence: tuple[Evidence, ...],
    previous_outcomes: tuple[JobOutcome, ...],
) -> WorkspaceInputManifest:
    exposed_previous_outcomes = (
        () if job.job_type is JobType.REVIEW else previous_outcomes
    )
    entries: list[dict[str, Any]] = []
    attachment_sha = "2" * 64
    for attachment_id in job.attachment_refs:
        entries.append(
            {
                "input_kind": "ATTACHMENT",
                "resource_id": attachment_id,
                "relative_path": f"inputs/attachments/{attachment_id}/payload",
                "resource_kind": "FILE",
                "size": 128,
                "sha256": attachment_sha,
                "content_type": "application/octet-stream",
                "filename_suffix": None,
            }
        )
    for item in evidence:
        entries.append(
            {
                "input_kind": "EVIDENCE",
                "resource_id": item.evidence_id,
                "relative_path": None,
                "resource_kind": None,
                "size": None,
                "sha256": None,
                "source_type": item.source_type.value,
                "source_ref": item.source_ref,
                "locator": item.locator.model_dump(mode="json"),
                "summary": item.summary,
                "content_hash": item.content_hash,
            }
        )
    for artifact_id in job.artifact_refs:
        artifact_sha = "1" * 64
        assert job.logparse_tool_ref is not None
        assert job.logparse_product is not None
        assert job.attachment_refs
        entries.append(
            {
                "input_kind": "ARTIFACT",
                "resource_id": artifact_id,
                "relative_path": f"inputs/artifacts/{artifact_id}/tree",
                "resource_kind": "DIRECTORY",
                "size": 512,
                "sha256": artifact_sha,
                "artifact_kind": ArtifactKind.LOGPARSE_RUN.value,
                "name": "saved-logparse-run",
                "content_type": "application/vnd.problem-locator.logparse-run+directory",
                "metadata": {
                    "tree_manifest_sha256": artifact_sha,
                    "logparse_version_ref": job.logparse_tool_ref.model_dump(mode="json"),
                    "parse_manifest_relative_path": "task/parse_manifest.json",
                    "source_attachment_id": job.attachment_refs[0],
                    "source_attachment_sha256": attachment_sha,
                    "parse_parameters": {"product": job.logparse_product},
                },
            }
        )
    for outcome in exposed_previous_outcomes:
        encoded = canonical_json_bytes(outcome)
        entries.append(
            {
                "input_kind": "PREVIOUS_OUTCOME",
                "resource_id": outcome.outcome_id,
                "relative_path": (
                    f"inputs/outcomes/{outcome.outcome_id}/job_outcome.json"
                ),
                "resource_kind": ResourceKind.FILE.value,
                "size": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "source_job_id": outcome.job_id,
                "result_type": outcome.result_type.value,
            }
        )
    return WorkspaceInputManifest.model_validate(
        {
            "schema_version": 2,
            "job_id": job.job_id,
            "case_id": job.case_id,
            "job_type": job.job_type.value,
            "logparse_tool_ref": (
                None
                if job.logparse_tool_ref is None
                else job.logparse_tool_ref.model_dump(mode="json")
            ),
            "logparse_product": job.logparse_product,
            "entries": entries,
            "resolved_logparse_plan": (
                None
                if job.logparse_tool_ref is None
                else resolved_logparse_plan(
                    job,
                    problem_time="2026-01-02T03:04:05.000Z",
                    anchors=[
                        {
                            "label": "request",
                            "module": "payment",
                            "slot": "caller",
                            "process_name": "payment-service",
                            "pid": None,
                        }
                    ],
                ).model_dump(mode="json")
            ),
            "review_subject": (
                blind_review_subject(job).model_dump(mode="json")
                if job.job_type is JobType.REVIEW
                else None
            ),
        }
    )


def _materials(
    job: Job,
    *,
    evidence: tuple[Evidence, ...] = (),
    previous_outcomes: tuple[JobOutcome, ...] = (),
    profile: str | None = None,
) -> ContextMaterials:
    exposed_previous_outcomes = (
        () if job.job_type is JobType.REVIEW else previous_outcomes
    )
    fixture = MATERIAL_TEXT[job.job_type.value.lower()]
    role_values = {
        "skill": fixture.get("skill"),
        "skill_index": fixture.get("skill_index"),
    }
    return ContextMaterials(
        profile=fixture["profile"] if profile is None else profile,
        tool_bundle=fixture["tool_bundle"],
        output_contract=fixture["output_contract"],
        manifest=_manifest(job, evidence, previous_outcomes),
        previous_outcomes=exposed_previous_outcomes,
        evidence=evidence,
        **role_values,
    )


def _complete_inputs(
    job_type: JobType,
) -> tuple[Job, ContextMaterials, tuple[JobOutcome, ...], tuple[Evidence, ...]]:
    job = _base_job(job_type)
    previous = _previous_outcome(job)
    previous_outcomes = () if previous is None else (previous,)
    evidence = tuple(
        _evidence(job, evidence_id, UNICODE_SUMMARY)
        for evidence_id in job.evidence_refs
    )
    return (
        job,
        _materials(
            job,
            evidence=evidence,
            previous_outcomes=previous_outcomes,
        ),
        previous_outcomes,
        evidence,
    )


def _section_bytes(context: Any, index: int) -> bytes:
    encoded = context.body.encode("utf-8")
    offset = sum(section.utf8_bytes for section in context.sections[:index])
    return encoded[offset : offset + context.sections[index].utf8_bytes]


def _section_content(context: Any, index: int) -> bytes:
    framed = _section_bytes(context, index)
    header = (
        f"<<<SECTION {index} {context.sections[index].kind.value}>>>\n"
    ).encode("ascii")
    trailer = b"<<<END SECTION>>>\n"
    assert framed.startswith(header) and framed.endswith(trailer)
    return framed[len(header) : -len(trailer)]


@pytest.mark.parametrize("job_type", list(JobType))
def test_three_roles_have_fixed_framing_order_and_required_core(
    job_type: JobType,
) -> None:
    job, materials, _, _ = _complete_inputs(job_type)
    context = ContextBuilder().build(job, materials)

    assert [section.kind.value for section in context.sections] == EXPECTED_ORDER[
        job_type.value
    ]
    assert [section.ordinal for section in context.sections] == list(
        range(len(context.sections))
    )
    assert context.body.encode("utf-8") == b"".join(
        _section_bytes(context, index) for index in range(len(context.sections))
    )
    assert all(
        _section_bytes(context, index).startswith(
            f"<<<SECTION {index} {section.kind.value}>>>\n".encode("ascii")
        )
        and _section_bytes(context, index).endswith(b"<<<END SECTION>>>\n")
        for index, section in enumerate(context.sections)
    )
    assert all(
        section.required
        for section in context.sections
        if section.kind is not ContextSectionKind.EVIDENCE
    )
    assert "\r" not in context.body


@pytest.mark.parametrize("job_type", list(JobType))
def test_output_contract_is_the_final_instruction_before_manifest(
    job_type: JobType,
) -> None:
    job, materials, _, _ = _complete_inputs(job_type)
    context = ContextBuilder().build(job, materials)
    kinds = [section.kind for section in context.sections]
    output_index = kinds.index(ContextSectionKind.OUTPUT_CONTRACT)

    assert output_index == len(kinds) - 2
    assert kinds[-1] is ContextSectionKind.RESOURCE_MANIFEST
    assert all(
        index < output_index
        for index, kind in enumerate(kinds)
        if kind in {
            ContextSectionKind.PREVIOUS_OUTCOME,
            ContextSectionKind.EVIDENCE,
        }
    )


def test_job_instruction_goal_and_resource_manifest_are_byte_exact() -> None:
    job, materials, _, _ = _complete_inputs(JobType.DIAGNOSE)
    context = ContextBuilder().build(job, materials)
    instruction_index = next(
        index
        for index, section in enumerate(context.sections)
        if section.kind is ContextSectionKind.JOB_INSTRUCTION
    )
    manifest_index = next(
        index
        for index, section in enumerate(context.sections)
        if section.kind is ContextSectionKind.RESOURCE_MANIFEST
    )

    assert _section_content(context, instruction_index) == canonical_json_bytes(
        JobInstructionPayload(
            job_id=job.job_id,
            job_type=job.job_type,
            goal=job.goal,
            base_state_revision=job.base_state_revision,
        )
    )
    assert job.goal.encode("utf-8") in _section_content(context, instruction_index)
    assert _section_content(context, manifest_index) == canonical_json_bytes(
        materials.manifest
    )
    assert context.sections[manifest_index].required


@pytest.mark.parametrize("job_type", list(JobType))
def test_production_output_contract_materializes_exact_installed_s00_schemas(
    job_type: JobType,
) -> None:
    job, materials, _, _ = _complete_inputs(job_type)
    production_contract = (
        ASSET_ROOT
        / "output-contracts"
        / job_type.value.lower()
        / "output-contract.md"
    ).read_text(encoding="utf-8")
    context = ContextBuilder().build(
        job,
        replace(materials, output_contract=production_contract),
    )
    output_index = next(
        index
        for index, section in enumerate(context.sections)
        if section.kind is ContextSectionKind.OUTPUT_CONTRACT
    )
    content = _section_content(context, output_index)
    expected = schema_bundle_bytes()
    markers = {
        "agent-job-outcome-draft.schema.json": (
            b"<<<BEGIN S00 AGENT JOB OUTCOME DRAFT SCHEMA>>>\n",
            b"<<<END S00 AGENT JOB OUTCOME DRAFT SCHEMA>>>",
        ),
    }
    if job_type is JobType.DIAGNOSE:
        assert b"<<<BEGIN S00 USER RESULT SCHEMA>>>" not in content
        assert b"<<<END S00 USER RESULT SCHEMA>>>" not in content
        assert b"{{S00_USER_RESULT_SCHEMA_JSON}}" not in content
        assert (
            b'application/vnd.problem-locator.logparse-run+directory' in content
        )
        assert b'not the hash of `parse_manifest.json`' in content
        assert b'`workspace_relative_path`' in content
        assert b'`AFTER_LOGPARSE`' in content
        assert b'order_id' not in content

    assert b"{{S00_" not in content
    for schema_name, (begin, end) in markers.items():
        assert content.count(begin) == 1
        assert content.count(end) == 1
        start = content.index(begin) + len(begin)
        finish = content.index(end, start)
        assert content[start:finish] == expected[schema_name]


def test_previous_outcome_is_full_canonical_dto_and_manifest_hash_is_checked() -> None:
    job, materials, previous, _ = _complete_inputs(JobType.DIAGNOSE)
    context = ContextBuilder().build(job, materials)
    previous_index = next(
        index
        for index, section in enumerate(context.sections)
        if section.kind is ContextSectionKind.PREVIOUS_OUTCOME
    )
    assert _section_content(context, previous_index) == canonical_json_bytes(previous[0])
    assert context.sections[previous_index].source_refs == [previous[0].outcome_id]

    manifest_payload = materials.manifest.model_dump(mode="json")
    previous_entry = next(
        entry
        for entry in manifest_payload["entries"]
        if entry["input_kind"] == "PREVIOUS_OUTCOME"
    )
    previous_entry["sha256"] = "9" * 64
    drifted = replace(
        materials,
        manifest=WorkspaceInputManifest.model_validate(manifest_payload),
    )
    with pytest.raises(ValueError, match="previous Outcome metadata drifted"):
        ContextBuilder().build(job, drifted)


def test_unicode_bytes_and_section_hashes_use_exact_framed_utf8() -> None:
    job, materials, _, _ = _complete_inputs(JobType.REVIEW)
    context = ContextBuilder().build(job, materials)
    evidence_index = next(
        index
        for index, section in enumerate(context.sections)
        if section.kind is ContextSectionKind.EVIDENCE
    )
    framed = _section_bytes(context, evidence_index)

    assert UNICODE_SUMMARY.encode("utf-8") in framed
    assert len(framed) > len(framed.decode("utf-8"))
    assert context.sections[evidence_index].utf8_bytes == len(framed)
    assert context.sections[evidence_index].content_sha256 == hashlib.sha256(
        framed
    ).hexdigest()


@pytest.mark.parametrize("job_type", list(JobType))
def test_each_role_accepts_exact_limit_and_rejects_one_extra_byte(
    job_type: JobType,
) -> None:
    job = _minimal_job(job_type)
    previous = _previous_outcome(job)
    previous_outcomes = () if previous is None else (previous,)
    evidence = tuple(
        _evidence(job, evidence_id, "required Evidence")
        for evidence_id in job.evidence_refs
    )
    base = _materials(
        job,
        evidence=evidence,
        previous_outcomes=previous_outcomes,
        profile="P",
    )
    initial = ContextBuilder().build(job, base)
    padding = job.resource_limits.context_bytes - initial.utf8_bytes
    assert padding > 0
    exact_materials = replace(base, profile="P" + ("x" * padding))

    exact = ContextBuilder().build(job, exact_materials)
    assert exact.utf8_bytes == exact.limit_bytes == job.resource_limits.context_bytes

    with pytest.raises(ContextLimitExceeded) as caught:
        ContextBuilder().build(
            job,
            replace(exact_materials, profile=exact_materials.profile + "x"),
        )
    assert caught.value.observed == job.resource_limits.context_bytes + 1
    assert caught.value.limit == job.resource_limits.context_bytes


def test_manifest_is_reserved_before_optional_evidence_and_scan_is_job_ordered() -> None:
    ids = [
        "00000000-0000-0000-0000-000000000040",
        "00000000-0000-0000-0000-000000000041",
        "00000000-0000-0000-0000-000000000042",
        "00000000-0000-0000-0000-000000000043",
    ]
    job = _job_with_evidence(_base_job(JobType.DIAGNOSE), ids)
    evidence = (
        _evidence(job, ids[0], "a" * 30_000),
        _evidence(job, ids[1], "b" * 50_000),
        _evidence(job, ids[2], "c" * 50_000),
        _evidence(job, ids[3], "small trailing Evidence"),
    )
    previous = _previous_outcome(job)
    assert previous is not None
    materials = _materials(
        job,
        evidence=evidence,
        previous_outcomes=(previous,),
    )
    context = ContextBuilder().build(job, materials)
    selected_ids = [
        section.source_refs[0]
        for section in context.sections
        if section.kind is ContextSectionKind.EVIDENCE
    ]

    assert selected_ids == [ids[0], ids[3]]
    assert all(
        not section.required
        for section in context.sections
        if section.kind is ContextSectionKind.EVIDENCE
    )
    assert context.sections[-1].kind is ContextSectionKind.RESOURCE_MANIFEST
    assert _section_content(context, len(context.sections) - 1) == canonical_json_bytes(
        materials.manifest
    )


def test_reviewer_candidate_evidence_union_is_required_when_optional_is_skipped() -> None:
    supporting_id = "00000000-0000-0000-0000-000000000040"
    completion_only_id = "00000000-0000-0000-0000-000000000041"
    optional_id = "00000000-0000-0000-0000-000000000042"
    payload = _base_job(JobType.REVIEW).model_dump(mode="json")
    candidate = payload["context_snapshot"]["candidate_conclusion"]
    assert candidate is not None
    candidate["completion_criteria_mapping"][0]["evidence_refs"] = [
        completion_only_id
    ]
    candidate["content_hash"] = canonical_json_sha256(
        {
            "statement": candidate["statement"],
            "supporting_evidence_refs": candidate["supporting_evidence_refs"],
            "completion_criteria_mapping": candidate["completion_criteria_mapping"],
        }
    )
    payload["review_target"]["candidate_content_hash"] = candidate["content_hash"]
    payload["evidence_refs"] = [supporting_id, completion_only_id, optional_id]
    payload["context_snapshot"]["evidence_refs"] = [
        supporting_id,
        completion_only_id,
        optional_id,
    ]
    job = Job.model_validate(payload)
    evidence = (
        _evidence(job, supporting_id, "supporting Evidence"),
        _evidence(job, completion_only_id, "completion-only Evidence"),
        _evidence(job, optional_id, "z" * 65_000),
    )
    previous = _previous_outcome(job)
    assert previous is not None
    context = ContextBuilder().build(
        job,
        _materials(
            job,
            evidence=evidence,
            previous_outcomes=(previous,),
            profile="R" * 80_000,
        ),
    )
    evidence_sections = [
        section
        for section in context.sections
        if section.kind is ContextSectionKind.EVIDENCE
    ]

    assert [section.source_refs[0] for section in evidence_sections] == [
        supporting_id,
        completion_only_id,
    ]
    assert all(section.required for section in evidence_sections)


def test_context_fixture_manifest_covers_every_owned_byte() -> None:
    manifest_path = CONTEXT_FIXTURES / "fixture-manifest.json"
    payload = _load_json(manifest_path)
    manifest = FixtureManifest.model_validate(payload)
    assert canonical_json_bytes(manifest) == manifest_path.read_bytes()
    assert manifest.owner_spec == "S04"
    assert manifest.root == "tests/fixtures/components/runtime-context"

    actual = {
        path.relative_to(CONTEXT_FIXTURES).as_posix(): path
        for path in CONTEXT_FIXTURES.rglob("*")
        if path.is_file() and path != manifest_path
    }
    assert [entry.path for entry in manifest.files] == sorted(actual)
    for entry in manifest.files:
        path = actual[entry.path]
        assert not path.is_symlink()
        data = path.read_bytes()
        assert entry.size == len(data)
        assert entry.sha256 == hashlib.sha256(data).hexdigest()
