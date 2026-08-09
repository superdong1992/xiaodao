#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const PROPOSAL_DIRECTORY = /^p-[0-9a-f]{64}$/;
const TERMINAL_JOB_STATUSES = new Set(["SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"]);
const ROOT_ENTRIES = new Set([".instance.lock", "data-format.json", "jobs", "resources", "state.json", "state.json.prev", "tmp"]);
const REQUIRED_ROOT_ENTRIES = new Set(["data-format.json", "jobs", "resources", "state.json", "tmp"]);
const TEMPORARY_ENTRIES = new Set(["proposals", "quarantine", "state", "uploads", "workspaces"]);

export class CheckpointTemporaryError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

function requireCondition(condition, code) {
  if (!condition) throw new CheckpointTemporaryError(code);
}

function canonicalJson(value) {
  const normalize = (item) => {
    if (Array.isArray(item)) return item.map(normalize);
    if (item && typeof item === "object") {
      return Object.fromEntries(Object.keys(item).sort().map((key) => [key, normalize(item[key])]));
    }
    return item;
  };
  return `${JSON.stringify(normalize(value))}\n`;
}

function objectMap(value, code) {
  requireCondition(value && typeof value === "object" && !Array.isArray(value), code);
  return value;
}

function realDirectory(directory, code) {
  let metadata;
  try {
    metadata = fs.lstatSync(directory);
  } catch {
    throw new CheckpointTemporaryError(code);
  }
  requireCondition(metadata.isDirectory() && !metadata.isSymbolicLink(), code);
  return directory;
}

function ordinaryFile(filePath, code) {
  let metadata;
  try {
    metadata = fs.lstatSync(filePath);
  } catch {
    throw new CheckpointTemporaryError(code);
  }
  requireCondition(metadata.isFile() && !metadata.isSymbolicLink() && metadata.nlink === 1, code);
  return filePath;
}

function exactEntries(directory, allowed, required, code) {
  const entries = fs.readdirSync(realDirectory(directory, code));
  requireCondition(entries.every((name) => allowed.has(name)), code);
  requireCondition([...required].every((name) => entries.includes(name)), code);
  return entries;
}

function readCanonicalObject(filePath, code) {
  const bytes = fs.readFileSync(ordinaryFile(filePath, code));
  let value;
  try {
    value = JSON.parse(bytes.toString("utf8"));
  } catch {
    throw new CheckpointTemporaryError(code);
  }
  objectMap(value, code);
  requireCondition(Buffer.from(canonicalJson(value), "utf8").equals(bytes), code);
  return value;
}

function readValidatedState(filePath) {
  let value;
  try {
    value = JSON.parse(fs.readFileSync(ordinaryFile(filePath, "CHECKPOINT_STATE_SHAPE_INVALID"), "utf8"));
  } catch {
    throw new CheckpointTemporaryError("CHECKPOINT_STATE_SHAPE_INVALID");
  }
  return objectMap(value, "CHECKPOINT_STATE_SHAPE_INVALID");
}

function scanDiscardedTree(root, code) {
  const pending = [realDirectory(root, code)];
  while (pending.length > 0) {
    const directory = pending.pop();
    for (const name of fs.readdirSync(directory)) {
      const candidate = path.join(directory, name);
      const metadata = fs.lstatSync(candidate);
      requireCondition(!metadata.isSymbolicLink(), code);
      if (metadata.isDirectory()) {
        pending.push(candidate);
      } else {
        requireCondition(metadata.isFile() && metadata.nlink === 1, code);
      }
    }
  }
}

function sha256Text(value) {
  return crypto.createHash("sha256").update(value, "utf8").digest("hex");
}

function stateFacts(state) {
  const jobs = new Map();
  const attachments = new Map();
  const processedJobs = new Set();
  const proposalMarkers = new Map();
  const generatedProposalMarkers = new Map();
  const cases = objectMap(state.cases, "CHECKPOINT_STATE_SHAPE_INVALID");
  for (const aggregate of Object.values(cases)) {
    objectMap(aggregate, "CHECKPOINT_STATE_SHAPE_INVALID");
    for (const [jobId, job] of Object.entries(objectMap(aggregate.jobs, "CHECKPOINT_STATE_SHAPE_INVALID"))) {
      requireCondition(UUID.test(jobId) && job?.job_id === jobId && typeof job.status === "string", "CHECKPOINT_STATE_SHAPE_INVALID");
      requireCondition(!jobs.has(jobId), "CHECKPOINT_STATE_SHAPE_INVALID");
      jobs.set(jobId, job);
    }
    for (const [attachmentId, attachment] of Object.entries(objectMap(aggregate.attachments, "CHECKPOINT_STATE_SHAPE_INVALID"))) {
      requireCondition(UUID.test(attachmentId) && attachment?.attachment_id === attachmentId, "CHECKPOINT_STATE_SHAPE_INVALID");
      requireCondition(!attachments.has(attachmentId), "CHECKPOINT_STATE_SHAPE_INVALID");
      attachments.set(attachmentId, attachment);
    }
    const processing = objectMap(aggregate.outcome_processing_records, "CHECKPOINT_STATE_SHAPE_INVALID");
    for (const [outcomeId, record] of Object.entries(processing)) {
      requireCondition(UUID.test(outcomeId) && record?.outcome_id === outcomeId && UUID.test(record?.job_id ?? ""), "CHECKPOINT_STATE_SHAPE_INVALID");
      processedJobs.add(record.job_id);
    }
    const outcomes = objectMap(aggregate.outcomes, "CHECKPOINT_STATE_SHAPE_INVALID");
    for (const [outcomeId, outcome] of Object.entries(outcomes)) {
      const record = processing[outcomeId];
      requireCondition(record?.job_id === outcome?.job_id && UUID.test(outcome.job_id), "CHECKPOINT_STATE_SHAPE_INVALID");
      for (const collectionName of ["proposed_evidence", "proposed_artifacts"]) {
        const proposals = outcome[collectionName];
        requireCondition(Array.isArray(proposals), "CHECKPOINT_STATE_SHAPE_INVALID");
        for (const proposal of proposals) {
          const staged = proposal?.staged_resource_ref;
          if (staged === null || staged === undefined) continue;
          const proposalKey = staged.proposal_key;
          requireCondition(
            staged.owner_job_id === outcome.job_id
              && typeof proposalKey === "string"
              && proposalKey.trim().length > 0
              && ["FILE", "DIRECTORY"].includes(staged.resource_kind),
            "CHECKPOINT_STATE_SHAPE_INVALID",
          );
          const relative = `${staged.owner_job_id}/p-${sha256Text(proposalKey)}`;
          const previous = proposalMarkers.get(relative);
          requireCondition(previous === undefined || canonicalJson(previous) === canonicalJson(staged), "CHECKPOINT_STATE_SHAPE_INVALID");
          proposalMarkers.set(relative, staged);
        }
      }
    }
    for (const [artifactId, artifact] of Object.entries(objectMap(aggregate.artifacts, "CHECKPOINT_STATE_SHAPE_INVALID"))) {
      if (artifact?.kind !== "AUDIT_BUNDLE") continue;
      const ownerJobId = artifact.created_by_job_id;
      const sourceOutcomeId = artifact.metadata?.source_outcome_id;
      const record = processing[sourceOutcomeId];
      requireCondition(
        UUID.test(artifactId)
          && UUID.test(ownerJobId ?? "")
          && artifact.metadata?.source_job_id === ownerJobId
          && record?.job_id === ownerJobId
          && Array.isArray(record.generated_artifact_ids)
          && record.generated_artifact_ids.includes(artifactId)
          && artifact.resource_kind === "FILE"
          && Number.isInteger(artifact.size)
          && typeof artifact.sha256 === "string",
        "CHECKPOINT_STATE_SHAPE_INVALID",
      );
      const relative = `${ownerJobId}/p-${sha256Text("server-audit-bundle")}`;
      requireCondition(!generatedProposalMarkers.has(relative) && !proposalMarkers.has(relative), "CHECKPOINT_STATE_SHAPE_INVALID");
      generatedProposalMarkers.set(relative, {
        owner_job_id: ownerJobId,
        proposal_key: "server-audit-bundle",
        resource_kind: "FILE",
        size: artifact.size,
        sha256: artifact.sha256,
        tree_manifest: null,
      });
    }
  }
  requireCondition([...jobs.values()].every((job) => TERMINAL_JOB_STATUSES.has(job.status)), "CHECKPOINT_NONTERMINAL_JOB");
  return { jobs, attachments, processedJobs, proposalMarkers, generatedProposalMarkers };
}

function verifyOutboxClear(dataRoot, facts) {
  const jobsRoot = realDirectory(path.join(dataRoot, "jobs"), "CHECKPOINT_JOB_LAYOUT_INVALID");
  for (const jobId of fs.readdirSync(jobsRoot)) {
    requireCondition(UUID.test(jobId), "CHECKPOINT_JOB_LAYOUT_INVALID");
    const jobRoot = realDirectory(path.join(jobsRoot, jobId), "CHECKPOINT_JOB_LAYOUT_INVALID");
    const outcomePath = path.join(jobRoot, "job_outcome.json");
    if (fs.existsSync(outcomePath)) {
      ordinaryFile(outcomePath, "CHECKPOINT_JOB_LAYOUT_INVALID");
      requireCondition(facts.processedJobs.has(jobId), "CHECKPOINT_UNPROCESSED_OUTCOME");
    }
  }
}

function verifyUploads(dataRoot, facts) {
  const uploads = realDirectory(path.join(dataRoot, "tmp", "uploads"), "CHECKPOINT_UPLOAD_STAGE_INVALID");
  let count = 0;
  for (const attachmentId of fs.readdirSync(uploads)) {
    requireCondition(UUID.test(attachmentId), "CHECKPOINT_UPLOAD_STAGE_INVALID");
    const stage = realDirectory(path.join(uploads, attachmentId), "CHECKPOINT_UPLOAD_STAGE_INVALID");
    const attachment = facts.attachments.get(attachmentId);
    requireCondition(
      attachment?.status === "READY"
        && Number.isInteger(attachment.size)
        && typeof attachment.sha256 === "string"
        && typeof attachment.storage_key === "string",
      "CHECKPOINT_UPLOAD_STAGE_NOT_DISCARDABLE",
    );
    const expected = {
      attachment_id: attachmentId,
      resource_kind: "FILE",
      size: attachment.size,
      sha256: attachment.sha256,
    };
    const marker = readCanonicalObject(path.join(stage, "staged.json"), "CHECKPOINT_UPLOAD_STAGE_INVALID");
    requireCondition(canonicalJson(marker) === canonicalJson(expected), "CHECKPOINT_UPLOAD_STAGE_INVALID");
    const entries = exactEntries(stage, new Set(["payload", "staged.json"]), new Set(["staged.json"]), "CHECKPOINT_UPLOAD_STAGE_INVALID");
    if (entries.includes("payload")) ordinaryFile(path.join(stage, "payload"), "CHECKPOINT_UPLOAD_STAGE_INVALID");
    scanDiscardedTree(stage, "CHECKPOINT_UPLOAD_STAGE_INVALID");
    count += 1;
  }
  return count;
}

function verifyProposals(dataRoot, facts) {
  const proposals = realDirectory(path.join(dataRoot, "tmp", "proposals"), "CHECKPOINT_PROPOSAL_STAGE_INVALID");
  let stageCount = 0;
  let ownerCount = 0;
  for (const ownerJobId of fs.readdirSync(proposals)) {
    requireCondition(UUID.test(ownerJobId) && TERMINAL_JOB_STATUSES.has(facts.jobs.get(ownerJobId)?.status), "CHECKPOINT_PROPOSAL_STAGE_NOT_DISCARDABLE");
    const owner = realDirectory(path.join(proposals, ownerJobId), "CHECKPOINT_PROPOSAL_STAGE_INVALID");
    ownerCount += 1;
    for (const directoryName of fs.readdirSync(owner)) {
      requireCondition(PROPOSAL_DIRECTORY.test(directoryName), "CHECKPOINT_PROPOSAL_STAGE_INVALID");
      const relative = `${ownerJobId}/${directoryName}`;
      const expected = facts.proposalMarkers.get(relative);
      const generated = facts.generatedProposalMarkers.get(relative);
      requireCondition((expected !== undefined || generated !== undefined) && facts.processedJobs.has(ownerJobId), "CHECKPOINT_PROPOSAL_STAGE_NOT_DISCARDABLE");
      const stage = realDirectory(path.join(owner, directoryName), "CHECKPOINT_PROPOSAL_STAGE_INVALID");
      const marker = readCanonicalObject(path.join(stage, "staged.json"), "CHECKPOINT_PROPOSAL_STAGE_INVALID");
      if (expected !== undefined) {
        requireCondition(canonicalJson(marker) === canonicalJson(expected), "CHECKPOINT_PROPOSAL_STAGE_INVALID");
      } else {
        requireCondition(
          UUID.test(marker.staging_id ?? "")
            && Object.keys(marker).sort().join(",") === "owner_job_id,proposal_key,resource_kind,sha256,size,staging_id,tree_manifest"
            && Object.entries(generated).every(([name, value]) => canonicalJson(marker[name]) === canonicalJson(value)),
          "CHECKPOINT_PROPOSAL_STAGE_INVALID",
        );
      }
      const resourceKind = expected?.resource_kind ?? generated.resource_kind;
      const contentName = resourceKind === "DIRECTORY" ? "tree" : "payload";
      const entries = exactEntries(stage, new Set([contentName, "staged.json"]), new Set(["staged.json"]), "CHECKPOINT_PROPOSAL_STAGE_INVALID");
      if (entries.includes(contentName)) scanDiscardedTree(stage, "CHECKPOINT_PROPOSAL_STAGE_INVALID");
      stageCount += 1;
    }
  }
  return { stageCount, ownerCount };
}

export function classifyCheckpointTemporary(dataRoot) {
  const root = path.resolve(dataRoot);
  const rootEntries = exactEntries(root, ROOT_ENTRIES, REQUIRED_ROOT_ENTRIES, "CHECKPOINT_ROOT_LAYOUT_INVALID");
  for (const name of ["jobs", "resources", "tmp"]) realDirectory(path.join(root, name), "CHECKPOINT_ROOT_LAYOUT_INVALID");
  for (const name of ["data-format.json", "state.json"]) ordinaryFile(path.join(root, name), "CHECKPOINT_ROOT_LAYOUT_INVALID");
  for (const optional of [".instance.lock", "state.json.prev"]) {
    if (rootEntries.includes(optional)) ordinaryFile(path.join(root, optional), "CHECKPOINT_ROOT_LAYOUT_INVALID");
  }
  const temporary = path.join(root, "tmp");
  exactEntries(temporary, TEMPORARY_ENTRIES, TEMPORARY_ENTRIES, "CHECKPOINT_TEMP_ROOT_LAYOUT_INVALID");
  // The product's immediately preceding validate-state command is the
  // authority for StateFile schema and Python canonical JSON.  Do not
  // reserialize the whole StateFile with JavaScript: JSON.parse/stringify
  // cannot preserve Python's canonical distinction between 1.0 and 1.
  const state = readValidatedState(path.join(root, "state.json"));
  const facts = stateFacts(state);
  verifyOutboxClear(root, facts);
  const uploads = verifyUploads(root, facts);
  const proposals = verifyProposals(root, facts);
  const quarantineEntries = fs.readdirSync(realDirectory(path.join(temporary, "quarantine"), "CHECKPOINT_QUARANTINE_INVALID")).length;
  requireCondition(quarantineEntries === 0, "CHECKPOINT_QUARANTINE_NOT_EMPTY");
  const stateTemporaryEntries = fs.readdirSync(realDirectory(path.join(temporary, "state"), "CHECKPOINT_STATE_TEMP_INVALID")).length;
  requireCondition(stateTemporaryEntries === 0, "CHECKPOINT_STATE_TEMP_NOT_EMPTY");
  return {
    schema_version: 1,
    status: "PASS",
    code: null,
    outbox_clear: true,
    excluded_completed_uploads: uploads,
    excluded_processed_proposal_stages: proposals.stageCount,
    excluded_proposal_owner_directories: proposals.ownerCount,
    quarantine_entries: 0,
    state_temporary_entries: 0,
  };
}

function parseCli(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    requireCondition(["--data-root", "--output"].includes(name) && typeof argv[index + 1] === "string", "CHECKPOINT_CLASSIFIER_ARGUMENT_INVALID");
    values[name.slice(2).replaceAll("-", "_")] = argv[index + 1];
  }
  requireCondition(Object.keys(values).length === 2 && path.isAbsolute(values.data_root) && path.isAbsolute(values.output), "CHECKPOINT_CLASSIFIER_ARGUMENT_INVALID");
  return values;
}

function writeReceipt(filePath, receipt) {
  const descriptor = fs.openSync(filePath, "wx", 0o600);
  try {
    fs.writeFileSync(descriptor, canonicalJson(receipt), "utf8");
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

function main() {
  let values;
  try {
    values = parseCli(process.argv.slice(2));
    writeReceipt(values.output, classifyCheckpointTemporary(values.data_root));
  } catch (error) {
    const code = error instanceof CheckpointTemporaryError ? error.code : "CHECKPOINT_TEMPORARY_CLASSIFICATION_ERROR";
    if (values?.output && path.isAbsolute(values.output) && !fs.existsSync(values.output)) {
      writeReceipt(values.output, { schema_version: 1, status: "FAIL", code });
    }
    process.exitCode = 1;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) main();
