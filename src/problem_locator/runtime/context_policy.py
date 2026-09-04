"""Resolve one Job's exact versioned assets into deterministic context text."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from problem_locator.contracts import (
    ApplicationErrorDetail,
    ApplicationPortError,
    AssetCatalogPort,
    AssetKind,
    DiagnosisMode,
    ErrorCode,
    ExecutionStage,
    Job,
    JobType,
    ResolvedAsset,
    VersionedRef,
    canonical_json_bytes,
)
from problem_locator.contracts.methods_v2 import (
    MethodEvidenceGraphV2,
    MethodEvaluationPlanV2,
)

from .catalog import hash_product_directory
from .context_builder import (
    ContextMaterials,
    build_methods_review_method_cards_v2,
    build_methods_specialist_method_cards_v2,
)
from .failures import runtime_failure
from .methods_skill import (
    ResolvedSpecializedSkillV1,
    load_specialized_skill_registration,
)
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


@dataclass(frozen=True, slots=True)
class ResolvedJobAssets:
    """Exact Job assets resolved before any mutable application-state read."""

    profile: ResolvedAsset
    tool_bundle: ResolvedAsset
    context_policy: ResolvedAsset
    output_contract: ResolvedAsset
    skill: ResolvedAsset | None
    available_skills: tuple[ResolvedAsset, ...]
    logparse_tool: ResolvedAsset | None
    profile_text: str
    tool_bundle_text: str
    output_contract_text: str
    skill_text: str | None
    skill_index_text: str | None

    def bind_workspace(
        self,
        workspace: PreparedWorkspace,
        *,
        job: Job | None = None,
        loaded_method_ids: Sequence[str] | None = None,
        methods_evidence_graph: MethodEvidenceGraphV2 | None = None,
        methods_evaluation_plan: MethodEvaluationPlanV2 | None = None,
    ) -> ResolvedContextAssets:
        """Attach only the already-frozen Workspace view to context materials."""

        skill_text = self.skill_text
        methods_skill = None
        method_cards = ()
        if (methods_evidence_graph is None) != (methods_evaluation_plan is None):
            raise ValueError("Methods V2 Graph and Plan must be supplied together")
        if methods_evidence_graph is not None:
            if job is None or self.skill is None or methods_evaluation_plan is None:
                raise ValueError("Methods V2 context binding requires its Job and Skill")
            planned_method_ids = tuple(
                item.method_id for item in methods_evaluation_plan.evaluations
            )
            if loaded_method_ids is not None and tuple(loaded_method_ids) != planned_method_ids:
                raise ValueError("loaded_method_ids must match the Evaluation Plan")
            loaded_method_ids = planned_method_ids
            methods_skill = load_specialized_skill_registration(
                Path(self.skill.root_path)
            )
            if job.methods_review_target is None:
                method_cards = build_methods_specialist_method_cards_v2(
                    skill=methods_skill,
                    job=job,
                    graph=methods_evidence_graph,
                    plan=methods_evaluation_plan,
                )
            else:
                method_cards = build_methods_review_method_cards_v2(
                    skill=methods_skill,
                    target=job.methods_review_target,
                    plan=methods_evaluation_plan,
                )
        if loaded_method_ids is not None:
            if self.skill is None:
                raise _invalid_asset() from None
            skill_text = _load_entry_text(
                self.skill,
                self.skill.ref,
                AssetKind.DIAGNOSIS_SKILL,
                loaded_method_ids=loaded_method_ids,
            )
        methods_v1_specialist = (
            loaded_method_ids is not None
            and methods_evidence_graph is None
            and workspace.manifest.job_type is JobType.DIAGNOSE
            and workspace.manifest.resolved_logparse_plan is not None
        )
        materials = ContextMaterials(
            profile=self.profile_text,
            skill=skill_text,
            skill_index=self.skill_index_text,
            tool_bundle=self.tool_bundle_text,
            output_contract=self.output_contract_text,
            manifest=workspace.manifest,
            previous_outcomes=(
                ()
                if workspace.manifest.job_type is JobType.REVIEW
                or methods_v1_specialist
                else workspace.previous_outcomes
            ),
            evidence=workspace.evidence,
            methods_evidence_graph=methods_evidence_graph,
            methods_evaluation_plan=methods_evaluation_plan,
            methods_skill=methods_skill,
            methods_method_cards=method_cards,
        )
        return ResolvedContextAssets(
            profile=self.profile,
            tool_bundle=self.tool_bundle,
            context_policy=self.context_policy,
            output_contract=self.output_contract,
            skill=self.skill,
            available_skills=self.available_skills,
            logparse_tool=self.logparse_tool,
            materials=materials,
        )


@dataclass(frozen=True, slots=True)
class _ResolvedSkillSnapshot:
    """One validated Methods Skill view reused within a single Job resolution."""

    asset: ResolvedAsset
    specialized: ResolvedSpecializedSkillV1


def _invalid_asset(
    details: Iterable[ApplicationErrorDetail] = (),
) -> Exception:
    return runtime_failure(
        stage=ExecutionStage.ASSET_RESOLUTION,
        code=ErrorCode.ASSET_VERSION_UNAVAILABLE,
        message="A fixed runtime asset version is unavailable.",
        details=details,
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
        maximum_links = 2 if os.name == "nt" else 1
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink > maximum_links
        ):
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
    maximum_links = 2 if os.name == "nt" else 1
    if (
        target.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink > maximum_links
    ):
        raise _invalid_asset() from None
    return target


def _validate_resolved_asset(
    resolved: ResolvedAsset,
    expected_ref: VersionedRef,
    expected_kind: AssetKind,
) -> tuple[Path, ResolvedSpecializedSkillV1 | None]:
    if resolved.ref != expected_ref or resolved.asset_kind is not expected_kind:
        raise _invalid_asset() from None
    root = Path(resolved.root_path)
    specialized: ResolvedSpecializedSkillV1 | None = None
    try:
        metadata = root.stat(follow_symlinks=False)
        if root.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("asset root is not a real directory")
        # Built-in assets and Diagnosis Skills use the S04 product-directory
        # hash.  LOGPARSE_TOOL uses S07's wider runtime fingerprint (repository,
        # config, interpreter path and version), so its paired Catalog/Broker
        # is the authority and this resolver must not substitute a second hash
        # algorithm for that frozen ref.
        if expected_kind is AssetKind.DIAGNOSIS_SKILL:
            specialized = load_specialized_skill_registration(root)
            if specialized.combined_sha256 != expected_ref.content_hash:
                raise ValueError("registered Methods Skill content drifted")
        elif (
            expected_kind is not AssetKind.LOGPARSE_TOOL
            and hash_product_directory(root) != expected_ref.content_hash
        ):
            raise ValueError("asset directory content drifted")
    except (OSError, ValueError):
        raise _invalid_asset() from None
    return root, specialized


def _load_entry_text(
    resolved: ResolvedAsset,
    expected_ref: VersionedRef,
    expected_kind: AssetKind,
    *,
    loaded_method_ids: Sequence[str] | None = None,
) -> str:
    root, specialized = _validate_resolved_asset(resolved, expected_ref, expected_kind)
    if expected_kind is AssetKind.DIAGNOSIS_SKILL:
        assert specialized is not None
        package_root = specialized.package_root
        if loaded_method_ids is None:
            selected_method_ids = tuple(
                item.id for item in specialized.methods.methods
            )
        else:
            selected_method_ids = tuple(loaded_method_ids)
            if (
                any(not isinstance(item, str) or not item for item in selected_method_ids)
                or len(selected_method_ids) != len(set(selected_method_ids))
                or not set(selected_method_ids).issubset(
                    specialized.methods.method_by_id
                )
            ):
                raise _invalid_asset() from None
        selected = set(selected_method_ids)
        ordered_paths = [
            "SKILL.md",
            "methods.json",
            *specialized.methods.shared_references,
            *(
                item.reference
                for item in specialized.methods.methods
                if item.id in selected
            ),
        ]
        rendered: list[str] = []
        seen: set[str] = set()
        for relative in ordered_paths:
            if relative in seen:
                continue
            seen.add(relative)
            target = _safe_entry(package_root, relative)
            try:
                text = target.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                raise _invalid_asset() from None
            rendered.append(
                f'<<<METHODS_SKILL_FILE path="{relative}">>>\n'
                f"{text.rstrip()}\n"
                "<<<END METHODS_SKILL_FILE>>>"
            )
        return "\n".join(rendered) + "\n"
    manifest_name = (
        "asset.json"
    )
    manifest = _parse_manifest(root / manifest_name)
    target = _safe_entry(root, manifest.get("entry"))
    try:
        return target.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise _invalid_asset() from None


def _skill_index_entry(
    specialized: ResolvedSpecializedSkillV1,
    expected_ref: VersionedRef,
) -> dict[str, Any]:
    registration = specialized.registration
    return {
        "ref": expected_ref.model_dump(mode="json"),
        "capability": registration.capability,
        "summary": registration.summary,
        "required_user_inputs": list(specialized.methods.required_user_inputs),
        "required_artifacts": list(specialized.methods.required_artifacts),
        "requires_logparse": registration.preprocessing.requires_logparse,
        "logparse_product": registration.preprocessing.logparse_product,
    }


class RuntimeAssetResolver:
    """Resolve without substituting Catalog's current role bindings."""

    def __init__(self, catalog: AssetCatalogPort) -> None:
        self._catalog = catalog

    def _resolve(self, ref: VersionedRef, kind: AssetKind) -> ResolvedAsset:
        try:
            resolved = self._catalog.resolve(ref)
        except ApplicationPortError as exc:
            if exc.error.code is ErrorCode.ASSET_VERSION_UNAVAILABLE:
                raise _invalid_asset(exc.error.details) from None
            raise _invalid_asset() from None
        except Exception:
            raise _invalid_asset() from None
        _validate_resolved_asset(resolved, ref, kind)
        return resolved

    def _resolve_skill(self, ref: VersionedRef) -> _ResolvedSkillSnapshot:
        try:
            resolved = self._catalog.resolve(ref)
        except ApplicationPortError as exc:
            if exc.error.code is ErrorCode.ASSET_VERSION_UNAVAILABLE:
                raise _invalid_asset(exc.error.details) from None
            raise _invalid_asset() from None
        except Exception:
            raise _invalid_asset() from None
        _root, specialized = _validate_resolved_asset(
            resolved,
            ref,
            AssetKind.DIAGNOSIS_SKILL,
        )
        assert specialized is not None
        return _ResolvedSkillSnapshot(asset=resolved, specialized=specialized)

    def resolve_job(self, job: Job) -> ResolvedJobAssets:
        """Resolve every exact version and entry before reading Case state."""

        profile = self._resolve(job.agent_profile_ref, AssetKind.AGENT_PROFILE)
        tool_bundle = self._resolve(job.tool_bundle_ref, AssetKind.TOOL_BUNDLE)
        context_policy = self._resolve(job.context_policy_ref, AssetKind.CONTEXT_POLICY)
        output_contract = self._resolve(
            job.output_contract_ref,
            AssetKind.OUTPUT_CONTRACT,
        )
        available_skill_snapshots = tuple(
            self._resolve_skill(ref)
            for ref in job.available_skill_refs
        )
        available_skills = tuple(
            snapshot.asset for snapshot in available_skill_snapshots
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
                    "schema_version": 2,
                    "skills": [
                        _skill_index_entry(snapshot.specialized, ref)
                        for snapshot, ref in zip(
                            available_skill_snapshots,
                            job.available_skill_refs,
                            strict=True,
                        )
                    ],
                }
            ).decode("utf-8")
            skill_text = None
        elif job.diagnosis_mode is DiagnosisMode.GENERIC:
            if skill is not None or job.skill_ref is not None:
                raise _invalid_asset() from None
            skill_index = None
            skill_text = None
        else:
            if skill is None or job.skill_ref is None:
                raise _invalid_asset() from None
            skill_index = None
            skill_text = _load_entry_text(
                skill,
                job.skill_ref,
                AssetKind.DIAGNOSIS_SKILL,
                loaded_method_ids=(),
            )

        return ResolvedJobAssets(
            profile=profile,
            tool_bundle=tool_bundle,
            context_policy=context_policy,
            output_contract=output_contract,
            skill=skill,
            available_skills=available_skills,
            logparse_tool=logparse_tool,
            profile_text=profile_text,
            tool_bundle_text=tool_text,
            output_contract_text=output_text,
            skill_text=skill_text,
            skill_index_text=skill_index,
        )

    def resolve(
        self,
        job: Job,
        workspace: PreparedWorkspace,
    ) -> ResolvedContextAssets:
        """Compatibility convenience for callers with a prepared Workspace."""

        return self.resolve_job(job).bind_workspace(workspace)


__all__ = ["ResolvedContextAssets", "ResolvedJobAssets", "RuntimeAssetResolver"]
