from __future__ import annotations

import io
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from problem_locator.contracts import (
    CONTRACT_REVISION,
    SCHEMA_VERSION,
    Case,
    CaseAggregate,
    CaseStatus,
    ErrorCode,
    ExecutionFileRef,
    Job,
    JobOutcome,
    OutcomeDisposition,
    OutcomeReceipt,
    RuntimeExecutionReceipt,
    StateFile,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.contracts.errors import (
    CLI_EXIT_CONFIG_OR_STATE_CORRUPT,
    CLI_EXIT_SUCCESS,
)
from problem_locator.application.preparation import fixed_asset_refs
from problem_locator.entrypoints.cli import CliHooks, main
import problem_locator.entrypoints.replay as replay_module
from problem_locator.entrypoints.replay import (
    ReplayError,
    ReplayManifest,
    ReplayMode,
    ReplayRequest,
    ReplayResult,
    _asset_diff,
    _build_projected_state,
    _copy_execution_records,
    _decode_source_state,
    _find_unique_review,
    _initialize_replay_data_root,
    _publish_new,
    _read_source_execution_records,
    run_replay_job,
    validate_replay_paths,
)
from problem_locator.entrypoints.settings import Settings
from problem_locator.storage.layout import DATA_FORMAT_MARKER_BYTES
from problem_locator.storage.platform import PlatformFileSync


JOB_ID = "00000000-0000-4000-8000-000000000111"


def _settings(tmp_path: Path) -> Settings:
    skill = tmp_path / "skills"
    repo = tmp_path / "logparse"
    config = tmp_path / "logparse-config.json"
    for directory in (skill, repo):
        directory.mkdir(exist_ok=True)
    config.write_text("{}", encoding="utf-8")
    return Settings(
        data_root=tmp_path / "ignored-data",
        public_base_url="http://127.0.0.1:8000",
        bind_host="127.0.0.1",
        port=8000,
        claude_command="claude",
        skill_dir=skill,
        logparse_repo=repo,
        logparse_config_path=config,
        logparse_python=Path(sys.executable),
        dfx_log_level="INFO",
        dfx_log_dir=None,
    )


def _hooks(replay_runner) -> CliHooks:
    return CliHooks(
        state_admin_factory=lambda _path: None,
        app_factory=lambda _settings: None,
        server_runner=lambda _app, _host, _port, _workers: None,
        replay_runner=replay_runner,
    )


def test_replay_cli_passes_validated_request_to_explicit_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        Settings,
        "load",
        classmethod(lambda cls, **_kwargs: settings),
    )
    source = tmp_path / "source"
    output_dir = tmp_path / "replay"
    calls: list[tuple[ReplayRequest, Settings]] = []

    def replay_runner(request: ReplayRequest, active: Settings) -> ReplayResult:
        calls.append((request, active))
        return ReplayResult(
            schema_version=1,
            replay_id="replay-1",
            mode=request.mode,
            success=True,
            stop_reason="DIAGNOSIS_OUTCOME_READY_NOT_SUBMITTED",
            source_case_id=None,
            source_job_id=request.job_id,
            replay_case_id=None,
            diagnosis_job_id=request.job_id,
            diagnosis_outcome_id=None,
            review_job_id=None,
            review_outcome_id=None,
            final_case_status=None,
            stages=[],
            error=None,
            completed_at="2026-08-08T00:00:00.000Z",
        )

    stdout = io.BytesIO()
    stderr = io.BytesIO()
    exit_code = main(
        [
            "replay-job",
            "--source-data-root",
            str(source),
            "--job-id",
            JOB_ID,
            "--mode",
            "diagnose-only",
            "--output-dir",
            str(output_dir),
        ],
        hooks=_hooks(replay_runner),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == CLI_EXIT_SUCCESS
    assert stderr.getvalue() == b""
    assert calls[0][0] == ReplayRequest(source, JOB_ID, ReplayMode.DIAGNOSE_ONLY, output_dir)
    assert json.loads(stdout.getvalue())["stop_reason"] == "DIAGNOSIS_OUTCOME_READY_NOT_SUBMITTED"


def test_replay_cli_requires_explicit_replay_composition(tmp_path: Path) -> None:
    stderr = io.BytesIO()
    exit_code = main(
        [
            "replay-job",
            "--source-data-root",
            str(tmp_path / "source"),
            "--job-id",
            JOB_ID,
            "--mode",
            "review-only",
            "--output-dir",
            str(tmp_path / "output"),
        ],
        hooks=_hooks(None),
        stdout=io.BytesIO(),
        stderr=stderr,
    )

    assert exit_code == CLI_EXIT_CONFIG_OR_STATE_CORRUPT
    assert json.loads(stderr.getvalue())["code"] == ErrorCode.CONFIG_INVALID.value


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1, "contract_revision": "v1-contract-r1"},
        {"schema_version": 2, "contract_revision": "older-v2-contract"},
    ],
)
def test_source_state_hard_cut_reports_schema_unsupported(payload: dict[str, object]) -> None:
    with pytest.raises(ReplayError) as caught:
        _decode_source_state(
            (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        )

    assert caught.value.error.code is ErrorCode.STATE_SCHEMA_UNSUPPORTED
    assert caught.value.stop_reason == "SOURCE_STATE_SCHEMA_UNSUPPORTED"


def test_invalid_source_is_rejected_before_output_directory_creation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source = tmp_path / "source"
    (source / "resources" / "cases").mkdir(parents=True)
    (source / "jobs").mkdir()
    (source / "tmp").mkdir()
    (source / ".instance.lock").write_bytes(b"\0")
    (source / "state.json").write_bytes(
        b'{"contract_revision":"v1-contract-r1","schema_version":1}\n'
    )
    output = tmp_path / "replay-output"

    with pytest.raises(ReplayError) as caught:
        run_replay_job(
            ReplayRequest(source, JOB_ID, ReplayMode.DIAGNOSE_ONLY, output),
            settings,
            service_factory=lambda _settings: pytest.fail(
                "composition must not be built for a legacy source"
            ),
        )

    assert caught.value.error.code is ErrorCode.STATE_SCHEMA_UNSUPPORTED
    assert not output.exists()


def test_output_directory_must_not_overlap_source(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    request = ReplayRequest(
        source_data_root=source,
        job_id=JOB_ID,
        mode=ReplayMode.DIAGNOSE_ONLY,
        output_dir=source / "replay",
    )

    with pytest.raises(ReplayError) as caught:
        validate_replay_paths(request, settings)

    assert caught.value.error.code is ErrorCode.CONFIG_INVALID
    assert caught.value.stop_reason == "PATH_OVERLAP"


def test_replay_document_publication_is_atomic_read_only_and_parent_synced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "output" / "replay-manifest.json"
    destination.parent.mkdir()
    payload = b'{"schema_version":1}\n'
    sync_calls: list[Path] = []
    real_sync = PlatformFileSync.sync_directory

    def observe_sync(self: PlatformFileSync, path: Path) -> None:
        sync_calls.append(Path(path))
        real_sync(self, path)

    monkeypatch.setattr(PlatformFileSync, "sync_directory", observe_sync)

    _publish_new(destination, payload, read_only=True)

    assert destination.read_bytes() == payload
    assert not stat.S_IMODE(destination.lstat().st_mode) & 0o222
    assert sync_calls == [destination.parent]
    assert list(destination.parent.glob(f".{destination.name}.*.tmp")) == []


def test_replay_document_publication_never_replaces_an_existing_result(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "replay-result.json"
    destination.write_bytes(b"original\n")

    with pytest.raises(FileExistsError):
        _publish_new(destination, b"replacement\n", read_only=True)

    assert destination.read_bytes() == b"original\n"
    assert list(tmp_path.glob(f".{destination.name}.*.tmp")) == []


def test_replay_document_partial_write_failure_never_exposes_final_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "replay-result.json"
    real_write = os.write
    calls = 0

    def fail_after_prefix(descriptor: int, data: object) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            prefix = memoryview(data)[:3]
            return real_write(descriptor, prefix)
        raise OSError("injected replay write failure")

    monkeypatch.setattr(replay_module.os, "write", fail_after_prefix)

    with pytest.raises(OSError, match="injected replay write failure"):
        _publish_new(destination, b"complete-document\n", read_only=True)

    assert not destination.exists()
    assert list(tmp_path.glob(f".{destination.name}.*.tmp")) == []


def test_replay_document_parent_sync_failure_rolls_back_complete_link(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "replay-result.json"

    def fail_sync(_self: PlatformFileSync, _path: Path) -> None:
        raise OSError("injected replay parent sync failure")

    monkeypatch.setattr(PlatformFileSync, "sync_directory", fail_sync)

    with pytest.raises(OSError, match="injected replay parent sync failure"):
        _publish_new(destination, b"complete-document\n", read_only=True)

    assert not destination.exists()
    assert list(tmp_path.glob(f".{destination.name}.*.tmp")) == []


def test_v3_state_contract_is_the_replay_hard_cut() -> None:
    assert SCHEMA_VERSION == 3
    assert CONTRACT_REVISION == "v3-contract-r1"


def test_replay_projection_and_output_root_share_the_current_state_boundary(
    tmp_path: Path,
) -> None:
    layout = _initialize_replay_data_root(tmp_path / "replay-data")
    marker = json.loads(layout.data_format_marker.read_bytes())
    source = StateFile.model_validate_json(
        Path("tests/fixtures/contracts/positive/state.json").read_bytes()
    )
    aggregate = next(iter(source.cases.values()))
    source_job = next(iter(aggregate.jobs.values()))
    projected = _build_projected_state(
        aggregate,
        source_job,
        source_job,
        "00000000-0000-4000-8000-000000000992",
    )

    assert layout.data_format_marker.read_bytes() == DATA_FORMAT_MARKER_BYTES
    assert marker["state_schema_version"] == projected.schema_version == SCHEMA_VERSION
    assert marker["contract_revision"] == projected.contract_revision == CONTRACT_REVISION
    assert not layout.state.exists()
    assert not layout.has_business_content_without_state()


@pytest.mark.parametrize(
    "case_status",
    [
        CaseStatus.UNRESOLVED,
        CaseStatus.WAITING_INPUT,
        CaseStatus.WAITING_ATTACHMENT,
    ],
)
def test_through_review_accepts_valid_no_review_business_states(
    case_status: CaseStatus,
) -> None:
    aggregate = SimpleNamespace(
        case=SimpleNamespace(
            status=case_status,
            active_job_id=None,
            diagnosis_state=SimpleNamespace(candidate_conclusion=None),
        ),
        jobs={},
    )
    composition = SimpleNamespace(
        repository=SimpleNamespace(read_case=lambda _case_id: aggregate)
    )

    assert _find_unique_review(composition, JOB_ID) is None


def test_asset_diff_records_source_and_replay_values(tmp_path: Path) -> None:
    del tmp_path
    fixtures = Path("tests/fixtures/contracts/positive")
    source = Job.model_validate_json((fixtures / "job-diagnose.json").read_bytes())
    replay_payload = source.model_dump(mode="python")
    replay_payload["agent_profile_ref"] = {
        **source.agent_profile_ref.model_dump(mode="python"),
        "content_hash": "9" * 64,
    }
    replay = Job.model_validate(replay_payload)

    diff = {item.binding: item for item in _asset_diff(source, replay)}

    assert diff["agent_profile"].changed is True
    assert diff["agent_profile"].source_ref == source.agent_profile_ref
    assert diff["agent_profile"].replay_ref == replay.agent_profile_ref
    assert diff["skill"].changed is False


@pytest.mark.parametrize(
    ("disposition", "case_status", "expected_stop_reason"),
    [
        (OutcomeDisposition.APPLIED, CaseStatus.UNRESOLVED, None),
        (
            OutcomeDisposition.DUPLICATE,
            CaseStatus.UNRESOLVED,
            "DIAGNOSIS_OUTCOME_NOT_APPLIED",
        ),
        (
            OutcomeDisposition.STALE,
            CaseStatus.UNRESOLVED,
            "DIAGNOSIS_OUTCOME_NOT_APPLIED",
        ),
        (
            OutcomeDisposition.REJECTED,
            CaseStatus.UNRESOLVED,
            "DIAGNOSIS_OUTCOME_NOT_APPLIED",
        ),
        (OutcomeDisposition.APPLIED, CaseStatus.FAILED, "INVALID_NO_REVIEW_STATE"),
        (OutcomeDisposition.APPLIED, CaseStatus.RUNNING, "INVALID_NO_REVIEW_STATE"),
    ],
)
def test_through_review_fails_closed_on_diagnosis_receipt_and_no_review_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    disposition: OutcomeDisposition,
    case_status: CaseStatus,
    expected_stop_reason: str | None,
) -> None:
    fixtures = Path("tests/fixtures/contracts/positive")
    job = Job.model_validate_json((fixtures / "job-diagnose.json").read_bytes())
    outcome = JobOutcome.model_validate_json(
        (fixtures / "job-outcome-diagnosis.json").read_bytes()
    )
    outcome_bytes = canonical_json_bytes(outcome)
    execution = RuntimeExecutionReceipt(
        job_outcome=outcome,
        outcome_file_ref=ExecutionFileRef(
            relative_key=f"jobs/{job.job_id}/job_outcome.json",
            size=len(outcome_bytes),
            sha256=hashlib.sha256(outcome_bytes).hexdigest(),
        ),
    )
    source = tmp_path / "source"
    output = tmp_path / "output"
    settings = _settings(tmp_path)

    def prepare(progress, request, _settings, output_data_root):
        request.output_dir.mkdir()
        progress.output_created = True
        progress.source_case_id = job.case_id
        output_data_root.mkdir()
        job_bytes = canonical_json_bytes(job)
        manifest = ReplayManifest(
            schema_version=1,
            state_schema_version=SCHEMA_VERSION,
            contract_revision=CONTRACT_REVISION,
            replay_id=progress.replay_id,
            mode=request.mode,
            source_data_root=str(source),
            source_state_generation=1,
            source_state_sha256="a" * 64,
            source_installation_id="00000000-0000-4000-8000-000000000991",
            source_case_id=job.case_id,
            source_job_id=job.job_id,
            source_job_sha256=hashlib.sha256(job_bytes).hexdigest(),
            source_outcome_id=outcome.outcome_id,
            source_outcome_sha256=hashlib.sha256(outcome_bytes).hexdigest(),
            output_data_root=str(output_data_root),
            replay_installation_id="00000000-0000-4000-8000-000000000992",
            projected_job_sha256=hashlib.sha256(job_bytes).hexdigest(),
            projected_state_sha256="b" * 64,
            source_fixed_asset_refs=fixed_asset_refs(job),
            replay_fixed_asset_refs=fixed_asset_refs(job),
            asset_diff=_asset_diff(job, job),
            created_at="2026-08-08T00:00:00.000Z",
        )
        return None, job, manifest

    no_review_aggregate = SimpleNamespace(
        case=SimpleNamespace(
            status=case_status,
            active_job_id=None,
            diagnosis_state=SimpleNamespace(candidate_conclusion=None),
        ),
        jobs={},
    )
    repository = SimpleNamespace(
        read_snapshot=lambda: {"schema_version": 2},
        read_case=lambda _case_id: no_review_aggregate,
    )
    application = SimpleNamespace(
        submit_outcome=lambda *_args: OutcomeReceipt(
            disposition=disposition,
            case_view=None,
        )
    )
    composition = SimpleNamespace(
        repository=repository,
        application=application,
        close=lambda: None,
    )
    monkeypatch.setattr(replay_module, "_prepare_projection", prepare)
    monkeypatch.setattr(replay_module, "_execute_one", lambda *_args: execution)

    request = ReplayRequest(source, job.job_id, ReplayMode.THROUGH_REVIEW, output)
    if expected_stop_reason is not None:
        with pytest.raises(ReplayError) as caught:
            run_replay_job(
                request,
                settings,
                service_factory=lambda _settings: composition,
            )
        assert caught.value.stop_reason == expected_stop_reason
        assert caught.value.result is not None
        assert caught.value.result.success is False
        persisted = ReplayResult.model_validate_json(
            (output / "replay-result.json").read_bytes()
        )
        assert persisted.success is False
        assert persisted.stop_reason == expected_stop_reason
    else:
        result = run_replay_job(
            request,
            settings,
            service_factory=lambda _settings: composition,
        )
        assert result.success is True
        assert result.stop_reason == "NO_REVIEW_JOB"
        assert result.diagnosis_outcome_id == outcome.outcome_id
        assert result.review_job_id is None
        assert result.final_case_status == CaseStatus.UNRESOLVED.value


@pytest.mark.parametrize(
    ("review_disposition", "final_status", "expected_stop_reason"),
    [
        (
            OutcomeDisposition.DUPLICATE,
            CaseStatus.UNRESOLVED,
            "REVIEW_OUTCOME_NOT_APPLIED",
        ),
        (
            OutcomeDisposition.STALE,
            CaseStatus.UNRESOLVED,
            "REVIEW_OUTCOME_NOT_APPLIED",
        ),
        (
            OutcomeDisposition.REJECTED,
            CaseStatus.UNRESOLVED,
            "REVIEW_OUTCOME_NOT_APPLIED",
        ),
        (
            OutcomeDisposition.APPLIED,
            CaseStatus.FAILED,
            "INVALID_REVIEW_COMPLETION_STATE",
        ),
        (OutcomeDisposition.APPLIED, CaseStatus.UNRESOLVED, None),
    ],
)
def test_through_review_fails_closed_on_review_receipt_and_failed_case(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    review_disposition: OutcomeDisposition,
    final_status: CaseStatus,
    expected_stop_reason: str | None,
) -> None:
    fixtures = Path("tests/fixtures/contracts/positive")
    diagnosis_job = Job.model_validate_json((fixtures / "job-diagnose.json").read_bytes())
    review_job = Job.model_validate_json((fixtures / "job-review.json").read_bytes())
    diagnosis_outcome = JobOutcome.model_validate_json(
        (fixtures / "job-outcome-diagnosis.json").read_bytes()
    )
    review_outcome = JobOutcome.model_validate_json(
        (fixtures / "job-outcome-review.json").read_bytes()
    )

    def execution(outcome: JobOutcome) -> RuntimeExecutionReceipt:
        payload = canonical_json_bytes(outcome)
        return RuntimeExecutionReceipt(
            job_outcome=outcome,
            outcome_file_ref=ExecutionFileRef(
                relative_key=f"jobs/{outcome.job_id}/job_outcome.json",
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
        )

    executions = [execution(diagnosis_outcome), execution(review_outcome)]
    source = tmp_path / "source"
    output = tmp_path / "output"
    settings = _settings(tmp_path)

    def prepare(progress, request, _settings, output_data_root):
        request.output_dir.mkdir()
        progress.output_created = True
        progress.source_case_id = diagnosis_job.case_id
        output_data_root.mkdir()
        job_bytes = canonical_json_bytes(diagnosis_job)
        manifest = ReplayManifest(
            schema_version=1,
            state_schema_version=SCHEMA_VERSION,
            contract_revision=CONTRACT_REVISION,
            replay_id=progress.replay_id,
            mode=request.mode,
            source_data_root=str(source),
            source_state_generation=1,
            source_state_sha256="a" * 64,
            source_installation_id="00000000-0000-4000-8000-000000000991",
            source_case_id=diagnosis_job.case_id,
            source_job_id=diagnosis_job.job_id,
            source_job_sha256=hashlib.sha256(job_bytes).hexdigest(),
            source_outcome_id=diagnosis_outcome.outcome_id,
            source_outcome_sha256=hashlib.sha256(
                canonical_json_bytes(diagnosis_outcome)
            ).hexdigest(),
            output_data_root=str(output_data_root),
            replay_installation_id="00000000-0000-4000-8000-000000000992",
            projected_job_sha256=hashlib.sha256(job_bytes).hexdigest(),
            projected_state_sha256="b" * 64,
            source_fixed_asset_refs=fixed_asset_refs(diagnosis_job),
            replay_fixed_asset_refs=fixed_asset_refs(diagnosis_job),
            asset_diff=_asset_diff(diagnosis_job, diagnosis_job),
            created_at="2026-08-08T00:00:00.000Z",
        )
        return None, diagnosis_job, manifest

    reviewing = SimpleNamespace(
        case=SimpleNamespace(
            status=CaseStatus.REVIEWING,
            active_job_id=review_job.job_id,
            diagnosis_state=SimpleNamespace(
                candidate_conclusion=review_job.context_snapshot.candidate_conclusion
            ),
        ),
        jobs={review_job.job_id: review_job},
    )
    completed = SimpleNamespace(
        case=SimpleNamespace(
            status=final_status,
            active_job_id=None,
            diagnosis_state=SimpleNamespace(candidate_conclusion=None),
        ),
        jobs={},
    )
    read_calls = 0

    def read_case(_case_id):
        nonlocal read_calls
        read_calls += 1
        return reviewing if read_calls == 1 else completed

    dispositions = iter(
        [
            OutcomeReceipt(
                disposition=OutcomeDisposition.APPLIED,
                case_view=None,
            ),
            OutcomeReceipt(disposition=review_disposition, case_view=None),
        ]
    )
    composition = SimpleNamespace(
        repository=SimpleNamespace(
            read_snapshot=lambda: {"schema_version": 2},
            read_case=read_case,
        ),
        application=SimpleNamespace(submit_outcome=lambda *_args: next(dispositions)),
        close=lambda: None,
    )
    monkeypatch.setattr(replay_module, "_prepare_projection", prepare)
    monkeypatch.setattr(
        replay_module,
        "_execute_one",
        lambda *_args: executions.pop(0),
    )

    request = ReplayRequest(
        source,
        diagnosis_job.job_id,
        ReplayMode.THROUGH_REVIEW,
        output,
    )
    if expected_stop_reason is not None:
        with pytest.raises(ReplayError) as caught:
            run_replay_job(
                request,
                settings,
                service_factory=lambda _settings: composition,
            )
        assert caught.value.stop_reason == expected_stop_reason
        assert caught.value.result is not None
        assert caught.value.result.success is False
    else:
        result = run_replay_job(
            request,
            settings,
            service_factory=lambda _settings: composition,
        )
        assert result.success is True
        assert result.stop_reason == "THROUGH_REVIEW_COMPLETED"
        assert result.final_case_status == CaseStatus.UNRESOLVED.value


def test_projection_validates_old_job_record_before_writing_new_bindings(
    tmp_path: Path,
) -> None:
    fixtures = Path("tests/fixtures/contracts/positive")
    job_payload = json.loads((fixtures / "job-diagnose.json").read_text(encoding="utf-8"))
    job_payload.update(
        attachment_refs=[],
        artifact_refs=[],
        evidence_refs=[],
        previous_outcome_refs=[],
        base_state_revision=1,
    )
    job_payload["context_snapshot"]["diagnosis_state_revision"] = 1
    job_payload["context_snapshot"]["evidence_refs"] = []
    source_job = Job.model_validate(job_payload)
    rebound_payload = source_job.model_dump(mode="python")
    rebound_payload["agent_profile_ref"] = {
        **source_job.agent_profile_ref.model_dump(mode="python"),
        "content_hash": "9" * 64,
    }
    rebound_job = Job.model_validate(rebound_payload)

    state_payload = json.loads((fixtures / "state.json").read_text(encoding="utf-8"))
    case_payload = next(iter(state_payload["cases"].values()))["case"]
    snapshot = rebound_job.context_snapshot
    case_payload.update(
        status="RUNNING",
        active_job_id=rebound_job.job_id,
        selected_skill_ref=rebound_job.skill_ref.model_dump(mode="json"),
        diagnosis_state={
            "revision": snapshot.diagnosis_state_revision,
            "problem_spec": snapshot.problem_spec.model_dump(mode="json"),
            "user_facts": [],
            "confirmed_facts": [],
            "active_hypotheses": [],
            "rejected_hypotheses": [],
            "open_questions": [],
            "pending_requirements": [],
            "evidence_refs": [],
            "candidate_conclusion": None,
        },
    )
    aggregate = CaseAggregate(
        case=Case.model_validate(case_payload),
        jobs={rebound_job.job_id: rebound_job},
        outcomes={},
        outcome_processing_records={},
        execution_failure_records={},
        attachments={},
        evidence={},
        artifacts={},
    )
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    source_record = source_root / "jobs" / source_job.job_id / "job.json"
    source_record.parent.mkdir(parents=True)
    source_record.write_bytes(canonical_json_bytes(source_job))
    output_root.mkdir()

    source_job_bytes, source_outcome_bytes = _read_source_execution_records(
        source_root,
        aggregate.jobs,
        aggregate.outcomes,
        rebound_job.job_id,
        source_job,
    )
    _copy_execution_records(
        output_root,
        aggregate,
        rebound_job,
        source_job_bytes,
        source_outcome_bytes,
    )

    projected = parse_canonical_json_bytes(
        (output_root / "jobs" / rebound_job.job_id / "job.json").read_bytes(),
        Job,
    )
    assert projected == rebound_job
    assert projected.agent_profile_ref != source_job.agent_profile_ref
