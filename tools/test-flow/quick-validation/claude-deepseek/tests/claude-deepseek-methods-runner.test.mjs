import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  auditMethodsOracle,
  buildSourceWikiIdentity,
  buildMethodsWorkspace,
  canonicalEvidenceMarkers,
  methodsPrompt,
  parseArguments,
  safeMethodsRunnerError,
  validateGeneratedRegistration,
} from "../runtime/claude-deepseek-methods-runner.mjs";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "..", "..");

test("Methods runner accepts only its frozen standalone inputs", () => {
  const names = ["source-root", "claude-entry", "claude-settings", "meta-skill-root", "wiki", "oracle", "module", "python-entry", "cache-root", "work-root", "private-root", "evidence-root", "usage-root", "run-id"];
  const argv = names.flatMap((name) => [`--${name}`, `/${name}`]);
  assert.equal(parseArguments(argv)["run-id"], "/run-id");
  assert.throws(() => parseArguments([...argv, "--docker-context", "colima"]), (error) => error.code === "CLAUDE_DEEPSEEK_METHODS_ARGUMENT_UNKNOWN");
  assert.throws(() => parseArguments([...argv, "--registration-template", "/static.json"]), (error) => error.code === "CLAUDE_DEEPSEEK_METHODS_ARGUMENT_UNKNOWN");
  assert.throws(() => parseArguments(argv.slice(0, -2)), (error) => error.code === "CLAUDE_DEEPSEEK_METHODS_ARGUMENT_MISSING");
});

test("Methods workspace installs only the production generator and closed Wiki identity", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-methods-workspace-"));
  const workspaceRoot = path.join(root, "workspace");
  const configRoot = path.join(root, "config");
  const wiki = path.join(REPO, "tests", "cases", "release", "rpc-timeout-anonymized", "input", "wiki.md");
  const prepared = buildMethodsWorkspace({ workspaceRoot, configRoot, metaSkillRoot: path.join(REPO, ".claude", "skills", "wiki-to-logparse-diagnosis-skill"), wiki });
  assert.equal(prepared.sourceWikiIdentity.schema_version, 2);
  assert.equal(prepared.sourceWikiIdentity.source_path, "inputs/wiki.md");
  assert.equal(prepared.sourceWikiIdentity.sha256, "eb39edf220d0eed91ae03eb712efd8974a5e5c82c3deed035c236a0d1bf28aab");
  assert.equal(prepared.sourceWikiIdentity.log_templates.length, 6);
  assert.equal(prepared.sourceWikiIdentity.log_template_extraction_version, 2);
  assert.ok(fs.existsSync(path.join(configRoot, "skills", "wiki-to-logparse-diagnosis-skill", "SKILL.md")));
  assert.equal(fs.existsSync(path.join(workspaceRoot, "oracle.json")), false);
  assert.equal(fs.existsSync(path.join(workspaceRoot, "registration-template.json")), false);
});

test("generation prompt requires the meta Skill first and a complete registration with no runtime Helper call", () => {
  const prompt = methodsPrompt({ canonicalMarkers: ["API_COMPLETE service=", "LATE_RESPONSE service="] });
  assert.match(prompt, /first action must call the Skill tool/);
  assert.match(prompt, /output\/rpc-timeout-methods-v1/);
  assert.match(prompt, /Use only Skill, Read, and Write/);
  assert.match(prompt, /never write outside/);
  assert.match(prompt, /must not call Skill\(logparse-diagnose\)/);
  assert.match(prompt, /\["API_COMPLETE service=","LATE_RESPONSE service="\]/);
  assert.match(prompt, /Bare or shortened event names/);
  assert.match(prompt, /activation_markers/);
  assert.match(prompt, /must not activate a method/);
  assert.equal(prompt.toLowerCase().includes("oracle.json"), false);
});

test("source identity v2 extracts text and bare fences while ignoring other language fences", () => {
  const identity = buildSourceWikiIdentity(Buffer.from("```text\nRPC id={request_id}\n```\n\n```\nqueue %u\n```\n\n```json\n{\\\"ignored\\\":\\\"{field}\\\"}\n```\n", "utf8"));
  assert.equal(identity.schema_version, 2);
  assert.equal(identity.log_template_extraction_version, 2);
  assert.deepEqual(identity.log_templates, ["RPC id={request_id}", "queue %u"]);
  assert.deepEqual(canonicalEvidenceMarkers(identity.log_templates), ["RPC id=", "queue"]);
  assert.deepEqual(canonicalEvidenceMarkers(["{time} left {id} longer {cost} trailing suffix"]), ["longer"]);
  assert.deepEqual(canonicalEvidenceMarkers(["{only} trailing suffix"]), []);
});

test("gate-only oracle validates production-shaped Methods bytes and rejects canary leakage", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-methods-oracle-"));
  const registrationRoot = path.join(root, "rpc-timeout-methods-v1");
  const packageRoot = path.join(registrationRoot, "package", "diagnose-rpc-timeout");
  fs.mkdirSync(path.join(packageRoot, "references"), { recursive: true });
  const oraclePath = path.join(REPO, "tests", "cases", "release", "rpc-timeout-anonymized", "oracle.json");
  const oracle = JSON.parse(fs.readFileSync(oraclePath, "utf8"));
  const expected = oracle.expected_package;
  const methods = expected.method_marker_sets.map((item, index) => ({ id: `method-${index}`, title: item.semantic_id, reference: `references/method-${index}.md`, priority: index + 1, evidence_markers: item.all_markers, activation_markers: item.activation_markers }));
  const manifest = {
    schema_version: 1,
    skill_name: expected.skill_name,
    source_wiki_sha256: expected.source_wiki_sha256,
    required_user_inputs: ["problem_time", "client_slot", "client_process_name", "server_slot", "server_process_name", "client_pid", "server_pid", "service", "api"],
    required_artifacts: expected.required_artifacts,
    log_derived_fields: expected.required_log_derived_fields,
    shared_references: ["references/shared.md"],
    methods,
  };
  fs.writeFileSync(path.join(packageRoot, "SKILL.md"), "skill\n");
  fs.writeFileSync(path.join(packageRoot, "methods.json"), `${JSON.stringify(manifest)}\n`);
  fs.writeFileSync(path.join(packageRoot, "references", "shared.md"), expected.required_shared_markers.join("\n"));
  for (const [index] of methods.entries()) fs.writeFileSync(path.join(packageRoot, "references", `method-${index}.md`), "method\n");
  assert.equal(auditMethodsOracle({ registrationRoot, oraclePath }).status, "PASS");
  const merged = structuredClone(manifest);
  const everyMarker = [...new Set(expected.method_marker_sets.flatMap((item) => item.all_markers))];
  merged.methods[0].evidence_markers = everyMarker;
  merged.methods[1].evidence_markers = everyMarker;
  merged.methods[2].evidence_markers = everyMarker;
  fs.writeFileSync(path.join(packageRoot, "methods.json"), `${JSON.stringify(merged)}\n`);
  assert.throws(() => auditMethodsOracle({ registrationRoot, oraclePath }), (error) => error.code === "CLAUDE_DEEPSEEK_METHODS_ORACLE_MISMATCH");
  const wrongActivation = structuredClone(manifest);
  wrongActivation.methods[0].activation_markers = [
    wrongActivation.methods[0].evidence_markers[0],
  ];
  fs.writeFileSync(path.join(packageRoot, "methods.json"), `${JSON.stringify(wrongActivation)}\n`);
  assert.throws(() => auditMethodsOracle({ registrationRoot, oraclePath }), (error) => error.code === "CLAUDE_DEEPSEEK_METHODS_ORACLE_MISMATCH");
  fs.writeFileSync(path.join(packageRoot, "methods.json"), `${JSON.stringify(manifest)}\n`);
  fs.writeFileSync(path.join(packageRoot, "references", "source-log-templates.md"), expected.required_shared_markers.join("\n"));
  const hiddenShared = { ...manifest, shared_references: ["references/source-log-templates.md", "references/shared.md"] };
  fs.writeFileSync(path.join(packageRoot, "methods.json"), `${JSON.stringify(hiddenShared)}\n`);
  fs.writeFileSync(path.join(packageRoot, "references", "shared.md"), "missing shared symptoms\n");
  assert.throws(() => auditMethodsOracle({ registrationRoot, oraclePath }), (error) => error.code === "CLAUDE_DEEPSEEK_METHODS_ORACLE_MISMATCH");
  fs.writeFileSync(path.join(packageRoot, "methods.json"), `${JSON.stringify(manifest)}\n`);
  fs.writeFileSync(path.join(packageRoot, "references", "shared.md"), expected.required_shared_markers.join("\n"));
  fs.appendFileSync(path.join(packageRoot, "SKILL.md"), oracle.business_canaries[0]);
  assert.throws(() => auditMethodsOracle({ registrationRoot, oraclePath }), (error) => error.code === "CLAUDE_DEEPSEEK_METHODS_ORACLE_MISMATCH");
});

test("canonical validator receipt is independent and safe failure projection omits details", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-methods-validator-"));
  const python = path.join(root, "python");
  const validator = path.join(root, "validator.py");
  const registrationRoot = path.join(root, "registration");
  const wiki = path.join(root, "wiki.md");
  const sourceIdentity = path.join(root, "source-identity.json");
  fs.mkdirSync(registrationRoot);
  fs.writeFileSync(wiki, "wiki\n");
  fs.writeFileSync(sourceIdentity, "{}\n");
  fs.writeFileSync(validator, "validator\n");
  let observed = null;
  const spawnImpl = (entry, args) => { observed = { entry, args }; return { status: 0, signal: null, error: null, stdout: '{"ok":true,"method_count":3}\n' }; };
  assert.equal(validateGeneratedRegistration({ pythonEntry: python, validator, registrationRoot, wiki, module: "rpc", sourceIdentity }, { spawnImpl }).status, "PASS");
  assert.equal(observed.entry, python);
  assert.deepEqual(observed.args.slice(3), ["--registration-dir", registrationRoot, "--wiki", wiki, "--module", "rpc", "--source-identity", sourceIdentity, "--json"]);
  assert.deepEqual(safeMethodsRunnerError({ code: "SAFE", message: "closed", details: { token: "secret" } }), { schema_version: 1, status: "FAIL", code: "SAFE", message: "closed" });
});
