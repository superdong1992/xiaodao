from __future__ import annotations

import hashlib
import inspect
import json
import threading
import uuid
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from problem_locator.contracts import SCHEMA_MODELS
from problem_locator.contracts import ports
from problem_locator.contracts.enums import CancellationReason, ErrorCode
from problem_locator.contracts.errors import ApplicationPortError
from problem_locator.contracts.limits import JOB_STDOUT_STDERR_BYTES
from problem_locator.contracts.models import DiagnosisState
from problem_locator.contracts.serialization import canonical_json_bytes

from tests.contracts import fakes
from tests.contracts._support import FIXTURE_ROOT, load_json


PORT_FAKE_FACTORIES = {
    ports.ApplicationCommandPort: fakes.RecordingApplicationCommand,
    ports.ApplicationQueryPort: fakes.StubApplicationQuery,
    ports.AssetCatalogPort: fakes.FakeAssetCatalog,
    ports.AttachmentUploadGuard: fakes.InMemoryAttachmentUploadGuard,
    ports.BinaryStream: fakes.InMemoryBinaryStream,
    ports.CancellationSignal: fakes.InMemoryCancellationSignal,
    ports.Clock: fakes.FakeClock,
    ports.ContextSnapshotProjector: fakes.PureContextSnapshotProjector,
    ports.Coordinator: fakes.ScriptedCoordinator,
    ports.Dispatcher: fakes.RecordingDispatcher,
    ports.ExecutionRecordStore: fakes.InMemoryExecutionRecordStore,
    ports.IdGenerator: fakes.DeterministicIdGenerator,
    ports.JobControlPort: fakes.StubJobControl,
    ports.LogparseBrokerFactory: fakes.FakeLogparseBrokerFactory,
    ports.PublicationCommitGuard: fakes.InMemoryPublicationCommitGuard,
    ports.ResourceStore: fakes.InMemoryResourceStore,
    ports.Runtime: fakes.ScriptedRuntime,
    ports.StateAdminPort: fakes.StubStateAdmin,
    ports.StateChangeNotifier: fakes.InMemoryStateChangeNotifier,
    ports.StateRepository: fakes.InMemoryStateRepository,
}


def _model(schema_name: str, fixture_name: str):
    return TypeAdapter(SCHEMA_MODELS[schema_name]).validate_python(
        load_json(FIXTURE_ROOT / "positive" / fixture_name)
    )


def test_every_public_protocol_is_runtime_checkable() -> None:
    for name in ports.__all__:
        protocol = getattr(ports, name)
        assert inspect.isclass(protocol)
        assert getattr(protocol, "_is_runtime_protocol", False), name


@pytest.mark.parametrize(
    ("port_type", "factory"),
    sorted(PORT_FAKE_FACTORIES.items(), key=lambda item: item[0].__name__),
)
def test_public_fakes_structurally_conform(port_type: type, factory: type) -> None:
    assert isinstance(factory(), port_type)


def test_binary_stream_is_forward_only_bounded_and_closes_idempotently() -> None:
    stream = fakes.InMemoryBinaryStream(b"abcdef")
    assert isinstance(stream, ports.BinaryStream)
    assert stream.read(2) == b"ab"
    assert stream.read(3) == b"cde"
    assert stream.read(3) == b"f"
    assert stream.read(3) == b""
    assert stream.bytes_read == 6
    with pytest.raises(ValueError):
        stream.read(0)
    stream.close()
    stream.close()
    with pytest.raises(ValueError, match="closed"):
        stream.read(1)


def test_binary_stream_context_manager_closes_after_failure() -> None:
    stream = fakes.InMemoryBinaryStream(b"payload")
    with pytest.raises(RuntimeError):
        with stream:
            raise RuntimeError("consumer failed")
    assert stream.closed


def test_cancellation_is_one_way_and_first_reason_wins() -> None:
    signal = fakes.InMemoryCancellationSignal()
    assert signal.reason is None
    assert not signal.is_cancelled()
    assert not signal.wait(0)
    assert signal.cancel(CancellationReason.SERVICE_SHUTDOWN)
    assert not signal.cancel(CancellationReason.USER_CANCEL)
    assert signal.reason is CancellationReason.SERVICE_SHUTDOWN
    assert signal.wait(0)
    with pytest.raises(ValueError):
        signal.wait(-0.1)


def test_deterministic_id_derivation_matches_the_frozen_uuid5_algorithm() -> None:
    generator = fakes.DeterministicIdGenerator()
    kind = "artifact"
    parts = ["installation", "case", "outcome", "proposal"]
    canonical_name = canonical_json_bytes({"kind": kind, "parts": parts})[
        :-1
    ].decode()
    expected = str(uuid.uuid5(uuid.NAMESPACE_URL, canonical_name))
    assert generator.derive(kind, parts) == expected
    assert generator.derive(kind, parts) == expected
    assert expected == expected.lower()
    with pytest.raises(ValueError):
        generator.derive(kind, [])


def test_attachment_guard_lease_is_capability_bound_and_idempotent() -> None:
    guard = fakes.InMemoryAttachmentUploadGuard()
    store = fakes.InMemoryResourceStore(upload_guard=guard)
    attachment_id = "00000000-0000-0000-0000-000000000050"
    lease = guard.acquire(attachment_id)
    assert isinstance(lease, ports.AttachmentUploadLease)
    assert lease.attachment_id == attachment_id
    staged = store.stage_attachment(
        attachment_id,
        lease,
        fakes.InMemoryBinaryStream(b"archive"),
        expected_size=7,
        expected_sha256=hashlib.sha256(b"archive").hexdigest(),
    )
    assert staged.attachment_id == attachment_id
    lease.release()
    lease.release()
    assert lease.is_released()
    with pytest.raises(ApplicationPortError) as raised:
        store.stage_attachment(
            attachment_id,
            lease,
            fakes.InMemoryBinaryStream(b"archive"),
        )
    assert raised.value.error.code is ErrorCode.UPLOAD_INCOMPLETE


def test_attachment_guard_serializes_the_same_id_but_not_other_ids() -> None:
    guard = fakes.InMemoryAttachmentUploadGuard()
    first = guard.acquire("attachment-a")
    acquired = threading.Event()
    release_second = threading.Event()

    def contender() -> None:
        second = guard.acquire("attachment-a")
        acquired.set()
        release_second.wait(1)
        second.release()

    thread = threading.Thread(target=contender)
    thread.start()
    assert not acquired.wait(0.02)

    other = guard.acquire("attachment-b")
    other.release()
    first.release()
    assert acquired.wait(1)
    release_second.set()
    thread.join(1)
    assert not thread.is_alive()


def test_publication_guard_is_reentrant_and_leases_release_idempotently() -> None:
    guard = fakes.InMemoryPublicationCommitGuard()
    outer = guard.acquire()
    inner = guard.acquire()
    assert isinstance(outer, ports.PublicationCommitLease)
    assert guard.held_by_current_thread()
    inner.release()
    assert guard.held_by_current_thread()
    outer.release()
    outer.release()
    assert not guard.held_by_current_thread()


def test_resource_store_file_round_trip_preserves_exact_bytes() -> None:
    store = fakes.InMemoryResourceStore()
    payload = b"evidence\x00bytes"
    staged = store.stage_file(
        "00000000-0000-0000-0000-000000000011",
        "evidence",
        fakes.InMemoryBinaryStream(payload),
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )
    published = store.publish(staged, "cases/case/resources/evidence/payload")
    opened = store.open_read(published)
    assert opened.read(len(payload) + 1) == payload
    assert opened.read(1) == b""
    with pytest.raises(ApplicationPortError) as raised:
        store.publish(staged, "../escape")
    assert raised.value.error.code is ErrorCode.PATH_VIOLATION


def test_dispatcher_deduplicates_submit_and_cancel() -> None:
    dispatcher = fakes.RecordingDispatcher()
    job_id = "00000000-0000-0000-0000-000000000001"
    first = dispatcher.submit(job_id)
    duplicate = dispatcher.submit(job_id)
    assert first.accepted and not first.duplicate
    assert not duplicate.accepted and duplicate.duplicate
    assert dispatcher.cancel(job_id).signalled
    assert not dispatcher.cancel(job_id).signalled


def test_state_change_notifier_observes_monotonic_generation() -> None:
    notifier = fakes.InMemoryStateChangeNotifier()
    assert not notifier.wait_for_change("case-1", 0, 0)
    notifier.notify("case-1", 2)
    notifier.notify("case-1", 1)
    assert notifier.wait_for_change("case-1", 1, 0)
    assert not notifier.wait_for_change("case-1", 2, 0)


def test_execution_log_sinks_share_one_combined_byte_limit() -> None:
    store = fakes.InMemoryExecutionRecordStore()
    sinks = store.open_log_sinks(
        "job-1", combined_limit_bytes=JOB_STDOUT_STDERR_BYTES
    )
    assert isinstance(sinks.stdout, ports.AppendOnlyByteSink)
    assert isinstance(sinks.stderr, ports.AppendOnlyByteSink)
    assert sinks.combined_limit_bytes == JOB_STDOUT_STDERR_BYTES
    sinks.stdout.write(b"abc")
    sinks.stderr.write(b"de")

    counter = fakes._CombinedLogCounter(5)
    bounded_stdout = fakes._InMemoryAppendOnlyByteSink(counter)
    bounded_stderr = fakes._InMemoryAppendOnlyByteSink(counter)
    bounded_stdout.write(b"abc")
    bounded_stderr.write(b"de")
    with pytest.raises(ValueError, match="limit"):
        bounded_stdout.write(b"f")
    sinks.stdout.close()
    sinks.stdout.close()
    with pytest.raises(ValueError, match="closed"):
        sinks.stdout.write(b"x")


def test_execution_record_store_round_trips_canonical_job_and_outcome() -> None:
    store = fakes.InMemoryExecutionRecordStore()
    job = _model("job.schema.json", "job-route.json")
    outcome = _model("job-outcome.schema.json", "job-outcome-route.json")
    job_ref = store.publish_job(job)
    outcome_bytes = canonical_json_bytes(outcome)
    outcome_ref = store.publish_outcome_bytes(job.job_id, outcome_bytes)

    published_job = store.read_published_job(job.job_id)
    published_outcome = store.read_published_outcome(job.job_id)
    assert published_job is not None and published_job.job == job
    assert published_job.job_file_ref == job_ref
    assert published_outcome is not None and published_outcome.job_outcome == outcome
    assert published_outcome.outcome_file_ref == outcome_ref


def test_pure_context_projector_copies_the_complete_semantic_projection() -> None:
    state = _model("state.schema.json", "state.json")
    aggregate = next(iter(state.cases.values()))
    diagnosis_state = aggregate.case.diagnosis_state
    assert isinstance(diagnosis_state, DiagnosisState)
    snapshot = fakes.PureContextSnapshotProjector().project(diagnosis_state)
    assert snapshot.diagnosis_state_revision == diagnosis_state.revision
    assert snapshot.problem_spec == diagnosis_state.problem_spec
    assert snapshot.evidence_refs == diagnosis_state.evidence_refs
    assert "revision" not in type(snapshot).model_fields


def test_state_repository_returns_copies_not_mutable_internal_aliases() -> None:
    state = _model("state.schema.json", "state.json")
    repository = fakes.InMemoryStateRepository(state)
    first = repository.read_snapshot()
    second = repository.read_snapshot()
    assert first == second
    assert first is not second
    assert repository.export_snapshot() == canonical_json_bytes(state)


def test_logparse_broker_exposes_exactly_two_ephemeral_environment_keys(
    tmp_path: Path,
) -> None:
    factory = fakes.FakeLogparseBrokerFactory()
    job = _model("job.schema.json", "job-diagnose.json")
    manifest = _model(
        "workspace-input-manifest.schema.json", "workspace-input-manifest.json"
    )
    signal = fakes.InMemoryCancellationSignal()
    session = factory.open(job, tmp_path, manifest, signal)
    assert isinstance(session, ports.LogparseBrokerSession)
    environment = session.agent_environment()
    assert set(environment) == {
        "PROBLEM_LOCATOR_LOGPARSE_ENDPOINT",
        "PROBLEM_LOCATOR_LOGPARSE_TOKEN",
    }
    assert all(environment.values())
    session.close()
    session.close()
    with pytest.raises(RuntimeError, match="closed"):
        session.agent_environment()


def test_counting_logparse_adapter_makes_parse_once_observable() -> None:
    adapter = fakes.CountingLogparseAdapter(
        parse_results=[{"manifest": "first"}],
        target_log_results=[["target.log"], ["target.log"]],
    )
    assert adapter.parse("archive") == {"manifest": "first"}
    assert adapter.target_logs("run", order_id="order-1") == ["target.log"]
    assert adapter.target_logs("run", order_id="order-2") == ["target.log"]
    assert adapter.parse_count == 1
    assert adapter.target_logs_count == 2
