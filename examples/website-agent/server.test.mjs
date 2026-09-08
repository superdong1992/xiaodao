import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { once } from "node:events";
import test from "node:test";
import { createAgentBackend, reportSections } from "./server.ts";

const conversation = "10000000-0000-0000-0000-000000000001";
const caseId = "20000000-0000-0000-0000-000000000001";
const artifactId = "30000000-0000-0000-0000-000000000001";
const jobId = "40000000-0000-0000-0000-000000000001";
const base = `http://xiaodao.internal`;
const conversationPath = `/api/agent/conversations/${conversation}`;
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
  time_relevance: { status: "UNKNOWN" }, evidence_gaps: [], limitations: [],
  recommendations: ["检查连接释放"], safety_notes: [],
};

function envelope(data) {
  return new Response(JSON.stringify({ ok: true, data, error: null }), {
    headers: { "Content-Type": "application/json" },
  });
}

async function withServer(options, exercise) {
  const server = createAgentBackend({ upstream: base, ...options });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  try { await exercise(`http://127.0.0.1:${server.address().port}`); }
  finally { server.close(); await once(server, "close"); }
}

function artifactFixture({ kind = "USER_RESULT", payload = Buffer.from(JSON.stringify(report)), badSource = false, badHash = false, badUrl = false } = {}) {
  const calls = [];
  const artifact = {
    artifact_id: artifactId, kind, name: kind === "USER_RESULT" ? "diagnosis-result.json" : "result.zip",
    content_type: kind === "USER_RESULT" ? "application/json" : "application/zip",
    size: payload.length, sha256: createHash("sha256").update(payload).digest("hex"),
    download_url: badUrl ? "http://other.internal/secret" : `${base}/api/v1/artifacts/${artifactId}/content?case_id=${caseId}`,
  };
  const fetchImpl = async (url, init) => {
    calls.push(String(url));
    const path = new URL(url).pathname;
    if (path === `/api/v1/agent/conversations/${conversation}`) return envelope({ conversation_id: conversation, case_id: caseId });
    if (path === `/api/v1/cases/${caseId}`) return envelope({ case_view: {
      case_id: caseId, status: "RESOLVED", final_result: { proposed_by_job_id: jobId },
      artifacts: [{ ...artifact, created_by_job_id: badSource ? artifactId : jobId, downloadable: true }],
    }});
    if (path === `/api/v1/cases/${caseId}/artifacts`) return envelope({ artifacts: [artifact] });
    if (path === `/api/v1/artifacts/${artifactId}/content`) {
      assert.equal(init.redirect, "manual");
      return new Response(badHash ? Buffer.alloc(payload.length, 0) : payload, { headers: {
        "Content-Type": artifact.content_type, "Content-Length": String(payload.length), "X-Content-SHA256": artifact.sha256,
      }});
    }
    throw new Error("unexpected upstream call");
  };
  return { calls, artifact, fetchImpl };
}

test("default access denies before any upstream request", async () => {
  let calls = 0;
  await withServer({ fetchImpl: async () => { calls++; throw new Error(); } }, async (origin) => {
    const response = await fetch(origin + conversationPath);
    assert.equal(response.status, 401);
  });
  assert.equal(calls, 0);
});

test("every conversation read, SSE, mutation and download enforces ownership", async () => {
  let calls = 0;
  await withServer({ access: { ...access, ownsConversation: async () => false },
    fetchImpl: async () => { calls++; throw new Error(); } }, async (origin) => {
    for (const path of ["", "/events", "/artifacts", "/report", `/artifacts/${artifactId}/content`]) {
      const response = await fetch(origin + conversationPath + path);
      assert.equal(response.status, 403, path);
    }
    for (const path of ["messages", "attachments"]) {
      const response = await fetch(`${origin}${conversationPath}/${path}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
      assert.equal(response.status, 403, path);
    }
  });
  assert.equal(calls, 0);
});

test("raw attachment upload also requires website ownership", async () => {
  let calls = 0;
  await withServer({ access: { ...access, ownsAttachment: async () => false },
    fetchImpl: async () => { calls++; throw new Error(); } }, async (origin) => {
    assert.equal((await fetch(`${origin}/api/agent/attachments/${artifactId}/content`, { method: "PUT", body: "private" })).status, 403);
  });
  assert.equal(calls, 0);
});

test("creation scopes idempotency to authenticated user and persists ownership", async () => {
  const stored = [];
  const upstreamIds = [];
  const callbacks = { ...access, rememberConversation: async (user, id) => stored.push([user.id, id]) };
  await withServer({ access: callbacks, fetchImpl: async (_url, init) => {
    upstreamIds.push(JSON.parse(init.body).request_id);
    return envelope({ conversation_id: conversation, request_id: upstreamIds.at(-1) });
  }}, async (origin) => {
    for (let i = 0; i < 2; i++) {
      const response = await fetch(origin + "/api/agent/conversations", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ request_id: "browser-request-1" }),
      });
      assert.equal(response.status, 200);
    }
  });
  assert.equal(upstreamIds[0], upstreamIds[1]);
  assert.notEqual(upstreamIds[0], "browser-request-1");
  assert.deepEqual(stored, [["alice", conversation], ["alice", conversation]]);
});

test("SSE transparently forwards data-only frames, Last-Event-ID and connection comments", async () => {
  const text = ': connected\n\n: heartbeat\n\ndata: {"sequence":2,"type":"agent.progress","data":{"message":"正在核对证据\\n请稍候"}}\n\n';
  await withServer({ access, fetchImpl: async (_url, init) => {
    assert.equal(init.headers.get("Last-Event-ID"), "1");
    return new Response(text, { headers: { "Content-Type": "text/event-stream" } });
  }}, async (origin) => {
    const response = await fetch(origin + conversationPath + "/events", { headers: { "Last-Event-ID": "1" } });
    assert.equal(response.headers.get("Content-Type"), "text/event-stream; charset=utf-8");
    assert.equal(response.headers.get("Cache-Control"), "no-cache, no-transform");
    assert.equal(response.headers.get("X-Accel-Buffering"), "no");
    assert.equal(await response.text(), text);
  });
});

test("report endpoint verifies bytes and exposes fixed Chinese sections", async () => {
  const fixture = artifactFixture();
  await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
    const response = await fetch(origin + conversationPath + "/report");
    assert.equal(response.status, 200);
    const value = (await response.json()).data;
    assert.equal(value.report.root_cause, "连接池耗尽");
    assert.deepEqual(value.sections.map((section) => section.title), ["定位结论", "问题描述", "关键发现", "原因与因素", "完成条件", "服务端验证", "时间相关性", "证据缺口", "限制", "处置建议与安全说明"]);
  });
  assert.equal(fixture.calls.filter((url) => url.includes("/content?")).length, 1);
});

for (const fault of ["badHash", "badSource", "badUrl"]) {
  test(`report rejects ${fault} and never returns unverified bytes`, async () => {
    const fixture = artifactFixture({ [fault]: true });
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + conversationPath + "/report");
      assert.equal(response.status, 502);
      assert.equal((await response.json()).data, null);
    });
    if (fault !== "badHash") assert.equal(fixture.calls.filter((url) => url.includes("/content?")).length, 0);
  });
}

test("list rewrites download URL; ZIP never downloads without explicit request and notice acknowledgement", async () => {
  const fixture = artifactFixture({ kind: "USER_RESULT_ARCHIVE", payload: Buffer.from("zip bytes") });
  await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
    const listing = await (await fetch(origin + conversationPath + "/artifacts")).json();
    const item = listing.data.artifacts[0];
    assert.match(item.download_notice, /原始目标日志/);
    assert.equal(item.download_url, `${conversationPath}/artifacts/${artifactId}/content`);
    for (const suffix of ["", "?download=archive"]) {
      assert.equal((await fetch(origin + item.download_url + suffix)).status, 409);
    }
    assert.equal(fixture.calls.filter((url) => url.includes("/content?")).length, 0);
    const response = await fetch(origin + item.download_url + "?download=archive&acknowledge_raw_logs=true");
    assert.equal(response.status, 200);
    assert.equal(await response.text(), "zip bytes");
    assert.equal(response.headers.get("X-Content-SHA256"), fixture.artifact.sha256);
  });
});

test("missing report fields are errors, never fabricated conclusions", () => {
  const incomplete = { ...report };
  delete incomplete.root_cause;
  assert.throws(() => reportSections(incomplete), /格式/);
  const sections = reportSections({ ...report, status: "INCONCLUSIVE", root_cause: null });
  assert.equal(sections[0].value, null);
});

test("legacy Generic V1 preserves its result when no downloadable artifact exists", async () => {
  const legacy = { conclusion: "当前证据不足", root_cause_analysis: "缺少服务端日志", source_job_id: jobId };
  await withServer({ access, fetchImpl: async (url) => {
    const path = new URL(url).pathname;
    if (path.endsWith(`/conversations/${conversation}`)) return envelope({ case_id: caseId });
    if (path.endsWith("/artifacts")) return envelope({ artifacts: [] });
    return envelope({ case_view: { case_id: caseId, status: "UNRESOLVED", artifacts: [], generic_result: legacy } });
  }}, async (origin) => {
    const response = await fetch(origin + conversationPath + "/report");
    assert.equal(response.status, 200);
    assert.deepEqual((await response.json()).data, { format: "generic-v1", report: legacy });
  });
});
