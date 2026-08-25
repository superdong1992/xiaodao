import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { canonicalJson } from "../../../lib/util.mjs";
import {
  CLAUDE_DEEPSEEK_BASH_PROGRAMS,
  CLAUDE_DEEPSEEK_CLI_SHA256,
  CLAUDE_DEEPSEEK_E2E_PHASES,
  CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS,
  CLAUDE_DEEPSEEK_MODEL,
  CLAUDE_DEEPSEEK_PUBLIC_TOOLS,
  CLAUDE_DEEPSEEK_VERSION,
  aggregateClaudeUsage,
  assertMethodsPackageUnchanged,
  auditClaudeInvocations,
  auditClaudeStream,
  auditClientBash,
  buildMethodsCacheManifest,
  buildMethodsProducerIdentity,
  methodsCachePath,
  publishMethodsCacheAtomically,
  validateMethodsCache,
} from "../runtime/claude-deepseek-contract.mjs";

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-deepseek-contract-"));
  const wiki = path.join(root, "wiki.md");
  fs.writeFileSync(wiki, "# RPC timeout\n");
  const meta = path.join(root, "meta");
  fs.mkdirSync(path.join(meta, "references"), { recursive: true });
  fs.mkdirSync(path.join(meta, "scripts"), { recursive: true });
  fs.writeFileSync(path.join(meta, "SKILL.md"), "---\nname: wiki-to-diagnosis-skill\ndescription: test\n---\n");
  fs.writeFileSync(path.join(meta, "references", "output-contract.md"), "contract\n");
  fs.writeFileSync(path.join(meta, "scripts", "validate_generated_skill.py"), "# validator\n");
  const registration = path.join(root, "registration-template.json");
  fs.writeFileSync(registration, `${JSON.stringify({ registration_id: "rpc-timeout-methods-v1", package: { skill_name: "diagnose-rpc-timeout" } })}\n`);
  const packageRoot = path.join(root, "package");
  fs.mkdirSync(path.join(packageRoot, "references"), { recursive: true });
  fs.writeFileSync(path.join(packageRoot, "SKILL.md"), "skill\n");
  fs.writeFileSync(path.join(packageRoot, "methods.json"), "{}\n");
  fs.writeFileSync(path.join(packageRoot, "references", "method.md"), "method\n");
  return { root, wiki, meta, registration, packageRoot };
}

function claudeIdentity() {
  return {
    status: "PASS",
    cli: {
      version: "2.1.89 (Claude Code)", package_version: "2.1.89", cli_sha256: "a".repeat(64),
      package_tree_digest: "b".repeat(64), platform: "darwin", architecture: "arm64",
    },
    settings: { fingerprint: "c".repeat(64) },
  };
}

function usage(overrides = {}) {
  return { input_tokens: 10, output_tokens: 5, cache_creation_input_tokens: 3, cache_read_input_tokens: 2, cost_usd: 0.01, ...overrides };
}

function invocations(phases, workflow = "e2e") {
  return phases.map((phase) => ({
    phase,
    model: CLAUDE_DEEPSEEK_MODEL,
    attempt: 1,
    retry: 0,
    status: "PASS",
    terminal: true,
    turns: 2,
    wall_timeout_seconds: workflow === "methods" ? 1800 : 600,
    started_at_utc: "2026-08-24T00:00:00.000Z",
    finished_at_utc: "2026-08-24T00:00:01.000Z",
    usage: usage(),
  }));
}

test("Claude identity constants freeze 2.1.89, CLI hash, DeepSeek model, and 64k output", () => {
  assert.equal(CLAUDE_DEEPSEEK_VERSION, "2.1.89");
  assert.equal(CLAUDE_DEEPSEEK_CLI_SHA256, "a9950ef6407fdc750bddb673852485500387e524a99d42385cb81e7d17128e01");
  assert.equal(CLAUDE_DEEPSEEK_MODEL, "deepseek-v4-flash[1m]");
  assert.equal(CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS, 64_000);
  assert.deepEqual(CLAUDE_DEEPSEEK_E2E_PHASES, ["CLIENT", "ROUTE", "LOGPARSE", "DIAGNOSE", "REVIEW"]);
  assert.equal(CLAUDE_DEEPSEEK_PUBLIC_TOOLS.length, 7);
});

test("Methods producer identity includes settings and cache freezes through atomic rename", () => {
  const f = fixture();
  const producer = buildMethodsProducerIdentity({ wiki: f.wiki, metaSkillRoot: f.meta, registrationTemplate: f.registration, claudeIdentity: claudeIdentity() });
  assert.match(producer.producer_identity, /^[a-f0-9]{64}$/);
  assert.equal(producer.inputs.claude.settings_fingerprint, "c".repeat(64));
  const cacheRoot = path.join(f.root, "cache");
  const destination = methodsCachePath(cacheRoot, producer.producer_identity);
  assert.equal(destination, path.join(cacheRoot, "claude-deepseek-methods", producer.producer_identity));
  const published = publishMethodsCacheAtomically({ cacheRoot, producer, packageRoot: f.packageRoot, registrationTemplate: f.registration, stagingRoot: path.join(f.root, "stage-one") });
  assert.equal(published.published, true);
  assert.equal(validateMethodsCache({ cacheRoot, producer, registrationTemplate: f.registration }).status, "PASS");
  const identical = publishMethodsCacheAtomically({ cacheRoot, producer, packageRoot: f.packageRoot, registrationTemplate: f.registration, stagingRoot: path.join(f.root, "stage-two") });
  assert.equal(identical.published, false);
  fs.appendFileSync(path.join(destination, "package", "diagnose-rpc-timeout", "SKILL.md"), "tamper\n");
  assert.throws(() => validateMethodsCache({ cacheRoot, producer, registrationTemplate: f.registration }), (error) => error.code === "CLAUDE_DEEPSEEK_METHODS_CACHE_IDENTITY_MISMATCH");
});

test("Methods package receipt detects post-validation tampering", () => {
  const f = fixture();
  const producer = buildMethodsProducerIdentity({ wiki: f.wiki, metaSkillRoot: f.meta, registrationTemplate: f.registration, claudeIdentity: claudeIdentity() });
  const cacheRoot = path.join(f.root, "cache");
  publishMethodsCacheAtomically({ cacheRoot, producer, packageRoot: f.packageRoot, registrationTemplate: f.registration, stagingRoot: path.join(f.root, "stage") });
  const receipt = validateMethodsCache({ cacheRoot, producer, registrationTemplate: f.registration });
  assert.equal(assertMethodsPackageUnchanged(receipt).status, "PASS");
  fs.writeFileSync(path.join(receipt.package_root, "methods.json"), '{"changed":true}\n');
  assert.throws(() => assertMethodsPackageUnchanged(receipt), (error) => error.code === "CLAUDE_DEEPSEEK_METHODS_PACKAGE_DRIFT");
});

test("cache manifest is canonical and binds byte inventory plus atomic policy", () => {
  const f = fixture();
  const producer = buildMethodsProducerIdentity({ wiki: f.wiki, metaSkillRoot: f.meta, registrationTemplate: f.registration, claudeIdentity: claudeIdentity() });
  const manifest = buildMethodsCacheManifest({ producer, packageRoot: f.packageRoot, registrationTemplate: f.registration });
  assert.equal(manifest.publish.strategy, "staging-directory-atomic-rename");
  assert.equal(manifest.package.files.filter((item) => item.kind === "file").length, 3);
  assert.equal(canonicalJson(manifest), canonicalJson(JSON.parse(canonicalJson(manifest))));
});

test("stream audit requires one init/result, pinned model, bounded turns, and allowed tools", () => {
  const events = [
    { type: "system", subtype: "init", model: CLAUDE_DEEPSEEK_MODEL },
    { type: "assistant", message: { content: [{ type: "tool_use", id: "t1", name: "Read", input: { file_path: "inputs/wiki.md" } }] } },
    { type: "result", subtype: "success", is_error: false, num_turns: 2, usage: usage(), total_cost_usd: 0.01 },
  ];
  const receipt = auditClaudeStream(events, { phase: "METHODS_BOOTSTRAP", allowedTools: ["Read", "Write", "Skill"], maxTurns: 16, wallTimeoutSeconds: 1800 });
  assert.equal(receipt.status, "PASS");
  assert.deepEqual(receipt.tools.map((item) => item.name), ["Read"]);
  const bad = structuredClone(events);
  bad[1].message.content[0].name = "Bash";
  assert.throws(() => auditClaudeStream(bad, { phase: "METHODS_BOOTSTRAP", allowedTools: ["Read", "Write", "Skill"], maxTurns: 16, wallTimeoutSeconds: 1800 }), (error) => error.code === "CLAUDE_DEEPSEEK_STREAM_TOOL_FORBIDDEN");
});

test("usage aggregation is cache-inclusive and enforces exactly one or five no-retry processes", () => {
  const methods = auditClaudeInvocations(invocations(["METHODS_BOOTSTRAP"], "methods"), { workflow: "methods" });
  assert.equal(methods.aggregate.total_tokens, 20);
  const e2e = auditClaudeInvocations(invocations(CLAUDE_DEEPSEEK_E2E_PHASES), { workflow: "e2e" });
  assert.equal(e2e.aggregate.total_tokens, 100);
  assert.deepEqual(aggregateClaudeUsage(invocations(["CLIENT", "ROUTE"])), {
    input_tokens: 20, output_tokens: 10, cache_creation_input_tokens: 6, cache_read_input_tokens: 4, total_tokens: 40, cost_usd: 0.02,
  });
  assert.throws(() => auditClaudeInvocations(invocations(["CLIENT", "ROUTE", "DIAGNOSE", "REVIEW"]), { workflow: "e2e" }), (error) => error.code === "CLAUDE_DEEPSEEK_INVOCATION_COUNT_INVALID");
  const retried = invocations(CLAUDE_DEEPSEEK_E2E_PHASES);
  retried[2].retry = 1;
  assert.throws(() => auditClaudeInvocations(retried, { workflow: "e2e" }), (error) => error.code === "CLAUDE_DEEPSEEK_INVOCATION_IDENTITY_INVALID");
  const over = invocations(CLAUDE_DEEPSEEK_E2E_PHASES);
  over[0].usage.cache_read_input_tokens = 2_000_001;
  assert.throws(() => auditClaudeInvocations(over, { workflow: "e2e" }), (error) => error.code === "CLAUDE_DEEPSEEK_BUDGET_EXCEEDED");
});

test("Bash audit permits exact openssl/stat and one descriptor-bound curl PUT only", () => {
  const archivePath = "/private/tmp/client/input/logs.zip";
  const archive = { size: 42, sha256: "a".repeat(64) };
  const descriptor = {
    url: "http://127.0.0.1:8123/uploads/token",
    required_headers: { "Content-Type": "application/zip", "Content-Length": "42", "X-Content-SHA256": "a".repeat(64), "Idempotency-Key": "attachment" },
  };
  const headers = Object.entries(descriptor.required_headers).flatMap(([name, value]) => ["-H", `'${name}: ${value}'`]).join(" ");
  const commands = [
    { command: `/usr/bin/openssl dgst -sha256 ${archivePath}`, status: "completed", exit_code: 0, stdout: `SHA2-256(${archivePath})= ${archive.sha256}\n` },
    { command: `/usr/bin/stat -f %z ${archivePath}`, status: "completed", exit_code: 0, stdout: "42\n" },
    { command: `/usr/bin/curl --request PUT ${headers} --upload-file ${archivePath} '${descriptor.url}'`, status: "completed", exit_code: 0, stdout: "" },
  ];
  assert.deepEqual(auditClientBash(commands, { archivePath, archive, descriptor }).programs, CLAUDE_DEEPSEEK_BASH_PROGRAMS);
  assert.throws(() => auditClientBash([...commands, commands[2]], { archivePath, archive, descriptor }), (error) => error.code === "CLAUDE_DEEPSEEK_BASH_CARDINALITY_INVALID");
  const chained = structuredClone(commands);
  chained[0].command += " ; uname -a";
  assert.throws(() => auditClientBash(chained, { archivePath, archive, descriptor }), (error) => error.code === "CLAUDE_DEEPSEEK_BASH_SYNTAX_FORBIDDEN");
});
