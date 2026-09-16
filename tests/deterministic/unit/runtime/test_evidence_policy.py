"""Private audit mode is explicit for new jobs and strict for historic jobs."""
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from problem_locator.contracts import Job, JobType, bytes_sha256, canonical_json_bytes
from problem_locator.runtime.diagnosis_runtime import DiagnosisRuntime, _method_grounding_audit_from_bytes
from problem_locator.runtime.methods_advisory import accept_method_diagnosis_advisory
from problem_locator.runtime.methods_grounding import verify_method_diagnosis
from problem_locator.runtime.methods_selection import diagnosis_mapping
from tests.deterministic.unit.runtime.test_methods_advisory import _mapped
from tests.deterministic.unit.runtime.test_methods_output_pipeline import _contract
from tests.deterministic.unit.runtime.test_methods_selection import _input


def test_historical_audit_without_evidence_mode_preserves_strict_policy():
    verified = verify_method_diagnosis(**_input())
    historical = asdict(verified.audit)
    historical.pop("validation_mode")
    loaded = _method_grounding_audit_from_bytes(canonical_json_bytes(historical))
    assert loaded == verified.audit and loaded.validation_mode == "strict"


def test_advisory_audit_mode_survives_private_serialization_for_review():
    verified = accept_method_diagnosis_advisory(**_input())
    loaded = _method_grounding_audit_from_bytes(canonical_json_bytes(asdict(verified.audit)))
    assert loaded == verified.audit and loaded.validation_mode == "advisory"


@pytest.mark.parametrize("mode", [None, "", "disabled", False])
def test_invalid_private_mode_cannot_silently_disable_checks(mode):
    audit = asdict(verify_method_diagnosis(**_input()).audit)
    audit["validation_mode"] = mode
    with pytest.raises(ValueError):
        _method_grounding_audit_from_bytes(canonical_json_bytes(audit))


def _prior_review():
    args = _input()
    logparse_receipt = canonical_json_bytes({"schema_version": 1, "receipt": "frozen-inputs"})
    args["logparse_receipt_sha256"] = bytes_sha256(logparse_receipt)
    diagnosis_job, accepted, mapped = _mapped(args)
    source_bytes = canonical_json_bytes(args["draft"])
    assert mapped.verification.audit.source_draft_sha256 == bytes_sha256(source_bytes)
    files = {
        "method-diagnosis.draft.json": source_bytes,
        "method-grounding-audit.json": canonical_json_bytes(asdict(accepted.audit)),
        "methods_logparse_receipt.json": logparse_receipt,
        "method-evidence-advisory.json": canonical_json_bytes(accepted.advisory),
        "method-diagnosis.effective.json": canonical_json_bytes(diagnosis_mapping(accepted.draft)),
    }
    job = _contract("job-review.json", Job)
    outcome = SimpleNamespace(job_type=JobType.DIAGNOSE, job_id=diagnosis_job.job_id,
                              decision_audit=mapped.verification.audit)
    aggregate = SimpleNamespace(outcomes={job.previous_outcome_refs[0]: outcome})

    def read(job_id, filename):
        assert job_id == diagnosis_job.job_id
        return files.get(filename)

    runtime = object.__new__(DiagnosisRuntime)
    runtime._execution_records = SimpleNamespace(read_audit_bytes=read)
    runtime._diagnose_backend = Mock()
    runtime._execute_backend = Mock(side_effect=AssertionError("Reviewer must not run during prior-input validation"))
    return runtime, job, aggregate, args["skill"], files, accepted


def test_prior_advisory_draft_is_loaded_only_after_authority_and_receipt_hashes_agree():
    runtime, job, aggregate, skill, files, accepted = _prior_review()
    prior, effective_bytes, audit_bytes = runtime._prior_methods_diagnosis(job, aggregate, skill)
    assert prior.draft == accepted.draft and prior.audit == accepted.audit
    assert effective_bytes == files["method-diagnosis.effective.json"]
    assert audit_bytes == files["method-grounding-audit.json"]
    runtime._execute_backend.assert_not_called()
    runtime._diagnose_backend.execute.assert_not_called()


@pytest.mark.parametrize("tamper", [
    "effective_limitations", "source_summary", "receipt_source_hash",
    "receipt_effective_hash", "missing_receipt",
])
def test_prior_advisory_input_tampering_is_rejected_before_any_reviewer_call(tamper):
    runtime, job, aggregate, skill, files, _ = _prior_review()
    if tamper == "effective_limitations":
        key = "method-diagnosis.effective.json"
        value = json.loads(files[key])
        value["limitations"] = ["Changed after the diagnosis was persisted."]
        files[key] = canonical_json_bytes(value)
    elif tamper == "source_summary":
        key = "method-diagnosis.draft.json"
        value = json.loads(files[key])
        value["evidence"][0]["summary"] = "Changed original model output."
        files[key] = canonical_json_bytes(value)
    elif tamper == "missing_receipt":
        files.pop("method-evidence-advisory.json")
    else:
        key = "method-evidence-advisory.json"
        value = json.loads(files[key])
        field = "source_draft_sha256" if tamper == "receipt_source_hash" else "effective_draft_sha256"
        value[field] = "b" * 64
        files[key] = canonical_json_bytes(value)
    with pytest.raises(ValueError, match="prior .* identity is invalid"):
        runtime._prior_methods_diagnosis(job, aggregate, skill)
    runtime._execute_backend.assert_not_called()
    runtime._diagnose_backend.execute.assert_not_called()
