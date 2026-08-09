import fs from "node:fs";
import path from "node:path";
import { assertFlow, ensureDirectory } from "./util.mjs";

const SAFE_EVENT_TYPES = /^[a-z][a-z0-9_.-]{1,80}$/;

export class EventWriter {
  constructor({ attemptRoot, runId, producerId, producerType, limitBytes = 64 * 1024 * 1024, clock = () => new Date() }) {
    assertFlow(/^[a-zA-Z0-9_.-]+$/.test(producerId), "EVENT_PRODUCER_ID", "Unsafe producer id");
    this.runId = runId;
    this.producerId = producerId;
    this.producerType = producerType;
    this.limitBytes = limitBytes;
    this.clock = clock;
    this.sequence = 0;
    this.started = process.hrtime.bigint();
    this.bytes = 0;
    this.closed = false;
    const eventsRoot = path.join(attemptRoot, "payload", "events");
    ensureDirectory(eventsRoot);
    this.filePath = path.join(eventsRoot, `${producerId}.ndjson`);
    this.descriptor = fs.openSync(this.filePath, "wx", 0o600);
  }

  write(eventType, { stageId = null, scenario = null, correlationId = null, requestId = null, caseId = null, jobId = null, data = {} } = {}) {
    assertFlow(!this.closed, "EVENT_WRITER_CLOSED", "Event writer is closed");
    assertFlow(SAFE_EVENT_TYPES.test(eventType), "EVENT_TYPE", `Invalid event type ${eventType}`);
    assertFlow(data !== null && typeof data === "object" && !Array.isArray(data), "EVENT_DATA", "Event data must be an object");
    const pending = [data];
    let unsafe = null;
    while (pending.length > 0 && unsafe === null) {
      const value = pending.pop();
      if (Array.isArray(value)) pending.push(...value.filter((item) => item && typeof item === "object"));
      else if (value && typeof value === "object") {
        for (const [key, child] of Object.entries(value)) {
          if (/(?:api[_-]?key|auth(?:orization)?|token|secret|password)/i.test(key)) { unsafe = key; break; }
          if (child && typeof child === "object") pending.push(child);
        }
      }
    }
    assertFlow(unsafe === null, "EVENT_DATA_SENSITIVE_KEY", `Sensitive event data key is forbidden: ${unsafe}`);
    const elapsedMs = Number(process.hrtime.bigint() - this.started) / 1_000_000;
    const envelope = {
      schema_version: 1,
      seq: ++this.sequence,
      timestamp_utc: this.clock().toISOString(),
      run_id: this.runId,
      producer_id: this.producerId,
      producer_type: this.producerType,
      event_type: eventType,
      stage_id: stageId,
      scenario,
      monotonic_elapsed_ms: Math.round(elapsedMs * 1000) / 1000,
      correlation_id: correlationId,
      request_id: requestId,
      case_id: caseId,
      job_id: jobId,
      data,
    };
    const line = `${JSON.stringify(envelope)}\n`;
    const size = Buffer.byteLength(line);
    assertFlow(this.bytes + size <= this.limitBytes, "EVENT_STREAM_LIMIT", `Event stream exceeded ${this.limitBytes} bytes`);
    fs.writeSync(this.descriptor, line, null, "utf8");
    fs.fsyncSync(this.descriptor);
    this.bytes += size;
    return envelope;
  }

  close() {
    if (this.closed) return;
    let failure = null;
    try { fs.fsyncSync(this.descriptor); } catch (error) { failure = error; }
    try { fs.closeSync(this.descriptor); } catch (error) { failure ??= error; }
    this.closed = true;
    if (failure) throw failure;
  }
}

export function validateEventFile(filePath, { allowPartialTail = false } = {}) {
  const raw = fs.readFileSync(filePath, "utf8");
  if (!allowPartialTail) assertFlow(raw.endsWith("\n"), "EVENT_PARTIAL_TAIL", `${filePath} has an incomplete final line`);
  const lines = raw.split("\n");
  if (lines.at(-1) === "") lines.pop();
  let expected = 1;
  let producerId = null;
  let producerType = null;
  let runId = null;
  for (const [index, line] of lines.entries()) {
    let event;
    try {
      event = JSON.parse(line);
    } catch (error) {
      if (allowPartialTail && index === lines.length - 1) break;
      throw error;
    }
    assertFlow(event.schema_version === 1, "EVENT_SCHEMA", `Invalid event schema at line ${index + 1}`);
    assertFlow(event.seq === expected, "EVENT_SEQUENCE", `Expected sequence ${expected}, got ${event.seq}`);
    assertFlow(SAFE_EVENT_TYPES.test(event.event_type), "EVENT_TYPE", `Invalid event type at line ${index + 1}`);
    assertFlow(typeof event.timestamp_utc === "string" && event.timestamp_utc.endsWith("Z") && Number.isFinite(Date.parse(event.timestamp_utc)), "EVENT_TIMESTAMP", `Invalid timestamp at line ${index + 1}`);
    assertFlow(typeof event.run_id === "string" && event.run_id.length > 0, "EVENT_RUN_ID", `Invalid run id at line ${index + 1}`);
    assertFlow(typeof event.producer_id === "string" && /^[a-zA-Z0-9_.-]+$/.test(event.producer_id), "EVENT_PRODUCER", `Invalid producer at line ${index + 1}`);
    assertFlow(typeof event.producer_type === "string" && event.producer_type.length > 0, "EVENT_PRODUCER_TYPE", `Invalid producer type at line ${index + 1}`);
    assertFlow(Number.isFinite(event.monotonic_elapsed_ms) && event.monotonic_elapsed_ms >= 0, "EVENT_MONOTONIC", `Invalid monotonic time at line ${index + 1}`);
    assertFlow(event.data !== null && typeof event.data === "object" && !Array.isArray(event.data), "EVENT_DATA", `Invalid data at line ${index + 1}`);
    producerId ??= event.producer_id;
    producerType ??= event.producer_type;
    runId ??= event.run_id;
    assertFlow(event.producer_id === producerId, "EVENT_PRODUCER_MIXED", "One event file has multiple producers");
    assertFlow(event.producer_type === producerType, "EVENT_PRODUCER_TYPE_MIXED", "One event file has multiple producer types");
    assertFlow(event.run_id === runId, "EVENT_RUN_MIXED", "One event file has multiple run ids");
    expected += 1;
  }
  assertFlow(expected > 1, "EVENT_STREAM_EMPTY", `${filePath} is empty`);
  return { status: "PASS", event_count: expected - 1, producer_id: producerId, producer_type: producerType, run_id: runId };
}

export function readRelayedEventPart({
  filePath,
  receiptPath,
  expectedProducerId,
  expectedRunId,
  allowEmpty = false,
}) {
  assertFlow(fs.existsSync(filePath), "EVENT_RELAY_PART_MISSING", `${filePath} is missing`);
  assertFlow(fs.existsSync(receiptPath), "EVENT_RELAY_RECEIPT_MISSING", `${receiptPath} is missing`);

  let receipt;
  try {
    receipt = JSON.parse(fs.readFileSync(receiptPath, "utf8"));
  } catch {
    assertFlow(false, "EVENT_RELAY_RECEIPT_INVALID", `${receiptPath} is not valid JSON`);
  }
  assertFlow(
    receipt?.schema_version === 1
      && receipt.status === "PASS"
      && receipt.code === null
      && Number.isInteger(receipt.source_event_count)
      && receipt.source_event_count >= 0
      && receipt.producer_id === expectedProducerId,
    "EVENT_RELAY_RECEIPT_INVALID",
    `${receiptPath} does not authorize this event part`,
  );

  const raw = fs.readFileSync(filePath, "utf8");
  if (raw.length === 0) {
    assertFlow(receipt.source_event_count === 0, "EVENT_RELAY_COUNT_MISMATCH", `${filePath} is empty but its receipt is not`);
    assertFlow(allowEmpty, "EVENT_RELAY_EMPTY_FORBIDDEN", `${filePath} is empty without an explicit policy`);
    return { status: "PASS", event_count: 0, events: [], receipt };
  }

  const validation = validateEventFile(filePath);
  assertFlow(validation.event_count === receipt.source_event_count, "EVENT_RELAY_COUNT_MISMATCH", `${filePath} disagrees with its relay receipt`);
  assertFlow(validation.producer_id === expectedProducerId, "EVENT_RELAY_PRODUCER_MISMATCH", `${filePath} has the wrong producer`);
  assertFlow(validation.run_id === expectedRunId, "EVENT_RELAY_RUN_MISMATCH", `${filePath} has the wrong run id`);
  return {
    status: "PASS",
    event_count: validation.event_count,
    events: raw.split("\n").filter(Boolean).map((line) => JSON.parse(line)),
    receipt,
  };
}

export function readServerMcpCorrespondence(attemptRoot, clientToolNames) {
  assertFlow(Array.isArray(clientToolNames) && clientToolNames.every((name) => typeof name === "string" && name.length > 0), "CLIENT_TOOL_SEQUENCE_INVALID", "Client tool sequence is invalid");
  const eventsPath = path.join(attemptRoot, "payload", "events", "service-linux.diagnostics.ndjson");
  validateEventFile(eventsPath);
  const events = fs.readFileSync(eventsPath, "utf8").split("\n").filter(Boolean).map((line) => JSON.parse(line));
  const startedToolNames = events.filter((event) => event.event_type === "mcp.tool.started" && typeof event.data?.tool === "string").map((event) => event.data.tool);
  const completedToolNames = events.filter((event) => event.event_type === "mcp.tool.completed" && typeof event.data?.tool === "string").map((event) => event.data.tool);
  const exact = (actual) => actual.length === clientToolNames.length && actual.every((name, index) => name === clientToolNames[index]);
  return {
    client_tool_names: [...clientToolNames],
    started_tool_names: startedToolNames,
    completed_tool_names: completedToolNames,
    started_exact: exact(startedToolNames),
    completed_exact: exact(completedToolNames),
  };
}
