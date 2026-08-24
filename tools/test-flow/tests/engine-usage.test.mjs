import assert from "node:assert/strict";
import test from "node:test";

import { applyHardCaps, validOutputTokenCapEvidence as engineValidOutputTokenCapEvidence } from "../lib/engine.mjs";
import { validOutputTokenCapEvidence as evidenceValidOutputTokenCapEvidence } from "../lib/evidence.mjs";
import { normalizeUsage, TOKEN_USAGE_FORMULA } from "../lib/usage.mjs";
import {
  environmentKeySummary,
  ISOLATED_AGENT_ENV_POLICY_VERSION,
  ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT,
} from "../runtime-support/isolated-agent-env.mjs";

const CAPS = Object.freeze({
  max_turns: 5,
  max_total_tokens: 100,
  max_output_tokens: 64,
  max_budget_usd: 1,
  hard_timeout_seconds: 60,
});

function invocation(usage, caps = CAPS) {
  const hasOutputCap = caps.max_output_tokens !== undefined;
  return {
    schema_version: 3,
    invocation_id: "isolated-agent:test",
    class: "isolated-agent",
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

function adjudicate(usage) {
  return applyHardCaps({
    result: { status: "PASS", invocations: [invocation(usage)] },
    planStage: { invocation_caps: [{ class: "isolated-agent", min_count: 1, max_count: 1, caps: CAPS }] },
    expectedModel: "test-model",
  });
}

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

test("the engine independently rejects a receipt above the planned 16-turn boundary", () => {
  const caps = { ...CAPS, max_turns: 16, max_total_tokens: 1000000 };
  const observed = invocation(normalizeUsage({
    input_tokens: 10,
    output_tokens: 20,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    cost_usd: 0.1,
  }), caps);
  observed.turns = 17;
  const result = applyHardCaps({
    result: { status: "PASS", invocations: [observed] },
    planStage: { invocation_caps: [{ class: "isolated-agent", min_count: 1, max_count: 1, caps }] },
    expectedModel: "test-model",
  });
  assert.equal(result.status, "FAIL");
  assert.equal(result.code, "MODEL_TURN_CAP_EXCEEDED");
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

test("a receipt cannot change a planned reasoning effort", () => {
  const observed = invocation(normalizeUsage({
    input_tokens: 10,
    output_tokens: 10,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    cost_usd: 0.1,
  }));
  observed.effective_reasoning_effort = "high";
  const result = applyHardCaps({
    result: { status: "PASS", invocations: [observed] },
    planStage: { invocation_caps: [{ class: "isolated-agent", min_count: 1, max_count: 1, reasoning_effort: "medium", caps: CAPS }] },
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
