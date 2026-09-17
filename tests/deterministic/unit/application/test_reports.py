"""Published report reads preserve the public artifact boundary without evidence replay."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from problem_locator.application.reports import MAX_REPORT_BYTES, read_published_report
from problem_locator.contracts import (
    ApplicationPortError, Artifact, ArtifactKind, CandidateConclusion, Case,
    CaseAggregate, CaseStatus, ErrorCode, GenericResult, GenericResultV2,
    ResourceKind, UnresolvedResult, UserResultPayloadV3, canonical_json_bytes,
)


FIXTURES = Path(__file__).parents[3] / "fixtures" / "contracts" / "positive"
CASE_ID = "00000000-0000-0000-0000-000000000001"
JOB_ID = "00000000-0000-0000-0000-000000000011"
ARTIFACT_ID = "00000000-0000-0000-0000-000000000070"
OTHER_ID = "00000000-0000-0000-0000-000000000099"
NOW = "2026-07-31T00:00:00.000Z"


class BytesStore:
    def __init__(self, content):
        self.content = content
        self.opened = []
        self.streams = []

    def open_read(self, resource):
        self.opened.append(resource)
        stream = io.BytesIO(self.content)
        self.streams.append(stream)
        return stream


def _artifact(content, **updates):
    values = dict(artifact_id=ARTIFACT_ID, case_id=CASE_ID, kind=ArtifactKind.USER_RESULT,
        name="diagnosis-result.json", content_type="application/json", resource_kind=ResourceKind.FILE,
        size=len(content), sha256=hashlib.sha256(content).hexdigest(),
        storage_key=f"resources/cases/{CASE_ID}/artifacts/{ARTIFACT_ID}/payload",
        metadata={"schema_version": 3, "format_id": "problem-locator-diagnosis-v3", "description": "正式报告。"},
        created_by_job_id=JOB_ID, created_at=NOW)
    # Construct permits corrupt ownership/type/metadata in negative read-side tests.
    values.update(updates)
    return Artifact.model_construct(**values)


def _aggregate(case, artifact=None):
    return CaseAggregate.model_construct(case=case, jobs={}, outcomes={}, outcome_processing_records={},
        execution_failure_records={}, attachments={}, evidence={},
        artifacts={} if artifact is None else {artifact.artifact_id: artifact})


def _case(**updates):
    values = json.loads((FIXTURES / "state.json").read_bytes())["cases"][CASE_ID]["case"]
    case = Case.model_validate(values)
    return case.model_copy(update={"active_job_id": None, **updates})


def specialist_report(status="COMPLETED", *, content=None):
    payload = json.loads((FIXTURES / "user-result.json").read_bytes())
    payload["status"] = status
    if status != "COMPLETED":
        payload["root_cause"] = None
        payload["evidence_gaps"] = ["缺少完整日志，无法核对全部条件。"]
        payload["completion_criteria_mapping"][0].update(status="UNKNOWN", evidence_bindings=[])
    if status == "INCONCLUSIVE":
        payload.update(causal_factors=[], findings=[], supporting_evidence_bindings=[], verification_rules=[])
    report = UserResultPayloadV3.model_validate(payload)
    content = canonical_json_bytes(report) if content is None else content
    artifact = _artifact(content)
    case_status = {"COMPLETED": CaseStatus.RESOLVED, "PARTIAL": CaseStatus.PARTIALLY_RESOLVED,
        "INCONCLUSIVE": CaseStatus.UNRESOLVED}[status]
    # Minimal result identities intentionally make evidence replay impossible:
    # this API reads a published report; it does not re-run diagnosis validation.
    candidate = CandidateConclusion.model_construct(proposed_by_job_id=JOB_ID)
    unresolved = UnresolvedResult.model_construct(source_job_id=JOB_ID, user_result_artifact_id=ARTIFACT_ID)
    case = _case(status=case_status, final_result=candidate if status != "INCONCLUSIVE" else None,
        unresolved_result=unresolved if status == "INCONCLUSIVE" else None, archive_status="PENDING")
    return _aggregate(case, artifact), BytesStore(content), report


def generic_report(version=2, *, content="# 定位报告\r\n\r\n保留原始换行和中文。\n".encode()):
    common = dict(status="RESOLVED", skill_name="generic-problem-locator-smoke", source_job_id=JOB_ID,
        source_outcome_id=OTHER_ID, occurred_at=NOW)
    if version == 1:
        result = GenericResult.model_validate({**common, "conclusion": "历史定位结论。",
            "root_cause_analysis": "历史根因分析。"})
        return _aggregate(_case(status=CaseStatus.RESOLVED, selected_skill_ref=None,
            generic_result=result)), BytesStore(content), result
    result = GenericResultV2.model_validate({**common, "format_version": 2,
        "report_markdown": content.decode(), "report_utf8_size": len(content),
        "report_sha256": hashlib.sha256(content).hexdigest(), "report_artifact_id": ARTIFACT_ID})
    artifact = _artifact(content, kind=ArtifactKind.GENERIC_REPORT, name="report.md", content_type="text/markdown")
    return _aggregate(_case(status=CaseStatus.RESOLVED, selected_skill_ref=None,
        generic_result_v2=result), artifact), BytesStore(content), result


@pytest.mark.parametrize("status", ["COMPLETED", "PARTIAL", "INCONCLUSIVE"])
def test_reads_complete_partial_and_inconclusive_without_revalidating_evidence(status):
    aggregate, resources, expected = specialist_report(status)
    result = read_published_report(aggregate, resources)
    assert result.case is aggregate.case
    assert result.report == expected and result.format == "problem-locator-diagnosis-v3"
    assert result.artifact.artifact_id == ARTIFACT_ID and result.source_job_id == JOB_ID
    assert len(resources.opened) == 1 and resources.streams[0].closed
    assert "storage_key" not in result.artifact.model_dump()


@pytest.mark.parametrize("status", [CaseStatus.NEW, CaseStatus.RUNNING, CaseStatus.WAITING_INPUT,
    CaseStatus.WAITING_ATTACHMENT, CaseStatus.REVIEWING, CaseStatus.FAILED, CaseStatus.CANCELLED,
    CaseStatus.INTERRUPTED])
def test_non_result_states_do_not_read_any_artifact(status):
    aggregate, resources, _ = specialist_report()
    aggregate = aggregate.model_copy(update={"case": aggregate.case.model_copy(update={"status": status})})
    result = read_published_report(aggregate, resources)
    assert result.format is result.report is result.artifact is None and resources.opened == []


@pytest.mark.parametrize("version", [1, 2])
def test_generic_historical_results_preserve_their_original_representation(version):
    aggregate, resources, expected = generic_report(version)
    result = read_published_report(aggregate, resources)
    assert result.source_job_id == JOB_ID
    if version == 1:
        assert result.format == "generic-v1" and result.report == expected
        assert result.markdown is result.artifact is None and resources.opened == []
    else:
        assert result.format == "markdown" and result.markdown.encode() == resources.content
        assert result.report is None and result.artifact.kind is ArtifactKind.GENERIC_REPORT


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "foreign-case", "foreign-job", "directory", "mime", "name"])
def test_only_unique_public_report_with_matching_ownership_and_type_is_read(mutation):
    aggregate, resources, _ = specialist_report()
    artifact = aggregate.artifacts[ARTIFACT_ID]
    updates = {
        "foreign-case": {"case_id": OTHER_ID}, "foreign-job": {"created_by_job_id": OTHER_ID},
        "directory": {"resource_kind": ResourceKind.DIRECTORY}, "mime": {"content_type": "text/plain"},
        "name": {"name": "private.json"},
    }
    artifacts = {} if mutation == "missing" else {ARTIFACT_ID: artifact.model_copy(update=updates.get(mutation, {}))}
    if mutation == "duplicate":
        artifacts[OTHER_ID] = artifact.model_copy(update={"artifact_id": OTHER_ID})
    aggregate = aggregate.model_copy(update={"artifacts": artifacts})
    with pytest.raises(ApplicationPortError) as caught:
        read_published_report(aggregate, resources)
    assert caught.value.error.code is ErrorCode.STATE_CORRUPT and resources.opened == []


def test_old_job_reports_are_ignored_when_one_current_report_is_published():
    aggregate, resources, expected = specialist_report()
    artifacts = dict(aggregate.artifacts)
    artifacts[OTHER_ID] = artifacts[ARTIFACT_ID].model_copy(update={"artifact_id": OTHER_ID,
        "created_by_job_id": OTHER_ID})
    result = read_published_report(aggregate.model_copy(update={"artifacts": artifacts}), resources)
    assert result.report == expected and len(resources.opened) == 1


def test_terminal_case_without_result_identity_is_a_controlled_storage_error():
    aggregate, resources, _ = specialist_report()
    aggregate = aggregate.model_copy(update={"case": aggregate.case.model_copy(update={"final_result": None})})
    with pytest.raises(ApplicationPortError) as caught:
        read_published_report(aggregate, resources)
    assert caught.value.error.code is ErrorCode.STATE_CORRUPT and resources.opened == []


@pytest.mark.parametrize("field", ["report_artifact_id", "source_job_id", "report_utf8_size", "report_sha256"])
def test_generic_publication_must_match_the_frozen_artifact_identity(field):
    aggregate, resources, _ = generic_report()
    wrong = {"report_artifact_id": OTHER_ID, "source_job_id": OTHER_ID,
        "report_utf8_size": len(resources.content) + 1, "report_sha256": "f" * 64}
    generic = aggregate.case.generic_result_v2.model_copy(update={field: wrong[field]})
    case = aggregate.case.model_copy(update={"generic_result_v2": generic})
    with pytest.raises(ApplicationPortError) as caught:
        read_published_report(aggregate.model_copy(update={"case": case}), resources)
    assert caught.value.error.code is ErrorCode.STATE_CORRUPT and resources.opened == []


@pytest.mark.parametrize("difference", [-1, 1])
def test_actual_content_size_must_match_frozen_metadata(difference):
    aggregate, resources, _ = specialist_report()
    resources.content = resources.content[:-1] if difference < 0 else resources.content + b" "
    with pytest.raises(ApplicationPortError) as caught:
        read_published_report(aggregate, resources)
    assert caught.value.error.code is ErrorCode.RESOURCE_SIZE_MISMATCH
    assert resources.streams[0].closed


def test_actual_sha256_must_match_frozen_metadata():
    aggregate, resources, _ = specialist_report()
    resources.content = resources.content.replace(b"inventory", b"InventorY", 1)
    with pytest.raises(ApplicationPortError) as caught:
        read_published_report(aggregate, resources)
    assert caught.value.error.code is ErrorCode.RESOURCE_HASH_MISMATCH
    assert resources.streams[0].closed


def test_report_limit_rejects_before_opening_a_file():
    aggregate, resources, _ = specialist_report()
    artifact = aggregate.artifacts[ARTIFACT_ID].model_copy(update={"size": MAX_REPORT_BYTES + 1})
    with pytest.raises(ApplicationPortError) as caught:
        read_published_report(aggregate.model_copy(update={"artifacts": {ARTIFACT_ID: artifact}}), resources)
    assert caught.value.error.code is ErrorCode.RESOURCE_LIMIT_EXCEEDED and resources.opened == []


def test_exact_report_limit_can_be_read_without_loading_more_than_the_recorded_bytes():
    # Individual text fields retain their 64 KiB contract while the complete
    # canonical JSON reaches the aggregate read limit exactly.
    _, _, expected = specialist_report()
    payload = expected.model_dump(mode="json")
    payload["limitations"] = [f"{index:03d}:" + "x" * 65_500 for index in range(256)] + ["x"]
    padding = MAX_REPORT_BYTES - len(canonical_json_bytes(payload))
    assert 0 < padding < 65_535
    payload["limitations"][-1] += "x" * padding
    content = canonical_json_bytes(payload)
    assert len(content) == MAX_REPORT_BYTES
    aggregate, resources, _ = specialist_report(content=content)
    result = read_published_report(aggregate, resources)
    assert canonical_json_bytes(result.report) == content and resources.streams[0].closed


@pytest.mark.parametrize("content", [b"\xff", b'{"status":"COMPLETED","status":"COMPLETED"}',
    b'{"value":NaN}', b"{}", b"[]", b"```json\n{}\n```", b"\xef\xbb\xbf{}"])
def test_published_json_is_strict_even_when_model_output_accepts_presentational_wrappers(content):
    aggregate, resources, _ = specialist_report(content=content)
    with pytest.raises(ApplicationPortError) as caught:
        read_published_report(aggregate, resources)
    assert caught.value.error.code is ErrorCode.STATE_CORRUPT
    assert resources.streams[0].closed


def test_json_report_status_must_match_the_case_terminal_status():
    aggregate, resources, _ = specialist_report()
    aggregate = aggregate.model_copy(update={"case": aggregate.case.model_copy(update={"status": CaseStatus.PARTIALLY_RESOLVED})})
    with pytest.raises(ApplicationPortError) as caught:
        read_published_report(aggregate, resources)
    assert caught.value.error.code is ErrorCode.STATE_CORRUPT


@pytest.mark.parametrize("content", [b"\xff", b"# Another report\n"])
def test_generic_file_must_be_utf8_and_match_the_frozen_markdown(content):
    aggregate, resources, _ = generic_report()
    artifact = _artifact(content, kind=ArtifactKind.GENERIC_REPORT, name="report.md", content_type="text/markdown")
    generic = aggregate.case.generic_result_v2.model_copy(update={"report_utf8_size": len(content),
        "report_sha256": hashlib.sha256(content).hexdigest()})
    case = aggregate.case.model_copy(update={"generic_result_v2": generic})
    with pytest.raises(ApplicationPortError) as caught:
        read_published_report(_aggregate(case, artifact), BytesStore(content))
    assert caught.value.error.code is ErrorCode.STATE_CORRUPT
