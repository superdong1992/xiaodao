function median(values) {
  const ordered = [...values].sort((left, right) => left - right);
  const middle = Math.floor(ordered.length / 2);
  return ordered.length % 2 === 1 ? ordered[middle] : (ordered[middle - 1] + ordered[middle]) / 2;
}

export function classifyRun({ functional, performance, operation }, mapping = { pass: 0, pass_with_warnings: 0, fail: 1, blocked: 2, error: 3 }) {
  if (operation === "ERROR") return { overall: "ERROR", exit_code: mapping.error };
  if (functional === "FAIL") return { overall: "FAIL", exit_code: mapping.fail };
  if (performance === "FAIL") return { overall: "FAIL", exit_code: mapping.fail };
  if (functional !== "PASS") return { overall: "BLOCKED", exit_code: mapping.blocked };
  if (["SLOW", "NOT_CALIBRATED"].includes(performance)) return { overall: "PASS_WITH_WARNINGS", exit_code: mapping.pass_with_warnings };
  return { overall: "PASS", exit_code: mapping.pass };
}

export function performanceThreshold(samples, { external = false, policy = null } = {}) {
  const effective = policy ?? {
    window: 10,
    min_samples: 5,
    mad_multiplier: 6,
    relative_floor: 1.25,
    local_absolute_floor_seconds: 5,
    external_absolute_floor_seconds: 30,
  };
  const window = samples.slice(-effective.window);
  if (window.length < effective.min_samples) return null;
  const center = median(window);
  const deviation = median(window.map((value) => Math.abs(value - center)));
  const absolute = external ? effective.external_absolute_floor_seconds : effective.local_absolute_floor_seconds;
  const threshold = Math.max(
    center + effective.mad_multiplier * deviation,
    center * effective.relative_floor,
    center + absolute,
  );
  return {
    sample_count: window.length,
    median_seconds: center,
    mad_seconds: deviation,
    threshold_seconds: threshold,
  };
}

export function assessPerformance(elapsedSeconds, samples, { external = false, policy = null } = {}) {
  const baseline = performanceThreshold(samples, { external, policy });
  if (!baseline) return { status: "NOT_CALIBRATED", baseline: null };
  return {
    status: elapsedSeconds > baseline.threshold_seconds ? "SLOW" : "PASS",
    baseline,
  };
}

export function adjudicateStagePerformance({
  elapsedSeconds,
  samples,
  stage,
  effect,
  policy,
  priorConsecutiveSlow = 0,
}) {
  const stagePolicy = policy.stages[stage.id] ?? policy.stages["*"];
  if (stagePolicy.hard_cap_seconds !== null && elapsedSeconds > stagePolicy.hard_cap_seconds) {
    return {
      status: "FAIL",
      reason: "HARD_CAP_EXCEEDED",
      hard_cap_seconds: stagePolicy.hard_cap_seconds,
      baseline: null,
      consecutive_significant_regressions: 0,
    };
  }
  const external = ["external", "real"].includes(stage.progress_class);
  const assessment = assessPerformance(elapsedSeconds, samples, { external, policy });
  if (assessment.status !== "SLOW") return { ...assessment, reason: null, consecutive_significant_regressions: 0 };
  const consecutive = priorConsecutiveSlow + 1;
  if (effect === "gate" && stagePolicy.mode === "gate" && consecutive >= policy.consecutive_release_failures) {
    return { ...assessment, status: "FAIL", reason: "CONSECUTIVE_SIGNIFICANT_REGRESSION", consecutive_significant_regressions: consecutive };
  }
  return { ...assessment, reason: "SIGNIFICANT_REGRESSION", consecutive_significant_regressions: consecutive };
}
