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
    ApplicationError,
    ApplicationPortError,
    AssetAvailabilityReport,
    AssetKind,
    ErrorCode,
    JobType,
    LogparseBrokerFactory,
    PORT_ERROR_CODES,
    ResolvedAsset,
    RuntimeBindings,
    VersionedRef,
    bytes_sha256,
    canonical_json_sha256,
    default_resource_limits,
)


BUILTIN_ASSET_ROOT = Path(__file__).with_name("assets")
_DEFAULT_ASSET_VERSION = "1.0.0"
_DEFAULT_LOGPARSE_PRODUCT = "default"
_LOG_ARCHIVE_CONTENT_TYPES = (
    "application/gzip",
    "application/zip",
    "application/x-tar",
)
_SKILL_ID_PATTERN = re.compile(r"[a-z][a-z0-9-]{1,63}\Z")
_WINDOWS_DRIVE_PATTERN = re.compile(r"[A-Za-z]:")


def _catalog_port_error(
    operation: str,
    code: ErrorCode,
    message: str,
) -> ApplicationPortError:
    """Build only an error allowed by the frozen method-qualified channel."""

    method_key = f"AssetCatalogPort.{operation}"
    if code not in PORT_ERROR_CODES[method_key]:
        raise AssertionError(f"{code.value} is not allowed for {method_key}")
    return ApplicationPortError(
        ApplicationError(
            code=code,
            message=message,
            details=[],
            retryable=False,
        )
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
    capability: str
    summary: str
    entry_document: str
    tool_bundle_id: str
    requires_logparse: bool
    logparse_product: str | None
    requirements: tuple[dict[str, Any], ...]
    logparse_plan: dict[str, Any] | None
    verification_contract: dict[str, Any]


_BUILTIN_SPECS = (
    _BuiltinSpec("profiles/router", AssetKind.AGENT_PROFILE, "agent-profile/router"),
    _BuiltinSpec(
        "profiles/specialist",
        AssetKind.AGENT_PROFILE,
        "agent-profile/specialist",
        "1.0.1",
    ),
    _BuiltinSpec(
        "profiles/reviewer",
        AssetKind.AGENT_PROFILE,
        "agent-profile/reviewer",
        "1.0.1",
    ),
    _BuiltinSpec(
        "tool-bundles/router",
        AssetKind.TOOL_BUNDLE,
        "tool-bundle/router",
        "2.0.0",
    ),
    _BuiltinSpec(
        "tool-bundles/diagnose",
        AssetKind.TOOL_BUNDLE,
        "tool-bundle/diagnose",
        "2.0.0",
    ),
    _BuiltinSpec(
        "tool-bundles/review",
        AssetKind.TOOL_BUNDLE,
        "tool-bundle/review",
        "2.0.0",
    ),
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
        "2.0.0",
    ),
    _BuiltinSpec(
        "output-contracts/diagnose",
        AssetKind.OUTPUT_CONTRACT,
        "output-contract/diagnose",
        "3.0.0",
    ),
    _BuiltinSpec(
        "output-contracts/review",
        AssetKind.OUTPUT_CONTRACT,
        "output-contract/review",
        "2.0.0",
    ),
)
_BUILTIN_SPECS_BY_ID = {item.asset_id: item for item in _BUILTIN_SPECS}


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
    seen_file_ids: set[tuple[int, int]] = set()

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
            # Some Windows providers expose st_ino == 0 for every file.  Use
            # the native volume/file-index pair there so aliases in the product
            # are still detected without treating every file as identical.
            file_id = (
                _windows_file_identity(Path(child.path))
                if os.name == "nt"
                else (child_stat.st_dev, child_stat.st_ino)
            )
            if file_id is not None:
                if file_id in seen_file_ids:
                    raise ValueError(
                        f"asset product hard links are forbidden: {relative_text}"
                    )
                seen_file_ids.add(file_id)
            # Codex workspaces on Windows are projected with one infrastructure
            # link, so an ordinary file reports st_nlink == 2.  A second link
            # created inside or outside the product still raises the count to 3;
            # duplicate identities inside the tree are rejected above on every OS.
            maximum_links = 2 if os.name == "nt" else 1
            if child_stat.st_nlink > maximum_links:
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


def _windows_file_identity(path: Path) -> tuple[int, int] | None:
    """Return Windows' stable volume/file-index identity for one open file."""

    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = create_file(str(path), 0, 0x7, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), f"cannot open asset product file: {path}")
    try:
        information = _ByHandleFileInformation()
        if not get_information(handle, ctypes.byref(information)):
            raise OSError(
                ctypes.get_last_error(),
                f"cannot identify asset product file: {path}",
            )
        file_index = (information.file_index_high << 32) | information.file_index_low
        return information.volume_serial_number, file_index
    finally:
        close_handle(handle)


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
    if manifest["version"] != expected.version:
        raise ValueError(f"built-in asset version must equal {expected.version}")
    if not isinstance(manifest["entry"], str):
        raise ValueError("asset.json entry must be a string")
    _require_product_entry(root, manifest["entry"], manifest_name="asset.json")
    return ResolvedAsset(
        ref=VersionedRef(
            id=expected.asset_id,
            version=expected.version,
            content_hash=content_hash,
        ),
        asset_kind=expected.asset_kind,
        root_path=str(root.resolve()),
    )


def _require_binding(value: Any, *, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    source = value.get("source")
    if source == "USER_FACT":
        _require_exact_fields(
            value,
            required={"source", "name"},
            manifest_name=field_name,
        )
        if not isinstance(value["name"], str) or not value["name"]:
            raise ValueError(f"{field_name}.name must be non-empty")
    elif source == "SKILL_FIXED":
        _require_exact_fields(
            value,
            required={"source", "value"},
            manifest_name=field_name,
        )
        if not isinstance(value["value"], str) or not value["value"]:
            raise ValueError(f"{field_name}.value must be non-empty")
    else:
        raise ValueError(f"{field_name}.source must be USER_FACT or SKILL_FIXED")
    return value


def _require_skill_requirements(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise ValueError("diagnosis skill requirements must be an array")
    requirements: list[dict[str, Any]] = []
    names: set[str] = set()
    attachment_by_stage: set[str] = set()
    for index, item in enumerate(value):
        field_name = f"requirements[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{field_name} must be an object")
        _require_exact_fields(
            item,
            required={
                "name",
                "kind",
                "stage",
                "fulfillment_source",
                "prompt",
                "constraints",
                "supplement_policy",
            },
            manifest_name=field_name,
        )
        name = item["name"]
        if not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None:
            raise ValueError(f"{field_name}.name is invalid")
        if name in names:
            raise ValueError("diagnosis skill requirement names must be unique")
        names.add(name)
        kind = item["kind"]
        stage = item["stage"]
        source = item["fulfillment_source"]
        if item["supplement_policy"] not in {"NONE", "MISSING_ONLY"}:
            raise ValueError(f"{field_name}.supplement_policy is invalid")
        if stage not in {"INITIAL", "AFTER_LOGPARSE"}:
            raise ValueError(f"{field_name}.stage is invalid")
        if not isinstance(item["prompt"], str) or not item["prompt"]:
            raise ValueError(f"{field_name}.prompt must be non-empty")
        constraints = item["constraints"]
        if not isinstance(constraints, dict):
            raise ValueError(f"{field_name}.constraints must be an object")
        if kind == "INPUT":
            if source != "USER_FACT":
                raise ValueError("INPUT requirements must use USER_FACT fulfillment")
            _require_exact_fields(
                constraints,
                required={
                    "value_type",
                    "min_utf8_bytes",
                    "max_utf8_bytes",
                    "pattern",
                    "allowed_values",
                },
                manifest_name=f"{field_name}.constraints",
            )
            if constraints["value_type"] != "STRING":
                raise ValueError("INPUT requirement value_type must be STRING")
            minimum = constraints["min_utf8_bytes"]
            maximum = constraints["max_utf8_bytes"]
            if (
                type(minimum) is not int
                or type(maximum) is not int
                or not 1 <= minimum <= maximum <= 65_536
            ):
                raise ValueError("INPUT requirement byte limits are invalid")
            pattern = constraints["pattern"]
            if pattern is not None:
                if not isinstance(pattern, str):
                    raise ValueError("INPUT requirement pattern must be a string or null")
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError("INPUT requirement pattern is invalid") from exc
            allowed = constraints["allowed_values"]
            if not isinstance(allowed, list) or any(
                not isinstance(entry, str) or not entry for entry in allowed
            ) or len(allowed) != len(set(allowed)):
                raise ValueError("INPUT requirement allowed_values are invalid")
        elif kind == "ATTACHMENT":
            if source != "READY_ATTACHMENT":
                raise ValueError(
                    "ATTACHMENT requirements must use READY_ATTACHMENT fulfillment"
                )
            if stage == "AFTER_LOGPARSE":
                raise ValueError("AFTER_LOGPARSE supports INPUT requirements only")
            if stage in attachment_by_stage:
                raise ValueError("only one ATTACHMENT requirement is allowed per stage")
            attachment_by_stage.add(stage)
            _require_exact_fields(
                constraints,
                required={"allowed_content_types", "min_count", "max_count"},
                manifest_name=f"{field_name}.constraints",
            )
            content_types = constraints["allowed_content_types"]
            if not isinstance(content_types, list) or any(
                not isinstance(entry, str) or not entry for entry in content_types
            ) or len(content_types) != len(set(content_types)):
                raise ValueError("ATTACHMENT allowed_content_types are invalid")
            minimum = constraints["min_count"]
            maximum = constraints["max_count"]
            if (
                type(minimum) is not int
                or type(maximum) is not int
                or not 1 <= minimum <= maximum
            ):
                raise ValueError("ATTACHMENT count limits are invalid")
        else:
            raise ValueError(f"{field_name}.kind is invalid")
        requirements.append(item)
    return tuple(requirements)


def _require_logparse_plan(
    value: Any,
    *,
    requirements: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("requires_logparse skill needs a logparse_plan object")
    _require_exact_fields(
        value,
        required={"attachment_requirement", "problem_time_binding", "anchors"},
        manifest_name="logparse_plan",
    )
    requirement_by_name = {item["name"]: item for item in requirements}
    attachment = value["attachment_requirement"]
    if attachment is not None:
        requirement = requirement_by_name.get(attachment)
        if requirement is None or requirement["kind"] != "ATTACHMENT":
            raise ValueError(
                "logparse_plan attachment_requirement must name an ATTACHMENT requirement"
            )
        if tuple(requirement["constraints"]["allowed_content_types"]) != _LOG_ARCHIVE_CONTENT_TYPES:
            raise ValueError("logparse archive ContentTypes are platform-fixed")
    _require_binding(value["problem_time_binding"], field_name="problem_time_binding")
    anchors = value["anchors"]
    if not isinstance(anchors, list) or not anchors:
        raise ValueError("logparse_plan anchors must be a non-empty array")
    labels: set[str] = set()
    for index, anchor in enumerate(anchors):
        field_name = f"logparse_plan.anchors[{index}]"
        if not isinstance(anchor, dict):
            raise ValueError(f"{field_name} must be an object")
        _require_exact_fields(
            anchor,
            required={"label", "module", "slot", "process_name", "pid"},
            manifest_name=field_name,
        )
        label = anchor["label"]
        if not isinstance(label, str) or not label or label in labels:
            raise ValueError("logparse anchor labels must be non-empty and unique")
        labels.add(label)
        for binding_name in ("module", "slot", "process_name"):
            _require_binding(
                anchor[binding_name],
                field_name=f"{field_name}.{binding_name}",
            )
        if anchor["pid"] is not None:
            _require_binding(anchor["pid"], field_name=f"{field_name}.pid")
    user_fact_names = {
        binding["name"]
        for binding in [
            value["problem_time_binding"],
            *[
                anchor[field]
                for anchor in anchors
                for field in ("module", "slot", "process_name", "pid")
                if anchor[field] is not None
            ],
        ]
        if binding["source"] == "USER_FACT"
    }
    if not user_fact_names <= {
        item["name"] for item in requirements if item["kind"] == "INPUT"
    }:
        raise ValueError("USER_FACT tool bindings must name INPUT requirements")
    return value


_VERIFICATION_RULE_KINDS = frozenset(
    {
        "EVENT_PRESENT",
        "EVENT_TIME_WINDOW",
        "FACT_FIELD_EQUALS",
        "ROLE_COVERAGE",
        "CROSS_ROLE_CORRELATION",
        "EVENT_ORDER",
        "SEMANTIC_CAUSALITY",
    }
)
_LOWER_SNAKE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def _require_name_list(
    value: Any,
    *,
    field_name: str,
    minimum: int = 0,
) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) < minimum
        or len(value) > 100
        or any(
            not isinstance(item, str)
            or _LOWER_SNAKE_PATTERN.fullmatch(item) is None
            for item in value
        )
        or len(value) != len(set(value))
    ):
        raise ValueError(f"{field_name} must contain unique lower-snake names")
    return value


def _require_event_field(
    value: Any,
    *,
    field_name: str,
) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    _require_exact_fields(
        value,
        required={"event", "field"},
        manifest_name=field_name,
    )
    event = value["event"]
    field = value["field"]
    if (
        not isinstance(event, str)
        or _LOWER_SNAKE_PATTERN.fullmatch(event) is None
        or not isinstance(field, str)
        or _LOWER_SNAKE_PATTERN.fullmatch(field) is None
    ):
        raise ValueError(f"{field_name} names are invalid")
    return event, field


def _require_verification_contract(
    value: Any,
    *,
    requirements: tuple[dict[str, Any], ...],
    logparse_plan: dict[str, Any] | None,
    requires_logparse: bool,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("verification_contract must be an object")
    _require_exact_fields(
        value,
        required={"schema_version", "event_extractors", "rules"},
        manifest_name="verification_contract",
    )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("verification_contract schema_version must equal integer 1")
    anchor_labels = (
        set()
        if logparse_plan is None
        else {item["label"] for item in logparse_plan["anchors"]}
    )
    extractors = value["event_extractors"]
    if not isinstance(extractors, list) or len(extractors) > 100:
        raise ValueError("verification_contract event_extractors must be an array")
    if requires_logparse != bool(extractors):
        raise ValueError("logparse Skills require extractors; non-logparse Skills forbid them")
    extractor_by_id: dict[str, dict[str, Any]] = {}
    for index, extractor in enumerate(extractors):
        field_name = f"verification_contract.event_extractors[{index}]"
        if not isinstance(extractor, dict):
            raise ValueError(f"{field_name} must be an object")
        _require_exact_fields(
            extractor,
            required={
                "id",
                "anchor",
                "line_pattern",
                "timestamp_group",
                "timestamp_format",
                "field_groups",
                "match_cardinality",
            },
            manifest_name=field_name,
        )
        extractor_id = extractor["id"]
        anchor = extractor["anchor"]
        pattern = extractor["line_pattern"]
        timestamp_group = extractor["timestamp_group"]
        if (
            not isinstance(extractor_id, str)
            or _LOWER_SNAKE_PATTERN.fullmatch(extractor_id) is None
            or extractor_id in extractor_by_id
        ):
            raise ValueError("event extractor ids must be unique lower-snake names")
        if not isinstance(anchor, str) or anchor not in anchor_labels:
            raise ValueError("event extractor anchor must name a logparse anchor")
        if (
            not isinstance(pattern, str)
            or not pattern
            or len(pattern.encode("utf-8")) > 8192
            or "\n" in pattern
            or "\r" in pattern
            or not pattern.startswith("^")
            or not pattern.endswith("$")
        ):
            raise ValueError("event extractor line_pattern must be UTF-8 single-line anchored")
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ValueError("event extractor line_pattern is invalid") from exc
        fields = _require_name_list(
            extractor["field_groups"],
            field_name=f"{field_name}.field_groups",
        )
        if (
            not isinstance(timestamp_group, str)
            or _LOWER_SNAKE_PATTERN.fullmatch(timestamp_group) is None
            or timestamp_group in fields
            or set(compiled.groupindex) != {timestamp_group, *fields}
        ):
            raise ValueError("event extractor named capture groups are invalid")
        if extractor["timestamp_format"] != "RFC3339_MILLIS_UTC":
            raise ValueError("event extractor timestamp_format is invalid")
        if extractor["match_cardinality"] != "EXACTLY_ONE":
            raise ValueError("event extractor match_cardinality is invalid")
        extractor_by_id[extractor_id] = extractor

    rules = value["rules"]
    if not isinstance(rules, list) or not rules or len(rules) > 200:
        raise ValueError("verification_contract rules must be a non-empty array")
    requirement_by_name = {item["name"]: item for item in requirements}
    input_names = {
        item["name"] for item in requirements if item["kind"] == "INPUT"
    }
    seen_rule_ids: set[str] = set()
    has_semantic_rule = False
    for index, rule in enumerate(rules):
        field_name = f"verification_contract.rules[{index}]"
        if not isinstance(rule, dict):
            raise ValueError(f"{field_name} must be an object")
        _require_exact_fields(
            rule,
            required={
                "id",
                "kind",
                "description",
                "depends_on",
                "remediation_requirements",
                "parameters",
            },
            manifest_name=field_name,
        )
        rule_id = rule["id"]
        kind = rule["kind"]
        if (
            not isinstance(rule_id, str)
            or _LOWER_SNAKE_PATTERN.fullmatch(rule_id) is None
            or rule_id in seen_rule_ids
            or kind not in _VERIFICATION_RULE_KINDS
            or not isinstance(rule["description"], str)
            or not rule["description"]
            or "\n" in rule["description"]
            or "\r" in rule["description"]
        ):
            raise ValueError(f"{field_name} identity is invalid")
        dependencies = _require_name_list(
            rule["depends_on"], field_name=f"{field_name}.depends_on"
        )
        if not set(dependencies) <= seen_rule_ids:
            raise ValueError("verification rule dependencies must name preceding rules")
        remediation = _require_name_list(
            rule["remediation_requirements"],
            field_name=f"{field_name}.remediation_requirements",
        )
        if any(
            name not in requirement_by_name
            or requirement_by_name[name]["supplement_policy"] != "MISSING_ONLY"
            for name in remediation
        ):
            raise ValueError("rule remediation must name MISSING_ONLY requirements")
        parameters = rule["parameters"]
        if not isinstance(parameters, dict):
            raise ValueError(f"{field_name}.parameters must be an object")
        event_fields: list[tuple[str, str | None]] = []
        if kind == "EVENT_PRESENT":
            _require_exact_fields(parameters, required={"event"}, manifest_name=field_name)
            event_fields.append((parameters["event"], None))
        elif kind == "EVENT_TIME_WINDOW":
            _require_exact_fields(
                parameters,
                required={
                    "event",
                    "reference",
                    "before_ms",
                    "after_ms",
                    "lower_bound",
                    "upper_bound",
                },
                manifest_name=field_name,
            )
            before_ms = parameters["before_ms"]
            after_ms = parameters["after_ms"]
            if (
                type(before_ms) is not int
                or type(after_ms) is not int
                or not 0 <= before_ms <= 86_400_000
                or not 0 <= after_ms <= 86_400_000
                or parameters["lower_bound"] not in {"INCLUSIVE", "EXCLUSIVE"}
                or parameters["upper_bound"] not in {"INCLUSIVE", "EXCLUSIVE"}
            ):
                raise ValueError("event time window must declare valid bounds")
            reference = _require_binding(
                parameters["reference"], field_name=f"{field_name}.reference"
            )
            if reference["source"] == "USER_FACT" and reference["name"] not in input_names:
                raise ValueError("event time reference must name an INPUT requirement")
            event_fields.append((parameters["event"], None))
        elif kind == "FACT_FIELD_EQUALS":
            _require_exact_fields(
                parameters,
                required={"event", "field", "fact_name"},
                manifest_name=field_name,
            )
            event_fields.append(
                _require_event_field(
                    {"event": parameters["event"], "field": parameters["field"]},
                    field_name=f"{field_name}.parameters",
                )
            )
            if parameters["fact_name"] not in input_names:
                raise ValueError("FACT_FIELD_EQUALS fact_name must name INPUT")
        elif kind == "ROLE_COVERAGE":
            _require_exact_fields(parameters, required={"coverage"}, manifest_name=field_name)
            coverage = parameters["coverage"]
            if not isinstance(coverage, list) or not coverage or len(coverage) > 20:
                raise ValueError("ROLE_COVERAGE coverage is invalid")
            roles: set[str] = set()
            for item in coverage:
                if not isinstance(item, dict):
                    raise ValueError("ROLE_COVERAGE item must be an object")
                _require_exact_fields(
                    item, required={"role", "event"}, manifest_name=field_name
                )
                if not isinstance(item["role"], str) or item["role"] in roles:
                    raise ValueError("ROLE_COVERAGE roles must be unique")
                roles.add(item["role"])
                event_fields.append((item["event"], None))
        elif kind == "CROSS_ROLE_CORRELATION":
            _require_exact_fields(parameters, required={"members"}, manifest_name=field_name)
            members = parameters["members"]
            if not isinstance(members, list) or not 2 <= len(members) <= 20:
                raise ValueError("CROSS_ROLE_CORRELATION members are invalid")
            event_fields.extend(
                _require_event_field(item, field_name=f"{field_name}.member")
                for item in members
            )
            if len(event_fields) != len(set(event_fields)):
                raise ValueError("CROSS_ROLE_CORRELATION members must be unique")
        elif kind == "EVENT_ORDER":
            _require_exact_fields(
                parameters,
                required={"before_event", "after_event", "allow_equal"},
                manifest_name=field_name,
            )
            if type(parameters["allow_equal"]) is not bool:
                raise ValueError("EVENT_ORDER allow_equal must be boolean")
            event_fields.extend(
                ((parameters["before_event"], None), (parameters["after_event"], None))
            )
        else:
            has_semantic_rule = True
            _require_exact_fields(
                parameters,
                required={"assertion", "evidence_events"},
                manifest_name=field_name,
            )
            if not isinstance(parameters["assertion"], str) or not parameters["assertion"]:
                raise ValueError("SEMANTIC_CAUSALITY assertion must be non-empty")
            event_fields.extend(
                (event_id, None)
                for event_id in _require_name_list(
                    parameters["evidence_events"],
                    field_name=f"{field_name}.evidence_events",
                )
            )
        anchors: set[str] = set()
        for event_id, event_field in event_fields:
            extractor = extractor_by_id.get(event_id)
            if extractor is None:
                raise ValueError("verification rule names an unknown event")
            anchors.add(extractor["anchor"])
            if event_field is not None and event_field not in extractor["field_groups"]:
                raise ValueError("verification rule names an unknown event field")
        if kind == "ROLE_COVERAGE" and any(
            extractor_by_id[item["event"]]["anchor"] != item["role"]
            for item in parameters["coverage"]
        ):
            raise ValueError("ROLE_COVERAGE role must equal event anchor")
        if kind == "CROSS_ROLE_CORRELATION" and len(anchors) < 2:
            raise ValueError("CROSS_ROLE_CORRELATION must span multiple anchors")
        seen_rule_ids.add(rule_id)
    if not has_semantic_rule:
        raise ValueError("verification_contract requires SEMANTIC_CAUSALITY")
    return value


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
        "requirements",
        "logparse_plan",
        "verification_contract",
    }
    _require_exact_fields(
        manifest,
        required=required,
        optional={"logparse_product"},
        manifest_name="diagnosis-skill.json",
    )
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 3:
        raise ValueError("diagnosis-skill.json schema_version must equal integer 3")
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
    requirements = _require_skill_requirements(manifest["requirements"])
    logparse_plan = manifest["logparse_plan"]
    if requires_logparse:
        if logparse_product is not None and (
            not isinstance(logparse_product, str)
            or not logparse_product
            or logparse_product == _DEFAULT_LOGPARSE_PRODUCT
        ):
            raise ValueError(
                "logparse_product must be omitted for default or a non-default string"
            )
        logparse_plan = _require_logparse_plan(
            logparse_plan,
            requirements=requirements,
        )
        logparse_product = logparse_product or _DEFAULT_LOGPARSE_PRODUCT
    else:
        if logparse_product is not None:
            raise ValueError("non-logparse skill must omit logparse_product")
        if logparse_plan is not None:
            raise ValueError("non-logparse skill must use logparse_plan=null")
        if any(item["stage"] == "AFTER_LOGPARSE" for item in requirements):
            raise ValueError("non-logparse skill forbids AFTER_LOGPARSE requirements")
    verification_contract = _require_verification_contract(
        manifest["verification_contract"],
        requirements=requirements,
        logparse_plan=logparse_plan,
        requires_logparse=requires_logparse,
    )
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
        requirements=requirements,
        logparse_plan=logparse_plan,
        verification_contract=verification_contract,
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
        expected = _BUILTIN_SPECS_BY_ID[asset_id]
        ref = self._builtin_refs[asset_id]
        resolved = self._assets[_ref_key(ref)]
        if (
            ref.id != asset_id
            or not isinstance(resolved, ResolvedAsset)
            or resolved.ref != ref
            or resolved.asset_kind is not expected.asset_kind
        ):
            raise ValueError("built-in role binding is invalid")
        return _clone_model(ref)

    @staticmethod
    def _asset_is_current(resolved: ResolvedAsset) -> bool:
        """Revalidate S04-owned product bytes without replacing their ref.

        The S07 logparse ref fingerprints repository, configuration, and
        interpreter facts that are intentionally wider than one product
        directory.  Its paired broker remains the authority for that asset.
        """

        try:
            root = Path(resolved.root_path)
            root_metadata = root.lstat()
            if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(
                root_metadata.st_mode
            ):
                return False
            if resolved.asset_kind is AssetKind.LOGPARSE_TOOL:
                return True
            return (
                hash_product_directory(root) == resolved.ref.content_hash
            )
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    def _ref_is_current(self, ref: VersionedRef) -> bool:
        try:
            resolved = self._assets.get(_ref_key(ref))
            return (
                isinstance(resolved, ResolvedAsset)
                and resolved.ref == ref
                and self._asset_is_current(resolved)
            )
        except (AttributeError, TypeError, ValueError):
            return False

    @staticmethod
    def _descriptor_is_configured(descriptor: object) -> bool:
        if not isinstance(descriptor, _SkillDescriptor):
            return False
        resolved = descriptor.resolved_asset
        if (
            not isinstance(resolved, ResolvedAsset)
            or resolved.asset_kind is not AssetKind.DIAGNOSIS_SKILL
            or descriptor.tool_bundle_id != "tool-bundle/diagnose"
            or not isinstance(descriptor.capability, str)
            or not descriptor.capability
            or not isinstance(descriptor.summary, str)
            or not descriptor.summary
            or not isinstance(descriptor.entry_document, str)
            or not descriptor.entry_document
            or type(descriptor.requires_logparse) is not bool
        ):
            return False
        if descriptor.requires_logparse:
            return (
                isinstance(descriptor.logparse_product, str)
                and bool(descriptor.logparse_product)
            )
        return descriptor.logparse_product is None

    def _route_skill_configuration_is_valid(self) -> bool:
        try:
            descriptors = tuple(self._skills.values())
            if not all(self._descriptor_is_configured(item) for item in descriptors):
                return False
            expected = tuple(
                item.resolved_asset.ref
                for item in sorted(
                    descriptors,
                    key=lambda item: (
                        item.resolved_asset.ref.id,
                        item.resolved_asset.ref.version,
                        item.resolved_asset.ref.content_hash,
                    ),
                )
            )
            return expected == self._route_skill_refs and len(self._skills) == len(
                self._route_skill_refs
            )
        except (AttributeError, TypeError, ValueError):
            return False

    def check(self, refs: Sequence[VersionedRef]) -> AssetAvailabilityReport:
        missing = [_clone_model(ref) for ref in refs if not self._ref_is_current(ref)]
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
        return _clone_model(resolved)

    def route_bindings(self) -> RuntimeBindings:
        try:
            if not self._route_skill_configuration_is_valid():
                raise ValueError("route skill configuration is invalid")
            bindings = RuntimeBindings(
                agent_profile_ref=self._builtin_ref("agent-profile/router"),
                available_skill_refs=[
                    _clone_model(ref) for ref in self._route_skill_refs
                ],
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
                raise ValueError("route binding asset is unavailable")
            for descriptor in self._skills.values():
                if descriptor.requires_logparse:
                    if (
                        self._logparse_tool_ref is None
                        or not self._ref_is_current(self._logparse_tool_ref)
                    ):
                        raise ValueError("route logparse asset is unavailable")
                    logparse_asset = self._assets[
                        _ref_key(self._logparse_tool_ref)
                    ]
                    if (
                        not isinstance(logparse_asset, ResolvedAsset)
                        or logparse_asset.asset_kind is not AssetKind.LOGPARSE_TOOL
                        or self._logparse_broker_factory is None
                    ):
                        raise ValueError("route logparse configuration is invalid")
        except ApplicationPortError:
            raise
        except Exception:
            raise _catalog_port_error(
                "route_bindings",
                ErrorCode.CONFIG_INVALID,
                "The route runtime binding configuration is invalid.",
            ) from None
        return _clone_model(bindings)

    def diagnose_bindings(self, skill_ref: VersionedRef) -> RuntimeBindings:
        try:
            skill_key = _ref_key(skill_ref)
        except (AttributeError, TypeError, ValueError):
            raise _catalog_port_error(
                "diagnose_bindings",
                ErrorCode.ASSET_VERSION_UNAVAILABLE,
                "The pinned diagnosis runtime bindings are unavailable.",
            ) from None
        try:
            descriptor = self._skills.get(skill_key)
        except Exception:
            raise _catalog_port_error(
                "diagnose_bindings",
                ErrorCode.CONFIG_INVALID,
                "The diagnosis runtime binding configuration is invalid.",
            ) from None
        if descriptor is None:
            if self._ref_is_current(skill_ref):
                raise _catalog_port_error(
                    "diagnose_bindings",
                    ErrorCode.CONFIG_INVALID,
                    "The diagnosis runtime binding configuration is invalid.",
                ) from None
            raise _catalog_port_error(
                "diagnose_bindings",
                ErrorCode.ASSET_VERSION_UNAVAILABLE,
                "The pinned diagnosis runtime bindings are unavailable.",
            ) from None
        if (
            not self._descriptor_is_configured(descriptor)
            or descriptor.resolved_asset.ref != skill_ref
        ):
            raise _catalog_port_error(
                "diagnose_bindings",
                ErrorCode.CONFIG_INVALID,
                "The diagnosis runtime binding configuration is invalid.",
            ) from None
        if not self._ref_is_current(skill_ref):
            raise _catalog_port_error(
                "diagnose_bindings",
                ErrorCode.ASSET_VERSION_UNAVAILABLE,
                "The pinned diagnosis runtime bindings are unavailable.",
            ) from None
        if descriptor.requires_logparse and (
            self._logparse_tool_ref is None
            or not self._ref_is_current(self._logparse_tool_ref)
        ):
            raise _catalog_port_error(
                "diagnose_bindings",
                ErrorCode.ASSET_VERSION_UNAVAILABLE,
                "The pinned diagnosis runtime bindings are unavailable.",
            ) from None
        if descriptor.requires_logparse:
            try:
                assert self._logparse_tool_ref is not None
                logparse_asset = self._assets[_ref_key(self._logparse_tool_ref)]
                if (
                    not isinstance(logparse_asset, ResolvedAsset)
                    or logparse_asset.asset_kind is not AssetKind.LOGPARSE_TOOL
                    or self._logparse_broker_factory is None
                ):
                    raise ValueError("logparse role binding is invalid")
            except Exception:
                raise _catalog_port_error(
                    "diagnose_bindings",
                    ErrorCode.CONFIG_INVALID,
                    "The diagnosis runtime binding configuration is invalid.",
                ) from None
        try:
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
            builtin_refs = (
                bindings.agent_profile_ref,
                bindings.tool_bundle_ref,
                bindings.context_policy_ref,
                bindings.output_contract_ref,
            )
            if not all(self._ref_is_current(ref) for ref in builtin_refs):
                raise ValueError("diagnosis built-in configuration is unavailable")
        except ApplicationPortError:
            raise
        except Exception:
            raise _catalog_port_error(
                "diagnose_bindings",
                ErrorCode.CONFIG_INVALID,
                "The diagnosis runtime binding configuration is invalid.",
            ) from None
        return _clone_model(bindings)

    def review_bindings(self, skill_ref: VersionedRef) -> RuntimeBindings:
        try:
            skill_key = _ref_key(skill_ref)
        except (AttributeError, TypeError, ValueError):
            raise _catalog_port_error(
                "review_bindings",
                ErrorCode.ASSET_VERSION_UNAVAILABLE,
                "The pinned review runtime bindings are unavailable.",
            ) from None
        try:
            descriptor = self._skills.get(skill_key)
        except Exception:
            raise _catalog_port_error(
                "review_bindings",
                ErrorCode.CONFIG_INVALID,
                "The review runtime binding configuration is invalid.",
            ) from None
        if descriptor is None:
            if self._ref_is_current(skill_ref):
                raise _catalog_port_error(
                    "review_bindings",
                    ErrorCode.CONFIG_INVALID,
                    "The review runtime binding configuration is invalid.",
                ) from None
            raise _catalog_port_error(
                "review_bindings",
                ErrorCode.ASSET_VERSION_UNAVAILABLE,
                "The pinned review runtime bindings are unavailable.",
            ) from None
        if (
            not self._descriptor_is_configured(descriptor)
            or descriptor.resolved_asset.ref != skill_ref
        ):
            raise _catalog_port_error(
                "review_bindings",
                ErrorCode.CONFIG_INVALID,
                "The review runtime binding configuration is invalid.",
            ) from None
        if not self._ref_is_current(skill_ref):
            raise _catalog_port_error(
                "review_bindings",
                ErrorCode.ASSET_VERSION_UNAVAILABLE,
                "The pinned review runtime bindings are unavailable.",
            ) from None
        try:
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
            builtin_refs = (
                bindings.agent_profile_ref,
                bindings.tool_bundle_ref,
                bindings.context_policy_ref,
                bindings.output_contract_ref,
            )
            if not all(self._ref_is_current(ref) for ref in builtin_refs):
                raise ValueError("review built-in configuration is unavailable")
        except ApplicationPortError:
            raise
        except Exception:
            raise _catalog_port_error(
                "review_bindings",
                ErrorCode.CONFIG_INVALID,
                "The review runtime binding configuration is invalid.",
            ) from None
        return _clone_model(bindings)


AssetCatalog = VersionedAssetCatalog

__all__ = [
    "AssetCatalog",
    "BUILTIN_ASSET_ROOT",
    "VersionedAssetCatalog",
    "hash_product_directory",
]
