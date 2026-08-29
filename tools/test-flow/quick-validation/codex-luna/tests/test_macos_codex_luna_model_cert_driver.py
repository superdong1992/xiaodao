from __future__ import annotations

from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys


RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME_ROOT))

from macos_codex_luna_model_cert_driver import (  # noqa: E402
    FakeModelRoleBackend,
    run_production_model_cert,
)


def _sequence(backend: FakeModelRoleBackend) -> list[tuple[object, object]]:
    return [(item["role"], item["attempt"]) for item in backend.invocations]


def _release_registration(tmp_path: Path) -> Path:
    source_case = (
        Path(__file__).resolve().parents[5]
        / "tests/cases/release/rpc-timeout-anonymized"
    )
    root = tmp_path / "registration"
    package = root / "package/diagnose-rpc-timeout"
    references = package / "references"
    references.mkdir(parents=True)
    registration = json.loads(
        (
            source_case
            / "registration/rpc-timeout-methods-v1/registration-template.json"
        ).read_bytes()
    )
    (root / "registration-template.json").write_text(
        json.dumps(registration, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    wiki_sha256 = hashlib.sha256(
        (source_case / "input/wiki.md").read_bytes()
    ).hexdigest()
    methods = {
        "schema_version": 1,
        "skill_name": "diagnose-rpc-timeout",
        "source_wiki_sha256": wiki_sha256,
        "required_user_inputs": [
            "problem_time",
            "client_process",
            "server_process",
            "service",
            "api",
        ],
        "required_artifacts": ["log_archive"],
        "log_derived_fields": [
            "request_id",
            "client_send_us",
            "server_recv_us",
            "server_send_us",
            "client_now_us",
            "start_us",
            "end_us",
            "cost_us",
            "print_time_ms",
            "ordinal",
            "queue_us",
            "timeout_ms",
            "current_us",
            "request_us",
        ],
        "shared_references": ["references/shared-boundaries.md"],
        "methods": [
            {
                "id": "api-execution-slow",
                "title": "API 执行时间过长",
                "reference": "references/api-execution-slow.md",
                "priority": 1,
                "evidence_markers": [
                    "LATE_RESPONSE service=",
                    "API_COMPLETE service=",
                    "DEADLOOP_DETECTED service=",
                ],
            },
            {
                "id": "server-queueing",
                "title": "服务端收包排队",
                "reference": "references/server-queueing.md",
                "priority": 2,
                "evidence_markers": [
                    "LATE_RESPONSE service=",
                    "QUEUE_HISTORY print_time_ms=",
                ],
            },
            {
                "id": "client-receive-blocked",
                "title": "客户端收包线程阻塞",
                "reference": "references/client-receive-blocked.md",
                "priority": 3,
                "evidence_markers": ["LATE_RESPONSE service="],
            },
        ],
    }
    (package / "methods.json").write_text(
        json.dumps(methods, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (package / "SKILL.md").write_text(
        """---
name: diagnose-rpc-timeout
description: Test-owned release registration consumed by the production Runtime.
---

# RPC timeout diagnosis

Read request.json, method-evidence-graph.json, and method-evaluation-plan.json.
Return only evaluation_ref, verdict, and reason; UNKNOWN is allowed.
""",
        encoding="utf-8",
    )
    headings = "\n\n".join(
        (
            "## 适用条件\n固定用例。",
            "## 所需证据\n使用方法 marker。",
            "## 计算与判断\n按冻结 Evidence Graph 判断。",
            "## 确认条件\n存在正向证据。",
            "## 未知边界\n证据不足时 UNKNOWN。",
            "## 输出含义\n输出 evaluation verdict。",
        )
    )
    for name in (
        "api-execution-slow.md",
        "server-queueing.md",
        "client-receive-blocked.md",
    ):
        (references / name).write_text(headings + "\n", encoding="utf-8")
    (references / "shared-boundaries.md").write_text(
        "RPC 超时不等于取消。\n",
        encoding="utf-8",
    )
    return root


def test_production_runtime_generates_graph_plan_state_outcome_and_methods_result(
    tmp_path: Path,
) -> None:
    backend = FakeModelRoleBackend()

    result = run_production_model_cert(
        work_root=tmp_path / "normal",
        role_backend=backend,
    )

    assert result["status"] == "PASS"
    assert result["production_runtime"] == (
        "problem_locator.runtime.diagnosis_runtime.DiagnosisRuntime"
    )
    assert result["preprocessing_calls"] in {0, 1}
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
    assert result["records"]["source_state"]["filename"] == (
        "methods-state-v2.json"
    )
    assert result["records"]["source_state"]["status"] == "REVIEWER_PENDING"
    assert result["records"]["terminal_state"]["filename"] == (
        "methods-state-v2.json"
    )
    assert result["records"]["terminal_state"]["status"] == "RESOLVED"
    assert result["records"]["specialist_outcome"]["filename"] == (
        "job_outcome.json"
    )
    assert result["records"]["reviewer_outcome"]["filename"] == (
        "job_outcome.json"
    )
    assert result["scenario_id"] == "deterministic-rpc-timeout"
    assert result["model_invocations"] == 0
    assert set(result["captured_execution_files"]) == {
        "source_job",
        "reviewer_job",
        "evidence_graph",
        "evaluation_plan",
        "limitations",
        "source_state",
        "source_outcome",
        "terminal_state",
        "reviewer_outcome",
    }
    capture_root = tmp_path / "normal/model-cert-evidence"
    for name in (
        "methods-source-job.json",
        "methods-reviewer-job.json",
        "methods-evidence-graph-v2.json",
        "methods-evaluation-plan-v2.json",
        "methods-limitations-v2.json",
        "methods-source-state-v2.json",
        "methods-source-outcome-v2.json",
        "methods-terminal-state-v2.json",
        "methods-reviewer-outcome-v2.json",
        "methods-result-v2.json",
        "methods.json",
    ):
        assert (capture_root / name).is_file(), name


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


def test_production_runtime_archives_every_legal_early_terminal(
    tmp_path: Path,
) -> None:
    fixtures = [
        (
            "specialist-protocol",
            FakeModelRoleBackend(
                protocol_exhausted_roles=frozenset({"SPECIALIST"})
            ),
            "SPECIALIST_PROTOCOL_REPAIR_EXHAUSTED",
            "UNRESOLVED",
            [
                ("SPECIALIST", "PRIMARY"),
                ("SPECIALIST", "REPAIR"),
            ],
            False,
        ),
        (
            "specialist-model",
            FakeModelRoleBackend(
                model_failure_roles=frozenset({"SPECIALIST"})
            ),
            "SPECIALIST_MODEL_EXECUTION_FAILED",
            "UNRESOLVED",
            [("SPECIALIST", "PRIMARY")],
            False,
        ),
        (
            "no-evidence",
            FakeModelRoleBackend(no_matching_evidence=True),
            "NO_MATCHING_METHOD_EVIDENCE",
            "UNRESOLVED",
            [],
            False,
        ),
        (
            "reviewer-model",
            FakeModelRoleBackend(
                model_failure_roles=frozenset({"REVIEWER"})
            ),
            "REVIEWER_MODEL_EXECUTION_FAILED",
            "UNRESOLVED",
            [
                ("SPECIALIST", "PRIMARY"),
                ("REVIEWER", "PRIMARY"),
            ],
            True,
        ),
        (
            "specialist-failed",
            FakeModelRoleBackend(
                invariant_failure_roles=frozenset({"SPECIALIST"})
            ),
            "SERVER_INVARIANT_VIOLATION",
            "FAILED",
            [("SPECIALIST", "PRIMARY")],
            False,
        ),
    ]
    for (
        name,
        backend,
        reason_code,
        methods_status,
        expected_attempts,
        has_reviewer,
    ) in fixtures:
        evidence_root = tmp_path / name / "evidence"
        result = run_production_model_cert(
            work_root=tmp_path / name / "work",
            evidence_root=evidence_root,
            role_backend=backend,
        )

        assert result["status"] == "PASS", name
        assert result["public_case_status"] == methods_status, name
        assert result["methods_result"]["status"] == methods_status, name
        assert result["methods_result"]["reason_code"] == reason_code, name
        assert result["methods_result"]["diagnostic_id"].startswith("diag-"), name
        assert _sequence(backend) == expected_attempts, name
        assert len(result["role_attempts"]) == len(expected_attempts), name
        assert ("reviewer_job" in result["records"]) is has_reviewer, name
        assert ("reviewer_outcome" in result["records"]) is has_reviewer, name
        assert ("terminal_state" in result["records"]) is has_reviewer, name
        for filename in (
            "methods-evidence-graph-v2.json",
            "methods-evaluation-plan-v2.json",
            "methods-limitations-v2.json",
            "methods-source-state-v2.json",
            "methods-source-outcome-v2.json",
            "methods-result-v2.json",
        ):
            assert (evidence_root / filename).is_file(), (name, filename)
        assert not (evidence_root / "model-cert.json").exists(), name


def test_production_bundle_passes_replayable_semantic_oracle_and_mutations_fail(
    tmp_path: Path,
) -> None:
    source_root = Path(__file__).resolve().parents[5]
    release_driver = json.loads(
        (
            source_root
            / "tests/cases/release/rpc-timeout-anonymized/scenarios"
            / "multiple-rpc-timeouts/driver.json"
        ).read_bytes()
    )
    assert release_driver["initial_user_fact_values"][0] == (
        "2026-08-23T02:00:05.300Z"
    )
    registration_root = _release_registration(tmp_path)
    valid_evidence = tmp_path / "valid-evidence"
    wrong_evidence = tmp_path / "wrong-evidence"
    valid = run_production_model_cert(
        work_root=tmp_path / "valid-work",
        evidence_root=valid_evidence,
        registration_root=registration_root,
        role_backend=FakeModelRoleBackend(
            rejected_method_ids=frozenset({"server-queueing"})
        ),
    )
    wrong = run_production_model_cert(
        work_root=tmp_path / "wrong-work",
        evidence_root=wrong_evidence,
        registration_root=registration_root,
        role_backend=FakeModelRoleBackend(),
    )
    valid_scenario = tmp_path / "valid-scenario.json"
    wrong_scenario = tmp_path / "wrong-scenario.json"
    valid_scenario.write_text(
        json.dumps(valid["scenario"], ensure_ascii=False), encoding="utf-8"
    )
    wrong_scenario.write_text(
        json.dumps(wrong["scenario"], ensure_ascii=False), encoding="utf-8"
    )
    script = r"""
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
const [modulePath, sourceRoot, validRoot, validScenarioPath, wrongRoot, wrongScenarioPath] = process.argv.slice(1);
const oracle = await import(pathToFileURL(modulePath).href);
const util = await import(pathToFileURL(path.join(sourceRoot, "tools/test-flow/lib/util.mjs")).href);
const invocations = [
  { invocation_id: "fake-specialist", ordinal: 1, role: "SPECIALIST", attempt: "PRIMARY" },
  { invocation_id: "fake-reviewer", ordinal: 2, role: "REVIEWER", attempt: "PRIMARY" },
];
const options = (certRoot, scenarioPath) => ({
  sourceRoot,
  certRoot,
  scenario: JSON.parse(fs.readFileSync(scenarioPath, "utf8")),
  providerInvocations: invocations,
  modelId: "production-zero-model-fixture",
});
const baseline = oracle.buildEvidenceV2ScenarioOracleReceipt(options(validRoot, validScenarioPath));
oracle.validateEvidenceV2ScenarioOracleReceipt(baseline, options(validRoot, validScenarioPath));
let failures = 0;
try { oracle.buildEvidenceV2ScenarioOracleReceipt(options(wrongRoot, wrongScenarioPath)); } catch { failures += 1; }
const mutate = (file, change) => {
  const target = path.join(validRoot, file);
  const original = fs.readFileSync(target);
  const value = JSON.parse(original.toString("utf8"));
  change(value);
  fs.writeFileSync(target, util.canonicalJson(value));
  try { oracle.buildEvidenceV2ScenarioOracleReceipt(options(validRoot, validScenarioPath)); }
  catch { failures += 1; }
  finally { fs.writeFileSync(target, original); }
};
mutate("methods-evidence-graph-v2.json", (graph) => {
  const event = graph.events.find((item) => item.identity_tokens.length > 0);
  event.identity_tokens.pop();
});
mutate("methods-evaluation-plan-v2.json", (plan) => { plan.evidence_graph_ref = `graph-${"0".repeat(64)}`; });
mutate("methods.json", (methods) => { methods.methods[0].evidence_markers[0] += "-drift"; });
for (const file of [
  "methods-source-job.json",
  "methods-reviewer-job.json",
  "methods-evidence-graph-v2.json",
  "methods-evaluation-plan-v2.json",
  "methods-limitations-v2.json",
  "methods-source-state-v2.json",
  "methods-source-outcome-v2.json",
  "methods-terminal-state-v2.json",
  "methods-reviewer-outcome-v2.json",
  "methods-result-v2.json",
  "methods.json",
]) {
  const target = path.join(validRoot, file);
  const missing = `${target}.missing`;
  fs.renameSync(target, missing);
  try { oracle.buildEvidenceV2ScenarioOracleReceipt(options(validRoot, validScenarioPath)); }
  catch { failures += 1; }
  finally { fs.renameSync(missing, target); }
}
if (failures !== 15) throw new Error(`expected fifteen semantic failures, got ${failures}`);
process.stdout.write(JSON.stringify({ status: baseline.status, failures }));
"""
    node = shutil.which("node")
    assert node is not None
    completed = subprocess.run(
        [
            node,
            "--input-type=module",
            "--eval",
            script,
            str(source_root / "tools/validation/evidence-v2-scenario-oracle.mjs"),
            str(source_root),
            str(valid_evidence),
            str(valid_scenario),
            str(wrong_evidence),
            str(wrong_scenario),
        ],
        cwd=source_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"status": "PASS", "failures": 15}
