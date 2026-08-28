from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from problem_locator.contracts import (
    ApplicationPortError,
    AssetKind,
    DiagnosisMode,
    FixtureManifest,
    Job,
    ResolvedAsset,
    VersionedRef,
    WorkspaceInputManifest,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.runtime.authoritative_targets import (
    AuthoritativeTargetLog,
    AuthoritativeTargetSet,
)
from problem_locator.runtime.catalog import BUILTIN_ASSET_ROOT, VersionedAssetCatalog, hash_product_directory
from problem_locator.runtime.context_policy import _load_entry_text, _skill_index_entry
from problem_locator.runtime.methods_grounding import (
    FrozenTargetLogV1,
    SkillLoadReceiptV1,
    scan_method_markers,
    verify_method_diagnosis,
    verify_method_review,
)
from problem_locator.runtime.methods_skill import (
    load_methods_package,
    load_registered_skill_from_package,
    load_specialized_skill_registration,
)
from problem_locator.runtime.methods_outcome import map_verified_methods_draft
from problem_locator.runtime.output_reader import ValidatedMethodsPreprocessing
from problem_locator.runtime.result_types import CapturedTargetLog
from problem_locator.runtime.user_results import build_server_result_bundle
from problem_locator.contracts import EvidenceBinding


WIKI_SHA256 = "a" * 64
RECEIPT_SHA256 = "b" * 64


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_package(parent: Path, *, name: str = "diagnose-test-timeout") -> Path:
    root = parent / name
    references = root / "references"
    references.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        f"""---
name: {name}
description: Diagnose a test timeout from frozen evidence.
---

# Test locator

Read `method-evidence-graph.json` and `method-evaluation-plan.json`. Do not
rescan logs. Evaluate every `evaluation_ref` in plan order and return only
`verdict` and `reason`; use `UNKNOWN` when the evidence cannot decide the rule.
""",
        encoding="utf-8",
    )
    card = """# Slow execution

## 适用条件
Use for the requested operation.

## 所需证据
`API_COMPLETE`

## 计算与判断
Use the stated timestamps.

## 确认条件
The positive marker is present.

## 未知边界
Missing logs do not exclude the cause.

## 输出含义
Keep each event separate with its raw line and identity.
"""
    (references / "slow-execution.md").write_text(card, encoding="utf-8")
    (references / "unrelated-method.md").write_text(
        card.replace("# Slow execution", "# Unrelated method").replace(
            "`API_COMPLETE`", "`UNRELATED_POSITIVE`"
        ),
        encoding="utf-8",
    )
    (references / "shared-boundaries.md").write_text(
        "# Shared boundaries\n\nTimeout is not cancellation.\n", encoding="utf-8"
    )
    _write_json(
        root / "methods.json",
        {
            "schema_version": 1,
            "skill_name": name,
            "source_wiki_sha256": WIKI_SHA256,
            "required_user_inputs": [
                "problem_time",
                "client_process",
                "server_process",
            ],
            "required_artifacts": ["log_archive"],
            "log_derived_fields": ["request_id"],
            "shared_references": ["references/shared-boundaries.md"],
            "methods": [
                {
                    "id": "slow-execution",
                    "title": "Slow execution",
                    "reference": "references/slow-execution.md",
                    "priority": 1,
                    "evidence_markers": ["API_COMPLETE"],
                },
                {
                    "id": "unrelated-method",
                    "title": "Unrelated method",
                    "reference": "references/unrelated-method.md",
                    "priority": 2,
                    "evidence_markers": ["UNRELATED_POSITIVE"],
                }
            ],
        },
    )
    return root


def _write_registration(store: Path) -> Path:
    root = store / "test-timeout"
    package = _write_package(root / "package")
    _write_json(
        root / "registration-template.json",
        {
            "schema_version": 1,
            "registration_id": "test-timeout",
            "version": "1.0.0",
            "capability": "test timeout diagnosis",
            "deployment_scope": "PRODUCTION",
            "summary": "Diagnose one deterministic timeout fixture.",
            "package": {
                "relative_path": f"package/{package.name}",
                "skill_name": package.name,
                "source_wiki_sha256": WIKI_SHA256,
            },
            "runtime": {
                "diagnose": {
                    "agent_profile_id": "agent-profile/specialist",
                    "tool_bundle_id": "tool-bundle/diagnose",
                    "context_policy_id": "context-policy/diagnose",
                    "output_contract_id": "output-contract/diagnose",
                },
                "review": {
                    "agent_profile_id": "agent-profile/reviewer",
                    "tool_bundle_id": "tool-bundle/review",
                    "context_policy_id": "context-policy/review",
                    "output_contract_id": "output-contract/review",
                },
                "preprocessing": {
                    "requires_logparse": True,
                    "logparse_product": "test-timeout",
                    "roles": [
                        {
                            "label": "client",
                            "description": "Calling process.",
                            "presence": "REQUIRED",
                            "source_reference": "Reviewed test registration.",
                        },
                        {
                            "label": "server",
                            "description": "Serving process.",
                            "presence": "REQUIRED",
                            "source_reference": "Reviewed test registration.",
                        },
                    ],
                    "logparse_plan": {
                        "attachment_requirement": "log_archive",
                        "problem_time_binding": {
                            "source": "USER_FACT",
                            "name": "problem_time",
                        },
                        "anchors": [
                            {
                                "label": "client",
                                "module": {"source": "SKILL_FIXED", "value": "rpc"},
                                "slot": {"source": "SKILL_FIXED", "value": "client"},
                                "process_name": {
                                    "source": "USER_FACT",
                                    "name": "client_process",
                                },
                                "pid": None,
                            },
                            {
                                "label": "server",
                                "module": {"source": "SKILL_FIXED", "value": "rpc"},
                                "slot": {"source": "SKILL_FIXED", "value": "server"},
                                "process_name": {
                                    "source": "USER_FACT",
                                    "name": "server_process",
                                },
                                "pid": None,
                            },
                        ],
                    },
                },
            },
        },
    )
    return root


def _target(source_id: str, text: str) -> FrozenTargetLogV1:
    content = text.encode("utf-8")
    return FrozenTargetLogV1(
        source_id=source_id,
        relative_path=f"inputs/target-logs/{source_id}.log",
        content_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )


def _diagnosis(*, line: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "CONFIRMED",
        "confirmed_methods": ["slow-execution"],
        "candidate_methods": [],
        "evidence": [
            {
                "method_id": "slow-execution",
                "summary": "The completion marker identifies a slow request.",
                "identity_tokens": ["request_id=42"],
                "sources": [
                    {
                        "source_id": "server",
                        "line_number": 2,
                        "marker": "API_COMPLETE",
                        "line": line,
                    }
                ],
            }
        ],
        "limitations": [],
        "safety_notes": ["Timeout is not cancellation."],
    }


def test_registration_resolves_closed_package_and_three_bound_digests(tmp_path: Path) -> None:
    root = _write_registration(tmp_path / "skills")
    resolved = load_specialized_skill_registration(root)

    assert resolved.registration_id == "test-timeout"
    assert resolved.package_root == (root / "package/diagnose-test-timeout").resolve()
    assert resolved.methods.required_user_inputs == (
        "problem_time",
        "client_process",
        "server_process",
    )
    assert len({resolved.registration_sha256, resolved.package_tree_sha256, resolved.combined_sha256}) == 3
    assert load_registered_skill_from_package(root) == (
        "test-timeout",
        resolved.package_root,
        resolved.combined_sha256,
    )


def test_logparse_registration_accepts_default_product_id(tmp_path: Path) -> None:
    root = _write_registration(tmp_path / "skills")
    template = root / "registration-template.json"
    value = json.loads(template.read_text(encoding="utf-8"))
    value["runtime"]["preprocessing"]["logparse_product"] = "default"
    _write_json(template, value)

    resolved = load_specialized_skill_registration(root)

    assert resolved.registration.preprocessing.requires_logparse is True
    assert resolved.registration.preprocessing.logparse_product == "default"


def test_package_and_catalog_contract_reject_legacy_or_extra_files(tmp_path: Path) -> None:
    package = _write_package(tmp_path)
    (package / "diagnosis-skill.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="exactly SKILL.md"):
        load_methods_package(package)

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "diagnosis-skill.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="legacy diagnosis-skill.json"):
        load_specialized_skill_registration(legacy)


def test_production_loader_rejects_one_v1_runtime_field(tmp_path: Path) -> None:
    package = _write_package(tmp_path)
    skill_path = package / "SKILL.md"
    valid_text = skill_path.read_text(encoding="utf-8")
    assert load_methods_package(package).skill_name == package.name

    skill_path.write_text(valid_text + "\nRead `target_logs` again.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="uses V1 runtime field target_logs"):
        load_methods_package(package)


def test_marker_scan_loads_only_relevant_method_cards_before_context(
    tmp_path: Path,
) -> None:
    skill = load_specialized_skill_registration(_write_registration(tmp_path / "skills"))
    resolved_asset = ResolvedAsset(
        ref=VersionedRef(
            id=f"diagnosis-skill/{skill.registration_id}",
            version=skill.registration.version,
            content_hash=skill.combined_sha256,
        ),
        asset_kind=AssetKind.DIAGNOSIS_SKILL,
        root_path=str((tmp_path / "skills" / "test-timeout").resolve()),
    )
    logs = (_target("server", "API_COMPLETE request_id=42\n"),)

    receipt = scan_method_markers(skill=skill, target_logs=logs)
    base = _load_entry_text(
        resolved_asset,
        resolved_asset.ref,
        AssetKind.DIAGNOSIS_SKILL,
        loaded_method_ids=(),
    )
    selected = _load_entry_text(
        resolved_asset,
        resolved_asset.ref,
        AssetKind.DIAGNOSIS_SKILL,
        loaded_method_ids=receipt.loaded_method_ids,
    )

    assert receipt.loaded_method_ids == ("slow-execution",)
    assert 'path="references/shared-boundaries.md"' in base
    assert 'path="references/slow-execution.md"' not in base
    assert 'path="references/unrelated-method.md"' not in base
    assert 'path="references/slow-execution.md"' in selected
    assert 'path="references/unrelated-method.md"' not in selected


def test_grounding_rejects_a_receipt_that_does_not_match_injected_marker_cards(
    tmp_path: Path,
) -> None:
    skill = load_specialized_skill_registration(_write_registration(tmp_path / "skills"))
    cited = "API_COMPLETE request_id=42"
    logs = (_target("server", f"noise\n{cited}\n"),)
    wrong_receipt = SkillLoadReceiptV1(
        package_tree_sha256=skill.package_tree_sha256,
        scanned_source_ids=("server",),
        marker_hits=(("server", "API_COMPLETE", 2),),
        loaded_method_ids=("unrelated-method",),
    )

    with pytest.raises(ValueError, match="injected Methods cards differ"):
        verify_method_diagnosis(
            skill=skill,
            draft=_diagnosis(line=cited),
            target_logs=logs,
            logparse_receipt_sha256=RECEIPT_SHA256,
            skill_load=wrong_receipt,
        )


def test_grounding_binds_exact_line_marker_identity_and_receipt(tmp_path: Path) -> None:
    skill = load_specialized_skill_registration(_write_registration(tmp_path / "skills"))
    cited = "2026-08-23T10:00:05Z API_COMPLETE request_id=42 cost_us=6500000"
    logs = (
        _target("client", "unrelated\n"),
        _target("server", f"noise\n{cited}\n"),
    )
    verified = verify_method_diagnosis(
        skill=skill,
        draft=_diagnosis(line=cited),
        target_logs=logs,
        logparse_receipt_sha256=RECEIPT_SHA256,
        skill_load=scan_method_markers(skill=skill, target_logs=logs),
    )

    assert verified.audit.registration_id == "test-timeout"
    assert verified.audit.logparse_receipt_sha256 == RECEIPT_SHA256
    assert verified.audit.skill_load.scanned_source_ids == ("client", "server")
    assert verified.audit.skill_load.loaded_method_ids == ("slow-execution",)
    assert verified.audit.skill_load.marker_hits == (("server", "API_COMPLETE", 2),)

    review = verify_method_review(
        verified,
        {
            "schema_version": 1,
            "verdict": "PASS",
            "findings": [
                {
                    "method_id": "slow-execution",
                    "identity_tokens": ["request_id=42"],
                    "verdict": "PASS",
                    "reason": "The raw source and computation are sufficient.",
                }
            ],
            "limitations": [],
        },
    )
    assert review.verdict == "PASS"


def test_grounding_matches_declared_marker_without_case_sensitivity(
    tmp_path: Path,
) -> None:
    skill = load_specialized_skill_registration(_write_registration(tmp_path / "skills"))
    cited = "2026-08-23T10:00:05Z api_complete request_id=42 cost_us=6500000"
    logs = (_target("server", f"noise\n{cited}\n"),)

    receipt = scan_method_markers(skill=skill, target_logs=logs)
    verified = verify_method_diagnosis(
        skill=skill,
        draft=_diagnosis(line=cited),
        target_logs=logs,
        logparse_receipt_sha256=RECEIPT_SHA256,
        skill_load=receipt,
    )

    assert receipt.marker_hits == (("server", "API_COMPLETE", 2),)
    assert receipt.loaded_method_ids == ("slow-execution",)
    assert verified.draft.evidence[0].sources[0].line == cited
    assert verified.draft.evidence[0].sources[0].marker == "API_COMPLETE"

    changed_identity = _diagnosis(line=cited)
    changed_identity["evidence"][0]["identity_tokens"] = ["REQUEST_ID=42"]
    with pytest.raises(ValueError, match="same evidence"):
        verify_method_diagnosis(
            skill=skill,
            draft=changed_identity,
            target_logs=logs,
            logparse_receipt_sha256=RECEIPT_SHA256,
            skill_load=receipt,
        )


def test_grounding_rejects_marker_owned_only_by_another_method(
    tmp_path: Path,
) -> None:
    skill = load_specialized_skill_registration(_write_registration(tmp_path / "skills"))
    cited = "2026-08-23T10:00:05Z unrelated_positive request_id=42"
    logs = (_target("server", f"API_COMPLETE request_id=99\n{cited}\n"),)
    draft = _diagnosis(line=cited)
    draft["evidence"][0]["sources"][0]["marker"] = "UNRELATED_POSITIVE"
    receipt = scan_method_markers(skill=skill, target_logs=logs)

    assert receipt.loaded_method_ids == ("slow-execution", "unrelated-method")
    with pytest.raises(ValueError, match="not indexed by its method"):
        verify_method_diagnosis(
            skill=skill,
            draft=draft,
            target_logs=logs,
            logparse_receipt_sha256=RECEIPT_SHA256,
            skill_load=receipt,
        )


def test_grounding_allows_a_literal_shared_by_multiple_methods(
    tmp_path: Path,
) -> None:
    registration = _write_registration(tmp_path / "skills")
    methods_path = registration / "package/diagnose-test-timeout/methods.json"
    methods_value = json.loads(methods_path.read_text(encoding="utf-8"))
    methods_value["methods"][1]["evidence_markers"] = ["API_COMPLETE"]
    _write_json(methods_path, methods_value)
    skill = load_specialized_skill_registration(registration)
    cited = "2026-08-23T10:00:05Z api_complete request_id=42"
    logs = (_target("server", f"noise\n{cited}\n"),)
    receipt = scan_method_markers(skill=skill, target_logs=logs)

    verified = verify_method_diagnosis(
        skill=skill,
        draft=_diagnosis(line=cited),
        target_logs=logs,
        logparse_receipt_sha256=RECEIPT_SHA256,
        skill_load=receipt,
    )

    assert receipt.loaded_method_ids == ("slow-execution", "unrelated-method")
    assert receipt.marker_hits == (
        ("server", "API_COMPLETE", 2),
        ("server", "API_COMPLETE", 2),
    )
    assert verified.draft.confirmed_methods == ("slow-execution",)
    assert verified.draft.evidence[0].sources[0].marker == "API_COMPLETE"


def test_grounded_methods_are_mapped_by_the_server_into_candidate_domain(
    tmp_path: Path,
) -> None:
    skill = load_specialized_skill_registration(_write_registration(tmp_path / "skills"))
    cited = "2026-08-23T10:00:05Z api_complete request_id=42 cost_us=6500000"
    content = f"noise\n{cited}\n".encode("utf-8")
    frozen = _target("caller", content.decode("utf-8"))
    diagnosis_value = _diagnosis(line=cited)
    diagnosis_value["limitations"] = [
        "Only the frozen target logs were observable for this method."
    ]
    diagnosis_value["safety_notes"] = [
        "A timeout does not prove that downstream execution was cancelled."
    ]
    diagnosis_value["evidence"][0]["sources"][0]["source_id"] = "caller"
    skill_load = scan_method_markers(skill=skill, target_logs=(frozen,))
    verified = verify_method_diagnosis(
        skill=skill,
        draft=diagnosis_value,
        target_logs=(frozen,),
        logparse_receipt_sha256=RECEIPT_SHA256,
        skill_load=skill_load,
    )

    assert skill_load.marker_hits == (("caller", "API_COMPLETE", 2),)
    assert skill_load.loaded_method_ids == ("slow-execution",)
    assert verified.draft.evidence[0].sources[0].marker == "API_COMPLETE"

    repository_root = Path(__file__).resolve().parents[4]
    job = parse_canonical_json_bytes(
        (repository_root / "tests/fixtures/contracts/positive/job-diagnose.json").read_bytes(),
        model_type=Job,
    )
    job_value = job.model_dump(mode="json")
    job_value["skill_ref"] = {
        "id": f"diagnosis-skill/{skill.registration_id}",
        "version": skill.registration.version,
        "content_hash": skill.combined_sha256,
    }
    job = Job.model_validate(job_value)
    manifest = parse_canonical_json_bytes(
        (
            repository_root
            / "tests/fixtures/contracts/positive/workspace-input-manifest.json"
        ).read_bytes(),
        model_type=WorkspaceInputManifest,
    )
    artifact_id = job.artifact_refs[0]
    target = AuthoritativeTargetLog(
        ordinal=1,
        label="caller",
        requested_module="payment",
        requested_slot="request",
        requested_process_name="payment-service",
        requested_pid=None,
        module_key="payment",
        module_name="payment",
        slot="request",
        process_name="payment-service",
        pid=None,
        match_status="exact",
        board_cycle=None,
        cpu_id=None,
        cpu_cycle=None,
        caveats=(),
        source_kind="INPUT_ARTIFACT",
        source_ref=artifact_id,
        source_root=f"inputs/artifacts/{artifact_id}/tree",
        log_path="caller.log",
        archive_name="caller__payment__slot_request__payment-service.log",
    )
    binding = EvidenceBinding(
        existing_evidence_id=None,
        evidence_proposal_key="methods-target-1",
    )
    captured = CapturedTargetLog(
        target=target,
        content=content,
        evidence_bindings=(binding,),
    )
    validated = ValidatedMethodsPreprocessing(
        request_bytes=canonical_json_bytes({"schema_version": 1}),
        broker_audit_bytes=canonical_json_bytes({"schema_version": 1}),
        authoritative_targets=AuthoritativeTargetSet(
            problem_time="2026-07-31T00:00:00.000Z",
            targets=(target,),
            source_size=512,
            source_sha256="1" * 64,
        ),
        target_logs=(captured,),
        proposal_resources=(),
    )
    source_bytes = canonical_json_bytes(diagnosis_value)
    mapped = map_verified_methods_draft(
        job=job,
        manifest=manifest,
        source_draft_bytes=source_bytes,
        verified_diagnosis=verified,
        preprocessing=SimpleNamespace(validated=validated),
    )

    candidate = mapped.draft.payload.candidate_conclusion_draft
    assert mapped.draft_bytes == source_bytes
    assert candidate is not None
    assert candidate.terminal_path_id == "methods_complete"
    assert candidate.causal_factors[0].factor_id == "slow_execution"
    assert mapped.verification.positive_gate_passed is True
    assert mapped.verification.audit.required_evidence_bindings == [binding]
    assert mapped.draft.proposed_evidence_drafts[0].source_binding.existing_source_ref == artifact_id
    assert mapped.draft.payload.limitations == diagnosis_value["limitations"]
    assert mapped.draft.payload.safety_notes == diagnosis_value["safety_notes"]
    assert b'"raw_line":"2026-08-23T10:00:05Z api_complete request_id=42' in (
        mapped.verification.decision_evidence_bytes
    )
    bundle = build_server_result_bundle(
        job=job,
        result_type=mapped.draft.result_type,
        payload=mapped.draft.payload,
        verification=mapped.verification,
        authoritative_targets=validated.authoritative_targets,
        captured_logs=validated.target_logs,
    )
    assert bundle.report.limitations == diagnosis_value["limitations"]
    assert bundle.report.safety_notes == diagnosis_value["safety_notes"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["evidence"][0]["sources"][0].update({"line_number": 1}), "differs"),
        (lambda value: value["evidence"][0]["sources"][0].update({"marker": "UNKNOWN"}), "not indexed"),
        (lambda value: value["evidence"][0].update({"identity_tokens": ["request_id=99"]}), "same evidence"),
        (lambda value: value.update({"confirmed_methods": ["unknown-method"]}), "unknown methods"),
    ],
)
def test_grounding_rejects_ungrounded_claims(
    tmp_path: Path,
    mutation: Any,
    message: str,
) -> None:
    skill = load_specialized_skill_registration(_write_registration(tmp_path / "skills"))
    cited = "2026-08-23T10:00:05Z API_COMPLETE request_id=42 cost_us=6500000"
    draft = _diagnosis(line=cited)
    mutation(draft)
    with pytest.raises(ValueError, match=message):
        verify_method_diagnosis(
            skill=skill,
            draft=draft,
            target_logs=(_target("server", f"noise\n{cited}\n"),),
            logparse_receipt_sha256=RECEIPT_SHA256,
            skill_load=scan_method_markers(
                skill=skill,
                target_logs=(_target("server", f"noise\n{cited}\n"),),
            ),
        )


def test_registration_hash_drift_changes_combined_identity(tmp_path: Path) -> None:
    root = _write_registration(tmp_path / "skills")
    before = load_specialized_skill_registration(root)
    path = root / "registration-template.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["summary"] = "Changed product-owned summary."
    _write_json(path, value)
    after = load_specialized_skill_registration(root)

    assert after.package_tree_sha256 == before.package_tree_sha256
    assert after.registration_sha256 != before.registration_sha256
    assert after.combined_sha256 != before.combined_sha256


class _BrokerFactory:
    def open(self, job: Any, workspace_root: Path, workspace_manifest: Any, cancellation: Any) -> Any:
        raise AssertionError("catalog construction must not open Logparse")


def test_catalog_routes_registered_methods_skill_for_empty_partial_and_extra_facts(
    tmp_path: Path,
) -> None:
    store = tmp_path / "skills"
    _write_registration(store)
    logparse_root = tmp_path / "logparse"
    logparse_root.mkdir()
    (logparse_root / "identity.txt").write_text("test\n", encoding="utf-8")
    logparse = ResolvedAsset(
        ref=VersionedRef(
            id="logparse-tool/test",
            version="1.0.0",
            content_hash=hash_product_directory(logparse_root),
        ),
        asset_kind=AssetKind.LOGPARSE_TOOL,
        root_path=str(logparse_root),
    )
    catalog = VersionedAssetCatalog(
        skill_dir=store,
        assets_root=BUILTIN_ASSET_ROOT,
        logparse_tool=logparse,
        logparse_broker_factory=_BrokerFactory(),
        generic_skill_name="generic-problem-locator-smoke",
    )

    routes = [
        catalog.route_bindings(names)
        for names in (
            [],
            ["problem_time"],
            ["problem_time", "client_process", "server_process", "order_id"],
        )
    ]
    assert all(len(route.available_skill_refs) == 1 for route in routes)
    skill_ref = routes[0].available_skill_refs[0]
    assert all(route.available_skill_refs[0] == skill_ref for route in routes)
    assert skill_ref.id == "diagnosis-skill/test-timeout"
    assert catalog.resolve(skill_ref).root_path == str((store / "test-timeout").resolve())
    assert catalog.resolved_specialized_skill(skill_ref).registration_id == "test-timeout"
    resolved_asset = catalog.resolve(skill_ref)
    rendered = _load_entry_text(resolved_asset, skill_ref, AssetKind.DIAGNOSIS_SKILL)
    assert '<<<METHODS_SKILL_FILE path="SKILL.md">>>' in rendered
    assert '<<<METHODS_SKILL_FILE path="methods.json">>>' in rendered
    assert '<<<METHODS_SKILL_FILE path="references/slow-execution.md">>>' in rendered
    index = _skill_index_entry(resolved_asset, skill_ref)
    assert "registration_id" not in index
    assert index["ref"] == skill_ref.model_dump(mode="json")
    assert index["required_user_inputs"] == [
        "problem_time",
        "client_process",
        "server_process",
    ]
    diagnose = catalog.diagnose_bindings(skill_ref)
    assert diagnose.diagnosis_mode is DiagnosisMode.SPECIALIZED
    assert diagnose.logparse_product == "test-timeout"
    assert diagnose.logparse_tool_ref == logparse.ref
    review = catalog.review_bindings(skill_ref)
    assert review.skill_ref == skill_ref
    assert review.logparse_tool_ref is None


def test_catalog_rejects_legacy_skill_directory(tmp_path: Path) -> None:
    store = tmp_path / "skills"
    legacy = store / "old-skill"
    legacy.mkdir(parents=True)
    (legacy / "diagnosis-skill.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="legacy diagnosis-skill.json"):
        VersionedAssetCatalog(
            skill_dir=store,
            assets_root=BUILTIN_ASSET_ROOT,
            generic_skill_name="generic-problem-locator-smoke",
        )


def test_catalog_rejects_test_only_and_missing_logparse_pair(tmp_path: Path) -> None:
    store = tmp_path / "skills"
    root = _write_registration(store)
    template = root / "registration-template.json"
    value = json.loads(template.read_text(encoding="utf-8"))
    value["deployment_scope"] = "TEST_ONLY"
    _write_json(template, value)
    with pytest.raises(ValueError, match="TEST_ONLY diagnosis skills are forbidden"):
        VersionedAssetCatalog(
            skill_dir=store,
            assets_root=BUILTIN_ASSET_ROOT,
            generic_skill_name="generic-problem-locator-smoke",
        )

    value["deployment_scope"] = "PRODUCTION"
    _write_json(template, value)
    with pytest.raises(ValueError, match="paired logparse asset"):
        VersionedAssetCatalog(
            skill_dir=store,
            assets_root=BUILTIN_ASSET_ROOT,
            generic_skill_name="generic-problem-locator-smoke",
        )


def test_catalog_supports_empty_specialized_store_for_generic_mode(tmp_path: Path) -> None:
    store = tmp_path / "skills"
    store.mkdir()
    catalog = VersionedAssetCatalog(
        skill_dir=store,
        assets_root=BUILTIN_ASSET_ROOT,
        generic_skill_name="generic-problem-locator-smoke",
    )
    assert catalog.route_bindings().available_skill_refs == []
    assert catalog.generic_diagnose_bindings().diagnosis_mode is DiagnosisMode.GENERIC


def test_product_hash_rejects_links_and_detects_content_drift(tmp_path: Path) -> None:
    root = tmp_path / "product"
    root.mkdir()
    entry = root / "entry.txt"
    entry.write_text("before\n", encoding="utf-8")
    before = hash_product_directory(root)
    entry.write_text("after\n", encoding="utf-8")
    assert hash_product_directory(root) != before

    try:
        os.symlink(entry, root / "alias.txt")
    except OSError as exc:  # pragma: no cover - platform privilege dependent
        pytest.skip(f"symlink creation unavailable: {exc}")
    with pytest.raises(ValueError, match="links are forbidden"):
        hash_product_directory(root)


def test_catalog_marks_registration_or_package_drift_unavailable(tmp_path: Path) -> None:
    store = tmp_path / "skills"
    root = _write_registration(store)
    logparse_root = tmp_path / "logparse"
    logparse_root.mkdir()
    (logparse_root / "identity.txt").write_text("test\n", encoding="utf-8")
    logparse = ResolvedAsset(
        ref=VersionedRef(
            id="logparse-tool/test",
            version="1.0.0",
            content_hash=hash_product_directory(logparse_root),
        ),
        asset_kind=AssetKind.LOGPARSE_TOOL,
        root_path=str(logparse_root),
    )
    catalog = VersionedAssetCatalog(
        skill_dir=store,
        assets_root=BUILTIN_ASSET_ROOT,
        logparse_tool=logparse,
        logparse_broker_factory=_BrokerFactory(),
        generic_skill_name="generic-problem-locator-smoke",
    )
    ref = catalog.route_bindings().available_skill_refs[0]
    package_entry = root / "package/diagnose-test-timeout/SKILL.md"
    package_entry.write_text(package_entry.read_text(encoding="utf-8") + "\ndrift\n", encoding="utf-8")
    assert catalog.check([ref]).missing_refs == [ref]
    with pytest.raises(ApplicationPortError):
        catalog.resolve(ref)


def test_runtime_catalog_fixture_manifest_matches_methods_layout() -> None:
    fixture_root = (
        Path(__file__).resolve().parents[4]
        / "tests/fixtures/components/runtime-catalog"
    )
    manifest_path = fixture_root / "fixture-manifest.json"
    manifest = FixtureManifest.model_validate_json(manifest_path.read_bytes())
    actual = {
        path.relative_to(fixture_root).as_posix(): path
        for path in fixture_root.rglob("*")
        if path.is_file() and path != manifest_path
    }
    assert [item.path for item in manifest.files] == sorted(actual)
    for item in manifest.files:
        payload = actual[item.path].read_bytes()
        assert item.size == len(payload)
        assert item.sha256 == hashlib.sha256(payload).hexdigest()
