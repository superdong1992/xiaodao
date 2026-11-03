"""Product-line context remains optional and preserves historical payloads."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from problem_locator.contracts import Case, CaseAggregate, CreateCase, Job, JobType, StateFile
from problem_locator.contracts.limits import default_resource_limits
from problem_locator.contracts.serialization import (
    business_request_preimage, business_request_sha256, canonical_json_bytes,
)
from tests.deterministic.contracts._support import FIXTURE_ROOT, load_json
from tests.deterministic.contracts.test_command_hash_and_patch import _problem_spec_input


def _command(**extra):
    return CreateCase(idempotency_key="product-line-case", raw_problem_text="设备偶发重启",
        problem_spec=_problem_spec_input(), initial_user_facts=[], wait_seconds=0, **extra)


def test_product_line_preserves_legacy_create_case_hash_and_distinguishes_values():
    legacy = _command()
    explicit_null = _command(product_line=None)
    assert legacy.product_line is None
    assert "product_line" not in business_request_preimage(explicit_null)
    assert canonical_json_bytes(legacy) == canonical_json_bytes(explicit_null)
    assert business_request_sha256(legacy) == business_request_sha256(explicit_null)
    first = _command(product_line="交换机")
    second = _command(product_line="路由器")
    assert business_request_preimage(first)["product_line"] == "交换机"
    assert len({business_request_sha256(value) for value in (legacy, first, second)}) == 3


def test_product_line_old_state_and_jobs_roundtrip_without_new_null_fields():
    raw = load_json(FIXTURE_ROOT / "positive" / "state.json")
    state = StateFile.model_validate(raw)
    for aggregate in state.cases.values():
        assert aggregate.case.product_line is None
        assert "product_line" not in aggregate.case.model_dump()
        for job in aggregate.jobs.values():
            assert job.generic_product_line is None
            assert "generic_product_line" not in job.model_dump()
    assert StateFile.model_validate_json(state.model_dump_json()) == state


@pytest.mark.parametrize("value", [42, True, [], {"label": "交换机"}, "\ud800"])
def test_product_line_rejects_invalid_text(value):
    with pytest.raises(ValidationError):
        _command(product_line=value)


@pytest.mark.parametrize("name", ["job-route.json", "job-diagnose.json", "job-review.json"])
def test_product_line_is_forbidden_on_non_generic_jobs(name):
    raw = load_json(FIXTURE_ROOT / "positive" / name)
    raw["generic_product_line"] = "交换机"
    with pytest.raises(ValidationError, match="product.line.*GENERIC|GENERIC.*product.line"):
        Job.model_validate(raw)


def test_product_line_case_roundtrip_preserves_exact_string():
    state = StateFile.model_validate(load_json(FIXTURE_ROOT / "positive" / "state.json"))
    case = next(iter(state.cases.values())).case
    changed = Case.model_validate({**case.model_dump(), "product_line": "  数据中心交换机 α  "})
    assert Case.model_validate_json(changed.model_dump_json()).product_line == "  数据中心交换机 α  "


@pytest.mark.parametrize("job_product_line", [None, "路由器", "交换机"])
def test_product_line_generic_job_must_match_its_case(job_product_line):
    state = load_json(FIXTURE_ROOT / "positive" / "state.json")
    aggregate = next(iter(state["cases"].values()))
    job = load_json(FIXTURE_ROOT / "positive" / "job-route.json")
    job.update(job_type="DIAGNOSE", diagnosis_mode="GENERIC", review_policy=None,
        generic_skill_name="generic-problem-locator-smoke", generic_product_line=job_product_line,
        generic_problem_text=aggregate["case"]["raw_problem_text"], context_snapshot=None,
        evidence_refs=[], attachment_refs=[], previous_outcome_refs=[], artifact_refs=[],
        available_skill_refs=[], skill_ref=None, logparse_tool_ref=None, logparse_product=None,
        resource_limits=default_resource_limits(JobType.DIAGNOSE))
    aggregate["jobs"] = {job["job_id"]: job}
    aggregate["case"].update(active_job_id=job["job_id"], selected_skill_ref=None, product_line="交换机")
    if job_product_line != "交换机":
        with pytest.raises(ValidationError, match="GENERIC Job product line must equal"):
            CaseAggregate.model_validate(aggregate)
    else:
        restored = CaseAggregate.model_validate_json(CaseAggregate.model_validate(aggregate).model_dump_json())
        assert restored.jobs[job["job_id"]].generic_product_line == restored.case.product_line == "交换机"
