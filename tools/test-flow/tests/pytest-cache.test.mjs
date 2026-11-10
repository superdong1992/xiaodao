import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import * as actions from "../lib/actions.mjs";
import { removeTreeWritable } from "../lib/util.mjs";

function configureDriver(t, launcher) {
  for (const name of ["TEST_FLOW_PYTHON", "TEST_FLOW_LOGPARSE_PYTHON"]) {
    const previous = process.env[name];
    process.env[name] = launcher;
    t.after(() => {
      if (previous === undefined) delete process.env[name];
      else process.env[name] = previous;
    });
  }
}

function pytestDriver(root) {
  const launcher = path.join(root, "fake-python.cjs");
  // Exercise the real process/environment/cleanup path without requiring Python
  // packages, a database, or a model in the Node framework suite.
  fs.writeFileSync(launcher, `#!${process.execPath}
const fs = require("node:fs");
const path = require("node:path");
const args = process.argv.slice(2);
if (args[0] === "-c") {
  if (args[1].includes("importlib.metadata")) {
    console.log(JSON.stringify({ executable: __filename, python_version: "3.12.13",
      version_info: [3, 12, 13], packages: { pytest: "9.0.2" }, sys_path: [] }));
  }
  process.exit(0);
}
const junit = args.find(value => value.startsWith("--junitxml=")).slice(11);
const basetemp = args.find(value => value.startsWith("--basetemp=")).slice(11);
const cache = process.env.PYTHONPYCACHEPREFIX;
const sentinel = path.join(cache, "compiled-module.sentinel");
const observation = { cache, basetemp, args, pid: process.pid,
  cache_exists: fs.existsSync(sentinel), basetemp_exists: fs.existsSync(basetemp),
  no_user_site: process.env.PYTHONNOUSERSITE };
fs.mkdirSync(cache, { recursive: true });
fs.mkdirSync(basetemp, { recursive: true });
fs.writeFileSync(sentinel, "bytecode fixture");
fs.writeFileSync(path.join(basetemp, "fixture-state"), "gate-local");
fs.writeFileSync(path.join(path.dirname(junit), "driver-observation.json"), JSON.stringify(observation));
fs.writeFileSync(junit, '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="fixture" /></testsuite></testsuites>');
console.log("1 passed");
`, { mode: 0o755 });
  return launcher;
}

async function runGate(root, attempt, gateId, { kind = "deterministic", real = false } = {}) {
  const stage = { id: "cache-regression", kind, timeout_seconds: 10 };
  const attemptRoot = path.join(root, attempt);
  const result = await actions.executeGate({
    repoRoot: root,
    attemptRoot,
    runtimeProfile: { version: "3.12" },
    options: { logparseSource: root },
    policies: { raw_log_file_limit_bytes: 1024 * 1024, poll_milliseconds: 50 },
  }, stage, gateId, {
    kind: "pytest", selectors: ["test_fixture.py"], min_passed: 1,
    skip_policy: "forbid-all-skipped",
    ...(real ? { environment_profile: "real-logparse" } : {}),
  });
  assert.equal(result.status, "PASS", JSON.stringify(result));
  const evidence = path.join(attemptRoot, "payload", "stages", stage.id, "gates", gateId);
  const observation = JSON.parse(fs.readFileSync(path.join(evidence, "driver-observation.json"), "utf8"));
  assert.equal(observation.no_user_site, "1");
  assert.deepEqual(observation.args.slice(0, 6), ["-m", "pytest", "test_fixture.py", "-q", "-p", "no:cacheprovider"]);
  assert.equal(fs.existsSync(observation.basetemp), false);
  assert.equal(fs.existsSync(path.join(attemptRoot, "scratch", `${stage.id}--${gateId}`)), false);
  return observation;
}

test("POSIX pytest Gates reuse only attempt bytecode and retain gate-local scratch cleanup", {
  skip: process.platform === "win32" && "POSIX process boundary",
}, async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "pytest-cache-regression-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const launcher = pytestDriver(root);
  configureDriver(t, launcher);

  const first = await runGate(root, "attempt-1", "unit");
  assert.equal(first.cache_exists, false);
  assert.equal(fs.existsSync(first.cache), true, "Gate cleanup must preserve attempt bytecode");
  const second = await runGate(root, "attempt-1", "integration");
  assert.equal(second.cache, first.cache);
  assert.equal(second.cache_exists, true);
  assert.notEqual(second.pid, first.pid);
  assert.notEqual(second.basetemp, first.basetemp);
  assert.equal(second.basetemp_exists, false);

  const nextAttempt = await runGate(root, "attempt-2", "unit");
  assert.notEqual(nextAttempt.cache, first.cache);
  assert.equal(nextAttempt.cache_exists, false);
  assert.equal(nextAttempt.basetemp_exists, false);

  // The existing attempt-finalization boundary removes the shared scratch,
  // while evidence and another attempt's cache remain untouched.
  removeTreeWritable(path.join(root, "attempt-1", "scratch"), path.join(root, "attempt-1"));
  assert.equal(fs.existsSync(first.cache), false);
  assert.equal(fs.existsSync(path.join(root, "attempt-1", "payload")), true);
  assert.equal(fs.existsSync(nextAttempt.cache), true);
});

test("real profiles and non-deterministic pytest Gates retain independent disposable caches", {
  skip: process.platform === "win32" && "POSIX process boundary",
}, async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "pytest-cache-isolation-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const launcher = pytestDriver(root);
  configureDriver(t, launcher);
  const first = await runGate(root, "attempt-1", "real-1", { real: true });
  const second = await runGate(root, "attempt-1", "real-2", { real: true });
  const nonDeterministic = await runGate(root, "attempt-1", "journey", { kind: "real-journey" });
  assert.notEqual(first.cache, second.cache);
  for (const observation of [first, second, nonDeterministic]) {
    assert.equal(observation.cache_exists, false);
    assert.equal(fs.existsSync(observation.cache), false);
    assert.equal(observation.cache, path.join(path.dirname(observation.basetemp), "pycache"));
  }
});

test("Windows and isolated-agent pytest bytecode remain inside each Gate scratch", () => {
  const scratch = path.resolve("gate-scratch");
  const attemptRoot = path.resolve("attempt");
  for (const override of [
    { platform: "win32" },
    { real: true },
    { isolatedAgent: true },
    { stageKind: "isolated-real" },
  ]) {
    assert.equal(actions.pytestBytecodeCachePath({
      platform: "linux", stageKind: "deterministic", attemptRoot, scratch, ...override,
    }), path.join(scratch, "pycache"));
  }
});
