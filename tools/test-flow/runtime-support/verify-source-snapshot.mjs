#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

function canonicalize(value) {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonicalize(value[key])]));
  }
  return value;
}

function canonicalJson(value) {
  return `${JSON.stringify(canonicalize(value))}\n`;
}

function digest(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function fileDigest(filePath) {
  const hash = crypto.createHash("sha256");
  const descriptor = fs.openSync(filePath, "r");
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  try {
    for (;;) {
      const count = fs.readSync(descriptor, buffer, 0, buffer.length, null);
      if (count === 0) break;
      hash.update(buffer.subarray(0, count));
    }
  } finally {
    fs.closeSync(descriptor);
  }
  return hash.digest("hex");
}

function comparePaths(left, right) {
  const leftPath = typeof left === "string" ? left : left.path;
  const rightPath = typeof right === "string" ? right : right.path;
  return leftPath < rightPath ? -1 : leftPath > rightPath ? 1 : 0;
}

function argument(name) {
  const index = process.argv.indexOf(name);
  if (index === -1 || index + 1 >= process.argv.length) throw new Error(`SOURCE_SNAPSHOT_ARGUMENT_MISSING:${name}`);
  return process.argv[index + 1];
}

function leafPaths(root) {
  const values = [];
  const visit = (directory, prefix = "") => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
      const absolute = path.join(directory, entry.name);
      if (entry.isDirectory()) visit(absolute, relative);
      else if (entry.isFile() || entry.isSymbolicLink()) values.push(relative);
      else throw new Error(`SOURCE_SNAPSHOT_ENTRY_UNSUPPORTED:${relative}`);
    }
  };
  visit(root);
  return values.sort(comparePaths);
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
  if (typeof target !== "string" || target.length === 0 || path.isAbsolute(target)) throw new Error(`SOURCE_SNAPSHOT_SYMLINK_TARGET_INVALID:${absolute}`);
  if (!pathInside(root, path.resolve(path.dirname(absolute), target))) throw new Error(`SOURCE_SNAPSHOT_SYMLINK_OUTSIDE_ROOT:${absolute}`);
  let resolvedTarget;
  try { resolvedTarget = fs.realpathSync(absolute); } catch { throw new Error(`SOURCE_SNAPSHOT_SYMLINK_UNRESOLVED:${absolute}`); }
  if (!resolvedPathInside(root, resolvedTarget)) throw new Error(`SOURCE_SNAPSHOT_SYMLINK_OUTSIDE_ROOT:${absolute}`);
}

function record(root, expected) {
  const absolute = path.resolve(root, ...expected.path.split("/"));
  if (!absolute.startsWith(`${root}${path.sep}`)) throw new Error(`SOURCE_SNAPSHOT_PATH_INVALID:${expected.path}`);
  const metadata = fs.lstatSync(absolute);
  if (expected.kind === "symlink") {
    const target = fs.readlinkSync(absolute);
    validateSymlink(root, absolute, target);
    return { path: expected.path, kind: "symlink", mode: "120000", target };
  }
  if (!metadata.isFile()) throw new Error(`SOURCE_SNAPSHOT_ENTRY_INVALID:${expected.path}`);
  return {
    path: expected.path,
    kind: "file",
    mode: metadata.mode & 0o111 ? "100755" : "100644",
    size: metadata.size,
    sha256: fileDigest(absolute),
  };
}

const root = path.resolve(argument("--root"));
const manifestPath = path.resolve(argument("--manifest"));
const expectedDigest = argument("--expected-digest");
const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
if (manifest?.schema_version !== 1 || manifest.algorithm !== "git-visible-worktree-v1" || !Array.isArray(manifest.records)) {
  throw new Error("SOURCE_SNAPSHOT_MANIFEST_INVALID");
}
const records = [...manifest.records].sort(comparePaths);
const manifestDigest = digest(canonicalJson(records));
if (manifest.digest !== expectedDigest || manifestDigest !== expectedDigest || manifest.file_count !== records.length) {
  throw new Error("SOURCE_SNAPSHOT_MANIFEST_DIGEST_MISMATCH");
}
if (canonicalJson(leafPaths(root)) !== canonicalJson(records.map((item) => item.path))) {
  throw new Error("SOURCE_SNAPSHOT_PATH_SET_DRIFT");
}
const observed = records.map((item) => record(root, item));
if (digest(canonicalJson(observed)) !== expectedDigest) throw new Error("SOURCE_SNAPSHOT_CONTENT_DRIFT");
process.stdout.write(`${JSON.stringify({ schema_version: 1, status: "PASS", digest: expectedDigest, file_count: records.length })}\n`);
