import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { once } from "node:events";
import test from "node:test";
import { createAgentClient, AgentApiError } from "./browser-client.js";
import { createAgentBackend } from "./server.mjs";
import { createContinuationController } from "./continuation-controller.js";
import { mountContinuationView, renderInputChanges } from "./continuation-view.js";
import { sha256File } from "./file-sha256.js";
import { previewSamples } from "./preview.mjs";
import { createPreviewApi } from "./preview-model.js";

const id = (value) => `00000000-0000-0000-0000-${String(value).padStart(12, "0")}`;
const cid = id(1), rid = id(2), did = id(3), next = id(4), oldLog = id(5), newLog = id(6);
const attachment = (attachment_id, name) => ({ attachment_id, conversation_id: cid, name, size: 32, status: "READY" });
const logA = attachment(oldLog, "original.tar.gz"), logB = attachment(newLog, "server.zip");
const changes = { before_problem_text: "14:00 超时", after_problem_text: "14:30 超时", added_attachments: [logB], removed_attachments: [] };
const draft = (overrides = {}) => ({ conversation_id: cid, draft_id: did, parent_run_id: rid, revision: 1, status: "DRAFT",
  source_problem_text: changes.before_problem_text, effective_problem_text: changes.after_problem_text,
  change_note: "更正时间", diagnosis_goal: "验证连接池耗尽", log_mode: "APPEND", attachment_ids: [oldLog, newLog],
  attachments: [logA, logB], input_changes: changes, failure_message: null, confirmed_run_id: null, ...overrides });
const versions = (overrides = {}) => ({ conversation_id: cid, root_run_id: rid, latest_run_id: rid, can_continue: true,
  versions: [{ run_id: rid, parent_run_id: null, version: 1, created_at: "2026-10-16T00:00:00Z", input_changes: null,
    diagnosis_goal: "", runtime_changes: [] }], ...overrides });
const receipt = (request_id) => ({ conversation_id: cid, run_id: next, parent_run_id: rid, request_id, status: "ACCEPTED" });
const followupReceipt = (request_id) => ({ conversation_id: cid, run_id: rid, followup_id: id(70), request_id, event_id: 1, status: "ACCEPTED" });
const envelope = (data) => new Response(JSON.stringify({ ok: true, data, error: null }), { headers: { "Content-Type": "application/json" } });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const settle = () => new Promise((resolve) => setImmediate(resolve));
const memoryStorage = () => { const values = new Map(); return { getItem: (key) => values.get(key), setItem: (key, value) => values.set(key, value) }; };

test("继续诊断浏览器接口保留请求、草稿版本和读取标识", async () => {
  const calls = [], signal = new AbortController().signal;
  const client = createAgentClient({ fetchImpl: async (path, init) => {
    calls.push({ path, init }); const input = init.body && JSON.parse(init.body);
    return envelope(path.endsWith("report-versions") ? versions() : path.endsWith("confirm") ? receipt(input.request_id) : draft());
  } });
  const create = { request_id: "create-unchanged", text: "改为14:30", log_mode: "APPEND", attachment_ids: [newLog] };
  const update = { request_id: "patch-unchanged", expected_revision: 1, effective_problem_text: "14:30 超时", log_mode: "REPLACE", attachment_ids: [newLog] };
  await client.continuations.create(cid, rid, create);
  await client.continuations.get(cid, did, { signal });
  await client.continuations.update(cid, did, update);
  await client.continuations.confirm(cid, did, { request_id: "confirm-unchanged", expected_revision: 1 });
  await client.continuations.versions(cid, rid, { signal });
  assert.deepEqual(calls.map(({ path, init }) => [path, init.method]), [
    [`/api/agent/conversations/${cid}/runs/${rid}/continuation-drafts`, "POST"],
    [`/api/agent/conversations/${cid}/continuation-drafts/${did}`, "GET"],
    [`/api/agent/conversations/${cid}/continuation-drafts/${did}`, "PATCH"],
    [`/api/agent/conversations/${cid}/continuation-drafts/${did}/confirm`, "POST"],
    [`/api/agent/conversations/${cid}/runs/${rid}/report-versions`, "GET"],
  ]);
  assert.deepEqual(JSON.parse(calls[0].init.body), create); assert.deepEqual(JSON.parse(calls[2].init.body), update);
  assert.equal(calls[1].init.signal, signal); assert.equal(calls[4].init.signal, signal);
  assert.ok(calls.every(({ init }) => init.credentials === "same-origin" && init.redirect === "error"));
});

test("拒绝不一致的草稿、版本和回执；非法输入不发请求", async () => {
  let value = draft({ parent_run_id: next }), calls = 0;
  const client = createAgentClient({ fetchImpl: async () => { calls++; return envelope(value); } });
  await assert.rejects(client.continuations.create(cid, rid, { request_id: "one" }), { code: "WEBSITE_INVALID_RESPONSE" });
  value = draft({ conversation_id: id(99) });
  await assert.rejects(client.continuations.get(cid, did), { code: "WEBSITE_INVALID_RESPONSE" });
  value = versions({ versions: [] });
  await assert.rejects(client.continuations.versions(cid, rid), { code: "WEBSITE_INVALID_RESPONSE" });
  value = receipt("different");
  await assert.rejects(client.continuations.confirm(cid, did, { request_id: "one", expected_revision: 1 }), { code: "WEBSITE_INVALID_RESPONSE" });
  const before = calls;
  for (const input of [{ request_id: "one", expected_revision: 0 }, { request_id: "one", expected_revision: 1, unknown: true },
    { request_id: "one", expected_revision: 1, attachment_ids: [oldLog, oldLog] },
    { request_id: "one", expected_revision: 1, effective_problem_text: " " }])
    await assert.rejects(client.continuations.update(cid, did, input), TypeError);
  await assert.rejects(client.continuations.get(cid, "../run"), TypeError);
  assert.equal(calls, before);
});

test("FOLLOWUP 草稿保留非空追问，旧草稿仍可读取，无效追问不能流入确认", async () => {
  let value = draft({ status: "FOLLOWUP", followup_text: "请解释报告中的结论。" });
  const client = createAgentClient({ fetchImpl: async () => envelope(value) });
  assert.equal((await client.continuations.get(cid, did)).followup_text, value.followup_text);
  for (const text of [undefined, null, "", " ", "字".repeat(22000)]) {
    value = draft({ status: "FOLLOWUP", followup_text: text });
    await assert.rejects(client.continuations.get(cid, did), { code: "WEBSITE_INVALID_RESPONSE" });
  }
  value = draft();
  assert.equal((await client.continuations.get(cid, did)).status, "DRAFT");
});

test("FOLLOWUP 只发原报告追问；响应丢失跨刷新重用 ID，成功后不会重复发送", async () => {
  const storage = memoryStorage(), calls = [], callbacks = [];
  const answerDraft = draft({ status: "FOLLOWUP", followup_text: "请解释报告中的结论。" });
  let state;
  const client = { continuations: {
    versions: async () => versions(), create: async () => answerDraft, get: async () => answerDraft,
    confirm: () => assert.fail("报告追问不能创建新诊断"), update: () => assert.fail("报告追问不能直接改为诊断草稿"),
  }, followups: { send: async (conversationId, runId, input) => {
    assert.equal(conversationId, cid); assert.equal(runId, rid); calls.push(structuredClone(input));
    if (calls.length === 1) throw new AgentApiError("响应丢失");
    return followupReceipt(input.request_id);
  } } };
  let counter = 0;
  const options = { client, conversationId: cid, runId: rid, storage,
    makeRequestId: () => `followup-${++counter}`, onFollowup: (result) => callbacks.push(result) };
  const first = createContinuationController(options); await first.start(); await first.create({ text: answerDraft.followup_text });
  await assert.rejects(first.confirm()); await assert.rejects(first.update({ effective_problem_text: "改为诊断" }));
  await assert.rejects(first.sendFollowup()); assert.equal(first.newDraft(), false); first.destroy();
  const second = createContinuationController({ ...options, makeRequestId: () => assert.fail("重试不能分配新请求 ID") });
  second.subscribe((value) => { state = value; }); await second.start(); await second.retry();
  assert.deepEqual(calls[0], calls[1]); assert.deepEqual(Object.keys(calls[1]).sort(), ["request_id", "text"]);
  assert.equal(state.pending, null); assert.equal(state.followup_receipt.run_id, rid); assert.equal(callbacks.length, 1); second.destroy();
  const third = createContinuationController({ ...options, makeRequestId: () => assert.fail("已发送的草稿不能重复提交") });
  third.subscribe((value) => { state = value; }); await third.start(); await third.sendFollowup();
  assert.equal(calls.length, 2); assert.equal(callbacks.length, 2);
  assert.equal(third.newDraft(), true); assert.equal(state.draft, null); assert.equal(state.followup_receipt, null); third.destroy();
});

test("真实 BFF 为继续诊断透传登录态、保留请求体并拒绝越界响应", async () => {
  const upstream = [], server = createAgentBackend({ upstream: "http://xiaodao.internal", fetchImpl: async (url, init) => {
    upstream.push({ url: String(url), init }); const input = init.body && JSON.parse(init.body);
    return envelope(String(url).endsWith("report-versions") ? versions() : String(url).endsWith("confirm") ? receipt(input.request_id) : draft());
  } });
  server.listen(0, "127.0.0.1"); await once(server, "listening");
  const origin = `http://127.0.0.1:${server.address().port}`, root = `/api/agent/conversations/${cid}`;
  try {
    for (const [path, method, input] of [[`/runs/${rid}/continuation-drafts`, "POST", { request_id: "create", attachment_ids: [newLog] }],
      [`/continuation-drafts/${did}`, "GET"], [`/continuation-drafts/${did}`, "PATCH", { request_id: "patch", expected_revision: 1, attachment_ids: [newLog] }],
      [`/continuation-drafts/${did}/confirm`, "POST", { request_id: "confirm", expected_revision: 1 }], [`/runs/${rid}/report-versions`, "GET"]]) {
      const response = await fetch(`${origin}${root}${path}`, { method, headers: { Cookie: "session=test", "Content-Type": "application/json" }, body: input && JSON.stringify(input) });
      assert.equal(response.status, 200); assert.equal((await response.json()).ok, true);
      const forwarded = upstream.at(-1); assert.equal(forwarded.url, `http://xiaodao.internal/api/v1/agent/conversations/${cid}${path}`);
      assert.equal(new Headers(forwarded.init.headers).get("cookie"), "session=test");
      assert.equal(forwarded.init.body, input && JSON.stringify(input));
    }
    const count = upstream.length;
    assert.equal((await fetch(`${origin}${root}/runs/${rid}/report-versions?share=true`)).status, 400);
    assert.equal((await fetch(`${origin}${root}/continuation-drafts/${did}/confirm`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: "confirm", expected_revision: 0 }) })).status, 400);
    assert.equal(upstream.length, count);
  } finally { server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); }
});

test("真实 BFF 接收 FOLLOWUP 草稿，并保留不能确认的明确错误", async () => {
  const server = createAgentBackend({ upstream: "http://xiaodao.internal", fetchImpl: async (_url, init) => {
    if (init.method === "POST") return new Response(JSON.stringify({ ok: false, data: null, error: {
      code: "AGENT_FOLLOWUP_REQUIRED", message: "服务端内部消息", details: [], retryable: false,
    } }), { status: 409, headers: { "Content-Type": "application/json" } });
    return envelope(draft({ status: "FOLLOWUP", followup_text: "请解释报告中的结论。" }));
  } });
  server.listen(0, "127.0.0.1"); await once(server, "listening");
  const path = `http://127.0.0.1:${server.address().port}/api/agent/conversations/${cid}/continuation-drafts/${did}`;
  try {
    const loaded = await fetch(path, { headers: { Cookie: "session=test" } });
    assert.equal(loaded.status, 200); assert.equal((await loaded.json()).data.status, "FOLLOWUP");
    const rejected = await fetch(`${path}/confirm`, { method: "POST", headers: { Cookie: "session=test", "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: "no-new-case", expected_revision: 1 }) });
    assert.equal(rejected.status, 409); const error = (await rejected.json()).error;
    assert.equal(error.code, "AGENT_FOLLOWUP_REQUIRED"); assert.match(error.message, /报告.*追问/);
    assert.doesNotMatch(error.message, /内部/);
  } finally { server.closeAllConnections(); await new Promise((resolve) => server.close(resolve)); }
});

test("创建响应丢失后，刷新页面重试仍使用相同请求和更正文字", async () => {
  const storage = memoryStorage(), calls = []; let first = true, state;
  const client = { continuations: { versions: async () => versions(), create: async (_cid, _rid, input) => {
    calls.push(structuredClone(input)); if (first) { first = false; throw new AgentApiError("连接中断"); } return draft();
  } } };
  const options = { client, conversationId: cid, runId: rid, storage, makeRequestId: () => "stable-create" };
  const one = createContinuationController(options); await one.start();
  await assert.rejects(one.create({ text: "实际为14:30", attachment_ids: [newLog] })); one.destroy();
  const two = createContinuationController({ ...options, makeRequestId: () => assert.fail("恢复请求不能生成新 ID") });
  two.subscribe((value) => { state = value; }); await two.start(); await two.retry();
  assert.deepEqual(calls[0], calls[1]); assert.equal(state.pending, null); assert.equal(state.draft.draft_id, did); two.destroy();
});

test("确认响应丢失后只重放原版本；草稿不自动触发诊断", async () => {
  let state, attempts = 0, confirmed = 0; const inputs = [];
  const client = { continuations: { versions: async () => versions(), create: async () => draft(), confirm: async (_cid, _did, input) => {
    inputs.push(input); if (++attempts === 1) throw new AgentApiError("网络未知结果"); return receipt(input.request_id);
  } } };
  const controller = createContinuationController({ client, conversationId: cid, runId: rid, storage: null,
    makeRequestId: () => "stable", onConfirmed: () => { confirmed++; } });
  controller.subscribe((value) => { state = value; }); await controller.start(); await controller.create({ attachment_ids: [] });
  assert.equal(attempts, 0); await assert.rejects(controller.confirm()); assert.equal(state.pending.action, "confirm");
  await controller.retry(); assert.deepEqual(inputs[0], inputs[1]); assert.equal(inputs[0].expected_revision, 1);
  assert.equal(confirmed, 1); assert.equal(state.draft.confirmed_run_id, next); controller.destroy();
});

test("草稿版本冲突刷新后需要重新核对；FAILED 草稿可直接编辑恢复", async () => {
  let state, reads = 0, conflict = true;
  const client = { continuations: { versions: async () => versions(), create: async () => draft({ status: "FAILED", failure_message: "整理失败" }),
    get: async () => { reads++; return draft({ revision: 2 }); },
    update: async (_cid, _did, input) => {
      if (conflict) { conflict = false; throw new AgentApiError("草稿已更新", { status: 409, code: "AGENT_DRAFT_CHANGED" }); }
      assert.equal(input.expected_revision, 2); return draft({ revision: 3, effective_problem_text: input.effective_problem_text });
    } } };
  const controller = createContinuationController({ client, conversationId: cid, runId: rid, storage: null, makeRequestId: () => "edit" });
  controller.subscribe((value) => { state = value; }); await controller.start(); await controller.create({ text: "更正" });
  await assert.rejects(controller.update({ effective_problem_text: "核对后的完整问题" }));
  assert.equal(reads, 1); assert.equal(state.pending, null); assert.equal(state.draft.revision, 2);
  await controller.update({ effective_problem_text: "核对后的完整问题" }); assert.equal(state.draft.revision, 3); controller.destroy();
});

test("PREPARING 只轮询草稿，切换报告后不再轮询或污染新面板", async () => {
  const timers = []; let reads = 0, emissions = 0;
  const pending = deferred();
  const controller = createContinuationController({ conversationId: cid, runId: rid, storage: null, makeRequestId: () => "create",
    schedule: (work) => { timers.push(work); return timers.length; }, cancelSchedule: () => {},
    client: { continuations: { versions: async () => versions(), create: async () => draft({ status: "PREPARING" }), get: async () => { reads++; return pending.promise; } } } });
  controller.subscribe(() => { emissions++; }); await controller.start(); await controller.create({ text: "更正" });
  assert.equal(timers.length, 1); timers[0](); assert.equal(reads, 1);
  controller.destroy(); const before = emissions; pending.resolve(draft({ revision: 2 })); await settle(); assert.equal(emissions, before);
});

test("日志上传失败重用哈希、预约和原 File，同内容重复选择只引用一次", async () => {
  let hashes = 0, reserves = 0, uploads = 0, state; const file = new File(["actual bytes"], "server.zip");
  const prepared = { attachment: { attachment_id: newLog } };
  const controller = createContinuationController({ conversationId: cid, runId: rid, storage: null, makeRequestId: () => id(77),
    sha256: async () => { hashes++; return "a".repeat(64); }, client: { attachments: {
      prepare: async () => { reserves++; return prepared; }, upload: async (reservation, actual) => {
        uploads++; assert.equal(reservation, prepared); assert.equal(actual, file); if (uploads === 1) throw new Error("上传失败");
      },
    } } });
  controller.subscribe((value) => { state = value; }); await assert.rejects(controller.uploadFiles([file])); await controller.retry();
  assert.deepEqual([hashes, reserves, uploads], [1, 1, 2]); assert.equal(state.uploads[0].attachment_id, newLog);
  await controller.uploadFiles([new File(["actual bytes"], "same.zip")]);
  assert.deepEqual([hashes, reserves, uploads], [2, 1, 2]); assert.equal(new Set(state.uploads.map((item) => item.attachment_id)).size, 1); controller.destroy();
});

test("日志校验显示进度并可取消，重试后上传使用同一个取消信号", async () => {
  let state, controls, hashes = 0, uploads = 0;
  const file = new File(["12345678"], "server.zip"), ready = deferred();
  const controller = createContinuationController({ conversationId: cid, runId: rid, storage: null, makeRequestId: () => id(77),
    sha256: async (_file, options) => {
      hashes++; controls = options;
      if (hashes > 1) { options.onProgress(file.size, file.size); return "a".repeat(64); }
      options.onProgress(4, file.size); ready.resolve();
      await new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => reject(new DOMException("已取消文件校验。", "AbortError")), { once: true }));
    }, client: { attachments: { prepare: async () => ({ attachment: { attachment_id: newLog } }),
      upload: async (_prepared, actual, options) => { uploads++; assert.equal(actual, file); assert.equal(options.signal, controls.signal); },
    } } });
  controller.subscribe((value) => { state = value; });
  const root = container(), dispose = mountContinuationView(root, controller, { conversationId: cid, runId: rid });
  const pending = controller.uploadFiles([file]); await ready.promise;
  assert.equal(state.uploads[0].status, "CHECKING"); assert.ok(root.textContent.includes("正在校验 50%"));
  assert.equal(uploads, 0); const cancel = find(root, "button", "取消校验和上传"); assert.equal(cancel.hidden, false);
  cancel.dispatch("click"); await assert.rejects(pending, { name: "AbortError" });
  assert.equal(state.uploads[0].status, "CANCELLED"); assert.equal(cancel.hidden, true);
  assert.equal(find(root, "button", "重试上次请求").hidden, false);
  await controller.retry(); assert.equal(hashes, 2); assert.equal(uploads, 1); assert.equal(state.uploads[0].status, "READY");
  assert.equal(controls.signal.aborted, false); dispose();
});

test("切换报告终止正在上传的请求", async () => {
  const ready = deferred(); let signal, hashes = 0;
  const controller = createContinuationController({ conversationId: cid, runId: rid, storage: null, makeRequestId: () => id(77),
    sha256: async () => { hashes++; return "a".repeat(64); }, client: { attachments: {
      prepare: async () => ({ attachment: { attachment_id: newLog } }), upload: async (_prepared, _file, options) => {
        signal = options.signal; ready.resolve();
        await new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(new DOMException("已取消", "AbortError")), { once: true }));
      },
    } } });
  const pending = controller.uploadFiles([new File(["bytes"], "server.zip")]); await ready.promise;
  controller.destroy(); await pending; assert.equal(signal.aborted, true); assert.equal(hashes, 1);
});

test("上传前拒绝不支持的后缀与超额总量", async () => {
  const controller = createContinuationController({ conversationId: cid, runId: rid, storage: null,
    sha256: () => assert.fail("无效文件不能开始哈希") });
  await assert.rejects(controller.uploadFiles([new File(["a"], "LOG.ZIP")]), /请选择/);
  await assert.rejects(controller.uploadFiles([new File(["a"], "logs.TAR.gz")]), /请选择/);
  await assert.rejects(controller.uploadFiles(Array.from({ length: 21 }, () => new File(["a"], "a.zip"))), /20/);
  await assert.rejects(controller.uploadFiles([{ name: "a.zip", size: 2_684_354_560 }, { name: "b.zip", size: 2_684_354_560 }, { name: "c.zip", size: 1 }]), /5 GiB/);
  controller.destroy();
});

test("分块文件 SHA-256 与标准实现一致，且不读取整个文件", async () => {
  for (const length of [0, 3, 55, 56, 63, 64, 65, 1024 * 1024, 1024 * 1024 + 17]) {
    const bytes = Buffer.alloc(length); for (let i = 0; i < length; i++) bytes[i] = i % 251;
    const file = new Blob([bytes]), originalSlice = file.slice.bind(file);
    file.arrayBuffer = () => assert.fail("不能一次读整个文件");
    file.slice = (start, end) => { assert.ok(end - start <= 1024 * 1024); return originalSlice(start, end); };
    assert.equal(await sha256File(file, { yieldTask: async () => {} }), createHash("sha256").update(bytes).digest("hex"));
  }
  const abort = new AbortController(); abort.abort();
  await assert.rejects(sha256File(new Blob(["x"]), { signal: abort.signal }), { name: "AbortError" });
});

class DomNode {
  constructor(ownerDocument, tagName) { Object.assign(this, { ownerDocument, tagName, children: [], attributes: {}, listeners: new Map(), ownText: "", value: "", hidden: false, disabled: false }); }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map((item) => item.textContent).join(""); }
  set innerHTML(_value) { assert.fail("不允许解析不可信 HTML"); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(key, work) { this.listeners.set(key, work); }
  dispatch(type) { this.listeners.get(type)?.({ type, preventDefault() {} }); }
  append(...items) { for (const item of items) this.children.push(...(item.tagName === "#fragment" ? item.children : [item])); }
  prepend(...items) { this.children.unshift(...items); }
  replaceChildren(...items) { this.children = []; this.ownText = ""; this.append(...items); }
}
const container = () => { const doc = { createElement: (tag) => new DomNode(doc, tag), createDocumentFragment: () => new DomNode(doc, "#fragment") }; return doc.createElement("main"); };
const nodes = (root) => [root, ...root.children.flatMap(nodes)];
const find = (root, tag, text) => nodes(root).find((item) => item.tagName === tag && (text === undefined || item.textContent.includes(text)));
const viewState = (overrides = {}) => ({ draft: draft(), versions: versions(), uploads: [], pending: null, busy: false, uploading: false, loading: false, error: null, ...overrides });

test("FOLLOWUP 面板只展示安全文字和追问操作，不显示诊断确认表单", async () => {
  const root = container(); let emit, sent = 0, reopened = 0;
  const answerDraft = draft({ status: "FOLLOWUP", followup_text: "为什么认为是 <script>连接池耗尽</script>？" });
  const controller = { subscribe: (work) => { emit = work; work(viewState({ draft: answerDraft })); return () => {}; }, destroy() {},
    sendFollowup: async () => { sent++; }, newDraft: () => { reopened++; }, confirm: () => assert.fail("不能确认报告追问") };
  const dispose = mountContinuationView(root, controller, { conversationId: cid, runId: rid });
  assert.ok(root.textContent.includes("这是对当前报告的追问，无需新建诊断"));
  const send = find(root, "button", "发送报告追问"), reopen = find(root, "button", "重新准备继续诊断");
  assert.equal(nodes(root).filter((item) => item.tagName === "form").every((item) => item.hidden), true);
  assert.equal(find(root, "button", "确认并继续诊断").disabled, true);
  assert.equal(nodes(root).filter((item) => item.tagName === "script").length, 0);
  assert.equal(find(root, "pre", "连接池耗尽").textContent, answerDraft.followup_text);
  assert.equal(send.disabled, false); send.dispatch("click"); await settle(); assert.equal(sent, 1);
  assert.equal(reopen.hidden, false); reopen.dispatch("click"); assert.equal(reopened, 1);
  emit(viewState({ draft: answerDraft, pending: { action: "followup" } }));
  assert.equal(send.disabled, true); assert.equal(reopen.disabled, true);
  emit(viewState({ draft: answerDraft, followup_receipt: followupReceipt("sent") }));
  assert.equal(send.textContent, "查看报告追问"); dispose();
});

test("草稿必须先预览；编辑后禁止确认，保存发送最终日志集合", async () => {
  const root = container(), updates = []; let emit, confirmed = 0;
  const controller = { subscribe: (work) => { emit = work; work(viewState()); return () => {}; }, destroy() {},
    update: async (input) => { updates.push(input); }, confirm: async () => { confirmed++; } };
  const dispose = mountContinuationView(root, controller, { conversationId: cid, runId: rid });
  const confirm = find(root, "button", "确认并继续诊断"), editor = nodes(root).filter((item) => item.tagName === "form")[1];
  assert.equal(confirm.disabled, false); const problem = find(editor, "textarea");
  problem.value = "<script>用户更正</script>"; problem.dispatch("input"); assert.equal(confirm.disabled, true);
  const checkbox = nodes(editor).find((item) => item.tagName === "input" && item.value === oldLog);
  checkbox.checked = false; checkbox.dispatch("change"); editor.dispatch("submit"); await settle();
  assert.equal(updates[0].effective_problem_text, problem.value); assert.deepEqual(updates[0].attachment_ids, [newLog]);
  assert.equal(confirmed, 0);
  emit(viewState({ draft: draft({ revision: 2, effective_problem_text: problem.value, attachments: [logB], attachment_ids: [newLog],
    input_changes: { ...changes, after_problem_text: problem.value, removed_attachments: [logA] } }) }));
  assert.equal(confirm.disabled, false); confirm.dispatch("click"); await settle(); assert.equal(confirmed, 1);
  assert.equal(nodes(root).filter((item) => item.tagName === "script").length, 0); dispose();
});

test("并排显示真实报告及输入变化，不从正文伪造语义条目", async () => {
  const root = container(), calls = [], selected = [];
  const allVersions = versions({ latest_run_id: next, versions: [...versions().versions, { run_id: next, parent_run_id: rid,
    version: 2, created_at: "2026-10-16T01:00:00Z", input_changes: changes, diagnosis_goal: "核对", runtime_changes: ["模型配置已更新"] }] });
  const controller = { subscribe: (work) => { work(viewState({ draft: null, versions: allVersions })); return () => {}; }, destroy() {} };
  const client = { conversations: { get: async (_cid, options) => {
    calls.push(options); return { conversation_id: cid, selected_run_id: options.run_id, result: { report_state: "READY", format: "markdown",
      markdown: options.run_id === rid ? "原报告：原因待确认" : "## 本轮变化\n撤回先前推测，新增服务端证据。", failure: null } };
  } } };
  const dispose = mountContinuationView(root, controller, { client, conversationId: cid, runId: next, onSelectRun: (value) => selected.push(value) });
  find(root, "button", "并排查看").dispatch("click"); await settle();
  assert.deepEqual(calls.map((item) => item.run_id), [rid, next]); assert.ok(calls.every((item) => item.include.join() === "report"));
  for (const text of ["原报告：原因待确认", "撤回先前推测，新增服务端证据。", "模型配置已更新", "14:30 超时", "server.zip"]) assert.ok(root.textContent.includes(text), text);
  find(root, "button", "查看所选版本").dispatch("click"); await settle(); assert.deepEqual(selected, [next]); dispose();
});

test("失败日志不能悄悄漏过确认，已取消的新日志仍可重新勾选", () => {
  const root = container(); let emit;
  const controller = { subscribe: (work) => { emit = work; work(viewState({ uploads: [
    { local_id: "new-log", attachment_id: newLog, name: "server.zip", status: "FAILED", size: 32 },
  ] })); return () => {}; }, destroy() {} };
  const dispose = mountContinuationView(root, controller, { conversationId: cid, runId: rid });
  assert.equal(find(root, "button", "确认并继续诊断").disabled, true);
  const uploads = [{ local_id: "new-log", attachment_id: newLog, name: "server.zip", status: "READY", size: 32 }];
  emit(viewState({ uploads }));
  emit(viewState({ uploads, draft: draft({ revision: 2, attachments: [logA], attachment_ids: [oldLog],
    input_changes: { ...changes, added_attachments: [] } }) }));
  const option = nodes(root).find((item) => item.tagName === "input" && item.value === newLog);
  assert.ok(option); assert.equal(option.checked, false);
  assert.equal(find(root, "button", "确认并继续诊断").disabled, false); dispose();
});

test("确认后的新一轮失败时，刷新父报告可重新准备草稿；运行中不能重开", async () => {
  const storage = memoryStorage();
  storage.setItem(`xiaodao-continuation:${cid}:${rid}`, JSON.stringify({ draft_id: did, pending: null }));
  let canContinue = false, created = 0, state;
  const client = { continuations: {
    versions: async () => versions({ can_continue: canContinue }),
    get: async () => draft({ status: "CONFIRMED", confirmed_run_id: next }),
    create: async (_cid, runId, input) => {
      created++; assert.equal(runId, rid); assert.equal(input.request_id, "new-attempt"); return draft({ draft_id: id(99) });
    },
  } };
  const controller = createContinuationController({ client, conversationId: cid, runId: rid, storage, makeRequestId: () => "new-attempt" });
  controller.subscribe((value) => { state = value; });
  const root = container(), dispose = mountContinuationView(root, controller, { client, conversationId: cid, runId: rid });
  await controller.start();
  const reopen = find(root, "button", "重新准备继续诊断"), initial = nodes(root).find((item) => item.tagName === "form");
  assert.equal(reopen.hidden, true); assert.equal(controller.newDraft(), false); assert.equal(initial.hidden, true);
  canContinue = true; await controller.refresh();
  assert.equal(reopen.hidden, false); reopen.dispatch("click");
  assert.equal(initial.hidden, false); assert.equal(state.draft, null);
  assert.deepEqual(JSON.parse(storage.getItem(`xiaodao-continuation:${cid}:${rid}`)), { draft_id: null, pending: null });
  initial.dispatch("submit"); await settle();
  assert.equal(created, 1); assert.equal(state.draft.draft_id, id(99)); dispose();
});

test("离线预览完成多日志追加、替换、确认、切换历史版本且旧报告不变", async () => {
  const client = createAgentClient({ fetchImpl: createPreviewApi(previewSamples()) });
  const directory = await client.conversations.list(), conversationId = directory.items[6].conversation_id;
  const original = await client.conversations.get(conversationId), runId = original.selected_run_id;
  async function upload(name, content) {
    const file = new File([content], name), hash = createHash("sha256").update(content).digest("hex");
    const prepared = await client.attachments.prepare(conversationId, { request_id: name, name, content_type: "application/zip",
      declared_size: file.size, declared_sha256: hash });
    await client.attachments.upload(prepared, file); return prepared.attachment.attachment_id;
  }
  const first = await upload("first.zip", "first"), second = await upload("second.zip", "second");
  const created = await client.continuations.create(conversationId, runId, { request_id: "draft", text: "时间改为14:30", attachment_ids: [first, second], log_mode: "APPEND" });
  assert.deepEqual(created.attachment_ids, [first, second]);
  const edited = await client.continuations.update(conversationId, created.draft_id, { request_id: "edit", expected_revision: 1,
    effective_problem_text: "14:30 付款超时", log_mode: "REPLACE", attachment_ids: [second] });
  const result = await client.continuations.confirm(conversationId, created.draft_id, { request_id: "confirm", expected_revision: edited.revision });
  const duplicate = await client.continuations.confirm(conversationId, created.draft_id, { request_id: "confirm", expected_revision: edited.revision });
  assert.equal(result.run_id, duplicate.run_id);
  const history = await client.continuations.versions(conversationId, result.run_id); assert.equal(history.versions.length, 2);
  const old = await client.conversations.get(conversationId, { run_id: runId }); assert.deepEqual(old.result, original.result);
  const added = await client.continuations.create(conversationId, result.run_id, { request_id: "append-again", attachment_ids: [first], log_mode: "APPEND" });
  assert.deepEqual(added.attachment_ids, [second, first]);
});

test("离线预览 FOLLOWUP 草稿只追加原报告问答，不能确认或改写为新诊断", async () => {
  const scheduled = [], client = createAgentClient({ fetchImpl: createPreviewApi(previewSamples(), { schedule: (work) => scheduled.push(work) }) });
  const directory = await client.conversations.list(), conversationId = directory.items[6].conversation_id;
  const original = await client.conversations.get(conversationId), runId = original.selected_run_id;
  let result;
  const controller = createContinuationController({ client, conversationId, runId, storage: null,
    makeRequestId: (() => { let index = 0; return () => `preview-question-${++index}`; })(), onFollowup: (value) => { result = value; } });
  await controller.start();
  const created = await controller.create({ text: "请解释报告中的结论。" });
  assert.equal(created.status, "FOLLOWUP"); assert.equal(created.followup_text, "请解释报告中的结论。");
  await assert.rejects(client.continuations.confirm(conversationId, created.draft_id, { request_id: "invalid-confirm", expected_revision: created.revision }), { status: 409 });
  await assert.rejects(client.continuations.update(conversationId, created.draft_id, { request_id: "invalid-edit", expected_revision: created.revision,
    effective_problem_text: "直接修改草稿" }), { status: 409 });
  await controller.sendFollowup(); await controller.sendFollowup();
  assert.equal(result.run_id, runId); assert.equal(scheduled.length, 1);
  assert.equal((await client.followups.list(conversationId, runId)).items.length, 1);
  assert.equal((await client.continuations.versions(conversationId, runId)).versions.length, 1);
  const current = await client.conversations.get(conversationId);
  assert.equal(current.current_run.run_id, runId); assert.deepEqual(current.result, original.result);
  assert.equal(controller.newDraft(), true);
  const changed = await controller.create({ text: "时间改为 14:30。" }); assert.equal(changed.status, "DRAFT");
  controller.destroy();
});
