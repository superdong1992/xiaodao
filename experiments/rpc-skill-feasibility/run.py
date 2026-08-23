#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_ROOT = Path(__file__).resolve().parent
META_SKILL = REPO_ROOT / ".agents/skills/wiki-to-diagnosis-skill-experiment"
DEFAULT_WIKI = (
    REPO_ROOT
    / "tests/cases/release/rpc-timeout-anonymized/input/wiki.md"
)
DEFAULT_RUNTIME_ROOT = REPO_ROOT / ".tmp/rpc-skill-feasibility"
DEFAULT_LOGPARSE_ROOT = (
    REPO_ROOT.parent
    / "Codex/2026-06-29-github-issue-locator-logparse/logparse"
)
LOGPARSE_PRODUCT = "rpc-skill-feasibility"
MODEL = "gpt-5.6-luna"
REASONING_EFFORT = "medium"
GENERATED_SKILL_NAME = "diagnose-rpc-timeout"
EXPECTED_WIKI_SHA256 = (
    "9138ee2358137fdc1dcc828f08a0f89ebc5e55816f71a1142ccd5d9b8ddc8161"
)
CASE_KEYS = {
    "scenario_id",
    "problem_time",
    "problem",
    "client_process",
    "server_process",
    "expected_status",
    "expected_branch_marker",
    "expected_terms",
}
REQUIRED_WIKI_MARKERS = {
    "rpc call %s:%s timeout limit %u recv no response",
    "%s rpc %s call unsuccess, reqid(%u), timeout %u",
    "LATE_RESPONSE",
    "API_COMPLETE",
    "QUEUE_HISTORY",
    "DEADLOOP_DETECTED",
}
META_CANARIES = {
    "CCCC",
    "BBBB",
    "API_COMPLETE",
    "QUEUE_HISTORY",
    "DEADLOOP_DETECTED",
    "LATE_RESPONSE",
    "rpc call %s:%s",
}
DIAGNOSIS_RESULT_KEYS = {
    "schema_version",
    "scenario_id",
    "status",
    "confirmed_methods",
    "candidate_methods",
    "evidence",
    "limitations",
    "safety_notes",
    "logparse_receipt_sha256",
}
EVIDENCE_KEYS = {"method_id", "anchor", "marker", "summary"}
DIAGNOSIS_TRACE_FORBIDDEN = {
    "mech-target-logs",
    "cli.py parse",
    "logparse-config",
    "case.json",
    "expected_branch",
    "oracle",
    "/tests/cases/",
    "/raw/",
}


class ExperimentError(RuntimeError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class Case:
    root: Path
    data: dict[str, Any]

    @property
    def scenario_id(self) -> str:
        return str(self.data["scenario_id"])


def canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(canonical_bytes(value))
    os.replace(temporary, path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentError("structure", f"cannot read JSON {path}: {exc}") from exc


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def tree_digest(root: Path) -> str:
    records: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.is_symlink():
            continue
        payload = path.read_bytes()
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": len(payload),
                "sha256": sha256_bytes(payload),
            }
        )
    return sha256_bytes(canonical_bytes(records))


def ordinary_file(path: Path, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ExperimentError("structure", f"missing {label}: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ExperimentError("structure", f"{label} is not one ordinary file: {path}")


def run_checked(
    command: list[str],
    *,
    cwd: Path,
    timeout: int = 120,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
    stdout_stream = stdout_path.open("w", encoding="utf-8") if stdout_path else subprocess.PIPE
    stderr_stream = stderr_path.open("w", encoding="utf-8") if stderr_path else subprocess.PIPE
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout_stream,
            stderr=stderr_stream,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExperimentError("execution", f"command timed out: {command[0]}") from exc
    finally:
        if stdout_path:
            stdout_stream.close()
        if stderr_path:
            stderr_stream.close()
    if completed.returncode != 0:
        stdout = "" if stdout_path else str(completed.stdout or "")
        stderr = "" if stderr_path else str(completed.stderr or "")
        detail = (stderr or stdout).strip()
        if stderr_path and stderr_path.exists():
            detail = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
        raise ExperimentError(
            "execution",
            f"command failed ({completed.returncode}): {' '.join(command[:4])}\n{detail[-4000:]}",
        )
    return completed


def git_text(root: Path, *args: str) -> str:
    completed = run_checked(["git", *args], cwd=root)
    return str(completed.stdout).strip()


def load_cases(selected: set[str] | None) -> list[Case]:
    case_root = EXPERIMENT_ROOT / "cases"
    cases: list[Case] = []
    for descriptor in sorted(case_root.glob("*/case.json")):
        data = read_json(descriptor)
        if not isinstance(data, dict) or set(data) != CASE_KEYS:
            raise ExperimentError("structure", f"case contract mismatch: {descriptor}")
        scenario_id = data.get("scenario_id")
        if not isinstance(scenario_id, str) or scenario_id != descriptor.parent.name:
            raise ExperimentError("structure", f"scenario_id mismatch: {descriptor}")
        if selected is not None and scenario_id not in selected:
            continue
        if data.get("expected_status") not in {"CONFIRMED", "INSUFFICIENT"}:
            raise ExperimentError("structure", f"unsupported expected status: {descriptor}")
        marker = data.get("expected_branch_marker")
        if marker is not None and marker not in REQUIRED_WIKI_MARKERS:
            raise ExperimentError("structure", f"invalid expected branch marker: {descriptor}")
        terms = data.get("expected_terms")
        if not isinstance(terms, list) or any(not isinstance(item, str) or not item for item in terms):
            raise ExperimentError("structure", f"invalid expected terms: {descriptor}")
        for label in ("client", "server"):
            ordinary_file(descriptor.parent / "raw" / f"{label}.log", f"{label} raw log")
        cases.append(Case(descriptor.parent, data))
    if not cases:
        raise ExperimentError("structure", "no experiment cases selected")
    if selected is not None:
        missing = selected - {case.scenario_id for case in cases}
        if missing:
            raise ExperimentError("structure", f"unknown cases: {sorted(missing)}")
    return cases


def validate_sources(wiki: Path, cases: list[Case]) -> dict[str, object]:
    ordinary_file(wiki, "manual Wiki")
    wiki_sha256 = sha256_file(wiki)
    if wiki_sha256 != EXPECTED_WIKI_SHA256:
        raise ExperimentError(
            "wiki_drift",
            "the manual Wiki differs from the approved feasibility baseline; refusing to continue",
        )
    ordinary_file(EXPERIMENT_ROOT / "logparse-config.json", "Logparse config")
    ordinary_file(
        EXPERIMENT_ROOT / "schemas/diagnosis-result.schema.json",
        "diagnosis output schema",
    )
    ordinary_file(META_SKILL / "SKILL.md", "meta Skill entrypoint")
    ordinary_file(
        META_SKILL / "references/output-contract.md",
        "meta Skill output contract",
    )
    ordinary_file(
        META_SKILL / "scripts/validate_generated_skill.py",
        "generated Skill validator",
    )
    meta_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(META_SKILL.rglob("*"))
        if path.is_file() and path.suffix in {".md", ".py"}
    )
    leaked = sorted(term for term in META_CANARIES if term in meta_text)
    if leaked:
        raise ExperimentError(
            "generalization",
            f"RPC-specific canaries leaked into the meta Skill: {leaked}",
        )
    for marker in REQUIRED_WIKI_MARKERS:
        if marker not in wiki.read_text(encoding="utf-8"):
            raise ExperimentError("wiki_drift", f"required marker absent from Wiki: {marker}")
    return {
        "wiki_sha256": wiki_sha256,
        "meta_skill_sha256": tree_digest(META_SKILL),
        "case_ids": [case.scenario_id for case in cases],
    }


def logparse_identity(logparse_root: Path) -> dict[str, str]:
    cli = logparse_root / "cli.py"
    python = logparse_root / ".venv/bin/python"
    ordinary_file(cli, "Logparse CLI")
    ordinary_file(python.resolve(), "Logparse Python")
    try:
        head = git_text(logparse_root, "rev-parse", "HEAD")
        status = git_text(logparse_root, "status", "--porcelain=v1", "--untracked-files=all")
    except ExperimentError:
        head = "unavailable"
        status = "unavailable"
    version = run_checked([os.fspath(python), "--version"], cwd=logparse_root)
    python_version = (str(version.stdout) or str(version.stderr)).strip()
    return {
        "git_head": head,
        "git_status_sha256": sha256_bytes(status.encode("utf-8")),
        "cli_sha256": sha256_file(cli),
        "python": python_version,
    }


def case_input_identity(
    case: Case,
    *,
    config: Path,
    logparse: dict[str, str],
) -> dict[str, object]:
    raw_inputs = []
    for label in ("client", "server"):
        path = case.root / "raw" / f"{label}.log"
        raw_inputs.append(
            {
                "label": label,
                "name": path.name,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return {
        "schema_version": 1,
        "scenario_id": case.scenario_id,
        "problem_time": case.data["problem_time"],
        "config_sha256": sha256_file(config),
        "logparse": logparse,
        "raw_inputs": raw_inputs,
    }


def build_case_archive(case: Case, archive: Path) -> None:
    temporary = archive.with_suffix(".zip.tmp")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        temporary,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as bundle:
        for label in ("client", "server"):
            source = case.root / "raw" / f"{label}.log"
            info = zipfile.ZipInfo(f"{label}.log", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            bundle.writestr(info, source.read_bytes())
    os.replace(temporary, archive)


def _target_from_payload(payload: object, label: str) -> dict[str, object]:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ExperimentError("logparse", f"invalid {label} target_logs envelope")
    targets = payload.get("target_logs")
    if not isinstance(targets, list) or len(targets) != 1 or not isinstance(targets[0], dict):
        raise ExperimentError("logparse", f"invalid {label} target_logs count")
    target = targets[0]
    if target.get("match_status") != "exact" or not isinstance(target.get("log_path"), str):
        raise ExperimentError("logparse", f"Logparse did not resolve exact {label} target: {target}")
    return target


def _verify_cached_preprocessing(
    cache: Path,
    *,
    fingerprint: str,
) -> dict[str, object]:
    state = read_json(cache / "state.json")
    if not isinstance(state, dict) or state.get("input_fingerprint") != fingerprint:
        raise ExperimentError(
            "logparse_cache",
            f"cached input identity changed for {cache.name}; refusing a silent reparse",
        )
    if state.get("status") != "COMPLETE":
        raise ExperimentError(
            "logparse_cache",
            f"previous preprocessing is incomplete for {cache.name}; move the cache before retrying",
        )
    receipt_path = cache / "receipt.json"
    ordinary_file(receipt_path, "cached Logparse receipt")
    receipt_sha256 = sha256_file(receipt_path)
    if state.get("receipt_sha256") != receipt_sha256:
        raise ExperimentError("logparse_cache", f"cached receipt changed for {cache.name}")
    receipt = read_json(receipt_path)
    if (
        not isinstance(receipt, dict)
        or receipt.get("parse_invocations") != 1
        or receipt.get("target_query_invocations") != 2
        or receipt.get("status") != "PASS"
    ):
        raise ExperimentError("logparse_cache", f"cached receipt contract failed for {cache.name}")
    frozen = receipt.get("frozen_target_logs")
    if not isinstance(frozen, list) or len(frozen) != 2:
        raise ExperimentError("logparse_cache", f"cached frozen logs are invalid for {cache.name}")
    for item in frozen:
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            raise ExperimentError("logparse_cache", f"cached frozen log item is invalid for {cache.name}")
        path = cache / str(item["file"])
        ordinary_file(path, "cached frozen target log")
        if sha256_file(path) != item.get("sha256"):
            raise ExperimentError("logparse_cache", f"cached frozen target log changed: {path}")
    return {
        "cache": cache,
        "cache_hit": True,
        "receipt": receipt,
        "receipt_sha256": receipt_sha256,
    }


def preprocess_case(
    case: Case,
    *,
    runtime_root: Path,
    logparse_root: Path,
    identity: dict[str, str],
) -> dict[str, object]:
    config = EXPERIMENT_ROOT / "logparse-config.json"
    cache = runtime_root / "preprocessed" / case.scenario_id
    input_identity = case_input_identity(case, config=config, logparse=identity)
    fingerprint = sha256_bytes(canonical_bytes(input_identity))
    state_path = cache / "state.json"
    if state_path.exists():
        return _verify_cached_preprocessing(cache, fingerprint=fingerprint)
    if cache.exists() and any(cache.iterdir()):
        raise ExperimentError(
            "logparse_cache",
            f"preprocessing cache has no state receipt: {cache}",
        )

    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"{case.scenario_id}.zip"
    parsed_root = cache / "parsed"
    frozen_root = cache / "frozen"
    frozen_root.mkdir(parents=True, exist_ok=True)
    build_case_archive(case, archive)
    state = {
        "schema_version": 1,
        "status": "PARSING",
        "input_fingerprint": fingerprint,
        "parse_invocations": 1,
        "target_query_invocations": 0,
    }
    write_json(state_path, state)

    python = logparse_root / ".venv/bin/python"
    cli = logparse_root / "cli.py"
    parse_stdout = cache / "parse.stdout.txt"
    parse_stderr = cache / "parse.stderr.txt"
    try:
        run_checked(
            [
                os.fspath(python),
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
            stdout_path=parse_stdout,
            stderr_path=parse_stderr,
        )
    except ExperimentError:
        state["status"] = "PARSE_FAILED"
        write_json(state_path, state)
        raise

    result_files = sorted(parsed_root.glob("*/result.json"))
    if len(result_files) != 1:
        state["status"] = "TARGET_FAILED"
        write_json(state_path, state)
        raise ExperimentError("logparse", f"expected one parsed task for {case.scenario_id}")
    task_id = result_files[0].parent.name
    frozen_items: list[dict[str, object]] = []
    for label, process_key in (
        ("client", "client_process"),
        ("server", "server_process"),
    ):
        target_stdout = cache / f"target-{label}.json"
        target_stderr = cache / f"target-{label}.stderr.txt"
        run_checked(
            [
                os.fspath(python),
                os.fspath(cli),
                "mech-target-logs",
                task_id,
                "--problem-time",
                str(case.data["problem_time"]),
                "--module",
                "rpc",
                "--slot",
                "1",
                "--process-name",
                str(case.data[process_key]),
                "--label",
                label,
                "-o",
                os.fspath(parsed_root),
                "--explain",
            ],
            cwd=logparse_root,
            timeout=120,
            stdout_path=target_stdout,
            stderr_path=target_stderr,
        )
        state["target_query_invocations"] = int(state["target_query_invocations"]) + 1
        write_json(state_path, state)
        target_payload = read_json(target_stdout)
        target = _target_from_payload(target_payload, label)
        source = Path(str(target["log_path"])).resolve()
        parsed_resolved = parsed_root.resolve()
        if parsed_resolved not in source.parents:
            raise ExperimentError("logparse", f"target path escaped parsed tree: {source}")
        ordinary_file(source, f"{label} selected target log")
        destination = frozen_root / f"{label}.log"
        destination.write_bytes(source.read_bytes())
        frozen_items.append(
            {
                "label": label,
                "process_name": case.data[process_key],
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
        "scenario_id": case.scenario_id,
        "input_fingerprint": fingerprint,
        "parse_invocations": 1,
        "target_query_invocations": 2,
        "logparse_processes_during_diagnosis": 0,
        "logparse_identity": identity,
        "config": {
            "product": LOGPARSE_PRODUCT,
            "sha256": sha256_file(config),
        },
        "raw_inputs": input_identity["raw_inputs"],
        "archive": {
            "name": archive.name,
            "size": archive.stat().st_size,
            "sha256": sha256_file(archive),
        },
        "parsed_tree_sha256": tree_digest(parsed_root),
        "frozen_target_logs": frozen_items,
    }
    receipt_path = cache / "receipt.json"
    write_json(receipt_path, receipt)
    receipt_sha256 = sha256_file(receipt_path)
    state.update(
        {
            "status": "COMPLETE",
            "target_query_invocations": 2,
            "receipt_sha256": receipt_sha256,
        }
    )
    write_json(state_path, state)
    return {
        "cache": cache,
        "cache_hit": False,
        "receipt": receipt,
        "receipt_sha256": receipt_sha256,
    }


def copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )


def init_isolated_git(workspace: Path) -> None:
    run_checked(["git", "init", "-q"], cwd=workspace)


def codex_version(codex_bin: str) -> str:
    completed = run_checked([codex_bin, "--version"], cwd=REPO_ROOT)
    return str(completed.stdout).strip().splitlines()[-1]


def codex_command(
    codex_bin: str,
    *,
    workspace: Path,
    sandbox: str,
    prompt: str,
    trace: Path,
    stderr: Path,
    final: Path,
    output_schema: Path | None = None,
) -> None:
    command = [
        codex_bin,
        "exec",
        "--ephemeral",
        "--json",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--color",
        "never",
        "-m",
        MODEL,
        "-c",
        f'model_reasoning_effort="{REASONING_EFFORT}"',
        "-s",
        sandbox,
        "-C",
        os.fspath(workspace),
        "-o",
        os.fspath(final),
    ]
    if output_schema is not None:
        command.extend(["--output-schema", os.fspath(output_schema)])
    command.append(prompt)
    run_checked(
        command,
        cwd=workspace,
        timeout=1200,
        stdout_path=trace,
        stderr_path=stderr,
    )


def trace_summary(trace: Path) -> dict[str, object]:
    usage: dict[str, object] = {}
    thread_id: str | None = None
    commands: list[str] = []
    errors: list[str] = []
    for number, raw_line in enumerate(trace.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ExperimentError("codex_trace", f"invalid JSONL at {trace}:{number}: {exc}") from exc
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            thread_id = event["thread_id"]
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
        if event.get("type") in {"turn.failed", "error"}:
            errors.append(json.dumps(event, ensure_ascii=False, sort_keys=True))
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "command_execution":
            command = item.get("command")
            commands.append(command if isinstance(command, str) else json.dumps(command, ensure_ascii=False))
    if errors:
        raise ExperimentError("codex_execution", f"Codex trace reports errors: {errors[-1]}")
    return {"thread_id": thread_id, "usage": usage, "commands": commands}


def run_validator(skill_dir: Path, wiki: Path, validator: Path) -> dict[str, object]:
    completed = run_checked(
        [
            sys.executable,
            "-B",
            os.fspath(validator),
            "--skill-dir",
            os.fspath(skill_dir),
            "--wiki",
            os.fspath(wiki),
            "--json",
        ],
        cwd=skill_dir.parent,
    )
    result = json.loads(str(completed.stdout))
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ExperimentError("structure", f"generated Skill validator failed: {result}")
    return result


def map_generated_methods(skill_dir: Path) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
    manifest = read_json(skill_dir / "methods.json")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("methods"), list):
        raise ExperimentError("structure", "generated methods.json is invalid")
    methods = manifest["methods"]
    if len(methods) != 3:
        raise ExperimentError(
            "wiki_fidelity",
            f"the current Wiki must produce three cause methods, got {len(methods)}",
        )
    by_id: dict[str, dict[str, object]] = {}
    for method in methods:
        if not isinstance(method, dict) or not isinstance(method.get("id"), str):
            raise ExperimentError("structure", "generated method item is invalid")
        by_id[str(method["id"])] = method
    package_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(skill_dir.rglob("*"))
        if path.is_file() and path.suffix in {".md", ".json"}
    )
    missing = {marker for marker in REQUIRED_WIKI_MARKERS if marker not in package_text}
    if missing:
        raise ExperimentError("wiki_fidelity", f"generated Skill lost Wiki markers: {sorted(missing)}")

    def marker_set(method: dict[str, object]) -> set[str]:
        return {str(item) for item in method.get("evidence_markers", [])}

    def has_marker(method: dict[str, object], expected: str) -> bool:
        return any(marker == expected or marker.startswith(expected + " ") for marker in marker_set(method))

    api = [
        method
        for method in methods
        if has_marker(method, "API_COMPLETE") and has_marker(method, "DEADLOOP_DETECTED")
    ]
    queue = [method for method in methods if has_marker(method, "QUEUE_HISTORY")]
    client = [
        method
        for method in methods
        if has_marker(method, "LATE_RESPONSE")
        and not any(
            has_marker(method, marker)
            for marker in ("API_COMPLETE", "DEADLOOP_DETECTED", "QUEUE_HISTORY")
        )
    ]
    if len(api) != 1 or len(queue) != 1 or len(client) != 1:
        raise ExperimentError(
            "branch_routing",
            "could not map exactly one generated method to each Wiki cause branch",
        )
    mapping = {
        "API_COMPLETE": str(api[0]["id"]),
        "QUEUE_HISTORY": str(queue[0]["id"]),
        "LATE_RESPONSE": str(client[0]["id"]),
    }
    if len(set(mapping.values())) != 3:
        raise ExperimentError("branch_routing", "generated branch methods are not independent")
    return by_id, mapping


def generate_skill(
    *,
    round_number: int,
    runtime_root: Path,
    wiki: Path,
    codex_bin: str,
) -> dict[str, object]:
    round_root = runtime_root / "rounds" / f"round-{round_number}"
    if round_root.exists():
        raise ExperimentError(
            "iteration",
            f"round {round_number} already exists; use a new round after an evidence-backed change",
        )
    workspace = round_root / "generation/workspace"
    workspace.mkdir(parents=True)
    copy_tree(META_SKILL, workspace / ".agents/skills/wiki-to-diagnosis-skill-experiment")
    (workspace / "generated").mkdir()
    input_root = workspace / "input"
    input_root.mkdir()
    shutil.copyfile(wiki, input_root / "wiki.md")
    init_isolated_git(workspace)
    prompt = """使用 $wiki-to-diagnosis-skill-experiment，把 input/wiki.md 转换成一个名为 diagnose-rpc-timeout 的定位 Skill，并写入 generated/diagnose-rpc-timeout。

要求：
- 人工 Wiki 是唯一业务事实源，不得修改。
- 只生成元 Skill 输出合同允许的文件，不生成旧版 manifest、GenerationSpec、README 或测试框架。
- 生成物必须消费冻结的 target_logs 与 receipt，运行时不能再次调用 Logparse。
- 完成后执行元 Skill 自带的 validate_generated_skill.py；只有校验 PASS 才结束。
"""
    trace = round_root / "generation/codex.jsonl"
    stderr = round_root / "generation/codex.stderr.txt"
    final = round_root / "generation/final.txt"
    codex_command(
        codex_bin,
        workspace=workspace,
        sandbox="workspace-write",
        prompt=prompt,
        trace=trace,
        stderr=stderr,
        final=final,
    )
    summary = trace_summary(trace)
    generated = workspace / "generated" / GENERATED_SKILL_NAME
    validation = run_validator(
        generated,
        input_root / "wiki.md",
        workspace
        / ".agents/skills/wiki-to-diagnosis-skill-experiment/scripts/validate_generated_skill.py",
    )
    methods, mapping = map_generated_methods(generated)
    return {
        "round_root": round_root,
        "workspace": workspace,
        "skill_dir": generated,
        "skill_sha256": tree_digest(generated),
        "validation": validation,
        "methods": methods,
        "branch_mapping": mapping,
        "trace": summary,
    }


def load_generated_round(
    *,
    round_number: int,
    runtime_root: Path,
    wiki: Path,
) -> dict[str, object]:
    round_root = runtime_root / "rounds" / f"round-{round_number}"
    workspace = round_root / "generation/workspace"
    generated = workspace / "generated" / GENERATED_SKILL_NAME
    ordinary_file(round_root / "generation/codex.jsonl", "completed generation trace")
    validation = run_validator(
        generated,
        wiki,
        META_SKILL / "scripts/validate_generated_skill.py",
    )
    methods, mapping = map_generated_methods(generated)
    summary = trace_summary(round_root / "generation/codex.jsonl")
    diagnoses_root = round_root / "diagnoses"
    if diagnoses_root.exists() and any(diagnoses_root.iterdir()):
        raise ExperimentError(
            "iteration",
            "cannot resume a generated round after diagnosis workspaces have been created",
        )
    return {
        "round_root": round_root,
        "workspace": workspace,
        "skill_dir": generated,
        "skill_sha256": tree_digest(generated),
        "validation": validation,
        "methods": methods,
        "branch_mapping": mapping,
        "trace": summary,
    }


def prepare_diagnosis_workspace(
    *,
    case: Case,
    preprocessing: dict[str, object],
    generated_skill: Path,
    root: Path,
) -> tuple[Path, str]:
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    copy_tree(generated_skill, workspace / ".agents/skills" / GENERATED_SKILL_NAME)
    evidence_root = workspace / "evidence"
    input_root = workspace / "input"
    evidence_root.mkdir(parents=True)
    input_root.mkdir(parents=True)
    cache = Path(str(preprocessing["cache"]))
    receipt_path = cache / "receipt.json"
    receipt_sha256 = sha256_file(receipt_path)
    shutil.copyfile(receipt_path, input_root / "logparse-receipt.json")
    target_logs = []
    receipt = preprocessing["receipt"]
    assert isinstance(receipt, dict)
    frozen = receipt["frozen_target_logs"]
    assert isinstance(frozen, list)
    for item in frozen:
        assert isinstance(item, dict)
        label = str(item["label"])
        source = cache / str(item["file"])
        destination = evidence_root / f"{label}.log"
        shutil.copyfile(source, destination)
        target_logs.append(
            {
                "label": label,
                "process_name": item["process_name"],
                "match_status": "exact",
                "log_path": f"evidence/{label}.log",
                "sha256": item["sha256"],
            }
        )
    write_json(
        input_root / "target_logs.json",
        {"schema_version": 1, "target_logs": target_logs},
    )
    (input_root / "problem.md").write_text(
        f"# 待定位问题\n\nscenario_id: `{case.scenario_id}`\n\n{case.data['problem']}\n",
        encoding="utf-8",
    )
    shutil.copyfile(
        EXPERIMENT_ROOT / "schemas/diagnosis-result.schema.json",
        input_root / "diagnosis-result.schema.json",
    )
    init_isolated_git(workspace)
    return workspace, receipt_sha256


def validate_diagnosis_trace(summary: dict[str, object], scenario_id: str) -> None:
    commands = summary.get("commands")
    if not isinstance(commands, list):
        raise ExperimentError("codex_trace", f"missing command trace for {scenario_id}")
    violations: list[str] = []
    for command in commands:
        rendered = str(command)
        lowered = rendered.casefold()
        if any(term.casefold() in lowered for term in DIAGNOSIS_TRACE_FORBIDDEN):
            violations.append(rendered)
        if "find /" in lowered or "rg --files /" in lowered:
            violations.append(rendered)
    if violations:
        raise ExperimentError(
            "input_scope",
            f"diagnosis accessed forbidden inputs for {scenario_id}: {violations}",
        )


def _string_array(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ExperimentError("output_contract", f"{label} must be an array of non-empty strings")
    if len(value) != len(set(value)):
        raise ExperimentError("output_contract", f"{label} must contain unique values")
    return value


def validate_diagnosis_result(
    *,
    case: Case,
    result: object,
    receipt_sha256: str,
    methods: dict[str, dict[str, object]],
    branch_mapping: dict[str, str],
    workspace: Path,
) -> dict[str, object]:
    if not isinstance(result, dict) or set(result) != DIAGNOSIS_RESULT_KEYS:
        raise ExperimentError("output_contract", f"result keys mismatch for {case.scenario_id}")
    if result.get("schema_version") != 1 or result.get("scenario_id") != case.scenario_id:
        raise ExperimentError("output_contract", f"result identity mismatch for {case.scenario_id}")
    if result.get("status") not in {"CONFIRMED", "PARTIAL", "INSUFFICIENT"}:
        raise ExperimentError("output_contract", f"invalid result status for {case.scenario_id}")
    if result.get("logparse_receipt_sha256") != receipt_sha256:
        raise ExperimentError("output_contract", f"receipt hash mismatch for {case.scenario_id}")
    confirmed = _string_array(result.get("confirmed_methods"), "confirmed_methods")
    candidates = _string_array(result.get("candidate_methods"), "candidate_methods")
    known_ids = set(methods)
    if not set(confirmed + candidates).issubset(known_ids):
        raise ExperimentError("branch_routing", f"unknown method id in {case.scenario_id}")
    evidence = result.get("evidence")
    if not isinstance(evidence, list):
        raise ExperimentError("output_contract", f"evidence must be an array for {case.scenario_id}")
    evidence_text = "\n".join(
        (workspace / "evidence" / f"{label}.log").read_text(encoding="utf-8")
        for label in ("client", "server")
    )
    confirmed_with_evidence: set[str] = set()
    for index, item in enumerate(evidence):
        if not isinstance(item, dict) or set(item) != EVIDENCE_KEYS:
            raise ExperimentError("output_contract", f"invalid evidence {index} for {case.scenario_id}")
        if item.get("method_id") not in known_ids or item.get("anchor") not in {"client", "server"}:
            raise ExperimentError("output_contract", f"invalid evidence identity for {case.scenario_id}")
        marker = item.get("marker")
        summary = item.get("summary")
        if not isinstance(marker, str) or not marker or marker not in evidence_text:
            raise ExperimentError(
                "evidence_grounding",
                f"evidence marker is not present in frozen logs for {case.scenario_id}: {marker}",
            )
        if not isinstance(summary, str) or not summary:
            raise ExperimentError("output_contract", f"empty evidence summary for {case.scenario_id}")
        if item.get("method_id") in confirmed:
            confirmed_with_evidence.add(str(item["method_id"]))
    if set(confirmed) != confirmed_with_evidence:
        raise ExperimentError("evidence_grounding", f"confirmed method lacks evidence for {case.scenario_id}")

    limitations = _string_array(result.get("limitations"), "limitations")
    safety_notes = _string_array(result.get("safety_notes"), "safety_notes")
    safety_text = " ".join(safety_notes).casefold()
    if "不等于取消" not in safety_text and not (
        "not" in safety_text and ("cancel" in safety_text or "cancellation" in safety_text)
    ):
        raise ExperimentError("wiki_fidelity", f"timeout cancellation safety note missing in {case.scenario_id}")

    expected_status = str(case.data["expected_status"])
    if result.get("status") != expected_status:
        raise ExperimentError(
            "branch_routing",
            f"expected {expected_status}, got {result.get('status')} for {case.scenario_id}",
        )
    expected_marker = case.data["expected_branch_marker"]
    if expected_marker is None:
        if confirmed:
            raise ExperimentError("overclaim", f"insufficient evidence confirmed a cause: {confirmed}")
        limitation_text = " ".join(limitations).casefold()
        if not any(term in limitation_text for term in ("抑制", "限流", "suppression", "rate limit")):
            raise ExperimentError(
                "wiki_fidelity",
                "insufficient result did not preserve suppression/rate-limit uncertainty",
            )
    else:
        expected_method = branch_mapping[str(expected_marker)]
        if confirmed != [expected_method]:
            raise ExperimentError(
                "branch_routing",
                f"expected only {expected_method}, got {confirmed} for {case.scenario_id}",
            )
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True)
    missing_terms = [term for term in case.data["expected_terms"] if term not in rendered]
    if missing_terms:
        raise ExperimentError(
            "calculation",
            f"result omitted expected queue contributors for {case.scenario_id}: {missing_terms}",
        )
    return {
        "scenario_id": case.scenario_id,
        "status": result["status"],
        "confirmed_methods": confirmed,
        "candidate_methods": candidates,
        "evidence_count": len(evidence),
        "receipt_sha256": receipt_sha256,
    }


def diagnose_case(
    *,
    case: Case,
    preprocessing: dict[str, object],
    generation: dict[str, object],
    codex_bin: str,
) -> dict[str, object]:
    round_root = Path(str(generation["round_root"]))
    diagnosis_root = round_root / "diagnoses" / case.scenario_id
    workspace, receipt_sha256 = prepare_diagnosis_workspace(
        case=case,
        preprocessing=preprocessing,
        generated_skill=Path(str(generation["skill_dir"])),
        root=diagnosis_root,
    )
    prompt = f"""使用 $diagnose-rpc-timeout 定位 input/problem.md 中的问题。

输入边界：
- Logparse 已经完成；只读取 input/target_logs.json 列出的 evidence 日志和 input/logparse-receipt.json。
- 不调用 Logparse，不读取工作区以外路径，不查找 raw、case.json、oracle 或预期答案。
- 检查所有有正向证据的方法，不能在第一条命中后停止。
- 最终只输出符合 input/diagnosis-result.schema.json 的 JSON，文字字段使用自然中文。
- scenario_id 必须是 {case.scenario_id}。
- logparse_receipt_sha256 必须是 {receipt_sha256}。
"""
    trace = diagnosis_root / "codex.jsonl"
    stderr = diagnosis_root / "codex.stderr.txt"
    final = diagnosis_root / "result.json"
    codex_command(
        codex_bin,
        workspace=workspace,
        sandbox="read-only",
        prompt=prompt,
        trace=trace,
        stderr=stderr,
        final=final,
        output_schema=workspace / "input/diagnosis-result.schema.json",
    )
    summary = trace_summary(trace)
    validate_diagnosis_trace(summary, case.scenario_id)
    result = read_json(final)
    validated = validate_diagnosis_result(
        case=case,
        result=result,
        receipt_sha256=receipt_sha256,
        methods=generation["methods"],  # type: ignore[arg-type]
        branch_mapping=generation["branch_mapping"],  # type: ignore[arg-type]
        workspace=workspace,
    )
    validated["trace"] = {
        "thread_id": summary["thread_id"],
        "usage": summary["usage"],
        "command_count": len(summary["commands"]),  # type: ignore[arg-type]
        "logparse_invocations": 0,
    }
    validated["result"] = result
    return validated


def build_promotion(
    *,
    runtime_root: Path,
    round_number: int,
    source_identity: dict[str, object],
    logparse: dict[str, str],
    codex_cli_version: str,
    generation: dict[str, object],
    preprocessing: dict[str, dict[str, object]],
    diagnoses: list[dict[str, object]],
) -> Path:
    promotion = runtime_root / "promotion" / f"round-{round_number}"
    if promotion.exists():
        raise ExperimentError("iteration", f"promotion already exists: {promotion}")
    promotion.mkdir(parents=True)
    copy_tree(Path(str(generation["skill_dir"])), promotion / "generated-skill")
    diagnosis_artifacts = promotion / "diagnoses"
    diagnosis_artifacts.mkdir()
    for item in diagnoses:
        write_json(
            diagnosis_artifacts / f"{item['scenario_id']}.json",
            item["result"],
        )
    result = {
        "schema_version": 1,
        "status": "PASS",
        "scope": "functional-feasibility-only",
        "round": round_number,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "codex_cli_version": codex_cli_version,
        "source": source_identity,
        "logparse_identity": logparse,
        "generated_skill_sha256": generation["skill_sha256"],
        "generation": {
            "validation": generation["validation"],
            "branch_mapping": generation["branch_mapping"],
            "thread_id": generation["trace"]["thread_id"],  # type: ignore[index]
            "usage": generation["trace"]["usage"],  # type: ignore[index]
        },
        "preprocessing": {
            case_id: {
                "cache_hit": item["cache_hit"],
                "parse_invocations": item["receipt"]["parse_invocations"],  # type: ignore[index]
                "target_query_invocations": item["receipt"]["target_query_invocations"],  # type: ignore[index]
                "receipt_sha256": item["receipt_sha256"],
            }
            for case_id, item in sorted(preprocessing.items())
        },
        "diagnoses": diagnoses,
        "claims": {
            "test_flow_used": False,
            "release_verdict": False,
            "logparse_invocations_during_diagnosis": 0,
        },
    }
    write_json(promotion / "results.json", result)
    lines = [
        "# RPC 超时定位 Skill 可行性结果",
        "",
        "结论：功能可行性验证通过。该结论只覆盖本实验，不是 Test Flow 或 Release verdict。",
        "",
        f"- 模型：`{MODEL}`，reasoning effort `{REASONING_EFFORT}`",
        f"- Codex CLI：`{codex_cli_version}`",
        f"- Wiki SHA-256：`{source_identity['wiki_sha256']}`",
        f"- 生成 Skill SHA-256：`{generation['skill_sha256']}`",
        f"- 生成轮次：{round_number}",
        "- Logparse：每个用例 parse 一次、target query 两次；诊断阶段调用零次",
        "",
        "## 场景结果",
        "",
    ]
    for item in diagnoses:
        methods = ", ".join(item["confirmed_methods"]) or "无"
        lines.append(f"- `{item['scenario_id']}`：{item['status']}；确认方法：{methods}")
    lines.extend(
        [
            "",
            "运行摘要见 `results.json`，逐场景完整结果见 `diagnoses/`；原始 Codex JSONL 和 Logparse 解析树只保存在 `.tmp`。",
            "",
        ]
    )
    (promotion / "RESULTS.md").write_text("\n".join(lines), encoding="utf-8")
    return promotion


def record_failure(runtime_root: Path, round_number: int | None, exc: ExperimentError) -> None:
    failure_root = runtime_root / "failures"
    failure_root.mkdir(parents=True, exist_ok=True)
    name = f"round-{round_number}.json" if round_number is not None else "prepare.json"
    write_json(
        failure_root / name,
        {
            "schema_version": 1,
            "status": "FAIL",
            "round": round_number,
            "category": exc.category,
            "message": str(exc),
        },
    )


def parse_selected(raw: str | None) -> set[str] | None:
    if raw is None:
        return None
    selected = {item.strip() for item in raw.split(",") if item.strip()}
    return selected or None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--round", type=int)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument(
        "--reuse-generated",
        action="store_true",
        help="reuse the validated generated Skill from the selected round",
    )
    parser.add_argument("--cases", help="comma-separated scenario IDs; defaults to all")
    parser.add_argument("--wiki", type=Path, default=DEFAULT_WIKI)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--logparse-root", type=Path, default=DEFAULT_LOGPARSE_ROOT)
    parser.add_argument("--codex-bin", default="codex")
    args = parser.parse_args()
    if args.prepare_only and args.round is not None:
        parser.error("--prepare-only and --round cannot be used together")
    if args.prepare_only and args.reuse_generated:
        parser.error("--prepare-only and --reuse-generated cannot be used together")
    if not args.prepare_only and args.round not in {1, 2, 3}:
        parser.error("a full run requires --round 1, 2, or 3")

    runtime_root = args.runtime_root.resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    round_number = args.round
    wiki = args.wiki.resolve()
    wiki_before = sha256_file(wiki)
    try:
        cases = load_cases(parse_selected(args.cases))
        source_identity = validate_sources(wiki, cases)
        identity = logparse_identity(args.logparse_root.resolve())
        preprocessing: dict[str, dict[str, object]] = {}
        for case in cases:
            print(f"[prepare] {case.scenario_id}", flush=True)
            preprocessing[case.scenario_id] = preprocess_case(
                case,
                runtime_root=runtime_root,
                logparse_root=args.logparse_root.resolve(),
                identity=identity,
            )
        if args.prepare_only:
            print(
                json.dumps(
                    {
                        "status": "PASS",
                        "wiki_sha256": source_identity["wiki_sha256"],
                        "preprocessing": {
                            case_id: {
                                "cache_hit": item["cache_hit"],
                                "parse_invocations": item["receipt"]["parse_invocations"],
                                "target_query_invocations": item["receipt"]["target_query_invocations"],
                                "receipt_sha256": item["receipt_sha256"],
                            }
                            for case_id, item in preprocessing.items()
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
            )
            return 0

        assert round_number is not None
        cli_version = codex_version(args.codex_bin)
        if args.reuse_generated:
            print(f"[generate] reuse validated round {round_number}", flush=True)
            generation = load_generated_round(
                round_number=round_number,
                runtime_root=runtime_root,
                wiki=wiki,
            )
        else:
            print(f"[generate] round {round_number} with {MODEL}", flush=True)
            generation = generate_skill(
                round_number=round_number,
                runtime_root=runtime_root,
                wiki=wiki,
                codex_bin=args.codex_bin,
            )
        diagnoses: list[dict[str, object]] = []
        for case in cases:
            print(f"[diagnose] {case.scenario_id}", flush=True)
            diagnoses.append(
                diagnose_case(
                    case=case,
                    preprocessing=preprocessing[case.scenario_id],
                    generation=generation,
                    codex_bin=args.codex_bin,
                )
            )
        if {case.scenario_id for case in cases} != {
            path.parent.name for path in (EXPERIMENT_ROOT / "cases").glob("*/case.json")
        }:
            raise ExperimentError(
                "iteration",
                "a subset run cannot be promoted; rerun all cases after affected cases pass",
            )
        promotion = build_promotion(
            runtime_root=runtime_root,
            round_number=round_number,
            source_identity=source_identity,
            logparse=identity,
            codex_cli_version=cli_version,
            generation=generation,
            preprocessing=preprocessing,
            diagnoses=diagnoses,
        )
        print(json.dumps({"status": "PASS", "promotion": os.fspath(promotion)}, ensure_ascii=False))
        return 0
    except ExperimentError as exc:
        record_failure(runtime_root, round_number, exc)
        print(f"FAIL [{exc.category}]: {exc}", file=sys.stderr)
        return 1
    finally:
        if wiki.exists() and sha256_file(wiki) != wiki_before:
            print("FATAL: the manual Wiki changed during the experiment", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
