/** Execute the README snippets themselves, so copied examples cannot drift. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const readme = readFileSync(new URL("./README.md", import.meta.url), "utf8").replace(/\r\n/g, "\n");
const scripts = [...readme.matchAll(/^```javascript\n([\s\S]*?)^```/gm)].map((match) => match[1]);
const creation = scripts.find((script) => script.includes("async function submitProblem()"));
const upload = scripts.find((script) => script.includes("async function submitLogs()"));
const html = readme.match(/<script type="module">([\s\S]*?)<\/script>/)[1]
  .replace(/^\s*import [^\n]+;$/gm, "");
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
function ids() { let next = 0; return { randomUUID: () => `logical-request-${++next}` }; }

for (const [path, marker, entry] of [
  ["./README.md", "把 `report-view.js`", "browser-client.js"],
  ["../../docs/website-agent-quickstart.md", "将 `examples/website-agent/`", "browser-client.js"],
  ["../../docs/website-report-followup.md", "复制 [示例目录]", "browser-client.js"],
  ["../../docs/website-report-followup.md", "BFF 同步部署 `server.ts`", "server.ts"],
  ["../../docs/website-redis-deployment.md", "同步部署 `examples/website-agent/server.ts`", "server.ts"],
  ["../../docs/production-upgrade-2026-09-23.md", "同步部署整个", "server.mjs"],
]) {
  test(`部署清单包含入口的全部本地静态依赖：${path}/${entry}`, () => {
    const document = readFileSync(new URL(path, import.meta.url), "utf8").replace(/\r\n/g, "\n");
    const paragraph = document.split(/\n\s*\n/).find((part) => part.startsWith(marker)); assert.ok(paragraph, marker);
    const listed = new Set([...paragraph.matchAll(/`([^`]+\.(?:m?js|ts))`/g)].map((match) => match[1].split("/").at(-1)));
    assert.ok(listed.has(entry));
    const checked = new Set();
    function inspect(file) {
      if (checked.has(file)) return; checked.add(file);
      const source = readFileSync(new URL(file, import.meta.url), "utf8");
      const dependencies = [...source.matchAll(/(?:from\s*|import\s*|new URL\(\s*)["']\.\/([^"']+)["']/g)].map((match) => match[1]);
      for (const dependency of dependencies) { assert.ok(listed.has(dependency), `${file} 需要一并部署 ${dependency}`); inspect(dependency); }
    }
    inspect(entry);
  });
}

test("继续诊断部署说明包含 Worker 与全部浏览器依赖", () => {
  const guide = readFileSync(new URL("../../docs/website-report-followup.md", import.meta.url), "utf8");
  const paragraphs = guide.split(/\r?\n\s*\r?\n/).filter((part) => part.startsWith("与现有浏览器模块一起部署") || part.startsWith("复制 [示例目录]"));
  const files = new Set([...paragraphs.join("\n").matchAll(/`([^`]+\.m?js)`/g)].map((match) => match[1]));
  for (const file of files) {
    const source = readFileSync(new URL(file, import.meta.url), "utf8");
    for (const match of source.matchAll(/(?:from\s*|import\s*|new URL\(\s*)["']\.\/([^"']+)["']/g))
      assert.ok(files.has(match[1]), `${file} 需要一并部署 ${match[1]}`);
  }
  assert.ok(files.has("file-sha256-worker.js"));
});

test("copied report example passes unwrapped data directly and preserves the report on refresh failure", async () => {
  const execute = new AsyncFunction("createAgentClient", "renderReport", "document", html);
  const report = { textContent: "已有报告" }, error = { textContent: "" };
  const document = { querySelector: (selector) => selector === "#diagnosis-report" ? report : error };
  const data = { report_state: "READY", report: { status: "PARTIAL" } };
  let rendered = 0;
  await execute(() => ({ conversations: { get: async () => ({ result: data }) } }), (container, value) => {
    assert.equal(container, report); assert.equal(value, data); rendered++;
  }, document);
  assert.equal(rendered, 1);
  await execute(() => ({ conversations: { get: async () => { throw new Error("报告暂时无法读取"); } } }), () => {
    assert.fail("a failed response must not render");
  }, document);
  assert.equal(report.textContent, "已有报告");
  assert.equal(error.textContent, "报告暂时无法读取");
});

test("copied create/send example retains original logical IDs after lost create and message responses", async () => {
  const created = [], messages = [];
  const client = { conversations: {
    create: async (requestId) => {
      created.push(requestId);
      if (created.length === 1) throw new Error("lost create response");
      return { conversation_id: "conversation", request_id: "server-namespaced-id" };
    },
    send: async (conversation, message) => {
      messages.push({ conversation, message: structuredClone(message) });
      if (messages.length === 1) throw new Error("lost message response");
      return { status: "ACCEPTED" };
    },
  } };
  const execute = new AsyncFunction("client", "crypto", "problemText", `${creation}\nreturn submitProblem;`);
  const submit = await execute(client, ids(), "只提交用户原话。");
  await assert.rejects(submit(), /lost create/);
  await assert.rejects(submit(), /lost message/);
  assert.deepEqual(await submit(), { status: "ACCEPTED" });
  assert.deepEqual(created, ["logical-request-1", "logical-request-1"]);
  assert.deepEqual(messages[0], messages[1]);
  assert.deepEqual(messages[0].message, { request_id: "logical-request-2", text: "只提交用户原话。", attachment_ids: [] });
});

const executeUpload = new AsyncFunction("client", "crypto", "file", "conversationId", "websiteUpload",
  `const selectedRunId = "selected-run";\n${upload}\nreturn { metadata, submitLogs };`);

for (const [name, contentType] of [["logs.zip", "application/zip"], ["LOGS.zip", "application/zip"],
  ["logs.tar", "application/x-tar"], ["logs.tar.gz", "application/gzip"], ["日志.tgz", "application/gzip"], ["logs.gz", "application/gzip"]]) {
  test(`copied upload example preserves legal filename and MIME: ${name}`, async () => {
    const file = new File(["bytes"], name, { type: "" });
    let hashes = 0;
    const { metadata } = await executeUpload({}, ids(), file, "conversation", {
      sha256: async (actual) => { hashes++; assert.equal(actual, file); return "a".repeat(64); },
    });
    assert.deepEqual(metadata, { request_id: "logical-request-1", name, content_type: contentType,
      declared_size: file.size, declared_sha256: "a".repeat(64) });
    assert.equal(hashes, 1);
  });
}

for (const file of [{ name: "LOGS.ZIP", size: 10 }, { name: "logs.TAR.gz", size: 10 },
  { name: "logs.txt", size: 10 }, { name: "logs.zip", size: 0 }, { name: "logs.zip", size: 2684354561 }]) {
  test(`copied upload example rejects invalid name/size before hashing: ${file.name}/${file.size}`, async () => {
    let hashes = 0;
    await assert.rejects(executeUpload({}, ids(), file, "conversation", {
      sha256: async () => { hashes++; return "a".repeat(64); },
    }));
    assert.equal(hashes, 0);
  });
}

test("copied upload retry reuses the hash, reservation, Blob and attachment-only message", async () => {
  const file = new File(["bytes"], "logs.zip");
  const prepared = { attachment: { attachment_id: "attachment" }, upload: {} };
  let hashes = 0, reservations = 0, uploads = 0;
  const messages = [];
  const client = { attachments: {
    prepare: async () => { reservations++; return prepared; },
    upload: async (reservation, body) => {
      assert.equal(reservation, prepared); assert.equal(body, file); uploads++;
      return { attachment_id: "attachment", status: "READY" };
    },
  }, conversations: {
    send: async (conversation, message) => {
      assert.equal(conversation, "conversation"); messages.push(structuredClone(message));
      if (messages.length === 1) throw new Error("lost attachment message response");
      return { status: "ACCEPTED" };
    },
  } };
  const { submitLogs } = await executeUpload(client, ids(), file, "conversation", {
    sha256: async () => { hashes++; return "a".repeat(64); },
  });
  await assert.rejects(submitLogs(), /lost attachment message/);
  assert.deepEqual(await submitLogs(), { status: "ACCEPTED" });
  assert.deepEqual([hashes, reservations, uploads], [1, 1, 1]);
  assert.deepEqual(messages, Array(2).fill({ request_id: "logical-request-2", attachment_ids: ["attachment"], target_run_id: "selected-run" }));
});
