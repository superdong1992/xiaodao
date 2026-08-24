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

Follow the exact one-Case journey in the task. Use one distinct stable `request_id` for each logical write and the latest observed `case_revision` for every `expected_case_revision`. Never create another Case after a timeout or error. Poll with `problem_locator_get_case` only to the finite deadline supplied by the task.

For the attachment:

1. Call `problem_locator_prepare_attachment` exactly once with `name=logs.zip`, `content_type=application/zip`, and the task-supplied size/SHA-256.
2. Use the returned UploadDescriptor URL and exactly its four headers. Do not construct an upload URL.
3. Run system `curl` once for that descriptor-authorized PUT. Quote the URL, headers, and ZIP path as independent arguments. Do not call any other business HTTP endpoint.
4. Use the upload response revision and READY attachment ID in a separate `problem_locator_submit_supplement` call.

After the Case reaches a terminal state, call `problem_locator_list_artifacts`. Do not download any artifact. Finish with a concise JSON summary containing `case_id`, terminal Case status, and the returned artifact metadata.
