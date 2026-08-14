from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath


_ARCHIVE_PROJECTION = "logparse-current-loose-diagnostic-v2"
_CONFIG_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z")
_TIMESTAMP_REGEX = (
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
    r"([+-]\d{2}:\d{2})?"
)
_DIAGNOSTIC_PATTERN = (
    r"Service=[^;]*;\s*Slot=(?P<Slot>[^;]+);\s*"
    r"CPU-Id=(?P<CPU_Id>[^;]*);\s*"
    r"ProcessName=(?P<ProcessName>[^;]+);\s*"
    r"Context=(?P<Context>.*)\)$"
)
_DISCOVERY_PLUGIN = "backend.extensions.products.current.scanner.ScannerPlugin"
_PARSER_PLUGIN = "backend.extensions.products.current.parser.ParserPlugin"
_MECHANISM_PLUGIN = "backend.extensions.mechanisms.module1.Module1Plugin"


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def safe_relative(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise SystemExit("release case path is invalid")
    result = PurePosixPath(value)
    if result.is_absolute() or any(part in {"", ".", ".."} for part in result.parts):
        raise SystemExit("release case path is invalid")
    return result


def ordinary_file(root: Path, relative: object) -> Path:
    path = root.joinpath(*safe_relative(relative).parts)
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise SystemExit("release case input must be an ordinary single-link file")
    return path


def single_case(release_root: Path) -> Path:
    candidates = sorted(
        path.parent for path in release_root.glob("*/case.json") if path.is_file()
    )
    if len(candidates) != 1:
        raise SystemExit("exactly one reviewed release case is required")
    return candidates[0]


def product_digest(root: Path) -> str:
    records = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise SystemExit("approved Skill contains a non-ordinary file")
        payload = path.read_bytes()
        records.append(
            {
                "path": path.name,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    if [item["path"] for item in records] != ["SKILL.md", "diagnosis-skill.json"]:
        raise SystemExit("approved Skill file set is invalid")
    return hashlib.sha256(canonical(records)).hexdigest()


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit("release case generator module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _CONFIG_IDENTIFIER.fullmatch(value):
        raise SystemExit(f"release case {label} is invalid")
    return value


def exact_binding_value(binding: object, facts: dict[str, str], label: str) -> str:
    if not isinstance(binding, dict):
        raise SystemExit(f"release case {label} binding is invalid")
    if set(binding) == {"source", "value"} and binding.get("source") == "SKILL_FIXED":
        return identifier(binding.get("value"), label)
    if set(binding) == {"name", "source"} and binding.get("source") == "USER_FACT":
        name = binding.get("name")
        if not isinstance(name, str) or name not in facts:
            raise SystemExit(f"release case {label} fact binding is invalid")
        return identifier(facts[name], label)
    raise SystemExit(f"release case {label} binding is unsupported")


def driver_facts(driver: dict[str, object]) -> dict[str, str]:
    names = driver.get("initial_user_fact_names")
    values = driver.get("initial_user_fact_values")
    if (
        not isinstance(names, list)
        or not isinstance(values, list)
        or len(names) != len(values)
        or any(not isinstance(item, str) or not item for item in names)
        or any(not isinstance(item, str) for item in values)
        or len(set(names)) != len(names)
    ):
        raise SystemExit("release case initial facts are invalid")
    return dict(zip(names, values, strict=True))


def normalized_problem_time(skill_manifest: dict[str, object], facts: dict[str, str]) -> str:
    plan = skill_manifest.get("logparse_plan")
    if not isinstance(plan, dict):
        raise SystemExit("release case logparse plan is invalid")
    binding = plan.get("problem_time_binding")
    if not isinstance(binding, dict) or set(binding) != {"name", "source"}:
        raise SystemExit("release case problem time binding is invalid")
    if binding.get("source") != "USER_FACT" or binding.get("name") not in facts:
        raise SystemExit("release case problem time fact is unavailable")
    raw = facts[str(binding["name"])]
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise SystemExit("release case problem time is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise SystemExit("release case problem time must include a timezone")
    return parsed.isoformat(timespec="microseconds")


def build_logparse_projection(
    skill_manifest: dict[str, object],
    driver: dict[str, object],
) -> tuple[dict[str, object], dict[str, dict[str, str]], str]:
    product = identifier(skill_manifest.get("logparse_product"), "logparse product")
    facts = driver_facts(driver)
    timestamp = normalized_problem_time(skill_manifest, facts)
    plan = skill_manifest.get("logparse_plan")
    assert isinstance(plan, dict)
    anchors = plan.get("anchors")
    attachment_names = driver.get("attachment_files")
    attachment_labels = driver.get("attachment_anchor_names")
    if (
        not isinstance(anchors, list)
        or not anchors
        or not isinstance(attachment_names, list)
        or not isinstance(attachment_labels, list)
        or len(attachment_names) != len(attachment_labels)
        or not attachment_names
    ):
        raise SystemExit("release case attachment anchors are invalid")
    anchors_by_label: dict[str, dict[str, object]] = {}
    for anchor in anchors:
        if not isinstance(anchor, dict):
            raise SystemExit("release case logparse anchor is invalid")
        label = identifier(anchor.get("label"), "anchor label")
        if label in anchors_by_label:
            raise SystemExit("release case logparse anchor is duplicated")
        if anchor.get("pid") is not None:
            raise SystemExit("release case runtime projection does not accept PID anchors")
        anchors_by_label[label] = anchor

    projections: dict[str, dict[str, str]] = {}
    basenames: set[str] = set()
    modules: set[str] = set()
    projected_labels: set[str] = set()
    for raw_path, raw_label in zip(attachment_names, attachment_labels, strict=True):
        relative = safe_relative(raw_path).as_posix()
        label = identifier(raw_label, "attachment anchor label")
        if label in projected_labels:
            raise SystemExit("release case attachment anchor is duplicated")
        projected_labels.add(label)
        anchor = anchors_by_label.get(label)
        if anchor is None:
            raise SystemExit("release case attachment anchor is not declared by the Skill")
        basename = PurePosixPath(relative).name
        if basename in basenames or relative in projections:
            raise SystemExit("release case attachment filenames are ambiguous")
        basenames.add(basename)
        module = exact_binding_value(anchor.get("module"), facts, "anchor module")
        slot = exact_binding_value(anchor.get("slot"), facts, "anchor slot")
        process = exact_binding_value(
            anchor.get("process_name"), facts, "anchor process name"
        )
        modules.add(module)
        projections[relative] = {
            "label": label,
            "module": module,
            "slot": slot,
            "process": process,
            "timestamp": timestamp,
        }
    if set(anchors_by_label) != projected_labels:
        raise SystemExit("release case Skill anchors and attachment anchors differ")

    mechanisms = {
        module: {
            "plugin": _MECHANISM_PLUGIN,
            "enabled": True,
            "depends_on": [],
            "config": {
                "module_name": module,
                "diag_pattern": _DIAGNOSTIC_PATTERN,
                "active_master_keyword": "",
                "lifecycle_split": {
                    "process_name_mapping": {},
                    "reliable_processes": [],
                    "multi_instance_processes": [],
                },
                "sequence_pattern": r"(?!)",
            },
        }
        for module in sorted(modules)
    }
    config = {
        "schema_version": 2,
        "pipeline": {
            "debug_expand_gz": False,
            "extraction_workers": "auto",
            "diagnostic_scan_workers": "auto",
            "keep_workspace": False,
        },
        "products": {
            product: {
                "archive": {
                    "recursive_extraction": False,
                    "compressed_extensions": [".zip"],
                },
                "discovery": {
                    "plugin": _DISCOVERY_PLUGIN,
                    "config": {
                        "loose_diagnostics": {
                            "enabled": True,
                            "file_patterns": sorted(basenames),
                        },
                        "filename_timestamp_regex": r"(?!)",
                    },
                },
                "parser": {
                    "plugin": _PARSER_PLUGIN,
                    "config": {
                        "timestamp_regex": _TIMESTAMP_REGEX,
                        "active_period_gap_seconds": 300,
                    },
                },
                "mechanisms": mechanisms,
            }
        },
    }
    return config, projections, product


def projected_log_bytes(payload: bytes, projection: dict[str, str]) -> bytes:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SystemExit("release case attachment is not UTF-8") from exc
    if "\x00" in text:
        raise SystemExit("release case attachment contains NUL")
    lines = text.splitlines()
    if not lines:
        raise SystemExit("release case attachment is empty")
    try:
        initial_timestamp = datetime.fromisoformat(projection["timestamp"])
        rendered = []
        for ordinal, line in enumerate(lines):
            timestamp = (initial_timestamp + timedelta(microseconds=ordinal)).isoformat(
                timespec="microseconds"
            )
            prefix = (
                f'{timestamp} {projection["module"].upper()} '
                f'Service=release-case; Slot={projection["slot"]}; CPU-Id=0; '
                f'ProcessName={projection["process"]}; Context='
            )
            rendered.append(f"{prefix}{line})")
    except (OverflowError, ValueError) as exc:
        raise SystemExit("release case projection timestamp is invalid") from exc
    return ("\n".join(rendered) + "\n").encode("utf-8")


def build_archive(
    case_root: Path,
    driver_path: Path,
    target: Path,
    projections: dict[str, dict[str, str]],
) -> tuple[int, str, int]:
    driver = json.loads(driver_path.read_bytes())
    attachments = driver.get("attachment_files")
    if not isinstance(attachments, list) or not attachments:
        raise SystemExit("journey scenario requires attachment files")
    scenario_root = driver_path.parent
    temporary = target.with_suffix(target.suffix + ".tmp")
    if temporary.exists():
        raise SystemExit("temporary archive already exists")
    with zipfile.ZipFile(
        temporary,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for relative in attachments:
            source = ordinary_file(scenario_root, relative)
            name = safe_relative(relative).as_posix()
            if name not in projections:
                raise SystemExit("release case attachment projection is missing")
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(
                info,
                projected_log_bytes(source.read_bytes(), projections[name]),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    payload = temporary.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if target.exists():
        if target.read_bytes() != payload:
            raise SystemExit("existing release archive differs from the deterministic payload")
        temporary.unlink()
    else:
        os.replace(temporary, target)
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise SystemExit("generated release archive is corrupt")
        count = len(archive.infolist())
    return len(payload), digest, count


def write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise SystemExit("release case runtime config already exists") from exc
    path.chmod(0o600)


def smoke_test_logparse(
    *,
    repo: Path,
    python: Path,
    archive: Path,
    config: Path,
    product: str,
    projections: dict[str, dict[str, str]],
) -> None:
    cli = ordinary_file(repo, "cli.py")
    environment = {
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
    }
    with tempfile.TemporaryDirectory(prefix="release-logparse-", dir="/tmp") as raw:
        output = Path(raw) / "output"
        completed = subprocess.run(
            [
                os.fspath(python),
                os.fspath(cli),
                "parse",
                os.fspath(archive),
                "-c",
                os.fspath(config),
                "-o",
                os.fspath(output),
                "--product",
                product,
            ],
            cwd=repo,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            raise SystemExit("release case Logparse smoke parse failed")
        results = list(output.glob("*/result.json"))
        if len(results) != 1:
            raise SystemExit("release case Logparse smoke output is invalid")
        result = json.loads(results[0].read_bytes())
        modules = result.get("mech_results")
        if not isinstance(modules, list):
            raise SystemExit("release case Logparse mechanism output is invalid")
        for projection in projections.values():
            module_matches = [
                item
                for item in modules
                if isinstance(item, dict)
                and projection["module"].casefold()
                in {
                    str(item.get("module_key", "")).casefold(),
                    str(item.get("module_name", "")).casefold(),
                }
            ]
            if len(module_matches) != 1:
                raise SystemExit("release case Logparse module projection is invalid")
            slots = module_matches[0].get("slots")
            matching_slots = [
                item
                for item in slots if isinstance(item, dict) and item.get("slot_id") == projection["slot"]
            ] if isinstance(slots, list) else []
            if len(matching_slots) != 1:
                raise SystemExit("release case Logparse slot projection is invalid")
            processes = [
                process
                for cycle in matching_slots[0].get("board_cycles", [])
                if isinstance(cycle, dict)
                for process in cycle.get("processes", [])
                if isinstance(process, dict)
            ]
            if sum(
                1 for item in processes if item.get("process_name") == projection["process"]
            ) != 1:
                raise SystemExit("release case Logparse process projection is invalid")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--generator", type=Path, required=True)
    parser.add_argument("--validator", type=Path, required=True)
    parser.add_argument("--generated-skills", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--logparse-config", type=Path, required=True)
    parser.add_argument("--logparse-python", type=Path, required=True)
    parser.add_argument("--logparse-repo", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()

    case_root = single_case(args.release_root)
    descriptor = json.loads(ordinary_file(case_root, "case.json").read_bytes())
    scenario_id = descriptor.get("journey_scenario")
    scenario = next(
        (
            item
            for item in descriptor.get("scenarios", [])
            if isinstance(item, dict) and item.get("scenario_id") == scenario_id
        ),
        None,
    )
    if scenario is None:
        raise SystemExit("journey scenario is not declared")
    driver_path = ordinary_file(case_root, scenario.get("driver"))
    generator = load_module(args.generator, "_release_case_generator_v5")
    validator = load_module(args.validator, "_release_case_validator_v5")
    generation_spec = generator.load_generation_spec(
        ordinary_file(case_root, descriptor["generation_spec"])
    )
    generated_result = generator.generate_diagnosis_skill(
        generation_spec,
        args.generated_skills,
    )
    validation = validator.validate_skill_directory(generated_result.skill_dir)
    if not validation.ok:
        raise SystemExit("generated Skill failed validation")

    skill_manifest = json.loads(
        ordinary_file(case_root, f'{descriptor["approved_skill_dir"]}/diagnosis-skill.json').read_bytes()
    )
    skill_id = skill_manifest.get("id")
    generated = args.generated_skills / str(skill_id)
    approved = case_root.joinpath(*safe_relative(descriptor["approved_skill_dir"]).parts)
    if not generated.is_dir() or product_digest(generated) != product_digest(approved):
        raise SystemExit("generated Skill differs from the reviewed approved product")

    driver = json.loads(driver_path.read_bytes())
    logparse_config, projections, logparse_product = build_logparse_projection(
        skill_manifest,
        driver,
    )
    config_payload = canonical(logparse_config)
    write_new(args.logparse_config, config_payload)

    archive_name = f'{descriptor["case_id"]}.zip'
    archive_path = args.evidence_root / archive_name
    size, digest, member_count = build_archive(
        case_root,
        driver_path,
        archive_path,
        projections,
    )
    smoke_test_logparse(
        repo=args.logparse_repo,
        python=args.logparse_python,
        archive=archive_path,
        config=args.logparse_config,
        product=logparse_product,
        projections=projections,
    )
    receipt = {
        "schema_version": 1,
        "status": "PASS",
        "case_id": descriptor["case_id"],
        "scenario_id": scenario_id,
        "skill_id": skill_id,
        "skill_product_digest": product_digest(generated),
        "logparse_product": logparse_product,
        "logparse_config_size": len(config_payload),
        "logparse_config_sha256": hashlib.sha256(config_payload).hexdigest(),
        "archive_projection": _ARCHIVE_PROJECTION,
        "archive_name": archive_name,
        "archive_content_type": "application/zip",
        "archive_size": size,
        "archive_sha256": digest,
        "archive_member_count": member_count,
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    if args.receipt.exists():
        raise SystemExit("release case receipt already exists")
    args.receipt.write_bytes(canonical(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
