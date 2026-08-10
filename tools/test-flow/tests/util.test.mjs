import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  commandExists,
  compactScratchPath,
  createScratchBinding,
  removeScratchBinding,
  resolveCommand,
} from "../lib/util.mjs";

test("explicit executable paths are resolved without a PATH lookup", () => {
  const expected = fs.realpathSync(process.execPath);
  assert.equal(resolveCommand(process.execPath), expected);
  assert.equal(commandExists(process.execPath), true);
});

test("missing explicit executable paths do not fall through to a PATH lookup", () => {
  const missing = path.join(path.dirname(process.execPath), "test-flow-command-that-does-not-exist.exe");
  assert.equal(resolveCommand(missing), null);
  assert.equal(commandExists(missing), false);
});

test("gate scratch paths stay inside one compact deterministic namespace", () => {
  const root = path.resolve("test-flow-attempt");
  const first = compactScratchPath(root, "deterministic.full--det.unit");
  assert.equal(path.dirname(first), path.join(root, "scratch"));
  assert.match(path.basename(first), /^[a-f0-9]{12}$/);
  assert.equal(first, compactScratchPath(root, "deterministic.full--det.unit"));
  assert.notEqual(first, compactScratchPath(root, "deterministic.full--det.integration"));
});

test("scratch bindings are attempt-contained except for the bounded Windows short root", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-scratch-alias-"));
  try {
    const attemptRoot = path.join(root, "evidence", "run");
    fs.mkdirSync(attemptRoot, { recursive: true });
    const linux = createScratchBinding(root, attemptRoot, "deterministic.full--det.unit", "linux");
    assert.equal(path.dirname(linux.path), path.join(attemptRoot, "scratch"));
    assert.equal(linux.external_root, null);
    removeScratchBinding(linux);
    assert.equal(fs.existsSync(linux.path), false);

    const windows = createScratchBinding(root, attemptRoot, "deterministic.full--det.unit", "win32");
    assert.equal(path.dirname(windows.path), path.join(root, ".tmp", "s"));
    assert.match(path.basename(windows.path), /^[a-f0-9]{12}$/);
    assert.ok(windows.path.length < compactScratchPath(attemptRoot, "deterministic.full--det.unit").length);
    fs.writeFileSync(path.join(windows.path, "payload"), "temporary");
    removeScratchBinding(windows);
    assert.equal(fs.existsSync(windows.path), false);
    assert.equal(fs.existsSync(path.join(root, ".tmp", "s")), false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
