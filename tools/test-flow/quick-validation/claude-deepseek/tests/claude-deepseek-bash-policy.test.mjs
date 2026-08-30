import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("Evidence V2 model-cert has no Client, shell, upload, download, or result archive path", () => {
  const runner = fs.readFileSync(path.join(ROOT, "runtime", "claude-deepseek-e2e-runner.mjs"), "utf8");
  const wrapper = fs.readFileSync(path.join(ROOT, "runtime", "claude-deepseek-service-wrapper.mjs"), "utf8");
  assert.equal(runner.includes("claude-deepseek-bash-policy.mjs"), false);
  assert.equal(runner.includes("result.zip"), false);
  assert.equal(runner.includes("problem_locator_prepare_attachment"), false);
  assert.match(wrapper, /disallowedTools: \["Bash", "Glob", "Grep", "Skill"\]/u);
  assert.match(wrapper, /result\.bash\.length === 0 && result\.mcp\.length === 0/u);
});
