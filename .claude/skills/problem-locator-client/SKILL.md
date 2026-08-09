---
name: problem-locator-client
description: Operate a current Problem Locator 2.0 diagnosis case through its seven Remote MCP tools and transfer selected attachments or downloadable artifacts with system curl. Use when creating, inspecting, continuing, resuming, or cancelling a diagnosis case, supplying requested facts or local files, or downloading a reviewed diagnosis result.
---

# Problem Locator Client

Treat the service as the authority for every Case, revision, requirement, Job, and Artifact. Use Remote MCP for structured control and HTTP only for file bytes.

## Connect directly to the remote MCP server

Configure Claude Code from [references/client-mcp-config.json](references/client-mcp-config.json) so it connects directly to the controlled-network HTTP endpoint ending in `/mcp`. Windows and macOS use their native Claude Code by default; a Linux Client is used only when explicitly selected. A client does not install the `problem-locator` package, run a local MCP server or proxy, or install Problem Locator Hooks. Keep the configured server key exactly `problem-locator`. If the machine has `HTTP_PROXY` or `HTTPS_PROXY`, add only the remote MCP host or IP to `NO_PROXY`; do not disable the corporate proxy globally.

Current Problem Locator exposes only flat MCP input schemas. Every root property is a scalar, nullable scalar, or scalar array. Do not send `problem_spec`, `initial_user_facts`, or `inputs`, and never encode an object as a JSON string. The Linux service remains the validation and diagnostic authority.

Use `/mcp` to verify that `problem-locator` is the connected remote HTTP server. No Problem Locator client DFX file is produced; schema, arguments and validation errors are observed from the Linux service.

## Use the fixed tools

Call only these Remote MCP tools:

- `problem_locator_create_case`
- `problem_locator_prepare_attachment`
- `problem_locator_submit_supplement`
- `problem_locator_get_case`
- `problem_locator_resume_case`
- `problem_locator_cancel_case`
- `problem_locator_list_artifacts`

## Use the exact JSON argument shapes

Use only the following flat argument shapes:

`problem_locator_create_case`:

```json
{
  "request_id": "<stable-request-id>",
  "statement": "<problem statement>",
  "expected_behavior": "<expected behavior>",
  "actual_behavior": "<actual behavior>",
  "scope": "<diagnosis scope>",
  "goals": ["<goal>"],
  "non_goals": [],
  "constraints": [],
  "completion_criteria": ["<criterion>"],
  "initial_user_fact_names": ["<requirement_name>"],
  "initial_user_fact_values": ["<exact string value>"],
  "wait_seconds": 0
}
```

The two initial fact arrays default to `[]`, must have equal lengths, and are
paired by index. Fact names must be unique.

`problem_locator_prepare_attachment`:

```json
{
  "request_id": "<stable-request-id>",
  "case_id": "<case-uuid>",
  "expected_case_revision": 1,
  "name": "logs.zip",
  "content_type": "application/zip",
  "declared_size": null,
  "declared_sha256": null
}
```

`problem_locator_submit_supplement`:

```json
{
  "request_id": "<stable-request-id>",
  "case_id": "<case-uuid>",
  "expected_case_revision": 2,
  "input_names": ["order_id"],
  "input_values": ["order-1"],
  "attachment_ids": ["<ready-attachment-uuid>"],
  "wait_seconds": 0
}
```

`problem_locator_get_case`:

```json
{
  "case_id": "<case-uuid>",
  "wait_for_job_id": null,
  "wait_seconds": 0
}
```

`problem_locator_resume_case`:

```json
{
  "request_id": "<stable-request-id>",
  "case_id": "<case-uuid>",
  "expected_case_revision": 2,
  "wait_seconds": 0
}
```

`problem_locator_cancel_case`:

```json
{
  "request_id": "<stable-request-id>",
  "case_id": "<case-uuid>",
  "expected_case_revision": 2
}
```

`problem_locator_list_artifacts`:

```json
{
  "case_id": "<case-uuid>"
}
```

Only `declared_size`, `declared_sha256`, and `wait_for_job_id` accept explicit
`null`. The two initial fact arrays and each `wait_seconds` are optional with
defaults `[]` and `0`; all other members shown above are required for their tool.

Generate one stable `request_id` for each logical write operation and reuse it when retrying that same operation. Pass the latest displayed `case_revision` as `expected_case_revision`. Keep `wait_seconds` within `0..30`; a timeout means the same asynchronous Job continues.

After every write response, show the durable business receipt first. When `case_view` is present, also show the user the current Case and diagnosis-state revisions, status, open requirements, active Job, and next required action. When `case_view` is null, report that the write was persisted at the receipt's `case_id` and `case_revision` but the current projection is unavailable; do not turn the success into a failure or invent current Case state. Preserve the receipt's `case_id`, then use `problem_locator_get_case` to refresh when state reads are healthy.

## Create or inspect a Case

1. Build all eight flat problem fields without a revision and preserve the user's text exactly.
2. Call `problem_locator_create_case` with a fresh stable `request_id`.
3. Poll or finitely wait with `problem_locator_get_case`; never create a replacement Case merely because waiting timed out.

Use `problem_locator_resume_case` only for a persisted pending or interrupted Case. Use `problem_locator_submit_supplement` for a waiting Case. Use `problem_locator_cancel_case` only after confirming the current revision with the user when cancellation is not already explicit.

## Submit requested facts

Read the open requirements from the latest Case view. Put each exact requirement name in `input_names` and its answer at the same index in `input_values`, then call `problem_locator_submit_supplement` with a new stable `request_id`, the latest revision, and any READY `attachment_ids`. The arrays must have equal lengths and unique names. Preserve values exactly; do not trim, normalize, or invent missing facts.

On `REVISION_CONFLICT`, call `problem_locator_get_case`, review the new state, update `expected_case_revision`, and retry the same logical submission without changing its stable request ID. Do not retry an `IDEMPOTENCY_CONFLICT` as if it were a revision conflict.

## Upload a selected file

1. Ask the user to identify the local file; do not ask for a Logparse archive Content-Type. Derive it from the canonical lowercase filename suffix: `.zip` maps to `application/zip`, `.tar` maps to `application/x-tar`, and `.tar.gz`, `.tgz`, or `.gz` map to `application/gzip`. Reject path-like attachment names, control characters, uppercase archive suffixes, and unsupported suffixes while still accepting the user's legitimate local path. For a non-Logparse attachment, use the Content-Type declared by its requirement or caller context rather than inventing one.
2. Call `problem_locator_prepare_attachment` with the current revision and a fresh stable `request_id`. Its exact filename and byte-count members are `name` and `declared_size`; never send `attachment_name` or `declared_byte_count`. The full input has exactly `request_id`, `case_id`, `expected_case_revision`, `name`, `content_type`, `declared_size`, and `declared_sha256`.
3. Use the returned `UploadDescriptor` verbatim. Require exactly its four headers. Read the complete local file to determine its byte count and lowercase SHA-256, stop if it exceeds `max_bytes`, and verify any non-null declared length/hash. Replace a null `Content-Length` or `X-Content-SHA256` with the measured value. Keep `Idempotency-Key` equal to `attachment_id` and do not reuse the prepare request ID for PUT.
4. Invoke system `curl` with an argument array, or quote every URL, header value, and local path as an independent argument. Never concatenate an unquoted shell command. Support spaces, Unicode, quotes, and shell metacharacters in the local path.
5. Read the PUT response's new `case_revision`.
6. Call `problem_locator_submit_supplement` with a separate stable request ID and the READY `attachment_id`.

Treat READY as “upload published,” not “adopted by the diagnosis.” Uploading alone must never be reported as having continued the Case. Never place file bytes in an MCP request or response.

## Download a reviewed Artifact

1. Call `problem_locator_list_artifacts`; do not infer a URL from an Artifact ID or a Case view.
2. Select only an Artifact returned by that tool and use its `download_url` verbatim.
3. If the destination exists, stop and ask the user for permission or a new name. Never overwrite automatically.
4. Download with system `curl` using independent argv values, then verify the received byte count and SHA-256 against the `ArtifactView`.

Do not request or display internal `LOGPARSE_RUN` objects, storage keys, service-side absolute paths, raw environment values, credentials, or hidden execution logs. Case and resource IDs identify objects but do not prove authorization; use this client only against the intended controlled-network service.
