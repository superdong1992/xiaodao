import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { quickValidationCodexEntryStrategy, quickValidationScratchRoot } from "../lib/actions.mjs";
import { supportedQuickValidationOrchestrator } from "../lib/planner.mjs";
import {
  WSL_CONTAINER_SUITE_SCENARIOS,
  buildContainerSuitePlan,
} from "../quick-validation/wsl/container-suite.mjs";
import {
  CODEX_LUNA_EXPECTED_CLI_VERSION,
  CODEX_LUNA_LINUX_EXPECTED_CLI_SHA256,
  CODEX_LUNA_LINUX_EXPECTED_CLI_VERSION,
  codexLunaAppServerCliVersion,
  codexLunaExecutableIdentity,
  codexLunaHelperDirectory,
} from "../runtime-support/codex-luna-contract.mjs";

const TEST_FLOW_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const WSL_ROOT = path.join(TEST_FLOW_ROOT, "quick-validation", "wsl");
const MARKED = { TEST_FLOW_QUICK_UBUNTU2204_CONTAINER: "1" };

test("WSL all-scenarios freezes nine isolated containers instead of one provider suite container", () => {
  const providerPlan = {
    schema_version: 1,
    framework: "macos-codex-luna-fast-e2e",
    goal: "e2e",
    mode: "e2e-suite",
    scenarios: [...WSL_CONTAINER_SUITE_SCENARIOS],
    execution: {
      entry: "tools/test-flow/quick-validation/codex-luna/run.mjs",
      expected_model_calls: 44,
      token_cap: 18_000_000,
      equivalent_usd_cap: 27,
      wall_timeout_seconds: 16_200,
    },
    admission: { status: "READY", blockers: [] },
    plan_sha256: "b".repeat(64),
  };
  const plan = buildContainerSuitePlan({
    provider: "codex-luna",
    providerPlan,
    imageSeal: {
      schema_version: 1,
      image_id: `sha256:${"a".repeat(64)}`,
      platform: "linux/amd64",
      profile: "ubuntu22.04-central-v1",
      status: "PASS",
    },
  });
  assert.equal(plan.execution.topology, "NINE_ISOLATED_CONTAINERS");
  assert.equal(plan.execution.container_count, 9);
  assert.equal(plan.execution.max_concurrency, 9);
  assert.equal(plan.execution.scenarios_per_container, 1);
  assert.equal(plan.execution.suite_wall_seconds, 1_800);
  assert.deepEqual(plan.scenarios, WSL_CONTAINER_SUITE_SCENARIOS);
});

test("central Quick Goals accept native macOS or the explicitly marked Linux container only", () => {
  assert.equal(supportedQuickValidationOrchestrator("darwin", "arm64", {}), true);
  assert.equal(supportedQuickValidationOrchestrator("darwin", "x64", {}), false);
  assert.equal(supportedQuickValidationOrchestrator("linux", "x64", {}), false);
  assert.equal(supportedQuickValidationOrchestrator("linux", "x64", MARKED), true);
  assert.equal(supportedQuickValidationOrchestrator("linux", "arm64", MARKED), false);
});

test("the marked container relocates only Quick model scratch outside persisted evidence", () => {
  const fixture = fs.mkdtempSync(path.join(os.tmpdir(), "quick-scratch-"));
  try {
    const attemptRoot = path.join(fixture, "evidence", "run-1");
    const privateRoot = path.join(fixture, "private");
    fs.mkdirSync(attemptRoot, { recursive: true });
    const actual = quickValidationScratchRoot(
      { attemptRoot },
      "codex-methods",
      { ...MARKED, TEST_FLOW_QUICK_SCRATCH_ROOT: privateRoot },
    );
    assert.equal(actual, path.join(privateRoot, "run-1", "codex-methods"));
    assert.throws(
      () => quickValidationScratchRoot(
        { attemptRoot },
        "codex-methods",
        { ...MARKED, TEST_FLOW_QUICK_SCRATCH_ROOT: path.join(attemptRoot, "nested") },
      ),
      /QUICK_VALIDATION_SCRATCH_ROOT_OVERLAP/u,
    );
  } finally {
    fs.rmSync(fixture, { recursive: true, force: true });
  }
});

test("the container marker selects only the frozen Linux Codex and app-server identities", () => {
  const native = codexLunaExecutableIdentity({ platform: "darwin", architecture: "arm64", environment: {} });
  assert.equal(native.version, CODEX_LUNA_EXPECTED_CLI_VERSION);
  assert.equal(native.linux_sandbox_sha256, null);
  const linux = codexLunaExecutableIdentity({ platform: "linux", architecture: "x64", environment: MARKED });
  assert.equal(linux.version, CODEX_LUNA_LINUX_EXPECTED_CLI_VERSION);
  assert.equal(linux.cli_sha256, CODEX_LUNA_LINUX_EXPECTED_CLI_SHA256);
  assert.equal(linux.linux_sandbox_sha256, linux.cli_sha256);
  assert.equal(codexLunaHelperDirectory("/run/private/codex", { platform: "linux", architecture: "x64", environment: MARKED }), "/usr/bin");
  const nativeEntry = path.join(os.tmpdir(), "native", "codex");
  assert.equal(codexLunaHelperDirectory(nativeEntry, { platform: "darwin", architecture: "arm64", environment: {} }), path.dirname(path.resolve(nativeEntry)));
  assert.equal(codexLunaAppServerCliVersion({ platform: "linux", architecture: "x64", environment: {} }), "0.149.0-alpha.4.1");
  assert.equal(codexLunaAppServerCliVersion({ platform: "linux", architecture: "x64", environment: MARKED }), "0.149.1");
  assert.equal(quickValidationCodexEntryStrategy({ platform: "darwin", architecture: "arm64", environment: {} }), "attempt-private-copy");
  assert.equal(quickValidationCodexEntryStrategy({ platform: "linux", architecture: "x64", environment: MARKED }), "sealed-system-entry");
  assert.equal(quickValidationCodexEntryStrategy({ platform: "linux", architecture: "x64", environment: {} }), "attempt-private-copy");
});

test("the Ubuntu wrapper delegates only to provider standalone Fast E2E entries", () => {
  const launcher = fs.readFileSync(path.join(WSL_ROOT, "run.sh"), "utf8");
  const containerSuite = fs.readFileSync(path.join(WSL_ROOT, "container-suite.mjs"), "utf8");
  const codexE2E = fs.readFileSync(path.join(TEST_FLOW_ROOT, "quick-validation", "codex-luna", "runtime", "macos-codex-luna-e2e-runner.mjs"), "utf8");
  const codexService = fs.readFileSync(path.join(TEST_FLOW_ROOT, "quick-validation", "codex-luna", "runtime", "macos-codex-luna-service-wrapper.mjs"), "utf8");
  const codexRuntime = fs.readFileSync(path.join(TEST_FLOW_ROOT, "runtime-support", "codex-luna-app-server-runtime.mjs"), "utf8");
  assert.doesNotMatch(launcher, /tools\/test-flow\/run\.sh/u);
  assert.match(launcher, /quick-validation\/codex-luna\/run\.sh/u);
  assert.match(launcher, /quick-validation\/claude-deepseek\/run\.sh/u);
  assert.match(launcher, /quick-validation\/claude-deepseek-lan-skill\/run\.sh/u);
  assert.match(launcher, /claude-deepseek-lan-skill:generation/u);
  assert.match(launcher, /claude-deepseek-lan-skill:diagnosis/u);
  assert.match(launcher, /--goal generation/u);
  assert.match(launcher, /--goal diagnosis/u);
  assert.match(launcher, /--mode/u);
  assert.match(launcher, /--all-scenarios/u);
  assert.match(launcher, /container-suite\.mjs/u);
  assert.match(launcher, /run_scenario_container "\$scenario_id" "\$scenario_container" &/u);
  assert.match(launcher, /--scenario "\$scenario_id" --allow-real-model/u);
  assert.match(launcher, /\.children\/\$scenario_id/u);
  assert.match(launcher, /container-runtime\/\$scenario_id/u);
  assert.match(launcher, /shared-preflight/u);
  assert.match(launcher, /-materialize/u);
  assert.match(launcher, /materialize --suite-root \/suite/u);
  assert.match(launcher, /run_utility_container[\s\S]*\/private\/tmp:rw,exec,nosuid,nodev,mode=1777/u);
  assert.match(containerSuite, /NINE_ISOLATED_CONTAINERS/u);
  assert.match(containerSuite, /container_count: WSL_CONTAINER_SUITE_SCENARIOS\.length/u);
  assert.match(containerSuite, /max_concurrency: WSL_CONTAINER_SUITE_SCENARIOS\.length/u);
  assert.match(containerSuite, /FINISH_ALL_STARTED_CONTAINERS/u);
  assert.doesNotMatch(launcher, /--track dev/u);
  assert.match(launcher, /--client macos/u);
  assert.match(launcher, /dev\.macos-claude-deepseek-methods/u);
  assert.match(launcher, /PROVIDER_MODE_MISMATCH/u);
  assert.match(launcher, /CENTRAL_OR_PROVIDER_OWNED_ARGUMENT/u);
  assert.match(launcher, /ALL_SCENARIOS_REQUIRES_E2E/u);
  assert.match(launcher, /SCENARIO_REQUIRES_E2E/u);
  assert.match(launcher, /TEST_FLOW_QUICK_UBUNTU2204_CONTAINER=1/u);
  assert.match(launcher, /TEST_FLOW_QUICK_SCRATCH_ROOT=\/run\/test-flow-scratch/u);
  assert.match(launcher, /TEST_FLOW_PYTHON=\/opt\/venvs\/xiaodao\/bin\/python/u);
  assert.match(launcher, /type=bind,src=\$repo_root,dst=\$repo_root,readonly/u);
  assert.match(launcher, /cache_mount\+=",readonly"/u);
  assert.match(launcher, /dst=\/run\/secrets\/image-seal\.json,readonly/u);
  assert.match(launcher, /codex-auth\.json,readonly/u);
  assert.match(launcher, /claude-settings\.json,readonly/u);
  assert.match(launcher, /--security-opt seccomp=unconfined/u);
  assert.match(launcher, /--rm --init/u);
  assert.match(launcher, /--user 0:0/u);
  assert.match(launcher, /--read-only --network bridge/u);
  assert.match(launcher, /\/private\/tmp:rw,exec,nosuid,nodev,mode=1777/u);
  assert.match(launcher, /\/run\/test-flow-scratch:rw,exec,nosuid,nodev,mode=0700/u);
  assert.doesNotMatch(launcher, /--privileged|--cap-add|--cap-drop|docker\.sock|--pid host|--network host/u);
  assert.doesNotMatch(codexService, /setpriv|codex-child-privilege-boundary/u);
  assert.match(codexE2E, /--expected-cli-version/u);
  assert.match(codexRuntime, /fs\.writeFileSync\(destination, source, \{ encoding: "utf8", flag: "wx", mode: 0o400 \}\)/u);
});

test("central Codex Quick Gates consume the provider adapter receipt and can verify a published cache without a model", () => {
  const actions = fs.readFileSync(path.join(TEST_FLOW_ROOT, "lib", "actions.mjs"), "utf8");
  const gates = JSON.parse(fs.readFileSync(path.join(TEST_FLOW_ROOT, "config", "gates.v2.json"), "utf8")).gates;
  assert.match(actions, /path\.join\(outputRoot, "adapter-receipt\.json"\)/u);
  assert.match(actions, /--verify-cache-only/u);
  assert.match(actions, /context\.planStage\.invocation_caps\.length === 0/u);
  for (const gateId of ["real.macos-codex-luna-methods", "real.macos-codex-luna-e2e"]) {
    assert.equal(gates[gateId].evidence.includes("adapter-receipt.json"), true, gateId);
    assert.equal(gates[gateId].evidence.includes("gate-receipt.json"), false, gateId);
  }
});

test("the image supplies Ubuntu 22.04 runtimes and only a BSD-stat compatibility boundary", () => {
  const dockerfile = fs.readFileSync(path.join(WSL_ROOT, "Dockerfile"), "utf8");
  const prepare = fs.readFileSync(path.join(WSL_ROOT, "prepare-image.sh"), "utf8");
  assert.match(dockerfile, /^FROM ubuntu@sha256:/mu);
  assert.match(dockerfile, /install -m 0755 \/opt\/codex\/bin\/codex \/usr\/bin\/codex/u);
  assert.match(dockerfile, /package\/cli\.js/u);
  assert.match(dockerfile, /mv \/usr\/bin\/stat \/usr\/bin\/stat\.gnu/u);
  assert.match(dockerfile, /\[ "\$1" = "-f" \].*\[ "\$2" = "%z" \]/u);
  assert.match(dockerfile, /exec \/usr\/bin\/stat\.gnu "\$@"/u);
  assert.match(dockerfile, /ln -s \/opt\/venvs\/xiaodao\/bin\/python \/usr\/bin\/python3/u);
  assert.match(dockerfile, /mv \/opt\/venvs\/logparse \/opt\/logparse\/\.venv/u);
  assert.match(dockerfile, /sys\.prefix == '\/opt\/logparse\/\.venv'/u);
  assert.match(dockerfile, /mkdir -p -m 1777 \/private\/tmp/u);
  assert.match(dockerfile, /problem-locator\.quick\.container="ubuntu22\.04-central-v1"/u);
  assert.doesNotMatch(dockerfile, /COPY .*auth|COPY .*settings/u);
  assert.match(prepare, /--network none/u);
  assert.doesNotMatch(prepare, /buildx build[^\n]*--pull/u);
  assert.match(prepare, /seal_root="\$cache_root\/quick-validation\/ubuntu2204-central"/u);
  assert.match(prepare, /\/usr\/bin\/stat -f %z/u);
});

test("container shell sources are LF-only", () => {
  for (const name of ["prepare-image.sh", "run.sh"]) {
    assert.doesNotMatch(fs.readFileSync(path.join(WSL_ROOT, name), "utf8"), /\r/u, name);
  }
});
