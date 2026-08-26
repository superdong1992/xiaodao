import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { canonicalJson } from "../../../lib/util.mjs";
import {
  CLAUDE_DEEPSEEK_BASH_PROGRAMS,
  CLAUDE_DEEPSEEK_CLI_SHA256,
  CLAUDE_DEEPSEEK_CLIENT_PROMPT_VERSION,
  CLAUDE_DEEPSEEK_CONTRACT_VERSION,
  CLAUDE_DEEPSEEK_E2E_PHASES,
  CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS,
  CLAUDE_DEEPSEEK_MODEL,
  CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
  CLAUDE_DEEPSEEK_PUBLIC_TOOLS,
  CLAUDE_DEEPSEEK_SCENARIOS,
  CLAUDE_DEEPSEEK_VERSION,
  aggregateClaudeUsage,
  assertRegistrationUnchanged,
  auditClaudeInvocations,
  auditClaudeStream,
  auditClientBash,
  buildRegistrationCacheManifest,
  buildRegistrationProducerIdentity,
  claudeDeepseekE2ECallCount,
  claudeDeepseekE2EPhases,
  loadScenarioFacts,
  mapScenarioToCreateCase,
  publishRegistrationCacheAtomically,
  registrationCachePath,
  validateRegistrationCache,
} from "../runtime/claude-deepseek-contract.mjs";

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-deepseek-contract-"));
  const wiki = path.join(root, "wiki.md");
  fs.writeFileSync(wiki, "# RPC timeout\n");
  const meta = path.join(root, "meta");
  fs.mkdirSync(path.join(meta, "references"), { recursive: true });
  fs.mkdirSync(path.join(meta, "scripts"), { recursive: true });
  fs.writeFileSync(path.join(meta, "SKILL.md"), "---\nname: wiki-to-logparse-diagnosis-skill\ndescription: test\n---\n");
  fs.writeFileSync(path.join(meta, "references", "output-contract.md"), "contract\n");
  fs.writeFileSync(path.join(meta, "scripts", "validate_generated_skill.py"), "# validator\n");
  const registrationRoot = path.join(root, "rpc-timeout-methods-v1");
  fs.mkdirSync(path.join(registrationRoot, "package"), { recursive: true });
  const registration = path.join(registrationRoot, "registration-template.json");
  fs.writeFileSync(registration, `${JSON.stringify({
    schema_version: 1,
    registration_id: "rpc-timeout-methods-v1",
    version: "1.0.0",
    capability: "test",
    deployment_scope: "PRODUCTION",
    summary: "test",
    package: { relative_path: "package/diagnose-rpc-timeout", skill_name: "diagnose-rpc-timeout", source_wiki_sha256: crypto.createHash("sha256").update(fs.readFileSync(wiki)).digest("hex") },
    runtime: { diagnose: {}, review: {}, preprocessing: {
      requires_logparse: true,
      logparse_product: "default",
      logparse_plan: {
        attachment_requirement: "log_archive",
        problem_time_binding: { source: "USER_FACT", name: "problem_time" },
        anchors: [
          { label: "client", module: { source: "SKILL_FIXED", value: "rpc" }, slot: { source: "USER_FACT", name: "client_slot" }, process_name: { source: "USER_FACT", name: "client_process_name" }, pid: { source: "USER_FACT", name: "client_pid" } },
          { label: "server", module: { source: "SKILL_FIXED", value: "rpc" }, slot: { source: "USER_FACT", name: "server_slot" }, process_name: { source: "USER_FACT", name: "server_process_name" }, pid: { source: "USER_FACT", name: "server_pid" } },
        ],
      },
    } },
  })}\n`);
  const packageRoot = path.join(registrationRoot, "package", "diagnose-rpc-timeout");
  fs.mkdirSync(path.join(packageRoot, "references"), { recursive: true });
  fs.writeFileSync(path.join(packageRoot, "SKILL.md"), "skill\n");
  fs.writeFileSync(path.join(packageRoot, "methods.json"), `${JSON.stringify({ required_user_inputs: ["problem_time", "client_slot", "client_process_name", "server_slot", "server_process_name", "client_pid", "server_pid"], required_artifacts: ["log_archive"] })}\n`);
  fs.writeFileSync(path.join(packageRoot, "references", "method.md"), "method\n");
  return { root, wiki, meta, registration, registrationRoot, packageRoot };
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
  assert.equal(CLAUDE_DEEPSEEK_CONTRACT_VERSION, 3);
  assert.equal(CLAUDE_DEEPSEEK_CLIENT_PROMPT_VERSION, 3);
  assert.equal(CLAUDE_DEEPSEEK_VERSION, "2.1.89");
  assert.equal(CLAUDE_DEEPSEEK_CLI_SHA256, "a9950ef6407fdc750bddb673852485500387e524a99d42385cb81e7d17128e01");
  assert.equal(CLAUDE_DEEPSEEK_MODEL, "deepseek-v4-flash[1m]");
  assert.equal(CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS, 64_000);
  assert.deepEqual(CLAUDE_DEEPSEEK_E2E_PHASES, ["CLIENT", "ROUTE", "LOGPARSE", "DIAGNOSE", "REVIEW"]);
  assert.equal(CLAUDE_DEEPSEEK_PUBLIC_TOOLS.length, 7);
});

test("Claude standalone owns the same nine scenarios with lifecycle-aware call counts", () => {
  assert.equal(CLAUDE_DEEPSEEK_SCENARIOS.length, 9);
  assert.deepEqual(claudeDeepseekE2EPhases("insufficient-evidence"), ["CLIENT", "ROUTE", "LOGPARSE", "DIAGNOSE"]);
  assert.equal(claudeDeepseekE2ECallCount("insufficient-evidence"), 4);
  for (const scenario of CLAUDE_DEEPSEEK_SCENARIOS.filter((item) => item !== "insufficient-evidence")) assert.equal(claudeDeepseekE2ECallCount(scenario), 5, scenario);
});

test("Claude mapper publishes both slots and stable process-name USER_FACT ids", () => {
  const casePath = path.resolve("experiments", "rpc-skill-feasibility", "cases", "api-execution-overrun", "case.json");
  const mapped = mapScenarioToCreateCase(loadScenarioFacts(casePath, "api-execution-overrun"));
  assert.deepEqual(mapped.initial_user_fact_names, ["problem_time", "client_slot", "client_process_name", "server_slot", "server_process_name", "service", "api"]);
  assert.deepEqual(mapped.initial_user_fact_values.slice(1, 5), ["1", "rpc_client", "1", "rpc_server"]);
  for (const scenario of CLAUDE_DEEPSEEK_SCENARIOS) {
    const facts = loadScenarioFacts(path.resolve("experiments", "rpc-skill-feasibility", "cases", scenario, "case.json"), scenario);
    assert.equal(facts.client_slot, "1", scenario);
    assert.equal(facts.server_slot, "1", scenario);
  }
});

test("registration producer identity includes settings and cache freezes the complete generated root", () => {
  const f = fixture();
  const producer = buildRegistrationProducerIdentity({ wiki: f.wiki, metaSkillRoot: f.meta, claudeIdentity: claudeIdentity(), module: "rpc" });
  assert.match(producer.producer_identity, /^[a-f0-9]{64}$/);
  assert.equal(producer.inputs.claude.settings_fingerprint, "c".repeat(64));
  assert.equal(producer.inputs.module, "rpc");
  assert.equal(producer.inputs.source_identity.log_template_extraction_version, 2);
  assert.match(producer.inputs.meta_skill.tree_sha256, /^[a-f0-9]{64}$/);
  assert.match(producer.inputs.validator.sha256, /^[a-f0-9]{64}$/);
  assert.match(producer.inputs.runner.sha256, /^[a-f0-9]{64}$/);
  assert.equal(Object.hasOwn(producer.inputs, "registration_template"), false);
  const cacheRoot = path.join(f.root, "cache");
  const destination = registrationCachePath(cacheRoot, producer.producer_identity);
  assert.equal(destination, path.join(cacheRoot, "claude-deepseek-registration", producer.producer_identity));
  const published = publishRegistrationCacheAtomically({ cacheRoot, producer, registrationRoot: f.registrationRoot, stagingRoot: path.join(f.root, "stage-one") });
  assert.equal(published.published, true);
  assert.deepEqual(fs.readdirSync(published.registration_root).sort(), ["package", "registration-template.json"]);
  assert.equal(validateRegistrationCache({ cacheRoot, producer }).status, "PASS");
  const identical = publishRegistrationCacheAtomically({ cacheRoot, producer, registrationRoot: f.registrationRoot, stagingRoot: path.join(f.root, "stage-two") });
  assert.equal(identical.published, false);
  fs.appendFileSync(path.join(destination, "registration", "rpc-timeout-methods-v1", "package", "diagnose-rpc-timeout", "SKILL.md"), "tamper\n");
  assert.throws(() => validateRegistrationCache({ cacheRoot, producer }), (error) => error.code === "CLAUDE_DEEPSEEK_REGISTRATION_CACHE_IDENTITY_MISMATCH");
});

test("registration receipt detects post-validation tampering", () => {
  const f = fixture();
  const producer = buildRegistrationProducerIdentity({ wiki: f.wiki, metaSkillRoot: f.meta, claudeIdentity: claudeIdentity(), module: "rpc" });
  const cacheRoot = path.join(f.root, "cache");
  publishRegistrationCacheAtomically({ cacheRoot, producer, registrationRoot: f.registrationRoot, stagingRoot: path.join(f.root, "stage") });
  const receipt = validateRegistrationCache({ cacheRoot, producer });
  assert.equal(assertRegistrationUnchanged(receipt).status, "PASS");
  fs.writeFileSync(path.join(receipt.package_root, "methods.json"), '{"changed":true}\n');
  assert.throws(() => assertRegistrationUnchanged(receipt), (error) => error.code === "CLAUDE_DEEPSEEK_REGISTRATION_DRIFT");
});

test("cache manifest is canonical and binds byte inventory plus atomic policy", () => {
  const f = fixture();
  const producer = buildRegistrationProducerIdentity({ wiki: f.wiki, metaSkillRoot: f.meta, claudeIdentity: claudeIdentity(), module: "rpc" });
  const manifest = buildRegistrationCacheManifest({ producer, registrationRoot: f.registrationRoot });
  assert.equal(manifest.publish.strategy, "staging-directory-atomic-rename");
  assert.equal(manifest.registration.files.filter((item) => item.kind === "file").length, 4);
  assert.equal(manifest.registration.runtime_ref.id, "diagnosis-skill/rpc-timeout-methods-v1");
  assert.equal(canonicalJson(manifest), canonicalJson(JSON.parse(canonicalJson(manifest))));
});

test("registration runtime ref uses the Server code-point order for package paths", () => {
  const f = fixture();
  const producer = buildRegistrationProducerIdentity({ wiki: f.wiki, metaSkillRoot: f.meta, claudeIdentity: claudeIdentity(), module: "rpc" });
  const manifest = buildRegistrationCacheManifest({ producer, registrationRoot: f.registrationRoot });
  const entries = ["SKILL.md", "methods.json", "references/method.md"].map((relative) => {
    const bytes = fs.readFileSync(path.join(f.packageRoot, ...relative.split("/")));
    return { path: relative, size: bytes.length, sha256: crypto.createHash("sha256").update(bytes).digest("hex") };
  });
  const packageTreeSha256 = crypto.createHash("sha256").update(canonicalJson({ version: 1, entries })).digest("hex");
  const registrationSha256 = crypto.createHash("sha256").update(fs.readFileSync(f.registration)).digest("hex");
  const contentHash = crypto.createHash("sha256").update(canonicalJson({
    schema_version: 1,
    registration_id: "rpc-timeout-methods-v1",
    registration_sha256: registrationSha256,
    package_tree_sha256: packageTreeSha256,
  })).digest("hex");
  assert.equal(manifest.registration.package_tree_sha256, packageTreeSha256);
  assert.equal(manifest.registration.runtime_ref.content_hash, contentHash);
});

test("stream audit requires one init/result, pinned model, bounded turns, and allowed tools", () => {
  const events = [
    { type: "system", subtype: "init", model: CLAUDE_DEEPSEEK_MODEL },
    { type: "assistant", message: { content: [{ type: "tool_use", id: "t1", name: "Read", input: { file_path: "inputs/wiki.md" } }] } },
    { type: "result", subtype: "success", is_error: false, num_turns: 2, usage: usage(), total_cost_usd: 0.01 },
  ];
  const receipt = auditClaudeStream(events, { phase: "REGISTRATION_GENERATION", allowedTools: ["Read", "Write", "Skill"], maxTurns: 16, wallTimeoutSeconds: 1800 });
  assert.equal(receipt.status, "PASS");
  assert.deepEqual(receipt.tools.map((item) => item.name), ["Read"]);
  const bad = structuredClone(events);
  bad[1].message.content[0].name = "Bash";
  assert.throws(() => auditClaudeStream(bad, { phase: "REGISTRATION_GENERATION", allowedTools: ["Read", "Write", "Skill"], maxTurns: 16, wallTimeoutSeconds: 1800 }), (error) => error.code === "CLAUDE_DEEPSEEK_STREAM_TOOL_FORBIDDEN");
  const denied = structuredClone(bad);
  denied.splice(2, 0, { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "t1", is_error: true, content: "No such tool available" }] } });
  assert.equal(auditClaudeStream(denied, { phase: "REGISTRATION_GENERATION", allowedTools: ["Read", "Write", "Skill"], maxTurns: 16, wallTimeoutSeconds: 1800 }).status, "PASS");
  assert.equal(CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS, 300);
});

test("usage aggregation is cache-inclusive and enforces lifecycle-aware no-retry processes", () => {
  const methods = auditClaudeInvocations(invocations(["REGISTRATION_GENERATION"], "methods"), { workflow: "generation" });
  assert.equal(methods.aggregate.total_tokens, 20);
  const e2e = auditClaudeInvocations(invocations(CLAUDE_DEEPSEEK_E2E_PHASES), { workflow: "e2e", scenarioId: "api-execution-overrun" });
  assert.equal(e2e.aggregate.total_tokens, 100);
  assert.equal(auditClaudeInvocations(invocations(["CLIENT", "ROUTE", "LOGPARSE", "DIAGNOSE"]), { workflow: "e2e", scenarioId: "insufficient-evidence" }).aggregate.total_tokens, 80);
  assert.deepEqual(aggregateClaudeUsage(invocations(["CLIENT", "ROUTE"])), {
    input_tokens: 20, output_tokens: 10, cache_creation_input_tokens: 6, cache_read_input_tokens: 4, total_tokens: 40, cost_usd: 0.02,
  });
  assert.throws(() => auditClaudeInvocations(invocations(["CLIENT", "ROUTE", "DIAGNOSE", "REVIEW"]), { workflow: "e2e", scenarioId: "api-execution-overrun" }), (error) => error.code === "CLAUDE_DEEPSEEK_INVOCATION_COUNT_INVALID");
  const retried = invocations(CLAUDE_DEEPSEEK_E2E_PHASES);
  retried[2].retry = 1;
  assert.throws(() => auditClaudeInvocations(retried, { workflow: "e2e", scenarioId: "api-execution-overrun" }), (error) => error.code === "CLAUDE_DEEPSEEK_INVOCATION_IDENTITY_INVALID");
  const calibrated = invocations(CLAUDE_DEEPSEEK_E2E_PHASES);
  [1.019079, 0.203663, 0.091923, 0.872387, 1.093734].forEach((cost, index) => { calibrated[index].usage.cost_usd = cost; });
  assert.equal(auditClaudeInvocations(calibrated, { workflow: "e2e", scenarioId: "api-execution-overrun" }).aggregate.cost_usd, 3.280786);
  calibrated[4].usage.cost_usd = 2;
  assert.throws(() => auditClaudeInvocations(calibrated, { workflow: "e2e", scenarioId: "api-execution-overrun" }), (error) => error.code === "CLAUDE_DEEPSEEK_BUDGET_EXCEEDED");
  const over = invocations(CLAUDE_DEEPSEEK_E2E_PHASES);
  over[0].usage.cache_read_input_tokens = 2_000_001;
  assert.throws(() => auditClaudeInvocations(over, { workflow: "e2e", scenarioId: "api-execution-overrun" }), (error) => error.code === "CLAUDE_DEEPSEEK_BUDGET_EXCEEDED");
});

test("Bash audit permits the exact upload sequence and descriptor-bound result download", () => {
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
  const resultPath = "/private/tmp/client/output/result.zip";
  const artifact = { size: 99, sha256: "b".repeat(64), download_url: "http://127.0.0.1:8123/api/v1/artifacts/00000000-0000-0000-0000-000000000010/content?case_id=00000000-0000-0000-0000-000000000001" };
  const downloaded = [
    ...commands,
    { command: `/usr/bin/curl --silent --show-error --fail-with-body --max-time 60 --request GET --output '${resultPath}' '${artifact.download_url}'`, status: "completed", exit_code: 0, stdout: "" },
    { command: `/usr/bin/stat -f %z '${resultPath}'`, status: "completed", exit_code: 0, stdout: "99\n" },
    { command: `/usr/bin/openssl dgst -sha256 '${resultPath}'`, status: "completed", exit_code: 0, stdout: `${artifact.sha256}\n` },
  ];
  assert.equal(auditClientBash(downloaded, { archivePath, archive, descriptor, download: { path: resultPath, artifact } }).download_count, 1);
});
