# Route output contract

Write exactly one UTF-8 JSON object to `output/job_outcome.draft.json`.
It must be a ROUTE `AgentJobOutcomeDraftV2` with exactly these twelve top-level
fields and no others:
`base_state_revision`, `case_id`, `consumed_evidence_refs`, `error`, `job_id`,
`job_type`, `payload`, `proposed_artifact_drafts`, `proposed_evidence_drafts`,
`result_type`, `rule_claims`, and `schema_version`.

Use these fixed values:

- `schema_version`: `2`
- `job_type`: `"ROUTE"`
- `consumed_evidence_refs`: `[]`
- `proposed_artifact_drafts`: `[]`
- `proposed_evidence_drafts`: `[]`
- `rule_claims`: `[]`
- `error`: `null`

Copy `job_id` and the positive integer `base_state_revision` exactly from
`JOB_INSTRUCTION`. Copy `case_id` exactly from `RESOURCE_MANIFEST`. A draft never
contains `outcome_id`, `produced_at`, `decision_audit`, or server-verification
fields.

Choose exactly one of these two branches:

1. A compatible Skill exists: set `result_type="COMPLETED"`. Set `payload` to an
   object with exactly `kind`, `skill_ref`, `reason`, and `confidence`. Set
   `kind="MATCHED"`; copy `skill_ref` exactly from one
   `SKILL_INDEX.skills[*].ref`; write a short non-empty `reason`; and set
   `confidence` to a JSON number from `0` through `1`.
2. No compatible Skill exists: set `result_type="NO_CAPABILITY"`. Use the same
   exact payload fields, with `kind="NO_CAPABILITY"`, `skill_ref=null`, a short
   non-empty `reason`, and `confidence` from `0` through `1`.

Do not emit `FAILED`, `REROUTE`, `NEED_INPUT`, `NEED_ATTACHMENT`, or
`INCONCLUSIVE`. Do not add fields to the payload or `skill_ref` object.

The complete `ref` object is the only valid source for Skill identity. Copy its `id`,
`version`, and 64-lowercase-hex-character `content_hash` without shortening,
deriving, or rewriting any value; never remove the `diagnosis-skill/` namespace
from `ref.id`. Before writing a match, verify `payload.skill_ref` is exactly equal to
one complete `SKILL_INDEX.skills[*].ref` object. Capability, summary, Skill
name, and other descriptive text are not identity aliases.

`SKILL_INDEX` contains every registered production Methods Skill whose immutable
identity and package structure passed server validation. The Router, not a
user-fact-name filter, decides semantic compatibility from the frozen problem and
capabilities. If the production catalog is empty, the service returns
`NO_CAPABILITY` without invoking you.

Never create a temporary file at workspace root; its direct children remain
exactly `inputs`, `runtime`, and `output`. Write the complete draft directly to
`output/job_outcome.draft.json`, then exit without invoking another tool or
editing the Workspace again. The service independently parses, validates,
Canonical-JSON-normalizes, and finalizes the draft after the Agent exits.
