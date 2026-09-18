import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { once } from "node:events";
import test from "node:test";
import { AgentApiError, createAgentClient } from "./browser-client.js";
import { createAgentBackend } from "./server.ts";

const conversationId = "10000000-0000-0000-0000-000000000001";
const attachmentId = "20000000-0000-0000-0000-000000000002";
const otherId = "30000000-0000-0000-0000-000000000003";
const prefix = `/api/agent/conversations/${conversationId}`;
const payload = Buffer.from("合成压缩日志原始字节\r\n");
const sha256 = createHash("sha256").update(payload).digest("hex");

function envelope(data, status = 200) {
  return new Response(JSON.stringify({ ok: true, data, error: null }), {
    status, headers: { "Content-Type": "application/json" },
  });
}

function preparedUpload() {
  return {
    attachment: {
      attachment_id: attachmentId, conversation_id: conversationId, request_id: "reserve-original",
      name: "logs.zip", content_type: "application/zip", size: payload.length, sha256,
      status: "RESERVED", created_at: "2026-09-17T00:00:00.000Z", case_attachment_id: null,
    },
    upload: {
      attachment_id: attachmentId, method: "PUT", url: `/api/agent/attachments/${attachmentId}/content`,
      required_headers: {
        "Idempotency-Key": attachmentId, "Content-Type": "application/zip", "X-Content-SHA256": sha256,
      },
      expected_content_length: payload.length, max_bytes: 2684354560, expires_at: null,
    },
  };
}

function noReadBlob() {
  const file = new Blob([payload], { type: "application/zip" });
  for (const method of ["arrayBuffer", "text", "stream", "slice"]) {
    Object.defineProperty(file, method, { value: () => assert.fail("上传封装不能重新扫描或复制文件。") });
  }
  return file;
}

test("browser methods use website paths, unchanged request bodies and unwrapped data", async () => {
  const calls = [];
  const client = createAgentClient({ fetchImpl: async (path, init) => {
    calls.push({ path, init });
    return envelope({ accepted: calls.length });
  } });
  const message = { request_id: "message-original", text: "RPC 超时", attachment_ids: [attachmentId] };
  const metadata = { request_id: "reserve-original", name: "logs.zip", content_type: "application/zip",
    declared_size: payload.length, declared_sha256: sha256 };
  const signal = new AbortController().signal;
  assert.deepEqual(await client.conversations.create("create-original"), { accepted: 1 });
  assert.deepEqual(await client.conversations.send(conversationId, message), { accepted: 2 });
  assert.deepEqual(await client.conversations.get(conversationId, { signal }), { accepted: 3 });
  assert.deepEqual(await client.conversations.get(conversationId, { include: [], signal }), { accepted: 4 });
  assert.deepEqual(await client.conversations.get(conversationId, { include: ["report", "artifacts"], signal }), { accepted: 5 });
  assert.deepEqual(await client.attachments.prepare(conversationId, metadata), { accepted: 6 });
  assert.deepEqual(calls.map(({ path, init }) => [path, init.method]), [
    ["/api/agent/conversations", "POST"], [`${prefix}/messages`, "POST"],
    [prefix, "GET"], [`${prefix}?include=none`, "GET"], [`${prefix}?include=report,artifacts`, "GET"],
    ["/api/agent/attachments", "POST"],
  ]);
  assert.deepEqual(JSON.parse(calls[0].init.body), { request_id: "create-original" });
  assert.deepEqual(JSON.parse(calls[1].init.body), message);
  assert.deepEqual(JSON.parse(calls[5].init.body), { ...metadata, conversation_id: conversationId });
  assert.deepEqual(Object.keys(client).sort(), ["attachments", "conversations"]);
  assert.deepEqual(Object.keys(client.conversations).sort(), ["create", "delete", "eventsUrl", "get", "list", "rename", "send", "stop"]);
  assert.deepEqual(Object.keys(client.attachments).sort(), ["prepare", "upload"]);
  for (const { init } of calls) {
    assert.equal(init.credentials, "same-origin");
    assert.equal(init.redirect, "error");
    assert.equal(init.cache, "no-store");
    if (init.method === "POST") assert.equal(init.headers.get("Content-Type"), "application/json");
    else { assert.equal(init.body, undefined); assert.equal(init.signal, signal); }
  }
  assert.equal(client.conversations.eventsUrl(conversationId), `${prefix}/events`);
  assert.equal(calls.length, 6, "只生成 events URL，不自动建立订阅或轮询。");
});

test("browser client supports a same-origin path prefix and refuses network base addresses", async () => {
  const paths = [];
  const client = createAgentClient({ basePath: "/website/api/agent/", fetchImpl: async (path) => {
    paths.push(path);
    return envelope({ report_state: "PENDING" });
  } });
  await client.conversations.get(conversationId, { include: ["report"] });
  assert.deepEqual(paths, [`/website/api/agent/conversations/${conversationId}?include=report`]);
  assert.equal(client.conversations.eventsUrl(conversationId), `/website/api/agent/conversations/${conversationId}/events`);
  for (const basePath of ["https://outside.example/api", "//outside.example/api", "/api?token=x", "/api#part", "/../api"]) {
    assert.throws(() => createAgentClient({ basePath }), TypeError);
  }
});

test("browser get rejects malformed includes before sending a request", () => {
  const client = createAgentClient({ fetchImpl: async () => assert.fail("非法 include 不能发起请求。") });
  for (const include of [null, "report", ["report", "report"], ["none"], ["unknown"]]) {
    assert.throws(() => client.conversations.get(conversationId, { include }), TypeError);
  }
});

test("browser management and history keep cursors and stop request identities unchanged", async () => {
  const calls = [];
  const client = createAgentClient({ headers: { "X-Agent-Owner-Key": "browser-supplied" }, fetchImpl: async (path, init) => {
    calls.push({ path, init }); assert.equal(init.headers.has("X-Agent-Owner-Key"), false);
    return envelope({ request: calls.length });
  } });
  await client.conversations.list();
  await client.conversations.list({ cursor: "opaque+/=", limit: 20 });
  await client.conversations.rename(conversationId, "新的标题");
  await client.conversations.stop(conversationId, { request_id: "stop-original", run_id: otherId });
  await client.conversations.stop(conversationId, { request_id: "stop-original", run_id: otherId });
  await client.conversations.delete(conversationId);
  await client.conversations.get(conversationId, { include: ["history", "report"], run_id: otherId,
    history_before: "opaque+/=", history_limit: 100 });
  assert.deepEqual(calls.map(({ path, init }) => [path, init.method]), [
    ["/api/agent/conversations", "GET"], ["/api/agent/conversations?cursor=opaque%2B%2F%3D&limit=20", "GET"],
    [prefix, "PATCH"], [prefix + "/stop", "POST"], [prefix + "/stop", "POST"], [prefix, "DELETE"],
    [prefix + `?include=history,report&run_id=${otherId}&history_before=opaque%2B%2F%3D&history_limit=100`, "GET"],
  ]);
  assert.deepEqual(JSON.parse(calls[2].init.body), { title: "新的标题" });
  assert.deepEqual(JSON.parse(calls[3].init.body), { request_id: "stop-original", run_id: otherId });
  assert.equal(calls[3].init.body, calls[4].init.body);
  assert.equal(calls[5].init.body, undefined);
  for (const limit of [0, 101, "20", 1.5]) {
    assert.throws(() => client.conversations.list({ limit }), TypeError);
    assert.throws(() => client.conversations.get(conversationId, { history_limit: limit }), TypeError);
  }
});

for (const data of [
  { report_state: "PENDING", format: null, report: null, markdown: null, failure: null },
  { report_state: "UNAVAILABLE", format: null, report: null, markdown: null,
    failure: { code: "OUTCOME_INVALID", message: "本次定位未能完成。", details: [], retryable: false } },
  ...["COMPLETED", "PARTIAL", "INCONCLUSIVE"].map((status) => ({
    report_state: "READY", format: "problem-locator-diagnosis-v3", report: { status }, archive_status: "PENDING",
  })),
  { report_state: "READY", format: "markdown", report: null, markdown: "# 定位报告\r\n保留原文。\n" },
  { report_state: "READY", format: "generic-v1", report: { conclusion: "历史结论", root_cause_analysis: "历史分析" } },
]) {
  test(`browser client preserves report state and content: ${data.report_state}/${data.report?.status ?? data.format}`, async () => {
    let calls = 0;
    const detail = { schema_version: 3, conversation_id: conversationId, report_state: data.report_state,
      included: ["report"], history: null, attachments: null, result: data, artifacts: null };
    const client = createAgentClient({ fetchImpl: async () => { calls++; return envelope(detail); } });
    assert.deepEqual(await client.conversations.get(conversationId, { include: ["report"] }), detail);
    assert.equal(calls, 1);
  });
}

test("browser client retains controlled server error details without resubmitting", async () => {
  let calls = 0;
  const error = { code: "DISPATCH_REJECTED", message: "结果交付状态暂时无法确认。",
    details: [{ field: "phase", actual: "RESULT_DELIVERY" }, { field: "persistence", actual: "UNKNOWN" }], retryable: true };
  const client = createAgentClient({ fetchImpl: async () => {
    calls++;
    return new Response(JSON.stringify({ ok: false, data: null, error }), { status: 503 });
  } });
  await assert.rejects(client.conversations.send(conversationId, { request_id: "keep-me", text: "问题" }), (caught) => {
    assert.ok(caught instanceof AgentApiError);
    assert.equal(caught.status, 503);
    for (const key of ["code", "message", "details", "retryable"]) assert.deepEqual(caught[key], error[key]);
    return true;
  });
  assert.equal(calls, 1);
});

test("browser client rejects invalid or non-JSON responses and network failures without retry", async () => {
  const cases = [
    { respond: () => new Response("<html>login</html>"), code: "WEBSITE_INVALID_RESPONSE", status: 200 },
    { respond: () => new Response("proxy failed", { status: 502 }), code: "WEBSITE_INVALID_RESPONSE", status: 502 },
    { respond: () => new Response(JSON.stringify({ ok: true, data: null, error: null })), code: "WEBSITE_INVALID_RESPONSE", status: 200 },
    { respond: () => envelope({ report_state: "READY" }, 503), code: "WEBSITE_INVALID_RESPONSE", status: 503 },
    { respond: () => { throw new TypeError("private network details"); }, code: "WEBSITE_NETWORK_ERROR", status: 0 },
  ];
  for (const scenario of cases) {
    let calls = 0;
    const client = createAgentClient({ fetchImpl: async () => { calls++; return scenario.respond(); } });
    await assert.rejects(client.conversations.create("fixed-create-id"), (error) => {
      assert.ok(error instanceof AgentApiError);
      assert.equal(error.code, scenario.code);
      assert.equal(error.status, scenario.status);
      assert.equal(error.retryable, false);
      assert.ok(!error.message.includes("private network details"));
      return true;
    });
    assert.equal(calls, 1);
  }
});

test("browser client preserves explicit cancellation", async () => {
  const controller = new AbortController();
  const cancelled = new DOMException("读取已取消", "AbortError");
  controller.abort();
  let calls = 0;
  const client = createAgentClient({ fetchImpl: async (_path, init) => {
    calls++;
    assert.equal(init.signal, controller.signal);
    throw cancelled;
  } });
  await assert.rejects(client.conversations.get(conversationId, { signal: controller.signal }), (error) => error === cancelled);
  assert.equal(calls, 1);
});

test("browser client preserves cancellation while reading the JSON response body", async () => {
  const controller = new AbortController();
  const cancelled = new DOMException("报告读取已取消", "AbortError");
  let bodyController;
  const body = new ReadableStream({ start(stream) { bodyController = stream; } });
  controller.signal.addEventListener("abort", () => bodyController.error(cancelled), { once: true });
  let calls = 0;
  const client = createAgentClient({ fetchImpl: async (_path, init) => {
    calls++;
    assert.equal(init.signal, controller.signal);
    return new Response(body, { headers: { "Content-Type": "application/json" } });
  } });
  const pending = client.conversations.get(conversationId, { signal: controller.signal });
  queueMicrotask(() => controller.abort());
  await assert.rejects(pending, (error) => error === cancelled);
  assert.equal(calls, 1);
});

test("explicit creation retry retains the browser request ID instead of the namespaced receipt", async () => {
  const received = [];
  const client = createAgentClient({ fetchImpl: async (_path, init) => {
    received.push(JSON.parse(init.body));
    return envelope({ conversation_id: conversationId, request_id: "server-user-scoped-hash", schema_version: 1 });
  } });
  const first = await client.conversations.create("browser-original-id");
  const second = await client.conversations.create("browser-original-id");
  assert.equal(first.request_id, "server-user-scoped-hash");
  assert.deepEqual(second, first);
  assert.deepEqual(received, [{ request_id: "browser-original-id" }, { request_id: "browser-original-id" }]);
});

test("CSRF headers are refreshed on every request and protocol headers override caller defaults", async () => {
  const sent = [];
  let token = "csrf-1";
  const client = createAgentClient({ headers: () => ({ "X-CSRF-Token": token,
    "Content-Type": "text/plain", "Content-Length": "999", "X-Content-SHA256": "wrong-default" }),
  fetchImpl: async (_path, init) => {
    sent.push(init.headers);
    return envelope({ accepted: true });
  } });
  await client.conversations.create("csrf-create");
  token = "csrf-2";
  await client.conversations.get(conversationId);
  token = "csrf-3";
  await client.attachments.upload(preparedUpload(), noReadBlob());
  assert.deepEqual(sent.map((headers) => headers.get("X-CSRF-Token")), ["csrf-1", "csrf-2", "csrf-3"]);
  assert.equal(sent[0].get("Content-Type"), "application/json");
  assert.equal(sent[2].get("Content-Type"), "application/zip");
  assert.equal(sent[2].get("X-Content-SHA256"), sha256);
  assert.ok(sent.every((headers) => !headers.has("Content-Length")));
});

test("upload sends the original Blob once without reading it or addressing the descriptor URL", async () => {
  const file = noReadBlob();
  const calls = [];
  const client = createAgentClient({ basePath: "/website/api/agent", fetchImpl: async (path, init) => {
    calls.push({ path, init });
    return envelope({ ...preparedUpload().attachment, status: "READY" });
  } });
  for (const url of ["https://outside.example/steal", "//outside.example/steal", "/admin/delete", "javascript:alert(1)"]) {
    const prepared = preparedUpload();
    prepared.upload.url = url;
    const result = await client.attachments.upload(prepared, file);
    assert.equal(result.status, "READY");
  }
  assert.equal(calls.length, 4);
  for (const { path, init } of calls) {
    assert.equal(path, `/website/api/agent/attachments/${attachmentId}/content`);
    assert.equal(init.body, file);
    assert.equal(init.method, "PUT");
    assert.equal(init.headers.get("Idempotency-Key"), attachmentId);
    assert.equal(init.headers.get("X-Content-SHA256"), sha256);
    assert.equal(init.headers.get("Content-Type"), "application/zip");
    assert.equal(init.headers.has("Content-Length"), false);
  }
});

const invalidUploads = {
  "wrong method": (prepared) => { prepared.upload.method = "POST"; },
  "wrong descriptor ID": (prepared) => { prepared.upload.attachment_id = otherId; },
  "invalid attachment ID": (prepared) => { prepared.attachment.attachment_id = prepared.upload.attachment_id = "../other"; },
  "wrong expected size": (prepared) => { prepared.upload.expected_content_length++; },
  "wrong attachment size": (prepared) => { prepared.attachment.size++; },
  "wrong header ID": (prepared) => { prepared.upload.required_headers["Idempotency-Key"] = "reservation-request-id"; },
  "wrong header type": (prepared) => { prepared.upload.required_headers["Content-Type"] = "application/json"; },
  "wrong header hash": (prepared) => { prepared.upload.required_headers["X-Content-SHA256"] = "f".repeat(64); },
  "invalid hash": (prepared) => { prepared.attachment.sha256 = prepared.upload.required_headers["X-Content-SHA256"] = "not-a-hash"; },
  "missing headers": (prepared) => { delete prepared.upload.required_headers; },
};
for (const [name, mutate] of Object.entries(invalidUploads)) {
  test(`upload rejects ${name} before network IO`, async () => {
    const client = createAgentClient({ fetchImpl: async () => assert.fail("无效预约不能发起上传。") });
    const prepared = preparedUpload();
    mutate(prepared);
    await assert.rejects(client.attachments.upload(prepared, noReadBlob()), TypeError);
  });
}

test("upload rejects non-Blob bodies and incomplete descriptors without reading a file", async () => {
  const client = createAgentClient({ fetchImpl: async () => assert.fail("无效上传不能发起请求。") });
  for (const body of [null, "raw text", { size: payload.length }, new Uint8Array(payload)]) {
    await assert.rejects(client.attachments.upload(preparedUpload(), body), TypeError);
  }
  for (const prepared of [undefined, null, {}, { attachment: preparedUpload().attachment }]) {
    await assert.rejects(client.attachments.upload(prepared, noReadBlob()), TypeError);
  }
});

test("browser client and real website backend complete create, prepare, upload, send and report", async () => {
  const ownedConversations = new Set(), ownedAttachments = new Set();
  const upstreamCalls = [], receivedTokens = [];
  const prepared = preparedUpload();
  const result = {
    schema_version: 1, conversation_id: conversationId, case_id: otherId, case_revision: 5,
    case_status: "RESOLVED", archive_status: "NOT_REQUIRED", report_state: "READY",
    source_job_id: otherId, format: "markdown", report: null, markdown: "# 合成定位结果\r\n问题已确认。\n",
    artifact: { artifact_id: otherId }, failure: null,
  };
  const access = {
    authenticate: async (request) => { receivedTokens.push(request.headers["x-csrf-token"]); return { id: "alice" }; },
    ownsConversation: async (_user, id) => ownedConversations.has(id),
    rememberConversation: async (_user, id) => { ownedConversations.add(id); },
    ownsAttachment: async (_user, id) => ownedAttachments.has(id),
    rememberAttachment: async (_user, conversation, id) => {
      assert.equal(conversation, conversationId);
      ownedAttachments.add(id);
    },
  };
  const server = createAgentBackend({ upstream: "http://xiaodao.internal/internal", access,
    fetchImpl: async (url, init = {}) => {
      const pathname = new URL(url).pathname;
      upstreamCalls.push(pathname);
      assert.equal(new URL(url).origin, "http://xiaodao.internal");
      assert.equal(init.redirect, "manual");
      if (pathname === "/internal/api/v1/agent/conversations") {
        assert.equal(init.method, "POST");
        const expected = createHash("sha256").update(JSON.stringify(["alice", "journey-create"])).digest("hex");
        assert.deepEqual(JSON.parse(init.body), { request_id: expected });
        return envelope({ conversation_id: conversationId, request_id: expected, schema_version: 1 });
      }
      if (pathname === "/internal/api/v1/agent/attachments") {
        assert.deepEqual(JSON.parse(init.body), { request_id: "reserve-original", name: "logs.zip",
          conversation_id: conversationId, content_type: "application/zip", declared_size: payload.length, declared_sha256: sha256 });
        return envelope({ ...prepared, upload: { ...prepared.upload, url: "https://outside.example/unused" } });
      }
      if (pathname === `/internal/api/v1/agent/attachments/${attachmentId}/content`) {
        assert.equal(init.method, "PUT");
        assert.equal(init.headers.get("Content-Length"), String(payload.length));
        assert.equal(init.headers.get("Idempotency-Key"), attachmentId);
        assert.equal(init.headers.get("Content-Type"), "application/zip");
        assert.equal(init.headers.get("X-Content-SHA256"), sha256);
        const chunks = [];
        for await (const chunk of init.body) chunks.push(chunk);
        assert.deepEqual(Buffer.concat(chunks), payload);
        return envelope({ ...prepared.attachment, status: "READY" });
      }
      if (pathname === `/internal/api/v1/agent/conversations/${conversationId}/messages`) {
        assert.deepEqual(JSON.parse(init.body), { request_id: "journey-message", text: "请定位这个问题。", attachment_ids: [attachmentId] });
        return envelope({ conversation_id: conversationId, message_id: otherId, request_id: "journey-message", event_id: 3, status: "ACCEPTED" });
      }
      if (pathname === `/internal/api/v1/agent/conversations/${conversationId}`) {
        assert.equal(new URL(url).search, "?include=report");
        return envelope({ schema_version: 3, conversation_id: conversationId, case_id: result.case_id,
          selected_run_id: otherId, current_run: { run_id: otherId }, capabilities: { can_send: true },
          source_job_id: result.source_job_id, case_status: result.case_status, report_state: "READY",
          included: ["report"], result, history: null, attachments: null, artifacts: null, failure: null });
      }
      assert.fail(`unexpected upstream call: ${pathname}`);
    },
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  let token = "create-csrf";
  const origin = `http://127.0.0.1:${server.address().port}`;
  const client = createAgentClient({ headers: () => ({ "X-CSRF-Token": token }),
    fetchImpl: (path, init) => fetch(new URL(path, origin), init) });
  try {
    const created = await client.conversations.create("journey-create");
    assert.equal(created.conversation_id, conversationId);
    assert.notEqual(created.request_id, "journey-create");
    token = "reserve-csrf";
    const reservation = await client.attachments.prepare(created.conversation_id, { request_id: "reserve-original",
      name: "logs.zip", content_type: "application/zip", declared_size: payload.length, declared_sha256: sha256 });
    assert.equal(reservation.upload.url, `/api/agent/attachments/${attachmentId}/content`);
    token = "upload-csrf";
    const uploaded = await client.attachments.upload(reservation, new Blob([payload]));
    assert.equal(uploaded.status, "READY");
    token = "message-csrf";
    const receipt = await client.conversations.send(created.conversation_id, { request_id: "journey-message",
      text: "请定位这个问题。", attachment_ids: [uploaded.attachment_id] });
    assert.equal(receipt.status, "ACCEPTED");
    token = "report-csrf";
    const detail = await client.conversations.get(created.conversation_id, { include: ["report"] });
    assert.deepEqual(detail.result, result);
    assert.equal(upstreamCalls.length, 5);
    assert.deepEqual(receivedTokens, ["create-csrf", "reserve-csrf", "upload-csrf", "message-csrf", "report-csrf"]);
  } finally {
    const closed = once(server, "close");
    server.close();
    server.closeAllConnections();
    await closed;
  }
});
