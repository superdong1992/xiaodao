#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { canonicalJson, sha256Bytes, sha256File } from "../../lib/util.mjs";
import { aggregateUsage, failureDomain } from "../standalone-suite.mjs";

export const WSL_CONTAINER_SUITE_SCENARIOS = Object.freeze([
  "api-execution-overrun",
  "client-receive-blocked",
  "deadloop-detected",
  "insufficient-evidence",
  "multiple-rpc-timeouts",
  "server-queue-delay",
  "server-queue-five",
  "server-queue-single",
  "unrelated-log-noise",
]);

const PROVIDERS = Object.freeze({
  "codex-luna": Object.freeze({
    framework: "macos-codex-luna-fast-e2e",
    count_field: "model_calls",
    expected_field: "expected_model_calls",
    wall_field: "wall_timeout_seconds",
  }),
  "claude-deepseek": Object.freeze({
    framework: "macos-claude-deepseek-quick-validation",
    count_field: "model_processes",
    expected_field: "expected_model_processes",
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

function providerContract(provider) {
  const contract = PROVIDERS[provider];
  requireSuite(contract !== undefined, "WSL_CONTAINER_SUITE_PROVIDER_INVALID", "Provider is outside the closed container suite", { provider });
  return contract;
}

function exactScenarioOrder(value) {
  return Array.isArray(value)
    && value.length === WSL_CONTAINER_SUITE_SCENARIOS.length
    && value.every((scenario, index) => scenario === WSL_CONTAINER_SUITE_SCENARIOS[index]);
}

function normalizedSeal(value) {
  requireSuite(
    isPlainObject(value)
      && value.schema_version === 1
      && value.status === "PASS"
      && value.platform === "linux/amd64"
      && value.profile === "ubuntu22.04-central-v1"
      && /^sha256:[a-f0-9]{64}$/.test(value.image_id),
    "WSL_CONTAINER_SUITE_IMAGE_SEAL_INVALID",
    "Container suite requires the frozen Ubuntu 22.04 image seal",
  );
  return {
    schema_version: value.schema_version,
    image_id: value.image_id,
    platform: value.platform,
    profile: value.profile,
    status: value.status,
  };
}

export function buildContainerSuitePlan({ provider, providerPlan, imageSeal }) {
  const contract = providerContract(provider);
  requireSuite(isPlainObject(providerPlan), "WSL_CONTAINER_SUITE_PROVIDER_PLAN_INVALID", "Provider plan is invalid");
  requireSuite(providerPlan.framework === contract.framework && providerPlan.mode === "e2e-suite", "WSL_CONTAINER_SUITE_PROVIDER_PLAN_MISMATCH", "Provider plan is not the matching nine-scenario E2E suite");
  requireSuite(exactScenarioOrder(providerPlan.scenarios), "WSL_CONTAINER_SUITE_SCENARIOS_INVALID", "Provider plan scenario order differs from the frozen nine-case matrix");
  requireSuite(isPlainObject(providerPlan.execution), "WSL_CONTAINER_SUITE_PROVIDER_EXECUTION_INVALID", "Provider execution plan is missing");
  const expected = providerPlan.execution[contract.expected_field];
  requireSuite(expected === 44, "WSL_CONTAINER_SUITE_MODEL_COUNT_INVALID", "Nine-container suite must freeze 44 logical model activities", { actual: expected });
  const aggregateWall = providerPlan.execution[contract.wall_field];
  requireSuite(Number.isSafeInteger(aggregateWall) && aggregateWall > 0 && aggregateWall % WSL_CONTAINER_SUITE_SCENARIOS.length === 0, "WSL_CONTAINER_SUITE_WALL_LIMIT_INVALID", "Provider suite wall limit cannot be projected onto nine containers");
  const seal = normalizedSeal(imageSeal);
  const core = {
    schema_version: 1,
    framework: `wsl-${provider}-nine-container-fast-e2e`,
    goal: providerPlan.goal,
    mode: "e2e-container-suite",
    scenario: null,
    scenarios: [...WSL_CONTAINER_SUITE_SCENARIOS],
    execution: {
      entry: "tools/test-flow/quick-validation/wsl/run.sh",
      provider_entry: providerPlan.execution.entry,
      topology: "NINE_ISOLATED_CONTAINERS",
      container_count: WSL_CONTAINER_SUITE_SCENARIOS.length,
      max_concurrency: WSL_CONTAINER_SUITE_SCENARIOS.length,
      scenarios_per_container: 1,
      launch_barrier: "ALL_PROVIDER_PLANS_AND_SHARED_CONTRACTS_PASS",
      engineering_failure_policy: "FINISH_ALL_STARTED_CONTAINERS",
      fixed_aggregate_order: true,
      automatic_retry: false,
      history_reuse: false,
      image_seal: seal,
      model_activity_unit: contract.count_field,
      expected_model_activity: expected,
      token_cap: providerPlan.execution.token_cap,
      equivalent_usd_cap: providerPlan.execution.equivalent_usd_cap ?? providerPlan.execution.usd_cap,
      per_container_wall_seconds: aggregateWall / WSL_CONTAINER_SUITE_SCENARIOS.length,
      suite_wall_seconds: aggregateWall / WSL_CONTAINER_SUITE_SCENARIOS.length,
    },
    provider_plan: providerPlan,
    provider_plan_sha256: providerPlan.plan_sha256,
    admission: providerPlan.admission,
  };
  return { ...core, plan_sha256: sha256Bytes(canonicalJson(core)) };
}

function runtimeReceipt(suiteRoot, scenario) {
  const runtimeRoot = path.join(suiteRoot, "evidence", "container-runtime", scenario);
  const readText = (name) => {
    const file = path.join(runtimeRoot, name);
    return fs.existsSync(file) ? fs.readFileSync(file, "utf8").trim() : null;
  };
  const exitText = readText("exit-code.txt");
  const exitCode = exitText !== null && /^\d+$/.test(exitText) ? Number(exitText) : null;
  const files = ["stdout.txt", "stderr.txt"].flatMap((name) => {
    const file = path.join(runtimeRoot, name);
    return fs.existsSync(file) ? [{ name, path: path.posix.join("evidence", "container-runtime", scenario, name), sha256: sha256File(file), size: fs.statSync(file).size }] : [];
  });
  return {
    scenario_id: scenario,
    container_name: readText("container-name.txt"),
    exit_code: exitCode,
    logs: files,
  };
}

export function runContainerSuitePreflight({ provider, output }) {
  providerContract(provider);
  const moduleRoot = path.dirname(fileURLToPath(import.meta.url));
  const testFlowRoot = path.resolve(moduleRoot, "..", "..");
  const repoRoot = path.resolve(testFlowRoot, "..", "..");
  const files = provider === "codex-luna"
    ? [
        path.join(testFlowRoot, "quick-validation", "standalone-suite.test.mjs"),
        path.join(testFlowRoot, "quick-validation", "wsl", "container-suite.test.mjs"),
        path.join(testFlowRoot, "tests", "wsl-quick-validation.test.mjs"),
        ...fs.readdirSync(path.join(testFlowRoot, "quick-validation", "codex-luna", "tests"))
          .filter((name) => name.endsWith(".test.mjs"))
          .map((name) => path.join(testFlowRoot, "quick-validation", "codex-luna", "tests", name)),
      ]
    : [
        path.join(testFlowRoot, "quick-validation", "standalone-suite.test.mjs"),
        path.join(testFlowRoot, "quick-validation", "wsl", "container-suite.test.mjs"),
        path.join(testFlowRoot, "tests", "wsl-quick-validation.test.mjs"),
        ...[
          "claude-deepseek-contract.test.mjs",
          "claude-deepseek-process.test.mjs",
          "claude-deepseek-bash-policy.test.mjs",
          "claude-deepseek-service-wrapper.test.mjs",
          "claude-deepseek-e2e-runner.test.mjs",
        ].map((name) => path.join(testFlowRoot, "quick-validation", "claude-deepseek", "tests", name)),
      ];
  const result = spawnSync(process.execPath, ["--test", "--test-reporter=tap", ...files], {
    cwd: repoRoot,
    env: process.env,
    encoding: "utf8",
    timeout: 180_000,
    stdio: ["ignore", "pipe", "pipe"],
  });
  fs.mkdirSync(path.dirname(output), { recursive: true, mode: 0o700 });
  fs.writeFileSync(output, `${result.stdout}${result.stderr}`, { encoding: "utf8", mode: 0o600, flag: "wx" });
  requireSuite(result.status === 0 && result.signal === null && !result.error, "WSL_CONTAINER_SUITE_PREFLIGHT_FAILED", "Shared deterministic container-suite preflight failed", { provider });
  return { schema_version: 1, status: "PASS", provider, test_count: files.length, evidence: { path: output, sha256: sha256File(output), size: fs.statSync(output).size } };
}

export function materializeContainerSuite({ suiteRoot, write = true }) {
  const root = path.resolve(suiteRoot);
  fs.mkdirSync(path.join(root, "scenarios"), { recursive: true, mode: 0o700 });
  const results = [];
  for (const scenario of WSL_CONTAINER_SUITE_SCENARIOS) {
    const rawRoot = path.join(root, ".children", scenario);
    const finalRoot = path.join(root, "scenarios", scenario);
    const candidates = fs.existsSync(rawRoot)
      ? fs.readdirSync(rawRoot, { withFileTypes: true }).filter((entry) => entry.isDirectory()).map((entry) => path.join(rawRoot, entry.name))
      : [];
    if (candidates.length !== 1 || fs.existsSync(finalRoot)) {
      results.push({ scenario_id: scenario, status: "ERROR", code: candidates.length !== 1 ? "CHILD_RUN_CARDINALITY_INVALID" : "CHILD_DESTINATION_EXISTS", candidate_count: candidates.length });
      continue;
    }
    try {
      fs.renameSync(candidates[0], finalRoot);
      results.push({ scenario_id: scenario, status: "PASS", code: null, candidate_count: 1, verdict_present: fs.existsSync(path.join(finalRoot, "verdict.json")) });
    } catch (error) {
      results.push({ scenario_id: scenario, status: "ERROR", code: error?.code ?? "CHILD_RUN_RENAME_FAILED", candidate_count: 1 });
    }
  }
  const receipt = { schema_version: 1, status: results.every((item) => item.status === "PASS" && item.verdict_present) ? "PASS" : "FAIL", operation: "ROOT_UTILITY_ATOMIC_RENAME", results };
  if (write) {
    const destination = path.join(root, "evidence", "container-runtime", "materialization.json");
    fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 });
    writeJsonExclusive(destination, receipt);
  }
  requireSuite(receipt.status === "PASS", "WSL_CONTAINER_SUITE_MATERIALIZATION_FAILED", "One or more scenario runs could not be materialized by the root utility container");
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

export function aggregateContainerSuite({ provider, suiteRoot, write = true }) {
  const contract = providerContract(provider);
  const root = path.resolve(suiteRoot);
  const plan = readJson(path.join(root, "plan.json"), "WSL_CONTAINER_SUITE_PLAN_INVALID");
  requireSuite(plan.framework === `wsl-${provider}-nine-container-fast-e2e` && exactScenarioOrder(plan.scenarios), "WSL_CONTAINER_SUITE_PLAN_MISMATCH", "Aggregate plan does not match the selected provider and scenario matrix");
  const blocked = plan.admission?.status !== "READY";
  const references = [];
  const scenarioResults = [];
  const engineeringFailures = [];
  const receipts = [];
  if (!blocked) {
    for (const scenario of WSL_CONTAINER_SUITE_SCENARIOS) {
      const receipt = runtimeReceipt(root, scenario);
      receipts.push(receipt);
      const verdictPath = path.join(root, "scenarios", scenario, "verdict.json");
      if (!fs.existsSync(verdictPath)) {
        const failure = { code: "WSL_CONTAINER_CHILD_VERDICT_MISSING", message: "Scenario container did not seal verdict.json", scenario_id: scenario, exit_code: receipt.exit_code };
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
      if (child.scenario !== scenario || !["PASS", "FAIL", "BLOCKED"].includes(child.status)) {
        const failure = { code: "WSL_CONTAINER_CHILD_VERDICT_MISMATCH", message: "Scenario container verdict identity or status is invalid", scenario_id: scenario };
        engineeringFailures.push(failure);
        scenarioResults.push(unsealedResult(scenario, failure, contract.count_field));
        continue;
      }
      const count = child[contract.count_field];
      const expectedForScenario = scenario === "insufficient-evidence" ? 4 : 5;
      const exitMatches = (child.status === "PASS" && receipt.exit_code === 0)
        || (child.status === "FAIL" && receipt.exit_code === 1)
        || (child.status === "BLOCKED" && receipt.exit_code === 2);
      let domain = child.status === "PASS" ? null : child.status === "BLOCKED" ? "ENGINEERING" : failureDomain(child.failure);
      let failure = child.failure ?? null;
      const countValid = isPlainObject(count)
        && count.expected === expectedForScenario
        && count.retry_count === 0
        && (Number.isSafeInteger(count.actual) || (child.status === "FAIL" && count.actual === null));
      if (!countValid || !exitMatches) {
        domain = "ENGINEERING";
        failure = { code: "WSL_CONTAINER_CHILD_RECEIPT_MISMATCH", message: "Scenario verdict, model activity, and container exit code do not reconcile", scenario_id: scenario, exit_code: receipt.exit_code };
      }
      const reference = {
        scenario_id: scenario,
        status: child.status,
        failure_domain: domain,
        [contract.count_field]: count ?? null,
        usage: child.usage ?? null,
        failure,
        verdict: { path: path.posix.join("scenarios", scenario, "verdict.json"), sha256: sha256File(verdictPath) },
      };
      references.push(reference);
      scenarioResults.push(reference);
      if (domain === "ENGINEERING") engineeringFailures.push({ scenario_id: scenario, ...(failure ?? { code: "WSL_CONTAINER_CHILD_ENGINEERING_FAILURE" }) });
    }
  }
  if (blocked) {
    for (const scenario of WSL_CONTAINER_SUITE_SCENARIOS) {
      scenarioResults.push({ scenario_id: scenario, status: "NOT_RUN", failure_domain: null, [contract.count_field]: null, usage: null, failure: null, verdict: null });
    }
  }
  const actual = references.reduce((sum, item) => sum + (Number.isSafeInteger(item[contract.count_field]?.actual) ? item[contract.count_field].actual : 0), 0);
  const startedAt = readTimestamp(path.join(root, "started-at.txt")) ?? new Date().toISOString();
  const finishedAt = new Date().toISOString();
  const status = blocked
    ? "BLOCKED"
    : engineeringFailures.length > 0
      ? "ERROR"
      : references.length === WSL_CONTAINER_SUITE_SCENARIOS.length && references.every((item) => item.status === "PASS")
        ? "PASS"
        : "FAIL";
  const activity = { expected: plan.execution.expected_model_activity, actual, retry_count: 0 };
  const attempted = blocked ? 0 : receipts.filter((item) => item.exit_code !== null).length;
  const persistedRunId = fs.existsSync(path.join(root, "run-id.txt")) ? fs.readFileSync(path.join(root, "run-id.txt"), "utf8").trim() : path.basename(root);
  requireSuite(/^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/u.test(persistedRunId), "WSL_CONTAINER_SUITE_RUN_ID_INVALID", "Container suite run ID is invalid");
  const verdict = {
    schema_version: 1,
    framework: plan.framework,
    run_id: persistedRunId,
    goal: plan.goal,
    mode: plan.mode,
    scenario: null,
    scenario_order: [...WSL_CONTAINER_SUITE_SCENARIOS],
    status,
    started_at_utc: startedAt,
    finished_at_utc: finishedAt,
    elapsed_seconds: Math.max(0, (Date.parse(finishedAt) - Date.parse(startedAt)) / 1000),
    plan_sha256: plan.plan_sha256,
    source_snapshot: false,
    container_scheduling: plan.execution,
    [contract.count_field]: activity,
    usage: aggregateUsage(references.map((item) => item.usage)),
    preflight_evidence: evidenceFiles(path.join(root, "evidence", "preflight"), path.join("evidence", "preflight")),
    materialization: fs.existsSync(path.join(root, "evidence", "container-runtime", "materialization.json"))
      ? readJson(path.join(root, "evidence", "container-runtime", "materialization.json"), "WSL_CONTAINER_SUITE_MATERIALIZATION_RECEIPT_INVALID")
      : null,
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
    failure_domain: blocked ? "ADMISSION" : engineeringFailures.length > 0 ? "ENGINEERING" : null,
    stop_reason: engineeringFailures.length > 0
      ? { domain: "ENGINEERING", failure: engineeringFailures[0], completion_policy: attempted === 0 ? "NO_SCENARIOS_STARTED" : "FINISH_ALL_STARTED_CONTAINERS" }
      : null,
  };
  if (write) writeJsonExclusive(path.join(root, "verdict.json"), verdict);
  return verdict;
}

function readTimestamp(file) {
  if (!fs.existsSync(file)) return null;
  const value = fs.readFileSync(file, "utf8").trim();
  return Number.isFinite(Date.parse(value)) ? new Date(value).toISOString() : null;
}

function evidenceFiles(root, relativeRoot) {
  if (!fs.existsSync(root)) return [];
  return fs.readdirSync(root, { withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => {
      const file = path.join(root, entry.name);
      return { name: entry.name, path: path.posix.join(relativeRoot, entry.name), sha256: sha256File(file), size: fs.statSync(file).size };
    })
    .sort((left, right) => left.name.localeCompare(right.name));
}

function writeJsonExclusive(file, value) {
  fs.writeFileSync(file, `${canonicalJson(value)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
}

function parseCli(argv) {
  const command = argv[0];
  requireSuite(["plan", "admission", "preflight", "materialize", "aggregate"].includes(command), "WSL_CONTAINER_SUITE_COMMAND_INVALID", "Command must be plan, admission, preflight, materialize, or aggregate");
  const values = {};
  for (let index = 1; index < argv.length; index += 2) {
    const token = argv[index];
    requireSuite(token?.startsWith("--") && index + 1 < argv.length, "WSL_CONTAINER_SUITE_ARGUMENT_INVALID", "Container suite arguments must be name/value pairs");
    const name = token.slice(2);
    requireSuite(["provider", "provider-plan", "image-seal", "output", "suite-root", "plan", "display-root"].includes(name) && values[name] === undefined, "WSL_CONTAINER_SUITE_ARGUMENT_INVALID", "Container suite argument is unknown or duplicated", { name });
    values[name] = argv[index + 1];
  }
  return { command, values };
}

async function main() {
  const { command, values } = parseCli(process.argv.slice(2));
  if (command === "plan") {
    requireSuite(values.provider && values["provider-plan"] && values["image-seal"], "WSL_CONTAINER_SUITE_ARGUMENT_REQUIRED", "plan requires provider, provider-plan, and image-seal");
    const plan = buildContainerSuitePlan({ provider: values.provider, providerPlan: readJson(path.resolve(values["provider-plan"]), "WSL_CONTAINER_SUITE_PROVIDER_PLAN_INVALID"), imageSeal: readJson(path.resolve(values["image-seal"]), "WSL_CONTAINER_SUITE_IMAGE_SEAL_INVALID") });
    if (values.output) writeJsonExclusive(path.resolve(values.output), plan);
    process.stdout.write(`${canonicalJson(plan)}\n`);
    return;
  }
  if (command === "admission") {
    requireSuite(values.plan, "WSL_CONTAINER_SUITE_ARGUMENT_REQUIRED", "admission requires plan");
    const plan = readJson(path.resolve(values.plan), "WSL_CONTAINER_SUITE_PLAN_INVALID");
    requireSuite(typeof plan.admission?.status === "string", "WSL_CONTAINER_SUITE_ADMISSION_INVALID", "Plan admission status is missing");
    process.stdout.write(`${canonicalJson(plan.admission)}\n`);
    process.exitCode = plan.admission.status === "READY" ? 0 : 2;
    return;
  }
  if (command === "preflight") {
    requireSuite(values.provider && values.output, "WSL_CONTAINER_SUITE_ARGUMENT_REQUIRED", "preflight requires provider and output");
    const receipt = runContainerSuitePreflight({ provider: values.provider, output: path.resolve(values.output) });
    process.stdout.write(`${canonicalJson(receipt)}\n`);
    return;
  }
  if (command === "materialize") {
    requireSuite(values["suite-root"], "WSL_CONTAINER_SUITE_ARGUMENT_REQUIRED", "materialize requires suite-root");
    const receipt = materializeContainerSuite({ suiteRoot: values["suite-root"] });
    process.stdout.write(`${canonicalJson(receipt)}\n`);
    return;
  }
  requireSuite(values.provider && values["suite-root"], "WSL_CONTAINER_SUITE_ARGUMENT_REQUIRED", "aggregate requires provider and suite-root");
  const verdict = aggregateContainerSuite({ provider: values.provider, suiteRoot: values["suite-root"] });
  const displayRoot = values["display-root"] ? path.normalize(values["display-root"]) : path.resolve(values["suite-root"]);
  process.stdout.write(`${canonicalJson({ run_id: verdict.run_id, status: verdict.status, verdict: path.join(displayRoot, "verdict.json") })}\n`);
  process.exitCode = verdict.status === "PASS" ? 0 : verdict.status === "BLOCKED" ? 2 : 1;
}

const MODULE_PATH = fileURLToPath(import.meta.url);
if (process.argv[1] && path.resolve(process.argv[1]) === MODULE_PATH) {
  main().catch((error) => {
    process.stderr.write(`${canonicalJson({ code: error?.code ?? "WSL_CONTAINER_SUITE_FAILED", message: error?.message ?? "Container suite failed" })}\n`);
    process.exitCode = 2;
  });
}
