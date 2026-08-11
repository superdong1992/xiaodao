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
Evidence. Independently apply every rule listed by the pinned Skill and
`REVIEW_SUBJECT.required_rule_ids`, in that exact order, then select the first matching
Skill terminal path. Re-check event sets, bounded multi-line records, selectors,
cardinality, observation limits, typed values, units, clock domains, explicit
tolerances, joins, numeric expressions, and semantic causality.

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

`fact_refs` must equal the facts the service independently uses. List USER_FACT event
selectors first, then rule-owned facts: `FACT_FIELD_EQUALS`, `FACT_IN`, a USER_FACT
`EVENT_TIME_WINDOW` reference, and FACT nodes visited by `NUMERIC_COMPARE`; preserve
first-use order and de-duplicate. Claims that use none of these have `fact_refs=[]`.

`PASS` is allowed when the independently selected path is the Candidate's declared
`terminal_path_id`, its `COMPLETE|PARTIAL` resolution equals the Candidate, every
Candidate factor is supported by its bound rules and Evidence, and all four problem
arrays are empty. A PARTIAL PASS is not a complete root-cause claim: verified factors,
excluded factors and unresolved gaps must all be represented. Rules outside the
selected branch remain in the audit; do not omit an UNKNOWN, FAIL, or NOT_APPLICABLE
rule to manufacture a path. Missing required Evidence, a time/fact/role/correlation/order
mismatch, conflicting Evidence, or an unsupported causal edge forbids PASS. Use
`NEED_MORE_EVIDENCE` only with a Skill-declared open
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
