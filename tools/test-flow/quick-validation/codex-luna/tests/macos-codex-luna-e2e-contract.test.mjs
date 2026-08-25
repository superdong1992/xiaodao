import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import {
  aggregateCodexUsage,
  assertMethodsPackageUnchanged,
  auditFlatMcpInputSchema,
  auditHttpBoundary,
  auditListedMcpTools,
  auditMcpToolCalls,
  auditModelInvocations,
  auditOracle,
  auditUploadedAttachment,
  buildDeterministicLogsZip,
  buildMethodsCacheManifest,
  buildMethodsProducerIdentity,
  loadScenarioFacts,
  loadScenarioOracle,
  MACOS_CODEX_LUNA_PUBLIC_TOOLS,
  MACOS_CODEX_LUNA_CALL_WALL_SECONDS,
  MACOS_CODEX_LUNA_NO_PROGRESS_SECONDS,
  mapScenarioToCreateCase,
  methodsCachePath,
  validateMethodsCache,
  writeDeterministicLogsZip,
} from "../runtime/macos-codex-luna-e2e-contract.mjs";
import { canonicalJson, sha256Bytes } from "../../../runtime-support/codex-luna-contract.mjs";

function fixture() {
  const root = fs.mkdtempSync(path.join("/private/tmp", "macos-luna-e2e-contract-"));
  const scenarioRoot = path.join(root, "scenario");
  const rawRoot = path.join(scenarioRoot, "raw");
  fs.mkdirSync(rawRoot, { recursive: true });
  const scenario = {
    scenario_id: "api-execution-overrun",
    problem_time: "2026-08-23T10:00:05.500000+08:00",
    client_process: "rpc_client",
    server_process: "rpc_server",
    service: "svc_orders",
    api: "Reserve",
    expected_status: "CONFIRMED",
    expected_branch_markers: ["API_COMPLETE"],
    expected_terms: [],
    expected_evidence_identities: [],
    forbidden_evidence_terms: ["invented"],
  };
  fs.writeFileSync(path.join(scenarioRoot, "case.json"), `${JSON.stringify(scenario)}\n`);
  fs.writeFileSync(path.join(rawRoot, "client.log"), "client one\nclient timeout\n");
  fs.writeFileSync(path.join(rawRoot, "server.log"), "server one\nAPI_COMPLETE\n");
  const wiki = path.join(root, "wiki.md");
  fs.writeFileSync(wiki, "# RPC timeout\n");
  const meta = path.join(root, "meta");
  fs.mkdirSync(path.join(meta, "references"), { recursive: true });
  fs.mkdirSync(path.join(meta, "scripts"), { recursive: true });
  fs.writeFileSync(path.join(meta, "SKILL.md"), "---\nname: wiki-to-diagnosis-skill\ndescription: test\n---\n");
  fs.writeFileSync(path.join(meta, "references", "output-contract.md"), "contract\n");
  fs.writeFileSync(path.join(meta, "scripts", "validate_generated_skill.py"), "# validator\n");
  const registration = path.join(root, "registration-template.json");
  fs.writeFileSync(registration, `${JSON.stringify({
    registration_id: "rpc-timeout-methods-v1",
    package: { skill_name: "diagnose-rpc-timeout" },
  })}\n`);
  const packageRoot = path.join(root, "generated", "diagnose-rpc-timeout");
  fs.mkdirSync(path.join(packageRoot, "references"), { recursive: true });
  fs.writeFileSync(path.join(packageRoot, "SKILL.md"), "skill\n");
  fs.writeFileSync(path.join(packageRoot, "methods.json"), "{}\n");
  fs.writeFileSync(path.join(packageRoot, "references", "method.md"), "method\n");
  return { root, scenarioRoot, rawRoot, wiki, meta, registration, packageRoot };
}

function codexIdentity() {
  return {
    status: "PASS",
    cli: {
      version: "codex-cli 0.149.0-alpha.4.1",
      sha256: "0".repeat(64),
      platform: "darwin",
      architecture: "arm64",
      code_mode_host: {
        sha256: "1".repeat(64),
      },
    },
  };
}

function scalarSchema(type = "string") {
  return { type };
}

function toolSchemas() {
  return MACOS_CODEX_LUNA_PUBLIC_TOOLS.map((name) => ({
    name,
    inputSchema: {
      type: "object",
      properties: {
        value: scalarSchema(),
        optional: { type: ["string", "null"] },
        values: { type: "array", items: scalarSchema() },
      },
      additionalProperties: false,
    },
  }));
}

function mcpCalls() {
  return [
    { server: "problem-locator", tool: "problem_locator_create_case", status: "completed", error: null, arguments: { request_id: "create" } },
    { server: "problem-locator", tool: "problem_locator_get_case", status: "completed", error: null, arguments: { case_id: "case", wait_for_job_id: null, wait_seconds: 0 } },
    { server: "problem-locator", tool: "problem_locator_prepare_attachment", status: "completed", error: null, arguments: { request_id: "prepare", expected_case_revision: 2 } },
    { server: "problem-locator", tool: "problem_locator_submit_supplement", status: "completed", error: null, arguments: { request_id: "submit", expected_case_revision: 3, attachment_ids: ["attachment"] } },
    { server: "problem-locator", tool: "problem_locator_get_case", status: "completed", error: null, arguments: { case_id: "case", wait_for_job_id: null, wait_seconds: 30 } },
    { server: "problem-locator", tool: "problem_locator_list_artifacts", status: "completed", error: null, arguments: { case_id: "case" } },
  ];
}

function invocations(phases) {
  return phases.map((phase) => ({
    phase,
    model: "gpt-5.6-luna",
    reasoning_effort: "medium",
    attempt: 1,
    retry: 0,
    status: "PASS",
    terminal: true,
    started_at_utc: "2026-08-24T00:00:00.000Z",
    finished_at_utc: "2026-08-24T00:00:01.000Z",
    wall_timeout_seconds: 600,
    usage: { input_tokens: 100, cached_input_tokens: 20, output_tokens: 50 },
  }));
}

test("scenario mapper excludes oracle fields and preserves the five deterministic user facts", () => {
  const f = fixture();
  const facts = loadScenarioFacts(path.join(f.scenarioRoot, "case.json"), "api-execution-overrun");
  assert.deepEqual(Object.keys(facts), ["scenario_id", "problem_time", "client_process", "server_process", "service", "api"]);
  const mapped = mapScenarioToCreateCase(facts);
  assert.deepEqual(mapped.initial_user_fact_names, ["problem_time", "client_process", "server_process", "service", "api"]);
  assert.deepEqual(mapped.initial_user_fact_values, ["2026-08-23T02:00:05.500Z", facts.client_process, facts.server_process, facts.service, facts.api]);
  assert.match(mapped.raw_problem_text, /2026-08-23T02:00:05\.500Z/);
  assert.equal(JSON.stringify(mapped).includes("CONFIRMED"), false);
  assert.throws(
    () => mapScenarioToCreateCase({ ...facts, expected_status: "CONFIRMED" }),
    (error) => error.code === "MACOS_CODEX_LUNA_MAPPER_INPUT_INVALID",
  );
  assert.throws(
    () => mapScenarioToCreateCase({ ...facts, problem_time: "2026-08-23T10:00:05.500" }),
    (error) => error.code === "MACOS_CODEX_LUNA_MAPPER_INPUT_INVALID",
  );
  const oracle = loadScenarioOracle(path.join(f.scenarioRoot, "case.json"), facts.scenario_id);
  assert.equal(oracle.expected_status, "CONFIRMED");
  assert.match(oracle.source_sha256, /^[a-f0-9]{64}$/);
});

test("logs.zip is byte deterministic, ordered, source-preserving, and refuses overwrite", () => {
  const f = fixture();
  const first = buildDeterministicLogsZip({ clientLog: path.join(f.rawRoot, "client.log"), serverLog: path.join(f.rawRoot, "server.log") });
  const second = buildDeterministicLogsZip({ clientLog: path.join(f.rawRoot, "client.log"), serverLog: path.join(f.rawRoot, "server.log") });
  assert.deepEqual(first.bytes, second.bytes);
  assert.equal(first.receipt.sha256, sha256Bytes(first.bytes));
  assert.deepEqual(first.receipt.members.map((member) => member.name), ["client.log", "server.log"]);
  assert.equal(first.receipt.newline_policy, "preserve-source-bytes");
  const destination = path.join(f.root, "out", "logs.zip");
  assert.deepEqual(writeDeterministicLogsZip({ clientLog: path.join(f.rawRoot, "client.log"), serverLog: path.join(f.rawRoot, "server.log"), destination }), first.receipt);
  assert.deepEqual(fs.readFileSync(destination), first.bytes);
  assert.throws(
    () => writeDeterministicLogsZip({ clientLog: path.join(f.rawRoot, "client.log"), serverLog: path.join(f.rawRoot, "server.log"), destination }),
    (error) => error.code === "MACOS_CODEX_LUNA_ZIP_EXISTS",
  );
});

test("all seven MCP schemas must be flat and the call ledger enforces order, revisions, and one Case", () => {
  const listed = auditListedMcpTools(toolSchemas());
  assert.equal(listed.status, "PASS");
  assert.equal(listed.schemas.length, 7);
  assert.throws(
    () => auditFlatMcpInputSchema({ type: "object", properties: { nested: { type: "object", properties: { x: scalarSchema() } } }, additionalProperties: false }),
    (error) => error.code === "MACOS_CODEX_LUNA_MCP_SCHEMA_NOT_FLAT",
  );
  assert.throws(
    () => auditFlatMcpInputSchema({ $defs: { value: scalarSchema() }, type: "object", properties: { value: { $ref: "#/$defs/value" } }, additionalProperties: false }),
    (error) => error.code === "MACOS_CODEX_LUNA_MCP_SCHEMA_FORBIDDEN",
  );
  const audited = auditMcpToolCalls(mcpCalls(), { attachmentId: "attachment", uploadRevision: 3 });
  assert.equal(audited.status, "PASS");
  const secondCase = mcpCalls();
  secondCase.splice(1, 0, secondCase[0]);
  assert.throws(() => auditMcpToolCalls(secondCase), (error) => error.code === "MACOS_CODEX_LUNA_CREATE_CASE_CARDINALITY_INVALID");
  const nested = mcpCalls();
  nested[1].arguments.bad = { nested: true };
  assert.throws(() => auditMcpToolCalls(nested), (error) => error.code === "MACOS_CODEX_LUNA_MCP_CALL_INVALID");
});

test("HTTP audit permits only MCP transport and the one descriptor PUT", () => {
  const receipt = auditHttpBoundary([
    { method: "POST", url: "http://127.0.0.1:8123/mcp", source: "codex-mcp" },
    { method: "PUT", url: "http://127.0.0.1:8123/uploads/token", source: "client-command" },
  ], {
    mcpUrl: "http://127.0.0.1:8123/mcp",
    uploadUrl: "http://127.0.0.1:8123/uploads/token",
  });
  assert.equal(receipt.status, "PASS");
  assert.throws(() => auditHttpBoundary([
    { method: "GET", url: "http://127.0.0.1:8123/api/cases/case", source: "client-command" },
    { method: "PUT", url: "http://127.0.0.1:8123/uploads/token", source: "client-command" },
  ], {
    mcpUrl: "http://127.0.0.1:8123/mcp",
    uploadUrl: "http://127.0.0.1:8123/uploads/token",
  }), (error) => error.code === "MACOS_CODEX_LUNA_HTTP_BOUNDARY_VIOLATION");
});

test("attachment audit binds the exact descriptor, READY bytes, upload revision, and supplement", () => {
  const archive = { size: 42, sha256: "a".repeat(64) };
  const descriptor = {
    attachment_id: "attachment",
    method: "PUT",
    required_headers: {
      "Idempotency-Key": "attachment",
      "Content-Type": "application/zip",
      "Content-Length": "42",
      "X-Content-SHA256": "a".repeat(64),
    },
  };
  const attachment = {
    attachment_id: "attachment",
    status: "READY",
    name: "logs.zip",
    content_type: "application/zip",
    declared_size: 42,
    size: 42,
    declared_sha256: "a".repeat(64),
    sha256: "a".repeat(64),
  };
  const uploadReceipt = { operation: "UploadAttachmentContent", primary_resource_id: "attachment", status: "READY", case_revision: 3 };
  const submitArguments = { expected_case_revision: 3, attachment_ids: ["attachment"] };
  assert.equal(auditUploadedAttachment({ attachment, uploadReceipt, descriptor, archive, submitArguments }).status, "PASS");
  assert.throws(
    () => auditUploadedAttachment({ attachment: { ...attachment, sha256: "b".repeat(64) }, uploadReceipt, descriptor, archive, submitArguments }),
    (error) => error.code === "MACOS_CODEX_LUNA_ATTACHMENT_BYTES_MISMATCH",
  );
  assert.throws(
    () => auditUploadedAttachment({ attachment, uploadReceipt, descriptor, archive, submitArguments: { ...submitArguments, expected_case_revision: 2 } }),
    (error) => error.code === "MACOS_CODEX_LUNA_UPLOAD_REVISION_STALE",
  );
});

test("Methods cache binds all producer inputs and detects package drift", () => {
  const f = fixture();
  const producer = buildMethodsProducerIdentity({ wiki: f.wiki, metaSkillRoot: f.meta, registrationTemplate: f.registration, codexIdentity: codexIdentity() });
  const cacheRoot = path.join(f.root, "cache");
  const destination = methodsCachePath(cacheRoot, producer.producer_identity);
  fs.mkdirSync(path.join(destination, "package"), { recursive: true });
  fs.cpSync(f.packageRoot, path.join(destination, "package", "diagnose-rpc-timeout"), { recursive: true });
  const manifest = buildMethodsCacheManifest({ producer, packageRoot: path.join(destination, "package", "diagnose-rpc-timeout"), registrationTemplate: f.registration });
  fs.writeFileSync(path.join(destination, "manifest.json"), `${canonicalJson(manifest)}\n`);
  const receipt = validateMethodsCache({ cacheRoot, producer, registrationTemplate: f.registration });
  assert.equal(receipt.status, "PASS");
  assert.equal(assertMethodsPackageUnchanged(receipt).status, "PASS");
  fs.appendFileSync(path.join(f.meta, "scripts", "validate_generated_skill.py"), "# drift\n");
  assert.notEqual(buildMethodsProducerIdentity({ wiki: f.wiki, metaSkillRoot: f.meta, registrationTemplate: f.registration, codexIdentity: codexIdentity() }).producer_identity, producer.producer_identity);
  fs.appendFileSync(path.join(receipt.package_root, "SKILL.md"), "drift\n");
  assert.throws(() => assertMethodsPackageUnchanged(receipt), (error) => error.code === "MACOS_CODEX_LUNA_METHODS_PACKAGE_DRIFT");
  assert.throws(() => validateMethodsCache({ cacheRoot, producer, registrationTemplate: f.registration }), (error) => error.code === "MACOS_CODEX_LUNA_METHODS_CACHE_IDENTITY_MISMATCH");
});

test("Methods content cache key excludes validated Codex runtime and platform identity", () => {
  const f = fixture();
  const nativeIdentity = codexIdentity();
  const linuxIdentity = structuredClone(nativeIdentity);
  linuxIdentity.cli.version = "codex-cli 0.149.1";
  linuxIdentity.cli.sha256 = "2".repeat(64);
  linuxIdentity.cli.code_mode_host.sha256 = "3".repeat(64);
  linuxIdentity.cli.platform = "linux";
  linuxIdentity.cli.architecture = "x64";
  const nativeProducer = buildMethodsProducerIdentity({ wiki: f.wiki, metaSkillRoot: f.meta, registrationTemplate: f.registration, codexIdentity: nativeIdentity });
  const linuxProducer = buildMethodsProducerIdentity({ wiki: f.wiki, metaSkillRoot: f.meta, registrationTemplate: f.registration, codexIdentity: linuxIdentity });
  assert.equal(linuxProducer.producer_identity, nativeProducer.producer_identity);
  assert.deepEqual(linuxProducer.inputs, nativeProducer.inputs);
  assert.equal(Object.hasOwn(linuxProducer.inputs, "codex"), false);
  assert.deepEqual(linuxProducer.inputs.model, { model: "gpt-5.6-luna", reasoning_effort: "medium" });
});

test("model audit requires one bootstrap or exactly five ordered E2E terminal calls with no retry", () => {
  assert.equal(auditModelInvocations(invocations(["METHODS_BOOTSTRAP"]), { workflow: "methods" }).aggregate.total_tokens, 150);
  assert.equal(auditModelInvocations(invocations(["CLIENT", "ROUTE", "LOGPARSE", "DIAGNOSE", "REVIEW"]), { workflow: "e2e" }).retry_count, 0);
  assert.deepEqual(aggregateCodexUsage(invocations(["CLIENT", "ROUTE"]).map((item) => ({ usage: item.usage }))), {
    input_tokens: 200,
    cached_input_tokens: 40,
    output_tokens: 100,
    total_tokens: 300,
    equivalent_usd: 0.000153,
  });
  assert.throws(
    () => auditModelInvocations(invocations(["CLIENT", "ROUTE", "DIAGNOSE", "REVIEW"]), { workflow: "e2e" }),
    (error) => error.code === "MACOS_CODEX_LUNA_INVOCATION_COUNT_INVALID",
  );
  const retried = invocations(["CLIENT", "ROUTE", "LOGPARSE", "DIAGNOSE", "REVIEW"]);
  retried[2].retry = 1;
  assert.throws(() => auditModelInvocations(retried, { workflow: "e2e" }), (error) => error.code === "MACOS_CODEX_LUNA_INVOCATION_IDENTITY_INVALID");
  const missingUsage = invocations(["CLIENT", "ROUTE", "LOGPARSE", "DIAGNOSE", "REVIEW"]);
  delete missingUsage[4].usage;
  assert.throws(() => auditModelInvocations(missingUsage, { workflow: "e2e" }), (error) => error.code === "MACOS_CODEX_LUNA_TERMINAL_USAGE_INVALID");
  const overBudget = invocations(["CLIENT", "ROUTE", "LOGPARSE", "DIAGNOSE", "REVIEW"]);
  overBudget[0].usage.input_tokens = 2_000_001;
  assert.throws(() => auditModelInvocations(overBudget, { workflow: "e2e" }), (error) => error.code === "MACOS_CODEX_LUNA_BUDGET_EXCEEDED");
  assert.equal(MACOS_CODEX_LUNA_CALL_WALL_SECONDS, 600);
  assert.equal(MACOS_CODEX_LUNA_NO_PROGRESS_SECONDS, 180);
});

test("oracle is loaded after execution and binds status, marker, forbidden terms, and source bytes", () => {
  const f = fixture();
  const oracle = loadScenarioOracle(path.join(f.scenarioRoot, "case.json"), "api-execution-overrun");
  const serverBytes = fs.readFileSync(path.join(f.rawRoot, "server.log"));
  const result = auditOracle({
    oracle,
    publicCase: { status: "COMPLETED" },
    sealedDiagnosis: {
      status: "CONFIRMED",
      confirmed_methods: ["api-complete-method"],
      evidence: [{ method_id: "api-complete-method", identity_tokens: ["request_id=1"], sources: [{ source_id: "server", file_name: "server.log", raw_sha256: sha256Bytes(serverBytes), line_number: 2, marker: "API_COMPLETE service=", line: "API_COMPLETE" }] }],
    },
    evidenceSources: [{ source_id: "server", file_name: "server.log", raw_sha256: sha256Bytes(serverBytes), lines: ["server one", "API_COMPLETE", ""] }],
  });
  assert.equal(result.status, "PASS");
  assert.throws(() => auditOracle({
    oracle,
    publicCase: { status: "COMPLETED" },
    sealedDiagnosis: { status: "CONFIRMED", confirmed_methods: ["api-complete-method"], evidence: [{ method_id: "api-complete-method", identity_tokens: [], sources: [{ marker: "API_COMPLETELY" }] }] },
    evidenceSources: [],
  }), (error) => error.code === "MACOS_CODEX_LUNA_BRANCH_MARKER_MISSING");
  assert.throws(() => auditOracle({
    oracle,
    publicCase: { status: "COMPLETED" },
    sealedDiagnosis: { status: "CONFIRMED", confirmed_methods: ["api-complete-method"], evidence: [{ method_id: "api-complete-method", identity_tokens: [], summary: "invented", sources: [{ marker: "API_COMPLETE" }] }] },
    evidenceSources: [],
  }), (error) => error.code === "MACOS_CODEX_LUNA_FORBIDDEN_TERM_PRESENT");
});
