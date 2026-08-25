from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from problem_locator.application.audit_bundle import (
    AUDIT_BUNDLE_LOG_MAX_BYTES,
    AuditBundleSource,
    build_audit_bundle,
)
from problem_locator.application.audit_bundle_assembler import (
    _source_draft_filename,
    _validated_decision_evidence_bytes,
    _validated_review_subject_bytes,
)
from problem_locator.contracts import (
    DecisionAuditV2,
    Job,
    JobOutcome,
    WorkspaceInputManifest,
    canonical_json_bytes,
)


FIXTURES = Path(__file__).resolve().parents[4] / "tests/fixtures/contracts/positive"


@pytest.mark.parametrize(
    ("fixture", "expected"),
    (
        ("job-route.json", "agent_job_outcome.draft.json"),
        ("job-diagnose.json", "method-diagnosis.draft.json"),
        ("job-review.json", "method-review.draft.json"),
    ),
)
def test_audit_bundle_reads_the_protocol_specific_source_draft(
    fixture: str,
    expected: str,
) -> None:
    job = Job.model_validate_json((FIXTURES / fixture).read_bytes())

    assert _source_draft_filename(job) == expected


def test_audit_bundle_is_byte_deterministic_and_manifested() -> None:
    sources = [
        AuditBundleSource("case/summary.json", b'{"status":"UNRESOLVED"}'),
        AuditBundleSource("jobs/diagnose/context.txt", b"fixed context\n"),
    ]
    first = build_audit_bundle(
        case_id="00000000-0000-0000-0000-000000000001",
        source_outcome_id="00000000-0000-0000-0000-000000000002",
        sources=sources,
    )
    second = build_audit_bundle(
        case_id="00000000-0000-0000-0000-000000000001",
        source_outcome_id="00000000-0000-0000-0000-000000000002",
        sources=sources,
    )
    assert first.payload == second.payload
    assert first.sha256 == second.sha256
    with zipfile.ZipFile(io.BytesIO(first.payload)) as archive:
        assert archive.namelist()[0] == "manifest.json"
        manifest = json.loads(archive.read("manifest.json"))
        assert [entry["path"] for entry in manifest["entries"]] == [
            "case/summary.json",
            "jobs/diagnose/context.txt",
        ]


def test_audit_bundle_truncates_observable_logs_with_explicit_omission() -> None:
    raw = b"a" * (AUDIT_BUNDLE_LOG_MAX_BYTES + 100)
    built = build_audit_bundle(
        case_id="00000000-0000-0000-0000-000000000001",
        source_outcome_id="00000000-0000-0000-0000-000000000002",
        sources=[
            AuditBundleSource(
                "jobs/diagnose/stdout.log",
                raw,
                required=False,
                truncate_as_log=True,
            )
        ],
    )
    record = built.manifest["entries"][0]
    assert record["original_size"] == len(raw)
    assert record["retained_size"] == AUDIT_BUNDLE_LOG_MAX_BYTES
    assert record["omissions"][0]["kind"] == "MIDDLE_OMITTED"


@pytest.mark.parametrize("path", ["../secret", "/absolute", "a//b"])
def test_audit_bundle_rejects_unsafe_entry_paths(path: str) -> None:
    with pytest.raises(ValueError):
        build_audit_bundle(
            case_id="00000000-0000-0000-0000-000000000001",
            source_outcome_id="00000000-0000-0000-0000-000000000002",
            sources=[AuditBundleSource(path, b"x")],
        )


def test_review_subject_audit_bytes_are_bound_to_the_review_outcome() -> None:
    job = Job.model_validate_json((FIXTURES / "job-review.json").read_bytes())
    outcome = JobOutcome.model_validate_json(
        (FIXTURES / "job-outcome-review.json").read_bytes()
    )
    manifest = WorkspaceInputManifest.model_validate_json(
        (FIXTURES / "workspace-input-manifest-review.json").read_bytes()
    )
    assert manifest.review_subject is not None
    subject_bytes = canonical_json_bytes(manifest.review_subject)

    assert _validated_review_subject_bytes(
        subject_bytes=subject_bytes,
        job=job,
        outcome=outcome,
    ) == subject_bytes

    corrupted = manifest.review_subject.model_copy(
        update={"subject_hash": "0" * 64}
    )
    with pytest.raises(ValueError, match="REVIEW subject"):
        _validated_review_subject_bytes(
            subject_bytes=canonical_json_bytes(corrupted),
            job=job,
            outcome=outcome,
        )


def test_decision_evidence_jsonl_exactly_matches_audit_physical_lines() -> None:
    outcome = JobOutcome.model_validate_json(
        (FIXTURES / "job-outcome-diagnosis.json").read_bytes()
    )
    assert outcome.decision_audit is not None
    value = outcome.decision_audit.model_dump(mode="json")
    evaluation = value["rules"][0]["server_evaluation"]
    evaluation["evidence_bindings"] = [
        {
            "existing_evidence_id": (
                "00000000-0000-0000-0000-000000000040"
            ),
            "evidence_proposal_key": None,
        }
    ]
    evaluation["line_ranges"] = [
        {
            "path": "targets/timeout.log",
            "line_start": 7,
            "line_end": 7,
            "raw_bytes_sha256": "a" * 64,
        }
    ]
    audit = DecisionAuditV2.model_validate(value)
    evidence_bytes = canonical_json_bytes(
        {
            "schema_version": 1,
            "evidence_ref": "00000000-0000-0000-0000-000000000040",
            "anchor": "client",
            "relative_path": "targets/timeout.log",
            "line_number": 7,
            "raw_line": "observable timeout line",
            "raw_line_sha256": "a" * 64,
        }
    )

    assert _validated_decision_evidence_bytes(
        evidence_bytes=evidence_bytes,
        audit=audit,
    ) == evidence_bytes
