import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  auditNonLogparseFileTrace,
  auditLogparseToolTrace,
  boundedServicePrompt,
  expectedLogparseCommand,
  parseArguments,
  safeServiceError,
  serviceToolPolicy,
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
  assert.match(source, /serviceToolPolicy\(\{ phase, workspaceRoot \}\)/);
  assert.match(source, /tools: toolPolicy\.tools,\s+allowedTools: toolPolicy\.allowedTools,/u);
  assert.match(source, /allowToolErrors: !logparsePhase/);
  assert.match(source, /auditOnlyAllowedTools: \["Bash", "Glob"\]/);
  assert.equal(source.includes("Skill(diagnose-rpc-timeout)"), false);
  assert.match(source, /CLAUDE_DEEPSEEK_SERVICE_BUSINESS_SKILL_LOADED/);
  assert.equal(source.includes("runServiceLogparseCommand"), false);
  assert.match(source, /Bash\(problem-locator-logparse:\*\)/);
  assert.match(source, /auditLogparseToolTrace/);
  assert.match(source, /finalizerEntry: path\.resolve\(values\["finalizer-entry"\]\)/);
  assert.match(source, /CLAUDE_DEEPSEEK_SERVICE_RETRY_FORBIDDEN/);
});

test("non-LOGPARSE permissions bind Read and Claude file edits to the absolute Job workspace", () => {
  const workspaceRoot = path.resolve(os.tmpdir(), "claude-service-job");
  const permissionPath = (value) => {
    const resolved = path.resolve(value);
    const drive = /^([A-Za-z]):[\\/](.*)$/u.exec(resolved);
    const portable = drive ? `${drive[1]}/${drive[2].replaceAll("\\", "/")}` : resolved.replaceAll("\\", "/").replace(/^\/+/, "");
    return `//${portable}`;
  };
  assert.deepEqual(serviceToolPolicy({ phase: "ROUTE", workspaceRoot }), {
    tools: ["Read", "Write"],
    allowedTools: [`Read(${permissionPath(workspaceRoot)}/**)`, `Edit(${permissionPath(path.join(workspaceRoot, "output"))}/**)`],
  });
  assert.deepEqual(serviceToolPolicy({ phase: "LOGPARSE", workspaceRoot }), {
    tools: ["Bash", "Skill"],
    allowedTools: ["Skill(logparse-diagnose)", "Bash(problem-locator-logparse:*)"],
  });
  assert.equal(serviceToolPolicy({ phase: "ROUTE", workspaceRoot }).allowedTools.includes("Edit(/output/**)"), false);
  assert.equal(serviceToolPolicy({ phase: "ROUTE", workspaceRoot }).allowedTools.some((rule) => rule.startsWith("Write(")), false);
});

test("non-LOGPARSE file trace rejects denial and Claude's default memory write", () => {
  const workspaceRoot = path.resolve(os.tmpdir(), "claude-service-trace");
  const valid = {
    records: [
      { name: "Read", is_error: false, input: { file_path: path.join(workspaceRoot, "runtime", "job-context.json") } },
      { name: "Write", is_error: false, input: { file_path: path.join(workspaceRoot, "output", "job_outcome.draft.json") } },
    ],
  };
  assert.deepEqual(auditNonLogparseFileTrace({ phase: "ROUTE", result: valid, workspaceRoot }), {
    schema_version: 1, required: true, status: "PASS", reads: 1, writes: 1, denied: 0, workspace_escape: false,
  });
  assert.throws(
    () => auditNonLogparseFileTrace({ phase: "ROUTE", result: { records: [{ ...valid.records[1], is_error: true }] }, workspaceRoot }),
    (error) => error.code === "CLAUDE_DEEPSEEK_SERVICE_FILE_TOOL_FAILED",
  );
  assert.throws(
    () => auditNonLogparseFileTrace({ phase: "ROUTE", result: { records: [{ ...valid.records[1], input: { file_path: path.resolve(workspaceRoot, "..", "server-config", "memory", "_probe.md") } }] }, workspaceRoot }),
    (error) => error.code === "CLAUDE_DEEPSEEK_SERVICE_FILE_SCOPE_INVALID",
  );
});

test("service prompt closes repository reads and keeps Logparse inside the controlled LOGPARSE model", () => {
  const prompt = boundedServicePrompt("ROUTE", "frozen product prompt");
  assert.match(prompt, /current working directory is the only readable workspace/);
  assert.match(prompt, /Do not read repository/);
  assert.match(prompt, /Do not attempt Glob, Grep/);
  assert.match(prompt, /natural Simplified Chinese/);
  assert.match(prompt, /无法确认具体贡献者/);
  assert.match(prompt, /harness runs only the fixed product finalizer/);
  assert.doesNotMatch(prompt, /Skill\(diagnose-rpc-timeout\)/);
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
