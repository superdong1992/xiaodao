from __future__ import annotations

import codecs
import gzip
import hashlib
import io
import json
import os
import stat
import struct
import tarfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from problem_locator.contracts import Attachment, AttachmentStatus, CancellationReason, ErrorCode
from problem_locator.runtime.direct_generic_logs import (
    DirectGenericLogError, DirectGenericLogLimits, prepare_direct_generic_sources,
)
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.generic_logs import verify_generic_logs
from problem_locator.storage.capacity import StorageCapacityError
from tests.deterministic.contracts.fakes import InMemoryCancellationSignal


@pytest.fixture(autouse=True)
def writable_cleanup(tmp_path):
    yield
    for path in tmp_path.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)


def _workspace(tmp_path):
    root = tmp_path / "main"
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    metadata = inputs.stat()
    return SimpleNamespace(root=root, inputs_device=metadata.st_dev, inputs_inode=metadata.st_ino)


def _source(tmp_path, name, data, ordinal=1):
    source = tmp_path / f"source-{ordinal}"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    mime = ("application/gzip" if name.endswith((".gz", ".tgz")) else
        "application/zip" if name.endswith(".zip") else "application/x-tar" if name.endswith(".tar") else "text/plain")
    attachment = Attachment(attachment_id=f"00000000-0000-0000-0000-{ordinal:012d}",
        case_id="00000000-0000-0000-0000-000000000099", status=AttachmentStatus.READY,
        name=name, content_type=mime, declared_size=len(data), declared_sha256=digest,
        size=len(data), sha256=digest, storage_key=f"resources/source-{ordinal}",
        created_at="2026-10-20T00:00:00.000Z", updated_at="2026-10-20T00:00:00.000Z")
    return source, attachment


def _zip(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            if isinstance(name, str) and "\\" in name:
                # ZipInfo normally rewrites Windows separators when writing.
                # Preserve the malicious on-disk name for this cross-platform test.
                entry = zipfile.ZipInfo("placeholder")
                entry.filename = entry.orig_filename = name
                name = entry
            archive.writestr(name, data)
    return stream.getvalue()


def _tar(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data in entries:
            member = name if isinstance(name, tarfile.TarInfo) else tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return stream.getvalue()


def _prepare(workspace, sources, **kwargs):
    return prepare_direct_generic_sources(workspace, sources=sources,
        cancellation=kwargs.pop("cancellation", InMemoryCancellationSignal()),
        deadline=kwargs.pop("deadline", time.monotonic() + 30), **kwargs)


@pytest.mark.parametrize("kind", ["text", "zip", "tar", "tar.gz", "tgz", "gz"])
def test_reads_original_text_bytes_without_logparse(tmp_path, kind):
    text = codecs.BOM_UTF8 + "真实日志：dns resolution failed\n".encode()
    name = "server.log" if kind == "text" else "server.log.gz" if kind == "gz" else "logs." + kind
    data = (text if kind == "text" else _zip([("node/server.log", text)]) if kind == "zip" else
        _tar([("node/server.log", text)]) if kind == "tar" else
        gzip.compress(text if kind == "gz" else _tar([("node/server.log", text)])))
    source = _source(tmp_path, name, data)
    workspace = _workspace(tmp_path)
    receipt = _prepare(workspace, [source])
    parsed = json.loads(receipt)
    assert parsed["schema_version"] == 3 and parsed["preparation"] == "direct-v1"
    assert len(parsed["logs"]) == 1
    row = parsed["logs"][0]
    assert row["encoding"] == "utf-8-sig"
    assert row["source_attachment_sha256"] == hashlib.sha256(data).hexdigest()
    assert row["sha256"] == hashlib.sha256(text).hexdigest()
    target = workspace.root / row["log_path"]
    assert target.read_bytes() == text
    assert not target.stat().st_mode & 0o222
    assert b"dns resolution failed" not in receipt
    assert "task_id" not in parsed
    verify_generic_logs(workspace, receipt=receipt, parsed=parsed, cancellation=InMemoryCancellationSignal())


def test_preserves_same_names_from_multiple_sources(tmp_path):
    sources = [_source(tmp_path, "logs.zip", _zip([("server.log", text)]), ordinal)
        for ordinal, text in enumerate([b"node-a timeout\n", b"node-b disconnect\n"], 1)]
    workspace = _workspace(tmp_path)
    parsed = json.loads(_prepare(workspace, sources))
    assert [row["relative_path"] for row in parsed["logs"]] == ["server.log", "server.log"]
    assert len({row["log_path"] for row in parsed["logs"]}) == 2
    assert [row["source_attachment_id"] for row in parsed["logs"]] == [item.attachment_id for _, item in sources]
    assert [(workspace.root / row["log_path"]).read_bytes() for row in parsed["logs"]] == [
        b"node-a timeout\n", b"node-b disconnect\n"]


def test_skipped_inputs_are_reported_and_never_exposed_as_readable_logs(tmp_path):
    data = _zip([("binary.log", b"\x00\xff"), ("empty.log", b""), ("bom.log", codecs.BOM_UTF8),
        ("legacy.log", b"\xff"), ("nested.zip", _zip([("x.log", b"x")])), ("usable.jsonl", b'{"error":"E23"}\n')])
    workspace = _workspace(tmp_path)
    parsed = json.loads(_prepare(workspace, [_source(tmp_path, "logs.zip", data)]))
    assert [row["relative_path"] for row in parsed["logs"]] == ["usable.jsonl"]
    assert {row["relative_path"]: row["reason"] for row in parsed["skipped"]} == {
        "binary.log": "NON_TEXT", "empty.log": "EMPTY_FILE", "bom.log": "EMPTY_FILE",
        "legacy.log": "UNSUPPORTED_ENCODING", "nested.zip": "NESTED_ARCHIVE"}
    assert parsed["coverage"]["complete"] is False
    assert parsed["coverage"]["scope"] == "INPUT_PREPARATION"
    assert parsed["coverage"]["empty_reason"] is None
    assert len(list((workspace.root / "inputs/generic-logs").iterdir())) == 1


@pytest.mark.parametrize("kind,with_directory", [("zip", False), ("zip", True), ("tar", False), ("tar", True)])
def test_empty_or_directory_only_archives_explain_why_no_logs_are_available(tmp_path, kind, with_directory):
    directory = tarfile.TarInfo("folder")
    directory.type = tarfile.DIRTYPE
    data = (_zip([("folder/", b"")] if with_directory else []) if kind == "zip" else
        _tar([(directory, b"")] if with_directory else []))
    parsed = json.loads(_prepare(_workspace(tmp_path), [_source(tmp_path, "logs." + kind, data)]))
    assert parsed["logs"] == [] and parsed["skipped"] == []
    assert parsed["coverage"]["empty_reason"] == "NO_FILES"
    assert parsed["coverage"]["scope"] == "INPUT_PREPARATION"


def test_all_skipped_files_explain_that_there_is_no_readable_text(tmp_path):
    data = _zip([("binary.log", b"\x00\xff"), ("empty.log", b"")])
    parsed = json.loads(_prepare(_workspace(tmp_path), [_source(tmp_path, "logs.zip", data)]))
    assert parsed["logs"] == []
    assert parsed["coverage"]["empty_reason"] == "NO_READABLE_TEXT"
    assert parsed["coverage"]["complete"] is False
    assert {entry["reason"] for entry in parsed["skipped"]} == {"NON_TEXT", "EMPTY_FILE"}


def test_zip_root_directory_is_a_valid_empty_archive_entry(tmp_path):
    parsed = json.loads(_prepare(_workspace(tmp_path), [_source(tmp_path, "logs.zip", _zip([("./", b"")]))]))
    assert parsed["logs"] == []
    assert parsed["coverage"]["empty_reason"] == "NO_FILES"


def test_zip_directory_cannot_hide_unverified_payload(tmp_path):
    with pytest.raises(DirectGenericLogError) as raised:
        _prepare(_workspace(tmp_path), [_source(tmp_path, "logs.zip", _zip([("dir/", b"hidden bytes")]))])
    assert raised.value.reason_code == "ARCHIVE_INVALID"


@pytest.mark.parametrize("name", ["../outside.log", "/absolute.log", "C:/drive.log", r"dir\escape.log",
    "a/../../bad.log", "a//bad.log"])
@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_rejects_unsafe_member_paths(tmp_path, name, kind):
    data = (_zip if kind == "zip" else _tar)([(name, b"bad")])
    workspace = _workspace(tmp_path)
    with pytest.raises(DirectGenericLogError, match="路径") as raised:
        _prepare(workspace, [_source(tmp_path, "logs." + kind, data)])
    assert raised.value.reason_code == "UNSAFE_ARCHIVE_PATH"
    assert not (workspace.root / "inputs/generic_logs.json").exists()
    assert not (tmp_path / "outside.log").exists()


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_rejects_duplicate_paths_in_one_source(tmp_path, kind):
    data = (_zip if kind == "zip" else _tar)([("same.log", b"first"), ("./same.log", b"second")])
    with pytest.raises(DirectGenericLogError) as raised:
        _prepare(_workspace(tmp_path), [_source(tmp_path, "logs." + kind, data)])
    assert raised.value.reason_code == "ARCHIVE_PATH_CONFLICT"


@pytest.mark.parametrize("entry_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE])
def test_rejects_tar_links_and_special_files(tmp_path, entry_type):
    member = tarfile.TarInfo("unsafe")
    member.type, member.linkname = entry_type, "../secret"
    with pytest.raises(DirectGenericLogError) as raised:
        _prepare(_workspace(tmp_path), [_source(tmp_path, "logs.tar", _tar([(member, b"")]))])
    assert raised.value.reason_code == "UNSAFE_ARCHIVE_ENTRY"


def test_rejects_zip_symlink(tmp_path):
    member = zipfile.ZipInfo("link.log")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(DirectGenericLogError) as raised:
        _prepare(_workspace(tmp_path), [_source(tmp_path, "logs.zip", _zip([(member, b"../secret")]))])
    assert raised.value.reason_code == "UNSAFE_ARCHIVE_ENTRY"


def test_rejects_encrypted_zip_before_member_read(tmp_path):
    data = bytearray(_zip([("secret.log", b"secret")]))
    local, central = data.index(b"PK\x03\x04"), data.index(b"PK\x01\x02")
    struct.pack_into("<H", data, local + 6, 1)
    struct.pack_into("<H", data, central + 8, 1)
    with pytest.raises(DirectGenericLogError) as raised:
        _prepare(_workspace(tmp_path), [_source(tmp_path, "logs.zip", bytes(data))])
    assert raised.value.reason_code == "ENCRYPTED_ARCHIVE"


@pytest.mark.parametrize("name,data", [("logs.zip", b"broken"), ("logs.tar", b"broken"),
    ("logs.tar", _tar([("x.log", b"error=timeout")])[:1024]),
    ("logs.gz", gzip.compress(b"error=timeout")[:-3]),
    ("logs.tar.gz", gzip.compress(_tar([("x.log", b"error=timeout")]))[:-3])])
def test_rejects_corrupt_or_truncated_container(tmp_path, name, data):
    with pytest.raises(DirectGenericLogError) as raised:
        _prepare(_workspace(tmp_path), [_source(tmp_path, name, data)])
    assert raised.value.reason_code == "ARCHIVE_INVALID"


@pytest.mark.parametrize("limits,entries,reason", [
    (DirectGenericLogLimits(max_expanded_bytes=9), [("a.log", b"12345"), ("b.log", b"12345")], "EXPANDED_BYTES_LIMIT"),
    (DirectGenericLogLimits(max_readable_bytes=9), [("a.log", b"12345"), ("b.log", b"12345")], "READABLE_BYTES_LIMIT"),
    (DirectGenericLogLimits(max_entries=1), [("a.log", b"1"), ("b.log", b"2")], "ENTRY_COUNT_LIMIT"),
    (DirectGenericLogLimits(max_depth=1), [("dir/a.log", b"1")], "PATH_DEPTH_LIMIT"),
    (DirectGenericLogLimits(max_manifest_bytes=10), [("a.log", b"1")], "MANIFEST_BYTES_LIMIT"),
])
def test_enforces_aggregate_budgets(tmp_path, limits, entries, reason):
    with pytest.raises(DirectGenericLogError) as raised:
        _prepare(_workspace(tmp_path), [_source(tmp_path, "logs.zip", _zip(entries))], limits=limits)
    assert raised.value.reason_code == reason


def test_budgets_apply_to_skipped_binary_and_all_sources(tmp_path):
    sources = [_source(tmp_path, "logs.zip", _zip([("binary.log", b"\x00" * 8)]), 1),
        _source(tmp_path, "server.log", b"error", 2)]
    with pytest.raises(DirectGenericLogError) as raised:
        _prepare(_workspace(tmp_path), sources, limits=DirectGenericLogLimits(max_expanded_bytes=10))
    assert raised.value.reason_code == "EXPANDED_BYTES_LIMIT"


@pytest.mark.parametrize("kind", ["zip", "gz"])
def test_rejects_extreme_compression_ratio(tmp_path, kind):
    text = b"x" * (2 * 1024 * 1024)
    data = _zip([("server.log", text)]) if kind == "zip" else gzip.compress(text)
    with pytest.raises(DirectGenericLogError) as raised:
        _prepare(_workspace(tmp_path), [_source(tmp_path, "logs." + kind, data)],
            limits=DirectGenericLogLimits(max_compression_ratio=10))
    assert raised.value.reason_code == "COMPRESSION_RATIO_LIMIT"


def test_archive_directory_entry_count_is_checked_before_zip_metadata_allocation(tmp_path, monkeypatch):
    data = _zip([("a.log", b"a"), ("b.log", b"b")])
    def forbidden(*args, **kwargs):
        pytest.fail("oversized central directory must fail before ZipFile")
    monkeypatch.setattr(zipfile, "ZipFile", forbidden)
    with pytest.raises(DirectGenericLogError) as raised:
        _prepare(_workspace(tmp_path), [_source(tmp_path, "logs.zip", data)],
            limits=DirectGenericLogLimits(max_entries=1))
    assert raised.value.reason_code == "ENTRY_COUNT_LIMIT"


def test_zip_directory_count_cannot_hide_entries_from_preallocation_limit(tmp_path, monkeypatch):
    data = bytearray(_zip([("a.log", b"a"), ("b.log", b"b")]))
    end = data.index(b"PK\x05\x06")
    struct.pack_into("<2H", data, end + 8, 1, 1)
    monkeypatch.setattr(zipfile, "ZipFile", lambda *args, **kwargs: pytest.fail("directory must be checked first"))
    with pytest.raises(DirectGenericLogError) as raised:
        _prepare(_workspace(tmp_path), [_source(tmp_path, "logs.zip", bytes(data))],
            limits=DirectGenericLogLimits(max_entries=1))
    assert raised.value.reason_code == "ENTRY_COUNT_LIMIT"


def test_tar_pax_metadata_limit_precedes_materialization(tmp_path):
    member = tarfile.TarInfo("pax")
    member.type, member.size = tarfile.XHDTYPE, 2 * 1024 * 1024
    data = member.tobuf() + b"\x00" * 1024
    with pytest.raises(DirectGenericLogError) as raised:
        _prepare(_workspace(tmp_path), [_source(tmp_path, "logs.tar", data)])
    assert raised.value.reason_code == "ARCHIVE_METADATA_LIMIT"


def test_rejects_concatenated_tar_instead_of_claiming_all_files_are_available(tmp_path):
    data = _tar([("visible.log", b"first")]) + _tar([("hidden.log", b"second")])
    with pytest.raises(DirectGenericLogError) as raised:
        _prepare(_workspace(tmp_path), [_source(tmp_path, "logs.tar", data)])
    assert raised.value.reason_code == "ARCHIVE_INVALID"


def test_rejects_excessive_chained_tar_metadata_before_recursive_processing(tmp_path):
    header = tarfile.TarInfo("pax")
    header.type, header.size = tarfile.XHDTYPE, 0
    data = header.tobuf() * 40 + _tar([("server.log", b"x")])
    with pytest.raises(DirectGenericLogError) as raised:
        _prepare(_workspace(tmp_path), [_source(tmp_path, "logs.tar", data)])
    assert raised.value.reason_code == "ARCHIVE_METADATA_LIMIT"


def test_large_log_is_streamed_in_bounded_chunks_and_remains_outside_manifest(tmp_path, monkeypatch):
    data = b"request timeout\n" * 180_000
    source = _source(tmp_path, "server.log", data)
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("log must not be buffered"))
    parsed = json.loads(_prepare(workspace, [source]))
    assert parsed["logs"][0]["size"] == len(data)
    assert len(json.dumps(parsed)) < 2048
    with (workspace.root / parsed["logs"][0]["log_path"]).open("rb") as stream:
        assert stream.read(16) == data[:16]


@pytest.mark.parametrize("cause", ["cancel", "deadline"])
def test_honors_cancellation_and_deadline_before_source_read(tmp_path, cause):
    kwargs = ({"cancellation": InMemoryCancellationSignal(CancellationReason.USER_CANCEL)}
        if cause == "cancel" else {"deadline": time.monotonic() - 1})
    with pytest.raises(RuntimeExecutionError) as raised:
        _prepare(_workspace(tmp_path), [_source(tmp_path, "server.log", b"x")], **kwargs)
    assert raised.value.failure.code == (ErrorCode.BACKEND_CANCELLED if cause == "cancel" else ErrorCode.BACKEND_TIMEOUT)


def test_cancellation_during_streaming_does_not_publish_manifest(tmp_path, monkeypatch):
    from problem_locator.runtime import direct_generic_logs as module
    signal = InMemoryCancellationSignal()
    original = module._Budget.expand
    def cancel_after_first_chunk(budget, amount):
        original(budget, amount)
        signal.cancel(CancellationReason.USER_CANCEL)
    monkeypatch.setattr(module._Budget, "expand", cancel_after_first_chunk)
    workspace = _workspace(tmp_path)
    with pytest.raises(RuntimeExecutionError) as raised:
        _prepare(workspace, [_source(tmp_path, "server.log", b"x" * (2 * 1024 * 1024))], cancellation=signal)
    assert raised.value.failure.code == ErrorCode.BACKEND_CANCELLED
    assert not (workspace.root / "inputs/generic_logs.json").exists()


def test_detects_source_content_drift(tmp_path):
    path, attachment = _source(tmp_path, "server.log", b"original")
    path.write_bytes(b"modified")
    with pytest.raises(DirectGenericLogError) as raised:
        _prepare(_workspace(tmp_path), [(path, attachment)])
    assert raised.value.reason_code == "SOURCE_CHANGED"


@pytest.mark.parametrize("target", ["manifest", "log"])
def test_direct_log_receipt_detects_tampering_after_preparation(tmp_path, target):
    workspace = _workspace(tmp_path)
    receipt = _prepare(workspace, [_source(tmp_path, "server.log", b"original")])
    parsed = json.loads(receipt)
    path = workspace.root / ("inputs/generic_logs.json" if target == "manifest" else parsed["logs"][0]["log_path"])
    path.chmod(0o644)
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError):
        verify_generic_logs(workspace, receipt=receipt, parsed=parsed, cancellation=InMemoryCancellationSignal())


def test_disk_pressure_fails_before_destination_bytes_are_written(tmp_path):
    class Pressure:
        def try_reserve(self, *args):
            raise StorageCapacityError()
    workspace = _workspace(tmp_path)
    with pytest.raises(RuntimeExecutionError) as raised:
        _prepare(workspace, [_source(tmp_path, "server.log", b"error")], capacity=Pressure())
    assert raised.value.failure.code == ErrorCode.WORKSPACE_PREPARE_FAILED
    assert list((workspace.root / "inputs/generic-logs").iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="requires unprivileged Unix hard links")
def test_rejects_hard_linked_source(tmp_path):
    source = _source(tmp_path, "server.log", b"error")
    os.link(source[0], tmp_path / "other-name")
    with pytest.raises(DirectGenericLogError) as raised:
        _prepare(_workspace(tmp_path), [source])
    assert raised.value.reason_code == "SOURCE_CHANGED"
