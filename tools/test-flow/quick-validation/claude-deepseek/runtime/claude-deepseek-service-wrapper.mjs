#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { canonicalJson, sha256Bytes } from "../../../lib/util.mjs";
import {
  aggregateClaudeUsage,
  CLAUDE_DEEPSEEK_CALL_WALL_SECONDS,
  CLAUDE_DEEPSEEK_E2E_MAX_TURNS,
  CLAUDE_DEEPSEEK_E2E_USD_LIMIT,
  CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS,
  CLAUDE_DEEPSEEK_MODEL_CERT_BUDGET_ENFORCEMENT,
  CLAUDE_DEEPSEEK_MODEL_CERT_PLAN_CAPS,
  CLAUDE_DEEPSEEK_MODEL_CERT_ROLE_POOL_USD,
  CLAUDE_DEEPSEEK_MODEL,
  CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
  validateClaudeDeepseekRoleReceipt,
} from "./claude-deepseek-contract.mjs";
import { runClaudeProcess } from "./claude-deepseek-process.mjs";

const MODULE_PATH = fileURLToPath(import.meta.url);
const MAX_PROMPT_BYTES = 4 * 1024 * 1024;
const ROLE_MARKER = "<<<METHODS_EVIDENCE_V2_ROLE>>>";
const ROLE_END_MARKER = "<<<END METHODS_EVIDENCE_V2_ROLE>>>";
const ROLE_SPEC = Object.freeze({
  SPECIALIST: Object.freeze({
    promptRole: "Specialist",
    output: "output/method-diagnosis.draft.json",
  }),
  REVIEWER: Object.freeze({
    promptRole: "Reviewer",
    output: "output/method-review.draft.json",
  }),
});

class ServiceWrapperError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "ServiceWrapperError";
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) {
  throw new ServiceWrapperError(code, message, details);
}

function requireWrapper(condition, code, message, details = {}) {
  if (!condition) fail(code, message, details);
}

export function parseArguments(argv) {
  const values = {};
  const names = new Set([
    "claude-entry",
    "settings",
    "config-root",
    "private-root",
    "evidence-root",
    "usage-root",
    "run-id",
  ]);
  for (let index = 0; index < argv.length; index += 2) {
    const argument = argv[index];
    if (!argument?.startsWith("--") || index + 1 >= argv.length || argv[index + 1].startsWith("--")) {
      fail("CLAUDE_DEEPSEEK_SERVICE_ARGUMENT_INVALID", "Model-cert wrapper arguments must use --name value pairs");
    }
    const name = argument.slice(2);
    if (!names.has(name)) fail("CLAUDE_DEEPSEEK_SERVICE_ARGUMENT_UNKNOWN", "Model-cert wrapper received an unsupported argument");
    if (Object.hasOwn(values, name)) fail("CLAUDE_DEEPSEEK_SERVICE_ARGUMENT_DUPLICATE", "Model-cert wrapper argument is duplicated");
    values[name] = argv[index + 1];
  }
  if (![...names].every((name) => typeof values[name] === "string" && values[name])) {
    fail("CLAUDE_DEEPSEEK_SERVICE_ARGUMENT_MISSING", "Model-cert wrapper arguments are incomplete");
  }
  return values;
}

async function readPrompt(stream) {
  const chunks = [];
  let size = 0;
  for await (const chunk of stream) {
    size += chunk.length;
    if (size > MAX_PROMPT_BYTES) fail("CLAUDE_DEEPSEEK_SERVICE_PROMPT_LIMIT", "Evidence V2 role prompt exceeds the wrapper byte cap");
    chunks.push(Buffer.from(chunk));
  }
  try {
    const prompt = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks));
    if (!prompt.trim()) fail("CLAUDE_DEEPSEEK_SERVICE_PROMPT_EMPTY", "Evidence V2 role prompt is empty");
    return prompt;
  } catch (error) {
    if (error instanceof ServiceWrapperError) throw error;
    fail("CLAUDE_DEEPSEEK_SERVICE_PROMPT_UTF8", "Evidence V2 role prompt must be UTF-8");
  }
}

export function parseMethodsRolePrompt(prompt) {
  requireWrapper(typeof prompt === "string" && prompt.length > 0, "CLAUDE_DEEPSEEK_ROLE_PROMPT_INVALID", "Evidence V2 role prompt is invalid");
  requireWrapper(prompt.split(ROLE_MARKER).length === 2 && prompt.split(ROLE_END_MARKER).length === 2, "CLAUDE_DEEPSEEK_ROLE_MARKER_INVALID", "Evidence V2 role marker is missing or duplicated");
  const roleMatch = /Role: (Specialist|Reviewer)\. Attempt: (primary evaluation|only repair)\./u.exec(prompt);
  requireWrapper(roleMatch !== null, "CLAUDE_DEEPSEEK_ROLE_IDENTITY_INVALID", "Evidence V2 role or attempt is not explicit");
  const role = roleMatch[1] === "Specialist" ? "SPECIALIST" : "REVIEWER";
  const attempt = roleMatch[2] === "primary evaluation" ? "PRIMARY" : "REPAIR";
  const expectedOutput = ROLE_SPEC[role].output;
  requireWrapper(prompt.includes(`Write only ${expectedOutput}.`), "CLAUDE_DEEPSEEK_ROLE_OUTPUT_INVALID", "Evidence V2 role prompt does not bind its one output draft");
  requireWrapper(prompt.includes("evaluation_ref, verdict, and reason"), "CLAUDE_DEEPSEEK_ROLE_CONTRACT_INVALID", "Evidence V2 evaluation array contract is missing");
  return Object.freeze({ role, attempt, output: expectedOutput });
}

function writeJsonNew(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, canonicalJson(value), { encoding: "utf8", mode: 0o600, flag: "wx" });
}

function attemptKey(role, attempt) {
  return `${role.toLowerCase()}-${attempt.toLowerCase()}`;
}

function roundedUsd(value) {
  return Math.round(value * 1_000_000) / 1_000_000;
}

function exactKeys(value, keys) {
  return value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && canonicalJson(Object.keys(value).sort()) === canonicalJson([...keys].sort());
}

function normalizedUsageOrNull(value) {
  try { return aggregateClaudeUsage([{ usage: value }]); }
  catch { return null; }
}

function closedProviderTerminal(value) {
  if (!exactKeys(value, ["subtype", "is_error", "stop_reason", "exit_code", "signal"])) return null;
  if (typeof value.subtype !== "string"
    || value.subtype.length === 0
    || typeof value.is_error !== "boolean"
    || (value.stop_reason !== null && typeof value.stop_reason !== "string")
    || (value.exit_code !== null && (!Number.isSafeInteger(value.exit_code) || value.exit_code < 0))
    || (value.signal !== null && (typeof value.signal !== "string" || value.signal.length === 0))) return null;
  return Object.freeze({ ...value });
}

export function roleInvocationBudget(usageRoot, role, attempt) {
  requireWrapper(Object.hasOwn(ROLE_SPEC, role) && ["PRIMARY", "REPAIR"].includes(attempt), "CLAUDE_DEEPSEEK_ROLE_BUDGET_IDENTITY_INVALID", "Evidence V2 role budget identity is invalid");
  let priorCostUsd = 0;
  if (attempt === "REPAIR") {
    const primaryPath = path.join(path.resolve(usageRoot), `${attemptKey(role, "PRIMARY")}.json`);
    requireWrapper(fs.existsSync(primaryPath), "CLAUDE_DEEPSEEK_ROLE_BUDGET_PRIMARY_MISSING", `${role} repair has no archived primary usage`);
    const primary = JSON.parse(fs.readFileSync(primaryPath, "utf8"));
    requireWrapper(
      primary?.role === role
        && primary?.evaluation_attempt === "PRIMARY"
        && primary?.status === "PASS"
        && primary?.usage_complete === true,
      "CLAUDE_DEEPSEEK_ROLE_BUDGET_PRIMARY_INVALID",
      `${role} primary usage receipt is not eligible for repair budgeting`,
    );
    priorCostUsd = aggregateClaudeUsage([primary]).cost_usd;
  }
  const effectiveCallCapUsd = Math.max(0, roundedUsd(CLAUDE_DEEPSEEK_MODEL_CERT_ROLE_POOL_USD - priorCostUsd));
  return Object.freeze({
    schema_version: 1,
    stage_cap_usd: CLAUDE_DEEPSEEK_E2E_USD_LIMIT,
    role,
    role_pool_usd: CLAUDE_DEEPSEEK_MODEL_CERT_ROLE_POOL_USD,
    prior_cost_usd: priorCostUsd,
    effective_call_cap_usd: effectiveCallCapUsd,
    enforcement: CLAUDE_DEEPSEEK_MODEL_CERT_BUDGET_ENFORCEMENT,
  });
}

export function claimRoleAttempt(privateRoot, role, attempt) {
  requireWrapper(Object.hasOwn(ROLE_SPEC, role) && ["PRIMARY", "REPAIR"].includes(attempt), "CLAUDE_DEEPSEEK_ROLE_CLAIM_INVALID", "Evidence V2 role claim is invalid");
  const claimsRoot = path.join(path.resolve(privateRoot), "model-role-claims");
  fs.mkdirSync(claimsRoot, { recursive: true, mode: 0o700 });
  const existing = new Set(fs.readdirSync(claimsRoot, { withFileTypes: true }).filter((item) => item.isDirectory()).map((item) => item.name));
  const primary = attemptKey(role, "PRIMARY");
  const repair = attemptKey(role, "REPAIR");
  if (attempt === "PRIMARY") {
    requireWrapper(!existing.has(primary) && !existing.has(repair), "CLAUDE_DEEPSEEK_ROLE_RETRY_FORBIDDEN", `${role} already consumed its primary model call`);
  } else {
    requireWrapper(existing.has(primary) && !existing.has(repair), "CLAUDE_DEEPSEEK_ROLE_REPAIR_FORBIDDEN", `${role} repair requires exactly one rejected primary and cannot repeat`);
  }
  requireWrapper(existing.size < 4, "CLAUDE_DEEPSEEK_MODEL_CALL_CAP", "Evidence V2 model-cert exceeded its four-call hard cap");
  const claim = path.join(claimsRoot, attemptKey(role, attempt));
  try { fs.mkdirSync(claim, { mode: 0o700 }); }
  catch (error) {
    if (error?.code === "EEXIST") fail("CLAUDE_DEEPSEEK_ROLE_RETRY_FORBIDDEN", `${role} ${attempt} model call is already claimed`);
    throw error;
  }
  return claim;
}

function portablePermissionPath(value) {
  const resolved = path.resolve(value);
  if (/[\r\n*?]/u.test(resolved)) fail("CLAUDE_DEEPSEEK_SERVICE_WORKSPACE_PATH_INVALID", "Job workspace path cannot be represented in a Claude permission rule");
  const drive = /^([A-Za-z]):[\\/](.*)$/u.exec(resolved);
  const portable = drive ? `${drive[1]}/${drive[2].replaceAll("\\", "/")}` : resolved.split(path.sep).join("/").replace(/^\/+/, "");
  return `//${portable.replaceAll("\\", "\\\\").replaceAll("(", "\\(").replaceAll(")", "\\)")}`;
}

export function roleToolPolicy({ workspaceRoot, output }) {
  const inputs = portablePermissionPath(path.join(workspaceRoot, "inputs"));
  const outputFile = portablePermissionPath(path.join(workspaceRoot, ...output.split("/")));
  const policy = {
    schema_version: 1,
    tools: ["Read", "Write"],
    // Claude Code emits file creation as Write, while its permission matcher
    // authorizes that tool with the Edit(path) permission category.
    allowed_tools: [`Read(${inputs}/**)`, `Read(${outputFile})`, `Edit(${outputFile})`],
    readable_scope: "job-workspace-inputs-and-role-draft",
    writable_scope: output,
    network: false,
    shell: false,
    skill_loading: false,
  };
  return Object.freeze({ ...policy, sha256: sha256Bytes(canonicalJson(policy)) });
}

function inside(root, candidate) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return relative !== "" && relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}

function resolveToolPath(workspaceRoot, value) {
  requireWrapper(typeof value === "string" && value.length > 0 && !/[\r\n\0]/u.test(value), "CLAUDE_DEEPSEEK_ROLE_TOOL_PATH_INVALID", "Evidence V2 file-tool path is invalid");
  const portable = value.replaceAll("\\", "/");
  if (/^\/(?:inputs|output)\//u.test(portable)) return path.join(workspaceRoot, ...portable.slice(1).split("/"));
  return path.isAbsolute(value) ? path.resolve(value) : path.resolve(workspaceRoot, value);
}

export function auditRoleWorkspace({ workspaceRoot, roleSpec, processResult }) {
  const inputs = path.join(workspaceRoot, "inputs");
  const requiredInputs = ["request.json", "method-evidence-graph.json", "method-evaluation-plan.json"];
  requireWrapper(requiredInputs.every((name) => fs.existsSync(path.join(inputs, name)) && fs.statSync(path.join(inputs, name)).isFile()), "CLAUDE_DEEPSEEK_ROLE_INPUT_MISSING", "Evidence V2 role workspace is missing request, Graph, or Plan");
  const forbidden = [
    path.join(inputs, "target_logs.json"),
    path.join(inputs, "logparse-receipt.json"),
    path.join(inputs, "attachments"),
    path.join(inputs, "target-logs"),
  ];
  requireWrapper(forbidden.every((target) => !fs.existsSync(target)), "CLAUDE_DEEPSEEK_ROLE_INPUT_LEAK", "Evidence V2 role workspace contains raw Logparse or attachment inputs");
  requireWrapper(Array.isArray(processResult?.records) && processResult.records.length > 0, "CLAUDE_DEEPSEEK_ROLE_TRACE_INVALID", "Evidence V2 role must produce a bounded file-tool trace");
  const outputRoot = path.join(workspaceRoot, "output");
  const expectedOutput = path.join(workspaceRoot, ...roleSpec.output.split("/"));
  let reads = 0;
  const writes = [];
  for (const record of processResult.records) {
    requireWrapper(record?.is_error !== true, "CLAUDE_DEEPSEEK_ROLE_TOOL_FAILED", "Evidence V2 role file tool failed or was denied");
    const target = resolveToolPath(workspaceRoot, record?.input?.file_path);
    if (record.name === "Read" && (inside(inputs, target) || target === expectedOutput)) reads += 1;
    else if (record.name === "Write" && inside(outputRoot, target) && target === expectedOutput && typeof record.input?.content === "string") writes.push(record);
    else fail("CLAUDE_DEEPSEEK_ROLE_TOOL_SCOPE_INVALID", "Evidence V2 role used a tool or path outside its frozen Read/Write policy");
  }
  requireWrapper(writes.length === 1, "CLAUDE_DEEPSEEK_ROLE_WRITE_COUNT_INVALID", "Evidence V2 role must write its draft exactly once");
  requireWrapper(fs.existsSync(expectedOutput) && fs.statSync(expectedOutput).isFile(), "CLAUDE_DEEPSEEK_ROLE_DRAFT_MISSING", "Evidence V2 role output draft is missing");
  const disk = fs.readFileSync(expectedOutput);
  requireWrapper(disk.equals(Buffer.from(writes[0].input.content, "utf8")), "CLAUDE_DEEPSEEK_ROLE_DRAFT_MISMATCH", "Evidence V2 role draft differs from the successful Write input");
  return Object.freeze({
    schema_version: 1,
    status: "PASS",
    role: roleSpec.role,
    attempt: roleSpec.attempt,
    reads,
    writes: 1,
    output_path: roleSpec.output,
    output_size: disk.length,
    output_sha256: sha256Bytes(disk),
    harness_normalized: false,
  });
}

function privateEnvironment(claim) {
  const home = path.join(claim, "home");
  const temporary = path.join(claim, "tmp");
  fs.mkdirSync(home, { recursive: true, mode: 0o700 });
  fs.mkdirSync(temporary, { recursive: true, mode: 0o700 });
  return { home, temporary };
}

export async function runServiceInvocation(values, {
  stdin = process.stdin,
  stdout = process.stdout,
  ambient = process.env,
  runClaude = runClaudeProcess,
} = {}) {
  const workspaceRoot = process.cwd();
  const prompt = await readPrompt(stdin);
  const parsed = parseMethodsRolePrompt(prompt);
  const roleSpec = { ...parsed, role: parsed.role };
  const policy = roleToolPolicy({ workspaceRoot, output: parsed.output });
  const key = attemptKey(parsed.role, parsed.attempt);
  const traceRoot = path.join(path.resolve(values["evidence-root"]), "model-role-invocations");
  fs.mkdirSync(traceRoot, { recursive: true, mode: 0o700 });
  const progressPath = path.join(traceRoot, `${key}.progress`);
  const startedAtUtc = new Date().toISOString();
  let result = null;
  let budget = null;
  let claim = null;
  try {
    claim = claimRoleAttempt(values["private-root"], parsed.role, parsed.attempt);
    budget = roleInvocationBudget(values["usage-root"], parsed.role, parsed.attempt);
    requireWrapper(budget.effective_call_cap_usd > 0, "CLAUDE_DEEPSEEK_ROLE_BUDGET_EXHAUSTED", `${parsed.role} exhausted its model-cert role budget`);
    try { fs.writeFileSync(progressPath, "", { encoding: "utf8", mode: 0o600, flag: "wx" }); }
    catch (error) {
      if (error?.code === "EEXIST") fail("CLAUDE_DEEPSEEK_ROLE_PROGRESS_EXISTS", `${parsed.role} ${parsed.attempt} progress receipt already exists`);
      throw error;
    }
    result = await runClaude({
      claudeEntry: path.resolve(values["claude-entry"]),
      settings: path.resolve(values.settings),
      cwd: workspaceRoot,
      prompt,
      phase: parsed.role,
      invocationId: `${values["run-id"]}:${key}`,
      tools: policy.tools,
      allowedTools: policy.allowed_tools,
      disallowedTools: ["Bash", "Glob", "Grep", "Skill"],
      auditOnlyAllowedTools: ["Bash", "Glob", "Grep", "Skill"],
      maxTurns: CLAUDE_DEEPSEEK_E2E_MAX_TURNS,
      maxBudgetUsd: budget.effective_call_cap_usd,
      wallTimeoutSeconds: CLAUDE_DEEPSEEK_CALL_WALL_SECONDS,
      noProgressSeconds: CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
      tracePath: path.join(traceRoot, `${key}.stream-json.ndjson`),
      stderrPath: path.join(traceRoot, `${key}.stderr.txt`),
      environment: {
        configRoot: path.resolve(values["config-root"]),
        ...privateEnvironment(claim),
      },
    }, { ambient, onProgress: () => {
      fs.appendFileSync(progressPath, ".\n", { encoding: "utf8" });
      stdout.write(`TEST_FLOW_PROGRESS stage.progress claude-deepseek ${key}\n`);
    } });
    const terminalUsage = normalizedUsageOrNull(result?.receipt?.usage);
    requireWrapper(terminalUsage !== null, "CLAUDE_DEEPSEEK_ROLE_USAGE_INVALID", "Evidence V2 role terminal usage is incomplete");
    requireWrapper(
      terminalUsage.cost_usd <= budget.effective_call_cap_usd,
      "CLAUDE_DEEPSEEK_CALL_BUDGET_EXCEEDED",
      "Evidence V2 role terminal cost exceeded its effective Claude CLI threshold",
    );
    const providerTerminal = closedProviderTerminal(result?.receipt?.provider_terminal);
    requireWrapper(
      providerTerminal?.subtype === "success"
        && providerTerminal.is_error === false
        && providerTerminal.exit_code === 0
        && providerTerminal.signal === null,
      "CLAUDE_DEEPSEEK_ROLE_TERMINAL_INVALID",
      "Evidence V2 role provider terminal is not one closed successful process result",
    );
    requireWrapper(result.skills.length === 0 && result.bash.length === 0 && result.mcp.length === 0, "CLAUDE_DEEPSEEK_ROLE_NON_FILE_TOOL", "Evidence V2 role attempted a Skill, shell, or MCP call");
    requireWrapper(result.denied.every((item) => item.executed === false), "CLAUDE_DEEPSEEK_ROLE_DENIED_EXECUTION", "A denied Evidence V2 role tool executed");
    const workspaceAudit = auditRoleWorkspace({ workspaceRoot, roleSpec, processResult: result });
    const receipt = Object.freeze({
      ...result.receipt,
      workflow: `${parsed.role}:${parsed.attempt}`,
      role: parsed.role,
      evaluation_attempt: parsed.attempt,
      role_call_ordinal: parsed.attempt === "PRIMARY" ? 1 : 2,
      max_budget_usd: budget.effective_call_cap_usd,
      budget,
      provider_terminal: providerTerminal,
      usage: terminalUsage,
      prompt: {
        sha256: sha256Bytes(prompt),
        utf8_size: Buffer.byteLength(prompt, "utf8"),
      },
      tool_policy: policy,
      workspace_audit: workspaceAudit,
      usage_complete: true,
      failure_code: null,
    });
    validateClaudeDeepseekRoleReceipt(receipt, {
      planCaps: CLAUDE_DEEPSEEK_MODEL_CERT_PLAN_CAPS,
      expectedRole: parsed.role,
      expectedAttempt: parsed.attempt,
      priorCostUsd: budget.prior_cost_usd,
    });
    writeJsonNew(path.join(path.resolve(values["usage-root"]), `${key}.json`), receipt);
    writeJsonNew(path.join(traceRoot, `${key}.receipt.json`), receipt);
    const terminal = result.events.at(-1);
    stdout.write(`${String(terminal?.result ?? "")}\n`);
    return receipt;
  } catch (error) {
    if (claim === null) throw error;
    const observed = error?.details?.terminal ?? null;
    const usage = normalizedUsageOrNull(result?.receipt?.usage ?? observed?.usage ?? null);
    const hasProcessExit = Object.hasOwn(error?.details ?? {}, "exit_code") || Object.hasOwn(error?.details ?? {}, "signal");
    const providerTerminal = closedProviderTerminal(result?.receipt?.provider_terminal) ?? (
      observed === null && !hasProcessExit ? null : closedProviderTerminal({
        subtype: observed?.subtype ?? null,
        is_error: observed?.is_error ?? null,
        stop_reason: observed?.stop_reason ?? null,
        exit_code: error?.details?.exit_code ?? null,
        signal: error?.details?.signal ?? null,
      })
    );
    const receipt = Object.freeze({
      schema_version: 1,
      invocation_id: `${values["run-id"]}:${key}`,
      phase: parsed.role,
      model: result?.receipt?.model ?? CLAUDE_DEEPSEEK_MODEL,
      attempt: 1,
      retry: 0,
      status: "FAIL",
      terminal: true,
      started_at_utc: result?.receipt?.started_at_utc ?? startedAtUtc,
      finished_at_utc: new Date().toISOString(),
      turns: result?.receipt?.turns ?? observed?.turns ?? 0,
      wall_timeout_seconds: CLAUDE_DEEPSEEK_CALL_WALL_SECONDS,
      max_turns: CLAUDE_DEEPSEEK_E2E_MAX_TURNS,
      max_output_tokens: CLAUDE_DEEPSEEK_MAX_OUTPUT_TOKENS,
      appended_system_prompt: null,
      workflow: `${parsed.role}:${parsed.attempt}`,
      role: parsed.role,
      evaluation_attempt: parsed.attempt,
      role_call_ordinal: parsed.attempt === "PRIMARY" ? 1 : 2,
      max_budget_usd: budget?.effective_call_cap_usd ?? null,
      budget,
      disallowed_tools: ["Bash", "Glob", "Grep", "Skill"],
      prompt: {
        sha256: sha256Bytes(prompt),
        utf8_size: Buffer.byteLength(prompt, "utf8"),
      },
      tool_policy: policy,
      workspace_audit: null,
      environment_policy: result?.receipt?.environment_policy ?? null,
      provider_terminal: providerTerminal,
      usage_complete: usage !== null,
      usage,
      failure_code: typeof error?.code === "string" ? error.code : "CLAUDE_DEEPSEEK_MODEL_CALL_FAILED",
    });
    writeJsonNew(path.join(path.resolve(values["usage-root"]), `${key}.json`), receipt);
    writeJsonNew(path.join(traceRoot, `${key}.receipt.json`), receipt);
    throw error;
  }
}

export function readRoleInvocationReceipts(usageRoot) {
  const root = path.resolve(usageRoot);
  if (!fs.existsSync(root)) return [];
  const order = ["specialist-primary", "specialist-repair", "reviewer-primary", "reviewer-repair"];
  return order
    .map((key) => path.join(root, `${key}.json`))
    .filter((target) => fs.existsSync(target))
    .map((target) => JSON.parse(fs.readFileSync(target, "utf8")));
}

export function safeServiceError(error) {
  return {
    schema_version: 1,
    status: "FAIL",
    code: error?.code ?? "CLAUDE_DEEPSEEK_SERVICE_WRAPPER_FAILED",
    message: error?.message ?? String(error),
  };
}

async function main() {
  try { await runServiceInvocation(parseArguments(process.argv.slice(2))); }
  catch (error) {
    process.stderr.write(canonicalJson(safeServiceError(error)));
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === MODULE_PATH) await main();
