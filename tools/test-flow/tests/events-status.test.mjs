import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { EventWriter, validateEventFile } from "../lib/events.mjs";
import { classifyRun, performanceThreshold } from "../lib/status.mjs";

test("NDJSON event stream is flushed, sequenced and rejects a partial terminal line", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-events-"));
  try {
    const writer = new EventWriter({ attemptRoot: root, runId: "run-event", producerId: "orchestrator", producerType: "orchestrator" });
    writer.write("stage.started", { stageId: "deterministic.full", data: { kind: "deterministic" } });
    assert.ok(fs.statSync(writer.filePath).size > 0, "event must be visible before close");
    writer.write("stage.completed", { stageId: "deterministic.full", data: { status: "PASS" } });
    writer.close();
    assert.deepEqual(validateEventFile(writer.filePath), {
      status: "PASS",
      event_count: 2,
      producer_id: "orchestrator",
      producer_type: "orchestrator",
      run_id: "run-event",
    });
    fs.appendFileSync(writer.filePath, "{\"schema_version\":1");
    assert.throws(() => validateEventFile(writer.filePath), /incomplete final line/);
    assert.equal(validateEventFile(writer.filePath, { allowPartialTail: true }).event_count, 2);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("event envelope refuses sensitive keys but permits ordinary values containing the word token", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-events-sensitive-"));
  try {
    const writer = new EventWriter({ attemptRoot: root, runId: "run-event", producerId: "host", producerType: "host" });
    writer.write("stage.progress", { data: { message_code: "token_count_updated" } });
    assert.throws(() => writer.write("stage.progress", { data: { auth_token: "redacted" } }), /Sensitive event data key/);
    writer.close();
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("status axes preserve functional PASS when cleanup fails", () => {
  assert.deepEqual(classifyRun({ functional: "PASS", performance: "PASS", operation: "ERROR" }), { overall: "ERROR", exit_code: 3 });
  assert.deepEqual(classifyRun({ functional: "PASS", performance: "SLOW", operation: "PASS" }), { overall: "PASS_WITH_WARNINGS", exit_code: 0 });
  assert.deepEqual(classifyRun({ functional: "INCONCLUSIVE", performance: "NOT_RUN", operation: "PASS" }), { overall: "BLOCKED", exit_code: 2 });
});

test("performance baseline uses last ten robust samples and calibrates only after five", () => {
  assert.equal(performanceThreshold([1, 2, 3, 4]), null);
  const threshold = performanceThreshold([100, 101, 99, 100, 100, 101, 100, 99, 100, 100, 10_000], { external: true });
  assert.equal(threshold.sample_count, 10);
  assert.equal(threshold.median_seconds, 100);
  assert.equal(threshold.threshold_seconds, 130);
});
