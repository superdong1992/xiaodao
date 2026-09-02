import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import {
  buildMethodsProducerIdentity,
  MACOS_CODEX_LUNA_BLIND_REVIEW_E2E_CALLS,
  MACOS_CODEX_LUNA_BLIND_REVIEW_E2E_MAX_CALLS,
  MACOS_CODEX_LUNA_E2E_CALLS,
  MACOS_CODEX_LUNA_E2E_MAX_CALLS,
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
  assert.deepEqual(macosCodexLunaE2EPhases("multiple-rpc-timeouts"), ["SPECIALIST"]);
  assert.equal(macosCodexLunaE2ECallCount("multiple-rpc-timeouts"), 1);
  assert.deepEqual(macosCodexLunaE2EPhases("multiple-rpc-timeouts", "BLIND_CONSENSUS"), ["SPECIALIST", "REVIEWER"]);
  assert.equal(macosCodexLunaE2ECallCount("multiple-rpc-timeouts", "BLIND_CONSENSUS"), 2);
  assert.throws(() => macosCodexLunaE2ECallCount("api-execution-overrun"), { code: "MACOS_CODEX_LUNA_SCENARIO_UNSUPPORTED" });
  assert.throws(() => macosCodexLunaE2ECallCount("multiple-rpc-timeouts", "UNKNOWN"), { code: "MACOS_CODEX_LUNA_EVALUATION_MODE_INVALID" });
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
