import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { createAttempt, finalizeAttempt, verifyVerdict } from "../lib/evidence.mjs";
import { EventWriter } from "../lib/events.mjs";
import { removeTreeWritable } from "../lib/util.mjs";

function candidate(runId) {
  return {
    schema_version: 1,
    run_id: runId,
    track: "dev",
    goal: "dev.default",
    functional_status: "PASS",
    performance_status: "PASS",
    operation_status: "PASS",
    failure_domain: null,
    stages: [{ id: "deterministic.full", kind: "deterministic", status: "PASS", producer_identity: "a", proof_identity: "b" }],
    source: { head: "a".repeat(40), clean: true },
  };
}

function closeMinimalStream(attemptRoot, runId) {
  const writer = new EventWriter({ attemptRoot, runId, producerId: "orchestrator", producerType: "orchestrator" });
  writer.write("run.created", { data: { track: "dev" } });
  writer.close();
}

test("cleanup failure keeps functional evidence reusable but commits overall ERROR", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-evidence-"));
  try {
    const attemptRoot = createAttempt({ evidenceRoot: root, runId: "run-cleanup" });
    closeMinimalStream(attemptRoot, "run-cleanup");
    const verdict = await finalizeAttempt({
      attemptRoot,
      candidate: candidate("run-cleanup"),
      resourcePolicy: async () => ({ schema_version: 1, status: "ERROR", code: "CLEANUP_PARTIAL", remaining: [{ kind: "volume", name: "kept" }] }),
    });
    assert.equal(verdict.functional_status, "PASS");
    assert.equal(verdict.operation_status, "ERROR");
    assert.equal(verdict.overall, "ERROR");
    assert.equal(verdict.exit_code, 3);
    assert.equal(verdict.evidence_reusable, true);
    assert.equal(verifyVerdict(attemptRoot).status, "PASS");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("secret hit preserves resources and can never leave a reusable PASS", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-secret-"));
  try {
    const attemptRoot = createAttempt({ evidenceRoot: root, runId: "run-secret" });
    closeMinimalStream(attemptRoot, "run-secret");
    fs.writeFileSync(path.join(attemptRoot, "payload", "logs", "leak.log"), "sk-ant-abcdefghijklmnopqrstuv\n", "utf8");
    let preserveValue = null;
    const verdict = await finalizeAttempt({
      attemptRoot,
      candidate: candidate("run-secret"),
      resourcePolicy: async ({ preserve }) => { preserveValue = preserve; return { schema_version: 1, status: "PASS", policy: "PRESERVE", remaining: [] }; },
    });
    assert.equal(preserveValue, true);
    assert.equal(verdict.overall, "ERROR");
    assert.equal(verdict.failure_domain, "SECURITY");
    assert.equal(verdict.evidence_reusable, false);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("payload modification after verdict invalidates the verdict", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-tamper-"));
  try {
    const attemptRoot = createAttempt({ evidenceRoot: root, runId: "run-tamper" });
    closeMinimalStream(attemptRoot, "run-tamper");
    await finalizeAttempt({ attemptRoot, candidate: candidate("run-tamper"), resourcePolicy: async () => ({ schema_version: 1, status: "PASS", remaining: [] }) });
    assert.equal(verifyVerdict(attemptRoot).status, "PASS");
    fs.appendFileSync(path.join(attemptRoot, "payload", "candidate-result.json"), " ");
    assert.equal(verifyVerdict(attemptRoot).status, "INVALID");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("an interrupted attempt without verdict is UNFINALIZED, never PASS", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-unfinalized-"));
  try {
    const attemptRoot = createAttempt({ evidenceRoot: root, runId: "run-unfinalized" });
    assert.deepEqual(verifyVerdict(attemptRoot), { status: "UNFINALIZED", verdict: null });
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("an incomplete required event stream commits an auditable ERROR, never PASS", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-event-error-"));
  try {
    const attemptRoot = createAttempt({ evidenceRoot: root, runId: "run-event-error" });
    closeMinimalStream(attemptRoot, "run-event-error");
    fs.appendFileSync(path.join(attemptRoot, "payload", "events", "orchestrator.ndjson"), "{");
    const verdict = await finalizeAttempt({
      attemptRoot,
      candidate: candidate("run-event-error"),
      resourcePolicy: async () => ({ schema_version: 1, status: "PASS", policy: "PRESERVE", remaining: [] }),
    });
    assert.equal(verdict.overall, "ERROR");
    assert.equal(verdict.failure_domain, "HARNESS");
    assert.equal(verdict.evidence_reusable, false);
    assert.equal(verifyVerdict(attemptRoot).status, "PASS");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("attempt-scoped cleanup removes nested read-only test trees", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-cleanup-"));
  try {
    const scratch = path.join(root, "scratch");
    const nested = path.join(scratch, "workspace", "inputs", "tree");
    fs.mkdirSync(nested, { recursive: true });
    fs.writeFileSync(path.join(nested, "payload.txt"), "immutable\n", "utf8");
    fs.chmodSync(nested, 0o500);
    fs.chmodSync(path.dirname(nested), 0o500);
    removeTreeWritable(scratch, root);
    assert.equal(fs.existsSync(scratch), false);
  } finally {
    if (fs.existsSync(root)) {
      fs.chmodSync(root, 0o700);
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
});
