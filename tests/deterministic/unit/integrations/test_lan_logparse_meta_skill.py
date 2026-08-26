from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[4]
META_SKILL = ROOT / ".claude/skills/wiki-to-logparse-diagnosis-skill"
VALIDATOR_PATH = META_SKILL / "scripts/validate_generated_skill.py"
PACKER_PATH = META_SKILL / "assets/pack_result_zip.py"
METHODS_VALIDATOR_PATH = (
    ROOT / ".agents/skills/wiki-to-diagnosis-skill/scripts/validate_generated_skill.py"
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validator = _load(VALIDATOR_PATH, "lan_logparse_validator")
methods_validator = _load(METHODS_VALIDATOR_PATH, "methods_validator_for_lan_parity")
packer = _load(PACKER_PATH, "lan_logparse_packer")


WIKI_TEXT = """# RPC 超时

定位时用户必须提供问题时间、客户端和服务端进程信息、服务名和 API 名，并上传日志。

## 可能原因

1. API 执行时间过长。

```text
API_COMPLETE service={service} api={api} request_id={request_id} cost_us={cost_us}
```
"""
SOURCE_TEMPLATE = (
    "API_COMPLETE service={service} api={api} request_id={request_id} cost_us={cost_us}"
)
REQUIRED_INPUTS = [
    "problem_time",
    "client_slot",
    "client_process_name",
    "server_slot",
    "server_process_name",
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
name: diagnose-rpc-timeout-lan
description: 局域网 RPC 超时定位；收集必填 anchor 后加载 Helper，并返回摘要和 result.zip。
---

# RPC 超时定位

先读取 `methods.json` 和 `logparse.json`，检查全部 `required_user_inputs` 和
`required_artifacts`，包括 `client_slot`、`client_process_name`、`server_slot`、
`server_process_name`。
输入齐全后使用 Skill 工具恰好调用一次 `Skill(logparse-diagnose)`，并遵守加载后的现装合同。
只读取 `target_logs[*].log_path`，不得遍历目录或猜测路径。每条证据保留 `identity_tokens`
和 `sources`。把 `result.txt` 和实际使用日志放入扁平目录，再调用一次
`scripts/pack_result_zip.py` 生成 `result.zip`。
日志副本名使用 `<label>__<module>__slot_<slot>__<process_name>[__pid_<pid>].log`。
最终回复直接给出结论、关键证据、证据缺口、使用日志和 ZIP 路径。
"""


def _method_card() -> str:
    return """# API 执行时间过长

## 适用条件
目标 API 调用超时。

## 所需证据
完整 API_COMPLETE 日志。

## 计算与判断
使用 Wiki 中的 cost_us。

## 确认条件
观察到对应正向日志。

## 未知边界
日志缺失不能排除原因。

## 输出含义
每个独立事件分别输出完整来源和身份字面量。
"""


def _write_valid_package(tmp_path: Path) -> tuple[Path, Path]:
    wiki = tmp_path / "wiki.md"
    wiki.write_text(WIKI_TEXT, encoding="utf-8")
    skill = tmp_path / "diagnose-rpc-timeout-lan"
    references = skill / "references"
    scripts = skill / "scripts"
    references.mkdir(parents=True)
    scripts.mkdir()
    (skill / "SKILL.md").write_text(_valid_skill_text(), encoding="utf-8")
    _write_json(
        skill / "methods.json",
        {
            "schema_version": 1,
            "skill_name": skill.name,
            "source_wiki_sha256": hashlib.sha256(wiki.read_bytes()).hexdigest(),
            "required_user_inputs": REQUIRED_INPUTS,
            "required_artifacts": ["log_archive"],
            "log_derived_fields": ["request_id", "cost_us"],
            "shared_references": ["references/source-log-templates.md"],
            "methods": [
                {
                    "id": "api-execution-slow",
                    "title": "API 执行时间过长",
                    "reference": "references/api-execution-slow.md",
                    "priority": 1,
                    "evidence_markers": ["API_COMPLETE service="],
                }
            ],
        },
    )
    _write_json(
        skill / "logparse.json",
        {
            "schema_version": 1,
            "helper_skill": "logparse-diagnose",
            "module": "rpc",
            "problem_time_input": "problem_time",
            "artifact_input": "log_archive",
            "roles": validator.EXPECTED_ROLES,
        },
    )
    (references / "source-log-templates.md").write_text(
        f"# Source log templates\n\n```text\n{SOURCE_TEMPLATE}\n```\n",
        encoding="utf-8",
    )
    (references / "api-execution-slow.md").write_text(
        _method_card(), encoding="utf-8"
    )
    shutil.copyfile(PACKER_PATH, scripts / "pack_result_zip.py")
    return skill, wiki


def _validate(skill: Path, wiki: Path, module: str = "rpc") -> dict[str, object]:
    return validator.validate(skill, wiki, module)


def _errors(result: dict[str, object]) -> str:
    return "\n".join(result["errors"])


def test_lan_logparse_meta_skill_preserves_current_source_identity_contract() -> None:
    wiki = WIKI_TEXT.encode("utf-8")
    expected = methods_validator.build_source_wiki_identity(wiki, "inputs/wiki.md")
    assert validator.build_source_wiki_identity(wiki, "inputs/wiki.md") == expected


def test_valid_lan_logparse_generated_skill_passes(tmp_path: Path) -> None:
    skill, wiki = _write_valid_package(tmp_path)

    result = _validate(skill, wiki)

    assert result["ok"] is True, result["errors"]
    assert result["module"] == "rpc"
    assert result["method_count"] == 1
    assert result["template_count"] == 1


@pytest.mark.parametrize("missing", ("client_slot", "server_slot"))
def test_validator_rejects_missing_required_slot(tmp_path: Path, missing: str) -> None:
    skill, wiki = _write_valid_package(tmp_path)
    methods = json.loads((skill / "methods.json").read_text(encoding="utf-8"))
    methods["required_user_inputs"].remove(missing)
    _write_json(skill / "methods.json", methods)

    result = _validate(skill, wiki)

    assert result["ok"] is False
    assert "required_user_inputs must start" in _errors(result)


def test_validator_rejects_fixed_or_remapped_slots(tmp_path: Path) -> None:
    skill, wiki = _write_valid_package(tmp_path)
    logparse = json.loads((skill / "logparse.json").read_text(encoding="utf-8"))
    logparse["roles"][0] = {
        "label": "client",
        "required": True,
        "slot": "1",
        "process_name_input": "client_process_name",
        "pid_input": "client_pid",
    }
    _write_json(skill / "logparse.json", logparse)

    result = _validate(skill, wiki)

    assert result["ok"] is False
    assert "exact required client/server input mappings" in _errors(result)


@pytest.mark.parametrize("alias", ("service_name", "api_name"))
def test_validator_rejects_unstable_wiki_input_alias(
    tmp_path: Path,
    alias: str,
) -> None:
    skill, wiki = _write_valid_package(tmp_path)
    methods = json.loads((skill / "methods.json").read_text(encoding="utf-8"))
    original = "service" if alias == "service_name" else "api"
    methods["required_user_inputs"][methods["required_user_inputs"].index(original)] = alias
    _write_json(skill / "methods.json", methods)

    result = _validate(skill, wiki)

    assert result["ok"] is False
    assert f"forbidden aliases: {alias}" in _errors(result)


def test_validator_rejects_missing_helper_invocation(tmp_path: Path) -> None:
    skill, wiki = _write_valid_package(tmp_path)
    path = skill / "SKILL.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "Skill(logparse-diagnose)", "已安装的日志 Helper"
        ),
        encoding="utf-8",
    )

    result = _validate(skill, wiki)

    assert result["ok"] is False
    assert "must mention Skill(logparse-diagnose)" in _errors(result)


def test_validator_rejects_helper_without_exactly_once_contract(tmp_path: Path) -> None:
    skill, wiki = _write_valid_package(tmp_path)
    path = skill / "SKILL.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "恰好调用一次 `Skill(logparse-diagnose)`",
            "调用 `Skill(logparse-diagnose)`",
        ),
        encoding="utf-8",
    )

    result = _validate(skill, wiki)

    assert result["ok"] is False
    assert "exactly one Skill(logparse-diagnose) load" in _errors(result)


def test_validator_rejects_missing_flat_zip_contract(tmp_path: Path) -> None:
    skill, wiki = _write_valid_package(tmp_path)
    path = skill / "SKILL.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("扁平目录", "交付目录"),
        encoding="utf-8",
    )

    result = _validate(skill, wiki)

    assert result["ok"] is False
    assert "flat result.zip delivery" in _errors(result)


def test_validator_rejects_missing_safe_log_filename_contract(tmp_path: Path) -> None:
    skill, wiki = _write_valid_package(tmp_path)
    path = skill / "SKILL.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "<label>__<module>__slot_<slot>__<process_name>[__pid_<pid>].log",
            "client.log 和 server.log",
        ),
        encoding="utf-8",
    )

    result = _validate(skill, wiki)

    assert result["ok"] is False
    assert "<label>__<module>__slot_<slot>__<process_name>[__pid_<pid>].log" in _errors(result)


@pytest.mark.parametrize(
    "command",
    (
        "problem-locator-logparse target-logs --request x --result y",
        "python3 cli.py parse input -c config -o output",
        "python3 cli.py mech-target-logs --slot 1",
    ),
)
def test_validator_rejects_direct_logparse_fallback(
    tmp_path: Path, command: str
) -> None:
    skill, wiki = _write_valid_package(tmp_path)
    path = skill / "SKILL.md"
    path.write_text(path.read_text(encoding="utf-8") + f"\n{command}\n", encoding="utf-8")

    result = _validate(skill, wiki)

    assert result["ok"] is False
    assert "delegate instead of embedding" in _errors(result)


def test_validator_allows_explicit_prohibition_of_direct_logparse(
    tmp_path: Path,
) -> None:
    skill, wiki = _write_valid_package(tmp_path)
    path = skill / "SKILL.md"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n不得直接调用 `problem-locator-logparse`，也不得自行运行 `cli.py`。\n",
        encoding="utf-8",
    )

    result = _validate(skill, wiki)

    assert result["ok"] is True, result["errors"]


def test_validator_rejects_module_drift(tmp_path: Path) -> None:
    skill, wiki = _write_valid_package(tmp_path)

    result = _validate(skill, wiki, module="compact")

    assert result["ok"] is False
    assert "does not match the user-confirmed module" in _errors(result)


def test_validator_rejects_lost_source_template(tmp_path: Path) -> None:
    skill, wiki = _write_valid_package(tmp_path)
    (skill / "references/source-log-templates.md").write_text(
        "# Source log templates\n\n```text\n```\n", encoding="utf-8"
    )

    result = _validate(skill, wiki)

    assert result["ok"] is False
    assert "must exactly match" in _errors(result)


def test_validator_rejects_modified_packer(tmp_path: Path) -> None:
    skill, wiki = _write_valid_package(tmp_path)
    path = skill / "scripts/pack_result_zip.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n# modified\n", encoding="utf-8")

    result = _validate(skill, wiki)

    assert result["ok"] is False
    assert "must exactly match the meta Skill asset" in _errors(result)


def test_validator_rejects_reference_escape(tmp_path: Path) -> None:
    skill, wiki = _write_valid_package(tmp_path)
    methods = json.loads((skill / "methods.json").read_text(encoding="utf-8"))
    methods["methods"][0]["reference"] = "references/../outside.md"
    _write_json(skill / "methods.json", methods)

    result = _validate(skill, wiki)

    assert result["ok"] is False
    assert "reference is invalid" in _errors(result)


def test_fixed_packer_writes_report_first_and_rejects_existing_output(
    tmp_path: Path,
) -> None:
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    (delivery / "result.txt").write_text("定位结论\n", encoding="utf-8")
    (delivery / "client__rpc__slot_1__client.log").write_text(
        "client evidence\n", encoding="utf-8"
    )
    output = tmp_path / "result.zip"

    assert packer.pack_result_zip(delivery, output) == output.resolve()
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == [
            "result.txt",
            "client__rpc__slot_1__client.log",
        ]
        assert all(info.date_time == packer.FIXED_DATE_TIME for info in archive.infolist())

    with pytest.raises(ValueError, match="must not already exist"):
        packer.pack_result_zip(delivery, output)


def test_fixed_packer_rejects_non_flat_delivery(tmp_path: Path) -> None:
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    (delivery / "result.txt").write_text("定位结论\n", encoding="utf-8")
    (delivery / "nested").mkdir()

    with pytest.raises(ValueError, match="ordinary flat files"):
        packer.pack_result_zip(delivery, tmp_path / "result.zip")


def test_fixed_packer_requires_at_least_one_used_log(tmp_path: Path) -> None:
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    (delivery / "result.txt").write_text("定位结论\n", encoding="utf-8")

    with pytest.raises(ValueError, match="at least one used log"):
        packer.pack_result_zip(delivery, tmp_path / "result.zip")


def test_fixed_packer_rejects_non_log_payload(tmp_path: Path) -> None:
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    (delivery / "result.txt").write_text("定位结论\n", encoding="utf-8")
    (delivery / "broker-request.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="only result.txt and used .log files"):
        packer.pack_result_zip(delivery, tmp_path / "result.zip")
