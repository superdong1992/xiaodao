from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from problem_locator.runtime.methods_skill import load_specialized_skill_registration


ROOT = Path(__file__).resolve().parents[4]
META_SKILL = ROOT / ".claude/skills/wiki-to-logparse-diagnosis-skill"
VALIDATOR_PATH = META_SKILL / "scripts/validate_generated_skill.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = _load(VALIDATOR_PATH, "production_registration_validator")


WIKI_TEXT = """# RPC 超时

定位时用户必须提供问题时间、客户端和服务端进程信息、服务名和 API 名，并上传日志。

## 可能原因

1. API 执行时间过长。

```python
print("IGNORED field={not_a_log}")
```

```text
API_COMPLETE service={service} api={api} request_id={request_id} cost_us={cost_us}
```

下面的无语言代码块也是稳定日志模板：

```
QUEUE_DELAY service={service} api={api} request_id={request_id} queue_us={queue_us}
```
"""
SOURCE_TEMPLATES = [
    "API_COMPLETE service={service} api={api} request_id={request_id} cost_us={cost_us}",
    "QUEUE_DELAY service={service} api={api} request_id={request_id} queue_us={queue_us}",
]
REQUIRED_INPUTS = [
    "problem_time",
    "client_slot",
    "client_process_name",
    "server_slot",
    "server_process_name",
    "client_pid",
    "server_pid",
    "service",
    "api",
]


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _valid_skill_text() -> str:
    return """---
name: diagnose-rpc-timeout
description: 从 Server 冻结的双端日志中定位 RPC 超时原因。
---

# RPC 超时定位

读取冻结 `request.json`、Server 写入的 `method-evidence-graph.json` 和
`method-evaluation-plan.json`。方法规则需要用户输入时读取 request 中的冻结值。日志证据只能来自
Evidence Graph 和 Evaluation Plan；不读取目标日志，也不重新扫描 marker。

按 Evaluation Plan 顺序逐项评估全部 `evaluation_ref`，不能在第一个确认项后停止。每项只输出
`evaluation_ref`、`verdict` 和 `reason`；证据无法决定时使用 `UNKNOWN`，并在 reason 中说明观测限制。
Server 生成的 evidence sources 可能来自 target_logs，并在内部保留 identity_tokens。

Logparse 预处理、目标日志冻结、Review 和最终 Artifact 发布由 Server 完成；诊断阶段不重新执行这些操作。
`client_pid` 和 `server_pid` 是可选事实；缺失时不请求补充，也不构成证据缺口。
"""


def _method_card() -> str:
    return """# API 执行时间过长

## 适用条件
目标 API 调用超时。

## 所需证据
完整 API_COMPLETE 或 QUEUE_DELAY 日志。

## 计算与判断
使用 Wiki 中的 cost_us 和 queue_us。

## 确认条件
观察到对应正向日志并满足 Wiki 条件。

## 未知边界
日志缺失不能排除原因。

## 输出含义
Server 把全部独立事件绑定到 evaluation_ref；Agent 只返回该引用、verdict 和 reason。
"""


def _registration_template(registration_id: str, wiki_sha256: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "registration_id": registration_id,
        "version": "1.0.0",
        "capability": "Diagnose RPC timeout evidence in frozen client and server logs.",
        "deployment_scope": "PRODUCTION",
        "summary": "使用 Wiki 派生方法分析 Server 冻结的 RPC 双端日志。",
        "package": {
            "relative_path": "package/diagnose-rpc-timeout",
            "skill_name": "diagnose-rpc-timeout",
            "source_wiki_sha256": wiki_sha256,
        },
        "runtime": {
            "diagnose": validator.DIAGNOSE_BINDING,
            "review": validator.REVIEW_BINDING,
            "preprocessing": {
                "requires_logparse": True,
                "logparse_product": "default",
                "roles": [
                    {
                        "label": "client",
                        "description": "客户端进程日志。",
                        "presence": "REQUIRED",
                        "source_reference": "Wiki 要求的客户端进程信息。",
                    },
                    {
                        "label": "server",
                        "description": "服务端进程日志。",
                        "presence": "REQUIRED",
                        "source_reference": "Wiki 要求的服务端进程信息。",
                    },
                ],
                "logparse_plan": {
                    "attachment_requirement": "log_archive",
                    "problem_time_binding": {
                        "source": "USER_FACT",
                        "name": "problem_time",
                    },
                    "anchors": [
                        {
                            "label": "client",
                            "module": {"source": "SKILL_FIXED", "value": "rpc"},
                            "slot": {"source": "USER_FACT", "name": "client_slot"},
                            "process_name": {
                                "source": "USER_FACT",
                                "name": "client_process_name",
                            },
                            "pid": {"source": "USER_FACT", "name": "client_pid"},
                        },
                        {
                            "label": "server",
                            "module": {"source": "SKILL_FIXED", "value": "rpc"},
                            "slot": {"source": "USER_FACT", "name": "server_slot"},
                            "process_name": {
                                "source": "USER_FACT",
                                "name": "server_process_name",
                            },
                            "pid": {"source": "USER_FACT", "name": "server_pid"},
                        },
                    ],
                },
            },
        },
    }


def _write_valid_registration(tmp_path: Path) -> tuple[Path, Path, Path]:
    wiki = tmp_path / "wiki.md"
    wiki.write_text(WIKI_TEXT, encoding="utf-8")
    wiki_sha256 = hashlib.sha256(wiki.read_bytes()).hexdigest()
    registration = tmp_path / "rpc-timeout-methods-v1"
    skill = registration / "package/diagnose-rpc-timeout"
    references = skill / "references"
    references.mkdir(parents=True)
    (skill / "SKILL.md").write_text(_valid_skill_text(), encoding="utf-8")
    _write_json(
        skill / "methods.json",
        {
            "schema_version": 1,
            "skill_name": skill.name,
            "source_wiki_sha256": wiki_sha256,
            "required_user_inputs": REQUIRED_INPUTS,
            "required_artifacts": ["log_archive"],
            "log_derived_fields": ["request_id", "cost_us", "queue_us"],
            "shared_references": ["references/source-log-templates.md"],
            "methods": [
                {
                    "id": "api-execution-slow",
                    "title": "API 执行时间过长",
                    "reference": "references/api-execution-slow.md",
                    "priority": 1,
                    "evidence_markers": [
                        "API_COMPLETE service=",
                        "QUEUE_DELAY service=",
                    ],
                }
            ],
        },
    )
    (references / "source-log-templates.md").write_text(
        "# Source log templates\n\n```text\n"
        + "\n".join(SOURCE_TEMPLATES)
        + "\n```\n",
        encoding="utf-8",
    )
    (references / "api-execution-slow.md").write_text(
        _method_card(), encoding="utf-8"
    )
    _write_json(
        registration / "registration-template.json",
        _registration_template(registration.name, wiki_sha256),
    )
    identity = tmp_path / "source-wiki-identity.json"
    _write_json(
        identity,
        validator.build_source_wiki_identity(wiki.read_bytes(), "inputs/wiki.md"),
    )
    return registration, wiki, identity


def _validate(
    registration: Path,
    wiki: Path,
    module: str = "rpc",
    source_identity: Path | None = None,
) -> dict[str, object]:
    return validator.validate(registration, wiki, module, source_identity)


def _errors(result: dict[str, object]) -> str:
    return "\n".join(result["errors"])


def _methods_path(registration: Path) -> Path:
    return registration / "package/diagnose-rpc-timeout/methods.json"


def _registration_path(registration: Path) -> Path:
    return registration / "registration-template.json"


def _skill_path(registration: Path) -> Path:
    return registration / "package/diagnose-rpc-timeout/SKILL.md"


def _replace_wiki_templates(
    registration: Path,
    wiki: Path,
    *,
    templates: list[str],
    markers: list[str],
    log_derived_fields: list[str],
) -> None:
    wiki.write_text(
        "# 边界测试\n\n```text\n" + "\n".join(templates) + "\n```\n",
        encoding="utf-8",
    )
    wiki_sha256 = hashlib.sha256(wiki.read_bytes()).hexdigest()
    methods = json.loads(_methods_path(registration).read_text(encoding="utf-8"))
    methods["source_wiki_sha256"] = wiki_sha256
    methods["log_derived_fields"] = log_derived_fields
    methods["methods"][0]["evidence_markers"] = markers
    _write_json(_methods_path(registration), methods)
    payload = json.loads(_registration_path(registration).read_text(encoding="utf-8"))
    payload["package"]["source_wiki_sha256"] = wiki_sha256
    _write_json(_registration_path(registration), payload)
    source_templates = (
        registration
        / "package/diagnose-rpc-timeout/references/source-log-templates.md"
    )
    source_templates.write_text(
        "# Source log templates\n\n```text\n"
        + "\n".join(templates)
        + "\n```\n",
        encoding="utf-8",
    )


def test_source_identity_v2_extracts_text_and_bare_fences() -> None:
    identity = validator.build_source_wiki_identity(
        WIKI_TEXT.encode("utf-8"), "inputs/wiki.md"
    )

    assert identity["schema_version"] == 2
    assert identity["log_template_extraction_version"] == 2
    assert identity["log_templates"] == SOURCE_TEMPLATES


def test_marker_starting_with_placeholder_ignores_trailing_suffix() -> None:
    assert (
        validator._canonical_evidence_marker(
            "{request_id} between={value} trailing-suffix-is-much-longer"
        )
        == "between="
    )
    assert validator._canonical_evidence_marker("{request_id} trailing-only") is None


def test_validator_rejects_shortened_event_name_marker(tmp_path: Path) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    methods = json.loads(_methods_path(registration).read_text(encoding="utf-8"))
    methods["methods"][0]["evidence_markers"] = ["API_COMPLETE"]
    _write_json(_methods_path(registration), methods)

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert (
        "method 1 evidence marker is not a canonical stable Wiki log marker: API_COMPLETE"
        in _errors(result)
    )


def test_valid_production_registration_passes(tmp_path: Path) -> None:
    registration, wiki, identity = _write_valid_registration(tmp_path)

    result = _validate(registration, wiki, source_identity=identity)
    skill_text = (
        registration / "package/diagnose-rpc-timeout/SKILL.md"
    ).read_text(encoding="utf-8")
    methods = json.loads(_methods_path(registration).read_text(encoding="utf-8"))

    assert result["ok"] is True, result["errors"]
    assert result["registration_id"] == registration.name
    assert result["skill_name"] == "diagnose-rpc-timeout"
    assert result["module"] == "rpc"
    assert result["method_count"] == 1
    assert result["template_count"] == 2
    assert result["log_template_extraction_version"] == 2
    assert methods["required_user_inputs"] == REQUIRED_INPUTS
    assert "request.json" in skill_text
    assert all(field in skill_text for field in ("evaluation_ref", "verdict", "reason"))


def test_valid_production_registration_loads_in_server(tmp_path: Path) -> None:
    registration, _, _ = _write_valid_registration(tmp_path)

    loaded = load_specialized_skill_registration(registration)

    assert loaded.registration.registration_id == registration.name
    assert loaded.registration.version == "1.0.0"
    assert loaded.registration.deployment_scope == "PRODUCTION"
    assert loaded.registration.preprocessing.logparse_product == "default"
    assert loaded.methods.required_user_inputs[:7] == tuple(REQUIRED_INPUTS[:7])


@pytest.mark.parametrize("name", ("logparse.json", "pack_result_zip.py", "result.zip"))
def test_validator_rejects_server_owned_files_in_package(
    tmp_path: Path,
    name: str,
) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    (registration / "package/diagnose-rpc-timeout" / name).write_text(
        "not allowed\n", encoding="utf-8"
    )

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert "Methods package entries must be exactly" in _errors(result)


def test_validator_rejects_registration_root_without_template(tmp_path: Path) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    _registration_path(registration).unlink()

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert "registration root entries must be exactly" in _errors(result)


def test_validator_rejects_additional_required_artifact(tmp_path: Path) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    methods = json.loads(_methods_path(registration).read_text(encoding="utf-8"))
    methods["required_artifacts"].append("packet_capture")
    _write_json(_methods_path(registration), methods)

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert "required_artifacts must equal exactly [log_archive]" in _errors(result)


@pytest.mark.parametrize("missing", ("client_slot", "server_slot"))
def test_validator_rejects_missing_required_slot(tmp_path: Path, missing: str) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    methods = json.loads(_methods_path(registration).read_text(encoding="utf-8"))
    methods["required_user_inputs"].remove(missing)
    _write_json(_methods_path(registration), methods)

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert "five mandatory anchor facts" in _errors(result)


@pytest.mark.parametrize("pid", ("client_pid", "server_pid"))
def test_validator_requires_optional_pid_bindings_after_mandatory_prefix(
    tmp_path: Path,
    pid: str,
) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    methods = json.loads(_methods_path(registration).read_text(encoding="utf-8"))
    methods["required_user_inputs"].remove(pid)
    _write_json(_methods_path(registration), methods)

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert "then client_pid and server_pid" in _errors(result)


def test_validator_requires_business_skill_optional_pid_semantics(tmp_path: Path) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    path = _skill_path(registration)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "缺失时不请求补充，也不构成证据缺口",
            "缺失时请求用户补充",
        ),
        encoding="utf-8",
    )

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert "fixed optional PID boundary" in _errors(result)


@pytest.mark.parametrize("alias", ("service_name", "api_name"))
def test_validator_rejects_unstable_wiki_input_alias(
    tmp_path: Path,
    alias: str,
) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    methods = json.loads(_methods_path(registration).read_text(encoding="utf-8"))
    original = "service" if alias == "service_name" else "api"
    methods["required_user_inputs"][methods["required_user_inputs"].index(original)] = alias
    _write_json(_methods_path(registration), methods)

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert f"forbidden aliases: {alias}" in _errors(result)


def test_validator_rejects_module_drift(tmp_path: Path) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)

    result = _validate(registration, wiki, module="compact")

    assert result["ok"] is False
    assert "one fixed module" in _errors(result)


@pytest.mark.parametrize(
    ("anchor_index", "field", "replacement"),
    (
        (0, "slot", {"source": "SKILL_FIXED", "value": "1"}),
        (1, "process_name", {"source": "USER_FACT", "name": "client_process_name"}),
        (0, "pid", None),
    ),
)
def test_validator_rejects_fixed_or_remapped_anchor_facts(
    tmp_path: Path,
    anchor_index: int,
    field: str,
    replacement: object,
) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    payload = json.loads(_registration_path(registration).read_text(encoding="utf-8"))
    payload["runtime"]["preprocessing"]["logparse_plan"]["anchors"][anchor_index][
        field
    ] = replacement
    _write_json(_registration_path(registration), payload)

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert "exact client/server USER_FACT bindings" in _errors(result)


def test_validator_rejects_non_default_internal_product(tmp_path: Path) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    payload = json.loads(_registration_path(registration).read_text(encoding="utf-8"))
    payload["runtime"]["preprocessing"]["logparse_product"] = "rpc"
    _write_json(_registration_path(registration), payload)

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert "logparse_product must be default" in _errors(result)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    (
        ("version", "1.1.0", "version must be 1.0.0"),
        ("deployment_scope", "TEST_ONLY", "deployment_scope must be PRODUCTION"),
    ),
)
def test_validator_rejects_non_production_identity(
    tmp_path: Path,
    field: str,
    replacement: str,
    message: str,
) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    payload = json.loads(_registration_path(registration).read_text(encoding="utf-8"))
    payload[field] = replacement
    _write_json(_registration_path(registration), payload)

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert message in _errors(result)


@pytest.mark.parametrize("binding", ("diagnose", "review"))
def test_validator_rejects_runtime_binding_drift(
    tmp_path: Path,
    binding: str,
) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    payload = json.loads(_registration_path(registration).read_text(encoding="utf-8"))
    payload["runtime"][binding]["tool_bundle_id"] = "tool-bundle/custom"
    _write_json(_registration_path(registration), payload)

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert f"runtime.{binding} must use the fixed product binding" in _errors(result)


@pytest.mark.parametrize(
    ("instruction", "message"),
    (
        ("调用 `Skill(logparse-diagnose)`。", "Server-owned token logparse-diagnose"),
        (
            "problem-locator-logparse target-logs --request x --result y",
            "Server-owned token problem-locator-logparse",
        ),
        ("生成 `result.zip`。", "Server-owned token result.zip"),
    ),
)
def test_validator_rejects_business_skill_server_work(
    tmp_path: Path,
    instruction: str,
    message: str,
) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    path = _skill_path(registration)
    path.write_text(
        path.read_text(encoding="utf-8") + f"\n{instruction}\n",
        encoding="utf-8",
    )

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert message in _errors(result)


@pytest.mark.parametrize(
    ("instruction", "token"),
    (
        ("不得运行 `pack_result_zip.py`。", "pack_result_zip"),
        ("load logparse-diagnose before reading evidence.", "logparse-diagnose"),
        ("invoke broker preprocessing before diagnosis.", "broker preprocessing invocation"),
        ("调用 Helper 获取目标日志。", "Helper invocation"),
        ("Helper调用必须先完成。", "Helper invocation"),
        ("调用broker预处理后继续。", "broker preprocessing invocation"),
        ("调用 Skill(other-diagnosis)。", "Skill( tool call"),
        ("调用Skill(other-diagnosis)。", "Skill( tool call"),
    ),
)
def test_validator_rejects_forbidden_token_hidden_in_reference(
    tmp_path: Path,
    instruction: str,
    token: str,
) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    path = registration / "package/diagnose-rpc-timeout/references/api-execution-slow.md"
    path.write_text(
        path.read_text(encoding="utf-8") + f"\n{instruction}\n",
        encoding="utf-8",
    )

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert f"Server-owned token {token}" in _errors(result)
    assert "references/api-execution-slow.md" in _errors(result)


def test_validator_allows_ordinary_message_broker_evidence(tmp_path: Path) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    path = registration / "package/diagnose-rpc-timeout/references/api-execution-slow.md"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n业务证据可以说明 message broker 延迟，但不得据此推断未记录的事件。\n",
        encoding="utf-8",
    )

    result = _validate(registration, wiki)

    assert result["ok"] is True, result["errors"]


def test_validator_rejects_lost_bare_fence_template(tmp_path: Path) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    path = registration / "package/diagnose-rpc-timeout/references/source-log-templates.md"
    path.write_text(
        f"# Source log templates\n\n```text\n{SOURCE_TEMPLATES[0]}\n```\n",
        encoding="utf-8",
    )

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert "version 2 Wiki log template inventory" in _errors(result)


def test_validator_rejects_stale_source_identity_extraction_version(
    tmp_path: Path,
) -> None:
    registration, wiki, identity = _write_valid_registration(tmp_path)
    payload = json.loads(identity.read_text(encoding="utf-8"))
    payload["log_template_extraction_version"] = 1
    _write_json(identity, payload)

    result = _validate(registration, wiki, source_identity=identity)

    assert result["ok"] is False
    assert "log_template_extraction_version must be 2" in _errors(result)


def test_validator_rejects_identifier_array_above_server_limit(tmp_path: Path) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    methods = json.loads(_methods_path(registration).read_text(encoding="utf-8"))
    methods["required_user_inputs"].extend(
        f"extra_{index:03d}" for index in range(192)
    )
    _write_json(_methods_path(registration), methods)

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert "required_user_inputs must contain at most 200 identifiers" in _errors(result)
    with pytest.raises(ValueError, match="unique lower snake-case identifiers"):
        load_specialized_skill_registration(registration)


def test_validator_rejects_method_array_above_server_limit(tmp_path: Path) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    package = registration / "package/diagnose-rpc-timeout"
    methods = json.loads(_methods_path(registration).read_text(encoding="utf-8"))
    for index in range(2, 102):
        reference = f"references/generated-{index:03d}.md"
        methods["methods"].append(
            {
                "id": f"generated-{index:03d}",
                "title": f"生成方法 {index}",
                "reference": reference,
                "priority": index,
                "evidence_markers": ["API_COMPLETE service="],
            }
        )
        (package / reference).write_text(_method_card(), encoding="utf-8")
    _write_json(_methods_path(registration), methods)

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert "methods must be a non-empty array with at most 100 items" in _errors(result)
    with pytest.raises(ValueError, match="non-empty bounded array"):
        load_specialized_skill_registration(registration)


def test_validator_rejects_marker_array_above_server_limit(tmp_path: Path) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    templates = [f"MARKER_{index:03d} value={{value}}" for index in range(101)]
    markers = [f"MARKER_{index:03d} value=" for index in range(101)]
    _replace_wiki_templates(
        registration,
        wiki,
        templates=templates,
        markers=markers,
        log_derived_fields=["value"],
    )

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert "method 1 evidence_markers are invalid" in _errors(result)
    with pytest.raises(ValueError, match="evidence_markers are invalid"):
        load_specialized_skill_registration(registration)


def test_validator_rejects_marker_above_server_byte_limit(tmp_path: Path) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    marker = "X" * 1025
    _replace_wiki_templates(
        registration,
        wiki,
        templates=[f"{marker}{{value}}"],
        markers=[marker],
        log_derived_fields=["value"],
    )

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert "method 1 evidence_markers are invalid" in _errors(result)
    with pytest.raises(ValueError, match="evidence_markers are invalid"):
        load_specialized_skill_registration(registration)


def test_validator_rejects_registration_and_package_digest_drift(tmp_path: Path) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    payload = json.loads(_registration_path(registration).read_text(encoding="utf-8"))
    payload["package"]["source_wiki_sha256"] = "0" * 64
    _write_json(_registration_path(registration), payload)

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert "registration and Methods package Wiki digests differ" in _errors(result)


def test_validator_rejects_reference_escape(tmp_path: Path) -> None:
    registration, wiki, _ = _write_valid_registration(tmp_path)
    methods = json.loads(_methods_path(registration).read_text(encoding="utf-8"))
    methods["methods"][0]["reference"] = "references/../outside.md"
    _write_json(_methods_path(registration), methods)

    result = _validate(registration, wiki)

    assert result["ok"] is False
    assert "method 1 reference is invalid" in _errors(result)
