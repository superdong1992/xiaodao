#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { canonicalJson, sha256Bytes, sha256File } from "../../../lib/util.mjs";
import { materializeClaudeSettings } from "../../../lib/release-inputs.mjs";
import {
  auditSkillGenerationTrace,
  discoverLinkedSkillReferences,
  skillGenerationPermissionRules,
} from "../../../runtime-support/isolated-agent-tool-audit.mjs";
import {
  CLAUDE_DEEPSEEK_METHODS_MAX_TURNS,
  CLAUDE_DEEPSEEK_METHODS_USD_LIMIT,
  CLAUDE_DEEPSEEK_METHODS_WALL_SECONDS,
  CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
  CLAUDE_DEEPSEEK_SKILL_NAME,
  assertMethodsPackageUnchanged,
  auditClaudeInvocations,
  buildMethodsProducerIdentity,
  methodsCachePath,
  publishMethodsCacheAtomically,
  treeDigest,
  validateClaudeDeepseekIdentity,
  validateMethodsCache,
} from "./claude-deepseek-contract.mjs";
import { runClaudeProcess } from "./claude-deepseek-process.mjs";

const MODULE_PATH = fileURLToPath(import.meta.url);

class MethodsRunnerError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "MethodsRunnerError";
    this.code = code;
    this.details = details;
  }
}

function fail(code, message, details = {}) {
  throw new MethodsRunnerError(code, message, details);
}

function requireMethods(condition, code, message, details = {}) {
  if (!condition) fail(code, message, details);
}

function createEmptyRoot(root, label) {
  const resolved = path.resolve(root);
  if (fs.existsSync(resolved)) requireMethods(fs.statSync(resolved).isDirectory() && fs.readdirSync(resolved).length === 0, "CLAUDE_DEEPSEEK_METHODS_ROOT_NOT_EMPTY", `${label} must be empty`);
  else fs.mkdirSync(resolved, { recursive: true, mode: 0o700 });
  return resolved;
}

function writeJson(filePath, value, { exclusive = true } = {}) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, canonicalJson(value), { encoding: "utf8", mode: 0o600, flag: exclusive ? "wx" : "w" });
}

function copyTree(source, destination) {
  requireMethods(!fs.existsSync(destination), "CLAUDE_DEEPSEEK_METHODS_COPY_COLLISION", "Copy destination already exists");
  fs.mkdirSync(destination, { recursive: true, mode: 0o700 });
  for (const entry of fs.readdirSync(source, { withFileTypes: true })) {
    const from = path.join(source, entry.name);
    const to = path.join(destination, entry.name);
    const metadata = fs.lstatSync(from);
    requireMethods(!metadata.isSymbolicLink(), "CLAUDE_DEEPSEEK_METHODS_COPY_LINK", "Methods inputs cannot contain links");
    if (entry.isDirectory()) copyTree(from, to);
    else if (entry.isFile() && metadata.nlink === 1) fs.copyFileSync(from, to, fs.constants.COPYFILE_EXCL);
    else fail("CLAUDE_DEEPSEEK_METHODS_COPY_NODE", "Methods inputs may contain ordinary files only");
  }
}

function buildSourceWikiIdentity(wikiBytes) {
  const text = new TextDecoder("utf-8", { fatal: true }).decode(wikiBytes);
  const logTemplates = [];
  let inTextFence = false;
  for (const rawLine of text.split(/\r\n|[\n\v\f\r\x1c-\x1e\x85\u2028\u2029]/u)) {
    const stripped = rawLine.trim();
    if (stripped === "```text") { inTextFence = true; continue; }
    if (stripped === "```" && inTextFence) { inTextFence = false; continue; }
    if (inTextFence && stripped && /\{[A-Za-z_][A-Za-z0-9_]*\}|%[A-Za-z]/.test(stripped)) logTemplates.push(stripped);
  }
  return {
    schema_version: 2,
    algorithm: "sha256",
    source_path: "inputs/wiki.md",
    sha256: sha256Bytes(wikiBytes),
    log_template_extraction_version: 1,
    log_templates: logTemplates,
    log_template_inventory_sha256: sha256Bytes(canonicalJson({ version: 1, templates: logTemplates }).trimEnd()),
  };
}

export function buildMethodsWorkspace({ workspaceRoot, configRoot, metaSkillRoot, wiki }) {
  fs.mkdirSync(workspaceRoot, { recursive: true, mode: 0o700 });
  fs.mkdirSync(configRoot, { recursive: true, mode: 0o700 });
  const gitRoot = path.join(workspaceRoot, ".git");
  fs.mkdirSync(path.join(gitRoot, "objects"), { recursive: true, mode: 0o700 });
  fs.mkdirSync(path.join(gitRoot, "refs", "heads"), { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(gitRoot, "HEAD"), "ref: refs/heads/main\n", { mode: 0o600, flag: "wx" });
  const inputs = path.join(workspaceRoot, "inputs");
  const runtime = path.join(workspaceRoot, "runtime");
  const output = path.join(workspaceRoot, "output");
  for (const directory of [inputs, runtime, output]) fs.mkdirSync(directory, { mode: 0o700 });
  fs.copyFileSync(wiki, path.join(inputs, "wiki.md"), fs.constants.COPYFILE_EXCL);
  const sourceWikiIdentity = buildSourceWikiIdentity(fs.readFileSync(wiki));
  writeJson(path.join(runtime, "source-wiki-identity.json"), sourceWikiIdentity);
  const installedSkill = path.join(configRoot, "skills", "wiki-to-diagnosis-skill");
  copyTree(metaSkillRoot, installedSkill);
  return { workspaceRoot, configRoot, installedSkill, sourceWikiIdentity, output };
}

export function methodsPrompt() {
  return `Use the wiki-to-diagnosis-skill Skill to convert inputs/wiki.md into one Methods Skill named diagnose-rpc-timeout.

Your first action must call the Skill tool with exactly {"skill":"wiki-to-diagnosis-skill"}. After that succeeds, read inputs/wiki.md and runtime/source-wiki-identity.json in full. Copy identity.sha256 verbatim into methods.json and use identity.log_templates as the complete ordered duplicate-preserving checklist for references/source-log-templates.md. Read only the linked references/output-contract.md from the Skill base directory. Do not read repository files, registrations, tests, validators, or oracles. Use only Skill, Read, and Write.

Generate the complete package directly under output/diagnose-rpc-timeout. Write exactly the output-contract files: SKILL.md, methods.json, and required references including references/source-log-templates.md. Finish all Reads before the first Write, use exactly one successful Write per final file, keep Writes contiguous, never overwrite, never write outside that package, and stop after the final Write. The authored Wiki is the only source of business meaning. Do not include package JSON or Markdown in the final response.`;
}

export function validateGeneratedPackage({ pythonEntry, validator, packageRoot, wiki }) {
  const result = spawnSync(pythonEntry, ["-I", "-B", validator, "--skill-dir", packageRoot, "--wiki", wiki, "--json"], {
    cwd: path.dirname(validator),
    env: { PATH: "/usr/bin:/bin:/usr/sbin:/sbin", LANG: "C.UTF-8", PYTHONDONTWRITEBYTECODE: "1", PYTHONNOUSERSITE: "1" },
    encoding: "utf8",
    timeout: 120_000,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let receipt = null;
  try { receipt = JSON.parse(result.stdout); } catch {}
  requireMethods(result.status === 0 && result.signal === null && !result.error && receipt?.ok === true, "CLAUDE_DEEPSEEK_METHODS_VALIDATION_FAILED", "Generated Methods package failed the canonical validator");
  return { schema_version: 1, status: "PASS", validator_sha256: sha256File(validator), result: receipt };
}

export function auditMethodsOracle({ packageRoot, oraclePath }) {
  const oracle = JSON.parse(fs.readFileSync(oraclePath, "utf8"));
  const manifest = JSON.parse(fs.readFileSync(path.join(packageRoot, "methods.json"), "utf8"));
  requireMethods(oracle.schema_version === 2 && oracle.oracle_visibility === "GATE_ONLY", "CLAUDE_DEEPSEEK_METHODS_ORACLE_INVALID", "Methods oracle must be gate-only v2");
  const expected = oracle.expected_package;
  const mismatches = [];
  const equal = (name, actual, wanted) => { if (canonicalJson(actual) !== canonicalJson(wanted)) mismatches.push(name); };
  equal("skill_name", manifest.skill_name, expected.skill_name);
  equal("source_wiki_sha256", manifest.source_wiki_sha256, expected.source_wiki_sha256);
  equal("required_user_inputs", manifest.required_user_inputs, expected.required_user_inputs);
  equal("required_artifacts", manifest.required_artifacts, expected.required_artifacts);
  equal("log_derived_fields", manifest.log_derived_fields, expected.required_log_derived_fields);
  const markerSets = (manifest.methods ?? []).map((method) => new Set(method.evidence_markers ?? []));
  for (const required of expected.method_marker_sets) if (!markerSets.some((actual) => required.all_markers.every((marker) => actual.has(marker)))) mismatches.push(`method_marker_set:${required.semantic_id}`);
  const sharedText = (manifest.shared_references ?? []).map((relative) => fs.readFileSync(path.join(packageRoot, ...relative.split("/")), "utf8")).join("\n");
  if (expected.required_shared_markers.some((marker) => !sharedText.includes(marker))) mismatches.push("required_shared_markers");
  const paths = new Set();
  const visit = (root) => fs.readdirSync(root, { withFileTypes: true }).forEach((entry) => {
    const target = path.join(root, entry.name);
    if (entry.isDirectory()) visit(target); else paths.add(path.relative(packageRoot, target).split(path.sep).join("/"));
  });
  visit(packageRoot);
  if (expected.forbidden_paths.some((forbidden) => [...paths].some((item) => item === forbidden || item.endsWith(`/${forbidden}`)))) mismatches.push("forbidden_paths");
  const productText = [...paths].filter((item) => /\.(?:md|json)$/.test(item)).map((item) => fs.readFileSync(path.join(packageRoot, ...item.split("/")), "utf8")).join("\n");
  if (oracle.author_note_markers_forbidden_in_product.some((marker) => productText.includes(marker))) mismatches.push("author_note_markers");
  if (oracle.business_canaries.some((marker) => productText.includes(marker))) mismatches.push("gate_only_canary_leak");
  requireMethods(mismatches.length === 0, "CLAUDE_DEEPSEEK_METHODS_ORACLE_MISMATCH", "Generated Methods package failed the gate-only semantic oracle", { mismatches });
  return { schema_version: 2, status: "PASS", oracle_visibility: "GATE_ONLY", oracle_sha256: sha256File(oraclePath), mismatch_count: 0, method_marker_set_count: expected.method_marker_sets.length };
}

function secretScan({ roots, settings }) {
  const parsed = JSON.parse(fs.readFileSync(settings, "utf8"));
  const canaries = [parsed.env?.ANTHROPIC_AUTH_TOKEN].filter((item) => typeof item === "string" && item.length >= 8);
  let scanned = 0;
  const visit = (root) => {
    for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
      const target = path.join(root, entry.name);
      if (entry.isDirectory()) visit(target);
      else if (entry.isFile()) {
        scanned += 1;
        const bytes = fs.readFileSync(target);
        for (const canary of canaries) requireMethods(!bytes.includes(Buffer.from(canary)), "CLAUDE_DEEPSEEK_SECRET_LEAK", "Generated evidence contains a provider credential");
      }
    }
  };
  roots.filter((root) => fs.existsSync(root)).forEach(visit);
  return { schema_version: 1, status: "PASS", scanned_files: scanned, canary_count: canaries.length, secret_values_persisted: false };
}

function evidence({ evidenceRoot, identity, producer, invocation, usage, packageReceipt, oracle, security, cacheMode }) {
  const identityReceipt = { schema_version: 1, status: "PASS", claude: identity, producer, execution: cacheMode };
  const gate = { schema_version: 1, status: "PASS", mode: cacheMode, checks: { invocation: invocation !== null, validation: true, semantic_oracle: true, cache_identity: true, atomic_publish: cacheMode === "bootstrap", security: true } };
  writeJson(path.join(evidenceRoot, "claude-identity.json"), identityReceipt);
  writeJson(path.join(evidenceRoot, "model-invocations.json"), { schema_version: 1, status: "PASS", retry_policy: "NONE", invocations: invocation ? [invocation] : [] });
  writeJson(path.join(evidenceRoot, "model-usage.json"), usage);
  writeJson(path.join(evidenceRoot, "methods-package.json"), packageReceipt);
  writeJson(path.join(evidenceRoot, "scenario-evaluation-audit.json"), oracle);
  writeJson(path.join(evidenceRoot, "security-audit.json"), security);
  writeJson(path.join(evidenceRoot, "adapter-receipt.json"), gate);
  return gate;
}

export function verifyMethodsCacheOnly(options) {
  createEmptyRoot(options.workRoot, "Methods work root");
  createEmptyRoot(options.privateRoot, "Methods private root");
  const evidenceRoot = createEmptyRoot(options.evidenceRoot, "Methods evidence root");
  createEmptyRoot(options.usageRoot, "Methods usage root");
  const identity = validateClaudeDeepseekIdentity(options.claudeEntry, options.claudeSettings);
  const producer = buildMethodsProducerIdentity({ wiki: options.wiki, metaSkillRoot: options.metaSkillRoot, registrationTemplate: options.registrationTemplate, claudeIdentity: identity });
  const cache = validateMethodsCache({ cacheRoot: options.cacheRoot, producer, registrationTemplate: options.registrationTemplate });
  assertMethodsPackageUnchanged(cache);
  const validator = validateGeneratedPackage({ pythonEntry: options.pythonEntry, validator: path.join(options.metaSkillRoot, "scripts", "validate_generated_skill.py"), packageRoot: cache.package_root, wiki: options.wiki });
  const oracle = auditMethodsOracle({ packageRoot: cache.package_root, oraclePath: options.oracle });
  const usage = { schema_version: 1, status: "PASS", workflow: "methods-cache-verification", expected_phases: [], retry_count: 0, aggregate: { input_tokens: 0, output_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, total_tokens: 0, cost_usd: 0 } };
  const security = secretScan({ roots: [cache.package_root], settings: options.claudeSettings });
  return evidence({ evidenceRoot, identity, producer, invocation: null, usage, packageReceipt: { schema_version: 1, status: "PASS", producer_identity: producer.producer_identity, package_tree_sha256: cache.manifest.package.tree_sha256, validator, cache: cache.manifest }, oracle, security, cacheMode: "cache-verification" });
}

export async function runMethodsBootstrap(options, { ambient = process.env, onProgress = null } = {}) {
  const workRoot = createEmptyRoot(options.workRoot, "Methods work root");
  const privateRoot = createEmptyRoot(options.privateRoot, "Methods private root");
  const evidenceRoot = createEmptyRoot(options.evidenceRoot, "Methods evidence root");
  createEmptyRoot(options.usageRoot, "Methods usage root");
  const identity = validateClaudeDeepseekIdentity(options.claudeEntry, options.claudeSettings);
  const producer = buildMethodsProducerIdentity({ wiki: options.wiki, metaSkillRoot: options.metaSkillRoot, registrationTemplate: options.registrationTemplate, claudeIdentity: identity });
  const workspaceRoot = path.join(workRoot, "generation");
  const configRoot = path.join(privateRoot, "claude-config");
  const prepared = buildMethodsWorkspace({ workspaceRoot, configRoot, metaSkillRoot: options.metaSkillRoot, wiki: options.wiki });
  const settings = path.join(privateRoot, "claude-settings.json");
  materializeClaudeSettings(options.claudeSettings, settings);
  const linkedReferences = discoverLinkedSkillReferences(prepared.installedSkill);
  const allowedTools = skillGenerationPermissionRules({ workspaceRoot, skillRoot: prepared.installedSkill, linkedReferences, sourceRoot: options.sourceRoot });
  const home = path.join(privateRoot, "home");
  const temporary = path.join(privateRoot, "tmp");
  for (const directory of [home, temporary]) fs.mkdirSync(directory, { mode: 0o700 });
  const processResult = await runClaudeProcess({
    claudeEntry: options.claudeEntry,
    settings,
    cwd: workspaceRoot,
    prompt: methodsPrompt(),
    phase: "METHODS_BOOTSTRAP",
    invocationId: `${options.runId}:methods-bootstrap`,
    tools: ["Read", "Write", "Skill"],
    allowedTools,
    maxTurns: CLAUDE_DEEPSEEK_METHODS_MAX_TURNS,
    maxBudgetUsd: CLAUDE_DEEPSEEK_METHODS_USD_LIMIT,
    wallTimeoutSeconds: CLAUDE_DEEPSEEK_METHODS_WALL_SECONDS,
    noProgressSeconds: CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
    tracePath: path.join(evidenceRoot, "methods-bootstrap.stream-json.ndjson"),
    stderrPath: path.join(evidenceRoot, "methods-bootstrap.stderr.txt"),
    receiptPath: path.join(options.usageRoot, "methods-bootstrap.json"),
    environment: { configRoot, home, temporary },
  }, { ambient, onProgress });
  const traceAudit = auditSkillGenerationTrace({ events: processResult.events, workspaceRoot, skillRoot: prepared.installedSkill, sourceRoot: options.sourceRoot });
  const packageRoot = path.join(prepared.output, CLAUDE_DEEPSEEK_SKILL_NAME);
  const packageContract = { tree_sha256: treeDigest(packageRoot), source_wiki_sha256: prepared.sourceWikiIdentity.sha256 };
  const validator = validateGeneratedPackage({ pythonEntry: options.pythonEntry, validator: path.join(options.metaSkillRoot, "scripts", "validate_generated_skill.py"), packageRoot, wiki: options.wiki });
  const oracle = auditMethodsOracle({ packageRoot, oraclePath: options.oracle });
  const usage = auditClaudeInvocations([processResult.receipt], { workflow: "methods" });
  const stagingRoot = path.join(path.dirname(methodsCachePath(options.cacheRoot, producer.producer_identity)), `.${producer.producer_identity}.staging-${process.pid}`);
  const cache = publishMethodsCacheAtomically({ cacheRoot: options.cacheRoot, producer, packageRoot, registrationTemplate: options.registrationTemplate, stagingRoot });
  const security = secretScan({ roots: [evidenceRoot, workspaceRoot, cache.package_root], settings: options.claudeSettings });
  const packageReceipt = { schema_version: 1, status: "PASS", producer_identity: producer.producer_identity, package_tree_sha256: packageContract.tree_sha256, validator, trace_audit: traceAudit, cache: cache.manifest, published: cache.published };
  return evidence({ evidenceRoot, identity, producer, invocation: processResult.receipt, usage, packageReceipt, oracle, security, cacheMode: "bootstrap" });
}

export function safeMethodsRunnerError(error) {
  return { schema_version: 1, status: "FAIL", code: error?.code ?? "CLAUDE_DEEPSEEK_METHODS_RUNNER_FAILED", message: error?.message ?? String(error) };
}

export function parseArguments(argv) {
  const values = {};
  const flags = new Set(["verify-cache-only"]);
  const names = new Set(["source-root", "claude-entry", "claude-settings", "meta-skill-root", "wiki", "oracle", "registration-template", "python-entry", "cache-root", "work-root", "private-root", "evidence-root", "usage-root", "run-id"]);
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    requireMethods(argument.startsWith("--"), "CLAUDE_DEEPSEEK_METHODS_ARGUMENT_INVALID", "Arguments must use --name value syntax");
    const name = argument.slice(2);
    requireMethods(names.has(name) || flags.has(name), "CLAUDE_DEEPSEEK_METHODS_ARGUMENT_UNKNOWN", "Methods runner received an unsupported argument");
    requireMethods(!Object.hasOwn(values, name), "CLAUDE_DEEPSEEK_METHODS_ARGUMENT_DUPLICATE", "Argument is duplicated");
    if (flags.has(name)) values[name] = true;
    else {
      requireMethods(index + 1 < argv.length && !argv[index + 1].startsWith("--"), "CLAUDE_DEEPSEEK_METHODS_ARGUMENT_MISSING", "Argument value is missing");
      values[name] = argv[++index];
    }
  }
  const required = [...names];
  requireMethods(required.every((name) => typeof values[name] === "string" && values[name]), "CLAUDE_DEEPSEEK_METHODS_ARGUMENT_MISSING", "Methods runner arguments are incomplete");
  return values;
}

async function main() {
  try {
    const values = parseArguments(process.argv.slice(2));
    const options = Object.fromEntries(Object.entries({
      runId: values["run-id"], sourceRoot: values["source-root"], claudeEntry: values["claude-entry"], claudeSettings: values["claude-settings"], metaSkillRoot: values["meta-skill-root"], wiki: values.wiki, oracle: values.oracle, registrationTemplate: values["registration-template"], pythonEntry: values["python-entry"], cacheRoot: values["cache-root"], workRoot: values["work-root"], privateRoot: values["private-root"], evidenceRoot: values["evidence-root"], usageRoot: values["usage-root"],
    }).map(([key, value]) => [key, key === "runId" ? value : path.resolve(value)]));
    const result = values["verify-cache-only"] ? verifyMethodsCacheOnly(options) : await runMethodsBootstrap(options);
    process.stdout.write(canonicalJson(result));
  } catch (error) {
    process.stderr.write(canonicalJson(safeMethodsRunnerError(error)));
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === MODULE_PATH) await main();
