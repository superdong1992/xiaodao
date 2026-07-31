"""Deterministic, disposable per-Job workspaces."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from problem_locator.contracts.enums import (
    AttachmentStatus,
    ErrorCode,
    ExecutionStage,
    ResourceKind,
)
from problem_locator.contracts.models import (
    Artifact,
    Attachment,
    CaseAggregate,
    Evidence,
    Job,
    JobOutcome,
    LogparseParseClaim,
    ResourceRef,
    TreeManifest,
    TreeManifestEntry,
    WorkspaceArtifactInput,
    WorkspaceAttachmentInput,
    WorkspaceEvidenceInput,
    WorkspaceInputManifest,
    WorkspacePreviousOutcomeInput,
    validate_workspace_manifest_for_job,
)
from problem_locator.contracts.ports import ResourceStore
from problem_locator.contracts.serialization import (
    bytes_sha256,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)

from .failures import RuntimeExecutionError, runtime_failure


_READ_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class PreparedWorkspace:
    """The immutable input view handed to Context Builder and Backend."""

    root: Path
    manifest: WorkspaceInputManifest
    manifest_bytes: bytes
    attachments: tuple[Attachment, ...]
    evidence: tuple[Evidence, ...]
    artifacts: tuple[Artifact, ...]
    previous_outcomes: tuple[JobOutcome, ...]

    @property
    def context_path(self) -> Path:
        return self.root / "runtime" / "context.txt"

    @property
    def outcome_path(self) -> Path:
        return self.root / "output" / "job_outcome.json"

    @property
    def tool_state_root(self) -> Path:
        return self.root / "runtime" / "tool-state"


def _safe_destination(root: Path, relative_path: str) -> Path:
    """Resolve one fixed relative path without permitting an escape."""

    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.PATH_VIOLATION,
            message="Workspace path validation failed.",
        )
    candidate = root / relative
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.PATH_VIOLATION,
            message="Workspace path validation failed.",
        ) from exc
    return candidate


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.part")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def inspect_file(path: Path) -> tuple[int, str]:
    """Return verified ordinary-file size/hash without following links."""

    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.RESOURCE_NOT_FOUND,
            message="A fixed workspace resource is unavailable.",
        ) from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.PATH_VIOLATION,
            message="Workspace input must be an ordinary unlinked file.",
        )
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_READ_CHUNK_BYTES):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.WORKSPACE_PREPARE_FAILED,
            message="Workspace input could not be read safely.",
            retryable=True,
        ) from exc
    return size, digest.hexdigest()


def inspect_tree(root: Path) -> tuple[int, str, TreeManifest]:
    """Build the frozen TreeManifest for an ordinary, link-free directory."""

    try:
        root_metadata = root.stat(follow_symlinks=False)
    except OSError as exc:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.RESOURCE_NOT_FOUND,
            message="A fixed workspace resource is unavailable.",
        ) from exc
    if root.is_symlink() or not stat.S_ISDIR(root_metadata.st_mode):
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.PATH_VIOLATION,
            message="Workspace directory input is not a safe directory.",
        )
    entries: list[TreeManifestEntry] = []
    try:
        candidates = sorted(
            root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
        )
    except OSError as exc:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.WORKSPACE_PREPARE_FAILED,
            message="Workspace directory could not be enumerated safely.",
            retryable=True,
        ) from exc
    for candidate in candidates:
        relative = candidate.relative_to(root).as_posix()
        try:
            relative.encode("utf-8", errors="strict")
            metadata = candidate.stat(follow_symlinks=False)
        except (OSError, UnicodeEncodeError) as exc:
            raise runtime_failure(
                stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.PATH_VIOLATION,
                message="Workspace directory contains an invalid node.",
            ) from exc
        if candidate.is_symlink():
            raise runtime_failure(
                stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.PATH_VIOLATION,
                message="Workspace directory links are forbidden.",
            )
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise runtime_failure(
                stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.PATH_VIOLATION,
                message="Workspace directory contains a non-ordinary file.",
            )
        size, sha256 = inspect_file(candidate)
        entries.append(TreeManifestEntry(path=relative, size=size, sha256=sha256))
    manifest = TreeManifest(version=1, entries=entries)
    size = sum(entry.size for entry in entries)
    return size, bytes_sha256(canonical_json_bytes(manifest)), manifest


def _verify_materialized(
    resource_store: ResourceStore,
    resource_ref: ResourceRef,
    destination: Path,
) -> None:
    try:
        materialized = resource_store.materialize_read_only(resource_ref, destination)
    except RuntimeExecutionError:
        raise
    except Exception as exc:
        # S00 r1 does not yet freeze a typed ResourceStore failure channel.
        # Fail closed without inspecting implementation-specific exception text.
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.WORKSPACE_PREPARE_FAILED,
            message="A fixed resource could not be materialized.",
            retryable=True,
        ) from exc
    try:
        actual_path = Path(materialized.path).resolve(strict=True)
        expected_path = destination.resolve(strict=True)
    except OSError as exc:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.RESOURCE_NOT_FOUND,
            message="A materialized resource is missing.",
        ) from exc
    if actual_path != expected_path:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.PATH_VIOLATION,
            message="ResourceStore materialized outside the requested path.",
        )
    if resource_ref.resource_kind is ResourceKind.FILE:
        actual_size, actual_sha256 = inspect_file(destination)
    else:
        actual_size, actual_sha256, _ = inspect_tree(destination)
    if actual_size != resource_ref.size:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.RESOURCE_SIZE_MISMATCH,
            message="A fixed resource size does not match its metadata.",
        )
    if actual_sha256 != resource_ref.sha256:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.RESOURCE_HASH_MISMATCH,
            message="A fixed resource hash does not match its metadata.",
        )


def _attachment_ref(attachment: Attachment) -> ResourceRef:
    if (
        attachment.status is not AttachmentStatus.READY
        or attachment.size is None
        or attachment.sha256 is None
        or attachment.storage_key is None
    ):
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.RESOURCE_NOT_FOUND,
            message="A fixed Attachment is not READY.",
        )
    return ResourceRef(
        resource_kind=ResourceKind.FILE,
        storage_key=attachment.storage_key,
        size=attachment.size,
        sha256=attachment.sha256,
    )


def _artifact_ref(artifact: Artifact) -> ResourceRef:
    return ResourceRef(
        resource_kind=artifact.resource_kind,
        storage_key=artifact.storage_key,
        size=artifact.size,
        sha256=artifact.sha256,
    )


def _set_inputs_read_only(inputs_root: Path) -> None:
    try:
        for path in sorted(inputs_root.rglob("*"), reverse=True):
            if path.is_symlink():
                raise ValueError("links are forbidden")
            if path.is_dir():
                path.chmod(0o555)
            else:
                path.chmod(0o444)
        inputs_root.chmod(0o555)
    except (OSError, ValueError) as exc:
        raise runtime_failure(
            stage=ExecutionStage.WORKSPACE_PREPARE,
            code=ErrorCode.WORKSPACE_PREPARE_FAILED,
            message="Workspace inputs could not be made read-only.",
            retryable=True,
        ) from exc


class WorkspaceManager:
    """Create and verify the fixed workspace tree for exactly one Job."""

    def __init__(self, data_root: Path) -> None:
        self._data_root = Path(data_root)

    def prepare(
        self,
        job: Job,
        aggregate: CaseAggregate,
        resource_store: ResourceStore,
    ) -> PreparedWorkspace:
        if aggregate.case.case_id != job.case_id or aggregate.jobs.get(job.job_id) != job:
            raise runtime_failure(
                stage=ExecutionStage.OUTCOME_VALIDATE,
                code=ErrorCode.OUTCOME_INVALID,
                message="The executing Job does not match its stored immutable record.",
            )
        attachments = self._resolve_ordered(
            job.attachment_refs, aggregate.attachments, "Attachment"
        )
        evidence = self._resolve_ordered(job.evidence_refs, aggregate.evidence, "Evidence")
        artifacts = self._resolve_ordered(job.artifact_refs, aggregate.artifacts, "Artifact")
        outcomes = self._resolve_ordered(
            job.previous_outcome_refs, aggregate.outcomes, "previous Outcome"
        )

        root = self._data_root / "tmp" / "workspaces" / job.job_id
        try:
            root.mkdir(parents=True, exist_ok=False)
            (root / "inputs").mkdir()
            (root / "runtime" / "tool-state").mkdir(parents=True)
            (root / "output" / "proposals").mkdir(parents=True)
        except OSError as exc:
            raise runtime_failure(
                stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.WORKSPACE_PREPARE_FAILED,
                message="Job Workspace could not be created safely.",
                retryable=True,
            ) from exc

        entries: list[
            WorkspaceAttachmentInput
            | WorkspaceEvidenceInput
            | WorkspaceArtifactInput
            | WorkspacePreviousOutcomeInput
        ] = []
        for attachment in attachments:
            reference = _attachment_ref(attachment)
            relative = f"inputs/attachments/{attachment.attachment_id}/payload"
            destination = _safe_destination(root, relative)
            _verify_materialized(resource_store, reference, destination)
            entries.append(
                WorkspaceAttachmentInput(
                    input_kind="ATTACHMENT",
                    resource_id=attachment.attachment_id,
                    relative_path=relative,
                    resource_kind=ResourceKind.FILE,
                    size=reference.size,
                    sha256=reference.sha256,
                    content_type=attachment.content_type,
                )
            )
        for item in evidence:
            relative: str | None = None
            resource_kind: ResourceKind | None = None
            size: int | None = None
            sha256: str | None = None
            if item.resource_ref is not None:
                resource_kind = item.resource_ref.resource_kind
                leaf = "payload" if resource_kind is ResourceKind.FILE else "tree"
                relative = f"inputs/evidence/{item.evidence_id}/{leaf}"
                _verify_materialized(
                    resource_store,
                    item.resource_ref,
                    _safe_destination(root, relative),
                )
                size = item.resource_ref.size
                sha256 = item.resource_ref.sha256
            entries.append(
                WorkspaceEvidenceInput(
                    input_kind="EVIDENCE",
                    resource_id=item.evidence_id,
                    relative_path=relative,
                    resource_kind=resource_kind,
                    size=size,
                    sha256=sha256,
                    source_type=item.source_type,
                    source_ref=item.source_ref,
                    locator=item.locator,
                    summary=item.summary,
                    content_hash=item.content_hash,
                )
            )
        for artifact in artifacts:
            reference = _artifact_ref(artifact)
            leaf = "payload" if artifact.resource_kind is ResourceKind.FILE else "tree"
            relative = f"inputs/artifacts/{artifact.artifact_id}/{leaf}"
            _verify_materialized(
                resource_store,
                reference,
                _safe_destination(root, relative),
            )
            entries.append(
                WorkspaceArtifactInput(
                    input_kind="ARTIFACT",
                    resource_id=artifact.artifact_id,
                    relative_path=relative,
                    resource_kind=artifact.resource_kind,
                    size=artifact.size,
                    sha256=artifact.sha256,
                    artifact_kind=artifact.kind,
                    name=artifact.name,
                    content_type=artifact.content_type,
                    metadata=artifact.metadata,
                )
            )
        for outcome in outcomes:
            data = canonical_json_bytes(outcome)
            relative = f"inputs/outcomes/{outcome.outcome_id}/job_outcome.json"
            path = _safe_destination(root, relative)
            try:
                _atomic_write(path, data)
            except OSError as exc:
                raise runtime_failure(
                    stage=ExecutionStage.WORKSPACE_PREPARE,
                    code=ErrorCode.WORKSPACE_PREPARE_FAILED,
                    message="A previous Outcome could not be materialized.",
                    retryable=True,
                ) from exc
            entries.append(
                WorkspacePreviousOutcomeInput(
                    input_kind="PREVIOUS_OUTCOME",
                    resource_id=outcome.outcome_id,
                    relative_path=relative,
                    resource_kind=ResourceKind.FILE,
                    size=len(data),
                    sha256=bytes_sha256(data),
                    source_job_id=outcome.job_id,
                    result_type=outcome.result_type,
                )
            )

        manifest = WorkspaceInputManifest(
            schema_version=1,
            job_id=job.job_id,
            case_id=job.case_id,
            job_type=job.job_type,
            logparse_tool_ref=job.logparse_tool_ref,
            logparse_product=job.logparse_product,
            entries=entries,
        )
        validate_workspace_manifest_for_job(manifest, job)
        manifest_bytes = canonical_json_bytes(manifest)
        try:
            _atomic_write(root / "inputs" / "manifest.json", manifest_bytes)
        except OSError as exc:
            raise runtime_failure(
                stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.WORKSPACE_PREPARE_FAILED,
                message="Workspace manifest could not be published.",
                retryable=True,
            ) from exc
        _set_inputs_read_only(root / "inputs")
        return PreparedWorkspace(
            root=root,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            attachments=tuple(attachments),
            evidence=tuple(evidence),
            artifacts=tuple(artifacts),
            previous_outcomes=tuple(outcomes),
        )

    @staticmethod
    def _resolve_ordered(
        references: list[str], mapping: dict[str, object], label: str
    ) -> list[object]:
        resolved: list[object] = []
        for reference in references:
            value = mapping.get(reference)
            if value is None:
                raise runtime_failure(
                    stage=ExecutionStage.WORKSPACE_PREPARE,
                    code=ErrorCode.RESOURCE_NOT_FOUND,
                    message=f"A fixed {label} is unavailable.",
                )
            resolved.append(value)
        return resolved

    @staticmethod
    def write_context(workspace: PreparedWorkspace, body: str) -> None:
        try:
            _atomic_write(workspace.context_path, body.encode("utf-8"))
        except OSError as exc:
            raise runtime_failure(
                stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.WORKSPACE_PREPARE_FAILED,
                message="Runtime context could not be written.",
                retryable=True,
            ) from exc

    @staticmethod
    def temporary_output_bytes(workspace: PreparedWorkspace) -> int:
        """Count service/Agent-created runtime and output ordinary files."""

        try:
            root_metadata = workspace.root.stat(follow_symlinks=False)
            if workspace.root.is_symlink() or not stat.S_ISDIR(root_metadata.st_mode):
                raise OSError("workspace root is not a directory")
            top_level = {candidate.name: candidate for candidate in workspace.root.iterdir()}
            if set(top_level) != {"inputs", "runtime", "output"}:
                raise OSError("workspace root shape changed")
            for candidate in top_level.values():
                metadata = candidate.stat(follow_symlinks=False)
                if candidate.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                    raise OSError("workspace top-level directory changed")
        except OSError as exc:
            raise runtime_failure(
                stage=ExecutionStage.BACKEND_EXECUTE,
                code=ErrorCode.WORKSPACE_LIMIT,
                message="Workspace output roots could not be measured safely.",
            ) from exc

        total = 0
        for subtree in (workspace.root / "runtime", workspace.root / "output"):
            try:
                candidates = subtree.rglob("*")
                for candidate in candidates:
                    candidate.relative_to(subtree).as_posix().encode(
                        "utf-8", errors="strict"
                    )
                    metadata = candidate.stat(follow_symlinks=False)
                    if candidate.is_symlink() or (
                        not stat.S_ISDIR(metadata.st_mode)
                        and not stat.S_ISREG(metadata.st_mode)
                    ):
                        raise runtime_failure(
                            stage=ExecutionStage.BACKEND_EXECUTE,
                            code=ErrorCode.WORKSPACE_LIMIT,
                            message="Workspace contains an invalid output node.",
                        )
                    if stat.S_ISREG(metadata.st_mode):
                        if metadata.st_nlink != 1:
                            raise runtime_failure(
                                stage=ExecutionStage.BACKEND_EXECUTE,
                                code=ErrorCode.WORKSPACE_LIMIT,
                                message="Workspace contains a linked output file.",
                            )
                        total += metadata.st_size
            except RuntimeExecutionError:
                raise
            except (OSError, UnicodeEncodeError, ValueError) as exc:
                raise runtime_failure(
                    stage=ExecutionStage.BACKEND_EXECUTE,
                    code=ErrorCode.WORKSPACE_LIMIT,
                    message="Workspace output could not be measured safely.",
                ) from exc
        return total

    @staticmethod
    def read_claim(workspace: PreparedWorkspace) -> LogparseParseClaim | None:
        root = workspace.tool_state_root
        try:
            nodes = sorted(root.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise runtime_failure(
                stage=ExecutionStage.TOOL_EXECUTE,
                code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
                message="Logparse tool state could not be inspected.",
            ) from exc
        if not nodes:
            return None
        if len(nodes) != 1 or nodes[0].name != "logparse-parse.claim":
            raise runtime_failure(
                stage=ExecutionStage.TOOL_EXECUTE,
                code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
                message="Logparse tool state contains an unexpected node.",
            )
        claim_path = nodes[0]
        if claim_path.is_symlink():
            raise runtime_failure(
                stage=ExecutionStage.TOOL_EXECUTE,
                code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
                message="Logparse parse claim is not an ordinary file.",
            )
        try:
            return parse_canonical_json_bytes(
                claim_path.read_bytes(), model_type=LogparseParseClaim
            )
        except (OSError, TypeError, ValueError) as exc:
            raise runtime_failure(
                stage=ExecutionStage.TOOL_EXECUTE,
                code=ErrorCode.LOGPARSE_OUTPUT_INVALID,
                message="Logparse parse claim is invalid.",
            ) from exc


__all__ = [
    "PreparedWorkspace",
    "WorkspaceManager",
    "inspect_file",
    "inspect_tree",
]
