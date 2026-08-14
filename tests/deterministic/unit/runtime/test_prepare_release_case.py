from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SUPPORT = REPOSITORY_ROOT / "tools" / "test-flow" / "runtime-support" / "prepare_release_case.py"
RELEASE_ROOT = REPOSITORY_ROOT / "tests" / "cases" / "release"
CASE_ROOTS = [path.parent for path in RELEASE_ROOT.glob("*/case.json")]
assert len(CASE_ROOTS) == 1
CASE_ROOT = CASE_ROOTS[0]


def _module():
    spec = importlib.util.spec_from_file_location("_test_prepare_release_case", SUPPORT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _reviewed_inputs() -> tuple[dict, dict]:
    descriptor = json.loads((CASE_ROOT / "case.json").read_bytes())
    scenario = next(
        item
        for item in descriptor["scenarios"]
        if item["scenario_id"] == descriptor["journey_scenario"]
    )
    driver = json.loads((CASE_ROOT / scenario["driver"]).read_bytes())
    skill = json.loads(
        (CASE_ROOT / descriptor["approved_skill_dir"] / "diagnosis-skill.json").read_bytes()
    )
    return skill, driver


def test_reviewed_skill_and_driver_project_to_one_bounded_logparse_runtime() -> None:
    support = _module()
    skill, driver = _reviewed_inputs()

    config, projections, product = support.build_logparse_projection(skill, driver)

    assert product == skill["logparse_product"]
    assert list(config["products"]) == [product]
    assert config["products"][product]["archive"] == {
        "recursive_extraction": False,
        "compressed_extensions": [".zip"],
    }
    assert set(projections) == set(driver["attachment_files"])
    assert {item["label"] for item in projections.values()} == set(
        driver["attachment_anchor_names"]
    )
    assert config["products"][product]["discovery"]["config"][
        "loose_diagnostics"
    ]["file_patterns"] == sorted(Path(item).name for item in driver["attachment_files"])
    assert set(config["products"][product]["mechanisms"]) == {
        item["module"] for item in projections.values()
    }
    for projection in projections.values():
        rendered = support.projected_log_bytes(b"public sample line\n", projection).decode()
        assert rendered.endswith("Context=public sample line)\n")
        assert projection["module"].upper() in rendered
        assert f'Slot={projection["slot"]};' in rendered
        assert f'ProcessName={projection["process"]};' in rendered


def test_projected_log_bytes_preserves_source_order_with_monotonic_timestamps() -> None:
    support = _module()
    skill, driver = _reviewed_inputs()
    _config, projections, _product = support.build_logparse_projection(skill, driver)
    projection = next(iter(projections.values()))

    rendered = support.projected_log_bytes(
        b"first source line\nsecond source line\nthird source line\n",
        projection,
    ).decode().splitlines()

    timestamps = [datetime.fromisoformat(line.split(" ", 1)[0]) for line in rendered]
    assert timestamps == [
        datetime.fromisoformat(projection["timestamp"]) + timedelta(microseconds=ordinal)
        for ordinal in range(3)
    ]
    assert [line.rsplit("Context=", 1)[1] for line in rendered] == [
        "first source line)",
        "second source line)",
        "third source line)",
    ]


def test_projection_rejects_skill_and_attachment_anchor_drift() -> None:
    support = _module()
    skill, driver = _reviewed_inputs()
    skill["logparse_plan"]["anchors"] = skill["logparse_plan"]["anchors"][:-1]

    with pytest.raises(SystemExit, match="attachment anchor is not declared"):
        support.build_logparse_projection(skill, driver)


@pytest.mark.parametrize(
    "payload,message",
    [
        (b"", "empty"),
        (b"bad\x00line\n", "NUL"),
        (b"\xff", "UTF-8"),
    ],
)
def test_projected_log_bytes_rejects_unsafe_inputs(payload: bytes, message: str) -> None:
    support = _module()
    skill, driver = _reviewed_inputs()
    _config, projections, _product = support.build_logparse_projection(skill, driver)

    with pytest.raises(SystemExit, match=message):
        support.projected_log_bytes(payload, next(iter(projections.values())))
