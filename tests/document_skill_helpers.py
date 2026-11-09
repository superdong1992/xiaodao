"""Valid document-Skill registrations shared by deterministic tests."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path


def document_input(name: str, *, required: bool = True, prompt: str | None = None,
                   pattern: str | None = None, allowed_values: list[str] | None = None) -> dict:
    return {
        "name": name, "prompt": prompt or f"请提供 {name}。", "required": required,
        "constraints": {"value_type": "STRING", "min_utf8_bytes": 1, "max_utf8_bytes": 4096,
                        "pattern": pattern, "allowed_values": list(allowed_values or [])},
    }


def document_registration(name: str = "document-triage", *, mode: str = "none",
                          skill_name: str | None = None, user_inputs: list[dict] | None = None,
                          attachment_required: bool = True) -> dict:
    skill_name = skill_name or name
    preprocessing = {"mode": mode, "logparse_product": None, "roles": [], "logparse_plan": None}
    inputs = list(user_inputs or [])
    attachment = None
    if mode != "none":
        attachment = {
            "name": "log_archive" if mode == "logparse" else "materials",
            "prompt": "请上传定位所需的材料。", "required": attachment_required,
            "allowed_content_types": ["application/zip"] if mode == "logparse" else ["text/plain", "application/json", "application/zip"],
            "min_count": 1, "max_count": 20,
        }
    if mode == "logparse":
        if user_inputs is None:
            inputs = [document_input("problem_time"), document_input("process_name")]
        preprocessing.update({
            "logparse_product": "document-test",
            "roles": [{"label": "service", "description": "待定位的服务进程", "presence": "REQUIRED",
                       "source_reference": "SKILL.md"}],
            "logparse_plan": {
                "attachment_requirement": "log_archive",
                "problem_time_binding": {"source": "USER_FACT", "name": "problem_time"},
                "anchors": [{"label": "service", "module": {"source": "SKILL_FIXED", "value": "service"},
                             "slot": {"source": "SKILL_FIXED", "value": "0"},
                             "process_name": {"source": "USER_FACT", "name": "process_name"}, "pid": None}],
            },
        })
    return {
        "schema_version": 3, "registration_id": name, "version": "1.0.0",
        "capability": "document-service-triage", "deployment_scope": "PRODUCTION",
        "summary": "按服务故障定位文档分析用户提供的事实和材料。",
        "routing": {"applicability": [{"id": "service-incident", "description": "问题涉及服务故障。"}], "exclusions": []},
        "source": {"kind": "WIKI", "sha256": hashlib.sha256(b"authored wiki\n").hexdigest()},
        "package": {"format": "skill-document-v1", "relative_path": f"package/{skill_name}", "skill_name": skill_name},
        "inputs": {"user": deepcopy(inputs), "fixed": [], "derived": []},
        "attachment": attachment, "runtime": {"preprocessing": preprocessing},
    }


def write_document_registration(store: Path, name: str = "document-triage", *, mode: str = "none",
                                skill_name: str | None = None, user_inputs: list[dict] | None = None,
                                attachment_required: bool = True, registration: dict | None = None) -> Path:
    value = deepcopy(registration) if registration is not None else document_registration(
        name, mode=mode, skill_name=skill_name, user_inputs=user_inputs, attachment_required=attachment_required)
    root = Path(store) / value["registration_id"]
    package = root / value["package"]["relative_path"]
    (package / "references").mkdir(parents=True)
    skill_name = value["package"]["skill_name"]
    (package / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: 根据服务故障文档分析问题。\n---\n\n"
        "# 服务故障定位\n\n读取服务端提供的请求和材料，按 references/diagnosis.md 分析。\n"
        "按服务端输出合同提交 Markdown 报告，说明结论、限制和建议。\n", encoding="utf-8")
    (package / "references" / "diagnosis.md").write_text(
        "# 定位方法\n\n核对故障时间和服务状态；无法确认时说明缺失的信息。\n", encoding="utf-8")
    (root / "registration-template.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return root
