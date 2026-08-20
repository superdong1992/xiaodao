#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { TextDecoder } from "node:util";
import { atomicCreateJson, canonicalJson, sha256Bytes, sha256File } from "../lib/util.mjs";
import { normalizeUsage, TOKEN_USAGE_FORMULA } from "../lib/usage.mjs";
import {
  auditIncompleteSkillGenerationTrace,
  auditPartialSkillGenerationTrace,
  auditSkillGenerationTrace,
  buildSkillGenerationIncompleteAuditRejectedReceipt,
  discoverLinkedSkillReferences,
  isSkillGenerationPhaseCheckpointMode,
  SkillGenerationTraceAuditError,
  SKILL_GENERATION_TRACE_SCHEMA_VERSION,
  skillGenerationPermissionRules,
  validIsolatedAgentStreamEventType,
  validSkillGenerationWriteJsonDiagnostic,
} from "./isolated-agent-tool-audit.mjs";
import {
  GENERATION_BLUEPRINT_SUBMISSION_JSON_SCHEMA,
  SKILL_GENERATION_RULE_IR,
  validSkillGenerationRuleIrCompilerFailure,
  validSkillGenerationRuleIrDiagnostic,
} from "./skill-generation-rule-ir.mjs";
import {
  assertIsolatedAgentInboundEnvironment,
  buildIsolatedAgentEnvironment,
  environmentKeySummary,
  explicitEnvironmentFrom,
  ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY,
  ISOLATED_AGENT_ENV_POLICY_VERSION,
  ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT,
  ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_ENFORCEMENT,
  ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY,
  ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_LIMIT,
} from "./isolated-agent-env.mjs";

function argumentsMap(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    if (!argv[index]?.startsWith("--") || index + 1 >= argv.length) throw new Error("WRAPPER_ARGUMENT_INVALID");
    values[argv[index].slice(2).replaceAll("-", "_")] = argv[index + 1];
  }
  return values;
}

function writeNew(filePath, value) {
  const directory = path.dirname(filePath);
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const temporary = path.join(
    directory,
    `.${path.basename(filePath)}.${process.pid}.${crypto.randomUUID()}.tmp`,
  );
  try {
    fs.writeFileSync(temporary, `${JSON.stringify(value)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
    fs.linkSync(temporary, filePath);
    fs.unlinkSync(temporary);
  } finally {
    if (fs.existsSync(temporary)) fs.rmSync(temporary, { force: true });
  }
}

function sanitizedEventType(event) {
  const value = event?.type;
  return validIsolatedAgentStreamEventType(value) ? value : null;
}

function ruleIrFailure(phase, constraintId, irBytes) {
  const diagnostic = {
    schema_version: 1,
    phase,
    constraint_id: constraintId,
    ir: {
      size_bytes: irBytes.length,
      sha256: sha256Bytes(irBytes),
    },
  };
  const error = new Error("GenerationBlueprint compiler or deep validator failed");
  error.code = "SKILL_TRACE_RULE_IR_INVALID";
  error.details = {
    diagnostic: validSkillGenerationRuleIrDiagnostic(diagnostic) ? diagnostic : null,
  };
  throw error;
}

function compileAndValidateRuleIr(value) {
  const canonicalIr = canonicalJson(value);
  const irBytes = Buffer.from(canonicalIr, "utf8");
  if (irBytes.length > SKILL_GENERATION_RULE_IR.max_canonical_bytes) {
    ruleIrFailure("WRAPPER", "IR_SIZE_INVALID", irBytes);
  }
  const environment = Object.fromEntries(
    ["SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP"]
      .filter((name) => typeof process.env[name] === "string")
      .map((name) => [name, process.env[name]]),
  );
  environment.PYTHONNOUSERSITE = "1";
  environment.PYTHONDONTWRITEBYTECODE = "1";
  const result = spawnSync(
    validatorCommand,
    [...validatorPrefix, validatorScript, "--source-root", values.source_root],
    {
      cwd: values.source_root,
      env: environment,
      input: canonicalIr,
      windowsHide: true,
      maxBuffer: 2 * 1024 * 1024,
    },
  );
  if (result.status !== 0 || result.signal !== null || result.error || result.stderr.length !== 0) {
    let compilerFailure = null;
    try {
      const stderr = new TextDecoder("utf-8", { fatal: true }).decode(result.stderr);
      compilerFailure = JSON.parse(stderr);
    } catch {}
    if (validSkillGenerationRuleIrCompilerFailure(compilerFailure)) {
      ruleIrFailure(compilerFailure.phase, compilerFailure.constraint_id, irBytes);
    }
    ruleIrFailure("WRAPPER", "COMPILER_PROCESS", irBytes);
  }
  let text;
  let compilation;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(result.stdout);
    compilation = JSON.parse(text);
  } catch {
    ruleIrFailure("WRAPPER", "COMPILER_ENVELOPE", irBytes);
  }
  if (text !== canonicalJson(compilation)) {
    ruleIrFailure("WRAPPER", "COMPILER_ENVELOPE", irBytes);
  }
  return compilation;
}

const values = argumentsMap(process.argv.slice(2));
const inboundEnvironment = assertIsolatedAgentInboundEnvironment(process.env, { allowSessionCredentials: true });
const hasMaxOutputTokens = values.max_output_tokens !== undefined;
const hasMaxOutputTokensUpperLimit = values.max_output_tokens_upper_limit !== undefined;
const maxOutputTokensUpperLimit = hasMaxOutputTokens ? Number(values.max_output_tokens_upper_limit) : null;
const caps = {
  max_turns: Number(values.max_turns),
  max_total_tokens: Number(values.max_total_tokens),
  ...(hasMaxOutputTokens ? { max_output_tokens: Number(values.max_output_tokens) } : {}),
  max_budget_usd: Number(values.max_budget_usd),
  hard_timeout_seconds: Number(values.hard_timeout_seconds),
};
const workflow = values.workflow ?? "job";
let validatorPrefix = null;
let validatorScript = null;
let validatorCommand = null;
if (workflow === "skill-generation") {
  try {
    validatorPrefix = JSON.parse(values.validator_prefix_json);
  } catch {
    validatorPrefix = null;
  }
  validatorCommand = values.validator_command;
  validatorScript = values.validator_script;
}
const outputCapValid = hasMaxOutputTokens === hasMaxOutputTokensUpperLimit
  && (!hasMaxOutputTokens || (
    Number.isSafeInteger(caps.max_output_tokens)
    && caps.max_output_tokens > 0
    && Number.isSafeInteger(maxOutputTokensUpperLimit)
    && maxOutputTokensUpperLimit > 0
    && caps.max_output_tokens <= maxOutputTokensUpperLimit
    && caps.max_output_tokens <= caps.max_total_tokens
  ));
if (!values.claude_entry || !values.settings || !values.model || !values.usage_root || !["job", "skill-generation"].includes(workflow) || (workflow === "skill-generation" && (
  !values.skill_root
  || !values.source_root
  || typeof validatorCommand !== "string"
  || !path.isAbsolute(validatorCommand)
  || !fs.existsSync(validatorCommand)
  || !fs.statSync(validatorCommand).isFile()
  || typeof validatorScript !== "string"
  || !path.isAbsolute(validatorScript)
  || !fs.existsSync(validatorScript)
  || !fs.statSync(validatorScript).isFile()
  || fs.realpathSync.native(validatorScript) !== fs.realpathSync.native(path.join(
    values.source_root,
    "tools",
    "test-flow",
    "runtime-support",
    "compile_skill_generation_rule_ir.py",
  ))
  || !Array.isArray(validatorPrefix)
  || validatorPrefix.length > 8
  || !validatorPrefix.every((item) => typeof item === "string" && item.length > 0 && !item.includes("\0"))
)) || !Number.isSafeInteger(caps.max_turns) || caps.max_turns <= 0 || !Number.isSafeInteger(caps.max_total_tokens) || caps.max_total_tokens <= 0 || !outputCapValid || !Number.isFinite(caps.max_budget_usd) || caps.max_budget_usd <= 0 || !Number.isSafeInteger(caps.hard_timeout_seconds) || caps.hard_timeout_seconds <= 0) {
  throw new Error("WRAPPER_REQUIRED_INPUT_INVALID");
}

let linkedReferences = [];
let toolArguments;
if (workflow === "skill-generation") {
  linkedReferences = discoverLinkedSkillReferences(values.skill_root);
  if (!isSkillGenerationPhaseCheckpointMode(linkedReferences)) throw new Error("WRAPPER_SKILL_STRUCTURED_OUTPUT_REQUIRED");
  const permissionRules = skillGenerationPermissionRules({
    workspaceRoot: process.cwd(),
    skillRoot: values.skill_root,
    linkedReferences,
    sourceRoot: values.source_root,
  });
  toolArguments = [
    "--tools", "Read,Skill,StructuredOutput",
    "--allowedTools", ...permissionRules,
    "--permission-mode", "dontAsk",
    "--json-schema", JSON.stringify(GENERATION_BLUEPRINT_SUBMISSION_JSON_SCHEMA),
  ];
} else {
  toolArguments = ["--tools", "Read,Write", "--dangerously-skip-permissions"];
}

const claudeEnvironment = buildIsolatedAgentEnvironment({
  ambient: process.env,
  explicit: {
    ...explicitEnvironmentFrom(process.env, [
      "CLAUDE_CONFIG_DIR",
      "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    ]),
    ...(hasMaxOutputTokens ? { [ISOLATED_AGENT_CLAUDE_OUTPUT_TOKEN_KEY]: String(caps.max_output_tokens) } : {}),
    ...(workflow === "skill-generation" ? {
      [ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_KEY]: String(ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_LIMIT),
    } : {}),
  },
  allowSessionCredentials: true,
  allowClaudeChildControls: true,
});

const child = spawn(process.execPath, [
  values.claude_entry,
  "-p",
  "--output-format", "stream-json",
  "--verbose",
  "--no-session-persistence",
  "--setting-sources", "user",
  "--settings", values.settings,
  "--model", values.model,
  "--max-turns", String(caps.max_turns),
  "--max-budget-usd", String(caps.max_budget_usd),
  ...toolArguments,
], { cwd: process.cwd(), env: claudeEnvironment, stdio: ["pipe", "pipe", "pipe"] });

process.stdin.pipe(child.stdin);
const stdout = [];
child.stdout.on("data", (chunk) => {
  stdout.push(chunk);
  if (workflow !== "skill-generation") process.stdout.write(chunk);
});
child.stderr.on("data", (chunk) => {
  if (workflow !== "skill-generation") process.stderr.write(chunk);
});
let timedOut = false;
const timeout = setTimeout(() => {
  timedOut = true;
  child.kill("SIGTERM");
  setTimeout(() => child.exitCode === null && child.kill("SIGKILL"), 5000).unref();
}, caps.hard_timeout_seconds * 1000);
timeout.unref();

const exit = await new Promise((resolve, reject) => {
  child.once("error", reject);
  child.once("exit", (code, signal) => resolve({ code, signal }));
});
clearTimeout(timeout);
let streamText = null;
let streamParseValid = true;
try {
  streamText = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(stdout));
} catch {
  streamParseValid = false;
}
const streamLines = streamParseValid ? streamText.split(/\r?\n/).filter(Boolean) : [];
let events = [];
if (streamParseValid) {
  try {
    events = streamLines.map((line) => JSON.parse(line));
  } catch {
    streamParseValid = false;
  }
}
const init = events.filter((event) => event?.type === "system" && event.subtype === "init");
const terminal = events.filter((event) => event?.type === "result");
const terminalShapeValid = terminal.length === 1
  && typeof terminal[0].subtype === "string"
  && /^[a-z][a-z0-9_]{0,63}$/.test(terminal[0].subtype)
  && typeof terminal[0].is_error === "boolean";
const structurallyCompleteStream = streamParseValid
  && init.length === 1
  && terminal.length === 1
  && events[0] === init[0]
  && events.at(-1) === terminal[0]
  && init[0].model === values.model
  && terminalShapeValid;
const streamComplete = !timedOut && structurallyCompleteStream;
const final = streamComplete ? terminal[0] : null;
const streamReceipt = {
  schema_version: 1,
  event_count: streamLines.length,
  parsed_event_count: events.length,
  init_count: init.length,
  result_count: terminal.length,
  last_event_type: sanitizedEventType(events.at(-1)),
  complete: streamComplete,
};
let usage = null;
let usageComplete = false;
if (final !== null) {
  try {
    const rawTokenUsage = {
      input_tokens: final.usage?.input_tokens,
      output_tokens: final.usage?.output_tokens,
      cache_creation_input_tokens: final.usage?.cache_creation_input_tokens,
      cache_read_input_tokens: final.usage?.cache_read_input_tokens,
    };
    const rawCost = final.total_cost_usd ?? final.cost_usd;
    const rawTokens = Object.values(rawTokenUsage);
    const rawTotalTokens = rawTokens.reduce((total, value) => total + value, 0);
    if (!rawTokens.every((value) => Number.isSafeInteger(value) && value >= 0)
      || !Number.isSafeInteger(rawTotalTokens)
      || rawTotalTokens === 0
      || typeof rawCost !== "number"
      || !Number.isFinite(rawCost)
      || rawCost < 0) throw new Error("WRAPPER_MODEL_USAGE_RAW_INVALID");
    usage = normalizeUsage({
      ...rawTokenUsage,
      cost_usd: rawCost,
    });
    usageComplete = true;
  } catch {
    usage = null;
  }
}
const terminalSucceeded = usageComplete && final.subtype === "success" && final.is_error === false;
const turnsShapeValid = usageComplete && Number.isSafeInteger(final.num_turns) && final.num_turns > 0;
let wrapperFailureCode = null;
if (timedOut) wrapperFailureCode = "WRAPPER_MODEL_TIMEOUT";
else if (!streamComplete) wrapperFailureCode = "WRAPPER_MODEL_STREAM_INVALID";
else if (!usageComplete) wrapperFailureCode = "WRAPPER_MODEL_USAGE_INVALID";
else if (usage.total_tokens > caps.max_total_tokens
  || usage.cost_usd > caps.max_budget_usd
  || (Number.isSafeInteger(final.num_turns) && final.num_turns > caps.max_turns)) wrapperFailureCode = "WRAPPER_MODEL_CAP_EXCEEDED";
else if (!turnsShapeValid) wrapperFailureCode = "WRAPPER_MODEL_TERMINAL_INVALID";
else if (!terminalSucceeded) wrapperFailureCode = "WRAPPER_MODEL_TERMINAL_INVALID";
else if (exit.code !== 0 || exit.signal !== null) wrapperFailureCode = "WRAPPER_CHILD_PROCESS_FAILED";
let toolTraceAudit = null;
const terminalReportedFailure = final !== null
  && (final.subtype !== "success" || final.is_error !== false);
const incompletePrefixCandidate = streamParseValid
  && streamLines.length === events.length
  && events.length > 0
  && init.length === 1
  && terminal.length === 0;
if (workflow === "skill-generation" && (timedOut || !streamComplete) && incompletePrefixCandidate) {
  try {
    toolTraceAudit = auditIncompleteSkillGenerationTrace({
      events,
      workspaceRoot: process.cwd(),
      skillRoot: values.skill_root,
      sourceRoot: values.source_root,
    });
  } catch (error) {
    // Terminal-less evidence is retained only when the whole observed prefix
    // is safe and follows the frozen production sequence. A trusted audit
    // rejection retains only its fixed enum code; messages, details, paths,
    // inputs, and raw tool data remain excluded. Unknown failures fail closed.
    toolTraceAudit = error instanceof SkillGenerationTraceAuditError
      ? buildSkillGenerationIncompleteAuditRejectedReceipt(error.code, streamReceipt)
      : null;
  }
} else if (workflow === "skill-generation" && streamComplete && terminalReportedFailure) {
  try {
    toolTraceAudit = auditPartialSkillGenerationTrace({
      events,
      workspaceRoot: process.cwd(),
      skillRoot: values.skill_root,
      sourceRoot: values.source_root,
    });
  } catch {
    // A partial receipt is diagnostic only. Unsafe paths, malformed records,
    // or an incomplete tool result fail closed to null; raw model data is
    // never copied into the invocation receipt.
    toolTraceAudit = null;
  }
} else if (workflow === "skill-generation" && terminalSucceeded) {
  try {
    const compilation = compileAndValidateRuleIr(final.structured_output);
    toolTraceAudit = auditSkillGenerationTrace({
      events,
      workspaceRoot: process.cwd(),
      skillRoot: values.skill_root,
      sourceRoot: values.source_root,
      compilation,
    });
    const outputPath = path.join(process.cwd(), "output", "generation-spec.json");
    const canonicalOutput = canonicalJson(compilation.spec);
    const expectedOutput = {
      ordinal: 9,
      path: "workspace/output/generation-spec.json",
      size_bytes: Buffer.byteLength(canonicalOutput),
      sha256: sha256Bytes(canonicalOutput),
    };
    if (JSON.stringify(toolTraceAudit.output) !== JSON.stringify(expectedOutput)) {
      const error = new Error("Structured output seal mismatch");
      error.code = "SKILL_TRACE_STRUCTURED_OUTPUT_MISMATCH";
      throw error;
    }
    atomicCreateJson(outputPath, compilation.spec);
    if (fs.statSync(outputPath).size !== expectedOutput.size_bytes || sha256File(outputPath) !== expectedOutput.sha256) {
      const error = new Error("Materialized structured output mismatch");
      error.code = "SKILL_TRACE_STRUCTURED_OUTPUT_MISMATCH";
      throw error;
    }
  } catch (error) {
    wrapperFailureCode ??= "WRAPPER_SKILL_TRACE_INVALID";
    const auditedCode = typeof error?.code === "string" && /^SKILL_TRACE_[A-Z0-9_]+$/.test(error.code)
      ? error.code
      : "SKILL_TRACE_AUDIT_FAILED";
    const diagnostic = auditedCode === "SKILL_TRACE_WRITE_JSON_INVALID"
      && validSkillGenerationWriteJsonDiagnostic(error?.details?.diagnostic)
      ? error.details.diagnostic
      : auditedCode === "SKILL_TRACE_RULE_IR_INVALID"
        && validSkillGenerationRuleIrDiagnostic(error?.details?.diagnostic)
        ? error.details.diagnostic
        : null;
    toolTraceAudit = {
      schema_version: SKILL_GENERATION_TRACE_SCHEMA_VERSION,
      status: "FAIL",
      workflow: "skill-generation",
      code: ["SKILL_TRACE_WRITE_JSON_INVALID", "SKILL_TRACE_RULE_IR_INVALID"].includes(auditedCode)
        && diagnostic === null
        ? "SKILL_TRACE_AUDIT_FAILED"
        : auditedCode,
      ...(diagnostic === null ? {} : { diagnostic }),
    };
  }
}
const wrapperExitCode = timedOut ? 124 : wrapperFailureCode !== null ? 1 : 0;
const invocationId = `isolated-agent:${process.pid}:${crypto.randomUUID()}`;
writeNew(path.join(values.usage_root, `${invocationId.replaceAll(":", "-")}.json`), {
  schema_version: 3,
  invocation_id: invocationId,
  class: "isolated-agent",
  workflow,
  environment_policy: {
    schema_version: 1,
    version: ISOLATED_AGENT_ENV_POLICY_VERSION,
    provider_auth_source: "audited-settings-file",
    session_credentials: Object.hasOwn(claudeEnvironment, "PROBLEM_LOCATOR_LOGPARSE_TOKEN") ? "explicit-logparse-broker" : "NONE",
    inbound: inboundEnvironment,
    claude_process: environmentKeySummary(claudeEnvironment),
  },
  tool_trace_audit: toolTraceAudit,
  effective_model: init.length === 1 && init[0].model === values.model ? values.model : null,
  effective_caps: caps,
  // Claude Code 2.1.89 derives terminal modelUsage.maxOutputTokens from its
  // static model profile. It is not an echo of the request max_tokens value.
  usage_complete: usageComplete,
  usage,
  terminal: usageComplete ? { subtype: final.subtype, is_error: final.is_error } : null,
  turns: usageComplete && Number.isSafeInteger(final.num_turns) ? final.num_turns : null,
  stream: streamReceipt,
  wrapper_outcome: {
    schema_version: 1,
    status: wrapperFailureCode === null ? "PASS" : "FAIL",
    code: wrapperFailureCode,
  },
  hard_cap_enforcement: {
    turns: "claude-cli",
    cost_usd: "claude-cli",
    hard_timeout_seconds: "wrapper-process-watchdog",
    total_tokens: `terminal-usage-postcondition:${TOKEN_USAGE_FORMULA}`,
    ...(hasMaxOutputTokens ? { max_output_tokens: ISOLATED_AGENT_OUTPUT_CAP_ENFORCEMENT } : {}),
    ...(workflow === "skill-generation" ? {
      structured_output_retries: ISOLATED_AGENT_STRUCTURED_OUTPUT_RETRY_ENFORCEMENT,
    } : {}),
  },
  timed_out: timedOut,
  process: { exit_code: exit.code, signal: exit.signal, wrapper_exit_code: wrapperExitCode },
});
if (wrapperFailureCode !== null) process.stderr.write(`${wrapperFailureCode}\n`);
process.exitCode = wrapperExitCode;
