# Route output contract

Write one ROUTE `AgentJobOutcomeDraftV2` to
`output/job_outcome.draft.json`. The draft must be accepted by
`schemas/v2/agent-job-outcome-draft.schema.json`.

The exact installed schema follows. The bytes between the markers are one complete
JSON document and are authoritative for every nested field.

<<<BEGIN S00 AGENT JOB OUTCOME DRAFT SCHEMA>>>
{{S00_AGENT_JOB_OUTCOME_DRAFT_SCHEMA_JSON}}
<<<END S00 AGENT JOB OUTCOME DRAFT SCHEMA>>>

The top-level object has exactly these twelve fields and no others:
`["base_state_revision","case_id","consumed_evidence_refs","error","job_id","job_type","payload","proposed_artifact_drafts","proposed_evidence_drafts","result_type","rule_claims","schema_version"]`.

Set `schema_version=2`. Copy `job_id`, `job_type`, and `base_state_revision`
exactly from `JOB_INSTRUCTION`, and copy `case_id` exactly from
`RESOURCE_MANIFEST.case_id`. A draft never contains `outcome_id`, `produced_at`,
`decision_audit`, or any server-verification field.

For a `MATCHED` route, use `result_type=COMPLETED` and copy one compatible
`skill_ref` exactly from `SKILL_INDEX`. For `NO_CAPABILITY`, use
`result_type=NO_CAPABILITY` and `skill_ref=null`. ROUTE forbids `REROUTE`,
`NEED_INPUT`, `NEED_ATTACHMENT`, and `INCONCLUSIVE`. `FAILED` requires a null
payload and a non-null error; every non-failed draft requires `error=null`.
Every `SKILL_INDEX` entry has already passed exact identity matching against all
frozen user-fact names. If that filtering leaves no entry, the service publishes
`NO_CAPABILITY` deterministically without invoking the routing Agent.

ROUTE consumes and proposes nothing. Therefore `consumed_evidence_refs`, both
proposal arrays, and `rule_claims` are empty. Never create a temporary file at
workspace root; its direct children remain exactly `inputs`, `runtime`, and
`output`.

After the complete draft exists, the last command that modifies the Workspace must
be exactly:

```text
problem-locator-seal-outcome-draft
```

The sealer validates and Canonical-JSON-normalizes only the Agent draft, then writes
`runtime/tool-state/agent-job-outcome-draft.finalized`. It does not create an
Outcome ID, timestamp, decision audit, or final Outcome. Do not edit `output/`
after it succeeds. After the Agent process exits, the service independently creates
the authoritative `output/job_outcome.json`.
