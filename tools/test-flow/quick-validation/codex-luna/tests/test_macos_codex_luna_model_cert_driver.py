from __future__ import annotations

from pathlib import Path
import sys


RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME_ROOT))

from macos_codex_luna_model_cert_driver import (  # noqa: E402
    FakeModelRoleBackend,
    run_production_model_cert,
)


def _sequence(backend: FakeModelRoleBackend) -> list[tuple[object, object]]:
    return [(item["role"], item["attempt"]) for item in backend.invocations]


def test_production_runtime_generates_graph_plan_state_outcome_and_methods_result(
    tmp_path: Path,
) -> None:
    backend = FakeModelRoleBackend()

    result = run_production_model_cert(
        work_root=tmp_path / "normal",
        role_backend=backend,
    )

    assert result["status"] == "PASS"
    assert result["production_runtime"] is True
    assert result["preprocessing_calls"] == 1
    assert _sequence(backend) == [
        ("SPECIALIST", "PRIMARY"),
        ("REVIEWER", "PRIMARY"),
    ]
    assert result["public_case_status"] == "RESOLVED"
    assert result["methods_result"]["status"] == "RESOLVED"
    assert result["methods_result"]["confirmed_method_ids"] == [
        "rpc-call-timeout"
    ]
    assert result["records"]["graph"]["filename"] == (
        "methods-evidence-graph-v2.json"
    )
    assert result["records"]["plan"]["filename"] == (
        "methods-evaluation-plan-v2.json"
    )
    assert result["records"]["source_state"] == {
        "filename": "methods-state-v2.json",
        "status": "REVIEWER_PENDING",
    }
    assert result["records"]["terminal_state"] == {
        "filename": "methods-state-v2.json",
        "status": "RESOLVED",
    }
    assert result["records"]["specialist_outcome"]["filename"] == (
        "job_outcome.json"
    )
    assert result["records"]["reviewer_outcome"]["filename"] == (
        "job_outcome.json"
    )
    assert result["legacy_surfaces"] == {
        "candidate": False,
        "grounding": False,
        "partial_status": False,
        "artifact_result": False,
    }


def test_production_runtime_allows_one_specialist_repair_only(
    tmp_path: Path,
) -> None:
    backend = FakeModelRoleBackend(
        invalid_primary_roles=frozenset({"SPECIALIST"})
    )

    result = run_production_model_cert(
        work_root=tmp_path / "specialist-repair",
        role_backend=backend,
    )

    assert result["methods_result"]["status"] == "RESOLVED"
    assert _sequence(backend) == [
        ("SPECIALIST", "PRIMARY"),
        ("SPECIALIST", "REPAIR"),
        ("REVIEWER", "PRIMARY"),
    ]


def test_production_runtime_allows_one_repair_per_role_and_four_calls_total(
    tmp_path: Path,
) -> None:
    backend = FakeModelRoleBackend(
        invalid_primary_roles=frozenset({"SPECIALIST", "REVIEWER"})
    )

    result = run_production_model_cert(
        work_root=tmp_path / "both-repair",
        role_backend=backend,
    )

    assert result["methods_result"]["status"] == "RESOLVED"
    assert _sequence(backend) == [
        ("SPECIALIST", "PRIMARY"),
        ("SPECIALIST", "REPAIR"),
        ("REVIEWER", "PRIMARY"),
        ("REVIEWER", "REPAIR"),
    ]
    assert len(backend.invocations) == 4
