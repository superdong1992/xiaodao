import threading
from contextlib import contextmanager
from types import SimpleNamespace

from problem_locator.bootstrap import RetentionService


def service(tmp_path, free, *, low=10, target=20, reclaimable=True):
    instance = object.__new__(RetentionService)
    instance._stop, instance._wake, instance._lock = threading.Event(), threading.Event(), threading.Lock()
    instance._capacity_targets = {}
    instance.cleaner = SimpleNamespace(_layout=SimpleNamespace(data_root=tmp_path))
    disk = {"free": free}
    calls = []
    instance.capacity = SimpleNamespace(snapshot=lambda path: SimpleNamespace(
        free=disk["free"], target=target, under_pressure=disk["free"] < low))
    instance._run_once = lambda: calls.append("normal_cleanup")
    def reclaim(*, limit):
        assert limit == 1
        calls.append("completed_run")
        if reclaimable:
            disk["free"] += 4
        return reclaimable
    instance.history = SimpleNamespace(reclaim_web_resources=reclaim)
    return instance, disk, calls


def test_pressure_reclaims_old_runs_after_regular_cleanup_and_rechecks_real_free(tmp_path):
    instance, disk, calls = service(tmp_path, 9)
    instance._reclaim_capacity()
    assert calls == ["normal_cleanup", "completed_run", "completed_run", "completed_run"]
    assert disk["free"] == 21


def test_rejected_large_write_requests_cleanup_even_above_low_watermark(tmp_path):
    instance, disk, calls = service(tmp_path, 15)
    instance.wake_reclaim(tmp_path, 25)
    assert instance._wake.is_set()
    instance._reclaim_capacity()
    assert disk["free"] == 27
    assert calls[0] == "normal_cleanup"
    assert not instance._capacity_targets


def test_no_eligible_history_stops_without_spinning_or_cancelling_active_work(tmp_path):
    instance, disk, calls = service(tmp_path, 9, reclaimable=False)
    instance._reclaim_capacity()
    assert calls == ["normal_cleanup", "completed_run"]
    assert disk["free"] == 9


def test_healthy_disk_never_reclaims_history(tmp_path):
    instance, _, calls = service(tmp_path, 30)
    instance._reclaim_capacity()
    assert calls == []


def test_temporary_target_can_disappear_during_cleanup_without_losing_request(tmp_path):
    instance, disk, calls = service(tmp_path, 15)
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    instance.wake_reclaim(temporary, 25)
    instance.wake_reclaim(tmp_path / "already-gone", 24)
    instance._run_once = lambda: (calls.append("normal_cleanup"), temporary.rmdir())
    instance._reclaim_capacity()
    assert disk["free"] == 27
    assert calls.count("normal_cleanup") == 1
    assert calls.count("completed_run") == 3


def test_pending_explicit_deletions_run_before_ttl_and_prevent_early_reclamation(tmp_path):
    instance, disk, calls = service(tmp_path, 9)
    instance._run_once = lambda: RetentionService._run_once(instance)
    @contextmanager
    def database_read():
        yield SimpleNamespace(execute=lambda _: SimpleNamespace(fetchone=lambda: (2,)))
    def delete():
        calls.append("explicit_delete")
        # The first task is busy. The next task can actually release space.
        if calls.count("explicit_delete") == 2:
            disk["free"] = 25
        return disk["free"] == 25
    instance.deletion_cleanup = SimpleNamespace(
        repository=SimpleNamespace(database_read=database_read), run_once=delete)
    instance.history.run_once = lambda: calls.append("history")
    result = SimpleNamespace(deleted=[], empty_directories_deleted=[], quarantined=[],
        failed_deletions=[], skipped=[], interrupted=False)
    instance.cleaner.run_once = lambda: (calls.append("temporary"), result)[1]
    instance._reclaim_capacity()
    assert calls == ["explicit_delete", "explicit_delete", "history", "temporary"]
    assert disk["free"] == 25
