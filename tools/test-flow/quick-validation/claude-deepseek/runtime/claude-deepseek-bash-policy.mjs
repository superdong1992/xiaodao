#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const MODULE_PATH = fileURLToPath(import.meta.url);
const UPLOAD_STEPS = Object.freeze(["upload-openssl", "upload-stat", "upload-curl"]);
const DOWNLOAD_STEPS = Object.freeze(["download-curl", "download-stat", "download-openssl"]);

function denied(reason) { return { allowed: false, reason }; }

function shellWords(command) {
  if (typeof command !== "string" || command.length === 0 || /[\n\r\0;&|`]/u.test(command)) return null;
  const words = command.match(/(?:[^\s'"]+|'[^']*'|"[^"]*")+/gu) ?? [];
  return words.map((word) => ((word.startsWith("'") && word.endsWith("'")) || (word.startsWith('"') && word.endsWith('"'))) ? word.slice(1, -1) : word);
}

function uploadCurlContract(words, policy) {
  const headers = new Map();
  let method = null;
  let maxTime = null;
  let uploadFile = null;
  let target = null;
  for (let index = 1; index < words.length; index += 1) {
    const word = words[index];
    if (["--silent", "--show-error", "--fail-with-body"].includes(word)) continue;
    if (word === "--request") { method = words[index += 1] ?? null; continue; }
    if (word === "--max-time") {
      if (maxTime !== null) return denied("curl --max-time must appear exactly once");
      maxTime = words[index += 1] ?? null;
      continue;
    }
    if (word === "--upload-file") { uploadFile = words[index += 1] ?? null; continue; }
    if (word === "--header" || word === "-H") {
      const rendered = words[index += 1] ?? "";
      const split = rendered.indexOf(":");
      if (split <= 0) return denied("curl headers must use exact Name: value arguments");
      const name = rendered.slice(0, split);
      if (headers.has(name)) return denied("curl header names must be unique");
      headers.set(name, rendered.slice(split + 1).trim());
      continue;
    }
    if (/^https?:\/\//u.test(word) && target === null) { target = word; continue; }
    return denied("curl contains an unsupported option or argument");
  }
  if (method !== "PUT" || maxTime !== "60" || uploadFile !== policy.archive_path || target === null) return denied("curl must be one 60-second-bounded PUT of the frozen archive");
  let url;
  try { url = new URL(target); } catch { return denied("curl target is not a valid URL"); }
  const match = url.pathname.match(/^\/api\/v1\/attachments\/([A-Za-z0-9._-]+)\/content$/u);
  if (url.origin !== policy.upload_origin || match === null || [...url.searchParams.keys()].length !== 0 || url.hash !== "") return denied("curl target is outside the local UploadDescriptor boundary");
  const expected = new Map([
    ["Content-Length", String(policy.archive_size)],
    ["Content-Type", "application/zip"],
    ["Idempotency-Key", match[1]],
    ["X-Content-SHA256", policy.archive_sha256],
  ]);
  if (headers.size !== expected.size || [...expected].some(([name, value]) => headers.get(name) !== value)) return denied("curl headers do not bind the frozen archive and attachment");
  return { allowed: true, step: "upload-curl" };
}

function downloadCurlContract(words, policy) {
  let method = null;
  let maxTime = null;
  let output = null;
  let target = null;
  for (let index = 1; index < words.length; index += 1) {
    const word = words[index];
    if (["--silent", "--show-error", "--fail-with-body"].includes(word)) continue;
    if (word === "--request") { method = words[index += 1] ?? null; continue; }
    if (word === "--max-time") { if (maxTime !== null) return denied("download curl --max-time must appear exactly once"); maxTime = words[index += 1] ?? null; continue; }
    if (word === "--output") { if (output !== null) return denied("download curl --output must appear exactly once"); output = words[index += 1] ?? null; continue; }
    if (/^https?:\/\//u.test(word) && target === null) { target = word; continue; }
    return denied("download curl contains an unsupported option or argument");
  }
  if (method !== "GET" || maxTime !== "60" || output !== policy.download_path || target === null) return denied("curl must be one 60-second-bounded GET to the frozen result path");
  let url;
  try { url = new URL(target); } catch { return denied("download curl target is not a valid URL"); }
  const match = url.pathname.match(/^\/api\/v1\/artifacts\/([0-9a-f-]{36})\/content$/u);
  if (url.origin !== policy.upload_origin || match === null || url.searchParams.size !== 1 || !/^[0-9a-f-]{36}$/u.test(url.searchParams.get("case_id") ?? "") || url.hash !== "") return denied("download curl target is outside the local Artifact boundary");
  return { allowed: true, step: "download-curl" };
}

function classify(words, policy) {
  if (words === null) return denied("Bash chaining, substitution, multiline input, and empty commands are forbidden");
  if (words.length === 4 && words[0] === "/usr/bin/openssl" && words[1] === "dgst" && words[2] === "-sha256" && words[3] === policy.archive_path) return { allowed: true, step: "upload-openssl" };
  if (words.length === 4 && words[0] === "/usr/bin/stat" && words[1] === "-f" && words[2] === "%z" && words[3] === policy.archive_path) return { allowed: true, step: "upload-stat" };
  if (policy.expect_download && words.length === 4 && words[0] === "/usr/bin/stat" && words[1] === "-f" && words[2] === "%z" && words[3] === policy.download_path) return { allowed: true, step: "download-stat" };
  if (policy.expect_download && words.length === 4 && words[0] === "/usr/bin/openssl" && words[1] === "dgst" && words[2] === "-sha256" && words[3] === policy.download_path) return { allowed: true, step: "download-openssl" };
  if (words[0] === "/usr/bin/curl") return words.includes("PUT") ? uploadCurlContract(words, policy) : policy.expect_download ? downloadCurlContract(words, policy) : denied("Artifact download is not expected for this scenario");
  return denied("Only the frozen upload checks, descriptor PUT, Artifact GET, and result checks are allowed");
}

function claim(policy, step) {
  const steps = policy.expect_download ? [...UPLOAD_STEPS, ...DOWNLOAD_STEPS] : [...UPLOAD_STEPS];
  const index = steps.indexOf(step);
  if (index < 0) return denied("Bash policy step is invalid");
  fs.mkdirSync(policy.claim_root, { recursive: true, mode: 0o700 });
  if (steps.slice(0, index).some((prior) => !fs.existsSync(path.join(policy.claim_root, prior)))) return denied("Allowed Bash commands must execute in the frozen upload/download order");
  const destination = path.join(policy.claim_root, step);
  try { fs.writeFileSync(destination, `${step}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" }); }
  catch (error) { return error.code === "EEXIST" ? denied(`Bash ${step} may execute only once`) : denied("Bash policy claim could not be sealed"); }
  return { allowed: true, step };
}

export function evaluateBashToolUse(policy, event) {
  if (policy?.schema_version !== 2 || typeof policy.archive_path !== "string" || !path.isAbsolute(policy.archive_path) || !Number.isSafeInteger(policy.archive_size) || !/^[a-f0-9]{64}$/u.test(policy.archive_sha256 ?? "") || !/^http:\/\/127\.0\.0\.1:\d+$/u.test(policy.upload_origin ?? "") || typeof policy.claim_root !== "string" || !path.isAbsolute(policy.claim_root) || typeof policy.expect_download !== "boolean" || (policy.expect_download && (typeof policy.download_path !== "string" || !path.isAbsolute(policy.download_path)))) return denied("Bash policy is invalid");
  if (event?.tool_name !== "Bash") return denied("The Bash policy received a non-Bash tool");
  const classified = classify(shellWords(event.tool_input?.command), policy);
  return classified.allowed ? claim(policy, classified.step) : classified;
}

function parsePolicy(argv) {
  if (argv.length !== 2 || argv[0] !== "--policy") throw new Error("Bash policy requires --policy <absolute-json>");
  const policyPath = path.resolve(argv[1]);
  return JSON.parse(fs.readFileSync(policyPath, "utf8"));
}

function main() {
  try {
    const policy = parsePolicy(process.argv.slice(2));
    const event = JSON.parse(fs.readFileSync(0, "utf8"));
    const result = evaluateBashToolUse(policy, event);
    if (!result.allowed) { process.stderr.write(`${result.reason}\n`); process.exitCode = 2; }
  } catch { process.stderr.write("Claude/DeepSeek Bash policy rejected an invalid invocation\n"); process.exitCode = 2; }
}

if (process.argv[1] && path.resolve(process.argv[1]) === MODULE_PATH) main();
