import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  controlledClaudeEnvironment,
  projectClaudeTools,
  runClaudeProcess,
} from "../runtime/claude-deepseek-process.mjs";

test("controlled Claude environment drops ambient provider, proxy, and hook state", () => {
  const environment = controlledClaudeEnvironment({
    LANG: "zh_CN.UTF-8",
    ANTHROPIC_AUTH_TOKEN: "ambient-secret",
    ANTHROPIC_BASE_URL: "https://wrong.example",
    HTTP_PROXY: "http://proxy.example",
    CLAUDE_CODE_HOOKS: "bad",
  }, {
    configRoot: "/private/tmp/config",
    home: "/private/tmp/home",
    temporary: "/private/tmp/tmp",
  });
  assert.equal(environment.ANTHROPIC_AUTH_TOKEN, undefined);
  assert.equal(environment.ANTHROPIC_BASE_URL, undefined);
  assert.equal(environment.HTTP_PROXY, undefined);
  assert.equal(environment.CLAUDE_CODE_HOOKS, undefined);
  assert.equal(environment.CLAUDE_CODE_MAX_OUTPUT_TOKENS, "64000");
  assert.equal(environment.CLAUDE_CONFIG_DIR, "/private/tmp/config");
});

test("controlled Claude environment adds only the explicit Logparse broker directory", () => {
  const environment = controlledClaudeEnvironment({}, {
    configRoot: "/private/tmp/config",
    home: "/private/tmp/home",
    temporary: "/private/tmp/tmp",
    brokerExecutableDirectory: "/private/tmp/contract-bin",
  });
  assert.equal(environment.PATH, ["/private/tmp/contract-bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"].join(path.delimiter));
  assert.throws(
    () => controlledClaudeEnvironment({}, {
      configRoot: "/private/tmp/config",
      home: "/private/tmp/home",
      temporary: "/private/tmp/tmp",
      brokerExecutableDirectory: "relative-bin",
    }),
    /broker executable directory/,
  );
});

test("tool projection separates Skill, MCP and Bash without parsing narrative text", () => {
  const events = [
    { type: "assistant", message: { content: [
      { type: "tool_use", id: "skill", name: "Skill", input: { skill: "problem-locator-client" } },
      { type: "tool_use", id: "mcp", name: "mcp__problem-locator__problem_locator_get_case", input: { case_id: "case", wait_seconds: 0 } },
      { type: "tool_use", id: "bash", name: "Bash", input: { command: "/usr/bin/stat -f %z /tmp/logs.zip" } },
    ] } },
    { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "skill", is_error: false, content: "loaded" }] }, tool_use_result: { loaded: true } },
    { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "mcp", is_error: false, content: "ok" }] }, tool_use_result: { structuredContent: { ok: true, data: { case_id: "case" }, error: null } } },
    { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "bash", is_error: false, content: "42" }] }, tool_use_result: { stdout: "42\n", stderr: "", exitCode: 0 } },
  ];
  const projected = projectClaudeTools(events);
  assert.deepEqual(projected.skills, [{ ordinal: 0, skill: "problem-locator-client" }]);
  assert.equal(projected.mcp[0].tool, "problem_locator_get_case");
  assert.equal(projected.bash[0].exit_code, 0);
  assert.deepEqual(projected.denied, []);
});

test("tool projection records permission-denied attempts without counting them as executions", () => {
  const events = [
    { type: "assistant", message: { content: [
      { type: "tool_use", id: "curl", name: "Bash", input: { command: "curl --request PUT https://denied.invalid" } },
      { type: "tool_use", id: "glob", name: "Glob", input: { pattern: "**/*" } },
      { type: "tool_use", id: "stat", name: "Bash", input: { command: "/usr/bin/stat -f %z /tmp/logs.zip" } },
    ] } },
    { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "curl", is_error: true, content: "permission denied" }] }, tool_use_result: { error: "permission denied" } },
    { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "glob", is_error: true, content: "permission denied" }] }, tool_use_result: { error: "permission denied" } },
    { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "stat", is_error: false, content: "42" }] }, tool_use_result: { stdout: "42\n", stderr: "", exitCode: 0 } },
  ];
  const projected = projectClaudeTools(events, { allowToolErrors: true });
  assert.equal(projected.bash.length, 1);
  assert.equal(projected.bash[0].command, "/usr/bin/stat -f %z /tmp/logs.zip");
  assert.deepEqual(projected.denied.map(({ name, program, executed }) => ({ name, program, executed })), [
    { name: "Bash", program: "curl", executed: false },
    { name: "Glob", program: null, executed: false },
  ]);
  assert.ok(projected.denied.every((item) => /^[0-9a-f]{64}$/u.test(item.input_sha256)));
});

test("process wrapper emits one audited terminal receipt with no retry", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-deepseek-process-"));
  const fake = path.join(root, "fake-cli.js");
  const settings = path.join(root, "settings.json");
  const cwd = path.join(root, "workspace");
  const configRoot = path.join(root, "config");
  const home = path.join(root, "home");
  const temporary = path.join(root, "tmp");
  for (const directory of [cwd, configRoot, home, temporary]) fs.mkdirSync(directory);
  fs.writeFileSync(settings, "{}\n");
  fs.writeFileSync(fake, `
const systemPromptIndex = process.argv.indexOf("--append-system-prompt");
if (systemPromptIndex < 0 || process.argv[systemPromptIndex + 1] !== "fixed system rule") process.exit(9);
process.stdin.resume();
process.stdin.on("end", () => {
  const events = [
    {type:"system",subtype:"init",model:"deepseek-v4-flash[1m]",cwd:process.cwd(),permissionMode:"dontAsk",tools:["Read"]},
    {type:"assistant",message:{role:"assistant",content:[{type:"tool_use",id:"read",name:"Read",input:{file_path:"inputs/wiki.md"}}]}},
    {type:"user",message:{role:"user",content:[{type:"tool_result",tool_use_id:"read",is_error:false,content:"ok"}]},tool_use_result:{content:"ok"}},
    {type:"result",subtype:"success",is_error:false,num_turns:2,usage:{input_tokens:10,output_tokens:5,cache_creation_input_tokens:3,cache_read_input_tokens:2},total_cost_usd:0.01},
  ];
  for (const event of events) process.stdout.write(JSON.stringify(event)+"\\n");
});
`);
  const tracePath = path.join(root, "trace.ndjson");
  const receiptPath = path.join(root, "receipt.json");
  const result = await runClaudeProcess({
    claudeEntry: fake,
    settings,
    cwd,
    prompt: "read",
    appendSystemPrompt: "fixed system rule",
    phase: "METHODS_BOOTSTRAP",
    invocationId: "run:methods",
    tools: ["Read"],
    allowedTools: [],
    maxTurns: 16,
    maxBudgetUsd: 10,
    wallTimeoutSeconds: 30,
    noProgressSeconds: 5,
    tracePath,
    receiptPath,
    environment: { configRoot, home, temporary },
  }, { ambient: {} });
  assert.equal(result.receipt.status, "PASS");
  assert.equal(result.receipt.retry, 0);
  assert.equal(result.receipt.usage.total_tokens, 20);
  assert.equal(result.receipt.appended_system_prompt.utf8_size, 17);
  assert.match(result.receipt.appended_system_prompt.sha256, /^[a-f0-9]{64}$/u);
  assert.equal(fs.readFileSync(tracePath, "utf8").trim().split("\n").length, 4);
  assert.equal(JSON.parse(fs.readFileSync(receiptPath, "utf8")).phase, "METHODS_BOOTSTRAP");
});

test("failed process exposes provider terminal usage to the role wrapper", async (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-deepseek-process-failure-"));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const fake = path.join(root, "fake-cli.js");
  const settings = path.join(root, "settings.json");
  const cwd = path.join(root, "workspace");
  const configRoot = path.join(root, "config");
  const home = path.join(root, "home");
  const temporary = path.join(root, "tmp");
  for (const directory of [cwd, configRoot, home, temporary]) fs.mkdirSync(directory);
  fs.writeFileSync(settings, "{}\n");
  fs.writeFileSync(fake, `
process.stdin.resume();
process.stdin.on("end", () => {
  process.stdout.write(JSON.stringify({type:"system",subtype:"init",model:"deepseek-v4-flash[1m]",cwd:process.cwd(),permissionMode:"dontAsk",tools:["Read"]})+"\\n");
  process.stdout.write(JSON.stringify({type:"result",subtype:"error",is_error:true,num_turns:1,usage:{input_tokens:11,output_tokens:4,cache_creation_input_tokens:2,cache_read_input_tokens:1},total_cost_usd:0.02})+"\\n");
  process.exitCode = 7;
});
`);
  await assert.rejects(
    runClaudeProcess({
      claudeEntry: fake,
      settings,
      cwd,
      prompt: "read",
      phase: "SPECIALIST",
      invocationId: "run:specialist-primary",
      tools: ["Read"],
      allowedTools: [],
      maxTurns: 10,
      maxBudgetUsd: 1,
      wallTimeoutSeconds: 30,
      noProgressSeconds: 5,
      tracePath: path.join(root, "trace.ndjson"),
      environment: { configRoot, home, temporary },
    }, { ambient: {} }),
    (error) => {
      assert.equal(error.code, "CLAUDE_DEEPSEEK_PROCESS_FAILED");
      assert.equal(error.details.terminal.terminal, true);
      assert.equal(error.details.terminal.is_error, true);
      assert.equal(error.details.terminal.turns, 1);
      assert.deepEqual(error.details.terminal.usage, {
        schema_version: 1,
        input_tokens: 11,
        output_tokens: 4,
        cache_creation_input_tokens: 2,
        cache_read_input_tokens: 1,
        total_tokens: 18,
        cost_usd: 0.02,
      });
      return true;
    },
  );
});
