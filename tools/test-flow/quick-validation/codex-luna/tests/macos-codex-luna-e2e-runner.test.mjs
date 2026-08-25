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
  e2eProgressLine,
  extractCommandHttpEntries,
  partitionMcpCalls,
  persistServiceFailureEvidence,
  persistWorkspaceFailureEvidence,
  selectScenarioJobs,
  serviceLauncherArguments,
  structuredMcpData,
  validDescriptorUploadCommand,
} from "../runtime/macos-codex-luna-e2e-runner.mjs";

test("E2E service launch disables Python bytecode inside the materialized source snapshot", () => {
  const sourceRoot = path.resolve("sealed-source");
  assert.deepEqual(serviceLauncherArguments(sourceRoot), [
    "-I",
    "-B",
    path.join(sourceRoot, "tools", "test-flow", "runtime-support", "test_service_launcher.py"),
    "serve",
  ]);
});

test("Codex E2E forwards semantic progress heartbeats to the outer Test Flow watchdog", () => {
  assert.equal(
    e2eProgressLine("diagnose"),
    "TEST_FLOW_PROGRESS stage.progress codex-luna diagnose\n",
  );
  assert.throws(
    () => e2eProgressLine("unknown"),
    (error) => error.code === "MACOS_CODEX_LUNA_PROGRESS_PHASE_INVALID",
  );
});

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

test("Workspace identity failures persist only closed, secret-scanned diagnostics", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-workspace-failure-"));
  const dfxRoot = path.join(root, "dfx");
  const evidenceRoot = path.join(root, "evidence");
  const privateRoot = path.join(root, "private");
  fs.mkdirSync(dfxRoot, { recursive: true });
  fs.mkdirSync(evidenceRoot, { recursive: true });
  fs.mkdirSync(privateRoot, { recursive: true });
  const canary = "do-not-persist-canary";
  fs.writeFileSync(path.join(dfxRoot, "debug.jsonl"), `${JSON.stringify({ event: "debug", private_value: canary })}\n`);
  const fields = [
    ["workspace.measurement_phase", "stable", "before_scan"],
    ["workspace.root", "kind=directory;device=1;inode=2", "kind=directory;device=1;inode=2"],
    ["workspace.inputs", "kind=directory;device=1;inode=3", "kind=directory;device=1;inode=3"],
    ["workspace.output", "kind=directory;device=1;inode=4", "kind=directory;device=1;inode=4"],
    ["workspace.runtime", "kind=directory;device=1;inode=5", "kind=directory;device=1;inode=5"],
    ["workspace.top_level_shape", "expected-shape", "observed-shape"],
  ];
  const event = {
    schema_version: 1,
    sequence: 17,
    timestamp: "2026-08-25T09:00:03.271Z",
    level: "ERROR",
    event: "job.stage.failed",
    correlation_id: null,
    request_id: null,
    case_id: "case-id",
    job_id: "job-id",
    job_type: "ROUTE",
    outcome_id: null,
    duration_ms: null,
    data: {
      stage: "BACKEND_EXECUTE",
      code: "WORKSPACE_LIMIT",
      message: "Workspace output roots could not be measured safely.",
      retryable: false,
      details: [
        ...fields.map(([field, expected, actual]) => ({ field, resource_type: "WORKSPACE", expected, actual })),
        { field: "unrelated", resource_type: "PRIVATE", expected: canary, actual: canary },
      ],
    },
  };
  fs.writeFileSync(path.join(dfxRoot, "journey.jsonl"), `${JSON.stringify(event)}\n`);

  try {
    const result = persistWorkspaceFailureEvidence({
      dfxRoot,
      evidenceRoot,
      privateRoot,
      serviceTermination: { code: 0, signal: null },
      canaries: [canary],
    });
    assert.equal(result.receipt.job.type, "ROUTE");
    assert.equal(result.receipt.workspace_details.length, fields.length);
    assert.equal(result.secret_scan.status, "PASS");
    const persisted = fs.readFileSync(path.join(evidenceRoot, "service-runtime", "workspace-failure.json"), "utf8");
    assert.doesNotMatch(persisted, new RegExp(canary, "u"));
    assert.doesNotMatch(persisted, new RegExp(root.replaceAll("\\", "\\\\"), "u"));
    assert.equal(JSON.parse(persisted).service_termination.code, 0);
    assert.equal(JSON.parse(fs.readFileSync(path.join(evidenceRoot, "service-runtime", "workspace-failure-secret-scan.json"), "utf8")).status, "PASS");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("failed service Jobs persist only bounded logs after credential canary scanning", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-service-failure-"));
  const jobId = "12345678-1234-1234-1234-123456789abc";
  const dataRoot = path.join(root, "data");
  const dfxRoot = path.join(root, "dfx");
  const evidenceRoot = path.join(root, "evidence");
  const privateRoot = path.join(root, "private");
  const jobRoot = path.join(dataRoot, "jobs", jobId);
  for (const directory of [jobRoot, dfxRoot, evidenceRoot, privateRoot]) fs.mkdirSync(directory, { recursive: true });
  fs.writeFileSync(path.join(jobRoot, "stdout.log"), "safe stdout\n");
  fs.writeFileSync(path.join(jobRoot, "stderr.log"), '{"code":"CODEX_LUNA_APP_SERVER_PROCESS_FAILED"}\n');
  fs.writeFileSync(path.join(root, "service.log"), "safe service log\n");
  fs.writeFileSync(path.join(dfxRoot, "debug.jsonl"), '{"event":"debug"}\n');
  fs.writeFileSync(path.join(dfxRoot, "journey.jsonl"), '{"event":"job.stage.failed"}\n');
  try {
    const result = persistServiceFailureEvidence({
      failedCase: {
        case_id: "case-id",
        status: "FAILED",
        failure: {
          code: "BACKEND_EXIT_FAILED",
          message: "Agent process exited unsuccessfully.",
          source_job_id: jobId,
          source_outcome_id: "outcome-id",
        },
      },
      dataRoot,
      dfxRoot,
      serviceLog: path.join(root, "service.log"),
      evidenceRoot,
      privateRoot,
      serviceTermination: { code: 0, signal: null },
      canaries: ["credential-canary-value"],
    });
    assert.equal(result.receipt.case.failure.source_job_id, jobId);
    assert.equal(result.secret_scan.status, "PASS");
    const destination = path.join(evidenceRoot, "service-runtime", "failure");
    assert.deepEqual(fs.readdirSync(destination).sort(), [
      "debug.jsonl",
      "job-stderr.log",
      "job-stdout.log",
      "journey.jsonl",
      "receipt.json",
      "service.log",
    ]);
    assert.match(fs.readFileSync(path.join(destination, "job-stderr.log"), "utf8"), /CODEX_LUNA_APP_SERVER_PROCESS_FAILED/u);
    assert.equal(JSON.parse(fs.readFileSync(path.join(evidenceRoot, "service-runtime", "failure-secret-scan.json"), "utf8")).status, "PASS");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
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
