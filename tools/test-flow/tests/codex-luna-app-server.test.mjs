import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import test from "node:test";

import {
  buildCodexLunaAccountReadRequest,
  buildCodexLunaAppServerArguments,
  buildCodexLunaAppServerEvidenceSummary,
  buildCodexLunaInitializedNotification,
  buildCodexLunaInitializeRequest,
  buildCodexLunaIsolatedConfig,
  buildCodexLunaPermissionProfileListRequest,
  buildCodexLunaSkillsListRequest,
  buildCodexLunaThreadStartRequest,
  buildCodexLunaTurnStartRequest,
  CODEX_LUNA_APP_SERVER_PROTOCOL_VERSION,
  CODEX_LUNA_APP_SERVER_REQUEST_IDS,
  CODEX_LUNA_DISABLED_FEATURES,
  CODEX_LUNA_RAW_RESPONSE_ITEM_SANITIZER_FIELDS,
  CODEX_LUNA_RAW_RESPONSE_ITEM_TYPES_ALLOWED,
  CODEX_LUNA_RAW_SHELL_FUNCTION_NAMES,
  CODEX_LUNA_SYSTEM_SKILL_NAMES,
  codexLunaPermissionProfileId,
  parseCodexLunaAppServerTranscript,
  writeExternalChatgptAuthLoginRequest,
} from "../runtime-support/codex-luna-app-server.mjs";
import {
  CODEX_LUNA_MODEL,
  CODEX_LUNA_REASONING_EFFORT,
} from "../runtime-support/codex-luna-contract.mjs";

const PINNED_CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex";
const WORKSPACE = "/private/tmp/test-flow-codex-luna/invocation";
const SKILL = `${WORKSPACE}/.agents/skills/diagnose-rpc-timeout/SKILL.md`;
const CODEX_HOME = "/private/tmp/test-flow-codex-luna/codex-home";
const SHELL_HOME = `${WORKSPACE}/.shell-home`;
const THREAD_ID = "0198-thread-one";
const TURN_ID = "0198-turn-one";
const SECRET = "secret-access-token-value-that-must-never-leak";

function usageBreakdown(overrides = {}) {
  return {
    totalTokens: 120,
    inputTokens: 100,
    cachedInputTokens: 40,
    cacheWriteInputTokens: 0,
    outputTokens: 20,
    reasoningOutputTokens: 5,
    ...overrides,
  };
}

function systemSkillPath(name, codexHome = CODEX_HOME) {
  return path.join(codexHome, "skills", ".system", name, "SKILL.md");
}

function buildProfile(mode = "diagnosis", overrides = {}) {
  return buildCodexLunaIsolatedConfig({
    workspaceRoot: WORKSPACE,
    skillPath: SKILL,
    codexHome: CODEX_HOME,
    shellHome: SHELL_HOME,
    mode,
    ...overrides,
  });
}

function parse(messages = transcript(), overrides = {}) {
  return parseCodexLunaAppServerTranscript(messages, {
    workspaceRoot: WORKSPACE,
    skillPath: SKILL,
    codexHome: CODEX_HOME,
    mode: "diagnosis",
    ...overrides,
  });
}

function tokenUsage() {
  return {
    total: usageBreakdown(),
    last: usageBreakdown(),
    modelContextWindow: 400_000,
  };
}

function turn(status) {
  return {
    id: TURN_ID,
    status,
    items: [],
    itemsView: "full",
    error: null,
    startedAt: 1,
    completedAt: status === "completed" ? 2 : null,
    durationMs: status === "completed" ? 1_000 : null,
  };
}

function rawResponseItem(item, overrides = {}) {
  return {
    method: "rawResponseItem/completed",
    params: { threadId: THREAD_ID, turnId: TURN_ID, item, ...overrides },
  };
}

function transcript() {
  const profileId = codexLunaPermissionProfileId("diagnosis");
  const systemSkills = CODEX_LUNA_SYSTEM_SKILL_NAMES.map((name) => ({
    name,
    description: `${name} system skill`,
    path: systemSkillPath(name),
    scope: "system",
    enabled: false,
  }));
  return [
    {
      id: CODEX_LUNA_APP_SERVER_REQUEST_IDS.initialize,
      result: {
        userAgent: "codex_app_server_rs/0.149.0-alpha.4.1",
        codexHome: CODEX_HOME,
        platformFamily: "unix",
        platformOs: "macos",
      },
    },
    { id: CODEX_LUNA_APP_SERVER_REQUEST_IDS.login, result: { type: "chatgptAuthTokens" } },
    { method: "account/login/completed", params: { loginId: null, success: true, error: null, onboardingEntrypoint: null } },
    { method: "account/updated", params: { authMode: "chatgptAuthTokens", planType: "plus" } },
    { method: "account/rateLimits/updated", params: { rateLimits: {} } },
    {
      id: CODEX_LUNA_APP_SERVER_REQUEST_IDS.accountRead,
      result: { account: { type: "chatgpt", email: null, planType: "plus" }, requiresOpenaiAuth: true },
    },
    {
      id: CODEX_LUNA_APP_SERVER_REQUEST_IDS.permissionProfileList,
      result: { data: [{ id: profileId, description: "test", allowed: true }], nextCursor: null },
    },
    {
      id: CODEX_LUNA_APP_SERVER_REQUEST_IDS.skillsList,
      result: {
        data: [{
          cwd: WORKSPACE,
          skills: [
            ...systemSkills,
            { name: "diagnose-rpc-timeout", description: "intended", path: SKILL, scope: "repo", enabled: true },
          ],
          errors: [],
        }],
      },
    },
    {
      id: CODEX_LUNA_APP_SERVER_REQUEST_IDS.threadStart,
      result: {
        thread: {
          id: THREAD_ID,
          sessionId: THREAD_ID,
          forkedFromId: null,
          parentThreadId: null,
          ephemeral: true,
          path: null,
          source: "appServer",
          modelProvider: "openai",
          cwd: WORKSPACE,
          cliVersion: "0.149.0-alpha.4.1",
          turns: [],
        },
        model: CODEX_LUNA_MODEL,
        modelProvider: "openai",
        serviceTier: null,
        cwd: WORKSPACE,
        runtimeWorkspaceRoots: [],
        instructionSources: [],
        approvalPolicy: "never",
        approvalsReviewer: "user",
        sandbox: { type: "readOnly", networkAccess: false },
        activePermissionProfile: { id: profileId, extends: null },
        reasoningEffort: CODEX_LUNA_REASONING_EFFORT,
        multiAgentMode: "explicitRequestOnly",
      },
    },
    { method: "thread/started", params: { thread: { id: THREAD_ID } } },
    { id: CODEX_LUNA_APP_SERVER_REQUEST_IDS.turnStart, result: { turn: turn("inProgress") } },
    { method: "turn/started", params: { threadId: THREAD_ID, turn: turn("inProgress") } },
    {
      method: "item/started",
      params: {
        threadId: THREAD_ID,
        turnId: TURN_ID,
        startedAtMs: 10,
        item: { type: "commandExecution", id: "cmd-1", command: "sed -n '1,20p' input/request.json", cwd: WORKSPACE, status: "inProgress" },
      },
    },
    {
      method: "item/completed",
      params: {
        threadId: THREAD_ID,
        turnId: TURN_ID,
        completedAtMs: 20,
        item: {
          type: "commandExecution",
          id: "cmd-1",
          command: "sed -n '1,20p' input/request.json",
          cwd: WORKSPACE,
          status: "completed",
          exitCode: 0,
          durationMs: 10,
          aggregatedOutput: "safe output",
          commandActions: [],
          processId: null,
          pluginId: null,
          scriptPath: null,
          source: "agent",
        },
      },
    },
    { method: "turn/diff/updated", params: { threadId: THREAD_ID, turnId: TURN_ID, diff: "ignored unified diff" } },
    {
      method: "item/started",
      params: {
        threadId: THREAD_ID,
        turnId: TURN_ID,
        startedAtMs: 30,
        item: { type: "agentMessage", id: "msg-1", text: "", phase: "final_answer" },
      },
    },
    {
      method: "item/completed",
      params: {
        threadId: THREAD_ID,
        turnId: TURN_ID,
        completedAtMs: 40,
        item: { type: "agentMessage", id: "msg-1", text: "Diagnosis complete.", phase: "final_answer", memoryCitation: null, delivery: null },
      },
    },
    rawResponseItem({ type: "reasoning", id: "raw-reasoning", summary: [], encrypted_content: null }),
    rawResponseItem({ type: "message", id: "raw-message", role: "assistant", content: [], phase: "final_answer" }),
    rawResponseItem({ type: "function_call", id: "raw-function", name: "shell_command", arguments: "{}", call_id: "raw-shell-function-1" }),
    rawResponseItem({ type: "function_call_output", id: "raw-function-output", call_id: "raw-shell-function-1", output: { type: "text", text: "ignored" } }),
    rawResponseItem({ type: "local_shell_call", id: "raw-local-shell", call_id: "raw-local-shell-1", status: "completed", action: { type: "exec", command: ["pwd"], timeout_ms: null, working_directory: null, env: null, user: null } }),
    rawResponseItem({ type: "function_call_output", id: "raw-local-shell-output", call_id: "raw-local-shell-1", output: { type: "text", text: "ignored" } }),
    {
      method: "rawResponse/completed",
      params: {
        threadId: THREAD_ID,
        turnId: TURN_ID,
        responseId: "response-1",
        usage: usageBreakdown({ totalTokens: 70, inputTokens: 60, cachedInputTokens: 20, outputTokens: 10, reasoningOutputTokens: 3 }),
      },
    },
    {
      method: "rawResponse/completed",
      params: {
        threadId: THREAD_ID,
        turnId: TURN_ID,
        responseId: "response-2",
        usage: usageBreakdown({ totalTokens: 50, inputTokens: 40, cachedInputTokens: 20, outputTokens: 10, reasoningOutputTokens: 2 }),
      },
    },
    { method: "thread/tokenUsage/updated", params: { threadId: THREAD_ID, turnId: TURN_ID, tokenUsage: tokenUsage() } },
    { method: "turn/completed", params: { threadId: THREAD_ID, turn: turn("completed") } },
  ];
}

test("isolated config uses only a named least-privilege profile and binds one absolute SKILL.md", () => {
  const generation = buildProfile("generation");
  const diagnosis = buildProfile("diagnosis");

  assert.equal(generation.workspace_access, "write");
  assert.equal(diagnosis.workspace_access, "read");
  assert.equal(generation.root_access, "deny");
  assert.equal(generation.minimal_access, "read");
  assert.equal(generation.network_enabled, false);
  assert.equal(generation.skill_path, SKILL);
  assert.match(generation.config_toml, /default_permissions = "test-flow-codex-luna-generation"/);
  assert.match(generation.config_toml, /":root" = "deny"/);
  assert.match(generation.config_toml, /":minimal" = "read"/);
  assert.match(generation.config_toml, /\[permissions\.test-flow-codex-luna-generation\.filesystem\.":workspace_roots"\]\n"\." = "write"/);
  assert.match(diagnosis.config_toml, /\[permissions\.test-flow-codex-luna-diagnosis\.filesystem\.":workspace_roots"\]\n"\." = "read"/);
  assert.match(generation.config_toml, /\[permissions\.test-flow-codex-luna-generation\.network\]\nenabled = false/);
  assert.match(generation.config_toml, /\[\[skills\.config\]\]\npath = "\/private\/tmp\/test-flow-codex-luna\/invocation\/\.agents\/skills\/diagnose-rpc-timeout\/SKILL\.md"\nenabled = true/);
  for (const name of CODEX_LUNA_SYSTEM_SKILL_NAMES) {
    const configuredPath = systemSkillPath(name).replaceAll("/", "\\/").replaceAll(".", "\\.");
    assert.match(generation.config_toml, new RegExp(`\\[\\[skills\\.config\\]\\]\\npath = "${configuredPath}"\\nenabled = false`));
  }
  assert.match(generation.config_toml, /\[shell_environment_policy\]\ninherit = "none"\nignore_default_excludes = false/);
  assert.match(generation.config_toml, /\[shell_environment_policy\.set\]\nPATH = "\/usr\/bin:\/bin:\/usr\/sbin:\/sbin"\nLANG = "C\.UTF-8"\nHOME = "\/private\/tmp\/test-flow-codex-luna\/invocation\/\.shell-home"\nPYTHONDONTWRITEBYTECODE = "1"\nPYTHONNOUSERSITE = "1"/);
  assert.equal(generation.shell_environment.codex_home_forwarded, false);
  assert.deepEqual(generation.shell_environment.keys, ["PATH", "LANG", "HOME", "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE"]);
  assert.doesNotMatch(generation.config_toml, /CODEX_HOME|\[tools\]/);
  assert.doesNotMatch(generation.config_toml, /sandbox_mode|sandbox_workspace_write/);
  assert.equal(generation.config_byte_count, Buffer.byteLength(generation.config_toml));
  assert.match(generation.config_sha256, /^[a-f0-9]{64}$/);

  assert.throws(
    () => buildCodexLunaIsolatedConfig({ workspaceRoot: "/", skillPath: "/SKILL.md", codexHome: CODEX_HOME, mode: "generation" }),
    (error) => error.code === "CODEX_LUNA_APP_SERVER_WORKSPACE_TOO_BROAD",
  );
  assert.throws(
    () => buildCodexLunaIsolatedConfig({ workspaceRoot: WORKSPACE, skillPath: `${WORKSPACE}/skill`, codexHome: CODEX_HOME, mode: "generation" }),
    (error) => error.code === "CODEX_LUNA_APP_SERVER_SKILL_PATH_INVALID",
  );
  assert.throws(
    () => buildCodexLunaIsolatedConfig({ workspaceRoot: WORKSPACE, skillPath: "/private/tmp/other/SKILL.md", codexHome: CODEX_HOME, mode: "generation" }),
    (error) => error.code === "CODEX_LUNA_APP_SERVER_SKILL_OUTSIDE_WORKSPACE",
  );
  assert.throws(
    () => buildCodexLunaIsolatedConfig({ workspaceRoot: WORKSPACE, skillPath: SKILL, codexHome: `${WORKSPACE}/codex-home`, mode: "generation" }),
    (error) => error.code === "CODEX_LUNA_APP_SERVER_PRIVATE_PATH_OVERLAP",
  );
});

test("client config binds one required loopback Streamable HTTP MCP server and the exact seven-tool allowlist", () => {
  const client = buildProfile("client", {
    mcpServer: { name: "problem-locator", url: "http://127.0.0.1:43123/mcp" },
  });
  assert.equal(client.workspace_access, "read");
  assert.equal(client.network_enabled, true);
  assert.deepEqual(client.mcp_server.enabled_tools, [
    "problem_locator_create_case",
    "problem_locator_prepare_attachment",
    "problem_locator_submit_supplement",
    "problem_locator_get_case",
    "problem_locator_resume_case",
    "problem_locator_cancel_case",
    "problem_locator_list_artifacts",
  ]);
  assert.match(client.config_toml, /\[mcp_servers\."problem-locator"\]/);
  assert.match(client.config_toml, /url = "http:\/\/127\.0\.0\.1:43123\/mcp"/);
  assert.match(client.config_toml, /required = true/);
  assert.match(client.config_toml, /default_tools_approval_mode = "approve"/);
  assert.match(client.config_toml, /\[permissions\.test-flow-codex-luna-client\.network\]\nenabled = true/);
  assert.throws(
    () => buildProfile("client", { mcpServer: { name: "problem-locator", url: "http://localhost:43123/mcp" } }),
    (error) => error.code === "CODEX_LUNA_APP_SERVER_MCP_INVALID",
  );
  assert.throws(
    () => buildProfile("client", { mcpServer: { name: "problem-locator", url: "http://127.0.0.1:43123/mcp", enabled_tools: ["problem_locator_get_case"] } }),
    (error) => error.code === "CODEX_LUNA_APP_SERVER_MCP_TOOLS_INVALID",
  );
});

test("client transcript accepts completed Problem Locator MCP items but other modes remain fail-closed", () => {
  const messages = transcript();
  const clientProfile = codexLunaPermissionProfileId("client");
  messages.find((entry) => entry.id === CODEX_LUNA_APP_SERVER_REQUEST_IDS.permissionProfileList).result.data[0].id = clientProfile;
  const thread = messages.find((entry) => entry.id === CODEX_LUNA_APP_SERVER_REQUEST_IDS.threadStart).result;
  thread.activePermissionProfile.id = clientProfile;
  thread.sandbox.networkAccess = true;
  const finalIndex = messages.findIndex((entry) => entry.method === "item/started" && entry.params.item.type === "agentMessage");
  messages.splice(finalIndex, 0,
    {
      method: "item/started",
      params: { threadId: THREAD_ID, turnId: TURN_ID, startedAtMs: 25, item: { type: "mcpToolCall", id: "mcp-1", server: "problem-locator", tool: "problem_locator_get_case", status: "inProgress", arguments: { case_id: "case", wait_for_job_id: null, wait_seconds: 0 } } },
    },
    {
      method: "item/completed",
      params: { threadId: THREAD_ID, turnId: TURN_ID, completedAtMs: 26, item: { type: "mcpToolCall", id: "mcp-1", server: "problem-locator", tool: "problem_locator_get_case", status: "completed", arguments: { case_id: "case", wait_for_job_id: null, wait_seconds: 0 }, result: { content: [] }, error: null } },
    },
  );
  const parsed = parse(messages, { mode: "client" });
  assert.equal(parsed.mcp_tool_call_count, 1);
  assert.equal(parsed.mcp_tool_calls[0].tool, "problem_locator_get_case");
  assert.throws(() => parse(messages, { mode: "diagnosis" }), (error) => error.code === "CODEX_LUNA_APP_SERVER_TOOL_REJECTED");
});

test("request builders select external auth, an empty runtime-root list, and no legacy sandbox field", () => {
  const initialize = buildCodexLunaInitializeRequest();
  assert.equal(initialize.method, "initialize");
  assert.equal(initialize.params.capabilities.experimentalApi, true);
  assert.equal(initialize.params.capabilities.requestAttestation, false);
  assert.deepEqual(initialize.params.capabilities.optOutNotificationMethods, ["remoteControl/status/changed"]);
  assert.deepEqual(buildCodexLunaInitializedNotification(), { method: "initialized", params: {} });
  assert.deepEqual(buildCodexLunaAccountReadRequest(), { method: "account/read", id: CODEX_LUNA_APP_SERVER_REQUEST_IDS.accountRead, params: { refreshToken: false } });
  assert.deepEqual(buildCodexLunaPermissionProfileListRequest({ workspaceRoot: WORKSPACE }), { method: "permissionProfile/list", id: CODEX_LUNA_APP_SERVER_REQUEST_IDS.permissionProfileList, params: { cwd: WORKSPACE, limit: 100 } });
  assert.deepEqual(buildCodexLunaSkillsListRequest({ workspaceRoot: WORKSPACE }), { method: "skills/list", id: CODEX_LUNA_APP_SERVER_REQUEST_IDS.skillsList, params: { cwds: [WORKSPACE], forceReload: true } });

  const args = buildCodexLunaAppServerArguments();
  assert.deepEqual(CODEX_LUNA_RAW_SHELL_FUNCTION_NAMES, ["shell_command"]);
  assert.deepEqual(CODEX_LUNA_RAW_RESPONSE_ITEM_TYPES_ALLOWED, ["message", "reasoning", "local_shell_call", "function_call", "function_call_output"]);
  assert.deepEqual(CODEX_LUNA_RAW_RESPONSE_ITEM_SANITIZER_FIELDS, {
    message: ["type", "role"],
    reasoning: ["type"],
    local_shell_call: ["type", "call_id", "status", "action.type"],
    function_call: ["type", "name", "namespace", "call_id"],
    function_call_output: ["type", "call_id"],
  });
  assert.deepEqual(CODEX_LUNA_DISABLED_FEATURES, [
    "apps", "auth_elicitation", "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "code_mode_host", "computer_use", "current_time_reminder", "default_mode_request_user_input",
    "deferred_executor", "enable_mcp_apps", "executor_capability_discovery", "external_agent_memory_import",
    "goals", "guardian_approval", "hooks", "image_generation", "in_app_browser", "in_app_chat", "memories",
    "multi_agent", "multi_agent_v2", "network_proxy", "plugin_sharing", "plugins", "realtime_conversation",
    "recommended_plugins", "remote_compaction_v2", "remote_plugin", "request_permissions_tool", "shell_snapshot",
    "skill_mcp_dependency_install", "skill_search", "standalone_web_search", "tool_call_mcp_elicitation",
    "tool_suggest", "unbounded_connection_retries", "view_image", "workspace_dependencies",
  ]);
  assert.deepEqual(args.slice(0, 3), ["app-server", "--stdio", "--strict-config"]);
  assert.deepEqual(args.slice(3), CODEX_LUNA_DISABLED_FEATURES.flatMap((feature) => ["--disable", feature]));
  assert.equal(args.includes("shell_tool"), false);
  assert.equal(args.includes("unified_exec"), false);

  const thread = buildCodexLunaThreadStartRequest({ workspaceRoot: WORKSPACE, mode: "generation", developerInstructions: "Use the configured skill." });
  assert.equal(thread.params.model, CODEX_LUNA_MODEL);
  assert.equal(thread.params.allowProviderModelFallback, false);
  assert.deepEqual(thread.params.runtimeWorkspaceRoots, []);
  assert.equal(thread.params.permissions, codexLunaPermissionProfileId("generation"));
  assert.equal(thread.params.approvalPolicy, "never");
  assert.equal(thread.params.ephemeral, true);
  assert.deepEqual(thread.params.dynamicTools, []);
  assert.deepEqual(thread.params.selectedCapabilityRoots, []);
  assert.equal(thread.params.experimentalRawEvents, true);
  assert.equal(Object.hasOwn(thread.params, "sandbox"), false);

  const schema = { type: "object", properties: { answer: { type: "string" } }, required: ["answer"], additionalProperties: false };
  const turnRequest = buildCodexLunaTurnStartRequest({ threadId: THREAD_ID, prompt: "Diagnose.", workspaceRoot: WORKSPACE, skillPath: SKILL, mode: "diagnosis", outputSchema: schema });
  assert.deepEqual(turnRequest.params.input, [
    { type: "skill", name: "diagnose-rpc-timeout", path: SKILL },
    { type: "text", text: "Diagnose.", text_elements: [] },
  ]);
  assert.deepEqual(turnRequest.params.runtimeWorkspaceRoots, []);
  assert.equal(turnRequest.params.permissions, codexLunaPermissionProfileId("diagnosis"));
  assert.equal(turnRequest.params.model, CODEX_LUNA_MODEL);
  assert.equal(turnRequest.params.effort, CODEX_LUNA_REASONING_EFFORT);
  assert.equal(Object.hasOwn(turnRequest.params, "sandboxPolicy"), false);
  schema.properties.answer.type = "number";
  assert.equal(turnRequest.params.outputSchema.properties.answer.type, "string");
});

test("external ChatGPT token is written only to stdin and never returned or echoed by failures", () => {
  let line = null;
  const receipt = writeExternalChatgptAuthLoginRequest({ write(value) { line = value; return true; } }, {
    accessToken: SECRET,
    chatgptAccountId: "account-123",
    chatgptPlanType: "plus",
  });
  const envelope = JSON.parse(line);
  assert.equal(envelope.method, "account/login/start");
  assert.equal(envelope.params.type, "chatgptAuthTokens");
  assert.equal(envelope.params.accessToken, SECRET);
  assert.equal(envelope.params.chatgptAccountId, "account-123");
  assert.equal(receipt.credential_returned, false);
  assert.doesNotMatch(JSON.stringify(receipt), new RegExp(SECRET));
  assert.equal(Object.hasOwn(receipt, "accessToken"), false);

  assert.throws(
    () => writeExternalChatgptAuthLoginRequest({ write() { throw new Error(SECRET); } }, {
      accessToken: SECRET,
      chatgptAccountId: "account-123",
    }),
    (error) => error.code === "CODEX_LUNA_APP_SERVER_AUTH_WRITE_FAILED" && !String(error).includes(SECRET) && !JSON.stringify(error.details).includes(SECRET),
  );
});

test("one complete app-server turn binds thread, turn, model, profile, final message, commands, and ThreadTokenUsage.last", () => {
  const parsed = parse(transcript(), { secretValues: [SECRET] });
  assert.equal(parsed.status, "PASS");
  assert.equal(parsed.protocol_version, CODEX_LUNA_APP_SERVER_PROTOCOL_VERSION);
  assert.equal(parsed.thread_id, THREAD_ID);
  assert.equal(parsed.turn_id, TURN_ID);
  assert.equal(parsed.permission_profile_id, codexLunaPermissionProfileId("diagnosis"));
  assert.equal(parsed.model, CODEX_LUNA_MODEL);
  assert.equal(parsed.reasoning_effort, CODEX_LUNA_REASONING_EFFORT);
  assert.equal(parsed.final_agent_message, "Diagnosis complete.");
  assert.equal(parsed.command_count, 1);
  assert.deepEqual(parsed.commands[0], {
    item_id: "cmd-1",
    command: "sed -n '1,20p' input/request.json",
    cwd: WORKSPACE,
    status: "completed",
    exit_code: 0,
    duration_ms: 10,
  });
  assert.deepEqual(parsed.usage, {
    input_tokens: 100,
    cached_input_tokens: 40,
    cache_write_input_tokens: 0,
    output_tokens: 20,
    reasoning_output_tokens: 5,
    total_tokens: 120,
  });
  assert.equal(parsed.thread_token_usage.last.totalTokens, 120);
  assert.equal(parsed.raw_response_count, 2);
  assert.deepEqual(parsed.raw_response_usage, usageBreakdown());
  assert.equal(parsed.raw_response_item_count, 6);
  assert.deepEqual(parsed.raw_response_item_type_counts, {
    message: 1,
    reasoning: 1,
    local_shell_call: 1,
    function_call: 1,
    function_call_output: 2,
  });
  assert.deepEqual(parsed.raw_shell_function_names, ["shell_command"]);
  assert.deepEqual(parsed.raw_shell_call_ids, ["raw-shell-function-1", "raw-local-shell-1"]);
  assert.deepEqual(parsed.raw_shell_output_call_ids, ["raw-shell-function-1", "raw-local-shell-1"]);
  assert.doesNotMatch(JSON.stringify(parsed), /ignored unified diff/);
  assert.doesNotMatch(JSON.stringify(parsed), new RegExp(SECRET));
});

test("server approval, permission, input, refresh, and dynamic tool requests fail closed", async (t) => {
  for (const method of [
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/tool/requestUserInput",
    "item/permissions/requestApproval",
    "mcpServer/elicitation/request",
    "item/tool/call",
    "account/chatgptAuthTokens/refresh",
  ]) {
    await t.test(method, () => {
      const mutated = transcript();
      mutated.splice(-1, 0, { method, id: 99, params: { reason: "test" } });
      assert.throws(
        () => parse(mutated),
        (error) => error.code === "CODEX_LUNA_APP_SERVER_SERVER_REQUEST_REJECTED" && error.details.method === method,
      );
    });
  }
});

test("MCP, web, collaboration, image, file-change, and other non-command tools fail closed", async (t) => {
  for (const itemType of [
    "mcpToolCall",
    "dynamicToolCall",
    "collabAgentToolCall",
    "subAgentActivity",
    "webSearch",
    "imageView",
    "imageGeneration",
    "fileChange",
    "plan",
    "sleep",
  ]) {
    await t.test(itemType, () => {
      const mutated = transcript();
      mutated.splice(-2, 0, {
        method: "item/completed",
        params: { threadId: THREAD_ID, turnId: TURN_ID, completedAtMs: 41, item: { type: itemType, id: `forbidden-${itemType}` } },
      });
      assert.throws(
        () => parse(mutated),
        (error) => error.code === "CODEX_LUNA_APP_SERVER_TOOL_REJECTED" && error.details.item_type === itemType,
      );
    });
  }
});

test("raw Responses items allow only assistant messages, reasoning, and the pinned Luna shell protocol", async (t) => {
  await t.test("sanitizer minimum fields remain sufficient", () => {
    const messages = transcript();
    for (const message of messages.filter((entry) => entry.method === "rawResponseItem/completed")) {
      const item = message.params.item;
      if (item.type === "message") message.params.item = { type: item.type, role: item.role };
      else if (item.type === "reasoning") message.params.item = { type: item.type };
      else if (item.type === "function_call") message.params.item = { type: item.type, name: item.name, namespace: null, call_id: item.call_id };
      else if (item.type === "local_shell_call") message.params.item = { type: item.type, call_id: item.call_id, status: item.status, action: { type: item.action.type } };
      else message.params.item = { type: item.type, call_id: item.call_id };
    }
    assert.equal(parse(messages).raw_response_item_count, 6);
  });

  for (const itemType of [
    "agent_message",
    "tool_search_call",
    "tool_search_output",
    "custom_tool_call",
    "custom_tool_call_output",
    "web_search_call",
    "image_generation_call",
    "compaction",
    "compaction_trigger",
    "context_compaction",
    "other",
    "future_capability",
  ]) {
    await t.test(`reject ${itemType}`, () => {
      const messages = transcript();
      messages.find((entry) => entry.method === "rawResponseItem/completed").params.item = { type: itemType };
      assert.throws(
        () => parse(messages),
        (error) => error.code === "CODEX_LUNA_APP_SERVER_RAW_RESPONSE_ITEM_TYPE_REJECTED" && error.details.item_type === itemType,
      );
    });
  }

  for (const functionName of ["exec_command", "write_stdin", "apply_patch", "shell", "mcp", "web_search"]) {
    await t.test(`reject function ${functionName}`, () => {
      const messages = transcript();
      messages.find((entry) => entry.method === "rawResponseItem/completed" && entry.params.item.type === "function_call").params.item.name = functionName;
      assert.throws(
        () => parse(messages),
        (error) => error.code === "CODEX_LUNA_APP_SERVER_RAW_SHELL_FUNCTION_REJECTED" && error.details.function_name === functionName,
      );
    });
  }

  await t.test("reject namespaced function", () => {
    const messages = transcript();
    messages.find((entry) => entry.method === "rawResponseItem/completed" && entry.params.item.type === "function_call").params.item.namespace = "plugin";
    assert.throws(() => parse(messages), (error) => error.code === "CODEX_LUNA_APP_SERVER_RAW_SHELL_FUNCTION_REJECTED");
  });
  await t.test("reject non-assistant message", () => {
    const messages = transcript();
    messages.find((entry) => entry.method === "rawResponseItem/completed" && entry.params.item.type === "message").params.item.role = "tool";
    assert.throws(() => parse(messages), (error) => error.code === "CODEX_LUNA_APP_SERVER_RAW_MESSAGE_INVALID");
  });
  await t.test("reject duplicate shell call id", () => {
    const messages = transcript();
    messages.find((entry) => entry.method === "rawResponseItem/completed" && entry.params.item.type === "local_shell_call").params.item.call_id = "raw-shell-function-1";
    assert.throws(() => parse(messages), (error) => error.code === "CODEX_LUNA_APP_SERVER_RAW_SHELL_CALL_INVALID");
  });
  await t.test("reject invalid local shell action", () => {
    const messages = transcript();
    messages.find((entry) => entry.method === "rawResponseItem/completed" && entry.params.item.type === "local_shell_call").params.item.action.type = "spawn";
    assert.throws(() => parse(messages), (error) => error.code === "CODEX_LUNA_APP_SERVER_RAW_SHELL_CALL_INVALID");
  });
  await t.test("reject orphan shell output", () => {
    const messages = transcript();
    messages.find((entry) => entry.method === "rawResponseItem/completed" && entry.params.item.type === "function_call_output").params.item.call_id = "orphan";
    assert.throws(() => parse(messages), (error) => error.code === "CODEX_LUNA_APP_SERVER_RAW_SHELL_OUTPUT_INVALID");
  });
  await t.test("reject duplicate shell output", () => {
    const messages = transcript();
    const outputIndex = messages.findIndex((entry) => entry.method === "rawResponseItem/completed" && entry.params.item.type === "function_call_output");
    messages.splice(outputIndex + 1, 0, rawResponseItem({ type: "function_call_output", call_id: "raw-shell-function-1" }));
    assert.throws(() => parse(messages), (error) => error.code === "CODEX_LUNA_APP_SERVER_RAW_SHELL_OUTPUT_INVALID");
  });
});

test("the transcript parser rejects incomplete usage, multiple scopes, failed turns, non-final ordering, and credential output", async (t) => {
  await t.test("incomplete usage", () => {
    const mutated = transcript();
    const usageEvent = mutated.find((message) => message.method === "thread/tokenUsage/updated");
    delete usageEvent.params.tokenUsage.last.reasoningOutputTokens;
    assert.throws(
      () => parse(mutated),
      (error) => error.code === "CODEX_LUNA_APP_SERVER_USAGE_INVALID",
    );
  });
  await t.test("multiple turns", () => {
    const mutated = transcript();
    mutated.find((message) => message.method === "thread/tokenUsage/updated").params.turnId = "second-turn";
    assert.throws(
      () => parse(mutated),
      (error) => error.code === "CODEX_LUNA_APP_SERVER_TURN_CARDINALITY_INVALID",
    );
  });
  await t.test("delta from another turn", () => {
    const mutated = transcript();
    mutated.splice(-1, 0, { method: "item/agentMessage/delta", params: { threadId: THREAD_ID, turnId: "second-turn", itemId: "msg-1", delta: "x" } });
    assert.throws(
      () => parse(mutated),
      (error) => error.code === "CODEX_LUNA_APP_SERVER_TURN_CARDINALITY_INVALID",
    );
  });
  await t.test("turn diff from another turn", () => {
    const mutated = transcript();
    mutated.find((message) => message.method === "turn/diff/updated").params.turnId = "second-turn";
    assert.throws(
      () => parse(mutated),
      (error) => error.code === "CODEX_LUNA_APP_SERVER_TURN_CARDINALITY_INVALID",
    );
  });
  await t.test("rate-limit state before login", () => {
    const mutated = transcript();
    const rateIndex = mutated.findIndex((message) => message.method === "account/rateLimits/updated");
    const [rateLimit] = mutated.splice(rateIndex, 1);
    mutated.splice(2, 0, rateLimit);
    assert.throws(
      () => parse(mutated),
      (error) => error.code === "CODEX_LUNA_APP_SERVER_RATE_LIMIT_ORDER_INVALID",
    );
  });
  await t.test("plan delta notification", () => {
    const mutated = transcript();
    mutated.splice(-1, 0, { method: "item/plan/delta", params: { threadId: THREAD_ID, turnId: TURN_ID, itemId: "plan-1", delta: "x" } });
    assert.throws(
      () => parse(mutated),
      (error) => error.code === "CODEX_LUNA_APP_SERVER_NOTIFICATION_REJECTED",
    );
  });
  await t.test("failed completion", () => {
    const mutated = transcript();
    const completed = mutated.find((message) => message.method === "turn/completed");
    completed.params.turn.status = "failed";
    completed.params.turn.error = { message: "failed" };
    assert.throws(
      () => parse(mutated),
      (error) => error.code === "CODEX_LUNA_APP_SERVER_TURN_COMPLETION_INVALID",
    );
  });
  await t.test("agent message is not last", () => {
    const mutated = transcript();
    mutated.splice(-2, 0, {
      method: "item/completed",
      params: {
        threadId: THREAD_ID,
        turnId: TURN_ID,
        completedAtMs: 41,
        item: { type: "reasoning", id: "reason-after-final", summary: [], content: [] },
      },
    });
    assert.throws(
      () => parse(mutated),
      (error) => error.code === "CODEX_LUNA_APP_SERVER_FINAL_MESSAGE_NOT_LAST",
    );
  });
  await t.test("credential in inbound output", () => {
    const mutated = transcript();
    const final = mutated.find((message) => message.method === "item/completed" && message.params.item.type === "agentMessage");
    final.params.item.text = `unexpected ${SECRET}`;
    assert.throws(
      () => parse(mutated, { secretValues: [SECRET] }),
      (error) => error.code === "CODEX_LUNA_APP_SERVER_SECRET_LEAK" && !String(error).includes(SECRET),
    );
  });
  await t.test("raw response usage is missing", () => {
    const mutated = transcript();
    mutated.find((message) => message.method === "rawResponse/completed").params.usage = null;
    assert.throws(
      () => parse(mutated),
      (error) => error.code === "CODEX_LUNA_APP_SERVER_RAW_USAGE_MISSING",
    );
  });
  await t.test("raw response aggregate differs from terminal last", () => {
    const mutated = transcript();
    const raw = mutated.find((message) => message.method === "rawResponse/completed").params.usage;
    raw.inputTokens += 1;
    raw.totalTokens += 1;
    assert.throws(
      () => parse(mutated),
      (error) => error.code === "CODEX_LUNA_APP_SERVER_USAGE_RECONCILIATION_FAILED",
    );
  });
  await t.test("terminal total differs from raw aggregate and last", () => {
    const mutated = transcript();
    const total = mutated.find((message) => message.method === "thread/tokenUsage/updated").params.tokenUsage.total;
    total.inputTokens += 1;
    total.totalTokens += 1;
    assert.throws(
      () => parse(mutated),
      (error) => error.code === "CODEX_LUNA_APP_SERVER_USAGE_RECONCILIATION_FAILED",
    );
  });
  await t.test("terminal message phase must be exact final_answer", () => {
    const mutated = transcript();
    const final = mutated.find((message) => message.method === "item/completed" && message.params.item.type === "agentMessage");
    final.params.item.phase = null;
    assert.throws(
      () => parse(mutated),
      (error) => error.code === "CODEX_LUNA_APP_SERVER_FINAL_MESSAGE_INVALID",
    );
  });
});

test("thread response persistence, provider, sandbox, reviewer, and instruction-source boundaries fail closed", async (t) => {
  const cases = [
    ["outer provider", (response) => { response.modelProvider = "other"; }, "CODEX_LUNA_APP_SERVER_MODEL_IDENTITY_INVALID"],
    ["inner provider", (response) => { response.thread.modelProvider = "other"; }, "CODEX_LUNA_APP_SERVER_THREAD_BOUNDARY_INVALID"],
    ["non-ephemeral", (response) => { response.thread.ephemeral = false; }, "CODEX_LUNA_APP_SERVER_THREAD_BOUNDARY_INVALID"],
    ["persisted path", (response) => { response.thread.path = `${WORKSPACE}/thread.jsonl`; }, "CODEX_LUNA_APP_SERVER_THREAD_BOUNDARY_INVALID"],
    ["parent lineage", (response) => { response.thread.parentThreadId = "parent"; }, "CODEX_LUNA_APP_SERVER_THREAD_BOUNDARY_INVALID"],
    ["session mismatch", (response) => { response.thread.sessionId = "other-session"; }, "CODEX_LUNA_APP_SERVER_THREAD_BOUNDARY_INVALID"],
    ["source mismatch", (response) => { response.thread.source = "cli"; }, "CODEX_LUNA_APP_SERVER_THREAD_BOUNDARY_INVALID"],
    ["preexisting turns", (response) => { response.thread.turns = [turn("completed")]; }, "CODEX_LUNA_APP_SERVER_THREAD_BOUNDARY_INVALID"],
    ["network projection", (response) => { response.sandbox.networkAccess = true; }, "CODEX_LUNA_APP_SERVER_SANDBOX_PROJECTION_INVALID"],
    ["reviewer", (response) => { response.approvalsReviewer = "guardian"; }, "CODEX_LUNA_APP_SERVER_APPROVAL_REVIEWER_INVALID"],
    ["multi-agent", (response) => { response.multiAgentMode = "auto"; }, "CODEX_LUNA_APP_SERVER_MULTI_AGENT_BOUNDARY_INVALID"],
    ["runtime roots", (response) => { response.runtimeWorkspaceRoots = [WORKSPACE]; }, "CODEX_LUNA_APP_SERVER_WORKSPACE_BINDING_INVALID"],
    ["outside instruction source", (response) => { response.instructionSources = ["/private/tmp/outside/AGENTS.md"]; }, "CODEX_LUNA_APP_SERVER_INSTRUCTION_SOURCES_INVALID"],
  ];
  for (const [name, mutate, code] of cases) {
    await t.test(name, () => {
      const mutated = transcript();
      const response = mutated.find((message) => message.id === CODEX_LUNA_APP_SERVER_REQUEST_IDS.threadStart).result;
      mutate(response);
      assert.throws(() => parse(mutated), (error) => error.code === code);
    });
  }
});

test("completed command execution must remain in the invocation workspace", () => {
  const mutated = transcript();
  mutated.find((message) => message.method === "item/completed" && message.params.item.type === "commandExecution").params.item.cwd = "/private/tmp/outside";
  assert.throws(
    () => parse(mutated),
    (error) => error.code === "CODEX_LUNA_APP_SERVER_COMMAND_WORKSPACE_INVALID",
  );
});

test("account, custom-profile, skill, and isolated-home proof mutations fail closed", async (t) => {
  const cases = [
    ["account", (messages) => { messages.find((message) => message.id === CODEX_LUNA_APP_SERVER_REQUEST_IDS.accountRead).result.account.type = "apiKey"; }, "CODEX_LUNA_APP_SERVER_ACCOUNT_PROOF_INVALID"],
    ["profile", (messages) => { messages.find((message) => message.id === CODEX_LUNA_APP_SERVER_REQUEST_IDS.permissionProfileList).result.data[0].allowed = false; }, "CODEX_LUNA_APP_SERVER_PERMISSION_PROOF_INVALID"],
    ["extra enabled skill", (messages) => { messages.find((message) => message.id === CODEX_LUNA_APP_SERVER_REQUEST_IDS.skillsList).result.data[0].skills[0].enabled = true; }, "CODEX_LUNA_APP_SERVER_SKILLS_PROOF_INVALID"],
    ["missing disabled system skill", (messages) => { messages.find((message) => message.id === CODEX_LUNA_APP_SERVER_REQUEST_IDS.skillsList).result.data[0].skills.shift(); }, "CODEX_LUNA_APP_SERVER_SYSTEM_SKILL_PROOF_INVALID"],
    ["Codex home", (messages) => { messages.find((message) => message.id === CODEX_LUNA_APP_SERVER_REQUEST_IDS.initialize).result.codexHome = "/private/tmp/not-the-isolated-home"; }, "CODEX_LUNA_APP_SERVER_CODEX_HOME_BINDING_INVALID"],
  ];
  for (const [name, mutate, code] of cases) {
    await t.test(name, () => {
      const messages = transcript();
      mutate(messages);
      assert.throws(() => parse(messages), (error) => error.code === code);
    });
  }
});

test("evidence includes exact profile bytes and protocol identity without credentials", () => {
  const profile = buildProfile("diagnosis");
  const parsed = parse(transcript(), { secretValues: [SECRET] });
  const evidence = buildCodexLunaAppServerEvidenceSummary({ profile, transcript: parsed, secretValues: [SECRET] });
  assert.equal(evidence.status, "PASS");
  assert.equal(evidence.protocol.version, CODEX_LUNA_APP_SERVER_PROTOCOL_VERSION);
  assert.equal(evidence.protocol.authentication, "external-chatgpt-tokens-in-memory");
  assert.equal(evidence.protocol.experimental_raw_events, true);
  assert.deepEqual(evidence.protocol.raw_response_item_types_allowed, CODEX_LUNA_RAW_RESPONSE_ITEM_TYPES_ALLOWED);
  assert.deepEqual(evidence.protocol.raw_shell_function_names_allowed, CODEX_LUNA_RAW_SHELL_FUNCTION_NAMES);
  assert.deepEqual(evidence.protocol.disabled_features, CODEX_LUNA_DISABLED_FEATURES);
  assert.match(evidence.protocol.launch_arguments_sha256, /^[a-f0-9]{64}$/);
  assert.equal(evidence.permission_profile.bytes_utf8, profile.config_toml);
  assert.equal(evidence.permission_profile.byte_count, Buffer.byteLength(profile.config_toml));
  assert.equal(evidence.permission_profile.sha256, profile.config_sha256);
  assert.equal(evidence.permission_profile.shell_environment.codex_home_forwarded, false);
  assert.doesNotMatch(JSON.stringify(evidence), new RegExp(SECRET));

  assert.throws(
    () => buildCodexLunaAppServerEvidenceSummary({ profile: { ...profile, config_toml: `${profile.config_toml}# drift\n` }, transcript: parsed }),
    (error) => error.code === "CODEX_LUNA_APP_SERVER_PROFILE_BYTES_INVALID",
  );
});

test("pinned app-server accepts and reports the strict profile and skill boundary without auth or a model turn", {
  skip: process.platform !== "darwin" || !fs.existsSync(PINNED_CODEX),
}, async () => {
  const root = fs.mkdtempSync(path.join(fs.existsSync("/private/tmp") ? "/private/tmp" : os.tmpdir(), "codex-app-server-profile-"));
  let child = null;
  try {
    const codexHome = path.join(root, "codex-home");
    const home = path.join(root, "home");
    const workspace = path.join(root, "workspace");
    const shellHome = path.join(workspace, ".shell-home");
    const skill = path.join(workspace, ".agents", "skills", "test-skill", "SKILL.md");
    fs.mkdirSync(codexHome, { recursive: true, mode: 0o700 });
    fs.mkdirSync(home, { recursive: true, mode: 0o700 });
    fs.mkdirSync(shellHome, { recursive: true, mode: 0o700 });
    fs.mkdirSync(path.dirname(skill), { recursive: true, mode: 0o700 });
    fs.writeFileSync(skill, "---\nname: test-skill\ndescription: protocol-only test\n---\n", { mode: 0o600 });
    for (const name of CODEX_LUNA_SYSTEM_SKILL_NAMES) {
      const systemSkill = systemSkillPath(name, codexHome);
      fs.mkdirSync(path.dirname(systemSkill), { recursive: true, mode: 0o700 });
      fs.writeFileSync(systemSkill, `---\nname: ${name}\ndescription: disabled system skill\n---\n`, { mode: 0o600 });
    }
    const profile = buildCodexLunaIsolatedConfig({
      workspaceRoot: workspace,
      skillPath: skill,
      codexHome,
      shellHome,
      mode: "diagnosis",
    });
    fs.writeFileSync(path.join(codexHome, "config.toml"), profile.config_toml, { mode: 0o600 });
    child = spawn(PINNED_CODEX, buildCodexLunaAppServerArguments(), {
      cwd: workspace,
      env: {
        HOME: home,
        CODEX_HOME: codexHome,
        PATH: "/usr/bin:/bin:/usr/sbin:/sbin",
        TMPDIR: root,
        NO_COLOR: "1",
      },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let pending = "";
    let stderr = "";
    const responses = new Map();
    const waiters = new Map();
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.stdout.on("data", (chunk) => {
      pending += chunk;
      for (;;) {
        const newline = pending.indexOf("\n");
        if (newline < 0) break;
        const line = pending.slice(0, newline).trim();
        pending = pending.slice(newline + 1);
        if (!line) continue;
        const message = JSON.parse(line);
        if (!Object.hasOwn(message, "id") || typeof message.method === "string") continue;
        const waiter = waiters.get(message.id);
        if (waiter) {
          waiters.delete(message.id);
          if (message.error) waiter.reject(new Error(JSON.stringify(message.error)));
          else waiter.resolve(message.result);
        } else {
          responses.set(message.id, message);
        }
      }
    });
    const closed = new Promise((resolve, reject) => {
      child.once("error", reject);
      child.once("close", (code, signal) => {
        for (const waiter of waiters.values()) waiter.reject(new Error(`app-server closed before response: ${code ?? signal}`));
        waiters.clear();
        resolve({ code, signal });
      });
    });
    const request = (message) => new Promise((resolve, reject) => {
      const existing = responses.get(message.id);
      if (existing) {
        responses.delete(message.id);
        if (existing.error) reject(new Error(JSON.stringify(existing.error)));
        else resolve(existing.result);
        return;
      }
      waiters.set(message.id, { resolve, reject });
      child.stdin.write(`${JSON.stringify(message)}\n`);
    });
    const timeout = setTimeout(() => child.kill("SIGKILL"), 30_000);
    const initialized = await request(buildCodexLunaInitializeRequest());
    child.stdin.write(`${JSON.stringify(buildCodexLunaInitializedNotification())}\n`);
    const permissionProfiles = await request(buildCodexLunaPermissionProfileListRequest({ workspaceRoot: workspace }));
    const listedSkills = await request(buildCodexLunaSkillsListRequest({ workspaceRoot: workspace }));
    child.stdin.end();
    const exit = await closed;
    clearTimeout(timeout);
    assert.deepEqual(exit, { code: 0, signal: null }, stderr);
    assert.equal(initialized.codexHome, codexHome);
    assert.ok(permissionProfiles.data.some((entry) => entry.id === profile.profile_id && entry.allowed === true));
    const cwdSkills = listedSkills.data.find((entry) => entry.cwd === workspace);
    assert.deepEqual(cwdSkills?.errors, []);
    assert.ok(cwdSkills?.skills.some((entry) => entry.name === "test-skill" && entry.path === skill && entry.enabled === true));
    for (const name of CODEX_LUNA_SYSTEM_SKILL_NAMES) {
      assert.ok(cwdSkills?.skills.some((entry) => entry.name === name && entry.path === systemSkillPath(name, codexHome) && entry.enabled === false));
    }
    assert.equal(fs.existsSync(path.join(codexHome, "auth.json")), false);
  } finally {
    if (child?.exitCode === null && child?.signalCode === null) child.kill("SIGKILL");
    fs.rmSync(root, { recursive: true, force: true });
  }
});
