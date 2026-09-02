#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";

import { validateEvidenceV2CoreVerdict } from "../../../../validation/evidence-v2-core.mjs";
import {
  EVIDENCE_V2_SCENARIO_ORACLE_RECEIPT_FILENAME,
  buildEvidenceV2ScenarioOracleReceipt,
  validateEvidenceV2ScenarioOracleReceipt,
} from "../../../../validation/evidence-v2-scenario-oracle.mjs";
import {
  canonicalJson,
  CODEX_LUNA_APP_SERVER_SCHEMA_TREE_SHA256,
  CODEX_LUNA_MODEL,
  codexLunaAppServerCliVersion,
  sha256Bytes,
  sha256File,
  treeDigest,
  validateCodexLunaIdentity,
} from "../../../runtime-support/codex-luna-contract.mjs";
import { projectEvidenceV2ProviderTerminalFailure } from "../../../runtime-support/evidence-v2-provider-terminal.mjs";
import {
  assertMethodsPackageUnchanged,
  buildMethodsProducerIdentity,
  MACOS_CODEX_LUNA_PRICE_SNAPSHOT,
  validateMethodsCache,
} from "./macos-codex-luna-e2e-contract.mjs";
import { readModelCertInvocationReceipts } from "./macos-codex-luna-model-cert-wrapper.mjs";

const MODULE_PATH = fileURLToPath(import.meta.url);
const CORE_VERDICT_RECEIPT_PATH = "payload/stages/deterministic.full/gates/det.evidence-v2-core/core-verdict.json";
const CONTRACT_MANIFEST_PATH = "schemas/v2/contract-manifest.json";
const FIXED_SCENARIO = "multiple-rpc-timeouts";
const EVALUATION_MODES = new Set(["SPECIALIST_ONLY", "BLIND_CONSENSUS"]);

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

function evaluationMode(value = "SPECIALIST_ONLY") {
  requireCert(EVALUATION_MODES.has(value), "CODEX_LUNA_MODEL_CERT_EVALUATION_MODE_INVALID", "Model-cert evaluation mode is invalid");
  return value;
}

function createEmptyRoot(root, label) {
  const resolved = path.resolve(root);
  if (fs.existsSync(resolved)) {
    requireCert(fs.statSync(resolved).isDirectory() && fs.readdirSync(resolved).length === 0, "CODEX_LUNA_MODEL_CERT_ROOT_NOT_EMPTY", `${label} must be empty`);
  } else fs.mkdirSync(resolved, { recursive: true, mode: 0o700 });
  return resolved;
}

function writeJsonNew(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, `${canonicalJson(value)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
}

export function materializeProviderTerminalFailure(runtimeReceipt, evidenceRoot, {
  modelCalls,
  repairs,
} = {}) {
  const failure = projectEvidenceV2ProviderTerminalFailure({
    certificationTarget: "P2",
    methodsResult: runtimeReceipt?.methods_result,
  });
  if (failure === null) return null;
  const receipt = Object.freeze({
    ...failure,
    model_calls: modelCalls,
    repairs,
  });
  writeJsonNew(path.join(evidenceRoot, "adapter-receipt.json"), receipt);
  return receipt;
}

function failedModelUsage(invocations) {
  try {
    return {
      schema_version: 1,
      status: "FAIL",
      usage_complete: true,
      aggregate: aggregateUsage(invocations),
    };
  } catch {
    return {
      schema_version: 1,
      status: "FAIL",
      usage_complete: false,
      aggregate: null,
    };
  }
}

function copyPlainTree(sourceRoot, destinationRoot) {
  const source = path.resolve(sourceRoot);
  const metadata = fs.lstatSync(source);
  requireCert(metadata.isDirectory() && !metadata.isSymbolicLink(), "CODEX_LUNA_MODEL_CERT_REGISTRATION_INVALID", "Registration source must be a plain directory");
  fs.mkdirSync(destinationRoot, { recursive: false, mode: 0o700 });
  for (const entry of fs.readdirSync(source, { withFileTypes: true }).sort((left, right) => left.name.localeCompare(right.name))) {
    const sourcePath = path.join(source, entry.name);
    const destinationPath = path.join(destinationRoot, entry.name);
    const entryMetadata = fs.lstatSync(sourcePath);
    requireCert(!entryMetadata.isSymbolicLink(), "CODEX_LUNA_MODEL_CERT_REGISTRATION_INVALID", "Registration contains a symbolic link");
    if (entryMetadata.isDirectory()) copyPlainTree(sourcePath, destinationPath);
    else {
      requireCert(entryMetadata.isFile(), "CODEX_LUNA_MODEL_CERT_REGISTRATION_INVALID", "Registration contains a non-file node");
      fs.copyFileSync(sourcePath, destinationPath, fs.constants.COPYFILE_EXCL);
    }
  }
}

function normalizedUsage(value) {
  const inputTokens = Number(value?.input_tokens);
  const outputTokens = Number(value?.output_tokens);
  requireCert(value && Number.isSafeInteger(inputTokens) && inputTokens >= 0 && Number.isSafeInteger(outputTokens) && outputTokens >= 0, "CODEX_LUNA_MODEL_CERT_USAGE_INVALID", "Codex model-cert usage is incomplete");
  const costUsd = Math.round(((inputTokens * MACOS_CODEX_LUNA_PRICE_SNAPSHOT.rates.input + outputTokens * MACOS_CODEX_LUNA_PRICE_SNAPSHOT.rates.output) / 1_000_000) * 1_000_000) / 1_000_000;
  return Object.freeze({
    schema_version: 1,
    input_tokens: inputTokens,
    output_tokens: outputTokens,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    total_tokens: inputTokens + outputTokens,
    cost_usd: costUsd,
  });
}

function aggregateUsage(invocations) {
  const aggregate = { schema_version: 1, input_tokens: 0, output_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, total_tokens: 0, cost_usd: 0 };
  for (const invocation of invocations) {
    const usage = normalizedUsage(invocation.usage);
    for (const field of ["input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "total_tokens"]) aggregate[field] += usage[field];
    aggregate.cost_usd = Math.round((aggregate.cost_usd + usage.cost_usd) * 1_000_000) / 1_000_000;
  }
  return Object.freeze(aggregate);
}

function promptAttempts(runtimeReceipt) {
  requireCert(Array.isArray(runtimeReceipt?.role_attempts), "CODEX_LUNA_MODEL_CERT_RUNTIME_PROMPTS_INVALID", "Production Runtime role prompt receipts are missing");
  return runtimeReceipt.role_attempts.map((item) => ({ role: item.role, attempt: item.attempt, prompt: item.prompt }));
}

function validScenarioIdentity(runtimeReceipt) {
  const scenario = runtimeReceipt?.scenario;
  const keys = ["scenario_id", "source_wiki_sha256", "registration_id", "skill_content_sha256", "user_inputs_sha256", "sources", "evidence_graph", "evaluation_plan"];
  const digest = /^[a-f0-9]{64}$/u;
  const graph = scenario?.evidence_graph;
  const plan = scenario?.evaluation_plan;
  return scenario !== null
    && typeof scenario === "object"
    && !Array.isArray(scenario)
    && Object.keys(scenario).sort().join("\0") === [...keys].sort().join("\0")
    && scenario.scenario_id === FIXED_SCENARIO
    && scenario.registration_id === runtimeReceipt.registration_id
    && digest.test(scenario.source_wiki_sha256)
    && digest.test(scenario.skill_content_sha256)
    && digest.test(scenario.user_inputs_sha256)
    && Array.isArray(scenario.sources)
    && scenario.sources.length > 0
    && new Set(scenario.sources.map((item) => item?.source_id)).size === scenario.sources.length
    && scenario.sources.every((item) => Object.keys(item ?? {}).sort().join("\0") === "content_sha256\0source_id" && typeof item.source_id === "string" && item.source_id.length > 0 && digest.test(item.content_sha256))
    && graph?.ref === runtimeReceipt.methods_result_identity?.evidence_graph_ref
    && /^graph-[a-f0-9]{64}$/u.test(graph?.ref ?? "")
    && digest.test(graph?.canonical_sha256 ?? "")
    && Number.isSafeInteger(graph?.canonical_size)
    && graph.canonical_size > 0
    && plan?.ref === runtimeReceipt.methods_result_identity?.plan_ref
    && /^plan-[a-f0-9]{64}$/u.test(plan?.ref ?? "")
    && digest.test(plan?.canonical_sha256 ?? "")
    && Number.isSafeInteger(plan?.canonical_size)
    && plan.canonical_size > 0;
}

export function auditRuntimeAndInvocations(runtimeReceipt, invocations, {
  evaluationMode: expectedEvaluationMode = "SPECIALIST_ONLY",
} = {}) {
  const selectedMode = evaluationMode(expectedEvaluationMode);
  requireCert(runtimeReceipt?.status === "PASS" && runtimeReceipt.execution_mode === "real-model", "CODEX_LUNA_MODEL_CERT_RUNTIME_RECEIPT_INVALID", "Production Evidence V2 Runtime receipt is invalid");
  requireCert(runtimeReceipt.production_runtime === "problem_locator.runtime.diagnosis_runtime.DiagnosisRuntime", "CODEX_LUNA_MODEL_CERT_RUNTIME_IDENTITY_INVALID", "Model-cert did not use the production DiagnosisRuntime");
  requireCert(runtimeReceipt.evaluation_mode === selectedMode, "CODEX_LUNA_MODEL_CERT_RUNTIME_EVALUATION_MODE_MISMATCH", "Production Runtime used a different evaluation mode");
  requireCert(runtimeReceipt.scenario_id === FIXED_SCENARIO && validScenarioIdentity(runtimeReceipt), "CODEX_LUNA_MODEL_CERT_SCENARIO_IDENTITY_INVALID", "Production Runtime did not bind the complete fixed release scenario");
  const prompts = promptAttempts(runtimeReceipt);
  requireCert(prompts.length === invocations.length, "CODEX_LUNA_MODEL_CERT_RUNTIME_INVOCATION_COUNT_MISMATCH", "Production prompt count differs from provider calls");
  for (const [index, invocation] of invocations.entries()) {
    const prompt = prompts[index];
    requireCert(
      invocation.role === prompt.role
        && invocation.attempt === prompt.attempt
        && invocation.prompt?.sha256 === prompt.prompt?.sha256
        && invocation.prompt?.size === prompt.prompt?.size,
      "CODEX_LUNA_MODEL_CERT_RUNTIME_INVOCATION_IDENTITY_MISMATCH",
      "Provider call does not bind the exact production role prompt",
      { ordinal: index + 1 },
    );
  }
  requireCert(runtimeReceipt.model_invocations === invocations.length, "CODEX_LUNA_MODEL_CERT_RUNTIME_MODEL_COUNT_MISMATCH", "Runtime model count differs from provider receipts");
  return Object.freeze({ schema_version: 1, status: "PASS", prompt_count: prompts.length, records: runtimeReceipt.records });
}

function modelRevision({ identity, sourceRoot }) {
  return sha256Bytes(canonicalJson({
    cli: identity.cli,
    app_server_schema_tree_sha256: CODEX_LUNA_APP_SERVER_SCHEMA_TREE_SHA256,
    app_server_runtime_sha256: sha256File(path.join(sourceRoot, "tools/test-flow/runtime-support/codex-luna-app-server-runtime.mjs")),
    model_cert_wrapper_sha256: sha256File(path.join(sourceRoot, "tools/test-flow/quick-validation/codex-luna/runtime/macos-codex-luna-model-cert-wrapper.mjs")),
  }));
}

function executionIdentity({ sourceRoot, identity, invocations }) {
  const promptPolicy = invocations.map((item) => ({ role: item.role, attempt: item.attempt, prompt: item.prompt }));
  const profile = { cli: identity.cli, model: identity.model, reasoning_effort: identity.reasoning_effort, invocations: invocations.map((item) => item.profile) };
  const toolPolicy = invocations.map((item) => ({ role: item.role, attempt: item.attempt, policy: item.tool_policy }));
  return Object.freeze({
    runtime: { id: "problem-locator-diagnosis-runtime-evidence-v2", sha256: sha256File(path.join(sourceRoot, "src/problem_locator/runtime/diagnosis_runtime.py")) },
    prompt_policy: { id: "production-method-role-prompts-v2", sha256: sha256Bytes(canonicalJson(promptPolicy)) },
    profile: { id: "codex-luna-app-server-profile-v1", sha256: sha256Bytes(canonicalJson(profile)) },
    tool_policy: { id: "codex-luna-method-role-workspace-v1", sha256: sha256Bytes(canonicalJson(toolPolicy)) },
  });
}

function modelCertInvocations(invocations) {
  return invocations.map((item, index) => ({
    invocation_id: item.invocation_id,
    ordinal: index + 1,
    role: item.role,
    attempt: item.attempt,
    prompt: { sha256: item.prompt.sha256, size: item.prompt.size },
    usage: normalizedUsage(item.usage),
  }));
}

export function buildModelCertInput({ sourceSnapshotDigest, contractManifestSha256, coreVerdictSha256, scenarioOracleSha256, identity, invocations, runtimeReceipt, sourceRoot, evaluationMode: requestedEvaluationMode = "SPECIALIST_ONLY" }) {
  const selectedMode = evaluationMode(requestedEvaluationMode);
  const methods = runtimeReceipt.methods_result_identity;
  const repairs = runtimeReceipt.repair_counts;
  const specialistCalls = invocations.filter((item) => item.role === "SPECIALIST").length;
  const reviewerCalls = invocations.filter((item) => item.role === "REVIEWER").length;
  return Object.freeze({
    schema_version: 2,
    receipt_type: "evidence-v2-model-cert-input",
    status: "PASS",
    certification_target: "P2",
    evaluation_mode: selectedMode,
    source_snapshot_digest: sourceSnapshotDigest,
    contract_manifest: { path: CONTRACT_MANIFEST_PATH, sha256: contractManifestSha256 },
    core_verdict: { path: CORE_VERDICT_RECEIPT_PATH, sha256: coreVerdictSha256 },
    scenario_oracle: { path: EVIDENCE_V2_SCENARIO_ORACLE_RECEIPT_FILENAME, sha256: scenarioOracleSha256 },
    provider: { id: "openai", transport: "codex-app-server" },
    model: {
      id: CODEX_LUNA_MODEL,
      revision: modelRevision({ identity, sourceRoot }),
      revision_source: "frozen-codex-cli-and-app-server-runtime-identity",
    },
    execution_identity: executionIdentity({ sourceRoot, identity, invocations }),
    scenario: runtimeReceipt.scenario,
    invocations: modelCertInvocations(invocations),
    call_counts: {
      total_calls: invocations.length,
      specialist_calls: specialistCalls,
      reviewer_calls: reviewerCalls,
      specialist_repairs: repairs.specialist,
      reviewer_repairs: repairs.reviewer,
      model_retries: 0,
    },
    usage: aggregateUsage(invocations),
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

export async function runProductionRuntime(options, { ambient, onProgress }) {
  const script = path.join(options.sourceRoot, "tools/test-flow/quick-validation/codex-luna/runtime/macos_codex_luna_model_cert_driver.py");
  const receiptPath = path.join(options.evidenceRoot, "runtime-receipt.json");
  const args = [
    script,
    "--mode", "real",
    "--evaluation-mode", evaluationMode(options.evaluationMode),
    "--source-root", options.sourceRoot,
    "--registration-root", options.registrationRoot,
    "--work-root", path.join(options.workRoot, "runtime-chain"),
    "--receipt-path", receiptPath,
    "--node-entry", process.execPath,
    "--codex-entry", options.codexEntry,
    "--auth-source", options.authSource,
    "--skill-source", options.skillSource,
    "--expected-cli-version", options.expectedCliVersion,
    "--private-root", options.privateRoot,
    "--evidence-root", options.evidenceRoot,
    "--usage-root", options.usageRoot,
    "--run-id", options.runId,
  ];
  if (options.scenarioRoot) {
    args.push(
      "--scenario-root", options.scenarioRoot,
      "--scenario-id", options.scenario,
    );
  }
  const child = spawn(options.pythonEntry, args, { cwd: options.sourceRoot, env: pythonEnvironment(options.sourceRoot, ambient), stdio: ["ignore", "pipe", "pipe"] });
  const stderr = [];
  child.stderr.on("data", (chunk) => stderr.push(Buffer.from(chunk)));
  child.stdout.resume();
  let observed = treeBytes(options.evidenceRoot) + treeBytes(options.usageRoot);
  const progress = setInterval(() => {
    const current = treeBytes(options.evidenceRoot) + treeBytes(options.usageRoot);
    if (current > observed) { observed = current; onProgress?.("model-role-stream"); }
  }, 1_000);
  progress.unref();
  const result = await new Promise((resolve) => {
    child.once("error", (error) => resolve({ code: null, signal: null, error }));
    child.once("exit", (code, signal) => resolve({ code, signal, error: null }));
  });
  clearInterval(progress);
  requireCert(result.code === 0 && result.signal === null && !result.error, "CODEX_LUNA_MODEL_CERT_PRODUCTION_RUNTIME_FAILED", "Production Evidence V2 Runtime driver failed", { stderr: Buffer.concat(stderr).toString("utf8").slice(-2_000) });
  requireCert(fs.existsSync(receiptPath), "CODEX_LUNA_MODEL_CERT_RUNTIME_RECEIPT_MISSING", "Production Runtime receipt is missing");
  return JSON.parse(fs.readFileSync(receiptPath, "utf8"));
}

function materializeCachedRegistration({ workRoot, cache, registrationTemplate }) {
  const registration = JSON.parse(fs.readFileSync(registrationTemplate, "utf8"));
  const destination = path.join(workRoot, "registration-input");
  fs.mkdirSync(destination, { mode: 0o700 });
  fs.copyFileSync(registrationTemplate, path.join(destination, "registration-template.json"), fs.constants.COPYFILE_EXCL);
  const packageDestination = path.join(destination, ...registration.package.relative_path.split("/"));
  fs.mkdirSync(path.dirname(packageDestination), { recursive: true, mode: 0o700 });
  copyPlainTree(cache.package_root, packageDestination);
  return { root: destination, source: "codex-methods-cache", tree_sha256: treeDigest(destination) };
}

export function defaultRegistrationInput({ options, sourceRoot, workRoot, identity }) {
  if (options.registrationRoot) {
    const root = path.resolve(options.registrationRoot);
    requireCert(fs.statSync(root).isDirectory(), "CODEX_LUNA_MODEL_CERT_REGISTRATION_INVALID", "External production registration is unavailable");
    return { registration: { root, source: "external-validated-production-registration", tree_sha256: treeDigest(root) }, producer: null, cache: null };
  }
  requireCert(typeof options.cacheRoot === "string" && options.cacheRoot.length > 0, "CODEX_LUNA_MODEL_CERT_CACHE_REQUIRED", "Model-cert requires --registration-root or the exact Codex Methods cache");
  const caseRoot = path.join(sourceRoot, "tests/cases/release/rpc-timeout-anonymized");
  const registrationTemplate = path.join(caseRoot, "registration/rpc-timeout-methods-v1/registration-template.json");
  const producer = buildMethodsProducerIdentity({
    wiki: path.join(caseRoot, "input/wiki.md"),
    metaSkillRoot: path.join(sourceRoot, ".agents/skills/wiki-to-diagnosis-skill"),
    registrationTemplate,
    codexIdentity: identity,
  });
  const cache = validateMethodsCache({ cacheRoot: options.cacheRoot, producer, registrationTemplate });
  assertMethodsPackageUnchanged(cache);
  return { registration: materializeCachedRegistration({ workRoot, cache, registrationTemplate }), producer, cache };
}

async function materializeStandaloneModelCert({ sourceRoot, sourceSnapshotDigest, coreVerdictPath, evidenceRoot, evaluationMode: requestedEvaluationMode = "SPECIALIST_ONLY" }) {
  const modulePath = path.join(sourceRoot, "tools/validation/evidence-v2-certification.mjs");
  requireCert(fs.existsSync(modulePath), "CODEX_LUNA_MODEL_CERT_SHARED_CONTRACT_MISSING", "Shared Evidence V2 certification builder is unavailable");
  const shared = await import(pathToFileURL(modulePath).href);
  requireCert(typeof shared.buildEvidenceV2ModelCert === "function", "CODEX_LUNA_MODEL_CERT_SHARED_CONTRACT_INVALID", "Shared Evidence V2 certification builder is invalid");
  const cert = shared.buildEvidenceV2ModelCert({
    certificationTarget: "P2",
    evaluationMode: evaluationMode(requestedEvaluationMode),
    sourceSnapshotDigest,
    sourceRoot,
    coreVerdictPath,
    certRoot: evidenceRoot,
  });
  writeJsonNew(path.join(evidenceRoot, "model-cert.json"), cert);
  return cert;
}

export async function runE2E(options, {
  ambient = process.env,
  onProgress = null,
  validateIdentity = validateCodexLunaIdentity,
  validateCore = validateEvidenceV2CoreVerdict,
  runRuntime = runProductionRuntime,
  registrationInput = defaultRegistrationInput,
  readInvocations = readModelCertInvocationReceipts,
  materializeModelCert = materializeStandaloneModelCert,
} = {}) {
  const selectedEvaluationMode = evaluationMode(options.evaluationMode);
  const sourceRoot = path.resolve(options.sourceRoot);
  const workRoot = createEmptyRoot(options.workRoot, "model-cert work root");
  const privateRoot = createEmptyRoot(options.privateRoot, "model-cert private root");
  const evidenceRoot = createEmptyRoot(options.evidenceRoot, "model-cert evidence root");
  const usageRoot = createEmptyRoot(options.usageRoot, "model-cert usage root");
  requireCert(/^[a-f0-9]{64}$/u.test(options.sourceSnapshotDigest), "CODEX_LUNA_MODEL_CERT_SOURCE_SNAPSHOT_INVALID", "Model-cert source snapshot digest is missing or invalid");
  const coreVerdictPath = path.resolve(options.coreVerdict);
  validateCore(JSON.parse(fs.readFileSync(coreVerdictPath, "utf8")), { sourceSnapshotDigest: options.sourceSnapshotDigest, sourceRoot, gateRoot: path.dirname(coreVerdictPath) });
  const identity = validateIdentity(options.codexEntry, options.authSource);
  const resolved = registrationInput({ options, sourceRoot, workRoot, identity });
  onProgress?.("production-runtime");
  const runtimeReceipt = await runRuntime({
    ...options,
    sourceRoot,
    workRoot,
    privateRoot,
    evidenceRoot,
    usageRoot,
    registrationRoot: resolved.registration.root,
    skillSource: path.join(sourceRoot, "tools/test-flow/quick-validation/codex-luna/fixtures/model-cert-skill/codex-luna-evidence-v2-evaluator/SKILL.md"),
    expectedCliVersion: codexLunaAppServerCliVersion(),
    evaluationMode: selectedEvaluationMode,
  }, { ambient, onProgress });
  const invocations = readInvocations(usageRoot, {
    allowFailurePrefix: true,
    evaluationMode: selectedEvaluationMode,
  });
  const terminalFailure = projectEvidenceV2ProviderTerminalFailure({
    certificationTarget: "P2",
    methodsResult: runtimeReceipt?.methods_result,
  });
  if (terminalFailure !== null) {
    writeJsonNew(path.join(evidenceRoot, "codex-identity.json"), { schema_version: 1, status: "PASS", codex: identity, producer: resolved.producer, registration: resolved.registration });
    writeJsonNew(path.join(evidenceRoot, "methods-package.json"), { schema_version: 2, status: "PASS", registration_source: resolved.registration.source, registration_tree_sha256: resolved.registration.tree_sha256, registration_id: runtimeReceipt.registration_id, skill_content_sha256: runtimeReceipt.scenario.skill_content_sha256 });
    writeJsonNew(path.join(evidenceRoot, "model-invocations.json"), { schema_version: 1, status: "FAIL", retry_policy: "ROLE_PROTOCOL_REPAIR_ONLY", invocations });
    writeJsonNew(path.join(evidenceRoot, "model-usage.json"), failedModelUsage(invocations));
    const failureReceipt = materializeProviderTerminalFailure(runtimeReceipt, evidenceRoot, {
      modelCalls: invocations.length,
      repairs: runtimeReceipt.repair_counts,
    });
    fail(failureReceipt.code, failureReceipt.reason, {
      diagnostic_id: failureReceipt.diagnostic_id,
      evaluation_ref: failureReceipt.evaluation_ref,
    });
  }
  const runtimeAudit = auditRuntimeAndInvocations(runtimeReceipt, invocations, { evaluationMode: selectedEvaluationMode });
  if (resolved.cache !== null) assertMethodsPackageUnchanged(resolved.cache);
  requireCert(
    treeDigest(resolved.registration.root) === resolved.registration.tree_sha256,
    "CODEX_LUNA_MODEL_CERT_REGISTRATION_DRIFT",
    "Validated production registration changed during model certification",
  );
  const contractManifest = path.join(sourceRoot, CONTRACT_MANIFEST_PATH);
  writeJsonNew(path.join(evidenceRoot, "codex-identity.json"), { schema_version: 1, status: "PASS", codex: identity, producer: resolved.producer, registration: resolved.registration });
  writeJsonNew(path.join(evidenceRoot, "methods-package.json"), { schema_version: 2, status: "PASS", registration_source: resolved.registration.source, registration_tree_sha256: resolved.registration.tree_sha256, registration_id: runtimeReceipt.registration_id, skill_content_sha256: runtimeReceipt.scenario.skill_content_sha256 });
  writeJsonNew(path.join(evidenceRoot, "model-invocations.json"), { schema_version: 1, status: "PASS", retry_policy: "ROLE_PROTOCOL_REPAIR_ONLY", invocations });
  writeJsonNew(path.join(evidenceRoot, "model-usage.json"), { schema_version: 1, status: "PASS", aggregate: aggregateUsage(invocations) });
  const normalizedInvocations = modelCertInvocations(invocations);
  const scenarioOracle = buildEvidenceV2ScenarioOracleReceipt({
    sourceRoot,
    certRoot: evidenceRoot,
    scenario: runtimeReceipt.scenario,
    providerInvocations: normalizedInvocations,
    modelId: CODEX_LUNA_MODEL,
    evaluationMode: selectedEvaluationMode,
  });
  writeJsonNew(path.join(evidenceRoot, EVIDENCE_V2_SCENARIO_ORACLE_RECEIPT_FILENAME), scenarioOracle);
  validateEvidenceV2ScenarioOracleReceipt(scenarioOracle, {
    sourceRoot,
    certRoot: evidenceRoot,
    scenario: runtimeReceipt.scenario,
    providerInvocations: normalizedInvocations,
    modelId: CODEX_LUNA_MODEL,
    evaluationMode: selectedEvaluationMode,
  });
  const modelCertInput = buildModelCertInput({
    sourceSnapshotDigest: options.sourceSnapshotDigest,
    contractManifestSha256: sha256File(contractManifest),
    coreVerdictSha256: sha256File(coreVerdictPath),
    scenarioOracleSha256: sha256File(path.join(evidenceRoot, EVIDENCE_V2_SCENARIO_ORACLE_RECEIPT_FILENAME)),
    identity,
    invocations,
    runtimeReceipt,
    sourceRoot,
    evaluationMode: selectedEvaluationMode,
  });
  writeJsonNew(path.join(evidenceRoot, "model-cert-input.json"), modelCertInput);
  const modelCert = await materializeModelCert({
    sourceRoot,
    sourceSnapshotDigest: options.sourceSnapshotDigest,
    coreVerdictPath,
    evidenceRoot,
    evaluationMode: selectedEvaluationMode,
  });
  const gate = {
    schema_version: 1,
    status: "PASS",
    certification_target: "P2",
    evaluation_mode: selectedEvaluationMode,
    checks: {
      core_binding: "PASS",
      registration_identity: "PASS",
      production_runtime: runtimeAudit.status,
      role_calls: "PASS",
      scenario: "PASS",
      scenario_oracle: scenarioOracle.status,
      methods_result: runtimeReceipt.methods_result_identity.status,
    },
    model_calls: invocations.length,
    repairs: runtimeReceipt.repair_counts,
    model_cert_input_sha256: sha256Bytes(canonicalJson(modelCertInput)),
    model_cert_sha256: sha256Bytes(canonicalJson(modelCert)),
  };
  writeJsonNew(path.join(evidenceRoot, "adapter-receipt.json"), gate);
  return gate;
}

export function parseArguments(argv) {
  const values = {};
  const names = new Set(["source-root", "codex-entry", "auth-source", "python-entry", "cache-root", "registration-root", "work-root", "private-root", "evidence-root", "usage-root", "run-id", "source-snapshot-digest", "core-verdict", "scenario", "evaluation-mode"]);
  for (let index = 0; index < argv.length; index += 2) {
    const argument = argv[index];
    requireCert(argument?.startsWith("--") && index + 1 < argv.length && !argv[index + 1].startsWith("--"), "CODEX_LUNA_MODEL_CERT_ARGUMENT_INVALID", "Model-cert arguments must use --name value pairs");
    const name = argument.slice(2);
    requireCert(names.has(name), "CODEX_LUNA_MODEL_CERT_ARGUMENT_UNKNOWN", `Unsupported model-cert argument --${name}`);
    requireCert(!Object.hasOwn(values, name), "CODEX_LUNA_MODEL_CERT_ARGUMENT_DUPLICATE", `Model-cert argument --${name} is duplicated`);
    values[name] = argv[index + 1];
  }
  const required = ["source-root", "codex-entry", "auth-source", "python-entry", "work-root", "private-root", "evidence-root", "usage-root", "run-id", "source-snapshot-digest", "core-verdict"];
  requireCert(required.every((name) => typeof values[name] === "string" && values[name]), "CODEX_LUNA_MODEL_CERT_ARGUMENT_MISSING", "Model-cert arguments are incomplete");
  requireCert(values["registration-root"] || values["cache-root"], "CODEX_LUNA_MODEL_CERT_REGISTRATION_INPUT_MISSING", "Model-cert requires --registration-root or --cache-root");
  if (values.scenario !== undefined) requireCert(values.scenario === FIXED_SCENARIO, "CODEX_LUNA_MODEL_CERT_SCENARIO_INVALID", "Evidence V2 model-cert uses only multiple-rpc-timeouts");
  values["evaluation-mode"] = evaluationMode(values["evaluation-mode"]);
  return values;
}

export function safeE2EError(error) {
  return { schema_version: 1, status: "FAIL", code: error?.code ?? "CODEX_LUNA_MODEL_CERT_RUNNER_FAILED", message: error?.message ?? String(error) };
}

async function main() {
  try {
    const values = parseArguments(process.argv.slice(2));
    const options = {
      sourceRoot: path.resolve(values["source-root"]),
      codexEntry: path.resolve(values["codex-entry"]),
      authSource: path.resolve(values["auth-source"]),
      pythonEntry: path.resolve(values["python-entry"]),
      cacheRoot: values["cache-root"] ? path.resolve(values["cache-root"]) : null,
      registrationRoot: values["registration-root"] ? path.resolve(values["registration-root"]) : null,
      workRoot: path.resolve(values["work-root"]),
      privateRoot: path.resolve(values["private-root"]),
      evidenceRoot: path.resolve(values["evidence-root"]),
      usageRoot: path.resolve(values["usage-root"]),
      runId: values["run-id"],
      sourceSnapshotDigest: values["source-snapshot-digest"],
      coreVerdict: path.resolve(values["core-verdict"]),
      scenario: values.scenario ?? FIXED_SCENARIO,
      evaluationMode: values["evaluation-mode"],
    };
    const result = await runE2E(options, { onProgress: (phase) => process.stdout.write(`TEST_FLOW_PROGRESS stage.progress codex-luna ${phase}\n`) });
    process.stdout.write(`${canonicalJson(result)}\n`);
  } catch (error) {
    process.stderr.write(`${canonicalJson(safeE2EError(error))}\n`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === MODULE_PATH) await main();
