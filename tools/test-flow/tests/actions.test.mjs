import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  collectIsolatedModelUsage,
  evaluatePytestSummary,
  frozenServerImageId,
  hostCapabilityProcessSpec,
  materializePytestSummary,
  parseJUnitSummary,
  planAffectedSelection,
  probeLoopbackCapability,
  pytestBaseTempPath,
  pytestScratchBoundary,
  serverCapabilityTerminationResult,
  validLinuxClientBrowserCapabilityReceipt,
  validCrossJobPassRuntimeBoundary,
  validCodexLunaPassBoundary,
  validClaudeDeepseekInvocationLedger,
  validHostCapabilityReceipt,
  validServerRuntimeIdentity,
} from "../lib/actions.mjs";
import {
  RELEASE_CLAUDE_CLI_SHA256,
  RELEASE_CLAUDE_VERSION_OUTPUT,
  RELEASE_PYTHON_VERSION,
  RELEASE_UV_SHA256,
  RELEASE_UV_VERSION,
  RELEASE_UV_VERSION_OUTPUT,
  RELEASE_UVX_SHA256,
  RELEASE_UVX_VERSION_OUTPUT,
} from "../lib/release-inputs.mjs";
import { applyGateEvidenceContract } from "../lib/engine.mjs";
import { canonicalJson, removeTreeWritable } from "../lib/util.mjs";
import {
  environmentKeySummary,
  ISOLATED_AGENT_ENV_POLICY_VERSION,
  ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT,
} from "../runtime-support/isolated-agent-env.mjs";
import {
  SKILL_GENERATION_TRACE_SCHEMA_VERSION,
} from "../runtime-support/isolated-agent-tool-audit.mjs";
import {
  buildPosthocBudgetReceipt,
  CODEX_LUNA_APP_SERVER_SCHEMA_TREE_SHA256,
  CODEX_LUNA_PERMISSION_PROFILE_VERSION,
  normalizeCodexUsage,
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
} from "../runtime-support/codex-luna-app-server.mjs";

test("Claude E2E ledger must exactly match the scenario-specific planned phases", () => {
  const phases = ["CLIENT", "ROUTE", "LOGPARSE", "DIAGNOSE"];
  const planStage = { invocation_caps: [{ phases, min_count: 4, max_count: 4 }] };
  const invocations = phases.map((phase) => ({ phase, status: "PASS", terminal: true }));
  assert.equal(validClaudeDeepseekInvocationLedger(planStage, { status: "PASS", invocations }), true);
  assert.equal(validClaudeDeepseekInvocationLedger(planStage, { status: "PASS", invocations: [...invocations, { phase: "REVIEW", status: "PASS", terminal: true }] }), false);
  assert.equal(validClaudeDeepseekInvocationLedger(planStage, { status: "PASS", invocations: [invocations[0], invocations[2], invocations[1], invocations[3]] }), false);
  assert.equal(validClaudeDeepseekInvocationLedger({ invocation_caps: [{ phases, min_count: 5, max_count: 5 }] }, { status: "PASS", invocations }), false);
});

function codexLunaFixtureGenerationPrompt() {
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

function codexLunaFixtureDiagnosisPrompt(scenarioId, receiptSha256) {
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

function writeTest(file) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, "def test_placeholder():\n    assert True\n");
}

function passingSkillTraceAudit() {
  const requiredReads = [
    "workspace/inputs/wiki.md",
    "workspace/runtime/source-wiki-identity.json",
    "skill/references/output-contract.md",
  ];
  const files = [
    { path: "workspace/output/diagnose-rpc-timeout/SKILL.md", size_bytes: 3, sha256: "b".repeat(64), write_ordinal: 4 },
    { path: "workspace/output/diagnose-rpc-timeout/methods.json", size_bytes: 3, sha256: "c".repeat(64), write_ordinal: 5 },
    { path: "workspace/output/diagnose-rpc-timeout/references/method.md", size_bytes: 3, sha256: "d".repeat(64), write_ordinal: 6 },
    { path: "workspace/output/diagnose-rpc-timeout/references/source-log-templates.md", size_bytes: 3, sha256: "e".repeat(64), write_ordinal: 7 },
  ];
  const canonical = (value) => {
    if (Array.isArray(value)) return `[${value.map((item) => canonical(item)).join(",")}]`;
    if (value !== null && typeof value === "object") return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
    return JSON.stringify(value);
  };
  const digestFiles = files.map(({ path: filePath, size_bytes, sha256 }) => ({ path: filePath, size_bytes, sha256 }));
  return {
    schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
    status: "PASS",
    workflow: "skill-generation",
    skill: "wiki-to-diagnosis-skill",
    tool_inventory: ["Skill", "Read", "Write"],
    permission_mode: "dontAsk",
    permission_policy_sha256: "a".repeat(64),
    tool_sequence: [
      { ordinal: 0, tool: "Skill", outcome: "SUCCESS" },
      ...requiredReads.map((readPath, index) => ({ ordinal: index + 1, tool: "Read", outcome: "SUCCESS", path: readPath })),
      ...files.map((file) => ({ ordinal: file.write_ordinal, tool: "Write", outcome: "SUCCESS", path: file.path })),
    ],
    required_reads: requiredReads,
    observed_reads: requiredReads.map((readPath, index) => ({ ordinal: index + 1, path: readPath })),
    linked_references: requiredReads.filter((readPath) => readPath.startsWith("skill/")),
    package: {
      skill_name: "diagnose-rpc-timeout",
      root: "workspace/output/diagnose-rpc-timeout",
      file_count: files.length,
      files,
      content_tree_sha256: crypto.createHash("sha256").update(canonical({ version: 1, files: digestFiles })).digest("hex"),
    },
    source_log_templates: {
      extraction_version: 1,
      count: 1,
      inventory_sha256: "f".repeat(64),
      reference_path: "workspace/output/diagnose-rpc-timeout/references/source-log-templates.md",
      reference_sha256: "e".repeat(64),
    },
    terminal: { subtype: "success", is_error: false },
  };
}

function passingCodexLunaBoundary() {
  const canonical = (value) => {
    if (Array.isArray(value)) return `[${value.map((item) => canonical(item)).join(",")}]`;
    if (value !== null && typeof value === "object") return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
    return JSON.stringify(value);
  };
  const sha = "a".repeat(64);
  const account = "b".repeat(64);
  const runId = "run-codex-unit";
  const digest = (value) => crypto.createHash("sha256").update(value).digest("hex");
  const accessTokenSha256 = digest("fixture access token");
  const accessTokenLength = 100;
  const outboundReceipt = (message) => ({
    schema_version: 1,
    method: message.method,
    id: Object.hasOwn(message, "id") ? message.id : null,
    params_sha256: digest(canonical(message.params ?? null)),
  });
  const workRoot = "/attempt/scratch/codex-luna-methods/work";
  const privateRoot = "/attempt/scratch/codex-luna-methods/private";
  const generationWorkspace = `${workRoot}/generation`;
  const diagnosisWorkspaces = Array.from({ length: 9 }, (_, index) => `${workRoot}/diagnoses/scenario-${index + 1}`);
  const generationWorkspacePathSha256 = digest(path.resolve(generationWorkspace));
  const diagnosisWorkspacePathSha256 = diagnosisWorkspaces.map((workspace) => digest(path.resolve(workspace)));
  const forbiddenReadPathSha256 = [
    digest("/snapshot/AGENTS.md"),
    digest("/snapshot/experiments/rpc-skill-feasibility/cases/api-execution-overrun/raw/client.log"),
    digest("/attempt/scratch/codex-luna-methods/release-inputs/auth.json"),
  ];
  const callEvidenceRoot = "payload/stages/real.codex-luna-methods/gates/real.codex-luna-methods";
  const logparseConfigSha256 = digest("logparse config");
  const executedCodex = {
    schema_version: 1,
    status: "PASS",
    cli: {
      version: "codex-cli 0.149.0-alpha.4.1",
      sha256: "09db9560f6f9dec139d3324254fb3c8fdbad5ecce1d8c794113dc15294f6aefd",
      size: 123,
      platform: "darwin",
      architecture: "arm64",
      entry_path_sha256: "c".repeat(64),
    },
    auth: {
      kind: "chatgpt-external-tokens",
      auth_mode: "chatgpt",
      sha256: sha,
      size: 456,
      account_id_sha256: account,
      transfer: "app-server-account-login-start-memory-only",
    },
    filesystem_sandbox: {
      kind: "codex-permission-profile",
      profile_version: CODEX_LUNA_PERMISSION_PROFILE_VERSION,
      enforcement: "single-layer-codex-command-sandbox",
      command_network: "disabled",
      auth_storage: "external-memory-no-auth-file",
      app_server_transport: "stdio-json-rpc",
    },
    model: "gpt-5.6-luna",
    reasoning_effort: "medium",
  };
  const external = { status: "PRESENT", root: "/logparse", head: "d".repeat(40), clean: true };
  const logparseRuntime = {
    status: "PRESENT",
    cli: { sha256: "e".repeat(64) },
    python: { resolved_sha256: "f".repeat(64), version: "Python 3.12.13" },
  };
  const scenarioIds = Array.from({ length: 9 }, (_, index) => `scenario-${index + 1}`);
  const preprocessingCases = scenarioIds.map((scenarioId, index) => ({
    scenario_id: scenarioId,
    status: "PASS",
    parse_invocations: 1,
    target_query_invocations: 2,
    receipt_sha256: digest(`preprocessing receipt ${index + 1}`),
    frozen_target_logs: ["client", "server"].map((label) => ({ label, size: 10 + index, sha256: digest(`${scenarioId} ${label}`) })),
  }));
  const preprocessing = {
    schema_version: 1,
    status: "PASS",
    case_count: 9,
    logparse_identity: {
      schema_version: 1,
      git_head: external.head,
      git_status_sha256: crypto.createHash("sha256").update("").digest("hex"),
      cli_sha256: logparseRuntime.cli.sha256,
      python_sha256: logparseRuntime.python.resolved_sha256,
      python_version: logparseRuntime.python.version,
    },
    config: { product: "rpc-skill-feasibility", sha256: logparseConfigSha256 },
    totals: { parse_invocations: 9, target_query_invocations: 18, diagnosis_invocations: 0 },
    cases: preprocessingCases,
  };
  const environment = {
    schema_version: 1,
    policy: "explicit-safe-environment-v1",
    inherited_keys: ["LANG", "PATH"],
    stripped_sensitive_key_names: [],
    sensitive_values_forwarded: 0,
    home_isolated: true,
    codex_home_isolated: true,
    user_config_ignored: true,
    user_rules_ignored: true,
  };
  const metaSkillTreeSha256 = "3".repeat(64);
  const wikiSha256 = "1".repeat(64);
  const wikiSize = 789;
  const validatorPythonPathSha256 = "4".repeat(64);
  const validatorRuntimeSha256 = crypto.createHash("sha256").update(canonical(logparseRuntime)).digest("hex");
  const protocolSchema = {
    schema_version: 1,
    status: "PASS",
    experimental: true,
    file_count: 401,
    tree_sha256: CODEX_LUNA_APP_SERVER_SCHEMA_TREE_SHA256,
  };
  const identity = {
    schema_version: 1,
    contract_version: 1,
    run_id: runId,
    invocation_class: "codex-luna-agent",
    cli: { schema_version: 1, ...executedCodex.cli, exact_match: true },
    model: executedCodex.model,
    reasoning_effort: executedCodex.reasoning_effort,
    protocol_schema: protocolSchema,
    auth: {
      schema_version: 1,
      mode: "chatgpt-external-tokens",
      source_sha256: sha,
      byte_count: 456,
      account_id_sha256: account,
      access_token_sha256: accessTokenSha256,
      access_token_length: accessTokenLength,
      transfer: "app-server-account-login-start-memory-only",
      transmitted_fields: ["access_token", "account_id"],
      withheld_fields: ["refresh_token", "id_token"],
      credential_persisted: false,
      auth_json_files: 0,
      refresh_policy: "fail-closed-no-refresh-replay",
    },
    environment,
    model_shell_environment: {
      inherit: "none",
      set_keys: ["HOME", "LANG", "PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE"],
      auth_environment_available: false,
      home_is_workspace_local: true,
    },
    meta_skill: { name: "wiki-to-diagnosis-skill", tree_sha256: metaSkillTreeSha256 },
    wiki: { sha256: wikiSha256, size: wikiSize },
    scenarios: scenarioIds,
    validator_runtime: {
      policy: "exact-planned-logparse-python-isolated-pre-and-post-v1",
      identity_sha256: validatorRuntimeSha256,
      python_entry_path_sha256: validatorPythonPathSha256,
    },
    filesystem_sandbox: executedCodex.filesystem_sandbox,
  };
  const revalidatedRecords = [];
  const calls = Array.from({ length: 10 }, (_, index) => {
    const ordinal = String(index + 1).padStart(2, "0");
    const generation = index === 0;
    const workspace = generation ? generationWorkspace : diagnosisWorkspaces[index - 1];
    const skillName = generation ? "wiki-to-diagnosis-skill" : "diagnose-rpc-timeout";
    const skillPath = `${workspace}/.agents/skills/${skillName}/SKILL.md`;
    const codexHome = `${privateRoot}/calls/${ordinal}/codex-home`;
    const profile = buildCodexLunaIsolatedConfig({
      workspaceRoot: workspace,
      skillPath,
      codexHome,
      mode: generation ? "generation" : "diagnosis",
    });
    const threadId = `thread-${index + 1}`;
    const turnId = `turn-${index + 1}`;
    const usage = normalizeCodexUsage({
      input_tokens: 100,
      cached_input_tokens: 40,
      cache_write_input_tokens: 0,
      output_tokens: 20,
      reasoning_output_tokens: 5,
    });
    const usageBreakdown = {
      totalTokens: 120,
      inputTokens: 100,
      cachedInputTokens: 40,
      cacheWriteInputTokens: 0,
      outputTokens: 20,
      reasoningOutputTokens: 5,
    };
    const finalMessage = `final ${index + 1}`;
    const transcriptSummary = {
      schema_version: 1,
      status: "PASS",
      protocol_version: "v2",
      transport: "jsonl-stdio",
      pinned_cli_version: "0.149.0-alpha.4.1",
      server_user_agent: "codex_app_server_rs/0.149.0-alpha.4.1",
      server_platform_family: "unix",
      server_platform_os: "macos",
      auth_mode: "chatgptAuthTokens",
      account_plan_type: "plus",
      permission_profile_id: profile.profile_id,
      invocation_mode: profile.invocation_mode,
      workspace_root_sha256: digest(profile.workspace_root),
      intended_skill_name: profile.skill_name,
      intended_skill_path_sha256: digest(profile.skill_path),
      codex_home_sha256: profile.codex_home_sha256,
      disabled_system_skill_path_sha256s: profile.disabled_system_skill_paths.map((entry) => digest(entry)),
      instruction_source_path_sha256s: [digest(skillPath)],
      thread_id: threadId,
      turn_id: turnId,
      model: "gpt-5.6-luna",
      reasoning_effort: "medium",
      final_agent_message: finalMessage,
      commands: [],
      command_count: 0,
      raw_response_count: 1,
      raw_response_ids: [`response-${index + 1}`],
      raw_response_usage: usageBreakdown,
      thread_token_usage: { total: usageBreakdown, last: usageBreakdown, modelContextWindow: 400000 },
      usage: { ...usage, schema_version: undefined, equivalent_usd_upper_bound: undefined },
      inbound_message_count: 20,
    };
    delete transcriptSummary.usage.schema_version;
    delete transcriptSummary.usage.equivalent_usd_upper_bound;
    const appServerEvidence = buildCodexLunaAppServerEvidenceSummary({ profile, transcript: transcriptSummary });
    const outputSchema = generation ? null : {
      type: "object",
      properties: {
        scenario_id: { const: scenarioIds[index - 1] },
        logparse_receipt_sha256: { const: preprocessingCases[index - 1].receipt_sha256 },
      },
    };
    const prompt = generation
      ? codexLunaFixtureGenerationPrompt()
      : codexLunaFixtureDiagnosisPrompt(scenarioIds[index - 1], preprocessingCases[index - 1].receipt_sha256);
    const outbound = [
      outboundReceipt(buildCodexLunaInitializeRequest()),
      outboundReceipt(buildCodexLunaInitializedNotification()),
      {
        schema_version: 1,
        method: "account/login/start",
        id: CODEX_LUNA_APP_SERVER_REQUEST_IDS.login,
        params_sha256: null,
        auth: {
          type: "chatgptAuthTokens",
          account_id_sha256: account,
          access_token_sha256: accessTokenSha256,
          access_token_length: accessTokenLength,
          credential_returned: false,
        },
      },
      outboundReceipt(buildCodexLunaAccountReadRequest()),
      outboundReceipt(buildCodexLunaPermissionProfileListRequest({ workspaceRoot: workspace })),
      outboundReceipt(buildCodexLunaSkillsListRequest({ workspaceRoot: workspace })),
      outboundReceipt(buildCodexLunaThreadStartRequest({ workspaceRoot: workspace, mode: generation ? "generation" : "diagnosis" })),
      outboundReceipt(buildCodexLunaTurnStartRequest({
        threadId,
        prompt,
        workspaceRoot: workspace,
        skillPath,
        mode: generation ? "generation" : "diagnosis",
        outputSchema,
      })),
      outboundReceipt({ method: "account/logout", id: 7 }),
    ];
    const traceSha256 = digest(`trace ${index + 1}`);
    const finalFileSha256 = digest(finalMessage);
    const codexHomeManifest = [{ path: "config.toml", size: profile.config_byte_count, sha256: profile.config_sha256 }];
    const codexHomeTreeSha256 = digest(`${canonical(codexHomeManifest)}\n`);
    const appServer = {
      ...appServerEvidence,
      outbound,
      arguments: buildCodexLunaAppServerArguments(),
      preflight: {
        schema_version: 1,
        status: "PASS",
        profile_id: profile.profile_id,
        profile_sha256: profile.config_sha256,
        workspace_path_sha256: digest(profile.workspace_root),
        workspace_read: "PASS",
        workspace_write: generation ? "ALLOWED" : "DENIED",
        command_network: { status: "DENIED", endpoint: "ipv4-loopback-listener", exit_code: 1 },
        forbidden_reads: forbiddenReadPathSha256.map((pathSha256) => ({ status: "DENIED", path_sha256: pathSha256, exit_code: 1 })),
      },
      cleanup: {
        schema_version: 1,
        status: "PASS",
        logout_request_id: 7,
        process_exit_code: 0,
        process_signal: null,
        timed_out: false,
        no_progress_timed_out: false,
        stdin_closed: true,
      },
      codex_home: {
        schema_version: 1,
        status: "PASS",
        relative_path: `calls/${ordinal}/codex-home`,
        path_sha256: digest(profile.codex_home),
        config_sha256: profile.config_sha256,
        tree_sha256: codexHomeTreeSha256,
        manifest: codexHomeManifest,
        auth_json_files: 0,
      },
      feature_disables: [...CODEX_LUNA_DISABLED_FEATURES],
      protocol_schema_tree_sha256: CODEX_LUNA_APP_SERVER_SCHEMA_TREE_SHA256,
      trace_sha256: traceSha256,
      final_sha256: finalFileSha256,
      login: {
        schema_version: 1,
        method: "account/login/start",
        id: CODEX_LUNA_APP_SERVER_REQUEST_IDS.login,
        auth_type: "chatgptAuthTokens",
        account_id_sha256: account,
        plan_type_present: false,
        write_accepted: true,
        credential_returned: false,
      },
    };
    revalidatedRecords.push({
      invocation_id: `${runId}:codex-luna:${ordinal}`,
      trace_sha256: traceSha256,
      app_server_evidence_sha256: digest(canonical(appServerEvidence)),
      thread_id: threadId,
      turn_id: turnId,
      usage,
      final_message_sha256: digest(finalMessage),
      final_file_sha256: finalFileSha256,
      profile_sha256: profile.config_sha256,
      profile_byte_count: profile.config_byte_count,
      codex_home_tree_sha256: codexHomeTreeSha256,
      outbound_sha256: digest(canonical(outbound)),
    });
    return {
      schema_version: 1,
      invocation_id: `${runId}:codex-luna:${ordinal}`,
      class: "codex-luna-agent",
      workflow: generation ? "methods-generation" : "methods-diagnosis",
      logical_id: generation ? "generate" : scenarioIds[index - 1],
      ordinal: index + 1,
      attempt: 1,
      retry_allowed: false,
      status: "PASS",
      trace: generation ? "traces/01-generation.jsonl" : `traces/${ordinal}-${scenarioIds[index - 1]}.jsonl`,
      thread_id: threadId,
      turn_id: turnId,
      usage_complete: true,
      usage,
      terminal: { event: "turn.completed", thread_id: threadId, turn_id: turnId },
      failure: null,
      process: {
        exit_code: 0,
        signal: null,
        spawn_error: null,
        timed_out: false,
        no_progress_timed_out: false,
        app_server: appServer,
      },
    };
  });
  const ledger = { schema_version: 1, run_id: runId, invocation_class: "codex-luna-agent", expected_calls: 10, retry_policy: "NONE", calls };
  const usageReceipts = calls.map((call) => ({
    schema_version: 1,
    invocation_id: call.invocation_id,
    class: call.class,
    workflow: call.workflow,
    logical_id: call.logical_id,
    effective_model: "gpt-5.6-luna",
    effective_reasoning_effort: "medium",
    effective_caps: { max_calls: 10, call_wall_seconds: 1200, no_progress_seconds: 360, stage_wall_seconds: 7200, max_total_tokens_posthoc: 5000000, max_equivalent_usd_posthoc: 10 },
    usage_complete: true,
    usage: call.usage,
    turns: 1,
    turns_source: "app-server-one-ephemeral-thread-one-terminal-turn-with-raw-response-usage",
    terminal: call.terminal,
    wrapper_outcome: { schema_version: 1, status: "PASS", code: null },
    posthoc_enforcement: { schema_version: 1, exception_id: "PSE-CODEX-LUNA-POSTHOC-001", calls: "runner-precondition-exactly-ten-no-retry" },
    process: call.process,
  }));
  const callManifest = {
    schema_version: 1,
    status: "PASS",
    run_id: runId,
    path_base: "attempt-root",
    records: calls.map((call, index) => {
      const ordinal = String(index + 1).padStart(2, "0");
      const prefix = index === 0 ? `${ordinal}-generation` : `${ordinal}-${scenarioIds[index - 1]}`;
      return {
        invocation_id: call.invocation_id,
        workflow: call.workflow,
        logical_id: call.logical_id,
        thread_id: call.thread_id,
        trace: { path: `${callEvidenceRoot}/traces/${prefix}.jsonl`, size: 10, sha256: revalidatedRecords[index].trace_sha256 },
        stderr: { path: `${callEvidenceRoot}/traces/${prefix}.stderr.txt`, size: 0, sha256: digest("") },
        final: { path: `${callEvidenceRoot}/traces/${prefix}.final.${index === 0 ? "txt" : "json"}`, size: 10, sha256: revalidatedRecords[index].final_file_sha256 },
        usage_receipt: { path: `payload/model-usage/codex-luna/${call.invocation_id.replaceAll(":", "-")}.json`, size: 10, sha256: digest(`usage receipt ${index + 1}`) },
      };
    }),
  };
  const budget = buildPosthocBudgetReceipt({ calls, usageComplete: true });
  const aggregate = budget.aggregate;
  const checks = budget.checks;
  const security = {
    schema_version: 1,
    status: "PASS",
    protocol_schema: {
      schema_version: 1,
      status: "PASS",
      file_count: 401,
      tree_sha256: CODEX_LUNA_APP_SERVER_SCHEMA_TREE_SHA256,
    },
    environment,
    auth_isolation: {
      schema_version: 1,
      mode: "chatgpt-external-tokens",
      source_sha256: sha,
      byte_count: 456,
      account_id_sha256: account,
      access_token_sha256: accessTokenSha256,
      access_token_length: accessTokenLength,
      transfer: "app-server-account-login-start-memory-only",
      transmitted_fields: ["access_token", "account_id"],
      withheld_fields: ["refresh_token", "id_token"],
      credential_persisted: false,
      auth_json_files: 0,
      refresh_policy: "fail-closed-no-refresh-replay",
    },
    artifact_secret_scan: { schema_version: 1, status: "PASS", scanned_files: 20, scanned: [] },
    permission_profiles: {
      schema_version: 1,
      status: "PASS",
      call_count: 10,
      profile_version: CODEX_LUNA_PERMISSION_PROFILE_VERSION,
      enforcement: "single-layer-codex-command-sandbox",
      call_receipts: calls.map((call) => ({ invocation_id: call.invocation_id, receipt_sha256: digest(canonical(call.process.app_server.permission_profile)) })),
    },
    oracle_and_logparse_scope: { scenario_count: 9, all_passed: true, logparse_invocations_during_diagnosis: 0, oracle_accesses: 0, raw_input_accesses: 0 },
  };
  const generatedPackageFiles = [
    { path: "SKILL.md", size: 10, sha256: digest("skill") },
    { path: "methods.json", size: 10, sha256: digest("methods") },
    { path: "references/method.md", size: 10, sha256: digest("method") },
  ];
  const generatedPackageTreeSha256 = digest(`${canonical(generatedPackageFiles)}\n`);
  const skill = {
    schema_version: 1,
    skill_name: "diagnose-rpc-timeout",
    methods_schema_version: 1,
    package_tree_sha256: generatedPackageTreeSha256,
    source_wiki_sha256: wikiSha256,
    generation_final_sha256: callManifest.records[0].final.sha256,
    generation_scope_audit: { schema_version: 1, status: "PASS", command_count: 1, legacy_contract_accesses: 0, oracle_accesses: 0, raw_input_accesses: 0, workspace_root_sha256: generationWorkspacePathSha256 },
    validator: { ok: true, skill_name: "diagnose-rpc-timeout", source_wiki_sha256: wikiSha256, method_count: 1, marker_count: 4, errors: [], runtime_identity_sha256: validatorRuntimeSha256, runtime_policy: "exact-planned-logparse-python-isolated-pre-and-post-v1" },
    method_ids: ["method-1"],
    generation_thread_id: calls[0].thread_id,
    durable_package: {
      path: "generated-skill",
      tree_sha256: generatedPackageTreeSha256,
      manifest: generatedPackageFiles,
    },
  };
  const diagnoses = scenarioIds.map((scenarioId, index) => ({
    scenario_id: scenarioId,
    status: "CONFIRMED",
    package_tree_sha256: skill.package_tree_sha256,
    thread_id: calls[index + 1].thread_id,
    receipt_sha256: preprocessingCases[index].receipt_sha256,
    scope_audit: { schema_version: 1, status: "PASS", command_count: 1, logparse_invocations: 0, oracle_accesses: 0, raw_input_accesses: 0, workspace_root_sha256: diagnosisWorkspacePathSha256[index] },
    result_sha256: callManifest.records[index + 1].final.sha256,
  }));
  const receipt = {
    schema_version: 1,
    run_id: runId,
    status: "PASS_WITH_WARNINGS",
    invocation_class: "codex-luna-agent",
    model: executedCodex.model,
    reasoning_effort: executedCodex.reasoning_effort,
    cli_identity: identity.cli,
    call_contract: { expected: 10, actual: 10, generation: 1, diagnosis: 9, retries: 0 },
    generated_skill: skill,
    diagnoses,
    posthoc_budget: { exception_id: budget.exception_id, status: budget.status, aggregate, checks },
    security_audit: { status: "PASS" },
  };
  const consumer = {
    schema_version: 1,
    status: "PASS",
    run_id: runId,
    trace_revalidation: { schema_version: 1, status: "PASS", records: revalidatedRecords },
    secret_scan: { schema_version: 1, status: "PASS", scanned_files: 30, scanned: [] },
    generated_package: {
      schema_version: 1,
      status: "PASS",
      path: `${callEvidenceRoot}/generated-skill`,
      tree_sha256: skill.package_tree_sha256,
      file_count: generatedPackageFiles.length,
      files: generatedPackageFiles,
    },
  };
  return {
    bundle: { receipt, ledger, budget, security, identity, skill, preprocessing, callManifest, usageReceipts, consumer },
    expected: {
      runId,
      executedCodex,
      external,
      logparseRuntime,
      scenarioIds,
      metaSkillTreeSha256,
      wikiSha256,
      wikiSize,
      validatorPythonPathSha256,
      logparseConfigSha256,
      callEvidenceRoot,
      generationWorkspacePathSha256,
      diagnosisWorkspacePathSha256,
      forbiddenReadPathSha256,
    },
  };
}

test("Darwin explicit Linux runs Client capability inside the frozen Linux image", () => {
  const imageId = `sha256:${"a".repeat(64)}`;
  const spec = hostCapabilityProcessSpec({
    client: "linux",
    platform: "darwin",
    sourceSnapshotRoot: "/snapshot",
    outputRoot: "/attempt/stage",
    claudeEntry: "/host/claude/cli.js",
    runtimeProfileDigest: "b".repeat(64),
    dockerContext: "colima",
    clientImageId: imageId,
    runId: "release-run-123",
    hostUid: 501,
    hostGid: 20,
  });
  assert.equal(spec.command, "docker");
  assert.equal(spec.executionTopology, "darwin-orchestrated-linux-container");
  assert.equal(spec.clientImageId, imageId);
  assert.ok(spec.args.includes("linux/amd64"));
  assert.ok(spec.args.includes("/opt/claude-code/cli.js"));
  assert.ok(spec.args.includes("--network"));
  assert.ok(spec.args.includes("none"));
  assert.deepEqual(spec.clientUser, { uid: 501, gid: 20, root: false });
  assert.ok(spec.args.includes("501:20"));
  assert.ok(spec.args.includes("HOME=/client-home"));
  assert.ok(spec.args.some((value) => value.includes("/client-home:") && value.includes("uid=501") && value.includes("gid=20")));
  assert.equal(spec.args.includes("/host/claude/cli.js"), false);
  assert.throws(() => hostCapabilityProcessSpec({
    client: "linux",
    platform: "darwin",
    sourceSnapshotRoot: "/snapshot",
    outputRoot: "/attempt/stage",
    claudeEntry: "/host/claude/cli.js",
    runtimeProfileDigest: "b".repeat(64),
    dockerContext: "colima",
    clientImageId: imageId,
    runId: "release-run-123",
    hostUid: 0,
    hostGid: 0,
  }), /HOST_CAPABILITY_TOPOLOGY_UNSUPPORTED/);

  const receipt = {
    schema_version: 3,
    status: "PASS",
    runtime_profile_digest: "b".repeat(64),
    client: "linux",
    architecture: "x64",
    execution_topology: spec.executionTopology,
    client_image_id: imageId,
    execution_user: { uid: 501, gid: 20, root: false },
    node_version: "v24.0.0",
    node_executable: "/usr/bin/node",
    node_sha256: "c".repeat(64),
    flat_schema: true,
    flat_call: true,
    client_dfx_absent: true,
  };
  const expected = {
    runtimeProfileDigest: "b".repeat(64),
    client: "linux",
    executionTopology: spec.executionTopology,
    clientImageId: imageId,
    clientUser: spec.clientUser,
  };
  assert.equal(validHostCapabilityReceipt(receipt, expected), true);
  assert.equal(validHostCapabilityReceipt({ ...receipt, execution_user: { uid: 0, gid: 0, root: true } }, expected), false);
});

test("native Client capability stays native and unsupported host-client pairs fail closed", () => {
  const native = hostCapabilityProcessSpec({
    client: "macos",
    platform: "darwin",
    sourceSnapshotRoot: "/snapshot",
    outputRoot: "/attempt/stage",
    claudeEntry: "/host/claude/cli.js",
    runtimeProfileDigest: "b".repeat(64),
    runId: "release-run-123",
  });
  assert.equal(native.command, process.execPath);
  assert.equal(native.executionTopology, "native-host");
  assert.equal(native.clientImageId, null);
  assert.ok(native.args.includes("/host/claude/cli.js"));
  assert.throws(
    () => hostCapabilityProcessSpec({
      client: "windows",
      platform: "darwin",
      sourceSnapshotRoot: "/snapshot",
      outputRoot: "/attempt/stage",
      claudeEntry: "/host/claude/cli.js",
      runtimeProfileDigest: "b".repeat(64),
      dockerContext: "colima",
      clientImageId: `sha256:${"a".repeat(64)}`,
      runId: "release-run-123",
    }),
    /HOST_CAPABILITY_TOPOLOGY_UNSUPPORTED/,
  );
});

test("Linux Server capability accepts only the image ID frozen by planning", () => {
  const imageId = `sha256:${"c".repeat(64)}`;
  assert.equal(frozenServerImageId({ release_inputs: { image: { server: { image_id: imageId } } } }), imageId);
  assert.throws(() => frozenServerImageId({ release_inputs: { image: { server: { image_id: "mutable:tag" } } } }), /SERVER_IMAGE_IDENTITY_MISSING/);
  assert.throws(() => frozenServerImageId({ release_inputs: { image: { server: null } } }), /SERVER_IMAGE_IDENTITY_MISSING/);
});

test("Linux Server runtime identity requires exact CLI, uv and uvx bytes inside the frozen image", () => {
  const imageId = `sha256:${"a".repeat(64)}`;
  const identity = {
    schema_version: 1,
    image_id: imageId,
    claude: {
      path: "/opt/claude-code/cli.js",
      sha256: RELEASE_CLAUDE_CLI_SHA256,
      version: RELEASE_CLAUDE_VERSION_OUTPUT,
    },
    node: { architecture: "x64" },
    uv: {
      path: "/usr/local/bin/uv",
      sha256: RELEASE_UV_SHA256,
      version: RELEASE_UV_VERSION_OUTPUT,
    },
    uvx: {
      path: "/usr/local/bin/uvx",
      sha256: RELEASE_UVX_SHA256,
      version: RELEASE_UVX_VERSION_OUTPUT,
    },
    python: { version: `Python ${RELEASE_PYTHON_VERSION}` },
  };
  assert.equal(validServerRuntimeIdentity(identity, imageId), true);
  assert.equal(validServerRuntimeIdentity({ ...identity, uv: { ...identity.uv, version: `uv ${RELEASE_UV_VERSION}` } }, imageId), false);
  assert.equal(validServerRuntimeIdentity({ ...identity, uvx: { ...identity.uvx, version: `uvx ${RELEASE_UV_VERSION}` } }, imageId), false);
  assert.equal(validServerRuntimeIdentity({ ...identity, uv: { ...identity.uv, version: "uv 0.11.32 (aarch64-unknown-linux-gnu)" } }, imageId), false);
  assert.equal(validServerRuntimeIdentity({ ...identity, uvx: { ...identity.uvx, version: `${RELEASE_UVX_VERSION_OUTPUT} trailing` } }, imageId), false);
  assert.equal(validServerRuntimeIdentity({ ...identity, uv: { ...identity.uv, version: RELEASE_UVX_VERSION_OUTPUT }, uvx: { ...identity.uvx, version: RELEASE_UV_VERSION_OUTPUT } }, imageId), false);
  assert.equal(validServerRuntimeIdentity({ ...identity, image_id: `sha256:${"b".repeat(64)}` }, imageId), false);
  assert.equal(validServerRuntimeIdentity({ ...identity, uv: { ...identity.uv, sha256: "0".repeat(64) } }, imageId), false);
  assert.equal(validServerRuntimeIdentity({ ...identity, uvx: { ...identity.uvx, version: "uvx mutable" } }, imageId), false);
  assert.equal(validServerRuntimeIdentity({ ...identity, claude: { ...identity.claude, sha256: "0".repeat(64) } }, imageId), false);
});

test("Linux Server capability preserves only an allowlisted structured adapter termination", () => {
  assert.deepEqual(
    serverCapabilityTerminationResult({ schema_version: 1, status: "BLOCKED", code: "SERVER_CAPABILITY_RUNTIME_IDENTITY" }, 2),
    { status: "BLOCKED", failure_domain: "INFRA", code: "SERVER_CAPABILITY_RUNTIME_IDENTITY" },
  );
  assert.deepEqual(
    serverCapabilityTerminationResult({ schema_version: 1, status: "FAIL", code: "SERVER_CAPABILITY_CONTRACT" }, 3),
    { status: "FAIL", failure_domain: "EXTERNAL", code: "SERVER_CAPABILITY_CONTRACT" },
  );
  assert.equal(serverCapabilityTerminationResult({ schema_version: 1, status: "BLOCKED", code: "ARBITRARY_STDERR" }, 2), null);
  assert.equal(serverCapabilityTerminationResult({ schema_version: 1, status: "BLOCKED", code: "SERVER_CAPABILITY_RUNTIME_IDENTITY" }, 3), null);
  assert.equal(serverCapabilityTerminationResult({ schema_version: 1, status: "BLOCKED", code: "SERVER_CAPABILITY_RUNTIME_IDENTITY", detail: "extra" }, 2), null);
});

test("dual Linux CrossJob PASS receipts bind exact topology, images, Client runtime and generated Skill", () => {
  const serverImageId = `sha256:${"a".repeat(64)}`;
  const clientImageId = `sha256:${"b".repeat(64)}`;
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
    release_inputs: {
      topology: "darwin-orchestrated-dual-linux-containers",
      image: {
        server: { image_id: serverImageId },
        client: { image_id: clientImageId },
      },
      claude: {
        selected_client_runtime: {
          platform: "linux/amd64",
          claude: { version: RELEASE_CLAUDE_VERSION_OUTPUT, cli_sha256: RELEASE_CLAUDE_CLI_SHA256 },
        },
      },
      browser: { version: "Google Chrome for Testing 152.0", executable_sha256: sha },
    },
  };
  const runtime = {
    schema_version: 1,
    status: "PASS",
    platform: "linux/amd64",
    image_id: clientImageId,
    identity_boundary: "client-image-id",
    user: { uid: 501, gid: 20, root: false },
    node: { version: "v24.0.0", architecture: "x64", executable: "/usr/bin/node", sha256: sha },
    claude: { version: RELEASE_CLAUDE_VERSION_OUTPUT, cli_sha256: RELEASE_CLAUDE_CLI_SHA256 },
    headless_shell: {
      product: "Chrome Headless Shell for Testing",
      version: "Google Chrome for Testing 152.0",
      executable_sha256: sha,
    },
  };
  const receipt = {
    status: "PASS",
    topology: "dual-linux-containers",
    runtime_images: { server_image_id: serverImageId, client_image_id: clientImageId },
    runtime_resources: {
      client_container: "client-1",
      server_container: "server-1",
      client_image_id: clientImageId,
      server_image_id: serverImageId,
      network: "network-1",
      selected_client_runtime: runtime,
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
  assert.equal(validCrossJobPassRuntimeBoundary(receipt, { plan, generatedSkill }), true);
  assert.equal(validCrossJobPassRuntimeBoundary({ ...receipt, topology: "host-client" }, { plan, generatedSkill }), false);
  assert.equal(validCrossJobPassRuntimeBoundary({
    ...receipt,
    runtime_images: { ...receipt.runtime_images, client_image_id: serverImageId },
  }, { plan, generatedSkill }), false);
  assert.equal(validCrossJobPassRuntimeBoundary({
    ...receipt,
    runtime_resources: {
      ...receipt.runtime_resources,
      selected_client_runtime: { ...runtime, platform: "macos" },
    },
  }, { plan, generatedSkill }), false);
  assert.equal(validCrossJobPassRuntimeBoundary({ ...receipt, generated_skill: null }, { plan, generatedSkill }), false);
  assert.equal(validCrossJobPassRuntimeBoundary({
    ...receipt,
    runtime_resources: { ...receipt.runtime_resources, selected_client_runtime: { ...runtime, user: { uid: 0, gid: 0, root: true } } },
  }, { plan, generatedSkill }), false);
});

test("Linux Client browser capability is a closed zero-model runnable receipt", () => {
  const sha = "a".repeat(64);
  const runId = "run-browser-capability";
  const clientContainer = "pltf-client-browser-capability";
  const challenge = crypto.createHash("sha256")
    .update(`${runId}:${clientContainer}:linux-client-browser-capability-v1`)
    .digest("hex");
  const runtimeResources = {
    client_container: clientContainer,
    client_image_id: `sha256:${"b".repeat(64)}`,
    selected_client_runtime: { user: { uid: 501, gid: 20, root: false } },
  };
  const plan = {
    run_id: runId,
    release_inputs: {
      browser: { version: "Google Chrome for Testing 152.0", executable_sha256: sha },
    },
  };
  const result = { schema_version: 1, ok: true, capability: "headless-dom-roundtrip", challenge };
  const capture = { byte_count: 1, sha256: sha, truncated: false };
  const receipt = {
    schema_version: 1,
    status: "PASS",
    code: null,
    kind: "linux-client-headless-dom-roundtrip",
    topology: "dual-linux-containers",
    run_id: runId,
    client_container: clientContainer,
    client_image_id: runtimeResources.client_image_id,
    execution_user: runtimeResources.selected_client_runtime.user,
    home: { path: "/client-home", realpath: "/client-home", present: true, writable: true },
    browser: {
      status: "PRESENT",
      product: "Chrome Headless Shell for Testing",
      version: plan.release_inputs.browser.version,
      executable_sha256: sha,
      code: null,
    },
    runner: {
      relative_path: "tools/test-flow/runtime-support/linux_client_browser_runner.py",
      sha256: sha,
      argument_profile: "chrome-headless-shell-for-testing-local-v1",
    },
    launcher_contract: {
      kind: "docker-cli-exec-to-python-subprocess",
      network_scope: "container-loopback-only",
      docker_exec_count: 1,
      retries: 0,
    },
    probe: {
      origin: "http://127.0.0.1:18765",
      challenge_sha256: challenge,
      result_sha256: crypto.createHash("sha256").update(canonicalJson(result)).digest("hex"),
      launcher_exit_code: 0,
      launcher_signal: null,
      browser_exit_code: 0,
      browser_signal_number: null,
      browser_signal_name: null,
      timed_out: false,
      stdout: capture,
      stderr: capture,
      result_marker: "data-result",
      cleanup: {
        http_server_stopped: true,
        profile_removed: true,
        process_tree: {
          strategy: "posix-process-group-v1",
          session_started: true,
          termination_reason: "RESIDUAL_AFTER_EXIT",
          term_sent: true,
          kill_sent: false,
          parent_reaped: true,
          group_absent: true,
        },
      },
    },
    usage_complete: true,
    invocations: [],
    usage: {
      schema_version: 1,
      input_tokens: 0,
      output_tokens: 0,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 0,
      total_tokens: 0,
      cost_usd: 0,
    },
  };
  const expected = { plan, runtimeResources, runnerSha256: sha };
  assert.equal(validLinuxClientBrowserCapabilityReceipt(receipt, expected), true);

  const mutations = [
    (value) => { delete value.home; },
    (value) => { value.unexpected = true; },
    (value) => { value.home.path = "/root"; },
    (value) => { value.execution_user = { uid: 0, gid: 0, root: true }; },
    (value) => { value.client_image_id = `sha256:${"c".repeat(64)}`; },
    (value) => { value.browser.executable_sha256 = "d".repeat(64); },
    (value) => { value.launcher_contract.retries = 1; },
    (value) => { value.probe.browser_exit_code = 133; },
    (value) => { value.probe.browser_signal_number = 5; value.probe.browser_signal_name = "SIGTRAP"; },
    (value) => { value.probe.cleanup.process_tree.group_absent = false; },
    (value) => { value.probe.cleanup.process_tree.kill_sent = true; value.probe.cleanup.process_tree.term_sent = false; },
    (value) => { value.invocations.push({}); },
    (value) => { value.usage.input_tokens = 1; value.usage.total_tokens = 1; },
  ];
  for (const mutate of mutations) {
    const changed = JSON.parse(JSON.stringify(receipt));
    mutate(changed);
    assert.equal(validLinuxClientBrowserCapabilityReceipt(changed, expected), false);
  }
});

test("Codex Luna PASS boundary rejects identity, effort, Logparse, auth and Skill mutations", () => {
  const passing = passingCodexLunaBoundary();
  assert.equal(validCodexLunaPassBoundary(passing.bundle, passing.expected), true);
  const mutations = [
    ["reasoning effort", (value) => { value.bundle.receipt.reasoning_effort = "high"; }],
    ["CLI identity", (value) => { value.bundle.identity.cli.sha256 = "0".repeat(64); }],
    ["coherent protocol schema digest mutation", (value) => {
      value.bundle.identity.protocol_schema.tree_sha256 = "0".repeat(64);
      value.bundle.security.protocol_schema.tree_sha256 = "0".repeat(64);
    }],
    ["protocol schema manifest digest", (value) => {
      const manifest = Array.from({ length: 401 }, (_, index) => ({ path: `schema-${index}.json`, size: 1, sha256: crypto.createHash("sha256").update(String(index)).digest("hex") }));
      value.bundle.identity.protocol_schema.manifest = manifest;
      value.bundle.security.protocol_schema.manifest = structuredClone(manifest);
    }],
    ["Logparse identity", (value) => { value.bundle.preprocessing.logparse_identity.git_head = "0".repeat(40); }],
    ["Logparse config", (value) => { value.bundle.preprocessing.config.sha256 = "0".repeat(64); }],
    ["preprocessing order", (value) => { value.bundle.preprocessing.cases.reverse(); }],
    ["preprocessing counts", (value) => { value.bundle.preprocessing.cases[2].target_query_invocations = 3; }],
    ["frozen source set", (value) => { value.bundle.preprocessing.cases[3].frozen_target_logs[1].label = "client"; }],
    ["auth account", (value) => { value.bundle.security.auth_isolation.account_id_sha256 = "0".repeat(64); }],
    ["coherent auth transfer broadens secret fields", (value) => {
      value.bundle.identity.auth.transmitted_fields = ["access_token", "account_id", "refresh_token"];
      value.bundle.security.auth_isolation.transmitted_fields = ["access_token", "account_id", "refresh_token"];
    }],
    ["Skill tree", (value) => { value.bundle.skill.package_tree_sha256 = "0".repeat(64); }],
    ["generation final", (value) => { value.bundle.skill.generation_final_sha256 = "0".repeat(64); }],
    ["diagnosis final", (value) => { value.bundle.receipt.diagnoses[0].result_sha256 = "0".repeat(64); }],
    ["generation scope", (value) => { value.bundle.skill.generation_scope_audit.oracle_accesses = 1; }],
    ["diagnosis scope and matching aggregate", (value) => {
      value.bundle.receipt.diagnoses[1].scope_audit.logparse_invocations = 1;
      value.bundle.security.oracle_and_logparse_scope.logparse_invocations_during_diagnosis = 1;
    }],
    ["manifest path traversal", (value) => { value.bundle.callManifest.records[0].trace.path = "payload/../source/trace.jsonl"; }],
    ["manifest path base", (value) => { value.bundle.callManifest.path_base = "gate-root"; }],
    ["permission profile bytes and hash changed coherently", (value) => {
      const profile = value.bundle.ledger.calls[0].process.app_server.permission_profile;
      profile.bytes_utf8 += "\n# tampered\n";
      profile.byte_count = Buffer.byteLength(profile.bytes_utf8);
      profile.sha256 = crypto.createHash("sha256").update(profile.bytes_utf8).digest("hex");
      const encode = (item) => Array.isArray(item)
        ? `[${item.map((entry) => encode(entry)).join(",")}]`
        : item !== null && typeof item === "object"
          ? `{${Object.keys(item).sort().map((key) => `${JSON.stringify(key)}:${encode(item[key])}`).join(",")}}`
          : JSON.stringify(item);
      value.bundle.security.permission_profiles.call_receipts[0].receipt_sha256 = crypto.createHash("sha256").update(encode(profile)).digest("hex");
    }],
    ["permission profile aggregate", (value) => { value.bundle.security.permission_profiles.call_receipts[0].receipt_sha256 = "0".repeat(64); }],
    ["app-server turn outbound receipt", (value) => { value.bundle.ledger.calls[0].process.app_server.outbound[7].params_sha256 = "0".repeat(64); }],
    ["forbidden-read target substitution", (value) => { value.bundle.ledger.calls[0].process.app_server.preflight.forbidden_reads[1].path_sha256 = "0".repeat(64); }],
    ["external login safe stub", (value) => { value.bundle.ledger.calls[0].process.app_server.login.credential_returned = true; }],
    ["CODEX_HOME manifest", (value) => { value.bundle.ledger.calls[0].process.app_server.codex_home.manifest[0].sha256 = "0".repeat(64); }],
    ["producer and consumer secret scans cannot be empty", (value) => {
      value.bundle.security.artifact_secret_scan.scanned_files = 0;
      value.bundle.consumer.secret_scan.scanned_files = 0;
    }],
    ["durable generated package manifest", (value) => { value.bundle.consumer.generated_package.files[0].sha256 = "0".repeat(64); }],
    ["producer durable generated package receipt", (value) => { value.bundle.skill.durable_package.manifest[0].sha256 = "0".repeat(64); }],
    ["coherent producer usage, budget, and result mutation", (value) => {
      const call = value.bundle.ledger.calls[2];
      call.usage.input_tokens += 1;
      call.usage.total_tokens += 1;
      call.usage.equivalent_usd_upper_bound = Math.ceil((call.usage.equivalent_usd_upper_bound + 0.000001) * 1_000_000) / 1_000_000;
      value.bundle.usageReceipts[2].usage = structuredClone(call.usage);
      const changedBudget = buildPosthocBudgetReceipt({ calls: value.bundle.ledger.calls, usageComplete: true });
      value.bundle.budget = changedBudget;
      value.bundle.receipt.posthoc_budget = {
        exception_id: changedBudget.exception_id,
        status: changedBudget.status,
        aggregate: changedBudget.aggregate,
        checks: changedBudget.checks,
      };
    }],
    ["coherent producer trace and manifest hash mutation", (value) => {
      const changedTrace = crypto.createHash("sha256").update("coherently changed trace").digest("hex");
      value.bundle.ledger.calls[5].process.app_server.trace_sha256 = changedTrace;
      value.bundle.callManifest.records[5].trace.sha256 = changedTrace;
    }],
    ["duplicate thread", (value) => {
      value.bundle.ledger.calls[4].thread_id = value.bundle.ledger.calls[3].thread_id;
      value.bundle.ledger.calls[4].terminal.thread_id = value.bundle.ledger.calls[3].thread_id;
      value.bundle.callManifest.records[4].thread_id = value.bundle.ledger.calls[3].thread_id;
      value.bundle.receipt.diagnoses[3].thread_id = value.bundle.ledger.calls[3].thread_id;
    }],
    ["sensitive inherited environment", (value) => {
      value.bundle.identity.environment.inherited_keys.push("OPENAI_API_KEY");
      value.bundle.security.environment.inherited_keys.push("OPENAI_API_KEY");
    }],
    ["failed ledger call", (value) => { value.bundle.ledger.calls[4].status = "FAIL"; }],
  ];
  for (const [label, mutate] of mutations) {
    const changed = JSON.parse(JSON.stringify(passing));
    mutate(changed);
    assert.equal(validCodexLunaPassBoundary(changed.bundle, changed.expected), false, label);
  }
});

test("Windows pytest selects the shortest safe default and honors an absolute override", () => {
  const temporaryDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-pytest-boundary-"));
  const longAttemptRoot = path.join(
    temporaryDirectory,
    "codex-worktrees",
    "a-very-long-worktree-name-that-must-not-prefix-pytest-scratch",
  );
  try {
    const ordinary = pytestScratchBoundary({
      platform: "win32",
      temporaryDirectory,
      repoRoot: longAttemptRoot,
      attemptRoot: longAttemptRoot,
      isolatedAgent: false,
      configuredWindowsDirectory: null,
    });
    const isolated = pytestScratchBoundary({
      platform: "win32",
      temporaryDirectory,
      repoRoot: longAttemptRoot,
      attemptRoot: longAttemptRoot,
      isolatedAgent: true,
      configuredWindowsDirectory: null,
    });
    assert.equal(ordinary, path.resolve(temporaryDirectory));
    assert.equal(isolated, ordinary);
    assert.equal(ordinary.includes("codex-worktrees"), false);

    const shortRepoRoot = path.join(temporaryDirectory, "r");
    assert.equal(
      pytestScratchBoundary({
        platform: "win32",
        temporaryDirectory: path.join(temporaryDirectory, "long-system-temp-name"),
        repoRoot: shortRepoRoot,
        attemptRoot: longAttemptRoot,
        isolatedAgent: false,
        configuredWindowsDirectory: null,
      }),
      path.resolve(shortRepoRoot, ".tmp", "p"),
    );
    const configured = path.join(temporaryDirectory, "configured");
    assert.equal(
      pytestScratchBoundary({
        platform: "win32",
        temporaryDirectory,
        repoRoot: longAttemptRoot,
        attemptRoot: longAttemptRoot,
        configuredWindowsDirectory: configured,
      }),
      path.resolve(configured),
    );
    assert.throws(
      () => pytestScratchBoundary({
        platform: "win32",
        temporaryDirectory,
        repoRoot: longAttemptRoot,
        attemptRoot: longAttemptRoot,
        configuredWindowsDirectory: "relative-scratch",
      }),
      /PYTEST_WINDOWS_SCRATCH_ROOT_ABSOLUTE_REQUIRED/,
    );

    const scratch = fs.mkdtempSync(path.join(ordinary, "p-"));
    assert.equal(path.dirname(scratch), ordinary);
    removeTreeWritable(scratch, ordinary);
    assert.equal(fs.existsSync(scratch), false);
    assert.throws(
      () => removeTreeWritable(ordinary, ordinary),
      (error) => error.code === "CLEANUP_PATH_OUTSIDE_ATTEMPT",
    );
  } finally {
    fs.rmSync(temporaryDirectory, { recursive: true, force: true });
  }
});

test("Windows pytest base temp uses an extended-length path without moving scratch", () => {
  assert.equal(
    pytestBaseTempPath("C:\\workspace\\.tmp\\p\\p-123456", "win32"),
    "\\\\?\\C:\\workspace\\.tmp\\p\\p-123456",
  );
  assert.equal(
    pytestBaseTempPath("\\\\server\\share\\p-123456", "win32"),
    "\\\\?\\UNC\\server\\share\\p-123456",
  );
  assert.equal(pytestBaseTempPath("/tmp/p-123456", "linux"), "/tmp/p-123456");
});

test("non-Windows pytest scratch keeps the attempt root boundary", () => {
  const attemptRoot = path.join(os.tmpdir(), "test-flow-attempt-boundary");
  assert.equal(
    pytestScratchBoundary({
      platform: "linux",
      temporaryDirectory: path.join(os.tmpdir(), "must-not-be-used"),
      attemptRoot,
    }),
    path.resolve(attemptRoot),
  );
});

test("a narrow affected selection runs before the full suite", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-affected-narrow-"));
  try {
    for (const name of ["a", "b", "c", "d"]) writeTest(path.join(root, "tests", "deterministic", "unit", `test_${name}.py`));
    const selection = planAffectedSelection(root, ["tests/deterministic/unit/test_a.py"]);
    assert.deepEqual(selection.selectors, ["tests/deterministic/unit/test_a.py"]);
    assert.equal(selection.covered_test_files, 1);
    assert.equal(selection.total_test_files, 4);
    assert.equal(selection.defer_to_full, false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("a broad affected selection is folded into the following full suite", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-affected-broad-"));
  try {
    for (const name of ["a", "b", "c", "d"]) writeTest(path.join(root, "tests", "deterministic", "unit", `test_${name}.py`));
    fs.writeFileSync(path.join(root, "tests", "deterministic", "unit", "conftest.py"), "VALUE = 1\n");
    const selection = planAffectedSelection(root, ["tests/deterministic/unit/conftest.py"]);
    assert.deepEqual(selection.selectors, ["tests/deterministic/unit"]);
    assert.equal(selection.covered_test_files, 4);
    assert.equal(selection.total_test_files, 4);
    assert.equal(selection.defer_to_full, true);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("REST guide and OpenAPI snapshot changes select the browser contract regression", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-affected-rest-guide-"));
  try {
    for (const name of ["a", "b", "c", "d"]) writeTest(path.join(root, "tests", "deterministic", "unit", `test_${name}.py`));
    writeTest(path.join(root, "tests", "deterministic", "contracts", "test_contract.py"));
    const webApiTest = path.join(root, "tests", "deterministic", "unit", "interfaces", "test_web_api.py");
    writeTest(webApiTest);

    const guideSelection = planAffectedSelection(root, ["docs/browser-rest-api.md"]);
    assert.deepEqual(guideSelection.selectors, ["tests/deterministic/unit/interfaces/test_web_api.py"]);
    assert.equal(guideSelection.covered_test_files, 1);
    assert.equal(guideSelection.defer_to_full, false);

    const snapshotSelection = planAffectedSelection(root, ["schemas/v2/web-api.openapi.snapshot.json"]);
    assert.deepEqual(snapshotSelection.selectors, [
      "tests/deterministic/contracts",
      "tests/deterministic/unit/interfaces/test_web_api.py",
    ]);
    assert.equal(snapshotSelection.covered_test_files, 2);
    assert.equal(snapshotSelection.defer_to_full, false);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("loopback denial is classified as infrastructure BLOCKED before pytest", () => {
  const receipt = probeLoopbackCapability(
    { command: "/frozen/python", interpreterPrefix: [] },
    "/repository",
    {},
    () => ({ status: 1, signal: null, stdout: "", stderr: "PermissionError: [Errno 1] Operation not permitted" }),
  );
  assert.deepEqual(receipt, {
    schema_version: 1,
    status: "BLOCKED",
    capability: "ipv4-loopback-bind",
    exit_code: 1,
    signal: null,
    error_code: null,
    failure_code: "LOOPBACK_BIND_PERMISSION_DENIED",
  });
});

test("pytest cannot pass with zero executed tests or an all-skipped result", () => {
  assert.deepEqual(evaluatePytestSummary({ executed: 0, passed: 0, skipped: 0 }), {
    status: "FAIL",
    failure_domain: "CONTRACT",
    code: "PYTEST_NO_EXECUTED_TESTS",
  });
  assert.equal(evaluatePytestSummary({ executed: 0, passed: 0, skipped: 7 }).code, "PYTEST_NO_EXECUTED_TESTS");
});

test("pytest skip and minimum-pass policies are enforced from parsed JUnit", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-junit-"));
  try {
    const junit = path.join(root, "pytest.xml");
    fs.writeFileSync(junit, '<testsuites tests="4" failures="0" errors="0" skipped="1"></testsuites>\n');
    const summary = parseJUnitSummary(junit);
    assert.deepEqual(summary, { schema_version: 2, tests: 4, passed: 3, failures: 0, errors: 0, skipped: 1, executed: 3 });
    assert.equal(evaluatePytestSummary(summary, { minPassed: 4, skipPolicy: "allow-explicit" }).code, "PYTEST_MIN_PASSED_NOT_MET");
    assert.equal(evaluatePytestSummary(summary, { minPassed: 3, skipPolicy: "forbid" }).code, "PYTEST_SKIP_FORBIDDEN");
    assert.equal(evaluatePytestSummary(summary, { minPassed: 3, skipPolicy: "allow-explicit" }).status, "PASS");
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("pytest's testsuites wrapper aggregates inner suite counters", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-junit-wrapper-"));
  try {
    const junit = path.join(root, "pytest.xml");
    fs.writeFileSync(junit, '<testsuites name="pytest tests"><testsuite name="unit" tests="2" failures="0" errors="0" skipped="0"></testsuite><testsuite name="journey" tests="3" failures="0" errors="0" skipped="1"></testsuite></testsuites>\n');
    assert.deepEqual(parseJUnitSummary(junit), {
      schema_version: 2,
      tests: 5,
      passed: 4,
      failures: 0,
      errors: 0,
      skipped: 1,
      executed: 4,
    });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("a parseable failing JUnit result is materialized as a summary", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-junit-failure-summary-"));
  try {
    fs.writeFileSync(path.join(root, "pytest.xml"), '<testsuites tests="1" failures="1" errors="0" skipped="0"></testsuites>\n');
    const summary = materializePytestSummary(root);
    assert.deepEqual(summary, {
      schema_version: 2,
      tests: 1,
      passed: 0,
      failures: 1,
      errors: 0,
      skipped: 0,
      executed: 1,
    });
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(root, "pytest-summary.json"), "utf8")), summary);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("failed Gates index existing declared evidence while PASS still requires every file", () => {
  const attemptRoot = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-failure-evidence-"));
  try {
    const stage = { id: "real.skill-generation" };
    const gatePlan = { id: "real.agent.skill-generation" };
    const gate = { evidence: ["pytest.xml", "pytest-summary.json", "scenario-evaluation-audit.json"] };
    const gateRoot = path.join(attemptRoot, "payload", "stages", stage.id, "gates", gatePlan.id);
    fs.mkdirSync(gateRoot, { recursive: true });
    fs.writeFileSync(path.join(gateRoot, "pytest.xml"), "<testsuites tests=\"1\" failures=\"1\"/>\n");
    fs.writeFileSync(path.join(gateRoot, "scenario-evaluation-audit.json"), '{"schema_version":1,"status":"FAIL"}\n');

    const failed = applyGateEvidenceContract({
      actionResult: { status: "FAIL", failure_domain: "CONTRACT", code: "PYTEST_FAILED" },
      gate,
      gatePlan,
      stage,
      attemptRoot,
    });
    assert.equal(failed.result.status, "FAIL");
    assert.deepEqual(failed.evidence.map((item) => path.basename(item.path)), ["pytest.xml", "scenario-evaluation-audit.json"]);

    const incompletePass = applyGateEvidenceContract({
      actionResult: { status: "PASS" },
      gate,
      gatePlan,
      stage,
      attemptRoot,
    });
    assert.equal(incompletePass.result.status, "ERROR");
    assert.equal(incompletePass.result.code, "GATE_REQUIRED_EVIDENCE_MISSING");

    fs.writeFileSync(path.join(gateRoot, "pytest-summary.json"), '{"schema_version":2}\n');
    const completePass = applyGateEvidenceContract({
      actionResult: { status: "PASS" },
      gate,
      gatePlan,
      stage,
      attemptRoot,
    });
    assert.equal(completePass.result.status, "PASS");
    assert.equal(completePass.evidence.length, 3);
  } finally {
    fs.rmSync(attemptRoot, { recursive: true, force: true });
  }
});

test("failed isolated invocation usage is collected as evidence without converting the Gate to PASS", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-failed-model-usage-"));
  try {
    const usageRoot = path.join(root, "model-usage");
    fs.mkdirSync(usageRoot);
    const usage = {
      schema_version: 1,
      input_tokens: 24411,
      output_tokens: 97144,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 68736,
      total_tokens: 190291,
      cost_usd: 3.398151,
    };
    fs.writeFileSync(path.join(usageRoot, "failed.json"), `${JSON.stringify({
      schema_version: 3,
      invocation_id: "isolated-agent:failed",
      class: "isolated-agent",
      workflow: "skill-generation",
      environment_policy: {
        schema_version: 1,
        version: ISOLATED_AGENT_ENV_POLICY_VERSION,
        provider_auth_source: "audited-settings-file",
        session_credentials: "NONE",
        inbound: environmentKeySummary({ PATH: "/bin" }),
        claude_process: environmentKeySummary({ PATH: "/bin" }),
      },
      tool_trace_audit: null,
      effective_model: "test-model",
      effective_caps: { max_turns: 12, max_total_tokens: 1000000, max_budget_usd: 3, hard_timeout_seconds: 900 },
      usage_complete: true,
      usage,
      terminal: { subtype: "error_max_budget_usd", is_error: true },
      turns: 7,
      wrapper_outcome: { schema_version: 1, status: "FAIL", code: "WRAPPER_MODEL_TERMINAL_INVALID" },
      hard_cap_enforcement: {},
      timed_out: false,
      process: { exit_code: 1, signal: null },
    })}\n`);
    const summary = collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation");
    assert.equal(summary.status, "PASS");
    assert.deepEqual(summary.usage, usage);
    assert.equal(summary.invocations.length, 1);
    assert.equal(summary.invocations[0].wrapper_outcome.status, "FAIL");
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(root, "model-usage.json"), "utf8")), summary);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("missing failed invocation usage remains incomplete instead of hiding the original Gate failure", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-missing-model-usage-"));
  try {
    assert.throws(
      () => collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation"),
      /ISOLATED_MODEL_USAGE_RECEIPT_MISSING/,
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("isolated usage collection requires the child-env and sealed runtime binding for an output cap", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-output-cap-receipt-"));
  try {
    const usageRoot = path.join(root, "model-usage");
    fs.mkdirSync(usageRoot);
    const usage = {
      schema_version: 1,
      input_tokens: 10,
      output_tokens: 20,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 0,
      total_tokens: 30,
      cost_usd: 0.01,
    };
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify({
      schema_version: 3,
      invocation_id: "isolated-agent:capped",
      class: "isolated-agent",
      workflow: "skill-generation",
      environment_policy: {
        schema_version: 1,
        version: ISOLATED_AGENT_ENV_POLICY_VERSION,
        provider_auth_source: "audited-settings-file",
        session_credentials: "NONE",
        inbound: environmentKeySummary({ HOME: "/home/test", PATH: "/bin" }),
        claude_process: environmentKeySummary({ CLAUDE_CODE_MAX_OUTPUT_TOKENS: "64000", HOME: "/home/test", PATH: "/bin" }),
      },
      tool_trace_audit: passingSkillTraceAudit(),
      effective_model: "test-model",
      effective_caps: { max_turns: 12, max_total_tokens: 1000000, max_output_tokens: 64000, max_budget_usd: 10, hard_timeout_seconds: 900 },
      usage_complete: true,
      usage,
      terminal: { subtype: "success", is_error: false },
      turns: 1,
      wrapper_outcome: { schema_version: 1, status: "PASS", code: null },
      hard_cap_enforcement: {
        total_tokens: "terminal-usage-postcondition:input+output+cache_creation_input+cache_read_input",
        max_output_tokens: ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT,
      },
      timed_out: false,
      process: { exit_code: 0, signal: null },
    })}\n`);
    const summary = collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation");
    assert.equal(summary.status, "PASS");
    assert.equal(summary.invocations[0].hard_cap_enforcement.max_output_tokens, ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT);
    const invalidReceipt = structuredClone(summary.invocations[0]);
    invalidReceipt.hard_cap_enforcement.max_output_tokens = "terminal-model-usage-echo";
    fs.rmSync(path.join(root, "model-usage.json"));
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(invalidReceipt)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation"),
      /ISOLATED_MODEL_ENVIRONMENT_POLICY_RECEIPT_INVALID/,
    );
    const legacyReceipt = structuredClone(summary.invocations[0]);
    legacyReceipt.observed_request_limits = [64000];
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(legacyReceipt)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation"),
      /ISOLATED_MODEL_USAGE_RECEIPT_INVALID/,
    );
    const invalidToolAudit = structuredClone(summary.invocations[0]);
    invalidToolAudit.tool_trace_audit.package.files[0].sha256 = "0".repeat(64);
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(invalidToolAudit)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation"),
      /ISOLATED_MODEL_TOOL_TRACE_AUDIT_INVALID/,
    );
    const failedToolAudit = structuredClone(summary.invocations[0]);
    failedToolAudit.tool_trace_audit = {
      schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
      status: "FAIL",
      workflow: "skill-generation",
      code: "SKILL_TRACE_TOOL_RESULT_ERROR",
    };
    failedToolAudit.wrapper_outcome = { schema_version: 1, status: "FAIL", code: "WRAPPER_SKILL_TRACE_INVALID" };
    failedToolAudit.process.exit_code = 1;
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(failedToolAudit)}\n`);
    const failedSummary = collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation");
    assert.equal(failedSummary.invocations[0].tool_trace_audit.schema_version, SKILL_GENERATION_TRACE_SCHEMA_VERSION);
    fs.rmSync(path.join(root, "model-usage.json"));
    failedToolAudit.tool_trace_audit.unexpected = true;
    fs.writeFileSync(path.join(usageRoot, "capped.json"), `${JSON.stringify(failedToolAudit)}\n`);
    assert.throws(
      () => collectIsolatedModelUsage({ gateRoot: root }, "real-skill-generation"),
      /ISOLATED_MODEL_TOOL_TRACE_AUDIT_INVALID/,
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
