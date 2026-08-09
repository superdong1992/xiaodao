from __future__ import annotations

from problem_locator.contracts import (
    ApplicationError,
    ContextSnapshotProjector,
    Coordinator,
    RouteOutcomeTriggerPayload,
    TriggerType,
    canonical_json_bytes,
    validate_transition_plan_for_outcome,
)
from problem_locator.domain import DomainCoordinator, PureContextSnapshotProjector
from tests.deterministic.unit.domain._builders import (
    continuation,
    diagnose_job,
    route_job,
    route_outcome,
    runtime_bindings,
    snapshot_with_active,
    state_from_job,
    trigger,
)


def test_s01_implementations_conform_to_frozen_ports() -> None:
    assert isinstance(DomainCoordinator(), Coordinator)
    assert isinstance(PureContextSnapshotProjector(), ContextSnapshotProjector)


def test_route_plan_and_projection_are_canonical_contract_values() -> None:
    source = route_job()
    outcome = route_outcome()
    snapshot = snapshot_with_active(source)
    target = diagnose_job()
    request = trigger(
        snapshot,
        trigger_type=TriggerType.ROUTE_OUTCOME,
        payload=RouteOutcomeTriggerPayload(job_outcome=outcome),
        bindings={target.job_type: runtime_bindings(target)},
        continuation_resources=continuation(
            incoming_outcome_id=outcome.outcome_id,
            job=source,
        ),
        occurred_at=outcome.produced_at,
    )

    first = DomainCoordinator().plan(snapshot, request)
    second = DomainCoordinator().plan(snapshot, request)

    assert not isinstance(first, ApplicationError)
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert validate_transition_plan_for_outcome(first, outcome) is first

    diagnosis_state = state_from_job(target)
    projected = PureContextSnapshotProjector().project(diagnosis_state)
    assert projected.diagnosis_state_revision == diagnosis_state.revision
    assert canonical_json_bytes(projected) == canonical_json_bytes(
        target.context_snapshot
    )
