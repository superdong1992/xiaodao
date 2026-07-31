"""Pure projection of the current diagnosis state into a Job snapshot."""

from __future__ import annotations

from problem_locator.contracts import ContextSnapshot, DiagnosisState


class PureContextSnapshotProjector:
    """Project the complete frozen DiagnosisState view without I/O or mutation."""

    def project(self, target_diagnosis_state: DiagnosisState) -> ContextSnapshot:
        state = target_diagnosis_state
        return ContextSnapshot.model_validate(
            {
                "diagnosis_state_revision": state.revision,
                "problem_spec": state.problem_spec,
                "user_facts": state.user_facts,
                "confirmed_facts": state.confirmed_facts,
                "active_hypotheses": state.active_hypotheses,
                "rejected_hypotheses": state.rejected_hypotheses,
                "open_questions": state.open_questions,
                "pending_requirements": state.pending_requirements,
                "evidence_refs": state.evidence_refs,
                "candidate_conclusion": state.candidate_conclusion,
            }
        )


__all__ = ["PureContextSnapshotProjector"]
