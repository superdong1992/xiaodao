"""Resolve one Job's exact versioned assets into deterministic context text."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from problem_locator.contracts import (
    AssetCatalogPort,
    AssetKind,
    ErrorCode,
    ExecutionStage,
    Job,
    JobType,
    ResolvedAsset,
    VersionedRef,
    canonical_json_bytes,
)

from .catalog import hash_product_directory
from .context_builder import ContextMaterials
from .failures import runtime_failure
from .workspace import PreparedWorkspace


@dataclass(frozen=True, slots=True)
class ResolvedContextAssets:
    """Exact resolved assets plus the text handed to Context Builder."""

    profile: ResolvedAsset
    tool_bundle: ResolvedAsset
    context_policy: ResolvedAsset
    output_contract: ResolvedAsset
    skill: ResolvedAsset | None
    available_skills: tuple[ResolvedAsset, ...]
    logparse_tool: ResolvedAsset | None
    materials: ContextMaterials


def _invalid_asset() -> Exception:
    return runtime_failure(
        stage=ExecutionStage.ASSET_RESOLUTION,
        code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
        message="A fixed runtime asset version is unavailable.",
    )


def _parse_manifest(path: Path) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate manifest key")
            value[key] = item
        return value

    def reject_constant(_: str) -> None:
        raise ValueError("non-finite manifest value")

    try:
        metadata = path.stat(follow_symlinks=False)
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("manifest is not an ordinary file")
        value = json.loads(
            path.read_bytes().decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _invalid_asset() from None
    if not isinstance(value, dict):
        raise _invalid_asset() from None
    return value


def _safe_entry(root: Path, relative_value: object) -> Path:
    if not isinstance(relative_value, str) or not relative_value:
        raise _invalid_asset() from None
    relative = PurePosixPath(relative_value)
    if (
        relative.is_absolute()
        or "\\" in relative_value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise _invalid_asset() from None
    target = root.joinpath(*relative.parts)
    try:
        metadata = target.stat(follow_symlinks=False)
        target.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        raise _invalid_asset() from None
    if target.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise _invalid_asset() from None
    return target


def _validate_resolved_asset(
    resolved: ResolvedAsset,
    expected_ref: VersionedRef,
    expected_kind: AssetKind,
) -> Path:
    if resolved.ref != expected_ref or resolved.asset_kind is not expected_kind:
        raise _invalid_asset() from None
    root = Path(resolved.root_path)
    try:
        metadata = root.stat(follow_symlinks=False)
        if root.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("asset root is not a real directory")
        if hash_product_directory(root) != expected_ref.content_hash:
            raise ValueError("asset directory content drifted")
    except (OSError, ValueError):
        raise _invalid_asset() from None
    return root


def _load_entry_text(
    resolved: ResolvedAsset,
    expected_ref: VersionedRef,
    expected_kind: AssetKind,
) -> str:
    root = _validate_resolved_asset(resolved, expected_ref, expected_kind)
    manifest_name = (
        "diagnosis-skill.json"
        if expected_kind is AssetKind.DIAGNOSIS_SKILL
        else "asset.json"
    )
    manifest = _parse_manifest(root / manifest_name)
    entry_field = "entry_document" if manifest_name == "diagnosis-skill.json" else "entry"
    target = _safe_entry(root, manifest.get(entry_field))
    try:
        return target.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise _invalid_asset() from None


def _skill_index_entry(
    resolved: ResolvedAsset,
    expected_ref: VersionedRef,
) -> dict[str, Any]:
    root = _validate_resolved_asset(
        resolved,
        expected_ref,
        AssetKind.DIAGNOSIS_SKILL,
    )
    manifest = _parse_manifest(root / "diagnosis-skill.json")
    required = {
        "schema_version",
        "id",
        "version",
        "capability",
        "summary",
        "entry_document",
        "tool_bundle_id",
        "requires_logparse",
    }
    if set(manifest) not in (required, required | {"logparse_product"}):
        raise _invalid_asset() from None
    if (
        manifest.get("schema_version") != 1
        or not isinstance(manifest.get("capability"), str)
        or not manifest["capability"]
        or not isinstance(manifest.get("summary"), str)
        or not manifest["summary"]
    ):
        raise _invalid_asset() from None
    return {
        "ref": expected_ref.model_dump(mode="json"),
        "capability": manifest["capability"],
        "summary": manifest["summary"],
        "requires_logparse": manifest.get("requires_logparse"),
        "logparse_product": manifest.get("logparse_product"),
    }


class RuntimeAssetResolver:
    """Resolve without substituting Catalog's current role bindings."""

    def __init__(self, catalog: AssetCatalogPort) -> None:
        self._catalog = catalog

    def _resolve(self, ref: VersionedRef, kind: AssetKind) -> ResolvedAsset:
        report = self._catalog.check([ref])
        if not report.available or report.missing_refs:
            raise _invalid_asset() from None
        try:
            resolved = self._catalog.resolve(ref)
        except Exception:
            raise _invalid_asset() from None
        _validate_resolved_asset(resolved, ref, kind)
        return resolved

    def resolve(
        self,
        job: Job,
        workspace: PreparedWorkspace,
    ) -> ResolvedContextAssets:
        profile = self._resolve(job.agent_profile_ref, AssetKind.AGENT_PROFILE)
        tool_bundle = self._resolve(job.tool_bundle_ref, AssetKind.TOOL_BUNDLE)
        context_policy = self._resolve(job.context_policy_ref, AssetKind.CONTEXT_POLICY)
        output_contract = self._resolve(
            job.output_contract_ref,
            AssetKind.OUTPUT_CONTRACT,
        )
        available_skills = tuple(
            self._resolve(ref, AssetKind.DIAGNOSIS_SKILL)
            for ref in job.available_skill_refs
        )
        skill = (
            None
            if job.skill_ref is None
            else self._resolve(job.skill_ref, AssetKind.DIAGNOSIS_SKILL)
        )
        logparse_tool = (
            None
            if job.logparse_tool_ref is None
            else self._resolve(job.logparse_tool_ref, AssetKind.LOGPARSE_TOOL)
        )

        profile_text = _load_entry_text(
            profile,
            job.agent_profile_ref,
            AssetKind.AGENT_PROFILE,
        )
        tool_text = _load_entry_text(
            tool_bundle,
            job.tool_bundle_ref,
            AssetKind.TOOL_BUNDLE,
        )
        output_text = _load_entry_text(
            output_contract,
            job.output_contract_ref,
            AssetKind.OUTPUT_CONTRACT,
        )
        if job.job_type is JobType.ROUTE:
            skill_index = canonical_json_bytes(
                {
                    "schema_version": 1,
                    "skills": [
                        _skill_index_entry(resolved, ref)
                        for resolved, ref in zip(
                            available_skills,
                            job.available_skill_refs,
                            strict=True,
                        )
                    ],
                }
            ).decode("utf-8")
            skill_text = None
        else:
            if skill is None or job.skill_ref is None:
                raise _invalid_asset() from None
            skill_index = None
            skill_text = _load_entry_text(
                skill,
                job.skill_ref,
                AssetKind.DIAGNOSIS_SKILL,
            )

        materials = ContextMaterials(
            profile=profile_text,
            skill=skill_text,
            skill_index=skill_index,
            tool_bundle=tool_text,
            output_contract=output_text,
            manifest=workspace.manifest,
            previous_outcomes=workspace.previous_outcomes,
            evidence=workspace.evidence,
        )
        return ResolvedContextAssets(
            profile=profile,
            tool_bundle=tool_bundle,
            context_policy=context_policy,
            output_contract=output_contract,
            skill=skill,
            available_skills=available_skills,
            logparse_tool=logparse_tool,
            materials=materials,
        )


__all__ = ["ResolvedContextAssets", "RuntimeAssetResolver"]
