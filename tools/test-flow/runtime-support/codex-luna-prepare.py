#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
EXPECTED_CASE_COUNT = 9
LOGPARSE_PRODUCT = "rpc-skill-feasibility"
REQUIRED_CASE_KEYS = {
    "scenario_id",
    "problem_time",
    "client_process",
    "server_process",
    "service",
    "api",
}


class PrepareError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    with temporary.open("xb") as stream:
        stream.write(canonical_bytes(value))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def read_json(path: Path, label: str) -> Any:
    ordinary_file(path, label)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrepareError("CODEX_LUNA_PREPARE_JSON_INVALID", f"{label} is invalid JSON: {exc}") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def ordinary_file(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise PrepareError("CODEX_LUNA_PREPARE_FILE_MISSING", f"{label} is unavailable: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise PrepareError(
            "CODEX_LUNA_PREPARE_FILE_NOT_ORDINARY",
            f"{label} must be one ordinary file: {path}",
        )
    return metadata


def ordinary_directory(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise PrepareError("CODEX_LUNA_PREPARE_DIRECTORY_MISSING", f"{label} is unavailable: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise PrepareError(
            "CODEX_LUNA_PREPARE_DIRECTORY_INVALID",
            f"{label} must be one real directory: {path}",
        )
    if path.resolve() != path.absolute():
        raise PrepareError(
            "CODEX_LUNA_PREPARE_DIRECTORY_SYMLINKED",
            f"{label} must not contain symlinked components: {path}",
        )
    return metadata


def tree_digest(root: Path) -> str:
    ordinary_directory(root, "digest tree")
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise PrepareError(
                "CODEX_LUNA_PREPARE_TREE_SYMLINK",
                f"audited trees cannot contain symlinks: {path}",
            )
        if path.is_dir():
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise PrepareError(
                "CODEX_LUNA_PREPARE_TREE_NODE_INVALID",
                f"audited trees may contain only ordinary files: {path}",
            )
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": metadata.st_size,
                "sha256": sha256_file(path),
            }
        )
    return sha256_bytes(canonical_bytes(records))


def run_checked(
    command: list[str],
    *,
    cwd: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout: int,
) -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "TMPDIR",
            "TMP",
            "TEMP",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "HOME",
            "USER",
            "LOGNAME",
            "SHELL",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
    }
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
    stdout_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stderr_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with stdout_path.open("x", encoding="utf-8") as stdout, stderr_path.open(
            "x", encoding="utf-8"
        ) as stderr:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                text=True,
                timeout=timeout,
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        raise PrepareError(
            "CODEX_LUNA_PREPARE_COMMAND_TIMEOUT",
            f"Logparse command timed out: {command[1] if len(command) > 1 else command[0]}",
        ) from exc
    if completed.returncode != 0:
        detail = stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        raise PrepareError(
            "CODEX_LUNA_PREPARE_COMMAND_FAILED",
            f"Logparse command failed ({completed.returncode}): {detail}",
        )


def git_identity(root: Path) -> dict[str, str]:
    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise PrepareError(
                "CODEX_LUNA_PREPARE_LOGPARSE_GIT_INVALID",
                f"cannot establish Logparse Git identity: {completed.stderr.strip()}",
            )
        return completed.stdout.strip()

    head = git("rev-parse", "HEAD")
    status = git("status", "--porcelain=v1", "--untracked-files=all")
    return {
        "git_head": head,
        "git_status_sha256": sha256_bytes(status.encode("utf-8")),
    }


def logparse_identity(root: Path) -> tuple[Path, Path, dict[str, object]]:
    ordinary_directory(root, "Logparse source")
    cli = root / "cli.py"
    python = root / ".venv/bin/python"
    python_target = python.resolve()
    ordinary_file(cli, "Logparse CLI")
    ordinary_file(python_target, "Logparse Python")
    completed = subprocess.run(
        [os.fspath(python), "-I", "--version"],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise PrepareError(
            "CODEX_LUNA_PREPARE_PYTHON_INVALID",
            "cannot establish Logparse Python identity",
        )
    identity: dict[str, object] = {
        "schema_version": 1,
        **git_identity(root),
        "cli_sha256": sha256_file(cli),
        "python_sha256": sha256_file(python_target),
        "python_version": (completed.stdout or completed.stderr).strip(),
    }
    return python, cli, identity


def load_cases(case_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    ordinary_directory(case_root, "Codex scenario root")
    descriptors = sorted(case_root.glob("*/case.json"))
    if len(descriptors) != EXPECTED_CASE_COUNT:
        raise PrepareError(
            "CODEX_LUNA_PREPARE_CASE_COUNT_INVALID",
            f"expected exactly {EXPECTED_CASE_COUNT} scenarios, got {len(descriptors)}",
        )
    cases: list[tuple[Path, dict[str, Any]]] = []
    ids: set[str] = set()
    for descriptor in descriptors:
        data = read_json(descriptor, "Codex scenario descriptor")
        if not isinstance(data, dict) or not REQUIRED_CASE_KEYS.issubset(data):
            raise PrepareError(
                "CODEX_LUNA_PREPARE_CASE_INVALID",
                f"scenario descriptor lacks required fields: {descriptor}",
            )
        scenario_id = data.get("scenario_id")
        if not isinstance(scenario_id, str) or scenario_id != descriptor.parent.name:
            raise PrepareError(
                "CODEX_LUNA_PREPARE_CASE_ID_INVALID",
                f"scenario ID does not match directory: {descriptor}",
            )
        if scenario_id in ids:
            raise PrepareError(
                "CODEX_LUNA_PREPARE_CASE_DUPLICATE",
                f"duplicate scenario ID: {scenario_id}",
            )
        ids.add(scenario_id)
        for key in (
            "problem_time",
            "client_process",
            "server_process",
            "service",
            "api",
        ):
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise PrepareError(
                    "CODEX_LUNA_PREPARE_CASE_INPUT_INVALID",
                    f"invalid {key} for {scenario_id}",
                )
        for label in ("client", "server"):
            ordinary_file(descriptor.parent / "raw" / f"{label}.log", f"{label} raw log")
        cases.append((descriptor.parent, data))
    return cases


def build_archive(case_root: Path, archive: Path) -> None:
    temporary = archive.with_name(f"{archive.name}.tmp")
    with zipfile.ZipFile(
        temporary,
        "x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as bundle:
        for label in ("client", "server"):
            source = case_root / "raw" / f"{label}.log"
            info = zipfile.ZipInfo(
                f"{label}.log",
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            bundle.writestr(info, source.read_bytes())
    os.replace(temporary, archive)


def target_from_payload(payload: object, label: str) -> dict[str, object]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise PrepareError(
            "CODEX_LUNA_PREPARE_TARGET_INVALID",
            f"invalid {label} target_logs envelope",
        )
    targets = payload.get("target_logs")
    if (
        not isinstance(targets, list)
        or len(targets) != 1
        or not isinstance(targets[0], dict)
    ):
        raise PrepareError(
            "CODEX_LUNA_PREPARE_TARGET_COUNT_INVALID",
            f"invalid {label} target_logs count",
        )
    target = targets[0]
    if target.get("match_status") != "exact" or not isinstance(
        target.get("log_path"), str
    ):
        raise PrepareError(
            "CODEX_LUNA_PREPARE_TARGET_NOT_EXACT",
            f"Logparse did not resolve exact {label} target",
        )
    return target


def preprocess_case(
    case_root: Path,
    data: dict[str, Any],
    *,
    output: Path,
    logparse_root: Path,
    python: Path,
    cli: Path,
    identity: dict[str, object],
    config: Path,
) -> dict[str, object]:
    scenario_id = str(data["scenario_id"])
    case_output = output / "preprocessed" / scenario_id
    case_output.mkdir(parents=True, mode=0o700)
    archive = case_output / f"{scenario_id}.zip"
    parsed_root = case_output / "parsed"
    frozen_root = case_output / "frozen"
    frozen_root.mkdir(mode=0o700)
    build_archive(case_root, archive)

    raw_inputs = []
    for label in ("client", "server"):
        source = case_root / "raw" / f"{label}.log"
        raw_inputs.append(
            {
                "label": label,
                "name": source.name,
                "size": source.stat().st_size,
                "sha256": sha256_file(source),
            }
        )
    input_identity = {
        "schema_version": 2,
        "scenario_id": scenario_id,
        "problem_time": data["problem_time"],
        "target_selectors": {
            "module": "rpc",
            "slot": "1",
            "client_process": data["client_process"],
            "server_process": data["server_process"],
        },
        "config_sha256": sha256_file(config),
        "logparse": identity,
        "raw_inputs": raw_inputs,
    }

    run_checked(
        [
            os.fspath(python),
            "-I",
            "-B",
            os.fspath(cli),
            "parse",
            os.fspath(archive),
            "-c",
            os.fspath(config),
            "-o",
            os.fspath(parsed_root),
            "--product",
            LOGPARSE_PRODUCT,
        ],
        cwd=logparse_root,
        timeout=180,
        stdout_path=case_output / "parse.stdout.txt",
        stderr_path=case_output / "parse.stderr.txt",
    )
    result_files = sorted(parsed_root.glob("*/result.json"))
    if len(result_files) != 1:
        raise PrepareError(
            "CODEX_LUNA_PREPARE_TASK_COUNT_INVALID",
            f"expected one parsed task for {scenario_id}",
        )
    task_id = result_files[0].parent.name
    frozen_items: list[dict[str, object]] = []
    for label, process_key in (
        ("client", "client_process"),
        ("server", "server_process"),
    ):
        target_stdout = case_output / f"target-{label}.json"
        run_checked(
            [
                os.fspath(python),
                "-I",
                "-B",
                os.fspath(cli),
                "mech-target-logs",
                task_id,
                "--problem-time",
                str(data["problem_time"]),
                "--module",
                "rpc",
                "--slot",
                "1",
                "--process-name",
                str(data[process_key]),
                "--label",
                label,
                "-o",
                os.fspath(parsed_root),
                "--explain",
            ],
            cwd=logparse_root,
            timeout=120,
            stdout_path=target_stdout,
            stderr_path=case_output / f"target-{label}.stderr.txt",
        )
        target = target_from_payload(
            read_json(target_stdout, f"{label} target response"), label
        )
        source = Path(str(target["log_path"])).resolve()
        if parsed_root.resolve() not in source.parents:
            raise PrepareError(
                "CODEX_LUNA_PREPARE_TARGET_ESCAPE",
                f"selected target escaped parsed tree: {source}",
            )
        ordinary_file(source, f"selected {label} target log")
        destination = frozen_root / f"{label}.log"
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o600)
        frozen_items.append(
            {
                "label": label,
                "process_name": data[process_key],
                "file": f"frozen/{label}.log",
                "size": destination.stat().st_size,
                "sha256": sha256_file(destination),
                "match_status": target.get("match_status"),
                "error_code": target.get("error_code"),
            }
        )

    receipt = {
        "schema_version": 1,
        "status": "PASS",
        "scenario_id": scenario_id,
        "input_fingerprint": sha256_bytes(canonical_bytes(input_identity)),
        "parse_invocations": 1,
        "target_query_invocations": 2,
        "logparse_processes_during_diagnosis": 0,
        "logparse_identity": identity,
        "config": {
            "product": LOGPARSE_PRODUCT,
            "sha256": sha256_file(config),
        },
        "raw_inputs": raw_inputs,
        "archive": {
            "name": archive.name,
            "size": archive.stat().st_size,
            "sha256": sha256_file(archive),
        },
        "parsed_tree_sha256": tree_digest(parsed_root),
        "frozen_target_logs": frozen_items,
    }
    receipt_path = case_output / "receipt.json"
    write_json(receipt_path, receipt)
    return {
        "scenario_id": scenario_id,
        "status": "PASS",
        "parse_invocations": 1,
        "target_query_invocations": 2,
        "receipt_sha256": sha256_file(receipt_path),
        "frozen_target_logs": [
            {
                "label": item["label"],
                "size": item["size"],
                "sha256": item["sha256"],
            }
            for item in frozen_items
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare the nine frozen Logparse inputs for the Codex Luna Test Flow proof."
    )
    parser.add_argument("--case-root", required=True, type=Path)
    parser.add_argument("--logparse-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    arguments = parser.parse_args()

    case_root = arguments.case_root.resolve()
    logparse_root = arguments.logparse_root.resolve()
    output_root = arguments.output_root.resolve()
    aggregate_path = output_root / "codex-luna-preprocessing.json"
    try:
        if output_root.exists():
            ordinary_directory(output_root, "Codex preprocessing output")
            if any(output_root.iterdir()):
                raise PrepareError(
                    "CODEX_LUNA_PREPARE_OUTPUT_NOT_EMPTY",
                    f"preprocessing output must be empty: {output_root}",
                )
        else:
            output_root.mkdir(parents=True, mode=0o700)
        config = case_root.parent / "logparse-config.json"
        ordinary_file(config, "Codex Logparse config")
        cases = load_cases(case_root)
        python, cli, identity = logparse_identity(logparse_root)
        results = []
        for case_path, data in cases:
            print(f"[codex-luna-prepare] {data['scenario_id']}", flush=True)
            results.append(
                preprocess_case(
                    case_path,
                    data,
                    output=output_root,
                    logparse_root=logparse_root,
                    python=python,
                    cli=cli,
                    identity=identity,
                    config=config,
                )
            )
        aggregate = {
            "schema_version": SCHEMA_VERSION,
            "status": "PASS",
            "case_count": len(results),
            "logparse_identity": identity,
            "config": {
                "product": LOGPARSE_PRODUCT,
                "sha256": sha256_file(config),
            },
            "totals": {
                "parse_invocations": sum(
                    int(item["parse_invocations"]) for item in results
                ),
                "target_query_invocations": sum(
                    int(item["target_query_invocations"]) for item in results
                ),
                "diagnosis_invocations": 0,
            },
            "cases": results,
        }
        if aggregate["totals"] != {
            "parse_invocations": 9,
            "target_query_invocations": 18,
            "diagnosis_invocations": 0,
        }:
            raise PrepareError(
                "CODEX_LUNA_PREPARE_TOTALS_INVALID",
                "preprocessing invocation totals do not match the 9x(1+2) contract",
            )
        write_json(aggregate_path, aggregate)
        print(json.dumps(aggregate, ensure_ascii=False, sort_keys=True))
        return 0
    except PrepareError as exc:
        output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not aggregate_path.exists():
            write_json(
                aggregate_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "FAIL",
                    "code": exc.code,
                    "message": str(exc),
                },
            )
        print(f"FAIL [{exc.code}]: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
