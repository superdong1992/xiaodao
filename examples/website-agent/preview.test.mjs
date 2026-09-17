import assert from "node:assert/strict";
import { once } from "node:events";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createPreviewServer, previewSamples } from "./preview.mjs";

test("offline preview covers every public report state and format with complete envelopes", () => {
  const samples = previewSamples();
  const required = JSON.parse(readFileSync(new URL("../../schemas/v2/user-result.schema.json", import.meta.url))).required;
  assert.deepEqual(new Set(samples.map((sample) => sample.response.data.report_state)), new Set(["READY", "PENDING", "UNAVAILABLE"]));
  assert.deepEqual(new Set(samples.filter((sample) => sample.response.data.report_state === "READY").map((sample) => sample.response.data.format)),
    new Set(["problem-locator-diagnosis-v3", "markdown", "generic-v1"]));
  for (const { response } of samples) {
    assert.equal(response.ok, true);
    assert.equal(response.error, null);
    assert.equal(Object.keys(response.data).length, 13);
    if (response.data.format === "problem-locator-diagnosis-v3") {
      assert.deepEqual(Object.keys(response.data.report).sort(), [...required].sort());
    }
  }
  const unknown = samples.find((sample) => sample.response.data.failure?.details.some((item) => item.actual === "UNKNOWN"));
  assert.equal(unknown.response.data.report_state, "READY");
  assert.ok(unknown.response.data.report);
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
