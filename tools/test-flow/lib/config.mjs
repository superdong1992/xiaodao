import fs from "node:fs";
import path from "node:path";
import { assertFlow, readJson } from "./util.mjs";

const ALLOWED_ACTIONS = new Set([
  "framework_self_test",
  "deterministic_affected",
  "deterministic_full",
  "host_capability",
  "server_linux_capability",
  "real_logparse",
  "real_agent_backend",
  "real_route",
  "real_diagnose",
  "real_review",
  "journey_environment",
  "journey_route",
  "journey_upload",
  "journey_diagnose",
  "journey_review_audit",
  "journey_publish_restart",
  "rollout_parity_once",
  "finalize",
]);

function requireObject(value, code, label) {
  assertFlow(value !== null && typeof value === "object" && !Array.isArray(value), code, `${label} must be an object`);
}

function validateFlow(flow) {
  requireObject(flow, "CONFIG_FLOW_OBJECT", "flow config");
  assertFlow(flow.schema_version === 1, "CONFIG_FLOW_VERSION", "Unsupported flow schema version");
  requireObject(flow.defaults, "CONFIG_DEFAULTS", "flow defaults");
  requireObject(flow.tracks, "CONFIG_TRACKS", "flow tracks");
  assertFlow(Array.isArray(flow.stages) && flow.stages.length > 0, "CONFIG_STAGES", "flow stages must be non-empty");

  const identifiers = new Set();
  for (const stage of flow.stages) {
    requireObject(stage, "CONFIG_STAGE_OBJECT", "stage");
    assertFlow(typeof stage.id === "string" && /^[a-z0-9.-]+$/.test(stage.id), "CONFIG_STAGE_ID", "Invalid stage id");
    assertFlow(!identifiers.has(stage.id), "CONFIG_STAGE_DUPLICATE", `Duplicate stage ${stage.id}`);
    identifiers.add(stage.id);
    assertFlow(Array.isArray(stage.depends_on), "CONFIG_STAGE_DEPENDENCIES", `${stage.id} dependencies must be an array`);
    assertFlow(ALLOWED_ACTIONS.has(stage.action), "CONFIG_STAGE_ACTION", `Untrusted action ${stage.action}`);
    assertFlow(Number.isInteger(stage.timeout_seconds) && stage.timeout_seconds > 0, "CONFIG_STAGE_TIMEOUT", `Invalid timeout for ${stage.id}`);
  }
  for (const stage of flow.stages) {
    for (const dependency of stage.depends_on) {
      assertFlow(identifiers.has(dependency), "CONFIG_STAGE_DEPENDENCY_UNKNOWN", `${stage.id} depends on unknown ${dependency}`);
      assertFlow(dependency !== stage.id, "CONFIG_STAGE_SELF_DEPENDENCY", `${stage.id} depends on itself`);
    }
  }
  topologicalStages(flow.stages, [...identifiers]);
}

function validateProofs(proofs, flow) {
  requireObject(proofs, "CONFIG_PROOFS_OBJECT", "proof config");
  assertFlow(proofs.schema_version === 1, "CONFIG_PROOFS_VERSION", "Unsupported proof schema version");
  requireObject(proofs.goals, "CONFIG_GOALS", "goals");
  requireObject(proofs.proofs, "CONFIG_PROOF_MAP", "proofs");
  const stageIds = new Set(flow.stages.map((stage) => stage.id));
  for (const [goalId, goal] of Object.entries(proofs.goals)) {
    assertFlow(/^[a-z0-9.-]+$/.test(goalId), "CONFIG_GOAL_ID", `Invalid goal ${goalId}`);
    assertFlow(Array.isArray(goal.tracks) && goal.tracks.length > 0, "CONFIG_GOAL_TRACKS", `${goalId} needs tracks`);
    assertFlow(Array.isArray(goal.proofs) && goal.proofs.length > 0, "CONFIG_GOAL_PROOFS", `${goalId} needs proofs`);
    for (const proofId of goal.proofs) {
      assertFlow(Object.hasOwn(proofs.proofs, proofId), "CONFIG_GOAL_PROOF_UNKNOWN", `${goalId} references unknown ${proofId}`);
    }
  }
  for (const [proofId, proof] of Object.entries(proofs.proofs)) {
    if (proof.stages) {
      assertFlow(Array.isArray(proof.stages), "CONFIG_PROOF_STAGES", `${proofId} stages must be an array`);
      for (const stage of proof.stages) {
        assertFlow(stageIds.has(stage), "CONFIG_PROOF_STAGE_UNKNOWN", `${proofId} references unknown ${stage}`);
      }
    }
    assertFlow(
      proof.stages || proof.selector || proof.action,
      "CONFIG_PROOF_EMPTY",
      `${proofId} must define stages, selector, or action`,
    );
  }
}

function validateIdentities(identities, flow) {
  requireObject(identities, "CONFIG_IDENTITIES_OBJECT", "identity config");
  assertFlow(identities.schema_version === 1, "CONFIG_IDENTITIES_VERSION", "Unsupported identity schema version");
  requireObject(identities.groups, "CONFIG_IDENTITY_GROUPS", "identity groups");
  const groupNames = new Set(Object.keys(identities.groups));
  for (const stage of flow.stages) {
    assertFlow(groupNames.has(stage.identity_group), "CONFIG_IDENTITY_GROUP_UNKNOWN", `${stage.id} has unknown identity group`);
  }
  assertFlow(Array.isArray(identities.change_rules), "CONFIG_CHANGE_RULES", "change_rules must be an array");
  for (const rule of identities.change_rules) {
    assertFlow(typeof rule.pattern === "string", "CONFIG_CHANGE_PATTERN", "change pattern must be a string");
    // Compile now so an invalid expression fails admission instead of mid-run.
    new RegExp(rule.pattern);
    assertFlow(Array.isArray(rule.groups), "CONFIG_CHANGE_GROUPS", "change rule groups must be an array");
    for (const group of rule.groups) {
      assertFlow(groupNames.has(group), "CONFIG_CHANGE_GROUP_UNKNOWN", `Unknown changed identity group ${group}`);
    }
  }
}

export function loadConfiguration(repoRoot, configRoot = path.join(repoRoot, "tools", "test-flow", "config")) {
  const files = {
    flow: path.join(configRoot, "flow.v1.json"),
    proofs: path.join(configRoot, "proofs.v1.json"),
    identities: path.join(configRoot, "identities.v1.json"),
    gates: path.join(configRoot, "gates.v1.json"),
  };
  for (const [label, filePath] of Object.entries(files)) {
    assertFlow(fs.existsSync(filePath), "CONFIG_FILE_MISSING", `Missing ${label} config: ${filePath}`);
  }
  const config = {
    flow: readJson(files.flow),
    proofs: readJson(files.proofs),
    identities: readJson(files.identities),
    gates: readJson(files.gates),
    files,
  };
  validateFlow(config.flow);
  validateProofs(config.proofs, config.flow);
  validateIdentities(config.identities, config.flow);
  requireObject(config.gates, "CONFIG_GATES_OBJECT", "gate config");
  assertFlow(config.gates.schema_version === 1, "CONFIG_GATES_VERSION", "Unsupported gate schema version");
  requireObject(config.gates.gates, "CONFIG_GATES", "gates");
  for (const [gateId, gate] of Object.entries(config.gates.gates)) {
    assertFlow(/^[a-z0-9.-]+$/.test(gateId), "CONFIG_GATE_ID", `Invalid gate id ${gateId}`);
    assertFlow(gate.kind === "pytest", "CONFIG_GATE_KIND", `Unsupported gate kind for ${gateId}`);
    assertFlow(typeof gate.selector === "string" && !path.isAbsolute(gate.selector), "CONFIG_GATE_SELECTOR", `Invalid gate selector for ${gateId}`);
    assertFlow(Array.isArray(gate.dependencies), "CONFIG_GATE_DEPENDENCIES", `${gateId} dependencies must be an array`);
  }
  return config;
}

export function topologicalStages(stages, selectedIds) {
  const byId = new Map(stages.map((stage) => [stage.id, stage]));
  const selected = new Set(selectedIds);
  const visiting = new Set();
  const visited = new Set();
  const ordered = [];
  function visit(identifier) {
    if (visited.has(identifier)) return;
    assertFlow(!visiting.has(identifier), "CONFIG_STAGE_CYCLE", `Stage cycle at ${identifier}`);
    const stage = byId.get(identifier);
    assertFlow(stage, "CONFIG_STAGE_UNKNOWN", `Unknown stage ${identifier}`);
    visiting.add(identifier);
    for (const dependency of stage.depends_on) {
      selected.add(dependency);
      visit(dependency);
    }
    visiting.delete(identifier);
    visited.add(identifier);
    ordered.push(stage);
  }
  for (const identifier of [...selected]) visit(identifier);
  return ordered;
}

export function stagesForGoal(config, { goalId, track, requestedStage, changedGroups, reusableStages = new Set() }) {
  const goal = config.proofs.goals[goalId];
  assertFlow(goal, "GOAL_UNKNOWN", `Unknown proof goal ${goalId}`);
  assertFlow(goal.tracks.includes(track), "GOAL_TRACK_MISMATCH", `${goalId} is not valid for ${track}`);
  const selected = new Set();
  for (const proofId of goal.proofs) {
    const proof = config.proofs.proofs[proofId];
    for (const stage of proof.stages ?? []) selected.add(stage);
    if (proof.selector === "requested-real-stage") {
      assertFlow(requestedStage, "REAL_STAGE_REQUIRED", `${goalId} requires --stage`);
      assertFlow(
        config.flow.stages.some((stage) => stage.id === requestedStage && ["isolated-real", "real-journey"].includes(stage.kind)),
        "REAL_STAGE_INVALID",
        `${requestedStage} is not a real-model stage`,
      );
      selected.add(requestedStage);
    }
    if (proof.selector === "identity-changed") {
      for (const candidate of proof.candidates) {
        const stage = config.flow.stages.find((entry) => entry.id === candidate);
        if (!reusableStages.has(candidate)) {
          selected.add(candidate);
        }
      }
    }
  }
  return topologicalStages(config.flow.stages, [...selected]);
}

export function changedIdentityGroups(identityConfig, changedFiles) {
  const groups = new Set();
  for (const file of changedFiles) {
    for (const rule of identityConfig.change_rules) {
      if (new RegExp(rule.pattern).test(file)) {
        for (const group of rule.groups) groups.add(group);
      }
    }
  }
  return groups;
}
