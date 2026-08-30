import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  auditCodexLunaRuntimeSecrets,
  installServiceSkillInCodexHome,
  readCodexLunaExternalAuth,
  runCodexLunaAppServerCall,
} from "../runtime-support/codex-luna-app-server-runtime.mjs";
import { codexLunaAppServerCliVersion } from "../runtime-support/codex-luna-contract.mjs";

const RUNTIME_SOURCE = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../runtime-support/codex-luna-app-server-runtime.mjs",
);
const posixRuntimeTest = process.platform === "win32" ? test.skip : test;
const SYSTEM_SKILLS = [
  "imagegen",
  "openai-docs",
  "plugin-creator",
  "review-agent",
  "skill-creator",
  "skill-installer",
];
const ACCESS_TOKEN = "access-token-canary-0123456789";
const REFRESH_TOKEN = "refresh-token-canary-0123456789";
const ID_TOKEN = "id-token-canary-0123456789";
const ACCOUNT_ID = "account-canary-0123456789";
const WORK_CONTENT = "work-content-that-must-be-replaced-by-a-receipt";
const WARNING_CONTENT = "warning-content-that-must-be-replaced-by-a-receipt";
const PATCH_CONTENT = "patch-content-that-must-be-replaced-by-a-receipt";
const FAKE_CLI_VERSION = codexLunaAppServerCliVersion();
const DEVELOPER_INSTRUCTIONS = "Keep the invocation cwd fixed.";

test("service Skill installation binds the declared Skill name instead of its staging directory", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "codex-luna-service-skill-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const source = path.join(root, "staging-name", "SKILL.md");
  const codexHome = path.join(root, "codex-home");
  fs.mkdirSync(path.dirname(source), { recursive: true });
  fs.mkdirSync(codexHome, { recursive: true });
  fs.writeFileSync(source, "---\nname: declared-service-skill\ndescription: fixture\n---\n");
  const installed = installServiceSkillInCodexHome(codexHome, source);
  assert.equal(installed, path.join(codexHome, "skills", "declared-service-skill", "SKILL.md"));
  assert.equal(fs.readFileSync(installed, "utf8"), fs.readFileSync(source, "utf8"));
  assert.equal(fs.lstatSync(installed).mode & 0o777, process.platform === "win32" ? 0o444 : 0o400);
  assert.throws(
    () => installServiceSkillInCodexHome(codexHome, source),
    (error) => error.code === "EEXIST",
  );
});

const FAKE_CODEX = String.raw`#!/usr/bin/env node
const fs = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");

let args = process.argv.slice(2);
const fakeMode = process.env.FAKE_CODEX_MODE || "success";
const profileMode = fakeMode === "generation-apply-patch" ? "generation" : ["late-mcp-notification", "mcp-call-limit", "coalesced-mcp-call-limit"].includes(fakeMode) ? "client" : "diagnosis";
const skillPath = process.env.FAKE_CODEX_SKILL;

if (args[0] === "-C") {
  if (path.resolve(args[1] || "") !== process.cwd()) process.exit(63);
  args = args.slice(2);
} else if (args[0] === "app-server") {
  process.exit(63);
}

if (args[0] === "sandbox") {
  const separator = args.indexOf("--");
  const command = args.slice(separator + 1);
  if (command[0] === "/bin/cat" && command[1] === skillPath) {
    process.stdout.write(fs.readFileSync(skillPath, "utf8"));
    process.exit(0);
  }
  if (profileMode === "generation" && command[0] === "/usr/bin/touch" && command[1].startsWith(path.dirname(path.dirname(path.dirname(path.dirname(skillPath)))))) {
    fs.writeFileSync(command[1], "");
    process.exit(0);
  }
  if (profileMode === "client" && command[0] === "/usr/bin/nc") process.exit(0);
  process.exit(1);
}

if (args[0] !== "app-server" || args[1] !== "--stdio") process.exit(64);

const send = (value) => process.stdout.write(JSON.stringify(value) + "\n");
const sendBatch = (values) => process.stdout.write(values.map((value) => JSON.stringify(value)).join("\n") + "\n");
const threadId = "fake-thread-0001";
const turnId = "fake-turn-0001";
const usage = {
  totalTokens: 17,
  inputTokens: 11,
  cachedInputTokens: 3,
  cacheWriteInputTokens: 0,
  outputTokens: 6,
  reasoningOutputTokens: 2,
};
const turn = (status) => ({
  id: turnId,
  status,
  items: [],
  itemsView: "full",
  error: null,
  startedAt: 1,
  completedAt: status === "completed" ? 2 : null,
  durationMs: status === "completed" ? 1 : null,
});

const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
lines.on("line", (line) => {
  const message = JSON.parse(line);
  if (message.method === "initialize") {
    send({
      id: message.id,
      result: {
        userAgent: "codex_app_server_rs/0.149.0-alpha.4.1",
        codexHome: process.env.CODEX_HOME,
        platformFamily: "unix",
        platformOs: "macos",
      },
    });
    if (fakeMode === "server-request") {
      send({ id: 99, method: "item/permissions/requestApproval", params: { reason: "must fail closed" } });
    }
    return;
  }
  if (message.method === "initialized") return;
  if (message.method === "account/login/start") {
    if (fakeMode === "credential-echo") {
      send({ method: "fake/credentialEcho", params: { value: message.params.accessToken } });
      return;
    }
    send({ id: message.id, result: { type: "chatgptAuthTokens" } });
    send({ method: "account/login/completed", params: { loginId: null, success: true, error: null, onboardingEntrypoint: null } });
    send({ method: "account/updated", params: { authMode: "chatgptAuthTokens", planType: "plus" } });
    send({ method: "account/rateLimits/updated", params: { primary: { usedPercent: 1 } } });
    send({ method: "warning", params: { message: ${JSON.stringify(WARNING_CONTENT)}, threadId: null } });
    return;
  }
  if (message.method === "account/read") {
    send({ id: message.id, result: { account: { type: "chatgpt", email: "not-persisted@example.test", planType: "plus" }, requiresOpenaiAuth: true } });
    return;
  }
  if (message.method === "permissionProfile/list") {
    send({ id: message.id, result: { data: [{ id: "test-flow-codex-luna-" + profileMode, description: "fake least privilege", allowed: true }], nextCursor: null } });
    return;
  }
  if (message.method === "skills/list") {
    const systemSkills = ${JSON.stringify(SYSTEM_SKILLS)}.map((name) => ({
      name,
      description: "disabled fake system skill",
      path: path.join(process.env.CODEX_HOME, "skills", ".system", name, "SKILL.md"),
      scope: "system",
      enabled: false,
    }));
    send({
      id: message.id,
      result: {
        data: [{
          cwd: message.params.cwds[0],
          skills: [
            ...systemSkills,
            {
              name: path.basename(path.dirname(skillPath)),
              description: "intended fake repo skill",
              path: skillPath,
              scope: "repo",
              enabled: true,
            },
          ],
          errors: [],
        }],
      },
    });
    return;
  }
  if (message.method === "thread/start") {
    if (message.params.developerInstructions !== process.env.FAKE_CODEX_DEVELOPER_INSTRUCTIONS) process.exit(65);
    if (fakeMode === "response-error") {
      send({ id: message.id, error: { code: -32001, message: "client profile rejected" } });
      return;
    }
    const cwd = message.params.cwd;
    send({
      id: message.id,
      result: {
        thread: {
          id: threadId,
          sessionId: threadId,
          forkedFromId: null,
          parentThreadId: null,
          ephemeral: true,
          path: null,
          source: "vscode",
          modelProvider: "openai",
          cwd,
          cliVersion: ${JSON.stringify(FAKE_CLI_VERSION)},
          turns: [],
        },
        model: "gpt-5.6-luna",
        modelProvider: "openai",
        serviceTier: null,
        cwd,
        runtimeWorkspaceRoots: [cwd],
        instructionSources: [],
        approvalPolicy: "never",
        approvalsReviewer: "user",
        sandbox: { type: profileMode === "generation" ? "workspaceWrite" : "readOnly", networkAccess: profileMode === "client" },
        activePermissionProfile: { id: "test-flow-codex-luna-" + profileMode, extends: null },
        reasoningEffort: "medium",
        multiAgentMode: "explicitRequestOnly",
      },
    });
    send({ method: "thread/started", params: { thread: { id: threadId } } });
    return;
  }
  if (message.method === "turn/start") {
    const turnStartResponse = { id: message.id, result: { turn: turn("inProgress") } };
    const turnStartedNotification = { method: "turn/started", params: { threadId, turn: turn("inProgress") } };
    if (fakeMode === "coalesced-mcp-call-limit") {
      sendBatch([
        turnStartResponse,
        turnStartedNotification,
        ...Array.from({ length: 3 }, (_, offset) => ({
          method: "item/started",
          params: {
            threadId,
            turnId,
            item: {
              type: "mcpToolCall",
              id: "mcp-" + (offset + 1),
              server: "problem-locator",
              tool: "problem_locator_get_case",
              status: "inProgress",
              arguments: { case_id: "case-one", wait_seconds: 30 },
            },
          },
        })),
      ]);
      return;
    }
    send(turnStartResponse);
    send(turnStartedNotification);
    if (fakeMode === "wall-timeout") {
      send({ method: "item/started", params: { threadId, turnId, item: { type: "agentMessage", id: "partial-message", text: "", phase: "commentary" } } });
      return;
    }
    if (fakeMode === "error-notification") {
      send({ method: "error", params: { threadId, turnId, error: { message: "private provider detail", additionalDetails: "private additional detail", codexErrorInfo: "serverOverloaded" }, willRetry: false } });
      return;
    }
    if (fakeMode === "error-after-usage") {
      send({ method: "thread/tokenUsage/updated", params: { threadId, turnId, tokenUsage: { total: usage, last: usage, modelContextWindow: 400000 } } });
      send({ method: "error", params: { threadId, turnId, error: { message: "provider failed after usage", codexErrorInfo: "internalServerError" }, willRetry: false } });
      return;
    }
    if (fakeMode === "retrying-error-notification") {
      send({ method: "error", params: { threadId, turnId, error: { message: "private transient detail", additionalDetails: null, codexErrorInfo: { responseStreamDisconnected: { httpStatusCode: 503 } } }, willRetry: true } });
    }
    if (fakeMode === "wrong-raw-function") {
      send({ method: "rawResponseItem/completed", params: { threadId, turnId, item: { type: "function_call", name: "unexpected_shell", call_id: "bad-call-1", arguments: "{}" } } });
    }
    if (fakeMode === "mcp-call-limit") {
      for (let index = 1; index <= 3; index += 1) {
        send({ method: "item/started", params: { threadId, turnId, item: { type: "mcpToolCall", id: "mcp-" + index, server: "problem-locator", tool: "problem_locator_get_case", status: "inProgress", arguments: { case_id: "case-one", wait_seconds: 30 } } } });
      }
      return;
    }
    if (profileMode === "generation") {
      send({ method: "rawResponseItem/completed", params: { threadId, turnId, item: { type: "custom_tool_call", name: "exec", call_id: "code-call-1", input: ${JSON.stringify(PATCH_CONTENT)}, status: "completed" } } });
      send({ method: "item/started", params: { threadId, turnId, item: { type: "fileChange", id: "patch-item-1", status: "inProgress", changes: [] } } });
      send({ method: "item/completed", params: { threadId, turnId, item: { type: "fileChange", id: "patch-item-1", status: "completed", changes: [{ path: "generated/fake-locator/SKILL.md", kind: { type: "add" }, diff: ${JSON.stringify(PATCH_CONTENT)} }] } } });
      send({ method: "rawResponseItem/completed", params: { threadId, turnId, item: { type: "custom_tool_call_output", name: "exec", call_id: "code-call-1", output: ${JSON.stringify(PATCH_CONTENT)} } } });
    }
    send({
      method: "rawResponseItem/completed",
      params: {
        threadId,
        turnId,
        item: { type: "message", id: "raw-item-1", role: "assistant", content: [{ type: "output_text", text: ${JSON.stringify(WORK_CONTENT)} }] },
      },
    });
    send({ method: "item/started", params: { threadId, turnId, item: { type: "agentMessage", id: "final-message", text: "", phase: "final_answer" } } });
    send({ method: "item/completed", params: { threadId, turnId, item: { type: "agentMessage", id: "final-message", text: "fake diagnosis complete", phase: "final_answer" } } });
    send({ method: "rawResponse/completed", params: { threadId, turnId, responseId: "response-1", usage } });
    send({ method: "thread/tokenUsage/updated", params: { threadId, turnId, tokenUsage: { total: usage, last: usage, modelContextWindow: 400000 } } });
    send({ method: "turn/completed", params: { threadId, turn: turn("completed") } });
    return;
  }
  if (message.method === "account/logout") {
    if (fakeMode === "late-mcp-notification") send({ method: "mcpServer/startupStatus/updated", params: { server: "problem-locator", status: "ready" } });
    send({ id: message.id, result: {} });
    lines.close();
    setImmediate(() => process.exit(0));
  }
});
`;

function makeFixture(t, fakeMode = "success") {
  const invocationMode = fakeMode === "generation-apply-patch" ? "generation" : ["late-mcp-notification", "mcp-call-limit", "coalesced-mcp-call-limit"].includes(fakeMode) ? "client" : "diagnosis";
  const root = fs.mkdtempSync(path.join(fs.existsSync("/private/tmp") ? "/private/tmp" : os.tmpdir(), "codex-luna-runtime-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const workspaceRoot = path.join(root, "workspace");
  const skillPath = path.join(workspaceRoot, ".agents", "skills", "fake-locator", "SKILL.md");
  const privateRoot = path.join(root, "private");
  const evidenceRoot = path.join(root, "evidence");
  const authSource = path.join(root, "external-auth.json");
  const forbiddenPath = path.join(root, "forbidden.txt");
  const codexEntry = path.join(root, "fake-codex");
  const codeModeHost = path.join(root, "codex-code-mode-host");
  fs.mkdirSync(path.dirname(skillPath), { recursive: true });
  fs.mkdirSync(privateRoot, { recursive: true });
  fs.mkdirSync(evidenceRoot, { recursive: true });
  fs.writeFileSync(skillPath, "---\nname: fake-locator\ndescription: fake\n---\nUse evidence.\n");
  fs.writeFileSync(forbiddenPath, "forbidden source bytes\n");
  fs.writeFileSync(authSource, `${JSON.stringify({
    auth_mode: "chatgpt",
    OPENAI_API_KEY: null,
    tokens: {
      access_token: ACCESS_TOKEN,
      refresh_token: REFRESH_TOKEN,
      id_token: ID_TOKEN,
      account_id: ACCOUNT_ID,
    },
  })}\n`, { mode: 0o600 });
  fs.writeFileSync(codexEntry, FAKE_CODEX, { mode: 0o755 });
  fs.chmodSync(codexEntry, 0o755);
  fs.writeFileSync(codeModeHost, "#!/bin/sh\nexit 0\n", { mode: 0o755 });
  fs.chmodSync(codeModeHost, 0o755);

  const auth = readCodexLunaExternalAuth(authSource, {});
  const callRoot = path.join(privateRoot, "call-1");
  const tracePath = path.join(evidenceRoot, "trace.jsonl");
  const stderrPath = path.join(evidenceRoot, "stderr.log");
  const finalPath = path.join(evidenceRoot, "final.txt");
  return {
    root,
    workspaceRoot,
    skillPath,
    privateRoot,
    evidenceRoot,
    authSource,
    forbiddenPath,
    codexEntry,
    auth,
    callRoot,
    tracePath,
    stderrPath,
    finalPath,
    options: {
      codexEntry,
      auth,
      environment: {
        PATH: process.env.PATH,
        FAKE_CODEX_MODE: fakeMode,
        FAKE_CODEX_SKILL: skillPath,
        FAKE_CODEX_DEVELOPER_INSTRUCTIONS: DEVELOPER_INSTRUCTIONS,
      },
      workspaceRoot,
      skillPath,
      mode: invocationMode,
      prompt: "Diagnose the fixture.",
      developerInstructions: DEVELOPER_INSTRUCTIONS,
      outputSchema: {
        type: "object",
        properties: { diagnosis: { type: "string" } },
        required: ["diagnosis"],
        additionalProperties: false,
      },
      callRoot,
      privateRoot,
      tracePath,
      stderrPath,
      finalPath,
      forbiddenReadPaths: [authSource, forbiddenPath],
      wallSeconds: 10,
      noProgressSeconds: 5,
      ...(invocationMode === "client" ? { mcpServer: { name: "problem-locator", url: "http://127.0.0.1:43123/mcp" } } : {}),
    },
  };
}

test("external auth receipt is memory-only and contains no raw credential", (t) => {
  const fixture = makeFixture(t);
  assert.equal(fixture.auth.receipt.mode, "chatgpt-external-tokens");
  assert.equal(fixture.auth.receipt.transfer, "app-server-account-login-start-memory-only");
  assert.equal(fixture.auth.receipt.credential_persisted, false);
  assert.deepEqual(fixture.auth.receipt.transmitted_fields, ["access_token", "account_id"]);
  assert.deepEqual(fixture.auth.receipt.withheld_fields, ["refresh_token", "id_token"]);
  const serialized = JSON.stringify(fixture.auth.receipt);
  for (const secret of [ACCESS_TOKEN, REFRESH_TOKEN, ID_TOKEN, ACCOUNT_ID]) {
    assert.doesNotMatch(serialized, new RegExp(secret));
  }
});

test("schema generation command does not apply unsupported global strict-config", () => {
  const source = fs.readFileSync(RUNTIME_SOURCE, "utf8");
  const start = source.indexOf("export function generateCodexLunaProtocolSchemaReceipt");
  const end = source.indexOf("\nfunction notificationMatches", start);
  assert.ok(start >= 0 && end > start);
  const implementation = source.slice(start, end);
  assert.match(implementation, /\["app-server", "generate-json-schema", "--experimental", "--out", schemaRoot\]/);
  assert.doesNotMatch(implementation, /strict-config/);
});

test("sandbox permission probes do not apply unsupported global strict-config", () => {
  const source = fs.readFileSync(RUNTIME_SOURCE, "utf8");
  const start = source.indexOf("function spawnProbe");
  const end = source.indexOf("\nasync function loopbackProbe", start);
  assert.ok(start >= 0 && end > start);
  const implementation = source.slice(start, end);
  assert.match(implementation, /\["sandbox", "-P", profileId, "-C", workspaceRoot, "--", \.\.\.command\]/);
  assert.doesNotMatch(implementation, /strict-config/);
});

posixRuntimeTest("fake app-server proves preflight, cleanup, sanitized trace, final, usage, and no auth.json", async (t) => {
  const fixture = makeFixture(t);
  const result = await runCodexLunaAppServerCall(fixture.options);

  assert.equal(result.turn_count, 1);
  assert.equal(result.thread_id, "fake-thread-0001");
  assert.equal(result.turn_id, "fake-turn-0001");
  assert.equal(result.final_text, "fake diagnosis complete");
  assert.deepEqual(result.usage, {
    input_tokens: 11,
    cached_input_tokens: 3,
    cache_write_input_tokens: 0,
    output_tokens: 6,
    reasoning_output_tokens: 2,
    total_tokens: 17,
  });
  assert.equal(result.process.exit_code, 0);
  assert.equal(result.app_server.status, "PASS");
  assert.equal(
    result.app_server.permission_profile.id,
    `test-flow-codex-luna-${fixture.options.mode}`,
  );
  assert.equal(
    result.app_server.permission_profile.invocation_mode,
    fixture.options.mode,
  );
  assert.equal(Object.hasOwn(result.app_server, "permission_profile_id"), false);
  assert.equal(Object.hasOwn(result.app_server, "invocation_mode"), false);
  assert.deepEqual(result.app_server.arguments.slice(0, 3), ["-C", "<WORKSPACE_ROOT>", "app-server"]);
  assert.equal(result.app_server.mcp_tool_call_limit, null);
  assert.equal(result.app_server.developer_instructions.utf8_size, Buffer.byteLength(DEVELOPER_INSTRUCTIONS, "utf8"));
  assert.match(result.app_server.developer_instructions.sha256, /^[a-f0-9]{64}$/u);
  assert.equal(result.app_server.preflight.status, "PASS");
  assert.equal(result.app_server.turn.warning_receipts.length, 1);
  assert.match(result.app_server.turn.warning_receipts[0].redacted_sha256, /^[a-f0-9]{64}$/);
  assert.equal(result.app_server.preflight.workspace_read, "PASS");
  assert.equal(result.app_server.preflight.workspace_write, "DENIED");
  assert.equal(result.app_server.preflight.command_network.status, "DENIED");
  assert.equal(result.app_server.preflight.forbidden_reads.length, 2);
  assert.equal(result.app_server.cleanup.status, "PASS");
  assert.equal(result.app_server.cleanup.stdin_closed, true);
  assert.equal(result.app_server.code_mode_host.status, "PASS");
  assert.match(result.app_server.code_mode_host.sha256, /^[a-f0-9]{64}$/);
  assert.equal(result.app_server.codex_home.auth_json_files, 0);
  assert.equal(result.app_server.codex_home.manifest.some((entry) => path.basename(entry.path) === "auth.json"), false);
  assert.match(result.app_server.trace_sha256, /^[a-f0-9]{64}$/);
  assert.match(result.app_server.final_sha256, /^[a-f0-9]{64}$/);
  assert.equal(result.app_server.turn.raw_response_count, 1);
  assert.equal(result.app_server.turn.thread_token_usage.last.totalTokens, 17);
  assert.equal(result.app_server.turn.raw_response_usage.totalTokens, 17);

  const traceText = fs.readFileSync(fixture.tracePath, "utf8");
  assert.doesNotMatch(traceText, new RegExp(WORK_CONTENT));
  assert.doesNotMatch(traceText, new RegExp(WARNING_CONTENT));
  assert.doesNotMatch(traceText, /not-persisted@example\.test/);
  for (const secret of [ACCESS_TOKEN, REFRESH_TOKEN, ID_TOKEN, ACCOUNT_ID]) {
    assert.doesNotMatch(traceText, new RegExp(secret));
  }
  const envelopes = traceText.trim().split("\n").map((line) => JSON.parse(line));
  assert.ok(envelopes.every((entry, index) => entry.schema_version === 1 && entry.seq === index + 1 && entry.direction === "server_to_client"));
  const rawItem = envelopes.find((entry) => entry.message.method === "rawResponseItem/completed");
  assert.match(rawItem.message.params.item.content_receipt.redacted_sha256, /^[a-f0-9]{64}$/);
  assert.equal(Object.hasOwn(rawItem.message.params.item, "content"), false);
  assert.equal(fs.readFileSync(fixture.finalPath, "utf8"), "fake diagnosis complete\n");

  const scan = auditCodexLunaRuntimeSecrets({
    roots: [fixture.privateRoot, fixture.evidenceRoot, fixture.workspaceRoot],
    auth: fixture.auth,
  });
  assert.equal(scan.status, "PASS");
  assert.ok(scan.scanned_files > 0);
});

posixRuntimeTest("generation Code Mode persists only paired exec identity and hashed source, output, and nested file diff receipts", async (t) => {
  const fixture = makeFixture(t, "generation-apply-patch");
  const result = await runCodexLunaAppServerCall(fixture.options);

  assert.equal(result.app_server.preflight.workspace_write, "ALLOWED");
  assert.deepEqual(result.app_server.turn.raw_custom_tool_names, ["exec"]);
  assert.deepEqual(result.app_server.turn.raw_custom_tool_call_ids, ["code-call-1"]);
  assert.deepEqual(result.app_server.turn.raw_custom_tool_output_call_ids, ["code-call-1"]);
  assert.equal(result.app_server.turn.file_change_count, 1);
  assert.match(result.app_server.turn.file_changes[0].changes[0].diff_receipt.redacted_sha256, /^[a-f0-9]{64}$/);

  const traceText = fs.readFileSync(fixture.tracePath, "utf8");
  assert.doesNotMatch(traceText, new RegExp(PATCH_CONTENT));
  const envelopes = traceText.trim().split("\n").map((line) => JSON.parse(line));
  const customCall = envelopes.find((entry) => entry.message.method === "rawResponseItem/completed" && entry.message.params.item.type === "custom_tool_call");
  assert.deepEqual({ name: customCall.message.params.item.name, call_id: customCall.message.params.item.call_id }, { name: "exec", call_id: "code-call-1" });
  assert.equal(Object.hasOwn(customCall.message.params.item, "input"), false);
  assert.match(customCall.message.params.item.content_receipt.redacted_sha256, /^[a-f0-9]{64}$/);
  const fileChange = envelopes.find((entry) => entry.message.method === "item/completed" && entry.message.params.item.type === "fileChange");
  assert.equal(Object.hasOwn(fileChange.message.params.item.changes[0], "diff"), false);
  assert.match(fileChange.message.params.item.changes[0].diff_receipt.redacted_sha256, /^[a-f0-9]{64}$/);
});

posixRuntimeTest("a server-initiated request fails closed and persists no credential", async (t) => {
  const fixture = makeFixture(t, "server-request");
  await assert.rejects(
    runCodexLunaAppServerCall(fixture.options),
    (error) => error.code === "CODEX_LUNA_APP_SERVER_SERVER_REQUEST_REJECTED"
      && error.details.method === "item/permissions/requestApproval"
      && !JSON.stringify(error).includes(ACCESS_TOKEN),
  );
  assert.equal(fs.readFileSync(fixture.stderrPath, "utf8"), "[Test Flow withheld app-server stderr after a failed secret/protocol boundary.]\n");
  assert.equal(fs.existsSync(fixture.tracePath), false);
  assert.equal(fs.existsSync(path.join(fixture.callRoot, "codex-home", "auth.json")), false);
});

posixRuntimeTest("a credential echo fails closed before transcript persistence", async (t) => {
  const fixture = makeFixture(t, "credential-echo");
  await assert.rejects(
    runCodexLunaAppServerCall(fixture.options),
    (error) => error.code === "CODEX_LUNA_APP_SERVER_SECRET_OUTPUT"
      && !String(error).includes(ACCESS_TOKEN)
      && !JSON.stringify(error.details).includes(ACCESS_TOKEN),
  );
  const stderr = fs.readFileSync(fixture.stderrPath, "utf8");
  assert.doesNotMatch(stderr, new RegExp(ACCESS_TOKEN));
  assert.equal(fs.existsSync(fixture.tracePath), false);
  assert.equal(fs.existsSync(path.join(fixture.callRoot, "codex-home", "auth.json")), false);
});

posixRuntimeTest("a wall timeout persists only the sanitized partial transcript", async (t) => {
  const fixture = makeFixture(t, "wall-timeout");
  fixture.options.wallSeconds = 1;
  fixture.options.noProgressSeconds = 5;
  await assert.rejects(
    runCodexLunaAppServerCall(fixture.options),
    (error) => error.code === "CODEX_LUNA_APP_SERVER_WALL_TIMEOUT",
  );
  const trace = fs.readFileSync(fixture.tracePath, "utf8");
  assert.match(trace, /partial-message/);
  for (const secret of [ACCESS_TOKEN, REFRESH_TOKEN, ID_TOKEN, ACCOUNT_ID]) assert.doesNotMatch(trace, new RegExp(secret));
  assert.equal(fs.readFileSync(fixture.stderrPath, "utf8"), "[Test Flow withheld app-server stderr after a failed secret/protocol boundary.]\n");
});

posixRuntimeTest("a bounded client MCP call count stops tight polling before the stdout boundary", async (t) => {
  const fixture = makeFixture(t, "mcp-call-limit");
  fixture.options.maxMcpToolCalls = 2;
  await assert.rejects(
    runCodexLunaAppServerCall(fixture.options),
    (error) => error.code === "CODEX_LUNA_APP_SERVER_MCP_CALL_LIMIT"
      && error.details.limit === 2
      && error.details.observed === 3,
  );
  const trace = fs.readFileSync(fixture.tracePath, "utf8");
  assert.equal(trace.trim().split("\n").filter((line) => JSON.parse(line).message?.params?.item?.type === "mcpToolCall").length, 3);
  assert.equal(fs.readFileSync(fixture.stderrPath, "utf8"), "[Test Flow withheld app-server stderr after a failed secret/protocol boundary.]\n");
});

posixRuntimeTest("a coalesced turn response and MCP limit failure settle without a late notification waiter", { timeout: 3_000 }, async (t) => {
  const fixture = makeFixture(t, "coalesced-mcp-call-limit");
  fixture.options.maxMcpToolCalls = 2;
  fixture.options.wallSeconds = 1;
  fixture.options.noProgressSeconds = 1;
  await assert.rejects(
    runCodexLunaAppServerCall(fixture.options),
    (error) => error.code === "CODEX_LUNA_APP_SERVER_MCP_CALL_LIMIT"
      && error.details.limit === 2
      && error.details.observed === 3,
  );
});

posixRuntimeTest("a terminal error notification persists only official closed fields and a receipt", async (t) => {
  const fixture = makeFixture(t, "error-notification");
  await assert.rejects(
    runCodexLunaAppServerCall(fixture.options),
    (error) => {
      assert.equal(error.code, "CODEX_LUNA_APP_SERVER_ERROR_NOTIFICATION");
      assert.deepEqual(error.details, { codex_error_info: "serverOverloaded", http_status_code: null, will_retry: false });
      return true;
    },
  );
  const trace = fs.readFileSync(fixture.tracePath, "utf8");
  assert.match(trace, /serverOverloaded/);
  assert.match(trace, /\"will_retry\":false/);
  assert.doesNotMatch(trace, /private provider detail/);
  assert.doesNotMatch(trace, /private additional detail/);
  assert.match(trace, /error_receipt/);
});

posixRuntimeTest("a failed provider turn retains its terminal usage", async (t) => {
  const fixture = makeFixture(t, "error-after-usage");
  await assert.rejects(
    runCodexLunaAppServerCall(fixture.options),
    (error) => {
      assert.equal(error.code, "CODEX_LUNA_APP_SERVER_ERROR_NOTIFICATION");
      assert.deepEqual(error.details.usage, {
        input_tokens: 11,
        cached_input_tokens: 3,
        cache_write_input_tokens: 0,
        output_tokens: 6,
        reasoning_output_tokens: 2,
        total_tokens: 17,
      });
      return true;
    },
  );
});

posixRuntimeTest("an explicitly retryable error notification remains in the audited turn and does not abort", async (t) => {
  const fixture = makeFixture(t, "retrying-error-notification");
  const result = await runCodexLunaAppServerCall(fixture.options);
  assert.equal(result.process.exit_code, 0);
  assert.deepEqual(result.app_server.turn.retryable_error_receipts.map(({ thread_id, turn_id, ...receipt }) => receipt), [{
    codex_error_info: "responseStreamDisconnected",
    http_status_code: 503,
    will_retry: true,
  }]);
  const trace = fs.readFileSync(fixture.tracePath, "utf8");
  assert.match(trace, /responseStreamDisconnected/);
  assert.match(trace, /\"will_retry\":true/);
  assert.doesNotMatch(trace, /private transient detail/);
});

posixRuntimeTest("a rejected raw shell function persists its closed name in the sanitized trace", async (t) => {
  const fixture = makeFixture(t, "wrong-raw-function");
  await assert.rejects(
    runCodexLunaAppServerCall(fixture.options),
    (error) => error.code === "CODEX_LUNA_APP_SERVER_RAW_SHELL_FUNCTION_REJECTED" && error.details.function_name === "unexpected_shell",
  );
  const trace = fs.readFileSync(fixture.tracePath, "utf8");
  assert.match(trace, /unexpected_shell/);
  for (const secret of [ACCESS_TOKEN, REFRESH_TOKEN, ID_TOKEN, ACCOUNT_ID]) assert.doesNotMatch(trace, new RegExp(secret));
});

posixRuntimeTest("an App Server response failure retains its request id, code, and message", async (t) => {
  const fixture = makeFixture(t, "response-error");
  await assert.rejects(
    runCodexLunaAppServerCall(fixture.options),
    (error) => error.code === "CODEX_LUNA_APP_SERVER_RESPONSE_ERROR"
      && error.details.id === 5
      && error.details.response_code === -32001
      && error.details.response_message === "client profile rejected",
  );
});

posixRuntimeTest("client cleanup ignores informational MCP Server lifecycle notifications", async (t) => {
  const fixture = makeFixture(t, "late-mcp-notification");
  const result = await runCodexLunaAppServerCall(fixture.options);
  assert.equal(result.app_server.cleanup.status, "PASS");
  assert.equal(result.process.exit_code, 0);
});

posixRuntimeTest("service invocation may place shell HOME in an existing writable workspace subtree", async (t) => {
  const fixture = makeFixture(t);
  const writableParent = path.join(fixture.workspaceRoot, "runtime", "tool-state", "service");
  fs.mkdirSync(writableParent, { recursive: true });
  fixture.options.shellHome = path.join(writableParent, ".shell-home");
  fixture.options.shellPath = `${path.join(fixture.root, "repository-bin")}:/usr/bin:/bin:/usr/sbin:/sbin`;
  const result = await runCodexLunaAppServerCall(fixture.options);
  assert.equal(result.app_server.cleanup.status, "PASS");
  assert.equal(fs.existsSync(fixture.options.shellHome), true);
  assert.match(result.app_server.permission_profile.bytes_utf8, new RegExp(`PATH = ${JSON.stringify(fixture.options.shellPath)}`));
});
