---
name: problem-locator-client
description: Operate a Problem Locator V1 diagnosis case through its seven Remote MCP tools and transfer selected attachments or downloadable artifacts with system curl. Use when creating, inspecting, continuing, resuming, or cancelling a diagnosis case, supplying requested facts or local files, or downloading a reviewed diagnosis result.
---

# Problem Locator Client

Treat the service as the authority for every Case, revision, requirement, Job, and Artifact. Use Remote MCP for structured control and HTTP only for file bytes.

## Use the fixed tools

Call only these Remote MCP tools:

- `problem_locator_create_case`
- `problem_locator_prepare_attachment`
- `problem_locator_submit_supplement`
- `problem_locator_get_case`
- `problem_locator_resume_case`
- `problem_locator_cancel_case`
- `problem_locator_list_artifacts`

Generate one stable `request_id` for each logical write operation and reuse it when retrying that same operation. Pass the latest displayed `case_revision` as `expected_case_revision`. Keep `wait_seconds` within `0..30`; a timeout means the same asynchronous Job continues.

After every write response, show the durable business receipt first. When `case_view` is present, also show the user the current Case and diagnosis-state revisions, status, open requirements, active Job, and next required action. When `case_view` is null, report that the write was persisted at the receipt's `case_id` and `case_revision` but the current projection is unavailable; do not turn the success into a failure or invent current Case state. Preserve the receipt's `case_id`, then use `problem_locator_get_case` to refresh when state reads are healthy.

## Create or inspect a Case

1. Build the complete `problem_spec` without a revision and preserve the user's text exactly.
2. Call `problem_locator_create_case` with a fresh stable `request_id`.
3. Poll or finitely wait with `problem_locator_get_case`; never create a replacement Case merely because waiting timed out.

Use `problem_locator_resume_case` only for a persisted pending or interrupted Case. Use `problem_locator_submit_supplement` for a waiting Case. Use `problem_locator_cancel_case` only after confirming the current revision with the user when cancellation is not already explicit.

## Submit requested facts

Read the open requirements from the latest Case view. Map each answer to its exact requirement `name`, then call `problem_locator_submit_supplement` with a new stable `request_id`, the latest revision, `inputs`, and any READY `attachment_ids`. Preserve values exactly; do not trim, normalize, or invent missing facts.

On `REVISION_CONFLICT`, call `problem_locator_get_case`, review the new state, update `expected_case_revision`, and retry the same logical submission without changing its stable request ID. Do not retry an `IDEMPOTENCY_CONFLICT` as if it were a revision conflict.

## Upload a selected file

1. Ask the user to identify the local file and its canonical lowercase Content-Type. For archives, require a canonical lowercase filename suffix that matches it: `.zip` for `application/zip`, `.tar` for `application/x-tar`, or `.tar.gz`, `.tgz`, or `.gz` for `application/gzip`; reject path-like attachment names, control characters, uppercase archive suffixes, and mismatched suffixes while still accepting the user's legitimate local path.
2. Call `problem_locator_prepare_attachment` with the current revision and a fresh stable `request_id`.
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
