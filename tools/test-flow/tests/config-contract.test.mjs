import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { loadConfiguration, resolveGoalClosure } from "../lib/config.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

function withConfigMutation(fileName, mutate, action) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-config-v2-"));
  try {
    fs.cpSync(path.join(REPO_ROOT, "tools", "test-flow", "config"), root, { recursive: true });
    const filePath = path.join(root, fileName);
    const value = JSON.parse(fs.readFileSync(filePath, "utf8"));
    mutate(value);
    fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`);
    return action(root);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

test("v2 is the only loaded Test Flow configuration bundle", () => {
  const config = loadConfiguration(REPO_ROOT);
  assert.equal(config.proofs.schema_version, 2);
  assert.equal(config.stages.schema_version, 2);
  assert.equal(config.gates.schema_version, 2);
  assert.equal(config.identities.schema_version, 2);
  assert.equal(config.policy.schema_version, 2);
  assert.equal(config.runtimeProfiles.schema_version, 2);
  assert.ok(Object.values(config.files).every((filePath) => filePath.endsWith(".v2.json")));
  assert.match(config.bundle_digest, /^[a-f0-9]{64}$/);
  for (const retired of ["flow.v1.json", "proofs.v1.json", "gates.v1.json", "identities.v1.json"]) {
    assert.equal(fs.existsSync(path.join(REPO_ROOT, "tools", "test-flow", "config", retired)), false);
  }
});

test("unknown configuration fields fail closed", () => {
  assert.throws(() => withConfigMutation("gates.v2.json", (value) => {
    value.gates["det.unit"].unused_patch_field = true;
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_GATE_FIELDS");
});

test("Codex runtime profile must equal the executable/model contract constants", () => {
  const mutations = [
    ["version", "0.149.0-alpha.4.2", "CONFIG_RUNTIME_CODEX_VERSION"],
    ["executable_sha256", "0".repeat(64), "CONFIG_RUNTIME_CODEX_HASH"],
    ["model", "gpt-5.6-sol", "CONFIG_RUNTIME_CODEX_MODEL"],
    ["reasoning_effort", "high", "CONFIG_RUNTIME_CODEX_EFFORT"],
  ];
  for (const [field, replacement, code] of mutations) {
    assert.throws(() => withConfigMutation("runtime-profiles.v2.json", (value) => {
      value.profiles.release.codex[field] = replacement;
    }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === code);
  }
});

test("orphan Gates fail before admission", () => {
  assert.throws(() => withConfigMutation("gates.v2.json", (value) => {
    value.gates["det.orphan"] = {
      kind: "pytest",
      selectors: ["tests/deterministic/unit"],
      min_passed: 1,
      skip_policy: "forbid",
      runtime_profile: "python-test",
      evidence: ["pytest.xml", "pytest-summary.json"],
    };
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_ORPHAN_GATE");
});

test("release.full has one isolated Wiki generation Gate followed by one fresh six-stage CrossJob closure without Codex coupling", () => {
  const config = loadConfiguration(REPO_ROOT);
  const expected = [
    "journey.cross-job.environment",
    "journey.cross-job.route",
    "journey.cross-job.upload",
    "journey.cross-job.diagnose",
    "journey.cross-job.review",
    "journey.cross-job.publish-restart",
  ];
  for (const client of ["windows", "macos", "linux"]) {
    const closure = resolveGoalClosure(config, { goalId: "release.full", track: "release", client });
    assert.deepEqual(closure.stages.filter((stage) => stage.id.startsWith("journey.cross-job.")).map((stage) => stage.id), expected);
    assert.deepEqual(
      closure.stages.filter((stage) => stage.kind === "isolated-real").map((stage) => stage.id),
      ["real.skill-generation"],
    );
    assert.equal(closure.stages.some((stage) => stage.id === "real.codex-luna-methods"), false);
    assert.ok(closure.stages.findIndex((stage) => stage.id === "real.skill-generation") < closure.stages.findIndex((stage) => stage.id === "journey.cross-job.environment"));
    assert.ok(closure.stages.filter((stage) => stage.id.startsWith("journey.cross-job.")).every((stage) => stage.reuse.release === "never"));
  }
});

test("Codex Luna is an independent Darwin Release goal and an explicit Dev real proof", () => {
  const config = loadConfiguration(REPO_ROOT);
  const release = resolveGoalClosure(config, {
    goalId: "release.codex-luna-methods",
    track: "release",
    client: "macos",
  });
  assert.deepEqual(release.stages.map((stage) => stage.id), [
    "framework.self-test",
    "repository.static",
    "deterministic.affected",
    "deterministic.full",
    "real.codex-luna-methods",
  ]);
  assert.equal(release.stages.some((stage) => stage.id === "real.skill-generation"), false);
  assert.equal(release.stages.some((stage) => stage.id.startsWith("journey.cross-job.")), false);

  const crossJobDev = resolveGoalClosure(config, {
    goalId: "dev.real",
    track: "dev",
    requestedStage: "journey.cross-job.environment",
    client: "macos",
  });
  assert.equal(crossJobDev.stages.some((stage) => stage.id === "real.codex-luna-methods"), false);

  const codexDev = resolveGoalClosure(config, {
    goalId: "dev.real",
    track: "dev",
    requestedStage: "real.codex-luna-methods",
    client: "macos",
  });
  assert.equal(codexDev.stages.at(-1).id, "real.codex-luna-methods");
  assert.equal(codexDev.stages.some((stage) => stage.id === "real.skill-generation"), false);
});

test("repository Python compilation covers runtime support scripts used by Release", () => {
  const config = loadConfiguration(REPO_ROOT);
  assert.deepEqual(config.gates.gates["repo.compileall"].paths, [
    "src",
    "tests",
    "tools/test-flow/runtime-support",
  ]);
});

test("every isolated real Agent Gate declares its exact invocation count", () => {
  const config = loadConfiguration(REPO_ROOT);
  const gates = Object.entries(config.gates.gates)
    .filter(([, gate]) => gate.kind === "pytest" && gate.environment_profile?.startsWith("real-") && gate.environment_profile !== "real-logparse");
  assert.ok(gates.length > 0);
  for (const [gateId, gate] of gates) {
    assert.ok(Number.isInteger(gate.isolated_agent_invocations) && gate.isolated_agent_invocations > 0, gateId);
    assert.ok(gate.min_passed >= gate.isolated_agent_invocations, gateId);
  }
  assert.deepEqual(config.gates.gates["real.agent.skill-generation"].evidence, [
    "pytest.xml",
    "pytest-summary.json",
    "model-usage.json",
    "scenario-evaluation-audit.json",
    "generated-skill.json",
  ]);
});

test("model watchdogs cover every serial Backend invocation and Stage evidence", () => {
  const config = loadConfiguration(REPO_ROOT);
  const release = config.runtimeProfiles.profiles.release;
  const isolatedStages = config.stages.stages.filter((stage) => stage.kind === "isolated-real" && stage.id !== "real.logparse");
  assert.ok(isolatedStages.length > 0);
  for (const stage of isolatedStages) {
    const count = stage.gates.reduce((sum, gateId) => sum + config.gates.gates[gateId].isolated_agent_invocations, 0);
    const cap = release.real_caps[stage.real_cap_id ?? "isolated"];
    assert.ok(count * (cap.hard_timeout_seconds + 30) + 30 < stage.timeout_seconds, stage.id);
  }
  assert.ok(config.policy.process.real_no_progress_seconds < release.real_caps.isolated.hard_timeout_seconds);
  assert.equal(release.claude.max_output_tokens_upper_limit, 64000);
  assert.deepEqual(release.real_caps["isolated.skill-generation"], {
    max_turns: 16,
    max_total_tokens: 1000000,
    max_output_tokens: 64000,
    max_budget_usd: 10,
    hard_timeout_seconds: 1800,
  });
  assert.equal(config.stages.stages.find((stage) => stage.id === "real.skill-generation").estimated_tokens, 600000);
  assert.deepEqual(release.real_caps.service_agent, {
    max_turns: 50,
    max_total_tokens: 2000000,
    max_budget_usd: 3,
    hard_timeout_seconds: 600,
  });
  assert.ok(Object.entries(release.real_caps).every(([capId, cap]) => capId === "isolated.skill-generation" || cap.max_output_tokens === undefined));
  assert.equal(config.stages.stages.find((stage) => stage.id === "real.skill-generation").real_cap_id, "isolated.skill-generation");
});

test("finalization, rollout and the legacy v6 isolated diagnose path are not schedulable", () => {
  const config = loadConfiguration(REPO_ROOT);
  const ids = config.stages.stages.map((stage) => stage.id);
  assert.equal(ids.includes("evidence.finalize"), false);
  assert.equal(ids.includes("rollout.parity"), false);
  assert.equal(ids.includes("real.diagnose"), false);
  assert.equal(Object.hasOwn(config.proofs.goals, "release.rollout-parity"), false);
  assert.equal(config.proofs.goals["dev.real"].selectable_proofs.includes("proof.real-diagnose"), false);
  assert.equal(Object.hasOwn(config.proofs.proofs, "proof.real-diagnose"), false);
  assert.equal(Object.hasOwn(config.gates.gates, "real.agent.diagnose"), false);
  assert.equal(Object.hasOwn(config.identities.components, "skill.legacy-diagnose"), false);
  assert.equal(Object.hasOwn(config.identities.sets, "real-diagnose"), false);
});

test("host capability has one executable adapter path and no legacy environment-gated pytest", () => {
  const config = loadConfiguration(REPO_ROOT);
  const stage = config.stages.stages.find((candidate) => candidate.id === "platform.host-capability");
  const serverStage = config.stages.stages.find((candidate) => candidate.id === "platform.server-linux-capability");
  assert.deepEqual(stage.gates, ["platform.host-adapter"]);
  assert.equal(Object.hasOwn(config.gates.gates, "platform.compat-contract"), false);
  assert.deepEqual(serverStage.gates, ["platform.server-linux-adapter"]);
  assert.deepEqual(config.gates.gates["platform.server-linux-adapter"].required_claims, [
    "linux-runtime", "installed-distribution", "native-startup", "process-tree-cleanup",
  ]);
  assert.equal(Object.hasOwn(config.gates.gates, "platform.client-contract"), false);
  assert.equal(fs.existsSync(path.join(REPO_ROOT, "tests", "platform", "client")), false);
  assert.equal(fs.existsSync(path.join(REPO_ROOT, "tools", "test-flow", "adapters", "fixtures", "claude-flat-probe.mjs")), true);
  assert.deepEqual(config.identities.components["adapter.host"].paths, [
    "tools/test-flow/adapters/host-capability.mjs",
    "tools/test-flow/adapters/fixtures/claude-flat-probe.mjs",
  ]);
});

test("every public platform has a repository-owned adapter and no harness identity input", () => {
  const config = loadConfiguration(REPO_ROOT);
  for (const client of ["windows", "macos", "linux"]) {
    assert.equal(fs.existsSync(path.join(REPO_ROOT, "tools", "test-flow", "adapters", `${client}-linux-release.mjs`)), true);
  }
  const identityPaths = Object.values(config.identities.components)
    .filter((component) => component.kind === "paths")
    .flatMap((component) => component.paths);
  assert.equal(identityPaths.some((entry) => entry.includes("tools/test-flow/harness")), false);
  assert.equal(fs.existsSync(path.join(REPO_ROOT, "tools", "test-flow", "harness")), false);
});

test("every repository identity path exists and Methods registration and generator identities stay distinct", () => {
  const config = loadConfiguration(REPO_ROOT);
  for (const [componentId, component] of Object.entries(config.identities.components)) {
    if (component.kind !== "paths") continue;
    for (const relative of component.paths) {
      assert.equal(fs.existsSync(path.join(REPO_ROOT, relative)), true, `${componentId} is missing ${relative}`);
    }
  }
  assert.deepEqual(config.identities.components["skill.registration"], {
    kind: "release-case",
    root: "tests/cases/release",
    partition: "registration",
  });
  assert.deepEqual(config.identities.components["case.wiki"], {
    kind: "release-case",
    root: "tests/cases/release",
    partition: "wiki",
  });
  assert.deepEqual(config.identities.components["case.journey"], {
    kind: "release-case",
    root: "tests/cases/release",
    partition: "journey",
  });
  assert.deepEqual(config.identities.components["case.oracle"], {
    kind: "release-case",
    root: "tests/cases/release",
    partition: "oracle",
  });
  assert.deepEqual(config.identities.components["skill.generator"].paths, [
    ".agents/skills/wiki-to-diagnosis-skill",
  ]);
  assert.deepEqual(config.identities.components["skill.logparse"].paths, [
    ".claude/skills/logparse-diagnose",
  ]);
  assert.deepEqual(config.identities.components["skill.generic-adapter"].paths, [
    ".claude/skills/adapt-lan-generic-locator-v2",
  ]);
  assert.deepEqual(config.identities.components["skill.generic-parity"].paths, [
    "tests/fixtures/components/generic-problem-locator-dual-mode",
  ]);
  assert.ok(config.identities.sets.deterministic.producer.includes("skill.generic-adapter"));
  assert.ok(config.identities.sets["real-generic-locator"].producer.includes("skill.generic-adapter"));
  for (const setId of ["real-agent", "real-generic-locator", "real-route", "real-review", "real-skill-generation"]) {
    assert.ok(config.identities.sets[setId].producer.includes("runtime.support"), setId);
  }
  assert.ok(config.identities.sets["real-skill-generation"].producer.includes("product.source"));
  assert.ok(config.identities.sets["real-skill-generation"].producer.includes("case.journey"));
});

test("the standalone browser REST guide participates in framework proof identity", () => {
  const config = loadConfiguration(REPO_ROOT);
  assert.ok(config.identities.components["framework.docs"].paths.includes("docs/browser-rest-api.md"));
  assert.ok(config.identities.sets.framework.proof.includes("framework.docs"));
});

test("Git checkouts and the current worktree preserve byte-pinned text as LF", () => {
  const attributes = fs.readFileSync(path.join(REPO_ROOT, ".gitattributes"), "utf8");
  assert.ok(attributes.split(/\r?\n/u).includes("* text=auto eol=lf"));
  const result = spawnSync(
    "git",
    ["-c", `safe.directory=${REPO_ROOT}`, "-C", REPO_ROOT, "ls-files", "--eol"],
    { encoding: "utf8" },
  );
  assert.equal(result.status, 0, result.stderr);
  const nonLfText = result.stdout.split(/\r?\n/u)
    .filter((line) => /w\/(?:crlf|mixed)\b/u.test(line));
  assert.deepEqual(nonLfText, [], "tracked text contains CRLF or mixed working-tree bytes");
});

test("unsupported policy versions and dead fields fail closed", () => {
  assert.throws(() => withConfigMutation("policy.v2.json", (value) => {
    value.process.progress_allowlist_version = "test-flow-progress-v1";
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_PROCESS_VERSION");
  assert.throws(() => withConfigMutation("proofs.v2.json", (value) => {
    value.proofs["proof.framework"].fresh = true;
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_PROOF_FIELDS");
});

test("isolated invocation and aggregate deadline declarations fail closed", () => {
  assert.throws(() => withConfigMutation("gates.v2.json", (value) => {
    delete value.gates["real.agent.skill-generation"].isolated_agent_invocations;
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_PYTEST_INVOCATIONS");
  assert.throws(() => withConfigMutation("gates.v2.json", (value) => {
    value.gates["real.agent.review"].isolated_agent_invocations = 2;
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_PYTEST_INVOCATIONS");
  assert.throws(() => withConfigMutation("stages.v2.json", (value) => {
    value.stages.find((stage) => stage.id === "real.review").timeout_seconds = 960;
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_ISOLATED_TIMEOUT_MARGIN");
  assert.throws(() => withConfigMutation("stages.v2.json", (value) => {
    value.stages.find((stage) => stage.id === "real.skill-generation").real_cap_id = "isolated.unknown";
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_STAGE_REAL_CAP_UNKNOWN");
  assert.throws(() => withConfigMutation("stages.v2.json", (value) => {
    value.stages.find((stage) => stage.id === "framework.self-test").real_cap_id = "isolated";
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_STAGE_REAL_CAP_SCOPE");
  assert.throws(() => withConfigMutation("stages.v2.json", (value) => {
    value.stages.find((stage) => stage.id === "real.skill-generation").timeout_seconds = 1860;
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_ISOLATED_TIMEOUT_MARGIN");
  assert.throws(() => withConfigMutation("stages.v2.json", (value) => {
    value.stages.find((stage) => stage.id === "real.skill-generation").estimated_tokens = 0;
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_STAGE_ESTIMATED_TOKENS");
  assert.throws(() => withConfigMutation("stages.v2.json", (value) => {
    value.stages.find((stage) => stage.id === "framework.self-test").estimated_tokens = 1;
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_STAGE_ESTIMATED_TOKENS_SCOPE");
  assert.throws(() => withConfigMutation("stages.v2.json", (value) => {
    value.stages.find((stage) => stage.id === "real.skill-generation").estimated_tokens = 1000001;
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_STAGE_ESTIMATED_TOKENS_CAP");
});

test("isolated output token caps are positive and cannot exceed the pinned Claude runtime", () => {
  assert.throws(() => withConfigMutation("runtime-profiles.v2.json", (value) => {
    value.profiles.release.claude.max_output_tokens_upper_limit = 128000;
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_RUNTIME_MAX_OUTPUT_TOKENS");
  assert.throws(() => withConfigMutation("runtime-profiles.v2.json", (value) => {
    value.profiles.release.real_caps["isolated.skill-generation"].max_output_tokens = 0;
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_RUNTIME_MAX_OUTPUT_TOKENS");
  assert.throws(() => withConfigMutation("runtime-profiles.v2.json", (value) => {
    value.profiles.release.real_caps["isolated.skill-generation"].max_output_tokens = 64001;
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_RUNTIME_MAX_OUTPUT_TOKENS");
  assert.throws(() => withConfigMutation("runtime-profiles.v2.json", (value) => {
    value.profiles.release.real_caps.service_agent.max_output_tokens = 1;
  }, (root) => loadConfiguration(REPO_ROOT, root)), (error) => error.code === "CONFIG_RUNTIME_MAX_OUTPUT_TOKENS_SCOPE");
});
