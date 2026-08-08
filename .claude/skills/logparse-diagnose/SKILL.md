---
name: logparse-diagnose
description: Use inside a Problem Locator DIAGNOSE Job when a selected diagnosis Skill needs target logs from a fixed Attachment or an already accepted LOGPARSE_RUN. This skill uses only the job-scoped problem-locator-logparse broker client and never invokes logparse directly.
---

# Logparse Diagnose

Act only as the broker-facing helper for the selected `diagnose-*` Skill. Do not
route, diagnose the business cause, create a Candidate, or write
`output/job_outcome.draft.json`. Return the broker's machine result to the calling Skill.

## Authority and inputs

Use the current S00 contract and the S07 request contract. Read exactly the
Runtime-produced, read-only `inputs/manifest.json`; do not scan `inputs/`, read a
Repository, infer file names, or use an old Session.

The manifest is authoritative for:

- `job_id` and `case_id`
- fixed `logparse_tool_ref` and `logparse_product`
- Attachment ids, hashes, ContentTypes, and materialized relative paths
- accepted `LOGPARSE_RUN` Artifact ids, hashes, metadata, and tree paths

Never accept `logparse_product` from a request or caller. Never read
`LOGPARSE_REPO`, `LOGPARSE_CONFIG_PATH`, or `LOGPARSE_PYTHON`.

The manifest always records the effective product. `default` means the selected
Diagnosis Skill omitted a product and the broker invokes upstream Logparse without
`--product`; any non-default value was explicitly fixed by that Skill and is passed
by the broker. This helper never adds, removes, or overrides the flag itself.

Archive Content-Type is also platform-owned rather than Skill-authored. The fixed
filename mapping is `.gz/.tar.gz/.tgz -> application/gzip`, `.zip -> application/zip`,
and `.tar -> application/x-tar`.

## Only allowed client

Use only the installed `problem-locator-logparse` client. It speaks to the
Runtime-created job-scoped broker through the current process environment. Do not
print, persist, forward, or inspect the endpoint/token. Do not start `cli.py`,
construct raw logparse argv, use a shell, or fall back to a direct CLI path.

Allowed commands are exactly:

```text
problem-locator-logparse parse-targets
  --request output/proposals/<proposal_key>/request.json
  --result output/proposals/<proposal_key>/target_logs.json

problem-locator-logparse target-logs
  --request output/proposals/<proposal_key>/request.json
  --result output/proposals/<proposal_key>/target_logs.json
```

Both paths must be safe Workspace-relative POSIX paths rooted below the same
proposal key. No other flags or positional arguments are allowed.

## Request bytes

Write each request as valid, unambiguous UTF-8 JSON. The installed
`problem-locator-logparse` client validates the operation-specific model,
recursively canonicalizes the same request file, and atomically replaces it
before contacting the broker. Common request fields are
`schema_version=1`, a single millisecond UTC RFC 3339 `problem_time`, and ordered
`anchors[]`. Every anchor contains only `label,module,slot,process_name,pid`; pid
may be null. `label`, `module`, `slot`, and `process_name` are always JSON strings;
copy every resolved binding byte-for-byte without converting numeric-looking
strings such as `"1"` into numbers. Values must already be supported by the
calling Skill's fixed input.

`parse-targets` adds only `attachment_id` and `artifact_proposal_key`.
`target-logs` adds only `artifact_id`. Neither request contains product, paths,
endpoint/token, raw configuration, arbitrary argv, or extra fields.

The broker owns the parse claim and exposes its read-only canonical parse request
bytes through the S00 session audit surface. The Agent does not create, edit, or
delete anything in `runtime/tool-state/`.

## First parse

Call `parse-targets` only when all conditions hold:

- the manifest contains no `artifact_kind=LOGPARSE_RUN` entry;
- the selected Attachment is the one fixed by the current Job;
- there is no earlier parse attempt in this Job;
- the calling Skill has complete anchors and problem_time;
- the proposal output root is new and empty.

Invoke it exactly once. A failure ends this Job through the current S00 failure
contract; do not retry parse in the same Job. Do not open, enumerate, unpack,
scan, grep, or infer anything from the original archive.

The single result file is always a machine-readable JSON object. On success it
contains the broker's `target_logs`; a `parse-targets` success also contains one
`logparse_run_artifact_draft`, while a `target-logs` reuse result does not. On a
nonzero broker rejection it is the
exact public S00 `ExecutionFailure`, including `retryable`. Return that failure
unchanged and end the Job; never infer retryability from the process exit code or
safe stderr summary. On success, return the result, the controlled
`parse_manifest.json` relative reference, and the proposal tree root to the
calling Skill without rewriting lifecycle selection or target paths.

When the calling Skill proposes that new tree as a `LOGPARSE_RUN`, copy the
broker-returned `logparse_run_artifact_draft` object byte-for-byte into
`proposed_artifact_drafts`; do not construct or edit it. Its metadata
object contains exactly these six fields: `tree_manifest_sha256`,
`logparse_version_ref`, `parse_manifest_relative_path`, `source_attachment_id`,
`source_attachment_sha256`, and `parse_parameters` containing only `product`.
Do not add generic Artifact metadata such as `schema_version`, `format_id`,
or `description`; the strict `LogparseRunMetadata` branch does not allow them.
The Artifact draft envelope is also fixed: `artifact_kind=LOGPARSE_RUN`,
`content_type=application/vnd.problem-locator.logparse-run+directory`,
`resource_kind=DIRECTORY`, and both `declared_size` and `declared_sha256` are
null. Do not guess a MIME type, expand a version string, or independently
hash/size the broker-owned tree.

If the calling Skill must return `NEED_INPUT` after this parse, proposals alone
are not persistent. It must propose the necessary LOGPARSE Evidence and add one
`state_delta.add_evidence_bindings` entry for every Evidence item that must cross
the Job boundary. Each entry uses `existing_evidence_id=null` and the matching
`evidence_proposal_key`. Each such Evidence draft binds the returned run through
its `artifact_proposal_key`; that dependency makes the platform accept the
`LOGPARSE_RUN` together with the Evidence. Findings or prose do not replace these
bindings. The continuation then reuses the accepted run with `target-logs` and
must not call `parse-targets` again.

## Reuse an accepted LOGPARSE_RUN

If the manifest contains any `LOGPARSE_RUN`, `parse-targets` is forbidden even
when the source Attachment is also present. Validate the manifest entry's kind,
directory resource kind, content type, size/hash, source Attachment id/hash,
fixed tool ref, fixed product, and safe parse manifest path. Then call only
`target-logs` with that Artifact id.

Use the read-only materialized root
`inputs/artifacts/<artifact_id>/tree`. Do not alter it and do not parse again.
Cross-Job continuity comes from the accepted Artifact, Evidence, source
Attachment, StateDelta, and PREVIOUS_OUTCOME—not from conversation history.

## Result and path boundary

The broker result is the only authority for target log selection. Each returned
`log_path` must resolve within the current controlled output root. Give the
calling Skill only safe relative POSIX locations and machine fields needed to
form S00 Evidence locators. Do not use nearby logs, directory traversal,
fallback grep/rg, lifecycle guesses, or paths constructed from user text.

For every LOGPARSE Evidence draft, set `workspace_relative_path` to null. Put the
broker-returned path only in `locator.relative_path`, relative to the bound
LOGPARSE_RUN tree root, and bind that run through either the same-Outcome
`artifact_proposal_key` or an existing Artifact ID. Never copy a target log into
an Evidence proposal or declare a path inside the LOGPARSE_RUN tree as the
Evidence proposal's own workspace path.

Missing, ambiguous, invalid, escaped, cancelled, or failed broker results remain
the current S00 broker/application failure. This Skill defines no public DTO,
exception type, result type, or error code and performs no compatibility mapping.

## Sensitive-data boundary

Never put the broker endpoint/token, raw configuration, absolute paths,
environment values, request credentials, or raw log body into stdout/stderr,
AgentJobOutcome, Evidence/Artifact metadata, Candidate, or USER_RESULT. Logs may
be read only at broker-returned controlled paths by the calling diagnosis Skill.
