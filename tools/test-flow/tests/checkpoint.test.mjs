import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { createCheckpoint, extractCheckpointSourceArchive, restoreCheckpoint, verifyCheckpoint } from "../lib/checkpoint.mjs";

const QUIESCENT = {
  status: "PASS",
  service_stopped: true,
  running_jobs: 0,
  queued_jobs: 0,
  active_workers: 0,
  temporary_workspaces: 0,
  state_validation: "PASS",
};

function writableTree(root) {
  if (!fs.existsSync(root)) return;
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const absolute = path.join(root, entry.name);
    if (entry.isDirectory()) writableTree(absolute);
    try { fs.chmodSync(absolute, entry.isDirectory() ? 0o700 : 0o600); } catch {}
  }
  try { fs.chmodSync(root, 0o700); } catch {}
}

test("sealed checkpoint restores into a new empty root with the same portable digest", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-checkpoint-"));
  try {
    const state = path.join(root, "state");
    fs.mkdirSync(path.join(state, "cases"), { recursive: true });
    fs.writeFileSync(path.join(state, "state.json"), "{\"schema_version\":2}\n");
    fs.writeFileSync(path.join(state, "cases", "one.json"), "{\"status\":\"WAITING_INPUT\"}\n");
    const identity = { producer_identity: "producer", proof_identity: "proof" };
    const checkpoint = createCheckpoint({
      stateRoot: state,
      checkpointsRoot: path.join(root, "checkpoints"),
      stageId: "journey.cross-job.route",
      continuation: { schema_version: 1, next_stage: "journey.cross-job.upload" },
      identity,
      quiescenceReceipt: QUIESCENT,
    });
    assert.equal(verifyCheckpoint(checkpoint.path).status, "PASS");
    const target = path.join(root, "new-empty-data-root");
    fs.mkdirSync(target);
    const restored = restoreCheckpoint({ checkpointRoot: checkpoint.path, targetRoot: target, currentIdentity: identity });
    assert.equal(restored.portable_digest, checkpoint.portable_digest);
    assert.equal(fs.readFileSync(path.join(target, "cases", "one.json"), "utf8"), "{\"status\":\"WAITING_INPUT\"}\n");
  } finally { writableTree(root); fs.rmSync(root, { recursive: true, force: true }); }
});

test("container-produced checkpoint source archives extract only into an empty target", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-checkpoint-source-"));
  try {
    const state = path.join(root, "state");
    fs.mkdirSync(path.join(state, "tmp", "workspaces"), { recursive: true });
    fs.writeFileSync(path.join(state, "state.json"), "{\"schema_version\":2}\n");
    const checkpoint = createCheckpoint({
      stateRoot: state,
      checkpointsRoot: path.join(root, "checkpoints"),
      stageId: "journey.cross-job.route",
      continuation: {},
      identity: {},
      quiescenceReceipt: QUIESCENT,
    });
    const target = path.join(root, "extracted");
    const result = extractCheckpointSourceArchive({
      archivePath: path.join(checkpoint.path, "data-root.tar"),
      targetRoot: target,
    });
    assert.equal(result.status, "PASS");
    assert.equal(result.portable_digest, checkpoint.portable_digest);
    assert.equal(fs.readdirSync(path.join(target, "tmp", "workspaces")).length, 0);
    assert.throws(() => extractCheckpointSourceArchive({
      archivePath: path.join(checkpoint.path, "data-root.tar"),
      targetRoot: target,
    }), /CHECKPOINT_SOURCE_TARGET_NOT_EMPTY/);
  } finally { writableTree(root); fs.rmSync(root, { recursive: true, force: true }); }
});

test("checkpoint extraction populates nested read-only artifact directories before restoring modes", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-checkpoint-read-only-"));
  try {
    const state = path.join(root, "state");
    const tree = path.join(state, "resources", "cases", "case-1", "artifacts", "artifact-1", "tree");
    const payload = path.join(tree, "payload");
    fs.mkdirSync(payload, { recursive: true });
    const result = path.join(payload, "result.json");
    fs.writeFileSync(result, "{\"status\":\"PASS\"}\n");
    fs.chmodSync(result, 0o444);
    fs.chmodSync(payload, 0o555);
    fs.chmodSync(tree, 0o555);

    const checkpoint = createCheckpoint({
      stateRoot: state,
      checkpointsRoot: path.join(root, "checkpoints"),
      stageId: "journey.cross-job.diagnose",
      continuation: {},
      identity: {},
      quiescenceReceipt: QUIESCENT,
    });
    const target = path.join(root, "extracted");
    const extracted = extractCheckpointSourceArchive({
      archivePath: path.join(checkpoint.path, "data-root.tar"),
      targetRoot: target,
    });

    assert.equal(extracted.status, "PASS");
    assert.equal(extracted.portable_digest, checkpoint.portable_digest);
    const restoredModes = [
      fs.statSync(path.join(target, path.relative(state, tree))).mode & 0o777,
      fs.statSync(path.join(target, path.relative(state, payload))).mode & 0o777,
      fs.statSync(path.join(target, path.relative(state, result))).mode & 0o777,
    ];
    if (process.platform === "win32") {
      for (const mode of restoredModes) assert.equal(mode & 0o222, 0);
    } else {
      assert.deepEqual(restoredModes, [0o555, 0o555, 0o444]);
    }
    assert.equal(fs.readFileSync(path.join(target, path.relative(state, result)), "utf8"), "{\"status\":\"PASS\"}\n");
  } finally { writableTree(root); fs.rmSync(root, { recursive: true, force: true }); }
});

test("checkpoint creation rejects a non-quiescent boundary", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-checkpoint-running-"));
  try {
    const state = path.join(root, "state");
    fs.mkdirSync(state);
    assert.throws(() => createCheckpoint({
      stateRoot: state,
      checkpointsRoot: path.join(root, "checkpoints"),
      stageId: "journey.cross-job.diagnose",
      continuation: {},
      identity: {},
      quiescenceReceipt: { ...QUIESCENT, running_jobs: 1 },
    }), /CHECKPOINT_NOT_QUIESCENT/);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("tampering with archive or seal is rejected before restore", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-checkpoint-tamper-"));
  try {
    const state = path.join(root, "state");
    fs.mkdirSync(state);
    fs.writeFileSync(path.join(state, "state.json"), "{}\n");
    const checkpoint = createCheckpoint({ stateRoot: state, checkpointsRoot: path.join(root, "checkpoints"), stageId: "journey.cross-job.route", continuation: {}, identity: {}, quiescenceReceipt: QUIESCENT });
    const archive = path.join(checkpoint.path, "data-root.tar");
    fs.chmodSync(archive, 0o600);
    fs.appendFileSync(archive, "tamper");
    assert.equal(verifyCheckpoint(checkpoint.path).code, "CHECKPOINT_FILE_HASH_MISMATCH");
    assert.throws(() => restoreCheckpoint({ checkpointRoot: checkpoint.path, targetRoot: path.join(root, "target"), currentIdentity: {} }), /CHECKPOINT_FILE_HASH_MISMATCH/);
  } finally { writableTree(root); fs.rmSync(root, { recursive: true, force: true }); }
});
