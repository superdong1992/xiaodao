#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { normalizeUsage, TOKEN_USAGE_FORMULA } from "../lib/usage.mjs";
import {
  auditSkillGenerationTrace,
  discoverLinkedSkillReferences,
  SKILL_GENERATION_TRACE_SCHEMA_VERSION,
  skillGenerationPermissionRules,
} from "./isolated-agent-tool-audit.mjs";
import {
  assertIsolatedAgentInboundEnvironment,
  buildIsolatedAgentEnvironment,
  environmentKeySummary,
  explicitEnvironmentFrom,
  ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY,
  ISOLATED_AGENT_ENV_POLICY_VERSION,
  ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT,
} from "./isolated-agent-env.mjs";

function argumentsMap(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    if (!argv[index]?.startsWith("--") || index + 1 >= argv.length) throw new Error("WRAPPER_ARGUMENT_INVALID");
    values[argv[index].slice(2).replaceAll("-", "_")] = argv[index + 1];
  }
  return values;
}

function writeNew(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, `${JSON.stringify(value)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
}

const values = argumentsMap(process.argv.slice(2));
const inboundEnvironment = assertIsolatedAgentInboundEnvironment(process.env, { allowSessionCredentials: true });
const hasMaxOutputTokens = values.max_output_tokens !== undefined;
const hasMaxOutputTokensUpperLimit = values.max_output_tokens_upper_limit !== undefined;
const maxOutputTokensUpperLimit = hasMaxOutputTokens ? Number(values.max_output_tokens_upper_limit) : null;
const caps = {
  max_turns: Number(values.max_turns),
  max_total_tokens: Number(values.max_total_tokens),
  ...(hasMaxOutputTokens ? { max_output_tokens: Number(values.max_output_tokens) } : {}),
  max_budget_usd: Number(values.max_budget_usd),
  hard_timeout_seconds: Number(values.hard_timeout_seconds),
};
const workflow = values.workflow ?? "job";
const outputCapValid = hasMaxOutputTokens === hasMaxOutputTokensUpperLimit
  && (!hasMaxOutputTokens || (
    Number.isSafeInteger(caps.max_output_tokens)
    && caps.max_output_tokens > 0
    && Number.isSafeInteger(maxOutputTokensUpperLimit)
    && maxOutputTokensUpperLimit > 0
    && caps.max_output_tokens <= maxOutputTokensUpperLimit
    && caps.max_output_tokens <= caps.max_total_tokens
  ));
if (!values.claude_entry || !values.settings || !values.model || !values.usage_root || !["job", "skill-generation"].includes(workflow) || (workflow === "skill-generation" && (!values.skill_root || !values.source_root)) || !Number.isSafeInteger(caps.max_turns) || caps.max_turns <= 0 || !Number.isSafeInteger(caps.max_total_tokens) || caps.max_total_tokens <= 0 || !outputCapValid || !Number.isFinite(caps.max_budget_usd) || caps.max_budget_usd <= 0 || !Number.isSafeInteger(caps.hard_timeout_seconds) || caps.hard_timeout_seconds <= 0) {
  throw new Error("WRAPPER_REQUIRED_INPUT_INVALID");
}

let linkedReferences = [];
let toolArguments;
if (workflow === "skill-generation") {
  linkedReferences = discoverLinkedSkillReferences(values.skill_root);
  const permissionRules = skillGenerationPermissionRules({
    workspaceRoot: process.cwd(),
    skillRoot: values.skill_root,
    linkedReferences,
    sourceRoot: values.source_root,
  });
  toolArguments = [
    "--tools", "Read,Write,Skill",
    "--allowedTools", ...permissionRules,
    "--permission-mode", "dontAsk",
  ];
} else {
  toolArguments = ["--tools", "Read,Write", "--dangerously-skip-permissions"];
}

const claudeEnvironment = buildIsolatedAgentEnvironment({
  ambient: process.env,
  explicit: {
    ...explicitEnvironmentFrom(process.env, [
      "CLAUDE_CONFIG_DIR",
      "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    ]),
    ...(hasMaxOutputTokens ? { [ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY]: String(caps.max_output_tokens) } : {}),
  },
  allowSessionCredentials: true,
  allowClaudeChildControls: true,
});

const child = spawn(process.execPath, [
  values.claude_entry,
  "-p",
  "--output-format", "stream-json",
  "--verbose",
  "--no-session-persistence",
  "--setting-sources", "user",
  "--settings", values.settings,
  "--model", values.model,
  "--max-turns", String(caps.max_turns),
  "--max-budget-usd", String(caps.max_budget_usd),
  ...toolArguments,
], { cwd: process.cwd(), env: claudeEnvironment, stdio: ["pipe", "pipe", "pipe"] });

process.stdin.pipe(child.stdin);
const stdout = [];
child.stdout.on("data", (chunk) => { stdout.push(chunk); process.stdout.write(chunk); });
child.stderr.on("data", (chunk) => process.stderr.write(chunk));
let timedOut = false;
const timeout = setTimeout(() => {
  timedOut = true;
  child.kill("SIGTERM");
  setTimeout(() => child.exitCode === null && child.kill("SIGKILL"), 5000).unref();
}, caps.hard_timeout_seconds * 1000);
timeout.unref();

const exit = await new Promise((resolve, reject) => {
  child.once("error", reject);
  child.once("exit", (code, signal) => resolve({ code, signal }));
});
clearTimeout(timeout);
const events = Buffer.concat(stdout).toString("utf8").split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
const init = events.filter((event) => event.type === "system" && event.subtype === "init");
const terminal = events.filter((event) => event.type === "result");
if (init.length !== 1 || terminal.length !== 1 || events.at(-1)?.type !== "result" || init[0].model !== values.model) throw new Error("WRAPPER_MODEL_STREAM_INVALID");
const final = terminal[0];
let usage;
try {
  usage = normalizeUsage({
    input_tokens: Number(final.usage?.input_tokens ?? -1),
    output_tokens: Number(final.usage?.output_tokens ?? -1),
    cache_creation_input_tokens: Number(final.usage?.cache_creation_input_tokens ?? -1),
    cache_read_input_tokens: Number(final.usage?.cache_read_input_tokens ?? -1),
    cost_usd: Number(final.total_cost_usd ?? final.cost_usd ?? -1),
  });
} catch {
  throw new Error("WRAPPER_MODEL_USAGE_INVALID");
}
const terminalSucceeded = final.subtype === "success" && final.is_error === false;
const turnsValid = Number.isSafeInteger(final.num_turns) && final.num_turns > 0 && final.num_turns <= caps.max_turns;
let wrapperFailureCode = null;
if (timedOut) wrapperFailureCode = "WRAPPER_MODEL_TIMEOUT";
else if (!terminalSucceeded || !turnsValid) wrapperFailureCode = "WRAPPER_MODEL_TERMINAL_INVALID";
else if (usage.total_tokens > caps.max_total_tokens || usage.cost_usd > caps.max_budget_usd) wrapperFailureCode = "WRAPPER_MODEL_CAP_EXCEEDED";
else if (exit.code !== 0 || exit.signal !== null) wrapperFailureCode = "WRAPPER_CHILD_PROCESS_FAILED";
let toolTraceAudit = null;
if (workflow === "skill-generation" && terminalSucceeded) {
  try {
    toolTraceAudit = auditSkillGenerationTrace({
      events,
      workspaceRoot: process.cwd(),
      skillRoot: values.skill_root,
      sourceRoot: values.source_root,
    });
  } catch (error) {
    wrapperFailureCode ??= "WRAPPER_SKILL_TRACE_INVALID";
    toolTraceAudit = {
      schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
      status: "FAIL",
      workflow: "skill-generation",
      code: typeof error?.code === "string" && /^SKILL_TRACE_[A-Z0-9_]+$/.test(error.code)
        ? error.code
        : "SKILL_TRACE_AUDIT_FAILED",
    };
  }
}
const invocationId = `isolated-agent:${process.pid}:${crypto.randomUUID()}`;
writeNew(path.join(values.usage_root, `${invocationId.replaceAll(":", "-")}.json`), {
  schema_version: 3,
  invocation_id: invocationId,
  class: "isolated-agent",
  workflow,
  environment_policy: {
    schema_version: 1,
    version: ISOLATED_AGENT_ENV_POLICY_VERSION,
    provider_auth_source: "audited-settings-file",
    session_credentials: Object.hasOwn(claudeEnvironment, "PROBLEM_LOCATOR_LOGPARSE_TOKEN") ? "explicit-logparse-broker" : "NONE",
    inbound: inboundEnvironment,
    claude_process: environmentKeySummary(claudeEnvironment),
  },
  tool_trace_audit: toolTraceAudit,
  effective_model: init[0].model,
  effective_caps: caps,
  // Claude Code 2.1.89 derives terminal modelUsage.maxOutputTokens from its
  // static model profile. It is not an echo of the request max_tokens value.
  usage_complete: true,
  usage,
  terminal: { subtype: final.subtype, is_error: final.is_error },
  turns: Number.isSafeInteger(final.num_turns) ? final.num_turns : null,
  wrapper_outcome: {
    schema_version: 1,
    status: wrapperFailureCode === null ? "PASS" : "FAIL",
    code: wrapperFailureCode,
  },
  hard_cap_enforcement: {
    turns: "claude-cli",
    cost_usd: "claude-cli",
    hard_timeout_seconds: "wrapper-process-watchdog",
    total_tokens: `terminal-usage-postcondition:${TOKEN_USAGE_FORMULA}`,
    ...(hasMaxOutputTokens ? { max_output_tokens: ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT } : {}),
  },
  timed_out: timedOut,
  process: { exit_code: exit.code, signal: exit.signal },
});
if (wrapperFailureCode !== null) process.stderr.write(`${wrapperFailureCode}\n`);
if (timedOut) process.exitCode = 124;
else if (wrapperFailureCode !== null) process.exitCode = 1;
else process.exitCode = 0;
