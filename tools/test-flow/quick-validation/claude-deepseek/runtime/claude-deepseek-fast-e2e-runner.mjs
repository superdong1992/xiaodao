import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";

import { materializeClaudeSettings } from "../../../lib/release-inputs.mjs";
import { canonicalJson, sha256File } from "../../../lib/util.mjs";
import {
  CLAUDE_DEEPSEEK_MODEL,
  aggregateClaudeUsage,
  assertRegistrationUnchanged,
  auditClaudeModelCertInvocations,
  treeDigest,
  validateClaudeDeepseekIdentity,
  validateRegistrationRoot,
} from "./claude-deepseek-contract.mjs";
import { readRoleInvocationReceipts } from "./claude-deepseek-service-wrapper.mjs";
import {
  FAST_E2E_MARKER_TO_METHOD,
  deriveFastE2EV2Expectation,
  scenarioPaths,
} from "../../fast-e2e-scenarios.mjs";

class FastE2EError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "FastE2EError";
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) {
  throw new FastE2EError(code, message, details);
}

function requireFast(condition, code, message, details = {}) {
  if (!condition) fail(code, message, details);
}

function createEmptyRoot(root, label) {
  const resolved = path.resolve(root);
  if (fs.existsSync(resolved)) {
    requireFast(
      fs.statSync(resolved).isDirectory() && fs.readdirSync(resolved).length === 0,
      "CLAUDE_DEEPSEEK_FAST_E2E_ROOT_NOT_EMPTY",
      `${label} must be empty`,
    );
  } else fs.mkdirSync(resolved, { recursive: true, mode: 0o700 });
  return resolved;
}

function writeJsonNew(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, canonicalJson(value), {
    encoding: "utf8",
    mode: 0o600,
    flag: "wx",
  });
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

export function validateFastRegistrationInput(registrationRoot) {
  const validated = validateRegistrationRoot(registrationRoot);
  const templatePath = path.join(validated.root, "registration-template.json");
  const methodsPath = path.join(validated.package_root, "methods.json");
  return Object.freeze({
    schema_version: 1,
    status: "PASS",
    source: "fast-e2e-production-registration",
    registration_root: validated.root,
    package_root: validated.package_root,
    manifest: {
      registration: {
        registration_id: validated.registration.registration_id,
        skill_name: validated.registration.package.skill_name,
        tree_sha256: treeDigest(validated.root),
        runtime_ref: validated.runtime_ref,
        template_sha256: sha256File(templatePath),
        methods_sha256: sha256File(methodsPath),
      },
    },
  });
}

function pythonEnvironment(sourceRoot, ambient) {
  return {
    ...ambient,
    PYTHONPATH: [path.join(sourceRoot, "src"), sourceRoot, ambient.PYTHONPATH]
      .filter(Boolean)
      .join(path.delimiter),
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
  return [
    path.join(
      options.sourceRoot,
      "tools/test-flow/quick-validation/claude-deepseek/runtime/claude_deepseek_model_cert_runtime.py",
    ),
    "--mode", "real",
    "--source-root", options.sourceRoot,
    "--scenario-root", options.scenarioRoot,
    "--scenario-id", options.scenario,
    "--registration-root", options.registrationRoot,
    "--work-root", path.join(options.workRoot, "runtime-chain"),
    "--receipt-path", path.join(options.evidenceRoot, "runtime-receipt.json"),
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

export async function runProductionRuntime(
  options,
  { ambient = process.env, onProgress = null } = {},
) {
  const receiptPath = path.join(options.evidenceRoot, "runtime-receipt.json");
  const child = spawn(options.pythonEntry, productionRuntimeArguments(options), {
    cwd: options.sourceRoot,
    env: pythonEnvironment(options.sourceRoot, ambient),
    stdio: ["ignore", "pipe", "pipe"],
  });
  const stderr = [];
  child.stdout.on("data", () => {});
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
  requireFast(
    result.code === 0 && result.signal === null && !result.error,
    "CLAUDE_DEEPSEEK_FAST_E2E_RUNTIME_FAILED",
    "Production Evidence V2 Runtime driver failed",
    { stderr: Buffer.concat(stderr).toString("utf8").slice(-2_000) },
  );
  requireFast(
    fs.existsSync(receiptPath),
    "CLAUDE_DEEPSEEK_FAST_E2E_RUNTIME_RECEIPT_MISSING",
    "Production Runtime receipt is missing",
  );
  return readJson(receiptPath);
}

export function auditRuntime(runtimeReceipt, invocations, scenarioId) {
  requireFast(
    runtimeReceipt?.status === "PASS"
      && runtimeReceipt.execution_mode === "real-model"
      && runtimeReceipt.production_runtime
        === "problem_locator.runtime.diagnosis_runtime.DiagnosisRuntime",
    "CLAUDE_DEEPSEEK_FAST_E2E_RUNTIME_RECEIPT_INVALID",
    "Fast E2E did not use the production Evidence V2 Runtime",
  );
  requireFast(
    runtimeReceipt.scenario_id === scenarioId
      && runtimeReceipt.scenario?.scenario_id === scenarioId,
    "CLAUDE_DEEPSEEK_FAST_E2E_SCENARIO_IDENTITY_MISMATCH",
    "Production Runtime used a different Fast E2E scenario",
  );
  const prompts = runtimeReceipt.role_attempts;
  requireFast(
    Array.isArray(prompts)
      && prompts.length === invocations.length
      && runtimeReceipt.model_invocations === invocations.length,
    "CLAUDE_DEEPSEEK_FAST_E2E_INVOCATION_COUNT_MISMATCH",
    "Production role prompts differ from provider calls",
  );
  for (const [index, invocation] of invocations.entries()) {
    const prompt = prompts[index];
    requireFast(
      invocation.role === prompt.role
        && invocation.evaluation_attempt === prompt.attempt
        && invocation.prompt?.sha256 === prompt.prompt?.sha256
        && invocation.prompt?.size === prompt.prompt?.size,
      "CLAUDE_DEEPSEEK_FAST_E2E_INVOCATION_IDENTITY_MISMATCH",
      "Provider call does not bind its production role prompt",
      { ordinal: index + 1 },
    );
  }
  return { schema_version: 1, status: "PASS", prompt_count: prompts.length };
}

export function auditFastInvocations(invocations, scenarioId) {
  if (scenarioId !== "insufficient-evidence") {
    return auditClaudeModelCertInvocations(invocations);
  }
  requireFast(
    Array.isArray(invocations) && invocations.length === 0,
    "CLAUDE_DEEPSEEK_DIAGNOSIS_STATUS_MISMATCH",
    "Insufficient-evidence Fast E2E must terminate before either model role",
  );
  return {
    schema_version: 1,
    status: "PASS",
    actual_call_count: 0,
    repair_counts: { specialist: 0, reviewer: 0 },
    aggregate: aggregateClaudeUsage([]),
  };
}

const SEMANTIC_CAUSE_MARKERS = Object.freeze({
  api_execution_overrun: Object.freeze([
    "API_COMPLETE service=",
    "DEADLOOP_DETECTED service=",
  ]),
  server_receive_queueing: Object.freeze(["QUEUE_HISTORY print_time_ms="]),
  client_receive_blocked: Object.freeze(["LATE_RESPONSE service="]),
});

export function semanticMethodMapping(methods) {
  const result = new Map();
  for (const [semanticId, markers] of Object.entries(SEMANTIC_CAUSE_MARKERS)) {
    const foldedMarkers = markers.map((marker) => marker.toLocaleLowerCase("en-US"));
    const alreadyMapped = new Set([...result.values()].map((method) => method.id));
    const candidates = methods.methods.filter((method) => (
      !alreadyMapped.has(method.id)
        && method.activation_markers.some((marker) => (
          foldedMarkers.includes(marker.toLocaleLowerCase("en-US"))
        ))
    ));
    requireFast(
      candidates.length === 1,
      "CLAUDE_DEEPSEEK_DIAGNOSIS_SHAPE_INVALID",
      "Fast E2E semantic method does not map to exactly one generated method card",
      { semantic_id: semanticId },
    );
    result.set(semanticId, candidates[0]);
  }
  requireFast(
    new Set([...result.values()].map((method) => method.id)).size === result.size,
    "CLAUDE_DEEPSEEK_DIAGNOSIS_SHAPE_INVALID",
    "Fast E2E semantic methods are not distinct",
  );
  return result;
}

function markerPrefix(value) {
  return `${value} `;
}

function roleReasons(evidenceRoot) {
  const statePath = path.join(evidenceRoot, "methods-terminal-state-v2.json");
  if (!fs.existsSync(statePath)) return [];
  const state = readJson(statePath);
  return [state.specialist_evaluation, state.reviewer_evaluation]
    .flatMap((evaluation) => evaluation?.evaluations ?? [])
    .map((evaluation) => evaluation?.reason)
    .filter((reason) => typeof reason === "string" && reason.length > 0);
}

export function auditOracle({ sourceRoot, scenarioId, evidenceRoot, runtimeReceipt }) {
  const expectation = deriveFastE2EV2Expectation(sourceRoot, scenarioId);
  const legacy = readJson(scenarioPaths(sourceRoot, scenarioId).case);
  const methods = readJson(path.join(evidenceRoot, "methods.json"));
  const graph = readJson(path.join(evidenceRoot, "methods-evidence-graph-v2.json"));
  const result = runtimeReceipt.methods_result;
  const methodBySemanticId = semanticMethodMapping(methods);
  const expectedConfirmed = methods.methods
    .filter((method) => expectation.expected_confirmed_semantic_ids.some((semanticId) => (
      methodBySemanticId.get(semanticId)?.id === method.id
    )))
    .map((method) => method.id);

  requireFast(
    result.status === expectation.expected_terminal_status,
    "CLAUDE_DEEPSEEK_PUBLIC_STATUS_MISMATCH",
    "Fast E2E terminal status differs from the historical oracle",
    { expected: expectation.expected_terminal_status, actual: result.status },
  );
  requireFast(
    canonicalJson(result.confirmed_method_ids) === canonicalJson(expectedConfirmed),
    "CLAUDE_DEEPSEEK_DIAGNOSIS_STATUS_MISMATCH",
    "Fast E2E confirmed methods differ from the historical oracle",
    { expected: expectedConfirmed, actual: result.confirmed_method_ids },
  );
  if (expectation.expected_terminal_status === "UNRESOLVED") {
    requireFast(
      result.reason_code === "NO_MATCHING_METHOD_EVIDENCE",
      "CLAUDE_DEEPSEEK_DIAGNOSIS_STATUS_MISMATCH",
      "Insufficient-evidence scenario did not terminate before model evaluation",
      { reason_code: result.reason_code },
    );
  }

  const confirmedHitRefs = new Set(result.confirmed_hit_refs);
  const confirmedEventRefs = new Set(result.confirmed_event_refs);
  const confirmedHits = graph.hits.filter((hit) => confirmedHitRefs.has(hit.hit_ref));
  const confirmedEvents = graph.events.filter((event) => confirmedEventRefs.has(event.event_ref));
  const hitByRef = new Map(graph.hits.map((hit) => [hit.hit_ref, hit]));
  for (const marker of expectation.source_expected_branch_markers) {
    const semanticId = FAST_E2E_MARKER_TO_METHOD[marker];
    const methodId = methodBySemanticId.get(semanticId).id;
    requireFast(
      confirmedHits.some((hit) => (
        hit.method_id === methodId
          && hit.marker.toLocaleLowerCase("en-US")
            .startsWith(markerPrefix(marker).toLocaleLowerCase("en-US"))
      )),
      "CLAUDE_DEEPSEEK_BRANCH_MARKER_MISSING",
      "Fast E2E confirmed evidence is missing an expected historical branch",
      { marker },
    );
  }

  const matchedEventRefs = new Set();
  for (const identity of legacy.expected_evidence_identities) {
    const semanticId = FAST_E2E_MARKER_TO_METHOD[identity.branch_marker];
    const methodId = methodBySemanticId.get(semanticId).id;
    const matches = confirmedEvents.filter((event) => (
      event.method_id === methodId
        && identity.identity_tokens.every((token) => event.identity_tokens.includes(token))
        && event.evidence_hit_refs.some((hitRef) => (
          hitByRef.get(hitRef)?.marker.toLocaleLowerCase("en-US")
            .startsWith(markerPrefix(identity.branch_marker).toLocaleLowerCase("en-US"))
        ))
    ));
    requireFast(
      matches.length === 1 && !matchedEventRefs.has(matches[0]?.event_ref),
      "CLAUDE_DEEPSEEK_EXPECTED_EVIDENCE_IDENTITY_MISMATCH",
      "Fast E2E evidence identity is missing, duplicated, or merged",
      { marker: identity.branch_marker, identity_tokens: identity.identity_tokens },
    );
    matchedEventRefs.add(matches[0].event_ref);
  }

  const selectedText = canonicalJson({
    hits: confirmedHits,
    events: confirmedEvents,
    reasons: [...roleReasons(evidenceRoot), ...(result.reasons ?? [])],
    limitations: result.limitations,
  });
  for (const term of legacy.expected_terms) {
    requireFast(
      selectedText.includes(term),
      "CLAUDE_DEEPSEEK_EXPECTED_TERM_MISSING",
      "Fast E2E confirmed result is missing a historical oracle term",
      { term },
    );
  }
  for (const term of legacy.forbidden_evidence_terms) {
    requireFast(
      !confirmedHits.some((hit) => hit.line.includes(term)),
      "CLAUDE_DEEPSEEK_FORBIDDEN_TERM_PRESENT",
      "Fast E2E confirmed evidence contains historical unrelated noise",
      { term },
    );
  }

  return Object.freeze({
    schema_version: 1,
    status: "PASS",
    scenario_id: scenarioId,
    expected_terminal_status: expectation.expected_terminal_status,
    actual_terminal_status: result.status,
    expected_confirmed_method_ids: expectedConfirmed,
    actual_confirmed_method_ids: result.confirmed_method_ids,
    confirmed_hit_refs: result.confirmed_hit_refs,
    confirmed_event_refs: result.confirmed_event_refs,
  });
}

export async function runFastE2E(options, {
  ambient = process.env,
  onProgress = null,
  validateIdentity = validateClaudeDeepseekIdentity,
  validateRegistration = validateFastRegistrationInput,
  runRuntime = runProductionRuntime,
  readInvocations = readRoleInvocationReceipts,
} = {}) {
  const sourceRoot = path.resolve(options.sourceRoot);
  const workRoot = createEmptyRoot(options.workRoot, "Fast E2E work root");
  const privateRoot = createEmptyRoot(options.privateRoot, "Fast E2E private root");
  const evidenceRoot = createEmptyRoot(options.evidenceRoot, "Fast E2E evidence root");
  const usageRoot = createEmptyRoot(options.usageRoot, "Fast E2E usage root");
  const scenario = scenarioPaths(sourceRoot, options.scenario);
  const identity = validateIdentity(options.claudeEntry, options.claudeSettings);
  const registration = validateRegistration(options.registrationRoot, sourceRoot);
  assertRegistrationUnchanged(registration);
  const stagedSettings = path.join(privateRoot, "claude-settings.json");
  materializeClaudeSettings(options.claudeSettings, stagedSettings);
  const configRoot = path.join(privateRoot, "claude-config");
  fs.mkdirSync(configRoot, { mode: 0o700 });
  onProgress?.("production-runtime");
  const runtimeReceipt = await runRuntime({
    ...options,
    sourceRoot,
    scenarioRoot: scenario.root,
    workRoot,
    privateRoot,
    evidenceRoot,
    usageRoot,
    stagedSettings,
    configRoot,
    registrationRoot: registration.registration_root,
  }, { ambient, onProgress });
  const invocations = readInvocations(usageRoot);
  const modelAudit = auditFastInvocations(invocations, options.scenario);
  auditRuntime(runtimeReceipt, invocations, options.scenario);

  writeJsonNew(path.join(evidenceRoot, "claude-identity.json"), {
    schema_version: 1,
    status: "PASS",
    claude: identity,
    producer: null,
    registration: registration.manifest.registration,
  });
  writeJsonNew(path.join(evidenceRoot, "methods-package.json"), {
    schema_version: 2,
    status: "PASS",
    producer_identity: null,
    registration_tree_sha256: registration.manifest.registration.tree_sha256,
    runtime_ref: registration.manifest.registration.runtime_ref ?? {
      id: `diagnosis-skill/${runtimeReceipt.registration_id}`,
      version: "1.0.0",
      content_hash: runtimeReceipt.scenario.skill_content_sha256,
    },
  });
  writeJsonNew(path.join(evidenceRoot, "model-invocations.json"), {
    schema_version: 1,
    status: "PASS",
    retry_policy: "ROLE_PROTOCOL_REPAIR_ONLY",
    invocations,
  });
  writeJsonNew(path.join(evidenceRoot, "model-usage.json"), {
    schema_version: 1,
    status: "PASS",
    usage_complete: true,
    aggregate: aggregateClaudeUsage(invocations),
  });

  let oracle;
  try {
    oracle = auditOracle({
      sourceRoot,
      scenarioId: options.scenario,
      evidenceRoot,
      runtimeReceipt,
    });
  } catch (error) {
    writeJsonNew(path.join(evidenceRoot, "fast-e2e-oracle.json"), {
      schema_version: 1,
      status: "FAIL",
      scenario_id: options.scenario,
      code: error?.code ?? "CLAUDE_DEEPSEEK_FAST_E2E_ORACLE_FAILED",
      message: error?.message ?? String(error),
      details: error?.details ?? {},
    });
    throw error;
  }
  writeJsonNew(path.join(evidenceRoot, "fast-e2e-oracle.json"), oracle);
  assertRegistrationUnchanged(registration);
  const gate = Object.freeze({
    schema_version: 1,
    status: "PASS",
    receipt_type: "evidence-v2-fast-e2e",
    provider: "claude-deepseek",
    model: CLAUDE_DEEPSEEK_MODEL,
    scenario_id: options.scenario,
    model_calls: modelAudit.actual_call_count,
    repairs: modelAudit.repair_counts,
    checks: {
      production_runtime: "PASS",
      role_calls: "PASS",
      historical_oracle: "PASS",
    },
  });
  writeJsonNew(path.join(evidenceRoot, "adapter-receipt.json"), gate);
  return gate;
}
