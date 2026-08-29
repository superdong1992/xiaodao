import fs from "node:fs";
import path from "node:path";

import { assertFlow, canonicalJson, readJson, sha256Bytes, sha256File } from "./util.mjs";

const CASE_FIELDS = ["allowed_actions", "case_id", "input_wiki", "journey_scenario", "registration_template", "scenarios", "schema_version", "semantic_oracle"];
const SCENARIO_FIELDS = ["driver", "oracle", "scenario_id"];
const DRIVER_FIELDS = ["attachment_anchor_names", "attachment_files", "initial_user_fact_names", "initial_user_fact_values", "problem", "scenario_id", "supplement_input_names", "supplement_input_values"];
const PROBLEM_FIELDS = ["actual_behavior", "completion_criteria", "constraints", "expected_behavior", "goals", "non_goals", "raw_problem_text", "scope", "statement"];
const REGISTRATION_FIELDS = ["capability", "deployment_scope", "package", "registration_id", "runtime", "schema_version", "summary", "version"];
const PACKAGE_FIELDS = ["relative_path", "skill_name", "source_wiki_sha256"];
const RUNTIME_FIELDS = ["diagnose", "preprocessing", "review"];
const ROLE_BINDING_FIELDS = ["agent_profile_id", "context_policy_id", "output_contract_id", "tool_bundle_id"];
const PREPROCESSING_FIELDS = ["logparse_plan", "logparse_product", "requires_logparse", "roles"];
const ROLE_FIELDS = ["description", "label", "presence", "source_reference"];
const PLAN_FIELDS = ["anchors", "attachment_requirement", "problem_time_binding"];
const ANCHOR_FIELDS = ["label", "module", "pid", "process_name", "slot"];
const SEMANTIC_ORACLE_FIELDS = ["author_note_markers_forbidden_in_product", "business_canaries", "expected_package", "oracle_visibility", "schema_version"];
const EXPECTED_PACKAGE_FIELDS = ["forbidden_paths", "method_marker_sets", "required_artifacts", "required_log_derived_fields", "required_shared_markers", "required_user_inputs", "skill_name", "source_wiki_sha256"];
const METHOD_MARKER_SET_FIELDS = ["all_markers", "semantic_id"];
const SCENARIO_ORACLE_FIELDS = ["expected_method_verdicts", "expected_status", "forbidden_evidence_terms", "oracle_visibility", "required_evidence_identities", "required_request_timeout", "scenario_id", "schema_version"];
const METHOD_VERDICT_FIELDS = ["semantic_id", "verdict"];
const EVIDENCE_IDENTITY_FIELDS = ["identity_tokens", "marker", "semantic_id"];
const REQUEST_TIMEOUT_FIELDS = ["decoy_api", "decoy_request_id", "decoy_service", "decoy_timeout_ms", "marker", "request_id", "timeout_ms", "unlinked_marker", "unlinked_timeout_ms"];
const MANIFEST_FIELDS = ["files", "owner_spec", "root", "schema_version"];
const MANIFEST_FILE_FIELDS = ["path", "purpose", "schema_ref", "sha256", "size"];
const ALLOWED_ACTIONS = new Set(["methods_skill_generation", "specialized_diagnosis"]);
const RESOLVED_VERDICTS = new Set(["CONFIRMED", "REJECTED"]);
const REGISTRATION_ID = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const USER_FACT = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/;
const SHA256 = /^[0-9a-f]{64}$/;
const SEMVER = /^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:[-+][0-9A-Za-z.-]+)?$/;
const DIAGNOSE_BINDING = { agent_profile_id: "agent-profile/specialist", tool_bundle_id: "tool-bundle/diagnose", context_policy_id: "context-policy/diagnose", output_contract_id: "output-contract/diagnose" };
const REVIEW_BINDING = { agent_profile_id: "agent-profile/reviewer", tool_bundle_id: "tool-bundle/review", context_policy_id: "context-policy/review", output_contract_id: "output-contract/review" };

function exactKeys(value, expected, code, label) {
  const actual = value && typeof value === "object" && !Array.isArray(value) ? Object.keys(value).sort() : [];
  assertFlow(canonicalJson(actual) === canonicalJson([...expected].sort()), code, `${label} fields are invalid`, { actual, expected: [...expected].sort() });
}

function safeRelative(value, code = "RELEASE_CASE_PATH_INVALID") {
  assertFlow(typeof value === "string" && value.length > 0 && !value.includes("\\") && !path.posix.isAbsolute(value) && value.split("/").every((part) => part && part !== "." && part !== ".."), code, `Release case path is unsafe: ${String(value)}`);
  return value;
}

function stringArray(value, code, label, { nonempty = false, pattern = null, unique = true } = {}) {
  assertFlow(Array.isArray(value) && (!nonempty || value.length > 0) && value.every((item) => typeof item === "string" && item.length > 0 && (!pattern || pattern.test(item))) && (!unique || value.length === new Set(value).size), code, `${label} must contain valid strings`);
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
    assertFlow(!owners.has(identity), "RELEASE_CASE_ROLE_ALIAS", `Release case file roles must be mutually exclusive: ${owners.get(identity)} and ${role}`);
    owners.set(identity, role);
  }
  return new Set(owners.keys());
}

export function diagnosisSkillRuntimeRefId(registrationId) {
  assertFlow(typeof registrationId === "string" && REGISTRATION_ID.test(registrationId), "RELEASE_CASE_REGISTRATION_ID", "Release case Methods registration id is invalid");
  return `diagnosis-skill/${registrationId}`;
}

export const methodsSkillRuntimeRefId = diagnosisSkillRuntimeRefId;

export function compareReleaseCaseEntries(left, right) {
  return left.name < right.name ? -1 : left.name > right.name ? 1 : 0;
}

function ordinaryFiles(root) {
  const records = [];
  const visit = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true }).sort(compareReleaseCaseEntries)) {
      const absolute = path.join(directory, entry.name);
      const relative = path.relative(root, absolute).split(path.sep).join("/");
      if (entry.isSymbolicLink()) assertFlow(false, "RELEASE_CASE_LINK_FORBIDDEN", `Release case links are forbidden: ${relative}`);
      if (entry.isDirectory()) visit(absolute);
      else if (entry.isFile() && relative !== "fixture-manifest.json") {
        const metadata = fs.statSync(absolute);
        const maximumLinks = process.platform === "win32" ? 2 : 1;
        assertFlow(metadata.nlink <= maximumLinks, "RELEASE_CASE_HARDLINK_FORBIDDEN", `Release case hard links are forbidden: ${relative}`);
        records.push({ path: relative, absolute, size: metadata.size, sha256: sha256File(absolute) });
      } else if (!entry.isFile()) assertFlow(false, "RELEASE_CASE_NODE_UNSUPPORTED", `Unsupported release case node: ${relative}`);
    }
  };
  visit(root);
  return records.sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
}

function resolveOwnedPath(root, relative, { directory = false } = {}) {
  safeRelative(relative);
  const absolute = path.resolve(root, ...relative.split("/"));
  assertFlow(absolute.startsWith(`${path.resolve(root)}${path.sep}`), "RELEASE_CASE_PATH_ESCAPE", `Release case path escapes its root: ${relative}`);
  const metadata = fs.lstatSync(absolute);
  assertFlow(!metadata.isSymbolicLink(), "RELEASE_CASE_LINK_FORBIDDEN", `Release case path is a link: ${relative}`);
  assertFlow(directory ? metadata.isDirectory() : metadata.isFile(), "RELEASE_CASE_PATH_KIND", `Release case path has the wrong kind: ${relative}`);
  return absolute;
}

export function verifyReleaseCaseManifest(caseRoot) {
  const root = path.resolve(caseRoot);
  const manifest = readJson(path.join(root, "fixture-manifest.json"));
  exactKeys(manifest, MANIFEST_FIELDS, "RELEASE_CASE_MANIFEST_FIELDS", "Release case manifest");
  assertFlow(manifest.schema_version === 2, "RELEASE_CASE_MANIFEST_VERSION", "Release case manifest schema_version must be 2");
  assertFlow(manifest.owner_spec === "METHODS_SKILL_RELEASE_CASE", "RELEASE_CASE_MANIFEST_OWNER", "Release case manifest owner is invalid");
  safeRelative(manifest.root, "RELEASE_CASE_MANIFEST_ROOT");
  assertFlow(Array.isArray(manifest.files), "RELEASE_CASE_MANIFEST_FILES", "Release case manifest files must be an array");
  const actual = ordinaryFiles(root);
  const declared = manifest.files.map((entry) => {
    exactKeys(entry, MANIFEST_FILE_FIELDS, "RELEASE_CASE_MANIFEST_ENTRY_FIELDS", "Release case manifest entry");
    safeRelative(entry.path);
    assertFlow(typeof entry.purpose === "string" && entry.purpose.trim(), "RELEASE_CASE_MANIFEST_PURPOSE", `Release case purpose is empty: ${entry.path}`);
    assertFlow(entry.schema_ref === null, "RELEASE_CASE_MANIFEST_SCHEMA_REF", `Release case-local files do not accept external schema refs: ${entry.path}`);
    assertFlow(Number.isInteger(entry.size) && entry.size >= 0, "RELEASE_CASE_MANIFEST_SIZE", `Release case size is invalid: ${entry.path}`);
    assertFlow(typeof entry.sha256 === "string" && SHA256.test(entry.sha256), "RELEASE_CASE_MANIFEST_SHA", `Release case digest is invalid: ${entry.path}`);
    return entry;
  });
  assertFlow(canonicalJson(declared.map((entry) => entry.path)) === canonicalJson(actual.map((entry) => entry.path)), "RELEASE_CASE_MANIFEST_COVERAGE", "Release case manifest does not cover the exact owned file set");
  actual.forEach((record, index) => {
    assertFlow(declared[index].size === record.size, "RELEASE_CASE_MANIFEST_SIZE_DRIFT", `Release case size drift: ${record.path}`);
    assertFlow(declared[index].sha256 === record.sha256, "RELEASE_CASE_MANIFEST_HASH_DRIFT", `Release case hash drift: ${record.path}`);
  });
  return { root, manifest, records: actual };
}

function validateBinding(value, label, { nullable = false } = {}) {
  if (nullable && value === null) return;
  assertFlow(value && typeof value === "object" && !Array.isArray(value), "RELEASE_CASE_REGISTRATION_BINDING", `${label} must be a binding object`);
  const fields = Object.keys(value).sort().join(",");
  const validUserFact = fields === "name,source" && value.source === "USER_FACT" && typeof value.name === "string" && USER_FACT.test(value.name);
  const validFixed = fields === "source,value" && value.source === "SKILL_FIXED" && typeof value.value === "string" && value.value.length > 0;
  assertFlow(validUserFact || validFixed, "RELEASE_CASE_REGISTRATION_BINDING", `${label} has an invalid binding`);
}

function loadRegistrationTemplate(loaded) {
  const value = readJson(loaded.registration_template_path);
  exactKeys(value, REGISTRATION_FIELDS, "RELEASE_CASE_REGISTRATION_FIELDS", "Release registration template");
  assertFlow(value.schema_version === 1, "RELEASE_CASE_REGISTRATION_VERSION", "Release registration schema_version must be 1");
  assertFlow(typeof value.registration_id === "string" && REGISTRATION_ID.test(value.registration_id), "RELEASE_CASE_REGISTRATION_ID", "Release registration_id is invalid");
  assertFlow(path.basename(path.dirname(loaded.registration_template_path)) === value.registration_id, "RELEASE_CASE_REGISTRATION_DIRECTORY", "Release registration_id must match its directory");
  assertFlow(typeof value.version === "string" && SEMVER.test(value.version), "RELEASE_CASE_REGISTRATION_SEMVER", "Release registration version is invalid");
  assertFlow(typeof value.capability === "string" && value.capability.length > 0 && !value.capability.includes("\n"), "RELEASE_CASE_REGISTRATION_CAPABILITY", "Release registration capability is invalid");
  assertFlow(typeof value.summary === "string" && value.summary.trim(), "RELEASE_CASE_REGISTRATION_SUMMARY", "Release registration summary is invalid");
  assertFlow(["PRODUCTION", "TEST_ONLY"].includes(value.deployment_scope), "RELEASE_CASE_REGISTRATION_SCOPE", "Release registration deployment_scope is invalid");
  exactKeys(value.package, PACKAGE_FIELDS, "RELEASE_CASE_REGISTRATION_PACKAGE_FIELDS", "Release registration package");
  assertFlow(typeof value.package.skill_name === "string" && REGISTRATION_ID.test(value.package.skill_name), "RELEASE_CASE_REGISTRATION_SKILL_NAME", "Release registration skill_name is invalid");
  assertFlow(value.package.relative_path === `package/${value.package.skill_name}`, "RELEASE_CASE_REGISTRATION_PACKAGE_PATH", "Release registration package path is invalid");
  assertFlow(typeof value.package.source_wiki_sha256 === "string" && SHA256.test(value.package.source_wiki_sha256), "RELEASE_CASE_REGISTRATION_WIKI_SHA", "Release registration Wiki digest is invalid");
  assertFlow(value.package.source_wiki_sha256 === sha256File(loaded.wiki_path), "RELEASE_CASE_REGISTRATION_WIKI_DRIFT", "Release registration Wiki digest differs from the case Wiki");
  exactKeys(value.runtime, RUNTIME_FIELDS, "RELEASE_CASE_REGISTRATION_RUNTIME_FIELDS", "Release registration runtime");
  for (const [role, expected] of [["diagnose", DIAGNOSE_BINDING], ["review", REVIEW_BINDING]]) {
    exactKeys(value.runtime[role], ROLE_BINDING_FIELDS, "RELEASE_CASE_REGISTRATION_ROLE_BINDING_FIELDS", `Release registration ${role} binding`);
    assertFlow(canonicalJson(value.runtime[role]) === canonicalJson(expected), "RELEASE_CASE_REGISTRATION_ROLE_BINDING", `Release registration ${role} binding must use product built-ins`);
  }
  const preprocessing = value.runtime.preprocessing;
  exactKeys(preprocessing, PREPROCESSING_FIELDS, "RELEASE_CASE_REGISTRATION_PREPROCESSING_FIELDS", "Release registration preprocessing");
  assertFlow(preprocessing.requires_logparse === true, "RELEASE_CASE_REGISTRATION_LOGPARSE", "Release registration must require Logparse");
  assertFlow(typeof preprocessing.logparse_product === "string" && preprocessing.logparse_product && preprocessing.logparse_product !== "default", "RELEASE_CASE_REGISTRATION_LOGPARSE_PRODUCT", "Release registration Logparse product is invalid");
  assertFlow(Array.isArray(preprocessing.roles) && preprocessing.roles.length > 0, "RELEASE_CASE_REGISTRATION_ROLES", "Release registration roles are invalid");
  const roleLabels = preprocessing.roles.map((role) => {
    exactKeys(role, ROLE_FIELDS, "RELEASE_CASE_REGISTRATION_ROLE_FIELDS", "Release registration role");
    assertFlow(typeof role.label === "string" && USER_FACT.test(role.label), "RELEASE_CASE_REGISTRATION_ROLE_LABEL", "Release registration role label is invalid");
    assertFlow(typeof role.description === "string" && role.description.trim() && typeof role.source_reference === "string" && role.source_reference.trim(), "RELEASE_CASE_REGISTRATION_ROLE_METADATA", "Release registration role metadata is invalid");
    assertFlow(["REQUIRED", "OPTIONAL"].includes(role.presence), "RELEASE_CASE_REGISTRATION_ROLE_PRESENCE", "Release registration role presence is invalid");
    return role.label;
  });
  assertFlow(roleLabels.length === new Set(roleLabels).size && preprocessing.roles.some((role) => role.presence === "REQUIRED"), "RELEASE_CASE_REGISTRATION_ROLES", "Release registration roles must be unique and include REQUIRED");
  const plan = preprocessing.logparse_plan;
  exactKeys(plan, PLAN_FIELDS, "RELEASE_CASE_REGISTRATION_PLAN_FIELDS", "Release registration Logparse plan");
  assertFlow(plan.attachment_requirement === "log_archive", "RELEASE_CASE_REGISTRATION_ATTACHMENT", "Release registration attachment requirement must be log_archive");
  validateBinding(plan.problem_time_binding, "problem_time_binding");
  assertFlow(Array.isArray(plan.anchors) && plan.anchors.length === roleLabels.length, "RELEASE_CASE_REGISTRATION_ANCHORS", "Release registration anchors must match roles");
  const anchorLabels = plan.anchors.map((anchor, index) => {
    exactKeys(anchor, ANCHOR_FIELDS, "RELEASE_CASE_REGISTRATION_ANCHOR_FIELDS", "Release registration anchor");
    assertFlow(anchor.label === roleLabels[index], "RELEASE_CASE_REGISTRATION_ANCHOR_LABEL", "Release registration anchor order must match roles");
    validateBinding(anchor.module, `anchors[${index}].module`);
    validateBinding(anchor.slot, `anchors[${index}].slot`);
    validateBinding(anchor.process_name, `anchors[${index}].process_name`);
    validateBinding(anchor.pid, `anchors[${index}].pid`, { nullable: true });
    return anchor.label;
  });
  assertFlow(anchorLabels.length === new Set(anchorLabels).size, "RELEASE_CASE_REGISTRATION_ANCHORS", "Release registration anchor labels must be unique");
  return value;
}

function rolePathEntries(loaded) {
  return [["input_wiki", loaded.wiki_path], ["registration_template", loaded.registration_template_path], ["semantic_oracle", loaded.semantic_oracle_path], ...loaded.scenarios.flatMap((scenario) => [[`scenario[${scenario.scenario_id}].driver`, scenario.driver_path], [`scenario[${scenario.scenario_id}].oracle`, scenario.oracle_path]])];
}

function loadDriver(scenario, reservedRolePaths) {
  const driver = readJson(scenario.driver_path);
  exactKeys(driver, DRIVER_FIELDS, "RELEASE_CASE_DRIVER_FIELDS", "Release case driver");
  exactKeys(driver.problem, PROBLEM_FIELDS, "RELEASE_CASE_PROBLEM_FIELDS", "Release case problem");
  assertFlow(driver.scenario_id === scenario.scenario_id, "RELEASE_CASE_DRIVER_ID", "Release case driver scenario_id is inconsistent");
  for (const field of PROBLEM_FIELDS.filter((name) => !["completion_criteria", "constraints", "goals", "non_goals"].includes(name))) assertFlow(typeof driver.problem[field] === "string" && driver.problem[field].trim(), "RELEASE_CASE_PROBLEM_VALUE", `Release case problem ${field} is empty`);
  for (const field of ["completion_criteria", "constraints", "goals", "non_goals"]) stringArray(driver.problem[field], "RELEASE_CASE_PROBLEM_ARRAY", `Release case problem ${field}`, { nonempty: field !== "non_goals" });
  for (const [namesField, valuesField] of [["initial_user_fact_names", "initial_user_fact_values"], ["supplement_input_names", "supplement_input_values"]]) {
    const names = stringArray(driver[namesField], "RELEASE_CASE_INPUT_NAMES", `Release case ${namesField}`, { pattern: USER_FACT });
    const values = stringArray(driver[valuesField], "RELEASE_CASE_INPUT_VALUES", `Release case ${valuesField}`, { unique: false });
    assertFlow(names.length === values.length, "RELEASE_CASE_INPUT_LENGTH", `${namesField} and ${valuesField} must have equal lengths`);
  }
  stringArray(driver.attachment_files, "RELEASE_CASE_ATTACHMENTS", "Release case attachment_files");
  stringArray(driver.attachment_anchor_names, "RELEASE_CASE_ATTACHMENT_ANCHORS", "Release case attachment_anchor_names", { pattern: USER_FACT });
  assertFlow(driver.attachment_anchor_names.length === driver.attachment_files.length, "RELEASE_CASE_ATTACHMENT_LENGTH", "attachment anchors and files must have equal lengths");
  const attachmentPaths = driver.attachment_files.map((relative) => {
    safeRelative(relative, "RELEASE_CASE_ATTACHMENT_PATH");
    const absolute = resolveOwnedPath(path.dirname(scenario.driver_path), relative);
    assertFlow(!reservedRolePaths.has(pathIdentity(absolute)), "RELEASE_CASE_ATTACHMENT_ROLE_ALIAS", "Release case attachment aliases an input or oracle role");
    return absolute;
  });
  return { driver, attachment_paths: attachmentPaths };
}

export function loadReleaseCase(caseRoot) {
  const verified = verifyReleaseCaseManifest(caseRoot);
  const descriptor = readJson(resolveOwnedPath(verified.root, "case.json"));
  exactKeys(descriptor, CASE_FIELDS, "RELEASE_CASE_FIELDS", "Release case descriptor");
  assertFlow(descriptor.schema_version === 2, "RELEASE_CASE_VERSION", "Release case schema_version must be 2");
  assertFlow(typeof descriptor.case_id === "string" && REGISTRATION_ID.test(descriptor.case_id), "RELEASE_CASE_ID", "Release case_id is invalid");
  assertFlow(Array.isArray(descriptor.allowed_actions) && descriptor.allowed_actions.length > 0 && descriptor.allowed_actions.length === new Set(descriptor.allowed_actions).size && descriptor.allowed_actions.every((value) => ALLOWED_ACTIONS.has(value)), "RELEASE_CASE_ACTIONS", "Release case actions are invalid");
  assertFlow(Array.isArray(descriptor.scenarios) && descriptor.scenarios.length > 0, "RELEASE_CASE_SCENARIOS", "Release case requires scenarios");
  const scenarios = descriptor.scenarios.map((scenario) => {
    exactKeys(scenario, SCENARIO_FIELDS, "RELEASE_CASE_SCENARIO_FIELDS", "Release case scenario");
    assertFlow(typeof scenario.scenario_id === "string" && REGISTRATION_ID.test(scenario.scenario_id), "RELEASE_CASE_SCENARIO_ID", "Release scenario_id is invalid");
    return { scenario_id: scenario.scenario_id, driver_path: resolveOwnedPath(verified.root, scenario.driver), oracle_path: resolveOwnedPath(verified.root, scenario.oracle) };
  });
  assertFlow(scenarios.length === new Set(scenarios.map((item) => item.scenario_id)).size, "RELEASE_CASE_SCENARIO_DUPLICATE", "Release scenario IDs must be unique");
  assertFlow(scenarios.some((item) => item.scenario_id === descriptor.journey_scenario), "RELEASE_CASE_JOURNEY_SCENARIO", "Release journey_scenario must name a declared scenario");
  const loaded = { case_id: descriptor.case_id, allowed_actions: [...descriptor.allowed_actions], journey_scenario: descriptor.journey_scenario, wiki_path: resolveOwnedPath(verified.root, descriptor.input_wiki), registration_template_path: resolveOwnedPath(verified.root, descriptor.registration_template), semantic_oracle_path: resolveOwnedPath(verified.root, descriptor.semantic_oracle), scenarios, manifest: verified.manifest };
  const reservedRolePaths = assertExclusiveRolePaths(rolePathEntries(loaded));
  for (const scenario of scenarios) loadDriver(scenario, reservedRolePaths);
  loadRegistrationTemplate(loaded);
  return loaded;
}

export function loadReleaseCaseInputs(caseRoot) {
  const loaded = loadReleaseCase(caseRoot);
  const reservedRolePaths = new Set(rolePathEntries(loaded).map(([, absolute]) => pathIdentity(absolute)));
  const registrationTemplate = loadRegistrationTemplate(loaded);
  return {
    case_id: loaded.case_id,
    allowed_actions: loaded.allowed_actions,
    journey_scenario: loaded.journey_scenario,
    wiki: fs.readFileSync(loaded.wiki_path, "utf8"),
    wiki_path: loaded.wiki_path,
    registration_template: registrationTemplate,
    registration_template_path: loaded.registration_template_path,
    product_registration: { registration_id: registrationTemplate.registration_id, runtime_ref_id: diagnosisSkillRuntimeRefId(registrationTemplate.registration_id), version: registrationTemplate.version, skill_name: registrationTemplate.package.skill_name, source_wiki_sha256: registrationTemplate.package.source_wiki_sha256, logparse_product: registrationTemplate.runtime.preprocessing.logparse_product, attachment_requirement: registrationTemplate.runtime.preprocessing.logparse_plan.attachment_requirement },
    scenarios: loaded.scenarios.map((scenario) => { const { driver, attachment_paths } = loadDriver(scenario, reservedRolePaths); return { scenario_id: scenario.scenario_id, driver, attachment_paths }; }),
  };
}

function loadSemanticOracle(loaded) {
  const oracle = readJson(loaded.semantic_oracle_path);
  exactKeys(oracle, SEMANTIC_ORACLE_FIELDS, "RELEASE_CASE_SEMANTIC_ORACLE_FIELDS", "Release case semantic oracle");
  assertFlow(oracle.schema_version === 2 && oracle.oracle_visibility === "GATE_ONLY", "RELEASE_CASE_SEMANTIC_ORACLE_VERSION", "Release case semantic oracle metadata is invalid");
  stringArray(oracle.author_note_markers_forbidden_in_product, "RELEASE_CASE_NOTE_MARKERS", "Release case forbidden note markers");
  stringArray(oracle.business_canaries, "RELEASE_CASE_CANARIES", "Release case business canaries", { nonempty: true });
  const expected = oracle.expected_package;
  exactKeys(expected, EXPECTED_PACKAGE_FIELDS, "RELEASE_CASE_EXPECTED_PACKAGE_FIELDS", "Release case expected package");
  assertFlow(typeof expected.skill_name === "string" && REGISTRATION_ID.test(expected.skill_name), "RELEASE_CASE_EXPECTED_SKILL_NAME", "Release expected skill name is invalid");
  assertFlow(typeof expected.source_wiki_sha256 === "string" && SHA256.test(expected.source_wiki_sha256), "RELEASE_CASE_EXPECTED_WIKI_SHA", "Release expected Wiki digest is invalid");
  assertFlow(expected.source_wiki_sha256 === sha256File(loaded.wiki_path), "RELEASE_CASE_EXPECTED_WIKI_DRIFT", "Release expected Wiki digest differs from the case Wiki");
  stringArray(expected.required_user_inputs, "RELEASE_CASE_EXPECTED_INPUTS", "Release expected user inputs", { nonempty: true, pattern: USER_FACT });
  stringArray(expected.required_artifacts, "RELEASE_CASE_EXPECTED_ARTIFACTS", "Release expected artifacts", { nonempty: true, pattern: USER_FACT });
  stringArray(expected.required_log_derived_fields, "RELEASE_CASE_EXPECTED_LOG_FIELDS", "Release expected log-derived fields", { nonempty: true, pattern: USER_FACT });
  stringArray(expected.required_shared_markers, "RELEASE_CASE_EXPECTED_SHARED_MARKERS", "Release expected shared markers", { nonempty: true });
  stringArray(expected.forbidden_paths, "RELEASE_CASE_EXPECTED_FORBIDDEN_PATHS", "Release forbidden paths", { nonempty: true });
  assertFlow(Array.isArray(expected.method_marker_sets) && expected.method_marker_sets.length > 0, "RELEASE_CASE_EXPECTED_METHODS", "Release expected method marker sets are invalid");
  const semanticIds = [];
  for (const item of expected.method_marker_sets) {
    exactKeys(item, METHOD_MARKER_SET_FIELDS, "RELEASE_CASE_EXPECTED_METHOD_FIELDS", "Release expected method marker set");
    assertFlow(typeof item.semantic_id === "string" && USER_FACT.test(item.semantic_id), "RELEASE_CASE_EXPECTED_METHOD_ID", "Release expected semantic method id is invalid");
    stringArray(item.all_markers, "RELEASE_CASE_EXPECTED_METHOD_MARKERS", "Release expected method markers", { nonempty: true });
    semanticIds.push(item.semantic_id);
  }
  assertFlow(semanticIds.length === new Set(semanticIds).size, "RELEASE_CASE_EXPECTED_METHOD_ID", "Release expected semantic method ids must be unique");
  const wiki = fs.readFileSync(loaded.wiki_path, "utf8");
  for (const marker of [...expected.required_shared_markers, ...expected.method_marker_sets.flatMap((item) => item.all_markers)]) assertFlow(wiki.includes(marker), "RELEASE_CASE_ORACLE_MARKER_NOT_IN_WIKI", `Release oracle marker is absent from the Wiki: ${marker}`);
  const registration = loadRegistrationTemplate(loaded);
  assertFlow(registration.package.skill_name === expected.skill_name && registration.package.source_wiki_sha256 === expected.source_wiki_sha256, "RELEASE_CASE_ORACLE_REGISTRATION", "Release expected package differs from registration");
  return oracle;
}

function loadScenarioOracle(scenario, loaded) {
  const oracle = readJson(scenario.oracle_path);
  exactKeys(oracle, SCENARIO_ORACLE_FIELDS, "RELEASE_CASE_SCENARIO_ORACLE_FIELDS", "Release scenario oracle");
  assertFlow(oracle.schema_version === 2 && oracle.oracle_visibility === "GATE_ONLY", "RELEASE_CASE_SCENARIO_ORACLE_VERSION", "Release scenario oracle metadata is invalid");
  assertFlow(oracle.scenario_id === scenario.scenario_id, "RELEASE_CASE_SCENARIO_ORACLE_ID", "Release scenario oracle id is inconsistent");
  assertFlow(oracle.expected_status === "RESOLVED", "RELEASE_CASE_SCENARIO_STATUS", "Evidence V2 Release scenarios must expect RESOLVED");
  assertFlow(Array.isArray(oracle.expected_method_verdicts) && oracle.expected_method_verdicts.length > 0, "RELEASE_CASE_METHOD_VERDICTS", "Release method verdicts must be a non-empty array");
  const verdictSemanticIds = [];
  let confirmedMethods = 0;
  for (const item of oracle.expected_method_verdicts) {
    exactKeys(item, METHOD_VERDICT_FIELDS, "RELEASE_CASE_METHOD_VERDICT_FIELDS", "Release method verdict");
    assertFlow(typeof item.semantic_id === "string" && USER_FACT.test(item.semantic_id) && RESOLVED_VERDICTS.has(item.verdict), "RELEASE_CASE_METHOD_VERDICT_INVALID", "Release method verdict is invalid");
    verdictSemanticIds.push(item.semantic_id);
    if (item.verdict === "CONFIRMED") confirmedMethods += 1;
  }
  assertFlow(verdictSemanticIds.length === new Set(verdictSemanticIds).size, "RELEASE_CASE_METHOD_VERDICT_DUPLICATE", "Release method verdict semantic IDs must be unique");
  assertFlow(confirmedMethods > 0, "RELEASE_CASE_METHOD_VERDICTS", "Resolved Release scenarios must confirm at least one method");
  exactKeys(oracle.required_request_timeout, REQUEST_TIMEOUT_FIELDS, "RELEASE_CASE_REQUEST_TIMEOUT_FIELDS", "Release request timeout expectation");
  const timeout = oracle.required_request_timeout;
  assertFlow(
    typeof timeout.marker === "string" && timeout.marker.length > 0
      && typeof timeout.unlinked_marker === "string" && timeout.unlinked_marker.length > 0
      && timeout.marker !== timeout.unlinked_marker
      && typeof timeout.request_id === "string" && timeout.request_id.length > 0
      && Number.isSafeInteger(timeout.timeout_ms) && timeout.timeout_ms > 0
      && Number.isSafeInteger(timeout.unlinked_timeout_ms) && timeout.unlinked_timeout_ms > 0
      && timeout.unlinked_timeout_ms === timeout.timeout_ms
      && typeof timeout.decoy_service === "string" && timeout.decoy_service.length > 0
      && typeof timeout.decoy_api === "string" && timeout.decoy_api.length > 0
      && typeof timeout.decoy_request_id === "string" && timeout.decoy_request_id.length > 0
      && timeout.decoy_request_id !== timeout.request_id
      && Number.isSafeInteger(timeout.decoy_timeout_ms) && timeout.decoy_timeout_ms > 0,
    "RELEASE_CASE_REQUEST_TIMEOUT_INVALID",
    "Release request timeout expectation is invalid",
  );
  stringArray(oracle.forbidden_evidence_terms, "RELEASE_CASE_FORBIDDEN_EVIDENCE", "Release forbidden evidence terms");
  assertFlow(Array.isArray(oracle.required_evidence_identities), "RELEASE_CASE_EVIDENCE_IDENTITIES", "Release evidence identities must be an array");
  const { attachment_paths: attachmentPaths } = loadDriver(scenario, new Set(rolePathEntries(loaded).map(([, absolute]) => pathIdentity(absolute))));
  const attachmentLines = attachmentPaths.flatMap((absolute) => fs.readFileSync(absolute, "utf8")
    .split(/\r?\n/)
    .map((line, index) => ({ absolute, line_number: index + 1, line }))
    .filter((item) => item.line.length > 0));
  const claimedOccurrences = new Set();
  for (const identity of oracle.required_evidence_identities) {
    exactKeys(identity, EVIDENCE_IDENTITY_FIELDS, "RELEASE_CASE_EVIDENCE_IDENTITY_FIELDS", "Release evidence identity");
    assertFlow(typeof identity.semantic_id === "string" && USER_FACT.test(identity.semantic_id), "RELEASE_CASE_EVIDENCE_SEMANTIC_ID", "Release evidence semantic ID is invalid");
    assertFlow(typeof identity.marker === "string" && identity.marker.length > 0, "RELEASE_CASE_EVIDENCE_MARKER", "Release evidence marker is invalid");
    stringArray(identity.identity_tokens, "RELEASE_CASE_EVIDENCE_TOKENS", "Release evidence identity tokens", { nonempty: true });
    const matches = attachmentLines.filter((item) => item.line.includes(identity.marker)
      && identity.identity_tokens.every((token) => item.line.includes(token)));
    assertFlow(matches.length === 1, "RELEASE_CASE_EVIDENCE_NOT_UNIQUE", "Release evidence identity must match exactly one frozen log line");
    const occurrence = `${pathIdentity(matches[0].absolute)}\0${matches[0].line_number}`;
    assertFlow(!claimedOccurrences.has(occurrence), "RELEASE_CASE_EVIDENCE_EVENT_MERGED", "Release evidence identities must name distinct frozen log events");
    claimedOccurrences.add(occurrence);
  }
  return oracle;
}

export function loadReleaseCaseOracle(caseRoot) {
  const loaded = loadReleaseCase(caseRoot);
  const semanticOracle = loadSemanticOracle(loaded);
  const canonicalMethodMarkers = new Set(
    semanticOracle.expected_package.method_marker_sets.flatMap((item) => item.all_markers),
  );
  const scenarios = loaded.scenarios.map((scenario) => {
    const oracle = loadScenarioOracle(scenario, loaded);
    const expectedSemanticIds = semanticOracle.expected_package.method_marker_sets.map((item) => item.semantic_id).sort();
    const verdictSemanticIds = oracle.expected_method_verdicts.map((item) => item.semantic_id).sort();
    assertFlow(canonicalJson(verdictSemanticIds) === canonicalJson(expectedSemanticIds), "RELEASE_CASE_METHOD_VERDICT_COVERAGE", "Release method verdicts must exactly cover the semantic method set");
    assertFlow(oracle.required_evidence_identities.every((identity) => expectedSemanticIds.includes(identity.semantic_id)), "RELEASE_CASE_EVIDENCE_SEMANTIC_ID", "Release evidence identity names an unknown semantic method");
    for (const marker of [
      ...oracle.required_evidence_identities.map((identity) => identity.marker),
      oracle.required_request_timeout.marker,
      oracle.required_request_timeout.unlinked_marker,
    ]) {
      assertFlow(canonicalMethodMarkers.has(marker), "RELEASE_CASE_SCENARIO_MARKER_NOT_CANONICAL", `Release scenario marker is not an exact method marker: ${marker}`);
    }
    return { scenario_id: scenario.scenario_id, oracle };
  });
  for (const scenario of loaded.scenarios) {
    const driver = readJson(scenario.driver_path);
    assertFlow(canonicalJson([...driver.initial_user_fact_names].sort()) === canonicalJson([...semanticOracle.expected_package.required_user_inputs].sort()), "RELEASE_CASE_DRIVER_INPUT_COVERAGE", "Release driver inputs must exactly cover the expected Methods user inputs");
  }
  return { case_id: loaded.case_id, semantic_oracle: semanticOracle, scenarios };
}

export function releaseCaseDigests(caseRoot) {
  const loaded = loadReleaseCase(caseRoot);
  const root = path.resolve(caseRoot);
  const relative = (absolute) => path.relative(root, absolute).split(path.sep).join("/");
  const oraclePaths = new Set([relative(loaded.semantic_oracle_path), ...loaded.scenarios.map((scenario) => relative(scenario.oracle_path))]);
  const records = verifyReleaseCaseManifest(caseRoot).records.map(({ path: recordPath, size, sha256 }) => ({ path: recordPath, size, sha256 }));
  const input = records.filter((record) => !oraclePaths.has(record.path));
  const oracle = records.filter((record) => oraclePaths.has(record.path));
  return { input_digest: sha256Bytes(canonicalJson(input)), oracle_digest: sha256Bytes(canonicalJson(oracle)), all_digest: sha256Bytes(canonicalJson(records)), input_records: input, oracle_records: oracle };
}

export function discoverReleaseCaseRoot(releaseRoot) {
  const root = path.resolve(releaseRoot);
  const candidates = fs.readdirSync(root, { withFileTypes: true }).filter((entry) => entry.isDirectory() && fs.existsSync(path.join(root, entry.name, "case.json"))).map((entry) => path.join(root, entry.name)).sort();
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
  const wiki = relative(loaded.wiki_path);
  const registration = relative(loaded.registration_template_path);
  let selected;
  if (partition === "wiki") selected = records.filter((record) => record.path === wiki);
  else if (partition === "registration") selected = records.filter((record) => record.path === registration);
  else if (partition === "oracle") selected = records.filter((record) => record.path === semanticOracle || scenarioOracles.has(record.path));
  else if (partition === "journey") selected = records.filter((record) => record.path !== semanticOracle && !scenarioOracles.has(record.path));
  else assertFlow(false, "RELEASE_CASE_PARTITION", `Unknown Release case partition: ${String(partition)}`);
  return { case_id: loaded.case_id, partition, digest: sha256Bytes(canonicalJson(selected)), records: selected };
}
