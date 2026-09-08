// The first-party CrossJob website driver. All product input is user text or
// attachment metadata; no Case construction or domain command shortcut exists.
import crypto from "node:crypto";
import { pathToFileURL } from "node:url";

const HASH = /^[a-f0-9]{64}$/;
const EVENT_FIELDS = ["schema_version", "sequence", "conversation_id", "case_id", "job_id", "type", "created_at", "data"].sort();
const sha = (value) => crypto.createHash("sha256").update(value).digest("hex");
function check(value, code) { if (!value) throw new Error(code); }

export function websiteUserDescription(driver, requirements) {
  const names = requirements.filter((item) => item.status === "OPEN" && item.kind === "INPUT").map((item) => item.name);
  check(names.length > 0 && new Set(names).size === names.length, "WEBSITE_INPUT_REQUIREMENTS_MISSING");
  return names.map((name) => {
    const index = driver.initial_user_fact_names.indexOf(name);
    check(index >= 0, "WEBSITE_REQUIRED_INPUT_UNAVAILABLE");
    return `补充信息（${name}）：${driver.initial_user_fact_values[index]}`;
  }).join("\n");
}

function checkInitialCase(view, caseView, raw) {
  check(view.case_id && caseView?.case_id === view.case_id && view.case_status === "WAITING_INPUT"
    && caseView.status === "WAITING_INPUT", "WEBSITE_CASE_FIRST_REQUIRED");
  const expected = { statement: raw, expected_behavior: "用户未单独说明；以 raw_problem_text 为准。",
    actual_behavior: raw, scope: "仅定位 raw_problem_text 所述问题。", goals: ["定位问题原因并给出结论。"],
    non_goals: [], constraints: [], completion_criteria: ["给出基于证据的结论；证据不足时明确说明。"] };
  check(caseView.raw_problem_text === raw && caseView.user_facts?.length === 0
    && Object.entries(expected).every(([name, value]) => JSON.stringify(caseView.problem_spec?.[name]) === JSON.stringify(value)), "WEBSITE_CASE_FIRST_DEFAULTS");
  const questions = caseView.pending_requirements.filter((item) => item.status === "OPEN").map((item) => item.prompt);
  check(questions.length > 0 && JSON.stringify(view.current_questions) === JSON.stringify(questions), "WEBSITE_REQUIREMENT_PROMPTS_CHANGED");
  return questions;
}

export function parseSseFrame(frame, conversationId, after) {
  const lines = frame.replace(/\r?\n\r?\n$/, "").split(/\r?\n/);
  if ((lines.length === 1 && lines[0] === "") || lines.every((line) => line.startsWith(":"))) return null;
  check(lines.length === 1 && lines[0].startsWith("data: "), "WEBSITE_SSE_FRAME_FORMAT");
  const event = JSON.parse(lines[0].slice("data: ".length));
  check(JSON.stringify(Object.keys(event).sort()) === JSON.stringify(EVENT_FIELDS), "WEBSITE_SSE_FIELDS");
  check(event.schema_version === 1 && event.conversation_id === conversationId && event.sequence === after + 1
    && typeof event.type === "string", "WEBSITE_SSE_SEQUENCE");
  check(event.type !== "agent.failed" && event.type !== "conversation.interrupted", "WEBSITE_AGENT_FAILED");
  if (event.type === "agent.progress") check(typeof event.data.message === "string" && /[\u3400-\u9fff]/u.test(event.data.message), "WEBSITE_PROGRESS_CHINESE");
  return event;
}

export async function runWebsiteStep(input, fetchImpl = fetch) {
  const records = [], events = [];
  const base = input.public_base_url.replace(/\/$/, "");
  let conversationId = input.conversation_id ?? null;
  const request = async (method, route, body = undefined) => {
    const response = await fetchImpl(base + route, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(30_000) });
    const value = await response.json();
    records.push({ method, path: route, request: body ?? null, status: response.status, response: value,
      correlation_id: response.headers.get("x-problem-locator-correlation-id") });
    check(response.ok && value.ok === true && value.error === null, "WEBSITE_HTTP_REJECTED");
    return value.data;
  };
  const observe = async (cursor, action, done) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), input.timeout_ms ?? 660_000);
    const path = `/api/v1/agent/conversations/${conversationId}/events`;
    let text = "", raw = "", count = 0;
    try {
      const response = await fetchImpl(base + path, { headers: { "Last-Event-ID": String(cursor) }, signal: controller.signal });
      check(response.ok && response.headers.get("content-type")?.startsWith("text/event-stream"), "WEBSITE_SSE_UNAVAILABLE");
      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8", { fatal: true });
      if (action) await action();
      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) { check(done(null), "WEBSITE_SSE_CLOSED_EARLY"); break; }
        const decoded = decoder.decode(chunk.value, { stream: true });
        raw += decoded; text += decoded;
        check(Buffer.byteLength(raw) <= 4_194_304, "WEBSITE_SSE_LIMIT");
        for (;;) {
          const match = /\r?\n\r?\n/.exec(text);
          if (!match) break;
          const frame = text.slice(0, match.index); text = text.slice(match.index + match[0].length);
          const event = parseSseFrame(frame, conversationId, cursor + count);
          if (event) {
            events.push(event); count += 1;
            process.stderr.write("TEST_FLOW_PROGRESS stage.progress website.agent.event\n");
          }
          if (event && done(event)) { await reader.cancel(); return; }
        }
      }
    } finally {
      clearTimeout(timer); controller.abort();
      records.push({ method: "GET", path, last_event_id: cursor, raw_sse: raw, event_count: count, sha256: sha(raw) });
    }
  };
  const cursor = () => events.at(-1)?.sequence ?? input.cursor ?? 0;
  const message = (suffix, text, attachment_ids = []) => request("POST", `/api/v1/agent/conversations/${conversationId}/messages`, { request_id: input.request_id + suffix, text, attachment_ids });
  if (input.phase === "route") {
    const created = await request("POST", "/api/v1/agent/conversations", { request_id: input.request_id });
    conversationId = created.conversation_id;
    const replay = await request("POST", "/api/v1/agent/conversations", { request_id: input.request_id });
    check(JSON.stringify(replay) === JSON.stringify(created), "WEBSITE_CREATE_REPLAY_CHANGED");
    const raw = input.driver.problem.raw_problem_text ?? input.driver.problem.statement;
    await observe(0, () => message("-initial", raw), (event) => event?.type === "assistant.question");
    check(events.at(-1).case_id, "WEBSITE_QUESTION_BEFORE_CASE");
    const initial_view = await request("GET", `/api/v1/agent/conversations/${conversationId}`);
    const initial_case_response = await request("GET", `/api/v1/cases/${initial_view.case_id}`);
    const questions = checkInitialCase(initial_view, initial_case_response.case_view, raw);
    check(JSON.stringify(events.at(-1).data.questions) === JSON.stringify(questions), "WEBSITE_REQUIREMENT_PROMPTS_CHANGED");
    const complete = websiteUserDescription(input.driver, initial_case_response.case_view.pending_requirements);
    await observe(cursor(), () => message("-details", complete), (event) => event?.type === "case.updated" && ["WAITING_INPUT", "WAITING_ATTACHMENT"].includes(event.data.status));
    const view = await request("GET", `/api/v1/agent/conversations/${conversationId}`);
    check(view.case_id === initial_view.case_id && view.case_status === "WAITING_ATTACHMENT", "WEBSITE_REQUIRED_FACTS_NOT_EXTRACTED");
    const case_response = await request("GET", `/api/v1/cases/${view.case_id}`);
    return { schema_version: 1, phase: input.phase, conversation_id: conversationId, initial_view, initial_case_response, view, case_response, events, records };
  }
  if (input.phase === "prepare") {
    const prepared = await request("POST", `/api/v1/agent/conversations/${conversationId}/attachments`, {
      request_id: input.request_id, name: input.archive.name, content_type: input.archive.content_type,
      declared_size: input.archive.size, declared_sha256: input.archive.sha256,
    });
    return { schema_version: 1, phase: input.phase, conversation_id: conversationId, prepared, events, records };
  }
  if (input.phase === "upload") {
    await observe(input.cursor, null, (event) => event?.type === "attachment.updated" && event.data.status === "READY");
  } else if (input.phase === "diagnose") {
    await observe(input.cursor, () => message("-logs", "请用附件中的日志继续定位。", [input.attachment_id]),
      (event) => event?.type === "conversation.completed" && event.data.status === "COMPLETED");
    check(events.some((event) => event.type === "result.available"), "WEBSITE_RESULT_EVENT_MISSING");
    check(events.some((event) => event.type === "case.updated" && event.data.status === "REVIEWING"), "WEBSITE_REVIEW_NOT_OBSERVED");
  } else if (input.phase === "restart") {
    const expected = input.expected_events;
    check(Array.isArray(expected) && expected.length > 0, "WEBSITE_REPLAY_EXPECTATION_MISSING");
    await observe(input.cursor, null, (event) => event?.sequence === expected.at(-1).sequence);
    check(JSON.stringify(events) === JSON.stringify(expected), "WEBSITE_RESTART_EVENTS_CHANGED");
  } else throw new Error("WEBSITE_PHASE_UNKNOWN");
  const view = await request("GET", `/api/v1/agent/conversations/${conversationId}`);
  check(view.conversation_id === conversationId && view.case_id === input.case_id, "WEBSITE_CASE_IDENTITY_CHANGED");
  if (["diagnose", "restart"].includes(input.phase)) check(view.status === "COMPLETED" && view.archive_status === "READY", "WEBSITE_FINAL_STATE_INVALID");
  const result = { schema_version: 1, phase: input.phase, conversation_id: conversationId, view, events, records };
  if (["diagnose", "restart"].includes(input.phase)) {
    result.case_response = await request("GET", `/api/v1/cases/${view.case_id}`);
    result.artifacts_response = await request("GET", `/api/v1/cases/${view.case_id}/artifacts`);
  }
  return result;
}

export function validateWebsiteEvidence(evidence, expected = {}) {
  check(evidence?.schema_version === 1 && Array.isArray(evidence.events) && Array.isArray(evidence.records), "WEBSITE_EVIDENCE_INVALID");
  check(!expected.phase || evidence.phase === expected.phase, "WEBSITE_EVIDENCE_PHASE");
  check(!expected.conversation_id || evidence.conversation_id === expected.conversation_id, "WEBSITE_EVIDENCE_IDENTITY");
  const streams = evidence.records.filter((record) => Object.hasOwn(record, "raw_sse"));
  const replayed = [];
  for (const stream of streams) {
    check(HASH.test(stream.sha256) && sha(stream.raw_sse) === stream.sha256, "WEBSITE_EVIDENCE_SSE_HASH");
    let cursor = stream.last_event_id;
    check(Number.isSafeInteger(cursor) && cursor >= 0 && Number.isSafeInteger(stream.event_count) && stream.event_count > 0, "WEBSITE_EVIDENCE_CURSOR");
    for (const frame of stream.raw_sse.split(/\r?\n\r?\n/).slice(0, -1)) {
      const event = parseSseFrame(frame, evidence.conversation_id, cursor);
      if (event) { if (cursor - stream.last_event_id < stream.event_count) replayed.push(event); cursor += 1; }
    }
    // The network chunk can contain frames beyond the stop condition. Those
    // remain raw evidence, while the consumed prefix alone advances the cursor.
    check(cursor - stream.last_event_id >= stream.event_count, "WEBSITE_EVIDENCE_SSE_COUNT");
  }
  check(JSON.stringify(evidence.events) === JSON.stringify(replayed), "WEBSITE_EVIDENCE_EVENT_SOURCE");
  check(evidence.records.filter((record) => !Object.hasOwn(record, "raw_sse")).every((record) => record.status === 200
    && record.response?.ok === true && record.response.error === null), "WEBSITE_EVIDENCE_HTTP_STATUS");
  if (evidence.phase === "route") {
    const creates = evidence.records.filter((record) => record.method === "POST" && record.path === "/api/v1/agent/conversations");
    const messages = evidence.records.filter((record) => record.method === "POST" && record.path.endsWith("/messages"));
    check(creates.length === 2 && messages.length === 2 && messages.every((record) => Object.keys(record.request).sort().join(",") === "attachment_ids,request_id,text"), "WEBSITE_EVIDENCE_RAW_INPUT");
    const initialCase = evidence.initial_case_response?.case_view;
    const questions = checkInitialCase(evidence.initial_view, initialCase, messages[0].request.text);
    check(evidence.records.some((record) => record.path === `/api/v1/cases/${initialCase.case_id}`
      && JSON.stringify(record.response?.data) === JSON.stringify(evidence.initial_case_response)), "WEBSITE_EVIDENCE_INITIAL_CASE_SOURCE");
    const asked = evidence.events.filter((event) => event.type === "assistant.question");
    check(asked.length > 0 && asked.every((event) => event.case_id === initialCase.case_id)
      && JSON.stringify(asked[0].data.questions) === JSON.stringify(questions), "WEBSITE_EVIDENCE_REQUIREMENT_QUESTIONS");
    check(evidence.view.case_status === "WAITING_ATTACHMENT" && evidence.view.case_id === initialCase.case_id, "WEBSITE_EVIDENCE_WAITING_ATTACHMENT");
  }
  if (evidence.phase === "diagnose") {
    const reportIndex = evidence.events.findIndex((event) => event.type === "result.available");
    const reviewIndex = evidence.events.findIndex((event) => event.type === "case.updated" && event.data.status === "REVIEWING");
    const pendingIndex = evidence.events.findIndex((event) => event.type === "archive.updated" && event.data.status === "PENDING");
    const archiveIndex = evidence.events.findIndex((event) => event.type === "archive.updated" && event.data.status === "READY");
    const completedIndex = evidence.events.findIndex((event) => event.type === "conversation.completed" && event.data.status === "COMPLETED");
    check(reportIndex >= 0 && pendingIndex > reportIndex && archiveIndex > pendingIndex
      && completedIndex > archiveIndex && evidence.view.status === "COMPLETED", "WEBSITE_EVIDENCE_PUBLICATION_ORDER");
    check(reviewIndex >= 0 && reviewIndex < reportIndex, "WEBSITE_EVIDENCE_REVIEW");
    check(!evidence.events.slice(0, reportIndex).some((event) => event.type === "archive.updated" && event.data.artifacts?.length), "WEBSITE_EVIDENCE_PREMATURE_RESULT");
  }
  return true;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const result = await runWebsiteStep(JSON.parse(process.argv[2]));
  process.stdout.write(JSON.stringify(result));
}
