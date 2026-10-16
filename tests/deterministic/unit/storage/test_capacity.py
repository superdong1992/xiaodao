"""Storage admission leaves text untouched and rejects large writes before I/O."""
from __future__ import annotations

import errno
import hashlib
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from problem_locator.agent.models import AgentStoreError
from problem_locator.agent.uploads import ConversationUploads
from problem_locator.contracts import ApplicationPortError, ErrorCode
from problem_locator.followup.snapshots import copy_snapshot
from problem_locator.integrations.result_archive import write_result_archive_file
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.generic_logs import freeze_generic_logs
from problem_locator.storage.capacity import DiskCapacityGuard, StorageCapacityError, GIB
from problem_locator.storage.resource_files import FormalResourceReader
from tests.deterministic.contracts.fakes import InMemoryCancellationSignal
from tests.deterministic.unit.runtime.test_generic_logs import _files
from tests.deterministic.unit.storage.test_resource_files import (
    _layout, _formal_file, JOB_ID, RESOURCE_ID, DurableRecordingFileSync,
)
from tests.deterministic.unit.storage.test_resource_store import Harness, RecordingStream, ATTACHMENT_ID


def guard(free, *, total=100 * GIB, wake=None):
    return DiskCapacityGuard(disk_usage=lambda _: SimpleNamespace(total=total, free=free), wake_reclaim=wake)


def test_watermarks_use_larger_absolute_or_fractional_value(tmp_path):
    observed = guard(9 * GIB).snapshot(tmp_path)
    assert (observed.low, observed.target, observed.under_pressure) == (10 * GIB, 20 * GIB, True)
    observed = guard(150 * GIB, total=1000 * GIB).snapshot(tmp_path)
    assert (observed.low, observed.target, observed.under_pressure) == (100 * GIB, 150 * GIB, False)


def test_large_integer_reservation_never_allocates_the_payload(tmp_path):
    capacity = guard(20 * GIB)
    with capacity.try_reserve(tmp_path / "future" / "file", 5 * GIB):
        observed = capacity.snapshot(tmp_path)
        assert observed.reserved == 5 * GIB
        assert observed.effective_free == 15 * GIB
    assert capacity.snapshot(tmp_path).reserved == 0


def test_rejected_write_requests_target_even_when_disk_is_above_low_water(tmp_path):
    wakes = []
    capacity = guard(12 * GIB, wake=lambda *args: wakes.append(args))
    assert not capacity.snapshot(tmp_path).under_pressure
    with pytest.raises(StorageCapacityError) as caught:
        with capacity.try_reserve(tmp_path / "file", 15 * GIB):
            pytest.fail("insufficient capacity must be rejected")
    assert caught.value.retryable and caught.value.status_code == 503
    assert wakes == [(tmp_path, 25 * GIB)]
    assert capacity.snapshot(tmp_path).reserved == 0


def test_concurrent_writers_cannot_both_spend_same_free_bytes(tmp_path):
    capacity = guard(15 * GIB)
    entered, release = threading.Event(), threading.Event()

    def first():
        with capacity.try_reserve(tmp_path / "one", 4 * GIB):
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(first)
        assert entered.wait(5)
        try:
            with pytest.raises(StorageCapacityError):
                with capacity.try_reserve(tmp_path / "two", 4 * GIB):
                    pytest.fail("concurrent reservation overspent the disk")
        finally:
            release.set()
        pending.result()
    with capacity.try_reserve(tmp_path / "two", 4 * GIB):
        pass


def test_reservation_released_on_failure_and_disk_full_wakes_cleanup(tmp_path):
    wakes = []
    capacity = guard(20 * GIB, wake=lambda *args: wakes.append(args))
    with pytest.raises(StorageCapacityError):
        with capacity.try_reserve(tmp_path, GIB):
            raise OSError(errno.ENOSPC, "full")
    assert capacity.snapshot(tmp_path).reserved == 0
    assert wakes[0][1] == 20 * GIB
    with pytest.raises(RuntimeError):
        with capacity.try_reserve(tmp_path, GIB):
            raise RuntimeError("cancelled")
    assert capacity.snapshot(tmp_path).reserved == 0


def test_wakeup_can_read_guard_without_deadlock(tmp_path):
    capacity = guard(0)
    observations = []
    capacity.set_wake_reclaim(lambda path, required: observations.append(capacity.snapshot(path)))
    with pytest.raises(StorageCapacityError):
        with capacity.try_reserve(tmp_path, 1):
            pass
    assert observations[0].under_pressure


def test_capacity_probe_failure_rejects_without_creating_destination(tmp_path):
    def broken(_):
        raise OSError("volume unavailable")
    capacity = DiskCapacityGuard(disk_usage=broken)
    with pytest.raises(StorageCapacityError):
        with capacity.try_reserve(tmp_path / "file", 1):
            pytest.fail("unknown disk must not admit a write")
    assert not (tmp_path / "file").exists()


def test_nested_empty_file_reservations_release_safely(tmp_path):
    capacity = guard(20 * GIB)
    with capacity.try_reserve(tmp_path, 0):
        with capacity.try_reserve(tmp_path, 0):
            pass
    assert capacity.snapshot(tmp_path).reserved == 0


def test_reservations_on_distinct_volumes_do_not_consume_each_other(tmp_path, monkeypatch):
    capacity = guard(15 * GIB)
    volumes = {"one": SimpleNamespace(stat=lambda: SimpleNamespace(st_dev=1)),
        "two": SimpleNamespace(stat=lambda: SimpleNamespace(st_dev=2))}
    monkeypatch.setattr(capacity, "_existing_parent", lambda destination: volumes[destination.name])
    with capacity.try_reserve(tmp_path / "one", 4 * GIB):
        with capacity.try_reserve(tmp_path / "two", 4 * GIB):
            assert capacity.snapshot(tmp_path / "one").reserved == 4 * GIB
            assert capacity.snapshot(tmp_path / "two").reserved == 4 * GIB


class UploadStore:
    def __init__(self, record):
        self.record = record
    def get_attachment(self, _):
        return self.record
    def reserve_attachment(self, *args):
        return self.record
    def require_owner(self, *args):
        pass
    def set_attachment_status(self, _, status):
        self.record.status = status
    def complete_attachment(self, _, storage_path):
        self.record.status = "READY"
        return self.record


def uploads(tmp_path, capacity):
    layout = _layout(tmp_path)
    content = b"log contents"
    record = SimpleNamespace(attachment_id=ATTACHMENT_ID, conversation_id=JOB_ID, status="RESERVED",
        content_type="application/zip", size=len(content), sha256=hashlib.sha256(content).hexdigest())
    store = UploadStore(record)
    return ConversationUploads(store, None, layout, capacity=capacity), store, content


def test_web_upload_rejection_does_not_read_or_create_payload_and_can_retry(tmp_path):
    upload, store, content = uploads(tmp_path, guard(0))
    stream = io.BytesIO(content)
    record = store.record
    args = (record.attachment_id, record.attachment_id, record.content_type, record.size, record.sha256)
    with pytest.raises(AgentStoreError) as caught:
        upload.upload(*args, stream)
    assert caught.value.code == "AGENT_STORAGE_PRESSURE"
    assert caught.value.retryable and stream.tell() == 0
    assert not (upload.layout.conversation_uploads / ATTACHMENT_ID / "payload").exists()
    upload.capacity = guard(20 * GIB)
    assert upload.upload(*args, io.BytesIO(content)).status == "READY"


def test_completed_upload_replay_does_not_require_more_capacity(tmp_path):
    upload, store, content = uploads(tmp_path, guard(20 * GIB))
    record = store.record
    args = (record.attachment_id, record.attachment_id, record.content_type, record.size, record.sha256)
    upload.upload(*args, io.BytesIO(content))
    upload.capacity = guard(0)
    assert upload.upload(*args, io.BytesIO(content)).status == "READY"


def test_expired_upload_cannot_be_replayed_prepared_or_imported(tmp_path):
    upload, store, content = uploads(tmp_path, guard(20 * GIB))
    record = store.record
    record.status = "EXPIRED"
    operations = [
        lambda: upload.prepare(JOB_ID, "request", "logs.zip", record.content_type, record.size, record.sha256),
        lambda: upload.upload(ATTACHMENT_ID, ATTACHMENT_ID, record.content_type, record.size,
            record.sha256, io.BytesIO(content)),
        lambda: upload.import_into_case(JOB_ID, RESOURCE_ID, ATTACHMENT_ID, None, run_id=JOB_ID),
    ]
    for operation in operations:
        with pytest.raises(AgentStoreError) as caught:
            operation()
        assert caught.value.code == "AGENT_RESOURCE_EXPIRED"
        assert caught.value.status_code == 410


def test_case_upload_rejects_before_consuming_stream(tmp_path):
    harness = Harness(tmp_path)
    harness.store.capacity = guard(0)
    stream = RecordingStream(b"log")
    with harness.attachments.acquire(ATTACHMENT_ID) as lease:
        with pytest.raises(ApplicationPortError) as caught:
            harness.store.stage_attachment(ATTACHMENT_ID, lease, stream, expected_size=3)
    assert caught.value.error.code == ErrorCode.UPLOAD_INCOMPLETE
    assert caught.value.error.retryable and stream.requests == []


def test_workspace_copy_rejects_without_creating_file_or_modifying_source(tmp_path):
    layout = _layout(tmp_path)
    source, reference = _formal_file(layout, b"original log", read_only=True)
    destination = layout.workspaces / JOB_ID / "inputs" / "evidence" / RESOURCE_ID / "payload"
    reader = FormalResourceReader(layout, DurableRecordingFileSync(), capacity=guard(0))
    with pytest.raises(StorageCapacityError):
        reader.materialize(reference, destination)
    assert source.read_bytes() == b"original log"
    assert not destination.exists()


def test_sparse_multi_gib_workspace_copy_reserves_logical_size_before_reading(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    source, reference = _formal_file(layout, b"")
    if not hasattr(source.stat(), "st_blocks"):
        pytest.skip("当前平台不提供稀疏文件块数；需在 Linux 上验证真实稀疏大文件边界。")
    # Probe support with a bounded hole before extending to multiple GiB.
    with source.open("r+b") as stream:
        stream.truncate(1024 * 1024)
    if source.stat().st_blocks * 512 >= source.stat().st_size:
        pytest.skip("当前文件系统不支持稀疏文件。")
    logical_size = 2 * GIB + 1
    with source.open("r+b") as stream:
        stream.truncate(logical_size)
    assert source.stat().st_blocks * 512 < 1024 * 1024
    source.chmod(0o444)
    reference = reference.model_copy(update={"size": logical_size})

    def forbidden(*args, **kwargs):
        pytest.fail("磁盘准入失败前不应读取、哈希或复制稀疏日志内容。")

    monkeypatch.setattr("problem_locator.storage.resource_files.FileBinaryStream", forbidden)
    monkeypatch.setattr("problem_locator.storage.resource_files.hash_file", forbidden)
    monkeypatch.setattr("problem_locator.storage.resource_files.copy_binary_stream", forbidden)
    # Allocated blocks fit easily; the logical copy exceeds free-minus-low by
    # exactly one byte. No GiB payload is allocated, read, or copied by the test.
    capacity = guard(12 * GIB)
    destination = layout.workspaces / JOB_ID / "inputs" / "evidence" / RESOURCE_ID / "payload"
    reader = FormalResourceReader(layout, DurableRecordingFileSync(), capacity=capacity)
    with pytest.raises(StorageCapacityError):
        reader.materialize(reference, destination)
    assert not destination.exists() and list(destination.parent.iterdir()) == []
    assert source.stat().st_size == logical_size
    assert source.stat().st_blocks * 512 < 1024 * 1024
    assert capacity.snapshot(destination).reserved == 0


def test_generic_log_copy_reports_retryable_pressure(tmp_path):
    workspace, root, source, parsed = _files(tmp_path)
    workspace.capacity = guard(0)
    with pytest.raises(RuntimeExecutionError) as caught:
        freeze_generic_logs(workspace, controlled_root=root, parsed=parsed,
            cancellation=InMemoryCancellationSignal())
    assert caught.value.failure.code == ErrorCode.WORKSPACE_PREPARE_FAILED
    assert caught.value.failure.retryable
    assert source.exists()
    assert not (workspace.root / "inputs/generic-logs/log-000001.log").exists()


def test_followup_copy_rejects_before_copying_logs(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    root = layout.jobs / JOB_ID / "followup-inputs"
    (root / "inputs").mkdir(parents=True)
    content = b"followup log"
    (root / "inputs/log.log").write_bytes(content)
    row = {"path": "inputs/log.log", "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    manifest = {"run_id": JOB_ID, "source_job_id": JOB_ID, "report_sha256": "a" * 64, "files": [row]}
    raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    (root / "manifest.json").write_text(raw, encoding="utf-8", newline="")
    snapshot = dict(manifest_json=raw, source_job_id=JOB_ID, run_id=JOB_ID, report_sha256="a" * 64)
    service = SimpleNamespace(layout=layout, agent=SimpleNamespace(uploads=SimpleNamespace(capacity=guard(0))))
    # This admission test supplies an already verified manifest. Linux tree
    # security has its own tests; Windows DirEntry does not report link counts.
    monkeypatch.setattr("problem_locator.followup.snapshots._input_files", lambda _: {"inputs/log.log"})
    destination = layout.workspaces / RESOURCE_ID
    destination.mkdir()
    with pytest.raises(StorageCapacityError):
        copy_snapshot(service, snapshot, destination, InMemoryCancellationSignal())
    assert not (destination / "inputs/log.log").exists()
    assert (root / "inputs/log.log").read_bytes() == content


def test_zip_capacity_rejected_before_destination_open(tmp_path):
    plan = SimpleNamespace(logs=[], result_text="报告", manifest_json="{}")
    destination = tmp_path / "result.zip"
    with pytest.raises(StorageCapacityError):
        write_result_archive_file(destination, plan=plan, source_paths=[], capacity=guard(0))
    assert not destination.exists()
