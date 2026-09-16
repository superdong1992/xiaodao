"""Select independently grounded Methods findings for a frozen no-review Job."""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any, Mapping, Sequence

from problem_locator.contracts import canonical_json_sha256

from .methods_grounding import (
    FrozenTargetLogV1, MethodDiagnosisDraftV1, MethodEvidenceV1,
    MethodsValidationError, SkillLoadReceiptV1, VerifiedMethodDiagnosisV1,
    verify_method_diagnosis, verify_method_evidence,
)
from .methods_skill import ResolvedSpecializedSkillV1


def diagnosis_mapping(draft: MethodDiagnosisDraftV1) -> dict[str, Any]:
    return {"schema_version": 1, **asdict(draft)}


def _identity(raw: object) -> tuple[str, tuple[str, ...]] | None:
    if not isinstance(raw, dict):
        return None
    method, tokens = raw.get("method_id"), raw.get("identity_tokens")
    if (not isinstance(method, str) or not method.strip()
            or not isinstance(tokens, list) or not tokens
            or any(not isinstance(token, str) or not token.strip() for token in tokens)):
        return None
    return method, tuple(sorted(tokens))


def select_method_diagnosis(
    *, skill: ResolvedSpecializedSkillV1, draft: Mapping[str, Any],
    target_logs: Sequence[FrozenTargetLogV1], logparse_receipt_sha256: str,
    skill_load: SkillLoadReceiptV1, missing_targets: Sequence[str] = (),
) -> VerifiedMethodDiagnosisV1:
    """Keep complete findings only; shared frozen-input failures remain fatal.

    Header ambiguity is never repaired. A malformed item cannot hide a conflict
    with another item having the same identity. Rejected claims are not promoted
    into candidate methods and their summaries never reach the effective draft.
    """
    if not isinstance(draft, dict):
        raise ValueError("method diagnosis draft must be an object")
    status = draft.get("status")
    if not isinstance(status, str) or status not in {"CONFIRMED", "PARTIAL", "INSUFFICIENT"}:
        raise ValueError("method diagnosis status is invalid")
    raw_items = draft.get("evidence")
    if not isinstance(raw_items, list) or len(raw_items) > 500:
        raise ValueError("evidence must be a bounded array")
    header = MethodDiagnosisDraftV1.from_mapping({**draft, "status": "PARTIAL", "evidence": []})
    if status == "CONFIRMED" and (not header.confirmed_methods or not raw_items):
        raise ValueError("CONFIRMED requires recognizable method evidence")
    if status == "INSUFFICIENT" and (header.confirmed_methods or raw_items):
        raise ValueError("INSUFFICIENT forbids confirmed methods and evidence")

    # Verify shared Skill/receipt/scan identities once, before item selection.
    shared = verify_method_diagnosis(
        skill=skill, draft=MethodDiagnosisDraftV1("INSUFFICIENT", (), (), (), (), ()),
        target_logs=target_logs, logparse_receipt_sha256=logparse_receipt_sha256,
        skill_load=skill_load,
    )
    by_source = {item.source_id: item for item in target_logs}
    source_lines: dict[str, tuple[str, ...]] = {}
    methods = skill.methods.method_by_id
    rejected: list[dict[str, Any]] = []
    groups: dict[tuple[str, tuple[str, ...]], list[tuple[int, MethodEvidenceV1 | None]]] = {}
    recognizable = 0

    def reject(section: str, index: int, reason: str) -> None:
        rejected.append({"section": section, "index": index, "reason_code": reason})

    for index, raw in enumerate(raw_items):
        identity = _identity(raw)
        item = None
        try:
            parsed = MethodDiagnosisDraftV1.from_mapping({
                **draft, "status": "PARTIAL", "evidence": [raw],
            })
            item = parsed.evidence[0]
            recognizable += 1
            # Ordering has no bearing on the exact finding or its citations.
            item = replace(item, identity_tokens=tuple(sorted(item.identity_tokens)),
                           sources=tuple(sorted(item.sources, key=lambda value: (
                               value.source_id, value.line_number, value.marker, value.line))))
        except (TypeError, ValueError):
            reject("evidence", index, "EVIDENCE_STRUCTURE_INVALID")
        if identity is not None:
            groups.setdefault(identity, []).append((index, item))
    if raw_items and not recognizable:
        raise ValueError("diagnosis has no structurally recognizable evidence")

    retained: list[MethodEvidenceV1] = []
    retained_items: list[dict[str, Any]] = []
    merged: list[dict[str, int]] = []
    for identity, entries in groups.items():
        first = entries[0][1]
        if any(item is None or item != first for _, item in entries):
            if len(entries) > 1:
                for index, _ in entries:
                    reject("evidence", index, "EVIDENCE_IDENTITY_CONFLICT")
            continue
        assert first is not None
        try:
            if first.method_id not in skill_load.loaded_method_ids:
                raise ValueError("method has no positive frozen marker scan")
            verify_method_evidence(first, skill=skill,
                confirmed_methods=header.confirmed_methods, by_source=by_source,
                source_lines=source_lines)
        except (TypeError, ValueError) as exc:
            reason = (exc.reason_code.value if isinstance(exc, MethodsValidationError)
                      else "EVIDENCE_GROUNDING_INVALID")
            for index, _ in entries:
                reject("evidence", index, reason)
            continue
        retained.append(first)
        retained_items.append({"index": entries[0][0], "method_id": identity[0],
                               "identity_tokens": list(identity[1])})
        merged.extend({"index": index, "retained_index": entries[0][0]}
                      for index, _ in entries[1:])

    confirmed = tuple(method for method in header.confirmed_methods
                      if any(item.method_id == method for item in retained))
    for index, method in enumerate(header.confirmed_methods):
        if method not in confirmed:
            reject("confirmed_methods", index, "CONFIRMED_EVIDENCE_MISSING")
    candidates = tuple(method for method in header.candidate_methods if method in methods)
    for index, method in enumerate(header.candidate_methods):
        if method not in methods:
            reject("candidate_methods", index, "METHOD_UNKNOWN")
    for index, _ in enumerate(missing_targets):
        reject("targets", index, "TARGET_UNAVAILABLE")
    effective_status = ("INSUFFICIENT" if not retained else
                        "PARTIAL" if rejected or candidates or status == "PARTIAL" else "CONFIRMED")
    gaps = tuple(f"{item['section']}[{item['index']}] 未纳入结论：{item['reason_code']}。"
                 for item in rejected)
    if effective_status != "CONFIRMED" and not gaps:
        gaps = ("现有证据尚不足以确认全部完成条件。",)
    # Keep original limitations bounded independently of the bounded selection
    # list, and do not copy rejected summaries into any public field.
    limitations = header.limitations
    effective = MethodDiagnosisDraftV1(effective_status, confirmed, candidates,
                                      tuple(retained), limitations, header.safety_notes)
    selection = {
        "schema_version": 1, "source_draft_sha256": canonical_json_sha256(draft),
        "effective_draft_sha256": canonical_json_sha256(diagnosis_mapping(effective)),
        "registration_sha256": shared.audit.registration_sha256,
        "package_tree_sha256": shared.audit.package_tree_sha256,
        "logparse_receipt_sha256": logparse_receipt_sha256,
        "target_log_sha256": {item.source_id: item.content_sha256 for item in target_logs},
        "retained": retained_items, "merged": merged, "rejected": rejected,
        "missing_targets": list(missing_targets), "status": effective_status,
        "gap_messages": list(gaps),
    }
    return VerifiedMethodDiagnosisV1(effective, replace(shared.audit,
        status=effective_status, confirmed_methods=confirmed, evidence_count=len(retained)), selection)
