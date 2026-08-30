import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  canonicalJson,
  ensureDirectory,
  removeTreeWritable,
  resolveCommand,
  resolvePythonTestRuntime,
  runSync,
  sha256Bytes,
  sha256File,
  writeJsonSync,
} from "./util.mjs";
import { runProcess } from "./process.mjs";
import {
  RELEASE_BASE_IMAGE,
  RELEASE_CLAUDE_CLI_SHA256,
  RELEASE_CLAUDE_VERSION_OUTPUT,
  RELEASE_CHROME_HEADLESS_SHELL_PRODUCT,
  RELEASE_PYTHON_VERSION,
  RELEASE_UV_SHA256,
  RELEASE_UV_VERSION_OUTPUT,
  RELEASE_UVX_SHA256,
  RELEASE_UVX_VERSION_OUTPUT,
  codexLogparseRuntimeIdentity,
  dockerServerIdentity,
  externalGitIdentity,
  materializeClaudeSettings,
  packageTreeIdentity,
  sameDockerRuntimeIdentity,
} from "./release-inputs.mjs";
import { isCompleteUsage, sumUsage, TOKEN_USAGE_FORMULA, zeroUsage } from "./usage.mjs";
import {
  discoverReleaseCaseRoot,
  loadReleaseCaseInputs,
  loadReleaseCaseOracle,
} from "./release-case.mjs";
import {
  METHODS_V2_CAPTURED_FILES,
  validateMethodsV2ExecutionRecords,
} from "./methods-oracle.mjs";
import {
  buildIsolatedAgentEnvironment,
  ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY,
  ISOLATED_AGENT_ENV_POLICY_VERSION,
  ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT,
  validEnvironmentKeySummary,
} from "../runtime-support/isolated-agent-env.mjs";
import {
  SKILL_GENERATION_TRACE_SCHEMA_VERSION,
  validSkillGenerationTraceAuditReceipt,
} from "../runtime-support/isolated-agent-tool-audit.mjs";
import { projectEvidenceV2ProviderTerminalFailure } from "../runtime-support/evidence-v2-provider-terminal.mjs";
import {
  auditNoSecretLeak,
  buildPosthocBudgetReceipt,
  CODEX_LUNA_APP_SERVER_SCHEMA_TREE_SHA256,
  CODEX_LUNA_CALL_WALL_SECONDS,
  CODEX_LUNA_CONTRACT_VERSION,
  CODEX_LUNA_EQUIVALENT_USD_LIMIT,
  CODEX_LUNA_MODEL,
  CODEX_LUNA_NO_PROGRESS_SECONDS,
  CODEX_LUNA_NORMAL_CALLS,
  CODEX_LUNA_POSTHOC_EXCEPTION_ID,
  CODEX_LUNA_PERMISSION_PROFILE_VERSION,
  CODEX_LUNA_REASONING_EFFORT,
  CODEX_LUNA_SCENARIO_COUNT,
  CODEX_LUNA_STAGE_WALL_SECONDS,
  CODEX_LUNA_TOKEN_LIMIT,
  canonicalJson as canonicalCodexJson,
  collectSecretCanaries,
  normalizeCodexUsage,
  treeDigest,
  treeManifest,
  validateCodexLunaIdentity,
} from "../runtime-support/codex-luna-contract.mjs";
import {
  buildCodexLunaAccountReadRequest,
  buildCodexLunaAppServerArguments,
  buildCodexLunaAppServerEvidenceSummary,
  buildCodexLunaInitializeRequest,
  buildCodexLunaInitializedNotification,
  buildCodexLunaIsolatedConfig,
  buildCodexLunaPermissionProfileListRequest,
  buildCodexLunaSkillsListRequest,
  buildCodexLunaThreadStartRequest,
  buildCodexLunaTurnStartRequest,
  CODEX_LUNA_APP_SERVER_REQUEST_IDS,
  CODEX_LUNA_DISABLED_FEATURES,
  parseCodexLunaAppServerTranscript,
} from "../runtime-support/codex-luna-app-server.mjs";
import {
  CLAUDE_DEEPSEEK_E2E_CALLS,
  CLAUDE_DEEPSEEK_METHODS_CALLS,
  CLAUDE_DEEPSEEK_MODEL_CERT_ROLE_POOL_USD,
  validateClaudeDeepseekRoleReceipt,
} from "../quick-validation/claude-deepseek/runtime/claude-deepseek-contract.mjs";
import {
  buildEvidenceV2CoreVerdict,
  EVIDENCE_V2_CORE_RECEIPT,
} from "../../validation/evidence-v2-core.mjs";
import {
  buildEvidenceV2ModelCert,
  buildEvidenceV2ReleaseVerdict,
  EVIDENCE_V2_CORE_VERDICT_PATH,
  EVIDENCE_V2_MODEL_CERT_FILENAME,
  EVIDENCE_V2_MODEL_CERT_RECEIPT,
  EVIDENCE_V2_RELEASE_VERDICT_FILENAME,
  validateEvidenceV2ModelCert,
} from "../../validation/evidence-v2-certification.mjs";
import { validateEvidenceV2ReleaseScenarioGraph } from "../../validation/evidence-v2-scenario-oracle.mjs";

const LINUX_CLIENT_BROWSER_RUNNER_RELATIVE = "tools/test-flow/runtime-support/linux_client_browser_runner.py";
const LINUX_CLIENT_BROWSER_ARGUMENT_PROFILE = "chrome-headless-shell-for-testing-local-v1";

function pythonRuntime(repoRoot) {
  return resolvePythonTestRuntime(repoRoot);
}

function gateExecutionId(stage, gateId) {
  return `${stage.id}--${gateId}`;
}

export function pytestScratchBoundary({
  platform = process.platform,
  temporaryDirectory = os.tmpdir(),
  repoRoot = null,
  isolatedAgent = false,
  configuredWindowsDirectory = process.env.TEST_FLOW_WINDOWS_SCRATCH_ROOT ?? null,
  attemptRoot,
} = {}) {
  if (platform === "win32") {
    if (configuredWindowsDirectory !== null) {
      if (!path.isAbsolute(configuredWindowsDirectory)) {
        throw new Error("PYTEST_WINDOWS_SCRATCH_ROOT_ABSOLUTE_REQUIRED");
      }
      return path.resolve(configuredWindowsDirectory);
    }
    const systemTemporary = path.resolve(temporaryDirectory);
    if (isolatedAgent || repoRoot === null) return systemTemporary;
    const repositoryTemporary = path.resolve(repoRoot, ".tmp", "p");
    return repositoryTemporary.length <= systemTemporary.length
      ? repositoryTemporary
      : systemTemporary;
  }
  if (!attemptRoot) throw new Error("PYTEST_ATTEMPT_ROOT_REQUIRED");
  return path.resolve(attemptRoot);
}

export function pytestBaseTempPath(scratch, platform = process.platform) {
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  const absolute = pathApi.resolve(scratch);
  if (platform !== "win32" || absolute.startsWith("\\\\?\\")) return absolute;
  if (absolute.startsWith("\\\\")) return `\\\\?\\UNC\\${absolute.slice(2)}`;
  return `\\\\?\\${absolute}`;
}

function gateRoot(context, stage) {
  return context.gateRoot ?? path.join(context.attemptRoot, "payload", "stages", stage.id);
}

export function hostCapabilityProcessSpec({
  client,
  platform = process.platform,
  sourceSnapshotRoot,
  outputRoot,
  claudeEntry,
  runtimeProfileDigest,
  dockerContext = null,
  clientImageId = null,
  runId,
  hostUid = typeof process.getuid === "function" ? process.getuid() : null,
  hostGid = typeof process.getgid === "function" ? process.getgid() : null,
}) {
  const adapter = path.join(sourceSnapshotRoot, "tools", "test-flow", "adapters", "host-capability.mjs");
  const nativeClient = platform === "darwin" ? "macos" : platform === "win32" ? "windows" : "linux";
  if (client === nativeClient) {
    return {
      command: process.execPath,
      args: [
        adapter,
        "--repo-root", sourceSnapshotRoot,
        "--output-root", outputRoot,
        "--claude-entry", claudeEntry,
        "--runtime-profile-digest", runtimeProfileDigest,
        "--execution-topology", "native-host",
      ],
      cwd: sourceSnapshotRoot,
      container: null,
      resourceLabel: null,
      executionTopology: "native-host",
      clientImageId: null,
    };
  }
  if (
    platform !== "darwin"
    || client !== "linux"
    || dockerContext !== "colima"
    || !/^sha256:[a-f0-9]{64}$/.test(clientImageId ?? "")
    || !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$/.test(runId ?? "")
    || !Number.isSafeInteger(hostUid)
    || hostUid <= 0
    || !Number.isSafeInteger(hostGid)
    || hostGid < 0
  ) throw new Error("HOST_CAPABILITY_TOPOLOGY_UNSUPPORTED");
  const container = `pltf-client-cap-${runId.slice(-16)}`.replace(/[^a-zA-Z0-9_.-]/g, "-");
  const resourceLabel = `problem-locator.test-flow.run=${runId}`;
  const clientUser = `${hostUid}:${hostGid}`;
  return {
    command: "docker",
    args: [
      "--context", dockerContext,
      "run", "--name", container,
      "--label", resourceLabel,
      "--user", clientUser,
      "--env", "HOME=/client-home",
      "--pull", "never",
      "--platform", "linux/amd64",
      "--network", "none",
      "--read-only",
      "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,mode=1777,size=268435456",
      "--tmpfs", `/client-home:rw,noexec,nosuid,nodev,mode=0700,uid=${hostUid},gid=${hostGid},size=268435456`,
      "--mount", `type=bind,src=${sourceSnapshotRoot},dst=/workspace,readonly`,
      "--mount", `type=bind,src=${outputRoot},dst=/evidence`,
      clientImageId,
      "/usr/bin/node",
      "/workspace/tools/test-flow/adapters/host-capability.mjs",
      "--repo-root", "/workspace",
      "--output-root", "/evidence",
      "--claude-entry", "/opt/claude-code/cli.js",
      "--runtime-profile-digest", runtimeProfileDigest,
      "--execution-topology", "darwin-orchestrated-linux-container",
      "--client-image-id", clientImageId,
    ],
    cwd: sourceSnapshotRoot,
    container,
    resourceLabel,
    executionTopology: "darwin-orchestrated-linux-container",
    clientImageId,
    clientUser: { uid: hostUid, gid: hostGid, root: false },
  };
}

export function validHostCapabilityReceipt(receipt, {
  runtimeProfileDigest,
  client,
  executionTopology,
  clientImageId,
  clientUser = null,
}) {
  const expectedContainerUser = executionTopology === "darwin-orchestrated-linux-container";
  const user = receipt?.execution_user;
  const userValid = exactObjectKeys(user, ["uid", "gid", "root"])
    && user.root === false
    && (expectedContainerUser
      ? Number.isSafeInteger(user.uid)
        && user.uid > 0
        && Number.isSafeInteger(user.gid)
        && user.gid >= 0
        && user.uid === clientUser?.uid
        && user.gid === clientUser?.gid
      : (user.uid === null || (Number.isSafeInteger(user.uid) && user.uid > 0))
        && (user.gid === null || (Number.isSafeInteger(user.gid) && user.gid >= 0)));
  return receipt?.schema_version === 3
    && receipt.status === "PASS"
    && receipt.runtime_profile_digest === runtimeProfileDigest
    && receipt.client === client
    && receipt.execution_topology === executionTopology
    && receipt.client_image_id === clientImageId
    && /^v\d+\./.test(receipt.node_version ?? "")
    && path.isAbsolute(receipt.node_executable ?? "")
    && /^[a-f0-9]{64}$/.test(receipt.node_sha256 ?? "")
    && (client !== "linux" || receipt.architecture === "x64")
    && receipt.flat_schema === true
    && receipt.flat_call === true
    && receipt.client_dfx_absent === true
    && userValid;
}

export function frozenServerImageId(plan) {
  const imageId = plan?.release_inputs?.image?.server?.image_id ?? null;
  if (!/^sha256:[a-f0-9]{64}$/.test(imageId ?? "")) {
    throw new Error("SERVER_IMAGE_IDENTITY_MISSING");
  }
  return imageId;
}

export function validServerRuntimeIdentity(runtimeIdentity, serverImageId) {
  return runtimeIdentity?.schema_version === 1
    && runtimeIdentity.image_id === serverImageId
    && runtimeIdentity.claude?.path === "/opt/claude-code/cli.js"
    && runtimeIdentity.claude?.sha256 === RELEASE_CLAUDE_CLI_SHA256
    && runtimeIdentity.claude?.version === RELEASE_CLAUDE_VERSION_OUTPUT
    && runtimeIdentity.node?.architecture === "x64"
    && runtimeIdentity.uv?.path === "/usr/local/bin/uv"
    && runtimeIdentity.uv?.sha256 === RELEASE_UV_SHA256
    && runtimeIdentity.uv?.version === RELEASE_UV_VERSION_OUTPUT
    && runtimeIdentity.uvx?.path === "/usr/local/bin/uvx"
    && runtimeIdentity.uvx?.sha256 === RELEASE_UVX_SHA256
    && runtimeIdentity.uvx?.version === RELEASE_UVX_VERSION_OUTPUT
    && runtimeIdentity.python?.version === `Python ${RELEASE_PYTHON_VERSION}`;
}

const SERVER_CAPABILITY_BLOCKED_CODES = new Set([
  "SERVER_CAPABILITY_CONTEXT",
  "SERVER_CAPABILITY_DOCKER",
  "SERVER_CAPABILITY_OS",
  "SERVER_CAPABILITY_ARCH",
  "SERVER_CAPABILITY_IMAGE",
  "SERVER_CAPABILITY_IMAGE_PLATFORM",
  "SERVER_CAPABILITY_OFFLINE_INSTALL",
  "SERVER_CAPABILITY_RUNTIME_IDENTITY",
]);
const SERVER_CAPABILITY_FAILED_CODES = new Set([
  "SERVER_CAPABILITY_ARGUMENTS",
  "SERVER_CAPABILITY_METADATA",
  "SERVER_CAPABILITY_IMAGE_METADATA",
  "SERVER_CAPABILITY_IMAGE_IDENTITY",
  "SERVER_CAPABILITY_CONTRACT",
  "SERVER_CAPABILITY_PLATFORM_EVIDENCE",
  "SERVER_CAPABILITY_CONTAINER_RECEIPT",
  "SERVER_CAPABILITY_CONTAINER_METADATA",
  "SERVER_CAPABILITY_CONTAINER_IDENTITY",
]);

export function serverCapabilityTerminationResult(receipt, exitCode) {
  if (receipt === null || typeof receipt !== "object" || Array.isArray(receipt)
    || Object.keys(receipt).sort().join(",") !== "code,schema_version,status"
    || receipt.schema_version !== 1
    || typeof receipt.code !== "string") return null;
  if (exitCode === 2 && receipt.status === "BLOCKED" && SERVER_CAPABILITY_BLOCKED_CODES.has(receipt.code)) {
    return { status: "BLOCKED", failure_domain: "INFRA", code: receipt.code };
  }
  if (exitCode === 3 && receipt.status === "FAIL" && SERVER_CAPABILITY_FAILED_CODES.has(receipt.code)) {
    return { status: "FAIL", failure_domain: "EXTERNAL", code: receipt.code };
  }
  return null;
}

const SHA256 = /^[a-f0-9]{64}$/;
const SAFE_DOCKER_RESOURCE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;

export function dockerRuntimeBoundaryResult(plannedDocker, observedDocker, processResult = null) {
  if (sameDockerRuntimeIdentity(plannedDocker, observedDocker)) return null;
  return {
    ...(processResult ?? { elapsed_seconds: 0 }),
    status: "BLOCKED",
    failure_domain: "INFRA",
    code: "DOCKER_RUNTIME_IDENTITY_DRIFT",
  };
}

function probeDockerRuntimeBoundary(context, processResult = null) {
  return dockerRuntimeBoundaryResult(
    context.plan.release_inputs?.docker,
    dockerServerIdentity(context.options.dockerContext ?? "default"),
    processResult,
  );
}

function exactObjectKeys(value, expected) {
  return value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && Object.keys(value).sort().join("\0") === [...expected].sort().join("\0");
}

function validRedactedCapture(value) {
  return exactObjectKeys(value, ["byte_count", "sha256", "truncated"])
    && Number.isSafeInteger(value.byte_count)
    && value.byte_count >= 0
    && SHA256.test(value.sha256 ?? "")
    && value.truncated === false;
}

function validBrowserProcessTreeCleanup(value) {
  if (!exactObjectKeys(value, [
    "strategy", "session_started", "termination_reason", "term_sent",
    "kill_sent", "parent_reaped", "group_absent",
  ])
    || value.strategy !== "posix-process-group-v1"
    || typeof value.session_started !== "boolean"
    || !["NONE", "TIMEOUT", "RESIDUAL_AFTER_EXIT"].includes(value.termination_reason)
    || typeof value.term_sent !== "boolean"
    || typeof value.kill_sent !== "boolean"
    || typeof value.parent_reaped !== "boolean"
    || typeof value.group_absent !== "boolean"
    || (value.kill_sent && !value.term_sent)) return false;
  if (!value.session_started) {
    return value.termination_reason === "NONE"
      && value.term_sent === false
      && value.kill_sent === false
      && value.parent_reaped === false
      && value.group_absent === true;
  }
  if (value.termination_reason === "NONE") return value.term_sent === false && value.kill_sent === false;
  return value.term_sent === true;
}

function completedBrowserProcessTree(value) {
  return validBrowserProcessTreeCleanup(value)
    && value.session_started === true
    && value.parent_reaped === true
    && value.group_absent === true;
}

function exactZeroUsage(value) {
  return exactObjectKeys(value, [
    "schema_version", "input_tokens", "output_tokens", "cache_creation_input_tokens",
    "cache_read_input_tokens", "total_tokens", "cost_usd",
  ])
    && isCompleteUsage(value)
    && value.input_tokens === 0
    && value.output_tokens === 0
    && value.cache_creation_input_tokens === 0
    && value.cache_read_input_tokens === 0
    && value.total_tokens === 0
    && value.cost_usd === 0;
}

export function validLinuxClientBrowserCapabilityReceipt(receipt, {
  plan,
  runtimeResources,
  runnerSha256,
}) {
  const runtime = runtimeResources?.selected_client_runtime;
  const challenge = sha256Bytes(`${plan?.run_id}:${runtimeResources?.client_container}:linux-client-browser-capability-v1`);
  const expectedResult = { schema_version: 1, ok: true, capability: "headless-dom-roundtrip", challenge };
  return exactObjectKeys(receipt, [
    "schema_version", "status", "code", "kind", "topology", "run_id",
    "client_container", "client_image_id", "execution_user", "home", "browser",
    "runner", "launcher_contract", "probe", "usage_complete", "invocations", "usage",
  ])
    && receipt.schema_version === 1
    && receipt.status === "PASS"
    && receipt.code === null
    && receipt.kind === "linux-client-headless-dom-roundtrip"
    && receipt.topology === "dual-linux-containers"
    && receipt.run_id === plan?.run_id
    && receipt.client_container === runtimeResources?.client_container
    && receipt.client_image_id === runtimeResources?.client_image_id
    && canonicalJson(receipt.execution_user) === canonicalJson(runtime?.user)
    && exactObjectKeys(receipt.execution_user, ["uid", "gid", "root"])
    && Number.isSafeInteger(receipt.execution_user.uid)
    && receipt.execution_user.uid > 0
    && Number.isSafeInteger(receipt.execution_user.gid)
    && receipt.execution_user.gid >= 0
    && receipt.execution_user.root === false
    && exactObjectKeys(receipt.home, ["path", "realpath", "present", "writable"])
    && receipt.home.path === "/client-home"
    && receipt.home.realpath === "/client-home"
    && receipt.home.present === true
    && receipt.home.writable === true
    && exactObjectKeys(receipt.browser, ["status", "product", "version", "executable_sha256", "code"])
    && receipt.browser.status === "PRESENT"
    && receipt.browser.product === RELEASE_CHROME_HEADLESS_SHELL_PRODUCT
    && receipt.browser.version === plan?.release_inputs?.browser?.version
    && receipt.browser.executable_sha256 === plan?.release_inputs?.browser?.executable_sha256
    && receipt.browser.code === null
    && exactObjectKeys(receipt.runner, ["relative_path", "sha256", "argument_profile"])
    && receipt.runner.relative_path === LINUX_CLIENT_BROWSER_RUNNER_RELATIVE
    && receipt.runner.sha256 === runnerSha256
    && receipt.runner.argument_profile === LINUX_CLIENT_BROWSER_ARGUMENT_PROFILE
    && exactObjectKeys(receipt.launcher_contract, ["kind", "network_scope", "docker_exec_count", "retries"])
    && receipt.launcher_contract.kind === "docker-cli-exec-to-python-subprocess"
    && receipt.launcher_contract.network_scope === "container-loopback-only"
    && receipt.launcher_contract.docker_exec_count === 1
    && receipt.launcher_contract.retries === 0
    && exactObjectKeys(receipt.probe, [
      "origin", "challenge_sha256", "result_sha256", "launcher_exit_code", "launcher_signal",
      "browser_exit_code", "browser_signal_number", "browser_signal_name", "timed_out",
      "stdout", "stderr", "result_marker", "cleanup",
    ])
    && receipt.probe.origin === "http://127.0.0.1:18765"
    && receipt.probe.challenge_sha256 === challenge
    && receipt.probe.result_sha256 === sha256Bytes(canonicalJson(expectedResult))
    && receipt.probe.launcher_exit_code === 0
    && receipt.probe.launcher_signal === null
    && receipt.probe.browser_exit_code === 0
    && receipt.probe.browser_signal_number === null
    && receipt.probe.browser_signal_name === null
    && receipt.probe.timed_out === false
    && validRedactedCapture(receipt.probe.stdout)
    && validRedactedCapture(receipt.probe.stderr)
    && receipt.probe.result_marker === "data-result"
    && exactObjectKeys(receipt.probe.cleanup, ["http_server_stopped", "profile_removed", "process_tree"])
    && receipt.probe.cleanup.http_server_stopped === true
    && receipt.probe.cleanup.profile_removed === true
    && completedBrowserProcessTree(receipt.probe.cleanup.process_tree)
    && receipt.usage_complete === true
    && Array.isArray(receipt.invocations)
    && receipt.invocations.length === 0
    && exactZeroUsage(receipt.usage);
}

function expectedCrossJobResourceName(prefix, runId, suffix = "") {
  if (typeof runId !== "string" || runId.length === 0) return null;
  const digest = sha256Bytes(`${runId}:${suffix}`).slice(0, 16);
  return `${prefix}-${digest}${suffix ? `-${suffix}` : ""}`;
}

function expectedCrossJobServerContainer(plan, receipt) {
  const suffix = receipt?.stage_id === "journey.cross-job.publish-restart" ? "restart" : "initial";
  return expectedCrossJobResourceName("pltf-server", plan?.run_id, suffix);
}

export function validCrossJobPassRuntimeBoundary(receipt, { plan, generatedSkill }) {
  const dual = plan?.release_inputs?.topology === "darwin-orchestrated-dual-linux-containers";
  const expectedTopology = dual ? "dual-linux-containers" : "host-client";
  const serverImageId = plan?.release_inputs?.image?.server?.image_id ?? null;
  const clientImageId = plan?.release_inputs?.image?.client?.image_id ?? null;
  const generated = receipt?.generated_skill;
  const generatedValid = exactObjectKeys(generated, [
    "registration_id", "skill_name", "tree_digest", "package_digest",
    "registration_sha256", "package_tree_sha256", "combined_sha256",
    "content_tree_sha256", "generation_receipt_sha256", "source_wiki_sha256",
  ])
    && generated.registration_id === generatedSkill?.registration_id
    && generated.skill_name === generatedSkill?.skill_name
    && generated.registration_sha256 === generatedSkill?.registration_sha256
    && generated.package_tree_sha256 === generatedSkill?.package_tree_sha256
    && generated.combined_sha256 === generatedSkill?.combined_sha256
    && generated.source_wiki_sha256 === generatedSkill?.source_wiki_sha256
    && generated.generation_receipt_sha256 === generatedSkill?.generation_receipt_sha256
    && [generated.tree_digest, generated.package_digest, generated.content_tree_sha256].every((value) => SHA256.test(value ?? ""));
  if (receipt?.status !== "PASS" || receipt.topology !== expectedTopology || !generatedValid || !/^sha256:[a-f0-9]{64}$/.test(serverImageId ?? "")) return false;
  if (!exactObjectKeys(receipt.runtime_images, ["server_image_id", "client_image_id"])
    || receipt.runtime_images.server_image_id !== serverImageId
    || receipt.runtime_images.client_image_id !== (dual ? clientImageId : null)) return false;
  const resources = receipt.runtime_resources;
  if (!exactObjectKeys(resources, [
    "client_container", "server_container", "client_image_id", "server_image_id",
    "network", "selected_client_runtime",
  ])
    || !SAFE_DOCKER_RESOURCE.test(resources.server_container ?? "")
    || resources.server_image_id !== serverImageId) return false;
  const expectedServerContainer = expectedCrossJobServerContainer(plan, receipt);
  if (expectedServerContainer !== null && resources.server_container !== expectedServerContainer) return false;
  if (!dual) {
    return resources.client_container === null
      && resources.client_image_id === null
      && resources.network === null
      && resources.selected_client_runtime === null;
  }
  if (!/^sha256:[a-f0-9]{64}$/.test(clientImageId ?? "")
    || !SAFE_DOCKER_RESOURCE.test(resources.client_container ?? "")
    || !SAFE_DOCKER_RESOURCE.test(resources.network ?? "")
    || resources.client_image_id !== clientImageId) return false;
  const expectedClientContainer = expectedCrossJobResourceName("pltf-client", plan?.run_id);
  const expectedNetwork = expectedCrossJobResourceName("pltf-net", plan?.run_id);
  if ((expectedClientContainer !== null && resources.client_container !== expectedClientContainer)
    || (expectedNetwork !== null && resources.network !== expectedNetwork)) return false;
  const runtime = resources.selected_client_runtime;
  const declared = plan.release_inputs.claude?.selected_client_runtime;
  return exactObjectKeys(runtime, ["schema_version", "status", "platform", "image_id", "identity_boundary", "user", "node", "claude", "headless_shell"])
    && runtime.schema_version === 1
    && runtime.status === "PASS"
    && runtime.platform === "linux/amd64"
    && runtime.platform === declared?.platform
    && runtime.image_id === clientImageId
    && runtime.identity_boundary === "client-image-id"
    && exactObjectKeys(runtime.user, ["uid", "gid", "root"])
    && Number.isSafeInteger(runtime.user.uid)
    && runtime.user.uid > 0
    && Number.isSafeInteger(runtime.user.gid)
    && runtime.user.gid >= 0
    && runtime.user.root === false
    && exactObjectKeys(runtime.node, ["version", "architecture", "executable", "sha256"])
    && /^v\d+\./.test(runtime.node.version ?? "")
    && runtime.node.architecture === "x64"
    && path.isAbsolute(runtime.node.executable ?? "")
    && SHA256.test(runtime.node.sha256 ?? "")
    && exactObjectKeys(runtime.claude, ["version", "cli_sha256"])
    && runtime.claude.version === declared?.claude?.version
    && runtime.claude.cli_sha256 === declared?.claude?.cli_sha256
    && exactObjectKeys(runtime.headless_shell, ["product", "version", "executable_sha256"])
    && runtime.headless_shell.product === RELEASE_CHROME_HEADLESS_SHELL_PRODUCT
    && runtime.headless_shell.version === plan.release_inputs.browser?.version
    && runtime.headless_shell.executable_sha256 === plan.release_inputs.browser?.executable_sha256;
}

export function crossJobBrowserCapabilityPolicy({ topology, stageId, status, capability, capabilityValid }) {
  if (stageId !== "journey.cross-job.environment" || status !== "PASS") return true;
  return topology === "dual-linux-containers" ? capabilityValid === true : capability === null;
}

export function crossJobBrowserFailureContract(stageId) {
  const contracts = {
    "journey.cross-job.environment": { label: "capability", status: "BLOCKED", failure_domain: "INFRA" },
    "journey.cross-job.upload": { label: "upload", status: "FAIL", failure_domain: "BROWSER" },
    "journey.cross-job.diagnose": { label: "resolved-api", status: "FAIL", failure_domain: "BROWSER" },
  };
  const contract = contracts[stageId] ?? null;
  return contract === null ? null : { ...contract, path: `chrome-${contract.label}-failure.json` };
}

export function validCrossJobBrowserFailureBinding(stageId, binding) {
  const contract = crossJobBrowserFailureContract(stageId);
  return contract !== null
    && exactObjectKeys(binding, ["path", "sha256"])
    && binding.path === contract.path
    && SHA256.test(binding.sha256 ?? "");
}

function validNonRootUser(value) {
  return exactObjectKeys(value, ["uid", "gid", "root"])
    && Number.isSafeInteger(value.uid)
    && value.uid > 0
    && Number.isSafeInteger(value.gid)
    && value.gid >= 0
    && value.root === false;
}

function validFailureHome(value) {
  if (!exactObjectKeys(value, ["path", "realpath", "present", "writable"])
    || ![null, "/client-home"].includes(value.path)
    || ![null, "/client-home"].includes(value.realpath)
    || typeof value.present !== "boolean"
    || typeof value.writable !== "boolean") return false;
  if (value.path !== null && value.path !== "/client-home") return false;
  if (value.realpath !== null && value.realpath !== "/client-home") return false;
  if (value.present === false) return value.path === null && value.realpath === null && value.writable === false;
  if (value.writable === true) return value.path === "/client-home" && value.realpath === "/client-home";
  return value.path === "/client-home";
}

function validBrowserFailureLauncher(value) {
  if (!exactObjectKeys(value, ["kind", "exit_code", "signal", "encoded_signal_candidate", "candidate_attribution"])
    || value.kind !== "docker-cli-exec"
    || !(value.exit_code === null || (Number.isSafeInteger(value.exit_code) && value.exit_code >= 0))
    || !(value.signal === null || /^SIG[A-Z0-9]+$/.test(value.signal))) return false;
  const candidate = value.exit_code !== null && value.exit_code >= 129 && value.exit_code <= 192
    ? value.exit_code - 128
    : null;
  return value.encoded_signal_candidate === candidate
    && value.candidate_attribution === (candidate === null ? null : "UNCONFIRMED_POSIX_EXIT_CONVENTION")
    && !(value.exit_code !== null && value.signal !== null);
}

function validBrowserFailureWrapper(value) {
  if (value === null) return true;
  return exactObjectKeys(value, ["status", "failure_code", "cleanup"])
    && ["PASS", "ERROR"].includes(value.status)
    && (value.status === "PASS" ? value.failure_code === null : /^[A-Z][A-Z0-9_]{0,127}$/.test(value.failure_code ?? ""))
    && exactObjectKeys(value.cleanup, ["http_server_stopped", "profile_removed", "process_tree"])
    && typeof value.cleanup.http_server_stopped === "boolean"
    && typeof value.cleanup.profile_removed === "boolean"
    && validBrowserProcessTreeCleanup(value.cleanup.process_tree)
    && (value.status !== "PASS" || completedBrowserProcessTree(value.cleanup.process_tree));
}

function validBrowserFailureProcess(value) {
  if (!exactObjectKeys(value, ["started", "exit_code", "signal_number", "signal_name", "timed_out", "attribution"])) return false;
  if (value.attribution === "UNOBSERVED") {
    return [value.started, value.exit_code, value.signal_number, value.signal_name, value.timed_out].every((item) => item === null)
      || (value.started === false
        && value.exit_code === null
        && value.signal_number === null
        && value.signal_name === null
        && value.timed_out === false);
  }
  if (value.attribution === "CONFIRMED_SUBPROCESS_SIGNAL") {
    return value.started === true
      && value.exit_code === null
      && Number.isSafeInteger(value.signal_number)
      && value.signal_number > 0
      && /^SIG[A-Z0-9]+$/.test(value.signal_name ?? "")
      && value.timed_out === false;
  }
  if (value.attribution === "CONFIRMED_SUBPROCESS_EXIT_CODE") {
    return value.started === true
      && Number.isSafeInteger(value.exit_code)
      && value.exit_code >= 0
      && value.signal_number === null
      && value.signal_name === null
      && value.timed_out === false;
  }
  return value.attribution === "CONFIRMED_SUBPROCESS_TIMEOUT"
    && value.started === true
    && value.exit_code === null
    && value.signal_number === null
    && value.signal_name === null
    && value.timed_out === true;
}

function browserFailureCodeMatches(failure) {
  const { code, label, launcher, browser_process: processReceipt, wrapper, capture, client } = failure;
  const prefix = `CHROME_${label.toUpperCase()}_`;
  if (typeof code !== "string" || !code.startsWith(prefix)) return false;
  const suffix = code.slice(prefix.length);
  if (suffix.startsWith("DOCKER_EXIT_")) return suffix === `DOCKER_EXIT_${launcher.exit_code}` && launcher.exit_code !== null && launcher.exit_code !== 0;
  if (suffix.startsWith("DOCKER_SIGNAL_")) return suffix === `DOCKER_SIGNAL_${launcher.signal}` && launcher.signal !== null;
  if (suffix.startsWith("SIGNAL_")) return suffix === `SIGNAL_${processReceipt.signal_name ?? processReceipt.signal_number}` && processReceipt.signal_number !== null;
  if (suffix.startsWith("EXIT_")) return suffix === `EXIT_${processReceipt.exit_code}` && processReceipt.exit_code !== null && processReceipt.exit_code !== 0;
  if (suffix === "TIMEOUT") return processReceipt.timed_out === true;
  if (suffix === "WRAPPER_ERROR") return wrapper?.status === "ERROR";
  if (suffix === "EXECUTION_RECEIPT_INVALID") return wrapper === null && processReceipt.attribution === "UNOBSERVED";
  const successfulProcess = wrapper?.status === "PASS"
    && processReceipt.attribution === "CONFIRMED_SUBPROCESS_EXIT_CODE"
    && processReceipt.exit_code === 0;
  if (suffix === "RESULT_MISSING") return successfulProcess && capture.result_marker_present === false;
  if (["RESULT_INVALID", "EXECUTION_FAILED"].includes(suffix)) return successfulProcess && capture.result_marker_present === true;
  if (suffix !== "RUNTIME_BOUNDARY_INVALID" || !successfulProcess) return false;
  const runtimeReady = client.home?.path === "/client-home"
    && client.home?.realpath === "/client-home"
    && client.home?.present === true
    && client.home?.writable === true
    && wrapper.cleanup.http_server_stopped === true
    && wrapper.cleanup.profile_removed === true
    && completedBrowserProcessTree(wrapper.cleanup.process_tree);
  return runtimeReady === false;
}

export function validLinuxClientBrowserFailureReceipt(failure, {
  plan,
  stageId,
  status,
  failureDomain,
  code,
  clientContainer,
  runnerSha256,
}) {
  const contract = crossJobBrowserFailureContract(stageId);
  const label = contract?.label ?? null;
  const clientImageId = plan?.release_inputs?.image?.client?.image_id ?? null;
  const browser = plan?.release_inputs?.browser;
  return label !== null
    && exactObjectKeys(failure, [
      "schema_version", "status", "failure_domain", "code", "label", "topology", "run_id",
      "execution_layer", "client", "runner", "browser", "launcher", "wrapper",
      "browser_process", "capture",
    ])
    && failure.schema_version === 1
    && status === contract.status
    && failureDomain === contract.failure_domain
    && failure.status === contract.status
    && failure.failure_domain === contract.failure_domain
    && failure.code === code
    && failure.label === label
    && failure.topology === "dual-linux-containers"
    && failure.run_id === plan?.run_id
    && failure.execution_layer === "docker-exec-linux-client-wrapper"
    && exactObjectKeys(failure.client, ["container", "image_id", "user", "home"])
    && failure.client.container === clientContainer
    && failure.client.image_id === clientImageId
    && validNonRootUser(failure.client.user)
    && (failure.client.home === null || validFailureHome(failure.client.home))
    && exactObjectKeys(failure.runner, ["relative_path", "sha256", "argument_profile"])
    && failure.runner.relative_path === LINUX_CLIENT_BROWSER_RUNNER_RELATIVE
    && failure.runner.sha256 === runnerSha256
    && failure.runner.argument_profile === LINUX_CLIENT_BROWSER_ARGUMENT_PROFILE
    && exactObjectKeys(failure.browser, ["status", "product", "version", "executable_sha256", "code"])
    && failure.browser.status === "PRESENT"
    && failure.browser.product === RELEASE_CHROME_HEADLESS_SHELL_PRODUCT
    && failure.browser.version === browser?.version
    && failure.browser.executable_sha256 === browser?.executable_sha256
    && failure.browser.code === null
    && validBrowserFailureLauncher(failure.launcher)
    && validBrowserFailureWrapper(failure.wrapper)
    && validBrowserFailureProcess(failure.browser_process)
    && (failure.wrapper === null ? failure.client.home === null : validFailureHome(failure.client.home))
    && (failure.wrapper === null
      ? failure.browser_process.attribution === "UNOBSERVED"
      : failure.launcher.exit_code === 0 && failure.launcher.signal === null)
    && !(failure.wrapper?.status === "PASS" && failure.browser_process.attribution === "UNOBSERVED")
    && exactObjectKeys(failure.capture, ["browser_stdout", "browser_stderr", "launcher_stderr", "result_marker_present"])
    && validRedactedCapture(failure.capture.browser_stdout)
    && (failure.capture.browser_stderr === null || validRedactedCapture(failure.capture.browser_stderr))
    && (failure.wrapper === null ? failure.capture.browser_stderr === null : failure.capture.browser_stderr !== null)
    && validRedactedCapture(failure.capture.launcher_stderr)
    && typeof failure.capture.result_marker_present === "boolean"
    && browserFailureCodeMatches(failure);
}

function validLinuxClientBrowserCapabilityEvidence(context, receipt) {
  const capability = receipt?.browser_capability;
  const dual = context.plan?.release_inputs?.topology === "darwin-orchestrated-dual-linux-containers";
  if (!dual) return crossJobBrowserCapabilityPolicy({
    topology: receipt?.topology,
    stageId: receipt?.stage_id,
    status: receipt?.status,
    capability,
    capabilityValid: false,
  });
  const evidencePath = path.join(
    context.attemptRoot,
    "payload", "stages", "journey.cross-job.environment", "linux-client-browser-capability.json",
  );
  if (!fs.existsSync(evidencePath)) return false;
  const metadata = fs.lstatSync(evidencePath);
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.nlink !== 1) return false;
  let persisted;
  try { persisted = JSON.parse(fs.readFileSync(evidencePath, "utf8")); } catch { return false; }
  if (canonicalJson(persisted) !== canonicalJson(capability)) return false;
  const runnerPath = path.join(context.sourceSnapshotRoot, ...LINUX_CLIENT_BROWSER_RUNNER_RELATIVE.split("/"));
  const capabilityValid = fs.existsSync(runnerPath)
    && validLinuxClientBrowserCapabilityReceipt(capability, {
      plan: context.plan,
      runtimeResources: receipt.runtime_resources,
      runnerSha256: sha256File(runnerPath),
    });
  return crossJobBrowserCapabilityPolicy({
    topology: receipt?.topology,
    stageId: receipt?.stage_id,
    status: receipt?.status,
    capability,
    capabilityValid,
  });
}

function validLinuxClientBrowserFailureEvidence(context, stage, receipt) {
  const binding = receipt?.browser_failure;
  if (!validCrossJobBrowserFailureBinding(stage.id, binding)) return false;
  const stageRoot = path.join(context.attemptRoot, "payload", "stages", stage.id);
  const evidencePath = path.join(stageRoot, binding.path);
  if (!evidencePath.startsWith(`${stageRoot}${path.sep}`) || !fs.existsSync(evidencePath)) return false;
  const metadata = fs.lstatSync(evidencePath);
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.nlink !== 1 || sha256File(evidencePath) !== binding.sha256) return false;
  let failure;
  try { failure = JSON.parse(fs.readFileSync(evidencePath, "utf8")); } catch { return false; }
  const runnerPath = path.join(context.sourceSnapshotRoot, ...LINUX_CLIENT_BROWSER_RUNNER_RELATIVE.split("/"));
  return fs.existsSync(runnerPath)
    && validLinuxClientBrowserFailureReceipt(failure, {
      plan: context.plan,
      stageId: stage.id,
      status: receipt.status,
      failureDomain: receipt.failure_domain,
      code: receipt.code,
      clientContainer: expectedCrossJobResourceName("pltf-client", context.plan.run_id),
      runnerSha256: sha256File(runnerPath),
    });
}

function generatedSkillBoundary(context) {
  const receiptPath = path.join(
    context.attemptRoot,
    "payload", "stages", "real.skill-generation", "gates", "real.agent.skill-generation", "generated-skill.json",
  );
  const receipt = JSON.parse(fs.readFileSync(receiptPath, "utf8"));
  if (receipt?.schema_version !== 1 || receipt.status !== "PASS") throw new Error("GENERATED_SKILL_GATE_RECEIPT_INVALID");
  return { ...receipt, generation_receipt_sha256: sha256File(receiptPath) };
}

function generatedProductionRegistrationRoot(context) {
  const generated = generatedSkillBoundary(context);
  const root = path.join(
    context.attemptRoot,
    "payload", "stages", "real.skill-generation", "gates", "real.agent.skill-generation",
    "generated-skill", generated.registration_id,
  );
  const metadata = fs.statSync(root);
  if (!metadata.isDirectory()) throw new Error("EVIDENCE_V2_SHARED_REGISTRATION_MISSING");
  for (const name of ["registration-template.json", "package"]) {
    if (!fs.existsSync(path.join(root, name))) throw new Error("EVIDENCE_V2_SHARED_REGISTRATION_INVALID");
  }
  return root;
}

export function evidenceV2ProviderRuntimeInputs(context) {
  return Object.freeze({
    scenario: "multiple-rpc-timeouts",
    sourceRoot: context.sourceSnapshotRoot,
    sourceSnapshotDigest: context.sourceSnapshotDigest,
    coreVerdictPath: path.join(context.attemptRoot, ...EVIDENCE_V2_CORE_VERDICT_PATH.split("/")),
    registrationRoot: generatedProductionRegistrationRoot(context),
  });
}

function uniqueStrings(value, code) {
  if (!Array.isArray(value)
    || value.some((item) => typeof item !== "string" || item.length === 0)
    || value.length !== new Set(value).size) throw new Error(code);
  return value;
}

function uniqueSortedStrings(value, code) {
  uniqueStrings(value, code);
  return [...value].sort();
}

function orderedSubsequence(values, sequence) {
  let cursor = 0;
  for (const value of values) {
    const index = sequence.indexOf(value, cursor);
    if (index < 0) return false;
    cursor = index + 1;
  }
  return true;
}

function exactDirectory(directory, expectedNames) {
  const metadata = fs.lstatSync(directory);
  if (!metadata.isDirectory() || metadata.isSymbolicLink()) throw new Error("GENERATED_METHODS_DIRECTORY_INVALID");
  const resolved = path.resolve(directory);
  const real = fs.realpathSync.native(directory);
  const samePath = process.platform === "win32"
    ? resolved.toLowerCase() === real.toLowerCase()
    : resolved === real;
  if (!samePath || canonicalJson(fs.readdirSync(directory).sort()) !== canonicalJson([...expectedNames].sort())) {
    throw new Error("GENERATED_METHODS_DIRECTORY_INVALID");
  }
}

export function validateGeneratedMethodsScenarioOracle(scenarioOracle) {
  if (scenarioOracle?.oracle?.expected_status !== "RESOLVED") {
    throw new Error("GENERATED_METHODS_SCENARIO_STATUS_INVALID");
  }
  const verdicts = scenarioOracle.oracle.expected_method_verdicts;
  if (!Array.isArray(verdicts) || verdicts.length === 0
    || verdicts.some((item) => item === null || typeof item !== "object" || Array.isArray(item)
      || canonicalJson(Object.keys(item).sort()) !== canonicalJson(["semantic_id", "verdict"])
      || typeof item.semantic_id !== "string" || item.semantic_id.length === 0
      || !["CONFIRMED", "REJECTED"].includes(item.verdict))
    || verdicts.length !== new Set(verdicts.map((item) => item.semantic_id)).size
    || !verdicts.some((item) => item.verdict === "CONFIRMED")) {
    throw new Error("GENERATED_METHODS_METHOD_VERDICTS_INVALID");
  }
  return scenarioOracle.oracle;
}

export function exactGeneratedEvidenceMarker(declared, required) {
  return declared === required;
}

function generatedMethodsExpectation(context, generatedSkill, inputs, gateOracle, scenarioOracle) {
  const componentId = /^[a-z0-9][a-z0-9-]{0,127}$/;
  if (!componentId.test(generatedSkill?.registration_id ?? "") || !componentId.test(generatedSkill?.skill_name ?? "")) {
    throw new Error("GENERATED_METHODS_IDENTITY_INVALID");
  }
  const generationGateRoot = path.join(
    context.attemptRoot,
    "payload", "stages", "real.skill-generation", "gates", "real.agent.skill-generation",
  );
  const generatedRoot = path.join(generationGateRoot, "generated-skill");
  const registrationRoot = path.join(generatedRoot, generatedSkill.registration_id);
  const packageRoot = path.join(registrationRoot, "package");
  const skillRoot = path.join(packageRoot, generatedSkill.skill_name);
  exactDirectory(generatedRoot, [generatedSkill.registration_id]);
  exactDirectory(registrationRoot, ["package", "registration-template.json"]);
  exactDirectory(packageRoot, [generatedSkill.skill_name]);
  exactDirectory(skillRoot, ["SKILL.md", "methods.json", "references"]);

  const registrationPath = path.join(registrationRoot, "registration-template.json");
  const methodsPath = path.join(skillRoot, "methods.json");
  for (const filePath of [registrationPath, methodsPath]) {
    const metadata = fs.lstatSync(filePath);
    if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.nlink !== 1) {
      throw new Error("GENERATED_METHODS_FILE_INVALID");
    }
  }
  const registrationSha256 = sha256File(registrationPath);
  if (registrationSha256 !== generatedSkill.registration_sha256
    || registrationSha256 !== sha256File(inputs.registration_template_path)) {
    throw new Error("GENERATED_METHODS_REGISTRATION_DRIFT");
  }
  const registration = JSON.parse(fs.readFileSync(registrationPath, "utf8"));
  if (canonicalJson(registration) !== canonicalJson(inputs.registration_template)) {
    throw new Error("GENERATED_METHODS_REGISTRATION_DRIFT");
  }

  const packageIdentity = packageTreeIdentity(skillRoot);
  if (packageIdentity.status !== "PRESENT") throw new Error("GENERATED_METHODS_PACKAGE_INVALID");
  const packageEntries = packageIdentity.records
    .filter((entry) => entry.kind === "file")
    .map(({ path: entryPath, size, sha256 }) => ({ path: entryPath, size, sha256 }))
    .sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  const packageTreeSha256 = sha256Bytes(canonicalJson({ version: 1, entries: packageEntries }));
  const combinedSha256 = sha256Bytes(canonicalJson({
    schema_version: 1,
    registration_id: generatedSkill.registration_id,
    registration_sha256: registrationSha256,
    package_tree_sha256: packageTreeSha256,
  }));
  if (packageTreeSha256 !== generatedSkill.package_tree_sha256 || combinedSha256 !== generatedSkill.combined_sha256) {
    throw new Error("GENERATED_METHODS_PACKAGE_IDENTITY_DRIFT");
  }

  const methods = JSON.parse(fs.readFileSync(methodsPath, "utf8"));
  const expectedPackage = gateOracle.semantic_oracle?.expected_package;
  if (methods?.schema_version !== 1
    || methods.skill_name !== generatedSkill.skill_name
    || methods.source_wiki_sha256 !== expectedPackage?.source_wiki_sha256
    || methods.source_wiki_sha256 !== generatedSkill.source_wiki_sha256
    || canonicalJson(uniqueStrings(methods.required_user_inputs, "GENERATED_METHODS_INPUTS_INVALID")) !== canonicalJson(uniqueStrings(expectedPackage.required_user_inputs, "GENERATED_METHODS_INPUTS_INVALID"))
    || canonicalJson(uniqueStrings(methods.required_artifacts, "GENERATED_METHODS_ARTIFACTS_INVALID")) !== canonicalJson(uniqueStrings(expectedPackage.required_artifacts, "GENERATED_METHODS_ARTIFACTS_INVALID"))
    || canonicalJson(uniqueStrings(methods.log_derived_fields, "GENERATED_METHODS_LOG_FIELDS_INVALID")) !== canonicalJson(uniqueStrings(expectedPackage.required_log_derived_fields, "GENERATED_METHODS_LOG_FIELDS_INVALID"))
    || !Array.isArray(methods.methods)
    || methods.methods.length === 0) {
    throw new Error("GENERATED_METHODS_DOCUMENT_INVALID");
  }
  const methodFields = ["activation_markers", "evidence_markers", "id", "priority", "reference", "title"];
  methods.methods.forEach((method, index) => {
    if (method === null || typeof method !== "object" || Array.isArray(method)
      || canonicalJson(Object.keys(method).sort()) !== canonicalJson(methodFields)
      || !componentId.test(method.id ?? "")
      || typeof method.title !== "string" || method.title.trim().length === 0
      || typeof method.reference !== "string" || !method.reference.startsWith("references/")
      || !Number.isSafeInteger(method.priority) || method.priority !== index + 1) {
      throw new Error("GENERATED_METHODS_METHOD_FIELDS_INVALID");
    }
    const evidenceMarkers = uniqueStrings(method.evidence_markers, "GENERATED_METHODS_MARKERS_INVALID");
    const activationMarkers = uniqueStrings(method.activation_markers, "GENERATED_METHODS_ACTIVATION_MARKERS_INVALID");
    if (evidenceMarkers.length === 0 || activationMarkers.length === 0
      || !orderedSubsequence(activationMarkers, evidenceMarkers)) {
      throw new Error("GENERATED_METHODS_ACTIVATION_MARKERS_INVALID");
    }
  });
  const knownMethodIds = methods.methods.map((method) => method?.id);
  uniqueSortedStrings(knownMethodIds, "GENERATED_METHODS_IDS_INVALID");
  if (knownMethodIds.some((methodId) => !componentId.test(methodId))) throw new Error("GENERATED_METHODS_IDS_INVALID");

  const generatedMethods = expectedPackage.method_marker_sets.map((semantic) => {
    const semanticMarkers = uniqueSortedStrings(semantic.all_markers, "GENERATED_METHODS_ORACLE_MARKERS_INVALID");
    const semanticActivationMarkers = uniqueStrings(semantic.activation_markers, "GENERATED_METHODS_ORACLE_ACTIVATION_MARKERS_INVALID");
    const matches = methods.methods.filter((method) => (
      canonicalJson(uniqueSortedStrings(method?.evidence_markers, "GENERATED_METHODS_MARKERS_INVALID")) === canonicalJson(semanticMarkers)
      && canonicalJson(method.activation_markers) === canonicalJson(semanticActivationMarkers)
    ));
    if (matches.length !== 1) throw new Error("GENERATED_METHODS_SEMANTIC_MAPPING_INVALID");
    return {
      semantic_id: semantic.semantic_id,
      markers: semanticMarkers,
      activation_markers: semanticActivationMarkers,
      method_id: matches[0].id,
    };
  });
  if (generatedMethods.length !== methods.methods.length
    || new Set(generatedMethods.map((entry) => entry.method_id)).size !== methods.methods.length) {
    throw new Error("GENERATED_METHODS_SET_DRIFT");
  }
  validateGeneratedMethodsScenarioOracle(scenarioOracle);

  const generatedBySemanticId = new Map(generatedMethods.map((entry) => [entry.semantic_id, entry]));
  const semanticVerdicts = scenarioOracle.oracle.expected_method_verdicts.map((item) => {
    const entry = generatedBySemanticId.get(item.semantic_id);
    if (!entry) throw new Error("GENERATED_METHODS_METHOD_VERDICT_MAPPING_INVALID");
    return { method_id: entry.method_id, verdict: item.verdict };
  });
  if (semanticVerdicts.length !== generatedMethods.length) throw new Error("GENERATED_METHODS_METHOD_VERDICT_COVERAGE_INVALID");
  const methodCards = methods.methods
    .map((method) => ({
      id: method.id,
      priority: method.priority,
      evidence_markers: [...method.evidence_markers],
      activation_markers: [...method.activation_markers],
    }))
    .sort((left, right) => left.priority - right.priority || left.id.localeCompare(right.id));
  const verdictByMethodId = new Map(semanticVerdicts.map((item) => [item.method_id, item.verdict]));
  const methodVerdicts = methodCards.map((method) => ({ method_id: method.id, verdict: verdictByMethodId.get(method.id) }));
  const orderedConfirmedMethods = methodVerdicts.filter((item) => item.verdict === "CONFIRMED").map((item) => item.method_id);
  const requiredEvidenceIdentities = scenarioOracle.oracle.required_evidence_identities.map((identity) => {
    const entry = generatedBySemanticId.get(identity.semantic_id);
    if (!entry || !entry.markers.some((marker) => exactGeneratedEvidenceMarker(marker, identity.marker))) {
      throw new Error("GENERATED_METHODS_EVIDENCE_IDENTITY_MAPPING_INVALID");
    }
    return {
      method_id: entry.method_id,
      marker: identity.marker,
      identity_tokens: uniqueSortedStrings(identity.identity_tokens, "GENERATED_METHODS_EVIDENCE_IDENTITY_INVALID"),
    };
  });
  return {
    confirmedMethods: orderedConfirmedMethods,
    methodVerdicts,
    methodCards,
    methods,
    requiredEvidenceIdentities,
  };
}

export function validMethodsV2OracleEvidence(context, receipt, generatedSkill) {
  try {
    const caseRoot = discoverReleaseCaseRoot(path.join(context.repoRoot, "tests", "cases", "release"));
    const inputs = loadReleaseCaseInputs(caseRoot);
    const gateOracle = loadReleaseCaseOracle(caseRoot);
    const scenario = inputs.scenarios.find((item) => item.scenario_id === inputs.journey_scenario);
    const scenarioOracle = gateOracle.scenarios.find((item) => item.scenario_id === inputs.journey_scenario);
    if (!scenario || !scenarioOracle || receipt?.methods_v2?.schema_version !== 2) return false;
    const methodsExpectation = generatedMethodsExpectation(context, generatedSkill, inputs, gateOracle, scenarioOracle);
    const stageRoot = path.join(context.attemptRoot, "payload", "stages", "journey.cross-job.diagnose");
    const files = Object.fromEntries(Object.entries(METHODS_V2_CAPTURED_FILES).map(([key, filename]) => (
      [key, fs.readFileSync(path.join(stageRoot, filename))]
    )));
    const reviewerOutcome = JSON.parse(files.reviewer_outcome.toString("utf8"));
    validateEvidenceV2ReleaseScenarioGraph({
      sourceRoot: context.repoRoot,
      methods: methodsExpectation.methods,
      graph: JSON.parse(files.evidence_graph.toString("utf8")),
      publicMethodsResult: reviewerOutcome.methods_terminal_projection,
    });
    const validated = validateMethodsV2ExecutionRecords({
      files,
      invocations: (receipt.invocations ?? []).filter((invocation) => ["DIAGNOSE", "REVIEW"].includes(invocation?.job_type)),
      publicMethodsResult: reviewerOutcome.methods_terminal_projection,
      expected: {
        source_job_id: receipt.methods_v2.source_job_id,
        reviewer_job_id: receipt.methods_v2.reviewer_job_id,
        case_id: receipt.methods_v2.case_id,
        skill_ref: {
          id: inputs.product_registration.runtime_ref_id,
          version: inputs.product_registration.version,
          content_hash: generatedSkill.combined_sha256,
        },
        source_ids: [...scenario.driver.attachment_anchor_names].sort(),
        method_cards: methodsExpectation.methodCards,
        loaded_method_ids: methodsExpectation.methodCards.map((method) => method.id),
        method_verdicts: methodsExpectation.methodVerdicts,
        confirmed_method_ids: methodsExpectation.confirmedMethods,
        required_evidence_identities: methodsExpectation.requiredEvidenceIdentities,
      },
    });
    return sameIdentity(validated, receipt.methods_v2);
  } catch {
    return false;
  }
}

function xmlInteger(attributes, name) {
  const match = new RegExp(`(?:^|\\s)${name}="(\\d+)"`).exec(attributes);
  return match ? Number(match[1]) : 0;
}

export function parseJUnitSummary(filePath) {
  if (!fs.existsSync(filePath)) throw new Error("JUNIT_MISSING");
  const text = fs.readFileSync(filePath, "utf8");
  const aggregateRoot = /<testsuites\b([^>]*)>/.exec(text);
  const suites = [...text.matchAll(/<testsuite\b([^>]*)>/g)].map((match) => match[1]);
  const attributes = aggregateRoot && /(?:^|\s)tests="\d+"/.test(aggregateRoot[1])
    ? [aggregateRoot[1]]
    : suites;
  if (attributes.length === 0) throw new Error("JUNIT_ROOT_INVALID");
  const tests = attributes.reduce((total, value) => total + xmlInteger(value, "tests"), 0);
  const failures = attributes.reduce((total, value) => total + xmlInteger(value, "failures"), 0);
  const errors = attributes.reduce((total, value) => total + xmlInteger(value, "errors"), 0);
  const skipped = attributes.reduce((total, value) => total + xmlInteger(value, "skipped"), 0);
  if (![tests, failures, errors, skipped].every(Number.isSafeInteger) || failures + errors + skipped > tests) throw new Error("JUNIT_COUNTS_INVALID");
  return {
    schema_version: 2,
    tests,
    passed: tests - failures - errors - skipped,
    failures,
    errors,
    skipped,
    executed: tests - skipped,
  };
}

export function materializePytestSummary(stageEvidence) {
  const junitPath = path.join(stageEvidence, "pytest.xml");
  if (!fs.existsSync(junitPath)) return null;
  const summary = parseJUnitSummary(junitPath);
  writeJsonSync(path.join(stageEvidence, "pytest-summary.json"), summary);
  return summary;
}

export function materializeEvidenceV2CoreVerdict({
  sourceSnapshotDigest,
  sourceSnapshotRoot,
  gateRoot: evidenceRoot,
}) {
  const verdict = buildEvidenceV2CoreVerdict({
    sourceSnapshotDigest,
    sourceRoot: sourceSnapshotRoot,
    gateRoot: evidenceRoot,
  });
  writeJsonSync(path.join(evidenceRoot, "core-verdict.json"), verdict);
  return verdict;
}

export function materializeEvidenceV2ModelCert({
  certificationTarget,
  sourceSnapshotDigest,
  sourceSnapshotRoot,
  attemptRoot,
  gateRoot: evidenceRoot,
}) {
  const coreVerdictPath = path.join(
    attemptRoot,
    ...EVIDENCE_V2_CORE_VERDICT_PATH.split("/"),
  );
  const cert = buildEvidenceV2ModelCert({
    certificationTarget,
    sourceSnapshotDigest,
    sourceRoot: sourceSnapshotRoot,
    coreVerdictPath,
    certRoot: evidenceRoot,
  });
  writeJsonSync(path.join(evidenceRoot, EVIDENCE_V2_MODEL_CERT_FILENAME), cert);
  return cert;
}

export function materializeEvidenceV2ReleaseVerdict({
  sourceSnapshotDigest,
  sourceSnapshotRoot,
  artifactRoot,
  coreVerdictPath,
  p1ModelCertPath,
  p2ModelCertPath,
  outputRoot = artifactRoot,
}) {
  const verdict = buildEvidenceV2ReleaseVerdict({
    sourceSnapshotDigest,
    sourceRoot: sourceSnapshotRoot,
    artifactRoot,
    coreVerdictPath,
    p1ModelCertPath,
    p2ModelCertPath,
  });
  writeJsonSync(
    path.join(outputRoot, EVIDENCE_V2_RELEASE_VERDICT_FILENAME),
    verdict,
  );
  return verdict;
}

export function attachEvidenceV2ModelCert(result, {
  context,
  gate,
  gateRoot: evidenceRoot,
}) {
  if (result.status !== "PASS" || gate.result_receipt !== EVIDENCE_V2_MODEL_CERT_RECEIPT) return result;
  const certPath = path.join(evidenceRoot, EVIDENCE_V2_MODEL_CERT_FILENAME);
  if (!fs.existsSync(certPath)) {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "EVIDENCE_V2_MODEL_CERT_MISSING" };
  }
  try {
    const modelCert = JSON.parse(fs.readFileSync(certPath, "utf8"));
    const coreVerdictPath = path.join(context.attemptRoot, ...EVIDENCE_V2_CORE_VERDICT_PATH.split("/"));
    validateEvidenceV2ModelCert(modelCert, {
      certificationTarget: gate.certification_target,
      sourceSnapshotDigest: context.sourceSnapshotDigest,
      sourceRoot: context.sourceSnapshotRoot,
      coreVerdictPath,
      certRoot: evidenceRoot,
    });
    return { ...result, model_cert: modelCert };
  } catch (error) {
    return {
      ...result,
      status: "ERROR",
      failure_domain: "HARNESS",
      code: error?.code ?? "EVIDENCE_V2_MODEL_CERT_INVALID",
    };
  }
}

function evidenceV2ReleaseVerdict(context) {
  const coreVerdictPath = path.join(context.attemptRoot, ...EVIDENCE_V2_CORE_VERDICT_PATH.split("/"));
  const certPath = (stageId) => path.join(
    context.attemptRoot,
    "payload", "stages", stageId, "gates", stageId,
    EVIDENCE_V2_MODEL_CERT_FILENAME,
  );
  try {
    const verdict = materializeEvidenceV2ReleaseVerdict({
      sourceSnapshotDigest: context.sourceSnapshotDigest,
      sourceSnapshotRoot: context.sourceSnapshotRoot,
      artifactRoot: context.attemptRoot,
      coreVerdictPath,
      p1ModelCertPath: certPath("real.macos-claude-deepseek-e2e"),
      p2ModelCertPath: certPath("real.macos-codex-luna-e2e"),
      outputRoot: context.gateRoot,
    });
    return {
      status: "PASS",
      failure_domain: null,
      code: null,
      elapsed_seconds: 0,
      invocations: [],
      usage_complete: true,
      adapter_receipt: {
        schema_version: verdict.schema_version,
        receipt_type: verdict.receipt_type,
        source_snapshot_digest: verdict.source_snapshot_digest,
        model_cert_targets: verdict.model_certs.map((cert) => cert.certification_target),
      },
    };
  } catch (error) {
    return {
      status: "ERROR",
      failure_domain: "HARNESS",
      code: error?.code ?? "EVIDENCE_V2_RELEASE_VERDICT_INVALID",
      elapsed_seconds: 0,
      invocations: [],
      usage_complete: true,
    };
  }
}

export function evaluatePytestSummary(summary, { minPassed = 1, skipPolicy = "forbid-all-skipped" } = {}) {
  if (summary.executed === 0) return { status: "FAIL", failure_domain: "CONTRACT", code: "PYTEST_NO_EXECUTED_TESTS" };
  if (summary.passed < minPassed) return { status: "FAIL", failure_domain: "CONTRACT", code: "PYTEST_MIN_PASSED_NOT_MET" };
  if (skipPolicy === "forbid" && summary.skipped > 0) return { status: "FAIL", failure_domain: "CONTRACT", code: "PYTEST_SKIP_FORBIDDEN" };
  return { status: "PASS", failure_domain: null, code: null };
}

function listTestFiles(root) {
  if (!fs.existsSync(root)) return [];
  const metadata = fs.statSync(root);
  if (metadata.isFile()) return /^test_.*\.py$/.test(path.basename(root)) ? [root] : [];
  const files = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const nested = path.join(root, entry.name);
    if (entry.isDirectory()) files.push(...listTestFiles(nested));
    else if (entry.isFile() && /^test_.*\.py$/.test(entry.name)) files.push(nested);
  }
  return files;
}

function collapseSelectors(selectors) {
  const ordered = [...selectors].sort((left, right) => left.length - right.length || left.localeCompare(right));
  const kept = [];
  for (const selector of ordered) {
    if (kept.some((parent) => selector === parent || selector.startsWith(`${parent}/`))) continue;
    kept.push(selector);
  }
  return kept.sort();
}

function affectedSelectors(changedFiles) {
  const selectors = new Set();
  const add = (...values) => values.forEach((value) => selectors.add(value));
  for (const file of changedFiles) {
    if (file.startsWith("tests/deterministic/") && file.endsWith(".py")) {
      add(path.posix.basename(file).startsWith("test_") ? file : path.posix.dirname(file));
      continue;
    }
    if (file === "docs/browser-rest-api.md" || file === "schemas/v2/web-api.openapi.snapshot.json") {
      add("tests/deterministic/unit/interfaces/test_web_api.py");
    }
    if (/^(schemas|src\/problem_locator\/contracts)\//.test(file)) add("tests/deterministic/contracts");
    if (/^src\/problem_locator\/domain\//.test(file)) add("tests/deterministic/unit/domain", "tests/deterministic/integration/test_s01_contract_domain_seam.py");
    if (/^src\/problem_locator\/storage\//.test(file)) add("tests/deterministic/unit/storage", "tests/deterministic/integration");
    if (/^src\/problem_locator\/application\//.test(file)) add("tests/deterministic/unit/application", "tests/deterministic/integration");
    if (/^src\/problem_locator\/dispatch\//.test(file)) add("tests/deterministic/unit/dispatch", "tests/deterministic/integration");
    if (/^src\/problem_locator\/(interfaces|entrypoints)\//.test(file)) add("tests/deterministic/unit/interfaces", "tests/deterministic/integration");
    if (/^src\/problem_locator\/runtime\//.test(file)) add("tests/deterministic/unit/runtime", "tests/deterministic/integration");
    if (/^src\/problem_locator\/integrations\//.test(file)) add("tests/deterministic/unit/integrations", "tests/deterministic/integration");
    if (/^src\/problem_locator\/(bootstrap|__init__|__main__)\.py$/.test(file)) add("tests/deterministic/integration");
    if (/^(pyproject\.toml|uv\.lock)$/.test(file)) add("tests/deterministic/contracts", "tests/deterministic/unit", "tests/deterministic/integration");
    if (/^tests\/fixtures\//.test(file)) add("tests/deterministic/contracts", "tests/deterministic/journey");
  }
  return collapseSelectors(selectors);
}

export function planAffectedSelection(repoRoot, changedFiles) {
  const selectors = affectedSelectors(changedFiles);
  const fullRoot = path.join(repoRoot, "tests", "deterministic");
  const allTests = new Set(listTestFiles(fullRoot).map((file) => path.resolve(file)));
  const covered = new Set();
  for (const selector of selectors) {
    for (const file of listTestFiles(path.resolve(repoRoot, selector))) covered.add(path.resolve(file));
  }
  const coverage = allTests.size === 0 ? 0 : covered.size / allTests.size;
  return {
    selectors,
    covered_test_files: covered.size,
    total_test_files: allTests.size,
    coverage,
    defer_to_full: coverage >= 0.5,
  };
}

export function probeLoopbackCapability(runtime, repoRoot, environment = process.env, invoke = runSync) {
  const probe = invoke(runtime.command, [
    ...(runtime.interpreterPrefix ?? []),
    "-c",
    "import socket; server = socket.socket(socket.AF_INET, socket.SOCK_STREAM); server.bind(('127.0.0.1', 0)); server.close()",
  ], {
    cwd: repoRoot,
    env: { ...environment, PYTHONNOUSERSITE: "1" },
  });
  const output = `${probe.stderr ?? ""}\n${probe.stdout ?? ""}`;
  return {
    schema_version: 1,
    status: probe.status === 0 ? "PASS" : "BLOCKED",
    capability: "ipv4-loopback-bind",
    exit_code: probe.status,
    signal: probe.signal ?? null,
    error_code: probe.error?.code ?? null,
    failure_code: probe.status === 0
      ? null
      : /PermissionError|operation not permitted|permission denied/i.test(output)
        ? "LOOPBACK_BIND_PERMISSION_DENIED"
        : "LOOPBACK_BIND_UNAVAILABLE",
  };
}

async function pytestAction(context, stage, selectors, {
  extra = [],
  env = {},
  real = false,
  minPassed = 1,
  skipPolicy = "forbid-all-skipped",
  selection = null,
  isolatedAgent = false,
} = {}) {
  const runtime = pythonRuntime(context.repoRoot);
  if (!runtime) {
    return { status: "BLOCKED", failure_domain: "INFRA", code: "PYTHON_312_TEST_RUNTIME_MISSING", elapsed_seconds: 0 };
  }
  const expectedPython = context.runtimeProfile?.version ?? context.runtimeProfile?.python ?? null;
  if (typeof expectedPython !== "string" || !runtime.details.python_version.startsWith(`${expectedPython.split(".").slice(0, 2).join(".")}.`)) {
    return { status: "BLOCKED", failure_domain: "INFRA", code: "PYTHON_RUNTIME_PROFILE_MISMATCH", elapsed_seconds: 0 };
  }
  const stageEvidence = gateRoot(context, stage);
  ensureDirectory(stageEvidence);
  if (selectors.length === 0) {
    const summary = { schema_version: 2, tests: 0, passed: 0, failures: 0, errors: 0, skipped: 0, executed: 0, not_required: true };
    writeJsonSync(path.join(stageEvidence, "pytest-summary.json"), summary);
    return { status: "NOT_REQUIRED", failure_domain: null, code: "AFFECTED_SCOPE_EMPTY", elapsed_seconds: 0, pytest: summary, selection };
  }
  const externalScratch = process.platform === "win32";
  const scratchBoundary = pytestScratchBoundary({
    attemptRoot: context.attemptRoot,
    repoRoot: context.repoRoot,
    isolatedAgent,
  });
  if (externalScratch) ensureDirectory(scratchBoundary);
  const scratch = externalScratch
    ? fs.mkdtempSync(path.join(scratchBoundary, "p-"))
    : path.join(context.attemptRoot, "scratch", gateExecutionId(stage, context.gateId ?? stage.id));
  const pytestScratch = externalScratch ? scratch : path.join(scratch, "pytest");
  ensureDirectory(scratch);
  const processEnvironment = {
    PYTHONNOUSERSITE: "1",
    PYTHONPYCACHEPREFIX: path.join(scratch, "pycache"),
    ...env,
  };
  const loopbackEnvironment = isolatedAgent
    ? buildIsolatedAgentEnvironment({ ambient: process.env, explicit: processEnvironment })
    : process.env;
  const loopback = probeLoopbackCapability(runtime, context.repoRoot, loopbackEnvironment);
  writeJsonSync(path.join(stageEvidence, "loopback-capability.json"), loopback);
  if (loopback.status !== "PASS") {
    removeTreeWritable(scratch, scratchBoundary);
    return {
      status: "BLOCKED",
      failure_domain: "INFRA",
      code: loopback.failure_code,
      elapsed_seconds: 0,
      capability_receipt: "loopback-capability.json",
    };
  }
  const args = [
    ...runtime.prefix,
    ...selectors,
    "-q",
    "-p", "no:cacheprovider",
    `--basetemp=${pytestBaseTempPath(pytestScratch)}`,
    `--junitxml=${path.join(stageEvidence, "pytest.xml")}`,
    ...extra,
  ];
  let result;
  try {
    result = await runProcess({
      repoRoot: context.repoRoot,
      attemptRoot: context.attemptRoot,
      stage,
      command: runtime.command,
      args,
      cwd: context.repoRoot,
      env: processEnvironment,
      hardTimeoutSeconds: stage.timeout_seconds,
      noProgressSeconds: null,
      rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
      eventWriter: context.eventWriter,
      executionId: gateExecutionId(stage, context.gateId ?? stage.id),
      pollMilliseconds: context.policies.poll_milliseconds,
      progressAllowlistVersion: context.policies.progress_allowlist_version,
      environmentPolicy: isolatedAgent ? ISOLATED_AGENT_ENV_POLICY_VERSION : null,
    });
  } finally {
    removeTreeWritable(scratch, scratchBoundary);
  }
  let summary = null;
  let summaryError = null;
  try {
    summary = materializePytestSummary(stageEvidence);
  } catch (error) {
    summaryError = error;
  }
  if (result.status === "ERROR") return { ...result, failure_domain: "HARNESS", code: result.termination?.trigger ?? "PROCESS_EVIDENCE_ERROR", pytest: summary };
  if (result.status === "INCONCLUSIVE") return { ...result, failure_domain: "EXTERNAL", code: result.termination.trigger, pytest: summary };
  if (result.status !== "PASS") return { ...result, failure_domain: real ? "CONTRACT" : "PRODUCT", code: "PYTEST_FAILED", pytest: summary };
  if (summaryError !== null || summary === null) {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: summaryError?.message ?? "JUNIT_MISSING", pytest: null };
  }
  const evaluation = evaluatePytestSummary(summary, { minPassed, skipPolicy });
  return { ...result, ...evaluation, pytest: summary, selection };
}

function quoteShell(value) {
  return `'${String(value).replaceAll("'", `'"'"'`)}'`;
}

function nodeTestFiles(repoRoot, gate) {
  if (gate.test_files) return gate.test_files.map((entry) => path.join(repoRoot, entry));
  const directory = path.join(repoRoot, path.dirname(gate.test_glob));
  const excluded = new Set((gate.exclude ?? []).map((entry) => path.resolve(repoRoot, entry)));
  return fs.readdirSync(directory)
    .filter((name) => name.endsWith(".test.mjs"))
    .map((name) => path.join(directory, name))
    .filter((filePath) => !excluded.has(path.resolve(filePath)))
    .sort();
}

function parseTapSummary(filePath) {
  const text = fs.readFileSync(filePath, "utf8");
  const number = (label) => {
    const match = new RegExp(`^# ${label} (\\d+)$`, "m").exec(text);
    return match ? Number(match[1]) : null;
  };
  const tests = number("tests");
  const passed = number("pass");
  const failed = number("fail");
  const skipped = number("skipped") ?? 0;
  if (![tests, passed, failed, skipped].every(Number.isSafeInteger)) throw new Error("NODE_TEST_TAP_SUMMARY_INVALID");
  return { schema_version: 2, tests, passed, failed, skipped };
}

export function evaluateNodeTestSummary(summary, gate) {
  if (summary.passed < gate.min_passed || summary.failed > 0) {
    return { status: "FAIL", failure_domain: "HARNESS", code: "NODE_TEST_MIN_PASSED_NOT_MET" };
  }
  if (gate.python_driver === true && summary.skipped > 0) {
    return { status: "FAIL", failure_domain: "HARNESS", code: "NODE_TEST_PYTHON_DRIVER_SKIPPED" };
  }
  return { status: "PASS", failure_domain: null, code: null };
}

async function nodeTestAction(context, stage, gate) {
  const files = nodeTestFiles(context.repoRoot, gate);
  if (files.length === 0) return { status: "ERROR", failure_domain: "HARNESS", code: "NODE_TEST_SELECTION_EMPTY", elapsed_seconds: 0 };
  let environment = {};
  if (gate.python_driver === true) {
    const runtime = pythonRuntime(context.repoRoot);
    if (runtime === null || runtime.interpreterPrefix.length !== 0) {
      return { status: "BLOCKED", failure_domain: "INFRA", code: "NODE_TEST_PYTHON_DRIVER_MISSING", elapsed_seconds: 0 };
    }
    environment = { TEST_FLOW_QUICK_PYTHON: runtime.command };
  }
  const result = await runProcess({
    repoRoot: context.repoRoot,
    attemptRoot: context.attemptRoot,
    stage,
    command: process.execPath,
    args: ["--test", "--test-reporter=tap", ...files],
    cwd: context.repoRoot,
    env: environment,
    hardTimeoutSeconds: stage.timeout_seconds,
    noProgressSeconds: null,
    rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
    eventWriter: context.eventWriter,
    executionId: gateExecutionId(stage, context.gateId),
    pollMilliseconds: context.policies.poll_milliseconds,
    progressAllowlistVersion: context.policies.progress_allowlist_version,
  });
  const outputPath = path.join(context.attemptRoot, result.stdout_path);
  const tapPath = path.join(gateRoot(context, stage), "node-test.tap");
  if (fs.existsSync(outputPath)) fs.copyFileSync(outputPath, tapPath, fs.constants.COPYFILE_EXCL);
  if (result.status !== "PASS") return { ...result, failure_domain: "HARNESS", code: "NODE_TEST_FAILED" };
  let summary;
  try { summary = parseTapSummary(tapPath); } catch (error) {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: error.message };
  }
  const evaluation = evaluateNodeTestSummary(summary, gate);
  return { ...result, ...evaluation, node_test: summary };
}

async function repositoryCheck(context, stage, gate) {
  let command;
  let args;
  const scratch = path.join(context.attemptRoot, "scratch", gateExecutionId(stage, context.gateId));
  ensureDirectory(scratch);
  let externalPycacheRoot = null;
  let pycacheRoot = path.join(scratch, "pycache");
  if (gate.check === "python-compileall") {
    const runtime = pythonRuntime(context.repoRoot);
    if (!runtime) return { status: "BLOCKED", failure_domain: "INFRA", code: "PYTHON_312_TEST_RUNTIME_MISSING", elapsed_seconds: 0 };
    if (process.platform === "win32") {
      externalPycacheRoot = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-compileall-"));
      pycacheRoot = externalPycacheRoot;
    }
    command = runtime.command;
    args = [...runtime.interpreterPrefix, "-m", "compileall", "-q", ...gate.paths];
  } else if (gate.check === "uv-lock") {
    command = resolveCommand(process.env.UV ?? "uv");
    if (!command) return { status: "BLOCKED", failure_domain: "INFRA", code: "UV_REQUIRED", elapsed_seconds: 0 };
    args = ["lock", "--check", "--offline"];
  } else if (gate.check === "git-diff-check") {
    command = resolveCommand("git");
    if (!command) return { status: "BLOCKED", failure_domain: "INFRA", code: "GIT_REQUIRED", elapsed_seconds: 0 };
    args = ["diff", "--check", "HEAD"];
  } else {
    return { status: "ERROR", failure_domain: "HARNESS", code: "REPOSITORY_CHECK_UNSUPPORTED", elapsed_seconds: 0 };
  }
  let result;
  try {
    result = await runProcess({
      repoRoot: gate.check === "git-diff-check" ? context.sourceRepoRoot : context.repoRoot,
      attemptRoot: context.attemptRoot,
      stage,
      command,
      args,
      cwd: gate.check === "git-diff-check" ? context.sourceRepoRoot : context.repoRoot,
      env: {
        PYTHONNOUSERSITE: "1",
        PYTHONPYCACHEPREFIX: pycacheRoot,
      },
      hardTimeoutSeconds: stage.timeout_seconds,
      noProgressSeconds: null,
      rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
      eventWriter: context.eventWriter,
      executionId: gateExecutionId(stage, context.gateId),
      pollMilliseconds: context.policies.poll_milliseconds,
      progressAllowlistVersion: context.policies.progress_allowlist_version,
    });
  } finally {
    removeTreeWritable(scratch, context.attemptRoot);
    if (externalPycacheRoot !== null) removeTreeWritable(externalPycacheRoot, os.tmpdir());
  }
  const receipt = {
    schema_version: 2,
    check: gate.check,
    status: result.status,
    exit_code: result.exit_code,
    elapsed_seconds: result.elapsed_seconds,
    stdout_path: result.stdout_path,
    stderr_path: result.stderr_path,
  };
  writeJsonSync(path.join(gateRoot(context, stage), "repository-check.json"), receipt);
  if (result.status !== "PASS") return { ...result, failure_domain: gate.check === "git-diff-check" ? "CONTRACT" : "PRODUCT", code: `REPOSITORY_${gate.check.toUpperCase().replaceAll("-", "_")}_FAILED` };
  return { ...result, failure_domain: null, repository_check: receipt };
}

function preparedClaudeRuntime(context) {
  const entry = context.options.claudeEntry;
  const sourceSettings = context.options.claudeSettings;
  if (!entry || !path.isAbsolute(entry) || !fs.existsSync(entry)) return null;
  if (!sourceSettings || !path.isAbsolute(sourceSettings) || !fs.existsSync(sourceSettings)) return null;
  const root = path.join(context.attemptRoot, "scratch", "claude-runtime");
  const settings = path.join(root, "settings.json");
  const config = path.join(root, "config");
  ensureDirectory(config);
  if (!fs.existsSync(settings)) materializeClaudeSettings(sourceSettings, settings);
  return {
    entry,
    settings,
    config,
    environment: {
      CLAUDE_CONFIG_DIR: config,
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
    },
  };
}

function agentCommand(context, workflow = "job") {
  const runtime = preparedClaudeRuntime(context);
  if (!runtime) return null;
  const caps = context.planStage.hard_caps;
  if (!caps?.max_turns || !caps?.max_budget_usd || !caps?.hard_timeout_seconds) return null;
  const usageRoot = path.join(context.gateRoot, "model-usage");
  ensureDirectory(usageRoot);
  const skillRootArgument = workflow === "skill-generation"
    ? ` --skill-root ${quoteShell(path.join(runtime.config, "skills", "wiki-to-diagnosis-skill"))} --source-root ${quoteShell(context.repoRoot)}`
    : "";
  const maxOutputTokensArgument = caps.max_output_tokens === undefined
    ? ""
    : ` --max-output-tokens ${caps.max_output_tokens} --max-output-tokens-upper-limit ${context.runtimeProfile.claude.max_output_tokens_upper_limit}`;
  return `${quoteShell(process.execPath)} ${quoteShell(path.join(context.repoRoot, "tools", "test-flow", "runtime-support", "isolated-agent-wrapper.mjs"))} --claude-entry ${quoteShell(runtime.entry)} --settings ${quoteShell(runtime.settings)} --model ${quoteShell(context.runtimeProfile.claude.model)} --usage-root ${quoteShell(usageRoot)} --max-turns ${caps.max_turns} --max-total-tokens ${caps.max_total_tokens}${maxOutputTokensArgument} --max-budget-usd ${caps.max_budget_usd} --hard-timeout-seconds ${caps.hard_timeout_seconds} --workflow ${quoteShell(workflow)}${skillRootArgument}`;
}

function validIsolatedOutputCapReceipt(invocation) {
  const cap = invocation.effective_caps?.max_output_tokens;
  const inboundKeys = invocation.environment_policy?.inbound?.key_names ?? [];
  const claudeKeys = invocation.environment_policy?.claude_process?.key_names ?? [];
  const enforcement = invocation.hard_cap_enforcement?.max_output_tokens;
  if (inboundKeys.includes(ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY)) return false;
  if (cap === undefined) {
    return !claudeKeys.includes(ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY)
      && enforcement === undefined;
  }
  return Number.isSafeInteger(cap)
    && cap > 0
    && cap <= invocation.effective_caps.max_total_tokens
    && claudeKeys.includes(ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY)
    && enforcement === ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT;
}

export function collectIsolatedModelUsage(context, profile) {
  const usageRoot = path.join(context.gateRoot, "model-usage");
  const files = fs.existsSync(usageRoot) ? fs.readdirSync(usageRoot).filter((name) => name.endsWith(".json")).sort() : [];
  const invocations = files.map((name) => JSON.parse(fs.readFileSync(path.join(usageRoot, name), "utf8")));
  if (invocations.length === 0) throw new Error("ISOLATED_MODEL_USAGE_RECEIPT_MISSING");
  if (invocations.some((invocation) => (
    invocation?.schema_version !== 3
    || invocation.class !== "isolated-agent"
    || invocation.usage_complete !== true
    || !isCompleteUsage(invocation.usage)
    || Object.hasOwn(invocation, "observed_request_limits")
  ))) throw new Error("ISOLATED_MODEL_USAGE_RECEIPT_INVALID");
  if (invocations.some((invocation) => (
    invocation.environment_policy?.schema_version !== 1
    || invocation.environment_policy?.version !== ISOLATED_AGENT_ENV_POLICY_VERSION
    || invocation.environment_policy?.provider_auth_source !== "audited-settings-file"
    || !validEnvironmentKeySummary(invocation.environment_policy?.inbound)
    || !validEnvironmentKeySummary(invocation.environment_policy?.claude_process)
    || !validIsolatedOutputCapReceipt(invocation)
  ))) throw new Error("ISOLATED_MODEL_ENVIRONMENT_POLICY_RECEIPT_INVALID");
  const expectedWorkflow = profile === "real-skill-generation" ? "skill-generation" : "job";
  if (invocations.some((invocation) => invocation.workflow !== expectedWorkflow)) throw new Error("ISOLATED_MODEL_WORKFLOW_RECEIPT_INVALID");
  if (invocations.some((invocation) => (
    typeof invocation.terminal?.subtype !== "string"
    || typeof invocation.terminal?.is_error !== "boolean"
    || invocation.wrapper_outcome?.schema_version !== 1
    || !["PASS", "FAIL"].includes(invocation.wrapper_outcome?.status)
    || (invocation.wrapper_outcome.status === "PASS" && invocation.wrapper_outcome.code !== null)
    || (invocation.wrapper_outcome.status === "FAIL" && !/^WRAPPER_[A-Z0-9_]+$/.test(invocation.wrapper_outcome.code ?? ""))
  ))) throw new Error("ISOLATED_MODEL_TERMINAL_RECEIPT_INVALID");
  if (expectedWorkflow === "skill-generation" && invocations.some((invocation) => {
    const audit = invocation.tool_trace_audit;
    if (audit === null) return invocation.wrapper_outcome.status === "PASS";
    const passedAudit = validSkillGenerationTraceAuditReceipt(audit);
    const failedAudit = audit?.schema_version === SKILL_GENERATION_TRACE_SCHEMA_VERSION
      && audit.status === "FAIL"
      && audit.workflow === "skill-generation"
      && /^SKILL_TRACE_[A-Z0-9_]+$/.test(audit.code ?? "")
      && Object.keys(audit).sort().join("\0") === ["code", "schema_version", "status", "workflow"].join("\0");
    return invocation.wrapper_outcome.status === "PASS" ? !passedAudit : !(passedAudit || failedAudit);
  })) throw new Error("ISOLATED_MODEL_TOOL_TRACE_AUDIT_INVALID");
  if (expectedWorkflow === "job" && invocations.some((invocation) => invocation.tool_trace_audit !== null)) throw new Error("ISOLATED_MODEL_TOOL_TRACE_AUDIT_UNEXPECTED");
  const usage = sumUsage(invocations.map((invocation) => invocation.usage));
  const summary = {
    schema_version: 3,
    status: "PASS",
    usage_complete: true,
    token_formula: TOKEN_USAGE_FORMULA,
    invocations,
    usage,
  };
  writeJsonSync(path.join(context.gateRoot, "model-usage.json"), summary);
  return summary;
}

async function hostCapability(context, stage) {
  const outputRoot = gateRoot(context, stage);
  ensureDirectory(outputRoot);
  const runId = path.basename(context.attemptRoot);
  let processSpec;
  try {
    processSpec = hostCapabilityProcessSpec({
      client: context.client,
      sourceSnapshotRoot: context.sourceSnapshotRoot,
      outputRoot,
      claudeEntry: context.options.claudeEntry,
      runtimeProfileDigest: context.plan.runtime_profile_digest,
      dockerContext: context.options.dockerContext ?? null,
      clientImageId: context.plan.release_inputs?.image?.client?.image_id ?? null,
      runId,
    });
  } catch {
    return { status: "BLOCKED", failure_domain: "INFRA", code: "HOST_CAPABILITY_TOPOLOGY_UNSUPPORTED", elapsed_seconds: 0 };
  }
  const dockerBacked = processSpec.container !== null;
  if (dockerBacked) {
    const dockerBoundary = probeDockerRuntimeBoundary(context);
    if (dockerBoundary !== null) return dockerBoundary;
    context.resources.register("container", processSpec.container, processSpec.resourceLabel);
  }
  const result = await runProcess({
    repoRoot: context.repoRoot,
    attemptRoot: context.attemptRoot,
    stage,
    command: processSpec.command,
    args: processSpec.args,
    cwd: processSpec.cwd,
    hardTimeoutSeconds: stage.timeout_seconds,
    noProgressSeconds: null,
    rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
    eventWriter: context.eventWriter,
    executionId: gateExecutionId(stage, context.gateId),
    pollMilliseconds: context.policies.poll_milliseconds,
    progressAllowlistVersion: context.policies.progress_allowlist_version,
  });
  if (dockerBacked) {
    const postRunDockerBoundary = probeDockerRuntimeBoundary(context, result);
    if (postRunDockerBoundary !== null) return postRunDockerBoundary;
  }
  if (result.status === "ERROR") return { ...result, failure_domain: "HARNESS", code: result.termination?.trigger ?? "HOST_EVIDENCE_ERROR" };
  if (result.status !== "PASS") return { ...result, status: result.status === "INCONCLUSIVE" ? "INCONCLUSIVE" : "BLOCKED", failure_domain: "EXTERNAL", code: "HOST_CAPABILITY_FAILED" };
  let receipt;
  try { receipt = JSON.parse(fs.readFileSync(path.join(outputRoot, "host-capability-result.json"), "utf8")); } catch {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "HOST_CAPABILITY_RECEIPT_INVALID" };
  }
  if (!validHostCapabilityReceipt(receipt, {
    runtimeProfileDigest: context.plan.runtime_profile_digest,
    client: context.client,
    executionTopology: processSpec.executionTopology,
    clientImageId: processSpec.clientImageId,
    clientUser: processSpec.clientUser ?? null,
  })) {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "HOST_CAPABILITY_RECEIPT_INVALID" };
  }
  return { ...result, adapter_receipt: receipt };
}

async function serverLinuxCapability(context, stage, gate) {
  const outputRoot = gateRoot(context, stage);
  ensureDirectory(outputRoot);
  let serverImageId;
  try {
    serverImageId = frozenServerImageId(context.plan);
  } catch {
    return { status: "BLOCKED", failure_domain: "INFRA", code: "SERVER_IMAGE_IDENTITY_MISSING", elapsed_seconds: 0 };
  }
  const runId = path.basename(context.attemptRoot);
  const dockerBoundary = probeDockerRuntimeBoundary(context);
  if (dockerBoundary !== null) return dockerBoundary;
  const containerName = `pltf-cap-${runId.slice(-17)}`.replace(/[^a-zA-Z0-9_.-]/g, "-");
  const resourceLabel = `problem-locator.test-flow.run=${runId}`;
  context.resources.register("container", containerName, resourceLabel);
  const result = await runProcess({
    repoRoot: context.repoRoot,
    attemptRoot: context.attemptRoot,
    stage,
    command: process.execPath,
    args: [
      path.join(context.sourceSnapshotRoot, "tools", "test-flow", "adapters", "server-linux-capability.mjs"),
      "--output-root", outputRoot,
      "--docker-context", context.options.dockerContext ?? "default",
      "--image", serverImageId,
      "--repo-root", context.sourceSnapshotRoot,
      "--logparse-source", context.options.logparseSource,
      "--runtime-profile-digest", context.plan.runtime_profile_digest,
      "--model", context.runtimeProfile.claude.model,
      "--service-agent-max-turns", context.runtimeProfile.real_caps.service_agent.max_turns,
      "--service-agent-max-total-tokens", context.runtimeProfile.real_caps.service_agent.max_total_tokens,
      "--service-agent-max-budget-usd", context.runtimeProfile.real_caps.service_agent.max_budget_usd,
      "--service-agent-hard-timeout-seconds", context.runtimeProfile.real_caps.service_agent.hard_timeout_seconds,
      "--container-name", containerName,
      "--resource-label", resourceLabel,
    ],
    cwd: context.repoRoot,
    hardTimeoutSeconds: stage.timeout_seconds,
    noProgressSeconds: context.planStage.no_progress_seconds,
    rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
    eventWriter: context.eventWriter,
    executionId: gateExecutionId(stage, context.gateId),
    pollMilliseconds: context.policies.poll_milliseconds,
    progressAllowlistVersion: context.policies.progress_allowlist_version,
  });
  const postRunDockerBoundary = probeDockerRuntimeBoundary(context, result);
  if (postRunDockerBoundary !== null) return postRunDockerBoundary;
  if (result.status === "ERROR") return { ...result, failure_domain: "HARNESS", code: result.termination?.trigger ?? "SERVER_EVIDENCE_ERROR" };
  if (result.status !== "PASS") {
    let terminationReceipt = null;
    try {
      terminationReceipt = JSON.parse(fs.readFileSync(path.join(outputRoot, "server-capability-termination.json"), "utf8"));
    } catch {}
    const controlledTermination = serverCapabilityTerminationResult(terminationReceipt, result.exit_code);
    if (controlledTermination === null) {
      return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "SERVER_CAPABILITY_TERMINATION_INVALID" };
    }
    return { ...result, ...controlledTermination };
  }
  let receipt;
  let runtimeIdentity;
  let junit;
  try {
    receipt = JSON.parse(fs.readFileSync(path.join(outputRoot, "server-linux-capability-result.json"), "utf8"));
    runtimeIdentity = JSON.parse(fs.readFileSync(path.join(outputRoot, "server-runtime-identity.json"), "utf8"));
    junit = parseJUnitSummary(path.join(outputRoot, "platform-server.xml"));
  } catch {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "SERVER_CAPABILITY_RECEIPT_INVALID" };
  }
  const claims = gate.required_claims ?? [];
  const runtimeIdentityPath = path.join(outputRoot, "server-runtime-identity.json");
  const runtimeIdentityValid = validServerRuntimeIdentity(runtimeIdentity, serverImageId);
  if (receipt?.schema_version !== 3 || receipt.status !== "PASS" || receipt.runtime_profile_digest !== context.plan.runtime_profile_digest || receipt.image !== serverImageId || receipt.image_id !== serverImageId || receipt.docker_context !== (context.options.dockerContext ?? "default") || receipt.runtime_identity_sha256 !== sha256File(runtimeIdentityPath) || !runtimeIdentityValid || claims.some((claim) => receipt.claims?.[claim] !== "PASS") || Object.keys(receipt.claims ?? {}).some((claim) => !claims.includes(claim)) || junit.executed !== 3 || junit.passed !== 3 || junit.skipped !== 0) {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "SERVER_CAPABILITY_RECEIPT_INVALID" };
  }
  return { ...result, adapter_receipt: receipt, pytest: junit };
}

function publicExternalGitIdentity(identity) {
  return {
    status: identity?.status ?? null,
    root: identity?.root ?? null,
    head: identity?.head ?? null,
    clean: identity?.clean ?? false,
  };
}

function sameIdentity(expected, observed) {
  return canonicalJson(expected) === canonicalJson(observed);
}

function exactCodexCli(receipt, expected) {
  return receipt?.schema_version === 1
    && receipt.version === expected?.version
    && receipt.sha256 === expected?.sha256
    && receipt.size === expected?.size
    && receipt.platform === expected?.platform
    && receipt.architecture === expected?.architecture
    && receipt.entry_path_sha256 === expected?.entry_path_sha256
    && receipt.exact_match === true;
}

export function validCodexLogparsePreprocessingIdentity(preprocessing, { external, runtime }) {
  const expected = {
    schema_version: 1,
    git_head: external?.head ?? null,
    git_status_sha256: sha256Bytes(""),
    cli_sha256: runtime?.cli?.sha256 ?? null,
    python_sha256: runtime?.python?.resolved_sha256 ?? null,
    python_version: runtime?.python?.version ?? null,
  };
  return external?.status === "PRESENT"
    && external.clean === true
    && runtime?.status === "PRESENT"
    && sameIdentity(preprocessing?.logparse_identity, expected);
}

export function validCodexLunaExecutionIdentity(identity, { expected, runId }) {
  return expected?.status === "PASS"
    && identity?.schema_version === 1
    && identity.contract_version === CODEX_LUNA_CONTRACT_VERSION
    && identity.run_id === runId
    && identity.invocation_class === "codex-luna-agent"
    && exactCodexCli(identity.cli, expected.cli)
    && identity.model === CODEX_LUNA_MODEL
    && identity.reasoning_effort === CODEX_LUNA_REASONING_EFFORT
    && exactObjectKeys(identity.auth, [
      "schema_version", "mode", "source_sha256", "byte_count", "account_id_sha256", "access_token_sha256", "access_token_length",
      "transfer", "transmitted_fields", "withheld_fields", "credential_persisted", "refresh_policy", "auth_json_files",
    ])
    && identity.auth.schema_version === 1
    && identity.auth?.mode === "chatgpt-external-tokens"
    && identity.auth?.source_sha256 === expected.auth?.sha256
    && identity.auth?.byte_count === expected.auth?.size
    && identity.auth?.account_id_sha256 === expected.auth?.account_id_sha256
    && /^[a-f0-9]{64}$/.test(identity.auth?.access_token_sha256 ?? "")
    && Number.isSafeInteger(identity.auth?.access_token_length)
    && identity.auth.access_token_length > 0
    && identity.auth?.transfer === expected.auth?.transfer
    && sameIdentity(identity.auth?.transmitted_fields, ["access_token", "account_id"])
    && sameIdentity(identity.auth?.withheld_fields, ["refresh_token", "id_token"])
    && identity.auth?.credential_persisted === false
    && identity.auth?.auth_json_files === 0
    && identity.auth?.refresh_policy === "fail-closed-no-refresh-replay"
    && sameIdentity(identity.filesystem_sandbox, expected.filesystem_sandbox);
}

function validCodexEnvironmentAudit(value) {
  const allowedInherited = new Set([
    "LANG", "LC_ALL", "LC_CTYPE", "SHELL", "USER", "LOGNAME", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
    "PATH", "TMPDIR", "TMP", "TEMP", "NO_COLOR", "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE", "PYTHONUTF8",
  ]);
  const inherited = value?.inherited_keys ?? [];
  const stripped = value?.stripped_sensitive_key_names ?? [];
  return value?.schema_version === 1
    && value.policy === "explicit-safe-environment-v1"
    && Array.isArray(inherited)
    && sameIdentity(inherited, [...new Set(inherited)].sort())
    && inherited.every((name) => allowedInherited.has(name) && !/(?:TOKEN|KEY|SECRET|PASSWORD|AUTH|CREDENTIAL)/i.test(name))
    && Array.isArray(stripped)
    && sameIdentity(stripped, [...new Set(stripped)].sort())
    && stripped.every((name) => /(?:TOKEN|KEY|SECRET|PASSWORD|AUTH|CREDENTIAL)/i.test(name))
    && value.sensitive_values_forwarded === 0
    && value.home_isolated === true
    && value.codex_home_isolated === true
    && value.user_config_ignored === true
    && value.user_rules_ignored === true;
}

function validCodexLunaProtocolSchemaReceipt(value, { requireExperimental = false } = {}) {
  if (value?.schema_version !== 1
    || value.status !== "PASS"
    || (requireExperimental && value.experimental !== true)
    || (!requireExperimental && value.experimental !== undefined && value.experimental !== true)
    || value.file_count !== 401
    || value.tree_sha256 !== CODEX_LUNA_APP_SERVER_SCHEMA_TREE_SHA256) return false;
  if (value.manifest === undefined) return true;
  return Array.isArray(value.manifest)
    && value.manifest.length === value.file_count
    && new Set(value.manifest.map((entry) => entry?.path)).size === value.file_count
    && value.manifest.every((entry) => exactObjectKeys(entry, ["path", "size", "sha256"])
      && typeof entry.path === "string"
      && entry.path.length > 0
      && path.posix.normalize(entry.path) === entry.path
      && !entry.path.startsWith("../")
      && !entry.path.includes("/../")
      && Number.isSafeInteger(entry.size)
      && entry.size >= 0
      && /^[a-f0-9]{64}$/.test(entry.sha256 ?? ""))
    && sha256Bytes(`${canonicalCodexJson(value.manifest)}\n`) === value.tree_sha256;
}

function sameCodexLunaProtocolSchemaIdentity(left, right) {
  return left?.file_count === right?.file_count
    && left?.tree_sha256 === right?.tree_sha256;
}

function codexAppServerBaseEvidence(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  return {
    schema_version: value.schema_version,
    status: value.status,
    protocol: value.protocol,
    permission_profile: value.permission_profile,
    turn: value.turn,
  };
}

function codexAppServerOutboundReceipt(message) {
  return {
    schema_version: 1,
    method: message.method,
    id: Object.hasOwn(message, "id") ? message.id : null,
    params_sha256: sha256Bytes(canonicalCodexJson(message.params ?? null)),
  };
}

function validCodexAppServerOutbound(value, base, auth) {
  if (!Array.isArray(value) || value.length !== 9) return false;
  if (!/^[a-f0-9]{64}$/.test(base?.turn?.workspace_root_sha256 ?? "")) return false;
  const records = [
    codexAppServerOutboundReceipt(buildCodexLunaInitializeRequest()),
    codexAppServerOutboundReceipt(buildCodexLunaInitializedNotification()),
  ];
  if (!sameIdentity(value.slice(0, 2), records)) return false;
  const login = value[2];
  if (login?.schema_version !== 1
    || login.method !== "account/login/start"
    || login.id !== CODEX_LUNA_APP_SERVER_REQUEST_IDS.login
    || login.params_sha256 !== null
    || login.auth?.type !== "chatgptAuthTokens"
    || login.auth?.account_id_sha256 !== auth?.account_id_sha256
    || login.auth?.access_token_sha256 !== auth?.access_token_sha256
    || login.auth?.access_token_length !== auth?.access_token_length
    || login.auth?.credential_returned !== false) return false;
  const expectedTail = [
    ["account/read", CODEX_LUNA_APP_SERVER_REQUEST_IDS.accountRead],
    ["permissionProfile/list", CODEX_LUNA_APP_SERVER_REQUEST_IDS.permissionProfileList],
    ["skills/list", CODEX_LUNA_APP_SERVER_REQUEST_IDS.skillsList],
    ["thread/start", CODEX_LUNA_APP_SERVER_REQUEST_IDS.threadStart],
    ["turn/start", CODEX_LUNA_APP_SERVER_REQUEST_IDS.turnStart],
    ["account/logout", 7],
  ];
  return value.slice(3).every((record, index) => record?.schema_version === 1
    && record.method === expectedTail[index][0]
    && record.id === expectedTail[index][1]
    && /^[a-f0-9]{64}$/.test(record.params_sha256 ?? ""));
}

function validateCodexAppServerOutboundForContext(value, {
  workspaceRoot,
  skillPath,
  mode,
  threadId,
  prompt,
  outputSchema,
}) {
  const expected = [
    buildCodexLunaAccountReadRequest(),
    buildCodexLunaPermissionProfileListRequest({ workspaceRoot }),
    buildCodexLunaSkillsListRequest({ workspaceRoot }),
    buildCodexLunaThreadStartRequest({ workspaceRoot, mode }),
    buildCodexLunaTurnStartRequest({ threadId, prompt, workspaceRoot, skillPath, mode, outputSchema }),
  ].map(codexAppServerOutboundReceipt);
  if (!Array.isArray(value)
    || !sameIdentity(value.slice(3, 8), expected)
    || !sameIdentity(value[8], codexAppServerOutboundReceipt({ method: "account/logout", id: 7 }))) {
    throw new Error("CODEX_LUNA_APP_SERVER_OUTBOUND_INVALID");
  }
  return sha256Bytes(canonicalCodexJson(value));
}

function validCodexAppServerProcess(value, revalidated, workspacePathSha256, forbiddenReadPathSha256, auth, mode) {
  const base = codexAppServerBaseEvidence(value);
  const codexHomeManifest = value?.codex_home?.manifest;
  const codexHomeManifestValid = Array.isArray(codexHomeManifest)
    && codexHomeManifest.length > 0
    && new Set(codexHomeManifest.map((entry) => entry?.path)).size === codexHomeManifest.length
    && codexHomeManifest.every((entry) => exactObjectKeys(entry, ["path", "size", "sha256"])
      && typeof entry.path === "string"
      && entry.path.length > 0
      && path.posix.normalize(entry.path) === entry.path
      && !entry.path.startsWith("../")
      && !entry.path.includes("/../")
      && entry.path !== "auth.json"
      && !entry.path.endsWith("/auth.json")
      && Number.isSafeInteger(entry.size)
      && entry.size >= 0
      && /^[a-f0-9]{64}$/.test(entry.sha256 ?? ""))
    && value.codex_home.tree_sha256 === sha256Bytes(`${canonicalCodexJson(codexHomeManifest)}\n`)
    && codexHomeManifest.some((entry) => entry.path === "config.toml"
      && entry.size === base?.permission_profile?.byte_count
      && entry.sha256 === base?.permission_profile?.sha256);
  const forbiddenReads = value?.preflight?.forbidden_reads;
  const forbiddenReadsValid = Array.isArray(forbiddenReadPathSha256)
    && forbiddenReadPathSha256.length === 3
    && new Set(forbiddenReadPathSha256).size === forbiddenReadPathSha256.length
    && forbiddenReadPathSha256.every((digest) => /^[a-f0-9]{64}$/.test(digest ?? ""))
    && Array.isArray(forbiddenReads)
    && forbiddenReads.length === forbiddenReadPathSha256.length
    && sameIdentity(forbiddenReads.map((entry) => entry?.path_sha256), forbiddenReadPathSha256)
    && forbiddenReads.every((entry) => entry?.status === "DENIED"
      && Number.isSafeInteger(entry?.exit_code)
      && entry.exit_code !== 0);
  return base?.schema_version === 1
    && base.status === "PASS"
    && base.permission_profile?.network_enabled === false
    && base.permission_profile?.root_access === "deny"
    && base.permission_profile?.minimal_access === "read"
    && base.turn?.workspace_root_sha256 === workspacePathSha256
    && base.turn?.thread_id === revalidated?.thread_id
    && base.turn?.turn_id === revalidated?.turn_id
    && base.turn?.model === CODEX_LUNA_MODEL
    && base.turn?.reasoning_effort === CODEX_LUNA_REASONING_EFFORT
    && value.trace_sha256 === revalidated?.trace_sha256
    && value.final_sha256 === revalidated?.final_file_sha256
    && sha256Bytes(base.turn?.final_agent_message ?? "") === revalidated?.final_message_sha256
    && base.permission_profile?.sha256 === revalidated?.profile_sha256
    && base.permission_profile?.byte_count === revalidated?.profile_byte_count
    && sha256Bytes(canonicalCodexJson(base)) === revalidated?.app_server_evidence_sha256
    && value.protocol_schema_tree_sha256 === CODEX_LUNA_APP_SERVER_SCHEMA_TREE_SHA256
    && validCodexAppServerOutbound(value.outbound, base, auth)
    && sha256Bytes(canonicalCodexJson(value.outbound)) === revalidated?.outbound_sha256
    && sameIdentity(value.feature_disables, [...CODEX_LUNA_DISABLED_FEATURES])
    && sameIdentity(value.arguments, buildCodexLunaAppServerArguments())
    && value.preflight?.schema_version === 1
    && value.preflight?.status === "PASS"
    && value.preflight?.profile_id === base.permission_profile?.id
    && value.preflight?.profile_sha256 === base.permission_profile?.sha256
    && value.preflight?.workspace_path_sha256 === workspacePathSha256
    && value.preflight?.workspace_read === "PASS"
    && value.preflight?.workspace_write === (mode === "generation" ? "ALLOWED" : "DENIED")
    && value.preflight?.command_network?.status === "DENIED"
    && forbiddenReadsValid
    && value.cleanup?.schema_version === 1
    && value.cleanup?.status === "PASS"
    && value.cleanup?.logout_request_id === 7
    && value.cleanup?.process_exit_code === 0
    && value.cleanup?.process_signal === null
    && value.cleanup?.timed_out === false
    && value.cleanup?.no_progress_timed_out === false
    && value.cleanup?.stdin_closed === true
    && value.codex_home?.status === "PASS"
    && codexHomeManifestValid
    && value.codex_home?.auth_json_files === 0
    && value.codex_home?.config_sha256 === base.permission_profile?.sha256
    && value.codex_home?.tree_sha256 === revalidated?.codex_home_tree_sha256
    && sameIdentity(value.login, {
      schema_version: 1,
      method: "account/login/start",
      id: CODEX_LUNA_APP_SERVER_REQUEST_IDS.login,
      auth_type: "chatgptAuthTokens",
      account_id_sha256: auth?.account_id_sha256,
      plan_type_present: false,
      write_accepted: true,
      credential_returned: false,
    });
}

function validCodexGenerationScopeAudit(value, workspacePathSha256) {
  return exactObjectKeys(value, [
    "schema_version", "status", "command_count", "legacy_contract_accesses", "oracle_accesses", "raw_input_accesses", "workspace_root_sha256",
  ])
    && value.schema_version === 1
    && value.status === "PASS"
    && Number.isSafeInteger(value.command_count)
    && value.command_count >= 0
    && value.legacy_contract_accesses === 0
    && value.oracle_accesses === 0
    && value.raw_input_accesses === 0
    && value.workspace_root_sha256 === workspacePathSha256;
}

function validCodexDiagnosisScopeAudit(value, workspacePathSha256) {
  return exactObjectKeys(value, [
    "schema_version", "status", "command_count", "logparse_invocations", "oracle_accesses", "raw_input_accesses", "workspace_root_sha256",
  ])
    && value.schema_version === 1
    && value.status === "PASS"
    && Number.isSafeInteger(value.command_count)
    && value.command_count >= 0
    && value.logparse_invocations === 0
    && value.oracle_accesses === 0
    && value.raw_input_accesses === 0
    && value.workspace_root_sha256 === workspacePathSha256;
}

function validCodexEvidenceFile(value, expectedPath, { allowEmpty = false } = {}) {
  return exactObjectKeys(value, ["path", "size", "sha256"])
    && value.path === expectedPath
    && value.path.startsWith("payload/")
    && path.posix.normalize(value.path) === value.path
    && !value.path.includes("/../")
    && Number.isSafeInteger(value.size)
    && (allowEmpty ? value.size >= 0 : value.size > 0)
    && /^[a-f0-9]{64}$/.test(value.sha256 ?? "");
}

function validCodexPreprocessingCases(preprocessing, expected) {
  const cases = preprocessing?.cases;
  const scenarioIds = expected?.scenarioIds ?? [];
  return sameIdentity(preprocessing?.config, {
    product: "rpc-skill-feasibility",
    sha256: expected?.logparseConfigSha256,
  })
    && Array.isArray(cases)
    && cases.length === CODEX_LUNA_SCENARIO_COUNT
    && new Set(cases.map((item) => item?.scenario_id)).size === CODEX_LUNA_SCENARIO_COUNT
    && cases.every((item, index) => exactObjectKeys(item, [
      "scenario_id", "status", "parse_invocations", "target_query_invocations", "receipt_sha256", "frozen_target_logs",
    ])
      && item.scenario_id === scenarioIds[index]
      && item.status === "PASS"
      && item.parse_invocations === 1
      && item.target_query_invocations === 2
      && /^[a-f0-9]{64}$/.test(item.receipt_sha256 ?? "")
      && Array.isArray(item.frozen_target_logs)
      && item.frozen_target_logs.length === 2
      && sameIdentity(item.frozen_target_logs.map((log) => log?.label), ["client", "server"])
      && item.frozen_target_logs.every((log) => exactObjectKeys(log, ["label", "size", "sha256"])
        && Number.isSafeInteger(log.size)
        && log.size > 0
        && /^[a-f0-9]{64}$/.test(log.sha256 ?? "")));
}

function validCodexUsageReceipt(value, call) {
  return value?.schema_version === 1
    && value.invocation_id === call?.invocation_id
    && value.class === "codex-luna-agent"
    && value.workflow === call?.workflow
    && value.logical_id === call?.logical_id
    && value.effective_model === CODEX_LUNA_MODEL
    && value.effective_reasoning_effort === CODEX_LUNA_REASONING_EFFORT
    && sameIdentity(value.effective_caps, {
      max_calls: CODEX_LUNA_NORMAL_CALLS,
      call_wall_seconds: CODEX_LUNA_CALL_WALL_SECONDS,
      no_progress_seconds: CODEX_LUNA_NO_PROGRESS_SECONDS,
      stage_wall_seconds: CODEX_LUNA_STAGE_WALL_SECONDS,
      max_total_tokens_posthoc: CODEX_LUNA_TOKEN_LIMIT,
      max_equivalent_usd_posthoc: CODEX_LUNA_EQUIVALENT_USD_LIMIT,
    })
    && value.usage_complete === true
    && sameIdentity(value.usage, call?.usage)
    && Number.isSafeInteger(value.turns)
    && value.turns > 0
    && value.turns_source === "app-server-one-ephemeral-thread-one-terminal-turn-with-raw-response-usage"
    && sameIdentity(value.terminal, call?.terminal)
    && value.wrapper_outcome?.schema_version === 1
    && value.wrapper_outcome?.status === "PASS"
    && value.wrapper_outcome?.code === null
    && value.posthoc_enforcement?.schema_version === 1
    && value.posthoc_enforcement?.exception_id === CODEX_LUNA_POSTHOC_EXCEPTION_ID
    && value.posthoc_enforcement?.calls === "runner-precondition-exactly-ten-no-retry"
    && sameIdentity(value.process, call?.process);
}

export function validCodexLunaPassBoundary(bundle, expected) {
  const {
    receipt,
    ledger,
    budget,
    security,
    identity,
    skill,
    preprocessing,
    callManifest,
    usageReceipts,
    consumer,
  } = bundle ?? {};
  const calls = ledger?.calls;
  const scenarioIds = expected?.scenarioIds ?? [];
  const logicalIds = ["generate", ...scenarioIds];
  const workspacePathSha256 = [expected?.generationWorkspacePathSha256, ...(expected?.diagnosisWorkspacePathSha256 ?? [])];
  const sha = /^[a-f0-9]{64}$/;
  const revalidatedCalls = consumer?.trace_revalidation?.records;
  const consumerShape = consumer?.schema_version === 1
    && consumer.status === "PASS"
    && consumer.run_id === expected?.runId
    && consumer.trace_revalidation?.schema_version === 1
    && consumer.trace_revalidation?.status === "PASS"
    && Array.isArray(revalidatedCalls)
    && revalidatedCalls.length === CODEX_LUNA_NORMAL_CALLS
    && revalidatedCalls.every((record) => exactObjectKeys(record, [
      "invocation_id", "trace_sha256", "app_server_evidence_sha256", "thread_id", "turn_id", "usage",
      "final_message_sha256", "final_file_sha256", "profile_sha256", "profile_byte_count", "codex_home_tree_sha256", "outbound_sha256",
    ])
      && [record.trace_sha256, record.app_server_evidence_sha256, record.final_message_sha256, record.final_file_sha256,
        record.profile_sha256, record.codex_home_tree_sha256, record.outbound_sha256].every((digest) => /^[a-f0-9]{64}$/.test(digest ?? ""))
      && Number.isSafeInteger(record.profile_byte_count)
      && record.profile_byte_count > 0)
    && consumer.secret_scan?.schema_version === 1
    && consumer.secret_scan?.status === "PASS"
    && Number.isSafeInteger(consumer.secret_scan?.scanned_files)
    && consumer.secret_scan.scanned_files > 0;
  const callShape = Array.isArray(calls)
    && calls.length === CODEX_LUNA_NORMAL_CALLS
    && new Set(calls.map((call) => call?.invocation_id)).size === CODEX_LUNA_NORMAL_CALLS
    && calls.every((call, index) => call?.schema_version === 1
      && call.invocation_id === `${expected?.runId}:codex-luna:${String(index + 1).padStart(2, "0")}`
      && call.class === "codex-luna-agent"
      && call.ordinal === index + 1
      && call.logical_id === logicalIds[index]
      && call.attempt === 1
      && call.retry_allowed === false
      && call.status === "PASS"
      && call.usage_complete === true
      && typeof call.thread_id === "string"
      && call.thread_id.length > 0
      && typeof call.turn_id === "string"
      && call.turn_id.length > 0
      && call.terminal?.event === "turn.completed"
      && call.terminal?.thread_id === call.thread_id
      && call.terminal?.turn_id === call.turn_id
      && call.failure === null
      && call.process?.exit_code === 0
      && call.process?.signal === null
      && call.process?.spawn_error === null
      && call.process?.timed_out === false
      && call.process?.no_progress_timed_out === false
      && validCodexAppServerProcess(
        call.process?.app_server,
        revalidatedCalls?.[index],
        workspacePathSha256[index],
        expected?.forbiddenReadPathSha256,
        identity?.auth,
        index === 0 ? "generation" : "diagnosis",
      )
      && revalidatedCalls?.[index]?.invocation_id === call.invocation_id
      && revalidatedCalls?.[index]?.thread_id === call.thread_id
      && revalidatedCalls?.[index]?.turn_id === call.turn_id
      && sameIdentity(revalidatedCalls?.[index]?.usage, call.usage));
  const workflowShape = callShape
    && calls.every((call, index) => call.workflow === (index === 0 ? "methods-generation" : "methods-diagnosis"))
    && new Set(calls.map((call) => call.thread_id)).size === CODEX_LUNA_NORMAL_CALLS
    && new Set(calls.map((call) => call.turn_id)).size === CODEX_LUNA_NORMAL_CALLS
    && new Set(calls.map((call) => call.process.app_server.permission_profile.sha256)).size === CODEX_LUNA_NORMAL_CALLS
    && new Set(calls.map((call) => call.process.app_server.turn.workspace_root_sha256)).size === CODEX_LUNA_NORMAL_CALLS;
  const usageValid = Array.isArray(usageReceipts)
    && usageReceipts.length === CODEX_LUNA_NORMAL_CALLS
    && usageReceipts.every((value, index) => validCodexUsageReceipt(value, calls?.[index]));
  const manifestValid = callManifest?.schema_version === 1
    && callManifest.status === "PASS"
    && callManifest.run_id === expected?.runId
    && callManifest.path_base === "attempt-root"
    && Array.isArray(callManifest.records)
    && callManifest.records.length === CODEX_LUNA_NORMAL_CALLS
    && callManifest.records.every((record, index) => {
      const ordinal = String(index + 1).padStart(2, "0");
      const prefix = index === 0 ? `${ordinal}-generation` : `${ordinal}-${scenarioIds[index - 1]}`;
      const invocationId = calls?.[index]?.invocation_id;
      const finalExtension = index === 0 ? "txt" : "json";
      return exactObjectKeys(record, ["invocation_id", "workflow", "logical_id", "thread_id", "trace", "stderr", "final", "usage_receipt"])
        && record.invocation_id === invocationId
        && record.workflow === calls?.[index]?.workflow
        && record.logical_id === calls?.[index]?.logical_id
        && record.thread_id === calls?.[index]?.thread_id
        && validCodexEvidenceFile(record.trace, `${expected?.callEvidenceRoot}/traces/${prefix}.jsonl`)
        && record.trace.sha256 === revalidatedCalls?.[index]?.trace_sha256
        && validCodexEvidenceFile(record.stderr, `${expected?.callEvidenceRoot}/traces/${prefix}.stderr.txt`, { allowEmpty: true })
        && validCodexEvidenceFile(record.final, `${expected?.callEvidenceRoot}/traces/${prefix}.final.${finalExtension}`)
        && record.final.sha256 === revalidatedCalls?.[index]?.final_file_sha256
        && validCodexEvidenceFile(record.usage_receipt, `payload/model-usage/codex-luna/${invocationId?.replaceAll(":", "-")}.json`);
    });
  const generatedPackage = consumer?.generated_package;
  const generatedPackageValid = generatedPackage?.schema_version === 1
    && generatedPackage.status === "PASS"
    && generatedPackage.path === `${expected?.callEvidenceRoot}/generated-skill`
    && generatedPackage.tree_sha256 === skill?.package_tree_sha256
    && Number.isSafeInteger(generatedPackage.file_count)
    && generatedPackage.file_count > 0
    && Array.isArray(generatedPackage.files)
    && generatedPackage.files.length === generatedPackage.file_count
    && generatedPackage.files.every((file) => exactObjectKeys(file, ["path", "size", "sha256"])
      && typeof file.path === "string"
      && file.path.length > 0
      && path.posix.normalize(file.path) === file.path
      && !file.path.startsWith("../")
      && !file.path.includes("/../")
      && Number.isSafeInteger(file.size)
      && file.size > 0
      && sha.test(file.sha256 ?? ""))
    && generatedPackage.tree_sha256 === sha256Bytes(`${canonicalCodexJson(generatedPackage.files)}\n`);
  const authAudit = security?.auth_isolation;
  const securityValid = security?.schema_version === 1
    && security.status === "PASS"
    && exactObjectKeys(authAudit, [
      "schema_version", "mode", "source_sha256", "byte_count", "account_id_sha256", "access_token_sha256", "access_token_length",
      "transfer", "transmitted_fields", "withheld_fields", "credential_persisted", "refresh_policy", "auth_json_files",
    ])
    && authAudit.schema_version === 1
    && authAudit?.mode === "chatgpt-external-tokens"
    && authAudit.source_sha256 === expected?.executedCodex?.auth?.sha256
    && authAudit.byte_count === expected?.executedCodex?.auth?.size
    && authAudit.account_id_sha256 === expected?.executedCodex?.auth?.account_id_sha256
    && /^[a-f0-9]{64}$/.test(authAudit.access_token_sha256 ?? "")
    && Number.isSafeInteger(authAudit.access_token_length)
    && authAudit.access_token_length > 0
    && authAudit.transfer === expected?.executedCodex?.auth?.transfer
    && sameIdentity(authAudit.transmitted_fields, ["access_token", "account_id"])
    && sameIdentity(authAudit.withheld_fields, ["refresh_token", "id_token"])
    && authAudit.credential_persisted === false
    && authAudit.auth_json_files === 0
    && authAudit.refresh_policy === "fail-closed-no-refresh-replay"
    && validCodexLunaProtocolSchemaReceipt(security.protocol_schema)
    && security.artifact_secret_scan?.status === "PASS"
    && Number.isSafeInteger(security.artifact_secret_scan?.scanned_files)
    && security.artifact_secret_scan.scanned_files > 0
    && security.permission_profiles?.schema_version === 1
    && security.permission_profiles?.status === "PASS"
    && security.permission_profiles?.call_count === CODEX_LUNA_NORMAL_CALLS
    && security.permission_profiles?.profile_version === CODEX_LUNA_PERMISSION_PROFILE_VERSION
    && security.permission_profiles?.enforcement === "single-layer-codex-command-sandbox"
    && sameIdentity(security.permission_profiles?.call_receipts, calls?.map((call) => ({
      invocation_id: call.invocation_id,
      receipt_sha256: sha256Bytes(canonicalCodexJson(call.process.app_server.permission_profile)),
    })))
    && security.oracle_and_logparse_scope?.scenario_count === CODEX_LUNA_SCENARIO_COUNT
    && sameIdentity(security.oracle_and_logparse_scope, {
      scenario_count: CODEX_LUNA_SCENARIO_COUNT,
      all_passed: receipt?.diagnoses?.every((item) => item?.scope_audit?.status === "PASS") ?? false,
      logparse_invocations_during_diagnosis: receipt?.diagnoses?.reduce((sum, item) => sum + (item?.scope_audit?.logparse_invocations ?? NaN), 0),
      oracle_accesses: receipt?.diagnoses?.reduce((sum, item) => sum + (item?.scope_audit?.oracle_accesses ?? NaN), 0),
      raw_input_accesses: receipt?.diagnoses?.reduce((sum, item) => sum + (item?.scope_audit?.raw_input_accesses ?? NaN), 0),
    });
  const skillValid = skill?.schema_version === 1
    && skill.skill_name === "diagnose-rpc-timeout"
    && skill.methods_schema_version === 1
    && sha.test(skill.package_tree_sha256 ?? "")
    && skill.source_wiki_sha256 === expected?.wikiSha256
    && validCodexGenerationScopeAudit(skill.generation_scope_audit, expected?.generationWorkspacePathSha256)
    && skill.generation_final_sha256 === callManifest?.records?.[0]?.final?.sha256
    && skill.validator?.ok === true
    && skill.validator?.skill_name === skill.skill_name
    && skill.validator?.source_wiki_sha256 === expected?.wikiSha256
    && skill.validator?.method_count === skill.method_ids?.length
    && Number.isSafeInteger(skill.validator?.marker_count)
    && skill.validator.marker_count > 0
    && Array.isArray(skill.validator?.errors)
    && skill.validator.errors.length === 0
    && skill.validator.runtime_identity_sha256 === sha256Bytes(canonicalCodexJson(expected?.logparseRuntime))
    && skill.validator.runtime_policy === "exact-planned-logparse-python-isolated-pre-and-post-v1"
    && Array.isArray(skill.method_ids)
    && skill.method_ids.length > 0
    && Array.isArray(skill.method_activation_markers)
    && skill.method_activation_markers.length === skill.method_ids.length
    && sameIdentity(skill.method_activation_markers.map((item) => item?.method_id), skill.method_ids)
    && skill.method_activation_markers.every((item) => exactObjectKeys(item, ["method_id", "activation_markers"])
      && Array.isArray(item.activation_markers) && item.activation_markers.length > 0
      && item.activation_markers.every((marker) => typeof marker === "string" && marker.length > 0)
      && item.activation_markers.length === new Set(item.activation_markers).size)
    && skill.durable_package?.path === "generated-skill"
    && skill.durable_package?.tree_sha256 === generatedPackage?.tree_sha256
    && sameIdentity(skill.durable_package?.manifest, generatedPackage?.files);
  const diagnosesValid = Array.isArray(receipt?.diagnoses)
    && receipt.diagnoses.length === CODEX_LUNA_SCENARIO_COUNT
    && receipt.diagnoses.every((item, index) => item?.scenario_id === scenarioIds[index]
      && ["CONFIRMED", "PARTIAL", "INSUFFICIENT"].includes(item?.status)
      && item.package_tree_sha256 === skill?.package_tree_sha256
      && item.thread_id === calls?.[index + 1]?.thread_id
      && item.receipt_sha256 === preprocessing?.cases?.[index]?.receipt_sha256
      && validCodexDiagnosisScopeAudit(item.scope_audit, expected?.diagnosisWorkspacePathSha256?.[index])
      && item.result_sha256 === callManifest?.records?.[index + 1]?.final?.sha256);
  let recomputedBudget = null;
  try {
    recomputedBudget = buildPosthocBudgetReceipt({
      calls: calls ?? [],
      usageComplete: Array.isArray(calls) && calls.every((call) => call?.usage_complete === true),
    });
  } catch {}
  const budgetValid = budget?.schema_version === 1
    && recomputedBudget !== null
    && sameIdentity(budget, recomputedBudget)
    && budget.status === "PASS_WITH_WARNINGS"
    && budget.exception_id === CODEX_LUNA_POSTHOC_EXCEPTION_ID;
  return validCodexLunaExecutionIdentity(identity, { expected: expected?.executedCodex, runId: expected?.runId })
    && validCodexLunaProtocolSchemaReceipt(identity?.protocol_schema, { requireExperimental: true })
    && sameCodexLunaProtocolSchemaIdentity(identity?.protocol_schema, security?.protocol_schema)
    && sameIdentity(identity?.auth, security?.auth_isolation)
    && validCodexEnvironmentAudit(identity?.environment)
    && sameIdentity(identity?.environment, security?.environment)
    && sameIdentity(identity?.model_shell_environment, {
      inherit: "none",
      set_keys: ["HOME", "LANG", "PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE"],
      auth_environment_available: false,
      home_is_workspace_local: true,
    })
    && sameIdentity(identity?.meta_skill, { name: "wiki-to-diagnosis-skill", tree_sha256: expected?.metaSkillTreeSha256 })
    && sameIdentity(identity?.wiki, { sha256: expected?.wikiSha256, size: expected?.wikiSize })
    && sameIdentity(identity?.scenarios, scenarioIds)
    && sameIdentity(identity?.validator_runtime, {
      policy: "exact-planned-logparse-python-isolated-pre-and-post-v1",
      identity_sha256: sha256Bytes(canonicalCodexJson(expected?.logparseRuntime)),
      python_entry_path_sha256: expected?.validatorPythonPathSha256,
    })
    && validCodexLogparsePreprocessingIdentity(preprocessing, { external: expected?.external, runtime: expected?.logparseRuntime })
    && preprocessing?.schema_version === 1
    && preprocessing.status === "PASS"
    && preprocessing.case_count === CODEX_LUNA_SCENARIO_COUNT
    && preprocessing.totals?.parse_invocations === CODEX_LUNA_SCENARIO_COUNT
    && preprocessing.totals?.target_query_invocations === CODEX_LUNA_SCENARIO_COUNT * 2
    && preprocessing.totals?.diagnosis_invocations === 0
    && validCodexPreprocessingCases(preprocessing, expected)
    && ledger?.schema_version === 1
    && ledger.run_id === expected?.runId
    && ledger.invocation_class === "codex-luna-agent"
    && ledger.expected_calls === CODEX_LUNA_NORMAL_CALLS
    && ledger.retry_policy === "NONE"
    && consumerShape
    && workflowShape
    && usageValid
    && manifestValid
    && budgetValid
    && securityValid
    && generatedPackageValid
    && skillValid
    && receipt?.schema_version === 1
    && receipt.run_id === expected?.runId
    && receipt.status === "PASS_WITH_WARNINGS"
    && receipt.invocation_class === "codex-luna-agent"
    && receipt.model === CODEX_LUNA_MODEL
    && receipt.reasoning_effort === CODEX_LUNA_REASONING_EFFORT
    && sameIdentity(receipt.cli_identity, identity.cli)
    && receipt.call_contract?.expected === CODEX_LUNA_NORMAL_CALLS
    && receipt.call_contract?.actual === CODEX_LUNA_NORMAL_CALLS
    && receipt.call_contract?.generation === 1
    && receipt.call_contract?.diagnosis === CODEX_LUNA_SCENARIO_COUNT
    && receipt.call_contract?.retries === 0
    && sameIdentity(receipt.generated_skill, skill)
    && skill.generation_thread_id === calls?.[0]?.thread_id
    && diagnosesValid
    && receipt.posthoc_budget?.exception_id === CODEX_LUNA_POSTHOC_EXCEPTION_ID
    && receipt.posthoc_budget?.status === budget.status
    && sameIdentity(receipt.posthoc_budget?.aggregate, budget.aggregate)
    && sameIdentity(receipt.posthoc_budget?.checks, budget.checks)
    && receipt.security_audit?.status === security.status;
}

function codexUsage(value) {
  return {
    schema_version: 1,
    input_tokens: Number(value?.input_tokens ?? 0),
    output_tokens: Number(value?.output_tokens ?? 0),
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    total_tokens: Number(value?.input_tokens ?? 0) + Number(value?.output_tokens ?? 0),
    cost_usd: Number(value?.equivalent_usd_upper_bound ?? 0),
  };
}

function releaseWikiPath(sourceSnapshotRoot) {
  const root = path.join(sourceSnapshotRoot, "tests", "cases", "release");
  const cases = fs.readdirSync(root, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && fs.existsSync(path.join(root, entry.name, "case.json")))
    .map((entry) => path.join(root, entry.name));
  if (cases.length !== 1) throw new Error("RELEASE_CASE_SELECTION");
  const descriptor = JSON.parse(fs.readFileSync(path.join(cases[0], "case.json"), "utf8"));
  if (typeof descriptor.input_wiki !== "string" || descriptor.input_wiki.length === 0) throw new Error("RELEASE_CASE_WIKI_INVALID");
  const wiki = path.resolve(cases[0], descriptor.input_wiki);
  if (!wiki.startsWith(`${cases[0]}${path.sep}`) || !fs.existsSync(wiki)) throw new Error("RELEASE_CASE_WIKI_INVALID");
  return wiki;
}

function releaseCodexScenarioIds(casesRoot) {
  const ids = fs.readdirSync(casesRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && fs.existsSync(path.join(casesRoot, entry.name, "case.json")))
    .map((entry) => {
      const value = JSON.parse(fs.readFileSync(path.join(casesRoot, entry.name, "case.json"), "utf8"));
      if (value?.scenario_id !== entry.name) throw new Error("CODEX_LUNA_SCENARIO_ID_INVALID");
      return entry.name;
    })
    .sort();
  if (ids.length !== CODEX_LUNA_SCENARIO_COUNT || new Set(ids).size !== ids.length) throw new Error("CODEX_LUNA_SCENARIO_SET_INVALID");
  return ids;
}

function codexEvidenceFile(filePath, relativePath) {
  const metadata = fs.lstatSync(filePath);
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.nlink !== 1) throw new Error("CODEX_LUNA_CALL_ARTIFACT_INVALID");
  return { path: relativePath, size: metadata.size, sha256: sha256File(filePath) };
}

function unwrapCodexLunaSanitizedTrace(traceText) {
  if (typeof traceText !== "string" || traceText.trim().length === 0) throw new Error("CODEX_LUNA_TRACE_EMPTY");
  return traceText.split(/\r?\n/).filter((line) => line.length > 0).map((line, index) => {
    let envelope;
    try { envelope = JSON.parse(line); } catch { throw new Error("CODEX_LUNA_TRACE_ENVELOPE_INVALID"); }
    if (!exactObjectKeys(envelope, ["schema_version", "seq", "direction", "message"])
      || envelope.schema_version !== 1
      || envelope.seq !== index + 1
      || envelope.direction !== "server_to_client"
      || envelope.message === null
      || typeof envelope.message !== "object"
      || Array.isArray(envelope.message)) {
      throw new Error("CODEX_LUNA_TRACE_ENVELOPE_INVALID");
    }
    return envelope.message;
  });
}

function codexLunaConsumerSecretCanaries(authSource) {
  const values = new Set(collectSecretCanaries(authSource, process.env));
  let auth = null;
  try { auth = JSON.parse(fs.readFileSync(authSource, "utf8")); } catch {}
  for (const token of [auth?.tokens?.access_token, auth?.tokens?.id_token]) {
    if (typeof token !== "string") continue;
    const payload = token.split(".")[1];
    if (!payload) continue;
    try {
      const decoded = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
      const visit = (value) => {
        if (typeof value === "string" && value.length >= 8) values.add(value);
        else if (Array.isArray(value)) value.forEach(visit);
        else if (value !== null && typeof value === "object") Object.values(value).forEach(visit);
      };
      visit(decoded);
    } catch {}
  }
  return [...values].sort((left, right) => right.length - left.length);
}

function codexLunaCallContext({ workRoot, privateRoot, scenarioIds, call, index }) {
  const generation = index === 0;
  const workspace = generation
    ? path.join(workRoot, "generation")
    : path.join(workRoot, "diagnoses", scenarioIds[index - 1]);
  const skillName = generation ? "wiki-to-diagnosis-skill" : "diagnose-rpc-timeout";
  const skillPath = path.join(workspace, ".agents", "skills", skillName, "SKILL.md");
  const relativeCodexHome = call?.process?.app_server?.codex_home?.relative_path;
  if (typeof relativeCodexHome !== "string"
    || relativeCodexHome.length === 0
    || path.posix.normalize(relativeCodexHome) !== relativeCodexHome
    || relativeCodexHome === ".."
    || relativeCodexHome.startsWith("../")
    || path.posix.isAbsolute(relativeCodexHome)) {
    throw new Error("CODEX_LUNA_CODEX_HOME_RECEIPT_INVALID");
  }
  const codexHome = path.resolve(privateRoot, ...relativeCodexHome.split("/"));
  const relative = path.relative(path.resolve(privateRoot), codexHome);
  if (relative === "" || relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new Error("CODEX_LUNA_CODEX_HOME_RECEIPT_INVALID");
  }
  if (call.process.app_server.codex_home.path_sha256 !== sha256Bytes(codexHome)) {
    throw new Error("CODEX_LUNA_CODEX_HOME_RECEIPT_INVALID");
  }
  return { workspace, skillPath, codexHome, mode: generation ? "generation" : "diagnosis" };
}

function codexLunaGenerationPrompt() {
  return `使用 $wiki-to-diagnosis-skill，把 input/wiki.md 转换成一个名为 diagnose-rpc-timeout 的定位 Skill，并写入 generated/diagnose-rpc-timeout。

要求：
- 人工 Wiki 是唯一业务事实源，不得修改。
- 开始生成前，先完整读取 input/wiki.md 和 runtime/source-wiki-identity.json；identity 是 input/wiki.md 的闭合 schema-v2 投影，不得修改、重算或猜测其中任何值。
- 将 identity.sha256 原样写入 methods.json 的 source_wiki_sha256。
- identity.log_templates 是完整性清单：固定文件 references/source-log-templates.md 必须依次且仅包含标题行 # Source log templates、一个空行、起始 text 代码围栏、按数组顺序逐字写入且每项一行的全部模板、结束代码围栏和最终换行；不得重排、去重或添加其他内容。
- methods.json 的 shared_references[0] 必须是 references/source-log-templates.md；清单只用于完整性核对，不得作为 Wiki 以外的业务事实源。
- 只生成 methods-v1 输出合同允许的 SKILL.md、methods.json 和 references/*.md；不生成旧版 manifest、GenerationSpec、README 或测试框架。
- 完整保留 Wiki 声明的用户参数、日志附件和日志派生字段；不得把日志字段改成用户参数。
- 生成物消费 request.json、冻结的 target_logs 与 receipt，诊断时不能再次调用 Logparse。
- 检查全部正向证据；每个原因、每次独立事件分别输出 evidence，并保留来源整行、精确行号和同源 identity_tokens。
- Wiki 明确列出的原因决定方法边界；同一原因的不同日志是证据分支，不得另拆方法。
- 完成后执行元 Skill 自带的 validate_generated_skill.py；只有 PASS 才结束。`;
}

function codexLunaDiagnosisPrompt(scenarioId, receiptSha256) {
  return `使用 $diagnose-rpc-timeout 定位 input/request.json 中的问题。

输入边界：
- Logparse 已完成；只读取 input/request.json、input/target_logs.json 列出的 evidence 日志和 input/logparse-receipt.json。
- 不调用 Logparse，不读取工作区以外路径，不查找 raw、case.json、oracle 或预期答案。
- 检查 service/API 范围内所有相关调用和全部正向证据，不能在第一条命中后停止。
- 每个原因、每次独立调用分别输出一条 evidence；证据不足以证明同一次调用时不得合并。
- sources 必须给出 source_id、从 1 开始的精确 line_number、该行 marker 和完整冻结日志原文 line。
- identity_tokens 必须原样来自本条 evidence 的 sources；候选方法没有正向日志时不得编造 evidence。
- 最终只输出符合 input/diagnosis-result.schema.json 的 JSON，文字字段使用自然中文。
- scenario_id 必须是 ${scenarioId}。
- logparse_receipt_sha256 必须是 ${receiptSha256}。`;
}

function buildCodexLunaCallManifest({
  attemptRoot,
  outputRoot,
  usageRoot,
  workRoot,
  privateRoot,
  authSource,
  runId,
  scenarioIds,
  preprocessing,
  ledger,
}) {
  const tracesRoot = path.join(outputRoot, "traces");
  const logicalIds = ["generate", ...scenarioIds];
  const expectedTraceFiles = [];
  const expectedUsageFiles = [];
  const usageReceipts = [];
  const canaries = codexLunaConsumerSecretCanaries(authSource);
  const traceRevalidation = [];
  const records = ledger.calls.map((call, index) => {
    const ordinal = String(index + 1).padStart(2, "0");
    const prefix = index === 0 ? `${ordinal}-generation` : `${ordinal}-${scenarioIds[index - 1]}`;
    const traceName = `${prefix}.jsonl`;
    const stderrName = `${prefix}.stderr.txt`;
    const finalName = index === 0 ? `${prefix}.final.txt` : `${prefix}.final.json`;
    expectedTraceFiles.push(traceName, stderrName, finalName);
    if (call.trace !== `traces/${traceName}` || call.logical_id !== logicalIds[index]) throw new Error("CODEX_LUNA_CALL_TRACE_IDENTITY_INVALID");
    const usageName = `${call.invocation_id.replaceAll(":", "-")}.json`;
    expectedUsageFiles.push(usageName);
    const usagePath = path.join(usageRoot, usageName);
    const usage = JSON.parse(fs.readFileSync(usagePath, "utf8"));
    usageReceipts.push(usage);
    const tracePath = path.join(tracesRoot, traceName);
    const traceText = fs.readFileSync(tracePath, "utf8");
    const context = codexLunaCallContext({ workRoot, privateRoot, scenarioIds, call, index });
    const parsed = parseCodexLunaAppServerTranscript(unwrapCodexLunaSanitizedTrace(traceText), {
      workspaceRoot: context.workspace,
      skillPath: context.skillPath,
      codexHome: context.codexHome,
      mode: context.mode,
      secretValues: canaries,
    });
    const shellLang = [...new Set([process.env.LANG, process.env.LC_ALL, "C.UTF-8"].filter((value) => typeof value === "string" && value.length > 0))]
      .find((value) => sha256Bytes(value) === call.process?.app_server?.permission_profile?.shell_environment?.lang_sha256);
    if (!shellLang) throw new Error("CODEX_LUNA_SHELL_ENVIRONMENT_IDENTITY_INVALID");
    const profile = buildCodexLunaIsolatedConfig({
      workspaceRoot: context.workspace,
      skillPath: context.skillPath,
      codexHome: context.codexHome,
      shellLang,
      mode: context.mode,
    });
    const configPath = path.join(context.codexHome, "config.toml");
    if (fs.readFileSync(configPath, "utf8") !== profile.config_toml) {
      throw new Error("CODEX_LUNA_CODEX_HOME_CONFIG_INVALID");
    }
    const codexHomeManifest = treeManifest(context.codexHome);
    const codexHomeTreeSha256 = sha256Bytes(`${canonicalCodexJson(codexHomeManifest)}\n`);
    if (codexHomeManifest.some((entry) => entry.path === "auth.json" || entry.path.endsWith("/auth.json"))
      || !sameIdentity(call.process?.app_server?.codex_home?.manifest, codexHomeManifest)
      || call.process?.app_server?.codex_home?.tree_sha256 !== codexHomeTreeSha256) {
      throw new Error("CODEX_LUNA_CODEX_HOME_TREE_INVALID");
    }
    const appServerEvidence = buildCodexLunaAppServerEvidenceSummary({ profile, transcript: parsed, secretValues: canaries });
    const producerEvidence = codexAppServerBaseEvidence(call.process?.app_server);
    if (!sameIdentity(producerEvidence, appServerEvidence)) throw new Error("CODEX_LUNA_APP_SERVER_EVIDENCE_MISMATCH");
    const caseReceipt = index === 0 ? null : preprocessing?.cases?.[index - 1];
    if (index > 0 && caseReceipt?.scenario_id !== scenarioIds[index - 1]) {
      throw new Error("CODEX_LUNA_PREPROCESSING_TURN_IDENTITY_INVALID");
    }
    const prompt = index === 0
      ? codexLunaGenerationPrompt()
      : codexLunaDiagnosisPrompt(scenarioIds[index - 1], caseReceipt.receipt_sha256);
    const outputSchema = index === 0
      ? null
      : JSON.parse(fs.readFileSync(path.join(context.workspace, "input", "diagnosis-result.schema.json"), "utf8"));
    const outboundSha256 = validateCodexAppServerOutboundForContext(call.process.app_server.outbound, {
      workspaceRoot: context.workspace,
      skillPath: context.skillPath,
      mode: context.mode,
      threadId: parsed.thread_id,
      prompt,
      outputSchema,
    });
    const traceSha256 = sha256File(tracePath);
    if (call.process.app_server.trace_sha256 !== traceSha256) throw new Error("CODEX_LUNA_TRACE_IDENTITY_INVALID");
    const normalizedUsage = normalizeCodexUsage(parsed.usage);
    if (!sameIdentity(normalizedUsage, call.usage) || !sameIdentity(normalizedUsage, usage.usage)) {
      throw new Error("CODEX_LUNA_TRACE_USAGE_MISMATCH");
    }
    const finalPath = path.join(tracesRoot, finalName);
    const finalText = fs.readFileSync(finalPath, "utf8");
    if (finalText !== parsed.final_agent_message && finalText !== `${parsed.final_agent_message}\n`) {
      throw new Error("CODEX_LUNA_FINAL_MESSAGE_MISMATCH");
    }
    traceRevalidation.push({
      invocation_id: call.invocation_id,
      trace_sha256: traceSha256,
      app_server_evidence_sha256: sha256Bytes(canonicalCodexJson(appServerEvidence)),
      thread_id: parsed.thread_id,
      turn_id: parsed.turn_id,
      usage: normalizedUsage,
      final_message_sha256: sha256Bytes(parsed.final_agent_message),
      final_file_sha256: sha256File(finalPath),
      profile_sha256: profile.config_sha256,
      profile_byte_count: profile.config_byte_count,
      codex_home_tree_sha256: codexHomeTreeSha256,
      outbound_sha256: outboundSha256,
    });
    return {
      invocation_id: call.invocation_id,
      workflow: call.workflow,
      logical_id: call.logical_id,
      thread_id: call.thread_id,
      trace: codexEvidenceFile(tracePath, path.relative(attemptRoot, tracePath).split(path.sep).join("/")),
      stderr: codexEvidenceFile(path.join(tracesRoot, stderrName), path.relative(attemptRoot, path.join(tracesRoot, stderrName)).split(path.sep).join("/")),
      final: codexEvidenceFile(path.join(tracesRoot, finalName), path.relative(attemptRoot, path.join(tracesRoot, finalName)).split(path.sep).join("/")),
      usage_receipt: codexEvidenceFile(usagePath, path.relative(attemptRoot, usagePath).split(path.sep).join("/")),
    };
  });
  const actualTraceFiles = fs.readdirSync(tracesRoot).sort();
  const actualUsageFiles = fs.readdirSync(usageRoot).sort();
  if (!sameIdentity(actualTraceFiles, expectedTraceFiles.sort()) || !sameIdentity(actualUsageFiles, expectedUsageFiles.sort())) {
    throw new Error("CODEX_LUNA_CALL_ARTIFACT_SET_INVALID");
  }
  const generatedSkillRoot = path.join(outputRoot, "generated-skill");
  const generatedFiles = treeManifest(generatedSkillRoot);
  const generatedPackage = {
    schema_version: 1,
    status: "PASS",
    path: path.relative(attemptRoot, generatedSkillRoot).split(path.sep).join("/"),
    tree_sha256: treeDigest(generatedSkillRoot),
    file_count: generatedFiles.length,
    files: generatedFiles,
  };
  const secretScan = auditNoSecretLeak({ roots: [outputRoot, usageRoot, workRoot, privateRoot], canaries });
  if (secretScan.scanned_files <= 0) throw new Error("CODEX_LUNA_SECRET_SCAN_EMPTY");
  return {
    manifest: { schema_version: 1, status: "PASS", run_id: runId, path_base: "attempt-root", records },
    usageReceipts,
    consumer: {
      schema_version: 1,
      status: "PASS",
      run_id: runId,
      trace_revalidation: { schema_version: 1, status: "PASS", records: traceRevalidation },
      secret_scan: secretScan,
      generated_package: generatedPackage,
    },
  };
}

function sameCodexPayloadIdentity(planned, executed) {
  return planned?.schema_version === 1
    && planned.status === "PASS"
    && executed?.schema_version === 1
    && executed.status === "PASS"
    && planned.model === executed.model
    && planned.reasoning_effort === executed.reasoning_effort
    && planned.cli?.version === executed.cli?.version
    && planned.cli?.sha256 === executed.cli?.sha256
    && planned.cli?.size === executed.cli?.size
    && planned.cli?.platform === executed.cli?.platform
    && planned.cli?.architecture === executed.cli?.architecture
    && sameIdentity(planned.auth, executed.auth)
    && sameIdentity(planned.filesystem_sandbox, executed.filesystem_sandbox);
}

function materializeCodexLunaStageInputs({ scratchRoot, codexEntry, codexAuth, planned, validatorRuntime }) {
  const source = validateCodexLunaIdentity(codexEntry, codexAuth);
  if (!sameIdentity(planned, source)) throw new Error("CODEX_LUNA_PLANNED_IDENTITY_DRIFT");
  const inputRoot = path.join(scratchRoot, "release-inputs");
  fs.mkdirSync(inputRoot, { recursive: false, mode: 0o700 });
  const stagedEntry = path.join(inputRoot, "codex");
  const stagedAuth = path.join(inputRoot, "auth.json");
  const validatorRuntimeIdentity = path.join(inputRoot, "validator-runtime.json");
  fs.copyFileSync(codexEntry, stagedEntry, fs.constants.COPYFILE_EXCL);
  fs.chmodSync(stagedEntry, 0o500);
  fs.copyFileSync(codexAuth, stagedAuth, fs.constants.COPYFILE_EXCL);
  fs.chmodSync(stagedAuth, 0o400);
  writeJsonSync(validatorRuntimeIdentity, validatorRuntime);
  fs.chmodSync(validatorRuntimeIdentity, 0o400);
  const executed = validateCodexLunaIdentity(stagedEntry, stagedAuth);
  if (!sameCodexPayloadIdentity(planned, executed)) throw new Error("CODEX_LUNA_STAGED_IDENTITY_MISMATCH");
  fs.chmodSync(inputRoot, 0o500);
  return { inputRoot, stagedEntry, stagedAuth, validatorRuntimeIdentity, source, executed };
}

async function codexLunaMethods(context, stage) {
  const outputRoot = gateRoot(context, stage);
  const scratchRoot = path.join(context.attemptRoot, "scratch", "codex-luna-methods");
  const preprocessingRoot = path.join(scratchRoot, "preprocessing");
  const preprocessingReceipt = path.join(preprocessingRoot, "codex-luna-preprocessing.json");
  const casesRoot = path.join(context.sourceSnapshotRoot, "experiments", "rpc-skill-feasibility", "cases");
  const scenarioIds = releaseCodexScenarioIds(casesRoot);
  const metaSkillRoot = path.join(context.sourceSnapshotRoot, ".agents", "skills", "wiki-to-diagnosis-skill");
  const wiki = releaseWikiPath(context.sourceSnapshotRoot);
  const runId = path.basename(context.attemptRoot);
  const logparseRoot = context.options.logparseSource ?? "";
  const logparsePython = path.join(logparseRoot, ".venv", "bin", "python");
  const plannedCodex = context.plan.release_inputs?.codex;
  const plannedExternal = context.plan.release_inputs?.external_sources?.logparse;
  const plannedLogparseRuntime = context.plan.release_inputs?.codex_logparse_runtime;
  const preExternal = externalGitIdentity(logparseRoot, plannedExternal?.head);
  const preLogparseRuntime = codexLogparseRuntimeIdentity(logparseRoot);
  if (!sameIdentity(publicExternalGitIdentity(preExternal), plannedExternal)
    || !sameIdentity(preLogparseRuntime, plannedLogparseRuntime)) {
    return { status: "BLOCKED", failure_domain: "INFRA", code: "CODEX_LOGPARSE_RUNTIME_IDENTITY_DRIFT", elapsed_seconds: 0 };
  }
  ensureDirectory(scratchRoot);
  let stageInputs;
  try {
    stageInputs = materializeCodexLunaStageInputs({
      scratchRoot,
      codexEntry: context.options.codexEntry,
      codexAuth: context.options.codexAuth,
      planned: plannedCodex,
      validatorRuntime: plannedLogparseRuntime,
    });
  } catch (error) {
    return { status: "BLOCKED", failure_domain: "INFRA", code: String(error?.message ?? "CODEX_LUNA_STAGE_INPUT_INVALID"), elapsed_seconds: 0 };
  }
  const started = Date.now();
  const prepared = await runProcess({
    repoRoot: context.repoRoot,
    attemptRoot: context.attemptRoot,
    stage,
    command: logparsePython,
    args: [
      "-I",
      "-B",
      path.join(context.sourceSnapshotRoot, "tools", "test-flow", "runtime-support", "codex-luna-prepare.py"),
      "--case-root", casesRoot,
      "--logparse-root", logparseRoot,
      "--output-root", preprocessingRoot,
    ],
    cwd: context.sourceSnapshotRoot,
    hardTimeoutSeconds: Math.min(1200, stage.timeout_seconds - 60),
    noProgressSeconds: context.policies.real_no_progress_seconds,
    rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
    eventWriter: context.eventWriter,
    executionId: `${gateExecutionId(stage, context.gateId)}-preprocess`,
    pollMilliseconds: context.policies.poll_milliseconds,
    progressAllowlistVersion: context.policies.progress_allowlist_version,
  });
  if (prepared.status !== "PASS") return { ...prepared, failure_domain: prepared.status === "ERROR" ? "HARNESS" : "EXTERNAL", code: prepared.termination?.trigger ?? "CODEX_PREPROCESSING_FAILED" };
  let preprocessing;
  try { preprocessing = JSON.parse(fs.readFileSync(preprocessingReceipt, "utf8")); } catch {
    return { ...prepared, status: "ERROR", failure_domain: "HARNESS", code: "CODEX_PREPROCESSING_RECEIPT_INVALID" };
  }
  const postExternal = externalGitIdentity(logparseRoot, plannedExternal?.head);
  const postLogparseRuntime = codexLogparseRuntimeIdentity(logparseRoot);
  const runtimeAudit = {
    schema_version: 1,
    status: sameIdentity(publicExternalGitIdentity(postExternal), plannedExternal)
      && sameIdentity(postLogparseRuntime, plannedLogparseRuntime) ? "PASS" : "FAIL",
    policy: "plan-bound-git-venv-python-base-cli-pre-and-post-preprocessing-v1",
    planned: { external: plannedExternal, runtime: plannedLogparseRuntime },
    observed: {
      pre: { external: publicExternalGitIdentity(preExternal), runtime: preLogparseRuntime },
      post: { external: publicExternalGitIdentity(postExternal), runtime: postLogparseRuntime },
    },
  };
  if (runtimeAudit.status !== "PASS") {
    ensureDirectory(outputRoot);
    writeJsonSync(path.join(outputRoot, "codex-luna-logparse-runtime.json"), runtimeAudit);
    return { ...prepared, status: "BLOCKED", failure_domain: "INFRA", code: "CODEX_LOGPARSE_RUNTIME_IDENTITY_DRIFT" };
  }
  if (!validCodexLogparsePreprocessingIdentity(preprocessing, { external: plannedExternal, runtime: plannedLogparseRuntime })
    || preprocessing.status !== "PASS" || preprocessing.case_count !== CODEX_LUNA_SCENARIO_COUNT
    || preprocessing.totals?.parse_invocations !== CODEX_LUNA_SCENARIO_COUNT
    || preprocessing.totals?.target_query_invocations !== CODEX_LUNA_SCENARIO_COUNT * 2
    || preprocessing.totals?.diagnosis_invocations !== 0
    || !validCodexPreprocessingCases(preprocessing, {
      scenarioIds,
      logparseConfigSha256: sha256File(path.join(casesRoot, "..", "logparse-config.json")),
    })) {
    ensureDirectory(outputRoot);
    writeJsonSync(path.join(outputRoot, "codex-luna-logparse-runtime.json"), runtimeAudit);
    return { ...prepared, status: "ERROR", failure_domain: "HARNESS", code: "CODEX_PREPROCESSING_RECEIPT_INVALID" };
  }
  let beforeModelIdentity;
  try {
    beforeModelIdentity = validateCodexLunaIdentity(stageInputs.stagedEntry, stageInputs.stagedAuth);
  } catch {
    return { ...prepared, status: "BLOCKED", failure_domain: "INFRA", code: "CODEX_LUNA_STAGED_IDENTITY_DRIFT" };
  }
  if (!sameIdentity(beforeModelIdentity, stageInputs.executed)) {
    return { ...prepared, status: "BLOCKED", failure_domain: "INFRA", code: "CODEX_LUNA_STAGED_IDENTITY_DRIFT" };
  }
  const elapsedBeforeModel = Math.ceil((Date.now() - started) / 1000);
  const remaining = stage.timeout_seconds - elapsedBeforeModel - 30;
  if (remaining <= 0) return { ...prepared, status: "ERROR", failure_domain: "HARNESS", code: "CODEX_STAGE_TIMEOUT" };
  const usageRoot = path.join(context.attemptRoot, "payload", "model-usage", "codex-luna");
  const result = await runProcess({
    repoRoot: context.repoRoot,
    attemptRoot: context.attemptRoot,
    stage,
    command: process.execPath,
    args: [
      path.join(context.sourceSnapshotRoot, "tools", "test-flow", "runtime-support", "codex-luna-exploration-runner.mjs"),
      "--codex-entry", stageInputs.stagedEntry,
      "--auth-source", stageInputs.stagedAuth,
      "--meta-skill-root", metaSkillRoot,
      "--wiki", wiki,
      "--case-root", casesRoot,
      "--preprocessed-root", path.join(preprocessingRoot, "preprocessed"),
      "--validator-python", logparsePython,
      "--validator-runtime-root", logparseRoot,
      "--validator-runtime-identity", stageInputs.validatorRuntimeIdentity,
      "--work-root", path.join(scratchRoot, "work"),
      "--private-root", path.join(scratchRoot, "private"),
      "--evidence-root", outputRoot,
      "--usage-root", usageRoot,
      "--run-id", runId,
      "--allow-posthoc-budget",
    ],
    cwd: context.sourceSnapshotRoot,
    hardTimeoutSeconds: Math.min(7260, remaining),
    noProgressSeconds: context.policies.real_no_progress_seconds,
    rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
    eventWriter: context.eventWriter,
    executionId: gateExecutionId(stage, context.gateId),
    pollMilliseconds: context.policies.poll_milliseconds,
    progressAllowlistVersion: context.policies.progress_allowlist_version,
  });
  ensureDirectory(outputRoot);
  fs.copyFileSync(preprocessingReceipt, path.join(outputRoot, "codex-luna-preprocessing.json"), fs.constants.COPYFILE_EXCL);
  writeJsonSync(path.join(outputRoot, "codex-luna-logparse-runtime.json"), runtimeAudit);
  let postModelIdentity = null;
  try { postModelIdentity = validateCodexLunaIdentity(stageInputs.stagedEntry, stageInputs.stagedAuth); } catch {}
  const stageInputReceipt = {
    schema_version: 1,
    status: postModelIdentity && sameIdentity(postModelIdentity, stageInputs.executed) ? "PASS" : "FAIL",
    policy: "plan-validated-attempt-private-copy-read-execute-only-v1",
    planned_source_identity: plannedCodex,
    observed_source_identity: stageInputs.source,
    executed_identity: stageInputs.executed,
    before_model_identity: beforeModelIdentity,
    post_model_identity: postModelIdentity,
    filesystem: {
      directory_mode: (fs.statSync(stageInputs.inputRoot).mode & 0o777).toString(8).padStart(3, "0"),
      cli_mode: (fs.statSync(stageInputs.stagedEntry).mode & 0o777).toString(8).padStart(3, "0"),
      auth_mode: (fs.statSync(stageInputs.stagedAuth).mode & 0o777).toString(8).padStart(3, "0"),
      validator_runtime_mode: (fs.statSync(stageInputs.validatorRuntimeIdentity).mode & 0o777).toString(8).padStart(3, "0"),
    },
  };
  writeJsonSync(path.join(outputRoot, "codex-luna-stage-inputs.json"), stageInputReceipt);
  if (stageInputReceipt.status !== "PASS") return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CODEX_LUNA_STAGED_IDENTITY_DRIFT" };
  if (result.status === "ERROR") return { ...result, failure_domain: "HARNESS", code: result.termination?.trigger ?? "CODEX_LUNA_EVIDENCE_ERROR" };
  let receipt;
  let ledger;
  let budget;
  let security;
  let identity;
  let skill;
  try {
    receipt = JSON.parse(fs.readFileSync(path.join(outputRoot, "codex-luna-result.json"), "utf8"));
    ledger = JSON.parse(fs.readFileSync(path.join(outputRoot, "codex-luna-invocations.json"), "utf8"));
    budget = JSON.parse(fs.readFileSync(path.join(outputRoot, "codex-luna-usage.json"), "utf8"));
    security = JSON.parse(fs.readFileSync(path.join(outputRoot, "codex-luna-security-audit.json"), "utf8"));
    identity = JSON.parse(fs.readFileSync(path.join(outputRoot, "codex-luna-identity.json"), "utf8"));
    skill = JSON.parse(fs.readFileSync(path.join(outputRoot, "codex-luna-skill.json"), "utf8"));
  } catch {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CODEX_LUNA_RECEIPT_INVALID" };
  }
  let callEvidence;
  try {
    callEvidence = buildCodexLunaCallManifest({
      attemptRoot: context.attemptRoot,
      outputRoot,
      usageRoot,
      workRoot: path.join(scratchRoot, "work"),
      privateRoot: path.join(scratchRoot, "private"),
      authSource: stageInputs.stagedAuth,
      runId,
      scenarioIds,
      preprocessing,
      ledger,
    });
    writeJsonSync(path.join(outputRoot, "codex-luna-call-manifest.json"), callEvidence.manifest);
    writeJsonSync(path.join(outputRoot, "codex-luna-consumer-audit.json"), callEvidence.consumer);
  } catch {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CODEX_LUNA_CALL_EVIDENCE_INVALID" };
  }
  const passBoundary = validCodexLunaPassBoundary({
    receipt,
    ledger,
    budget,
    security,
    identity,
    skill,
    preprocessing,
    callManifest: callEvidence.manifest,
    usageReceipts: callEvidence.usageReceipts,
    consumer: callEvidence.consumer,
  }, {
    runId,
    executedCodex: stageInputs.executed,
    external: plannedExternal,
    logparseRuntime: plannedLogparseRuntime,
    scenarioIds,
    metaSkillTreeSha256: treeDigest(metaSkillRoot),
    wikiSha256: sha256File(wiki),
    wikiSize: fs.statSync(wiki).size,
    validatorPythonPathSha256: sha256Bytes(path.resolve(logparsePython)),
    logparseConfigSha256: sha256File(path.join(casesRoot, "..", "logparse-config.json")),
    callEvidenceRoot: path.relative(context.attemptRoot, outputRoot).split(path.sep).join("/"),
    generationWorkspacePathSha256: sha256Bytes(path.join(scratchRoot, "work", "generation")),
    diagnosisWorkspacePathSha256: scenarioIds.map((scenarioId) => sha256Bytes(path.join(scratchRoot, "work", "diagnoses", scenarioId))),
    forbiddenReadPathSha256: [
      sha256Bytes(path.join(context.sourceSnapshotRoot, "AGENTS.md")),
      sha256Bytes(path.join(context.sourceSnapshotRoot, "experiments", "rpc-skill-feasibility", "cases", "api-execution-overrun", "raw", "client.log")),
      sha256Bytes(path.resolve(stageInputs.stagedAuth)),
    ],
  });
  if (result.status !== "PASS" || !passBoundary) {
    return { ...result, status: receipt?.status === "FAIL" ? "FAIL" : "ERROR", failure_domain: receipt?.status === "FAIL" ? "CONTRACT" : "HARNESS", code: receipt?.failure?.code ?? "CODEX_LUNA_RECEIPT_INVALID" };
  }
  const caps = context.planStage.hard_caps;
  const invocations = ledger.calls.map((call, index) => ({
    schema_version: 3,
    invocation_id: call.invocation_id,
    class: "codex-luna-agent",
    workflow: call.workflow,
    effective_model: receipt.model,
    effective_reasoning_effort: receipt.reasoning_effort,
    effective_caps: caps,
    usage_complete: call.usage_complete === true,
    usage: codexUsage(call.usage),
    turns: callEvidence.usageReceipts[index].turns,
    terminal: { subtype: "success", is_error: false, event: call.terminal?.event ?? null, thread_id: call.thread_id },
    wrapper_outcome: { schema_version: 1, status: "PASS", code: null },
    hard_cap_enforcement: {
      calls: "exactly-ten-no-retry",
      wall: "wrapper-process-watchdog",
      total_tokens: `posthoc-terminal-aggregate:${TOKEN_USAGE_FORMULA}`,
      cost_usd: "posthoc-terminal-aggregate-conservative-api-equivalent",
    },
  }));
  return {
    ...result,
    status: "PASS",
    failure_domain: null,
    code: null,
    invocations,
    usage: sumUsage(invocations.map((invocation) => invocation.usage)),
    usage_complete: true,
    adapter_receipt: {
      schema_version: receipt.schema_version,
      status: receipt.status,
      warning: receipt.warning,
      model: receipt.model,
      reasoning_effort: receipt.reasoning_effort,
      execution_identity: {
        cli_sha256: identity.cli.sha256,
        cli_entry_path_sha256: identity.cli.entry_path_sha256,
        auth_sha256: identity.auth.source_sha256,
        account_id_sha256: identity.auth.account_id_sha256,
        logparse_venv_tree_sha256: plannedLogparseRuntime.venv.tree_sha256,
        logparse_python_sha256: plannedLogparseRuntime.python.resolved_sha256,
      },
      call_contract: receipt.call_contract,
      posthoc_budget: receipt.posthoc_budget,
    },
  };
}

function macosCodexPythonEntry(repoRoot) {
  const runtime = resolvePythonTestRuntime(repoRoot);
  if (runtime === null || runtime.interpreterPrefix.length !== 0) throw new Error("MACOS_CODEX_LUNA_PYTHON_RUNTIME_MISSING");
  return runtime.command;
}

export function quickValidationCodexEntryStrategy({
  platform = process.platform,
  architecture = process.arch,
  environment = process.env,
} = {}) {
  return platform === "linux"
    && architecture === "x64"
    && environment.TEST_FLOW_QUICK_UBUNTU2204_CONTAINER === "1"
    ? "sealed-system-entry"
    : "attempt-private-copy";
}

export function quickValidationScratchRoot(context, name, environment = process.env) {
  const configured = environment.TEST_FLOW_QUICK_SCRATCH_ROOT;
  if (!configured) return path.join(context.attemptRoot, "scratch", name);
  if (environment.TEST_FLOW_QUICK_UBUNTU2204_CONTAINER !== "1" || !path.isAbsolute(configured)) {
    throw new Error("QUICK_VALIDATION_SCRATCH_ROOT_INVALID");
  }
  const root = path.resolve(configured);
  const attemptRoot = path.resolve(context.attemptRoot);
  if (root === attemptRoot
    || root.startsWith(`${attemptRoot}${path.sep}`)
    || attemptRoot.startsWith(`${root}${path.sep}`)) {
    throw new Error("QUICK_VALIDATION_SCRATCH_ROOT_OVERLAP");
  }
  const runRoot = path.join(root, path.basename(attemptRoot));
  ensureDirectory(runRoot);
  return path.join(runRoot, name);
}

function materializeMacosCodexInputs({ scratchRoot, codexEntry, codexAuth, planned }) {
  const source = validateCodexLunaIdentity(codexEntry, codexAuth);
  if (!sameIdentity(planned, source)) throw new Error("MACOS_CODEX_LUNA_PLANNED_IDENTITY_DRIFT");
  const inputRoot = path.join(scratchRoot, "codex-inputs");
  fs.mkdirSync(inputRoot, { recursive: false, mode: 0o700 });
  const entryStrategy = quickValidationCodexEntryStrategy();
  const stagedEntry = entryStrategy === "sealed-system-entry"
    ? path.resolve(codexEntry)
    : path.join(inputRoot, "codex");
  const stagedAuth = path.join(inputRoot, "auth.json");
  if (entryStrategy === "sealed-system-entry") {
    if (stagedEntry !== "/usr/bin/codex") throw new Error("QUICK_VALIDATION_CODEX_SYSTEM_ENTRY_INVALID");
  } else {
    fs.copyFileSync(codexEntry, stagedEntry, fs.constants.COPYFILE_EXCL);
    fs.chmodSync(stagedEntry, 0o500);
  }
  fs.copyFileSync(codexAuth, stagedAuth, fs.constants.COPYFILE_EXCL);
  fs.chmodSync(stagedAuth, 0o400);
  const executed = validateCodexLunaIdentity(stagedEntry, stagedAuth);
  if (!sameCodexPayloadIdentity(planned, executed)) throw new Error("MACOS_CODEX_LUNA_STAGED_IDENTITY_DRIFT");
  fs.chmodSync(inputRoot, 0o500);
  return { inputRoot, stagedEntry, stagedAuth, source, executed, entryStrategy };
}

function macosCodexInvocationProjection(invocation, hardCaps, invocationClass) {
  const workflow = `${invocation.role}:${invocation.attempt}`;
  let usage = null;
  try { usage = normalizeCodexUsage(invocation.usage); } catch {}
  const failed = invocation.status === "FAIL";
  return {
    schema_version: 3,
    invocation_id: invocation.invocation_id,
    class: invocationClass,
    workflow,
    effective_model: invocation.model,
    effective_reasoning_effort: invocation.reasoning_effort,
    effective_caps: hardCaps,
    usage_complete: invocation.terminal === true && usage !== null,
    usage: usage === null ? undefined : codexUsage(usage),
    turns: 1,
    terminal: { subtype: failed ? "error" : "success", is_error: failed, event: failed ? "turn.failed" : "turn.completed", thread_id: invocation.thread_id ?? null },
    wrapper_outcome: { schema_version: 1, status: invocation.status, code: invocation.failure_code ?? null },
    hard_cap_enforcement: {
      calls: "exact-no-retry",
      wall: "wrapper-process-watchdog",
      total_tokens: `posthoc-terminal-aggregate:${TOKEN_USAGE_FORMULA}`,
      cost_usd: "posthoc-terminal-aggregate-published-price-snapshot",
    },
  };
}

async function runMacosCodexLunaGate(context, stage, { workflow }) {
  const outputRoot = gateRoot(context, stage);
  const scratchRoot = quickValidationScratchRoot(context, workflow === "methods" ? "macos-codex-luna-methods" : "macos-codex-luna-e2e");
  ensureDirectory(scratchRoot);
  let staged;
  let pythonEntry;
  let providerInputs = null;
  try {
    staged = materializeMacosCodexInputs({
      scratchRoot,
      codexEntry: context.options.codexEntry,
      codexAuth: context.options.codexAuth,
      planned: context.plan.release_inputs?.codex,
    });
    pythonEntry = macosCodexPythonEntry(context.repoRoot);
    if (workflow === "e2e") providerInputs = evidenceV2ProviderRuntimeInputs(context);
  } catch (error) {
    return { status: "BLOCKED", failure_domain: "INFRA", code: String(error?.message ?? "MACOS_CODEX_LUNA_INPUT_INVALID"), elapsed_seconds: 0 };
  }
  const usageRoot = path.join(context.attemptRoot, "payload", "model-usage", workflow === "methods" ? "macos-codex-luna-methods" : "macos-codex-luna-e2e");
  const runner = path.join(context.sourceSnapshotRoot, "tools", "test-flow", "quick-validation", "codex-luna", "runtime", workflow === "methods" ? "macos-codex-luna-methods-runner.mjs" : "macos-codex-luna-e2e-runner.mjs");
  const common = [
    "--codex-entry", staged.stagedEntry,
    "--auth-source", staged.stagedAuth,
    "--python-entry", pythonEntry,
    "--work-root", path.join(scratchRoot, "work"),
    "--private-root", path.join(scratchRoot, "private"),
    "--evidence-root", outputRoot,
    "--usage-root", usageRoot,
    "--run-id", path.basename(context.attemptRoot),
  ];
  const args = workflow === "methods"
    ? [
      runner,
      ...common,
      "--cache-root", context.options.cacheRoot ?? path.join(context.repoRoot, ".tmp", "test-flow-cache"),
      "--allow-posthoc-budget",
      "--meta-skill-root", path.join(context.sourceSnapshotRoot, ".agents", "skills", "wiki-to-diagnosis-skill"),
      "--wiki", releaseWikiPath(context.sourceSnapshotRoot),
      "--registration-template", path.join(context.sourceSnapshotRoot, "tests", "cases", "release", "rpc-timeout-anonymized", "registration", "rpc-timeout-methods-v1", "registration-template.json"),
      ...(context.planStage.invocation_caps.length === 0 ? ["--verify-cache-only"] : []),
    ]
    : [
      runner,
      ...common,
      "--source-root", providerInputs.sourceRoot,
      "--registration-root", providerInputs.registrationRoot,
      "--source-snapshot-digest", providerInputs.sourceSnapshotDigest,
      "--core-verdict", providerInputs.coreVerdictPath,
      "--scenario", providerInputs.scenario,
    ];
  const result = await runProcess({
    repoRoot: context.repoRoot,
    attemptRoot: context.attemptRoot,
    stage,
    command: process.execPath,
    args,
    cwd: context.sourceSnapshotRoot,
    hardTimeoutSeconds: stage.timeout_seconds - 30,
    noProgressSeconds: context.planStage.no_progress_seconds,
    rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
    eventWriter: context.eventWriter,
    executionId: gateExecutionId(stage, context.gateId),
    pollMilliseconds: context.policies.poll_milliseconds,
    progressAllowlistVersion: context.policies.progress_allowlist_version,
  });
  if (result.status !== "PASS") {
    return providerRunnerFailureResult({
      provider: "codex-luna",
      result,
      attemptRoot: context.attemptRoot,
      outputRoot,
      usageRoot,
      planStage: context.planStage,
      invocationClass: workflow === "methods" ? "codex-luna-methods-bootstrap" : "codex-luna-macos-e2e",
      fallbackCode: "MACOS_CODEX_LUNA_RUNNER_FAILED",
    });
  }
  let gate;
  let invocationLedger;
  try {
    gate = JSON.parse(fs.readFileSync(path.join(outputRoot, "adapter-receipt.json"), "utf8"));
    invocationLedger = JSON.parse(fs.readFileSync(path.join(outputRoot, "model-invocations.json"), "utf8"));
  } catch {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "MACOS_CODEX_LUNA_GATE_RECEIPT_INVALID" };
  }
  const ledgerValid = workflow === "methods"
    ? invocationLedger.status === "PASS" && invocationLedger.invocations?.length === context.planStage.invocation_caps.reduce((sum, declaration) => sum + declaration.max_count, 0)
    : validEvidenceV2ProviderInvocationLedger(context.planStage, invocationLedger);
  if (gate.status !== "PASS" || !ledgerValid) {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "MACOS_CODEX_LUNA_GATE_RECEIPT_INVALID" };
  }
  const invocationClass = workflow === "methods" ? "codex-luna-methods-bootstrap" : "codex-luna-macos-e2e";
  const invocations = invocationLedger.invocations.map((invocation) => macosCodexInvocationProjection(invocation, context.planStage.hard_caps, invocationClass));
  return {
    ...result,
    status: "PASS",
    failure_domain: null,
    code: null,
    invocations,
    usage: sumUsage(invocations.map((invocation) => invocation.usage)),
    usage_complete: true,
    adapter_receipt: gate,
  };
}

function claudeDeepseekInvocationProjection(invocation, hardCaps, invocationClass) {
  const attempt = invocation.evaluation_attempt ?? invocation.attempt;
  const failed = invocation.status === "FAIL";
  const usageComplete = invocation.terminal === true && isCompleteUsage(invocation.usage);
  const observedTerminal = invocation.provider_terminal;
  const terminal = observedTerminal !== null && typeof observedTerminal === "object"
    ? {
        subtype: typeof observedTerminal.subtype === "string" ? observedTerminal.subtype : (failed ? "error" : "success"),
        is_error: typeof observedTerminal.is_error === "boolean" ? observedTerminal.is_error : failed,
        stop_reason: observedTerminal.stop_reason ?? null,
        exit_code: observedTerminal.exit_code ?? null,
        signal: observedTerminal.signal ?? null,
      }
    : { subtype: failed ? "error" : "success", is_error: failed };
  return {
    schema_version: 3,
    invocation_id: invocation.invocation_id,
    class: invocationClass,
    workflow: `${invocation.role}:${attempt}`,
    effective_model: invocation.model,
    effective_caps: hardCaps,
    usage_complete: usageComplete,
    usage: usageComplete ? invocation.usage : undefined,
    environment_policy: invocation.environment_policy,
    turns: invocation.turns,
    terminal,
    provider_budget: invocation.budget ?? null,
    wrapper_outcome: { schema_version: 1, status: invocation.status, code: invocation.failure_code ?? null },
    hard_cap_enforcement: {
      turns: "claude-cli",
      cost_usd: "claude-cli",
      hard_timeout_seconds: "wrapper-process-watchdog",
      total_tokens: `posthoc-terminal-aggregate:${TOKEN_USAGE_FORMULA}`,
      max_output_tokens: ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT,
      model_process_retry: "forbidden-by-phase-claim",
    },
  };
}

export function validClaudeDeepseekInvocationLedger(planStage, ledger) {
  if (!Array.isArray(planStage?.invocation_caps) || ledger?.status !== "PASS" || !Array.isArray(ledger.invocations)) return false;
  if (planStage.invocation_caps.length !== 1) return false;
  const declaration = planStage.invocation_caps[0];
  if (!Array.isArray(declaration.phases)) return false;
  if (declaration.class === "claude-deepseek-registration-generation") {
    return declaration.min_count === declaration.phases.length
      && declaration.max_count === declaration.phases.length
      && ledger.invocations.length === declaration.phases.length
      && ledger.invocations.every((invocation, index) => invocation?.status === "PASS" && invocation?.terminal === true && invocation.phase === declaration.phases[index]);
  }
  return validEvidenceV2ProviderInvocationLedger(planStage, ledger)
    && ledger.invocations.every((invocation) => validProviderInvocationReceipt(invocation, {
      role: invocation.role,
      attempt: providerInvocationAttempt(invocation),
    }, "claude-deepseek", planStage.hard_caps))
    && validDeepseekReceiptSequence(ledger.invocations, planStage.hard_caps);
}

export function validEvidenceV2ProviderInvocationLedger(planStage, ledger) {
  if (!Array.isArray(planStage?.invocation_caps) || planStage.invocation_caps.length !== 1 || ledger?.status !== "PASS" || !Array.isArray(ledger.invocations)) return false;
  const declaration = planStage.invocation_caps[0];
  if (declaration.min_count !== 2 || declaration.max_count !== 4 || declaration.normal_count !== 2 || declaration.repair_max_count !== 2) return false;
  if (ledger.invocations.length < declaration.min_count || ledger.invocations.length > declaration.max_count) return false;
  const topology = ledger.invocations.map((invocation) => {
    const attempt = invocation?.evaluation_attempt ?? invocation?.attempt;
    return `${invocation?.role}:${attempt}`;
  }).join(",");
  const legal = new Set([
    "SPECIALIST:PRIMARY,REVIEWER:PRIMARY",
    "SPECIALIST:PRIMARY,SPECIALIST:REPAIR,REVIEWER:PRIMARY",
    "SPECIALIST:PRIMARY,REVIEWER:PRIMARY,REVIEWER:REPAIR",
    "SPECIALIST:PRIMARY,SPECIALIST:REPAIR,REVIEWER:PRIMARY,REVIEWER:REPAIR",
  ]);
  return legal.has(topology)
    && new Set(ledger.invocations.map((invocation) => invocation?.invocation_id)).size === ledger.invocations.length
    && ledger.invocations.every((invocation) => typeof invocation?.invocation_id === "string" && invocation.invocation_id.length > 0 && invocation?.status === "PASS" && invocation?.terminal === true);
}

const PROVIDER_ROLE_RECEIPTS = Object.freeze([
  Object.freeze({ name: "specialist-primary.json", role: "SPECIALIST", attempt: "PRIMARY" }),
  Object.freeze({ name: "specialist-repair.json", role: "SPECIALIST", attempt: "REPAIR" }),
  Object.freeze({ name: "reviewer-primary.json", role: "REVIEWER", attempt: "PRIMARY" }),
  Object.freeze({ name: "reviewer-repair.json", role: "REVIEWER", attempt: "REPAIR" }),
]);

const PROVIDER_ROLE_SEQUENCE_PREFIXES = new Set([
  "SPECIALIST:PRIMARY",
  "SPECIALIST:PRIMARY,SPECIALIST:REPAIR",
  "SPECIALIST:PRIMARY,REVIEWER:PRIMARY",
  "SPECIALIST:PRIMARY,SPECIALIST:REPAIR,REVIEWER:PRIMARY",
  "SPECIALIST:PRIMARY,REVIEWER:PRIMARY,REVIEWER:REPAIR",
  "SPECIALIST:PRIMARY,SPECIALIST:REPAIR,REVIEWER:PRIMARY,REVIEWER:REPAIR",
]);

function exactAttemptPath(attemptRoot, relativePath) {
  if (typeof relativePath !== "string" || relativePath.length === 0 || path.isAbsolute(relativePath)) return null;
  const root = path.resolve(attemptRoot);
  const target = path.resolve(root, ...relativePath.split("/"));
  return target.startsWith(`${root}${path.sep}`) ? target : null;
}

export function providerRunnerFailureCode({ result, attemptRoot, fallbackCode }) {
  if (typeof result?.termination?.trigger === "string" && result.termination.trigger.length > 0) {
    return result.termination.trigger;
  }
  if (result?.stderr_truncated === true) return fallbackCode;
  const stderrPath = exactAttemptPath(attemptRoot, result?.stderr_path);
  if (stderrPath === null || !fs.existsSync(stderrPath)) return fallbackCode;
  try {
    const parsed = JSON.parse(fs.readFileSync(stderrPath, "utf8").trim());
    const keys = Object.keys(parsed ?? {}).sort();
    if (keys.join("\0") !== ["code", "message", "schema_version", "status"].join("\0")) return fallbackCode;
    if (parsed.schema_version !== 1 || parsed.status !== "FAIL") return fallbackCode;
    if (!/^[A-Z][A-Z0-9_]*$/u.test(parsed.code ?? "") || typeof parsed.message !== "string" || parsed.message.length === 0) return fallbackCode;
    return parsed.code;
  } catch {
    return fallbackCode;
  }
}

function providerInvocationAttempt(invocation) {
  return invocation?.evaluation_attempt ?? invocation?.attempt;
}

function roundedUsd(value) {
  return Math.round(value * 1_000_000) / 1_000_000;
}

function validDeepseekReceiptSequence(invocations, planCaps) {
  const primaryCost = {};
  const roleCost = { SPECIALIST: 0, REVIEWER: 0 };
  let totalCost = 0;
  let totalTokens = 0;
  for (const invocation of invocations) {
    const attempt = providerInvocationAttempt(invocation);
    const usage = isCompleteUsage(invocation.usage) ? invocation.usage : null;
    const expectedPrior = attempt === "PRIMARY" ? 0 : primaryCost[invocation.role];
    if (!Number.isFinite(expectedPrior) || invocation.budget.prior_cost_usd !== expectedPrior) return false;
    if (attempt === "PRIMARY" && usage !== null) primaryCost[invocation.role] = usage.cost_usd;
    if (usage !== null) {
      roleCost[invocation.role] = roundedUsd(roleCost[invocation.role] + usage.cost_usd);
      totalCost = roundedUsd(totalCost + usage.cost_usd);
      totalTokens += usage.total_tokens;
    }
    if (invocation.status === "PASS" && roleCost[invocation.role] > CLAUDE_DEEPSEEK_MODEL_CERT_ROLE_POOL_USD) return false;
  }
  return !invocations.every((invocation) => invocation.status === "PASS") || (
    Number.isSafeInteger(planCaps?.max_total_tokens)
      && Number.isFinite(planCaps?.max_budget_usd)
      && totalTokens <= planCaps.max_total_tokens
      && totalCost <= planCaps.max_budget_usd
  );
}

function validProviderInvocationReceipt(invocation, expected, provider, planCaps = null) {
  if (invocation?.schema_version !== 1
    || !["PASS", "FAIL"].includes(invocation.status)
    || invocation.terminal !== true
    || invocation.role !== expected.role
    || providerInvocationAttempt(invocation) !== expected.attempt
    || typeof invocation.invocation_id !== "string"
    || invocation.invocation_id.length === 0) return false;
  if (provider === "codex-luna") {
    try {
      if (invocation.usage !== null && invocation.usage !== undefined) normalizeCodexUsage(invocation.usage);
      return (invocation.status === "FAIL" || (invocation.usage !== null && invocation.usage !== undefined))
        && invocation.provider === "openai-codex-app-server"
        && invocation.attempt === expected.attempt
        && invocation.repair === (expected.attempt === "REPAIR");
    } catch {
      return false;
    }
  }
  if (planCaps === null) return false;
  try {
    validateClaudeDeepseekRoleReceipt(invocation, {
      planCaps,
      expectedRole: expected.role,
      expectedAttempt: expected.attempt,
    });
    return true;
  } catch {
    return false;
  }
}

function validProviderInvocationSequence(invocations) {
  if (!Array.isArray(invocations) || invocations.length === 0 || invocations.length > PROVIDER_ROLE_RECEIPTS.length) return false;
  if (new Set(invocations.map((item) => item.invocation_id)).size !== invocations.length) return false;
  const sequence = invocations.map((item) => `${item.role}:${providerInvocationAttempt(item)}`).join(",");
  return PROVIDER_ROLE_SEQUENCE_PREFIXES.has(sequence)
    && invocations.slice(0, -1).every((item) => item.status === "PASS");
}

function readJsonOrNull(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return null;
  }
}

function readProviderRoleReceipts(usageRoot, provider, planCaps = null) {
  if (!fs.existsSync(usageRoot)) return [];
  const actualNames = fs.readdirSync(usageRoot).filter((name) => name.endsWith(".json")).sort();
  const allowedNames = new Set(PROVIDER_ROLE_RECEIPTS.map((item) => item.name));
  if (actualNames.some((name) => !allowedNames.has(name))) return [];
  const receipts = [];
  for (const expected of PROVIDER_ROLE_RECEIPTS) {
    const receiptPath = path.join(usageRoot, expected.name);
    if (!fs.existsSync(receiptPath)) continue;
    const receipt = readJsonOrNull(receiptPath);
    if (!validProviderInvocationReceipt(receipt, expected, provider, planCaps)) return [];
    receipts.push(receipt);
  }
  return validProviderInvocationSequence(receipts)
    && (provider !== "claude-deepseek" || validDeepseekReceiptSequence(receipts, planCaps))
    ? receipts
    : [];
}

function modelUsageAggregate(outputRoot) {
  const receiptPath = path.join(outputRoot, "model-usage.json");
  if (!fs.existsSync(receiptPath)) return { present: false, complete: false, failure_ledger: false, usage: null };
  const receipt = readJsonOrNull(receiptPath);
  const aggregate = receipt?.aggregate;
  return {
    present: true,
    complete: receipt?.usage_complete === true,
    failure_ledger: canonicalJson(Object.keys(receipt ?? {}).sort()) === canonicalJson([
      "aggregate", "schema_version", "status", "usage_complete",
    ]) && receipt.schema_version === 1 && receipt.status === "FAIL",
    usage: isCompleteUsage(aggregate) ? aggregate : null,
  };
}

function validExplicitZeroCallLedger(outputRoot, ledger) {
  if (canonicalJson(Object.keys(ledger ?? {}).sort()) !== canonicalJson([
    "invocations", "retry_policy", "schema_version", "status",
  ])) return false;
  if (ledger.schema_version !== 1
    || ledger.status !== "FAIL"
    || ledger.retry_policy !== "ROLE_PROTOCOL_REPAIR_ONLY"
    || !Array.isArray(ledger.invocations)
    || ledger.invocations.length !== 0) return false;
  const runtimeReceipt = readJsonOrNull(path.join(outputRoot, "runtime-receipt.json"));
  return runtimeReceipt?.status === "PASS"
    && runtimeReceipt.execution_mode === "real-model"
    && runtimeReceipt.production_runtime === "problem_locator.runtime.diagnosis_runtime.DiagnosisRuntime"
    && runtimeReceipt.model_invocations === 0
    && Array.isArray(runtimeReceipt.role_attempts)
    && runtimeReceipt.role_attempts.length === 0
    && runtimeReceipt.methods_result?.status === "UNRESOLVED"
    && runtimeReceipt.methods_result?.reason_code === "NO_MATCHING_METHOD_EVIDENCE";
}

export function collectProviderFailureReceipts({ provider, outputRoot }) {
  const adapterPath = path.join(outputRoot, "adapter-receipt.json");
  const runtimePath = path.join(outputRoot, "runtime-receipt.json");
  if (!fs.existsSync(adapterPath) || !fs.existsSync(runtimePath)) return {};
  const adapter = readJsonOrNull(adapterPath);
  const runtimeReceipt = readJsonOrNull(runtimePath);
  const certificationTarget = provider === "claude-deepseek" ? "P1" : "P2";
  let expected;
  try {
    expected = projectEvidenceV2ProviderTerminalFailure({
      certificationTarget,
      methodsResult: runtimeReceipt?.methods_result,
    });
  } catch {
    return {};
  }
  if (
    expected === null
    || runtimeReceipt?.status !== "PASS"
    || runtimeReceipt.production_runtime !== "problem_locator.runtime.diagnosis_runtime.DiagnosisRuntime"
    || Object.entries(expected).some(([key, value]) => canonicalJson(adapter?.[key]) !== canonicalJson(value))
  ) return {};
  return {
    adapter_receipt: {
      ...adapter,
      runtime_receipt: {
        path: "runtime-receipt.json",
        sha256: sha256File(runtimePath),
        status: runtimeReceipt.status,
        production_runtime: runtimeReceipt.production_runtime,
        methods_status: runtimeReceipt.methods_result.status,
      },
    },
    runtime_receipt: runtimeReceipt,
  };
}

function sameUsage(left, right) {
  return isCompleteUsage(left)
    && isCompleteUsage(right)
    && canonicalJson(left) === canonicalJson(right);
}

function providerAggregateMatches(provider, aggregate, projected) {
  if (!isCompleteUsage(aggregate) || !isCompleteUsage(projected)) return false;
  if (provider !== "codex-luna") return sameUsage(aggregate, projected);
  return [
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "total_tokens",
  ].every((field) => aggregate[field] === projected[field]);
}

export function collectProviderFailureObservability({
  provider,
  result,
  outputRoot,
  usageRoot,
  planStage,
  invocationClass,
}) {
  const projection = provider === "codex-luna" ? macosCodexInvocationProjection : claudeDeepseekInvocationProjection;
  let rawInvocations = [];
  let explicitZeroCallLedger = false;
  const ledgerPath = path.join(outputRoot, "model-invocations.json");
  if (fs.existsSync(ledgerPath)) {
    const ledger = readJsonOrNull(ledgerPath);
    const ledgerValid = provider === "claude-deepseek"
      ? validClaudeDeepseekInvocationLedger(planStage, ledger)
      : validEvidenceV2ProviderInvocationLedger(planStage, ledger);
    if (ledgerValid) rawInvocations = ledger.invocations;
    else if (validExplicitZeroCallLedger(outputRoot, ledger)) explicitZeroCallLedger = true;
  }
  if (rawInvocations.length === 0) rawInvocations = readProviderRoleReceipts(usageRoot, provider, planStage.hard_caps);
  const invocations = rawInvocations.map((invocation) => projection(invocation, planStage.hard_caps, invocationClass));
  const knownInvocationUsage = invocations
    .filter((invocation) => invocation.usage_complete === true && isCompleteUsage(invocation.usage))
    .map((invocation) => invocation.usage);
  const projectedUsage = knownInvocationUsage.length === invocations.length && invocations.length > 0
    ? sumUsage(knownInvocationUsage)
    : null;
  const knownPartialUsage = knownInvocationUsage.length > 0 ? sumUsage(knownInvocationUsage) : null;
  const aggregate = modelUsageAggregate(outputRoot);
  if (projectedUsage !== null) {
    return {
      invocations,
      usage: projectedUsage,
      usage_complete: aggregate.present && providerAggregateMatches(provider, aggregate.usage, projectedUsage),
    };
  }
  if (invocations.length > 0) {
    return {
      invocations,
      usage: knownPartialUsage ?? aggregate.usage ?? (isCompleteUsage(result?.usage) ? result.usage : undefined),
      usage_complete: false,
    };
  }
  if (aggregate.usage !== null) {
    return {
      invocations: [],
      usage: aggregate.usage,
      usage_complete: explicitZeroCallLedger
        && aggregate.complete
        && aggregate.failure_ledger
        && sameUsage(aggregate.usage, zeroUsage()),
    };
  }
  return {
    invocations: [],
    usage: isCompleteUsage(result?.usage) ? result.usage : undefined,
    usage_complete: false,
  };
}

export function providerRunnerFailureResult({
  provider,
  result,
  attemptRoot,
  outputRoot,
  usageRoot,
  planStage,
  invocationClass,
  fallbackCode,
}) {
  const observed = collectProviderFailureObservability({
    provider,
    result,
    outputRoot,
    usageRoot,
    planStage,
    invocationClass,
  });
  const receipts = collectProviderFailureReceipts({ provider, outputRoot });
  return {
    ...result,
    failure_domain: result.status === "ERROR" ? "HARNESS" : "CONTRACT",
    code: providerRunnerFailureCode({ result, attemptRoot, fallbackCode }),
    ...observed,
    ...receipts,
  };
}

async function runMacosClaudeDeepseekGate(context, stage, { workflow }) {
  const outputRoot = gateRoot(context, stage);
  const scratchRoot = quickValidationScratchRoot(context, workflow === "methods" ? "macos-claude-deepseek-methods" : "macos-claude-deepseek-e2e");
  ensureDirectory(scratchRoot);
  let pythonEntry;
  let providerInputs = null;
  try {
    pythonEntry = macosCodexPythonEntry(context.repoRoot);
    if (workflow === "e2e") providerInputs = evidenceV2ProviderRuntimeInputs(context);
  }
  catch (error) { return { status: "BLOCKED", failure_domain: "INFRA", code: String(error?.message ?? "CLAUDE_DEEPSEEK_PYTHON_RUNTIME_MISSING"), elapsed_seconds: 0 }; }
  const usageRoot = path.join(context.attemptRoot, "payload", "model-usage", workflow === "methods" ? "macos-claude-deepseek-methods" : "macos-claude-deepseek-e2e");
  const runner = path.join(context.sourceSnapshotRoot, "tools", "test-flow", "quick-validation", "claude-deepseek", "runtime", workflow === "methods" ? "claude-deepseek-methods-runner.mjs" : "claude-deepseek-e2e-runner.mjs");
  const common = [
    runner,
    "--source-root", context.sourceSnapshotRoot,
    "--claude-entry", context.options.claudeEntry,
    "--claude-settings", context.options.claudeSettings,
    "--python-entry", pythonEntry,
    "--cache-root", context.options.cacheRoot ?? path.join(context.repoRoot, ".tmp", "test-flow-cache"),
    "--work-root", path.join(scratchRoot, "work"),
    "--private-root", path.join(scratchRoot, "private"),
    "--evidence-root", outputRoot,
    "--usage-root", usageRoot,
    "--run-id", path.basename(context.attemptRoot),
  ];
  const releaseCaseRoot = path.join(context.sourceSnapshotRoot, "tests", "cases", "release", "rpc-timeout-anonymized");
  const args = workflow === "methods" ? [
    ...common,
    "--meta-skill-root", path.join(context.sourceSnapshotRoot, ".claude", "skills", "wiki-to-logparse-diagnosis-skill"),
    "--wiki", path.join(releaseCaseRoot, "input", "wiki.md"),
    "--oracle", path.join(releaseCaseRoot, "oracle.json"),
    "--module", "rpc",
    ...(context.planStage.invocation_caps.length === 0 ? ["--verify-cache-only"] : []),
  ] : [
    ...common,
    "--runtime-root", providerInputs.sourceRoot,
    "--registration-root", providerInputs.registrationRoot,
    "--source-snapshot-digest", providerInputs.sourceSnapshotDigest,
    "--core-verdict", providerInputs.coreVerdictPath,
    "--scenario", providerInputs.scenario,
  ];
  const result = await runProcess({
    repoRoot: context.repoRoot,
    attemptRoot: context.attemptRoot,
    stage,
    command: process.execPath,
    args,
    cwd: context.sourceSnapshotRoot,
    hardTimeoutSeconds: stage.timeout_seconds - 30,
    noProgressSeconds: context.planStage.no_progress_seconds,
    rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
    eventWriter: context.eventWriter,
    executionId: gateExecutionId(stage, context.gateId),
    pollMilliseconds: context.policies.poll_milliseconds,
    progressAllowlistVersion: context.policies.progress_allowlist_version,
  });
  if (result.status !== "PASS") {
    return providerRunnerFailureResult({
      provider: "claude-deepseek",
      result,
      attemptRoot: context.attemptRoot,
      outputRoot,
      usageRoot,
      planStage: context.planStage,
      invocationClass: workflow === "methods" ? "claude-deepseek-registration-generation" : "claude-deepseek-macos-e2e",
      fallbackCode: "CLAUDE_DEEPSEEK_RUNNER_FAILED",
    });
  }
  let gate;
  let ledger;
  try {
    gate = JSON.parse(fs.readFileSync(path.join(outputRoot, "adapter-receipt.json"), "utf8"));
    ledger = JSON.parse(fs.readFileSync(path.join(outputRoot, "model-invocations.json"), "utf8"));
  } catch {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CLAUDE_DEEPSEEK_GATE_RECEIPT_INVALID" };
  }
  if (gate.status !== "PASS" || !validClaudeDeepseekInvocationLedger(context.planStage, ledger)) return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CLAUDE_DEEPSEEK_GATE_RECEIPT_INVALID" };
  const invocationClass = workflow === "methods" ? "claude-deepseek-registration-generation" : "claude-deepseek-macos-e2e";
  const invocations = ledger.invocations.map((invocation) => claudeDeepseekInvocationProjection(invocation, context.planStage.hard_caps, invocationClass));
  return { ...result, status: "PASS", failure_domain: null, code: null, invocations, usage: sumUsage(invocations.map((invocation) => invocation.usage)), usage_complete: true, adapter_receipt: gate };
}

async function crossJob(context, stage) {
  const dockerBoundary = probeDockerRuntimeBoundary(context);
  if (dockerBoundary !== null) return dockerBoundary;
  const adapter = context.options.crossJobAdapter;
  if (!adapter) return { status: "BLOCKED", failure_domain: "INFRA", code: "CROSS_JOB_ADAPTER_MISSING", elapsed_seconds: 0 };
  if (!path.isAbsolute(adapter) || !fs.existsSync(adapter)) return { status: "BLOCKED", failure_domain: "INFRA", code: "CROSS_JOB_ADAPTER_INVALID", elapsed_seconds: 0 };
  const adapterArguments = [];
  const add = (name, value) => {
    if (value !== undefined && value !== null && value !== "") adapterArguments.push(name, String(value));
  };
  add("--stage", stage.id);
  add("--repo-root", context.sourceSnapshotRoot);
  add("--attempt-root", context.attemptRoot);
  add("--client", context.client);
  add("--track", context.track);
  add("--source-snapshot-digest", context.sourceSnapshotDigest);
  add("--source-snapshot-manifest", context.sourceSnapshotManifestPath);
  add("--claude-entry", context.options.claudeEntry);
  add("--claude-settings", context.options.claudeSettings);
  add("--docker-context", context.options.dockerContext ?? "default");
  add("--cache-root", context.options.cacheRoot ?? path.join(context.repoRoot, ".tmp", "test-flow-cache"));
  add("--logparse-source", context.options.logparseSource);
  add("--mcp-source", context.options.mcpSource);
  add(
    "--generated-skill-root",
    path.join(
      context.attemptRoot,
      "payload",
      "stages",
      "real.skill-generation",
      "gates",
      "real.agent.skill-generation",
      "generated-skill",
    ),
  );
  add("--base-image", RELEASE_BASE_IMAGE);
  add("--server-image-id", context.plan.release_inputs?.image?.server?.image_id);
  add("--client-image-id", context.plan.release_inputs?.image?.client?.image_id);
  add("--runtime-profile-digest", context.plan.runtime_profile_digest);
  add("--chrome-version", context.plan.release_inputs?.browser?.version);
  add("--chrome-sha256", context.plan.release_inputs?.browser?.executable_sha256);
  add("--gate-id", context.gateId);
  add("--resource-registry", context.resources.filePath);
  add("--resource-label", `problem-locator.test-flow.run=${path.basename(context.attemptRoot)}`);
  if (context.planStage.hard_caps) {
    add("--max-turns", context.planStage.hard_caps.max_turns);
    add("--max-total-tokens", context.planStage.hard_caps.max_total_tokens);
    add("--max-budget-usd", context.planStage.hard_caps.max_budget_usd);
    add("--hard-timeout-seconds", context.planStage.hard_caps.hard_timeout_seconds);
  }
  const serviceCaps = context.runtimeProfile.real_caps.service_agent;
  add("--service-agent-max-turns", serviceCaps.max_turns);
  add("--service-agent-max-total-tokens", serviceCaps.max_total_tokens);
  add("--service-agent-max-budget-usd", serviceCaps.max_budget_usd);
  add("--service-agent-hard-timeout-seconds", serviceCaps.hard_timeout_seconds);
  if (stage.id === "journey.cross-job.environment") adapterArguments.push("--fresh-data-root");
  if (context.restoredCheckpoint) {
    adapterArguments.push(
      "--restored-data-root", context.restoredCheckpoint.state_root,
      "--restored-continuation", context.restoredCheckpoint.continuation_path,
      "--restored-checkpoint-id", context.restoredCheckpoint.checkpoint_id,
    );
  }
  const stageIndex = context.plan.stages.findIndex((candidate) => candidate.id === stage.id);
  const laterExecutedJourney = context.plan.stages.slice(stageIndex + 1).some((candidate) =>
    candidate.decision === "RUN" && candidate.id.startsWith("journey.cross-job."));
  if (!laterExecutedJourney) adapterArguments.push("--terminal-after-stage");
  const checkpointStage = stage.id === "journey.cross-job.diagnose" ? "journey.cross-job.review" : stage.id;
  if (["journey.cross-job.route", "journey.cross-job.upload", "journey.cross-job.review", "journey.cross-job.publish-restart"].includes(checkpointStage)) {
    adapterArguments.push(
      "--checkpoint-output-source",
      path.join(context.attemptRoot, "payload", "stages", checkpointStage, "checkpoint-source.json"),
    );
  }
  const nodeAdapter = adapter.endsWith(".mjs");
  const result = await runProcess({
    repoRoot: context.repoRoot,
    attemptRoot: context.attemptRoot,
    stage,
    command: nodeAdapter ? process.execPath : adapter,
    args: nodeAdapter ? [adapter, ...adapterArguments] : adapterArguments,
    cwd: context.repoRoot,
    hardTimeoutSeconds: stage.timeout_seconds,
    noProgressSeconds: context.policies.real_no_progress_seconds,
    rawLogLimitBytes: context.policies.raw_log_file_limit_bytes,
    eventWriter: context.eventWriter,
    executionId: gateExecutionId(stage, context.gateId),
    pollMilliseconds: context.policies.poll_milliseconds,
    progressAllowlistVersion: context.policies.progress_allowlist_version,
  });
  const postRunDockerBoundary = probeDockerRuntimeBoundary(context, result);
  if (postRunDockerBoundary !== null) return postRunDockerBoundary;
  const receiptPath = path.join(context.attemptRoot, "payload", "stages", stage.id, "adapter-result.json");
  if (!fs.existsSync(receiptPath)) {
    if (result.status === "ERROR") return { ...result, failure_domain: "HARNESS", code: result.termination?.trigger ?? "CROSS_JOB_EVIDENCE_ERROR" };
    if (result.status === "INCONCLUSIVE") return { ...result, failure_domain: "EXTERNAL", code: result.termination?.trigger ?? "EXTERNAL_INCONCLUSIVE" };
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CROSS_JOB_RECEIPT_MISSING" };
  }
  let receipt;
  let generatedSkill;
  try {
    receipt = JSON.parse(fs.readFileSync(receiptPath, "utf8"));
    generatedSkill = generatedSkillBoundary(context);
  } catch {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CROSS_JOB_RECEIPT_INVALID" };
  }
  const receiptStatuses = new Set(["PASS", "FAIL", "INCONCLUSIVE", "BLOCKED", "ERROR"]);
  if (receipt.schema_version !== 3 || !receiptStatuses.has(receipt.status) || receipt.stage_id !== stage.id || receipt.gate_id !== context.gateId || receipt.runtime_profile_digest !== context.plan.runtime_profile_digest) {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CROSS_JOB_RECEIPT_INVALID" };
  }
  if (receipt.status === "PASS" && !validCrossJobPassRuntimeBoundary(receipt, { plan: context.plan, generatedSkill })) {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CROSS_JOB_RUNTIME_BOUNDARY_INVALID" };
  }
  if (receipt.status === "PASS" && stage.id === "journey.cross-job.environment" && !validLinuxClientBrowserCapabilityEvidence(context, receipt)) {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CROSS_JOB_BROWSER_CAPABILITY_RECEIPT_INVALID" };
  }
  if (receipt.status === "PASS" && stage.id === "journey.cross-job.upload" && receipt.browser_upload?.status !== "PASS") {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CROSS_JOB_BROWSER_UPLOAD_RECEIPT_INVALID" };
  }
  if (receipt.status === "PASS" && stage.id === "journey.cross-job.diagnose" && receipt.browser_api?.status !== "PASS") {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CROSS_JOB_BROWSER_API_RECEIPT_INVALID" };
  }
  if (receipt.status === "PASS" && stage.id === "journey.cross-job.diagnose" && !validMethodsV2OracleEvidence(context, receipt, generatedSkill)) {
    return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CROSS_JOB_METHODS_V2_ORACLE_EVIDENCE_INVALID" };
  }
  if (result.status === "ERROR") return { ...result, failure_domain: "HARNESS", code: result.termination?.trigger ?? "CROSS_JOB_EVIDENCE_ERROR" };
  if (result.status === "INCONCLUSIVE") return { ...result, failure_domain: "EXTERNAL", code: result.termination?.trigger ?? "EXTERNAL_INCONCLUSIVE" };
  if (receipt.status !== "PASS") {
    if (receipt.topology === "dual-linux-containers"
      && (receipt.failure_domain === "BROWSER" || /^CHROME_(CAPABILITY|UPLOAD|RESOLVED-API)_/.test(receipt.code ?? ""))
      && !validLinuxClientBrowserFailureEvidence(context, stage, receipt)) {
      return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CROSS_JOB_BROWSER_FAILURE_EVIDENCE_INVALID" };
    }
    return {
      ...result,
      status: receipt.status,
      failure_domain: receipt.failure_domain ?? "HARNESS",
      code: receipt.code ?? "CROSS_JOB_STAGE_FAILED",
      usage: receipt.usage ?? result.usage,
      usage_complete: receipt.usage_complete === true,
      effective_caps: receipt.effective_caps ?? null,
      invocations: receipt.invocations ?? [],
      adapter_receipt: {
        stage_id: receipt.stage_id,
        client_tool_calls: receipt.client_tool_calls ?? 0,
        server_tool_calls: receipt.server_tool_calls ?? 0,
        checkpoint_ready: receipt.checkpoint_ready ?? false,
        browser_upload: receipt.browser_upload ?? null,
        browser_api: receipt.browser_api ?? null,
        browser_capability: receipt.browser_capability ?? null,
        browser_failure: receipt.browser_failure ?? null,
        methods_v2: receipt.methods_v2 ?? null,
      },
    };
  }
  if (result.status !== "PASS") return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "CROSS_JOB_PROCESS_RECEIPT_MISMATCH" };
  return {
    ...result,
    usage: receipt.usage ?? result.usage,
    usage_complete: receipt.usage_complete === true,
    effective_caps: receipt.effective_caps ?? null,
    invocations: receipt.invocations ?? [],
    fresh_admission: receipt.fresh_admission ?? null,
    adapter_receipt: {
      stage_id: receipt.stage_id,
      client_tool_calls: receipt.client_tool_calls ?? 0,
      server_tool_calls: receipt.server_tool_calls ?? 0,
      checkpoint_ready: receipt.checkpoint_ready ?? false,
      restart_verified: receipt.restart_verified ?? false,
      browser_upload: receipt.browser_upload ?? null,
      browser_api: receipt.browser_api ?? null,
      browser_capability: receipt.browser_capability ?? null,
      browser_failure: receipt.browser_failure ?? null,
      methods_v2: receipt.methods_v2 ?? null,
      topology: receipt.topology,
      runtime_images: receipt.runtime_images,
      runtime_resources: receipt.runtime_resources,
      generated_skill: receipt.generated_skill,
    },
  };
}

function reviewObservation(context) {
  const partsRoot = path.join(context.attemptRoot, "payload", "events", "parts");
  const streams = fs.existsSync(partsRoot)
    ? fs.readdirSync(partsRoot).filter((name) => name.endsWith(".journey.ndjson")).sort()
    : [];
  if (streams.length === 0) return { status: "ERROR", failure_domain: "HARNESS", code: "REVIEW_EVENT_STREAM_MISSING", elapsed_seconds: 0 };
  const events = streams.flatMap((name) => fs.readFileSync(path.join(partsRoot, name), "utf8").split("\n").filter(Boolean).map((line) => JSON.parse(line)));
  const reviewEvents = events.filter((event) => event?.data?.job_type === "REVIEW" && typeof event.job_id === "string");
  const jobIds = [...new Set(reviewEvents.map((event) => event.job_id))];
  if (jobIds.length !== 1) return { status: "FAIL", failure_domain: "CONTRACT", code: "REVIEW_JOB_IDENTITY_AMBIGUOUS", elapsed_seconds: 0 };
  const reviewJob = reviewEvents.filter((event) => event.job_id === jobIds[0]);
  const required = ["job.pending_persisted", "job.claimed", "job.outcome.produced", "job.outcome.applied"];
  const producerIds = [...new Set(reviewJob.map((event) => event.producer_id))];
  const ordinals = required.map((type) => reviewJob.find((event) => event.event_type === type)?.seq ?? null);
  const failure = reviewJob.some((event) => ["job.claim.failed", "job.outcome.rejected", "job.outcome.stale"].includes(event.event_type));
  const queued = reviewJob.some((event) => ["job.queued", "job.queue.duplicate"].includes(event.event_type));
  const ordered = producerIds.length === 1 && ordinals.every(Number.isInteger) && ordinals.every((value, index) => index === 0 || value > ordinals[index - 1]);
  const receipt = {
    schema_version: 2,
    status: queued && ordered && !failure ? "PASS" : "FAIL",
    review_job_id: jobIds[0],
    observed_review_events: reviewJob.length,
    producer_id: producerIds.length === 1 ? producerIds[0] : null,
    queued,
    ordered,
    failure_observed: failure,
  };
  writeJsonSync(path.join(gateRoot(context, { id: "journey.cross-job.review" }), "review-observation.json"), receipt);
  if (receipt.status !== "PASS") return { status: "FAIL", failure_domain: "CONTRACT", code: "REVIEW_OBSERVATION_INCOMPLETE", elapsed_seconds: 0 };
  return { status: "PASS", failure_domain: null, code: null, elapsed_seconds: 0, review_job_id: jobIds[0], observed_review_events: reviewJob.length };
}

function realEnvironment(context, profile) {
  if (profile === "real-logparse") {
    const source = context.options.logparseSource;
    const python = process.env.TEST_FLOW_LOGPARSE_PYTHON;
    if (!source || !python) return { error: "LOGPARSE_RUNTIME_MISSING" };
    return {
      env: {
        LOGPARSE_REPO: source,
        LOGPARSE_CONFIG_PATH: process.env.TEST_FLOW_LOGPARSE_CONFIG ?? path.join(source, "config.yaml"),
        LOGPARSE_PYTHON: python,
      },
    };
  }
  const command = agentCommand(
    context,
    profile === "real-skill-generation" ? "skill-generation" : "job",
  );
  if (!command) return { error: "CLAUDE_COMMAND_OR_HARD_CAP_MISSING" };
  const runtime = preparedClaudeRuntime(context);
  if (!runtime) return { error: "CLAUDE_RUNTIME_MISSING" };
  const common = {
    ...runtime.environment,
    TEST_FLOW_AGENT_BACKEND_WALL_TIME_SECONDS: String(Math.min(
      context.planStage.timeout_seconds - 30,
      context.planStage.hard_caps.hard_timeout_seconds + 30,
    )),
    S08_REAL_AGENT_COMMAND: command,
    S08_REAL_GENERIC_LOCATOR_AGENT_COMMAND: command,
    S08_REAL_SKILL_GENERATION_AGENT_COMMAND: command,
    S08_REAL_ROUTE_AGENT_COMMAND: command,
    S08_REAL_REVIEW_AGENT_COMMAND: command,
  };
  if (profile === "real-agent-backend") return { env: { ...common, S08_REAL_AGENT_GATE: "1" } };
  if (profile === "real-generic-locator") {
    const skillName = "generic-problem-locator-dual-mode";
    const skillPath = path.join(
      context.repoRoot,
      "tests",
      "fixtures",
      "components",
      skillName,
    );
    if (!fs.existsSync(path.join(skillPath, "SKILL.md"))) return { error: "GENERIC_LOCATOR_SKILL_MISSING" };
    const skillRoot = path.join(runtime.config, "skills");
    const installed = path.join(skillRoot, skillName);
    ensureDirectory(skillRoot);
    if (!fs.existsSync(installed)) fs.cpSync(skillPath, installed, { recursive: true, errorOnExist: true, force: false });
    return { env: { ...common, S08_REAL_GENERIC_LOCATOR_GATE: "1" } };
  }
  if (profile === "real-skill-generation") {
    const skillName = "wiki-to-diagnosis-skill";
    const skillPath = path.join(context.repoRoot, ".agents", "skills", skillName);
    if (!fs.existsSync(path.join(skillPath, "SKILL.md"))) return { error: "WIKI_SKILL_GENERATOR_MISSING" };
    const skillRoot = path.join(runtime.config, "skills");
    const installed = path.join(skillRoot, skillName);
    ensureDirectory(skillRoot);
    if (!fs.existsSync(installed)) fs.cpSync(skillPath, installed, { recursive: true, errorOnExist: true, force: false });
    return {
      env: {
        ...common,
        S08_REAL_SKILL_GENERATION_GATE: "1",
        S08_REAL_SKILL_GENERATION_AUDIT_PATH: path.join(
          context.gateRoot,
          "scenario-evaluation-audit.json",
        ),
        S08_REAL_SKILL_GENERATION_OUTPUT_ROOT: path.join(
          context.gateRoot,
          "generated-skill",
        ),
        S08_REAL_SKILL_GENERATION_RECEIPT_PATH: path.join(
          context.gateRoot,
          "generated-skill.json",
        ),
        S08_RELEASE_CASES_ROOT: path.join(context.repoRoot, "tests", "cases", "release"),
      },
    };
  }
  if (profile === "real-route") return { env: { ...common, S08_REAL_ROUTE_AGENT_GATE: "1" } };
  if (profile === "real-review") return { env: { ...common, S08_REAL_REVIEW_AGENT_GATE: "1" } };
  return { error: "REAL_ENVIRONMENT_PROFILE_UNSUPPORTED" };
}

export async function executeGate(context, stage, gateId, gate) {
  const root = path.join(context.attemptRoot, "payload", "stages", stage.id, "gates", gateId);
  ensureDirectory(root);
  const scoped = { ...context, gateId, gateRoot: root };
  if (gate.kind === "node-test") return nodeTestAction(scoped, stage, gate);
  if (gate.kind === "repository-check") return repositoryCheck(scoped, stage, gate);
  if (gate.kind === "pytest") {
    let selectors = gate.selectors ?? [];
    let selection = null;
    if (gate.selector_mode === "affected") {
      selection = planAffectedSelection(context.repoRoot, context.changedFiles);
      if (selection.defer_to_full) {
        const summary = { schema_version: 2, tests: 0, passed: 0, failures: 0, errors: 0, skipped: 0, executed: 0, not_required: true };
        writeJsonSync(path.join(root, "pytest-summary.json"), summary);
        return { status: "NOT_REQUIRED", failure_domain: null, code: "AFFECTED_SCOPE_DEFERRED_TO_FULL", elapsed_seconds: 0, selection, pytest: summary };
      }
      selectors = selection.selectors;
    }
    let environment = {};
    if (gate.environment_profile) {
      const prepared = realEnvironment(scoped, gate.environment_profile);
      if (prepared.error) return { status: "BLOCKED", failure_domain: "INFRA", code: prepared.error, elapsed_seconds: 0 };
      environment = prepared.env;
    }
    let result = await pytestAction(scoped, stage, selectors, {
      extra: gate.pytest_args ?? [],
      env: environment,
      real: Boolean(gate.environment_profile),
      minPassed: gate.min_passed,
      skipPolicy: gate.skip_policy,
      selection,
      isolatedAgent: Boolean(gate.environment_profile && gate.environment_profile !== "real-logparse"),
    });
    if (result.status === "PASS" && gate.result_receipt === EVIDENCE_V2_CORE_RECEIPT) {
      try {
        const coreVerdict = materializeEvidenceV2CoreVerdict({
          sourceSnapshotDigest: context.sourceSnapshotDigest,
          sourceSnapshotRoot: context.sourceSnapshotRoot,
          gateRoot: root,
        });
        result = { ...result, core_verdict: coreVerdict };
      } catch (error) {
        result = {
          ...result,
          status: "ERROR",
          failure_domain: "HARNESS",
          code: error?.code ?? "EVIDENCE_V2_CORE_RECEIPT_INVALID",
        };
      }
    }
    if (gate.environment_profile && gate.environment_profile !== "real-logparse") {
      try {
        const modelUsage = collectIsolatedModelUsage(scoped, gate.environment_profile);
        return { ...result, invocations: modelUsage.invocations, usage: modelUsage.usage, usage_complete: true };
      } catch (error) {
        if (result.status !== "PASS" && error?.message === "ISOLATED_MODEL_USAGE_RECEIPT_MISSING") {
          return { ...result, invocations: [], usage_complete: false };
        }
        return { ...result, status: "ERROR", failure_domain: "HARNESS", code: "ISOLATED_MODEL_USAGE_RECEIPT_INVALID" };
      }
    }
    return { ...result, invocations: [] };
  }
  if (gate.kind === "capability-adapter") {
    if (gate.adapter === "host-capability") return hostCapability(scoped, stage);
    if (gate.adapter === "server-linux-capability") return serverLinuxCapability(scoped, stage, gate);
    if (gate.adapter === "macos-codex-luna-methods") return runMacosCodexLunaGate(scoped, stage, { workflow: "methods" });
    if (gate.adapter === "macos-codex-luna-e2e") {
      const result = await runMacosCodexLunaGate(scoped, stage, { workflow: "e2e" });
      return attachEvidenceV2ModelCert(result, { context, gate, gateRoot: root });
    }
    if (gate.adapter === "macos-claude-deepseek-methods") return runMacosClaudeDeepseekGate(scoped, stage, { workflow: "methods" });
    if (gate.adapter === "macos-claude-deepseek-e2e") {
      const result = await runMacosClaudeDeepseekGate(scoped, stage, { workflow: "e2e" });
      return attachEvidenceV2ModelCert(result, { context, gate, gateRoot: root });
    }
    if (gate.adapter === "evidence-v2-release-verdict") return evidenceV2ReleaseVerdict(scoped);
  }
  if (gate.kind === "cross-job-adapter") return crossJob(scoped, stage);
  if (gate.kind === "observation" && gate.observation === "review-state-transition") return reviewObservation(scoped);
  return { status: "ERROR", failure_domain: "HARNESS", code: "GATE_EXECUTOR_NOT_IMPLEMENTED", elapsed_seconds: 0 };
}

export { affectedSelectors, pythonRuntime };
