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
import { environmentControlIdentity } from "../lib/identity.mjs";
import { removeTreeWritable } from "../lib/util.mjs";
import { TOKEN_USAGE_FORMULA } from "../lib/usage.mjs";
import {
  environmentKeySummary,
  ISOLATED_AGENT_ENV_POLICY_VERSION,
  ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT,
  ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_ENFORCEMENT,
  ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY,
} from "../runtime-support/isolated-agent-env.mjs";
import {
  buildSkillGenerationIncompleteAuditRejectedReceipt,
  SKILL_GENERATION_TRACE_CODES,
  SKILL_GENERATION_TOOL_ATTEMPT_POLICY,
  SKILL_GENERATION_TRACE_SCHEMA_VERSION,
} from "../runtime-support/isolated-agent-tool-audit.mjs";
import { SKILL_GENERATION_RULE_IR } from "../runtime-support/skill-generation-rule-ir.mjs";

function writeTest(file) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, "def test_placeholder():\n    assert True\n");
}

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

test("Windows pytest selects the shortest safe default and honors an absolute override", () => {
  const longRepository = "C:\\Users\\example\\deep\\codex-worktrees\\project";
  const ordinary = pytestScratchBoundary({
    platform: "win32",
    temporaryDirectory: "C:\\Temp",
    repoRoot: longRepository,
    attemptRoot: longRepository,
    isolatedAgent: false,
    configuredWindowsDirectory: null,
  });
  const isolated = pytestScratchBoundary({
    platform: "win32",
    temporaryDirectory: "C:\\Temp",
    repoRoot: longRepository,
    attemptRoot: longRepository,
    isolatedAgent: true,
    configuredWindowsDirectory: null,
  });
  assert.equal(ordinary, "C:\\Temp");
  assert.equal(isolated, ordinary);
  assert.equal(ordinary.includes("codex-worktrees"), false);

  assert.equal(
    pytestScratchBoundary({
      platform: "win32",
      temporaryDirectory: "C:\\a-very-long-system-temporary-directory",
      repoRoot: "C:\\r",
      attemptRoot: longRepository,
      isolatedAgent: false,
      configuredWindowsDirectory: null,
    }),
    "C:\\r\\.tmp\\p",
  );
  assert.equal(
    pytestScratchBoundary({
      platform: "win32",
      temporaryDirectory: "C:\\Temp",
      repoRoot: longRepository,
      attemptRoot: longRepository,
      configuredWindowsDirectory: "C:\\tf",
    }),
    "C:\\tf",
  );
  assert.throws(
    () => pytestScratchBoundary({
      platform: "win32",
      temporaryDirectory: "C:\\Temp",
      repoRoot: longRepository,
      attemptRoot: longRepository,
      configuredWindowsDirectory: "relative-scratch",
    }),
    /PYTEST_WINDOWS_SCRATCH_ROOT_ABSOLUTE_REQUIRED/,
  );
});

test("Windows pytest scratch does not inherit a long repository path", () => {
  const boundary = pytestScratchBoundary({
    platform: "win32",
    temporaryDirectory: "C:\\Temp",
    repoRoot: "C:\\Users\\example\\deep\\worktrees\\project",
    attemptRoot: "C:\\Users\\example\\deep\\worktrees\\project\\.tmp\\attempt",
    configuredWindowsDirectory: null,
  });
  assert.equal(boundary, "C:\\Temp");
  assert.equal(pytestBaseTempPath(boundary, "win32"), "\\\\?\\C:\\Temp");
});

test("non-Windows pytest scratch keeps the attempt root boundary", () => {
  assert.equal(
    pytestScratchBoundary({
      platform: "linux",
      temporaryDirectory: "/tmp/must-not-be-used",
      attemptRoot: "/test-flow/attempt",
    }),
    "/test-flow/attempt",
  );
  assert.throws(
    () => pytestScratchBoundary({ platform: "linux" }),
    /PYTEST_ATTEMPT_ROOT_REQUIRED/,
  );
});

test(
  "Windows pytest cleans a namespaced child without crossing its selected boundary",
  { skip: process.platform !== "win32" },
  () => {
    const ordinaryBoundary = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-pytest-boundary-"));
    const cleanupBoundary = pytestBaseTempPath(fs.realpathSync.native(ordinaryBoundary), "win32");
    const scratch = fs.mkdtempSync(path.join(cleanupBoundary, "p-"));
    try {
      const nested = path.join(
        scratch,
        ...Array.from({ length: 8 }, (_, index) => `segment-${index}-${"x".repeat(24)}`),
      );
      fs.mkdirSync(nested, { recursive: true });
      fs.writeFileSync(path.join(nested, "evidence.txt"), "namespaced cleanup\n");
      removeTreeWritable(scratch, cleanupBoundary);
      assert.equal(fs.existsSync(scratch), false);
      assert.equal(fs.existsSync(cleanupBoundary), true);
      assert.throws(
        () => removeTreeWritable(cleanupBoundary, cleanupBoundary),
        (error) => error.code === "CLEANUP_PATH_OUTSIDE_ATTEMPT",
      );
    } finally {
      fs.rmSync(cleanupBoundary, { recursive: true, force: true });
    }
  },
);

test("Windows scratch override participates in runtime environment identity", () => {
  const absent = environmentControlIdentity({});
  const first = environmentControlIdentity({ TEST_FLOW_WINDOWS_SCRATCH_ROOT: "C:\\scratch-a" });
  const second = environmentControlIdentity({ TEST_FLOW_WINDOWS_SCRATCH_ROOT: "C:\\scratch-b" });
  assert.equal(absent.TEST_FLOW_WINDOWS_SCRATCH_ROOT, null);
  assert.match(first.TEST_FLOW_WINDOWS_SCRATCH_ROOT, /^[a-f0-9]{64}$/u);
  assert.notEqual(first.TEST_FLOW_WINDOWS_SCRATCH_ROOT, second.TEST_FLOW_WINDOWS_SCRATCH_ROOT);
});

function passingSkillTraceAudit() {
  const requiredReads = [
    "workspace/inputs/wiki.md",
    "workspace/inputs/clarifications.md",
    "skill/references/generation-spec-v6-reference.md",
    "skill/references/verification-contract-v2-reference.md",
    "skill/references/checkpoints/01-begin-repeated-families-and-paths.md",
    "skill/references/checkpoints/02-begin-9-1-inventory.md",
    "skill/references/checkpoints/03-begin-9-2-witnesses.md",
    "skill/references/checkpoints/04-write-now.md",
  ];
  const linkedReferences = [
    "skill/references/checkpoints/01-begin-repeated-families-and-paths.md",
    "skill/references/checkpoints/02-begin-9-1-inventory.md",
    "skill/references/checkpoints/03-begin-9-2-witnesses.md",
    "skill/references/checkpoints/04-write-now.md",
    "skill/references/generation-spec-v6-reference.md",
    "skill/references/verification-contract-v2-reference.md",
  ];
  const outputOrdinal = requiredReads.length + 1;
  return {
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "PASS",
    workflow: "skill-generation",
    skill: "wiki-to-diagnosis-skill",
    tool_inventory: ["Skill", "Read", "StructuredOutput"],
    permission_mode: "dontAsk",
    permission_policy_sha256: "a".repeat(64),
    attempt_policy: SKILL_GENERATION_TOOL_ATTEMPT_POLICY,
    attempt_policy_sha256: crypto.createHash("sha256").update(JSON.stringify(SKILL_GENERATION_TOOL_ATTEMPT_POLICY)).digest("hex"),
    tool_sequence: [
      { ordinal: 0, tool: "Skill", outcome: "SUCCESS" },
      ...requiredReads.map((readPath, index) => ({ ordinal: index + 1, tool: "Read", outcome: "SUCCESS", path: readPath })),
      { ordinal: outputOrdinal, tool: "StructuredOutput", outcome: "SUCCESS" },
    ],
    accepted_validation_rejections: [],
    required_reads: requiredReads,
    observed_reads: requiredReads.map((readPath, index) => ({ ordinal: index + 1, path: readPath })),
    linked_references: linkedReferences,
    ir_input: {
      ordinal: outputOrdinal,
      size_bytes: 3,
      sha256: "c".repeat(64),
    },
    compiler: {
      id: SKILL_GENERATION_RULE_IR.compiler_id,
      version: SKILL_GENERATION_RULE_IR.compiler_version,
      blueprint_schema_version: SKILL_GENERATION_RULE_IR.blueprint_schema_version,
      family_kind: SKILL_GENERATION_RULE_IR.family_kind,
      family_version: SKILL_GENERATION_RULE_IR.family_version,
    },
    output: {
      ordinal: outputOrdinal,
      path: "workspace/output/generation-spec.json",
      size_bytes: 3,
      sha256: "b".repeat(64),
    },
    terminal: { subtype: "success", is_error: false },
  };
}

function failedPartialSkillTraceAudit({ subtype = "error_max_turns", isError = true } = {}) {
  return {
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "FAIL",
    workflow: "skill-generation",
    code: "SKILL_TRACE_RESULT_NOT_SUCCESS",
    tool_sequence: [
      { ordinal: 0, tool: "Skill", outcome: "SUCCESS" },
      { ordinal: 1, tool: "Read", outcome: "SUCCESS", path: "workspace/inputs/wiki.md" },
      {
        ordinal: 2,
        tool: "StructuredOutput",
        outcome: "ERROR",
        size_bytes: 3,
        sha256: "c".repeat(64),
        diagnostic: { schema_version: 1, status: "INVALID_IR" },
      },
    ],
    terminal: { subtype, is_error: isError },
  };
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
        claude_process: environmentKeySummary({ PATH: "/bin", [ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY]: "2" }),
      },
      tool_trace_audit: failedPartialSkillTraceAudit({ subtype: "error_max_budget_usd" }),
      effective_model: "test-model",
      effective_caps: { max_turns: 12, max_total_tokens: 1000000, max_budget_usd: 3, hard_timeout_seconds: 900 },
      usage_complete: true,
      usage,
      terminal: { subtype: "error_max_budget_usd", is_error: true },
      turns: 7,
      stream: { schema_version: 1, event_count: 2, parsed_event_count: 2, init_count: 1, result_count: 1, last_event_type: "result", complete: true },
      wrapper_outcome: { schema_version: 1, status: "FAIL", code: "WRAPPER_MODEL_CAP_EXCEEDED" },
      hard_cap_enforcement: {
        total_tokens: `terminal-usage-postcondition:${TOKEN_USAGE_FORMULA}`,
        structured_output_retries: ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_ENFORCEMENT,
      },
      timed_out: false,
      process: { exit_code: 1, signal: null, wrapper_exit_code: 1 },
    })}\n`);
    const summary = collectIsolatedModelUsage(
      { gateRoot: root },
      "real-skill-generation",
      { actionStatus: "FAIL" },
    );
    assert.equal(summary.status, "PASS");
    assert.deepEqual(summary.usage, usage);
    assert.equal(summary.invocations.length, 1);
    assert.equal(summary.invocations[0].wrapper_outcome.status, "FAIL");
    assert.equal(summary.invocations[0].tool_trace_audit.status, "FAIL");
    assert.doesNotMatch(JSON.stringify(summary.invocations[0].tool_trace_audit), /content|thinking|raw|file_path/iu);
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(root, "model-usage.json"), "utf8")), summary);
    fs.rmSync(path.join(root, "model-usage.json"));
    const injected = structuredClone(summary.invocations[0]);
    injected.tool_trace_audit.tool_sequence[0].content = "must-not-pass";
    fs.writeFileSync(path.join(usageRoot, "failed.json"), `${JSON.stringify(injected)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage(
        { gateRoot: root },
        "real-skill-generation",
        { actionStatus: "FAIL" },
      ),
      /ISOLATED_MODEL_TOOL_TRACE_AUDIT_INVALID/,
    );
    const diagnosticTamper = structuredClone(summary.invocations[0]);
    diagnosticTamper.tool_trace_audit.tool_sequence.at(-1).diagnostic.raw = "must-not-pass";
    fs.writeFileSync(path.join(usageRoot, "failed.json"), `${JSON.stringify(diagnosticTamper)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage(
        { gateRoot: root },
        "real-skill-generation",
        { actionStatus: "FAIL" },
      ),
      /ISOLATED_MODEL_TOOL_TRACE_AUDIT_INVALID/,
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("failed pytest requires a content-free audit for every parse-complete terminal-less Skill timeout", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-incomplete-model-usage-"));
  try {
    const usageRoot = path.join(root, "model-usage");
    fs.mkdirSync(usageRoot);
    const invocation = {
      schema_version: 3,
      invocation_id: "isolated-agent:timeout",
      class: "isolated-agent",
      workflow: "skill-generation",
      environment_policy: {
        schema_version: 1,
        version: ISOLATED_AGENT_ENV_POLICY_VERSION,
        provider_auth_source: "audited-settings-file",
        session_credentials: "NONE",
        inbound: environmentKeySummary({ PATH: "/bin" }),
        claude_process: environmentKeySummary({ PATH: "/bin", [ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY]: "2" }),
      },
      tool_trace_audit: null,
      effective_model: "test-model",
      effective_caps: { max_turns: 12, max_total_tokens: 1000000, max_budget_usd: 10, hard_timeout_seconds: 1800 },
      usage_complete: false,
      usage: null,
      terminal: null,
      turns: null,
      stream: { schema_version: 1, event_count: 22, parsed_event_count: 22, init_count: 1, result_count: 0, last_event_type: "assistant", complete: false },
      wrapper_outcome: { schema_version: 1, status: "FAIL", code: "WRAPPER_MODEL_TIMEOUT" },
      hard_cap_enforcement: {
        hard_timeout_seconds: "wrapper-process-watchdog",
        total_tokens: `terminal-usage-postcondition:${TOKEN_USAGE_FORMULA}`,
        structured_output_retries: ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_ENFORCEMENT,
      },
      timed_out: true,
      process: { exit_code: null, signal: "SIGTERM", wrapper_exit_code: 124 },
    };
    fs.writeFileSync(path.join(usageRoot, "timeout.json"), `${JSON.stringify(invocation)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation"),
      /ISOLATED_MODEL_USAGE_RECEIPT_INVALID/,
    );
    assert.throws(
      () => collectIsolatedModelUsage(
        { gateRoot: root },
        "real-skill-generation",
        { actionStatus: "FAIL" },
      ),
      /ISOLATED_MODEL_USAGE_RECEIPT_INVALID/,
    );

    const rejectedInvocation = structuredClone(invocation);
    rejectedInvocation.tool_trace_audit = buildSkillGenerationIncompleteAuditRejectedReceipt(
      SKILL_GENERATION_TRACE_CODES.PHASE_SEQUENCE_INVALID,
      structuredClone(rejectedInvocation.stream),
    );
    fs.writeFileSync(path.join(usageRoot, "timeout.json"), `${JSON.stringify(rejectedInvocation)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation"),
      /ISOLATED_MODEL_TERMINAL_RECEIPT_INVALID/,
    );
    const rejectedSummary = collectIsolatedModelUsage(
      { gateRoot: root },
      "real-skill-generation",
      { actionStatus: "FAIL" },
    );
    assert.equal(rejectedSummary.status, "INCOMPLETE");
    assert.equal(rejectedSummary.usage_complete, false);
    assert.equal(rejectedSummary.usage, null);
    assert.deepEqual(rejectedSummary.invocations, [rejectedInvocation]);
    assert.doesNotMatch(JSON.stringify(rejectedSummary.invocations[0].tool_trace_audit), /content|thinking|raw|file_path|message|details/iu);
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(root, "model-usage.json"), "utf8")), rejectedSummary);

    const streamInvalidInvocation = structuredClone(rejectedInvocation);
    streamInvalidInvocation.wrapper_outcome.code = "WRAPPER_MODEL_STREAM_INVALID";
    streamInvalidInvocation.timed_out = false;
    streamInvalidInvocation.process = { exit_code: 1, signal: null, wrapper_exit_code: 1 };
    fs.rmSync(path.join(root, "model-usage.json"));
    fs.writeFileSync(path.join(usageRoot, "timeout.json"), `${JSON.stringify(streamInvalidInvocation)}\n`);
    const streamInvalidSummary = collectIsolatedModelUsage(
      { gateRoot: root },
      "real-skill-generation",
      { actionStatus: "FAIL" },
    );
    assert.equal(streamInvalidSummary.status, "INCOMPLETE");
    assert.deepEqual(streamInvalidSummary.invocations, [streamInvalidInvocation]);

    const nonDiagnosableStream = structuredClone(streamInvalidInvocation);
    nonDiagnosableStream.tool_trace_audit = null;
    nonDiagnosableStream.stream.parsed_event_count -= 1;
    fs.rmSync(path.join(root, "model-usage.json"));
    fs.writeFileSync(path.join(usageRoot, "timeout.json"), `${JSON.stringify(nonDiagnosableStream)}\n`);
    const nonDiagnosableSummary = collectIsolatedModelUsage(
      { gateRoot: root },
      "real-skill-generation",
      { actionStatus: "FAIL" },
    );
    assert.equal(nonDiagnosableSummary.status, "INCOMPLETE");
    assert.deepEqual(nonDiagnosableSummary.invocations, [nonDiagnosableStream]);

    const unknownEventStream = structuredClone(streamInvalidInvocation);
    unknownEventStream.tool_trace_audit = null;
    unknownEventStream.stream.last_event_type = null;
    fs.rmSync(path.join(root, "model-usage.json"));
    fs.writeFileSync(path.join(usageRoot, "timeout.json"), `${JSON.stringify(unknownEventStream)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage(
        { gateRoot: root },
        "real-skill-generation",
        { actionStatus: "FAIL" },
      ),
      /ISOLATED_MODEL_USAGE_RECEIPT_INVALID/,
    );

    const tamperedRejected = structuredClone(rejectedInvocation);
    tamperedRejected.tool_trace_audit.raw = "must-not-pass";
    fs.writeFileSync(path.join(usageRoot, "timeout.json"), `${JSON.stringify(tamperedRejected)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage(
        { gateRoot: root },
        "real-skill-generation",
        { actionStatus: "FAIL" },
      ),
      /ISOLATED_MODEL_USAGE_RECEIPT_INVALID/,
    );

    const terminalNamedStream = structuredClone(rejectedInvocation);
    terminalNamedStream.stream.last_event_type = "result";
    terminalNamedStream.tool_trace_audit.stream.last_event_type = "result";
    fs.writeFileSync(path.join(usageRoot, "timeout.json"), `${JSON.stringify(terminalNamedStream)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage(
        { gateRoot: root },
        "real-skill-generation",
        { actionStatus: "FAIL" },
      ),
      /ISOLATED_MODEL_USAGE_RECEIPT_INVALID/,
    );

    const invalidTimeoutProcess = structuredClone(rejectedInvocation);
    invalidTimeoutProcess.process = { exit_code: 0, signal: null, wrapper_exit_code: 124 };
    fs.writeFileSync(path.join(usageRoot, "timeout.json"), `${JSON.stringify(invalidTimeoutProcess)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage(
        { gateRoot: root },
        "real-skill-generation",
        { actionStatus: "FAIL" },
      ),
      /ISOLATED_MODEL_USAGE_RECEIPT_INVALID/,
    );

    const invalidTimeoutSignal = structuredClone(rejectedInvocation);
    invalidTimeoutSignal.process = { exit_code: null, signal: "SIGUSR1", wrapper_exit_code: 124 };
    fs.writeFileSync(path.join(usageRoot, "timeout.json"), `${JSON.stringify(invalidTimeoutSignal)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage(
        { gateRoot: root },
        "real-skill-generation",
        { actionStatus: "FAIL" },
      ),
      /ISOLATED_MODEL_USAGE_RECEIPT_INVALID/,
    );

    const prefixInvocation = structuredClone(rejectedInvocation);
    prefixInvocation.tool_trace_audit = {
      schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
      status: "FAIL",
      workflow: "skill-generation",
      code: SKILL_GENERATION_TRACE_CODES.INCOMPLETE_PREFIX,
      stream_state: "TERMINAL_MISSING",
      tool_sequence: [{ ordinal: 0, tool: "Skill", outcome: "PENDING" }],
      stream: structuredClone(prefixInvocation.stream),
    };
    fs.writeFileSync(path.join(usageRoot, "timeout.json"), `${JSON.stringify(prefixInvocation)}\n`);
    const prefixSummary = collectIsolatedModelUsage(
      { gateRoot: root },
      "real-skill-generation",
      { actionStatus: "FAIL" },
    );
    assert.equal(prefixSummary.status, "INCOMPLETE");
    assert.deepEqual(prefixSummary.invocations[0].tool_trace_audit, prefixInvocation.tool_trace_audit);
    assert.doesNotMatch(JSON.stringify(prefixSummary.invocations[0].tool_trace_audit), /content|thinking|raw|file_path/iu);

    const tamperedPrefix = structuredClone(prefixInvocation);
    tamperedPrefix.tool_trace_audit.stream.event_count += 1;
    fs.rmSync(path.join(root, "model-usage.json"));
    fs.writeFileSync(path.join(usageRoot, "timeout.json"), `${JSON.stringify(tamperedPrefix)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage(
        { gateRoot: root },
        "real-skill-generation",
        { actionStatus: "FAIL" },
      ),
      /ISOLATED_MODEL_USAGE_RECEIPT_INVALID/,
    );

    const wrongFailure = structuredClone(prefixInvocation);
    wrongFailure.wrapper_outcome.code = "WRAPPER_MODEL_USAGE_INVALID";
    wrongFailure.timed_out = false;
    wrongFailure.process = { exit_code: 1, signal: null, wrapper_exit_code: 1 };
    wrongFailure.stream.complete = true;
    wrongFailure.tool_trace_audit.stream.complete = true;
    fs.writeFileSync(path.join(usageRoot, "timeout.json"), `${JSON.stringify(wrongFailure)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage(
        { gateRoot: root },
        "real-skill-generation",
        { actionStatus: "FAIL" },
      ),
      /ISOLATED_MODEL_USAGE_RECEIPT_INVALID/,
    );
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
        claude_process: environmentKeySummary({ CLAUDE_CODE_MAX_OUTPUT_TOKENS: "64000", HOME: "/home/test", PATH: "/bin", [ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY]: "2" }),
      },
      tool_trace_audit: passingSkillTraceAudit(),
      effective_model: "test-model",
      effective_caps: { max_turns: 12, max_total_tokens: 1000000, max_output_tokens: 64000, max_budget_usd: 10, hard_timeout_seconds: 900 },
      usage_complete: true,
      usage,
      terminal: { subtype: "success", is_error: false },
      turns: 1,
      stream: { schema_version: 1, event_count: 2, parsed_event_count: 2, init_count: 1, result_count: 1, last_event_type: "result", complete: true },
      wrapper_outcome: { schema_version: 1, status: "PASS", code: null },
      hard_cap_enforcement: {
        total_tokens: `terminal-usage-postcondition:${TOKEN_USAGE_FORMULA}`,
        max_output_tokens: ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT,
        structured_output_retries: ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_ENFORCEMENT,
      },
      timed_out: false,
      process: { exit_code: 0, signal: null, wrapper_exit_code: 0 },
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
    const invalidRetryReceipt = structuredClone(summary.invocations[0]);
    invalidRetryReceipt.hard_cap_enforcement.structured_output_retries = "unsealed-retry-limit";
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(invalidRetryReceipt)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation"),
      /ISOLATED_MODEL_ENVIRONMENT_POLICY_RECEIPT_INVALID/,
    );
    const invalidTotalTokenMarker = structuredClone(summary.invocations[0]);
    invalidTotalTokenMarker.hard_cap_enforcement.total_tokens = "terminal-usage-postcondition:legacy-shorthand";
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(invalidTotalTokenMarker)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation"),
      /ISOLATED_MODEL_ENVIRONMENT_POLICY_RECEIPT_INVALID/,
    );
    const missingRetryKey = structuredClone(summary.invocations[0]);
    missingRetryKey.environment_policy.claude_process = environmentKeySummary({
      CLAUDE_CODE_MAX_OUTPUT_TOKENS: "64000",
      HOME: "/home/test",
      PATH: "/bin",
    });
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(missingRetryKey)}\n`);
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
    const zeroTokenPass = structuredClone(summary.invocations[0]);
    zeroTokenPass.usage = {
      schema_version: 1,
      input_tokens: 0,
      output_tokens: 0,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 0,
      total_tokens: 0,
      cost_usd: 0,
    };
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(zeroTokenPass)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation"),
      /ISOLATED_MODEL_USAGE_RECEIPT_INVALID/,
    );
    const writeDowngrade = structuredClone(summary.invocations[0]);
    writeDowngrade.tool_trace_audit.tool_sequence.at(-1).tool = "Write";
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(writeDowngrade)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation"),
      /ISOLATED_MODEL_TOOL_TRACE_AUDIT_INVALID/,
    );
    const partialAsPass = structuredClone(summary.invocations[0]);
    partialAsPass.tool_trace_audit = failedPartialSkillTraceAudit();
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(partialAsPass)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation"),
      /ISOLATED_MODEL_TOOL_TRACE_AUDIT_INVALID/,
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
    failedToolAudit.process.wrapper_exit_code = 1;
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(failedToolAudit)}\n`);
    const failedSummary = collectIsolatedModelUsage(
      { gateRoot: root },
      "real-skill-generation",
      { actionStatus: "FAIL" },
    );
    assert.equal(failedSummary.invocations[0].tool_trace_audit.schema_version, SKILL_GENERATION_TRACE_SCHEMA_VERSION);
    fs.rmSync(path.join(root, "model-usage.json"));

    const unknownCodeAudit = structuredClone(failedToolAudit);
    unknownCodeAudit.tool_trace_audit.code = "SKILL_TRACE_UNKNOWN";
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(unknownCodeAudit)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage(
        { gateRoot: root },
        "real-skill-generation",
        { actionStatus: "FAIL" },
      ),
      /ISOLATED_MODEL_TOOL_TRACE_AUDIT_INVALID/,
    );

    const failedJsonAudit = structuredClone(failedToolAudit);
    failedJsonAudit.tool_trace_audit = {
      schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
      status: "FAIL",
      workflow: "skill-generation",
      code: "SKILL_TRACE_WRITE_JSON_INVALID",
      diagnostic: {
        schema_version: 1,
        kind: "JSON_SYNTAX_ERROR",
        size_bytes: 146007,
        sha256: "c".repeat(64),
        offset: 59227,
        line: 1,
        column: 59228,
      },
    };
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(failedJsonAudit)}\n`);
    const failedJsonSummary = collectIsolatedModelUsage(
      { gateRoot: root },
      "real-skill-generation",
      { actionStatus: "FAIL" },
    );
    assert.equal(failedJsonSummary.invocations[0].tool_trace_audit.code, "SKILL_TRACE_WRITE_JSON_INVALID");
    assert.doesNotMatch(JSON.stringify(failedJsonSummary.invocations[0].tool_trace_audit), /content|snippet|message|private/iu);

    fs.rmSync(path.join(root, "model-usage.json"));
    const tamperedJsonAudit = structuredClone(failedJsonAudit);
    tamperedJsonAudit.tool_trace_audit.diagnostic.snippet = "must-not-be-accepted";
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(tamperedJsonAudit)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage(
        { gateRoot: root },
        "real-skill-generation",
        { actionStatus: "FAIL" },
      ),
      /ISOLATED_MODEL_TOOL_TRACE_AUDIT_INVALID/,
    );

    failedToolAudit.tool_trace_audit.unexpected = true;
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(failedToolAudit)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage(
        { gateRoot: root },
        "real-skill-generation",
        { actionStatus: "FAIL" },
      ),
      /ISOLATED_MODEL_TOOL_TRACE_AUDIT_INVALID/,
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
