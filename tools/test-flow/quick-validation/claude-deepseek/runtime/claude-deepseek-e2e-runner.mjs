#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

import { materializeClaudeSettings } from "../../../lib/release-inputs.mjs";
import { canonicalJson, sha256Bytes, sha256File } from "../../../lib/util.mjs";
import { validateEvidenceV2CoreVerdict } from "../../../../validation/evidence-v2-core.mjs";
import { validateEvidenceV2ModelCertInputSchema } from "../../../../validation/evidence-v2-certification.mjs";
import {
  CLAUDE_DEEPSEEK_MODEL,
  assertRegistrationUnchanged,
  auditClaudeModelCertInvocations,
  buildRegistrationProducerIdentity,
  treeDigest,
  validateClaudeDeepseekIdentity,
  validateRegistrationCache,
} from "./claude-deepseek-contract.mjs";
import { readRoleInvocationReceipts } from "./claude-deepseek-service-wrapper.mjs";

const MODULE_PATH = fileURLToPath(import.meta.url);
const CORE_VERDICT_RECEIPT_PATH = "payload/stages/deterministic.full/gates/det.evidence-v2-core/core-verdict.json";
const CONTRACT_MANIFEST_PATH = "schemas/v2/contract-manifest.json";

class ModelCertRunnerError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "ModelCertRunnerError";
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) {
  throw new ModelCertRunnerError(code, message, details);
}

function requireCert(condition, code, message, details = {}) {
  if (!condition) fail(code, message, details);
}

function createEmptyRoot(root, label) {
  const resolved = path.resolve(root);
  if (fs.existsSync(resolved)) {
    requireCert(fs.statSync(resolved).isDirectory() && fs.readdirSync(resolved).length === 0, "CLAUDE_DEEPSEEK_MODEL_CERT_ROOT_NOT_EMPTY", `${label} must be empty`);
  } else fs.mkdirSync(resolved, { recursive: true, mode: 0o700 });
  return resolved;
}

function writeJsonNew(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, canonicalJson(value), { encoding: "utf8", mode: 0o600, flag: "wx" });
}

function normalizedUsage(value) {
  const fields = ["input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"];
  requireCert(value?.schema_version === 1 && fields.every((key) => Number.isSafeInteger(value[key]) && value[key] >= 0), "CLAUDE_DEEPSEEK_MODEL_CERT_USAGE_INVALID", "Model-cert usage is incomplete");
  requireCert(value.total_tokens === fields.reduce((sum, key) => sum + value[key], 0) && Number.isFinite(value.cost_usd) && value.cost_usd >= 0, "CLAUDE_DEEPSEEK_MODEL_CERT_USAGE_INVALID", "Model-cert usage total or cost is invalid");
  return Object.freeze({ schema_version: 1, ...Object.fromEntries(fields.map((key) => [key, value[key]])), total_tokens: value.total_tokens, cost_usd: value.cost_usd });
}

function promptAttempts(runtimeReceipt) {
  requireCert(Array.isArray(runtimeReceipt?.role_attempts), "CLAUDE_DEEPSEEK_RUNTIME_PROMPTS_INVALID", "Production Runtime role prompt receipts are missing");
  return runtimeReceipt.role_attempts.map((item) => ({ role: item.role, attempt: item.attempt, prompt: item.prompt }));
}

export function auditRuntimeAndInvocations(runtimeReceipt, invocations) {
  requireCert(runtimeReceipt?.status === "PASS" && runtimeReceipt.execution_mode === "real-model", "CLAUDE_DEEPSEEK_RUNTIME_RECEIPT_INVALID", "Production Evidence V2 Runtime receipt is invalid");
  requireCert(runtimeReceipt.production_runtime === "problem_locator.runtime.diagnosis_runtime.DiagnosisRuntime", "CLAUDE_DEEPSEEK_RUNTIME_IDENTITY_INVALID", "Model-cert did not use the production DiagnosisRuntime");
  requireCert(runtimeReceipt.hard_cut?.candidate === false
    && runtimeReceipt.hard_cut?.partial_result === false
    && runtimeReceipt.hard_cut?.result_zip === false
    && runtimeReceipt.hard_cut?.methods_v1_grounding === false
    && runtimeReceipt.hard_cut?.harness_normalized === false,
  "CLAUDE_DEEPSEEK_RUNTIME_HARD_CUT_INVALID", "Model-cert Runtime reintroduced a legacy Methods V1 or harness-normalization path");
  const prompts = promptAttempts(runtimeReceipt);
  requireCert(prompts.length === invocations.length, "CLAUDE_DEEPSEEK_RUNTIME_INVOCATION_COUNT_MISMATCH", "Production prompt count differs from provider model calls");
  for (const [index, invocation] of invocations.entries()) {
    const prompt = prompts[index];
    requireCert(invocation.role === prompt.role
      && invocation.evaluation_attempt === prompt.attempt
      && invocation.prompt?.sha256 === prompt.prompt?.sha256
      && invocation.prompt?.utf8_size === prompt.prompt?.size,
    "CLAUDE_DEEPSEEK_RUNTIME_INVOCATION_IDENTITY_MISMATCH", "Provider call does not bind the exact production role prompt", { ordinal: index + 1 });
  }
  requireCert(runtimeReceipt.model_invocations === invocations.length, "CLAUDE_DEEPSEEK_RUNTIME_MODEL_COUNT_MISMATCH", "Runtime model count differs from provider receipts");
  requireCert(runtimeReceipt.scenario?.evidence_graph?.ref === runtimeReceipt.methods_result_identity?.evidence_graph_ref
    && runtimeReceipt.scenario?.evaluation_plan?.ref === runtimeReceipt.methods_result_identity?.plan_ref,
  "CLAUDE_DEEPSEEK_RUNTIME_SCENARIO_RESULT_MISMATCH", "Runtime scenario identity differs from the final methods_result");
  return { schema_version: 1, status: "PASS", prompt_count: prompts.length, methods_result: runtimeReceipt.methods_result_identity, records: runtimeReceipt.records };
}

export function auditScenarioIdentity({ sourceWiki, scenarioRoot, producer, cache, runtimeReceipt }) {
  const scenario = runtimeReceipt?.scenario;
  const driver = JSON.parse(fs.readFileSync(path.join(scenarioRoot, "driver.json"), "utf8"));
  const names = driver.initial_user_fact_names;
  const values = driver.initial_user_fact_values;
  const sourceIds = driver.attachment_anchor_names;
  const sourceFiles = driver.attachment_files;
  requireCert(driver.scenario_id === "multiple-rpc-timeouts"
    && Array.isArray(names) && Array.isArray(values) && names.length === values.length
    && Array.isArray(sourceIds) && Array.isArray(sourceFiles) && sourceIds.length > 0 && sourceIds.length === sourceFiles.length,
  "CLAUDE_DEEPSEEK_SCENARIO_DRIVER_INVALID", "The frozen release scenario driver is invalid");
  const expectedSources = sourceIds.map((sourceId, index) => ({ source_id: sourceId, content_sha256: sha256File(path.join(scenarioRoot, sourceFiles[index])) }));
  const expectedUserInputs = sha256Bytes(canonicalJson({ initial_user_fact_names: names, initial_user_fact_values: values }));
  requireCert(scenario?.scenario_id === "multiple-rpc-timeouts"
    && scenario.source_wiki_sha256 === sha256File(sourceWiki)
    && scenario.source_wiki_sha256 === producer.inputs.wiki.sha256
    && scenario.registration_id === cache.manifest.registration.registration_id
    && scenario.skill_content_sha256 === cache.manifest.registration.runtime_ref.content_hash
    && scenario.user_inputs_sha256 === expectedUserInputs
    && canonicalJson(scenario.sources) === canonicalJson(expectedSources),
  "CLAUDE_DEEPSEEK_SCENARIO_IDENTITY_MISMATCH", "Runtime scenario differs from the frozen Wiki, registration, driver, or sources");
  requireCert(scenario.evidence_graph?.ref === runtimeReceipt.methods_result_identity?.evidence_graph_ref
    && scenario.evidence_graph?.canonical_sha256 === runtimeReceipt.records?.graph?.sha256
    && scenario.evidence_graph?.canonical_size === runtimeReceipt.records?.graph?.size
    && scenario.evaluation_plan?.ref === runtimeReceipt.methods_result_identity?.plan_ref
    && scenario.evaluation_plan?.canonical_sha256 === runtimeReceipt.records?.plan?.sha256
    && scenario.evaluation_plan?.canonical_size === runtimeReceipt.records?.plan?.size,
  "CLAUDE_DEEPSEEK_SCENARIO_PRODUCTION_RECORD_MISMATCH", "Scenario Graph or Plan differs from the production execution record");
  return { schema_version: 1, status: "PASS", scenario };
}

function executionIdentity({ sourceRoot, identity, invocations }) {
  const runtimeFile = path.join(sourceRoot, "src", "problem_locator", "runtime", "diagnosis_runtime.py");
  const promptPolicy = invocations.map((item) => ({ role: item.role, attempt: item.evaluation_attempt, prompt: item.prompt }));
  const profile = {
    cli: identity.cli,
    settings: identity.settings,
    model: identity.model,
    max_output_tokens: identity.max_output_tokens,
  };
  const toolPolicies = invocations.map((item) => ({
    role: item.role,
    attempt: item.evaluation_attempt,
    tools: item.tool_policy.tools,
    readable_scope: item.tool_policy.readable_scope,
    writable_scope: item.tool_policy.writable_scope,
    network: item.tool_policy.network,
    shell: item.tool_policy.shell,
    skill_loading: item.tool_policy.skill_loading,
  }));
  return Object.freeze({
    runtime: { id: "problem-locator-diagnosis-runtime-evidence-v2", sha256: sha256File(runtimeFile) },
    prompt_policy: { id: "production-method-role-prompts-v2", sha256: sha256Bytes(canonicalJson(promptPolicy)) },
    profile: { id: "claude-code-deepseek-profile-v1", sha256: sha256Bytes(canonicalJson(profile)) },
    tool_policy: { id: "methods-role-read-write-only-v1", sha256: sha256Bytes(canonicalJson(toolPolicies)) },
  });
}

export function buildModelCertInput({
  sourceSnapshotDigest,
  contractManifestSha256,
  coreVerdictSha256,
  identity,
  invocations,
  usage,
  runtimeReceipt,
  sourceRoot,
}) {
  const methods = runtimeReceipt.methods_result_identity;
  const repairs = runtimeReceipt.repair_counts;
  const specialistCalls = invocations.filter((item) => item.role === "SPECIALIST").length;
  const reviewerCalls = invocations.filter((item) => item.role === "REVIEWER").length;
  return Object.freeze({
    schema_version: 1,
    receipt_type: "evidence-v2-model-cert-input",
    status: "PASS",
    certification_target: "P1",
    source_snapshot_digest: sourceSnapshotDigest,
    contract_manifest: { path: CONTRACT_MANIFEST_PATH, sha256: contractManifestSha256 },
    core_verdict: { path: CORE_VERDICT_RECEIPT_PATH, sha256: coreVerdictSha256 },
    scenario: runtimeReceipt.scenario,
    provider: { id: "deepseek", transport: "claude-code-compatible-api" },
    model: { id: CLAUDE_DEEPSEEK_MODEL, revision: identity.settings.fingerprint, revision_source: "settings-fingerprint" },
    execution_identity: executionIdentity({ sourceRoot, identity, invocations }),
    invocations: invocations.map((item, index) => ({
      invocation_id: item.invocation_id,
      ordinal: index + 1,
      role: item.role,
      attempt: item.evaluation_attempt,
      prompt: { sha256: item.prompt.sha256, size: item.prompt.utf8_size },
      usage: normalizedUsage(item.usage),
    })),
    call_counts: {
      total_calls: invocations.length,
      specialist_calls: specialistCalls,
      reviewer_calls: reviewerCalls,
      specialist_repairs: repairs.specialist,
      reviewer_repairs: repairs.reviewer,
      model_retries: 0,
    },
    usage: normalizedUsage({ schema_version: 1, ...usage }),
    methods_result: {
      canonical_sha256: methods.sha256,
      canonical_size: methods.size,
      case_id: methods.case_id,
      source_job_id: methods.source_job_id,
      result_ref: methods.result_ref,
      evaluation_id: methods.evaluation_id,
      status: methods.status,
      plan_ref: methods.plan_ref,
      evidence_graph_ref: methods.evidence_graph_ref,
      diagnostic_id: methods.diagnostic_id,
    },
  });
}

function pythonEnvironment(sourceRoot, ambient) {
  return {
    ...ambient,
    PYTHONPATH: [path.join(sourceRoot, "src"), sourceRoot, ambient.PYTHONPATH].filter(Boolean).join(path.delimiter),
    PYTHONDONTWRITEBYTECODE: "1",
    PYTHONNOUSERSITE: "1",
  };
}

function treeBytes(root) {
  if (!fs.existsSync(root)) return 0;
  let total = 0;
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const target = path.join(root, entry.name);
    if (entry.isDirectory()) total += treeBytes(target);
    else if (entry.isFile()) total += fs.statSync(target).size;
  }
  return total;
}

export function productionRuntimeArguments(options) {
  const script = path.join(options.sourceRoot, "tools", "test-flow", "quick-validation", "claude-deepseek", "runtime", "claude_deepseek_model_cert_runtime.py");
  const runtimeWork = path.join(options.workRoot, "runtime-chain");
  const receiptPath = path.join(options.evidenceRoot, "runtime-receipt.json");
  return [
    script,
    "--mode", "real",
    "--source-root", options.sourceRoot,
    "--source-wiki", options.sourceWiki,
    "--scenario-root", options.scenarioRoot,
    "--registration-root", options.registrationRoot,
    "--work-root", runtimeWork,
    "--receipt-path", receiptPath,
    "--node-entry", process.execPath,
    "--claude-entry", options.claudeEntry,
    "--claude-settings", options.stagedSettings,
    "--config-root", options.configRoot,
    "--private-root", options.privateRoot,
    "--evidence-root", options.evidenceRoot,
    "--usage-root", options.usageRoot,
    "--run-id", options.runId,
  ];
}

async function runProductionRuntime(options, { ambient, onProgress }) {
  const receiptPath = path.join(options.evidenceRoot, "runtime-receipt.json");
  const args = productionRuntimeArguments(options);
  const child = spawn(options.pythonEntry, args, {
    cwd: options.sourceRoot, env: pythonEnvironment(options.sourceRoot, ambient), stdio: ["ignore", "pipe", "pipe"],
  });
  const stdout = [];
  const stderr = [];
  child.stdout.on("data", (chunk) => stdout.push(Buffer.from(chunk)));
  child.stderr.on("data", (chunk) => stderr.push(Buffer.from(chunk)));
  let observed = treeBytes(options.evidenceRoot) + treeBytes(options.usageRoot);
  const progress = setInterval(() => {
    const current = treeBytes(options.evidenceRoot) + treeBytes(options.usageRoot);
    if (current > observed) {
      observed = current;
      onProgress?.("model-role-stream");
    }
  }, 1_000);
  progress.unref();
  const result = await new Promise((resolve) => {
    child.once("error", (error) => resolve({ code: null, signal: null, error }));
    child.once("exit", (code, signal) => resolve({ code, signal, error: null }));
  });
  clearInterval(progress);
  requireCert(result.code === 0 && result.signal === null && !result.error, "CLAUDE_DEEPSEEK_PRODUCTION_RUNTIME_FAILED", "Production Evidence V2 Runtime driver failed", { stderr: Buffer.concat(stderr).toString("utf8").slice(-2_000) });
  requireCert(fs.existsSync(receiptPath), "CLAUDE_DEEPSEEK_RUNTIME_RECEIPT_MISSING", "Production Runtime receipt is missing");
  return JSON.parse(fs.readFileSync(receiptPath, "utf8"));
}

export async function runE2E(options, { ambient = process.env, onProgress = null } = {}) {
  const sourceRoot = path.resolve(options.sourceRoot);
  const workRoot = createEmptyRoot(options.workRoot, "model-cert work root");
  const privateRoot = createEmptyRoot(options.privateRoot, "model-cert private root");
  const evidenceRoot = createEmptyRoot(options.evidenceRoot, "model-cert evidence root");
  const usageRoot = createEmptyRoot(options.usageRoot, "model-cert usage root");
  const sourceSnapshotDigest = options.sourceSnapshotDigest;
  requireCert(/^[a-f0-9]{64}$/u.test(sourceSnapshotDigest), "CLAUDE_DEEPSEEK_SOURCE_SNAPSHOT_INVALID", "Model-cert source snapshot digest is missing or invalid");
  const coreVerdictPath = path.resolve(options.coreVerdict);
  const coreGateRoot = path.dirname(coreVerdictPath);
  const coreVerdict = JSON.parse(fs.readFileSync(coreVerdictPath, "utf8"));
  validateEvidenceV2CoreVerdict(coreVerdict, { sourceSnapshotDigest, sourceRoot, gateRoot: coreGateRoot });
  const contractManifest = path.join(sourceRoot, ...CONTRACT_MANIFEST_PATH.split("/"));
  const identity = validateClaudeDeepseekIdentity(options.claudeEntry, options.claudeSettings);
  const caseRoot = path.join(sourceRoot, "tests", "cases", "release", "rpc-timeout-anonymized");
  const sourceWiki = path.join(caseRoot, "input", "wiki.md");
  const scenarioRoot = path.join(caseRoot, "scenarios", "multiple-rpc-timeouts");
  const producer = buildRegistrationProducerIdentity({
    wiki: sourceWiki,
    metaSkillRoot: path.join(sourceRoot, ".claude", "skills", "wiki-to-logparse-diagnosis-skill"),
    claudeIdentity: identity,
    module: "rpc",
  });
  const cache = validateRegistrationCache({ cacheRoot: options.cacheRoot, producer });
  assertRegistrationUnchanged(cache);
  const stagedSettings = path.join(privateRoot, "claude-settings.json");
  materializeClaudeSettings(options.claudeSettings, stagedSettings);
  const configRoot = path.join(privateRoot, "claude-config");
  fs.mkdirSync(configRoot, { mode: 0o700 });
  onProgress?.("production-runtime");
  const runtimeReceipt = await runProductionRuntime({
    ...options,
    sourceRoot,
    workRoot,
    privateRoot,
    evidenceRoot,
    usageRoot,
    stagedSettings,
    configRoot,
    registrationRoot: cache.registration_root,
    sourceWiki,
    scenarioRoot,
  }, { ambient, onProgress });
  const invocations = readRoleInvocationReceipts(usageRoot);
  const modelAudit = auditClaudeModelCertInvocations(invocations);
  const runtimeAudit = auditRuntimeAndInvocations(runtimeReceipt, invocations);
  const scenarioAudit = auditScenarioIdentity({ sourceWiki, scenarioRoot, producer, cache, runtimeReceipt });
  assertRegistrationUnchanged(cache);
  const modelCertInput = buildModelCertInput({
    sourceSnapshotDigest,
    contractManifestSha256: sha256File(contractManifest),
    coreVerdictSha256: sha256File(coreVerdictPath),
    identity,
    invocations,
    usage: modelAudit.aggregate,
    runtimeReceipt,
    sourceRoot,
  });
  validateEvidenceV2ModelCertInputSchema(modelCertInput, { certificationTarget: "P1" });
  const identityReceipt = { schema_version: 1, status: "PASS", claude: identity, producer, registration: cache.manifest.registration };
  const packageReceipt = { schema_version: 2, status: "PASS", producer_identity: producer.producer_identity, registration_tree_sha256: cache.manifest.registration.tree_sha256, runtime_ref: cache.manifest.registration.runtime_ref };
  writeJsonNew(path.join(evidenceRoot, "claude-identity.json"), identityReceipt);
  writeJsonNew(path.join(evidenceRoot, "methods-package.json"), packageReceipt);
  writeJsonNew(path.join(evidenceRoot, "model-invocations.json"), { schema_version: 1, status: "PASS", retry_policy: "ROLE_PROTOCOL_REPAIR_ONLY", invocations });
  writeJsonNew(path.join(evidenceRoot, "model-usage.json"), modelAudit);
  writeJsonNew(path.join(evidenceRoot, "model-cert-input.json"), modelCertInput);
  const gate = {
    schema_version: 1,
    status: "PASS",
    certification_target: "P1",
    checks: {
      core_binding: "PASS",
      registration_identity: "PASS",
      scenario_identity: scenarioAudit.status,
      production_runtime: runtimeAudit.status,
      role_calls: modelAudit.status,
      methods_result: runtimeReceipt.methods_result_identity.status,
    },
    model_calls: modelAudit.actual_call_count,
    repairs: modelAudit.repair_counts,
    model_cert_input_sha256: sha256Bytes(canonicalJson(modelCertInput)),
  };
  writeJsonNew(path.join(evidenceRoot, "adapter-receipt.json"), gate);
  return gate;
}

export function parseArguments(argv) {
  const values = {};
  const names = new Set(["source-root", "runtime-root", "claude-entry", "claude-settings", "python-entry", "cache-root", "work-root", "private-root", "evidence-root", "usage-root", "run-id", "source-snapshot-digest", "core-verdict", "logparse-root", "scenario"]);
  for (let index = 0; index < argv.length; index += 2) {
    const argument = argv[index];
    requireCert(argument?.startsWith("--") && index + 1 < argv.length && !argv[index + 1].startsWith("--"), "CLAUDE_DEEPSEEK_MODEL_CERT_ARGUMENT_INVALID", "Model-cert arguments must use --name value pairs");
    const name = argument.slice(2);
    requireCert(names.has(name), "CLAUDE_DEEPSEEK_MODEL_CERT_ARGUMENT_UNKNOWN", `Unsupported model-cert argument --${name}`);
    requireCert(!Object.hasOwn(values, name), "CLAUDE_DEEPSEEK_MODEL_CERT_ARGUMENT_DUPLICATE", `Model-cert argument --${name} is duplicated`);
    values[name] = argv[index + 1];
  }
  const required = ["source-root", "claude-entry", "claude-settings", "python-entry", "cache-root", "work-root", "private-root", "evidence-root", "usage-root", "run-id", "source-snapshot-digest", "core-verdict"];
  requireCert(required.every((name) => typeof values[name] === "string" && values[name]), "CLAUDE_DEEPSEEK_MODEL_CERT_ARGUMENT_MISSING", "Model-cert arguments are incomplete");
  if (values.scenario !== undefined) requireCert(values.scenario === "multiple-rpc-timeouts", "CLAUDE_DEEPSEEK_MODEL_CERT_SCENARIO_INVALID", "Evidence V2 model-cert uses only multiple-rpc-timeouts");
  return values;
}

export function safeE2EError(error) {
  return { schema_version: 1, status: "FAIL", code: error?.code ?? "CLAUDE_DEEPSEEK_MODEL_CERT_RUNNER_FAILED", message: error?.message ?? String(error) };
}

async function main() {
  try {
    const values = parseArguments(process.argv.slice(2));
    const paths = new Set(["sourceRoot", "claudeEntry", "claudeSettings", "pythonEntry", "cacheRoot", "workRoot", "privateRoot", "evidenceRoot", "usageRoot", "coreVerdict"]);
    const options = {
      sourceRoot: values["source-root"],
      runtimeRoot: values["runtime-root"] ?? values["source-root"],
      claudeEntry: values["claude-entry"],
      claudeSettings: values["claude-settings"],
      pythonEntry: values["python-entry"],
      cacheRoot: values["cache-root"],
      workRoot: values["work-root"],
      privateRoot: values["private-root"],
      evidenceRoot: values["evidence-root"],
      usageRoot: values["usage-root"],
      coreVerdict: values["core-verdict"],
      runId: values["run-id"],
      sourceSnapshotDigest: values["source-snapshot-digest"],
    };
    for (const name of paths) options[name] = path.resolve(options[name]);
    process.stdout.write(canonicalJson(await runE2E(options, { onProgress: (phase) => process.stdout.write(`TEST_FLOW_PROGRESS stage.progress claude-deepseek ${phase}\n`) })));
  } catch (error) {
    process.stderr.write(canonicalJson(safeE2EError(error)));
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === MODULE_PATH) await main();
