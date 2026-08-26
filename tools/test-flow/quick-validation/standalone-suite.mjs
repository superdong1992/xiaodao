import fs from "node:fs";
import path from "node:path";

export const STANDALONE_LINUX_MARKER = "TEST_FLOW_QUICK_UBUNTU2204_CONTAINER";
export const STANDALONE_LINUX_IMAGE_SEAL = "/run/secrets/image-seal.json";

const CONTRACT_FAILURE_SUFFIXES = Object.freeze([
  // Completed business-workflow failures: the harness stayed healthy, so the
  // suite can seal this scenario and collect the remaining matrix.
  "CLIENT_INCOMPLETE",
  "MCP_BUSINESS_ERROR",
  "STATE_NOT_TERMINAL",
  "FINAL_CASE_INVALID",
  "SERVER_JOB_LIFECYCLE_INVALID",
  "USER_RESULT_CARDINALITY_INVALID",
  "ARTIFACT_INDEX_EMPTY",
  "ARTIFACT_INDEX_MISMATCH",
  "ATTACHMENT_CARDINALITY_INVALID",
  "ATTACHMENT_DECLARATION_MISMATCH",
  "ATTACHMENT_NOT_SUBMITTED",
  "CREATE_CASE_CARDINALITY_INVALID",
  "PREPARE_CARDINALITY_INVALID",
  "RECOVERY_CARDINALITY_INVALID",
  "RECOVERY_ERROR_INVALID",
  "RECOVERY_LEDGER_INVALID",
  "RECOVERY_PROJECTION_MISMATCH",
  "RECOVERY_REPEATED",
  "RECOVERY_REQUEST_ID_INVALID",
  "UPLOAD_COMMAND_INVALID",
  "GROUNDING_AUDIT_MISMATCH",
  "SERVICE_DRAFT_MISSING",
  "SERVICE_DRAFT_INVALID",
  "SERVICE_DRAFT_REJECTED",
  "METHODS_DRAFT_MISSING",
  "METHODS_DRAFT_INVALID",
  // Gate-only oracle failures.
  "PUBLIC_STATUS_MISMATCH",
  "DIAGNOSIS_STATUS_MISMATCH",
  "DIAGNOSIS_SHAPE_INVALID",
  "BRANCH_MARKER_MISSING",
  "REQUIRED_MARKER_MISSING",
  "EVIDENCE_IDENTITY_MISSING",
  "EVIDENCE_IDENTITY_INVALID",
  "EXPECTED_EVIDENCE_IDENTITY_MISMATCH",
  "EXPECTED_EVIDENCE_IDENTITY_MERGED",
  "EXPECTED_TERM_MISSING",
  "FORBIDDEN_TERM_PRESENT",
]);

export function standalonePlatform({
  platform = process.platform,
  architecture = process.arch,
  environment = process.env,
  imageSeal = undefined,
  osRelease = undefined,
  imageSealPath = STANDALONE_LINUX_IMAGE_SEAL,
  osReleasePath = "/etc/os-release",
} = {}) {
  if (platform === "darwin" && architecture === "arm64") {
    return { status: "SUPPORTED", topology: "native-darwin-arm64", platform, architecture, sealed: false, image_seal: null };
  }
  if (platform === "linux" && architecture === "x64") {
    if (environment[STANDALONE_LINUX_MARKER] !== "1") {
      return { status: "UNSUPPORTED", topology: null, platform, architecture, sealed: false, code: "LINUX_MARKER_MISSING", image_seal: null };
    }
    let parsedSeal = imageSeal;
    let parsedOsRelease = osRelease;
    try {
      if (parsedSeal === undefined) parsedSeal = JSON.parse(fs.readFileSync(imageSealPath, "utf8"));
      if (parsedOsRelease === undefined) {
        parsedOsRelease = Object.fromEntries(fs.readFileSync(osReleasePath, "utf8")
          .split(/\r?\n/u)
          .map((line) => line.match(/^([A-Z_]+)=(?:"([^"]*)"|(.*))$/u))
          .filter(Boolean)
          .map((match) => [match[1], match[2] ?? match[3]]));
      }
    } catch {
      return { status: "UNSUPPORTED", topology: null, platform, architecture, sealed: false, code: "LINUX_IMAGE_SEAL_UNREADABLE", image_seal: null };
    }
    const sealValid = parsedSeal !== null
      && typeof parsedSeal === "object"
      && !Array.isArray(parsedSeal)
      && parsedSeal.schema_version === 1
      && parsedSeal.platform === "linux/amd64"
      && parsedSeal.profile === "ubuntu22.04-central-v1"
      && parsedSeal.status === "PASS"
      && /^sha256:[0-9a-f]{64}$/u.test(parsedSeal.image_id);
    if (!sealValid) {
      return { status: "UNSUPPORTED", topology: null, platform, architecture, sealed: false, code: "LINUX_IMAGE_SEAL_INVALID", image_seal: null };
    }
    if (parsedOsRelease?.ID !== "ubuntu" || parsedOsRelease?.VERSION_ID !== "22.04") {
      return { status: "UNSUPPORTED", topology: null, platform, architecture, sealed: false, code: "LINUX_OS_RELEASE_MISMATCH", image_seal: null };
    }
    return {
      status: "SUPPORTED",
      topology: "sealed-ubuntu2204-linux-x64",
      platform,
      architecture,
      sealed: true,
      image_seal: {
        schema_version: parsedSeal.schema_version,
        image_id: parsedSeal.image_id,
        platform: parsedSeal.platform,
        profile: parsedSeal.profile,
        status: parsedSeal.status,
      },
    };
  }
  return { status: "UNSUPPORTED", topology: null, platform, architecture, sealed: false, code: "PLATFORM_UNSUPPORTED", image_seal: null };
}

export function expectedSuiteCalls(scenarios, callCount) {
  if (!Array.isArray(scenarios) || typeof callCount !== "function") throw new TypeError("Suite scenarios and call counter are required");
  return scenarios.reduce((sum, scenario) => sum + callCount(scenario), 0);
}

export function failureDomain(failure) {
  const code = typeof failure?.code === "string" ? failure.code : "STANDALONE_SUITE_UNKNOWN_FAILURE";
  return CONTRACT_FAILURE_SUFFIXES.some((suffix) => code.endsWith(suffix)) ? "CONTRACT" : "ENGINEERING";
}

export function scenarioDecision(verdict) {
  if (verdict?.status === "PASS") return { failure_domain: null, stop: false };
  const domain = verdict?.failure_domain ?? failureDomain(verdict?.failure);
  return { failure_domain: domain, stop: domain === "ENGINEERING" };
}

export function aggregateUsage(values) {
  const aggregate = {};
  for (const value of values) {
    if (value === null || typeof value !== "object" || Array.isArray(value)) continue;
    for (const [key, amount] of Object.entries(value)) {
      if (typeof amount === "number" && Number.isFinite(amount)) aggregate[key] = (aggregate[key] ?? 0) + amount;
    }
  }
  for (const [key, amount] of Object.entries(aggregate)) {
    if (!Number.isInteger(amount)) aggregate[key] = Math.round(amount * 1_000_000) / 1_000_000;
  }
  return aggregate;
}

export function standaloneScenarioRoots({ runRoot, runId, scratchRoot = null }) {
  if (typeof runRoot !== "string" || runRoot.length === 0 || typeof runId !== "string" || runId.length === 0) {
    throw new TypeError("Suite run root and run ID are required");
  }
  const persistedRunRoot = path.resolve(runRoot);
  const scratchRunRoot = scratchRoot === null ? persistedRunRoot : path.join(path.resolve(scratchRoot), runId);
  return {
    scratch_run_root: scratchRunRoot,
    work_root: path.join(scratchRunRoot, "work"),
    private_root: path.join(scratchRunRoot, "private"),
    evidence_root: path.join(persistedRunRoot, "evidence"),
    usage_root: path.join(persistedRunRoot, "usage"),
  };
}

export function scenarioVerdictReference({ suiteRoot, scenario, verdict, sha256File, modelField = "model_calls" }) {
  if (!["model_calls", "model_processes"].includes(modelField)) throw new TypeError("Suite model count field is invalid");
  const relative = path.posix.join("scenarios", scenario, "verdict.json");
  const absolute = path.join(suiteRoot, "scenarios", scenario, "verdict.json");
  return {
    scenario_id: scenario,
    status: verdict.status,
    failure_domain: verdict.failure_domain ?? null,
    [modelField]: verdict.model_calls ?? verdict.model_processes ?? null,
    usage: verdict.usage ?? null,
    failure: verdict.failure ?? null,
    verdict: { path: relative, sha256: sha256File(absolute) },
  };
}

export function suiteStatus({ blocked = false, engineeringFailure = null, references, expectedCount }) {
  if (blocked) return "BLOCKED";
  if (engineeringFailure !== null) return "ERROR";
  if (references.length !== expectedCount) return "ERROR";
  return references.every((item) => item.status === "PASS") ? "PASS" : "FAIL";
}
