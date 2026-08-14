import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { createAttempt } from "../lib/evidence.mjs";
import { EventWriter } from "../lib/events.mjs";
import { canonicalWindowsEnvironment, runProcess } from "../lib/process.mjs";
import { resolveCommand } from "../lib/util.mjs";
import { ISOLATED_AGENT_ENV_POLICY_VERSION } from "../runtime-support/isolated-agent-env.mjs";

function stage(id) { return { id, kind: "isolated-real" }; }
const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

async function waitForContent(filePath, pattern, timeoutMilliseconds = 5000) {
  const deadline = Date.now() + timeoutMilliseconds;
  while (Date.now() < deadline) {
    if (fs.existsSync(filePath) && pattern.test(fs.readFileSync(filePath, "utf8"))) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`PROCESS_TEST_LOG_NOT_VISIBLE:${path.basename(filePath)}`);
}

test("Windows environment canonicalization is case-insensitive and last-value-wins", () => {
  const environment = canonicalWindowsEnvironment({
    PATH: "first-path",
    Path: "second-path",
    TEST_FLOW_VALUE: "first-value",
    test_flow_value: "second-value",
    UNRELATED: "preserved",
  });
  assert.deepEqual(environment, {
    Path: "second-path",
    test_flow_value: "second-value",
    UNRELATED: "preserved",
  });
});

test("command resolution accepts an existing absolute executable", () => {
  assert.equal(resolveCommand(process.execPath), path.resolve(process.execPath));
  assert.equal(resolveCommand(path.join(os.tmpdir(), "test-flow-command-that-does-not-exist")), null);
});

test("stdout is visible while the child is still running", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-process-stream-"));
  try {
    const attempt = createAttempt({ evidenceRoot: root, runId: "run-stream" });
    const writer = new EventWriter({ attemptRoot: attempt, runId: "run-stream", producerId: "orchestrator", producerType: "orchestrator" });
    let completed = false;
    const running = runProcess({ repoRoot: REPO_ROOT, attemptRoot: attempt, stage: stage("stream"), command: process.execPath, args: ["-e", "console.log('first'); setTimeout(() => process.exit(0), 3000)"], cwd: root, hardTimeoutSeconds: 10, noProgressSeconds: null, eventWriter: writer }).finally(() => { completed = true; });
    const log = path.join(attempt, "payload", "logs", "stream.stdout.log");
    await waitForContent(log, /first/);
    assert.equal(completed, false);
    assert.equal((await running).status, "PASS");
    writer.close();
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("semantic progress extends no-progress window", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-process-progress-"));
  try {
    const attempt = createAttempt({ evidenceRoot: root, runId: "run-progress" });
    const intervalMilliseconds = process.platform === "win32" ? 750 : 100;
    const noProgressSeconds = process.platform === "win32" ? 2.5 : 0.25;
    const result = await runProcess({
      repoRoot: REPO_ROOT,
      attemptRoot: attempt,
      stage: stage("progress"),
      command: process.execPath,
      args: ["-e", `let n=0; const t=setInterval(()=>{console.log('TEST_FLOW_PROGRESS stage.progress'); if(++n===5){clearInterval(t);process.exit(0)}},${intervalMilliseconds})`],
      cwd: root,
      hardTimeoutSeconds: process.platform === "win32" ? 10 : 2,
      noProgressSeconds,
    });
    assert.equal(result.status, "PASS");
    assert.equal(result.termination, null);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("process telemetry preserves cache token components and derives their inclusive total", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-process-usage-"));
  try {
    const attempt = createAttempt({ evidenceRoot: root, runId: "run-usage" });
    const terminal = {
      type: "result",
      usage: {
        input_tokens: 11,
        output_tokens: 7,
        cache_creation_input_tokens: 13,
        cache_read_input_tokens: 17,
      },
      total_cost_usd: 0.25,
    };
    const result = await runProcess({
      repoRoot: REPO_ROOT,
      attemptRoot: attempt,
      stage: stage("usage"),
      command: process.execPath,
      args: ["-e", `console.log(${JSON.stringify(JSON.stringify(terminal))})`],
      cwd: root,
      hardTimeoutSeconds: 2,
      noProgressSeconds: null,
    });
    assert.deepEqual(result.usage, {
      schema_version: 1,
      input_tokens: 11,
      output_tokens: 7,
      cache_creation_input_tokens: 13,
      cache_read_input_tokens: 17,
      total_tokens: 48,
      cost_usd: 0.25,
    });
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("raw byte noise does not reset semantic no-progress timeout", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-process-noise-"));
  try {
    const attempt = createAttempt({ evidenceRoot: root, runId: "run-noise" });
    const result = await runProcess({
      repoRoot: REPO_ROOT,
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
    assert.equal(result.termination.kill.requested, true);
    if (process.platform === "win32") assert.equal(result.termination.kill.forced, false);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("raw log cap terminates the process and cannot produce PASS", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-process-cap-"));
  try {
    const attempt = createAttempt({ evidenceRoot: root, runId: "run-cap" });
    const result = await runProcess({
      repoRoot: REPO_ROOT,
      attemptRoot: attempt,
      stage: stage("cap"),
      command: process.execPath,
      args: ["-e", "process.stdout.write('x'.repeat(65536)); setInterval(()=>{},1000)"],
      cwd: root,
      hardTimeoutSeconds: process.platform === "win32" ? 10 : 2,
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
      repoRoot: REPO_ROOT,
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

test("isolated Agent process policy does not inherit ambient secrets", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-process-environment-"));
  const previous = process.env.TEST_FLOW_AMBIENT_SECRET_CANARY;
  process.env.TEST_FLOW_AMBIENT_SECRET_CANARY = "ambient-secret-canary";
  try {
    const attempt = createAttempt({ evidenceRoot: root, runId: "run-environment" });
    const result = await runProcess({
      repoRoot: REPO_ROOT,
      attemptRoot: attempt,
      stage: stage("environment"),
      command: process.execPath,
      args: ["-e", "console.log(JSON.stringify(Object.keys(process.env).sort()))"],
      cwd: root,
      env: { S08_REAL_AGENT_GATE: "1" },
      environmentPolicy: ISOLATED_AGENT_ENV_POLICY_VERSION,
      hardTimeoutSeconds: 2,
      noProgressSeconds: null,
    });
    assert.equal(result.status, "PASS");
    const keys = JSON.parse(fs.readFileSync(path.join(attempt, "payload", "logs", "environment.stdout.log"), "utf8"));
    assert.equal(keys.includes("TEST_FLOW_AMBIENT_SECRET_CANARY"), false);
    assert.equal(keys.includes("S08_REAL_AGENT_GATE"), true);
    if (process.platform === "win32") {
      for (const injected of ["PATHEXT", "PSEXECUTIONPOLICYPREFERENCE", "PSMODULEPATH"]) {
        assert.equal(keys.includes(injected), false);
      }
    }
  } finally {
    if (previous === undefined) delete process.env.TEST_FLOW_AMBIENT_SECRET_CANARY;
    else process.env.TEST_FLOW_AMBIENT_SECRET_CANARY = previous;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("an unknown isolated Agent environment policy fails before child execution", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-process-environment-version-"));
  try {
    const attempt = createAttempt({ evidenceRoot: root, runId: "run-environment-version" });
    await assert.rejects(() => runProcess({
      repoRoot: REPO_ROOT,
      attemptRoot: attempt,
      stage: stage("environment-version"),
      command: process.execPath,
      args: ["-e", "process.exit(0)"],
      cwd: root,
      hardTimeoutSeconds: 2,
      noProgressSeconds: null,
      environmentPolicy: "unknown-v1",
    }), (error) => error.code === "PROCESS_ENVIRONMENT_POLICY_VERSION");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
