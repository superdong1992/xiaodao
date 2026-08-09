import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { atomicCreateJson, canonicalJson, ensureDirectory, readJson, sha256Bytes, sha256File, writeJsonSync } from "./util.mjs";
import { scanPayload } from "./evidence.mjs";

const BLOCK = 512;

function safeRelative(relative) {
  const normalized = relative.split(path.sep).join("/");
  if (!normalized || normalized === "." || normalized.startsWith("/") || normalized.includes("\0")) return false;
  const parts = normalized.split("/");
  return !parts.some((part) => part === "" || part === "." || part === "..");
}

function collectStateEntries(root) {
  const records = [];
  function visit(current) {
    for (const entry of fs.readdirSync(current, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const absolute = path.join(current, entry.name);
      const relative = path.relative(root, absolute).split(path.sep).join("/");
      if (!safeRelative(relative)) throw new Error(`CHECKPOINT_UNSAFE_PATH:${relative}`);
      const stat = fs.lstatSync(absolute);
      if (stat.isSymbolicLink()) throw new Error(`CHECKPOINT_SYMLINK_FORBIDDEN:${relative}`);
      if (stat.isDirectory()) {
        records.push({ kind: "directory", path: relative, mode: stat.mode & 0o777, uid: stat.uid, gid: stat.gid, size: 0, content_sha256: null });
        visit(absolute);
      } else if (stat.isFile()) {
        if (stat.nlink !== 1) throw new Error(`CHECKPOINT_HARDLINK_FORBIDDEN:${relative}`);
        records.push({
          kind: "file",
          path: relative,
          mode: stat.mode & 0o777,
          uid: stat.uid,
          gid: stat.gid,
          size: stat.size,
          content_sha256: sha256File(absolute),
        });
      } else {
        throw new Error(`CHECKPOINT_SPECIAL_FILE_FORBIDDEN:${relative}`);
      }
    }
  }
  visit(root);
  return records;
}

function writeString(buffer, offset, length, value) {
  const encoded = Buffer.from(value, "utf8");
  if (encoded.length > length) throw new Error(`CHECKPOINT_TAR_FIELD_TOO_LONG:${value}`);
  encoded.copy(buffer, offset);
}

function writeOctal(buffer, offset, length, value) {
  const encoded = Math.max(0, value).toString(8).padStart(length - 1, "0");
  if (encoded.length > length - 1) throw new Error(`CHECKPOINT_TAR_NUMBER_TOO_LARGE:${value}`);
  writeString(buffer, offset, length, `${encoded}\0`);
}

function splitTarPath(relative) {
  const encoded = Buffer.byteLength(relative);
  if (encoded <= 100) return { name: relative, prefix: "" };
  const parts = relative.split("/");
  for (let index = parts.length - 1; index > 0; index -= 1) {
    const prefix = parts.slice(0, index).join("/");
    const name = parts.slice(index).join("/");
    if (Buffer.byteLength(prefix) <= 155 && Buffer.byteLength(name) <= 100) return { name, prefix };
  }
  throw new Error(`CHECKPOINT_TAR_PATH_TOO_LONG:${relative}`);
}

function tarHeader(record) {
  const buffer = Buffer.alloc(BLOCK);
  const { name, prefix } = splitTarPath(record.path);
  writeString(buffer, 0, 100, name);
  writeOctal(buffer, 100, 8, record.mode);
  writeOctal(buffer, 108, 8, record.uid);
  writeOctal(buffer, 116, 8, record.gid);
  writeOctal(buffer, 124, 12, record.kind === "file" ? record.size : 0);
  writeOctal(buffer, 136, 12, 0);
  buffer.fill(0x20, 148, 156);
  writeString(buffer, 156, 1, record.kind === "directory" ? "5" : "0");
  writeString(buffer, 257, 6, "ustar\0");
  writeString(buffer, 263, 2, "00");
  writeString(buffer, 345, 155, prefix);
  const checksum = buffer.reduce((sum, byte) => sum + byte, 0);
  const checksumText = checksum.toString(8).padStart(6, "0");
  writeString(buffer, 148, 8, `${checksumText}\0 `);
  return buffer;
}

function createTar(stateRoot, records, archivePath) {
  const descriptor = fs.openSync(archivePath, "wx", 0o600);
  try {
    for (const record of records) {
      fs.writeSync(descriptor, tarHeader(record));
      if (record.kind !== "file") continue;
      const source = fs.openSync(path.join(stateRoot, ...record.path.split("/")), "r");
      try {
        const buffer = Buffer.allocUnsafe(1024 * 1024);
        let written = 0;
        for (;;) {
          const count = fs.readSync(source, buffer, 0, buffer.length, null);
          if (count === 0) break;
          fs.writeSync(descriptor, buffer.subarray(0, count));
          written += count;
        }
        if (written !== record.size) throw new Error(`CHECKPOINT_SOURCE_CHANGED:${record.path}`);
        const padding = (BLOCK - (written % BLOCK)) % BLOCK;
        if (padding) fs.writeSync(descriptor, Buffer.alloc(padding));
      } finally {
        fs.closeSync(source);
      }
    }
    fs.writeSync(descriptor, Buffer.alloc(BLOCK * 2));
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

function assertQuiescent(receipt) {
  const okay = receipt?.status === "PASS"
    && receipt.service_stopped === true
    && receipt.running_jobs === 0
    && receipt.queued_jobs === 0
    && receipt.active_workers === 0
    && receipt.temporary_workspaces === 0
    && receipt.state_validation === "PASS";
  if (!okay) throw new Error("CHECKPOINT_NOT_QUIESCENT");
}

function checkpointFiles(root) {
  return fs.readdirSync(root, { withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name !== "checkpoint-seal.json")
    .map((entry) => ({ name: entry.name, size: fs.statSync(path.join(root, entry.name)).size, sha256: sha256File(path.join(root, entry.name)) }))
    .sort((left, right) => left.name.localeCompare(right.name));
}

function makeReadOnly(root) {
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const absolute = path.join(root, entry.name);
    if (entry.isDirectory()) makeReadOnly(absolute);
    else fs.chmodSync(absolute, 0o400);
  }
  fs.chmodSync(root, 0o500);
}

export function createCheckpoint({
  stateRoot,
  checkpointsRoot,
  stageId,
  continuation,
  identity,
  parentCheckpointId = "GENESIS",
  quiescenceReceipt,
  knownSecrets = [],
}) {
  assertQuiescent(quiescenceReceipt);
  if (!/^[a-z0-9.-]+$/.test(stageId)) throw new Error(`CHECKPOINT_STAGE_ID:${stageId}`);
  const stageRoot = path.join(checkpointsRoot, stageId);
  ensureDirectory(stageRoot);
  const staging = path.join(stageRoot, `.pending-${process.pid}-${crypto.randomUUID()}`);
  fs.mkdirSync(staging, { recursive: false, mode: 0o700 });
  try {
    const records = collectStateEntries(stateRoot);
    const portableDigest = sha256Bytes(canonicalJson(records));
    createTar(stateRoot, records, path.join(staging, "data-root.tar"));
    fs.writeFileSync(
      path.join(staging, "portable-state-manifest.ndjson"),
      records.map((record) => JSON.stringify(record)).join("\n") + (records.length ? "\n" : ""),
      { encoding: "utf8", flag: "wx", mode: 0o600 },
    );
    writeJsonSync(path.join(staging, "continuation.json"), continuation);
    writeJsonSync(path.join(staging, "quiescence-receipt.json"), quiescenceReceipt);
    writeJsonSync(path.join(staging, "execution-identity.json"), identity);
    const scan = scanPayload(staging, { knownSecrets });
    writeJsonSync(path.join(staging, "checkpoint-secret-scan.json"), scan);
    if (scan.status !== "PASS") throw new Error("CHECKPOINT_SECRET_SCAN_FAILED");
    const files = checkpointFiles(staging);
    const checkpointId = sha256Bytes(canonicalJson({
      schema_version: 1,
      stage_id: stageId,
      parent_checkpoint_id: parentCheckpointId,
      portable_digest: portableDigest,
      identity,
      files,
    }));
    const seal = {
      schema_version: 1,
      status: "PASS",
      checkpoint_id: checkpointId,
      stage_id: stageId,
      parent_checkpoint_id: parentCheckpointId,
      portable_digest: portableDigest,
      files,
    };
    atomicCreateJson(path.join(staging, "checkpoint-seal.json"), seal);
    const destination = path.join(stageRoot, checkpointId);
    if (fs.existsSync(destination)) throw new Error(`CHECKPOINT_ALREADY_EXISTS:${checkpointId}`);
    makeReadOnly(staging);
    fs.renameSync(staging, destination);
    return {
      checkpoint_id: checkpointId,
      parent_checkpoint_id: parentCheckpointId,
      stage_id: stageId,
      path: destination,
      portable_digest: portableDigest,
    };
  } catch (error) {
    if (fs.existsSync(staging)) {
      try { fs.chmodSync(staging, 0o700); } catch {}
      for (const entry of fs.readdirSync(staging, { withFileTypes: true })) {
        const absolute = path.join(staging, entry.name);
        try { fs.chmodSync(absolute, entry.isDirectory() ? 0o700 : 0o600); } catch {}
      }
      fs.rmSync(staging, { recursive: true, force: true });
    }
    throw error;
  }
}

export function verifyCheckpoint(checkpointRoot, { knownSecrets = [] } = {}) {
  const sealPath = path.join(checkpointRoot, "checkpoint-seal.json");
  if (!fs.existsSync(sealPath)) return { status: "FAIL", code: "CHECKPOINT_SEAL_MISSING" };
  const seal = readJson(sealPath);
  const files = checkpointFiles(checkpointRoot);
  if (sha256Bytes(canonicalJson(files)) !== sha256Bytes(canonicalJson(seal.files))) {
    return { status: "FAIL", code: "CHECKPOINT_FILE_HASH_MISMATCH" };
  }
  const identity = readJson(path.join(checkpointRoot, "execution-identity.json"));
  const expectedId = sha256Bytes(canonicalJson({
    schema_version: 1,
    stage_id: seal.stage_id,
    parent_checkpoint_id: seal.parent_checkpoint_id,
    portable_digest: seal.portable_digest,
    identity,
    files,
  }));
  if (expectedId !== seal.checkpoint_id || path.basename(checkpointRoot) !== seal.checkpoint_id) {
    return { status: "FAIL", code: "CHECKPOINT_ID_MISMATCH" };
  }
  const scan = scanPayload(checkpointRoot, { knownSecrets });
  if (scan.status !== "PASS") return { status: "FAIL", code: "CHECKPOINT_CURRENT_SCAN_FAILED", scan };
  return { status: "PASS", seal, scan };
}

function parseOctal(buffer, start, length) {
  const raw = buffer.subarray(start, start + length).toString("ascii").replace(/\0.*$/, "").trim();
  return raw ? Number.parseInt(raw, 8) : 0;
}

function parseTarHeader(header) {
  const allZero = header.every((byte) => byte === 0);
  if (allZero) return null;
  const storedChecksum = parseOctal(header, 148, 8);
  const copy = Buffer.from(header);
  copy.fill(0x20, 148, 156);
  const actualChecksum = copy.reduce((sum, byte) => sum + byte, 0);
  if (storedChecksum !== actualChecksum) throw new Error("CHECKPOINT_TAR_CHECKSUM");
  const name = header.subarray(0, 100).toString("utf8").replace(/\0.*$/, "");
  const prefix = header.subarray(345, 500).toString("utf8").replace(/\0.*$/, "");
  const type = header.subarray(156, 157).toString("ascii") || "0";
  if (!['0', '5', '\0'].includes(type)) throw new Error(`CHECKPOINT_TAR_TYPE:${type}`);
  const rawRelative = prefix ? `${prefix}/${name}` : name;
  const relative = type === "5" && rawRelative.endsWith("/") ? rawRelative.slice(0, -1) : rawRelative;
  if (!safeRelative(relative)) throw new Error(`CHECKPOINT_TAR_UNSAFE_PATH:${rawRelative}`);
  return {
    path: relative,
    kind: type === "5" ? "directory" : "file",
    mode: parseOctal(header, 100, 8),
    uid: parseOctal(header, 108, 8),
    gid: parseOctal(header, 116, 8),
    size: parseOctal(header, 124, 12),
  };
}

function extractTar(archivePath, targetRoot) {
  const archive = fs.openSync(archivePath, "r");
  let offset = 0;
  try {
    for (;;) {
      const header = Buffer.alloc(BLOCK);
      const count = fs.readSync(archive, header, 0, BLOCK, offset);
      if (count === 0) break;
      if (count !== BLOCK) throw new Error("CHECKPOINT_TAR_PARTIAL_HEADER");
      offset += BLOCK;
      const record = parseTarHeader(header);
      if (!record) break;
      const destination = path.join(targetRoot, ...record.path.split("/"));
      const resolved = path.resolve(destination);
      const prefix = `${path.resolve(targetRoot)}${path.sep}`;
      if (!resolved.startsWith(prefix)) throw new Error(`CHECKPOINT_TAR_ESCAPE:${record.path}`);
      if (record.kind === "directory") {
        fs.mkdirSync(destination, { recursive: false, mode: record.mode });
      } else {
        fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 });
        const output = fs.openSync(destination, "wx", record.mode);
        try {
          let remaining = record.size;
          const buffer = Buffer.allocUnsafe(Math.min(1024 * 1024, Math.max(1, remaining)));
          while (remaining > 0) {
            const wanted = Math.min(buffer.length, remaining);
            const read = fs.readSync(archive, buffer, 0, wanted, offset);
            if (read !== wanted) throw new Error(`CHECKPOINT_TAR_PARTIAL_FILE:${record.path}`);
            fs.writeSync(output, buffer.subarray(0, read));
            offset += read;
            remaining -= read;
          }
          fs.fsyncSync(output);
        } finally {
          fs.closeSync(output);
        }
        const padding = (BLOCK - (record.size % BLOCK)) % BLOCK;
        offset += padding;
      }
      try {
        fs.chownSync(destination, record.uid, record.gid);
      } catch (error) {
        if (typeof process.getuid === "function" && process.getuid() === 0) throw error;
      }
      fs.chmodSync(destination, record.mode);
    }
  } finally {
    fs.closeSync(archive);
  }
}

export function extractCheckpointSourceArchive({ archivePath, targetRoot }) {
  if (!path.isAbsolute(archivePath) || !path.isAbsolute(targetRoot)) {
    throw new Error("CHECKPOINT_SOURCE_PATH_NOT_ABSOLUTE");
  }
  if (fs.existsSync(targetRoot)) {
    if (!fs.statSync(targetRoot).isDirectory() || fs.readdirSync(targetRoot).length !== 0) {
      throw new Error("CHECKPOINT_SOURCE_TARGET_NOT_EMPTY");
    }
  } else {
    fs.mkdirSync(targetRoot, { recursive: false, mode: 0o700 });
  }
  try {
    extractTar(archivePath, targetRoot);
    const records = collectStateEntries(targetRoot);
    return {
      status: "PASS",
      entry_count: records.length,
      portable_digest: sha256Bytes(canonicalJson(records)),
    };
  } catch (error) {
    for (const entry of fs.readdirSync(targetRoot)) {
      fs.rmSync(path.join(targetRoot, entry), { recursive: true, force: true });
    }
    throw error;
  }
}

export function restoreCheckpoint({ checkpointRoot, targetRoot, currentIdentity, knownSecrets = [] }) {
  const verified = verifyCheckpoint(checkpointRoot, { knownSecrets });
  if (verified.status !== "PASS") throw new Error(verified.code);
  const expectedIdentity = readJson(path.join(checkpointRoot, "execution-identity.json"));
  if (sha256Bytes(canonicalJson(expectedIdentity)) !== sha256Bytes(canonicalJson(currentIdentity))) {
    throw new Error("CHECKPOINT_IDENTITY_MISMATCH");
  }
  if (fs.existsSync(targetRoot)) {
    if (!fs.statSync(targetRoot).isDirectory() || fs.readdirSync(targetRoot).length !== 0) throw new Error("CHECKPOINT_TARGET_NOT_EMPTY");
  } else {
    fs.mkdirSync(targetRoot, { recursive: false, mode: 0o700 });
  }
  try {
    extractTar(path.join(checkpointRoot, "data-root.tar"), targetRoot);
    const restored = collectStateEntries(targetRoot);
    const digest = sha256Bytes(canonicalJson(restored));
    if (digest !== verified.seal.portable_digest) throw new Error("CHECKPOINT_RESTORED_DIGEST_MISMATCH");
    return { status: "PASS", checkpoint_id: verified.seal.checkpoint_id, portable_digest: digest };
  } catch (error) {
    // Restore targets are unique and new; clearing a failed partial restore does
    // not touch prior evidence or the sealed checkpoint.
    for (const entry of fs.readdirSync(targetRoot)) fs.rmSync(path.join(targetRoot, entry), { recursive: true, force: true });
    throw error;
  }
}
