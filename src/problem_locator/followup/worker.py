"""One claimed task, one isolated model invocation, one durable answer."""
from __future__ import annotations

import json
import hashlib
import threading
from pathlib import Path

from problem_locator.contracts import JOB_STDOUT_STDERR_BYTES, CancellationReason, ExecutionLogSinks, ResourceLimits
from problem_locator.contracts.limits import DEFAULT_REPORT_FOLLOWUP_TIMEOUT_SECONDS
from problem_locator.diagnostics import log_event
from problem_locator.dispatch.cancellation import CancellationController
from problem_locator.runtime.agent_backend import AgentBackend
from problem_locator.storage.atomic import require_real_directory
from problem_locator.storage.paths import ensure_no_symlink_ancestors

from .context import build_prompt, history_markdown, parse_result, validate_answer
from .models import MAX_CONTEXT_BYTES
from .snapshots import InputChanged, _row, copy_snapshot, protect_inputs, verify_inputs, write_new

FOLLOWUP_LIMITS = ResourceLimits(context_bytes=MAX_CONTEXT_BYTES,
    wall_time_seconds=DEFAULT_REPORT_FOLLOWUP_TIMEOUT_SECONDS,
    stdout_stderr_bytes=262_144, workspace_bytes=1024**3 + 16 * 1024**2)


def _record_quote_recovery(workspace, followup_id, raw, effective, offsets):
    receipt = {"schema_version": 1, "rule": "followup_answer_quotes_v1",
        "raw_sha256": hashlib.sha256(raw).hexdigest(), "raw_size_bytes": len(raw),
        "effective_sha256": hashlib.sha256(effective).hexdigest(), "effective_size_bytes": len(effective),
        "inserted_escape_offsets": list(offsets)}
    for name, content in (
        ("followup-response-original.txt", raw),
        ("followup-response-effective.txt", effective),
        ("followup-response-recovery.json", json.dumps(receipt, sort_keys=True).encode("utf-8")),
    ):
        write_new(workspace / "runtime" / name, content)
    log_event("agent.followup.answer_quotes_recovered", followup_id=followup_id,
        rule=receipt["rule"], raw_sha256=receipt["raw_sha256"],
        effective_sha256=receipt["effective_sha256"], inserted_escape_count=len(offsets))


class _DiscardSink:
    def write(self, chunk):
        pass

    def flush(self):
        pass

    def close(self):
        pass


class _Cancellation:
    def __init__(self, service, fid, signal):
        self.service, self.fid, self.signal = service, fid, signal

    @property
    def reason(self):
        if self.service._stop.is_set():
            return CancellationReason.SERVICE_SHUTDOWN
        return self.signal.reason or CancellationReason.USER_CANCEL

    def is_cancelled(self):
        return self.service._stop.is_set() or self.signal.is_cancelled() or not self.service.store.execution_allowed(self.fid)

    def wait(self, timeout_seconds):
        self.signal.wait(min(0.1, timeout_seconds) if timeout_seconds is not None else 0.1)
        return self.is_cancelled()


class FollowupWorker:
    def __init__(self, service, command, *, backend=None):
        self.service = service
        self.backend = backend if backend is not None else AgentBackend(command)
        self._processing = threading.Lock()
        self._lock = threading.Lock()
        self._active = None

    def cancel(self, *, conversation_id=None, followup_id=None, shutdown=False):
        with self._lock:
            if self._active is None:
                return
            task, signal = self._active
            if (shutdown or (conversation_id is None or task["conversation_id"] == conversation_id)
                    and (followup_id is None or task["followup_id"] == followup_id)):
                signal.cancel(CancellationReason.SERVICE_SHUTDOWN if shutdown else CancellationReason.USER_CANCEL)

    def run_once(self):
        service = self.service
        if not service.enabled or service._stop.is_set() or not self._processing.acquire(blocking=False):
            return False
        task = None
        invoked = False
        observation = service.foreground_activity.begin_scan()
        try:
            task = service.store.claim_task()
            if task is None:
                if service.store.last_claim_empty:
                    service.foreground_activity.observed_empty(observation)
                return False
            signal = CancellationController()
            with self._lock:
                self._active = (task, signal)
            cancellation = _Cancellation(service, task["followup_id"], signal)
            with service.agent.usage_guard.acquire(task["conversation_id"]):
                if cancellation.is_cancelled():
                    raise InterruptedError()
                source, history, snapshot = service.store.task_context(task)
                item = json.loads(task["body"])
                mode = item["context_mode"]
                if mode == "REPORT_AND_LOGS" and snapshot["status"] != "READY":
                    raise InputChanged("snapshot was revoked")
                diagnosis_context = service.decision_context(task, history, snapshot)
                prompt, context = build_prompt(source, history, item["text"], mode,
                    read_search=service.read_search_supported, correction=json.loads(task["correction_json"]),
                    snapshot_status=snapshot["status"], request_key=task["followup_id"],
                    diagnosis_context=diagnosis_context)
                root = service.layout.workspaces
                require_real_directory(root)
                workspace = root / task["workspace_id"]
                ensure_no_symlink_ancestors(service.layout.data_root, workspace)
                workspace.mkdir(mode=0o700)
                for name in ("inputs", "runtime", "output"):
                    (workspace / name).mkdir(mode=0o700)
                copy_preparation = (mode == "REPORT_ONLY" and source.has_log_manifest and not source.has_logs
                    and snapshot["status"] == "READY")
                copied_snapshot = mode == "REPORT_AND_LOGS" or copy_preparation
                rows = copy_snapshot(service, snapshot, workspace, cancellation) if copied_snapshot else []
                generated = {
                    "inputs/context.json": json.dumps(context, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                    "inputs/history.md": history_markdown(history).encode("utf-8"),
                }
                attempt = context["diagnosis_context"]["latest_attempt"]
                if attempt and attempt.get("user_messages_file"):
                    generated[attempt["user_messages_file"]] = json.dumps(
                        diagnosis_context["latest_attempt"]["user_messages"], ensure_ascii=False).encode("utf-8")
                if mode == "REPORT_ONLY" and not copied_snapshot:
                    generated.update({"inputs/problem.txt": source.problem_text.encode("utf-8"),
                        "inputs/report.md": source.report_markdown.encode("utf-8")})
                for relative, raw in generated.items():
                    write_new(workspace / relative, raw)
                    rows.append(_row(relative, raw))
                protect_inputs(workspace / "inputs")
                verify_inputs(workspace, rows, cancellation)
                if cancellation.is_cancelled():
                    raise InterruptedError()
                limits = FOLLOWUP_LIMITS.model_copy(update={"wall_time_seconds": service.timeout_seconds,
                    "workspace_bytes": service.snapshot_max_bytes + 16 * 1024**2})
                invoked = True
                result = self.backend.execute(prompt=prompt, workspace_root=workspace, cancellation=cancellation,
                    log_sinks=ExecutionLogSinks(stdout=_DiscardSink(), stderr=_DiscardSink(),
                        combined_limit_bytes=JOB_STDOUT_STDERR_BYTES), resource_limits=limits,
                    broker_environment=None, diagnosis_mode="GENERIC", backend_phase="REPORT_FOLLOWUP",
                    file_access="read-search" if service.read_search_supported else "none")
                if cancellation.is_cancelled():
                    raise InterruptedError()
                verify_inputs(workspace, rows, cancellation)
                decision = parse_result(result.final_result, request_key=task["followup_id"],
                    on_recovery=lambda raw, effective, offsets: _record_quote_recovery(
                        workspace, task["followup_id"], raw, effective, offsets))
                if cancellation.is_cancelled():
                    raise InterruptedError()
                if decision.action in {"ANSWER", "REQUEST_INPUT"}:
                    answer = validate_answer(decision.answer_markdown, mode)
                    service.store.finish_task(task["followup_id"], answer=answer)
                else:
                    service.continue_diagnosis(task, decision.model_dump(mode="json", exclude={"protocol", "request_key"}))
            return True
        except Exception as exc:
            if task is not None:
                try:
                    service.store.finish_task(task["followup_id"],
                        code="AGENT_FOLLOWUP_INPUT_CHANGED" if isinstance(exc, InputChanged) else "AGENT_FOLLOWUP_FAILED",
                        interrupted=service._stop.is_set())
                except Exception:
                    # RUNNING is deliberately never requeued after an uncertain
                    # model/result commit. Startup recovery marks it interrupted.
                    pass
            log_event("agent.followup.failed", model_invoked=invoked, error_type=type(exc).__name__)
            return False
        finally:
            with self._lock:
                self._active = None
            self._processing.release()
