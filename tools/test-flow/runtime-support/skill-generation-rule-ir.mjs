const COMPILER_ID = "generation-blueprint-ordered-interval";
const COMPILER_VERSION = "1.0.0";
const BLUEPRINT_SCHEMA_VERSION = 1;
const FAMILY_KIND = "ORDERED_INTERVAL";
const FAMILY_VERSION = 1;

export const SKILL_GENERATION_RULE_IR = Object.freeze({
  compiler_id: COMPILER_ID,
  compiler_version: COMPILER_VERSION,
  blueprint_schema_version: BLUEPRINT_SCHEMA_VERSION,
  family_kind: FAMILY_KIND,
  family_version: FAMILY_VERSION,
  max_canonical_bytes: 48 * 1024,
});

export const SKILL_GENERATION_RULE_IR_DIAGNOSTIC = Object.freeze({
  schema_version: 1,
  phases: Object.freeze(["ADAPTER", "COMPILER", "DEEP_VALIDATOR", "WRAPPER"]),
  constraints: Object.freeze([
    "IR_SIZE_INVALID",
    "IR_JSON_INVALID",
    "IR_ROOT_OBJECT",
    "IR_CANONICAL_BOUNDED",
    "FINITE_JSON",
    "EXACT_KEYS",
    "OBJECT_REQUIRED",
    "STRING_KEYS_REQUIRED",
    "ARRAY_REQUIRED",
    "NONEMPTY_TEXT_REQUIRED",
    "SAFE_IDENTIFIER_REQUIRED",
    "INTEGER_RANGE",
    "TEMPLATE_RESIDUE",
    "PATH_RESOLUTION_STATUS",
    "POSITION_ORDINALS",
    "POSITION_FIELDS_DISTINCT",
    "FAMILY_KIND",
    "FAMILY_VERSION",
    "FAMILY_POSITION_COUNT",
    "POSITION_NAMES_UNIQUE",
    "POSITION_EVENTS_UNIQUE",
    "BASE_DEPENDENCIES_UNIQUE",
    "FAMILY_PATH_STATUS",
    "RULE_ID_DUPLICATE",
    "RULE_DEPENDENCY_SHAPE",
    "RULE_DEPENDENCY_TOPOLOGY",
    "PATH_ID_DUPLICATE",
    "TERMINAL_TERM_SHAPE",
    "TERMINAL_RULE_REFERENCE",
    "BLUEPRINT_SCHEMA_VERSION",
    "COMPILER_IDENTITY",
    "SPEC_KEYS",
    "VERIFICATION_SCHEMA_VERSION",
    "EXPECTED_COUNTS",
    "GENERATOR_LOAD",
    "DEEP_VALIDATOR_REJECTED",
    "COMPILER_PROCESS",
    "COMPILER_ENVELOPE",
  ]),
});

function deepFreeze(value) {
  if (value !== null && typeof value === "object" && !Object.isFrozen(value)) {
    for (const nested of Object.values(value)) deepFreeze(nested);
    Object.freeze(value);
  }
  return value;
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

export function validSkillGenerationRuleIrCompilerFailure(value) {
  return exactKeys(value, ["schema_version", "phase", "constraint_id"])
    && value.schema_version === SKILL_GENERATION_RULE_IR_DIAGNOSTIC.schema_version
    && SKILL_GENERATION_RULE_IR_DIAGNOSTIC.phases.includes(value.phase)
    && value.phase !== "WRAPPER"
    && SKILL_GENERATION_RULE_IR_DIAGNOSTIC.constraints.includes(value.constraint_id);
}

export function validSkillGenerationRuleIrDiagnostic(value) {
  return exactKeys(value, ["schema_version", "phase", "constraint_id", "ir"])
    && value.schema_version === SKILL_GENERATION_RULE_IR_DIAGNOSTIC.schema_version
    && SKILL_GENERATION_RULE_IR_DIAGNOSTIC.phases.includes(value.phase)
    && SKILL_GENERATION_RULE_IR_DIAGNOSTIC.constraints.includes(value.constraint_id)
    && exactKeys(value.ir, ["size_bytes", "sha256"])
    && Number.isSafeInteger(value.ir.size_bytes)
    && value.ir.size_bytes > 0
    && /^[a-f0-9]{64}$/.test(value.ir.sha256 ?? "");
}

const nonemptyString = { type: "string", minLength: 1 };
const objectItem = { type: "object" };
const exactObjectArray = (count) => ({
  type: "array",
  minItems: count,
  maxItems: count,
  items: objectItem,
});
const exactStringArray = (count) => ({
  type: "array",
  minItems: count,
  maxItems: count,
  items: nonemptyString,
});

const SPEC_REQUIRED = Object.freeze([
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
  "time_characteristics",
  "analysis_steps",
  "judgement_rules",
  "output_requirements",
  "assumptions",
  "requires_logparse",
]);

const POSITION_PROPERTIES = Object.freeze({
  ordinal: { type: "integer", minimum: 1, maximum: 5 },
  name: nonemptyString,
  event: nonemptyString,
  end_field: nonemptyString,
  cost_field: nonemptyString,
  queue_field: nonemptyString,
  timeout_field: nonemptyString,
});
const POSITION_KEYS = Object.freeze(Object.keys(POSITION_PROPERTIES));

const SHARED_PROPERTIES = Object.freeze({
  call_event: nonemptyString,
  call_timeout_field: nonemptyString,
  call_present_rule_id: nonemptyString,
  detail_event: nonemptyString,
  detail_timeout_field: nonemptyString,
  detail_present_rule_id: nonemptyString,
  base_semantic_dependency_rule_ids: exactStringArray(2),
});
const SHARED_KEYS = Object.freeze(Object.keys(SHARED_PROPERTIES));

const TEXT_KEYS = Object.freeze([
  "present_prefix",
  "present_suffix",
  "timeout_infix",
  "timeout_suffix",
  "core_prefix",
  "core_infix",
  "core_suffix",
  "serial_prefix",
  "serial_infix",
  "serial_suffix",
  "interval_prefix",
  "interval_infix",
  "interval_suffix",
  "unattributed_assertion",
  "overlap_assertion",
  "full_assertion",
  "gap_assertion",
]);

const PATH_METADATA_SCHEMA = deepFreeze({
  type: "object",
  additionalProperties: false,
  required: ["id", "resolution_status"],
  properties: {
    id: nonemptyString,
    resolution_status: { type: "string", enum: ["COMPLETE", "PARTIAL"] },
  },
});

const EXPECTED_COUNTS = Object.freeze({
  positions: 5,
  policies: 2,
  extractors: 10,
  prefix_rules: 7,
  mechanical_rules: 105,
  middle_rules: 9,
  semantic_rules: 39,
  suffix_rules: 5,
  total_rules: 165,
  family_terminal_paths: 3,
  literal_terminal_paths: 6,
  total_terminal_paths: 9,
});
const EXPECTED_COUNT_KEYS = Object.freeze(Object.keys(EXPECTED_COUNTS));

export const GENERATION_BLUEPRINT_SUBMISSION_JSON_SCHEMA = deepFreeze({
  type: "object",
  additionalProperties: false,
  required: ["schema_version", "compiler", "spec", "verification"],
  properties: {
    schema_version: { const: BLUEPRINT_SCHEMA_VERSION },
    compiler: {
      type: "object",
      additionalProperties: false,
      required: ["id", "version"],
      properties: {
        id: { const: COMPILER_ID },
        version: { const: COMPILER_VERSION },
      },
    },
    spec: {
      type: "object",
      additionalProperties: false,
      required: [...SPEC_REQUIRED],
      properties: {
        schema_version: { const: 6 },
        generator_version: { const: "6.0.0" },
        id: nonemptyString,
        version: nonemptyString,
        capability: nonemptyString,
        deployment_scope: nonemptyString,
        summary: nonemptyString,
        chinese_title: nonemptyString,
        module_name: nonemptyString,
        problem_scope: nonemptyString,
        roles: exactObjectArray(2),
        requirements: exactObjectArray(5),
        logparse_plan: {
          type: "object",
          additionalProperties: false,
          required: ["anchors"],
          properties: { anchors: exactObjectArray(2) },
        },
        time_characteristics: exactStringArray(4),
        analysis_steps: exactStringArray(5),
        judgement_rules: exactStringArray(6),
        output_requirements: exactStringArray(5),
        assumptions: exactStringArray(3),
        requires_logparse: { const: true },
        logparse_product: nonemptyString,
      },
    },
    verification: {
      type: "object",
      additionalProperties: false,
      required: [
        "schema_version",
        "observation_policies",
        "event_extractors",
        "literal_rule_segments",
        "literal_terminal_segments",
        "ordered_interval_family",
        "expected_counts",
      ],
      properties: {
        schema_version: { const: 2 },
        observation_policies: exactObjectArray(2),
        event_extractors: exactObjectArray(10),
        literal_rule_segments: {
          type: "object",
          additionalProperties: false,
          required: ["prefix", "middle", "suffix"],
          properties: {
            prefix: exactObjectArray(7),
            middle: exactObjectArray(9),
            suffix: exactObjectArray(5),
          },
        },
        literal_terminal_segments: {
          type: "object",
          additionalProperties: false,
          required: ["after_complete", "after_families"],
          properties: {
            after_complete: exactObjectArray(2),
            after_families: exactObjectArray(4),
          },
        },
        ordered_interval_family: {
          type: "object",
          additionalProperties: false,
          required: ["kind", "version", "namespace", "positions", "shared", "texts", "names", "terminal_paths"],
          properties: {
            kind: { const: FAMILY_KIND },
            version: { const: FAMILY_VERSION },
            namespace: nonemptyString,
            positions: {
              type: "array",
              minItems: 5,
              maxItems: 5,
              items: {
                type: "object",
                additionalProperties: false,
                required: [...POSITION_KEYS],
                properties: POSITION_PROPERTIES,
              },
            },
            shared: {
              type: "object",
              additionalProperties: false,
              required: [...SHARED_KEYS],
              properties: SHARED_PROPERTIES,
            },
            texts: {
              type: "object",
              additionalProperties: false,
              required: [...TEXT_KEYS],
              properties: Object.fromEntries(TEXT_KEYS.map((name) => [name, nonemptyString])),
            },
            names: {
              type: "object",
              additionalProperties: false,
              required: ["unattributed_semantic_suffix"],
              properties: { unattributed_semantic_suffix: nonemptyString },
            },
            terminal_paths: {
              type: "object",
              additionalProperties: false,
              required: ["complete", "unattributed", "mixed"],
              properties: {
                complete: PATH_METADATA_SCHEMA,
                unattributed: PATH_METADATA_SCHEMA,
                mixed: PATH_METADATA_SCHEMA,
              },
            },
          },
        },
        expected_counts: {
          type: "object",
          additionalProperties: false,
          required: [...EXPECTED_COUNT_KEYS],
          properties: Object.fromEntries(
            Object.entries(EXPECTED_COUNTS).map(([name, count]) => [name, { const: count }]),
          ),
        },
      },
    },
  },
});

function validNonemptyString(value) {
  return typeof value === "string" && value.length > 0;
}

function validObjectArray(value, count) {
  return Array.isArray(value) && value.length === count && value.every(isPlainObject);
}

function validStringArray(value, count) {
  return Array.isArray(value) && value.length === count && value.every(validNonemptyString);
}

function validSpec(value) {
  const allowed = [...SPEC_REQUIRED, "logparse_product"];
  if (!isPlainObject(value)
    || !SPEC_REQUIRED.every((name) => Object.hasOwn(value, name))
    || !Object.keys(value).every((name) => allowed.includes(name))
    || value.schema_version !== 6
    || value.generator_version !== "6.0.0"
    || value.requires_logparse !== true) return false;
  for (const name of ["id", "version", "capability", "deployment_scope", "summary", "chinese_title", "module_name", "problem_scope"]) {
    if (!validNonemptyString(value[name])) return false;
  }
  if (Object.hasOwn(value, "logparse_product") && !validNonemptyString(value.logparse_product)) return false;
  return validObjectArray(value.roles, 2)
    && validObjectArray(value.requirements, 5)
    && exactKeys(value.logparse_plan, ["anchors"])
    && validObjectArray(value.logparse_plan.anchors, 2)
    && validStringArray(value.time_characteristics, 4)
    && validStringArray(value.analysis_steps, 5)
    && validStringArray(value.judgement_rules, 6)
    && validStringArray(value.output_requirements, 5)
    && validStringArray(value.assumptions, 3);
}

function validPosition(value, index) {
  if (!exactKeys(value, POSITION_KEYS) || value.ordinal !== index + 1) return false;
  return POSITION_KEYS.filter((name) => name !== "ordinal").every((name) => validNonemptyString(value[name]));
}

function validPathMetadata(value, status) {
  return exactKeys(value, ["id", "resolution_status"])
    && validNonemptyString(value.id)
    && value.resolution_status === status;
}

function validFamily(value) {
  if (!exactKeys(value, ["kind", "version", "namespace", "positions", "shared", "texts", "names", "terminal_paths"])
    || value.kind !== FAMILY_KIND
    || value.version !== FAMILY_VERSION
    || !validNonemptyString(value.namespace)
    || !Array.isArray(value.positions)
    || value.positions.length !== 5
    || !value.positions.every(validPosition)
    || !exactKeys(value.shared, SHARED_KEYS)
    || !SHARED_KEYS.filter((name) => name !== "base_semantic_dependency_rule_ids").every((name) => validNonemptyString(value.shared[name]))
    || !validStringArray(value.shared.base_semantic_dependency_rule_ids, 2)
    || !exactKeys(value.texts, TEXT_KEYS)
    || !TEXT_KEYS.every((name) => validNonemptyString(value.texts[name]))
    || !exactKeys(value.names, ["unattributed_semantic_suffix"])
    || !validNonemptyString(value.names.unattributed_semantic_suffix)
    || !exactKeys(value.terminal_paths, ["complete", "unattributed", "mixed"])) return false;
  return validPathMetadata(value.terminal_paths.complete, "COMPLETE")
    && validPathMetadata(value.terminal_paths.unattributed, "PARTIAL")
    && validPathMetadata(value.terminal_paths.mixed, "PARTIAL");
}

function validVerification(value) {
  if (!exactKeys(value, [
    "schema_version", "observation_policies", "event_extractors", "literal_rule_segments",
    "literal_terminal_segments", "ordered_interval_family", "expected_counts",
  ])
    || value.schema_version !== 2
    || !validObjectArray(value.observation_policies, 2)
    || !validObjectArray(value.event_extractors, 10)
    || !exactKeys(value.literal_rule_segments, ["prefix", "middle", "suffix"])
    || !validObjectArray(value.literal_rule_segments.prefix, 7)
    || !validObjectArray(value.literal_rule_segments.middle, 9)
    || !validObjectArray(value.literal_rule_segments.suffix, 5)
    || !exactKeys(value.literal_terminal_segments, ["after_complete", "after_families"])
    || !validObjectArray(value.literal_terminal_segments.after_complete, 2)
    || !validObjectArray(value.literal_terminal_segments.after_families, 4)
    || !validFamily(value.ordered_interval_family)
    || !exactKeys(value.expected_counts, EXPECTED_COUNT_KEYS)) return false;
  return EXPECTED_COUNT_KEYS.every((name) => value.expected_counts[name] === EXPECTED_COUNTS[name]);
}

export function validGenerationBlueprintSubmission(value) {
  return exactKeys(value, ["schema_version", "compiler", "spec", "verification"])
    && value.schema_version === BLUEPRINT_SCHEMA_VERSION
    && exactKeys(value.compiler, ["id", "version"])
    && value.compiler.id === COMPILER_ID
    && value.compiler.version === COMPILER_VERSION
    && validSpec(value.spec)
    && validVerification(value.verification);
}

export function buildGenerationBlueprintSubmissionDiagnostic(value) {
  return Object.freeze({
    schema_version: 1,
    status: validGenerationBlueprintSubmission(value)
      ? "SCHEMA_VALID_TOOL_REJECTED"
      : "INVALID_IR",
  });
}

export function validGenerationBlueprintSubmissionDiagnostic(value) {
  return exactKeys(value, ["schema_version", "status"])
    && value.schema_version === 1
    && ["INVALID_IR", "SCHEMA_VALID_TOOL_REJECTED"].includes(value.status);
}

export function validSkillGenerationCompilerReceipt(value) {
  return exactKeys(value, ["id", "version", "blueprint_schema_version", "family_kind", "family_version"])
    && value.id === COMPILER_ID
    && value.version === COMPILER_VERSION
    && value.blueprint_schema_version === BLUEPRINT_SCHEMA_VERSION
    && value.family_kind === FAMILY_KIND
    && value.family_version === FAMILY_VERSION;
}

export const GENERATION_BLUEPRINT_SPEC_REQUIRED_KEYS = SPEC_REQUIRED;
