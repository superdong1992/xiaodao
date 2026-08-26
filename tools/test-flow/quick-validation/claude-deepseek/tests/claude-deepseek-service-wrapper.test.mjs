import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  boundedServicePrompt,
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

test("service Claude process has no dangerous permission mode or model Bash", () => {
  const source = fs.readFileSync(path.join(ROOT, "runtime", "claude-deepseek-service-wrapper.mjs"), "utf8");
  assert.equal(source.includes("dangerously-skip-permissions"), false);
  assert.match(source, /tools: \["Read", "Write", "Skill"\]/);
  assert.match(source, /Read\(\/\*\*\)/);
  assert.match(source, /Edit\(\/output\/\*\*\)/);
  assert.match(source, /allowToolErrors: true/);
  assert.match(source, /auditOnlyAllowedTools: \["Bash", "Glob"\]/);
  assert.equal(source.includes('tools: ["Bash"'), false);
  assert.match(source, /runServiceLogparseCommand/);
  assert.match(source, /logparseEntry: path\.resolve\(values\["logparse-entry"\]\)/);
  assert.match(source, /finalizerEntry: path\.resolve\(values\["finalizer-entry"\]\)/);
  assert.match(source, /CLAUDE_DEEPSEEK_SERVICE_RETRY_FORBIDDEN/);
});

test("service prompt closes repository reads and delegates fixed product commands to the harness", () => {
  const prompt = boundedServicePrompt("ROUTE", "frozen product prompt");
  assert.match(prompt, /current working directory is the only readable workspace/);
  assert.match(prompt, /Do not read repository/);
  assert.match(prompt, /Do not attempt Bash, Glob, Grep/);
  assert.match(prompt, /natural Simplified Chinese/);
  assert.match(prompt, /无法确认具体贡献者/);
  assert.match(prompt, /harness runs any fixed product finalizer or Logparse command/);
  assert.match(prompt, /frozen product prompt/);
  const review = boundedServicePrompt("REVIEW", "review prompt");
  assert.match(review, /copy every existing candidate limitation sentence verbatim/);
  assert.match(review, /do not translate, paraphrase, summarize, or drop it/);
});

test("safe service failure does not project secrets or raw provider output", () => {
  assert.deepEqual(safeServiceError({ code: "CLOSED", message: "safe", details: { token: "secret" } }), {
    schema_version: 1, status: "FAIL", code: "CLOSED", message: "safe",
  });
});
