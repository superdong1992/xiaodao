from __future__ import annotations

import sys
from pathlib import Path

from problem_locator.entrypoints.settings import Settings
from problem_locator.integrations.logparse import build_logparse_runtime
from problem_locator.runtime.agent_backend import AgentBackend
from problem_locator.runtime.catalog import VersionedAssetCatalog
from problem_locator.runtime.diagnosis_runtime import DiagnosisRuntime
from problem_locator.runtime.methods_skill import load_specialized_skill_registration
from problem_locator.runtime.workspace import WorkspaceManager
from tests.deterministic.contracts.fakes import (
    DeterministicIdGenerator,
    FakeClock,
    InMemoryExecutionRecordStore,
    InMemoryResourceStore,
    InMemoryStateRepository,
)


ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = ROOT / "tests/fixtures/components/runtime-catalog/skill-dir"
FAKE_LOGPARSE_REPO = ROOT / "tests/fixtures/components/logparse/fake/repo"
FAKE_LOGPARSE_CONFIG = FAKE_LOGPARSE_REPO / "config.yaml"
RPC_SKILL_ID = "diagnosis-skill/rpc-log-analysis"
RPC_REGISTRATION_SHA256 = (
    "a1f8f59d5c3904ed545c5ae54b4d9a3b5e80e80bdd74e4cc51dff2b7adb8bcde"
)
RPC_PACKAGE_TREE_SHA256 = (
    "f23f38f73ca562245113840d59cce1fc1225a602ab23b3382f59b4480923d0a2"
)
RPC_COMBINED_SHA256 = (
    "b58b29122582330050d1ff2754fc96d8adf72087a10ee0570d5db8d394fc59dd"
)


def test_settings_pin_one_s07_pair_into_s04_catalog_and_runtime(tmp_path: Path) -> None:
    configured_python = Path(sys.executable)
    settings = Settings.load(
        environ={
            "DATA_ROOT": str(tmp_path / "data"),
            "PUBLIC_BASE_URL": "http://127.0.0.1:8000",
            "SKILL_DIR": str(SKILL_DIR),
            "GENERIC_SKILL_NAME": "generic-problem-locator-smoke",
            "LOGPARSE_REPO": str(FAKE_LOGPARSE_REPO),
            "LOGPARSE_CONFIG_PATH": str(FAKE_LOGPARSE_CONFIG),
            "LOGPARSE_PYTHON": str(configured_python),
        }
    )
    frozen_asset_inputs = (
        settings.skill_dir,
        settings.logparse_repo,
        settings.logparse_config_path,
        settings.logparse_python,
    )
    assert frozen_asset_inputs == (
        SKILL_DIR,
        FAKE_LOGPARSE_REPO,
        FAKE_LOGPARSE_CONFIG,
        configured_python,
    )

    logparse_asset, broker_factory = build_logparse_runtime(
        settings.logparse_repo,
        settings.logparse_config_path,
        settings.logparse_python,
    )
    assert broker_factory.resolved_asset is logparse_asset

    catalog = VersionedAssetCatalog(
        skill_dir=settings.skill_dir,
        generic_skill_name=settings.generic_skill_name,
        logparse_tool=logparse_asset,
        logparse_broker_factory=broker_factory,
        allow_test_skills=True,
    )
    route_bindings = catalog.route_bindings()
    rpc_ref = next(
        ref
        for ref in route_bindings.available_skill_refs
        if ref.id == RPC_SKILL_ID
    )
    assert rpc_ref.version == "1.0.0"
    resolved_skill = load_specialized_skill_registration(
        settings.skill_dir / "rpc-log-analysis"
    )
    assert resolved_skill.registration_sha256 == RPC_REGISTRATION_SHA256
    assert resolved_skill.package_tree_sha256 == RPC_PACKAGE_TREE_SHA256
    assert resolved_skill.combined_sha256 == RPC_COMBINED_SHA256
    assert rpc_ref.content_hash == RPC_COMBINED_SHA256

    diagnose_bindings = catalog.diagnose_bindings(rpc_ref)
    assert diagnose_bindings.skill_ref == rpc_ref
    assert diagnose_bindings.logparse_tool_ref == logparse_asset.ref
    assert diagnose_bindings.logparse_product == "compact"
    assert catalog.resolve(rpc_ref).ref == rpc_ref
    assert catalog.resolve(logparse_asset.ref) == logparse_asset

    # The frozen Catalog Port deliberately has no factory-identity read surface.
    # Verify the composition seam without opening a broker or loopback socket.
    assert catalog._logparse_broker_factory is broker_factory
    runtime = DiagnosisRuntime(
        state_repository=InMemoryStateRepository(),
        resource_store=InMemoryResourceStore(),
        asset_catalog=catalog,
        logparse_broker_factory=broker_factory,
        execution_records=InMemoryExecutionRecordStore(),
        clock=FakeClock(),
        id_generator=DeterministicIdGenerator(seed="s08-s07-composition"),
        workspace_manager=WorkspaceManager(settings.data_root),
        backend=AgentBackend(settings.claude_command),
    )
    assert runtime._logparse_broker_factory is broker_factory
