"""Select the registered package format without changing legacy Methods rules."""

from pathlib import Path
from typing import TypeAlias, TypeGuard

from .asset_snapshot import snapshot_asset
from .document_skill import ResolvedDocumentSkill, load_document_skill_registration
from .methods_skill import (
    ResolvedSpecializedSkillV1,
    _json_object,
    load_specialized_skill_registration as load_methods_skill_registration,
)


ResolvedSpecializedSkill: TypeAlias = ResolvedSpecializedSkillV1 | ResolvedDocumentSkill


def is_document_skill(value: object) -> TypeGuard[ResolvedDocumentSkill]:
    return isinstance(value, ResolvedDocumentSkill)


def load_specialized_skill_registration(registration_root: Path) -> ResolvedSpecializedSkill:
    root = Path(registration_root)
    frozen = snapshot_asset(root)
    if frozen is not None and isinstance(frozen[1], (ResolvedSpecializedSkillV1, ResolvedDocumentSkill)):
        return frozen[1]
    # Leave the legacy loader's diagnostics and closed-directory checks intact.
    path = root / "registration-template.json"
    if path.is_file():
        raw, _ = _json_object(path, label="registration-template.json")
        if type(raw.get("schema_version")) is int and raw["schema_version"] == 3:
            return load_document_skill_registration(root)
    return load_methods_skill_registration(root)


def load_registered_skill_from_package(registration_root: Path) -> tuple[str, Path, str]:
    resolved = load_specialized_skill_registration(registration_root)
    return resolved.registration_id, resolved.package_root, resolved.combined_sha256


__all__ = ["ResolvedSpecializedSkill", "is_document_skill", "load_specialized_skill_registration",
    "load_registered_skill_from_package"]
