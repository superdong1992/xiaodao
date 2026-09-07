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
const conversationId = "10000000-0000-4000-8000-000000000001";

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function reportResponse(ok = true) {
  return { ok, json: async () => ({ ok, data: { report: "verified fixture" } }) };
}

function browser({ fetchReport = async () => reportResponse(), renderReport = async () => {} } = {}) {
  const listeners = new Map();
  const trace = [];
  const retries = [];
  const requests = [];
  const urls = [];
  const context = vm.createContext({
    conversationId,
    EventSource: class {
      constructor(url) { urls.push(url); }
      addEventListener(type, callback) { listeners.set(type, callback); }
      close() { trace.push("closed"); }
    },
    renderEventAsText: async (event) => { trace.push(`${event.sequence}:${event.type}`); },
    renderVerifiedReport: async (data) => { await renderReport(data); trace.push("report-rendered"); },
    showEventRetry: (error) => { retries.push(error.message); trace.push("retry"); },
    fetch: (url) => { requests.push(url); return fetchReport(url); },
  });
  vm.runInContext(subscriptionSource, context, { filename: "website-agent-api.md", timeout: 1000 });
  assert.deepEqual(urls, [`/api/agent/conversations/${conversationId}/events`]);
  return {
    trace, retries, requests,
    get cursor() { return vm.runInContext("lastSequence", context); },
    get pending() { return vm.runInContext("pending", context); },
    get stopped() { return vm.runInContext("stopped", context); },
    emit(type, sequence, overrides = {}, frame = {}) {
      const event = { schema_version: 1, conversation_id: conversationId, type, sequence, data: {}, ...overrides };
      assert.ok(listeners.has(type), `missing event handler: ${type}`);
      listeners.get(type)({ data: JSON.stringify(event), lastEventId: String(sequence), ...frame });
    },
    drain() { return vm.runInContext("queue", context); },
  };
}

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
  assert.deepEqual(page.requests, [`/api/agent/conversations/${conversationId}/report`]);
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
  assert.equal(page.requests.length, 1);
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

for (const [name, overrides, frame] of [
  ["wrong conversation", { conversation_id: "another-conversation" }],
  ["wrong event type", { type: "conversation.completed" }],
  ["wrong schema", { schema_version: 2 }],
  ["string schema", { schema_version: "1" }],
  ["zero sequence", { sequence: 0 }, { lastEventId: "0" }],
  ["fractional sequence", { sequence: 1.5 }, { lastEventId: "1.5" }],
  ["unsafe sequence", { sequence: Number.MAX_SAFE_INTEGER + 1 }, { lastEventId: String(Number.MAX_SAFE_INTEGER + 1) }],
  ["mismatched SSE id", {}, { lastEventId: "2" }],
  ["malformed JSON", {}, { data: "{" }],
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
    assert.deepEqual(page.requests, []);
  });
}

test("quickstart identifies the deployed contract and separates preflight from model acceptance", () => {
  const openapi = JSON.parse(read("schemas/v2/web-api.openapi.snapshot.json"));
  assert.equal(openapi.info.version, "8.0.0");
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
  assert.equal(names.length, 5);
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
    ["docs/website-agent-api.md", ["website-agent-quickstart.md", "../schemas/v2/web-api.openapi.snapshot.json"]],
    ["docs/website-agent-quickstart.md", ["website-agent-api.md", "../examples/website-agent/README.md", "../tools/test-flow/README.md"]],
    ["examples/website-agent/README.md", ["../../docs/website-agent-quickstart.md", "../../docs/website-agent-api.md", "server.ts"]],
  ]);
  for (const [filename, required] of entrypoints) {
    const markdown = read(filename);
    for (const link of required) {
      assert.ok(markdown.includes(`](${link})`), `${filename} must link to ${link}`);
      assert.ok(fs.existsSync(path.resolve(ROOT, path.dirname(filename), link)), `broken link: ${filename} -> ${link}`);
    }
  }
});
