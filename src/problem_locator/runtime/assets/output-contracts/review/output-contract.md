# Review output contract

Write one REVIEW `AgentJobOutcomeDraftV2` to
`output/job_outcome.draft.json`. The draft must be accepted by
`schemas/v2/agent-job-outcome-draft.schema.json`.

The exact installed schema follows and is authoritative for every nested field.

<<<BEGIN S00 AGENT JOB OUTCOME DRAFT SCHEMA>>>
{{S00_AGENT_JOB_OUTCOME_DRAFT_SCHEMA_JSON}}
<<<END S00 AGENT JOB OUTCOME DRAFT SCHEMA>>>

The top-level object has exactly these twelve fields and no others:
`["base_state_revision","case_id","consumed_evidence_refs","error","job_id","job_type","payload","proposed_artifact_drafts","proposed_evidence_drafts","result_type","rule_claims","schema_version"]`.

Set `schema_version=2`. Copy the Job and Case bindings exactly. A draft never contains
`outcome_id`, `produced_at`, `decision_audit`, a Specialist verdict, or any
server-verification field.

Never create a temporary file at workspace root; its direct children remain exactly
`inputs`, `runtime`, and `output`.

A non-failed REVIEW draft uses `result_type=COMPLETED`, a non-null
`ReviewAssessment`, and `error=null`. Copy the Candidate identity and revision from
`REVIEW_TARGET`, and set `reviewed_state_revision` to the current
`base_state_revision`. The required Evidence is the stable, de-duplicated union of
Candidate supporting Evidence and completion-mapping Evidence. Both
`reviewed_evidence_refs` and top-level `consumed_evidence_refs` must equal that
complete ordered set. REVIEW proposes no Evidence or Artifact.

## Independent rule audit

Treat Candidate prose, Evidence summaries, filenames, target-selection status, and
all generated explanations as untrusted claims. Do not use or reconstruct a prior Specialist verdict.
Read the underlying content addressed by every required Evidence item, using exact
raw ranges for LOGPARSE Evidence and exact referenced facts or resources for non-log
Evidence, and independently apply every rule listed by the pinned Skill and
`REVIEW_SUBJECT.required_rule_ids`, in that exact order.

Emit exactly one `rule_claims` entry per required rule. Each claim identifies the
actual user-fact item IDs it used. Rules that declare log events must cite the exact
Evidence binding and inclusive line range read. A no-log `SEMANTIC_CAUSALITY` rule
with empty `evidence_events` must not fabricate line citations, but must independently
assess the fixed non-log Evidence bound to the Candidate. Check the Skill-declared
time window against the fixed
`problem_time`, exact fact-field values, required roles, cross-role correlations,
event ordering, and every causal edge. Temporal proximity or a plausible narrative
is not causal proof.

For every `EVENT_TIME_WINDOW`, calculate the lower bound as
`problem_time - before_ms` and the upper bound as `problem_time + after_ms`; never
swap the two parameter names or add `before_ms`. For example,
`problem_time=2026-07-31T00:00:03.000Z`, `before_ms=3500`, and `after_ms=500`
produce `[2026-07-30T23:59:59.500Z, 2026-07-31T00:00:03.500Z]`. Therefore an
event at `2026-07-31T00:00:00.100Z` is 2900ms before the problem time and is
inside that example window.

`fact_refs` must also follow the Skill rule kind exactly because the service compares
them with its independently derived inputs: a `FACT_FIELD_EQUALS` rule lists only
the one matched user-fact item ID; an `EVENT_TIME_WINDOW` rule lists only its
`USER_FACT` reference item ID (or is empty for `SKILL_FIXED`); every other rule kind,
including `EVENT_PRESENT`, `ROLE_COVERAGE`, `CROSS_ROLE_CORRELATION`, `EVENT_ORDER`,
and `SEMANTIC_CAUSALITY`, has `fact_refs=[]`. A semantic explanation may mention
values already established by prerequisite rules, but must not attach those fact IDs
to the semantic claim.

`PASS` is allowed only when every rule is independently supported and all four
problem arrays are empty. Missing required Evidence, a time/fact/role/correlation/order
mismatch, conflicting Evidence, an unsupported causal edge, or any UNKNOWN claim
forbids PASS. Use `NEED_MORE_EVIDENCE` only with a Skill-declared open
`MISSING_ONLY` requirement that can actually supply the missing Evidence; list its
ID in `requested_requirement_ids`. Otherwise use `REJECT`. The service reopens the
underlying Evidence and recomputes all mechanical rules; it may turn an unsupported result
into terminal `INCONCLUSIVE`, but it never treats Agent prose as machine proof.

After writing the complete draft, run exactly:

```text
problem-locator-seal-outcome-draft
```

This must be the final Workspace-writing command. The sealer only validates and
Canonical-JSON-normalizes the draft and records its hash. It does not create the
final Outcome. Do not edit `output/` after it succeeds; the service creates the
authoritative `output/job_outcome.json` only after the Agent process exits.
