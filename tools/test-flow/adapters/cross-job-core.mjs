#!/usr/bin/env node
// Shared first-party Windows/macOS/Linux Client to Linux Server CrossJob core.
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import {
  RELEASE_CLAUDE_CLI_SHA256,
  RELEASE_CLAUDE_VERSION_OUTPUT,
  RELEASE_CLAUDE_VERSION,
  RELEASE_CHROME_HEADLESS_SHELL_ARCHIVE_SHA256,
  RELEASE_CHROME_HEADLESS_SHELL_EXECUTABLE_SHA256,
  RELEASE_CHROME_HEADLESS_SHELL_PRODUCT,
  RELEASE_CHROME_HEADLESS_SHELL_VERSION,
  RELEASE_CHROME_HEADLESS_SHELL_VERSION_OUTPUT,
  RELEASE_CLIENT_IMAGE,
  RELEASE_LOGPARSE_COMMIT,
  RELEASE_MCP_COMMIT,
  RELEASE_MODEL,
  claudeSettingsIdentity,
  materializeAttemptClaudeSettings,
  materializeClaudeSettings,
  packageTreeIdentity,
  dockerContextArgs,
  validateClaudeDistribution,
} from "../lib/release-inputs.mjs";
import { extractCheckpointSourceArchive } from "../lib/checkpoint.mjs";
import {
  fixedGetCasePollInput,
  fixedGetCasePollingInvariant,
} from "../lib/cross-job-polling.mjs";
import {
  NEGATIVE_PROBE_VALIDATION_FIELDS,
  readRelayedEventPart,
  readServerMcpCorrespondence,
} from "../lib/events.mjs";
import { recoverStageAuditProgress } from "../lib/evidence.mjs";
import {
  discoverReleaseCaseRoot,
  loadReleaseCaseInputs,
  loadReleaseCaseOracle,
  releaseCaseDigests,
} from "../lib/release-case.mjs";
import { verifyMaterializedSourceSnapshot } from "../lib/source-snapshot.mjs";
import {
  METHODS_V2_CAPTURED_FILES,
  validateMethodsV2ExecutionRecords,
  validateMethodsV2RestartSnapshot,
} from "../lib/methods-oracle.mjs";
import {
  isCompleteUsage,
  normalizeUsage,
  sumUsage,
  TOKEN_USAGE_FORMULA,
  zeroUsage,
} from "../lib/usage.mjs";
import { canonicalJson, ensureDirectory, sha256Bytes, sha256File } from "../lib/util.mjs";
import { chromeIdentity, resolveChromeExecutable } from "../lib/browser.mjs";

const MAX_ATTACHMENT_BYTES = 2684354560;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const TOOL_NAMES = Object.freeze([
  "problem_locator_create_case",
  "problem_locator_prepare_attachment",
  "problem_locator_submit_supplement",
  "problem_locator_get_case",
  "problem_locator_resume_case",
  "problem_locator_cancel_case",
  "problem_locator_list_artifacts",
]);
const FULL_TOOL_NAMES = Object.freeze(TOOL_NAMES.map((name) => `mcp__problem-locator__${name}`));
const INSTANCE_ORDER = Object.freeze(["route", "upload", "diagnose", "restart"]);
const DUAL_LINUX_TOPOLOGY = "dual-linux-containers";
const LINUX_CLIENT_HOME = "/client-home";
const LINUX_BROWSER_RUNNER_RELATIVE = "tools/test-flow/runtime-support/linux_client_browser_runner.py";
const LINUX_BROWSER_RUNNER_CONTAINER = `/workspace/${LINUX_BROWSER_RUNNER_RELATIVE}`;
const LINUX_BROWSER_SUMMARY_PREFIX = "TEST_FLOW_BROWSER_EXECUTION_V1=";
const LINUX_BROWSER_ARGUMENT_PROFILE = "chrome-headless-shell-for-testing-local-v1";
const ADAPTER_STAGE_IDS = new Set([
  "journey.cross-job.environment",
  "journey.cross-job.route",
  "journey.cross-job.upload",
  "journey.cross-job.diagnose",
  "journey.cross-job.publish-restart",
]);

class StageError extends Error {
  constructor(code, status = "ERROR", domain = "HARNESS", browserFailure = null) {
    super(code);
    this.code = code;
    this.status = status;
    this.domain = domain;
    this.browserFailure = browserFailure;
  }
}

function requireCondition(condition, code, status = "ERROR", domain = "HARNESS") {
  if (!condition) throw new StageError(code, status, domain);
}

export function linuxClientUserIdentity({
  uid = typeof process.getuid === "function" ? process.getuid() : null,
  gid = typeof process.getgid === "function" ? process.getgid() : null,
} = {}) {
  requireCondition(Number.isSafeInteger(uid) && uid > 0, "LINUX_CLIENT_NON_ROOT_UID_REQUIRED", "BLOCKED", "INFRA");
  requireCondition(Number.isSafeInteger(gid) && gid >= 0, "LINUX_CLIENT_GID_REQUIRED", "BLOCKED", "INFRA");
  return Object.freeze({ uid, gid, root: false, docker_user: `${uid}:${gid}` });
}

function parseArguments(argv) {
  const values = {};
  const flags = new Set();
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index];
    if (["--fresh-data-root", "--terminal-after-stage"].includes(name)) {
      flags.add(name);
      continue;
    }
    requireCondition(name.startsWith("--") && index + 1 < argv.length, "ADAPTER_ARGUMENT_INVALID");
    values[name.slice(2).replaceAll("-", "_")] = argv[++index];
  }
  return { values, flags };
}

function writeNew(filePath, value) {
  ensureDirectory(path.dirname(filePath));
  const descriptor = fs.openSync(filePath, "wx", 0o600);
  try {
    fs.writeFileSync(descriptor, canonicalJson(value), "utf8");
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

function writeTextNew(filePath, value) {
  ensureDirectory(path.dirname(filePath));
  const descriptor = fs.openSync(filePath, "wx", 0o600);
  try {
    fs.writeFileSync(descriptor, value, "utf8");
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

function scriptJson(value) {
  return JSON.stringify(value)
    .replaceAll("<", "\\u003c")
    .replaceAll("\u2028", "\\u2028")
    .replaceAll("\u2029", "\\u2029");
}

function atomicState(filePath, value) {
  ensureDirectory(path.dirname(filePath));
  const temporary = `${filePath}.${process.pid}.${crypto.randomUUID()}.tmp`;
  fs.writeFileSync(temporary, canonicalJson(value), { encoding: "utf8", mode: 0o600, flag: "wx" });
  fs.renameSync(temporary, filePath);
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function validateGeneratedSkillRoot(rootPath, attemptRoot) {
  requireCondition(rootPath && path.isAbsolute(rootPath), "GENERATED_SKILL_ROOT_REQUIRED", "BLOCKED", "INFRA");
  const root = path.resolve(rootPath);
  const attempt = path.resolve(attemptRoot);
  requireCondition(root.startsWith(`${attempt}${path.sep}`), "GENERATED_SKILL_ROOT_OUTSIDE_ATTEMPT", "BLOCKED", "INFRA");
  requireCondition(fs.existsSync(root) && fs.lstatSync(root).isDirectory() && !fs.lstatSync(root).isSymbolicLink(), "GENERATED_SKILL_ROOT_MISSING", "BLOCKED", "INFRA");
  const realRoot = fs.realpathSync.native(root);
  const realAttempt = fs.realpathSync.native(attempt);
  const contained = process.platform === "win32"
    ? realRoot.toLowerCase().startsWith(`${realAttempt.toLowerCase()}${path.sep}`)
    : realRoot.startsWith(`${realAttempt}${path.sep}`);
  requireCondition(contained, "GENERATED_SKILL_ROOT_REALPATH_INVALID", "BLOCKED", "INFRA");
  const identity = packageTreeIdentity(root);
  requireCondition(identity.status === "PRESENT", "GENERATED_SKILL_TREE_INVALID", "FAIL", "CONTRACT");
  const registrationEntries = fs.readdirSync(root, { withFileTypes: true });
  requireCondition(registrationEntries.length === 1 && registrationEntries[0].isDirectory(), "GENERATED_SKILL_REGISTRATION_SET_INVALID", "FAIL", "CONTRACT");
  const registrationId = registrationEntries[0].name;
  requireCondition(/^[a-z0-9][a-z0-9-]{0,127}$/.test(registrationId), "GENERATED_SKILL_REGISTRATION_ID_INVALID", "FAIL", "CONTRACT");
  const registrationRoot = path.join(root, registrationId);
  const registrationNames = fs.readdirSync(registrationRoot).sort();
  requireCondition(canonicalJson(registrationNames) === canonicalJson(["package", "registration-template.json"]), "GENERATED_SKILL_REGISTRATION_FILES_INVALID", "FAIL", "CONTRACT");
  const registration = readJson(path.join(registrationRoot, "registration-template.json"));
  requireCondition(registration?.registration_id === registrationId, "GENERATED_SKILL_REGISTRATION_METADATA_INVALID", "FAIL", "CONTRACT");
  const packageRoot = path.join(registrationRoot, "package");
  const skillEntries = fs.readdirSync(packageRoot, { withFileTypes: true });
  requireCondition(skillEntries.length === 1 && skillEntries[0].isDirectory(), "GENERATED_SKILL_PACKAGE_SET_INVALID", "FAIL", "CONTRACT");
  const skillName = skillEntries[0].name;
  const skillRoot = path.join(packageRoot, skillName);
  const skillNames = fs.readdirSync(skillRoot).sort();
  requireCondition(canonicalJson(skillNames) === canonicalJson(["SKILL.md", "methods.json", "references"]), "GENERATED_SKILL_PACKAGE_FILES_INVALID", "FAIL", "CONTRACT");
  const references = fs.readdirSync(path.join(skillRoot, "references"), { withFileTypes: true });
  requireCondition(references.length > 0 && references.every((entry) => entry.isFile() && entry.name.endsWith(".md")), "GENERATED_SKILL_REFERENCES_INVALID", "FAIL", "CONTRACT");
  const methods = readJson(path.join(skillRoot, "methods.json"));
  requireCondition(methods?.schema_version === 1 && methods.skill_name === skillName && Array.isArray(methods.methods) && methods.methods.length > 0, "GENERATED_SKILL_METHODS_INVALID", "FAIL", "CONTRACT");
  requireCondition(
    registration?.package?.relative_path === `package/${skillName}`
      && registration.package.skill_name === skillName
      && registration.package.source_wiki_sha256 === methods.source_wiki_sha256
      && SHA256.test(methods.source_wiki_sha256 ?? ""),
    "GENERATED_SKILL_PACKAGE_BINDING_INVALID",
    "FAIL",
    "CONTRACT",
  );
  const packageIdentity = packageTreeIdentity(skillRoot);
  requireCondition(packageIdentity.status === "PRESENT", "GENERATED_SKILL_PACKAGE_TREE_INVALID", "FAIL", "CONTRACT");
  const packageEntries = packageIdentity.records
    .filter((entry) => entry.kind === "file")
    .map(({ path: entryPath, size, sha256 }) => ({ path: entryPath, size, sha256 }))
    .sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  const registrationSha256 = sha256File(path.join(registrationRoot, "registration-template.json"));
  const packageTreeSha256 = sha256Bytes(canonicalJson({ version: 1, entries: packageEntries }));
  const combinedSha256 = sha256Bytes(canonicalJson({
    schema_version: 1,
    registration_id: registrationId,
    registration_sha256: registrationSha256,
    package_tree_sha256: packageTreeSha256,
  }));
  const contentTreeSha256 = sha256Bytes(canonicalJson({
    version: 1,
    entries: identity.records
      .filter((entry) => entry.kind === "file")
      .map(({ path: entryPath, size, sha256 }) => ({ path: entryPath, size, sha256 }))
      .sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0),
  }));
  const generationReceiptPath = path.join(path.dirname(root), "generated-skill.json");
  requireCondition(fs.existsSync(generationReceiptPath), "GENERATED_SKILL_GATE_RECEIPT_MISSING", "BLOCKED", "INFRA");
  const generationReceiptMetadata = fs.lstatSync(generationReceiptPath);
  requireCondition(
    generationReceiptMetadata.isFile() && !generationReceiptMetadata.isSymbolicLink() && generationReceiptMetadata.nlink === 1,
    "GENERATED_SKILL_GATE_RECEIPT_INVALID",
    "FAIL",
    "CONTRACT",
  );
  const generationReceipt = readJson(generationReceiptPath);
  requireCondition(
    generationReceipt?.schema_version === 1
      && generationReceipt.status === "PASS"
      && generationReceipt.registration_id === registrationId
      && generationReceipt.skill_name === skillName
      && generationReceipt.source_wiki_sha256 === methods.source_wiki_sha256
      && generationReceipt.registration_sha256 === registrationSha256
      && generationReceipt.package_tree_sha256 === packageTreeSha256
      && generationReceipt.combined_sha256 === combinedSha256,
    "GENERATED_SKILL_GATE_RECEIPT_DRIFT",
    "FAIL",
    "CONTRACT",
  );
  return {
    root,
    registration_root: registrationRoot,
    skill_root: skillRoot,
    registration_id: registrationId,
    skill_name: skillName,
    tree_digest: identity.digest,
    package_digest: packageIdentity.digest,
    registration_sha256: registrationSha256,
    package_tree_sha256: packageTreeSha256,
    combined_sha256: combinedSha256,
    content_tree_sha256: contentTreeSha256,
    generation_receipt_sha256: sha256File(generationReceiptPath),
    source_wiki_sha256: methods.source_wiki_sha256,
    registration,
    methods,
    generation_receipt: generationReceipt,
  };
}

function sortedStrings(value, code) {
  requireCondition(Array.isArray(value) && value.every((item) => typeof item === "string" && item.length > 0) && value.length === new Set(value).size, code, "FAIL", "CONTRACT");
  return [...value].sort();
}

function mapOracleMarkerGroups(groups, generatedMethods, code) {
  const selected = groups.map((group) => {
    const markers = sortedStrings(group, code);
    const candidates = generatedMethods.filter((entry) => markers.every((marker) => (
      entry.semantic_markers.some((declared) => declared === marker)
    )));
    requireCondition(candidates.length > 0, code, "FAIL", "CONTRACT");
    const minimumMarkerCount = Math.min(...candidates.map((entry) => entry.semantic_markers.length));
    const minimal = candidates.filter((entry) => entry.semantic_markers.length === minimumMarkerCount);
    requireCondition(minimal.length === 1, `${code}_AMBIGUOUS`, "FAIL", "CONTRACT");
    return minimal[0].method.id;
  });
  requireCondition(selected.length === new Set(selected).size, `${code}_DUPLICATE`, "FAIL", "CONTRACT");
  return selected;
}

function selectedReleaseCase(repoRoot, generatedSkill) {
  const root = discoverReleaseCaseRoot(path.join(repoRoot, "tests", "cases", "release"));
  const inputs = loadReleaseCaseInputs(root);
  const gateOracle = loadReleaseCaseOracle(root);
  const digests = releaseCaseDigests(root);
  const scenario = inputs.scenarios.find((item) => item.scenario_id === inputs.journey_scenario);
  const scenarioOracle = gateOracle.scenarios.find((item) => item.scenario_id === inputs.journey_scenario);
  requireCondition(scenario && scenarioOracle, "RELEASE_CASE_JOURNEY_SCENARIO_MISSING");
  const product = inputs.product_registration;
  requireCondition(canonicalJson(generatedSkill.registration) === canonicalJson(inputs.registration_template), "GENERATED_SKILL_REGISTRATION_TEMPLATE_DRIFT", "FAIL", "CONTRACT");
  requireCondition(
    generatedSkill.registration_sha256 === sha256File(inputs.registration_template_path),
    "GENERATED_SKILL_REGISTRATION_BYTES_DRIFT",
    "FAIL",
    "CONTRACT",
  );
  requireCondition(
    generatedSkill.registration_id === product.registration_id
      && generatedSkill.skill_name === product.skill_name
      && generatedSkill.generation_receipt.case_id === inputs.case_id
      && generatedSkill.generation_receipt.runtime_ref_id === product.runtime_ref_id
      && generatedSkill.generation_receipt.version === product.version
      && generatedSkill.source_wiki_sha256 === product.source_wiki_sha256
      && generatedSkill.registration.version === product.version
      && generatedSkill.registration.runtime?.preprocessing?.logparse_product === product.logparse_product
      && generatedSkill.registration.runtime?.preprocessing?.logparse_plan?.attachment_requirement === product.attachment_requirement,
    "GENERATED_SKILL_PRODUCT_REGISTRATION_DRIFT",
    "FAIL",
    "CONTRACT",
  );
  const expectedPackage = gateOracle.semantic_oracle.expected_package;
  const methods = generatedSkill.methods;
  requireCondition(
    methods.skill_name === expectedPackage.skill_name
      && methods.source_wiki_sha256 === expectedPackage.source_wiki_sha256
      && canonicalJson(sortedStrings(methods.required_user_inputs, "GENERATED_SKILL_REQUIRED_INPUTS_INVALID")) === canonicalJson(sortedStrings(expectedPackage.required_user_inputs, "RELEASE_CASE_EXPECTED_INPUTS_INVALID"))
      && canonicalJson(sortedStrings(methods.required_artifacts, "GENERATED_SKILL_REQUIRED_ARTIFACTS_INVALID")) === canonicalJson(sortedStrings(expectedPackage.required_artifacts, "RELEASE_CASE_EXPECTED_ARTIFACTS_INVALID"))
      && canonicalJson(sortedStrings(methods.log_derived_fields, "GENERATED_SKILL_LOG_FIELDS_INVALID")) === canonicalJson(sortedStrings(expectedPackage.required_log_derived_fields, "RELEASE_CASE_EXPECTED_LOG_FIELDS_INVALID")),
    "GENERATED_SKILL_METHODS_PACKAGE_DRIFT",
    "FAIL",
    "CONTRACT",
  );
  const methodIds = methods.methods.map((method) => method.id);
  requireCondition(methodIds.length === new Set(methodIds).size, "GENERATED_SKILL_METHOD_IDS_DUPLICATE", "FAIL", "CONTRACT");
  const generatedMethods = gateOracle.semantic_oracle.expected_package.method_marker_sets.map((semantic) => {
    const semanticMarkers = sortedStrings(semantic.all_markers, "RELEASE_CASE_METHOD_MARKERS_INVALID");
    const matches = methods.methods.filter((method) => (
      canonicalJson(sortedStrings(method.evidence_markers, "GENERATED_SKILL_METHOD_MARKERS_INVALID")) === canonicalJson(semanticMarkers)
    ));
    requireCondition(matches.length === 1, "GENERATED_SKILL_METHOD_SEMANTIC_MAPPING_INVALID", "FAIL", "CONTRACT");
    return { semantic_id: semantic.semantic_id, semantic_markers: semanticMarkers, method: matches[0] };
  });
  requireCondition(generatedMethods.length === methods.methods.length && new Set(generatedMethods.map((entry) => entry.method.id)).size === methods.methods.length, "GENERATED_SKILL_METHOD_SET_DRIFT", "FAIL", "CONTRACT");
  requireCondition(scenarioOracle.oracle.expected_status === "RESOLVED", "RELEASE_CASE_EXPECTED_STATUS_INVALID", "FAIL", "CONTRACT");
  requireCondition(scenarioOracle.oracle.required_candidate_marker_groups.length === 0, "RELEASE_CASE_CANDIDATES_PRESENT", "FAIL", "CONTRACT");
  const confirmedMethodIds = mapOracleMarkerGroups(scenarioOracle.oracle.required_confirmed_marker_groups, generatedMethods, "RELEASE_CASE_CONFIRMED_METHOD_MAPPING");
  mapOracleMarkerGroups(scenarioOracle.oracle.required_candidate_marker_groups, generatedMethods, "RELEASE_CASE_UNCONFIRMED_METHOD_MAPPING");
  const requiredEvidenceIdentities = scenarioOracle.oracle.required_evidence_identities.map((identity) => {
    const [mappedMethodId] = mapOracleMarkerGroups([[identity.marker]], generatedMethods, "RELEASE_CASE_EVIDENCE_IDENTITY_MAPPING");
    requireCondition(
      confirmedMethodIds.includes(mappedMethodId),
      "RELEASE_CASE_EVIDENCE_IDENTITY_METHOD_UNCONFIRMED",
      "FAIL",
      "CONTRACT",
    );
    return {
      method_id: mappedMethodId,
      marker: identity.marker,
      identity_tokens: sortedStrings(identity.identity_tokens, "RELEASE_CASE_EVIDENCE_IDENTITY_TOKENS_INVALID"),
    };
  });
  const methodCards = generatedMethods
    .map(({ method }) => ({
      id: method.id,
      priority: method.priority,
      evidence_markers: [...method.evidence_markers],
    }))
    .sort((left, right) => left.priority - right.priority || left.id.localeCompare(right.id));
  const confirmedMethodSet = new Set(confirmedMethodIds);
  const orderedConfirmedMethodIds = methodCards
    .map((method) => method.id)
    .filter((methodId) => confirmedMethodSet.has(methodId));
  return {
    root,
    case_id: inputs.case_id,
    scenario_id: scenario.scenario_id,
    driver: scenario.driver,
    oracle: scenarioOracle.oracle,
    semantic_oracle: gateOracle.semantic_oracle,
    logparse_product: product.logparse_product,
    skill: {
      id: product.registration_id,
      runtime_ref_id: product.runtime_ref_id,
      version: product.version,
      content_hash: generatedSkill.combined_sha256,
      product_digest: generatedSkill.combined_sha256,
      attachment_requirement: product.attachment_requirement,
    },
    result_expectation: {
      case_status: scenarioOracle.oracle.expected_status,
      method_cards: methodCards,
      loaded_method_ids: methodCards.map((method) => method.id),
      confirmed_method_ids: orderedConfirmedMethodIds,
      required_evidence_identities: requiredEvidenceIdentities,
    },
    input_digest: digests.input_digest,
    oracle_digest: digests.oracle_digest,
  };
}

function safeName(prefix, runId, suffix = "") {
  const digest = sha256Bytes(`${runId}:${suffix}`).slice(0, 16);
  return `${prefix}-${digest}${suffix ? `-${suffix}` : ""}`;
}

function dockerSocketMounted(mounts) {
  return (mounts ?? []).some((mount) => [mount?.Source, mount?.Destination]
    .some((entry) => typeof entry === "string" && path.posix.basename(entry.replaceAll("\\", "/")) === "docker.sock"));
}

function exactLoopbackPortBinding(value, port) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  if (canonicalJson(Object.keys(value).sort()) !== canonicalJson(["8000/tcp"])) return false;
  const bindings = value["8000/tcp"];
  return Array.isArray(bindings)
    && bindings.length === 1
    && canonicalJson(Object.keys(bindings[0] ?? {}).sort()) === canonicalJson(["HostIp", "HostPort"])
    && bindings[0].HostIp === "127.0.0.1"
    && bindings[0].HostPort === String(port);
}

export function validServerRuntimeInspection({
  topology,
  stageId,
  state,
  expectedServerImageId,
  expectedRunId,
  server,
  serverImage,
}) {
  const dualLinuxContainers = topology === DUAL_LINUX_TOPOLOGY;
  const expectedInitialContainer = safeName("pltf-server", expectedRunId, "initial");
  const expectedRestartContainer = safeName("pltf-server", expectedRunId, "restart");
  const expectedActiveContainer = stageId === "journey.cross-job.publish-restart"
    ? expectedRestartContainer
    : expectedInitialContainer;
  if (!["host-client", DUAL_LINUX_TOPOLOGY].includes(topology)
    || !/^sha256:[a-f0-9]{64}$/.test(expectedServerImageId ?? "")
    || state?.run_id !== expectedRunId
    || state?.image_id !== expectedServerImageId
    || state?.runtime_images?.server_image_id !== expectedServerImageId
    || state?.initial_container !== expectedInitialContainer
    || state?.restart_container !== expectedRestartContainer
    || state?.active_container !== expectedActiveContainer
    || server?.Name !== `/${state.active_container}`
    || server?.Image !== expectedServerImageId
    || server?.Config?.Image !== expectedServerImageId
    || server?.Config?.Labels?.["problem-locator.test-flow.run"] !== expectedRunId
    || server?.State?.Running !== true
    || serverImage?.Id !== expectedServerImageId
    || serverImage?.Os !== "linux"
    || serverImage?.Architecture !== "amd64"
    || dockerSocketMounted(server?.Mounts)) return false;

  const hostPortBindings = server?.HostConfig?.PortBindings ?? {};
  const publishedPorts = server?.NetworkSettings?.Ports ?? {};
  if (dualLinuxContainers) {
    return Object.keys(hostPortBindings).length === 0
      && Object.values(publishedPorts).every((binding) => binding === null);
  }
  return Number.isSafeInteger(state?.port)
    && state.port > 0
    && state.port <= 65535
    && state.network === null
    && state.client_container === null
    && state.client_image_id === null
    && state.runtime_images?.client_image_id === null
    && state.selected_client_runtime_observed === null
    && exactLoopbackPortBinding(hostPortBindings, state.port)
    && exactLoopbackPortBinding(publishedPorts, state.port);
}

function currentReleaseRuntimeIdentity(configuration) {
  const distribution = validateClaudeDistribution(configuration.claudeEntry);
  requireCondition(distribution.status === "PRESENT", `CLAUDE_DISTRIBUTION_${distribution.code ?? "INVALID"}`, "BLOCKED", "INFRA");
  const settings = claudeSettingsIdentity(configuration.claudeSettings);
  requireCondition(settings.status === "PRESENT", `CLAUDE_SETTINGS_${settings.code ?? "INVALID"}`, "BLOCKED", "INFRA");
  const dualLinuxContainers = configuration.topology === DUAL_LINUX_TOPOLOGY;
  return {
    schema_version: 2,
    orchestrator_distribution_source: {
      entry: distribution.entry,
      version: distribution.version,
      cli_sha256: distribution.cli_sha256,
      package_manifest_sha256: distribution.package_manifest_sha256,
      package_tree_digest: distribution.package_tree_digest,
      tarball_sha256: distribution.tarball_sha256,
      node_version: distribution.node?.version ?? null,
      node_sha256: distribution.node?.sha256 ?? null,
    },
    selected_client_runtime: dualLinuxContainers ? {
      execution_topology: "darwin-orchestrated-linux-container",
      platform: "linux/amd64",
      image_id: configuration.expectedClientImageId,
      node_identity_boundary: "client-image-id",
      node_observation: "runtime-container-probe-required",
      claude_version: RELEASE_CLAUDE_VERSION_OUTPUT,
      claude_cli_sha256: RELEASE_CLAUDE_CLI_SHA256,
    } : {
      execution_topology: "native-host",
      platform: configuration.client,
      image_id: null,
      node_identity_boundary: "node-binary-sha256",
      node_version: distribution.node?.version ?? null,
      node_sha256: distribution.node?.sha256 ?? null,
      claude_version: distribution.version,
      claude_cli_sha256: distribution.cli_sha256,
    },
    settings: {
      endpoint: settings.endpoint,
      model: settings.model,
      fingerprint: settings.fingerprint,
      hooks_copied: settings.hooks_copied,
    },
  };
}

async function verifyRepositoryIdentity(configuration) {
  requireCondition(configuration.repoRoot.startsWith(`${configuration.attemptRoot}${path.sep}`), "RELEASE_SOURCE_SNAPSHOT_OUTSIDE_ATTEMPT", "BLOCKED", "INFRA");
  requireCondition(configuration.sourceSnapshotManifest.startsWith(`${configuration.attemptRoot}${path.sep}`), "RELEASE_SOURCE_MANIFEST_OUTSIDE_ATTEMPT", "BLOCKED", "INFRA");
  const sourceManifest = readJson(configuration.sourceSnapshotManifest);
  requireCondition(sourceManifest?.digest === configuration.sourceSnapshotDigest, "RELEASE_SOURCE_SNAPSHOT_MANIFEST_DRIFT", "BLOCKED", "INFRA");
  const verification = verifyMaterializedSourceSnapshot(configuration.repoRoot, sourceManifest);
  requireCondition(verification.status === "PASS", "RELEASE_SOURCE_SNAPSHOT_DRIFT", "BLOCKED", "INFRA");
}

async function run(command, args, { cwd = undefined, env = process.env, forward = true, maximumBytes = 64 * 1024 * 1024 } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, env, stdio: ["ignore", "pipe", "pipe"] });
    const stdout = [];
    const stderr = [];
    let bytes = 0;
    let terminalError = null;
    const consume = (collection, target, chunk) => {
      if (terminalError) return;
      bytes += chunk.length;
      if (bytes > maximumBytes) {
        terminalError = new StageError("ADAPTER_COMMAND_OUTPUT_LIMIT");
        child.kill("SIGKILL");
        return;
      }
      collection.push(chunk);
      if (forward) target.write(chunk);
    };
    child.stdout.on("data", (chunk) => consume(stdout, process.stdout, chunk));
    child.stderr.on("data", (chunk) => consume(stderr, process.stderr, chunk));
    child.once("error", (error) => { terminalError = terminalError ?? error; });
    child.once("close", (code, signal) => {
      if (terminalError) {
        reject(terminalError);
        return;
      }
      resolve({
        status: code,
        signal,
        stdout: Buffer.concat(stdout).toString("utf8"),
        stderr: Buffer.concat(stderr).toString("utf8"),
      });
    });
  });
}

export { run as runCommandCapture };

async function runHostChromePage(label, page, fixtureBytes = null) {
  const chrome = chromeIdentity();
  const chromeExecutable = resolveChromeExecutable();
  requireCondition(chrome.status === "PRESENT" && chromeExecutable, chrome.code ?? "CHROME_REQUIRED", "BLOCKED", "INFRA");
  const fixtureServer = http.createServer((request, response) => {
    if (request.url === "/fixture" && fixtureBytes !== null) {
      response.writeHead(200, {
        "Cache-Control": "no-store",
        "Content-Length": String(fixtureBytes.length),
        "Content-Type": "application/octet-stream",
      });
      response.end(fixtureBytes);
      return;
    }
    response.writeHead(200, {
      "Cache-Control": "no-store",
      "Content-Type": "text/html; charset=utf-8",
    });
    response.end(page);
  });
  await new Promise((resolve, reject) => {
    fixtureServer.once("error", reject);
    fixtureServer.listen({ host: "127.0.0.1", port: 0 }, resolve);
  });
  const fixtureAddress = fixtureServer.address();
  requireCondition(typeof fixtureAddress === "object" && fixtureAddress && Number.isInteger(fixtureAddress.port), "BROWSER_FIXTURE_ADDRESS_INVALID");
  const browserOrigin = `http://127.0.0.1:${fixtureAddress.port}`;
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), `test-flow-chrome-${label}-`));
  let chromeRun;
  try {
    const chromeArguments = [
      "--headless=new",
      "--disable-background-networking",
      "--disable-component-update",
      "--disable-default-apps",
      "--disable-gpu",
      "--disable-sync",
      "--metrics-recording-only",
      "--no-default-browser-check",
      "--no-first-run",
      "--no-proxy-server",
      `--user-data-dir=${profile}`,
      "--virtual-time-budget=30000",
      "--dump-dom",
      `${browserOrigin}/`,
    ];
    if (typeof process.getuid === "function" && process.getuid() === 0) chromeArguments.unshift("--no-sandbox");
    chromeRun = await run(chromeExecutable, chromeArguments, { forward: false, maximumBytes: 8 * 1024 * 1024 });
  } finally {
    await new Promise((resolve) => fixtureServer.close(resolve));
    fs.rmSync(profile, { recursive: true, force: true });
  }
  requireCondition(chromeRun.status === 0, `CHROME_${label.toUpperCase()}_EXIT_${chromeRun.status}`, "FAIL", "BROWSER");
  const match = chromeRun.stdout.match(/data-result="([A-Za-z0-9+/=]+)"/);
  requireCondition(match, `CHROME_${label.toUpperCase()}_RESULT_MISSING`, "FAIL", "BROWSER");
  let result;
  try {
    result = JSON.parse(Buffer.from(match[1], "base64").toString("utf8"));
  } catch {
    throw new StageError(`CHROME_${label.toUpperCase()}_RESULT_INVALID`, "FAIL", "BROWSER");
  }
  return { browserOrigin, chrome, chromeRun, result };
}

function hasExactKeys(value, expected) {
  return value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && canonicalJson(Object.keys(value).sort()) === canonicalJson([...expected].sort());
}

function validCaptureDigest(value) {
  return hasExactKeys(value, ["byte_count", "sha256", "truncated"])
    && Number.isSafeInteger(value.byte_count)
    && value.byte_count >= 0
    && SHA256.test(value.sha256 ?? "")
    && value.truncated === false;
}

function validLinuxBrowserProcessTree(value) {
  if (!hasExactKeys(value, [
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

function completedLinuxBrowserProcessTree(value) {
  return validLinuxBrowserProcessTree(value)
    && value.session_started === true
    && value.parent_reaped === true
    && value.group_absent === true;
}

export function validLinuxClientBrowserExecution(value, { label, stdout = null } = {}) {
  if (!hasExactKeys(value, [
    "schema_version", "wrapper_status", "failure_code", "label", "argument_profile",
    "home", "browser_started", "browser_exit_code", "browser_signal_number",
    "browser_signal_name", "timed_out", "stdout", "stderr", "cleanup",
  ])
    || value.schema_version !== 1
    || !["PASS", "ERROR"].includes(value.wrapper_status)
    || value.label !== label
    || value.argument_profile !== LINUX_BROWSER_ARGUMENT_PROFILE
    || !hasExactKeys(value.home, ["path", "realpath", "present", "writable"])
    || ![null, LINUX_CLIENT_HOME].includes(value.home.path)
    || ![null, LINUX_CLIENT_HOME].includes(value.home.realpath)
    || typeof value.home.present !== "boolean"
    || typeof value.home.writable !== "boolean"
    || typeof value.browser_started !== "boolean"
    || !(value.browser_exit_code === null || (Number.isSafeInteger(value.browser_exit_code) && value.browser_exit_code >= 0))
    || !(value.browser_signal_number === null || (Number.isSafeInteger(value.browser_signal_number) && value.browser_signal_number > 0))
    || !(value.browser_signal_name === null || /^SIG[A-Z0-9]+$/.test(value.browser_signal_name))
    || typeof value.timed_out !== "boolean"
    || !validCaptureDigest(value.stdout)
    || !validCaptureDigest(value.stderr)
    || !hasExactKeys(value.cleanup, ["http_server_stopped", "profile_removed", "process_tree"])
    || typeof value.cleanup.http_server_stopped !== "boolean"
    || typeof value.cleanup.profile_removed !== "boolean"
    || !validLinuxBrowserProcessTree(value.cleanup.process_tree)) return false;
  if (value.wrapper_status === "PASS" && value.failure_code !== null) return false;
  if (value.wrapper_status === "ERROR" && !/^[A-Z][A-Z0-9_]{0,127}$/.test(value.failure_code ?? "")) return false;
  if (value.wrapper_status === "PASS" && !completedLinuxBrowserProcessTree(value.cleanup.process_tree)) return false;
  if (value.browser_started !== value.cleanup.process_tree.session_started) return false;
  if (value.timed_out && value.cleanup.process_tree.termination_reason !== "TIMEOUT") return false;
  if (!value.timed_out && value.cleanup.process_tree.termination_reason === "TIMEOUT") return false;
  if (value.browser_signal_number === null !== (value.browser_signal_name === null)) return false;
  if (value.timed_out && (value.browser_exit_code !== null || value.browser_signal_number !== null)) return false;
  if (!value.timed_out && value.browser_started && ((value.browser_exit_code === null) === (value.browser_signal_number === null))) return false;
  if (stdout !== null && (value.stdout.byte_count !== Buffer.byteLength(stdout) || value.stdout.sha256 !== sha256Bytes(stdout))) return false;
  return true;
}

export function parseLinuxClientBrowserExecution(chromeRun, label) {
  const summaries = String(chromeRun?.stderr ?? "")
    .split(/\r?\n/)
    .filter((line) => line.startsWith(LINUX_BROWSER_SUMMARY_PREFIX));
  if (summaries.length !== 1) return null;
  const encoded = summaries[0].slice(LINUX_BROWSER_SUMMARY_PREFIX.length);
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)) return null;
  try {
    const value = JSON.parse(Buffer.from(encoded, "base64").toString("utf8"));
    return validLinuxClientBrowserExecution(value, { label, stdout: chromeRun.stdout }) ? value : null;
  } catch {
    return null;
  }
}

function redactedTextCapture(value) {
  const text = String(value ?? "");
  return {
    byte_count: Buffer.byteLength(text),
    sha256: sha256Bytes(text),
    truncated: false,
  };
}

export function buildLinuxClientBrowserFailureReceipt({
  label,
  runId,
  clientContainer,
  clientImageId,
  clientRuntime,
  browser,
  runnerSha256,
  chromeRun,
  execution,
  status = "FAIL",
  failureDomain = "BROWSER",
  code = null,
}) {
  const outerStatus = Number.isSafeInteger(chromeRun?.status) ? chromeRun.status : null;
  const encodedSignalCandidate = outerStatus !== null && outerStatus >= 129 && outerStatus <= 192
    ? outerStatus - 128
    : null;
  let browserAttribution = "UNOBSERVED";
  if (execution?.browser_signal_number !== null && execution?.browser_signal_number !== undefined) browserAttribution = "CONFIRMED_SUBPROCESS_SIGNAL";
  else if (execution?.browser_exit_code !== null && execution?.browser_exit_code !== undefined) browserAttribution = "CONFIRMED_SUBPROCESS_EXIT_CODE";
  else if (execution?.timed_out === true) browserAttribution = "CONFIRMED_SUBPROCESS_TIMEOUT";
  return {
    schema_version: 1,
    status,
    failure_domain: failureDomain,
    code,
    label,
    topology: DUAL_LINUX_TOPOLOGY,
    run_id: runId,
    execution_layer: "docker-exec-linux-client-wrapper",
    client: {
      container: clientContainer,
      image_id: clientImageId,
      user: clientRuntime?.user ?? null,
      home: execution?.home ?? clientRuntime?.home ?? null,
    },
    runner: {
      relative_path: LINUX_BROWSER_RUNNER_RELATIVE,
      sha256: runnerSha256,
      argument_profile: LINUX_BROWSER_ARGUMENT_PROFILE,
    },
    browser,
    launcher: {
      kind: "docker-cli-exec",
      exit_code: outerStatus,
      signal: chromeRun?.signal ?? null,
      encoded_signal_candidate: encodedSignalCandidate,
      candidate_attribution: encodedSignalCandidate === null ? null : "UNCONFIRMED_POSIX_EXIT_CONVENTION",
    },
    wrapper: execution ? {
      status: execution.wrapper_status,
      failure_code: execution.failure_code,
      cleanup: execution.cleanup,
    } : null,
    browser_process: execution ? {
      started: execution.browser_started,
      exit_code: execution.browser_exit_code,
      signal_number: execution.browser_signal_number,
      signal_name: execution.browser_signal_name,
      timed_out: execution.timed_out,
      attribution: browserAttribution,
    } : {
      started: null,
      exit_code: null,
      signal_number: null,
      signal_name: null,
      timed_out: null,
      attribution: "UNOBSERVED",
    },
    capture: {
      browser_stdout: execution?.stdout ?? redactedTextCapture(chromeRun?.stdout),
      browser_stderr: execution?.stderr ?? null,
      launcher_stderr: redactedTextCapture(chromeRun?.stderr),
      result_marker_present: /data-result="[A-Za-z0-9+/=]+"/.test(String(chromeRun?.stdout ?? "")),
    },
  };
}

async function runChromePage(
  configuration,
  state,
  label,
  page,
  fixtureBytes = null,
  {
    failureStatus = "FAIL",
    failureDomain = "BROWSER",
    validateResult = null,
    resultFailureCode = null,
  } = {},
) {
  if (configuration.topology !== DUAL_LINUX_TOPOLOGY) return runHostChromePage(label, page, fixtureBytes);
  requireCondition(state.client_container, "LINUX_CLIENT_CONTAINER_MISSING", "BLOCKED", "INFRA");
  const runtime = ensureClientRuntime(configuration, state);
  const browserRoot = path.join(runtime.runtimeRoot, "browser", configuration.stage, label);
  ensureDirectory(browserRoot);
  const pagePath = path.join(browserRoot, "index.html");
  const fixturePath = path.join(browserRoot, "fixture");
  writeTextNew(pagePath, page);
  if (fixtureBytes !== null) {
    const descriptor = fs.openSync(fixturePath, "wx", 0o600);
    try { fs.writeFileSync(descriptor, fixtureBytes); fs.fsyncSync(descriptor); } finally { fs.closeSync(descriptor); }
  }
  const containerRoot = `/client-runtime/browser/${configuration.stage}/${label}`;
  const port = 18765;
  const browserOrigin = `http://127.0.0.1:${port}`;
  const runnerPath = path.join(configuration.repoRoot, ...LINUX_BROWSER_RUNNER_RELATIVE.split("/"));
  requireCondition(fs.existsSync(runnerPath), "LINUX_CLIENT_BROWSER_RUNNER_MISSING", "BLOCKED", "INFRA");
  const runnerSha256 = sha256File(runnerPath);
  const chromeRun = await run("docker", dockerArgs(configuration.dockerContext, [
    "exec", state.client_container,
    "/opt/venvs/xiaodao/bin/python", LINUX_BROWSER_RUNNER_CONTAINER,
    "--chrome", "/opt/chrome-headless-shell/chrome-headless-shell",
    "--directory", containerRoot,
    "--port", String(port),
    "--label", label,
  ]), { forward: false, maximumBytes: 8 * 1024 * 1024 });
  const execution = parseLinuxClientBrowserExecution(chromeRun, label);
  const persistFailure = (code) => {
    const browser = {
      status: "PRESENT",
      product: RELEASE_CHROME_HEADLESS_SHELL_PRODUCT,
      version: RELEASE_CHROME_HEADLESS_SHELL_VERSION_OUTPUT,
      executable_sha256: RELEASE_CHROME_HEADLESS_SHELL_EXECUTABLE_SHA256,
      code: null,
    };
    const failureReceipt = buildLinuxClientBrowserFailureReceipt({
      label,
      runId: state.run_id,
      clientContainer: state.client_container,
      clientImageId: state.client_image_id,
      clientRuntime: state.selected_client_runtime_observed,
      browser,
      runnerSha256,
      chromeRun,
      execution,
      status: failureStatus,
      failureDomain,
      code,
    });
    const failurePath = path.join(configuration.stageRoot, `chrome-${label}-failure.json`);
    writeNew(failurePath, failureReceipt);
    const binding = { path: path.basename(failurePath), sha256: sha256File(failurePath) };
    throw new StageError(code, failureStatus, failureDomain, binding);
  };
  if (chromeRun.status !== 0 || chromeRun.signal !== null) {
    const suffix = chromeRun.signal ? `DOCKER_SIGNAL_${chromeRun.signal}` : `DOCKER_EXIT_${chromeRun.status}`;
    persistFailure(`CHROME_${label.toUpperCase()}_${suffix}`);
  }
  if (!execution) persistFailure(`CHROME_${label.toUpperCase()}_EXECUTION_RECEIPT_INVALID`);
  if (execution.wrapper_status !== "PASS") persistFailure(`CHROME_${label.toUpperCase()}_WRAPPER_ERROR`);
  if (execution.timed_out) persistFailure(`CHROME_${label.toUpperCase()}_TIMEOUT`);
  if (execution.browser_signal_number !== null) persistFailure(`CHROME_${label.toUpperCase()}_SIGNAL_${execution.browser_signal_name ?? execution.browser_signal_number}`);
  if (execution.browser_exit_code !== 0) persistFailure(`CHROME_${label.toUpperCase()}_EXIT_${execution.browser_exit_code}`);
  if (execution.home.path !== LINUX_CLIENT_HOME
    || execution.home.realpath !== LINUX_CLIENT_HOME
    || execution.home.present !== true
    || execution.home.writable !== true
    || execution.cleanup.http_server_stopped !== true
    || execution.cleanup.profile_removed !== true
    || !completedLinuxBrowserProcessTree(execution.cleanup.process_tree)) {
    persistFailure(`CHROME_${label.toUpperCase()}_RUNTIME_BOUNDARY_INVALID`);
  }
  const match = chromeRun.stdout.match(/data-result="([A-Za-z0-9+/=]+)"/);
  if (!match) persistFailure(`CHROME_${label.toUpperCase()}_RESULT_MISSING`);
  let result;
  try { result = JSON.parse(Buffer.from(match[1], "base64").toString("utf8")); }
  catch { persistFailure(`CHROME_${label.toUpperCase()}_RESULT_INVALID`); }
  if (validateResult !== null && !validateResult(result)) {
    persistFailure(resultFailureCode ?? `CHROME_${label.toUpperCase()}_RESULT_INVALID`);
  }
  return {
    browserOrigin,
    chrome: {
      status: "PRESENT",
      product: RELEASE_CHROME_HEADLESS_SHELL_PRODUCT,
      version: RELEASE_CHROME_HEADLESS_SHELL_VERSION_OUTPUT,
      executable_sha256: RELEASE_CHROME_HEADLESS_SHELL_EXECUTABLE_SHA256,
      code: null,
    },
    chromeRun,
    browserExecution: execution,
    runner: { relative_path: LINUX_BROWSER_RUNNER_RELATIVE, sha256: runnerSha256 },
    failBrowser: persistFailure,
    result,
  };
}

async function probeLinuxClientBrowserCapability(configuration, state, stageRoot) {
  if (configuration.topology !== DUAL_LINUX_TOPOLOGY) return null;
  const challenge = sha256Bytes(`${state.run_id}:${state.client_container}:linux-client-browser-capability-v1`);
  const page = `<!doctype html><html><head><meta charset="utf-8"><title>PENDING</title></head><body><script>
const value = ${scriptJson({ schema_version: 1, ok: true, capability: "headless-dom-roundtrip", challenge })};
const bytes = new TextEncoder().encode(JSON.stringify(value));
let binary = "";
for (const byte of bytes) binary += String.fromCharCode(byte);
document.documentElement.dataset.result = btoa(binary);
document.title = "DONE";
</script></body></html>`;
  const {
    browserOrigin,
    chrome,
    chromeRun,
    browserExecution,
    runner,
    result,
  } = await runChromePage(
    configuration,
    state,
    "capability",
    page,
    null,
    {
      failureStatus: "BLOCKED",
      failureDomain: "INFRA",
      validateResult: (value) => (
        hasExactKeys(value, ["schema_version", "ok", "capability", "challenge"])
          && value.schema_version === 1
          && value.ok === true
          && value.capability === "headless-dom-roundtrip"
          && value.challenge === challenge
      ),
      resultFailureCode: "CHROME_CAPABILITY_RESULT_INVALID",
    },
  );
  const receipt = {
    schema_version: 1,
    status: "PASS",
    code: null,
    kind: "linux-client-headless-dom-roundtrip",
    topology: DUAL_LINUX_TOPOLOGY,
    run_id: state.run_id,
    client_container: state.client_container,
    client_image_id: state.client_image_id,
    execution_user: state.selected_client_runtime_observed.user,
    home: browserExecution.home,
    browser: chrome,
    runner: { ...runner, argument_profile: browserExecution.argument_profile },
    launcher_contract: {
      kind: "docker-cli-exec-to-python-subprocess",
      network_scope: "container-loopback-only",
      docker_exec_count: 1,
      retries: 0,
    },
    probe: {
      origin: browserOrigin,
      challenge_sha256: challenge,
      result_sha256: sha256Bytes(canonicalJson(result)),
      launcher_exit_code: chromeRun.status,
      launcher_signal: chromeRun.signal,
      browser_exit_code: browserExecution.browser_exit_code,
      browser_signal_number: browserExecution.browser_signal_number,
      browser_signal_name: browserExecution.browser_signal_name,
      timed_out: browserExecution.timed_out,
      stdout: browserExecution.stdout,
      stderr: browserExecution.stderr,
      result_marker: "data-result",
      cleanup: browserExecution.cleanup,
    },
    usage_complete: true,
    invocations: [],
    usage: zeroUsage(),
  };
  writeNew(path.join(stageRoot, "linux-client-browser-capability.json"), receipt);
  return receipt;
}

function dockerArgs(context, args) {
  return dockerContextArgs(context ?? "default", args);
}

async function docker(context, args, options = {}) {
  const result = await run("docker", dockerArgs(context, args), options);
  if (result.status !== 0) throw new StageError(`DOCKER_COMMAND_FAILED:${args[0]}`, "BLOCKED", "INFRA");
  return result;
}

function appendResource(registryPath, attemptRoot, kind, name, label) {
  const registry = path.resolve(registryPath);
  const root = path.resolve(attemptRoot);
  requireCondition(registry.startsWith(`${root}${path.sep}`), "RESOURCE_REGISTRY_OUTSIDE_ATTEMPT");
  requireCondition(label === `problem-locator.test-flow.run=${path.basename(root)}`, "RESOURCE_LABEL_INVALID");
  requireCondition(["container", "network", "volume"].includes(kind) && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(name), "RESOURCE_RECORD_INVALID");
  const existing = fs.existsSync(registry)
    ? fs.readFileSync(registry, "utf8").split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line))
    : [];
  requireCondition(!existing.some((entry) => entry.kind === kind && entry.name === name), "RESOURCE_RECORD_DUPLICATE");
  ensureDirectory(path.dirname(registry));
  fs.appendFileSync(registry, `${JSON.stringify({ schema_version: 2, kind, name, label })}\n`, { encoding: "utf8", mode: 0o600 });
}

async function availablePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once("error", reject);
    server.listen({ host: "127.0.0.1", port: 0 }, () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : null;
      server.close((error) => error ? reject(error) : resolve(port));
    });
  });
}

async function waitHostReady(publicBaseUrl) {
  const deadline = Date.now() + 90_000;
  let probes = 0;
  while (Date.now() < deadline) {
    probes += 1;
    try {
      const [live, ready] = await Promise.all([
        fetch(`${publicBaseUrl}/live`, { signal: AbortSignal.timeout(3000) }),
        fetch(`${publicBaseUrl}/ready`, { signal: AbortSignal.timeout(3000) }),
      ]);
      if (live.status === 200 && ready.status === 200) {
        const liveBody = await live.json();
        const readyBody = await ready.json();
        requireCondition(liveBody?.ok === true && readyBody?.ok === true && readyBody?.data?.ready === true, "SERVICE_READINESS_BODY", "FAIL", "PRODUCT");
        process.stdout.write("TEST_FLOW_PROGRESS request.completed\n");
        return { probes, live: true, ready: true };
      }
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new StageError("SERVICE_READINESS_TIMEOUT", "BLOCKED", "INFRA");
}

async function waitReady(configuration, state) {
  if (configuration.topology !== DUAL_LINUX_TOPOLOGY) return waitHostReady(state.public_base_url);
  const probe = await run("docker", dockerArgs(configuration.dockerContext, [
    "exec", state.client_container,
    "/opt/venvs/xiaodao/bin/python", "-I", "-c",
    `import json, sys, time, urllib.request
base=sys.argv[1]
for attempt in range(1, 361):
    try:
        with urllib.request.urlopen(base + "/live", timeout=3) as live, urllib.request.urlopen(base + "/ready", timeout=3) as ready:
            live_body=json.load(live); ready_body=json.load(ready)
            if live.status == 200 and ready.status == 200 and live_body.get("ok") is True and ready_body.get("ok") is True and ready_body.get("data", {}).get("ready") is True:
                print(json.dumps({"probes": attempt, "live": True, "ready": True}, separators=(",", ":")))
                raise SystemExit(0)
    except Exception:
        pass
    time.sleep(0.25)
raise SystemExit(1)`,
    state.public_base_url,
  ]), { forward: false });
  requireCondition(probe.status === 0, "SERVICE_READINESS_TIMEOUT", "BLOCKED", "INFRA");
  process.stdout.write("TEST_FLOW_PROGRESS request.completed\n");
  return JSON.parse(probe.stdout.trim());
}

async function captureReadinessDiagnostic(configuration, state, instance, code) {
  const pidProbe = await run("docker", dockerArgs(configuration.dockerContext, [
    "exec", state.active_container,
    "sh", "-c",
    'pid_file="/tmp/test-flow-service-$1/service.pid"; test -s "$pid_file"; pid=$(cat "$pid_file"); kill -0 "$pid"',
    "test-flow-readiness", instance,
  ]), { forward: false });
  const tcpProbe = await run("docker", dockerArgs(configuration.dockerContext, [
    "exec", state.active_container,
    "/opt/venvs/xiaodao/bin/python", "-I", "-c",
    'import socket; s=socket.socket(); s.settimeout(2); print("OPEN" if s.connect_ex(("127.0.0.1", 8000)) == 0 else "CLOSED"); s.close()',
  ]), { forward: false });
  const topProbe = await run("docker", dockerArgs(configuration.dockerContext, [
    "top", state.active_container, "-eo", "args",
  ]), { forward: false });
  const supervisorLog = path.join(configuration.stageRoot, `supervisor-${instance}.log`);
  const serviceLog = path.join(configuration.attemptRoot, "payload", "logs", `service-${instance}.log`);
  writeNew(path.join(configuration.stageRoot, `readiness-${instance}.json`), {
    schema_version: 1,
    status: "FAIL",
    code,
    service_pid_running: pidProbe.status === 0,
    internal_tcp_8000: tcpProbe.status === 0 && tcpProbe.stdout.trim() === "OPEN",
    launcher_process_present: topProbe.status === 0 && topProbe.stdout.includes("/test-flow-runtime/test_service_launcher.py"),
    journey_relay_present: topProbe.status === 0 && topProbe.stdout.includes("relay_service_journey.py"),
    supervisor_log_bytes: fs.existsSync(supervisorLog) ? fs.statSync(supervisorLog).size : null,
    service_log_bytes: fs.existsSync(serviceLog) ? fs.statSync(serviceLog).size : null,
  });
}

function copyTreeNoSymlinks(source, destination) {
  const metadata = fs.lstatSync(source);
  requireCondition(!metadata.isSymbolicLink(), "CLIENT_SKILL_SYMLINK_FORBIDDEN");
  if (metadata.isDirectory()) {
    fs.mkdirSync(destination, { recursive: false, mode: 0o700 });
    for (const name of fs.readdirSync(source).sort()) copyTreeNoSymlinks(path.join(source, name), path.join(destination, name));
    return;
  }
  requireCondition(metadata.isFile() && metadata.nlink === 1, "CLIENT_SKILL_FILE_INVALID");
  fs.copyFileSync(source, destination, fs.constants.COPYFILE_EXCL);
  fs.chmodSync(destination, 0o600);
}

function ensureClientRuntime(configuration, state) {
  const runtimeRoot = path.join(configuration.attemptRoot, "scratch", `${configuration.client}-client`);
  const configRoot = path.join(runtimeRoot, "config");
  const settingsPath = path.join(runtimeRoot, "settings.json");
  const mcpPath = path.join(runtimeRoot, "mcp.json");
  const home = path.join(runtimeRoot, "home");
  ensureDirectory(runtimeRoot);
  ensureDirectory(home);
  if (!fs.existsSync(configRoot)) {
    fs.mkdirSync(configRoot, { mode: 0o700 });
    fs.mkdirSync(path.join(configRoot, "skills"), { mode: 0o700 });
    copyTreeNoSymlinks(
      path.join(configuration.repoRoot, ".claude", "skills", "problem-locator-client"),
      path.join(configRoot, "skills", "problem-locator-client"),
    );
  }
  if (!fs.existsSync(settingsPath)) materializeClaudeSettings(configuration.containerClaudeSettings, settingsPath);
  const frozenSettings = claudeSettingsIdentity(settingsPath);
  requireCondition(frozenSettings.status === "PRESENT" && frozenSettings.fingerprint === state.runtime_identity.settings.fingerprint, "CLIENT_SETTINGS_IDENTITY_DRIFT", "BLOCKED", "INFRA");
  const clientSkill = packageTreeIdentity(path.join(configRoot, "skills", "problem-locator-client"));
  requireCondition(clientSkill.status === "PRESENT", "CLIENT_SKILL_COPY_INVALID");
  if (state.client_skill_digest) requireCondition(state.client_skill_digest === clientSkill.digest, "CLIENT_SKILL_COPY_DRIFT");
  else state.client_skill_digest = clientSkill.digest;
  const expectedMcp = { mcpServers: { "problem-locator": { type: "http", url: `${state.public_base_url}/mcp`, alwaysLoad: true } } };
  if (!fs.existsSync(mcpPath)) writeNew(mcpPath, expectedMcp);
  else requireCondition(canonicalJson(readJson(mcpPath)) === canonicalJson(expectedMcp), "CLIENT_MCP_CONFIG_DRIFT");
  return { runtimeRoot, configRoot, settingsPath, mcpPath, home };
}

function normalizedToolName(fullName) {
  if (TOOL_NAMES.includes(fullName)) return fullName;
  return TOOL_NAMES.find((name) => fullName.endsWith(name)) ?? null;
}

function assertFlatInput(input) {
  requireCondition(input && typeof input === "object" && !Array.isArray(input), "CLIENT_TOOL_INPUT_NOT_OBJECT", "FAIL", "CONTRACT");
  for (const value of Object.values(input)) {
    if ([null, "string", "number", "boolean"].includes(value === null ? null : typeof value)) continue;
    requireCondition(Array.isArray(value) && value.every((item) => item === null || ["string", "number", "boolean"].includes(typeof item)), "CLIENT_TOOL_INPUT_NOT_FLAT", "FAIL", "CONTRACT");
  }
}

export function parseClaudeStream(text, expectedCwd, { allowErrorTerminal = false } = {}) {
  const events = text.split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
  const initEvents = events.filter((event) => event.type === "system" && event.subtype === "init");
  const results = events.filter((event) => event.type === "result");
  requireCondition(initEvents.length === 1 && results.length === 1 && events.at(-1)?.type === "result", "CLIENT_STREAM_TERMINAL_INVALID");
  const terminal = results[0];
  const terminalSucceeded = terminal.subtype === "success" && terminal.is_error === false;
  requireCondition(terminalSucceeded || (allowErrorTerminal && terminal.is_error === true), "CLIENT_RESULT_NOT_SUCCESS", "INCONCLUSIVE", "EXTERNAL");
  const init = initEvents[0];
  requireCondition(path.resolve(init.cwd) === path.resolve(expectedCwd), "CLIENT_CWD_MISMATCH");
  requireCondition(init.model === RELEASE_MODEL, "CLIENT_EFFECTIVE_MODEL_MISMATCH", "BLOCKED", "INFRA");
  requireCondition(init.permissionMode === "dontAsk", "CLIENT_PERMISSION_MODE_MISMATCH");
  const reported = new Set((init.tools ?? []).map(normalizedToolName).filter(Boolean));
  requireCondition((init.tools ?? []).includes("Skill") && TOOL_NAMES.every((name) => reported.has(name)), "CLIENT_TOOL_INVENTORY_MISMATCH");
  const servers = init.mcp_servers ?? [];
  requireCondition(servers.length === 1 && (servers[0].name ?? servers[0].serverName) === "problem-locator" && (!servers[0].status || servers[0].status === "connected"), "CLIENT_STRICT_MCP_NOT_CONNECTED", "BLOCKED", "INFRA");

  const byId = new Map();
  const records = [];
  const assistantTextEvents = [];
  let streamOrdinal = 0;
  for (const event of events) {
    const content = event.message?.content;
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      const currentStreamOrdinal = streamOrdinal;
      streamOrdinal += 1;
      if (block?.type === "text" && event.type === "assistant") {
        requireCondition(
          event.message?.role === "assistant"
            && typeof block.text === "string",
          "CLIENT_ASSISTANT_TEXT_INVALID",
        );
        assistantTextEvents.push({
          ordinal: assistantTextEvents.length,
          stream_ordinal: currentStreamOrdinal,
          text: block.text,
        });
      }
      if (block?.type === "tool_use") {
        requireCondition(event.type === "assistant" && event.message?.role === "assistant", "CLIENT_TOOL_USE_ROLE");
        requireCondition(typeof block.id === "string" && !byId.has(block.id), "CLIENT_TOOL_USE_ID");
        const toolName = block.name === "Skill" ? "Skill" : normalizedToolName(block.name);
        requireCondition(toolName !== null, "CLIENT_UNEXPECTED_TOOL", "FAIL", "CONTRACT");
        if (toolName !== "Skill") assertFlatInput(block.input);
        const record = { ordinal: records.length, stream_ordinal: currentStreamOrdinal, result_stream_ordinal: null, tool_use_id: block.id, full_name: block.name, tool_name: toolName, input: block.input, result: null };
        records.push(record);
        byId.set(block.id, record);
      }
      if (block?.type === "tool_result") {
        requireCondition(event.type === "user" && event.message?.role === "user", "CLIENT_TOOL_RESULT_ROLE");
        const record = byId.get(block.tool_use_id);
        requireCondition(record && record.result === null && block.is_error !== true, "CLIENT_TOOL_RESULT_ID");
        record.result_stream_ordinal = currentStreamOrdinal;
        if (record.tool_name === "Skill") record.result = { skill_loaded: true };
        else {
          const raw = event.tool_use_result;
          requireCondition(raw && typeof raw === "object" && !event.toolUseResult && raw.isError !== true && raw.is_error !== true, "CLIENT_TOOL_RESULT_ENVELOPE");
          requireCondition(raw.structuredContent && typeof raw.structuredContent === "object" && !Array.isArray(raw.structuredContent), "CLIENT_TOOL_RESULT_STRUCTURED_CONTENT");
          record.result = raw.structuredContent;
        }
      }
    }
  }
  requireCondition(records.every((record) => record.result !== null), "CLIENT_TOOL_RESULT_MISSING");
  const skills = records.filter((record) => record.tool_name === "Skill");
  requireCondition(skills.length === 1 && skills[0].ordinal === 0 && skills[0].input?.skill === "problem-locator-client", "CLIENT_SKILL_INVOCATION_INVALID", "FAIL", "CONTRACT");
  const mcpRecords = records.filter((record) => record.tool_name !== "Skill");
  requireCondition(mcpRecords.length > 0, "CLIENT_MCP_CALL_MISSING", "FAIL", "CONTRACT");
  let usage;
  try {
    usage = normalizeUsage({
      input_tokens: Number(terminal.usage?.input_tokens ?? -1),
      output_tokens: Number(terminal.usage?.output_tokens ?? -1),
      cache_creation_input_tokens: Number(terminal.usage?.cache_creation_input_tokens ?? -1),
      cache_read_input_tokens: Number(terminal.usage?.cache_read_input_tokens ?? -1),
      cost_usd: Number(terminal.total_cost_usd ?? terminal.cost_usd ?? -1),
    });
  } catch {
    requireCondition(false, "CLIENT_MODEL_USAGE_INVALID");
  }
  return {
    schema_version: 1,
    init: { cwd: init.cwd, effective_model: init.model, permission_mode: init.permissionMode, tools: init.tools, mcp_servers: servers },
    records: mcpRecords,
    assistant_text_events: assistantTextEvents,
    usage,
    terminal: { subtype: terminal.subtype, is_error: terminal.is_error },
    turns: Number(terminal.num_turns),
  };
}

async function runClaude(configuration, state, stageRoot, phase, prompt, maxTurns, maxBudgetUsd) {
  const runtime = ensureClientRuntime(configuration, state);
  const containerClient = configuration.topology === DUAL_LINUX_TOPOLOGY;
  const promptPath = path.join(stageRoot, `${phase}.prompt.txt`);
  const stdoutPath = path.join(stageRoot, `${phase}.stream-json.stdout.ndjson`);
  const stderrPath = path.join(stageRoot, `${phase}.stderr.txt`);
  const auditPath = path.join(stageRoot, `${phase}.authoritative.json`);
  writeTextNew(promptPath, `${prompt}\n`);
  const stdoutDescriptor = fs.openSync(stdoutPath, "wx", 0o600);
  const stderrDescriptor = fs.openSync(stderrPath, "wx", 0o600);
  const environment = { ...process.env };
  for (const name of Object.keys(environment)) {
    if (/^(?:ANTHROPIC_|CLAUDE_MODEL$|HTTP_PROXY$|HTTPS_PROXY$|ALL_PROXY$|http_proxy$|https_proxy$|all_proxy$)/.test(name)) delete environment[name];
  }
  environment.HOME = runtime.home;
  environment.CLAUDE_CONFIG_DIR = runtime.configRoot;
  environment.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1";
  environment.NO_PROXY = "127.0.0.1,localhost";
  environment.no_proxy = environment.NO_PROXY;
  const claudeArgs = [
    "--print",
    "--output-format", "stream-json",
    "--verbose",
    "--model", "sonnet",
    "--max-turns", String(maxTurns),
    "--max-budget-usd", String(maxBudgetUsd),
    "--setting-sources", "user",
    "--settings", containerClient ? "/client-runtime/settings.json" : runtime.settingsPath,
    "--mcp-config", containerClient ? "/client-runtime/mcp.json" : runtime.mcpPath,
    "--strict-mcp-config",
    "--tools=Skill",
    "--allowedTools", "Skill(problem-locator-client)",
    ...FULL_TOOL_NAMES,
    "--permission-mode", "dontAsk",
    "--no-chrome",
    "--no-session-persistence",
    prompt,
  ];
  const command = containerClient ? "docker" : process.execPath;
  const args = containerClient
    ? dockerArgs(configuration.dockerContext, [
        "exec",
        "--workdir", "/workspace",
        "--env", "HOME=/client-runtime/home",
        "--env", "CLAUDE_CONFIG_DIR=/client-runtime/config",
        "--env", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
        "--env", "NO_PROXY=problem-locator-server,127.0.0.1,localhost",
        "--env", "no_proxy=problem-locator-server,127.0.0.1,localhost",
        state.client_container,
        "timeout", "--signal=TERM", "--kill-after=10s", `${configuration.hardCaps.hard_timeout_seconds}s`,
        "node", "/opt/claude-code/cli.js",
        ...claudeArgs,
      ])
    : [configuration.claudeEntry, ...claudeArgs];
  const chunks = [];
  const stderrChunks = [];
  const invocationStartedUtc = new Date().toISOString();
  const invocationStarted = process.hrtime.bigint();
  const exit = await new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd: containerClient ? undefined : configuration.repoRoot, env: containerClient ? process.env : environment, stdio: ["ignore", "pipe", "pipe"] });
    let timedOut = false;
    const timeout = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
      setTimeout(() => child.exitCode === null && child.kill("SIGKILL"), 5000).unref();
    }, (configuration.hardCaps.hard_timeout_seconds + (containerClient ? 20 : 0)) * 1000);
    timeout.unref();
    child.stdout.on("data", (chunk) => {
      chunks.push(chunk);
      fs.writeSync(stdoutDescriptor, chunk);
      process.stdout.write(chunk);
    });
    child.stderr.on("data", (chunk) => {
      stderrChunks.push(chunk);
      fs.writeSync(stderrDescriptor, chunk);
      process.stderr.write(chunk);
    });
    child.once("error", (error) => { clearTimeout(timeout); reject(error); });
    child.once("exit", (code, signal) => { clearTimeout(timeout); resolve({ code, signal, timedOut }); });
  });
  fs.fsyncSync(stdoutDescriptor);
  fs.fsyncSync(stderrDescriptor);
  fs.closeSync(stdoutDescriptor);
  fs.closeSync(stderrDescriptor);
  const timedOut = exit.timedOut || (containerClient && [124, 137].includes(exit.code));
  if (timedOut) {
    writeNew(path.join(stageRoot, `${phase}.timing.json`), {
      schema_version: 2,
      span: containerClient ? "linux-client-container.model-and-mcp-wait" : "host.model-and-mcp-wait",
      phase,
      clock_domain: containerClient ? "linux-client-container" : `${configuration.client}-host`,
      started_at_utc: invocationStartedUtc,
      finished_at_utc: new Date().toISOString(),
      duration_ms: Math.round(Number(process.hrtime.bigint() - invocationStarted) / 1_000_000),
      timed_out: true,
      max_turns: maxTurns,
      max_budget_usd: maxBudgetUsd,
      max_total_tokens: configuration.hardCaps.max_total_tokens,
      effective_model: null,
      usage: null,
    });
    throw new StageError(`CLAUDE_${phase.toUpperCase()}_HARD_TIMEOUT`, "INCONCLUSIVE", "EXTERNAL");
  }
  const audit = parseClaudeStream(Buffer.concat(chunks).toString("utf8"), containerClient ? "/workspace" : configuration.repoRoot, { allowErrorTerminal: exit.code !== 0 });
  writeNew(auditPath, audit);
  writeNew(path.join(stageRoot, `${phase}.timing.json`), {
    schema_version: 2,
    span: containerClient ? "linux-client-container.model-and-mcp-wait" : "host.model-and-mcp-wait",
    phase,
    clock_domain: containerClient ? "linux-client-container" : `${configuration.client}-host`,
    started_at_utc: invocationStartedUtc,
    finished_at_utc: new Date().toISOString(),
    duration_ms: Math.round(Number(process.hrtime.bigint() - invocationStarted) / 1_000_000),
    timed_out: exit.timedOut,
    max_turns: maxTurns,
    max_budget_usd: maxBudgetUsd,
    max_total_tokens: configuration.hardCaps.max_total_tokens,
    effective_model: audit.init.effective_model,
    usage: audit.usage,
  });
  const forbidden = [
    path.join(runtime.runtimeRoot, "client-dfx.jsonl"),
    path.join(runtime.runtimeRoot, ".problem-locator", "client-dfx.jsonl"),
    path.join(stageRoot, `${phase}.client-dfx.jsonl`),
  ];
  requireCondition(forbidden.every((filePath) => !fs.existsSync(filePath)), "CLIENT_DFX_FORBIDDEN");
  if (containerClient) {
    const containerDfx = await run("docker", dockerArgs(configuration.dockerContext, [
      "exec", state.client_container, "sh", "-eu", "-c",
      "test ! -e /client-runtime/client-dfx.jsonl && test ! -e /client-runtime/.problem-locator/client-dfx.jsonl",
    ]), { forward: false });
    requireCondition(containerDfx.status === 0, "CLIENT_DFX_FORBIDDEN");
  }
  if (exit.code !== 0) throw new StageError(`CLAUDE_${phase.toUpperCase()}_EXIT_${exit.code ?? exit.signal}`, "INCONCLUSIVE", "EXTERNAL");
  return audit;
}

function successData(record) {
  requireCondition(record.result && record.result.ok === true && record.result.error === null && record.result.data && typeof record.result.data === "object", `MCP_${record.tool_name}_FAILED`, "FAIL", "CONTRACT");
  return record.result.data;
}

function applicationResponse(record) {
  const data = successData(record);
  return record.tool_name === "problem_locator_prepare_attachment" ? data.application_response : data;
}

function caseView(record) {
  return applicationResponse(record)?.case_view ?? null;
}

function exactKeys(value, expected, code) {
  requireCondition(value && typeof value === "object" && !Array.isArray(value), code, "FAIL", "CONTRACT");
  requireCondition(canonicalJson(Object.keys(value).sort()) === canonicalJson([...expected].sort()), code, "FAIL", "CONTRACT");
}

function openRequirementNames(view, kind) {
  return (view?.pending_requirements ?? [])
    .filter((item) => item.status === "OPEN" && item.kind === kind)
    .map((item) => item.name);
}

const PHASE_ONE_USER_MESSAGE = "订单 RPC 偶发超时，请定位原因；我有一份日志，可以在需要时提供。";

const PHASE_FACT_LABELS = Object.freeze({
  problem_time: "问题时间",
  client_process: "客户端进程",
  server_process: "服务端进程",
  service: "服务名",
  api: "API 名",
});

export function phaseOneUserMessage() {
  return PHASE_ONE_USER_MESSAGE;
}

export function phaseTwoUserMessage(releaseCase, archive) {
  const facts = releaseCase.driver.initial_user_fact_names.map(
    (name, index) => {
      requireCondition(Boolean(PHASE_FACT_LABELS[name]), "PHASE2_USER_FACT_LABEL_MISSING", "FAIL", "CONTRACT");
      return `- ${PHASE_FACT_LABELS[name]}：${releaseCase.driver.initial_user_fact_values[index]}`;
    },
  );
  return [
    "补充信息如下：",
    ...facts,
    `日志文件：${archive.name}；格式：${archive.content_type}；大小：${archive.size} 字节；SHA-256：${archive.sha256}。`,
    "请继续处理；需要日志时请先申请上传地址，拿到地址后先暂停。",
  ].join("\n");
}

function expectedCreateInput(requestId) {
  const rawProblemText = phaseOneUserMessage();
  return {
    request_id: requestId,
    raw_problem_text: rawProblemText,
    statement: rawProblemText,
    expected_behavior: "用户未单独说明；以 raw_problem_text 为准。",
    actual_behavior: rawProblemText,
    scope: "仅定位 raw_problem_text 所述问题。",
    goals: ["定位问题原因并给出结论。"],
    non_goals: [],
    constraints: [],
    completion_criteria: ["给出基于证据的结论；证据不足时明确说明。"],
    initial_user_fact_names: [],
    initial_user_fact_values: [],
    wait_seconds: 0,
  };
}

export function phaseOnePrompt() {
  return `第一步请先加载 problem-locator-client Skill，调用 Skill 工具时使用 {"skill":"problem-locator-client"}。

加载成功后，请处理下面这位用户的新请求：

${phaseOneUserMessage()}`;
}

export function phaseTwoPrompt(state, releaseCase, archive) {
  requireCondition(UUID.test(state?.case_id ?? ""), "PHASE2_CASE_ID_INVALID", "FAIL", "CONTRACT");
  return `第一步请先加载 problem-locator-client Skill，调用 Skill 工具时使用 {"skill":"problem-locator-client"}。

这是同一问题的下一轮用户回复。请继续处理已经创建的 Case ${state.case_id}，先读取服务端的最新 Case 状态，再处理用户回复：

${phaseTwoUserMessage(releaseCase, archive)}`;
}

export function assertPhaseOneCaseFirst(audit) {
  const firstBusinessCall = audit?.records?.[0];
  requireCondition(
    firstBusinessCall?.tool_name === "problem_locator_create_case",
    "PHASE1_CREATE_NOT_FIRST_BUSINESS_CALL",
    "FAIL",
    "CONTRACT",
  );
  requireCondition(
    Number.isSafeInteger(firstBusinessCall.stream_ordinal),
    "PHASE1_CREATE_STREAM_ORDINAL_INVALID",
    "FAIL",
    "CONTRACT",
  );
  const preCreateProse = (audit.assistant_text_events ?? []).filter(
    (event) => event.stream_ordinal < firstBusinessCall.stream_ordinal
      && event.text.trim().length > 0,
  );
  requireCondition(
    preCreateProse.length === 0,
    "PHASE1_PROSE_BEFORE_CREATE",
    "FAIL",
    "CONTRACT",
  );
  return true;
}

function openRequirements(view, kind) {
  return (view?.pending_requirements ?? [])
    .filter((item) => item.status === "OPEN" && item.kind === kind);
}

export function validatePhaseOne(audit, releaseCase, requestIds) {
  assertPhaseOneCaseFirst(audit);
  const records = audit.records;
  requireCondition(records.every((record) => record.result?.ok === true), "PHASE1_MCP_CALL_FAILED", "FAIL", "CONTRACT");
  const create = records.filter((record) => record.tool_name === "problem_locator_create_case");
  const gets = records.filter((record) => record.tool_name === "problem_locator_get_case");
  requireCondition(
    create.length === 1 && gets.length >= 1 && records.length === create.length + gets.length,
    "PHASE1_CALL_CARDINALITY",
    "FAIL",
    "CONTRACT",
  );
  requireCondition(records[0] === create[0], "PHASE1_CREATE_NOT_FIRST_BUSINESS_CALL", "FAIL", "CONTRACT");
  const createRequestId = create[0].input.request_id;
  requireCondition(typeof createRequestId === "string" && createRequestId.length > 0, "PHASE1_REQUEST_ID_INVALID", "FAIL", "CONTRACT");
  const createInput = expectedCreateInput(createRequestId);
  exactKeys(create[0].input, Object.keys(createInput), "PHASE1_CREATE_INPUT_SHAPE");
  requireCondition(
    canonicalJson(create[0].input) === canonicalJson(createInput)
      && create[0].input.initial_user_fact_names.length === 0
      && create[0].input.initial_user_fact_values.length === 0
      && !Object.hasOwn(create[0].input, "problem_spec"),
    "PHASE1_CREATE_INPUT",
    "FAIL",
    "CONTRACT",
  );
  const createdCaseId = successData(create[0]).business_receipt?.case_id;
  requireCondition(UUID.test(createdCaseId ?? ""), "PHASE1_CREATED_CASE_VIEW", "FAIL", "CONTRACT");
  requireCondition(
    gets.every((record) => canonicalJson(record.input) === canonicalJson(fixedGetCasePollInput(createdCaseId))),
    "PHASE1_GET_CASE_INPUT",
    "FAIL",
    "CONTRACT",
  );
  const inputViews = gets
    .map((record) => ({ record, view: caseView(record) }))
    .filter(({ view }) => (
      view?.case_id === createdCaseId
        && view.status === "WAITING_INPUT"
        && canonicalJson(openRequirementNames(view, "INPUT"))
          === canonicalJson(releaseCase.driver.initial_user_fact_names)
    ));
  requireCondition(inputViews.length >= 1, "PHASE1_INPUT_REQUIREMENTS_NOT_OBSERVED", "FAIL", "CONTRACT");
  const inputView = inputViews.at(-1);
  requireCondition(records.at(-1) === inputView.record, "PHASE1_DID_NOT_STOP_AT_INPUT", "FAIL", "CONTRACT");
  const requirements = openRequirements(inputView.view, "INPUT");
  requireCondition(
    requirements.length === releaseCase.driver.initial_user_fact_names.length
      && requirements.every((item) => typeof item.prompt === "string" && item.prompt.trim().length > 0),
    "PHASE1_INPUT_REQUIREMENT_PROMPTS_INVALID",
    "FAIL",
    "CONTRACT",
  );
  requireCondition(
    Number.isSafeInteger(inputView.record.result_stream_ordinal),
    "PHASE1_INPUT_RESULT_ORDINAL_INVALID",
    "FAIL",
    "CONTRACT",
  );
  const questionsAfterResult = (audit.assistant_text_events ?? []).filter(
    (event) => event.stream_ordinal > inputView.record.result_stream_ordinal
      && event.text.trim().length > 0,
  );
  const questionText = questionsAfterResult.map((event) => event.text).join("\n");
  requireCondition(
    questionsAfterResult.length > 0
      && requirements.every((item) => questionText.includes(item.prompt)),
    "PHASE1_REQUIREMENTS_NOT_ASKED_AFTER_OBSERVATION",
    "FAIL",
    "CONTRACT",
  );
  const requestedBy = [...new Set(requirements.map((item) => item.requested_by_job_id))];
  requireCondition(requestedBy.length === 1 && UUID.test(requestedBy[0] ?? ""), "PHASE1_REQUIREMENT_JOB_INVALID", "FAIL", "CONTRACT");
  requireCondition(
    inputView.view.selected_skill_ref?.id === releaseCase.skill.runtime_ref_id
      && inputView.view.selected_skill_ref?.version === releaseCase.skill.version,
    "PHASE1_SELECTED_SKILL",
    "FAIL",
    "CONTRACT",
  );
  return {
    case_id: createdCaseId,
    waiting_input_case_revision: inputView.view.case_revision,
    input_requirements: requirements.map((item) => ({ name: item.name, prompt: item.prompt })),
    methods_preflight_job_id: requestedBy[0],
    selected_skill_ref: inputView.view.selected_skill_ref,
    request_ids: {
      ...requestIds,
      create: createRequestId,
    },
  };
}

export function validatePhaseTwo(audit, state, releaseCase, requestIds, archive, publicBaseUrl) {
  const records = audit.records;
  requireCondition(records.every((record) => record.result?.ok === true), "PHASE2_MCP_CALL_FAILED", "FAIL", "CONTRACT");
  const creates = records.filter((record) => record.tool_name === "problem_locator_create_case");
  const submits = records.filter((record) => record.tool_name === "problem_locator_submit_supplement");
  const gets = records.filter((record) => record.tool_name === "problem_locator_get_case");
  const prepare = records.filter((record) => record.tool_name === "problem_locator_prepare_attachment");
  requireCondition(
    creates.length === 0 && submits.length === 1 && gets.length >= 2 && prepare.length === 1,
    "PHASE2_CALL_CARDINALITY",
    "FAIL",
    "CONTRACT",
  );
  requireCondition(records[0] === gets[0], "PHASE2_GET_NOT_FIRST_BUSINESS_CALL", "FAIL", "CONTRACT");
  requireCondition(records.at(-1) === prepare[0], "PHASE2_PREPARE_NOT_TERMINAL", "FAIL", "CONTRACT");
  requireCondition(
    gets.every((record) => canonicalJson(record.input) === canonicalJson(fixedGetCasePollInput(state.case_id))),
    "PHASE2_GET_CASE_INPUT",
    "FAIL",
    "CONTRACT",
  );
  const expectedNames = state.input_requirements.map((item) => item.name);
  const inputView = gets
    .map((record) => ({ record, view: caseView(record) }))
    .find(({ record, view }) => (
      record.ordinal < submits[0].ordinal
        && view?.case_id === state.case_id
        && view.status === "WAITING_INPUT"
        && canonicalJson(openRequirementNames(view, "INPUT")) === canonicalJson(expectedNames)
    ));
  requireCondition(inputView, "PHASE2_INPUT_REQUIREMENTS_NOT_REOBSERVED", "FAIL", "CONTRACT");
  requireCondition(
    inputView.view.case_revision === state.waiting_input_case_revision,
    "PHASE2_INPUT_REVISION_DRIFT",
    "FAIL",
    "CONTRACT",
  );
  const currentRequirements = openRequirements(inputView.view, "INPUT");
  requireCondition(
    canonicalJson(currentRequirements.map((item) => ({ name: item.name, prompt: item.prompt })))
      === canonicalJson(state.input_requirements),
    "PHASE2_INPUT_REQUIREMENTS_DRIFT",
    "FAIL",
    "CONTRACT",
  );
  const expectedValuesByName = new Map(releaseCase.driver.initial_user_fact_names.map(
    (name, index) => [name, releaseCase.driver.initial_user_fact_values[index]],
  ));
  const expectedValues = expectedNames.map((name) => expectedValuesByName.get(name));
  requireCondition(expectedValues.every((value) => typeof value === "string"), "PHASE2_USER_ANSWER_MISSING", "FAIL", "CONTRACT");
  exactKeys(submits[0].input, ["request_id", "case_id", "expected_case_revision", "input_names", "input_values", "attachment_ids", "wait_seconds"], "PHASE2_INPUT_SUBMISSION_SHAPE");
  requireCondition(
    submits[0].input.case_id === state.case_id
      && submits[0].input.expected_case_revision === inputView.view.case_revision
      && canonicalJson(submits[0].input.input_names) === canonicalJson(expectedNames)
      && canonicalJson(submits[0].input.input_values) === canonicalJson(expectedValues)
      && canonicalJson(submits[0].input.attachment_ids) === canonicalJson([])
      && submits[0].input.wait_seconds === 0,
    "PHASE2_INPUT_SUBMISSION",
    "FAIL",
    "CONTRACT",
  );
  const attachmentView = gets
    .map((record) => ({ record, view: caseView(record) }))
    .find(({ record, view }) => (
      record.ordinal > submits[0].ordinal
        && record.ordinal < prepare[0].ordinal
        && view?.case_id === state.case_id
        && view.status === "WAITING_ATTACHMENT"
        && canonicalJson(openRequirementNames(view, "ATTACHMENT"))
          === canonicalJson([releaseCase.skill.attachment_requirement])
    ));
  requireCondition(attachmentView, "PHASE2_ATTACHMENT_REQUIREMENT_NOT_OBSERVED", "FAIL", "CONTRACT");
  exactKeys(prepare[0].input, ["request_id", "case_id", "expected_case_revision", "name", "content_type", "declared_size", "declared_sha256"], "PHASE2_PREPARE_INPUT_SHAPE");
  requireCondition(
    prepare[0].input.case_id === state.case_id
      && prepare[0].input.expected_case_revision === attachmentView.view.case_revision
      && prepare[0].input.name === archive.name
      && prepare[0].input.content_type === archive.content_type
      && prepare[0].input.declared_size === archive.size
      && prepare[0].input.declared_sha256 === archive.sha256,
    "PHASE2_PREPARE_INPUT",
    "FAIL",
    "CONTRACT",
  );
  const generatedRequestIds = [
    state.request_ids.create,
    submits[0].input.request_id,
    prepare[0].input.request_id,
  ];
  requireCondition(
    generatedRequestIds.every((value) => typeof value === "string" && value.length > 0)
      && new Set(generatedRequestIds).size === generatedRequestIds.length,
    "PHASE2_REQUEST_IDS_INVALID",
    "FAIL",
    "CONTRACT",
  );
  const prepareData = successData(prepare[0]);
  const response = prepareData.application_response;
  const view = response?.case_view;
  const descriptor = prepareData.upload;
  requireCondition(
    view?.status === "WAITING_ATTACHMENT"
      && view.case_id === state.case_id
      && Number.isInteger(view.case_revision),
    "PHASE2_CASE_VIEW",
    "FAIL",
    "CONTRACT",
  );
  const attachmentRequirements = openRequirements(view, "ATTACHMENT");
  requireCondition(
    canonicalJson(attachmentRequirements.map((item) => item.name))
        === canonicalJson([releaseCase.skill.attachment_requirement])
      && attachmentRequirements[0]?.requested_by_job_id === state.methods_preflight_job_id,
    "PHASE2_ATTACHMENT_REQUIREMENT",
    "FAIL",
    "CONTRACT",
  );
  requireCondition(
    view.selected_skill_ref?.id === releaseCase.skill.runtime_ref_id
      && view.selected_skill_ref?.version === releaseCase.skill.version,
    "PHASE2_SELECTED_SKILL",
    "FAIL",
    "CONTRACT",
  );
  exactKeys(descriptor, ["attachment_id", "method", "url", "required_headers", "max_bytes", "expires_at"], "PHASE2_UPLOAD_DESCRIPTOR_SHAPE");
  requireCondition(
    UUID.test(descriptor.attachment_id)
      && descriptor.method === "PUT"
      && descriptor.url === `${publicBaseUrl}/api/v1/attachments/${descriptor.attachment_id}/content`
      && descriptor.max_bytes === MAX_ATTACHMENT_BYTES
      && descriptor.expires_at === null,
    "PHASE2_UPLOAD_DESCRIPTOR",
    "FAIL",
    "CONTRACT",
  );
  exactKeys(descriptor.required_headers, ["Content-Length", "Content-Type", "Idempotency-Key", "X-Content-SHA256"], "PHASE2_UPLOAD_HEADERS_SHAPE");
  requireCondition(
    descriptor.required_headers["Content-Length"] === String(archive.size)
      && descriptor.required_headers["Content-Type"] === archive.content_type
      && descriptor.required_headers["Idempotency-Key"] === descriptor.attachment_id
      && descriptor.required_headers["X-Content-SHA256"] === archive.sha256,
    "PHASE2_UPLOAD_HEADERS",
    "FAIL",
    "CONTRACT",
  );
  return {
    attachment_id: descriptor.attachment_id,
    prepared_case_revision: view.case_revision,
    prepare_expected_case_revision: prepare[0].input.expected_case_revision,
    upload_descriptor: descriptor,
    request_ids: {
      ...requestIds,
      submit_inputs: submits[0].input.request_id,
      prepare: prepare[0].input.request_id,
    },
  };
}

async function uploadAttachment(configuration, state, stageRoot) {
  const descriptor = state.upload_descriptor;
  const archive = path.join(configuration.attemptRoot, "payload", state.archive.name);
  requireCondition(fs.existsSync(archive) && fs.statSync(archive).size === state.archive.size && sha256File(archive) === state.archive.sha256, "UPLOAD_FIXTURE_INVALID");
  requireCondition(descriptor.url === `${state.public_base_url}/api/v1/attachments/${state.attachment_id}/content`, "UPLOAD_URL_INVALID");
  const flatCreate = expectedCreateInput(state.request_ids.create);
  const problemKeys = ["statement", "expected_behavior", "actual_behavior", "scope", "goals", "non_goals", "constraints", "completion_criteria"];
  const createBody = {
    request_id: flatCreate.request_id,
    raw_problem_text: flatCreate.raw_problem_text,
    problem_spec: Object.fromEntries(problemKeys.map((name) => [name, flatCreate[name]])),
    initial_user_facts: flatCreate.initial_user_fact_names.map((name, index) => ({ name, value: flatCreate.initial_user_fact_values[index] })),
    wait_seconds: flatCreate.wait_seconds,
  };
  const prepareBody = {
    request_id: state.request_ids.prepare,
    expected_case_revision: state.prepare_expected_case_revision,
    name: state.archive.name,
    content_type: state.archive.content_type,
    declared_size: state.archive.size,
    declared_sha256: state.archive.sha256,
  };

  const archiveBytes = fs.readFileSync(archive);
  const page = `<!doctype html><html><head><meta charset="utf-8"><title>PENDING</title></head><body><script>
const configuration = ${scriptJson({
    createUrl: `${state.public_base_url}/api/v1/cases`,
    caseUrl: `${state.public_base_url}/api/v1/cases/${state.case_id}`,
    prepareUrl: `${state.public_base_url}/api/v1/cases/${state.case_id}/attachments`,
    createBody,
    prepareBody,
  })};
function encoded(value) {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}
async function jsonRequest(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let envelope = null;
  try { envelope = JSON.parse(text); } catch {}
  return {
    status: response.status,
    envelope,
    correlation_id: response.headers.get("x-problem-locator-correlation-id"),
  };
}
async function upload(label, bytes, descriptor) {
  const response = await fetch(descriptor.url, {
    method: "PUT",
    headers: descriptor.required_headers,
    body: new Blob([bytes], { type: descriptor.required_headers["Content-Type"] }),
  });
  const text = await response.text();
  let envelope = null;
  try { envelope = JSON.parse(text); } catch {}
  return {
    label,
    status: response.status,
    error_code: envelope?.error?.code ?? null,
    data: envelope?.data ?? null,
    correlation_id: response.headers.get("x-problem-locator-correlation-id"),
    response_sha256: response.headers.get("x-content-sha256"),
  };
}
(async () => {
  const create = await jsonRequest(configuration.createUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(configuration.createBody),
  });
  const queryBefore = await jsonRequest(configuration.caseUrl + "?wait_seconds=0");
  const prepare = await jsonRequest(configuration.prepareUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(configuration.prepareBody),
  });
  const descriptor = prepare.envelope?.data?.upload;
  if (!descriptor) throw new Error("REST prepare did not return an upload descriptor");
  const fixture = await fetch("/fixture", { cache: "no-store" });
  if (!fixture.ok) throw new Error("fixture fetch failed");
  const bytes = await fixture.arrayBuffer();
  if (bytes.byteLength < 2) throw new Error("fixture is too small");
  const wrongHash = bytes.slice(0);
  new Uint8Array(wrongHash)[0] ^= 1;
  const results = [
    await upload("size-mismatch", bytes.slice(0, bytes.byteLength - 1), descriptor),
    await upload("hash-mismatch", wrongHash, descriptor),
    await upload("ready", bytes, descriptor),
  ];
  const queryAfter = await jsonRequest(configuration.caseUrl + "?wait_seconds=0");
  document.documentElement.dataset.result = encoded({ ok: true, body_kind: "Blob", byte_length: bytes.byteLength, create, query_before: queryBefore, prepare, descriptor, results, query_after: queryAfter });
  document.title = "DONE";
})().catch((error) => {
  document.documentElement.dataset.result = encoded({ ok: false, error: String(error?.stack ?? error) });
  document.title = "FAILED";
});
</script></body></html>`;

  const startedAtUtc = new Date().toISOString();
  const started = process.hrtime.bigint();
  const { browserOrigin, chrome, chromeRun, failBrowser, result: browserResult } = await runChromePage(
    configuration,
    state,
    "upload",
    page,
    archiveBytes,
  );
  if (!(browserResult.ok === true && browserResult.body_kind === "Blob" && browserResult.byte_length === archiveBytes.length)
    && typeof failBrowser === "function") failBrowser("CHROME_UPLOAD_EXECUTION_FAILED");
  requireCondition(browserResult.ok === true && browserResult.body_kind === "Blob" && browserResult.byte_length === archiveBytes.length, "CHROME_UPLOAD_EXECUTION_FAILED", "FAIL", "BROWSER");
  requireCondition(browserResult.create?.status === 200 && browserResult.create.envelope?.ok === true && browserResult.create.envelope?.data?.business_receipt?.case_id === state.case_id, "CHROME_CREATE_REPLAY_INVALID", "FAIL", "CONTRACT");
  requireCondition(browserResult.query_before?.status === 200 && browserResult.query_before.envelope?.data?.case_view?.status === "WAITING_ATTACHMENT", "CHROME_QUERY_BEFORE_UPLOAD_INVALID", "FAIL", "CONTRACT");
  requireCondition(browserResult.prepare?.status === 200 && browserResult.prepare.envelope?.ok === true && browserResult.prepare.envelope?.data?.application_response?.business_receipt?.primary_resource_id === state.attachment_id, "CHROME_PREPARE_REPLAY_INVALID", "FAIL", "CONTRACT");
  const webDescriptor = browserResult.descriptor;
  exactKeys(webDescriptor, ["attachment_id", "method", "url", "required_headers", "expected_content_length", "max_bytes", "expires_at"], "CHROME_UPLOAD_DESCRIPTOR_SHAPE");
  exactKeys(webDescriptor.required_headers, ["Content-Type", "Idempotency-Key", "X-Content-SHA256"], "CHROME_UPLOAD_HEADERS_SHAPE");
  requireCondition(webDescriptor.attachment_id === state.attachment_id && webDescriptor.method === "PUT" && webDescriptor.url === descriptor.url && webDescriptor.expected_content_length === state.archive.size && webDescriptor.required_headers["Idempotency-Key"] === state.attachment_id && webDescriptor.required_headers["Content-Type"] === state.archive.content_type && webDescriptor.required_headers["X-Content-SHA256"] === state.archive.sha256, "CHROME_UPLOAD_DESCRIPTOR_INVALID", "FAIL", "CONTRACT");
  const [sizeMismatch, hashMismatch, ready] = browserResult.results ?? [];
  requireCondition(sizeMismatch?.label === "size-mismatch" && sizeMismatch.status === 400 && sizeMismatch.error_code === "VALIDATION_ERROR", "CHROME_SIZE_MISMATCH_NOT_REJECTED", "FAIL", "CONTRACT");
  requireCondition(hashMismatch?.label === "hash-mismatch" && hashMismatch.status === 422 && hashMismatch.error_code === "RESOURCE_HASH_MISMATCH", "CHROME_HASH_MISMATCH_NOT_REJECTED", "FAIL", "CONTRACT");
  requireCondition(ready?.label === "ready" && ready.status === 200 && ready.data?.case_id === state.case_id && ready.data?.attachment_id === state.attachment_id && ready.data?.status === "READY" && Number.isInteger(ready.data.case_revision) && ready.data.case_revision > state.prepared_case_revision, "CHROME_UPLOAD_RESPONSE_INVALID", "FAIL", "CONTRACT");
  requireCondition(browserResult.query_after?.status === 200 && browserResult.query_after.envelope?.data?.case_view?.case_revision === ready.data.case_revision, "CHROME_QUERY_AFTER_UPLOAD_INVALID", "FAIL", "CONTRACT");
  requireCondition(typeof ready.correlation_id === "string" && ready.correlation_id.length > 0, "CHROME_CORRELATION_HEADER_NOT_EXPOSED", "FAIL", "CONTRACT");
  const browserHeaders = webDescriptor.required_headers;
  const browserReceipt = {
    schema_version: 1,
    status: "PASS",
    browser: chrome,
    origin: browserOrigin,
    target_origin: new URL(descriptor.url).origin,
    cross_origin: browserOrigin !== new URL(descriptor.url).origin,
    operations: ["create_case", "get_case", "prepare_attachment", "upload_attachment"],
    body_kind: browserResult.body_kind,
    content_length_control: "user-agent",
    scripted_headers: Object.keys(browserHeaders).sort(),
    size_mismatch_status: sizeMismatch.status,
    hash_mismatch_status: hashMismatch.status,
    ready_status: ready.status,
    correlation_header_exposed: true,
  };
  writeNew(path.join(stageRoot, "chrome-upload.json"), browserReceipt);
  writeNew(path.join(stageRoot, "upload.timing.json"), {
    schema_version: 2,
    span: configuration.topology === DUAL_LINUX_TOPOLOGY ? "linux-client-container.http-upload" : "host.http-upload",
    clock_domain: configuration.topology === DUAL_LINUX_TOPOLOGY ? "linux-client-container" : `${configuration.client}-host`,
    started_at_utc: startedAtUtc,
    finished_at_utc: new Date().toISOString(),
    duration_ms: Math.round(Number(process.hrtime.bigint() - started) / 1_000_000),
    request_bytes: fs.statSync(archive).size,
    response_bytes: Buffer.byteLength(chromeRun.stdout),
    http_status: ready.status,
    retries: 0,
    timed_out: false,
  });
  writeTextNew(path.join(stageRoot, "upload.response.json"), `${canonicalJson(ready)}`);
  process.stdout.write("TEST_FLOW_PROGRESS attachment.upload.completed\n");
  return { status: "READY", case_revision: ready.data.case_revision, browser_upload: browserReceipt };
}

function phaseThreePrompt(state, releaseCase) {
  const supplement = releaseCase.driver.supplement_input_names.length > 0
    ? `2. Poll problem_locator_get_case with non-empty case_id and wait_seconds 30 until WAITING_INPUT has exactly these OPEN INPUT requirements: ${canonicalJson(releaseCase.driver.supplement_input_names).trimEnd()}. Use wait_seconds 30 on every poll; do not rapid-poll.\n3. Call problem_locator_submit_supplement exactly once with request_id "${state.request_ids.submit_inputs}", the latest revision, input_names ${canonicalJson(releaseCase.driver.supplement_input_names).trimEnd()}, input_values ${canonicalJson(releaseCase.driver.supplement_input_values).trimEnd()}, attachment_ids [], wait_seconds 0.\n4.`
    : "2. Do not submit any ordinary inputs because this scenario declares none.\n3.";
  return `Perform phase 3 of the controlled Problem Locator journey. Use only the Skill tool and the seven problem_locator Remote MCP tools. Treat structured MCP results as authoritative. Case ${state.case_id}, attachment ${state.attachment_id}, and case_revision ${state.case_revision} are authoritative.

0. First call Skill with exact input {"skill":"problem-locator-client"}; do not call MCP before it succeeds.
1. Call problem_locator_submit_supplement exactly once with request_id "${state.request_ids.submit_attachment}", case_id "${state.case_id}", expected_case_revision ${state.case_revision}, input_names [], input_values [], attachment_ids ["${state.attachment_id}"], wait_seconds 0.
${fixedGetCasePollingInvariant(state.case_id)}
${supplement} Poll with the same literal get-case input. Observe REVIEWING, then continue unchanged until status RESOLVED with methods_result.status RESOLVED. Use wait_seconds 30 on every poll, do not rapid-poll, and do not skip REVIEWING.
5. Call problem_locator_list_artifacts exactly once for this Case and stop. Do not call another tool.`;
}

function validatePhaseThree(audit, state, releaseCase) {
  const records = audit.records;
  const successful = records.filter((record) => record.result?.ok === true);
  const submits = successful.filter((record) => record.tool_name === "problem_locator_submit_supplement");
  const gets = successful.filter((record) => record.tool_name === "problem_locator_get_case");
  const lists = successful.filter((record) => record.tool_name === "problem_locator_list_artifacts");
  const hasSupplement = releaseCase.driver.supplement_input_names.length > 0;
  requireCondition(submits.length === (hasSupplement ? 2 : 1) && gets.length >= (hasSupplement ? 3 : 2) && lists.length === 1, "PHASE3_CALL_CARDINALITY", "FAIL", "CONTRACT");
  requireCondition(records[0] === submits[0] && records.at(-1) === lists[0], "PHASE3_CALL_ORDER", "FAIL", "CONTRACT");
  exactKeys(submits[0].input, ["request_id", "case_id", "expected_case_revision", "input_names", "input_values", "attachment_ids", "wait_seconds"], "PHASE3_ATTACHMENT_INPUT_SHAPE");
  requireCondition(submits[0].input.request_id === state.request_ids.submit_attachment && submits[0].input.case_id === state.case_id && submits[0].input.expected_case_revision === state.case_revision && canonicalJson(submits[0].input.attachment_ids) === canonicalJson([state.attachment_id]), "PHASE3_ATTACHMENT_INPUT", "FAIL", "CONTRACT");
  const views = gets.map((record) => ({ ordinal: record.ordinal, view: caseView(record) })).filter((entry) => entry.view?.case_id === state.case_id);
  let terminalPredecessor = submits[0];
  if (hasSupplement) {
    const inputView = views.find((entry) => entry.view.status === "WAITING_INPUT" && canonicalJson(openRequirementNames(entry.view, "INPUT")) === canonicalJson(releaseCase.driver.supplement_input_names));
    requireCondition(inputView, "PHASE3_INPUT_REQUIREMENT_NOT_OBSERVED", "FAIL", "CONTRACT");
    exactKeys(submits[1].input, ["request_id", "case_id", "expected_case_revision", "input_names", "input_values", "attachment_ids", "wait_seconds"], "PHASE3_INPUT_SHAPE");
    requireCondition(submits[1].input.request_id === state.request_ids.submit_inputs && submits[1].input.case_id === state.case_id && submits[1].input.expected_case_revision === inputView.view.case_revision && canonicalJson(submits[1].input.input_names) === canonicalJson(releaseCase.driver.supplement_input_names) && canonicalJson(submits[1].input.input_values) === canonicalJson(releaseCase.driver.supplement_input_values) && canonicalJson(submits[1].input.attachment_ids) === canonicalJson([]), "PHASE3_INPUT", "FAIL", "CONTRACT");
    terminalPredecessor = submits[1];
  }
  const reviewing = views.find((entry) => entry.ordinal > terminalPredecessor.ordinal && entry.view.status === "REVIEWING");
  const resolved = [...views].reverse().find((entry) => entry.ordinal > (reviewing?.ordinal ?? Infinity) && entry.view.status === releaseCase.result_expectation.case_status);
  requireCondition(
    reviewing && resolved && resolved.view.methods_result?.schema_version === 2
      && resolved.view.methods_result.status === "RESOLVED"
      && resolved.view.final_result === null && resolved.view.unresolved_result === null
      && resolved.view.generic_result === null && resolved.view.generic_result_v2 === null,
    "PHASE3_METHODS_V2_RESOLUTION",
    "FAIL",
    "CONTRACT",
  );
  requireCondition(resolved.view.selected_skill_ref?.id === releaseCase.skill.runtime_ref_id && resolved.view.selected_skill_ref?.version === releaseCase.skill.version, "PHASE3_SELECTED_SKILL", "FAIL", "CONTRACT");
  const listData = successData(lists[0]);
  const artifacts = listData.artifacts;
  requireCondition(Array.isArray(artifacts) && artifacts.length === 0 && Array.isArray(resolved.view.artifacts) && resolved.view.artifacts.length === 0, "PHASE3_METHODS_V2_ARTIFACTS_PRESENT", "FAIL", "CONTRACT");
  return {
    case_id: state.case_id,
    attachment_id: state.attachment_id,
    resolved_case_revision: resolved.view.case_revision,
    diagnosis_state_revision: resolved.view.diagnosis_state_revision,
    selected_skill_ref: resolved.view.selected_skill_ref,
    methods_result: resolved.view.methods_result,
    observed_statuses: views.map((entry) => entry.view.status),
    rest_supplements: submits.map((record) => ({
      request_id: record.input.request_id,
      expected_case_revision: record.input.expected_case_revision,
      inputs: record.input.input_names.map((name, index) => ({ name, value: record.input.input_values[index] })),
      attachment_ids: record.input.attachment_ids,
      wait_seconds: record.input.wait_seconds,
    })),
  };
}

async function verifyResolvedWebApi(configuration, state, summary, stageRoot) {
  const page = `<!doctype html><html><head><meta charset="utf-8"><title>PENDING</title></head><body><script>
const configuration = ${scriptJson({
    caseUrl: `${state.public_base_url}/api/v1/cases/${state.case_id}`,
    supplementsUrl: `${state.public_base_url}/api/v1/cases/${state.case_id}/supplements`,
    artifactsUrl: `${state.public_base_url}/api/v1/cases/${state.case_id}/artifacts`,
    supplements: summary.rest_supplements,
  })};
function encoded(value) {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}
async function jsonRequest(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let envelope = null;
  try { envelope = JSON.parse(text); } catch {}
  return { status: response.status, envelope, correlation_id: response.headers.get("x-problem-locator-correlation-id") };
}
(async () => {
  const supplements = [];
  for (const body of configuration.supplements) {
    supplements.push(await jsonRequest(configuration.supplementsUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }));
  }
  const query = await jsonRequest(configuration.caseUrl + "?wait_seconds=30");
  const artifacts = await jsonRequest(configuration.artifactsUrl);
  document.documentElement.dataset.result = encoded({ ok: true, supplements, query, artifacts });
  document.title = "DONE";
})().catch((error) => {
  document.documentElement.dataset.result = encoded({ ok: false, error: String(error?.stack ?? error) });
  document.title = "FAILED";
});
</script></body></html>`;
  const { browserOrigin, chrome, failBrowser, result } = await runChromePage(configuration, state, "resolved-api", page);
  if (result.ok !== true && typeof failBrowser === "function") failBrowser("CHROME_RESOLVED-API_EXECUTION_FAILED");
  requireCondition(result.ok === true, "CHROME_RESOLVED_API_EXECUTION_FAILED", "FAIL", "BROWSER");
  requireCondition(Array.isArray(result.supplements) && result.supplements.length === summary.rest_supplements.length && result.supplements.every((item) => item.status === 200 && item.envelope?.ok === true && typeof item.correlation_id === "string"), "CHROME_SUPPLEMENT_REPLAY_INVALID", "FAIL", "CONTRACT");
  const restView = result.query?.envelope?.data?.case_view;
  requireCondition(
    result.query?.status === 200 && restView?.case_id === state.case_id
      && restView.case_revision === summary.resolved_case_revision
      && result.query.envelope?.data?.wait_timed_out === false
      && restView.status === "RESOLVED"
      && canonicalJson(restView.methods_result) === canonicalJson(summary.methods_result)
      && restView.final_result === null && restView.unresolved_result === null
      && restView.generic_result === null && restView.generic_result_v2 === null,
    "CHROME_TERMINAL_QUERY_INVALID",
    "FAIL",
    "CONTRACT",
  );
  const listed = result.artifacts?.envelope?.data?.artifacts;
  requireCondition(result.artifacts?.status === 200 && Array.isArray(listed) && listed.length === 0, "CHROME_METHODS_V2_ARTIFACT_LIST_INVALID", "FAIL", "CONTRACT");
  const receipt = {
    schema_version: 1,
    status: "PASS",
    browser: chrome,
    origin: browserOrigin,
    target_origin: new URL(state.public_base_url).origin,
    cross_origin: browserOrigin !== new URL(state.public_base_url).origin,
    operations: ["submit_supplement", "get_case", "list_artifacts"],
    supplement_replays: result.supplements.length,
    methods_result_sha256: sha256Bytes(canonicalJson(summary.methods_result)),
    artifacts_verified: 0,
    correlation_header_exposed: true,
  };
  writeNew(path.join(stageRoot, "chrome-resolved-api.json"), receipt);
  return receipt;
}

function restartPrompt(state) {
  return `Perform one read-only post-restart persistence verification for Case ${state.case_id}. Treat only Remote MCP structured results as authoritative.
0. First call Skill exactly once with {"skill":"problem-locator-client"}.
1. Call problem_locator_get_case exactly once with case_id "${state.case_id}", wait_for_job_id null, wait_seconds 0.
2. Call problem_locator_list_artifacts exactly once with case_id "${state.case_id}".
3. Stop. Do not create, prepare, submit, resume, cancel, upload, or call another tool.`;
}

function validateRestart(audit, state, releaseCase) {
  const records = audit.records;
  requireCondition(records.length === 2 && records[0].tool_name === "problem_locator_get_case" && records[1].tool_name === "problem_locator_list_artifacts", "RESTART_CALL_SEQUENCE", "FAIL", "CONTRACT");
  const view = caseView(records[0]);
  const artifacts = successData(records[1]).artifacts;
  requireCondition(view?.case_id === state.case_id && view.status === releaseCase.result_expectation.case_status && view.case_revision === state.resolved_case_revision, "RESTART_CASE_MISMATCH", "FAIL", "CONTRACT");
  requireCondition(view.selected_skill_ref?.id === releaseCase.skill.runtime_ref_id && view.selected_skill_ref?.version === releaseCase.skill.version, "RESTART_SELECTED_SKILL", "FAIL", "CONTRACT");
  requireCondition(Array.isArray(artifacts) && artifacts.length === 0, "RESTART_METHODS_V2_ARTIFACTS_PRESENT", "FAIL", "CONTRACT");
  return { case_view: view, artifacts };
}

function indexEventParts(configuration, state, mode, indexLabel = null) {
  const attemptRoot = configuration.attemptRoot;
  const runId = state.run_id;
  const partsRoot = path.join(attemptRoot, "payload", "events", "parts");
  const suffix = mode === "journey" ? "journey.ndjson" : "diagnostics.ndjson";
  const streams = [];
  let eventCount = 0;
  for (const instance of INSTANCE_ORDER) {
    const part = path.join(partsRoot, `service-linux.${instance}.${suffix}`);
    const receipt = path.join(attemptRoot, "payload", `service-${instance}-${mode}-relay.json`);
    if (!fs.existsSync(part) && !fs.existsSync(receipt)) continue;
    let relayed;
    try {
      relayed = readRelayedEventPart({
        filePath: part,
        receiptPath: receipt,
        expectedProducerId: mode === "journey" ? `service-linux-${instance}` : `service-linux-diagnostics-${instance}`,
        expectedRunId: runId,
        allowEmpty: mode === "journey" && ["route", "restart"].includes(instance),
      });
    } catch (error) {
      const code = typeof error?.code === "string" && /^[A-Z0-9_]+$/.test(error.code)
        ? error.code
        : "EVENT_PART_INVALID";
      throw new StageError(`SERVICE_${code}`, "ERROR", "HARNESS");
    }
    eventCount += relayed.event_count;
    streams.push({
      instance,
      producer_id: relayed.receipt.producer_id,
      clock_domain: relayed.receipt.clock_domain,
      event_count: relayed.event_count,
      events_path: path.relative(attemptRoot, part).split(path.sep).join("/"),
      events_sha256: relayed.receipt.events_sha256,
      raw_path: path.relative(attemptRoot, part.replace(/\.ndjson$/, ".raw")).split(path.sep).join("/"),
      raw_sha256: relayed.receipt.raw_sha256,
      receipt_path: path.relative(attemptRoot, receipt).split(path.sep).join("/"),
    });
  }
  requireCondition(eventCount > 0 || (mode === "journey" && streams.some((stream) => ["route", "restart"].includes(stream.instance))), `SERVICE_${mode.toUpperCase()}_EVENTS_EMPTY`);
  const index = { schema_version: 2, authority: "producer-streams", aggregate_is_authoritative: false, mode, event_count: eventCount, streams };
  const suffixName = indexLabel ? `-${indexLabel}` : "";
  writeNew(path.join(configuration.stageRoot, `${mode}-event-index${suffixName}.json`), index);
  return index;
}

function verifyCorrespondence(state, attemptRoot) {
  const correspondence = readServerMcpCorrespondence(attemptRoot, state.client_calls, {
    validationProbeRequestId: state.validation_probe_request_id,
  });
  requireCondition(correspondence.started_exact, "CLIENT_SERVER_STARTED_CORRESPONDENCE", "FAIL", "CONTRACT");
  requireCondition(correspondence.completed_exact, "CLIENT_SERVER_COMPLETED_CORRESPONDENCE", "FAIL", "CONTRACT");
  requireCondition(correspondence.pair_exact && correspondence.request_exact, "CLIENT_SERVER_REQUEST_CORRESPONDENCE", "FAIL", "CONTRACT");
  requireCondition(correspondence.tools_listed_exact, "SERVER_TOOLS_LISTED_INCOMPLETE", "FAIL", "CONTRACT");
  requireCondition(correspondence.validation_probe_exact, "SERVER_VALIDATION_DFX_INCOMPLETE", "FAIL", "CONTRACT");
  return { status: "PASS", client_tool_calls: state.client_calls.length, server_started: correspondence.started_tool_names.length, server_completed: correspondence.completed_tool_names.length, request_exact: true, pair_exact: true, tools_listed_exact: true, validation_probe_exact: true, validation_probe_request_id: state.validation_probe_request_id, validation_fields: correspondence.validation_fields, client_dfx_absent: true };
}

async function runServerDfxProbe(configuration, state) {
  requireCondition(!state.validation_probe_request_id, "SERVER_DFX_PROBE_ALREADY_RUN");
  const requestId = `test-flow-${sha256Bytes(state.run_id).slice(0, 16)}-invalid-v2`;
  const containerOutput = `/evidence/stages/${configuration.stage}/server-dfx-probe.json`;
  await docker(configuration.dockerContext, [
    "exec", state.active_container,
    "/opt/venvs/xiaodao/bin/python", "-I", "/test-flow-runtime/server_dfx_probe.py",
    "--request-id", requestId,
    "--output", containerOutput,
  ]);
  const receipt = readJson(path.join(configuration.stageRoot, "server-dfx-probe.json"));
  requireCondition(
    receipt?.schema_version === 2
      && receipt.status === "PASS"
      && receipt.validation_probe_request_id === requestId
      && receipt.flat_schema === true
      && receipt.tool_count === 7
      && canonicalJson(receipt.tool_names) === canonicalJson(TOOL_NAMES)
      && canonicalJson(receipt.validation_fields) === canonicalJson(NEGATIVE_PROBE_VALIDATION_FIELDS),
    "SERVER_DFX_PROBE_RECEIPT_INVALID",
    "FAIL",
    "CONTRACT",
  );
  state.validation_probe_request_id = requestId;
  atomicState(configuration.statePath, state);
  return receipt;
}

async function startService(configuration, state, instance, { allowEmptyJourney = true } = {}) {
  requireCondition(!state.current_instance, "SERVICE_ALREADY_RUNNING");
  const bootstrapLog = `/evidence/stages/${configuration.stage}/supervisor-${instance}.log`;
  const journeyPolicy = allowEmptyJourney ? "allow-empty" : "require-events";
  await docker(configuration.dockerContext, [
    "exec", "--detach", state.active_container,
    "sh", "-c", 'exec sh /test-flow-runtime/service-supervisor.sh "$1" "$2" >"$3" 2>&1',
    "test-flow-supervisor", instance, journeyPolicy, bootstrapLog,
  ]);
  state.current_instance = instance;
  atomicState(configuration.statePath, state);
  try {
    return await waitReady(configuration, state);
  } catch (error) {
    try { await captureReadinessDiagnostic(configuration, state, instance, error?.code ?? "SERVICE_READINESS_ERROR"); } catch {}
    throw error;
  }
}

function relayFailureFromServiceReceipt(configuration, instance) {
  const payloadRoot = path.join(configuration.attemptRoot, "payload");
  const supervisorPath = path.join(payloadRoot, `service-${instance}-supervisor.json`);
  if (!fs.existsSync(supervisorPath)) return null;
  try {
    const supervisor = readJson(supervisorPath);
    if (supervisor?.schema_version !== 1 || supervisor.instance !== instance || supervisor.status !== "FAIL") return null;
    const relayKind = supervisor.code === "JOURNEY_RELAY"
      ? "journey"
      : supervisor.code === "DIAGNOSTIC_RELAY" ? "diagnostics" : null;
    if (!relayKind) return null;
    const relayPath = path.join(payloadRoot, `service-${instance}-${relayKind}-relay.json`);
    const relay = fs.existsSync(relayPath) ? readJson(relayPath) : null;
    const relayCode = relay?.status === "FAIL" && /^[A-Z0-9_]+$/.test(relay.code ?? "")
      ? `_${relay.code}`
      : "";
    return new StageError(`SERVICE_${instance.toUpperCase()}_${supervisor.code}${relayCode}`, "ERROR", "HARNESS");
  } catch {
    return null;
  }
}

async function quiesceService(configuration, state) {
  requireCondition(typeof state.current_instance === "string", "SERVICE_NOT_RUNNING");
  const instance = state.current_instance;
  try {
    await docker(configuration.dockerContext, ["exec", state.active_container, "sh", "/test-flow-runtime/stop-service.sh", instance]);
  } catch (error) {
    throw relayFailureFromServiceReceipt(configuration, instance) ?? error;
  }
  state.current_instance = null;
  atomicState(configuration.statePath, state);
}

async function stopEnvironmentDiagnostic(configuration, state) {
  await quiesceService(configuration, state);
  return indexEventParts(configuration, state, "diagnostics");
}

async function stopService(configuration, state, { indexLabel = null } = {}) {
  const instance = state.current_instance;
  await quiesceService(configuration, state);
  indexEventParts(configuration, state, "journey", indexLabel);
  indexEventParts(configuration, state, "diagnostics", indexLabel);
  const serviceUsage = await auditServiceAgentUsage(configuration, state, instance);
  return {
    ...verifyCorrespondence(state, configuration.attemptRoot),
    service_invocations: serviceUsage.invocations,
    service_no_model_jobs: serviceUsage.noModelJobs,
  };
}

async function archiveFailureServiceEvidence(configuration) {
  if (!configuration.statePath || !fs.existsSync(configuration.statePath)) return;
  const state = readJson(configuration.statePath);
  if (state.schema_version !== 1 || state.run_id !== path.basename(configuration.attemptRoot)) return;
  if (typeof state.current_instance === "string") {
    try { await quiesceService(configuration, state); } catch {}
  }
  for (const mode of ["journey", "diagnostics"]) {
    try {
      const indexPath = path.join(configuration.stageRoot, `${mode}-event-index.json`);
      if (!fs.existsSync(indexPath)) indexEventParts(configuration, state, mode);
    } catch {}
  }
}

export async function installGeneratedSkill(configuration, state, containerName, stageId, dockerRunner = docker) {
  const installedRoot = "/opt/e2e-skills";
  await dockerRunner(configuration.dockerContext, [
    "exec", containerName,
    "sh", "-eu", "-c",
    `source_root=/run/generated-specialized-skill
installed_root=/opt/e2e-skills
test -d "$source_root"
test ! -e "$installed_root"
install -d -m 0555 -o 0 -g 0 "$installed_root"
cp -a "$source_root"/. "$installed_root"/
diff -qr "$source_root" "$installed_root"
test -z "$(find "$installed_root" -type l -print -quit)"
test -z "$(find "$installed_root" -type f -links +1 -print -quit)"
chown -R 0:0 "$installed_root"
find "$installed_root" -type d -exec chmod 0555 {} +
find "$installed_root" -type f -exec chmod 0444 {} +
test -z "$(find "$installed_root" -xdev -perm /022 -print -quit)"
test -z "$(find "$installed_root" -xdev ! -user root -print -quit)"
test -z "$(find "$installed_root" -xdev ! -group root -print -quit)"`,
  ]);
  const installedProbe = await dockerRunner(configuration.dockerContext, [
    "exec", containerName,
    "/opt/venvs/xiaodao/bin/python", "-I", "-c",
    `import hashlib,json,os,stat,sys
root=sys.argv[1]
records=[]
for current, directories, files in os.walk(root, followlinks=False):
    directories.sort(); files.sort()
    for name in files:
        absolute=os.path.join(current,name); metadata=os.lstat(absolute)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1: raise SystemExit(2)
        payload=open(absolute,"rb").read()
        records.append({"path":os.path.relpath(absolute,root).replace(os.sep,"/"),"size":len(payload),"sha256":hashlib.sha256(payload).hexdigest()})
print(json.dumps(records,ensure_ascii=False,separators=(",",":"),sort_keys=True))`,
    installedRoot,
  ], { forward: false });
  const installedEntries = JSON.parse(installedProbe.stdout).sort((left, right) => (
    left.path < right.path ? -1 : left.path > right.path ? 1 : 0
  ));
  const installedContentTreeSha256 = sha256Bytes(canonicalJson({ version: 1, entries: installedEntries }));
  requireCondition(installedContentTreeSha256 === configuration.generatedSkill.content_tree_sha256, "GENERATED_SKILL_INSTALLED_TREE_DRIFT", "FAIL", "CONTRACT");
  const receipt = {
    schema_version: 1,
    status: "PASS",
    registration_id: configuration.generatedSkill.registration_id,
    skill_name: configuration.generatedSkill.skill_name,
    tree_digest: configuration.generatedSkill.tree_digest,
    package_digest: configuration.generatedSkill.package_digest,
    registration_sha256: configuration.generatedSkill.registration_sha256,
    package_tree_sha256: configuration.generatedSkill.package_tree_sha256,
    combined_sha256: configuration.generatedSkill.combined_sha256,
    content_tree_sha256: configuration.generatedSkill.content_tree_sha256,
    generation_receipt_sha256: configuration.generatedSkill.generation_receipt_sha256,
    installed_content_tree_sha256: installedContentTreeSha256,
    source_wiki_sha256: configuration.generatedSkill.source_wiki_sha256,
    mount: "/run/generated-specialized-skill",
    installed_root: installedRoot,
    container: containerName,
  };
  writeNew(path.join(configuration.attemptRoot, "payload", "stages", stageId, "generated-skill-install.json"), receipt);
  state.generated_skill = {
    registration_id: receipt.registration_id,
    skill_name: receipt.skill_name,
    tree_digest: receipt.tree_digest,
    package_digest: receipt.package_digest,
    registration_sha256: receipt.registration_sha256,
    package_tree_sha256: receipt.package_tree_sha256,
    combined_sha256: receipt.combined_sha256,
    content_tree_sha256: receipt.content_tree_sha256,
    generation_receipt_sha256: receipt.generation_receipt_sha256,
    installed_content_tree_sha256: receipt.installed_content_tree_sha256,
    source_wiki_sha256: receipt.source_wiki_sha256,
  };
  atomicState(configuration.statePath, state);
  return receipt;
}

async function initializeContainer(configuration, state, containerName, mode, stageId) {
  const receipt = `/evidence/stages/${stageId}/container-init.json`;
  const generatedSkillInstall = await installGeneratedSkill(configuration, state, containerName, stageId);
  await docker(configuration.dockerContext, [
    "exec", containerName,
    "sh", "/test-flow-runtime/initialize-container.sh",
    mode,
    configuration.sourceSnapshotDigest,
    RELEASE_LOGPARSE_COMMIT,
    RELEASE_MCP_COMMIT,
    receipt,
  ]);
  const hostReceipt = path.join(configuration.attemptRoot, "payload", "stages", stageId, "container-init.json");
  const initialization = readJson(hostReceipt);
  const caseReceipt = readJson(
    path.join(configuration.attemptRoot, "payload", "stages", stageId, "release-case.json"),
  );
  requireCondition(
    initialization.status === "PASS"
      && initialization.xiaodao_snapshot_digest === configuration.sourceSnapshotDigest
      && initialization.case_source_redacted === true
      && SHA256.test(initialization.logparse_config_sha256),
    "CONTAINER_INIT_RECEIPT_INVALID",
  );
  requireCondition(
    caseReceipt?.schema_version === 2
      && caseReceipt.status === "PASS"
      && caseReceipt.case_id === configuration.releaseCase.case_id
      && caseReceipt.scenario_id === configuration.releaseCase.scenario_id
      && caseReceipt.registration_id === configuration.generatedSkill.registration_id
      && caseReceipt.skill_name === configuration.generatedSkill.skill_name
      && caseReceipt.runtime_ref_id === configuration.releaseCase.skill.runtime_ref_id
      && caseReceipt.version === configuration.releaseCase.skill.version
      && caseReceipt.source_wiki_sha256 === configuration.generatedSkill.source_wiki_sha256
      && caseReceipt.registration_sha256 === configuration.generatedSkill.registration_sha256
      && caseReceipt.package_tree_sha256 === configuration.generatedSkill.package_tree_sha256
      && caseReceipt.combined_sha256 === configuration.generatedSkill.combined_sha256
      && caseReceipt.logparse_product === configuration.releaseCase.logparse_product
      && caseReceipt.attachment_requirement === configuration.releaseCase.skill.attachment_requirement
      && caseReceipt.archive_projection === "frozen-raw-log-v1"
      && Number.isSafeInteger(caseReceipt.logparse_config_size)
      && caseReceipt.logparse_config_size > 0
      && SHA256.test(caseReceipt.logparse_config_sha256)
      && caseReceipt.logparse_config_sha256 === initialization.logparse_config_sha256
      && typeof caseReceipt.archive_name === "string"
      && caseReceipt.archive_content_type === "application/zip"
      && Number.isSafeInteger(caseReceipt.archive_size)
      && caseReceipt.archive_size > 0
      && SHA256.test(caseReceipt.archive_sha256)
      && Number.isSafeInteger(caseReceipt.archive_member_count)
      && caseReceipt.archive_member_count > 0,
    "RELEASE_CASE_RECEIPT_INVALID",
  );
  state.release_case = {
    case_id: configuration.releaseCase.case_id,
    scenario_id: configuration.releaseCase.scenario_id,
    input_digest: configuration.releaseCase.input_digest,
    oracle_digest: configuration.releaseCase.oracle_digest,
    skill_id: configuration.releaseCase.skill.id,
    registration_id: caseReceipt.registration_id,
    skill_name: caseReceipt.skill_name,
    combined_sha256: caseReceipt.combined_sha256,
    logparse_product: caseReceipt.logparse_product,
    logparse_config_sha256: caseReceipt.logparse_config_sha256,
  };
  state.archive = {
    name: caseReceipt.archive_name,
    content_type: caseReceipt.archive_content_type,
    size: caseReceipt.archive_size,
    sha256: caseReceipt.archive_sha256,
    member_count: caseReceipt.archive_member_count,
  };
  atomicState(configuration.statePath, state);
  return { initialization, caseReceipt, generatedSkillInstall };
}

async function createContainer(configuration, state, containerName, mode, stageId, register = true) {
  if (register) appendResource(configuration.resourceRegistry, configuration.attemptRoot, "container", containerName, configuration.resourceLabel);
  const networkArguments = configuration.topology === DUAL_LINUX_TOPOLOGY
    ? ["--network", state.network, "--network-alias", "problem-locator-server"]
    : ["--network", "bridge", "--publish", `127.0.0.1:${state.port}:8000/tcp`];
  await docker(configuration.dockerContext, [
    "run", "--detach", "--init",
    "--name", containerName,
    "--label", configuration.resourceLabel,
    "--pull", "never",
    "--platform", "linux/amd64",
    ...networkArguments,
    "--env", `E2E_RUN_ID=${state.run_id}`,
    "--env", `E2E_PUBLIC_BASE_URL=${state.public_base_url}`,
    "--env", `TEST_FLOW_SERVICE_MODEL=${RELEASE_MODEL}`,
    "--env", `TEST_FLOW_SERVICE_MAX_TURNS=${configuration.serviceAgentCaps.max_turns}`,
    "--env", `TEST_FLOW_SERVICE_MAX_TOTAL_TOKENS=${configuration.serviceAgentCaps.max_total_tokens}`,
    "--env", `TEST_FLOW_SERVICE_MAX_BUDGET_USD=${configuration.serviceAgentCaps.max_budget_usd}`,
    "--env", `TEST_FLOW_SERVICE_HARD_TIMEOUT_SECONDS=${configuration.serviceAgentCaps.hard_timeout_seconds}`,
    "--tmpfs", "/root/.claude:rw,noexec,nosuid,nodev,mode=0700,size=536870912",
    "--tmpfs", "/run/plagent-claude:rw,noexec,nosuid,nodev,mode=0700,size=536870912",
    "--mount", `type=bind,src=${configuration.repoRoot},dst=/source/xiaodao,readonly`,
    "--mount", `type=bind,src=${configuration.logparseSource},dst=/source/logparse,readonly`,
    "--mount", `type=bind,src=${configuration.mcpSource},dst=/source/problem-locator-mcp,readonly`,
    "--mount", `type=bind,src=${configuration.containerClaudeSettings},dst=/run/host-claude-settings.json,readonly`,
    "--mount", `type=bind,src=${path.join(configuration.repoRoot, ".claude", "skills", "logparse-diagnose")},dst=/run/plagent-claude/.claude/skills/logparse-diagnose,readonly`,
    "--mount", `type=bind,src=${path.join(configuration.repoRoot, "tools", "test-flow", "runtime-support")},dst=/test-flow-runtime,readonly`,
    "--mount", `type=bind,src=${configuration.generatedSkill.root},dst=/run/generated-specialized-skill,readonly`,
    "--mount", `type=bind,src=${path.join(configuration.attemptRoot, "payload")},dst=/evidence`,
    "--mount", `type=volume,src=${state.volume},dst=/var/lib/problem-locator`,
    state.image_id,
    "sleep", "infinity",
  ]);
  state.active_container = containerName;
  atomicState(configuration.statePath, state);
}

async function createLinuxClientContainer(configuration, state) {
  const runtime = ensureClientRuntime(configuration, state);
  const clientUser = linuxClientUserIdentity();
  appendResource(configuration.resourceRegistry, configuration.attemptRoot, "container", state.client_container, configuration.resourceLabel);
  await docker(configuration.dockerContext, [
    "run", "--detach", "--init",
    "--name", state.client_container,
    "--label", configuration.resourceLabel,
    "--user", clientUser.docker_user,
    "--pull", "never",
    "--platform", "linux/amd64",
    "--network", state.network,
    "--network-alias", "problem-locator-client",
    "--read-only",
    "--env", `HOME=${LINUX_CLIENT_HOME}`,
    "--env", "NO_PROXY=problem-locator-server,problem-locator-client,127.0.0.1,localhost",
    "--env", "no_proxy=problem-locator-server,problem-locator-client,127.0.0.1,localhost",
    "--shm-size", "1073741824",
    "--tmpfs", "/tmp:rw,exec,nosuid,nodev,mode=1777,size=1073741824",
    "--tmpfs", `/client-home:rw,noexec,nosuid,nodev,mode=0700,uid=${clientUser.uid},gid=${clientUser.gid},size=536870912`,
    "--mount", `type=bind,src=${configuration.repoRoot},dst=/workspace,readonly`,
    "--mount", `type=bind,src=${runtime.runtimeRoot},dst=/client-runtime`,
    state.client_image_id,
    "sleep", "infinity",
  ]);
  state.selected_client_runtime_observed = await probeLinuxClientRuntime(configuration, state);
}

async function probeLinuxClientRuntime(configuration, state) {
  const runtimeProbe = await docker(configuration.dockerContext, [
    "exec", state.client_container,
    "/usr/bin/node", "-e",
    `const crypto=require("node:crypto");const fs=require("node:fs");const cp=require("node:child_process");
const digest=(file)=>crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
const home=process.env.HOME??null;
let homeWritable=false;
let homeRealpath=null;
if(home){homeRealpath=fs.realpathSync(home);const probe=home+"/.test-flow-home-"+process.pid+"-"+crypto.randomUUID();fs.writeFileSync(probe,"",{flag:"wx",mode:0o600});fs.unlinkSync(probe);homeWritable=true;}
process.stdout.write(JSON.stringify({
  uid:typeof process.getuid==="function"?process.getuid():null,
  gid:typeof process.getgid==="function"?process.getgid():null,
  home,
  home_realpath:homeRealpath,
  home_writable:homeWritable,
  node_version:process.version,
  node_architecture:process.arch,
  node_executable:fs.realpathSync(process.execPath),
  node_sha256:digest(process.execPath),
  claude_cli_sha256:digest("/opt/claude-code/cli.js"),
  claude_version:cp.execFileSync("/usr/local/bin/claude",["--version"],{encoding:"utf8"}).trim(),
  headless_shell_sha256:digest("/opt/chrome-headless-shell/chrome-headless-shell"),
  headless_shell_version:cp.execFileSync("/opt/chrome-headless-shell/chrome-headless-shell",["--version"],{encoding:"utf8"}).trim(),
}));`,
  ], { forward: false });
  const runtimeIdentity = JSON.parse(runtimeProbe.stdout);
  requireCondition(
    Number.isSafeInteger(runtimeIdentity.uid)
      && runtimeIdentity.uid > 0
      && Number.isSafeInteger(runtimeIdentity.gid)
      && runtimeIdentity.gid >= 0
      && runtimeIdentity.home === LINUX_CLIENT_HOME
      && runtimeIdentity.home_realpath === LINUX_CLIENT_HOME
      && runtimeIdentity.home_writable === true
      && /^v\d+\./.test(runtimeIdentity.node_version ?? "")
      && runtimeIdentity.node_architecture === "x64"
      && path.isAbsolute(runtimeIdentity.node_executable ?? "")
      && SHA256.test(runtimeIdentity.node_sha256 ?? "")
      && runtimeIdentity.claude_cli_sha256 === RELEASE_CLAUDE_CLI_SHA256
      && runtimeIdentity.claude_version === RELEASE_CLAUDE_VERSION_OUTPUT
      && runtimeIdentity.headless_shell_sha256 === RELEASE_CHROME_HEADLESS_SHELL_EXECUTABLE_SHA256
      && runtimeIdentity.headless_shell_version === RELEASE_CHROME_HEADLESS_SHELL_VERSION_OUTPUT,
    "LINUX_CLIENT_RUNTIME_IDENTITY_DRIFT",
    "BLOCKED",
    "INFRA",
  );
  return {
    schema_version: 1,
    status: "PASS",
    platform: "linux/amd64",
    image_id: state.client_image_id,
    identity_boundary: "client-image-id",
    user: { uid: runtimeIdentity.uid, gid: runtimeIdentity.gid, root: false },
    node: {
      version: runtimeIdentity.node_version,
      architecture: runtimeIdentity.node_architecture,
      executable: runtimeIdentity.node_executable,
      sha256: runtimeIdentity.node_sha256,
    },
    claude: { version: runtimeIdentity.claude_version, cli_sha256: runtimeIdentity.claude_cli_sha256 },
    headless_shell: {
      product: RELEASE_CHROME_HEADLESS_SHELL_PRODUCT,
      version: runtimeIdentity.headless_shell_version,
      executable_sha256: runtimeIdentity.headless_shell_sha256,
    },
  };
}

async function createFreshEnvironment(configuration, stageRoot, runtimeIdentity) {
  requireCondition(configuration.freshDataRoot, "FRESH_DATA_ROOT_FLAG_REQUIRED", "BLOCKED", "INFRA");
  requireCondition(!fs.existsSync(configuration.statePath), "ADAPTER_STATE_ALREADY_EXISTS");
  const runId = path.basename(configuration.attemptRoot);
  const dualLinuxContainers = configuration.topology === DUAL_LINUX_TOPOLOGY;
  const port = dualLinuxContainers ? null : await availablePort();
  const imageInspect = await docker(configuration.dockerContext, ["image", "inspect", configuration.baseImage], { forward: false });
  const imageMetadata = JSON.parse(imageInspect.stdout)[0];
  requireCondition(imageMetadata?.Os === "linux" && imageMetadata?.Architecture === "amd64" && typeof imageMetadata?.Id === "string", "RELEASE_IMAGE_IDENTITY_INVALID", "BLOCKED", "INFRA");
  if (configuration.expectedServerImageId) requireCondition(imageMetadata.Id === configuration.expectedServerImageId, "RELEASE_SERVER_IMAGE_ID_DRIFT", "BLOCKED", "INFRA");
  let clientImageMetadata = null;
  if (dualLinuxContainers) {
    const clientImageInspect = await docker(configuration.dockerContext, ["image", "inspect", RELEASE_CLIENT_IMAGE], { forward: false });
    clientImageMetadata = JSON.parse(clientImageInspect.stdout)[0];
    const labels = clientImageMetadata?.Config?.Labels ?? {};
    requireCondition(
      clientImageMetadata?.Os === "linux"
        && clientImageMetadata?.Architecture === "amd64"
        && typeof clientImageMetadata?.Id === "string"
        && clientImageMetadata.Id === configuration.expectedClientImageId
        && labels["problem-locator.e2e.role"] === "linux-client"
        && labels["problem-locator.e2e.claude"] === `npm-${RELEASE_CLAUDE_VERSION}`
        && labels["problem-locator.e2e.chrome-headless-shell-version"] === RELEASE_CHROME_HEADLESS_SHELL_VERSION
        && labels["problem-locator.e2e.chrome-headless-shell-sha256"] === RELEASE_CHROME_HEADLESS_SHELL_EXECUTABLE_SHA256
        && labels["problem-locator.e2e.chrome-headless-shell-archive-sha256"] === RELEASE_CHROME_HEADLESS_SHELL_ARCHIVE_SHA256,
      "RELEASE_CLIENT_IMAGE_IDENTITY_INVALID",
      "BLOCKED",
      "INFRA",
    );
  }
  const state = {
    schema_version: 1,
    run_id: runId,
    volume: safeName("pltf-data", runId),
    network: dualLinuxContainers ? safeName("pltf-net", runId) : null,
    client_container: dualLinuxContainers ? safeName("pltf-client", runId) : null,
    initial_container: safeName("pltf-server", runId, "initial"),
    restart_container: safeName("pltf-server", runId, "restart"),
    active_container: null,
    current_instance: null,
    port,
    public_base_url: dualLinuxContainers ? "http://problem-locator-server:8000" : `http://127.0.0.1:${port}`,
    image_id: imageMetadata.Id,
    client_image_id: clientImageMetadata?.Id ?? null,
    topology: configuration.topology,
    runtime_images: {
      server_image_id: configuration.expectedServerImageId,
      client_image_id: dualLinuxContainers ? configuration.expectedClientImageId : null,
    },
    runtime_identity: runtimeIdentity,
    selected_client_runtime_observed: null,
    request_ids: {
      create: `${configuration.client}-${sha256Bytes(runId).slice(0, 12)}-create-v1`,
      prepare: `${configuration.client}-${sha256Bytes(runId).slice(0, 12)}-prepare-v1`,
      submit_attachment: `${configuration.client}-${sha256Bytes(runId).slice(0, 12)}-submit-attachment-v1`,
      submit_inputs: `${configuration.client}-${sha256Bytes(runId).slice(0, 12)}-submit-inputs-v1`,
    },
    client_calls: [],
    audited_service_job_ids: [],
    usage: zeroUsage(),
  };
  if (dualLinuxContainers) {
    appendResource(configuration.resourceRegistry, configuration.attemptRoot, "network", state.network, configuration.resourceLabel);
    await docker(configuration.dockerContext, ["network", "create", "--label", configuration.resourceLabel, state.network]);
  }
  appendResource(configuration.resourceRegistry, configuration.attemptRoot, "volume", state.volume, configuration.resourceLabel);
  await docker(configuration.dockerContext, ["volume", "create", "--label", configuration.resourceLabel, state.volume]);
  if (dualLinuxContainers) await createLinuxClientContainer(configuration, state);
  await createContainer(configuration, state, state.initial_container, "fresh", configuration.stage, true);
  await docker(configuration.dockerContext, ["exec", state.active_container, "sh", "-eu", "-c", "test -z \"$(find /var/lib/problem-locator -mindepth 1 -print -quit)\""]);
  const volumeInspect = await docker(configuration.dockerContext, ["volume", "inspect", state.volume], { forward: false });
  const volumeMetadata = JSON.parse(volumeInspect.stdout)[0];
  requireCondition(volumeMetadata.Labels?.["problem-locator.test-flow.run"] === runId, "FRESH_VOLUME_LABEL_MISMATCH");
  let topologyAdmission = null;
  if (dualLinuxContainers) {
    const [clientInspect, serverInspect, networkInspect] = await Promise.all([
      docker(configuration.dockerContext, ["container", "inspect", state.client_container], { forward: false }),
      docker(configuration.dockerContext, ["container", "inspect", state.initial_container], { forward: false }),
      docker(configuration.dockerContext, ["network", "inspect", state.network], { forward: false }),
    ]);
    const clientMetadata = JSON.parse(clientInspect.stdout)[0];
    const serverMetadata = JSON.parse(serverInspect.stdout)[0];
    const networkMetadata = JSON.parse(networkInspect.stdout)[0];
    const clientAddress = clientMetadata.NetworkSettings?.Networks?.[state.network]?.IPAddress;
    const serverAddress = serverMetadata.NetworkSettings?.Networks?.[state.network]?.IPAddress;
    const serverPortBindings = serverMetadata.HostConfig?.PortBindings ?? {};
    const serverPublishedPorts = serverMetadata.NetworkSettings?.Ports ?? {};
    const forbiddenDockerSocket = [...(clientMetadata.Mounts ?? []), ...(serverMetadata.Mounts ?? [])]
      .some((mount) => mount.Source === "/var/run/docker.sock" || mount.Destination === "/var/run/docker.sock");
    const clientHomeBindings = (clientMetadata.Config?.Env ?? []).filter((entry) => entry === `HOME=${LINUX_CLIENT_HOME}`);
    requireCondition(
      networkMetadata.Labels?.["problem-locator.test-flow.run"] === runId
        && typeof clientAddress === "string" && clientAddress.length > 0
        && typeof serverAddress === "string" && serverAddress.length > 0
        && clientAddress !== serverAddress
        && Object.keys(serverPortBindings).length === 0
        && Object.values(serverPublishedPorts).every((binding) => binding === null)
        && clientHomeBindings.length === 1
        && forbiddenDockerSocket === false,
      "DUAL_LINUX_TOPOLOGY_INVALID",
      "BLOCKED",
      "INFRA",
    );
    topologyAdmission = {
      schema_version: 1,
      status: "PASS",
      orchestrator: process.platform,
      client: { platform: "linux/amd64", image_id: state.client_image_id, container: state.client_container, address: clientAddress, runtime: state.selected_client_runtime_observed },
      server: { platform: "linux/amd64", image_id: state.image_id, container: state.initial_container, address: serverAddress, published_ports: [] },
      network: state.network,
      endpoint: state.public_base_url,
      docker_socket_mounted: false,
    };
    writeNew(path.join(stageRoot, "dual-linux-topology.json"), topologyAdmission);
  }
  const browserCapability = dualLinuxContainers
    ? await probeLinuxClientBrowserCapability(configuration, state, stageRoot)
    : null;
  const freshAdmission = {
    schema_version: 1,
    status: "PASS",
    lineage_root: "GENESIS",
    volume: state.volume,
    initial_data_root: "EMPTY",
    docker_context: configuration.dockerContext,
    server_os: "linux",
    server_architecture: "x86_64",
    platform: "linux/amd64",
    topology: configuration.topology,
    topology_receipt: topologyAdmission ? "dual-linux-topology.json" : null,
    browser_capability_receipt: browserCapability ? "linux-client-browser-capability.json" : null,
  };
  writeNew(path.join(stageRoot, "fresh-admission.json"), freshAdmission);
  await initializeContainer(configuration, state, state.initial_container, "fresh", configuration.stage);
  ensureClientRuntime(configuration, state);
  atomicState(configuration.statePath, state);
  await startService(configuration, state, "route", { allowEmptyJourney: true });
  const dfxProbe = await runServerDfxProbe(configuration, state);
  return { state, freshAdmission, dfxProbe, browserCapability };
}

function exportedJobs(exported) {
  return Object.values(exported?.state?.cases ?? {}).flatMap((aggregate) => Object.values(aggregate.jobs ?? {}));
}

function jobCounts(exported) {
  const jobs = exportedJobs(exported);
  return {
    running: jobs.filter((job) => job.status === "RUNNING").length,
    queued: jobs.filter((job) => job.status === "PENDING").length,
  };
}

async function createCheckpointSource(configuration, state, continuation) {
  const checkpointPath = configuration.checkpointOutputSource;
  requireCondition(checkpointPath && path.isAbsolute(checkpointPath) && path.resolve(checkpointPath).startsWith(`${path.resolve(configuration.attemptRoot)}${path.sep}`), "CHECKPOINT_OUTPUT_INVALID");
  requireCondition(!state.current_instance, "CHECKPOINT_SERVICE_RUNNING");
  const stageRoot = path.dirname(checkpointPath);
  ensureDirectory(stageRoot);
  const validation = await docker(configuration.dockerContext, [
    "exec", state.active_container,
    "runuser", "-u", "plagent", "--",
    "/usr/bin/env", "-i",
    "HOME=/run/plagent-claude", "LANG=C.UTF-8", "PATH=/opt/venvs/xiaodao/bin:/usr/local/bin:/usr/bin:/bin", "PYTHONNOUSERSITE=1",
    "/opt/venvs/xiaodao/bin/python", "-I", "-m", "problem_locator", "validate-state", "--data-root", "/var/lib/problem-locator",
  ], { forward: false });
  const validationReport = JSON.parse(validation.stdout);
  requireCondition(validationReport.valid === true && Array.isArray(validationReport.errors) && validationReport.errors.length === 0, "CHECKPOINT_STATE_INVALID", "FAIL", "PRODUCT");
  writeNew(path.join(stageRoot, "state-validation.json"), validationReport);
  const exportContainerPath = `/tmp/test-flow-state-export-${configuration.stage.replaceAll(".", "-")}.json`;
  await docker(configuration.dockerContext, [
    "exec", state.active_container,
    "runuser", "-u", "plagent", "--",
    "/usr/bin/env", "-i",
    "HOME=/run/plagent-claude", "LANG=C.UTF-8", "PATH=/opt/venvs/xiaodao/bin:/usr/local/bin:/usr/bin:/bin", "PYTHONNOUSERSITE=1",
    "/opt/venvs/xiaodao/bin/python", "-I", "-m", "problem_locator", "export-state", "--data-root", "/var/lib/problem-locator", "--output", exportContainerPath,
  ], { forward: false });
  const exportHostPath = path.join(stageRoot, "state-export.json");
  await docker(configuration.dockerContext, ["cp", `${state.active_container}:${exportContainerPath}`, exportHostPath], { forward: false });
  await docker(configuration.dockerContext, ["exec", state.active_container, "rm", "-f", exportContainerPath], { forward: false });
  const exported = readJson(exportHostPath);
  const counts = jobCounts(exported);
  const workspaceProbe = await docker(configuration.dockerContext, [
    "exec", state.active_container,
    "find", "/var/lib/problem-locator/tmp/workspaces", "-mindepth", "1", "-maxdepth", "1", "-printf", "%y %f\\n",
  ], { forward: false });
  const workspaceEntries = workspaceProbe.stdout.split(/\r?\n/).filter(Boolean)
    .map((line) => ({ kind: line.slice(0, 1), job_id: line.slice(2) }));
  const workspaceIds = workspaceEntries.map((entry) => entry.job_id).sort();
  const terminalJobs = new Set(exportedJobs(exported)
    .filter((job) => ["SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"].includes(job.status))
    .map((job) => job.job_id));
  const retainedWorkspacesAreTerminal = workspaceEntries.every((entry) => entry.kind === "d" && terminalJobs.has(entry.job_id));
  const processes = await docker(configuration.dockerContext, ["exec", state.active_container, "ps", "-ww", "-eo", "args"], { forward: false });
  const activeWorkers = processes.stdout.split(/\r?\n/).filter((line) => /test_service_launcher\.py|\/usr\/local\/bin\/claude|service-supervisor/.test(line)).length;
  const receipt = {
    schema_version: 1,
    status: counts.running === 0 && counts.queued === 0 && activeWorkers === 0 && retainedWorkspacesAreTerminal ? "PASS" : "FAIL",
    service_stopped: true,
    running_jobs: counts.running,
    queued_jobs: counts.queued,
    active_workers: activeWorkers,
    temporary_workspaces: 0,
    excluded_terminal_workspaces: workspaceIds.length,
    state_validation: "PASS",
  };
  requireCondition(receipt.status === "PASS", "CHECKPOINT_NOT_QUIESCENT", "ERROR", "HARNESS");
  const scratchRoot = path.join(configuration.attemptRoot, "scratch", "checkpoint-sources");
  ensureDirectory(scratchRoot);
  const stateRoot = path.join(scratchRoot, configuration.stage);
  const archiveHostPath = path.join(scratchRoot, `${configuration.stage}.stable-state.tar`);
  const archiveContainerPath = `/tmp/test-flow-stable-state-${configuration.stage.replaceAll(".", "-")}.tar`;
  const classificationContainerPath = `/tmp/test-flow-stable-state-${configuration.stage.replaceAll(".", "-")}-classification.json`;
  const classificationHostPath = path.join(stageRoot, "checkpoint-temporary-classification.json");
  requireCondition(!fs.existsSync(stateRoot) && !fs.existsSync(archiveHostPath), "CHECKPOINT_STAGING_EXISTS");
  const exportResult = await run("docker", dockerArgs(configuration.dockerContext, [
    "exec", state.active_container, "sh", "/test-flow-runtime/export-checkpoint.sh", archiveContainerPath, classificationContainerPath,
  ]), { forward: false });
  const classificationCopy = await run("docker", dockerArgs(configuration.dockerContext, [
    "cp", `${state.active_container}:${classificationContainerPath}`, classificationHostPath,
  ]), { forward: false });
  const classification = classificationCopy.status === 0 ? readJson(classificationHostPath) : null;
  if (exportResult.status !== 0) {
    const code = classification?.status === "FAIL" && /^CHECKPOINT_[A-Z0-9_]+$/.test(classification.code)
      ? classification.code
      : "CHECKPOINT_STABLE_EXPORT_FAILED";
    throw new StageError(code, "ERROR", "HARNESS");
  }
  requireCondition(classification?.schema_version === 1 && classification.status === "PASS" && classification.code === null && classification.outbox_clear === true, "CHECKPOINT_TEMPORARY_CLASSIFICATION_RECEIPT_INVALID");
  try {
    await docker(configuration.dockerContext, ["cp", `${state.active_container}:${archiveContainerPath}`, archiveHostPath], { forward: false });
    const extraction = extractCheckpointSourceArchive({ archivePath: archiveHostPath, targetRoot: stateRoot });
    const workspaces = path.join(stateRoot, "tmp", "workspaces");
    requireCondition(fs.existsSync(workspaces) && fs.readdirSync(workspaces).length === 0 && !fs.existsSync(path.join(stateRoot, ".instance.lock")), "CHECKPOINT_STABLE_LAYOUT_INVALID");
    writeNew(path.join(stageRoot, "checkpoint-stable-export.json"), {
      schema_version: 1,
      status: extraction.status,
      entry_count: extraction.entry_count,
      portable_digest: extraction.portable_digest,
      excluded_instance_lock: true,
      excluded_terminal_workspaces: workspaceIds.length,
      excluded_completed_uploads: classification.excluded_completed_uploads,
      excluded_processed_proposal_stages: classification.excluded_processed_proposal_stages,
      outbox_clear: true,
      temporary_layout_empty: true,
    });
  } finally {
    try { await docker(configuration.dockerContext, ["exec", state.active_container, "rm", "-f", archiveContainerPath, classificationContainerPath], { forward: false }); } catch {}
    try { fs.rmSync(archiveHostPath, { force: true }); } catch {}
  }
  const adapterContinuation = {
    adapter_state_schema_version: 5,
    adapter_case_input_digest: state.release_case?.input_digest ?? null,
    adapter_case_scenario_id: state.release_case?.scenario_id ?? null,
    adapter_case_skill_id: state.release_case?.skill_id ?? null,
    adapter_case_id: state.case_id ?? null,
    adapter_attachment_id: state.attachment_id ?? null,
    adapter_prepared_case_revision: state.prepared_case_revision ?? null,
    adapter_prepare_expected_case_revision: state.prepare_expected_case_revision ?? null,
    adapter_case_revision: state.case_revision ?? null,
    adapter_status: state.status ?? null,
    adapter_resolved_case_revision: state.resolved_case_revision ?? null,
    adapter_observed_statuses: state.observed_statuses ?? [],
    adapter_audited_service_job_ids: state.audited_service_job_ids ?? [],
    adapter_request_create: state.request_ids?.create ?? null,
    adapter_request_prepare: state.request_ids?.prepare ?? null,
    adapter_request_submit_attachment: state.request_ids?.submit_attachment ?? null,
    adapter_request_submit_inputs: state.request_ids?.submit_inputs ?? null,
    adapter_upload_method: state.upload_descriptor?.method ?? null,
    adapter_upload_max_bytes: state.upload_descriptor?.max_bytes ?? null,
    adapter_upload_expires_at: state.upload_descriptor?.expires_at ?? null,
    adapter_upload_content_length: state.upload_descriptor?.required_headers?.["Content-Length"] ?? null,
    adapter_upload_content_type: state.upload_descriptor?.required_headers?.["Content-Type"] ?? null,
    adapter_upload_idempotency_key: state.upload_descriptor?.required_headers?.["Idempotency-Key"] ?? null,
    adapter_upload_sha256: state.upload_descriptor?.required_headers?.["X-Content-SHA256"] ?? null,
    adapter_methods_result_json: state.methods_result ? canonicalJson(state.methods_result) : null,
    adapter_methods_v2_json: state.methods_v2 ? canonicalJson(state.methods_v2) : null,
  };
  requireCondition(Object.values(adapterContinuation).every((value) => value === null || ["string", "number", "boolean"].includes(typeof value) || (Array.isArray(value) && value.every((entry) => entry === null || ["string", "number", "boolean"].includes(typeof entry)))), "CHECKPOINT_CONTINUATION_NOT_FLAT");
  writeNew(checkpointPath, {
    schema_version: 1,
    state_root: stateRoot,
    continuation: { ...continuation, ...adapterContinuation },
    quiescence_receipt: receipt,
  });
  return receipt;
}

function addUsage(state, usage) {
  state.usage = sumUsage([state.usage, usage]);
}

export function validSuccessfulInvocationReceipt(invocation) {
  return invocation?.schema_version === 3
    && invocation.usage_complete === true
    && isCompleteUsage(invocation.usage)
    && invocation.terminal?.subtype === "success"
    && invocation.terminal?.is_error === false
    && invocation.wrapper_outcome?.schema_version === 1
    && invocation.wrapper_outcome?.status === "PASS"
    && invocation.wrapper_outcome?.code === null;
}

function validServiceAgentInvocationReceipt(invocation) {
  return validSuccessfulInvocationReceipt(invocation)
    && typeof invocation.job_id === "string"
    && UUID.test(invocation.job_id);
}

function validMethodsPreflightReceipt(receipt) {
  return receipt?.schema_version === 2
    && canonicalJson(Object.keys(receipt).sort()) === canonicalJson([
      "schema_version", "kind", "job_id", "job_type", "result_type",
      "registration_id", "decision_audit_absent", "model_invoked", "log_pair",
      "job_sha256", "job_outcome_sha256", "methods_preflight_sha256",
    ].sort())
    && receipt.kind === "methods-server-preflight"
    && typeof receipt.job_id === "string"
    && receipt.job_type === "DIAGNOSE"
    && ["NEED_INPUT", "NEED_ATTACHMENT"].includes(receipt.result_type)
    && typeof receipt.registration_id === "string"
    && receipt.decision_audit_absent === true
    && receipt.model_invoked === false
    && receipt.log_pair === "ABSENT"
    && [
      receipt.job_sha256,
      receipt.job_outcome_sha256,
      receipt.methods_preflight_sha256,
    ].every((digest) => /^[a-f0-9]{64}$/.test(digest));
}

export function validServiceAgentUsageReceipt(receipt) {
  if (
    canonicalJson(Object.keys(receipt ?? {}).sort()) !== canonicalJson([
      "schema_version", "status", "usage_complete", "token_formula",
      "invocations", "no_model_jobs", "new_job_ids",
    ].sort())
    || receipt.schema_version !== 3
    || receipt.status !== "PASS"
    || receipt.usage_complete !== true
    || receipt.token_formula !== TOKEN_USAGE_FORMULA
    || !Array.isArray(receipt.invocations)
    || !receipt.invocations.every(validServiceAgentInvocationReceipt)
    || !Array.isArray(receipt.no_model_jobs)
    || !receipt.no_model_jobs.every(validMethodsPreflightReceipt)
    || !Array.isArray(receipt.new_job_ids)
    || !receipt.new_job_ids.every((jobId) => typeof jobId === "string" && UUID.test(jobId))
  ) return false;
  const invocationJobIds = receipt.invocations.map((invocation) => invocation.job_id);
  const noModelJobIds = receipt.no_model_jobs.map((preflight) => preflight.job_id);
  if (new Set(noModelJobIds).size !== noModelJobIds.length) return false;
  if (invocationJobIds.some((jobId) => noModelJobIds.includes(jobId))) return false;
  const expectedJobIds = [...new Set([...invocationJobIds, ...noModelJobIds])].sort();
  return canonicalJson(receipt.new_job_ids) === canonicalJson(expectedJobIds);
}

export function validRouteMethodsPreflightEvidence(
  receipts,
  { registrationId, expectedJobId },
) {
  if (!Array.isArray(receipts) || receipts.length !== 1) return false;
  const [receipt] = receipts;
  return validMethodsPreflightReceipt(receipt)
    && receipt.result_type === "NEED_ATTACHMENT"
    && receipt.registration_id === registrationId
    && receipt.job_id === expectedJobId;
}

function clientInvocation(configuration, phase, audit, caps) {
  const containerClient = configuration.topology === DUAL_LINUX_TOPOLOGY;
  return {
    schema_version: 3,
    invocation_id: `${containerClient ? "linux-client-container" : "host-client"}:${phase}`,
    class: containerClient ? "linux-client-container" : "host-client",
    execution_topology: containerClient ? "darwin-orchestrated-linux-container" : "native-host-client",
    effective_model: audit.init.effective_model,
    effective_caps: caps,
    usage_complete: true,
    usage: audit.usage,
    terminal: audit.terminal,
    turns: audit.turns,
    wrapper_outcome: { schema_version: 1, status: "PASS", code: null },
    hard_cap_enforcement: {
      turns: "claude-cli",
      cost_usd: "claude-cli",
      hard_timeout_seconds: containerClient ? "container-cli-timeout-plus-orchestrator-watchdog" : "host-process-watchdog",
      total_tokens: `terminal-usage-postcondition:${TOKEN_USAGE_FORMULA}`,
    },
  };
}

async function auditServiceAgentUsage(configuration, state, instance) {
  const output = `/evidence/stages/${configuration.stage}/service-agent-usage-${instance}.json`;
  const args = [
    "exec", state.active_container,
    "/opt/venvs/xiaodao/bin/python", "-I", "/test-flow-runtime/audit_service_agent_usage.py",
    "--jobs-root", "/var/lib/problem-locator/jobs",
    "--output", output,
    "--model", RELEASE_MODEL,
    "--max-turns", String(configuration.serviceAgentCaps.max_turns),
    "--max-total-tokens", String(configuration.serviceAgentCaps.max_total_tokens),
    "--max-budget-usd", String(configuration.serviceAgentCaps.max_budget_usd),
    "--hard-timeout-seconds", String(configuration.serviceAgentCaps.hard_timeout_seconds),
  ];
  for (const jobId of state.audited_service_job_ids ?? []) args.push("--exclude-job-id", jobId);
  await docker(configuration.dockerContext, args);
  const receipt = readJson(path.join(configuration.stageRoot, `service-agent-usage-${instance}.json`));
  requireCondition(
    validServiceAgentUsageReceipt(receipt),
    "SERVICE_AGENT_USAGE_RECEIPT_INVALID",
  );
  state.audited_service_job_ids = [...new Set([...(state.audited_service_job_ids ?? []), ...receipt.new_job_ids])].sort();
  atomicState(configuration.statePath, state);
  return { invocations: receipt.invocations, noModelJobs: receipt.no_model_jobs };
}

const METHODS_V2_EXECUTION_SOURCES = Object.freeze({
  source_job: ["SOURCE", "job.json"],
  reviewer_job: ["REVIEWER", "job.json"],
  evidence_graph: ["SOURCE", "methods-evidence-graph-v2.json"],
  evaluation_plan: ["SOURCE", "methods-evaluation-plan-v2.json"],
  limitations: ["SOURCE", "methods-limitations-v2.json"],
  source_state: ["SOURCE", "methods-state-v2.json"],
  source_outcome: ["SOURCE", "job_outcome.json"],
  terminal_state: ["REVIEWER", "methods-state-v2.json"],
  reviewer_outcome: ["REVIEWER", "job_outcome.json"],
});

async function captureMethodsV2Files(configuration, state, { sourceJobId, reviewerJobId, prefix = "" }) {
  const captured = {};
  for (const [key, [owner, sourceName]] of Object.entries(METHODS_V2_EXECUTION_SOURCES)) {
    const jobId = owner === "SOURCE" ? sourceJobId : reviewerJobId;
    const destinationName = `${prefix}${METHODS_V2_CAPTURED_FILES[key]}`;
    const destination = path.join(configuration.stageRoot, destinationName);
    requireCondition(!fs.existsSync(destination), "METHODS_V2_ORACLE_CAPTURE_ALREADY_EXISTS");
    const copied = await run("docker", dockerArgs(configuration.dockerContext, [
      "cp",
      `${state.active_container}:/var/lib/problem-locator/jobs/${jobId}/${sourceName}`,
      destination,
    ]), { forward: false });
    requireCondition(copied.status === 0, "METHODS_V2_EXECUTION_RECORD_MISSING", "FAIL", "CONTRACT");
    const metadata = fs.lstatSync(destination);
    requireCondition(metadata.isFile() && !metadata.isSymbolicLink() && metadata.nlink === 1 && metadata.size > 0, "METHODS_V2_EXECUTION_RECORD_INVALID", "FAIL", "CONTRACT");
    captured[key] = fs.readFileSync(destination);
  }
  return captured;
}

function methodsV2Expected(configuration, state, sourceJobId, reviewerJobId) {
  return {
    source_job_id: sourceJobId,
    reviewer_job_id: reviewerJobId,
    case_id: state.case_id,
    skill_ref: {
      id: configuration.releaseCase.skill.runtime_ref_id,
      version: configuration.releaseCase.skill.version,
      content_hash: configuration.releaseCase.skill.content_hash,
    },
    source_ids: [...configuration.releaseCase.driver.attachment_anchor_names].sort(),
    method_cards: configuration.releaseCase.result_expectation.method_cards,
    loaded_method_ids: configuration.releaseCase.result_expectation.loaded_method_ids,
    confirmed_method_ids: configuration.releaseCase.result_expectation.confirmed_method_ids,
    required_evidence_identities: configuration.releaseCase.result_expectation.required_evidence_identities,
  };
}

async function captureMethodsV2Oracle(configuration, state, serviceInvocations) {
  const sourceJobIds = [...new Set(serviceInvocations
    .filter((invocation) => invocation.job_type === "DIAGNOSE")
    .map((invocation) => invocation.job_id))];
  const reviewerJobIds = [...new Set(serviceInvocations
    .filter((invocation) => invocation.job_type === "REVIEW")
    .map((invocation) => invocation.job_id))];
  requireCondition(
    sourceJobIds.length === 1 && reviewerJobIds.length === 1
      && UUID.test(sourceJobIds[0] ?? "") && UUID.test(reviewerJobIds[0] ?? ""),
    "METHODS_V2_ROLE_JOB_IDENTITY_INVALID",
    "FAIL",
    "CONTRACT",
  );
  const sourceJobId = sourceJobIds[0];
  const reviewerJobId = reviewerJobIds[0];
  const files = await captureMethodsV2Files(configuration, state, { sourceJobId, reviewerJobId });
  let summary;
  try {
    summary = validateMethodsV2ExecutionRecords({
      files,
      expected: methodsV2Expected(configuration, state, sourceJobId, reviewerJobId),
      invocations: serviceInvocations,
      publicMethodsResult: state.methods_result,
    });
  } catch (error) {
    throw new StageError(error?.code ?? "METHODS_V2_ORACLE_VALIDATION_FAILED", "FAIL", "CONTRACT");
  }
  writeNew(path.join(configuration.stageRoot, "methods-v2-oracle.json"), summary);
  return summary;
}

async function verifyRestartMethodsV2(configuration, state, restartView) {
  const restartedFiles = await captureMethodsV2Files(configuration, state, {
    sourceJobId: state.methods_v2.source_job_id,
    reviewerJobId: state.methods_v2.reviewer_job_id,
    prefix: "restart-",
  });
  try {
    validateMethodsV2RestartSnapshot({
      caseView: restartView.case_view,
      artifacts: restartView.artifacts,
      methodsSummary: state.methods_v2,
      restartedFiles,
    });
  } catch (error) {
    throw new StageError(error?.code ?? "METHODS_V2_RESTART_VALIDATION_FAILED", "FAIL", "CONTRACT");
  }
}

async function verifyRuntimeResources(configuration, state) {
  const dualLinuxContainers = configuration.topology === DUAL_LINUX_TOPOLOGY;
  const runId = path.basename(configuration.attemptRoot);
  requireCondition(
    state.active_container
      && state.image_id === configuration.expectedServerImageId
      && state.runtime_images?.server_image_id === configuration.expectedServerImageId
      && state.runtime_images?.client_image_id === (dualLinuxContainers ? configuration.expectedClientImageId : null),
    "SERVER_RUNTIME_STATE_INVALID",
    "BLOCKED",
    "INFRA",
  );
  if (dualLinuxContainers) {
    requireCondition(state.client_container && state.network, "DUAL_LINUX_RUNTIME_STATE_INVALID", "BLOCKED", "INFRA");
  }
  const inspections = await Promise.all([
    docker(configuration.dockerContext, ["container", "inspect", state.active_container], { forward: false }),
    docker(configuration.dockerContext, ["image", "inspect", configuration.expectedServerImageId], { forward: false }),
    ...(dualLinuxContainers ? [
      docker(configuration.dockerContext, ["container", "inspect", state.client_container], { forward: false }),
      docker(configuration.dockerContext, ["image", "inspect", configuration.expectedClientImageId], { forward: false }),
      docker(configuration.dockerContext, ["network", "inspect", state.network], { forward: false }),
    ] : []),
  ]);
  const server = JSON.parse(inspections[0].stdout)[0];
  const serverImage = JSON.parse(inspections[1].stdout)[0];
  requireCondition(
    validServerRuntimeInspection({
      topology: configuration.topology,
      stageId: configuration.stage,
      state,
      expectedServerImageId: configuration.expectedServerImageId,
      expectedRunId: runId,
      server,
      serverImage,
    }),
    "SERVER_RUNTIME_RESOURCE_DRIFT",
    "BLOCKED",
    "INFRA",
  );

  if (!dualLinuxContainers) {
    return {
      client_container: null,
      server_container: state.active_container,
      client_image_id: null,
      server_image_id: server.Image,
      network: null,
      selected_client_runtime: null,
    };
  }

  const client = JSON.parse(inspections[2].stdout)[0];
  const clientImage = JSON.parse(inspections[3].stdout)[0];
  const network = JSON.parse(inspections[4].stdout)[0];
  const observedClientRuntime = await probeLinuxClientRuntime(configuration, state);
  const clientAddress = client.NetworkSettings?.Networks?.[state.network]?.IPAddress;
  const serverAddress = server.NetworkSettings?.Networks?.[state.network]?.IPAddress;
  const clientHomeBindings = (client.Config?.Env ?? []).filter((entry) => entry === `HOME=${LINUX_CLIENT_HOME}`);
  requireCondition(
    state.client_image_id === configuration.expectedClientImageId
      && client.Name === `/${state.client_container}`
      && client.Image === configuration.expectedClientImageId
      && client.Config?.Image === configuration.expectedClientImageId
      && client.Config?.User === `${observedClientRuntime.user.uid}:${observedClientRuntime.user.gid}`
      && client.Config?.Labels?.["problem-locator.test-flow.run"] === runId
      && client.State?.Running === true
      && clientImage?.Id === configuration.expectedClientImageId
      && clientImage?.Os === "linux"
      && clientImage?.Architecture === "amd64"
      && network.Name === state.network
      && network.Labels?.["problem-locator.test-flow.run"] === runId
      && typeof clientAddress === "string" && clientAddress.length > 0
      && typeof serverAddress === "string" && serverAddress.length > 0
      && clientAddress !== serverAddress
      && client.HostConfig?.ReadonlyRootfs === true
      && clientHomeBindings.length === 1
      && state.selected_client_runtime_observed !== null
      && canonicalJson(observedClientRuntime) === canonicalJson(state.selected_client_runtime_observed)
      && dockerSocketMounted(client.Mounts) === false,
    "DUAL_LINUX_RUNTIME_RESOURCE_DRIFT",
    "BLOCKED",
    "INFRA",
  );
  return {
    client_container: state.client_container,
    server_container: state.active_container,
    client_image_id: client.Image,
    server_image_id: server.Image,
    network: state.network,
    selected_client_runtime: observedClientRuntime,
  };
}

async function stageReceipt(configuration, value) {
  const state = fs.existsSync(configuration.statePath) ? readJson(configuration.statePath) : null;
  const runtimeResources = state ? await verifyRuntimeResources(configuration, state) : null;
  const receiptPath = path.join(configuration.stageRoot, "adapter-result.json");
  const invocations = value.invocations ?? [];
  requireCondition(
    Array.isArray(invocations) && invocations.every(validSuccessfulInvocationReceipt),
    "MODEL_INVOCATION_RECEIPT_INVALID",
  );
  const usage = value.usage ? normalizeUsage(value.usage) : sumUsage(invocations.map((invocation) => invocation.usage));
  const usageComplete = isCompleteUsage(usage);
  writeNew(receiptPath, {
    ...value,
    schema_version: 3,
    stage_id: configuration.stage,
    gate_id: configuration.gateId,
    runtime_profile_digest: configuration.runtimeProfileDigest,
    topology: configuration.topology,
    runtime_images: {
      server_image_id: configuration.expectedServerImageId,
      client_image_id: configuration.topology === DUAL_LINUX_TOPOLOGY ? configuration.expectedClientImageId : null,
    },
    runtime_resources: runtimeResources,
    generated_skill: {
      registration_id: configuration.generatedSkill.registration_id,
      skill_name: configuration.generatedSkill.skill_name,
      tree_digest: configuration.generatedSkill.tree_digest,
      package_digest: configuration.generatedSkill.package_digest,
      registration_sha256: configuration.generatedSkill.registration_sha256,
      package_tree_sha256: configuration.generatedSkill.package_tree_sha256,
      combined_sha256: configuration.generatedSkill.combined_sha256,
      content_tree_sha256: configuration.generatedSkill.content_tree_sha256,
      generation_receipt_sha256: configuration.generatedSkill.generation_receipt_sha256,
      source_wiki_sha256: configuration.generatedSkill.source_wiki_sha256,
    },
    effective_caps: null,
    usage_complete: usageComplete,
    invocations,
    usage,
  });
}

async function applyRestoredCheckpoint(configuration, state) {
  requireCondition(configuration.track === "dev", "CHECKPOINT_RESTORE_RELEASE_FORBIDDEN", "BLOCKED", "INFRA");
  requireCondition(configuration.restoredDataRoot && configuration.restoredContinuation && configuration.restoredCheckpointId, "CHECKPOINT_RESTORE_INPUT_MISSING");
  const continuation = readJson(configuration.restoredContinuation);
  requireCondition(
    continuation?.schema_version === 1
      && continuation.release_eligible === false
      && continuation.next_stage === configuration.stage
      && continuation.adapter_state_schema_version === 5
      && continuation.adapter_case_input_digest === configuration.releaseCase.input_digest
      && continuation.adapter_case_scenario_id === configuration.releaseCase.scenario_id
      && continuation.adapter_case_skill_id === configuration.releaseCase.skill.id
      && (continuation.adapter_methods_result_json === null || typeof continuation.adapter_methods_result_json === "string")
      && (continuation.adapter_methods_v2_json === null || typeof continuation.adapter_methods_v2_json === "string"),
    "CHECKPOINT_CONTINUATION_INVALID",
  );
  if (state.current_instance) {
    const discarded = await stopService(configuration, state, { indexLabel: "restore-discarded-route" });
    requireCondition(discarded.service_invocations.length === 0, "CHECKPOINT_RESTORE_FRESH_ENVIRONMENT_MODEL_ACTIVITY", "FAIL", "CONTRACT");
    requireCondition(discarded.service_no_model_jobs.length === 0, "CHECKPOINT_RESTORE_FRESH_ENVIRONMENT_PREFLIGHT_ACTIVITY", "FAIL", "CONTRACT");
  }
  await docker(configuration.dockerContext, [
    "exec", state.active_container,
    "sh", "-eu", "-c",
    'test "$1" = /var/lib/problem-locator; find "$1" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +',
    "test-flow-checkpoint-restore", "/var/lib/problem-locator",
  ]);
  await docker(configuration.dockerContext, ["cp", `${configuration.restoredDataRoot}/.`, `${state.active_container}:/var/lib/problem-locator`]);
  await docker(configuration.dockerContext, ["exec", state.active_container, "chown", "-R", "10001:10001", "/var/lib/problem-locator"]);
  const validation = await docker(configuration.dockerContext, [
    "exec", state.active_container,
    "runuser", "-u", "plagent", "--",
    "/usr/bin/env", "-i",
    "HOME=/run/plagent-claude", "LANG=C.UTF-8", "PATH=/opt/venvs/xiaodao/bin:/usr/local/bin:/usr/bin:/bin", "PYTHONNOUSERSITE=1",
    "/opt/venvs/xiaodao/bin/python", "-I", "-m", "problem_locator", "validate-state", "--data-root", "/var/lib/problem-locator",
  ], { forward: false });
  const validationReport = JSON.parse(validation.stdout);
  requireCondition(validationReport.valid === true && Array.isArray(validationReport.errors) && validationReport.errors.length === 0, "CHECKPOINT_RESTORED_STATE_INVALID", "FAIL", "PRODUCT");

  const caseId = continuation.adapter_case_id;
  Object.assign(state, {
    case_id: caseId,
    attachment_id: continuation.adapter_attachment_id,
    prepared_case_revision: continuation.adapter_prepared_case_revision,
    prepare_expected_case_revision: continuation.adapter_prepare_expected_case_revision,
    case_revision: continuation.adapter_case_revision,
    status: continuation.adapter_status,
    resolved_case_revision: continuation.adapter_resolved_case_revision,
    observed_statuses: continuation.adapter_observed_statuses,
    audited_service_job_ids: continuation.adapter_audited_service_job_ids,
    request_ids: {
      create: continuation.adapter_request_create,
      prepare: continuation.adapter_request_prepare,
      submit_attachment: continuation.adapter_request_submit_attachment,
      submit_inputs: continuation.adapter_request_submit_inputs,
    },
    client_calls: [],
    usage: zeroUsage(),
  });
  if (continuation.adapter_upload_method) {
    state.upload_descriptor = {
      attachment_id: state.attachment_id,
      method: continuation.adapter_upload_method,
      url: `${state.public_base_url}/api/v1/attachments/${state.attachment_id}/content`,
      required_headers: {
        "Content-Length": continuation.adapter_upload_content_length,
        "Content-Type": continuation.adapter_upload_content_type,
        "Idempotency-Key": continuation.adapter_upload_idempotency_key,
        "X-Content-SHA256": continuation.adapter_upload_sha256,
      },
      max_bytes: continuation.adapter_upload_max_bytes,
      expires_at: continuation.adapter_upload_expires_at,
    };
  }
  state.methods_result = continuation.adapter_methods_result_json === null
    ? null
    : JSON.parse(continuation.adapter_methods_result_json);
  state.methods_v2 = continuation.adapter_methods_v2_json === null
    ? null
    : JSON.parse(continuation.adapter_methods_v2_json);
  atomicState(configuration.statePath, state);
  if (configuration.stage === "journey.cross-job.upload") await startService(configuration, state, "upload");
  else if (configuration.stage === "journey.cross-job.diagnose") await startService(configuration, state, "diagnose");
  else requireCondition(configuration.stage === "journey.cross-job.publish-restart", "CHECKPOINT_RESTORE_STAGE_UNSUPPORTED");
  writeNew(path.join(configuration.stageRoot, "adapter-restore-applied.json"), {
    schema_version: 2,
    status: "PASS",
    checkpoint_id: configuration.restoredCheckpointId,
    next_stage: configuration.stage,
    restored_data_root: "VERIFIED",
  });
}

async function execute(configuration) {
  requireCondition(process.platform === configuration.expectedHostPlatform && configuration.client === configuration.expectedClient, "CROSS_JOB_ADAPTER_HOST_REQUIRED", "BLOCKED", "INFRA");
  requireCondition(["host-client", DUAL_LINUX_TOPOLOGY].includes(configuration.topology), "CROSS_JOB_TOPOLOGY_INVALID", "BLOCKED", "INFRA");
  if (configuration.topology === DUAL_LINUX_TOPOLOGY) {
    requireCondition(process.platform === "darwin" && configuration.client === "linux" && configuration.dockerContext === "colima", "DUAL_LINUX_TOPOLOGY_HOST_INVALID", "BLOCKED", "INFRA");
  }
  requireCondition(["release", "dev"].includes(configuration.track), "CROSS_JOB_ADAPTER_TRACK_UNSUPPORTED", "BLOCKED", "INFRA");
  if (configuration.expectedDockerContext !== "default") requireCondition(configuration.dockerContext === configuration.expectedDockerContext, "CROSS_JOB_ADAPTER_DOCKER_CONTEXT", "BLOCKED", "INFRA");
  requireCondition(configuration.sourceSnapshotDigest && configuration.sourceSnapshotManifest && configuration.logparseSource && configuration.mcpSource && configuration.claudeEntry && configuration.claudeSettings, "CROSS_JOB_ADAPTER_INPUT_MISSING", "BLOCKED", "INFRA");
  if (configuration.track === "release") requireCondition(!configuration.restoredDataRoot && !configuration.restoredCheckpointId, "RELEASE_CHECKPOINT_RESTORE_FORBIDDEN", "BLOCKED", "INFRA");
  await verifyRepositoryIdentity(configuration);
  const runtimeIdentity = currentReleaseRuntimeIdentity(configuration);
  try {
    configuration.containerClaudeSettings = materializeAttemptClaudeSettings(
      configuration.claudeSettings,
      configuration.attemptRoot,
      runtimeIdentity.settings.fingerprint,
    ).path;
  } catch (error) {
    throw new StageError(`CLAUDE_SETTINGS_STAGING_${String(error?.message ?? error)}`, "BLOCKED", "INFRA");
  }

  if (configuration.stage === "journey.cross-job.environment") {
    const { state, freshAdmission, dfxProbe, browserCapability } = await createFreshEnvironment(configuration, configuration.stageRoot, runtimeIdentity);
    const terminalCorrespondence = configuration.terminalAfterStage ? await stopService(configuration, state) : null;
    const invocations = terminalCorrespondence?.service_invocations ?? [];
    requireCondition(invocations.length === 0, "ENVIRONMENT_UNEXPECTED_MODEL_INVOCATION", "FAIL", "CONTRACT");
    requireCondition((terminalCorrespondence?.service_no_model_jobs ?? []).length === 0, "ENVIRONMENT_UNEXPECTED_PREFLIGHT_ACTIVITY", "FAIL", "CONTRACT");
    await stageReceipt(configuration, { status: "PASS", fresh_admission: freshAdmission, server_dfx_probe: dfxProbe, browser_capability: browserCapability, client_tool_calls: 0, server_tool_calls: 1, terminal_correspondence: terminalCorrespondence, checkpoint_ready: false, invocations });
    return;
  }

  requireCondition(fs.existsSync(configuration.statePath), "ADAPTER_STATE_MISSING");
  const state = readJson(configuration.statePath);
  requireCondition(state.schema_version === 1 && state.run_id === path.basename(configuration.attemptRoot), "ADAPTER_STATE_INVALID");
  requireCondition(state.topology === configuration.topology, "ADAPTER_TOPOLOGY_DRIFT", "BLOCKED", "INFRA");
  requireCondition(
    state.image_id === configuration.expectedServerImageId
      && state.runtime_images?.server_image_id === configuration.expectedServerImageId
      && state.runtime_images?.client_image_id === (configuration.topology === DUAL_LINUX_TOPOLOGY ? configuration.expectedClientImageId : null),
    "ADAPTER_IMAGE_IDENTITY_DRIFT",
    "BLOCKED",
    "INFRA",
  );
  requireCondition(
    state.generated_skill?.tree_digest === configuration.generatedSkill.tree_digest
      && state.generated_skill?.registration_id === configuration.generatedSkill.registration_id
      && state.generated_skill?.registration_sha256 === configuration.generatedSkill.registration_sha256
      && state.generated_skill?.package_tree_sha256 === configuration.generatedSkill.package_tree_sha256
      && state.generated_skill?.combined_sha256 === configuration.generatedSkill.combined_sha256
      && state.generated_skill?.content_tree_sha256 === configuration.generatedSkill.content_tree_sha256
      && state.generated_skill?.generation_receipt_sha256 === configuration.generatedSkill.generation_receipt_sha256
      && state.generated_skill?.installed_content_tree_sha256 === configuration.generatedSkill.content_tree_sha256,
    "GENERATED_SKILL_IDENTITY_DRIFT",
    "BLOCKED",
    "INFRA",
  );
  requireCondition(canonicalJson(state.runtime_identity) === canonicalJson(runtimeIdentity), "RELEASE_RUNTIME_IDENTITY_DRIFT", "BLOCKED", "INFRA");
  await verifyRuntimeResources(configuration, state);
  if (configuration.restoredDataRoot) await applyRestoredCheckpoint(configuration, state);

  if (configuration.stage === "journey.cross-job.route") {
    requireCondition(configuration.hardCaps !== null, "ROUTE_HARD_CAPS_MISSING", "BLOCKED", "INFRA");
    const phaseOneAudit = await runClaude(configuration, state, configuration.stageRoot, "phase1", phaseOnePrompt(), configuration.hardCaps.max_turns, configuration.hardCaps.max_budget_usd);
    const phaseOneSummary = validatePhaseOne(phaseOneAudit, configuration.releaseCase, state.request_ids);
    Object.assign(state, phaseOneSummary);
    state.client_calls.push(...phaseOneAudit.records.map((record, index) => ({ phase: "phase1", ordinal: state.client_calls.length + index, tool_name: record.tool_name, input: record.input })));
    addUsage(state, phaseOneAudit.usage);
    atomicState(configuration.statePath, state);
    const phaseTwoAudit = await runClaude(configuration, state, configuration.stageRoot, "phase2", phaseTwoPrompt(state, configuration.releaseCase, state.archive), configuration.hardCaps.max_turns, configuration.hardCaps.max_budget_usd);
    const phaseTwoSummary = validatePhaseTwo(phaseTwoAudit, state, configuration.releaseCase, state.request_ids, state.archive, state.public_base_url);
    Object.assign(state, phaseTwoSummary);
    state.client_calls.push(...phaseTwoAudit.records.map((record, index) => ({ phase: "phase2", ordinal: state.client_calls.length + index, tool_name: record.tool_name, input: record.input })));
    addUsage(state, phaseTwoAudit.usage);
    atomicState(configuration.statePath, state);
    const correspondence = await stopService(configuration, state);
    const jobTypes = correspondence.service_invocations.map((invocation) => invocation.job_type).sort();
    requireCondition(jobTypes.length === 1 && jobTypes[0] === "ROUTE", "ROUTE_SERVICE_AGENT_INVOCATIONS", "FAIL", "CONTRACT");
    requireCondition(
      validRouteMethodsPreflightEvidence(correspondence.service_no_model_jobs, {
        registrationId: configuration.generatedSkill.registration_id,
        expectedJobId: state.methods_preflight_job_id,
      }),
      "ROUTE_METHODS_PREFLIGHT_JOBS",
      "FAIL",
      "CONTRACT",
    );
    const checkpoint = await createCheckpointSource(configuration, state, {
      case_id: state.case_id,
      attachment_id: state.attachment_id,
      prepared_case_revision: state.prepared_case_revision,
      request_ids: Object.values(state.request_ids),
    });
    if (!configuration.terminalAfterStage) await startService(configuration, state, "upload");
    const invocations = [
      clientInvocation(configuration, "route-intake", phaseOneAudit, configuration.hardCaps),
      clientInvocation(configuration, "route-supplement", phaseTwoAudit, configuration.hardCaps),
      ...correspondence.service_invocations,
    ];
    await stageReceipt(configuration, { status: "PASS", client_tool_calls: phaseOneAudit.records.length + phaseTwoAudit.records.length, server_tool_calls: correspondence.server_completed, checkpoint_ready: checkpoint.status === "PASS", invocations });
    return;
  }

  if (configuration.stage === "journey.cross-job.upload") {
    const upload = await uploadAttachment(configuration, state, configuration.stageRoot);
    Object.assign(state, upload);
    atomicState(configuration.statePath, state);
    const correspondence = await stopService(configuration, state);
    requireCondition(correspondence.service_invocations.length === 0, "UPLOAD_UNEXPECTED_MODEL_INVOCATION", "FAIL", "CONTRACT");
    requireCondition(correspondence.service_no_model_jobs.length === 0, "UPLOAD_UNEXPECTED_PREFLIGHT_ACTIVITY", "FAIL", "CONTRACT");
    const checkpoint = await createCheckpointSource(configuration, state, {
      case_id: state.case_id,
      attachment_id: state.attachment_id,
      case_revision: state.case_revision,
      attachment_status: state.status,
      request_ids: Object.values(state.request_ids),
    });
    if (!configuration.terminalAfterStage) await startService(configuration, state, "diagnose");
    await stageReceipt(configuration, { status: "PASS", client_tool_calls: 0, server_tool_calls: correspondence.server_completed, checkpoint_ready: checkpoint.status === "PASS", browser_upload: upload.browser_upload, invocations: [] });
    return;
  }

  if (configuration.stage === "journey.cross-job.diagnose") {
    requireCondition(configuration.hardCaps !== null, "DIAGNOSE_HARD_CAPS_MISSING", "BLOCKED", "INFRA");
    const audit = await runClaude(configuration, state, configuration.stageRoot, "phase3", phaseThreePrompt(state, configuration.releaseCase), configuration.hardCaps.max_turns, configuration.hardCaps.max_budget_usd);
    const summary = validatePhaseThree(audit, state, configuration.releaseCase);
    const browserApi = await verifyResolvedWebApi(
      configuration,
      state,
      summary,
      configuration.stageRoot,
    );
    Object.assign(state, summary);
    state.client_calls.push(...audit.records.map((record, index) => ({ phase: "phase3", ordinal: state.client_calls.length + index, tool_name: record.tool_name, input: record.input })));
    addUsage(state, audit.usage);
    atomicState(configuration.statePath, state);
    const correspondence = await stopService(configuration, state);
    const diagnoseCalls = correspondence.service_invocations.filter((invocation) => invocation.job_type === "DIAGNOSE");
    const reviewCalls = correspondence.service_invocations.filter((invocation) => invocation.job_type === "REVIEW");
    requireCondition(
      correspondence.service_invocations.length === diagnoseCalls.length + reviewCalls.length
        && diagnoseCalls.length >= 1 && diagnoseCalls.length <= 2
        && reviewCalls.length >= 1 && reviewCalls.length <= 2
        && correspondence.service_invocations.length >= 2 && correspondence.service_invocations.length <= 4,
      "METHODS_V2_SERVICE_AGENT_INVOCATIONS",
      "FAIL",
      "CONTRACT",
    );
    requireCondition(correspondence.service_no_model_jobs.length === 0, "DIAGNOSE_UNEXPECTED_PREFLIGHT_ACTIVITY", "FAIL", "CONTRACT");
    const methodsV2 = await captureMethodsV2Oracle(
      configuration,
      state,
      correspondence.service_invocations,
    );
    state.methods_v2 = methodsV2;
    atomicState(configuration.statePath, state);
    const checkpoint = await createCheckpointSource(configuration, state, {
      case_id: state.case_id,
      attachment_id: state.attachment_id,
      resolved_case_revision: state.resolved_case_revision,
      methods_result_ref: state.methods_result.result_ref,
      methods_source_job_id: methodsV2.source_job_id,
      methods_reviewer_job_id: methodsV2.reviewer_job_id,
      observed_statuses: state.observed_statuses,
    });
    const invocations = [clientInvocation(configuration, "diagnose", audit, configuration.hardCaps), ...correspondence.service_invocations];
    await stageReceipt(configuration, { status: "PASS", client_tool_calls: audit.records.length, server_tool_calls: correspondence.server_completed, checkpoint_ready: checkpoint.status === "PASS", browser_api: browserApi, methods_v2: methodsV2, invocations });
    return;
  }

  if (configuration.stage === "journey.cross-job.publish-restart") {
    requireCondition(!state.current_instance, "PUBLISH_RESTART_SERVICE_STILL_RUNNING");
    await docker(configuration.dockerContext, ["container", "stop", "--time", "10", state.initial_container]);
    await createContainer(configuration, state, state.restart_container, "restart", configuration.stage, true);
    await initializeContainer(configuration, state, state.restart_container, "restart", configuration.stage);
    // This phase is deliberately read-only (get_case + list_artifacts), so the
    // business Journey writer has no state transition to emit. Diagnostics
    // remain mandatory and are still used for exact MCP correspondence.
    await startService(configuration, state, "restart", { allowEmptyJourney: true });
    requireCondition(configuration.hardCaps !== null, "PUBLISH_RESTART_HARD_CAPS_MISSING", "BLOCKED", "INFRA");
    const audit = await runClaude(configuration, state, configuration.stageRoot, "restart", restartPrompt(state), configuration.hardCaps.max_turns, configuration.hardCaps.max_budget_usd);
    const restartView = validateRestart(audit, state, configuration.releaseCase);
    state.client_calls.push(...audit.records.map((record, index) => ({ phase: "restart", ordinal: state.client_calls.length + index, tool_name: record.tool_name, input: record.input })));
    addUsage(state, audit.usage);
    atomicState(configuration.statePath, state);
    await verifyRestartMethodsV2(configuration, state, restartView);
    const correspondence = await stopService(configuration, state);
    requireCondition(correspondence.service_invocations.length === 0, "RESTART_UNEXPECTED_MODEL_INVOCATION", "FAIL", "CONTRACT");
    requireCondition(correspondence.service_no_model_jobs.length === 0, "RESTART_UNEXPECTED_PREFLIGHT_ACTIVITY", "FAIL", "CONTRACT");
    const checkpoint = await createCheckpointSource(configuration, state, {
      case_id: state.case_id,
      resolved_case_revision: state.resolved_case_revision,
      methods_result_ref: state.methods_result.result_ref,
      methods_source_job_id: state.methods_v2.source_job_id,
      methods_reviewer_job_id: state.methods_v2.reviewer_job_id,
      restart_verified: true,
    });
    writeNew(path.join(configuration.stageRoot, "client-server-correspondence.json"), { schema_version: 1, ...correspondence });
    await stageReceipt(configuration, { status: "PASS", client_tool_calls: audit.records.length, server_tool_calls: correspondence.server_completed, checkpoint_ready: checkpoint.status === "PASS", restart_verified: true, invocations: [clientInvocation(configuration, "publish-restart", audit, configuration.hardCaps)] });
    return;
  }

  throw new StageError("CROSS_JOB_ADAPTER_STAGE_UNKNOWN");
}

async function main() {
const parsed = parseArguments(process.argv.slice(2));
const values = parsed.values;
const configuration = {
  stage: values.stage,
  repoRoot: values.repo_root && path.resolve(values.repo_root),
  attemptRoot: values.attempt_root && path.resolve(values.attempt_root),
  client: values.client,
  track: values.track,
  sourceSnapshotDigest: values.source_snapshot_digest,
  sourceSnapshotManifest: values.source_snapshot_manifest && path.resolve(values.source_snapshot_manifest),
  claudeEntry: values.claude_entry && path.resolve(values.claude_entry),
  claudeSettings: values.claude_settings && path.resolve(values.claude_settings),
  dockerContext: values.docker_context,
  cacheRoot: values.cache_root && path.resolve(values.cache_root),
  logparseSource: values.logparse_source && path.resolve(values.logparse_source),
  mcpSource: values.mcp_source && path.resolve(values.mcp_source),
  baseImage: values.base_image,
  expectedServerImageId: values.server_image_id,
  expectedClientImageId: values.client_image_id,
  resourceRegistry: values.resource_registry && path.resolve(values.resource_registry),
  resourceLabel: values.resource_label,
  gateId: values.gate_id,
  runtimeProfileDigest: values.runtime_profile_digest,
  expectedChromeVersion: values.chrome_version,
  expectedChromeSha256: values.chrome_sha256,
  checkpointOutputSource: values.checkpoint_output_source && path.resolve(values.checkpoint_output_source),
  restoredDataRoot: values.restored_data_root && path.resolve(values.restored_data_root),
  restoredCheckpointId: values.restored_checkpoint_id,
  restoredContinuation: values.restored_continuation && path.resolve(values.restored_continuation),
  freshDataRoot: parsed.flags.has("--fresh-data-root"),
  terminalAfterStage: parsed.flags.has("--terminal-after-stage"),
  expectedClient: process.env.TEST_FLOW_FIRST_PARTY_CLIENT,
  expectedHostPlatform: process.env.TEST_FLOW_FIRST_PARTY_HOST_PLATFORM,
  expectedDockerContext: process.env.TEST_FLOW_FIRST_PARTY_DOCKER_CONTEXT,
  topology: process.env.TEST_FLOW_FIRST_PARTY_TOPOLOGY,
  generatedSkillRoot: values.generated_skill_root && path.resolve(values.generated_skill_root),
  hardCaps: values.max_turns ? {
    max_turns: Number(values.max_turns),
    max_total_tokens: Number(values.max_total_tokens),
    max_budget_usd: Number(values.max_budget_usd),
    hard_timeout_seconds: Number(values.hard_timeout_seconds),
  } : null,
  serviceAgentCaps: {
    max_turns: Number(values.service_agent_max_turns),
    max_total_tokens: Number(values.service_agent_max_total_tokens),
    max_budget_usd: Number(values.service_agent_max_budget_usd),
    hard_timeout_seconds: Number(values.service_agent_hard_timeout_seconds),
  },
};
configuration.statePath = configuration.attemptRoot && path.join(configuration.attemptRoot, "scratch", "cross-job", "state.json");
configuration.stageRoot = configuration.attemptRoot && ADAPTER_STAGE_IDS.has(configuration.stage)
  ? path.join(configuration.attemptRoot, "payload", "stages", configuration.stage)
  : null;

let receiptWritten = false;
try {
  requireCondition(ADAPTER_STAGE_IDS.has(configuration.stage), "ADAPTER_STAGE_INVALID");
  for (const [name, value] of Object.entries({ stage: configuration.stage, gateId: configuration.gateId, runtimeProfileDigest: configuration.runtimeProfileDigest, expectedClient: configuration.expectedClient, expectedHostPlatform: configuration.expectedHostPlatform, topology: configuration.topology, expectedChromeVersion: configuration.expectedChromeVersion, expectedChromeSha256: configuration.expectedChromeSha256, repoRoot: configuration.repoRoot, attemptRoot: configuration.attemptRoot, sourceSnapshotDigest: configuration.sourceSnapshotDigest, sourceSnapshotManifest: configuration.sourceSnapshotManifest, resourceRegistry: configuration.resourceRegistry, resourceLabel: configuration.resourceLabel, baseImage: configuration.baseImage, generatedSkillRoot: configuration.generatedSkillRoot })) {
    requireCondition(Boolean(value), `ADAPTER_REQUIRED_${name.toUpperCase()}`);
  }
  requireCondition(/^sha256:[a-f0-9]{64}$/.test(configuration.expectedServerImageId ?? ""), "SERVER_IMAGE_ID_REQUIRED", "BLOCKED", "INFRA");
  if (configuration.topology === DUAL_LINUX_TOPOLOGY) {
    requireCondition(
      configuration.expectedChromeVersion === RELEASE_CHROME_HEADLESS_SHELL_VERSION_OUTPUT
        && configuration.expectedChromeSha256 === RELEASE_CHROME_HEADLESS_SHELL_EXECUTABLE_SHA256,
      "CHROME_IDENTITY_DRIFT",
      "BLOCKED",
      "INFRA",
    );
    requireCondition(/^sha256:[a-f0-9]{64}$/.test(configuration.expectedClientImageId ?? ""), "DUAL_LINUX_IMAGE_IDS_REQUIRED", "BLOCKED", "INFRA");
  } else {
    const observedChrome = chromeIdentity();
    requireCondition(observedChrome.status === "PRESENT" && observedChrome.version === configuration.expectedChromeVersion && observedChrome.executable_sha256 === configuration.expectedChromeSha256, "CHROME_IDENTITY_DRIFT", "BLOCKED", "INFRA");
  }
  configuration.generatedSkill = validateGeneratedSkillRoot(configuration.generatedSkillRoot, configuration.attemptRoot);
  if (configuration.hardCaps) {
    requireCondition(Number.isSafeInteger(configuration.hardCaps.max_turns) && configuration.hardCaps.max_turns > 0, "ADAPTER_MAX_TURNS_INVALID");
    requireCondition(Number.isSafeInteger(configuration.hardCaps.max_total_tokens) && configuration.hardCaps.max_total_tokens > 0, "ADAPTER_MAX_TOKENS_INVALID");
    requireCondition(Number.isFinite(configuration.hardCaps.max_budget_usd) && configuration.hardCaps.max_budget_usd > 0, "ADAPTER_MAX_BUDGET_INVALID");
    requireCondition(Number.isSafeInteger(configuration.hardCaps.hard_timeout_seconds) && configuration.hardCaps.hard_timeout_seconds > 0, "ADAPTER_HARD_TIMEOUT_INVALID");
  }
  requireCondition(Number.isSafeInteger(configuration.serviceAgentCaps.max_turns) && configuration.serviceAgentCaps.max_turns > 0, "ADAPTER_SERVICE_MAX_TURNS_INVALID");
  requireCondition(Number.isSafeInteger(configuration.serviceAgentCaps.max_total_tokens) && configuration.serviceAgentCaps.max_total_tokens > 0, "ADAPTER_SERVICE_MAX_TOKENS_INVALID");
  requireCondition(Number.isFinite(configuration.serviceAgentCaps.max_budget_usd) && configuration.serviceAgentCaps.max_budget_usd > 0, "ADAPTER_SERVICE_MAX_BUDGET_INVALID");
  requireCondition(Number.isSafeInteger(configuration.serviceAgentCaps.hard_timeout_seconds) && configuration.serviceAgentCaps.hard_timeout_seconds > 0, "ADAPTER_SERVICE_HARD_TIMEOUT_INVALID");
  configuration.releaseCase = selectedReleaseCase(configuration.repoRoot, configuration.generatedSkill);
  ensureDirectory(configuration.stageRoot);
  await execute(configuration);
  receiptWritten = true;
  process.stdout.write("TEST_FLOW_PROGRESS stage.completed\n");
} catch (error) {
  const failure = error instanceof StageError ? error : new StageError(`ADAPTER_UNEXPECTED:${String(error?.message ?? error)}`);
  try { await archiveFailureServiceEvidence(configuration); } catch {}
  try {
    if (configuration.stageRoot) {
      ensureDirectory(configuration.stageRoot);
      const receiptPath = path.join(configuration.stageRoot, "adapter-result.json");
      if (!fs.existsSync(receiptPath)) {
        const progress = recoverStageAuditProgress({
          attemptRoot: configuration.attemptRoot,
          stageRoot: configuration.stageRoot,
          stageId: configuration.stage,
        });
        writeNew(receiptPath, {
          schema_version: 3,
          stage_id: configuration.stage ?? "unknown",
          gate_id: configuration.gateId ?? null,
          runtime_profile_digest: configuration.runtimeProfileDigest ?? null,
          topology: configuration.topology ?? null,
          runtime_images: {
            server_image_id: configuration.expectedServerImageId ?? null,
            client_image_id: configuration.topology === DUAL_LINUX_TOPOLOGY ? configuration.expectedClientImageId ?? null : null,
          },
          generated_skill: configuration.generatedSkill ? {
            registration_id: configuration.generatedSkill.registration_id,
            skill_name: configuration.generatedSkill.skill_name,
            tree_digest: configuration.generatedSkill.tree_digest,
            package_digest: configuration.generatedSkill.package_digest,
            registration_sha256: configuration.generatedSkill.registration_sha256,
            package_tree_sha256: configuration.generatedSkill.package_tree_sha256,
            combined_sha256: configuration.generatedSkill.combined_sha256,
            content_tree_sha256: configuration.generatedSkill.content_tree_sha256,
            generation_receipt_sha256: configuration.generatedSkill.generation_receipt_sha256,
            source_wiki_sha256: configuration.generatedSkill.source_wiki_sha256,
          } : null,
          effective_caps: configuration.hardCaps,
          usage_complete: false,
          status: failure.status,
          failure_domain: failure.domain,
          code: failure.code,
          browser_failure: failure.browserFailure,
          client_tool_calls: progress.client_tool_calls,
          server_tool_calls: progress.server_tool_calls,
          checkpoint_ready: false,
          usage: progress.usage,
        });
        receiptWritten = true;
      }
    }
  } catch {}
  process.stderr.write(`${failure.code}\n`);
  process.exitCode = failure.status === "INCONCLUSIVE" || failure.status === "BLOCKED" ? 2 : failure.status === "FAIL" ? 1 : 3;
}

void receiptWritten;
}

const invokedDirectly = process.argv[1]
  ? path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)
  : false;

if (invokedDirectly || process.env.TEST_FLOW_FIRST_PARTY_TOPOLOGY) await main();
