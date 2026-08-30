import fs from "node:fs";
import path from "node:path";

export const FAST_E2E_BASE_SCENARIOS = Object.freeze([
  "api-execution-overrun",
  "client-receive-blocked",
  "insufficient-evidence",
  "server-queue-delay",
]);

export const FAST_E2E_CAPABILITY_BOUNDARY_SCENARIOS = Object.freeze([
  "deadloop-detected",
  "multiple-rpc-timeouts",
  "server-queue-five",
  "server-queue-single",
  "unrelated-log-noise",
]);

export const FAST_E2E_SCENARIOS = Object.freeze([
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

export const FAST_E2E_SEMANTIC_IDS = Object.freeze([
  "api_execution_overrun",
  "server_receive_queueing",
  "client_receive_blocked",
]);

export const FAST_E2E_MARKER_TO_METHOD = Object.freeze({
  API_COMPLETE: "api_execution_overrun",
  DEADLOOP_DETECTED: "api_execution_overrun",
  QUEUE_HISTORY: "server_receive_queueing",
  LATE_RESPONSE: "client_receive_blocked",
});

const HISTORICAL_CASES_PATH = Object.freeze([
  "experiments",
  "rpc-skill-feasibility",
  "cases",
]);

function supportedScenario(scenarioId) {
  if (!FAST_E2E_SCENARIOS.includes(scenarioId)) {
    throw new Error(`FAST_E2E_SCENARIO_UNSUPPORTED:${String(scenarioId)}`);
  }
  return scenarioId;
}

export function scenarioPaths(sourceRoot, scenarioId) {
  const id = supportedScenario(scenarioId);
  const root = path.join(path.resolve(sourceRoot), ...HISTORICAL_CASES_PATH, id);
  return Object.freeze({
    root,
    case: path.join(root, "case.json"),
    client_log: path.join(root, "raw", "client.log"),
    server_log: path.join(root, "raw", "server.log"),
  });
}

export function fastE2ENormalCallCount(scenarioId) {
  return supportedScenario(scenarioId) === "insufficient-evidence" ? 0 : 2;
}

export function fastE2ECallHardCap(scenarioId) {
  return supportedScenario(scenarioId) === "insufficient-evidence" ? 0 : 4;
}

function readHistoricalCase(sourceRoot, scenarioId) {
  const casePath = scenarioPaths(sourceRoot, scenarioId).case;
  const value = JSON.parse(fs.readFileSync(casePath, "utf8"));
  if (value?.scenario_id !== scenarioId) {
    throw new Error(`FAST_E2E_SCENARIO_ID_MISMATCH:${scenarioId}`);
  }
  if (!["CONFIRMED", "INSUFFICIENT"].includes(value.expected_status)) {
    throw new Error(`FAST_E2E_EXPECTED_STATUS_INVALID:${scenarioId}`);
  }
  if (!Array.isArray(value.expected_branch_markers)) {
    throw new Error(`FAST_E2E_EXPECTED_MARKERS_INVALID:${scenarioId}`);
  }
  return value;
}

export function deriveFastE2EV2Expectation(sourceRoot, scenarioId) {
  const legacy = readHistoricalCase(sourceRoot, supportedScenario(scenarioId));
  const sourceMarkers = legacy.expected_branch_markers.map((marker) => {
    if (typeof marker !== "string" || !Object.hasOwn(FAST_E2E_MARKER_TO_METHOD, marker)) {
      throw new Error(`FAST_E2E_MARKER_UNMAPPED:${String(marker)}`);
    }
    return marker;
  });
  const confirmedMethods = Object.freeze([
    ...new Set(sourceMarkers.map((marker) => FAST_E2E_MARKER_TO_METHOD[marker])),
  ]);

  return Object.freeze({
    schema_version: 1,
    scenario_id: legacy.scenario_id,
    source_expected_status: legacy.expected_status,
    source_expected_branch_markers: Object.freeze([...sourceMarkers]),
    expected_terminal_status: legacy.expected_status === "INSUFFICIENT"
      ? "UNRESOLVED"
      : "RESOLVED",
    // The historical oracle only names positive causes. A cause whose marker
    // is absent is not mechanically REJECTED; it is simply not loaded into the
    // Evidence V2 Evaluation Plan.
    expected_confirmed_semantic_ids: confirmedMethods,
  });
}
