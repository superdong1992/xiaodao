from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath


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


def build_archive(case_root: Path, driver_path: Path, target: Path) -> tuple[int, str, int]:
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
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--generator", type=Path, required=True)
    parser.add_argument("--validator", type=Path, required=True)
    parser.add_argument("--generated-skills", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
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

    archive_name = f'{descriptor["case_id"]}.zip'
    archive_path = args.evidence_root / archive_name
    size, digest, member_count = build_archive(case_root, driver_path, archive_path)
    receipt = {
        "schema_version": 1,
        "status": "PASS",
        "case_id": descriptor["case_id"],
        "scenario_id": scenario_id,
        "skill_id": skill_id,
        "skill_product_digest": product_digest(generated),
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
