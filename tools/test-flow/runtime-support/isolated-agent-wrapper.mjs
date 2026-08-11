#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";

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
const caps = {
  max_turns: Number(values.max_turns),
  max_total_tokens: Number(values.max_total_tokens),
  max_budget_usd: Number(values.max_budget_usd),
  hard_timeout_seconds: Number(values.hard_timeout_seconds),
};
const workflow = values.workflow ?? "job";
if (!values.claude_entry || !values.settings || !values.model || !values.usage_root || !["job", "skill-generation"].includes(workflow) || !Number.isSafeInteger(caps.max_turns) || caps.max_turns <= 0 || !Number.isSafeInteger(caps.max_total_tokens) || caps.max_total_tokens <= 0 || !Number.isFinite(caps.max_budget_usd) || caps.max_budget_usd <= 0 || !Number.isSafeInteger(caps.hard_timeout_seconds) || caps.hard_timeout_seconds <= 0) {
  throw new Error("WRAPPER_REQUIRED_INPUT_INVALID");
}

const toolArguments = workflow === "skill-generation"
  ? ["--tools", "Read,Write,Skill", "--allowedTools", "Skill(wiki-to-diagnosis-skill)"]
  : ["--tools", "Read,Write"];

const child = spawn(process.execPath, [
  values.claude_entry,
  "-p",
  "--output-format", "stream-json",
  "--verbose",
  "--no-session-persistence",
  "--dangerously-skip-permissions",
  "--setting-sources", "user",
  "--settings", values.settings,
  "--model", values.model,
  "--max-turns", String(caps.max_turns),
  "--max-budget-usd", String(caps.max_budget_usd),
  ...toolArguments,
], { cwd: process.cwd(), env: process.env, stdio: ["pipe", "pipe", "pipe"] });

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
const usage = {
  input_tokens: Number(final.usage?.input_tokens ?? -1),
  output_tokens: Number(final.usage?.output_tokens ?? -1),
  cost_usd: Number(final.total_cost_usd ?? final.cost_usd ?? -1),
};
if (!Number.isSafeInteger(usage.input_tokens) || usage.input_tokens < 0 || !Number.isSafeInteger(usage.output_tokens) || usage.output_tokens < 0 || !Number.isFinite(usage.cost_usd) || usage.cost_usd < 0) throw new Error("WRAPPER_MODEL_USAGE_INVALID");
if (final.subtype !== "success" || final.is_error !== false || !Number.isSafeInteger(final.num_turns) || final.num_turns <= 0 || final.num_turns > caps.max_turns) throw new Error("WRAPPER_MODEL_TERMINAL_INVALID");
if (usage.input_tokens + usage.output_tokens > caps.max_total_tokens || usage.cost_usd > caps.max_budget_usd) throw new Error("WRAPPER_MODEL_CAP_EXCEEDED");
const invocationId = `isolated-agent:${process.pid}:${crypto.randomUUID()}`;
writeNew(path.join(values.usage_root, `${invocationId.replaceAll(":", "-")}.json`), {
  schema_version: 2,
  invocation_id: invocationId,
  class: "isolated-agent",
  effective_model: init[0].model,
  effective_caps: caps,
  usage_complete: true,
  usage,
  terminal: { subtype: final.subtype, is_error: final.is_error },
  turns: final.num_turns,
  hard_cap_enforcement: {
    turns: "claude-cli",
    cost_usd: "claude-cli",
    hard_timeout_seconds: "wrapper-process-watchdog",
    total_tokens: "terminal-usage-postcondition",
  },
  timed_out: timedOut,
  process: { exit_code: exit.code, signal: exit.signal },
});
if (timedOut) process.exitCode = 124;
else process.exitCode = exit.code ?? 1;
