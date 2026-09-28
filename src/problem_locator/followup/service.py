"""Report follow-ups with no changes to Case or the existing Agent projections."""
from __future__ import annotations

import hashlib
import threading
import time

from problem_locator.agent.models import AgentStoreError
from problem_locator.contracts import DiagnosisMode, JobStatus
from problem_locator.contracts.models import is_specialized_direct, specialized_direct_skill_name
from problem_locator.diagnostics import log_event
from problem_locator.runtime.claude_command import supports_read_search

from .models import (MAX_TEXT_BYTES, MAX_TURNS, FollowupRequest, FollowupSource,
    FollowupStopRequest, FollowupView)
from .snapshots import SnapshotWorker
from .store import FollowupStore, fail, timestamp
from .worker import FollowupWorker


class ReportFollowupService:
    def __init__(self, agent, command, *, enabled=False, snapshot_max_bytes=1024**3,
                 snapshot_total_bytes=5 * 1024**3, backend=None):
        if type(snapshot_max_bytes) is not int or type(snapshot_total_bytes) is not int or not 1 <= snapshot_max_bytes <= snapshot_total_bytes:
            raise ValueError("报告追问快照预算无效。")
        self.agent, self.enabled = agent, enabled
        self.store = FollowupStore(agent.store.repository, agent.store.clock)
        self.layout = agent.store.repository.layout
        self.snapshot_max_bytes, self.snapshot_total_bytes = snapshot_max_bytes, snapshot_total_bytes
        self.read_search_supported = supports_read_search(command)
        self.enabled_at = self.store.now()
        self._stop, self._wake = threading.Event(), threading.Event()
        self._observe_lock, self._lifecycle = threading.Lock(), threading.Lock()
        self._observe_pending = set()
        self._threads = []
        self.store.notify = self._wake.set
        self.worker = FollowupWorker(self, command, backend=backend)
        self.snapshot_worker = SnapshotWorker(self)

    def _source(self, cid, rid):
        captured = self.agent.store.read_conversation(cid, case_snapshot=True, run_id=rid)
        if captured.snapshot is None or captured.view.case_id is None:
            return None
        aggregate = captured.snapshot.cases.get(captured.view.case_id)
        if aggregate is None:
            return None
        result = aggregate.case.generic_result_v2
        if result is None:
            return None
        job = aggregate.jobs.get(result.source_job_id)
        if job is None or job.status is not JobStatus.SUCCEEDED:
            return None
        generic = job.diagnosis_mode is DiagnosisMode.GENERIC and job.generic_skill_name == result.skill_name
        direct = is_specialized_direct(job) and specialized_direct_skill_name(job) == result.skill_name
        if not generic and not direct:
            return None
        problem = job.generic_problem_text if generic else aggregate.case.raw_problem_text
        if (not isinstance(problem, str) or not problem.strip() or len(problem.encode("utf-8")) > MAX_TEXT_BYTES
                or len(result.report_markdown.encode("utf-8")) > MAX_TEXT_BYTES
                or hashlib.sha256(result.report_markdown.encode("utf-8")).hexdigest() != result.report_sha256):
            return None
        _, published, _ = self.agent.application.read_conversation_delivery(
            captured.view.case_id, captured.snapshot, report=True, artifacts=False)
        if (published is None or published.format != "markdown" or published.source_job_id != job.job_id
                or published.artifact is None or published.artifact.artifact_id != result.report_artifact_id
                or published.markdown != result.report_markdown):
            return None
        run = self.agent.store.get_run(cid, rid)
        return FollowupSource(case_id=aggregate.case.case_id, source_job_id=job.job_id,
            problem_text=problem, report_markdown=result.report_markdown, report_sha256=result.report_sha256,
            source_kind="GENERIC" if generic else "SKILL_DIRECT", has_logs=bool(job.attachment_refs),
            occurred_at=timestamp(run.get("completed_at") or result.occurred_at))

    def schedule_observe(self, conversation_id):
        if self.enabled and not self._stop.is_set():
            with self._observe_lock:
                # Notifications are hints. Do not turn them into an unbounded
                # in-memory queue when the service cannot keep up.
                if len(self._observe_pending) < 10_000:
                    self._observe_pending.add(conversation_id)
            self._wake.set()

    def observe_report(self, conversation_id, run_id):
        if not self.enabled or self._stop.is_set():
            return False
        with self.agent.usage_guard.acquire(conversation_id):
            source = self._source(conversation_id, run_id)
            if source is None:
                return False
            existing = self.store.snapshot(run_id)
            if (timestamp(source.occurred_at) < self.enabled_at
                    and (existing is None or existing["status"] == "UNAVAILABLE")):
                # Historical reports remain available for explanation. Never
                # reparse old uploads or guess that old temporary logs survived.
                return False
            return self.store.enqueue_snapshot(conversation_id, run_id, source)

    def get(self, conversation_id, run_id, owner_key=None, cursor=None, limit=50):
        with self.agent.operation_lease(conversation_id, owner_key=owner_key):
            page = self.store.page(conversation_id, run_id, owner_key, cursor=cursor, limit=limit)
            source = self._source(conversation_id, run_id) if self.enabled else None
            reason = ("DISABLED" if not self.enabled else "UNSUPPORTED" if source is None else
                "EXPIRED" if self.store.expired(source) else "BUSY" if page["active_followup"] is not None else
                "LIMIT_EXCEEDED" if page["count"] >= MAX_TURNS else None)
            snapshot = page["snapshot"]
            status = "DISABLED" if not self.enabled else "UNAVAILABLE" if snapshot is None else snapshot["status"]
            return FollowupView(conversation_id=conversation_id, run_id=run_id, can_ask=reason is None,
                reason=reason, snapshot_status=status, active_followup=page["active_followup"],
                items=page["items"], next_cursor=page["next_cursor"], last_event_id=page["last_event_id"])

    def submit(self, conversation_id, run_id, request_id, text, owner_key=None):
        request = FollowupRequest(request_id=request_id, text=text)
        with self.agent.operation_lease(conversation_id, owner_key=owner_key):
            previous = self.store.request(conversation_id, run_id, request_id, text, owner_key)
            if previous is not None:
                return previous
            if not self.enabled:
                raise fail("DISABLED", "报告追问尚未开启。")
            if self._stop.is_set():
                raise AgentStoreError("AGENT_UNAVAILABLE", "追问服务暂时不可用，请稍后重试。", 503)
            self.agent._available()
            source = self._source(conversation_id, run_id)
            if source is None:
                raise fail("UNSUPPORTED", "这份报告暂不支持追问，请新建对话。")
            return self.store.submit(conversation_id, run_id, request, source, owner_key,
                logs_supported=self.read_search_supported)

    def stop(self, conversation_id, run_id, followup_id, request_id, owner_key=None):
        request = FollowupStopRequest(request_id=request_id)
        with self.agent.operation_lease(conversation_id, owner_key=owner_key):
            receipt = self.store.stop(conversation_id, run_id, followup_id, request.request_id, owner_key)
            self.worker.cancel(conversation_id=conversation_id, followup_id=followup_id)
            return receipt

    def list_events(self, conversation_id, run_id, after_sequence=0, limit=20, owner_key=None):
        with self.agent.operation_lease(conversation_id, owner_key=owner_key):
            return self.store.events(conversation_id, run_id, owner_key, after_sequence, limit)

    def cancel_conversation(self, conversation_id):
        self.worker.cancel(conversation_id=conversation_id)
        self._wake.set()

    def start(self):
        with self._lifecycle:
            if self._threads or self._stop.is_set():
                return
            self.store.recover(enabled=self.enabled)
            if not self.enabled:
                return
            for name, worker in (("report-followup", self.worker), ("report-followup-snapshot", self.snapshot_worker)):
                thread = threading.Thread(target=self._run, args=(worker,), name=name, daemon=True)
                self._threads.append(thread)
                thread.start()

    def _run(self, worker):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                progressed = worker.run_once()
            except Exception as exc:
                log_event("agent.followup.worker_failed", error_type=type(exc).__name__)
                progressed = False
            if not progressed:
                self._wake.wait(0.5)

    def shutdown(self, timeout_seconds=30):
        self._stop.set()
        self._wake.set()
        self.worker.cancel(shutdown=True)
        deadline = time.monotonic() + max(0, timeout_seconds)
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))
        return all(not thread.is_alive() for thread in self._threads)
