"""Immutable, exact-version runtime asset catalog.

The catalog is deliberately a startup object.  It fingerprints all bundled
assets and diagnosis skills once, then resolves only complete ``VersionedRef``
values for the lifetime of the process.
"""

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
    AssetAvailabilityReport,
    AssetKind,
    JobType,
    LogparseBrokerFactory,
    ResolvedAsset,
    RuntimeBindings,
    VersionedRef,
    bytes_sha256,
    canonical_json_sha256,
    default_resource_limits,
)


BUILTIN_ASSET_ROOT = Path(__file__).with_name("assets")
_ASSET_VERSION = "1.0.0"
_SKILL_ID_PATTERN = re.compile(r"[a-z][a-z0-9-]{1,63}\Z")
_WINDOWS_DRIVE_PATTERN = re.compile(r"[A-Za-z]:")


@dataclass(frozen=True, slots=True)
class _BuiltinSpec:
    relative_root: str
    asset_kind: AssetKind
    asset_id: str


@dataclass(frozen=True, slots=True)
class _SkillDescriptor:
    resolved_asset: ResolvedAsset
    capability: str
    summary: str
    entry_document: str
    tool_bundle_id: str
    requires_logparse: bool
    logparse_product: str | None


_BUILTIN_SPECS = (
    _BuiltinSpec("profiles/router", AssetKind.AGENT_PROFILE, "agent-profile/router"),
    _BuiltinSpec(
        "profiles/specialist",
        AssetKind.AGENT_PROFILE,
        "agent-profile/specialist",
    ),
    _BuiltinSpec("profiles/reviewer", AssetKind.AGENT_PROFILE, "agent-profile/reviewer"),
    _BuiltinSpec("tool-bundles/router", AssetKind.TOOL_BUNDLE, "tool-bundle/router"),
    _BuiltinSpec(
        "tool-bundles/diagnose",
        AssetKind.TOOL_BUNDLE,
        "tool-bundle/diagnose",
    ),
    _BuiltinSpec("tool-bundles/review", AssetKind.TOOL_BUNDLE, "tool-bundle/review"),
    _BuiltinSpec(
        "context-policies/route",
        AssetKind.CONTEXT_POLICY,
        "context-policy/route",
    ),
    _BuiltinSpec(
        "context-policies/diagnose",
        AssetKind.CONTEXT_POLICY,
        "context-policy/diagnose",
    ),
    _BuiltinSpec(
        "context-policies/review",
        AssetKind.CONTEXT_POLICY,
        "context-policy/review",
    ),
    _BuiltinSpec(
        "output-contracts/route",
        AssetKind.OUTPUT_CONTRACT,
        "output-contract/route",
    ),
    _BuiltinSpec(
        "output-contracts/diagnose",
        AssetKind.OUTPUT_CONTRACT,
        "output-contract/diagnose",
    ),
    _BuiltinSpec(
        "output-contracts/review",
        AssetKind.OUTPUT_CONTRACT,
        "output-contract/review",
    ),
)


def _ref_key(ref: VersionedRef) -> tuple[str, str, str]:
    return (ref.id, ref.version, ref.content_hash)


def _clone_model(value: Any) -> Any:
    return value.model_copy(deep=True)


def _safe_relative_path(value: str, *, field_name: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty relative POSIX path")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8") from exc
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "\\" in value
        or "//" in value
        or value.endswith("/")
        or _WINDOWS_DRIVE_PATTERN.match(value)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"{field_name} must be a safe relative POSIX path")
    return path


def _is_excluded_product_path(path: PurePosixPath) -> bool:
    return (
        path.name == ".DS_Store"
        or path.name.endswith(".pyc")
        or "__pycache__" in path.parts
        or ".pytest_cache" in path.parts
        or path.name == ".managed"
        or path.name.startswith(".managed.")
        or path.name == ".codex-managed"
    )


def _directory_entries(root: Path) -> tuple[dict[str, Any], ...]:
    """Return the normalized full-product file list, rejecting unsafe nodes."""

    root = Path(root)
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise ValueError(f"asset product directory is unavailable: {root}") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError(f"asset product root must be a real directory: {root}")

    result: list[dict[str, Any]] = []

    def visit(directory: Path, prefix: tuple[str, ...]) -> None:
        try:
            children = list(os.scandir(directory))
        except OSError as exc:
            raise ValueError(f"asset product directory cannot be scanned: {directory}") from exc
        try:
            children.sort(key=lambda entry: entry.name)
        except UnicodeError as exc:
            raise ValueError("asset product contains a non-UTF-8 path") from exc

        for child in children:
            parts = (*prefix, child.name)
            relative_text = "/".join(parts)
            relative = _safe_relative_path(
                relative_text,
                field_name="asset product path",
            )
            try:
                child_stat = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError(f"asset product node cannot be inspected: {relative_text}") from exc
            mode = child_stat.st_mode
            if stat.S_ISLNK(mode):
                raise ValueError(f"asset product links are forbidden: {relative_text}")
            if stat.S_ISDIR(mode):
                if _is_excluded_product_path(relative):
                    continue
                visit(Path(child.path), parts)
                continue
            if not stat.S_ISREG(mode):
                raise ValueError(
                    f"asset product contains a non-ordinary node: {relative_text}"
                )
            if child_stat.st_nlink != 1:
                raise ValueError(f"asset product hard links are forbidden: {relative_text}")
            if _is_excluded_product_path(relative):
                continue
            try:
                data = Path(child.path).read_bytes()
            except OSError as exc:
                raise ValueError(f"asset product file cannot be read: {relative_text}") from exc
            result.append(
                {
                    "path": relative_text,
                    "size": len(data),
                    "sha256": bytes_sha256(data),
                }
            )

    visit(root, ())
    result.sort(key=lambda entry: entry["path"])
    return tuple(result)


def hash_product_directory(root: Path) -> str:
    """Hash a complete product directory using the frozen V1 preimage."""

    entries = _directory_entries(root)
    return canonical_json_sha256({"version": 1, "entries": list(entries)})


def _parse_json_object(path: Path) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key in {path.name}: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON value in {path.name}: {value}")

    try:
        data = path.read_bytes()
        parsed = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid UTF-8 JSON manifest: {path}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"asset manifest must be a JSON object: {path}")
    return parsed


def _require_exact_fields(
    value: dict[str, Any],
    *,
    required: set[str],
    optional: set[str] | frozenset[str] = frozenset(),
    manifest_name: str,
) -> None:
    fields = set(value)
    missing = required - fields
    extra = fields - required - optional
    if missing or extra:
        raise ValueError(
            f"{manifest_name} fields are invalid; "
            f"missing={sorted(missing)!r}, extra={sorted(extra)!r}"
        )


def _require_product_entry(
    root: Path,
    relative_value: str,
    *,
    manifest_name: str,
) -> str:
    relative = _safe_relative_path(relative_value, field_name="entry document")
    if relative.name == manifest_name and len(relative.parts) == 1:
        raise ValueError(f"{manifest_name} cannot also be its product entry")
    if _is_excluded_product_path(relative):
        raise ValueError("asset entry cannot name an excluded product file")
    target = root.joinpath(*relative.parts)
    try:
        target_stat = target.lstat()
    except OSError as exc:
        raise ValueError(f"asset entry is unavailable: {relative_value}") from exc
    if not stat.S_ISREG(target_stat.st_mode) or stat.S_ISLNK(target_stat.st_mode):
        raise ValueError(f"asset entry must be an ordinary file: {relative_value}")
    if target_stat.st_nlink != 1:
        raise ValueError(f"asset entry cannot be a hard link: {relative_value}")
    return relative.as_posix()


def _load_builtin(root: Path, expected: _BuiltinSpec) -> ResolvedAsset:
    content_hash = hash_product_directory(root)
    manifest = _parse_json_object(root / "asset.json")
    _require_exact_fields(
        manifest,
        required={"schema_version", "asset_kind", "id", "version", "entry"},
        manifest_name="asset.json",
    )
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("asset.json schema_version must equal integer 1")
    if manifest["asset_kind"] != expected.asset_kind.value:
        raise ValueError(f"asset.json asset_kind mismatch for {expected.asset_id}")
    if manifest["id"] != expected.asset_id:
        raise ValueError(f"asset.json id mismatch for {expected.relative_root}")
    if manifest["version"] != _ASSET_VERSION:
        raise ValueError(f"built-in asset version must equal {_ASSET_VERSION}")
    if not isinstance(manifest["entry"], str):
        raise ValueError("asset.json entry must be a string")
    _require_product_entry(root, manifest["entry"], manifest_name="asset.json")
    return ResolvedAsset(
        ref=VersionedRef(
            id=expected.asset_id,
            version=_ASSET_VERSION,
            content_hash=content_hash,
        ),
        asset_kind=expected.asset_kind,
        root_path=str(root.resolve()),
    )


def _load_skill(root: Path) -> _SkillDescriptor:
    content_hash = hash_product_directory(root)
    manifest_path = root / "diagnosis-skill.json"
    manifest = _parse_json_object(manifest_path)
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
    _require_exact_fields(
        manifest,
        required=required,
        optional={"logparse_product"},
        manifest_name="diagnosis-skill.json",
    )
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("diagnosis-skill.json schema_version must equal integer 1")
    skill_id = manifest["id"]
    if not isinstance(skill_id, str) or _SKILL_ID_PATTERN.fullmatch(skill_id) is None:
        raise ValueError("diagnosis skill id does not match the frozen pattern")
    version = manifest["version"]
    if not isinstance(version, str) or not version:
        raise ValueError("diagnosis skill version must be a non-empty string")
    capability = manifest["capability"]
    summary = manifest["summary"]
    if not isinstance(capability, str) or not capability:
        raise ValueError("diagnosis skill capability must be a non-empty string")
    if not isinstance(summary, str) or not summary:
        raise ValueError("diagnosis skill summary must be a non-empty string")
    tool_bundle_id = manifest["tool_bundle_id"]
    if tool_bundle_id != "tool-bundle/diagnose":
        raise ValueError("diagnosis skill tool_bundle_id must be tool-bundle/diagnose")
    requires_logparse = manifest["requires_logparse"]
    if type(requires_logparse) is not bool:
        raise ValueError("diagnosis skill requires_logparse must be a boolean")
    logparse_product = manifest.get("logparse_product")
    if requires_logparse:
        if not isinstance(logparse_product, str) or not logparse_product:
            raise ValueError("requires_logparse skill needs a non-empty logparse_product")
    elif logparse_product is not None:
        raise ValueError("non-logparse skill must use a null logparse_product")
    entry_document = manifest["entry_document"]
    if not isinstance(entry_document, str):
        raise ValueError("diagnosis skill entry_document must be a string")
    entry_document = _require_product_entry(
        root,
        entry_document,
        manifest_name="diagnosis-skill.json",
    )
    resolved = ResolvedAsset(
        ref=VersionedRef(
            id=f"diagnosis-skill/{skill_id}",
            version=version,
            content_hash=content_hash,
        ),
        asset_kind=AssetKind.DIAGNOSIS_SKILL,
        root_path=str(root.resolve()),
    )
    return _SkillDescriptor(
        resolved_asset=resolved,
        capability=capability,
        summary=summary,
        entry_document=entry_document,
        tool_bundle_id=tool_bundle_id,
        requires_logparse=requires_logparse,
        logparse_product=logparse_product,
    )


class VersionedAssetCatalog:
    """Startup-scanned implementation of the frozen ``AssetCatalogPort``."""

    def __init__(
        self,
        *,
        skill_dir: Path,
        assets_root: Path = BUILTIN_ASSET_ROOT,
        logparse_tool: ResolvedAsset | None = None,
        logparse_broker_factory: LogparseBrokerFactory | None = None,
    ) -> None:
        if (logparse_tool is None) != (logparse_broker_factory is None):
            raise ValueError(
                "logparse ResolvedAsset and LogparseBrokerFactory must be supplied together"
            )
        if logparse_broker_factory is not None and not isinstance(
            logparse_broker_factory,
            LogparseBrokerFactory,
        ):
            raise TypeError("logparse_broker_factory must implement LogparseBrokerFactory")

        self._assets: dict[tuple[str, str, str], ResolvedAsset] = {}
        self._id_versions: dict[tuple[str, str], str] = {}
        self._builtin_refs: dict[str, VersionedRef] = {}
        self._skills: dict[tuple[str, str, str], _SkillDescriptor] = {}
        self._logparse_tool_ref: VersionedRef | None = None
        # The frozen factory Port intentionally has no fingerprint read surface.
        # Keeping this exact instance alive enforces pair identity as far as S00
        # permits without inventing a private protocol.
        self._logparse_broker_factory = logparse_broker_factory

        assets_root = Path(assets_root)
        for expected in _BUILTIN_SPECS:
            resolved = _load_builtin(
                assets_root.joinpath(*PurePosixPath(expected.relative_root).parts),
                expected,
            )
            self._register(resolved)
            self._builtin_refs[expected.asset_id] = _clone_model(resolved.ref)

        for descriptor in self._scan_skills(Path(skill_dir)):
            self._register(descriptor.resolved_asset)
            key = _ref_key(descriptor.resolved_asset.ref)
            self._skills[key] = descriptor

        if logparse_tool is not None:
            if not isinstance(logparse_tool, ResolvedAsset):
                raise TypeError("logparse_tool must be a ResolvedAsset")
            if logparse_tool.asset_kind is not AssetKind.LOGPARSE_TOOL:
                raise ValueError("logparse_tool must have asset_kind=LOGPARSE_TOOL")
            external_root = Path(logparse_tool.root_path)
            try:
                external_stat = external_root.lstat()
            except OSError as exc:
                raise ValueError("logparse tool root_path is unavailable") from exc
            if stat.S_ISLNK(external_stat.st_mode) or not stat.S_ISDIR(external_stat.st_mode):
                raise ValueError("logparse tool root_path must be a real directory")
            normalized = ResolvedAsset(
                ref=_clone_model(logparse_tool.ref),
                asset_kind=logparse_tool.asset_kind,
                root_path=str(external_root.resolve()),
            )
            self._register(normalized)
            self._logparse_tool_ref = _clone_model(normalized.ref)

        if self._logparse_tool_ref is None and any(
            descriptor.requires_logparse for descriptor in self._skills.values()
        ):
            raise ValueError(
                "requires_logparse diagnosis skill needs a paired logparse asset and factory"
            )

        self._route_skill_refs = tuple(
            _clone_model(descriptor.resolved_asset.ref)
            for descriptor in sorted(
                self._skills.values(),
                key=lambda item: (
                    item.resolved_asset.ref.id,
                    item.resolved_asset.ref.version,
                    item.resolved_asset.ref.content_hash,
                ),
            )
        )

    def _register(self, resolved: ResolvedAsset) -> None:
        ref = resolved.ref
        id_version = (ref.id, ref.version)
        if id_version in self._id_versions:
            previous_hash = self._id_versions[id_version]
            raise ValueError(
                "duplicate asset id/version is forbidden: "
                f"{ref.id}@{ref.version} ({previous_hash} != {ref.content_hash})"
            )
        key = _ref_key(ref)
        if key in self._assets:
            raise ValueError(f"duplicate exact asset ref is forbidden: {ref.id}@{ref.version}")
        self._id_versions[id_version] = ref.content_hash
        self._assets[key] = _clone_model(resolved)

    @staticmethod
    def _scan_skills(skill_dir: Path) -> tuple[_SkillDescriptor, ...]:
        try:
            root_stat = skill_dir.lstat()
        except OSError as exc:
            raise ValueError("SKILL_DIR is unavailable") from exc
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise ValueError("SKILL_DIR must be a real directory")
        try:
            children = sorted(skill_dir.iterdir(), key=lambda path: path.name)
        except (OSError, UnicodeError) as exc:
            raise ValueError("SKILL_DIR cannot be scanned deterministically") from exc
        descriptors: list[_SkillDescriptor] = []
        for child in children:
            try:
                child.name.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("SKILL_DIR contains a non-UTF-8 path") from exc
            if child.is_symlink():
                raise ValueError(f"SKILL_DIR links are forbidden: {child.name}")
            if not child.is_dir():
                continue
            manifest_path = child / "diagnosis-skill.json"
            if manifest_path.is_symlink():
                raise ValueError(
                    f"diagnosis skill manifest links are forbidden: {child.name}"
                )
            if not manifest_path.exists():
                continue
            descriptors.append(_load_skill(child))
        return tuple(descriptors)

    def _builtin_ref(self, asset_id: str) -> VersionedRef:
        return _clone_model(self._builtin_refs[asset_id])

    @staticmethod
    def _asset_is_current(resolved: ResolvedAsset) -> bool:
        """Revalidate S04-owned product bytes without replacing their ref.

        The S07 logparse ref fingerprints repository, configuration, and
        interpreter facts that are intentionally wider than one product
        directory.  Its paired broker remains the authority for that asset.
        """

        if resolved.asset_kind is AssetKind.LOGPARSE_TOOL:
            return True
        try:
            return (
                hash_product_directory(Path(resolved.root_path))
                == resolved.ref.content_hash
            )
        except (OSError, TypeError, ValueError):
            return False

    def _require_current(self, refs: Sequence[VersionedRef]) -> None:
        if not self.check(refs).available:
            raise LookupError("one or more fixed asset versions are unavailable")

    def check(self, refs: Sequence[VersionedRef]) -> AssetAvailabilityReport:
        missing: list[VersionedRef] = []
        for ref in refs:
            resolved = self._assets.get(_ref_key(ref))
            if resolved is None or not self._asset_is_current(resolved):
                missing.append(_clone_model(ref))
        return AssetAvailabilityReport(available=not missing, missing_refs=missing)

    def resolve(self, ref: VersionedRef) -> ResolvedAsset:
        resolved = self._assets.get(_ref_key(ref))
        if resolved is None or not self._asset_is_current(resolved):
            raise LookupError(f"asset unavailable: {ref.id}@{ref.version}#{ref.content_hash}")
        return _clone_model(resolved)

    def route_bindings(self) -> RuntimeBindings:
        bindings = RuntimeBindings(
            agent_profile_ref=self._builtin_ref("agent-profile/router"),
            available_skill_refs=[_clone_model(ref) for ref in self._route_skill_refs],
            skill_ref=None,
            tool_bundle_ref=self._builtin_ref("tool-bundle/router"),
            context_policy_ref=self._builtin_ref("context-policy/route"),
            output_contract_ref=self._builtin_ref("output-contract/route"),
            logparse_tool_ref=None,
            logparse_product=None,
            resource_limits=default_resource_limits(JobType.ROUTE),
        )
        self._require_current(
            (
                bindings.agent_profile_ref,
                *bindings.available_skill_refs,
                bindings.tool_bundle_ref,
                bindings.context_policy_ref,
                bindings.output_contract_ref,
            )
        )
        return _clone_model(bindings)

    def diagnose_bindings(self, skill_ref: VersionedRef) -> RuntimeBindings:
        descriptor = self._skills.get(_ref_key(skill_ref))
        if descriptor is None:
            raise LookupError("diagnose bindings unavailable for the exact skill ref")
        if descriptor.requires_logparse and self._logparse_tool_ref is None:
            raise LookupError("required logparse binding is unavailable")
        bindings = RuntimeBindings(
            agent_profile_ref=self._builtin_ref("agent-profile/specialist"),
            available_skill_refs=[],
            skill_ref=_clone_model(descriptor.resolved_asset.ref),
            tool_bundle_ref=self._builtin_ref(descriptor.tool_bundle_id),
            context_policy_ref=self._builtin_ref("context-policy/diagnose"),
            output_contract_ref=self._builtin_ref("output-contract/diagnose"),
            logparse_tool_ref=(
                _clone_model(self._logparse_tool_ref)
                if descriptor.requires_logparse
                else None
            ),
            logparse_product=descriptor.logparse_product,
            resource_limits=default_resource_limits(JobType.DIAGNOSE),
        )
        current_refs = [
            bindings.agent_profile_ref,
            bindings.skill_ref,
            bindings.tool_bundle_ref,
            bindings.context_policy_ref,
            bindings.output_contract_ref,
        ]
        if bindings.logparse_tool_ref is not None:
            current_refs.append(bindings.logparse_tool_ref)
        self._require_current(
            [ref for ref in current_refs if ref is not None]
        )
        return _clone_model(bindings)

    def review_bindings(self, skill_ref: VersionedRef) -> RuntimeBindings:
        descriptor = self._skills.get(_ref_key(skill_ref))
        if descriptor is None:
            raise LookupError("review bindings unavailable for the exact skill ref")
        bindings = RuntimeBindings(
            agent_profile_ref=self._builtin_ref("agent-profile/reviewer"),
            available_skill_refs=[],
            skill_ref=_clone_model(descriptor.resolved_asset.ref),
            tool_bundle_ref=self._builtin_ref("tool-bundle/review"),
            context_policy_ref=self._builtin_ref("context-policy/review"),
            output_contract_ref=self._builtin_ref("output-contract/review"),
            logparse_tool_ref=None,
            logparse_product=None,
            resource_limits=default_resource_limits(JobType.REVIEW),
        )
        self._require_current(
            (
                bindings.agent_profile_ref,
                descriptor.resolved_asset.ref,
                bindings.tool_bundle_ref,
                bindings.context_policy_ref,
                bindings.output_contract_ref,
            )
        )
        return _clone_model(bindings)


AssetCatalog = VersionedAssetCatalog

__all__ = [
    "AssetCatalog",
    "BUILTIN_ASSET_ROOT",
    "VersionedAssetCatalog",
    "hash_product_directory",
]
