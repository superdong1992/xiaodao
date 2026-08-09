import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  CheckpointTemporaryError,
  classifyCheckpointTemporary,
} from "../harness/macos-classify-checkpoint-temporary.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CLASSIFIER = path.resolve(HERE, "..", "harness", "macos-classify-checkpoint-temporary.mjs");
const CASE_ID = "00000000-0000-0000-0000-000000000001";
const JOB_ID = "00000000-0000-0000-0000-000000000002";
const OUTCOME_ID = "00000000-0000-0000-0000-000000000003";
const ATTACHMENT_ID = "00000000-0000-0000-0000-000000000004";
const STAGING_ID = "00000000-0000-0000-0000-000000000005";
const AUDIT_ARTIFACT_ID = "00000000-0000-0000-0000-000000000006";
const AUDIT_STAGING_ID = "00000000-0000-0000-0000-000000000007";
const PROPOSAL_KEY = "result";
const ATTACHMENT_SHA256 = "a".repeat(64);
const PROPOSAL_SHA256 = "b".repeat(64);

function canonical(value) {
  const normalize = (item) => {
    if (Array.isArray(item)) return item.map(normalize);
    if (item && typeof item === "object") return Object.fromEntries(Object.keys(item).sort().map((key) => [key, normalize(item[key])]));
    return item;
  };
  return `${JSON.stringify(normalize(value))}\n`;
}

function writeCanonical(filePath, value) {
  fs.writeFileSync(filePath, canonical(value), { encoding: "utf8", mode: 0o600 });
}

function proposalDirectoryName(key) {
  return `p-${crypto.createHash("sha256").update(key, "utf8").digest("hex")}`;
}

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "test-flow-checkpoint-temporary-"));
  for (const directory of [
    "jobs",
    "resources",
    "tmp/uploads",
    "tmp/proposals",
    "tmp/workspaces",
    "tmp/quarantine",
    "tmp/state",
  ]) fs.mkdirSync(path.join(root, directory), { recursive: true, mode: 0o700 });
  writeCanonical(path.join(root, "data-format.json"), { schema_version: 1 });
  const proposalMarker = {
    staging_id: STAGING_ID,
    owner_job_id: JOB_ID,
    proposal_key: PROPOSAL_KEY,
    resource_kind: "FILE",
    size: 7,
    sha256: PROPOSAL_SHA256,
    tree_manifest: null,
  };
  const state = {
    cases: {
      [CASE_ID]: {
        jobs: { [JOB_ID]: { job_id: JOB_ID, status: "SUCCEEDED" } },
        attachments: {
          [ATTACHMENT_ID]: {
            attachment_id: ATTACHMENT_ID,
            status: "READY",
            size: 11,
            sha256: ATTACHMENT_SHA256,
            storage_key: `resources/cases/${CASE_ID}/attachments/${ATTACHMENT_ID}/payload`,
          },
        },
        outcomes: {
          [OUTCOME_ID]: {
            outcome_id: OUTCOME_ID,
            job_id: JOB_ID,
            proposed_evidence: [],
            proposed_artifacts: [{ proposal_key: PROPOSAL_KEY, staged_resource_ref: proposalMarker }],
          },
        },
        outcome_processing_records: {
          [OUTCOME_ID]: { outcome_id: OUTCOME_ID, job_id: JOB_ID, disposition: "APPLIED", generated_artifact_ids: [AUDIT_ARTIFACT_ID] },
        },
        artifacts: {
          [AUDIT_ARTIFACT_ID]: {
            artifact_id: AUDIT_ARTIFACT_ID,
            kind: "AUDIT_BUNDLE",
            created_by_job_id: JOB_ID,
            resource_kind: "FILE",
            size: 13,
            sha256: "d".repeat(64),
            metadata: { source_job_id: JOB_ID, source_outcome_id: OUTCOME_ID },
          },
        },
      },
    },
  };
  writeCanonical(path.join(root, "state.json"), state);
  const job = path.join(root, "jobs", JOB_ID);
  fs.mkdirSync(job);
  writeCanonical(path.join(job, "job_outcome.json"), { outcome_id: OUTCOME_ID, job_id: JOB_ID });
  const upload = path.join(root, "tmp", "uploads", ATTACHMENT_ID);
  fs.mkdirSync(upload);
  writeCanonical(path.join(upload, "staged.json"), {
    attachment_id: ATTACHMENT_ID,
    resource_kind: "FILE",
    size: 11,
    sha256: ATTACHMENT_SHA256,
  });
  const proposal = path.join(root, "tmp", "proposals", JOB_ID, proposalDirectoryName(PROPOSAL_KEY));
  fs.mkdirSync(proposal, { recursive: true });
  writeCanonical(path.join(proposal, "staged.json"), proposalMarker);
  fs.writeFileSync(path.join(proposal, "payload"), "ignored\n", { mode: 0o600 });
  const auditProposal = path.join(root, "tmp", "proposals", JOB_ID, proposalDirectoryName("server-audit-bundle"));
  fs.mkdirSync(auditProposal, { recursive: true });
  writeCanonical(path.join(auditProposal, "staged.json"), {
    staging_id: AUDIT_STAGING_ID,
    owner_job_id: JOB_ID,
    proposal_key: "server-audit-bundle",
    resource_kind: "FILE",
    size: 13,
    sha256: "d".repeat(64),
    tree_manifest: null,
  });
  return { root, state, proposalMarker };
}

function expectCode(callback, code) {
  assert.throws(callback, (error) => error instanceof CheckpointTemporaryError && error.code === code);
}

test("checkpoint classifier accepts only completed uploads and processed proposal stages", () => {
  const value = fixture();
  try {
    const receipt = classifyCheckpointTemporary(value.root);
    assert.equal(receipt.status, "PASS");
    assert.equal(receipt.outbox_clear, true);
    assert.equal(receipt.excluded_completed_uploads, 1);
    assert.equal(receipt.excluded_processed_proposal_stages, 2);
  } finally {
    fs.rmSync(value.root, { recursive: true, force: true });
  }
});

test("checkpoint classifier rejects a finalized but unprocessed durable outbox", () => {
  const value = fixture();
  try {
    const unprocessedJob = "00000000-0000-0000-0000-000000000008";
    value.state.cases[CASE_ID].jobs[unprocessedJob] = { job_id: unprocessedJob, status: "SUCCEEDED" };
    writeCanonical(path.join(value.root, "state.json"), value.state);
    const job = path.join(value.root, "jobs", unprocessedJob);
    fs.mkdirSync(job);
    writeCanonical(path.join(job, "job_outcome.json"), { job_id: unprocessedJob });
    expectCode(() => classifyCheckpointTemporary(value.root), "CHECKPOINT_UNPROCESSED_OUTCOME");
  } finally {
    fs.rmSync(value.root, { recursive: true, force: true });
  }
});

test("checkpoint classifier rejects a completed upload that is not READY", () => {
  const value = fixture();
  try {
    value.state.cases[CASE_ID].attachments[ATTACHMENT_ID].status = "UPLOADING";
    writeCanonical(path.join(value.root, "state.json"), value.state);
    expectCode(() => classifyCheckpointTemporary(value.root), "CHECKPOINT_UPLOAD_STAGE_NOT_DISCARDABLE");
  } finally {
    fs.rmSync(value.root, { recursive: true, force: true });
  }
});

test("checkpoint classifier rejects unknown proposal staging and pending cleanup", () => {
  const value = fixture();
  try {
    const unknown = path.join(value.root, "tmp", "proposals", JOB_ID, `p-${"c".repeat(64)}`);
    fs.mkdirSync(unknown);
    writeCanonical(path.join(unknown, "staged.json"), value.proposalMarker);
    expectCode(() => classifyCheckpointTemporary(value.root), "CHECKPOINT_PROPOSAL_STAGE_NOT_DISCARDABLE");
    fs.rmSync(unknown, { recursive: true, force: true });
    fs.writeFileSync(path.join(value.root, "tmp", "quarantine", "pending"), "pending\n");
    expectCode(() => classifyCheckpointTemporary(value.root), "CHECKPOINT_QUARANTINE_NOT_EMPTY");
  } finally {
    fs.rmSync(value.root, { recursive: true, force: true });
  }
});

test("checkpoint classifier CLI writes a sanitized failure receipt", () => {
  const value = fixture();
  try {
    value.state.cases[CASE_ID].attachments[ATTACHMENT_ID].status = "UPLOADING";
    writeCanonical(path.join(value.root, "state.json"), value.state);
    const receipt = path.join(value.root, "classifier-receipt.json");
    const result = spawnSync(process.execPath, [CLASSIFIER, "--data-root", value.root, "--output", receipt], { encoding: "utf8" });
    assert.equal(result.status, 1);
    assert.deepEqual(JSON.parse(fs.readFileSync(receipt, "utf8")), {
      schema_version: 1,
      status: "FAIL",
      code: "CHECKPOINT_UPLOAD_STAGE_NOT_DISCARDABLE",
    });
    assert.equal(result.stdout, "");
    assert.equal(result.stderr, "");
  } finally {
    fs.rmSync(value.root, { recursive: true, force: true });
  }
});
