import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { EventWriter, readRelayedEventPart, readServerMcpCorrespondence, validateEventFile } from "../lib/events.mjs";
import { classifyRun, performanceThreshold } from "../lib/status.mjs";

test("NDJSON event stream is flushed, sequenced and rejects a partial terminal line", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-events-"));
  try {
    const writer = new EventWriter({ attemptRoot: root, runId: "run-event", producerId: "orchestrator", producerType: "orchestrator" });
    writer.write("stage.started", { stageId: "deterministic.full", data: { kind: "deterministic" } });
    assert.ok(fs.statSync(writer.filePath).size > 0, "event must be visible before close");
    writer.write("stage.completed", { stageId: "deterministic.full", data: { status: "PASS" } });
    writer.close();
    assert.deepEqual(validateEventFile(writer.filePath), {
      status: "PASS",
      event_count: 2,
      producer_id: "orchestrator",
      producer_type: "orchestrator",
      run_id: "run-event",
    });
    fs.appendFileSync(writer.filePath, "{\"schema_version\":1");
    assert.throws(() => validateEventFile(writer.filePath), /incomplete final line/);
    assert.equal(validateEventFile(writer.filePath, { allowPartialTail: true }).event_count, 2);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("event envelope refuses sensitive keys but permits ordinary values containing the word token", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-events-sensitive-"));
  try {
    const writer = new EventWriter({ attemptRoot: root, runId: "run-event", producerId: "host", producerType: "host" });
    writer.write("stage.progress", { data: { message_code: "token_count_updated" } });
    assert.throws(() => writer.write("stage.progress", { data: { auth_token: "redacted" } }), /Sensitive event data key/);
    writer.close();
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("an explicitly authorized zero-event relay part is complete, not a partial tail", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-empty-relay-"));
  try {
    const part = path.join(root, "restart.journey.ndjson");
    const receipt = path.join(root, "restart.journey.receipt.json");
    fs.writeFileSync(part, "", { encoding: "utf8", mode: 0o600 });
    fs.writeFileSync(receipt, JSON.stringify({
      schema_version: 1,
      status: "PASS",
      code: null,
      source_event_count: 0,
      producer_id: "service-linux-restart",
    }), { encoding: "utf8", mode: 0o600 });

    const result = readRelayedEventPart({
      filePath: part,
      receiptPath: receipt,
      expectedProducerId: "service-linux-restart",
      expectedRunId: "run-empty-relay",
      allowEmpty: true,
    });
    assert.equal(result.event_count, 0);
    assert.deepEqual(result.events, []);
    assert.throws(() => readRelayedEventPart({
      filePath: part,
      receiptPath: receipt,
      expectedProducerId: "service-linux-restart",
      expectedRunId: "run-empty-relay",
    }), (error) => error.code === "EVENT_RELAY_EMPTY_FORBIDDEN");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("a relayed event part must match its sealed receipt and retain its terminal LF", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-relay-receipt-"));
  try {
    const attemptRoot = path.join(root, "attempt");
    const writer = new EventWriter({ attemptRoot, runId: "run-relay", producerId: "service-linux-diagnostics-restart", producerType: "service" });
    writer.write("mcp.tool.started", { data: { tool: "problem_locator_get_case" } });
    writer.close();
    const receipt = path.join(root, "relay.json");
    fs.writeFileSync(receipt, JSON.stringify({
      schema_version: 1,
      status: "PASS",
      code: null,
      source_event_count: 1,
      producer_id: "service-linux-diagnostics-restart",
    }), { encoding: "utf8", mode: 0o600 });

    assert.equal(readRelayedEventPart({
      filePath: writer.filePath,
      receiptPath: receipt,
      expectedProducerId: "service-linux-diagnostics-restart",
      expectedRunId: "run-relay",
    }).event_count, 1);

    fs.truncateSync(writer.filePath, fs.statSync(writer.filePath).size - 1);
    assert.throws(() => readRelayedEventPart({
      filePath: writer.filePath,
      receiptPath: receipt,
      expectedProducerId: "service-linux-diagnostics-restart",
      expectedRunId: "run-relay",
    }), (error) => error.code === "EVENT_PARTIAL_TAIL");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("MCP correspondence reads diagnostics rather than business journey events", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-correspondence-"));
  try {
    const journey = new EventWriter({ attemptRoot: root, runId: "run-correspondence", producerId: "service-linux", producerType: "service" });
    journey.write("case.created", { data: { job_type: "ROUTE" } });
    journey.close();
    fs.renameSync(journey.filePath, path.join(path.dirname(journey.filePath), "service-linux.journey.ndjson"));
    const diagnostics = new EventWriter({ attemptRoot: root, runId: "run-correspondence", producerId: "service-linux-diagnostics", producerType: "service" });
    diagnostics.write("mcp.tool.started", { data: { tool: "problem_locator_create_case" } });
    diagnostics.write("mcp.tool.completed", { data: { tool: "problem_locator_create_case", ok: true } });
    diagnostics.close();
    fs.renameSync(diagnostics.filePath, path.join(path.dirname(diagnostics.filePath), "service-linux.diagnostics.ndjson"));

    const result = readServerMcpCorrespondence(root, ["problem_locator_create_case"]);
    assert.equal(result.started_exact, true);
    assert.equal(result.completed_exact, true);
    assert.deepEqual(result.started_tool_names, ["problem_locator_create_case"]);
    assert.deepEqual(result.completed_tool_names, ["problem_locator_create_case"]);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("status axes preserve functional PASS when cleanup fails", () => {
  assert.deepEqual(classifyRun({ functional: "PASS", performance: "PASS", operation: "ERROR" }), { overall: "ERROR", exit_code: 3 });
  assert.deepEqual(classifyRun({ functional: "PASS", performance: "SLOW", operation: "PASS" }), { overall: "PASS_WITH_WARNINGS", exit_code: 0 });
  assert.deepEqual(classifyRun({ functional: "INCONCLUSIVE", performance: "NOT_RUN", operation: "PASS" }), { overall: "BLOCKED", exit_code: 2 });
});

test("performance baseline uses last ten robust samples and calibrates only after five", () => {
  assert.equal(performanceThreshold([1, 2, 3, 4]), null);
  const threshold = performanceThreshold([100, 101, 99, 100, 100, 101, 100, 99, 100, 100, 10_000], { external: true });
  assert.equal(threshold.sample_count, 10);
  assert.equal(threshold.median_seconds, 100);
  assert.equal(threshold.threshold_seconds, 130);
});
