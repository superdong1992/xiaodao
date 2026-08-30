#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { canonicalJson, sha256Bytes, sha256File } from "../../../lib/util.mjs";
import { materializeClaudeSettings } from "../../../lib/release-inputs.mjs";
import {
  discoverLinkedSkillReferences,
} from "../../../runtime-support/isolated-agent-tool-audit.mjs";
import {
  CLAUDE_DEEPSEEK_MODULE,
  CLAUDE_DEEPSEEK_METHODS_MAX_TURNS,
  CLAUDE_DEEPSEEK_METHODS_USD_LIMIT,
  CLAUDE_DEEPSEEK_METHODS_WALL_SECONDS,
  CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
  CLAUDE_DEEPSEEK_REGISTRATION_ID,
  CLAUDE_DEEPSEEK_SKILL_NAME,
  assertRegistrationUnchanged,
  auditClaudeInvocations,
  buildRegistrationProducerIdentity,
  publishRegistrationCacheAtomically,
  registrationCachePath,
  treeDigest,
  treeManifest,
  validateClaudeDeepseekIdentity,
  validateRegistrationCache,
  validateRegistrationRoot,
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

export function buildSourceWikiIdentity(wikiBytes) {
  const text = new TextDecoder("utf-8", { fatal: true }).decode(wikiBytes);
  const logTemplates = [];
  let inFence = false;
  let collectFence = false;
  for (const rawLine of text.split(/\r\n|[\n\v\f\r\x1c-\x1e\x85\u2028\u2029]/u)) {
    const stripped = rawLine.trim();
    if (!inFence && stripped.startsWith("```")) {
      inFence = true;
      collectFence = stripped === "```text" || stripped === "```";
      continue;
    }
    if (inFence && stripped === "```") { inFence = false; collectFence = false; continue; }
    if (collectFence && stripped && /\{[A-Za-z_][A-Za-z0-9_]*\}|%[A-Za-z]/.test(stripped)) logTemplates.push(stripped);
  }
  return {
    schema_version: 2,
    algorithm: "sha256",
    source_path: "inputs/wiki.md",
    sha256: sha256Bytes(wikiBytes),
    log_template_extraction_version: 2,
    log_templates: logTemplates,
    log_template_inventory_sha256: sha256Bytes(canonicalJson({ version: 2, templates: logTemplates }).trimEnd()),
  };
}

const LOG_PLACEHOLDER_PATTERN = /\{[A-Za-z_][A-Za-z0-9_]*\}|%[A-Za-z]/gu;

function canonicalEvidenceMarker(template) {
  const matches = [...template.matchAll(LOG_PLACEHOLDER_PATTERN)];
  if (matches.length === 0) return template.trim() || null;
  const prefix = template.slice(0, matches[0].index).trim();
  if (prefix) return prefix;
  let best = null;
  let bestLength = -1;
  for (let index = 0; index + 1 < matches.length; index += 1) {
    const segment = template.slice(matches[index].index + matches[index][0].length, matches[index + 1].index).trim();
    const length = [...segment].length;
    if (segment && length > bestLength) {
      best = segment;
      bestLength = length;
    }
  }
  return best;
}

export function canonicalEvidenceMarkers(logTemplates) {
  requireMethods(Array.isArray(logTemplates) && logTemplates.every((item) => typeof item === "string"), "CLAUDE_DEEPSEEK_LOG_TEMPLATE_INVENTORY_INVALID", "Canonical markers require the closed source log-template inventory");
  const markers = [];
  for (const template of logTemplates) {
    const marker = canonicalEvidenceMarker(template);
    if (marker !== null && !markers.includes(marker)) markers.push(marker);
  }
  return markers;
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
  const installedSkill = path.join(configRoot, "skills", "wiki-to-logparse-diagnosis-skill");
  copyTree(metaSkillRoot, installedSkill);
  return { workspaceRoot, configRoot, installedSkill, sourceWikiIdentity, output };
}

export function methodsPrompt({ registrationId = CLAUDE_DEEPSEEK_REGISTRATION_ID, module = CLAUDE_DEEPSEEK_MODULE, canonicalMarkers } = {}) {
  requireMethods(Array.isArray(canonicalMarkers) && canonicalMarkers.length > 0 && canonicalMarkers.every((item) => typeof item === "string" && item && !/[\r\n]/u.test(item)) && new Set(canonicalMarkers).size === canonicalMarkers.length, "CLAUDE_DEEPSEEK_CANONICAL_MARKERS_INVALID", "Generation prompt requires the exact canonical marker allowlist");
  return `Use the wiki-to-logparse-diagnosis-skill Skill to convert inputs/wiki.md into one production registration named ${registrationId}. The generated Methods Skill name is ${CLAUDE_DEEPSEEK_SKILL_NAME}, and the fixed Logparse module is ${module}.

Your first action must call the Skill tool with exactly {"skill":"wiki-to-logparse-diagnosis-skill"}. After that succeeds, read inputs/wiki.md and runtime/source-wiki-identity.json in full. Copy identity.sha256 verbatim into the package methods.json and use identity.log_templates as the complete ordered duplicate-preserving checklist for references/source-log-templates.md. Read only the linked references/output-contract.md from the Skill base directory. Do not read repository files, registrations, tests, validators, or oracles. Use only Skill, Read, and Write.

Every methods.json evidence_markers and activation_markers item must be copied byte-for-byte from this canonical stable marker allowlist: ${JSON.stringify(canonicalMarkers)}. Bare or shortened event names such as API_COMPLETE, DEADLOOP_DETECTED, LATE_RESPONSE, and QUEUE_HISTORY are invalid unless that exact whole string appears in the allowlist. Do not invent, shorten, or extend a marker. evidence_markers must include every log used to judge the method. activation_markers must be the ordered evidence_markers subset whose presence makes that method worth evaluating; a marker may activate more than one method, and activation alone does not mean CONFIRMED. Generic timeout/failure lines that only establish request context must not activate a method. Decide both lists from the authored Wiki; the allowlist does not assign markers to methods.

Generate the complete registration directly under output/${registrationId}. Its root entries must be exactly registration-template.json and package; package must contain exactly ${CLAUDE_DEEPSEEK_SKILL_NAME} with SKILL.md, methods.json, and required references including references/source-log-templates.md. The registration must use deployment_scope PRODUCTION, version 1.0.0, logparse_product default, module ${module}, and USER_FACT bindings for both slot/process/pid anchors. The generated Methods Skill consumes only the Server-frozen request.json, target_logs.json, receipt, and listed logs; it must not call Skill(logparse-diagnose), the broker, Logparse, or a ZIP packer. Finish all Reads before the first Write, use exactly one successful Write per final file, keep Writes contiguous, never overwrite, never write outside that registration, and stop after the final Write. The authored Wiki is the only source of business meaning. Do not include generated JSON or Markdown in the final response.`;
}

function permissionAbsolute(filePath) {
  const resolved = path.resolve(filePath);
  const drive = /^([A-Za-z]):[\\/](.*)$/u.exec(resolved);
  const portable = drive ? `${drive[1]}/${drive[2].replaceAll("\\", "/")}` : resolved.split(path.sep).join("/").replace(/^\/+/, "");
  return `//${portable.replaceAll("\\", "\\\\").replaceAll("(", "\\(").replaceAll(")", "\\)")}`;
}

function generationPermissionRules({ workspaceRoot, skillRoot, outputContract }) {
  return [
    "Skill(wiki-to-logparse-diagnosis-skill)",
    "Read(/inputs/wiki.md)",
    "Read(/runtime/source-wiki-identity.json)",
    `Read(${permissionAbsolute(outputContract)})`,
    "Edit(/output/**)",
  ];
}

function inside(root, candidate) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return relative !== "" && !relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative);
}

function resolveToolPath(workspaceRoot, value) {
  requireMethods(typeof value === "string" && value.length > 0, "CLAUDE_DEEPSEEK_GENERATION_PATH_INVALID", "Generation tool path is invalid");
  const portable = value.replaceAll("\\", "/");
  if (/^\/(?:inputs|runtime|output)\//u.test(portable)) return path.join(workspaceRoot, ...portable.slice(1).split("/"));
  return path.isAbsolute(value) ? path.resolve(value) : path.resolve(workspaceRoot, value);
}

export function auditRegistrationGenerationTrace({ processResult, workspaceRoot, installedSkill, registrationRoot }) {
  const records = processResult?.records;
  requireMethods(Array.isArray(records) && records.length >= 6, "CLAUDE_DEEPSEEK_GENERATION_TRACE_INVALID", "Registration generation tool trace is incomplete");
  requireMethods(records[0]?.name === "Skill" && records[0]?.input?.skill === "wiki-to-logparse-diagnosis-skill" && processResult.skills?.length === 1, "CLAUDE_DEEPSEEK_GENERATION_SKILL_INVALID", "Generation must load wiki-to-logparse-diagnosis-skill exactly once as its first action");
  const reads = records.filter((item) => item.name === "Read");
  const writes = records.filter((item) => item.name === "Write");
  const outputContract = path.join(installedSkill, "references", "output-contract.md");
  const expectedReads = [path.join(workspaceRoot, "inputs", "wiki.md"), path.join(workspaceRoot, "runtime", "source-wiki-identity.json"), outputContract].map((item) => path.resolve(item));
  const actualReads = reads.map((item) => resolveToolPath(workspaceRoot, item.input?.file_path));
  requireMethods(reads.length === 3 && new Set(actualReads).size === 3 && expectedReads.every((item) => actualReads.includes(item)), "CLAUDE_DEEPSEEK_GENERATION_READS_INVALID", "Generation must read only the Wiki, source identity, and output contract once each");
  requireMethods(writes.length >= 5 && records.slice(1, 4).every((item) => item.name === "Read") && records.slice(4).every((item) => item.name === "Write"), "CLAUDE_DEEPSEEK_GENERATION_SEQUENCE_INVALID", "Generation Reads and contiguous Writes are out of order");
  const written = writes.map((item) => {
    const target = resolveToolPath(workspaceRoot, item.input?.file_path);
    requireMethods(inside(registrationRoot, target) && typeof item.input?.content === "string", "CLAUDE_DEEPSEEK_GENERATION_WRITE_INVALID", "Generation Write escaped the registration root");
    requireMethods(fs.existsSync(target) && fs.readFileSync(target).equals(Buffer.from(item.input.content, "utf8")), "CLAUDE_DEEPSEEK_GENERATION_WRITE_MISMATCH", "Generated bytes differ from the successful Write input");
    return path.relative(registrationRoot, target).split(path.sep).join("/");
  });
  const actualFiles = treeManifest(registrationRoot).filter((item) => item.kind === "file").map((item) => item.path).sort();
  requireMethods(new Set(written).size === written.length && canonicalJson([...written].sort()) === canonicalJson(actualFiles), "CLAUDE_DEEPSEEK_GENERATION_TREE_INVALID", "Every registration file must come from exactly one successful Write");
  return {
    schema_version: 2,
    status: "PASS",
    workflow: "registration-generation",
    skill: "wiki-to-logparse-diagnosis-skill",
    tool_sequence: records.map((item) => item.name),
    stream_trace_sha256: sha256Bytes(processResult.stdout),
    registration_id: CLAUDE_DEEPSEEK_REGISTRATION_ID,
    registration_tree_sha256: treeDigest(registrationRoot),
    file_count: actualFiles.length,
  };
}

export function validateGeneratedRegistration({ pythonEntry, validator, registrationRoot, wiki, module, sourceIdentity }, { spawnImpl = spawnSync } = {}) {
  const result = spawnImpl(pythonEntry, ["-I", "-B", validator, "--registration-dir", registrationRoot, "--wiki", wiki, "--module", module, "--source-identity", sourceIdentity, "--json"], {
    cwd: path.dirname(validator),
    env: { PATH: "/usr/bin:/bin:/usr/sbin:/sbin", LANG: "C.UTF-8", PYTHONDONTWRITEBYTECODE: "1", PYTHONNOUSERSITE: "1" },
    encoding: "utf8",
    timeout: 120_000,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let receipt = null;
  try { receipt = JSON.parse(result.stdout); } catch {}
  requireMethods(result.status === 0 && result.signal === null && !result.error && receipt?.ok === true, "CLAUDE_DEEPSEEK_REGISTRATION_VALIDATION_FAILED", "Generated registration failed the canonical validator");
  return { schema_version: 1, status: "PASS", validator_sha256: sha256File(validator), result: receipt };
}

export function auditMethodsOracle({ registrationRoot, oraclePath }) {
  const packageRoot = path.join(registrationRoot, "package", CLAUDE_DEEPSEEK_SKILL_NAME);
  const oracle = JSON.parse(fs.readFileSync(oraclePath, "utf8"));
  const manifest = JSON.parse(fs.readFileSync(path.join(packageRoot, "methods.json"), "utf8"));
  requireMethods(oracle.schema_version === 2 && oracle.oracle_visibility === "GATE_ONLY", "CLAUDE_DEEPSEEK_METHODS_ORACLE_INVALID", "Methods oracle must be gate-only v2");
  const expected = oracle.expected_package;
  const mismatches = [];
  const equal = (name, actual, wanted) => { if (canonicalJson(actual) !== canonicalJson(wanted)) mismatches.push(name); };
  equal("skill_name", manifest.skill_name, expected.skill_name);
  equal("source_wiki_sha256", manifest.source_wiki_sha256, expected.source_wiki_sha256);
  const wikiInputs = expected.required_user_inputs.filter((item) => !["problem_time", "client_process", "server_process"].includes(item));
  equal("required_user_inputs", manifest.required_user_inputs, ["problem_time", "client_slot", "client_process_name", "server_slot", "server_process_name", "client_pid", "server_pid", ...wikiInputs]);
  equal("required_artifacts", manifest.required_artifacts, expected.required_artifacts);
  equal("log_derived_fields", manifest.log_derived_fields, expected.required_log_derived_fields);
  const markerSets = (manifest.methods ?? []).map((method) => ({
    evidence: new Set(method.evidence_markers ?? []),
    activation: method.activation_markers ?? [],
  }));
  if (markerSets.length !== expected.method_marker_sets.length) mismatches.push("method_count");
  const oneToOne = (expectedIndex, used) => {
    if (expectedIndex === expected.method_marker_sets.length) return true;
    const required = expected.method_marker_sets[expectedIndex];
    for (const [actualIndex, actual] of markerSets.entries()) {
      if (
        used.has(actualIndex)
        || actual.evidence.size !== required.all_markers.length
        || !required.all_markers.every((marker) => actual.evidence.has(marker))
        || canonicalJson(actual.activation) !== canonicalJson(required.activation_markers)
      ) continue;
      used.add(actualIndex);
      if (oneToOne(expectedIndex + 1, used)) return true;
      used.delete(actualIndex);
    }
    return false;
  };
  if (!oneToOne(0, new Set())) mismatches.push("method_marker_sets_one_to_one");
  const sharedText = (manifest.shared_references ?? []).filter((relative) => relative !== "references/source-log-templates.md").map((relative) => fs.readFileSync(path.join(packageRoot, ...relative.split("/")), "utf8")).join("\n");
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
  const gate = { schema_version: 1, status: "PASS", mode: cacheMode, checks: { invocation: invocation !== null, validation: true, semantic_oracle: true, cache_identity: true, atomic_publish: cacheMode === "generation", security: true } };
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
  const workRoot = createEmptyRoot(options.workRoot, "Methods work root");
  createEmptyRoot(options.privateRoot, "Methods private root");
  const evidenceRoot = createEmptyRoot(options.evidenceRoot, "Methods evidence root");
  createEmptyRoot(options.usageRoot, "Methods usage root");
  const identity = validateClaudeDeepseekIdentity(options.claudeEntry, options.claudeSettings);
  const producer = buildRegistrationProducerIdentity({ wiki: options.wiki, metaSkillRoot: options.metaSkillRoot, claudeIdentity: identity, module: options.module });
  const cache = validateRegistrationCache({ cacheRoot: options.cacheRoot, producer });
  assertRegistrationUnchanged(cache);
  const sourceIdentity = path.join(workRoot, "source-wiki-identity.json");
  writeJson(sourceIdentity, buildSourceWikiIdentity(fs.readFileSync(options.wiki)));
  const validator = validateGeneratedRegistration({ pythonEntry: options.pythonEntry, validator: path.join(options.metaSkillRoot, "scripts", "validate_generated_skill.py"), registrationRoot: cache.registration_root, wiki: options.wiki, module: options.module, sourceIdentity });
  const oracle = auditMethodsOracle({ registrationRoot: cache.registration_root, oraclePath: options.oracle });
  const usage = { schema_version: 1, status: "PASS", workflow: "registration-cache-verification", expected_phases: [], retry_count: 0, aggregate: { input_tokens: 0, output_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0, total_tokens: 0, cost_usd: 0 } };
  const security = secretScan({ roots: [cache.registration_root], settings: options.claudeSettings });
  return evidence({ evidenceRoot, identity, producer, invocation: null, usage, packageReceipt: { schema_version: 2, status: "PASS", producer_identity: producer.producer_identity, registration_tree_sha256: cache.manifest.registration.tree_sha256, runtime_ref: cache.manifest.registration.runtime_ref, validator, cache: cache.manifest }, oracle, security, cacheMode: "cache-verification" });
}

export async function runMethodsBootstrap(options, { ambient = process.env, onProgress = null } = {}) {
  const workRoot = createEmptyRoot(options.workRoot, "Methods work root");
  const privateRoot = createEmptyRoot(options.privateRoot, "Methods private root");
  const evidenceRoot = createEmptyRoot(options.evidenceRoot, "Methods evidence root");
  createEmptyRoot(options.usageRoot, "Methods usage root");
  const identity = validateClaudeDeepseekIdentity(options.claudeEntry, options.claudeSettings);
  const producer = buildRegistrationProducerIdentity({ wiki: options.wiki, metaSkillRoot: options.metaSkillRoot, claudeIdentity: identity, module: options.module });
  const workspaceRoot = path.join(workRoot, "generation");
  const configRoot = path.join(privateRoot, "claude-config");
  const prepared = buildMethodsWorkspace({ workspaceRoot, configRoot, metaSkillRoot: options.metaSkillRoot, wiki: options.wiki });
  const settings = path.join(privateRoot, "claude-settings.json");
  materializeClaudeSettings(options.claudeSettings, settings);
  const linkedReferences = discoverLinkedSkillReferences(prepared.installedSkill);
  requireMethods(linkedReferences.length === 1 && linkedReferences[0] === "references/output-contract.md", "CLAUDE_DEEPSEEK_GENERATION_REFERENCE_INVALID", "Generation Skill must link exactly its output contract");
  const allowedTools = generationPermissionRules({ workspaceRoot, skillRoot: prepared.installedSkill, outputContract: path.join(prepared.installedSkill, linkedReferences[0]) });
  const home = path.join(privateRoot, "home");
  const temporary = path.join(privateRoot, "tmp");
  for (const directory of [home, temporary]) fs.mkdirSync(directory, { mode: 0o700 });
  const processResult = await runClaudeProcess({
    claudeEntry: options.claudeEntry,
    settings,
    cwd: workspaceRoot,
    prompt: methodsPrompt({ module: options.module, canonicalMarkers: canonicalEvidenceMarkers(prepared.sourceWikiIdentity.log_templates) }),
    phase: "REGISTRATION_GENERATION",
    invocationId: `${options.runId}:registration-generation`,
    tools: ["Read", "Write", "Skill"],
    allowedTools,
    maxTurns: CLAUDE_DEEPSEEK_METHODS_MAX_TURNS,
    maxBudgetUsd: CLAUDE_DEEPSEEK_METHODS_USD_LIMIT,
    wallTimeoutSeconds: CLAUDE_DEEPSEEK_METHODS_WALL_SECONDS,
    noProgressSeconds: CLAUDE_DEEPSEEK_NO_PROGRESS_SECONDS,
    tracePath: path.join(evidenceRoot, "registration-generation.stream-json.ndjson"),
    stderrPath: path.join(evidenceRoot, "registration-generation.stderr.txt"),
    receiptPath: path.join(options.usageRoot, "registration-generation.json"),
    environment: { configRoot, home, temporary },
  }, { ambient, onProgress });
  const registrationRoot = path.join(prepared.output, CLAUDE_DEEPSEEK_REGISTRATION_ID);
  const traceAudit = auditRegistrationGenerationTrace({ processResult, workspaceRoot, installedSkill: prepared.installedSkill, registrationRoot });
  const validatedRoot = validateRegistrationRoot(registrationRoot, { module: options.module });
  const registrationContract = { tree_sha256: treeDigest(registrationRoot), source_wiki_sha256: prepared.sourceWikiIdentity.sha256, runtime_ref: validatedRoot.runtime_ref };
  const validator = validateGeneratedRegistration({ pythonEntry: options.pythonEntry, validator: path.join(options.metaSkillRoot, "scripts", "validate_generated_skill.py"), registrationRoot, wiki: options.wiki, module: options.module, sourceIdentity: path.join(workspaceRoot, "runtime", "source-wiki-identity.json") });
  const oracle = auditMethodsOracle({ registrationRoot, oraclePath: options.oracle });
  const usage = auditClaudeInvocations([processResult.receipt], { workflow: "generation" });
  const stagingRoot = path.join(path.dirname(registrationCachePath(options.cacheRoot, producer.producer_identity)), `.${producer.producer_identity}.staging-${process.pid}`);
  const cache = publishRegistrationCacheAtomically({ cacheRoot: options.cacheRoot, producer, registrationRoot, stagingRoot });
  const security = secretScan({ roots: [evidenceRoot, workspaceRoot, cache.registration_root], settings: options.claudeSettings });
  const packageReceipt = { schema_version: 2, status: "PASS", producer_identity: producer.producer_identity, registration_tree_sha256: registrationContract.tree_sha256, runtime_ref: registrationContract.runtime_ref, validator, trace_audit: traceAudit, cache: cache.manifest, published: cache.published };
  return evidence({ evidenceRoot, identity, producer, invocation: processResult.receipt, usage, packageReceipt, oracle, security, cacheMode: "generation" });
}

export function safeMethodsRunnerError(error) {
  return { schema_version: 1, status: "FAIL", code: error?.code ?? "CLAUDE_DEEPSEEK_METHODS_RUNNER_FAILED", message: error?.message ?? String(error) };
}

export function parseArguments(argv) {
  const values = {};
  const flags = new Set(["verify-cache-only"]);
  const names = new Set(["source-root", "claude-entry", "claude-settings", "meta-skill-root", "wiki", "oracle", "module", "python-entry", "cache-root", "work-root", "private-root", "evidence-root", "usage-root", "run-id"]);
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
      runId: values["run-id"], sourceRoot: values["source-root"], claudeEntry: values["claude-entry"], claudeSettings: values["claude-settings"], metaSkillRoot: values["meta-skill-root"], wiki: values.wiki, oracle: values.oracle, module: values.module, pythonEntry: values["python-entry"], cacheRoot: values["cache-root"], workRoot: values["work-root"], privateRoot: values["private-root"], evidenceRoot: values["evidence-root"], usageRoot: values["usage-root"],
    }).map(([key, value]) => [key, ["runId", "module"].includes(key) ? value : path.resolve(value)]));
    const result = values["verify-cache-only"] ? verifyMethodsCacheOnly(options) : await runMethodsBootstrap(options);
    process.stdout.write(canonicalJson(result));
  } catch (error) {
    process.stderr.write(canonicalJson(safeMethodsRunnerError(error)));
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === MODULE_PATH) await main();
