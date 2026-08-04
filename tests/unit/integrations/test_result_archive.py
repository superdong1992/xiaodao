from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from problem_locator.contracts import canonical_json_bytes
from problem_locator.integrations.result_archive import (
    build_result_archive,
    validate_result_archive_bytes,
)


def _workspace(root: Path, proposal_key: str) -> tuple[Path, bytes, bytes]:
    (root / "inputs/artifacts/run-1/tree/logs").mkdir(parents=True)
    proposal = root / f"output/proposals/{proposal_key}"
    proposal.mkdir(parents=True)
    first = b"first target log\n"
    second = b"second target log\n"
    (root / "inputs/artifacts/run-1/tree/logs/first.log").write_bytes(first)
    (root / "inputs/artifacts/run-1/tree/logs/second.log").write_bytes(second)
    (proposal / "request.json").write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "result_text": "Diagnosis result\n",
                "target_log_paths": [
                    "inputs/artifacts/run-1/tree/logs/first.log",
                    "inputs/artifacts/run-1/tree/logs/second.log",
                ],
            }
        )
    )
    return proposal, first, second


def test_controlled_result_archive_is_flat_and_deterministic(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    first_proposal, first_log, second_log = _workspace(first_root, "archive")
    first = build_result_archive(
        first_root,
        "output/proposals/archive/request.json",
        "output/proposals/archive/result.zip",
    ).read_bytes()

    second_root = tmp_path / "second"
    _workspace(second_root, "archive")
    second = build_result_archive(
        second_root,
        "output/proposals/archive/request.json",
        "output/proposals/archive/result.zip",
    ).read_bytes()
    assert first == second
    assert validate_result_archive_bytes(
        first,
        target_logs=(first_log, second_log),
    ) == "Diagnosis result\n"
    with zipfile.ZipFile(first_proposal / "result.zip") as archive:
        assert archive.namelist() == [
            "result.txt",
            "target-log-001.log",
            "target-log-002.log",
        ]


def test_manual_result_archive_contains_only_result_text(tmp_path: Path) -> None:
    proposal = tmp_path / "output/proposals/archive"
    proposal.mkdir(parents=True)
    (proposal / "request.json").write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "result_text": "Manual diagnosis\n",
                "target_log_paths": [],
            }
        )
    )
    data = build_result_archive(
        tmp_path,
        "output/proposals/archive/request.json",
        "output/proposals/archive/result.zip",
    ).read_bytes()
    assert validate_result_archive_bytes(data, target_logs=()) == "Manual diagnosis\n"


def test_packer_rejects_original_upload_or_arbitrary_workspace_file(tmp_path: Path) -> None:
    proposal = tmp_path / "output/proposals/archive"
    proposal.mkdir(parents=True)
    (tmp_path / "inputs/attachments/upload").mkdir(parents=True)
    (tmp_path / "inputs/attachments/upload/payload.zip").write_bytes(b"upload")
    (proposal / "request.json").write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "result_text": "Diagnosis\n",
                "target_log_paths": ["inputs/attachments/upload/payload.zip"],
            }
        )
    )
    with pytest.raises(ValueError, match="LOGPARSE_RUN tree"):
        build_result_archive(
            tmp_path,
            "output/proposals/archive/request.json",
            "output/proposals/archive/result.zip",
        )


def test_runtime_archive_validator_rejects_wrong_log_bytes(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _, first_log, second_log = _workspace(root, "archive")
    data = build_result_archive(
        root,
        "output/proposals/archive/request.json",
        "output/proposals/archive/result.zip",
    ).read_bytes()
    with pytest.raises(ValueError, match="do not match"):
        validate_result_archive_bytes(data, target_logs=(first_log, second_log + b"drift"))
