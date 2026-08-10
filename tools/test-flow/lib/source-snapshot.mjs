import fs from "node:fs";
import path from "node:path";

import {
  assertFlow,
  canonicalJson,
  ensureDirectory,
  runSync,
  sha256Bytes,
  sha256File,
} from "./util.mjs";

const ALGORITHM = "git-visible-worktree-v1";

function comparePaths(left, right) {
  const leftPath = typeof left === "string" ? left : left.path;
  const rightPath = typeof right === "string" ? right : right.path;
  return leftPath < rightPath ? -1 : leftPath > rightPath ? 1 : 0;
}

function normalizeCandidatePath(repoRoot, candidate) {
  const relative = candidate.split(path.sep).join("/");
  const absolute = path.resolve(repoRoot, relative);
  const resolved = path.relative(repoRoot, absolute).split(path.sep).join("/");
  assertFlow(relative !== "" && resolved === relative && relative !== ".." && !relative.startsWith("../"), "SOURCE_SNAPSHOT_PATH_INVALID", `Invalid source path ${candidate}`);
  return { relative, absolute };
}

function pathInside(root, candidate) {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function resolvedPathInside(root, candidate) {
  const relative = path.relative(fs.realpathSync(root), candidate);
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function validateSymlink(root, absolute, target) {
  assertFlow(typeof target === "string" && target.length > 0 && !path.isAbsolute(target), "SOURCE_SNAPSHOT_SYMLINK_TARGET_INVALID", `Source snapshot symlink must use a non-empty relative target: ${absolute}`);
  const lexicalTarget = path.resolve(path.dirname(absolute), target);
  assertFlow(pathInside(root, lexicalTarget), "SOURCE_SNAPSHOT_SYMLINK_OUTSIDE_ROOT", `Source snapshot symlink escapes its root: ${absolute}`);
  let resolvedTarget;
  try {
    resolvedTarget = fs.realpathSync(absolute);
  } catch (error) {
    assertFlow(false, "SOURCE_SNAPSHOT_SYMLINK_UNRESOLVED", `Source snapshot symlink cannot be resolved: ${absolute}`, { cause: error?.code ?? "UNKNOWN" });
  }
  assertFlow(resolvedPathInside(root, resolvedTarget), "SOURCE_SNAPSHOT_SYMLINK_OUTSIDE_ROOT", `Source snapshot symlink resolves outside its root: ${absolute}`);
}

function gitVisiblePaths(repoRoot) {
  const listed = runSync("git", ["-C", repoRoot, "ls-files", "--cached", "--others", "--exclude-standard", "-z"]);
  assertFlow(listed.status === 0, "SOURCE_SNAPSHOT_GIT_REQUIRED", "Git could not enumerate the release source snapshot");
  return [...new Set(listed.stdout.split("\0").filter(Boolean))].sort(comparePaths);
}

function sourceRecord(repoRoot, candidate) {
  const { relative, absolute } = normalizeCandidatePath(repoRoot, candidate);
  let metadata;
  try {
    metadata = fs.lstatSync(absolute);
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
  if (metadata.isSymbolicLink()) {
    const target = fs.readlinkSync(absolute);
    validateSymlink(repoRoot, absolute, target);
    return { path: relative, kind: "symlink", mode: "120000", target };
  }
  if (metadata.isFile()) {
    return {
      path: relative,
      kind: "file",
      mode: metadata.mode & 0o111 ? "100755" : "100644",
      size: metadata.size,
      sha256: sha256File(absolute),
    };
  }
  assertFlow(false, "SOURCE_SNAPSHOT_ENTRY_UNSUPPORTED", `Source snapshot entry must be a file or symlink: ${relative}`);
}

function materializedPaths(root) {
  const records = [];
  const visit = (directory, prefix = "") => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
      const absolute = path.join(directory, entry.name);
      if (entry.isSymbolicLink() || entry.isFile()) records.push(relative);
      else if (entry.isDirectory()) visit(absolute, relative);
      else assertFlow(false, "SOURCE_SNAPSHOT_ENTRY_UNSUPPORTED", `Materialized snapshot entry must be a file, symlink, or directory: ${relative}`);
    }
  };
  visit(root);
  return records.sort(comparePaths);
}

function manifest(records) {
  const normalized = [...records].sort(comparePaths);
  return {
    schema_version: 1,
    algorithm: ALGORITHM,
    digest: sha256Bytes(canonicalJson(normalized)),
    file_count: normalized.length,
    records: normalized,
  };
}

export function captureSourceSnapshot(repoRoot) {
  const records = gitVisiblePaths(repoRoot).map((candidate) => sourceRecord(repoRoot, candidate)).filter(Boolean);
  return manifest(records);
}

export function verifySourceSnapshot(repoRoot, expected) {
  try {
    const observed = captureSourceSnapshot(repoRoot);
    return {
      schema_version: 1,
      status: observed.digest === expected.digest ? "PASS" : "FAIL",
      expected_digest: expected.digest,
      observed_digest: observed.digest,
      expected_file_count: expected.file_count,
      observed_file_count: observed.file_count,
    };
  } catch (error) {
    return {
      schema_version: 1,
      status: "ERROR",
      expected_digest: expected.digest,
      observed_digest: null,
      code: error?.code ?? "SOURCE_SNAPSHOT_VERIFY_ERROR",
    };
  }
}

export function materializeSourceSnapshot(repoRoot, targetRoot, expected) {
  assertFlow(!fs.existsSync(targetRoot), "SOURCE_SNAPSHOT_TARGET_EXISTS", "Source snapshot target must be new");
  ensureDirectory(targetRoot);
  for (const record of expected.records) {
    const source = normalizeCandidatePath(repoRoot, record.path).absolute;
    const observed = sourceRecord(repoRoot, record.path);
    assertFlow(observed && canonicalJson(observed) === canonicalJson(record), "SOURCE_SNAPSHOT_DRIFT", `Source entry changed before materialization: ${record.path}`);
    const target = path.join(targetRoot, ...record.path.split("/"));
    ensureDirectory(path.dirname(target));
    if (record.kind === "symlink") {
      fs.symlinkSync(record.target, target);
      continue;
    }
    fs.copyFileSync(source, target, fs.constants.COPYFILE_EXCL);
    fs.chmodSync(target, record.mode === "100755" ? 0o755 : 0o644);
  }
  const verification = verifyMaterializedSourceSnapshot(targetRoot, expected);
  assertFlow(verification.status === "PASS", "SOURCE_SNAPSHOT_MATERIALIZATION_INVALID", "Materialized source snapshot differs from its manifest", verification);
  return verification;
}

export function verifyMaterializedSourceSnapshot(snapshotRoot, expected) {
  try {
    assertFlow(expected?.schema_version === 1 && expected.algorithm === ALGORITHM && Array.isArray(expected.records), "SOURCE_SNAPSHOT_MANIFEST_INVALID", "Source snapshot manifest is invalid");
    const expectedPaths = expected.records.map((record) => record.path).sort(comparePaths);
    const observedPaths = materializedPaths(snapshotRoot);
    assertFlow(canonicalJson(observedPaths) === canonicalJson(expectedPaths), "SOURCE_SNAPSHOT_PATH_SET_DRIFT", "Materialized source snapshot path set differs from its manifest");
    const records = expected.records.map((record) => {
      const absolute = normalizeCandidatePath(snapshotRoot, record.path).absolute;
      const metadata = fs.lstatSync(absolute);
      if (record.kind === "symlink") {
        const target = fs.readlinkSync(absolute);
        validateSymlink(snapshotRoot, absolute, target);
        return { path: record.path, kind: "symlink", mode: "120000", target };
      }
      assertFlow(metadata.isFile(), "SOURCE_SNAPSHOT_ENTRY_INVALID", `Materialized snapshot entry is not a file: ${record.path}`);
      return {
        path: record.path,
        kind: "file",
        mode: metadata.mode & 0o111 ? "100755" : "100644",
        size: metadata.size,
        sha256: sha256File(absolute),
      };
    });
    const observed = manifest(records);
    return {
      schema_version: 1,
      status: observed.digest === expected.digest ? "PASS" : "FAIL",
      expected_digest: expected.digest,
      observed_digest: observed.digest,
      expected_file_count: expected.file_count,
      observed_file_count: observed.file_count,
    };
  } catch (error) {
    return { schema_version: 1, status: "ERROR", expected_digest: expected.digest, observed_digest: null, code: error?.code ?? "SOURCE_SNAPSHOT_VERIFY_ERROR" };
  }
}

export function publicSourceSnapshot(manifestValue) {
  return {
    schema_version: manifestValue.schema_version,
    algorithm: manifestValue.algorithm,
    status: manifestValue.digest ? "PRESENT" : "MISSING",
    digest: manifestValue.digest,
    file_count: manifestValue.file_count,
  };
}
