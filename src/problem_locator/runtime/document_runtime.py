"""Execute v3 document Skills using server-prepared, immutable materials."""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import re
import time
from typing import TYPE_CHECKING

from problem_locator.contracts import (
    ApplicationErrorDetail, AttachmentRequirementConstraints, DiagnosisOutcome,
    DiagnosisStateDelta, ErrorCode, ExecutionStage, InputRequirementConstraints,
    JobOutcome, OutcomeResultType, PendingRequirement, RequirementKind,
    RequirementStatus, ResolvedLogparsePlanInput, SupplementPolicy,
    bytes_sha256, canonical_json_bytes, is_specialized_direct, specialized_direct_skill_name,
)
from problem_locator.journey import record_stage_completed, record_stage_started
from problem_locator.storage.capacity import capacity_reservation

from .agent_backend import BackendExecutionLimits
from .asset_snapshot import snapshot_bytes
from .continuation_context import build_report_continuation_context
from .direct_generic_logs import DirectGenericLogError, prepare_direct_sources
from .failures import runtime_failure
from .resolved_logparse import _active_role_labels, compile_resolved_logparse_plans
from .skill_direct import parse_skill_direct_response
from .workspace import _atomic_write, _fallback_expected_directory, _set_inputs_read_only

if TYPE_CHECKING:
    from .diagnosis_runtime import DiagnosisRuntime
    from .document_skill import ResolvedDocumentSkill


_MATERIAL_FORMAT_LABELS = {
    "text/plain": "文本（.log/.txt）", "application/json": "JSON（.json）",
    "application/x-ndjson": "逐行 JSON（.jsonl/.ndjson）", "application/jsonl": "逐行 JSON（.jsonl）",
    "application/zip": "ZIP（.zip）", "application/x-tar": "TAR（.tar）",
    "application/gzip": "GZIP（.gz/.tar.gz/.tgz）",
}


def _invalid_input(message, field):
    return runtime_failure(stage=ExecutionStage.WORKSPACE_PREPARE,
        code=ErrorCode.WORKSPACE_PREPARE_FAILED, message=message,
        details=[ApplicationErrorDetail(field=field, resource_type=None,
            resource_id=None, resource_ref=None, expected="registered-input",
            actual=None, limit=None, observed=None)])


def document_preflight(job, aggregate, skill):
    """Validate initial facts too; only missing mandatory inputs become requirements."""
    registration = skill.registration
    definitions = {item.name: item for item in registration.inputs.user}
    facts = {}
    for fact in job.context_snapshot.user_facts:
        name = fact.provenance.input_name
        if name not in definitions or name in facts:
            raise _invalid_input("已提供的参数未在当前 Skill 中声明，或名称重复。", "user_facts")
        value = fact.statement
        constraints = definitions[name].constraints
        try:
            size = len(value.encode("utf-8"))
            valid = (constraints["min_utf8_bytes"] <= size <= constraints["max_utf8_bytes"]
                and (constraints["pattern"] is None or re.fullmatch(constraints["pattern"], value) is not None)
                and (not constraints["allowed_values"] or value in constraints["allowed_values"]))
        except (UnicodeError, TypeError, ValueError):
            valid = False
        if not valid:
            raise _invalid_input(f"参数 {name} 不符合当前 Skill 的格式要求。", name)
        facts[name] = value
    required = {item.name for item in definitions.values() if item.required}
    preparation = registration.preprocessing
    if preparation.requires_logparse:
        plan = preparation.logparse_plan
        for binding in (plan["problem_time_binding"],):
            if binding["source"] == "USER_FACT":
                required.add(binding["name"])
        active = _active_role_labels(list(preparation.roles), plan["anchors"], facts)
        for anchor in plan["anchors"]:
            if anchor["label"] in active:
                for field in ("module", "slot", "process_name"):
                    binding = anchor[field]
                    if binding["source"] == "USER_FACT":
                        required.add(binding["name"])
    missing = [item for item in definitions.values() if item.name in required and item.name not in facts]
    attachment = registration.attachment
    missing_materials = False
    if attachment is not None:
        selected = [aggregate.attachments[ref] for ref in job.attachment_refs]
        if not selected:
            # Website uploads are adopted after ROUTE. An explicit initial
            # selection must cross that handoff even when materials are optional.
            missing_materials = attachment.required or aggregate.case.initial_log_archive_expected
        elif (not attachment.min_count <= len(selected) <= attachment.max_count
                or any(item.content_type not in attachment.allowed_content_types for item in selected)):
            raise _invalid_input("材料格式或数量不符合当前 Skill 的要求。", "attachment_ids")
    return facts, missing, missing_materials


def _publish_preflight(runtime, job, skill, missing, missing_materials):
    requirements, input_ids, attachment_ids = [], [], []
    for definition in missing:
        rid = runtime._id_generator.derive("pending_requirement", (job.job_id, "INPUT", definition.name))
        requirements.append(PendingRequirement(requirement_id=rid, kind=RequirementKind.INPUT,
            name=definition.name, prompt=definition.prompt, required=True,
            constraints=InputRequirementConstraints.model_validate(definition.constraints),
            status=RequirementStatus.OPEN, requested_by_job_id=job.job_id,
            fulfilled_by_refs=[], supplement_policy=SupplementPolicy.MISSING_ONLY))
        input_ids.append(rid)
    if missing_materials:
        definition = skill.registration.attachment
        rid = runtime._id_generator.derive("pending_requirement", (job.job_id, "ATTACHMENT", definition.name))
        count = (str(definition.min_count) if definition.min_count == definition.max_count
                 else f"{definition.min_count}～{definition.max_count}")
        formats = "、".join(_MATERIAL_FORMAT_LABELS[item] for item in definition.allowed_content_types)
        prompt = (f"{definition.prompt}\n允许格式：{formats}。"
                  f"文件数量：{count} 份。")
        requirements.append(PendingRequirement(requirement_id=rid, kind=RequirementKind.ATTACHMENT,
            name=definition.name, prompt=prompt, required=True,
            constraints=AttachmentRequirementConstraints(
                allowed_content_types=list(definition.allowed_content_types),
                min_count=definition.min_count, max_count=definition.max_count),
            status=RequirementStatus.OPEN, requested_by_job_id=job.job_id,
            fulfilled_by_refs=[], supplement_policy=SupplementPolicy.MISSING_ONLY))
        attachment_ids.append(rid)
    outcome = JobOutcome(outcome_id=runtime._id_generator.new("job_outcome"),
        job_id=job.job_id, case_id=job.case_id, job_type=job.job_type,
        base_state_revision=job.base_state_revision,
        result_type=OutcomeResultType.NEED_INPUT if input_ids else OutcomeResultType.NEED_ATTACHMENT,
        payload=DiagnosisOutcome(findings=[], state_delta=DiagnosisStateDelta(
            problem_spec_patch=None, add_user_facts=[], proposed_facts=[], add_active_hypotheses=[],
            update_hypotheses=[], reject_hypotheses=[], add_open_questions=[], resolve_questions=[],
            add_pending_requirements=requirements, fulfill_requirements=[], add_evidence_bindings=[]),
            requested_input=input_ids, requested_attachments=attachment_ids,
            candidate_conclusion_draft=None, recommended_next_step="请补充当前定位方法所需的参数和材料。"),
        consumed_evidence_refs=[], proposed_evidence=[], proposed_artifacts=[], error=None,
        produced_at=runtime._clock.now(), decision_audit=None)
    runtime._publish_audit_bytes(job, "document-preflight.json", canonical_json_bytes({
        "schema_version": 1, "registration_id": skill.registration_id,
        "missing_user_inputs": [item.name for item in missing],
        "missing_materials": missing_materials}))
    receipt = runtime._publisher.publish_success(job, outcome)
    runtime._record_produced_outcome(receipt)
    return receipt


def _freeze_inputs(workspace, request, evidence, target_contents, documents):
    inputs, _ = _fallback_expected_directory(workspace, ("inputs",),
        ((workspace.inputs_device, workspace.inputs_inode),))
    request_bytes, evidence_bytes = canonical_json_bytes(request), canonical_json_bytes(evidence)
    if any((inputs / name).exists() for name in ("request.json", "evidence-manifest.json")):
        raise ValueError("文档型诊断输入已经存在。")
    inputs.chmod(0o755)
    try:
        skill_root = inputs / "skill"
        skill_root.mkdir(mode=0o700)
        (skill_root / "references").mkdir(mode=0o700)
        for relative, content in documents:
            destination = skill_root / relative
            with capacity_reservation(workspace.capacity, destination, len(content)):
                _atomic_write(destination, content)
        if target_contents:
            target_root = inputs / "evidence"
            target_root.mkdir(mode=0o700)
            for ordinal, content in enumerate(target_contents, 1):
                destination = target_root / f"log-{ordinal:06d}.log"
                with capacity_reservation(workspace.capacity, destination, len(content)):
                    _atomic_write(destination, content)
        for name, content in (("request.json", request_bytes), ("evidence-manifest.json", evidence_bytes)):
            with capacity_reservation(workspace.capacity, inputs / name, len(content)):
                _atomic_write(inputs / name, content)
    finally:
        _set_inputs_read_only(inputs)
    return request_bytes, evidence_bytes


def _verify_inputs(workspace, request_bytes, evidence_bytes, evidence, documents, check_abort):
    inputs, _ = _fallback_expected_directory(workspace, ("inputs",),
        ((workspace.inputs_device, workspace.inputs_inode),))
    for name, expected in (("request.json", request_bytes), ("evidence-manifest.json", evidence_bytes)):
        path = inputs / name
        if path.is_symlink() or path.read_bytes() != expected:
            raise _invalid_input("诊断期间请求或材料清单发生变化。", name)
    for item in [*evidence["files"], *documents]:
        check_abort()
        path = workspace.root / item["path"]
        parents = path.relative_to(inputs).parents
        if (path.is_symlink() or any((inputs / parent).is_symlink() for parent in parents)
                or not path.is_file() or path.stat().st_nlink != 1):
            raise _invalid_input("诊断材料的文件身份发生变化。", "evidence")
        digest, size = hashlib.sha256(), 0
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                check_abort()
                digest.update(chunk)
                size += len(chunk)
        if size != item["size"] or digest.hexdigest() != item["sha256"]:
            raise _invalid_input("诊断材料内容与准备时的记录不一致。", "evidence")


def execute_document_skill(runtime: DiagnosisRuntime, job, aggregate, assets, cancellation,
                           skill: ResolvedDocumentSkill):
    preprocessing = skill.registration.preprocessing
    if (not is_specialized_direct(job)
            or preprocessing.requires_logparse != (job.logparse_tool_ref is not None)
            or preprocessing.logparse_product != job.logparse_product):
        raise runtime_failure(stage=ExecutionStage.ASSET_RESOLUTION,
            code=ErrorCode.ASSET_VERSION_UNAVAILABLE, message="文档型 Skill 与任务固定的运行配置不一致。")
    facts, missing, missing_materials = document_preflight(job, aggregate, skill)
    if missing or missing_materials:
        return _publish_preflight(runtime, job, skill, missing, missing_materials)
    mode = skill.registration.preprocessing.mode
    limits = runtime._backend_test_limits or BackendExecutionLimits.from_resource_limits(job.resource_limits)
    deadline = time.monotonic() + limits.wall_time_seconds

    def check_abort():
        if cancellation.is_cancelled():
            raise runtime_failure(stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.BACKEND_CANCELLED, message="专用诊断已取消。")
        if time.monotonic() >= deadline:
            raise runtime_failure(stage=ExecutionStage.WORKSPACE_PREPARE,
                code=ErrorCode.BACKEND_TIMEOUT, message="专用诊断超时。")

    check_abort()
    preparing = record_stage_started(ExecutionStage.WORKSPACE_PREPARE)
    plans = tuple(ResolvedLogparsePlanInput.model_validate({
        **plan.model_dump(mode="json"), "schema_version": 2,
    }) for plan in compile_resolved_logparse_plans(job, aggregate, assets)) if mode == "logparse" else ()
    workspace = runtime._workspace_manager.prepare_document_main_metadata_only(
        job, aggregate, resolved_logparse_plan=plans[0] if plans else None)
    evidence = {"schema_version": 1, "preparation": mode, "sources": [], "files": [],
        "skipped": [], "limitations": [], "coverage": {"scope": "INPUT_PREPARATION", "complete": True}}
    target_contents = []
    if mode == "direct_files" and job.attachment_refs:
        sources = runtime._workspace_manager.prepare_document_source_workspace(
            job, aggregate, runtime._resource_store, check_abort=check_abort)
        entries = {entry.resource_id: entry for entry in sources.manifest.entries
                   if entry.input_kind == "ATTACHMENT"}
        try:
            prepared = json.loads(prepare_direct_sources(workspace,
                sources=[(sources.root / entries[item.attachment_id].relative_path, item)
                         for item in sources.attachments], cancellation=cancellation,
                deadline=deadline, capacity=workspace.capacity, directory="evidence", publish_manifest=False))
        except DirectGenericLogError as exc:
            raise _invalid_input(str(exc), "attachment_ids") from exc
        evidence.update({key: prepared[key] for key in ("sources", "skipped", "coverage")})
        evidence["files"] = [{**item, "source_id": f"file-{index:06d}", "path": item["log_path"]}
                             for index, item in enumerate(prepared["logs"], 1)]
    elif mode == "logparse":
        if not plans:
            raise _invalid_input("Logparse 模式缺少已固定的解析计划。", "preprocessing")
        runtime._announce(job, "LOGPARSE")
        evidence["logparse_receipts"] = []
        for plan in plans:
            check_abort()
            source = runtime._read_logparse_source(job=job, aggregate=aggregate, plan=plan,
                assets=assets, cancellation=cancellation, batch=len(plans) > 1, deadline=deadline)
            attachment = aggregate.attachments.get(plan.attachment_id)
            identity = ({"source_attachment_id": attachment.attachment_id,
                "source_attachment_name": attachment.name, "source_attachment_sha256": attachment.sha256,
                "size": attachment.size, "content_type": attachment.content_type} if attachment else
                {"source_artifact_id": plan.artifact_id})
            evidence["sources"].append(identity)
            evidence["logparse_receipts"].append(source.receipt_context)
            for item in source.validated.target_logs:
                target_contents.append(item.content)
                number = len(target_contents)
                evidence["files"].append({**identity, "source_id": f"file-{number:06d}",
                    "label": item.target.label, "path": f"inputs/evidence/log-{number:06d}.log",
                    "size": len(item.content), "sha256": bytes_sha256(item.content)})
            for target in source.validated.authoritative_targets.unresolved:
                evidence["limitations"].append({"label": target.label, "match_status": target.match_status,
                    "caveats": list(target.caveats)})
        evidence["coverage"]["complete"] = not evidence["limitations"]
    if not evidence["files"] and mode != "none":
        evidence["limitations"].append("没有可读材料；本次材料准备不能证明已完成日志分析。")
    documents = [(relative, snapshot_bytes(skill.package_root / relative)) for relative in skill.document_paths]
    document_manifest = [{"path": f"inputs/skill/{relative}", "size": len(content),
                          "sha256": bytes_sha256(content)} for relative, content in documents]
    request = {"schema_version": 1, "job_id": job.job_id, "case_id": job.case_id,
        "registration_id": skill.registration_id, "skill_ref": job.skill_ref.model_dump(mode="json"),
        "source": asdict(skill.registration.source), "problem_spec": job.context_snapshot.problem_spec.model_dump(mode="json"),
        "raw_problem_text": aggregate.case.raw_problem_text,
        "product_line": aggregate.case.product_line, "user_inputs": facts,
        "fixed_inputs": {item.name: item.value for item in skill.registration.inputs.fixed},
        "derived_fields": [asdict(item) for item in skill.registration.inputs.derived],
        "preparation": mode, "evidence_manifest_path": "inputs/evidence-manifest.json",
        "skill_entry_path": "inputs/skill/SKILL.md", "skill_documents": document_manifest}
    request_bytes, evidence_bytes = _freeze_inputs(workspace, request, evidence, target_contents, documents)
    runtime._publish_audit_bytes(job, "document-request.json", request_bytes)
    runtime._publish_audit_bytes(job, "document-evidence-manifest.json", evidence_bytes)
    record_stage_completed(ExecutionStage.WORKSPACE_PREPARE, preparing,
        data={"preparation": mode, "file_count": len(evidence["files"])})
    materials = assets.bind_workspace(workspace).materials
    entry_text = dict(documents)["SKILL.md"].decode("utf-8")
    materials = replace(materials, skill=(entry_text + "\n\n"
        "本 Skill 的工作区入口是 inputs/skill/SKILL.md；相对引用从 inputs/skill/ 解析。"
        "参考资料已固定在 inputs/skill/references/，按需要读取 request.json 中 skill_documents 列出的文档。\n"))
    context = runtime._materialize_context(job, workspace, materials)
    prompt = context.body + build_report_continuation_context(job.continuation_context) + (
        "\n本次为文档型专用 Skill。读取 inputs/request.json 和 inputs/evidence-manifest.json，"
        "只使用清单列出的可读材料；资源登记表中的原始上传路径不属于可读材料。"
        "本次准备方式为 " + mode + "。清单只说明准备情况，不代表已分析；请保留跳过项和证据缺口。"
        "按 Skill 判断并遵守输出合同返回状态标记和 Markdown 正文，不创建结果文件。\n")
    if len(prompt.encode("utf-8")) + len(request_bytes) + len(evidence_bytes) > job.resource_limits.context_bytes:
        raise runtime_failure(stage=ExecutionStage.CONTEXT_BUILD, code=ErrorCode.CONTEXT_LIMIT,
            message="文档型 Skill 的上下文超过任务限额。")
    runtime._publish_audit_bytes(job, "document-prompt.txt", prompt.encode("utf-8"))
    check_abort()
    runtime._announce(job, "DIAGNOSING")
    result = runtime._backend_for_job(job).execute(prompt=prompt, workspace_root=workspace.root,
        cancellation=cancellation, log_sinks=runtime._open_log_sinks(job),
        resource_limits=job.resource_limits,
        test_limits=replace(limits, wall_time_seconds=max(0.001, deadline - time.monotonic())),
        broker_environment=None, backend_phase="DIAGNOSE", file_access="read-only")
    check_abort()
    _verify_inputs(workspace, request_bytes, evidence_bytes, evidence, document_manifest, check_abort)
    if isinstance(result.final_result, str):
        runtime._publish_audit_bytes(job, "document-diagnosis.raw.txt", result.final_result.encode("utf-8"))
    payload = parse_skill_direct_response(result.final_result, skill_name=specialized_direct_skill_name(job))
    outcome = JobOutcome(outcome_id=runtime._id_generator.new("job_outcome"), job_id=job.job_id,
        case_id=job.case_id, job_type=job.job_type, base_state_revision=job.base_state_revision,
        result_type=OutcomeResultType.COMPLETED, payload=payload, consumed_evidence_refs=[],
        proposed_evidence=[], proposed_artifacts=[], error=None, produced_at=runtime._clock.now(), decision_audit=None)
    runtime._publish_audit_bytes(job, "skill-direct-delivery.json", canonical_json_bytes({
        "schema_version": 1, "validation_mode": "off", "job_id": job.job_id,
        "skill_ref": job.skill_ref.model_dump(mode="json"), "status": payload.status.value,
        "report_sha256": payload.report_sha256, "report_utf8_size": payload.report_utf8_size}))
    runtime._announce(job, "REPORTING")
    receipt = runtime._publisher.publish_success(job, outcome, workspace.manifest)
    runtime._record_produced_outcome(receipt)
    return receipt
