import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { once } from "node:events";
import fs from "node:fs";
import fsPromises from "node:fs/promises";
import { syncBuiltinESMExports } from "node:module";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import timers from "node:timers";
import test from "node:test";
import { createAgentBackend } from "./server.ts";
import { createAgentBackend as createPureJsBackend, DOWNLOAD_TEMP_TTL_MS,
  DOWNLOAD_TOTAL_TIMEOUT_MS, sweepDownloadSpools } from "./server.mjs";
// 与后端示例同一 Gate 执行，保证浏览器模块变化后重新验证。
import "./report-view.test.mjs";
import "./browser-client.test.mjs";
import "./preview.test.mjs";
import "./onboarding.test.mjs";

test("TypeScript compatibility entry exports the same BFF implementation", () => {
  assert.equal(createAgentBackend, createPureJsBackend);
});

const conversation = "10000000-0000-0000-0000-000000000001";
const caseId = "20000000-0000-0000-0000-000000000001";
const artifactId = "30000000-0000-0000-0000-000000000001";
const runId = "50000000-0000-0000-0000-000000000001";
const ownerKey = createHash("sha256").update(JSON.stringify(["xiaodao-website", "alice"])).digest("hex");
const jobId = "40000000-0000-0000-0000-000000000001";
const base = "http://xiaodao.internal";
const conversationPath = `/api/agent/conversations/${conversation}`;
const reportDownloadPath = `${conversationPath}/files/${artifactId}/content`;
const allIncludes = ["history", "report", "artifacts"];
const access = {
  authenticate: async () => ({ id: "alice" }),
  ownsConversation: async (_user, id) => id === conversation,
  rememberConversation: async () => {},
  ownsAttachment: async (_user, id) => id === artifactId,
  rememberAttachment: async () => {},
};
const report = {
  schema_version: 3, format_id: "problem-locator-diagnosis-v3", status: "COMPLETED",
  source_job_type: "DIAGNOSE", problem_statement: "RPC 超时", root_cause: "连接池耗尽",
  findings: [], causal_factors: [], candidate_factors: [], excluded_factors: [],
  supporting_evidence_bindings: [], completion_criteria_mapping: [], verification_rules: [],
  time_relevance: { assessment: "UNKNOWN" }, evidence_gaps: [], limitations: [],
  recommendations: ["检查连接释放"], safety_notes: [],
};
function envelope(data) {
  return new Response(JSON.stringify({ ok: true, data, error: null }), { headers: { "Content-Type": "application/json" } });
}
async function withServer(options, exercise) {
  const server = createAgentBackend({ upstream: base, ...options });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  try { await exercise(`http://127.0.0.1:${server.address().port}`); }
  finally { const closed = once(server, "close"); server.close(); server.closeAllConnections(); await closed; }
}
function publicArtifact(kind = "USER_RESULT", payload = Buffer.from(JSON.stringify(report))) {
  return { artifact_id: artifactId, kind,
    name: kind === "USER_RESULT" ? "diagnosis-result.json" : kind === "GENERIC_REPORT" ? "generic-diagnosis.md" : "result.zip",
    content_type: kind === "USER_RESULT" ? "application/json" : kind === "GENERIC_REPORT" ? "text/markdown" : "application/zip",
    size: payload.length, sha256: createHash("sha256").update(payload).digest("hex"), resource_kind: "FILE",
    created_by_job_id: jobId, created_at: "2026-09-17T00:00:00.000Z", downloadable: true,
    download_url: `${base}/api/v1/agent/conversations/${conversation}/files/${artifactId}/content?run_id=${runId}` };
}
function nativeReport(overrides = {}) {
  const artifact = publicArtifact(); delete artifact.download_url;
  return { schema_version: 1, conversation_id: conversation, case_id: caseId, case_revision: 5,
    case_status: "RESOLVED", archive_status: "PENDING", report_state: "READY", source_job_id: jobId,
    format: "problem-locator-diagnosis-v3", report, markdown: null, artifact, failure: null, ...overrides };
}
function nativeDetail(included = allIncludes, overrides = {}) {
  return { schema_version: 3, conversation_id: conversation, title: "RPC 超时", selected_run_id: runId,
    current_run: { run_id: runId }, capabilities: { can_send: false, can_stop: false, can_rediagnose: true, can_rename: true, can_delete: true }, history_next_cursor: null, status: "RUNNING", case_id: caseId,
    case_revision: 5, job_id: null, source_job_id: jobId, case_status: "RESOLVED", archive_status: "PENDING",
    progress: { stage: "ARCHIVE", message: "正在整理目标日志" }, report_state: "READY", included,
    current_questions: [], failure: null, last_event_id: 7,
    created_at: "2026-09-17T00:00:00.000Z", updated_at: "2026-09-17T00:01:00.000Z",
    history: included.includes("history") ? [{ id: jobId, run_id: runId, type: "user.message", created_at: "2026-09-17T00:00:00.000Z",
      message: { message_id: jobId, request_id: "message-one", text: "RPC 超时", status: "APPLIED", run_id: runId,
        attachment_ids: [], created_at: "2026-09-17T00:00:00.000Z", notice: null }, questions: null, result: null }] : null,
    attachments: included.includes("history") ? [] : null, result: included.includes("report") ? nativeReport() : null,
    artifacts: included.includes("artifacts") ? [publicArtifact()] : null, ...overrides };
}
function nativeFixture(data, query = "?include=report") {
  const calls = [];
  return { calls, fetchImpl: async (url, init) => {
    calls.push(String(url));
    assert.equal(new URL(url).pathname + new URL(url).search, `/api/v1/agent/conversations/${conversation}${query}`);
    assert.equal(init.redirect, "manual"); assert.equal(init.method, undefined);
    return envelope(data);
  } };
}
function artifactFixture({ kind = "USER_RESULT", payload = Buffer.from(JSON.stringify(report)), badSource = false,
  badHash = false, badUrl = false, headerOverrides = {}, receivedPayload, downloadStatus = 200,
  artifactOverrides = {}, summaryOverrides = {}, caseOverrides = {} } = {}) {
  const calls = [], artifact = { ...publicArtifact(kind, payload), ...artifactOverrides };
  if (badUrl) artifact.download_url = "http://other.internal/secret";
  const fetchImpl = async (url, init) => {
    calls.push(String(url)); const parsed = new URL(url);
    if (parsed.pathname === `/api/v1/agent/conversations/${conversation}`) {
      assert.equal(parsed.searchParams.get("include"), "artifacts", "下载只请求产物，不加载历史或报告。");
      assert.ok([...parsed.searchParams.keys()].every((key) => ["include", "run_id"].includes(key)));
      if (parsed.searchParams.has("run_id")) assert.equal(parsed.searchParams.get("run_id"), runId);
      return envelope(nativeDetail(["artifacts"], {
        artifacts: [{ ...artifact, created_by_job_id: badSource ? artifactId : jobId, ...summaryOverrides }], ...caseOverrides }));
    }
    if (parsed.pathname === `/api/v1/agent/conversations/${conversation}/files/${artifactId}/content`) {
      assert.equal(init.redirect, "manual"); assert.equal(parsed.searchParams.get("run_id"), runId);
      const headers = new Headers({ "Content-Type": artifact.content_type,
        "Content-Length": String(payload.length), "X-Content-SHA256": artifact.sha256 });
      for (const [name, value] of Object.entries(headerOverrides)) {
        if (value === null) headers.delete(name); else headers.set(name, value);
      }
      return new Response(receivedPayload ?? (badHash ? Buffer.alloc(payload.length, 0) : payload), { status: downloadStatus, headers });
    }
    assert.fail("不应额外读取 Case、产物列表或旧报告接口。");
  };
  return { calls, artifact, fetchImpl };
}

test("full conversation uses one native read and returns history, result and authorized file links", async () => {
  const fixture = nativeFixture(nativeDetail(), "");
  await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
    const response = await fetch(origin + conversationPath); assert.equal(response.status, 200);
    const detail = (await response.json()).data;
    assert.equal(detail.schema_version, 3); assert.deepEqual(detail.included, allIncludes);
    assert.deepEqual(detail.result, nativeReport()); assert.equal(detail.result.sections, undefined);
    assert.deepEqual(detail.history, nativeDetail().history);
    assert.equal(detail.artifacts[0].download_url, reportDownloadPath + `?run_id=${runId}`);
  });
  assert.deepEqual(fixture.calls, [`${base}/api/v1/agent/conversations/${conversation}`]);
});
for (const included of [[], ["history"], ["report"], ["artifacts"], ["history", "report"], ["history", "artifacts"], ["report", "artifacts"]]) {
  const query = `?include=${included.length ? included.join(",") : "none"}`;
  test(`conversation ${query} reads once and leaves excluded fields null`, async () => {
    const fixture = nativeFixture(nativeDetail(included), query);
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + conversationPath + query); assert.equal(response.status, 200);
      const data = (await response.json()).data; assert.deepEqual(data.included, included);
      for (const [part, field] of [["history", "history"], ["history", "attachments"], ["report", "result"], ["artifacts", "artifacts"]]) {
        assert.equal(data[field] === null, !included.includes(part));
      }
    });
    assert.equal(fixture.calls.length, 1);
  });
}
test("include order is normalized and malformed queries never reach upstream", async () => {
  const fixture = nativeFixture(nativeDetail(["history", "report"]), "?include=history,report");
  await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
    assert.equal((await fetch(origin + conversationPath + "?include=report,history")).status, 200);
    for (const query of ["?include=", "?include=report,report", "?include=none,report", "?include=unknown",
      "?include=history&include=report", "?include=none&x=1", "?x=1", "?include=history,"]) {
      assert.equal((await fetch(origin + conversationPath + query)).status, 400, query);
    }
  });
  assert.equal(fixture.calls.length, 1);
});
for (const reportState of ["PENDING", "UNAVAILABLE"]) {
  test(`${reportState} remains HTTP 200 with an explicit empty result`, async () => {
    const result = nativeReport({ report_state: reportState, source_job_id: null, format: null, report: null, markdown: null, artifact: null });
    const value = nativeDetail(["report"], { report_state: reportState, source_job_id: null, result });
    const fixture = nativeFixture(value);
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + conversationPath + "?include=report"); assert.equal(response.status, 200);
      assert.deepEqual((await response.json()).data, value);
    }); assert.equal(fixture.calls.length, 1);
  });
}
for (const status of ["PARTIAL", "INCONCLUSIVE"]) {
  test(`${status} remains a READY result`, async () => {
    const caseStatus = status === "PARTIAL" ? "PARTIALLY_RESOLVED" : "UNRESOLVED";
    const payload = { ...report, status, root_cause: null, limitations: ["缺少一段日志"] };
    const fixture = nativeFixture(nativeDetail(["report"], { case_status: caseStatus, result: nativeReport({ case_status: caseStatus, report: payload }) }));
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + conversationPath + "?include=report"); assert.equal(response.status, 200);
      assert.deepEqual((await response.json()).data.result.report, payload);
    });
  });
}
test("Markdown and legacy Generic preserve their result fields", async () => {
  for (const fields of [{ format: "markdown", report: null, markdown: "# 诊断结果\r\n采用 \"rpc_timeout\" 方法 🧭\r\n" },
    { format: "generic-v1", report: { conclusion: "当前证据不足", root_cause_analysis: "缺少日志" }, markdown: null, artifact: null }]) {
    const value = nativeDetail(["report"], { result: nativeReport(fields) }), fixture = nativeFixture(value);
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + conversationPath + "?include=report"); assert.equal(response.status, 200);
      assert.deepEqual((await response.json()).data, value);
    }); assert.equal(fixture.calls.length, 1);
  }
});
for (const overrides of [{ schema_version: 1 }, { conversation_id: caseId }, { report_state: "UNKNOWN" },
  { included: ["report", "history"] }, { history: [] }, { attachments: [] }, { artifacts: [] }, { result: null },
  { source_job_id: artifactId }, { result: nativeReport({ case_id: artifactId }) }, { result: nativeReport({ format: "html" }) },
  { result: nativeReport({ report: [] }) }, { result: nativeReport({ report_state: "PENDING" }) },
  { result: nativeReport({ format: "markdown", report: null, markdown: {} }) }]) {
  test(`conversation rejects inconsistent included result ${JSON.stringify(overrides)}`, async () => {
    const fixture = nativeFixture(nativeDetail(["report"], overrides));
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + conversationPath + "?include=report"); assert.equal(response.status, 502);
      assert.equal((await response.json()).data, null);
    }); assert.equal(fixture.calls.length, 1);
  });
}
test("synthetic response exercises the combined transport budget", async () => {
  const text = "x".repeat(65_000);
  const largeReport = { ...report, limitations: Array.from({ length: 256 }, (_, index) => `${index}:` + "\u0001".repeat(65_000)) };
  // This deliberately oversized synthetic report tests only the BFF transport
  // allowance; it is not evidence that the native publication schema accepts it.
  const value = nativeDetail(allIncludes, { history: Array.from({ length: 32 }, (_, index) => ({ message_id: String(index), text })),
    result: nativeReport({ report: largeReport }) });
  const body = JSON.stringify({ ok: true, data: value, error: null });
  assert.ok(Buffer.byteLength(body) > 6 * 16 * 1024 * 1024 + 64 * 1024);
  let calls = 0;
  await withServer({ access, fetchImpl: async () => { calls++; return new Response(body); } }, async (origin) => {
    const response = await fetch(origin + conversationPath); assert.equal(response.status, 200);
    const received = (await response.json()).data;
    assert.deepEqual(received.result.report, largeReport); assert.deepEqual(received.history, value.history);
  }); assert.equal(calls, 1);
});
test("valid large report fields and message history coexist above the old JSON response limit", async () => {
  const published = JSON.parse(fs.readFileSync(new URL("../../tests/fixtures/contracts/positive/user-result.json", import.meta.url), "utf8"));
  const largeReport = { ...published, limitations: Array.from({ length: 240 }, (_, index) => `${index}:` + "x".repeat(64_000)) };
  assert.ok(Buffer.byteLength(JSON.stringify(largeReport)) < 16 * 1024 * 1024);
  const messages = Array.from({ length: 32 }, (_, index) => ({
    message_id: `50000000-0000-0000-0000-${String(index + 1).padStart(12, "0")}`,
    request_id: `message-${index}`, text: "y".repeat(65_000), attachment_ids: [], status: "APPLIED",
    created_at: "2026-09-17T00:00:00.000Z", notice: null,
  }));
  const history = messages.map((message) => ({ id: message.message_id, run_id: runId, type: "user.message",
    created_at: message.created_at, message: { ...message, run_id: runId }, questions: null, result: null }));
  const value = nativeDetail(allIncludes, { history, result: nativeReport({ report: largeReport }) });
  assert.ok(Buffer.byteLength(JSON.stringify(value)) > 16 * 1024 * 1024);
  const fixture = nativeFixture(value, "");
  await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
    const response = await fetch(origin + conversationPath); assert.equal(response.status, 200);
    const received = (await response.json()).data;
    assert.deepEqual(received.result.report, largeReport); assert.deepEqual(received.history, history);
  }); assert.equal(fixture.calls.length, 1);
});
for (const [include, limit] of [["none", 16 * 1024 * 1024], ["report", 6 * 16 * 1024 * 1024 + 64 * 1024]]) {
  test(`include=${include} enforces its actual response budget without Content-Length`, async () => {
    const chunk = new Uint8Array(1024 * 1024).fill(32); let sent = 0;
    await withServer({ access, fetchImpl: async () => new Response(new ReadableStream({ pull(controller) {
      if (sent > limit) controller.close(); else { sent += chunk.length; controller.enqueue(chunk); }
    } })) }, async (origin) => {
      const response = await fetch(origin + conversationPath + `?include=${include}`); assert.equal(response.status, 502);
      assert.equal((await response.json()).data, null);
    }); assert.ok(sent <= limit + 2 * chunk.length);
  });
}
test("default access denies before any upstream request", async () => {
  await withServer({ fetchImpl: async () => assert.fail("未授权请求不能访问上游。") }, async (origin) => {
    assert.equal((await fetch(origin + conversationPath)).status, 401);
  });
});
test("all operations carry server-derived identity and preserve native ownership denial", async () => {
  let calls = 0;
  await withServer({ access, fetchImpl: async (_url, init) => {
    calls++; assert.equal(new Headers(init.headers).get("X-Agent-Owner-Key"), ownerKey);
    return new Response(JSON.stringify({ ok: false, data: null, error: {
      code: "AGENT_CONVERSATION_NOT_FOUND", message: "private", details: [], retryable: false } }), { status: 404 });
  } }, async (origin) => {
    for (const path of [conversationPath, conversationPath + "?include=none", conversationPath + "/events", reportDownloadPath]) {
      assert.equal((await fetch(origin + path, { headers: { "X-Agent-Owner-Key": "b".repeat(64) } })).status, 404);
    }
    for (const [path, body] of [[conversationPath + "/messages", {}], ["/api/agent/attachments", { conversation_id: conversation }]]) {
      assert.equal((await fetch(origin + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).status, 404);
    }
  }); assert.equal(calls, 6);
});
test("removed report, status, artifacts and nested preparation routes return 404", async () => {
  await withServer({ access, fetchImpl: async () => assert.fail("旧接口不能访问上游。") }, async (origin) => {
    for (const suffix of ["/status", "/report", "/artifacts", `/artifacts/${artifactId}/content`, "/files"]) {
      assert.equal((await fetch(origin + conversationPath + suffix)).status, 404, suffix);
    }
    assert.equal((await fetch(origin + conversationPath + "/attachments", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })).status, 404);
  });
});
test("raw upload carries trusted owner without a duplicate attachment directory", async () => {
  await withServer({ access, fetchImpl: async (_url, init) => {
    assert.equal(new Headers(init.headers).get("X-Agent-Owner-Key"), ownerKey);
    for await (const _chunk of init.body) {}
    return new Response(JSON.stringify({ ok: false, data: null, error: { code: "AGENT_ATTACHMENT_NOT_FOUND", message: "private", details: [], retryable: false } }), { status: 404 });
  } }, async (origin) => {
    assert.equal((await fetch(origin + "/api/agent/attachments/" + artifactId + "/content", { method: "PUT", body: "private",
      headers: { "Content-Type": "application/zip", "Idempotency-Key": artifactId, "X-Content-SHA256": "a".repeat(64) } })).status, 404);
  });
});
test("creation keeps the stable browser request key with native owner isolation and no duplicate directory", async () => {
  const ids = [];
  await withServer({ access: { ...access, rememberConversation: async () => assert.fail("no duplicate directory") }, fetchImpl: async (_url, init) => {
    assert.equal(new Headers(init.headers).get("X-Agent-Owner-Key"), ownerKey);
    ids.push(JSON.parse(init.body).request_id); return envelope({ conversation_id: conversation, request_id: ids.at(-1) });
  } }, async (origin) => {
    for (let index = 0; index < 2; index++) assert.equal((await fetch(origin + "/api/agent/conversations", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ request_id: "browser-request-1" }) })).status, 200);
  });
  assert.equal(ids[0], ids[1]); assert.equal(ids[0], createHash("sha256").update(JSON.stringify(["alice", "browser-request-1"])).digest("hex"));
});

test("pre-upgrade browser create replay reuses its original native key and cannot revive a deleted conversation", async () => {
  const legacyKey = createHash("sha256").update(JSON.stringify(["alice", "old-browser-request"])).digest("hex");
  let deleted = false, calls = 0;
  await withServer({ access, fetchImpl: async (_url, init) => {
    calls++;
    assert.equal(JSON.parse(init.body).request_id, legacyKey);
    assert.equal(new Headers(init.headers).get("X-Agent-Owner-Key"), ownerKey);
    if (deleted) return new Response(JSON.stringify({ ok: false, data: null,
      error: { code: "AGENT_CONVERSATION_NOT_FOUND", message: "deleted", details: [], retryable: false } }), { status: 404 });
    return envelope({ conversation_id: conversation, request_id: legacyKey });
  } }, async (origin) => {
    const replay = () => fetch(origin + "/api/agent/conversations", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ request_id: "old-browser-request" }) });
    assert.equal((await (await replay()).json()).data.conversation_id, conversation);
    deleted = true;
    const removed = await replay(); assert.equal(removed.status, 404); assert.equal((await removed.json()).data, null);
  });
  assert.equal(calls, 2);
});
test("directory, rename, stop, delete and historical reads use one authoritative request each", async () => {
  const calls = [];
  await withServer({ access, ownerNamespace: "company-site", fetchImpl: async (url, init) => {
    assert.equal(new Headers(init.headers).get("X-Agent-Owner-Key"), createHash("sha256").update(JSON.stringify(["company-site", "alice"])).digest("hex"));
    calls.push({ path: new URL(url).pathname + new URL(url).search, method: init.method ?? "GET", body: init.body ? JSON.parse(init.body) : null });
    if (calls.length === 1) return envelope({ items: [], next_cursor: "next-cursor" });
    if (calls.length === 5) return envelope(nativeDetail(["history", "report"], { history_next_cursor: "older" }));
    return envelope({ conversation_id: conversation, run_id: runId, status: "ACCEPTED" });
  } }, async (origin) => {
    assert.equal((await fetch(origin + "/api/agent/conversations?limit=20&cursor=opaque%2Bcursor")).status, 200);
    for (const [method, suffix, body] of [["PATCH", "", { title: "更新标题" }], ["POST", "/stop", { request_id: "stop-one", run_id: runId }], ["DELETE", "", null]]) {
      assert.equal((await fetch(origin + conversationPath + suffix, { method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined })).status, 200);
    }
    const response = await fetch(origin + conversationPath + `?include=report,history&run_id=${runId}&history_before=opaque%2Bbefore&history_limit=50`);
    assert.equal(response.status, 200); assert.equal((await response.json()).data.history_next_cursor, "older");
  });
  assert.deepEqual(calls.map(({ method }) => method), ["GET", "PATCH", "POST", "DELETE", "GET"]);
  assert.deepEqual(calls[2].body, { request_id: "stop-one", run_id: runId });
  assert.equal(calls[3].body, null);
  assert.equal(calls[4].path, `/api/v1/agent/conversations/${conversation}?include=history,report&run_id=${runId}&history_before=opaque%2Bbefore&history_limit=50`);
});
test("historical file is pinned to selected run, never the latest run or untrusted URL", async () => {
  const oldRun = "60000000-0000-0000-0000-000000000001", calls = [];
  const bytes = Buffer.from(JSON.stringify(report));
  await withServer({ access, fetchImpl: async (url, init) => {
    const parsed = new URL(url); calls.push(parsed.pathname + parsed.search);
    assert.equal(new Headers(init.headers).get("X-Agent-Owner-Key"), ownerKey);
    if (calls.length === 1) return envelope(nativeDetail(["artifacts"], { selected_run_id: oldRun,
      artifacts: [{ ...publicArtifact(), download_url: "http://evil.invalid/private" }] }));
    assert.equal(parsed.searchParams.get("run_id"), oldRun);
    return new Response(bytes, { headers: { "Content-Type": "application/json" } });
  } }, async (origin) => {
    assert.equal((await fetch(origin + reportDownloadPath + `?run_id=${oldRun}`)).status, 200);
  });
  assert.deepEqual(calls, [`/api/v1/agent/conversations/${conversation}?include=artifacts&run_id=${oldRun}`,
    `/api/v1/agent/conversations/${conversation}/files/${artifactId}/content?run_id=${oldRun}`]);
});
test("invalid paging and spoofed owner query never reach upstream", async () => {
  await withServer({ access, fetchImpl: async () => assert.fail("bad query") }, async (origin) => {
    for (const query of ["limit=0", "limit=101", "limit=1&limit=2", "owner_key=spoof", "cursor="])
      assert.equal((await fetch(origin + "/api/agent/conversations?" + query)).status, 400);
    for (const query of ["run_id=bad", "history_limit=0", "history_limit=101", "history_before=", "owner_key=spoof"])
      assert.equal((await fetch(origin + conversationPath + "?" + query)).status, 400);
  });
});
test("history terminal cards preserve controlled failure and stop conflicts retain their stable code", async () => {
  const failure = { code: "OUTCOME_INVALID", message: "SECRET /srv/path", details: [
    { field: "phase", actual: "OUTCOME_VALIDATE" }, { field: "raw_output", actual: "SECRET" }], retryable: true };
  await withServer({ access, fetchImpl: async (_url, init) => {
    if (init.method === "POST") return new Response(JSON.stringify({ ok: false, data: null,
      error: { code: "AGENT_RUN_CHANGED", message: "internal", details: [], retryable: false } }), { status: 409 });
    return envelope(nativeDetail(["history"], { history: [{ id: "closed", run_id: runId, type: "diagnosis.result",
      created_at: "2026-09-17T00:00:00.000Z", message: null, questions: null,
      result: { status: "FAILED", report_state: "UNAVAILABLE", case_id: caseId, case_status: "FAILED", source_job_id: null, failure } }] }));
  } }, async (origin) => {
    const response = await fetch(origin + conversationPath + "?include=history"), text = await response.text();
    assert.equal(response.status, 200); assert.ok(!text.includes("SECRET") && !text.includes("/srv"));
    const safe = JSON.parse(text).data.history[0].result.failure;
    assert.equal(safe.code, "OUTCOME_INVALID"); assert.equal(safe.retryable, false); assert.equal(safe.details.length, 1);
    const conflict = await fetch(origin + conversationPath + "/stop", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ request_id: "old-stop", run_id: runId }) });
    assert.equal(conflict.status, 409); assert.equal((await conflict.json()).error.code, "AGENT_RUN_CHANGED");
  });
});
test("SSE forwards data-only frames, Last-Event-ID and comments", async () => {
  const text = ': connected\n\n: heartbeat\n\ndata: {"sequence":2,"type":"agent.progress","data":{"message":"正在核对证据\\n请稍候"}}\n\n';
  await withServer({ access, fetchImpl: async (_url, init) => {
    assert.equal(init.headers.get("Last-Event-ID"), "1"); return new Response(text, { headers: { "Content-Type": "text/event-stream" } });
  } }, async (origin) => {
    const response = await fetch(origin + conversationPath + "/events", { headers: { "Last-Event-ID": "1" } });
    assert.equal(response.headers.get("Content-Type"), "text/event-stream; charset=utf-8");
    assert.equal(response.headers.get("X-Accel-Buffering"), "no"); assert.equal(await response.text(), text);
  });
});
test("controlled failures survive detail refresh, nested result and pre-stream errors", async () => {
  const failure = { code: "INTAKE_OUTPUT_INVALID", message: "SECRET /srv/private", retryable: true,
    details: [{ field: "phase", actual: "INTAKE" }, { field: "diagnostic_id", actual: jobId },
      { field: "location", actual: "input_values[2]" }, { field: "raw_output", actual: "SECRET" }] };
  await withServer({ access, fetchImpl: async (url) => {
    if (new URL(url).pathname.endsWith("/events")) return new Response(JSON.stringify({ ok: false, data: null,
      error: { ...failure, code: "DISPATCH_REJECTED" } }), { status: 503 });
    return envelope(nativeDetail(["report"], { result: nativeReport({ failure }), failure }));
  } }, async (origin) => {
    for (let index = 0; index < 2; index++) {
      const response = await fetch(origin + conversationPath + "?include=report"), text = await response.text();
      assert.ok(!text.includes("SECRET") && !text.includes("/srv")); const value = JSON.parse(text).data;
      assert.deepEqual(value.failure, { code: failure.code, message: "补充信息整理失败，请核对输入后新建任务。", retryable: false, details: failure.details.slice(0, 3) });
      assert.deepEqual(value.result.failure, value.failure);
    }
    const response = await fetch(origin + conversationPath + "/events"); assert.equal(response.status, 503);
    assert.equal((await response.json()).error.retryable, true);
  });
});
test("light detail retains archive uncertainty without fetching a report", async () => {
  const fixture = nativeFixture(nativeDetail([], { source_job_id: null, case_revision: null,
    failure: { code: "DISPATCH_REJECTED", message: "SECRET", retryable: true,
      details: [{ field: "phase", actual: "ARCHIVE_STATUS_COMMIT" }, { field: "persistence", actual: "UNKNOWN" }] } }), "?include=none");
  await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
    const response = await fetch(origin + conversationPath + "?include=none"), detail = (await response.json()).data;
    assert.equal(response.status, 200); assert.equal(detail.failure.message, "报告已生成，但归档状态暂时无法确认。");
    assert.equal(detail.failure.retryable, false); assert.equal(detail.result, null);
  }); assert.equal(fixture.calls.length, 1);
});
test("paused accepted requests expose safe HTTP errors on detail and SSE", async () => {
  const details = [{ field: "phase", actual: "DISPATCH_PAUSED" }, { field: "persistence", actual: "UNKNOWN" }];
  await withServer({ access, fetchImpl: async () => new Response(JSON.stringify({ ok: false, data: null,
    error: { code: "DISPATCH_REJECTED", message: "private upstream", details, retryable: true } }), { status: 503 }) }, async (origin) => {
    for (const suffix of ["?include=none", "/events"]) {
      const response = await fetch(origin + conversationPath + suffix); assert.equal(response.status, 503);
      assert.deepEqual((await response.json()).error, { code: "DISPATCH_REJECTED", message: "服务异常，已接收的任务暂时无法继续。", details, retryable: true });
    }
  });
});
test("file download verifies bytes using one artifact-only conversation snapshot", async () => {
  const fixture = artifactFixture();
  await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
    const response = await fetch(origin + reportDownloadPath); assert.equal(response.status, 200); assert.deepEqual(await response.json(), report);
  });
  assert.deepEqual(fixture.calls, [`${base}/api/v1/agent/conversations/${conversation}?include=artifacts`, `${base}/api/v1/agent/conversations/${conversation}/files/${artifactId}/content?run_id=${runId}`]);
});
test("configured prefix and verified IDs determine downloads, never published URLs", async () => {
  const fixture = artifactFixture({ badUrl: true }), requested = [];
  await withServer({ access, upstream: base + "/internal/xiaodao", fetchImpl: async (url, init) => {
    const parsed = new URL(url); requested.push(parsed.href); assert.ok(parsed.pathname.startsWith("/internal/xiaodao/api/v1/"));
    parsed.pathname = parsed.pathname.slice("/internal/xiaodao".length); return fixture.fetchImpl(parsed, init);
  } }, async (origin) => { assert.equal((await fetch(origin + reportDownloadPath)).status, 200); });
  assert.deepEqual(requested, [`${base}/internal/xiaodao/api/v1/agent/conversations/${conversation}?include=artifacts`, `${base}/internal/xiaodao/api/v1/agent/conversations/${conversation}/files/${artifactId}/content?run_id=${runId}`]);
});
for (const options of [{}, { badHash: true }, { receivedPayload: Buffer.from("short") }]) {
  test(`missing integrity headers still verifies actual bytes ${JSON.stringify(options)}`, async () => {
    const fixture = artifactFixture({ ...options, headerOverrides: { "Content-Length": null, "X-Content-SHA256": null } });
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + reportDownloadPath); assert.equal(response.status, Object.keys(options).length ? 502 : 200);
      if (!response.ok) assert.equal((await response.json()).data, null);
    });
  });
}
for (const options of [{ headerOverrides: { "Content-Length": "1" } }, { headerOverrides: { "X-Content-SHA256": "a".repeat(64) } },
  { headerOverrides: { "Content-Type": "text/html" } }, { headerOverrides: { "Content-Encoding": "gzip" } },
  { downloadStatus: 307, headerOverrides: { Location: "http://other.internal/private" } }]) {
  test(`invalid download headers or redirect rejects ${JSON.stringify(options)}`, async () => {
    const fixture = artifactFixture(options);
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + reportDownloadPath); assert.equal(response.status, 502); assert.equal((await response.json()).data, null);
    }); assert.ok(fixture.calls.every((url) => new URL(url).origin === base));
  });
}
test("artifact inclusion rewrites links and ZIP requires explicit acknowledgement", async () => {
  const fixture = artifactFixture({ kind: "USER_RESULT_ARCHIVE", payload: Buffer.from("zip bytes") });
  await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
    const listing = await (await fetch(origin + conversationPath + "?include=artifacts")).json(), item = listing.data.artifacts[0];
    assert.match(item.download_notice, /原始目标日志/); assert.equal(item.download_url, reportDownloadPath + `?run_id=${runId}`);
    for (const suffix of ["", "&download=archive"]) assert.equal((await fetch(origin + item.download_url + suffix)).status, 409);
    assert.equal(fixture.calls.filter((url) => url.includes("/content?")).length, 0);
    const response = await fetch(origin + item.download_url + "&download=archive&acknowledge_raw_logs=true");
    assert.equal(response.status, 200); assert.equal(await response.text(), "zip bytes");
  });
});
for (const count of [1, 200]) {
  test(`artifact inclusion reads one snapshot for ${count} entries`, async () => {
    const artifacts = Array.from({ length: count }, (_, index) => ({ ...publicArtifact("USER_RESULT_ARCHIVE"), artifact_id: `30000000-0000-0000-0000-${String(index + 1).padStart(12, "0")}` }));
    const fixture = artifactFixture({ caseOverrides: { artifacts } });
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + conversationPath + "?include=artifacts"); assert.equal(response.status, 200);
      assert.equal((await response.json()).data.artifacts.length, count);
    }); assert.equal(fixture.calls.length, 1);
  });
}
test("small report downloads perform no temporary-file IO", async (context) => {
  const directory = context.mock.method(fsPromises, "mkdtemp", () => { throw new Error("unexpected temporary directory"); });
  const writing = context.mock.method(fs, "createWriteStream", () => { throw new Error("unexpected temporary file"); }); syncBuiltinESMExports();
  try {
    const fixture = artifactFixture();
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + reportDownloadPath); assert.equal(response.status, 200); assert.equal(await response.text(), JSON.stringify(report));
    }); assert.equal(directory.mock.callCount(), 0); assert.equal(writing.mock.callCount(), 0); assert.equal(fixture.calls.length, 2);
  } finally { context.mock.restoreAll(); syncBuiltinESMExports(); }
});
test("chunked Generic bytes retain Unicode and CRLF", async () => {
  const markdown = "# 诊断结果\r\n采用 \"rpc_timeout\" 方法 🧭\r\n", payload = Buffer.from(markdown); let offset = 0;
  const receivedPayload = new ReadableStream({ pull(controller) {
    if (offset === payload.length) controller.close(); else controller.enqueue(payload.subarray(offset, ++offset));
  } });
  const fixture = artifactFixture({ kind: "GENERIC_REPORT", payload, receivedPayload, headerOverrides: { "Content-Length": null, "X-Content-SHA256": null } });
  await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
    const response = await fetch(origin + reportDownloadPath); assert.equal(response.status, 200); assert.equal(await response.text(), markdown);
  }); assert.equal(fixture.calls.length, 2);
});
for (const [name, options] of [
  ["invalid Case", { caseOverrides: { case_id: "../private" } }], ["wrong source Job", { badSource: true }],
  ["missing source Job", { caseOverrides: { source_job_id: null } }], ["unavailable download", { summaryOverrides: { downloadable: false } }],
  ["non-boolean permission", { summaryOverrides: { downloadable: "true" } }], ["directory", { summaryOverrides: { resource_kind: "DIRECTORY" } }],
  ["invalid artifact ID", { artifactOverrides: { artifact_id: "../private" } }], ["invalid hash", { artifactOverrides: { sha256: "not-a-sha256" } }],
  ["wrong type", { artifactOverrides: { content_type: "text/html" } }], ["wrong name", { artifactOverrides: { name: "private.json" } }],
  ["negative size", { artifactOverrides: { size: -1 } }], ["non-integer size", { artifactOverrides: { size: 1.5 } }],
  ["oversized size", { artifactOverrides: { size: 5_368_709_120 + 1 } }],
]) {
  test(`artifact-only snapshot rejects ${name} before downloading`, async () => {
    const fixture = artifactFixture(options);
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + reportDownloadPath); assert.equal(response.status, 502); assert.equal((await response.json()).data, null);
    }); assert.equal(fixture.calls.length, 1);
  });
}
test("artifact-only snapshot rejects duplicate identities", async () => {
  const artifact = publicArtifact(), fixture = artifactFixture({ caseOverrides: { artifacts: [artifact, artifact] } });
  await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
    const response = await fetch(origin + reportDownloadPath); assert.equal(response.status, 502); assert.equal((await response.json()).data, null);
  }); assert.equal(fixture.calls.length, 1);
});
for (const difference of [-1, 1]) {
  test(`report rejects ${difference < 0 ? "truncated" : "longer"} bytes without optional headers`, async () => {
    const payload = Buffer.from(JSON.stringify(report));
    const receivedPayload = difference < 0 ? payload.subarray(0, -1) : Buffer.concat([payload, Buffer.from(" ")]);
    const fixture = artifactFixture({ payload, receivedPayload, headerOverrides: { "Content-Length": null, "X-Content-SHA256": null } });
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + reportDownloadPath); assert.equal(response.status, 502); assert.equal((await response.json()).data, null);
    }); assert.equal(fixture.calls.length, 2);
  });
}
for (const options of [{ headerOverrides: { "Content-Length": null, "X-Content-SHA256": null }, expected: 200 },
  { headerOverrides: { "Content-Length": null, "X-Content-SHA256": null }, badHash: true, expected: 502 },
  { headerOverrides: { "Content-Type": "text/html" }, expected: 502 }, { headerOverrides: { "Content-Encoding": "gzip" }, expected: 502 },
  { headerOverrides: { "X-Content-SHA256": "0".repeat(64) }, expected: 502 }]) {
  test(`ZIP retains spool verification ${JSON.stringify(options)}`, async () => {
    const fixture = artifactFixture({ ...options, kind: "USER_RESULT_ARCHIVE", payload: Buffer.from("zip bytes") });
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + reportDownloadPath + "?download=archive&acknowledge_raw_logs=true"); assert.equal(response.status, options.expected);
      if (response.ok) assert.equal(await response.text(), "zip bytes"); else assert.equal((await response.json()).data, null);
    }); assert.equal(fixture.calls.length, 2);
  });
}

async function spoolFixture(root, pid, label, processIdentity = null) {
  const directory = join(root, `xiaodao-website-p${pid}-${label}`);
  await fsPromises.mkdir(directory);
  await fsPromises.writeFile(join(directory, "payload"), "temporary archive");
  await fsPromises.writeFile(join(directory, "owner.json"), JSON.stringify({
    schema_version: 1, pid, process_identity: processIdentity,
  }));
  return directory;
}

async function removeTestRoot(root) {
  assert.equal(dirname(resolve(root)), resolve(tmpdir()));
  assert.match(basename(root), /^xiaodao-spool-tests-/);
  await fsPromises.rm(root, { recursive: true, force: true });
}

test("expired download spools reclaim dead owners and failed same-process cleanup without touching live owners", async () => {
  const root = await fsPromises.mkdtemp(join(tmpdir(), "xiaodao-spool-tests-"));
  try {
    const dead = await spoolFixture(root, 2_147_483_647, "dead");
    const failedCleanup = await spoolFixture(root, process.pid, "inactive");
    const live = await spoolFixture(root, process.ppid, "live");
    const legacy = join(root, "xiaodao-website-legacy");
    await fsPromises.mkdir(legacy);
    await fsPromises.writeFile(join(legacy, "payload"), "unknown owner");
    await sweepDownloadSpools({ root });
    assert.ok(fs.existsSync(dead), "期限内的目录需要保留。");
    await sweepDownloadSpools({ root, now: Date.now() + DOWNLOAD_TEMP_TTL_MS * 2 });
    assert.equal(fs.existsSync(dead), false);
    assert.equal(fs.existsSync(failedCleanup), false);
    assert.ok(fs.existsSync(live), "其他存活进程的目录不能删除。");
    assert.ok(fs.existsSync(legacy), "旧版未知归属目录不能自动删除。");
  } finally { await removeTestRoot(root); }
});

test("Linux start identity reclaims a stale spool even when its PID has been reused", { skip: process.platform !== "linux" }, async () => {
  const root = await fsPromises.mkdtemp(join(tmpdir(), "xiaodao-spool-tests-"));
  try {
    const reused = await spoolFixture(root, process.ppid, "reused", "previous-boot:0");
    await sweepDownloadSpools({ root, now: Date.now() + DOWNLOAD_TEMP_TTL_MS * 2 });
    assert.equal(fs.existsSync(reused), false);
  } finally { await removeTestRoot(root); }
});

test("active download survives the sweeper and its spool disappears after delivery", async (context) => {
  const originalMkdtemp = fsPromises.mkdtemp, originalRmdir = fsPromises.rmdir;
  let directory, release;
  const created = new Promise((resolveCreated) => {
    context.mock.method(fsPromises, "mkdtemp", async (...args) => {
      directory = await originalMkdtemp(...args); resolveCreated(); return directory;
    });
  });
  const removed = new Promise((resolveRemoved) => {
    context.mock.method(fsPromises, "rmdir", async (...args) => {
      const result = await originalRmdir(...args);
      if (args[0] === directory) resolveRemoved();
      return result;
    });
  });
  syncBuiltinESMExports();
  const payload = Buffer.from("zip bytes");
  const fixture = artifactFixture({ kind: "USER_RESULT_ARCHIVE", payload,
    receivedPayload: new ReadableStream({ start(controller) {
      release = () => { controller.enqueue(payload); controller.close(); };
    } }) });
  try {
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const pending = fetch(origin + reportDownloadPath + "?download=archive&acknowledge_raw_logs=true");
      await created;
      await sweepDownloadSpools({ now: Date.now() + DOWNLOAD_TEMP_TTL_MS * 2 });
      assert.ok(fs.existsSync(directory));
      release();
      const response = await pending;
      assert.equal(response.status, 200);
      assert.equal(await response.text(), "zip bytes");
      await removed;
      assert.equal(fs.existsSync(directory), false);
    });
  } finally { context.mock.restoreAll(); syncBuiltinESMExports(); }
});

test("download total timeout cancels a stalled upstream and removes its spool", async (context) => {
  const originalSetTimeout = timers.setTimeout, originalWriteFile = fsPromises.writeFile;
  let expire, directory, cancelled = false;
  context.mock.method(timers, "setTimeout", (callback, delay, ...args) => {
    if (delay === DOWNLOAD_TOTAL_TIMEOUT_MS) {
      expire = callback;
      return originalSetTimeout(() => {}, delay);
    }
    return originalSetTimeout(callback, delay, ...args);
  });
  const prepared = new Promise((resolvePrepared) => {
    context.mock.method(fsPromises, "writeFile", async (path, ...args) => {
      const result = await originalWriteFile(path, ...args);
      if (basename(String(path)) === "owner.json") { directory = dirname(String(path)); resolvePrepared(); }
      return result;
    });
  });
  syncBuiltinESMExports();
  const fixture = artifactFixture({ kind: "USER_RESULT_ARCHIVE", payload: Buffer.from("zip bytes"),
    receivedPayload: new ReadableStream({ cancel() { cancelled = true; } }) });
  try {
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const pending = fetch(origin + reportDownloadPath + "?download=archive&acknowledge_raw_logs=true");
      await prepared;
      expire();
      const response = await pending;
      assert.equal(response.status, 504);
      assert.match((await response.json()).error.message, /下载超时/);
      assert.equal(cancelled, true);
      assert.equal(fs.existsSync(directory), false);
    });
  } finally { context.mock.restoreAll(); syncBuiltinESMExports(); }
});

test("startup cleanup failure is visible and the periodic sweep retries", async (context) => {
  const originalReaddir = fsPromises.readdir, originalSetInterval = timers.setInterval;
  let attempts = 0, retrySweep, reported;
  const report = new Promise((resolveReported) => { reported = resolveReported; });
  context.mock.method(fsPromises, "readdir", async (...args) => {
    if (++attempts === 1) throw Object.assign(new Error("fixture cleanup denied"), { code: "EACCES" });
    return originalReaddir(...args);
  });
  context.mock.method(timers, "setInterval", (callback, delay, ...args) => {
    retrySweep = callback; return originalSetInterval(callback, delay, ...args);
  });
  context.mock.method(console, "error", (message, code) => {
    assert.match(message, /下一轮重试/); assert.equal(code, "EACCES"); reported();
  });
  syncBuiltinESMExports();
  try {
    await withServer({ access }, async () => {
      await report;
      await retrySweep();
      assert.equal(attempts, 2);
    });
  } finally { context.mock.restoreAll(); syncBuiltinESMExports(); }
});

test("expired SSE cursor preserves its public code and safe retained sequence", async () => {
  await withServer({ access, fetchImpl: async (_url, init) => {
    assert.equal(init.headers.get("Last-Event-ID"), "1");
    return new Response(JSON.stringify({ ok: false, data: null, error: {
      code: "AGENT_EVENT_CURSOR_EXPIRED", message: "private /srv/path", retryable: false,
      details: [{ field: "retained_after_sequence", actual: 2, internal: "private" },
        { field: "retained_after_sequence", actual: -1 },
        { field: "retained_after_sequence", actual: "private" },
        { field: "retained_after_sequence", actual: Number.MAX_SAFE_INTEGER + 1 },
        { field: "path", actual: "/srv/private" }],
    } }), { status: 409, headers: { "Content-Type": "application/json" } });
  } }, async (origin) => {
    const response = await fetch(origin + conversationPath + "/events", { headers: { "Last-Event-ID": "1" } });
    assert.equal(response.status, 409);
    assert.deepEqual((await response.json()).error, {
      code: "AGENT_EVENT_CURSOR_EXPIRED",
      message: "历史事件已过保留期，请先刷新会话状态，再从 last_event_id 重新订阅。",
      details: [{ field: "retained_after_sequence", actual: 2 }], retryable: false,
    });
  });
});
