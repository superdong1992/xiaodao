from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import problem_locator.integrations.agent_json as agent_json_module

from problem_locator.contracts import (
    AgentJobOutcome,
    ErrorCode,
    Job,
    UserResultPayload,
    WorkspaceInputManifest,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.integrations.agent_json import InvalidJsonBytesError
from problem_locator.runtime.outcome_finalizer import (
    FINALIZATION_MARKER_RELATIVE_PATH,
    FinalizedAgentOutcomeMarker,
    finalize_agent_outcome,
)
from problem_locator.runtime.failures import RuntimeExecutionError
from problem_locator.runtime.output_reader import read_agent_output


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_FIXTURES = REPOSITORY_ROOT / "tests/fixtures/contracts/positive"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_FIXTURES / name).read_bytes())


def _reverse_objects(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _reverse_objects(child)
            for key, child in reversed(list(value.items()))
        }
    if isinstance(value, list):
        return [_reverse_objects(child) for child in value]
    return value


def _pretty_reversed(value: object) -> bytes:
    return (
        json.dumps(
            _reverse_objects(value),
            ensure_ascii=False,
            indent=2,
        )
        .replace("\n", "\r\n")
        .encode("utf-8")
    )


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "output/proposals/user_result").mkdir(parents=True)
    (root / "runtime/tool-state").mkdir(parents=True)
    return root


def test_finalizer_recursively_canonicalizes_outcome_and_writes_exact_marker(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    draft = _fixture("agent-job-outcome-route.json")
    old_id = draft["outcome_id"]
    old_time = draft["produced_at"]
    outcome_path = root / "output/job_outcome.json"
    outcome_path.write_bytes(_pretty_reversed(draft))

    marker = finalize_agent_outcome(root)

    outcome_bytes = outcome_path.read_bytes()
    outcome = parse_canonical_json_bytes(outcome_bytes, AgentJobOutcome)
    assert outcome.outcome_id != old_id
    assert outcome.produced_at != old_time
    assert canonical_json_bytes(outcome) == outcome_bytes
    assert marker.size == len(outcome_bytes)
    assert marker.sha256 == hashlib.sha256(outcome_bytes).hexdigest()
    marker_bytes = (root / FINALIZATION_MARKER_RELATIVE_PATH).read_bytes()
    assert parse_canonical_json_bytes(
        marker_bytes,
        FinalizedAgentOutcomeMarker,
    ) == marker


def test_finalizer_canonicalizes_user_result_and_recomputes_outcome_declaration(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    draft = _fixture("agent-job-outcome-diagnosis.json")
    user_result = _fixture("user-result.json")
    proposal = draft["proposed_artifact_drafts"][0]  # type: ignore[index]
    proposal["declared_size"] = 1  # type: ignore[index]
    proposal["declared_sha256"] = "0" * 64  # type: ignore[index]
    outcome_path = root / "output/job_outcome.json"
    result_path = root / "output/proposals/user_result/diagnosis-result.json"
    outcome_path.write_bytes(_pretty_reversed(draft))
    result_path.write_bytes(_pretty_reversed(user_result))

    finalize_agent_outcome(root)

    result_bytes = result_path.read_bytes()
    assert parse_canonical_json_bytes(result_bytes, UserResultPayload)
    outcome = parse_canonical_json_bytes(
        outcome_path.read_bytes(),
        AgentJobOutcome,
    )
    finalized = outcome.proposed_artifact_drafts[0]
    assert finalized.declared_size == len(result_bytes)
    assert finalized.declared_sha256 == hashlib.sha256(result_bytes).hexdigest()


def test_runtime_rejects_user_result_rewritten_after_finalization(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    draft = _fixture("agent-job-outcome-diagnosis.json")
    user_result = _fixture("user-result.json")
    outcome_path = root / "output/job_outcome.json"
    result_path = root / "output/proposals/user_result/diagnosis-result.json"
    outcome_path.write_bytes(_pretty_reversed(draft))
    result_path.write_bytes(_pretty_reversed(user_result))
    finalize_agent_outcome(root)
    user_result["candidate_statement"] = "Rewritten after finalization."
    result_path.write_bytes(canonical_json_bytes(user_result))
    job = parse_canonical_json_bytes(
        (CONTRACT_FIXTURES / "job-diagnose.json").read_bytes(),
        Job,
    )
    manifest = parse_canonical_json_bytes(
        (CONTRACT_FIXTURES / "workspace-input-manifest.json").read_bytes(),
        WorkspaceInputManifest,
    )

    with pytest.raises(RuntimeExecutionError) as captured:
        read_agent_output(root, job, manifest)

    assert captured.value.failure.code is ErrorCode.OUTCOME_INVALID


@pytest.mark.parametrize(
    "invalid_bytes",
    [
        b'\xef\xbb\xbf{"value":1}',
        b'{"value":1,"value":2}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":',
        b'\xff',
    ],
)
def test_finalizer_rejects_ambiguous_or_invalid_json_without_marker(
    tmp_path: Path,
    invalid_bytes: bytes,
) -> None:
    root = _workspace(tmp_path)
    outcome_path = root / "output/job_outcome.json"
    outcome_path.write_bytes(invalid_bytes)

    with pytest.raises(InvalidJsonBytesError):
        finalize_agent_outcome(root)

    assert outcome_path.read_bytes() == invalid_bytes
    assert not (root / FINALIZATION_MARKER_RELATIVE_PATH).exists()


def test_finalizer_rejects_schema_error_without_replacing_draft(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    draft = _fixture("agent-job-outcome-route.json")
    draft["job_type"] = "UNKNOWN"
    original = _pretty_reversed(draft)
    outcome_path = root / "output/job_outcome.json"
    outcome_path.write_bytes(original)

    with pytest.raises(ValueError):
        finalize_agent_outcome(root)

    assert outcome_path.read_bytes() == original
    assert not (root / FINALIZATION_MARKER_RELATIVE_PATH).exists()


@pytest.mark.parametrize("failed_replace", [1, 2])
def test_finalizer_atomic_write_failure_never_leaves_a_valid_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_replace: int,
) -> None:
    root = _workspace(tmp_path)
    original = _pretty_reversed(_fixture("agent-job-outcome-route.json"))
    outcome_path = root / "output/job_outcome.json"
    outcome_path.write_bytes(original)
    real_replace = agent_json_module.os.replace
    calls = 0

    def fail_selected_replace(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == failed_replace:
            raise OSError("injected atomic replace failure")
        real_replace(source, target)

    monkeypatch.setattr(agent_json_module.os, "replace", fail_selected_replace)

    with pytest.raises(OSError, match="injected"):
        finalize_agent_outcome(root)

    assert not (root / FINALIZATION_MARKER_RELATIVE_PATH).exists()
    assert list((root / "output").glob(".*.tmp")) == []
    assert list((root / "runtime/tool-state").glob(".*.tmp")) == []
    if failed_replace == 1:
        assert outcome_path.read_bytes() == original
    else:
        assert parse_canonical_json_bytes(
            outcome_path.read_bytes(),
            AgentJobOutcome,
        )
