import assert from "node:assert/strict";
import { once } from "node:events";
import test from "node:test";
import { createAgentClient } from "./browser-client.js";
import { createAgentBackend } from "./server.mjs";

const cid = "10000000-0000-0000-0000-000000000001", rid = "20000000-0000-0000-0000-000000000001";
const fid = "30000000-0000-0000-0000-000000000001", comment = "40000000-0000-0000-0000-000000000001";
const root = `/api/agent/conversations/${cid}/runs/${rid}`;
const receipt = (followupId = null, requestId = "comment-1") => ({ schema_version: 1, conversation_id: cid,
  run_id: rid, followup_id: followupId, comment_id: comment, request_id: requestId, created_at: "2026-10-17T01:00:00.000Z" });
const envelope = (data) => new Response(JSON.stringify({ ok: true, data, error: null }), { headers: { "Content-Type": "application/json" } });
const send = (base, path, body, options = {}) => fetch(base + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), ...options });
async function withServer(options, work) {
  const server = createAgentBackend({ upstream: "http://internal.invalid", access: { authenticate: async () => ({ id: "alice" }) }, ...options }).listen(0, "127.0.0.1");
  await once(server, "listening");
  try { await work(`http://127.0.0.1:${server.address().port}`); }
  finally { const closed = once(server, "close"); server.close(); server.closeAllConnections(); await closed; }
}

test("standalone feedback SDK binds report or answer without followup submission", async () => {
  const calls = [], client = createAgentClient({ fetchImpl: async (url, init) => {
    calls.push({ url, init }); return envelope(receipt(url.includes("/followups/") ? fid : null));
  } });
  await client.conversations.submitFeedbackComment(cid, rid, { request_id: "comment-1", text: "缺少检查步骤" });
  await client.conversations.submitFollowupFeedbackComment(cid, rid, fid, { request_id: "comment-1" });
  assert.deepEqual(calls.map(({ url }) => url), [root + "/feedback/comments", root + `/followups/${fid}/feedback/comments`]);
  assert.equal(calls[0].init.method, "POST");
  for (const input of [{ request_id: "x", analysis_text: "冒充管理员" }, { request_id: "x", text: "中".repeat(5462) },
    { request_id: "x", text: 1 }, { request_id: " ", text: "意见" }])
    await assert.rejects(client.conversations.submitFeedbackComment(cid, rid, input), TypeError);
  assert.equal(calls.length, 2);
});

test("BFF preserves current identity and allows user-only or server analysis-only material", async () => {
  const calls = [], loads = [];
  await withServer({ loadFeedbackAnalysis: async (context) => {
    loads.push(context); assert.ok(context.authContext.ownerKey);
    return { analysis_text: "已核对日志中的连接池耗尽。", analysis_reference: "analysis-123" };
  }, fetchImpl: async (url, init) => {
    calls.push({ url: String(url), init });
    if (init.method === "GET") return envelope({ rating: "DISLIKE" });
    assert.ok(new Headers(init.headers).get("X-Agent-Owner-Key"));
    return envelope(receipt(String(url).includes("/followups/") ? fid : null));
  } }, async (base) => {
    for (const [path, input] of [[root + "/feedback/comments", { request_id: "comment-1", text: "遗漏线索" }],
      [root + `/followups/${fid}/feedback/comments`, { request_id: "comment-1", text: " " }]]) {
      const response = await send(base, path, input); assert.equal(response.status, 200); await response.json();
    }
    assert.equal(loads[0].followupId, null); assert.equal(loads[1].followupId, fid);
    assert.deepEqual(JSON.parse(calls[3].init.body), { request_id: "comment-1", text: null,
      analysis_text: "已核对日志中的连接池耗尽。", analysis_reference: "analysis-123" });
    const before = calls.length;
    for (const input of [{ request_id: "x", text: "意见", analysis_text: "伪造资料" },
      { request_id: "x", analysis_reference: "forged" }, { request_id: "x", owner_key: "forged" }]) {
      const response = await send(base, root + "/feedback/comments", input); assert.equal(response.status, 400); await response.json();
    }
    assert.equal(calls.length, before);
  });
});

test("default BFF accepts user feedback without admin loader and rejects empty submissions", async () => {
  const calls = [];
  await withServer({ fetchImpl: async (url, init) => { calls.push({ url: String(url), body: JSON.parse(init.body) }); return envelope(receipt()); } }, async (base) => {
    const response = await send(base, root + "/feedback/comments", { request_id: "comment-1", text: "请核对" });
    assert.equal(response.status, 200); await response.json();
    assert.deepEqual(calls[0].body, { request_id: "comment-1", text: "请核对", analysis_text: null, analysis_reference: null });
    for (const suffix of ["", "?share=true"]) {
      const empty = await send(base, root + "/feedback/comments" + suffix, { request_id: "x" });
      assert.equal(empty.status, 400); await empty.json();
    }
    assert.equal(calls.length, 1);
  });
});

test("server loader can preserve immutable material across retry and authorization precedes loading", async () => {
  const saved = new Map(), bodies = []; let revision = "首次分析";
  await withServer({ loadFeedbackAnalysis: async ({ authContext, conversationId, runId, followupId, requestId }) => {
    const key = JSON.stringify([authContext.ownerKey, conversationId, runId, followupId, requestId]);
    if (!saved.has(key)) saved.set(key, { analysis_text: revision, analysis_reference: "analysis-123" });
    return saved.get(key);
  }, fetchImpl: async (url, init) => {
    if (init.method === "GET") return envelope({ rating: "LIKE" }); // Old comment replay after a vote switch remains allowed.
    bodies.push(init.body); return envelope(receipt());
  } }, async (base) => {
    const path = root + "/feedback/comments", input = { request_id: "comment-1" };
    await (await send(base, path, input)).json(); revision = "后来修改的分析";
    await (await send(base, path, input)).json();
    assert.equal(bodies.length, 2); assert.equal(bodies[0], bodies[1]);
  });
  await withServer({ loadFeedbackAnalysis: () => assert.fail("不能读取其他用户的资料"), fetchImpl: async () =>
    new Response(JSON.stringify({ ok: false, data: null, error: { code: "AGENT_CONVERSATION_NOT_FOUND", message: "私有细节", retryable: false, details: [] } }), { status: 404 }) }, async (base) => {
    const response = await send(base, root + "/feedback/comments", { request_id: "comment-1", text: "意见" });
    assert.equal(response.status, 404); assert.doesNotMatch(await response.text(), /私有细节/);
  });
});

test("comment receipt mismatch and DISLIKE requirement propagate without fallback", async () => {
  await withServer({ fetchImpl: async () => envelope({ ...receipt(), request_id: "other" }) }, async (base) => {
    const response = await send(base, root + "/feedback/comments", { request_id: "comment-1", text: "意见" });
    assert.equal(response.status, 502); await response.json();
  });
  await withServer({ fetchImpl: async () => new Response(JSON.stringify({ ok: false, data: null,
    error: { code: "AGENT_FEEDBACK_REQUIRES_DISLIKE", message: "内部错误", retryable: false, details: [] } }), { status: 409 }) }, async (base) => {
    const response = await send(base, root + "/feedback/comments", { request_id: "comment-1", text: "意见" });
    assert.equal(response.status, 409); const result = await response.json();
    assert.equal(result.error.code, "AGENT_FEEDBACK_REQUIRES_DISLIKE"); assert.match(result.error.message, /请先.*点踩/);
  });
});


test("SDK invalid comment receipt uses feedback wording and never retries", async () => {
  let calls = 0;
  const client = createAgentClient({ fetchImpl: async () => { calls++; return envelope({ ...receipt(), run_id: fid }); } });
  for (const submit of [() => client.conversations.submitFeedbackComment(cid, rid, { request_id: "comment-1", text: "意见" }),
    () => client.conversations.submitFollowupFeedbackComment(cid, rid, fid, { request_id: "comment-1", text: "意见" })]) {
    await assert.rejects(submit(), (error) => error.status === 502 && error.code === "WEBSITE_INVALID_RESPONSE" &&
      error.message === "反馈回执与提交对象不一致或格式无效，请刷新后重试。");
  }
  assert.equal(calls, 2);
});
