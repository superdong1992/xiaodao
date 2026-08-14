import fs from "node:fs";
import path from "node:path";
import { assertFlow, canonicalJson, ensureDirectory, sha256Bytes, sha256File } from "./util.mjs";

const SAFE_EVENT_TYPES = /^[a-z][a-z0-9_.-]{1,80}$/;
export const NEGATIVE_PROBE_VALIDATION_FIELDS = Object.freeze([
  "actual_behavior",
  "completion_criteria",
  "constraints",
  "expected_behavior",
  "goals",
  "non_goals",
  "problem_spec",
  "raw_problem_text",
  "scope",
  "statement",
]);
const EVENT_FIELDS = Object.freeze([
  "schema_version", "seq", "timestamp_utc", "source_timestamp_utc", "run_id",
  "producer_id", "producer_type", "clock_domain", "event_type", "stage_id",
  "scenario", "monotonic_elapsed_ms", "correlation_id", "request_id", "case_id",
  "job_id", "data",
]);

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
      schema_version: 2,
      seq: ++this.sequence,
      timestamp_utc: this.clock().toISOString(),
      source_timestamp_utc: null,
      run_id: this.runId,
      producer_id: this.producerId,
      producer_type: this.producerType,
      clock_domain: this.producerId,
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
  let priorMonotonic = -1;
  for (const [index, line] of lines.entries()) {
    let event;
    try {
      event = JSON.parse(line);
    } catch (error) {
      if (allowPartialTail && index === lines.length - 1) break;
      throw error;
    }
    assertFlow(canonicalJson(Object.keys(event).sort()) === canonicalJson([...EVENT_FIELDS].sort()), "EVENT_FIELDS", `Invalid event fields at line ${index + 1}`);
    assertFlow(event.schema_version === 2, "EVENT_SCHEMA", `Invalid event schema at line ${index + 1}`);
    assertFlow(event.seq === expected, "EVENT_SEQUENCE", `Expected sequence ${expected}, got ${event.seq}`);
    assertFlow(SAFE_EVENT_TYPES.test(event.event_type), "EVENT_TYPE", `Invalid event type at line ${index + 1}`);
    assertFlow(typeof event.timestamp_utc === "string" && event.timestamp_utc.endsWith("Z") && Number.isFinite(Date.parse(event.timestamp_utc)), "EVENT_TIMESTAMP", `Invalid timestamp at line ${index + 1}`);
    assertFlow(event.source_timestamp_utc === null || (typeof event.source_timestamp_utc === "string" && Number.isFinite(Date.parse(event.source_timestamp_utc))), "EVENT_SOURCE_TIMESTAMP", `Invalid source timestamp at line ${index + 1}`);
    assertFlow(typeof event.run_id === "string" && event.run_id.length > 0, "EVENT_RUN_ID", `Invalid run id at line ${index + 1}`);
    assertFlow(typeof event.producer_id === "string" && /^[a-zA-Z0-9_.-]+$/.test(event.producer_id), "EVENT_PRODUCER", `Invalid producer at line ${index + 1}`);
    assertFlow(typeof event.producer_type === "string" && event.producer_type.length > 0, "EVENT_PRODUCER_TYPE", `Invalid producer type at line ${index + 1}`);
    assertFlow(event.clock_domain === event.producer_id, "EVENT_CLOCK_DOMAIN", `Invalid clock domain at line ${index + 1}`);
    for (const name of ["stage_id", "scenario", "correlation_id", "request_id", "case_id", "job_id"]) assertFlow(event[name] === null || typeof event[name] === "string", "EVENT_IDENTIFIER", `Invalid ${name} at line ${index + 1}`);
    assertFlow(Number.isFinite(event.monotonic_elapsed_ms) && event.monotonic_elapsed_ms >= 0, "EVENT_MONOTONIC", `Invalid monotonic time at line ${index + 1}`);
    assertFlow(event.monotonic_elapsed_ms >= priorMonotonic, "EVENT_MONOTONIC_ORDER", `Monotonic time moved backwards at line ${index + 1}`);
    priorMonotonic = event.monotonic_elapsed_ms;
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
    receipt?.schema_version === 2
      && receipt.status === "PASS"
      && receipt.code === null
      && Number.isInteger(receipt.source_event_count)
      && receipt.source_event_count >= 0
      && receipt.producer_id === expectedProducerId
      && receipt.clock_domain === expectedProducerId
      && typeof receipt.allow_empty === "boolean"
      && /^[a-f0-9]{64}$/.test(receipt.raw_sha256 ?? "")
      && /^[a-f0-9]{64}$/.test(receipt.events_sha256 ?? ""),
    "EVENT_RELAY_RECEIPT_INVALID",
    `${receiptPath} does not authorize this event part`,
  );

  const raw = fs.readFileSync(filePath, "utf8");
  assertFlow(sha256File(filePath) === receipt.events_sha256, "EVENT_RELAY_DIGEST_MISMATCH", `${filePath} differs from its relay receipt`);
  const rawPath = filePath.replace(/\.ndjson$/, ".raw");
  assertFlow(fs.existsSync(rawPath) && sha256File(rawPath) === receipt.raw_sha256, "EVENT_RELAY_RAW_DIGEST_MISMATCH", `${rawPath} differs from its relay receipt`);
  if (raw.length === 0) {
    assertFlow(receipt.source_event_count === 0, "EVENT_RELAY_COUNT_MISMATCH", `${filePath} is empty but its receipt is not`);
    assertFlow(allowEmpty && receipt.allow_empty, "EVENT_RELAY_EMPTY_FORBIDDEN", `${filePath} is empty without an explicit policy`);
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

export function readServerDiagnosticEvents(attemptRoot) {
  const root = path.join(attemptRoot, "payload", "events", "parts");
  const order = ["route", "upload", "diagnose", "restart"];
  const streams = [];
  const events = [];
  const rawEvents = [];
  for (const instance of order) {
    const filePath = path.join(root, `service-linux.${instance}.diagnostics.ndjson`);
    if (!fs.existsSync(filePath)) continue;
    const receiptPath = path.join(attemptRoot, "payload", `service-${instance}-diagnostics-relay.json`);
    const relayed = readRelayedEventPart({
      filePath,
      receiptPath,
      expectedProducerId: `service-linux-diagnostics-${instance}`,
      expectedRunId: path.basename(attemptRoot),
    });
    const rawPath = filePath.replace(/\.ndjson$/, ".raw");
    let parsedRaw;
    try {
      parsedRaw = fs.readFileSync(rawPath, "utf8").split("\n").filter(Boolean).map((line) => JSON.parse(line));
    } catch {
      assertFlow(false, "SERVER_DIAGNOSTIC_RAW_INVALID", `${rawPath} is not valid NDJSON`);
    }
    assertFlow(parsedRaw.length === relayed.event_count, "SERVER_DIAGNOSTIC_RAW_COUNT", `${rawPath} disagrees with its relay output`);
    streams.push({
      instance,
      file: path.relative(attemptRoot, filePath).split(path.sep).join("/"),
      raw_file: path.relative(attemptRoot, rawPath).split(path.sep).join("/"),
      receipt_file: path.relative(attemptRoot, receiptPath).split(path.sep).join("/"),
      status: "PASS",
      event_count: relayed.event_count,
      producer_id: relayed.receipt.producer_id,
      producer_type: "service",
      run_id: path.basename(attemptRoot),
      clock_domain: relayed.receipt.clock_domain,
      events_sha256: relayed.receipt.events_sha256,
      raw_sha256: relayed.receipt.raw_sha256,
    });
    events.push(...relayed.events);
    rawEvents.push(...parsedRaw.map((event, index) => ({ instance, relay_event: relayed.events[index], event })));
  }
  assertFlow(streams.length > 0, "SERVER_DIAGNOSTIC_STREAM_MISSING", "No authoritative server diagnostic stream exists");
  return { streams, events, raw_events: rawEvents };
}

const PUBLIC_TOOL_NAMES = Object.freeze([
  "problem_locator_create_case",
  "problem_locator_prepare_attachment",
  "problem_locator_submit_supplement",
  "problem_locator_get_case",
  "problem_locator_resume_case",
  "problem_locator_cancel_case",
  "problem_locator_list_artifacts",
]);

const SCALAR_SCHEMA_TYPES = new Set(["boolean", "integer", "number", "string"]);

function nullableScalar(schema) {
  if (!schema || typeof schema !== "object" || Array.isArray(schema)) return false;
  if (SCALAR_SCHEMA_TYPES.has(schema.type)) return true;
  if (!Array.isArray(schema.anyOf) || schema.anyOf.length < 2) return false;
  const types = new Set(schema.anyOf.map((entry) => entry?.type));
  return types.has("null") && [...types].every((type) => type === "null" || SCALAR_SCHEMA_TYPES.has(type));
}

export function validateFlatPublicToolSchema(schema) {
  if (!schema || typeof schema !== "object" || Array.isArray(schema) || schema.type !== "object" || schema.$defs || schema.$ref) return false;
  if (!schema.properties || typeof schema.properties !== "object" || Array.isArray(schema.properties)) return false;
  const pending = [schema];
  while (pending.length > 0) {
    const value = pending.pop();
    if (!value || typeof value !== "object") continue;
    if (Object.hasOwn(value, "$ref") || Object.hasOwn(value, "$defs")) return false;
    if (Array.isArray(value)) pending.push(...value);
    else pending.push(...Object.values(value));
  }
  return Object.values(schema.properties).every((property) => {
    if (nullableScalar(property)) return true;
    return property?.type === "array" && nullableScalar(property.items);
  });
}

function validationFieldSet(rawEvent) {
  const values = Array.isArray(rawEvent?.validation_errors) ? rawEvent.validation_errors : [];
  return new Set(values.flatMap((entry) => {
    const field = entry?.field ?? entry?.loc ?? entry?.location;
    if (Array.isArray(field)) return [field.map(String).join(".")];
    if (typeof field === "string") return field.split(".");
    return [];
  }));
}

function exactStringSet(actual, expected) {
  return actual.size === expected.length && expected.every((value) => actual.has(value));
}

export function readServerMcpCorrespondence(attemptRoot, clientCalls, { validationProbeRequestId = null } = {}) {
  assertFlow(Array.isArray(clientCalls), "CLIENT_TOOL_SEQUENCE_INVALID", "Client tool sequence is invalid");
  const expected = clientCalls.map((entry) => typeof entry === "string"
    ? { tool_name: entry, request_id: null }
    : { tool_name: entry?.tool_name, request_id: entry?.input?.request_id ?? null });
  assertFlow(expected.every((entry) => typeof entry.tool_name === "string" && entry.tool_name.length > 0), "CLIENT_TOOL_SEQUENCE_INVALID", "Client tool sequence is invalid");
  const { streams, events, raw_events: rawEvents } = readServerDiagnosticEvents(attemptRoot);
  const allStarts = events.filter((event) => event.event_type === "mcp.tool.started" && typeof event.data?.tool === "string");
  const allCompletions = events.filter((event) => event.event_type === "mcp.tool.completed" && typeof event.data?.tool === "string");
  const starts = allStarts.filter((event) => event.request_id !== validationProbeRequestId);
  const completions = allCompletions.filter((event) => event.request_id !== validationProbeRequestId);
  const startedToolNames = starts.map((event) => event.data.tool);
  const completedToolNames = completions.map((event) => event.data.tool);
  const expectedNames = expected.map((entry) => entry.tool_name);
  const exact = (actual) => actual.length === expectedNames.length && actual.every((name, index) => name === expectedNames[index]);
  const pairExact = starts.length === completions.length && starts.every((start, index) => {
    const completion = completions[index];
    return completion
      && start.data.tool === completion.data.tool
      && start.correlation_id
      && start.correlation_id === completion.correlation_id
      && start.request_id === completion.request_id;
  });
  const requestExact = starts.length === expected.length && starts.every((event, index) => event.request_id === expected[index].request_id);
  const listedRaw = rawEvents.filter((entry) => entry.event?.event === "mcp.tools.listed");
  const toolsListedExact = listedRaw.length > 0 && listedRaw.every(({ event, relay_event: relay }) => {
    const tools = event.tools;
    if (!Array.isArray(tools) || canonicalJson(tools.map((tool) => tool?.name)) !== canonicalJson(PUBLIC_TOOL_NAMES)) return false;
    if (!tools.every((tool) => validateFlatPublicToolSchema(tool?.input_schema))) return false;
    const hashes = tools.map((tool) => sha256Bytes(canonicalJson(tool.input_schema).slice(0, -1)));
    return relay.data?.tool_count === 7
      && canonicalJson(relay.data?.tool_names) === canonicalJson(PUBLIC_TOOL_NAMES)
      && canonicalJson(relay.data?.tool_schema_sha256) === canonicalJson(hashes);
  });
  const probeStarts = allStarts.filter((event) => event.request_id === validationProbeRequestId);
  const probeCompletions = allCompletions.filter((event) => event.request_id === validationProbeRequestId);
  const probeFailures = rawEvents.filter(({ event }) => event?.event === "mcp.tool.validation_failed" && event.request_id === validationProbeRequestId);
  const rawProbeStarts = rawEvents.filter(({ event }) => event?.event === "mcp.tool.started" && event.request_id === validationProbeRequestId);
  const validationFields = probeFailures.length === 1 ? validationFieldSet(probeFailures[0].event) : new Set();
  const probeCorrelation = probeStarts[0]?.correlation_id;
  const validationProbeExact = typeof validationProbeRequestId === "string"
    && probeStarts.length === 1
    && probeCompletions.length === 1
    && probeFailures.length === 1
    && rawProbeStarts.length === 1
    && probeStarts[0].data.tool === "problem_locator_create_case"
    && canonicalJson(probeStarts[0].data.argument_names) === canonicalJson(["problem_spec", "request_id"])
    && canonicalJson(Object.keys(rawProbeStarts[0].event.arguments ?? {}).sort()) === canonicalJson(["problem_spec", "request_id"])
    && probeCompletions[0].data.tool === "problem_locator_create_case"
    && probeCompletions[0].data.ok === false
    && probeCompletions[0].data.error_code === "VALIDATION_ERROR"
    && typeof probeCorrelation === "string"
    && probeCorrelation.length > 0
    && probeCorrelation === probeCompletions[0].correlation_id
    && probeCorrelation === probeFailures[0].event.correlation_id
    && probeCorrelation === probeFailures[0].relay_event.correlation_id
    && exactStringSet(validationFields, NEGATIVE_PROBE_VALIDATION_FIELDS);
  return {
    streams,
    client_tool_names: expectedNames,
    started_tool_names: startedToolNames,
    completed_tool_names: completedToolNames,
    started_exact: exact(startedToolNames),
    completed_exact: exact(completedToolNames),
    pair_exact: pairExact,
    request_exact: requestExact,
    tools_listed_exact: toolsListedExact,
    validation_probe_exact: validationProbeExact,
    validation_probe_request_id: validationProbeRequestId,
    validation_fields: [...validationFields].sort(),
  };
}

function eventFilesRecursive(root, relative = "", output = []) {
  if (!fs.existsSync(root)) return output;
  for (const entry of fs.readdirSync(path.join(root, relative), { withFileTypes: true }).sort((left, right) => left.name.localeCompare(right.name))) {
    const child = relative ? `${relative}/${entry.name}` : entry.name;
    if (entry.isDirectory()) eventFilesRecursive(root, child, output);
    else if (entry.isFile() && child.endsWith(".ndjson")) output.push(child);
  }
  return output;
}

function matchingFilesRecursive(root, predicate, relative = "", output = []) {
  if (!fs.existsSync(root)) return output;
  for (const entry of fs.readdirSync(path.join(root, relative), { withFileTypes: true }).sort((left, right) => left.name.localeCompare(right.name))) {
    const child = relative ? `${relative}/${entry.name}` : entry.name;
    if (entry.isDirectory()) matchingFilesRecursive(root, predicate, child, output);
    else if (entry.isFile() && predicate(child)) output.push(child);
  }
  return output;
}

export function buildWaterfallSummary(attemptRoot, candidate) {
  const eventsRoot = path.join(attemptRoot, "payload", "events");
  const producers = [];
  const ids = { correlation_id: new Set(), request_id: new Set(), case_id: new Set(), job_id: new Set() };
  let totalBytes = 0;
  let transferBytes = 0;
  let retryEvents = 0;
  let timeoutEvents = 0;
  let serverOperationDurationMs = 0;
  const jobEvents = new Map();
  for (const relative of eventFilesRecursive(eventsRoot)) {
    const filePath = path.join(eventsRoot, relative);
    const raw = fs.readFileSync(filePath, "utf8");
    if (raw.length === 0) continue;
    const events = raw.split("\n").filter(Boolean).map((line) => JSON.parse(line));
    if (events.length === 0) continue;
    const first = events[0];
    const last = events.at(-1);
    const bytes = Buffer.byteLength(raw);
    totalBytes += bytes;
    for (const event of events) {
      for (const name of Object.keys(ids)) if (typeof event[name] === "string" && event[name]) ids[name].add(event[name]);
      if (/retry|duplicate/.test(event.event_type)) retryEvents += 1;
      if (/timeout|deadline/.test(event.event_type) || /TIMEOUT|DEADLINE/.test(String(event.data?.error_code ?? ""))) timeoutEvents += 1;
      if (Number.isFinite(event.data?.duration_ms)) serverOperationDurationMs += Number(event.data.duration_ms);
      if (typeof event.job_id === "string" && event.job_id && typeof event.data?.job_type === "string") {
        const values = jobEvents.get(event.job_id) ?? [];
        values.push(event);
        jobEvents.set(event.job_id, values);
      }
    }
    producers.push({
      file: relative,
      producer_id: first.producer_id,
      clock_domain: first.clock_domain,
      event_count: events.length,
      first_timestamp_utc: first.timestamp_utc,
      last_timestamp_utc: last.timestamp_utc,
      local_duration_ms: Math.max(0, Number(last.monotonic_elapsed_ms) - Number(first.monotonic_elapsed_ms)),
      bytes,
      source_bytes: events.reduce((sum, event) => sum + Number(event.data?.source_bytes ?? 0), 0),
      mcp_started: events.filter((event) => event.event_type === "mcp.tool.started").length,
      mcp_completed: events.filter((event) => event.event_type === "mcp.tool.completed").length,
    });
  }
  const hostSpans = matchingFilesRecursive(path.join(attemptRoot, "payload", "stages"), (name) => name.endsWith(".timing.json")).map((relative) => {
    const value = JSON.parse(fs.readFileSync(path.join(attemptRoot, "payload", "stages", relative), "utf8"));
    transferBytes += Number(value.request_bytes ?? 0) + Number(value.response_bytes ?? 0);
    if (value.retries) retryEvents += Number(value.retries);
    if (value.timed_out) timeoutEvents += 1;
    return { file: `stages/${relative}`, ...value };
  });
  const serverJobSpans = [...jobEvents.entries()].map(([jobId, events]) => {
    const ordered = [...events].sort((left, right) => left.seq - right.seq);
    const first = ordered[0];
    const last = ordered.at(-1);
    return {
      job_id: jobId,
      job_type: first.data.job_type,
      producer_id: first.producer_id,
      clock_domain: first.clock_domain,
      first_timestamp_utc: first.timestamp_utc,
      last_timestamp_utc: last.timestamp_utc,
      local_duration_ms: Math.max(0, Number(last.monotonic_elapsed_ms) - Number(first.monotonic_elapsed_ms)),
      event_count: ordered.length,
    };
  }).sort((left, right) => left.job_id.localeCompare(right.job_id));
  const stages = (candidate.stages ?? []).map((stage) => ({
    id: stage.id,
    result_source: stage.result_source,
    status: stage.status,
    elapsed_seconds: stage.elapsed_seconds,
    performance_status: stage.performance_status,
    performance_identity: stage.performance_identity,
  }));
  return {
    schema_version: 2,
    status: "PASS",
    authority: "indexed-from-sealed-gate-and-producer-evidence",
    cross_clock_subtraction_forbidden: true,
    stages,
    producers,
    host_spans: hostSpans,
    server_job_spans: serverJobSpans,
    totals: {
      event_bytes: totalBytes,
      transfer_bytes: transferBytes,
      retry_events: retryEvents,
      timeout_events: timeoutEvents,
      server_operation_duration_ms: Math.round(serverOperationDurationMs * 1000) / 1000,
      correlation_ids: ids.correlation_id.size,
      request_ids: ids.request_id.size,
      case_ids: ids.case_id.size,
      job_ids: ids.job_id.size,
    },
  };
}
