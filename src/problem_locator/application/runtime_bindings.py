"""Internal validation for pinned runtime bindings copied through Coordinator plans."""

from __future__ import annotations

from problem_locator.contracts import (
    DiagnosisMode,
    JobSpec,
    JobType,
    RuntimeBindings,
    VersionedRef,
    default_resource_limits,
)


def runtime_bindings_match_role(
    job_type: JobType,
    bindings: RuntimeBindings | None,
    *,
    expected_skill_ref: VersionedRef | None,
) -> bool:
    if not isinstance(bindings, RuntimeBindings):
        return False
    invalid = bindings.resource_limits != default_resource_limits(job_type)
    if job_type is JobType.ROUTE:
        invalid = invalid or any(
            value is not None
            for value in (
                bindings.diagnosis_mode,
                bindings.generic_skill_name,
                bindings.skill_ref,
                bindings.logparse_tool_ref,
                bindings.logparse_product,
            )
        )
    elif job_type is JobType.REVIEW:
        invalid = (
            invalid
            or bool(bindings.available_skill_refs)
            or bindings.skill_ref is None
            or bindings.skill_ref != expected_skill_ref
            or bindings.diagnosis_mode is not None
            or bindings.generic_skill_name is not None
        )
        invalid = invalid or any(
            value is not None
            for value in (
                bindings.logparse_tool_ref,
                bindings.logparse_product,
            )
        )
    elif bindings.diagnosis_mode is DiagnosisMode.GENERIC:
        invalid = invalid or (
            bool(bindings.available_skill_refs)
            or bindings.skill_ref is not None
            or expected_skill_ref is not None
            or bindings.generic_skill_name is None
            or bindings.logparse_tool_ref is not None
            or bindings.logparse_product is not None
        )
    else:
        invalid = (
            invalid
            or bindings.diagnosis_mode is not DiagnosisMode.SPECIALIZED
            or bindings.generic_skill_name is not None
            or bool(bindings.available_skill_refs)
            or bindings.skill_ref is None
            or bindings.skill_ref != expected_skill_ref
        )
    return not invalid


def rebuild_runtime_bindings_for_role(
    job_type: JobType,
    bindings: RuntimeBindings,
    *,
    expected_skill_ref: VersionedRef | None,
) -> RuntimeBindings:
    """Strictly rebuild a Catalog success and validate its requested role."""

    if not isinstance(bindings, RuntimeBindings):
        raise TypeError("runtime bindings must use the frozen DTO")
    rebuilt = RuntimeBindings.model_validate(
        bindings.model_dump(mode="python", warnings=False),
        strict=True,
    )
    if not runtime_bindings_match_role(
        job_type,
        rebuilt,
        expected_skill_ref=expected_skill_ref,
    ):
        raise ValueError("runtime bindings do not match their requested role")
    return rebuilt


def runtime_bindings_from_job_spec(spec: JobSpec) -> RuntimeBindings:
    return RuntimeBindings(
        diagnosis_mode=spec.diagnosis_mode,
        review_policy=spec.review_policy,
        generic_skill_name=spec.generic_skill_name,
        agent_profile_ref=spec.agent_profile_ref,
        available_skill_refs=list(spec.available_skill_refs),
        skill_ref=spec.skill_ref,
        tool_bundle_ref=spec.tool_bundle_ref,
        context_policy_ref=spec.context_policy_ref,
        output_contract_ref=spec.output_contract_ref,
        logparse_tool_ref=spec.logparse_tool_ref,
        logparse_product=spec.logparse_product,
        resource_limits=spec.resource_limits,
    )


__all__ = [
    "rebuild_runtime_bindings_for_role",
    "runtime_bindings_from_job_spec",
    "runtime_bindings_match_role",
]
