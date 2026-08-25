---
name: problem-locator-client
description: Drive the test-owned macOS Codex/Luna Problem Locator smoke through exactly seven public MCP tools and one descriptor-authorized attachment upload.
---

# macOS Codex/Luna E2E client

Use only the configured `problem-locator` Streamable HTTP MCP server and these tools:

- `problem_locator_create_case`
- `problem_locator_prepare_attachment`
- `problem_locator_submit_supplement`
- `problem_locator_get_case`
- `problem_locator_resume_case`
- `problem_locator_cancel_case`
- `problem_locator_list_artifacts`

Every MCP root input must be flat: scalar, nullable scalar, or scalar-array properties only. Never send nested objects, object arrays, dynamic maps, JSON strings containing objects, `problem_spec`, `initial_user_facts`, or `inputs`.

Follow the exact one-Case journey in the task. Use one distinct stable `request_id` for each logical write and the latest observed `case_revision` for every `expected_case_revision`. Immediately before `prepare_attachment` and `submit_supplement`, call `problem_locator_get_case` with `wait_seconds=0` and copy that response's exact `case_revision`; never reuse a revision from an earlier response. Never create another Case after a timeout or error. Poll with `problem_locator_get_case` only to the finite deadline supplied by the task. Every `wait_seconds` value must be between 0 and 30 inclusive; when a Job ID is known, pass it as `wait_for_job_id` while waiting for that same Job.

If one logical write returns `REVISION_CONFLICT` or `ATTACHMENT_NOT_READY` without a success business receipt, refresh the same Case and replay that same logical write at most once with the same `request_id` and corrected latest revision/readiness. This is the only permitted correction; do not create or prepare a second resource.

For the attachment:

1. Before the MCP call, run `/usr/bin/openssl dgst -sha256 <zip>` and `/usr/bin/stat -f %z <zip>`. Use their exact output, confirm the digest is exactly 64 lowercase hex characters, and confirm both values equal the task receipt. Never copy, shorten, normalize, or retype the digest from memory.
2. Call `problem_locator_prepare_attachment` exactly once with `name=logs.zip`, `content_type=application/zip`, and the command-verified size/SHA-256.
3. Use the returned UploadDescriptor URL and exactly its four headers. Do not construct an upload URL.
4. Run system `curl` once for that descriptor-authorized PUT. Quote the URL, headers, and ZIP path as independent arguments. Do not call any other business HTTP endpoint. A commentary/final statement that the PUT ran is not execution: continue only after the command tool returns a completed receipt with exit code 0.
5. The public Case projection does not expose the attachment's internal READY state; its attachment requirement remains OPEN until supplement submission. Immediately after the successful PUT, refresh the same Case once with `wait_seconds=0`, then use that exact latest revision and the descriptor attachment ID in `problem_locator_submit_supplement`. If that write returns `ATTACHMENT_NOT_READY`, apply the one permitted same-request-ID correction above; do not poll the OPEN requirement waiting for it to become fulfilled before submission.

After the Case reaches a terminal state, call `problem_locator_list_artifacts`. Do not download any artifact. Finish with a concise JSON summary containing `case_id`, terminal Case status, and the returned artifact metadata.
