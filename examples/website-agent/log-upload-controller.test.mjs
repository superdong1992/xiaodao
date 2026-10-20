import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import { createLogUploadController } from "./log-upload-controller.js";

for (const [name, contentType] of [
  ["application.log", "text/plain"], ["application.txt", "text/plain"],
  ["events.json", "application/json"], ["events.jsonl", "application/x-ndjson"],
  ["events.ndjson", "application/x-ndjson"], ["logs.zip", "application/zip"],
]) {
  test(`uploads the original selected file with canonical metadata: ${name}`, async () => {
    const raw = new TextEncoder().encode("\ufeff事件=请求超时\n");
    const file = new File([raw], name, { type: "application/octet-stream" });
    let serial = 0, reservation, uploaded;
    const controller = createLogUploadController({
      conversationId: "conversation", makeRequestId: () => `request-${++serial}`,
      sha256: async (value) => createHash("sha256").update(new Uint8Array(await value.arrayBuffer())).digest("hex"),
      client: { attachments: {
        prepare: async (cid, body) => {
          assert.equal(cid, "conversation");
          assert.equal(body.name, name);
          assert.equal(body.content_type, contentType);
          assert.equal(body.declared_size, raw.length);
          assert.equal(body.declared_sha256, createHash("sha256").update(raw).digest("hex"));
          reservation = { attachment: { attachment_id: "selected-log" } };
          return reservation;
        },
        upload: async (prepared, value) => {
          assert.equal(prepared, reservation);
          assert.equal(value, file);
          uploaded = new Uint8Array(await value.arrayBuffer());
        },
      } },
    });
    await controller.uploadFiles([file]);
    assert.deepEqual(uploaded, raw);
    assert.deepEqual(controller.attachmentIds(), ["selected-log"]);
    assert.equal(controller.getState().uploads[0].status, "READY");
    controller.destroy();
  });
}
