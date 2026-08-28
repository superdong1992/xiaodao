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
  METHODS_V2_CAPTURED_FILES,
  validateMethodsV2ExecutionRecords,
  validateMethodsV2RestartSnapshot,
} from "../lib/methods-oracle.mjs";
import {
  crossJobBrowserCapabilityPolicy,
  crossJobBrowserFailureContract,
  dockerRuntimeBoundaryResult,
  validCrossJobPassRuntimeBoundary,
  validCrossJobBrowserFailureBinding,
  validLinuxClientBrowserFailureReceipt,
  validMethodsV2OracleEvidence,
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

function v2Ref(prefix, kind, value) {
  return `${prefix}-${sha256Bytes(canonicalJson({ kind, ...value }))}`;
}

function v2StateRef(state) {
  return v2Ref("state", "method-state-v2", {
    case_id: state.case_id,
    source_job_id: state.source_job_id,
    evaluation_id: state.evaluation_id,
    plan_ref: state.plan_ref,
    evaluation_refs: state.evaluation_refs,
    status: state.status,
    current_role: state.current_role,
    specialist_protocol_failures: state.specialist_protocol_failures,
    reviewer_protocol_failures: state.reviewer_protocol_failures,
    specialist_evaluation: state.specialist_evaluation,
    reviewer_evaluation: state.reviewer_evaluation,
    consensus: state.consensus,
    reason_code: state.reason_code,
    diagnostic_id: state.diagnostic_id,
    diagnostic_evaluation_ref: state.diagnostic_evaluation_ref,
    reasons: state.reasons,
  });
}

function methodsV2ContractFixture() {
  const caseId = "00000000-0000-4000-8000-000000000101";
  const sourceJobId = "00000000-0000-4000-8000-000000000102";
  const reviewerJobId = "00000000-0000-4000-8000-000000000103";
  const evaluationId = "00000000-0000-4000-8000-000000000104";
  const skillSha256 = "a".repeat(64);
  const skillRef = { id: "diagnosis-skill/methods-v2", version: "2.0.0", content_hash: skillSha256 };
  const methodCards = [
    { id: "first-method", priority: 1, evidence_markers: ["shared marker"] },
    { id: "second-method", priority: 2, evidence_markers: ["shared marker"] },
  ];
  const source = {
    source_id: "server",
    relative_path: "inputs/target-logs/server.log",
    content_sha256: "b".repeat(64),
  };
  source.source_ref = v2Ref("source", "method-evidence-source-v2", source);
  const line = "SHARED MARKER request_id=42";
  const hits = methodCards.map((method) => {
    const value = {
      method_id: method.id,
      method_priority: method.priority,
      marker_index: 1,
      source_ref: source.source_ref,
      source_id: source.source_id,
      line_number: 1,
      marker: "shared marker",
      line,
    };
    return { hit_ref: v2Ref("hit", "method-evidence-hit-v2", value), ...value };
  });
  const events = hits.map((hit) => {
    const value = {
      method_id: hit.method_id,
      method_priority: hit.method_priority,
      identity_tokens: ["request_id=42"],
      evidence_hit_refs: [hit.hit_ref],
    };
    return { event_ref: v2Ref("event", "method-evidence-event-v2", value), ...value };
  });
  const graphValue = {
    skill_sha256: skillSha256,
    source_refs: [source.source_ref],
    hit_refs: hits.map((item) => item.hit_ref),
    event_refs: events.map((item) => item.event_ref),
    loaded_method_ids: methodCards.map((item) => item.id),
    limitations: ["Only the frozen target was evaluated."],
  };
  const graph = {
    events,
    graph_ref: v2Ref("graph", "method-evidence-graph-v2", graphValue),
    hits,
    limitations: graphValue.limitations,
    loaded_method_ids: graphValue.loaded_method_ids,
    skill_sha256: skillSha256,
    sources: [source],
  };
  const evaluations = methodCards.map((method, index) => {
    const value = {
      method_id: method.id,
      method_priority: method.priority,
      evidence_event_refs: [events[index].event_ref],
      evidence_hit_refs: [hits[index].hit_ref],
    };
    return { evaluation_ref: v2Ref("eval", "method-evaluation-v2", value), ...value };
  });
  const planValue = {
    skill_sha256: skillSha256,
    evidence_graph_ref: graph.graph_ref,
    evaluation_refs: evaluations.map((item) => item.evaluation_ref),
  };
  const plan = {
    evaluations,
    evidence_graph_ref: graph.graph_ref,
    plan_ref: v2Ref("plan", "method-evaluation-plan-v2", planValue),
    skill_sha256: skillSha256,
  };
  const limitationsValue = {
    case_id: caseId,
    source_job_id: sourceJobId,
    evidence_graph_ref: graph.graph_ref,
    plan_ref: plan.plan_ref,
    limitations: graph.limitations,
  };
  const limitations = {
    ...limitationsValue,
    record_ref: v2Ref("limitations", "method-limitations-record-v2", limitationsValue),
    schema_version: 2,
  };
  const roleItems = (prefix) => evaluations.map((item, index) => ({
    evaluation_ref: item.evaluation_ref,
    reason: `${prefix} reason ${index + 1}`,
    verdict: index === 0 ? "CONFIRMED" : "REJECTED",
  }));
  const specialist = { evaluations: roleItems("specialist"), plan_ref: plan.plan_ref, repair_used: false, role: "SPECIALIST" };
  const reviewer = { evaluations: roleItems("reviewer"), plan_ref: plan.plan_ref, repair_used: false, role: "REVIEWER" };
  const sourceState = {
    case_id: caseId,
    consensus: null,
    current_role: "REVIEWER",
    diagnostic_evaluation_ref: null,
    diagnostic_id: null,
    evaluation_id: evaluationId,
    evaluation_refs: evaluations.map((item) => item.evaluation_ref),
    plan_ref: plan.plan_ref,
    reason_code: null,
    reasons: [],
    reviewer_evaluation: null,
    reviewer_protocol_failures: 0,
    source_job_id: sourceJobId,
    specialist_evaluation: specialist,
    specialist_protocol_failures: 0,
    status: "REVIEWER_PENDING",
  };
  sourceState.state_ref = v2StateRef(sourceState);
  const confirmedEvaluationRefs = [evaluations[0].evaluation_ref];
  const diagnosticId = v2Ref("diag", "method-diagnostic-v2", {
    case_id: caseId,
    source_job_id: sourceJobId,
    evaluation_id: evaluationId,
    plan_ref: plan.plan_ref,
    status: "RESOLVED",
    reason_code: null,
    evaluation_ref: null,
  });
  const terminalState = {
    ...sourceState,
    consensus: {
      confirmed_evaluation_refs: confirmedEvaluationRefs,
      confirmed_method_ids: [methodCards[0].id],
      plan_ref: plan.plan_ref,
      status: "RESOLVED",
    },
    current_role: null,
    diagnostic_id: diagnosticId,
    reviewer_evaluation: reviewer,
    status: "RESOLVED",
  };
  terminalState.state_ref = v2StateRef(terminalState);
  const target = {
    evaluation_id: evaluationId,
    graph_ref: graph.graph_ref,
    plan_ref: plan.plan_ref,
    reviewed_state_revision: 1,
    schema_version: 2,
    skill_ref: skillRef,
    source_job_id: sourceJobId,
  };
  const sourceJob = { case_id: caseId, diagnosis_mode: "SPECIALIZED", job_id: sourceJobId, job_type: "DIAGNOSE", skill_ref: skillRef };
  const reviewerJob = {
    case_id: caseId,
    context_snapshot: { candidate_conclusion: null },
    job_id: reviewerJobId,
    job_type: "REVIEW",
    methods_review_target: target,
    review_target: null,
    skill_ref: skillRef,
  };
  const sourceOutcome = {
    case_id: caseId,
    consumed_evidence_refs: [],
    decision_audit: null,
    error: null,
    job_id: sourceJobId,
    job_type: "DIAGNOSE",
    methods_review_target: target,
    payload: null,
    proposed_artifacts: [],
    proposed_evidence: [],
    result_type: "COMPLETED",
  };
  const confirmedEventRefs = [events[0].event_ref];
  const confirmedHitRefs = [hits[0].hit_ref];
  const resultEvaluations = [{
    evaluation_ref: evaluations[0].evaluation_ref,
    method_id: evaluations[0].method_id,
    evidence_event_refs: evaluations[0].evidence_event_refs,
    evidence_hit_refs: evaluations[0].evidence_hit_refs,
    verdict: "CONFIRMED",
  }];
  const resultRef = v2Ref("result", "method-terminal-result-v2", {
    case_id: caseId,
    source_job_id: sourceJobId,
    terminal_job_id: reviewerJobId,
    evaluation_id: evaluationId,
    status: "RESOLVED",
    plan_ref: plan.plan_ref,
    evidence_graph_ref: graph.graph_ref,
    reason_code: null,
    diagnostic_id: diagnosticId,
    diagnostic_evaluation_ref: null,
    evaluations: resultEvaluations,
    confirmed_evaluation_refs: confirmedEvaluationRefs,
    confirmed_method_ids: [methodCards[0].id],
    confirmed_event_refs: confirmedEventRefs,
    confirmed_hit_refs: confirmedHitRefs,
    limitations: graph.limitations,
    reasons: [],
  });
  const publicMethodsResult = {
    case_id: caseId,
    confirmed_evaluation_refs: confirmedEvaluationRefs,
    confirmed_event_refs: confirmedEventRefs,
    confirmed_hit_refs: confirmedHitRefs,
    confirmed_method_ids: [methodCards[0].id],
    diagnostic_evaluation_ref: null,
    diagnostic_id: diagnosticId,
    evaluation_id: evaluationId,
    evidence_graph_ref: graph.graph_ref,
    limitations: graph.limitations,
    plan_ref: plan.plan_ref,
    reason_code: null,
    reasons: [],
    result_ref: resultRef,
    schema_version: 2,
    source_job_id: reviewerJobId,
    status: "RESOLVED",
  };
  const reviewerOutcome = {
    case_id: caseId,
    consumed_evidence_refs: [],
    decision_audit: null,
    error: null,
    job_id: reviewerJobId,
    job_type: "REVIEW",
    methods_reviewer_result: {
      evaluations: reviewer.evaluations,
      repair_used: false,
      review_job_id: reviewerJobId,
      role: "REVIEWER",
      schema_version: 2,
      target,
    },
    methods_terminal_projection: publicMethodsResult,
    payload: null,
    proposed_artifacts: [],
    proposed_evidence: [],
    result_type: "COMPLETED",
  };
  const values = {
    source_job: sourceJob,
    reviewer_job: reviewerJob,
    evidence_graph: graph,
    evaluation_plan: plan,
    limitations,
    source_state: sourceState,
    source_outcome: sourceOutcome,
    terminal_state: terminalState,
    reviewer_outcome: reviewerOutcome,
  };
  return {
    files: Object.fromEntries(Object.entries(values).map(([key, value]) => [key, jsonBytes(value)])),
    expected: {
      source_job_id: sourceJobId,
      reviewer_job_id: reviewerJobId,
      case_id: caseId,
      skill_ref: skillRef,
      source_ids: ["server"],
      method_cards: methodCards,
      loaded_method_ids: methodCards.map((item) => item.id),
      confirmed_method_ids: [methodCards[0].id],
      required_evidence_identities: [{ method_id: methodCards[0].id, marker: "shared marker", identity_tokens: ["request_id=42"] }],
    },
    invocations: [
      { effective_model: "same-model", job_id: sourceJobId, job_type: "DIAGNOSE" },
      { effective_model: "same-model", job_id: reviewerJobId, job_type: "REVIEW" },
    ],
    publicMethodsResult,
  };
}

test("CrossJob Evidence V2 oracle verifies method-qualified Graph, complete Plan, blind consensus, and zero-artifact restart", () => {
  const fixture = methodsV2ContractFixture();
  const summary = validateMethodsV2ExecutionRecords(fixture);
  assert.equal(summary.status, "PASS");
  assert.equal(summary.evidence_hit_count, 2);
  assert.equal(summary.evaluation_count, 2);
  assert.deepEqual(summary.confirmed_method_ids, ["first-method"]);
  assert.equal(summary.service_model_calls, 2);
  assert.deepEqual(Object.keys(summary.record_sha256).sort(), Object.keys(METHODS_V2_CAPTURED_FILES).sort());
  const caseView = {
    case_id: summary.case_id,
    status: "RESOLVED",
    final_result: null,
    unresolved_result: null,
    generic_result: null,
    generic_result_v2: null,
    methods_result: fixture.publicMethodsResult,
    artifacts: [],
  };
  assert.equal(validateMethodsV2RestartSnapshot({
    caseView,
    artifacts: [],
    methodsSummary: summary,
    restartedFiles: fixture.files,
  }), true);
});

test("CrossJob Evidence V2 oracle rejects one-field shared-literal, role-output, and restart mutations", () => {
  const fixture = methodsV2ContractFixture();
  const missingQualifiedHit = { ...fixture, files: { ...fixture.files } };
  const graph = JSON.parse(missingQualifiedHit.files.evidence_graph);
  graph.hits.splice(1, 1);
  missingQualifiedHit.files.evidence_graph = jsonBytes(graph);
  assert.throws(
    () => validateMethodsV2ExecutionRecords(missingQualifiedHit),
    (error) => error.code === "METHODS_V2_GRAPH_METHOD_QUALIFICATION",
  );

  const extraRoleField = { ...fixture, files: { ...fixture.files } };
  const terminal = JSON.parse(extraRoleField.files.terminal_state);
  terminal.reviewer_evaluation.evaluations[0].marker = "shared marker";
  extraRoleField.files.terminal_state = jsonBytes(terminal);
  assert.throws(() => validateMethodsV2ExecutionRecords(extraRoleField));

  const summary = validateMethodsV2ExecutionRecords(fixture);
  const changedCase = {
    case_id: summary.case_id,
    status: "RESOLVED",
    final_result: null,
    unresolved_result: null,
    generic_result: null,
    generic_result_v2: null,
    methods_result: { ...fixture.publicMethodsResult, result_ref: `result-${"f".repeat(64)}` },
    artifacts: [],
  };
  assert.throws(
    () => validateMethodsV2RestartSnapshot({ caseView: changedCase, artifacts: [], methodsSummary: summary, restartedFiles: fixture.files }),
    (error) => error.code === "METHODS_V2_RESTART_CASE_MISMATCH",
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
