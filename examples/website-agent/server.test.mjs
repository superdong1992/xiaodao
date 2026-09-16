import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { once } from "node:events";
import fs from "node:fs";
import fsPromises from "node:fs/promises";
import { syncBuiltinESMExports } from "node:module";
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

function artifactFixture({ kind = "USER_RESULT", payload = Buffer.from(JSON.stringify(report)), badSource = false, badHash = false, badUrl = false,
  headerOverrides = {}, receivedPayload, downloadStatus = 200,
  artifactOverrides = {}, summaryOverrides = {}, caseOverrides = {} } = {}) {
  const calls = [];
  const artifact = {
    artifact_id: artifactId, kind, name: kind === "USER_RESULT" ? "diagnosis-result.json" : kind === "GENERIC_REPORT" ? "generic-diagnosis.md" : "result.zip",
    content_type: kind === "USER_RESULT" ? "application/json" : kind === "GENERIC_REPORT" ? "text/markdown" : "application/zip",
    size: payload.length, sha256: createHash("sha256").update(payload).digest("hex"),
    download_url: badUrl ? "http://other.internal/secret" : `${base}/api/v1/artifacts/${artifactId}/content?case_id=${caseId}`,
    ...artifactOverrides,
  };
  const fetchImpl = async (url, init) => {
    calls.push(String(url));
    const path = new URL(url).pathname;
    if (path === `/api/v1/agent/conversations/${conversation}`) return envelope({ conversation_id: conversation, case_id: caseId });
    if (path === `/api/v1/cases/${caseId}`) return envelope({ case_view: {
      case_id: caseId, status: "RESOLVED", final_result: { proposed_by_job_id: jobId },
      artifacts: [{ ...artifact, resource_kind: "FILE", created_by_job_id: badSource ? artifactId : jobId,
        downloadable: true, ...summaryOverrides }], ...caseOverrides,
    }});
    if (path === `/api/v1/cases/${caseId}/artifacts`) throw new Error("CaseView already contains the authoritative artifact list");
    if (path === `/api/v1/artifacts/${artifactId}/content`) {
      assert.equal(init.redirect, "manual");
      const headers = new Headers({
        "Content-Type": artifact.content_type, "Content-Length": String(payload.length), "X-Content-SHA256": artifact.sha256,
      });
      for (const [name, value] of Object.entries(headerOverrides)) {
        if (value === null) headers.delete(name); else headers.set(name, value);
      }
      return new Response(receivedPayload ?? (badHash ? Buffer.alloc(payload.length, 0) : payload), { status: downloadStatus, headers });
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
  assert.deepEqual(fixture.calls, [
    `${base}/api/v1/agent/conversations/${conversation}`,
    `${base}/api/v1/cases/${caseId}`,
    `${base}/api/v1/artifacts/${artifactId}/content?case_id=${caseId}`,
  ]);
});

for (const fault of ["badHash", "badSource"]) {
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

test("published download URL cannot choose the host or path of an internal download", async () => {
  const fixture = artifactFixture({ badUrl: true });
  await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
    const response = await fetch(origin + conversationPath + "/report");
    assert.equal(response.status, 200);
    assert.equal((await response.json()).data.report.root_cause, report.root_cause);
  });
  assert.deepEqual(fixture.calls.filter((url) => url.includes("/content?")), [
    `${base}/api/v1/artifacts/${artifactId}/content?case_id=${caseId}`,
  ]);
  assert.ok(fixture.calls.every((url) => new URL(url).origin === base));
});

test("configured internal path prefix survives reconstructed download routing", async () => {
  const fixture = artifactFixture({ badUrl: true });
  const requested = [];
  await withServer({ access, upstream: base + "/internal/xiaodao", fetchImpl: async (url, init) => {
    const parsed = new URL(url);
    requested.push(parsed.href);
    assert.ok(parsed.pathname.startsWith("/internal/xiaodao/api/v1/"));
    parsed.pathname = parsed.pathname.slice("/internal/xiaodao".length);
    return fixture.fetchImpl(parsed, init);
  } }, async (origin) => {
    assert.equal((await fetch(origin + conversationPath + "/report")).status, 200);
  });
  assert.ok(requested.includes(`${base}/internal/xiaodao/api/v1/artifacts/${artifactId}/content?case_id=${caseId}`));
});

test("missing optional download integrity headers still verifies received bytes", async () => {
  const absent = { "Content-Length": null, "X-Content-SHA256": null };
  for (const options of [{}, { badHash: true }, { receivedPayload: Buffer.from("short") }]) {
    const fixture = artifactFixture({ ...options, headerOverrides: absent });
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + conversationPath + "/report");
      assert.equal(response.status, Object.keys(options).length ? 502 : 200);
      if (!response.ok) assert.equal((await response.json()).data, null);
    });
  }
});

for (const options of [
  { headerOverrides: { "Content-Length": "1" } },
  { headerOverrides: { "X-Content-SHA256": "a".repeat(64) } },
  { headerOverrides: { "Content-Type": "text/html" } },
  { headerOverrides: { "Content-Encoding": "gzip" } },
  { downloadStatus: 307, headerOverrides: { Location: "http://other.internal/private" } },
]) {
  test(`present incorrect integrity headers or redirect rejects ${JSON.stringify(options)}`, async () => {
    const fixture = artifactFixture(options);
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + conversationPath + "/report");
      assert.equal(response.status, 502);
      assert.equal((await response.json()).data, null);
    });
    assert.ok(fixture.calls.every((url) => new URL(url).origin === base));
  });
}

test("safe failure diagnostics survive snapshot refresh and pre-stream HTTP errors", async () => {
  const diagnosticId = "50000000-0000-0000-0000-000000000001";
  const failure = { code: "INTAKE_OUTPUT_INVALID", message: "SECRET /srv/private/model-output", retryable: true,
    details: [{ field: "phase", actual: "INTAKE" }, { field: "diagnostic_id", actual: diagnosticId },
      { field: "location", actual: "input_values[2]" },
      { field: "location", actual: "inputs./srv/private" },
      { field: "location", actual: "raw_output" },
      { field: "raw_output", actual: "SECRET /srv/private" }] };
  await withServer({ access, fetchImpl: async (url) => {
    if (new URL(url).pathname.endsWith("/events")) return new Response(JSON.stringify({ ok: false, data: null,
      error: { ...failure, code: "DISPATCH_REJECTED" } }), { status: 503, headers: { "Content-Type": "application/json" } });
    return envelope({ conversation_id: conversation, status: "FAILED", failure });
  } }, async (origin) => {
    for (let i = 0; i < 2; i++) {
      const response = await fetch(origin + conversationPath);
      const text = await response.text();
      assert.ok(!text.includes("SECRET") && !text.includes("/srv"));
      assert.deepEqual(JSON.parse(text).data.failure, { code: failure.code,
        message: "补充信息整理失败，请核对输入后新建任务。", retryable: false,
        details: failure.details.slice(0, 3) });
    }
    const response = await fetch(origin + conversationPath + "/events");
    assert.equal(response.status, 503);
    const value = await response.json();
    assert.equal(value.error.code, "DISPATCH_REJECTED");
    assert.equal(value.error.retryable, true);
    assert.deepEqual(value.error.details, failure.details.slice(0, 3));
  });
});

test("archive uncertainty remains visible without blocking the already published report", async () => {
  const fixture = artifactFixture();
  await withServer({ access, fetchImpl: async (url, init) => {
    if (new URL(url).pathname === `/api/v1/agent/conversations/${conversation}`) return envelope({
      conversation_id: conversation, case_id: caseId, status: "RUNNING", archive_status: "PENDING",
      failure: { code: "DISPATCH_REJECTED", message: "internal", retryable: false,
        details: [{ field: "phase", actual: "ARCHIVE_STATUS_COMMIT" }, { field: "persistence", actual: "UNKNOWN" }] },
    });
    return fixture.fetchImpl(url, init);
  } }, async (origin) => {
    const snapshot = (await (await fetch(origin + conversationPath)).json()).data;
    assert.equal(snapshot.failure.message, "报告已生成，但归档状态暂时无法确认。");
    assert.equal(snapshot.status, "RUNNING");
    assert.equal(snapshot.failure.retryable, false);
    const response = await fetch(origin + conversationPath + "/report");
    assert.equal(response.status, 200);
    assert.equal((await response.json()).data.report.root_cause, report.root_cause);
  });
});

test("accepted message paused before Case creation exposes safe HTTP error on snapshot and SSE", async () => {
  const details = [{ field: "phase", actual: "DISPATCH_PAUSED" }, { field: "persistence", actual: "UNKNOWN" }];
  await withServer({ access, fetchImpl: async () => new Response(JSON.stringify({ ok: false, data: null,
    error: { code: "DISPATCH_REJECTED", message: "upstream internal", details, retryable: true } }),
    { status: 503, headers: { "Content-Type": "application/json" } }) }, async (origin) => {
    for (const suffix of ["", "/events"]) {
      const response = await fetch(origin + conversationPath + suffix);
      assert.equal(response.status, 503);
      assert.deepEqual((await response.json()).error, { code: "DISPATCH_REJECTED",
        message: "服务异常，已接收的任务暂时无法继续。", details, retryable: true });
    }
  });
});

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

for (const count of [1, 200]) {
  test(`artifact listing uses one Case snapshot for ${count} entries`, async () => {
    const seed = artifactFixture({ kind: "USER_RESULT_ARCHIVE", payload: Buffer.from("zip bytes") });
    const artifacts = Array.from({ length: count }, (_, index) => ({
      ...seed.artifact, artifact_id: `30000000-0000-0000-0000-${String(index + 1).padStart(12, "0")}`,
      resource_kind: "FILE", created_by_job_id: jobId, downloadable: true,
    }));
    const fixture = artifactFixture({ caseOverrides: { artifacts } });
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + conversationPath + "/artifacts");
      assert.equal(response.status, 200);
      assert.equal((await response.json()).data.artifacts.length, count);
    });
    assert.deepEqual(fixture.calls, [
      `${base}/api/v1/agent/conversations/${conversation}`, `${base}/api/v1/cases/${caseId}`,
    ]);
  });
}

test("report rendering and small report downloads perform no temporary-file IO", async (context) => {
  const directory = context.mock.method(fsPromises, "mkdtemp", () => { throw new Error("report unexpectedly created a temporary directory"); });
  const writing = context.mock.method(fs, "createWriteStream", () => { throw new Error("report unexpectedly opened a temporary file"); });
  syncBuiltinESMExports();
  try {
    const fixture = artifactFixture();
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const rendered = await fetch(origin + conversationPath + "/report");
      assert.equal(rendered.status, 200);
      assert.deepEqual((await rendered.json()).data.report, report);
      const downloaded = await fetch(origin + conversationPath + `/artifacts/${artifactId}/content`);
      assert.equal(downloaded.status, 200);
      assert.equal(await downloaded.text(), JSON.stringify(report));
    });
    assert.equal(directory.mock.callCount(), 0);
    assert.equal(writing.mock.callCount(), 0);
    assert.equal(fixture.calls.length, 6);
  } finally {
    context.mock.restoreAll();
    syncBuiltinESMExports();
  }
});

test("chunked Generic report bytes preserve Unicode and CRLF after verification", async () => {
  const markdown = "# 诊断结果\r\n采用 \"rpc_timeout\" 方法 🧭\r\n";
  const payload = Buffer.from(markdown);
  let offset = 0;
  const receivedPayload = new ReadableStream({
    pull(controller) {
      if (offset === payload.length) controller.close();
      else controller.enqueue(payload.subarray(offset, ++offset));
    },
  });
  const fixture = artifactFixture({ kind: "GENERIC_REPORT", payload, receivedPayload,
    headerOverrides: { "Content-Length": null, "X-Content-SHA256": null },
    caseOverrides: { final_result: null, generic_result_v2: { source_job_id: jobId, report_artifact_id: artifactId } } });
  await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
    const response = await fetch(origin + conversationPath + "/report");
    assert.equal(response.status, 200);
    assert.deepEqual((await response.json()).data, { format: "markdown", markdown });
  });
  assert.equal(fixture.calls.length, 3);
});

for (const [name, options] of [
  ["wrong Case", { caseOverrides: { case_id: artifactId } }],
  ["wrong source Job", { badSource: true }],
  ["unavailable download", { summaryOverrides: { downloadable: false } }],
  ["non-boolean download permission", { summaryOverrides: { downloadable: "true" } }],
  ["directory resource", { summaryOverrides: { resource_kind: "DIRECTORY" } }],
  ["invalid artifact ID", { artifactOverrides: { artifact_id: "../private" } }],
  ["invalid hash", { artifactOverrides: { sha256: "not-a-sha256" } }],
  ["wrong report type", { artifactOverrides: { content_type: "text/html" } }],
  ["wrong report name", { artifactOverrides: { name: "private.json" } }],
  ["negative size", { artifactOverrides: { size: -1 } }],
  ["non-integer size", { artifactOverrides: { size: 1.5 } }],
  ["wrong authoritative report", { caseOverrides: { final_result: null,
    unresolved_result: { source_job_id: jobId, user_result_artifact_id: jobId } } }],
]) {
  test(`single-snapshot report rejects ${name} before downloading`, async () => {
    const fixture = artifactFixture(options);
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + conversationPath + "/report");
      assert.equal(response.status, 502);
      assert.equal((await response.json()).data, null);
    });
    assert.equal(fixture.calls.length, 2);
    assert.ok(fixture.calls.every((url) => !url.includes("/content?")));
  });
}

test("single-snapshot report still rejects duplicate artifact identities", async () => {
  const seed = artifactFixture().artifact;
  const duplicate = { ...seed, resource_kind: "FILE", created_by_job_id: jobId, downloadable: true };
  const fixture = artifactFixture({ caseOverrides: { artifacts: [duplicate, duplicate] } });
  await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
    const response = await fetch(origin + conversationPath + "/report");
    assert.equal(response.status, 502);
    assert.equal((await response.json()).data, null);
  });
  assert.equal(fixture.calls.length, 2);
});

test("oversized report metadata is rejected before content fetch or allocation", async () => {
  const fixture = artifactFixture({ artifactOverrides: { size: 16 * 1024 * 1024 + 1 } });
  await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
    const response = await fetch(origin + conversationPath + "/report");
    assert.equal(response.status, 502);
    assert.equal((await response.json()).data, null);
  });
  assert.equal(fixture.calls.length, 2);
});

for (const difference of [-1, 1]) {
  test(`bounded report buffer rejects a ${difference < 0 ? "truncated" : "longer"} body with absent optional headers`, async () => {
    const payload = Buffer.from(JSON.stringify(report));
    const receivedPayload = difference < 0 ? payload.subarray(0, -1) : Buffer.concat([payload, Buffer.from(" ")]);
    const fixture = artifactFixture({ payload, receivedPayload,
      headerOverrides: { "Content-Length": null, "X-Content-SHA256": null } });
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + conversationPath + "/report");
      assert.equal(response.status, 502);
      const envelope = await response.json();
      assert.equal(envelope.data, null);
      assert.ok(!JSON.stringify(envelope).includes(report.root_cause));
    });
    assert.equal(fixture.calls.length, 3);
  });
}

for (const options of [
  { headerOverrides: { "Content-Length": null, "X-Content-SHA256": null }, expected: 200 },
  { headerOverrides: { "Content-Length": null, "X-Content-SHA256": null }, badHash: true, expected: 502 },
  { headerOverrides: { "Content-Type": "text/html" }, expected: 502 },
  { headerOverrides: { "Content-Encoding": "gzip" }, expected: 502 },
  { headerOverrides: { "X-Content-SHA256": "0".repeat(64) }, expected: 502 },
]) {
  test(`ZIP keeps spool verification with shared header rules ${JSON.stringify(options)}`, async () => {
    const fixture = artifactFixture({ ...options, kind: "USER_RESULT_ARCHIVE", payload: Buffer.from("zip bytes") });
    await withServer({ access, fetchImpl: fixture.fetchImpl }, async (origin) => {
      const response = await fetch(origin + conversationPath + `/artifacts/${artifactId}/content?download=archive&acknowledge_raw_logs=true`);
      assert.equal(response.status, options.expected);
      if (response.ok) assert.equal(await response.text(), "zip bytes");
      else assert.equal((await response.json()).data, null);
    });
    assert.equal(fixture.calls.length, 3);
  });
}
