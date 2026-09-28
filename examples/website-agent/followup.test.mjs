import assert from "node:assert/strict";
import { once } from "node:events";
import { createHash } from "node:crypto";
import test from "node:test";
import { createAgentClient, AgentApiError } from "./browser-client.js";
import { sendConversationInput } from "./conversation-input.js";
import { createFollowupController } from "./followup-controller.js";
import { mountFollowupView } from "./followup-view.js";
import { createAgentBackend, HttpError } from "./server.mjs";
import { createPreviewApi } from "./preview-model.js";
import { previewSamples } from "./preview.mjs";

const cid = "10000000-0000-0000-0000-000000000001", rid = "20000000-0000-0000-0000-000000000001";
const fid = "30000000-0000-0000-0000-000000000001", other = "40000000-0000-0000-0000-000000000001";
const path = `/api/agent/conversations/${cid}/runs/${rid}/followups`, timestamp = "2026-09-24T00:00:00.000Z";
const item = (overrides = {}) => ({ followup_id: fid, run_id: rid, request_id: "question-1", ordinal: 1,
  status: "COMPLETED", context_mode: "REPORT_ONLY", text: "为什么这样判断？", answer_markdown: "依据已保存的报告。",
  failure: null, created_at: timestamp, updated_at: timestamp, ...overrides });
const view = (overrides = {}) => ({ schema_version: 1, conversation_id: cid, run_id: rid, can_ask: true, reason: null,
  snapshot_status: "UNAVAILABLE", active_followup: null, items: [], next_cursor: null, last_event_id: 0, ...overrides });
const receipt = (overrides = {}) => ({ conversation_id: cid, run_id: rid, followup_id: fid,
  request_id: "question-1", event_id: 1, status: "ACCEPTED", ...overrides });
const event = (sequence, data = item()) => ({ schema_version: 1, sequence, conversation_id: cid, run_id: rid,
  followup_id: data.followup_id, type: "followup.updated", created_at: timestamp, data });
const envelope = (data) => new Response(JSON.stringify({ ok: true, data, error: null }), { headers: { "Content-Type": "application/json" } });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const storage = () => { const values = new Map(); return { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value), values }; };
const tick = () => new Promise((resolve) => setImmediate(resolve));
function controlled(options = {}) {
  const streams = [], timers = [], memory = options.storage ?? storage();
  const client = { conversations: { send: () => assert.fail("追问不能退回旧消息接口") }, followups: {
    list: async () => view(), send: async (_cid, _rid, input) => receipt({ request_id: input.request_id }),
    stop: async (_cid, _rid, _fid, input) => receipt({ run_id: _rid, followup_id: _fid, request_id: input.request_id, status: "CANCELLED" }),
    events: (_cid, _rid, args) => { streams.push(args); return new Promise((resolve) => args.signal.addEventListener("abort", resolve, { once: true })); },
    ...options.methods,
  } };
  const controller = createFollowupController({ client, conversationId: cid, runId: rid, storage: memory,
    makeRequestId: options.makeRequestId ?? (() => "question-1"), schedule: (work) => { timers.push(work); return work; },
    cancelSchedule: (work) => { const index = timers.indexOf(work); if (index >= 0) timers.splice(index, 1); } });
  return { controller, streams, timers, memory, client };
}

test("followup SDK preserves identities, explicit cursor, CSRF and original send target_run_id", async () => {
  const calls = [], client = createAgentClient({ headers: () => ({ "X-CSRF-Token": "fresh", "X-Agent-Owner-Key": "forged" }),
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      if (url.endsWith("/messages")) return envelope({ accepted: true });
      return envelope(init.method === "POST" ? receipt({ status: url.endsWith("/stop") ? "CANCELLED" : "ACCEPTED" }) : view());
    } });
  await client.followups.list(cid, rid, { cursor: "older+/=", limit: 2 });
  await client.followups.send(cid, rid, { request_id: "question-1", text: "问题" });
  await client.followups.stop(cid, rid, fid, { request_id: "question-1" });
  await client.conversations.send(cid, { request_id: "old", text: "补充", target_run_id: rid });
  assert.deepEqual(calls.map((call) => call.url), [`${path}?cursor=older%2B%2F%3D&limit=2`, path, `${path}/${fid}/stop`, `/api/agent/conversations/${cid}/messages`]);
  assert.equal(JSON.parse(calls[3].init.body).target_run_id, rid);
  for (const { init } of calls) {
    assert.equal(init.headers.get("X-CSRF-Token"), "fresh"); assert.equal(init.headers.has("X-Agent-Owner-Key"), false);
    assert.equal(init.credentials, "same-origin"); assert.equal(init.redirect, "error");
  }
  for (const input of [{ request_id: "x", text: " " }, { request_id: "x", text: "中".repeat(21846) },
    { request_id: "x", text: "正常", attachment_ids: [] }]) await assert.rejects(client.followups.send(cid, rid, input), TypeError);
});

test("new website supplements always target the selected active run", async () => {
  const requests = [], client = { conversations: {
    get: () => assert.fail("普通补充直接使用已保存的目标轮次"),
    send: async (...args) => { requests.push(args); return { status: "ACCEPTED" }; },
  }, followups: { send: () => assert.fail("未出报告不应提交报告追问") } };
  const result = await sendConversationInput({ client, conversationId: cid, runId: rid, requestId: "supplement", text: "补充事实", reportReady: false });
  assert.equal(result.kind, "message");
  assert.deepEqual(requests, [[cid, { request_id: "supplement", text: "补充事实", attachment_ids: [], target_run_id: rid }]]);
});

test("a report completion race refreshes the same run and switches once to followups with unchanged identity", async () => {
  const trace = [], client = { conversations: {
    get: async (_cid, query) => { trace.push("get"); assert.deepEqual(query, { include: [], run_id: rid });
      return { conversation_id: cid, selected_run_id: rid, report_state: "READY", current_run: { run_id: other } }; },
    send: async (_id, message) => { trace.push("message"); assert.equal(message.target_run_id, rid);
      throw new AgentApiError("报告刚刚生成", { status: 409, code: "AGENT_RUN_CHANGED" }); },
  }, followups: { send: async (...args) => { trace.push("followup"); assert.deepEqual(args, [cid, rid,
    { request_id: "same-request", text: "如何理解这个现象？" }]); return receipt({ request_id: "same-request" }); } } };
  const result = await sendConversationInput({ client, conversationId: cid, runId: rid, requestId: "same-request", text: "如何理解这个现象？", reportReady: false });
  assert.equal(result.kind, "followup"); assert.deepEqual(trace, ["message", "get", "followup"]);
});

test("followup rejection never falls back to messages, and completed reports cannot accept new logs", async () => {
  let asks = 0;
  const client = { conversations: { get: async () => ({ conversation_id: cid, selected_run_id: rid,
    current_run: { run_id: other }, report_state: "READY" }), send: () => assert.fail("追问失败不能退回旧消息") },
  followups: { send: async () => { asks++; throw new AgentApiError("功能关闭", { status: 409, code: "AGENT_FOLLOWUP_DISABLED" }); } } };
  await assert.rejects(sendConversationInput({ client, conversationId: cid, runId: rid, requestId: "one", text: "解释", reportReady: true }),
    (error) => error.code === "AGENT_FOLLOWUP_DISABLED");
  await assert.rejects(sendConversationInput({ client, conversationId: cid, runId: rid, requestId: "logs", attachmentIds: [fid], reportReady: true }),
    (error) => error.code === "WEBSITE_FOLLOWUP_TEXT_ONLY");
  assert.equal(asks, 1);
});

test("a lost diagnostic message response retries its original entry even after report completion", async () => {
  const messages = [], client = { conversations: {
    get: () => assert.fail("未知提交结果不能仅凭新快照改换提交入口"),
    send: async (_cid, input) => { messages.push(input); if (messages.length === 1) throw new AgentApiError("响应丢失"); return { status: "ACCEPTED" }; },
  }, followups: { send: () => assert.fail("已用于诊断的补充不能重复变成报告追问") } };
  const saved = { client, conversationId: cid, runId: rid, requestId: "original", text: "诊断中的补充", reportReady: false };
  await assert.rejects(sendConversationInput(saved));
  assert.equal((await sendConversationInput(saved)).kind, "message");
  assert.deepEqual(messages[0], messages[1]); assert.equal(messages[1].target_run_id, rid);
});

test("followup SDK validates run identity and parses fragmented data-only SSE with replay deduplication", async () => {
  const received = [], wire = `: connected\n\ndata: ${JSON.stringify(event(2))}\n\n: heartbeat\n\ndata: ${JSON.stringify(event(3))}\n\n`;
  const bytes = new TextEncoder().encode(wire);
  const client = createAgentClient({ fetchImpl: async (_url, init) => {
    assert.equal(init.headers.get("Last-Event-ID"), "2");
    return new Response(new ReadableStream({ start(controller) {
      for (let n = 0; n < bytes.length; n += 11) controller.enqueue(bytes.slice(n, n + 11)); controller.close();
    } }), { headers: { "Content-Type": "text/event-stream" } });
  } });
  await client.followups.events(cid, rid, { after: 2, onEvent: async (value) => { received.push(value.sequence); } });
  assert.deepEqual(received, [3]);
  const invalid = createAgentClient({ fetchImpl: async () => envelope(view({ run_id: other })) });
  await assert.rejects(invalid.followups.list(cid, rid), (error) => error.code === "WEBSITE_INVALID_RESPONSE");
  const wrongStream = createAgentClient({ fetchImpl: async () => new Response(`data: ${JSON.stringify({ ...event(1), run_id: other })}\n\n`, { headers: { "Content-Type": "text/event-stream" } }) });
  await assert.rejects(wrongStream.followups.events(cid, rid, { onEvent: () => assert.fail("不能接收另一轮事件") }), /不一致/);
});

test("refresh starts after atomic snapshot cursor and delayed submit receipt cannot revert a completed answer", async () => {
  const submission = deferred(); let read = 0;
  const box = controlled({ methods: { list: async () => ++read === 1 ? view({ last_event_id: 4 }) : view({ items: [item()], last_event_id: 6 }), send: () => submission.promise } });
  await box.controller.start(); assert.equal(box.streams[0].after, 4);
  box.controller.setDraft("为什么这样判断？"); const sent = box.controller.submit();
  await box.streams[0].onEvent(event(6));
  assert.equal(box.controller.getState().items[0].status, "COMPLETED");
  submission.resolve(receipt({ event_id: 5 })); await sent;
  assert.equal(box.controller.getState().items[0].answer_markdown, "依据已保存的报告。");
  assert.equal(box.streams.at(-1).after, 6); assert.equal(box.controller.getState().receipts[0].followup_id, fid);
  assert.equal(box.controller.getState().draft, ""); box.controller.destroy();
});

test("lost submission survives reload and explicit retry preserves ID and text without old-send fallback", async () => {
  const memory = storage(), calls = [];
  const box = controlled({ storage: memory, methods: { send: async (_cid, _rid, input) => {
    calls.push(input); throw new AgentApiError("连接中断");
  } } });
  await box.controller.start(); box.controller.setDraft("同一个问题"); await assert.rejects(box.controller.submit()); box.controller.destroy();
  const restored = controlled({ storage: memory, methods: { send: async (_cid, _rid, input) => { calls.push(input); return receipt(); } } });
  assert.equal(restored.controller.getState().draft, "", "授权查询前不展示本地草稿");
  await restored.controller.start(); assert.equal(restored.controller.getState().pending.request_id, "question-1");
  restored.controller.setDraft("不能偷偷改掉未确认请求"); await restored.controller.retryPending();
  assert.deepEqual(calls, Array(2).fill({ request_id: "question-1", text: "同一个问题" }));
  restored.controller.destroy();
});

test("older pagination cannot overwrite live completion or advance its event cursor", async () => {
  const page = deferred(), running = item({ ordinal: 2, status: "RUNNING", answer_markdown: null });
  const box = controlled({ methods: { list: async (_cid, _rid, args) => args.cursor ? page.promise :
    view({ items: [running], active_followup: running, can_ask: false, reason: "BUSY", last_event_id: 2, next_cursor: "older" }) } });
  await box.controller.start(); const older = box.controller.loadOlder();
  await box.streams[0].onEvent(event(3, item({ ordinal: 2 })));
  page.resolve(view({ items: [item({ followup_id: other, ordinal: 1 }), running], last_event_id: 99 })); await older;
  assert.equal(box.controller.getState().items.length, 2); assert.equal(box.controller.getState().items[1].status, "COMPLETED");
  assert.equal(box.controller.getState().last_event_id, 3); box.controller.destroy();
});

for (const lateFailure of [false, true]) {
  test(`destroyed controller's late ${lateFailure ? "rejection" : "receipt"} cannot erase a new instance's pending request`, async () => {
    const memory = storage(), oldResponse = deferred(), newResponse = deferred();
    const old = controlled({ storage: memory, methods: { send: () => oldResponse.promise } });
    await old.controller.start(); old.controller.setDraft("第一个问题");
    const first = old.controller.submit(); const firstResult = first.catch((error) => error); old.controller.destroy();
    // 新实例已从快照确认第一个问题，用户又提交了一个尚未收到回执的问题。
    const fresh = controlled({ storage: memory, makeRequestId: () => "question-2", methods: {
      list: async () => view({ items: [item({ text: "第一个问题" })], last_event_id: 3 }), send: () => newResponse.promise,
    } });
    await fresh.controller.start(); fresh.controller.setDraft("第二个问题"); const second = fresh.controller.submit();
    const savedBefore = [...memory.values.values()][0];
    assert.deepEqual(JSON.parse(savedBefore).pending, { request_id: "question-2", text: "第二个问题" });
    if (lateFailure) oldResponse.reject(new AgentApiError("迟到拒绝", { status: 409 }));
    else oldResponse.resolve(receipt());
    await firstResult;
    assert.equal([...memory.values.values()][0], savedBefore);
    assert.equal(fresh.controller.getState().draft, "第二个问题");
    newResponse.resolve(receipt({ request_id: "question-2", followup_id: other, event_id: 4 }));
    await second; fresh.controller.destroy();
  });
}

test("an old page rejection cannot replace the successful refreshed snapshot with an error", async () => {
  const older = deferred(); let snapshots = 0;
  const box = controlled({ methods: { list: async (_cid, _rid, args) => args.cursor ? older.promise :
    view({ items: [item()], last_event_id: ++snapshots, next_cursor: "older" }) } });
  await box.controller.start(); const previousPage = box.controller.loadOlder();
  await box.controller.refresh(); assert.equal(box.controller.getState().last_event_id, 2);
  older.reject(new Error("上一次分页的迟到失败")); await previousPage;
  assert.equal(box.controller.getState().error, null);
  assert.equal(box.controller.getState().items[0].answer_markdown, "依据已保存的报告。");
  assert.equal(box.controller.getState().last_event_id, 2); box.controller.destroy();
});

test("cross-run busy uses active run to stop and destroyed controllers ignore late snapshots", async () => {
  const calls = [], active = item({ run_id: other, status: "RUNNING", answer_markdown: null });
  const box = controlled({ methods: { list: async () => view({ active_followup: active, can_ask: false, reason: "BUSY" }),
    stop: async (...args) => { calls.push(args); return receipt({ run_id: other, status: "CANCELLED" }); } } });
  await box.controller.start(); await box.controller.stop(); assert.equal(calls[0][1], other);
  assert.deepEqual(box.controller.getState().items, []); box.controller.destroy();
  const pending = deferred(), late = controlled({ methods: { list: () => pending.promise } });
  let changes = 0; late.controller.subscribe(() => changes++); const started = late.controller.start(); late.controller.destroy(); const count = changes;
  pending.resolve(view({ items: [item()] })); await started; assert.equal(changes, count); assert.equal(late.streams.length, 0);
});

test("out-of-range followup cursor re-reads a snapshot instead of resubmitting", async () => {
  let reads = 0, subscriptions = 0;
  const box = controlled({ methods: { list: async () => view({ last_event_id: ++reads * 10 }), events: async () => {
    if (++subscriptions === 1) throw new AgentApiError("游标超出范围", { status: 409, code: "AGENT_FOLLOWUP_INVALID_CURSOR" });
  }, send: () => assert.fail("只读恢复不能调用模型") } });
  await box.controller.start(); await tick(); assert.equal(box.timers.length, 1);
  box.timers.shift()(); await tick(); assert.equal(reads, 2); assert.equal(box.controller.getState().last_event_id, 20); box.controller.destroy();
});

test("an event rendering failure leaves the successful cursor unchanged", async () => {
  const box = controlled(); await box.controller.start();
  box.controller.subscribe((state) => { if (state.items.length) throw new Error("render failed"); });
  await assert.rejects(box.streams[0].onEvent(event(1)), /render failed/);
  assert.equal(box.controller.getState().last_event_id, 0); box.controller.destroy();
});

async function withServer(options, work) {
  const server = createAgentBackend({ upstream: "http://internal.invalid", access: { authenticate: async () => ({ id: "alice" }) }, ...options }).listen(0, "127.0.0.1");
  await once(server, "listening");
  try { await work(`http://127.0.0.1:${server.address().port}`); }
  finally { const closed = once(server, "close"); server.close(); server.closeAllConnections(); await closed; }
}
test("followup BFF forwards trusted owner, validates scope/query/body and sanitizes failure", async () => {
  const calls = [], expectedOwner = createHash("sha256").update(JSON.stringify(["xiaodao-website", "alice"])).digest("hex");
  await withServer({ fetchImpl: async (url, init) => {
    calls.push({ url: String(url), init }); assert.equal(new Headers(init.headers).get("X-Agent-Owner-Key"), expectedOwner);
    return envelope(init.method === "POST" ? receipt({ status: String(url).endsWith("/stop") ? "CANCELLED" : "ACCEPTED" }) : view());
  } }, async (base) => {
    const get = await fetch(base + path + "?limit=2", { headers: { "X-Agent-Owner-Key": "forged" } }); assert.equal(get.status, 200); await get.json();
    for (const [suffix, body] of [["", { request_id: "question-1", text: "问题" }], [`/${fid}/stop`, { request_id: "question-1" }]]) {
      const response = await fetch(base + path + suffix, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      assert.equal(response.status, 200); await response.json();
    }
    for (const query of ["?owner=forged", "?limit=0", "?limit=2&limit=3"]) { const response = await fetch(base + path + query); assert.equal(response.status, 400); await response.text(); }
    const bad = await fetch(base + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ request_id: "x", text: "问题", owner: "forged" }) });
    assert.equal(bad.status, 400); await bad.text(); assert.equal(calls.length, 3);
  });
  await withServer({ fetchImpl: async () => envelope(view({ run_id: other })) }, async (base) => { const response = await fetch(base + path); assert.equal(response.status, 502); await response.text(); });
  await withServer({ access: { authenticate: async () => null }, fetchImpl: () => assert.fail("未授权不能转发") }, async (base) => {
    const response = await fetch(base + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); assert.equal(response.status, 401); await response.text();
  });
  await withServer({ access: { authenticate: async (request) => {
    if (request.method === "POST" && request.headers["x-csrf-token"] !== "valid") throw new HttpError(403, "请求校验失败。");
    return { id: "alice" };
  } }, fetchImpl: () => assert.fail("CSRF 校验失败不能转发") }, async (base) => {
    const response = await fetch(base + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ request_id: "one", text: "问题" }) });
    assert.equal(response.status, 403); await response.text();
  });
  await withServer({ fetchImpl: async () => new Response(JSON.stringify({ ok: false, data: null, error: {
    code: "AGENT_FOLLOWUP_BUSY", message: "/secret/token", details: [{ field: "private_path", actual: "/secret" }], retryable: false,
  } }), { status: 409 }) }, async (base) => { const response = await fetch(base + path); assert.equal(response.status, 409); const value = await response.text(); assert.doesNotMatch(value, /secret/); assert.match(value, /AGENT_FOLLOWUP_BUSY/); });
});

test("followup BFF streams exact replay frames and Last-Event-ID independently", async () => {
  const wire = `: connected\n\ndata: ${JSON.stringify(event(8))}\n\n: heartbeat\n\n`;
  await withServer({ fetchImpl: async (url, init) => {
    assert.ok(String(url).endsWith(`/runs/${rid}/followups/events`)); assert.equal(new Headers(init.headers).get("Last-Event-ID"), "7");
    return new Response(wire, { headers: { "Content-Type": "text/event-stream" } });
  } }, async (base) => {
    const response = await fetch(base + path + "/events", { headers: { "Last-Event-ID": "7" } });
    assert.equal(response.headers.get("x-accel-buffering"), "no"); assert.equal(await response.text(), wire);
    const invalid = await fetch(base + path + "/events", { headers: { "Last-Event-ID": "-1" } }); assert.equal(invalid.status, 400); await invalid.text();
  });
});

test("offline preview answers only through followups, supports report-only, pagination, stop and new conversation", async () => {
  const tasks = [], client = createAgentClient({ fetchImpl: createPreviewApi(previewSamples(), { schedule: (work) => tasks.push(work) }) });
  const directory = await client.conversations.list(), original = directory.items.find((entry) => entry.title === "旧报告追问");
  const id = original.conversation_id, run = original.current_run.run_id, report = await client.conversations.get(id);
  assert.equal((await client.followups.list(id, run)).snapshot_status, "UNAVAILABLE");
  await client.followups.send(id, run, { request_id: "one", text: "解释" }); tasks.shift()();
  const second = await client.followups.send(id, run, { request_id: "two", text: "再核对" });
  await client.followups.stop(id, run, second.followup_id, { request_id: "stop" }); tasks.shift()();
  const page = await client.followups.list(id, run, { limit: 1 }); assert.equal(page.items[0].status, "CANCELLED"); assert.ok(page.next_cursor);
  const older = await client.followups.list(id, run, { limit: 1, cursor: page.next_cursor }); assert.match(older.items[0].answer_markdown, /未重新核对原日志/);
  assert.deepEqual((await client.conversations.get(id)).result, report.result);
  const created = await client.conversations.create("new"); assert.notEqual(created.conversation_id, id);
  assert.equal((await client.conversations.create("new")).conversation_id, created.conversation_id);
});

test("followup view keeps Markdown inert, report-only notice visible and typing node stable", async () => {
  class Node {
    constructor(doc, tag) { this.ownerDocument = doc; this.tagName = tag; this.children = []; this.dataset = {}; this.text = ""; this.value = ""; }
    set textContent(value) { this.text = String(value); this.children = []; } get textContent() { return this.text + this.children.map((node) => node.textContent).join(""); }
    set innerHTML(_) { assert.fail("不能解析不可信 HTML"); } setAttribute() {}
    append(...nodes) { for (const node of nodes) this.children.push(...(node.tagName === "fragment" ? node.children : [node])); }
    replaceChildren(...nodes) { this.children = []; this.text = ""; this.append(...nodes); }
  }
  const doc = { createElement: (tag) => new Node(doc, tag), createDocumentFragment: () => new Node(doc, "fragment") };
  const root = doc.createElement("main"), all = (node) => [node, ...node.children.flatMap(all)];
  const box = controlled({ methods: { list: async () => view({ items: [item({ answer_markdown: "<script>alert(1)</script>" })] }) } });
  const cleanup = mountFollowupView(root, box.controller); await box.controller.start();
  const textarea = all(root).find((node) => node.tagName === "textarea"); textarea.value = "继续解释"; textarea.oninput();
  assert.equal(all(root).find((node) => node.tagName === "textarea"), textarea);
  assert.match(root.textContent, /未重新核对原日志/); assert.match(root.textContent, /<script>/);
  assert.equal(all(root).some((node) => node.tagName === "script"), false); cleanup();
});
