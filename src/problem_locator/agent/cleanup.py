"""Restartable cleanup of explicitly deleted Agent conversations only."""
from __future__ import annotations

import logging
import threading
import uuid
from contextlib import ExitStack
from pathlib import PurePosixPath

from pydantic import TypeAdapter

from problem_locator.contracts import OpaqueId
from problem_locator.diagnostics import log_event
from problem_locator.storage.paths import job_workspace_names, workspace_owner_id

_ID = TypeAdapter(OpaqueId)


class ConversationCleanupService:
    """Revoke first; persist exact ownership before releasing database references.

    All lifecycle/read/write leases belong to the injected conversation guard.
    Model and archive workers additionally drain before any resource is removed.
    This worker never invokes a model or resumes a diagnostic task.
    """

    def __init__(self, store, repository, quarantine, usage_guard, *, dispatcher=None, archive=None):
        self.store, self.repository = store, repository
        self.quarantine, self.usage_guard = quarantine, usage_guard
        self.dispatcher, self.archive = dispatcher, archive
        self._stop = threading.Event()
        self._processing = threading.Lock()
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="agent-cleanup", daemon=True)
            self._thread.start()

    def shutdown(self, timeout_seconds=30):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(max(0, timeout_seconds))
            return not self._thread.is_alive()
        return True

    def _run(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as error:
                log_event("agent.cleanup.worker_failed", level=logging.ERROR, error=error)
            self._stop.wait(0.5)

    @staticmethod
    def _ids(values):
        return sorted({_ID.validate_python(value) for value in values})

    def _manifest(self, conversation_id, previous, context):
        case_ids = self._ids([*previous.get("case_ids", []), *context.get("case_ids", [])])
        observed = self.repository.prepare_agent_case_cleanup(conversation_id, case_ids)
        job_ids = self._ids([*previous.get("job_ids", []), *context.get("job_ids", []), *observed["job_ids"]])
        attachment_ids = self._ids([*previous.get("attachment_ids", []), *context.get("attachment_ids", [])])
        workspace_ids = self._ids([*previous.get("workspace_ids", []), *context.get("workspace_ids", [])])
        paths = set(previous.get("paths", [])) | set(observed["paths"])
        paths.update(f"resources/conversations/{value}" for value in attachment_ids)
        paths.update(f"tmp/workspaces/{value}" for value in workspace_ids)
        for value in job_ids:
            paths.update((f"jobs/{value}", f"tmp/proposals/{value}"))
            paths.update(f"tmp/workspaces/{name}" for name in job_workspace_names(value))
        cleanup_id = str(uuid.uuid5(uuid.UUID(conversation_id), "agent-conversation-cleanup-v2"))
        # Paths originate exclusively from the durable Agent/Case metadata, not
        # HTTP input. Reject malformed identities before touching the filesystem.
        for value in paths:
            parts = PurePosixPath(value).parts
            if "\\" in value or value != "/".join(parts) or any(item in {".", ".."} for item in parts):
                raise ValueError("invalid cleanup path")
            if len(parts) == 2 and parts[0] == "jobs" and parts[1] in job_ids:
                continue
            if len(parts) == 3:
                if parts[:2] == ("tmp", "workspaces"):
                    owner_id = workspace_owner_id(parts[2])
                    if owner_id in job_ids or (parts[2] == owner_id and owner_id in workspace_ids):
                        continue
                    raise ValueError("cleanup workspace is outside its owning conversation")
                _ID.validate_python(parts[2])
                if parts[:2] == ("resources", "cases") and parts[2] in case_ids:
                    continue
                if parts[:2] == ("resources", "conversations") and parts[2] in attachment_ids:
                    continue
                if parts[:2] == ("tmp", "proposals") and parts[2] in job_ids:
                    continue
                if parts[:2] == ("tmp", "uploads"):
                    # This identity was captured by prepare_agent_case_cleanup
                    # before its Case snapshot could be removed.
                    if value in observed["paths"] or value in previous.get("paths", []):
                        continue
            raise ValueError("cleanup path is outside its owning conversation")
        return {**previous, "case_ids": case_ids, "job_ids": job_ids, "attachment_ids": attachment_ids,
            "workspace_ids": workspace_ids,
            "cleanup_id": cleanup_id, "paths": sorted(paths)}

    def run_once(self) -> bool:
        if self._stop.is_set() or not self._processing.acquire(blocking=False):
            return False
        conversation_id = None
        try:
            task = self.store.claim_cleanup()
            if task is None:
                return False
            conversation_id = _ID.validate_python(task["conversation_id"])
            context = self.store.cleanup_context(conversation_id)
            case_ids = self._ids([*task["manifest"].get("case_ids", []), *context.get("case_ids", [])])
            for worker in (self.dispatcher, self.archive):
                if worker is not None:
                    worker.cancel_cases(case_ids)
            lease = self.usage_guard.acquire_cleanup_if_idle(conversation_id)
            if lease is None:
                self.store.fail_cleanup(conversation_id, "CLEANUP_BUSY")
                return False
            with lease, ExitStack() as case_leases:
                context = self.store.cleanup_context(conversation_id)
                case_ids = self._ids([*case_ids, *context.get("case_ids", [])])
                for case_id in case_ids:
                    case_lease = self.repository.case_cleanup_if_idle(case_id)
                    if case_lease is None:
                        self.store.fail_cleanup(conversation_id, "CLEANUP_BUSY")
                        return False
                    case_leases.enter_context(case_lease)
                for worker in (self.dispatcher, self.archive):
                    if worker is not None:
                        worker.cancel_cases(case_ids)
                        if not worker.cases_idle(case_ids):
                            self.store.fail_cleanup(conversation_id, "CLEANUP_BUSY")
                            return False
                if context.get("pending_stop") or not self.repository.agent_cases_cleanup_ready(conversation_id, case_ids):
                    self.store.fail_cleanup(conversation_id, "CLEANUP_AWAITING_STOP")
                    return False
                manifest = self._manifest(conversation_id, task["manifest"], context)
                self.store.update_cleanup_manifest(conversation_id, manifest)
                self.repository.purge_agent_cases(conversation_id, manifest["case_ids"])
                root = self.repository.layout.data_root
                for relative in manifest["paths"]:
                    if self._stop.is_set():
                        self.store.fail_cleanup(conversation_id, "CLEANUP_INTERRUPTED")
                        return False
                    source = root / relative
                    destination = self.repository.layout.quarantine / manifest["cleanup_id"] / relative
                    if not source.exists() and not source.is_symlink() and not destination.exists() and not destination.is_symlink():
                        continue
                    isolated = self.quarantine.move_if(manifest["cleanup_id"], source, lambda: True)
                    if isolated is None:
                        raise ValueError("cleanup isolation was not completed")
                    self.quarantine.delete(isolated)
                self.store.finish_cleanup(conversation_id)
            return True
        except Exception as error:
            if conversation_id is not None:
                self.store.fail_cleanup(conversation_id, "CLEANUP_FAILED")
            log_event("agent.cleanup.failed", level=logging.ERROR, error=error)
            return False
        finally:
            self._processing.release()


__all__ = ["ConversationCleanupService"]
