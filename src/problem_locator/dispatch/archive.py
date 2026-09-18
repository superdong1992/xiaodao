"""Durable background ZIP delivery, independent of diagnostic success."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import PurePosixPath

from problem_locator.application.mutations import build_state_mutation
from problem_locator.contracts import ApplicationPortError, Artifact, ArtifactKind, ErrorCode, ResourceKind, ResourceType, UserResultArchiveMetadataV3
from problem_locator.contracts.models import ArchivePlan
from problem_locator.diagnostics import log_event
from problem_locator.integrations.result_archive import write_result_archive_file
from problem_locator.journey import record_journey_event
from problem_locator.operational import OperationalState
from problem_locator.storage.paths import ensure_no_symlink_ancestors


class ArchiveService:
    def __init__(self, repository, resource_store, publication_guard, notifier, clock, *, workers=1,
                 operational_state: OperationalState | None = None):
        if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
            raise ValueError("archive workers must be positive")
        self._repository = repository
        self._resources = resource_store
        self._guard = publication_guard
        self._notifier = notifier
        self._clock = clock
        self.operational_state = operational_state or OperationalState(clock.now)
        self._workers = workers
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._activity = threading.RLock()
        self._active_cases: set[str] = set()
        self._deleted_cases: set[str] = set()

    def cancel_cases(self, case_ids) -> None:
        """Cancel only explicitly deleted Cases; ordinary shutdown stays resumable."""
        selected = tuple(case_ids)
        with self._activity:
            self._repository.cancel_archive_tasks(selected)
            self._deleted_cases.update(selected)

    def cases_idle(self, case_ids) -> bool:
        with self._activity:
            return not self._active_cases.intersection(case_ids)

    def _deleted(self, case_id, *, authoritative=False) -> bool:
        with self._activity:
            return case_id in self._deleted_cases or (
                authoritative and self._repository.is_agent_case_deleted(case_id))

    def start(self):
        if self._threads:
            return
        for index in range(self._workers):
            thread = threading.Thread(target=self._run, name=f"problem-locator-archive-{index}", daemon=True)
            self._threads.append(thread)
            thread.start()

    def shutdown(self, timeout_seconds):
        self._stop.set()
        deadline = time.monotonic() + timeout_seconds
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))
        return all(not thread.is_alive() for thread in self._threads)

    def _run(self):
        while not self._stop.is_set():
            try:
                processed = self.run_once()
            except Exception as error:
                log_event("archive.worker.failed", level=logging.ERROR, error=error)
                processed = False
            if not processed:
                self._stop.wait(0.5)

    def _set_status(self, case_id, status, artifact=None):
        with self._guard.acquire(case_id), self._repository.archive_publication(case_id):
            state = self._repository.read_snapshot(case_id)
            case = state.cases[case_id].case
            updated = case.model_copy(update={"archive_status": status,
                "case_revision": case.case_revision + 1, "updated_at": self._clock.now()})
            commit = self._repository.commit(state.generation, case.case_revision,
                build_state_mutation(upsert_case=updated, insert_artifacts=[] if artifact is None else [artifact]))
        try:
            self._notifier.notify(case_id, commit.generation)
        except Exception:
            # Delivery hints cannot revoke an already durable archive.
            pass

    def run_once(self) -> bool:
        with self._activity:
            task = self._repository.claim_archive_task()
            if task is None:
                return False
            case_id, payload = task
            self._active_cases.add(case_id)
        started = time.perf_counter()
        staged = None
        published = False
        try:
            if self._deleted(case_id, authoritative=True):
                raise InterruptedError("archive belongs to a deleted conversation")
            plan = ArchivePlan.model_validate(payload["plan"])
            paths = []
            for key in payload["source_storage_keys"]:
                relative = PurePosixPath(key)
                if relative.parts[:4] != ("resources", "cases", case_id, "artifacts") or ".." in relative.parts:
                    raise ValueError("archive source escapes the owning Case")
                path = self._resources.layout.data_root / key
                ensure_no_symlink_ancestors(self._resources.layout.resources, path)
                paths.append(path)
            staged = self._resources.stage_archive(payload["source_job_id"], lambda path:
                write_result_archive_file(path, plan=plan, source_paths=paths,
                    cancelled=lambda: self._stop.is_set() or self._deleted(case_id)))
            artifact_id = str(uuid.uuid5(uuid.UUID(payload["report_artifact_id"]), "result-archive-v10"))
            # Deletion and final publication share this short barrier. Long
            # compression stays outside it and observes cancellation per chunk.
            with self._activity, self._guard.acquire(case_id), self._repository.archive_publication(case_id):
                if self._deleted(case_id, authoritative=True):
                    raise InterruptedError("archive belongs to a deleted conversation")
                aggregate = self._repository.read_case(case_id)
                self._resources.seed_case_resources(aggregate)
                target = self._resources.plan_target(case_id, ResourceType.ARTIFACT, artifact_id,
                    ResourceKind.FILE, staged.size, staged.sha256)
                self._resources.validate_case_capacity(case_id, [target])
                ref = self._resources.publish(staged, target.final_storage_key)
                artifact = Artifact(artifact_id=artifact_id, case_id=case_id, kind=ArtifactKind.USER_RESULT_ARCHIVE,
                    name="result.zip", content_type="application/zip", resource_kind=ResourceKind.FILE,
                    size=ref.size, sha256=ref.sha256, storage_key=ref.storage_key,
                    metadata=UserResultArchiveMetadataV3(schema_version=3, format_id="problem-locator-result-archive-v3",
                        description="后台生成的诊断归档。", user_result_proposal_key="server-user-result", target_log_count=len(plan.logs)),
                    created_by_job_id=payload["source_job_id"], created_at=self._clock.now())
                self._set_status(case_id, "READY", artifact)
                published = True
            record_journey_event("case.archive.ready", case_id=case_id,
                duration_ms=(time.perf_counter() - started) * 1000, data={"bytes": ref.size, "compression_level": 1})
        except InterruptedError:
            if not self._deleted(case_id, authoritative=True):
                self._repository.requeue_archive_task(case_id)
        except Exception as error:
            if self._deleted(case_id, authoritative=True):
                return True
            if not published:
                try:
                    self._set_status(case_id, "FAILED")
                except Exception as status_error:
                    # Deletion can win after the first failure check but before
                    # the FAILED publication barrier. That is cancellation,
                    # not an unknown infrastructure commit that pauses service.
                    if self._deleted(case_id, authoritative=True):
                        return True
                    self.operational_state.record(case_id=case_id, job_id=payload["source_job_id"],
                        phase="ARCHIVE_STATUS_COMMIT",
                        error_code=error.error.code if isinstance(error, ApplicationPortError) else ErrorCode.RESOURCE_PUBLISH_FAILED,
                        secondary_error_code=status_error.error.code if isinstance(status_error, ApplicationPortError) else ErrorCode.STATE_WRITE_FAILED)
                    log_event("case.archive.status_unknown", level=logging.ERROR, case_id=case_id,
                        job_id=payload["source_job_id"], error=status_error)
            log_event("case.archive.failed", level=logging.ERROR, case_id=case_id, error=error)
            record_journey_event("case.archive.failed", case_id=case_id, duration_ms=(time.perf_counter() - started) * 1000)
        finally:
            try:
                if staged is not None:
                    self._resources.discard(staged)
                self._resources.forget_case(case_id)
            finally:
                with self._activity:
                    self._active_cases.discard(case_id)
        return True
