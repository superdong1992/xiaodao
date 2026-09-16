import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { executeGate, planAffectedSelection } from "../lib/actions.mjs";

function testFile(root, relative) {
  const target = path.join(root, relative);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, "def test_example():\n    assert True\n");
}

function temporaryRepository(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-affected-selection-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return root;
}

test("whole integration selection below half of test files requires full", async (t) => {
  const root = temporaryRepository(t);
  const agentTests = ["test_intake.py", "test_intake_tolerance.py", "test_store.py"];
  for (const [directory, count] of [
    ["integration", 10],
    ["unit/runtime", 57],
    ["contracts", 121],
  ]) {
    for (let index = 0; index < count; index += 1) {
      testFile(root, `tests/deterministic/${directory}/test_${index}.py`);
    }
  }
  const agentPaths = agentTests.map((name) => `tests/deterministic/unit/agent/${name}`);
  for (const file of agentPaths) testFile(root, file);
  const changedFiles = ["src/problem_locator/runtime/diagnosis_runtime.py", ...agentPaths];
  const expectedSelection = {
    selectors: [
      "tests/deterministic/integration",
      ...agentPaths,
      "tests/deterministic/unit/runtime",
    ],
    covered_test_files: 70,
    total_test_files: 191,
    coverage: 70 / 191,
    defer_to_full: true,
  };

  await t.test("retains the complete 70-of-191 selection while deferring it", () => {
    assert.deepEqual(planAffectedSelection(root, changedFiles), expectedSelection);
  });

  await t.test("one explicitly changed integration test remains narrow", () => {
    const changedFile = "tests/deterministic/integration/test_0.py";
    assert.deepEqual(planAffectedSelection(root, [changedFile]), {
      selectors: [changedFile],
      covered_test_files: 1,
      total_test_files: 191,
      coverage: 1 / 191,
      defer_to_full: false,
    });
  });

  for (const fullInPlan of [true, false]) {
    await t.test(fullInPlan
      ? "dev.default defers the unchanged selection to its planned full suite"
      : "dev.quick without full is inconclusive and cannot claim a zero-test pass", async () => {
      const attemptRoot = path.join(root, fullInPlan ? "default-attempt" : "quick-attempt");
      const stages = [{ id: "deterministic.affected" }];
      if (fullInPlan) stages.push({ id: "deterministic.full" });
      const result = await executeGate({
        repoRoot: root,
        attemptRoot,
        changedFiles,
        plan: { stages },
      }, { id: "deterministic.affected" }, "det.affected", {
        kind: "pytest",
        selector_mode: "affected",
      });

      assert.equal(result.status, fullInPlan ? "NOT_REQUIRED" : "INCONCLUSIVE");
      assert.equal(result.code, fullInPlan
        ? "AFFECTED_SCOPE_DEFERRED_TO_FULL"
        : "AFFECTED_SCOPE_REQUIRES_FULL");
      assert.equal(result.failure_domain, fullInPlan ? null : "CONTRACT");
      assert.equal(result.elapsed_seconds, 0);
      assert.deepEqual(result.selection, expectedSelection);
      assert.deepEqual(result.pytest, {
        schema_version: 2,
        tests: 0,
        passed: 0,
        failures: 0,
        errors: 0,
        skipped: 0,
        executed: 0,
        not_required: fullInPlan,
      });
      const summaryPath = path.join(attemptRoot, "payload", "stages", "deterministic.affected",
        "gates", "det.affected", "pytest-summary.json");
      assert.deepEqual(JSON.parse(fs.readFileSync(summaryPath, "utf8")), result.pytest);
    });
  }
});

test("the existing half-of-files boundary still defers without integration", (t) => {
  const root = temporaryRepository(t);
  const files = ["a", "b", "c", "d"].map((name) => `tests/deterministic/unit/test_${name}.py`);
  for (const file of files) testFile(root, file);
  assert.deepEqual(planAffectedSelection(root, files.slice(0, 2)), {
    selectors: files.slice(0, 2),
    covered_test_files: 2,
    total_test_files: 4,
    coverage: 0.5,
    defer_to_full: true,
  });
  assert.equal(planAffectedSelection(root, files.slice(0, 1)).defer_to_full, false);
});
