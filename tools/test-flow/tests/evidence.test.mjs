import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import { providerRunnerFailureResult } from "../lib/actions.mjs";
import {
  applyGateEvidenceContract,
  buildRunCandidate,
  writeExecutedStageReceipt,
  writeGateReceipt,
} from "../lib/engine.mjs";
import { allowedEmptyEventFiles, createAttempt, finalizeAttempt, recoverStageAuditProgress, requiredEventFiles, validPlannedStageResultSource, verifyVerdict } from "../lib/evidence.mjs";
import { EventWriter } from "../lib/events.mjs";
import { FAILURE_DIAGNOSTIC_FIELDS, projectCandidateFailureDiagnostic, validFailureDiagnostic } from "../lib/failure-diagnostic.mjs";
import { zeroUsage } from "../lib/usage.mjs";
import { canonicalJson, removeTreeWritable, resolveCommand, sha256Bytes, sha256File, writeJsonSync } from "../lib/util.mjs";
import {
  materializeProviderTerminalFailure as materializeP1TerminalFailure,
  safeE2EError as safeP1Error,
} from "../quick-validation/claude-deepseek/runtime/claude-deepseek-e2e-runner.mjs";
import {
  materializeProviderTerminalFailure as materializeP2TerminalFailure,
  safeE2EError as safeP2Error,
} from "../quick-validation/codex-luna/runtime/macos-codex-luna-e2e-runner.mjs";

const TOOL_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const REPO_ROOT = path.resolve(TOOL_ROOT, "..", "..");
const PRODUCTION_RUNTIME_DRIVER = path.join(TOOL_ROOT, "quick-validation", "codex-luna", "runtime", "macos_codex_luna_model_cert_driver.py");
const RELEASE_CASE_ROOT = path.join(REPO_ROOT, "tests", "cases", "release", "rpc-timeout-anonymized");
const STATUS_POLICY = { pass: 0, pass_with_warnings: 0, fail: 1, blocked: 2, error: 3 };
const ZERO_USAGE = zeroUsage();

test("admission-blocked plans require every Stage to remain not executed", () => {
  const reusedPlan = { decision: "REUSE" };
  const runPlan = { decision: "RUN" };
  assert.equal(validPlannedStageResultSource(
    reusedPlan,
    { result_source: "NOT_EXECUTED" },
    "BLOCKED",
  ), true);
  assert.equal(validPlannedStageResultSource(
    runPlan,
    { result_source: "NOT_EXECUTED" },
    "BLOCKED",
  ), true);
  assert.equal(validPlannedStageResultSource(
    reusedPlan,
    { result_source: "REUSED" },
    "BLOCKED",
  ), false);
  assert.equal(validPlannedStageResultSource(
    reusedPlan,
    { result_source: "REUSED" },
    "ADMITTED",
  ), true);
});

function python312Executable(candidate) {
  if (!candidate) return null;
  const executable = path.isAbsolute(candidate) ? path.resolve(candidate) : resolveCommand(candidate);
  if (executable === null || !fs.existsSync(executable) || !fs.statSync(executable).isFile()) return null;
  const probe = spawnSync(executable, ["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"], {
    cwd: REPO_ROOT,
    env: process.env,
    encoding: "utf8",
  });
  return probe.status === 0 && probe.stdout.trim() === "3.12"
    ? { command: executable, interpreterPrefix: [] }
    : null;
}

function productionDriverCandidate(environment = process.env, platform = process.platform, exists = fs.existsSync) {
  if (environment.TEST_FLOW_PYTHON) return environment.TEST_FLOW_PYTHON;
  if (environment.TEST_FLOW_QUICK_PYTHON) return environment.TEST_FLOW_QUICK_PYTHON;
  if (platform === "linux") {
    const sealed = "/opt/venvs/xiaodao/bin/python";
    return exists(sealed) ? sealed : null;
  }
  if (platform === "win32") {
    const bundled = path.join(
      os.homedir(), ".cache", "codex-runtimes", "codex-primary-runtime",
      "dependencies", "python", "python.exe",
    );
    return exists(bundled) ? bundled : null;
  }
  return null;
}

const PRODUCTION_DRIVER_PYTHON = python312Executable(productionDriverCandidate());
const PRODUCTION_DRIVER_SKIP = PRODUCTION_DRIVER_PYTHON === null
  && process.platform === "darwin"
  && !process.env.TEST_FLOW_PYTHON
  && !process.env.TEST_FLOW_QUICK_PYTHON;

function closeMinimalStream(attemptRoot, runId) {
  const writer = new EventWriter({ attemptRoot, runId, producerId: "orchestrator", producerType: "orchestrator" });
  writer.write("run.created", { data: { track: "dev" } });
  writer.close();
}

function writeExecutedStage(attemptRoot) {
  const stageId = "deterministic.full";
  const gateId = "det.unit";
  const gateRoot = path.join(attemptRoot, "payload", "stages", stageId, "gates", gateId);
  fs.mkdirSync(gateRoot, { recursive: true });
  const receiptPath = path.join(gateRoot, "gate-receipt.json");
  writeJsonSync(receiptPath, {
    schema_version: 2,
    stage_id: stageId,
    gate_id: gateId,
    gate_kind: "pytest",
    gate_identity: "gate-identity-a",
    definition_digest: "gate-definition-a",
    evidence_contract: null,
    runtime_profile: "python-test",
    runtime_profile_digest: "runtime-python-a",
    result_source: "EXECUTED",
    status: "PASS",
    code: null,
    failure_domain: null,
    elapsed_seconds: 1,
    usage: ZERO_USAGE,
    usage_complete: true,
    effective_caps: null,
    model_invocations: [],
    fresh_admission: null,
    evidence: [],
    execution: { exit_code: 0, signal: null, termination: null, stdout_path: null, stderr_path: null },
    assertions: { pytest: { executed: 1, passed: 1, skipped: 0 }, node_test: null, selection: null, adapter: null },
  });
  const gate = {
    id: gateId,
    kind: "pytest",
    status: "PASS",
    code: null,
    failure_domain: null,
    gate_identity: "gate-identity-a",
    definition_digest: "gate-definition-a",
    evidence_contract: null,
    runtime_profile: "python-test",
    runtime_profile_digest: "runtime-python-a",
    receipt_path: path.relative(attemptRoot, receiptPath).split(path.sep).join("/"),
    receipt_digest: sha256File(receiptPath),
    elapsed_seconds: 1,
    usage: ZERO_USAGE,
    usage_complete: true,
    effective_caps: null,
    model_invocations: [],
    fresh_admission: null,
    evidence: [],
  };
  const stageReceipt = {
    schema_version: 2,
    id: stageId,
    kind: "deterministic",
    status: "PASS",
    code: null,
    failure_domain: null,
    operation_failure: null,
    result_source: "EXECUTED",
    producer_identity: "producer-a",
    proof_identity: "proof-a",
    performance_identity: "performance-a",
    performance_status: "PASS",
    performance_reason: null,
    performance_baseline: null,
    consecutive_significant_regressions: 0,
    elapsed_seconds: 1,
    usage: ZERO_USAGE,
    gates: [gate],
    checkpoint: null,
    restored_checkpoint: null,
  };
  const stagePath = path.join(attemptRoot, "payload", "stages", stageId, "stage-receipt.json");
  writeJsonSync(stagePath, stageReceipt);
  return {
    ...stageReceipt,
    stage_receipt_path: path.relative(attemptRoot, stagePath).split(path.sep).join("/"),
    stage_receipt_digest: sha256File(stagePath),
  };
}

function providerPlanStage(provider) {
  return {
    hard_caps: { hard_timeout_seconds: 600, max_budget_usd: 4, max_output_tokens: 64_000, max_total_tokens: 2_000_000, max_turns: 50 },
    invocation_caps: [{
      aggregate: true,
      caps: { hard_timeout_seconds: 600, max_budget_usd: 4, max_output_tokens: 64_000, max_total_tokens: 2_000_000, max_turns: 50 },
      class: provider === "claude-deepseek" ? "claude-deepseek-macos-e2e" : "codex-luna-macos-e2e",
      max_count: 4,
      min_count: 2,
      normal_count: 2,
      repair_max_count: 2,
      phases: ["SPECIALIST:PRIMARY", "SPECIALIST:REPAIR?", "REVIEWER:PRIMARY", "REVIEWER:REPAIR?"],
    }],
    producer_identity: `producer-${provider}`,
    proof_identity: `proof-${provider}`,
    performance_identity: `performance-${provider}`,
  };
}

function writeReleaseRegistrationInput(root) {
  const registrationSource = path.join(RELEASE_CASE_ROOT, "registration", "rpc-timeout-methods-v1", "registration-template.json");
  const wiki = fs.readFileSync(path.join(RELEASE_CASE_ROOT, "input", "wiki.md"), "utf8");
  const expected = JSON.parse(fs.readFileSync(path.join(RELEASE_CASE_ROOT, "oracle.json"), "utf8")).expected_package;
  const packageRoot = path.join(root, "package", "diagnose-rpc-timeout");
  const references = path.join(packageRoot, "references");
  fs.mkdirSync(references, { recursive: true });
  fs.copyFileSync(registrationSource, path.join(root, "registration-template.json"));
  const methodSpecs = [
    ["api-execution-slow", "API 执行时间过长", "api-execution-slow.md"],
    ["server-queueing", "服务端收包排队", "server-queueing.md"],
    ["client-receive-blocked", "客户端收包线程阻塞", "client-receive-blocked.md"],
  ];
  const methods = {
    schema_version: 1,
    skill_name: expected.skill_name,
    source_wiki_sha256: expected.source_wiki_sha256,
    required_user_inputs: expected.required_user_inputs,
    required_artifacts: expected.required_artifacts,
    log_derived_fields: expected.required_log_derived_fields,
    shared_references: ["references/source-log-templates.md", "references/shared-boundaries.md"],
    methods: methodSpecs.map(([id, title, filename], index) => ({
      id,
      title,
      reference: `references/${filename}`,
      priority: index + 1,
      evidence_markers: expected.method_marker_sets[index].all_markers,
      activation_markers: expected.method_marker_sets[index].activation_markers,
    })),
  };
  fs.writeFileSync(path.join(packageRoot, "methods.json"), `${JSON.stringify(methods, null, 2)}\n`);
  fs.writeFileSync(path.join(packageRoot, "SKILL.md"), "---\nname: diagnose-rpc-timeout\ndescription: Test-owned production Runtime fixture.\n---\n\nRead request.json and the evaluation_input embedded in the runtime context. Use evaluation_input.observations and evaluation_input.markers through each item in evaluation_input.evaluations and its events. Return only evaluation_ref, verdict, supporting_event_refs, and reason; UNKNOWN is allowed.\n");
  const templates = [];
  let inFence = false;
  for (const rawLine of wiki.split(/\r?\n/u)) {
    const line = rawLine.trim();
    if (line === "```text") { inFence = true; continue; }
    if (line === "```" && inFence) { inFence = false; continue; }
    if (inFence && line) templates.push(line);
  }
  for (const method of methods.methods) {
    const matchedMarkers = [];
    const selected = templates.filter((template) => {
      const matches = method.evidence_markers.filter((marker) => template.includes(marker));
      assert.ok(matches.length <= 1, `expected at most one canonical marker for ${template}`);
      if (matches.length === 0) return false;
      matchedMarkers.push(matches[0]);
      return true;
    });
    assert.deepEqual(matchedMarkers, method.evidence_markers, `method ${method.id} markers must follow source template order`);
    const card = [
      "## 适用条件\n固定 Release 用例。",
      `## 所需证据\n${selected.map((template) => `- \`${template}\``).join("\n")}`,
      "## 计算与判断\n按冻结 Evidence Graph 中的完整方法证据计算。",
      "## 确认条件\n满足方法规则时确认。",
      "## 未知边界\n必要证据缺失时返回 UNKNOWN。",
      "## 输出含义\n输出 evaluation verdict。",
    ].join("\n\n");
    fs.writeFileSync(path.join(packageRoot, method.reference), `${card}\n`);
  }
  fs.writeFileSync(path.join(references, "source-log-templates.md"), `# Source log templates\n\n\`\`\`text\n${templates.join("\n")}\n\`\`\`\n`);
  fs.writeFileSync(path.join(references, "shared-boundaries.md"), "RPC 超时不等于取消。\n");
}

function writeProviderServiceFixture({ fixtureRoot, provider, runId, posthocBudgetFailure }) {
  const evidenceRoot = path.join(fixtureRoot, "evidence");
  const usageRoot = path.join(fixtureRoot, "usage");
  const privateRoot = path.join(fixtureRoot, "private");
  const helperPath = path.join(fixtureRoot, "provider-service.mjs");
  const configPath = path.join(fixtureRoot, "provider-config.json");
  fs.mkdirSync(evidenceRoot, { recursive: true });
  fs.mkdirSync(usageRoot, { recursive: true });
  fs.mkdirSync(privateRoot, { recursive: true });
  let values;
  let wrapperPath;
  let helper;
  if (provider === "claude-deepseek") {
    wrapperPath = path.join(TOOL_ROOT, "quick-validation", "claude-deepseek", "runtime", "claude-deepseek-service-wrapper.mjs");
    values = {
      "claude-entry": path.join(fixtureRoot, "fake-claude-entry"),
      settings: path.join(fixtureRoot, "claude-settings.json"),
      "config-root": path.join(fixtureRoot, "claude-config"),
      "private-root": privateRoot,
      "evidence-root": evidenceRoot,
      "usage-root": usageRoot,
      "run-id": runId,
    };
    fs.mkdirSync(values["config-root"], { recursive: true });
    fs.writeFileSync(values["claude-entry"], "fixture\n");
    fs.writeFileSync(values.settings, "{}\n");
    helper = [
      `import * as wrapper from ${JSON.stringify(pathToFileURL(wrapperPath).href)};`,
      "import fs from 'node:fs';",
      "const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));",
      "const values=config.values;",
      "const rawError=new Error('raw DeepSeek provider budget terminal');",
      "rawError.code='CLAUDE_DEEPSEEK_MAX_BUDGET_EXCEEDED';",
      "rawError.details={exit_code:1,signal:null,terminal:{subtype:'error_max_budget_usd',is_error:true,stop_reason:'tool_use',turns:1,usage:{schema_version:1,input_tokens:23302,output_tokens:37245,cache_creation_input_tokens:0,cache_read_input_tokens:0,total_tokens:60547,cost_usd:1.047635}}};",
      "const posthoc=config.posthocBudgetFailure&&typeof wrapper.roleInvocationBudget==='function';",
      "const runClaude=posthoc?async()=>({receipt:{model:'deepseek-v4-flash[1m]',started_at_utc:'2026-08-29T00:00:00.000Z',finished_at_utc:'2026-08-29T00:00:01.000Z',turns:1,provider_terminal:{subtype:'success',is_error:false,stop_reason:'end_turn',exit_code:0,signal:null},usage:{schema_version:1,input_tokens:150,output_tokens:50,cache_creation_input_tokens:0,cache_read_input_tokens:0,total_tokens:200,cost_usd:2.000001},environment_policy:null}}):async()=>{throw rawError;};",
      "try{await wrapper.runServiceInvocation(values,{runClaude});process.exitCode=0;}catch(error){process.stderr.write(JSON.stringify({code:error?.code??'PROVIDER_FIXTURE_FAILED'})+'\\n');process.exitCode=1;}",
      "",
    ].join("\n");
  } else {
    wrapperPath = path.join(TOOL_ROOT, "quick-validation", "codex-luna", "runtime", "macos-codex-luna-model-cert-wrapper.mjs");
    values = {
      "codex-entry": path.join(fixtureRoot, "fake-codex"),
      "auth-source": path.join(fixtureRoot, "codex-auth.json"),
      "skill-source": path.join(fixtureRoot, "codex-evaluator-SKILL.md"),
      "expected-cli-version": "0.149.0-alpha.4.1",
      "private-root": privateRoot,
      "evidence-root": evidenceRoot,
      "usage-root": usageRoot,
      "run-id": runId,
    };
    fs.writeFileSync(values["codex-entry"], "fixture\n");
    fs.writeFileSync(values["skill-source"], "---\nname: codex-luna-evidence-v2-evaluator\ndescription: fixture\n---\n");
    fs.writeFileSync(values["auth-source"], `${JSON.stringify({
      auth_mode: "chatgpt",
      OPENAI_API_KEY: null,
      tokens: {
        access_token: "fixture-access-token",
        refresh_token: "fixture-refresh-token",
        id_token: "fixture-id-token",
        account_id: "fixture-account-id",
      },
    })}\n`);
    helper = [
      `import * as wrapper from ${JSON.stringify(pathToFileURL(wrapperPath).href)};`,
      "import fs from 'node:fs';",
      "const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));",
      "const rawError=new Error('raw Codex provider terminal');",
      "rawError.code='CODEX_LUNA_APP_SERVER_ERROR_NOTIFICATION';",
      "rawError.details={usage:{input_tokens:12,cached_input_tokens:2,cache_write_input_tokens:0,output_tokens:4,reasoning_output_tokens:1,total_tokens:16},thread_id:'thread-failed',turn_id:'turn-failed'};",
      "try{await wrapper.runModelRoleInvocation(config.values,{ambient:{},runAppServerCall:async()=>{throw rawError;}});process.exitCode=0;}catch(error){process.stderr.write(JSON.stringify({code:error?.code??'PROVIDER_FIXTURE_FAILED'})+'\\n');process.exitCode=1;}",
      "",
    ].join("\n");
  }
  fs.writeFileSync(helperPath, helper);
  fs.writeFileSync(configPath, canonicalJson({ values, posthocBudgetFailure }));
  const commandPath = path.join(fixtureRoot, "provider-command.json");
  fs.writeFileSync(commandPath, canonicalJson([process.execPath, helperPath, configPath]));
  return { evidenceRoot, usageRoot, commandPath };
}

function runProductionFailureRuntime({ attemptRoot, gateRoot, usageRoot, provider, posthocBudgetFailure }) {
  assert.notEqual(PRODUCTION_DRIVER_PYTHON, null, "Python 3.12 is required for the production failure fixture");
  const fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "ev2-runtime-"));
  const receiptPath = path.join(fixtureRoot, "evidence", "runtime-receipt.json");
  const registrationRoot = path.join(fixtureRoot, "registration");
  writeReleaseRegistrationInput(registrationRoot);
  const service = writeProviderServiceFixture({
    fixtureRoot,
    provider,
    runId: path.basename(attemptRoot),
    posthocBudgetFailure,
  });
  const bootstrap = [
    "import json,os,runpy,subprocess,sys,traceback,types",
    "from pathlib import Path",
    "mark=types.SimpleNamespace(parametrize=lambda *a,**k:(lambda f:f))",
    "sys.modules['pytest']=types.SimpleNamespace(fixture=lambda f:f,mark=mark)",
    "module=runpy.run_path(sys.argv[1])",
    "command=json.loads(Path(sys.argv[7]).read_text(encoding='utf-8'))",
    "class ServiceSubprocessBackend:",
    "    def execute(self,**kwargs):",
    "        try:",
    "            return self._execute(**kwargs)",
    "        except BaseException:",
    "            Path(sys.argv[8]).write_text(traceback.format_exc(),encoding='utf-8')",
    "            raise",
    "    def _execute(self,**kwargs):",
    "        result=subprocess.run(command,input=str(kwargs['prompt']).encode('utf-8'),cwd=os.fspath(kwargs['workspace_root']),env=dict(os.environ),stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)",
    "        if result.stdout:",
    "            kwargs['log_sinks'].stdout.write(result.stdout)",
    "        if result.stderr:",
    "            kwargs['log_sinks'].stderr.write(result.stderr)",
    "        module['_close_sinks'](kwargs['log_sinks'])",
    "        if result.returncode!=0:",
    "            raise module['runtime_failure'](stage=module['ExecutionStage'].BACKEND_EXECUTE,code=module['ErrorCode'].BACKEND_EXIT_FAILED,message='provider service fixture failed')",
    "        return module['BackendExecution'](returncode=0,stdout_stderr_bytes=len(result.stdout)+len(result.stderr),workspace_bytes=0,elapsed_seconds=0.01)",
    "backend=ServiceSubprocessBackend()",
    "receipt=module['run_production_model_cert'](work_root=Path(sys.argv[4]),role_backend=backend,source_root=Path(sys.argv[2]),registration_root=Path(sys.argv[3]),evidence_root=Path(sys.argv[5]),execution_mode='real-model')",
    "Path(sys.argv[6]).write_bytes(module['canonical_json_bytes'](receipt))",
  ].join("\n");
  try {
    const result = spawnSync(PRODUCTION_DRIVER_PYTHON.command, [
      ...PRODUCTION_DRIVER_PYTHON.interpreterPrefix,
      "-c", bootstrap,
      PRODUCTION_RUNTIME_DRIVER,
      REPO_ROOT,
      registrationRoot,
      path.join(fixtureRoot, "work"),
      service.evidenceRoot,
      receiptPath,
      service.commandPath,
      path.join(fixtureRoot, "backend-error.txt"),
    ], { cwd: REPO_ROOT, env: process.env, encoding: "utf8", timeout: 120_000 });
    assert.equal(result.status, 0, result.stderr);
    const receipt = JSON.parse(fs.readFileSync(receiptPath, "utf8"));
    if (receipt.methods_result?.reason_code === "SERVER_INVARIANT_VIOLATION") {
      const debugPath = path.join(fixtureRoot, "backend-error.txt");
      assert.fail(fs.existsSync(debugPath) ? fs.readFileSync(debugPath, "utf8") : "provider backend violated a server invariant without an exception trace");
    }
    for (const name of fs.readdirSync(service.evidenceRoot)) {
      const source = path.join(service.evidenceRoot, name);
      if (fs.statSync(source).isFile()) fs.copyFileSync(source, path.join(gateRoot, name));
    }
    for (const name of fs.readdirSync(service.usageRoot)) {
      const source = path.join(service.usageRoot, name);
      if (fs.statSync(source).isFile()) fs.copyFileSync(source, path.join(usageRoot, name));
    }
    return receipt;
  } finally {
    fs.rmSync(fixtureRoot, { recursive: true, force: true });
  }
}

async function writeEvidenceV2FailureStage(attemptRoot, {
  certificationTarget = "P1",
  includeEvidence = true,
  p1PosthocBudgetFailure = false,
  dropProviderInvocations = false,
} = {}) {
  const provider = certificationTarget === "P1" ? "claude-deepseek" : "codex-luna";
  const stageId = certificationTarget === "P1" ? "real.macos-claude-deepseek-e2e" : "real.macos-codex-luna-e2e";
  const gateId = stageId;
  const gateRoot = path.join(attemptRoot, "payload", "stages", stageId, "gates", gateId);
  const usageRoot = path.join(attemptRoot, "payload", "model-usage", `${provider}-e2e`);
  fs.mkdirSync(gateRoot, { recursive: true });
  fs.mkdirSync(usageRoot, { recursive: true });
  const runtime = runProductionFailureRuntime({
    attemptRoot,
    gateRoot,
    usageRoot,
    provider,
    posthocBudgetFailure: p1PosthocBudgetFailure,
  });
  const materialize = certificationTarget === "P1" ? materializeP1TerminalFailure : materializeP2TerminalFailure;
  const safeError = certificationTarget === "P1" ? safeP1Error : safeP2Error;
  materialize(runtime, gateRoot, { modelCalls: runtime.model_invocations, repairs: runtime.repair_counts });

  const logRoot = path.join(attemptRoot, "payload", "logs");
  fs.mkdirSync(logRoot, { recursive: true });
  const stderrPath = path.join(logRoot, `${stageId}--${gateId}.stderr.log`);
  fs.writeFileSync(stderrPath, canonicalJson(safeError({
    code: runtime.methods_result.reason_code,
    message: runtime.methods_result.reasons[0],
  })));
  const planStage = providerPlanStage(provider);
  let actionResult = providerRunnerFailureResult({
    provider,
    result: {
      status: "FAIL",
      elapsed_seconds: 1,
      exit_code: 1,
      signal: null,
      termination: null,
      stderr_truncated: false,
      stderr_path: path.relative(attemptRoot, stderrPath).split(path.sep).join("/"),
    },
    attemptRoot,
    outputRoot: gateRoot,
    usageRoot,
    planStage,
    invocationClass: provider === "claude-deepseek" ? "claude-deepseek-macos-e2e" : "codex-luna-macos-e2e",
    fallbackCode: provider === "claude-deepseek" ? "CLAUDE_DEEPSEEK_RUNNER_FAILED" : "MACOS_CODEX_LUNA_RUNNER_FAILED",
  });
  if (dropProviderInvocations) actionResult = { ...actionResult, invocations: [] };
  const stage = { id: stageId, kind: "capability" };
  const gatePlan = {
    id: gateId,
    gate_identity: `gate-identity-${certificationTarget.toLowerCase()}`,
    definition_digest: `gate-definition-${certificationTarget.toLowerCase()}`,
    evidence_contract: null,
    runtime_profile: "release",
    runtime_profile_digest: "runtime-release-a",
  };
  const gate = { evidence: includeEvidence ? ["runtime-receipt.json", "adapter-receipt.json"] : [] };
  const contracted = applyGateEvidenceContract({ actionResult, gate, gatePlan, stage, attemptRoot });
  const gateReceipt = writeGateReceipt({ attemptRoot, stage, gatePlan, actionResult: contracted.result, evidence: contracted.evidence, planStage });
  return writeExecutedStageReceipt({ attemptRoot, stage, planStage, gateResults: [gateReceipt] });
}

function writeOrdinaryStage(attemptRoot, status) {
  const stage = { id: `ordinary.${status.toLowerCase()}`, kind: "deterministic" };
  const gatePlan = {
    id: `${stage.id}.gate`,
    gate_identity: `${stage.id}-gate-identity`,
    definition_digest: `${stage.id}-gate-definition`,
    evidence_contract: null,
    runtime_profile: null,
    runtime_profile_digest: null,
  };
  const planStage = {
    invocation_caps: [],
    producer_identity: `${stage.id}-producer`,
    proof_identity: `${stage.id}-proof`,
    performance_identity: `${stage.id}-performance`,
  };
  const actionResult = status === "PASS"
    ? { status: "PASS", code: null, failure_domain: null, elapsed_seconds: 0, usage_complete: true, invocations: [] }
    : { status: "FAIL", code: "ORDINARY_PRODUCT_FAILURE", failure_domain: "PRODUCT", elapsed_seconds: 0, usage_complete: true, invocations: [] };
  const gateReceipt = writeGateReceipt({ attemptRoot, stage, gatePlan, actionResult, evidence: [], planStage });
  return writeExecutedStageReceipt({
    attemptRoot,
    stage,
    planStage,
    gateResults: [gateReceipt],
    performance: status === "PASS"
      ? { status: "PASS", reason: null, baseline: null, consecutive_significant_regressions: 0 }
      : undefined,
  });
}

function writeReusedStage(attemptRoot, sourceRunId, sourceStageDigest) {
  const stageReceipt = {
    schema_version: 2,
    id: "deterministic.full",
    kind: "deterministic",
    status: "PASS",
    code: null,
    failure_domain: null,
    result_source: "REUSED",
    reused_from: { run_id: sourceRunId, source_stage_receipt_digest: sourceStageDigest },
    producer_identity: "producer-a",
    proof_identity: "proof-a",
    performance_identity: "performance-a",
    performance_status: "NOT_MEASURED",
    performance_baseline: null,
    elapsed_seconds: null,
    usage: ZERO_USAGE,
    gates: [],
    checkpoint: null,
  };
  const stagePath = path.join(attemptRoot, "payload", "stages", stageReceipt.id, "stage-receipt.json");
  fs.mkdirSync(path.dirname(stagePath), { recursive: true });
  writeJsonSync(stagePath, stageReceipt);
  return {
    ...stageReceipt,
    stage_receipt_path: path.relative(attemptRoot, stagePath).split(path.sep).join("/"),
    stage_receipt_digest: sha256File(stagePath),
  };
}

function writePlanAndBuildCandidate(attemptRoot, runId, stageInput) {
  const stages = Array.isArray(stageInput) ? stageInput : [stageInput];
  const proof = {
    id: "proof.test-flow",
    acceptance: "all",
    stages: stages.map((stage) => stage.id),
    proof_definition_digest: "proof-definition-a",
  };
  const admission = { status: "ADMITTED", blockers: [], warnings: [] };
  const configDigests = {
    proofs: "config-proofs", stages: "config-stages", gates: "config-gates",
    identities: "config-identities", policy: "config-policy", runtimeProfiles: "config-runtime",
  };
  const sourceManifest = {
    schema_version: 1,
    algorithm: "git-visible-worktree-v1",
    digest: sha256Bytes(canonicalJson([])),
    file_count: 0,
    records: [],
  };
  const sourceSnapshot = {
    schema_version: 1,
    algorithm: sourceManifest.algorithm,
    status: "PRESENT",
    digest: sourceManifest.digest,
    file_count: 0,
  };
  const sourceVerification = {
    schema_version: 1,
    status: "PASS",
    worktree: { status: "PASS", expected_digest: sourceManifest.digest, observed_digest: sourceManifest.digest },
    materialized: { status: "PASS", expected_digest: sourceManifest.digest, observed_digest: sourceManifest.digest },
  };
  const planCore = {
    schema_version: 2,
    track: "dev",
    goal: "dev.default",
    client: "macos",
    runtime_profile: "release",
    runtime_profile_digest: "runtime-release-a",
    config_digests: configDigests,
    config_bundle_digest: "config-bundle-a",
    resume: "auto",
    source: { available: true, base_commit: "a".repeat(40), branch: "codex/test", worktree_clean: false, snapshot: sourceSnapshot, baseline: { source: "explicit", commit: "b".repeat(40) }, changed_files: [] },
    release_inputs: null,
    lineage: { root: "AUTO", initial_data_root: "TRACK_POLICY", checkpoint_reuse: "CONFIGURED_PER_STAGE" },
    proofs: [proof],
    stages: stages.map((stage) => {
      const provider = stage.id === "real.macos-claude-deepseek-e2e"
        ? "claude-deepseek"
        : stage.id === "real.macos-codex-luna-e2e" ? "codex-luna" : null;
      const plannedGates = stage.gates.length > 0 ? stage.gates : [{
        id: "det.unit",
        gate_identity: "gate-identity-a",
        definition_digest: "gate-definition-a",
        evidence_contract: null,
        runtime_profile: "python-test",
        runtime_profile_digest: "runtime-python-a",
      }];
      return {
        id: stage.id,
        producer_identity: stage.producer_identity,
        proof_identity: stage.proof_identity,
        performance_identity: stage.performance_identity,
        decision: stage.result_source === "REUSED" ? "REUSE" : "RUN",
        invocation_caps: provider === null ? [] : providerPlanStage(provider).invocation_caps,
        gates: plannedGates.map((gate) => ({
          id: gate.id,
          gate_identity: gate.gate_identity,
          definition_digest: gate.definition_digest,
          evidence_contract: gate.evidence_contract,
          required_evidence: [],
          runtime_profile: gate.runtime_profile,
          runtime_profile_digest: gate.runtime_profile_digest,
        })),
      };
    }),
    admission,
    retry: { recommendation: "RUN", reason: null, previous_run_id: null, stage_id: null, previous_code: null },
    intent: { reason: null, hypothesis: null, expected_evidence: null },
    budget: { estimated_tokens: 0, sum_of_per_invocation_caps_usd: 0, cumulative_spending_cap: null, per_invocation_hard_enforced: true },
    policies: { status: STATUS_POLICY },
  };
  const planFingerprint = sha256Bytes(canonicalJson(planCore));
  writeJsonSync(path.join(attemptRoot, "payload", "run-plan.json"), {
    ...planCore,
    plan_fingerprint: planFingerprint,
    run_id: runId,
    created_at_utc: "2026-08-10T00:00:00.000Z",
  });
  writeJsonSync(path.join(attemptRoot, "payload", "source", "source-snapshot.json"), sourceManifest);
  writeJsonSync(path.join(attemptRoot, "payload", "source", "source-snapshot-verification.json"), sourceVerification);
  return buildRunCandidate({
    attemptRoot,
    runId,
    plan: { ...planCore, plan_fingerprint: planFingerprint, run_id: runId },
    stageResults: stages,
    operationStatus: "PASS",
    sourceSnapshotVerification: sourceVerification,
    preFinalizationResourceReceipt: null,
  });
}

async function createFinalized(evidenceRoot, runId, {
  reusedFrom = null,
  evidenceV2Failure = false,
  certificationTarget = "P1",
  includeFailureEvidence = true,
  ordinaryBefore = null,
  p1PosthocBudgetFailure = false,
  dropProviderInvocations = false,
  resourceStatus = "PASS",
  mutateBeforeFinalize = null,
} = {}) {
  const attemptRoot = createAttempt({ evidenceRoot, runId });
  closeMinimalStream(attemptRoot, runId);
  const stage = reusedFrom
    ? writeReusedStage(attemptRoot, reusedFrom.runId, reusedFrom.stageDigest)
    : evidenceV2Failure
      ? await writeEvidenceV2FailureStage(attemptRoot, {
        certificationTarget,
        includeEvidence: includeFailureEvidence,
        p1PosthocBudgetFailure,
        dropProviderInvocations,
      })
      : writeExecutedStage(attemptRoot);
  const stages = ordinaryBefore === null ? [stage] : [writeOrdinaryStage(attemptRoot, ordinaryBefore), stage];
  const candidate = writePlanAndBuildCandidate(attemptRoot, runId, stages);
  mutateBeforeFinalize?.({ attemptRoot, candidate, stage, stages });
  const verdict = await finalizeAttempt({
    attemptRoot,
    candidate,
    policy: { evidence: { scanner_version: "test-flow-secret-scan-v2", event_visibility_seconds: 0 } },
    resourcePolicy: async ({ preserve }) => ({
      schema_version: 2,
      status: resourceStatus,
      policy: preserve ? "PRESERVE" : "DELETE",
      ...(resourceStatus === "PASS" ? {} : { code: "CLEANUP_PARTIAL" }),
      inspected: [],
      remaining: resourceStatus === "PASS" ? [] : [{ kind: "volume", name: "kept" }],
    }),
  });
  return { attemptRoot, verdict, stage };
}

function assertProviderRuntimeCausality({ attemptRoot, provider, runtimeReceipt, gateReceipt }) {
  const roleReceipt = JSON.parse(fs.readFileSync(path.join(
    attemptRoot,
    "payload", "model-usage", `${provider}-e2e`, "specialist-primary.json",
  ), "utf8"));
  assert.equal(runtimeReceipt.model_invocations, 1);
  assert.equal(runtimeReceipt.role_attempts.length, 1);
  assert.equal(gateReceipt.model_invocations.length, 1);
  assert.equal(roleReceipt.role, runtimeReceipt.role_attempts[0].role);
  assert.equal(roleReceipt.evaluation_attempt ?? roleReceipt.attempt, runtimeReceipt.role_attempts[0].attempt);
  assert.equal(roleReceipt.workflow, gateReceipt.model_invocations[0].workflow);
  assert.equal(roleReceipt.prompt.sha256, runtimeReceipt.role_attempts[0].prompt.sha256);
  assert.equal(roleReceipt.prompt.size ?? roleReceipt.prompt.utf8_size, runtimeReceipt.role_attempts[0].prompt.size);
  assert.equal(roleReceipt.invocation_id, gateReceipt.model_invocations[0].invocation_id);
  return roleReceipt;
}

test("a fully sealed v2 candidate verifies and binds plan, Proof, Stage and Gate receipts", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-evidence-valid-"));
  try {
    const result = await createFinalized(root, "run-20260810T000000Z-aaaaaaaa");
    assert.equal(result.verdict.overall, "PASS");
    assert.equal(result.verdict.verification_status, "PASS");
    assert.equal(result.verdict.evidence_reusable, true);
    assert.equal(result.verdict.failure_diagnostic, null);
    assert.equal(result.verdict.proofs[0].status, "PASS");
    assert.equal(verifyVerdict(result.attemptRoot).status, "PASS");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("cleanup failure commits overall ERROR and cannot remain reusable", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-evidence-cleanup-"));
  try {
    const result = await createFinalized(root, "run-20260810T000001Z-bbbbbbbb", { resourceStatus: "ERROR" });
    assert.equal(result.verdict.functional_status, "PASS");
    assert.equal(result.verdict.operation_status, "ERROR");
    assert.equal(result.verdict.overall, "ERROR");
    assert.equal(result.verdict.evidence_reusable, false);
    assert.equal(result.verdict.failure_diagnostic, null);
    assert.equal(verifyVerdict(result.attemptRoot).status, "PASS");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("production driver discovery does not skip Release Linux without framework env injection", () => {
  const sealed = "/opt/venvs/xiaodao/bin/python";
  assert.equal(productionDriverCandidate({ TEST_FLOW_PYTHON: "/formal/python", TEST_FLOW_QUICK_PYTHON: "/quick/python" }, "linux", () => true), "/formal/python");
  assert.equal(productionDriverCandidate({ TEST_FLOW_QUICK_PYTHON: "/quick/python" }, "linux", () => true), "/quick/python");
  assert.equal(productionDriverCandidate({}, "linux", (candidate) => candidate === sealed), sealed);
  assert.equal(productionDriverCandidate({}, "darwin", () => false), null);
  if (process.platform === "linux") assert.equal(PRODUCTION_DRIVER_SKIP, false);
});

test("a verified Evidence V2 terminal failure is directly visible in the authoritative verdict", { skip: PRODUCTION_DRIVER_SKIP }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-failure-diagnostic-"));
  try {
    const result = await createFinalized(root, "run-20260810T000001Z-d1a60001", { evidenceV2Failure: true });
    assert.equal(result.verdict.overall, "FAIL", fs.readFileSync(path.join(result.attemptRoot, "payload", "receipt-audit.json"), "utf8"));
    assert.equal(result.verdict.functional_status, "FAIL");
    assert.equal(result.verdict.failure_domain, "CONTRACT");
    assert.match(result.verdict.failure_fingerprint, /SPECIALIST_MODEL_EXECUTION_FAILED/u);
    const diagnostic = result.verdict.failure_diagnostic;
    assert.notEqual(diagnostic, null, fs.readFileSync(path.join(result.attemptRoot, result.verdict.gates[0].receipt_path), "utf8"));
    const gateReceipt = JSON.parse(fs.readFileSync(path.join(result.attemptRoot, result.verdict.gates[0].receipt_path), "utf8"));
    const runtimeReceipt = JSON.parse(fs.readFileSync(path.join(path.dirname(path.join(result.attemptRoot, result.verdict.gates[0].receipt_path)), "runtime-receipt.json"), "utf8"));
    const providerFailure = gateReceipt.model_invocations.at(-1);
    assertProviderRuntimeCausality({
      attemptRoot: result.attemptRoot,
      provider: "claude-deepseek",
      runtimeReceipt,
      gateReceipt,
    });
    assert.equal(runtimeReceipt.execution_mode, "real-model");
    assert.equal(gateReceipt.model_invocations.length, runtimeReceipt.model_invocations);
    assert.equal(gateReceipt.model_invocations.length, gateReceipt.assertions.adapter.model_calls);
    assert.equal(providerFailure.wrapper_outcome.status, "FAIL");
    assert.equal(providerFailure.terminal.is_error, true);
    assert.deepEqual(Object.fromEntries(Object.entries(diagnostic).filter(([key]) => !key.startsWith("provider_"))), {
      schema_version: 1,
      certification_target: "P1",
      code: "SPECIALIST_MODEL_EXECUTION_FAILED",
      reason_code: "SPECIALIST_MODEL_EXECUTION_FAILED",
      reason: "Specialist 评估未能完成。",
      diagnostic_id: runtimeReceipt.methods_result.diagnostic_id,
      evaluation_ref: null,
    });
    assert.equal(diagnostic.provider_code, providerFailure.wrapper_outcome.code);
    assert.equal(diagnostic.provider_subtype, providerFailure.terminal.subtype);
    assert.equal(validFailureDiagnostic(diagnostic), true);
    assert.equal(verifyVerdict(result.attemptRoot).status, "PASS");

    const verdictPath = path.join(result.attemptRoot, "verdict.json");
    const original = JSON.parse(fs.readFileSync(verdictPath, "utf8"));
    const changed = structuredClone(original);
    changed.failure_diagnostic.diagnostic_id = `diag-${"b".repeat(64)}`;
    fs.writeFileSync(verdictPath, canonicalJson(changed), "utf8");
    assert.equal(verifyVerdict(result.attemptRoot).status, "INVALID");
    fs.writeFileSync(verdictPath, canonicalJson(original), "utf8");
    const providerChanged = structuredClone(original);
    providerChanged.failure_diagnostic.provider_code = original.failure_diagnostic.provider_code === "CLAUDE_DEEPSEEK_PROCESS_FAILED"
      ? "CLAUDE_DEEPSEEK_MAX_BUDGET_EXCEEDED"
      : "CLAUDE_DEEPSEEK_PROCESS_FAILED";
    fs.writeFileSync(verdictPath, canonicalJson(providerChanged), "utf8");
    assert.equal(verifyVerdict(result.attemptRoot).status, "INVALID");
    fs.writeFileSync(verdictPath, canonicalJson(original), "utf8");
    const subtypeChanged = structuredClone(original);
    subtypeChanged.failure_diagnostic.provider_subtype = original.failure_diagnostic.provider_subtype === "different_error"
      ? "another_error"
      : "different_error";
    fs.writeFileSync(verdictPath, canonicalJson(subtypeChanged), "utf8");
    assert.equal(verifyVerdict(result.attemptRoot).status, "INVALID");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("a closed provider success remains visible when the wrapper rejects post-hoc budget", { skip: PRODUCTION_DRIVER_SKIP }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-posthoc-provider-diagnostic-"));
  try {
    const result = await createFinalized(root, "run-20260810T000001Z-d1a60008", {
      evidenceV2Failure: true,
      p1PosthocBudgetFailure: true,
    });
    const gateReceipt = JSON.parse(fs.readFileSync(path.join(result.attemptRoot, result.verdict.gates[0].receipt_path), "utf8"));
    const runtimeReceipt = JSON.parse(fs.readFileSync(path.join(path.dirname(path.join(result.attemptRoot, result.verdict.gates[0].receipt_path)), "runtime-receipt.json"), "utf8"));
    const invocation = gateReceipt.model_invocations.at(-1);
    assertProviderRuntimeCausality({
      attemptRoot: result.attemptRoot,
      provider: "claude-deepseek",
      runtimeReceipt,
      gateReceipt,
    });
    assert.deepEqual(invocation.terminal, {
      subtype: "success",
      is_error: false,
      stop_reason: "end_turn",
      exit_code: 0,
      signal: null,
    });
    assert.equal(invocation.wrapper_outcome.schema_version, 1);
    assert.equal(invocation.wrapper_outcome.status, "FAIL");
    assert.match(invocation.wrapper_outcome.code, /^(?:CLAUDE_DEEPSEEK_)?CALL_BUDGET_EXCEEDED$/u);
    assert.equal(result.verdict.failure_diagnostic?.provider_code, invocation.wrapper_outcome.code);
    assert.equal(result.verdict.failure_diagnostic?.provider_subtype, "success");
    assert.equal(result.verdict.overall, "FAIL");
    assert.equal(verifyVerdict(result.attemptRoot).status, "PASS");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("a verified P2 terminal failure projects the same public diagnostic contract", { skip: PRODUCTION_DRIVER_SKIP }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-p2-failure-diagnostic-"));
  try {
    const result = await createFinalized(root, "run-20260810T000001Z-d1a60003", {
      evidenceV2Failure: true,
      certificationTarget: "P2",
    });
    assert.equal(result.verdict.overall, "FAIL");
    assert.equal(result.verdict.failure_diagnostic?.certification_target, "P2", fs.readFileSync(path.join(result.attemptRoot, result.verdict.gates[0].receipt_path), "utf8"));
    assert.equal(result.verdict.failure_diagnostic.reason_code, "SPECIALIST_MODEL_EXECUTION_FAILED");
    assert.match(result.verdict.failure_diagnostic.diagnostic_id, /^diag-[a-f0-9]{64}$/u);
    const gateReceipt = JSON.parse(fs.readFileSync(path.join(result.attemptRoot, result.verdict.gates[0].receipt_path), "utf8"));
    const runtimeReceipt = JSON.parse(fs.readFileSync(path.join(path.dirname(path.join(result.attemptRoot, result.verdict.gates[0].receipt_path)), "runtime-receipt.json"), "utf8"));
    const providerFailure = gateReceipt.model_invocations.at(-1);
    assertProviderRuntimeCausality({
      attemptRoot: result.attemptRoot,
      provider: "codex-luna",
      runtimeReceipt,
      gateReceipt,
    });
    assert.equal(gateReceipt.model_invocations.length, 1);
    assert.equal(gateReceipt.model_invocations.length, runtimeReceipt.model_invocations);
    assert.equal(gateReceipt.model_invocations.length, gateReceipt.assertions.adapter.model_calls);
    assert.equal(providerFailure.effective_model, "gpt-5.6-luna");
    assert.equal(providerFailure.wrapper_outcome.status, "FAIL");
    assert.equal(result.verdict.failure_diagnostic.provider_code, providerFailure.wrapper_outcome.code);
    assert.equal(result.verdict.failure_diagnostic.provider_subtype, providerFailure.terminal.subtype);
    assert.equal(verifyVerdict(result.attemptRoot).status, "PASS", JSON.stringify(verifyVerdict(result.attemptRoot)));
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("a P2 Gate cannot project a diagnostic after dropping its production provider invocation", { skip: PRODUCTION_DRIVER_SKIP }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-p2-invocation-mismatch-"));
  try {
    const result = await createFinalized(root, "run-20260810T000001Z-d1a60009", {
      evidenceV2Failure: true,
      certificationTarget: "P2",
      dropProviderInvocations: true,
    });
    const gateReceipt = JSON.parse(fs.readFileSync(path.join(result.attemptRoot, result.verdict.gates[0].receipt_path), "utf8"));
    assert.equal(gateReceipt.assertions.adapter.model_calls, 1);
    assert.equal(gateReceipt.model_invocations.length, 0);
    assert.equal(result.verdict.failure_diagnostic, null);
    assert.equal(verifyVerdict(result.attemptRoot).status, "PASS");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("legacy adapter files outside the current Gate evidence index cannot populate the diagnostic", { skip: PRODUCTION_DRIVER_SKIP }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-unlisted-failure-diagnostic-"));
  try {
    for (const [index, certificationTarget] of ["P1", "P2"].entries()) {
      const result = await createFinalized(root, `run-20260810T000001Z-d1a6001${index}`, {
        evidenceV2Failure: true,
        certificationTarget,
        includeFailureEvidence: false,
      });
      const stageId = certificationTarget === "P1" ? "real.macos-claude-deepseek-e2e" : "real.macos-codex-luna-e2e";
      const gateRoot = path.join(result.attemptRoot, "payload", "stages", stageId, "gates", stageId);
      assert.equal(fs.existsSync(path.join(gateRoot, "adapter-receipt.json")), true);
      assert.equal(fs.existsSync(path.join(gateRoot, "runtime-receipt.json")), true);
      assert.deepEqual(result.verdict.gates[0].evidence, []);
      assert.equal(result.verdict.failure_diagnostic, null);
      assert.equal(verifyVerdict(result.attemptRoot).status, "PASS", JSON.stringify(verifyVerdict(result.attemptRoot)));
    }
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("an earlier ordinary failure prevents a later provider diagnostic from becoming authoritative", { skip: PRODUCTION_DRIVER_SKIP }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-earlier-failure-diagnostic-"));
  try {
    for (const [index, certificationTarget] of ["P1", "P2"].entries()) {
      const result = await createFinalized(root, `run-20260810T000001Z-d1a6002${index}`, {
        evidenceV2Failure: true,
        certificationTarget,
        ordinaryBefore: "FAIL",
      });
      assert.equal(result.verdict.stages[0].code, "ORDINARY_PRODUCT_FAILURE");
      assert.equal(result.verdict.failure_diagnostic, null);
      assert.match(result.verdict.failure_fingerprint, /ORDINARY_PRODUCT_FAILURE/u);
      assert.equal(verifyVerdict(result.attemptRoot).status, "PASS");
    }
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("stage order and Gate digest drift clear the diagnostic and fail receipt verification", { skip: PRODUCTION_DRIVER_SKIP }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-failure-diagnostic-drift-"));
  try {
    const reordered = await createFinalized(root, "run-20260810T000001Z-d1a60006", {
      evidenceV2Failure: true,
      ordinaryBefore: "PASS",
      mutateBeforeFinalize: ({ candidate }) => { candidate.stages.reverse(); },
    });
    assert.equal(reordered.verdict.failure_diagnostic, null);
    assert.equal(reordered.verdict.verification_status, "FAIL");
    assert.equal(verifyVerdict(reordered.attemptRoot).status, "INVALID");

    const digestDrift = await createFinalized(root, "run-20260810T000001Z-d1a60007", {
      evidenceV2Failure: true,
      mutateBeforeFinalize: ({ candidate }) => { candidate.stages[0].gates[0].receipt_digest = "f".repeat(64); },
    });
    assert.equal(digestDrift.verdict.failure_diagnostic, null);
    assert.equal(digestDrift.verdict.verification_status, "FAIL");
    assert.equal(verifyVerdict(digestDrift.attemptRoot).status, "INVALID");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("an unverified Evidence V2 adapter/runtime pair cannot populate the verdict diagnostic", { skip: PRODUCTION_DRIVER_SKIP }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-unverified-failure-diagnostic-"));
  try {
    const result = await createFinalized(root, "run-20260810T000001Z-d1a60002", {
      evidenceV2Failure: true,
      mutateBeforeFinalize: ({ attemptRoot }) => {
        const runtimePath = path.join(
          attemptRoot,
          "payload", "stages", "real.macos-claude-deepseek-e2e", "gates",
          "real.macos-claude-deepseek-e2e", "runtime-receipt.json",
        );
        const runtime = JSON.parse(fs.readFileSync(runtimePath, "utf8"));
        runtime.methods_result.diagnostic_id = `diag-${"c".repeat(64)}`;
        fs.writeFileSync(runtimePath, canonicalJson(runtime), "utf8");
      },
    });
    assert.equal(result.verdict.overall, "ERROR");
    assert.equal(result.verdict.verification_status, "FAIL");
    assert.equal(result.verdict.failure_diagnostic, null);
    assert.equal(verifyVerdict(result.attemptRoot).status, "INVALID");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("the failure diagnostic JSON Schema and runtime validator close the same fields", () => {
  const schema = JSON.parse(fs.readFileSync(path.join(TOOL_ROOT, "schemas", "failure-diagnostic.schema.json"), "utf8"));
  assert.deepEqual(schema.type, ["object", "null"]);
  assert.equal(schema.additionalProperties, false);
  assert.deepEqual([...schema.required].sort(), [...FAILURE_DIAGNOSTIC_FIELDS].sort());
  assert.deepEqual(Object.keys(schema.properties).sort(), [...FAILURE_DIAGNOSTIC_FIELDS].sort());
  assert.equal(schema.properties.certification_target.enum.join(","), "P1,P2");
  assert.equal(schema.properties.evaluation_ref.oneOf.some((item) => item.type === "null"), true);
  const valid = {
    schema_version: 1,
    certification_target: "P1",
    code: "SPECIALIST_MODEL_EXECUTION_FAILED",
    reason_code: "SPECIALIST_MODEL_EXECUTION_FAILED",
    reason: "Specialist 评估未能完成。",
    diagnostic_id: `diag-${"a".repeat(64)}`,
    evaluation_ref: null,
    provider_code: "CLAUDE_DEEPSEEK_PROCESS_FAILED",
    provider_subtype: "error",
  };
  assert.equal(validFailureDiagnostic(valid), true);
  assert.equal(validFailureDiagnostic({ ...valid, provider_code: null }), false);
  assert.equal(validFailureDiagnostic({ ...valid, provider_subtype: null }), false);
  assert.equal(validFailureDiagnostic({ ...valid, provider_code: null, provider_subtype: null }), true);
  assert.equal(projectCandidateFailureDiagnostic({
    attemptRoot: os.tmpdir(),
    stages: [{
      id: "deterministic.full",
      status: "FAIL",
      performance_status: "NOT_MEASURED",
      gates: [{ id: "det.unit", status: "FAIL", failure_domain: "PRODUCT" }],
    }],
  }), null);
});

test("secret evidence is preserved but never reusable", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-secret-"));
  try {
    let preserved = false;
    const attemptRoot = createAttempt({ evidenceRoot: root, runId: "run-20260810T000002Z-cccccccc" });
    closeMinimalStream(attemptRoot, path.basename(attemptRoot));
    const stage = writeExecutedStage(attemptRoot);
    const candidate = writePlanAndBuildCandidate(attemptRoot, path.basename(attemptRoot), stage);
    fs.writeFileSync(path.join(attemptRoot, "payload", "logs", "leak.log"), "sk-ant-abcdefghijklmnopqrstuv\n");
    const verdict = await finalizeAttempt({
      attemptRoot,
      candidate,
      resourcePolicy: async ({ preserve }) => {
        preserved = preserve;
        return { schema_version: 2, status: "PASS", policy: "PRESERVE", inspected: [], remaining: [] };
      },
    });
    assert.equal(preserved, true);
    assert.equal(verdict.overall, "ERROR");
    assert.equal(verdict.failure_domain, "SECURITY");
    assert.equal(verdict.evidence_reusable, false);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("payload and verdict-only tampering are both detected", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-tamper-"));
  try {
    const first = await createFinalized(root, "run-20260810T000003Z-dddddddd");
    fs.appendFileSync(path.join(first.attemptRoot, "payload", "candidate-result.json"), " ");
    assert.equal(verifyVerdict(first.attemptRoot).status, "INVALID");

    for (const [index, mutate] of [
      (value) => { value.overall = "FAIL"; },
      (value) => { value.evidence_reusable = false; },
      (value) => { value.stages[0].producer_identity = "tampered"; },
      (value) => { value.source.snapshot.digest = "f".repeat(64); },
    ].entries()) {
      const item = await createFinalized(root, `run-20260810T00000${4 + index}Z-${String(index + 1).repeat(8)}`);
      const verdictPath = path.join(item.attemptRoot, "verdict.json");
      const value = JSON.parse(fs.readFileSync(verdictPath, "utf8"));
      mutate(value);
      fs.writeFileSync(verdictPath, canonicalJson(value), "utf8");
      assert.equal(verifyVerdict(item.attemptRoot).status, "INVALID");
    }
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("an incomplete event stream produces a verifiable ERROR verdict instead of crashing finalization", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-event-error-"));
  try {
    const result = await createFinalized(root, "run-20260810T000010Z-eeeeeeee", {
      mutateBeforeFinalize: ({ attemptRoot }) => fs.appendFileSync(path.join(attemptRoot, "payload", "events", "orchestrator.ndjson"), "{"),
    });
    assert.equal(result.verdict.overall, "ERROR");
    assert.equal(result.verdict.failure_domain, "HARNESS");
    assert.equal(result.verdict.verification_status, "FAIL");
    assert.equal(verifyVerdict(result.attemptRoot).status, "PASS");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("missing verdict is UNFINALIZED, never a successful attempt", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-unfinalized-"));
  try {
    const attemptRoot = createAttempt({ evidenceRoot: root, runId: "run-20260810T000011Z-ffffffff" });
    assert.deepEqual(verifyVerdict(attemptRoot), { status: "UNFINALIZED", verdict: null });
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("reuse points only to the original executed receipt and breaks if the source disappears", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-reuse-source-"));
  try {
    const source = await createFinalized(root, "run-20260810T000012Z-aaaabbbb");
    const derived = await createFinalized(root, "run-20260810T000013Z-ccccdddd", {
      reusedFrom: { runId: source.verdict.run_id, stageDigest: source.stage.stage_receipt_digest },
    });
    assert.equal(source.verdict.failure_diagnostic, null);
    assert.equal(derived.verdict.failure_diagnostic, null);
    assert.equal(verifyVerdict(derived.attemptRoot).status, "PASS");

    const chained = await createFinalized(root, "run-20260810T000014Z-eeeeffff", {
      reusedFrom: { runId: derived.verdict.run_id, stageDigest: derived.stage.stage_receipt_digest },
    });
    assert.equal(verifyVerdict(chained.attemptRoot).reason, "REUSE_SOURCE_STAGE_INVALID");

    fs.rmSync(source.attemptRoot, { recursive: true, force: true });
    assert.equal(verifyVerdict(derived.attemptRoot).reason, "REUSE_SOURCE_INVALID");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("prune refuses to delete a source run that a valid derived verdict references", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-prune-source-"));
  try {
    const source = await createFinalized(root, "run-20260810T000015Z-1234abcd");
    await createFinalized(root, "run-20260810T000016Z-5678efab", {
      reusedFrom: { runId: source.verdict.run_id, stageDigest: source.stage.stage_receipt_digest },
    });
    const command = spawnSync(process.execPath, [path.join(TOOL_ROOT, "evidence.mjs"), "prune", "--evidence-root", root, "--run-id", source.verdict.run_id, "--execute"], { encoding: "utf8" });
    assert.equal(command.status, 3);
    assert.match(command.stderr, /PRUNE_REFERENCED_SOURCE/);
    assert.equal(fs.existsSync(source.attemptRoot), true);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("evidence report filters exact run ids and never labels an invalid verdict reusable", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-report-validity-"));
  try {
    await createFinalized(root, "run-20260810T000017Z-1111aaaa");
    const invalid = await createFinalized(root, "run-20260810T000018Z-2222bbbb");
    const verdictPath = path.join(invalid.attemptRoot, "verdict.json");
    const verdict = JSON.parse(fs.readFileSync(verdictPath, "utf8"));
    verdict.overall = "FAIL";
    fs.writeFileSync(verdictPath, canonicalJson(verdict), "utf8");

    const command = spawnSync(process.execPath, [path.join(TOOL_ROOT, "evidence.mjs"), "report", "--evidence-root", root, "--run-id", verdict.run_id], { encoding: "utf8" });
    assert.equal(command.status, 0, command.stderr);
    const report = JSON.parse(command.stdout);
    assert.equal(report.attempt_count, 1);
    assert.deepEqual(report.attempts.map((item) => item.run_id), [verdict.run_id]);
    assert.equal(report.attempts[0].verification_status, "INVALID");
    assert.equal(report.attempts[0].evidence_reusable, false);
    assert.equal(report.attempts[0].retention, "MANUAL_REVIEW");
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test("event requirements are derived from executed Gate evidence contracts", () => {
  assert.deepEqual(requiredEventFiles([{ id: "det.unit", status: "PASS", result_source: "EXECUTED" }]), ["orchestrator.ndjson"]);
  const routeContract = { id: "cross-job-tool-call-v2", event_stream: { instance: "route", pass_requires: ["diagnostics", "journey"], pass_allows_empty: [], failure_allows_empty: ["journey"] } };
  const routeGate = { stage_id: "journey.cross-job.route", id: "journey.route", evidence_contract: routeContract, status: "PASS", result_source: "EXECUTED" };
  assert.deepEqual(requiredEventFiles([routeGate]), [
    "orchestrator.ndjson",
    "parts/service-linux.route.diagnostics.ndjson",
    "parts/service-linux.route.journey.ndjson",
  ]);
  assert.deepEqual(allowedEmptyEventFiles([{ ...routeGate, status: "FAIL" }]), ["parts/service-linux.route.journey.ndjson"]);
  assert.deepEqual(requiredEventFiles([{ ...routeGate, status: "FAIL" }]), ["orchestrator.ndjson"]);
  const environmentGate = { stage_id: "journey.cross-job.environment", id: "journey.environment", evidence_contract: { id: "cross-job-environment-v3", event_stream: { ...routeContract.event_stream, pass_allows_empty: ["journey"] } }, status: "PASS", result_source: "EXECUTED" };
  assert.deepEqual(allowedEmptyEventFiles([environmentGate]), ["parts/service-linux.route.journey.ndjson"]);
  assert.deepEqual(allowedEmptyEventFiles([environmentGate, routeGate]), []);
});

test("failed adapter progress recovers completed calls and authoritative usage", () => {
  const attemptRoot = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-stage-progress-"));
  try {
    const stageId = "journey.cross-job.route";
    const stageRoot = path.join(attemptRoot, "payload", "stages", stageId);
    fs.mkdirSync(stageRoot, { recursive: true });
    writeJsonSync(path.join(stageRoot, "phase1.authoritative.json"), {
      records: [{ tool_name: "problem_locator_create_case" }, { tool_name: "problem_locator_get_case" }],
      usage: {
        schema_version: 1,
        input_tokens: 100,
        output_tokens: 20,
        cache_creation_input_tokens: 30,
        cache_read_input_tokens: 40,
        total_tokens: 190,
        cost_usd: 0.125,
      },
    });
    writeJsonSync(path.join(attemptRoot, "payload", "service-route-supervisor.json"), { status: "PASS" });
    const writer = new EventWriter({ attemptRoot, runId: "run-progress", producerId: "service-linux-route-diagnostics", producerType: "service" });
    writer.write("mcp.tool.completed", { data: { tool: "problem_locator_create_case", ok: true } });
    writer.write("mcp.tool.completed", { data: { tool: "problem_locator_get_case", ok: true } });
    writer.close();
    const parts = path.join(attemptRoot, "payload", "events", "parts");
    fs.mkdirSync(parts, { recursive: true });
    fs.renameSync(writer.filePath, path.join(parts, "service-linux.route.diagnostics.ndjson"));
    assert.deepEqual(recoverStageAuditProgress({ attemptRoot, stageRoot, stageId }), {
      client_tool_calls: 2,
      server_tool_calls: 2,
      usage: {
        schema_version: 1,
        input_tokens: 100,
        output_tokens: 20,
        cache_creation_input_tokens: 30,
        cache_read_input_tokens: 40,
        total_tokens: 190,
        cost_usd: 0.125,
      },
    });
  } finally { fs.rmSync(attemptRoot, { recursive: true, force: true }); }
});

test("attempt-scoped cleanup removes nested read-only scratch trees", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-cleanup-"));
  try {
    const scratch = path.join(root, "scratch");
    const nested = path.join(scratch, "workspace", "inputs", "tree");
    fs.mkdirSync(nested, { recursive: true });
    fs.writeFileSync(path.join(nested, "payload.txt"), "immutable\n");
    fs.chmodSync(nested, 0o500);
    fs.chmodSync(path.dirname(nested), 0o500);
    removeTreeWritable(scratch, root);
    assert.equal(fs.existsSync(scratch), false);
  } finally {
    if (fs.existsSync(root)) {
      fs.chmodSync(root, 0o700);
      fs.rmSync(root, { recursive: true, force: true });
    }
  }
});
