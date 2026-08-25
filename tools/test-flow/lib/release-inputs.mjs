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
export const RELEASE_UV_VERSION_OUTPUT = RELEASE_RUNTIME_PROFILE.uv.version_output;
export const RELEASE_UVX_VERSION_OUTPUT = RELEASE_RUNTIME_PROFILE.uv.uvx_version_output;
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

// The explicit Linux Client uses the smaller official Chrome Headless Shell
// artifact. Host-client Releases continue to discover ordinary Google Chrome
// through browser.mjs and do not consume this cache identity.
export const RELEASE_CHROME_HEADLESS_SHELL_VERSION = "152.0.7977.54";
export const RELEASE_CHROME_HEADLESS_SHELL_PRODUCT = "Chrome Headless Shell for Testing";
export const RELEASE_CHROME_HEADLESS_SHELL_VERSION_OUTPUT = `Google Chrome for Testing ${RELEASE_CHROME_HEADLESS_SHELL_VERSION}`;
export const RELEASE_CHROME_HEADLESS_SHELL_ARCHIVE_SHA256 = "11cedb5568cd374a76eb738e40bd434cd0c9956820fb406b8bd9edca53428d3e";
export const RELEASE_CHROME_HEADLESS_SHELL_EXECUTABLE_SHA256 = "8a3f72f9676736c45e94ae3279b4e2e6a1e323187f9a5e73c9a760e8cc1296ea";
export const RELEASE_CHROME_HEADLESS_SHELL_PLATFORM = "linux64";
export const RELEASE_CLIENT_IMAGE = `${RELEASE_BASE_IMAGE}-client-cft-headless-shell-${RELEASE_CHROME_HEADLESS_SHELL_VERSION}`;
const RELEASE_CLIENT_BROWSER_SMOKE_PROFILE = "chrome-headless-shell-plan-smoke-v1";
const RELEASE_CLIENT_BROWSER_EXECUTABLE = "/opt/chrome-headless-shell/chrome-headless-shell";
const RELEASE_CLIENT_BROWSER_SMOKE_CHALLENGE = sha256Bytes("problem-locator-linux-client-headless-shell-plan-smoke-v1");

export const CLAUDE_SETTINGS_ENV_KEYS = Object.freeze([...RELEASE_RUNTIME_PROFILE.settings_environment_allowlist]);
const DOCKER_CONTEXT = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const DOCKER_IDENTITY_SHA256 = /^[a-f0-9]{64}$/;
const DOCKER_AMBIENT_ENVIRONMENT = Object.freeze([
  "DOCKER_API_VERSION",
  "DOCKER_CONTEXT",
  "DOCKER_HOST",
  "DOCKER_TLS_VERIFY",
  "DOCKER_CERT_PATH",
]);

export function dockerContextArgs(contextName, args) {
  const context = contextName ?? "default";
  if (!DOCKER_CONTEXT.test(context) || !Array.isArray(args) || args.some((item) => typeof item !== "string")) {
    throw new Error("DOCKER_CONTEXT_ARGUMENTS_INVALID");
  }
  return context === "default" ? [...args] : ["--context", context, ...args];
}

export function sameDockerRuntimeIdentity(expected, observed) {
  const requiredFields = [
    "context",
    "effective_context",
    "server_id",
    "os",
    "architecture",
    "version",
    "context_fingerprint",
    "docker_cli_sha256",
  ];
  return expected?.status === "PRESENT"
    && observed?.status === "PRESENT"
    && requiredFields.every((field) => (
      typeof expected[field] === "string"
      && expected[field].length > 0
      && typeof observed[field] === "string"
      && observed[field].length > 0
      && expected[field] === observed[field]
    ))
    && DOCKER_IDENTITY_SHA256.test(expected.context_fingerprint)
    && DOCKER_IDENTITY_SHA256.test(observed.context_fingerprint)
    && DOCKER_IDENTITY_SHA256.test(expected.docker_cli_sha256)
    && DOCKER_IDENTITY_SHA256.test(observed.docker_cli_sha256);
}

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
  const chromeHeadlessShellRoot = path.join(
    cacheRoot,
    "chrome-headless-shell-for-testing",
    RELEASE_CHROME_HEADLESS_SHELL_VERSION,
    RELEASE_CHROME_HEADLESS_SHELL_PLATFORM,
  );
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
    chromeHeadlessShellRoot,
    chromeHeadlessShellArchive: path.join(
      chromeHeadlessShellRoot,
      `chrome-headless-shell-${RELEASE_CHROME_HEADLESS_SHELL_PLATFORM}-${RELEASE_CHROME_HEADLESS_SHELL_VERSION}.zip`,
    ),
    chromeHeadlessShellDistribution: path.join(
      chromeHeadlessShellRoot,
      `chrome-headless-shell-${RELEASE_CHROME_HEADLESS_SHELL_PLATFORM}`,
    ),
    chromeHeadlessShellExecutable: path.join(
      chromeHeadlessShellRoot,
      `chrome-headless-shell-${RELEASE_CHROME_HEADLESS_SHELL_PLATFORM}`,
      "chrome-headless-shell",
    ),
    chromeHeadlessShellSeal: path.join(chromeHeadlessShellRoot, "cache-seal.json"),
    releaseSeal: path.join(cacheRoot, "release-cache-seal.json"),
    baseImage: RELEASE_BASE_IMAGE,
    clientImage: RELEASE_CLIENT_IMAGE,
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

export function validateChromeHeadlessShellCache(paths) {
  try {
    if (![paths.chromeHeadlessShellArchive, paths.chromeHeadlessShellExecutable, paths.chromeHeadlessShellSeal].every(ordinaryFile)) {
      throw new Error("CHROME_HEADLESS_SHELL_CACHE_FILE_MISSING");
    }
    fs.accessSync(paths.chromeHeadlessShellExecutable, fs.constants.X_OK);
    const actual = {
      archive_sha256: sha256File(paths.chromeHeadlessShellArchive),
      executable_sha256: sha256File(paths.chromeHeadlessShellExecutable),
    };
    if (actual.archive_sha256 !== RELEASE_CHROME_HEADLESS_SHELL_ARCHIVE_SHA256
      || actual.executable_sha256 !== RELEASE_CHROME_HEADLESS_SHELL_EXECUTABLE_SHA256) {
      throw new Error("CHROME_HEADLESS_SHELL_CACHE_HASH_MISMATCH");
    }
    const seal = JSON.parse(fs.readFileSync(paths.chromeHeadlessShellSeal, "utf8"));
    if (seal.schema_version !== 1
      || seal.kind !== "official-chrome-headless-shell-for-testing-cache"
      || seal.product !== RELEASE_CHROME_HEADLESS_SHELL_PRODUCT
      || seal.version !== RELEASE_CHROME_HEADLESS_SHELL_VERSION
      || seal.platform !== RELEASE_CHROME_HEADLESS_SHELL_PLATFORM
      || seal.archive_sha256 !== actual.archive_sha256
      || seal.executable_sha256 !== actual.executable_sha256) {
      throw new Error("CHROME_HEADLESS_SHELL_CACHE_SEAL_INVALID");
    }
    return {
      status: "PRESENT",
      product: RELEASE_CHROME_HEADLESS_SHELL_PRODUCT,
      version: RELEASE_CHROME_HEADLESS_SHELL_VERSION_OUTPUT,
      platform: RELEASE_CHROME_HEADLESS_SHELL_PLATFORM,
      ...actual,
    };
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

function runtimeTreeRecords(root, current = root, records = []) {
  for (const entry of fs.readdirSync(current, { withFileTypes: true }).sort((left, right) => left.name.localeCompare(right.name))) {
    const absolute = path.join(current, entry.name);
    const relative = path.relative(root, absolute).split(path.sep).join("/");
    const metadata = fs.lstatSync(absolute);
    if (metadata.isSymbolicLink()) {
      records.push({
        path: relative,
        kind: "symlink",
        mode: metadata.mode & 0o777,
        target: fs.readlinkSync(absolute),
      });
    } else if (metadata.isDirectory()) {
      records.push({ path: `${relative}/`, kind: "directory", mode: metadata.mode & 0o777 });
      runtimeTreeRecords(root, absolute, records);
    } else if (metadata.isFile()) {
      records.push({
        path: relative,
        kind: "file",
        mode: metadata.mode & 0o777,
        nlink: metadata.nlink,
        size: metadata.size,
        sha256: sha256File(absolute),
      });
    } else {
      records.push({ path: relative, kind: "unsupported", mode: metadata.mode & 0o777 });
    }
  }
  return records;
}

function runtimeImportPathIdentities(entries, venvRoot) {
  const venv = fs.realpathSync.native(venvRoot);
  const coveredRoots = [];
  return entries.map((entry, index) => {
    const resolved = path.resolve(entry);
    const pathSha256 = sha256Bytes(resolved);
    if (!fs.existsSync(resolved)) return { index, status: "MISSING", path_sha256: pathSha256 };
    const real = fs.realpathSync.native(resolved);
    if (real === venv || real.startsWith(`${venv}${path.sep}`)) {
      return {
        index,
        status: "COVERED_BY_VENV",
        path_sha256: pathSha256,
        relative: path.relative(venv, real).split(path.sep).join("/") || ".",
      };
    }
    const covering = coveredRoots.findIndex((root) => real === root.path || real.startsWith(`${root.path}${path.sep}`));
    if (covering !== -1) {
      return { index, status: "COVERED_BY_EXTERNAL_ROOT", path_sha256: pathSha256, covered_by_index: coveredRoots[covering].index };
    }
    const metadata = fs.lstatSync(real);
    if (metadata.isFile() && !metadata.isSymbolicLink() && metadata.nlink === 1) {
      return {
        index,
        status: "PRESENT",
        kind: "file",
        path_sha256: pathSha256,
        real_path_sha256: sha256Bytes(real),
        size: metadata.size,
        sha256: sha256File(real),
      };
    }
    if (!metadata.isDirectory() || metadata.isSymbolicLink()) {
      return { index, status: "INVALID", path_sha256: pathSha256, kind: "unsupported" };
    }
    const records = runtimeTreeRecords(real);
    if (records.some((record) => record.kind === "unsupported")) {
      return { index, status: "INVALID", path_sha256: pathSha256, kind: "directory" };
    }
    const fileRecords = records.filter((record) => record.kind === "file");
    coveredRoots.push({ path: real, index });
    return {
      index,
      status: "PRESENT",
      kind: "directory",
      path_sha256: pathSha256,
      real_path_sha256: sha256Bytes(real),
      tree_sha256: sha256Bytes(canonicalJson(records)),
      record_count: records.length,
      file_count: fileRecords.length,
      byte_count: fileRecords.reduce((sum, record) => sum + record.size, 0),
    };
  });
}

// The Codex exploration proof executes Logparse through the source checkout's
// private virtual environment.  Git identity intentionally ignores .venv, so
// freeze the exact runtime bytes as a separate Release input instead of
// allowing an ambient interpreter or dependency update to reuse an identity.
export function codexLogparseRuntimeIdentity(sourceRoot) {
  const result = {
    schema_version: 1,
    status: "INVALID",
    root: sourceRoot && path.isAbsolute(sourceRoot) ? path.resolve(sourceRoot) : null,
    cli: null,
    venv: null,
    python: null,
    code: null,
  };
  try {
    if (!sourceRoot || !path.isAbsolute(sourceRoot)) throw new Error("CODEX_LOGPARSE_ROOT_INVALID");
    const root = path.resolve(sourceRoot);
    if (!fs.existsSync(root) || !fs.lstatSync(root).isDirectory() || fs.realpathSync.native(root) !== root) {
      throw new Error("CODEX_LOGPARSE_ROOT_INVALID");
    }
    const cliPath = path.join(root, "cli.py");
    if (!ordinaryFile(cliPath)) throw new Error("CODEX_LOGPARSE_CLI_INVALID");
    const venvRoot = path.join(root, ".venv");
    if (!fs.existsSync(venvRoot) || !fs.lstatSync(venvRoot).isDirectory() || fs.lstatSync(venvRoot).isSymbolicLink()) {
      throw new Error("CODEX_LOGPARSE_VENV_INVALID");
    }
    const records = runtimeTreeRecords(venvRoot);
    if (records.some((record) => record.kind === "unsupported")) throw new Error("CODEX_LOGPARSE_VENV_NODE_INVALID");
    const pythonEntry = path.join(venvRoot, "bin", "python");
    const pythonTarget = resolvedOrdinaryFile(pythonEntry);
    if (!pythonTarget) throw new Error("CODEX_LOGPARSE_PYTHON_INVALID");
    const probeProgram = [
      "import json,platform,sys,sysconfig",
      "print(json.dumps({'version': platform.python_version(), 'version_full': sys.version, 'implementation': sys.implementation.name, 'cache_tag': sys.implementation.cache_tag, 'executable': sys.executable, 'prefix': sys.prefix, 'base_prefix': sys.base_prefix, 'exec_prefix': sys.exec_prefix, 'base_exec_prefix': sys.base_exec_prefix, 'platform': sysconfig.get_platform(), 'sys_path': sys.path}, sort_keys=True))",
    ].join(";");
    const versionProbe = runSync(pythonEntry, ["-I", "-c", probeProgram], { cwd: root, maxBuffer: 1024 * 1024 });
    if (versionProbe.status !== 0) throw new Error("CODEX_LOGPARSE_PYTHON_VERSION_INVALID");
    let pythonProbe;
    try { pythonProbe = JSON.parse(versionProbe.stdout.trim()); } catch { throw new Error("CODEX_LOGPARSE_PYTHON_PROBE_INVALID"); }
    if (!pythonProbe || typeof pythonProbe.version !== "string" || !Array.isArray(pythonProbe.sys_path)
      || fs.realpathSync.native(pythonProbe.executable) !== pythonTarget
      || fs.realpathSync.native(pythonProbe.prefix) !== fs.realpathSync.native(venvRoot)) {
      throw new Error("CODEX_LOGPARSE_PYTHON_PROBE_INVALID");
    }
    const importPaths = runtimeImportPathIdentities(pythonProbe.sys_path, venvRoot);
    if (importPaths.some((entry) => entry.status === "INVALID")) throw new Error("CODEX_LOGPARSE_PYTHON_IMPORT_PATH_INVALID");
    const version = `Python ${pythonProbe.version}`;
    const fileRecords = records.filter((record) => record.kind === "file");
    result.cli = {
      path: "cli.py",
      size: fs.statSync(cliPath).size,
      sha256: sha256File(cliPath),
    };
    result.venv = {
      path: ".venv",
      tree_sha256: sha256Bytes(canonicalJson(records)),
      record_count: records.length,
      file_count: fileRecords.length,
      directory_count: records.filter((record) => record.kind === "directory").length,
      symlink_count: records.filter((record) => record.kind === "symlink").length,
      byte_count: fileRecords.reduce((sum, record) => sum + record.size, 0),
    };
    result.python = {
      entry: ".venv/bin/python",
      entry_kind: fs.lstatSync(pythonEntry).isSymbolicLink() ? "symlink" : "file",
      entry_target: fs.lstatSync(pythonEntry).isSymbolicLink() ? fs.readlinkSync(pythonEntry) : null,
      resolved_path_sha256: sha256Bytes(pythonTarget),
      resolved_size: fs.statSync(pythonTarget).size,
      resolved_sha256: sha256File(pythonTarget),
      version,
      runtime: {
        implementation: pythonProbe.implementation,
        cache_tag: pythonProbe.cache_tag,
        platform: pythonProbe.platform,
        version_full_sha256: sha256Bytes(pythonProbe.version_full),
        executable_path_sha256: sha256Bytes(path.resolve(pythonProbe.executable)),
        prefix_path_sha256: sha256Bytes(path.resolve(pythonProbe.prefix)),
        base_prefix_path_sha256: sha256Bytes(path.resolve(pythonProbe.base_prefix)),
        exec_prefix_path_sha256: sha256Bytes(path.resolve(pythonProbe.exec_prefix)),
        base_exec_prefix_path_sha256: sha256Bytes(path.resolve(pythonProbe.base_exec_prefix)),
        import_paths: importPaths,
      },
    };
    result.status = "PRESENT";
    return result;
  } catch (error) {
    result.code = String(error?.message ?? error);
    return result;
  }
}

export function dockerServerIdentity(contextName, {
  commandResolver = resolveCommand,
  fileResolver = resolvedOrdinaryFile,
  commandRunner = runSync,
  fileHasher = sha256File,
  environment = process.env,
} = {}) {
  const effectiveContext = contextName ?? "default";
  const result = {
    status: "INVALID",
    context: effectiveContext,
    os: null,
    architecture: null,
    version: null,
    server_id: null,
    docker_cli: null,
    docker_cli_sha256: null,
    context_fingerprint: null,
    effective_context: null,
    ambient_environment_fingerprint: null,
    colima_version: null,
    colima_status_fingerprint: null,
    code: null,
  };
  try {
    if (!DOCKER_CONTEXT.test(effectiveContext)) throw new Error("DOCKER_CONTEXT_INVALID");
    const dockerCommand = commandResolver("docker");
    const docker = fileResolver(dockerCommand);
    if (!docker) throw new Error("DOCKER_CLIENT_MISSING");
    result.docker_cli = docker;
    result.docker_cli_sha256 = fileHasher(result.docker_cli);
    const server = commandRunner(docker, dockerContextArgs(effectiveContext, ["version", "--format", "{{json .Server}}"]));
    if (server.status !== 0) throw new Error("DOCKER_SERVER_UNAVAILABLE");
    const info = commandRunner(docker, dockerContextArgs(effectiveContext, ["info", "--format", "{{json .}}"]));
    if (info.status !== 0) throw new Error("DOCKER_SERVER_INFO_UNAVAILABLE");
    let versionMetadata;
    let infoMetadata;
    try {
      versionMetadata = JSON.parse(server.stdout);
      infoMetadata = JSON.parse(info.stdout);
    } catch {
      throw new Error("DOCKER_SERVER_METADATA_INVALID");
    }
    const normalizeArchitecture = (value) => {
      const architecture = String(value ?? "").toLowerCase();
      return architecture === "x86_64" ? "amd64" : architecture;
    };
    const versionOs = String(versionMetadata.Os ?? versionMetadata.OsType ?? "").toLowerCase();
    const infoOs = String(infoMetadata.OSType ?? infoMetadata.Os ?? "").toLowerCase();
    const versionArchitecture = normalizeArchitecture(versionMetadata.Arch ?? versionMetadata.Architecture);
    const infoArchitecture = normalizeArchitecture(infoMetadata.Architecture ?? infoMetadata.Arch);
    const versionValue = typeof versionMetadata.Version === "string" ? versionMetadata.Version.trim() : "";
    const infoVersion = typeof infoMetadata.ServerVersion === "string" ? infoMetadata.ServerVersion.trim() : "";
    const serverId = typeof infoMetadata.ID === "string" ? infoMetadata.ID.trim() : "";
    if (!serverId) throw new Error("DOCKER_SERVER_ID_MISSING");
    if (!versionOs || !infoOs || versionOs !== infoOs
      || !versionArchitecture || !infoArchitecture || versionArchitecture !== infoArchitecture
      || !versionValue || !infoVersion || versionValue !== infoVersion) {
      throw new Error("DOCKER_SERVER_METADATA_MISMATCH");
    }
    result.os = infoOs;
    result.architecture = infoArchitecture;
    result.version = infoVersion;
    result.server_id = serverId;
    if (result.os !== RELEASE_DOCKER_OS) throw new Error("DOCKER_SERVER_OS_MISMATCH");
    if (result.architecture !== RELEASE_DOCKER_ARCH) throw new Error("DOCKER_SERVER_ARCH_MISMATCH");
    const currentContext = effectiveContext === "default"
      ? commandRunner(docker, ["context", "show"])
      : { status: 0, stdout: `${effectiveContext}\n` };
    if (currentContext.status !== 0 || !currentContext.stdout.trim()) throw new Error("DOCKER_CONTEXT_SHOW_FAILED");
    result.effective_context = currentContext.stdout.trim();
    const inspected = commandRunner(
      docker,
      effectiveContext === "default" ? ["context", "inspect"] : ["context", "inspect", effectiveContext],
    );
    if (inspected.status !== 0) throw new Error("DOCKER_CONTEXT_INSPECT_FAILED");
    let inspectedContexts;
    try { inspectedContexts = JSON.parse(inspected.stdout); } catch { throw new Error("DOCKER_CONTEXT_INSPECT_INVALID"); }
    const ambientEnvironment = Object.fromEntries(DOCKER_AMBIENT_ENVIRONMENT.map((name) => [
      name,
      environment[name] === undefined ? null : sha256Bytes(environment[name]),
    ]));
    result.ambient_environment_fingerprint = sha256Bytes(canonicalJson(ambientEnvironment));
    if (effectiveContext === RELEASE_DOCKER_CONTEXT) {
      const colimaCommand = commandResolver("colima");
      const colima = fileResolver(colimaCommand);
      if (!colima) throw new Error("COLIMA_CLIENT_MISSING");
      const colimaVersion = commandRunner(colima, ["version"]);
      if (colimaVersion.status !== 0) throw new Error("COLIMA_VERSION_FAILED");
      result.colima_version = colimaVersion.stdout.trim().split(/\r?\n/, 1)[0];
      const colimaStatus = commandRunner(colima, ["status", "--json"]);
      if (colimaStatus.status !== 0) throw new Error("COLIMA_STATUS_FAILED");
      result.colima_status_fingerprint = sha256Bytes(colimaStatus.stdout);
    }
    result.context_fingerprint = sha256Bytes(canonicalJson({
      requested_context: effectiveContext,
      effective_context: result.effective_context,
      inspected_contexts: inspectedContexts,
      ambient_environment: ambientEnvironment,
      colima: effectiveContext === RELEASE_DOCKER_CONTEXT ? {
        version: result.colima_version,
        status_fingerprint: result.colima_status_fingerprint,
      } : null,
    }));
    result.status = "PRESENT";
    return result;
  } catch (error) {
    result.code = String(error?.message ?? error);
    return result;
  }
}

export function validatePortableReleaseServerImage(paths, dockerIdentity) {
  try {
    if (dockerIdentity?.status !== "PRESENT") throw new Error("DOCKER_IDENTITY_INVALID");
    const metadata = inspectReleaseImage(dockerIdentity, paths.baseImage);
    const labels = metadata.Config?.Labels ?? {};
    if (labels["problem-locator.e2e.claude"] !== `npm-${RELEASE_CLAUDE_VERSION}`
      || labels["problem-locator.e2e.uv"] !== RELEASE_UV_VERSION
      || labels["problem-locator.e2e.hatchling"] !== RELEASE_HATCHLING_VERSION) {
      throw new Error("RELEASE_BASE_IMAGE_LABEL_MISMATCH");
    }
    return {
      status: "PRESENT",
      image: paths.baseImage,
      image_id: metadata.Id,
      server: { image: paths.baseImage, image_id: metadata.Id, platform: "linux/amd64" },
      client: null,
      browser: null,
      platform: "linux/amd64",
      seal_sha256: null,
      validation_level: "exact-image-id-pending-capability-runtime-probe",
    };
  } catch (error) {
    return {
      status: "INVALID",
      image: paths.baseImage,
      image_id: null,
      server: null,
      client: null,
      browser: null,
      platform: null,
      seal_sha256: null,
      validation_level: null,
      code: String(error?.message ?? error),
    };
  }
}

function inspectReleaseImage(dockerIdentity, image) {
  const inspect = runSync(
    dockerIdentity.docker_cli,
    dockerContextArgs(dockerIdentity.context, ["image", "inspect", image]),
  );
  if (inspect.status !== 0) throw new Error(`RELEASE_IMAGE_MISSING:${image}`);
  const metadata = JSON.parse(inspect.stdout)[0];
  if (metadata.Os !== RELEASE_DOCKER_OS || metadata.Architecture !== RELEASE_DOCKER_ARCH) {
    throw new Error(`RELEASE_IMAGE_PLATFORM_MISMATCH:${image}`);
  }
  return metadata;
}

export function probeReleaseClientHeadlessShell({
  dockerIdentity,
  clientImageId,
  commandRunner = runSync,
  uid = typeof process.getuid === "function" ? process.getuid() : null,
  gid = typeof process.getgid === "function" ? process.getgid() : null,
} = {}) {
  if (dockerIdentity?.status !== "PRESENT"
    || typeof dockerIdentity.docker_cli !== "string"
    || dockerIdentity.docker_cli.length === 0
    || !/^sha256:[a-f0-9]{64}$/.test(clientImageId ?? "")) {
    throw new Error("RELEASE_CLIENT_HEADLESS_SHELL_SMOKE_IDENTITY_INVALID");
  }
  if (!Number.isSafeInteger(uid) || uid <= 0 || !Number.isSafeInteger(gid) || gid < 0) {
    throw new Error("RELEASE_CLIENT_HEADLESS_SHELL_SMOKE_NON_ROOT_REQUIRED");
  }
  const page = `<html><head><title>${RELEASE_CLIENT_BROWSER_SMOKE_CHALLENGE}</title></head><body>${RELEASE_CLIENT_BROWSER_SMOKE_CHALLENGE}</body></html>`;
  const result = commandRunner(
    dockerIdentity.docker_cli,
    dockerContextArgs(dockerIdentity.context, [
      "run", "--rm", "--init", "--pull", "never",
      "--platform", "linux/amd64",
      "--network", "none",
      "--read-only",
      "--user", `${uid}:${gid}`,
      "--env", "HOME=/client-home",
      "--tmpfs", `/client-home:rw,noexec,nosuid,nodev,mode=0700,uid=${uid},gid=${gid},size=536870912`,
      "--tmpfs", "/tmp:rw,exec,nosuid,nodev,mode=1777,size=1073741824",
      clientImageId,
      "/usr/bin/timeout", "45s",
      RELEASE_CLIENT_BROWSER_EXECUTABLE,
      "--no-sandbox",
      "--disable-background-networking",
      "--no-first-run",
      "--no-proxy-server",
      "--user-data-dir=/tmp/test-flow-headless-shell-plan-smoke",
      "--dump-dom",
      `data:text/html,${encodeURIComponent(page)}`,
    ]),
  );
  if (result.status !== 0 || result.signal !== null) {
    const disposition = result.signal ?? (Number.isSafeInteger(result.status) ? `EXIT_${result.status}` : "UNOBSERVED");
    throw new Error(`RELEASE_CLIENT_HEADLESS_SHELL_SMOKE_${disposition}`);
  }
  const stdout = String(result.stdout ?? "");
  if (!stdout.includes(RELEASE_CLIENT_BROWSER_SMOKE_CHALLENGE)) {
    throw new Error("RELEASE_CLIENT_HEADLESS_SHELL_SMOKE_RESULT_MISSING");
  }
  return {
    schema_version: 1,
    status: "PASS",
    code: null,
    kind: "linux-client-headless-shell-plan-smoke",
    argument_profile: RELEASE_CLIENT_BROWSER_SMOKE_PROFILE,
    execution_layer: "docker-run-client-image",
    network_scope: "none",
    image_id: clientImageId,
    execution_user: { uid, gid, root: false },
    executable: RELEASE_CLIENT_BROWSER_EXECUTABLE,
    challenge_sha256: RELEASE_CLIENT_BROWSER_SMOKE_CHALLENGE,
    stdout: {
      byte_count: Buffer.byteLength(stdout),
      sha256: sha256Bytes(stdout),
      truncated: false,
    },
    launcher: { exit_code: 0, signal: null, retries: 0 },
  };
}

export function validateReleaseImage(paths, dockerIdentity, { requireClientImage = false } = {}) {
  try {
    if (dockerIdentity?.status !== "PRESENT") throw new Error("DOCKER_IDENTITY_INVALID");
    if (!ordinaryFile(paths.releaseSeal)) throw new Error("RELEASE_CACHE_SEAL_MISSING");
    const seal = JSON.parse(fs.readFileSync(paths.releaseSeal, "utf8"));
    const metadata = inspectReleaseImage(dockerIdentity, paths.baseImage);
    const labels = metadata.Config?.Labels ?? {};
    if (labels["problem-locator.e2e.claude"] !== `npm-${RELEASE_CLAUDE_VERSION}`
      || labels["problem-locator.e2e.uv"] !== RELEASE_UV_VERSION
      || labels["problem-locator.e2e.hatchling"] !== RELEASE_HATCHLING_VERSION) {
      throw new Error("RELEASE_BASE_IMAGE_LABEL_MISMATCH");
    }
    const legacyValid = seal.schema_version === 1
      && seal.kind === "macos-linux-release-cache"
      && seal.image === paths.baseImage
      && seal.image_id === metadata.Id;
    const currentValid = seal.schema_version === 3
      && seal.kind === "macos-dual-linux-release-cache"
      && seal.server_image === paths.baseImage
      && seal.server_image_id === metadata.Id
      && seal.client_image === paths.clientImage
      && seal.platform === "linux/amd64";
    const commonValid = seal.platform === "linux/amd64"
      && seal.claude_tarball_sha256 === RELEASE_CLAUDE_TARBALL_SHA256
      && seal.claude_cli_sha256 === RELEASE_CLAUDE_CLI_SHA256
      && seal.uv_archive_sha256 === RELEASE_UV_ARCHIVE_SHA256
      && seal.uv_sha256 === RELEASE_UV_SHA256
      && seal.uvx_sha256 === RELEASE_UVX_SHA256
      && seal.hatchling_version === RELEASE_HATCHLING_VERSION;
    if (!(legacyValid || currentValid) || !commonValid) throw new Error("RELEASE_CACHE_SEAL_INVALID");

    let client = null;
    let browser = null;
    if (requireClientImage) {
      if (!currentValid) throw new Error("RELEASE_DUAL_IMAGE_SEAL_REQUIRED");
      const chromeCache = validateChromeHeadlessShellCache(paths);
      if (chromeCache.status !== "PRESENT") throw new Error(chromeCache.code);
      const clientMetadata = inspectReleaseImage(dockerIdentity, paths.clientImage);
      const clientLabels = clientMetadata.Config?.Labels ?? {};
      if (clientMetadata.Id !== seal.client_image_id
        || clientLabels["problem-locator.e2e.role"] !== "linux-client"
        || clientLabels["problem-locator.e2e.chrome-headless-shell-version"] !== RELEASE_CHROME_HEADLESS_SHELL_VERSION
        || clientLabels["problem-locator.e2e.chrome-headless-shell-product"] !== RELEASE_CHROME_HEADLESS_SHELL_PRODUCT
        || clientLabels["problem-locator.e2e.chrome-headless-shell-sha256"] !== RELEASE_CHROME_HEADLESS_SHELL_EXECUTABLE_SHA256
        || clientLabels["problem-locator.e2e.chrome-headless-shell-archive-sha256"] !== RELEASE_CHROME_HEADLESS_SHELL_ARCHIVE_SHA256
        || seal.chrome_headless_shell_product !== RELEASE_CHROME_HEADLESS_SHELL_PRODUCT
        || seal.chrome_headless_shell_version !== RELEASE_CHROME_HEADLESS_SHELL_VERSION
        || seal.chrome_headless_shell_archive_sha256 !== RELEASE_CHROME_HEADLESS_SHELL_ARCHIVE_SHA256
        || seal.chrome_headless_shell_executable_sha256 !== RELEASE_CHROME_HEADLESS_SHELL_EXECUTABLE_SHA256) {
        throw new Error("RELEASE_CLIENT_IMAGE_IDENTITY_MISMATCH");
      }
      const capability = probeReleaseClientHeadlessShell({
        dockerIdentity,
        clientImageId: clientMetadata.Id,
      });
      client = { image: paths.clientImage, image_id: clientMetadata.Id, platform: "linux/amd64" };
      browser = {
        status: "PRESENT",
        product: RELEASE_CHROME_HEADLESS_SHELL_PRODUCT,
        version: RELEASE_CHROME_HEADLESS_SHELL_VERSION_OUTPUT,
        executable_sha256: RELEASE_CHROME_HEADLESS_SHELL_EXECUTABLE_SHA256,
        archive_sha256: RELEASE_CHROME_HEADLESS_SHELL_ARCHIVE_SHA256,
        capability,
        code: null,
      };
    }
    return {
      status: "PRESENT",
      image: paths.baseImage,
      image_id: metadata.Id,
      server: { image: paths.baseImage, image_id: metadata.Id, platform: "linux/amd64" },
      client,
      browser,
      platform: "linux/amd64",
      seal_sha256: sha256File(paths.releaseSeal),
      validation_level: requireClientImage
        ? "sealed-cache-plus-plan-smoke-plus-capability-runtime-reprobe"
        : "sealed-cache-plus-capability-runtime-reprobe",
    };
  } catch (error) {
    return {
      status: "INVALID",
      image: paths.baseImage,
      image_id: null,
      server: null,
      client: null,
      browser: null,
      platform: null,
      seal_sha256: null,
      validation_level: null,
      code: String(error?.message ?? error),
    };
  }
}
