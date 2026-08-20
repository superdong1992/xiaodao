import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  collectIsolatedModelUsage,
  evaluatePytestSummary,
  materializePytestSummary,
  parseJUnitSummary,
  planAffectedSelection,
  probeLoopbackCapability,
  pytestBaseTempPath,
  pytestScratchBoundary,
} from "../lib/actions.mjs";
import { applyGateEvidenceContract } from "../lib/engine.mjs";
import { removeTreeWritable } from "../lib/util.mjs";
import {
  environmentKeySummary,
  ISOLATED_AGENT_ENV_POLICY_VERSION,
  ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT,
} from "../runtime-support/isolated-agent-env.mjs";
import {
  SKILL_GENERATION_TOOL_ATTEMPT_POLICY,
  SKILL_GENERATION_TRACE_SCHEMA_VERSION,
} from "../runtime-support/isolated-agent-tool-audit.mjs";

function writeTest(file) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, "def test_placeholder():\n    assert True\n");
}

function passingSkillTraceAudit() {
  const requiredReads = [
    "workspace/inputs/wiki.md",
    "workspace/inputs/clarifications.md",
    "skill/references/generation-spec-v6-reference.md",
    "skill/references/verification-contract-v2-reference.md",
  ];
  return {
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "PASS",
    workflow: "skill-generation",
    skill: "wiki-to-diagnosis-skill",
    tool_inventory: ["Skill", "Read", "Write"],
    permission_mode: "dontAsk",
    permission_policy_sha256: "a".repeat(64),
    attempt_policy: SKILL_GENERATION_TOOL_ATTEMPT_POLICY,
    attempt_policy_sha256: crypto.createHash("sha256").update(JSON.stringify(SKILL_GENERATION_TOOL_ATTEMPT_POLICY)).digest("hex"),
    tool_sequence: [
      { ordinal: 0, tool: "Skill", outcome: "SUCCESS" },
      ...requiredReads.map((readPath, index) => ({ ordinal: index + 1, tool: "Read", outcome: "SUCCESS", path: readPath })),
      { ordinal: 5, tool: "Write", outcome: "SUCCESS", path: "workspace/output/generation-spec.json" },
    ],
    accepted_validation_rejections: [],
    required_reads: requiredReads,
    observed_reads: requiredReads.map((readPath, index) => ({ ordinal: index + 1, path: readPath })),
    linked_references: requiredReads.filter((readPath) => readPath.startsWith("skill/")),
    output: {
      ordinal: 5,
      path: "workspace/output/generation-spec.json",
      size_bytes: 3,
      sha256: "b".repeat(64),
    },
    terminal: { subtype: "success", is_error: false },
  };
}

test("Windows pytest selects the shortest safe default and honors an absolute override", () => {
  const temporaryDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-pytest-boundary-"));
  const longAttemptRoot = path.join(
    temporaryDirectory,
    "codex-worktrees",
    "a-very-long-worktree-name-that-must-not-prefix-pytest-scratch",
  );
  try {
    const ordinary = pytestScratchBoundary({
      platform: "win32",
      temporaryDirectory,
      repoRoot: longAttemptRoot,
      attemptRoot: longAttemptRoot,
      isolatedAgent: false,
      configuredWindowsDirectory: null,
    });
    const isolated = pytestScratchBoundary({
      platform: "win32",
      temporaryDirectory,
      repoRoot: longAttemptRoot,
      attemptRoot: longAttemptRoot,
      isolatedAgent: true,
      configuredWindowsDirectory: null,
    });
    assert.equal(ordinary, path.resolve(temporaryDirectory));
    assert.equal(isolated, ordinary);
    assert.equal(ordinary.includes("codex-worktrees"), false);

    const shortRepoRoot = path.join(temporaryDirectory, "r");
    assert.equal(
      pytestScratchBoundary({
        platform: "win32",
        temporaryDirectory: path.join(temporaryDirectory, "long-system-temp-name"),
        repoRoot: shortRepoRoot,
        attemptRoot: longAttemptRoot,
        isolatedAgent: false,
        configuredWindowsDirectory: null,
      }),
      path.resolve(shortRepoRoot, ".tmp", "p"),
    );
    const configured = path.join(temporaryDirectory, "configured");
    assert.equal(
      pytestScratchBoundary({
        platform: "win32",
        temporaryDirectory,
        repoRoot: longAttemptRoot,
        attemptRoot: longAttemptRoot,
        configuredWindowsDirectory: configured,
      }),
      path.resolve(configured),
    );
    assert.throws(
      () => pytestScratchBoundary({
        platform: "win32",
        temporaryDirectory,
        repoRoot: longAttemptRoot,
        attemptRoot: longAttemptRoot,
        configuredWindowsDirectory: "relative-scratch",
      }),
      /PYTEST_WINDOWS_SCRATCH_ROOT_ABSOLUTE_REQUIRED/,
    );

    const scratch = fs.mkdtempSync(path.join(ordinary, "p-"));
    assert.equal(path.dirname(scratch), ordinary);
    removeTreeWritable(scratch, ordinary);
    assert.equal(fs.existsSync(scratch), false);
    assert.throws(
      () => removeTreeWritable(ordinary, ordinary),
      (error) => error.code === "CLEANUP_PATH_OUTSIDE_ATTEMPT",
    );
  } finally {
    fs.rmSync(temporaryDirectory, { recursive: true, force: true });
  }
});

test("Windows pytest base temp uses an extended-length path without moving scratch", () => {
  assert.equal(
    pytestBaseTempPath("C:\\workspace\\.tmp\\p\\p-123456", "win32"),
    "\\\\?\\C:\\workspace\\.tmp\\p\\p-123456",
  );
  assert.equal(
    pytestBaseTempPath("\\\\server\\share\\p-123456", "win32"),
    "\\\\?\\UNC\\server\\share\\p-123456",
  );
  assert.equal(pytestBaseTempPath("/tmp/p-123456", "linux"), "/tmp/p-123456");
});

test("non-Windows pytest scratch keeps the attempt root boundary", () => {
  const attemptRoot = path.join(os.tmpdir(), "test-flow-attempt-boundary");
  assert.equal(
    pytestScratchBoundary({
      platform: "linux",
      temporaryDirectory: path.join(os.tmpdir(), "must-not-be-used"),
      attemptRoot,
    }),
    path.resolve(attemptRoot),
  );
});

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

test("REST guide and OpenAPI snapshot changes select the browser contract regression", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-affected-rest-guide-"));
  try {
    for (const name of ["a", "b", "c", "d"]) writeTest(path.join(root, "tests", "deterministic", "unit", `test_${name}.py`));
    writeTest(path.join(root, "tests", "deterministic", "contracts", "test_contract.py"));
    const webApiTest = path.join(root, "tests", "deterministic", "unit", "interfaces", "test_web_api.py");
    writeTest(webApiTest);

    const guideSelection = planAffectedSelection(root, ["docs/browser-rest-api.md"]);
    assert.deepEqual(guideSelection.selectors, ["tests/deterministic/unit/interfaces/test_web_api.py"]);
    assert.equal(guideSelection.covered_test_files, 1);
    assert.equal(guideSelection.defer_to_full, false);

    const snapshotSelection = planAffectedSelection(root, ["schemas/v2/web-api.openapi.snapshot.json"]);
    assert.deepEqual(snapshotSelection.selectors, [
      "tests/deterministic/contracts",
      "tests/deterministic/unit/interfaces/test_web_api.py",
    ]);
    assert.equal(snapshotSelection.covered_test_files, 2);
    assert.equal(snapshotSelection.defer_to_full, false);
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

test("a parseable failing JUnit result is materialized as a summary", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-junit-failure-summary-"));
  try {
    fs.writeFileSync(path.join(root, "pytest.xml"), '<testsuites tests="1" failures="1" errors="0" skipped="0"></testsuites>\n');
    const summary = materializePytestSummary(root);
    assert.deepEqual(summary, {
      schema_version: 2,
      tests: 1,
      passed: 0,
      failures: 1,
      errors: 0,
      skipped: 0,
      executed: 1,
    });
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(root, "pytest-summary.json"), "utf8")), summary);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("failed Gates index existing declared evidence while PASS still requires every file", () => {
  const attemptRoot = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-failure-evidence-"));
  try {
    const stage = { id: "real.skill-generation" };
    const gatePlan = { id: "real.agent.skill-generation" };
    const gate = { evidence: ["pytest.xml", "pytest-summary.json", "scenario-evaluation-audit.json"] };
    const gateRoot = path.join(attemptRoot, "payload", "stages", stage.id, "gates", gatePlan.id);
    fs.mkdirSync(gateRoot, { recursive: true });
    fs.writeFileSync(path.join(gateRoot, "pytest.xml"), "<testsuites tests=\"1\" failures=\"1\"/>\n");
    fs.writeFileSync(path.join(gateRoot, "scenario-evaluation-audit.json"), '{"schema_version":1,"status":"FAIL"}\n');

    const failed = applyGateEvidenceContract({
      actionResult: { status: "FAIL", failure_domain: "CONTRACT", code: "PYTEST_FAILED" },
      gate,
      gatePlan,
      stage,
      attemptRoot,
    });
    assert.equal(failed.result.status, "FAIL");
    assert.deepEqual(failed.evidence.map((item) => path.basename(item.path)), ["pytest.xml", "scenario-evaluation-audit.json"]);

    const incompletePass = applyGateEvidenceContract({
      actionResult: { status: "PASS" },
      gate,
      gatePlan,
      stage,
      attemptRoot,
    });
    assert.equal(incompletePass.result.status, "ERROR");
    assert.equal(incompletePass.result.code, "GATE_REQUIRED_EVIDENCE_MISSING");

    fs.writeFileSync(path.join(gateRoot, "pytest-summary.json"), '{"schema_version":2}\n');
    const completePass = applyGateEvidenceContract({
      actionResult: { status: "PASS" },
      gate,
      gatePlan,
      stage,
      attemptRoot,
    });
    assert.equal(completePass.result.status, "PASS");
    assert.equal(completePass.evidence.length, 3);
  } finally {
    fs.rmSync(attemptRoot, { recursive: true, force: true });
  }
});

test("failed isolated invocation usage is collected as evidence without converting the Gate to PASS", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-failed-model-usage-"));
  try {
    const usageRoot = path.join(root, "model-usage");
    fs.mkdirSync(usageRoot);
    const usage = {
      schema_version: 1,
      input_tokens: 24411,
      output_tokens: 97144,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 68736,
      total_tokens: 190291,
      cost_usd: 3.398151,
    };
    fs.writeFileSync(path.join(usageRoot, "failed.json"), `${JSON.stringify({
      schema_version: 3,
      invocation_id: "isolated-agent:failed",
      class: "isolated-agent",
      workflow: "skill-generation",
      environment_policy: {
        schema_version: 1,
        version: ISOLATED_AGENT_ENV_POLICY_VERSION,
        provider_auth_source: "audited-settings-file",
        session_credentials: "NONE",
        inbound: environmentKeySummary({ PATH: "/bin" }),
        claude_process: environmentKeySummary({ PATH: "/bin" }),
      },
      tool_trace_audit: null,
      effective_model: "test-model",
      effective_caps: { max_turns: 12, max_total_tokens: 1000000, max_budget_usd: 3, hard_timeout_seconds: 900 },
      usage_complete: true,
      usage,
      terminal: { subtype: "error_max_budget_usd", is_error: true },
      turns: 7,
      wrapper_outcome: { schema_version: 1, status: "FAIL", code: "WRAPPER_MODEL_TERMINAL_INVALID" },
      hard_cap_enforcement: {},
      timed_out: false,
      process: { exit_code: 1, signal: null },
    })}\n`);
    const summary = collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation");
    assert.equal(summary.status, "PASS");
    assert.deepEqual(summary.usage, usage);
    assert.equal(summary.invocations.length, 1);
    assert.equal(summary.invocations[0].wrapper_outcome.status, "FAIL");
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(root, "model-usage.json"), "utf8")), summary);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("missing failed invocation usage remains incomplete instead of hiding the original Gate failure", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-missing-model-usage-"));
  try {
    assert.throws(
      () => collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation"),
      /ISOLATED_MODEL_USAGE_RECEIPT_MISSING/,
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("isolated usage collection requires the child-env and sealed runtime binding for an output cap", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-output-cap-receipt-"));
  try {
    const usageRoot = path.join(root, "model-usage");
    fs.mkdirSync(usageRoot);
    const usage = {
      schema_version: 1,
      input_tokens: 10,
      output_tokens: 20,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 0,
      total_tokens: 30,
      cost_usd: 0.01,
    };
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify({
      schema_version: 3,
      invocation_id: "isolated-agent:capped",
      class: "isolated-agent",
      workflow: "skill-generation",
      environment_policy: {
        schema_version: 1,
        version: ISOLATED_AGENT_ENV_POLICY_VERSION,
        provider_auth_source: "audited-settings-file",
        session_credentials: "NONE",
        inbound: environmentKeySummary({ HOME: "/home/test", PATH: "/bin" }),
        claude_process: environmentKeySummary({ CLAUDE_CODE_MAX_OUTPUT_TOKENS: "64000", HOME: "/home/test", PATH: "/bin" }),
      },
      tool_trace_audit: passingSkillTraceAudit(),
      effective_model: "test-model",
      effective_caps: { max_turns: 12, max_total_tokens: 1000000, max_output_tokens: 64000, max_budget_usd: 10, hard_timeout_seconds: 900 },
      usage_complete: true,
      usage,
      terminal: { subtype: "success", is_error: false },
      turns: 1,
      wrapper_outcome: { schema_version: 1, status: "PASS", code: null },
      hard_cap_enforcement: {
        total_tokens: "terminal-usage-postcondition:input+output+cache_creation_input+cache_read_input",
        max_output_tokens: ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT,
      },
      timed_out: false,
      process: { exit_code: 0, signal: null },
    })}\n`);
    const summary = collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation");
    assert.equal(summary.status, "PASS");
    assert.equal(summary.invocations[0].hard_cap_enforcement.max_output_tokens, ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT);
    const invalidReceipt = structuredClone(summary.invocations[0]);
    invalidReceipt.hard_cap_enforcement.max_output_tokens = "terminal-model-usage-echo";
    fs.rmSync(path.join(root, "model-usage.json"));
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(invalidReceipt)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation"),
      /ISOLATED_MODEL_ENVIRONMENT_POLICY_RECEIPT_INVALID/,
    );
    const legacyReceipt = structuredClone(summary.invocations[0]);
    legacyReceipt.observed_request_limits = [64000];
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(legacyReceipt)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation"),
      /ISOLATED_MODEL_USAGE_RECEIPT_INVALID/,
    );
    const invalidToolAudit = structuredClone(summary.invocations[0]);
    invalidToolAudit.tool_trace_audit.attempt_policy.max_empty_write_rejections = 2;
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(invalidToolAudit)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation"),
      /ISOLATED_MODEL_TOOL_TRACE_AUDIT_INVALID/,
    );
    const failedToolAudit = structuredClone(summary.invocations[0]);
    failedToolAudit.tool_trace_audit = {
      schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
      status: "FAIL",
      workflow: "skill-generation",
      code: "SKILL_TRACE_TOOL_RESULT_ERROR",
    };
    failedToolAudit.wrapper_outcome = { schema_version: 1, status: "FAIL", code: "WRAPPER_SKILL_TRACE_INVALID" };
    failedToolAudit.process.exit_code = 1;
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(failedToolAudit)}\n`);
    const failedSummary = collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation");
    assert.equal(failedSummary.invocations[0].tool_trace_audit.schema_version, SKILL_GENERATION_TRACE_SCHEMA_VERSION);
    fs.rmSync(path.join(root, "model-usage.json"));
    failedToolAudit.tool_trace_audit.unexpected = true;
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(failedToolAudit)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation"),
      /ISOLATED_MODEL_TOOL_TRACE_AUDIT_INVALID/,
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
