from __future__ import annotations

from problem_locator.contracts import ContextSnapshotProjector, canonical_json_bytes
from problem_locator.domain import PureContextSnapshotProjector
from tests.deterministic.contracts.fakes import PureContextSnapshotProjector as ContractProjector

from ._builders import review_job, state_from_job


def test_projector_matches_the_frozen_complete_projection() -> None:
    state = state_from_job(review_job())
    projector = PureContextSnapshotProjector()

    actual = projector.project(state)
    expected = ContractProjector().project(state)

    assert isinstance(projector, ContextSnapshotProjector)
    assert actual == expected
    assert actual.diagnosis_state_revision == state.revision
    assert canonical_json_bytes(actual) == canonical_json_bytes(expected)


def test_projector_is_repeatable_and_does_not_mutate_the_state() -> None:
    state = state_from_job(review_job())
    before = canonical_json_bytes(state)

    first = PureContextSnapshotProjector().project(state)
    second = PureContextSnapshotProjector().project(state)

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert canonical_json_bytes(state) == before
