from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from problem_locator.contracts import (
    ApplicationPortError,
    ErrorCode,
    MethodEvaluationRoleV2,
    canonical_json_bytes,
    parse_canonical_json_bytes,
)
from problem_locator.domain.methods_state_v2 import start_method_state_v2
from problem_locator.runtime.methods_evidence_v2 import (
    build_method_evaluation_plan_v2,
    scan_method_evidence_v2,
)
from problem_locator.runtime.methods_grounding import FrozenTargetLogV1
from problem_locator.runtime.methods_records_v2 import (
    METHODS_EVALUATION_PLAN_V2_FILENAME,
    METHODS_EVIDENCE_GRAPH_V2_FILENAME,
    METHODS_LIMITATIONS_V2_FILENAME,
    METHODS_STATE_V2_FILENAME,
    build_method_limitations_record_v2,
    method_prompt_filename_v2,
    method_rejected_attempt_filename_v2,
    publish_method_evaluation_plan_v2,
    publish_method_evidence_graph_v2,
    publish_method_limitations_record_v2,
    publish_method_prompt_v2,
    publish_method_rejected_attempt_v2,
    publish_method_state_v2,
    read_method_evaluation_plan_v2,
    read_method_evidence_graph_v2,
    read_method_limitations_record_v2,
    read_method_prompt_v2,
    read_method_rejected_attempt_v2,
    read_method_state_v2,
)
from problem_locator.storage.coordination import StorageCoordinationLock
from problem_locator.storage.execution_records import FileExecutionRecordStore
from tests.deterministic.unit.runtime.methods_v2_test_support import (
    load_test_methods_skill,
)


JOB_ID = "00000000-0000-0000-0000-000000000071"
EVALUATION_ID = "00000000-0000-0000-0000-000000000072"
CASE_ID = "00000000-0000-0000-0000-000000000073"
REVIEW_JOB_ID = "00000000-0000-0000-0000-000000000074"


def _runtime_values(tmp_path: Path):
    skill = load_test_methods_skill(
        tmp_path / "skill-input",
        name="records-test",
        methods=(("records-method", "RECORDS_MARKER"),),
    )
    content = b"RECORDS_MARKER request_id=req-1\n"
    target = FrozenTargetLogV1(
        source_id="server",
        relative_path="logs/server.log",
        content_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )
    graph = scan_method_evidence_v2(skill=skill, target_logs=(target,))
    plan = build_method_evaluation_plan_v2(skill=skill, evidence=graph)
    state = start_method_state_v2(
        case_id=CASE_ID,
        source_job_id=JOB_ID,
        evaluation_id=EVALUATION_ID,
        plan=plan,
    )
    return graph, plan, state


def _store(tmp_path: Path) -> FileExecutionRecordStore:
    return FileExecutionRecordStore(tmp_path, StorageCoordinationLock())


def test_real_store_roundtrips_typed_core_records_and_all_prompt_names(
    tmp_path: Path,
) -> None:
    graph, plan, state = _runtime_values(tmp_path)
    records = _store(tmp_path)

    graph_ref = publish_method_evidence_graph_v2(records, job_id=JOB_ID, graph=graph)
    plan_ref = publish_method_evaluation_plan_v2(records, job_id=JOB_ID, plan=plan)
    state_ref = publish_method_state_v2(records, job_id=JOB_ID, state=state)

    assert graph_ref.relative_key.endswith(METHODS_EVIDENCE_GRAPH_V2_FILENAME)
    assert plan_ref.relative_key.endswith(METHODS_EVALUATION_PLAN_V2_FILENAME)
    assert state_ref.relative_key.endswith(METHODS_STATE_V2_FILENAME)
    assert read_method_evidence_graph_v2(records, job_id=JOB_ID) == graph
    assert read_method_evaluation_plan_v2(records, job_id=JOB_ID) == plan
    assert read_method_state_v2(records, job_id=JOB_ID) == state
    assert records.read_audit_bytes(
        JOB_ID, METHODS_EVIDENCE_GRAPH_V2_FILENAME
    ) == canonical_json_bytes(graph)
    assert records.read_audit_bytes(
        JOB_ID, METHODS_EVALUATION_PLAN_V2_FILENAME
    ) == canonical_json_bytes(plan)
    assert records.read_audit_bytes(
        JOB_ID, METHODS_STATE_V2_FILENAME
    ) == canonical_json_bytes(state)

    for role in ("SPECIALIST", "REVIEWER"):
        for attempt in ("PRIMARY", "REPAIR"):
            prompt = f"{role}:{attempt}\n".encode()
            ref = publish_method_prompt_v2(
                records,
                job_id=JOB_ID,
                role=role,
                attempt=attempt,
                prompt_bytes=prompt,
            )
            assert ref.relative_key.endswith(
                method_prompt_filename_v2(role=role, attempt=attempt)
            )
            assert (
                read_method_prompt_v2(
                    records,
                    job_id=JOB_ID,
                    role=role,
                    attempt=attempt,
                )
                == prompt
            )


@pytest.mark.parametrize("role", ["SPECIALIST", "REVIEWER"])
def test_primary_and_repair_rejections_are_append_only_and_do_not_overwrite(
    tmp_path: Path,
    role: MethodEvaluationRoleV2,
) -> None:
    records = _store(tmp_path)
    primary = b'{"primary":"rejected"}\n'
    repair = b'{"repair":"rejected"}\n'

    primary_ref = publish_method_rejected_attempt_v2(
        records,
        job_id=JOB_ID,
        role=role,
        attempt="PRIMARY",
        raw_bytes=primary,
    )
    repair_ref = publish_method_rejected_attempt_v2(
        records,
        job_id=JOB_ID,
        role=role,
        attempt="REPAIR",
        raw_bytes=repair,
    )

    assert primary_ref.relative_key != repair_ref.relative_key
    assert primary_ref.relative_key.endswith(
        method_rejected_attempt_filename_v2(
            role=role, attempt="PRIMARY"
        )
    )
    assert repair_ref.relative_key.endswith(
        method_rejected_attempt_filename_v2(
            role=role, attempt="REPAIR"
        )
    )
    assert read_method_rejected_attempt_v2(
        records,
        job_id=JOB_ID,
        role=role,
        attempt="PRIMARY",
    ) == primary
    assert read_method_rejected_attempt_v2(
        records,
        job_id=JOB_ID,
        role=role,
        attempt="REPAIR",
    ) == repair

    assert publish_method_rejected_attempt_v2(
        records,
        job_id=JOB_ID,
        role=role,
        attempt="PRIMARY",
        raw_bytes=primary,
    ) == primary_ref
    with pytest.raises(ApplicationPortError) as caught:
        publish_method_rejected_attempt_v2(
            records,
            job_id=JOB_ID,
            role=role,
            attempt="PRIMARY",
            raw_bytes=b"different",
        )
    assert caught.value.error.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert read_method_rejected_attempt_v2(
        records,
        job_id=JOB_ID,
        role=role,
        attempt="PRIMARY",
    ) == primary
    assert read_method_rejected_attempt_v2(
        records,
        job_id=JOB_ID,
        role=role,
        attempt="REPAIR",
    ) == repair


def test_typed_read_rejects_one_field_mutated_from_a_production_graph(
    tmp_path: Path,
) -> None:
    graph, _, _ = _runtime_values(tmp_path)
    records = _store(tmp_path)
    value = graph.model_dump(mode="json")
    value["skill_sha256"] = "5" * 64
    mutated = canonical_json_bytes(value)
    records.publish_audit_bytes(JOB_ID, METHODS_EVIDENCE_GRAPH_V2_FILENAME, mutated)

    with pytest.raises(ValidationError):
        read_method_evidence_graph_v2(records, job_id=JOB_ID)

    assert parse_canonical_json_bytes(mutated)["graph_ref"] == graph.graph_ref


def test_real_store_freezes_deduplicated_limitations_for_the_reviewer_job(
    tmp_path: Path,
) -> None:
    graph, plan, _ = _runtime_values(tmp_path)
    records = _store(tmp_path)
    record = build_method_limitations_record_v2(
        case_id=CASE_ID,
        source_job_id=JOB_ID,
        graph=graph,
        plan=plan,
        limitations=(
            "Only the frozen server log was evaluated.",
            "Only the frozen server log was evaluated.",
            "No client log was supplied.",
        ),
    )

    ref = publish_method_limitations_record_v2(
        records,
        job_id=JOB_ID,
        record=record,
    )
    reviewer_read = read_method_limitations_record_v2(
        records,
        job_id=JOB_ID,
    )

    assert record.limitations == (
        "Only the frozen server log was evaluated.",
        "No client log was supplied.",
    )
    assert ref.relative_key.endswith(METHODS_LIMITATIONS_V2_FILENAME)
    assert reviewer_read == record
    assert records.read_audit_bytes(
        JOB_ID,
        METHODS_LIMITATIONS_V2_FILENAME,
    ) == canonical_json_bytes(record)
    assert publish_method_limitations_record_v2(
        records,
        job_id=JOB_ID,
        record=record,
    ) == ref
    with pytest.raises(ValueError, match="source Job"):
        publish_method_limitations_record_v2(
            records,
            job_id=REVIEW_JOB_ID,
            record=record,
        )


def test_limitations_read_rejects_one_field_mutated_from_production_record(
    tmp_path: Path,
) -> None:
    graph, plan, _ = _runtime_values(tmp_path)
    record = build_method_limitations_record_v2(
        case_id=CASE_ID,
        source_job_id=JOB_ID,
        graph=graph,
        plan=plan,
        limitations=("Only the frozen server log was evaluated.",),
    )
    value = record.model_dump(mode="json")
    value["limitations"].append(value["limitations"][0])
    records = _store(tmp_path)
    records.publish_audit_bytes(
        JOB_ID,
        METHODS_LIMITATIONS_V2_FILENAME,
        canonical_json_bytes(value),
    )

    with pytest.raises(ValidationError):
        read_method_limitations_record_v2(records, job_id=JOB_ID)
