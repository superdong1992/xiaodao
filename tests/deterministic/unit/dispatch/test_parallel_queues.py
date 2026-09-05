from __future__ import annotations

import threading

from problem_locator.contracts import JobType
from problem_locator.dispatch import InProcessDispatcher


class ControlledWorker:
    def __init__(self, keys):
        self.entered = {key: threading.Event() for key in keys}
        self.release = {key: threading.Event() for key in keys}
        self.signals = {}

    def execute_one(self, key, cancellation):
        self.signals[key] = cancellation
        self.entered[key].set()
        assert self.release[key].wait(3)

    def request_shutdown(self):
        for event in self.release.values():
            event.set()


def test_two_diagnoses_overlap_and_route_does_not_wait_for_them():
    identities = {'d1': ('c1', JobType.DIAGNOSE), 'd2': ('c2', JobType.DIAGNOSE),
        'd3': ('c3', JobType.DIAGNOSE), 'r1': ('c4', JobType.ROUTE)}
    # Dispatcher receipts use public opaque Job IDs.
    identities = {f'00000000-0000-0000-0000-{i:012d}': value for i, value in enumerate(identities.values(), 1)}
    d1, d2, d3, route = identities
    worker = ControlledWorker(identities)
    dispatcher = InProcessDispatcher(worker, job_identity=identities.__getitem__)
    dispatcher.start()
    dispatcher.enable_claiming()
    try:
        for key in identities:
            dispatcher.submit(key)
        assert worker.entered[d1].wait(1)
        assert worker.entered[d2].wait(1)
        assert worker.entered[route].wait(1)
        assert not worker.entered[d3].is_set()
        assert dispatcher.cancel(d1).signalled
        assert worker.signals[d1].is_cancelled()
        assert not worker.signals[d2].is_cancelled()
        assert not worker.signals[route].is_cancelled()
        worker.release[d1].set()
        assert worker.entered[d3].wait(1)
    finally:
        assert dispatcher.shutdown(3)


def test_one_case_is_serial_across_route_and_diagnose_queues():
    route = '00000000-0000-0000-0000-000000000001'
    diagnose = '00000000-0000-0000-0000-000000000002'
    identities = {route: ('same-case', JobType.ROUTE), diagnose: ('same-case', JobType.DIAGNOSE)}
    worker = ControlledWorker(identities)
    dispatcher = InProcessDispatcher(worker, job_identity=identities.__getitem__)
    dispatcher.start()
    dispatcher.enable_claiming()
    try:
        dispatcher.submit(route)
        assert worker.entered[route].wait(1)
        dispatcher.submit(diagnose)
        assert not worker.entered[diagnose].wait(.1)
        worker.release[route].set()
        assert worker.entered[diagnose].wait(1)
        assert not worker.signals[diagnose].is_cancelled()
    finally:
        assert dispatcher.shutdown(3)
