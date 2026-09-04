---
name: logparse-diagnose
description: Legacy broker-facing compatibility Helper for older Problem Locator environments or delegated diagnosis Skills. The current V6 specialized Runtime executes its fixed preprocessing request directly and does not call this Helper.
---

# Logparse Diagnose

The current V6 specialized hot path does not invoke this Skill. It remains a
closed compatibility asset for older Server prompts and explicitly delegated
diagnosis flows; do not add it to a new specialized deployment merely to satisfy
the current Runtime.

Act only as the broker-facing Helper for the Problem Locator Runtime or the selected
`diagnose-*` Skill. Do not route, diagnose the business cause, create a Candidate,
or write `output/job_outcome.draft.json`. Return control without interpreting the
broker's machine result.

## Invocation modes

Use `SERVER_PREPROCESS` mode only when the product-owned Runtime prompt explicitly
declares that mode and supplies one exact operation, one prewritten request path,
and one result path. In this mode:

- do not load or execute another Skill, including the selected business diagnosis
  Skill;
- do not read or rewrite the prewritten request;
- invoke the supplied `problem-locator-logparse` command exactly once, only after
  this Helper has loaded successfully;
- wait for that request and exit immediately on success or failure, with no retry,
  direct-Logparse fallback, alternate broker command, or path substitution; and
- do not read the broker result or target log bodies, diagnose, or write any
  diagnosis/review output. The Runtime alone validates the result, freezes target
  logs, and decides whether Pass B may start.

When the prompt does not explicitly declare `SERVER_PREPROCESS`, use the delegated
diagnosis mode described in the remaining lifecycle sections. Never infer Server
mode from path names or environment values.

## Authority and inputs

In delegated diagnosis mode, use the current S00 contract and the S07 request
contract. Read exactly the Runtime-produced, read-only `inputs/manifest.json`; do
not scan `inputs/`, read a Repository, infer file names, or use an old Session.
In `SERVER_PREPROCESS` mode, use only the operation and paths supplied by the
product-owned prompt; do not inspect the manifest or derive replacements.

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
construct raw Logparse argv, or fall back to a direct Logparse CLI path. Invoke the
supplied client command with exactly one Bash tool call. The Bash input must be the
single unmodified command: do not use a shell wrapper such as `sh -c`, command
chaining, pipes, redirection, substitutions, environment assignments, or an
alternate executable.

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

In delegated diagnosis mode, write each request as valid, unambiguous UTF-8 JSON.
In `SERVER_PREPROCESS` mode, the Runtime has already written the canonical request;
do not read, edit, replace, or recreate it. In both modes, the installed
`problem-locator-logparse` client validates the operation-specific model,
recursively canonicalizes the same request file, and atomically replaces it before
contacting the broker. Common request fields are
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

This section applies only to delegated diagnosis mode. In `SERVER_PREPROCESS`, the
product-owned prompt fixes whether the sole operation is `parse-targets` or
`target-logs`; do not repeat the lifecycle decision.

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

This section applies only to delegated diagnosis mode. In `SERVER_PREPROCESS`, use
the sole operation already fixed by the product-owned prompt.

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

The broker result is the only authority for target log selection. In
`SERVER_PREPROCESS`, do not read or interpret that result; the Runtime owns all
validation and freezing after the broker process exits. In delegated diagnosis
mode, each returned
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
