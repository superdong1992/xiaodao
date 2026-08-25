from __future__ import annotations

import importlib.util
import json
import sys
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
    registration = json.loads(
        (CASE_ROOT / descriptor["registration_template"]).read_bytes()
    )
    return registration["runtime"]["preprocessing"], driver


def test_product_registration_and_driver_project_to_one_bounded_logparse_runtime() -> None:
    support = _module()
    preprocessing, driver = _reviewed_inputs()

    config, projections, product = support.build_logparse_projection(preprocessing, driver)

    assert product == preprocessing["logparse_product"]
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


def test_frozen_log_bytes_preserves_authored_lines_without_projection() -> None:
    support = _module()
    payload = b"first source line\nsecond source line\nthird source line\n"

    assert support.frozen_log_bytes(payload) == payload
    assert support.frozen_log_bytes(payload.rstrip(b"\n")) == payload


def test_projection_rejects_skill_and_attachment_anchor_drift() -> None:
    support = _module()
    preprocessing, driver = _reviewed_inputs()
    preprocessing["logparse_plan"]["anchors"] = preprocessing["logparse_plan"]["anchors"][:-1]

    with pytest.raises(SystemExit, match="attachment anchor is not declared"):
        support.build_logparse_projection(preprocessing, driver)


@pytest.mark.parametrize(
    "payload,message",
    [
        (b"", "empty"),
        (b"bad\x00line\n", "NUL"),
        (b"\xff", "UTF-8"),
    ],
)
def test_frozen_log_bytes_rejects_unsafe_inputs(payload: bytes, message: str) -> None:
    support = _module()

    with pytest.raises(SystemExit, match=message):
        support.frozen_log_bytes(payload)
