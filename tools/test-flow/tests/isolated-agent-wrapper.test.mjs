import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT } from "../runtime-support/isolated-agent-env.mjs";
import { SKILL_GENERATION_TRACE_SCHEMA_VERSION } from "../runtime-support/isolated-agent-tool-audit.mjs";

const WRAPPER = path.resolve("tools/test-flow/runtime-support/isolated-agent-wrapper.mjs");

test("a failed model terminal persists complete usage without changing the failure exit", () => {
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
    assert.match(result.stderr, /WRAPPER_MODEL_TERMINAL_INVALID/);
    const files = fs.readdirSync(usageRoot);
    assert.equal(files.length, 1);
    const receipt = JSON.parse(fs.readFileSync(path.join(usageRoot, files[0]), "utf8"));
    assert.deepEqual(receipt.wrapper_outcome, {
      schema_version: 1,
      status: "FAIL",
      code: "WRAPPER_MODEL_TERMINAL_INVALID",
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
    assert.equal(receipt.tool_trace_audit, null);
    assert.equal(Object.hasOwn(receipt.effective_caps, "max_output_tokens"), false);
    assert.equal(Object.hasOwn(receipt.hard_cap_enforcement, "max_output_tokens"), false);
    assert.equal(receipt.environment_policy.claude_process.key_names.includes("CLAUDE_CODE_MAX_OUTPUT_TOKENS"), false);
    assert.doesNotMatch(JSON.stringify(receipt), /wiki|clarification|prompt|content/i);
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
    fs.mkdirSync(path.join(skillRoot, "references"), { recursive: true });
    fs.writeFileSync(path.join(workspace, "inputs", "wiki.md"), "wiki\n");
    fs.writeFileSync(path.join(workspace, "inputs", "clarifications.md"), "clarifications\n");
    fs.writeFileSync(path.join(skillRoot, "SKILL.md"), [
      "[generation](references/generation-spec-v6-reference.md)",
      "[verification](references/verification-contract-v2-reference.md)",
      "",
    ].join("\n"));
    fs.writeFileSync(path.join(skillRoot, "references", "generation-spec-v6-reference.md"), "generation\n");
    fs.writeFileSync(path.join(skillRoot, "references", "verification-contract-v2-reference.md"), "verification\n");
    fs.writeFileSync(settings, "{}\n");
    fs.writeFileSync(fakeClaude, `
const model = process.argv[process.argv.indexOf("--model") + 1];
console.log(JSON.stringify({type:"system",subtype:"init",cwd:process.cwd(),model,permissionMode:"dontAsk",tools:["Read","Skill","Write"]}));
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
      "--max-budget-usd", "10",
      "--hard-timeout-seconds", "30",
      "--workflow", "skill-generation",
      "--skill-root", skillRoot,
      "--source-root", sourceRoot,
    ], {
      cwd: workspace,
      encoding: "utf8",
      env: { PATH: process.env.PATH ?? "", HOME: root },
    });
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /WRAPPER_SKILL_TRACE_INVALID/);
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
