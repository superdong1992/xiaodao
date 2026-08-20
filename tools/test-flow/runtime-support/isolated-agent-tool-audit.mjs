import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { canonicalJson, sha256Bytes } from "../lib/util.mjs";
import {
  buildGenerationBlueprintSubmissionDiagnostic,
  SKILL_GENERATION_RULE_IR,
  validGenerationBlueprintSubmission,
  validGenerationBlueprintSubmissionDiagnostic,
  validSkillGenerationCompilerReceipt,
  validSkillGenerationRuleIrDiagnostic,
} from "./skill-generation-rule-ir.mjs";

export const SKILL_GENERATION_TRACE_CODES = Object.freeze({
  EVENTS_INVALID: "SKILL_TRACE_EVENTS_INVALID",
  STREAM_ERROR: "SKILL_TRACE_STREAM_ERROR",
  INIT_INVALID: "SKILL_TRACE_INIT_INVALID",
  INIT_CWD_MISMATCH: "SKILL_TRACE_INIT_CWD_MISMATCH",
  PERMISSION_MODE_INVALID: "SKILL_TRACE_PERMISSION_MODE_INVALID",
  TOOL_INVENTORY_INVALID: "SKILL_TRACE_TOOL_INVENTORY_INVALID",
  RESULT_INVALID: "SKILL_TRACE_RESULT_INVALID",
  RESULT_NOT_SUCCESS: "SKILL_TRACE_RESULT_NOT_SUCCESS",
  TOOL_EVENT_INVALID: "SKILL_TRACE_TOOL_EVENT_INVALID",
  TOOL_NOT_ALLOWED: "SKILL_TRACE_TOOL_NOT_ALLOWED",
  TOOL_USE_ID_INVALID: "SKILL_TRACE_TOOL_USE_ID_INVALID",
  TOOL_RESULT_UNMATCHED: "SKILL_TRACE_TOOL_RESULT_UNMATCHED",
  TOOL_RESULT_DUPLICATE: "SKILL_TRACE_TOOL_RESULT_DUPLICATE",
  TOOL_RESULT_ERROR: "SKILL_TRACE_TOOL_RESULT_ERROR",
  TOOL_RESULT_MISSING: "SKILL_TRACE_TOOL_RESULT_MISSING",
  SKILL_INVOCATION_INVALID: "SKILL_TRACE_SKILL_INVOCATION_INVALID",
  SKILL_RESULT_INVALID: "SKILL_TRACE_SKILL_RESULT_INVALID",
  ROOT_INVALID: "SKILL_TRACE_ROOT_INVALID",
  SKILL_DOCUMENT_INVALID: "SKILL_TRACE_SKILL_DOCUMENT_INVALID",
  SKILL_LINK_INVALID: "SKILL_TRACE_SKILL_LINK_INVALID",
  REQUIRED_REFERENCE_INVALID: "SKILL_TRACE_REQUIRED_REFERENCE_INVALID",
  PATH_ABSOLUTE: "SKILL_TRACE_PATH_ABSOLUTE",
  PATH_TRAVERSAL: "SKILL_TRACE_PATH_TRAVERSAL",
  PATH_NOT_NORMALIZED: "SKILL_TRACE_PATH_NOT_NORMALIZED",
  PATH_MISSING: "SKILL_TRACE_PATH_MISSING",
  PATH_SYMLINK: "SKILL_TRACE_PATH_SYMLINK",
  PATH_HARDLINK: "SKILL_TRACE_PATH_HARDLINK",
  PATH_KIND_INVALID: "SKILL_TRACE_PATH_KIND_INVALID",
  READ_INPUT_INVALID: "SKILL_TRACE_READ_INPUT_INVALID",
  REQUIRED_READ_PARTIAL: "SKILL_TRACE_REQUIRED_READ_PARTIAL",
  REQUIRED_READ_ORDER_INVALID: "SKILL_TRACE_REQUIRED_READ_ORDER_INVALID",
  PHASE_SEQUENCE_INVALID: "SKILL_TRACE_PHASE_SEQUENCE_INVALID",
  READ_UNLINKED: "SKILL_TRACE_READ_UNLINKED",
  REQUIRED_READ_MISSING: "SKILL_TRACE_REQUIRED_READ_MISSING",
  WRITE_INPUT_INVALID: "SKILL_TRACE_WRITE_INPUT_INVALID",
  WRITE_COUNT_INVALID: "SKILL_TRACE_WRITE_COUNT_INVALID",
  WRITE_PATH_INVALID: "SKILL_TRACE_WRITE_PATH_INVALID",
  WRITE_CONTENT_MISMATCH: "SKILL_TRACE_WRITE_CONTENT_MISMATCH",
  WRITE_JSON_INVALID: "SKILL_TRACE_WRITE_JSON_INVALID",
  STRUCTURED_OUTPUT_COUNT_INVALID: "SKILL_TRACE_STRUCTURED_OUTPUT_COUNT_INVALID",
  STRUCTURED_OUTPUT_INPUT_INVALID: "SKILL_TRACE_STRUCTURED_OUTPUT_INPUT_INVALID",
  STRUCTURED_OUTPUT_SCHEMA_INVALID: "SKILL_TRACE_STRUCTURED_OUTPUT_SCHEMA_INVALID",
  STRUCTURED_OUTPUT_MISMATCH: "SKILL_TRACE_STRUCTURED_OUTPUT_MISMATCH",
  RULE_IR_INVALID: "SKILL_TRACE_RULE_IR_INVALID",
  INCOMPLETE_PREFIX: "SKILL_TRACE_INCOMPLETE_PREFIX",
  INCOMPLETE_PREFIX_SEQUENCE_INVALID: "SKILL_TRACE_INCOMPLETE_PREFIX_SEQUENCE_INVALID",
  INCOMPLETE_PREFIX_REJECTED: "SKILL_TRACE_INCOMPLETE_PREFIX_REJECTED",
});

export const ISOLATED_AGENT_STREAM_EVENT_TYPES = Object.freeze([
  "system",
  "assistant",
  "user",
  "result",
  "error",
  "rate_limit_event",
]);

export function validIsolatedAgentStreamEventType(value) {
  return typeof value === "string" && ISOLATED_AGENT_STREAM_EVENT_TYPES.includes(value);
}

const SKILL_GENERATION_INCOMPLETE_AUDIT_REJECTION_CODES = Object.freeze([
  SKILL_GENERATION_TRACE_CODES.EVENTS_INVALID,
  SKILL_GENERATION_TRACE_CODES.STREAM_ERROR,
  SKILL_GENERATION_TRACE_CODES.INIT_INVALID,
  SKILL_GENERATION_TRACE_CODES.INIT_CWD_MISMATCH,
  SKILL_GENERATION_TRACE_CODES.PERMISSION_MODE_INVALID,
  SKILL_GENERATION_TRACE_CODES.TOOL_INVENTORY_INVALID,
  SKILL_GENERATION_TRACE_CODES.RESULT_INVALID,
  SKILL_GENERATION_TRACE_CODES.TOOL_EVENT_INVALID,
  SKILL_GENERATION_TRACE_CODES.TOOL_NOT_ALLOWED,
  SKILL_GENERATION_TRACE_CODES.TOOL_USE_ID_INVALID,
  SKILL_GENERATION_TRACE_CODES.TOOL_RESULT_UNMATCHED,
  SKILL_GENERATION_TRACE_CODES.TOOL_RESULT_DUPLICATE,
  SKILL_GENERATION_TRACE_CODES.TOOL_RESULT_ERROR,
  SKILL_GENERATION_TRACE_CODES.TOOL_RESULT_MISSING,
  SKILL_GENERATION_TRACE_CODES.SKILL_INVOCATION_INVALID,
  SKILL_GENERATION_TRACE_CODES.SKILL_RESULT_INVALID,
  SKILL_GENERATION_TRACE_CODES.ROOT_INVALID,
  SKILL_GENERATION_TRACE_CODES.SKILL_DOCUMENT_INVALID,
  SKILL_GENERATION_TRACE_CODES.SKILL_LINK_INVALID,
  SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID,
  SKILL_GENERATION_TRACE_CODES.PATH_ABSOLUTE,
  SKILL_GENERATION_TRACE_CODES.PATH_TRAVERSAL,
  SKILL_GENERATION_TRACE_CODES.PATH_NOT_NORMALIZED,
  SKILL_GENERATION_TRACE_CODES.PATH_MISSING,
  SKILL_GENERATION_TRACE_CODES.PATH_SYMLINK,
  SKILL_GENERATION_TRACE_CODES.PATH_HARDLINK,
  SKILL_GENERATION_TRACE_CODES.PATH_KIND_INVALID,
  SKILL_GENERATION_TRACE_CODES.READ_INPUT_INVALID,
  SKILL_GENERATION_TRACE_CODES.PHASE_SEQUENCE_INVALID,
  SKILL_GENERATION_TRACE_CODES.READ_UNLINKED,
  SKILL_GENERATION_TRACE_CODES.STRUCTURED_OUTPUT_INPUT_INVALID,
]);

export class SkillGenerationTraceAuditError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "SkillGenerationTraceAuditError";
    this.code = code;
    this.details = details;
  }
}

const LEGACY_ALLOWED_TOOLS = Object.freeze(["Skill", "Read", "Write"]);
export const SKILL_GENERATION_PHASE_ALLOWED_TOOLS = Object.freeze(["Skill", "Read", "StructuredOutput"]);
const PARSEABLE_TOOLS = Object.freeze([...new Set([...LEGACY_ALLOWED_TOOLS, ...SKILL_GENERATION_PHASE_ALLOWED_TOOLS])]);
export const SKILL_GENERATION_TRACE_SCHEMA_VERSION = 8;
const LEGACY_SKILL_GENERATION_TOOL_ATTEMPT_POLICY = Object.freeze({
  schema_version: 1,
  version: "skill-generation-tool-attempts-v2",
  classification: "locally-recomputed-required-fields-absent",
  max_empty_write_rejections: 1,
  empty_write_rejection_requires_explicit_error: true,
  empty_write_rejection_must_follow_required_reads: true,
  empty_write_rejection_must_immediately_precede_success: true,
  empty_write_rejection_result_must_precede_success: true,
  successful_write_count: 1,
  successful_write_must_be_final_tool: true,
});
export const SKILL_GENERATION_TOOL_ATTEMPT_POLICY = Object.freeze({
  schema_version: 5,
  version: "skill-generation-tool-attempts-v6",
  classification: "batched-inputs-exact-ordered-checkpoints-compact-ir-structured-output",
  max_empty_write_rejections: 0,
  required_read_count: 8,
  required_reads_exactly_once: true,
  initial_input_read_batch_size: 2,
  remaining_reads_must_be_serial: true,
  phase_checkpoint_count: 4,
  successful_structured_output_count: 1,
  structured_output_must_immediately_follow_final_checkpoint: true,
  structured_output_must_be_final_tool: true,
  structured_output_payload: "GenerationBlueprint-v1",
  deterministic_compiler: `${SKILL_GENERATION_RULE_IR.compiler_id}@${SKILL_GENERATION_RULE_IR.compiler_version}`,
  terminal_result: "DONE",
});
const REQUIRED_INPUT_PATHS = Object.freeze([
  "inputs/wiki.md",
  "inputs/clarifications.md",
]);
const DEFAULT_REQUIRED_REFERENCES = Object.freeze([
  "references/generation-spec-v6-reference.md",
  "references/verification-contract-v2-reference.md",
]);
export const SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES = Object.freeze([
  "references/checkpoints/01-begin-repeated-families-and-paths.md",
  "references/checkpoints/02-begin-9-1-inventory.md",
  "references/checkpoints/03-begin-9-2-witnesses.md",
  "references/checkpoints/04-write-now.md",
]);
const PHASE_REQUIRED_REFERENCES = Object.freeze([
  ...DEFAULT_REQUIRED_REFERENCES,
  ...SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES,
]);
const DEFAULT_REQUIRED_RECEIPT_READS = Object.freeze([
  ...REQUIRED_INPUT_PATHS.map((relative) => `workspace/${relative}`),
  ...DEFAULT_REQUIRED_REFERENCES.map((relative) => `skill/${relative}`),
]);
export const SKILL_GENERATION_PHASE_REQUIRED_RECEIPT_READS = Object.freeze([
  ...REQUIRED_INPUT_PATHS.map((relative) => `workspace/${relative}`),
  ...PHASE_REQUIRED_REFERENCES.map((relative) => `skill/${relative}`),
]);
const OUTPUT_PATH = "output/generation-spec.json";

function fail(code, message, details = {}) {
  throw new SkillGenerationTraceAuditError(code, message, details);
}

function requireAudit(condition, code, message, details = {}) {
  if (!condition) fail(code, message, details);
}

function isPlainObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function exactKeys(value, expected) {
  return isPlainObject(value)
    && Object.keys(value).sort().join("\0") === [...expected].sort().join("\0");
}

function deepFreeze(value) {
  if (value !== null && typeof value === "object" && !Object.isFrozen(value)) {
    for (const nested of Object.values(value)) deepFreeze(nested);
    Object.freeze(value);
  }
  return value;
}

export const GENERATION_SPEC_SUBMISSION_JSON_SCHEMA = deepFreeze({
  type: "object",
  additionalProperties: false,
  required: [
    "schema_version",
    "generator_version",
    "id",
    "version",
    "capability",
    "deployment_scope",
    "summary",
    "chinese_title",
    "module_name",
    "problem_scope",
    "roles",
    "requirements",
    "logparse_plan",
    "verification_contract",
    "time_characteristics",
    "analysis_steps",
    "judgement_rules",
    "output_requirements",
    "assumptions",
    "requires_logparse",
  ],
  properties: {
    schema_version: { const: 6 },
    generator_version: { const: "6.0.0" },
    id: { type: "string", minLength: 1 },
    version: { type: "string", minLength: 1 },
    capability: { type: "string", minLength: 1 },
    deployment_scope: { type: "string", minLength: 1 },
    summary: { type: "string", minLength: 1 },
    chinese_title: { type: "string", minLength: 1 },
    module_name: { type: "string", minLength: 1 },
    problem_scope: { type: "string", minLength: 1 },
    roles: { type: "array", minItems: 2, maxItems: 2, items: { type: "object" } },
    requirements: { type: "array", minItems: 5, maxItems: 5, items: { type: "object" } },
    logparse_plan: {
      type: "object",
      additionalProperties: false,
      required: ["anchors"],
      properties: {
        anchors: { type: "array", minItems: 2, maxItems: 2, items: { type: "object" } },
      },
    },
    verification_contract: {
      type: "object",
      additionalProperties: false,
      required: [
        "schema_version",
        "observation_policies",
        "event_extractors",
        "rules",
        "terminal_paths",
      ],
      properties: {
        schema_version: { const: 2 },
        observation_policies: { type: "array", minItems: 2, maxItems: 2, items: { type: "object" } },
        event_extractors: { type: "array", minItems: 10, maxItems: 10, items: { type: "object" } },
        rules: { type: "array", minItems: 165, maxItems: 165, items: { type: "object" } },
        terminal_paths: { type: "array", minItems: 9, maxItems: 9, items: { type: "object" } },
      },
    },
    time_characteristics: { type: "array", minItems: 4, maxItems: 4, items: { type: "string", minLength: 1 } },
    analysis_steps: { type: "array", minItems: 5, maxItems: 5, items: { type: "string", minLength: 1 } },
    judgement_rules: { type: "array", minItems: 6, maxItems: 6, items: { type: "string", minLength: 1 } },
    output_requirements: { type: "array", minItems: 5, maxItems: 5, items: { type: "string", minLength: 1 } },
    assumptions: { type: "array", minItems: 3, maxItems: 3, items: { type: "string", minLength: 1 } },
    requires_logparse: { const: true },
    logparse_product: { type: "string", minLength: 1 },
  },
});

export function validGenerationSpecSubmission(value) {
  if (!isPlainObject(value)) return false;
  const schema = GENERATION_SPEC_SUBMISSION_JSON_SCHEMA;
  const allowedRootKeys = Object.keys(schema.properties);
  if (!schema.required.every((name) => Object.hasOwn(value, name))
    || !Object.keys(value).every((name) => allowedRootKeys.includes(name))
    || value.schema_version !== schema.properties.schema_version.const
    || value.generator_version !== schema.properties.generator_version.const
    || value.requires_logparse !== schema.properties.requires_logparse.const) return false;
  for (const name of ["id", "version", "capability", "deployment_scope", "summary", "chinese_title", "module_name", "problem_scope"]) {
    const constraint = schema.properties[name];
    if (typeof value[name] !== constraint.type || value[name].length < constraint.minLength) return false;
  }
  if (Object.hasOwn(value, "logparse_product")) {
    const constraint = schema.properties.logparse_product;
    if (typeof value.logparse_product !== constraint.type || value.logparse_product.length < constraint.minLength) return false;
  }
  for (const name of ["roles", "requirements"]) {
    const items = value[name];
    const constraint = schema.properties[name];
    if (!Array.isArray(items)
      || items.length < constraint.minItems
      || items.length > constraint.maxItems
      || !items.every((item) => isPlainObject(item))) return false;
  }
  const logparsePlan = value.logparse_plan;
  const logparsePlanSchema = schema.properties.logparse_plan;
  if (!isPlainObject(logparsePlan)
    || !exactKeys(logparsePlan, logparsePlanSchema.required)
    || !Array.isArray(logparsePlan.anchors)
    || logparsePlan.anchors.length < logparsePlanSchema.properties.anchors.minItems
    || logparsePlan.anchors.length > logparsePlanSchema.properties.anchors.maxItems
    || !logparsePlan.anchors.every((item) => isPlainObject(item))) return false;
  for (const name of ["time_characteristics", "analysis_steps", "judgement_rules", "output_requirements", "assumptions"]) {
    const items = value[name];
    const constraint = schema.properties[name];
    if (!Array.isArray(items)
      || items.length < constraint.minItems
      || items.length > constraint.maxItems
      || !items.every((item) => typeof item === constraint.items.type && item.length >= constraint.items.minLength)) return false;
  }
  const verification = value.verification_contract;
  const verificationSchema = schema.properties.verification_contract;
  if (!isPlainObject(verification)
    || !verificationSchema.required.every((name) => Object.hasOwn(verification, name))
    || !Object.keys(verification).every((name) => Object.hasOwn(verificationSchema.properties, name))
    || verification.schema_version !== verificationSchema.properties.schema_version.const) return false;
  for (const name of ["observation_policies", "event_extractors", "rules", "terminal_paths"]) {
    const items = verification[name];
    const constraint = verificationSchema.properties[name];
    if (!Array.isArray(items)
      || (constraint.minItems !== undefined && items.length < constraint.minItems)
      || (constraint.maxItems !== undefined && items.length > constraint.maxItems)
      || !items.every((item) => isPlainObject(item))) return false;
  }
  return true;
}

function validatedRuleIrCompilation(compilation, irBytes) {
  requireAudit(
    exactKeys(compilation, ["schema_version", "compiler", "ir", "output", "spec"])
      && compilation.schema_version === 1,
    SKILL_GENERATION_TRACE_CODES.RULE_IR_INVALID,
    "The deterministic compiler envelope is invalid",
  );
  requireAudit(
    validSkillGenerationCompilerReceipt(compilation.compiler),
    SKILL_GENERATION_TRACE_CODES.RULE_IR_INVALID,
    "The deterministic compiler identity is invalid",
  );
  const expectedIr = {
    size_bytes: irBytes.length,
    sha256: sha256Bytes(irBytes),
  };
  requireAudit(
    exactKeys(compilation.ir, ["size_bytes", "sha256"])
      && compilation.ir.size_bytes === expectedIr.size_bytes
      && compilation.ir.sha256 === expectedIr.sha256,
    SKILL_GENERATION_TRACE_CODES.RULE_IR_INVALID,
    "The deterministic compiler IR seal is invalid",
  );
  requireAudit(
    validGenerationSpecSubmission(compilation.spec),
    SKILL_GENERATION_TRACE_CODES.RULE_IR_INVALID,
    "The expanded GenerationSpec shape is invalid",
  );
  const outputBytes = Buffer.from(canonicalJson(compilation.spec), "utf8");
  const expectedOutput = {
    size_bytes: outputBytes.length,
    sha256: sha256Bytes(outputBytes),
  };
  requireAudit(
    exactKeys(compilation.output, ["size_bytes", "sha256"])
      && compilation.output.size_bytes === expectedOutput.size_bytes
      && compilation.output.sha256 === expectedOutput.sha256,
    SKILL_GENERATION_TRACE_CODES.RULE_IR_INVALID,
    "The deterministic compiler output seal is invalid",
  );
  return {
    compiler: { ...compilation.compiler },
    ir: expectedIr,
    output: expectedOutput,
  };
}

const GENERATION_SPEC_DIAGNOSTIC_MAX_VIOLATIONS = 64;
const DIAGNOSTIC_ACTUAL_KINDS = new Set([
  "missing", "null", "boolean", "integer", "number", "string", "array", "object",
]);
const diagnosticConstraints = [];
const addDiagnosticConstraint = (constraint_id, schema_pointer, keyword, expected_kind = null, expected_count = null) => {
  diagnosticConstraints.push(Object.freeze({ constraint_id, schema_pointer, keyword, expected_kind, expected_count }));
};
const diagnosticToken = (value) => value.toUpperCase();
addDiagnosticConstraint("ROOT_TYPE", "#/type", "type", "object");
for (const name of GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.required) {
  addDiagnosticConstraint(`ROOT_REQUIRED_${diagnosticToken(name)}`, "#/required", "required", "present");
}
addDiagnosticConstraint("ROOT_ADDITIONAL_PROPERTIES", "#/additionalProperties", "additionalProperties", null, 0);
for (const name of ["schema_version", "generator_version", "requires_logparse"]) {
  addDiagnosticConstraint(`ROOT_CONST_${diagnosticToken(name)}`, `#/properties/${name}/const`, "const", "frozen_constant");
}
for (const name of ["id", "version", "capability", "deployment_scope", "summary", "chinese_title", "module_name", "problem_scope", "logparse_product"]) {
  addDiagnosticConstraint(`ROOT_TYPE_${diagnosticToken(name)}`, `#/properties/${name}/type`, "type", "string");
  addDiagnosticConstraint(`ROOT_MIN_LENGTH_${diagnosticToken(name)}`, `#/properties/${name}/minLength`, "minLength", null, 1);
}
for (const [name, count] of [["roles", 2], ["requirements", 5]]) {
  addDiagnosticConstraint(`ROOT_TYPE_${diagnosticToken(name)}`, `#/properties/${name}/type`, "type", "array");
  addDiagnosticConstraint(`ROOT_MIN_ITEMS_${diagnosticToken(name)}`, `#/properties/${name}/minItems`, "minItems", null, count);
  addDiagnosticConstraint(`ROOT_MAX_ITEMS_${diagnosticToken(name)}`, `#/properties/${name}/maxItems`, "maxItems", null, count);
  addDiagnosticConstraint(`ROOT_ITEM_TYPE_${diagnosticToken(name)}`, `#/properties/${name}/items/type`, "type", "object");
}
addDiagnosticConstraint("LOGPARSE_PLAN_TYPE", "#/properties/logparse_plan/type", "type", "object");
addDiagnosticConstraint("LOGPARSE_PLAN_REQUIRED_ANCHORS", "#/properties/logparse_plan/required", "required", "present");
addDiagnosticConstraint("LOGPARSE_PLAN_ADDITIONAL_PROPERTIES", "#/properties/logparse_plan/additionalProperties", "additionalProperties", null, 0);
addDiagnosticConstraint("LOGPARSE_PLAN_ANCHORS_TYPE", "#/properties/logparse_plan/properties/anchors/type", "type", "array");
addDiagnosticConstraint("LOGPARSE_PLAN_ANCHORS_MIN_ITEMS", "#/properties/logparse_plan/properties/anchors/minItems", "minItems", null, 2);
addDiagnosticConstraint("LOGPARSE_PLAN_ANCHORS_MAX_ITEMS", "#/properties/logparse_plan/properties/anchors/maxItems", "maxItems", null, 2);
addDiagnosticConstraint("LOGPARSE_PLAN_ANCHORS_ITEM_TYPE", "#/properties/logparse_plan/properties/anchors/items/type", "type", "object");
addDiagnosticConstraint("VERIFICATION_TYPE", "#/properties/verification_contract/type", "type", "object");
for (const name of GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.properties.verification_contract.required) {
  addDiagnosticConstraint(`VERIFICATION_REQUIRED_${diagnosticToken(name)}`, "#/properties/verification_contract/required", "required", "present");
}
addDiagnosticConstraint("VERIFICATION_ADDITIONAL_PROPERTIES", "#/properties/verification_contract/additionalProperties", "additionalProperties", null, 0);
addDiagnosticConstraint("VERIFICATION_CONST_SCHEMA_VERSION", "#/properties/verification_contract/properties/schema_version/const", "const", "frozen_constant");
for (const [name, count] of [["observation_policies", 2], ["event_extractors", 10], ["rules", 165], ["terminal_paths", 9]]) {
  const token = diagnosticToken(name);
  const pointer = `#/properties/verification_contract/properties/${name}`;
  addDiagnosticConstraint(`VERIFICATION_TYPE_${token}`, `${pointer}/type`, "type", "array");
  addDiagnosticConstraint(`VERIFICATION_MIN_ITEMS_${token}`, `${pointer}/minItems`, "minItems", null, count);
  addDiagnosticConstraint(`VERIFICATION_MAX_ITEMS_${token}`, `${pointer}/maxItems`, "maxItems", null, count);
  addDiagnosticConstraint(`VERIFICATION_ITEM_TYPE_${token}`, `${pointer}/items/type`, "type", "object");
}
for (const [name, count] of [["time_characteristics", 4], ["analysis_steps", 5], ["judgement_rules", 6], ["output_requirements", 5], ["assumptions", 3]]) {
  const token = diagnosticToken(name);
  const pointer = `#/properties/${name}`;
  addDiagnosticConstraint(`ROOT_TYPE_${token}`, `${pointer}/type`, "type", "array");
  addDiagnosticConstraint(`ROOT_MIN_ITEMS_${token}`, `${pointer}/minItems`, "minItems", null, count);
  addDiagnosticConstraint(`ROOT_MAX_ITEMS_${token}`, `${pointer}/maxItems`, "maxItems", null, count);
  addDiagnosticConstraint(`ROOT_ITEM_TYPE_${token}`, `${pointer}/items/type`, "type", "string");
  addDiagnosticConstraint(`ROOT_ITEM_MIN_LENGTH_${token}`, `${pointer}/items/minLength`, "minLength", null, 1);
}
const GENERATION_SPEC_DIAGNOSTIC_CONSTRAINTS = Object.freeze(diagnosticConstraints);
const GENERATION_SPEC_DIAGNOSTIC_CONSTRAINT_BY_ID = new Map(
  GENERATION_SPEC_DIAGNOSTIC_CONSTRAINTS.map((constraint, index) => [constraint.constraint_id, { ...constraint, index }]),
);

function diagnosticKind(value) {
  if (value === undefined) return "missing";
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  if (typeof value === "number") return Number.isInteger(value) ? "integer" : "number";
  return typeof value === "object" ? "object" : typeof value;
}

export function validGenerationSpecSubmissionDiagnostic(value) {
  if (!exactKeys(value, ["schema_version", "status", "violations"])
    || value.schema_version !== 1
    || !["INVALID", "SCHEMA_VALID_TOOL_REJECTED"].includes(value.status)
    || !Array.isArray(value.violations)
    || value.violations.length > GENERATION_SPEC_DIAGNOSTIC_MAX_VIOLATIONS
    || (value.status === "INVALID") !== (value.violations.length > 0)) return false;
  let previousIndex = -1;
  for (const violation of value.violations) {
    if (!exactKeys(violation, [
      "constraint_id", "schema_pointer", "keyword", "expected_kind", "expected_count",
      "actual_kind", "actual_count",
    ])) return false;
    const constraint = GENERATION_SPEC_DIAGNOSTIC_CONSTRAINT_BY_ID.get(violation.constraint_id);
    if (!constraint
      || constraint.index <= previousIndex
      || violation.schema_pointer !== constraint.schema_pointer
      || violation.keyword !== constraint.keyword
      || violation.expected_kind !== constraint.expected_kind
      || violation.expected_count !== constraint.expected_count
      || !(violation.actual_kind === null || DIAGNOSTIC_ACTUAL_KINDS.has(violation.actual_kind))
      || !(violation.actual_count === null || (Number.isSafeInteger(violation.actual_count) && violation.actual_count >= 0))) return false;
    if (constraint.keyword === "required" && !(violation.actual_kind === "missing" && violation.actual_count === null)) return false;
    if (constraint.keyword === "additionalProperties" && !(violation.actual_kind === null && Number.isSafeInteger(violation.actual_count) && violation.actual_count > 0)) return false;
    if (["minItems", "maxItems", "minLength"].includes(constraint.keyword)
      && !(violation.actual_kind === null && Number.isSafeInteger(violation.actual_count))) return false;
    if (["type", "const"].includes(constraint.keyword) && !(violation.actual_kind !== null && violation.actual_count === null)) return false;
    if (["type", "const"].includes(constraint.keyword) && violation.actual_kind === "missing") return false;
    if (constraint.keyword === "type" && violation.actual_kind === constraint.expected_kind) return false;
    if (["minItems", "minLength"].includes(constraint.keyword) && violation.actual_count >= constraint.expected_count) return false;
    if (constraint.keyword === "maxItems" && violation.actual_count <= constraint.expected_count) return false;
    previousIndex = constraint.index;
  }
  return true;
}

export function buildGenerationSpecSubmissionDiagnostic(value) {
  const observed = new Map();
  const add = (constraintId, actual_kind = null, actual_count = null) => {
    if (observed.has(constraintId)) return;
    const constraint = GENERATION_SPEC_DIAGNOSTIC_CONSTRAINT_BY_ID.get(constraintId);
    if (!constraint) throw new Error("GENERATION_SPEC_DIAGNOSTIC_CONSTRAINT_UNKNOWN");
    observed.set(constraintId, {
      constraint_id: constraint.constraint_id,
      schema_pointer: constraint.schema_pointer,
      keyword: constraint.keyword,
      expected_kind: constraint.expected_kind,
      expected_count: constraint.expected_count,
      actual_kind,
      actual_count,
    });
  };
  const required = GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.required;
  if (!isPlainObject(value)) {
    add("ROOT_TYPE", diagnosticKind(value));
  } else {
    for (const name of required) {
      if (!Object.hasOwn(value, name)) add(`ROOT_REQUIRED_${diagnosticToken(name)}`, "missing");
    }
    const extras = Object.keys(value).filter((name) => !Object.hasOwn(GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.properties, name));
    if (extras.length > 0) add("ROOT_ADDITIONAL_PROPERTIES", null, extras.length);
    for (const name of ["schema_version", "generator_version", "requires_logparse"]) {
      if (Object.hasOwn(value, name) && value[name] !== GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.properties[name].const) {
        add(`ROOT_CONST_${diagnosticToken(name)}`, diagnosticKind(value[name]));
      }
    }
    for (const name of ["id", "version", "capability", "deployment_scope", "summary", "chinese_title", "module_name", "problem_scope", "logparse_product"]) {
      if (!Object.hasOwn(value, name)) continue;
      if (typeof value[name] !== "string") add(`ROOT_TYPE_${diagnosticToken(name)}`, diagnosticKind(value[name]));
      else if (value[name].length < 1) add(`ROOT_MIN_LENGTH_${diagnosticToken(name)}`, null, value[name].length);
    }
    for (const [name, count] of [["roles", 2], ["requirements", 5]]) {
      if (!Object.hasOwn(value, name)) continue;
      if (!Array.isArray(value[name])) add(`ROOT_TYPE_${diagnosticToken(name)}`, diagnosticKind(value[name]));
      else {
        if (value[name].length < count) add(`ROOT_MIN_ITEMS_${diagnosticToken(name)}`, null, value[name].length);
        if (value[name].length > count) add(`ROOT_MAX_ITEMS_${diagnosticToken(name)}`, null, value[name].length);
        const invalid = value[name].find((item) => !isPlainObject(item));
        if (invalid !== undefined) add(`ROOT_ITEM_TYPE_${diagnosticToken(name)}`, diagnosticKind(invalid));
      }
    }
    const logparsePlan = value.logparse_plan;
    if (Object.hasOwn(value, "logparse_plan")) {
      if (!isPlainObject(logparsePlan)) add("LOGPARSE_PLAN_TYPE", diagnosticKind(logparsePlan));
      else {
        if (!Object.hasOwn(logparsePlan, "anchors")) add("LOGPARSE_PLAN_REQUIRED_ANCHORS", "missing");
        const extras = Object.keys(logparsePlan).filter((name) => name !== "anchors");
        if (extras.length > 0) add("LOGPARSE_PLAN_ADDITIONAL_PROPERTIES", null, extras.length);
        if (Object.hasOwn(logparsePlan, "anchors")) {
          if (!Array.isArray(logparsePlan.anchors)) add("LOGPARSE_PLAN_ANCHORS_TYPE", diagnosticKind(logparsePlan.anchors));
          else {
            if (logparsePlan.anchors.length < 2) add("LOGPARSE_PLAN_ANCHORS_MIN_ITEMS", null, logparsePlan.anchors.length);
            if (logparsePlan.anchors.length > 2) add("LOGPARSE_PLAN_ANCHORS_MAX_ITEMS", null, logparsePlan.anchors.length);
            const invalid = logparsePlan.anchors.find((item) => !isPlainObject(item));
            if (invalid !== undefined) add("LOGPARSE_PLAN_ANCHORS_ITEM_TYPE", diagnosticKind(invalid));
          }
        }
      }
    }
    const verification = value.verification_contract;
    if (Object.hasOwn(value, "verification_contract")) {
      if (!isPlainObject(verification)) add("VERIFICATION_TYPE", diagnosticKind(verification));
      else {
        const verificationSchema = GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.properties.verification_contract;
        for (const name of verificationSchema.required) {
          if (!Object.hasOwn(verification, name)) add(`VERIFICATION_REQUIRED_${diagnosticToken(name)}`, "missing");
        }
        const extras = Object.keys(verification).filter((name) => !Object.hasOwn(verificationSchema.properties, name));
        if (extras.length > 0) add("VERIFICATION_ADDITIONAL_PROPERTIES", null, extras.length);
        if (Object.hasOwn(verification, "schema_version") && verification.schema_version !== 2) {
          add("VERIFICATION_CONST_SCHEMA_VERSION", diagnosticKind(verification.schema_version));
        }
        for (const [name, count] of [["observation_policies", 2], ["event_extractors", 10], ["rules", 165], ["terminal_paths", 9]]) {
          if (!Object.hasOwn(verification, name)) continue;
          const token = diagnosticToken(name);
          if (!Array.isArray(verification[name])) add(`VERIFICATION_TYPE_${token}`, diagnosticKind(verification[name]));
          else {
            if (verification[name].length < count) add(`VERIFICATION_MIN_ITEMS_${token}`, null, verification[name].length);
            if (verification[name].length > count) add(`VERIFICATION_MAX_ITEMS_${token}`, null, verification[name].length);
            const invalid = verification[name].find((item) => !isPlainObject(item));
            if (invalid !== undefined) add(`VERIFICATION_ITEM_TYPE_${token}`, diagnosticKind(invalid));
          }
        }
      }
    }
    for (const [name, count] of [["time_characteristics", 4], ["analysis_steps", 5], ["judgement_rules", 6], ["output_requirements", 5], ["assumptions", 3]]) {
      if (!Object.hasOwn(value, name)) continue;
      const token = diagnosticToken(name);
      if (!Array.isArray(value[name])) add(`ROOT_TYPE_${token}`, diagnosticKind(value[name]));
      else {
        if (value[name].length < count) add(`ROOT_MIN_ITEMS_${token}`, null, value[name].length);
        if (value[name].length > count) add(`ROOT_MAX_ITEMS_${token}`, null, value[name].length);
        const invalidType = value[name].find((item) => typeof item !== "string");
        if (invalidType !== undefined) add(`ROOT_ITEM_TYPE_${token}`, diagnosticKind(invalidType));
        else if (value[name].some((item) => item.length < 1)) add(`ROOT_ITEM_MIN_LENGTH_${token}`, null, 0);
      }
    }
  }
  const violations = [...observed.values()]
    .sort((left, right) => GENERATION_SPEC_DIAGNOSTIC_CONSTRAINT_BY_ID.get(left.constraint_id).index
      - GENERATION_SPEC_DIAGNOSTIC_CONSTRAINT_BY_ID.get(right.constraint_id).index)
    .slice(0, GENERATION_SPEC_DIAGNOSTIC_MAX_VIOLATIONS);
  if (validGenerationSpecSubmission(value) !== (violations.length === 0)) {
    throw new Error("GENERATION_SPEC_DIAGNOSTIC_COVERAGE_GAP");
  }
  const diagnostic = {
    schema_version: 1,
    status: violations.length === 0 ? "SCHEMA_VALID_TOOL_REJECTED" : "INVALID",
    violations,
  };
  if (!validGenerationSpecSubmissionDiagnostic(diagnostic)) throw new Error("GENERATION_SPEC_DIAGNOSTIC_INVALID");
  return diagnostic;
}

function portableRelativePath(value) {
  requireAudit(typeof value === "string" && value.length > 0 && !value.includes("\0"), SKILL_GENERATION_TRACE_CODES.PATH_NOT_NORMALIZED, "Tool paths must be non-empty portable relative paths");
  requireAudit(!path.posix.isAbsolute(value) && !path.win32.isAbsolute(value), SKILL_GENERATION_TRACE_CODES.PATH_ABSOLUTE, "Absolute tool paths are forbidden");
  const slashed = value.replaceAll("\\", "/");
  const segments = slashed.split("/");
  requireAudit(!segments.includes(".."), SKILL_GENERATION_TRACE_CODES.PATH_TRAVERSAL, "Tool paths must not traverse parent directories");
  requireAudit(!segments.some((segment) => segment === "" || segment === ".") && slashed === value && path.posix.normalize(slashed) === slashed, SKILL_GENERATION_TRACE_CODES.PATH_NOT_NORMALIZED, "Tool paths must use normalized forward-slash syntax");
  return slashed;
}

function dotSegmentPath(value) {
  const slashed = value.replaceAll("\\", "/");
  const withoutRoot = slashed
    .replace(/^[A-Za-z]:\//, "")
    .replace(/^\/+/, "");
  return withoutRoot.split("/").some((segment) => segment === "" || segment === "." || segment === "..");
}

function pathInside(root, candidate) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function inspectRoot(root, label) {
  requireAudit(typeof root === "string" && path.isAbsolute(root), SKILL_GENERATION_TRACE_CODES.ROOT_INVALID, `${label} must be an absolute path`);
  let metadata;
  try {
    metadata = fs.lstatSync(root);
  } catch (error) {
    fail(SKILL_GENERATION_TRACE_CODES.ROOT_INVALID, `${label} is unavailable`, { cause: error?.code ?? "UNKNOWN" });
  }
  requireAudit(!metadata.isSymbolicLink() && metadata.isDirectory(), SKILL_GENERATION_TRACE_CODES.ROOT_INVALID, `${label} must be a real directory`);
  let real;
  try {
    real = fs.realpathSync.native(root);
  } catch (error) {
    fail(SKILL_GENERATION_TRACE_CODES.ROOT_INVALID, `${label} cannot be resolved`, { cause: error?.code ?? "UNKNOWN" });
  }
  const resolved = path.resolve(root);
  const same = process.platform === "win32"
    ? real.toLowerCase() === resolved.toLowerCase()
    : real === resolved;
  requireAudit(same, SKILL_GENERATION_TRACE_CODES.ROOT_INVALID, `${label} must not contain symlinked path components`);
}

function inspectRegularFile(root, relativePath) {
  inspectRoot(root, "Path root");
  let current = path.resolve(root);
  const segments = portableRelativePath(relativePath).split("/");
  for (let index = 0; index < segments.length; index += 1) {
    current = path.join(current, segments[index]);
    let metadata;
    try {
      metadata = fs.lstatSync(current);
    } catch (error) {
      fail(SKILL_GENERATION_TRACE_CODES.PATH_MISSING, `Audited path does not exist: ${relativePath}`, { path: relativePath, cause: error?.code ?? "UNKNOWN" });
    }
    requireAudit(!metadata.isSymbolicLink(), SKILL_GENERATION_TRACE_CODES.PATH_SYMLINK, `Audited paths must not contain symlinks: ${relativePath}`, { path: relativePath });
    const final = index === segments.length - 1;
    requireAudit(final ? metadata.isFile() : metadata.isDirectory(), SKILL_GENERATION_TRACE_CODES.PATH_KIND_INVALID, `Audited path has an invalid node kind: ${relativePath}`, { path: relativePath });
    if (final) requireAudit(metadata.nlink === 1, SKILL_GENERATION_TRACE_CODES.PATH_HARDLINK, `Audited files must not be hardlinked: ${relativePath}`, { path: relativePath });
  }
  requireAudit(pathInside(root, current), SKILL_GENERATION_TRACE_CODES.PATH_TRAVERSAL, `Audited path escapes its root: ${relativePath}`, { path: relativePath });
  return current;
}

function ordinaryMarkdownLinks(document) {
  const links = [];
  const ordinaryText = ordinaryMarkdownText(document);
  const pattern = /(?<!!)\[[^\]\r\n]*\]\(\s*(?:<([^>\r\n]+)>|([^\s)]+))(?:\s+(?:"[^"]*"|'[^']*'))?\s*\)/g;
  for (const match of ordinaryText.matchAll(pattern)) links.push(match[1] ?? match[2]);
  return links;
}

function ordinaryMarkdownText(document) {
  return document
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/^[ \t]*(?:```|~~~)[^\r\n]*\r?\n[\s\S]*?^[ \t]*(?:```|~~~)[ \t]*$/gm, "")
    .replace(/`[^`\r\n]*`/g, "");
}

export function discoverLinkedSkillReferences(skillRoot) {
  inspectRoot(skillRoot, "Skill root");
  const skillDocumentPath = inspectRegularFile(skillRoot, "SKILL.md");
  let document;
  try {
    document = fs.readFileSync(skillDocumentPath, "utf8");
  } catch (error) {
    fail(SKILL_GENERATION_TRACE_CODES.SKILL_DOCUMENT_INVALID, "The Skill document could not be read as UTF-8", { cause: error?.code ?? "UNKNOWN" });
  }
  const ordinaryText = ordinaryMarkdownText(document);
  requireAudit(
    !/!\[[^\]\r\n]*\]\(\s*(?:<[^>\r\n]+>|[^\s)]+)|\[[^\]\r\n]+\]\[[^\]\r\n]*\]|<a\s+[^>]*href\s*=/i.test(ordinaryText),
    SKILL_GENERATION_TRACE_CODES.SKILL_LINK_INVALID,
    "Only ordinary inline Markdown links may declare Skill references",
  );
  const references = new Set();
  for (const target of ordinaryMarkdownLinks(document)) {
    requireAudit(!/^[a-z][a-z0-9+.-]*:/i.test(target) && !target.startsWith("#"), SKILL_GENERATION_TRACE_CODES.SKILL_LINK_INVALID, "Skill reference links must be local file paths");
    let normalized;
    try {
      normalized = portableRelativePath(target);
    } catch (error) {
      fail(SKILL_GENERATION_TRACE_CODES.SKILL_LINK_INVALID, "A local Skill link is not a safe relative reference", { cause: error?.code ?? "UNKNOWN" });
    }
    requireAudit(normalized.startsWith("references/"), SKILL_GENERATION_TRACE_CODES.SKILL_LINK_INVALID, "Local Skill links must remain under references/", { path: normalized });
    inspectRegularFile(skillRoot, normalized);
    references.add(normalized);
  }
  return [...references].sort();
}

function validatedLinkedReferences(skillRoot, linkedReferences) {
  requireAudit(Array.isArray(linkedReferences) && linkedReferences.length > 0, SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "linkedReferences must be a non-empty array");
  const discovered = discoverLinkedSkillReferences(skillRoot);
  const normalized = linkedReferences.map((relative) => portableRelativePath(relative));
  requireAudit(new Set(normalized).size === normalized.length, SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "linkedReferences must be unique");
  requireAudit(normalized.every((relative) => relative.startsWith("references/")), SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "linkedReferences must remain under references/");
  requireAudit([...normalized].sort().join("\0") === discovered.join("\0"), SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "linkedReferences must exactly match safe ordinary references discovered from SKILL.md");
  for (const relative of normalized) inspectRegularFile(skillRoot, relative);
  return discovered;
}

export function isSkillGenerationPhaseCheckpointMode(linkedReferences) {
  const checkpointCount = SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES.filter((relative) => linkedReferences.includes(relative)).length;
  if (checkpointCount === 0) return false;
  requireAudit(
    checkpointCount === SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES.length
      && [...linkedReferences].sort().join("\0") === [...PHASE_REQUIRED_REFERENCES].sort().join("\0"),
    SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID,
    "Checkpoint mode requires exactly the two contract references and four frozen phase checkpoints",
  );
  return true;
}

function permissionAbsolute(filePath) {
  const resolved = path.resolve(filePath);
  let portable;
  const drive = /^([A-Za-z]):[\\/](.*)$/.exec(resolved);
  if (drive) portable = `${drive[1]}/${drive[2].replaceAll("\\", "/")}`;
  else portable = resolved.split(path.sep).join("/").replace(/^\/+/, "");
  return `//${portable.replaceAll("\\", "\\\\").replaceAll("(", "\\(").replaceAll(")", "\\)")}`;
}

export function skillGenerationPermissionRules({ workspaceRoot, skillRoot, linkedReferences, sourceRoot = null }) {
  inspectRoot(workspaceRoot, "Workspace root");
  inspectRoot(skillRoot, "Skill root");
  if (sourceRoot !== null) {
    inspectRoot(sourceRoot, "Source root");
    requireAudit(!pathInside(sourceRoot, workspaceRoot) && !pathInside(workspaceRoot, sourceRoot), SKILL_GENERATION_TRACE_CODES.ROOT_INVALID, "The audited workspace must be outside the source repository");
  }
  for (const relative of REQUIRED_INPUT_PATHS) inspectRegularFile(workspaceRoot, relative);
  const references = validatedLinkedReferences(skillRoot, linkedReferences);
  const phaseMode = isSkillGenerationPhaseCheckpointMode(references);
  const permissionReferences = phaseMode ? PHASE_REQUIRED_REFERENCES : references;
  return [
    "Skill(wiki-to-diagnosis-skill)",
    "Read(/inputs/wiki.md)",
    "Read(/inputs/clarifications.md)",
    ...permissionReferences.map((relative) => `Read(${permissionAbsolute(path.join(skillRoot, ...relative.split("/")))})`),
    ...(phaseMode ? ["StructuredOutput"] : [
      // Claude Code's Edit(path) permission category authorizes both Edit and
      // Write file operations. Legacy direct-audit fixtures retain this path;
      // the production checkpoint workflow never exposes either tool.
      "Edit(/output/generation-spec.json)",
    ]),
  ];
}

function sameAbsolutePath(left, right) {
  const resolvedLeft = path.resolve(left);
  const resolvedRight = path.resolve(right);
  return process.platform === "win32"
    ? resolvedLeft.toLowerCase() === resolvedRight.toLowerCase()
    : resolvedLeft === resolvedRight;
}

function observedPath(value, { workspaceRoot, skillRoot, linkedReferences, mode }) {
  requireAudit(typeof value === "string" && value.length > 0 && !value.includes("\0"), SKILL_GENERATION_TRACE_CODES.PATH_NOT_NORMALIZED, "Tool paths must be non-empty strings");
  requireAudit(!dotSegmentPath(value), SKILL_GENERATION_TRACE_CODES.PATH_TRAVERSAL, "Tool paths must not contain empty, current, or parent segments");
  const absolute = path.isAbsolute(value);
  const foreignAbsolute = process.platform === "win32"
    ? path.posix.isAbsolute(value)
    : path.win32.isAbsolute(value);
  if (!absolute && foreignAbsolute) {
    fail(SKILL_GENERATION_TRACE_CODES.PATH_ABSOLUTE, "Foreign-platform absolute tool paths are forbidden");
  }
  const workspaceCandidates = mode === "read" ? REQUIRED_INPUT_PATHS : [OUTPUT_PATH];
  if (!absolute) {
    const relative = portableRelativePath(value);
    requireAudit(workspaceCandidates.includes(relative), mode === "read" ? SKILL_GENERATION_TRACE_CODES.READ_UNLINKED : SKILL_GENERATION_TRACE_CODES.WRITE_PATH_INVALID, "Relative tool path is outside the fixed workspace allowlist", { path: relative });
    inspectRegularFile(workspaceRoot, relative);
    return { receiptPath: `workspace/${relative}`, absolutePath: path.join(workspaceRoot, ...relative.split("/")) };
  }

  for (const relative of workspaceCandidates) {
    const candidate = path.join(workspaceRoot, ...relative.split("/"));
    if (sameAbsolutePath(value, candidate)) {
      inspectRegularFile(workspaceRoot, relative);
      requireAudit(sameAbsolutePath(fs.realpathSync.native(value), fs.realpathSync.native(candidate)), SKILL_GENERATION_TRACE_CODES.PATH_SYMLINK, "Absolute workspace path must resolve exactly to its allowed file");
      return { receiptPath: `workspace/${relative}`, absolutePath: candidate };
    }
  }
  if (mode === "read") {
    for (const relative of linkedReferences) {
      const candidate = path.join(skillRoot, ...relative.split("/"));
      if (sameAbsolutePath(value, candidate)) {
        inspectRegularFile(skillRoot, relative);
        requireAudit(sameAbsolutePath(fs.realpathSync.native(value), fs.realpathSync.native(candidate)), SKILL_GENERATION_TRACE_CODES.PATH_SYMLINK, "Absolute Skill reference path must resolve exactly to its discovered file");
        return { receiptPath: `skill/${relative}`, absolutePath: candidate };
      }
    }
  }
  fail(mode === "read" ? SKILL_GENERATION_TRACE_CODES.READ_UNLINKED : SKILL_GENERATION_TRACE_CODES.WRITE_PATH_INVALID, "Absolute tool path is outside the exact audited allowlist");
}

function normalizeRequiredReferences(values, links) {
  requireAudit(Array.isArray(values) && values.length > 0, SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "At least one required Skill reference is needed");
  const normalized = values.map((value) => {
    let candidate;
    try {
      candidate = portableRelativePath(value);
    } catch (error) {
      fail(SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "A required Skill reference is invalid", { cause: error?.code ?? "UNKNOWN" });
    }
    requireAudit(candidate.startsWith("references/") && links.has(candidate), SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "A required Skill reference is not linked by SKILL.md", { path: candidate });
    return candidate;
  });
  requireAudit(new Set(normalized).size === normalized.length, SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "Required Skill references must be unique");
  return normalized;
}

function parseToolRecords(events, { allowPending = false } = {}) {
  const records = [];
  const byId = new Map();
  for (const [eventIndex, event] of events.entries()) {
    const content = event?.message?.content;
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      if (block?.type === "tool_use") {
        requireAudit(event.type === "assistant" && event.message?.role === "assistant", SKILL_GENERATION_TRACE_CODES.TOOL_EVENT_INVALID, "tool_use must be emitted by an assistant message");
        requireAudit(PARSEABLE_TOOLS.includes(block.name), SKILL_GENERATION_TRACE_CODES.TOOL_NOT_ALLOWED, `Tool is not allowed: ${String(block.name)}`);
        requireAudit(typeof block.id === "string" && block.id.length > 0 && !byId.has(block.id), SKILL_GENERATION_TRACE_CODES.TOOL_USE_ID_INVALID, "tool_use IDs must be non-empty and unique");
        const record = {
          ordinal: records.length,
          id: block.id,
          tool: block.name,
          input: block.input,
          result: null,
          use_event_index: eventIndex,
          result_event_index: null,
        };
        records.push(record);
        byId.set(record.id, record);
      } else if (block?.type === "tool_result") {
        requireAudit(event.type === "user" && event.message?.role === "user", SKILL_GENERATION_TRACE_CODES.TOOL_EVENT_INVALID, "tool_result must be emitted by a user message");
        requireAudit(block.is_error === undefined || typeof block.is_error === "boolean", SKILL_GENERATION_TRACE_CODES.TOOL_EVENT_INVALID, "tool_result is_error must be boolean when present");
        const record = byId.get(block.tool_use_id);
        requireAudit(record !== undefined, SKILL_GENERATION_TRACE_CODES.TOOL_RESULT_UNMATCHED, "tool_result does not match a tool_use");
        requireAudit(record.result === null, SKILL_GENERATION_TRACE_CODES.TOOL_RESULT_DUPLICATE, "A tool_use has more than one tool_result");
        const raw = event.tool_use_result;
        requireAudit(raw?.isError === undefined || typeof raw.isError === "boolean", SKILL_GENERATION_TRACE_CODES.TOOL_EVENT_INVALID, "tool_use_result isError must be boolean when present");
        requireAudit(raw?.is_error === undefined || typeof raw.is_error === "boolean", SKILL_GENERATION_TRACE_CODES.TOOL_EVENT_INVALID, "tool_use_result is_error must be boolean when present");
        requireAudit(raw?.success === undefined || typeof raw.success === "boolean", SKILL_GENERATION_TRACE_CODES.TOOL_EVENT_INVALID, "tool_use_result success must be boolean when present");
        const failed = block.is_error === true
          || raw?.isError === true
          || raw?.is_error === true
          || raw?.success === false;
        record.result = {
          raw,
          outcome: failed ? "ERROR" : "SUCCESS",
          explicit_error: block.is_error === true,
          contradictory_success: failed && raw?.success === true,
        };
        record.result_event_index = eventIndex;
      }
    }
  }
  if (!allowPending) {
    requireAudit(records.every((record) => record.result !== null), SKILL_GENERATION_TRACE_CODES.TOOL_RESULT_MISSING, "Every tool_use must have exactly one tool_result");
  }
  return records;
}

function emptyWriteValidationRejection(record) {
  return record.tool === "Write"
    && exactKeys(record.input, [])
    && record.result?.outcome === "ERROR"
    && record.result.explicit_error === true
    && record.result.contradictory_success === false;
}

function validateSkillInvocation(records) {
  const skills = records.filter((record) => record.tool === "Skill");
  requireAudit(skills.length === 1 && skills[0].ordinal === 0 && exactKeys(skills[0].input, ["skill"]) && skills[0].input.skill === "wiki-to-diagnosis-skill", SKILL_GENERATION_TRACE_CODES.SKILL_INVOCATION_INVALID, "The first and only Skill call must load wiki-to-diagnosis-skill with exact input");
  requireAudit(isPlainObject(skills[0].result.raw) && skills[0].result.raw.success === true, SKILL_GENERATION_TRACE_CODES.SKILL_RESULT_INVALID, "The Skill tool_result must explicitly report success");
  requireAudit(records.slice(1).every((record) => skills[0].result_event_index < record.use_event_index), SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_ORDER_INVALID, "The Skill must finish loading before any Read or Write starts");
}

function validateReadInput(record) {
  requireAudit(isPlainObject(record.input) && typeof record.input.file_path === "string", SKILL_GENERATION_TRACE_CODES.READ_INPUT_INVALID, "Read input must contain file_path");
  const allowed = new Set(["file_path", "offset", "limit", "pages"]);
  requireAudit(Object.keys(record.input).every((key) => allowed.has(key)), SKILL_GENERATION_TRACE_CODES.READ_INPUT_INVALID, "Read input contains an unsupported field");
  return record.input.file_path;
}

function validateWriteInput(record) {
  requireAudit(
    exactKeys(record.input, ["file_path", "content"])
      && typeof record.input.file_path === "string"
      && typeof record.input.content === "string"
      && record.input.content.trim().length > 0,
    SKILL_GENERATION_TRACE_CODES.WRITE_INPUT_INVALID,
    "Write input must contain only non-empty string file_path and content fields",
  );
  return record.input.file_path;
}

function jsonLineColumn(text, offset) {
  if (!Number.isSafeInteger(offset) || offset < 0 || offset > text.length) return null;
  const prefix = text.slice(0, offset);
  const line = prefix.split("\n").length;
  const lastNewline = prefix.lastIndexOf("\n");
  return { offset, line, column: offset - lastNewline };
}

function jsonSyntaxLocation(text, error) {
  const message = typeof error?.message === "string" ? error.message : "";
  const position = /(?:at )?position\s+(\d+)/iu.exec(message);
  if (position) return jsonLineColumn(text, Number(position[1]));
  if (/unexpected end of json input/iu.test(message)) return jsonLineColumn(text, text.length);
  const reported = /line\s+(\d+)\s+column\s+(\d+)/iu.exec(message);
  if (!reported) return null;
  const line = Number(reported[1]);
  const column = Number(reported[2]);
  if (!Number.isSafeInteger(line) || line < 1 || !Number.isSafeInteger(column) || column < 1) return null;
  const lines = text.split("\n");
  if (line > lines.length || column > lines[line - 1].length + 1) return null;
  const offset = lines.slice(0, line - 1).reduce((total, value) => total + value.length + 1, 0) + column - 1;
  return { offset, line, column };
}

function writeJsonDiagnostic(bytes, text, kind, error = null) {
  const location = kind === "JSON_SYNTAX_ERROR" ? jsonSyntaxLocation(text, error) : null;
  return {
    schema_version: 1,
    kind,
    size_bytes: bytes.length,
    sha256: crypto.createHash("sha256").update(bytes).digest("hex"),
    offset: location?.offset ?? null,
    line: location?.line ?? null,
    column: location?.column ?? null,
  };
}

export function validSkillGenerationWriteJsonDiagnostic(value) {
  if (!exactKeys(value, ["schema_version", "kind", "size_bytes", "sha256", "offset", "line", "column"])
    || value.schema_version !== 1
    || !["JSON_SYNTAX_ERROR", "JSON_ROOT_NOT_OBJECT"].includes(value.kind)
    || !Number.isSafeInteger(value.size_bytes)
    || value.size_bytes <= 0
    || !/^[a-f0-9]{64}$/.test(value.sha256 ?? "")) return false;
  const locationMissing = value.offset === null && value.line === null && value.column === null;
  const locationPresent = Number.isSafeInteger(value.offset) && value.offset >= 0
    && value.offset <= value.size_bytes
    && Number.isSafeInteger(value.line) && value.line >= 1
    && value.line <= value.offset + 1
    && Number.isSafeInteger(value.column) && value.column >= 1
    && value.column <= value.offset + 1;
  return value.kind === "JSON_SYNTAX_ERROR" ? locationPresent : locationMissing;
}

function validReceiptPath(value, prefixes) {
  try {
    const normalized = portableRelativePath(value);
    return prefixes.some((prefix) => normalized.startsWith(prefix));
  } catch {
    return false;
  }
}

function validFailedTerminalSummary(value) {
  return exactKeys(value, ["subtype", "is_error"])
    && typeof value.subtype === "string"
    && /^[a-z][a-z0-9_]{0,63}$/.test(value.subtype)
    && typeof value.is_error === "boolean"
    && (value.subtype !== "success" || value.is_error !== false);
}

function validSanitizedStructuredOutputRecord(record) {
  const keys = record.outcome === "ERROR"
    ? ["ordinal", "tool", "outcome", "size_bytes", "sha256", "diagnostic"]
    : ["ordinal", "tool", "outcome", "size_bytes", "sha256"];
  return exactKeys(record, keys)
    && record.tool === "StructuredOutput"
    && ["SUCCESS", "ERROR", "PENDING"].includes(record.outcome)
    && Number.isSafeInteger(record.size_bytes)
    && record.size_bytes > 0
    && /^[a-f0-9]{64}$/.test(record.sha256 ?? "")
    && (record.outcome === "ERROR"
      ? validGenerationBlueprintSubmissionDiagnostic(record.diagnostic)
      : !Object.hasOwn(record, "diagnostic"));
}

export function validSkillGenerationIncompleteAuditRejectedReceipt(value) {
  return exactKeys(value, [
    "schema_version", "status", "workflow", "code", "audit_code", "stream_state", "stream",
  ])
    && value.schema_version === SKILL_GENERATION_TRACE_SCHEMA_VERSION
    && value.status === "FAIL"
    && value.workflow === "skill-generation"
    && value.code === SKILL_GENERATION_TRACE_CODES.INCOMPLETE_PREFIX_REJECTED
    && SKILL_GENERATION_INCOMPLETE_AUDIT_REJECTION_CODES.includes(value.audit_code)
    && value.stream_state === "TERMINAL_MISSING"
    && validTerminalMissingStream(value.stream, { allowError: true })
    && (value.stream.last_event_type !== "error"
      || value.audit_code === SKILL_GENERATION_TRACE_CODES.STREAM_ERROR);
}

export function buildSkillGenerationIncompleteAuditRejectedReceipt(auditCode, stream) {
  const receipt = {
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "FAIL",
    workflow: "skill-generation",
    code: SKILL_GENERATION_TRACE_CODES.INCOMPLETE_PREFIX_REJECTED,
    audit_code: auditCode,
    stream_state: "TERMINAL_MISSING",
    stream,
  };
  return validSkillGenerationIncompleteAuditRejectedReceipt(receipt) ? receipt : null;
}

function validTerminalMissingStream(stream, { allowError = false } = {}) {
  return exactKeys(stream, [
    "schema_version", "event_count", "parsed_event_count", "init_count",
    "result_count", "last_event_type", "complete",
  ])
    && stream.schema_version === 1
    && Number.isSafeInteger(stream.event_count)
    && stream.event_count > 0
    && stream.parsed_event_count === stream.event_count
    && stream.init_count === 1
    && stream.result_count === 0
    && validIsolatedAgentStreamEventType(stream.last_event_type)
    && stream.last_event_type !== "result"
    && (allowError || stream.last_event_type !== "error")
    && stream.complete === false;
}

export function validSkillGenerationIncompleteTraceAuditReceipt(value) {
  if (!exactKeys(value, [
    "schema_version", "status", "workflow", "code", "stream_state", "tool_sequence", "stream",
  ])
    || value.schema_version !== SKILL_GENERATION_TRACE_SCHEMA_VERSION
    || value.status !== "FAIL"
    || value.workflow !== "skill-generation"
    || ![
      SKILL_GENERATION_TRACE_CODES.INCOMPLETE_PREFIX,
      SKILL_GENERATION_TRACE_CODES.INCOMPLETE_PREFIX_SEQUENCE_INVALID,
    ].includes(value.code)
    || value.stream_state !== "TERMINAL_MISSING"
    || !Array.isArray(value.tool_sequence)
    || value.tool_sequence.length > 11) return false;

  const stream = value.stream;
  if (!validTerminalMissingStream(stream)) return false;

  const records = value.tool_sequence;
  const sequenceViolation = records.length === 11;
  if (value.code !== (sequenceViolation
    ? SKILL_GENERATION_TRACE_CODES.INCOMPLETE_PREFIX_SEQUENCE_INVALID
    : SKILL_GENERATION_TRACE_CODES.INCOMPLETE_PREFIX)) return false;
  if (records.length === 2) return false;

  for (const [ordinal, record] of records.entries()) {
    if (record?.ordinal !== ordinal || !["SUCCESS", "ERROR", "PENDING"].includes(record.outcome)) return false;
    if (ordinal === 0) {
      if (!exactKeys(record, ["ordinal", "tool", "outcome"]) || record.tool !== "Skill") return false;
      continue;
    }
    if (ordinal <= 8) {
      if (!exactKeys(record, ["ordinal", "tool", "outcome", "path"])
        || record.tool !== "Read"
        || record.path !== SKILL_GENERATION_PHASE_REQUIRED_RECEIPT_READS[ordinal - 1]) return false;
      continue;
    }
    if (!validSanitizedStructuredOutputRecord(record)) return false;
  }

  const pending = records.filter((record) => record.outcome === "PENDING");
  if (pending.length > 2) return false;
  if (pending.length === 2) {
    if (records.length !== 3 || pending[0].ordinal !== 1 || pending[1].ordinal !== 2) return false;
  } else if (pending.length === 1 && pending[0].ordinal !== records.length - 1) return false;

  if (records.length === 0) return true;
  if (records.length === 1) return true;
  if (records.length === 3) return records[0].outcome === "SUCCESS";
  if (sequenceViolation) {
    return records.slice(0, 9).every((record) => record.outcome === "SUCCESS")
      && ["SUCCESS", "ERROR"].includes(records[9].outcome);
  }
  return records.slice(0, -1).every((record) => record.outcome === "SUCCESS");
}

export function validSkillGenerationPartialTraceAuditReceipt(value) {
  if (!exactKeys(value, [
    "schema_version", "status", "workflow", "code", "tool_sequence", "terminal",
  ])
    || value.schema_version !== SKILL_GENERATION_TRACE_SCHEMA_VERSION
    || value.status !== "FAIL"
    || value.workflow !== "skill-generation"
    || value.code !== SKILL_GENERATION_TRACE_CODES.RESULT_NOT_SUCCESS
    || !Array.isArray(value.tool_sequence)
    || !validFailedTerminalSummary(value.terminal)) return false;
  return value.tool_sequence.every((record, ordinal) => {
    if (record?.ordinal !== ordinal
      || !SKILL_GENERATION_PHASE_ALLOWED_TOOLS.includes(record?.tool)
      || !["SUCCESS", "ERROR"].includes(record?.outcome)) return false;
    if (record.tool === "Read") {
      return exactKeys(record, ["ordinal", "tool", "outcome", "path"])
        && SKILL_GENERATION_PHASE_REQUIRED_RECEIPT_READS.includes(record.path);
    }
    if (record.tool === "StructuredOutput") {
      return validSanitizedStructuredOutputRecord(record);
    }
    return exactKeys(record, ["ordinal", "tool", "outcome"]);
  });
}

export function validSkillGenerationFailedTraceAuditReceipt(value) {
  if (validSkillGenerationPartialTraceAuditReceipt(value)) return true;
  if (validSkillGenerationIncompleteTraceAuditReceipt(value)) return true;
  if (validSkillGenerationIncompleteAuditRejectedReceipt(value)) return true;
  const failedBase = value?.schema_version === SKILL_GENERATION_TRACE_SCHEMA_VERSION
    && value.status === "FAIL"
    && value.workflow === "skill-generation"
    && Object.values(SKILL_GENERATION_TRACE_CODES).includes(value.code)
    && ![
      SKILL_GENERATION_TRACE_CODES.RESULT_NOT_SUCCESS,
      SKILL_GENERATION_TRACE_CODES.INCOMPLETE_PREFIX,
      SKILL_GENERATION_TRACE_CODES.INCOMPLETE_PREFIX_SEQUENCE_INVALID,
      SKILL_GENERATION_TRACE_CODES.INCOMPLETE_PREFIX_REJECTED,
    ].includes(value.code);
  if (!failedBase) return false;
  if (value.code === SKILL_GENERATION_TRACE_CODES.WRITE_JSON_INVALID) {
    return exactKeys(value, ["schema_version", "status", "workflow", "code", "diagnostic"])
      && validSkillGenerationWriteJsonDiagnostic(value.diagnostic);
  }
  if (value.code === SKILL_GENERATION_TRACE_CODES.RULE_IR_INVALID) {
    return exactKeys(value, ["schema_version", "status", "workflow", "code", "diagnostic"])
      && validSkillGenerationRuleIrDiagnostic(value.diagnostic);
  }
  return exactKeys(value, ["schema_version", "status", "workflow", "code"]);
}

export function validSkillGenerationTraceAuditReceipt(value) {
  if (!exactKeys(value, [
    "schema_version", "status", "workflow", "skill", "tool_inventory", "permission_mode",
    "permission_policy_sha256", "attempt_policy", "attempt_policy_sha256", "tool_sequence",
    "accepted_validation_rejections", "required_reads", "observed_reads", "linked_references",
    "ir_input", "compiler", "output", "terminal",
  ])) return false;
  const phasePolicy = JSON.stringify(value?.attempt_policy) === JSON.stringify(SKILL_GENERATION_TOOL_ATTEMPT_POLICY);
  const attemptPolicy = phasePolicy ? SKILL_GENERATION_TOOL_ATTEMPT_POLICY : null;
  if (value.schema_version !== SKILL_GENERATION_TRACE_SCHEMA_VERSION
    || value.status !== "PASS"
    || value.workflow !== "skill-generation"
    || value.skill !== "wiki-to-diagnosis-skill"
    || value.permission_mode !== "dontAsk"
    || JSON.stringify(value.tool_inventory) !== JSON.stringify(SKILL_GENERATION_PHASE_ALLOWED_TOOLS)
    || !/^[a-f0-9]{64}$/.test(value.permission_policy_sha256 ?? "")
    || attemptPolicy === null
    || value.attempt_policy_sha256 !== crypto.createHash("sha256").update(JSON.stringify(attemptPolicy)).digest("hex")) return false;
  if (!Array.isArray(value.tool_sequence)
    || value.tool_sequence.length === 0
    || !Array.isArray(value.accepted_validation_rejections)
    || !Array.isArray(value.required_reads)
    || value.required_reads.length === 0
    || !Array.isArray(value.observed_reads)
    || !Array.isArray(value.linked_references)) return false;
  if (!value.tool_sequence.every((record, ordinal) => record?.ordinal === ordinal)) return false;
  if (!value.tool_sequence.every((record) => SKILL_GENERATION_PHASE_ALLOWED_TOOLS.includes(record?.tool))) return false;

  const skillRecords = value.tool_sequence.filter((record) => record?.tool === "Skill");
  const readRecords = value.tool_sequence.filter((record) => record?.tool === "Read");
  const structuredOutputRecords = value.tool_sequence.filter((record) => record?.tool === "StructuredOutput");
  if (skillRecords.length !== 1
    || !exactKeys(skillRecords[0], ["ordinal", "tool", "outcome"])
    || skillRecords[0].ordinal !== 0
    || skillRecords[0].outcome !== "SUCCESS"
    || structuredOutputRecords.length !== 1
    || structuredOutputRecords[0].ordinal !== 9
    || structuredOutputRecords[0].ordinal !== value.tool_sequence.length - 1
    || !exactKeys(structuredOutputRecords[0], ["ordinal", "tool", "outcome"])
    || structuredOutputRecords[0].outcome !== "SUCCESS") return false;
  if (!readRecords.every((record) => exactKeys(record, ["ordinal", "tool", "outcome", "path"])
    && record.outcome === "SUCCESS"
    && validReceiptPath(record.path, ["workspace/inputs/", "skill/references/"]))) return false;
  if (value.accepted_validation_rejections.length !== 0) return false;
  const expectedRequiredReads = SKILL_GENERATION_PHASE_REQUIRED_RECEIPT_READS;
  const expectedLinkedReferences = [...PHASE_REQUIRED_REFERENCES].sort().map((relative) => `skill/${relative}`);
  if (JSON.stringify(value.required_reads) !== JSON.stringify(expectedRequiredReads)
    || !value.required_reads.every((readPath) => validReceiptPath(readPath, ["workspace/inputs/", "skill/references/"]))
    || new Set(value.linked_references).size !== value.linked_references.length
    || !value.linked_references.every((readPath) => validReceiptPath(readPath, ["skill/references/"]))
    || !DEFAULT_REQUIRED_RECEIPT_READS.filter((readPath) => readPath.startsWith("skill/")).every((readPath) => value.linked_references.includes(readPath))
    || !readRecords.filter((record) => record.path.startsWith("skill/")).every((record) => value.linked_references.includes(record.path))) return false;
  const expectedSequence = [
    { ordinal: 0, tool: "Skill", outcome: "SUCCESS" },
    ...SKILL_GENERATION_PHASE_REQUIRED_RECEIPT_READS.map((readPath, index) => ({
      ordinal: index + 1,
      tool: "Read",
      outcome: "SUCCESS",
      path: readPath,
    })),
    { ordinal: 9, tool: "StructuredOutput", outcome: "SUCCESS" },
  ];
  if (JSON.stringify(value.tool_sequence) !== JSON.stringify(expectedSequence)
    || JSON.stringify(value.linked_references) !== JSON.stringify(expectedLinkedReferences)) return false;
  const expectedObservedReads = readRecords.map((record) => ({ ordinal: record.ordinal, path: record.path }));
  if (JSON.stringify(value.observed_reads) !== JSON.stringify(expectedObservedReads)
    || !value.observed_reads.every((record) => exactKeys(record, ["ordinal", "path"])
      && Number.isSafeInteger(record.ordinal)
      && record.ordinal > 0
      && record.ordinal < structuredOutputRecords[0].ordinal)) return false;
  if (!value.required_reads.every((required) => value.observed_reads.some((record) => record.path === required))) return false;
  if (!exactKeys(value.ir_input, ["ordinal", "size_bytes", "sha256"])
    || value.ir_input.ordinal !== structuredOutputRecords[0].ordinal
    || !Number.isSafeInteger(value.ir_input.size_bytes)
    || value.ir_input.size_bytes <= 0
    || value.ir_input.size_bytes > SKILL_GENERATION_RULE_IR.max_canonical_bytes
    || !/^[a-f0-9]{64}$/.test(value.ir_input.sha256 ?? "")
    || !validSkillGenerationCompilerReceipt(value.compiler)) return false;
  if (!exactKeys(value.output, ["ordinal", "path", "size_bytes", "sha256"])
    || value.output.ordinal !== structuredOutputRecords[0].ordinal
    || value.output.path !== "workspace/output/generation-spec.json"
    || !Number.isSafeInteger(value.output.size_bytes)
    || value.output.size_bytes <= 0
    || !/^[a-f0-9]{64}$/.test(value.output.sha256 ?? "")) return false;
  return exactKeys(value.terminal, ["subtype", "is_error"])
    && value.terminal.subtype === "success"
    && value.terminal.is_error === false;
}

/**
 * Seals a terminal-less production stream prefix without retaining model
 * text, raw tool results, tool inputs, or absolute paths. A rejected prefix
 * throws a fixed-code SkillGenerationTraceAuditError so the wrapper can seal
 * only that code and the canonical stream summary, never a partially trusted
 * tool sequence or the exception message/details.
 */
export function auditIncompleteSkillGenerationTrace({
  events,
  workspaceRoot,
  skillRoot,
  sourceRoot = null,
}) {
  requireAudit(Array.isArray(events) && events.length > 0 && events.every(isPlainObject), SKILL_GENERATION_TRACE_CODES.EVENTS_INVALID, "events must be a non-empty array of stream-json objects");
  requireAudit(!events.some((event) => event.type === "error"), SKILL_GENERATION_TRACE_CODES.STREAM_ERROR, "The stream contains an explicit error event");
  requireAudit(!events.some((event) => event.type === "result"), SKILL_GENERATION_TRACE_CODES.RESULT_INVALID, "An incomplete prefix must not contain a terminal result event");
  requireAudit(events.every((event) => !["assistant", "user"].includes(event.type) || (
    isPlainObject(event.message)
      && event.message.role === event.type
      && Array.isArray(event.message.content)
  )), SKILL_GENERATION_TRACE_CODES.TOOL_EVENT_INVALID, "Assistant and user prefix events must contain role-matched content arrays");
  inspectRoot(workspaceRoot, "Workspace root");
  inspectRoot(skillRoot, "Skill root");
  if (sourceRoot !== null) {
    inspectRoot(sourceRoot, "Source root");
    requireAudit(!pathInside(sourceRoot, workspaceRoot) && !pathInside(workspaceRoot, sourceRoot), SKILL_GENERATION_TRACE_CODES.ROOT_INVALID, "The audited workspace must be outside the source repository");
  }
  const linkedReferences = discoverLinkedSkillReferences(skillRoot);
  requireAudit(isSkillGenerationPhaseCheckpointMode(linkedReferences), SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "Incomplete traces are available only for the production checkpoint workflow");

  const initEvents = events.filter((event) => event.type === "system" && event.subtype === "init");
  requireAudit(initEvents.length === 1 && events[0] === initEvents[0], SKILL_GENERATION_TRACE_CODES.INIT_INVALID, "The prefix must begin with exactly one init event");
  const init = initEvents[0];
  requireAudit(typeof init.cwd === "string" && path.resolve(init.cwd) === path.resolve(workspaceRoot), SKILL_GENERATION_TRACE_CODES.INIT_CWD_MISMATCH, "The init cwd must equal the audited workspace");
  requireAudit(init.permissionMode === "dontAsk", SKILL_GENERATION_TRACE_CODES.PERMISSION_MODE_INVALID, "The effective permission mode must be dontAsk");
  requireAudit(Array.isArray(init.tools)
    && init.tools.length === SKILL_GENERATION_PHASE_ALLOWED_TOOLS.length
    && new Set(init.tools).size === SKILL_GENERATION_PHASE_ALLOWED_TOOLS.length
    && SKILL_GENERATION_PHASE_ALLOWED_TOOLS.every((tool) => init.tools.includes(tool)), SKILL_GENERATION_TRACE_CODES.TOOL_INVENTORY_INVALID, "The incomplete prefix must use the production tool inventory");

  const records = parseToolRecords(events, { allowPending: true });
  requireAudit(records.length <= 11 && records.length !== 2, SKILL_GENERATION_TRACE_CODES.PHASE_SEQUENCE_INVALID, "The incomplete prefix has an impossible production length");
  const expectedTools = [
    "Skill",
    ...SKILL_GENERATION_PHASE_REQUIRED_RECEIPT_READS.map(() => "Read"),
    "StructuredOutput",
    "StructuredOutput",
  ];
  requireAudit(records.every((record, ordinal) => record.ordinal === ordinal && record.tool === expectedTools[ordinal]), SKILL_GENERATION_TRACE_CODES.PHASE_SEQUENCE_INVALID, "The incomplete prefix does not follow the production tool order");

  const pending = records.filter((record) => record.result === null);
  requireAudit(pending.length <= 2, SKILL_GENERATION_TRACE_CODES.TOOL_RESULT_MISSING, "The incomplete prefix contains too many pending tools");
  if (pending.length === 2) {
    requireAudit(records.length === 3 && pending[0] === records[1] && pending[1] === records[2], SKILL_GENERATION_TRACE_CODES.PHASE_SEQUENCE_INVALID, "Only the exact initial Read batch may contain two pending tools");
  } else if (pending.length === 1) {
    requireAudit(pending[0] === records.at(-1), SKILL_GENERATION_TRACE_CODES.PHASE_SEQUENCE_INVALID, "A single pending tool must be the final observed tool");
  }
  requireAudit(records.every((record) => record.result === null || record.result.contradictory_success === false), SKILL_GENERATION_TRACE_CODES.TOOL_RESULT_ERROR, "A contradictory tool result cannot enter an incomplete receipt");

  if (records.length >= 1) {
    const skill = records[0];
    requireAudit(exactKeys(skill.input, ["skill"]) && skill.input.skill === "wiki-to-diagnosis-skill", SKILL_GENERATION_TRACE_CODES.SKILL_INVOCATION_INVALID, "The prefix must begin with the exact Skill invocation");
    if (skill.result?.outcome === "SUCCESS") {
      requireAudit(isPlainObject(skill.result.raw) && skill.result.raw.success === true, SKILL_GENERATION_TRACE_CODES.SKILL_RESULT_INVALID, "A completed Skill result must explicitly report success");
    }
  }
  if (records.length >= 3) {
    const batch = records.slice(1, 3);
    const batchContent = events[batch[0].use_event_index]?.message?.content;
    requireAudit(records[0].result?.outcome === "SUCCESS"
      && records[0].result_event_index < batch[0].use_event_index
      && batch[0].use_event_index === batch[1].use_event_index
      && Array.isArray(batchContent)
      && batchContent.length === 2
      && batchContent[0]?.type === "tool_use"
      && batchContent[0]?.id === batch[0].id
      && batchContent[1]?.type === "tool_use"
      && batchContent[1]?.id === batch[1].id, SKILL_GENERATION_TRACE_CODES.PHASE_SEQUENCE_INVALID, "The initial inputs must be the exact ordered two-Read batch");
  }
  for (let ordinal = 3; ordinal < records.length; ordinal += 1) {
    const current = records[ordinal];
    const useContent = events[current.use_event_index]?.message?.content;
    const useBlocks = Array.isArray(useContent) ? useContent.filter((block) => block?.type === "tool_use") : [];
    requireAudit(useBlocks.length === 1 && useBlocks[0].id === current.id, SKILL_GENERATION_TRACE_CODES.PHASE_SEQUENCE_INVALID, "Every post-batch tool must be dispatched serially");
    const predecessors = ordinal === 3 ? records.slice(1, 3) : [records[ordinal - 1]];
    const predecessorsReady = ordinal === 10
      ? predecessors.every((record) => record.result !== null && record.result_event_index < current.use_event_index)
      : predecessors.every((record) => record.result?.outcome === "SUCCESS" && record.result_event_index < current.use_event_index);
    requireAudit(predecessorsReady, SKILL_GENERATION_TRACE_CODES.PHASE_SEQUENCE_INVALID, "Every production barrier must complete before the next tool");
  }

  const toolSequence = records.map((record) => {
    const base = {
      ordinal: record.ordinal,
      tool: record.tool,
      outcome: record.result?.outcome ?? "PENDING",
    };
    if (record.tool === "Read") {
      requireAudit(exactKeys(record.input, ["file_path"]), SKILL_GENERATION_TRACE_CODES.READ_INPUT_INVALID, "Production Read input must contain only file_path");
      const normalized = observedPath(record.input.file_path, {
        workspaceRoot,
        skillRoot,
        linkedReferences,
        mode: "read",
      });
      requireAudit(normalized.receiptPath === SKILL_GENERATION_PHASE_REQUIRED_RECEIPT_READS[record.ordinal - 1], SKILL_GENERATION_TRACE_CODES.PHASE_SEQUENCE_INVALID, "The production Read path is out of order");
      return { ...base, path: normalized.receiptPath };
    }
    if (record.tool === "StructuredOutput") {
      requireAudit(isPlainObject(record.input), SKILL_GENERATION_TRACE_CODES.STRUCTURED_OUTPUT_INPUT_INVALID, "StructuredOutput input must be a root object");
      const inputBytes = Buffer.from(canonicalJson(record.input), "utf8");
      return {
        ...base,
        size_bytes: inputBytes.length,
        sha256: sha256Bytes(inputBytes),
        ...(base.outcome === "ERROR" ? {
          diagnostic: buildGenerationBlueprintSubmissionDiagnostic(record.input),
        } : {}),
      };
    }
    return base;
  });
  const lastEventType = events.at(-1)?.type;
  requireAudit(validIsolatedAgentStreamEventType(lastEventType), SKILL_GENERATION_TRACE_CODES.EVENTS_INVALID, "The prefix last event type is not a frozen CLI event type");
  const receipt = {
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "FAIL",
    workflow: "skill-generation",
    code: records.length === 11
      ? SKILL_GENERATION_TRACE_CODES.INCOMPLETE_PREFIX_SEQUENCE_INVALID
      : SKILL_GENERATION_TRACE_CODES.INCOMPLETE_PREFIX,
    stream_state: "TERMINAL_MISSING",
    tool_sequence: toolSequence,
    stream: {
      schema_version: 1,
      event_count: events.length,
      parsed_event_count: events.length,
      init_count: 1,
      result_count: 0,
      last_event_type: lastEventType,
      complete: false,
    },
  };
  requireAudit(validSkillGenerationIncompleteTraceAuditReceipt(receipt), SKILL_GENERATION_TRACE_CODES.EVENTS_INVALID, "The incomplete trace receipt is invalid");
  return receipt;
}

/**
 * Produces a content-free diagnostic receipt for a structurally complete but
 * unsuccessful production Skill-generation stream. This receipt is never a
 * public PASS receipt. Any unsafe path, malformed tool record, or non-phase
 * tool causes the caller to fail closed instead of retaining raw model data.
 */
export function auditPartialSkillGenerationTrace({
  events,
  workspaceRoot,
  skillRoot,
  sourceRoot = null,
}) {
  requireAudit(Array.isArray(events) && events.length > 0 && events.every(isPlainObject), SKILL_GENERATION_TRACE_CODES.EVENTS_INVALID, "events must be a non-empty array of stream-json objects");
  requireAudit(!events.some((event) => event.type === "error"), SKILL_GENERATION_TRACE_CODES.STREAM_ERROR, "The stream contains an explicit error event");
  inspectRoot(workspaceRoot, "Workspace root");
  inspectRoot(skillRoot, "Skill root");
  if (sourceRoot !== null) {
    inspectRoot(sourceRoot, "Source root");
    requireAudit(!pathInside(sourceRoot, workspaceRoot) && !pathInside(workspaceRoot, sourceRoot), SKILL_GENERATION_TRACE_CODES.ROOT_INVALID, "The audited workspace must be outside the source repository");
  }
  const linkedReferences = discoverLinkedSkillReferences(skillRoot);
  requireAudit(isSkillGenerationPhaseCheckpointMode(linkedReferences), SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID, "Partial traces are available only for the production checkpoint workflow");

  const initEvents = events.filter((event) => event.type === "system" && event.subtype === "init");
  requireAudit(initEvents.length === 1 && events[0] === initEvents[0], SKILL_GENERATION_TRACE_CODES.INIT_INVALID, "The trace must begin with exactly one init event");
  const init = initEvents[0];
  requireAudit(typeof init.cwd === "string" && path.resolve(init.cwd) === path.resolve(workspaceRoot), SKILL_GENERATION_TRACE_CODES.INIT_CWD_MISMATCH, "The init cwd must equal the audited workspace");
  requireAudit(init.permissionMode === "dontAsk", SKILL_GENERATION_TRACE_CODES.PERMISSION_MODE_INVALID, "The effective permission mode must be dontAsk");
  requireAudit(Array.isArray(init.tools)
    && init.tools.length === SKILL_GENERATION_PHASE_ALLOWED_TOOLS.length
    && new Set(init.tools).size === SKILL_GENERATION_PHASE_ALLOWED_TOOLS.length
    && SKILL_GENERATION_PHASE_ALLOWED_TOOLS.every((tool) => init.tools.includes(tool)), SKILL_GENERATION_TRACE_CODES.TOOL_INVENTORY_INVALID, "The partial trace must use the production tool inventory");

  const terminalEvents = events.filter((event) => event.type === "result");
  requireAudit(terminalEvents.length === 1 && events.at(-1) === terminalEvents[0], SKILL_GENERATION_TRACE_CODES.RESULT_INVALID, "The trace must end with exactly one result event");
  const terminal = terminalEvents[0];
  const terminalSummary = { subtype: terminal.subtype, is_error: terminal.is_error };
  requireAudit(validFailedTerminalSummary(terminalSummary), SKILL_GENERATION_TRACE_CODES.RESULT_NOT_SUCCESS, "The partial trace requires an unsuccessful terminal result");

  const records = parseToolRecords(events);
  requireAudit(records.every((record) => SKILL_GENERATION_PHASE_ALLOWED_TOOLS.includes(record.tool)), SKILL_GENERATION_TRACE_CODES.TOOL_NOT_ALLOWED, "The partial trace contains a non-production tool");
  const toolSequence = records.map((record) => {
    const base = {
      ordinal: record.ordinal,
      tool: record.tool,
      outcome: record.result.outcome,
    };
    if (record.tool === "Read") {
      const normalized = observedPath(validateReadInput(record), {
        workspaceRoot,
        skillRoot,
        linkedReferences,
        mode: "read",
      });
      requireAudit(SKILL_GENERATION_PHASE_REQUIRED_RECEIPT_READS.includes(normalized.receiptPath), SKILL_GENERATION_TRACE_CODES.READ_UNLINKED, "The partial trace Read is outside the production allowlist");
      return { ...base, path: normalized.receiptPath };
    }
    if (record.tool === "StructuredOutput") {
      requireAudit(isPlainObject(record.input), SKILL_GENERATION_TRACE_CODES.STRUCTURED_OUTPUT_INPUT_INVALID, "StructuredOutput input must be a root object");
      const inputBytes = Buffer.from(canonicalJson(record.input), "utf8");
      return {
        ...base,
        size_bytes: inputBytes.length,
        sha256: sha256Bytes(inputBytes),
        ...(base.outcome === "ERROR" ? {
          diagnostic: buildGenerationBlueprintSubmissionDiagnostic(record.input),
        } : {}),
      };
    }
    return base;
  });
  const receipt = {
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "FAIL",
    workflow: "skill-generation",
    code: SKILL_GENERATION_TRACE_CODES.RESULT_NOT_SUCCESS,
    tool_sequence: toolSequence,
    terminal: terminalSummary,
  };
  requireAudit(validSkillGenerationPartialTraceAuditReceipt(receipt), SKILL_GENERATION_TRACE_CODES.EVENTS_INVALID, "The partial trace receipt is invalid");
  return receipt;
}

/**
 * Audits a completed Claude stream-json trace for the isolated Wiki conversion
 * workflow. requiredReferencePaths are relative to skillRoot. All tool paths
 * and every path returned in the receipt are relative to workspaceRoot.
 */
export function auditSkillGenerationTrace({
  events,
  workspaceRoot,
  skillRoot,
  sourceRoot = null,
  requiredReferencePaths = null,
  compilation = null,
}) {
  requireAudit(Array.isArray(events) && events.length > 0 && events.every(isPlainObject), SKILL_GENERATION_TRACE_CODES.EVENTS_INVALID, "events must be a non-empty array of stream-json objects");
  requireAudit(!events.some((event) => event.type === "error"), SKILL_GENERATION_TRACE_CODES.STREAM_ERROR, "The stream contains an explicit error event");
  inspectRoot(workspaceRoot, "Workspace root");
  inspectRoot(skillRoot, "Skill root");
  if (sourceRoot !== null) {
    inspectRoot(sourceRoot, "Source root");
    requireAudit(!pathInside(sourceRoot, workspaceRoot) && !pathInside(workspaceRoot, sourceRoot), SKILL_GENERATION_TRACE_CODES.ROOT_INVALID, "The audited workspace must be outside the source repository");
  }
  const linkedReferences = discoverLinkedSkillReferences(skillRoot);
  const phaseMode = isSkillGenerationPhaseCheckpointMode(linkedReferences);
  const allowedTools = phaseMode ? SKILL_GENERATION_PHASE_ALLOWED_TOOLS : LEGACY_ALLOWED_TOOLS;

  const initEvents = events.filter((event) => event.type === "system" && event.subtype === "init");
  requireAudit(initEvents.length === 1 && events[0] === initEvents[0], SKILL_GENERATION_TRACE_CODES.INIT_INVALID, "The trace must begin with exactly one init event");
  const init = initEvents[0];
  requireAudit(typeof init.cwd === "string" && path.resolve(init.cwd) === path.resolve(workspaceRoot), SKILL_GENERATION_TRACE_CODES.INIT_CWD_MISMATCH, "The init cwd must equal the audited workspace");
  requireAudit(init.permissionMode === "dontAsk", SKILL_GENERATION_TRACE_CODES.PERMISSION_MODE_INVALID, "The effective permission mode must be dontAsk");
  requireAudit(Array.isArray(init.tools) && init.tools.length === allowedTools.length && new Set(init.tools).size === allowedTools.length && allowedTools.every((tool) => init.tools.includes(tool)), SKILL_GENERATION_TRACE_CODES.TOOL_INVENTORY_INVALID, `The init tool inventory must contain only ${allowedTools.join(", ")}`);

  const terminalEvents = events.filter((event) => event.type === "result");
  requireAudit(terminalEvents.length === 1 && events.at(-1) === terminalEvents[0], SKILL_GENERATION_TRACE_CODES.RESULT_INVALID, "The trace must end with exactly one result event");
  const terminal = terminalEvents[0];
  requireAudit(terminal.subtype === "success" && terminal.is_error === false, SKILL_GENERATION_TRACE_CODES.RESULT_NOT_SUCCESS, "The terminal result must report success");
  requireAudit(terminal.result === "DONE", SKILL_GENERATION_TRACE_CODES.RESULT_NOT_SUCCESS, "The successful terminal result must be exactly DONE");

  const links = new Set(linkedReferences);
  const requiredReferences = normalizeRequiredReferences(
    requiredReferencePaths ?? (phaseMode ? PHASE_REQUIRED_REFERENCES : DEFAULT_REQUIRED_REFERENCES),
    links,
  );
  if (phaseMode) {
    requireAudit(
      requiredReferences.join("\0") === PHASE_REQUIRED_REFERENCES.join("\0"),
      SKILL_GENERATION_TRACE_CODES.REQUIRED_REFERENCE_INVALID,
      "Checkpoint mode cannot omit, reorder or add required references",
    );
  }
  const requiredReads = [
    ...REQUIRED_INPUT_PATHS.map((relative) => `workspace/${relative}`),
    ...requiredReferences.map((relative) => `skill/${relative}`),
  ];

  for (const inputPath of REQUIRED_INPUT_PATHS) inspectRegularFile(workspaceRoot, inputPath);
  const records = parseToolRecords(events);
  const attemptPolicy = phaseMode
    ? SKILL_GENERATION_TOOL_ATTEMPT_POLICY
    : LEGACY_SKILL_GENERATION_TOOL_ATTEMPT_POLICY;
  const failedRecords = records.filter((record) => record.result.outcome === "ERROR");
  const acceptedValidationRejections = failedRecords.filter(emptyWriteValidationRejection);
  requireAudit(
    failedRecords.length === acceptedValidationRejections.length,
    SKILL_GENERATION_TRACE_CODES.TOOL_RESULT_ERROR,
    phaseMode
      ? "Checkpoint mode rejects every failed tool invocation"
      : "Only one explicitly failed, strictly empty Write input may be treated as a non-mutating validation rejection",
  );
  requireAudit(
    acceptedValidationRejections.length <= attemptPolicy.max_empty_write_rejections,
    SKILL_GENERATION_TRACE_CODES.WRITE_COUNT_INVALID,
    phaseMode
      ? "Checkpoint mode does not allow an empty Write validation rejection"
      : "At most one strictly empty Write validation rejection is allowed",
  );
  validateSkillInvocation(records);

  const reads = [];
  for (const record of records.filter((candidate) => candidate.tool === "Read")) {
    const toolPath = validateReadInput(record);
    const normalized = observedPath(toolPath, { workspaceRoot, skillRoot, linkedReferences, mode: "read" });
    if (requiredReads.includes(normalized.receiptPath)) {
      requireAudit(!Object.hasOwn(record.input, "offset") && !Object.hasOwn(record.input, "limit") && !Object.hasOwn(record.input, "pages"), SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_PARTIAL, "Every required input and contract reference must be read in full", { path: normalized.receiptPath });
    }
    reads.push({ ordinal: record.ordinal, path: normalized.receiptPath, result_event_index: record.result_event_index });
  }
  const observedReadPaths = new Set(reads.map((read) => read.path));
  const missingReads = requiredReads.filter((relative) => !observedReadPaths.has(relative));
  requireAudit(missingReads.length === 0, SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_MISSING, "The trace did not read every required input and Skill reference", { paths: missingReads });

  let finalRecord;
  let outputSeal;
  let irInputSeal = null;
  let compilerReceipt = null;
  const outputPath = "workspace/output/generation-spec.json";
  if (phaseMode) {
    const writes = records.filter((candidate) => candidate.tool === "Write");
    requireAudit(writes.length === 0, SKILL_GENERATION_TRACE_CODES.TOOL_NOT_ALLOWED, "Checkpoint mode never permits Write");
    const structuredOutputs = records.filter((candidate) => candidate.tool === "StructuredOutput");
    const successfulStructuredOutputs = structuredOutputs.filter((record) => record.result.outcome === "SUCCESS");
    requireAudit(structuredOutputs.length === 1 && successfulStructuredOutputs.length === 1, SKILL_GENERATION_TRACE_CODES.STRUCTURED_OUTPUT_COUNT_INVALID, "The trace must contain exactly one successful StructuredOutput invocation");
    finalRecord = successfulStructuredOutputs[0];
    const expectedTools = ["Skill", ...requiredReads.map(() => "Read"), "StructuredOutput"];
    const initialBatchContent = events[records[1]?.use_event_index]?.message?.content;
    requireAudit(
      records.length === expectedTools.length
        && records.every((record, index) => record.tool === expectedTools[index])
        && reads.length === requiredReads.length
        && reads.every((read, index) => read.ordinal === index + 1 && read.path === requiredReads[index])
        && records.slice(1, -1).every((record) => exactKeys(record.input, ["file_path"]))
        && records[0].result_event_index < records[1].use_event_index
        && records[1].use_event_index === records[2].use_event_index
        && Array.isArray(initialBatchContent)
        && initialBatchContent.length === 2
        && initialBatchContent[0]?.type === "tool_use"
        && initialBatchContent[0]?.id === records[1].id
        && initialBatchContent[1]?.type === "tool_use"
        && initialBatchContent[1]?.id === records[2].id
        && records[1].result_event_index < records[3].use_event_index
        && records[2].result_event_index < records[3].use_event_index
        && records.slice(3, -1).every((record, index) => record.result_event_index < records[index + 4].use_event_index)
        && finalRecord.ordinal === 9
        && finalRecord.result_event_index < events.length - 1
        && records[8].tool === "Read"
        && reads[7].path === `skill/${SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES[3]}`,
      SKILL_GENERATION_TRACE_CODES.PHASE_SEQUENCE_INVALID,
      "Checkpoint mode requires the exact serialized Skill, authority Read, phase checkpoint and StructuredOutput sequence",
    );
    requireAudit(isPlainObject(finalRecord.input), SKILL_GENERATION_TRACE_CODES.STRUCTURED_OUTPUT_INPUT_INVALID, "StructuredOutput input must be the GenerationBlueprint IR root object");
    requireAudit(isPlainObject(terminal.structured_output), SKILL_GENERATION_TRACE_CODES.STRUCTURED_OUTPUT_INPUT_INVALID, "The terminal structured_output must be an IR root object");
    requireAudit(validGenerationBlueprintSubmission(finalRecord.input), SKILL_GENERATION_TRACE_CODES.STRUCTURED_OUTPUT_SCHEMA_INVALID, "StructuredOutput input must satisfy the frozen GenerationBlueprint submission schema");
    requireAudit(validGenerationBlueprintSubmission(terminal.structured_output), SKILL_GENERATION_TRACE_CODES.STRUCTURED_OUTPUT_SCHEMA_INVALID, "The terminal structured_output must satisfy the frozen GenerationBlueprint submission schema");
    requireAudit(canonicalJson(finalRecord.input) === canonicalJson(terminal.structured_output), SKILL_GENERATION_TRACE_CODES.STRUCTURED_OUTPUT_MISMATCH, "The terminal structured_output must equal the StructuredOutput tool input");
    const irBytes = Buffer.from(canonicalJson(finalRecord.input), "utf8");
    requireAudit(irBytes.length <= SKILL_GENERATION_RULE_IR.max_canonical_bytes, SKILL_GENERATION_TRACE_CODES.RULE_IR_INVALID, "The GenerationBlueprint IR exceeds its canonical byte limit");
    const validatedCompilation = validatedRuleIrCompilation(compilation, irBytes);
    irInputSeal = {
      ordinal: finalRecord.ordinal,
      ...validatedCompilation.ir,
    };
    compilerReceipt = validatedCompilation.compiler;
    outputSeal = {
      ordinal: finalRecord.ordinal,
      path: outputPath,
      ...validatedCompilation.output,
    };
  } else {
    requireAudit(compilation === null, SKILL_GENERATION_TRACE_CODES.RULE_IR_INVALID, "Legacy audit cannot accept an IR compilation envelope");
    const writes = records.filter((candidate) => candidate.tool === "Write");
    const successfulWrites = writes.filter((record) => record.result.outcome === "SUCCESS");
    requireAudit(successfulWrites.length === 1, SKILL_GENERATION_TRACE_CODES.WRITE_COUNT_INVALID, "The trace must contain exactly one successful Write invocation");
    finalRecord = successfulWrites[0];
    requireAudit(finalRecord.ordinal === records.length - 1, SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_ORDER_INVALID, "The successful Write must be the final tool invocation");
    for (const requiredRead of requiredReads) {
      requireAudit(reads.some((read) => read.path === requiredRead && read.result_event_index < finalRecord.use_event_index), SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_ORDER_INVALID, "Every required Read must finish before the successful Write starts", { path: requiredRead });
    }
    if (acceptedValidationRejections.length === 1) {
      const rejectedWrite = acceptedValidationRejections[0];
      requireAudit(rejectedWrite.ordinal === finalRecord.ordinal - 1, SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_ORDER_INVALID, "The empty Write validation rejection must immediately precede the successful Write");
      requireAudit(rejectedWrite.result_event_index < finalRecord.use_event_index, SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_ORDER_INVALID, "The empty Write validation rejection must finish before the successful Write starts");
      for (const requiredRead of requiredReads) {
        requireAudit(reads.some((read) => read.path === requiredRead && read.result_event_index < rejectedWrite.use_event_index), SKILL_GENERATION_TRACE_CODES.REQUIRED_READ_ORDER_INVALID, "Every required Read must finish before an empty Write validation rejection", { path: requiredRead });
      }
    }
    const writeToolPath = validateWriteInput(finalRecord);
    const normalizedWrite = observedPath(writeToolPath, { workspaceRoot, skillRoot, linkedReferences, mode: "write" });
    const outputAbsolute = normalizedWrite.absolutePath;
    const outputBytes = fs.readFileSync(outputAbsolute);
    const outputText = outputBytes.toString("utf8");
    requireAudit(outputBytes.equals(Buffer.from(finalRecord.input.content, "utf8")), SKILL_GENERATION_TRACE_CODES.WRITE_CONTENT_MISMATCH, "The output bytes do not match the successful Write input");
    let outputJson;
    try {
      outputJson = JSON.parse(outputText);
    } catch (error) {
      fail(SKILL_GENERATION_TRACE_CODES.WRITE_JSON_INVALID, "The successful Write content must be valid JSON", { diagnostic: writeJsonDiagnostic(outputBytes, outputText, "JSON_SYNTAX_ERROR", error) });
    }
    if (!isPlainObject(outputJson)) {
      fail(SKILL_GENERATION_TRACE_CODES.WRITE_JSON_INVALID, "The successful Write JSON root must be a plain object", { diagnostic: writeJsonDiagnostic(outputBytes, outputText, "JSON_ROOT_NOT_OBJECT") });
    }
    outputSeal = {
      ordinal: finalRecord.ordinal,
      path: normalizedWrite.receiptPath,
      size_bytes: outputBytes.length,
      sha256: sha256Bytes(outputBytes),
    };
  }

  return {
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "PASS",
    workflow: "skill-generation",
    skill: "wiki-to-diagnosis-skill",
    tool_inventory: [...allowedTools],
    permission_mode: init.permissionMode,
    permission_policy_sha256: crypto.createHash("sha256").update(JSON.stringify(
      skillGenerationPermissionRules({ workspaceRoot, skillRoot, linkedReferences, sourceRoot }),
    )).digest("hex"),
    attempt_policy: attemptPolicy,
    attempt_policy_sha256: crypto.createHash("sha256").update(JSON.stringify(
      attemptPolicy,
    )).digest("hex"),
    tool_sequence: records.map((record) => {
      if (record.tool === "Read") return {
        ordinal: record.ordinal,
        tool: record.tool,
        outcome: "SUCCESS",
        path: observedPath(validateReadInput(record), { workspaceRoot, skillRoot, linkedReferences, mode: "read" }).receiptPath,
      };
      if (record === acceptedValidationRejections[0]) return {
        ordinal: record.ordinal,
        tool: record.tool,
        outcome: "REJECTED",
        classification: "EMPTY_INPUT_REQUIRED_FIELDS_ABSENT",
      };
      if (record.tool === "Write") return { ordinal: record.ordinal, tool: record.tool, outcome: "SUCCESS", path: outputSeal.path };
      return { ordinal: record.ordinal, tool: record.tool, outcome: "SUCCESS" };
    }),
    accepted_validation_rejections: acceptedValidationRejections.map((record) => ({
      ordinal: record.ordinal,
      tool: "Write",
      classification: "EMPTY_INPUT_REQUIRED_FIELDS_ABSENT",
      input_key_names: [],
      result_completed_before_success: true,
    })),
    required_reads: requiredReads,
    observed_reads: reads.map(({ ordinal, path: readPath }) => ({ ordinal, path: readPath })),
    linked_references: linkedReferences.map((relative) => `skill/${relative}`),
    ...(phaseMode ? { ir_input: irInputSeal, compiler: compilerReceipt } : {}),
    output: outputSeal,
    terminal: { subtype: terminal.subtype, is_error: terminal.is_error },
  };
}
