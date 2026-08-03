# Diagnose output contract

Atomically replace `output/job_outcome.json` with a DIAGNOSE `AgentJobOutcome` accepted by `schemas/v1/agent-job-outcome.schema.json`.

Before the first tool call, preserve the workspace root exactly as supplied. Its
only direct children are `inputs`, `runtime`, and `output`; the root is
intentionally non-writable during Agent execution. Never run `chmod` on it and
never create `err.txt`, stdout/stderr captures, scratch files, or directories
beside those three roots. Put transient command diagnostics only below `/tmp`
and final business output only below `output/`. In particular, invoke Logparse
broker helpers without a root-relative stderr redirection; a broker rejection
ends the Job under the fixed failure contract and is never debugged by creating
workspace-root files.

The exact schemas from this installed runtime follow. They are authoritative for every nested field. The bytes between each BEGIN and END marker are one complete JSON document.

<<<BEGIN S00 AGENT JOB OUTCOME SCHEMA>>>
{{S00_AGENT_JOB_OUTCOME_SCHEMA_JSON}}
<<<END S00 AGENT JOB OUTCOME SCHEMA>>>

<<<BEGIN S00 USER RESULT SCHEMA>>>
{{S00_USER_RESULT_SCHEMA_JSON}}
<<<END S00 USER RESULT SCHEMA>>>

The top-level object has exactly these twelve fields and no others. Its authoritative code-point-sorted field-name set is `["base_state_revision","case_id","consumed_evidence_refs","error","job_id","job_type","outcome_id","payload","produced_at","proposed_artifact_drafts","proposed_evidence_drafts","result_type"]`. Copy `job_id`, `job_type`, and `base_state_revision` exactly from `JOB_INSTRUCTION`; copy `case_id` exactly from `RESOURCE_MANIFEST.case_id`. Generate `outcome_id` as a fresh lowercase UUID and `produced_at` as the current real UTC timestamp with exactly millisecond precision in `YYYY-MM-DDTHH:MM:SS.sssZ` form. Never omit nulls or empty arrays, never reuse the Job or Case ID as the Outcome ID, and never add shortcut fields outside the frozen `AgentJobOutcome` schema.

A non-failed DIAGNOSE outcome requires a non-null `DiagnosisOutcome` payload and `error` null. Its authoritative field-name set is `["candidate_conclusion_draft","findings","recommended_next_step","requested_attachments","requested_input","state_delta"]`. The nested `state_delta` is a complete `DiagnosisStateDelta` whose authoritative field-name set is `["add_active_hypotheses","add_evidence_bindings","add_open_questions","add_pending_requirements","add_user_facts","fulfill_requirements","problem_spec_patch","proposed_facts","reject_hypotheses","resolve_questions","update_hypotheses"]`; every one of these fields is present even when its value is null or an empty array. `FAILED` requires `payload` null and a non-null error. For `NEED_INPUT`, `requested_input` must be non-empty and `requested_attachments` must be empty. For `NEED_ATTACHMENT`, `requested_attachments` must be non-empty and `requested_input` must be empty. For `COMPLETED` and `REROUTE`, both requested-requirement arrays must be empty. Every requested ID must identify a matching OPEN requirement already in `CONTEXT_SNAPSHOT` or added by this Outcome. Every new requirement must be OPEN, have `requested_by_job_id` equal to the current Job ID, and have empty `fulfilled_by_refs`. Agent outcomes must leave `state_delta.add_user_facts` and `state_delta.fulfill_requirements` empty.

Apply this deterministic group-A branch before the Skill narrative or any other diagnostic work. The required group-A names, in canonical workflow order, are exactly `caller_service`, `server_service`, `rpc_method`, and `problem_time`. If `CONTEXT_SNAPSHOT.user_facts` does not contain an active value for one or more of those exact names, return `NEED_INPUT` and request only the missing group-A names. Reuse the requirement ID of an existing matching OPEN `INPUT` requirement; otherwise add exactly one OPEN `INPUT` requirement for that missing name, with `required=true`, a concise non-empty prompt, a fresh lowercase UUID `requirement_id`, `requested_by_job_id` copied from `JOB_INSTRUCTION.job_id`, and empty `fulfilled_by_refs`. Set `requested_input` to exactly those missing group-A requirement IDs and set `requested_attachments=[]`. For a clean Case whose `user_facts` is empty, this branch therefore adds and requests exactly four requirements with the name set `["caller_service","problem_time","rpc_method","server_service"]`. It must not add or request `order_id`, `log_archive`, or any other requirement. Set `findings=[]`, `candidate_conclusion_draft=null`, `recommended_next_step` to a short request for the missing group-A values, and every other state-delta field to its schema-prescribed empty array or null value. Set both proposal-draft arrays and `consumed_evidence_refs` to `[]`; do not add Evidence, proposals, facts, hypotheses, questions, attachments, or a conclusion in this branch. Only after this branch is inapplicable may the following pre-Logparse branch run.

Apply this deterministic pre-Logparse branch before doing any other diagnostic work. If `CONTEXT_SNAPSHOT.user_facts` contains active values for all four names `caller_service`, `server_service`, `rpc_method`, and `problem_time`; `RESOURCE_MANIFEST.entries` contains no `ATTACHMENT` and no `LOGPARSE_RUN`; and there is no OPEN `log_archive` requirement, return `NEED_ATTACHMENT`. Add exactly one OPEN requirement named `log_archive`, with `kind` equal to `ATTACHMENT`, `required=true`, a concise non-empty upload prompt, a fresh lowercase UUID `requirement_id`, `requested_by_job_id` copied from `JOB_INSTRUCTION.job_id`, empty `fulfilled_by_refs`, and attachment constraints whose complete values are `allowed_content_types=["application/gzip","application/zip","application/x-tar"]`, `min_count=1`, and `max_count=1`. Set `requested_attachments` to exactly that new requirement ID and `requested_input` to `[]`. Set `findings=[]`, `candidate_conclusion_draft=null`, `recommended_next_step` to a short request to upload the log archive, and every other state-delta field to its schema-prescribed empty array or null value. Set both proposal-draft arrays and `consumed_evidence_refs` to `[]`; do not add Evidence, proposals, facts, hypotheses, questions, or a conclusion in this branch.

Apply this deterministic first-Logparse continuation branch after the single successful parse whenever the Job started with one fixed `ATTACHMENT`, no fixed `LOGPARSE_RUN`, and no active `order_id` in `CONTEXT_SNAPSHOT.user_facts`. The parameter is still missing even if a parsed log line contains an order-like value: only an active user fact whose provenance `input_name` is exactly `order_id` can satisfy parameter B. Return `NEED_INPUT`; add or reuse exactly one OPEN `INPUT` requirement named `order_id`, and set `requested_input` to exactly its requirement ID and `requested_attachments=[]`. Preserve exactly one client LOGPARSE Evidence draft with proposal key `logparse-client-evidence` and the one `LOGPARSE_RUN` Artifact draft produced by this Job, but do not propose server Evidence, a Candidate, or a `USER_RESULT`. Set `candidate_conclusion_draft=null`. Set `state_delta.add_evidence_bindings` to exactly `[{"existing_evidence_id":null,"evidence_proposal_key":"logparse-client-evidence"}]`; without this StateDelta binding the Case will discard both the Evidence and its dependent run before `WAITING_INPUT`. A same-Outcome LOGPARSE Evidence that only locates a file inside the new run must bind `artifact_proposal_key="logparse-run"` and use `workspace_relative_path=null`, `declared_size=null`, and `declared_sha256=null`; referenced log-file bytes are not an independently proposed Evidence resource. The LOGPARSE_RUN draft must use `artifact_kind="LOGPARSE_RUN"`, `content_type="application/vnd.problem-locator.logparse-run+directory"`, `resource_kind="DIRECTORY"`, and `workspace_relative_path="output/proposals/logparse-run/tree"`. Its `declared_sha256` and metadata `tree_manifest_sha256` are the SHA-256 of the complete S00 canonical `TreeManifest` bytes for every regular file under that tree, not the hash of `parse_manifest.json`; its `declared_size` is the sum of those file sizes.

Apply this deterministic accepted-run branch whenever `RESOURCE_MANIFEST.entries` contains one fixed `LOGPARSE_RUN` and `CONTEXT_SNAPSHOT.user_facts` contains an active `order_id`. Never call `parse-targets`, never propose another `LOGPARSE_RUN`, and never re-propose the fixed client Evidence. Call only `target-logs` with the manifest Artifact ID. Consume exactly the one fixed client LOGPARSE Evidence ID and propose exactly one new server LOGPARSE Evidence with proposal key `logparse-server-evidence`; bind it with `existing_source_ref` equal to the fixed LOGPARSE_RUN Artifact ID and `artifact_proposal_key=null`, and set its `workspace_relative_path`, `declared_size`, and `declared_sha256` to null. Set `state_delta.add_evidence_bindings` to exactly the server proposal binding. The Candidate and USER_RESULT supporting and completion-criterion bindings must have the fixed client Evidence ID first and the server Evidence proposal key second. The only Artifact draft in this branch is the required `USER_RESULT`.

Immediately before the accepted-run `target-logs` call, normalize and type-check
its request with the installed runtime by running the exact code below. This is
mandatory even if the file looks compact or a prior command described it as
canonical. Do not invoke the client until the sentinel prints. Do not rewrite
the request afterward, and invoke the client exactly once.

```python
import json
from pathlib import Path
from problem_locator.contracts import canonical_json_bytes, parse_canonical_json_bytes
from problem_locator.integrations.logparse import TargetLogsRequest

p = Path("output/proposals/logparse-server-evidence/request.json")
request = TargetLogsRequest.model_validate(json.loads(p.read_text(encoding="utf-8")))
canonical_request = canonical_json_bytes(request)
p.write_bytes(canonical_request)
assert canonical_request.endswith(b"\n") and not canonical_request.endswith(b"\n\n")
assert parse_canonical_json_bytes(p.read_bytes(), TargetLogsRequest) == request
print("TARGET_LOGS_REQUEST_SELF_CHECK_PASSED")
```

After that sentinel, use exactly:

```text
problem-locator-logparse target-logs --request output/proposals/logparse-server-evidence/request.json --result output/proposals/logparse-server-evidence/target_logs.json
```

Any nonzero client result ends the Job without retrying, editing the request, or
creating diagnostic files in the Workspace.

Every `PREVIOUS_OUTCOME` section is a post-staging persisted `JobOutcome` supplied only for continuity and evidence. Never copy its top-level `outcome_id`, `produced_at`, `proposed_artifacts`, or `proposed_evidence` fields into the current file: the current `AgentJobOutcome` must have a freshly generated Outcome ID, a timestamp obtained during the current execution, and the schema-required `proposed_artifact_drafts` and `proposed_evidence_drafts` fields. Before exiting, reopen the final `output/job_outcome.json` bytes and verify the exact twelve-field set, current Job bindings, branch invariants, and canonical encoding against the schemas above.

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

Do not trust a prose summary of the file. After canonicalization, run the installed
Python below as the final tool action. If any assertion or model validation fails,
repair the Agent-authored file and rerun this check until it prints the success
sentinel. This check validates the result; it does not generate or inject an Outcome.

```python
import hashlib, json, re
from pathlib import Path
from problem_locator.contracts import (
    AgentJobOutcome, TreeManifest, TreeManifestEntry, UserResultPayload,
    canonical_json_bytes,
)

root = Path(".")
raw = (root / "output/job_outcome.json").read_bytes()
value = json.loads(raw)
assert raw == canonical_json_bytes(value)
AgentJobOutcome.model_validate(value)
context = (root / "runtime/context.txt").read_text(encoding="utf-8")

def section(label):
    matches = re.findall(
        rf"(?ms)^<<<SECTION \d+ {re.escape(label)}>>>\n(.*?)\n<<<END SECTION>>>$",
        context,
    )
    assert len(matches) == 1
    return json.loads(matches[0])

job = section("JOB_INSTRUCTION")
snapshot = section("CONTEXT_SNAPSHOT")
manifest = section("RESOURCE_MANIFEST")
assert value["job_id"] == job["job_id"]
assert value["job_type"] == job["job_type"]
assert value["base_state_revision"] == job["base_state_revision"]
assert value["case_id"] == manifest["case_id"]

entries = manifest["entries"]
first_log = (
    sum(item["input_kind"] == "ATTACHMENT" for item in entries) == 1
    and not any(item.get("artifact_kind") == "LOGPARSE_RUN" for item in entries)
    and not any(
        item["status"] == "ACTIVE"
        and item["provenance"].get("input_name") == "order_id"
        for item in snapshot["user_facts"]
    )
)
if first_log:
    assert value["result_type"] == "NEED_INPUT" and value["error"] is None
    payload = value["payload"]
    added = payload["state_delta"]["add_pending_requirements"]
    assert len(added) == 1 and added[0]["name"] == "order_id"
    assert added[0]["kind"] == "INPUT" and added[0]["status"] == "OPEN"
    assert added[0]["requested_by_job_id"] == job["job_id"]
    assert added[0]["fulfilled_by_refs"] == []
    assert payload["requested_input"] == [added[0]["requirement_id"]]
    assert payload["requested_attachments"] == []
    assert payload["candidate_conclusion_draft"] is None
    assert payload["state_delta"]["add_evidence_bindings"] == [
        {
            "existing_evidence_id": None,
            "evidence_proposal_key": "logparse-client-evidence",
        }
    ]
    assert len(value["proposed_evidence_drafts"]) == 1
    evidence = value["proposed_evidence_drafts"][0]
    assert evidence["proposal_key"] == "logparse-client-evidence"
    assert evidence["source_type"] == "LOGPARSE"
    assert evidence["source_binding"]["artifact_proposal_key"] == "logparse-run"
    assert evidence["workspace_relative_path"] is None
    assert evidence["declared_size"] is None and evidence["declared_sha256"] is None
    assert len(value["proposed_artifact_drafts"]) == 1
    artifact = value["proposed_artifact_drafts"][0]
    assert artifact["proposal_key"] == "logparse-run"
    assert artifact["artifact_kind"] == "LOGPARSE_RUN"
    assert artifact["content_type"] == "application/vnd.problem-locator.logparse-run+directory"
    assert artifact["resource_kind"] == "DIRECTORY"
    assert artifact["workspace_relative_path"] == "output/proposals/logparse-run/tree"
    tree = root / artifact["workspace_relative_path"]
    files = sorted(path for path in tree.rglob("*") if path.is_file() and not path.is_symlink())
    tree_entries = [
        TreeManifestEntry(
            path=path.relative_to(tree).as_posix(),
            size=len(data := path.read_bytes()),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        for path in files
    ]
    tree_hash = hashlib.sha256(
        canonical_json_bytes(TreeManifest(version=1, entries=tree_entries))
    ).hexdigest()
    assert artifact["declared_size"] == sum(item.size for item in tree_entries)
    assert artifact["declared_sha256"] == tree_hash
    assert artifact["metadata"]["tree_manifest_sha256"] == tree_hash

runs = [
    item
    for item in entries
    if item["input_kind"] == "ARTIFACT"
    and item.get("artifact_kind") == "LOGPARSE_RUN"
]
client_evidence = [
    item
    for item in entries
    if item["input_kind"] == "EVIDENCE"
    and item.get("source_type") == "LOGPARSE"
]
active_order = any(
    item["status"] == "ACTIVE"
    and item["provenance"].get("input_name") == "order_id"
    for item in snapshot["user_facts"]
)
if len(runs) == 1 and len(client_evidence) == 1 and active_order:
    run_id = runs[0]["resource_id"]
    client_id = client_evidence[0]["resource_id"]
    server_key = "logparse-server-evidence"
    server_binding = {
        "existing_evidence_id": None,
        "evidence_proposal_key": server_key,
    }
    candidate_bindings = [
        {"existing_evidence_id": client_id, "evidence_proposal_key": None},
        server_binding,
    ]
    assert value["result_type"] == "COMPLETED" and value["error"] is None
    assert value["consumed_evidence_refs"] == [client_id]
    assert len(value["proposed_evidence_drafts"]) == 1
    evidence = value["proposed_evidence_drafts"][0]
    assert evidence["proposal_key"] == server_key
    assert evidence["source_type"] == "LOGPARSE"
    assert evidence["source_binding"] == {
        "artifact_proposal_key": None,
        "existing_source_ref": run_id,
    }
    assert evidence["workspace_relative_path"] is None
    assert evidence["declared_size"] is None and evidence["declared_sha256"] is None
    payload = value["payload"]
    assert payload["state_delta"]["add_evidence_bindings"] == [server_binding]
    candidate = payload["candidate_conclusion_draft"]
    assert candidate is not None
    assert candidate["supporting_evidence_bindings"] == candidate_bindings
    assert all(
        mapping["evidence_bindings"] == candidate_bindings
        for mapping in candidate["completion_criteria_mapping"]
    )
    assert len(value["proposed_artifact_drafts"]) == 1
    user_result_artifact = value["proposed_artifact_drafts"][0]
    assert user_result_artifact["artifact_kind"] == "USER_RESULT"
    assert user_result_artifact["proposal_key"] == "user-result"
    assert user_result_artifact["resource_kind"] == "FILE"
    assert user_result_artifact["content_type"] == "application/json"
    assert user_result_artifact["workspace_relative_path"].startswith(
        "output/proposals/user-result/"
    )
    user_result_raw = (
        root / user_result_artifact["workspace_relative_path"]
    ).read_bytes()
    user_result = json.loads(user_result_raw)
    assert user_result_raw == canonical_json_bytes(user_result)
    UserResultPayload.model_validate(user_result)
    assert user_result_artifact["declared_size"] == len(user_result_raw)
    assert user_result_artifact["declared_sha256"] == hashlib.sha256(
        user_result_raw
    ).hexdigest()
    assert user_result["problem_statement"] == snapshot["problem_spec"]["statement"]
    assert user_result["candidate_statement"] == candidate["statement"]
    assert user_result["supporting_evidence_bindings"] == candidate_bindings
    assert (
        user_result["completion_criteria_mapping"]
        == candidate["completion_criteria_mapping"]
    )
    assert all(
        mapping["evidence_bindings"] == candidate_bindings
        for mapping in user_result["completion_criteria_mapping"]
    )
print("AGENT_OUTPUT_SELF_CHECK_PASSED")
```

Validate the final bytes, not only the parsed value. Put proposal content only below its declared `output/proposals/<proposal_key>/` root. Stdout, stderr, Markdown, and partial files are never business output.
