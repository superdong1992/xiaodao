import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { createAttempt } from "../lib/evidence.mjs";
import { EventWriter } from "../lib/events.mjs";
import { runProcess } from "../lib/process.mjs";

function stage(id) { return { id, kind: "isolated-real" }; }

test("stdout is visible while the child is still running", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-process-stream-"));
  try {
    const attempt = createAttempt({ evidenceRoot: root, runId: "run-stream" });
    const writer = new EventWriter({ attemptRoot: attempt, runId: "run-stream", producerId: "orchestrator", producerType: "orchestrator" });
    const running = runProcess({ repoRoot: root, attemptRoot: attempt, stage: stage("stream"), command: process.execPath, args: ["-e", "console.log('first'); setTimeout(() => process.exit(0), 500)"], cwd: root, hardTimeoutSeconds: 2, noProgressSeconds: null, eventWriter: writer });
    await new Promise((resolve) => setTimeout(resolve, 150));
    const log = path.join(attempt, "payload", "logs", "stream.stdout.log");
    assert.match(fs.readFileSync(log, "utf8"), /first/);
    assert.equal((await running).status, "PASS");
    writer.close();
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("semantic progress extends no-progress window", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-process-progress-"));
  try {
    const attempt = createAttempt({ evidenceRoot: root, runId: "run-progress" });
    const result = await runProcess({
      repoRoot: root,
      attemptRoot: attempt,
      stage: stage("progress"),
      command: process.execPath,
      args: ["-e", "let n=0; const t=setInterval(()=>{console.log('TEST_FLOW_PROGRESS stage.progress'); if(++n===5){clearInterval(t);process.exit(0)}},100)"],
      cwd: root,
      hardTimeoutSeconds: 2,
      noProgressSeconds: 0.25,
    });
    assert.equal(result.status, "PASS");
    assert.equal(result.termination, null);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("raw byte noise does not reset semantic no-progress timeout", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-process-noise-"));
  try {
    const attempt = createAttempt({ evidenceRoot: root, runId: "run-noise" });
    const result = await runProcess({
      repoRoot: root,
      attemptRoot: attempt,
      stage: stage("noise"),
      command: process.execPath,
      args: ["-e", "setInterval(()=>console.log('ordinary noisy bytes'),25)"],
      cwd: root,
      hardTimeoutSeconds: 2,
      noProgressSeconds: 0.2,
    });
    assert.equal(result.status, "INCONCLUSIVE");
    assert.equal(result.termination.trigger, "NO_PROGRESS");
    assert.ok(result.termination.silence_seconds >= 0.2);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("raw log cap terminates the process and cannot produce PASS", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-process-cap-"));
  try {
    const attempt = createAttempt({ evidenceRoot: root, runId: "run-cap" });
    const result = await runProcess({
      repoRoot: root,
      attemptRoot: attempt,
      stage: stage("cap"),
      command: process.execPath,
      args: ["-e", "process.stdout.write('x'.repeat(65536)); setInterval(()=>{},1000)"],
      cwd: root,
      hardTimeoutSeconds: 2,
      noProgressSeconds: null,
      rawLogLimitBytes: 1024,
    });
    assert.equal(result.status, "ERROR");
    assert.equal(result.termination.trigger, "RAW_LOG_LIMIT");
    assert.equal(result.stdout_truncated, true);
    assert.equal(fs.statSync(path.join(attempt, "payload", "logs", "cap.stdout.log")).size, 1024);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("an unknown progress allowlist version fails before child execution", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-process-version-"));
  try {
    const attempt = createAttempt({ evidenceRoot: root, runId: "run-version" });
    await assert.rejects(() => runProcess({
      repoRoot: root,
      attemptRoot: attempt,
      stage: stage("version"),
      command: process.execPath,
      args: ["-e", "process.exit(0)"],
      cwd: root,
      hardTimeoutSeconds: 2,
      noProgressSeconds: null,
      progressAllowlistVersion: "unknown-v1",
    }), (error) => error.code === "PROCESS_PROGRESS_VERSION");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
