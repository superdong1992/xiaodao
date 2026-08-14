export const TOKEN_USAGE_SCHEMA_VERSION = 1;
export const TOKEN_USAGE_FORMULA = "input_tokens+output_tokens+cache_creation_input_tokens+cache_read_input_tokens";
export const TOKEN_USAGE_FIELDS = Object.freeze([
  "input_tokens",
  "output_tokens",
  "cache_creation_input_tokens",
  "cache_read_input_tokens",
]);

function nonNegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function roundedCost(value) {
  return Math.round(value * 1_000_000) / 1_000_000;
}

export function tokenTotal(value) {
  return TOKEN_USAGE_FIELDS.reduce((total, name) => total + Number(value?.[name] ?? 0), 0);
}

export function zeroUsage() {
  return {
    schema_version: TOKEN_USAGE_SCHEMA_VERSION,
    input_tokens: 0,
    output_tokens: 0,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    total_tokens: 0,
    cost_usd: 0,
  };
}

export function normalizeUsage(value = {}) {
  const usage = {
    schema_version: TOKEN_USAGE_SCHEMA_VERSION,
    input_tokens: Number(value?.input_tokens ?? 0),
    output_tokens: Number(value?.output_tokens ?? 0),
    cache_creation_input_tokens: Number(value?.cache_creation_input_tokens ?? 0),
    cache_read_input_tokens: Number(value?.cache_read_input_tokens ?? 0),
    total_tokens: 0,
    cost_usd: Number(value?.cost_usd ?? 0),
  };
  if (!TOKEN_USAGE_FIELDS.every((name) => nonNegativeInteger(usage[name])) || !Number.isFinite(usage.cost_usd) || usage.cost_usd < 0) {
    throw new Error("TOKEN_USAGE_INVALID");
  }
  usage.total_tokens = tokenTotal(usage);
  usage.cost_usd = roundedCost(usage.cost_usd);
  if (value?.schema_version !== undefined && value.schema_version !== TOKEN_USAGE_SCHEMA_VERSION) throw new Error("TOKEN_USAGE_SCHEMA_UNSUPPORTED");
  if (value?.total_tokens !== undefined && value.total_tokens !== usage.total_tokens) throw new Error("TOKEN_USAGE_TOTAL_MISMATCH");
  return usage;
}

export function isCompleteUsage(value) {
  if (value?.schema_version !== TOKEN_USAGE_SCHEMA_VERSION) return false;
  if (!TOKEN_USAGE_FIELDS.every((name) => nonNegativeInteger(value[name]))) return false;
  if (!nonNegativeInteger(value.total_tokens) || value.total_tokens !== tokenTotal(value)) return false;
  return Number.isFinite(value.cost_usd) && value.cost_usd >= 0;
}

export function sumUsage(values) {
  const total = zeroUsage();
  for (const value of values) {
    const usage = normalizeUsage(value);
    for (const name of TOKEN_USAGE_FIELDS) total[name] += usage[name];
    total.cost_usd = roundedCost(total.cost_usd + usage.cost_usd);
  }
  total.total_tokens = tokenTotal(total);
  return total;
}
