import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  auditLogparseToolTrace,
  boundedServicePrompt,
  expectedLogparseCommand,
  parseArguments,
  safeServiceError,
} from "../runtime/claude-deepseek-service-wrapper.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("service wrapper accepts only frozen Claude/provider roots and no external adapter", () => {
  const names = ["source-root", "runtime-root", "claude-entry", "settings", "config-root", "finalizer-entry", "logparse-entry", "private-root", "evidence-root", "usage-root", "run-id"];
  const argv = names.flatMap((name) => [`--${name}`, `/${name}`]);
  assert.equal(parseArguments(argv)["run-id"], "/run-id");
  assert.throws(() => parseArguments([...argv, "--adapter", "/tmp/other"]), (error) => error.code === "CLAUDE_DEEPSEEK_SERVICE_ARGUMENT_UNKNOWN");
});

test("service gives Bash only to LOGPARSE for the single broker call", () => {
  const source = fs.readFileSync(path.join(ROOT, "runtime", "claude-deepseek-service-wrapper.mjs"), "utf8");
  assert.equal(source.includes("dangerously-skip-permissions"), false);
  assert.match(source, /tools: logparsePhase \? \["Bash", "Skill"\] : \["Read", "Write", "Skill"\]/);
  assert.match(source, /Read\(\/\*\*\)/);
  assert.match(source, /Edit\(\/output\/\*\*\)/);
  assert.match(source, /allowToolErrors: !logparsePhase/);
  assert.match(source, /auditOnlyAllowedTools: \["Bash", "Glob"\]/);
  assert.equal(source.includes('tools: ["Bash"'), false);
  assert.equal(source.includes("runServiceLogparseCommand"), false);
  assert.match(source, /Bash\(problem-locator-logparse:\*\)/);
  assert.match(source, /auditLogparseToolTrace/);
  assert.match(source, /finalizerEntry: path\.resolve\(values\["finalizer-entry"\]\)/);
  assert.match(source, /CLAUDE_DEEPSEEK_SERVICE_RETRY_FORBIDDEN/);
});

test("service prompt closes repository reads and keeps Logparse inside the controlled LOGPARSE model", () => {
  const prompt = boundedServicePrompt("ROUTE", "frozen product prompt");
  assert.match(prompt, /current working directory is the only readable workspace/);
  assert.match(prompt, /Do not read repository/);
  assert.match(prompt, /Do not attempt Glob, Grep/);
  assert.match(prompt, /natural Simplified Chinese/);
  assert.match(prompt, /无法确认具体贡献者/);
  assert.match(prompt, /harness runs only the fixed product finalizer/);
  assert.match(prompt, /frozen product prompt/);
  const review = boundedServicePrompt("REVIEW", "review prompt");
  assert.match(review, /copy every existing candidate limitation sentence verbatim/);
  assert.match(review, /do not translate, paraphrase, summarize, or drop it/);
});

test("LOGPARSE trace requires Helper first and the exact broker command second", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-logparse-trace-"));
  fs.mkdirSync(path.join(root, "output", "proposals", "methods-preprocess"), { recursive: true });
  fs.writeFileSync(path.join(root, "output", "proposals", "methods-preprocess", "target_logs.json"), "{}\n");
  const command = "problem-locator-logparse parse-targets --request output/proposals/methods-preprocess/request.json --result output/proposals/methods-preprocess/target_logs.json";
  const prompt = `SERVER_PREPROCESS\n${command}\n`;
  assert.equal(expectedLogparseCommand(prompt).operation, "parse-targets");
  const result = {
    records: [{ name: "Skill", input: { skill: "logparse-diagnose" } }, { name: "Bash", input: { command } }],
    skills: [{ ordinal: 0, skill: "logparse-diagnose" }],
    bash: [{ ordinal: 1, command, exit_code: 0 }],
  };
  const receipt = auditLogparseToolTrace({ phase: "LOGPARSE", prompt, result, workspaceRoot: root });
  assert.equal(receipt.helper_calls, 1);
  assert.equal(receipt.broker_calls, 1);
  assert.ok(receipt.helper_tool_ordinal < receipt.broker_tool_ordinal);
  assert.throws(() => auditLogparseToolTrace({ phase: "LOGPARSE", prompt, result: { ...result, records: [...result.records].reverse() }, workspaceRoot: root }), (error) => error.code === "CLAUDE_DEEPSEEK_SERVICE_LOGPARSE_ORDER_INVALID");
});

test("safe service failure does not project secrets or raw provider output", () => {
  assert.deepEqual(safeServiceError({ code: "CLOSED", message: "safe", details: { token: "secret" } }), {
    schema_version: 1, status: "FAIL", code: "CLOSED", message: "safe",
  });
});
