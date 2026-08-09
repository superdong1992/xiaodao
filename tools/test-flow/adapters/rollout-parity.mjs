#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import {
  assertFlow,
  atomicCreateJson,
  canonicalJson,
  readJson,
  runSync,
  sha256Bytes,
  sha256File,
} from "../lib/util.mjs";

function argumentsOf(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    assertFlow(name?.startsWith("--") && value !== undefined, "PARITY_ARGUMENT", "Parity adapter arguments must be name/value pairs");
    values[name.slice(2)] = value;
  }
  return values;
}

function currentSource(repoRoot) {
  const head = runSync("git", ["rev-parse", "HEAD"], { cwd: repoRoot });
  const status = runSync("git", ["status", "--porcelain=v1", "--untracked-files=all"], { cwd: repoRoot });
  assertFlow(head.status === 0 && status.status === 0, "PARITY_GIT", "Parity requires an inspectable Git worktree");
  return { head: head.stdout.trim(), clean: status.stdout.trim() === "" };
}

function validateCommand(label, value) {
  assertFlow(value && typeof value === "object" && !Array.isArray(value), "PARITY_COMMAND", `${label} command must be an object`);
  assertFlow(path.isAbsolute(value.executable) && fs.existsSync(value.executable), "PARITY_EXECUTABLE", `${label} executable must be an existing absolute file`);
  assertFlow(/^[a-f0-9]{64}$/i.test(value.executable_sha256 ?? ""), "PARITY_EXECUTABLE_DIGEST", `${label} executable_sha256 is required`);
  assertFlow(sha256File(value.executable) === value.executable_sha256.toLowerCase(), "PARITY_EXECUTABLE_CHANGED", `${label} executable digest does not match`);
  assertFlow(Array.isArray(value.arguments) && value.arguments.every((item) => typeof item === "string"), "PARITY_COMMAND_ARGUMENTS", `${label} arguments must be a string array`);
  assertFlow(!value.arguments.includes("release.rollout-parity"), "PARITY_RECURSION", "The candidate command must not recursively invoke the parity goal");
  assertFlow(Array.isArray(value.inputs) && value.inputs.length > 0, "PARITY_INPUTS", `${label} must freeze at least one command input`);
  for (const input of value.inputs) {
    assertFlow(input && typeof input === "object" && !Array.isArray(input), "PARITY_INPUT", `${label} input must be an object`);
    assertFlow(path.isAbsolute(input.path) && fs.existsSync(input.path), "PARITY_INPUT_PATH", `${label} input must be an existing absolute file`);
    assertFlow(/^[a-f0-9]{64}$/i.test(input.sha256 ?? "") && sha256File(input.path) === input.sha256.toLowerCase(), "PARITY_INPUT_CHANGED", `${label} frozen input changed: ${input.path}`);
  }
  return value;
}

function progress(label, phase) {
  process.stdout.write(`TEST_FLOW_PROGRESS stage.progress parity-${label}-${phase}\n`);
}

function runCommand(label, command, repoRoot) {
  return new Promise((resolve, reject) => {
    validateCommand(label, command);
    const before = currentSource(repoRoot);
    assertFlow(before.clean, "PARITY_SOURCE_DIRTY", `Source became dirty before ${label}`);
    progress(label, "started");
    const started = process.hrtime.bigint();
    const child = spawn(command.executable, command.arguments, {
      cwd: repoRoot,
      env: process.env,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    const inspect = (chunk) => {
      const text = chunk.toString("utf8");
      if (/E2E_STEP_(?:START|END)|TEST_FLOW_PROGRESS|"event_type"\s*:/.test(text)) progress(label, "semantic-event");
    };
    child.stdout.on("data", (chunk) => { process.stdout.write(chunk); inspect(chunk); });
    child.stderr.on("data", (chunk) => { process.stderr.write(chunk); inspect(chunk); });
    child.once("error", reject);
    child.once("exit", (code, signal) => {
      const after = currentSource(repoRoot);
      const elapsed = Number(process.hrtime.bigint() - started) / 1e9;
      progress(label, "completed");
      resolve({
        label,
        status: code === 0 && signal === null && after.clean && after.head === before.head ? "PASS" : "FAIL",
        exit_code: code,
        signal,
        elapsed_seconds: Math.round(elapsed * 1000) / 1000,
        source_commit_before: before.head,
        source_commit_after: after.head,
        source_clean_after: after.clean,
        executable_sha256: command.executable_sha256.toLowerCase(),
        input_digests: command.inputs.map((input) => input.sha256.toLowerCase()),
      });
    });
  });
}

async function main() {
  const args = argumentsOf(process.argv.slice(2));
  for (const name of ["repo-root", "attempt-root", "output-root", "spec", "expected-source-commit", "expected-producer-identity"]) {
    assertFlow(typeof args[name] === "string" && args[name].length > 0, "PARITY_ARGUMENT_MISSING", `Missing --${name}`);
  }
  const repoRoot = path.resolve(args["repo-root"]);
  const outputRoot = path.resolve(args["output-root"]);
  const specPath = path.resolve(args.spec);
  assertFlow(path.isAbsolute(args.spec) && fs.existsSync(specPath), "PARITY_SPEC", "Parity spec must be an existing absolute file");
  const rawSpec = fs.readFileSync(specPath);
  const spec = readJson(specPath);
  assertFlow(spec.schema_version === 1, "PARITY_SPEC_VERSION", "Unsupported parity spec version");
  assertFlow(spec.source_commit === args["expected-source-commit"], "PARITY_SPEC_COMMIT", "Parity spec is not bound to the admitted source commit");
  assertFlow(/^[a-f0-9]{64}$/i.test(args["expected-producer-identity"]), "PARITY_PRODUCER_IDENTITY", "Expected producer identity is invalid");
  const source = currentSource(repoRoot);
  assertFlow(source.clean && source.head === spec.source_commit, "PARITY_SOURCE_ADMISSION", "Parity requires the exact clean source commit");
  const commands = {
    legacy: validateCommand("legacy", spec.legacy),
    candidate: validateCommand("candidate", spec.candidate),
  };
  const specDigest = sha256Bytes(rawSpec);
  const ledgerRoot = path.join(repoRoot, ".tmp", "test-flow-rollout-parity");
  const ledgerName = `${source.head.slice(0, 12)}-${args["expected-producer-identity"].slice(0, 12)}-${specDigest.slice(0, 12)}.json`;
  atomicCreateJson(path.join(ledgerRoot, ledgerName), {
    schema_version: 1,
    status: "RESERVED",
    source_commit: source.head,
    producer_identity: args["expected-producer-identity"],
    spec_digest: specDigest,
    attempt_id: path.basename(path.resolve(args["attempt-root"])),
    created_at_utc: new Date().toISOString(),
  });

  const runs = [];
  const legacy = await runCommand("legacy", commands.legacy, repoRoot);
  runs.push(legacy);
  if (legacy.status === "PASS") runs.push(await runCommand("candidate", commands.candidate, repoRoot));
  else runs.push({ label: "candidate", status: "NOT_RUN", exit_code: null, signal: null, elapsed_seconds: 0 });
  const status = runs.every((run) => run.status === "PASS") ? "PASS" : "FAIL";
  atomicCreateJson(path.join(outputRoot, "rollout-parity-receipt.json"), {
    schema_version: 1,
    status,
    source_commit: source.head,
    producer_identity: args["expected-producer-identity"],
    spec_digest: specDigest,
    order: ["legacy", "candidate"],
    runs,
    completed_at_utc: new Date().toISOString(),
  });
  process.stdout.write(canonicalJson({ status, pair: "legacy-then-candidate" }));
  process.exitCode = status === "PASS" ? 0 : 1;
}

main().catch((error) => {
  process.stderr.write(`${JSON.stringify({ status: "ERROR", code: error?.code ?? "PARITY_UNEXPECTED", message: String(error?.message ?? error) })}\n`);
  process.exitCode = 3;
});
