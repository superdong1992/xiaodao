import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { evaluatePytestSummary, parseJUnitSummary, planAffectedSelection, probeLoopbackCapability } from "../lib/actions.mjs";

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

test("pytest cannot pass with zero executed tests or an all-skipped result", () => {
  assert.deepEqual(evaluatePytestSummary({ executed: 0, passed: 0, skipped: 0 }), {
    status: "FAIL",
    failure_domain: "CONTRACT",
    code: "PYTEST_NO_EXECUTED_TESTS",
  });
  assert.equal(evaluatePytestSummary({ executed: 0, passed: 0, skipped: 7 }).code, "PYTEST_NO_EXECUTED_TESTS");
});

test("pytest skip and minimum-pass policies are enforced from parsed JUnit", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-junit-"));
  try {
    const junit = path.join(root, "pytest.xml");
    fs.writeFileSync(junit, '<testsuites tests="4" failures="0" errors="0" skipped="1"></testsuites>\n');
    const summary = parseJUnitSummary(junit);
    assert.deepEqual(summary, { schema_version: 2, tests: 4, passed: 3, failures: 0, errors: 0, skipped: 1, executed: 3 });
    assert.equal(evaluatePytestSummary(summary, { minPassed: 4, skipPolicy: "allow-explicit" }).code, "PYTEST_MIN_PASSED_NOT_MET");
    assert.equal(evaluatePytestSummary(summary, { minPassed: 3, skipPolicy: "forbid" }).code, "PYTEST_SKIP_FORBIDDEN");
    assert.equal(evaluatePytestSummary(summary, { minPassed: 3, skipPolicy: "allow-explicit" }).status, "PASS");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("pytest's testsuites wrapper aggregates inner suite counters", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-junit-wrapper-"));
  try {
    const junit = path.join(root, "pytest.xml");
    fs.writeFileSync(junit, '<testsuites name="pytest tests"><testsuite name="unit" tests="2" failures="0" errors="0" skipped="0"></testsuite><testsuite name="journey" tests="3" failures="0" errors="0" skipped="1"></testsuite></testsuites>\n');
    assert.deepEqual(parseJUnitSummary(junit), {
      schema_version: 2,
      tests: 5,
      passed: 4,
      failures: 0,
      errors: 0,
      skipped: 1,
      executed: 4,
    });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
