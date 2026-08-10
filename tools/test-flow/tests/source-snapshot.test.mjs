import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  captureSourceSnapshot,
  materializeSourceSnapshot,
  verifyMaterializedSourceSnapshot,
  verifySourceSnapshot,
} from "../lib/source-snapshot.mjs";

function git(root, ...args) {
  const result = spawnSync("git", ["-C", root, ...args], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
}

test("source snapshots bind tracked modifications and untracked files before the final persistence commit", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-source-snapshot-"));
  const materializedParent = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-source-materialized-"));
  try {
    git(root, "init", "--quiet");
    fs.writeFileSync(path.join(root, ".gitignore"), "ignored.txt\n");
    fs.writeFileSync(path.join(root, "tracked.txt"), "index version\n");
    git(root, "add", ".gitignore", "tracked.txt");
    fs.writeFileSync(path.join(root, "tracked.txt"), "dirty working tree version\n");
    fs.writeFileSync(path.join(root, "untracked.txt"), "untracked release input\n");
    fs.writeFileSync(path.join(root, "ignored.txt"), "never part of release source\n");

    const snapshot = captureSourceSnapshot(root);
    assert.equal(snapshot.algorithm, "git-visible-worktree-v1");
    assert.deepEqual(snapshot.records.map((record) => record.path), [".gitignore", "tracked.txt", "untracked.txt"]);
    assert.equal(verifySourceSnapshot(root, snapshot).status, "PASS");

    const materialized = path.join(materializedParent, "repository");
    materializeSourceSnapshot(root, materialized, snapshot);
    assert.equal(verifyMaterializedSourceSnapshot(materialized, snapshot).status, "PASS");
    assert.equal(fs.readFileSync(path.join(materialized, "tracked.txt"), "utf8"), "dirty working tree version\n");

    fs.writeFileSync(path.join(root, "tracked.txt"), "drifted after planning\n");
    assert.equal(verifySourceSnapshot(root, snapshot).status, "FAIL");
    fs.writeFileSync(path.join(materialized, "extra.txt"), "not in manifest\n");
    assert.notEqual(verifyMaterializedSourceSnapshot(materialized, snapshot).status, "PASS");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(materializedParent, { recursive: true, force: true });
  }
});

test("source snapshots preserve relocatable internal symlinks and reject external targets", { skip: process.platform === "win32" }, () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-source-symlink-"));
  const materializedParent = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-source-symlink-materialized-"));
  const externalRoot = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-source-symlink-external-"));
  try {
    git(root, "init", "--quiet");
    fs.writeFileSync(path.join(root, "target.txt"), "sealed target\n");
    fs.symlinkSync("target.txt", path.join(root, "internal-link"));
    git(root, "add", "target.txt", "internal-link");

    const snapshot = captureSourceSnapshot(root);
    const materialized = path.join(materializedParent, "repository");
    materializeSourceSnapshot(root, materialized, snapshot);
    assert.equal(verifyMaterializedSourceSnapshot(materialized, snapshot).status, "PASS");
    assert.equal(fs.readFileSync(path.join(materialized, "internal-link"), "utf8"), "sealed target\n");

    const externalTarget = path.join(externalRoot, "mutable.txt");
    fs.writeFileSync(externalTarget, "outside snapshot\n");
    fs.symlinkSync(path.relative(root, externalTarget), path.join(root, "external-link"));
    git(root, "add", "external-link");
    assert.throws(() => captureSourceSnapshot(root), { code: "SOURCE_SNAPSHOT_SYMLINK_OUTSIDE_ROOT" });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(materializedParent, { recursive: true, force: true });
    fs.rmSync(externalRoot, { recursive: true, force: true });
  }
});
