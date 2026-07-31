from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from problem_locator.contracts import (
    CaseAggregate,
    ErrorCode,
    ExecutionFileRef,
    ExecutionStage,
    FixtureManifest,
    Job,
    JobOutcome,
    RuntimeInfrastructureError,
    StateFile,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.runtime.catalog import VersionedAssetCatalog
from problem_locator.runtime.context_builder import ContextBuilder
from problem_locator.runtime.context_policy import RuntimeAssetResolver
from problem_locator.runtime.failures import RuntimeExecutionError, runtime_failure
from problem_locator.runtime.outcome_publisher import OutcomePublisher
from problem_locator.runtime.workspace import WorkspaceManager


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_FIXTURES = REPOSITORY_ROOT / "tests/fixtures/contracts/positive"
CATALOG_FIXTURES = REPOSITORY_ROOT / "tests/fixtures/components/runtime-catalog"


def _json(name: str) -> Any:
    return json.loads((CONTRACT_FIXTURES / name).read_bytes())


def _route_job() -> Job:
    return Job.model_validate(_json("job-route.json"))


class _Clock:
    def __init__(self) -> None:
        self.calls = 0

    def now(self) -> str:
        self.calls += 1
        return "2026-01-02T03:04:05.000Z"


class _Ids:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def new(self, kind: str) -> str:
        self.calls.append(kind)
        values = {
            "job_outcome": "00000000-0000-4000-8000-000000000401",
            "execution_failure": "00000000-0000-4000-8000-000000000402",
        }
        return values[kind]

    def derive(self, kind: str, stable_parts: list[str]) -> str:
        raise AssertionError("Runtime publication must not derive IDs")


class _Records:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[tuple[str, bytes]] = []

    def publish_outcome_bytes(self, job_id: str, canonical_bytes: bytes) -> ExecutionFileRef:
        self.calls.append((job_id, canonical_bytes))
        if self.failures:
            self.failures -= 1
            raise OSError("injected publication failure")
        return ExecutionFileRef(
            relative_key=f"jobs/{job_id}/job_outcome.json",
            size=len(canonical_bytes),
            sha256=hashlib.sha256(canonical_bytes).hexdigest(),
        )


def _failure() -> RuntimeExecutionError:
    return runtime_failure(
        stage=ExecutionStage.BACKEND_EXECUTE,
        code=ErrorCode.BACKEND_TIMEOUT,
        message="Agent execution exceeded the fixed wall time.",
        retryable=True,
    )


def test_system_failure_outcome_uses_only_injected_clock_and_id() -> None:
    records = _Records()
    clock = _Clock()
    ids = _Ids()
    publisher = OutcomePublisher(records, clock, ids)

    receipt = publisher.publish_failure(_route_job(), _failure().failure)

    outcome = receipt.job_outcome
    assert outcome.outcome_id == "00000000-0000-4000-8000-000000000401"
    assert outcome.produced_at == "2026-01-02T03:04:05.000Z"
    assert outcome.error is not None
    assert outcome.error.code is ErrorCode.BACKEND_TIMEOUT
    assert ids.calls == ["job_outcome"]
    assert clock.calls == 1
    assert records.calls == [(outcome.job_id, canonical_json_bytes(outcome))]


def test_success_publication_failure_falls_back_to_replayable_failure_outcome() -> None:
    job = _route_job()
    success = parse_canonical_json_bytes(
        (CONTRACT_FIXTURES / "job-outcome-route.json").read_bytes(),
        model_type=JobOutcome,
    )
    records = _Records(failures=1)
    ids = _Ids()
    publisher = OutcomePublisher(records, _Clock(), ids)

    receipt = publisher.publish_success(job, success)

    assert len(records.calls) == 2
    assert receipt.job_outcome.error is not None
    assert receipt.job_outcome.error.code is ErrorCode.EXECUTION_RECORD_FAILED
    assert ids.calls == ["job_outcome"]


def test_failure_record_publication_is_the_only_infrastructure_exception() -> None:
    ids = _Ids()
    publisher = OutcomePublisher(_Records(failures=1), _Clock(), ids)

    with pytest.raises(RuntimeInfrastructureError) as captured:
        publisher.publish_failure(_route_job(), _failure().failure)

    assert captured.value.failure_id == "00000000-0000-4000-8000-000000000402"
    assert captured.value.execution_failure.stage is ExecutionStage.EXECUTION_RECORD
    assert captured.value.execution_failure.code is ErrorCode.EXECUTION_RECORD_FAILED
    assert ids.calls == ["job_outcome", "execution_failure"]


class _UnusedResourceStore:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"ROUTE Workspace must not call ResourceStore.{name}")


class _BrokerFactory:
    def open(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("asset resolution must not open the broker")


def _route_aggregate(job: Job) -> CaseAggregate:
    state = StateFile.model_validate(_json("state.json"))
    aggregate = next(iter(state.cases.values()))
    payload = aggregate.model_dump(mode="json")
    payload["jobs"] = {job.job_id: job.model_dump(mode="json")}
    return CaseAggregate.model_validate(payload)


def _make_route_catalog(tmp_path: Path) -> VersionedAssetCatalog:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    shutil.copytree(
        CATALOG_FIXTURES / "skill-dir/manual-triage",
        skill_dir / "manual-triage",
    )
    return VersionedAssetCatalog(skill_dir=skill_dir)


def _job_from_catalog(catalog: VersionedAssetCatalog) -> Job:
    base = _route_job().model_dump(mode="json")
    bindings = catalog.route_bindings()
    base.update(bindings.model_dump(mode="json"))
    return Job.model_validate(base)


def _restore_permissions(root: Path) -> None:
    inputs = root / "inputs"
    if not inputs.exists():
        return
    for path in sorted(inputs.rglob("*"), reverse=True):
        path.chmod(0o755 if path.is_dir() else 0o644)
    inputs.chmod(0o755)


def test_workspace_asset_resolution_context_and_manifest_are_one_fixed_view(
    tmp_path: Path,
) -> None:
    catalog = _make_route_catalog(tmp_path)
    job = _job_from_catalog(catalog)
    manager = WorkspaceManager(tmp_path / "data")
    workspace = manager.prepare(job, _route_aggregate(job), _UnusedResourceStore())
    try:
        resolved = RuntimeAssetResolver(catalog).resolve(job, workspace)
        context = ContextBuilder().build(job, resolved.materials)
        manager.write_context(workspace, context.body)

        assert workspace.manifest_bytes == canonical_json_bytes(workspace.manifest)
        assert resolved.materials.manifest == workspace.manifest
        assert workspace.context_path.read_bytes() == context.body.encode("utf-8")
        assert context.body.endswith(
            "<<<END SECTION>>>\n"
        )
        assert canonical_json_bytes(workspace.manifest) in context.body.encode("utf-8")
        assert [ref.id for ref in job.available_skill_refs] == [
            "diagnosis-skill/manual-triage"
        ]
    finally:
        _restore_permissions(workspace.root)


def test_asset_content_drift_never_substitutes_the_frozen_job_version(
    tmp_path: Path,
) -> None:
    catalog = _make_route_catalog(tmp_path)
    job = _job_from_catalog(catalog)
    manager = WorkspaceManager(tmp_path / "data")
    workspace = manager.prepare(job, _route_aggregate(job), _UnusedResourceStore())
    try:
        skill = catalog.resolve(job.available_skill_refs[0])
        entry = Path(skill.root_path) / "SKILL.md"
        entry.write_text(entry.read_text() + "\nchanged after startup\n", encoding="utf-8")

        with pytest.raises(RuntimeExecutionError) as captured:
            RuntimeAssetResolver(catalog).resolve(job, workspace)

        assert captured.value.failure.stage is ExecutionStage.ASSET_RESOLUTION
        assert captured.value.failure.code is ErrorCode.ASSET_VERSION_UNAVAILABLE
    finally:
        _restore_permissions(workspace.root)


def test_diagnosis_runtime_fixture_manifests_remain_contract_valid() -> None:
    for root in (
        REPOSITORY_ROOT / "tests/fixtures/components/runtime-catalog",
        REPOSITORY_ROOT / "tests/fixtures/components/runtime-context",
        REPOSITORY_ROOT / "tests/fixtures/components/runtime-command",
        REPOSITORY_ROOT / "tests/fixtures/components/runtime-backend",
        REPOSITORY_ROOT / "tests/fixtures/components/runtime-output",
    ):
        manifest = FixtureManifest.model_validate_json(
            (root / "fixture-manifest.json").read_bytes()
        )
        assert manifest.owner_spec == "S04"
