#!/usr/bin/env python3
from __future__ import annotations

import copy
import importlib.util
import sys
import tempfile
from pathlib import Path


RUNNER_PATH = Path(__file__).with_name("run.py")


def load_runner():
    spec = importlib.util.spec_from_file_location("rpc_skill_feasibility_runner", RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load experiment runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def expect_category(runner, category: str, operation) -> None:
    try:
        operation()
    except runner.ExperimentError as exc:
        if exc.category != category:
            raise AssertionError(f"expected {category}, got {exc.category}: {exc}") from exc
    else:
        raise AssertionError(f"expected {category} failure")


def main() -> int:
    runner = load_runner()
    with tempfile.TemporaryDirectory(prefix="evidence-contract-") as temporary:
        workspace = Path(temporary)
        evidence_root = workspace / "evidence"
        evidence_root.mkdir()
        first_line = "2026-08-23T10:00:01Z EVENT_DONE job_id=alpha began=10 ended=20"
        second_line = "2026-08-23T10:00:02Z EVENT_DONE job_id=beta began=30 ended=40"
        (evidence_root / "client.log").write_text("", encoding="utf-8")
        (evidence_root / "server.log").write_text(
            f"{first_line}\n{second_line}\n",
            encoding="utf-8",
        )

        case = runner.Case(
            workspace,
            {
                "scenario_id": "generic-two-events",
                "expected_status": "CONFIRMED",
                "expected_branch_markers": ["EVENT_DONE"],
                "expected_terms": ["job_id=alpha", "job_id=beta"],
                "expected_evidence_identities": [
                    {
                        "branch_marker": "EVENT_DONE",
                        "identity_tokens": ["job_id=alpha"],
                    }
                ],
                "forbidden_evidence_terms": [],
            },
        )
        methods = {
            "task-completed": {
                "id": "task-completed",
                "evidence_markers": ["EVENT_DONE"],
            }
        }
        branch_mapping = {"EVENT_DONE": "task-completed"}
        receipt_sha256 = "a" * 64
        bound_schema = runner.bound_diagnosis_schema(receipt_sha256)
        receipt_contract = bound_schema["properties"]["logparse_receipt_sha256"]
        if receipt_contract.get("const") != receipt_sha256 or "pattern" in receipt_contract:
            raise AssertionError("receipt hash was not bound into the diagnosis schema")
        valid = {
            "schema_version": 2,
            "scenario_id": "generic-two-events",
            "status": "CONFIRMED",
            "confirmed_methods": ["task-completed"],
            "candidate_methods": [],
            "evidence": [
                {
                    "method_id": "task-completed",
                    "summary": "alpha completed",
                    "identity_tokens": ["job_id=alpha"],
                    "sources": [
                        {
                            "anchor": "server",
                            "marker": "EVENT_DONE",
                            "line": first_line,
                        }
                    ],
                },
                {
                    "method_id": "task-completed",
                    "summary": "beta completed",
                    "identity_tokens": ["job_id=beta"],
                    "sources": [
                        {
                            "anchor": "server",
                            "marker": "EVENT_DONE",
                            "line": second_line,
                        }
                    ],
                },
            ],
            "limitations": [],
            "safety_notes": ["超时不等于取消，后续工作仍可能继续。"],
            "logparse_receipt_sha256": receipt_sha256,
        }

        runner.validate_diagnosis_result(
            case=case,
            result=valid,
            receipt_sha256=receipt_sha256,
            methods=methods,
            branch_mapping=branch_mapping,
            workspace=workspace,
        )

        invented_line = copy.deepcopy(valid)
        invented_line["evidence"][0]["sources"][0]["line"] = first_line + " invented=true"
        expect_category(
            runner,
            "evidence_grounding",
            lambda: runner.validate_diagnosis_result(
                case=case,
                result=invented_line,
                receipt_sha256=receipt_sha256,
                methods=methods,
                branch_mapping=branch_mapping,
                workspace=workspace,
            ),
        )

        invented_identity = copy.deepcopy(valid)
        invented_identity["evidence"][0]["identity_tokens"] = ["job_id=missing"]
        expect_category(
            runner,
            "evidence_identity",
            lambda: runner.validate_diagnosis_result(
                case=case,
                result=invented_identity,
                receipt_sha256=receipt_sha256,
                methods=methods,
                branch_mapping=branch_mapping,
                workspace=workspace,
            ),
        )

        missing_evidence = copy.deepcopy(valid)
        missing_evidence["evidence"] = []
        expect_category(
            runner,
            "evidence_grounding",
            lambda: runner.validate_diagnosis_result(
                case=case,
                result=missing_evidence,
                receipt_sha256=receipt_sha256,
                methods=methods,
                branch_mapping=branch_mapping,
                workspace=workspace,
            ),
        )

    print("PASS: generic evidence contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
