#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  RELEASE_BASE_IMAGE,
  RELEASE_BASE_IMAGE_SOURCE,
  RELEASE_CLAUDE_CLI_SHA256,
  RELEASE_CLAUDE_TARBALL_SHA256,
  RELEASE_CLAUDE_VERSION,
  RELEASE_DOCKER_ARCH,
  RELEASE_DOCKER_OS,
  RELEASE_HATCHLING_VERSION,
  RELEASE_PYTHON_VERSION,
  RELEASE_UV_ARCHIVE_SHA256,
  RELEASE_UV_SHA256,
  RELEASE_UV_VERSION,
  RELEASE_UVX_SHA256,
  dockerCommandArguments,
  dockerServerIdentity,
  packageTreeIdentity,
  releaseArtifactDownloadInvocation,
  releaseClientForHost,
  releaseCachePaths,
  releaseDockerContextForClient,
  validateClaudeDistribution,
  validateUvCache,
} from "./lib/release-inputs.mjs";
import { canonicalJson, ensureDirectory, runSync, sha256Bytes, sha256File } from "./lib/util.mjs";

const TOOL_ROOT = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_REPO_ROOT = path.resolve(TOOL_ROOT, "..", "..");
const CLAUDE_URL = `https://registry.npmjs.org/@anthropic-ai/claude-code/-/claude-code-${RELEASE_CLAUDE_VERSION}.tgz`;
const UV_URL = `https://github.com/astral-sh/uv/releases/download/${RELEASE_UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz`;

function argument(name) {
  const index = process.argv.indexOf(name);
  return index === -1 ? null : process.argv[index + 1];
}

function has(name) {
  return process.argv.includes(name);
}

function execute(command, args, { cwd = undefined, maxBuffer = 128 * 1024 * 1024 } = {}) {
  const result = runSync(command, args, { cwd, maxBuffer });
  if (result.status !== 0) {
    const tail = `${result.stderr}\n${result.stdout}`.slice(-8000);
    throw new Error(`CACHE_COMMAND_FAILED:${command}:${result.status}:${tail}`);
  }
  return result;
}

function download(url, destination) {
  const invocation = releaseArtifactDownloadInvocation(
    process.platform,
    url,
    destination,
  );
  execute(invocation.command, invocation.args);
}

function publishDirectory(staging, destination) {
  ensureDirectory(path.dirname(destination));
  if (fs.existsSync(destination)) {
    const backup = `${destination}.previous-${new Date().toISOString().replace(/[-:.]/g, "")}-${crypto.randomBytes(3).toString("hex")}`;
    fs.renameSync(destination, backup);
    process.stdout.write(`preserved_previous_cache=${backup}\n`);
  }
  fs.renameSync(staging, destination);
}

function writeExclusive(filePath, value) {
  const descriptor = fs.openSync(filePath, "wx", 0o600);
  try {
    fs.writeFileSync(descriptor, canonicalJson(value), "utf8");
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

function prepareClaude(paths, stagingRoot) {
  const staging = path.join(stagingRoot, "claude");
  ensureDirectory(staging);
  const archive = path.join(staging, path.basename(paths.claudeTarball));
  download(CLAUDE_URL, archive);
  if (sha256File(archive) !== RELEASE_CLAUDE_TARBALL_SHA256) throw new Error("CLAUDE_TARBALL_HASH_MISMATCH");
  execute("tar", ["-xzf", archive, "-C", staging]);
  const entry = path.join(staging, "package", "cli.js");
  const manifestPath = path.join(staging, "package", "package.json");
  if (sha256File(entry) !== RELEASE_CLAUDE_CLI_SHA256) throw new Error("CLAUDE_CLI_HASH_MISMATCH");
  const manifestBytes = fs.readFileSync(manifestPath);
  const manifest = JSON.parse(manifestBytes.toString("utf8"));
  if (manifest.name !== "@anthropic-ai/claude-code" || manifest.version !== RELEASE_CLAUDE_VERSION) throw new Error("CLAUDE_PACKAGE_IDENTITY_MISMATCH");
  const tree = packageTreeIdentity(path.join(staging, "package"));
  if (tree.status !== "PRESENT") throw new Error("CLAUDE_PACKAGE_TREE_INVALID");
  writeExclusive(path.join(staging, "cache-seal.json"), {
    schema_version: 1,
    kind: "official-claude-npm-cache",
    source_url: CLAUDE_URL,
    package_name: manifest.name,
    package_version: manifest.version,
    tarball_sha256: RELEASE_CLAUDE_TARBALL_SHA256,
    cli_sha256: RELEASE_CLAUDE_CLI_SHA256,
    package_manifest_sha256: sha256Bytes(manifestBytes),
    package_tree_digest: tree.digest,
  });
  publishDirectory(staging, paths.claudeRoot);
  const validated = validateClaudeDistribution(paths.claudeEntry);
  if (validated.status !== "PRESENT") throw new Error(`CLAUDE_CACHE_VALIDATION_FAILED:${validated.code}`);
  process.stdout.write(`claude_cache=PASS version=${validated.version} cli_sha256=${validated.cli_sha256}\n`);
}

function prepareUv(paths, stagingRoot) {
  const staging = path.join(stagingRoot, "uv");
  const extracted = path.join(staging, "extracted");
  ensureDirectory(extracted);
  const archive = path.join(staging, path.basename(paths.uvArchive));
  download(UV_URL, archive);
  if (sha256File(archive) !== RELEASE_UV_ARCHIVE_SHA256) throw new Error("UV_ARCHIVE_HASH_MISMATCH");
  execute("tar", ["-xzf", archive, "-C", extracted]);
  const distribution = path.join(extracted, "uv-x86_64-unknown-linux-gnu");
  fs.copyFileSync(path.join(distribution, "uv"), path.join(staging, "uv"));
  fs.copyFileSync(path.join(distribution, "uvx"), path.join(staging, "uvx"));
  fs.chmodSync(path.join(staging, "uv"), 0o755);
  fs.chmodSync(path.join(staging, "uvx"), 0o755);
  fs.rmSync(extracted, { recursive: true, force: true });
  if (sha256File(path.join(staging, "uv")) !== RELEASE_UV_SHA256 || sha256File(path.join(staging, "uvx")) !== RELEASE_UVX_SHA256) {
    throw new Error("UV_BINARY_HASH_MISMATCH");
  }
  writeExclusive(path.join(staging, "cache-seal.json"), {
    schema_version: 1,
    kind: "official-uv-release-cache",
    source_url: UV_URL,
    version: RELEASE_UV_VERSION,
    platform: "x86_64-unknown-linux-gnu",
    archive_sha256: RELEASE_UV_ARCHIVE_SHA256,
    uv_sha256: RELEASE_UV_SHA256,
    uvx_sha256: RELEASE_UVX_SHA256,
  });
  publishDirectory(staging, paths.uvRoot);
  const validated = validateUvCache(paths);
  if (validated.status !== "PRESENT") throw new Error(`UV_CACHE_VALIDATION_FAILED:${validated.code}`);
  process.stdout.write(`uv_cache=PASS version=${RELEASE_UV_VERSION} archive_sha256=${validated.archive_sha256}\n`);
}

function prepareImage(repoRoot, paths, dockerContext, client) {
  const dockerIdentity = dockerServerIdentity(dockerContext, client);
  if (dockerIdentity.status !== "PRESENT") throw new Error(`DOCKER_IDENTITY_INVALID:${dockerIdentity.code}`);
  const logPath = path.join(paths.cacheRoot, "base-image-build.log");
  const build = runSync("docker", dockerCommandArguments(dockerContext, [
    "buildx", "build",
    "--platform", "linux/amd64",
    "--provenance=false",
    "--pull",
    "--load",
    "--progress", "plain",
    "--build-context", `uvcache=${paths.uvRoot}`,
    "--build-context", `claudecache=${paths.claudeRoot}`,
    "--build-arg", `BASE_IMAGE=${RELEASE_BASE_IMAGE_SOURCE}`,
    "--build-arg", `UV_SHA256=${RELEASE_UV_SHA256}`,
    "--build-arg", `UVX_SHA256=${RELEASE_UVX_SHA256}`,
    "--build-arg", `UV_VERSION=${RELEASE_UV_VERSION}`,
    "--build-arg", `CLAUDE_CLI_SHA256=${RELEASE_CLAUDE_CLI_SHA256}`,
    "--build-arg", `CLAUDE_VERSION=${RELEASE_CLAUDE_VERSION}`,
    "--build-arg", `PYTHON_VERSION=${RELEASE_PYTHON_VERSION}`,
    "--build-arg", `HATCHLING_VERSION=${RELEASE_HATCHLING_VERSION}`,
    "--tag", RELEASE_BASE_IMAGE,
    "--file", path.join(repoRoot, "tools", "test-flow", "Dockerfile"),
    repoRoot,
  ]), { cwd: repoRoot, maxBuffer: 256 * 1024 * 1024 });
  fs.writeFileSync(logPath, `${build.stdout}${build.stderr}`, { encoding: "utf8", mode: 0o600 });
  if (build.status !== 0) throw new Error(`BASE_IMAGE_BUILD_FAILED:${build.status}:${build.stderr.slice(-4000)}`);
  const inspect = execute("docker", dockerCommandArguments(dockerContext, ["image", "inspect", RELEASE_BASE_IMAGE]));
  const metadata = JSON.parse(inspect.stdout)[0];
  if (metadata.Os !== RELEASE_DOCKER_OS || metadata.Architecture !== RELEASE_DOCKER_ARCH) throw new Error("BASE_IMAGE_PLATFORM_MISMATCH");
  const sealPath = paths.releaseSeal;
  if (fs.existsSync(sealPath)) {
    const backup = `${sealPath}.previous-${crypto.randomBytes(4).toString("hex")}`;
    fs.renameSync(sealPath, backup);
    process.stdout.write(`preserved_previous_release_seal=${backup}\n`);
  }
  writeExclusive(sealPath, {
    schema_version: 1,
    kind: "linux-server-release-cache",
    image: RELEASE_BASE_IMAGE,
    image_id: metadata.Id,
    platform: "linux/amd64",
    base_image_source: RELEASE_BASE_IMAGE_SOURCE,
    python_version: RELEASE_PYTHON_VERSION,
    docker_context: dockerContext,
    docker_effective_context: dockerIdentity.effective_context,
    docker_context_fingerprint: dockerIdentity.context_fingerprint,
    claude_tarball_sha256: RELEASE_CLAUDE_TARBALL_SHA256,
    claude_cli_sha256: RELEASE_CLAUDE_CLI_SHA256,
    uv_archive_sha256: RELEASE_UV_ARCHIVE_SHA256,
    uv_sha256: RELEASE_UV_SHA256,
    uvx_sha256: RELEASE_UVX_SHA256,
    hatchling_version: RELEASE_HATCHLING_VERSION,
  });
  process.stdout.write(`base_image=PASS image=${RELEASE_BASE_IMAGE} image_id=${metadata.Id}\n`);
  process.stdout.write(`build_log=${logPath}\n`);
}

function main() {
  const repoRoot = path.resolve(argument("--repo-root") ?? DEFAULT_REPO_ROOT);
  const configuredCache = argument("--cache-root");
  if (configuredCache && !path.isAbsolute(configuredCache)) throw new Error("CACHE_ROOT_MUST_BE_ABSOLUTE");
  const hostClient = releaseClientForHost();
  const client = argument("--client") ?? hostClient;
  if (client !== hostClient) throw new Error(`CACHE_CLIENT_HOST_MISMATCH:${client}:${hostClient}`);
  const expectedDockerContext = releaseDockerContextForClient(client);
  const dockerContext = argument("--docker-context") ?? expectedDockerContext;
  if (dockerContext !== expectedDockerContext) throw new Error(`DOCKER_CONTEXT_MISMATCH:${client}:${expectedDockerContext}`);
  const paths = releaseCachePaths(repoRoot, configuredCache);
  ensureDirectory(paths.cacheRoot);
  const stagingRoot = path.join(paths.cacheRoot, `.prepare-${process.pid}-${crypto.randomUUID()}`);
  ensureDirectory(stagingRoot);
  try {
    prepareClaude(paths, stagingRoot);
    prepareUv(paths, stagingRoot);
    if (!has("--skip-image")) prepareImage(repoRoot, paths, dockerContext, client);
  } finally {
    fs.rmSync(stagingRoot, { recursive: true, force: true });
  }
}

try {
  main();
} catch (error) {
  process.stderr.write(`${String(error?.message ?? error)}\n`);
  process.exitCode = 1;
}
