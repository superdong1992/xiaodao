import fs from "node:fs";
import path from "node:path";
import { readJson } from "./util.mjs";
import { requiredEventFiles, scanPayload, validateEvidenceStreams, verifyVerdict } from "./evidence.mjs";

export function loadHistory(evidenceRoot) {
  if (!fs.existsSync(evidenceRoot)) return [];
  const history = [];
  for (const entry of fs.readdirSync(evidenceRoot, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const attemptRoot = path.join(evidenceRoot, entry.name);
    try {
      const verification = verifyVerdict(attemptRoot);
      if (verification.status !== "PASS") continue;
      history.push({ attempt_root: attemptRoot, verdict: verification.verdict });
    } catch {
      // Corrupt or partially written attempts are deliberately invisible to reuse.
    }
  }
  history.sort((left, right) => String(left.verdict.committed_at_utc).localeCompare(String(right.verdict.committed_at_utc)));
  return history;
}

export function lastSuccessfulDevCommit(history) {
  for (const entry of [...history].reverse()) {
    const verdict = entry.verdict;
    if (verdict.track === "dev" && ["PASS", "PASS_WITH_WARNINGS"].includes(verdict.overall) && verdict.source?.head) {
      return verdict.source.head;
    }
  }
  return null;
}

export function findReusableStages(history, desiredStageIdentities, { track, freshStageIds = new Set(), knownSecrets = [] }) {
  const reusable = new Map();
  const audits = new Map();
  for (const [stageId, desired] of Object.entries(desiredStageIdentities)) {
    if (freshStageIds.has(stageId)) continue;
    for (const entry of [...history].reverse()) {
      const stage = (entry.verdict.stages ?? []).find((candidate) => candidate.id === stageId);
      if (!stage || stage.status !== "PASS") continue;
      if (entry.verdict.evidence_reusable !== true) continue;
      if (stage.producer_identity !== desired.producer_identity || stage.proof_identity !== desired.proof_identity) continue;
      if (track === "release" && stage.kind === "real-journey") continue;
      let audit = audits.get(entry.attempt_root);
      if (!audit) {
        try {
          const scan = scanPayload(path.join(entry.attempt_root, "payload"), { knownSecrets });
          const streams = validateEvidenceStreams(entry.attempt_root, { requiredFiles: requiredEventFiles(entry.verdict.stages) });
          audit = { status: scan.status === "PASS" && streams.status === "PASS" ? "PASS" : "FAIL", scan_digest: scan.scanned_root_digest };
        } catch {
          audit = { status: "FAIL", scan_digest: null };
        }
        audits.set(entry.attempt_root, audit);
      }
      if (audit.status !== "PASS") continue;
      reusable.set(stageId, {
        attempt_root: entry.attempt_root,
        run_id: entry.verdict.run_id,
        stage,
        committed_at_utc: entry.verdict.committed_at_utc,
        current_reaudit: audit,
      });
      break;
    }
  }
  return reusable;
}

export function failureFingerprint({ stageId, identity, failureDomain, code }) {
  return JSON.stringify({
    stage_id: stageId,
    producer_identity: identity?.producer_identity ?? null,
    proof_identity: identity?.proof_identity ?? null,
    failure_domain: failureDomain ?? null,
    code: code ?? null,
  });
}

export function repeatedFailureAdvice(history, fingerprint) {
  for (const entry of [...history].reverse()) {
    const previous = entry.verdict.failure_fingerprint;
    if (!previous) continue;
    if (previous === fingerprint) {
      return {
        recommendation: "STOP",
        reason: "SAME_FAILURE_WITH_UNCHANGED_IDENTITY",
        previous_run_id: entry.verdict.run_id,
      };
    }
    break;
  }
  return { recommendation: "RUN", reason: null, previous_run_id: null };
}

export function performanceSamples(history, stageId, performanceIdentity) {
  const samples = [];
  for (const entry of history) {
    const stage = (entry.verdict.stages ?? []).find((candidate) => candidate.id === stageId);
    if (
      stage?.status === "PASS"
      && stage.performance_identity === performanceIdentity
      && Number.isFinite(stage.elapsed_seconds)
    ) {
      samples.push(stage.elapsed_seconds);
    }
  }
  return samples.slice(-10);
}

export function loadRunPlan(attemptRoot) {
  const planPath = path.join(attemptRoot, "payload", "run-plan.json");
  return fs.existsSync(planPath) ? readJson(planPath) : null;
}
