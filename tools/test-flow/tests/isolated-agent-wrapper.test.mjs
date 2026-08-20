import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { canonicalJson, sha256Bytes } from "../lib/util.mjs";
import {
  ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT,
  ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_ENFORCEMENT,
  ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY,
  ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_LIMIT,
} from "../runtime-support/isolated-agent-env.mjs";
import {
  SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES,
  SKILL_GENERATION_PHASE_REQUIRED_RECEIPT_READS,
  SKILL_GENERATION_TRACE_CODES as TRACE_CODES,
  SKILL_GENERATION_TRACE_SCHEMA_VERSION,
  validSkillGenerationIncompleteAuditRejectedReceipt,
  validSkillGenerationFailedTraceAuditReceipt,
  validSkillGenerationIncompleteTraceAuditReceipt,
  validSkillGenerationPartialTraceAuditReceipt,
  validSkillGenerationTraceAuditReceipt,
} from "../runtime-support/isolated-agent-tool-audit.mjs";
import {
  GENERATION_BLUEPRINT_SUBMISSION_JSON_SCHEMA,
  SKILL_GENERATION_RULE_IR,
  validGenerationBlueprintSubmission,
} from "../runtime-support/skill-generation-rule-ir.mjs";

const WRAPPER = path.resolve("tools/test-flow/runtime-support/isolated-agent-wrapper.mjs");

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
    compiler: {
      id: SKILL_GENERATION_RULE_IR.compiler_id,
      version: SKILL_GENERATION_RULE_IR.compiler_version,
    },
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
          "present_prefix", "present_suffix", "timeout_infix", "timeout_suffix",
          "core_prefix", "core_infix", "core_suffix", "serial_prefix", "serial_infix",
          "serial_suffix", "interval_prefix", "interval_infix", "interval_suffix",
          "unattributed_assertion", "overlap_assertion", "full_assertion", "gap_assertion",
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

function installFakeValidator(sourceRoot) {
  const validatorScript = path.join(
    sourceRoot,
    "tools",
    "test-flow",
    "runtime-support",
    "compile_skill_generation_rule_ir.py",
  );
  fs.mkdirSync(path.dirname(validatorScript), { recursive: true });
  fs.writeFileSync(validatorScript, `
const crypto = require("node:crypto");
const fs = require("node:fs");
const canonicalize = (value) => Array.isArray(value)
  ? value.map(canonicalize)
  : value !== null && typeof value === "object"
    ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonicalize(value[key])]))
    : value;
const canonical = (value) => JSON.stringify(canonicalize(value)) + "\\n";
const raw = fs.readFileSync(0, "utf8");
const ir = JSON.parse(raw);
if (raw !== canonical(ir)) process.exit(8);
const spec = ${JSON.stringify(minimalValidSubmission())};
const output = canonical(spec);
const envelope = {
  schema_version: 1,
  compiler: {
    id: ${JSON.stringify(SKILL_GENERATION_RULE_IR.compiler_id)},
    version: ${JSON.stringify(SKILL_GENERATION_RULE_IR.compiler_version)},
    blueprint_schema_version: ${SKILL_GENERATION_RULE_IR.blueprint_schema_version},
    family_kind: ${JSON.stringify(SKILL_GENERATION_RULE_IR.family_kind)},
    family_version: ${SKILL_GENERATION_RULE_IR.family_version},
  },
  ir: {size_bytes: Buffer.byteLength(raw), sha256: crypto.createHash("sha256").update(raw).digest("hex")},
  output: {size_bytes: Buffer.byteLength(output), sha256: crypto.createHash("sha256").update(output).digest("hex")},
  spec,
};
process.stdout.write(canonical(envelope));
`);
  return validatorScript;
}

function preparePhaseWrapperFixture(root) {
  const workspace = path.join(root, "workspace");
  const sourceRoot = path.join(root, "source");
  const skillRoot = path.join(root, "skill");
  const usageRoot = path.join(root, "usage");
  const settings = path.join(root, "settings.json");
  fs.mkdirSync(path.join(workspace, "inputs"), { recursive: true });
  fs.mkdirSync(path.join(workspace, "output"));
  fs.mkdirSync(sourceRoot);
  fs.mkdirSync(path.join(skillRoot, "references", "checkpoints"), { recursive: true });
  fs.writeFileSync(path.join(workspace, "inputs", "wiki.md"), "wiki\n");
  fs.writeFileSync(path.join(workspace, "inputs", "clarifications.md"), "clarifications\n");
  const references = [
    "references/generation-spec-v6-reference.md",
    "references/verification-contract-v2-reference.md",
    ...SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES,
  ];
  fs.writeFileSync(path.join(skillRoot, "SKILL.md"), references.map((relative, index) => `[reference ${index}](${relative})`).join("\n") + "\n");
  for (const relative of references) {
    const target = path.join(skillRoot, ...relative.split("/"));
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, `${relative}\n`);
  }
  fs.writeFileSync(settings, "{}\n");
  const validatorScript = path.join(
    sourceRoot,
    "tools",
    "test-flow",
    "runtime-support",
    "compile_skill_generation_rule_ir.py",
  );
  fs.mkdirSync(path.dirname(validatorScript), { recursive: true });
  fs.writeFileSync(validatorScript, `
const crypto = require("node:crypto");
const fs = require("node:fs");
const canonicalize = (value) => Array.isArray(value)
  ? value.map(canonicalize)
  : value !== null && typeof value === "object"
    ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonicalize(value[key])]))
    : value;
const canonical = (value) => JSON.stringify(canonicalize(value)) + "\\n";
const raw = fs.readFileSync(0, "utf8");
const ir = JSON.parse(raw);
if (raw !== canonical(ir)) process.exit(8);
const spec = ${JSON.stringify(minimalValidSubmission())};
const output = canonical(spec);
const envelope = {
  schema_version: 1,
  compiler: {
    id: ${JSON.stringify(SKILL_GENERATION_RULE_IR.compiler_id)},
    version: ${JSON.stringify(SKILL_GENERATION_RULE_IR.compiler_version)},
    blueprint_schema_version: ${SKILL_GENERATION_RULE_IR.blueprint_schema_version},
    family_kind: ${JSON.stringify(SKILL_GENERATION_RULE_IR.family_kind)},
    family_version: ${SKILL_GENERATION_RULE_IR.family_version},
  },
  ir: {size_bytes: Buffer.byteLength(raw), sha256: crypto.createHash("sha256").update(raw).digest("hex")},
  output: {size_bytes: Buffer.byteLength(output), sha256: crypto.createHash("sha256").update(output).digest("hex")},
  spec,
};
process.stdout.write(canonical(envelope));
`);
  return { workspace, sourceRoot, skillRoot, usageRoot, settings, references, validatorScript };
}

function validatorArguments(fixture) {
  return [
    "--validator-command", process.execPath,
    "--validator-prefix-json", "[]",
    "--validator-script", fixture.validatorScript,
  ];
}

function skillPrefixFakeSource(fixture, {
  batchPending = false,
  completedSerialReads = 0,
  structuredOutcomes = [],
  structuredOutput = minimalValidBlueprint(),
  wikiInput = null,
} = {}) {
  const initialInputs = [
    ["wiki", wikiInput ?? { file_path: path.join(fixture.workspace, "inputs", "wiki.md") }],
    ["clarifications", { file_path: path.join(fixture.workspace, "inputs", "clarifications.md") }],
  ];
  const serialReads = fixture.references.map((relative, index) => [
    `reference-${index}`,
    path.join(fixture.skillRoot, ...relative.split("/")),
  ]).slice(0, completedSerialReads);
  return `
const model = process.argv[process.argv.indexOf("--model") + 1];
const emit = (value) => console.log(JSON.stringify(value));
const use = (id, name, input) => emit({type:"assistant",message:{role:"assistant",content:[{type:"tool_use",id,name,input}]}});
const result = (id, name) => emit({type:"user",message:{role:"user",content:[{type:"tool_result",tool_use_id:id,is_error:false}]},tool_use_result:name === "Skill" ? {success:true} : {type:name.toLowerCase()}});
emit({type:"system",subtype:"init",cwd:process.cwd(),model,permissionMode:"dontAsk",tools:["Read","Skill","StructuredOutput"]});
use("skill", "Skill", {skill:"wiki-to-diagnosis-skill"}); result("skill", "Skill");
const initialInputs = ${JSON.stringify(initialInputs)};
emit({type:"assistant",message:{role:"assistant",content:initialInputs.map(([id,input]) => ({type:"tool_use",id,name:"Read",input}))}});
if (!${JSON.stringify(batchPending)}) {
  emit({type:"user",message:{role:"user",content:initialInputs.map(([id]) => ({type:"tool_result",tool_use_id:id,is_error:false}))},tool_use_result:{type:"read"}});
}
for (const [id, file_path] of ${JSON.stringify(serialReads)}) { use(id, "Read", {file_path}); result(id, "Read"); }
const structuredOutput = ${JSON.stringify(structuredOutput)};
for (const [index, outcome] of ${JSON.stringify(structuredOutcomes)}.entries()) {
  const id = index === 0 ? "structured-output" : "structured-output-again";
  use(id, "StructuredOutput", structuredOutput);
  if (outcome === "SUCCESS") result(id, "StructuredOutput");
}
setInterval(() => {}, 1000);
`;
}

function completeSkillFakeSource(fixture, structuredOutput = minimalValidBlueprint()) {
  const reads = [
    ["wiki", path.join(fixture.workspace, "inputs", "wiki.md")],
    ["clarifications", path.join(fixture.workspace, "inputs", "clarifications.md")],
    ...fixture.references.map((relative, index) => [
      `reference-${index}`,
      path.join(fixture.skillRoot, ...relative.split("/")),
    ]),
  ];
  return `
const model = process.argv[process.argv.indexOf("--model") + 1];
const emit = (value) => console.log(JSON.stringify(value));
const use = (id, name, input) => emit({type:"assistant",message:{role:"assistant",content:[{type:"tool_use",id,name,input}]}});
const result = (id, name) => emit({type:"user",message:{role:"user",content:[{type:"tool_result",tool_use_id:id,is_error:false}]},tool_use_result:name === "Skill" ? {success:true} : {type:name.toLowerCase()}});
emit({type:"system",subtype:"init",cwd:process.cwd(),model,permissionMode:"dontAsk",tools:["Read","Skill","StructuredOutput"]});
use("skill", "Skill", {skill:"wiki-to-diagnosis-skill"}); result("skill", "Skill");
const reads = ${JSON.stringify(reads)};
emit({type:"assistant",message:{role:"assistant",content:reads.slice(0,2).map(([id,file_path])=>({type:"tool_use",id,name:"Read",input:{file_path}}))}});
emit({type:"user",message:{role:"user",content:reads.slice(0,2).map(([id])=>({type:"tool_result",tool_use_id:id,is_error:false}))},tool_use_result:{type:"read"}});
for (const [id, file_path] of reads.slice(2)) { use(id, "Read", {file_path}); result(id, "Read"); }
const structuredOutput = ${JSON.stringify(structuredOutput)};
use("structured-output", "StructuredOutput", structuredOutput); result("structured-output", "StructuredOutput");
emit({type:"result",subtype:"success",is_error:false,result:"DONE",structured_output:structuredOutput,num_turns:10,total_cost_usd:0.01,usage:{input_tokens:10,output_tokens:20,cache_creation_input_tokens:0,cache_read_input_tokens:0}});
`;
}

function runFakeSkillWrapper(root, fixture, source, { hardTimeoutSeconds = 1 } = {}) {
  const fakeClaude = path.join(root, "fake-claude.mjs");
  fs.writeFileSync(fakeClaude, source);
  const result = spawnSync(process.execPath, [
    WRAPPER,
    "--claude-entry", fakeClaude,
    "--settings", fixture.settings,
    "--model", "test-model",
    "--usage-root", fixture.usageRoot,
    "--max-turns", "12",
    "--max-total-tokens", "1000000",
    "--max-output-tokens", "64000",
    "--max-output-tokens-upper-limit", "64000",
    "--max-budget-usd", "10",
    "--hard-timeout-seconds", String(hardTimeoutSeconds),
    "--workflow", "skill-generation",
    "--skill-root", fixture.skillRoot,
    "--source-root", fixture.sourceRoot,
    ...validatorArguments(fixture),
  ], {
    cwd: fixture.workspace,
    encoding: "utf8",
    env: { PATH: process.env.PATH ?? "", HOME: root },
  });
  const receiptFiles = fs.existsSync(fixture.usageRoot) ? fs.readdirSync(fixture.usageRoot) : [];
  const receipt = receiptFiles.length === 1
    ? JSON.parse(fs.readFileSync(path.join(fixture.usageRoot, receiptFiles[0]), "utf8"))
    : null;
  return { result, receipt };
}

function assertTimedOutSkillPrefix(result, receipt, fixture) {
  assert.equal(result.status, 124, result.stderr);
  assert.equal(result.stdout, "");
  assert.equal(result.stderr, "WRAPPER_MODEL_TIMEOUT\n");
  assert.notEqual(receipt, null);
  assert.deepEqual(receipt.wrapper_outcome, {
    schema_version: 1,
    status: "FAIL",
    code: "WRAPPER_MODEL_TIMEOUT",
  });
  assert.equal(receipt.terminal, null);
  assert.equal(receipt.usage, null);
  assert.equal(receipt.turns, null);
  assert.equal(receipt.usage_complete, false);
  assert.equal(receipt.timed_out, true);
  assert.equal(receipt.process.wrapper_exit_code, 124);
  assert.equal(receipt.stream.complete, false);
  assert.equal(receipt.stream.result_count, 0);
  assert.equal(fs.existsSync(path.join(fixture.workspace, "output", "generation-spec.json")), false);
}

function runFakeJobWrapper(root, source, { requestedModel = "test-model" } = {}) {
  const usageRoot = path.join(root, "usage");
  const fakeClaude = path.join(root, "fake-claude.mjs");
  const settings = path.join(root, "settings.json");
  fs.writeFileSync(settings, "{}\n");
  fs.writeFileSync(fakeClaude, source);
  const result = spawnSync(process.execPath, [
    WRAPPER,
    "--claude-entry", fakeClaude,
    "--settings", settings,
    "--model", requestedModel,
    "--usage-root", usageRoot,
    "--max-turns", "12",
    "--max-total-tokens", "1000000",
    "--max-budget-usd", "10",
    "--hard-timeout-seconds", "30",
    "--workflow", "job",
  ], {
    cwd: root,
    encoding: "utf8",
    env: { PATH: process.env.PATH ?? "", HOME: root },
  });
  const receiptFiles = fs.existsSync(usageRoot) ? fs.readdirSync(usageRoot) : [];
  const receipt = receiptFiles.length === 1
    ? JSON.parse(fs.readFileSync(path.join(usageRoot, receiptFiles[0]), "utf8"))
    : null;
  return { result, receipt };
}

test("a budget overrun takes precedence over a failed model terminal and preserves complete usage", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-terminal-"));
  try {
    const usageRoot = path.join(root, "usage");
    const fakeClaude = path.join(root, "fake-claude.mjs");
    const settings = path.join(root, "settings.json");
    fs.writeFileSync(settings, "{}\n");
    fs.writeFileSync(fakeClaude, `
const model = process.argv[process.argv.indexOf("--model") + 1];
console.log(JSON.stringify({type:"system",subtype:"init",model}));
console.log(JSON.stringify({
  type:"result",subtype:"error_max_budget_usd",is_error:true,num_turns:7,
  total_cost_usd:3.398151,
  usage:{input_tokens:24411,output_tokens:97144,cache_creation_input_tokens:0,cache_read_input_tokens:68736}
}));
process.exitCode = 1;
`);
    const result = spawnSync(process.execPath, [
      WRAPPER,
      "--claude-entry", fakeClaude,
      "--settings", settings,
      "--model", "test-model",
      "--usage-root", usageRoot,
      "--max-turns", "12",
      "--max-total-tokens", "1000000",
      "--max-budget-usd", "3",
      "--hard-timeout-seconds", "30",
      "--workflow", "job",
    ], {
      cwd: root,
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "", HOME: root },
    });
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /WRAPPER_MODEL_CAP_EXCEEDED/);
    const files = fs.readdirSync(usageRoot);
    assert.equal(files.length, 1);
    const receipt = JSON.parse(fs.readFileSync(path.join(usageRoot, files[0]), "utf8"));
    assert.deepEqual(receipt.wrapper_outcome, {
      schema_version: 1,
      status: "FAIL",
      code: "WRAPPER_MODEL_CAP_EXCEEDED",
    });
    assert.deepEqual(receipt.terminal, { subtype: "error_max_budget_usd", is_error: true });
    assert.deepEqual(receipt.usage, {
      schema_version: 1,
      input_tokens: 24411,
      output_tokens: 97144,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 68736,
      total_tokens: 190291,
      cost_usd: 3.398151,
    });
    assert.equal(receipt.usage_complete, true);
    assert.deepEqual(receipt.stream, {
      schema_version: 1,
      event_count: 2,
      parsed_event_count: 2,
      init_count: 1,
      result_count: 1,
      last_event_type: "result",
      complete: true,
    });
    assert.equal(receipt.tool_trace_audit, null);
    assert.equal(receipt.process.wrapper_exit_code, 1);
    assert.equal(Object.hasOwn(receipt.effective_caps, "max_output_tokens"), false);
    assert.equal(Object.hasOwn(receipt.hard_cap_enforcement, "max_output_tokens"), false);
    assert.equal(receipt.environment_policy.claude_process.key_names.includes("CLAUDE_CODE_MAX_OUTPUT_TOKENS"), false);
    assert.doesNotMatch(JSON.stringify(receipt), /wiki|clarification|prompt|content/i);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("a token overrun takes precedence over a failed model terminal", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-token-cap-precedence-"));
  try {
    const { result, receipt } = runFakeJobWrapper(root, `
const model = process.argv[process.argv.indexOf("--model") + 1];
console.log(JSON.stringify({type:"system",subtype:"init",model}));
console.log(JSON.stringify({type:"result",subtype:"error_max_turns",is_error:true,num_turns:2,total_cost_usd:0.01,usage:{input_tokens:1000001,output_tokens:0,cache_creation_input_tokens:0,cache_read_input_tokens:0}}));
process.exitCode = 1;
`);
    assert.equal(result.status, 1);
    assert.equal(result.stderr, "WRAPPER_MODEL_CAP_EXCEEDED\n");
    assert.deepEqual(receipt.wrapper_outcome, {
      schema_version: 1,
      status: "FAIL",
      code: "WRAPPER_MODEL_CAP_EXCEEDED",
    });
    assert.equal(receipt.usage.total_tokens, 1000001);
    assert.deepEqual(receipt.terminal, { subtype: "error_max_turns", is_error: true });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("trustworthy token or cost overruns take precedence over malformed turns", async (context) => {
  const cases = [
    ["token overrun", { input_tokens: 1000001, output_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 }, 0.01],
    ["cost overrun", { input_tokens: 10, output_tokens: 20, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 }, 10.01],
  ];
  for (const [name, usage, cost] of cases) {
    await context.test(name, () => {
      const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-malformed-turn-cap-"));
      try {
        const { result, receipt } = runFakeJobWrapper(root, `
const model = process.argv[process.argv.indexOf("--model") + 1];
console.log(JSON.stringify({type:"system",subtype:"init",model}));
console.log(JSON.stringify({type:"result",subtype:"success",is_error:false,num_turns:"malformed",total_cost_usd:${JSON.stringify(cost)},usage:${JSON.stringify(usage)}}));
`);
        assert.equal(result.status, 1);
        assert.equal(result.stderr, "WRAPPER_MODEL_CAP_EXCEEDED\n");
        assert.deepEqual(receipt.wrapper_outcome, {
          schema_version: 1,
          status: "FAIL",
          code: "WRAPPER_MODEL_CAP_EXCEEDED",
        });
        assert.equal(receipt.usage_complete, true);
        assert.equal(receipt.turns, null);
      } finally {
        fs.rmSync(root, { recursive: true, force: true });
      }
    });
  }
});

test("a timed out model persists an incomplete sanitized receipt before exiting 124", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-timeout-"));
  try {
    const usageRoot = path.join(root, "usage");
    const fakeClaude = path.join(root, "fake-claude.mjs");
    const settings = path.join(root, "settings.json");
    fs.writeFileSync(settings, "{}\n");
    fs.writeFileSync(fakeClaude, `
const model = process.argv[process.argv.indexOf("--model") + 1];
console.log(JSON.stringify({type:"system",subtype:"init",model}));
console.log(JSON.stringify({type:"assistant",message:{id:"thinking",content:[{type:"thinking",thinking:"must-not-enter-receipt"}]}}));
setInterval(() => {}, 1000);
`);
    const result = spawnSync(process.execPath, [
      WRAPPER,
      "--claude-entry", fakeClaude,
      "--settings", settings,
      "--model", "test-model",
      "--usage-root", usageRoot,
      "--max-turns", "12",
      "--max-total-tokens", "1000000",
      "--max-budget-usd", "10",
      "--hard-timeout-seconds", "1",
      "--workflow", "job",
    ], {
      cwd: root,
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "", HOME: root },
    });
    assert.equal(result.status, 124, result.stderr);
    assert.match(result.stderr, /WRAPPER_MODEL_TIMEOUT/);
    assert.doesNotMatch(result.stderr, /WRAPPER_MODEL_STREAM_INVALID/);
    const files = fs.readdirSync(usageRoot);
    assert.equal(files.length, 1);
    assert.match(files[0], /\.json$/u);
    const receipt = JSON.parse(fs.readFileSync(path.join(usageRoot, files[0]), "utf8"));
    assert.deepEqual(receipt.wrapper_outcome, {
      schema_version: 1,
      status: "FAIL",
      code: "WRAPPER_MODEL_TIMEOUT",
    });
    assert.equal(receipt.effective_model, "test-model");
    assert.equal(receipt.usage_complete, false);
    assert.equal(receipt.usage, null);
    assert.equal(receipt.terminal, null);
    assert.equal(receipt.turns, null);
    assert.equal(receipt.tool_trace_audit, null);
    assert.equal(receipt.timed_out, true);
    assert.equal(receipt.process.wrapper_exit_code, 124);
    assert.deepEqual(receipt.stream, {
      schema_version: 1,
      event_count: 2,
      parsed_event_count: 2,
      init_count: 1,
      result_count: 0,
      last_event_type: "assistant",
      complete: false,
    });
    assert.doesNotMatch(JSON.stringify(receipt), /must-not-enter-receipt|thinking|prompt|content/u);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("skill-generation timeouts retain every safe production prefix without creating output", async (context) => {
  await context.test("initial wiki and clarifications batch is doubly pending", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-timeout-batch-prefix-"));
    try {
      const fixture = preparePhaseWrapperFixture(root);
      const { result, receipt } = runFakeSkillWrapper(
        root,
        fixture,
        skillPrefixFakeSource(fixture, { batchPending: true }),
      );
      assertTimedOutSkillPrefix(result, receipt, fixture);
      assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(receipt.tool_trace_audit), true);
      assert.equal(validSkillGenerationTraceAuditReceipt(receipt.tool_trace_audit), false);
      assert.deepEqual(receipt.tool_trace_audit.stream, receipt.stream);
      assert.equal(receipt.tool_trace_audit.code, TRACE_CODES.INCOMPLETE_PREFIX);
      assert.deepEqual(receipt.tool_trace_audit.tool_sequence.map((record) => record.outcome), [
        "SUCCESS", "PENDING", "PENDING",
      ]);
      assert.deepEqual(
        receipt.tool_trace_audit.tool_sequence.filter((record) => record.tool === "Read").map((record) => record.path),
        SKILL_GENERATION_PHASE_REQUIRED_RECEIPT_READS.slice(0, 2),
      );
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  for (let completedSerialReads = 1; completedSerialReads <= SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES.length + 2; completedSerialReads += 1) {
    await context.test(`hang after serial production Read ${completedSerialReads} completed`, () => {
      const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-timeout-read-prefix-"));
      try {
        const fixture = preparePhaseWrapperFixture(root);
        const { result, receipt } = runFakeSkillWrapper(
          root,
          fixture,
          skillPrefixFakeSource(fixture, { completedSerialReads }),
        );
        assertTimedOutSkillPrefix(result, receipt, fixture);
        assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(receipt.tool_trace_audit), true);
        assert.equal(validSkillGenerationTraceAuditReceipt(receipt.tool_trace_audit), false);
        assert.deepEqual(receipt.tool_trace_audit.stream, receipt.stream);
        assert.equal(receipt.tool_trace_audit.code, TRACE_CODES.INCOMPLETE_PREFIX);
        assert.equal(receipt.tool_trace_audit.tool_sequence.length, 3 + completedSerialReads);
        assert.ok(receipt.tool_trace_audit.tool_sequence.every((record) => record.outcome === "SUCCESS"));
        assert.deepEqual(
          receipt.tool_trace_audit.tool_sequence.filter((record) => record.tool === "Read").map((record) => record.path),
          SKILL_GENERATION_PHASE_REQUIRED_RECEIPT_READS.slice(0, 2 + completedSerialReads),
        );
      } finally {
        fs.rmSync(root, { recursive: true, force: true });
      }
    });
  }

  for (const scenario of [
    {
      name: "StructuredOutput pending",
      outcomes: ["PENDING"],
      expectedCode: TRACE_CODES.INCOMPLETE_PREFIX,
      expectedOutcomes: ["PENDING"],
      secret: "pending-structured-output-secret",
    },
    {
      name: "StructuredOutput result completed",
      outcomes: ["SUCCESS"],
      expectedCode: TRACE_CODES.INCOMPLETE_PREFIX,
      expectedOutcomes: ["SUCCESS"],
      secret: null,
    },
    {
      name: "second StructuredOutput pending",
      outcomes: ["SUCCESS", "PENDING"],
      expectedCode: TRACE_CODES.INCOMPLETE_PREFIX_SEQUENCE_INVALID,
      expectedOutcomes: ["SUCCESS", "PENDING"],
      secret: null,
    },
  ]) {
    await context.test(`${scenario.name} before timeout`, () => {
      const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-timeout-output-prefix-"));
      try {
        const fixture = preparePhaseWrapperFixture(root);
        const structuredOutput = minimalValidBlueprint();
        if (scenario.secret !== null) structuredOutput.spec.summary = scenario.secret;
        const { result, receipt } = runFakeSkillWrapper(
          root,
          fixture,
          skillPrefixFakeSource(fixture, {
            completedSerialReads: fixture.references.length,
            structuredOutcomes: scenario.outcomes,
            structuredOutput,
          }),
        );
        assertTimedOutSkillPrefix(result, receipt, fixture);
        assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(receipt.tool_trace_audit), true);
        assert.equal(validSkillGenerationTraceAuditReceipt(receipt.tool_trace_audit), false);
        assert.deepEqual(receipt.tool_trace_audit.stream, receipt.stream);
        assert.equal(receipt.tool_trace_audit.code, scenario.expectedCode);
        const outputRecords = receipt.tool_trace_audit.tool_sequence.filter(
          (record) => record.tool === "StructuredOutput",
        );
        assert.deepEqual(outputRecords.map((record) => record.outcome), scenario.expectedOutcomes);
        const canonical = canonicalJson(structuredOutput);
        assert.ok(outputRecords.every((record) => (
          record.size_bytes === Buffer.byteLength(canonical)
          && record.sha256 === sha256Bytes(canonical)
        )));
        if (scenario.secret !== null) {
          assert.doesNotMatch(JSON.stringify(receipt), new RegExp(scenario.secret, "u"));
        }
      } finally {
        fs.rmSync(root, { recursive: true, force: true });
      }
    });
  }
});

test("rejected timed-out Skill prefixes retain only a fixed audit code and canonical stream", async (context) => {
  for (const scenario of [
    {
      name: "traversing Read path",
      secret: "must-not-leak-path",
      wikiInput: { file_path: "inputs/../must-not-leak-path.md" },
      auditCode: TRACE_CODES.PATH_TRAVERSAL,
    },
    {
      name: "unexpected Read content",
      secret: "must-not-leak-read-content",
      wikiInput: { file_path: "inputs/wiki.md", content: "must-not-leak-read-content" },
      auditCode: TRACE_CODES.READ_INPUT_INVALID,
    },
    {
      name: "safe but out-of-order Read path",
      secret: null,
      wikiInput: { file_path: null },
      auditCode: TRACE_CODES.PHASE_SEQUENCE_INVALID,
    },
  ]) {
    await context.test(scenario.name, () => {
      const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-timeout-unsafe-prefix-"));
      try {
        const fixture = preparePhaseWrapperFixture(root);
        const wikiInput = scenario.wikiInput.file_path === null
          ? { file_path: path.join(fixture.workspace, "inputs", "clarifications.md") }
          : scenario.wikiInput;
        const { result, receipt } = runFakeSkillWrapper(
          root,
          fixture,
          skillPrefixFakeSource(fixture, { batchPending: true, wikiInput }),
        );
        assertTimedOutSkillPrefix(result, receipt, fixture);
        assert.deepEqual(receipt.tool_trace_audit, {
          schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
          status: "FAIL",
          workflow: "skill-generation",
          code: TRACE_CODES.INCOMPLETE_PREFIX_REJECTED,
          audit_code: scenario.auditCode,
          stream_state: "TERMINAL_MISSING",
          stream: structuredClone(receipt.stream),
        });
        assert.equal(validSkillGenerationIncompleteAuditRejectedReceipt(receipt.tool_trace_audit), true);
        assert.equal(validSkillGenerationFailedTraceAuditReceipt(receipt.tool_trace_audit), true);
        assert.equal(validSkillGenerationIncompleteTraceAuditReceipt(receipt.tool_trace_audit), false);
        assert.equal(validSkillGenerationTraceAuditReceipt(receipt.tool_trace_audit), false);
        if (scenario.secret !== null) {
          assert.doesNotMatch(JSON.stringify(receipt), new RegExp(scenario.secret, "u"));
        }
        assert.doesNotMatch(JSON.stringify(receipt.tool_trace_audit), /file_path|content|raw|message|details|\.\./iu);
      } finally {
        fs.rmSync(root, { recursive: true, force: true });
      }
    });
  }
});

test("a late init in a parse-complete terminal-less timeout seals only INIT_INVALID", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-timeout-late-init-"));
  try {
    const fixture = preparePhaseWrapperFixture(root);
    const { result, receipt } = runFakeSkillWrapper(root, fixture, `
const model = process.argv[process.argv.indexOf("--model") + 1];
const emit = (value) => console.log(JSON.stringify(value));
emit({type:"assistant",message:{role:"assistant",content:[]}});
emit({type:"system",subtype:"init",cwd:process.cwd(),model,permissionMode:"dontAsk",tools:["Read","Skill","StructuredOutput"]});
setInterval(() => {}, 1000);
`);
    assertTimedOutSkillPrefix(result, receipt, fixture);
    assert.deepEqual(receipt.tool_trace_audit, {
      schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
      status: "FAIL",
      workflow: "skill-generation",
      code: TRACE_CODES.INCOMPLETE_PREFIX_REJECTED,
      audit_code: TRACE_CODES.INIT_INVALID,
      stream_state: "TERMINAL_MISSING",
      stream: structuredClone(receipt.stream),
    });
    assert.equal(validSkillGenerationIncompleteAuditRejectedReceipt(receipt.tool_trace_audit), true);
    assert.equal(validSkillGenerationTraceAuditReceipt(receipt.tool_trace_audit), false);
    assert.doesNotMatch(JSON.stringify(receipt.tool_trace_audit), /content|thinking|raw|message|details|file_path/iu);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("terminal-less event types are frozen without leaking unknown or error payloads", async (context) => {
  await context.test("known error event seals only STREAM_ERROR", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-timeout-error-event-"));
    try {
      const fixture = preparePhaseWrapperFixture(root);
      const secret = "must-not-leak-provider-error";
      const { result, receipt } = runFakeSkillWrapper(root, fixture, `
const model = process.argv[process.argv.indexOf("--model") + 1];
const emit = (value) => console.log(JSON.stringify(value));
emit({type:"system",subtype:"init",cwd:process.cwd(),model,permissionMode:"dontAsk",tools:["Read","Skill","StructuredOutput"]});
emit({type:"error",message:${JSON.stringify(secret)}});
setInterval(() => {}, 1000);
`);
      assertTimedOutSkillPrefix(result, receipt, fixture);
      assert.equal(receipt.stream.last_event_type, "error");
      assert.equal(receipt.tool_trace_audit.code, TRACE_CODES.INCOMPLETE_PREFIX_REJECTED);
      assert.equal(receipt.tool_trace_audit.audit_code, TRACE_CODES.STREAM_ERROR);
      assert.equal(validSkillGenerationIncompleteAuditRejectedReceipt(receipt.tool_trace_audit), true);
      assert.doesNotMatch(JSON.stringify(receipt), new RegExp(secret, "u"));
      assert.doesNotMatch(JSON.stringify(receipt.tool_trace_audit), /message|details|content|raw/iu);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  await context.test("unknown event type is reduced to null and the canary is absent", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-timeout-unknown-event-"));
    try {
      const fixture = preparePhaseWrapperFixture(root);
      const secretType = "secret-token-like-value";
      const { result, receipt } = runFakeSkillWrapper(root, fixture, `
const model = process.argv[process.argv.indexOf("--model") + 1];
const emit = (value) => console.log(JSON.stringify(value));
emit({type:"system",subtype:"init",cwd:process.cwd(),model,permissionMode:"dontAsk",tools:["Read","Skill","StructuredOutput"]});
emit({type:${JSON.stringify(secretType)},payload:${JSON.stringify(secretType)}});
setInterval(() => {}, 1000);
`);
      assertTimedOutSkillPrefix(result, receipt, fixture);
      assert.equal(receipt.stream.last_event_type, null);
      assert.equal(receipt.tool_trace_audit, null);
      assert.doesNotMatch(JSON.stringify(receipt), new RegExp(secretType, "u"));
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
});

test("malformed JSON and invalid UTF-8 remain STREAM_INVALID with null Skill audit", async (context) => {
  const cases = [
    ["malformed JSON line", `
const model = process.argv[process.argv.indexOf("--model") + 1];
console.log(JSON.stringify({type:"system",subtype:"init",cwd:process.cwd(),model,permissionMode:"dontAsk",tools:["Read","Skill","StructuredOutput"]}));
process.stdout.write("{malformed-json-line\\n");
`],
    ["invalid UTF-8", `
const model = process.argv[process.argv.indexOf("--model") + 1];
const init = Buffer.from(JSON.stringify({type:"system",subtype:"init",cwd:process.cwd(),model,permissionMode:"dontAsk",tools:["Read","Skill","StructuredOutput"]}) + "\\n", "utf8");
process.stdout.write(Buffer.concat([init, Buffer.from([0xff, 0xfe, 0x0a])]));
`],
  ];
  for (const [name, source] of cases) {
    await context.test(name, () => {
      const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-skill-stream-invalid-"));
      try {
        const fixture = preparePhaseWrapperFixture(root);
        const { result, receipt } = runFakeSkillWrapper(root, fixture, source, { hardTimeoutSeconds: 30 });
        assert.equal(result.status, 1, result.stderr);
        assert.equal(result.stdout, "");
        assert.equal(result.stderr, "WRAPPER_MODEL_STREAM_INVALID\n");
        assert.notEqual(receipt, null);
        assert.deepEqual(receipt.wrapper_outcome, {
          schema_version: 1,
          status: "FAIL",
          code: "WRAPPER_MODEL_STREAM_INVALID",
        });
        assert.equal(receipt.tool_trace_audit, null);
        assert.equal(receipt.terminal, null);
        assert.equal(receipt.usage, null);
        assert.equal(receipt.turns, null);
        assert.equal(receipt.usage_complete, false);
        assert.equal(receipt.timed_out, false);
        assert.equal(receipt.stream.complete, false);
        assert.equal(receipt.process.wrapper_exit_code, 1);
        assert.equal(fs.existsSync(path.join(fixture.workspace, "output", "generation-spec.json")), false);
        assert.doesNotMatch(JSON.stringify(receipt), /malformed-json-line|\ufffd|content|raw/iu);
      } finally {
        fs.rmSync(root, { recursive: true, force: true });
      }
    });
  }
});

test("a non-timeout stream without a terminal result persists STREAM_INVALID evidence", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-stream-invalid-"));
  try {
    const usageRoot = path.join(root, "usage");
    const fakeClaude = path.join(root, "fake-claude.mjs");
    const settings = path.join(root, "settings.json");
    fs.writeFileSync(settings, "{}\n");
    fs.writeFileSync(fakeClaude, `
const model = process.argv[process.argv.indexOf("--model") + 1];
console.log(JSON.stringify({type:"system",subtype:"init",model}));
console.log(JSON.stringify({type:"assistant",message:{id:"incomplete",content:[]}}));
`);
    const result = spawnSync(process.execPath, [
      WRAPPER,
      "--claude-entry", fakeClaude,
      "--settings", settings,
      "--model", "test-model",
      "--usage-root", usageRoot,
      "--max-turns", "12",
      "--max-total-tokens", "1000000",
      "--max-budget-usd", "10",
      "--hard-timeout-seconds", "30",
      "--workflow", "job",
    ], {
      cwd: root,
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "", HOME: root },
    });
    assert.equal(result.status, 1, result.stderr);
    assert.match(result.stderr, /WRAPPER_MODEL_STREAM_INVALID/);
    const [receiptFile] = fs.readdirSync(usageRoot);
    const receipt = JSON.parse(fs.readFileSync(path.join(usageRoot, receiptFile), "utf8"));
    assert.deepEqual(receipt.wrapper_outcome, {
      schema_version: 1,
      status: "FAIL",
      code: "WRAPPER_MODEL_STREAM_INVALID",
    });
    assert.equal(receipt.usage_complete, false);
    assert.equal(receipt.usage, null);
    assert.equal(receipt.terminal, null);
    assert.equal(receipt.turns, null);
    assert.equal(receipt.timed_out, false);
    assert.deepEqual(receipt.process, { exit_code: 0, signal: null, wrapper_exit_code: 1 });
    assert.deepEqual(receipt.stream, {
      schema_version: 1,
      event_count: 2,
      parsed_event_count: 2,
      init_count: 1,
      result_count: 0,
      last_event_type: "assistant",
      complete: false,
    });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("mismatched model and malformed terminal fields never enter the receipt", async (context) => {
  const requestedModel = "test-model";
  const cases = [
    {
      name: "mismatched absolute model",
      initModel: "C:\\private\\must-not-enter-receipt",
      terminal: { subtype: "success", is_error: false },
      expectedModel: null,
      secret: "C:\\private\\must-not-enter-receipt",
    },
    {
      name: "unsafe terminal subtype",
      initModel: requestedModel,
      terminal: { subtype: "C:\\private\\terminal-secret", is_error: true },
      expectedModel: requestedModel,
      secret: "C:\\private\\terminal-secret",
    },
    {
      name: "non-boolean terminal is_error",
      initModel: requestedModel,
      terminal: { subtype: "error_max_turns", is_error: "true" },
      expectedModel: requestedModel,
      secret: "error_max_turns",
    },
  ];
  for (const item of cases) {
    await context.test(item.name, () => {
      const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-terminal-shape-"));
      try {
        const { result, receipt } = runFakeJobWrapper(root, `
console.log(JSON.stringify({type:"system",subtype:"init",model:${JSON.stringify(item.initModel)}}));
console.log(JSON.stringify({type:"result",subtype:${JSON.stringify(item.terminal.subtype)},is_error:${JSON.stringify(item.terminal.is_error)},num_turns:1,total_cost_usd:0.01,usage:{input_tokens:10,output_tokens:20,cache_creation_input_tokens:0,cache_read_input_tokens:0}}));
`);
        assert.equal(result.status, 1);
        assert.match(result.stderr, /WRAPPER_MODEL_STREAM_INVALID/);
        assert.notEqual(receipt, null);
        assert.equal(receipt.effective_model, item.expectedModel);
        assert.equal(receipt.terminal, null);
        assert.equal(receipt.usage, null);
        assert.equal(receipt.usage_complete, false);
        assert.equal(receipt.stream.complete, false);
        assert.doesNotMatch(JSON.stringify(receipt), new RegExp(item.secret.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "u"));
      } finally {
        fs.rmSync(root, { recursive: true, force: true });
      }
    });
  }
});

test("raw usage and cost reject string boolean and array coercions", async (context) => {
  const validUsage = {
    input_tokens: 10,
    output_tokens: 20,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
  };
  const cases = [
    ["zero total tokens", { input_tokens: 0, output_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 }, 0],
    ["token string", { ...validUsage, input_tokens: "10" }, 0.01],
    ["token boolean", { ...validUsage, output_tokens: true }, 0.01],
    ["token array", { ...validUsage, cache_read_input_tokens: [0] }, 0.01],
    ["token negative", { ...validUsage, input_tokens: -1 }, 0.01],
    ["token unsafe integer", { ...validUsage, input_tokens: Number.MAX_SAFE_INTEGER + 1 }, 0.01],
    ["cost string", validUsage, "0.01"],
    ["cost boolean", validUsage, false],
    ["cost array", validUsage, [0.01]],
    ["cost negative", validUsage, -0.01],
  ];
  for (const [name, usage, cost] of cases) {
    await context.test(name, () => {
      const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-raw-usage-"));
      try {
        const { result, receipt } = runFakeJobWrapper(root, `
const model = process.argv[process.argv.indexOf("--model") + 1];
console.log(JSON.stringify({type:"system",subtype:"init",model}));
console.log(JSON.stringify({type:"result",subtype:"success",is_error:false,num_turns:1,total_cost_usd:${JSON.stringify(cost)},usage:${JSON.stringify(usage)}}));
`);
        assert.equal(result.status, 1);
        assert.match(result.stderr, /WRAPPER_MODEL_USAGE_INVALID/);
        assert.notEqual(receipt, null);
        assert.equal(receipt.effective_model, "test-model");
        assert.equal(receipt.stream.complete, true);
        assert.equal(receipt.usage_complete, false);
        assert.equal(receipt.usage, null);
        assert.equal(receipt.terminal, null);
        assert.equal(receipt.turns, null);
      } finally {
        fs.rmSync(root, { recursive: true, force: true });
      }
    });
  }
});

test("invalid UTF-8 makes the whole stream invalid without retaining decoded replacements", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-invalid-utf8-"));
  try {
    const { result, receipt } = runFakeJobWrapper(root, `
const model = process.argv[process.argv.indexOf("--model") + 1];
const init = Buffer.from(JSON.stringify({type:"system",subtype:"init",model}) + "\\n", "utf8");
const invalid = Buffer.from([0xff, 0xfe, 0x0a]);
const terminal = Buffer.from(JSON.stringify({type:"result",subtype:"success",is_error:false,num_turns:1,total_cost_usd:0.01,usage:{input_tokens:10,output_tokens:20,cache_creation_input_tokens:0,cache_read_input_tokens:0}}) + "\\n", "utf8");
process.stdout.write(Buffer.concat([init, invalid, terminal]));
`);
    assert.equal(result.status, 1);
    assert.match(result.stderr, /WRAPPER_MODEL_STREAM_INVALID/);
    assert.notEqual(receipt, null);
    assert.equal(receipt.effective_model, null);
    assert.equal(receipt.terminal, null);
    assert.equal(receipt.usage, null);
    assert.deepEqual(receipt.stream, {
      schema_version: 1,
      event_count: 0,
      parsed_event_count: 0,
      init_count: 0,
      result_count: 0,
      last_event_type: null,
      complete: false,
    });
    assert.doesNotMatch(JSON.stringify(receipt), /\ufffd|private|content|raw/iu);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("the wrapper requires init first even for a non-Skill workflow", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-late-init-"));
  try {
    const { result, receipt } = runFakeJobWrapper(root, `
const model = process.argv[process.argv.indexOf("--model") + 1];
console.log(JSON.stringify({type:"assistant",message:{role:"assistant",content:[]} }));
console.log(JSON.stringify({type:"system",subtype:"init",model}));
console.log(JSON.stringify({type:"result",subtype:"success",is_error:false,num_turns:1,total_cost_usd:0.01,usage:{input_tokens:10,output_tokens:20,cache_creation_input_tokens:0,cache_read_input_tokens:0}}));
`);
    assert.equal(result.status, 1);
    assert.match(result.stderr, /WRAPPER_MODEL_STREAM_INVALID/);
    assert.notEqual(receipt, null);
    assert.equal(receipt.effective_model, "test-model");
    assert.equal(receipt.terminal, null);
    assert.equal(receipt.usage, null);
    assert.equal(receipt.usage_complete, false);
    assert.equal(receipt.stream.complete, false);
    assert.equal(receipt.stream.init_count, 1);
    assert.equal(receipt.stream.result_count, 1);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("a failed Skill trace writes the strict nested audit schema without raw tool data", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-skill-trace-"));
  try {
    const workspace = path.join(root, "workspace");
    const sourceRoot = path.join(root, "source");
    const skillRoot = path.join(root, "skill");
    const usageRoot = path.join(root, "usage");
    const fakeClaude = path.join(root, "fake-claude.mjs");
    const settings = path.join(root, "settings.json");
    fs.mkdirSync(path.join(workspace, "inputs"), { recursive: true });
    fs.mkdirSync(path.join(workspace, "output"));
    fs.mkdirSync(sourceRoot);
    fs.mkdirSync(path.join(skillRoot, "references", "checkpoints"), { recursive: true });
    fs.writeFileSync(path.join(workspace, "inputs", "wiki.md"), "wiki\n");
    fs.writeFileSync(path.join(workspace, "inputs", "clarifications.md"), "clarifications\n");
    fs.writeFileSync(path.join(skillRoot, "SKILL.md"), [
      "[generation](references/generation-spec-v6-reference.md)",
      "[verification](references/verification-contract-v2-reference.md)",
      ...SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES.map((relative, index) => `[checkpoint ${index + 1}](${relative})`),
      "",
    ].join("\n"));
    fs.writeFileSync(path.join(skillRoot, "references", "generation-spec-v6-reference.md"), "generation\n");
    fs.writeFileSync(path.join(skillRoot, "references", "verification-contract-v2-reference.md"), "verification\n");
    for (const relative of SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES) {
      fs.writeFileSync(path.join(skillRoot, ...relative.split("/")), `${relative}\n`);
    }
    fs.writeFileSync(settings, "{}\n");
    const validatorScript = installFakeValidator(sourceRoot);
    fs.writeFileSync(fakeClaude, `
const model = process.argv[process.argv.indexOf("--model") + 1];
console.log(JSON.stringify({type:"system",subtype:"init",cwd:process.cwd(),model,permissionMode:"dontAsk",tools:["Read","Skill","StructuredOutput"]}));
console.log(JSON.stringify({
  type:"result",subtype:"success",is_error:false,result:"DONE",num_turns:1,total_cost_usd:0.01,
  structured_output:${JSON.stringify(minimalValidBlueprint())},
  usage:{input_tokens:10,output_tokens:20,cache_creation_input_tokens:0,cache_read_input_tokens:0}
}));
`);
    const result = spawnSync(process.execPath, [
      WRAPPER,
      "--claude-entry", fakeClaude,
      "--settings", settings,
      "--model", "test-model",
      "--usage-root", usageRoot,
      "--max-turns", "12",
      "--max-total-tokens", "1000000",
      "--max-budget-usd", "10",
      "--hard-timeout-seconds", "30",
      "--workflow", "skill-generation",
      "--skill-root", skillRoot,
      "--source-root", sourceRoot,
      ...validatorArguments({ validatorScript }),
    ], {
      cwd: workspace,
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "", HOME: root },
    });
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /WRAPPER_SKILL_TRACE_INVALID/);
    assert.equal(result.stdout, "");
    assert.equal(result.stderr.trim(), "WRAPPER_SKILL_TRACE_INVALID");
    const [receiptFile] = fs.readdirSync(usageRoot);
    const receipt = JSON.parse(fs.readFileSync(path.join(usageRoot, receiptFile), "utf8"));
    assert.deepEqual(receipt.tool_trace_audit, {
      schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
      status: "FAIL",
      workflow: "skill-generation",
      code: "SKILL_TRACE_SKILL_INVOCATION_INVALID",
    });
    assert.doesNotMatch(JSON.stringify(receipt.tool_trace_audit), /wiki|clarification|content|file_path|permission denied/i);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("error_max_turns preserves a content-free partial trace without materializing StructuredOutput", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-partial-trace-"));
  try {
    const fixture = preparePhaseWrapperFixture(root);
    const fakeClaude = path.join(root, "fake-claude.mjs");
    const secret = "must-not-leak-from-model-stream";
    const reads = [
      ["wiki", path.join(fixture.workspace, "inputs", "wiki.md")],
      ["clarifications", path.join(fixture.workspace, "inputs", "clarifications.md")],
      ...fixture.references.map((relative, index) => [
        `reference-${index}`,
        path.join(fixture.skillRoot, ...relative.split("/")),
      ]),
    ];
    fs.writeFileSync(fakeClaude, `
const model = process.argv[process.argv.indexOf("--model") + 1];
const emit = (value) => console.log(JSON.stringify(value));
const use = (id, name, input) => emit({type:"assistant",message:{role:"assistant",content:[{type:"tool_use",id,name,input}]}});
const result = (id, name) => emit({type:"user",message:{role:"user",content:[{type:"tool_result",tool_use_id:id,is_error:false}]},tool_use_result:name === "Skill" ? {success:true} : {type:name.toLowerCase()}});
emit({type:"system",subtype:"init",cwd:process.cwd(),model,permissionMode:"dontAsk",tools:["Read","Skill","StructuredOutput"]});
use("skill", "Skill", {skill:"wiki-to-diagnosis-skill"}); result("skill", "Skill");
for (const [id, file_path] of ${JSON.stringify(reads)}) { use(id, "Read", {file_path}); result(id, "Read"); }
const structuredOutput = {schema_version:6, private_payload:{content:${JSON.stringify(secret)}, thinking:${JSON.stringify(secret)}}};
use("structured-output", "StructuredOutput", structuredOutput); result("structured-output", "StructuredOutput");
emit({type:"result",subtype:"error_max_turns",is_error:true,num_turns:13,total_cost_usd:5.088489,usage:{input_tokens:57655,output_tokens:178246,cache_creation_input_tokens:0,cache_read_input_tokens:688128},errors:[${JSON.stringify(secret)}]});
process.exitCode = 1;
`);
    const result = spawnSync(process.execPath, [
      WRAPPER,
      "--claude-entry", fakeClaude,
      "--settings", fixture.settings,
      "--model", "test-model",
      "--usage-root", fixture.usageRoot,
      "--max-turns", "12",
      "--max-total-tokens", "1000000",
      "--max-output-tokens", "64000",
      "--max-output-tokens-upper-limit", "64000",
      "--max-budget-usd", "10",
      "--hard-timeout-seconds", "1800",
      "--workflow", "skill-generation",
      "--skill-root", fixture.skillRoot,
      "--source-root", fixture.sourceRoot,
      ...validatorArguments(fixture),
    ], {
      cwd: fixture.workspace,
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "", HOME: root },
    });
    assert.equal(result.status, 1);
    assert.equal(result.stdout, "");
    assert.equal(result.stderr, "WRAPPER_MODEL_CAP_EXCEEDED\n");
    assert.equal(fs.existsSync(path.join(fixture.workspace, "output", "generation-spec.json")), false);
    const [receiptFile] = fs.readdirSync(fixture.usageRoot);
    const receipt = JSON.parse(fs.readFileSync(path.join(fixture.usageRoot, receiptFile), "utf8"));
    assert.deepEqual(receipt.wrapper_outcome, {
      schema_version: 1,
      status: "FAIL",
      code: "WRAPPER_MODEL_CAP_EXCEEDED",
    });
    assert.deepEqual(receipt.terminal, { subtype: "error_max_turns", is_error: true });
    assert.equal(receipt.turns, 13);
    assert.equal(receipt.usage_complete, true);
    assert.equal(receipt.usage.total_tokens, 924029);
    assert.equal(receipt.hard_cap_enforcement.structured_output_retries, ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_ENFORCEMENT);
    assert.equal(validSkillGenerationPartialTraceAuditReceipt(receipt.tool_trace_audit), true);
    assert.equal(validSkillGenerationTraceAuditReceipt(receipt.tool_trace_audit), false);
    assert.deepEqual(receipt.tool_trace_audit.terminal, receipt.terminal);
    assert.equal(receipt.tool_trace_audit.tool_sequence.at(-1).tool, "StructuredOutput");
    assert.match(receipt.tool_trace_audit.tool_sequence.at(-1).sha256, /^[a-f0-9]{64}$/u);
    assert.doesNotMatch(JSON.stringify(receipt), new RegExp(secret, "u"));
    assert.doesNotMatch(JSON.stringify(receipt.tool_trace_audit), /private_payload|thinking|content|raw|file_path/iu);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("the child-only retry limit is two and a second StructuredOutput remains a failed partial trace", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-structured-retry-bound-"));
  try {
    const fixture = preparePhaseWrapperFixture(root);
    const fakeClaude = path.join(root, "fake-claude.mjs");
    fs.writeFileSync(fakeClaude, `
const model = process.argv[process.argv.indexOf("--model") + 1];
if (process.env.MAX_STRUCTURED_OUTPUT_RETRIES !== "2") process.exit(9);
const emit = (value) => console.log(JSON.stringify(value));
const use = (id, input) => emit({type:"assistant",message:{role:"assistant",content:[{type:"tool_use",id,name:"StructuredOutput",input}]}});
const result = (id) => emit({type:"user",message:{role:"user",content:[{type:"tool_result",tool_use_id:id,is_error:false}]},tool_use_result:{type:"structuredoutput"}});
emit({type:"system",subtype:"init",cwd:process.cwd(),model,permissionMode:"dontAsk",tools:["Read","Skill","StructuredOutput"]});
use("structured-output-1", {}); result("structured-output-1");
use("structured-output-2", {}); result("structured-output-2");
emit({type:"result",subtype:"error_max_structured_output_retries",is_error:true,num_turns:3,total_cost_usd:0.01,usage:{input_tokens:10,output_tokens:20,cache_creation_input_tokens:0,cache_read_input_tokens:0}});
process.exitCode = 1;
`);
    const result = spawnSync(process.execPath, [
      WRAPPER,
      "--claude-entry", fakeClaude,
      "--settings", fixture.settings,
      "--model", "test-model",
      "--usage-root", fixture.usageRoot,
      "--max-turns", "12",
      "--max-total-tokens", "1000000",
      "--max-budget-usd", "10",
      "--hard-timeout-seconds", "1800",
      "--workflow", "skill-generation",
      "--skill-root", fixture.skillRoot,
      "--source-root", fixture.sourceRoot,
      ...validatorArguments(fixture),
    ], {
      cwd: fixture.workspace,
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "", HOME: root },
    });
    assert.equal(result.status, 1);
    assert.equal(result.stderr, "WRAPPER_MODEL_TERMINAL_INVALID\n");
    const [receiptFile] = fs.readdirSync(fixture.usageRoot);
    const receipt = JSON.parse(fs.readFileSync(path.join(fixture.usageRoot, receiptFile), "utf8"));
    assert.equal(receipt.tool_trace_audit.tool_sequence.filter((record) => record.tool === "StructuredOutput").length, 2);
    assert.equal(receipt.environment_policy.inbound.key_names.includes(ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY), false);
    assert.equal(receipt.environment_policy.claude_process.key_names.includes(ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY), true);
    assert.equal(receipt.hard_cap_enforcement.structured_output_retries, ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_ENFORCEMENT);
    assert.equal(fs.existsSync(path.join(fixture.workspace, "output", "generation-spec.json")), false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("an unsafe unsuccessful trace fails closed to null without exposing the raw path", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-partial-trace-unsafe-"));
  try {
    const fixture = preparePhaseWrapperFixture(root);
    const fakeClaude = path.join(root, "fake-claude.mjs");
    const unsafePath = "inputs/../must-not-leak.md";
    fs.writeFileSync(fakeClaude, `
const model = process.argv[process.argv.indexOf("--model") + 1];
const emit = (value) => console.log(JSON.stringify(value));
emit({type:"system",subtype:"init",cwd:process.cwd(),model,permissionMode:"dontAsk",tools:["Read","Skill","StructuredOutput"]});
emit({type:"assistant",message:{role:"assistant",content:[{type:"tool_use",id:"unsafe",name:"Read",input:{file_path:${JSON.stringify(unsafePath)}}}]}});
emit({type:"user",message:{role:"user",content:[{type:"tool_result",tool_use_id:"unsafe",is_error:false}]},tool_use_result:{type:"read"}});
emit({type:"result",subtype:"error_max_turns",is_error:true,num_turns:12,total_cost_usd:0.01,usage:{input_tokens:10,output_tokens:20,cache_creation_input_tokens:0,cache_read_input_tokens:0}});
process.exitCode = 1;
`);
    const result = spawnSync(process.execPath, [
      WRAPPER,
      "--claude-entry", fakeClaude,
      "--settings", fixture.settings,
      "--model", "test-model",
      "--usage-root", fixture.usageRoot,
      "--max-turns", "12",
      "--max-total-tokens", "1000000",
      "--max-budget-usd", "10",
      "--hard-timeout-seconds", "1800",
      "--workflow", "skill-generation",
      "--skill-root", fixture.skillRoot,
      "--source-root", fixture.sourceRoot,
      ...validatorArguments(fixture),
    ], {
      cwd: fixture.workspace,
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "", HOME: root },
    });
    assert.equal(result.status, 1);
    assert.equal(result.stdout, "");
    assert.equal(result.stderr, "WRAPPER_MODEL_TERMINAL_INVALID\n");
    const [receiptFile] = fs.readdirSync(fixture.usageRoot);
    const receipt = JSON.parse(fs.readFileSync(path.join(fixture.usageRoot, receiptFile), "utf8"));
    assert.equal(receipt.tool_trace_audit, null);
    assert.doesNotMatch(JSON.stringify(receipt), /must-not-leak|\.\.|file_path|thinking|content|raw/iu);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("a compiler rejection seals only its fixed rule IR constraint and input digest", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-rule-ir-diagnostic-"));
  try {
    const fixture = preparePhaseWrapperFixture(root);
    const observedEnvironment = path.join(root, "compiler-environment.txt");
    fs.writeFileSync(fixture.validatorScript, `
require("node:fs").writeFileSync(${JSON.stringify(observedEnvironment)}, process.env.PYTHONDONTWRITEBYTECODE ?? "");
process.stdin.resume();
process.stdin.on("end", () => {
  process.stderr.write(JSON.stringify({schema_version:1,phase:"COMPILER",constraint_id:"POSITION_ORDINALS"}) + "\\n");
  process.exitCode = 1;
});
`);
    const structuredOutput = minimalValidBlueprint();
    const { result, receipt } = runFakeSkillWrapper(
      root,
      fixture,
      completeSkillFakeSource(fixture, structuredOutput),
      { hardTimeoutSeconds: 30 },
    );
    const irBytes = Buffer.from(canonicalJson(structuredOutput), "utf8");
    assert.equal(result.status, 1, result.stderr);
    assert.equal(result.stdout, "");
    assert.equal(result.stderr, "WRAPPER_SKILL_TRACE_INVALID\n");
    assert.deepEqual(receipt.tool_trace_audit, {
      schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
      status: "FAIL",
      workflow: "skill-generation",
      code: TRACE_CODES.RULE_IR_INVALID,
      diagnostic: {
        schema_version: 1,
        phase: "COMPILER",
        constraint_id: "POSITION_ORDINALS",
        ir: {
          size_bytes: irBytes.length,
          sha256: sha256Bytes(irBytes),
        },
      },
    });
    assert.equal(validSkillGenerationFailedTraceAuditReceipt(receipt.tool_trace_audit), true);
    assert.equal(fs.readFileSync(observedEnvironment, "utf8"), "1");
    assert.equal(fs.existsSync(path.join(fixture.workspace, "output", "generation-spec.json")), false);
    assert.doesNotMatch(JSON.stringify(receipt), /content|message|raw|must-not-leak/iu);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("StructuredOutput is canonicalized and atomically materialized with sealed CLI arguments", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-skill-structured-output-"));
  try {
    const workspace = path.join(root, "workspace");
    const sourceRoot = path.join(root, "source");
    const skillRoot = path.join(root, "skill");
    const usageRoot = path.join(root, "usage");
    const fakeClaude = path.join(root, "fake-claude.mjs");
    const settings = path.join(root, "settings.json");
    const outputPath = path.join(workspace, "output", "generation-spec.json");
    const observedArguments = path.join(root, "observed-arguments.json");
    fs.mkdirSync(path.join(workspace, "inputs"), { recursive: true });
    fs.mkdirSync(path.join(workspace, "output"));
    fs.mkdirSync(sourceRoot);
    fs.mkdirSync(path.join(skillRoot, "references", "checkpoints"), { recursive: true });
    fs.writeFileSync(path.join(workspace, "inputs", "wiki.md"), "wiki\n");
    fs.writeFileSync(path.join(workspace, "inputs", "clarifications.md"), "clarifications\n");
    const references = [
      "references/generation-spec-v6-reference.md",
      "references/verification-contract-v2-reference.md",
      ...SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES,
    ];
    fs.writeFileSync(path.join(skillRoot, "SKILL.md"), references.map((relative, index) => `[reference ${index}](${relative})`).join("\n") + "\n");
    for (const relative of references) {
      const target = path.join(skillRoot, ...relative.split("/"));
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.writeFileSync(target, `${relative}\n`);
    }
    fs.writeFileSync(settings, "{}\n");
    const validatorScript = installFakeValidator(sourceRoot);
    const reads = [
      ["wiki", path.join(workspace, "inputs", "wiki.md")],
      ["clarifications", path.join(workspace, "inputs", "clarifications.md")],
      ["generation", path.join(skillRoot, "references", "generation-spec-v6-reference.md")],
      ["verification", path.join(skillRoot, "references", "verification-contract-v2-reference.md")],
      ...SKILL_GENERATION_PHASE_CHECKPOINT_REFERENCES.map((relative, index) => [
        `checkpoint-${index + 1}`,
        path.join(skillRoot, ...relative.split("/")),
      ]),
    ];
    const structuredOutput = minimalValidBlueprint();
    const expandedOutput = minimalValidSubmission();
    assert.equal(validGenerationBlueprintSubmission(structuredOutput), true);
    fs.writeFileSync(fakeClaude, `
import fs from "node:fs";
fs.writeFileSync(${JSON.stringify(observedArguments)}, JSON.stringify({argv:process.argv.slice(2),structured_output_retries:process.env.MAX_STRUCTURED_OUTPUT_RETRIES??null}));
const model = process.argv[process.argv.indexOf("--model") + 1];
const emit = (value) => console.log(JSON.stringify(value));
const use = (id, name, input) => emit({type:"assistant",message:{role:"assistant",content:[{type:"tool_use",id,name,input}]}});
const result = (id, name) => emit({type:"user",message:{role:"user",content:[{type:"tool_result",tool_use_id:id,is_error:false}]},tool_use_result:name === "Skill" ? {success:true} : {type:name.toLowerCase()}});
emit({type:"system",subtype:"init",cwd:process.cwd(),model,permissionMode:"dontAsk",tools:["Read","Skill","StructuredOutput"]});
use("skill", "Skill", {skill:"wiki-to-diagnosis-skill"}); result("skill", "Skill");
const reads = ${JSON.stringify(reads)};
emit({type:"assistant",message:{role:"assistant",content:reads.slice(0,2).map(([id,file_path])=>({type:"tool_use",id,name:"Read",input:{file_path}}))}});
emit({type:"user",message:{role:"user",content:reads.slice(0,2).map(([id])=>({type:"tool_result",tool_use_id:id,is_error:false}))},tool_use_result:{type:"read"}});
for (const [id, file_path] of reads.slice(2)) { use(id, "Read", {file_path}); result(id, "Read"); }
const structuredOutput = ${JSON.stringify(structuredOutput)};
use("structured-output", "StructuredOutput", structuredOutput); result("structured-output", "StructuredOutput");
emit({type:"result",subtype:"success",is_error:false,result:"DONE",structured_output:structuredOutput,num_turns:10,total_cost_usd:0.01,usage:{input_tokens:10,output_tokens:20,cache_creation_input_tokens:0,cache_read_input_tokens:0}});
`);
    const result = spawnSync(process.execPath, [
      WRAPPER,
      "--claude-entry", fakeClaude,
      "--settings", settings,
      "--model", "test-model",
      "--usage-root", usageRoot,
      "--max-turns", "12",
      "--max-total-tokens", "1000000",
      "--max-budget-usd", "10",
      "--hard-timeout-seconds", "30",
      "--workflow", "skill-generation",
      "--skill-root", skillRoot,
      "--source-root", sourceRoot,
      ...validatorArguments({ validatorScript }),
    ], {
      cwd: workspace,
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "", HOME: root },
    });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, "");
    assert.equal(result.stderr, "");
    assert.equal(fs.readFileSync(outputPath, "utf8"), canonicalJson(expandedOutput));
    const observed = JSON.parse(fs.readFileSync(observedArguments, "utf8"));
    assert.equal(observed.argv[observed.argv.indexOf("--tools") + 1], "Read,Skill,StructuredOutput");
    assert.deepEqual(JSON.parse(observed.argv[observed.argv.indexOf("--json-schema") + 1]), GENERATION_BLUEPRINT_SUBMISSION_JSON_SCHEMA);
    assert.notDeepEqual(JSON.parse(observed.argv[observed.argv.indexOf("--json-schema") + 1]), { type: "object" });
    assert.equal(observed.structured_output_retries, String(ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_LIMIT));
    const [receiptFile] = fs.readdirSync(usageRoot);
    const receipt = JSON.parse(fs.readFileSync(path.join(usageRoot, receiptFile), "utf8"));
    assert.deepEqual(receipt.wrapper_outcome, { schema_version: 1, status: "PASS", code: null });
    assert.equal(receipt.tool_trace_audit.schema_version, SKILL_GENERATION_TRACE_SCHEMA_VERSION);
    assert.equal(receipt.tool_trace_audit.tool_sequence.at(-1).tool, "StructuredOutput");
    assert.equal(receipt.tool_trace_audit.ir_input.ordinal, 9);
    assert.equal(receipt.tool_trace_audit.ir_input.size_bytes, Buffer.byteLength(canonicalJson(structuredOutput)));
    assert.equal(receipt.tool_trace_audit.compiler.id, SKILL_GENERATION_RULE_IR.compiler_id);
    assert.equal(receipt.tool_trace_audit.compiler.version, SKILL_GENERATION_RULE_IR.compiler_version);
    assert.equal(receipt.tool_trace_audit.output.size_bytes, fs.statSync(outputPath).size);
    assert.equal(receipt.tool_trace_audit.attempt_policy.terminal_result, "DONE");
    assert.deepEqual(receipt.tool_trace_audit.terminal, { subtype: "success", is_error: false });
    assert.equal(Object.hasOwn(receipt.tool_trace_audit.terminal, "result"), false);
    assert.doesNotMatch(JSON.stringify(receipt.tool_trace_audit), /diagnose-test|content|file_path/iu);
    assert.equal(receipt.environment_policy.inbound.key_names.includes(ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY), false);
    assert.equal(receipt.environment_policy.claude_process.key_names.includes(ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY), true);
    assert.equal(receipt.hard_cap_enforcement.structured_output_retries, ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_ENFORCEMENT);

    const originalOutput = fs.readFileSync(outputPath);
    const replayUsageRoot = path.join(root, "usage-existing-output");
    const replay = spawnSync(process.execPath, [
      WRAPPER,
      "--claude-entry", fakeClaude,
      "--settings", settings,
      "--model", "test-model",
      "--usage-root", replayUsageRoot,
      "--max-turns", "12",
      "--max-total-tokens", "1000000",
      "--max-budget-usd", "10",
      "--hard-timeout-seconds", "30",
      "--workflow", "skill-generation",
      "--skill-root", skillRoot,
      "--source-root", sourceRoot,
      ...validatorArguments({ validatorScript }),
    ], {
      cwd: workspace,
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "", HOME: root },
    });
    assert.equal(replay.status, 1);
    assert.equal(replay.stdout, "");
    assert.equal(replay.stderr.trim(), "WRAPPER_SKILL_TRACE_INVALID");
    assert.deepEqual(fs.readFileSync(outputPath), originalOutput);
    const [replayReceiptFile] = fs.readdirSync(replayUsageRoot);
    const replayReceipt = JSON.parse(fs.readFileSync(path.join(replayUsageRoot, replayReceiptFile), "utf8"));
    assert.deepEqual(replayReceipt.wrapper_outcome, {
      schema_version: 1,
      status: "FAIL",
      code: "WRAPPER_SKILL_TRACE_INVALID",
    });
    assert.deepEqual(replayReceipt.tool_trace_audit, {
      schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
      status: "FAIL",
      workflow: "skill-generation",
      code: "SKILL_TRACE_AUDIT_FAILED",
    });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("a planned output token cap is injected only into the Claude child and sealed in the receipt", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-output-cap-"));
  try {
    const usageRoot = path.join(root, "usage");
    const fakeClaude = path.join(root, "fake-claude.mjs");
    const settings = path.join(root, "settings.json");
    const observed = path.join(root, "observed-output-tokens.txt");
    fs.writeFileSync(settings, "{}\n");
    fs.writeFileSync(fakeClaude, `
import fs from "node:fs";
const model = process.argv[process.argv.indexOf("--model") + 1];
fs.writeFileSync(${JSON.stringify(observed)}, process.env.CLAUDE_CODE_MAX_OUTPUT_TOKENS ?? "MISSING");
console.log(JSON.stringify({type:"system",subtype:"init",model}));
console.log(JSON.stringify({
  type:"result",subtype:"success",is_error:false,num_turns:1,total_cost_usd:0.01,
  usage:{input_tokens:10,output_tokens:20,cache_creation_input_tokens:0,cache_read_input_tokens:0},
  modelUsage:{[model]:{maxOutputTokens:32000},"auxiliary-model":{maxOutputTokens:64000}}
}));
`);
    const result = spawnSync(process.execPath, [
      WRAPPER,
      "--claude-entry", fakeClaude,
      "--settings", settings,
      "--model", "test-model",
      "--usage-root", usageRoot,
      "--max-turns", "12",
      "--max-total-tokens", "1000000",
      "--max-output-tokens", "64000",
      "--max-output-tokens-upper-limit", "64000",
      "--max-budget-usd", "10",
      "--hard-timeout-seconds", "30",
      "--workflow", "job",
    ], {
      cwd: root,
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "", HOME: root },
    });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(fs.readFileSync(observed, "utf8"), "64000");
    const files = fs.readdirSync(usageRoot);
    assert.equal(files.length, 1);
    const receipt = JSON.parse(fs.readFileSync(path.join(usageRoot, files[0]), "utf8"));
    assert.equal(receipt.effective_caps.max_output_tokens, 64000);
    assert.equal(receipt.hard_cap_enforcement.max_output_tokens, ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT);
    assert.equal(receipt.environment_policy.inbound.key_names.includes("CLAUDE_CODE_MAX_OUTPUT_TOKENS"), false);
    assert.equal(receipt.environment_policy.claude_process.key_names.includes("CLAUDE_CODE_MAX_OUTPUT_TOKENS"), true);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("terminal modelUsage is not required as a request-cap echo", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-output-cap-unproved-"));
  try {
    const usageRoot = path.join(root, "usage");
    const fakeClaude = path.join(root, "fake-claude.mjs");
    const settings = path.join(root, "settings.json");
    fs.writeFileSync(settings, "{}\n");
    fs.writeFileSync(fakeClaude, `
const model = process.argv[process.argv.indexOf("--model") + 1];
console.log(JSON.stringify({type:"system",subtype:"init",model}));
console.log(JSON.stringify({
  type:"result",subtype:"success",is_error:false,num_turns:1,total_cost_usd:0.01,
  usage:{input_tokens:10,output_tokens:20,cache_creation_input_tokens:0,cache_read_input_tokens:0}
}));
`);
    const result = spawnSync(process.execPath, [
      WRAPPER,
      "--claude-entry", fakeClaude,
      "--settings", settings,
      "--model", "test-model",
      "--usage-root", usageRoot,
      "--max-turns", "12",
      "--max-total-tokens", "1000000",
      "--max-output-tokens", "64000",
      "--max-output-tokens-upper-limit", "64000",
      "--max-budget-usd", "10",
      "--hard-timeout-seconds", "30",
      "--workflow", "job",
    ], {
      cwd: root,
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "", HOME: root },
    });
    assert.equal(result.status, 0, result.stderr);
    const [receiptFile] = fs.readdirSync(usageRoot);
    const receipt = JSON.parse(fs.readFileSync(path.join(usageRoot, receiptFile), "utf8"));
    assert.deepEqual(receipt.wrapper_outcome, { schema_version: 1, status: "PASS", code: null });
    assert.equal(receipt.process.wrapper_exit_code, 0);
    assert.equal(receipt.usage_complete, true);
    assert.deepEqual(receipt.terminal, { subtype: "success", is_error: false });
    assert.equal(receipt.stream.complete, true);
    assert.equal(receipt.stream.result_count, 1);
    assert.equal(receipt.hard_cap_enforcement.max_output_tokens, ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("the wrapper rejects an output token cap above its pinned runtime upper limit", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-output-cap-invalid-"));
  try {
    const result = spawnSync(process.execPath, [
      WRAPPER,
      "--claude-entry", path.join(root, "unused-claude.mjs"),
      "--settings", path.join(root, "unused-settings.json"),
      "--model", "test-model",
      "--usage-root", path.join(root, "usage"),
      "--max-turns", "12",
      "--max-total-tokens", "1000000",
      "--max-output-tokens", "64001",
      "--max-output-tokens-upper-limit", "64000",
      "--max-budget-usd", "10",
      "--hard-timeout-seconds", "30",
      "--workflow", "job",
    ], {
      cwd: root,
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "", HOME: root },
    });
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /WRAPPER_REQUIRED_INPUT_INVALID/);
    assert.equal(fs.existsSync(path.join(root, "usage")), false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
