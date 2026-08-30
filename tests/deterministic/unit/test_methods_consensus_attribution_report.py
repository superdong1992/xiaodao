from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


REPORT = (
    Path(__file__).resolve().parents[3]
    / "tools"
    / "methods-consensus-attribution-report.py"
)
FILENAME = "methods-consensus-attribution-v2.json"


def _record(index: int, subreason: str) -> dict[str, object]:
    reason_code = {
        "UNKNOWN_PRESENT": "INCOMPLETE_EVALUATION",
        "VERDICT_MISMATCH": "SPECIALIST_REVIEWER_DISAGREEMENT",
        "EVIDENCE_SET_MISMATCH": "SPECIALIST_REVIEWER_DISAGREEMENT",
        "NO_CONFIRMED": "NO_CONFIRMED_METHOD",
    }[subreason]
    suffix = f"{index:012d}"
    return {
        "schema_version": 1,
        "record_type": "methods-consensus-attribution-v2",
        "case_id": f"00000000-0000-0000-0000-{suffix}",
        "job_id": f"10000000-0000-0000-0000-{suffix}",
        "source_job_id": f"20000000-0000-0000-0000-{suffix}",
        "evaluation_id": f"30000000-0000-0000-0000-{suffix}",
        "evidence_graph_ref": f"method-graph-v2:{index}",
        "plan_ref": f"method-plan-v2:{index}",
        "skill_sha256": f"{index:x}" * 64,
        "terminal_status": "UNRESOLVED",
        "reason_code": reason_code,
        "attribution_stage": "CONSENSUS",
        "consensus_subreason": subreason,
        "evaluation_count": 1,
        "evaluation_event_counts": [
            {"evaluation_ref": f"method-evaluation-v2:{index}", "event_count": 2}
        ],
        "activated_method_count": 1,
        "package_method_count": 2,
    }


def _write_record(root: Path, index: int, record: dict[str, object]) -> Path:
    job_root = root / "jobs" / f"job-{index}"
    job_root.mkdir(parents=True)
    path = job_root / FILENAME
    path.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    return path


def _run_report(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPORT), "--records-root", str(root)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_report_aggregates_all_four_consensus_subreasons(tmp_path: Path) -> None:
    for index, subreason in enumerate(
        (
            "UNKNOWN_PRESENT",
            "VERDICT_MISMATCH",
            "EVIDENCE_SET_MISMATCH",
            "NO_CONFIRMED",
        ),
        start=1,
    ):
        _write_record(tmp_path, index, _record(index, subreason))
    (tmp_path / "jobs" / "job-1" / "methods-state-v2.json").write_text(
        json.dumps(
            {"status": "UNRESOLVED", "reason_code": "INCOMPLETE_EVALUATION"}
        ),
        encoding="utf-8",
    )
    state_root = tmp_path / "jobs" / "job-5"
    state_root.mkdir(parents=True)
    (state_root / "methods-state-v2.json").write_text(
        json.dumps(
            {
                "status": "UNRESOLVED",
                "reason_code": "REVIEWER_SEMANTIC_INVALID",
            }
        ),
        encoding="utf-8",
    )

    completed = _run_report(tmp_path)

    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)
    assert summary["records_scanned"] == 4
    assert summary["terminal_state_records_scanned"] == 2
    assert summary["terminal_records_scanned"] == 5
    assert summary["unresolved_records"] == 5
    assert summary["consensus_subreason_distribution"] == {
        "EVIDENCE_SET_MISMATCH": 1,
        "NO_CONFIRMED": 1,
        "UNKNOWN_PRESENT": 1,
        "VERDICT_MISMATCH": 1,
    }
    assert summary["reason_code_distribution"] == {
        "INCOMPLETE_EVALUATION": 1,
        "NO_CONFIRMED_METHOD": 1,
        "REVIEWER_SEMANTIC_INVALID": 1,
        "SPECIALIST_REVIEWER_DISAGREEMENT": 2,
    }
    assert summary["evaluation_count_distribution"] == {"1": 4}
    assert summary["event_count_per_evaluation_distribution"] == {"2": 4}
    assert summary["activation_rate"] == {
        "activated_method_total": 4,
        "package_method_total": 8,
        "weighted_ratio": 0.5,
    }


def test_report_rejects_one_field_method_count_mutation(tmp_path: Path) -> None:
    record = _record(1, "UNKNOWN_PRESENT")
    record["package_method_count"] = 0
    _write_record(tmp_path, 1, record)

    completed = _run_report(tmp_path)

    assert completed.returncode == 2
    assert "method counts are inconsistent" in completed.stderr
