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

test("copied report example passes unwrapped data directly and preserves the report on refresh failure", async () => {
  const execute = new AsyncFunction("createAgentClient", "renderReport", "document", html);
  const report = { textContent: "已有报告" }, error = { textContent: "" };
  const document = { querySelector: (selector) => selector === "#diagnosis-report" ? report : error };
  const data = { report_state: "READY", report: { status: "PARTIAL" } };
  let rendered = 0;
  await execute(() => ({ getReport: async () => data }), (container, value) => {
    assert.equal(container, report); assert.equal(value, data); rendered++;
  }, document);
  assert.equal(rendered, 1);
  await execute(() => ({ getReport: async () => { throw new Error("报告暂时无法读取"); } }), () => {
    assert.fail("a failed response must not render");
  }, document);
  assert.equal(report.textContent, "已有报告");
  assert.equal(error.textContent, "报告暂时无法读取");
});

test("copied create/send example retains original logical IDs after lost create and message responses", async () => {
  const created = [], messages = [];
  const client = {
    createConversation: async (requestId) => {
      created.push(requestId);
      if (created.length === 1) throw new Error("lost create response");
      return { conversation_id: "conversation", request_id: "server-namespaced-id" };
    },
    sendMessage: async (conversation, message) => {
      messages.push({ conversation, message: structuredClone(message) });
      if (messages.length === 1) throw new Error("lost message response");
      return { status: "ACCEPTED" };
    },
  };
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
  `${upload}\nreturn { metadata, submitLogs };`);

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
  const client = {
    prepareAttachment: async () => { reservations++; return prepared; },
    uploadAttachment: async (reservation, body) => {
      assert.equal(reservation, prepared); assert.equal(body, file); uploads++;
      return { attachment_id: "attachment", status: "READY" };
    },
    sendMessage: async (conversation, message) => {
      assert.equal(conversation, "conversation"); messages.push(structuredClone(message));
      if (messages.length === 1) throw new Error("lost attachment message response");
      return { status: "ACCEPTED" };
    },
  };
  const { submitLogs } = await executeUpload(client, ids(), file, "conversation", {
    sha256: async () => { hashes++; return "a".repeat(64); },
  });
  await assert.rejects(submitLogs(), /lost attachment message/);
  assert.deepEqual(await submitLogs(), { status: "ACCEPTED" });
  assert.deepEqual([hashes, reservations, uploads], [1, 1, 1]);
  assert.deepEqual(messages, Array(2).fill({ request_id: "logical-request-2", attachment_ids: ["attachment"] }));
});
