import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const read = (relative) => fs.readFileSync(path.join(ROOT, relative), "utf8").replace(/\r\n/g, "\n");
const guide = read("docs/website-agent-api.md");
const quickstart = read("docs/website-agent-quickstart.md");
const example = read("examples/website-agent/README.md");
const scripts = [...guide.matchAll(/^```javascript\n([\s\S]*?)^```/gm)];
assert.equal(scripts.length, 1, "the API guide must expose one executable browser subscription example");
const subscriptionSource = scripts[0][1];
const runId = "50000000-0000-0000-0000-000000000001";
const conversationId = "10000000-0000-4000-8000-000000000001";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function reportResponse(ok = true, data = {}) {
  const result = { conversation_id: conversationId, report_state: "READY", report: "verified fixture", ...data };
  return { ok, json: async () => ({ ok, data: { schema_version: 3, selected_run_id: runId, current_run: { run_id: runId }, conversation_id: result.conversation_id,
    report_state: result.report_state, included: ["report"], result } }) };
}

function snapshotResponse(data = {}, ok = true) {
  const ready = ["RESOLVED", "PARTIALLY_RESOLVED", "UNRESOLVED"].includes(data.case_status);
  const report_state = ready ? "READY" : "PENDING";
  return { ok, json: async () => ({ ok, data: { schema_version: 3, selected_run_id: runId, current_run: { run_id: runId }, conversation_id: conversationId,
    status: "RUNNING", case_status: null, failure: null, history: [], attachments: [], current_questions: [], last_event_id: 0,
    included: ["history", "report", "artifacts"], report_state,
    result: { conversation_id: conversationId, report_state, report: ready ? "verified fixture" : null }, ...data } }) };
}

function browser({ fetchReport = async () => reportResponse(), renderReport = async () => {},
  fetchSnapshot = async () => snapshotResponse(), renderFailure = async () => {} } = {}) {
  const listeners = new Map();
  let source;
  const trace = [];
  const retries = [];
  const requests = [];
  const urls = [];
  const snapshots = [];
  const failures = [];
  const context = vm.createContext({
    conversationId,
    EventSource: class {
      constructor(url) { urls.push(url); source = this; }
      addEventListener(type, callback) { listeners.set(type, callback); }
      close() { trace.push("closed"); }
    },
    renderEventAsText: async (event) => { trace.push(`${event.sequence}:${event.type}`); },
    renderVerifiedReport: async (data) => { await renderReport(data); trace.push("report-rendered"); },
    renderConversationAsText: async (data) => { snapshots.push(data); },
    renderFailureAsText: async (data) => { await renderFailure(data); failures.push(data); trace.push("failure-rendered"); },
    showEventRetry: (error) => { retries.push(error.message); trace.push("retry"); },
    fetch: (url) => { requests.push(url); return url.includes("?include=report") ? fetchReport(url) : fetchSnapshot(url); },
  });
  vm.runInContext(subscriptionSource, context, { filename: "website-agent-api.md", timeout: 1000 });
  assert.deepEqual(urls, [`/api/agent/conversations/${conversationId}/events`]);
  assert.equal(typeof source.onmessage, "function", "data-only SSE requires the default message receiver");
  assert.equal(listeners.size, 0, "business types must be dispatched from JSON, not named SSE events");
  const emitWire = (wire) => {
    for (const block of wire.split(/\r?\n\r?\n/)) {
      const data = [];
      let type = "message";
      for (const line of block.split(/\r?\n/)) {
        if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
        else if (line.startsWith("event:")) type = line.slice(6).replace(/^ /, "");
      }
      if (!data.length) continue; // Comments do not dispatch browser messages.
      const callback = type === "message" ? source.onmessage : listeners.get(type);
      assert.equal(typeof callback, "function", `missing browser event receiver: ${type}`);
      callback({ type, data: data.join("\n") });
    }
  };
  return {
    trace, retries, requests, emitWire, snapshots, failures,
    get cursor() { return vm.runInContext("lastSequence", context); },
    get pending() { return vm.runInContext("pending", context); },
    get stopped() { return vm.runInContext("stopped", context); },
    emit(type, sequence, overrides = {}, frame = {}) {
      const event = { schema_version: 2, run_id: runId, conversation_id: conversationId, type, sequence, data: {}, ...overrides };
      emitWire(`data: ${frame.data ?? JSON.stringify(event)}\n\n`);
    },
    drain() { return vm.runInContext("queue", context); },
    loadReport(selectedRun = null) { return vm.runInContext(selectedRun === null ? "loadReport()" : `loadReport(null, ${JSON.stringify(selectedRun)})`, context); },
  };
}

test("documented wire frames dispatch through onmessage and ignore connection and heartbeat comments", async () => {
  const blocks = [...guide.matchAll(/^```text\n([\s\S]*?)^```/gm)];
  assert.equal(blocks.length, 1);
  const wire = blocks[0][1].replaceAll("10000000-0000-0000-0000-000000000001", conversationId);
  assert.ok(wire.startsWith(": connected\n\n"));
  assert.ok(wire.includes(": heartbeat\n\n"));
  assert.doesNotMatch(wire, /^(?:event|id|retry):/m);
  assert.doesNotMatch(wire, /\[DONE\]/);
  assert.equal(wire.split("\n\n").filter((block) => block.startsWith("data:")).length, 2);
  for (const line of wire.trim().split("\n")) {
    if (line && !line.startsWith(":")) {
      assert.ok(line.startsWith("data: "));
      assert.equal(JSON.parse(line.slice(6)).schema_version, 2);
    }
  }
  const page = browser();
  page.emitWire(wire);
  await page.drain();
  assert.deepEqual(page.trace, ["13:case.updated", "14:assistant.question"]);
  assert.equal(page.cursor, 14);
  assert.equal(page.pending, 0);
  assert.deepEqual(page.retries, []);
});

test("documented onmessage dispatches every supported business type from JSON", async () => {
  const types = ["message.accepted", "message.updated", "assistant.question", "agent.progress",
    "case.updated", "result.available", "archive.updated", "attachment.updated",
    "agent.failed", "conversation.interrupted", "conversation.completed"];
  const page = browser();
  types.forEach((type, index) => page.emit(type, index + 1));
  await page.drain();
  assert.deepEqual(page.trace.filter((item) => /^\d+:/.test(item)), types.map((type, index) => `${index + 1}:${type}`));
  assert.equal(page.requests.filter((url) => url.includes("?include=report")).length, 1);
  assert.equal(page.cursor, types.length);
  assert.equal(page.stopped, true);
  assert.deepEqual(page.retries, []);
});

test("documented SSE waits for the verified report before advancing or closing", async () => {
  const download = deferred();
  const downloadStarted = deferred();
  const page = browser({ fetchReport: () => { downloadStarted.resolve(); return download.promise; } });
  page.emit("result.available", 1);
  page.emit("conversation.completed", 2);
  await downloadStarted.promise;
  assert.equal(page.cursor, 0);
  assert.equal(page.pending, 2);
  assert.deepEqual(page.trace, ["1:result.available"]);
  assert.equal(page.stopped, false);
  download.resolve(reportResponse());
  await page.drain();
  assert.deepEqual(page.trace, ["1:result.available", "report-rendered", "2:conversation.completed", "closed"]);
  assert.deepEqual(page.requests, [`/api/agent/conversations/${conversationId}`, `/api/agent/conversations/${conversationId}?include=report&run_id=${runId}`]);
  assert.equal(page.cursor, 2);
  assert.equal(page.pending, 0);
  assert.deepEqual(page.retries, []);
});

test("documented SSE waits for report rendering, not just its download", async () => {
  const rendering = deferred();
  const renderingStarted = deferred();
  const page = browser({ renderReport: () => { renderingStarted.resolve(); return rendering.promise; } });
  page.emit("result.available", 1);
  page.emit("conversation.completed", 2);
  await renderingStarted.promise;
  assert.equal(page.cursor, 0);
  assert.equal(page.stopped, false);
  assert.deepEqual(page.trace, ["1:result.available"]);
  rendering.resolve();
  await page.drain();
  assert.equal(page.cursor, 2);
  assert.deepEqual(page.trace.slice(1), ["report-rendered", "2:conversation.completed", "closed"]);
});

for (const [name, fetchReport, renderReport] of [
  ["HTTP failure", async () => reportResponse(false)],
  ["network failure", async () => { throw new Error("network unavailable"); }],
  ["invalid JSON", async () => ({ ok: true, json: async () => { throw new Error("invalid JSON"); } })],
  ["render failure", async () => reportResponse(), async () => { throw new Error("render failed"); }],
]) {
  test(`documented SSE ${name} preserves the successful cursor and offers retry`, async () => {
    const page = browser({ fetchReport, renderReport });
    page.emit("agent.progress", 1);
    await page.drain();
    page.emit("result.available", 2);
    page.emit("conversation.completed", 3);
    await page.drain();
    assert.equal(page.cursor, 1);
    assert.equal(page.stopped, true);
    assert.equal(page.pending, 0);
    assert.equal(page.retries.length, 1);
    assert.deepEqual(page.trace, ["1:agent.progress", "2:result.available", "closed", "retry"]);
    page.emit("conversation.completed", 3);
    await page.drain();
    assert.equal(page.cursor, 1);
    assert.equal(page.retries.length, 1);
  });
}

test("documented SSE deduplicates queued and replayed report events", async () => {
  const page = browser();
  page.emit("result.available", 1);
  page.emit("result.available", 1);
  await page.drain();
  page.emit("result.available", 1);
  page.emit("agent.progress", 2);
  page.emit("conversation.completed", 3);
  await page.drain();
  assert.deepEqual(page.trace, ["1:result.available", "report-rendered", "2:agent.progress", "3:conversation.completed", "closed"]);
  assert.equal(page.requests.filter((url) => url.includes("?include=report")).length, 1);
  assert.equal(page.cursor, 3);
});

test("documented SSE bounds pending work to 128 and stops safely on overflow", async () => {
  const download = deferred();
  const started = deferred();
  const page = browser({ fetchReport: () => { started.resolve(); return download.promise; } });
  page.emit("result.available", 1);
  await started.promise;
  for (let sequence = 2; sequence <= 128; sequence += 1) page.emit("agent.progress", sequence);
  assert.equal(page.pending, 128);
  assert.equal(page.stopped, false);
  page.emit("conversation.completed", 129);
  assert.equal(page.pending, 128);
  assert.equal(page.stopped, true);
  assert.equal(page.retries.length, 1);
  assert.match(page.retries[0], /重新连接.*回放历史/);
  download.resolve(reportResponse());
  await page.drain();
  assert.equal(page.pending, 0);
  assert.equal(page.cursor, 0);
  assert.deepEqual(page.trace, ["1:result.available", "closed", "retry"]);
});

const publicFailure = { code: "INTAKE_OUTPUT_INVALID", message: "补充信息整理失败，请核对输入后新建任务。", retryable: false,
  details: [{ field: "phase", actual: "INTAKE" }, { field: "diagnostic_id", actual: "diag-persisted-failure" }] };

test("documented page refresh restores durable failure without skipping unprocessed history", async () => {
  const page = browser({ fetchSnapshot: async () => snapshotResponse({ status: "FAILED", failure: publicFailure, last_event_id: 99 }) });
  await page.drain();
  assert.deepEqual(page.failures, [publicFailure]);
  assert.equal(page.cursor, 0);
  assert.equal(page.stopped, false);
  page.emit("agent.progress", 1);
  await page.drain();
  assert.equal(page.cursor, 1);
  assert.deepEqual(page.requests, [`/api/agent/conversations/${conversationId}`]);
});

for (const type of ["agent.failed", "conversation.interrupted"]) {
  test(`documented ${type} waits for snapshot and failure rendering before advancing or closing`, async () => {
    const snapshot = deferred();
    const snapshotStarted = deferred();
    const rendering = deferred();
    const renderingStarted = deferred();
    let reads = 0;
    const page = browser({ fetchSnapshot: async () => {
      if (++reads === 1) return snapshotResponse();
      snapshotStarted.resolve();
      return snapshot.promise;
    }, renderFailure: async () => { renderingStarted.resolve(); await rendering.promise; } });
    await page.drain();
    page.emit(type, 1);
    page.emit("conversation.completed", 2);
    await snapshotStarted.promise;
    assert.equal(page.cursor, 0);
    assert.equal(page.stopped, false);
    snapshot.resolve(snapshotResponse({ status: "FAILED", failure: publicFailure }));
    await renderingStarted.promise;
    assert.equal(page.cursor, 0);
    assert.equal(page.stopped, false);
    rendering.resolve();
    await page.drain();
    assert.deepEqual(page.trace, [`1:${type}`, "failure-rendered", "2:conversation.completed", "closed"]);
    assert.equal(page.cursor, 2);
    assert.deepEqual(page.failures, [publicFailure]);
    assert.equal(reads, 2);
  });
}

test("documented failed snapshot fetch preserves prior cursor and prevents completion", async () => {
  let reads = 0;
  const page = browser({ fetchSnapshot: async () => snapshotResponse({}, ++reads === 1) });
  page.emit("agent.progress", 1);
  await page.drain();
  page.emit("agent.failed", 2);
  page.emit("conversation.completed", 3);
  await page.drain();
  assert.equal(page.cursor, 1);
  assert.equal(page.stopped, true);
  assert.deepEqual(page.trace, ["1:agent.progress", "2:agent.failed", "closed", "retry"]);
  assert.deepEqual(page.failures, []);
  assert.equal(page.retries.length, 1);
});

test("documented archive uncertainty on refresh renders failure and loads JSON before ZIP is ready", async () => {
  const archiveFailure = { ...publicFailure, code: "DISPATCH_REJECTED", message: "报告已生成，但归档状态暂时无法确认。",
    details: [{ field: "phase", actual: "ARCHIVE_STATUS_COMMIT" }, { field: "persistence", actual: "UNKNOWN" }] };
  const page = browser({ fetchSnapshot: async () => snapshotResponse({ case_status: "RESOLVED", archive_status: "PENDING", failure: archiveFailure }) });
  await page.drain();
  assert.deepEqual(page.trace, ["failure-rendered", "report-rendered"]);
  assert.deepEqual(page.failures, [archiveFailure]);
  assert.deepEqual(page.requests, [`/api/agent/conversations/${conversationId}`]);
  assert.equal(page.cursor, 0);
  assert.equal(page.stopped, false);
});

test("documented refresh rejects another conversation before displaying its content", async () => {
  const page = browser({ fetchSnapshot: async () => snapshotResponse({ conversation_id: "another-conversation", failure: publicFailure }) });
  page.emit("conversation.completed", 1);
  await page.drain();
  assert.deepEqual(page.snapshots, []);
  assert.deepEqual(page.failures, []);
  assert.deepEqual(page.trace, ["closed", "retry"]);
  assert.equal(page.cursor, 0);
});

test("refresh followed by historical result event fetches and renders the immutable report once", async () => {
  const page = browser({ fetchSnapshot: async () => snapshotResponse({ case_status: "PARTIALLY_RESOLVED" }) });
  await page.drain();
  page.emit("result.available", 8);
  page.emit("archive.updated", 9);
  page.emit("conversation.completed", 10);
  await page.drain();
  assert.equal(page.requests.filter((url) => url.includes("?include=report")).length, 0);
  assert.equal(page.trace.filter((item) => item === "report-rendered").length, 1);
  assert.equal(page.cursor, 10);
  assert.deepEqual(page.retries, []);
});

test("concurrent report readers share one request and successful rendering", async () => {
  const report = deferred();
  const page = browser({ fetchReport: () => report.promise });
  await page.drain();
  const first = page.loadReport();
  const second = page.loadReport();
  assert.equal(first, second);
  report.resolve(reportResponse());
  assert.equal(await first, true);
  assert.equal(await second, true);
  assert.equal(await page.loadReport(), true);
  assert.equal(page.requests.filter((url) => url.includes("?include=report")).length, 1);
  assert.equal(page.trace.filter((item) => item === "report-rendered").length, 1);
});

test("historical events do not reread snapshots or reports until a card is explicitly opened", async () => {
  const newerRun = "60000000-0000-0000-0000-000000000001";
  const page = browser({ fetchSnapshot: async () => snapshotResponse({ current_run: { run_id: newerRun },
    selected_run_id: newerRun, case_status: "RESOLVED", last_event_id: 10 }), fetchReport: async () => reportResponse() });
  await page.drain();
  page.emit("run.started", 1);
  page.emit("result.available", 2);
  page.emit("agent.failed", 3);
  page.emit("conversation.interrupted", 4);
  page.emit("conversation.completed", 5);
  await page.drain();
  assert.equal(page.stopped, false);
  assert.equal(page.trace.filter((item) => item === "report-rendered").length, 1);
  assert.equal(page.requests.length, 1);
  assert.equal(await page.loadReport(runId), true);
  assert.equal(await page.loadReport(runId), true);
  assert.equal(page.trace.filter((item) => item === "report-rendered").length, 2);
  assert.equal(page.requests.filter((url) => url.includes(`run_id=${runId}`)).length, 1);
  page.emit("conversation.completed", 11, { run_id: newerRun });
  await page.drain();
  assert.equal(page.stopped, true); assert.equal(page.cursor, 11);
});

test("a newly started run after the restored snapshot refreshes the current run once", async () => {
  const newerRun = "60000000-0000-0000-0000-000000000001";
  let reads = 0;
  const page = browser({ fetchSnapshot: async () => ++reads === 1
    ? snapshotResponse({ last_event_id: 10 })
    : snapshotResponse({ last_event_id: 11, current_run: { run_id: newerRun }, selected_run_id: newerRun }) });
  await page.drain();
  page.emit("run.started", 11, { run_id: newerRun });
  page.emit("conversation.completed", 12); // 旧轮归档迟到。
  await page.drain();
  assert.equal(reads, 2);
  assert.equal(page.stopped, false);
  assert.equal(page.cursor, 12);
  assert.deepEqual(page.retries, []);
});

for (const state of ["PENDING", "UNAVAILABLE"]) {
  test(`normal ${state} report is not rendered or cached and can be read later`, async () => {
    let reads = 0;
    const page = browser({ fetchReport: async () => ++reads === 1
      ? reportResponse(true, { report_state: state, report: null, failure: state === "UNAVAILABLE" ? publicFailure : null })
      : reportResponse() });
    await page.drain();
    assert.equal(await page.loadReport(), false);
    assert.ok(!page.trace.includes("report-rendered"));
    assert.equal(await page.loadReport(), true);
    assert.equal(reads, 2);
    assert.deepEqual(page.retries, []);
  });
}

test("report rendering failure is not cached as success", async () => {
  let renders = 0;
  const page = browser({ renderReport: async () => { if (++renders === 1) throw new Error("render failed"); } });
  await page.drain();
  await assert.rejects(page.loadReport(), /render failed/);
  assert.equal(await page.loadReport(), true);
  assert.equal(page.requests.filter((url) => url.includes("?include=report")).length, 2);
});

test("report response for another conversation is rejected before rendering", async () => {
  const page = browser({ fetchReport: async () => reportResponse(true, { conversation_id: "another-conversation" }) });
  await page.drain();
  await assert.rejects(page.loadReport(), /报告暂未加载/);
  assert.ok(!page.trace.includes("report-rendered"));
});

for (const [name, overrides, frame] of [
  ["wrong conversation", { conversation_id: "another-conversation" }],
  ["unsupported event type", { type: "private.reasoning" }],
  ["missing event type", { type: undefined }],
  ["non-string event type", { type: ["agent.progress"] }],
  ["wrong schema", { schema_version: 1 }],
  ["string schema", { schema_version: "1" }],
  ["zero sequence", { sequence: 0 }],
  ["fractional sequence", { sequence: 1.5 }],
  ["string sequence", { sequence: "1" }],
  ["unsafe sequence", { sequence: Number.MAX_SAFE_INTEGER + 1 }],
  ["malformed JSON", {}, { data: "{" }],
  ["non-object JSON", {}, { data: "null" }],
]) {
  test(`documented SSE rejects ${name} before rendering`, async () => {
    const page = browser();
    page.emit("agent.progress", 1, overrides, frame);
    page.emit("conversation.completed", 2);
    await page.drain();
    assert.equal(page.cursor, 0);
    assert.equal(page.pending, 0);
    assert.equal(page.retries.length, 1);
    assert.deepEqual(page.trace, ["closed", "retry"]);
    assert.deepEqual(page.requests, [`/api/agent/conversations/${conversationId}`]);
  });
}

test("website guidance describes SSE v2 and explicit owned-storage upgrade", () => {
  for (const [name, content] of [["API reference", guide], ["quickstart", quickstart], ["backend README", example]]) {
    for (const term of ["onmessage", "sequence", "type", "Last-Event-ID", "fetch", "conversation.completed"])
      assert.ok(content.includes(term), `${name} is missing ${term}`);
    assert.match(content, /不.*(?:发送|包含).*`(?:id|event):`/);
    assert.match(content, /(?:重连|回放)[\s\S]*历史/);
    assert.match(content, /(?:处理成功|成功后才推进游标)/);
    assert.match(content, /8\.0\.0.*预览版/);
    assert.match(content, /8\.2\.0/);
    assert.match(content, /v11-contract-r2/);
    assert.match(content, /(?:显式.*升级|必须.*副本升级)/);
    assert.ok(content.includes("data-upgrade-v11-r2.md"));
    assert.match(content, /(?:那次|历史)[\s\S]*没有改变 V11 数据合同/);
    assert.doesNotMatch(content, /会在短暂断线时自动携带/);
    assert.doesNotMatch(content, /自带的游标表示/);
  }
  assert.doesNotMatch(subscriptionSource, /lastEventId|addEventListener\(type/);
  assert.match(subscriptionSource, /types\.includes\(event\.type\)/);
});

test("quickstart identifies the deployed contract and separates preflight from model acceptance", () => {
  const openapi = JSON.parse(read("schemas/v2/web-api.openapi.snapshot.json"));
  assert.equal(openapi.info.version, "8.2.0");
  assert.ok(quickstart.includes(`\`${openapi.info.version}\``));
  assert.match(quickstart, /V11/);
  const preflight = quickstart.split("## 2.")[1].split("## 3.")[0];
  for (const route of ["/live", "/ready", "/openapi.json", "/docs"]) assert.ok(preflight.includes(route));
  assert.match(preflight, /"ok":true,"data":\{"status":"live"\},"error":null/);
  assert.match(preflight, /data\.ready=true/);
  assert.match(preflight, /不套 `ok\/data` 信封/);
  assert.match(preflight, /不会创建定位任务/);
  assert.match(preflight, /创建一个空会话.*这会持久保存会话，但不会调用模型/);
  assert.match(preflight, /不证明模型或诊断全链路已经可用/);
  assert.match(quickstart, /发送消息会触发真实模型/);
  for (const term of ["--plan-only", "admission blocker", "Proof", "Stage", "Gate", "verdict.json"]) assert.ok(quickstart.includes(term));
  assert.match(quickstart, /手工联调记录不能替代官方/);
});

test("example setup documents the actual authorization callbacks and safe deployment boundaries", () => {
  const implementation = read("examples/website-agent/server.ts");
  const access = implementation.match(/export type Access = \{([\s\S]*?)\n\};/)[1];
  const names = [...access.matchAll(/^\s+(\w+)\([^\n]*\): Promise</gm)].map((match) => match[1]);
  assert.equal(names.length, 1);
  for (const name of names) assert.ok(example.includes(`\`${name}(`), `missing callback guidance: ${name}`);
  for (const setting of ["WEBSITE_AUTH_MODULE", "XIAODAO_BASE_URL", "PUBLIC_BASE_URL", "PORT"]) assert.ok(example.includes(setting));
  assert.match(example, /Node\.js 24\+/);
  assert.match(example, /具名导出 `access`/);
  assert.match(example, /从仓库根目录启动/);
  assert.match(example, /node examples\/website-agent\/server\.ts/);
  assert.match(example, /固定监听 `127\.0\.0\.1`/);
  assert.match(example, /业务请求全部返回 `401`/);
  assert.match(example, /持久、幂等/);
  assert.match(example, /CSRF \/ Origin/);
  assert.match(example, /不要为了联调删除授权检查/);
});

test("website onboarding entry points and relative documentation links resolve", () => {
  const entrypoints = new Map([
    ["README.md", ["docs/website-agent-quickstart.md", "docs/website-agent-api.md", "examples/website-agent/README.md"]],
    ["docs/website-agent-api.md", ["website-agent-quickstart.md", "../schemas/v2/web-api.openapi.snapshot.json", "data-upgrade-v11-r2.md"]],
    ["docs/website-agent-quickstart.md", ["website-agent-api.md", "../examples/website-agent/README.md", "../tools/test-flow/README.md", "data-upgrade-v11-r2.md"]],
    ["examples/website-agent/README.md", ["../../docs/website-agent-quickstart.md", "../../docs/website-agent-api.md", "server.ts", "../../docs/data-upgrade-v11-r2.md"]],
  ]);
  for (const [filename, required] of entrypoints) {
    const markdown = read(filename);
    for (const link of required) {
      assert.ok(markdown.includes(`](${link})`), `${filename} must link to ${link}`);
      assert.ok(fs.existsSync(path.resolve(ROOT, path.dirname(filename), link)), `broken link: ${filename} -> ${link}`);
    }
  }
});

test("website API exposes only conversation and attachment concepts with explicit partial reads", () => {
  for (const content of [guide, quickstart, example]) {
    assert.match(content, /schema_version=3/);
    assert.match(content, /included/);
    assert.match(content, /include=none|include: \[\]/);
    assert.match(content, /(?:未加载|未请求).*null/);
  }
  assert.match(guide, /旧.*\/status.*\/report.*已删除/);
  assert.match(subscriptionSource, /snapshot\.report_state === "READY"\) await loadReport\(snapshot\)/);
  assert.doesNotMatch(subscriptionSource, /conversations\/\$\{conversationId\}\/(?:status|report)/);
});
