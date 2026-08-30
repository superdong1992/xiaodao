"""Build a complete Methods V2 record bundle with the production Runtime.

The fixture deliberately uses a Unicode casefold pair that JavaScript
``toLowerCase`` cannot reproduce: marker prefix ``Straße`` and log line
prefix ``STRASSE``.
The hand-authored package and log are untrusted test inputs; Graph, Plan,
State, Outcome, and the public result all come from production code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import types


try:
    import pytest  # noqa: F401
except ModuleNotFoundError:
    mark = types.SimpleNamespace(parametrize=lambda *args, **kwargs: lambda value: value)
    sys.modules["pytest"] = types.SimpleNamespace(
        fixture=lambda value: value,
        mark=mark,
    )


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
RUNTIME_ROOT = (
    REPOSITORY_ROOT
    / "tools/test-flow/quick-validation/codex-luna/runtime"
)
sys.path.insert(0, str(RUNTIME_ROOT))

import macos_codex_luna_model_cert_driver as runtime_driver  # noqa: E402


METHOD_ID = "casefold-method"
SOURCE_LOG_TEMPLATE = "Straße request_id={request_id}"
MARKER = "Straße request_id="
LINE = "STRASSE request_id=42"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _registration(root: Path) -> Path:
    registration_id = "casefold-methods-v2"
    registration_root = root / registration_id
    package = registration_root / "package/diagnose-casefold-marker"
    references = package / "references"
    references.mkdir(parents=True)
    source_wiki_sha256 = hashlib.sha256(
        b"casefold production test wiki\n"
    ).hexdigest()
    template = json.loads(
        (
            REPOSITORY_ROOT
            / "tests/cases/release/rpc-timeout-anonymized/registration"
            / "rpc-timeout-methods-v1/registration-template.json"
        ).read_bytes()
    )
    template.update(
        {
            "registration_id": registration_id,
            "capability": "Exercise Unicode casefold evidence with the production Runtime.",
            "deployment_scope": "PRODUCTION",
            "summary": "Untrusted input package for a production-generated Methods V2 oracle bundle.",
        }
    )
    template["package"] = {
        "relative_path": "package/diagnose-casefold-marker",
        "skill_name": "diagnose-casefold-marker",
        "source_wiki_sha256": source_wiki_sha256,
    }
    _write_json(registration_root / "registration-template.json", template)
    _write_json(
        package / "methods.json",
        {
            "schema_version": 1,
            "skill_name": "diagnose-casefold-marker",
            "source_wiki_sha256": source_wiki_sha256,
            "required_user_inputs": [
                "problem_time",
                "client_process",
                "server_process",
                "service",
                "api",
            ],
            "required_artifacts": ["log_archive"],
            "log_derived_fields": ["request_id"],
            "shared_references": [
                "references/source-log-templates.md",
                "references/shared-boundaries.md",
            ],
            "methods": [
                {
                    "id": METHOD_ID,
                    "title": "Unicode casefold marker",
                    "reference": "references/casefold-method.md",
                    "priority": 1,
                    "evidence_markers": [MARKER],
                    "activation_markers": [MARKER],
                }
            ],
        },
    )
    (package / "SKILL.md").write_text(
        """---
name: diagnose-casefold-marker
description: Untrusted test input consumed by the production Runtime for Unicode casefold evidence.
---

# Unicode casefold diagnosis

Read request.json, method-evidence-graph.json, and method-evaluation-plan.json.
Return only evaluation_ref, verdict, supporting_event_refs, and reason;
UNKNOWN is allowed.
""",
        encoding="utf-8",
    )
    card = "\n\n".join(
        (
            "## 适用条件\n固定 Unicode casefold 用例。",
            f"## 所需证据\n- `{SOURCE_LOG_TEMPLATE}`",
            "## 计算与判断\n按 Evidence Graph 判断。",
            "## 确认条件\n存在绑定到当前 method 的证据。",
            "## 未知边界\n证据不足时返回 UNKNOWN。",
            "## 输出含义\n输出 evaluation verdict。",
        )
    )
    (references / "casefold-method.md").write_text(f"{card}\n", encoding="utf-8")
    (references / "source-log-templates.md").write_text(
        "# Source log templates\n\n"
        f"```text\n{SOURCE_LOG_TEMPLATE}\n```\n",
        encoding="utf-8",
    )
    (references / "shared-boundaries.md").write_text(
        "Evidence matching is owned by the production scanner.\n",
        encoding="utf-8",
    )
    return registration_root


def _casefold_fact_values(
    source_root: Path,
    generated_registration: bool,
    scenario_root: Path | None = None,
) -> tuple[
    list[dict[str, object]],
    dict[str, bytes],
    str,
    dict[str, list[str]],
]:
    facts, _, scenario_id, user_inputs = _ORIGINAL_FACT_VALUES(
        source_root,
        generated_registration,
        scenario_root,
    )
    return (
        facts,
        {
            "client": f"{LINE}\n".encode("utf-8"),
            "server": b"no matching marker request_id=99\n",
        },
        scenario_id,
        user_inputs,
    )


_ORIGINAL_FACT_VALUES = runtime_driver._fact_values


def build_bundle(output_root: Path) -> dict[str, object]:
    output_root.mkdir(parents=True)
    registration_root = _registration(output_root / "registration")
    evidence_root = output_root / "evidence"
    runtime_driver._fact_values = _casefold_fact_values
    try:
        runtime_receipt = runtime_driver.run_production_model_cert(
            work_root=output_root / "work",
            evidence_root=evidence_root,
            registration_root=registration_root,
            role_backend=runtime_driver.FakeModelRoleBackend(),
        )
    finally:
        runtime_driver._fact_values = _ORIGINAL_FACT_VALUES

    graph = json.loads(
        (evidence_root / "methods-evidence-graph-v2.json").read_bytes()
    )
    source_job = json.loads((evidence_root / "methods-source-job.json").read_bytes())
    reviewer_job = json.loads(
        (evidence_root / "methods-reviewer-job.json").read_bytes()
    )
    methods_result = json.loads((evidence_root / "methods-result-v2.json").read_bytes())
    methods = json.loads((evidence_root / "methods.json").read_bytes())
    if len(graph["hits"]) != 1:
        raise RuntimeError("production casefold fixture did not emit exactly one hit")
    hit = graph["hits"][0]
    if (
        hit["method_id"] != METHOD_ID
        or hit["marker_index"] != 1
        or hit["marker"] != MARKER
        or hit["line"] != LINE
    ):
        raise RuntimeError("production scanner changed the frozen casefold hit")

    expected = {
        "source_job_id": source_job["job_id"],
        "reviewer_job_id": reviewer_job["job_id"],
        "case_id": source_job["case_id"],
        "skill_ref": source_job["skill_ref"],
        "source_ids": sorted(source["source_id"] for source in graph["sources"]),
        "method_cards": [
            {
                "id": method["id"],
                "priority": method["priority"],
                "evidence_markers": method["evidence_markers"],
                "activation_markers": method["activation_markers"],
            }
            for method in methods["methods"]
        ],
        "loaded_method_ids": graph["loaded_method_ids"],
        "confirmed_method_ids": methods_result["confirmed_method_ids"],
        "required_evidence_identities": [
            {
                "method_id": METHOD_ID,
                "marker": MARKER,
                "identity_tokens": ["request_id=42"],
            }
        ],
    }
    invocations = [
        {
            "effective_model": "zero-model-role-double",
            "job_id": source_job["job_id"],
            "job_type": "DIAGNOSE",
        },
        {
            "effective_model": "zero-model-role-double",
            "job_id": reviewer_job["job_id"],
            "job_type": "REVIEW",
        },
    ]
    manifest = {
        "schema_version": 1,
        "status": runtime_receipt["status"],
        "input_provenance": "hand-authored-untrusted-package-and-log",
        "production_runtime": runtime_receipt["production_runtime"],
        "preprocessing_calls": runtime_receipt["preprocessing_calls"],
        "evidence_root": str(evidence_root),
        "expected": expected,
        "invocations": invocations,
        "public_methods_result": methods_result,
    }
    _write_json(output_root / "bundle-manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, required=True)
    options = parser.parse_args()
    manifest = build_bundle(options.output_root.resolve())
    print(json.dumps({"status": manifest["status"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
