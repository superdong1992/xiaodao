from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from problem_locator.contracts import (
    Artifact,
    ArtifactKind,
    GenericDiagnosisOutcome,
    GenericDiagnosisOutcomeV2,
    GenericReportMetadataV1,
    GenericResult,
    GenericResultStatus,
    GenericResultV2,
    GenericResultV2Draft,
    ResourceKind,
    finalize_generic_result_v2,
)


JOB_ID = "00000000-0000-4000-8000-000000000101"
OUTCOME_ID = "00000000-0000-4000-8000-000000000102"
ARTIFACT_ID = "00000000-0000-4000-8000-000000000103"
CASE_ID = "00000000-0000-4000-8000-000000000104"
REPORT = "# 原生 Markdown\n\n```text\n保留代码围栏与最终换行\n```\n"
REPORT_BYTES = REPORT.encode("utf-8")
REPORT_SHA256 = hashlib.sha256(REPORT_BYTES).hexdigest()


def test_generic_v1_public_payload_shape_remains_exact() -> None:
    payload = GenericDiagnosisOutcome(
        status=GenericResultStatus.RESOLVED,
        conclusion="Legacy conclusion.",
        root_cause_analysis="Legacy root-cause analysis.",
        skill_name="generic-problem-locator-smoke",
    )

    assert payload.model_dump(mode="json") == {
        "status": "RESOLVED",
        "conclusion": "Legacy conclusion.",
        "root_cause_analysis": "Legacy root-cause analysis.",
        "skill_name": "generic-problem-locator-smoke",
    }
    result = GenericResult(
        status=GenericResultStatus.RESOLVED,
        conclusion="Legacy conclusion.",
        root_cause_analysis="Legacy root-cause analysis.",
        skill_name="generic-problem-locator-smoke",
        source_job_id=JOB_ID,
        source_outcome_id=OUTCOME_ID,
        occurred_at="2026-08-18T00:00:00.000Z",
    )
    assert result.model_dump(mode="json") == {
        "status": "RESOLVED",
        "conclusion": "Legacy conclusion.",
        "root_cause_analysis": "Legacy root-cause analysis.",
        "skill_name": "generic-problem-locator-smoke",
        "source_job_id": JOB_ID,
        "source_outcome_id": OUTCOME_ID,
        "occurred_at": "2026-08-18T00:00:00.000Z",
    }


def test_generic_v2_models_bind_exact_markdown_bytes_and_artifact_id() -> None:
    payload = GenericDiagnosisOutcomeV2(
        format_version=2,
        status=GenericResultStatus.RESOLVED,
        report_markdown=REPORT,
        report_utf8_size=len(REPORT_BYTES),
        report_sha256=REPORT_SHA256,
        skill_name="generic-problem-locator-smoke",
    )
    draft = GenericResultV2Draft(
        **payload.model_dump(mode="python"),
        source_job_id=JOB_ID,
        source_outcome_id=OUTCOME_ID,
        occurred_at="2026-08-18T00:00:00.000Z",
    )

    result = finalize_generic_result_v2(draft, ARTIFACT_ID)

    assert result.report_markdown.encode("utf-8") == REPORT_BYTES
    assert result.report_utf8_size == len(REPORT_BYTES)
    assert result.report_sha256 == REPORT_SHA256
    assert result.report_artifact_id == ARTIFACT_ID


@pytest.mark.parametrize(
    "overrides",
    [
        {"report_utf8_size": len(REPORT_BYTES) + 1},
        {"report_sha256": "0" * 64},
    ],
)
def test_generic_v2_rejects_report_size_or_hash_drift(
    overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "format_version": 2,
        "status": GenericResultStatus.RESOLVED,
        "report_markdown": REPORT,
        "report_utf8_size": len(REPORT_BYTES),
        "report_sha256": REPORT_SHA256,
        "skill_name": "generic-problem-locator-smoke",
    }
    values.update(overrides)

    with pytest.raises(ValidationError):
        GenericDiagnosisOutcomeV2.model_validate(values)


@pytest.mark.parametrize(
    ("model_type", "extra"),
    [
        (GenericDiagnosisOutcomeV2, {}),
        (
            GenericResultV2Draft,
            {
                "source_job_id": JOB_ID,
                "source_outcome_id": OUTCOME_ID,
                "occurred_at": "2026-08-18T00:00:00.000Z",
            },
        ),
        (
            GenericResultV2,
            {
                "report_artifact_id": ARTIFACT_ID,
                "source_job_id": JOB_ID,
                "source_outcome_id": OUTCOME_ID,
                "occurred_at": "2026-08-18T00:00:00.000Z",
            },
        ),
    ],
)
def test_generic_v2_contract_models_reject_a_leading_utf8_bom(
    model_type: type,
    extra: dict[str, object],
) -> None:
    report = "\ufeff" + REPORT
    report_bytes = report.encode("utf-8")
    with pytest.raises(ValidationError):
        model_type.model_validate(
            {
                "format_version": 2,
                "status": "RESOLVED",
                "report_markdown": report,
                "report_utf8_size": len(report_bytes),
                "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
                "skill_name": "generic-problem-locator-smoke",
                **extra,
            }
        )


def test_generic_v2_rejects_mixed_legacy_fields() -> None:
    with pytest.raises(ValidationError):
        GenericDiagnosisOutcomeV2.model_validate(
            {
                "format_version": 2,
                "status": "RESOLVED",
                "report_markdown": REPORT,
                "report_utf8_size": len(REPORT_BYTES),
                "report_sha256": REPORT_SHA256,
                "skill_name": "generic-problem-locator-smoke",
                "conclusion": "must not coexist with V2",
            }
        )


def test_generic_report_artifact_metadata_must_match_formal_bytes() -> None:
    metadata = GenericReportMetadataV1(
        schema_version=1,
        format_id="problem-locator-generic-report-v1",
        description="服务端从权威 V2 Outcome 生成的原生 Markdown 报告。",
        generic_result_format_version=2,
        status=GenericResultStatus.RESOLVED,
        source_job_id=JOB_ID,
        source_outcome_id=OUTCOME_ID,
        report_utf8_size=len(REPORT_BYTES),
        report_sha256=REPORT_SHA256,
    )
    artifact = Artifact(
        artifact_id=ARTIFACT_ID,
        case_id=CASE_ID,
        kind=ArtifactKind.GENERIC_REPORT,
        name="generic-diagnosis-report.md",
        content_type="text/markdown",
        resource_kind=ResourceKind.FILE,
        size=len(REPORT_BYTES),
        sha256=REPORT_SHA256,
        storage_key=f"cases/{CASE_ID}/artifacts/{ARTIFACT_ID}",
        metadata=metadata,
        created_by_job_id=JOB_ID,
        created_at="2026-08-18T00:00:01.000Z",
    )

    assert artifact.sha256 == REPORT_SHA256
    with pytest.raises(ValidationError):
        Artifact.model_validate(
            {
                **artifact.model_dump(mode="python"),
                "size": len(REPORT_BYTES) + 1,
            }
        )
