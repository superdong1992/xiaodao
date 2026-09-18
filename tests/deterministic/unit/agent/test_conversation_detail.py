"""Unified conversation reads remain coherent, selective and model-free."""
from __future__ import annotations

import itertools
import threading
from types import SimpleNamespace

import pytest

from problem_locator.agent.models import AgentStoreError, ConversationDetail
from problem_locator.agent.service import AgentConversationService
from problem_locator.agent.store import AgentStore
from problem_locator.application.queries import ApplicationQueryService
from problem_locator.contracts import ApplicationPortError, ArtifactKind, ErrorCode
from problem_locator.operational import OperationalState
from tests.deterministic.unit.application.test_reports import (
    ARTIFACT_ID, CASE_ID, JOB_ID, OTHER_ID, generic_report, specialist_report,
)
from tests.deterministic.unit.storage.test_state_repository import _open


CHOICES = ("history", "report", "artifacts")
COMBINATIONS = [tuple(item for item, selected in zip(CHOICES, mask) if selected)
    for mask in itertools.product((False, True), repeat=3)]


def forbidden(*args, **kwargs):
    raise AssertionError("a read must not call the model or reload another Case snapshot")


@pytest.fixture
def setup(tmp_path):
    repository = _open(tmp_path)
    store = AgentStore(repository, runtime_epoch="detail-tests")
    conversation_id = store.create_conversation("detail:create").conversation_id
    aggregate, resources, expected = specialist_report()
    operational = OperationalState()
    queries = ApplicationQueryService(repository, resources, SimpleNamespace(), operational_state=operational)
    application = SimpleNamespace(operational_state=operational,
        read_conversation_delivery=queries.read_conversation_delivery, get_case=forbidden, execute=forbidden)
    service = AgentConversationService(store, application, SimpleNamespace(run=forbidden), repository.layout)
    context = SimpleNamespace(repository=repository, store=store, conversation_id=conversation_id,
        aggregate=aggregate, resources=resources, expected=expected, operational=operational, service=service)
    yield context
    repository.close()


def publish(context, aggregate=None):
    aggregate = aggregate or context.aggregate
    # Minimal result fixtures deliberately cannot replay evidence validation.
    # Publish them in the live snapshot and actual Agent SQL projection together.
    repository, store = context.repository, context.store
    with repository._lock_for(CASE_ID):
        with repository.database_transaction() as db:
            body = store._load(db, context.conversation_id)
            store._project_case(db, body, aggregate)
        repository._live[CASE_ID] = repository._base.model_copy(update={"cases": {CASE_ID: aggregate}})


@pytest.mark.parametrize("include", COMBINATIONS)
def test_all_include_combinations_and_default_have_explicit_null_semantics(setup, include):
    setup.store.submit_message(setup.conversation_id, "message:one", "订单异常，附上日志。")
    setup.store.append_progress(setup.conversation_id, "DIAGNOSE", dedupe_key="arbitrary-key")
    publish(setup)
    detail = setup.service.get_conversation(setup.conversation_id, include=tuple(reversed(include)))
    assert detail.schema_version == 3 and detail.included == list(include)
    assert detail.progress.stage == "DIAGNOSE" and detail.report_state == "READY"
    assert (detail.messages is not None) == ("history" in include)
    assert (detail.attachments is not None) == ("history" in include)
    assert (detail.result is not None) == ("report" in include)
    assert (detail.artifacts is not None) == ("artifacts" in include)
    if "history" in include:
        assert detail.messages[0].text == "订单异常，附上日志。" and detail.attachments == []
    if "report" in include:
        assert detail.result.report == setup.expected
    if "artifacts" in include:
        assert [item.artifact_id for item in detail.artifacts] == [ARTIFACT_ID]
        assert detail.artifacts[0].download_url is None
    if {"report", "artifacts"}.intersection(include):
        assert detail.source_job_id == JOB_ID and detail.case_revision == setup.aggregate.case.case_revision
    else:
        assert detail.source_job_id is None and detail.case_revision is None
    assert len(setup.resources.opened) == int("report" in include)
    assert ConversationDetail.model_validate_json(detail.model_dump_json()) == detail
    if include == CHOICES:
        assert setup.service.get_conversation(setup.conversation_id) == detail


@pytest.mark.parametrize("include", COMBINATIONS)
def test_selective_reads_do_not_load_unrequested_history_case_or_report(setup, monkeypatch, include):
    setup.store.submit_message(setup.conversation_id, "selective-message", "用于检查按需读取的历史消息。")
    publish(setup)
    loaded, sql = [], []
    original = setup.repository._load_case
    def load(case_id):
        loaded.append(case_id)
        assert not setup.repository._database_lock._is_owned()
        return original(case_id)
    monkeypatch.setattr(setup.repository, "_load_case", load)
    monkeypatch.setattr(setup.repository, "read_snapshot", forbidden)
    setup.repository._db.set_trace_callback(sql.append)
    setup.service.get_conversation(setup.conversation_id, include=include)
    assert loaded == ([CASE_ID] if {"report", "artifacts"}.intersection(include) else [])
    assert any("FROM agent_messages" in query for query in sql) == ("history" in include)
    assert any("FROM agent_attachments" in query for query in sql) == ("history" in include)
    assert len(setup.resources.opened) == int("report" in include)


def test_report_io_runs_outside_case_and_database_locks_and_capture_stays_coherent(setup, monkeypatch):
    publish(setup)
    original = setup.resources.open_read
    case_lock = setup.repository._lock_for(CASE_ID)
    def open_read(resource):
        assert not setup.repository._database_lock._is_owned()
        assert not case_lock._is_owned()
        # A concurrent archive update after capture must not leak its newer
        # status/revision into this response's report or artifact metadata.
        newer = setup.aggregate.model_copy(update={"case": setup.aggregate.case.model_copy(update={
            "case_revision": setup.aggregate.case.case_revision + 1, "archive_status": "FAILED"})})
        publish(setup, newer)
        return original(resource)
    monkeypatch.setattr(setup.resources, "open_read", open_read)
    detail = setup.service.get_conversation(setup.conversation_id, include=("report", "artifacts"))
    assert detail.case_revision == detail.result.case_revision == setup.aggregate.case.case_revision
    assert detail.archive_status == detail.result.archive_status == "PENDING"
    assert detail.source_job_id == detail.result.source_job_id == detail.artifacts[0].created_by_job_id
    assert setup.store.get_status(setup.conversation_id).archive_status == "FAILED"


def test_reader_waiting_for_case_lock_does_not_hold_database_lock(setup, monkeypatch):
    publish(setup)
    repository = setup.repository
    case_lock = repository._lock_for(CASE_ID)
    reader_waiting, completed, errors = threading.Event(), threading.Event(), []
    original = repository._lock_for
    def lock_for(case_id):
        assert not repository._database_lock._is_owned()
        reader_waiting.set()
        return original(case_id)
    monkeypatch.setattr(repository, "_lock_for", lock_for)
    def read():
        try:
            setup.service.get_conversation(setup.conversation_id, include=("artifacts",))
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()
    with case_lock:
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        assert reader_waiting.wait(2)
        acquired = repository._database_lock.acquire(timeout=2)
        assert acquired, "reader inverted Case→DB lock order"
        if acquired:
            repository._database_lock.release()
        assert not completed.is_set()
    assert completed.wait(2)
    thread.join(2)
    assert errors == []


def test_case_binding_during_initial_discovery_recaptures_without_mixing_snapshots(setup, monkeypatch):
    original, calls = setup.repository.read_case_snapshot_with, []
    def capture(case_id, callback):
        calls.append(case_id)
        if case_id is None:
            publish(setup)
        return original(case_id, callback)
    monkeypatch.setattr(setup.repository, "read_case_snapshot_with", capture)
    result = setup.service.get_conversation(setup.conversation_id)
    assert calls == [None, CASE_ID]
    assert result.case_id == CASE_ID and result.result.report == setup.expected


@pytest.mark.parametrize("format", ["COMPLETED", "PARTIAL", "INCONCLUSIVE", "generic-v1", "markdown"])
def test_unified_read_preserves_all_published_report_forms(setup, format):
    aggregate, resources, expected = (generic_report(1 if format == "generic-v1" else 2)
        if format in {"generic-v1", "markdown"} else specialist_report(format))
    setup.resources.content = resources.content
    publish(setup, aggregate)
    result = setup.service.get_conversation(setup.conversation_id)
    assert result.report_state == "READY"
    assert result.result.format == (format if format in {"generic-v1", "markdown"} else "problem-locator-diagnosis-v3")
    if format == "markdown":
        assert result.result.markdown.encode() == resources.content
    else:
        assert result.result.report == expected
    assert len(result.artifacts) == (0 if format == "generic-v1" else 1)


@pytest.mark.parametrize("archive_status", ["PENDING", "FAILED"])
def test_archive_unknown_or_failure_keeps_report_without_second_case_read(setup, archive_status):
    case = setup.aggregate.case.model_copy(update={"archive_status": archive_status})
    publish(setup, setup.aggregate.model_copy(update={"case": case}))
    setup.operational.record(case_id=CASE_ID, job_id=JOB_ID, phase="ARCHIVE_STATUS_COMMIT",
        error_code=ErrorCode.STATE_CORRUPT)
    result = setup.service.get_conversation(setup.conversation_id)
    assert result.result.report == setup.expected and len(result.artifacts) == 1
    assert (result.failure is not None) == (archive_status == "PENDING")
    if result.failure:
        assert result.failure.message == "报告已生成，但归档状态暂时无法确认。"
        assert result.result.failure == result.failure and result.failure.retryable is False
    assert setup.store.get_status(setup.conversation_id).failure is None


def test_pending_and_closed_missing_case_keep_explicit_report_states(setup):
    detail = setup.service.get_conversation(setup.conversation_id)
    assert detail.report_state == detail.result.report_state == "PENDING"
    assert detail.progress is None and detail.artifacts == [] and detail.case_id is None
    with setup.repository.database_transaction() as db:
        body = setup.store._load(db, setup.conversation_id)
        body.update(case_id=CASE_ID, case_status="INTERRUPTED", status="INTERRUPTED")
        setup.store._save(db, body)
    detail = setup.service.get_conversation(setup.conversation_id)
    assert detail.report_state == detail.result.report_state == "UNAVAILABLE"
    assert detail.result.report is None and detail.artifacts == []


def test_artifacts_only_filters_private_and_old_job_files_without_reading_bytes(setup):
    artifact = setup.aggregate.artifacts[ARTIFACT_ID]
    artifacts = dict(setup.aggregate.artifacts)
    artifacts[OTHER_ID] = artifact.model_copy(update={"artifact_id": OTHER_ID, "created_by_job_id": OTHER_ID})
    private_id = "00000000-0000-0000-0000-000000000098"
    artifacts[private_id] = artifact.model_copy(update={"artifact_id": private_id, "kind": ArtifactKind.DIAGNOSTIC_EXPORT})
    # Private artifacts aren't public Agent event data; set the live snapshot
    # after publishing the ordinary report projection.
    publish(setup)
    setup.repository._live[CASE_ID].cases[CASE_ID] = setup.aggregate.model_copy(update={"artifacts": artifacts})
    result = setup.service.get_conversation(setup.conversation_id, include=("artifacts",))
    assert [item.artifact_id for item in result.artifacts] == [ARTIFACT_ID]
    assert result.source_job_id == JOB_ID and setup.resources.opened == []


@pytest.mark.parametrize("kind", ["foreign-case", "unresolved-foreign-job", "missing-source"])
def test_artifacts_only_rejects_invalid_published_identity(setup, kind):
    aggregate = setup.aggregate
    if kind == "unresolved-foreign-job":
        aggregate, _, _ = specialist_report("INCONCLUSIVE")
    publish(setup, aggregate)
    if kind == "missing-source":
        aggregate = aggregate.model_copy(update={"case": aggregate.case.model_copy(update={"final_result": None})})
    else:
        changes = {"case_id": OTHER_ID} if kind == "foreign-case" else {"created_by_job_id": OTHER_ID}
        aggregate = aggregate.model_copy(update={"artifacts": {
            ARTIFACT_ID: aggregate.artifacts[ARTIFACT_ID].model_copy(update=changes)}})
    setup.repository._live[CASE_ID].cases[CASE_ID] = aggregate
    with pytest.raises(ApplicationPortError) as caught:
        setup.service.get_conversation(setup.conversation_id, include=("artifacts",))
    assert caught.value.error.code is ErrorCode.STATE_CORRUPT and setup.resources.opened == []


@pytest.mark.parametrize("include", [None, "history", ("history", "history"), ("unknown",), ({},)])
def test_invalid_include_rejected_before_any_storage_read(setup, monkeypatch, include):
    monkeypatch.setattr(setup.store, "read_conversation", forbidden)
    with pytest.raises(AgentStoreError) as caught:
        setup.service.get_conversation(setup.conversation_id, include=include)
    assert caught.value.code == "VALIDATION_ERROR"


def test_latest_progress_uses_type_index_for_any_dedupe_key_and_ignores_later_events(setup):
    store, conversation_id = setup.store, setup.conversation_id
    assert store.read_conversation(conversation_id).progress is None
    for stage, key in [("INTAKE", "intake:message-id"), ("ROUTE", "progress:job:route"),
                       ("LOGPARSE", "a-custom-key-without-prefix")]:
        store.append_progress(conversation_id, stage, dedupe_key=key)
        assert store.read_conversation(conversation_id).progress.stage == stage
    store.submit_message(conversation_id, "later-message", "新增说明。")
    assert store.read_conversation(conversation_id).progress.stage == "LOGPARSE"
    with setup.repository.database_read() as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT body FROM agent_events WHERE conversation_id=? "
            "AND json_extract(body, '$.type')='agent.progress' ORDER BY sequence DESC LIMIT 1",
            (conversation_id,)).fetchall()
    assert any("USING INDEX agent_events_progress" in row[3] for row in plan)
    assert not any("TEMP B-TREE" in row[3] for row in plan)


def test_reopening_old_event_history_builds_only_index_and_preserves_bytes(tmp_path):
    repository = _open(tmp_path)
    store = AgentStore(repository, runtime_epoch="first")
    conversation_id = store.create_conversation("old:create").conversation_id
    store.append_progress(conversation_id, "INTAKE", dedupe_key="intake:old")
    store.append_progress(conversation_id, "DIAGNOSE", dedupe_key="historical-custom")
    with repository.database_read() as db:
        before = db.execute("SELECT * FROM agent_events ORDER BY sequence").fetchall()
        db.execute("DROP INDEX agent_events_progress")
    repository.close()
    repository = _open(tmp_path)
    try:
        reopened = AgentStore(repository, runtime_epoch="second")
        assert reopened.read_conversation(conversation_id).progress.stage == "DIAGNOSE"
        with repository.database_read() as db:
            assert db.execute("SELECT * FROM agent_events ORDER BY sequence").fetchall() == before
            assert db.execute("SELECT name FROM sqlite_master WHERE name='agent_events_progress'").fetchone()
        reopened.append_progress(conversation_id, "REVIEW", dedupe_key="new-custom")
        assert reopened.read_conversation(conversation_id).progress.stage == "REVIEW"
    finally:
        repository.close()
