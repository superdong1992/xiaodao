import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import {
  canonicalJson,
  ensureDirectory,
  resolveCommand,
  runSync,
  sha256Bytes,
  sha256File,
} from "./util.mjs";

const RUNTIME_PROFILES_PATH = new URL("../config/runtime-profiles.v2.json", import.meta.url);
const RUNTIME_PROFILES = JSON.parse(fs.readFileSync(RUNTIME_PROFILES_PATH, "utf8"));
if (RUNTIME_PROFILES.schema_version !== 2 || RUNTIME_PROFILES.profiles?.release?.kind !== "formal-release") {
  throw new Error("RELEASE_RUNTIME_PROFILE_INVALID");
}
export const RELEASE_RUNTIME_PROFILE = Object.freeze(RUNTIME_PROFILES.profiles.release);
export const RELEASE_CLAUDE_VERSION = RELEASE_RUNTIME_PROFILE.claude.version;
export const RELEASE_CLAUDE_VERSION_OUTPUT = RELEASE_RUNTIME_PROFILE.claude.version_output;
export const RELEASE_CLAUDE_TARBALL_SHA256 = RELEASE_RUNTIME_PROFILE.claude.tarball_sha256;
export const RELEASE_CLAUDE_CLI_SHA256 = RELEASE_RUNTIME_PROFILE.claude.cli_sha256;
export const RELEASE_UV_VERSION = RELEASE_RUNTIME_PROFILE.uv.version;
export const RELEASE_UV_ARCHIVE_SHA256 = RELEASE_RUNTIME_PROFILE.uv.archive_sha256;
export const RELEASE_UV_SHA256 = RELEASE_RUNTIME_PROFILE.uv.uv_sha256;
export const RELEASE_UVX_SHA256 = RELEASE_RUNTIME_PROFILE.uv.uvx_sha256;
export const RELEASE_HATCHLING_VERSION = RELEASE_RUNTIME_PROFILE.hatchling;
export const RELEASE_PYTHON_VERSION = RELEASE_RUNTIME_PROFILE.python;
export const RELEASE_BASE_IMAGE = RELEASE_RUNTIME_PROFILE.base_image.name;
export const RELEASE_BASE_IMAGE_SOURCE = RELEASE_RUNTIME_PROFILE.base_image.source;
export const RELEASE_DOCKER_CONTEXT = RELEASE_RUNTIME_PROFILE.base_image.macos_docker_context;
export const RELEASE_DOCKER_OS = RELEASE_RUNTIME_PROFILE.base_image.os;
export const RELEASE_DOCKER_ARCH = RELEASE_RUNTIME_PROFILE.base_image.architecture;
export const RELEASE_MODEL = RELEASE_RUNTIME_PROFILE.claude.model;
export const RELEASE_LOGPARSE_COMMIT = RELEASE_RUNTIME_PROFILE.external_sources.logparse;
export const RELEASE_MCP_COMMIT = RELEASE_RUNTIME_PROFILE.external_sources.mcp;

export const CLAUDE_SETTINGS_ENV_KEYS = Object.freeze([...RELEASE_RUNTIME_PROFILE.settings_environment_allowlist]);

const MODEL_KEYS = Object.freeze([
  "ANTHROPIC_DEFAULT_HAIKU_MODEL",
  "ANTHROPIC_DEFAULT_OPUS_MODEL",
  "ANTHROPIC_DEFAULT_SONNET_MODEL",
]);

function ordinaryFile(filePath) {
  if (!filePath || !path.isAbsolute(filePath) || !fs.existsSync(filePath)) return false;
  const metadata = fs.lstatSync(filePath);
  return metadata.isFile() && !metadata.isSymbolicLink() && metadata.nlink === 1;
}

function resolvedOrdinaryFile(filePath) {
  if (!filePath || !path.isAbsolute(filePath) || !fs.existsSync(filePath)) return null;
  let resolved;
  try { resolved = fs.realpathSync(filePath); } catch { return null; }
  const metadata = fs.lstatSync(resolved);
  return metadata.isFile() && !metadata.isSymbolicLink() && metadata.nlink === 1 ? resolved : null;
}

function packageRecords(root, current = root, output = []) {
  for (const entry of fs.readdirSync(current, { withFileTypes: true }).sort((left, right) => left.name.localeCompare(right.name))) {
    const absolute = path.join(current, entry.name);
    const relative = path.relative(root, absolute).split(path.sep).join("/");
    const metadata = fs.lstatSync(absolute);
    if (metadata.isSymbolicLink()) {
      output.push({ path: relative, kind: "symlink" });
    } else if (metadata.isDirectory()) {
      output.push({ path: `${relative}/`, kind: "directory", mode: metadata.mode & 0o777 });
      packageRecords(root, absolute, output);
    } else if (metadata.isFile()) {
      output.push({
        path: relative,
        kind: "file",
        mode: metadata.mode & 0o777,
        size: metadata.size,
        nlink: metadata.nlink,
        sha256: sha256File(absolute),
      });
    } else {
      output.push({ path: relative, kind: "unsupported" });
    }
  }
  return output;
}

export function packageTreeIdentity(packageRoot) {
  if (!fs.existsSync(packageRoot) || !fs.statSync(packageRoot).isDirectory()) {
    return { status: "MISSING", digest: null, records: [] };
  }
  const records = packageRecords(packageRoot);
  const invalid = records.filter((entry) => entry.kind === "symlink" || entry.kind === "unsupported" || (entry.kind === "file" && entry.nlink !== 1));
  return {
    status: invalid.length === 0 ? "PRESENT" : "INVALID",
    digest: sha256Bytes(canonicalJson(records)),
    records,
    invalid: invalid.map((entry) => entry.path),
  };
}

function safeSettingsPayload(sourcePath) {
  if (!ordinaryFile(sourcePath)) throw new Error("CLAUDE_SETTINGS_FILE_INVALID");
  let parsed;
  try {
    parsed = JSON.parse(fs.readFileSync(sourcePath, "utf8").replace(/^\uFEFF/, ""));
  } catch {
    throw new Error("CLAUDE_SETTINGS_JSON_INVALID");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("CLAUDE_SETTINGS_ROOT_INVALID");
  const environment = parsed.env;
  if (!environment || typeof environment !== "object" || Array.isArray(environment)) throw new Error("CLAUDE_SETTINGS_ENV_INVALID");
  const actual = Object.keys(environment).sort();
  const expected = [...CLAUDE_SETTINGS_ENV_KEYS].sort();
  if (canonicalJson(actual) !== canonicalJson(expected)) throw new Error("CLAUDE_SETTINGS_ENV_ALLOWLIST_MISMATCH");
  if (!CLAUDE_SETTINGS_ENV_KEYS.every((name) => typeof environment[name] === "string")) throw new Error("CLAUDE_SETTINGS_ENV_VALUE_INVALID");
  if (!environment.ANTHROPIC_AUTH_TOKEN) throw new Error("CLAUDE_SETTINGS_AUTH_MISSING");
  if (!MODEL_KEYS.every((name) => environment[name] === RELEASE_MODEL)) throw new Error("CLAUDE_SETTINGS_MODEL_MISMATCH");
  let endpoint;
  try { endpoint = new URL(environment.ANTHROPIC_BASE_URL); } catch { throw new Error("CLAUDE_SETTINGS_ENDPOINT_INVALID"); }
  if (endpoint.protocol !== "https:" || !endpoint.host || endpoint.username || endpoint.password) throw new Error("CLAUDE_SETTINGS_ENDPOINT_INVALID");
  if (!/^\d+$/.test(environment.API_TIMEOUT_MS) || Number(environment.API_TIMEOUT_MS) <= 0) throw new Error("CLAUDE_SETTINGS_TIMEOUT_INVALID");
  if (environment.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC !== "1") throw new Error("CLAUDE_SETTINGS_TRAFFIC_POLICY_INVALID");
  return { env: Object.fromEntries(CLAUDE_SETTINGS_ENV_KEYS.map((name) => [name, environment[name]])) };
}

export function claudeSettingsIdentity(sourcePath) {
  try {
    const safe = safeSettingsPayload(sourcePath);
    const token = safe.env.ANTHROPIC_AUTH_TOKEN;
    const endpoint = new URL(safe.env.ANTHROPIC_BASE_URL);
    const message = canonicalJson({
      endpoint: `${endpoint.protocol}//${endpoint.host}`,
      models: Object.fromEntries(MODEL_KEYS.map((name) => [name, safe.env[name]])),
      api_timeout_ms: safe.env.API_TIMEOUT_MS,
      disable_nonessential_traffic: safe.env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC,
      policy: "env-allowlist-only-no-hooks-v1",
    });
    return {
      status: "PRESENT",
      source: path.resolve(sourcePath),
      endpoint: `${endpoint.protocol}//${endpoint.host}`,
      model: RELEASE_MODEL,
      fingerprint: crypto.createHmac("sha256", token).update(message).digest("hex"),
      copied_top_level_keys: ["env"],
      copied_env_key_count: CLAUDE_SETTINGS_ENV_KEYS.length,
      hooks_copied: false,
    };
  } catch (error) {
    return {
      status: "INVALID",
      source: sourcePath && path.isAbsolute(sourcePath) ? path.resolve(sourcePath) : null,
      code: String(error?.message ?? error),
      endpoint: null,
      model: null,
      fingerprint: null,
      hooks_copied: false,
    };
  }
}

export function materializeClaudeSettings(sourcePath, targetPath) {
  const safe = safeSettingsPayload(sourcePath);
  ensureDirectory(path.dirname(targetPath));
  const descriptor = fs.openSync(targetPath, "wx", 0o600);
  try {
    fs.writeFileSync(descriptor, canonicalJson(safe), "utf8");
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
  fs.chmodSync(targetPath, 0o600);
  return claudeSettingsIdentity(sourcePath);
}

export function materializeAttemptClaudeSettings(sourcePath, attemptRoot, expectedFingerprint) {
  if (!attemptRoot || !path.isAbsolute(attemptRoot)) throw new Error("CLAUDE_SETTINGS_ATTEMPT_ROOT_INVALID");
  const resolvedAttemptRoot = path.resolve(attemptRoot);
  if (!fs.existsSync(resolvedAttemptRoot) || !fs.statSync(resolvedAttemptRoot).isDirectory()) {
    throw new Error("CLAUDE_SETTINGS_ATTEMPT_ROOT_INVALID");
  }
  if (!expectedFingerprint || typeof expectedFingerprint !== "string") {
    throw new Error("CLAUDE_SETTINGS_FINGERPRINT_REQUIRED");
  }

  const targetPath = path.join(resolvedAttemptRoot, "scratch", "release-inputs", "claude-settings.json");
  if (!fs.existsSync(targetPath)) materializeClaudeSettings(sourcePath, targetPath);
  const identity = claudeSettingsIdentity(targetPath);
  if (identity.status !== "PRESENT" || identity.fingerprint !== expectedFingerprint) {
    throw new Error("CLAUDE_SETTINGS_STAGED_IDENTITY_MISMATCH");
  }
  return { path: targetPath, identity };
}

export function releaseCachePaths(repoRoot, configuredRoot = null) {
  const cacheRoot = path.resolve(configuredRoot ?? path.join(repoRoot, ".tmp", "test-flow-cache"));
  const claudeRoot = path.join(cacheRoot, "claude", RELEASE_CLAUDE_VERSION);
  const uvRoot = path.join(cacheRoot, "uv", RELEASE_UV_VERSION, "linux-x64");
  return {
    cacheRoot,
    claudeRoot,
    claudeEntry: path.join(claudeRoot, "package", "cli.js"),
    claudePackage: path.join(claudeRoot, "package", "package.json"),
    claudeTarball: path.join(claudeRoot, `claude-code-${RELEASE_CLAUDE_VERSION}.tgz`),
    claudeSeal: path.join(claudeRoot, "cache-seal.json"),
    uvRoot,
    uv: path.join(uvRoot, "uv"),
    uvx: path.join(uvRoot, "uvx"),
    uvArchive: path.join(uvRoot, "uv-x86_64-unknown-linux-gnu.tar.gz"),
    uvSeal: path.join(uvRoot, "cache-seal.json"),
    releaseSeal: path.join(cacheRoot, "release-cache-seal.json"),
    baseImage: RELEASE_BASE_IMAGE,
  };
}

export function validateClaudeDistribution(entryPath) {
  const result = {
    status: "INVALID",
    entry: entryPath && path.isAbsolute(entryPath) ? path.resolve(entryPath) : null,
    version: null,
    cli_sha256: null,
    package_manifest_sha256: null,
    package_tree_digest: null,
    tarball_sha256: null,
    package_name: null,
    package_version: null,
    node: {
      executable: process.execPath,
      version: process.version,
      sha256: ordinaryFile(process.execPath) ? sha256File(process.execPath) : null,
    },
    code: null,
  };
  try {
    if (!ordinaryFile(entryPath) || path.basename(entryPath) !== "cli.js") throw new Error("CLAUDE_ENTRY_INVALID");
    const entry = path.resolve(entryPath);
    result.cli_sha256 = sha256File(entry);
    if (result.cli_sha256 !== RELEASE_CLAUDE_CLI_SHA256) throw new Error("CLAUDE_CLI_HASH_MISMATCH");
    const packageRoot = path.dirname(entry);
    const manifestPath = path.join(packageRoot, "package.json");
    if (!ordinaryFile(manifestPath)) throw new Error("CLAUDE_PACKAGE_MANIFEST_INVALID");
    const manifestBytes = fs.readFileSync(manifestPath);
    const manifest = JSON.parse(manifestBytes.toString("utf8"));
    result.package_manifest_sha256 = sha256Bytes(manifestBytes);
    result.package_name = manifest.name ?? null;
    result.package_version = manifest.version ?? null;
    if (manifest.name !== "@anthropic-ai/claude-code" || manifest.version !== RELEASE_CLAUDE_VERSION) throw new Error("CLAUDE_PACKAGE_IDENTITY_MISMATCH");
    const tree = packageTreeIdentity(packageRoot);
    result.package_tree_digest = tree.digest;
    if (tree.status !== "PRESENT") throw new Error("CLAUDE_PACKAGE_TREE_INVALID");
    const cacheRoot = path.dirname(packageRoot);
    const tarballPath = path.join(cacheRoot, `claude-code-${RELEASE_CLAUDE_VERSION}.tgz`);
    const sealPath = path.join(cacheRoot, "cache-seal.json");
    if (!ordinaryFile(tarballPath) || !ordinaryFile(sealPath)) throw new Error("CLAUDE_CACHE_SEAL_MISSING");
    result.tarball_sha256 = sha256File(tarballPath);
    if (result.tarball_sha256 !== RELEASE_CLAUDE_TARBALL_SHA256) throw new Error("CLAUDE_TARBALL_HASH_MISMATCH");
    const seal = JSON.parse(fs.readFileSync(sealPath, "utf8"));
    const sealValid = seal.schema_version === 1
      && seal.kind === "official-claude-npm-cache"
      && seal.package_name === "@anthropic-ai/claude-code"
      && seal.package_version === RELEASE_CLAUDE_VERSION
      && seal.tarball_sha256 === result.tarball_sha256
      && seal.cli_sha256 === result.cli_sha256
      && seal.package_manifest_sha256 === result.package_manifest_sha256
      && seal.package_tree_digest === result.package_tree_digest;
    if (!sealValid) throw new Error("CLAUDE_CACHE_SEAL_INVALID");
    const version = runSync(process.execPath, [entry, "--version"], { cwd: cacheRoot });
    result.version = version.status === 0 ? version.stdout.trim() : null;
    if (version.status !== 0 || result.version !== RELEASE_CLAUDE_VERSION_OUTPUT) throw new Error("CLAUDE_VERSION_MISMATCH");
    result.status = "PRESENT";
    return result;
  } catch (error) {
    result.code = String(error?.message ?? error);
    return result;
  }
}

export function validateUvCache(paths) {
  try {
    if (![paths.uv, paths.uvx, paths.uvArchive, paths.uvSeal].every(ordinaryFile)) throw new Error("UV_CACHE_FILE_MISSING");
    const actual = {
      archive_sha256: sha256File(paths.uvArchive),
      uv_sha256: sha256File(paths.uv),
      uvx_sha256: sha256File(paths.uvx),
    };
    if (actual.archive_sha256 !== RELEASE_UV_ARCHIVE_SHA256 || actual.uv_sha256 !== RELEASE_UV_SHA256 || actual.uvx_sha256 !== RELEASE_UVX_SHA256) {
      throw new Error("UV_CACHE_HASH_MISMATCH");
    }
    const seal = JSON.parse(fs.readFileSync(paths.uvSeal, "utf8"));
    if (seal.schema_version !== 1 || seal.kind !== "official-uv-release-cache" || seal.version !== RELEASE_UV_VERSION
      || seal.archive_sha256 !== actual.archive_sha256 || seal.uv_sha256 !== actual.uv_sha256 || seal.uvx_sha256 !== actual.uvx_sha256) {
      throw new Error("UV_CACHE_SEAL_INVALID");
    }
    return { status: "PRESENT", ...actual };
  } catch (error) {
    return { status: "INVALID", code: String(error?.message ?? error) };
  }
}

export function externalGitIdentity(sourceRoot, expectedCommit) {
  const result = { status: "INVALID", root: sourceRoot && path.isAbsolute(sourceRoot) ? path.resolve(sourceRoot) : null, head: null, clean: false, code: null };
  try {
    if (!sourceRoot || !path.isAbsolute(sourceRoot) || !fs.existsSync(sourceRoot) || !fs.statSync(sourceRoot).isDirectory()) throw new Error("SOURCE_ROOT_INVALID");
    const head = runSync("git", ["-C", sourceRoot, "rev-parse", "HEAD"]);
    const status = runSync("git", ["-C", sourceRoot, "status", "--porcelain=v1", "--untracked-files=all"]);
    result.head = head.status === 0 ? head.stdout.trim() : null;
    result.clean = status.status === 0 && status.stdout.length === 0;
    if (result.head !== expectedCommit) throw new Error("SOURCE_COMMIT_MISMATCH");
    if (!result.clean) throw new Error("SOURCE_TREE_DIRTY");
    result.status = "PRESENT";
    return result;
  } catch (error) {
    result.code = String(error?.message ?? error);
    return result;
  }
}

export function dockerServerIdentity(contextName) {
  const result = {
    status: "INVALID",
    context: contextName ?? null,
    os: null,
    architecture: null,
    version: null,
    server_id: null,
    docker_cli: null,
    docker_cli_sha256: null,
    context_fingerprint: null,
    colima_version: null,
    colima_status_fingerprint: null,
    code: null,
  };
  try {
    if (contextName !== RELEASE_DOCKER_CONTEXT) throw new Error("DOCKER_CONTEXT_MISMATCH");
    const dockerCommand = resolveCommand("docker");
    const docker = resolvedOrdinaryFile(dockerCommand);
    if (!docker) throw new Error("DOCKER_CLIENT_MISSING");
    result.docker_cli = docker;
    result.docker_cli_sha256 = sha256File(result.docker_cli);
    const server = runSync(docker, ["--context", contextName, "version", "--format", "{{json .Server}}"]);
    if (server.status !== 0) throw new Error("DOCKER_SERVER_UNAVAILABLE");
    const metadata = JSON.parse(server.stdout);
    result.os = String(metadata.Os ?? metadata.OsType ?? "").toLowerCase();
    result.architecture = String(metadata.Arch ?? metadata.Architecture ?? "").toLowerCase();
    result.version = metadata.Version ?? null;
    result.server_id = metadata.ID ?? null;
    if (result.os !== RELEASE_DOCKER_OS) throw new Error("DOCKER_SERVER_OS_MISMATCH");
    if (!["amd64", "x86_64"].includes(result.architecture)) throw new Error("DOCKER_SERVER_ARCH_MISMATCH");
    result.architecture = RELEASE_DOCKER_ARCH;
    const inspected = runSync(docker, ["context", "inspect", contextName]);
    if (inspected.status !== 0) throw new Error("DOCKER_CONTEXT_INSPECT_FAILED");
    result.context_fingerprint = sha256Bytes(inspected.stdout);
    const colimaCommand = resolveCommand("colima");
    const colima = resolvedOrdinaryFile(colimaCommand);
    if (!colima) throw new Error("COLIMA_CLIENT_MISSING");
    const colimaVersion = runSync(colima, ["version"]);
    if (colimaVersion.status !== 0) throw new Error("COLIMA_VERSION_FAILED");
    result.colima_version = colimaVersion.stdout.trim().split(/\r?\n/, 1)[0];
    const colimaStatus = runSync(colima, ["status", "--json"]);
    if (colimaStatus.status !== 0) throw new Error("COLIMA_STATUS_FAILED");
    result.colima_status_fingerprint = sha256Bytes(colimaStatus.stdout);
    result.status = "PRESENT";
    return result;
  } catch (error) {
    result.code = String(error?.message ?? error);
    return result;
  }
}

export function validateReleaseImage(paths, dockerIdentity) {
  try {
    if (dockerIdentity?.status !== "PRESENT") throw new Error("DOCKER_IDENTITY_INVALID");
    if (!ordinaryFile(paths.releaseSeal)) throw new Error("RELEASE_CACHE_SEAL_MISSING");
    const seal = JSON.parse(fs.readFileSync(paths.releaseSeal, "utf8"));
    const inspect = runSync(dockerIdentity.docker_cli, ["--context", dockerIdentity.context, "image", "inspect", paths.baseImage]);
    if (inspect.status !== 0) throw new Error("RELEASE_BASE_IMAGE_MISSING");
    const metadata = JSON.parse(inspect.stdout)[0];
    const labels = metadata.Config?.Labels ?? {};
    if (metadata.Os !== RELEASE_DOCKER_OS || metadata.Architecture !== RELEASE_DOCKER_ARCH) throw new Error("RELEASE_BASE_IMAGE_PLATFORM_MISMATCH");
    if (labels["problem-locator.e2e.claude"] !== `npm-${RELEASE_CLAUDE_VERSION}`
      || labels["problem-locator.e2e.uv"] !== RELEASE_UV_VERSION
      || labels["problem-locator.e2e.hatchling"] !== RELEASE_HATCHLING_VERSION) {
      throw new Error("RELEASE_BASE_IMAGE_LABEL_MISMATCH");
    }
    const valid = seal.schema_version === 1
      && seal.kind === "macos-linux-release-cache"
      && seal.image === paths.baseImage
      && seal.image_id === metadata.Id
      && seal.platform === "linux/amd64"
      && seal.claude_tarball_sha256 === RELEASE_CLAUDE_TARBALL_SHA256
      && seal.claude_cli_sha256 === RELEASE_CLAUDE_CLI_SHA256
      && seal.uv_archive_sha256 === RELEASE_UV_ARCHIVE_SHA256
      && seal.uv_sha256 === RELEASE_UV_SHA256
      && seal.uvx_sha256 === RELEASE_UVX_SHA256
      && seal.hatchling_version === RELEASE_HATCHLING_VERSION;
    if (!valid) throw new Error("RELEASE_CACHE_SEAL_INVALID");
    return { status: "PRESENT", image: paths.baseImage, image_id: metadata.Id, platform: "linux/amd64", seal_sha256: sha256File(paths.releaseSeal) };
  } catch (error) {
    return { status: "INVALID", image: paths.baseImage, image_id: null, platform: null, seal_sha256: null, code: String(error?.message ?? error) };
  }
}
