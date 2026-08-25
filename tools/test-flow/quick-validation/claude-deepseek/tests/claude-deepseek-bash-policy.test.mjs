import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { evaluateBashToolUse } from "../runtime/claude-deepseek-bash-policy.mjs";

function event(command) { return { tool_name: "Bash", tool_input: { command } }; }

test("Bash policy allows the frozen three-command sequence exactly once and denies probes", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-bash-policy-"));
  const archivePath = path.join(root, "logs.zip");
  const sha = "a".repeat(64);
  const policy = { schema_version: 1, archive_path: archivePath, archive_size: 42, archive_sha256: sha, upload_origin: "http://127.0.0.1:8123", claim_root: path.join(root, "claims") };
  assert.equal(evaluateBashToolUse(policy, event("ls -la")).allowed, false);
  assert.equal(evaluateBashToolUse(policy, event("pwd")).allowed, false);
  assert.equal(evaluateBashToolUse(policy, event(`/usr/bin/openssl dgst -sha256 '${archivePath}'`)).allowed, true);
  assert.equal(evaluateBashToolUse(policy, event(`/usr/bin/stat -f %z '${archivePath}'`)).allowed, true);
  const curl = `/usr/bin/curl --silent --show-error --fail-with-body --request PUT --header 'Content-Length: 42' --header 'Content-Type: application/zip' --header 'Idempotency-Key: att-1' --header 'X-Content-SHA256: ${sha}' --upload-file '${archivePath}' 'http://127.0.0.1:8123/api/v1/attachments/att-1/content'`;
  assert.equal(evaluateBashToolUse(policy, event(curl)).allowed, true);
  assert.equal(evaluateBashToolUse(policy, event(curl)).allowed, false);
  assert.deepEqual(fs.readdirSync(policy.claim_root).sort(), ["curl", "openssl", "stat"]);
});

test("Bash policy rejects wrong order, external URLs, header drift, and shell composition", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-bash-policy-negative-"));
  const archivePath = path.join(root, "logs.zip");
  const sha = "b".repeat(64);
  const policy = { schema_version: 1, archive_path: archivePath, archive_size: 7, archive_sha256: sha, upload_origin: "http://127.0.0.1:8123", claim_root: path.join(root, "claims") };
  assert.equal(evaluateBashToolUse(policy, event(`/usr/bin/stat -f %z '${archivePath}'`)).allowed, false);
  assert.equal(evaluateBashToolUse(policy, event(`/usr/bin/openssl dgst -sha256 '${archivePath}' && pwd`)).allowed, false);
  assert.equal(evaluateBashToolUse(policy, event(`/usr/bin/curl --request PUT --upload-file '${archivePath}' https://example.invalid/upload`)).allowed, false);
});
