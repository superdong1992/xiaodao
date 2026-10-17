"""Small confirmed-input workflow in front of the existing Agent runner."""
from __future__ import annotations

import hashlib
import json
import threading

from problem_locator.agent.models import AgentAttachment, AgentMessage, AgentStoreError, MessageReceipt
from problem_locator.agent.store import _json
from problem_locator.contracts.limits import MAX_CASE_RESOURCE_BYTES
from problem_locator.diagnostics import log_event
from problem_locator.dispatch.cancellation import CancellationController
from problem_locator.storage.database import lock_conversation

from .models import (
    ConfirmDraftRequest, ContinuationDraft, ContinuationReceipt, CreateDraftRequest,
    InputChanges, ReportVersion, ReportVersions, UpdateDraftRequest,
)

_CLOSED = {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _public(value):
    return ContinuationDraft.model_validate({key: value[key] for key in ContinuationDraft.model_fields if key in value})


def capture_report_inputs(db, body, aggregate):
    """Persist actual adopted inputs while the authoritative Case is available."""
    result = aggregate.case.generic_result_v2
    if result is None:
        return
    job = aggregate.jobs.get(result.source_job_id)
    if job is None:
        return
    text = aggregate.case.raw_problem_text
    facts = [(item.provenance.input_name + "=" if item.provenance.input_name else "") + item.statement
             for item in aggregate.case.diagnosis_state.user_facts if item.status.value == "ACTIVE"]
    # Only a complete line can already carry a confirmed value: e.g. a source
    # value of 100 must not hide a confirmed value of 10 for the same field.
    source_lines = set(text.splitlines())
    facts = list(dict.fromkeys(item for item in [*facts, *job.generic_supplement_texts]
                              if item not in source_lines))
    effective = text + ("\n\n已确认的补充信息：\n" + "\n".join(facts) if facts else "")
    rows = db.execute("SELECT attachment_id,case_attachment_id FROM agent_attachment_imports WHERE run_id=?",
                      (body["run_id"],)).fetchall()
    source_ids = {case_id: aid for aid, case_id in rows}
    selected = [source_ids[item] for item in job.attachment_refs if item in source_ids]
    skill = job.skill_ref
    tool = job.logparse_tool_ref
    body["report_inputs"] = {
        "effective_problem_text": effective, "attachment_ids": selected,
        "runtime": {"skill": None if skill is None else skill.model_dump(mode="json"),
                    "generic_skill": job.generic_skill_name,
                    # A launch configuration digest is not a resolved model ID.
                    "model": body.get("model_configuration_fingerprint"),
                    "agent_profile": job.agent_profile_ref.model_dump(mode="json"),
                    "context_policy": job.context_policy_ref.model_dump(mode="json"),
                    "logparse": None if tool is None else tool.model_dump(mode="json")},
    }
    body.setdefault("report_version", 1)
    body.setdefault("root_run_id", body["run_id"])
    if body.get("parent_run_id"):
        current = [json.loads(row[0]) for aid in selected for row in db.execute(
            "SELECT body FROM agent_attachments WHERE attachment_id=?", (aid,)).fetchall()]
        body["input_changes"] = ContinuationService._changes(body["source_problem_text"], effective,
            body.get("source_attachments", []), current)


class ContinuationService:
    def __init__(self, agent):
        self.agent, self.store = agent, agent.store
        self.repository = self.store.repository
        self._processing = threading.Lock()
        with self.repository.database_transaction() as db:
            self.ensure_schema(db)

    @staticmethod
    def ensure_schema(db):
        db.execute("CREATE TABLE IF NOT EXISTS agent_continuation_drafts ("
            "draft_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,parent_run_id TEXT NOT NULL,"
            "status TEXT NOT NULL,body TEXT NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS agent_continuation_pending ON agent_continuation_drafts(status,conversation_id)")
        db.execute("CREATE TABLE IF NOT EXISTS agent_continuation_requests ("
            "conversation_id TEXT NOT NULL,request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,"
            "body TEXT NOT NULL,PRIMARY KEY(conversation_id,request_id))")

    def recover(self):
        with self.repository.database_transaction() as db:
            for cid, did in db.execute("SELECT conversation_id,draft_id FROM agent_continuation_drafts WHERE status='RUNNING'").fetchall():
                lock_conversation(db, cid)
                try:
                    value = self._load(db, cid, did)
                except AgentStoreError as error:
                    if error.status_code == 404:
                        # Deletion can commit before an interrupted preparation
                        # finishes. Its drafts belong to conversation cleanup.
                        continue
                    raise
                value.update(status="FAILED", failure_message="问题整理已中断，请直接编辑本轮信息后确认。")
                self._save(db, value)

    @staticmethod
    def _request(db, cid, request_id, fingerprint, result=None):
        row = db.execute("SELECT fingerprint,body FROM agent_continuation_requests WHERE conversation_id=? AND request_id=?",
                         (cid, request_id)).fetchone()
        if row is not None:
            if row[0] != fingerprint:
                raise AgentStoreError("AGENT_IDEMPOTENCY_CONFLICT", "同一 request_id 的内容不能更改。", 409)
            return json.loads(row[1])
        if result is not None:
            db.execute("INSERT INTO agent_continuation_requests VALUES (?,?,?,?)", (cid, request_id, fingerprint, _json(result)))
        return None

    def _load(self, db, cid, draft_id):
        self.store._load(db, cid)
        row = db.execute("SELECT body FROM agent_continuation_drafts WHERE conversation_id=? AND draft_id=?", (cid, draft_id)).fetchone()
        if row is None:
            raise AgentStoreError("AGENT_DRAFT_NOT_FOUND", "继续诊断草稿不存在。", 404)
        return json.loads(row[0])

    def _save(self, db, value, *, work_status=None):
        value["updated_at"] = self.store._now()
        _public(value)
        db.execute("UPDATE agent_continuation_drafts SET status=?,body=? WHERE draft_id=?",
                   (work_status or value["status"], _json(value), value["draft_id"]))

    def _head(self, db, cid, parent_id, *, parent=None, runs=None):
        current = self.store._load(db, cid)
        if parent is None:
            parent = self.store._load(db, cid, run_id=parent_id)
        if not parent.get("report_available"):
            raise AgentStoreError("AGENT_REPORT_REQUIRED", "请先等待正式报告生成。", 409)
        root = parent.get("root_run_id", parent_id)
        if runs is None:
            runs = (json.loads(raw) for (raw,) in db.execute(
                "SELECT body FROM agent_conversation_runs WHERE conversation_id=?", (cid,)))
        for other in runs:
            if (other.get("root_run_id", other["run_id"]) == root and other.get("report_available")
                    and other["ordinal"] > parent["ordinal"]):
                raise AgentStoreError("AGENT_REPORT_CHANGED", "已有新版报告，请从最新报告继续诊断。", 409)
        if current["status"] not in _CLOSED and not current.get("report_available"):
            raise AgentStoreError("AGENT_DIAGNOSIS_BUSY", "本会话已有诊断正在执行，请等待完成或先停止。", 409)
        return current, parent

    def _baseline(self, cid, rid):
        view = self.agent._conversation_detail(cid, ("report",), run_id=rid, history_before=None, history_limit=50)
        report = view.result
        if report is None or report.report_state != "READY" or report.format != "markdown":
            raise AgentStoreError("AGENT_CONTINUATION_UNSUPPORTED", "这份报告暂不支持继续诊断。", 409)
        run = self.store.get_run(cid, rid)
        inputs = run.get("report_inputs")
        if inputs is None and not run.get("resources_expired"):
            aggregate = self.repository.read_snapshot(view.case_id).cases[view.case_id]
            with self.repository.database_transaction() as db:
                current = self.store._load(db, cid, run_id=rid)
                capture_report_inputs(db, current, aggregate)
                self.store._save(db, current)
                inputs = current.get("report_inputs")
        if inputs is None:
            # Historical reports can outlive disposable Cases. Keep user input,
            # but never pretend an expired upload is still a current source.
            history = self.store.get_conversation(cid, run_id=rid)
            text = "\n\n".join(item.text for item in history.messages if item.status == "APPLIED" and item.text.strip())
            inputs = {"effective_problem_text": text, "attachment_ids": [], "runtime": {}}
        if not inputs["effective_problem_text"].strip():
            raise AgentStoreError("AGENT_INPUT_UNAVAILABLE", "原问题信息不可用，请重新提供完整问题。", 409)
        return run, report, inputs

    def _attachments(self, cid, ids, *, require_available=True):
        result, seen = [], set()
        for aid in ids:
            item = self.store.get_attachment(aid)
            if item.conversation_id != cid:
                raise AgentStoreError("AGENT_ATTACHMENT_NOT_READY", "附件不属于本会话。", 409)
            if require_available:
                self.agent.uploads._require_available(item)
                if item.status not in {"READY", "IMPORTED"}:
                    raise AgentStoreError("AGENT_ATTACHMENT_NOT_READY", "请先完成日志上传，或替换已清理的日志。", 409)
            if item.sha256 not in seen:
                result.append(AgentAttachment.model_validate(item.model_dump(mode="python")))
                seen.add(item.sha256)
        if len(result) > 20 or sum(item.size for item in result) > MAX_CASE_RESOURCE_BYTES:
            raise AgentStoreError("AGENT_ATTACHMENT_LIMIT", "本轮日志数量或总大小超出限制，请减少附件。", 413)
        return result

    @staticmethod
    def _changes(before, after, old, new):
        old_hashes, new_hashes = {item["sha256"] for item in old}, {item["sha256"] for item in new}
        return InputChanges(before_problem_text=before, after_problem_text=after,
            added_attachments=[item for item in new if item["sha256"] not in old_hashes],
            removed_attachments=[item for item in old if item["sha256"] not in new_hashes]).model_dump(mode="json")

    def create(self, cid, rid, request: CreateDraftRequest, *, owner_key=None):
        self.agent._available()
        fingerprint = _hash(["create", rid, request.model_dump(mode="json")])
        with self.agent.operation_lease(cid, owner_key=owner_key), self.agent._message_lock(cid):
            with self.repository.database_transaction() as db:
                self.store._load(db, cid)
                previous = self._request(db, cid, request.request_id, fingerprint)
                if previous is not None:
                    return _public(self._load(db, cid, previous["draft_id"]))
                self._head(db, cid, rid)
            run, report, inputs = self._baseline(cid, rid)
            old = [item.model_dump(mode="json") for item in self._attachments(cid, inputs["attachment_ids"], require_available=False)]
            ids = list(inputs["attachment_ids"]) if request.log_mode == "APPEND" else []
            new = [item.model_dump(mode="json") for item in self._attachments(cid, [*ids, *request.attachment_ids])]
            effective = request.effective_problem_text or inputs["effective_problem_text"]
            # Apply the same UTF-8 bound to reconstructed source information.
            CreateDraftRequest(request_id=request.request_id, effective_problem_text=effective)
            natural_text = "\n\n".join(dict.fromkeys(item.strip() for item in
                (request.text, request.change_note) if item.strip()))
            preparing = bool((natural_text or request.diagnosis_goal.strip()) and request.effective_problem_text is None)
            did, now = self.store._id("continuation"), self.store._now()
            value = dict(conversation_id=cid, draft_id=did, parent_run_id=rid, revision=1,
                status="PREPARING" if preparing else "DRAFT", source_problem_text=inputs["effective_problem_text"],
                effective_problem_text=effective, change_note=request.change_note or request.text,
                diagnosis_goal=request.diagnosis_goal, log_mode=request.log_mode,
                attachment_ids=[item["attachment_id"] for item in new], attachments=new,
                input_changes=self._changes(inputs["effective_problem_text"], effective, old, new),
                followup_text="", confirmed_run_id=None, failure_message=None, created_at=now, updated_at=now,
                _source_attachments=old, _source_runtime=inputs.get("runtime", {}), _text=natural_text,
                _parent_case_id=report.case_id, _parent_artifact_id=report.artifact.artifact_id,
                _parent_markdown=report.markdown, _parent_sha256=report.artifact.sha256,
                _parent_version=run.get("report_version", 1), _root_run_id=run.get("root_run_id", rid))
            with self.repository.database_transaction() as db:
                self._head(db, cid, rid)
                previous = self._request(db, cid, request.request_id, fingerprint)
                if previous is not None:
                    return _public(self._load(db, cid, previous["draft_id"]))
                db.execute("INSERT INTO agent_continuation_drafts VALUES (?,?,?,?,?)",
                           (did, cid, rid, "QUEUED" if preparing else "DRAFT", _json(value)))
                self._request(db, cid, request.request_id, fingerprint, {"draft_id": did})
            self.store._notify(cid)
            return _public(value)

    def get(self, cid, draft_id, *, owner_key=None):
        with self.agent.operation_lease(cid, owner_key=owner_key), self.repository.database_read() as db:
            return _public(self._load(db, cid, draft_id))

    def update(self, cid, draft_id, request: UpdateDraftRequest, *, owner_key=None):
        self.agent._available()
        fingerprint = _hash(["update", draft_id, request.model_dump(mode="json", exclude_unset=True)])
        with self.agent.operation_lease(cid, owner_key=owner_key), self.agent._message_lock(cid):
            with self.repository.database_transaction() as db:
                value = self._load(db, cid, draft_id)
                previous = self._request(db, cid, request.request_id, fingerprint)
                if previous is not None:
                    return ContinuationDraft.model_validate(previous)
            if value["status"] == "FOLLOWUP":
                raise AgentStoreError("AGENT_FOLLOWUP_REQUIRED", "这条内容是报告追问，请发送到原报告的问答区；需要重新诊断时，请重新准备草稿。", 409)
            if value["status"] in {"PREPARING", "CONFIRMED"} or value["revision"] != request.expected_revision:
                raise AgentStoreError("AGENT_DRAFT_CHANGED", "草稿正在整理、已确认或已更新，请刷新后再试。", 409)
            if request.text.strip():
                raise AgentStoreError("VALIDATION_ERROR", "请直接编辑整理后的问题，或重新创建自然语言草稿。", 400)
            patch = request.model_dump(mode="json", exclude_unset=True, exclude={"request_id", "expected_revision", "text"})
            if patch.get("effective_problem_text", "") is None:
                patch.pop("effective_problem_text")
            if "attachment_ids" in patch:
                attachments = self._attachments(cid, patch.pop("attachment_ids"))
                patch.update(attachments=[item.model_dump(mode="json") for item in attachments],
                             attachment_ids=[item.attachment_id for item in attachments])
            value.update(patch, status="DRAFT", failure_message=None, revision=value["revision"] + 1)
            value["input_changes"] = self._changes(value["source_problem_text"], value["effective_problem_text"],
                                                   value["_source_attachments"], value["attachments"])
            with self.repository.database_transaction() as db:
                latest = self._load(db, cid, draft_id)
                previous = self._request(db, cid, request.request_id, fingerprint)
                if previous is not None:
                    return ContinuationDraft.model_validate(previous)
                if latest["revision"] != request.expected_revision or latest["status"] in {"CONFIRMED", "PREPARING"}:
                    raise AgentStoreError("AGENT_DRAFT_CHANGED", "草稿已更新，请刷新后再试。", 409)
                self._save(db, value)
                self._request(db, cid, request.request_id, fingerprint, _public(value).model_dump(mode="json"))
            return _public(value)

    def confirm(self, cid, draft_id, request: ConfirmDraftRequest, *, owner_key=None):
        self.agent._available()
        fingerprint = _hash(["confirm", draft_id, request.model_dump(mode="json")])
        with self.agent.operation_lease(cid, owner_key=owner_key), self.agent._message_lock(cid):
            with self.repository.database_transaction() as db:
                value = self._load(db, cid, draft_id)
                previous = self._request(db, cid, request.request_id, fingerprint)
                if previous is not None:
                    return ContinuationReceipt.model_validate(previous)
                if value["status"] == "CONFIRMED":
                    result = ContinuationReceipt(conversation_id=cid, parent_run_id=value["parent_run_id"],
                        run_id=value["confirmed_run_id"], request_id=request.request_id)
                    self._request(db, cid, request.request_id, fingerprint, result.model_dump(mode="json"))
                    return result
                if value["status"] == "FOLLOWUP":
                    raise AgentStoreError("AGENT_FOLLOWUP_REQUIRED", "这条内容只需追问原报告，无需创建新一轮诊断。", 409)
                if value["status"] != "DRAFT" or value["revision"] != request.expected_revision:
                    raise AgentStoreError("AGENT_DRAFT_CHANGED", "请先完成问题整理，并确认最新草稿。", 409)
            # Only after validation do we open a run; imports are the existing
            # durable Agent work, never a side effect of preview or GET.
            self._attachments(cid, value["attachment_ids"])
            with self.repository.database_transaction() as db:
                value = self._load(db, cid, draft_id)
                previous = self._request(db, cid, request.request_id, fingerprint)
                if previous is not None:
                    return ContinuationReceipt.model_validate(previous)
                if value["revision"] != request.expected_revision or value["status"] != "DRAFT":
                    raise AgentStoreError("AGENT_DRAFT_CHANGED", "草稿已更新，请刷新后再试。", 409)
                current, parent = self._head(db, cid, value["parent_run_id"])
                body = self._open_run(db, current, parent, value, request_key="continuation-" + draft_id,
                    fingerprint=_hash(["continuation", draft_id]), notice="已确认本轮信息，正在开始诊断。")
                value.update(status="CONFIRMED", confirmed_run_id=body["run_id"])
                self._save(db, value)
                result = ContinuationReceipt(conversation_id=cid, parent_run_id=parent["run_id"],
                    run_id=body["run_id"], request_id=request.request_id)
                self._request(db, cid, request.request_id, fingerprint, result.model_dump(mode="json"))
            self.store._notify(cid)
            return result

    def _open_run(self, db, current, parent, value, *, request_key, fingerprint, notice):
        """The caller commits the triggering action and this run together."""
        from problem_locator.contracts.models import ReportContinuationContext
        context = ReportContinuationContext(parent_run_id=value["parent_run_id"],
            parent_case_id=value["_parent_case_id"], parent_report_artifact_id=value["_parent_artifact_id"],
            parent_report_sha256=value["_parent_sha256"], parent_report_markdown=value["_parent_markdown"],
            parent_report_version=value["_parent_version"],
            change_note=value["change_note"].strip() or "本轮信息和日志清单已经用户确认，请结合当前材料重新核对。",
            goal=value["diagnosis_goal"].strip() or "重新诊断并说明相较上一版的结论变化。")
        body = self.store._new_run(db, current)
        body.update(parent_run_id=parent["run_id"], root_run_id=value["_root_run_id"],
            report_version=value["_parent_version"] + 1, effective_problem_text=value["effective_problem_text"],
            diagnosis_goal=value["diagnosis_goal"], change_note=value["change_note"],
            source_problem_text=value["source_problem_text"], source_attachments=value["_source_attachments"],
            input_changes=value["input_changes"], source_runtime=value["_source_runtime"],
            model_configuration_fingerprint=self.agent.model_configuration_fingerprint,
            continuation_context=context.model_dump(mode="json"), intake_pending=True)
        cid, mid = current["conversation_id"], self.store._id("message")
        message = AgentMessage(message_id=mid, request_id=request_key,
            text=value["effective_problem_text"], attachment_ids=value["attachment_ids"],
            status="QUEUED", created_at=self.store._now(), run_id=body["run_id"], notice=notice)
        event = self.store._append(db, body, "message.accepted", message.model_dump(mode="json"), "message:" + mid)
        receipt = MessageReceipt(conversation_id=cid, message_id=mid, request_id=message.request_id,
            event_id=event.sequence, run_id=body["run_id"])
        db.execute("INSERT INTO agent_messages(message_id,conversation_id,request_id,fingerprint,body,receipt,run_id,event_sequence) VALUES (?,?,?,?,?,?,?,?)",
            (mid, cid, message.request_id, fingerprint, _json(message), _json(receipt), body["run_id"], event.sequence))
        self.store._save(db, body)
        return body

    @staticmethod
    def _handoff_state(db, followups, task):
        """Reload under the same conversation lock used by stop and deletion."""
        store = followups.store
        row = store._locked_row(db, "agent_followup_tasks", "followup_id", task["followup_id"])
        if row is None or (row["conversation_id"], row["run_id"]) != (task["conversation_id"], task["run_id"]):
            return None, False
        if row["status"] == "COMPLETED":
            return None, bool(json.loads(row["body"]).get("diagnosis_run_id"))
        if row["status"] not in {"RUNNING", "CANCELLING"} or row["epoch"] != store.epoch:
            return None, False
        if row["status"] == "CANCELLING" or not store.alive(db, row["conversation_id"], row["run_id"]):
            store._transition(db, row, "CANCELLED")
            return None, False
        if followups._stop.is_set() or followups.agent._stop.is_set():
            store._transition(db, row, "INTERRUPTED", code="AGENT_FOLLOWUP_INTERRUPTED")
            return None, False
        if store.resources_expired(db, row["conversation_id"], row["run_id"]):
            raise AgentStoreError("AGENT_ATTACHMENT_NOT_READY", "本轮日志已经清理，请重新上传后继续。", 409)
        return row, False

    def handoff_followup(self, followups, task, decision):
        """Reuse diagnosis execution without creating or classifying a draft."""
        cid, rid, fid = task["conversation_id"], task["run_id"], task["followup_id"]
        with self.agent.usage_guard.acquire(cid), self.agent._message_lock(cid):
            with self.repository.database_transaction() as db:
                row, replayed = self._handoff_state(db, followups, task)
                if row is None:
                    return replayed
                self._head(db, cid, rid)
                history = followups.store._history(db, rid, row["ordinal"])
                attachment_ids = followups.attachment_ids(row, history, log_mode=decision["log_mode"])
            run, report, inputs = self._baseline(cid, rid)
            request = CreateDraftRequest(request_id="followup-diagnosis-" + fid,
                effective_problem_text=decision["effective_problem_text"], diagnosis_goal=decision["diagnosis_goal"],
                change_note=decision["reason"], log_mode=decision["log_mode"])
            if request.log_mode == "REPLACE" and not attachment_ids:
                raise AgentStoreError("AGENT_ATTACHMENT_NOT_READY", "请先上传用于替换的日志，现有日志尚未更改。", 409)
            old = [value.model_dump(mode="json") for value in self._attachments(cid, inputs["attachment_ids"], require_available=False)]
            ids = inputs["attachment_ids"] if request.log_mode == "APPEND" else []
            new = [value.model_dump(mode="json") for value in self._attachments(cid, [*ids, *attachment_ids])]
            value = dict(parent_run_id=rid, source_problem_text=inputs["effective_problem_text"],
                effective_problem_text=request.effective_problem_text, diagnosis_goal=request.diagnosis_goal,
                change_note=request.change_note, attachment_ids=[entry["attachment_id"] for entry in new],
                input_changes=self._changes(inputs["effective_problem_text"], request.effective_problem_text, old, new),
                _source_attachments=old, _source_runtime=inputs.get("runtime", {}),
                _parent_case_id=report.case_id, _parent_artifact_id=report.artifact.artifact_id,
                _parent_markdown=report.markdown, _parent_sha256=report.artifact.sha256,
                _parent_version=run.get("report_version", 1), _root_run_id=run.get("root_run_id", rid))
            with self.repository.database_transaction() as db:
                row, replayed = self._handoff_state(db, followups, task)
                if row is None:
                    return replayed
                current, parent = self._head(db, cid, rid)
                snapshot = db.execute("SELECT report_sha256 FROM agent_followup_snapshots WHERE run_id=?", (rid,)).fetchone()
                if snapshot is None or snapshot[0] != value["_parent_sha256"]:
                    raise AgentStoreError("AGENT_REPORT_CHANGED", "报告来源发生变化，请刷新后从最新报告继续。", 409)
                # The usage lease protects files during preparation. Reload
                # descriptors on this connection so deletion/status changes
                # cannot pass between the final check and the atomic handoff.
                for expected in new:
                    saved = db.execute("SELECT body FROM agent_attachments WHERE attachment_id=?",
                        (expected["attachment_id"],)).fetchone()
                    actual = None if saved is None else json.loads(saved[0])
                    if (actual is None or actual["conversation_id"] != cid
                            or actual["status"] not in {"READY", "IMPORTED"}
                            or (actual["sha256"], actual["size"]) != (expected["sha256"], expected["size"])):
                        raise AgentStoreError("AGENT_ATTACHMENT_NOT_READY", "日志已经变化或被清理，请重新上传后继续。", 409)
                body = self._open_run(db, current, parent, value, request_key=request.request_id,
                    fingerprint=_hash(["followup-diagnosis", fid]), notice="现有资料需要进一步核查，正在继续诊断。")
                followups.store._transition(db, row, "COMPLETED", diagnosis_run_id=body["run_id"],
                    answer="现有上下文还不足以可靠回答，已开始继续诊断。\n\n" + request.change_note)
            self.store._notify(cid)
            followups.store.notify()
            return True

    def versions(self, cid, rid, *, owner_key=None):
        with self.agent.operation_lease(cid, owner_key=owner_key), self.repository.database_read() as db:
            selected = self.store._load(db, cid, run_id=rid)
            root = selected.get("root_run_id", rid)
            rows = [json.loads(row[0]) for row in db.execute("SELECT body FROM agent_conversation_runs WHERE conversation_id=? ORDER BY ordinal", (cid,))]
            published = [item for item in rows if item.get("root_run_id", item["run_id"]) == root and item.get("report_available")]
            if not published:
                raise AgentStoreError("AGENT_REPORT_REQUIRED", "本问题尚未生成正式报告。", 409)
            can_continue = True
            try:
                self._head(db, cid, rid, parent=selected, runs=rows)
            except AgentStoreError:
                can_continue = False
            values = []
            for item in published:
                old, new = item.get("source_runtime", {}), item.get("report_inputs", {}).get("runtime", {})
                changes = [name for key, name in (("skill", "诊断 Skill 已更新"), ("generic_skill", "通用诊断 Skill 已更新"),
                    ("logparse", "日志解析器已更新")) if key in old and key in new and old[key] != new[key]]
                if item.get("parent_run_id"):
                    if old.get("model") is None:
                        changes.append("旧版报告未记录模型执行配置，无法比较。")
                    elif new.get("model") is None:
                        changes.append("本版报告未记录模型执行配置，无法比较。")
                    elif old["model"] != new["model"]:
                        changes.append("模型执行配置已更新")
                values.append(ReportVersion(run_id=item["run_id"], parent_run_id=item.get("parent_run_id"),
                    version=item.get("report_version", 1), created_at=item.get("completed_at", item["created_at"]),
                    input_changes=item.get("input_changes"), diagnosis_goal=item.get("diagnosis_goal", ""), runtime_changes=changes))
        if can_continue:
            view = self.agent.get_conversation(cid, include=("report",), run_id=rid, owner_key=owner_key)
            can_continue = view.result is not None and view.result.format == "markdown"
        return ReportVersions(conversation_id=cid, root_run_id=root, latest_run_id=published[-1]["run_id"],
                              versions=values, can_continue=can_continue)

    def run_once(self, conversation_id=None):
        """Run at most one preparation, independently of ordinary Agent work."""
        operational = getattr(self.agent.application, "operational_state", None)
        if (self.agent._stop.is_set() or (operational is not None and not operational.accepting)
                or not self._processing.acquire(blocking=False)):
            return False
        try:
            return self._run_once(conversation_id)
        finally:
            self._processing.release()

    def _run_once(self, conversation_id):
        with self.repository.database_read() as db:
            query = "SELECT conversation_id,draft_id FROM agent_continuation_drafts WHERE status='QUEUED'"
            params = ()
            if conversation_id is not None:
                query += " AND conversation_id=?"
                params = (conversation_id,)
            rows = db.execute(query + " ORDER BY draft_id LIMIT 1", params).fetchall()
        if not rows:
            return False
        cid, did = rows[0]
        signal = CancellationController()
        try:
            with self.agent.usage_guard.acquire(cid):
                with self.repository.database_transaction() as db:
                    value = self._load(db, cid, did)
                    row = db.execute("SELECT status FROM agent_continuation_drafts WHERE draft_id=?", (did,)).fetchone()
                    if row is None or row[0] != "QUEUED":
                        return False
                    self._save(db, value, work_status="RUNNING")
                with self.agent._run_lock:
                    self.agent._active_runs[(cid, did)] = signal
                try:
                    if self.agent._stop.is_set():
                        raise ValueError("preparation interrupted")
                    changes = value["input_changes"]
                    has_input_changes = bool(changes["added_attachments"] or changes["removed_attachments"]
                        or changes["before_problem_text"] != changes["after_problem_text"])
                    prepared = self.agent.intake_engine.prepare_continuation(
                        source_problem_text=value["source_problem_text"], text=value["_text"],
                        diagnosis_goal=value["diagnosis_goal"], previous_report_markdown=value["_parent_markdown"],
                        has_input_changes=has_input_changes, cancellation=signal, workspace_id=did)
                    if prepared["intent"] not in {"FOLLOWUP", "CONTINUE"}:
                        raise ValueError("invalid continuation intent")
                    followup = prepared["intent"] == "FOLLOWUP" and not has_input_changes
                    effective = value["source_problem_text"] if followup else prepared["effective_problem_text"]
                    followup_text = "\n\n".join(dict.fromkeys(item.strip() for item in
                        (value["_text"], value["diagnosis_goal"]) if item.strip())) if followup else ""
                    if followup:
                        from problem_locator.followup.models import FollowupRequest
                        FollowupRequest(request_id=did, text=followup_text)
                    CreateDraftRequest(request_id=did, effective_problem_text=effective)
                    with self.repository.database_transaction() as db:
                        value = self._load(db, cid, did)
                        if signal.is_cancelled() or self.agent._stop.is_set():
                            raise ValueError("preparation interrupted")
                        value.update(effective_problem_text=effective, change_note=prepared["change_note"],
                                     status="FOLLOWUP" if followup else "DRAFT", followup_text=followup_text,
                                     revision=value["revision"] + 1)
                        value["input_changes"] = self._changes(value["source_problem_text"], effective,
                            value["_source_attachments"], value["attachments"])
                        self._save(db, value)
                except Exception as error:
                    log_event("agent.continuation.prepare_failed", conversation_id=cid, error_type=type(error).__name__)
                    with self.repository.database_transaction() as db:
                        try:
                            value = self._load(db, cid, did)
                        except AgentStoreError:
                            return True
                        value.update(status="FAILED", failure_message="问题整理未完成，请直接编辑本轮信息后确认。")
                        self._save(db, value)
                finally:
                    with self.agent._run_lock:
                        self.agent._active_runs.pop((cid, did), None)
            return True
        except AgentStoreError as error:
            if error.status_code == 404:
                # A deleted conversation is no longer a candidate for work.
                with self.repository.database_transaction() as db:
                    db.execute("DELETE FROM agent_continuation_drafts WHERE draft_id=?", (did,))
                return True
            raise
