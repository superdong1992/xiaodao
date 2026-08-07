from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from problem_locator.runtime import catalog as catalog_module
from problem_locator.contracts import (
    AgentJobOutcome,
    ApplicationPortError,
    AssetCatalogPort,
    AssetKind,
    ErrorCode,
    DiagnosisOutcome,
    DiagnosisStateDelta,
    FixtureManifest,
    JobType,
    PORT_ERROR_CODES,
    ReviewAssessment,
    RouteDecision,
    ResolvedAsset,
    VersionedRef,
    bytes_sha256,
    canonical_json_bytes,
    canonical_json_sha256,
    default_resource_limits,
)
from problem_locator.runtime.catalog import (
    BUILTIN_ASSET_ROOT,
    VersionedAssetCatalog,
    hash_product_directory,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests/fixtures/components/runtime-catalog"
SKILL_DIR = FIXTURE_ROOT / "skill-dir"
LOGPARSE_ROOT = FIXTURE_ROOT / "logparse-tool"


class _BrokerFactory:
    def open(self, job: Any, workspace_root: Path, workspace_manifest: Any, cancellation: Any) -> Any:
        raise AssertionError("catalog construction must not open a broker session")


def _logparse_asset(*, kind: AssetKind = AssetKind.LOGPARSE_TOOL) -> ResolvedAsset:
    return ResolvedAsset(
        ref=VersionedRef(
            id="logparse-tool/fake",
            version="3.4.5",
            content_hash=hash_product_directory(LOGPARSE_ROOT),
        ),
        asset_kind=kind,
        root_path=str(LOGPARSE_ROOT),
    )


def _catalog() -> VersionedAssetCatalog:
    return VersionedAssetCatalog(
        skill_dir=SKILL_DIR,
        logparse_tool=_logparse_asset(),
        logparse_broker_factory=_BrokerFactory(),
    )


def _assert_catalog_error(
    caught: pytest.ExceptionInfo[ApplicationPortError],
    *,
    operation: str,
    code: ErrorCode,
) -> None:
    assert caught.value.error.code is code
    assert caught.value.error.retryable is False
    assert caught.value.error.details == []
    assert code in PORT_ERROR_CODES[f"AssetCatalogPort.{operation}"]
    assert not isinstance(caught.value, LookupError)


def _write_skill(
    root: Path,
    *,
    skill_id: str = "test-skill",
    version: str = "1.0.0",
    requires_logparse: bool = False,
    logparse_product: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root.mkdir(parents=True)
    manifest: dict[str, Any] = {
        "schema_version": 2,
        "id": skill_id,
        "version": version,
        "capability": "test-capability",
        "summary": "A deterministic test skill.",
        "entry_document": "SKILL.md",
        "tool_bundle_id": "tool-bundle/diagnose",
        "requires_logparse": requires_logparse,
        "requirements": [],
        "logparse_plan": None,
    }
    if requires_logparse:
        manifest["requirements"] = [
            {
                "name": "problem_time",
                "kind": "INPUT",
                "stage": "INITIAL",
                "fulfillment_source": "USER_FACT",
                "prompt": "Provide problem time.",
                "constraints": {
                    "value_type": "STRING",
                    "min_utf8_bytes": 1,
                    "max_utf8_bytes": 256,
                    "pattern": None,
                    "allowed_values": [],
                },
            }
        ]
        manifest["logparse_plan"] = {
            "attachment_requirement": None,
            "problem_time_binding": {"source": "USER_FACT", "name": "problem_time"},
            "anchors": [
                {
                    "label": "target",
                    "module": {"source": "SKILL_FIXED", "value": "module"},
                    "slot": {"source": "SKILL_FIXED", "value": "slot"},
                    "process_name": {"source": "SKILL_FIXED", "value": "process"},
                    "pid": None,
                }
            ],
        }
        if logparse_product is not None:
            manifest["logparse_product"] = logparse_product
    if extra:
        manifest.update(extra)
    (root / "diagnosis-skill.json").write_bytes(canonical_json_bytes(manifest))
    (root / "SKILL.md").write_text("# Test skill\n", encoding="utf-8")
    return manifest


def test_builtin_assets_and_port_use_exact_versioned_refs() -> None:
    catalog = _catalog()
    assert isinstance(catalog, AssetCatalogPort)

    route = catalog.route_bindings()
    expected_builtin_ids = {
        "agent-profile/router",
        "agent-profile/specialist",
        "agent-profile/reviewer",
        "tool-bundle/router",
        "tool-bundle/diagnose",
        "tool-bundle/review",
        "context-policy/route",
        "context-policy/diagnose",
        "context-policy/review",
        "output-contract/route",
        "output-contract/diagnose",
        "output-contract/review",
    }
    refs = {
        route.agent_profile_ref.id: route.agent_profile_ref,
        route.tool_bundle_ref.id: route.tool_bundle_ref,
        route.context_policy_ref.id: route.context_policy_ref,
        route.output_contract_ref.id: route.output_contract_ref,
    }
    for skill_ref in route.available_skill_refs:
        diagnose = catalog.diagnose_bindings(skill_ref)
        review = catalog.review_bindings(skill_ref)
        for ref in (
            diagnose.agent_profile_ref,
            diagnose.tool_bundle_ref,
            diagnose.context_policy_ref,
            diagnose.output_contract_ref,
            review.agent_profile_ref,
            review.tool_bundle_ref,
            review.context_policy_ref,
            review.output_contract_ref,
        ):
            refs[ref.id] = ref

    assert set(refs) == expected_builtin_ids
    upgraded_versions = {
        "tool-bundle/router": "1.0.1",
        "tool-bundle/diagnose": "1.0.1",
        "tool-bundle/review": "1.0.1",
        "output-contract/route": "1.0.1",
        "output-contract/diagnose": "2.0.4",
        "output-contract/review": "1.0.1",
    }
    for ref in refs.values():
        assert ref.version == upgraded_versions.get(ref.id, "1.0.0")
        resolved = catalog.resolve(ref)
        assert resolved.ref == ref
        assert Path(resolved.root_path).is_dir()

    exact = route.agent_profile_ref
    wrong_version = exact.model_copy(update={"version": "9.9.9"})
    wrong_hash = exact.model_copy(update={"content_hash": "0" * 64})
    report = catalog.check([exact, wrong_version, wrong_hash, wrong_version])
    assert report.available is False
    assert report.missing_refs == [wrong_version, wrong_hash, wrong_version]
    with pytest.raises(ApplicationPortError) as wrong_version_error:
        catalog.resolve(wrong_version)
    _assert_catalog_error(
        wrong_version_error,
        operation="resolve",
        code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
    )
    with pytest.raises(ApplicationPortError) as wrong_hash_error:
        catalog.resolve(wrong_hash)
    _assert_catalog_error(
        wrong_hash_error,
        operation="resolve",
        code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
    )


@pytest.mark.parametrize("role", ["route", "diagnose", "review"])
def test_builtin_output_contract_requires_canonical_agent_bytes(role: str) -> None:
    contract = (
        BUILTIN_ASSET_ROOT
        / "output-contracts"
        / role
        / "output-contract.md"
    ).read_text(encoding="utf-8")

    assert "V1 Canonical JSON" in contract
    assert "recursively sorts every nested object key" in contract or (
        "递归排序所有嵌套对象键" in contract
    )
    assert "problem-locator-finalize-outcome" in contract
    assert "finalization marker" in contract


@pytest.mark.parametrize("role", ["route", "diagnose", "review"])
def test_builtin_output_contract_pins_server_finalization_as_last_write(
    role: str,
) -> None:
    contract = (
        BUILTIN_ASSET_ROOT
        / "output-contracts"
        / role
        / "output-contract.md"
    ).read_text(encoding="utf-8")

    assert "Never create a temporary file at workspace root" in contract
    assert "exactly `inputs`, `runtime`, and `output`" in contract
    assert "`Write`" in contract
    assert "draft" in contract
    assert "problem-locator-finalize-outcome" in contract
    assert "Do not write or edit `output/job_outcome.json` after" in contract or (
        "命令成功后不得再增删改" in contract
    )


@pytest.mark.parametrize("role", ["route", "diagnose", "review"])
def test_builtin_output_contract_materializes_complete_agent_envelope(role: str) -> None:
    contract = (
        BUILTIN_ASSET_ROOT
        / "output-contracts"
        / role
        / "output-contract.md"
    ).read_text(encoding="utf-8")

    assert "exactly these twelve fields and no others" in contract
    expected_fields = json.dumps(
        sorted(AgentJobOutcome.model_fields),
        separators=(",", ":"),
    )
    assert f"`{expected_fields}`" in contract
    assert "from `JOB_INSTRUCTION`" in contract
    assert "from `RESOURCE_MANIFEST.case_id`" in contract
    assert "fresh lowercase UUID" in contract
    assert "current real UTC timestamp with exactly millisecond precision" in contract
    assert "never reuse the Job or Case ID as the Outcome ID" in contract
    assert contract.count("{{S00_AGENT_JOB_OUTCOME_SCHEMA_JSON}}") == 1
    assert contract.count("<<<BEGIN S00 AGENT JOB OUTCOME SCHEMA>>>") == 1
    assert contract.count("<<<END S00 AGENT JOB OUTCOME SCHEMA>>>") == 1
    assert contract.count("{{S00_USER_RESULT_SCHEMA_JSON}}") == (
        1 if role == "diagnose" else 0
    )


def test_builtin_route_output_contract_materializes_result_type_rules() -> None:
    contract = (
        BUILTIN_ASSET_ROOT
        / "output-contracts"
        / "route"
        / "output-contract.md"
    ).read_text(encoding="utf-8")

    assert "For a `MATCHED` decision, set `result_type` to `COMPLETED`." in contract
    assert (
        "For a `NO_CAPABILITY` decision, set `result_type` to `NO_CAPABILITY`."
        in contract
    )
    assert "`REROUTE` is forbidden for ROUTE jobs" in contract
    assert "exactly a `RouteDecision` object with the four fields" in contract
    expected_fields = json.dumps(
        sorted(RouteDecision.model_fields),
        separators=(",", ":"),
    )
    assert f"`{expected_fields}`" in contract
    assert "`SKILL_INDEX.skills[i].ref`" in contract
    assert "Copy only that nested `ref` object" in contract
    assert "never the enclosing skill-index entry" in contract
    assert "top-level `decision`, `kind`, `skill_ref`" in contract


def test_builtin_diagnose_output_contract_materializes_request_rules() -> None:
    contract = (
        BUILTIN_ASSET_ROOT
        / "output-contracts"
        / "diagnose"
        / "output-contract.md"
    ).read_text(encoding="utf-8")

    assert "`NEED_INPUT` 仅填写非空 `requested_input`" in contract
    assert "`NEED_ATTACHMENT` 仅填写非空" in contract
    assert "`COMPLETED` 和 `REROUTE`" in contract
    assert "每个 requested ID 必须对应" in contract
    assert "`state_delta.add_user_facts`" in contract
    assert "`state_delta.fulfill_requirements`" in contract
    assert "当前选中 Skill 的 `requirements` 声明" in contract
    assert "不得添加 Skill 未声明的 requirement" in contract
    assert "USER_RESULT_ARCHIVE" in contract
    assert "problem-locator-pack-result" in contract
    assert "REVIEW PASS" in contract
    for rpc_name in ("caller_service", "server_service", "rpc_method", "order_id"):
        assert rpc_name not in contract
    assert "problem-locator-finalize-outcome" in contract
    assert "重算其声明 size/hash" in contract
    assert "递归排序所有嵌套对象键" in contract


def test_builtin_review_output_contract_materializes_review_binding_rules() -> None:
    contract = (
        BUILTIN_ASSET_ROOT
        / "output-contracts"
        / "review"
        / "output-contract.md"
    ).read_text(encoding="utf-8")

    assert "A non-failed REVIEW outcome always uses `result_type` `COMPLETED`" in contract
    assert "Copy `candidate_conclusion_id`, `candidate_revision`" in contract
    assert "set `reviewed_state_revision`" in contract
    assert "PASS must review every supporting Evidence reference" in contract
    assert "REVIEW must not propose Evidence or Artifact drafts" in contract
    expected_fields = json.dumps(
        sorted(ReviewAssessment.model_fields),
        separators=(",", ":"),
    )
    assert f"`{expected_fields}`" in contract


def test_builtin_tool_bundles_all_declare_the_installed_finalizer() -> None:
    for role in ("router", "diagnose", "review"):
        bundle = json.loads(
            (
                BUILTIN_ASSET_ROOT
                / "tool-bundles"
                / role
                / "tool-bundle.json"
            ).read_bytes()
        )
        assert "problem-locator-finalize-outcome" in bundle["tools"]


def test_builtin_specialist_profile_separates_narrative_from_fixed_inputs() -> None:
    profile = (
        BUILTIN_ASSET_ROOT / "profiles" / "specialist" / "profile.md"
    ).read_text(encoding="utf-8")

    assert "never as supplied parameter facts" in profile
    assert "CONTEXT_SNAPSHOT.user_facts" in profile
    assert "provenance.input_name" in profile
    assert "never infer or copy a missing value from narrative text" in profile
    assert "only when the fixed `RESOURCE_MANIFEST` contains" in profile
    assert "A narrative mention of a resource never supplies that resource" in profile


def test_direct_child_skill_scan_bindings_and_deep_copy_isolation() -> None:
    catalog = _catalog()
    route = catalog.route_bindings()
    assert [ref.id for ref in route.available_skill_refs] == [
        "diagnosis-skill/manual-triage",
        "diagnosis-skill/rpc-log-analysis",
    ]
    assert route.skill_ref is None
    assert route.logparse_tool_ref is None
    assert route.logparse_product is None
    assert route.resource_limits == default_resource_limits(JobType.ROUTE)

    manual_ref, log_ref = route.available_skill_refs
    manual = catalog.diagnose_bindings(manual_ref)
    assert manual.skill_ref == manual_ref
    assert manual.available_skill_refs == []
    assert manual.logparse_tool_ref is None
    assert manual.logparse_product is None
    assert manual.resource_limits == default_resource_limits(JobType.DIAGNOSE)

    logparse = catalog.diagnose_bindings(log_ref)
    assert logparse.skill_ref == log_ref
    assert logparse.logparse_tool_ref == _logparse_asset().ref
    assert logparse.logparse_product == "payment-service"

    review = catalog.review_bindings(log_ref)
    assert review.skill_ref == log_ref
    assert review.logparse_tool_ref is None
    assert review.logparse_product is None
    assert review.resource_limits == default_resource_limits(JobType.REVIEW)

    route.available_skill_refs.clear()
    assert len(catalog.route_bindings().available_skill_refs) == 2
    resolved = catalog.resolve(log_ref)
    resolved.root_path = "changed-by-caller"
    assert catalog.resolve(log_ref).root_path != "changed-by-caller"
    with pytest.raises(ApplicationPortError) as missing_binding:
        catalog.diagnose_bindings(log_ref.model_copy(update={"content_hash": "f" * 64}))
    _assert_catalog_error(
        missing_binding,
        operation="diagnose_bindings",
        code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
    )


def test_logparse_asset_and_factory_must_be_paired() -> None:
    with pytest.raises(ValueError, match="supplied together"):
        VersionedAssetCatalog(skill_dir=SKILL_DIR, logparse_tool=_logparse_asset())
    with pytest.raises(ValueError, match="supplied together"):
        VersionedAssetCatalog(
            skill_dir=SKILL_DIR,
            logparse_broker_factory=_BrokerFactory(),
        )
    with pytest.raises(ValueError, match="asset_kind=LOGPARSE_TOOL"):
        VersionedAssetCatalog(
            skill_dir=SKILL_DIR,
            logparse_tool=_logparse_asset(kind=AssetKind.TOOL_BUNDLE),
            logparse_broker_factory=_BrokerFactory(),
        )


def test_required_logparse_skill_rejects_missing_pair(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills"
    _write_skill(
        skill_dir / "requires",
        requires_logparse=True,
        logparse_product="inventory-service",
    )
    with pytest.raises(ValueError, match="requires_logparse"):
        VersionedAssetCatalog(skill_dir=skill_dir)

    plain_skill_dir = tmp_path / "plain-skills"
    _write_skill(plain_skill_dir / "plain")
    catalog = VersionedAssetCatalog(skill_dir=plain_skill_dir)
    assert catalog.diagnose_bindings(catalog.route_bindings().available_skill_refs[0]).logparse_tool_ref is None


def test_logparse_skill_omitted_product_uses_effective_default(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills"
    _write_skill(skill_dir / "default-product", requires_logparse=True)
    catalog = VersionedAssetCatalog(
        skill_dir=skill_dir,
        logparse_tool=_logparse_asset(),
        logparse_broker_factory=_BrokerFactory(),
    )
    ref = catalog.route_bindings().available_skill_refs[0]
    assert catalog.diagnose_bindings(ref).logparse_product == "default"


def test_directory_hash_uses_complete_canonical_file_manifest(tmp_path: Path) -> None:
    root = tmp_path / "product"
    (root / "nested").mkdir(parents=True)
    files = {
        "asset.json": b'{"schema_version":1}\n',
        "entry.md": "unicode: 路由\n".encode(),
        "nested/data.bin": b"\x00\x01\xff",
    }
    for relative, data in files.items():
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / ".DS_Store").write_bytes(b"ignored")
    (root / "ignored.pyc").write_bytes(b"ignored")
    (root / "__pycache__").mkdir()
    (root / "__pycache__/cached.pyc").write_bytes(b"ignored")
    (root / ".pytest_cache").mkdir()
    (root / ".pytest_cache/state").write_bytes(b"ignored")
    (root / ".managed").write_bytes(b"ignored")
    (root / ".managed.source").write_bytes(b"ignored")
    (root / ".codex-managed").write_bytes(b"ignored")

    entries = [
        {
            "path": relative,
            "size": len(data),
            "sha256": bytes_sha256(data),
        }
        for relative, data in sorted(files.items())
    ]
    expected = canonical_json_sha256({"version": 1, "entries": entries})
    assert hash_product_directory(root) == expected

    before = hash_product_directory(root)
    (root / "entry.md").write_text("changed\n", encoding="utf-8")
    assert hash_product_directory(root) != before


def test_hash_drift_never_resolves_the_previous_ref(tmp_path: Path) -> None:
    assets_root = tmp_path / "assets"
    shutil.copytree(BUILTIN_ASSET_ROOT, assets_root)
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    first = VersionedAssetCatalog(skill_dir=skill_dir, assets_root=assets_root)
    old_ref = first.route_bindings().agent_profile_ref

    with (assets_root / "profiles/router/profile.md").open("ab") as stream:
        stream.write(b"\nconfiguration drift\n")
    second = VersionedAssetCatalog(skill_dir=skill_dir, assets_root=assets_root)
    new_ref = second.route_bindings().agent_profile_ref
    assert new_ref.id == old_ref.id
    assert new_ref.version == old_ref.version
    assert new_ref.content_hash != old_ref.content_hash
    assert second.check([old_ref]).missing_refs == [old_ref]
    with pytest.raises(ApplicationPortError) as missing:
        second.resolve(old_ref)
    _assert_catalog_error(
        missing,
        operation="resolve",
        code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
    )


def test_same_catalog_instance_rejects_product_drift_on_every_port_view(
    tmp_path: Path,
) -> None:
    assets_root = tmp_path / "assets"
    shutil.copytree(BUILTIN_ASSET_ROOT, assets_root)
    skill_dir = tmp_path / "skills"
    _write_skill(skill_dir / "fixed")
    catalog = VersionedAssetCatalog(
        skill_dir=skill_dir,
        assets_root=assets_root,
    )
    route = catalog.route_bindings()
    skill_ref = route.available_skill_refs[0]

    (assets_root / "profiles/router/profile.md").write_text(
        "drifted after startup\n",
        encoding="utf-8",
    )

    report = catalog.check([route.agent_profile_ref])
    assert report.available is False
    assert report.missing_refs == [route.agent_profile_ref]
    with pytest.raises(ApplicationPortError) as unavailable:
        catalog.resolve(route.agent_profile_ref)
    _assert_catalog_error(
        unavailable,
        operation="resolve",
        code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
    )
    with pytest.raises(ApplicationPortError) as route_corrupt:
        catalog.route_bindings()
    _assert_catalog_error(
        route_corrupt,
        operation="route_bindings",
        code=ErrorCode.CONFIG_INVALID,
    )

    (skill_dir / "fixed/SKILL.md").write_text(
        "skill drifted after startup\n",
        encoding="utf-8",
    )
    assert catalog.check([skill_ref]).missing_refs == [skill_ref]
    with pytest.raises(ApplicationPortError) as diagnose_missing:
        catalog.diagnose_bindings(skill_ref)
    _assert_catalog_error(
        diagnose_missing,
        operation="diagnose_bindings",
        code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
    )
    with pytest.raises(ApplicationPortError) as review_missing:
        catalog.review_bindings(skill_ref)
    _assert_catalog_error(
        review_missing,
        operation="review_bindings",
        code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
    )


def test_binding_configuration_damage_uses_only_typed_port_errors(
    tmp_path: Path,
) -> None:
    assets_root = tmp_path / "assets"
    shutil.copytree(BUILTIN_ASSET_ROOT, assets_root)
    skill_dir = tmp_path / "skills"
    _write_skill(skill_dir / "fixed")
    catalog = VersionedAssetCatalog(skill_dir=skill_dir, assets_root=assets_root)
    skill_ref = catalog.route_bindings().available_skill_refs[0]

    (assets_root / "tool-bundles/diagnose/tool-bundle.json").write_text(
        "corrupt after startup\n",
        encoding="utf-8",
    )
    with pytest.raises(ApplicationPortError) as diagnose_corrupt:
        catalog.diagnose_bindings(skill_ref)
    _assert_catalog_error(
        diagnose_corrupt,
        operation="diagnose_bindings",
        code=ErrorCode.CONFIG_INVALID,
    )

    (assets_root / "tool-bundles/review/tool-bundle.json").write_text(
        "corrupt after startup\n",
        encoding="utf-8",
    )
    with pytest.raises(ApplicationPortError) as review_corrupt:
        catalog.review_bindings(skill_ref)
    _assert_catalog_error(
        review_corrupt,
        operation="review_bindings",
        code=ErrorCode.CONFIG_INVALID,
    )

    catalog._builtin_refs.pop("agent-profile/router")
    with pytest.raises(ApplicationPortError) as route_missing_config:
        catalog.route_bindings()
    _assert_catalog_error(
        route_missing_config,
        operation="route_bindings",
        code=ErrorCode.CONFIG_INVALID,
    )

    structural_assets = tmp_path / "structural-assets"
    shutil.copytree(BUILTIN_ASSET_ROOT, structural_assets)
    structural_skills = tmp_path / "structural-skills"
    _write_skill(structural_skills / "fixed")
    structural = VersionedAssetCatalog(
        skill_dir=structural_skills,
        assets_root=structural_assets,
    )
    structural_skill = structural.route_bindings().available_skill_refs[0]
    structural._builtin_refs["agent-profile/router"] = structural._builtin_refs[
        "agent-profile/reviewer"
    ]
    with pytest.raises(ApplicationPortError) as corrupt_role:
        structural.route_bindings()
    _assert_catalog_error(
        corrupt_role,
        operation="route_bindings",
        code=ErrorCode.CONFIG_INVALID,
    )

    structural._skills.pop(
        (
            structural_skill.id,
            structural_skill.version,
            structural_skill.content_hash,
        )
    )
    with pytest.raises(ApplicationPortError) as corrupt_diagnose_index:
        structural.diagnose_bindings(structural_skill)
    _assert_catalog_error(
        corrupt_diagnose_index,
        operation="diagnose_bindings",
        code=ErrorCode.CONFIG_INVALID,
    )
    with pytest.raises(ApplicationPortError) as corrupt_review_index:
        structural.review_bindings(structural_skill)
    _assert_catalog_error(
        corrupt_review_index,
        operation="review_bindings",
        code=ErrorCode.CONFIG_INVALID,
    )


def test_product_hash_rejects_symbolic_links(tmp_path: Path) -> None:
    symlink_root = tmp_path / "symlink-product"
    symlink_root.mkdir()
    (symlink_root / "entry.md").write_text("entry\n", encoding="utf-8")
    try:
        os.symlink(symlink_root / "entry.md", symlink_root / "alias.md")
    except OSError as exc:  # pragma: no cover - Windows privilege dependent
        pytest.skip(f"symlink creation unavailable: {exc}")
    with pytest.raises(ValueError, match="links are forbidden"):
        hash_product_directory(symlink_root)


def test_product_hash_rejects_hard_links(tmp_path: Path) -> None:
    hardlink_root = tmp_path / "hardlink-product"
    hardlink_root.mkdir()
    original = hardlink_root / "entry.md"
    original.write_text("entry\n", encoding="utf-8")
    try:
        os.link(original, hardlink_root / "alias.md")
    except OSError as exc:  # pragma: no cover - filesystem dependent
        pytest.skip(f"hard-link creation unavailable: {exc}")
    with pytest.raises(ValueError, match="hard links are forbidden"):
        hash_product_directory(hardlink_root)


def test_product_hash_rejects_non_ordinary_nodes(tmp_path: Path) -> None:
    root = tmp_path / "fifo-product"
    root.mkdir()
    try:
        os.mkfifo(root / "named-pipe")
    except (AttributeError, OSError) as exc:  # pragma: no cover - Windows dependent
        pytest.skip(f"FIFO creation unavailable: {exc}")
    with pytest.raises(ValueError, match="non-ordinary node"):
        hash_product_directory(root)


def test_product_hash_rejects_non_utf8_paths() -> None:
    try:
        non_utf8_name = os.fsdecode(b"\xff")
    except UnicodeDecodeError:  # pragma: no cover - UTF-8 Windows filesystem API
        pytest.skip("the platform cannot represent a non-UTF-8 path")
    with pytest.raises(ValueError, match="valid UTF-8"):
        catalog_module._safe_relative_path(
            non_utf8_name,
            field_name="asset product path",
        )


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"unexpected": "field"}, "fields are invalid"),
        ({"id": "UPPERCASE"}, "frozen pattern"),
        ({"tool_bundle_id": "tool-bundle/review"}, "tool_bundle_id"),
        ({"requires_logparse": False, "logparse_product": "not-null"}, "omit logparse_product"),
        ({"requires_logparse": True, "logparse_plan": None}, "logparse_plan object"),
        ({"entry_document": "../escape.md"}, "relative POSIX path"),
        ({"entry_document": "nested//entry.md"}, "relative POSIX path"),
        ({"schema_version": True}, "integer 2"),
    ],
)
def test_skill_manifest_is_strict(
    tmp_path: Path,
    extra: dict[str, Any],
    message: str,
) -> None:
    skill_dir = tmp_path / "skills"
    _write_skill(skill_dir / "candidate", extra=extra)
    with pytest.raises(ValueError, match=message):
        VersionedAssetCatalog(skill_dir=skill_dir)


def test_skill_manifest_rejects_duplicate_json_keys_and_missing_entry(tmp_path: Path) -> None:
    duplicate_dir = tmp_path / "duplicate-skills/duplicate"
    duplicate_dir.mkdir(parents=True)
    (duplicate_dir / "SKILL.md").write_text("# duplicate\n", encoding="utf-8")
    (duplicate_dir / "diagnosis-skill.json").write_text(
        '{"schema_version":1,"schema_version":1,"id":"duplicate","version":"1.0.0",'
        '"capability":"cap","summary":"summary","entry_document":"SKILL.md",'
        '"tool_bundle_id":"tool-bundle/diagnose","requires_logparse":false}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate JSON key"):
        VersionedAssetCatalog(skill_dir=duplicate_dir.parent)

    missing_dir = tmp_path / "missing-skills/missing"
    _write_skill(missing_dir)
    (missing_dir / "SKILL.md").unlink()
    with pytest.raises(ValueError, match="entry is unavailable"):
        VersionedAssetCatalog(skill_dir=missing_dir.parent)


def test_duplicate_skill_id_and_version_is_configuration_damage(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills"
    _write_skill(skill_dir / "first", skill_id="same-skill", version="1.0.0")
    _write_skill(skill_dir / "second", skill_id="same-skill", version="1.0.0")
    (skill_dir / "second/SKILL.md").write_text("different bytes\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate asset id/version"):
        VersionedAssetCatalog(skill_dir=skill_dir)


def test_builtin_manifest_is_strict(tmp_path: Path) -> None:
    assets_root = tmp_path / "assets"
    shutil.copytree(BUILTIN_ASSET_ROOT, assets_root)
    manifest_path = assets_root / "profiles/router/asset.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["private_extension"] = True
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    with pytest.raises(ValueError, match="fields are invalid"):
        VersionedAssetCatalog(skill_dir=skill_dir, assets_root=assets_root)


def test_catalog_fixture_manifest_matches_every_owned_file() -> None:
    manifest_path = FIXTURE_ROOT / "fixture-manifest.json"
    manifest = FixtureManifest.model_validate_json(manifest_path.read_bytes())
    assert manifest.owner_spec == "S04"
    assert manifest.root == "tests/fixtures/components/runtime-catalog"

    actual = {
        path.relative_to(FIXTURE_ROOT).as_posix(): path
        for path in FIXTURE_ROOT.rglob("*")
        if path.is_file() and path != manifest_path
    }
    assert [entry.path for entry in manifest.files] == sorted(actual)
    for entry in manifest.files:
        data = actual[entry.path].read_bytes()
        assert entry.size == len(data)
        assert entry.sha256 == hashlib.sha256(data).hexdigest()
