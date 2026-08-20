import assert from "node:assert/strict";
import test from "node:test";

import {
  applyHardCaps,
  validIncompleteSkillGenerationInvocationEvidence,
  validOutputTokenCapEvidence as engineValidOutputTokenCapEvidence,
  validStructuredOutputRetryEvidence as engineValidStructuredOutputRetryEvidence,
} from "../lib/engine.mjs";
import {
  validOutputTokenCapEvidence as evidenceValidOutputTokenCapEvidence,
  validStructuredOutputRetryEvidence as evidenceValidStructuredOutputRetryEvidence,
} from "../lib/evidence.mjs";
import { normalizeUsage, TOKEN_USAGE_FORMULA } from "../lib/usage.mjs";
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
  SKILL_GENERATION_TRACE_SCHEMA_VERSION,
} from "../runtime-support/isolated-agent-tool-audit.mjs";

const CAPS = Object.freeze({
  max_turns: 5,
  max_total_tokens: 100,
  max_output_tokens: 64,
  max_budget_usd: 1,
  hard_timeout_seconds: 60,
});

function invocation(usage, caps = CAPS, { workflow = "job" } = {}) {
  const hasOutputCap = caps.max_output_tokens !== undefined;
  return {
    schema_version: 3,
    invocation_id: "isolated-agent:test",
    class: "isolated-agent",
    workflow,
    effective_model: "test-model",
    effective_caps: caps,
    environment_policy: {
      schema_version: 1,
      version: ISOLATED_AGENT_ENV_POLICY_VERSION,
      inbound: environmentKeySummary({ HOME: "/home/test", PATH: "/bin" }),
      claude_process: environmentKeySummary(hasOutputCap
        ? { CLAUDE_CODE_MAX_OUTPUT_TOKENS: String(caps.max_output_tokens), HOME: "/home/test", PATH: "/bin" }
        : { HOME: "/home/test", PATH: "/bin" }),
    },
    usage_complete: true,
    usage,
    terminal: { subtype: "success", is_error: false },
    wrapper_outcome: { schema_version: 1, status: "PASS", code: null },
    turns: 1,
    hard_cap_enforcement: {
      turns: "claude-cli",
      cost_usd: "claude-cli",
      hard_timeout_seconds: "wrapper-process-watchdog",
      total_tokens: `terminal-usage-postcondition:${TOKEN_USAGE_FORMULA}`,
      ...(hasOutputCap ? { max_output_tokens: ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT } : {}),
    },
  };
}

test("engine and evidence bind the Skill structured retry limit to child-only environment evidence", () => {
  const observed = invocation(normalizeUsage({
    input_tokens: 10,
    output_tokens: 10,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    cost_usd: 0.1,
  }), CAPS, { workflow: "skill-generation" });
  observed.environment_policy.claude_process = environmentKeySummary({
    CLAUDE_CODE_MAX_OUTPUT_TOKENS: "64",
    HOME: "/home/test",
    PATH: "/bin",
    [ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY]: "2",
  });
  observed.hard_cap_enforcement.structured_output_retries = ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_ENFORCEMENT;
  assert.equal(engineValidStructuredOutputRetryEvidence(observed), true);
  assert.equal(evidenceValidStructuredOutputRetryEvidence(observed), true);

  observed.hard_cap_enforcement.structured_output_retries = "unsealed";
  assert.equal(engineValidStructuredOutputRetryEvidence(observed), false);
  assert.equal(evidenceValidStructuredOutputRetryEvidence(observed), false);
});

function adjudicate(usage) {
  return applyHardCaps({
    result: { status: "PASS", invocations: [invocation(usage)] },
    planStage: { invocation_caps: [{ class: "isolated-agent", min_count: 1, max_count: 1, caps: CAPS }] },
    expectedModel: "test-model",
  });
}

test("engine independently binds terminal-less prefix and rejection evidence to timeout state", () => {
  const stream = {
    schema_version: 1,
    event_count: 2,
    parsed_event_count: 2,
    init_count: 1,
    result_count: 0,
    last_event_type: "assistant",
    complete: false,
  };
  const failed = {
    workflow: "skill-generation",
    usage_complete: false,
    usage: null,
    terminal: null,
    turns: null,
    stream,
    timed_out: true,
    process: { exit_code: null, signal: "SIGTERM", wrapper_exit_code: 124 },
    wrapper_outcome: { schema_version: 1, status: "FAIL", code: "WRAPPER_MODEL_TIMEOUT" },
    tool_trace_audit: {
      schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
      status: "FAIL",
      workflow: "skill-generation",
      code: SKILL_GENERATION_TRACE_CODES.INCOMPLETE_PREFIX,
      stream_state: "TERMINAL_MISSING",
      tool_sequence: [{ ordinal: 0, tool: "Skill", outcome: "PENDING" }],
      stream: structuredClone(stream),
    },
  };
  assert.equal(validIncompleteSkillGenerationInvocationEvidence(failed), true);
  const preserved = applyHardCaps({
    result: { status: "FAIL", invocations: [failed] },
    planStage: { invocation_caps: [] },
    expectedModel: "test-model",
  });
  assert.equal(preserved.status, "FAIL");

  const rejectedPrefix = structuredClone(failed);
  rejectedPrefix.tool_trace_audit = buildSkillGenerationIncompleteAuditRejectedReceipt(
    SKILL_GENERATION_TRACE_CODES.PHASE_SEQUENCE_INVALID,
    structuredClone(stream),
  );
  assert.equal(validIncompleteSkillGenerationInvocationEvidence(rejectedPrefix), true);
  const preservedRejection = applyHardCaps({
    result: { status: "FAIL", invocations: [rejectedPrefix] },
    planStage: { invocation_caps: [] },
    expectedModel: "test-model",
  });
  assert.equal(preservedRejection.status, "FAIL");

  const wrongWrapperCode = structuredClone(rejectedPrefix);
  wrongWrapperCode.wrapper_outcome.code = "WRAPPER_MODEL_CAP_EXCEEDED";
  const wrongWrapperRejected = applyHardCaps({
    result: { status: "FAIL", invocations: [wrongWrapperCode] },
    planStage: { invocation_caps: [] },
    expectedModel: "test-model",
  });
  assert.equal(wrongWrapperRejected.status, "ERROR");
  assert.equal(wrongWrapperRejected.code, "MODEL_TOOL_TRACE_AUDIT_INVALID");

  const streamInvalidRejection = structuredClone(rejectedPrefix);
  streamInvalidRejection.wrapper_outcome.code = "WRAPPER_MODEL_STREAM_INVALID";
  streamInvalidRejection.timed_out = false;
  streamInvalidRejection.process = { exit_code: 1, signal: null, wrapper_exit_code: 1 };
  assert.equal(validIncompleteSkillGenerationInvocationEvidence(streamInvalidRejection), true);
  assert.equal(applyHardCaps({
    result: { status: "FAIL", invocations: [streamInvalidRejection] },
    planStage: { invocation_caps: [] },
    expectedModel: "test-model",
  }).status, "FAIL");

  const missingAudit = structuredClone(failed);
  missingAudit.tool_trace_audit = null;
  assert.equal(validIncompleteSkillGenerationInvocationEvidence(missingAudit), false);
  const missingRejected = applyHardCaps({
    result: { status: "FAIL", invocations: [missingAudit] },
    planStage: { invocation_caps: [] },
    expectedModel: "test-model",
  });
  assert.equal(missingRejected.status, "ERROR");
  assert.equal(missingRejected.code, "MODEL_TOOL_TRACE_AUDIT_INVALID");

  const invalidTimeoutProcess = structuredClone(rejectedPrefix);
  invalidTimeoutProcess.process = { exit_code: 0, signal: null, wrapper_exit_code: 124 };
  assert.equal(validIncompleteSkillGenerationInvocationEvidence(invalidTimeoutProcess), false);
  const invalidTimeoutRejected = applyHardCaps({
    result: { status: "FAIL", invocations: [invalidTimeoutProcess] },
    planStage: { invocation_caps: [] },
    expectedModel: "test-model",
  });
  assert.equal(invalidTimeoutRejected.status, "ERROR");
  assert.equal(invalidTimeoutRejected.code, "MODEL_TOOL_TRACE_AUDIT_INVALID");

  const invalidTimeoutSignal = structuredClone(rejectedPrefix);
  invalidTimeoutSignal.process = { exit_code: null, signal: "SIGUSR1", wrapper_exit_code: 124 };
  assert.equal(validIncompleteSkillGenerationInvocationEvidence(invalidTimeoutSignal), false);
  const invalidSignalRejected = applyHardCaps({
    result: { status: "FAIL", invocations: [invalidTimeoutSignal] },
    planStage: { invocation_caps: [] },
    expectedModel: "test-model",
  });
  assert.equal(invalidSignalRejected.status, "ERROR");
  assert.equal(invalidSignalRejected.code, "MODEL_TOOL_TRACE_AUDIT_INVALID");

  const terminalNamedStream = structuredClone(rejectedPrefix);
  terminalNamedStream.stream.last_event_type = "result";
  terminalNamedStream.tool_trace_audit.stream.last_event_type = "result";
  assert.equal(validIncompleteSkillGenerationInvocationEvidence(terminalNamedStream), false);
  const terminalNamedRejected = applyHardCaps({
    result: { status: "FAIL", invocations: [terminalNamedStream] },
    planStage: { invocation_caps: [] },
    expectedModel: "test-model",
  });
  assert.equal(terminalNamedRejected.status, "ERROR");
  assert.equal(terminalNamedRejected.code, "MODEL_TOOL_TRACE_AUDIT_INVALID");

  const leakedRejection = structuredClone(rejectedPrefix);
  leakedRejection.tool_trace_audit.raw = "must-not-pass";
  assert.equal(validIncompleteSkillGenerationInvocationEvidence(leakedRejection), false);
  const leakedRejected = applyHardCaps({
    result: { status: "FAIL", invocations: [leakedRejection] },
    planStage: { invocation_caps: [] },
    expectedModel: "test-model",
  });
  assert.equal(leakedRejected.status, "ERROR");
  assert.equal(leakedRejected.code, "MODEL_TOOL_TRACE_AUDIT_INVALID");

  const completeUsage = normalizeUsage({
    input_tokens: 10,
    output_tokens: 10,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    cost_usd: 0.1,
  });
  const passWithRejection = invocation(completeUsage, CAPS, { workflow: "skill-generation" });
  passWithRejection.tool_trace_audit = buildSkillGenerationIncompleteAuditRejectedReceipt(
    SKILL_GENERATION_TRACE_CODES.PHASE_SEQUENCE_INVALID,
    structuredClone(stream),
  );
  const rejectedPass = applyHardCaps({
    result: { status: "PASS", invocations: [passWithRejection] },
    planStage: { invocation_caps: [{ class: "isolated-agent", min_count: 1, max_count: 1, caps: CAPS }] },
    expectedModel: "test-model",
  });
  assert.equal(rejectedPass.status, "ERROR");
  assert.equal(rejectedPass.code, "MODEL_HARD_CAP_RECEIPT_MISMATCH");

  const tampered = structuredClone(failed);
  tampered.tool_trace_audit.stream.parsed_event_count -= 1;
  assert.equal(validIncompleteSkillGenerationInvocationEvidence(tampered), false);
  const rejected = applyHardCaps({
    result: { status: "FAIL", invocations: [tampered] },
    planStage: { invocation_caps: [] },
    expectedModel: "test-model",
  });
  assert.equal(rejected.status, "ERROR");
  assert.equal(rejected.code, "MODEL_TOOL_TRACE_AUDIT_INVALID");
});

test("model hard token cap includes cache creation and cache reads", () => {
  const result = adjudicate(normalizeUsage({
    input_tokens: 1,
    output_tokens: 1,
    cache_creation_input_tokens: 49,
    cache_read_input_tokens: 50,
    cost_usd: 0.1,
  }));
  assert.equal(result.status, "FAIL");
  assert.equal(result.code, "MODEL_TOKEN_CAP_EXCEEDED");
});

test("model usage cannot be complete when either cache component is absent", () => {
  const result = adjudicate({
    schema_version: 1,
    input_tokens: 10,
    output_tokens: 10,
    total_tokens: 20,
    cost_usd: 0.1,
  });
  assert.equal(result.status, "ERROR");
  assert.equal(result.code, "MODEL_HARD_CAP_RECEIPT_MISMATCH");
});

test("an isolated PASS receipt cannot report zero total tokens", () => {
  const result = adjudicate(normalizeUsage({
    input_tokens: 0,
    output_tokens: 0,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    cost_usd: 0,
  }));
  assert.equal(result.status, "ERROR");
  assert.equal(result.code, "MODEL_HARD_CAP_RECEIPT_MISMATCH");
});

test("zero cost remains valid when isolated token usage is positive", () => {
  const result = adjudicate(normalizeUsage({
    input_tokens: 1,
    output_tokens: 1,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    cost_usd: 0,
  }));
  assert.equal(result.status, "PASS");
});

test("a failed wrapper outcome cannot pass even when the terminal shape and usage look successful", () => {
  const observed = invocation(normalizeUsage({
    input_tokens: 10,
    output_tokens: 10,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    cost_usd: 0.1,
  }));
  observed.wrapper_outcome = { schema_version: 1, status: "FAIL", code: "WRAPPER_SKILL_TRACE_INVALID" };
  const result = applyHardCaps({
    result: { status: "PASS", invocations: [observed] },
    planStage: { invocation_caps: [{ class: "isolated-agent", min_count: 1, max_count: 1, caps: CAPS }] },
    expectedModel: "test-model",
  });
  assert.equal(result.status, "ERROR");
  assert.equal(result.code, "MODEL_HARD_CAP_RECEIPT_MISMATCH");
});

test("a receipt cannot change the planned per-response output token cap", () => {
  const observed = invocation(normalizeUsage({
    input_tokens: 10,
    output_tokens: 10,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    cost_usd: 0.1,
  }));
  observed.effective_caps = { ...CAPS, max_output_tokens: 32 };
  const result = applyHardCaps({
    result: { status: "PASS", invocations: [observed] },
    planStage: { invocation_caps: [{ class: "isolated-agent", min_count: 1, max_count: 1, caps: CAPS }] },
    expectedModel: "test-model",
  });
  assert.equal(result.status, "ERROR");
  assert.equal(result.code, "MODEL_HARD_CAP_RECEIPT_MISMATCH");
});

test("the single-response cap is independent from cumulative output usage", () => {
  const usage = normalizeUsage({
    input_tokens: 5,
    output_tokens: 80,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    cost_usd: 0.1,
  });
  const observed = invocation(usage);
  assert.equal(engineValidOutputTokenCapEvidence(observed, CAPS), true);
  assert.equal(evidenceValidOutputTokenCapEvidence(observed, CAPS), true);
  assert.equal(adjudicate(usage).status, "PASS");
});

test("engine and sealed-evidence audits require the sealed enforcement binding", () => {
  const observed = invocation(normalizeUsage({
    input_tokens: 10,
    output_tokens: 10,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    cost_usd: 0.1,
  }));
  delete observed.hard_cap_enforcement.max_output_tokens;
  assert.equal(engineValidOutputTokenCapEvidence(observed, CAPS), false);
  assert.equal(evidenceValidOutputTokenCapEvidence(observed, CAPS), false);
  observed.hard_cap_enforcement.max_output_tokens = "terminal-model-usage-echo";
  assert.equal(engineValidOutputTokenCapEvidence(observed, CAPS), false);
  assert.equal(evidenceValidOutputTokenCapEvidence(observed, CAPS), false);
});

test("engine and sealed-evidence audits reject the legacy request-limit observation field", () => {
  const observed = invocation(normalizeUsage({
    input_tokens: 10,
    output_tokens: 10,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    cost_usd: 0.1,
  }));
  observed.observed_request_limits = [64];
  assert.equal(engineValidOutputTokenCapEvidence(observed, CAPS), false);
  assert.equal(evidenceValidOutputTokenCapEvidence(observed, CAPS), false);
});

test("an undeclared output cap forbids an enforcement marker", () => {
  const caps = { max_turns: 5, max_total_tokens: 100, max_budget_usd: 1, hard_timeout_seconds: 60 };
  const observed = invocation(normalizeUsage({
    input_tokens: 10,
    output_tokens: 10,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    cost_usd: 0.1,
  }), caps);
  assert.equal(engineValidOutputTokenCapEvidence(observed, caps), true);
  assert.equal(evidenceValidOutputTokenCapEvidence(observed, caps), true);
  observed.hard_cap_enforcement.max_output_tokens = ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT;
  assert.equal(engineValidOutputTokenCapEvidence(observed, caps), false);
  assert.equal(evidenceValidOutputTokenCapEvidence(observed, caps), false);
});

test("an isolated receipt without a declared cap also forbids the child-only env key", () => {
  const caps = { max_turns: 5, max_total_tokens: 100, max_budget_usd: 1, hard_timeout_seconds: 60 };
  const observed = invocation(normalizeUsage({
    input_tokens: 10,
    output_tokens: 10,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    cost_usd: 0.1,
  }), caps);
  observed.environment_policy.claude_process = environmentKeySummary({
    CLAUDE_CODE_MAX_OUTPUT_TOKENS: "64000",
    HOME: "/home/test",
    PATH: "/bin",
  });
  assert.equal(engineValidOutputTokenCapEvidence(observed, caps), false);
  assert.equal(evidenceValidOutputTokenCapEvidence(observed, caps), false);
});

test("non-isolated receipts remain valid without an output-cap environment policy", () => {
  const caps = { max_turns: 5, max_total_tokens: 100, max_budget_usd: 1, hard_timeout_seconds: 60 };
  const observed = { class: "host-client", hard_cap_enforcement: {} };
  assert.equal(engineValidOutputTokenCapEvidence(observed, caps), true);
  assert.equal(evidenceValidOutputTokenCapEvidence(observed, caps), true);
});
