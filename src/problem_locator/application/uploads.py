"""Single-read Attachment upload orchestration for the V1 application service.

The long-lived per-Attachment guard covers early validation, the one forward
streaming pass, and the complete post-stage decision.  The global publication
lease is deliberately acquired only after streaming has finished and is held
through immutable publication and the matching state commit.
"""

from __future__ import annotations

from dataclasses import dataclass

from problem_locator.contracts import (
    ApplicationError,
    ApplicationPortError,
    Attachment,
    AttachmentStagedRef,
    AttachmentStatus,
    AttachmentUploadGuard,
    BusinessReceipt,
    Case,
    CaseAggregate,
    CaseStatus,
    Clock,
    ERROR_SPECS,
    ErrorCode,
    PublicationCommitGuard,
    ResourceKind,
    ResourceRef,
    ResourceStore,
    ResourceType,
    StateChangeNotifier,
    StateFile,
    StateRepository,
    UploadAttachmentContent,
)
from problem_locator.journey import record_journey_event

from .idempotency import (
    IdempotencyDisposition,
    decide_idempotency,
    make_idempotency_record,
)
from .mutations import build_state_mutation
from .preparation import finalize_attachment


_MAX_POST_STAGE_ATTEMPTS = 3
_TERMINAL_CASE_STATUSES = frozenset(
    {
        CaseStatus.RESOLVED,
        CaseStatus.UNRESOLVED,
        CaseStatus.FAILED,
        CaseStatus.CANCELLED,
    }
)


def _port_error(code: ErrorCode, message: str) -> ApplicationPortError:
    return ApplicationPortError(
        ApplicationError(
            code=code,
            message=message,
            details=[],
            retryable=ERROR_SPECS[code].application_retryable,
        )
    )


def _attachment(snapshot: StateFile, attachment_id: str) -> tuple[CaseAggregate, Attachment]:
    matches = [
        (aggregate, aggregate.attachments[attachment_id])
        for aggregate in snapshot.cases.values()
        if attachment_id in aggregate.attachments
    ]
    if not matches:
        raise _port_error(
            ErrorCode.ATTACHMENT_NOT_FOUND,
            "The requested Attachment does not exist.",
        )
    if len(matches) != 1:
        # StateFile validation makes this unreachable for a conforming
        # repository, but never choose an arbitrary owner on corrupt state.
        raise _port_error(
            ErrorCode.STATE_WRITE_FAILED,
            "The Attachment owner could not be determined safely.",
        )
    return matches[0]


def _validate_headers(
    command: UploadAttachmentContent,
    attachment: Attachment,
) -> None:
    """Validate every prepared/header binding before the stream is touched."""

    if command.expected_content_type != attachment.content_type:
        raise _port_error(
            ErrorCode.VALIDATION_ERROR,
            "The upload Content-Type does not match the prepared Attachment.",
        )
    if (
        attachment.declared_size is not None
        and command.expected_size != attachment.declared_size
    ):
        raise _port_error(
            ErrorCode.VALIDATION_ERROR,
            "The upload Content-Length does not match the prepared Attachment.",
        )
    if (
        attachment.declared_sha256 is not None
        and command.expected_sha256 != attachment.declared_sha256
    ):
        raise _port_error(
            ErrorCode.VALIDATION_ERROR,
            "The upload digest does not match the prepared Attachment.",
        )


def _matching_ready_attachment(
    command: UploadAttachmentContent,
    attachment: Attachment,
) -> bool:
    return (
        attachment.status is AttachmentStatus.READY
        and attachment.size == command.expected_size
        and attachment.sha256 == command.expected_sha256
    )


def _ready_receipt(aggregate: CaseAggregate, attachment: Attachment) -> BusinessReceipt:
    """Recover the only receipt fields derivable from an already READY object."""

    return BusinessReceipt(
        operation="UploadAttachmentContent",
        primary_resource_id=attachment.attachment_id,
        case_id=attachment.case_id,
        case_revision=aggregate.case.case_revision,
        job_id=None,
        status=AttachmentStatus.READY.value,
    )


def _validate_uploadable(
    command: UploadAttachmentContent,
    aggregate: CaseAggregate,
    attachment: Attachment,
) -> BusinessReceipt | None:
    _validate_headers(command, attachment)
    if attachment.status is AttachmentStatus.READY:
        if _matching_ready_attachment(command, attachment):
            return _ready_receipt(aggregate, attachment)
        raise _port_error(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "The Attachment is already bound to different bytes.",
        )
    if attachment.status is not AttachmentStatus.UPLOADING:
        raise _port_error(
            ErrorCode.INVALID_CASE_STATE,
            "The Attachment is not accepting upload content.",
        )
    if aggregate.case.status in _TERMINAL_CASE_STATUSES:
        raise _port_error(
            ErrorCode.INVALID_CASE_STATE,
            "The Case no longer accepts Attachment uploads.",
        )
    return None


def _updated_case(current: Case, *, occurred_at: str) -> Case:
    payload = current.model_dump(mode="python")
    payload.update(
        case_revision=current.case_revision + 1,
        updated_at=occurred_at,
    )
    return Case.model_validate(payload)


def _validate_published_resource(
    resource: ResourceRef,
    *,
    expected_storage_key: str,
    expected_size: int,
    expected_sha256: str,
) -> None:
    if (
        resource.resource_kind is not ResourceKind.FILE
        or resource.storage_key != expected_storage_key
        or resource.size != expected_size
        or resource.sha256 != expected_sha256
    ):
        raise _port_error(
            ErrorCode.RESOURCE_PUBLISH_FAILED,
            "The published Attachment receipt is inconsistent.",
        )


def _translate_capacity_conflict(error: ApplicationPortError) -> None:
    if error.error.code is ErrorCode.RESOURCE_HASH_MISMATCH:
        raise _port_error(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "The Attachment target is already bound to different bytes.",
        ) from error
    if error.error.code in {ErrorCode.RESOURCE_NOT_FOUND, ErrorCode.PATH_VIOLATION}:
        raise _port_error(
            ErrorCode.RESOURCE_PUBLISH_FAILED,
            "The Attachment resource could not be published.",
        ) from error
    raise error


def _translate_publish_failure(error: ApplicationPortError) -> None:
    if error.error.code in {ErrorCode.RESOURCE_NOT_FOUND, ErrorCode.PATH_VIOLATION}:
        raise _port_error(
            ErrorCode.RESOURCE_PUBLISH_FAILED,
            "The Attachment resource could not be published.",
        ) from error
    # A target mismatch cannot first appear here: capacity and publish share
    # one coordination lease.  RESOURCE_HASH_MISMATCH at this point therefore
    # reports staged or published byte drift and retains its frozen meaning.
    raise error


def _discard_best_effort(
    resource_store: ResourceStore,
    staged_ref: AttachmentStagedRef,
) -> None:
    """Release a completed stage without replacing its primary disposition."""

    try:
        resource_store.discard(staged_ref)
    except Exception:
        # A completed stage is recoverable cleanup state.  Its deletion failure
        # must not turn a durable/replayed receipt into a false negative or
        # replace the typed error already selected by the upload pipeline.
        pass


@dataclass(frozen=True, slots=True)
class AttachmentUploadService:
    """Composable command handler for exactly ``UploadAttachmentContent``."""

    repository: StateRepository
    resource_store: ResourceStore
    publication_guard: PublicationCommitGuard
    upload_guard: AttachmentUploadGuard
    clock: Clock
    notifier: StateChangeNotifier

    def execute(self, command: UploadAttachmentContent) -> BusinessReceipt:
        if not isinstance(command, UploadAttachmentContent):
            raise TypeError("AttachmentUploadService accepts UploadAttachmentContent")

        staged_ref = None
        upload_lease = None
        try:
            upload_lease = self.upload_guard.acquire(command.attachment_id)
            initial = self.repository.read_snapshot()
            initial_idempotency = decide_idempotency(initial, command)
            if initial_idempotency.disposition is IdempotencyDisposition.REPLAY:
                assert initial_idempotency.record is not None
                return initial_idempotency.record.business_receipt
            if initial_idempotency.disposition is IdempotencyDisposition.CONFLICT:
                raise _port_error(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "The idempotency key is already bound to another request.",
                )

            aggregate, attachment = _attachment(initial, command.attachment_id)
            ready_receipt = _validate_uploadable(command, aggregate, attachment)
            if ready_receipt is not None:
                return ready_receipt

            # This is the only operation allowed to consume byte_stream.  It
            # runs while the per-Attachment lease is active and before any
            # PublicationCommitLease is acquired.
            try:
                staged_ref = self.resource_store.stage_attachment(
                    command.attachment_id,
                    upload_lease,
                    command.byte_stream,
                    expected_size=command.expected_size,
                    expected_sha256=command.expected_sha256,
                )
            except ApplicationPortError:
                raise
            except Exception as error:
                # BinaryStream transport failures are normalized at the
                # upload boundary; exception text is never copied into the
                # public error payload.
                raise _port_error(
                    ErrorCode.UPLOAD_INCOMPLETE,
                    "The Attachment upload did not complete.",
                ) from error
            if staged_ref.attachment_id != command.attachment_id:
                raise _port_error(
                    ErrorCode.UPLOAD_INCOMPLETE,
                    "The staged Attachment receipt has the wrong identity.",
                )
            if staged_ref.size != command.expected_size:
                raise _port_error(
                    ErrorCode.RESOURCE_SIZE_MISMATCH,
                    "The staged Attachment size does not match Content-Length.",
                )
            if staged_ref.sha256 != command.expected_sha256:
                raise _port_error(
                    ErrorCode.RESOURCE_HASH_MISMATCH,
                    "The staged Attachment digest does not match the upload header.",
                )

            occurred_at = self.clock.now()
            committed_generation: int | None = None
            committed_case_id: str | None = None
            committed_receipt: BusinessReceipt | None = None
            committed_attachment: Attachment | None = None
            committed_resource_ref: ResourceRef | None = None
            committed_previous_case: Case | None = None
            committed_case: Case | None = None

            for attempt in range(_MAX_POST_STAGE_ATTEMPTS):
                publication_lease = self.publication_guard.acquire()
                retry_revision_conflict = False
                post_stage_replay: BusinessReceipt | None = None
                try:
                    fresh = self.repository.read_snapshot()
                    idempotency = decide_idempotency(fresh, command)
                    if idempotency.disposition is IdempotencyDisposition.REPLAY:
                        assert idempotency.record is not None
                        post_stage_replay = idempotency.record.business_receipt
                    elif idempotency.disposition is IdempotencyDisposition.CONFLICT:
                        raise _port_error(
                            ErrorCode.IDEMPOTENCY_CONFLICT,
                            "The idempotency key is already bound to another request.",
                        )
                    else:
                        fresh_aggregate, fresh_attachment = _attachment(
                            fresh, command.attachment_id
                        )
                        ready_receipt = _validate_uploadable(
                            command,
                            fresh_aggregate,
                            fresh_attachment,
                        )
                        if ready_receipt is not None:
                            post_stage_replay = ready_receipt
                        else:
                            target = self.resource_store.plan_target(
                                fresh_attachment.case_id,
                                ResourceType.ATTACHMENT,
                                fresh_attachment.attachment_id,
                                ResourceKind.FILE,
                                staged_ref.size,
                                staged_ref.sha256,
                            )
                            try:
                                self.resource_store.validate_case_capacity(
                                    fresh_attachment.case_id,
                                    [target],
                                )
                            except ApplicationPortError as error:
                                _translate_capacity_conflict(error)
                            try:
                                resource_ref = self.resource_store.publish(
                                    staged_ref,
                                    target.final_storage_key,
                                )
                            except ApplicationPortError as error:
                                _translate_publish_failure(error)
                            _validate_published_resource(
                                resource_ref,
                                expected_storage_key=target.final_storage_key,
                                expected_size=staged_ref.size,
                                expected_sha256=staged_ref.sha256,
                            )

                            updated_attachment = finalize_attachment(
                                fresh_attachment,
                                resource_ref,
                                occurred_at=occurred_at,
                            )
                            updated_case = _updated_case(
                                fresh_aggregate.case,
                                occurred_at=occurred_at,
                            )
                            receipt = BusinessReceipt(
                                operation="UploadAttachmentContent",
                                primary_resource_id=fresh_attachment.attachment_id,
                                case_id=fresh_attachment.case_id,
                                case_revision=updated_case.case_revision,
                                job_id=None,
                                status=AttachmentStatus.READY.value,
                            )
                            record = make_idempotency_record(
                                command,
                                idempotency.request_hash,
                                receipt,
                                case_id=fresh_attachment.case_id,
                                created_at=occurred_at,
                            )
                            mutation = build_state_mutation(
                                upsert_case=updated_case,
                                upsert_attachments=[updated_attachment],
                                insert_idempotency_records=[record],
                            )
                            try:
                                commit = self.repository.commit(
                                    fresh.generation,
                                    fresh_aggregate.case.case_revision,
                                    mutation,
                                )
                            except ApplicationPortError as error:
                                if (
                                    error.error.code is ErrorCode.REVISION_CONFLICT
                                    and attempt + 1 < _MAX_POST_STAGE_ATTEMPTS
                                ):
                                    retry_revision_conflict = True
                                else:
                                    raise
                            else:
                                committed_generation = commit.generation
                                committed_case_id = fresh_attachment.case_id
                                committed_receipt = receipt
                                committed_attachment = updated_attachment
                                committed_resource_ref = resource_ref
                                committed_previous_case = fresh_aggregate.case
                                committed_case = updated_case
                finally:
                    publication_lease.release()

                if post_stage_replay is not None:
                    _discard_best_effort(self.resource_store, staged_ref)
                    staged_ref = None
                    return post_stage_replay
                if committed_generation is not None:
                    break
                if retry_revision_conflict:
                    continue
                raise AssertionError("post-stage upload attempt produced no disposition")
            else:
                raise AssertionError("post-stage retry loop terminated unexpectedly")

            assert committed_case_id is not None
            assert committed_generation is not None
            assert committed_receipt is not None
            assert committed_attachment is not None
            assert committed_resource_ref is not None
            assert committed_previous_case is not None
            assert committed_case is not None
            record_journey_event(
                "attachment.uploaded",
                timestamp=occurred_at,
                request_id=command.idempotency_key,
                case_id=committed_case_id,
                data={
                    "operation": committed_receipt.operation,
                    "attachment": committed_attachment,
                    "resource_ref": committed_resource_ref,
                    "actual_size": staged_ref.size,
                    "actual_sha256": staged_ref.sha256,
                    "from_case_revision": committed_previous_case.case_revision,
                    "to_case_revision": committed_case.case_revision,
                    "generation": committed_generation,
                },
            )
            # Notification is a best-effort hint and always occurs outside the
            # publication lease.  Its failure cannot roll back the committed
            # business state.
            try:
                self.notifier.notify(committed_case_id, committed_generation)
            except Exception:
                pass

            # The exact immutable receipt was persisted in the successful
            # mutation.  A best-effort projection read belongs to the facade;
            # no post-commit storage failure may replace this success result.
            staged_ref = None
            return committed_receipt
        except BaseException:
            if staged_ref is not None:
                _discard_best_effort(self.resource_store, staged_ref)
            raise
        finally:
            try:
                command.byte_stream.close()
            except Exception:
                # Closing the caller-owned transport is best-effort cleanup.
                # It must not replace either a durable business receipt or the
                # modeled failure already selected by the upload pipeline.
                pass
            finally:
                if upload_lease is not None:
                    upload_lease.release()


__all__ = ["AttachmentUploadService"]
