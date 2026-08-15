"""Load the one built-in diagnosis input profile used by generator and runtime."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


PROFILE_PATH = Path(__file__).with_name("assets") / "input-profile" / "profile.json"
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LOG_ARCHIVE_CONTENT_TYPES = [
    "application/gzip",
    "application/zip",
    "application/x-tar",
]


def _exact(value: Mapping[str, Any], fields: set[str], name: str) -> None:
    if set(value) != fields:
        raise ValueError(f"{name} fields are invalid")


def _input_constraints(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    _exact(
        value,
        {"value_type", "min_utf8_bytes", "max_utf8_bytes", "pattern", "allowed_values"},
        name,
    )
    minimum = value["min_utf8_bytes"]
    maximum = value["max_utf8_bytes"]
    pattern = value["pattern"]
    allowed = value["allowed_values"]
    if (
        value["value_type"] != "STRING"
        or type(minimum) is not int
        or type(maximum) is not int
        or not 1 <= minimum <= maximum <= 65_536
        or (pattern is not None and not isinstance(pattern, str))
        or not isinstance(allowed, list)
        or any(not isinstance(item, str) or not item for item in allowed)
        or len(allowed) != len(set(allowed))
    ):
        raise ValueError(f"{name} is invalid")
    if pattern is not None:
        re.compile(pattern)
    return value


def _base_requirement(value: Any, name: str, *, attachment: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    _exact(
        value,
        {
            "name",
            "kind",
            "stage",
            "fulfillment_source",
            "prompt",
            "constraints",
            "supplement_policy",
            "requiredness",
        },
        name,
    )
    if not isinstance(value["name"], str) or _NAME.fullmatch(value["name"]) is None:
        raise ValueError(f"{name}.name is invalid")
    if not isinstance(value["prompt"], str) or not value["prompt"].strip():
        raise ValueError(f"{name}.prompt is invalid")
    if value["stage"] != "INITIAL" or value["requiredness"] != "REQUIRED":
        raise ValueError(f"{name} must be an INITIAL REQUIRED requirement")
    if value["supplement_policy"] != "MISSING_ONLY":
        raise ValueError(f"{name} must use MISSING_ONLY")
    if attachment:
        if value["kind"] != "ATTACHMENT" or value["fulfillment_source"] != "READY_ATTACHMENT":
            raise ValueError(f"{name} must be an ATTACHMENT")
        constraints = value["constraints"]
        if not isinstance(constraints, dict):
            raise ValueError(f"{name}.constraints must be an object")
        _exact(constraints, {"allowed_content_types", "min_count", "max_count"}, f"{name}.constraints")
        if (
            constraints["allowed_content_types"] != _LOG_ARCHIVE_CONTENT_TYPES
            or constraints["min_count"] != 1
            or constraints["max_count"] != 1
        ):
            raise ValueError(f"{name}.constraints are invalid")
    else:
        if value["kind"] != "INPUT" or value["fulfillment_source"] != "USER_FACT":
            raise ValueError(f"{name} must be an INPUT")
        _input_constraints(value["constraints"], f"{name}.constraints")
    return value


def _validate_profile(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("input profile must be an object")
    _exact(
        value,
        {"schema_version", "profile_id", "global_requirements", "role_fields", "log_archive_requirement"},
        "input profile",
    )
    if value["schema_version"] != 1 or value["profile_id"] != "builtin-global-v1":
        raise ValueError("input profile identity is invalid")
    globals_value = value["global_requirements"]
    if not isinstance(globals_value, list) or len(globals_value) != 1:
        raise ValueError("input profile must define one global requirement")
    _base_requirement(globals_value[0], "global_requirements[0]")
    if globals_value[0]["name"] != "problem_time":
        raise ValueError("the global requirement must be problem_time")
    fields = value["role_fields"]
    if not isinstance(fields, list) or len(fields) != 3:
        raise ValueError("input profile must define slot, process_name and pid")
    seen: list[str] = []
    for index, field in enumerate(fields):
        if not isinstance(field, dict):
            raise ValueError("role field must be an object")
        _exact(
            field,
            {"suffix", "prompt_template", "constraints", "supplement_policy", "requiredness"},
            f"role_fields[{index}]",
        )
        suffix = field["suffix"]
        seen.append(suffix)
        if not isinstance(field["prompt_template"], str) or "{role}" not in field["prompt_template"]:
            raise ValueError("role field prompt_template is invalid")
        _input_constraints(field["constraints"], f"role_fields[{index}].constraints")
        expected = "OPTIONAL" if suffix == "pid" else "REQUIRED"
        policy = "NONE" if suffix == "pid" else "MISSING_ONLY"
        if field["requiredness"] != expected or field["supplement_policy"] != policy:
            raise ValueError("role field requiredness is invalid")
    if seen != ["slot", "process_name", "pid"]:
        raise ValueError("role field order is invalid")
    archive = _base_requirement(value["log_archive_requirement"], "log_archive_requirement", attachment=True)
    if archive["name"] != "log_archive":
        raise ValueError("log archive requirement name is invalid")
    return value


@lru_cache(maxsize=1)
def _cached_profile() -> dict[str, Any]:
    value = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    return _validate_profile(value)


def load_builtin_input_profile() -> dict[str, Any]:
    """Return a detached validated profile snapshot."""

    return deepcopy(_cached_profile())


def canonical_profile_bytes(profile: Mapping[str, Any] | None = None) -> bytes:
    value = load_builtin_input_profile() if profile is None else _validate_profile(dict(profile))
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def builtin_input_profile_sha256(profile: Mapping[str, Any] | None = None) -> str:
    return hashlib.sha256(canonical_profile_bytes(profile)).hexdigest()


def _manifest_requirement(
    template: Mapping[str, Any],
    *,
    origin: str,
    role: str | None,
    name: str | None = None,
    prompt: str | None = None,
) -> dict[str, Any]:
    result = deepcopy(dict(template))
    result["name"] = name or result["name"]
    result["prompt"] = prompt or result["prompt"]
    result.update(
        {
            "origin": origin,
            "role": role,
            "activation_condition": None,
            "source_reference": None,
        }
    )
    return result


def expand_profile_requirements(
    roles: Sequence[Mapping[str, Any]],
    *,
    requires_logparse: bool,
) -> list[dict[str, Any]]:
    """Expand global, role and platform templates into flat manifest requirements."""

    profile = load_builtin_input_profile()
    result = [
        _manifest_requirement(item, origin="PROFILE_GLOBAL", role=None)
        for item in profile["global_requirements"]
    ]
    for role in roles:
        label = role.get("label")
        if not isinstance(label, str) or _NAME.fullmatch(label) is None:
            raise ValueError("role label must be lower snake case")
        for field in profile["role_fields"]:
            suffix = field["suffix"]
            template = {
                "name": f"{label}_{suffix}",
                "kind": "INPUT",
                "stage": "INITIAL",
                "fulfillment_source": "USER_FACT",
                "prompt": field["prompt_template"].format(role=label),
                "constraints": deepcopy(field["constraints"]),
                "supplement_policy": field["supplement_policy"],
                "requiredness": field["requiredness"],
            }
            result.append(_manifest_requirement(template, origin="PROFILE_ROLE", role=label))
    if requires_logparse:
        result.append(
            _manifest_requirement(
                profile["log_archive_requirement"],
                origin="PLATFORM",
                role=None,
            )
        )
    return result


__all__ = [
    "PROFILE_PATH",
    "builtin_input_profile_sha256",
    "canonical_profile_bytes",
    "expand_profile_requirements",
    "load_builtin_input_profile",
]
