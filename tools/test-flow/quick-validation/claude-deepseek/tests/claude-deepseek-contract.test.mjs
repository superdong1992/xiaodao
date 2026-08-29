import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { canonicalJson } from "../../../lib/util.mjs";
import { ISOLATED_AGENT_ENV_POLICY_VERSION, environmentKeySummary } from "../../../runtime-support/isolated-agent-env.mjs";
import {
  CLAUDE_DEEPSEEK_CLI_SHA256,
  CLAUDE_DEEPSEEK_CLIENT_PROMPT_VERSION,
  CLAUDE_DEEPSEEK_CONTRACT_VERSION,
  CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS,
  CLAUDE_DEEPSEEK_MODEL,
  CLAUDE_DEEPSEEK_MODEL_CERT_BUDGET_ENFORCEMENT,
  CLAUDE_DEEPSEEK_MODEL_CERT_PHASES,
  CLAUDE_DEEPSEEK_MODEL_CERT_ROLE_POOL_USD,
  CLAUDE_DEEPSEEK_MODEL_CERT_SCENARIO,
  CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
  CLAUDE_DEEPSEEK_PUBLIC_TOOLS,
  CLAUDE_DEEPSEEK_SCENARIOS,
  CLAUDE_DEEPSEEK_VERSION,
  aggregateClaudeUsage,
  assertRegistrationUnchanged,
  auditClaudeInvocations,
  auditClaudeModelCertInvocations,
  auditClaudeStream,
  buildRegistrationCacheManifest,
  buildRegistrationProducerIdentity,
  claudeDeepseekE2ECallCount,
  claudeDeepseekE2EPhases,
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
  assert.equal(CLAUDE_DEEPSEEK_MODEL_CERT_ROLE_POOL_USD, 2);
  assert.equal(CLAUDE_DEEPSEEK_MODEL_CERT_BUDGET_ENFORCEMENT, "claude-cli-threshold+terminal-posthoc-release-cap");
  assert.deepEqual(CLAUDE_DEEPSEEK_MODEL_CERT_PHASES, ["SPECIALIST", "REVIEWER"]);
  assert.deepEqual(CLAUDE_DEEPSEEK_PUBLIC_TOOLS, []);
});

test("Claude model cert owns one fixed production Runtime scenario and two normal calls", () => {
  assert.equal(CLAUDE_DEEPSEEK_MODEL_CERT_SCENARIO, "multiple-rpc-timeouts");
  assert.deepEqual(CLAUDE_DEEPSEEK_MODEL_CERT_PHASES, ["SPECIALIST", "REVIEWER"]);
  assert.deepEqual(CLAUDE_DEEPSEEK_SCENARIOS, ["multiple-rpc-timeouts"]);
  assert.deepEqual(claudeDeepseekE2EPhases(CLAUDE_DEEPSEEK_MODEL_CERT_SCENARIO), CLAUDE_DEEPSEEK_MODEL_CERT_PHASES);
  assert.equal(claudeDeepseekE2ECallCount(CLAUDE_DEEPSEEK_MODEL_CERT_SCENARIO), 2);
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
  const e2e = auditClaudeInvocations(invocations(CLAUDE_DEEPSEEK_MODEL_CERT_PHASES), { workflow: "e2e", scenarioId: CLAUDE_DEEPSEEK_MODEL_CERT_SCENARIO });
  assert.equal(e2e.aggregate.total_tokens, 40);
  assert.deepEqual(aggregateClaudeUsage(invocations(CLAUDE_DEEPSEEK_MODEL_CERT_PHASES)), {
    schema_version: 1, input_tokens: 20, output_tokens: 10, cache_creation_input_tokens: 6, cache_read_input_tokens: 4, total_tokens: 40, cost_usd: 0.02,
  });
  assert.throws(() => auditClaudeInvocations(invocations(["SPECIALIST"]), { workflow: "e2e", scenarioId: CLAUDE_DEEPSEEK_MODEL_CERT_SCENARIO }), (error) => error.code === "CLAUDE_DEEPSEEK_INVOCATION_COUNT_INVALID");
  const retried = invocations(CLAUDE_DEEPSEEK_MODEL_CERT_PHASES);
  retried[1].retry = 1;
  assert.throws(() => auditClaudeInvocations(retried, { workflow: "e2e", scenarioId: CLAUDE_DEEPSEEK_MODEL_CERT_SCENARIO }), (error) => error.code === "CLAUDE_DEEPSEEK_INVOCATION_IDENTITY_INVALID");
  const calibrated = invocations(CLAUDE_DEEPSEEK_MODEL_CERT_PHASES);
  [1.25, 1.25].forEach((cost, index) => { calibrated[index].usage.cost_usd = cost; });
  assert.equal(auditClaudeInvocations(calibrated, { workflow: "e2e", scenarioId: CLAUDE_DEEPSEEK_MODEL_CERT_SCENARIO }).aggregate.cost_usd, 2.5);
  calibrated[1].usage.cost_usd = 3;
  assert.throws(() => auditClaudeInvocations(calibrated, { workflow: "e2e", scenarioId: CLAUDE_DEEPSEEK_MODEL_CERT_SCENARIO }), (error) => error.code === "CLAUDE_DEEPSEEK_BUDGET_EXCEEDED");
  const over = invocations(CLAUDE_DEEPSEEK_MODEL_CERT_PHASES);
  over[0].usage.cache_read_input_tokens = 2_000_001;
  assert.throws(() => auditClaudeInvocations(over, { workflow: "e2e", scenarioId: CLAUDE_DEEPSEEK_MODEL_CERT_SCENARIO }), (error) => error.code === "CLAUDE_DEEPSEEK_BUDGET_EXCEEDED");
});

test("Evidence V2 model cert allows only S primary/repair then blind R primary/repair", () => {
  const roleInvocation = (role, evaluationAttempt) => {
    const priorCostUsd = evaluationAttempt === "PRIMARY" ? 0 : 0.01;
    const effectiveCallCapUsd = 2 - priorCostUsd;
    const output = role === "SPECIALIST" ? "output/method-diagnosis.draft.json" : "output/method-review.draft.json";
    const policy = {
      schema_version: 1,
      tools: ["Read", "Write"],
      allowed_tools: ["Read(//workspace/inputs/**)", `Read(//workspace/${output})`, `Write(//workspace/${output})`],
      readable_scope: "job-workspace-inputs-and-role-draft",
      writable_scope: output,
      network: false,
      shell: false,
      skill_loading: false,
    };
    const rawUsage = usage();
    return {
      ...invocations([role])[0],
      schema_version: 1,
      invocation_id: `run:${role.toLowerCase()}:${evaluationAttempt.toLowerCase()}`,
      usage: { schema_version: 1, ...rawUsage, total_tokens: 20 },
      role,
      evaluation_attempt: evaluationAttempt,
      role_call_ordinal: evaluationAttempt === "PRIMARY" ? 1 : 2,
      max_budget_usd: effectiveCallCapUsd,
      max_turns: 50,
      max_output_tokens: 64_000,
      appended_system_prompt: null,
      workflow: `${role}:${evaluationAttempt}`,
      budget: {
        schema_version: 1,
        stage_cap_usd: 4,
        role,
        role_pool_usd: 2,
        prior_cost_usd: priorCostUsd,
        effective_call_cap_usd: effectiveCallCapUsd,
        enforcement: CLAUDE_DEEPSEEK_MODEL_CERT_BUDGET_ENFORCEMENT,
      },
      prompt: { sha256: "a".repeat(64), utf8_size: 10 },
      environment_policy: {
        schema_version: 1,
        version: ISOLATED_AGENT_ENV_POLICY_VERSION,
        provider_auth_source: "audited-settings-file",
        inbound: environmentKeySummary({ PATH: "/bin" }),
        claude_process: environmentKeySummary({ PATH: "/bin" }),
      },
      provider_terminal: { subtype: "success", is_error: false, stop_reason: null, exit_code: 0, signal: null },
      workspace_audit: {
        schema_version: 1, status: "PASS", role, attempt: evaluationAttempt, reads: 1, writes: 1,
        output_path: output, output_size: 10, output_sha256: "b".repeat(64), harness_normalized: false,
      },
      tool_policy: { ...policy, sha256: crypto.createHash("sha256").update(canonicalJson(policy)).digest("hex") },
      usage_complete: true,
      failure_code: null,
      disallowed_tools: ["Bash", "Glob", "Grep", "Skill"],
      tool_count: 2,
      denied_tool_attempt_count: 0,
      mcp_call_count: 0,
      bash_call_count: 0,
    };
  };
  const normal = [roleInvocation("SPECIALIST", "PRIMARY"), roleInvocation("REVIEWER", "PRIMARY")];
  assert.deepEqual(auditClaudeModelCertInvocations(normal).repair_counts, { specialist: 0, reviewer: 0 });
  const repaired = [roleInvocation("SPECIALIST", "PRIMARY"), roleInvocation("SPECIALIST", "REPAIR"), roleInvocation("REVIEWER", "PRIMARY"), roleInvocation("REVIEWER", "REPAIR")];
  assert.equal(auditClaudeModelCertInvocations(repaired).actual_call_count, 4);
  assert.equal(auditClaudeModelCertInvocations(repaired).aggregate.cost_usd, 0.04);
  const overCallCap = structuredClone(normal);
  overCallCap[0].usage.cost_usd = 2.000001;
  assert.throws(() => auditClaudeModelCertInvocations(overCallCap), (error) => error.code === "CLAUDE_DEEPSEEK_MODEL_CERT_BUDGET_RECEIPT_INVALID");
  const wrongRepairRemainder = structuredClone(repaired);
  wrongRepairRemainder[1].budget.prior_cost_usd = 0;
  wrongRepairRemainder[1].budget.effective_call_cap_usd = 2;
  wrongRepairRemainder[1].max_budget_usd = 2;
  assert.throws(() => auditClaudeModelCertInvocations(wrongRepairRemainder), (error) => error.code === "CLAUDE_DEEPSEEK_MODEL_CERT_BUDGET_RECEIPT_INVALID");
  assert.throws(() => auditClaudeModelCertInvocations([...normal, roleInvocation("SPECIALIST", "REPAIR")]), (error) => error.code === "CLAUDE_DEEPSEEK_MODEL_CERT_SEQUENCE_INVALID");
  assert.throws(() => auditClaudeModelCertInvocations([...repaired, roleInvocation("REVIEWER", "REPAIR")]), (error) => error.code === "CLAUDE_DEEPSEEK_MODEL_CERT_CALL_COUNT_INVALID");
});
