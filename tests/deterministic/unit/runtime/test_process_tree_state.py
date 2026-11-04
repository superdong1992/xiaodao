from __future__ import annotations

from types import SimpleNamespace

import pytest

from problem_locator.runtime import process_tree as process_tree_module


class _Parent:
    def __init__(self, returncode: int | None = 0) -> None:
        self.returncode = returncode
        self.poll_calls = 0

    def poll(self) -> int | None:
        self.poll_calls += 1
        return self.returncode


class _Job:
    def __init__(self, active: int) -> None:
        self.active = active
        self.query_calls = 0
        self.close_calls = 0

    def active_processes(self) -> int:
        self.query_calls += 1
        return self.active

    def close(self) -> None:
        self.close_calls += 1

    def terminate_all(self) -> None:
        pytest.fail("状态查询不得终止进程")


def _forbid_termination(*args: object, **kwargs: object) -> None:
    pytest.fail("状态查询不得终止进程")


@pytest.mark.parametrize("group_exists", [True, False])
def test_posix_tree_query_waits_for_descendants_without_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    group_exists: bool,
) -> None:
    parent = _Parent()
    managed = process_tree_module.ManagedProcess(parent, process_group_id=777)  # type: ignore[arg-type]
    queried: list[int] = []

    def query(group_id: int) -> bool:
        queried.append(group_id)
        return group_exists

    monkeypatch.setattr(process_tree_module, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(process_tree_module, "_posix_group_exists", query)
    monkeypatch.setattr(managed, "terminate_tree", _forbid_termination)

    assert managed.is_tree_empty() is not group_exists
    assert managed.is_tree_empty() is not group_exists
    assert queried == [777, 777]
    assert managed._closed is False
    assert managed._cleanup_result is None


@pytest.mark.parametrize("active", [0, 2])
def test_windows_tree_query_keeps_job_open_until_explicit_close(
    monkeypatch: pytest.MonkeyPatch,
    active: int,
) -> None:
    job = _Job(active)
    managed = process_tree_module.ManagedProcess(_Parent(), windows_job=job)  # type: ignore[arg-type]
    monkeypatch.setattr(process_tree_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(managed, "terminate_tree", _forbid_termination)

    assert managed.is_tree_empty() is (active == 0)
    assert managed.is_tree_empty() is (active == 0)
    assert job.query_calls == 2
    assert job.close_calls == 0
    assert managed._closed is False
    assert managed._cleanup_result is None


@pytest.mark.parametrize("platform", ["posix", "nt"])
def test_tree_is_not_empty_while_parent_is_running(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    managed = process_tree_module.ManagedProcess(_Parent(None))  # type: ignore[arg-type]
    monkeypatch.setattr(process_tree_module, "os", SimpleNamespace(name=platform))
    assert managed.is_tree_empty() is False


@pytest.mark.parametrize("platform", ["posix", "nt"])
@pytest.mark.parametrize("cleanup_result", [True, False, None])
def test_closed_tree_query_uses_cached_result_without_rechecking_ownership(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    cleanup_result: bool | None,
) -> None:
    parent = _Parent()
    managed = process_tree_module.ManagedProcess(parent)  # type: ignore[arg-type]
    managed._closed = True
    managed._cleanup_result = cleanup_result
    monkeypatch.setattr(process_tree_module, "os", SimpleNamespace(name=platform))

    assert managed.is_tree_empty() is (cleanup_result is True)
    assert parent.poll_calls == 0


@pytest.mark.parametrize("platform", ["posix", "nt"])
def test_tree_query_rejects_missing_ownership(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    managed = process_tree_module.ManagedProcess(_Parent())  # type: ignore[arg-type]
    monkeypatch.setattr(process_tree_module, "os", SimpleNamespace(name=platform))
    with pytest.raises(process_tree_module.ProcessTreeError, match="not established"):
        managed.is_tree_empty()
    assert managed._closed is False


def test_posix_tree_query_error_keeps_ownership_available_for_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = process_tree_module.ManagedProcess(_Parent(), process_group_id=777)  # type: ignore[arg-type]

    def query(group_id: int) -> bool:
        raise OSError("injected query failure")

    monkeypatch.setattr(process_tree_module, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(process_tree_module, "_posix_group_exists", query)
    with pytest.raises(process_tree_module.ProcessTreeError, match="failed to query"):
        managed.is_tree_empty()
    assert managed._closed is False
    assert managed._cleanup_result is None


def test_windows_tree_query_error_does_not_close_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _Job(0)
    managed = process_tree_module.ManagedProcess(_Parent(), windows_job=job)  # type: ignore[arg-type]

    def query() -> int:
        raise process_tree_module.ProcessTreeError("injected query failure")

    monkeypatch.setattr(process_tree_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(job, "active_processes", query)
    with pytest.raises(process_tree_module.ProcessTreeError, match="query failure"):
        managed.is_tree_empty()
    assert job.close_calls == 0
    assert managed._closed is False
    assert managed._cleanup_result is None


@pytest.mark.parametrize("platform", ["posix", "nt"])
def test_empty_tree_query_then_close_releases_ownership_once(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    job = _Job(0)
    managed = process_tree_module.ManagedProcess(
        _Parent(), process_group_id=777, windows_job=job  # type: ignore[arg-type]
    )
    queried: list[int] = []

    def query(group_id: int) -> bool:
        queried.append(group_id)
        return False

    monkeypatch.setattr(process_tree_module, "os", SimpleNamespace(name=platform))
    monkeypatch.setattr(process_tree_module, "_posix_group_exists", query)
    monkeypatch.setattr(managed, "terminate_tree", _forbid_termination)

    assert managed.is_tree_empty() is True
    assert managed.close_after_exit() is True
    query_count = (len(queried), job.query_calls)
    assert managed.is_tree_empty() is True
    assert managed.close_after_exit() is True
    assert (len(queried), job.query_calls) == query_count
    assert job.close_calls == (1 if platform == "nt" else 0)
