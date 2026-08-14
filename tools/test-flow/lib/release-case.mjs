import fs from "node:fs";
import path from "node:path";

import {
  assertFlow,
  canonicalJson,
  readJson,
  sha256Bytes,
  sha256File,
} from "./util.mjs";


const CASE_FIELDS = [
  "allowed_actions",
  "approved_skill_dir",
  "case_id",
  "clarifications",
  "generation_spec",
  "input_wiki",
  "journey_scenario",
  "scenarios",
  "schema_version",
  "semantic_oracle",
];
const DIAGNOSIS_SKILL_MANIFEST_ID = /^[a-z][a-z0-9-]{1,63}$/;
const SCENARIO_FIELDS = ["driver", "oracle", "scenario_id"];
const DRIVER_FIELDS = [
  "attachment_anchor_names",
  "attachment_files",
  "initial_user_fact_names",
  "initial_user_fact_values",
  "problem",
  "scenario_id",
  "supplement_input_names",
  "supplement_input_values",
];
const PROBLEM_FIELDS = [
  "actual_behavior",
  "completion_criteria",
  "constraints",
  "expected_behavior",
  "goals",
  "non_goals",
  "raw_problem_text",
  "scope",
  "statement",
];

export function diagnosisSkillRuntimeRefId(manifestId) {
  assertFlow(
    typeof manifestId === "string" && DIAGNOSIS_SKILL_MANIFEST_ID.test(manifestId),
    "RELEASE_CASE_SKILL_ID",
    "Release case diagnosis skill manifest id is invalid",
  );
  return `diagnosis-skill/${manifestId}`;
}
const SCENARIO_ORACLE_FIELDS = [
  "candidate_factor_ids",
  "case_status",
  "causal_factor_ids",
  "criterion_statuses",
  "excluded_factor_ids",
  "required_rule_results",
  "required_safety_phrases",
  "resolution_status",
  "terminal_path_id",
];
const SEMANTIC_ORACLE_FIELDS = [
  "author_note_markers_forbidden_in_product",
  "business_canaries",
  "expected_skill",
  "generated_spec_oracle",
  "oracle_visibility",
  "schema_version",
];
const EXPECTED_SKILL_FIELDS = [
  "capability",
  "deployment_scope",
  "id",
  "observation_policy_kinds",
  "requirement_names",
  "requires_cross_clock_tolerance_ms",
  "requires_multiline_event",
  "requires_numeric_compare",
  "terminal_paths",
  "version",
];
const GENERATED_SPEC_ORACLE_FIELDS = [
  "event_policy_bindings",
  "observation_policies",
  "projection_version",
  "required_product_semantics",
];
const OBSERVATION_POLICY_FIELDS = [
  "boundary",
  "id",
  "key_fields",
  "kind",
  "max_observed",
  "scope",
  "window_ms",
];
const EVENT_POLICY_BINDING_FIELDS = ["event_id", "observation_policy_ids"];
const REQUIRED_PRODUCT_SEMANTIC_FIELDS = [
  "all_of_any_patterns",
  "id",
  "target_fields",
];
const PRODUCT_SEMANTIC_TARGET_FIELDS = new Set([
  "analysis_steps",
  "assumptions",
  "chinese_title",
  "judgement_rules",
  "output_requirements",
  "problem_scope",
  "summary",
  "time_characteristics",
]);
const MANIFEST_FIELDS = ["files", "owner_spec", "root", "schema_version"];
const MANIFEST_FILE_FIELDS = ["path", "purpose", "schema_ref", "sha256", "size"];
const ALLOWED_ACTIONS = new Set(["skill_generation", "specialized_diagnosis"]);

function exactKeys(value, expected, code, label) {
  const actual = value && typeof value === "object" && !Array.isArray(value)
    ? Object.keys(value).sort()
    : [];
  assertFlow(
    canonicalJson(actual) === canonicalJson([...expected].sort()),
    code,
    `${label} fields are invalid`,
    { actual, expected: [...expected].sort() },
  );
}

function safeRelative(value, code = "RELEASE_CASE_PATH_INVALID") {
  assertFlow(
    typeof value === "string"
      && value.length > 0
      && !value.includes("\\")
      && !path.posix.isAbsolute(value)
      && value.split("/").every((part) => part && part !== "." && part !== ".."),
    code,
    `Release case path is unsafe: ${String(value)}`,
  );
  return value;
}

function scalarArray(value, code, label, { nonempty = false } = {}) {
  assertFlow(
    Array.isArray(value)
      && (!nonempty || value.length > 0)
      && value.every((item) => ["string", "number", "boolean"].includes(typeof item)),
    code,
    `${label} must be a${nonempty ? " non-empty" : ""} scalar array`,
  );
  return value;
}

function pathIdentity(absolute) {
  const resolved = fs.realpathSync.native(absolute);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function assertExclusiveRolePaths(entries) {
  const owners = new Map();
  for (const [role, absolute] of entries) {
    const identity = pathIdentity(absolute);
    assertFlow(
      !owners.has(identity),
      "RELEASE_CASE_ROLE_ALIAS",
      `Release case file roles must be mutually exclusive: ${owners.get(identity)} and ${role}`,
    );
    owners.set(identity, role);
  }
  return new Set(owners.keys());
}

function rolePathEntries(loaded) {
  return [
    ["input_wiki", loaded.wiki_path],
    ["clarifications", loaded.clarifications_path],
    ["generation_spec", loaded.generation_spec_path],
    ...loaded.approved_skill_files.map((absolute, index) => [`approved_skill_file[${index}]`, absolute]),
    ["semantic_oracle", loaded.semantic_oracle_path],
    ...loaded.scenarios.flatMap((scenario) => [
      [`scenario[${scenario.scenario_id}].driver`, scenario.driver_path],
      [`scenario[${scenario.scenario_id}].oracle`, scenario.oracle_path],
    ]),
  ];
}

function loadDriver(scenario, reservedRolePaths) {
  const driver = readJson(scenario.driver_path);
  exactKeys(driver, DRIVER_FIELDS, "RELEASE_CASE_DRIVER_FIELDS", "Release case driver");
  exactKeys(driver.problem, PROBLEM_FIELDS, "RELEASE_CASE_PROBLEM_FIELDS", "Release case problem");
  assertFlow(driver.scenario_id === scenario.scenario_id, "RELEASE_CASE_DRIVER_ID", "Release case driver scenario_id is inconsistent");
  for (const field of PROBLEM_FIELDS.filter((name) => !["completion_criteria", "constraints", "goals", "non_goals"].includes(name))) {
    assertFlow(typeof driver.problem[field] === "string" && driver.problem[field].trim(), "RELEASE_CASE_PROBLEM_VALUE", `Release case problem ${field} is empty`);
  }
  for (const field of ["completion_criteria", "constraints", "goals", "non_goals"]) {
    scalarArray(driver.problem[field], "RELEASE_CASE_PROBLEM_ARRAY", `Release case problem ${field}`, { nonempty: field !== "non_goals" });
    assertFlow(driver.problem[field].every((item) => typeof item === "string" && item.length > 0), "RELEASE_CASE_PROBLEM_ARRAY_VALUE", `Release case problem ${field} contains an invalid value`);
  }
  for (const [namesField, valuesField] of [
    ["initial_user_fact_names", "initial_user_fact_values"],
    ["supplement_input_names", "supplement_input_values"],
  ]) {
    const names = scalarArray(driver[namesField], "RELEASE_CASE_INPUT_NAMES", `Release case ${namesField}`);
    const values = scalarArray(driver[valuesField], "RELEASE_CASE_INPUT_VALUES", `Release case ${valuesField}`);
    assertFlow(names.length === values.length, "RELEASE_CASE_INPUT_LENGTH", `${namesField} and ${valuesField} must have equal lengths`);
    assertFlow(names.every((item) => typeof item === "string" && item.length > 0), "RELEASE_CASE_INPUT_NAME", `${namesField} contains an invalid name`);
  }
  scalarArray(driver.attachment_files, "RELEASE_CASE_ATTACHMENTS", "Release case attachment_files");
  scalarArray(driver.attachment_anchor_names, "RELEASE_CASE_ATTACHMENT_ANCHORS", "Release case attachment_anchor_names");
  assertFlow(driver.attachment_anchor_names.length === driver.attachment_files.length, "RELEASE_CASE_ATTACHMENT_LENGTH", "attachment_anchor_names and attachment_files must have equal lengths");
  assertFlow(driver.attachment_anchor_names.every((item) => typeof item === "string" && /^[a-z][a-z0-9_]*$/.test(item)), "RELEASE_CASE_ATTACHMENT_ANCHOR", "Release case attachment anchor is invalid");
  for (const attachment of driver.attachment_files) {
    safeRelative(attachment, "RELEASE_CASE_ATTACHMENT_PATH");
    const attachmentPath = resolveOwnedPath(path.dirname(scenario.driver_path), attachment);
    assertFlow(
      !reservedRolePaths.has(pathIdentity(attachmentPath)),
      "RELEASE_CASE_ATTACHMENT_ROLE_ALIAS",
      `Release case attachment aliases an input or oracle role: ${attachment}`,
    );
  }
  return driver;
}

function loadScenarioOracle(scenario) {
  const oracle = readJson(scenario.oracle_path);
  exactKeys(oracle, SCENARIO_ORACLE_FIELDS, "RELEASE_CASE_ORACLE_FIELDS", "Release case scenario oracle");
  for (const field of ["candidate_factor_ids", "causal_factor_ids", "criterion_statuses", "excluded_factor_ids", "required_safety_phrases"]) {
    scalarArray(oracle[field], "RELEASE_CASE_ORACLE_ARRAY", `Release case oracle ${field}`);
  }
  assertFlow(oracle.required_rule_results && typeof oracle.required_rule_results === "object" && !Array.isArray(oracle.required_rule_results), "RELEASE_CASE_ORACLE_RULES", "Release case oracle required_rule_results must be an object");
  assertFlow(Object.entries(oracle.required_rule_results).every(([key, value]) => key && ["PASS", "FAIL", "UNKNOWN"].includes(value)), "RELEASE_CASE_ORACLE_RULE_RESULT", "Release case oracle rule result is invalid");
  for (const field of ["case_status", "resolution_status", "terminal_path_id"]) {
    assertFlow(typeof oracle[field] === "string" && oracle[field].length > 0, "RELEASE_CASE_ORACLE_VALUE", `Release case oracle ${field} is empty`);
  }
  return oracle;
}

function loadSemanticOracle(loaded) {
  const oracle = readJson(loaded.semantic_oracle_path);
  exactKeys(oracle, SEMANTIC_ORACLE_FIELDS, "RELEASE_CASE_SEMANTIC_ORACLE_FIELDS", "Release case semantic oracle");
  exactKeys(oracle.expected_skill, EXPECTED_SKILL_FIELDS, "RELEASE_CASE_EXPECTED_SKILL_FIELDS", "Release case expected skill");
  assertFlow(oracle.schema_version === 1 && oracle.oracle_visibility === "GATE_ONLY", "RELEASE_CASE_SEMANTIC_ORACLE_VERSION", "Release case semantic oracle metadata is invalid");
  scalarArray(oracle.author_note_markers_forbidden_in_product, "RELEASE_CASE_NOTE_MARKERS", "Release case forbidden note markers");
  scalarArray(oracle.business_canaries, "RELEASE_CASE_CANARIES", "Release case business canaries");
  exactKeys(oracle.generated_spec_oracle, GENERATED_SPEC_ORACLE_FIELDS, "RELEASE_CASE_GENERATED_ORACLE_FIELDS", "Release case generated spec oracle");
  assertFlow(oracle.generated_spec_oracle.projection_version === 4, "RELEASE_CASE_GENERATED_ORACLE_VERSION", "Release case generated spec oracle version is invalid");
  const policies = oracle.generated_spec_oracle.observation_policies;
  assertFlow(Array.isArray(policies), "RELEASE_CASE_OBSERVATION_POLICIES", "Release case observation policies must be an array");
  const policyIds = new Set();
  for (const policy of policies) {
    exactKeys(policy, OBSERVATION_POLICY_FIELDS, "RELEASE_CASE_OBSERVATION_POLICY_FIELDS", "Release case observation policy");
    assertFlow(typeof policy.id === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(policy.id) && !policyIds.has(policy.id), "RELEASE_CASE_OBSERVATION_POLICY_ID", "Release case observation policy id is invalid or duplicated");
    policyIds.add(policy.id);
    assertFlow(["SUPPRESSION", "RATE_LIMIT"].includes(policy.kind), "RELEASE_CASE_OBSERVATION_POLICY_KIND", "Release case observation policy kind is invalid");
    assertFlow(typeof policy.scope === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(policy.scope), "RELEASE_CASE_OBSERVATION_POLICY_SCOPE", "Release case observation policy scope is invalid");
    scalarArray(policy.key_fields, "RELEASE_CASE_OBSERVATION_POLICY_KEYS", "Release case observation policy key_fields");
    assertFlow(policy.key_fields.every((value) => typeof value === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(value)) && new Set(policy.key_fields).size === policy.key_fields.length, "RELEASE_CASE_OBSERVATION_POLICY_KEYS", "Release case observation policy key_fields are invalid");
    assertFlow(Number.isInteger(policy.window_ms) && policy.window_ms > 0 && policy.window_ms <= 604800000, "RELEASE_CASE_OBSERVATION_POLICY_WINDOW", "Release case observation policy window is invalid");
    assertFlow(["CLOSED_OPEN", "CLOSED_CLOSED"].includes(policy.boundary), "RELEASE_CASE_OBSERVATION_POLICY_BOUNDARY", "Release case observation policy boundary is invalid");
    assertFlow(
      policy.kind === "SUPPRESSION"
        ? policy.max_observed === null
        : Number.isInteger(policy.max_observed) && policy.max_observed > 0 && policy.max_observed <= 1000000,
      "RELEASE_CASE_OBSERVATION_POLICY_MAX",
      "Release case observation policy max_observed is invalid",
    );
  }
  assertFlow(
    canonicalJson([...new Set(policies.map((policy) => policy.kind))].sort())
      === canonicalJson([...new Set(oracle.expected_skill.observation_policy_kinds)].sort()),
    "RELEASE_CASE_OBSERVATION_POLICY_KINDS",
    "Release case expected policy kinds disagree with the executable policy projection",
  );
  const bindings = oracle.generated_spec_oracle.event_policy_bindings;
  assertFlow(Array.isArray(bindings), "RELEASE_CASE_EVENT_POLICY_BINDINGS", "Release case event policy bindings must be an array");
  const eventIds = new Set();
  for (const binding of bindings) {
    exactKeys(binding, EVENT_POLICY_BINDING_FIELDS, "RELEASE_CASE_EVENT_POLICY_BINDING_FIELDS", "Release case event policy binding");
    assertFlow(typeof binding.event_id === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(binding.event_id) && !eventIds.has(binding.event_id), "RELEASE_CASE_EVENT_POLICY_BINDING_ID", "Release case event policy binding id is invalid or duplicated");
    eventIds.add(binding.event_id);
    scalarArray(binding.observation_policy_ids, "RELEASE_CASE_EVENT_POLICY_BINDING_POLICIES", "Release case event policy binding observation_policy_ids");
    assertFlow(binding.observation_policy_ids.every((value) => typeof value === "string" && policyIds.has(value)) && new Set(binding.observation_policy_ids).size === binding.observation_policy_ids.length, "RELEASE_CASE_EVENT_POLICY_BINDING_POLICIES", "Release case event policy binding references an invalid or duplicated policy");
  }
  const semantics = oracle.generated_spec_oracle.required_product_semantics;
  assertFlow(Array.isArray(semantics) && semantics.length > 0, "RELEASE_CASE_REQUIRED_PRODUCT_SEMANTICS", "Release case required product semantics must be non-empty");
  const semanticIds = new Set();
  for (const semantic of semantics) {
    exactKeys(semantic, REQUIRED_PRODUCT_SEMANTIC_FIELDS, "RELEASE_CASE_REQUIRED_PRODUCT_SEMANTIC_FIELDS", "Release case required product semantic");
    assertFlow(typeof semantic.id === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(semantic.id) && !semanticIds.has(semantic.id), "RELEASE_CASE_REQUIRED_PRODUCT_SEMANTIC_ID", "Release case required product semantic id is invalid or duplicated");
    semanticIds.add(semantic.id);
    scalarArray(semantic.target_fields, "RELEASE_CASE_REQUIRED_PRODUCT_SEMANTIC_TARGET_FIELDS", "Release case required product semantic target_fields", { nonempty: true });
    assertFlow(
      semantic.target_fields.every((field) => PRODUCT_SEMANTIC_TARGET_FIELDS.has(field))
        && new Set(semantic.target_fields).size === semantic.target_fields.length,
      "RELEASE_CASE_REQUIRED_PRODUCT_SEMANTIC_TARGET_FIELDS",
      "Release case required product semantic target_fields are invalid or duplicated",
    );
    assertFlow(Array.isArray(semantic.all_of_any_patterns) && semantic.all_of_any_patterns.length > 0 && semantic.all_of_any_patterns.every((group) => Array.isArray(group) && group.length > 0 && group.every((pattern) => typeof pattern === "string" && pattern.length > 0)), "RELEASE_CASE_REQUIRED_PRODUCT_SEMANTIC_PATTERNS", "Release case required product semantic patterns are invalid");
  }
  return oracle;
}

export function compareReleaseCaseEntries(left, right) {
  return left.name < right.name ? -1 : left.name > right.name ? 1 : 0;
}

function ordinaryFiles(root) {
  const records = [];
  const visit = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true }).sort(compareReleaseCaseEntries)) {
      const absolute = path.join(directory, entry.name);
      const relative = path.relative(root, absolute).split(path.sep).join("/");
      if (entry.isSymbolicLink()) {
        assertFlow(false, "RELEASE_CASE_LINK_FORBIDDEN", `Release case links are forbidden: ${relative}`);
      }
      if (entry.isDirectory()) visit(absolute);
      else if (entry.isFile() && relative !== "fixture-manifest.json") {
        const stat = fs.statSync(absolute);
        assertFlow(stat.nlink === 1, "RELEASE_CASE_HARDLINK_FORBIDDEN", `Release case hard links are forbidden: ${relative}`);
        records.push({ path: relative, absolute, size: stat.size, sha256: sha256File(absolute) });
      } else if (!entry.isFile()) {
        assertFlow(false, "RELEASE_CASE_NODE_UNSUPPORTED", `Unsupported release case node: ${relative}`);
      }
    }
  };
  visit(root);
  return records.sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
}

function resolveOwnedPath(root, relative, { directory = false } = {}) {
  safeRelative(relative);
  const absolute = path.resolve(root, ...relative.split("/"));
  const prefix = `${path.resolve(root)}${path.sep}`;
  assertFlow(absolute.startsWith(prefix), "RELEASE_CASE_PATH_ESCAPE", `Release case path escapes its root: ${relative}`);
  const stat = fs.lstatSync(absolute);
  assertFlow(!stat.isSymbolicLink(), "RELEASE_CASE_LINK_FORBIDDEN", `Release case path is a link: ${relative}`);
  assertFlow(
    directory ? stat.isDirectory() : stat.isFile(),
    "RELEASE_CASE_PATH_KIND",
    `Release case path has the wrong kind: ${relative}`,
  );
  return absolute;
}

export function verifyReleaseCaseManifest(caseRoot) {
  const root = path.resolve(caseRoot);
  const manifestPath = path.join(root, "fixture-manifest.json");
  const manifest = readJson(manifestPath);
  exactKeys(manifest, MANIFEST_FIELDS, "RELEASE_CASE_MANIFEST_FIELDS", "Release case manifest");
  assertFlow(manifest.schema_version === 1, "RELEASE_CASE_MANIFEST_VERSION", "Release case manifest schema_version must be 1");
  assertFlow(manifest.owner_spec === "WIKI_DIAGNOSIS_GENERALIZATION", "RELEASE_CASE_MANIFEST_OWNER", "Release case manifest owner is invalid");
  safeRelative(manifest.root, "RELEASE_CASE_MANIFEST_ROOT");
  assertFlow(Array.isArray(manifest.files), "RELEASE_CASE_MANIFEST_FILES", "Release case manifest files must be an array");
  const actual = ordinaryFiles(root);
  const declared = manifest.files.map((entry) => {
    exactKeys(entry, MANIFEST_FILE_FIELDS, "RELEASE_CASE_MANIFEST_ENTRY_FIELDS", "Release case manifest entry");
    safeRelative(entry.path);
    assertFlow(typeof entry.purpose === "string" && entry.purpose.trim(), "RELEASE_CASE_MANIFEST_PURPOSE", `Release case purpose is empty: ${entry.path}`);
    assertFlow(entry.schema_ref === null, "RELEASE_CASE_MANIFEST_SCHEMA_REF", `Release case-local files do not accept external schema refs: ${entry.path}`);
    assertFlow(Number.isInteger(entry.size) && entry.size >= 0, "RELEASE_CASE_MANIFEST_SIZE", `Release case size is invalid: ${entry.path}`);
    assertFlow(typeof entry.sha256 === "string" && /^[0-9a-f]{64}$/.test(entry.sha256), "RELEASE_CASE_MANIFEST_SHA", `Release case digest is invalid: ${entry.path}`);
    return entry;
  });
  assertFlow(
    canonicalJson(declared.map((entry) => entry.path)) === canonicalJson(actual.map((entry) => entry.path)),
    "RELEASE_CASE_MANIFEST_COVERAGE",
    "Release case manifest does not cover the exact owned file set",
  );
  for (let index = 0; index < actual.length; index += 1) {
    assertFlow(declared[index].size === actual[index].size, "RELEASE_CASE_MANIFEST_SIZE_DRIFT", `Release case size drift: ${actual[index].path}`);
    assertFlow(declared[index].sha256 === actual[index].sha256, "RELEASE_CASE_MANIFEST_HASH_DRIFT", `Release case hash drift: ${actual[index].path}`);
  }
  return { root, manifest, records: actual };
}

export function loadReleaseCase(caseRoot) {
  const verified = verifyReleaseCaseManifest(caseRoot);
  const descriptor = readJson(resolveOwnedPath(verified.root, "case.json"));
  exactKeys(descriptor, CASE_FIELDS, "RELEASE_CASE_FIELDS", "Release case descriptor");
  assertFlow(descriptor.schema_version === 1, "RELEASE_CASE_VERSION", "Release case schema_version must be 1");
  assertFlow(typeof descriptor.case_id === "string" && /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(descriptor.case_id), "RELEASE_CASE_ID", "Release case_id is invalid");
  assertFlow(
    Array.isArray(descriptor.allowed_actions)
      && descriptor.allowed_actions.length > 0
      && new Set(descriptor.allowed_actions).size === descriptor.allowed_actions.length
      && descriptor.allowed_actions.every((value) => ALLOWED_ACTIONS.has(value)),
    "RELEASE_CASE_ACTIONS",
    "Release case actions are invalid",
  );
  assertFlow(Array.isArray(descriptor.scenarios) && descriptor.scenarios.length > 0, "RELEASE_CASE_SCENARIOS", "Release case requires scenarios");
  const scenarios = descriptor.scenarios.map((scenario) => {
    exactKeys(scenario, SCENARIO_FIELDS, "RELEASE_CASE_SCENARIO_FIELDS", "Release case scenario");
    assertFlow(typeof scenario.scenario_id === "string" && /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(scenario.scenario_id), "RELEASE_CASE_SCENARIO_ID", "Release scenario_id is invalid");
    return {
      scenario_id: scenario.scenario_id,
      driver_path: resolveOwnedPath(verified.root, scenario.driver),
      oracle_path: resolveOwnedPath(verified.root, scenario.oracle),
    };
  });
  assertFlow(new Set(scenarios.map((item) => item.scenario_id)).size === scenarios.length, "RELEASE_CASE_SCENARIO_DUPLICATE", "Release scenario IDs must be unique");
  assertFlow(scenarios.some((item) => item.scenario_id === descriptor.journey_scenario), "RELEASE_CASE_JOURNEY_SCENARIO", "Release journey_scenario must name a declared scenario");
  const approvedSkillDir = resolveOwnedPath(verified.root, descriptor.approved_skill_dir, { directory: true });
  const approvedPrefix = `${path.relative(verified.root, approvedSkillDir).split(path.sep).join("/")}/`;
  const approvedSkillFiles = verified.records
    .filter((record) => record.path.startsWith(approvedPrefix))
    .map((record) => resolveOwnedPath(verified.root, record.path));
  const loaded = {
    case_id: descriptor.case_id,
    allowed_actions: [...descriptor.allowed_actions],
    journey_scenario: descriptor.journey_scenario,
    wiki_path: resolveOwnedPath(verified.root, descriptor.input_wiki),
    clarifications_path: resolveOwnedPath(verified.root, descriptor.clarifications),
    generation_spec_path: resolveOwnedPath(verified.root, descriptor.generation_spec),
    approved_skill_dir: approvedSkillDir,
    approved_skill_files: approvedSkillFiles,
    semantic_oracle_path: resolveOwnedPath(verified.root, descriptor.semantic_oracle),
    scenarios,
    manifest: verified.manifest,
  };
  const reservedRolePaths = assertExclusiveRolePaths(rolePathEntries(loaded));
  for (const scenario of scenarios) loadDriver(scenario, reservedRolePaths);
  return loaded;
}

export function loadReleaseCaseInputs(caseRoot) {
  const loaded = loadReleaseCase(caseRoot);
  const reservedRolePaths = new Set(rolePathEntries(loaded).map(([, absolute]) => pathIdentity(absolute)));
  return {
    case_id: loaded.case_id,
    allowed_actions: loaded.allowed_actions,
    journey_scenario: loaded.journey_scenario,
    wiki: fs.readFileSync(loaded.wiki_path, "utf8"),
    clarifications: fs.readFileSync(loaded.clarifications_path, "utf8"),
    generation_spec: readJson(loaded.generation_spec_path),
    approved_skill_dir: loaded.approved_skill_dir,
    scenarios: loaded.scenarios.map((scenario) => {
      const driver = loadDriver(scenario, reservedRolePaths);
      return {
        scenario_id: scenario.scenario_id,
        driver,
        attachment_paths: driver.attachment_files.map((relative) => resolveOwnedPath(path.dirname(scenario.driver_path), relative)),
      };
    }),
  };
}

export function loadReleaseCaseOracle(caseRoot) {
  const loaded = loadReleaseCase(caseRoot);
  return {
    case_id: loaded.case_id,
    semantic_oracle: loadSemanticOracle(loaded),
    scenarios: loaded.scenarios.map((scenario) => ({
      scenario_id: scenario.scenario_id,
      oracle: loadScenarioOracle(scenario),
    })),
  };
}

export function releaseCaseDigests(caseRoot) {
  const loaded = loadReleaseCase(caseRoot);
  const oraclePaths = new Set([
    path.relative(path.resolve(caseRoot), loaded.semantic_oracle_path).split(path.sep).join("/"),
    ...loaded.scenarios.map((scenario) => path.relative(path.resolve(caseRoot), scenario.oracle_path).split(path.sep).join("/")),
  ]);
  const records = verifyReleaseCaseManifest(caseRoot).records.map(({ path: relative, size, sha256 }) => ({ path: relative, size, sha256 }));
  const input = records.filter((record) => !oraclePaths.has(record.path));
  const oracle = records.filter((record) => oraclePaths.has(record.path));
  return {
    input_digest: sha256Bytes(canonicalJson(input)),
    oracle_digest: sha256Bytes(canonicalJson(oracle)),
    all_digest: sha256Bytes(canonicalJson(records)),
    input_records: input,
    oracle_records: oracle,
  };
}

export function discoverReleaseCaseRoot(releaseRoot) {
  const root = path.resolve(releaseRoot);
  const candidates = fs.readdirSync(root, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && fs.existsSync(path.join(root, entry.name, "case.json")))
    .map((entry) => path.join(root, entry.name))
    .sort();
  assertFlow(candidates.length === 1, "RELEASE_CASE_SELECTION", "Exactly one reviewed Release case must be selected by the repository snapshot");
  return candidates[0];
}

export function releaseCasePartition(caseRoot, partition) {
  const loaded = loadReleaseCase(caseRoot);
  const root = path.resolve(caseRoot);
  const relative = (absolute) => path.relative(root, absolute).split(path.sep).join("/");
  const records = verifyReleaseCaseManifest(caseRoot).records.map(({ path: recordPath, size, sha256 }) => ({ path: recordPath, size, sha256 }));
  const semanticOracle = relative(loaded.semantic_oracle_path);
  const scenarioOracles = new Set(loaded.scenarios.map((scenario) => relative(scenario.oracle_path)));
  const approvedPrefix = `${relative(loaded.approved_skill_dir)}/`;
  const wikiPaths = new Set([relative(loaded.wiki_path), relative(loaded.clarifications_path)]);
  const generationSpec = relative(loaded.generation_spec_path);
  let selected;
  if (partition === "wiki") selected = records.filter((record) => wikiPaths.has(record.path));
  else if (partition === "approved") selected = records.filter((record) => record.path === generationSpec || record.path.startsWith(approvedPrefix));
  else if (partition === "oracle") selected = records.filter((record) => record.path === semanticOracle || scenarioOracles.has(record.path));
  else if (partition === "journey") selected = records.filter((record) => record.path !== semanticOracle && !scenarioOracles.has(record.path));
  else assertFlow(false, "RELEASE_CASE_PARTITION", `Unknown Release case partition: ${String(partition)}`);
  return {
    case_id: loaded.case_id,
    partition,
    digest: sha256Bytes(canonicalJson(selected)),
    records: selected,
  };
}
