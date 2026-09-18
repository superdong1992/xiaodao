import assert from "node:assert/strict";
import { once } from "node:events";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createPreviewServer, previewSamples } from "./preview.mjs";
import { createPreviewApi } from "./preview-model.js";
import { createAgentClient } from "./browser-client.js";

test("offline preview covers every public report state and format with complete envelopes", () => {
  const samples = previewSamples();
  const required = JSON.parse(readFileSync(new URL("../../schemas/v2/user-result.schema.json", import.meta.url))).required;
  assert.deepEqual(new Set(samples.map((sample) => sample.response.data.report_state)), new Set(["READY", "PENDING", "UNAVAILABLE"]));
  assert.deepEqual(new Set(samples.filter((sample) => sample.response.data.report_state === "READY").map((sample) => sample.response.data.result.format)),
    new Set(["problem-locator-diagnosis-v3", "markdown", "generic-v1"]));
  for (const { response } of samples) {
    assert.equal(response.ok, true);
    assert.equal(response.error, null);
    assert.equal(response.data.schema_version, 3);
    assert.deepEqual(response.data.included, ["history", "report", "artifacts"]);
    assert.equal(response.data.result.report_state, response.data.report_state);
    if (response.data.result.format === "problem-locator-diagnosis-v3") {
      assert.deepEqual(Object.keys(response.data.result.report).sort(), [...required].sort());
    }
  }
  const unknown = samples.find((sample) => sample.response.data.failure?.details.some((item) => item.actual === "UNKNOWN"));
  assert.equal(unknown.response.data.report_state, "READY");
  assert.ok(unknown.response.data.result.report);
});

test("offline management preview supports history, rename, stop, another run and deletion through the browser SDK", async () => {
  const client = createAgentClient({ fetchImpl: createPreviewApi(previewSamples()) });
  const directory = await client.conversations.list(), item = directory.items.find((value) => value.title === "暂无法确定");
  assert.ok(item.current_run.run_id);
  const id = item.conversation_id, original = await client.conversations.get(id), oldRun = original.selected_run_id;
  await client.conversations.rename(id, "我的故障定位");
  assert.equal((await client.conversations.get(id)).title, "我的故障定位");
  const receipt = await client.conversations.send(id, { request_id: "new-run", text: "请用新日志重查", attachment_ids: [] });
  assert.notEqual(receipt.run_id, oldRun);
  const next = await client.conversations.get(id, { history_limit: 2 });
  assert.equal(next.current_run.ordinal, 2); assert.equal(next.result.report_state, "PENDING");
  assert.ok(next.history_next_cursor); assert.equal(next.history.length, 2);
  const older = await client.conversations.get(id, { include: ["history"], history_limit: 2, history_before: next.history_next_cursor });
  assert.equal(older.history.length, 2); assert.equal(older.result, null);
  const historical = await client.conversations.get(id, { include: ["report"], run_id: oldRun });
  assert.equal(historical.result.report_state, "READY"); assert.equal(historical.current_run.run_id, receipt.run_id);
  const stop = { request_id: "stop-new", run_id: receipt.run_id };
  assert.equal((await client.conversations.stop(id, stop)).status, "CANCELLED");
  assert.equal((await client.conversations.get(id)).capabilities.can_rediagnose, true);
  assert.equal((await client.conversations.delete(id)).status, "DELETED");
  await assert.rejects(client.conversations.get(id), /不存在或已删除/);
  assert.equal((await client.conversations.list()).items.length, directory.items.length - 1);
});

test("offline preview serves only explicit local assets and never exposes API or repository files", async () => {
  const server = createPreviewServer().listen(0, "127.0.0.1");
  await once(server, "listening");
  const url = `http://127.0.0.1:${server.address().port}`;
  try {
    for (const route of ["/", "/preview.js", "/preview.css", "/report-view.js", "/report-view.css", "/sample-data.json"]) {
      const response = await fetch(url + route);
      assert.equal(response.status, 200);
      assert.match(response.headers.get("content-security-policy"), /connect-src 'self'/);
      assert.equal(response.headers.get("x-content-type-options"), "nosniff");
      assert.ok((await response.text()).length > 0);
    }
    for (const route of ["/.git/config", "/README.md", "/server.ts", "/api/agent/conversations", "/sample-data.json?url=http://outside.invalid"]) {
      const response = await fetch(url + route);
      assert.equal(response.status, 404);
      await response.text();
    }
    const write = await fetch(url + "/", { method: "POST", body: "ignored" });
    assert.equal(write.status, 404);
    await write.text();
  } finally {
    server.close();
    await once(server, "close");
  }
});
