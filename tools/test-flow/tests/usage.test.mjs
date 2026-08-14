import assert from "node:assert/strict";
import test from "node:test";

import {
  isCompleteUsage,
  normalizeUsage,
  sumUsage,
  TOKEN_USAGE_FORMULA,
} from "../lib/usage.mjs";

test("cache-inclusive token usage has a versioned four-component total", () => {
  const usage = normalizeUsage({
    input_tokens: 10,
    output_tokens: 20,
    cache_creation_input_tokens: 30,
    cache_read_input_tokens: 40,
    cost_usd: 0.5,
  });
  assert.equal(TOKEN_USAGE_FORMULA, "input_tokens+output_tokens+cache_creation_input_tokens+cache_read_input_tokens");
  assert.deepEqual(usage, {
    schema_version: 1,
    input_tokens: 10,
    output_tokens: 20,
    cache_creation_input_tokens: 30,
    cache_read_input_tokens: 40,
    total_tokens: 100,
    cost_usd: 0.5,
  });
  assert.equal(isCompleteUsage(usage), true);
});

test("complete usage rejects omitted cache components and inconsistent totals", () => {
  assert.equal(isCompleteUsage({ schema_version: 1, input_tokens: 10, output_tokens: 20, total_tokens: 30, cost_usd: 0 }), false);
  assert.equal(isCompleteUsage({
    schema_version: 1,
    input_tokens: 10,
    output_tokens: 20,
    cache_creation_input_tokens: 30,
    cache_read_input_tokens: 40,
    total_tokens: 30,
    cost_usd: 0,
  }), false);
});

test("usage aggregation includes cache creation and cache reads", () => {
  const total = sumUsage([
    normalizeUsage({ input_tokens: 1, output_tokens: 2, cache_creation_input_tokens: 3, cache_read_input_tokens: 4, cost_usd: 0.1 }),
    normalizeUsage({ input_tokens: 5, output_tokens: 6, cache_creation_input_tokens: 7, cache_read_input_tokens: 8, cost_usd: 0.2 }),
  ]);
  assert.deepEqual(total, {
    schema_version: 1,
    input_tokens: 6,
    output_tokens: 8,
    cache_creation_input_tokens: 10,
    cache_read_input_tokens: 12,
    total_tokens: 36,
    cost_usd: 0.3,
  });
});
