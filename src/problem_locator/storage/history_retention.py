"""Seven-day business history expiry with durable, exact-path cleanup receipts.

Only this installation's Case, run and upload metadata identifies candidates.
No model is invoked, and no codeagent or external installation path is visited.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from contextlib import ExitStack, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

from pydantic import TypeAdapter

from problem_locator.contracts import OpaqueId
from problem_locator.diagnostics import log_event
from .paths import job_workspace_names

HISTORY_RETENTION_SECONDS = 7 * 24 * 60 * 60
_ID = TypeAdapter(OpaqueId)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class HistoryRetentionService:
    """Expire each completed run separately, preserving in-flight operations."""

    def __init__(self, repository, store, quarantine, usage_guard, clock, *,
                 dispatcher=None, archive=None, attachment_registry=None,
                 coordination_lock=None):
        self.repository, self.store, self.quarantine = repository, store, quarantine
        self.usage_guard, self.clock = usage_guard, clock
        self.dispatcher, self.archive = dispatcher, archive
        self.attachment_registry = attachment_registry
        self.coordination_lock = coordination_lock
        self._processing = threading.Lock()
        self._last_compaction = None
        self._database_dirty = False

    def _barrier(self):
        # Match formal publication: coordination barrier -> Case mutex -> DB.
        # The usage reservations are counters, not held Case/thread locks.
        return self.coordination_lock if self.coordination_lock is not None else nullcontext()

    @staticmethod
    def _time(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

    @classmethod
    def _expired(cls, body, cutoff):
        return cls._time(body.get("completed_at", body["updated_at"])) <= cls._time(cutoff)

    def _idle(self, case_ids):
        return all(worker is None or worker.cases_idle(case_ids)
                   for worker in (self.dispatcher, self.archive))

    def _uploads_idle(self, attachment_ids):
        return self.attachment_registry is None or not set(attachment_ids).intersection(
            self.attachment_registry.active_attachment_ids())

    def _workspace_paths(self, job_ids, workspace_ids=()):
        paths = {f"tmp/workspaces/{value}" for value in workspace_ids}
        for job_id in job_ids:
            paths.update(f"tmp/workspaces/{name}" for name in job_workspace_names(job_id))
        return paths

    @staticmethod
    def _validate_manifest(manifest):
        ids = {name: {_ID.validate_python(value) for value in manifest.get(name, [])}
               for name in ("case_ids", "job_ids", "attachment_ids", "workspace_ids", "upload_ids")}
        if manifest.get("conversation_id") is not None:
            _ID.validate_python(manifest["conversation_id"])
        for relative in manifest["paths"]:
            parts = PurePosixPath(relative).parts
            if "\\" in relative or relative != "/".join(parts) or any(p in {".", ".."} for p in parts):
                raise ValueError("history cleanup path is invalid")
            if len(parts) == 2 and parts[0] == "jobs" and parts[1] in ids["job_ids"]:
                continue
            if len(parts) == 3:
                prefix, key = parts[:2], parts[2]
                if prefix == ("resources", "cases") and key in ids["case_ids"]:
                    continue
                if prefix == ("resources", "conversations") and key in ids["upload_ids"]:
                    continue
                if prefix == ("tmp", "uploads") and key in ids["attachment_ids"]:
                    continue
                if prefix == ("tmp", "proposals") and key in ids["job_ids"]:
                    continue
                if prefix == ("tmp", "workspaces"):
                    names = {name for job_id in ids["job_ids"] for name in job_workspace_names(job_id)}
                    if key in ids["workspace_ids"] or key in names:
                        continue
            raise ValueError("history cleanup path is outside its recorded owner")

    def _finish_paths(self, cleanup_id, manifest):
        self._validate_manifest(manifest)
        for relative in manifest["paths"]:
            source = self.repository.layout.data_root / relative
            destination = self.repository.layout.quarantine / cleanup_id / relative
            if not (source.exists() or source.is_symlink() or destination.exists() or destination.is_symlink()):
                continue
            moved = self.quarantine.move_if(cleanup_id, source, lambda: True)
            if moved is None:
                raise RuntimeError("history cleanup candidate was not isolated")
            self.quarantine.delete(moved)
        with self.repository.database_transaction() as db:
            db.execute("DELETE FROM history_cleanup_jobs WHERE cleanup_id=?", (cleanup_id,))
        self._database_dirty = True

    def _retry_paths(self):
        with self.repository.database_read() as db:
            rows = db.execute("SELECT cleanup_id,manifest FROM history_cleanup_jobs ORDER BY rowid").fetchall()
        changed, failures = False, []
        for cleanup_id, raw in rows:
            try:
                changed = self._retry_path(cleanup_id, raw) or changed
            except Exception as error:
                failures.append(error)
        return changed, failures

    def _retry_path(self, cleanup_id, raw):
        manifest = json.loads(raw)
        self._validate_manifest(manifest)
        with ExitStack() as leases:
            cid = manifest.get("conversation_id")
            if cid is not None:
                lease = self.usage_guard.acquire_cleanup_if_idle(cid)
                if lease is None:
                    return False
                leases.enter_context(lease)
            busy = False
            for case_id in manifest.get("case_ids", []):
                lease = self.repository.case_cleanup_if_idle(case_id)
                if lease is None:
                    busy = True
                    break
                leases.enter_context(lease)
            if busy or not self._idle(manifest.get("case_ids", [])) or not self._uploads_idle(manifest.get("attachment_ids", [])):
                return False
            self._finish_paths(cleanup_id, manifest)
            return True

    def _prune_keys_and_tombstones(self, cutoff):
        with self.repository.database_transaction() as db:
            before = db.total_changes
            expired = db.execute("SELECT k.request_hash,k.conversation_id FROM agent_create_keys k "
                "JOIN agent_create_key_times t USING(request_hash) WHERE t.created_at<=?", (cutoff,)).fetchall()
            for key, cid in expired:
                db.execute("DELETE FROM agent_create_keys WHERE request_hash=?", (key,))
                db.execute("DELETE FROM agent_create_key_times WHERE request_hash=?", (key,))
                # The request column has a UNIQUE constraint independent of the
                # retry index. Release both together when the seven days end.
                if db.execute("SELECT 1 FROM agent_create_keys WHERE conversation_id=?", (cid,)).fetchone() is None:
                    db.execute("UPDATE agent_conversations SET request_id=? WHERE conversation_id=?",
                               ("expired:" + cid, cid))
            rows = db.execute("SELECT conversation_id FROM agent_conversations WHERE cleanup_status='DELETED' "
                "AND deleted_at<=?", (cutoff,)).fetchall()
            for (cid,) in rows:
                db.execute("DELETE FROM agent_create_key_times WHERE request_hash IN "
                    "(SELECT request_hash FROM agent_create_keys WHERE conversation_id=?)", (cid,))
                for table in ("agent_create_keys", "agent_deleted_requests", "agent_cleanup_jobs", "agent_conversations"):
                    db.execute(f"DELETE FROM {table} WHERE conversation_id=?", (cid,))
            changed = db.total_changes > before
        self._database_dirty |= changed
        return changed

    @staticmethod
    def _prune_run(db, cid, run_id, cutoff):
        row = db.execute("SELECT r.body,c.current_run_id,c.body FROM agent_conversation_runs r "
            "JOIN agent_conversations c USING(conversation_id) WHERE r.conversation_id=? AND r.run_id=? "
            "AND c.deleted_at IS NULL", (cid, run_id)).fetchone()
        if row is None or row[1] == run_id or not HistoryRetentionService._expired(json.loads(row[0]), cutoff):
            return False
        maximum = db.execute("SELECT max(sequence) FROM agent_events WHERE conversation_id=? AND run_id=?",
                             (cid, run_id)).fetchone()[0]
        head = json.loads(row[2])
        head["events_pruned_through"] = max(head.get("events_pruned_through", 0), maximum or 0)
        db.execute("UPDATE agent_conversations SET body=? WHERE conversation_id=?", (_json(head), cid))
        db.execute("DELETE FROM agent_message_adoptions WHERE message_id IN "
            "(SELECT message_id FROM agent_messages WHERE conversation_id=? AND run_id=?)", (cid, run_id))
        for table in ("agent_attachment_imports", "agent_stop_requests", "agent_messages", "agent_events", "agent_dispatches"):
            db.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
        db.execute("DELETE FROM agent_conversation_runs WHERE run_id=? AND conversation_id=?", (run_id, cid))
        return True

    def _expire_run(self, cid, run_id, case_id, cutoff):
        with ExitStack() as leases:
            lease = self.usage_guard.acquire_cleanup_if_idle(cid)
            if lease is None:
                return False
            leases.enter_context(lease)
            if case_id is not None:
                lease = self.repository.case_cleanup_if_idle(case_id)
                if lease is None or not self._idle([case_id]):
                    if lease is not None:
                        with lease:
                            pass
                    return False
                leases.enter_context(lease)
            with self._barrier():
                manifest = self.repository.prepare_history_case_cleanup(case_id, cutoff) if case_id else {
                    "case_ids": [], "job_ids": [], "attachment_ids": [], "paths": []}
                if manifest is None or not self._uploads_idle(manifest["attachment_ids"]):
                    return False
                with self.repository.database_read() as db:
                    workspaces = [row[0] for row in db.execute("SELECT json_extract(payload,'$.workspace_id') "
                        "FROM agent_dispatches WHERE run_id=? AND json_extract(payload,'$.workspace_id') IS NOT NULL", (run_id,))]
                    body = db.execute("SELECT body FROM agent_conversation_runs WHERE run_id=?", (run_id,)).fetchone()
                if body is None:
                    return False
                workspaces.extend(json.loads(body[0]).get("legacy_workspace_ids", []))
                workspaces = sorted({_ID.validate_python(value) for value in workspaces})
                manifest.update(conversation_id=cid, workspace_ids=workspaces, upload_ids=[])
                manifest["paths"] = sorted(set(manifest["paths"]) | self._workspace_paths(manifest["job_ids"], workspaces))
                self._validate_manifest(manifest)
                cleanup_id = str(uuid.uuid5(uuid.UUID(run_id), "history-retention-v1"))
                committed = self.repository.commit_history_cleanup(cleanup_id, manifest,
                    case_id=case_id, cutoff=cutoff,
                    mutate=lambda db: self._prune_run(db, cid, run_id, cutoff))
            if not committed:
                return False
            self._database_dirty = True
            self._finish_paths(cleanup_id, manifest)
            return True

    def _expire_conversation(self, cid, cutoff):
        lease = self.usage_guard.acquire_cleanup_if_idle(cid)
        if lease is None:
            return False
        with lease, ExitStack() as case_leases:
            with self.repository.database_read() as db:
                row = db.execute("SELECT deleted_at FROM agent_conversations WHERE conversation_id=?", (cid,)).fetchone()
                if row is None or row[0] is not None:
                    return False
                runs = [(case_id, json.loads(raw)) for case_id, raw in db.execute(
                    "SELECT case_id,body FROM agent_conversation_runs WHERE conversation_id=?", (cid,))]
            if not runs or any(not self._expired(body, cutoff) for _, body in runs):
                return False
            case_ids = [case_id for case_id, _ in runs if case_id is not None]
            if not self._idle(case_ids):
                return False
            with self._barrier():
                for case_id in case_ids:
                    case_lease = self.repository.case_cleanup_if_idle(case_id)
                    if case_lease is None:
                        return False
                    case_leases.enter_context(case_lease)
                    manifest = self.repository.prepare_history_case_cleanup(case_id, cutoff)
                    if manifest is None or not self._uploads_idle(manifest["attachment_ids"]):
                        return False
            # Existing explicit deletion settles dormant WAITING_INPUT Cases,
            # drains workers and persists its own crash-safe resource manifest.
            self.store.request_delete(cid)
            self._database_dirty = True
            return True

    def _expire_uploads(self, cutoff):
        with self.repository.database_read() as db:
            rows = db.execute("SELECT a.attachment_id,a.conversation_id FROM agent_attachments a "
                "JOIN agent_conversations c USING(conversation_id) WHERE c.deleted_at IS NULL "
                "AND json_extract(a.body,'$.created_at')<=?", (cutoff,)).fetchall()
        changed, failures = False, []
        for aid, cid in rows:
            try:
                changed = self._expire_upload(aid, cid, cutoff) or changed
            except Exception as error:
                failures.append(error)
        if failures:
            raise failures[0]
        return changed

    def _expire_upload(self, aid, cid, cutoff):
        lease = self.usage_guard.acquire_cleanup_if_idle(cid)
        if lease is None:
            return False
        with lease:
            manifest = dict(conversation_id=cid, case_ids=[], job_ids=[], attachment_ids=[],
                workspace_ids=[], upload_ids=[aid], paths=[f"resources/conversations/{aid}"])
            self._validate_manifest(manifest)
            def remove(db):
                row = db.execute("SELECT body FROM agent_attachments WHERE attachment_id=? AND conversation_id=?", (aid, cid)).fetchone()
                if row is None or json.loads(row[0])["created_at"] > cutoff:
                    return False
                # A retained run may still display or use its selected
                # attachment, even after the original upload is seven days old.
                referenced = db.execute("SELECT 1 FROM agent_messages m,json_each(m.body,'$.attachment_ids') x "
                    "WHERE m.conversation_id=? AND x.value=? LIMIT 1", (cid, aid)).fetchone()
                if referenced or db.execute("SELECT 1 FROM agent_attachment_imports WHERE attachment_id=?", (aid,)).fetchone():
                    return False
                maximum = db.execute("SELECT max(sequence) FROM agent_events WHERE conversation_id=? "
                    "AND json_extract(body,'$.data.attachment_id')=?", (cid, aid)).fetchone()[0]
                row = db.execute("SELECT body FROM agent_conversations WHERE conversation_id=?", (cid,)).fetchone()
                head = json.loads(row[0])
                head["events_pruned_through"] = max(head.get("events_pruned_through", 0), maximum or 0)
                db.execute("UPDATE agent_conversations SET body=? WHERE conversation_id=?", (_json(head), cid))
                db.execute("DELETE FROM agent_events WHERE conversation_id=? AND json_extract(body,'$.data.attachment_id')=?", (cid, aid))
                db.execute("DELETE FROM agent_attachments WHERE attachment_id=?", (aid,))
                return True
            cleanup_id = str(uuid.uuid5(uuid.UUID(aid), "history-upload-retention-v1"))
            if not self.repository.commit_history_cleanup(cleanup_id, manifest, mutate=remove):
                return False
            self._database_dirty = True
            self._finish_paths(cleanup_id, manifest)
            return True

    def _expire_core_case(self, case_id, cutoff):
        if self.store.conversation_for_case(case_id) is not None:
            return False
        lease = self.repository.case_cleanup_if_idle(case_id)
        if lease is None:
            return False
        with lease:
            if not self._idle([case_id]):
                return False
            with self._barrier():
                manifest = self.repository.prepare_history_case_cleanup(case_id, cutoff)
                if manifest is None or not manifest["case_ids"] or not self._uploads_idle(manifest["attachment_ids"]):
                    return False
                manifest.update(workspace_ids=[], upload_ids=[])
                manifest["paths"] = sorted(set(manifest["paths"]) | self._workspace_paths(manifest["job_ids"]))
                self._validate_manifest(manifest)
                cleanup_id = str(uuid.uuid5(uuid.UUID(case_id), "history-case-retention-v1"))
                def still_core(db):
                    return db.execute("SELECT 1 FROM agent_conversation_runs WHERE case_id=?", (case_id,)).fetchone() is None
                if not self.repository.commit_history_cleanup(cleanup_id, manifest,
                        case_id=case_id, cutoff=cutoff, mutate=still_core):
                    return False
            self._database_dirty = True
            self._finish_paths(cleanup_id, manifest)
            return True

    def run_once(self) -> bool:
        if not self._processing.acquire(blocking=False):
            return False
        try:
            now = self._time(self.clock.now())
            cutoff = (now - timedelta(seconds=HISTORY_RETENTION_SECONDS)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            changed, failures = self._retry_paths()
            def attempt(action):
                try:
                    return action()
                except Exception as error:
                    failures.append(error)
                    return False
            changed = self._prune_keys_and_tombstones(cutoff) or changed
            with self.repository.database_read() as db:
                rows = db.execute("SELECT r.conversation_id,r.run_id,r.case_id,c.current_run_id "
                    "FROM agent_conversation_runs r JOIN agent_conversations c USING(conversation_id) "
                    "WHERE c.deleted_at IS NULL AND coalesce(json_extract(r.body,'$.completed_at'),"
                    "json_extract(r.body,'$.updated_at'))<=? ORDER BY r.rowid", (cutoff,)).fetchall()
            for cid, run_id, case_id, current in rows:
                if run_id == current:
                    changed = attempt(lambda: self._expire_conversation(cid, cutoff)) or changed
                else:
                    changed = attempt(lambda: self._expire_run(cid, run_id, case_id, cutoff)) or changed
            changed = attempt(lambda: self._expire_uploads(cutoff)) or changed
            for case_id in self.repository.history_case_candidates(cutoff):
                changed = attempt(lambda: self._expire_core_case(case_id, cutoff)) or changed
            # Explicit/asynchronous conversation cleanup can free pages after
            # this service's previous pass. Maintenance cannot depend only on
            # mutations made by this adapter.
            if self._last_compaction is None or now - self._last_compaction >= timedelta(hours=1):
                self.repository.compact_history_database()
                self._last_compaction, self._database_dirty = now, False
            if failures:
                # Each failed manifest remains durable. Report the first error
                # only after independent histories have had their cleanup turn.
                raise failures[0]
            return changed
        except Exception as error:
            log_event("history.retention.failed", level=logging.ERROR, error=error)
            raise
        finally:
            self._processing.release()


__all__ = ["HISTORY_RETENTION_SECONDS", "HistoryRetentionService"]
