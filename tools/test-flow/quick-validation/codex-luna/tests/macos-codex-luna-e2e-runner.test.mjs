import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  buildScenarioEvidenceSources,
  clientPrompt,
  createStandaloneGitBoundary,
  extractCommandHttpEntries,
  partitionMcpCalls,
  selectScenarioJobs,
  structuredMcpData,
  validDescriptorUploadCommand,
} from "../runtime/macos-codex-luna-e2e-runner.mjs";

test("Logparse target evidence binds staged bytes and traces prefixed lines to the raw ZIP member", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-logparse-source-"));
  const raw = path.join(root, "raw.log");
  const workspace = path.join(root, "workspace");
  const target = path.join(workspace, "inputs", "target.log");
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(raw, "first\nsecond\nthird\n");
  fs.writeFileSync(target, "[....] [diagnostic|raw.log] first\n[....] [diagnostic|raw.log] third\n");
  const digest = (file) => crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
  const sources = buildScenarioEvidenceSources({
    targetLogs: { target_logs: [{ source_id: "server", label: "server", log_path: "inputs/target.log", content_sha256: digest(target), size: fs.statSync(target).size }] },
    rawByLabel: new Map([["server", { file_name: "server.log", path: raw }]]),
    workspaceRoot: workspace,
  });
  assert.deepEqual(sources, [{ source_id: "server", file_name: "server.log", raw_sha256: digest(raw), target_sha256: digest(target), lines: ["[....] [diagnostic|raw.log] first", "[....] [diagnostic|raw.log] third"] }]);
  fs.writeFileSync(target, "[....] [diagnostic|raw.log] absent\n");
  const changed = { target_logs: [{ source_id: "server", label: "server", log_path: "inputs/target.log", content_sha256: digest(target), size: fs.statSync(target).size }] };
  assert.throws(
    () => buildScenarioEvidenceSources({ targetLogs: changed, rawByLabel: new Map([["server", { file_name: "server.log", path: raw }]]), workspaceRoot: workspace }),
    (error) => error.code === "MACOS_CODEX_LUNA_LOGPARSE_SOURCE_MISMATCH",
  );
});

test("scenario lifecycle distinguishes the attachment-request and completed DIAGNOSE jobs", () => {
  const jobs = [
    { job_id: "route", job_type: "ROUTE", status: "SUCCEEDED" },
    { job_id: "need-attachment", job_type: "DIAGNOSE", status: "SUCCEEDED" },
    { job_id: "completed", job_type: "DIAGNOSE", status: "SUCCEEDED" },
    { job_id: "review", job_type: "REVIEW", status: "SUCCEEDED" },
  ];
  const outcomes = new Map([
    ["route", { result_type: "COMPLETED" }],
    ["need-attachment", { result_type: "NEED_ATTACHMENT" }],
    ["completed", { result_type: "COMPLETED" }],
    ["review", { result_type: "COMPLETED" }],
  ]);
  assert.deepEqual(selectScenarioJobs(jobs, outcomes), {
    route: jobs[0],
    attachmentRequestDiagnose: jobs[1],
    diagnose: jobs[2],
    review: jobs[3],
  });
  assert.throws(
    () => selectScenarioJobs(jobs.slice(0, 1).concat(jobs.slice(2)), outcomes),
    (error) => error.code === "MACOS_CODEX_LUNA_SERVER_JOB_LIFECYCLE_INVALID",
  );
});

test("client prompt contains only mapped facts, archive identity, finite workflow, and no oracle", () => {
  const prompt = clientPrompt({
    mapped: {
      raw_problem_text: "problem",
      statement: "statement",
      expected_behavior: "expected",
      actual_behavior: "actual",
      scope: "scope",
      goals: ["goal"],
      non_goals: ["non-goal"],
      constraints: ["constraint"],
      completion_criteria: ["criterion"],
      initial_user_fact_names: ["problem_time"],
      initial_user_fact_values: ["time"],
    },
    archivePath: "/private/tmp/run/input/logs.zip",
    archive: { size: 123, sha256: "a".repeat(64) },
    runId: "run-one",
  });
  assert.match(prompt, /create_case → get_case/);
  assert.match(prompt, /轮询总时限 12 分钟/);
  assert.match(prompt, /\/private\/tmp\/run\/input\/logs\.zip/);
  assert.match(prompt, /openssl dgst -sha256/);
  assert.match(prompt, /恰好 64 位/);
  assert.match(prompt, /wait_seconds 必须是 0 到 30/);
  assert.match(prompt, /公开 Case 投影不会展示 attachment 内部 READY/);
  assert.match(prompt, /文字声称“已 PUT”不算执行/);
  assert.match(prompt, /禁止为等待该要求变为 FULFILLED 而轮询/);
  assert.match(prompt, /使用同一 request_id 最多纠正一次/);
  assert.doesNotMatch(prompt, /CONFIRMED|API_COMPLETE|expected_status|forbidden_evidence_terms/);
});

test("structured MCP result extractor accepts structuredContent or text JSON and rejects errors", () => {
  assert.deepEqual(structuredMcpData({ tool: "x", result: { structuredContent: { ok: true, data: { case_id: "case" }, error: null } } }), { case_id: "case" });
  assert.deepEqual(structuredMcpData({ tool: "x", result: { content: [{ type: "text", text: '{"ok":true,"data":{"artifacts":[]},"error":null}' }] } }), { artifacts: [] });
  assert.throws(
    () => structuredMcpData({ tool: "x", result: { structuredContent: { ok: false, data: null, error: { code: "FAIL" } } } }),
    (error) => error.code === "MACOS_CODEX_LUNA_MCP_RESULT_INVALID",
  );
});

test("MCP partition keeps successes and records recoverable business envelopes", () => {
  const success = { tool: "problem_locator_prepare_attachment", result: { structuredContent: { ok: true, data: { upload: {} }, error: null } } };
  const conflict = { tool: "problem_locator_prepare_attachment", result: { structuredContent: { ok: false, data: null, error: { code: "REVISION_CONFLICT" } } } };
  assert.deepEqual(partitionMcpCalls([conflict, success]), { successful: [success], recoveries: [{ tool: "problem_locator_prepare_attachment", code: "REVISION_CONFLICT" }] });
});

test("command HTTP extraction records curl method and URL without treating unrelated shell as HTTP", () => {
  assert.deepEqual(extractCommandHttpEntries([
    { item_id: "curl", command: "/usr/bin/curl -X PUT 'http://127.0.0.1:4321/upload/token' --data-binary '@/private/tmp/logs.zip'" },
    { item_id: "hash", command: "/usr/bin/shasum -a 256 /private/tmp/logs.zip" },
  ]), [{ method: "PUT", url: "http://127.0.0.1:4321/upload/token", source: "client-command:curl" }]);
});

test("descriptor upload accepts one successful curl file body spelling and rejects duplicates", () => {
  const upload = { url: "http://127.0.0.1:4321/upload/token", required_headers: { A: "1", B: "2", C: "3", D: "4" } };
  const archivePath = "/private/tmp/logs.zip";
  const receipt = { status: "completed", exit_code: 0, command: `/usr/bin/curl -X PUT -H 'A: 1' -H 'B: 2' -H 'C: 3' -H 'D: 4' --data-binary '@${archivePath}' '${upload.url}'` };
  assert.equal(validDescriptorUploadCommand({ commands: [receipt], upload, archivePath }), true);
  assert.equal(validDescriptorUploadCommand({ commands: [receipt, receipt], upload, archivePath }), false);
});

test("Client workspace owns a standalone Git boundary and cannot inherit parent AGENTS instructions", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-client-git-"));
  createStandaloneGitBoundary(root);
  assert.equal(fs.readFileSync(path.join(root, ".git", "HEAD"), "utf8"), "ref: refs/heads/main\n");
  assert.equal(fs.existsSync(path.join(root, ".git", "objects")), true);
  assert.throws(() => createStandaloneGitBoundary(root), (error) => error.code === "MACOS_CODEX_LUNA_CLIENT_GIT_COLLISION");
});
