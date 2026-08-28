from __future__ import annotations

import copy
import errno
import hashlib
import json
from pathlib import Path
from typing import Literal

import pytest

from problem_locator.contracts import MethodEvaluationRoleV2, canonical_json_bytes
from problem_locator.runtime.methods_evaluation_v2 import (
    MethodEvaluationResponseError,
)
from problem_locator.runtime.methods_evidence_v2 import (
    build_method_evaluation_plan_v2,
    scan_method_evidence_v2,
)
from problem_locator.runtime.methods_grounding import FrozenTargetLogV1
from problem_locator.runtime.output_reader import read_method_role_attempt_v2
from tests.deterministic.unit.runtime.methods_v2_test_support import (
    load_test_methods_skill,
)


def _plan(tmp_path: Path):
    skill = load_test_methods_skill(
        tmp_path / "skill-input",
        name="reader-test",
        methods=(
            ("first-method", "FIRST_MARKER"),
            ("second-method", "SECOND_MARKER"),
        ),
    )
    content = (
        b"FIRST_MARKER request_id=req-1\n"
        b"SECOND_MARKER request_id=req-2\n"
    )
    target = FrozenTargetLogV1(
        source_id="server",
        relative_path="logs/server.log",
        content_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )
    graph = scan_method_evidence_v2(skill=skill, target_logs=(target,))
    return build_method_evaluation_plan_v2(skill=skill, evidence=graph)


def _response(plan):
    return [
        {
            "evaluation_ref": item.evaluation_ref,
            "verdict": verdict,
            "reason": f"reason-{index}",
        }
        for index, (item, verdict) in enumerate(
            zip(plan.evaluations, ("CONFIRMED", "REJECTED"), strict=True),
            start=1,
        )
    ]


def _write_attempt(root: Path, filename: str, raw_bytes: bytes) -> None:
    output = root / "output"
    output.mkdir()
    (output / filename).write_bytes(raw_bytes)


@pytest.mark.parametrize(
    ("role", "attempt", "filename", "repair_used"),
    [
        ("SPECIALIST", "PRIMARY", "method-diagnosis.draft.json", False),
        ("SPECIALIST", "REPAIR", "method-diagnosis.draft.json", True),
        ("REVIEWER", "PRIMARY", "method-review.draft.json", False),
        ("REVIEWER", "REPAIR", "method-review.draft.json", True),
    ],
)
def test_reads_each_role_attempt_from_its_fixed_path(
    tmp_path: Path,
    role: MethodEvaluationRoleV2,
    attempt: Literal["PRIMARY", "REPAIR"],
    filename: str,
    repair_used: bool,
) -> None:
    plan = _plan(tmp_path)
    response = _response(plan)
    raw_bytes = json.dumps(response, ensure_ascii=False, indent=2).encode("utf-8")
    _write_attempt(tmp_path, filename, raw_bytes)

    result = read_method_role_attempt_v2(
        tmp_path,
        role=role,
        plan=plan,
        attempt=attempt,
    )

    assert result.raw_bytes == raw_bytes
    assert result.canonical_bytes == canonical_json_bytes(response)
    assert result.evaluation.role == role
    assert result.evaluation.repair_used is repair_used
    actual_refs = tuple(
        item.evaluation_ref for item in result.evaluation.evaluations
    )
    assert actual_refs == tuple(item.evaluation_ref for item in plan.evaluations)


def test_missing_attempt_is_a_typed_protocol_error(tmp_path: Path) -> None:
    with pytest.raises(MethodEvaluationResponseError) as caught:
        read_method_role_attempt_v2(
            tmp_path / "missing-workspace",
            role="SPECIALIST",
            plan=_plan(tmp_path),
            attempt="PRIMARY",
        )

    assert caught.value.raw_response_bytes is None


@pytest.mark.parametrize(
    "filesystem_error",
    [
        PermissionError(errno.EACCES, "denied"),
        OSError(errno.EIO, "read failure"),
    ],
    ids=["permission-error", "other-os-error"],
)
def test_real_filesystem_error_does_not_become_a_repairable_protocol_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filesystem_error: OSError,
) -> None:
    plan = _plan(tmp_path)

    def fail_stat(*_args: object, **_kwargs: object) -> object:
        raise filesystem_error

    monkeypatch.setattr(Path, "stat", fail_stat)

    with pytest.raises(type(filesystem_error)) as caught:
        read_method_role_attempt_v2(
            tmp_path,
            role="SPECIALIST",
            plan=plan,
            attempt="PRIMARY",
        )

    assert caught.value is filesystem_error


@pytest.mark.parametrize(
    "raw_bytes",
    [
        b"not-json\n",
        b'{"not":"an-array"}\n',
    ],
    ids=["invalid-json", "invalid-root"],
)
def test_invalid_json_or_root_preserves_exact_rejected_bytes(
    tmp_path: Path,
    raw_bytes: bytes,
) -> None:
    _write_attempt(tmp_path, "method-diagnosis.draft.json", raw_bytes)

    with pytest.raises(MethodEvaluationResponseError) as caught:
        read_method_role_attempt_v2(
            tmp_path,
            role="SPECIALIST",
            plan=_plan(tmp_path),
            attempt="PRIMARY",
        )

    assert caught.value.raw_response_bytes == raw_bytes


def test_coverage_error_starts_from_production_plan_and_mutates_one_item(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    response = copy.deepcopy(_response(plan))
    response.pop()
    raw_bytes = canonical_json_bytes(response)
    _write_attempt(tmp_path, "method-review.draft.json", raw_bytes)

    with pytest.raises(MethodEvaluationResponseError) as caught:
        read_method_role_attempt_v2(
            tmp_path,
            role="REVIEWER",
            plan=plan,
            attempt="REPAIR",
        )

    assert caught.value.raw_response_bytes == raw_bytes
