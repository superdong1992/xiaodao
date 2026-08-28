import { canonicalJson, sha256Bytes } from "./util.mjs";

const SHA256 = /^[a-f0-9]{64}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const METHOD_ID = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const SOURCE_ID = /^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*$/;
const SOURCE_REF = /^source-[a-f0-9]{64}$/;
const HIT_REF = /^hit-[a-f0-9]{64}$/;
const EVENT_REF = /^event-[a-f0-9]{64}$/;
const GRAPH_REF = /^graph-[a-f0-9]{64}$/;
const EVALUATION_REF = /^eval-[a-f0-9]{64}$/;
const PLAN_REF = /^plan-[a-f0-9]{64}$/;
const LIMITATIONS_REF = /^limitations-[a-f0-9]{64}$/;
const STATE_REF = /^state-[a-f0-9]{64}$/;
const DIAGNOSTIC_ID = /^diag-[a-f0-9]{64}$/;
const RESULT_REF = /^result-[a-f0-9]{64}$/;
const VERDICTS = new Set(["CONFIRMED", "REJECTED", "UNKNOWN"]);

const GRAPH_FIELDS = Object.freeze([
  "events", "graph_ref", "hits", "limitations", "loaded_method_ids", "skill_sha256", "sources",
]);
const SOURCE_FIELDS = Object.freeze(["content_sha256", "relative_path", "source_id", "source_ref"]);
const HIT_FIELDS = Object.freeze([
  "hit_ref", "line", "line_number", "marker", "marker_index", "method_id",
  "method_priority", "source_id", "source_ref",
]);
const EVENT_FIELDS = Object.freeze([
  "event_ref", "evidence_hit_refs", "identity_tokens", "method_id", "method_priority",
]);
const PLAN_FIELDS = Object.freeze(["evaluations", "evidence_graph_ref", "plan_ref", "skill_sha256"]);
const PLAN_ITEM_FIELDS = Object.freeze([
  "evaluation_ref", "evidence_event_refs", "evidence_hit_refs", "method_id", "method_priority",
]);
const LIMITATIONS_FIELDS = Object.freeze([
  "case_id", "evidence_graph_ref", "limitations", "plan_ref", "record_ref", "schema_version", "source_job_id",
]);
const STATE_FIELDS = Object.freeze([
  "case_id", "consensus", "current_role", "diagnostic_evaluation_ref", "diagnostic_id",
  "evaluation_id", "evaluation_refs", "plan_ref", "reason_code", "reasons",
  "reviewer_evaluation", "reviewer_protocol_failures", "source_job_id", "specialist_evaluation",
  "specialist_protocol_failures", "state_ref", "status",
]);
const ROLE_FIELDS = Object.freeze(["evaluations", "plan_ref", "repair_used", "role"]);
const ROLE_ITEM_FIELDS = Object.freeze(["evaluation_ref", "reason", "verdict"]);
const CONSENSUS_FIELDS = Object.freeze([
  "confirmed_evaluation_refs", "confirmed_method_ids", "plan_ref", "status",
]);
const PUBLIC_RESULT_FIELDS = Object.freeze([
  "case_id", "confirmed_evaluation_refs", "confirmed_event_refs", "confirmed_hit_refs",
  "confirmed_method_ids", "diagnostic_evaluation_ref", "diagnostic_id", "evaluation_id",
  "evidence_graph_ref", "limitations", "plan_ref", "reason_code", "reasons", "result_ref",
  "schema_version", "source_job_id", "status",
]);

export const METHODS_V2_CAPTURED_FILES = Object.freeze({
  source_job: "methods-source-job.json",
  reviewer_job: "methods-reviewer-job.json",
  evidence_graph: "methods-evidence-graph-v2.json",
  evaluation_plan: "methods-evaluation-plan-v2.json",
  limitations: "methods-limitations-v2.json",
  source_state: "methods-source-state-v2.json",
  source_outcome: "methods-source-outcome-v2.json",
  terminal_state: "methods-terminal-state-v2.json",
  reviewer_outcome: "methods-reviewer-outcome-v2.json",
});

export class MethodsOracleError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "MethodsOracleError";
    this.code = code;
  }
}

function fail(code, message) {
  throw new MethodsOracleError(code, message);
}

function requireOracle(condition, code, message) {
  if (!condition) fail(code, message);
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function exactKeys(value, expected, code, label) {
  requireOracle(
    isPlainObject(value)
      && canonicalJson(Object.keys(value).sort()) === canonicalJson([...expected].sort()),
    code,
    `${label} fields are invalid`,
  );
}

function uniqueStrings(value, { nonempty = false, pattern = null } = {}) {
  return Array.isArray(value)
    && (!nonempty || value.length > 0)
    && value.every((item) => typeof item === "string" && item.length > 0 && (!pattern || pattern.test(item)))
    && value.length === new Set(value).size;
}

function bytes(value, label) {
  if (Buffer.isBuffer(value)) return value;
  if (value instanceof Uint8Array) return Buffer.from(value);
  fail("METHODS_V2_ORACLE_BYTES_INVALID", `${label} must be bytes`);
}

function parseObject(value, label) {
  const payload = bytes(value, label);
  let parsed;
  try {
    parsed = JSON.parse(payload.toString("utf8"));
  } catch {
    fail("METHODS_V2_ORACLE_JSON_INVALID", `${label} is not valid JSON`);
  }
  requireOracle(isPlainObject(parsed), "METHODS_V2_ORACLE_JSON_INVALID", `${label} must be a JSON object`);
  requireOracle(
    payload.equals(Buffer.from(canonicalJson(parsed), "utf8")),
    "METHODS_V2_ORACLE_JSON_NON_CANONICAL",
    `${label} must use the canonical execution-record JSON encoding`,
  );
  return { payload, parsed };
}

function prefixedRef(prefix, kind, value) {
  return `${prefix}-${sha256Bytes(canonicalJson({ kind, ...value }))}`;
}

function compareBusiness(left, right) {
  for (let index = 0; index < left.length; index += 1) {
    if (left[index] < right[index]) return -1;
    if (left[index] > right[index]) return 1;
  }
  return 0;
}

function expectedMethodMap(expected) {
  requireOracle(isPlainObject(expected), "METHODS_V2_EXPECTATION_INVALID", "Evidence V2 expectation must be an object");
  for (const field of ["source_job_id", "reviewer_job_id", "case_id"]) {
    requireOracle(UUID.test(expected[field] ?? ""), "METHODS_V2_EXPECTATION_INVALID", `Expected ${field} is invalid`);
  }
  exactKeys(expected.skill_ref, ["content_hash", "id", "version"], "METHODS_V2_EXPECTATION_INVALID", "Expected Skill ref");
  requireOracle(
    typeof expected.skill_ref.id === "string" && expected.skill_ref.id.length > 0
      && typeof expected.skill_ref.version === "string" && expected.skill_ref.version.length > 0
      && SHA256.test(expected.skill_ref.content_hash ?? ""),
    "METHODS_V2_EXPECTATION_INVALID",
    "Expected Skill ref is invalid",
  );
  requireOracle(uniqueStrings(expected.source_ids, { nonempty: true, pattern: SOURCE_ID }), "METHODS_V2_EXPECTATION_INVALID", "Expected source IDs are invalid");
  requireOracle(Array.isArray(expected.method_cards) && expected.method_cards.length > 0, "METHODS_V2_EXPECTATION_INVALID", "Expected method cards are missing");
  const methods = new Map();
  for (const card of expected.method_cards) {
    exactKeys(card, ["evidence_markers", "id", "priority"], "METHODS_V2_EXPECTATION_INVALID", "Expected method card");
    requireOracle(
      METHOD_ID.test(card.id ?? "") && Number.isSafeInteger(card.priority) && card.priority > 0
        && uniqueStrings(card.evidence_markers, { nonempty: true }) && !methods.has(card.id),
      "METHODS_V2_EXPECTATION_INVALID",
      "Expected method card is invalid",
    );
    methods.set(card.id, card);
  }
  const ordered = [...methods.values()].sort((left, right) => compareBusiness(
    [left.priority, left.id],
    [right.priority, right.id],
  ));
  requireOracle(
    canonicalJson(expected.method_cards) === canonicalJson(ordered),
    "METHODS_V2_EXPECTATION_INVALID",
    "Expected method cards must use priority and method-id order",
  );
  requireOracle(
    uniqueStrings(expected.loaded_method_ids, { nonempty: true, pattern: METHOD_ID })
      && expected.loaded_method_ids.every((methodId) => methods.has(methodId)),
    "METHODS_V2_EXPECTATION_INVALID",
    "Expected loaded method IDs are invalid",
  );
  requireOracle(
    uniqueStrings(expected.confirmed_method_ids, { nonempty: true, pattern: METHOD_ID })
      && expected.confirmed_method_ids.every((methodId) => expected.loaded_method_ids.includes(methodId)),
    "METHODS_V2_EXPECTATION_INVALID",
    "Expected confirmed method IDs are invalid",
  );
  requireOracle(Array.isArray(expected.required_evidence_identities), "METHODS_V2_EXPECTATION_INVALID", "Expected evidence identities are invalid");
  for (const identity of expected.required_evidence_identities) {
    exactKeys(identity, ["identity_tokens", "marker", "method_id"], "METHODS_V2_EXPECTATION_INVALID", "Expected evidence identity");
    requireOracle(
      methods.has(identity.method_id) && typeof identity.marker === "string" && identity.marker.length > 0
        && uniqueStrings(identity.identity_tokens, { nonempty: true }),
      "METHODS_V2_EXPECTATION_INVALID",
      "Expected evidence identity is invalid",
    );
  }
  return methods;
}

function validateEvidenceGraph(graph, expected, methods) {
  exactKeys(graph, GRAPH_FIELDS, "METHODS_V2_GRAPH_FIELDS_INVALID", "Evidence Graph");
  requireOracle(GRAPH_REF.test(graph.graph_ref ?? "") && graph.skill_sha256 === expected.skill_ref.content_hash, "METHODS_V2_GRAPH_SKILL_MISMATCH", "Evidence Graph does not bind the pinned Skill");
  requireOracle(uniqueStrings(graph.limitations), "METHODS_V2_GRAPH_LIMITATIONS_INVALID", "Evidence Graph limitations are invalid");

  requireOracle(Array.isArray(graph.sources), "METHODS_V2_GRAPH_SOURCES_INVALID", "Evidence Graph sources are invalid");
  const sources = new Map();
  for (const source of graph.sources) {
    exactKeys(source, SOURCE_FIELDS, "METHODS_V2_GRAPH_SOURCE_FIELDS_INVALID", "Evidence source");
    const expectedRef = prefixedRef("source", "method-evidence-source-v2", {
      source_id: source.source_id,
      relative_path: source.relative_path,
      content_sha256: source.content_sha256,
    });
    requireOracle(
      SOURCE_ID.test(source.source_id ?? "") && typeof source.relative_path === "string" && source.relative_path.length > 0
        && !source.relative_path.includes("\\") && !source.relative_path.startsWith("/")
        && SHA256.test(source.content_sha256 ?? "") && SOURCE_REF.test(source.source_ref ?? "")
        && source.source_ref === expectedRef && !sources.has(source.source_ref),
      "METHODS_V2_GRAPH_SOURCE_INVALID",
      "Evidence source identity is invalid",
    );
    sources.set(source.source_ref, source);
  }
  requireOracle(
    canonicalJson(graph.sources.map((item) => item.source_id)) === canonicalJson([...expected.source_ids].sort()),
    "METHODS_V2_GRAPH_SOURCE_COVERAGE",
    "Evidence Graph source coverage differs from the frozen scenario",
  );

  requireOracle(Array.isArray(graph.hits), "METHODS_V2_GRAPH_HITS_INVALID", "Evidence Graph hits are invalid");
  const hits = new Map();
  for (const hit of graph.hits) {
    exactKeys(hit, HIT_FIELDS, "METHODS_V2_GRAPH_HIT_FIELDS_INVALID", "Evidence hit");
    const method = methods.get(hit.method_id);
    const source = sources.get(hit.source_ref);
    const expectedRef = prefixedRef("hit", "method-evidence-hit-v2", {
      method_id: hit.method_id,
      method_priority: hit.method_priority,
      marker_index: hit.marker_index,
      source_ref: hit.source_ref,
      source_id: hit.source_id,
      line_number: hit.line_number,
      marker: hit.marker,
      line: hit.line,
    });
    requireOracle(
      method && source && hit.source_id === source.source_id
        && hit.method_priority === method.priority
        && Number.isSafeInteger(hit.marker_index) && hit.marker_index > 0
        && hit.marker === method.evidence_markers[hit.marker_index - 1]
        && Number.isSafeInteger(hit.line_number) && hit.line_number > 0
        && typeof hit.line === "string" && hit.line.length > 0
        && hit.line.toLowerCase().includes(hit.marker.toLowerCase())
        && HIT_REF.test(hit.hit_ref ?? "") && hit.hit_ref === expectedRef && !hits.has(hit.hit_ref),
      "METHODS_V2_GRAPH_HIT_INVALID",
      "Evidence hit is not method-qualified or does not bind its original line",
    );
    hits.set(hit.hit_ref, hit);
  }
  const sortedHits = [...graph.hits].sort((left, right) => compareBusiness(
    [left.method_priority, left.method_id, left.marker_index, left.source_id, left.line_number],
    [right.method_priority, right.method_id, right.marker_index, right.source_id, right.line_number],
  ));
  requireOracle(canonicalJson(graph.hits) === canonicalJson(sortedHits), "METHODS_V2_GRAPH_HIT_ORDER", "Evidence hits are not in deterministic business order");

  for (const observed of graph.hits) {
    for (const method of methods.values()) {
      method.evidence_markers.forEach((marker, markerIndex) => {
        if (!observed.line.toLowerCase().includes(marker.toLowerCase())) return;
        const matching = graph.hits.filter((candidate) => (
          candidate.source_ref === observed.source_ref
            && candidate.line_number === observed.line_number
            && candidate.method_id === method.id
            && candidate.marker_index === markerIndex + 1
            && candidate.marker === marker
        ));
        requireOracle(matching.length === 1, "METHODS_V2_GRAPH_METHOD_QUALIFICATION", "A matching literal was not retained once for every owning method");
      });
    }
  }

  requireOracle(Array.isArray(graph.events), "METHODS_V2_GRAPH_EVENTS_INVALID", "Evidence Graph events are invalid");
  const events = new Map();
  const partition = [];
  const keyed = new Set();
  for (const event of graph.events) {
    exactKeys(event, EVENT_FIELDS, "METHODS_V2_GRAPH_EVENT_FIELDS_INVALID", "Evidence event");
    requireOracle(
      methods.has(event.method_id) && Number.isSafeInteger(event.method_priority) && event.method_priority === methods.get(event.method_id).priority
        && uniqueStrings(event.identity_tokens) && uniqueStrings(event.evidence_hit_refs, { nonempty: true, pattern: HIT_REF })
        && EVENT_REF.test(event.event_ref ?? "") && !events.has(event.event_ref),
      "METHODS_V2_GRAPH_EVENT_INVALID",
      "Evidence event identity is invalid",
    );
    const identityNames = event.identity_tokens.map((item) => item.split("=", 1)[0]);
    requireOracle(identityNames.length === new Set(identityNames).size, "METHODS_V2_GRAPH_EVENT_IDENTITY_INVALID", "Evidence event repeats an identity field");
    const eventHits = event.evidence_hit_refs.map((ref) => hits.get(ref));
    requireOracle(eventHits.every((item) => item && item.method_id === event.method_id && item.method_priority === event.method_priority), "METHODS_V2_GRAPH_EVENT_METHOD_MISMATCH", "Evidence event crosses method boundaries");
    if (event.identity_tokens.length === 0) {
      requireOracle(event.evidence_hit_refs.length === 1, "METHODS_V2_GRAPH_UNKEYED_EVENT_MERGED", "Unkeyed hits must remain independent events");
    } else {
      const key = `${event.method_id}\0${canonicalJson(event.identity_tokens)}`;
      requireOracle(!keyed.has(key), "METHODS_V2_GRAPH_EVENT_DUPLICATE", "One method identity produced multiple events");
      keyed.add(key);
    }
    const expectedRef = prefixedRef("event", "method-evidence-event-v2", {
      method_id: event.method_id,
      method_priority: event.method_priority,
      identity_tokens: event.identity_tokens,
      evidence_hit_refs: event.evidence_hit_refs,
    });
    requireOracle(event.event_ref === expectedRef, "METHODS_V2_GRAPH_EVENT_REF_MISMATCH", "Evidence event ref does not match its content");
    events.set(event.event_ref, event);
    partition.push(...event.evidence_hit_refs);
  }
  requireOracle(
    canonicalJson([...partition].sort()) === canonicalJson([...hits.keys()].sort()),
    "METHODS_V2_GRAPH_EVENT_PARTITION",
    "Evidence events do not exactly partition all method-qualified hits",
  );
  const hitOrder = new Map(graph.hits.map((item, index) => [item.hit_ref, index]));
  const sortedEvents = [...graph.events].sort((left, right) => compareBusiness(
    [left.method_priority, left.method_id, Math.min(...left.evidence_hit_refs.map((ref) => hitOrder.get(ref)))],
    [right.method_priority, right.method_id, Math.min(...right.evidence_hit_refs.map((ref) => hitOrder.get(ref)))],
  ));
  requireOracle(canonicalJson(graph.events) === canonicalJson(sortedEvents), "METHODS_V2_GRAPH_EVENT_ORDER", "Evidence events are not in deterministic business order");

  const loaded = [...new Set(graph.hits.map((item) => item.method_id))]
    .sort((left, right) => compareBusiness([methods.get(left).priority, left], [methods.get(right).priority, right]));
  requireOracle(
    uniqueStrings(graph.loaded_method_ids, { nonempty: true, pattern: METHOD_ID })
      && canonicalJson(graph.loaded_method_ids) === canonicalJson(loaded)
      && canonicalJson(graph.loaded_method_ids) === canonicalJson(expected.loaded_method_ids),
    "METHODS_V2_GRAPH_METHOD_COVERAGE",
    "Evidence Graph loaded methods differ from the production scenario",
  );
  for (const identity of expected.required_evidence_identities) {
    const matches = graph.events.filter((event) => event.method_id === identity.method_id
      && identity.identity_tokens.every((token) => event.identity_tokens.includes(token))
      && event.evidence_hit_refs.some((ref) => hits.get(ref).marker.includes(identity.marker)));
    requireOracle(matches.length === 1, "METHODS_V2_GRAPH_REQUIRED_IDENTITY", "A required method-qualified evidence identity is absent or ambiguous");
  }
  const graphRef = prefixedRef("graph", "method-evidence-graph-v2", {
    skill_sha256: graph.skill_sha256,
    source_refs: graph.sources.map((item) => item.source_ref),
    hit_refs: graph.hits.map((item) => item.hit_ref),
    event_refs: graph.events.map((item) => item.event_ref),
    loaded_method_ids: graph.loaded_method_ids,
    limitations: graph.limitations,
  });
  requireOracle(graph.graph_ref === graphRef, "METHODS_V2_GRAPH_REF_MISMATCH", "Evidence Graph ref does not match the complete graph");
  return { hits, events };
}

function validateEvaluationPlan(plan, graph, indexes) {
  exactKeys(plan, PLAN_FIELDS, "METHODS_V2_PLAN_FIELDS_INVALID", "Evaluation Plan");
  requireOracle(
    PLAN_REF.test(plan.plan_ref ?? "") && plan.skill_sha256 === graph.skill_sha256
      && plan.evidence_graph_ref === graph.graph_ref && Array.isArray(plan.evaluations),
    "METHODS_V2_PLAN_IDENTITY_MISMATCH",
    "Evaluation Plan does not bind the Evidence Graph and Skill",
  );
  const evaluations = new Map();
  for (const item of plan.evaluations) {
    exactKeys(item, PLAN_ITEM_FIELDS, "METHODS_V2_PLAN_ITEM_FIELDS_INVALID", "Evaluation Plan item");
    const eventRefs = graph.events.filter((event) => event.method_id === item.method_id).map((event) => event.event_ref);
    const hitRefs = graph.hits.filter((hit) => hit.method_id === item.method_id).map((hit) => hit.hit_ref);
    const expectedRef = prefixedRef("eval", "method-evaluation-v2", {
      method_id: item.method_id,
      method_priority: item.method_priority,
      evidence_event_refs: item.evidence_event_refs,
      evidence_hit_refs: item.evidence_hit_refs,
    });
    requireOracle(
      EVALUATION_REF.test(item.evaluation_ref ?? "") && item.evaluation_ref === expectedRef
        && !evaluations.has(item.evaluation_ref)
        && canonicalJson(item.evidence_event_refs) === canonicalJson(eventRefs)
        && canonicalJson(item.evidence_hit_refs) === canonicalJson(hitRefs)
        && item.evidence_event_refs.every((ref) => indexes.events.has(ref))
        && item.evidence_hit_refs.every((ref) => indexes.hits.has(ref)),
      "METHODS_V2_PLAN_ITEM_INVALID",
      "Evaluation Plan item does not exactly cover its method evidence",
    );
    evaluations.set(item.evaluation_ref, item);
  }
  requireOracle(
    canonicalJson(plan.evaluations.map((item) => item.method_id)) === canonicalJson(graph.loaded_method_ids)
      && canonicalJson(plan.evaluations.flatMap((item) => item.evidence_event_refs)) === canonicalJson(graph.events.map((item) => item.event_ref))
      && canonicalJson(plan.evaluations.flatMap((item) => item.evidence_hit_refs)) === canonicalJson(graph.hits.map((item) => item.hit_ref)),
    "METHODS_V2_PLAN_COVERAGE",
    "Evaluation Plan does not exactly cover the complete Evidence Graph",
  );
  const planRef = prefixedRef("plan", "method-evaluation-plan-v2", {
    skill_sha256: plan.skill_sha256,
    evidence_graph_ref: plan.evidence_graph_ref,
    evaluation_refs: plan.evaluations.map((item) => item.evaluation_ref),
  });
  requireOracle(plan.plan_ref === planRef, "METHODS_V2_PLAN_REF_MISMATCH", "Evaluation Plan ref does not match the complete plan");
  return evaluations;
}

function validateLimitations(record, graph, plan, expected) {
  exactKeys(record, LIMITATIONS_FIELDS, "METHODS_V2_LIMITATIONS_FIELDS_INVALID", "Limitations record");
  const recordRef = prefixedRef("limitations", "method-limitations-record-v2", {
    case_id: record.case_id,
    source_job_id: record.source_job_id,
    evidence_graph_ref: record.evidence_graph_ref,
    plan_ref: record.plan_ref,
    limitations: record.limitations,
  });
  requireOracle(
    record.schema_version === 2 && LIMITATIONS_REF.test(record.record_ref ?? "") && record.record_ref === recordRef
      && record.case_id === expected.case_id && record.source_job_id === expected.source_job_id
      && record.evidence_graph_ref === graph.graph_ref && record.plan_ref === plan.plan_ref
      && uniqueStrings(record.limitations) && canonicalJson(record.limitations) === canonicalJson(graph.limitations),
    "METHODS_V2_LIMITATIONS_MISMATCH",
    "Limitations do not remain identical from Graph to the server record",
  );
}

function validateRoleEvaluation(value, role, plan) {
  exactKeys(value, ROLE_FIELDS, "METHODS_V2_ROLE_FIELDS_INVALID", `${role} evaluation`);
  requireOracle(value.role === role && value.plan_ref === plan.plan_ref && typeof value.repair_used === "boolean" && Array.isArray(value.evaluations), "METHODS_V2_ROLE_IDENTITY_MISMATCH", `${role} evaluation identity is invalid`);
  const expectedRefs = plan.evaluations.map((item) => item.evaluation_ref);
  const actualRefs = [];
  for (const item of value.evaluations) {
    exactKeys(item, ROLE_ITEM_FIELDS, "METHODS_V2_ROLE_ITEM_FIELDS_INVALID", `${role} output item`);
    requireOracle(
      EVALUATION_REF.test(item.evaluation_ref ?? "") && VERDICTS.has(item.verdict)
        && typeof item.reason === "string" && item.reason.trim().length > 0,
      "METHODS_V2_ROLE_ITEM_INVALID",
      `${role} output must contain only evaluation_ref, verdict, and reason`,
    );
    actualRefs.push(item.evaluation_ref);
  }
  requireOracle(canonicalJson(actualRefs) === canonicalJson(expectedRefs), "METHODS_V2_ROLE_COVERAGE", `${role} output does not exactly cover the Evaluation Plan`);
  return value;
}

function stateRef(state) {
  return prefixedRef("state", "method-state-v2", {
    case_id: state.case_id,
    source_job_id: state.source_job_id,
    evaluation_id: state.evaluation_id,
    plan_ref: state.plan_ref,
    evaluation_refs: state.evaluation_refs,
    status: state.status,
    current_role: state.current_role,
    specialist_protocol_failures: state.specialist_protocol_failures,
    reviewer_protocol_failures: state.reviewer_protocol_failures,
    specialist_evaluation: state.specialist_evaluation,
    reviewer_evaluation: state.reviewer_evaluation,
    consensus: state.consensus,
    reason_code: state.reason_code,
    diagnostic_id: state.diagnostic_id,
    diagnostic_evaluation_ref: state.diagnostic_evaluation_ref,
    reasons: state.reasons,
  });
}

function validateStateBase(state, plan, expected) {
  exactKeys(state, STATE_FIELDS, "METHODS_V2_STATE_FIELDS_INVALID", "Methods state");
  requireOracle(
    STATE_REF.test(state.state_ref ?? "") && state.state_ref === stateRef(state)
      && state.case_id === expected.case_id && state.source_job_id === expected.source_job_id
      && UUID.test(state.evaluation_id ?? "") && state.plan_ref === plan.plan_ref
      && canonicalJson(state.evaluation_refs) === canonicalJson(plan.evaluations.map((item) => item.evaluation_ref))
      && Number.isSafeInteger(state.specialist_protocol_failures) && state.specialist_protocol_failures >= 0 && state.specialist_protocol_failures <= 2
      && Number.isSafeInteger(state.reviewer_protocol_failures) && state.reviewer_protocol_failures >= 0 && state.reviewer_protocol_failures <= 2
      && uniqueStrings(state.reasons),
    "METHODS_V2_STATE_IDENTITY_MISMATCH",
    "Methods state does not bind its Case, source Job, or complete Plan",
  );
}

function validateJobAndOutcomes({ sourceJob, reviewerJob, sourceOutcome, reviewerOutcome, sourceState, terminalState, graph, plan, expected }) {
  requireOracle(
    sourceJob.job_id === expected.source_job_id && sourceJob.case_id === expected.case_id
      && sourceJob.job_type === "DIAGNOSE" && sourceJob.diagnosis_mode === "SPECIALIZED"
      && canonicalJson(sourceJob.skill_ref) === canonicalJson(expected.skill_ref),
    "METHODS_V2_SOURCE_JOB_MISMATCH",
    "Source Job does not match the production Methods identity",
  );
  const target = sourceOutcome.methods_review_target;
  requireOracle(
    sourceOutcome.job_id === expected.source_job_id && sourceOutcome.case_id === expected.case_id
      && sourceOutcome.job_type === "DIAGNOSE" && sourceOutcome.result_type === "COMPLETED"
      && sourceOutcome.payload === null && sourceOutcome.error === null && sourceOutcome.decision_audit === null
      && Array.isArray(sourceOutcome.consumed_evidence_refs) && sourceOutcome.consumed_evidence_refs.length === 0
      && Array.isArray(sourceOutcome.proposed_evidence) && sourceOutcome.proposed_evidence.length === 0
      && Array.isArray(sourceOutcome.proposed_artifacts) && sourceOutcome.proposed_artifacts.length === 0
      && isPlainObject(target) && target.schema_version === 2 && target.source_job_id === expected.source_job_id
      && target.graph_ref === graph.graph_ref && target.plan_ref === plan.plan_ref
      && target.evaluation_id === sourceState.evaluation_id
      && canonicalJson(target.skill_ref) === canonicalJson(expected.skill_ref),
    "METHODS_V2_SOURCE_OUTCOME_MISMATCH",
    "Source Outcome is not the Candidate-free Reviewer handoff",
  );
  requireOracle(
    reviewerJob.job_id === expected.reviewer_job_id && reviewerJob.case_id === expected.case_id
      && reviewerJob.job_type === "REVIEW" && canonicalJson(reviewerJob.skill_ref) === canonicalJson(expected.skill_ref)
      && canonicalJson(reviewerJob.methods_review_target) === canonicalJson(target)
      && reviewerJob.review_target === null
      && reviewerJob.context_snapshot?.candidate_conclusion === null
      && !canonicalJson(reviewerJob).includes("specialist_evaluation"),
    "METHODS_V2_REVIEWER_JOB_MISMATCH",
    "Reviewer Job does not bind the source Graph, Plan, and Skill",
  );

  validateStateBase(sourceState, plan, expected);
  const specialist = validateRoleEvaluation(sourceState.specialist_evaluation, "SPECIALIST", plan);
  requireOracle(
    sourceState.status === "REVIEWER_PENDING" && sourceState.current_role === "REVIEWER"
      && sourceState.reviewer_evaluation === null && sourceState.consensus === null
      && sourceState.reason_code === null && sourceState.diagnostic_id === null
      && sourceState.diagnostic_evaluation_ref === null && sourceState.reasons.length === 0
      && sourceState.specialist_protocol_failures === (specialist.repair_used ? 1 : 0)
      && sourceState.reviewer_protocol_failures === 0,
    "METHODS_V2_SOURCE_STATE_INVALID",
    "Source Methods state is not the exact Reviewer-pending handoff",
  );

  validateStateBase(terminalState, plan, expected);
  const terminalSpecialist = validateRoleEvaluation(terminalState.specialist_evaluation, "SPECIALIST", plan);
  const reviewer = validateRoleEvaluation(terminalState.reviewer_evaluation, "REVIEWER", plan);
  requireOracle(canonicalJson(terminalSpecialist) === canonicalJson(specialist), "METHODS_V2_SPECIALIST_STATE_DRIFT", "Reviewer state changed the Specialist evaluation");
  exactKeys(terminalState.consensus, CONSENSUS_FIELDS, "METHODS_V2_CONSENSUS_FIELDS_INVALID", "Methods consensus");
  const aligned = terminalSpecialist.evaluations.every((item, index) => (
    item.evaluation_ref === reviewer.evaluations[index].evaluation_ref
      && item.verdict === reviewer.evaluations[index].verdict
  ));
  const confirmed = terminalSpecialist.evaluations.filter((item) => item.verdict === "CONFIRMED");
  const confirmedRefs = confirmed.map((item) => item.evaluation_ref);
  const byRef = new Map(plan.evaluations.map((item) => [item.evaluation_ref, item]));
  const confirmedMethods = confirmedRefs.map((ref) => byRef.get(ref).method_id);
  requireOracle(
    terminalState.status === "RESOLVED" && terminalState.current_role === null
      && terminalState.reason_code === null && terminalState.diagnostic_evaluation_ref === null
      && terminalState.reasons.length === 0 && DIAGNOSTIC_ID.test(terminalState.diagnostic_id ?? "")
      && terminalState.specialist_protocol_failures === (terminalSpecialist.repair_used ? 1 : 0)
      && terminalState.reviewer_protocol_failures === (reviewer.repair_used ? 1 : 0)
      && aligned && terminalSpecialist.evaluations.every((item) => item.verdict !== "UNKNOWN")
      && confirmed.length > 0 && terminalState.consensus.status === "RESOLVED"
      && terminalState.consensus.plan_ref === plan.plan_ref
      && canonicalJson(terminalState.consensus.confirmed_evaluation_refs) === canonicalJson(confirmedRefs)
      && canonicalJson(terminalState.consensus.confirmed_method_ids) === canonicalJson(confirmedMethods)
      && canonicalJson(confirmedMethods) === canonicalJson(expected.confirmed_method_ids),
    "METHODS_V2_CONSENSUS_INVALID",
    "Specialist and blind Reviewer do not form the required complete resolved consensus",
  );
  const diagnostic = prefixedRef("diag", "method-diagnostic-v2", {
    case_id: terminalState.case_id,
    source_job_id: terminalState.source_job_id,
    evaluation_id: terminalState.evaluation_id,
    plan_ref: terminalState.plan_ref,
    status: terminalState.status,
    reason_code: terminalState.reason_code,
    evaluation_ref: terminalState.diagnostic_evaluation_ref,
  });
  requireOracle(terminalState.diagnostic_id === diagnostic, "METHODS_V2_DIAGNOSTIC_ID_MISMATCH", "Terminal diagnostic ID is not stable");

  const reviewerResult = reviewerOutcome.methods_reviewer_result;
  const projection = reviewerOutcome.methods_terminal_projection;
  requireOracle(
    reviewerOutcome.job_id === expected.reviewer_job_id && reviewerOutcome.case_id === expected.case_id
      && reviewerOutcome.job_type === "REVIEW" && reviewerOutcome.result_type === "COMPLETED"
      && reviewerOutcome.payload === null && reviewerOutcome.error === null && reviewerOutcome.decision_audit === null
      && Array.isArray(reviewerOutcome.consumed_evidence_refs) && reviewerOutcome.consumed_evidence_refs.length === 0
      && Array.isArray(reviewerOutcome.proposed_evidence) && reviewerOutcome.proposed_evidence.length === 0
      && Array.isArray(reviewerOutcome.proposed_artifacts) && reviewerOutcome.proposed_artifacts.length === 0
      && isPlainObject(reviewerResult) && reviewerResult.schema_version === 2
      && reviewerResult.role === "REVIEWER" && reviewerResult.review_job_id === expected.reviewer_job_id
      && canonicalJson(reviewerResult.target) === canonicalJson(target)
      && canonicalJson(reviewerResult.evaluations) === canonicalJson(reviewer.evaluations)
      && reviewerResult.repair_used === reviewer.repair_used,
    "METHODS_V2_REVIEWER_OUTCOME_MISMATCH",
    "Reviewer Outcome does not contain the normalized blind review",
  );
  return { specialist, reviewer, confirmedRefs, confirmedMethods, projection };
}

function validatePublicProjection({ projection, publicMethodsResult, terminalState, graph, plan, limitations, expected, confirmedRefs, confirmedMethods }) {
  exactKeys(projection, PUBLIC_RESULT_FIELDS, "METHODS_V2_PUBLIC_FIELDS_INVALID", "Public Methods result");
  const confirmedPlan = confirmedRefs.map((ref) => plan.evaluations.find((item) => item.evaluation_ref === ref));
  const confirmedEventRefs = [...new Set(confirmedPlan.flatMap((item) => item.evidence_event_refs))];
  const confirmedHitRefs = [...new Set(confirmedPlan.flatMap((item) => item.evidence_hit_refs))];
  const resultEvaluations = confirmedPlan.map((item) => ({
    evaluation_ref: item.evaluation_ref,
    method_id: item.method_id,
    evidence_event_refs: item.evidence_event_refs,
    evidence_hit_refs: item.evidence_hit_refs,
    verdict: "CONFIRMED",
  }));
  const resultRef = prefixedRef("result", "method-terminal-result-v2", {
    case_id: expected.case_id,
    source_job_id: expected.source_job_id,
    terminal_job_id: expected.reviewer_job_id,
    evaluation_id: terminalState.evaluation_id,
    status: "RESOLVED",
    plan_ref: plan.plan_ref,
    evidence_graph_ref: graph.graph_ref,
    reason_code: null,
    diagnostic_id: terminalState.diagnostic_id,
    diagnostic_evaluation_ref: null,
    evaluations: resultEvaluations,
    confirmed_evaluation_refs: confirmedRefs,
    confirmed_method_ids: confirmedMethods,
    confirmed_event_refs: confirmedEventRefs,
    confirmed_hit_refs: confirmedHitRefs,
    limitations: limitations.limitations,
    reasons: [],
  });
  requireOracle(
    projection.schema_version === 2 && projection.case_id === expected.case_id
      && projection.source_job_id === expected.reviewer_job_id && projection.status === "RESOLVED"
      && RESULT_REF.test(projection.result_ref ?? "") && projection.result_ref === resultRef
      && projection.evaluation_id === terminalState.evaluation_id
      && projection.plan_ref === plan.plan_ref && projection.evidence_graph_ref === graph.graph_ref
      && projection.reason_code === null && projection.diagnostic_id === terminalState.diagnostic_id
      && projection.diagnostic_evaluation_ref === null
      && canonicalJson(projection.confirmed_evaluation_refs) === canonicalJson(confirmedRefs)
      && canonicalJson(projection.confirmed_method_ids) === canonicalJson(confirmedMethods)
      && canonicalJson(projection.confirmed_event_refs) === canonicalJson(confirmedEventRefs)
      && canonicalJson(projection.confirmed_hit_refs) === canonicalJson(confirmedHitRefs)
      && canonicalJson(projection.limitations) === canonicalJson(limitations.limitations)
      && canonicalJson(projection.reasons) === canonicalJson([])
      && canonicalJson(publicMethodsResult) === canonicalJson(projection),
    "METHODS_V2_PUBLIC_RESULT_MISMATCH",
    "Public methods_result is not the exact server-owned terminal projection",
  );
  return resultRef;
}

function validateInvocations(invocations, expected, roles) {
  requireOracle(Array.isArray(invocations), "METHODS_V2_INVOCATIONS_INVALID", "Service invocations are missing");
  const source = invocations.filter((item) => item.job_id === expected.source_job_id && item.job_type === "DIAGNOSE");
  const reviewer = invocations.filter((item) => item.job_id === expected.reviewer_job_id && item.job_type === "REVIEW");
  const expectedSourceCalls = roles.specialist.repair_used ? 2 : 1;
  const expectedReviewerCalls = roles.reviewer.repair_used ? 2 : 1;
  requireOracle(
    invocations.length === source.length + reviewer.length
      && source.length === expectedSourceCalls && reviewer.length === expectedReviewerCalls
      && invocations.length >= 2 && invocations.length <= 4
      && invocations.every((item) => typeof item.effective_model === "string" && item.effective_model.length > 0)
      && new Set(invocations.map((item) => item.effective_model)).size === 1,
    "METHODS_V2_INVOCATION_CARDINALITY",
    "Specialist and Reviewer calls do not match the one-repair-per-role contract",
  );
}

export function validateMethodsV2ExecutionRecords({ files, expected, invocations, publicMethodsResult }) {
  const methods = expectedMethodMap(expected);
  requireOracle(isPlainObject(files), "METHODS_V2_FILES_INVALID", "Evidence V2 captured files are missing");
  const documents = {};
  for (const key of Object.keys(METHODS_V2_CAPTURED_FILES)) {
    documents[key] = parseObject(files[key], METHODS_V2_CAPTURED_FILES[key]);
  }
  const graph = documents.evidence_graph.parsed;
  const plan = documents.evaluation_plan.parsed;
  const limitations = documents.limitations.parsed;
  const indexes = validateEvidenceGraph(graph, expected, methods);
  validateEvaluationPlan(plan, graph, indexes);
  validateLimitations(limitations, graph, plan, expected);
  const linked = validateJobAndOutcomes({
    sourceJob: documents.source_job.parsed,
    reviewerJob: documents.reviewer_job.parsed,
    sourceOutcome: documents.source_outcome.parsed,
    reviewerOutcome: documents.reviewer_outcome.parsed,
    sourceState: documents.source_state.parsed,
    terminalState: documents.terminal_state.parsed,
    graph,
    plan,
    expected,
  });
  const resultRef = validatePublicProjection({
    projection: linked.projection,
    publicMethodsResult,
    terminalState: documents.terminal_state.parsed,
    graph,
    plan,
    limitations,
    expected,
    confirmedRefs: linked.confirmedRefs,
    confirmedMethods: linked.confirmedMethods,
  });
  validateInvocations(invocations, expected, linked);
  return {
    schema_version: 2,
    status: "PASS",
    case_id: expected.case_id,
    source_job_id: expected.source_job_id,
    reviewer_job_id: expected.reviewer_job_id,
    graph_ref: graph.graph_ref,
    plan_ref: plan.plan_ref,
    evaluation_id: documents.terminal_state.parsed.evaluation_id,
    result_ref: resultRef,
    diagnostic_id: documents.terminal_state.parsed.diagnostic_id,
    confirmed_method_ids: linked.confirmedMethods,
    evaluation_count: plan.evaluations.length,
    evidence_event_count: graph.events.length,
    evidence_hit_count: graph.hits.length,
    specialist_repair_used: linked.specialist.repair_used,
    reviewer_repair_used: linked.reviewer.repair_used,
    service_model_calls: invocations.length,
    public_methods_result_sha256: sha256Bytes(canonicalJson(publicMethodsResult)),
    record_sha256: Object.fromEntries(Object.entries(documents).map(([key, document]) => [key, sha256Bytes(document.payload)])),
  };
}

export function validateMethodsV2RestartSnapshot({ caseView, artifacts, methodsSummary, restartedFiles }) {
  requireOracle(isPlainObject(caseView), "METHODS_V2_RESTART_CASE_INVALID", "Restart Case view is invalid");
  requireOracle(
    caseView.case_id === methodsSummary.case_id && caseView.status === "RESOLVED"
      && caseView.final_result === null && caseView.unresolved_result === null
      && caseView.generic_result === null && caseView.generic_result_v2 === null
      && sha256Bytes(canonicalJson(caseView.methods_result)) === methodsSummary.public_methods_result_sha256,
    "METHODS_V2_RESTART_CASE_MISMATCH",
    "Restart did not preserve the exact public Methods result",
  );
  requireOracle(Array.isArray(artifacts) && artifacts.length === 0 && Array.isArray(caseView.artifacts) && caseView.artifacts.length === 0, "METHODS_V2_RESTART_ARTIFACTS_PRESENT", "Evidence V2 terminal Cases must not publish legacy artifacts");
  requireOracle(isPlainObject(restartedFiles), "METHODS_V2_RESTART_RECORDS_INVALID", "Restart execution records are missing");
  for (const key of Object.keys(METHODS_V2_CAPTURED_FILES)) {
    const payload = bytes(restartedFiles[key], `restart-${METHODS_V2_CAPTURED_FILES[key]}`);
    requireOracle(sha256Bytes(payload) === methodsSummary.record_sha256[key], "METHODS_V2_RESTART_RECORD_DRIFT", `Restart changed ${METHODS_V2_CAPTURED_FILES[key]}`);
  }
  return true;
}
