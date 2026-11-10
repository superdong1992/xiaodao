from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from problem_locator.contracts import (
    ApplicationPortError, AssetKind, ErrorCode, Job, JobType, ResolvedAsset, ReviewPolicy,
    VersionedRef, is_specialized_direct,
)
from problem_locator.runtime.catalog import VersionedAssetCatalog
from problem_locator.runtime.catalog_hash import hash_product_directory
from problem_locator.runtime.context_builder import ContextBuilder
from problem_locator.runtime.context_policy import RuntimeAssetResolver, _load_entry_text, _skill_index_entry
from problem_locator.runtime.document_skill import document_source_sha256, load_document_skill_registration
from problem_locator.runtime.specialized_skill import is_document_skill, load_specialized_skill_registration
from tests.document_skill_helpers import document_input, document_registration, write_document_registration


def _write(root: Path, value: dict) -> None:
    (root / "registration-template.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class _UnusedBrokerFactory:
    def open(self, *args, **kwargs):
        raise AssertionError("注册与绑定不应调用 logparse。")


def _catalog(tmp_path: Path, *, mode="none", evidence_policy="off", user_inputs=None):
    root = write_document_registration(tmp_path / "skills", mode=mode, user_inputs=user_inputs)
    kwargs = {}
    if mode == "logparse":
        tool_root = tmp_path / "logparse"
        tool_root.mkdir()
        (tool_root / "identity.txt").write_text("test logparse", encoding="utf-8")
        kwargs = {"logparse_tool": ResolvedAsset(
            ref=VersionedRef(id="logparse-tool/document-test", version="1.0.0", content_hash=hash_product_directory(tool_root)),
            asset_kind=AssetKind.LOGPARSE_TOOL, root_path=str(tool_root)), "logparse_broker_factory": _UnusedBrokerFactory()}
    catalog = VersionedAssetCatalog(skill_dir=root.parent, generic_skill_name="generic-problem-locator-smoke",
        methods_evidence_validation=evidence_policy, specialized_reviewer_enabled=True, **kwargs)
    ref = catalog.route_bindings().available_skill_refs[0]
    return root, catalog, ref


@pytest.mark.parametrize("mode", ["none", "direct_files", "logparse"])
def test_document_registration_does_not_require_methods_or_logparse_files(tmp_path: Path, mode: str) -> None:
    root = write_document_registration(tmp_path, mode=mode)
    skill = load_specialized_skill_registration(root)
    assert is_document_skill(skill)
    assert not hasattr(skill, "methods")
    assert skill.document_paths == ("SKILL.md", "references/diagnosis.md")
    assert skill.registration.preprocessing.mode == mode
    assert skill.registration.preprocessing.requires_logparse is (mode == "logparse")
    assert skill.combined_sha256 == load_document_skill_registration(root).combined_sha256


def test_package_and_registration_names_may_differ(tmp_path: Path) -> None:
    root = write_document_registration(tmp_path, "service-triage", skill_name="diagnose-service")
    skill = load_document_skill_registration(root)
    assert skill.registration_id == "service-triage"
    assert skill.registration.skill_name == "diagnose-service"


@pytest.mark.parametrize("keywords", [None, [], ["SERVICE_QUEUE_STALLED", "队列阻塞告警"]])
def test_document_registration_preserves_optional_direct_match_keywords(tmp_path: Path, keywords) -> None:
    value = document_registration()
    original_routing = deepcopy(value["routing"])
    if keywords is not None:
        value["routing"]["direct_match_keywords"] = keywords
    root = write_document_registration(tmp_path, registration=value)
    skill = load_document_skill_registration(root)
    assert skill.registration.routing.direct_match_keywords == tuple(keywords or [])
    ref = VersionedRef(id="diagnosis-skill/document-triage", version="1.0.0", content_hash=skill.combined_sha256)
    expected = dict(original_routing)
    if keywords:
        expected["direct_match_keywords"] = keywords
    assert _skill_index_entry(skill, ref)["routing"] == expected


@pytest.mark.parametrize("keywords", [None, "SERVICE_QUEUE_STALLED", ["ERROR"], ["bad\x00keyword"]])
def test_document_registration_rejects_invalid_direct_match_keywords(tmp_path: Path, keywords) -> None:
    value = document_registration()
    value["routing"]["direct_match_keywords"] = keywords
    root = write_document_registration(tmp_path, registration=value)
    with pytest.raises(ValueError):
        load_document_skill_registration(root)


@pytest.mark.parametrize("policy", ["off", "advisory", "strict"])
@pytest.mark.parametrize("mode", ["none", "direct_files", "logparse"])
def test_v3_catalog_always_freezes_direct_bindings(tmp_path: Path, policy: str, mode: str) -> None:
    _, catalog, ref = _catalog(tmp_path, mode=mode, evidence_policy=policy)
    bindings = catalog.diagnose_bindings(ref)
    assert is_specialized_direct(bindings)
    assert bindings.review_policy is ReviewPolicy.NONE
    assert bindings.generic_log_preparation is None
    assert (bindings.logparse_tool_ref is not None) is (mode == "logparse")
    with pytest.raises(ApplicationPortError) as raised:
        catalog.review_bindings(ref)
    assert raised.value.error.code is ErrorCode.CONFIG_INVALID


def test_document_catalog_context_and_policy_use_frozen_snapshot(tmp_path: Path) -> None:
    root, catalog, ref = _catalog(tmp_path, user_inputs=[
        document_input("incident_id", prompt="请提供故障编号。"),
        document_input("region", required=False, allowed_values=["华南", "华北"]),
    ])
    skill = catalog.resolved_specialized_skill(ref)
    entry = _skill_index_entry(skill, ref)
    assert entry["required_user_inputs"] == ["incident_id"]
    assert entry["required_artifacts"] == []
    assert entry["requires_logparse"] is False
    policy = catalog.specialized_input_policy(ref)
    assert policy.skill_ref == ref
    assert [(item.name, item.prompt) for item in policy.optional_inputs] == [("region", "请提供 region。")]
    assert policy.optional_inputs[0].constraints.allowed_values == ["华南", "华北"]
    assert policy.attachment_mode == "none"
    asset = catalog.resolve(ref)
    before = _load_entry_text(asset, ref, AssetKind.DIAGNOSIS_SKILL, loaded_method_ids=())
    assert '<<<DOCUMENT_SKILL_FILE path="references/diagnosis.md">>>' in before
    assert "methods.json" not in before and "logparse-receipt.json" not in before
    (root / "package/document-triage/SKILL.md").write_text("changed", encoding="utf-8")
    assert _load_entry_text(asset, ref, AssetKind.DIAGNOSIS_SKILL) == before
    wrong_ref = ref.model_copy(update={"content_hash": "0" * 64})
    with pytest.raises(ApplicationPortError):
        catalog.specialized_input_policy(wrong_ref)


def test_document_logparse_context_preserves_previous_outcomes_and_problem(tmp_path: Path) -> None:
    from tests.deterministic.unit.runtime.test_context_builder import _base_job, _manifest, _previous_outcome

    _, catalog, ref = _catalog(tmp_path, mode="logparse", evidence_policy="strict")
    payload = _base_job(JobType.DIAGNOSE).model_dump(mode="json")
    payload.update(catalog.diagnose_bindings(ref).model_dump(mode="json"))
    payload["evidence_refs"] = []
    payload["context_snapshot"]["evidence_refs"] = []
    job = Job.model_validate(payload)
    previous = _previous_outcome(job)
    assert previous is not None
    manifest = _manifest(job, (), (previous,))
    assert manifest.resolved_logparse_plan is not None
    workspace = SimpleNamespace(manifest=manifest, previous_outcomes=(previous,), evidence=())
    materials = RuntimeAssetResolver(catalog).resolve_job(job).bind_workspace(workspace).materials
    assert materials.previous_outcomes == (previous,)
    context = ContextBuilder().build(job, materials)
    assert previous.error.message in context.body
    assert job.context_snapshot.problem_spec.statement in context.body
    assert materials.document_skill is True


@pytest.mark.parametrize("mutation", ["schema", "script", "methods", "reference_directory", "binary", "frontmatter_tool"])
def test_document_package_rejects_non_document_content(tmp_path: Path, mutation: str) -> None:
    root = write_document_registration(tmp_path)
    package = root / "package/document-triage"
    if mutation == "schema":
        value = document_registration()
        value["runtime"]["tool_bundle_id"] = "custom"
        _write(root, value)
    elif mutation == "script":
        (package / "run.py").write_text("print('x')", encoding="utf-8")
    elif mutation == "methods":
        (package / "methods.json").write_text("{}", encoding="utf-8")
    elif mutation == "reference_directory":
        (package / "references/nested.md").mkdir()
    elif mutation == "binary":
        (package / "references/diagnosis.md").write_bytes(b"\xff\x00")
    else:
        path = package / "SKILL.md"
        path.write_text(path.read_text(encoding="utf-8").replace("description:", "allowed-tools: Bash\ndescription:"), encoding="utf-8")
    with pytest.raises(ValueError):
        load_specialized_skill_registration(root)


@pytest.mark.parametrize("mutation", ["duplicate", "boolean", "constraint_type", "enum_pattern", "unknown_binding", "optional_time", "optional_required_role", "attachment_kind", "plan_on_none"])
def test_registration_rejects_ambiguous_or_invalid_input_contracts(tmp_path: Path, mutation: str) -> None:
    value = document_registration(mode="logparse")
    if mutation == "duplicate":
        value["inputs"]["fixed"] = [{"name": "problem_time", "value": "x"}]
    elif mutation == "boolean":
        value["inputs"]["user"][0]["required"] = "true"
    elif mutation == "constraint_type":
        value["inputs"]["user"][0]["constraints"]["min_utf8_bytes"] = True
    elif mutation == "enum_pattern":
        value["inputs"]["user"][0]["constraints"].update(pattern="[0-9]+", allowed_values=["wrong"])
    elif mutation == "unknown_binding":
        value["runtime"]["preprocessing"]["logparse_plan"]["problem_time_binding"]["name"] = "undeclared"
    elif mutation == "optional_time":
        value["inputs"]["user"][0]["required"] = False
    elif mutation == "optional_required_role":
        value["inputs"]["user"][1]["required"] = False
    elif mutation == "attachment_kind":
        value["attachment"]["allowed_content_types"] = ["text/plain"]
    else:
        value["runtime"]["preprocessing"]["mode"] = "none"
        value["attachment"] = None
    root = write_document_registration(tmp_path, registration=value)
    with pytest.raises(ValueError):
        load_document_skill_registration(root)


def test_optional_role_and_fixed_derived_groups_remain_distinct(tmp_path: Path) -> None:
    value = document_registration(mode="logparse")
    value["inputs"]["user"].append(document_input("peer_process", required=False))
    value["inputs"]["fixed"] = [{"name": "product_line", "value": "service"}]
    value["inputs"]["derived"] = [{"name": "timeout_count", "description": "根据日志统计超时次数。"}]
    preprocessing = value["runtime"]["preprocessing"]
    preprocessing["roles"].append({"label": "peer", "description": "相关对端", "presence": "OPTIONAL", "source_reference": "SKILL.md"})
    peer = deepcopy(preprocessing["logparse_plan"]["anchors"][0])
    peer.update(label="peer", process_name={"source": "USER_FACT", "name": "peer_process"})
    preprocessing["logparse_plan"]["anchors"].append(peer)
    skill = load_document_skill_registration(write_document_registration(tmp_path, registration=value))
    assert skill.registration.inputs.user[-1].requiredness == "OPTIONAL"
    assert skill.registration.inputs.fixed[0].value == "service"
    assert skill.registration.inputs.derived[0].name == "timeout_count"


def test_source_and_generated_registration_have_independent_hashes(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki.md"
    original = "原始 Wiki\r\n".encode("utf-8")
    wiki.write_bytes(original)
    assert document_source_sha256(wiki, "WIKI") == hashlib.sha256(original).hexdigest()
    root = write_document_registration(tmp_path / "skills")
    skill = load_document_skill_registration(root)
    assert document_source_sha256(skill.package_root, "SKILL") == skill.package_tree_sha256
    original_digest = skill.combined_sha256
    value = json.loads((root / "registration-template.json").read_text(encoding="utf-8"))
    value["inputs"]["user"] = [document_input("region", required=False)]
    _write(root, value)
    assert load_document_skill_registration(root).combined_sha256 != original_digest


def test_existing_document_source_allows_minimal_and_nested_layout(tmp_path: Path) -> None:
    root = tmp_path / "arbitrary-source-directory"
    root.mkdir()
    (root / "SKILL.md").write_text("# 既有定位步骤\n\n先检查服务状态。\n", encoding="utf-8")
    assert document_source_sha256(root, "SKILL") == hash_product_directory(root)
    (root / "references/details").mkdir(parents=True)
    (root / "references/details/service.md").write_text("# 服务状态\n\n检查服务退出原因。\n", encoding="utf-8")
    (root / "SKILL.md").write_text("# 既有定位步骤\n\n读取 [定位步骤](references/details/service.md)。\n", encoding="utf-8")
    assert document_source_sha256(root, "SKILL") == hash_product_directory(root)


@pytest.mark.parametrize("target", ["references/missing.md", "../../outside.md", "file:///etc/passwd", "%2e%2e/outside.md"])
def test_document_loader_rejects_missing_or_escaping_local_references(tmp_path: Path, target: str) -> None:
    root = write_document_registration(tmp_path)
    path = root / "package/document-triage/SKILL.md"
    path.write_text(path.read_text(encoding="utf-8") + f"\n读取 [细节]({target})。\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_document_skill_registration(root)


def test_legacy_methods_package_still_uses_legacy_loader(tmp_path: Path) -> None:
    fixture = Path(__file__).parents[3] / "fixtures/components/runtime-catalog/skill-dir/manual-triage"
    root = tmp_path / "manual-triage"
    shutil.copytree(fixture, root)
    skill = load_specialized_skill_registration(root)
    assert not is_document_skill(skill)
    assert skill.methods.skill_name == "manual-triage"
    catalog = VersionedAssetCatalog(skill_dir=tmp_path, generic_skill_name="generic-problem-locator-smoke")
    ref = catalog.route_bindings().available_skill_refs[0]
    assert catalog.specialized_input_policy(ref) is None
