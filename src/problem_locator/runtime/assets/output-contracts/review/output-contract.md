# Review output contract

Atomically replace `output/job_outcome.json` with a REVIEW `AgentJobOutcome` accepted by `schemas/v1/agent-job-outcome.schema.json`.

The exact schema from this installed runtime follows. It is authoritative for every nested field. The bytes between its BEGIN and END markers are one complete JSON document.

<<<BEGIN S00 AGENT JOB OUTCOME SCHEMA>>>
{{S00_AGENT_JOB_OUTCOME_SCHEMA_JSON}}
<<<END S00 AGENT JOB OUTCOME SCHEMA>>>

The top-level object has exactly these twelve fields and no others. Its authoritative code-point-sorted field-name set is `["base_state_revision","case_id","consumed_evidence_refs","error","job_id","job_type","outcome_id","payload","produced_at","proposed_artifact_drafts","proposed_evidence_drafts","result_type"]`. Copy `job_id`, `job_type`, and `base_state_revision` exactly from `JOB_INSTRUCTION`; copy `case_id` exactly from `RESOURCE_MANIFEST.case_id`. Generate `outcome_id` as a fresh lowercase UUID and `produced_at` as the current real UTC timestamp with exactly millisecond precision in `YYYY-MM-DDTHH:MM:SS.sssZ` form. Never omit nulls or empty arrays, never reuse the Job or Case ID as the Outcome ID, and never add shortcut fields outside the frozen `AgentJobOutcome` schema.

A non-failed REVIEW outcome always uses `result_type` `COMPLETED`, a non-null `ReviewAssessment` payload, and `error` null; `PASS`, `NEED_MORE_EVIDENCE`, and `REJECT` are payload verdicts, never result types. Its authoritative field-name set is `["candidate_conclusion_id","candidate_content_hash","candidate_revision","evidence_conflicts","missing_evidence","recommendation","reviewed_evidence_refs","reviewed_state_revision","stale_references","unsupported_findings","verdict"]`. `FAILED` requires `payload` null and a non-null error. Copy `candidate_conclusion_id`, `candidate_revision`, and `candidate_content_hash` exactly from `REVIEW_TARGET`, and set `reviewed_state_revision` to the current Job's `base_state_revision`. Every `reviewed_evidence_refs` entry must be fixed by the REVIEW Job. PASS must review every supporting Evidence reference of the fixed Candidate and is valid only with all four issue arrays empty. `NEED_MORE_EVIDENCE` requires a non-empty `missing_evidence` or `unsupported_findings` array; `REJECT` requires a non-empty `unsupported_findings`, `evidence_conflicts`, or `stale_references` array. REVIEW must not propose Evidence or Artifact drafts, so both proposal arrays are empty.

JSON Schema validity alone is insufficient: the final file must use V1 Canonical JSON bytes (UTF-8 without a BOM, code-point-sorted object keys at every nesting level, compact separators with no insignificant whitespace, no NaN or Infinity, and exactly one trailing LF). A `Write` tool call may append another LF, so it must never be the final operation on this file. Never create a temporary file at workspace root: throughout execution its direct children must remain exactly `inputs`, `runtime`, and `output`. After the complete JSON object exists, use Bash to run the installed Python and perform this final same-directory atomic canonicalization step; do not alter or skip it:

```python
import json, os, uuid
from datetime import UTC, datetime
from pathlib import Path
p = Path("output/job_outcome.json")
value = json.loads(p.read_text(encoding="utf-8"))
value["outcome_id"] = str(uuid.uuid4())
value["produced_at"] = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
canonical = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
assert canonical.endswith(b"\n") and not canonical.endswith(b"\n\n")
temporary = p.with_name("job_outcome.json.tmp")
with temporary.open("wb") as stream:
    stream.write(canonical)
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary, p)
assert p.read_bytes() == canonical
```

Validate the final bytes, not only the parsed value. Stdout, stderr, Markdown, and partial files are never business output.
