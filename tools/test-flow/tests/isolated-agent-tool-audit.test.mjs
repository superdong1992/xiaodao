import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { canonicalJson, sha256Bytes } from "../lib/util.mjs";

import {
  auditIncompleteSkillGenerationTrace,
  auditPartialSkillGenerationTrace,
  auditSkillGenerationTrace,
  buildGenerationSpecSubmissionDiagnostic,
  buildSkillGenerationIncompleteAuditRejectedReceipt,
  discoverLinkedSkillReferences,
  GENERATION_SPEC_SUBMISSION_JSON_SCHEMA,
  ISOLATED_AGENT_STREAM_EVENT_TYPES,
  skillGenerationPermissionRules,
  SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES,
  SKILL_GENERATION_PHASE_REQUIRED_RECEIPT_READS,
  SKILL_GENERATION_TOOL_ATTEMPT_POLICY,
  SKILL_GENERATION_TRACE_CODES as CODES,
  SKILL_GENERATION_TRACE_SCHEMA_VERSION,
  validGenerationSpecSubmission,
  validGenerationSpecSubmissionDiagnostic,
  validIsolatedAgentStreamEventType,
  validSkillGenerationFailedTraceAuditReceipt,
  validSkillGenerationIncompleteAuditRejectedReceipt,
  validSkillGenerationIncompleteTraceAuditReceipt,
  validSkillGenerationPartialTraceAuditReceipt,
  validSkillGenerationTraceAuditReceipt,
} from "../runtime-support/isolated-agent-tool-audit.mjs";
import {
  buildGenerationBlueprintSubmissionDiagnostic,
  GENERATION_BLUEPRINT_SUBMISSION_JSON_SCHEMA,
  SKILL_GENERATION_RULE_IR,
  validGenerationBlueprintSubmission,
  validGenerationBlueprintSubmissionDiagnostic,
  validSkillGenerationRuleIrDiagnostic,
} from "../runtime-support/skill-generation-rule-ir.mjs";

function write(root, relative, content = `${relative}\n`) {
  const target = path.join(root, ...relative.split("/"));
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, content, "utf8");
}

function workspaceFixture() {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-audit-"));
  const workspaceRoot = path.join(parent, "workspace");
  const skillRoot = path.join(parent, "installed-skill");
  fs.mkdirSync(workspaceRoot);
  write(workspaceRoot, "inputs/wiki.md");
  write(workspaceRoot, "inputs/clarifications.md");
  write(skillRoot, "SKILL.md", [
    "# Converter",
    "[generation](references/generation-spec-v6-reference.md)",
    "[verification](references/verification-contract-v2-reference.md)",
    "[optional](references/ordinary-example.md)",
    "",
  ].join("\n"));
  write(skillRoot, "references/generation-spec-v6-reference.md");
  write(skillRoot, "references/verification-contract-v2-reference.md");
  write(skillRoot, "references/ordinary-example.md");
  write(workspaceRoot, "unlinked.md");
  return { parent, workspaceRoot, skillRoot };
}

function phaseWorkspaceFixture() {
  const fixture = workspaceFixture();
  write(fixture.skillRoot, "SKILL.md", [
    "# Converter",
    "[generation](references/generation-spec-v6-reference.md)",
    "[verification](references/verification-contract-v2-reference.md)",
    ...SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES.map((relative, index) => `[checkpoint ${index + 1}](${relative})`),
    "",
  ].join("\n"));
  for (const relative of SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES) {
    write(fixture.skillRoot, relative, `control_only: true\ncheckpoint: ${path.basename(relative, ".md")}\n`);
  }
  return fixture;
}

function toolUse(id, name, input) {
  return {
    type: "assistant",
    message: { role: "assistant", content: [{ type: "tool_use", id, name, input }] },
  };
}

function toolResult(id, tool = "ordinary", { error = false, success = undefined } = {}) {
  const raw = tool === "Skill" ? { success: success ?? !error } : { type: tool.toLowerCase() };
  if (error && tool !== "Skill") raw.isError = true;
  return {
    type: "user",
    message: { role: "user", content: [{ type: "tool_result", tool_use_id: id, is_error: error }] },
    tool_use_result: raw,
  };
}

function invocation(id, name, input, options) {
  return [toolUse(id, name, input), toolResult(id, name, options)];
}

function readBatch(entries) {
  return [
    {
      type: "assistant",
      message: {
        role: "assistant",
        content: entries.map(([id, filePath]) => ({
          type: "tool_use",
          id,
          name: "Read",
          input: { file_path: filePath },
        })),
      },
    },
    {
      type: "user",
      message: {
        role: "user",
        content: entries.map(([id]) => ({
          type: "tool_result",
          tool_use_id: id,
          is_error: false,
        })),
      },
      tool_use_result: { type: "read" },
    },
  ];
}

function minimalValidSubmission() {
  return {
    schema_version: 6,
    generator_version: "6.0.0",
    id: "diagnose-test",
    version: "1.0.0",
    capability: "diagnose",
    deployment_scope: "server",
    summary: "summary",
    chinese_title: "title",
    module_name: "diagnose_test",
    problem_scope: "scope",
    roles: [{}, {}],
    requirements: Array.from({ length: 5 }, () => ({})),
    logparse_plan: { anchors: [{}, {}] },
    verification_contract: {
      schema_version: 2,
      observation_policies: [{}, {}],
      event_extractors: Array.from({ length: 10 }, () => ({})),
      rules: Array.from({ length: 165 }, () => ({})),
      terminal_paths: Array.from({ length: 9 }, () => ({})),
    },
    time_characteristics: Array.from({ length: 4 }, (_, index) => `time-${index}`),
    analysis_steps: Array.from({ length: 5 }, (_, index) => `analysis-${index}`),
    judgement_rules: Array.from({ length: 6 }, (_, index) => `judgement-${index}`),
    output_requirements: Array.from({ length: 5 }, (_, index) => `output-${index}`),
    assumptions: Array.from({ length: 3 }, (_, index) => `assumption-${index}`),
    requires_logparse: true,
  };
}

function minimalValidBlueprint({ includeLogparseProduct = false } = {}) {
  const final = minimalValidSubmission();
  const { verification_contract: verificationContract, ...spec } = final;
  if (includeLogparseProduct) spec.logparse_product = "source-supported-product";
  const positions = ["one", "two", "three", "four", "five"].map((name, index) => ({
    ordinal: index + 1,
    name,
    event: `ordered_target_${name}`,
    end_field: `${name}_end_us`,
    cost_field: `${name}_cost_us`,
    queue_field: `${name}_queue_us`,
    timeout_field: `${name}_timeout_ms`,
  }));
  return {
    schema_version: 1,
    compiler: { id: SKILL_GENERATION_RULE_IR.compiler_id, version: SKILL_GENERATION_RULE_IR.compiler_version },
    spec,
    verification: {
      schema_version: 2,
      observation_policies: verificationContract.observation_policies,
      event_extractors: verificationContract.event_extractors,
      literal_rule_segments: {
        prefix: Array.from({ length: 7 }, () => ({})),
        middle: Array.from({ length: 9 }, () => ({})),
        suffix: Array.from({ length: 5 }, () => ({})),
      },
      literal_terminal_segments: {
        after_complete: [{}, {}],
        after_families: [{}, {}, {}, {}],
      },
      ordered_interval_family: {
        kind: SKILL_GENERATION_RULE_IR.family_kind,
        version: SKILL_GENERATION_RULE_IR.family_version,
        namespace: "ordered",
        positions,
        shared: {
          call_event: "call_event",
          call_timeout_field: "timeout_ms",
          call_present_rule_id: "call_present",
          detail_event: "detail_event",
          detail_timeout_field: "timeout_ms",
          detail_present_rule_id: "detail_present",
          base_semantic_dependency_rule_ids: ["base_one", "base_two"],
        },
        texts: Object.fromEntries([
          "present_prefix", "present_suffix", "timeout_infix", "timeout_suffix", "core_prefix",
          "core_infix", "core_suffix", "serial_prefix", "serial_infix", "serial_suffix",
          "interval_prefix", "interval_infix", "interval_suffix", "unattributed_assertion",
          "overlap_assertion", "full_assertion", "gap_assertion",
        ].map((name) => [name, name])),
        names: { unattributed_semantic_suffix: "unattributed_confirmed" },
        terminal_paths: {
          complete: { id: "complete", resolution_status: "COMPLETE" },
          unattributed: { id: "unattributed", resolution_status: "PARTIAL" },
          mixed: { id: "mixed", resolution_status: "PARTIAL" },
        },
      },
      expected_counts: {
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
      },
    },
  };
}

function compilationFor(blueprint, spec = minimalValidSubmission()) {
  const ir = canonicalJson(blueprint);
  const output = canonicalJson(spec);
  return {
    schema_version: 1,
    compiler: {
      id: SKILL_GENERATION_RULE_IR.compiler_id,
      version: SKILL_GENERATION_RULE_IR.compiler_version,
      blueprint_schema_version: SKILL_GENERATION_RULE_IR.blueprint_schema_version,
      family_kind: SKILL_GENERATION_RULE_IR.family_kind,
      family_version: SKILL_GENERATION_RULE_IR.family_version,
    },
    ir: { size_bytes: Buffer.byteLength(ir), sha256: sha256Bytes(ir) },
    output: { size_bytes: Buffer.byteLength(output), sha256: sha256Bytes(output) },
    spec,
  };
}

function validEvents(workspaceRoot, skillRoot, content = "{}") {
  return [
    { type: "system", subtype: "init", cwd: workspaceRoot, permissionMode: "dontAsk", tools: ["Read", "Skill", "Write"] },
    ...invocation("skill", "Skill", { skill: "wiki-to-diagnosis-skill" }),
    ...invocation("wiki", "Read", { file_path: path.join(workspaceRoot, "inputs", "wiki.md") }),
    ...invocation("clarifications", "Read", { file_path: path.join(workspaceRoot, "inputs", "clarifications.md") }),
    ...invocation("generation", "Read", { file_path: path.join(skillRoot, "references", "generation-spec-v6-reference.md") }),
    ...invocation("verification", "Read", { file_path: path.join(skillRoot, "references", "verification-contract-v2-reference.md") }),
    ...invocation("optional", "Read", { file_path: path.join(skillRoot, "references", "ordinary-example.md"), limit: 200 }),
    ...invocation("write", "Write", { file_path: path.join(workspaceRoot, "output", "generation-spec.json"), content }),
    { type: "result", subtype: "success", is_error: false, result: "DONE" },
  ];
}

function validPhaseEvents(workspaceRoot, skillRoot, structuredOutput = minimalValidBlueprint()) {
  return [
    { type: "system", subtype: "init", cwd: workspaceRoot, permissionMode: "dontAsk", tools: ["Read", "Skill", "StructuredOutput"] },
    ...invocation("skill", "Skill", { skill: "wiki-to-diagnosis-skill" }),
    ...readBatch([
      ["wiki", path.join(workspaceRoot, "inputs", "wiki.md")],
      ["clarifications", path.join(workspaceRoot, "inputs", "clarifications.md")],
    ]),
    ...invocation("generation", "Read", { file_path: path.join(skillRoot, "references", "generation-spec-v6-reference.md") }),
    ...invocation("verification", "Read", { file_path: path.join(skillRoot, "references", "verification-contract-v2-reference.md") }),
    ...SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES.flatMap((relative, index) => invocation(
      `checkpoint-${index + 1}`,
      "Read",
      { file_path: path.join(skillRoot, ...relative.split("/")) },
    )),
    ...invocation("structured-output", "StructuredOutput", structuredOutput),
    { type: "result", subtype: "success", is_error: false, result: "DONE", structured_output: structuredClone(structuredOutput) },
  ];
}

function arrangeValid() {
  const fixture = workspaceFixture();
  const content = "{\"schema_version\":6}\n";
  write(fixture.workspaceRoot, "output/generation-spec.json", content);
  return { ...fixture, content, events: validEvents(fixture.workspaceRoot, fixture.skillRoot, content) };
}

function arrangeValidPhase() {
  const fixture = phaseWorkspaceFixture();
  const structuredOutput = minimalValidBlueprint();
  return {
    ...fixture,
    structuredOutput,
    compilation: compilationFor(structuredOutput),
    events: validPhaseEvents(fixture.workspaceRoot, fixture.skillRoot, structuredOutput),
  };
}

const PHASE_TOOL_IDS = Object.freeze([
  "skill",
  "wiki",
  "clarifications",
  "generation",
  "verification",
  "checkpoint-1",
  "checkpoint-2",
  "checkpoint-3",
  "checkpoint-4",
  "structured-output",
]);

function phasePrefixAfterCompletedOrdinal(events, ordinal) {
  assert.ok(Number.isSafeInteger(ordinal) && ordinal >= 0 && ordinal < PHASE_TOOL_IDS.length);
  const targetId = PHASE_TOOL_IDS[ordinal];
  const resultIndex = events.findIndex((event) => event?.message?.content?.some(
    (block) => block?.type === "tool_result" && block.tool_use_id === targetId,
  ));
  assert.notEqual(resultIndex, -1);
  const prefix = structuredClone(events.slice(0, resultIndex + 1));
  if (ordinal === 1) {
    prefix.at(-1).message.content = prefix.at(-1).message.content.filter(
      (block) => block.tool_use_id === targetId,
    );
  }
  return prefix;
}

function phaseIncompleteRecords(structuredOutput, outcomes) {
  const canonical = canonicalJson(structuredOutput);
  return outcomes.map((outcome, ordinal) => {
    const base = { ordinal, tool: ordinal === 0 ? "Skill" : ordinal <= 8 ? "Read" : "StructuredOutput", outcome };
    if (ordinal >= 1 && ordinal <= 8) {
      return { ...base, path: SKILL_GENERATION_PHASE_REQUIRED_RECEIPT_READS[ordinal - 1] };
    }
    if (ordinal >= 9) {
      return {
        ...base,
        size_bytes: Buffer.byteLength(canonical),
        sha256: sha256Bytes(canonical),
        ...(outcome === "ERROR" ? {
          diagnostic: buildGenerationBlueprintSubmissionDiagnostic(structuredOutput),
        } : {}),
      };
    }
    return base;
  });
}

function expectedIncompleteReceipt(toolSequence, events, code = CODES.INCOMPLETE_PREFIX) {
  return {
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "FAIL",
    workflow: "skill-generation",
    code,
    stream_state: "TERMINAL_MISSING",
    stream: {
      schema_version: 1,
      event_count: events.length,
      parsed_event_count: events.length,
      init_count: 1,
      result_count: 0,
      last_event_type: events.at(-1).type,
      complete: false,
    },
    tool_sequence: toolSequence,
  };
}

function replaceToolPath(events, tool, occurrence, filePath) {
  const matches = events.flatMap((event) => (event?.message?.content ?? []))
    .filter((block) => block?.type === "tool_use" && block.name === tool);
  matches[occurrence].input.file_path = filePath;
}

function errorCode(callback, code) {
  assert.throws(callback, (error) => error?.code === code, code);
}

test("the frozen submission schema accepts a neutral synthetic fixture and rejects exact-count drift", async (context) => {
  const validSubmission = minimalValidSubmission();
  assert.equal(Object.isFrozen(GENERATION_SPEC_SUBMISSION_JSON_SCHEMA), true);
  assert.equal(Object.isFrozen(GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.properties.verification_contract), true);
  assert.equal(GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.additionalProperties, false);
  assert.equal(GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.properties.schema_version.const, 6);
  assert.equal(GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.properties.generator_version.const, "6.0.0");
  assert.equal(GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.properties.requires_logparse.const, true);
  assert.equal(GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.properties.roles.minItems, 2);
  assert.equal(GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.properties.roles.maxItems, 2);
  assert.equal(GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.properties.requirements.minItems, 5);
  assert.equal(GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.properties.requirements.maxItems, 5);
  assert.deepEqual(GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.properties.logparse_plan.required, ["anchors"]);
  assert.equal(GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.properties.logparse_plan.additionalProperties, false);
  assert.equal(GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.properties.logparse_plan.properties.anchors.minItems, 2);
  assert.equal(GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.properties.logparse_plan.properties.anchors.maxItems, 2);
  assert.equal(GENERATION_SPEC_SUBMISSION_JSON_SCHEMA.properties.verification_contract.additionalProperties, false);
  assert.equal(validGenerationSpecSubmission(validSubmission), true);

  const cases = [
    ["empty", () => ({})],
    ["missing section", () => {
      const value = structuredClone(validSubmission);
      delete value.analysis_steps;
      return value;
    }],
    ["nine extractors", () => {
      const value = structuredClone(validSubmission);
      value.verification_contract.event_extractors.pop();
      return value;
    }],
    ["164 rules", () => {
      const value = structuredClone(validSubmission);
      value.verification_contract.rules.pop();
      return value;
    }],
    ["eight paths", () => {
      const value = structuredClone(validSubmission);
      value.verification_contract.terminal_paths.pop();
      return value;
    }],
    ...[
      ["roles", 2],
      ["requirements", 5],
      ["time_characteristics", 4],
      ["analysis_steps", 5],
      ["judgement_rules", 6],
      ["output_requirements", 5],
      ["assumptions", 3],
    ].flatMap(([field, count]) => [
      [`${field} under exact count ${count}`, () => ({ ...structuredClone(validSubmission), [field]: validSubmission[field].slice(0, count - 1) })],
      [`${field} over exact count ${count}`, () => ({ ...structuredClone(validSubmission), [field]: [...validSubmission[field], structuredClone(validSubmission[field][0])] })],
    ]),
    ...[
      ["observation_policies", 2],
      ["event_extractors", 10],
      ["rules", 165],
      ["terminal_paths", 9],
    ].flatMap(([field, count]) => [
      [`${field} under exact count ${count}`, () => {
        const value = structuredClone(validSubmission);
        value.verification_contract[field].pop();
        return value;
      }],
      [`${field} over exact count ${count}`, () => {
        const value = structuredClone(validSubmission);
        value.verification_contract[field].push(structuredClone(value.verification_contract[field][0]));
        return value;
      }],
    ]),
    ["extra root", () => ({ ...structuredClone(validSubmission), unexpected: true })],
    ["extra verification field", () => {
      const value = structuredClone(validSubmission);
      value.verification_contract.unexpected = true;
      return value;
    }],
    ...["id", "version", "capability", "deployment_scope", "summary", "chinese_title", "module_name", "problem_scope"].map((field) => [
      `${field} must be a non-empty string`,
      () => ({ ...structuredClone(validSubmission), [field]: "" }),
    ]),
    ["optional logparse_product must be a non-empty string when present", () => ({
      ...structuredClone(validSubmission),
      logparse_product: [],
    })],
    ["role entries must be objects", () => ({ ...structuredClone(validSubmission), roles: ["role", {}] })],
    ["requirement entries must be objects", () => ({
      ...structuredClone(validSubmission),
      requirements: ["requirement", ...structuredClone(validSubmission.requirements.slice(1))],
    })],
    ["logparse_plan must be an object", () => ({ ...structuredClone(validSubmission), logparse_plan: [] })],
    ["logparse_plan requires anchors", () => ({ ...structuredClone(validSubmission), logparse_plan: {} })],
    ["logparse_plan rejects extra properties", () => ({
      ...structuredClone(validSubmission),
      logparse_plan: { ...structuredClone(validSubmission.logparse_plan), unexpected: true },
    })],
    ["logparse_plan requires exactly two object anchors", () => ({
      ...structuredClone(validSubmission),
      logparse_plan: { anchors: ["invalid", {}] },
    })],
    ...["time_characteristics", "analysis_steps", "judgement_rules", "output_requirements", "assumptions"].map((field) => [
      `${field} must be a non-empty string array`,
      () => ({ ...structuredClone(validSubmission), [field]: [""] }),
    ]),
    ...["observation_policies", "event_extractors", "rules", "terminal_paths"].map((field) => [
      `${field} entries must be objects`,
      () => {
        const value = structuredClone(validSubmission);
        value.verification_contract[field][0] = "invalid";
        return value;
      },
    ]),
  ];
  for (const [name, build] of cases) {
    await context.test(name, () => assert.equal(validGenerationSpecSubmission(build()), false));
  }
});

test("the compact GenerationBlueprint schema binds optional product metadata and exact family counts", async (context) => {
  const withoutProduct = minimalValidBlueprint();
  const withProduct = minimalValidBlueprint({ includeLogparseProduct: true });
  assert.equal(Object.isFrozen(GENERATION_BLUEPRINT_SUBMISSION_JSON_SCHEMA), true);
  assert.deepEqual(GENERATION_BLUEPRINT_SUBMISSION_JSON_SCHEMA.required, [
    "schema_version", "compiler", "spec", "verification",
  ]);
  assert.equal(GENERATION_BLUEPRINT_SUBMISSION_JSON_SCHEMA.properties.spec.required.length, 19);
  assert.equal(
    GENERATION_BLUEPRINT_SUBMISSION_JSON_SCHEMA.properties.spec.required.includes("logparse_product"),
    false,
  );
  assert.equal(validGenerationBlueprintSubmission(withoutProduct), true);
  assert.equal(validGenerationBlueprintSubmission(withProduct), true);
  assert.ok(Buffer.byteLength(canonicalJson(withProduct)) <= SKILL_GENERATION_RULE_IR.max_canonical_bytes);
  assert.equal(Object.hasOwn(withProduct.verification.ordered_interval_family, "rules"), false);
  assert.equal(Object.hasOwn(withProduct.verification.ordered_interval_family.terminal_paths.complete, "condition"), false);
  assert.deepEqual(buildGenerationBlueprintSubmissionDiagnostic(withProduct), {
    schema_version: 1,
    status: "SCHEMA_VALID_TOOL_REJECTED",
  });

  const cases = [
    ["empty root", {}],
    ["unknown compiler version", (() => {
      const value = structuredClone(withProduct);
      value.compiler.version = "9.9.9";
      return value;
    })()],
    ["missing optional product is valid but an empty product is not", (() => {
      const value = structuredClone(withProduct);
      value.spec.logparse_product = "";
      return value;
    })()],
    ["nine extractors", (() => {
      const value = structuredClone(withProduct);
      value.verification.event_extractors.pop();
      return value;
    })()],
    ["wrong literal rule count", (() => {
      const value = structuredClone(withProduct);
      value.verification.literal_rule_segments.middle.pop();
      return value;
    })()],
    ["unknown family kind", (() => {
      const value = structuredClone(withProduct);
      value.verification.ordered_interval_family.kind = "UNKNOWN";
      return value;
    })()],
    ["position mapping drift", (() => {
      const value = structuredClone(withProduct);
      value.verification.ordered_interval_family.positions[1].ordinal = 1;
      return value;
    })()],
    ["wrong expected count", (() => {
      const value = structuredClone(withProduct);
      value.verification.expected_counts.total_rules = 164;
      return value;
    })()],
    ["expanded family rules are forbidden", (() => {
      const value = structuredClone(withProduct);
      value.verification.ordered_interval_family.rules = [];
      return value;
    })()],
  ];
  for (const [name, value] of cases) {
    await context.test(name, () => {
      assert.equal(validGenerationBlueprintSubmission(value), false);
      const diagnostic = buildGenerationBlueprintSubmissionDiagnostic(value);
      assert.equal(validGenerationBlueprintSubmissionDiagnostic(diagnostic), true);
      assert.equal(diagnostic.status, "INVALID_IR");
    });
  }
});

test("failed rule IR diagnostics retain only a fixed constraint and input seal", async (context) => {
  const diagnostic = {
    schema_version: 1,
    phase: "COMPILER",
    constraint_id: "POSITION_ORDINALS",
    ir: { size_bytes: 4096, sha256: "a".repeat(64) },
  };
  const receipt = {
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "FAIL",
    workflow: "skill-generation",
    code: CODES.RULE_IR_INVALID,
    diagnostic,
  };
  assert.equal(validSkillGenerationRuleIrDiagnostic(diagnostic), true);
  assert.equal(validSkillGenerationFailedTraceAuditReceipt(receipt), true);
  assert.equal(validSkillGenerationTraceAuditReceipt(receipt), false);
  assert.doesNotMatch(JSON.stringify(receipt), /content|input|message|path|raw|secret/iu);

  const mutations = [
    ["unknown phase", (value) => { value.phase = "MODEL"; }],
    ["unknown constraint", (value) => { value.constraint_id = "SECRET_VALUE"; }],
    ["missing seal", (value) => { delete value.ir; }],
    ["extra field", (value) => { value.details = "secret"; }],
    ["invalid size", (value) => { value.ir.size_bytes = 0; }],
    ["invalid digest", (value) => { value.ir.sha256 = "x".repeat(64); }],
  ];
  for (const [name, mutate] of mutations) {
    await context.test(name, () => {
      const changed = structuredClone(diagnostic);
      mutate(changed);
      assert.equal(validSkillGenerationRuleIrDiagnostic(changed), false);
      assert.equal(validSkillGenerationFailedTraceAuditReceipt({ ...receipt, diagnostic: changed }), false);
    });
  }
});

test("GenerationSpec diagnostics identify every frozen constraint family with catalog metadata", async (context) => {
  const changed = (mutate) => {
    const value = minimalValidSubmission();
    mutate(value);
    return value;
  };
  const cases = [
    ["root type", () => [], {
      constraint_id: "ROOT_TYPE",
      schema_pointer: "#/type",
      keyword: "type",
      expected_kind: "object",
      expected_count: null,
      actual_kind: "array",
      actual_count: null,
    }],
    ["required root property", () => changed((value) => { delete value.id; }), {
      constraint_id: "ROOT_REQUIRED_ID",
      schema_pointer: "#/required",
      keyword: "required",
      expected_kind: "present",
      expected_count: null,
      actual_kind: "missing",
      actual_count: null,
    }],
    ["additional root property", () => changed((value) => { value.unexpected = true; }), {
      constraint_id: "ROOT_ADDITIONAL_PROPERTIES",
      schema_pointer: "#/additionalProperties",
      keyword: "additionalProperties",
      expected_kind: null,
      expected_count: 0,
      actual_kind: null,
      actual_count: 1,
    }],
    ["root const", () => changed((value) => { value.schema_version = 7; }), {
      constraint_id: "ROOT_CONST_SCHEMA_VERSION",
      schema_pointer: "#/properties/schema_version/const",
      keyword: "const",
      expected_kind: "frozen_constant",
      expected_count: null,
      actual_kind: "integer",
      actual_count: null,
    }],
    ["root string type", () => changed((value) => { value.id = []; }), {
      constraint_id: "ROOT_TYPE_ID",
      schema_pointer: "#/properties/id/type",
      keyword: "type",
      expected_kind: "string",
      expected_count: null,
      actual_kind: "array",
      actual_count: null,
    }],
    ["root string minimum length", () => changed((value) => { value.id = ""; }), {
      constraint_id: "ROOT_MIN_LENGTH_ID",
      schema_pointer: "#/properties/id/minLength",
      keyword: "minLength",
      expected_kind: null,
      expected_count: 1,
      actual_kind: null,
      actual_count: 0,
    }],
    ["root object-array type", () => changed((value) => { value.roles = {}; }), {
      constraint_id: "ROOT_TYPE_ROLES",
      schema_pointer: "#/properties/roles/type",
      keyword: "type",
      expected_kind: "array",
      expected_count: null,
      actual_kind: "object",
      actual_count: null,
    }],
    ["root object-array minimum", () => changed((value) => { value.roles = []; }), {
      constraint_id: "ROOT_MIN_ITEMS_ROLES",
      schema_pointer: "#/properties/roles/minItems",
      keyword: "minItems",
      expected_kind: null,
      expected_count: 2,
      actual_kind: null,
      actual_count: 0,
    }],
    ["root object-array maximum", () => changed((value) => { value.roles.push({}); }), {
      constraint_id: "ROOT_MAX_ITEMS_ROLES",
      schema_pointer: "#/properties/roles/maxItems",
      keyword: "maxItems",
      expected_kind: null,
      expected_count: 2,
      actual_kind: null,
      actual_count: 3,
    }],
    ["root object-array item type", () => changed((value) => { value.roles[0] = []; }), {
      constraint_id: "ROOT_ITEM_TYPE_ROLES",
      schema_pointer: "#/properties/roles/items/type",
      keyword: "type",
      expected_kind: "object",
      expected_count: null,
      actual_kind: "array",
      actual_count: null,
    }],
    ["logparse plan type", () => changed((value) => { value.logparse_plan = []; }), {
      constraint_id: "LOGPARSE_PLAN_TYPE",
      schema_pointer: "#/properties/logparse_plan/type",
      keyword: "type",
      expected_kind: "object",
      expected_count: null,
      actual_kind: "array",
      actual_count: null,
    }],
    ["logparse anchors required", () => changed((value) => { value.logparse_plan = {}; }), {
      constraint_id: "LOGPARSE_PLAN_REQUIRED_ANCHORS",
      schema_pointer: "#/properties/logparse_plan/required",
      keyword: "required",
      expected_kind: "present",
      expected_count: null,
      actual_kind: "missing",
      actual_count: null,
    }],
    ["logparse plan exact properties", () => changed((value) => { value.logparse_plan.unexpected = true; }), {
      constraint_id: "LOGPARSE_PLAN_ADDITIONAL_PROPERTIES",
      schema_pointer: "#/properties/logparse_plan/additionalProperties",
      keyword: "additionalProperties",
      expected_kind: null,
      expected_count: 0,
      actual_kind: null,
      actual_count: 1,
    }],
    ["logparse anchors type", () => changed((value) => { value.logparse_plan.anchors = {}; }), {
      constraint_id: "LOGPARSE_PLAN_ANCHORS_TYPE",
      schema_pointer: "#/properties/logparse_plan/properties/anchors/type",
      keyword: "type",
      expected_kind: "array",
      expected_count: null,
      actual_kind: "object",
      actual_count: null,
    }],
    ["logparse anchors minimum", () => changed((value) => { value.logparse_plan.anchors = []; }), {
      constraint_id: "LOGPARSE_PLAN_ANCHORS_MIN_ITEMS",
      schema_pointer: "#/properties/logparse_plan/properties/anchors/minItems",
      keyword: "minItems",
      expected_kind: null,
      expected_count: 2,
      actual_kind: null,
      actual_count: 0,
    }],
    ["logparse anchors maximum", () => changed((value) => { value.logparse_plan.anchors.push({}); }), {
      constraint_id: "LOGPARSE_PLAN_ANCHORS_MAX_ITEMS",
      schema_pointer: "#/properties/logparse_plan/properties/anchors/maxItems",
      keyword: "maxItems",
      expected_kind: null,
      expected_count: 2,
      actual_kind: null,
      actual_count: 3,
    }],
    ["logparse anchors item type", () => changed((value) => { value.logparse_plan.anchors[0] = "bad"; }), {
      constraint_id: "LOGPARSE_PLAN_ANCHORS_ITEM_TYPE",
      schema_pointer: "#/properties/logparse_plan/properties/anchors/items/type",
      keyword: "type",
      expected_kind: "object",
      expected_count: null,
      actual_kind: "string",
      actual_count: null,
    }],
    ["verification type", () => changed((value) => { value.verification_contract = []; }), {
      constraint_id: "VERIFICATION_TYPE",
      schema_pointer: "#/properties/verification_contract/type",
      keyword: "type",
      expected_kind: "object",
      expected_count: null,
      actual_kind: "array",
      actual_count: null,
    }],
    ["verification required property", () => changed((value) => { delete value.verification_contract.rules; }), {
      constraint_id: "VERIFICATION_REQUIRED_RULES",
      schema_pointer: "#/properties/verification_contract/required",
      keyword: "required",
      expected_kind: "present",
      expected_count: null,
      actual_kind: "missing",
      actual_count: null,
    }],
    ["verification exact properties", () => changed((value) => { value.verification_contract.unexpected = true; }), {
      constraint_id: "VERIFICATION_ADDITIONAL_PROPERTIES",
      schema_pointer: "#/properties/verification_contract/additionalProperties",
      keyword: "additionalProperties",
      expected_kind: null,
      expected_count: 0,
      actual_kind: null,
      actual_count: 1,
    }],
    ["verification const", () => changed((value) => { value.verification_contract.schema_version = 3; }), {
      constraint_id: "VERIFICATION_CONST_SCHEMA_VERSION",
      schema_pointer: "#/properties/verification_contract/properties/schema_version/const",
      keyword: "const",
      expected_kind: "frozen_constant",
      expected_count: null,
      actual_kind: "integer",
      actual_count: null,
    }],
    ["verification array type", () => changed((value) => { value.verification_contract.rules = {}; }), {
      constraint_id: "VERIFICATION_TYPE_RULES",
      schema_pointer: "#/properties/verification_contract/properties/rules/type",
      keyword: "type",
      expected_kind: "array",
      expected_count: null,
      actual_kind: "object",
      actual_count: null,
    }],
    ["verification array minimum", () => changed((value) => { value.verification_contract.rules.pop(); }), {
      constraint_id: "VERIFICATION_MIN_ITEMS_RULES",
      schema_pointer: "#/properties/verification_contract/properties/rules/minItems",
      keyword: "minItems",
      expected_kind: null,
      expected_count: 165,
      actual_kind: null,
      actual_count: 164,
    }],
    ["verification array maximum", () => changed((value) => { value.verification_contract.rules.push({}); }), {
      constraint_id: "VERIFICATION_MAX_ITEMS_RULES",
      schema_pointer: "#/properties/verification_contract/properties/rules/maxItems",
      keyword: "maxItems",
      expected_kind: null,
      expected_count: 165,
      actual_kind: null,
      actual_count: 166,
    }],
    ["verification array item type", () => changed((value) => { value.verification_contract.rules[0] = "bad"; }), {
      constraint_id: "VERIFICATION_ITEM_TYPE_RULES",
      schema_pointer: "#/properties/verification_contract/properties/rules/items/type",
      keyword: "type",
      expected_kind: "object",
      expected_count: null,
      actual_kind: "string",
      actual_count: null,
    }],
    ["root string-array type", () => changed((value) => { value.analysis_steps = {}; }), {
      constraint_id: "ROOT_TYPE_ANALYSIS_STEPS",
      schema_pointer: "#/properties/analysis_steps/type",
      keyword: "type",
      expected_kind: "array",
      expected_count: null,
      actual_kind: "object",
      actual_count: null,
    }],
    ["root string-array minimum", () => changed((value) => { value.analysis_steps.pop(); }), {
      constraint_id: "ROOT_MIN_ITEMS_ANALYSIS_STEPS",
      schema_pointer: "#/properties/analysis_steps/minItems",
      keyword: "minItems",
      expected_kind: null,
      expected_count: 5,
      actual_kind: null,
      actual_count: 4,
    }],
    ["root string-array maximum", () => changed((value) => { value.analysis_steps.push("extra"); }), {
      constraint_id: "ROOT_MAX_ITEMS_ANALYSIS_STEPS",
      schema_pointer: "#/properties/analysis_steps/maxItems",
      keyword: "maxItems",
      expected_kind: null,
      expected_count: 5,
      actual_kind: null,
      actual_count: 6,
    }],
    ["root string-array item type", () => changed((value) => { value.analysis_steps[0] = 1; }), {
      constraint_id: "ROOT_ITEM_TYPE_ANALYSIS_STEPS",
      schema_pointer: "#/properties/analysis_steps/items/type",
      keyword: "type",
      expected_kind: "string",
      expected_count: null,
      actual_kind: "integer",
      actual_count: null,
    }],
    ["root string-array item minimum length", () => changed((value) => { value.analysis_steps[0] = ""; }), {
      constraint_id: "ROOT_ITEM_MIN_LENGTH_ANALYSIS_STEPS",
      schema_pointer: "#/properties/analysis_steps/items/minLength",
      keyword: "minLength",
      expected_kind: null,
      expected_count: 1,
      actual_kind: null,
      actual_count: 0,
    }],
  ];

  for (const [name, build, expectedViolation] of cases) {
    await context.test(name, () => {
      const diagnostic = buildGenerationSpecSubmissionDiagnostic(build());
      assert.deepEqual(diagnostic, {
        schema_version: 1,
        status: "INVALID",
        violations: [expectedViolation],
      });
      assert.equal(validGenerationSpecSubmissionDiagnostic(diagnostic), true);
    });
  }
});

test("GenerationSpec diagnostics are deterministic, bounded, and never echo dynamic names or values", () => {
  const secret = "diagnostic-secret-must-not-be-retained";
  const dynamicName = `dynamic-${secret}`;
  const input = minimalValidSubmission();
  delete input.summary;
  input.schema_version = 7;
  input.id = "";
  input.roles = [];
  input.logparse_plan = { [dynamicName]: secret };
  input.verification_contract.rules = [];
  input.verification_contract[dynamicName] = secret;
  input.analysis_steps[0] = "";
  input[dynamicName] = secret;

  const first = buildGenerationSpecSubmissionDiagnostic(input);
  const second = buildGenerationSpecSubmissionDiagnostic(structuredClone(input));
  assert.deepEqual(second, first);
  assert.deepEqual(first.violations.map((violation) => violation.constraint_id), [
    "ROOT_REQUIRED_SUMMARY",
    "ROOT_ADDITIONAL_PROPERTIES",
    "ROOT_CONST_SCHEMA_VERSION",
    "ROOT_MIN_LENGTH_ID",
    "ROOT_MIN_ITEMS_ROLES",
    "LOGPARSE_PLAN_REQUIRED_ANCHORS",
    "LOGPARSE_PLAN_ADDITIONAL_PROPERTIES",
    "VERIFICATION_ADDITIONAL_PROPERTIES",
    "VERIFICATION_MIN_ITEMS_RULES",
    "ROOT_ITEM_MIN_LENGTH_ANALYSIS_STEPS",
  ]);
  assert.equal(validGenerationSpecSubmissionDiagnostic(first), true);
  assert.doesNotMatch(JSON.stringify(first), new RegExp(secret, "u"));
  assert.doesNotMatch(JSON.stringify(first), /dynamic-/u);

  const schemaValidToolRejection = buildGenerationSpecSubmissionDiagnostic(minimalValidSubmission());
  assert.deepEqual(schemaValidToolRejection, {
    schema_version: 1,
    status: "SCHEMA_VALID_TOOL_REJECTED",
    violations: [],
  });
  assert.equal(validGenerationSpecSubmissionDiagnostic(schemaValidToolRejection), true);
});

test("the GenerationSpec diagnostic validator rejects extensions, forged catalog data, disorder, overflow, and stripping", () => {
  const invalidInput = minimalValidSubmission();
  invalidInput.id = "";
  invalidInput.roles = [];
  const receipt = buildGenerationSpecSubmissionDiagnostic(invalidInput);
  assert.equal(receipt.violations.length, 2);
  assert.equal(validGenerationSpecSubmissionDiagnostic(receipt), true);

  const withExtraRoot = structuredClone(receipt);
  withExtraRoot.extra = true;
  const withRawViolation = structuredClone(receipt);
  withRawViolation.violations[0].raw = "forbidden";
  const forgedPointer = structuredClone(receipt);
  forgedPointer.violations[0].schema_pointer = "#/properties/forged/type";
  const outOfOrder = structuredClone(receipt);
  outOfOrder.violations.reverse();
  const overCap = structuredClone(receipt);
  overCap.violations = Array.from({ length: 65 }, () => structuredClone(receipt.violations[0]));
  const strippedViolation = structuredClone(receipt);
  delete strippedViolation.violations[0].actual_count;
  const strippedRoot = {
    schema_version: 1,
    status: "INVALID",
    violations: [],
  };
  for (const candidate of [
    withExtraRoot,
    withRawViolation,
    forgedPointer,
    outOfOrder,
    overCap,
    strippedViolation,
    strippedRoot,
    {},
  ]) {
    assert.equal(validGenerationSpecSubmissionDiagnostic(candidate), false);
  }
});

test("audits a complete, confined Skill-generation trace and returns only relative paths", () => {
  const fixture = arrangeValid();
  try {
    const receipt = auditSkillGenerationTrace(fixture);
    assert.equal(receipt.schema_version, SKILL_GENERATION_TRACE_SCHEMA_VERSION);
    assert.equal(receipt.status, "PASS");
    assert.equal(receipt.workflow, "skill-generation");
    assert.deepEqual(receipt.tool_inventory, ["Skill", "Read", "Write"]);
    assert.equal(receipt.permission_mode, "dontAsk");
    assert.match(receipt.permission_policy_sha256, /^[a-f0-9]{64}$/);
    assert.equal(receipt.attempt_policy.version, "skill-generation-tool-attempts-v2");
    assert.equal(receipt.attempt_policy.max_empty_write_rejections, 1);
    assert.match(receipt.attempt_policy_sha256, /^[a-f0-9]{64}$/);
    assert.deepEqual(receipt.accepted_validation_rejections, []);
    assert.deepEqual(receipt.tool_sequence.map((item) => item.tool), ["Skill", "Read", "Read", "Read", "Read", "Read", "Write"]);
    assert.ok(receipt.tool_sequence.every((item) => item.outcome === "SUCCESS"));
    assert.deepEqual(receipt.required_reads, [
      "workspace/inputs/wiki.md",
      "workspace/inputs/clarifications.md",
      "skill/references/generation-spec-v6-reference.md",
      "skill/references/verification-contract-v2-reference.md",
    ]);
    assert.equal(receipt.output.path, "workspace/output/generation-spec.json");
    assert.equal(receipt.output.size_bytes, Buffer.byteLength(fixture.content));
    assert.match(receipt.output.sha256, /^[a-f0-9]{64}$/);
    assert.equal(validSkillGenerationTraceAuditReceipt(receipt), false);
    for (const item of receipt.tool_sequence) {
      if (item.path) assert.equal(path.posix.isAbsolute(item.path) || path.win32.isAbsolute(item.path), false);
    }
    assert.doesNotMatch(JSON.stringify(receipt), new RegExp(fixture.workspaceRoot.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    assert.doesNotMatch(JSON.stringify(receipt), new RegExp(fixture.skillRoot.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("audits one compact IR submission and seals the v7 compiler closure", () => {
  const fixture = arrangeValidPhase();
  try {
    const receipt = auditSkillGenerationTrace(fixture);
    assert.equal(receipt.schema_version, SKILL_GENERATION_TRACE_SCHEMA_VERSION);
    assert.deepEqual(receipt.attempt_policy, SKILL_GENERATION_TOOL_ATTEMPT_POLICY);
    assert.equal(receipt.attempt_policy.schema_version, 5);
    assert.equal(receipt.attempt_policy.version, "skill-generation-tool-attempts-v6");
    assert.equal(receipt.attempt_policy.structured_output_payload, "GenerationBlueprint-v1");
    assert.equal(
      receipt.attempt_policy.deterministic_compiler,
      `${SKILL_GENERATION_RULE_IR.compiler_id}@${SKILL_GENERATION_RULE_IR.compiler_version}`,
    );
    assert.equal(receipt.attempt_policy.terminal_result, "DONE");
    assert.equal(receipt.attempt_policy.max_empty_write_rejections, 0);
    assert.deepEqual(receipt.required_reads, SKILL_GENERATION_PHASE_REQUIRED_RECEIPT_READS);
    assert.deepEqual(receipt.tool_sequence, [
      { ordinal: 0, tool: "Skill", outcome: "SUCCESS" },
      ...SKILL_GENERATION_PHASE_REQUIRED_RECEIPT_READS.map((readPath, index) => ({
        ordinal: index + 1,
        tool: "Read",
        outcome: "SUCCESS",
        path: readPath,
      })),
      { ordinal: 9, tool: "StructuredOutput", outcome: "SUCCESS" },
    ]);
    const canonicalIr = canonicalJson(fixture.structuredOutput);
    const canonicalOutput = canonicalJson(fixture.compilation.spec);
    assert.deepEqual(receipt.tool_inventory, ["Skill", "Read", "StructuredOutput"]);
    assert.deepEqual(receipt.ir_input, {
      ordinal: 9,
      size_bytes: Buffer.byteLength(canonicalIr),
      sha256: sha256Bytes(canonicalIr),
    });
    assert.deepEqual(receipt.compiler, fixture.compilation.compiler);
    assert.deepEqual(receipt.output, {
      ordinal: 9,
      path: "workspace/output/generation-spec.json",
      size_bytes: Buffer.byteLength(canonicalOutput),
      sha256: sha256Bytes(canonicalOutput),
    });
    assert.deepEqual(JSON.parse(canonicalIr), fixture.structuredOutput);
    assert.equal(fs.existsSync(path.join(fixture.workspaceRoot, "output", "generation-spec.json")), false);
    assert.deepEqual(receipt.accepted_validation_rejections, []);
    assert.deepEqual(receipt.linked_references, [
      ...SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES.map((relative) => `skill/${relative}`),
      "skill/references/generation-spec-v6-reference.md",
      "skill/references/verification-contract-v2-reference.md",
    ]);
    assert.equal(validSkillGenerationTraceAuditReceipt(receipt), true);
    const wikiUse = fixture.events.findIndex((event) => event?.message?.content?.some((block) => block?.id === "wiki"));
    const clarificationsUse = fixture.events.findIndex((event) => event?.message?.content?.some((block) => block?.id === "clarifications"));
    assert.equal(wikiUse, clarificationsUse);
    assert.deepEqual(receipt.terminal, { subtype: "success", is_error: false });
    assert.equal(Object.hasOwn(receipt.terminal, "result"), false);
    assert.doesNotMatch(JSON.stringify(receipt), /control_only|checkpoint:|file_path|content/u);

    const downgraded = structuredClone(receipt);
    downgraded.attempt_policy = {
      ...downgraded.attempt_policy,
      version: "skill-generation-tool-attempts-v2",
      max_empty_write_rejections: 1,
    };
    assert.equal(validSkillGenerationTraceAuditReceipt(downgraded), false);

    const reordered = structuredClone(receipt);
    [reordered.tool_sequence[6], reordered.tool_sequence[7]] = [reordered.tool_sequence[7], reordered.tool_sequence[6]];
    assert.equal(validSkillGenerationTraceAuditReceipt(reordered), false);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("rejects any broken IR, compiler, or expanded-output seal before a PASS receipt", async (context) => {
  const fixture = arrangeValidPhase();
  try {
    const cases = [
      ["IR seal", (value) => { value.ir.sha256 = "0".repeat(64); }],
      ["compiler identity", (value) => { value.compiler.version = "9.9.9"; }],
      ["expanded output seal", (value) => { value.output.size_bytes += 1; }],
      ["expanded output shape", (value) => { value.spec.verification_contract.rules.pop(); }],
    ];
    for (const [name, mutate] of cases) {
      await context.test(name, () => {
        const compilation = structuredClone(fixture.compilation);
        mutate(compilation);
        errorCode(
          () => auditSkillGenerationTrace({ ...fixture, compilation }),
          CODES.RULE_IR_INVALID,
        );
      });
    }
    await context.test("missing compilation", () => {
      errorCode(
        () => auditSkillGenerationTrace({ ...fixture, compilation: null }),
        CODES.RULE_IR_INVALID,
      );
    });
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("audits every completed terminal-less production checkpoint and the initial pending batch", async (context) => {
  const fixture = arrangeValidPhase();
  try {
    for (let ordinal = 0; ordinal < PHASE_TOOL_IDS.length; ordinal += 1) {
      await context.test(`completed ordinal ${ordinal}`, () => {
        const events = phasePrefixAfterCompletedOrdinal(fixture.events, ordinal);
        const outcomes = ordinal === 1
          ? ["SUCCESS", "SUCCESS", "PENDING"]
          : Array.from({ length: ordinal + 1 }, () => "SUCCESS");
        const receipt = auditIncompleteSkillGenerationTrace({ ...fixture, events });
        assert.deepEqual(
          receipt,
          expectedIncompleteReceipt(phaseIncompleteRecords(fixture.structuredOutput, outcomes), events),
        );
        assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(receipt), true);
        assert.equal(validSkillGenerationFailedTraceAuditReceipt(receipt), true);
        assert.equal(validSkillGenerationTraceAuditReceipt(receipt), false);
        assert.equal(Object.hasOwn(receipt, "terminal"), false);
      });
    }

    await context.test("initial wiki and clarifications batch both pending", () => {
      const batchUseIndex = fixture.events.findIndex((event) => event?.message?.content?.some(
        (block) => block?.type === "tool_use" && block.id === "wiki",
      ));
      const events = structuredClone(fixture.events.slice(0, batchUseIndex + 1));
      const receipt = auditIncompleteSkillGenerationTrace({ ...fixture, events });
      assert.deepEqual(receipt, expectedIncompleteReceipt(phaseIncompleteRecords(
        fixture.structuredOutput,
        ["SUCCESS", "PENDING", "PENDING"],
      ), events));
      assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(receipt), true);
    });

    await context.test("latest serial Read may be recorded as ERROR", () => {
      const events = phasePrefixAfterCompletedOrdinal(fixture.events, 3);
      events.at(-1).message.content[0].is_error = true;
      const receipt = auditIncompleteSkillGenerationTrace({ ...fixture, events });
      assert.deepEqual(receipt, expectedIncompleteReceipt(phaseIncompleteRecords(
        fixture.structuredOutput,
        ["SUCCESS", "SUCCESS", "SUCCESS", "ERROR"],
      ), events));
      assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(receipt), true);
    });
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("hashes pending and completed StructuredOutput prefixes and diagnoses a second attempt", async (context) => {
  const fixture = arrangeValidPhase();
  try {
    await context.test("first StructuredOutput pending", () => {
      const structuredOutputUse = fixture.events.findIndex((event) => event?.message?.content?.some(
        (block) => block?.type === "tool_use" && block.id === "structured-output",
      ));
      const events = structuredClone(fixture.events.slice(0, structuredOutputUse + 1));
      const receipt = auditIncompleteSkillGenerationTrace({ ...fixture, events });
      assert.deepEqual(receipt, expectedIncompleteReceipt(phaseIncompleteRecords(
        fixture.structuredOutput,
        [...Array.from({ length: 9 }, () => "SUCCESS"), "PENDING"],
      ), events));
      assert.equal(Object.hasOwn(receipt.tool_sequence.at(-1), "diagnostic"), false);
      assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(receipt), true);
    });

    await context.test("first StructuredOutput result", () => {
      const events = phasePrefixAfterCompletedOrdinal(fixture.events, 9);
      const receipt = auditIncompleteSkillGenerationTrace({ ...fixture, events });
      assert.deepEqual(receipt, expectedIncompleteReceipt(phaseIncompleteRecords(
        fixture.structuredOutput,
        Array.from({ length: 10 }, () => "SUCCESS"),
      ), events));
      assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(receipt), true);
    });

    await context.test("invalid first StructuredOutput error is diagnosed before an undiagnosed second pending attempt", () => {
      const invalidSubmission = minimalValidBlueprint();
      invalidSubmission.verification.expected_counts.total_rules = 164;
      const completeEvents = validPhaseEvents(fixture.workspaceRoot, fixture.skillRoot, invalidSubmission);
      const events = phasePrefixAfterCompletedOrdinal(completeEvents, 9);
      events[events.length - 1] = toolResult("structured-output", "StructuredOutput", { error: true });
      events.push(toolUse("structured-output-again", "StructuredOutput", minimalValidBlueprint()));
      const receipt = auditIncompleteSkillGenerationTrace({ ...fixture, events });
      assert.equal(receipt.code, CODES.INCOMPLETE_PREFIX_SEQUENCE_INVALID);
      assert.deepEqual(receipt.tool_sequence[9].diagnostic, buildGenerationBlueprintSubmissionDiagnostic(invalidSubmission));
      assert.equal(receipt.tool_sequence[9].diagnostic.status, "INVALID_IR");
      assert.equal(receipt.tool_sequence[10].outcome, "PENDING");
      assert.equal(Object.hasOwn(receipt.tool_sequence[10], "diagnostic"), false);
      assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(receipt), true);
      assert.equal(validSkillGenerationFailedTraceAuditReceipt(receipt), true);
      assert.equal(validSkillGenerationTraceAuditReceipt(receipt), false);
      const strippedError = structuredClone(receipt);
      delete strippedError.tool_sequence[9].diagnostic;
      assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(strippedError), false);
      const annotatedPending = structuredClone(receipt);
      annotatedPending.tool_sequence[10].diagnostic = buildGenerationBlueprintSubmissionDiagnostic(minimalValidBlueprint());
      assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(annotatedPending), false);
    });

    await context.test("schema-valid first StructuredOutput rejection records an empty valid-tool diagnostic", () => {
      const events = phasePrefixAfterCompletedOrdinal(fixture.events, 9);
      events[events.length - 1] = toolResult("structured-output", "StructuredOutput", { error: true });
      const receipt = auditIncompleteSkillGenerationTrace({ ...fixture, events });
      assert.deepEqual(receipt.tool_sequence.at(-1).diagnostic, {
        schema_version: 1,
        status: "SCHEMA_VALID_TOOL_REJECTED",
      });
      assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(receipt), true);
    });

    for (const outcome of ["PENDING", "SUCCESS"]) {
      await context.test(`second StructuredOutput ${outcome.toLowerCase()}`, () => {
        const secondSubmission = { ...structuredClone(fixture.structuredOutput), id: "second-submission" };
        const events = phasePrefixAfterCompletedOrdinal(fixture.events, 9);
        events.push(toolUse("structured-output-again", "StructuredOutput", secondSubmission));
        if (outcome === "SUCCESS") events.push(toolResult("structured-output-again", "StructuredOutput"));
        const secondCanonical = canonicalJson(secondSubmission);
        const toolSequence = phaseIncompleteRecords(
          fixture.structuredOutput,
          Array.from({ length: 10 }, () => "SUCCESS"),
        );
        toolSequence.push({
          ordinal: 10,
          tool: "StructuredOutput",
          outcome,
          size_bytes: Buffer.byteLength(secondCanonical),
          sha256: sha256Bytes(secondCanonical),
        });
        const receipt = auditIncompleteSkillGenerationTrace({ ...fixture, events });
        assert.deepEqual(receipt, expectedIncompleteReceipt(
          toolSequence,
          events,
          CODES.INCOMPLETE_PREFIX_SEQUENCE_INVALID,
        ));
        assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(receipt), true);
        assert.equal(validSkillGenerationFailedTraceAuditReceipt(receipt), true);
        assert.equal(validSkillGenerationTraceAuditReceipt(receipt), false);
      });
    }

    await context.test("StructuredOutput content is retained only as canonical size and hash", () => {
      const secret = "terminal-less-structured-output-secret";
      const structuredOutput = minimalValidBlueprint();
      structuredOutput.spec.summary = secret;
      const completeEvents = validPhaseEvents(fixture.workspaceRoot, fixture.skillRoot, structuredOutput);
      const structuredOutputUse = completeEvents.findIndex((event) => event?.message?.content?.some(
        (block) => block?.type === "tool_use" && block.id === "structured-output",
      ));
      const events = structuredClone(completeEvents.slice(0, structuredOutputUse + 1));
      const receipt = auditIncompleteSkillGenerationTrace({ ...fixture, events });
      const canonical = canonicalJson(structuredOutput);
      assert.deepEqual(receipt.tool_sequence.at(-1), {
        ordinal: 9,
        tool: "StructuredOutput",
        outcome: "PENDING",
        size_bytes: Buffer.byteLength(canonical),
        sha256: sha256Bytes(canonical),
      });
      assert.doesNotMatch(JSON.stringify(receipt), new RegExp(secret, "u"));
      assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(receipt), true);
    });
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("terminal-less audit fails closed on unsafe inputs and its receipt validator rejects tampering", async (context) => {
  const fixture = arrangeValidPhase();
  try {
    const batchUseIndex = fixture.events.findIndex((event) => event?.message?.content?.some(
      (block) => block?.type === "tool_use" && block.id === "wiki",
    ));
    const batchPending = structuredClone(fixture.events.slice(0, batchUseIndex + 1));

    await context.test("unsafe Read path", () => {
      const events = structuredClone(batchPending);
      events.at(-1).message.content[0].input.file_path = "inputs/../inputs/wiki.md";
      assert.throws(() => auditIncompleteSkillGenerationTrace({ ...fixture, events }));
    });

    await context.test("null Read input", () => {
      const events = structuredClone(batchPending);
      events.at(-1).message.content[0].input = null;
      assert.throws(() => auditIncompleteSkillGenerationTrace({ ...fixture, events }));
    });

    await context.test("null StructuredOutput input", () => {
      const structuredOutputUse = fixture.events.findIndex((event) => event?.message?.content?.some(
        (block) => block?.type === "tool_use" && block.id === "structured-output",
      ));
      const events = structuredClone(fixture.events.slice(0, structuredOutputUse + 1));
      events.at(-1).message.content[0].input = null;
      assert.throws(() => auditIncompleteSkillGenerationTrace({ ...fixture, events }));
    });

    await context.test("ordinal ten cannot be an extra Read", () => {
      const events = phasePrefixAfterCompletedOrdinal(fixture.events, 9);
      events.push(toolUse("extra-read", "Read", {
        file_path: path.join(fixture.workspaceRoot, "inputs", "wiki.md"),
      }));
      assert.throws(() => auditIncompleteSkillGenerationTrace({ ...fixture, events }));
    });

    await context.test("serial Read paths cannot be exchanged", () => {
      const events = phasePrefixAfterCompletedOrdinal(fixture.events, 4);
      const generation = events.find((event) => event?.message?.content?.some((block) => block?.id === "generation"));
      const verification = events.find((event) => event?.message?.content?.some((block) => block?.id === "verification"));
      [generation.message.content[0].input.file_path, verification.message.content[0].input.file_path] = [
        verification.message.content[0].input.file_path,
        generation.message.content[0].input.file_path,
      ];
      assert.throws(() => auditIncompleteSkillGenerationTrace({ ...fixture, events }));
    });

    await context.test("contradictory tool result cannot enter a prefix receipt", () => {
      const events = phasePrefixAfterCompletedOrdinal(fixture.events, 3);
      events.at(-1).message.content[0].is_error = true;
      events.at(-1).tool_use_result.success = true;
      assert.throws(() => auditIncompleteSkillGenerationTrace({ ...fixture, events }));
    });

    await context.test("stream and tool receipt tampering", () => {
      const events = phasePrefixAfterCompletedOrdinal(fixture.events, 8);
      const receipt = auditIncompleteSkillGenerationTrace({ ...fixture, events });
      assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(receipt), true);
      assert.equal(validSkillGenerationFailedTraceAuditReceipt(receipt), true);
      assert.equal(validSkillGenerationTraceAuditReceipt(receipt), false);

      const tampered = [
        { ...structuredClone(receipt), terminal: { subtype: "success", is_error: false } },
        { ...structuredClone(receipt), stream_state: "OPEN" },
        { ...structuredClone(receipt), code: CODES.INCOMPLETE_PREFIX_SEQUENCE_INVALID },
        { ...structuredClone(receipt), stream: { ...receipt.stream, event_count: receipt.stream.event_count + 1 } },
        { ...structuredClone(receipt), stream: { ...receipt.stream, parsed_event_count: receipt.stream.parsed_event_count + 1 } },
        { ...structuredClone(receipt), stream: { ...receipt.stream, init_count: 0 } },
        { ...structuredClone(receipt), stream: { ...receipt.stream, result_count: 1 } },
        { ...structuredClone(receipt), stream: { ...receipt.stream, last_event_type: "assistant/content" } },
        { ...structuredClone(receipt), stream: { ...receipt.stream, complete: true } },
        {
          ...structuredClone(receipt),
          tool_sequence: receipt.tool_sequence.map((record, index) => (
            index === 1 ? { ...record, path: "workspace/inputs/other.md" } : record
          )),
        },
      ];
      for (const candidate of tampered) {
        assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(candidate), false);
        assert.equal(validSkillGenerationFailedTraceAuditReceipt(candidate), false);
        assert.equal(validSkillGenerationTraceAuditReceipt(candidate), false);
      }

      for (const code of [CODES.INCOMPLETE_PREFIX, CODES.INCOMPLETE_PREFIX_SEQUENCE_INVALID]) {
        const stripped = {
          schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
          status: "FAIL",
          workflow: "skill-generation",
          code,
        };
        assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(stripped), false);
        assert.equal(validSkillGenerationFailedTraceAuditReceipt(stripped), false);
        assert.equal(validSkillGenerationTraceAuditReceipt(stripped), false);
      }
    });
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("seals a rejected terminal-less audit as an exact content-free FAIL-only receipt", () => {
  assert.deepEqual(ISOLATED_AGENT_STREAM_EVENT_TYPES, [
    "system", "assistant", "user", "result", "error", "rate_limit_event",
  ]);
  assert.equal(ISOLATED_AGENT_STREAM_EVENT_TYPES.every(validIsolatedAgentStreamEventType), true);
  assert.equal(validIsolatedAgentStreamEventType("secret-token-like-value"), false);
  const stream = {
    schema_version: 1,
    event_count: 32,
    parsed_event_count: 32,
    init_count: 1,
    result_count: 0,
    last_event_type: "assistant",
    complete: false,
  };
  const receipt = buildSkillGenerationIncompleteAuditRejectedReceipt(
    CODES.PHASE_SEQUENCE_INVALID,
    structuredClone(stream),
  );
  assert.deepEqual(receipt, {
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "FAIL",
    workflow: "skill-generation",
    code: CODES.INCOMPLETE_PREFIX_REJECTED,
    audit_code: CODES.PHASE_SEQUENCE_INVALID,
    stream_state: "TERMINAL_MISSING",
    stream,
  });
  assert.equal(validSkillGenerationIncompleteAuditRejectedReceipt(receipt), true);
  assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(receipt), false);
  assert.equal(validSkillGenerationPartialTraceAuditReceipt(receipt), false);
  assert.equal(validSkillGenerationFailedTraceAuditReceipt(receipt), true);
  assert.equal(validSkillGenerationTraceAuditReceipt(receipt), false);
  assert.doesNotMatch(JSON.stringify(receipt), /content|thinking|raw|file_path|message|details|secret/iu);

  const tampered = [
    { ...structuredClone(receipt), status: "PASS" },
    { ...structuredClone(receipt), code: CODES.INCOMPLETE_PREFIX },
    { ...structuredClone(receipt), audit_code: "SKILL_TRACE_NOT_A_FIXED_CODE" },
    { ...structuredClone(receipt), raw: "must-not-pass" },
    { ...structuredClone(receipt), details: { path: "must-not-pass" } },
    { ...structuredClone(receipt), stream_state: "OPEN" },
    { ...structuredClone(receipt), stream: { ...receipt.stream, event_count: 33 } },
    { ...structuredClone(receipt), stream: { ...receipt.stream, result_count: 1 } },
    { ...structuredClone(receipt), stream: { ...receipt.stream, last_event_type: "result" } },
    { ...structuredClone(receipt), stream: { ...receipt.stream, last_event_type: "secret-token-like-value" } },
    { ...structuredClone(receipt), stream: { ...receipt.stream, complete: true } },
  ];
  for (const candidate of tampered) {
    assert.equal(validSkillGenerationIncompleteAuditRejectedReceipt(candidate), false);
    assert.equal(validSkillGenerationFailedTraceAuditReceipt(candidate), false);
    assert.equal(validSkillGenerationTraceAuditReceipt(candidate), false);
  }
  assert.equal(buildSkillGenerationIncompleteAuditRejectedReceipt(
    "SKILL_TRACE_NOT_A_FIXED_CODE",
    stream,
  ), null);
  assert.equal(buildSkillGenerationIncompleteAuditRejectedReceipt(
    CODES.WRITE_CONTENT_MISMATCH,
    stream,
  ), null);
  const errorStream = { ...stream, last_event_type: "error" };
  const errorReceipt = buildSkillGenerationIncompleteAuditRejectedReceipt(
    CODES.STREAM_ERROR,
    errorStream,
  );
  assert.equal(validSkillGenerationIncompleteAuditRejectedReceipt(errorReceipt), true);
  assert.equal(validSkillGenerationIncompleteTraceAuditReceipt({
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "FAIL",
    workflow: "skill-generation",
    code: CODES.INCOMPLETE_PREFIX,
    stream_state: "TERMINAL_MISSING",
    tool_sequence: [],
    stream: errorStream,
  }), false);
  assert.equal(buildSkillGenerationIncompleteAuditRejectedReceipt(
    CODES.PHASE_SEQUENCE_INVALID,
    errorStream,
  ), null);
  assert.equal(validSkillGenerationFailedTraceAuditReceipt({
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "FAIL",
    workflow: "skill-generation",
    code: CODES.INCOMPLETE_PREFIX_REJECTED,
  }), false);
  assert.equal(validSkillGenerationFailedTraceAuditReceipt({
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "FAIL",
    workflow: "skill-generation",
    code: "SKILL_TRACE_UNKNOWN",
  }), false);
});

test("seals an unsuccessful terminal as a content-free partial trace that can never be a public PASS", () => {
  const fixture = phaseWorkspaceFixture();
  const secret = "must-not-enter-partial-trace";
  const structuredOutput = {
    schema_version: 6,
    private_payload: { content: secret, thinking: secret },
  };
  try {
    const events = validPhaseEvents(fixture.workspaceRoot, fixture.skillRoot, structuredOutput);
    events.at(-1).subtype = "error_max_turns";
    events.at(-1).is_error = true;
    delete events.at(-1).structured_output;
    const receipt = auditPartialSkillGenerationTrace({ ...fixture, events });
    const canonical = canonicalJson(structuredOutput);
    assert.deepEqual(receipt, {
      schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
      status: "FAIL",
      workflow: "skill-generation",
      code: CODES.RESULT_NOT_SUCCESS,
      tool_sequence: [
        { ordinal: 0, tool: "Skill", outcome: "SUCCESS" },
        ...SKILL_GENERATION_PHASE_REQUIRED_RECEIPT_READS.map((readPath, index) => ({
          ordinal: index + 1,
          tool: "Read",
          outcome: "SUCCESS",
          path: readPath,
        })),
        {
          ordinal: 9,
          tool: "StructuredOutput",
          outcome: "SUCCESS",
          size_bytes: Buffer.byteLength(canonical),
          sha256: sha256Bytes(canonical),
        },
      ],
      terminal: { subtype: "error_max_turns", is_error: true },
    });
    assert.equal(validSkillGenerationPartialTraceAuditReceipt(receipt), true);
    assert.equal(validSkillGenerationFailedTraceAuditReceipt(receipt), true);
    assert.equal(validSkillGenerationTraceAuditReceipt(receipt), false);
    assert.equal(fs.existsSync(path.join(fixture.workspaceRoot, "output", "generation-spec.json")), false);
    assert.doesNotMatch(JSON.stringify(receipt), new RegExp(secret, "u"));
    assert.doesNotMatch(JSON.stringify(receipt), /private_payload|thinking|content|raw|file_path/iu);

    const injected = structuredClone(receipt);
    injected.tool_sequence.at(-1).content = secret;
    assert.equal(validSkillGenerationPartialTraceAuditReceipt(injected), false);
    assert.equal(validSkillGenerationFailedTraceAuditReceipt(injected), false);

    const unsafeEvents = structuredClone(events);
    unsafeEvents[3].message.content[0].input.file_path = "inputs/../inputs/wiki.md";
    errorCode(() => auditPartialSkillGenerationTrace({ ...fixture, events: unsafeEvents }), CODES.PATH_TRAVERSAL);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("partial terminal receipts diagnose a failed first StructuredOutput but never annotate the successful retry", () => {
  const fixture = phaseWorkspaceFixture();
  const secret = "partial-diagnostic-secret-must-not-leak";
  const invalidSubmission = minimalValidBlueprint();
  invalidSubmission.spec.summary = secret;
  invalidSubmission.verification.expected_counts.total_rules = 164;
  const successfulRetry = minimalValidBlueprint();
  try {
    const events = validPhaseEvents(fixture.workspaceRoot, fixture.skillRoot, invalidSubmission);
    const firstResultIndex = events.findIndex((event) => event?.message?.content?.some(
      (block) => block?.type === "tool_result" && block.tool_use_id === "structured-output",
    ));
    assert.notEqual(firstResultIndex, -1);
    events[firstResultIndex] = toolResult("structured-output", "StructuredOutput", { error: true });
    events.splice(-1, 0, ...invocation("structured-output-again", "StructuredOutput", successfulRetry));
    events.at(-1).subtype = "error_max_structured_output_retries";
    events.at(-1).is_error = true;
    delete events.at(-1).structured_output;

    const receipt = auditPartialSkillGenerationTrace({ ...fixture, events });
    assert.equal(receipt.tool_sequence[9].outcome, "ERROR");
    assert.deepEqual(
      receipt.tool_sequence[9].diagnostic,
      buildGenerationBlueprintSubmissionDiagnostic(invalidSubmission),
    );
    assert.equal(receipt.tool_sequence[9].diagnostic.status, "INVALID_IR");
    assert.equal(receipt.tool_sequence[10].outcome, "SUCCESS");
    assert.equal(Object.hasOwn(receipt.tool_sequence[10], "diagnostic"), false);
    assert.equal(validSkillGenerationPartialTraceAuditReceipt(receipt), true);
    assert.equal(validSkillGenerationFailedTraceAuditReceipt(receipt), true);
    assert.equal(validSkillGenerationTraceAuditReceipt(receipt), false);
    const strippedError = structuredClone(receipt);
    delete strippedError.tool_sequence[9].diagnostic;
    assert.equal(validSkillGenerationPartialTraceAuditReceipt(strippedError), false);
    const annotatedSuccess = structuredClone(receipt);
    annotatedSuccess.tool_sequence[10].diagnostic = buildGenerationBlueprintSubmissionDiagnostic(successfulRetry);
    assert.equal(validSkillGenerationPartialTraceAuditReceipt(annotatedSuccess), false);
    assert.doesNotMatch(JSON.stringify(receipt), new RegExp(secret, "u"));
    assert.doesNotMatch(JSON.stringify(receipt), /content|thinking|raw|file_path/iu);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("rejects Write, non-object StructuredOutput or terminal mismatch without creating output", async (context) => {
  for (const [name, mutate, code] of [
    ["ordinary Write", (events, fixture) => {
      events.at(-3).message.content[0].name = "Write";
      events.at(-3).message.content[0].input = { file_path: path.join(fixture.workspaceRoot, "output", "generation-spec.json"), content: "{}" };
    }, CODES.TOOL_NOT_ALLOWED],
    ["non-object tool input", (events) => { events.at(-3).message.content[0].input = []; }, CODES.STRUCTURED_OUTPUT_INPUT_INVALID],
    ["non-object terminal", (events) => { events.at(-1).structured_output = null; }, CODES.STRUCTURED_OUTPUT_INPUT_INVALID],
    ["submission schema", (events) => { events.at(-3).message.content[0].input.verification.expected_counts.total_rules = 164; }, CODES.STRUCTURED_OUTPUT_SCHEMA_INVALID],
    ["terminal submission schema", (events) => { events.at(-1).structured_output.verification.expected_counts.total_rules = 164; }, CODES.STRUCTURED_OUTPUT_SCHEMA_INVALID],
    ["terminal mismatch", (events, fixture) => {
      events.at(-1).structured_output = structuredClone(fixture.structuredOutput);
      events.at(-1).structured_output.spec.id = "different-id";
    }, CODES.STRUCTURED_OUTPUT_MISMATCH],
    ["terminal result", (events) => { events.at(-1).result = "not-DONE"; }, CODES.RESULT_NOT_SUCCESS],
  ]) {
    await context.test(name, () => {
      const fixture = arrangeValidPhase();
      try {
        const events = structuredClone(fixture.events);
        mutate(events, fixture);
        errorCode(() => auditSkillGenerationTrace({ ...fixture, events }), code);
        assert.equal(fs.existsSync(path.join(fixture.workspaceRoot, "output", "generation-spec.json")), false);
      } finally {
        fs.rmSync(fixture.parent, { recursive: true, force: true });
      }
    });
  }
});

test("phase checkpoint permissions grant six exact Skill reads and no optional or mutation path", () => {
  const fixture = arrangeValidPhase();
  try {
    const linkedReferences = discoverLinkedSkillReferences(fixture.skillRoot);
    const absoluteRule = (relative) => {
      const resolved = path.resolve(fixture.skillRoot, ...relative.split("/"));
      const drive = /^([A-Za-z]):[\\/](.*)$/.exec(resolved);
      const portable = drive
        ? `${drive[1]}/${drive[2].replaceAll("\\", "/")}`
        : resolved.split(path.sep).join("/").replace(/^\/+/, "");
      return `Read(//${portable.replaceAll("\\", "\\\\").replaceAll("(", "\\(").replaceAll(")", "\\)")})`;
    };
    const rules = skillGenerationPermissionRules({ ...fixture, linkedReferences });
    assert.deepEqual(rules, [
      "Skill(wiki-to-diagnosis-skill)",
      "Read(/inputs/wiki.md)",
      "Read(/inputs/clarifications.md)",
      ...[
        "references/generation-spec-v6-reference.md",
        "references/verification-contract-v2-reference.md",
        ...SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES,
      ].map(absoluteRule),
      "StructuredOutput",
    ]);
    assert.equal(rules.filter((rule) => rule.startsWith("Read(")).length, 8);
    assert.equal(rules.filter((rule) => rule.startsWith("Edit(")).length, 0);
    assert.doesNotMatch(rules.join("\n"), /wiki-template|neutral-logparse|Write|Edit|Bash|\*/u);

    fs.appendFileSync(
      path.join(fixture.skillRoot, "SKILL.md"),
      "[forbidden optional](references/ordinary-example.md)\n",
    );
    const injected = discoverLinkedSkillReferences(fixture.skillRoot);
    errorCode(
      () => skillGenerationPermissionRules({ ...fixture, linkedReferences: injected }),
      CODES.REQUIRED_REFERENCE_INVALID,
    );
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("rejects missing reordered partial batched or retried production phase checkpoints", async (context) => {
  const fixture = arrangeValidPhase();
  const useIndex = (events, id) => events.findIndex((event) => event?.message?.content?.some((block) => block?.type === "tool_use" && block.id === id));
  const resultIndex = (events, id) => events.findIndex((event) => event?.message?.content?.some((block) => block?.type === "tool_result" && block.tool_use_id === id));
  try {
    await context.test("serialized initial inputs", () => {
      const events = structuredClone(fixture.events);
      const batchUse = useIndex(events, "wiki");
      events.splice(batchUse, 2,
        ...invocation("wiki", "Read", { file_path: path.join(fixture.workspaceRoot, "inputs", "wiki.md") }),
        ...invocation("clarifications", "Read", { file_path: path.join(fixture.workspaceRoot, "inputs", "clarifications.md") }));
      errorCode(() => auditSkillGenerationTrace({ ...fixture, events }), CODES.PHASE_SEQUENCE_INVALID);
    });
    await context.test("reversed initial batch", () => {
      const events = structuredClone(fixture.events);
      events[useIndex(events, "wiki")].message.content.reverse();
      errorCode(() => auditSkillGenerationTrace({ ...fixture, events }), CODES.PHASE_SEQUENCE_INVALID);
    });
    await context.test("third Read in initial batch", () => {
      const events = structuredClone(fixture.events);
      const batchUse = useIndex(events, "wiki");
      const generationUse = useIndex(events, "generation");
      const [generationEvent] = events.splice(generationUse, 1);
      events[batchUse].message.content.push(generationEvent.message.content[0]);
      errorCode(() => auditSkillGenerationTrace({ ...fixture, events }), CODES.PHASE_SEQUENCE_INVALID);
    });
    await context.test("initial batch result barrier", () => {
      const events = structuredClone(fixture.events);
      const batchResult = resultIndex(events, "wiki");
      const blocks = events[batchResult].message.content;
      const clarificationResultIndex = blocks.findIndex((block) => block.tool_use_id === "clarifications");
      const [clarificationResult] = blocks.splice(clarificationResultIndex, 1);
      const generationUse = useIndex(events, "generation");
      events.splice(generationUse + 1, 0, {
        type: "user",
        message: { role: "user", content: [clarificationResult] },
        tool_use_result: { type: "read" },
      });
      errorCode(() => auditSkillGenerationTrace({ ...fixture, events }), CODES.PHASE_SEQUENCE_INVALID);
    });
    await context.test("missing", () => {
      const events = structuredClone(fixture.events);
      events.splice(useIndex(events, "checkpoint-2"), 2);
      errorCode(() => auditSkillGenerationTrace({ ...fixture, events }), CODES.REQUIRED_READ_MISSING);
    });
    await context.test("reordered", () => {
      const events = structuredClone(fixture.events);
      const second = events[useIndex(events, "checkpoint-2")].message.content[0].input.file_path;
      const third = events[useIndex(events, "checkpoint-3")].message.content[0].input.file_path;
      events[useIndex(events, "checkpoint-2")].message.content[0].input.file_path = third;
      events[useIndex(events, "checkpoint-3")].message.content[0].input.file_path = second;
      errorCode(() => auditSkillGenerationTrace({ ...fixture, events }), CODES.PHASE_SEQUENCE_INVALID);
    });
    await context.test("partial", () => {
      const events = structuredClone(fixture.events);
      events[useIndex(events, "checkpoint-1")].message.content[0].input.limit = 1;
      errorCode(() => auditSkillGenerationTrace({ ...fixture, events }), CODES.REQUIRED_READ_PARTIAL);
    });
    await context.test("batched", () => {
      const events = structuredClone(fixture.events);
      const firstUse = useIndex(events, "checkpoint-1");
      const secondUse = useIndex(events, "checkpoint-2");
      const [secondEvent] = events.splice(secondUse, 1);
      events[firstUse].message.content.push(secondEvent.message.content[0]);
      assert.ok(resultIndex(events, "checkpoint-1") > useIndex(events, "checkpoint-2"));
      errorCode(() => auditSkillGenerationTrace({ ...fixture, events }), CODES.PHASE_SEQUENCE_INVALID);
    });
    await context.test("duplicate", () => {
      const events = structuredClone(fixture.events);
      events.splice(-3, 0, ...invocation("checkpoint-4-again", "Read", {
        file_path: path.join(fixture.skillRoot, ...SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES[3].split("/")),
      }));
      errorCode(() => auditSkillGenerationTrace({ ...fixture, events }), CODES.PHASE_SEQUENCE_INVALID);
    });
    await context.test("empty Write rejection", () => {
      const events = structuredClone(fixture.events);
      events.splice(-3, 0, ...invocation("empty-write", "Write", {}, { error: true }));
      errorCode(() => auditSkillGenerationTrace({ ...fixture, events }), CODES.WRITE_COUNT_INVALID);
    });
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("records one explicit empty Write validation rejection immediately before the only successful Write", () => {
  const fixture = arrangeValid();
  try {
    const events = structuredClone(fixture.events);
    events.splice(-3, 0, ...invocation("empty-write", "Write", {}, { error: true }));
    const receipt = auditSkillGenerationTrace({ ...fixture, events });
    assert.equal(receipt.status, "PASS");
    assert.equal(validSkillGenerationTraceAuditReceipt(receipt), false);
    assert.deepEqual(receipt.tool_sequence.slice(-2), [
      {
        ordinal: receipt.tool_sequence.length - 2,
        tool: "Write",
        outcome: "REJECTED",
        classification: "EMPTY_INPUT_REQUIRED_FIELDS_ABSENT",
      },
      {
        ordinal: receipt.tool_sequence.length - 1,
        tool: "Write",
        outcome: "SUCCESS",
        path: "workspace/output/generation-spec.json",
      },
    ]);
    assert.deepEqual(receipt.accepted_validation_rejections, [{
      ordinal: receipt.tool_sequence.length - 2,
      tool: "Write",
      classification: "EMPTY_INPUT_REQUIRED_FIELDS_ABSENT",
      input_key_names: [],
      result_completed_before_success: true,
    }]);
    assert.equal(receipt.output.ordinal, receipt.tool_sequence.length - 1);
    assert.doesNotMatch(JSON.stringify(receipt), /isError|tool_use_error|InputValidationError|content|file_path/);

    const tampered = structuredClone(receipt);
    tampered.accepted_validation_rejections[0].input_key_names.push("file_path");
    assert.equal(validSkillGenerationTraceAuditReceipt(tampered), false);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("builds exact least-privilege CLI rules with Edit authorizing only the audited Write path", () => {
  const fixture = arrangeValid();
  try {
    const linkedReferences = discoverLinkedSkillReferences(fixture.skillRoot);
    const absoluteRule = (relative) => {
      const resolved = path.resolve(fixture.skillRoot, ...relative.split("/"));
      const drive = /^([A-Za-z]):[\\/](.*)$/.exec(resolved);
      const portable = drive
        ? `${drive[1]}/${drive[2].replaceAll("\\", "/")}`
        : resolved.split(path.sep).join("/").replace(/^\/+/, "");
      const escaped = portable.replaceAll("\\", "\\\\").replaceAll("(", "\\(").replaceAll(")", "\\)");
      return `Read(//${escaped})`;
    };
    assert.deepEqual(skillGenerationPermissionRules({ ...fixture, linkedReferences }), [
      "Skill(wiki-to-diagnosis-skill)",
      "Read(/inputs/wiki.md)",
      "Read(/inputs/clarifications.md)",
      ...linkedReferences.map(absoluteRule),
      "Edit(/output/generation-spec.json)",
    ]);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("permission rules escape special characters in absolute Skill paths", () => {
  const fixture = arrangeValid();
  const parentWithParens = `${fixture.parent} (safe)`;
  try {
    fs.renameSync(fixture.parent, parentWithParens);
    const workspaceRoot = path.join(parentWithParens, "workspace");
    const skillRoot = path.join(parentWithParens, "installed-skill");
    const linkedReferences = discoverLinkedSkillReferences(skillRoot);
    const rules = skillGenerationPermissionRules({ workspaceRoot, skillRoot, linkedReferences });
    assert.ok(rules.some((rule) => rule.includes("\\(safe\\)")));
    if (process.platform === "win32") {
      assert.ok(rules.filter((rule) => rule.startsWith("Read(//")).every((rule) => /^Read\(\/[\/][A-Za-z]\//.test(rule)));
      assert.ok(rules.every((rule) => !/\/\/[A-Za-z]:\//.test(rule)));
    }
  } finally {
    fs.rmSync(parentWithParens, { recursive: true, force: true });
  }
});

test("permission rules reject an unsafe root or incomplete and injected reference lists", () => {
  const fixture = arrangeValid();
  try {
    const linkedReferences = discoverLinkedSkillReferences(fixture.skillRoot);
    errorCode(() => skillGenerationPermissionRules({
      workspaceRoot: "relative-workspace",
      skillRoot: fixture.skillRoot,
      linkedReferences,
    }), CODES.ROOT_INVALID);
    errorCode(() => skillGenerationPermissionRules({
      ...fixture,
      linkedReferences: linkedReferences.slice(1),
    }), CODES.REQUIRED_REFERENCE_INVALID);
    errorCode(() => skillGenerationPermissionRules({
      ...fixture,
      linkedReferences: [...linkedReferences, "references/../unlinked.md"],
    }), CODES.PATH_TRAVERSAL);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("trace audit rejects a workspace nested in the source repository", () => {
  const fixture = arrangeValid();
  try {
    const linkedReferences = discoverLinkedSkillReferences(fixture.skillRoot);
    errorCode(() => skillGenerationPermissionRules({
      ...fixture,
      linkedReferences,
      sourceRoot: fixture.parent,
    }), CODES.ROOT_INVALID);
    errorCode(() => auditSkillGenerationTrace({
      ...fixture,
      sourceRoot: fixture.parent,
    }), CODES.ROOT_INVALID);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("discovers sorted, unique, ordinary direct Skill references", () => {
  const fixture = arrangeValid();
  try {
    assert.deepEqual(discoverLinkedSkillReferences(fixture.skillRoot), [
      "references/generation-spec-v6-reference.md",
      "references/ordinary-example.md",
      "references/verification-contract-v2-reference.md",
    ]);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("the production conversion Skill exposes only two contracts and four frozen checkpoints", () => {
  const skillRoot = path.resolve(".claude", "skills", "wiki-to-diagnosis-skill");
  assert.deepEqual(discoverLinkedSkillReferences(skillRoot), [
    ...SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES,
    "references/generation-spec-v6-reference.md",
    "references/verification-contract-v2-reference.md",
  ]);
  const skill = fs.readFileSync(path.join(skillRoot, "SKILL.md"), "utf8");
  assert.match(skill, /不得 `Read` 这两项可选示例/);
  assert.doesNotMatch(skill, /\[[^\]]+\]\(references\/(?:wiki-template\.md|neutral-logparse-generation-spec-v6\.json)\)/);
  assert.doesNotMatch(skill, /01-begin-q-matrix-and-paths/u);
});

test("reference discovery rejects remote, absolute, traversing, missing, and nonordinary links", async (context) => {
  const cases = [
    ["remote", "[bad](https://example.invalid/reference)", CODES.SKILL_LINK_INVALID],
    ["absolute", `[bad](${path.resolve("outside.md").replaceAll("\\", "/")})`, CODES.SKILL_LINK_INVALID],
    ["traversal", "[bad](references/../../outside.md)", CODES.SKILL_LINK_INVALID],
    ["missing", "[bad](references/missing.md)", CODES.PATH_MISSING],
    ["image-only", "![bad](references/ordinary-example.md)", CODES.SKILL_LINK_INVALID],
    ["reference-style", "[bad][reference]\n\n[reference]: references/ordinary-example.md", CODES.SKILL_LINK_INVALID],
    ["html", "<a href=\"references/ordinary-example.md\">bad</a>", CODES.SKILL_LINK_INVALID],
  ];
  for (const [name, markdown, code] of cases) {
    await context.test(name, () => {
      const fixture = arrangeValid();
      try {
        write(fixture.skillRoot, "SKILL.md", `${markdown}\n`);
        errorCode(() => discoverLinkedSkillReferences(fixture.skillRoot), code);
      } finally {
        fs.rmSync(fixture.parent, { recursive: true, force: true });
      }
    });
  }
});

test("reference discovery and audit reject hardlinked files", { skip: process.platform === "win32" }, async (context) => {
  for (const target of [
    { root: "skillRoot", relative: "references/ordinary-example.md" },
    { root: "workspaceRoot", relative: "inputs/wiki.md" },
    { root: "workspaceRoot", relative: "output/generation-spec.json" },
  ]) {
    await context.test(target.relative, () => {
      const fixture = arrangeValid();
      try {
        const original = path.join(fixture[target.root], ...target.relative.split("/"));
        fs.linkSync(original, path.join(fixture.parent, `hardlink-${path.basename(target.relative)}`));
        const action = target.root === "skillRoot"
          ? () => discoverLinkedSkillReferences(fixture.skillRoot)
          : () => auditSkillGenerationTrace(fixture);
        errorCode(action, CODES.PATH_HARDLINK);
      } finally {
        fs.rmSync(fixture.parent, { recursive: true, force: true });
      }
    });
  }
});

test("requires one init, one final successful result, and the exact tool inventory", () => {
  const fixture = arrangeValid();
  try {
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: fixture.events.slice(1) }), CODES.INIT_INVALID);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: [fixture.events[0], ...fixture.events] }), CODES.INIT_INVALID);
    const lateInit = structuredClone(fixture.events);
    const [init] = lateInit.splice(0, 1);
    lateInit.splice(2, 0, init);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: lateInit }), CODES.INIT_INVALID);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: fixture.events.slice(0, -1) }), CODES.RESULT_INVALID);
    const afterResult = structuredClone(fixture.events);
    afterResult.push({ type: "assistant", message: { role: "assistant", content: [] } });
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: afterResult }), CODES.RESULT_INVALID);
    const errorTerminal = structuredClone(fixture.events);
    errorTerminal.at(-1).subtype = "error_during_execution";
    errorTerminal.at(-1).is_error = true;
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: errorTerminal }), CODES.RESULT_NOT_SUCCESS);
    const inventory = structuredClone(fixture.events);
    inventory[0].tools.push("Bash");
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: inventory }), CODES.TOOL_INVENTORY_INVALID);
    const permissionMode = structuredClone(fixture.events);
    permissionMode[0].permissionMode = "bypassPermissions";
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: permissionMode }), CODES.PERMISSION_MODE_INVALID);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("partial audit rejects a pre-init tool, late init, or post-result event", () => {
  const fixture = arrangeValidPhase();
  try {
    const failedEvents = structuredClone(fixture.events);
    failedEvents.at(-1).subtype = "error_max_turns";
    failedEvents.at(-1).is_error = true;
    delete failedEvents.at(-1).structured_output;

    const preInitTool = structuredClone(failedEvents);
    preInitTool.unshift(...invocation("pre-init", "Read", {
      file_path: path.join(fixture.workspaceRoot, "inputs", "wiki.md"),
    }));
    errorCode(() => auditPartialSkillGenerationTrace({ ...fixture, events: preInitTool }), CODES.INIT_INVALID);

    const lateInit = structuredClone(failedEvents);
    const [init] = lateInit.splice(0, 1);
    lateInit.splice(2, 0, init);
    errorCode(() => auditPartialSkillGenerationTrace({ ...fixture, events: lateInit }), CODES.INIT_INVALID);

    const afterResult = structuredClone(failedEvents);
    afterResult.push({ type: "assistant", message: { role: "assistant", content: [] } });
    errorCode(() => auditPartialSkillGenerationTrace({ ...fixture, events: afterResult }), CODES.RESULT_INVALID);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("requires the first and only tool call to be the exact successful Skill invocation", () => {
  const fixture = arrangeValid();
  try {
    const wrongInput = structuredClone(fixture.events);
    wrongInput[1].message.content[0].input.extra = true;
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: wrongInput }), CODES.SKILL_INVOCATION_INVALID);

    const readFirst = structuredClone(fixture.events);
    const skillPair = readFirst.splice(1, 2);
    readFirst.splice(3, 0, ...skillPair);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: readFirst }), CODES.SKILL_INVOCATION_INVALID);

    const failedSkill = structuredClone(fixture.events);
    failedSkill[2].tool_use_result.success = false;
    failedSkill[2].message.content[0].is_error = false;
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: failedSkill }), CODES.TOOL_RESULT_ERROR);

    const implicitSkill = structuredClone(fixture.events);
    implicitSkill[2].tool_use_result = {};
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: implicitSkill }), CODES.SKILL_RESULT_INVALID);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("rejects disallowed tools and malformed, missing, duplicate, or failed tool results", () => {
  const fixture = arrangeValid();
  try {
    const otherTool = structuredClone(fixture.events);
    otherTool.splice(-1, 0, ...invocation("other", "Bash", { command: "true" }));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: otherTool }), CODES.TOOL_NOT_ALLOWED);

    const unpaired = structuredClone(fixture.events);
    unpaired.splice(4, 1);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: unpaired }), CODES.TOOL_RESULT_MISSING);

    const unmatched = structuredClone(fixture.events);
    unmatched.splice(-1, 0, toolResult("absent", "Read"));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: unmatched }), CODES.TOOL_RESULT_UNMATCHED);

    const duplicate = structuredClone(fixture.events);
    duplicate.splice(-1, 0, toolResult("wiki", "Read"));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: duplicate }), CODES.TOOL_RESULT_DUPLICATE);

    const failedRead = structuredClone(fixture.events);
    failedRead[4].message.content[0].is_error = true;
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: failedRead }), CODES.TOOL_RESULT_ERROR);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("permits only the two inputs and ordinary references linked by SKILL.md", () => {
  const fixture = arrangeValid();
  try {
    const missingRequired = structuredClone(fixture.events);
    missingRequired.splice(7, 2);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: missingRequired }), CODES.REQUIRED_READ_MISSING);

    const unlinked = structuredClone(fixture.events);
    unlinked[11].message.content[0].input.file_path = "unlinked.md";
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: unlinked }), CODES.READ_UNLINKED);

    write(fixture.workspaceRoot, "references/generation-spec-v6-reference.md");
    const workspaceShadow = structuredClone(fixture.events);
    replaceToolPath(
      workspaceShadow,
      "Read",
      2,
      path.join(fixture.workspaceRoot, "references", "generation-spec-v6-reference.md"),
    );
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: workspaceShadow }), CODES.READ_UNLINKED);

    const absolute = structuredClone(fixture.events);
    absolute[3].message.content[0].input.file_path = path.resolve(fixture.parent, "outside.md");
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: absolute }), CODES.READ_UNLINKED);

    const traversal = structuredClone(fixture.events);
    traversal[3].message.content[0].input.file_path = "inputs/../inputs/wiki.md";
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: traversal }), CODES.PATH_TRAVERSAL);

    const relativePaths = structuredClone(fixture.events);
    replaceToolPath(relativePaths, "Read", 0, "inputs/wiki.md");
    replaceToolPath(relativePaths, "Read", 1, "inputs/clarifications.md");
    replaceToolPath(relativePaths, "Write", 0, "output/generation-spec.json");
    assert.equal(auditSkillGenerationTrace({ ...fixture, events: relativePaths }).status, "PASS");

    const partialRequiredRead = structuredClone(fixture.events);
    partialRequiredRead[3].message.content[0].input.limit = 1;
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: partialRequiredRead }), CODES.REQUIRED_READ_PARTIAL);

    const readBeforeSkillResult = structuredClone(fixture.events);
    const skillResult = readBeforeSkillResult.splice(2, 1);
    readBeforeSkillResult.splice(4, 0, ...skillResult);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: readBeforeSkillResult }), CODES.REQUIRED_READ_ORDER_INVALID);

    const requiredReadAfterWrite = structuredClone(fixture.events);
    const generationRead = requiredReadAfterWrite.splice(7, 2);
    requiredReadAfterWrite.splice(-1, 0, ...generationRead);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: requiredReadAfterWrite }), CODES.REQUIRED_READ_ORDER_INVALID);

    errorCode(() => auditSkillGenerationTrace({
      ...fixture,
      requiredReferencePaths: ["references/not-linked.md"],
    }), CODES.REQUIRED_REFERENCE_INVALID);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("requires exactly one successful Write to the fixed regular output path", () => {
  const fixture = arrangeValid();
  try {
    const missingWrite = structuredClone(fixture.events);
    missingWrite.splice(-3, 2);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: missingWrite }), CODES.WRITE_COUNT_INVALID);

    const duplicateWrite = structuredClone(fixture.events);
    duplicateWrite.splice(-1, 0, ...invocation("write-again", "Write", {
      file_path: path.join(fixture.workspaceRoot, "output", "generation-spec.json"),
      content: fixture.content,
    }));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: duplicateWrite }), CODES.WRITE_COUNT_INVALID);

    const twoEmptyRejections = structuredClone(fixture.events);
    twoEmptyRejections.splice(-3, 0,
      ...invocation("empty-write-1", "Write", {}, { error: true }),
      ...invocation("empty-write-2", "Write", {}, { error: true }));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: twoEmptyRejections }), CODES.WRITE_COUNT_INVALID);

    const emptyBeforeRequiredReads = structuredClone(fixture.events);
    emptyBeforeRequiredReads.splice(3, 0, ...invocation("empty-write", "Write", {}, { error: true }));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: emptyBeforeRequiredReads }), CODES.REQUIRED_READ_ORDER_INVALID);

    const readBetweenRetryAndSuccess = structuredClone(fixture.events);
    readBetweenRetryAndSuccess.splice(-5, 0, ...invocation("empty-write", "Write", {}, { error: true }));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: readBetweenRetryAndSuccess }), CODES.REQUIRED_READ_ORDER_INVALID);

    const successBeforeRetryResult = structuredClone(fixture.events);
    successBeforeRetryResult.splice(-3, 0, ...invocation("empty-write", "Write", {}, { error: true }));
    const retryResultIndex = successBeforeRetryResult.findIndex((event) => event?.message?.content?.[0]?.tool_use_id === "empty-write");
    const [retryResult] = successBeforeRetryResult.splice(retryResultIndex, 1);
    const successfulWriteUseIndex = successBeforeRetryResult.findIndex((event) => event?.message?.content?.[0]?.id === "write");
    successBeforeRetryResult.splice(successfulWriteUseIndex + 1, 0, retryResult);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: successBeforeRetryResult }), CODES.REQUIRED_READ_ORDER_INVALID);

    const emptyAfterSuccess = structuredClone(fixture.events);
    emptyAfterSuccess.splice(-1, 0, ...invocation("empty-write", "Write", {}, { error: true }));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: emptyAfterSuccess }), CODES.REQUIRED_READ_ORDER_INVALID);

    for (const input of [
      { file_path: "output/generation-spec.json" },
      { content: fixture.content },
      { unexpected: true },
      null,
      [],
    ]) {
      const failedWriteWithInput = structuredClone(fixture.events);
      failedWriteWithInput.splice(-3, 0, ...invocation("failed-write", "Write", input, { error: true }));
      errorCode(() => auditSkillGenerationTrace({ ...fixture, events: failedWriteWithInput }), CODES.TOOL_RESULT_ERROR);
    }

    const permissionDeniedWrite = structuredClone(fixture.events);
    permissionDeniedWrite.at(-2).message.content[0].is_error = true;
    permissionDeniedWrite.at(-2).tool_use_result = "Error: permission denied";
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: permissionDeniedWrite }), CODES.TOOL_RESULT_ERROR);

    const contradictoryEmptyWrite = structuredClone(fixture.events);
    const contradictory = invocation("empty-write", "Write", {}, { error: true });
    contradictory[1].tool_use_result.success = true;
    contradictoryEmptyWrite.splice(-3, 0, ...contradictory);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: contradictoryEmptyWrite }), CODES.TOOL_RESULT_ERROR);

    const missingContent = structuredClone(fixture.events);
    delete missingContent.at(-3).message.content[0].input.content;
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: missingContent }), CODES.WRITE_INPUT_INVALID);

    const emptyContent = structuredClone(fixture.events);
    emptyContent.at(-3).message.content[0].input.content = " \n";
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: emptyContent }), CODES.WRITE_INPUT_INVALID);

    const wrongWrite = structuredClone(fixture.events);
    wrongWrite.at(-3).message.content[0].input.file_path = "output/other.json";
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: wrongWrite }), CODES.WRITE_PATH_INVALID);

    const failedWrongWrite = structuredClone(fixture.events);
    failedWrongWrite.splice(-3, 0, ...invocation("wrong-write", "Write", {
      file_path: "output/other.json",
      content: fixture.content,
    }, { error: true }));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: failedWrongWrite }), CODES.TOOL_RESULT_ERROR);

    const mismatchedContent = structuredClone(fixture.events);
    mismatchedContent.at(-3).message.content[0].input.content = "{}";
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: mismatchedContent }), CODES.WRITE_CONTENT_MISMATCH);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("rejects symlinks in every observed input, reference, or output path", { skip: process.platform === "win32" }, async (context) => {
  for (const target of [
    { root: "workspaceRoot", relative: "inputs/wiki.md" },
    { root: "skillRoot", relative: "references/generation-spec-v6-reference.md" },
    { root: "workspaceRoot", relative: "output/generation-spec.json" },
  ]) {
    await context.test(target.relative, () => {
      const fixture = arrangeValid();
      try {
        const link = path.join(fixture[target.root], ...target.relative.split("/"));
        const realTarget = path.join(fixture[target.root], `real-${path.basename(target.relative)}`);
        fs.writeFileSync(realTarget, fs.readFileSync(link));
        fs.rmSync(link);
        fs.symlinkSync(path.relative(path.dirname(link), realTarget), link);
        errorCode(() => auditSkillGenerationTrace(fixture), CODES.PATH_SYMLINK);
      } finally {
        fs.rmSync(fixture.parent, { recursive: true, force: true });
      }
    });
  }
});
