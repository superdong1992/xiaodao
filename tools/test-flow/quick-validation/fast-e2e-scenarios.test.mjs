import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import {
  FAST_E2E_BASE_SCENARIOS,
  FAST_E2E_CAPABILITY_BOUNDARY_SCENARIOS,
  FAST_E2E_MARKER_TO_METHOD,
  FAST_E2E_SCENARIOS,
  deriveFastE2EV2Expectation,
  fastE2ECallHardCap,
  fastE2ENormalCallCount,
  scenarioPaths,
} from "./fast-e2e-scenarios.mjs";

const REPO_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "..",
);

test("Fast E2E uses all nine historical feasibility scenarios in one fixed order", () => {
  assert.deepEqual(FAST_E2E_BASE_SCENARIOS, [
    "api-execution-overrun",
    "client-receive-blocked",
    "insufficient-evidence",
    "server-queue-delay",
  ]);
  assert.deepEqual(FAST_E2E_CAPABILITY_BOUNDARY_SCENARIOS, [
    "deadloop-detected",
    "multiple-rpc-timeouts",
    "server-queue-five",
    "server-queue-single",
    "unrelated-log-noise",
  ]);
  assert.deepEqual(FAST_E2E_SCENARIOS, [
    "api-execution-overrun",
    "client-receive-blocked",
    "deadloop-detected",
    "insufficient-evidence",
    "multiple-rpc-timeouts",
    "server-queue-delay",
    "server-queue-five",
    "server-queue-single",
    "unrelated-log-noise",
  ]);

  const casesRoot = path.join(
    REPO_ROOT,
    "experiments",
    "rpc-skill-feasibility",
    "cases",
  );
  const actual = fs.readdirSync(casesRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => entry.name)
    .sort();
  assert.equal(FAST_E2E_SCENARIOS.length, 9);
  assert.deepEqual([...FAST_E2E_SCENARIOS].sort(), actual);

  const moduleSource = fs.readFileSync(
    fileURLToPath(new URL("./fast-e2e-scenarios.mjs", import.meta.url)),
    "utf8",
  );
  assert.equal(moduleSource.includes("tests/cases/release"), false);
  for (const scenarioId of FAST_E2E_SCENARIOS) {
    const paths = scenarioPaths(REPO_ROOT, scenarioId);
    const normalizedRoot = paths.root.replaceAll("\\", "/");
    assert.match(
      normalizedRoot,
      new RegExp(`/experiments/rpc-skill-feasibility/cases/${scenarioId}$`, "u"),
    );
    assert.equal(normalizedRoot.includes("/tests/cases/release/"), false);
    assert.equal(fs.existsSync(paths.case), true);
    assert.equal(fs.existsSync(paths.client_log), true);
    assert.equal(fs.existsSync(paths.server_log), true);
    assert.equal(JSON.parse(fs.readFileSync(paths.case, "utf8")).scenario_id, scenarioId);
  }
});

test("the four base scenarios mechanically derive their exact V2 expectations", () => {
  const expected = {
    "api-execution-overrun": {
      markers: ["API_COMPLETE"],
      terminal: "RESOLVED",
      confirmed: ["api_execution_overrun"],
    },
    "client-receive-blocked": {
      markers: ["LATE_RESPONSE"],
      terminal: "RESOLVED",
      confirmed: ["client_receive_blocked"],
    },
    "insufficient-evidence": {
      markers: [],
      terminal: "UNRESOLVED",
      confirmed: [],
    },
    "server-queue-delay": {
      markers: ["QUEUE_HISTORY"],
      terminal: "RESOLVED",
      confirmed: ["server_receive_queueing"],
    },
  };

  for (const scenarioId of FAST_E2E_BASE_SCENARIOS) {
    const expectation = deriveFastE2EV2Expectation(REPO_ROOT, scenarioId);
    assert.deepEqual(expectation.source_expected_branch_markers, expected[scenarioId].markers);
    assert.equal(expectation.expected_terminal_status, expected[scenarioId].terminal);
    assert.deepEqual(
      expectation.expected_confirmed_semantic_ids,
      expected[scenarioId].confirmed,
    );
  }
});

test("historical multiple-rpc-timeouts positively confirms API and client methods", () => {
  assert.deepEqual(FAST_E2E_MARKER_TO_METHOD, {
    API_COMPLETE: "api_execution_overrun",
    DEADLOOP_DETECTED: "api_execution_overrun",
    QUEUE_HISTORY: "server_receive_queueing",
    LATE_RESPONSE: "client_receive_blocked",
  });
  const expectation = deriveFastE2EV2Expectation(REPO_ROOT, "multiple-rpc-timeouts");
  assert.deepEqual(expectation.source_expected_branch_markers, [
    "LATE_RESPONSE",
    "API_COMPLETE",
  ]);
  assert.equal(expectation.expected_terminal_status, "RESOLVED");
  assert.deepEqual(expectation.expected_confirmed_semantic_ids, [
    "client_receive_blocked",
    "api_execution_overrun",
  ]);
});

test("insufficient evidence confirms no method and ends UNRESOLVED", () => {
  const expectation = deriveFastE2EV2Expectation(REPO_ROOT, "insufficient-evidence");
  assert.equal(expectation.source_expected_status, "INSUFFICIENT");
  assert.equal(expectation.expected_terminal_status, "UNRESOLVED");
  assert.deepEqual(expectation.expected_confirmed_semantic_ids, []);
  assert.equal(fastE2ENormalCallCount("insufficient-evidence"), 0);
  assert.equal(fastE2ECallHardCap("insufficient-evidence"), 0);
  assert.equal(fastE2ENormalCallCount("api-execution-overrun"), 2);
  assert.equal(fastE2ECallHardCap("api-execution-overrun"), 4);
});
