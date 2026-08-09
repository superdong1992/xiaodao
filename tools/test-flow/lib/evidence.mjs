import fs from "node:fs";
import path from "node:path";
import { atomicCreateJson, canonicalJson, ensureDirectory, readJson, sha256Bytes, sha256File, writeJsonSync } from "./util.mjs";
import { validateEventFile } from "./events.mjs";

const SECRET_PATTERNS = [
  { code: "ANTHROPIC_KEY", expression: /sk-ant-[A-Za-z0-9_-]{16,}/g },
  { code: "BEARER_TOKEN", expression: /Bearer\s+[A-Za-z0-9._~+/=-]{16,}/gi },
  { code: "PRIVATE_KEY", expression: /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/g },
  { code: "GENERIC_SECRET", expression: /(?:api[_-]?key|auth[_-]?token|password|client[_-]?secret)\s*[:=]\s*["']?[A-Za-z0-9._~+/=-]{16,}/gi },
];

function listFiles(root, relative = "", output = []) {
  if (!fs.existsSync(root)) return output;
  const entries = fs.readdirSync(path.join(root, relative), { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name));
  for (const entry of entries) {
    const child = relative ? `${relative}/${entry.name}` : entry.name;
    if (entry.isSymbolicLink()) {
      output.push({ path: child, kind: "symlink", target: fs.readlinkSync(path.join(root, child)) });
    } else if (entry.isDirectory()) {
      listFiles(root, child, output);
    } else if (entry.isFile()) {
      const filePath = path.join(root, child);
      output.push({ path: child, kind: "file", size: fs.statSync(filePath).size, sha256: sha256File(filePath) });
    } else {
      output.push({ path: child, kind: "unsupported" });
    }
  }
  return output;
}

function scanFile(filePath, relativePath, knownSecrets) {
  const buffer = fs.readFileSync(filePath);
  const text = buffer.toString("utf8");
  const hits = [];
  for (const pattern of SECRET_PATTERNS) {
    pattern.expression.lastIndex = 0;
    if (pattern.expression.test(text)) hits.push({ path: relativePath, code: pattern.code });
  }
  for (const secret of knownSecrets.filter((value) => typeof value === "string" && value.length >= 8)) {
    if (buffer.includes(Buffer.from(secret))) hits.push({ path: relativePath, code: "KNOWN_SECRET" });
  }
  return hits;
}

export function scanPayload(payloadRoot, { knownSecrets = [] } = {}) {
  const files = listFiles(payloadRoot);
  const hits = [];
  for (const entry of files) {
    if (entry.kind !== "file") continue;
    hits.push(...scanFile(path.join(payloadRoot, entry.path), entry.path, knownSecrets));
  }
  const manifest = files.map(({ path: filePath, kind, size, sha256, target }) => ({ path: filePath, kind, size, sha256, target }));
  return {
    schema_version: 1,
    status: hits.length === 0 ? "PASS" : "FAIL",
    scanner: "test-flow-secret-scan-v1",
    scanned_root_digest: sha256Bytes(canonicalJson(manifest)),
    files_scanned: files.filter((entry) => entry.kind === "file").length,
    sensitive_value_occurrences: hits.length,
    hits,
  };
}

export function sealPayload(payloadRoot) {
  const files = listFiles(payloadRoot);
  const invalid = files.filter((entry) => entry.kind !== "file");
  return {
    schema_version: 1,
    status: invalid.length === 0 ? "PASS" : "FAIL",
    root_digest: sha256Bytes(canonicalJson(files)),
    files,
    invalid_entries: invalid.map((entry) => entry.path),
  };
}

export function verifyPayloadSeal(payloadRoot, seal) {
  const current = sealPayload(payloadRoot);
  return {
    status: current.status === "PASS" && seal.status === "PASS" && current.root_digest === seal.root_digest ? "PASS" : "FAIL",
    expected_digest: seal.root_digest,
    actual_digest: current.root_digest,
  };
}

export function createAttempt({ evidenceRoot, runId }) {
  ensureDirectory(evidenceRoot);
  const attemptRoot = path.join(evidenceRoot, runId);
  fs.mkdirSync(attemptRoot, { recursive: false, mode: 0o700 });
  for (const child of ["payload", "payload/events", "payload/logs", "payload/stages", "payload/checkpoints", "finalization"]) {
    ensureDirectory(path.join(attemptRoot, child));
  }
  writeJsonSync(path.join(attemptRoot, "attempt.json"), {
    schema_version: 1,
    run_id: runId,
    state: "CREATED",
    created_at_utc: new Date().toISOString(),
  });
  return attemptRoot;
}

export function requiredEventFiles(stages = []) {
  const required = ["orchestrator.ndjson"];
  if (stages.some((stage) => stage.id?.startsWith("journey.cross-job.") && stage.status === "PASS" && stage.result_source !== "REUSED")) {
    required.push("service-linux.journey.ndjson", "service-linux.diagnostics.ndjson");
  }
  return required;
}

export function validateEvidenceStreams(attemptRoot, { requiredFiles = ["orchestrator.ndjson"] } = {}) {
  const eventsRoot = path.join(attemptRoot, "payload", "events");
  const results = [];
  let status = "PASS";
  if (fs.existsSync(eventsRoot)) {
    for (const name of fs.readdirSync(eventsRoot).filter((entry) => entry.endsWith(".ndjson")).sort()) {
      try {
        results.push({ file: name, ...validateEventFile(path.join(eventsRoot, name)) });
      } catch (error) {
        status = "FAIL";
        results.push({ file: name, status: "FAIL", code: error?.code ?? "EVENT_STREAM_INVALID" });
      }
    }
  }
  const present = new Set(results.filter((result) => result.status === "PASS").map((result) => result.file));
  const missing = requiredFiles.filter((name) => !present.has(name));
  if (missing.length > 0) status = "FAIL";
  return { status, required_files: requiredFiles, missing_files: missing, streams: results };
}

export async function finalizeAttempt({
  attemptRoot,
  candidate,
  resourcePolicy,
  knownSecrets = [],
  compatibilityReport = true,
}) {
  const verdictPath = path.join(attemptRoot, "verdict.json");
  if (fs.existsSync(verdictPath)) return readJson(verdictPath);

  const streams = validateEvidenceStreams(attemptRoot, { requiredFiles: requiredEventFiles(candidate.stages) });
  writeJsonSync(path.join(attemptRoot, "payload", "candidate-result.json"), candidate);
  writeJsonSync(path.join(attemptRoot, "payload", "event-audit.json"), streams);
  let payloadSeal;
  try { payloadSeal = sealPayload(path.join(attemptRoot, "payload")); } catch {
    payloadSeal = { schema_version: 1, status: "ERROR", root_digest: null, files: [], invalid_entries: [], code: "PAYLOAD_SEAL_ERROR" };
  }
  writeJsonSync(path.join(attemptRoot, "finalization", "payload-seal.json"), payloadSeal);
  let scan;
  try { scan = scanPayload(path.join(attemptRoot, "payload"), { knownSecrets }); } catch {
    scan = { schema_version: 1, status: "ERROR", scanner: "test-flow-secret-scan-v1", scanned_root_digest: null, files_scanned: 0, sensitive_value_occurrences: 0, hits: [], code: "SECRET_SCAN_ERROR" };
  }
  writeJsonSync(path.join(attemptRoot, "finalization", "secret-scan.json"), scan);

  const preserve = candidate.functional_status !== "PASS" || scan.status !== "PASS" || payloadSeal.status !== "PASS";
  let resourceReceipt;
  try {
    resourceReceipt = await resourcePolicy({ preserve, runId: candidate.run_id });
  } catch (error) {
    resourceReceipt = { schema_version: 1, status: "ERROR", preserve, code: "RESOURCE_POLICY_FAILED", remaining: [] };
  }
  writeJsonSync(path.join(attemptRoot, "finalization", "resource-receipt.json"), resourceReceipt);

  let operationStatus = candidate.operation_status;
  let failureDomain = candidate.failure_domain ?? null;
  if (scan.status !== "PASS") {
    operationStatus = "ERROR";
    failureDomain = "SECURITY";
  } else if (payloadSeal.status !== "PASS" || streams.status !== "PASS") {
    operationStatus = "ERROR";
    failureDomain = "HARNESS";
  } else if (resourceReceipt.status !== "PASS") {
    operationStatus = "ERROR";
    failureDomain = failureDomain ?? "INFRA";
  }
  let metaScan;
  try { metaScan = scanPayload(path.join(attemptRoot, "finalization"), { knownSecrets }); } catch {
    metaScan = { schema_version: 1, status: "ERROR", scanner: "test-flow-secret-scan-v1", scanned_root_digest: null, files_scanned: 0, sensitive_value_occurrences: 0, hits: [], code: "META_SECRET_SCAN_ERROR" };
  }
  writeJsonSync(path.join(attemptRoot, "finalization", "meta-secret-scan.json"), metaScan);
  if (metaScan.status !== "PASS") {
    operationStatus = "ERROR";
    failureDomain = "SECURITY";
  }
  const overall = operationStatus === "ERROR"
    ? "ERROR"
    : candidate.functional_status === "FAIL"
      ? "FAIL"
      : candidate.functional_status !== "PASS"
        ? "BLOCKED"
        : candidate.performance_status === "FAIL"
          ? "FAIL"
          : candidate.performance_status === "SLOW" || candidate.performance_status === "NOT_CALIBRATED"
            ? "PASS_WITH_WARNINGS"
            : "PASS";
  const exitCode = overall === "ERROR" ? 3 : overall === "FAIL" ? 1 : overall === "BLOCKED" ? 2 : 0;
  const finalizationManifest = listFiles(path.join(attemptRoot, "finalization"));
  const verdict = {
    schema_version: 1,
    run_id: candidate.run_id,
    track: candidate.track,
    goal: candidate.goal,
    functional_status: candidate.functional_status,
    performance_status: candidate.performance_status,
    operation_status: operationStatus,
    overall,
    exit_code: exitCode,
    failure_domain: failureDomain,
    failure_fingerprint: candidate.failure_fingerprint ?? null,
    evidence_reusable: scan.status === "PASS" && metaScan.status === "PASS" && payloadSeal.status === "PASS" && streams.status === "PASS",
    stages: candidate.stages,
    source: candidate.source,
    payload_seal_digest: payloadSeal.root_digest,
    secret_scan_digest: scan.scanned_root_digest,
    resource_receipt_digest: sha256Bytes(canonicalJson(resourceReceipt)),
    finalization_digest: sha256Bytes(canonicalJson(finalizationManifest)),
    committed_at_utc: new Date().toISOString(),
  };
  if (compatibilityReport) {
    writeJsonSync(path.join(attemptRoot, "verification-report.json"), {
      schema_version: 1,
      authority: "verdict.json",
      status: overall,
      functional_status: candidate.functional_status,
      run_id: candidate.run_id,
    });
  }
  atomicCreateJson(verdictPath, verdict);
  return verdict;
}

export function verifyVerdict(attemptRoot) {
  const verdictPath = path.join(attemptRoot, "verdict.json");
  if (!fs.existsSync(verdictPath)) return { status: "UNFINALIZED", verdict: null };
  const verdict = readJson(verdictPath);
  const sealPath = path.join(attemptRoot, "finalization", "payload-seal.json");
  if (!fs.existsSync(sealPath)) return { status: "INVALID", verdict, reason: "MISSING_PAYLOAD_SEAL" };
  const seal = readJson(sealPath);
  const verification = verifyPayloadSeal(path.join(attemptRoot, "payload"), seal);
  const finalizationManifest = listFiles(path.join(attemptRoot, "finalization"));
  const finalizationDigest = sha256Bytes(canonicalJson(finalizationManifest));
  const finalizationValid = finalizationDigest === verdict.finalization_digest;
  return {
    status: verification.status === "PASS" && finalizationValid ? "PASS" : "INVALID",
    verdict,
    verification,
    finalization_verification: {
      status: finalizationValid ? "PASS" : "FAIL",
      expected_digest: verdict.finalization_digest,
      actual_digest: finalizationDigest,
    },
  };
}
