import { assertFlow } from "./util.mjs";

export const OVERALL = Object.freeze({
  PASS: "PASS",
  PASS_WITH_WARNINGS: "PASS_WITH_WARNINGS",
  FAIL: "FAIL",
  BLOCKED: "BLOCKED",
  ERROR: "ERROR",
  UNFINALIZED: "UNFINALIZED",
});

export const FAILURE_DOMAINS = new Set([
  "PRODUCT",
  "CONTRACT",
  "HARNESS",
  "EXTERNAL",
  "INFRA",
  "SECURITY",
]);

export function classifyRun({ functional, performance, operation }) {
  assertFlow(["PASS", "FAIL", "INCONCLUSIVE", "NOT_RUN"].includes(functional), "STATUS_FUNCTIONAL", `Invalid functional status ${functional}`);
  assertFlow(["PASS", "SLOW", "FAIL", "NOT_CALIBRATED", "NOT_RUN"].includes(performance), "STATUS_PERFORMANCE", `Invalid performance status ${performance}`);
  assertFlow(["PASS", "ERROR", "BLOCKED"].includes(operation), "STATUS_OPERATION", `Invalid operation status ${operation}`);

  if (operation === "ERROR") return { overall: OVERALL.ERROR, exit_code: 3 };
  if (functional === "FAIL" || performance === "FAIL") return { overall: OVERALL.FAIL, exit_code: 1 };
  if (operation === "BLOCKED" || functional === "INCONCLUSIVE" || functional === "NOT_RUN") {
    return { overall: OVERALL.BLOCKED, exit_code: 2 };
  }
  if (performance === "SLOW" || performance === "NOT_CALIBRATED") {
    return { overall: OVERALL.PASS_WITH_WARNINGS, exit_code: 0 };
  }
  return { overall: OVERALL.PASS, exit_code: 0 };
}

export function median(numbers) {
  if (numbers.length === 0) return null;
  const values = [...numbers].sort((left, right) => left - right);
  const middle = Math.floor(values.length / 2);
  return values.length % 2 === 0 ? (values[middle - 1] + values[middle]) / 2 : values[middle];
}

export function performanceThreshold(samples, { external = false } = {}) {
  if (samples.length < 5) return null;
  const window = samples.slice(-10);
  const center = median(window);
  const deviation = median(window.map((sample) => Math.abs(sample - center)));
  const absoluteFloor = external ? 30 : 5;
  return {
    sample_count: window.length,
    median_seconds: center,
    mad_seconds: deviation,
    threshold_seconds: center + Math.max(3 * deviation, 0.2 * center, absoluteFloor),
  };
}

export function assessPerformance(elapsedSeconds, samples, options = {}) {
  const baseline = performanceThreshold(samples, options);
  if (!baseline) return { status: "NOT_CALIBRATED", baseline: null };
  return {
    status: elapsedSeconds > baseline.threshold_seconds ? "SLOW" : "PASS",
    baseline,
    elapsed_seconds: elapsedSeconds,
  };
}
