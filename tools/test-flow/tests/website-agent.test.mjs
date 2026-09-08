import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import fs from "node:fs";
import test from "node:test";
import { runWebsiteStep, parseSseFrame, validateWebsiteEvidence, websiteUserDescription } from "../lib/website-agent.mjs";

const conversationId = "conversation-1", caseId = "case-1";
const driver = { problem: { raw_problem_text: "订单超时，请定位。", statement: "订单超时", expected_behavior: "正常响应", actual_behavior: "等待超时", scope: "一个订单" }, initial_user_fact_names: ["order_id"], initial_user_fact_values: ["ORDER-123"] };
const requirements = [{ status: "OPEN", kind: "INPUT", name: "order_id", prompt: "请提供订单 ID。" }];
const initialCase = { case_id: caseId, status: "WAITING_INPUT", raw_problem_text: driver.problem.raw_problem_text,
  problem_spec: { statement: driver.problem.raw_problem_text, actual_behavior: driver.problem.raw_problem_text,
    expected_behavior: "用户未单独说明；以 raw_problem_text 为准。", scope: "仅定位 raw_problem_text 所述问题。",
    goals: ["定位问题原因并给出结论。"], non_goals: [], constraints: [], completion_criteria: ["给出基于证据的结论；证据不足时明确说明。"] },
  user_facts: [], pending_requirements: requirements };
const event = (sequence, type, data = {}, withCase = false) => ({ schema_version: 1, sequence, conversation_id: conversationId, case_id: withCase ? caseId : null, job_id: null, type, created_at: "2026-09-07T00:00:00Z", data });
const frame = (value) => `data: ${JSON.stringify(value)}\n\n`;
const json = (data) => Response.json({ ok: true, data, error: null }, { headers: { "x-problem-locator-correlation-id": "test-correlation" } });
const sse = (events) => new Response(": connected\n\n: heartbeat\n\n" + events.map(frame).join(""), { headers: { "Content-Type": "text/event-stream" } });

async function route({ beforeCaseQuestion = false, caseOverride = {} } = {}) {
  const calls = []; let streams = 0;
  const fetcher = async (url, options) => {
    calls.push({ path: new URL(url).pathname, ...options });
    if (url.endsWith("/events")) {
      assert.equal(options.headers["Last-Event-ID"], streams === 0 ? "0" : "3");
      return streams++ === 0 ? sse([event(1, "message.accepted"), event(2, "case.updated", { status: "WAITING_INPUT", case_revision: 2 }, true),
        event(3, "assistant.question", { questions: [requirements[0].prompt] }, !beforeCaseQuestion)])
        : sse([event(4, "message.accepted", {}, true), event(5, "agent.progress", { message: "正在整理补充信息" }, true), event(6, "case.updated", { status: "WAITING_ATTACHMENT", case_revision: 3 }, true)]);
    }
    if (url.endsWith("/conversations")) return json({ conversation_id: conversationId, request_id: "route", schema_version: 1 });
    if (url.endsWith("/messages")) {
      const body = JSON.parse(options.body);
      assert.deepEqual(Object.keys(body).sort(), ["attachment_ids", "request_id", "text"]);
      assert.equal(Object.hasOwn(body, "problem_spec"), false);
      return json({ conversation_id: conversationId, message_id: "msg", request_id: body.request_id, event_id: streams, status: "ACCEPTED" });
    }
    if (url.includes("/cases/")) return json({ case_view: { ...initialCase, ...caseOverride } });
    return json({ conversation_id: conversationId, case_id: caseId, case_status: streams === 1 ? "WAITING_INPUT" : "WAITING_ATTACHMENT", status: "WAITING_INPUT", current_questions: [requirements[0].prompt] });
  };
  const evidence = await runWebsiteStep({ phase: "route", public_base_url: "http://localhost", request_id: "route", driver }, fetcher);
  return { evidence, calls };
}

test("website route creates from sparse raw text before OPEN requirements, reconnects SSE and supplements the same Case", async () => {
  const { evidence, calls } = await route();
  assert.equal(validateWebsiteEvidence(evidence, { phase: "route", conversation_id: conversationId }), true);
  assert.equal(calls.filter((item) => item.path.endsWith("/messages")).length, 2);
  assert.equal(JSON.parse(calls.find((item) => item.path.endsWith("/messages")).body).text, driver.problem.raw_problem_text);
  assert.match(calls.findLast((item) => item.path.endsWith("/messages")).body, /补充信息（order_id）：ORDER-123/);
  assert.deepEqual(evidence.initial_case_response.case_view.user_facts, []);
  assert.deepEqual(evidence.events.map((item) => item.sequence), [1, 2, 3, 4, 5, 6]);
  assert.deepEqual(evidence.records.filter((item) => "raw_sse" in item).map((item) => item.last_event_id), [0, 3]);
});

test("website supplement contains only requested OPEN INPUT values", () => {
  const text = websiteUserDescription(driver, [...requirements, { status: "FULFILLED", kind: "INPUT", name: "old" }, { status: "OPEN", kind: "ATTACHMENT", name: "logs" }]);
  assert.equal(text, "补充信息（order_id）：ORDER-123");
  for (const value of Object.values(driver.problem)) assert.ok(!text.includes(value));
  assert.doesNotMatch(text, /problem_spec|completion_criteria|safety_constraints/);
  assert.throws(() => websiteUserDescription(driver, []), /INPUT_REQUIREMENTS_MISSING/);
  assert.throws(() => websiteUserDescription(driver, [{ status: "OPEN", kind: "INPUT", name: "unknown" }]), /REQUIRED_INPUT_UNAVAILABLE/);
});

test("website route rejects creation-time intake questions, inferred facts and non-neutral defaults", async () => {
  await assert.rejects(route({ beforeCaseQuestion: true }), /QUESTION_BEFORE_CASE/);
  await assert.rejects(route({ caseOverride: { user_facts: [{ name: "order_id", value: "ORDER-123" }] } }), /CASE_FIRST_DEFAULTS/);
  await assert.rejects(route({ caseOverride: { problem_spec: { ...initialCase.problem_spec, expected_behavior: "正常响应" } } }), /CASE_FIRST_DEFAULTS/);
  await assert.rejects(route({ caseOverride: { pending_requirements: [{ ...requirements[0], prompt: "请补充预期行为。" }] } }), /REQUIREMENT_PROMPTS_CHANGED/);
});

test("SSE parser accepts one data-only business line and ignores connection comments", () => {
  const value = event(1, "agent.progress", { message: "正在核对证据\n请稍候" });
  assert.deepEqual(parseSseFrame(`data: ${JSON.stringify(value)}`, conversationId, 0), value);
  assert.equal(parseSseFrame(": connected", conversationId, 0), null);
  assert.equal(parseSseFrame(": heartbeat", conversationId, 0), null);
});

test("SSE parser rejects named fields, retry fields, multiple data lines and unframed content", () => {
  const value = event(1, "agent.progress", { message: "正在核对证据" });
  const data = `data: ${JSON.stringify(value)}`;
  for (const invalid of [
    `id: 1\nevent: agent.progress\n${data}`,
    `id: 1\n${data}`,
    `event: agent.progress\n${data}`,
    `retry: 2000\n${data}`,
    `${data}\n${data}`,
    `data: {\ndata: "sequence":1}`,
    `${data}\n: heartbeat`,
    "retry: 2000",
    JSON.stringify(value),
  ]) assert.throws(() => parseSseFrame(invalid, conversationId, 0), /FRAME_FORMAT/);
});

test("website stream handles UTF-8, escaped newlines and frame separators split across network chunks", async () => {
  const expected = [event(1, "agent.progress", { message: "正在解析日志\n请稍候" }),
    event(2, "attachment.updated", { status: "READY", name: "中文日志.zip" }, true)];
  const wire = ": connected\n\n: heartbeat\n\n" + expected.map(frame).join("");
  const bytes = new TextEncoder().encode(wire);
  let offset = 0;
  const body = new ReadableStream({ pull(controller) {
    if (offset === bytes.length) controller.close();
    else controller.enqueue(bytes.slice(offset, ++offset));
  } });
  const evidence = await runWebsiteStep({ phase: "upload", public_base_url: "http://localhost", conversation_id: conversationId, case_id: caseId, cursor: 0 }, async (url, options) => {
    if (url.endsWith("/events")) {
      assert.equal(options.headers["Last-Event-ID"], "0");
      return new Response(body, { headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
    }
    return json({ conversation_id: conversationId, case_id: caseId, status: "RUNNING" });
  });
  assert.deepEqual(evidence.events, expected);
  assert.equal(evidence.records[0].raw_sse, wire);
  assert.equal(validateWebsiteEvidence(evidence), true);
});

test("SSE parser rejects a gap, wrong conversation, internal fields and execution failures", () => {
  assert.equal(parseSseFrame(": heartbeat", conversationId, 0), null);
  assert.throws(() => parseSseFrame(frame(event(2, "message.accepted")), conversationId, 0), /SEQUENCE/);
  assert.throws(() => parseSseFrame(frame(event(1, "message.accepted")), "other", 0), /SEQUENCE/);
  assert.throws(() => parseSseFrame(frame(event(1, null)), conversationId, 0), /SEQUENCE/);
  assert.throws(() => parseSseFrame(frame({ ...event(1, "message.accepted"), storage_path: "/private" }), conversationId, 0), /FIELDS/);
  assert.throws(() => parseSseFrame(frame(event(1, "agent.failed")), conversationId, 0), /AGENT_FAILED/);
  assert.throws(() => parseSseFrame(frame(event(1, "agent.progress", { message: "internal-only" })), conversationId, 0), /CHINESE/);
});

test("website evidence detects changed network bytes and omitted or reordered observed events", async () => {
  const { evidence } = await route();
  const changed = structuredClone(evidence);
  changed.records.find((item) => "raw_sse" in item).raw_sse += "tampered";
  assert.throws(() => validateWebsiteEvidence(changed), /SSE_HASH/);
  const omitted = structuredClone(evidence); omitted.events.splice(1, 1);
  assert.throws(() => validateWebsiteEvidence(omitted), /EVENT_SOURCE/);
  const reordered = structuredClone(evidence); reordered.events.reverse();
  assert.throws(() => validateWebsiteEvidence(reordered), /EVENT_SOURCE/);
  assert.throws(() => validateWebsiteEvidence(evidence, { phase: "diagnose" }), /EVIDENCE_PHASE/);
});

const finalEvents = [event(6, "case.updated", { status: "REVIEWING", case_revision: 8 }, true), event(7, "result.available", { status: "RESOLVED", artifacts: [{ kind: "USER_RESULT" }], result_field: "final_result" }, true), event(8, "archive.updated", { status: "PENDING", artifacts: [] }, true), event(9, "archive.updated", { status: "READY", artifacts: [{ kind: "USER_RESULT_ARCHIVE" }] }, true), event(10, "conversation.completed", { status: "COMPLETED" }, true)];
async function diagnosisOrRestart(phase = "diagnose", replayEvents = finalEvents.slice(-2)) {
  const input = { phase, public_base_url: "http://localhost", request_id: phase, conversation_id: conversationId, case_id: caseId, cursor: phase === "diagnose" ? 5 : 8, attachment_id: "log-1", expected_events: finalEvents.slice(-2) };
  const fetcher = async (url, options) => {
    if (url.endsWith("/events")) return sse(phase === "diagnose" ? finalEvents : replayEvents);
    if (url.endsWith("/messages")) { assert.deepEqual(JSON.parse(options.body).attachment_ids, ["log-1"]); assert.match(JSON.parse(options.body).text, /日志/); return json({ status: "ACCEPTED" }); }
    if (url.endsWith("/artifacts")) return json({ artifacts: [] });
    if (url.includes("/cases/")) return json({ case_view: { case_id: caseId, status: "RESOLVED" } });
    return json({ conversation_id: conversationId, case_id: caseId, status: "COMPLETED", archive_status: "READY" });
  };
  return runWebsiteStep(input, fetcher);
}

test("diagnosis submits logs by message and proves REVIEWING then JSON before deferred ZIP", async () => {
  const evidence = await diagnosisOrRestart();
  assert.equal(validateWebsiteEvidence(evidence), true);
  assert.deepEqual(evidence.events.map((item) => item.type), ["case.updated", "result.available", "archive.updated", "archive.updated", "conversation.completed"]);
  assert.deepEqual(evidence.events.filter((item) => item.type === "archive.updated").map((item) => item.data.status), ["PENDING", "READY"]);
  const missingPending = structuredClone(evidence);
  const pending = missingPending.events.find((item) => item.type === "archive.updated" && item.data.status === "PENDING");
  pending.data.status = "READY";
  const stream = missingPending.records.find((item) => "raw_sse" in item);
  stream.raw_sse = ": heartbeat\n\n" + missingPending.events.map(frame).join("");
  stream.sha256 = createHash("sha256").update(stream.raw_sse).digest("hex");
  assert.throws(() => validateWebsiteEvidence(missingPending), /PUBLICATION_ORDER/);
});

test("restart replays immutable completed conversation events with Last-Event-ID", async () => {
  const evidence = await diagnosisOrRestart("restart");
  assert.equal(validateWebsiteEvidence(evidence), true);
  assert.equal(evidence.records[0].last_event_id, 8);
  const changed = structuredClone(finalEvents.slice(-2)); changed[0].data.status = "FAILED";
  await assert.rejects(() => diagnosisOrRestart("restart", changed), /RESTART_EVENTS_CHANGED/);
});

test("failed HTTP and incomplete event streams never produce a website PASS receipt", async () => {
  await assert.rejects(() => runWebsiteStep({ phase: "route", public_base_url: "http://localhost", request_id: "r", driver }, async () => Response.json({ ok: false, error: { code: "FAIL" } })), /HTTP_REJECTED/);
  await assert.rejects(() => runWebsiteStep({ phase: "upload", public_base_url: "http://localhost", conversation_id: conversationId, case_id: caseId, cursor: 0 }, async () => sse([])), /CLOSED_EARLY/);
});

test("official website gates freeze example ownership checks and disable active-Case checkpoint reuse", () => {
  const config = (name) => JSON.parse(fs.readFileSync(new URL(`../config/${name}.v2.json`, import.meta.url), "utf8"));
  const stages = config("stages").stages;
  assert.ok(stages.find((item) => item.id === "deterministic.full").gates.includes("det.website-example"));
  assert.deepEqual(config("gates").gates["det.website-example"].test_files, ["examples/website-agent/server.test.mjs"]);
  assert.ok(config("identities").components["proof.deterministic"].paths.includes("examples/website-agent"));
  for (const stage of stages.filter((item) => item.id.startsWith("journey.cross-job.") && !item.id.endsWith("environment"))) {
    assert.deepEqual(stage.reuse, { dev: "never", release: "never" });
    assert.equal(Object.hasOwn(stage, "checkpoint"), false);
  }
});
