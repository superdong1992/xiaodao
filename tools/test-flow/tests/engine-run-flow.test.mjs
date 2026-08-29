import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { runFlow } from "../lib/engine.mjs";
import { verifyVerdict } from "../lib/evidence.mjs";
import { canonicalJson } from "../lib/util.mjs";

const TOOL_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const CONFIG_ROOT = path.join(TOOL_ROOT, "config");

function runGit(repoRoot, ...args) {
  const result = spawnSync("git", ["-C", repoRoot, ...args], { encoding: "utf8" });
  assert.equal(result.status, 0, `${result.stderr}\n${result.stdout}`);
  return result.stdout.trim();
}

function readConfig(configRoot, name) {
  return JSON.parse(fs.readFileSync(path.join(configRoot, name), "utf8"));
}

function replaceConfig(configRoot, name, value) {
  fs.writeFileSync(path.join(configRoot, name), canonicalJson(value), "utf8");
}

function hostClient() {
  if (process.platform === "win32") return "windows";
  if (process.platform === "darwin") return "macos";
  return "linux";
}

function writeRunFlowFixture(root) {
  const repoRoot = path.join(root, "repo");
  const configRoot = path.join(repoRoot, "tools", "test-flow", "config");
  const adaptersRoot = path.join(repoRoot, "tools", "test-flow", "adapters");
  const fixtureRoot = path.join(repoRoot, "fixture");
  fs.mkdirSync(path.dirname(configRoot), { recursive: true });
  fs.cpSync(CONFIG_ROOT, configRoot, { recursive: true });
  if (process.platform === "win32") {
    fs.mkdirSync(adaptersRoot, { recursive: true });
    fs.copyFileSync(path.join(TOOL_ROOT, "adapters", "windows-process.ps1"), path.join(adaptersRoot, "windows-process.ps1"));
  }
  fs.mkdirSync(fixtureRoot, { recursive: true });
  fs.writeFileSync(path.join(fixtureRoot, "identity.txt"), "runFlow production fixture\n");

  const proofs = readConfig(configRoot, "proofs.v2.json");
  proofs.proofs["proof.run-flow-pass"] = {
    description: "Exercise two ordered PASS stages through the complete runFlow orchestration.",
    acceptance: "all",
    stages: ["test.run-flow.first", "test.run-flow.second"],
  };
  proofs.goals["dev.default"].required_proofs = ["proof.run-flow-pass"];
  replaceConfig(configRoot, "proofs.v2.json", proofs);

  const stages = readConfig(configRoot, "stages.v2.json");
  stages.stages.push(
    {
      id: "test.run-flow.first",
      kind: "deterministic",
      depends_on: [],
      gates: ["test.run-flow.first"],
      identity_set: "test-run-flow",
      timeout_seconds: 30,
      progress_class: "local",
      reuse: { dev: "never", release: "never" },
      platforms: ["windows", "macos", "linux"],
    },
    {
      id: "test.run-flow.second",
      kind: "deterministic",
      depends_on: ["test.run-flow.first"],
      gates: ["test.run-flow.second"],
      identity_set: "test-run-flow",
      timeout_seconds: 30,
      progress_class: "local",
      reuse: { dev: "never", release: "never" },
      platforms: ["windows", "macos", "linux"],
    },
  );
  replaceConfig(configRoot, "stages.v2.json", stages);

  const gates = readConfig(configRoot, "gates.v2.json");
  for (const id of ["test.run-flow.first", "test.run-flow.second"]) {
    gates.gates[id] = {
      kind: "repository-check",
      check: "git-diff-check",
      evidence: ["repository-check.json"],
    };
  }
  replaceConfig(configRoot, "gates.v2.json", gates);

  const identities = readConfig(configRoot, "identities.v2.json");
  identities.components["test.run-flow"] = { kind: "paths", paths: ["fixture"] };
  identities.sets["test-run-flow"] = {
    producer: ["test.run-flow"],
    proof: ["test.run-flow"],
  };
  replaceConfig(configRoot, "identities.v2.json", identities);

  const policy = readConfig(configRoot, "policy.v2.json");
  policy.evidence.event_visibility_seconds = 1;
  replaceConfig(configRoot, "policy.v2.json", policy);

  runGit(repoRoot, "init", "--initial-branch=codex/run-flow-fixture");
  runGit(repoRoot, "config", "user.name", "Test Flow Fixture");
  runGit(repoRoot, "config", "user.email", "test-flow-fixture@example.invalid");
  runGit(repoRoot, "add", ".");
  runGit(repoRoot, "commit", "-m", "runFlow fixture");
  return repoRoot;
}

test("runFlow atomically finalizes two ordered PASS stages", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-run-flow-pass-"));
  try {
    const repoRoot = writeRunFlowFixture(root);
    const evidenceRoot = path.join(root, "evidence");
    const result = await runFlow(repoRoot, {
      track: "dev",
      goal: "dev.default",
      client: hostClient(),
      resume: "fresh",
      evidenceRoot,
    });

    assert.equal(result.verdict.operation_status, "PASS", JSON.stringify(result.verdict));
    if (result.verdict.functional_status !== "PASS") {
      const gatePath = path.join(result.attemptRoot, result.verdict.gates[0].receipt_path);
      const gate = JSON.parse(fs.readFileSync(gatePath, "utf8"));
      const output = [gate.execution.stdout_path, gate.execution.stderr_path]
        .filter((item) => typeof item === "string")
        .map((item) => {
          const target = path.join(result.attemptRoot, item);
          return fs.existsSync(target) ? fs.readFileSync(target, "utf8") : `missing:${item}`;
        });
      assert.fail(JSON.stringify({ gate, output }));
    }
    assert.deepEqual(result.verdict.stages.map((stage) => stage.id), [
      "test.run-flow.first",
      "test.run-flow.second",
    ]);
    assert.deepEqual(result.verdict.stages.map((stage) => stage.status), ["PASS", "PASS"]);
    assert.equal(result.verdict.stages.some((stage) => stage.code === "ORCHESTRATOR_EXCEPTION"), false);
    for (const stage of result.verdict.stages) {
      assert.equal(stage.result_source, "EXECUTED");
      assert.equal(stage.gates.length, 1);
      assert.equal(stage.gates[0].status, "PASS");
      assert.equal(fs.existsSync(path.join(result.attemptRoot, stage.stage_receipt_path)), true);
    }
    assert.equal(fs.existsSync(path.join(result.attemptRoot, "payload", "candidate-result.json")), true);
    assert.equal(verifyVerdict(result.attemptRoot).status, "PASS");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
