import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  auditMethodsOracle,
  buildMethodsWorkspace,
  methodsPrompt,
  parseArguments,
  safeMethodsRunnerError,
  validateGeneratedPackage,
} from "../runtime/claude-deepseek-methods-runner.mjs";

const REPO = path.resolve(path.dirname(new URL(import.meta.url).pathname), "..", "..", "..", "..", "..");

test("Methods runner accepts only its frozen standalone inputs", () => {
  const names = ["source-root", "claude-entry", "claude-settings", "meta-skill-root", "wiki", "oracle", "registration-template", "python-entry", "cache-root", "work-root", "private-root", "evidence-root", "usage-root", "run-id"];
  const argv = names.flatMap((name) => [`--${name}`, `/${name}`]);
  assert.equal(parseArguments(argv)["run-id"], "/run-id");
  assert.throws(() => parseArguments([...argv, "--docker-context", "colima"]), (error) => error.code === "CLAUDE_DEEPSEEK_METHODS_ARGUMENT_UNKNOWN");
  assert.throws(() => parseArguments(argv.slice(0, -2)), (error) => error.code === "CLAUDE_DEEPSEEK_METHODS_ARGUMENT_MISSING");
});

test("Methods workspace installs only the production generator and closed Wiki identity", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-methods-workspace-"));
  const workspaceRoot = path.join(root, "workspace");
  const configRoot = path.join(root, "config");
  const wiki = path.join(REPO, "tests", "cases", "release", "rpc-timeout-anonymized", "input", "wiki.md");
  const prepared = buildMethodsWorkspace({ workspaceRoot, configRoot, metaSkillRoot: path.join(REPO, ".agents", "skills", "wiki-to-diagnosis-skill"), wiki });
  assert.equal(prepared.sourceWikiIdentity.schema_version, 2);
  assert.equal(prepared.sourceWikiIdentity.source_path, "inputs/wiki.md");
  assert.equal(prepared.sourceWikiIdentity.sha256, "eb39edf220d0eed91ae03eb712efd8974a5e5c82c3deed035c236a0d1bf28aab");
  assert.equal(prepared.sourceWikiIdentity.log_templates.length, 6);
  assert.ok(fs.existsSync(path.join(configRoot, "skills", "wiki-to-diagnosis-skill", "SKILL.md")));
  assert.equal(fs.existsSync(path.join(workspaceRoot, "oracle.json")), false);
  assert.equal(fs.existsSync(path.join(workspaceRoot, "registration-template.json")), false);
});

test("Methods prompt requires Skill first, exact output root, closed tools, and no oracle", () => {
  const prompt = methodsPrompt();
  assert.match(prompt, /first action must call the Skill tool/);
  assert.match(prompt, /output\/diagnose-rpc-timeout/);
  assert.match(prompt, /Use only Skill, Read, and Write/);
  assert.match(prompt, /never write outside/);
  assert.equal(prompt.toLowerCase().includes("oracle.json"), false);
});

test("gate-only oracle validates production-shaped Methods bytes and rejects canary leakage", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-methods-oracle-"));
  const packageRoot = path.join(root, "diagnose-rpc-timeout");
  fs.mkdirSync(path.join(packageRoot, "references"), { recursive: true });
  const oraclePath = path.join(REPO, "tests", "cases", "release", "rpc-timeout-anonymized", "oracle.json");
  const oracle = JSON.parse(fs.readFileSync(oraclePath, "utf8"));
  const expected = oracle.expected_package;
  const methods = expected.method_marker_sets.map((item, index) => ({ id: `method-${index}`, title: item.semantic_id, reference: `references/method-${index}.md`, priority: index + 1, evidence_markers: item.all_markers }));
  const manifest = {
    schema_version: 1,
    skill_name: expected.skill_name,
    source_wiki_sha256: expected.source_wiki_sha256,
    required_user_inputs: expected.required_user_inputs,
    required_artifacts: expected.required_artifacts,
    log_derived_fields: expected.required_log_derived_fields,
    shared_references: ["references/shared.md"],
    methods,
  };
  fs.writeFileSync(path.join(packageRoot, "SKILL.md"), "skill\n");
  fs.writeFileSync(path.join(packageRoot, "methods.json"), `${JSON.stringify(manifest)}\n`);
  fs.writeFileSync(path.join(packageRoot, "references", "shared.md"), expected.required_shared_markers.join("\n"));
  for (const [index] of methods.entries()) fs.writeFileSync(path.join(packageRoot, "references", `method-${index}.md`), "method\n");
  assert.equal(auditMethodsOracle({ packageRoot, oraclePath }).status, "PASS");
  fs.appendFileSync(path.join(packageRoot, "SKILL.md"), oracle.business_canaries[0]);
  assert.throws(() => auditMethodsOracle({ packageRoot, oraclePath }), (error) => error.code === "CLAUDE_DEEPSEEK_METHODS_ORACLE_MISMATCH");
});

test("canonical validator receipt is independent and safe failure projection omits details", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "claude-methods-validator-"));
  const python = path.join(root, "python");
  const validator = path.join(root, "validator.py");
  const packageRoot = path.join(root, "package");
  const wiki = path.join(root, "wiki.md");
  fs.mkdirSync(packageRoot);
  fs.writeFileSync(wiki, "wiki\n");
  fs.writeFileSync(python, "#!/bin/sh\nexec /usr/bin/python3 \"$@\"\n", { mode: 0o700 });
  fs.writeFileSync(validator, "import json\nprint(json.dumps({'ok': True, 'method_count': 3}))\n");
  assert.equal(validateGeneratedPackage({ pythonEntry: python, validator, packageRoot, wiki }).status, "PASS");
  assert.deepEqual(safeMethodsRunnerError({ code: "SAFE", message: "closed", details: { token: "secret" } }), { schema_version: 1, status: "FAIL", code: "SAFE", message: "closed" });
});
