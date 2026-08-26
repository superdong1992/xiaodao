import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  DIAGNOSIS_SCENARIOS,
  DIAGNOSIS_LIMITS,
  FIXED_MODULE,
  FRAMEWORK_ID,
  GENERATED_SKILL_NAME,
  META_SKILL_NAME,
  buildProducerIdentity,
  buildSourceWikiIdentity,
  publishGenerationCache,
  validateGenerationCache,
} from "../runtime/lan-skill-contract.mjs";
import { generationPrompt } from "../runtime/lan-skill-generation.mjs";
import { diagnosisPrompt } from "../runtime/lan-skill-diagnosis.mjs";


const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..", "..");


test("LAN meta Skill contract has distinct identity and fixed two-scenario matrix", () => {
  assert.equal(FRAMEWORK_ID, "claude-deepseek-lan-logparse-fast-e2e");
  assert.equal(META_SKILL_NAME, "wiki-to-logparse-diagnosis-skill");
  assert.equal(GENERATED_SKILL_NAME, "diagnose-rpc-timeout-lan");
  assert.equal(FIXED_MODULE, "rpc");
  assert.deepEqual(DIAGNOSIS_SCENARIOS, ["missing-slots", "complete"]);
  assert.deepEqual(DIAGNOSIS_LIMITS, {
    "missing-slots": { token_limit: 100_000, usd_limit: 1 },
    complete: { token_limit: 900_000, usd_limit: 7 },
  });
});


test("source identity preserves ordered duplicate Wiki templates", () => {
  const wiki = Buffer.from("```text\nA value={value}\nA value={value}\n```\n", "utf8");
  const identity = buildSourceWikiIdentity(wiki);
  assert.deepEqual(identity.log_templates, ["A value={value}", "A value={value}"]);
  assert.equal(identity.source_path, "inputs/wiki.md");
  assert.match(identity.sha256, /^[0-9a-f]{64}$/u);
  assert.match(identity.log_template_inventory_sha256, /^[0-9a-f]{64}$/u);
});


test("generation prompt fixes slots, helper delegation, module, and ZIP delivery", () => {
  const prompt = generationPrompt();
  for (const phrase of [
    `{"skill":"${META_SKILL_NAME}"}`,
    "client_slot",
    "server_slot",
    "logparse-diagnose",
    "target_logs[*].log_path",
    "result.txt",
    "result.zip",
    "module rpc",
  ]) assert.match(prompt, new RegExp(phrase.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&"), "u"));
  assert.match(prompt, /Do not embed old Logparse CLI commands/u);
  assert.match(prompt, /never use a complete template/u);
  assert.match(prompt, /never retain a \{field\} or %x placeholder/u);
});


test("output contract is self-contained about canonical marker extraction", () => {
  const contract = fs.readFileSync(path.join(ROOT, ".claude", "skills", META_SKILL_NAME, "references", "output-contract.md"), "utf8");
  assert.match(contract, /第一个 `\{field\}` 或 `%x` 占位符前/u);
  assert.match(contract, /最长的非空字面片段/u);
  assert.match(contract, /不得截短、改选其他片段、保留占位符或使用整条模板/u);
});


test("diagnosis prompts separate missing-slot and complete behavior", () => {
  const missing = diagnosisPrompt("missing-slots");
  assert.match(missing, /Neither client_slot nor server_slot was provided/u);
  assert.match(missing, /Do not load logparse-diagnose/u);
  const complete = diagnosisPrompt("complete");
  assert.match(complete, /client_slot=1 and server_slot=2/u);
  assert.match(complete, /Load the installed logparse-diagnose/u);
  assert.match(complete, /output\/result\.zip/u);
  assert.match(complete, /problem-locator-logparse target-logs/u);
  assert.match(complete, /client__rpc__slot_1__rpc_client\.log/u);
  assert.match(complete, /server__rpc__slot_2__rpc_server\.log/u);
  assert.match(complete, /at most one read-only ls command/u);
  assert.match(complete, /Do not run any verification or listing command/u);
});


test("broker contract stub is repository-owned and checks exact client/server slots", () => {
  const source = fs.readFileSync(path.join(ROOT, "tools", "test-flow", "quick-validation", "claude-deepseek-lan-skill", "runtime", "problem-locator-logparse"), "utf8");
  assert.match(source, /"slot": "1"/u);
  assert.match(source, /"slot": "2"/u);
  assert.match(source, /choices=\("target-logs",\)/u);
  assert.match(source, /\("output", "proposals"\)/u);
  assert.match(source, /request_parts\[2\] != result_parts\[2\]/u);
  assert.doesNotMatch(source, /parse-targets/u);
});


test("generation cache is immutable and bound to producer plus package bytes", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lan-generation-cache-"));
  const wiki = path.join(root, "wiki.md");
  fs.writeFileSync(wiki, "# Wiki\n", "utf8");
  const packageRoot = path.join(root, GENERATED_SKILL_NAME);
  fs.mkdirSync(packageRoot);
  fs.writeFileSync(path.join(packageRoot, "SKILL.md"), "fixture\n", "utf8");
  const runner = path.join(ROOT, "tools", "test-flow", "quick-validation", "claude-deepseek-lan-skill", "run.mjs");
  const producer = buildProducerIdentity({
    wiki,
    metaSkillRoot: path.join(ROOT, ".claude", "skills", META_SKILL_NAME),
    module: FIXED_MODULE,
    claudeIdentity: { version: "fixture", model: "fixture" },
    runnerFiles: [runner],
  });
  const cacheRoot = path.join(root, "cache");
  const stagingRoot = path.join(cacheRoot, ".staging", "generation-test");
  const published = publishGenerationCache({ cacheRoot, producer, packageRoot, stagingRoot });
  assert.equal(published.published, true);
  assert.equal(published.root.startsWith(path.resolve(cacheRoot)), true);
  assert.equal(fs.existsSync(stagingRoot), false);
  assert.equal(validateGenerationCache({ cacheRoot, producer }).status, "PASS");
  fs.appendFileSync(published.package_root + "/SKILL.md", "drift\n");
  assert.throws(() => validateGenerationCache({ cacheRoot, producer }), /drifted/u);
});
