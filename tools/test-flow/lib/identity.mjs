import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  canonicalJson,
  normalizeRepoPath,
  resolvePythonTestRuntime,
  runSync,
  sha256Bytes,
  sha256File,
} from "./util.mjs";
import {
  claudeSettingsIdentity,
  validateClaudeDistribution,
} from "./release-inputs.mjs";
import {
  discoverReleaseCaseRoot,
  releaseCasePartition,
} from "./release-case.mjs";
import { chromeIdentity } from "./browser.mjs";

const IGNORED_NAMES = new Set([".git", ".tmp", ".pytest_cache", "__pycache__", "node_modules", ".venv"]);

function git(repoRoot, args) {
  return runSync("git", ["-C", repoRoot, ...args]);
}

export function gitState(repoRoot) {
  const headResult = git(repoRoot, ["rev-parse", "HEAD"]);
  const statusResult = git(repoRoot, ["status", "--porcelain=v1", "-z", "--untracked-files=all"]);
  const branchResult = git(repoRoot, ["branch", "--show-current"]);
  if (headResult.status !== 0 || statusResult.status !== 0) {
    return { available: false, clean: false, head: null, branch: null, entries: [], error: headResult.stderr || statusResult.stderr };
  }
  const chunks = statusResult.stdout.split("\0").filter(Boolean);
  const entries = [];
  for (let index = 0; index < chunks.length; index += 1) {
    const chunk = chunks[index];
    const status = chunk.slice(0, 2);
    let file = chunk.slice(3).split(path.sep).join("/");
    let source = null;
    if (status.includes("R") || status.includes("C")) {
      source = file;
      file = chunks[++index]?.split(path.sep).join("/") ?? file;
    }
    entries.push({ status, file, source });
  }
  return {
    available: true,
    clean: entries.length === 0,
    head: headResult.stdout.trim(),
    branch: branchResult.status === 0 ? branchResult.stdout.trim() : null,
    entries,
  };
}

export function resolveChangeBaseline(repoRoot, successfulDevCommit = null) {
  const candidates = [];
  if (successfulDevCommit) candidates.push({ source: "last-successful-dev", ref: successfulDevCommit });
  candidates.push({ source: "merge-base-origin-main", ref: "origin/main", mergeBase: true });
  candidates.push({ source: "parent", ref: "HEAD^" });
  for (const candidate of candidates) {
    const result = candidate.mergeBase
      ? git(repoRoot, ["merge-base", "HEAD", candidate.ref])
      : git(repoRoot, ["rev-parse", "--verify", `${candidate.ref}^{commit}`]);
    if (result.status === 0) return { source: candidate.source, commit: result.stdout.trim() };
  }
  const head = git(repoRoot, ["rev-parse", "HEAD"]);
  return { source: "head", commit: head.status === 0 ? head.stdout.trim() : null };
}

export function changedFiles(repoRoot, baseline, state = gitState(repoRoot)) {
  const files = new Set();
  if (baseline?.commit) {
    const difference = git(repoRoot, ["diff", "--name-only", "-z", `${baseline.commit}...HEAD`]);
    if (difference.status === 0) {
      for (const name of difference.stdout.split("\0").filter(Boolean)) files.add(name.split(path.sep).join("/"));
    }
  }
  for (const entry of state.entries ?? []) {
    files.add(entry.file);
    if (entry.source) files.add(entry.source);
  }
  return [...files].sort();
}

function walk(root, current, records) {
  const stat = fs.lstatSync(current);
  const relative = path.relative(root, current).split(path.sep).join("/") || ".";
  if (stat.isSymbolicLink()) {
    records.push({ path: relative, kind: "symlink", target: fs.readlinkSync(current) });
    return;
  }
  if (stat.isDirectory()) {
    if (relative !== ".") records.push({ path: `${relative}/`, kind: "directory", mode: stat.mode & 0o777 });
    for (const entry of fs.readdirSync(current, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      if (IGNORED_NAMES.has(entry.name)) continue;
      walk(root, path.join(current, entry.name), records);
    }
    return;
  }
  if (stat.isFile()) {
    records.push({
      path: relative,
      kind: "file",
      mode: stat.mode & 0o777,
      size: stat.size,
      sha256: sha256File(current),
    });
    return;
  }
  records.push({ path: relative, kind: "unsupported", mode: stat.mode & 0o777 });
}

export function hashTree(treeRoot) {
  const absolute = path.resolve(treeRoot);
  if (!fs.existsSync(absolute)) return { status: "MISSING", root: absolute, digest: null, records: [] };
  const records = [];
  walk(absolute, absolute, records);
  return { status: "PRESENT", root: absolute, digest: sha256Bytes(canonicalJson(records)), records };
}

export function hashConfiguredPaths(repoRoot, configuredPaths) {
  const records = [];
  for (const configured of [...configuredPaths].sort()) {
    const { absolute, relative } = normalizeRepoPath(repoRoot, configured);
    if (!fs.existsSync(absolute)) {
      records.push({ path: relative, kind: "missing" });
      continue;
    }
    const stat = fs.lstatSync(absolute);
    if (stat.isDirectory()) {
      const nested = [];
      walk(absolute, absolute, nested);
      for (const record of nested) {
        const suffix = record.path === "." ? "" : record.path;
        records.push({ ...record, path: suffix ? `${relative}/${suffix}` : relative });
      }
    } else if (stat.isSymbolicLink()) {
      records.push({ path: relative, kind: "symlink", target: fs.readlinkSync(absolute) });
    } else if (stat.isFile()) {
      records.push({ path: relative, kind: "file", mode: stat.mode & 0o777, size: stat.size, sha256: sha256File(absolute) });
    } else {
      records.push({ path: relative, kind: "unsupported" });
    }
  }
  records.sort((left, right) => left.path.localeCompare(right.path));
  return { digest: sha256Bytes(canonicalJson(records)), records };
}

function safeEndpointContext(raw) {
  if (!raw) return null;
  try {
    const parsed = new URL(raw);
    return `${parsed.protocol}//${parsed.host}`;
  } catch {
    return "configured-invalid-url";
  }
}

export function modelContextFingerprint(environment = process.env, settingsPath = null) {
  if (settingsPath) {
    const identity = claudeSettingsIdentity(settingsPath);
    return {
      status: identity.status,
      fingerprint: identity.fingerprint,
      endpoint: identity.endpoint,
      model: identity.model,
      settings_policy: identity.status === "PRESENT" ? "env-allowlist-only-no-hooks-v1" : identity.code,
    };
  }
  const secret = environment.ANTHROPIC_AUTH_TOKEN || environment.ANTHROPIC_API_KEY || null;
  const explicit = environment.TEST_FLOW_PROVIDER_CONTEXT_FINGERPRINT || null;
  const message = canonicalJson({
    endpoint: safeEndpointContext(environment.ANTHROPIC_BASE_URL),
    model: environment.ANTHROPIC_MODEL || environment.CLAUDE_MODEL || "default",
    small_fast_model: environment.ANTHROPIC_SMALL_FAST_MODEL || null,
    disable_nonessential_traffic: environment.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC || null,
  });
  return {
    status: secret || /^[a-f0-9]{64}$/i.test(explicit ?? "") ? "PRESENT" : "MISSING_CREDENTIAL_CONTEXT",
    fingerprint: secret
      ? crypto.createHmac("sha256", secret).update(message).digest("hex")
      : /^[a-f0-9]{64}$/i.test(explicit ?? "")
        ? explicit.toLowerCase()
        : sha256Bytes(message),
    endpoint: safeEndpointContext(environment.ANTHROPIC_BASE_URL),
    model: environment.ANTHROPIC_MODEL || environment.CLAUDE_MODEL || "default",
  };
}

export function clientIdentity(claudeEntry = null) {
  if (!claudeEntry) {
    return {
      status: "MISSING",
      entry: null,
      version: null,
      cli_sha256: null,
      package_manifest_sha256: null,
      package_tree_digest: null,
      tarball_sha256: null,
      package_name: null,
      package_version: null,
      node: null,
      code: "CLAUDE_ENTRY_REQUIRED",
    };
  }
  return validateClaudeDistribution(claudeEntry);
}

export function pythonImportPathIdentity(repoRoot, pythonDetails) {
  const repository = path.resolve(repoRoot);
  return (pythonDetails?.sys_path ?? []).map((entry, index) => {
    const resolved = path.resolve(entry);
    const relative = path.relative(repository, resolved);
    if (relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative))) {
      return {
        index,
        kind: "repository",
        path: relative.split(path.sep).join("/") || ".",
      };
    }
    const tree = hashTree(resolved);
    return {
      index,
      kind: "external",
      path: resolved,
      status: tree.status,
      digest: tree.digest,
    };
  });
}

export function environmentIdentity(repoRoot, environment = process.env) {
  const python = resolvePythonTestRuntime(repoRoot, environment);
  const controlNames = [
    "LANG",
    "LC_ALL",
    "PYTHONHASHSEED",
    "PYTHONHOME",
    "PYTHONIOENCODING",
    "PYTHONPATH",
    "PYTHONUTF8",
    "PYTHONWARNINGS",
    "PYTEST_ADDOPTS",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    "PYTEST_PLUGINS",
    "TZ",
  ];
  return {
    platform: process.platform,
    architecture: process.arch,
    node: process.version,
    kernel: os.release(),
    chrome: chromeIdentity(environment),
    python_test_runtime: python
      ? {
          ...python.identity,
          import_paths: pythonImportPathIdentity(repoRoot, python.details),
          environment_controls: Object.fromEntries(
            controlNames.map((name) => [name, environment[name] === undefined ? null : sha256Bytes(environment[name])]),
          ),
        }
      : { status: "MISSING" },
  };
}

export function computeIdentitySets({
  repoRoot,
  identityConfig,
  externalTrees = {},
  environment = process.env,
  claudeEntry = null,
  claudeSettings = null,
  releaseRuntime = null,
}) {
  const components = {};
  const sharedClient = clientIdentity(claudeEntry);
  const sharedModel = modelContextFingerprint(environment, claudeSettings);
  const sharedEnvironment = environmentIdentity(repoRoot, environment);
  for (const [componentId, definition] of Object.entries(identityConfig.components)) {
    let value;
    if (definition.kind === "paths") {
      const tree = hashConfiguredPaths(repoRoot, definition.paths);
      value = { kind: definition.kind, status: tree.records.some((record) => record.kind === "missing" || record.kind === "unsupported") ? "MISSING" : "PRESENT", digest: tree.digest, records: tree.records };
    } else if (definition.kind === "release-case") {
      const configured = normalizeRepoPath(repoRoot, definition.root);
      if (!fs.existsSync(configured.absolute)) {
        value = { kind: definition.kind, partition: definition.partition, status: "MISSING", root: configured.relative, case_id: null, digest: null, records: [] };
      } else {
        const caseRoot = discoverReleaseCaseRoot(configured.absolute);
        const selected = releaseCasePartition(caseRoot, definition.partition);
        value = {
          kind: definition.kind,
          partition: definition.partition,
          status: "PRESENT",
          root: path.relative(repoRoot, caseRoot).split(path.sep).join("/"),
          case_id: selected.case_id,
          digest: selected.digest,
          records: selected.records,
        };
      }
    } else if (definition.kind === "external-tree") {
      const tree = externalTrees[definition.name]
        ? hashTree(externalTrees[definition.name])
        : { status: "MISSING", root: null, digest: null, records: [] };
      value = { kind: definition.kind, name: definition.name, status: tree.status, root: tree.root ?? null, digest: tree.digest };
    } else if (definition.kind === "client-distribution") {
      value = { kind: definition.kind, ...sharedClient };
    } else if (definition.kind === "claude-settings") {
      value = { kind: definition.kind, ...sharedModel };
    } else if (definition.kind === "release-runtime") {
      value = { kind: definition.kind, value: releaseRuntime };
    } else if (definition.kind === "environment") {
      value = { kind: definition.kind, value: sharedEnvironment };
    } else {
      throw new Error(`IDENTITY_COMPONENT_KIND_UNSUPPORTED:${componentId}`);
    }
    components[componentId] = {
      value,
      digest: sha256Bytes(canonicalJson(value)),
      missing: value.status && value.status !== "PRESENT" ? value.status : null,
    };
  }
  const sets = {};
  for (const [setId, definition] of Object.entries(identityConfig.sets)) {
    const producerComponents = Object.fromEntries(definition.producer.map((componentId) => [componentId, components[componentId].digest]));
    const proofComponents = Object.fromEntries(definition.proof.map((componentId) => [componentId, components[componentId].digest]));
    const producer = { schema_version: 2, identity_set: setId, components: producerComponents };
    const proof = { schema_version: 2, identity_set: setId, producer_digest: sha256Bytes(canonicalJson(producer)), components: proofComponents };
    const all = [...new Set([...definition.producer, ...definition.proof])];
    sets[setId] = {
      producer,
      proof,
      producer_digest: proof.producer_digest,
      proof_digest: sha256Bytes(canonicalJson(proof)),
      missing_inputs: all.filter((componentId) => components[componentId].missing).map((componentId) => ({ component_id: componentId, status: components[componentId].missing })),
    };
  }
  return { components, sets };
}

export function stageIdentity(stage, identitySets, policies) {
  const identitySet = identitySets[stage.identity_set];
  if (!identitySet) throw new Error(`IDENTITY_SET_UNKNOWN:${stage.identity_set}`);
  const stateful = stage.id.startsWith("journey.cross-job.");
  const producerIdentity = sha256Bytes(canonicalJson({
    schema_version: 2,
    identity_set: stage.identity_set,
    set_digest: identitySet.producer_digest,
    parent_checkpoint: stateful ? policies.parent_checkpoint ?? "GENESIS" : "GENESIS",
    scenario: stateful ? policies.scenario ?? "CrossJob" : null,
  }));
  const proofIdentity = sha256Bytes(canonicalJson({
    schema_version: 2,
    producer_identity: producerIdentity,
    set_proof_digest: identitySet.proof_digest,
    stage_definition_digest: policies.stage_definition_digest,
    dependency_proof_identities: [...(policies.dependency_proof_identities ?? [])].sort(),
    config_bundle_digest: policies.config_bundle_digest,
    evidence_contract_version: policies.evidence_contract_version,
    selection: stage.id,
  }));
  return { producer_identity: producerIdentity, proof_identity: proofIdentity };
}

export function performanceIdentity(stage, producerIdentity, performancePolicy) {
  return sha256Bytes(canonicalJson({
    schema_version: 2,
    stage_id: stage.id,
    producer_identity: producerIdentity,
    policy_version: performancePolicy.policy_version,
    progress_class: stage.progress_class,
    stage_policy: performancePolicy.stages[stage.id] ?? performancePolicy.stages["*"],
  }));
}
