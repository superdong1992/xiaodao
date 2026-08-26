import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  auditMcpRecoveries,
  buildScenarioEvidenceSources,
  clientDeveloperInstructions,
  clientPollingInstructions,
  clientPrompt,
  createStandaloneGitBoundary,
  e2eProgressLine,
  extractCommandHttpEntries,
  partitionMcpCalls,
  serviceJobFailureCode,
  persistServiceFailureEvidence,
  persistRuntimeFailureEvidence,
  persistWorkspaceFailureEvidence,
  sealOracleAdapterReceipt,
  selectScenarioJobs,
  serviceLauncherArguments,
  structuredMcpData,
  validDescriptorUploadCommand,
} from "../runtime/macos-codex-luna-e2e-runner.mjs";

test("oracle contract failures seal the exact missing marker after core evidence exists", (t) => {
  const evidenceRoot = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-oracle-failure-"));
  t.after(() => fs.rmSync(evidenceRoot, { recursive: true, force: true }));
  fs.writeFileSync(path.join(evidenceRoot, "model-usage.json"), '{"status":"PASS","total_tokens":123}\n');
  fs.writeFileSync(path.join(evidenceRoot, "server-sealed-diagnosis.json"), '{"status":"PASS"}\n');
  assert.throws(() => sealOracleAdapterReceipt({
    evidenceRoot,
    scenarioId: "multiple-rpc-timeouts",
    oracle: {
      scenario_id: "multiple-rpc-timeouts",
      expected_status: "CONFIRMED",
      expected_branch_markers: ["LATE_RESPONSE"],
      expected_terms: [],
      forbidden_evidence_terms: [],
      expected_evidence_identities: [],
    },
    publicStatus: "COMPLETED",
    sealedDiagnosis: { status: "CONFIRMED", confirmed_methods: ["api-execution-too-long"], evidence: [] },
    evidenceSources: [],
    checks: { model_usage: "PASS", security: "PASS" },
  }), (error) => error.code === "MACOS_CODEX_LUNA_BRANCH_MARKER_MISSING");
  const receipt = JSON.parse(fs.readFileSync(path.join(evidenceRoot, "adapter-receipt.json"), "utf8"));
  assert.equal(receipt.status, "FAIL");
  assert.equal(receipt.checks.oracle, "FAIL");
  assert.equal(receipt.failure.details.marker, "LATE_RESPONSE");
  assert.deepEqual(Object.keys(receipt.evidence_sha256), ["model-usage.json", "server-sealed-diagnosis.json"]);
});

test("oracle term failures seal the exact missing output token", (t) => {
  const evidenceRoot = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-oracle-term-"));
  t.after(() => fs.rmSync(evidenceRoot, { recursive: true, force: true }));
  fs.writeFileSync(path.join(evidenceRoot, "model-usage.json"), '{"status":"PASS","total_tokens":123}\n');
  assert.throws(() => sealOracleAdapterReceipt({
    evidenceRoot,
    scenarioId: "server-queue-five",
    oracle: {
      scenario_id: "server-queue-five",
      expected_status: "CONFIRMED",
      expected_branch_markers: [],
      expected_terms: ["1500000"],
      forbidden_evidence_terms: [],
      expected_evidence_identities: [],
    },
    publicStatus: "COMPLETED",
    sealedDiagnosis: { status: "CONFIRMED", confirmed_methods: ["server-receive-queue"], evidence: [] },
    evidenceSources: [],
    checks: { model_usage: "PASS", security: "PASS" },
  }), (error) => error.code === "MACOS_CODEX_LUNA_EXPECTED_TERM_MISSING");
  const receipt = JSON.parse(fs.readFileSync(path.join(evidenceRoot, "adapter-receipt.json"), "utf8"));
  assert.equal(receipt.failure.details.term, "1500000");
});

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

  const unresolvedJobs = jobs.slice(0, 3);
  const unresolvedOutcomes = new Map(outcomes);
  unresolvedOutcomes.set("completed", { result_type: "INCONCLUSIVE" });
  assert.deepEqual(selectScenarioJobs(unresolvedJobs, unresolvedOutcomes, "INCONCLUSIVE"), {
    route: unresolvedJobs[0],
    attachmentRequestDiagnose: unresolvedJobs[1],
    diagnose: unresolvedJobs[2],
    review: null,
  });
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
    scenarioId: "server-queue-delay",
  });
  assert.match(prompt, /create_case → get_case/);
  assert.match(prompt, /轮询总时限 8 分钟/);
  assert.match(prompt, /\/private\/tmp\/run\/input\/logs\.zip/);
  assert.match(prompt, /openssl dgst -sha256/);
  assert.match(prompt, /恰好 64 位/);
  assert.match(prompt, /wait_seconds 必须是 0 到 30/);
  assert.match(prompt, /公开 Case 投影不会展示 attachment 内部 READY/);
  assert.match(prompt, /文字声称“已 PUT”不算执行/);
  assert.match(prompt, /逐字复制 UploadDescriptor 的四个完整 required_headers/u);
  assert.match(prompt, /--fail-with-body/u);
  assert.match(prompt, /--upload-file '\/private\/tmp\/run\/input\/logs\.zip'/u);
  assert.match(prompt, /禁止在 curl 命令中重新计算、拼接或用 shell 变量展开 header/u);
  assert.match(prompt, /禁止使用 --data-binary/u);
  assert.match(prompt, /--max-time 60/u);
  assert.match(prompt, /第一个 curl 没有 terminal 回执时严禁发起第二个 curl/u);
  assert.match(prompt, /禁止为等待该要求变为 FULFILLED 而轮询/);
  assert.match(prompt, /使用同一 request_id 最多纠正一次/);
  assert.match(prompt, /若 prepare 仅因 declared_size 或 declared_sha256/);
  assert.match(prompt, /禁止第二次纠正或插入其他调用/);
  assert.doesNotMatch(prompt, /CONFIRMED|API_COMPLETE|expected_status|forbidden_evidence_terms/);
});

test("client developer instructions pin cwd and copy the complete archive identity", () => {
  const workspaceRoot = path.resolve("client-workspace");
  const instructions = clientDeveloperInstructions({ size: 732, sha256: "a".repeat(64) }, workspaceRoot);
  assert.match(instructions, /禁止 cd、chdir/);
  assert.ok(instructions.includes(JSON.stringify(workspaceRoot)));
  assert.match(instructions, /Skill 已由 app-server 加载/);
  assert.match(instructions, /禁止使用 commandExecution、sed、cat/);
  assert.match(instructions, /第一条允许的命令必须.*openssl\/stat/);
  assert.match(instructions, /整数 732/);
  assert.match(instructions, new RegExp("a".repeat(64)));
  assert.match(instructions, /禁止重算、缩写、漏字、改序或传 null/);
  assert.match(instructions, /立即用上述精确 size\/SHA 纠正一次/);
  assert.throws(() => clientDeveloperInstructions({ size: 732, sha256: "a".repeat(64) }, "relative"), (error) => error.code === "MACOS_CODEX_LUNA_CLIENT_DEVELOPER_INSTRUCTIONS_INVALID");
});

test("client polling instructions cap calls and require authoritative Job handoff", () => {
  const instructions = clientPollingInstructions();
  assert.match(instructions, /最多调用 get_case 16 次/);
  assert.match(instructions, /下一次必须改用新值/);
  assert.match(instructions, /active_job 为 null，严禁复用旧 job_id/);
  assert.match(instructions, /不得连续紧密重复相同 get_case 参数/);
});

test("one attachment declaration validation error permits only an immediate exact same-request correction", () => {
  const archive = { size: 564, sha256: "a".repeat(64) };
  const baseArguments = { request_id: "prepare-one", case_id: "case-one", expected_case_revision: 5, name: "logs.zip", content_type: "application/zip" };
  const failed = { tool: "problem_locator_prepare_attachment", arguments: { ...baseArguments, declared_size: 564, declared_sha256: "a".repeat(63) }, result: { structuredContent: { ok: false, data: null, error: { code: "VALIDATION_ERROR", retryable: false, details: [{ field: "declared_sha256" }] } } } };
  const corrected = { tool: failed.tool, arguments: { ...baseArguments, declared_size: archive.size, declared_sha256: archive.sha256 }, result: { structuredContent: { ok: true, data: { upload: {} }, error: null } } };
  assert.deepEqual(auditMcpRecoveries([failed, corrected], { archive }).recoveries, [{ tool: failed.tool, code: "ATTACHMENT_DECLARATION_VALIDATION", request_id: "prepare-one" }]);
  assert.throws(() => auditMcpRecoveries([failed, { ...corrected, arguments: { ...corrected.arguments, request_id: "prepare-two" } }], { archive }), (error) => error.code === "MACOS_CODEX_LUNA_MCP_RECOVERY_INVALID");
  assert.throws(() => auditMcpRecoveries([failed, { tool: "problem_locator_get_case", arguments: { case_id: "case-one" }, result: { structuredContent: { ok: true, data: {}, error: null } } }, corrected], { archive }), (error) => error.code === "MACOS_CODEX_LUNA_MCP_RECOVERY_INVALID");
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

test("service draft rejection is projected as a scenario contract failure", (t) => {
  const dataRoot = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-service-failure-"));
  t.after(() => fs.rmSync(dataRoot, { recursive: true, force: true }));
  const jobId = "00000000-0000-0000-0000-000000000123";
  const stderr = path.join(dataRoot, "jobs", jobId, "stderr.log");
  fs.mkdirSync(path.dirname(stderr), { recursive: true });
  const failedCase = { failure: { source_job_id: jobId } };
  fs.writeFileSync(stderr, '{"status":"FAIL","code":"MACOS_CODEX_LUNA_SERVICE_DRAFT_REJECTED"}\n');
  assert.equal(serviceJobFailureCode({ dataRoot, failedCase }), "MACOS_CODEX_LUNA_SERVICE_DRAFT_REJECTED");
  fs.writeFileSync(stderr, '{"status":"FAIL","code":"MACOS_CODEX_LUNA_SERVICE_FINALIZER_FAILED"}\n');
  assert.equal(serviceJobFailureCode({ dataRoot, failedCase }), "MACOS_CODEX_LUNA_SERVICE_JOB_FAILED");
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

test("client timeouts retain bounded secret-scanned service DFX", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-client-timeout-"));
  const dfxRoot = path.join(root, "dfx");
  const evidenceRoot = path.join(root, "evidence");
  const privateRoot = path.join(root, "private");
  const serviceLog = path.join(root, "service.log");
  for (const directory of [dfxRoot, privateRoot]) fs.mkdirSync(directory, { recursive: true });
  fs.writeFileSync(path.join(dfxRoot, "debug.jsonl"), '{"event":"mcp.tool.started"}\n');
  fs.writeFileSync(path.join(dfxRoot, "journey.jsonl"), '{"event":"job.progress"}\n');
  fs.writeFileSync(serviceLog, "bounded service log\n");
  try {
    const result = persistRuntimeFailureEvidence({ dfxRoot, serviceLog, evidenceRoot, privateRoot, canaries: ["secret-canary-value"] });
    assert.equal(result.secret_scan.status, "PASS");
    assert.equal(fs.readFileSync(path.join(evidenceRoot, "service-runtime", "client-failure", "debug.jsonl"), "utf8"), '{"event":"mcp.tool.started"}\n');
    assert.equal(JSON.parse(fs.readFileSync(path.join(evidenceRoot, "service-runtime", "client-failure-secret-scan.json"), "utf8")).status, "PASS");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("command HTTP extraction records curl method and URL without treating unrelated shell as HTTP", () => {
  const localSyntaxFailure = { item_id: "syntax", status: "failed", exit_code: 2, command: "/usr/bin/curl -X PUT 'http://127.0.0.1:4321/upload/token" };
  assert.deepEqual(extractCommandHttpEntries([
    localSyntaxFailure,
    { item_id: "curl", command: "/usr/bin/curl -X PUT 'http://127.0.0.1:4321/upload/token' --data-binary '@/private/tmp/logs.zip'" },
    { item_id: "hash", command: "/usr/bin/shasum -a 256 /private/tmp/logs.zip" },
  ]), [{ method: "PUT", url: "http://127.0.0.1:4321/upload/token", source: "client-command:curl" }]);
});

test("descriptor upload permits one proven local curl initialization correction but rejects network retries", () => {
  const upload = { url: "http://127.0.0.1:4321/upload/token", required_headers: { A: "1", B: "2", C: "3", D: "4" } };
  const archivePath = "/private/tmp/logs.zip";
  const receipt = { status: "completed", exit_code: 0, command: `/usr/bin/curl --max-time 60 -X PUT -H 'A: 1' -H 'B: 2' -H 'C: 3' -H 'D: 4' --data-binary '@${archivePath}' '${upload.url}'` };
  assert.equal(validDescriptorUploadCommand({ commands: [receipt], upload, archivePath }), true);
  const localSyntaxFailure = { ...receipt, item_id: "syntax", status: "failed", exit_code: 2, command: receipt.command.slice(0, -1) };
  assert.equal(validDescriptorUploadCommand({ commands: [localSyntaxFailure, receipt], upload, archivePath }), true);
  assert.equal(validDescriptorUploadCommand({ commands: [receipt, receipt], upload, archivePath }), false);
  assert.equal(validDescriptorUploadCommand({ commands: [{ ...localSyntaxFailure, exit_code: 7 }, receipt], upload, archivePath }), false);
  assert.equal(validDescriptorUploadCommand({ commands: [localSyntaxFailure, localSyntaxFailure, receipt], upload, archivePath }), false);
  assert.equal(validDescriptorUploadCommand({ commands: [{ ...receipt, command: receipt.command.replace(`@${archivePath}`, archivePath) }], upload, archivePath }), false);
  assert.equal(validDescriptorUploadCommand({ commands: [{ ...receipt, command: receipt.command.replace("--max-time 60 ", "") }], upload, archivePath }), false);
});

test("descriptor upload accepts one same-command SHA computation and rejects a different archive binding", () => {
  const archivePath = "/private/tmp/logs.zip";
  const archiveSha256 = "a".repeat(64);
  const upload = {
    url: "http://127.0.0.1:4321/upload/token",
    required_headers: {
      "Idempotency-Key": "attachment-id",
      "Content-Type": "application/zip",
      "Content-Length": "123",
      "X-Content-SHA256": archiveSha256,
    },
  };
  const prelude = "archive_sha=$(/usr/bin/openssl dgst -sha256 -r '" + archivePath + "'); archive_sha=${archive_sha%% *}; /usr/bin/test \"${#archive_sha}\" -eq 64; ";
  const receipt = {
    status: "completed",
    exit_code: 0,
    command: "/bin/bash -c '<outer-shell-escaped-command>'",
    logical_command: prelude + "/usr/bin/curl --max-time 60 -X PUT -H 'Idempotency-Key: attachment-id' -H 'Content-Type: application/zip' -H 'Content-Length: 123' -H \"X-Content-SHA256: ${archive_sha}\" --data-binary '@" + archivePath + "' '" + upload.url + "'",
  };
  assert.equal(validDescriptorUploadCommand({ commands: [receipt], upload, archivePath, archiveSha256 }), true);
  assert.equal(validDescriptorUploadCommand({ commands: [receipt], upload, archivePath, archiveSha256: "b".repeat(64) }), false);
  assert.equal(validDescriptorUploadCommand({ commands: [{ ...receipt, logical_command: receipt.logical_command.replace("${#archive_sha}", "${#other_sha}") }], upload, archivePath, archiveSha256 }), false);
});

test("descriptor upload binds one archive_path variable and rejects the observed at-sign space regression", () => {
  const archivePath = "/private/tmp/logs.zip";
  const archiveSha256 = "a".repeat(64);
  const upload = {
    url: "http://127.0.0.1:4321/upload/token",
    required_headers: {
      "Idempotency-Key": "attachment-id",
      "Content-Type": "application/zip",
      "Content-Length": "123",
      "X-Content-SHA256": archiveSha256,
    },
  };
  const command = `archive_path='${archivePath}'; archive_sha=$(/usr/bin/openssl dgst -sha256 -r "\${archive_path}"); archive_sha=\${archive_sha%% *}; /usr/bin/test "\${#archive_sha}" -eq 64; /usr/bin/curl --max-time 60 -X PUT -H 'Idempotency-Key: attachment-id' -H 'Content-Type: application/zip' -H 'Content-Length: 123' -H "X-Content-SHA256: \${archive_sha}" --data-binary "@\${archive_path}" '${upload.url}'`;
  const receipt = { status: "completed", exit_code: 0, logical_command: command };
  assert.equal(validDescriptorUploadCommand({ commands: [receipt], upload, archivePath, archiveSha256 }), true);
  assert.equal(validDescriptorUploadCommand({
    commands: [{ ...receipt, logical_command: command.replace('"@${archive_path}"', '"@ ${archive_path}"') }],
    upload,
    archivePath,
    archiveSha256,
  }), false);
  assert.equal(validDescriptorUploadCommand({
    commands: [{ ...receipt, logical_command: command.replace(`archive_path='${archivePath}'`, "archive_path='/private/tmp/other.zip'") }],
    upload,
    archivePath,
    archiveSha256,
  }), false);
});

test("descriptor upload accepts one literal descriptor-bound upload-file command and rejects header drift", () => {
  const archivePath = "/private/tmp/logs.zip";
  const archiveSha256 = "a".repeat(64);
  const upload = {
    url: "http://127.0.0.1:4321/upload/token",
    required_headers: {
      "Idempotency-Key": "attachment-id",
      "Content-Type": "application/zip",
      "Content-Length": "123",
      "X-Content-SHA256": archiveSha256,
    },
  };
  const command = `/usr/bin/curl --silent --show-error --fail-with-body --max-time 60 --request PUT --header 'Content-Length: 123' --header 'Content-Type: application/zip' --header 'Idempotency-Key: attachment-id' --header 'X-Content-SHA256: ${archiveSha256}' --upload-file '${archivePath}' '${upload.url}'`;
  const receipt = { status: "completed", exit_code: 0, logical_command: command };
  assert.equal(validDescriptorUploadCommand({ commands: [receipt], upload, archivePath, archiveSha256 }), true);
  assert.equal(validDescriptorUploadCommand({
    commands: [{ ...receipt, logical_command: command.replace(archiveSha256, "b".repeat(64)) }],
    upload,
    archivePath,
    archiveSha256,
  }), false);
  assert.equal(validDescriptorUploadCommand({
    commands: [{ ...receipt, logical_command: command.replace(archivePath, "/private/tmp/other.zip") }],
    upload,
    archivePath,
    archiveSha256,
  }), false);
});

test("client Skill uses the literal descriptor headers and upload-file without shell composition", () => {
  const skill = fs.readFileSync(path.join(process.cwd(), "tools", "test-flow", "quick-validation", "codex-luna", "fixtures", "client-skill", "problem-locator-client", "SKILL.md"), "utf8");
  assert.match(skill, /--fail-with-body/);
  assert.match(skill, /--upload-file '<absolute ZIP path>'/);
  assert.match(skill, /Copy each of the descriptor's four complete `Name: value` header strings exactly/);
  assert.match(skill, /Do not recompute, concatenate, or expand a shell variable into any curl header/);
  assert.match(skill, /not `--data-binary`/);
  assert.doesNotMatch(skill, /archive_sha=/);
});

test("Client workspace owns a standalone Git boundary and cannot inherit parent AGENTS instructions", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "macos-luna-client-git-"));
  createStandaloneGitBoundary(root);
  assert.equal(fs.readFileSync(path.join(root, ".git", "HEAD"), "utf8"), "ref: refs/heads/main\n");
  assert.equal(fs.existsSync(path.join(root, ".git", "objects")), true);
  assert.throws(() => createStandaloneGitBoundary(root), (error) => error.code === "MACOS_CODEX_LUNA_CLIENT_GIT_COLLISION");
});
