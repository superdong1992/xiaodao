from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from problem_locator.contracts import InvalidJsonBytesError, canonical_json_bytes
from problem_locator.integrations.agent_json import (
    AGENT_JSON_SURFACE_OWNERS,
    AgentJsonSurface,
    parse_agent_json_bytes,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def _reverse_objects(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _reverse_objects(child)
            for key, child in reversed(list(value.items()))
        }
    if isinstance(value, list):
        return [_reverse_objects(child) for child in value]
    return value


def test_agent_json_boundary_recursively_canonicalizes_without_changing_values(
) -> None:
    value = {
        "zeta": {"zulu": 1, "alpha": {"zulu": "中文", "alpha": 2}},
        "alpha": [{"zulu": 3, "alpha": 4}, {"zulu": 5, "alpha": 6}],
        "array_order": [3, 2, 1],
    }
    draft = (
        json.dumps(
            _reverse_objects(value),
            ensure_ascii=False,
            indent=2,
        )
        .replace("\n", "\r\n")
        .encode("utf-8")
    )

    document = parse_agent_json_bytes(draft)

    assert document.value == value
    assert document.canonical_bytes == canonical_json_bytes(value)
    assert document.canonical_bytes.endswith(b"\n")


@pytest.mark.parametrize(
    "invalid_bytes",
    [
        b'\xef\xbb\xbf{"value":1}',
        b'{"value":1,"value":2}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{"value":"\\ud800"}',
        b'{"value":',
        b'\xff',
    ],
)
def test_agent_json_boundary_rejects_ambiguous_or_invalid_json(
    invalid_bytes: bytes,
) -> None:
    with pytest.raises(InvalidJsonBytesError):
        parse_agent_json_bytes(invalid_bytes)


def test_all_agent_json_surfaces_have_one_server_side_owner() -> None:
    assert set(AGENT_JSON_SURFACE_OWNERS) == set(AgentJsonSurface)
    assert AGENT_JSON_SURFACE_OWNERS == {
        AgentJsonSurface.JOB_OUTCOME: "problem-locator-seal-outcome-draft",
        AgentJsonSurface.LOGPARSE_REQUEST: "problem-locator-logparse",
    }


@pytest.mark.parametrize(
    "relative_path",
    [
        "src/problem_locator/runtime/output_reader.py",
        "src/problem_locator/runtime/outcome_finalizer.py",
        "src/problem_locator/integrations/logparse/cli.py",
    ],
)
def test_agent_json_consumers_do_not_bypass_the_shared_parser(
    relative_path: str,
) -> None:
    source = (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_calls = {
        "json.loads",
        "json.load",
        "parse_canonical_json_bytes",
    }
    calls = {
        (
            f"{node.func.value.id}.{node.func.attr}"
            if isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            else node.func.id
            if isinstance(node.func, ast.Name)
            else ""
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    if relative_path.endswith("logparse/cli.py"):
        # Broker responses remain strictly Canonical server output. Only the
        # Agent-authored request must pass through the shared draft boundary.
        forbidden_calls.remove("parse_canonical_json_bytes")
    assert calls.isdisjoint(forbidden_calls)
    assert (
        "parse_agent_json_bytes" in source
        or "read_agent_json_file" in source
        or "normalize_agent_json_file" in source
    )
