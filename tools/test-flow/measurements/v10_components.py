"""零模型性能测量；输出原始观测值，不生成 Test Flow 或 Release verdict。

在仓库根目录使用已安装依赖的 Python 执行，并提供不存在的 --output-root。
历史上传桥接代码只取自指定 Git 基线；其余均测量当前工作树。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import threading
import time
import tracemalloc
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from problem_locator.contracts import JobType
from problem_locator.dispatch import InProcessDispatcher
from problem_locator.interfaces.http_streaming import AsyncRequestBinaryStream
from problem_locator.storage.coordination import StorageCoordinationLock
from problem_locator.storage.state_repository import CaseStateRepository
from problem_locator.storage.streams import copy_binary_stream
from tests.deterministic.unit.storage.fakes import DeterministicIdGenerator, FakeFileSync, FixedClock
from tests.deterministic.unit.storage.test_case_store_v10 import _TIME, _UUID, _cancel, _create
from tests.deterministic.unit.storage.test_state_repository import _empty_mutation


def distribution(values):
    ordered = sorted(values)
    return {"samples": len(values), "median_ms": statistics.median(values),
            "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * .95))],
            "max_ms": max(values)}


async def upload(root, bridge, name, *, memory=False):
    frame = b"x" * 8192
    total = 32 * 1024 * 1024

    async def source():
        for _ in range(total // len(frame)):
            yield frame

    await asyncio.to_thread(lambda: None)
    stream = bridge(source(), loop=asyncio.get_running_loop())
    if memory:
        tracemalloc.start()
    start = time.perf_counter()
    try:
        receipt = await asyncio.to_thread(copy_binary_stream, stream, root / name,
            file_sync=FakeFileSync(), byte_limit=total)
        elapsed = time.perf_counter() - start
        peak = tracemalloc.get_traced_memory()[1] if memory else None
    finally:
        if memory:
            tracemalloc.stop()
        await stream.aclose()
    expected = hashlib.sha256()
    for _ in range(total // len(frame)):
        expected.update(frame)
    assert (receipt.size, receipt.sha256) == (total, expected.hexdigest())
    return {"bytes": total, "asgi_frame_bytes": len(frame), "duration_ms": elapsed * 1000,
            "throughput_mib_s": total / 1024 ** 2 / elapsed,
            "thread_handoffs": len(stream.read_requests), "peak_traced_bytes": peak}


def storage(root, history_count):
    repository = CaseStateRepository(root, StorageCoordinationLock(), FixedClock(_TIME),
        DeterministicIdGenerator(seed=f"measurement-{history_count}"))
    try:
        seed = _create(repository, 0)
        _cancel(repository, seed)
        raw = repository._db.execute("SELECT snapshot FROM completed_cases WHERE case_id=?", (seed,)).fetchone()[0].decode()
        rows = []
        for index in range(1, history_count):
            mapped = {value: str(uuid5(NAMESPACE_URL, f"history/{index}/{value}"))
                      for value in set(_UUID.findall(raw))}
            rows.append((mapped[seed], _UUID.sub(lambda m: mapped[m[0]], raw).encode()))
        repository._db.execute("BEGIN")
        repository._db.executemany("INSERT INTO completed_cases VALUES (?, ?)", rows)
        repository._db.execute("COMMIT")
        del rows
        active = _create(repository, 100_000)
        statements = []
        repository._db.set_trace_callback(statements.append)
        updates = []
        for _ in range(200):
            start = time.perf_counter()
            snapshot = repository.read_snapshot(active)
            case = snapshot.cases[active].case
            repository.commit(snapshot.generation, case.case_revision,
                _empty_mutation(upsert_case=case.model_copy(update={"case_revision": case.case_revision + 1})))
            updates.append((time.perf_counter() - start) * 1000)
        repository._db.set_trace_callback(None)
        terminals = []
        for index in range(50):
            case_id = _create(repository, 200_000 + index)
            start = time.perf_counter()
            _cancel(repository, case_id)
            terminals.append((time.perf_counter() - start) * 1000)
        assert not statements, statements
        assert len(repository._live) == 1
        return {"history_cases": history_count, "active_read_and_commit": distribution(updates),
                "active_update_sql_statements": len(statements), "resident_cases": len(repository._live),
                "terminal_read_and_commit_wal_full": distribution(terminals)}
    finally:
        repository.close()


def scheduling(diagnose_workers):
    keys = [str(uuid5(NAMESPACE_URL, f"schedule/{i}")) for i in range(7)]
    route = keys[-1]
    identities = {key: (key, JobType.ROUTE if key == route else JobType.DIAGNOSE) for key in keys}
    submitted, started = {}, {}
    first = threading.Event()

    class Worker:
        def execute_one(self, key, cancellation):
            started[key] = time.perf_counter()
            first.set()
            time.sleep(.01 if key == route else .1)

        def request_shutdown(self):
            pass

    dispatcher = InProcessDispatcher(Worker(), job_identity=identities.__getitem__,
        route_workers=1, diagnose_workers=diagnose_workers)
    dispatcher.start()
    dispatcher.enable_claiming()
    begin = time.perf_counter()
    try:
        for key in keys[:-1]:
            submitted[key] = time.perf_counter()
            dispatcher.submit(key)
        assert first.wait(2)
        submitted[route] = time.perf_counter()
        dispatcher.submit(route)
        assert dispatcher.wait_until_idle(5)
        elapsed = time.perf_counter() - begin
        return {"diagnose_workers": diagnose_workers, "route_workers": 1,
                "jobs": len(keys), "diagnose_work_ms": 100, "route_work_ms": 10,
                "makespan_ms": elapsed * 1000, "diagnoses_per_second": 6 / elapsed,
                "route_queue_ms": (started[route] - submitted[route]) * 1000,
                "diagnose_queue": distribution([(started[key] - submitted[key]) * 1000 for key in keys[:-1]])}
    finally:
        assert dispatcher.shutdown(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--baseline", default="e55a08c")
    args = parser.parse_args()
    os.chdir(ROOT)
    args.output_root.mkdir(parents=True, exist_ok=False)
    source = subprocess.check_output(["git", "show", f"{args.baseline}:src/problem_locator/interfaces/http_streaming.py"], cwd=ROOT)
    baseline_namespace = {"__name__": "baseline_upload_bridge"}
    exec(compile(source, f"{args.baseline}:http_streaming.py", "exec"), baseline_namespace)
    baseline = baseline_namespace["AsyncRequestBinaryStream"]
    results = {"kind": "component_measurement", "formal_test_flow": False, "model_calls": 0,
               "platform": platform.platform(), "python": sys.version, "logical_cpus": os.cpu_count(),
               "upload_baseline_git": args.baseline, "upload_baseline_sha256": hashlib.sha256(source).hexdigest(),
               "current_module_sha256": {name: hashlib.sha256((ROOT / "src/problem_locator" / name).read_bytes()).hexdigest()
                   for name in ("interfaces/http_streaming.py", "storage/state_repository.py", "dispatch/dispatcher.py", "storage/streams.py")},
               "upload": [], "storage": [], "scheduling": []}
    for sample in range(3):
        for name, bridge in (("baseline", baseline), ("v10", AsyncRequestBinaryStream)):
            result = asyncio.run(upload(args.output_root, bridge, f"upload-{name}-{sample}"))
            results["upload"].append({"version": name, "sample": sample, **result})
    for name, bridge in (("baseline", baseline), ("v10", AsyncRequestBinaryStream)):
        result = asyncio.run(upload(args.output_root, bridge, f"memory-{name}", memory=True))
        results["upload"].append({"version": name, "memory_sample": True, **result})
    for count in (1, 1000, 10000):
        results["storage"].append(storage(args.output_root / f"history-{count}", count))
    for count in (1, 2):
        for _ in range(3):
            results["scheduling"].append(scheduling(count))
    (args.output_root / "measurement.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
