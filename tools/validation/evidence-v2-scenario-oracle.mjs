import fs from "node:fs";
import path from "node:path";

import {
  METHODS_V2_CAPTURED_FILES,
  validateMethodsV2ExecutionRecords,
} from "../test-flow/lib/methods-oracle.mjs";
import {
  discoverReleaseCaseRoot,
  loadReleaseCaseInputs,
  loadReleaseCaseOracle,
  releaseCaseDigests,
} from "../test-flow/lib/release-case.mjs";
import {
  assertFlow,
  canonicalJson,
  readJson,
  sha256Bytes,
  sha256File,
} from "../test-flow/lib/util.mjs";

const METHODS_ROOT_FIELDS = Object.freeze([
  "log_derived_fields",
  "methods",
  "required_artifacts",
  "required_user_inputs",
  "schema_version",
  "shared_references",
  "skill_name",
  "source_wiki_sha256",
]);
const METHOD_FIELDS = Object.freeze([
  "evidence_markers", "id", "priority", "reference", "title",
]);
const SUMMARY_FIELDS = Object.freeze([
  "case_id", "confirmed_method_ids", "diagnostic_id", "evaluation_count",
  "evaluation_id", "evidence_event_count", "evidence_hit_count", "graph_ref",
  "plan_ref", "public_methods_result_sha256", "record_sha256",
  "result_ref", "reviewer_job_id", "reviewer_repair_used", "schema_version",
  "service_model_calls", "source_job_id", "specialist_repair_used", "status",
]);

export const EVIDENCE_V2_SCENARIO_ORACLE_RECEIPT = "evidence-v2-scenario-oracle";
export const EVIDENCE_V2_SCENARIO_ORACLE_RECEIPT_FILENAME = "scenario-oracle-receipt.json";
export const EVIDENCE_V2_PUBLIC_METHODS_RESULT_FILENAME = "methods-result-v2.json";
export const EVIDENCE_V2_LOADED_METHODS_FILENAME = "methods.json";

function exactKeys(value, expected, code, label) {
  assertFlow(value !== null && typeof value === "object" && !Array.isArray(value), code, `${label} must be an object`);
  assertFlow(
    canonicalJson(Object.keys(value).sort()) === canonicalJson([...expected].sort()),
    code,
    `${label} fields do not match the contract`,
  );
}

function uniqueSortedStrings(value, code, label) {
  assertFlow(
    Array.isArray(value) && value.every((item) => typeof item === "string" && item.length > 0)
      && value.length === new Set(value).size,
    code,
    `${label} must contain unique non-empty strings`,
  );
  return [...value].sort();
}

function requireFile(root, relative, code, label) {
  assertFlow(typeof root === "string" && path.isAbsolute(root), code, `${label} root must be absolute`);
  assertFlow(typeof relative === "string" && relative.length > 0 && !relative.includes("\\") && !path.posix.isAbsolute(relative), code, `${label} path is invalid`);
  const absolute = path.resolve(root, ...relative.split("/"));
  const resolvedRoot = path.resolve(root);
  assertFlow(absolute.startsWith(`${resolvedRoot}${path.sep}`), code, `${label} escapes its root`);
  assertFlow(fs.existsSync(absolute) && fs.statSync(absolute).isFile(), code, `${label} is missing`);
  return absolute;
}

function methodSemanticMapping(methods, expectedPackage) {
  exactKeys(methods, METHODS_ROOT_FIELDS, "SCENARIO_ORACLE_METHODS_FIELDS", "copied methods.json");
  assertFlow(methods.schema_version === 1, "SCENARIO_ORACLE_METHODS_VERSION", "copied methods.json schema_version must be 1");
  assertFlow(
    methods.skill_name === expectedPackage.skill_name
      && methods.source_wiki_sha256 === expectedPackage.source_wiki_sha256
      && canonicalJson(uniqueSortedStrings(methods.required_user_inputs, "SCENARIO_ORACLE_METHODS_INPUTS", "Methods required inputs"))
        === canonicalJson(uniqueSortedStrings(expectedPackage.required_user_inputs, "SCENARIO_ORACLE_EXPECTED_INPUTS", "release required inputs"))
      && canonicalJson(uniqueSortedStrings(methods.required_artifacts, "SCENARIO_ORACLE_METHODS_ARTIFACTS", "Methods required artifacts"))
        === canonicalJson(uniqueSortedStrings(expectedPackage.required_artifacts, "SCENARIO_ORACLE_EXPECTED_ARTIFACTS", "release required artifacts"))
      && canonicalJson(uniqueSortedStrings(methods.log_derived_fields, "SCENARIO_ORACLE_METHODS_LOG_FIELDS", "Methods log fields"))
        === canonicalJson(uniqueSortedStrings(expectedPackage.required_log_derived_fields, "SCENARIO_ORACLE_EXPECTED_LOG_FIELDS", "release log fields")),
    "SCENARIO_ORACLE_METHODS_PACKAGE_DRIFT",
    "copied methods.json differs from the frozen release package contract",
  );
  uniqueSortedStrings(methods.shared_references, "SCENARIO_ORACLE_METHODS_SHARED_REFS", "Methods shared references");
  assertFlow(Array.isArray(methods.methods) && methods.methods.length > 0, "SCENARIO_ORACLE_METHODS_EMPTY", "copied methods.json has no methods");
  const methodIds = new Set();
  methods.methods.forEach((method, index) => {
    exactKeys(method, METHOD_FIELDS, "SCENARIO_ORACLE_METHOD_FIELDS", `Methods method ${index + 1}`);
    assertFlow(
      typeof method.id === "string" && method.id.length > 0 && !methodIds.has(method.id)
        && Number.isSafeInteger(method.priority) && method.priority === index + 1
        && typeof method.title === "string" && method.title.trim().length > 0
        && typeof method.reference === "string" && method.reference.startsWith("references/")
        && uniqueSortedStrings(method.evidence_markers, "SCENARIO_ORACLE_METHOD_MARKERS", `Methods method ${index + 1} markers`).length > 0,
      "SCENARIO_ORACLE_METHOD_INVALID",
      "copied methods.json contains an invalid method card",
    );
    methodIds.add(method.id);
  });
  const generated = expectedPackage.method_marker_sets.map((semantic) => {
    const semanticMarkers = uniqueSortedStrings(semantic.all_markers, "SCENARIO_ORACLE_EXPECTED_MARKERS", "release semantic markers");
    const matches = methods.methods.filter((method) => (
      canonicalJson(uniqueSortedStrings(method.evidence_markers, "SCENARIO_ORACLE_METHOD_MARKERS", "Methods markers"))
        === canonicalJson(semanticMarkers)
    ));
    assertFlow(matches.length === 1, "SCENARIO_ORACLE_METHOD_SEMANTIC_MAPPING", "release semantic method does not map to exactly one copied method card");
    return { semantic_id: semantic.semantic_id, markers: semanticMarkers, method: matches[0] };
  });
  assertFlow(
    generated.length === methods.methods.length && new Set(generated.map((item) => item.method.id)).size === methods.methods.length,
    "SCENARIO_ORACLE_METHOD_SET_DRIFT",
    "copied methods.json contains a missing or extra semantic method",
  );
  return generated;
}

function mapMarkerGroups(groups, generated, code) {
  const selected = groups.map((group) => {
    const markers = uniqueSortedStrings(group, code, "release marker group");
    const candidates = generated.filter((entry) => markers.every((marker) => (
      entry.markers.some((declared) => declared.includes(marker))
    )));
    assertFlow(candidates.length > 0, code, "release marker group does not map to a copied method card");
    const minimumMarkerCount = Math.min(...candidates.map((entry) => entry.markers.length));
    const minimal = candidates.filter((entry) => entry.markers.length === minimumMarkerCount);
    assertFlow(minimal.length === 1, `${code}_AMBIGUOUS`, "release marker group maps ambiguously");
    return minimal[0].method.id;
  });
  assertFlow(selected.length === new Set(selected).size, `${code}_DUPLICATE`, "release marker groups map to duplicate methods");
  return selected;
}

function buildMethodsExpectation({ methods, inputs, gateOracle, scenarioOracle }) {
  const generated = methodSemanticMapping(methods, gateOracle.semantic_oracle.expected_package);
  const confirmed = mapMarkerGroups(
    scenarioOracle.required_confirmed_marker_groups,
    generated,
    "SCENARIO_ORACLE_CONFIRMED_MAPPING",
  );
  mapMarkerGroups(
    scenarioOracle.required_candidate_marker_groups,
    generated,
    "SCENARIO_ORACLE_CANDIDATE_MAPPING",
  );
  const requiredEvidenceIdentities = scenarioOracle.required_evidence_identities.map((identity) => {
    const [methodId] = mapMarkerGroups(
      [[identity.marker]],
      generated,
      "SCENARIO_ORACLE_EVIDENCE_IDENTITY_MAPPING",
    );
    assertFlow(confirmed.includes(methodId), "SCENARIO_ORACLE_EVIDENCE_METHOD_UNCONFIRMED", "required evidence maps to an unconfirmed method");
    return {
      method_id: methodId,
      marker: identity.marker,
      identity_tokens: uniqueSortedStrings(identity.identity_tokens, "SCENARIO_ORACLE_EVIDENCE_TOKENS", "release evidence identity tokens"),
    };
  });
  const methodCards = generated.map(({ method }) => ({
    id: method.id,
    priority: method.priority,
    evidence_markers: [...method.evidence_markers],
  })).sort((left, right) => left.priority - right.priority || (left.id < right.id ? -1 : left.id > right.id ? 1 : 0));
  const confirmedSet = new Set(confirmed);
  return {
    method_cards: methodCards,
    loaded_method_ids: methodCards.map((method) => method.id),
    confirmed_method_ids: methodCards.map((method) => method.id).filter((methodId) => confirmedSet.has(methodId)),
    required_evidence_identities: requiredEvidenceIdentities,
    source_ids: [...inputs.scenarios.find((item) => item.scenario_id === inputs.journey_scenario).driver.attachment_anchor_names].sort(),
  };
}

function validateScenarioAgainstFrozenSource({ scenario, inputs, caseRoot, files, publicMethodsResult, sourceJob }) {
  const selected = inputs.scenarios.find((item) => item.scenario_id === inputs.journey_scenario);
  assertFlow(selected, "SCENARIO_ORACLE_RELEASE_SCENARIO_MISSING", "frozen release scenario is missing");
  const sourceById = Object.fromEntries(selected.driver.attachment_anchor_names.map((sourceId, index) => [
    sourceId,
    sha256File(selected.attachment_paths[index]),
  ]));
  const userInputs = {
    initial_user_fact_names: selected.driver.initial_user_fact_names,
    initial_user_fact_values: selected.driver.initial_user_fact_values,
  };
  const graph = JSON.parse(files.evidence_graph.toString("utf8"));
  const plan = JSON.parse(files.evaluation_plan.toString("utf8"));
  assertFlow(
    scenario?.scenario_id === selected.scenario_id
      && scenario.source_wiki_sha256 === sha256File(inputs.wiki_path)
      && scenario.registration_id === inputs.product_registration.registration_id
      && scenario.skill_content_sha256 === sourceJob.skill_ref?.content_hash
      && scenario.user_inputs_sha256 === sha256Bytes(canonicalJson(userInputs))
      && canonicalJson(scenario.sources) === canonicalJson(graph.sources.map((source) => ({ source_id: source.source_id, content_sha256: source.content_sha256 })))
      && scenario.sources.every((source) => sourceById[source.source_id] === source.content_sha256)
      && scenario.evidence_graph?.ref === graph.graph_ref
      && scenario.evidence_graph?.canonical_sha256 === sha256Bytes(files.evidence_graph)
      && scenario.evidence_graph?.canonical_size === files.evidence_graph.length
      && scenario.evaluation_plan?.ref === plan.plan_ref
      && scenario.evaluation_plan?.canonical_sha256 === sha256Bytes(files.evaluation_plan)
      && scenario.evaluation_plan?.canonical_size === files.evaluation_plan.length
      && publicMethodsResult.evidence_graph_ref === graph.graph_ref
      && publicMethodsResult.plan_ref === plan.plan_ref,
    "SCENARIO_ORACLE_FROZEN_SOURCE_MISMATCH",
    "production execution differs from the frozen release scenario",
  );
  return selected;
}

function invocationProjection(providerInvocations, modelId, sourceJob, reviewerJob) {
  assertFlow(Array.isArray(providerInvocations), "SCENARIO_ORACLE_PROVIDER_INVOCATIONS", "provider role receipts are missing");
  return providerInvocations.map((invocation) => ({
    effective_model: modelId,
    job_id: invocation.role === "SPECIALIST" ? sourceJob.job_id : reviewerJob.job_id,
    job_type: invocation.role === "SPECIALIST" ? "DIAGNOSE" : "REVIEW",
  }));
}

function summaryBinding(summary) {
  exactKeys(summary, SUMMARY_FIELDS, "SCENARIO_ORACLE_SUMMARY_FIELDS", "Methods V2 oracle summary");
  return summary;
}

export function buildEvidenceV2ScenarioOracleReceipt({
  sourceRoot,
  certRoot,
  scenario,
  providerInvocations,
  modelId,
}) {
  assertFlow(typeof sourceRoot === "string" && path.isAbsolute(sourceRoot), "SCENARIO_ORACLE_SOURCE_ROOT", "source root must be absolute");
  assertFlow(typeof certRoot === "string" && path.isAbsolute(certRoot), "SCENARIO_ORACLE_CERT_ROOT", "certification root must be absolute");
  assertFlow(typeof modelId === "string" && modelId.length > 0, "SCENARIO_ORACLE_MODEL_ID", "provider model id is missing");
  const caseRoot = discoverReleaseCaseRoot(path.join(sourceRoot, "tests", "cases", "release"));
  const inputs = loadReleaseCaseInputs(caseRoot);
  const gateOracle = loadReleaseCaseOracle(caseRoot);
  assertFlow(inputs.registration_template.deployment_scope === "PRODUCTION", "SCENARIO_ORACLE_REGISTRATION_SCOPE", "shared release registration must be production-reachable");
  const digests = releaseCaseDigests(caseRoot);
  const scenarioOracleEntry = gateOracle.scenarios.find((item) => item.scenario_id === inputs.journey_scenario);
  assertFlow(scenarioOracleEntry, "SCENARIO_ORACLE_RELEASE_ORACLE_MISSING", "frozen release scenario oracle is missing");

  const methodsPath = requireFile(certRoot, EVIDENCE_V2_LOADED_METHODS_FILENAME, "SCENARIO_ORACLE_METHODS_MISSING", "copied methods.json");
  const methods = readJson(methodsPath);
  const files = Object.fromEntries(Object.entries(METHODS_V2_CAPTURED_FILES).map(([key, filename]) => [
    key,
    fs.readFileSync(requireFile(certRoot, filename, "SCENARIO_ORACLE_EXECUTION_FILE_MISSING", filename)),
  ]));
  const publicPath = requireFile(certRoot, EVIDENCE_V2_PUBLIC_METHODS_RESULT_FILENAME, "SCENARIO_ORACLE_PUBLIC_RESULT_MISSING", "public methods_result");
  const publicBytes = fs.readFileSync(publicPath);
  assertFlow(publicBytes.equals(Buffer.from(canonicalJson(JSON.parse(publicBytes.toString("utf8"))), "utf8")), "SCENARIO_ORACLE_PUBLIC_RESULT_NON_CANONICAL", "public methods_result is not canonical JSON bytes");
  const publicMethodsResult = JSON.parse(publicBytes.toString("utf8"));
  const sourceJob = JSON.parse(files.source_job.toString("utf8"));
  const reviewerJob = JSON.parse(files.reviewer_job.toString("utf8"));
  assertFlow(
    sourceJob.case_id === reviewerJob.case_id
      && sourceJob.skill_ref?.id === inputs.product_registration.runtime_ref_id
      && sourceJob.skill_ref?.version === inputs.product_registration.version
      && reviewerJob.skill_ref?.id === inputs.product_registration.runtime_ref_id
      && reviewerJob.skill_ref?.version === inputs.product_registration.version,
    "SCENARIO_ORACLE_JOB_REGISTRATION_MISMATCH",
    "production Jobs do not bind the frozen product registration",
  );
  validateScenarioAgainstFrozenSource({ scenario, inputs, caseRoot, files, publicMethodsResult, sourceJob });
  const methodsExpectation = buildMethodsExpectation({
    methods,
    inputs,
    gateOracle,
    scenarioOracle: scenarioOracleEntry.oracle,
  });
  const summary = validateMethodsV2ExecutionRecords({
    files,
    expected: {
      source_job_id: sourceJob.job_id,
      reviewer_job_id: reviewerJob.job_id,
      case_id: sourceJob.case_id,
      skill_ref: sourceJob.skill_ref,
      ...methodsExpectation,
    },
    invocations: invocationProjection(providerInvocations, modelId, sourceJob, reviewerJob),
    publicMethodsResult,
  });
  assertFlow(summary.public_methods_result_sha256 === sha256Bytes(publicBytes), "SCENARIO_ORACLE_PUBLIC_RESULT_DIGEST", "public methods_result digest differs from the full oracle result");
  const executionRecords = Object.fromEntries(Object.entries(METHODS_V2_CAPTURED_FILES).map(([key, filename]) => [key, {
    path: filename,
    sha256: sha256Bytes(files[key]),
  }]));
  return {
    schema_version: 1,
    receipt_type: EVIDENCE_V2_SCENARIO_ORACLE_RECEIPT,
    status: "PASS",
    scenario_id: inputs.journey_scenario,
    release_case: {
      case_id: inputs.case_id,
      input_digest: digests.input_digest,
      oracle_digest: digests.oracle_digest,
    },
    methods: {
      path: EVIDENCE_V2_LOADED_METHODS_FILENAME,
      sha256: sha256File(methodsPath),
    },
    execution_records: executionRecords,
    public_methods_result: {
      path: EVIDENCE_V2_PUBLIC_METHODS_RESULT_FILENAME,
      sha256: sha256Bytes(publicBytes),
    },
    provider_role_receipts_sha256: sha256Bytes(canonicalJson(providerInvocations)),
    summary: summaryBinding(summary),
  };
}

export function validateEvidenceV2ScenarioOracleReceipt(receipt, options) {
  exactKeys(receipt, [
    "execution_records", "methods", "provider_role_receipts_sha256",
    "public_methods_result", "receipt_type", "release_case", "scenario_id",
    "schema_version", "status", "summary",
  ], "SCENARIO_ORACLE_RECEIPT_FIELDS", "scenario oracle receipt");
  assertFlow(receipt.schema_version === 1, "SCENARIO_ORACLE_RECEIPT_VERSION", "scenario oracle receipt schema_version must be 1");
  assertFlow(receipt.receipt_type === EVIDENCE_V2_SCENARIO_ORACLE_RECEIPT, "SCENARIO_ORACLE_RECEIPT_TYPE", "scenario oracle receipt type is invalid");
  assertFlow(receipt.status === "PASS", "SCENARIO_ORACLE_RECEIPT_STATUS", "scenario oracle receipt status must be PASS");
  const replayed = buildEvidenceV2ScenarioOracleReceipt(options);
  assertFlow(canonicalJson(receipt) === canonicalJson(replayed), "SCENARIO_ORACLE_REPLAY_MISMATCH", "scenario oracle receipt does not match replayed production evidence");
  return receipt;
}
