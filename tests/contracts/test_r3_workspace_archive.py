from __future__ import annotations

import pytest
from pydantic import ValidationError

from problem_locator.contracts import enums, models
from problem_locator.contracts.enums import AttachmentStatus, ResourceKind
from problem_locator.contracts.models import Attachment, PrepareAttachment, WorkspaceAttachmentInput


ATTACHMENT_ID = "00000000-0000-0000-0000-000000000050"
CASE_ID = "00000000-0000-0000-0000-000000000001"
NOW = "2026-07-31T00:00:00.000Z"


def test_attachment_filename_suffix_is_a_closed_public_enum() -> None:
    suffix_type = getattr(enums, "AttachmentFilenameSuffix")
    assert tuple(item.value for item in suffix_type) == (
        ".gz",
        ".zip",
        ".tar.gz",
        ".tgz",
        ".tar",
    )
    assert "AttachmentFilenameSuffix" in enums.__all__


@pytest.mark.parametrize(
    ("name", "content_type", "expected"),
    [
        ("logs.gz", "application/gzip", ".gz"),
        ("logs.tar.gz", "application/gzip", ".tar.gz"),
        ("logs.tgz", "application/gzip", ".tgz"),
        ("logs.zip", "application/zip", ".zip"),
        ("logs.tar", "application/x-tar", ".tar"),
        ("notes.txt", "text/plain", None),
    ],
)
def test_suffix_derivation_uses_the_frozen_matrix_and_longest_terminal_match(
    name: str,
    content_type: str,
    expected: str | None,
) -> None:
    derive = getattr(models, "derive_attachment_filename_suffix")
    result = derive(name, content_type)
    assert (None if result is None else result.value) == expected


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("logs.TAR.GZ", "application/gzip"),
        ("logs.GZ", "application/gzip"),
        ("logs.zip ", "application/zip"),
        ("logs", "application/zip"),
        ("logs.gzip", "application/gzip"),
        ("logs.zip", "application/gzip"),
        ("logs.gz", "application/zip"),
        ("report.zip", "text/plain"),
        ("../logs.zip", "application/zip"),
        (r"folder\logs.zip", "application/zip"),
        ("C:logs.zip", "application/zip"),
        (".", "text/plain"),
        ("..", "text/plain"),
        ("logs\n.zip", "application/zip"),
        ("logs\x7f.zip", "application/zip"),
        ("logs\x85.zip", "application/zip"),
        ("logs.zip", "Application/Zip"),
    ],
)
def test_suffix_derivation_rejects_unsafe_noncanonical_or_mismatched_inputs(
    name: str,
    content_type: str,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        models.derive_attachment_filename_suffix(name, content_type)


def test_suffix_derivation_reuses_the_existing_attachment_name_byte_limit() -> None:
    with pytest.raises(ValueError, match="65536"):
        models.derive_attachment_filename_suffix(
            "x" * 65_537 + ".zip",
            "application/zip",
        )


def test_prepare_and_persisted_attachment_reject_archive_name_type_drift_early() -> None:
    prepared = PrepareAttachment(
        idempotency_key="prepare-attachment-r3",
        case_id=CASE_ID,
        expected_case_revision=1,
        name="logs.tar.gz",
        content_type="application/gzip",
        declared_size=None,
        declared_sha256=None,
    )
    assert prepared.name == "logs.tar.gz"

    persisted = Attachment(
        attachment_id=ATTACHMENT_ID,
        case_id=CASE_ID,
        status=AttachmentStatus.UPLOADING,
        name="logs.tar.gz",
        content_type="application/gzip",
        declared_size=None,
        declared_sha256=None,
        size=None,
        sha256=None,
        storage_key=None,
        created_at=NOW,
        updated_at=NOW,
    )
    assert persisted.content_type == "application/gzip"

    for model_type, payload in (
        (
            PrepareAttachment,
            prepared.model_dump(mode="python") | {"name": "logs.zip"},
        ),
        (
            Attachment,
            persisted.model_dump(mode="python") | {"name": "logs.zip"},
        ),
    ):
        with pytest.raises(ValidationError, match="suffix|content_type"):
            model_type.model_validate(payload)


def test_workspace_attachment_path_helper_is_the_only_path_constructor() -> None:
    suffix_type = enums.AttachmentFilenameSuffix
    helper = getattr(models, "workspace_attachment_relative_path")
    assert helper(ATTACHMENT_ID, None) == (
        f"inputs/attachments/{ATTACHMENT_ID}/payload"
    )
    assert helper(ATTACHMENT_ID, suffix_type.TAR_GZ) == (
        f"inputs/attachments/{ATTACHMENT_ID}/payload.tar.gz"
    )
    with pytest.raises(ValueError):
        helper("../outside", suffix_type.ZIP)
    with pytest.raises((TypeError, ValueError)):
        helper(ATTACHMENT_ID, ".GZ")


def _workspace_payload() -> dict[str, object]:
    return {
        "input_kind": "ATTACHMENT",
        "resource_id": ATTACHMENT_ID,
        "relative_path": f"inputs/attachments/{ATTACHMENT_ID}/payload.tar.gz",
        "resource_kind": ResourceKind.FILE,
        "size": 128,
        "sha256": "2" * 64,
        "content_type": "application/gzip",
        "filename_suffix": ".tar.gz",
    }


def test_workspace_attachment_suffix_is_required_nullable_and_path_bound() -> None:
    value = WorkspaceAttachmentInput.model_validate(_workspace_payload())
    assert value.filename_suffix is enums.AttachmentFilenameSuffix.TAR_GZ

    schema = WorkspaceAttachmentInput.model_json_schema()
    assert "filename_suffix" in schema["required"]
    suffix_schema = schema["properties"]["filename_suffix"]
    assert "default" not in suffix_schema

    missing = _workspace_payload()
    missing.pop("filename_suffix")
    with pytest.raises(ValidationError):
        WorkspaceAttachmentInput.model_validate(missing)

    nonarchive = _workspace_payload() | {
        "content_type": "text/plain",
        "filename_suffix": None,
        "relative_path": f"inputs/attachments/{ATTACHMENT_ID}/payload",
    }
    assert WorkspaceAttachmentInput.model_validate(nonarchive).filename_suffix is None


@pytest.mark.parametrize(
    "changes",
    [
        {"filename_suffix": ".zip"},
        {"filename_suffix": None},
        {"filename_suffix": ".GZ"},
        {"filename_suffix": "../payload"},
        {"filename_suffix": ".tar.gz-extra"},
        {"relative_path": f"inputs/attachments/{ATTACHMENT_ID}/payload.gz"},
        {"relative_path": f"inputs/attachments/{ATTACHMENT_ID}/payload"},
        {"content_type": "application/zip"},
    ],
)
def test_workspace_attachment_rejects_suffix_mime_or_path_drift(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        WorkspaceAttachmentInput.model_validate(_workspace_payload() | changes)
