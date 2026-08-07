# Route output contract

Atomically replace `output/job_outcome.json` with a ROUTE `AgentJobOutcome` accepted by `schemas/v1/agent-job-outcome.schema.json`.

The exact schema from this installed runtime follows. It is authoritative for every nested field. The bytes between its BEGIN and END markers are one complete JSON document.

<<<BEGIN S00 AGENT JOB OUTCOME SCHEMA>>>
{{S00_AGENT_JOB_OUTCOME_SCHEMA_JSON}}
<<<END S00 AGENT JOB OUTCOME SCHEMA>>>

The top-level object has exactly these twelve fields and no others. Its authoritative code-point-sorted field-name set is `["base_state_revision","case_id","consumed_evidence_refs","error","job_id","job_type","outcome_id","payload","produced_at","proposed_artifact_drafts","proposed_evidence_drafts","result_type"]`. Copy `job_id`, `job_type`, and `base_state_revision` exactly from `JOB_INSTRUCTION`; copy `case_id` exactly from `RESOURCE_MANIFEST.case_id`. Generate `outcome_id` as a fresh lowercase UUID and `produced_at` as the current real UTC timestamp with exactly millisecond precision in `YYYY-MM-DDTHH:MM:SS.sssZ` form. Never omit nulls or empty arrays, never reuse the Job or Case ID as the Outcome ID, and never add shortcut fields such as top-level `decision`, `kind`, `skill_ref`, or `selected_skill_ref`.

For a `MATCHED` decision, set `result_type` to `COMPLETED`. Its non-null `payload` is exactly a `RouteDecision` object with the four fields whose authoritative field-name set is `["confidence","kind","reason","skill_ref"]`: set `kind` to `MATCHED`, use a finite JSON number from 0.0 through 1.0 for `confidence`, write a non-empty `reason`, and copy `skill_ref` exactly from `SKILL_INDEX.skills[i].ref` for one compatible entry. Copy only that nested `ref` object (`content_hash`, `id`, and `version`), never the enclosing skill-index entry. For a `NO_CAPABILITY` decision, set `result_type` to `NO_CAPABILITY`. Use the same four-field payload with `kind` `NO_CAPABILITY` and `skill_ref` null. `REROUTE` is forbidden for ROUTE jobs; it is a DIAGNOSE-only result. `NEED_INPUT` and `NEED_ATTACHMENT` are also forbidden for ROUTE jobs. The only other ROUTE result is `FAILED`, with `payload` null and a non-null error. Non-failed outcomes require `error` null. The service derives the selected Skill only after accepting the Outcome.

ROUTE consumes no Evidence and proposes no Evidence or Artifact drafts, so `consumed_evidence_refs`, `proposed_evidence_drafts`, and `proposed_artifact_drafts` must all be empty. A `Write` call may create only a syntactically valid JSON draft at `output/job_outcome.json`; it is never the final publication step. Never create a temporary file at workspace root: throughout execution its direct children must remain exactly `inputs`, `runtime`, and `output`.

After the complete draft exists, the final Workspace-writing command must be exactly:

```text
problem-locator-finalize-outcome
```

The installed server tool refreshes `outcome_id` and `produced_at`, validates `AgentJobOutcome`, recursively sorts every nested object key, atomically publishes V1 Canonical JSON, and records a size/SHA-256 finalization marker. A non-zero exit is a Job failure that must be corrected before returning. Do not write or edit `output/job_outcome.json` after this command. The job binding must match the current instruction exactly. Stdout, stderr, Markdown, and partial files are never business output.
