"""Read an already published report from one authoritative Case snapshot."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from problem_locator.contracts import (
    ApplicationPortError, ArtifactKind, ArtifactSummary, Case, CaseAggregate,
    CaseStatus, ErrorCode, GenericResult, ResourceKind, ResourceRef,
    UserResultPayloadV3, parse_canonical_json_bytes,
)
from problem_locator.contracts.ports import ResourceStore

from .errors import raise_port_error
from .projection import project_artifact_summary

MAX_REPORT_BYTES = 16 * 1024 * 1024
_RESULT_STATUSES = {CaseStatus.RESOLVED, CaseStatus.PARTIALLY_RESOLVED, CaseStatus.UNRESOLVED}


@dataclass(frozen=True, slots=True)
class PublishedReport:
    case: Case
    format: str | None = None
    report: UserResultPayloadV3 | GenericResult | None = None
    markdown: str | None = None
    artifact: ArtifactSummary | None = None
    source_job_id: str | None = None


def _read_bytes(resource_store: ResourceStore, artifact) -> bytes:
    if artifact.size > MAX_REPORT_BYTES:
        raise_port_error(ErrorCode.RESOURCE_LIMIT_EXCEEDED, "报告超过 16 MiB 读取上限。")
    resource = ResourceRef(resource_kind=artifact.resource_kind, storage_key=artifact.storage_key,
        size=artifact.size, sha256=artifact.sha256)
    try:
        stream = resource_store.open_read(resource)
    except ApplicationPortError as error:
        if error.error.code is ErrorCode.PATH_VIOLATION:
            raise_port_error(ErrorCode.RESOURCE_NOT_FOUND, "报告文件暂时无法读取。")
        raise
    content = bytearray()
    digest = hashlib.sha256()
    try:
        while True:
            requested = min(64 * 1024, artifact.size - len(content) + 1)
            chunk = stream.read(requested)
            if not isinstance(chunk, bytes) or len(chunk) > requested:
                raise_port_error(ErrorCode.RESOURCE_SIZE_MISMATCH, "报告文件大小与记录不一致。")
            if not chunk:
                break
            if len(content) + len(chunk) > artifact.size:
                raise_port_error(ErrorCode.RESOURCE_SIZE_MISMATCH, "报告文件大小与记录不一致。")
            content.extend(chunk)
            digest.update(chunk)
    except (OSError, ValueError):
        raise_port_error(ErrorCode.RESOURCE_NOT_FOUND, "报告文件暂时无法读取。")
    finally:
        stream.close()
    if len(content) != artifact.size:
        raise_port_error(ErrorCode.RESOURCE_SIZE_MISMATCH, "报告文件大小与记录不一致。")
    if digest.hexdigest() != artifact.sha256:
        raise_port_error(ErrorCode.RESOURCE_HASH_MISMATCH, "报告文件校验失败。")
    return bytes(content)


def read_published_report(aggregate: CaseAggregate, resource_store: ResourceStore) -> PublishedReport:
    """No model, evidence replay, archive IO, or second repository read."""
    case = aggregate.case
    if case.status not in _RESULT_STATUSES:
        return PublishedReport(case)
    generic = case.generic_result_v2 or case.generic_result
    if generic is not None:
        source_job = generic.source_job_id
        if case.generic_result_v2 is None:
            return PublishedReport(case, format="generic-v1", report=generic, source_job_id=source_job)
        expected_kind = ArtifactKind.GENERIC_REPORT
        expected_id = generic.report_artifact_id
    elif case.status is CaseStatus.UNRESOLVED and case.unresolved_result is not None:
        source_job = case.unresolved_result.source_job_id
        expected_kind = ArtifactKind.USER_RESULT
        expected_id = case.unresolved_result.user_result_artifact_id
    elif case.final_result is not None:
        source_job = case.final_result.proposed_by_job_id
        expected_kind, expected_id = ArtifactKind.USER_RESULT, None
    else:
        raise_port_error(ErrorCode.STATE_CORRUPT, "任务结果缺少正式报告记录。")

    candidates = []
    for artifact in aggregate.artifacts.values():
        if artifact.kind is not expected_kind:
            continue
        if artifact.case_id != case.case_id:
            raise_port_error(ErrorCode.STATE_CORRUPT, "报告归属与任务记录不一致。")
        try:
            summary = project_artifact_summary(case, artifact)
        except (ValueError, TypeError):
            raise_port_error(ErrorCode.STATE_CORRUPT, "报告元数据与任务记录不一致。")
        if summary.downloadable:
            candidates.append((artifact, summary))
    if len(candidates) != 1:
        raise_port_error(ErrorCode.STATE_CORRUPT, "任务缺少唯一的正式报告。")
    artifact, summary = candidates[0]
    expected_type = "application/json" if expected_kind is ArtifactKind.USER_RESULT else "text/markdown"
    if (artifact.case_id != case.case_id or artifact.created_by_job_id != source_job
            or artifact.resource_kind is not ResourceKind.FILE or artifact.content_type != expected_type
            or (expected_id is not None and artifact.artifact_id != expected_id)
            or (expected_kind is ArtifactKind.USER_RESULT and artifact.name != "diagnosis-result.json")):
        raise_port_error(ErrorCode.STATE_CORRUPT, "报告归属或类型与任务记录不一致。")
    content = _read_bytes(resource_store, artifact)
    try:
        if expected_kind is ArtifactKind.GENERIC_REPORT:
            markdown = content.decode("utf-8")
            if markdown != generic.report_markdown:
                raise ValueError("published Markdown differs from the frozen result")
            return PublishedReport(case, format="markdown", markdown=markdown,
                artifact=summary, source_job_id=source_job)
        # Reports are server-authored canonical artifacts, not model output.
        # Validate the stored public contract without re-running evidence checks.
        report = parse_canonical_json_bytes(content, UserResultPayloadV3)
        expected_status = {CaseStatus.RESOLVED: "COMPLETED", CaseStatus.PARTIALLY_RESOLVED: "PARTIAL",
            CaseStatus.UNRESOLVED: "INCONCLUSIVE"}[case.status]
        if report.status != expected_status:
            raise ValueError("report status differs from the frozen result")
    except (ValueError, TypeError, RecursionError):
        raise_port_error(ErrorCode.STATE_CORRUPT, "正式报告格式或状态与任务记录不一致。")
    return PublishedReport(case, format="problem-locator-diagnosis-v3", report=report,
        artifact=summary, source_job_id=source_job)
