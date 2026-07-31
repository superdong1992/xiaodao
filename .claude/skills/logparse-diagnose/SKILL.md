---
name: logparse-diagnose
description: Use inside a Problem Locator DIAGNOSE Job when a selected diagnosis Skill needs target logs from a fixed Attachment or an already accepted LOGPARSE_RUN. This skill uses only the job-scoped problem-locator-logparse broker client and never invokes logparse directly.
---

# Logparse Diagnose

Act only as the broker-facing helper for the selected `diagnose-*` Skill. Do not
route, diagnose the business cause, create a Candidate, or write
`output/job_outcome.json`. Return the broker's machine result to the calling Skill.

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

Write request files as S00 Canonical JSON: UTF-8, sorted object keys, compact
separators, no NaN/Infinity, and one trailing LF. Common request fields are
`schema_version=1`, a single millisecond UTC RFC 3339 `problem_time`, and ordered
`anchors[]`. Every anchor contains only `label,module,slot,process_name,pid`; pid
may be null. Values must already be supported by the calling Skill's fixed input.

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
is the broker's `target_logs` object; on a nonzero broker rejection it is the
exact public S00 `ExecutionFailure`, including `retryable`. Return that failure
unchanged and end the Job; never infer retryability from the process exit code or
safe stderr summary. On success, return the result, the controlled
`parse_manifest.json` relative reference, and the proposal tree root to the
calling Skill without rewriting lifecycle selection or target paths.

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

Missing, ambiguous, invalid, escaped, cancelled, or failed broker results remain
the current S00 broker/application failure. This Skill defines no public DTO,
exception type, result type, or error code and performs no compatibility mapping.

## Sensitive-data boundary

Never put the broker endpoint/token, raw configuration, absolute paths,
environment values, request credentials, or raw log body into stdout/stderr,
AgentJobOutcome, Evidence/Artifact metadata, Candidate, or USER_RESULT. Logs may
be read only at broker-returned controlled paths by the calling diagnosis Skill.
