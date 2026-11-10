"""Standalone v2/v3 registration validators accept the same direct-route rules."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / ".claude/skills/wiki-to-logparse-diagnosis-skill/scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_direct_keywords_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V2 = _load("validate_generated_skill")
V3 = _load("registration_v3")


def _validate(version, routing):
    if version == 3:
        V3._routing(routing)
        return
    errors = []
    V2._validate_routing(routing, errors)
    if errors:
        raise ValueError("; ".join(errors))


def _routing():
    return {"applicability": [{"id": "pool-deadlock", "description": "问题为连接池死锁。"}], "exclusions": []}


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("keywords", [[], ["POOL_DEADLOCK_42"], ["连接池死锁", "a" * 128], ["🦊" * 128]])
def test_standalone_registration_accepts_optional_direct_keywords(version, keywords):
    value = _routing()
    value["direct_match_keywords"] = keywords
    _validate(version, value)


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("keywords", [None, "POOL_DEADLOCK_42", {}, [True], [3], [""], ["ab"],
    ["a" * 129], ["🦊" * 129], [" POOL_DEADLOCK_42"], ["POOL_DEADLOCK_42 "],
    ["POOL\nDEADLOCK"], ["POOL\x00DEADLOCK"], ["POOL\x7fDEADLOCK"], ["POOL\x85DEADLOCK"],
    ["POOL_DEADLOCK_42", "POOL_DEADLOCK_42"], [f"ERR_{index}" for index in range(17)],
    ["timeout"], ["ERROR"], ["failure"], ["slow"], ["RPC"], ["超时"], ["失败"], ["错误"], ["故障"], ["异常"]])
def test_standalone_registration_rejects_invalid_direct_keywords(version, keywords):
    value = _routing()
    value["direct_match_keywords"] = keywords
    with pytest.raises(ValueError, match="direct_match_keywords"):
        _validate(version, value)


@pytest.mark.parametrize("version", [2, 3])
def test_standalone_routing_keeps_original_fields_required_and_extra_fields_closed(version):
    _validate(version, _routing())
    for mutation in ("missing", "extra"):
        value = _routing()
        value["direct_match_keywords"] = ["POOL_DEADLOCK_42"]
        if mutation == "missing":
            del value["exclusions"]
        else:
            value["regex"] = ".*"
        with pytest.raises(ValueError):
            _validate(version, value)
