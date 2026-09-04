"""Immutable runtime catalog for built-ins and registered Methods Skills."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from problem_locator.contracts import (
    ApplicationError,
    ApplicationPortError,
    AssetAvailabilityReport,
    AssetKind,
    DiagnosisMode,
    ErrorCode,
    JobType,
    LogparseBrokerFactory,
    PORT_ERROR_CODES,
    ResolvedAsset,
    RuntimeBindings,
    ReviewPolicy,
    VersionedRef,
    default_resource_limits,
)

from .catalog_hash import hash_product_directory
from .methods_skill import (
    ResolvedSpecializedSkillV1,
    load_specialized_skill_registration,
)


BUILTIN_ASSET_ROOT = Path(__file__).with_name("assets")
_DEFAULT_ASSET_VERSION = "1.0.0"
_GENERIC_SKILL_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def _catalog_port_error(operation: str, code: ErrorCode, message: str) -> ApplicationPortError:
    method_key = f"AssetCatalogPort.{operation}"
    if code not in PORT_ERROR_CODES[method_key]:
        raise AssertionError(f"{code.value} is not allowed for {method_key}")
    return ApplicationPortError(
        ApplicationError(code=code, message=message, details=[], retryable=False)
    )


@dataclass(frozen=True, slots=True)
class _BuiltinSpec:
    relative_root: str
    asset_kind: AssetKind
    asset_id: str
    version: str = _DEFAULT_ASSET_VERSION


@dataclass(frozen=True, slots=True)
class _SkillDescriptor:
    resolved_asset: ResolvedAsset
    specialized: ResolvedSpecializedSkillV1

    @property
    def capability(self) -> str:
        return self.specialized.registration.capability

    @property
    def deployment_scope(self) -> str:
        return self.specialized.registration.deployment_scope

    @property
    def summary(self) -> str:
        return self.specialized.registration.summary

    @property
    def requires_logparse(self) -> bool:
        return self.specialized.registration.preprocessing.requires_logparse

    @property
    def logparse_product(self) -> str | None:
        return self.specialized.registration.preprocessing.logparse_product


_BUILTIN_SPECS = (
    _BuiltinSpec("profiles/router", AssetKind.AGENT_PROFILE, "agent-profile/router"),
    _BuiltinSpec("profiles/specialist", AssetKind.AGENT_PROFILE, "agent-profile/specialist", "7.0.0"),
    _BuiltinSpec("profiles/reviewer", AssetKind.AGENT_PROFILE, "agent-profile/reviewer", "7.0.0"),
    _BuiltinSpec("profiles/generic-locator", AssetKind.AGENT_PROFILE, "agent-profile/generic-locator", "2.0.0"),
    _BuiltinSpec("tool-bundles/router", AssetKind.TOOL_BUNDLE, "tool-bundle/router", "3.0.0"),
    _BuiltinSpec("tool-bundles/diagnose", AssetKind.TOOL_BUNDLE, "tool-bundle/diagnose", "4.0.0"),
    _BuiltinSpec("tool-bundles/review", AssetKind.TOOL_BUNDLE, "tool-bundle/review", "3.0.0"),
    _BuiltinSpec("tool-bundles/generic-locator", AssetKind.TOOL_BUNDLE, "tool-bundle/generic-locator"),
    _BuiltinSpec("context-policies/route", AssetKind.CONTEXT_POLICY, "context-policy/route"),
    _BuiltinSpec("context-policies/diagnose", AssetKind.CONTEXT_POLICY, "context-policy/diagnose"),
    _BuiltinSpec("context-policies/review", AssetKind.CONTEXT_POLICY, "context-policy/review", "3.0.0"),
    _BuiltinSpec("context-policies/generic-locator", AssetKind.CONTEXT_POLICY, "context-policy/generic-locator"),
    _BuiltinSpec("output-contracts/route", AssetKind.OUTPUT_CONTRACT, "output-contract/route", "5.0.0"),
    _BuiltinSpec("output-contracts/diagnose", AssetKind.OUTPUT_CONTRACT, "output-contract/diagnose", "10.0.0"),
    _BuiltinSpec("output-contracts/review", AssetKind.OUTPUT_CONTRACT, "output-contract/review", "10.0.0"),
    _BuiltinSpec("output-contracts/generic-locator", AssetKind.OUTPUT_CONTRACT, "output-contract/generic-locator", "2.0.0"),
)
_BUILTIN_SPECS_BY_ID = {item.asset_id: item for item in _BUILTIN_SPECS}


def _ref_key(ref: VersionedRef) -> tuple[str, str, str]:
    return ref.id, ref.version, ref.content_hash


def _clone(value: Any) -> Any:
    return value.model_copy(deep=True)


def _parse_json_object(path: Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON key in {path.name}: {key}")
            value[key] = item
        return value

    try:
        metadata = path.stat(follow_symlinks=False)
        maximum_links = 2 if os.name == "nt" else 1
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > maximum_links:
            raise ValueError("manifest is not an ordinary file")
        value = json.loads(
            path.read_bytes().decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON value: {item}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid UTF-8 JSON manifest: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"asset manifest must be a JSON object: {path}")
    return value


def _safe_product_entry(root: Path, relative_value: Any, *, manifest_name: str) -> None:
    if not isinstance(relative_value, str) or not relative_value or "\\" in relative_value:
        raise ValueError("asset entry must be a safe relative POSIX path")
    relative = PurePosixPath(relative_value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("asset entry must be a safe relative POSIX path")
    if relative.name == manifest_name and len(relative.parts) == 1:
        raise ValueError("asset manifest cannot also be its entry")
    path = root.joinpath(*relative.parts)
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError("asset entry is unavailable") from exc
    maximum_links = 2 if os.name == "nt" else 1
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > maximum_links:
        raise ValueError("asset entry must be one ordinary file")


def _load_builtin(root: Path, spec: _BuiltinSpec) -> ResolvedAsset:
    manifest = _parse_json_object(root / "asset.json")
    if set(manifest) != {"schema_version", "asset_kind", "id", "version", "entry"}:
        raise ValueError("asset.json fields are invalid")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("asset.json schema_version must equal integer 1")
    if (
        manifest["asset_kind"] != spec.asset_kind.value
        or manifest["id"] != spec.asset_id
        or manifest["version"] != spec.version
    ):
        raise ValueError(f"built-in asset identity mismatch for {spec.asset_id}")
    _safe_product_entry(root, manifest["entry"], manifest_name="asset.json")
    return ResolvedAsset(
        ref=VersionedRef(
            id=spec.asset_id,
            version=spec.version,
            content_hash=hash_product_directory(root),
        ),
        asset_kind=spec.asset_kind,
        root_path=str(root.resolve()),
    )


def _load_skill(root: Path) -> _SkillDescriptor:
    specialized = load_specialized_skill_registration(root)
    registration = specialized.registration
    resolved = ResolvedAsset(
        ref=VersionedRef(
            id=f"diagnosis-skill/{registration.registration_id}",
            version=registration.version,
            content_hash=specialized.combined_sha256,
        ),
        asset_kind=AssetKind.DIAGNOSIS_SKILL,
        root_path=str(specialized.registration_root),
    )
    return _SkillDescriptor(resolved_asset=resolved, specialized=specialized)


class VersionedAssetCatalog:
    """Startup-scanned implementation of ``AssetCatalogPort``."""

    def __init__(
        self,
        *,
        skill_dir: Path,
        assets_root: Path = BUILTIN_ASSET_ROOT,
        logparse_tool: ResolvedAsset | None = None,
        logparse_broker_factory: LogparseBrokerFactory | None = None,
        generic_skill_name: str,
        specialized_reviewer_enabled: bool = False,
        allow_test_skills: bool = False,
    ) -> None:
        if type(allow_test_skills) is not bool:
            raise TypeError("allow_test_skills must be boolean")
        if type(specialized_reviewer_enabled) is not bool:
            raise TypeError("specialized_reviewer_enabled must be boolean")
        if (
            not isinstance(generic_skill_name, str)
            or len(generic_skill_name) > 64
            or _GENERIC_SKILL_NAME_PATTERN.fullmatch(generic_skill_name) is None
        ):
            raise ValueError("generic_skill_name must be a lowercase hyphen Skill name")
        if (logparse_tool is None) != (logparse_broker_factory is None):
            raise ValueError("logparse asset and factory must be supplied together")
        if logparse_broker_factory is not None and not isinstance(
            logparse_broker_factory, LogparseBrokerFactory
        ):
            raise TypeError("logparse_broker_factory must implement LogparseBrokerFactory")

        self._assets: dict[tuple[str, str, str], ResolvedAsset] = {}
        self._id_versions: dict[tuple[str, str], str] = {}
        self._builtin_refs: dict[str, VersionedRef] = {}
        self._skills: dict[tuple[str, str, str], _SkillDescriptor] = {}
        self._logparse_tool_ref: VersionedRef | None = None
        self._logparse_broker_factory = logparse_broker_factory
        self._generic_skill_name = generic_skill_name
        self._specialized_review_policy = (
            ReviewPolicy.INDEPENDENT
            if specialized_reviewer_enabled
            else ReviewPolicy.NONE
        )

        root = Path(assets_root)
        for spec in _BUILTIN_SPECS:
            resolved = _load_builtin(root.joinpath(*PurePosixPath(spec.relative_root).parts), spec)
            self._register(resolved)
            self._builtin_refs[spec.asset_id] = _clone(resolved.ref)

        descriptors = self._scan_skills(Path(skill_dir))
        test_only = sorted(
            item.resolved_asset.ref.id
            for item in descriptors
            if item.deployment_scope == "TEST_ONLY"
        )
        if test_only and not allow_test_skills:
            raise ValueError(
                "TEST_ONLY diagnosis skills are forbidden in the production catalog: "
                + ", ".join(test_only)
            )
        for descriptor in descriptors:
            self._register(descriptor.resolved_asset)
            self._skills[_ref_key(descriptor.resolved_asset.ref)] = descriptor

        if logparse_tool is not None:
            if not isinstance(logparse_tool, ResolvedAsset):
                raise TypeError("logparse_tool must be a ResolvedAsset")
            if logparse_tool.asset_kind is not AssetKind.LOGPARSE_TOOL:
                raise ValueError("logparse_tool must have asset_kind=LOGPARSE_TOOL")
            external_root = Path(logparse_tool.root_path)
            try:
                metadata = external_root.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError("logparse tool root_path is unavailable") from exc
            if external_root.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("logparse tool root_path must be a real directory")
            normalized = ResolvedAsset(
                ref=_clone(logparse_tool.ref),
                asset_kind=logparse_tool.asset_kind,
                root_path=str(external_root.resolve()),
            )
            self._register(normalized)
            self._logparse_tool_ref = _clone(normalized.ref)
        if self._logparse_tool_ref is None and any(item.requires_logparse for item in descriptors):
            raise ValueError("Logparse Methods Skill requires a paired logparse asset and factory")

        self._route_skill_refs = tuple(
            _clone(item.resolved_asset.ref)
            for item in sorted(
                descriptors,
                key=lambda descriptor: (
                    descriptor.resolved_asset.ref.id,
                    descriptor.resolved_asset.ref.version,
                    descriptor.resolved_asset.ref.content_hash,
                ),
            )
        )

    @staticmethod
    def _scan_skills(skill_dir: Path) -> tuple[_SkillDescriptor, ...]:
        try:
            metadata = skill_dir.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError("SKILL_DIR is unavailable") from exc
        if skill_dir.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("SKILL_DIR must be a real directory")
        try:
            children = sorted(skill_dir.iterdir(), key=lambda item: item.name)
        except (OSError, UnicodeError) as exc:
            raise ValueError("SKILL_DIR cannot be scanned deterministically") from exc
        descriptors: list[_SkillDescriptor] = []
        for child in children:
            if child.is_symlink():
                raise ValueError(f"SKILL_DIR links are forbidden: {child.name}")
            if not child.is_dir():
                continue
            legacy = child / "diagnosis-skill.json"
            if legacy.exists() or legacy.is_symlink():
                raise ValueError(
                    f"legacy diagnosis-skill.json registration is forbidden: {child.name}"
                )
            registration = child / "registration-template.json"
            if registration.is_symlink():
                raise ValueError(f"registration template links are forbidden: {child.name}")
            if registration.exists():
                descriptors.append(_load_skill(child))
        return tuple(descriptors)

    def _register(self, resolved: ResolvedAsset) -> None:
        ref = resolved.ref
        identity = (ref.id, ref.version)
        if identity in self._id_versions:
            raise ValueError(f"duplicate asset id/version is forbidden: {ref.id}@{ref.version}")
        key = _ref_key(ref)
        if key in self._assets:
            raise ValueError(f"duplicate exact asset ref is forbidden: {ref.id}@{ref.version}")
        self._id_versions[identity] = ref.content_hash
        self._assets[key] = _clone(resolved)

    def _builtin_ref(self, asset_id: str) -> VersionedRef:
        spec = _BUILTIN_SPECS_BY_ID[asset_id]
        ref = self._builtin_refs[asset_id]
        resolved = self._assets[_ref_key(ref)]
        if resolved.asset_kind is not spec.asset_kind or resolved.ref != ref:
            raise ValueError("built-in binding is invalid")
        return _clone(ref)

    @staticmethod
    def _skill_is_current(descriptor: _SkillDescriptor) -> bool:
        try:
            current = load_specialized_skill_registration(
                descriptor.specialized.registration_root
            )
            return (
                current.registration_id == descriptor.specialized.registration_id
                and current.combined_sha256 == descriptor.specialized.combined_sha256
                and current.package_tree_sha256 == descriptor.specialized.package_tree_sha256
            )
        except (OSError, TypeError, ValueError):
            return False

    def _asset_is_current(self, resolved: ResolvedAsset) -> bool:
        if resolved.asset_kind is AssetKind.LOGPARSE_TOOL:
            return True
        if resolved.asset_kind is AssetKind.DIAGNOSIS_SKILL:
            descriptor = self._skills.get(_ref_key(resolved.ref))
            return descriptor is not None and self._skill_is_current(descriptor)
        try:
            return hash_product_directory(Path(resolved.root_path)) == resolved.ref.content_hash
        except (OSError, TypeError, ValueError):
            return False

    def _ref_is_current(self, ref: VersionedRef) -> bool:
        try:
            resolved = self._assets.get(_ref_key(ref))
        except (AttributeError, TypeError, ValueError):
            return False
        return isinstance(resolved, ResolvedAsset) and resolved.ref == ref and self._asset_is_current(resolved)

    def check(self, refs: Sequence[VersionedRef]) -> AssetAvailabilityReport:
        missing = [_clone(ref) for ref in refs if not self._ref_is_current(ref)]
        return AssetAvailabilityReport(available=not missing, missing_refs=missing)

    def resolve(self, ref: VersionedRef) -> ResolvedAsset:
        try:
            resolved = self._assets.get(_ref_key(ref))
        except (AttributeError, TypeError, ValueError):
            resolved = None
        if not isinstance(resolved, ResolvedAsset) or not self._ref_is_current(ref):
            raise _catalog_port_error(
                "resolve",
                ErrorCode.ASSET_VERSION_UNAVAILABLE,
                "The requested pinned asset version is unavailable.",
            ) from None
        return _clone(resolved)

    def resolved_specialized_skill(self, ref: VersionedRef) -> ResolvedSpecializedSkillV1:
        """Return the current registration/package resolution for Test Flow and runtime."""

        descriptor = self._skills.get(_ref_key(ref))
        if descriptor is None or not self._ref_is_current(ref):
            raise _catalog_port_error(
                "resolve",
                ErrorCode.ASSET_VERSION_UNAVAILABLE,
                "The requested pinned Methods Skill is unavailable.",
            ) from None
        return load_specialized_skill_registration(descriptor.specialized.registration_root)

    def route_bindings(self, user_fact_names: Sequence[str] = ()) -> RuntimeBindings:
        try:
            if isinstance(user_fact_names, (str, bytes)):
                raise TypeError
            names = tuple(user_fact_names)
            if any(not isinstance(item, str) or not item for item in names) or len(names) != len(set(names)):
                raise ValueError
            bindings = RuntimeBindings(
                diagnosis_mode=None,
                review_policy=None,
                generic_skill_name=None,
                agent_profile_ref=self._builtin_ref("agent-profile/router"),
                available_skill_refs=[_clone(ref) for ref in self._route_skill_refs],
                skill_ref=None,
                tool_bundle_ref=self._builtin_ref("tool-bundle/router"),
                context_policy_ref=self._builtin_ref("context-policy/route"),
                output_contract_ref=self._builtin_ref("output-contract/route"),
                logparse_tool_ref=None,
                logparse_product=None,
                resource_limits=default_resource_limits(JobType.ROUTE),
            )
            refs = (
                bindings.agent_profile_ref,
                *bindings.available_skill_refs,
                bindings.tool_bundle_ref,
                bindings.context_policy_ref,
                bindings.output_contract_ref,
            )
            if not all(self._ref_is_current(ref) for ref in refs):
                raise ValueError
        except Exception:
            raise _catalog_port_error(
                "route_bindings",
                ErrorCode.CONFIG_INVALID,
                "The route runtime binding configuration is invalid.",
            ) from None
        return _clone(bindings)

    def _descriptor(self, skill_ref: VersionedRef, operation: str) -> _SkillDescriptor:
        try:
            descriptor = self._skills.get(_ref_key(skill_ref))
        except (AttributeError, TypeError, ValueError):
            descriptor = None
        if descriptor is None or not self._ref_is_current(skill_ref):
            raise _catalog_port_error(
                operation,
                ErrorCode.ASSET_VERSION_UNAVAILABLE,
                f"The pinned {operation.removesuffix('_bindings')} runtime bindings are unavailable.",
            ) from None
        return descriptor

    def diagnose_bindings(self, skill_ref: VersionedRef) -> RuntimeBindings:
        descriptor = self._descriptor(skill_ref, "diagnose_bindings")
        registration = descriptor.specialized.registration
        preprocessing = registration.preprocessing
        if preprocessing.requires_logparse and (
            self._logparse_tool_ref is None or not self._ref_is_current(self._logparse_tool_ref)
        ):
            raise _catalog_port_error(
                "diagnose_bindings",
                ErrorCode.ASSET_VERSION_UNAVAILABLE,
                "The pinned diagnosis runtime bindings are unavailable.",
            ) from None
        try:
            bindings = RuntimeBindings(
                diagnosis_mode=DiagnosisMode.SPECIALIZED,
                review_policy=self._specialized_review_policy,
                generic_skill_name=None,
                agent_profile_ref=self._builtin_ref(registration.diagnose.agent_profile_id),
                available_skill_refs=[],
                skill_ref=_clone(descriptor.resolved_asset.ref),
                tool_bundle_ref=self._builtin_ref(registration.diagnose.tool_bundle_id),
                context_policy_ref=self._builtin_ref(registration.diagnose.context_policy_id),
                output_contract_ref=self._builtin_ref(registration.diagnose.output_contract_id),
                logparse_tool_ref=(
                    _clone(self._logparse_tool_ref) if preprocessing.requires_logparse else None
                ),
                logparse_product=preprocessing.logparse_product,
                resource_limits=default_resource_limits(JobType.DIAGNOSE),
            )
        except Exception:
            raise _catalog_port_error(
                "diagnose_bindings",
                ErrorCode.CONFIG_INVALID,
                "The diagnosis runtime binding configuration is invalid.",
            ) from None
        return _clone(bindings)

    def generic_diagnose_bindings(self) -> RuntimeBindings:
        try:
            bindings = RuntimeBindings(
                diagnosis_mode=DiagnosisMode.GENERIC,
                review_policy=None,
                generic_skill_name=self._generic_skill_name,
                agent_profile_ref=self._builtin_ref("agent-profile/generic-locator"),
                available_skill_refs=[],
                skill_ref=None,
                tool_bundle_ref=self._builtin_ref("tool-bundle/generic-locator"),
                context_policy_ref=self._builtin_ref("context-policy/generic-locator"),
                output_contract_ref=self._builtin_ref("output-contract/generic-locator"),
                logparse_tool_ref=None,
                logparse_product=None,
                resource_limits=default_resource_limits(JobType.DIAGNOSE),
            )
        except Exception:
            raise _catalog_port_error(
                "generic_diagnose_bindings",
                ErrorCode.CONFIG_INVALID,
                "The generic diagnosis runtime binding configuration is invalid.",
            ) from None
        return _clone(bindings)

    def review_bindings(self, skill_ref: VersionedRef) -> RuntimeBindings:
        descriptor = self._descriptor(skill_ref, "review_bindings")
        registration = descriptor.specialized.registration
        try:
            bindings = RuntimeBindings(
                diagnosis_mode=None,
                review_policy=self._specialized_review_policy,
                generic_skill_name=None,
                agent_profile_ref=self._builtin_ref(registration.review.agent_profile_id),
                available_skill_refs=[],
                skill_ref=_clone(descriptor.resolved_asset.ref),
                tool_bundle_ref=self._builtin_ref(registration.review.tool_bundle_id),
                context_policy_ref=self._builtin_ref(registration.review.context_policy_id),
                output_contract_ref=self._builtin_ref(registration.review.output_contract_id),
                logparse_tool_ref=None,
                logparse_product=None,
                resource_limits=default_resource_limits(JobType.REVIEW),
            )
        except Exception:
            raise _catalog_port_error(
                "review_bindings",
                ErrorCode.CONFIG_INVALID,
                "The review runtime binding configuration is invalid.",
            ) from None
        return _clone(bindings)


AssetCatalog = VersionedAssetCatalog

__all__ = [
    "AssetCatalog",
    "BUILTIN_ASSET_ROOT",
    "VersionedAssetCatalog",
    "hash_product_directory",
]
