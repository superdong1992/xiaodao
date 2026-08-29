"""Strict Methods Skill package and product-registration contracts.

Generated packages intentionally contain only author-derived diagnosis material.
Product routing and runtime bindings live in a sibling registration template so
neither side can silently manufacture fields owned by the other.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from problem_locator.contracts import canonical_json_sha256

from .catalog_hash import hash_product_directory


_KEBAB = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SNAKE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SEMVER = re.compile(r"0|[1-9][0-9]*(?:\.(?:0|[1-9][0-9]*)){2}(?:[-+][0-9A-Za-z.-]+)?\Z")
_METHOD_HEADINGS = (
    "## 适用条件",
    "## 所需证据",
    "## 计算与判断",
    "## 确认条件",
    "## 未知边界",
    "## 输出含义",
)
_PACKAGE_ROOT_ENTRIES = frozenset({"SKILL.md", "methods.json", "references"})
_METHODS_FIELDS = frozenset(
    {
        "schema_version",
        "skill_name",
        "source_wiki_sha256",
        "required_user_inputs",
        "required_artifacts",
        "log_derived_fields",
        "shared_references",
        "methods",
    }
)
_METHOD_FIELDS = frozenset(
    {"id", "title", "reference", "priority", "evidence_markers"}
)
_REGISTRATION_FIELDS = frozenset(
    {
        "schema_version",
        "registration_id",
        "version",
        "capability",
        "deployment_scope",
        "summary",
        "package",
        "runtime",
    }
)
_PACKAGE_BINDING_FIELDS = frozenset(
    {"relative_path", "skill_name", "source_wiki_sha256"}
)
_RUNTIME_FIELDS = frozenset({"diagnose", "review", "preprocessing"})
_ROLE_BINDING_FIELDS = frozenset(
    {"agent_profile_id", "tool_bundle_id", "context_policy_id", "output_contract_id"}
)
_PREPROCESSING_FIELDS = frozenset(
    {"requires_logparse", "logparse_product", "roles", "logparse_plan"}
)
_ROLE_FIELDS = frozenset({"label", "description", "presence", "source_reference"})
_PLAN_FIELDS = frozenset({"attachment_requirement", "problem_time_binding", "anchors"})
_ANCHOR_FIELDS = frozenset({"label", "module", "slot", "process_name", "pid"})
_DIAGNOSE_BINDING = {
    "agent_profile_id": "agent-profile/specialist",
    "tool_bundle_id": "tool-bundle/diagnose",
    "context_policy_id": "context-policy/diagnose",
    "output_contract_id": "output-contract/diagnose",
}
_REVIEW_BINDING = {
    "agent_profile_id": "agent-profile/reviewer",
    "tool_bundle_id": "tool-bundle/review",
    "context_policy_id": "context-policy/review",
    "output_contract_id": "output-contract/review",
}


def _ordinary_file(path: Path, *, label: str) -> bytes:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    maximum_links = 2 if os.name == "nt" else 1
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > maximum_links:
        raise ValueError(f"{label} must be one ordinary file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"{label} cannot be read") from exc


def _real_directory(path: Path, *, label: str) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{label} must be one real directory")


def _json_object(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    raw = _ordinary_file(path, label=label)

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"{label} contains duplicate field {key}")
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise ValueError(f"{label} contains non-finite value {value}")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain one JSON object")
    return value, raw


def _exact_fields(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    if set(value) != expected:
        raise ValueError(
            f"{label} fields are invalid; missing={sorted(expected - set(value))!r}, "
            f"extra={sorted(set(value) - expected)!r}"
        )


def _identifier_list(value: Any, *, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > 200
        or any(not isinstance(item, str) or _SNAKE.fullmatch(item) is None for item in value)
        or len(value) != len(set(value))
    ):
        raise ValueError(f"{label} must contain unique lower snake-case identifiers")
    return tuple(value)


def _safe_reference(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} must be a safe references/*.md path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] != "references"
        or relative.suffix != ".md"
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"{label} must be a safe references/*.md path")
    return relative.as_posix()


def _frontmatter_name(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("SKILL.md frontmatter is not closed") from exc
    fields: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError("SKILL.md frontmatter contains an unsupported line")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if value and value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        if key in fields:
            raise ValueError(f"SKILL.md frontmatter contains duplicate field {key}")
        fields[key] = value
    if set(fields) != {"name", "description"} or not fields.get("description"):
        raise ValueError("SKILL.md frontmatter must contain only non-empty name and description")
    return fields["name"]


@dataclass(frozen=True, slots=True)
class MethodCardV1:
    id: str
    title: str
    reference: str
    priority: int
    evidence_markers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MethodsManifestV1:
    skill_name: str
    source_wiki_sha256: str
    required_user_inputs: tuple[str, ...]
    required_artifacts: tuple[str, ...]
    log_derived_fields: tuple[str, ...]
    shared_references: tuple[str, ...]
    methods: tuple[MethodCardV1, ...]

    @property
    def method_by_id(self) -> dict[str, MethodCardV1]:
        return {item.id: item for item in self.methods}


@dataclass(frozen=True, slots=True)
class RuntimeRoleBindingV1:
    agent_profile_id: str
    tool_bundle_id: str
    context_policy_id: str
    output_contract_id: str


@dataclass(frozen=True, slots=True)
class PreprocessingBindingV1:
    requires_logparse: bool
    logparse_product: str | None
    roles: tuple[dict[str, Any], ...]
    logparse_plan: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class RegistrationTemplateV1:
    registration_id: str
    version: str
    capability: str
    deployment_scope: str
    summary: str
    package_relative_path: str
    skill_name: str
    source_wiki_sha256: str
    diagnose: RuntimeRoleBindingV1
    review: RuntimeRoleBindingV1
    preprocessing: PreprocessingBindingV1


@dataclass(frozen=True, slots=True)
class ResolvedSpecializedSkillV1:
    registration_root: Path
    package_root: Path
    registration: RegistrationTemplateV1
    methods: MethodsManifestV1
    registration_sha256: str
    package_tree_sha256: str
    combined_sha256: str

    @property
    def registration_id(self) -> str:
        return self.registration.registration_id


def load_methods_package(
    package_root: Path,
    *,
    expected_skill_name: str | None = None,
    expected_source_wiki_sha256: str | None = None,
) -> MethodsManifestV1:
    """Validate one closed generated package and return its immutable index."""

    root = Path(package_root)
    _real_directory(root, label="Methods Skill package")
    try:
        entries = {item.name for item in root.iterdir()}
    except OSError as exc:
        raise ValueError("Methods Skill package cannot be scanned") from exc
    if entries != _PACKAGE_ROOT_ENTRIES:
        raise ValueError(
            "Methods Skill package root must contain exactly SKILL.md, methods.json, references"
        )
    references_root = root / "references"
    _real_directory(references_root, label="Methods Skill references")
    try:
        reference_nodes = tuple(sorted(references_root.iterdir(), key=lambda item: item.name))
    except (OSError, UnicodeError) as exc:
        raise ValueError("Methods Skill references cannot be scanned") from exc
    for node in reference_nodes:
        if node.name == "diagnosis-skill.json":
            raise ValueError("legacy diagnosis-skill.json is forbidden")
        _ordinary_file(node, label=f"Methods Skill reference {node.name}")
        if node.suffix != ".md":
            raise ValueError("Methods Skill references must all be Markdown files")

    try:
        skill_text = _ordinary_file(root / "SKILL.md", label="SKILL.md").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SKILL.md must be UTF-8") from exc
    skill_name = _frontmatter_name(skill_text)
    for phrase in (
        "request.json",
        "method-evidence-graph.json",
        "method-evaluation-plan.json",
        "evaluation_ref",
        "verdict",
        "reason",
        "UNKNOWN",
    ):
        if phrase not in skill_text:
            raise ValueError(f"SKILL.md must mention {phrase}")

    manifest, _ = _json_object(root / "methods.json", label="methods.json")
    _exact_fields(manifest, _METHODS_FIELDS, "methods.json")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("methods.json schema_version must equal integer 1")
    indexed_name = manifest["skill_name"]
    if not isinstance(indexed_name, str) or _KEBAB.fullmatch(indexed_name) is None:
        raise ValueError("methods.json skill_name is invalid")
    if indexed_name != root.name or indexed_name != skill_name:
        raise ValueError("Methods Skill name must match directory, frontmatter, and methods.json")
    if expected_skill_name is not None and indexed_name != expected_skill_name:
        raise ValueError("Methods Skill name differs from registration")
    source_wiki_sha256 = manifest["source_wiki_sha256"]
    if not isinstance(source_wiki_sha256, str) or _SHA256.fullmatch(source_wiki_sha256) is None:
        raise ValueError("methods.json source_wiki_sha256 is invalid")
    if (
        expected_source_wiki_sha256 is not None
        and source_wiki_sha256 != expected_source_wiki_sha256
    ):
        raise ValueError("Methods Skill Wiki digest differs from registration")

    required_user_inputs = _identifier_list(
        manifest["required_user_inputs"], label="required_user_inputs"
    )
    required_artifacts = _identifier_list(
        manifest["required_artifacts"], label="required_artifacts"
    )
    log_derived_fields = _identifier_list(
        manifest["log_derived_fields"], label="log_derived_fields"
    )
    combined_names = (*required_user_inputs, *required_artifacts, *log_derived_fields)
    if len(combined_names) != len(set(combined_names)):
        raise ValueError("Methods Skill input and derived identifiers must be disjoint")

    shared_raw = manifest["shared_references"]
    if not isinstance(shared_raw, list):
        raise ValueError("shared_references must be an array")
    shared = tuple(
        _safe_reference(item, label="shared reference") for item in shared_raw
    )
    if len(shared) != len(set(shared)):
        raise ValueError("shared_references must be unique")

    raw_methods = manifest["methods"]
    if not isinstance(raw_methods, list) or not raw_methods or len(raw_methods) > 100:
        raise ValueError("methods must be a non-empty bounded array")
    methods: list[MethodCardV1] = []
    method_ids: set[str] = set()
    method_references: set[str] = set()
    priorities: list[int] = []
    for index, raw_method in enumerate(raw_methods, start=1):
        if not isinstance(raw_method, dict):
            raise ValueError(f"methods[{index}] must be an object")
        _exact_fields(raw_method, _METHOD_FIELDS, f"methods[{index}]")
        method_id = raw_method["id"]
        if (
            not isinstance(method_id, str)
            or _KEBAB.fullmatch(method_id) is None
            or method_id in method_ids
        ):
            raise ValueError("method ids must be unique lower kebab-case names")
        method_ids.add(method_id)
        title = raw_method["title"]
        if not isinstance(title, str) or not title.strip() or "\n" in title or "\r" in title:
            raise ValueError(f"methods[{index}].title is invalid")
        reference = _safe_reference(raw_method["reference"], label=f"methods[{index}].reference")
        if reference in method_references or reference in shared:
            raise ValueError("method and shared references must be unique")
        method_references.add(reference)
        priority = raw_method["priority"]
        if type(priority) is not int or priority < 1:
            raise ValueError(f"methods[{index}].priority is invalid")
        priorities.append(priority)
        markers = raw_method["evidence_markers"]
        if (
            not isinstance(markers, list)
            or not markers
            or len(markers) > 100
            or any(
                not isinstance(marker, str)
                or not marker
                or "\n" in marker
                or "\r" in marker
                or len(marker.encode("utf-8")) > 1024
                for marker in markers
            )
            or len(markers) != len(set(markers))
        ):
            raise ValueError(f"methods[{index}].evidence_markers are invalid")
        methods.append(
            MethodCardV1(
                id=method_id,
                title=title,
                reference=reference,
                priority=priority,
                evidence_markers=tuple(markers),
            )
        )
    if priorities != list(range(1, len(methods) + 1)):
        raise ValueError("method priorities must be consecutive from 1 in array order")

    expected_references = {*shared, *method_references}
    actual_references = {f"references/{item.name}" for item in reference_nodes}
    if actual_references != expected_references:
        raise ValueError("references directory must exactly match methods.json")
    for index, method in enumerate(methods, start=1):
        try:
            text = _ordinary_file(root / method.reference, label=method.reference).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{method.reference} must be UTF-8") from exc
        for heading in _METHOD_HEADINGS:
            if heading not in text:
                raise ValueError(f"{method.reference} is missing heading {heading}")
        for marker in method.evidence_markers:
            if marker not in text:
                raise ValueError(
                    f"method {index} evidence marker is absent from its method reference: {marker}"
                )
    for reference in shared:
        try:
            _ordinary_file(root / reference, label=reference).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{reference} must be UTF-8") from exc

    return MethodsManifestV1(
        skill_name=indexed_name,
        source_wiki_sha256=source_wiki_sha256,
        required_user_inputs=required_user_inputs,
        required_artifacts=required_artifacts,
        log_derived_fields=log_derived_fields,
        shared_references=shared,
        methods=tuple(methods),
    )


def _runtime_binding(value: Any, *, expected: Mapping[str, str], label: str) -> RuntimeRoleBindingV1:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    _exact_fields(value, _ROLE_BINDING_FIELDS, label)
    if value != expected:
        raise ValueError(f"{label} must use the product-owned built-in binding")
    return RuntimeRoleBindingV1(**value)


def _binding(value: Any, *, input_names: set[str], label: str, nullable: bool = False) -> dict[str, str] | None:
    if value is None and nullable:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a binding object")
    if set(value) == {"source", "name"} and value.get("source") == "USER_FACT":
        name = value.get("name")
        if not isinstance(name, str) or name not in input_names:
            raise ValueError(f"{label} USER_FACT must name a required_user_input")
        return dict(value)
    if set(value) == {"source", "value"} and value.get("source") == "SKILL_FIXED":
        fixed = value.get("value")
        if not isinstance(fixed, str) or not fixed:
            raise ValueError(f"{label} SKILL_FIXED value must be non-empty")
        return dict(value)
    raise ValueError(f"{label} binding shape is invalid")


def _preprocessing(value: Any, methods: MethodsManifestV1) -> PreprocessingBindingV1:
    if not isinstance(value, dict):
        raise ValueError("runtime.preprocessing must be an object")
    _exact_fields(value, _PREPROCESSING_FIELDS, "runtime.preprocessing")
    requires_logparse = value["requires_logparse"]
    if type(requires_logparse) is not bool:
        raise ValueError("runtime.preprocessing.requires_logparse must be boolean")
    product = value["logparse_product"]
    roles_raw = value["roles"]
    plan_raw = value["logparse_plan"]
    if not requires_logparse:
        if product is not None or roles_raw != [] or plan_raw is not None:
            raise ValueError("non-Logparse registration must clear product, roles, and plan")
        return PreprocessingBindingV1(False, None, (), None)
    if not isinstance(product, str) or not product:
        raise ValueError("Logparse registration requires a product id")
    if "log_archive" not in methods.required_artifacts:
        raise ValueError("Logparse registration requires package artifact log_archive")
    if not isinstance(roles_raw, list) or not roles_raw or len(roles_raw) > 20:
        raise ValueError("Logparse registration roles must be a non-empty array")
    roles: list[dict[str, Any]] = []
    labels: set[str] = set()
    for index, role in enumerate(roles_raw):
        if not isinstance(role, dict):
            raise ValueError(f"runtime.preprocessing.roles[{index}] must be an object")
        _exact_fields(role, _ROLE_FIELDS, f"runtime.preprocessing.roles[{index}]")
        label = role["label"]
        if not isinstance(label, str) or _SNAKE.fullmatch(label) is None or label in labels:
            raise ValueError("Logparse role labels must be unique lower snake-case names")
        labels.add(label)
        if (
            role["presence"] not in {"REQUIRED", "OPTIONAL"}
            or not isinstance(role["description"], str)
            or not role["description"]
            or not isinstance(role["source_reference"], str)
            or not role["source_reference"]
        ):
            raise ValueError("Logparse role metadata is invalid")
        roles.append(dict(role))
    if not any(role["presence"] == "REQUIRED" for role in roles):
        raise ValueError("Logparse registration requires at least one REQUIRED role")
    if not isinstance(plan_raw, dict):
        raise ValueError("Logparse registration requires logparse_plan")
    _exact_fields(plan_raw, _PLAN_FIELDS, "runtime.preprocessing.logparse_plan")
    if plan_raw["attachment_requirement"] != "log_archive":
        raise ValueError("Logparse attachment_requirement must equal log_archive")
    input_names = set(methods.required_user_inputs)
    _binding(
        plan_raw["problem_time_binding"],
        input_names=input_names,
        label="problem_time_binding",
    )
    anchors = plan_raw["anchors"]
    if not isinstance(anchors, list) or not anchors or len(anchors) != len(roles):
        raise ValueError("Logparse anchors must match roles one-for-one")
    anchor_labels: list[str] = []
    for index, anchor in enumerate(anchors):
        if not isinstance(anchor, dict):
            raise ValueError(f"Logparse anchor {index} must be an object")
        _exact_fields(anchor, _ANCHOR_FIELDS, f"Logparse anchor {index}")
        label = anchor["label"]
        if not isinstance(label, str):
            raise ValueError("Logparse anchor label must be a string")
        anchor_labels.append(label)
        for field in ("module", "slot", "process_name"):
            _binding(anchor[field], input_names=input_names, label=f"anchors[{index}].{field}")
        _binding(
            anchor["pid"],
            input_names=input_names,
            label=f"anchors[{index}].pid",
            nullable=True,
        )
    if anchor_labels != [role["label"] for role in roles]:
        raise ValueError("Logparse anchor order and labels must equal roles")
    return PreprocessingBindingV1(
        requires_logparse=True,
        logparse_product=product,
        roles=tuple(roles),
        logparse_plan=dict(plan_raw),
    )


def load_specialized_skill_registration(registration_root: Path) -> ResolvedSpecializedSkillV1:
    """Resolve one product-owned registration and its closed generated package."""

    root = Path(registration_root)
    _real_directory(root, label="specialized Skill registration root")
    try:
        root_entries = {item.name for item in root.iterdir()}
    except OSError as exc:
        raise ValueError("specialized Skill registration root cannot be scanned") from exc
    if "diagnosis-skill.json" in root_entries:
        raise ValueError("legacy diagnosis-skill.json registrations are forbidden")
    if root_entries != {"registration-template.json", "package"}:
        raise ValueError(
            "registration root must contain exactly registration-template.json and package"
        )
    package_parent = root / "package"
    _real_directory(package_parent, label="registration package directory")
    try:
        package_children = tuple(package_parent.iterdir())
    except OSError as exc:
        raise ValueError("registration package directory cannot be scanned") from exc
    if len(package_children) != 1 or not package_children[0].is_dir() or package_children[0].is_symlink():
        raise ValueError("registration package directory must contain exactly one real Skill directory")

    raw, registration_bytes = _json_object(
        root / "registration-template.json", label="registration-template.json"
    )
    _exact_fields(raw, _REGISTRATION_FIELDS, "registration-template.json")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("registration-template.json schema_version must equal integer 1")
    registration_id = raw["registration_id"]
    if (
        not isinstance(registration_id, str)
        or _KEBAB.fullmatch(registration_id) is None
        or registration_id != root.name
    ):
        raise ValueError("registration_id must be lower kebab-case and match its directory")
    version = raw["version"]
    if not isinstance(version, str) or _SEMVER.fullmatch(version) is None:
        raise ValueError("registration version must be SemVer")
    capability = raw["capability"]
    summary = raw["summary"]
    if not isinstance(capability, str) or not capability or "\n" in capability:
        raise ValueError("registration capability must be non-empty single-line text")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("registration summary must be non-empty text")
    deployment_scope = raw["deployment_scope"]
    if deployment_scope not in {"PRODUCTION", "TEST_ONLY"}:
        raise ValueError("deployment_scope must be PRODUCTION or TEST_ONLY")

    package_binding = raw["package"]
    if not isinstance(package_binding, dict):
        raise ValueError("registration package must be an object")
    _exact_fields(package_binding, _PACKAGE_BINDING_FIELDS, "registration package")
    skill_name = package_binding["skill_name"]
    source_wiki_sha256 = package_binding["source_wiki_sha256"]
    expected_relative = f"package/{skill_name}" if isinstance(skill_name, str) else ""
    if package_binding["relative_path"] != expected_relative:
        raise ValueError("registration package relative_path must equal package/<skill_name>")
    if not isinstance(source_wiki_sha256, str) or _SHA256.fullmatch(source_wiki_sha256) is None:
        raise ValueError("registration package source_wiki_sha256 is invalid")
    package_root = package_children[0]
    methods = load_methods_package(
        package_root,
        expected_skill_name=skill_name,
        expected_source_wiki_sha256=source_wiki_sha256,
    )

    runtime = raw["runtime"]
    if not isinstance(runtime, dict):
        raise ValueError("registration runtime must be an object")
    _exact_fields(runtime, _RUNTIME_FIELDS, "registration runtime")
    diagnose = _runtime_binding(runtime["diagnose"], expected=_DIAGNOSE_BINDING, label="runtime.diagnose")
    review = _runtime_binding(runtime["review"], expected=_REVIEW_BINDING, label="runtime.review")
    preprocessing = _preprocessing(runtime["preprocessing"], methods)

    registration_sha256 = hashlib.sha256(registration_bytes).hexdigest()
    package_tree_sha256 = hash_product_directory(package_root)
    combined_sha256 = canonical_json_sha256(
        {
            "schema_version": 1,
            "registration_id": registration_id,
            "registration_sha256": registration_sha256,
            "package_tree_sha256": package_tree_sha256,
        }
    )
    registration = RegistrationTemplateV1(
        registration_id=registration_id,
        version=version,
        capability=capability,
        deployment_scope=deployment_scope,
        summary=summary,
        package_relative_path=package_binding["relative_path"],
        skill_name=skill_name,
        source_wiki_sha256=source_wiki_sha256,
        diagnose=diagnose,
        review=review,
        preprocessing=preprocessing,
    )
    return ResolvedSpecializedSkillV1(
        registration_root=root.resolve(),
        package_root=package_root.resolve(),
        registration=registration,
        methods=methods,
        registration_sha256=registration_sha256,
        package_tree_sha256=package_tree_sha256,
        combined_sha256=combined_sha256,
    )


def load_registered_skill_from_package(
    registration_root: Path,
) -> tuple[str, Path, str]:
    """Small Test Flow surface: registration id, package root, combined digest."""

    resolved = load_specialized_skill_registration(registration_root)
    return resolved.registration_id, resolved.package_root, resolved.combined_sha256


__all__ = [
    "MethodCardV1",
    "MethodsManifestV1",
    "PreprocessingBindingV1",
    "RegistrationTemplateV1",
    "ResolvedSpecializedSkillV1",
    "RuntimeRoleBindingV1",
    "load_methods_package",
    "load_registered_skill_from_package",
    "load_specialized_skill_registration",
]
