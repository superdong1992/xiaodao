import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { once } from "node:events";
import test from "node:test";
import { createAgentClient } from "./browser-client.js";
import { createFollowupController } from "./followup-controller.js";
import { validateFollowupItem } from "./followup-contract.js";
import { mountFollowupView } from "./followup-view.js";
import { mountReportVersionsView } from "./continuation-view.js";
import { createAgentBackend } from "./server.mjs";
import { createPreviewApi } from "./preview-model.js";
import { previewSamples } from "./preview.mjs";

const cid = "10000000-0000-0000-0000-000000000001", rid = "20000000-0000-0000-0000-000000000001";
const fid = "30000000-0000-0000-0000-000000000001", aid = "40000000-0000-0000-0000-000000000001";
const next = "50000000-0000-0000-0000-000000000001", timestamp = "2026-10-17T00:00:00Z";
const item = (extra = {}) => ({ followup_id: fid, run_id: rid, request_id: "ask", ordinal: 1, status: "COMPLETED",
  context_mode: "REPORT_ONLY", text: "结合新日志核对原因", answer_markdown: "已开始进一步诊断。", failure: null,
  created_at: timestamp, updated_at: timestamp, ...extra });
const view = (items = []) => ({ schema_version: 1, conversation_id: cid, run_id: rid, can_ask: true, reason: null,
  snapshot_status: "UNAVAILABLE", active_followup: null, items, next_cursor: null, last_event_id: items.length });
const receipt = { conversation_id: cid, run_id: rid, followup_id: fid, request_id: "ask", event_id: 1, status: "ACCEPTED" };
const memory = () => { const data = new Map(); return { getItem: (key) => data.get(key), setItem: (key, value) => data.set(key, value) }; };
const tick = () => new Promise((resolve) => setImmediate(resolve));

test("原追问接口接收附件并保持旧请求字节，交接结果兼容旧记录", async () => {
  const calls = [], client = createAgentClient({ fetchImpl: async (_url, init) => {
    calls.push(JSON.parse(init.body)); return new Response(JSON.stringify({ ok: true, data: receipt, error: null }));
  } });
  const plain = { request_id: "ask", text: "解释报告" }, withLogs = { ...plain, attachment_ids: [aid] };
  await client.followups.send(cid, rid, plain); await client.followups.send(cid, rid, withLogs);
  assert.deepEqual(calls, [plain, withLogs]);
  assert.equal(validateFollowupItem(item(), rid).run_id, rid);
  assert.equal(validateFollowupItem(item({ diagnosis_run_id: next, attachment_ids: [aid] }), rid).diagnosis_run_id, next);
  assert.throws(() => validateFollowupItem(item({ diagnosis_run_id: "wrong" }), rid));
});

test("统一追问上传后只提交一次入口，丢失回执跨刷新保留原日志集合", async () => {
  const storage = memory(), calls = [];
  const client = { attachments: { prepare: async () => ({ attachment: { attachment_id: aid } }), upload: async () => {} },
    followups: { list: async () => view(), events: async () => {}, send: async (...args) => {
      calls.push(args); if (calls.length === 1) throw new Error("响应丢失"); return receipt;
    } }, conversations: { send: () => assert.fail("不能退回 messages") },
    continuations: { create: () => assert.fail("页面不能选择诊断路径"), confirm: () => assert.fail("页面不能确认诊断") } };
  const options = { client, conversationId: cid, runId: rid, storage, makeRequestId: () => "ask", sha256: async () => "a".repeat(64) };
  let controller = createFollowupController(options); await controller.start(); controller.setDraft("核对新增日志");
  await controller.uploadFiles([new File(["logs"], "new.zip")]);
  await assert.rejects(controller.submit(), /响应丢失/); controller.destroy();
  controller = createFollowupController(options); await controller.start(); await controller.submit();
  assert.deepEqual(calls[0], [cid, rid, { request_id: "ask", text: "核对新增日志", attachment_ids: [aid] }]);
  assert.deepEqual(calls[1], calls[0]); controller.destroy();
});

test("报告页只挂载原追问入口及版本查看，不挂载草稿确认面板", () => {
  const source = readFileSync(new URL("./preview.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /mountContinuationView|createContinuationController/);
  assert.match(source, /mountReportVersionsView/);
});

test("后台交接展示进度，等待补充仍用同一输入框固定新run重试，READY才打开新报告", async () => {
  const storage = memory(), messages = [], opened = [], stops = [];
  let entries = [], status = "WAITING_INPUT", request = 0;
  const timers = new Set();
  const detail = () => ({ conversation_id: cid, selected_run_id: next, status,
    report_state: status === "COMPLETED" ? "READY" : "PENDING", progress: { message: "正在核对日志。" },
    current_questions: status === "WAITING_INPUT" ? ["请补充故障发生时间。"] : [], capabilities: { can_stop: status !== "COMPLETED" } });
  const client = { followups: { list: async () => view(entries), events: async () => {}, send: async () => {
    entries = [item({ diagnosis_run_id: next, attachment_ids: [] })]; return receipt;
  }, stop: () => assert.fail("已交接的诊断不能调用followups.stop") }, conversations: {
    get: async (_cid, query) => { assert.equal(query.run_id, next); assert.deepEqual(query.include, []); return detail(); },
    send: async (_cid, body) => { messages.push(body); if (messages.length === 1) throw new Error("补充回执丢失"); status = "RUNNING"; return { request_id: body.request_id, run_id: next }; },
    stop: async (_cid, body) => { stops.push(body); },
  } };
  const options = { client, conversationId: cid, runId: rid, storage, makeRequestId: () => ++request === 1 ? "ask" : `supplement-${request}`,
    schedule: (work) => { timers.add(work); return work; }, cancelSchedule: (work) => timers.delete(work),
    onDiagnosisReady: (value) => opened.push(value) };
  let controller = createFollowupController(options); await controller.start(); controller.setDraft("继续核对"); await controller.submit(); await tick();
  assert.deepEqual(controller.getState().current_questions, ["请补充故障发生时间。"]);
  assert.equal(controller.getState().input_run_id, next); assert.equal(opened.length, 0);
  controller.setDraft("发生在10:05"); await assert.rejects(controller.submit(), /补充回执丢失/); controller.destroy();
  assert.equal(timers.size, 0);
  controller = createFollowupController(options); await controller.start(); await tick(); await controller.submit(); await tick();
  assert.deepEqual(messages[0], { request_id: "supplement-2", text: "发生在10:05", attachment_ids: [], target_run_id: next });
  assert.deepEqual(messages[1], messages[0]); assert.equal(controller.getState().input_run_id, null);
  await controller.stopDiagnosis(next); assert.equal(stops[0].run_id, next);
  status = "COMPLETED"; await controller.refreshDiagnosis(next); await tick();
  assert.equal(opened.length, 1); assert.equal(opened[0].run_id, next); assert.equal(opened[0].parent_run_id, rid);
  await controller.refreshDiagnosis(next); assert.equal(opened.length, 1); controller.destroy();
});

test("切换报告终止关联诊断读取，迟到READY不会切回旧报告", async () => {
  let resolve, signal, opened = 0;
  const storage = memory(); storage.setItem(`xiaodao-followup:${cid}:${rid}`, JSON.stringify({ draft: "", pending: null,
    receipts: [receipt], stops: {} }));
  const controller = createFollowupController({ conversationId: cid, runId: rid, storage,
    client: { followups: { list: async () => view([item({ diagnosis_run_id: next })]), events: async () => {} },
      conversations: { get: (_cid, input) => { signal = input.signal; return new Promise((yes) => resolve = yes); } } },
    onDiagnosisReady: () => opened++ });
  await controller.start(); controller.destroy(); assert.equal(signal.aborted, true);
  resolve({ conversation_id: cid, selected_run_id: next, status: "COMPLETED", report_state: "READY" }); await tick();
  assert.equal(opened, 0);
});

test("等待补充的轮次已变化时刷新原目标，不自动改发追问", async () => {
  let reads = 0, status = "WAITING_INPUT";
  const controller = createFollowupController({ conversationId: cid, runId: rid, storage: memory(),
    schedule: () => 1, cancelSchedule() {}, client: {
      followups: { list: async () => view([item({ diagnosis_run_id: next })]), events: async () => {},
        send: () => assert.fail("补充冲突不能自动改发追问") },
      conversations: { get: async () => { reads++; return { conversation_id: cid, selected_run_id: next, status,
        report_state: "PENDING", current_questions: status === "WAITING_INPUT" ? ["请补充时间"] : [] }; },
        send: async (_cid, body) => { assert.equal(body.target_run_id, next); status = "RUNNING";
          throw Object.assign(new Error("诊断轮次已变化"), { status: 409, code: "AGENT_RUN_CHANGED" }); } },
    } });
  try {
    await controller.start(); await tick(); controller.setDraft("10:05");
    await assert.rejects(controller.submit(), /诊断轮次已变化/);
    assert.equal(reads, 2); assert.equal(controller.getState().input_run_id, null);
    assert.equal(controller.getState().pending, null); assert.equal(controller.getState().draft, "10:05");
  } finally { controller.destroy(); }
});

class DomNode {
  constructor(doc, tag) { Object.assign(this, { ownerDocument: doc, tagName: tag, children: [], dataset: {}, ownText: "", value: "", hidden: false }); }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map((node) => node.textContent).join(""); }
  setAttribute() {}
  set innerHTML(_) { assert.fail("不能解析用户HTML"); }
  append(...nodes) { for (const node of nodes) this.children.push(...(node.tagName === "fragment" ? node.children : [node])); }
  replaceChildren(...nodes) { this.children = []; this.ownText = ""; this.append(...nodes); }
}
const root = () => { const doc = { createElement: (tag) => new DomNode(doc, tag), createDocumentFragment: () => new DomNode(doc, "fragment") }; return doc.createElement("main"); };
const all = (node) => [node, ...node.children.flatMap(all)];
test("原追问框提供多日志和后端补充问题，版本区域只有查看与对比", async () => {
  const container = root(); let sent = 0;
  const controller = { subscribe: (listener) => { listener({ items: [item({ diagnosis_run_id: next })], diagnoses: {
    [next]: { detail: { status: "WAITING_INPUT", capabilities: { can_stop: true }, progress: { message: "等待补充" } } } },
    current_questions: ["请补充故障时间。"], input_run_id: next, can_submit: true, draft: "10:05", uploads: [], can_ask: false }); return () => {}; },
    submit: async () => sent++, destroy() {} };
  const dispose = mountFollowupView(container, controller);
  assert.equal(all(container).filter((node) => node.tagName === "form").length, 1);
  assert.equal(all(container).find((node) => node.tagName === "input").multiple, true);
  assert.match(container.textContent, /请补充故障时间/); assert.doesNotMatch(container.textContent, /确认并继续诊断|整理并预览/);
  all(container).find((node) => node.tagName === "form").onsubmit({ preventDefault() {} }); await tick(); assert.equal(sent, 1); dispose();
  const versionsRoot = root();
  const close = mountReportVersionsView(versionsRoot, { conversationId: cid, runId: rid, client: { continuations: {
    versions: async () => ({ versions: [{ run_id: rid, parent_run_id: null, version: 1 }], root_run_id: rid }) } } });
  await tick(); assert.equal(all(versionsRoot).some((node) => ["form", "textarea"].includes(node.tagName)), false); close();
});

test("离线原追问升级后保留旧报告，补充固定轮次后生成新版及版本链", async () => {
  const tasks = [], client = createAgentClient({ fetchImpl: createPreviewApi(previewSamples(), { schedule: (work) => tasks.push(work) }) });
  const original = (await client.conversations.list()).items.find((entry) => entry.title === "通用 Markdown");
  const cid = original.conversation_id, rid = original.current_run.run_id, report = await client.conversations.get(cid);
  await client.followups.send(cid, rid, { request_id: "one", text: "请继续核对超时根因。" }); tasks.shift()();
  const handed = (await client.followups.list(cid, rid)).items[0], next = handed.diagnosis_run_id;
  assert.ok(next); assert.notEqual(next, rid);
  const waiting = await client.conversations.get(cid, { run_id: next, include: [] });
  assert.equal(waiting.status, "WAITING_INPUT"); assert.ok(waiting.current_questions.length);
  assert.deepEqual((await client.conversations.get(cid, { run_id: rid })).result, report.result);
  assert.equal((await client.continuations.versions(cid, rid)).versions.length, 1);
  const supplement = { request_id: "time", text: "10:05", target_run_id: next, attachment_ids: [] };
  const receipt = await client.conversations.send(cid, supplement);
  assert.deepEqual(await client.conversations.send(cid, supplement), receipt);
  assert.equal((await client.conversations.get(cid, { run_id: next, include: [] })).status, "RUNNING");
  tasks.shift()();
  assert.equal((await client.conversations.get(cid, { run_id: next })).report_state, "READY");
  assert.equal((await client.continuations.versions(cid, next)).versions.length, 2);
  assert.deepEqual((await client.conversations.get(cid, { run_id: rid })).result, report.result);
});

test("附件过期410经真实BFF透传，追问可清除原请求并移除过期日志", async () => {
  for (const code of ["AGENT_RESOURCE_EXPIRED", "AGENT_ATTACHMENT_EXPIRED"]) {
    const server = createAgentBackend({ upstream: "http://internal.invalid", access: { authenticate: async () => ({ id: "alice" }) },
      fetchImpl: async () => new Response(JSON.stringify({ ok: false, data: null, error: { code, message: "/private/path", details: [], retryable: false } }), { status: 410 }),
    }).listen(0, "127.0.0.1");
    await once(server, "listening");
    let controller;
    try {
      const api = createAgentClient({ fetchImpl: (path, init) => fetch(`http://127.0.0.1:${server.address().port}${path}`, init) });
      const client = { ...api, attachments: { prepare: async () => ({ attachment: { attachment_id: aid } }), upload: async () => {} },
        followups: { ...api.followups, list: async () => view(), events: async () => {} } };
      controller = createFollowupController({ client, conversationId: cid, runId: rid, storage: memory(), makeRequestId: () => "ask", sha256: async () => "a".repeat(64) });
      await controller.start(); controller.setDraft("核对日志"); await controller.uploadFiles([new File(["logs"], "logs.zip")]);
      await assert.rejects(controller.submit(), (error) => error.status === 410 && error.code === code && !error.message.includes("private"));
      assert.equal(controller.getState().pending, null);
      controller.removeUpload(controller.getState().uploads[0].local_id); assert.equal(controller.getState().uploads.length, 0);
    } finally { controller?.destroy(); const closed = once(server, "close"); server.close(); server.closeAllConnections(); await closed; }
  }
});
