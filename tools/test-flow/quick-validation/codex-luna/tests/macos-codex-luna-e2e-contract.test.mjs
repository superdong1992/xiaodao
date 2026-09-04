import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import {
  auditMcpToolCalls,
  buildMethodsProducerIdentity,
  MACOS_CODEX_LUNA_BLIND_REVIEW_E2E_CALLS,
  MACOS_CODEX_LUNA_BLIND_REVIEW_E2E_MAX_CALLS,
  MACOS_CODEX_LUNA_E2E_CALLS,
  MACOS_CODEX_LUNA_E2E_MAX_CALLS,
  MACOS_CODEX_LUNA_CLIENT_PROMPT_VERSION,
  MACOS_CODEX_LUNA_SCENARIOS,
  macosCodexLunaE2ECallCount,
  macosCodexLunaE2EPhases,
  methodsCachePath,
  scenarioPaths,
  STANDALONE_CODEX_LUNA_SCENARIOS,
} from "../runtime/macos-codex-luna-e2e-contract.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..", "..");

test("Codex/Luna E2E contract defaults to one/two calls and preserves the blind two/four bound", () => {
  assert.deepEqual(MACOS_CODEX_LUNA_SCENARIOS, ["multiple-rpc-timeouts"]);
  assert.deepEqual(STANDALONE_CODEX_LUNA_SCENARIOS, ["multiple-rpc-timeouts"]);
  assert.equal(MACOS_CODEX_LUNA_E2E_CALLS, 1);
  assert.equal(MACOS_CODEX_LUNA_E2E_MAX_CALLS, 2);
  assert.equal(MACOS_CODEX_LUNA_BLIND_REVIEW_E2E_CALLS, 2);
  assert.equal(MACOS_CODEX_LUNA_BLIND_REVIEW_E2E_MAX_CALLS, 4);
  assert.equal(MACOS_CODEX_LUNA_CLIENT_PROMPT_VERSION, 4);
  assert.deepEqual(macosCodexLunaE2EPhases("multiple-rpc-timeouts"), ["SPECIALIST"]);
  assert.equal(macosCodexLunaE2ECallCount("multiple-rpc-timeouts"), 1);
  assert.deepEqual(macosCodexLunaE2EPhases("multiple-rpc-timeouts", "BLIND_CONSENSUS"), ["SPECIALIST", "REVIEWER"]);
  assert.equal(macosCodexLunaE2ECallCount("multiple-rpc-timeouts", "BLIND_CONSENSUS"), 2);
  assert.throws(() => macosCodexLunaE2ECallCount("api-execution-overrun"), { code: "MACOS_CODEX_LUNA_SCENARIO_UNSUPPORTED" });
  assert.throws(() => macosCodexLunaE2ECallCount("multiple-rpc-timeouts", "UNKNOWN"), { code: "MACOS_CODEX_LUNA_EVALUATION_MODE_INVALID" });
});

test("Codex/Luna client fixture uses the PUT receipt revision without a READY refresh", () => {
  const skill = fs.readFileSync(
    path.join(REPO_ROOT, "tools", "test-flow", "quick-validation", "codex-luna", "fixtures", "client-skill", "problem-locator-client", "SKILL.md"),
    "utf8",
  );
  assert.match(skill, /use the PUT response's exact new revision/u);
  assert.match(skill, /never add a get-case call merely to reconfirm READY/u);
  assert.doesNotMatch(skill, /Immediately after the successful PUT, refresh the same Case/u);
});

function clientMcpCall(tool, arguments_, result = null) {
  return {
    item_id: `item-${tool}`,
    server: "problem-locator",
    tool,
    status: "completed",
    arguments: arguments_,
    result,
    error: null,
  };
}

function terminalClientCalls({ inline = true, list = false } = {}) {
  const caseId = "00000000-0000-4000-8000-000000000301";
  const attachmentId = "00000000-0000-4000-8000-000000000302";
  const calls = [
    clientMcpCall("problem_locator_create_case", { request_id: "create", wait_seconds: 0 }),
    clientMcpCall("problem_locator_get_case", { case_id: caseId, wait_for_job_id: null, wait_seconds: 0 }),
    clientMcpCall("problem_locator_prepare_attachment", { request_id: "prepare", case_id: caseId, expected_case_revision: 1 }),
    clientMcpCall("problem_locator_submit_supplement", { request_id: "submit", case_id: caseId, expected_case_revision: 2, attachment_ids: [attachmentId], input_names: [], input_values: [], wait_seconds: 30 }),
    clientMcpCall("problem_locator_get_case", { case_id: caseId, wait_for_job_id: null, wait_seconds: 30 }, {
      structuredContent: {
        ok: true,
        error: null,
        data: {
          case_view: { case_id: caseId, status: "RESOLVED" },
          wait_timed_out: false,
          ...(inline ? { artifact_views: [{ artifact_id: "artifact-one" }] } : {}),
        },
      },
    }),
  ];
  if (list) calls.push(clientMcpCall("problem_locator_list_artifacts", { case_id: caseId }));
  return { attachmentId, calls };
}

test("Codex/Luna client audit removes the terminal list round trip and permits only a missing-field fallback", () => {
  const current = terminalClientCalls();
  const currentReceipt = auditMcpToolCalls(current.calls, { attachmentId: current.attachmentId, uploadRevision: 2 });
  assert.equal(currentReceipt.call_count, 5);
  assert.equal(currentReceipt.artifact_projection_source, "GET_CASE");

  const legacy = terminalClientCalls({ inline: false, list: true });
  assert.equal(auditMcpToolCalls(legacy.calls).artifact_projection_source, "LIST_ARTIFACTS");

  const redundant = terminalClientCalls({ inline: true, list: true });
  assert.throws(
    () => auditMcpToolCalls(redundant.calls),
    (error) => error.code === "MACOS_CODEX_LUNA_ARTIFACT_LIST_REDUNDANT",
  );

  const missingFallback = terminalClientCalls({ inline: false });
  assert.throws(
    () => auditMcpToolCalls(missingFallback.calls),
    (error) => error.code === "MACOS_CODEX_LUNA_ARTIFACT_FALLBACK_INVALID",
  );

  const unwaitedWrite = terminalClientCalls();
  unwaitedWrite.calls.find((call) => call.tool === "problem_locator_submit_supplement").arguments.wait_seconds = 0;
  assert.throws(
    () => auditMcpToolCalls(unwaitedWrite.calls),
    (error) => error.code === "MACOS_CODEX_LUNA_SUBMIT_WAIT_INVALID",
  );

  const redundantReadyRefresh = terminalClientCalls();
  redundantReadyRefresh.calls.splice(
    3,
    0,
    clientMcpCall("problem_locator_get_case", { case_id: "case", wait_for_job_id: null, wait_seconds: 0 }),
  );
  assert.throws(
    () => auditMcpToolCalls(redundantReadyRefresh.calls),
    (error) => error.code === "MACOS_CODEX_LUNA_READY_REFRESH_REDUNDANT",
  );
});

test("fixed scenario paths point to the release driver and raw source logs", () => {
  const paths = scenarioPaths(REPO_ROOT, "multiple-rpc-timeouts");
  assert.match(paths.case.replaceAll("\\", "/"), /tests\/cases\/release\/rpc-timeout-anonymized\/scenarios\/multiple-rpc-timeouts\/driver\.json$/u);
  assert.equal(path.basename(paths.client_log), "client.log");
  assert.equal(path.basename(paths.server_log), "server.log");
  const driver = JSON.parse(fs.readFileSync(paths.case, "utf8"));
  assert.equal(driver.scenario_id, "multiple-rpc-timeouts");
  assert.deepEqual(driver.attachment_anchor_names, ["client", "server"]);
});

test("Methods producer identity remains separate from the P2 model-cert registration input", (context) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "codex-luna-producer-"));
  context.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const meta = path.join(root, "meta");
  fs.mkdirSync(path.join(meta, "references"), { recursive: true });
  fs.mkdirSync(path.join(meta, "scripts"), { recursive: true });
  fs.writeFileSync(path.join(meta, "SKILL.md"), "# Meta\n");
  fs.writeFileSync(path.join(meta, "references", "output-contract.md"), "# Contract\n");
  fs.writeFileSync(path.join(meta, "scripts", "validate_generated_skill.py"), "# validator\n");
  const wiki = path.join(root, "wiki.md");
  const registration = path.join(root, "registration-template.json");
  fs.writeFileSync(wiki, "# Wiki\n");
  fs.writeFileSync(registration, "{}\n");
  const producer = buildMethodsProducerIdentity({ wiki, metaSkillRoot: meta, registrationTemplate: registration, codexIdentity: { status: "PASS" } });
  assert.match(producer.producer_identity, /^[a-f0-9]{64}$/u);
  assert.deepEqual(producer.inputs.model, { model: "gpt-5.6-luna", reasoning_effort: "medium" });
  assert.match(methodsCachePath(path.resolve(root, "cache"), producer.producer_identity).replaceAll("\\", "/"), /codex-luna-methods\/[a-f0-9]{64}$/u);
});
