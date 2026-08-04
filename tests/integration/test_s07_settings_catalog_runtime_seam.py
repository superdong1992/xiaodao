from __future__ import annotations

import sys
from pathlib import Path

from problem_locator.entrypoints.settings import Settings
from problem_locator.integrations.logparse import build_logparse_runtime
from problem_locator.runtime.agent_backend import AgentBackend
from problem_locator.runtime.catalog import (
    VersionedAssetCatalog,
    hash_product_directory,
)
from problem_locator.runtime.diagnosis_runtime import DiagnosisRuntime
from problem_locator.runtime.workspace import WorkspaceManager
from tests.contracts.fakes import (
    DeterministicIdGenerator,
    FakeClock,
    InMemoryExecutionRecordStore,
    InMemoryResourceStore,
    InMemoryStateRepository,
)


ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / ".claude/skills"
FAKE_LOGPARSE_REPO = ROOT / "tests/fixtures/components/logparse/fake/repo"
FAKE_LOGPARSE_CONFIG = FAKE_LOGPARSE_REPO / "config.yaml"
TAKEOVER_SKILL_ID = "diagnosis-skill/diagnose-service-takeover"
TAKEOVER_PRODUCT_HASH = (
    "08573b8e01e2b5c213c59b0b27b3922566293af1aed963c09c6f735f41abdd95"
)


def test_settings_pin_one_s07_pair_into_s04_catalog_and_runtime(tmp_path: Path) -> None:
    configured_python = Path(sys.executable)
    settings = Settings.load(
        environ={
            "DATA_ROOT": str(tmp_path / "data"),
            "PUBLIC_BASE_URL": "http://127.0.0.1:8000",
            "SKILL_DIR": str(SKILL_DIR),
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
        logparse_tool=logparse_asset,
        logparse_broker_factory=broker_factory,
    )
    route_bindings = catalog.route_bindings()
    takeover_ref = next(
        ref
        for ref in route_bindings.available_skill_refs
        if ref.id == TAKEOVER_SKILL_ID
    )
    assert takeover_ref.version == "3.0.4"
    assert (
        hash_product_directory(settings.skill_dir / "diagnose-service-takeover")
        == TAKEOVER_PRODUCT_HASH
    )
    assert takeover_ref.content_hash == TAKEOVER_PRODUCT_HASH

    diagnose_bindings = catalog.diagnose_bindings(takeover_ref)
    assert diagnose_bindings.skill_ref == takeover_ref
    assert diagnose_bindings.logparse_tool_ref == logparse_asset.ref
    assert diagnose_bindings.logparse_product == "compact"
    assert catalog.resolve(takeover_ref).ref == takeover_ref
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
