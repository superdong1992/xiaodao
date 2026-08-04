from __future__ import annotations

from problem_locator.contracts import (
    ApplicationPortError,
    ErrorCode,
    StateRepository,
    canonical_json_bytes,
)
from problem_locator.storage.coordination import StorageCoordinationLock
from problem_locator.storage.state_repository import JsonFileStateRepository
from tests.unit.storage.fakes import DeterministicIdGenerator, FixedClock


FIXED_TIME = "2026-07-31T08:00:00.000Z"


def _repository(tmp_path):
    return JsonFileStateRepository(
        tmp_path,
        StorageCoordinationLock(),
        FixedClock(FIXED_TIME),
        DeterministicIdGenerator(seed="s08-s02-contract-seam"),
    )


def test_real_json_repository_round_trips_the_frozen_r3_state(tmp_path) -> None:
    repository = _repository(tmp_path)

    assert isinstance(repository, StateRepository)
    snapshot = repository.read_snapshot()
    report = repository.validate_all()

    assert report.valid is True
    assert report.generation == snapshot.generation == 1
    assert snapshot.contract_revision == "v1-contract-r4"
    assert repository.export_snapshot() == canonical_json_bytes(snapshot)
    assert repository.export_snapshot() == repository.layout.state.read_bytes()


def test_r2_state_is_rejected_through_the_typed_state_error_channel(
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    payload = repository.read_snapshot().model_dump(mode="json")
    payload["contract_revision"] = "v1-contract-r2"
    repository.layout.state.write_bytes(canonical_json_bytes(payload))

    try:
        _repository(tmp_path)
    except ApplicationPortError as exc:
        assert exc.error.code is ErrorCode.STATE_SCHEMA_UNSUPPORTED
        assert exc.error.retryable is False
    else:  # pragma: no cover - a regression must fail loudly
        raise AssertionError("r2 state was accepted by the r3 repository")
