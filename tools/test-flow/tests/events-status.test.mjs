import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  buildWaterfallSummary,
  EventWriter,
  NEGATIVE_PROBE_VALIDATION_FIELDS,
  readRelayedEventPart,
  readServerMcpCorrespondence,
  validateEventFile,
  validateFlatPublicToolSchema,
} from "../lib/events.mjs";
import { performanceSamples } from "../lib/history.mjs";
import { adjudicateStagePerformance, classifyRun, performanceThreshold } from "../lib/status.mjs";
import { canonicalJson, sha256Bytes, sha256File } from "../lib/util.mjs";

const PUBLIC_TOOLS = [
  "problem_locator_create_case",
  "problem_locator_prepare_attachment",
  "problem_locator_submit_supplement",
  "problem_locator_get_case",
  "problem_locator_resume_case",
  "problem_locator_cancel_case",
  "problem_locator_list_artifacts",
];
const VALIDATION_FIELDS = [...NEGATIVE_PROBE_VALIDATION_FIELDS];

function writeRelay({ attemptRoot, runId, instance, mode, rawEvents, relayEvents, allowEmpty = false }) {
  const parts = path.join(attemptRoot, "payload", "events", "parts");
  fs.mkdirSync(parts, { recursive: true });
  const base = path.join(parts, `service-linux.${instance}.${mode}`);
  const filePath = `${base}.ndjson`;
  const rawPath = `${base}.raw`;
  fs.writeFileSync(filePath, relayEvents.map((event) => JSON.stringify(event)).join("\n") + (relayEvents.length ? "\n" : ""), { mode: 0o600 });
  fs.writeFileSync(rawPath, rawEvents.map((event) => JSON.stringify(event)).join("\n") + (rawEvents.length ? "\n" : ""), { mode: 0o600 });
  const producerId = mode === "journey" ? `service-linux-${instance}` : `service-linux-diagnostics-${instance}`;
  const receiptPath = path.join(attemptRoot, "payload", `service-${instance}-${mode}-relay.json`);
  fs.writeFileSync(receiptPath, `${JSON.stringify({
    schema_version: 2,
    status: "PASS",
    code: null,
    source_event_count: relayEvents.length,
    producer_id: producerId,
    clock_domain: producerId,
    raw_sha256: sha256File(rawPath),
    events_sha256: sha256File(filePath),
    allow_empty: allowEmpty,
  })}\n`, { mode: 0o600 });
  return { filePath, rawPath, receiptPath, producerId, runId };
}

function envelope({ runId, producerId, seq, eventType, requestId = null, correlationId = null, data = {} }) {
  return {
    schema_version: 2,
    seq,
    timestamp_utc: `2026-08-10T00:00:0${seq}.000Z`,
    source_timestamp_utc: null,
    run_id: runId,
    producer_id: producerId,
    producer_type: "service",
    clock_domain: producerId,
    event_type: eventType,
    stage_id: null,
    scenario: "CrossJob",
    monotonic_elapsed_ms: seq,
    correlation_id: correlationId,
    request_id: requestId,
    case_id: null,
    job_id: null,
    data,
  };
}

function writeDiagnosticContract(attemptRoot, runId) {
  const producerId = "service-linux-diagnostics-route";
  const schema = { type: "object", properties: { request_id: { type: "string" }, tags: { type: "array", items: { type: "string" } } } };
  const schemaHash = sha256Bytes(canonicalJson(schema).slice(0, -1));
  const tools = PUBLIC_TOOLS.map((name) => ({ name, input_schema: schema, input_schema_sha256: schemaHash }));
  const requestId = "request-normal";
  const probeId = "request-validation-probe";
  const rawEvents = [
    { event: "mcp.tools.listed", tools },
    { event: "mcp.tool.started", request_id: requestId, correlation_id: "correlation-normal", tool: "problem_locator_create_case" },
    { event: "mcp.tool.completed", request_id: requestId, correlation_id: "correlation-normal", tool: "problem_locator_create_case", ok: true },
    { event: "mcp.tool.started", request_id: probeId, correlation_id: "correlation-probe", tool: "problem_locator_create_case", arguments: { problem_spec: { statement: "removed composite field" }, request_id: probeId } },
    { event: "mcp.tool.validation_failed", request_id: probeId, correlation_id: "correlation-probe", validation_errors: VALIDATION_FIELDS.map((field) => ({ loc: [field], type: field === "problem_spec" ? "extra_forbidden" : "missing" })) },
    { event: "mcp.tool.completed", request_id: probeId, correlation_id: "correlation-probe", tool: "problem_locator_create_case", ok: false, error_code: "VALIDATION_ERROR" },
  ];
  const relayEvents = [
    envelope({ runId, producerId, seq: 1, eventType: "mcp.tools.listed", data: { tool_count: 7, tool_names: PUBLIC_TOOLS, tool_schema_sha256: Array(7).fill(schemaHash) } }),
    envelope({ runId, producerId, seq: 2, eventType: "mcp.tool.started", requestId, correlationId: "correlation-normal", data: { tool: "problem_locator_create_case" } }),
    envelope({ runId, producerId, seq: 3, eventType: "mcp.tool.completed", requestId, correlationId: "correlation-normal", data: { tool: "problem_locator_create_case", ok: true } }),
    envelope({ runId, producerId, seq: 4, eventType: "mcp.tool.started", requestId: probeId, correlationId: "correlation-probe", data: { tool: "problem_locator_create_case", argument_names: ["problem_spec", "request_id"] } }),
    envelope({ runId, producerId, seq: 5, eventType: "mcp.tool.validation_failed", requestId: probeId, correlationId: "correlation-probe", data: { validation_errors: VALIDATION_FIELDS.map((field) => ({ field, type: field === "problem_spec" ? "extra_forbidden" : "missing" })) } }),
    envelope({ runId, producerId, seq: 6, eventType: "mcp.tool.completed", requestId: probeId, correlationId: "correlation-probe", data: { tool: "problem_locator_create_case", ok: false, error_code: "VALIDATION_ERROR" } }),
  ];
  return { ...writeRelay({ attemptRoot, runId, instance: "route", mode: "diagnostics", rawEvents, relayEvents }), requestId, probeId };
}

test("v2 NDJSON is durable, sequenced and monotonic", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-events-"));
  try {
    const writer = new EventWriter({ attemptRoot: root, runId: "run-event", producerId: "orchestrator", producerType: "orchestrator" });
    writer.write("stage.started", { stageId: "deterministic.full", data: { kind: "deterministic" } });
    assert.ok(fs.statSync(writer.filePath).size > 0);
    writer.write("stage.completed", { stageId: "deterministic.full", data: { status: "PASS" } });
    writer.close();
    assert.deepEqual(validateEventFile(writer.filePath), {
      status: "PASS", event_count: 2, producer_id: "orchestrator", producer_type: "orchestrator", run_id: "run-event",
    });
    const lines = fs.readFileSync(writer.filePath, "utf8").trimEnd().split("\n").map(JSON.parse);
    lines[1].monotonic_elapsed_ms = -1;
    fs.writeFileSync(writer.filePath, `${lines.map(JSON.stringify).join("\n")}\n`);
    assert.throws(() => validateEventFile(writer.filePath), (error) => error.code === "EVENT_MONOTONIC");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("event envelopes reject sensitive keys while accepting ordinary metric names", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-events-sensitive-"));
  try {
    const writer = new EventWriter({ attemptRoot: root, runId: "run-event", producerId: "host", producerType: "host" });
    writer.write("stage.progress", { data: { message_code: "token_count_updated" } });
    assert.throws(() => writer.write("stage.progress", { data: { auth_token: "redacted" } }), (error) => error.code === "EVENT_DATA_SENSITIVE_KEY");
    writer.close();
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("waterfall summary indexes content-free Agent and Logparse timing coverage", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-dfx-telemetry-"));
  try {
    const writer = new EventWriter({ attemptRoot: root, runId: "run-dfx", producerId: "service-linux", producerType: "service" });
    writer.write("job.backend.telemetry", {
      caseId: "case-1",
      jobId: "job-1",
      data: {
        job_type: "DIAGNOSE",
        stream_status: "COMPLETE",
        stream_reason: null,
        content_included: false,
        cli_duration_ms: 100,
        model_api_duration_ms: 80,
        usage_total: 12,
      },
    });
    writer.write("job.logparse.phase.completed", {
      caseId: "case-1",
      jobId: "job-1",
      data: { job_type: "DIAGNOSE", logparse_operation: "parse-targets", logparse_phase: "PARSE", duration_ms: 10 },
    });
    writer.write("job.logparse.operation.completed", {
      caseId: "case-1",
      jobId: "job-1",
      data: { job_type: "DIAGNOSE", logparse_operation: "parse-targets", duration_ms: 12 },
    });
    writer.close();

    const summary = buildWaterfallSummary(root, { stages: [] });
    assert.deepEqual(summary.server_dfx_telemetry, {
      backend_events: 1,
      complete: 1,
      partial: 0,
      unavailable: 0,
      reasons: [],
      content_policy_violations: 0,
      logparse_operations: 1,
      logparse_phases: 1,
    });
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("an explicitly authorized empty relay retains raw and transformed digests", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-empty-relay-"));
  try {
    const relayed = writeRelay({ attemptRoot: root, runId: "run-empty", instance: "restart", mode: "journey", rawEvents: [], relayEvents: [], allowEmpty: true });
    assert.equal(readRelayedEventPart({
      filePath: relayed.filePath,
      receiptPath: relayed.receiptPath,
      expectedProducerId: relayed.producerId,
      expectedRunId: relayed.runId,
      allowEmpty: true,
    }).event_count, 0);
    assert.throws(() => readRelayedEventPart({
      filePath: relayed.filePath,
      receiptPath: relayed.receiptPath,
      expectedProducerId: relayed.producerId,
      expectedRunId: relayed.runId,
    }), (error) => error.code === "EVENT_RELAY_EMPTY_FORBIDDEN");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("server DFX proves exact seven flat schemas, request pairs and the negative probe", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-correspondence-"));
  try {
    const runId = "run-correspondence";
    const attemptRoot = path.join(root, runId);
    fs.mkdirSync(attemptRoot);
    const contract = writeDiagnosticContract(attemptRoot, runId);
    const result = readServerMcpCorrespondence(attemptRoot, [{ tool_name: "problem_locator_create_case", input: { request_id: contract.requestId } }], {
      validationProbeRequestId: contract.probeId,
    });
    assert.equal(result.tools_listed_exact, true);
    assert.equal(result.started_exact, true);
    assert.equal(result.completed_exact, true);
    assert.equal(result.request_exact, true);
    assert.equal(result.pair_exact, true);
    assert.equal(result.validation_probe_exact, true);
    assert.deepEqual(result.validation_fields, VALIDATION_FIELDS);

    fs.appendFileSync(contract.rawPath, "{}\n");
    assert.throws(() => readServerMcpCorrespondence(attemptRoot, []), (error) => error.code === "EVENT_RELAY_RAW_DIGEST_MISMATCH");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("flat schema validation rejects references, nested objects and object arrays", () => {
  assert.equal(validateFlatPublicToolSchema({ type: "object", properties: { request_id: { type: "string" }, values: { type: "array", items: { type: "string" } } } }), true);
  assert.equal(validateFlatPublicToolSchema({ type: "object", properties: { nested: { type: "object", properties: {} } } }), false);
  assert.equal(validateFlatPublicToolSchema({ type: "object", properties: { values: { type: "array", items: { type: "object" } } } }), false);
  assert.equal(validateFlatPublicToolSchema({ type: "object", properties: { value: { $ref: "#/$defs/value" } }, $defs: {} }), false);
});

test("status adjudication gives operation and functional failure precedence", () => {
  assert.deepEqual(classifyRun({ functional: "PASS", performance: "PASS", operation: "ERROR" }), { overall: "ERROR", exit_code: 3 });
  assert.deepEqual(classifyRun({ functional: "PASS", performance: "FAIL", operation: "PASS" }), { overall: "FAIL", exit_code: 1 });
  assert.deepEqual(classifyRun({ functional: "INCONCLUSIVE", performance: "NOT_RUN", operation: "PASS" }), { overall: "BLOCKED", exit_code: 2 });
  assert.deepEqual(classifyRun({ functional: "PASS", performance: "NOT_CALIBRATED", operation: "PASS" }), { overall: "PASS_WITH_WARNINGS", exit_code: 0 });
});

test("performance uses ten robust samples and the second same-identity Release regression fails", () => {
  assert.equal(performanceThreshold([1, 2, 3, 4]), null);
  const samples = [100, 101, 99, 100, 100, 101, 100, 99, 100, 100, 10_000];
  const policy = {
    policy_version: "robust-mad-v2", window: 10, min_samples: 5, consecutive_release_failures: 2,
    mad_multiplier: 6, relative_floor: 1.25, local_absolute_floor_seconds: 5, external_absolute_floor_seconds: 30,
    stages: { "*": { mode: "gate", hard_cap_seconds: null } },
  };
  const threshold = performanceThreshold(samples, { external: true, policy });
  assert.equal(threshold.sample_count, 10);
  assert.equal(threshold.median_seconds, 100);
  assert.equal(threshold.threshold_seconds, 130);
  const stage = { id: "real.route", progress_class: "real" };
  assert.equal(adjudicateStagePerformance({ elapsedSeconds: 131, samples, stage, effect: "gate", policy, priorConsecutiveSlow: 0 }).status, "SLOW");
  assert.equal(adjudicateStagePerformance({ elapsedSeconds: 131, samples, stage, effect: "gate", policy, priorConsecutiveSlow: 1 }).status, "FAIL");
});

test("reused zero-second results never enter performance history", () => {
  const history = [
    { verdict: { stages: [{ id: "deterministic.full", status: "PASS", result_source: "EXECUTED", performance_identity: "perf", elapsed_seconds: 42 }] } },
    { verdict: { stages: [{ id: "deterministic.full", status: "PASS", result_source: "REUSED", performance_identity: "perf", elapsed_seconds: 0 }] } },
  ];
  assert.deepEqual(performanceSamples(history, "deterministic.full", "perf", 10), [42]);
});
