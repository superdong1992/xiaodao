"""Protocol-only projections over frozen S00 response objects."""

from __future__ import annotations

from urllib.parse import quote, urlsplit, urlunsplit

from problem_locator.contracts.commands import (
    ApplicationResponse,
    ArtifactSummary,
    ArtifactView,
    UploadDescriptor,
)
from problem_locator.contracts.limits import MAX_ATTACHMENT_BYTES
from problem_locator.contracts.models import ContentType
from pydantic import TypeAdapter


_CONTENT_TYPE = TypeAdapter(ContentType)


def append_public_path(public_base_url: str, path: str, *, query: str = "") -> str:
    """Append an absolute service path without treating the base as a file path."""

    parsed = urlsplit(public_base_url)
    base_path = parsed.path.rstrip("/")
    suffix = path if path.startswith("/") else f"/{path}"
    return urlunsplit((parsed.scheme, parsed.netloc, f"{base_path}{suffix}", query, ""))


def upload_descriptor(
    response: ApplicationResponse,
    *,
    public_base_url: str,
    content_type: str,
    declared_size: int | None,
    declared_sha256: str | None,
) -> UploadDescriptor:
    """Project a successful prepare receipt into S00's UploadDescriptor."""

    attachment_id = response.business_receipt.primary_resource_id
    canonical_content_type = _CONTENT_TYPE.validate_python(content_type)
    return UploadDescriptor(
        attachment_id=attachment_id,
        method="PUT",
        url=append_public_path(
            public_base_url,
            f"/api/v1/attachments/{quote(attachment_id, safe='')}/content",
        ),
        required_headers={
            "Idempotency-Key": attachment_id,
            "Content-Type": canonical_content_type,
            "Content-Length": None if declared_size is None else str(declared_size),
            "X-Content-SHA256": declared_sha256,
        },
        max_bytes=MAX_ATTACHMENT_BYTES,
        expires_at=None,
    )


def artifact_view(
    summary: ArtifactSummary,
    *,
    case_id: str,
    public_base_url: str,
) -> ArtifactView:
    """Add only the public download URL to a downloadable ArtifactSummary."""

    if not summary.downloadable:
        raise ValueError("only downloadable artifacts may be projected")
    query = f"case_id={quote(case_id, safe='')}"
    return ArtifactView(
        artifact_id=summary.artifact_id,
        name=summary.name,
        content_type=summary.content_type,
        size=summary.size,
        sha256=summary.sha256,
        created_at=summary.created_at,
        download_url=append_public_path(
            public_base_url,
            f"/api/v1/artifacts/{quote(summary.artifact_id, safe='')}/content",
            query=query,
        ),
    )


__all__ = ["append_public_path", "artifact_view", "upload_descriptor"]
