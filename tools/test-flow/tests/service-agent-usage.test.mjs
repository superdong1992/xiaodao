import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { resolvePythonTestRuntime } from "../lib/util.mjs";

const REPO_ROOT = path.resolve(import.meta.dirname, "..", "..", "..");
const AUDIT = path.join(
  REPO_ROOT,
  "tools",
  "test-flow",
  "runtime-support",
  "audit_service_agent_usage.py",
);
const MODEL = "deepseek-v4-flash[1m]";
const JOB_ID = "11111111-1111-4111-8111-111111111111";
const SESSION_ID = "22222222-2222-4222-8222-222222222222";

function writeJob(root, events) {
  const jobRoot = path.join(root, "jobs", JOB_ID);
  fs.mkdirSync(jobRoot, { recursive: true });
  fs.writeFileSync(
    path.join(jobRoot, "job.json"),
    `${JSON.stringify({ job_id: JOB_ID, job_type: "DIAGNOSE" })}\n`,
  );
  fs.writeFileSync(
    path.join(jobRoot, "stdout.log"),
    `${events.map((event) => JSON.stringify(event)).join("\n")}\n`,
  );
}

function runAudit(root, overrides = {}) {
  const runtime = resolvePythonTestRuntime(REPO_ROOT);
  assert.ok(runtime, "Test Flow Python runtime is required");
  const output = path.join(root, "receipt.json");
  const result = spawnSync(runtime.command, [
    ...runtime.interpreterPrefix,
    "-I",
    AUDIT,
    "--jobs-root", path.join(root, "jobs"),
    "--output", output,
    "--model", MODEL,
    "--max-turns", String(overrides.maxTurns ?? 3),
    "--max-total-tokens", String(overrides.maxTotalTokens ?? 19),
    "--max-budget-usd", String(overrides.maxBudgetUsd ?? 0.25),
    "--hard-timeout-seconds", "1200",
  ], { cwd: REPO_ROOT, encoding: "utf8", env: process.env });
  return { output, result };
}

function multiSegmentEvents(secondSession = SESSION_ID) {
  return [
    { type: "system", subtype: "init", model: MODEL, session_id: SESSION_ID },
    { type: "assistant", message: { role: "assistant", content: [] } },
    {
      type: "result",
      subtype: "success",
      is_error: false,
      num_turns: 2,
      session_id: SESSION_ID,
      usage: { input_tokens: 10, output_tokens: 5 },
      total_cost_usd: 0.2,
    },
    {
      type: "system",
      subtype: "task_notification",
      status: "completed",
      session_id: SESSION_ID,
    },
    { type: "system", subtype: "init", model: MODEL, session_id: secondSession },
    {
      type: "result",
      subtype: "success",
      is_error: false,
      num_turns: 1,
      session_id: secondSession,
      usage: { input_tokens: 3, output_tokens: 1 },
      total_cost_usd: 0.25,
    },
  ];
}

test("service Agent usage audit accepts bounded same-session background-task continuation", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-service-usage-"));
  try {
    writeJob(root, multiSegmentEvents());
    const { output, result } = runAudit(root);
    assert.equal(result.status, 0, result.stderr);
    const receipt = JSON.parse(fs.readFileSync(output, "utf8"));
    assert.equal(receipt.status, "PASS");
    assert.deepEqual(receipt.new_job_ids, [JOB_ID]);
    assert.equal(receipt.invocations.length, 1);
    assert.deepEqual(receipt.invocations[0].usage, {
      input_tokens: 13,
      output_tokens: 6,
      cost_usd: 0.25,
    });
    assert.equal(receipt.invocations[0].turns, 3);
    assert.equal(receipt.invocations[0].stream_segments, 2);
    assert.equal(receipt.invocations[0].session_id, SESSION_ID);
    assert.equal(receipt.invocations[0].cost_accounting, "cumulative-terminal");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("service Agent usage audit rejects a continuation from another session", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-service-usage-"));
  try {
    writeJob(root, multiSegmentEvents("33333333-3333-4333-8333-333333333333"));
    const { result } = runAudit(root);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /MODEL_USAGE_STREAM_INVALID/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("service Agent usage audit applies caps to all continuation segments", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-service-usage-"));
  try {
    writeJob(root, multiSegmentEvents());
    const { result } = runAudit(root, { maxTurns: 2 });
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /MODEL_TURN_CAP_EXCEEDED/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
