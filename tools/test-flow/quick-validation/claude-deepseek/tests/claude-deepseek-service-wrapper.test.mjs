import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Readable, Writable } from "node:stream";
import test from "node:test";

import { ISOLATED_AGENT_ENV_POLICY_VERSION, environmentKeySummary } from "../../../runtime-support/isolated-agent-env.mjs";
import { validateClaudeDeepseekRoleReceipt } from "../runtime/claude-deepseek-contract.mjs";

import {
  auditRoleWorkspace,
  claimRoleAttempt,
  parseArguments,
  parseMethodsRolePrompt,
  readRoleInvocationReceipts,
  roleToolPolicy,
  runServiceInvocation,
} from "../runtime/claude-deepseek-service-wrapper.mjs";

function prompt(role, attempt) {
  const output = role === "Specialist" ? "output/method-diagnosis.draft.json" : "output/method-review.draft.json";
  return `frozen context\n<<<METHODS_EVIDENCE_V2_ROLE>>>\nRole: ${role}. Attempt: ${attempt}.\nWrite one JSON root array whose items contain only evaluation_ref, verdict, supporting_event_refs, and reason.\nWrite only ${output}.\n<<<END METHODS_EVIDENCE_V2_ROLE>>>\n`;
}

function workspace(root) {
  const inputs = path.join(root, "inputs");
  fs.mkdirSync(path.join(root, "output"), { recursive: true });
  fs.mkdirSync(inputs, { recursive: true });
  for (const name of ["request.json", "method-evidence-graph.json", "method-evaluation-plan.json"]) fs.writeFileSync(path.join(inputs, name), "{}\n");
}

const SUCCESS_PROVIDER_TERMINAL = Object.freeze({
  subtype: "success",
  is_error: false,
  stop_reason: null,
  exit_code: 0,
  signal: null,
});

const TEST_ENVIRONMENT_POLICY = Object.freeze({
  schema_version: 1,
  version: ISOLATED_AGENT_ENV_POLICY_VERSION,
  provider_auth_source: "audited-settings-file",
  inbound: environmentKeySummary({ PATH: "/bin" }),
  claude_process: environmentKeySummary({ PATH: "/bin" }),
});

function successfulProcessReceipt(options, role, costUsd = 0) {
  return {
    schema_version: 1,
    invocation_id: options.invocationId,
    phase: role,
    model: "deepseek-v4-flash[1m]",
    attempt: 1,
    retry: 0,
    status: "PASS",
    terminal: true,
    turns: 1,
    started_at_utc: "2026-08-29T00:00:00.000Z",
    finished_at_utc: "2026-08-29T00:00:01.000Z",
    wall_timeout_seconds: 600,
    max_turns: 50,
    max_budget_usd: options.maxBudgetUsd,
    max_output_tokens: 64_000,
    appended_system_prompt: null,
    environment_policy: TEST_ENVIRONMENT_POLICY,
    provider_terminal: SUCCESS_PROVIDER_TERMINAL,
    usage: { schema_version: 1, input_tokens: 10, output_tokens: 5, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, total_tokens: 15, cost_usd: costUsd },
    tool_count: 2,
    denied_tool_attempt_count: 0,
    disallowed_tools: ["Bash", "Glob", "Grep", "Skill"],
    mcp_call_count: 0,
    bash_call_count: 0,
  };
}

test("model-cert wrapper accepts only its frozen provider inputs", () => {
  const argv = ["--claude-entry", "/cli.js", "--settings", "/settings.json", "--config-root", "/config", "--private-root", "/private", "--evidence-root", "/evidence", "--usage-root", "/usage", "--run-id", "run"];
  assert.equal(parseArguments(argv)["run-id"], "run");
  assert.throws(() => parseArguments([...argv, "--adapter", "/tmp/other"]), (error) => error.code === "CLAUDE_DEEPSEEK_SERVICE_ARGUMENT_UNKNOWN");
});

test("production role marker binds Specialist and Reviewer primary or only repair", () => {
  assert.deepEqual(parseMethodsRolePrompt(prompt("Specialist", "primary evaluation")), { role: "SPECIALIST", attempt: "PRIMARY", output: "output/method-diagnosis.draft.json" });
  assert.deepEqual(parseMethodsRolePrompt(prompt("Reviewer", "only repair")), { role: "REVIEWER", attempt: "REPAIR", output: "output/method-review.draft.json" });
  assert.throws(() => parseMethodsRolePrompt("Candidate diagnosis"), (error) => error.code === "CLAUDE_DEEPSEEK_ROLE_MARKER_INVALID");
});

test("each role receives one primary and at most one repair with a four-call total cap", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-role-claims-"));
  try {
    claimRoleAttempt(root, "SPECIALIST", "PRIMARY");
    assert.throws(() => claimRoleAttempt(root, "SPECIALIST", "PRIMARY"), (error) => error.code === "CLAUDE_DEEPSEEK_ROLE_RETRY_FORBIDDEN");
    claimRoleAttempt(root, "SPECIALIST", "REPAIR");
    assert.throws(() => claimRoleAttempt(root, "SPECIALIST", "REPAIR"), (error) => error.code === "CLAUDE_DEEPSEEK_ROLE_REPAIR_FORBIDDEN");
    claimRoleAttempt(root, "REVIEWER", "PRIMARY");
    claimRoleAttempt(root, "REVIEWER", "REPAIR");
    assert.equal(fs.readdirSync(path.join(root, "model-role-claims")).length, 4);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("role workspace accepts Write then Read of its own draft and rejects other output reads", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-role-workspace-"));
  try {
    workspace(root);
    const output = path.join(root, "output", "method-diagnosis.draft.json");
    const content = '[{"evaluation_ref":"eval-a","verdict":"CONFIRMED","supporting_event_refs":["event-a"],"reason":"ok"}]';
    fs.writeFileSync(output, content);
    const receipt = auditRoleWorkspace({
      workspaceRoot: root,
      roleSpec: { role: "SPECIALIST", attempt: "PRIMARY", output: "output/method-diagnosis.draft.json" },
      processResult: { records: [
        { name: "Read", is_error: false, input: { file_path: path.join(root, "inputs", "method-evaluation-plan.json") } },
        { name: "Write", is_error: false, input: { file_path: output, content } },
        { name: "Read", is_error: false, input: { file_path: output } },
      ] },
    });
    assert.equal(receipt.harness_normalized, false);
    assert.equal(receipt.reads, 2);
    assert.throws(() => auditRoleWorkspace({
      workspaceRoot: root,
      roleSpec: { role: "SPECIALIST", attempt: "PRIMARY", output: "output/method-diagnosis.draft.json" },
      processResult: { records: [
        { name: "Read", is_error: false, input: { file_path: path.join(root, "inputs", "method-evaluation-plan.json") } },
        { name: "Write", is_error: false, input: { file_path: output, content } },
        { name: "Read", is_error: false, input: { file_path: path.join(root, "output", "method-review.draft.json") } },
      ] },
    }), (error) => error.code === "CLAUDE_DEEPSEEK_ROLE_TOOL_SCOPE_INVALID");
    fs.mkdirSync(path.join(root, "inputs", "target-logs"));
    assert.throws(() => auditRoleWorkspace({ workspaceRoot: root, roleSpec: { role: "SPECIALIST", attempt: "PRIMARY", output: "output/method-diagnosis.draft.json" }, processResult: { records: [] } }), (error) => error.code === "CLAUDE_DEEPSEEK_ROLE_INPUT_LEAK");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("role exposes Read/Write tools but grants its Write through the Edit permission category", () => {
  const root = path.resolve("role-workspace");
  const policy = roleToolPolicy({ workspaceRoot: root, output: "output/method-review.draft.json" });
  assert.deepEqual(policy.tools, ["Read", "Write"]);
  assert.equal(policy.allowed_tools.length, 3);
  assert.match(policy.allowed_tools[0], /^Read\(.+\/inputs\/\*\*\)$/u);
  assert.match(policy.allowed_tools[1], /^Read\(.+\/output\/method-review\.draft\.json\)$/u);
  assert.match(policy.allowed_tools[2], /^Edit\(.+\/output\/method-review\.draft\.json\)$/u);
  assert.doesNotMatch(policy.allowed_tools[2], /^Write\(/u);
  assert.equal(policy.readable_scope, "job-workspace-inputs-and-role-draft");
  assert.equal(policy.shell, false);
  assert.equal(policy.network, false);
  assert.equal(policy.writable_scope, "output/method-review.draft.json");
  assert.match(policy.sha256, /^[a-f0-9]{64}$/u);
});

test("wrapper preserves the raw evaluation array and records the exact production prompt", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-role-run-"));
  const previous = process.cwd();
  try {
    workspace(root);
    process.chdir(root);
    const values = {
      "claude-entry": path.join(root, "cli.js"), settings: path.join(root, "settings.json"), "config-root": path.join(root, "config"),
      "private-root": path.join(root, "private"), "evidence-root": path.join(root, "evidence"), "usage-root": path.join(root, "usage"), "run-id": "run",
    };
    for (const target of [values["claude-entry"], values.settings]) fs.writeFileSync(target, "fixture");
    fs.mkdirSync(values["config-root"]);
    const rawPrompt = prompt("Specialist", "primary evaluation");
    const output = path.join(root, "output", "method-diagnosis.draft.json");
    const content = '[{"evaluation_ref":"eval-a","verdict":"CONFIRMED","supporting_event_refs":["event-a"],"reason":"ok"}]';
    const chunks = [];
    const stdout = new Writable({ write(chunk, _encoding, callback) { chunks.push(Buffer.from(chunk)); callback(); } });
    const receipt = await runServiceInvocation(values, {
      stdin: Readable.from([rawPrompt]), stdout,
      runClaude: async (options, hooks) => {
        assert.equal(options.maxBudgetUsd, 2);
        hooks.onProgress();
        fs.writeFileSync(output, content);
        return {
          receipt: successfulProcessReceipt(options, "SPECIALIST"),
          records: [{ name: "Read", is_error: false, input: { file_path: path.join(root, "inputs", "method-evaluation-plan.json") } }, { name: "Write", is_error: false, input: { file_path: output, content } }],
          skills: [], bash: [], mcp: [], denied: [], events: [{ type: "result", result: "done" }],
        };
      },
    });
    assert.equal(receipt.workspace_audit.output_sha256, receipt.workspace_audit.output_sha256);
    assert.deepEqual(receipt.budget, {
      schema_version: 1,
      stage_cap_usd: 4,
      role: "SPECIALIST",
      role_pool_usd: 2,
      prior_cost_usd: 0,
      effective_call_cap_usd: 2,
      enforcement: "claude-cli-threshold+terminal-posthoc-release-cap",
    });
    assert.equal(receipt.max_budget_usd, 2);
    assert.equal(receipt.prompt.utf8_size, Buffer.byteLength(rawPrompt));
    assert.equal(fs.readFileSync(output, "utf8"), content);
    assert.equal(fs.readFileSync(path.join(root, "evidence", "model-role-invocations", "specialist-primary.progress"), "utf8"), ".\n");
    assert.match(Buffer.concat(chunks).toString("utf8"), /done/u);
  } finally { process.chdir(previous); fs.rmSync(root, { recursive: true, force: true }); }
});

test("Specialist and Reviewer each own a two-dollar pool and repairs consume only the role remainder", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-role-budgets-"));
  const previous = process.cwd();
  try {
    const values = {
      "claude-entry": path.join(root, "cli.js"), settings: path.join(root, "settings.json"), "config-root": path.join(root, "config"),
      "private-root": path.join(root, "private"), "evidence-root": path.join(root, "evidence"), "usage-root": path.join(root, "usage"), "run-id": "run",
    };
    for (const target of [values["claude-entry"], values.settings]) fs.writeFileSync(target, "fixture");
    fs.mkdirSync(values["config-root"]);
    const observedCaps = [];
    const invoke = async (label, attemptText, key, costUsd) => {
      const role = label === "Specialist" ? "SPECIALIST" : "REVIEWER";
      const work = path.join(root, "workspaces", key);
      workspace(work);
      process.chdir(work);
      const output = path.join(work, "output", role === "SPECIALIST" ? "method-diagnosis.draft.json" : "method-review.draft.json");
      const content = '[{"evaluation_ref":"eval-a","verdict":"CONFIRMED","supporting_event_refs":["event-a"],"reason":"ok"}]';
      return runServiceInvocation(values, {
        stdin: Readable.from([prompt(label, attemptText)]),
        stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
        runClaude: async (options) => {
          observedCaps.push({ key, cap: options.maxBudgetUsd });
          fs.writeFileSync(output, content);
          return {
            receipt: successfulProcessReceipt(options, role, costUsd),
            records: [
              { name: "Read", is_error: false, input: { file_path: path.join(work, "inputs", "method-evaluation-plan.json") } },
              { name: "Write", is_error: false, input: { file_path: output, content } },
              ...(role === "REVIEWER"
                ? [{ name: "Read", is_error: false, input: { file_path: output } }]
                : []),
            ],
            skills: [], bash: [], mcp: [], denied: [], events: [{ type: "result", result: "done" }],
          };
        },
      });
    };
    const specialistPrimary = await invoke("Specialist", "primary evaluation", "specialist-primary", 1.25);
    const specialistRepair = await invoke("Specialist", "only repair", "specialist-repair", 0.5);
    const reviewerPrimary = await invoke("Reviewer", "primary evaluation", "reviewer-primary", 0.75);
    const reviewerRepair = await invoke("Reviewer", "only repair", "reviewer-repair", 1);
    assert.deepEqual(observedCaps, [
      { key: "specialist-primary", cap: 2 },
      { key: "specialist-repair", cap: 0.75 },
      { key: "reviewer-primary", cap: 2 },
      { key: "reviewer-repair", cap: 1.25 },
    ]);
    assert.equal(specialistPrimary.budget.prior_cost_usd, 0);
    assert.equal(specialistRepair.budget.prior_cost_usd, 1.25);
    assert.equal(reviewerPrimary.budget.prior_cost_usd, 0);
    assert.equal(reviewerRepair.budget.prior_cost_usd, 0.75);
    assert.equal(specialistPrimary.usage.cost_usd + specialistRepair.budget.effective_call_cap_usd, 2);
    assert.equal(reviewerPrimary.usage.cost_usd + reviewerRepair.budget.effective_call_cap_usd, 2);
    assert.equal(readRoleInvocationReceipts(values["usage-root"]).reduce((sum, item) => sum + item.usage.cost_usd, 0), 3.5);
  } finally { process.chdir(previous); fs.rmSync(root, { recursive: true, force: true }); }
});

test("a repair with no role-pool balance closes before invoking the provider", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-role-budget-empty-"));
  const previous = process.cwd();
  try {
    const values = {
      "claude-entry": path.join(root, "cli.js"), settings: path.join(root, "settings.json"), "config-root": path.join(root, "config"),
      "private-root": path.join(root, "private"), "evidence-root": path.join(root, "evidence"), "usage-root": path.join(root, "usage"), "run-id": "run",
    };
    for (const target of [values["claude-entry"], values.settings]) fs.writeFileSync(target, "fixture");
    fs.mkdirSync(values["config-root"]);
    const primaryWork = path.join(root, "primary");
    workspace(primaryWork);
    process.chdir(primaryWork);
    const output = path.join(primaryWork, "output", "method-diagnosis.draft.json");
    const content = '[{"evaluation_ref":"eval-a","verdict":"UNKNOWN","supporting_event_refs":[],"reason":"unknown"}]';
    await runServiceInvocation(values, {
      stdin: Readable.from([prompt("Specialist", "primary evaluation")]),
      stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
      runClaude: async (options) => {
        fs.writeFileSync(output, content);
        return {
          receipt: successfulProcessReceipt(options, "SPECIALIST", 2),
          records: [
            { name: "Read", is_error: false, input: { file_path: path.join(primaryWork, "inputs", "method-evaluation-plan.json") } },
            { name: "Write", is_error: false, input: { file_path: output, content } },
          ],
          skills: [], bash: [], mcp: [], denied: [], events: [{ type: "result", result: "done" }],
        };
      },
    });
    const repairWork = path.join(root, "repair");
    workspace(repairWork);
    process.chdir(repairWork);
    let providerCalled = false;
    await assert.rejects(runServiceInvocation(values, {
      stdin: Readable.from([prompt("Specialist", "only repair")]),
      stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
      runClaude: async () => { providerCalled = true; },
    }), { code: "CLAUDE_DEEPSEEK_ROLE_BUDGET_EXHAUSTED" });
    assert.equal(providerCalled, false);
    const receipts = readRoleInvocationReceipts(values["usage-root"]);
    assert.equal(receipts.length, 2);
    assert.equal(receipts[1].status, "FAIL");
    assert.equal(receipts[1].failure_code, "CLAUDE_DEEPSEEK_ROLE_BUDGET_EXHAUSTED");
    assert.equal(receipts[1].budget.prior_cost_usd, 2);
    assert.equal(receipts[1].budget.effective_call_cap_usd, 0);
    assert.equal(receipts[1].usage_complete, false);
  } finally { process.chdir(previous); fs.rmSync(root, { recursive: true, force: true }); }
});

test("a successful CLI terminal that slightly exceeds its threshold becomes one closed failed call", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-role-posthoc-overage-"));
  const previous = process.cwd();
  try {
    workspace(root);
    process.chdir(root);
    const values = {
      "claude-entry": path.join(root, "cli.js"), settings: path.join(root, "settings.json"), "config-root": path.join(root, "config"),
      "private-root": path.join(root, "private"), "evidence-root": path.join(root, "evidence"), "usage-root": path.join(root, "usage"), "run-id": "run",
    };
    fs.writeFileSync(values.settings, "{}\n");
    fs.writeFileSync(values["claude-entry"], `
process.stdin.resume();
process.stdin.on("end", () => {
  process.stdout.write(JSON.stringify({type:"system",subtype:"init",model:"deepseek-v4-flash[1m]",cwd:process.cwd(),permissionMode:"dontAsk",tools:["Read","Write"]})+"\\n");
  process.stdout.write(JSON.stringify({
    type:"result",subtype:"success",is_error:false,num_turns:1,stop_reason:"end_turn",total_cost_usd:2.000001,
    usage:{input_tokens:0,output_tokens:0,cache_creation_input_tokens:0,cache_read_input_tokens:0},
    modelUsage:{"deepseek-v4-flash[1m]":{inputTokens:150,outputTokens:50,cacheReadInputTokens:0,cacheCreationInputTokens:0,costUSD:2.000001}}
  })+"\\n");
});
`);
    fs.mkdirSync(values["config-root"]);
    await assert.rejects(runServiceInvocation(values, {
      stdin: Readable.from([prompt("Specialist", "primary evaluation")]),
      stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
    }), { code: "CLAUDE_DEEPSEEK_CALL_BUDGET_EXCEEDED" });
    const [receipt] = readRoleInvocationReceipts(values["usage-root"]);
    assert.equal(receipt.status, "FAIL");
    assert.equal(receipt.failure_code, "CLAUDE_DEEPSEEK_CALL_BUDGET_EXCEEDED");
    assert.equal(receipt.usage.cost_usd, 2.000001);
    assert.equal(receipt.usage.total_tokens, 200);
    assert.equal(receipt.usage_complete, true);
    assert.equal(receipt.budget.effective_call_cap_usd, 2);
    assert.deepEqual(receipt.provider_terminal, { ...SUCCESS_PROVIDER_TERMINAL, stop_reason: "end_turn" });
    assert.equal(fs.existsSync(path.join(root, "evidence", "model-cert.json")), false);
  } finally { process.chdir(previous); fs.rmSync(root, { recursive: true, force: true }); }
});

test("a successful CLI terminal with two non-zero token sources in conflict closes as usage-invalid", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-role-success-usage-conflict-"));
  const previous = process.cwd();
  try {
    workspace(root);
    process.chdir(root);
    const values = {
      "claude-entry": path.join(root, "cli.js"), settings: path.join(root, "settings.json"), "config-root": path.join(root, "config"),
      "private-root": path.join(root, "private"), "evidence-root": path.join(root, "evidence"), "usage-root": path.join(root, "usage"), "run-id": "run",
    };
    fs.writeFileSync(values.settings, "{}\n");
    fs.writeFileSync(values["claude-entry"], `
process.stdin.resume();
process.stdin.on("end", () => {
  process.stdout.write(JSON.stringify({type:"system",subtype:"init",model:"deepseek-v4-flash[1m]",cwd:process.cwd(),permissionMode:"dontAsk",tools:["Read","Write"]})+"\\n");
  process.stdout.write(JSON.stringify({
    type:"result",subtype:"success",is_error:false,num_turns:1,stop_reason:"end_turn",total_cost_usd:1,
    usage:{input_tokens:10,output_tokens:5,cache_creation_input_tokens:0,cache_read_input_tokens:0},
    modelUsage:{"deepseek-v4-flash[1m]":{inputTokens:11,outputTokens:5,cacheReadInputTokens:0,cacheCreationInputTokens:0,costUSD:1}}
  })+"\\n");
});
`);
    fs.mkdirSync(values["config-root"]);
    await assert.rejects(runServiceInvocation(values, {
      stdin: Readable.from([prompt("Specialist", "primary evaluation")]),
      stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
    }), { code: "CLAUDE_DEEPSEEK_TERMINAL_USAGE_INVALID" });
    const [receipt] = readRoleInvocationReceipts(values["usage-root"]);
    assert.equal(receipt.status, "FAIL");
    assert.equal(receipt.failure_code, "CLAUDE_DEEPSEEK_TERMINAL_USAGE_INVALID");
    assert.equal(receipt.usage, null);
    assert.equal(receipt.usage_complete, false);
    assert.deepEqual(receipt.provider_terminal, { ...SUCCESS_PROVIDER_TERMINAL, stop_reason: "end_turn" });
  } finally { process.chdir(previous); fs.rmSync(root, { recursive: true, force: true }); }
});

test("an existing progress file after claim still produces one closed failed receipt", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-role-progress-conflict-"));
  const previous = process.cwd();
  try {
    workspace(root);
    process.chdir(root);
    const values = {
      "claude-entry": path.join(root, "cli.js"), settings: path.join(root, "settings.json"), "config-root": path.join(root, "config"),
      "private-root": path.join(root, "private"), "evidence-root": path.join(root, "evidence"), "usage-root": path.join(root, "usage"), "run-id": "run",
    };
    for (const target of [values["claude-entry"], values.settings]) fs.writeFileSync(target, "fixture");
    fs.mkdirSync(values["config-root"]);
    const traceRoot = path.join(values["evidence-root"], "model-role-invocations");
    fs.mkdirSync(traceRoot, { recursive: true });
    fs.writeFileSync(path.join(traceRoot, "specialist-primary.progress"), "existing\n");
    let providerCalled = false;
    await assert.rejects(runServiceInvocation(values, {
      stdin: Readable.from([prompt("Specialist", "primary evaluation")]),
      stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
      runClaude: async () => { providerCalled = true; },
    }), { code: "CLAUDE_DEEPSEEK_ROLE_PROGRESS_EXISTS" });
    assert.equal(providerCalled, false);
    const [receipt] = readRoleInvocationReceipts(values["usage-root"]);
    assert.equal(receipt.status, "FAIL");
    assert.equal(receipt.failure_code, "CLAUDE_DEEPSEEK_ROLE_PROGRESS_EXISTS");
    assert.equal(receipt.usage, null);
    assert.equal(receipt.provider_terminal, null);
    assert.equal(receipt.budget.effective_call_cap_usd, 2);
    assert.equal(fs.existsSync(path.join(values["private-root"], "model-role-claims", "specialist-primary")), true);
    assert.deepEqual(receipt, JSON.parse(fs.readFileSync(path.join(traceRoot, "specialist-primary.receipt.json"), "utf8")));
  } finally { process.chdir(previous); fs.rmSync(root, { recursive: true, force: true }); }
});

test("failed provider invocation writes one closed role receipt with terminal usage", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-role-failure-"));
  const previous = process.cwd();
  try {
    workspace(root);
    process.chdir(root);
    const values = {
      "claude-entry": path.join(root, "cli.js"), settings: path.join(root, "settings.json"), "config-root": path.join(root, "config"),
      "private-root": path.join(root, "private"), "evidence-root": path.join(root, "evidence"), "usage-root": path.join(root, "usage"), "run-id": "run",
    };
    for (const target of [values["claude-entry"], values.settings]) fs.writeFileSync(target, "fixture");
    fs.mkdirSync(values["config-root"]);
    const error = new Error("provider failed");
    error.code = "CLAUDE_DEEPSEEK_PROCESS_FAILED";
    error.details = {
      exit_code: 7,
      signal: null,
      terminal: {
        subtype: "error",
        is_error: true,
        stop_reason: "end_turn",
        turns: 1,
        usage: { schema_version: 1, input_tokens: 11, output_tokens: 3, cache_creation_input_tokens: 0, cache_read_input_tokens: 2, total_tokens: 16, cost_usd: 0.01 },
      },
    };
    await assert.rejects(runServiceInvocation(values, {
      stdin: Readable.from([prompt("Specialist", "primary evaluation")]),
      stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
      runClaude: async () => { throw error; },
    }), { code: "CLAUDE_DEEPSEEK_PROCESS_FAILED" });
    const [receipt] = readRoleInvocationReceipts(values["usage-root"]);
    assert.equal(receipt.status, "FAIL");
    assert.equal(receipt.workflow, "SPECIALIST:PRIMARY");
    assert.equal(receipt.role, "SPECIALIST");
    assert.equal(receipt.evaluation_attempt, "PRIMARY");
    assert.equal(receipt.failure_code, "CLAUDE_DEEPSEEK_PROCESS_FAILED");
    assert.equal(receipt.usage_complete, true);
    assert.equal(receipt.usage.total_tokens, 16);
    assert.deepEqual(receipt.provider_terminal, {
      subtype: "error",
      is_error: true,
      stop_reason: "end_turn",
      exit_code: 7,
      signal: null,
    });
    assert.equal(receipt.wall_timeout_seconds, 600);
    assert.ok(Date.parse(receipt.finished_at_utc) >= Date.parse(receipt.started_at_utc));
    assert.deepEqual(receipt, JSON.parse(fs.readFileSync(path.join(values["evidence-root"], "model-role-invocations", "specialist-primary.receipt.json"), "utf8")));
  } finally { process.chdir(previous); fs.rmSync(root, { recursive: true, force: true }); }
});

test("a denied tool result preserves the successful provider terminal in the failed role receipt", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-role-denied-tool-terminal-"));
  const previous = process.cwd();
  try {
    workspace(root);
    process.chdir(root);
    const values = {
      "claude-entry": path.join(root, "cli.js"), settings: path.join(root, "settings.json"), "config-root": path.join(root, "config"),
      "private-root": path.join(root, "private"), "evidence-root": path.join(root, "evidence"), "usage-root": path.join(root, "usage"), "run-id": "run",
    };
    fs.writeFileSync(values.settings, "{}\n");
    fs.writeFileSync(values["claude-entry"], `
process.stdin.resume();
process.stdin.on("end", () => {
  const output = require("node:path").join(process.cwd(), "output", "method-diagnosis.draft.json");
  const events = [
    {type:"system",subtype:"init",model:"deepseek-v4-flash[1m]",cwd:process.cwd(),permissionMode:"dontAsk",tools:["Read","Write"]},
    {type:"assistant",message:{role:"assistant",content:[{type:"tool_use",id:"write",name:"Write",input:{file_path:output,content:"[]"}}]}},
    {type:"user",message:{role:"user",content:[{type:"tool_result",tool_use_id:"write",is_error:true,content:"permission denied"}]},tool_use_result:{error:"permission denied"}},
    {
      type:"result",subtype:"success",is_error:false,num_turns:2,stop_reason:"end_turn",total_cost_usd:0.387648,
      usage:{input_tokens:32930,output_tokens:8390,cache_creation_input_tokens:0,cache_read_input_tokens:26496},
      modelUsage:{"deepseek-v4-flash[1m]":{inputTokens:32930,outputTokens:8390,cacheCreationInputTokens:0,cacheReadInputTokens:26496,costUSD:0.387648}}
    },
  ];
  for (const event of events) process.stdout.write(JSON.stringify(event)+"\\n");
});
`);
    fs.mkdirSync(values["config-root"]);
    await assert.rejects(runServiceInvocation(values, {
      stdin: Readable.from([prompt("Specialist", "primary evaluation")]),
      stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
    }), { code: "CLAUDE_DEEPSEEK_TOOL_RESULT_ERROR" });
    const [receipt] = readRoleInvocationReceipts(values["usage-root"]);
    assert.equal(receipt.status, "FAIL");
    assert.equal(receipt.failure_code, "CLAUDE_DEEPSEEK_TOOL_RESULT_ERROR");
    assert.equal(receipt.turns, 2);
    assert.equal(receipt.usage_complete, true);
    assert.deepEqual(receipt.usage, {
      schema_version: 1,
      input_tokens: 32930,
      output_tokens: 8390,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 26496,
      total_tokens: 67816,
      cost_usd: 0.387648,
    });
    assert.deepEqual(receipt.provider_terminal, {
      subtype: "success",
      is_error: false,
      stop_reason: "end_turn",
      exit_code: 0,
      signal: null,
    });
    assert.equal(validateClaudeDeepseekRoleReceipt(receipt, {
      expectedRole: "SPECIALIST",
      expectedAttempt: "PRIMARY",
      priorCostUsd: 0,
    }), receipt);
  } finally { process.chdir(previous); fs.rmSync(root, { recursive: true, force: true }); }
});

test("budget terminal details survive in the closed role receipt", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deepseek-role-budget-terminal-"));
  const previous = process.cwd();
  try {
    workspace(root);
    process.chdir(root);
    const values = {
      "claude-entry": path.join(root, "cli.js"), settings: path.join(root, "settings.json"), "config-root": path.join(root, "config"),
      "private-root": path.join(root, "private"), "evidence-root": path.join(root, "evidence"), "usage-root": path.join(root, "usage"), "run-id": "run",
    };
    for (const target of [values["claude-entry"], values.settings]) fs.writeFileSync(target, "fixture");
    fs.mkdirSync(values["config-root"]);
    const error = new Error("budget exhausted");
    error.code = "CLAUDE_DEEPSEEK_MAX_BUDGET_EXCEEDED";
    error.details = {
      exit_code: 1,
      signal: null,
      terminal: {
        subtype: "error_max_budget_usd",
        is_error: true,
        stop_reason: "tool_use",
        turns: 1,
        usage: { schema_version: 1, input_tokens: 23302, output_tokens: 37245, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, total_tokens: 60547, cost_usd: 1.047635 },
      },
    };
    await assert.rejects(runServiceInvocation(values, {
      stdin: Readable.from([prompt("Specialist", "primary evaluation")]),
      stdout: new Writable({ write(_chunk, _encoding, callback) { callback(); } }),
      runClaude: async () => { throw error; },
    }), { code: "CLAUDE_DEEPSEEK_MAX_BUDGET_EXCEEDED" });
    const [receipt] = readRoleInvocationReceipts(values["usage-root"]);
    assert.equal(receipt.failure_code, "CLAUDE_DEEPSEEK_MAX_BUDGET_EXCEEDED");
    assert.equal(receipt.usage_complete, true);
    assert.equal(receipt.usage.total_tokens, 60547);
    assert.deepEqual(receipt.provider_terminal, {
      subtype: "error_max_budget_usd",
      is_error: true,
      stop_reason: "tool_use",
      exit_code: 1,
      signal: null,
    });
    assert.equal(receipt.budget.effective_call_cap_usd, 2);
  } finally { process.chdir(previous); fs.rmSync(root, { recursive: true, force: true }); }
});
