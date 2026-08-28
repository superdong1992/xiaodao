import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  auditDiagnosisCommands,
  auditGenerationCommands,
  auditNoSecretLeak,
  buildCodexLunaSourceLogTemplatesBytes,
  buildCodexLunaSourceWikiIdentity,
  buildPosthocBudgetReceipt,
  canonicalJson,
  CODEX_LUNA_EXPECTED_CLI_SHA256,
  CODEX_LUNA_EXPECTED_CLI_VERSION,
  CODEX_LUNA_MAX_CALLS,
  CODEX_LUNA_MODEL,
  CODEX_LUNA_REASONING_EFFORT,
  collectSecretCanaries,
  extractCodexLunaWikiLogTemplates,
  normalizeCodexUsage,
  sha256Bytes,
  treeDigest,
  validateCodexLunaSourceWikiIdentity,
  validateCodexLunaIdentity,
  verifyMethodsV1Package,
} from "../runtime-support/codex-luna-contract.mjs";
import {
  boundDiagnosisSchema,
  buildDiagnosisWorkspace,
  buildGenerationWorkspace,
  buildInvocationUsageReceipt,
  environmentAudit,
  generationPrompt,
  parseArguments,
  safeEnvironment,
  validateDiagnosis,
  validatePreprocessedCase,
} from "../runtime-support/codex-luna-exploration-runner.mjs";
import { buildCodexLunaTurnStartRequest } from "../runtime-support/codex-luna-app-server.mjs";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");

function temporaryRoot(prefix) {
  const parent = fs.existsSync("/private/tmp") ? "/private/tmp" : os.tmpdir();
  return fs.mkdtempSync(path.join(parent, prefix));
}

function writeJson(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value)}\n`);
}

function usage(overrides = {}) {
  return {
    input_tokens: 100,
    cached_input_tokens: 40,
    cache_write_input_tokens: 0,
    output_tokens: 20,
    reasoning_output_tokens: 5,
    ...overrides,
  };
}

test("Codex Luna identity and proof dimensions are frozen", () => {
  assert.equal(CODEX_LUNA_EXPECTED_CLI_VERSION, "codex-cli 0.149.0-alpha.4.1");
  assert.equal(CODEX_LUNA_EXPECTED_CLI_SHA256, "09db9560f6f9dec139d3324254fb3c8fdbad5ecce1d8c794113dc15294f6aefd");
  assert.equal(CODEX_LUNA_MODEL, "gpt-5.6-luna");
  assert.equal(CODEX_LUNA_REASONING_EFFORT, "medium");
  assert.equal(CODEX_LUNA_MAX_CALLS, 10);
});

test("source Wiki identity v2 inventories trimmed text-fence templates in order with duplicates", () => {
  const wiki = Buffer.from([
    "outside {ignored}",
    "",
    "  ```text  ",
    "  API_COMPLETE service={service}  ",
    "plain text",
    "  QUEUE_HISTORY ordinal=%x",
    "API_COMPLETE service={service}",
    "```",
    "",
    "```json",
    "NOT_A_TEMPLATE value={value}",
    "```",
    "",
  ].join("\r\n"), "utf8");
  const templates = [
    "API_COMPLETE service={service}",
    "QUEUE_HISTORY ordinal=%x",
    "API_COMPLETE service={service}",
  ];
  assert.deepEqual(extractCodexLunaWikiLogTemplates(wiki.toString("utf8")), templates);
  const identity = buildCodexLunaSourceWikiIdentity(wiki);
  assert.deepEqual(Object.keys(identity).sort(), [
    "algorithm",
    "schema_version",
    "sha256",
    "source_path",
    "log_template_extraction_version",
    "log_templates",
    "log_template_inventory_sha256",
  ].sort());
  assert.equal(identity.schema_version, 2);
  assert.equal(identity.source_path, "input/wiki.md");
  assert.equal(identity.log_template_extraction_version, 1);
  assert.deepEqual(identity.log_templates, templates);
  assert.equal(identity.log_template_inventory_sha256, sha256Bytes(canonicalJson({ version: 1, templates })));
  assert.equal(
    buildCodexLunaSourceLogTemplatesBytes(templates).toString("utf8"),
    `# Source log templates\n\n\`\`\`text\n${templates.join("\n")}\n\`\`\`\n`,
  );
  assert.deepEqual(validateCodexLunaSourceWikiIdentity(structuredClone(identity), wiki), identity);
  assert.throws(
    () => validateCodexLunaSourceWikiIdentity({ ...identity, extra: true }, wiki),
    (error) => error.code === "CODEX_LUNA_SOURCE_WIKI_IDENTITY_INVALID",
  );
  assert.throws(
    () => validateCodexLunaSourceWikiIdentity({ ...identity, schema_version: 1 }, wiki),
    (error) => error.code === "CODEX_LUNA_SOURCE_WIKI_IDENTITY_INVALID",
  );
  const reordered = structuredClone(identity);
  [reordered.log_templates[0], reordered.log_templates[1]] = [reordered.log_templates[1], reordered.log_templates[0]];
  reordered.log_template_inventory_sha256 = sha256Bytes(canonicalJson({ version: 1, templates: reordered.log_templates }));
  assert.throws(
    () => validateCodexLunaSourceWikiIdentity(reordered, wiki),
    (error) => error.code === "CODEX_LUNA_SOURCE_WIKI_IDENTITY_INVALID",
  );
});

test("generation workspace writes canonical source identity v2 and prompt fixes the template reference", (t) => {
  const root = temporaryRoot("codex-luna-generation-workspace-");
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const metaSkillRoot = path.join(root, "meta-skill");
  const wiki = path.join(root, "wiki.md");
  const workspace = path.join(root, "workspace");
  fs.mkdirSync(metaSkillRoot);
  fs.writeFileSync(path.join(metaSkillRoot, "SKILL.md"), "# Meta\n");
  fs.writeFileSync(wiki, "```text\nMARKER id={id}\n```\n");
  const prepared = buildGenerationWorkspace({ attemptRoot: workspace, metaSkillRoot, wiki });
  assert.deepEqual(JSON.parse(fs.readFileSync(prepared.sourceWikiIdentityPath, "utf8")), prepared.sourceWikiIdentity);
  assert.equal(fs.readFileSync(prepared.sourceWikiIdentityPath, "utf8"), `${canonicalJson(prepared.sourceWikiIdentity)}\n`);
  assert.deepEqual(prepared.sourceWikiIdentity.log_templates, ["MARKER id={id}"]);
  assert.equal(fs.readFileSync(path.join(workspace, "input", "wiki.md"), "utf8"), fs.readFileSync(wiki, "utf8"));
  const prompt = generationPrompt();
  assert.match(prompt, /先完整读取 input\/wiki\.md 和 runtime\/source-wiki-identity\.json/);
  assert.match(prompt, /references\/source-log-templates\.md/);
  assert.match(prompt, /shared_references\[0\]/);
  const actionsSource = fs.readFileSync(path.join(REPO_ROOT, "tools", "test-flow", "lib", "actions.mjs"), "utf8");
  const mirrorStart = actionsSource.indexOf("function codexLunaGenerationPrompt()");
  const mirrorBodyStart = actionsSource.indexOf("return `", mirrorStart) + "return `".length;
  const mirrorBodyEnd = actionsSource.indexOf("`;", mirrorBodyStart);
  assert.ok(mirrorStart >= 0 && mirrorBodyEnd > mirrorBodyStart);
  assert.equal(actionsSource.slice(mirrorBodyStart, mirrorBodyEnd), prompt);
});

test("diagnosis workspace passes the parsed output schema to app-server", (t) => {
  const root = temporaryRoot("codex-luna-diagnosis-workspace-");
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const generatedSkill = path.join(root, "generated-skill");
  const frozen = path.join(root, "frozen");
  const attemptRoot = path.join(root, "attempt");
  fs.mkdirSync(generatedSkill, { recursive: true });
  fs.mkdirSync(frozen, { recursive: true });
  fs.writeFileSync(path.join(generatedSkill, "SKILL.md"), "# Test Skill\n");
  const receiptPath = path.join(frozen, "receipt.json");
  writeJson(receiptPath, { archive: { name: "logs.zip", sha256: "a".repeat(64) } });
  const sources = ["client", "server"].map((label) => {
    const sourcePath = path.join(frozen, `${label}.log`);
    fs.writeFileSync(sourcePath, `${label} line\n`);
    return { label, source_path: sourcePath, process_name: label, match_status: "MATCHED", sha256: sha256Bytes(`${label} line\n`) };
  });
  const prepared = buildDiagnosisWorkspace({
    attemptRoot,
    generatedSkill,
    caseItem: { data: { scenario_id: "schema-wire-shape", problem_time: "2026-01-01T00:00:00Z", client_process: "client", server_process: "server", service: "svc", api: "Call" } },
    preprocessing: { receipt: { archive: { name: "logs.zip", sha256: "a".repeat(64) } }, receipt_path: receiptPath, receipt_sha256: "b".repeat(64), sources },
    schemaPath: path.join(REPO_ROOT, "tools", "test-flow", "runtime-support", "codex-luna-diagnosis.schema.json"),
  });
  assert.equal(typeof prepared.outputSchema, "object");
  assert.equal(Array.isArray(prepared.outputSchema), false);
  assert.deepEqual(JSON.parse(fs.readFileSync(prepared.outputSchemaPath, "utf8")), prepared.outputSchema);
  const request = buildCodexLunaTurnStartRequest({
    threadId: "thread-1",
    prompt: "Diagnose.",
    workspaceRoot: prepared.workspace,
    skillPath: path.join(prepared.installedSkill, "SKILL.md"),
    mode: "diagnosis",
    outputSchema: prepared.outputSchema,
  });
  assert.deepEqual(request.params.outputSchema, prepared.outputSchema);
});

test("the raw exploration runners are retired in favor of the Test Flow Gate", () => {
  const experimentRoot = path.join(REPO_ROOT, "experiments", "rpc-skill-feasibility");
  assert.equal(fs.existsSync(path.join(experimentRoot, "run.py")), false);
  assert.equal(fs.existsSync(path.join(experimentRoot, "check_evidence_contract.py")), false);
  assert.equal(fs.existsSync(path.join(REPO_ROOT, "tools", "test-flow", "runtime-support", "codex-luna-exploration-runner.mjs")), true);
  assert.equal(fs.existsSync(path.join(REPO_ROOT, "tools", "test-flow", "runtime-support", "codex-luna-prepare.py")), true);
});

test("validateCodexLunaIdentity returns only a safe receipt for the current local identity", {
  skip: !fs.existsSync("/Applications/ChatGPT.app/Contents/Resources/codex")
    || !fs.existsSync(path.join(os.homedir(), ".codex", "auth.json")),
}, () => {
  const receipt = validateCodexLunaIdentity(
    "/Applications/ChatGPT.app/Contents/Resources/codex",
    path.join(os.homedir(), ".codex", "auth.json"),
  );
  assert.equal(receipt.status, "PASS");
  assert.equal(receipt.cli.version, CODEX_LUNA_EXPECTED_CLI_VERSION);
  assert.equal(receipt.cli.sha256, CODEX_LUNA_EXPECTED_CLI_SHA256);
  assert.match(receipt.cli.code_mode_host.sha256, /^[a-f0-9]{64}$/);
  assert.ok(receipt.cli.code_mode_host.size > 0);
  assert.equal(receipt.auth.auth_mode, "chatgpt");
  assert.equal(receipt.auth.kind, "chatgpt-external-tokens");
  assert.match(receipt.auth.account_id_sha256, /^[a-f0-9]{64}$/);
  const rendered = JSON.stringify(receipt);
  assert.doesNotMatch(rendered, /access_token|refresh_token|id_token/i);
});

test("Codex usage does not double-count cached or reasoning tokens and is conservatively priced", () => {
  const normalized = normalizeCodexUsage(usage());
  assert.equal(normalized.total_tokens, 120);
  assert.equal(normalized.equivalent_usd_upper_bound, 0.000076);
  assert.throws(
    () => normalizeCodexUsage(usage({ cached_input_tokens: 101 })),
    (error) => error.code === "CODEX_LUNA_USAGE_CACHE_INVALID",
  );
  assert.throws(
    () => normalizeCodexUsage(usage({ reasoning_output_tokens: 21 })),
    (error) => error.code === "CODEX_LUNA_USAGE_REASONING_INVALID",
  );
});

test("post-hoc receipt requires exactly ten complete calls", () => {
  const calls = Array.from({ length: 10 }, (_, index) => ({ logical_id: String(index), usage: normalizeCodexUsage(usage()) }));
  const receipt = buildPosthocBudgetReceipt({ calls, usageComplete: true });
  assert.equal(receipt.status, "PASS_WITH_WARNINGS");
  assert.deepEqual(receipt.checks, {
    call_count_valid: true,
    usage_complete: true,
    within_token_limit: true,
    within_equivalent_usd_limit: true,
  });
  assert.equal(buildPosthocBudgetReceipt({ calls: calls.slice(0, 9), usageComplete: true }).status, "FAIL");
  assert.equal(buildPosthocBudgetReceipt({ calls: calls.map((call, index) => index === 0 ? { ...call, usage: null } : call), usageComplete: false }).status, "FAIL");
});

test("per-invocation receipt binds model, effort, turns, caps, terminal, outcome, and post-hoc enforcement", () => {
  const normalized = normalizeCodexUsage(usage());
  const receipt = buildInvocationUsageReceipt({
    invocationId: "run:codex-luna:01",
    phase: "methods-generation",
    logicalId: "generate",
    trace: { thread_id: "thread-1", turn_id: "turn-1", turn_count: 1, usage: normalized },
    passed: true,
    failureCode: null,
    processReceipt: { exit_code: 0, signal: null, spawn_error: null, timed_out: false, no_progress_timed_out: false },
  });
  assert.equal(receipt.schema_version, 1);
  assert.equal(receipt.class, "codex-luna-agent");
  assert.equal(receipt.workflow, "methods-generation");
  assert.equal(receipt.effective_model, CODEX_LUNA_MODEL);
  assert.equal(receipt.effective_reasoning_effort, CODEX_LUNA_REASONING_EFFORT);
  assert.equal(receipt.effective_caps.max_calls, 10);
  assert.equal(receipt.effective_caps.max_total_tokens_posthoc, 5_000_000);
  assert.equal(receipt.effective_caps.max_equivalent_usd_posthoc, 10);
  assert.equal(receipt.turns, 1);
  assert.deepEqual(receipt.terminal, { event: "turn.completed", thread_id: "thread-1", turn_id: "turn-1" });
  assert.deepEqual(receipt.wrapper_outcome, { schema_version: 1, status: "PASS", code: null });
  assert.equal(receipt.posthoc_enforcement.calls, "runner-precondition-exactly-ten-no-retry");
  assert.equal(receipt.posthoc_enforcement.total_tokens, "terminal-usage-postcondition-only");
});

test("generation and diagnosis command audits reject legacy, Logparse, raw, and oracle access", () => {
  const root = temporaryRoot("codex-luna-scope-");
  try {
    assert.equal(auditGenerationCommands(["python3 .agents/skills/wiki-to-diagnosis-skill/scripts/validate_generated_skill.py --skill-dir generated/x --wiki input/wiki.md --json"], { workspaceRoot: root }).status, "PASS");
    assert.equal(auditGenerationCommands(["sed -n '1,200p' input/wiki.md runtime/source-wiki-identity.json"], { workspaceRoot: root }).status, "PASS");
    assert.throws(
      () => auditGenerationCommands(["cat output/generation-spec.json"], { workspaceRoot: root }),
      (error) => error.code === "CODEX_LUNA_GENERATION_SCOPE_VIOLATION",
    );
    assert.equal(auditDiagnosisCommands(["sed -n '1,20p' input/request.json"], { workspaceRoot: root }).logparse_invocations, 0);
    assert.throws(
      () => auditDiagnosisCommands(["python cli.py mech-target-logs task"], { workspaceRoot: root }),
      (error) => error.code === "CODEX_LUNA_DIAGNOSIS_SCOPE_VIOLATION",
    );
    assert.throws(
      () => auditDiagnosisCommands(["cat ../case.json"], { workspaceRoot: root }),
      (error) => error.code === "CODEX_LUNA_DIAGNOSIS_SCOPE_VIOLATION",
    );
    assert.throws(
      () => auditDiagnosisCommands(["find ../preprocessing -type f"], { workspaceRoot: root }),
      (error) => error.code === "CODEX_LUNA_DIAGNOSIS_SCOPE_VIOLATION",
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("methods-v1 package verifier rejects v6 and seals the exact tree", () => {
  const root = temporaryRoot("codex-luna-methods-");
  try {
    const skill = path.join(root, "diagnose-example");
    fs.mkdirSync(path.join(skill, "references"), { recursive: true });
    fs.writeFileSync(path.join(skill, "SKILL.md"), "---\nname: diagnose-example\ndescription: test\n---\n");
    fs.writeFileSync(path.join(skill, "references", "cause.md"), "# Cause\n");
    const wiki = "```text\nMARKER id={id}\n```\n";
    const sourceWikiIdentity = buildCodexLunaSourceWikiIdentity(wiki);
    fs.writeFileSync(path.join(skill, "references", "source-log-templates.md"), buildCodexLunaSourceLogTemplatesBytes(sourceWikiIdentity.log_templates));
    writeJson(path.join(skill, "methods.json"), {
      schema_version: 1,
      skill_name: "diagnose-example",
      source_wiki_sha256: sourceWikiIdentity.sha256,
      required_user_inputs: [],
      required_artifacts: ["log_archive"],
      log_derived_fields: [],
      shared_references: ["references/source-log-templates.md"],
      methods: [{ id: "cause", title: "Cause", reference: "references/cause.md", priority: 1, evidence_markers: ["MARKER"] }],
    });
    const receipt = verifyMethodsV1Package(skill, sourceWikiIdentity);
    assert.equal(receipt.tree_sha256, treeDigest(skill));
    fs.writeFileSync(path.join(skill, "references", "source-log-templates.md"), "# Source log templates\n\n```text\nMARKER id={other}\n```\n");
    assert.throws(
      () => verifyMethodsV1Package(skill, sourceWikiIdentity),
      (error) => error.code === "CODEX_LUNA_TEMPLATE_REFERENCE_INVALID",
    );
    fs.writeFileSync(path.join(skill, "references", "source-log-templates.md"), buildCodexLunaSourceLogTemplatesBytes(sourceWikiIdentity.log_templates));
    const methodsPath = path.join(skill, "methods.json");
    const methods = JSON.parse(fs.readFileSync(methodsPath, "utf8"));
    methods.methods[0].reference = "references/source-log-templates.md";
    writeJson(methodsPath, methods);
    assert.throws(
      () => verifyMethodsV1Package(skill, sourceWikiIdentity),
      (error) => error.code === "CODEX_LUNA_TEMPLATE_REFERENCE_INVALID",
    );
    methods.methods[0].reference = "references/cause.md";
    writeJson(methodsPath, methods);
    writeJson(path.join(skill, "diagnosis-skill.json"), { manifest_version: 6 });
    assert.throws(
      () => verifyMethodsV1Package(skill, sourceWikiIdentity),
      (error) => error.code === "CODEX_LUNA_SKILL_FILESET_INVALID",
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("auth and ambient secrets cannot enter evidence", () => {
  const root = temporaryRoot("codex-luna-secret-");
  try {
    const auth = path.join(root, "auth.json");
    const evidence = path.join(root, "evidence");
    fs.mkdirSync(evidence);
    writeJson(auth, { auth_mode: "chatgpt", tokens: { access_token: "secret-access-value-12345" } });
    const canaries = collectSecretCanaries(auth, { SOME_TOKEN: "ambient-secret-value-98765" });
    fs.writeFileSync(path.join(evidence, "safe.json"), "{\"status\":\"PASS\"}\n");
    assert.equal(auditNoSecretLeak({ roots: [evidence], canaries }).status, "PASS");
    fs.writeFileSync(path.join(evidence, "leaked.txt"), "secret-access-value-12345\n");
    assert.throws(
      () => auditNoSecretLeak({ roots: [evidence], canaries }),
      (error) => error.code === "CODEX_LUNA_SECRET_LEAK",
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("runner parses the complete CLI argv without skipping name/value pairs", () => {
  const argv = [
    "--codex-entry", "/Applications/ChatGPT.app/Contents/Resources/codex",
    "--auth-source", "/private/auth.json",
    "--meta-skill-root", "/workspace/.agents/skills/wiki-to-diagnosis-skill",
    "--wiki", "/workspace/input/wiki.md",
    "--case-root", "/workspace/cases",
    "--preprocessed-root", "/scratch/preprocessed",
    "--validator-python", "/logparse/.venv/bin/python",
    "--validator-runtime-root", "/logparse",
    "--validator-runtime-identity", "/private/validator-runtime.json",
    "--work-root", "/scratch/work",
    "--private-root", "/scratch/private",
    "--evidence-root", "/evidence/gate",
    "--usage-root", "/evidence/usage",
    "--run-id", "release-123",
    "--allow-posthoc-budget",
  ];
  const parsed = parseArguments(argv);
  assert.equal(parsed["codex-entry"], argv[1]);
  assert.equal(parsed["auth-source"], argv[3]);
  assert.equal(parsed["meta-skill-root"], argv[5]);
  assert.equal(parsed.wiki, argv[7]);
  assert.equal(parsed["case-root"], argv[9]);
  assert.equal(parsed["preprocessed-root"], argv[11]);
  assert.equal(parsed["validator-python"], argv[13]);
  assert.equal(parsed["validator-runtime-root"], argv[15]);
  assert.equal(parsed["validator-runtime-identity"], argv[17]);
  assert.equal(parsed["work-root"], argv[19]);
  assert.equal(parsed["private-root"], argv[21]);
  assert.equal(parsed["evidence-root"], argv[23]);
  assert.equal(parsed["usage-root"], argv[25]);
  assert.equal(parsed["run-id"], "release-123");
  assert.equal(parsed["allow-posthoc-budget"], true);
});

test("legacy exec JSONL and outer Darwin Seatbelt contracts are breaking-retired", () => {
  const contractSource = fs.readFileSync(path.resolve("tools/test-flow/runtime-support/codex-luna-contract.mjs"), "utf8");
  const runnerSource = fs.readFileSync(path.resolve("tools/test-flow/runtime-support/codex-luna-exploration-runner.mjs"), "utf8");
  assert.doesNotMatch(contractSource, /parseCodexJsonl|classifyInfrastructureRetry/);
  assert.doesNotMatch(runnerSource, /codexArguments|codexDarwinSandboxProfile|sandbox-exec/);
  assert.match(runnerSource, /runCodexLunaAppServerCall/);
});

test("runner emits only semantic Test Flow heartbeats while raw Codex JSONL stays in evidence", () => {
  const source = fs.readFileSync(path.resolve("tools/test-flow/runtime-support/codex-luna-exploration-runner.mjs"), "utf8");
  assert.match(source, /TEST_FLOW_PROGRESS stage\.progress codex-luna/);
  assert.match(source, /tracePath,/);
  assert.doesNotMatch(source, /process\.stdout\.write\(chunk\)/);
});

test("isolated child environment strips credentials and redirects HOME and CODEX_HOME", () => {
  const child = safeEnvironment(
    { PATH: "/bin", LANG: "C", OPENAI_API_KEY: "secret", PROBLEM_TOKEN: "secret", HTTP_PROXY: "http://secret" },
    { home: "/private/home", codexHome: "/private/codex-home" },
  );
  assert.deepEqual(child, {
    PATH: "/usr/bin:/bin:/usr/sbin:/sbin",
    LANG: "C.UTF-8",
    HOME: "/private/home",
    CODEX_HOME: "/private/codex-home",
    TMPDIR: path.join("/private/home", "tmp"),
    TMP: path.join("/private/home", "tmp"),
    TEMP: path.join("/private/home", "tmp"),
    NO_COLOR: "1",
    PYTHONDONTWRITEBYTECODE: "1",
    PYTHONNOUSERSITE: "1",
    PYTHONUTF8: "1",
  });
  const receipt = environmentAudit({ OPENAI_API_KEY: "secret", PROBLEM_TOKEN: "secret" }, child);
  assert.deepEqual(receipt.stripped_sensitive_key_names, ["OPENAI_API_KEY", "PROBLEM_TOKEN"]);
  assert.equal(receipt.sensitive_values_forwarded, 0);
});

test("diagnosis schema binds scenario and receipt identities", () => {
  const root = temporaryRoot("codex-luna-schema-");
  try {
    const schemaPath = path.join(root, "schema.json");
    writeJson(schemaPath, {
      type: "object",
      properties: {
        scenario_id: { type: "string", minLength: 1 },
        logparse_receipt_sha256: { type: "string", pattern: "^[0-9a-f]{64}$" },
      },
    });
    const receipt = "a".repeat(64);
    const bound = boundDiagnosisSchema(schemaPath, { scenarioId: "case-one", receiptSha256: receipt });
    assert.deepEqual(bound.properties.scenario_id, { type: "string", const: "case-one" });
    assert.deepEqual(bound.properties.logparse_receipt_sha256, { type: "string", const: receipt });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("preprocessed receipt and source-line diagnosis evidence are mechanically grounded", () => {
  const root = temporaryRoot("codex-luna-grounding-");
  try {
    const scenarioId = "case-one";
    const preprocessingRoot = path.join(root, "preprocessed");
    const caseRoot = path.join(preprocessingRoot, scenarioId);
    fs.mkdirSync(path.join(caseRoot, "frozen"), { recursive: true });
    const firstLine = "API_COMPLETE request_id=req-1 cost_us=1200";
    const secondLine = "API_COMPLETE request_id=req-2 cost_us=1400";
    fs.writeFileSync(path.join(caseRoot, "frozen", "client.log"), `${firstLine}\n${secondLine}\n`);
    fs.writeFileSync(path.join(caseRoot, "frozen", "server.log"), "server noise\n");
    fs.writeFileSync(path.join(caseRoot, "case.zip"), "archive-bytes");
    const hash = (name) => sha256Bytes(fs.readFileSync(path.join(caseRoot, "frozen", name)));
    writeJson(path.join(caseRoot, "receipt.json"), {
      schema_version: 1,
      status: "PASS",
      scenario_id: scenarioId,
      parse_invocations: 1,
      target_query_invocations: 2,
      logparse_processes_during_diagnosis: 0,
      archive: { name: "case.zip", sha256: sha256Bytes("archive-bytes") },
      frozen_target_logs: [
        { label: "client", file: "frozen/client.log", sha256: hash("client.log"), process_name: "client", match_status: "exact" },
        { label: "server", file: "frozen/server.log", sha256: hash("server.log"), process_name: "server", match_status: "exact" },
      ],
    });
    const caseItem = {
      data: {
        scenario_id: scenarioId,
        problem_time: "2026-01-01T00:00:00Z",
        client_process: "client",
        server_process: "server",
        service: "svc",
        api: "api",
        expected_status: "CONFIRMED",
        expected_branch_markers: ["API_COMPLETE"],
        expected_terms: ["1200", "1400"],
        expected_evidence_identities: [
          { branch_marker: "API_COMPLETE", identity_tokens: ["request_id=req-1"] },
          { branch_marker: "API_COMPLETE", identity_tokens: ["request_id=req-2"] },
        ],
        forbidden_evidence_terms: ["unrelated"],
      },
    };
    const preprocessing = validatePreprocessedCase(caseItem, preprocessingRoot);
    const workspace = path.join(root, "workspace");
    fs.mkdirSync(path.join(workspace, "evidence"), { recursive: true });
    fs.copyFileSync(path.join(caseRoot, "frozen", "client.log"), path.join(workspace, "evidence", "client.log"));
    fs.copyFileSync(path.join(caseRoot, "frozen", "server.log"), path.join(workspace, "evidence", "server.log"));
    const manifest = { methods: [{ id: "api-slow", evidence_markers: ["API_COMPLETE"] }] };
    const result = {
      schema_version: 2,
      scenario_id: scenarioId,
      status: "CONFIRMED",
      confirmed_methods: ["api-slow"],
      candidate_methods: [],
      evidence: [
        {
          method_id: "api-slow",
          summary: "cost_us=1200",
          identity_tokens: ["request_id=req-1"],
          sources: [{ source_id: "client", line_number: 1, marker: "API_COMPLETE", line: firstLine }],
        },
        {
          method_id: "api-slow",
          summary: "cost_us=1400",
          identity_tokens: ["request_id=req-2"],
          sources: [{ source_id: "client", line_number: 2, marker: "API_COMPLETE", line: secondLine }],
        },
      ],
      limitations: [],
      safety_notes: ["超时不等于取消"],
      logparse_receipt_sha256: preprocessing.receipt_sha256,
    };
    const validated = validateDiagnosis({ result, caseItem, preprocessing, manifest, branchMapping: { API_COMPLETE: "api-slow" }, workspace });
    assert.equal(validated.evidence_count, 2);

    const merged = structuredClone(result);
    merged.evidence = [{
      method_id: "api-slow",
      summary: "merged two independent calls",
      identity_tokens: ["request_id=req-1", "request_id=req-2"],
      sources: [
        { source_id: "client", line_number: 1, marker: "API_COMPLETE", line: firstLine },
        { source_id: "client", line_number: 2, marker: "API_COMPLETE", line: secondLine },
      ],
    }];
    assert.throws(
      () => validateDiagnosis({ result: merged, caseItem, preprocessing, manifest, branchMapping: { API_COMPLETE: "api-slow" }, workspace }),
      (error) => error.code === "CODEX_LUNA_IDENTITY_ORACLE_MERGED",
    );

    const wrongStatus = structuredClone(result);
    wrongStatus.status = "PARTIAL";
    assert.throws(
      () => validateDiagnosis({ result: wrongStatus, caseItem, preprocessing, manifest, branchMapping: { API_COMPLETE: "api-slow" }, workspace }),
      (error) => error.code === "CODEX_LUNA_RESULT_STATUS_INVALID",
    );

    result.evidence[0].sources[0].line_number = 3;
    assert.throws(
      () => validateDiagnosis({ result, caseItem, preprocessing, manifest, branchMapping: { API_COMPLETE: "api-slow" }, workspace }),
      (error) => error.code === "CODEX_LUNA_EVIDENCE_SOURCE_UNGROUNDED",
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

const posixRuntimeTest = process.platform === "win32" ? test.skip : test;

posixRuntimeTest("Codex Luna preprocessing CLI produces nine one-parse/two-query receipts", () => {
  const root = temporaryRoot("codex-luna-prepare-");
  try {
    const caseRoot = path.join(root, "fixture", "cases");
    const logparseRoot = path.join(root, "logparse");
    const outputRoot = path.join(root, "output");
    fs.mkdirSync(caseRoot, { recursive: true });
    fs.mkdirSync(path.join(logparseRoot, ".venv", "bin"), { recursive: true });
    fs.writeFileSync(path.join(logparseRoot, ".venv", "bin", "python"), `#!/bin/sh\nexec \"${process.env.PYTHON ?? "/usr/bin/python3"}\" \"$@\"\n`, { mode: 0o755 });
    const fakeCli = `#!/usr/bin/env python3
import argparse, json, pathlib, sys, zipfile
args = sys.argv[1:]
if args[0] == "parse":
    archive = pathlib.Path(args[1]); output = pathlib.Path(args[args.index("-o") + 1]); task = output / "task-1"; task.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        for name in ("client.log", "server.log"):
            (task / name).write_bytes(bundle.read(name))
    (task / "result.json").write_text("{}\\n")
elif args[0] == "mech-target-logs":
    output = pathlib.Path(args[args.index("-o") + 1]); label = args[args.index("--label") + 1]
    print(json.dumps({"schema_version": 1, "target_logs": [{"match_status": "exact", "log_path": str((output / "task-1" / f"{label}.log").resolve()), "error_code": "LP_TARGET_OK"}]}))
else:
    raise SystemExit(2)
`;
    fs.writeFileSync(path.join(logparseRoot, "cli.py"), fakeCli);
    writeJson(path.join(root, "fixture", "logparse-config.json"), { schema_version: 2, products: { "rpc-skill-feasibility": {} } });
    for (let index = 1; index <= 9; index += 1) {
      const scenarioId = `case-${index}`;
      const scenario = path.join(caseRoot, scenarioId);
      fs.mkdirSync(path.join(scenario, "raw"), { recursive: true });
      writeJson(path.join(scenario, "case.json"), {
        scenario_id: scenarioId,
        problem_time: "2026-01-01T00:00:00Z",
        client_process: "client",
        server_process: "server",
        service: "svc",
        api: "api",
      });
      fs.writeFileSync(path.join(scenario, "raw", "client.log"), `client ${index}\n`);
      fs.writeFileSync(path.join(scenario, "raw", "server.log"), `server ${index}\n`);
    }
    for (const command of [
      ["init", "-q"],
      ["add", "-A"],
      ["-c", "user.name=Codex Test", "-c", "user.email=codex@example.invalid", "commit", "-q", "-m", "fixture"],
    ]) {
      const completed = spawnSync("git", command, { cwd: logparseRoot, encoding: "utf8" });
      assert.equal(completed.status, 0, completed.stderr);
    }
    const script = path.resolve("tools/test-flow/runtime-support/codex-luna-prepare.py");
    const completed = spawnSync(process.env.PYTHON ?? "/usr/bin/python3", ["-B", script, "--case-root", caseRoot, "--logparse-root", logparseRoot, "--output-root", outputRoot], {
      cwd: path.resolve("."),
      encoding: "utf8",
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONPYCACHEPREFIX: path.join(root, "pycache") },
      timeout: 120_000,
    });
    assert.equal(completed.status, 0, completed.stderr);
    const aggregate = JSON.parse(fs.readFileSync(path.join(outputRoot, "codex-luna-preprocessing.json"), "utf8"));
    assert.equal(aggregate.status, "PASS");
    assert.equal(aggregate.case_count, 9);
    assert.deepEqual(aggregate.totals, { parse_invocations: 9, target_query_invocations: 18, diagnosis_invocations: 0 });
    for (let index = 1; index <= 9; index += 1) {
      const receipt = JSON.parse(fs.readFileSync(path.join(outputRoot, "preprocessed", `case-${index}`, "receipt.json"), "utf8"));
      assert.equal(receipt.parse_invocations, 1);
      assert.equal(receipt.target_query_invocations, 2);
      assert.equal(receipt.frozen_target_logs.length, 2);
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
