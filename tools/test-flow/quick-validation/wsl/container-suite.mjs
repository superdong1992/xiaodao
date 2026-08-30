#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { canonicalJson, sha256Bytes, sha256File } from "../../lib/util.mjs";
import { failureDomain, aggregateUsage } from "../standalone-suite.mjs";
import { FAST_E2E_SCENARIOS } from "../fast-e2e-scenarios.mjs";

export const WSL_CONTAINER_SUITE_SCENARIOS = Object.freeze([...FAST_E2E_SCENARIOS]);
export const WSL_CONTAINER_SUITE_EXPECTED_CALLS = 16;
export const WSL_CONTAINER_SUITE_CALL_HARD_CAP = 32;

const EXPECTED_CALLS_PER_SCENARIO = 2;
const CALL_HARD_CAP_PER_SCENARIO = 4;
const PROVIDERS = Object.freeze({
  "codex-luna": Object.freeze({
    framework: "macos-codex-luna-fast-e2e",
    entry: "tools/test-flow/quick-validation/codex-luna/run.mjs",
    count_field: "model_calls",
    expected_field: "expected_model_calls",
    hard_cap_field: "model_call_hard_cap",
    wall_field: "wall_timeout_seconds",
  }),
  "claude-deepseek": Object.freeze({
    framework: "macos-claude-deepseek-quick-validation",
    entry: "tools/test-flow/quick-validation/claude-deepseek/run.mjs",
    count_field: "model_processes",
    expected_field: "expected_model_processes",
    hard_cap_field: "model_process_hard_cap",
    wall_field: "stage_wall_seconds",
  }),
});

class ContainerSuiteError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "ContainerSuiteError";
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) {
  throw new ContainerSuiteError(code, message, details);
}

function requireSuite(condition, code, message, details = {}) {
  if (!condition) fail(code, message, details);
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function readJson(file, code) {
  try {
    const value = JSON.parse(fs.readFileSync(file, "utf8"));
    requireSuite(isPlainObject(value), code, "JSON root must be an object", { file });
    return value;
  } catch (error) {
    if (error instanceof ContainerSuiteError) throw error;
    fail(code, "JSON file is missing or invalid", { file, cause: error?.code ?? "INVALID_JSON" });
  }
}

function writeJsonExclusive(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  fs.writeFileSync(file, `${canonicalJson(value)}\n`, {
    encoding: "utf8",
    mode: 0o600,
    flag: "wx",
  });
}

function providerContract(provider) {
  const contract = PROVIDERS[provider];
  requireSuite(
    contract !== undefined,
    "WSL_CONTAINER_SUITE_PROVIDER_INVALID",
    "Provider is outside the Fast E2E container suite",
    { provider },
  );
  return contract;
}

function exactScenarioOrder(value) {
  return Array.isArray(value)
    && value.length === WSL_CONTAINER_SUITE_SCENARIOS.length
    && value.every((scenario, index) => scenario === WSL_CONTAINER_SUITE_SCENARIOS[index]);
}

function expectedCallsForScenario(scenario) {
  return scenario === "insufficient-evidence" ? 0 : EXPECTED_CALLS_PER_SCENARIO;
}

function hardCapForScenario(scenario) {
  return scenario === "insufficient-evidence" ? 0 : CALL_HARD_CAP_PER_SCENARIO;
}

function normalizedSeal(value) {
  requireSuite(
    isPlainObject(value)
      && value.schema_version === 1
      && value.status === "PASS"
      && value.platform === "linux/amd64"
      && value.profile === "ubuntu22.04-central-v1"
      && /^sha256:[a-f0-9]{64}$/u.test(value.image_id),
    "WSL_CONTAINER_SUITE_IMAGE_SEAL_INVALID",
    "Fast E2E requires the frozen Ubuntu 22.04 image seal",
  );
  return {
    schema_version: 1,
    image_id: value.image_id,
    platform: value.platform,
    profile: value.profile,
    status: value.status,
  };
}

function validatePerScenarioPlan(execution, contract) {
  requireSuite(
    Array.isArray(execution.per_scenario)
      && execution.per_scenario.length === WSL_CONTAINER_SUITE_SCENARIOS.length,
    "WSL_CONTAINER_SUITE_PER_SCENARIO_PLAN_INVALID",
    "Provider plan must expose all nine per-scenario call budgets",
  );
  for (const [index, scenario] of WSL_CONTAINER_SUITE_SCENARIOS.entries()) {
    const item = execution.per_scenario[index];
    requireSuite(
      isPlainObject(item)
        && item.scenario_id === scenario
        && item[contract.expected_field] === expectedCallsForScenario(scenario)
        && item[contract.hard_cap_field] === hardCapForScenario(scenario),
      "WSL_CONTAINER_SUITE_PER_SCENARIO_PLAN_INVALID",
      "Provider per-scenario call budget differs from the Fast E2E contract",
      { scenario },
    );
  }
}

export function buildContainerSuitePlan({ provider, providerPlan, imageSeal }) {
  const contract = providerContract(provider);
  requireSuite(
    isPlainObject(providerPlan)
      && providerPlan.framework === contract.framework
      && providerPlan.goal === "fast-e2e"
      && providerPlan.mode === "fast-e2e-suite"
      && providerPlan.scenario === null,
    "WSL_CONTAINER_SUITE_PROVIDER_PLAN_MISMATCH",
    "Provider plan is not the matching standalone Fast E2E suite",
  );
  requireSuite(
    exactScenarioOrder(providerPlan.scenarios),
    "WSL_CONTAINER_SUITE_SCENARIOS_INVALID",
    "Provider plan scenario order differs from the frozen nine-case matrix",
  );
  const execution = providerPlan.execution;
  requireSuite(
    isPlainObject(execution) && execution.entry === contract.entry,
    "WSL_CONTAINER_SUITE_PROVIDER_EXECUTION_INVALID",
    "Provider execution plan is missing or names another entry",
  );
  requireSuite(
    execution[contract.expected_field] === WSL_CONTAINER_SUITE_EXPECTED_CALLS
      && execution[contract.hard_cap_field] === WSL_CONTAINER_SUITE_CALL_HARD_CAP,
    "WSL_CONTAINER_SUITE_MODEL_COUNT_INVALID",
    "Nine-container Fast E2E must freeze 16 normal calls and a 32-call hard cap",
  );
  validatePerScenarioPlan(execution, contract);
  const aggregateWall = execution[contract.wall_field];
  requireSuite(
    Number.isSafeInteger(aggregateWall)
      && aggregateWall > 0
      && aggregateWall % WSL_CONTAINER_SUITE_SCENARIOS.length === 0,
    "WSL_CONTAINER_SUITE_WALL_LIMIT_INVALID",
    "Provider suite wall limit cannot be projected onto nine containers",
  );
  const seal = normalizedSeal(imageSeal);
  const core = {
    schema_version: 1,
    framework: `wsl-${provider}-nine-container-fast-e2e`,
    goal: "fast-e2e",
    mode: "fast-e2e-container-suite",
    scenario: null,
    scenarios: [...WSL_CONTAINER_SUITE_SCENARIOS],
    source_snapshot: false,
    release_verdict: false,
    execution: {
      entry: "tools/test-flow/quick-validation/wsl/run.sh",
      provider_entry: execution.entry,
      topology: "NINE_ISOLATED_CONTAINERS",
      container_count: WSL_CONTAINER_SUITE_SCENARIOS.length,
      max_concurrency: WSL_CONTAINER_SUITE_SCENARIOS.length,
      scenarios_per_container: 1,
      fixed_aggregate_order: true,
      automatic_retry: false,
      history_reuse: false,
      central_goal: false,
      expected_model_activity: WSL_CONTAINER_SUITE_EXPECTED_CALLS,
      model_activity_hard_cap: WSL_CONTAINER_SUITE_CALL_HARD_CAP,
      per_scenario: WSL_CONTAINER_SUITE_SCENARIOS.map((scenario) => ({
        scenario_id: scenario,
        expected_model_activity: expectedCallsForScenario(scenario),
        model_activity_hard_cap: hardCapForScenario(scenario),
      })),
      model_activity_unit: contract.count_field,
      per_container_wall_seconds: aggregateWall / WSL_CONTAINER_SUITE_SCENARIOS.length,
      suite_wall_seconds: aggregateWall / WSL_CONTAINER_SUITE_SCENARIOS.length,
      image_seal: seal,
    },
    provider_plan_sha256: providerPlan.plan_sha256,
    provider_plan: providerPlan,
    admission: providerPlan.admission,
  };
  return { ...core, plan_sha256: sha256Bytes(canonicalJson(core)) };
}

function readTimestamp(file) {
  if (!fs.existsSync(file)) return null;
  const value = fs.readFileSync(file, "utf8").trim();
  return Number.isFinite(Date.parse(value)) ? new Date(value).toISOString() : null;
}

function runtimeReceipt(suiteRoot, scenario) {
  const runtimeRoot = path.join(suiteRoot, "evidence", "container-runtime", scenario);
  const readText = (name) => {
    const file = path.join(runtimeRoot, name);
    return fs.existsSync(file) ? fs.readFileSync(file, "utf8").trim() : null;
  };
  const exitText = readText("exit-code.txt");
  const exitCode = exitText !== null && /^\d+$/u.test(exitText) ? Number(exitText) : null;
  const logs = ["stdout.txt", "stderr.txt"].flatMap((name) => {
    const file = path.join(runtimeRoot, name);
    return fs.existsSync(file)
      ? [{
          name,
          path: path.posix.join("evidence", "container-runtime", scenario, name),
          sha256: sha256File(file),
          size: fs.statSync(file).size,
        }]
      : [];
  });
  return {
    scenario_id: scenario,
    container_name: readText("container-name.txt"),
    exit_code: exitCode,
    logs,
  };
}

export function materializeContainerSuite({ suiteRoot, write = true }) {
  const root = path.resolve(suiteRoot);
  fs.mkdirSync(path.join(root, "scenarios"), { recursive: true, mode: 0o700 });
  const results = [];
  for (const scenario of WSL_CONTAINER_SUITE_SCENARIOS) {
    const rawRoot = path.join(root, ".children", scenario);
    const finalRoot = path.join(root, "scenarios", scenario);
    const candidates = fs.existsSync(rawRoot)
      ? fs.readdirSync(rawRoot, { withFileTypes: true })
          .filter((entry) => entry.isDirectory())
          .map((entry) => path.join(rawRoot, entry.name))
      : [];
    if (candidates.length !== 1 || fs.existsSync(finalRoot)) {
      results.push({
        scenario_id: scenario,
        status: "ERROR",
        code: candidates.length !== 1
          ? "CHILD_RUN_CARDINALITY_INVALID"
          : "CHILD_DESTINATION_EXISTS",
        candidate_count: candidates.length,
      });
      continue;
    }
    try {
      fs.renameSync(candidates[0], finalRoot);
      results.push({
        scenario_id: scenario,
        status: "PASS",
        code: null,
        candidate_count: 1,
        verdict_present: fs.existsSync(path.join(finalRoot, "verdict.json")),
      });
    } catch (error) {
      results.push({
        scenario_id: scenario,
        status: "ERROR",
        code: error?.code ?? "CHILD_RUN_RENAME_FAILED",
        candidate_count: 1,
      });
    }
  }
  const receipt = {
    schema_version: 1,
    status: results.every((item) => item.status === "PASS" && item.verdict_present)
      ? "PASS"
      : "FAIL",
    operation: "ROOT_UTILITY_ATOMIC_RENAME",
    results,
  };
  if (write) {
    writeJsonExclusive(
      path.join(root, "evidence", "container-runtime", "materialization.json"),
      receipt,
    );
  }
  requireSuite(
    receipt.status === "PASS",
    "WSL_CONTAINER_SUITE_MATERIALIZATION_FAILED",
    "One or more scenario runs could not be materialized",
  );
  return receipt;
}

function unsealedResult(scenario, failure, countField) {
  return {
    scenario_id: scenario,
    status: "ERROR",
    failure_domain: "ENGINEERING",
    [countField]: null,
    usage: null,
    failure,
    verdict: null,
  };
}

function childFailureDomain(child) {
  if (child.status === "PASS") return null;
  if (child.status === "BLOCKED") return "ENGINEERING";
  return ["CONTRACT", "ENGINEERING"].includes(child.failure_domain)
    ? child.failure_domain
    : failureDomain(child.failure);
}

function childCountValid(child, count, scenario) {
  const expected = expectedCallsForScenario(scenario);
  const hardCap = hardCapForScenario(scenario);
  return isPlainObject(count)
    && count.expected === expected
    && count.retry_count === 0
    && (
      count.actual === null
      || (Number.isSafeInteger(count.actual)
        && count.actual >= 0
        && count.actual <= hardCap)
    )
    && (child.status !== "PASS"
      || (Number.isSafeInteger(count.actual) && count.actual >= expected));
}

export function aggregateContainerSuite({ provider, suiteRoot, write = true }) {
  const contract = providerContract(provider);
  const root = path.resolve(suiteRoot);
  const plan = readJson(path.join(root, "plan.json"), "WSL_CONTAINER_SUITE_PLAN_INVALID");
  requireSuite(
    plan.framework === `wsl-${provider}-nine-container-fast-e2e`
      && plan.goal === "fast-e2e"
      && plan.mode === "fast-e2e-container-suite"
      && plan.source_snapshot === false
      && plan.release_verdict === false
      && exactScenarioOrder(plan.scenarios)
      && plan.execution?.expected_model_activity === WSL_CONTAINER_SUITE_EXPECTED_CALLS
      && plan.execution?.model_activity_hard_cap === WSL_CONTAINER_SUITE_CALL_HARD_CAP,
    "WSL_CONTAINER_SUITE_PLAN_MISMATCH",
    "Aggregate plan does not match the selected Fast E2E suite",
  );
  const blocked = plan.admission?.status !== "READY";
  const scenarioResults = [];
  const references = [];
  const receipts = [];
  const engineeringFailures = [];

  if (!blocked) {
    for (const scenario of WSL_CONTAINER_SUITE_SCENARIOS) {
      const receipt = runtimeReceipt(root, scenario);
      receipts.push(receipt);
      const verdictPath = path.join(root, "scenarios", scenario, "verdict.json");
      if (!fs.existsSync(verdictPath)) {
        const failure = {
          code: "WSL_CONTAINER_CHILD_VERDICT_MISSING",
          message: "Scenario container did not seal verdict.json",
          scenario_id: scenario,
          exit_code: receipt.exit_code,
        };
        engineeringFailures.push(failure);
        scenarioResults.push(unsealedResult(scenario, failure, contract.count_field));
        continue;
      }
      let child;
      try {
        child = readJson(verdictPath, "WSL_CONTAINER_CHILD_VERDICT_INVALID");
      } catch (error) {
        const failure = { code: error.code, message: error.message, scenario_id: scenario };
        engineeringFailures.push(failure);
        scenarioResults.push(unsealedResult(scenario, failure, contract.count_field));
        continue;
      }
      const identityValid = child.goal === "fast-e2e"
        && child.mode === "fast-e2e"
        && child.scenario === scenario
        && child.source_snapshot === false
        && ["PASS", "FAIL", "BLOCKED"].includes(child.status);
      const count = child[contract.count_field];
      const exitMatches = (child.status === "PASS" && receipt.exit_code === 0)
        || (child.status === "FAIL" && receipt.exit_code === 1)
        || (child.status === "BLOCKED" && receipt.exit_code === 2);
      let domain = childFailureDomain(child);
      let failure = child.failure ?? null;
      if (!identityValid || !childCountValid(child, count, scenario) || !exitMatches) {
        domain = "ENGINEERING";
        failure = {
          code: "WSL_CONTAINER_CHILD_RECEIPT_MISMATCH",
          message: "Scenario verdict, call count, and container exit code do not reconcile",
          scenario_id: scenario,
          exit_code: receipt.exit_code,
        };
      }
      const reference = {
        scenario_id: scenario,
        status: child.status,
        failure_domain: domain,
        [contract.count_field]: count ?? null,
        usage: child.usage ?? null,
        failure,
        verdict: {
          path: path.posix.join("scenarios", scenario, "verdict.json"),
          sha256: sha256File(verdictPath),
        },
      };
      references.push(reference);
      scenarioResults.push(reference);
      if (domain === "ENGINEERING") {
        engineeringFailures.push({
          scenario_id: scenario,
          ...(failure ?? { code: "WSL_CONTAINER_CHILD_ENGINEERING_FAILURE" }),
        });
      }
    }
  } else {
    for (const scenario of WSL_CONTAINER_SUITE_SCENARIOS) {
      scenarioResults.push({
        scenario_id: scenario,
        status: "NOT_RUN",
        failure_domain: null,
        [contract.count_field]: null,
        usage: null,
        failure: null,
        verdict: null,
      });
    }
  }

  const actual = references.reduce(
    (sum, item) => sum + (Number.isSafeInteger(item[contract.count_field]?.actual)
      ? item[contract.count_field].actual
      : 0),
    0,
  );
  const actualComplete = references.length === WSL_CONTAINER_SUITE_SCENARIOS.length
    && references.every((item) => Number.isSafeInteger(item[contract.count_field]?.actual));
  const startedAt = readTimestamp(path.join(root, "started-at.txt")) ?? new Date().toISOString();
  const finishedAt = new Date().toISOString();
  const status = blocked
    ? "BLOCKED"
    : engineeringFailures.length > 0
      ? "ERROR"
      : references.length === WSL_CONTAINER_SUITE_SCENARIOS.length
          && references.every((item) => item.status === "PASS")
        ? "PASS"
        : "FAIL";
  const runId = fs.existsSync(path.join(root, "run-id.txt"))
    ? fs.readFileSync(path.join(root, "run-id.txt"), "utf8").trim()
    : path.basename(root);
  requireSuite(
    /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/u.test(runId),
    "WSL_CONTAINER_SUITE_RUN_ID_INVALID",
    "Fast E2E suite run ID is invalid",
  );
  const activity = {
    expected: WSL_CONTAINER_SUITE_EXPECTED_CALLS,
    hard_cap: WSL_CONTAINER_SUITE_CALL_HARD_CAP,
    actual,
    actual_complete: actualComplete,
    retry_count: 0,
  };
  const attempted = blocked ? 0 : receipts.filter((item) => item.exit_code !== null).length;
  const verdict = {
    schema_version: 1,
    framework: plan.framework,
    run_id: runId,
    goal: "fast-e2e",
    mode: "fast-e2e-container-suite",
    scenario: null,
    scenario_order: [...WSL_CONTAINER_SUITE_SCENARIOS],
    status,
    started_at_utc: startedAt,
    finished_at_utc: finishedAt,
    elapsed_seconds: Math.max(0, (Date.parse(finishedAt) - Date.parse(startedAt)) / 1000),
    plan_sha256: plan.plan_sha256,
    source_snapshot: false,
    release_verdict: false,
    container_scheduling: plan.execution,
    [contract.count_field]: activity,
    usage: aggregateUsage(references.map((item) => item.usage)),
    summary: {
      expected: WSL_CONTAINER_SUITE_SCENARIOS.length,
      completed: references.length,
      attempted,
      passed: references.filter((item) => item.status === "PASS" && item.failure_domain === null).length,
      failed: references.filter((item) => item.status === "FAIL" && item.failure_domain !== "ENGINEERING").length,
      errored: scenarioResults.filter((item) => item.status === "ERROR" || item.failure_domain === "ENGINEERING").length,
      not_run: scenarioResults.filter((item) => item.status === "NOT_RUN").length,
    },
    scenarios: scenarioResults,
    container_receipts: receipts,
    engineering_failures: engineeringFailures,
    failure: blocked
      ? { code: "WSL_CONTAINER_SUITE_PLAN_BLOCKED", blockers: plan.admission?.blockers ?? [] }
      : engineeringFailures[0] ?? null,
    failure_domain: blocked
      ? "ADMISSION"
      : engineeringFailures.length > 0 ? "ENGINEERING" : null,
  };
  if (write) writeJsonExclusive(path.join(root, "verdict.json"), verdict);
  return verdict;
}

function parseCli(argv) {
  const command = argv[0];
  requireSuite(
    ["plan", "admission", "materialize", "aggregate"].includes(command),
    "WSL_CONTAINER_SUITE_COMMAND_INVALID",
    "Command must be plan, admission, materialize, or aggregate",
  );
  const values = {};
  for (let index = 1; index < argv.length; index += 2) {
    const token = argv[index];
    requireSuite(
      token?.startsWith("--") && index + 1 < argv.length,
      "WSL_CONTAINER_SUITE_ARGUMENT_INVALID",
      "Container suite arguments must be name/value pairs",
    );
    const name = token.slice(2);
    requireSuite(
      ["provider", "provider-plan", "image-seal", "output", "suite-root", "plan", "display-root"].includes(name)
        && values[name] === undefined,
      "WSL_CONTAINER_SUITE_ARGUMENT_INVALID",
      "Container suite argument is unknown or duplicated",
      { name },
    );
    values[name] = argv[index + 1];
  }
  return { command, values };
}

async function main() {
  const { command, values } = parseCli(process.argv.slice(2));
  if (command === "plan") {
    requireSuite(
      values.provider && values["provider-plan"] && values["image-seal"],
      "WSL_CONTAINER_SUITE_ARGUMENT_REQUIRED",
      "plan requires provider, provider-plan, and image-seal",
    );
    const plan = buildContainerSuitePlan({
      provider: values.provider,
      providerPlan: readJson(
        path.resolve(values["provider-plan"]),
        "WSL_CONTAINER_SUITE_PROVIDER_PLAN_INVALID",
      ),
      imageSeal: readJson(
        path.resolve(values["image-seal"]),
        "WSL_CONTAINER_SUITE_IMAGE_SEAL_INVALID",
      ),
    });
    if (values.output) writeJsonExclusive(path.resolve(values.output), plan);
    process.stdout.write(`${canonicalJson(plan)}\n`);
    return;
  }
  if (command === "admission") {
    requireSuite(
      values.plan,
      "WSL_CONTAINER_SUITE_ARGUMENT_REQUIRED",
      "admission requires plan",
    );
    const plan = readJson(path.resolve(values.plan), "WSL_CONTAINER_SUITE_PLAN_INVALID");
    requireSuite(
      typeof plan.admission?.status === "string",
      "WSL_CONTAINER_SUITE_ADMISSION_INVALID",
      "Plan admission status is missing",
    );
    process.stdout.write(`${canonicalJson(plan.admission)}\n`);
    process.exitCode = plan.admission.status === "READY" ? 0 : 2;
    return;
  }
  if (command === "materialize") {
    requireSuite(
      values["suite-root"],
      "WSL_CONTAINER_SUITE_ARGUMENT_REQUIRED",
      "materialize requires suite-root",
    );
    const receipt = materializeContainerSuite({ suiteRoot: values["suite-root"] });
    process.stdout.write(`${canonicalJson(receipt)}\n`);
    return;
  }
  requireSuite(
    values.provider && values["suite-root"],
    "WSL_CONTAINER_SUITE_ARGUMENT_REQUIRED",
    "aggregate requires provider and suite-root",
  );
  const verdict = aggregateContainerSuite({
    provider: values.provider,
    suiteRoot: values["suite-root"],
  });
  const displayRoot = values["display-root"]
    ? path.normalize(values["display-root"])
    : path.resolve(values["suite-root"]);
  process.stdout.write(`${canonicalJson({
    run_id: verdict.run_id,
    status: verdict.status,
    verdict: path.join(displayRoot, "verdict.json"),
  })}\n`);
  process.exitCode = verdict.status === "PASS"
    ? 0
    : verdict.status === "BLOCKED" ? 2 : 1;
}

const MODULE_PATH = fileURLToPath(import.meta.url);
if (process.argv[1] && path.resolve(process.argv[1]) === MODULE_PATH) {
  main().catch((error) => {
    process.stderr.write(`${canonicalJson({
      code: error?.code ?? "WSL_CONTAINER_SUITE_FAILED",
      message: error?.message ?? "Fast E2E container suite failed",
    })}\n`);
    process.exitCode = 2;
  });
}
