"""Select independently grounded Methods findings for a frozen Job."""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Any, Mapping, Sequence

from problem_locator.contracts import MethodsValidationReasonCode, canonical_json_sha256

from .methods_grounding import (
    FrozenTargetLogV1, MethodDiagnosisDraftV1, MethodEvidenceV1,
    MethodsValidationError, SkillLoadReceiptV1, VerifiedMethodDiagnosisV1,
    _strings, _verify_method_inputs, verify_method_evidence,
)
from .methods_skill import ResolvedSpecializedSkillV1


def diagnosis_mapping(draft: MethodDiagnosisDraftV1) -> dict[str, Any]:
    return {"schema_version": 1, **asdict(draft)}


def _identity(raw: object) -> tuple[str, tuple[str, ...], tuple[tuple[str, int], ...]] | None:
    if not isinstance(raw, dict):
        return None
    method = raw.get("method_id")
    if not isinstance(method, str) or not method.strip():
        return None
    tokens = tuple(sorted(_strings(raw.get("identity_tokens"))))
    sources = raw.get("sources", ())
    locations = tuple(sorted({(item["source_id"], item["line_number"])
        for item in sources if isinstance(item, dict)
        and isinstance(item.get("source_id"), str)
        and type(item.get("line_number")) is int})) if isinstance(sources, (list, tuple)) else ()
    return method, tokens, locations if not tokens else ()


def select_method_diagnosis(
    *, skill: ResolvedSpecializedSkillV1, draft: Mapping[str, Any],
    target_logs: Sequence[FrozenTargetLogV1], logparse_receipt_sha256: str,
    skill_load: SkillLoadReceiptV1, missing_targets: Sequence[str] = (),
) -> VerifiedMethodDiagnosisV1:
    """Keep complete findings only; shared frozen-input failures remain fatal.

    A malformed item cannot hide a conflict with another item having the same
    identity. Rejected claims are not promoted into candidate methods.
    """
    if not isinstance(draft, dict):
        raise ValueError("method diagnosis draft must be an object")
    raw_items = draft.get("evidence", [])
    raw_items = raw_items if isinstance(raw_items, (list, tuple)) else []
    header = MethodDiagnosisDraftV1.from_mapping(draft)
    status = header.status

    # Verify shared Skill/receipt/scan identities once, before item selection.
    shared_audit = _verify_method_inputs(
        skill=skill,
        target_logs=target_logs, logparse_receipt_sha256=logparse_receipt_sha256,
        skill_load=skill_load,
    )
    by_source = {item.source_id: item for item in target_logs}
    source_lines: dict[str, tuple[str, ...]] = {}
    methods = skill.methods.method_by_id
    rejected: list[dict[str, Any]] = []
    groups: dict[tuple[str, tuple[str, ...], tuple[tuple[str, int], ...]],
                 list[tuple[int, MethodEvidenceV1 | None]]] = {}

    def reject(section: str, index: int, reason: str) -> None:
        rejected.append({"section": section, "index": index, "reason_code": reason})

    for index, raw in enumerate(raw_items):
        identity = _identity(raw)
        parsed = MethodDiagnosisDraftV1.from_mapping({
            **draft, "status": "PARTIAL", "evidence": [raw],
        })
        item = parsed.evidence[0] if parsed.evidence else None
        if item is not None:
            item = replace(item, identity_tokens=tuple(sorted(item.identity_tokens)),
                           sources=tuple(sorted(item.sources, key=lambda value: (
                               value.source_id, value.line_number, value.marker, value.line))))
        else:
            reject("evidence", index, "EVIDENCE_STRUCTURE_INVALID")
        if identity is not None:
            groups.setdefault(identity, []).append((index, item))

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
                raise MethodsValidationError(MethodsValidationReasonCode.CONFIRMED_MARKER_SCAN_MISS,
                    "method has no positive frozen marker scan")
            verify_method_evidence(first, skill=skill, by_source=by_source, source_lines=source_lines)
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

    confirmed = tuple(dict.fromkeys(item.method_id for item in retained))
    for index, method in enumerate(header.confirmed_methods):
        if method not in confirmed:
            reject("confirmed_methods", index, "CONFIRMED_EVIDENCE_MISSING")
    candidates = tuple(method for method in header.candidate_methods if method in methods and method not in confirmed)
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
        "registration_sha256": shared_audit.registration_sha256,
        "package_tree_sha256": shared_audit.package_tree_sha256,
        "logparse_receipt_sha256": logparse_receipt_sha256,
        "target_log_sha256": {item.source_id: item.content_sha256 for item in target_logs},
        "retained": retained_items, "merged": merged, "rejected": rejected,
        "missing_targets": list(missing_targets), "status": effective_status,
        "gap_messages": list(gaps),
    }
    return VerifiedMethodDiagnosisV1(effective, replace(shared_audit,
        status=effective_status, confirmed_methods=confirmed, evidence_count=len(retained)), selection)
