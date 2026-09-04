import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  assertPhaseOneCaseFirst,
  buildLinuxClientBrowserFailureReceipt,
  installGeneratedSkill,
  linuxClientUserIdentity,
  parseClaudeStream,
  parseLinuxClientBrowserExecution,
  phaseOnePrompt,
  phaseOneUserMessage,
  phaseThreePrompt,
  phaseTwoPrompt,
  phaseTwoUserMessage,
  restartPrompt,
  runCommandCapture,
  validDirectMethodsServiceInvocations,
  validRouteMethodsPreflightEvidence,
  validLinuxClientBrowserExecution,
  validServerRuntimeInspection,
  validServiceAgentUsageReceipt,
  validSuccessfulInvocationReceipt,
  validatePhaseOne,
  validatePhaseThree,
  validatePhaseTwo,
  validateRestart,
} from "../adapters/cross-job-core.mjs";
import {
  crossJobBrowserCapabilityPolicy,
  crossJobBrowserFailureContract,
  dockerRuntimeBoundaryResult,
  validCrossJobPassRuntimeBoundary,
  validCrossJobBrowserFailureBinding,
  validLinuxClientBrowserFailureReceipt,
  validMethodsV2OracleEvidence,
} from "../lib/actions.mjs";
import { packageTreeIdentity, RELEASE_MODEL } from "../lib/release-inputs.mjs";
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

const CLIENT_MCP_TOOLS = [
  "problem_locator_create_case",
  "problem_locator_prepare_attachment",
  "problem_locator_submit_supplement",
  "problem_locator_get_case",
  "problem_locator_resume_case",
  "problem_locator_cancel_case",
  "problem_locator_list_artifacts",
];

function caseFirstStream({ questionBeforeCreate = null } = {}) {
  const cwd = process.cwd();
  const events = [
    {
      type: "system",
      subtype: "init",
      cwd,
      model: RELEASE_MODEL,
      permissionMode: "dontAsk",
      tools: ["Skill", ...CLIENT_MCP_TOOLS.map((name) => `mcp__problem-locator__${name}`)],
      mcp_servers: [{ name: "problem-locator", status: "connected" }],
    },
    {
      type: "assistant",
      message: {
        role: "assistant",
        content: [{ type: "tool_use", id: "skill-1", name: "Skill", input: { skill: "problem-locator-client" } }],
      },
    },
    {
      type: "user",
      message: { role: "user", content: [{ type: "tool_result", tool_use_id: "skill-1", content: "loaded" }] },
    },
  ];
  if (questionBeforeCreate !== null) {
    events.push({
      type: "assistant",
      message: { role: "assistant", content: [{ type: "text", text: questionBeforeCreate }] },
    });
  }
  events.push(
    {
      type: "assistant",
      message: {
        role: "assistant",
        content: [{ type: "tool_use", id: "create-1", name: "mcp__problem-locator__problem_locator_create_case", input: { request_id: "case-first-request" } }],
      },
    },
    {
      type: "user",
      tool_use_result: { structuredContent: { ok: true, error: null, data: {} } },
      message: { role: "user", content: [{ type: "tool_result", tool_use_id: "create-1", content: "created" }] },
    },
    {
      type: "result",
      subtype: "success",
      is_error: false,
      result: "",
      num_turns: 2,
      total_cost_usd: 0,
      usage: {
        input_tokens: 1,
        output_tokens: 1,
        cache_creation_input_tokens: 0,
        cache_read_input_tokens: 0,
      },
    },
  );
  return { cwd, text: events.map((event) => JSON.stringify(event)).join("\n") };
}

function phaseOnePromptFixture() {
  return {
    releaseCase: {
      driver: {
        problem: {
          raw_problem_text: "不应进入首轮消息：raw_problem_text",
          expected_behavior: "不应进入首轮消息：expected_behavior",
          actual_behavior: "不应进入首轮消息：actual_behavior",
          scope: "不应进入首轮消息：scope",
          goals: ["不应进入首轮消息：goals"],
          non_goals: ["不应进入首轮消息：non_goals"],
          constraints: ["不应进入首轮消息：constraints"],
          completion_criteria: ["不应进入首轮消息：completion_criteria"],
        },
        initial_user_fact_names: ["problem_time", "client_process", "server_process", "service", "api"],
        initial_user_fact_values: ["2026-08-23 10:00:05", "rpc_client", "rpc_server", "svc_orders", "Reserve"],
      },
      skill: {
        attachment_requirement: "log_archive",
        runtime_ref_id: "diagnosis-skill/rpc-timeout-methods-v1",
        version: "1.0.0",
      },
    },
    archive: {
      name: "logs.zip",
      content_type: "application/zip",
      size: 1234,
      sha256: "a".repeat(64),
    },
  };
}

test("Phase 1 accepts a direct Case creation with no preceding assistant prose", () => {
  const stream = caseFirstStream();
  const audit = parseClaudeStream(stream.text, stream.cwd);
  assert.equal(assertPhaseOneCaseFirst(audit), true);
  assert.deepEqual(audit.assistant_text_events, []);
  assert.equal(audit.records[0].tool_name, "problem_locator_create_case");
});

test("Phase 1 rejects asking the user before creating the Case", () => {
  const stream = caseFirstStream({ questionBeforeCreate: "请先补充问题时间和日志。" });
  const audit = parseClaudeStream(stream.text, stream.cwd);
  assert.equal(audit.assistant_text_events[0].text, "请先补充问题时间和日志。");
  assert.throws(
    () => assertPhaseOneCaseFirst(audit),
    (error) => error.code === "PHASE1_PROSE_BEFORE_CREATE",
  );
});

test("the two Client prompts keep the sparse intake separate from the user's natural supplement", () => {
  const fixture = phaseOnePromptFixture();
  const firstPrompt = phaseOnePrompt();
  const secondPrompt = phaseTwoPrompt(
    { case_id: "00000000-0000-4000-8000-000000000101" },
    fixture.releaseCase,
    fixture.archive,
  );
  assert.match(firstPrompt, /第一步请先加载 problem-locator-client Skill/u);
  assert.match(secondPrompt, /第一步请先加载 problem-locator-client Skill/u);
  assert.equal(phaseOneUserMessage(), "订单 RPC 偶发超时，请定位原因；我有一份日志，可以在需要时提供。");
  assert.match(secondPrompt, /问题时间：2026-08-23 10:00:05/u);
  assert.match(secondPrompt, /客户端进程：rpc_client/u);
  assert.match(secondPrompt, /服务端进程：rpc_server/u);
  assert.match(secondPrompt, /服务名：svc_orders/u);
  assert.match(secondPrompt, /API 名：Reserve/u);
  assert.match(secondPrompt, /logs\.zip/u);
  assert.match(secondPrompt, /拿到地址后先暂停/u);
  assert.equal(firstPrompt.includes("2026-08-23 10:00:05"), false);
  assert.equal(firstPrompt.includes("rpc_client"), false);
  assert.equal(firstPrompt.includes("logs.zip"), false);
  assert.equal(phaseTwoUserMessage(fixture.releaseCase, fixture.archive).includes("problem_time"), false);
  for (const prompt of [firstPrompt, secondPrompt]) {
    assert.doesNotMatch(prompt, /problem_locator_(?:create_case|get_case|prepare_attachment|submit_supplement)/u);
    assert.doesNotMatch(prompt, /(?:request_id|wait_seconds|expected_case_revision|initial_user_fact_names|WAITING_INPUT|WAITING_ATTACHMENT)/u);
    assert.doesNotMatch(prompt, /(?:problem_time|client_process|server_process|initial_user_fact_names|initial_user_fact_values)/u);
    assert.doesNotMatch(prompt, /(?:正常情况|实际情况|影响范围|我希望确认|不需要处理|限制条件|完成标准|后面不用问)/u);
    assert.doesNotMatch(prompt, /(?:expected_behavior|actual_behavior|scope|goals|non_goals|constraints|completion_criteria)/u);
    for (const value of Object.values(fixture.releaseCase.driver.problem)) {
      for (const item of Array.isArray(value) ? value : [value]) assert.equal(prompt.includes(item), false);
    }
  }
});

function twoTurnClientFixture() {
  const fixture = phaseOnePromptFixture();
  const caseId = "00000000-0000-4000-8000-000000000101";
  const routeJobId = "00000000-0000-4000-8000-000000000102";
  const attachmentId = "00000000-0000-4000-8000-000000000103";
  const publicBaseUrl = "http://127.0.0.1:8080";
  const createRequestId = "generated-create";
  const prepareRequestId = "generated-prepare";
  const rawProblemText = phaseOneUserMessage();
  const inputRequirements = fixture.releaseCase.driver.initial_user_fact_names.map((name) => ({
    kind: "INPUT",
    name,
    prompt: `请补充${name}。`,
    status: "OPEN",
    requested_by_job_id: routeJobId,
  }));
  const attachmentRequirement = {
    kind: "ATTACHMENT",
    name: "log_archive",
    prompt: "请提供日志。",
    status: "OPEN",
    requested_by_job_id: routeJobId,
  };
  const success = (data) => ({ ok: true, error: null, data });
  const phaseOneRecords = [
    {
      ordinal: 0,
      stream_ordinal: 2,
      result_stream_ordinal: 3,
      tool_name: "problem_locator_create_case",
      input: {
        request_id: createRequestId,
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
      },
      result: success({ business_receipt: { case_id: caseId }, case_view: null }),
    },
    {
      ordinal: 1,
      stream_ordinal: 4,
      result_stream_ordinal: 5,
      tool_name: "problem_locator_get_case",
      input: { case_id: caseId, wait_for_job_id: null, wait_seconds: 30 },
      result: success({
        case_view: {
          case_id: caseId,
          case_revision: 2,
          status: "WAITING_INPUT",
          pending_requirements: [...inputRequirements, attachmentRequirement],
          selected_skill_ref: {
            id: fixture.releaseCase.skill.runtime_ref_id,
            version: fixture.releaseCase.skill.version,
          },
        },
      }),
    },
  ];
  const phaseTwoRecords = [
    {
      ordinal: 0,
      stream_ordinal: 2,
      result_stream_ordinal: 3,
      tool_name: "problem_locator_get_case",
      input: { case_id: caseId, wait_for_job_id: null, wait_seconds: 30 },
      result: success({
        case_view: {
          case_id: caseId,
          case_revision: 2,
          status: "WAITING_INPUT",
          pending_requirements: [...inputRequirements, attachmentRequirement],
          selected_skill_ref: {
            id: fixture.releaseCase.skill.runtime_ref_id,
            version: fixture.releaseCase.skill.version,
          },
        },
      }),
    },
    {
      ordinal: 1,
      stream_ordinal: 4,
      result_stream_ordinal: 5,
      tool_name: "problem_locator_prepare_attachment",
      input: {
        request_id: prepareRequestId,
        case_id: caseId,
        expected_case_revision: 2,
        name: fixture.archive.name,
        content_type: fixture.archive.content_type,
        declared_size: fixture.archive.size,
        declared_sha256: fixture.archive.sha256,
      },
      result: success({
        application_response: {
          case_view: {
            case_id: caseId,
            case_revision: 3,
            status: "WAITING_INPUT",
            pending_requirements: [...inputRequirements, attachmentRequirement],
            selected_skill_ref: {
              id: fixture.releaseCase.skill.runtime_ref_id,
              version: fixture.releaseCase.skill.version,
            },
          },
        },
        upload: {
          attachment_id: attachmentId,
          method: "PUT",
          url: `${publicBaseUrl}/api/v1/attachments/${attachmentId}/content`,
          required_headers: {
            "Content-Length": String(fixture.archive.size),
            "Content-Type": fixture.archive.content_type,
            "Idempotency-Key": attachmentId,
            "X-Content-SHA256": fixture.archive.sha256,
          },
          max_bytes: 2684354560,
          expires_at: null,
        },
      }),
    },
  ];
  return {
    ...fixture,
    caseId,
    publicBaseUrl,
    inputRequirements,
    phaseOneRecords,
    phaseTwoRecords,
    questionText: inputRequirements.map((item) => item.prompt).join("\n"),
    requestIds: {
      create: "planned-create",
      prepare: "planned-prepare",
      submit_inputs: "planned-facts",
      submit_attachment: "planned-attachment",
    },
    actualRequestIds: { createRequestId, prepareRequestId },
  };
}

function terminalArtifactFixture() {
  const caseId = "00000000-0000-4000-8000-000000000201";
  const attachmentId = "00000000-0000-4000-8000-000000000202";
  const jobId = "00000000-0000-4000-8000-000000000203";
  const createdAt = "2026-09-04T00:00:00Z";
  const publicBaseUrl = "http://127.0.0.1:43123";
  const descriptors = [
    {
      artifact_id: "00000000-0000-4000-8000-000000000204",
      kind: "USER_RESULT",
      name: "diagnosis-result.json",
      content_type: "application/json",
      size: 41,
      sha256: "a".repeat(64),
      created_at: createdAt,
    },
    {
      artifact_id: "00000000-0000-4000-8000-000000000205",
      kind: "USER_RESULT_ARCHIVE",
      name: "result.zip",
      content_type: "application/zip",
      size: 73,
      sha256: "b".repeat(64),
      created_at: createdAt,
    },
  ].map((item) => ({
    ...item,
    download_url: `${publicBaseUrl}/api/v1/artifacts/${item.artifact_id}/content?case_id=${caseId}`,
  }));
  const summaries = descriptors.map(({ download_url: _downloadUrl, ...item }) => ({
    ...item,
    created_by_job_id: jobId,
  }));
  const releaseCase = {
    driver: {
      initial_user_fact_names: ["problem_time"],
      initial_user_fact_values: ["2026-09-04T00:00:00.000Z"],
      supplement_input_names: [],
      supplement_input_values: [],
    },
    result_expectation: { case_status: "RESOLVED", resolution_status: "RESOLVED" },
    skill: { runtime_ref_id: "rpc-timeout", version: "1.0.0" },
  };
  const state = {
    case_id: caseId,
    attachment_id: attachmentId,
    case_revision: 7,
    resolved_case_revision: 9,
    public_base_url: publicBaseUrl,
    public_artifact: descriptors[0],
    public_result_archive: descriptors[1],
    request_ids: { submit_attachment: "submit-terminal-attachment", submit_inputs: "unused" },
  };
  const success = (data) => ({ ok: true, error: null, data });
  const terminalView = {
    case_id: caseId,
    case_revision: 9,
    diagnosis_state_revision: 4,
    status: "RESOLVED",
    pending_requirements: [],
    methods_result: null,
    selected_skill_ref: {
      id: releaseCase.skill.runtime_ref_id,
      version: releaseCase.skill.version,
    },
    final_result: {
      status: "ACCEPTED",
      resolution_status: "RESOLVED",
      proposed_by_job_id: jobId,
    },
    artifacts: summaries,
  };
  return { caseId, descriptors, releaseCase, state, success, summaries, terminalView };
}

function phaseThreeRecords(fixture, { inline = true, list = false } = {}) {
  const records = [
    {
      ordinal: 0,
      tool_name: "problem_locator_submit_supplement",
      input: {
        request_id: fixture.state.request_ids.submit_attachment,
        case_id: fixture.caseId,
        expected_case_revision: fixture.state.case_revision,
        input_names: fixture.releaseCase.driver.initial_user_fact_names,
        input_values: fixture.releaseCase.driver.initial_user_fact_values,
        attachment_ids: [fixture.state.attachment_id],
        wait_seconds: 30,
      },
      result: fixture.success({ business_receipt: { case_id: fixture.caseId } }),
    },
    {
      ordinal: 1,
      tool_name: "problem_locator_get_case",
      input: { case_id: fixture.caseId, wait_for_job_id: null, wait_seconds: 30 },
      result: fixture.success({
        case_view: { case_id: fixture.caseId, case_revision: 8, status: "REVIEWING" },
        wait_timed_out: false,
        artifact_views: [],
      }),
    },
    {
      ordinal: 2,
      tool_name: "problem_locator_get_case",
      input: { case_id: fixture.caseId, wait_for_job_id: null, wait_seconds: 30 },
      result: fixture.success({
        case_view: fixture.terminalView,
        wait_timed_out: false,
        ...(inline ? { artifact_views: fixture.descriptors } : {}),
      }),
    },
  ];
  if (list) {
    records.push({
      ordinal: 3,
      tool_name: "problem_locator_list_artifacts",
      input: { case_id: fixture.caseId },
      result: fixture.success({ artifacts: fixture.descriptors }),
    });
  }
  return records;
}

test("terminal client contract consumes get_case artifact_views without a list round trip", () => {
  const fixture = terminalArtifactFixture();
  const summary = validatePhaseThree(
    { records: phaseThreeRecords(fixture) },
    fixture.state,
    fixture.releaseCase,
  );
  assert.equal(summary.artifact_projection_source, "GET_CASE");
  assert.equal(summary.public_artifact.artifact_id, fixture.descriptors[0].artifact_id);
  assert.doesNotMatch(phaseThreePrompt(fixture.state, fixture.releaseCase), /Call problem_locator_list_artifacts exactly once for this Case and stop/u);
  assert.match(phaseThreePrompt(fixture.state, fixture.releaseCase), /Only when the member is completely absent/u);
  assert.match(phaseThreePrompt(fixture.state, fixture.releaseCase), /one batched submission/u);

  const unwaited = phaseThreeRecords(fixture);
  unwaited[0].input.wait_seconds = 0;
  assert.throws(
    () => validatePhaseThree({ records: unwaited }, fixture.state, fixture.releaseCase),
    (error) => error.code === "PHASE3_ATTACHMENT_INPUT",
  );

  const directTerminal = phaseThreeRecords(fixture);
  directTerminal.splice(1, 1);
  const directSummary = validatePhaseThree(
    { records: directTerminal },
    fixture.state,
    fixture.releaseCase,
  );
  assert.deepEqual(directSummary.observed_statuses, ["RESOLVED"]);
});

test("direct Methods preprocessing leaves only Specialist and Reviewer Agent invocations", () => {
  const direct = [
    { job_type: "DIAGNOSE" },
    { job_type: "REVIEW" },
  ];
  assert.equal(validDirectMethodsServiceInvocations(direct), true);
  assert.equal(
    validDirectMethodsServiceInvocations([
      direct[0],
      { job_type: "DIAGNOSE" },
      direct[1],
    ]),
    false,
  );
  assert.equal(validDirectMethodsServiceInvocations([{ job_type: "DIAGNOSE" }]), false);
});

test("terminal client contract permits list_artifacts only when artifact_views is absent", () => {
  const fixture = terminalArtifactFixture();
  const legacy = validatePhaseThree(
    { records: phaseThreeRecords(fixture, { inline: false, list: true }) },
    fixture.state,
    fixture.releaseCase,
  );
  assert.equal(legacy.artifact_projection_source, "LIST_ARTIFACTS");
  assert.throws(
    () => validatePhaseThree(
      { records: phaseThreeRecords(fixture, { inline: true, list: true }) },
      fixture.state,
      fixture.releaseCase,
    ),
    (error) => error.code === "PHASE3_REDUNDANT_ARTIFACT_LIST",
  );
  const invalid = phaseThreeRecords(fixture);
  invalid[2].result.data.artifact_views = [];
  assert.throws(
    () => validatePhaseThree({ records: invalid }, fixture.state, fixture.releaseCase),
    (error) => error.code === "PHASE3_ARTIFACT_COUNT",
  );
});

test("restart persistence check uses inline descriptors and keeps a strict legacy fallback", () => {
  const fixture = terminalArtifactFixture();
  const currentGet = {
    ordinal: 0,
    tool_name: "problem_locator_get_case",
    input: { case_id: fixture.caseId, wait_for_job_id: null, wait_seconds: 0 },
    result: fixture.success({ case_view: fixture.terminalView, wait_timed_out: false, artifact_views: fixture.descriptors }),
  };
  const current = validateRestart({ records: [currentGet] }, fixture.state, fixture.releaseCase);
  assert.equal(current.artifact_projection_source, "GET_CASE");

  const legacyGet = clone(currentGet);
  delete legacyGet.result.data.artifact_views;
  const list = {
    ordinal: 1,
    tool_name: "problem_locator_list_artifacts",
    input: { case_id: fixture.caseId },
    result: fixture.success({ artifacts: fixture.descriptors }),
  };
  const legacy = validateRestart({ records: [legacyGet, list] }, fixture.state, fixture.releaseCase);
  assert.equal(legacy.artifact_projection_source, "LIST_ARTIFACTS");
  assert.throws(
    () => validateRestart({ records: [currentGet, list] }, fixture.state, fixture.releaseCase),
    (error) => error.code === "RESTART_REDUNDANT_ARTIFACT_LIST",
  );
  assert.match(restartPrompt(fixture.state), /Only when the member is completely absent/u);
});

test("the two-turn boundary creates first and prepares the attachment before the batched supplement", () => {
  const fixture = twoTurnClientFixture();
  const phaseOneSummary = validatePhaseOne(
    {
      records: fixture.phaseOneRecords,
      assistant_text_events: [{ stream_ordinal: 6, text: fixture.questionText }],
    },
    fixture.releaseCase,
    fixture.requestIds,
  );
  assert.equal(phaseOneSummary.case_id, fixture.caseId);
  assert.deepEqual(phaseOneSummary.request_ids, {
    ...fixture.requestIds,
    create: fixture.actualRequestIds.createRequestId,
  });
  assert.deepEqual(
    phaseOneSummary.input_requirements.map((item) => item.name),
    fixture.releaseCase.driver.initial_user_fact_names,
  );

  const phaseTwoSummary = validatePhaseTwo(
    { records: fixture.phaseTwoRecords, assistant_text_events: [] },
    phaseOneSummary,
    fixture.releaseCase,
    phaseOneSummary.request_ids,
    fixture.archive,
    fixture.publicBaseUrl,
  );
  assert.equal(phaseTwoSummary.attachment_id, "00000000-0000-4000-8000-000000000103");
  assert.deepEqual(phaseTwoSummary.request_ids, {
    ...fixture.requestIds,
    create: fixture.actualRequestIds.createRequestId,
    prepare: fixture.actualRequestIds.prepareRequestId,
  });
  assert.deepEqual(fixture.phaseOneRecords.map((item) => item.tool_name), [
    "problem_locator_create_case",
    "problem_locator_get_case",
  ]);
  assert.deepEqual(fixture.phaseTwoRecords.map((item) => item.tool_name), [
    "problem_locator_get_case",
    "problem_locator_prepare_attachment",
  ]);
});

test("the two-turn boundary rejects asking or preparing before requirement observation", () => {
  const fixture = twoTurnClientFixture();
  assert.throws(
    () => validatePhaseOne(
      { records: fixture.phaseOneRecords, assistant_text_events: [] },
      fixture.releaseCase,
      fixture.requestIds,
    ),
    (error) => error.code === "PHASE1_REQUIREMENTS_NOT_ASKED_AFTER_OBSERVATION",
  );
  assert.throws(
    () => validatePhaseOne(
      {
        records: fixture.phaseOneRecords,
        assistant_text_events: [{ stream_ordinal: 4, text: fixture.questionText }],
      },
      fixture.releaseCase,
      fixture.requestIds,
    ),
    (error) => error.code === "PHASE1_REQUIREMENTS_NOT_ASKED_AFTER_OBSERVATION",
  );

  const phaseOneSummary = validatePhaseOne(
    {
      records: fixture.phaseOneRecords,
      assistant_text_events: [{ stream_ordinal: 6, text: fixture.questionText }],
    },
    fixture.releaseCase,
    fixture.requestIds,
  );
  const preparedWithoutObservation = clone(fixture.phaseTwoRecords);
  preparedWithoutObservation[0].result.data.case_view.status = "RUNNING";
  assert.throws(
    () => validatePhaseTwo(
      { records: preparedWithoutObservation, assistant_text_events: [] },
      phaseOneSummary,
      fixture.releaseCase,
      phaseOneSummary.request_ids,
      fixture.archive,
      fixture.publicBaseUrl,
    ),
    (error) => error.code === "PHASE2_INPUT_REQUIREMENTS_NOT_REOBSERVED",
  );
});

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
    ["async function crossJob", "function realEnvironment", "const receiptPath"],
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
