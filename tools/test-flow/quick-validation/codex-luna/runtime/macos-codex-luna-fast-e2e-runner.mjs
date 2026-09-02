import fs from "node:fs";
import path from "node:path";

import {
  canonicalJson,
  codexLunaAppServerCliVersion,
  treeDigest,
  validateCodexLunaIdentity,
} from "../../../runtime-support/codex-luna-contract.mjs";
import {
  MACOS_CODEX_LUNA_PRICE_SNAPSHOT,
} from "./macos-codex-luna-e2e-contract.mjs";
import { runProductionRuntime } from "./macos-codex-luna-e2e-runner.mjs";
import { readModelCertInvocationReceipts } from "./macos-codex-luna-model-cert-wrapper.mjs";
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
      "CODEX_LUNA_FAST_E2E_ROOT_NOT_EMPTY",
      `${label} must be empty`,
    );
  } else fs.mkdirSync(resolved, { recursive: true, mode: 0o700 });
  return resolved;
}

function writeJsonNew(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, `${canonicalJson(value)}\n`, {
    encoding: "utf8",
    mode: 0o600,
    flag: "wx",
  });
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function explicitFastRegistrationInput({ options }) {
  requireFast(
    typeof options.registrationRoot === "string" && options.registrationRoot.length > 0,
    "CODEX_LUNA_FAST_E2E_REGISTRATION_ROOT_REQUIRED",
    "Fast E2E requires one explicit production registration root",
  );
  const root = path.resolve(options.registrationRoot);
  const metadata = fs.lstatSync(root);
  requireFast(
    metadata.isDirectory() && !metadata.isSymbolicLink(),
    "CODEX_LUNA_FAST_E2E_REGISTRATION_INVALID",
    "Fast E2E production registration must be a plain directory",
  );
  return {
    registration: {
      root,
      source: "external-validated-production-registration",
      tree_sha256: treeDigest(root),
    },
    producer: null,
    cache: null,
  };
}

function aggregateUsage(invocations) {
  const result = {
    schema_version: 1,
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    cost_usd: 0,
  };
  for (const invocation of invocations) {
    const input = Number(invocation.usage?.input_tokens);
    const output = Number(invocation.usage?.output_tokens);
    requireFast(
      Number.isSafeInteger(input) && input >= 0
        && Number.isSafeInteger(output) && output >= 0,
      "CODEX_LUNA_FAST_E2E_USAGE_INVALID",
      "Fast E2E provider usage is incomplete",
    );
    result.input_tokens += input;
    result.output_tokens += output;
    result.total_tokens += input + output;
    result.cost_usd += (
      input * MACOS_CODEX_LUNA_PRICE_SNAPSHOT.rates.input
      + output * MACOS_CODEX_LUNA_PRICE_SNAPSHOT.rates.output
    ) / 1_000_000;
  }
  result.cost_usd = Math.round(result.cost_usd * 1_000_000) / 1_000_000;
  return result;
}

function auditRuntime(runtimeReceipt, invocations, scenarioId) {
  requireFast(
    runtimeReceipt?.status === "PASS"
      && runtimeReceipt.execution_mode === "real-model"
      && runtimeReceipt.production_runtime
        === "problem_locator.runtime.diagnosis_runtime.DiagnosisRuntime",
    "CODEX_LUNA_FAST_E2E_RUNTIME_RECEIPT_INVALID",
    "Fast E2E did not use the production Evidence V2 Runtime",
  );
  requireFast(
    runtimeReceipt.scenario_id === scenarioId
      && runtimeReceipt.scenario?.scenario_id === scenarioId,
    "CODEX_LUNA_FAST_E2E_SCENARIO_IDENTITY_MISMATCH",
    "Production Runtime used a different Fast E2E scenario",
  );
  const prompts = runtimeReceipt.role_attempts;
  requireFast(
    Array.isArray(prompts)
      && prompts.length === invocations.length
      && runtimeReceipt.model_invocations === invocations.length,
    "CODEX_LUNA_FAST_E2E_INVOCATION_COUNT_MISMATCH",
    "Production role prompts differ from provider calls",
  );
  for (const [index, invocation] of invocations.entries()) {
    const prompt = prompts[index];
    requireFast(
      invocation.role === prompt.role
        && invocation.attempt === prompt.attempt
        && invocation.prompt?.sha256 === prompt.prompt?.sha256
        && invocation.prompt?.size === prompt.prompt?.size,
      "CODEX_LUNA_FAST_E2E_INVOCATION_IDENTITY_MISMATCH",
      "Provider call does not bind its production role prompt",
      { ordinal: index + 1 },
    );
  }
}

const SEMANTIC_CAUSE_MARKERS = Object.freeze({
  api_execution_overrun: Object.freeze([
    "API_COMPLETE service=",
    "DEADLOOP_DETECTED service=",
  ]),
  server_receive_queueing: Object.freeze(["QUEUE_HISTORY print_time_ms="]),
  client_receive_blocked: Object.freeze(["LATE_RESPONSE service="]),
});

function semanticMethodMapping(methods) {
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
      "MACOS_CODEX_LUNA_DIAGNOSIS_SHAPE_INVALID",
      "Fast E2E semantic method does not map to exactly one generated method card",
      { semantic_id: semanticId },
    );
    result.set(semanticId, candidates[0]);
  }
  requireFast(
    new Set([...result.values()].map((method) => method.id)).size === result.size,
    "MACOS_CODEX_LUNA_DIAGNOSIS_SHAPE_INVALID",
    "Fast E2E semantic methods are not distinct",
  );
  return result;
}

function markerPrefix(value) {
  return value.endsWith(" service=") || value.endsWith(" print_time_ms=")
    ? value
    : `${value} `;
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

function auditOracle({ sourceRoot, scenarioId, evidenceRoot, runtimeReceipt }) {
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
    "MACOS_CODEX_LUNA_PUBLIC_STATUS_MISMATCH",
    "Fast E2E terminal status differs from the historical oracle",
    { expected: expectation.expected_terminal_status, actual: result.status },
  );
  requireFast(
    canonicalJson(result.confirmed_method_ids) === canonicalJson(expectedConfirmed),
    "MACOS_CODEX_LUNA_DIAGNOSIS_STATUS_MISMATCH",
    "Fast E2E confirmed methods differ from the historical oracle",
    { expected: expectedConfirmed, actual: result.confirmed_method_ids },
  );
  if (expectation.expected_terminal_status === "UNRESOLVED") {
    requireFast(
      result.reason_code === "NO_MATCHING_METHOD_EVIDENCE",
      "MACOS_CODEX_LUNA_DIAGNOSIS_STATUS_MISMATCH",
      "Insufficient-evidence scenario did not terminate before model evaluation",
      { reason_code: result.reason_code },
    );
  }

  const confirmedHitRefs = new Set(result.confirmed_hit_refs);
  const confirmedEventRefs = new Set(result.confirmed_event_refs);
  const confirmedHits = graph.hits.filter((hit) => confirmedHitRefs.has(hit.hit_ref));
  const confirmedEvents = graph.events.filter((event) => confirmedEventRefs.has(event.event_ref));
  for (const marker of expectation.source_expected_branch_markers) {
    const semanticId = FAST_E2E_MARKER_TO_METHOD[marker];
    const methodId = methodBySemanticId.get(semanticId).id;
    requireFast(
      confirmedHits.some((hit) => (
        hit.method_id === methodId
          && hit.marker.toLocaleLowerCase("en-US")
            .startsWith(markerPrefix(marker).toLocaleLowerCase("en-US"))
      )),
      "MACOS_CODEX_LUNA_BRANCH_MARKER_MISSING",
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
    ));
    requireFast(
      matches.length === 1 && !matchedEventRefs.has(matches[0].event_ref),
      "MACOS_CODEX_LUNA_EXPECTED_EVIDENCE_IDENTITY_MISMATCH",
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
      "MACOS_CODEX_LUNA_EXPECTED_TERM_MISSING",
      "Fast E2E confirmed result is missing a historical oracle term",
      { term },
    );
  }
  for (const term of legacy.forbidden_evidence_terms) {
    requireFast(
      !confirmedHits.some((hit) => hit.line.includes(term)),
      "MACOS_CODEX_LUNA_FORBIDDEN_TERM_PRESENT",
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
  validateIdentity = validateCodexLunaIdentity,
  registrationInput = explicitFastRegistrationInput,
  runRuntime = runProductionRuntime,
  readInvocations = readModelCertInvocationReceipts,
} = {}) {
  const sourceRoot = path.resolve(options.sourceRoot);
  const workRoot = createEmptyRoot(options.workRoot, "Fast E2E work root");
  const privateRoot = createEmptyRoot(options.privateRoot, "Fast E2E private root");
  const evidenceRoot = createEmptyRoot(options.evidenceRoot, "Fast E2E evidence root");
  const usageRoot = createEmptyRoot(options.usageRoot, "Fast E2E usage root");
  const scenario = scenarioPaths(sourceRoot, options.scenario);
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
    scenarioRoot: scenario.root,
    evaluationMode: "BLIND_CONSENSUS",
    skillSource: path.join(
      sourceRoot,
      "tools/test-flow/quick-validation/codex-luna/fixtures/model-cert-skill/codex-luna-evidence-v2-evaluator/SKILL.md",
    ),
    expectedCliVersion: codexLunaAppServerCliVersion(),
  }, { ambient, onProgress });
  const invocations = readInvocations(usageRoot, {
    allowFailurePrefix: true,
    evaluationMode: "BLIND_CONSENSUS",
  });
  auditRuntime(runtimeReceipt, invocations, options.scenario);

  writeJsonNew(path.join(evidenceRoot, "codex-identity.json"), {
    schema_version: 1,
    status: "PASS",
    codex: identity,
    producer: resolved.producer,
    registration: resolved.registration,
  });
  writeJsonNew(path.join(evidenceRoot, "methods-package.json"), {
    schema_version: 2,
    status: "PASS",
    registration_source: resolved.registration.source,
    registration_tree_sha256: resolved.registration.tree_sha256,
    registration_id: runtimeReceipt.registration_id,
    skill_content_sha256: runtimeReceipt.scenario.skill_content_sha256,
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
    aggregate: aggregateUsage(invocations),
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
      code: error?.code ?? "MACOS_CODEX_LUNA_FAST_E2E_ORACLE_FAILED",
      message: error?.message ?? String(error),
      details: error?.details ?? {},
    });
    throw error;
  }
  writeJsonNew(path.join(evidenceRoot, "fast-e2e-oracle.json"), oracle);
  requireFast(
    treeDigest(resolved.registration.root) === resolved.registration.tree_sha256,
    "CODEX_LUNA_FAST_E2E_REGISTRATION_DRIFT",
    "Fast E2E registration changed during the scenario",
  );
  const gate = Object.freeze({
    schema_version: 1,
    status: "PASS",
    receipt_type: "evidence-v2-fast-e2e",
    scenario_id: options.scenario,
    model_calls: invocations.length,
    repairs: runtimeReceipt.repair_counts,
    checks: {
      production_runtime: "PASS",
      role_calls: "PASS",
      historical_oracle: "PASS",
    },
  });
  writeJsonNew(path.join(evidenceRoot, "adapter-receipt.json"), gate);
  return gate;
}

export {
  auditOracle,
  auditRuntime,
  semanticMethodMapping,
};
