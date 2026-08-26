import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";

import {
  aggregateUsage,
  expectedSuiteCalls,
  failureDomain,
  scenarioDecision,
  standaloneScenarioRoots,
  standalonePlatform,
  suiteStatus,
} from "./standalone-suite.mjs";

test("standalone platform admits native Darwin and only explicitly sealed Linux", () => {
  const imageSeal = {
    schema_version: 1,
    image_id: `sha256:${"a".repeat(64)}`,
    platform: "linux/amd64",
    profile: "ubuntu22.04-central-v1",
    status: "PASS",
  };
  const osRelease = { ID: "ubuntu", VERSION_ID: "22.04" };
  assert.equal(standalonePlatform({ platform: "darwin", architecture: "arm64", environment: {} }).topology, "native-darwin-arm64");
  const sealed = standalonePlatform({ platform: "linux", architecture: "x64", environment: { TEST_FLOW_QUICK_UBUNTU2204_CONTAINER: "1" }, imageSeal, osRelease });
  assert.equal(sealed.topology, "sealed-ubuntu2204-linux-x64");
  assert.deepEqual(sealed.image_seal, imageSeal);
  assert.equal(standalonePlatform({ platform: "linux", architecture: "x64", environment: {} }).status, "UNSUPPORTED");
  assert.equal(standalonePlatform({ platform: "linux", architecture: "x64", environment: { TEST_FLOW_QUICK_UBUNTU2204_CONTAINER: "1" }, imageSeal: { ...imageSeal, status: "FAIL" }, osRelease }).code, "LINUX_IMAGE_SEAL_INVALID");
  assert.equal(standalonePlatform({ platform: "linux", architecture: "x64", environment: { TEST_FLOW_QUICK_UBUNTU2204_CONTAINER: "1" }, imageSeal, osRelease: { ID: "ubuntu", VERSION_ID: "24.04" } }).code, "LINUX_OS_RELEASE_MISMATCH");
  assert.equal(standalonePlatform({ platform: "darwin", architecture: "x64", environment: {} }).status, "UNSUPPORTED");
});

test("sealed Linux keeps executable scratch separate from persisted evidence and usage", () => {
  const roots = standaloneScenarioRoots({ runRoot: "/persist/suite/scenarios/a", runId: "suite-a", scratchRoot: "/run/test-flow-scratch" });
  assert.equal(roots.work_root, path.join(path.resolve("/run/test-flow-scratch"), "suite-a", "work"));
  assert.equal(roots.private_root, path.join(path.resolve("/run/test-flow-scratch"), "suite-a", "private"));
  assert.equal(roots.evidence_root, path.join(path.resolve("/persist/suite/scenarios/a"), "evidence"));
  assert.equal(roots.usage_root, path.join(path.resolve("/persist/suite/scenarios/a"), "usage"));
  const scenarios = Array.from({ length: 9 }, (_, index) => standaloneScenarioRoots({ runRoot: `/persist/${index}`, runId: `suite-${index}`, scratchRoot: "/run/test-flow-scratch" }));
  assert.equal(new Set(scenarios.map((item) => item.work_root)).size, 9);
  assert.equal(new Set(scenarios.map((item) => item.evidence_root)).size, 9);
});

test("suite helpers total lifecycle calls, usage, and terminal status", () => {
  assert.equal(expectedSuiteCalls(["a", "b", "c"], (scenario) => scenario === "b" ? 4 : 5), 14);
  assert.deepEqual(aggregateUsage([{ input_tokens: 2, cost_usd: 0.1 }, { input_tokens: 3, cost_usd: 0.2 }]), { input_tokens: 5, cost_usd: 0.3 });
  assert.equal(suiteStatus({ references: [{ status: "PASS" }, { status: "PASS" }], expectedCount: 2 }), "PASS");
  assert.equal(suiteStatus({ references: [{ status: "FAIL" }, { status: "PASS" }], expectedCount: 2 }), "FAIL");
  assert.equal(suiteStatus({ references: [{ status: "PASS" }], expectedCount: 2, engineeringFailure: { code: "TIMEOUT" } }), "ERROR");
  assert.equal(suiteStatus({ references: [], expectedCount: 2, blocked: true }), "BLOCKED");
});

test("completed business and oracle failures continue while engineering failures stop", () => {
  assert.equal(failureDomain({ code: "MACOS_CODEX_LUNA_EXPECTED_TERM_MISSING" }), "CONTRACT");
  assert.equal(failureDomain({ code: "MACOS_CODEX_LUNA_CLIENT_INCOMPLETE" }), "CONTRACT");
  assert.equal(failureDomain({ code: "CLAUDE_DEEPSEEK_RECOVERY_PROJECTION_MISMATCH" }), "CONTRACT");
  assert.equal(failureDomain({ code: "MACOS_CODEX_LUNA_SERVICE_DRAFT_REJECTED" }), "CONTRACT");
  assert.equal(failureDomain({ code: "MACOS_CODEX_LUNA_MCP_READINESS_TIMEOUT" }), "ENGINEERING");
  assert.equal(failureDomain({ code: "CLAUDE_DEEPSEEK_TERMINAL_USAGE_INVALID" }), "ENGINEERING");
  assert.equal(failureDomain({ code: "NEW_UNCLASSIFIED_FAILURE" }), "ENGINEERING");
  assert.deepEqual(scenarioDecision({ status: "FAIL", failure: { code: "MACOS_CODEX_LUNA_EXPECTED_TERM_MISSING" } }), { failure_domain: "CONTRACT", stop: false });
  assert.deepEqual(scenarioDecision({ status: "FAIL", failure: { code: "MACOS_CODEX_LUNA_MCP_READINESS_TIMEOUT" } }), { failure_domain: "ENGINEERING", stop: true });
});
