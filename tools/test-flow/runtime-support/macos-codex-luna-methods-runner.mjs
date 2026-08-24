#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import {
  canonicalJson,
  CODEX_LUNA_MODEL,
  CODEX_LUNA_REASONING_EFFORT,
  sha256Bytes,
  treeDigest,
  validateCodexLunaIdentity,
  verifyMethodsV1Package,
} from "./codex-luna-contract.mjs";
import {
  auditCodexLunaRuntimeSecrets,
  readCodexLunaExternalAuth,
  runCodexLunaAppServerCall,
} from "./codex-luna-app-server-runtime.mjs";
import {
  buildGenerationWorkspace,
  generationPrompt,
  safeEnvironment,
} from "./codex-luna-exploration-runner.mjs";
import {
  auditModelInvocations,
  assertMethodsPackageUnchanged,
  buildMethodsCacheManifest,
  buildMethodsProducerIdentity,
  MACOS_CODEX_LUNA_CALL_WALL_SECONDS,
  MACOS_CODEX_LUNA_NO_PROGRESS_SECONDS,
  MACOS_CODEX_LUNA_SKILL_NAME,
  methodsCachePath,
  validateMethodsCache,
} from "./macos-codex-luna-e2e-contract.mjs";
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

export function safeMethodsRunnerError(error) {
  const safe = {
    schema_version: 1,
    status: "FAIL",
    code: error?.code ?? "MACOS_CODEX_LUNA_METHODS_RUNNER_FAILED",
    message: error?.message ?? String(error),
  };
  const details = error?.details;
  if (details !== null && typeof details === "object" && !Array.isArray(details)) {
    const projected = {};
    for (const key of ["method", "line", "item_type", "function_name", "id", "role", "field"]) {
      const value = details[key];
      if (typeof value === "string" || Number.isSafeInteger(value) || value === null) projected[key] = value;
    }
    if (Object.keys(projected).length > 0) safe.details = projected;
  }
  return safe;
}

function parseArguments(argv) {
  const values = {};
  const flags = new Set(["allow-posthoc-budget", "verify-cache-only"]);
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (!argument.startsWith("--")) fail("MACOS_CODEX_LUNA_METHODS_ARGUMENT_INVALID", "Arguments must use --name value syntax");
    const name = argument.slice(2);
    if (Object.hasOwn(values, name)) fail("MACOS_CODEX_LUNA_METHODS_ARGUMENT_DUPLICATE", "Argument is duplicated", { name });
    if (flags.has(name)) values[name] = true;
    else {
      if (index + 1 >= argv.length || argv[index + 1].startsWith("--")) fail("MACOS_CODEX_LUNA_METHODS_ARGUMENT_MISSING", "Argument value is missing", { name });
      values[name] = argv[++index];
    }
  }
  const required = ["codex-entry", "auth-source", "meta-skill-root", "wiki", "registration-template", "python-entry", "cache-root", "work-root", "private-root", "evidence-root", "usage-root", "run-id"];
  if (!required.every((name) => typeof values[name] === "string" && values[name].length > 0) || values["allow-posthoc-budget"] !== true) fail("MACOS_CODEX_LUNA_METHODS_ARGUMENT_MISSING", "Methods runner requires all frozen inputs/roots and explicit post-hoc budget acknowledgement");
  return values;
}

export function verifyMethodsCacheOnly(options, { ambient = process.env } = {}) {
  createEmptyRoot(options.workRoot, "Methods work root");
  createEmptyRoot(options.privateRoot, "Methods private root");
  const evidenceRoot = createEmptyRoot(options.evidenceRoot, "Methods evidence root");
  createEmptyRoot(options.usageRoot, "Methods usage root");
  const codexIdentity = validateCodexLunaIdentity(options.codexEntry, options.authSource);
  const producer = buildMethodsProducerIdentity({ wiki: options.wiki, metaSkillRoot: options.metaSkillRoot, registrationTemplate: options.registrationTemplate, codexIdentity });
  const cache = validateMethodsCache({ cacheRoot: options.cacheRoot, producer, registrationTemplate: options.registrationTemplate });
  assertMethodsPackageUnchanged(cache);
  const validator = path.join(options.metaSkillRoot, "scripts", "validate_generated_skill.py");
  const validatorReceipt = validateGeneratedPackage({ pythonEntry: options.pythonEntry, validator, packageRoot: cache.package_root, wiki: options.wiki });
  const auth = readCodexLunaExternalAuth(options.authSource, ambient);
  const security = auditCodexLunaRuntimeSecrets({ roots: [cache.package_root], auth });
  const identityReceipt = { schema_version: 1, status: "PASS", codex: codexIdentity, producer, execution: "cache-verification" };
  const usage = {
    schema_version: 1,
    status: "PASS",
    workflow: "methods-cache-verification",
    expected_phases: [],
    retry_count: 0,
    aggregate: { input_tokens: 0, cached_input_tokens: 0, output_tokens: 0, total_tokens: 0, equivalent_usd: 0 },
    price_snapshot: null,
  };
  const packageReceipt = {
    schema_version: 1,
    status: "PASS",
    producer_identity: producer.producer_identity,
    package_tree_sha256: cache.manifest.package.tree_sha256,
    validator: validatorReceipt,
    cache: { manifest_sha256: sha256Bytes(canonicalJson(cache.manifest)), manifest: cache.manifest },
  };
  const gate = {
    schema_version: 1,
    status: "PASS",
    mode: "cache-verification",
    checks: { invocation: false, validation: true, cache_publish: false, cache_identity: true, security: true },
    evidence: {
      codex_identity_sha256: sha256Bytes(canonicalJson(identityReceipt)),
      methods_package_sha256: sha256Bytes(canonicalJson(packageReceipt)),
      usage_sha256: sha256Bytes(canonicalJson(usage)),
    },
  };
  writeJson(path.join(evidenceRoot, "codex-identity.json"), identityReceipt);
  writeJson(path.join(evidenceRoot, "model-invocations.json"), { schema_version: 1, status: "PASS", retry_policy: "NONE", invocations: [] });
  writeJson(path.join(evidenceRoot, "model-usage.json"), usage);
  writeJson(path.join(evidenceRoot, "methods-package.json"), packageReceipt);
  writeJson(path.join(evidenceRoot, "security-audit.json"), { schema_version: 1, status: "PASS", secret_scan: security, auth_persisted: false });
  writeJson(path.join(evidenceRoot, "adapter-receipt.json"), gate);
  return gate;
}

function createEmptyRoot(root, label) {
  const resolved = path.resolve(root);
  if (fs.existsSync(resolved)) {
    if (!fs.statSync(resolved).isDirectory() || fs.readdirSync(resolved).length !== 0) fail("MACOS_CODEX_LUNA_METHODS_ROOT_NOT_EMPTY", `${label} must be an empty directory`);
  } else fs.mkdirSync(resolved, { recursive: true, mode: 0o700 });
  return resolved;
}

export function buildMethodsEnvironment(ambient, { codexHome, home, temporary, pythonEntry }) {
  if (!path.isAbsolute(pythonEntry) || !fs.existsSync(pythonEntry) || !fs.statSync(pythonEntry).isFile()) {
    fail("MACOS_CODEX_LUNA_PYTHON_RUNTIME_MISSING", "Methods validator Python must be one existing absolute file");
  }
  const environment = safeEnvironment(ambient, { codexHome, home, temporary });
  environment.PATH = `${path.dirname(pythonEntry)}:${environment.PATH}`;
  return environment;
}

function writeJson(filePath, value, { exclusive = true } = {}) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 });
  fs.writeFileSync(filePath, `${canonicalJson(value)}\n`, { encoding: "utf8", mode: 0o600, flag: exclusive ? "wx" : "w" });
}

function copyOrdinaryTree(source, destination) {
  if (fs.existsSync(destination)) fail("MACOS_CODEX_LUNA_METHODS_COPY_COLLISION", "Package destination already exists");
  fs.mkdirSync(destination, { recursive: true, mode: 0o700 });
  for (const entry of fs.readdirSync(source, { withFileTypes: true })) {
    const from = path.join(source, entry.name);
    const to = path.join(destination, entry.name);
    const metadata = fs.lstatSync(from);
    if (metadata.isSymbolicLink()) fail("MACOS_CODEX_LUNA_METHODS_PACKAGE_LINK", "Methods package cannot contain links");
    if (entry.isDirectory()) copyOrdinaryTree(from, to);
    else if (entry.isFile()) fs.copyFileSync(from, to, fs.constants.COPYFILE_EXCL);
    else fail("MACOS_CODEX_LUNA_METHODS_PACKAGE_NODE", "Methods package contains a non-file node");
  }
}

function freezeTree(root) {
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const target = path.join(root, entry.name);
    if (entry.isDirectory()) {
      freezeTree(target);
      fs.chmodSync(target, 0o500);
    } else fs.chmodSync(target, 0o400);
  }
  fs.chmodSync(root, 0o500);
}

function validateGeneratedPackage({ pythonEntry, validator, packageRoot, wiki }) {
  const result = spawnSync(pythonEntry, ["-I", "-B", validator, "--skill-dir", packageRoot, "--wiki", wiki, "--json"], {
    cwd: path.dirname(validator),
    env: { PATH: "/usr/bin:/bin:/usr/sbin:/sbin", LANG: "C.UTF-8", PYTHONDONTWRITEBYTECODE: "1", PYTHONNOUSERSITE: "1" },
    encoding: "utf8",
    timeout: 120_000,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let receipt = null;
  try { receipt = JSON.parse(result.stdout); } catch {}
  if (result.status !== 0 || result.signal !== null || result.error || receipt?.ok !== true) fail("MACOS_CODEX_LUNA_METHODS_VALIDATION_FAILED", "Generated Methods package failed its independent validator", { exit_code: result.status, signal: result.signal, spawn_error: result.error?.code ?? null });
  return { schema_version: 1, status: "PASS", validator_sha256: sha256Bytes(fs.readFileSync(validator)), result: receipt };
}

function publishCache({ cacheRoot, producer, packageRoot, registrationTemplate }) {
  const finalRoot = methodsCachePath(cacheRoot, producer.producer_identity);
  if (fs.existsSync(finalRoot)) fail("MACOS_CODEX_LUNA_METHODS_CACHE_ALREADY_PRESENT", "Exact Methods producer cache already exists; use the matching prior verdict instead of regenerating");
  const parent = path.dirname(finalRoot);
  fs.mkdirSync(parent, { recursive: true, mode: 0o700 });
  const temporary = path.join(parent, `.${producer.producer_identity}.tmp-${process.pid}`);
  if (fs.existsSync(temporary)) fail("MACOS_CODEX_LUNA_METHODS_CACHE_TEMP_COLLISION", "Methods cache temporary path already exists");
  fs.mkdirSync(path.join(temporary, "package"), { recursive: true, mode: 0o700 });
  const cachedPackage = path.join(temporary, "package", MACOS_CODEX_LUNA_SKILL_NAME);
  copyOrdinaryTree(packageRoot, cachedPackage);
  const manifest = buildMethodsCacheManifest({ producer, packageRoot: cachedPackage, registrationTemplate });
  writeJson(path.join(temporary, "manifest.json"), manifest);
  freezeTree(temporary);
  fs.renameSync(temporary, finalRoot);
  return validateMethodsCache({ cacheRoot, producer, registrationTemplate });
}

export async function runMethodsBootstrap(options, { ambient = process.env, onProgress = null } = {}) {
  const workRoot = createEmptyRoot(options.workRoot, "Methods work root");
  const privateRoot = createEmptyRoot(options.privateRoot, "Methods private root");
  const evidenceRoot = createEmptyRoot(options.evidenceRoot, "Methods evidence root");
  const usageRoot = createEmptyRoot(options.usageRoot, "Methods usage root");
  const codexIdentity = validateCodexLunaIdentity(options.codexEntry, options.authSource);
  const producer = buildMethodsProducerIdentity({ wiki: options.wiki, metaSkillRoot: options.metaSkillRoot, registrationTemplate: options.registrationTemplate, codexIdentity });
  const finalCacheRoot = methodsCachePath(options.cacheRoot, producer.producer_identity);
  if (fs.existsSync(finalCacheRoot)) fail("MACOS_CODEX_LUNA_METHODS_CACHE_ALREADY_PRESENT", "Exact Methods producer cache already exists; use the matching prior verdict instead of regenerating");
  const bootstrapRoot = path.join(privateRoot, "bootstrap");
  const codexHome = path.join(bootstrapRoot, "codex-home");
  const home = path.join(bootstrapRoot, "home");
  const temporary = path.join(bootstrapRoot, "tmp");
  for (const directory of [bootstrapRoot, codexHome, home, temporary]) fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const environment = buildMethodsEnvironment(ambient, { codexHome, home, temporary, pythonEntry: options.pythonEntry });
  const auth = readCodexLunaExternalAuth(options.authSource, ambient);
  const generationWorkspace = path.join(workRoot, "generation");
  const preparedGeneration = buildGenerationWorkspace({ attemptRoot: generationWorkspace, metaSkillRoot: options.metaSkillRoot, wiki: options.wiki });
  const tracesRoot = path.join(evidenceRoot, "traces");
  fs.mkdirSync(tracesRoot, { recursive: true, mode: 0o700 });
  const startedAtUtc = new Date().toISOString();
  const trace = await runCodexLunaAppServerCall({
    codexEntry: options.codexEntry,
    auth,
    environment,
    workspaceRoot: generationWorkspace,
    skillPath: path.join(generationWorkspace, ".agents", "skills", "wiki-to-diagnosis-skill", "SKILL.md"),
    mode: "generation",
    prompt: generationPrompt(),
    outputSchema: null,
    callRoot: path.join(privateRoot, "call"),
    privateRoot,
    tracePath: path.join(tracesRoot, "methods-bootstrap.jsonl"),
    stderrPath: path.join(tracesRoot, "methods-bootstrap.stderr.txt"),
    finalPath: path.join(tracesRoot, "methods-bootstrap.final.txt"),
    forbiddenReadPaths: [options.authSource, path.join(path.dirname(options.metaSkillRoot), "..", "..", "AGENTS.md")],
    wallSeconds: 600,
    noProgressSeconds: MACOS_CODEX_LUNA_NO_PROGRESS_SECONDS,
    onProgress: () => onProgress?.("methods-bootstrap"),
  });
  const finishedAtUtc = new Date().toISOString();
  const packageRoot = path.join(generationWorkspace, "generated", MACOS_CODEX_LUNA_SKILL_NAME);
  const packageContract = verifyMethodsV1Package(packageRoot, preparedGeneration.sourceWikiIdentity);
  const validator = path.join(options.metaSkillRoot, "scripts", "validate_generated_skill.py");
  const validatorReceipt = validateGeneratedPackage({ pythonEntry: options.pythonEntry, validator, packageRoot, wiki: options.wiki });
  const invocation = {
    schema_version: 1,
    invocation_id: `${options.runId}:methods-bootstrap`,
    phase: "METHODS_BOOTSTRAP",
    model: CODEX_LUNA_MODEL,
    reasoning_effort: CODEX_LUNA_REASONING_EFFORT,
    attempt: 1,
    retry: 0,
    status: "PASS",
    terminal: true,
    started_at_utc: startedAtUtc,
    finished_at_utc: finishedAtUtc,
    wall_timeout_seconds: MACOS_CODEX_LUNA_CALL_WALL_SECONDS,
    thread_id: trace.thread_id,
    turn_id: trace.turn_id,
    usage: trace.usage,
  };
  const usage = auditModelInvocations([invocation], { workflow: "methods" });
  const cache = publishCache({ cacheRoot: options.cacheRoot, producer, packageRoot, registrationTemplate: options.registrationTemplate });
  const security = auditCodexLunaRuntimeSecrets({ roots: [evidenceRoot, generationWorkspace], auth });
  const identityReceipt = { schema_version: 1, status: "PASS", codex: codexIdentity, producer };
  const packageReceipt = { schema_version: 1, status: "PASS", producer_identity: producer.producer_identity, package_tree_sha256: packageContract.tree_sha256, validator: validatorReceipt, cache: { manifest_sha256: sha256Bytes(canonicalJson(cache.manifest)), manifest: cache.manifest } };
  const gate = { schema_version: 1, status: "PASS", checks: { invocation: true, validation: true, cache_publish: true, security: true }, evidence: { codex_identity_sha256: sha256Bytes(canonicalJson(identityReceipt)), methods_package_sha256: sha256Bytes(canonicalJson(packageReceipt)), usage_sha256: sha256Bytes(canonicalJson(usage)) } };
  writeJson(path.join(evidenceRoot, "codex-identity.json"), identityReceipt);
  writeJson(path.join(evidenceRoot, "model-invocations.json"), { schema_version: 1, status: "PASS", retry_policy: "NONE", invocations: [invocation] });
  writeJson(path.join(evidenceRoot, "model-usage.json"), usage);
  writeJson(path.join(evidenceRoot, "methods-package.json"), packageReceipt);
  writeJson(path.join(evidenceRoot, "security-audit.json"), { schema_version: 1, status: "PASS", secret_scan: security, auth_persisted: false });
  writeJson(path.join(evidenceRoot, "adapter-receipt.json"), gate);
  return gate;
}

async function main() {
  try {
    const values = parseArguments(process.argv.slice(2));
    const options = {
      runId: values["run-id"],
      codexEntry: path.resolve(values["codex-entry"]),
      authSource: path.resolve(values["auth-source"]),
      metaSkillRoot: path.resolve(values["meta-skill-root"]),
      wiki: path.resolve(values.wiki),
      registrationTemplate: path.resolve(values["registration-template"]),
      pythonEntry: path.resolve(values["python-entry"]),
      cacheRoot: path.resolve(values["cache-root"]),
      workRoot: path.resolve(values["work-root"]),
      privateRoot: path.resolve(values["private-root"]),
      evidenceRoot: path.resolve(values["evidence-root"]),
      usageRoot: path.resolve(values["usage-root"]),
    };
    const result = values["verify-cache-only"] === true
      ? verifyMethodsCacheOnly(options)
      : await runMethodsBootstrap(options);
    process.stdout.write(`${canonicalJson(result)}\n`);
  } catch (error) {
    process.stderr.write(`${canonicalJson(safeMethodsRunnerError(error))}\n`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === MODULE_PATH) await main();

export { parseArguments, publishCache, validateGeneratedPackage };
