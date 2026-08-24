import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  auditCodexLunaRuntimeSecrets,
  readCodexLunaExternalAuth,
  runCodexLunaAppServerCall,
} from "../runtime-support/codex-luna-app-server-runtime.mjs";

const RUNTIME_SOURCE = path.resolve(
  path.dirname(new URL(import.meta.url).pathname),
  "../runtime-support/codex-luna-app-server-runtime.mjs",
);
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

const FAKE_CODEX = String.raw`#!/usr/bin/env node
const fs = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");

const args = process.argv.slice(2);
const fakeMode = process.env.FAKE_CODEX_MODE || "success";
const skillPath = process.env.FAKE_CODEX_SKILL;

if (args[0] === "sandbox") {
  const separator = args.indexOf("--");
  const command = args.slice(separator + 1);
  if (command[0] === "/bin/cat" && command[1] === skillPath) {
    process.stdout.write(fs.readFileSync(skillPath, "utf8"));
    process.exit(0);
  }
  process.exit(1);
}

if (args[0] !== "app-server" || args[1] !== "--stdio") process.exit(64);

const send = (value) => process.stdout.write(JSON.stringify(value) + "\n");
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
    return;
  }
  if (message.method === "account/read") {
    send({ id: message.id, result: { account: { type: "chatgpt", email: "not-persisted@example.test", planType: "plus" }, requiresOpenaiAuth: true } });
    return;
  }
  if (message.method === "permissionProfile/list") {
    send({ id: message.id, result: { data: [{ id: "test-flow-codex-luna-diagnosis", description: "fake least privilege", allowed: true }], nextCursor: null } });
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
          source: "appServer",
          modelProvider: "openai",
          cwd,
          cliVersion: "0.149.0-alpha.4.1",
          turns: [],
        },
        model: "gpt-5.6-luna",
        modelProvider: "openai",
        serviceTier: null,
        cwd,
        runtimeWorkspaceRoots: [],
        instructionSources: [],
        approvalPolicy: "never",
        approvalsReviewer: "user",
        sandbox: { type: "readOnly", networkAccess: false },
        activePermissionProfile: { id: "test-flow-codex-luna-diagnosis", extends: null },
        reasoningEffort: "medium",
        multiAgentMode: "explicitRequestOnly",
      },
    });
    send({ method: "thread/started", params: { thread: { id: threadId } } });
    return;
  }
  if (message.method === "turn/start") {
    send({ id: message.id, result: { turn: turn("inProgress") } });
    send({ method: "turn/started", params: { threadId, turn: turn("inProgress") } });
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
    send({ id: message.id, result: {} });
    lines.close();
    setImmediate(() => process.exit(0));
  }
});
`;

function makeFixture(t, fakeMode = "success") {
  const root = fs.mkdtempSync(path.join(fs.existsSync("/private/tmp") ? "/private/tmp" : os.tmpdir(), "codex-luna-runtime-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const workspaceRoot = path.join(root, "workspace");
  const skillPath = path.join(workspaceRoot, ".agents", "skills", "fake-locator", "SKILL.md");
  const privateRoot = path.join(root, "private");
  const evidenceRoot = path.join(root, "evidence");
  const authSource = path.join(root, "external-auth.json");
  const forbiddenPath = path.join(root, "forbidden.txt");
  const codexEntry = path.join(root, "fake-codex");
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
      },
      workspaceRoot,
      skillPath,
      mode: "diagnosis",
      prompt: "Diagnose the fixture.",
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

test("fake app-server proves preflight, cleanup, sanitized trace, final, usage, and no auth.json", async (t) => {
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
  assert.equal(result.app_server.preflight.status, "PASS");
  assert.equal(result.app_server.preflight.workspace_read, "PASS");
  assert.equal(result.app_server.preflight.workspace_write, "DENIED");
  assert.equal(result.app_server.preflight.command_network.status, "DENIED");
  assert.equal(result.app_server.preflight.forbidden_reads.length, 2);
  assert.equal(result.app_server.cleanup.status, "PASS");
  assert.equal(result.app_server.cleanup.stdin_closed, true);
  assert.equal(result.app_server.codex_home.auth_json_files, 0);
  assert.equal(result.app_server.codex_home.manifest.some((entry) => path.basename(entry.path) === "auth.json"), false);
  assert.match(result.app_server.trace_sha256, /^[a-f0-9]{64}$/);
  assert.match(result.app_server.final_sha256, /^[a-f0-9]{64}$/);
  assert.equal(result.app_server.turn.raw_response_count, 1);
  assert.equal(result.app_server.turn.thread_token_usage.last.totalTokens, 17);
  assert.equal(result.app_server.turn.raw_response_usage.totalTokens, 17);

  const traceText = fs.readFileSync(fixture.tracePath, "utf8");
  assert.doesNotMatch(traceText, new RegExp(WORK_CONTENT));
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

test("a server-initiated request fails closed and persists no credential", async (t) => {
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

test("a credential echo fails closed before transcript persistence", async (t) => {
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
