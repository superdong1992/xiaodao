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
      entry.markers.some((declared) => declared === marker)
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
  assertFlow(
    scenarioOracle.expected_status === "RESOLVED",
    "SCENARIO_ORACLE_EXPECTED_STATUS",
    "Evidence V2 model certification requires a resolved scenario",
  );
  assertFlow(
    scenarioOracle.required_candidate_marker_groups.length === 0,
    "SCENARIO_ORACLE_CANDIDATES_PRESENT",
    "A resolved Evidence V2 scenario cannot retain candidate methods",
  );
  const confirmed = mapMarkerGroups(
    scenarioOracle.required_confirmed_marker_groups,
    generated,
    "SCENARIO_ORACLE_CONFIRMED_MAPPING",
  );
  const candidate = mapMarkerGroups(
    scenarioOracle.required_candidate_marker_groups,
    generated,
    "SCENARIO_ORACLE_CANDIDATE_MAPPING",
  );
  assertFlow(candidate.length === 0, "SCENARIO_ORACLE_CANDIDATES_PRESENT", "Resolved scenario candidate mapping must be empty");
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
  const confirmedMethodIds = methodCards.map((method) => method.id).filter((methodId) => confirmedSet.has(methodId));
  assertFlow(
    confirmedMethodIds.length === generated.length,
    "SCENARIO_ORACLE_CONFIRMED_COVERAGE",
    "The resolved Release scenario must mechanically confirm every generated method",
  );
  return {
    generated,
    expected: {
      method_cards: methodCards,
      loaded_method_ids: methodCards.map((method) => method.id),
      confirmed_method_ids: confirmedMethodIds,
      required_evidence_identities: requiredEvidenceIdentities,
      source_ids: [...inputs.scenarios.find((item) => item.scenario_id === inputs.journey_scenario).driver.attachment_anchor_names].sort(),
    },
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
  return { selected, graph, plan };
}

function exactOne(values, code, message) {
  assertFlow(values.length === 1, code, message);
  return values[0];
}

function semanticMethod(generated, semanticId) {
  return exactOne(
    generated.filter((entry) => entry.semantic_id === semanticId),
    "SCENARIO_ORACLE_SEMANTIC_METHOD_MISSING",
    `Release semantic method is missing: ${semanticId}`,
  );
}

function methodHits(graph, generatedMethod, marker) {
  return graph.hits.filter((hit) => (
    hit.method_id === generatedMethod.method.id && hit.marker === marker
  ));
}

function lineFields(line) {
  return Object.fromEntries(
    [...line.matchAll(/(?<![A-Za-z0-9_])([a-z][a-z0-9_]*)=([^\s,;)]+)/g)]
      .map((match) => [match[1], match[2]]),
  );
}

function integerField(fields, name, code) {
  const value = Number(fields[name]);
  assertFlow(Number.isSafeInteger(value) && value >= 0, code, `Release evidence field is not a non-negative integer: ${name}`);
  return value;
}

function frozenLinesBySource(selected) {
  return Object.fromEntries(selected.driver.attachment_anchor_names.map((sourceId, index) => [
    sourceId,
    fs.readFileSync(selected.attachment_paths[index], "utf8").split(/\r?\n/),
  ]));
}

function validateFrozenGraphLines(graph, selected) {
  const linesBySource = frozenLinesBySource(selected);
  for (const hit of graph.hits) {
    const lines = linesBySource[hit.source_id];
    assertFlow(
      Array.isArray(lines) && lines[hit.line_number - 1] === hit.line,
      "SCENARIO_ORACLE_GRAPH_LINE_MISMATCH",
      "Production Evidence Graph hit differs from its frozen source line",
    );
  }
}

function validateRequestTimeoutEvidence({ graph, generated, timeoutExpectation, target }) {
  let linkedTimeout = null;
  for (const entry of generated) {
    const linked = exactOne(
      methodHits(graph, entry, timeoutExpectation.marker),
      "SCENARIO_ORACLE_LINKED_TIMEOUT_MISSING",
      "Each timeout-dependent method must receive the request-id-qualified timeout hit",
    );
    const linkedMatch = /Context=([^\s]+) rpc ([^\s]+) call unsuccess, reqid\((\d+)\), timeout (\d+)\)?$/.exec(linked.line);
    assertFlow(linkedMatch !== null, "SCENARIO_ORACLE_LINKED_TIMEOUT_INVALID", "Qualified client timeout evidence has an invalid shape");
    const [, service, api, requestId, timeoutText] = linkedMatch;
    const timeoutMs = Number(timeoutText);
    assertFlow(
      service === target.service && api === target.api
        && requestId === timeoutExpectation.request_id
        && timeoutMs === timeoutExpectation.timeout_ms,
      "SCENARIO_ORACLE_LINKED_TIMEOUT_MISMATCH",
      "The target request does not bind the exact frozen timeout",
    );
    linkedTimeout ??= { hit: linked, requestId, timeoutMs };
    assertFlow(
      linkedTimeout.requestId === requestId && linkedTimeout.timeoutMs === timeoutMs,
      "SCENARIO_ORACLE_LINKED_TIMEOUT_METHOD_DRIFT",
      "Method-qualified timeout hits disagree",
    );

    const unlinked = exactOne(
      methodHits(graph, entry, timeoutExpectation.unlinked_marker),
      "SCENARIO_ORACLE_UNLINKED_TIMEOUT_MISSING",
      "Each timeout-dependent method must retain the unlinked timeout hit separately",
    );
    const unlinkedMatch = /Context=rpc call ([^:\s]+):([^\s]+) timeout limit (\d+) recv no response\)?$/.exec(unlinked.line);
    assertFlow(unlinkedMatch !== null, "SCENARIO_ORACLE_UNLINKED_TIMEOUT_INVALID", "Unlinked client timeout evidence has an invalid shape");
    assertFlow(
      unlinkedMatch[1] === target.service && unlinkedMatch[2] === target.api
        && Number(unlinkedMatch[3]) === timeoutExpectation.unlinked_timeout_ms
        && !unlinked.line.includes(`reqid(${timeoutExpectation.request_id})`),
      "SCENARIO_ORACLE_UNLINKED_TIMEOUT_MISMATCH",
      "The unlinked timeout evidence changed or was incorrectly associated with the target request",
    );
    assertFlow(
      linked.hit_ref !== unlinked.hit_ref,
      "SCENARIO_ORACLE_TIMEOUT_EVENTS_MERGED",
      "Qualified and unlinked timeout evidence must remain independent hits",
    );
  }
  return linkedTimeout;
}

function validateClientSegments({ graph, generated, linkedTimeout, target }) {
  const method = semanticMethod(generated, "client_receive_blocked");
  const hit = exactOne(
    methodHits(graph, method, "LATE_RESPONSE service="),
    "SCENARIO_ORACLE_LATE_RESPONSE_MISSING",
    "Client receive blocking requires exactly one target late-response hit",
  );
  const fields = lineFields(hit.line);
  assertFlow(
    fields.service === target.service && fields.api === target.api
      && fields.request_id === linkedTimeout.requestId,
    "SCENARIO_ORACLE_LATE_RESPONSE_TARGET_MISMATCH",
    "Late-response evidence does not belong to the target request",
  );
  const clientSend = integerField(fields, "client_send_us", "SCENARIO_ORACLE_LATE_RESPONSE_FIELDS");
  const serverRecv = integerField(fields, "server_recv_us", "SCENARIO_ORACLE_LATE_RESPONSE_FIELDS");
  const serverSend = integerField(fields, "server_send_us", "SCENARIO_ORACLE_LATE_RESPONSE_FIELDS");
  const clientNow = integerField(fields, "client_now_us", "SCENARIO_ORACLE_LATE_RESPONSE_FIELDS");
  const timeoutUs = linkedTimeout.timeoutMs * 1000;
  const serverQueueUs = serverRecv - clientSend;
  const serverExecutionUs = serverSend - serverRecv;
  const clientQueueUs = clientNow - serverSend;
  const endToEndUs = clientNow - clientSend;
  assertFlow(
    serverQueueUs >= 0 && serverExecutionUs >= 0 && clientQueueUs >= 0
      && endToEndUs > timeoutUs
      && serverExecutionUs < timeoutUs
      && clientQueueUs > serverQueueUs,
    "SCENARIO_ORACLE_CLIENT_SEGMENTS_NOT_CONFIRMED",
    "Frozen late-response segments do not confirm client receive blocking",
  );
}

function validateApiCompleteEvents({ graph, generated, target }) {
  const method = semanticMethod(generated, "api_execution_overrun");
  const hits = methodHits(graph, method, "API_COMPLETE service=");
  assertFlow(hits.length === 2, "SCENARIO_ORACLE_API_EVENT_COUNT", "Release scenario must retain exactly two API completion hits");
  const eventRefs = [];
  for (const hit of hits) {
    const fields = lineFields(hit.line);
    const startUs = integerField(fields, "start_us", "SCENARIO_ORACLE_API_FIELDS");
    const endUs = integerField(fields, "end_us", "SCENARIO_ORACLE_API_FIELDS");
    const costUs = integerField(fields, "cost_us", "SCENARIO_ORACLE_API_FIELDS");
    assertFlow(
      fields.service === target.service && fields.api === target.api
        && endUs > startUs && costUs === endUs - startUs,
      "SCENARIO_ORACLE_API_EVENT_INVALID",
      "API completion evidence does not satisfy the frozen execution invariant",
    );
    const event = exactOne(
      graph.events.filter((item) => item.method_id === method.method.id && item.evidence_hit_refs.includes(hit.hit_ref)),
      "SCENARIO_ORACLE_API_EVENT_MISSING",
      "Each API completion hit must belong to one production Evidence event",
    );
    eventRefs.push(event.event_ref);
  }
  assertFlow(new Set(eventRefs).size === 2, "SCENARIO_ORACLE_API_EVENTS_MERGED", "Independent API completion calls were merged into one Evidence event");
}

function validateQueueHistory({ graph, generated, target }) {
  const method = semanticMethod(generated, "server_receive_queueing");
  const hits = methodHits(graph, method, "QUEUE_HISTORY print_time_ms=");
  assertFlow(hits.length === 2, "SCENARIO_ORACLE_QUEUE_HISTORY_COUNT", "Release scenario must retain one two-record queue-history group");
  const records = hits.map((hit) => {
    const fields = lineFields(hit.line);
    return {
      hit,
      fields,
      printTimeMs: integerField(fields, "print_time_ms", "SCENARIO_ORACLE_QUEUE_FIELDS"),
      endUs: integerField(fields, "end_us", "SCENARIO_ORACLE_QUEUE_FIELDS"),
      costUs: integerField(fields, "cost_us", "SCENARIO_ORACLE_QUEUE_FIELDS"),
      queueUs: integerField(fields, "queue_us", "SCENARIO_ORACLE_QUEUE_FIELDS"),
      timeoutMs: integerField(fields, "timeout_ms", "SCENARIO_ORACLE_QUEUE_FIELDS"),
    };
  });
  assertFlow(
    Math.max(...records.map((item) => item.printTimeMs)) - Math.min(...records.map((item) => item.printTimeMs)) <= 1000,
    "SCENARIO_ORACLE_QUEUE_GROUP_SPLIT",
    "Queue-history records do not belong to the same one-second output group",
  );
  const ordinalOrder = ["first", "second", "third", "fourth", "fifth"];
  records.sort((left, right) => ordinalOrder.indexOf(left.fields.ordinal) - ordinalOrder.indexOf(right.fields.ordinal));
  assertFlow(
    canonicalJson(records.map((item) => item.fields.ordinal)) === canonicalJson(["first", "second"]),
    "SCENARIO_ORACLE_QUEUE_ORDINALS",
    "Queue-history group does not use the complete ordered ordinal sequence",
  );
  const targetRecord = records.at(-1);
  assertFlow(
    targetRecord.fields.service === target.service && targetRecord.fields.api === target.api
      && targetRecord.costUs + targetRecord.queueUs > targetRecord.timeoutMs * 1000
      && targetRecord.costUs < targetRecord.timeoutMs * 1000,
    "SCENARIO_ORACLE_QUEUE_TARGET_NOT_CONFIRMED",
    "Queue-history target does not satisfy both queue-timeout conditions",
  );
  const targetExecutionStartUs = targetRecord.endUs - targetRecord.costUs;
  const targetQueueStartUs = targetExecutionStartUs - targetRecord.queueUs;
  const contributors = records.slice(0, -1).filter((prior) => {
    const priorExecutionStartUs = prior.endUs - prior.costUs;
    const overlapUs = Math.min(prior.endUs, targetExecutionStartUs)
      - Math.max(priorExecutionStartUs, targetQueueStartUs);
    return overlapUs > 0;
  });
  assertFlow(contributors.length > 0, "SCENARIO_ORACLE_QUEUE_CONTRIBUTOR_MISSING", "Queue-history group has no overlapping prior contributor");
}

function validateReleaseScenarioSemantics({ graph, selected, generated, scenarioOracle, publicMethodsResult }) {
  assertFlow(publicMethodsResult.status === scenarioOracle.expected_status, "SCENARIO_ORACLE_PUBLIC_STATUS", "Public Methods status differs from the Release scenario oracle");
  validateFrozenGraphLines(graph, selected);
  const facts = Object.fromEntries(selected.driver.initial_user_fact_names.map((name, index) => [
    name,
    selected.driver.initial_user_fact_values[index],
  ]));
  const target = { service: facts.service, api: facts.api };
  const linkedTimeout = validateRequestTimeoutEvidence({
    graph,
    generated,
    timeoutExpectation: scenarioOracle.required_request_timeout,
    target,
  });
  validateClientSegments({ graph, generated, linkedTimeout, target });
  validateApiCompleteEvents({ graph, generated, target });
  validateQueueHistory({ graph, generated, target });
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
  const frozenScenario = validateScenarioAgainstFrozenSource({ scenario, inputs, caseRoot, files, publicMethodsResult, sourceJob });
  const methodsExpectation = buildMethodsExpectation({
    methods,
    inputs,
    gateOracle,
    scenarioOracle: scenarioOracleEntry.oracle,
  });
  validateReleaseScenarioSemantics({
    graph: frozenScenario.graph,
    selected: frozenScenario.selected,
    generated: methodsExpectation.generated,
    scenarioOracle: scenarioOracleEntry.oracle,
    publicMethodsResult,
  });
  const summary = validateMethodsV2ExecutionRecords({
    files,
    expected: {
      source_job_id: sourceJob.job_id,
      reviewer_job_id: reviewerJob.job_id,
      case_id: sourceJob.case_id,
      skill_ref: sourceJob.skill_ref,
      ...methodsExpectation.expected,
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
