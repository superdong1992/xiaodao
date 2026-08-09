import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { planAffectedSelection, probeLoopbackCapability } from "../lib/actions.mjs";

function writeTest(file) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, "def test_placeholder():\n    assert True\n");
}

test("a narrow affected selection runs before the full suite", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-affected-narrow-"));
  try {
    for (const name of ["a", "b", "c", "d"]) writeTest(path.join(root, "tests", "deterministic", "unit", `test_${name}.py`));
    const selection = planAffectedSelection(root, ["tests/deterministic/unit/test_a.py"]);
    assert.deepEqual(selection.selectors, ["tests/deterministic/unit/test_a.py"]);
    assert.equal(selection.covered_test_files, 1);
    assert.equal(selection.total_test_files, 4);
    assert.equal(selection.defer_to_full, false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("a broad affected selection is folded into the following full suite", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-affected-broad-"));
  try {
    for (const name of ["a", "b", "c", "d"]) writeTest(path.join(root, "tests", "deterministic", "unit", `test_${name}.py`));
    fs.writeFileSync(path.join(root, "tests", "deterministic", "unit", "conftest.py"), "VALUE = 1\n");
    const selection = planAffectedSelection(root, ["tests/deterministic/unit/conftest.py"]);
    assert.deepEqual(selection.selectors, ["tests/deterministic/unit"]);
    assert.equal(selection.covered_test_files, 4);
    assert.equal(selection.total_test_files, 4);
    assert.equal(selection.defer_to_full, true);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("loopback denial is classified as infrastructure BLOCKED before pytest", () => {
  const receipt = probeLoopbackCapability(
    { command: "/frozen/python", interpreterPrefix: [] },
    "/repository",
    {},
    () => ({ status: 1, signal: null, stdout: "", stderr: "PermissionError: [Errno 1] Operation not permitted" }),
  );
  assert.deepEqual(receipt, {
    schema_version: 1,
    status: "BLOCKED",
    capability: "ipv4-loopback-bind",
    exit_code: 1,
    signal: null,
    error_code: null,
    failure_code: "LOOPBACK_BIND_PERMISSION_DENIED",
  });
});
