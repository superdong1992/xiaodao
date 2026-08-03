# Route output contract

Atomically replace `output/job_outcome.json` with a ROUTE `AgentJobOutcome` accepted by `schemas/v1/agent-job-outcome.schema.json`.

The exact schema from this installed runtime follows. It is authoritative for every nested field. The bytes between its BEGIN and END markers are one complete JSON document.

<<<BEGIN S00 AGENT JOB OUTCOME SCHEMA>>>
{{S00_AGENT_JOB_OUTCOME_SCHEMA_JSON}}
<<<END S00 AGENT JOB OUTCOME SCHEMA>>>

The top-level object has exactly these twelve fields and no others. Its authoritative code-point-sorted field-name set is `["base_state_revision","case_id","consumed_evidence_refs","error","job_id","job_type","outcome_id","payload","produced_at","proposed_artifact_drafts","proposed_evidence_drafts","result_type"]`. Copy `job_id`, `job_type`, and `base_state_revision` exactly from `JOB_INSTRUCTION`; copy `case_id` exactly from `RESOURCE_MANIFEST.case_id`. Generate `outcome_id` as a fresh lowercase UUID and `produced_at` as the current real UTC timestamp with exactly millisecond precision in `YYYY-MM-DDTHH:MM:SS.sssZ` form. Never omit nulls or empty arrays, never reuse the Job or Case ID as the Outcome ID, and never add shortcut fields such as top-level `decision`, `kind`, `skill_ref`, or `selected_skill_ref`.

For a `MATCHED` decision, set `result_type` to `COMPLETED`. Its non-null `payload` is exactly a `RouteDecision` object with the four fields whose authoritative field-name set is `["confidence","kind","reason","skill_ref"]`: set `kind` to `MATCHED`, use a finite JSON number from 0.0 through 1.0 for `confidence`, write a non-empty `reason`, and copy `skill_ref` exactly from `SKILL_INDEX.skills[i].ref` for one compatible entry. Copy only that nested `ref` object (`content_hash`, `id`, and `version`), never the enclosing skill-index entry. For a `NO_CAPABILITY` decision, set `result_type` to `NO_CAPABILITY`. Use the same four-field payload with `kind` `NO_CAPABILITY` and `skill_ref` null. `REROUTE` is forbidden for ROUTE jobs; it is a DIAGNOSE-only result. `NEED_INPUT` and `NEED_ATTACHMENT` are also forbidden for ROUTE jobs. The only other ROUTE result is `FAILED`, with `payload` null and a non-null error. Non-failed outcomes require `error` null. The service derives the selected Skill only after accepting the Outcome.

ROUTE consumes no Evidence and proposes no Evidence or Artifact drafts, so `consumed_evidence_refs`, `proposed_evidence_drafts`, and `proposed_artifact_drafts` must all be empty. JSON Schema validity alone is insufficient: the final file must use V1 Canonical JSON bytes (UTF-8 without a BOM, code-point-sorted object keys at every nesting level, compact separators with no insignificant whitespace, no NaN or Infinity, and exactly one trailing LF). A `Write` tool call may append another LF, so it must never be the final operation on this file. Never create a temporary file at workspace root: throughout execution its direct children must remain exactly `inputs`, `runtime`, and `output`. After the complete JSON object exists, use Bash to run the installed Python and perform this final same-directory atomic canonicalization step; do not alter or skip it:

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

Validate the final bytes, not only the parsed value. The job binding must match the current instruction exactly. Stdout, stderr, Markdown, and partial files are never business output.
