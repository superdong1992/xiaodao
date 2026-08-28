import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  buildLinuxClientBrowserFailureReceipt,
  installGeneratedSkill,
  linuxClientUserIdentity,
  parseLinuxClientBrowserExecution,
  runCommandCapture,
  validRouteMethodsPreflightEvidence,
  validLinuxClientBrowserExecution,
  validServerRuntimeInspection,
  validServiceAgentUsageReceipt,
  validSuccessfulInvocationReceipt,
} from "../adapters/cross-job-core.mjs";
import {
  validateMethodsGroundingExecutionRecord,
  validateReleaseDiagnosisReport,
} from "../lib/methods-oracle.mjs";
import {
  crossJobBrowserCapabilityPolicy,
  crossJobBrowserFailureContract,
  dockerRuntimeBoundaryResult,
  validCrossJobPassRuntimeBoundary,
  validCrossJobBrowserFailureBinding,
  validLinuxClientBrowserFailureReceipt,
  validMethodsGroundingOracleEvidence,
} from "../lib/actions.mjs";
import { packageTreeIdentity } from "../lib/release-inputs.mjs";
import { canonicalJson, sha256Bytes, sha256File } from "../lib/util.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function jsonBytes(value) {
  return Buffer.from(canonicalJson(value), "utf8");
}

function resourceName(prefix, runId, suffix = "") {
  const digest = crypto.createHash("sha256").update(`${runId}:${suffix}`).digest("hex").slice(0, 16);
  return `${prefix}-${digest}${suffix ? `-${suffix}` : ""}`;
}

const ROUTE_JOB_ID = "00000000-0000-0000-0000-000000000001";
const PREFLIGHT_JOB_ID = "00000000-0000-0000-0000-000000000002";

function successfulServiceInvocation(jobId = ROUTE_JOB_ID) {
  return {
    schema_version: 3,
    invocation_id: `server-agent:${jobId}:1`,
    class: "server-agent",
    job_id: jobId,
    job_type: "ROUTE",
    usage_complete: true,
    usage: {
      schema_version: 1,
      input_tokens: 1,
      output_tokens: 2,
      cache_creation_input_tokens: 3,
      cache_read_input_tokens: 4,
      total_tokens: 10,
      cost_usd: 0.01,
    },
    terminal: { subtype: "success", is_error: false },
    wrapper_outcome: { schema_version: 1, status: "PASS", code: null },
  };
}

function methodsPreflightReceipt(jobId = PREFLIGHT_JOB_ID) {
  return {
    schema_version: 2,
    kind: "methods-server-preflight",
    job_id: jobId,
    job_type: "DIAGNOSE",
    result_type: "NEED_ATTACHMENT",
    registration_id: "rpc-timeout-methods-v1",
    decision_audit_absent: true,
    model_invoked: false,
    log_pair: "ABSENT",
    job_sha256: "a".repeat(64),
    job_outcome_sha256: "b".repeat(64),
    methods_preflight_sha256: "c".repeat(64),
  };
}

function serviceAgentUsageReceipt() {
  return {
    schema_version: 3,
    status: "PASS",
    usage_complete: true,
    token_formula: "input_tokens+output_tokens+cache_creation_input_tokens+cache_read_input_tokens",
    invocations: [successfulServiceInvocation()],
    no_model_jobs: [methodsPreflightReceipt()],
    new_job_ids: [ROUTE_JOB_ID, PREFLIGHT_JOB_ID],
  };
}

function nativeServerInspection() {
  const runId = "run-native-runtime-boundary";
  const imageId = `sha256:${"a".repeat(64)}`;
  const container = resourceName("pltf-server", runId, "initial");
  const port = 43127;
  const binding = [{ HostIp: "127.0.0.1", HostPort: String(port) }];
  return {
    topology: "host-client",
    stageId: "journey.cross-job.environment",
    expectedServerImageId: imageId,
    expectedRunId: runId,
    state: {
      run_id: runId,
      image_id: imageId,
      initial_container: container,
      restart_container: resourceName("pltf-server", runId, "restart"),
      active_container: container,
      port,
      network: null,
      client_container: null,
      client_image_id: null,
      runtime_images: { server_image_id: imageId, client_image_id: null },
      selected_client_runtime_observed: null,
    },
    server: {
      Name: `/${container}`,
      Image: imageId,
      Config: { Image: imageId, Labels: { "problem-locator.test-flow.run": runId } },
      State: { Running: true },
      HostConfig: { PortBindings: { "8000/tcp": binding } },
      NetworkSettings: { Ports: { "8000/tcp": binding } },
      Mounts: [],
    },
    serverImage: { Id: imageId, Os: "linux", Architecture: "amd64" },
  };
}

test("dual Linux Client identity requires one explicit non-root uid:gid", () => {
  assert.deepEqual(linuxClientUserIdentity({ uid: 501, gid: 20 }), {
    uid: 501,
    gid: 20,
    root: false,
    docker_user: "501:20",
  });
  assert.throws(() => linuxClientUserIdentity({ uid: 0, gid: 0 }), /LINUX_CLIENT_NON_ROOT_UID_REQUIRED/);
  assert.throws(() => linuxClientUserIdentity({ uid: 501, gid: -1 }), /LINUX_CLIENT_GID_REQUIRED/);
});

test("dual Linux Client container binds one writable HOME before its runtime probe", () => {
  const source = fs.readFileSync(new URL("../adapters/cross-job-core.mjs", import.meta.url), "utf8");
  const start = source.indexOf("async function createLinuxClientContainer");
  const end = source.indexOf("async function probeLinuxClientRuntime", start);
  const section = source.slice(start, end);
  assert.match(section, /"--env", `HOME=\$\{LINUX_CLIENT_HOME\}`/);
  assert.equal((section.match(/HOME=\$\{LINUX_CLIENT_HOME\}/g) ?? []).length, 1);
  assert.match(source.slice(end, source.indexOf("async function createFreshEnvironment", end)), /runtimeIdentity\.home_writable === true/);
});

const posixRuntimeTest = process.platform === "win32" ? test.skip : test;

posixRuntimeTest("captured commands wait for inherited stdout to close before sealing evidence", async () => {
  const child = `setTimeout(() => { process.stdout.write("late-tail"); }, 60);`;
  const parent = `const {spawn}=require("node:child_process");const child=spawn(process.execPath,["-e",${JSON.stringify(child)}],{stdio:["ignore",1,2]});child.unref();process.exit(0);`;
  const result = await runCommandCapture(process.execPath, ["-e", parent], { forward: false });
  assert.equal(result.status, 0);
  assert.equal(result.signal, null);
  assert.equal(result.stdout, "late-tail");
});

function browserExecution({ stdout = "<html data-result=\"QQ==\"></html>", signalNumber = null, exitCode = 0 } = {}) {
  const capture = (value) => ({
    byte_count: Buffer.byteLength(value),
    sha256: sha256Bytes(value),
    truncated: false,
  });
  return {
    schema_version: 1,
    wrapper_status: "PASS",
    failure_code: null,
    label: "capability",
    argument_profile: "chrome-headless-shell-for-testing-local-v1",
    home: { path: "/client-home", realpath: "/client-home", present: true, writable: true },
    browser_started: true,
    browser_exit_code: signalNumber === null ? exitCode : null,
    browser_signal_number: signalNumber,
    browser_signal_name: signalNumber === null ? null : "SIGTRAP",
    timed_out: false,
    stdout: capture(stdout),
    stderr: capture("redacted-by-runner"),
    cleanup: {
      http_server_stopped: true,
      profile_removed: true,
      process_tree: {
        strategy: "posix-process-group-v1",
        session_started: true,
        termination_reason: "NONE",
        term_sent: false,
        kill_sent: false,
        parent_reaped: true,
        group_absent: true,
      },
    },
  };
}

test("Linux browser execution receipt distinguishes confirmed child signals from outer exit conventions", () => {
  const stdout = "<html data-result=\"QQ==\"></html>";
  const summary = browserExecution({ stdout });
  assert.equal(validLinuxClientBrowserExecution(summary, { label: "capability", stdout }), true);
  const encoded = Buffer.from(canonicalJson(summary), "utf8").toString("base64");
  assert.deepEqual(parseLinuxClientBrowserExecution({ stdout, stderr: `TEST_FLOW_BROWSER_EXECUTION_V1=${encoded}\n` }, "capability"), summary);

  const secretStdout = "RAW_DOM_SECRET";
  const secretStderr = "RAW_STDERR_SECRET";
  const unconfirmed = buildLinuxClientBrowserFailureReceipt({
    label: "capability",
    runId: "run-browser-failure",
    clientContainer: "pltf-client-browser-failure",
    clientImageId: `sha256:${"a".repeat(64)}`,
    clientRuntime: { user: { uid: 501, gid: 20, root: false } },
    browser: { status: "PRESENT", product: "Chrome Headless Shell for Testing", version: "Google Chrome for Testing 152.0", executable_sha256: "b".repeat(64), code: null },
    runnerSha256: "c".repeat(64),
    chromeRun: { status: 133, signal: null, stdout: secretStdout, stderr: secretStderr },
    execution: null,
    status: "BLOCKED",
    failureDomain: "INFRA",
    code: "CHROME_CAPABILITY_DOCKER_EXIT_133",
  });
  assert.equal(unconfirmed.launcher.encoded_signal_candidate, 5);
  assert.equal(unconfirmed.launcher.candidate_attribution, "UNCONFIRMED_POSIX_EXIT_CONVENTION");
  assert.equal(unconfirmed.browser_process.signal_number, null);
  assert.equal(JSON.stringify(unconfirmed).includes(secretStdout), false);
  assert.equal(JSON.stringify(unconfirmed).includes(secretStderr), false);
  const plan = {
    run_id: "run-browser-failure",
    release_inputs: {
      image: { client: { image_id: `sha256:${"a".repeat(64)}` } },
      browser: {
        version: "Google Chrome for Testing 152.0",
        executable_sha256: "b".repeat(64),
      },
    },
  };
  assert.equal(validLinuxClientBrowserFailureReceipt(unconfirmed, {
    plan,
    stageId: "journey.cross-job.environment",
    status: "BLOCKED",
    failureDomain: "INFRA",
    code: "CHROME_CAPABILITY_DOCKER_EXIT_133",
    clientContainer: "pltf-client-browser-failure",
    runnerSha256: "c".repeat(64),
  }), true);

  const confirmedExecution = browserExecution({ stdout, signalNumber: 5 });
  const confirmed = buildLinuxClientBrowserFailureReceipt({
    label: "capability",
    runId: "run-browser-failure",
    clientContainer: "pltf-client-browser-failure",
    clientImageId: `sha256:${"a".repeat(64)}`,
    clientRuntime: { user: { uid: 501, gid: 20, root: false } },
    browser: unconfirmed.browser,
    runnerSha256: "c".repeat(64),
    chromeRun: { status: 0, signal: null, stdout, stderr: "summary-only" },
    execution: confirmedExecution,
    status: "BLOCKED",
    failureDomain: "INFRA",
    code: "CHROME_CAPABILITY_SIGNAL_SIGTRAP",
  });
  assert.equal(confirmed.launcher.encoded_signal_candidate, null);
  assert.equal(confirmed.browser_process.signal_number, 5);
  assert.equal(confirmed.browser_process.signal_name, "SIGTRAP");
  assert.equal(confirmed.browser_process.attribution, "CONFIRMED_SUBPROCESS_SIGNAL");
  const expected = {
    plan,
    stageId: "journey.cross-job.environment",
    status: "BLOCKED",
    failureDomain: "INFRA",
    code: "CHROME_CAPABILITY_SIGNAL_SIGTRAP",
    clientContainer: "pltf-client-browser-failure",
    runnerSha256: "c".repeat(64),
  };
  assert.equal(validLinuxClientBrowserFailureReceipt(confirmed, expected), true);
  const mutations = [
    (value) => { value.label = "upload"; },
    (value) => { value.client.container = "pltf-client-other"; },
    (value) => { value.client.image_id = `sha256:${"d".repeat(64)}`; },
    (value) => { value.client.user = { uid: 0, gid: 0, root: true }; },
    (value) => { value.client.home.path = "/root"; },
    (value) => { value.browser.version = "Google Chrome 999"; },
    (value) => { value.browser.executable_sha256 = "e".repeat(64); },
    (value) => { value.runner.sha256 = "f".repeat(64); },
    (value) => { value.launcher.encoded_signal_candidate = 5; },
    (value) => { value.wrapper.cleanup.profile_removed = "yes"; },
    (value) => { value.wrapper.cleanup.process_tree.group_absent = false; },
    (value) => { value.wrapper.cleanup.process_tree.kill_sent = true; value.wrapper.cleanup.process_tree.term_sent = false; },
    (value) => { value.browser_process.attribution = "CONFIRMED_SUBPROCESS_EXIT_CODE"; },
    (value) => { value.capture.browser_stdout.sha256 = "0"; },
    (value) => { value.code = "CHROME_CAPABILITY_SIGNAL_SIGSEGV"; },
  ];
  for (const mutate of mutations) {
    const changed = clone(confirmed);
    mutate(changed);
    assert.equal(validLinuxClientBrowserFailureReceipt(changed, expected), false);
  }

  const coherentDisposition = clone(confirmed);
  coherentDisposition.status = "FAIL";
  coherentDisposition.failure_domain = "BROWSER";
  assert.equal(validLinuxClientBrowserFailureReceipt(coherentDisposition, {
    ...expected,
    status: "FAIL",
    failureDomain: "BROWSER",
  }), false);

  const dockerExitZero = clone(unconfirmed);
  dockerExitZero.code = "CHROME_CAPABILITY_DOCKER_EXIT_0";
  dockerExitZero.launcher.exit_code = 0;
  dockerExitZero.launcher.encoded_signal_candidate = null;
  dockerExitZero.launcher.candidate_attribution = null;
  assert.equal(validLinuxClientBrowserFailureReceipt(dockerExitZero, {
    ...expected,
    code: dockerExitZero.code,
  }), false);

  const browserExitZero = clone(confirmed);
  browserExitZero.code = "CHROME_CAPABILITY_EXIT_0";
  browserExitZero.browser_process = {
    started: true,
    exit_code: 0,
    signal_number: null,
    signal_name: null,
    timed_out: false,
    attribution: "CONFIRMED_SUBPROCESS_EXIT_CODE",
  };
  assert.equal(validLinuxClientBrowserFailureReceipt(browserExitZero, {
    ...expected,
    code: browserExitZero.code,
  }), false);

  const resultMissing = clone(confirmed);
  resultMissing.code = "CHROME_CAPABILITY_RESULT_MISSING";
  resultMissing.browser_process = {
    started: true,
    exit_code: 0,
    signal_number: null,
    signal_name: null,
    timed_out: false,
    attribution: "CONFIRMED_SUBPROCESS_EXIT_CODE",
  };
  resultMissing.capture.result_marker_present = false;
  assert.equal(validLinuxClientBrowserFailureReceipt(resultMissing, { ...expected, code: resultMissing.code }), true);
  resultMissing.capture.result_marker_present = true;
  assert.equal(validLinuxClientBrowserFailureReceipt(resultMissing, { ...expected, code: resultMissing.code }), false);

  const runtimeBoundary = clone(resultMissing);
  runtimeBoundary.code = "CHROME_CAPABILITY_RUNTIME_BOUNDARY_INVALID";
  assert.equal(validLinuxClientBrowserFailureReceipt(runtimeBoundary, { ...expected, code: runtimeBoundary.code }), false);
  runtimeBoundary.client.home.writable = false;
  assert.equal(validLinuxClientBrowserFailureReceipt(runtimeBoundary, { ...expected, code: runtimeBoundary.code }), true);
});

test("environment browser capability routing requires exact null for host Client and validated PASS for dual Linux", () => {
  assert.deepEqual(crossJobBrowserFailureContract("journey.cross-job.environment"), {
    label: "capability",
    status: "BLOCKED",
    failure_domain: "INFRA",
    path: "chrome-capability-failure.json",
  });
  assert.deepEqual(crossJobBrowserFailureContract("journey.cross-job.upload"), {
    label: "upload",
    status: "FAIL",
    failure_domain: "BROWSER",
    path: "chrome-upload-failure.json",
  });
  assert.equal(crossJobBrowserFailureContract("journey.cross-job.route"), null);
  assert.equal(validCrossJobBrowserFailureBinding("journey.cross-job.environment", {
    path: "chrome-capability-failure.json",
    sha256: "a".repeat(64),
  }), true);
  assert.equal(validCrossJobBrowserFailureBinding("journey.cross-job.environment", {
    path: "chrome-upload-failure.json",
    sha256: "a".repeat(64),
  }), false);
  assert.equal(crossJobBrowserCapabilityPolicy({
    topology: "host-client",
    stageId: "journey.cross-job.environment",
    status: "PASS",
    capability: null,
    capabilityValid: false,
  }), true);
  assert.equal(crossJobBrowserCapabilityPolicy({
    topology: "host-client",
    stageId: "journey.cross-job.environment",
    status: "PASS",
    capability: { status: "PASS" },
    capabilityValid: true,
  }), false);
  assert.equal(crossJobBrowserCapabilityPolicy({
    topology: "dual-linux-containers",
    stageId: "journey.cross-job.environment",
    status: "PASS",
    capability: null,
    capabilityValid: false,
  }), false);
  assert.equal(crossJobBrowserCapabilityPolicy({
    topology: "dual-linux-containers",
    stageId: "journey.cross-job.environment",
    status: "PASS",
    capability: { status: "PASS" },
    capabilityValid: true,
  }), true);
});

test("service Agent usage receipt is closed and new_job_ids exactly equals its evidence union", () => {
  const passing = serviceAgentUsageReceipt();
  assert.equal(validServiceAgentUsageReceipt(passing), true);

  const mutations = [
    (value) => { value.unexpected = true; },
    (value) => { value.new_job_ids.push("00000000-0000-0000-0000-000000000003"); },
    (value) => { value.new_job_ids.pop(); },
    (value) => { value.new_job_ids.push(PREFLIGHT_JOB_ID); },
    (value) => { value.new_job_ids[1] = 2; },
    (value) => { value.new_job_ids.reverse(); },
    (value) => { value.invocations[0].job_id = 2; },
    (value) => { value.no_model_jobs.push(clone(value.no_model_jobs[0])); },
    (value) => {
      value.no_model_jobs[0].job_id = ROUTE_JOB_ID;
      value.new_job_ids = [ROUTE_JOB_ID];
    },
  ];
  for (const mutate of mutations) {
    const candidate = clone(passing);
    mutate(candidate);
    assert.equal(validServiceAgentUsageReceipt(candidate), false);
  }
});

test("client invocation omits server job_id while service Agent evidence still requires it", () => {
  const client = successfulServiceInvocation();
  client.class = "linux-client-container";
  client.invocation_id = "linux-client-container:route";
  delete client.job_id;
  delete client.job_type;
  assert.equal(validSuccessfulInvocationReceipt(client), true);

  const serviceReceipt = serviceAgentUsageReceipt();
  serviceReceipt.invocations = [client];
  serviceReceipt.new_job_ids = [PREFLIGHT_JOB_ID];
  assert.equal(validServiceAgentUsageReceipt(serviceReceipt), false);
});

test("route no-model evidence binds the selected generated registration and waiting Job", () => {
  const passing = methodsPreflightReceipt();
  const expected = {
    registrationId: "rpc-timeout-methods-v1",
    expectedJobId: PREFLIGHT_JOB_ID,
  };
  assert.equal(validRouteMethodsPreflightEvidence([passing], expected), true);
  assert.equal(validRouteMethodsPreflightEvidence([], expected), false);
  assert.equal(validRouteMethodsPreflightEvidence([passing, passing], expected), false);

  for (const mutate of [
    (value) => { value.registration_id = "other-methods-v1"; },
    (value) => { value.result_type = "NEED_INPUT"; },
    (value) => { value.job_id = ROUTE_JOB_ID; },
    (value) => { value.unexpected = true; },
  ]) {
    const candidate = clone(passing);
    mutate(candidate);
    assert.equal(validRouteMethodsPreflightEvidence([candidate], expected), false);
  }
});

test("installed generated Skill identity is independent of probe traversal order", async () => {
  const temporaryRoot = fs.existsSync("/private/tmp") ? "/private/tmp" : os.tmpdir();
  const attemptRoot = fs.mkdtempSync(path.join(temporaryRoot, "test-flow-installed-skill-order-"));
  const sha = "a".repeat(64);
  const installedEntries = [
    { path: "rpc-timeout-methods-v1/registration-template.json", size: 2, sha256: sha },
    { path: "rpc-timeout-methods-v1/package/diagnose-rpc-timeout/SKILL.md", size: 3, sha256: "b".repeat(64) },
    { path: "rpc-timeout-methods-v1/package/diagnose-rpc-timeout/methods.json", size: 4, sha256: "c".repeat(64) },
  ];
  const canonicalEntries = [...installedEntries].sort((left, right) => (
    left.path < right.path ? -1 : left.path > right.path ? 1 : 0
  ));
  const contentTreeSha256 = sha256Bytes(canonicalJson({ version: 1, entries: canonicalEntries }));
  const generatedSkill = {
    registration_id: "rpc-timeout-methods-v1",
    skill_name: "diagnose-rpc-timeout",
    tree_digest: sha,
    package_digest: sha,
    registration_sha256: sha,
    package_tree_sha256: sha,
    combined_sha256: sha,
    content_tree_sha256: contentTreeSha256,
    generation_receipt_sha256: sha,
    source_wiki_sha256: sha,
  };
  let dockerCalls = 0;
  const dockerRunner = async () => {
    dockerCalls += 1;
    return dockerCalls === 1
      ? { stdout: "", stderr: "" }
      : { stdout: JSON.stringify(installedEntries), stderr: "" };
  };
  const state = {};
  try {
    const receipt = await installGeneratedSkill({
      attemptRoot,
      dockerContext: "test",
      generatedSkill,
      statePath: path.join(attemptRoot, "scratch", "cross-job", "state.json"),
    }, state, "test-client", "journey.cross-job.environment", dockerRunner);
    assert.equal(dockerCalls, 2);
    assert.equal(receipt.content_tree_sha256, contentTreeSha256);
    assert.equal(receipt.installed_content_tree_sha256, contentTreeSha256);
    assert.equal(state.generated_skill.installed_content_tree_sha256, contentTreeSha256);

    const mutatedEntries = clone(installedEntries);
    mutatedEntries[1].sha256 = "d".repeat(64);
    let mutationCalls = 0;
    const mutatedDockerRunner = async () => {
      mutationCalls += 1;
      return mutationCalls === 1
        ? { stdout: "", stderr: "" }
        : { stdout: JSON.stringify(mutatedEntries), stderr: "" };
    };
    await assert.rejects(
      installGeneratedSkill({
        attemptRoot: path.join(attemptRoot, "mutated"),
        dockerContext: "test",
        generatedSkill,
        statePath: path.join(attemptRoot, "mutated", "scratch", "cross-job", "state.json"),
      }, {}, "test-client", "journey.cross-job.environment", mutatedDockerRunner),
      (error) => error.code === "GENERATED_SKILL_INSTALLED_TREE_DRIFT",
    );
  } finally {
    fs.rmSync(attemptRoot, { recursive: true, force: true });
  }
});

test("native CrossJob server inspect is exact and rejects state, image, label, socket and publish mutations", () => {
  const passing = nativeServerInspection();
  assert.equal(validServerRuntimeInspection(passing), true);
  const restarted = clone(passing);
  restarted.stageId = "journey.cross-job.publish-restart";
  restarted.state.active_container = restarted.state.restart_container;
  restarted.server.Name = `/${restarted.state.restart_container}`;
  assert.equal(validServerRuntimeInspection(restarted), true);

  const mutations = [
    (value) => { value.state.image_id = `sha256:${"b".repeat(64)}`; },
    (value) => { value.state.runtime_images.server_image_id = `sha256:${"b".repeat(64)}`; },
    (value) => { value.server.Image = `sha256:${"b".repeat(64)}`; },
    (value) => { value.server.Config.Image = "mutable:latest"; },
    (value) => {
      value.state.active_container = "pltf-server-valid-looking-but-unplanned-initial";
      value.server.Name = `/${value.state.active_container}`;
    },
    (value) => { value.state.initial_container = "pltf-server-valid-looking-but-unplanned-initial"; },
    (value) => { value.server.Name = "/another-container"; },
    (value) => { value.server.Config.Labels["problem-locator.test-flow.run"] = "run-other"; },
    (value) => { value.server.State.Running = false; },
    (value) => { value.server.Mounts.push({ Source: "/var/run/docker.sock", Destination: "/run/docker.sock" }); },
    (value) => { value.server.HostConfig.PortBindings["8000/tcp"][0].HostPort = "43128"; },
    (value) => { value.server.HostConfig.PortBindings["9000/tcp"] = [{ HostIp: "127.0.0.1", HostPort: "49000" }]; },
    (value) => { value.server.NetworkSettings.Ports["8000/tcp"][0].HostIp = "0.0.0.0"; },
    (value) => { value.serverImage.Architecture = "arm64"; },
  ];
  for (const mutate of mutations) {
    const changed = clone(passing);
    mutate(changed);
    assert.equal(validServerRuntimeInspection(changed), false);
  }
});

function methodsGroundingFixture() {
  const diagnosisJobId = "00000000-0000-4000-8000-000000000011";
  const caseId = "00000000-0000-4000-8000-000000000001";
  const registrationSha256 = "a".repeat(64);
  const packageTreeSha256 = "b".repeat(64);
  const combinedSha256 = "c".repeat(64);
  const logparseReceiptBytes = jsonBytes({ schema_version: 1, status: "PASS" });
  const skillRef = {
    id: "diagnosis-skill/rpc-timeout-methods-v1",
    version: "1.0.0",
    content_hash: combinedSha256,
  };
  const job = {
    job_id: diagnosisJobId,
    case_id: caseId,
    job_type: "DIAGNOSE",
    diagnosis_mode: "SPECIALIZED",
    logparse_product: "rpc-skill-feasibility",
    skill_ref: skillRef,
  };
  const jobBytes = jsonBytes(job);
  const auditBytes = jsonBytes({
    schema_version: 1,
    registration_id: "rpc-timeout-methods-v1",
    registration_sha256: registrationSha256,
    package_tree_sha256: packageTreeSha256,
    combined_sha256: combinedSha256,
    logparse_receipt_sha256: crypto.createHash("sha256").update(logparseReceiptBytes).digest("hex"),
    status: "CONFIRMED",
    confirmed_methods: ["api-overrun"],
    evidence_count: 2,
    checked_source_count: 2,
    skill_load: {
      package_tree_sha256: packageTreeSha256,
      scanned_source_ids: ["client", "server"],
      marker_hits: [["server", "API_COMPLETE", 1], ["server", "API_COMPLETE", 2]],
      loaded_method_ids: ["api-overrun"],
    },
  });
  const expected = {
    diagnosis_job_id: diagnosisJobId,
    case_id: caseId,
    skill_ref: skillRef,
    logparse_product: "rpc-skill-feasibility",
    registration_id: "rpc-timeout-methods-v1",
    registration_sha256: registrationSha256,
    package_tree_sha256: packageTreeSha256,
    combined_sha256: combinedSha256,
    status: "CONFIRMED",
    confirmed_methods: ["api-overrun"],
    known_method_ids: ["api-overrun", "queue-delay"],
    source_ids: ["client", "server"],
    evidence_count: 2,
  };
  return { jobBytes, auditBytes, logparseReceiptBytes, expected };
}

test("CrossJob Methods status oracle reads the exact grounded execution record and fails on status or identity drift", () => {
  const fixture = methodsGroundingFixture();
  const validated = validateMethodsGroundingExecutionRecord(fixture);
  assert.equal(validated.actual_methods_status, "CONFIRMED");
  assert.equal(validated.expected_methods_status, "CONFIRMED");
  assert.equal(validated.evidence_count, 2);

  const wrongStatus = clone(JSON.parse(fixture.auditBytes));
  wrongStatus.status = "PARTIAL";
  assert.throws(
    () => validateMethodsGroundingExecutionRecord({ ...fixture, auditBytes: jsonBytes(wrongStatus) }),
    (error) => error.code === "METHODS_ORACLE_STATUS_MISMATCH",
  );

  const wrongJob = clone(JSON.parse(fixture.jobBytes));
  wrongJob.skill_ref.content_hash = "d".repeat(64);
  assert.throws(
    () => validateMethodsGroundingExecutionRecord({ ...fixture, jobBytes: jsonBytes(wrongJob) }),
    (error) => error.code === "METHODS_ORACLE_SKILL_REF_MISMATCH",
  );

  const nonCanonicalJob = Buffer.from(JSON.stringify(JSON.parse(fixture.jobBytes), null, 2), "utf8");
  assert.throws(
    () => validateMethodsGroundingExecutionRecord({ ...fixture, jobBytes: nonCanonicalJob }),
    (error) => error.code === "METHODS_ORACLE_JSON_NON_CANONICAL",
  );
});

function materializeMethodsConsumerFixture() {
  const temporaryRoot = fs.existsSync("/private/tmp") ? "/private/tmp" : os.tmpdir();
  const attemptRoot = fs.mkdtempSync(path.join(temporaryRoot, "test-flow-methods-consumer-"));
  const generationGateRoot = path.join(
    attemptRoot,
    "payload", "stages", "real.skill-generation", "gates", "real.agent.skill-generation",
  );
  const registrationId = "rpc-timeout-methods-v1";
  const skillName = "diagnose-rpc-timeout";
  const registrationRoot = path.join(generationGateRoot, "generated-skill", registrationId);
  const skillRoot = path.join(registrationRoot, "package", skillName);
  fs.mkdirSync(path.join(skillRoot, "references"), { recursive: true });
  fs.writeFileSync(path.join(skillRoot, "SKILL.md"), "# RPC timeout Methods\n", "utf8");
  fs.writeFileSync(path.join(skillRoot, "references", "methods.md"), "# Methods\n", "utf8");
  const sourceWikiSha256 = "eb39edf220d0eed91ae03eb712efd8974a5e5c82c3deed035c236a0d1bf28aab";
  const methods = {
    schema_version: 1,
    skill_name: skillName,
    source_wiki_sha256: sourceWikiSha256,
    required_user_inputs: ["problem_time", "client_process", "server_process", "service", "api"],
    required_artifacts: ["log_archive"],
    log_derived_fields: [
      "request_id", "client_send_us", "server_recv_us", "server_send_us", "client_now_us",
      "start_us", "end_us", "cost_us", "print_time_ms", "ordinal", "queue_us", "timeout_ms",
      "current_us", "request_us",
    ],
    methods: [
      {
        id: "api-execution-overrun",
        evidence_markers: ["LATE_RESPONSE service=", "API_COMPLETE service=", "DEADLOOP_DETECTED service="],
      },
      {
        id: "server-receive-queueing",
        evidence_markers: ["LATE_RESPONSE service=", "QUEUE_HISTORY print_time_ms="],
      },
      {
        id: "client-receive-blocked",
        evidence_markers: ["LATE_RESPONSE service="],
      },
    ],
  };
  fs.writeFileSync(path.join(skillRoot, "methods.json"), canonicalJson(methods), "utf8");
  const registrationPath = path.join(registrationRoot, "registration-template.json");
  fs.copyFileSync(
    path.join(REPO_ROOT, "tests", "cases", "release", "rpc-timeout-anonymized", "registration", registrationId, "registration-template.json"),
    registrationPath,
  );

  const packageIdentity = packageTreeIdentity(skillRoot);
  assert.equal(packageIdentity.status, "PRESENT");
  const packageEntries = packageIdentity.records
    .filter((entry) => entry.kind === "file")
    .map(({ path: entryPath, size, sha256 }) => ({ path: entryPath, size, sha256 }))
    .sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  const registrationSha256 = sha256File(registrationPath);
  const packageTreeSha256 = sha256Bytes(canonicalJson({ version: 1, entries: packageEntries }));
  const combinedSha256 = sha256Bytes(canonicalJson({
    schema_version: 1,
    registration_id: registrationId,
    registration_sha256: registrationSha256,
    package_tree_sha256: packageTreeSha256,
  }));
  const generatedSkill = {
    registration_id: registrationId,
    skill_name: skillName,
    registration_sha256: registrationSha256,
    package_tree_sha256: packageTreeSha256,
    combined_sha256: combinedSha256,
    source_wiki_sha256: sourceWikiSha256,
  };

  const stageRoot = path.join(attemptRoot, "payload", "stages", "journey.cross-job.diagnose");
  fs.mkdirSync(stageRoot, { recursive: true });
  const diagnosisJobId = "00000000-0000-4000-8000-000000000021";
  const caseId = "00000000-0000-4000-8000-000000000022";
  const skillRef = {
    id: `diagnosis-skill/${registrationId}`,
    version: "1.0.0",
    content_hash: combinedSha256,
  };
  const logparseReceiptBytes = jsonBytes({ schema_version: 1, status: "PASS" });
  const job = {
    job_id: diagnosisJobId,
    case_id: caseId,
    job_type: "DIAGNOSE",
    diagnosis_mode: "SPECIALIZED",
    logparse_product: "rpc-skill-feasibility",
    skill_ref: skillRef,
  };
  const jobBytes = jsonBytes(job);
  const audit = {
    schema_version: 1,
    registration_id: registrationId,
    registration_sha256: registrationSha256,
    package_tree_sha256: packageTreeSha256,
    combined_sha256: combinedSha256,
    logparse_receipt_sha256: sha256Bytes(logparseReceiptBytes),
    status: "CONFIRMED",
    confirmed_methods: ["api-execution-overrun", "client-receive-blocked"],
    evidence_count: 3,
    checked_source_count: 2,
    skill_load: {
      package_tree_sha256: packageTreeSha256,
      scanned_source_ids: ["client", "server"],
      marker_hits: [
        ["client", "LATE_RESPONSE", 1],
        ["server", "API_COMPLETE", 1],
        ["server", "API_COMPLETE", 2],
      ],
      loaded_method_ids: ["api-execution-overrun", "client-receive-blocked"],
    },
  };
  const expected = {
    diagnosis_job_id: diagnosisJobId,
    case_id: caseId,
    skill_ref: skillRef,
    logparse_product: "rpc-skill-feasibility",
    registration_id: registrationId,
    registration_sha256: registrationSha256,
    package_tree_sha256: packageTreeSha256,
    combined_sha256: combinedSha256,
    status: "CONFIRMED",
    confirmed_methods: ["api-execution-overrun", "client-receive-blocked"],
    known_method_ids: ["api-execution-overrun", "server-receive-queueing", "client-receive-blocked"],
    source_ids: ["client", "server"],
    evidence_count: 3,
  };
  const auditBytes = jsonBytes(audit);
  const summary = validateMethodsGroundingExecutionRecord({ jobBytes, auditBytes, logparseReceiptBytes, expected });
  fs.writeFileSync(path.join(stageRoot, "methods-diagnose-job.json"), jobBytes);
  fs.writeFileSync(path.join(stageRoot, "methods-grounding-audit.json"), auditBytes);
  fs.writeFileSync(path.join(stageRoot, "methods-logparse-receipt.json"), logparseReceiptBytes);
  const receipt = {
    methods_grounding: summary,
    invocations: [
      { job_type: "DIAGNOSE", job_id: diagnosisJobId },
      { job_type: "DIAGNOSE", job_id: diagnosisJobId },
      { job_type: "REVIEW", job_id: "00000000-0000-4000-8000-000000000023" },
    ],
  };
  return {
    attemptRoot,
    context: { attemptRoot, repoRoot: REPO_ROOT },
    generatedSkill,
    receipt,
    audit,
    expected,
    job,
    logparseReceiptBytes,
    methods,
    methodsPath: path.join(skillRoot, "methods.json"),
    stageRoot,
  };
}

test("CrossJob Methods consumer re-derives method IDs from the generated package and rejects coherent unknown-method tampering", () => {
  const fixture = materializeMethodsConsumerFixture();
  try {
    assert.equal(validMethodsGroundingOracleEvidence(fixture.context, fixture.receipt, fixture.generatedSkill), true);
    const ambiguousJob = clone(fixture.receipt);
    ambiguousJob.invocations[1].job_id = "00000000-0000-4000-8000-000000000024";
    assert.equal(validMethodsGroundingOracleEvidence(fixture.context, ambiguousJob, fixture.generatedSkill), false);

    const tamperedAudit = clone(fixture.audit);
    tamperedAudit.confirmed_methods = ["unknown-method"];
    tamperedAudit.skill_load.loaded_method_ids = ["unknown-method"];
    const tamperedAuditBytes = jsonBytes(tamperedAudit);
    fs.writeFileSync(path.join(fixture.stageRoot, "methods-grounding-audit.json"), tamperedAuditBytes);
    const tamperedReceipt = clone(fixture.receipt);
    tamperedReceipt.methods_grounding.confirmed_methods = ["unknown-method"];
    tamperedReceipt.methods_grounding.audit_sha256 = sha256Bytes(tamperedAuditBytes);
    assert.equal(validMethodsGroundingOracleEvidence(fixture.context, tamperedReceipt, fixture.generatedSkill), false);
  } finally {
    fs.rmSync(fixture.attemptRoot, { recursive: true, force: true });
  }
});

function coherentlyRebindGeneratedMethods(fixture, changedMethods) {
  fs.writeFileSync(fixture.methodsPath, jsonBytes(changedMethods));
  const skillRoot = path.dirname(fixture.methodsPath);
  const packageIdentity = packageTreeIdentity(skillRoot);
  assert.equal(packageIdentity.status, "PRESENT");
  const packageEntries = packageIdentity.records
    .filter((entry) => entry.kind === "file")
    .map(({ path: entryPath, size, sha256 }) => ({ path: entryPath, size, sha256 }))
    .sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  const packageTreeSha256 = sha256Bytes(canonicalJson({ version: 1, entries: packageEntries }));
  const combinedSha256 = sha256Bytes(canonicalJson({
    schema_version: 1,
    registration_id: fixture.generatedSkill.registration_id,
    registration_sha256: fixture.generatedSkill.registration_sha256,
    package_tree_sha256: packageTreeSha256,
  }));
  const generatedSkill = {
    ...fixture.generatedSkill,
    package_tree_sha256: packageTreeSha256,
    combined_sha256: combinedSha256,
  };
  const job = clone(fixture.job);
  job.skill_ref.content_hash = combinedSha256;
  const audit = clone(fixture.audit);
  audit.package_tree_sha256 = packageTreeSha256;
  audit.combined_sha256 = combinedSha256;
  audit.skill_load.package_tree_sha256 = packageTreeSha256;
  const expected = {
    ...fixture.expected,
    skill_ref: job.skill_ref,
    package_tree_sha256: packageTreeSha256,
    combined_sha256: combinedSha256,
  };
  const jobBytes = jsonBytes(job);
  const auditBytes = jsonBytes(audit);
  const summary = validateMethodsGroundingExecutionRecord({
    jobBytes,
    auditBytes,
    logparseReceiptBytes: fixture.logparseReceiptBytes,
    expected,
  });
  fs.writeFileSync(path.join(fixture.stageRoot, "methods-diagnose-job.json"), jobBytes);
  fs.writeFileSync(path.join(fixture.stageRoot, "methods-grounding-audit.json"), auditBytes);
  const receipt = { ...fixture.receipt, methods_grounding: summary };
  return { generatedSkill, receipt };
}

test("CrossJob Methods consumer rejects a coherently rebound ordered-field mutation", () => {
  const reordered = materializeMethodsConsumerFixture();
  try {
    const changedMethods = clone(reordered.methods);
    changedMethods.required_user_inputs.reverse();
    const changed = coherentlyRebindGeneratedMethods(reordered, changedMethods);
    assert.equal(validMethodsGroundingOracleEvidence(reordered.context, changed.receipt, changed.generatedSkill), false);
  } finally {
    fs.rmSync(reordered.attemptRoot, { recursive: true, force: true });
  }

});

function releaseReportFixture() {
  const first = "API_COMPLETE service=svc_orders api=Reserve start_us=10000000 end_us=16500000";
  const second = "API_COMPLETE service=svc_orders api=Reserve start_us=20000000 end_us=26800000";
  const report = {
    schema_version: 3,
    status: "PARTIAL",
    causal_factors: [{ factor_id: "api_overrun", required_rule_ids: ["event-1", "event-2"] }],
    candidate_factors: [{ factor_id: "queue_delay", required_rule_ids: ["candidate-queue"] }],
    excluded_factors: [],
    verification_rules: [
      { rule_id: "event-1", citations: [{ excerpt: first }] },
      { rule_id: "event-2", citations: [{ excerpt: second }] },
      { rule_id: "candidate-queue", citations: [] },
    ],
    completion_criteria_mapping: [
      { criterion_index: 0, criterion: "split events", status: "PARTIALLY_SATISFIED" },
      { criterion_index: 1, criterion: "retain gaps", status: "UNKNOWN" },
    ],
    recommendations: [],
    safety_notes: ["RPC 超时不等于取消。"],
  };
  const expectation = {
    report_status: "PARTIAL",
    resolution_status: "PARTIAL",
    causal_factor_ids: ["api_overrun"],
    candidate_factor_ids: ["queue_delay"],
    excluded_factor_ids: [],
    required_evidence_identities: [
      { factor_id: "api_overrun", marker: "API_COMPLETE", identity_tokens: ["start_us=10000000", "end_us=16500000"] },
      { factor_id: "api_overrun", marker: "API_COMPLETE", identity_tokens: ["start_us=20000000", "end_us=26800000"] },
    ],
    forbidden_evidence_terms: ["ORACLE_FORBIDDEN"],
  };
  return {
    report,
    expectation,
    completionCriteria: ["split events", "retain gaps"],
    requiredSafetyPhrases: ["超时不等于取消"],
  };
}

test("CrossJob report oracle requires safety_notes placement and one verification rule per same-method event", () => {
  const fixture = releaseReportFixture();
  assert.equal(validateReleaseDiagnosisReport(fixture), true);

  const misplacedSafety = clone(fixture);
  misplacedSafety.report.recommendations = ["RPC 超时不等于取消。"];
  misplacedSafety.report.safety_notes = ["Only the fixed scope was inspected."];
  assert.throws(
    () => validateReleaseDiagnosisReport(misplacedSafety),
    (error) => error.code === "RESTART_RESULT_SAFETY_NOTES",
  );

  const mergedEvents = clone(fixture);
  mergedEvents.report.causal_factors[0].required_rule_ids = ["merged-event"];
  mergedEvents.report.verification_rules = [{
    rule_id: "merged-event",
    citations: [{ excerpt: `${fixture.report.verification_rules[0].citations[0].excerpt} ${fixture.report.verification_rules[1].citations[0].excerpt}` }],
  }];
  assert.throws(
    () => validateReleaseDiagnosisReport(mergedEvents),
    (error) => error.code === "RELEASE_RESULT_EVIDENCE_EVENT_COUNT",
  );
});

function nativePassBoundary() {
  const runId = "run-native-runtime-boundary";
  const serverImageId = `sha256:${"a".repeat(64)}`;
  const sha = "c".repeat(64);
  const generatedSkill = {
    registration_id: "rpc-timeout-methods-v1",
    skill_name: "diagnose-rpc-timeout",
    registration_sha256: sha,
    package_tree_sha256: sha,
    combined_sha256: sha,
    source_wiki_sha256: sha,
    generation_receipt_sha256: sha,
  };
  const plan = {
    run_id: runId,
    release_inputs: {
      topology: "host-client-to-linux-server",
      image: { server: { image_id: serverImageId }, client: null },
    },
  };
  const receipt = {
    status: "PASS",
    stage_id: "journey.cross-job.route",
    topology: "host-client",
    runtime_images: { server_image_id: serverImageId, client_image_id: null },
    runtime_resources: {
      client_container: null,
      server_container: resourceName("pltf-server", runId, "initial"),
      client_image_id: null,
      server_image_id: serverImageId,
      network: null,
      selected_client_runtime: null,
    },
    generated_skill: {
      registration_id: generatedSkill.registration_id,
      skill_name: generatedSkill.skill_name,
      tree_digest: sha,
      package_digest: sha,
      registration_sha256: sha,
      package_tree_sha256: sha,
      combined_sha256: sha,
      content_tree_sha256: sha,
      generation_receipt_sha256: sha,
      source_wiki_sha256: sha,
    },
  };
  return { plan, receipt, generatedSkill };
}

test("native CrossJob PASS receipt binds the planned server image and exact active container", () => {
  const passing = nativePassBoundary();
  assert.equal(validCrossJobPassRuntimeBoundary(passing.receipt, passing), true);
  const restarted = clone(passing);
  restarted.receipt.stage_id = "journey.cross-job.publish-restart";
  restarted.receipt.runtime_resources.server_container = resourceName("pltf-server", restarted.plan.run_id, "restart");
  assert.equal(validCrossJobPassRuntimeBoundary(restarted.receipt, restarted), true);
  restarted.receipt.runtime_resources.server_container = resourceName("pltf-server", restarted.plan.run_id, "initial");
  assert.equal(validCrossJobPassRuntimeBoundary(restarted.receipt, restarted), false);

  const mutations = [
    (value) => { value.receipt.runtime_images.server_image_id = `sha256:${"d".repeat(64)}`; },
    (value) => { value.receipt.runtime_resources.server_image_id = `sha256:${"d".repeat(64)}`; },
    (value) => { value.receipt.runtime_resources.server_container = "pltf-server-valid-looking-but-wrong-initial"; },
    (value) => { value.receipt.runtime_resources.client_container = "unexpected-client"; },
    (value) => { value.receipt.runtime_resources.network = "unexpected-network"; },
  ];
  for (const mutate of mutations) {
    const changed = clone(passing);
    mutate(changed);
    assert.equal(validCrossJobPassRuntimeBoundary(changed.receipt, changed), false);
  }
});

function dockerIdentity() {
  return {
    status: "PRESENT",
    context: "colima",
    effective_context: "colima",
    server_id: "daemon-a",
    os: "linux",
    architecture: "amd64",
    version: "29.7.2",
    context_fingerprint: "e".repeat(64),
    docker_cli_sha256: "f".repeat(64),
  };
}

test("Docker-backed action boundary preserves exact identity and converts every post-run mutation to infrastructure BLOCKED", () => {
  const planned = dockerIdentity();
  assert.equal(dockerRuntimeBoundaryResult(planned, clone(planned)), null);
  for (const field of [
    "context", "effective_context", "server_id", "os", "architecture", "version",
    "context_fingerprint", "docker_cli_sha256",
  ]) {
    const observed = clone(planned);
    observed[field] = field.endsWith("sha256") || field === "context_fingerprint" ? "0".repeat(64) : `${observed[field]}-drift`;
    const result = dockerRuntimeBoundaryResult(planned, observed, {
      status: "PASS",
      elapsed_seconds: 9,
      stdout_path: "payload/process.stdout",
    });
    assert.deepEqual(result, {
      status: "BLOCKED",
      elapsed_seconds: 9,
      stdout_path: "payload/process.stdout",
      failure_domain: "INFRA",
      code: "DOCKER_RUNTIME_IDENTITY_DRIFT",
    });
  }
});

test("every Docker-backed action re-probes identity after its process and before reading PASS evidence", () => {
  const source = fs.readFileSync(new URL("../lib/actions.mjs", import.meta.url), "utf8");
  const sections = [
    ["async function hostCapability", "async function serverLinuxCapability", "if (result.status"],
    ["async function serverLinuxCapability", "function publicExternalGitIdentity", "let receipt"],
    ["async function crossJob", "function reviewObservation", "const receiptPath"],
  ];
  for (const [startMarker, endMarker, evidenceMarker] of sections) {
    const start = source.indexOf(startMarker);
    const end = source.indexOf(endMarker, start + startMarker.length);
    assert.ok(start >= 0 && end > start, `${startMarker} section missing`);
    const section = source.slice(start, end);
    const process = section.indexOf("await runProcess({");
    const postProbe = section.indexOf("const postRunDockerBoundary = probeDockerRuntimeBoundary(context, result);");
    const evidence = section.indexOf(evidenceMarker);
    assert.ok(process >= 0 && postProbe > process && evidence > postProbe, `${startMarker} must re-probe before evidence`);
  }
});
