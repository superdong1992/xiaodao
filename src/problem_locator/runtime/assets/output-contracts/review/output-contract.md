# Review output contract

Atomically replace `output/job_outcome.json` with a REVIEW `AgentJobOutcome` accepted by `schemas/v1/agent-job-outcome.schema.json`.

The exact schema from this installed runtime follows. It is authoritative for every nested field. The bytes between its BEGIN and END markers are one complete JSON document.

<<<BEGIN S00 AGENT JOB OUTCOME SCHEMA>>>
{{S00_AGENT_JOB_OUTCOME_SCHEMA_JSON}}
<<<END S00 AGENT JOB OUTCOME SCHEMA>>>

The top-level object has exactly these twelve fields and no others. Its authoritative code-point-sorted field-name set is `["base_state_revision","case_id","consumed_evidence_refs","error","job_id","job_type","outcome_id","payload","produced_at","proposed_artifact_drafts","proposed_evidence_drafts","result_type"]`. Copy `job_id`, `job_type`, and `base_state_revision` exactly from `JOB_INSTRUCTION`; copy `case_id` exactly from `RESOURCE_MANIFEST.case_id`. Generate `outcome_id` as a fresh lowercase UUID and `produced_at` as the current real UTC timestamp with exactly millisecond precision in `YYYY-MM-DDTHH:MM:SS.sssZ` form. Never omit nulls or empty arrays, never reuse the Job or Case ID as the Outcome ID, and never add shortcut fields outside the frozen `AgentJobOutcome` schema.

A non-failed REVIEW outcome always uses `result_type` `COMPLETED`, a non-null `ReviewAssessment` payload, and `error` null; `PASS`, `NEED_MORE_EVIDENCE`, and `REJECT` are payload verdicts, never result types. Its authoritative field-name set is `["candidate_conclusion_id","candidate_content_hash","candidate_revision","evidence_conflicts","missing_evidence","recommendation","reviewed_evidence_refs","reviewed_state_revision","stale_references","unsupported_findings","verdict"]`. `FAILED` requires `payload` null and a non-null error. Copy `candidate_conclusion_id`, `candidate_revision`, and `candidate_content_hash` exactly from `REVIEW_TARGET`, and set `reviewed_state_revision` to the current Job's `base_state_revision`. Every `reviewed_evidence_refs` entry must be fixed by the REVIEW Job. PASS must review every supporting Evidence reference of the fixed Candidate and is valid only with all four issue arrays empty. `NEED_MORE_EVIDENCE` requires a non-empty `missing_evidence` or `unsupported_findings` array; `REJECT` requires a non-empty `unsupported_findings`, `evidence_conflicts`, or `stale_references` array. REVIEW must not propose Evidence or Artifact drafts, so both proposal arrays are empty.

A `Write` call may create only a syntactically valid JSON draft at `output/job_outcome.json`; it is never the final publication step. Never create a temporary file at workspace root: throughout execution its direct children must remain exactly `inputs`, `runtime`, and `output`.

After the complete draft exists, the final Workspace-writing command must be exactly:

```text
problem-locator-finalize-outcome
```

The installed server tool refreshes `outcome_id` and `produced_at`, validates `AgentJobOutcome`, recursively sorts every nested object key, atomically publishes V1 Canonical JSON, and records a size/SHA-256 finalization marker. A non-zero exit is a Job failure that must be corrected before returning. Do not write or edit `output/job_outcome.json` after this command. Stdout, stderr, Markdown, and partial files are never business output.
