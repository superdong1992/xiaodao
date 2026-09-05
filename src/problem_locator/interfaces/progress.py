"""Compact MCP responses; full Case details remain an explicit query."""
from problem_locator.contracts.models import (
    ContractModel, OpaqueId, PositiveInt, CaseStatus, ArchiveStatus, JobSummary,
    PendingRequirement, AttachmentSummary, ArtifactSummary, CaseFailure,
    BusinessReceipt, ArtifactView, CaseView,
)


class CaseProgress(ContractModel):
    case_id: OpaqueId
    case_revision: PositiveInt
    status: CaseStatus
    active_job: JobSummary | None
    archive_status: ArchiveStatus
    pending_requirements: list[PendingRequirement]
    attachments: list[AttachmentSummary]
    artifacts: list[ArtifactSummary]
    failure: CaseFailure | None

    @classmethod
    def from_view(cls, view: CaseView):
        value = {name: getattr(view, name) for name in cls.model_fields}
        value['pending_requirements'] = [item for item in view.pending_requirements if item.status.value == 'OPEN']
        return cls.model_validate(value)


class McpApplicationResponse(ContractModel):
    business_receipt: BusinessReceipt
    case_view: CaseProgress | None
    wait_timed_out: bool
    dispatch_pending: bool
    artifact_views: list[ArtifactView]


class McpCaseQueryResponse(ContractModel):
    case_view: CaseProgress | CaseView
    wait_timed_out: bool
