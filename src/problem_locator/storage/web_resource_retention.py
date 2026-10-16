"""Expire Web execution resources while retaining the public conversation."""
from __future__ import annotations

import json
import uuid
from contextlib import ExitStack

from .database import dialect_for, lock_conversation

_CLOSED = ("COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED")
_RESULT = ("RESOLVED", "PARTIALLY_RESOLVED", "UNRESOLVED")
_CANDIDATE_BATCH_SIZE = 128


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class WebResourceRetention:
    def __init__(self, history):
        self.history = history

    def candidates(self, cutoff=None):
        """Visit bounded, ordered batches without keeping a database lease open.

        The cursor belongs only to this pass. Busy runs are retried from the
        beginning next time, and removal of earlier rows cannot shift a page.
        """
        h = self.history
        after = None
        while True:
            with h.repository.database_read() as db:
                d = dialect_for(db)
                completed = f"coalesce({d.json_text('r.body', '$.completed_at')},{d.json_text('r.body', '$.updated_at')})"
                finished = d.timestamp(completed)
                tail, params = ("", ()) if cutoff is None else (f" AND {finished}<={d.timestamp('?')}", (cutoff,))
                if after is not None:
                    tail += f" AND ({finished},r.run_id)>({d.timestamp('?')},?)"
                    params += after
                rows = db.execute(f"SELECT r.conversation_id,r.run_id,r.case_id,{completed} FROM agent_conversation_runs r "
                    "JOIN agent_conversations c USING(conversation_id) WHERE c.deleted_at IS NULL "
                    "AND r.status IN ('COMPLETED','FAILED','INTERRUPTED','CANCELLED') "
                    f"AND coalesce({d.json_int('r.body', '$.resources_expired')},0)=0" + tail +
                    f" ORDER BY {finished},r.run_id LIMIT ?", (*params, _CANDIDATE_BATCH_SIZE)).fetchall()
            for cid, rid, case_id, completed_at in rows:
                yield cid, rid, case_id
            if len(rows) < _CANDIDATE_BATCH_SIZE:
                return
            # Compare timestamps by instant, using exactly the ORDER BY
            # expression; lexical ISO ordering differs for legacy UTC offsets.
            after = rows[-1][3], rows[-1][1]

    @staticmethod
    def upload_in_use(db, cid, aid, *, excluding_run=None):
        """Historical message references do not keep bytes alive indefinitely."""
        d = dialect_for(db)
        references = d.json_array("m.body", "$.attachment_ids", "x")
        rows = db.execute(f"SELECT DISTINCT m.run_id FROM agent_messages m,{references} "
            "WHERE m.conversation_id=? AND x.value=? UNION "
            "SELECT run_id FROM agent_attachment_imports WHERE attachment_id=?", (cid, aid, aid)).fetchall()
        for (rid,) in rows:
            if rid == excluding_run:
                continue
            row = db.execute("SELECT body FROM agent_conversation_runs WHERE conversation_id=? AND run_id=?", (cid, rid)).fetchone()
            if row is not None and not json.loads(row[0]).get("resources_expired", False):
                return True
        attachments = d.json_array("r.payload", "$.message.attachment_ids", "x")
        excluded, params = ("", (cid, aid)) if excluding_run is None else (" AND r.run_id<>?", (cid, aid, excluding_run))
        return db.execute(f"SELECT 1 FROM agent_generic_restarts r,{attachments} "
            "WHERE r.conversation_id=? AND r.status IN ('PENDING','COMMITTED') "
            "AND x.value=?" + excluded + " LIMIT 1", params).fetchone() is not None

    @staticmethod
    def mark_upload_expired(db, cid, aid):
        row = db.execute("SELECT body,storage_path FROM agent_attachments WHERE conversation_id=? AND attachment_id=?", (cid, aid)).fetchone()
        if row is None:
            return False
        body = json.loads(row[0])
        if body["status"] == "EXPIRED":
            return False
        body["status"] = "EXPIRED"
        # Keep charging the reservation until its durable cleanup receipt has
        # physically finished. EXPIRED is terminal and cannot consume this path.
        db.execute("UPDATE agent_attachments SET body=?,storage_path=? WHERE attachment_id=?",
            (_json(body), row[1] or "cleanup-pending:" + aid, aid))
        return True

    def _run_uploads(self, db, cid, rid):
        d = dialect_for(db)
        refs = d.json_array("m.body", "$.attachment_ids", "x")
        restarts = d.json_array("r.payload", "$.message.attachment_ids", "x")
        candidates = [row[0] for row in db.execute(f"SELECT DISTINCT x.value FROM agent_messages m,{refs} "
            "WHERE m.conversation_id=? AND m.run_id=? UNION SELECT attachment_id FROM agent_attachment_imports WHERE run_id=? "
            f"UNION SELECT x.value FROM agent_generic_restarts r,{restarts} WHERE r.conversation_id=? AND r.run_id=?",
            (cid, rid, rid, cid, rid))]
        return sorted(aid for aid in candidates if not self.upload_in_use(db, cid, aid, excluding_run=rid)
            and db.execute("SELECT 1 FROM agent_attachments WHERE conversation_id=? AND attachment_id=? "
                f"AND {d.json_text('body', '$.status')}<>'EXPIRED'", (cid, aid)).fetchone() is not None)

    def expire_run(self, cid, rid, case_id, cutoff=None, *, reason="RETENTION_EXPIRED"):
        h = self.history
        with ExitStack() as leases:
            lease = h.usage_guard.acquire_cleanup_if_idle(cid, temporary=True)
            if lease is None:
                return False
            leases.enter_context(lease)
            with h.repository.database_read() as db:
                row = db.execute("SELECT r.body FROM agent_conversation_runs r JOIN agent_conversations c USING(conversation_id) "
                    "WHERE r.conversation_id=? AND r.run_id=? AND c.deleted_at IS NULL", (cid, rid)).fetchone()
                if row is None:
                    return False
                body = json.loads(row[0])
                if body["status"] not in _CLOSED or body.get("resources_expired") or (cutoff is not None and not h._expired(body, cutoff)):
                    return False
                followups = getattr(h.store, "followup_store", None)
                if followups is not None and followups.busy(db, cid, run_id=rid):
                    return False
            # The archiver reads directly under our conversation exclusion. It
            # must finish before the Case cleanup lease revokes resource reads.
            if h.archive_run is not None:
                if not h.archive_run(cid, rid):
                    return False
            elif body.get("report_available") or body.get("case_status") in _RESULT:
                return False
            if case_id is not None:
                lease = h.repository.case_cleanup_if_idle(case_id)
                if lease is None:
                    return False
                leases.enter_context(lease)
                if not h._idle([case_id]):
                    return False
            with h._barrier():
                # Web eligibility is anchored on the run. Core's own cleanup
                # API still checks that the Case is idle and has no archive task.
                case_cutoff = h.clock.now()
                manifest = h.repository.prepare_history_case_cleanup(case_id, case_cutoff) if case_id else {
                    "case_ids": [], "job_ids": [], "attachment_ids": [], "paths": []}
                if manifest is None or not h._uploads_idle(manifest["attachment_ids"]):
                    return False
                with h.repository.database_read() as db:
                    workspace = dialect_for(db).json_text("payload", "$.workspace_id")
                    workspaces = [row[0] for row in db.execute(f"SELECT {workspace} FROM agent_dispatches "
                        f"WHERE run_id=? AND {workspace} IS NOT NULL", (rid,))]
                    if followups is not None:
                        workspaces.extend(followups.workspace_ids(db, cid, run_id=rid))
                    upload_ids = self._run_uploads(db, cid, rid)
                workspaces.extend(body.get("legacy_workspace_ids", []))
                manifest.update(conversation_id=cid, run_id=rid, kind="WEB_RESOURCES", reason=reason,
                    workspace_ids=sorted(set(workspaces)), upload_ids=upload_ids)
                manifest["paths"] = sorted(set(manifest["paths"]) | h._workspace_paths(manifest["job_ids"], workspaces)
                    | {f"resources/conversations/{aid}" for aid in upload_ids})
                h._validate_manifest(manifest)
                cleanup_id = str(uuid.uuid5(uuid.UUID(rid), "web-resources-v1"))
                def expire(db):
                    lock_conversation(db, cid)
                    current = db.execute("SELECT r.body FROM agent_conversation_runs r JOIN agent_conversations c USING(conversation_id) "
                        "WHERE r.conversation_id=? AND r.run_id=? AND c.deleted_at IS NULL", (cid, rid)).fetchone()
                    if current is None:
                        return False
                    run = json.loads(current[0])
                    if run["status"] not in _CLOSED or run.get("resources_expired"):
                        return False
                    if followups is not None and followups.busy(db, cid, run_id=rid):
                        return False
                    if any(self.upload_in_use(db, cid, aid, excluding_run=rid) for aid in upload_ids):
                        return False
                    run.update(resources_expired=True, resources_expired_at=h.clock.now(), resource_cleanup_reason=reason)
                    db.execute("UPDATE agent_conversation_runs SET body=? WHERE run_id=?", (_json(run), rid))
                    head = db.execute("SELECT current_run_id,body FROM agent_conversations WHERE conversation_id=?", (cid,)).fetchone()
                    if head[0] == rid:
                        public = json.loads(head[1])
                        public.update({key: run[key] for key in ("resources_expired", "resources_expired_at", "resource_cleanup_reason")})
                        db.execute("UPDATE agent_conversations SET body=? WHERE conversation_id=?", (_json(public), cid))
                    if followups is not None:
                        followups.expire_resources(db, cid, rid)
                    memory = getattr(h.store, "memory_store", None)
                    if memory is not None:
                        memory.expire_sources(db, cid, run_id=rid)
                    for aid in upload_ids:
                        self.mark_upload_expired(db, cid, aid)
                    db.execute("DELETE FROM agent_message_adoptions WHERE message_id IN "
                        "(SELECT message_id FROM agent_messages WHERE conversation_id=? AND run_id=?)", (cid, rid))
                    for table in ("agent_attachment_imports", "agent_dispatches", "agent_generic_restarts"):
                        db.execute(f"DELETE FROM {table} WHERE run_id=?", (rid,))
                    return True
                if not h.repository.commit_history_cleanup(cleanup_id, manifest, case_id=case_id,
                        cutoff=case_cutoff, mutate=expire):
                    return False
            h._database_dirty = True
            h._finish_paths(cleanup_id, manifest)
            return True

    def finish_receipt(self, db, manifest):
        cid = manifest.get("conversation_id")
        if cid is None:
            return
        lock_conversation(db, cid)
        d = dialect_for(db)
        for aid in manifest.get("upload_ids", []):
            db.execute("UPDATE agent_attachments SET storage_path=NULL WHERE conversation_id=? AND attachment_id=? "
                f"AND {d.json_text('body', '$.status')}='EXPIRED'", (cid, aid))
        rid = manifest.get("run_id")
        followups = getattr(self.history.store, "followup_store", None)
        if rid is not None and followups is not None:
            followups.finish_resource_cleanup(db, cid, rid)
