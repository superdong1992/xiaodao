import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  E2E_GOAL,
  METHODS_GOAL,
  REQUIRED_EVIDENCE,
  parseArguments,
  sealGate,
} from "../run.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("standalone entry exposes only the two Darwin Claude/DeepSeek Dev goals", () => {
  assert.equal(parseArguments(["--goal", METHODS_GOAL]).goal, METHODS_GOAL);
  assert.equal(parseArguments(["--goal", E2E_GOAL, "--scenario", "api-execution-overrun"]).goal, E2E_GOAL);
  assert.throws(() => parseArguments(["--goal", "release.full"]), (error) => error.code === "CLAUDE_DEEPSEEK_GOAL_INVALID");
  assert.throws(() => parseArguments(["--goal", E2E_GOAL, "--client", "linux"]), (error) => error.code === "CLAUDE_DEEPSEEK_CLIENT_INVALID");
  assert.throws(() => parseArguments(["--goal", E2E_GOAL, "--docker-context", "colima"]), (error) => error.code === "CLAUDE_DEEPSEEK_ARGUMENT_UNKNOWN");
});

test("standalone entry does not import old CrossJob, Docker, browser, restart, or old Test Flow finalization", () => {
  const source = fs.readFileSync(path.join(ROOT, "run.mjs"), "utf8");
  for (const forbidden of ["cross-job-core.mjs", "lib/engine.mjs", "source-snapshot.mjs", "Dockerfile", "browser.mjs", "release.full"]) assert.equal(source.includes(forbidden), false, forbidden);
  assert.match(source, /old_cross_job: false/);
  assert.match(source, /old_test_flow_orchestrator: false/);
  assert.match(source, /automatic_model_retry: false/);
  assert.match(source, /docker: false/);
  assert.match(source, /browser: false/);
  assert.match(source, /restart: false/);
});

test("Methods contract stage includes migrated Codex Gate before Claude Bootstrap", () => {
  const source = fs.readFileSync(path.join(ROOT, "run.mjs"), "utf8");
  assert.match(source, /quick\.codex-luna\.contracts/);
  assert.match(source, /quick-codex-luna-contracts\.tap/);
  assert.match(source, /runDeterministicGates\(plan\.goal, evidenceRoot\)/);
  assert.ok(REQUIRED_EVIDENCE[METHODS_GOAL].includes("quick-codex-luna-contracts.tap"));
  assert.ok(REQUIRED_EVIDENCE[E2E_GOAL].includes("quick-claude-e2e-contracts.tap"));
});

test("central engine marks only Claude Quick deterministic contract Gates as zero-model usage complete", () => {
  const source = fs.readFileSync(path.join(ROOT, "..", "..", "lib", "engine.mjs"), "utf8");
  assert.match(source, /const claudeQuickContractGate = gate\.kind === "node-test"/);
  assert.match(source, /if \(claudeQuickContractGate\) \{\s*actionResult = \{ \.\.\.actionResult, usage_complete: true, invocations: \[\] \};/s);
  assert.match(source, /\["real\.macos-claude-deepseek-methods", "real\.macos-claude-deepseek-e2e"\]\.includes\(stage\.id\)/);
});

test("light Gate rejects missing evidence and wrong model-process cardinality", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-deepseek-gate-"));
  for (const name of REQUIRED_EVIDENCE[METHODS_GOAL]) {
    if (name === "model-invocations.json") fs.writeFileSync(path.join(root, name), '{"invocations":[]}\n');
    else if (name === "model-usage.json") fs.writeFileSync(path.join(root, name), '{"aggregate":{}}\n');
    else if (name === "adapter-receipt.json") fs.writeFileSync(path.join(root, name), '{"status":"PASS"}\n');
    else fs.writeFileSync(path.join(root, name), "{}\n");
  }
  assert.equal(sealGate({ goal: METHODS_GOAL, mode: "cache-verification", evidenceRoot: root, expectedCalls: 0 }).status, "PASS");
  const other = fs.mkdtempSync(path.join(os.tmpdir(), "claude-deepseek-gate-other-"));
  for (const name of REQUIRED_EVIDENCE[METHODS_GOAL]) fs.writeFileSync(path.join(other, name), name === "adapter-receipt.json" ? '{"status":"PASS"}\n' : name === "model-invocations.json" ? '{"invocations":[]}\n' : "{}\n");
  assert.equal(sealGate({ goal: METHODS_GOAL, mode: "bootstrap", evidenceRoot: other, expectedCalls: 1 }).status, "FAIL");
});
