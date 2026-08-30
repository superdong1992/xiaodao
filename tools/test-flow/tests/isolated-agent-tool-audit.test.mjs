import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  auditSkillGenerationTrace,
  discoverLinkedSkillReferences,
  skillGenerationPermissionRules,
  SKILL_GENERATION_TRACE_CODES as CODES,
  SKILL_GENERATION_TRACE_SCHEMA_VERSION,
  validSkillGenerationTraceAuditReceipt,
} from "../runtime-support/isolated-agent-tool-audit.mjs";

const SKILL = "diagnose-rpc-timeout";
const WIKI = [
  "# Wiki",
  "",
  "  ```text  ",
  "  API_COMPLETE service={service} cost_us={cost_us}  ",
  "not a template",
  "%s rpc timeout %u",
  "API_COMPLETE service={service} cost_us={cost_us}",
  "```",
  "",
].join("\r\n");
const LOG_TEMPLATES = Object.freeze([
  "API_COMPLETE service={service} cost_us={cost_us}",
  "%s rpc timeout %u",
  "API_COMPLETE service={service} cost_us={cost_us}",
]);
const SOURCE_LOG_TEMPLATES = `# Source log templates\n\n\`\`\`text\n${LOG_TEMPLATES.join("\n")}\n\`\`\`\n`;
const SOURCE_LOG_TEMPLATES_REFERENCE = "references/source-log-templates.md";

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  if (value !== null && typeof value === "object") return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function sourceIdentity(wiki = WIKI, templates = LOG_TEMPLATES) {
  const value = {
    algorithm: "sha256",
    log_template_extraction_version: 1,
    log_template_inventory_sha256: sha256(canonicalJson({ version: 1, templates })),
    log_templates: [...templates],
    schema_version: 2,
    sha256: sha256(wiki),
    source_path: "inputs/wiki.md",
  };
  return `${canonicalJson(value)}\n`;
}

const PACKAGE_FILES = Object.freeze({
  [`output/${SKILL}/SKILL.md`]: "---\nname: diagnose-rpc-timeout\ndescription: Diagnose RPC timeout evidence.\n---\n",
  [`output/${SKILL}/methods.json`]: `${JSON.stringify({
    schema_version: 1,
    skill_name: SKILL,
    shared_references: [SOURCE_LOG_TEMPLATES_REFERENCE, "references/shared-boundaries.md"],
    methods: [{
      id: "api-overrun",
      title: "API overrun",
      reference: "references/api-overrun.md",
      priority: 1,
      evidence_markers: ["API_COMPLETE service=", "%s rpc timeout"],
      activation_markers: ["API_COMPLETE service="],
    }],
  })}\n`,
  [`output/${SKILL}/references/api-overrun.md`]: "# API overrun\n",
  [`output/${SKILL}/references/shared-boundaries.md`]: "# Shared boundaries\n",
  [`output/${SKILL}/${SOURCE_LOG_TEMPLATES_REFERENCE}`]: SOURCE_LOG_TEMPLATES,
});

function write(root, relative, content = `${relative}\n`) {
  const target = path.join(root, ...relative.split("/"));
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, content, "utf8");
}

function fixtureRoot() {
  const parent = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), "methods-trace-audit-")));
  const workspaceRoot = path.join(parent, "workspace");
  const skillRoot = path.join(parent, "installed-meta-skill");
  fs.mkdirSync(workspaceRoot);
  write(workspaceRoot, "inputs/wiki.md", WIKI);
  write(workspaceRoot, "runtime/source-wiki-identity.json", sourceIdentity());
  write(skillRoot, "SKILL.md", "# Converter\n[output contract](references/output-contract.md)\n");
  write(skillRoot, "references/output-contract.md", "# Methods output contract\n");
  for (const [relative, content] of Object.entries(PACKAGE_FILES)) write(workspaceRoot, relative, content);
  return { parent, workspaceRoot, skillRoot };
}

function toolUse(id, name, input) {
  return { type: "assistant", message: { role: "assistant", content: [{ type: "tool_use", id, name, input }] } };
}

function toolResult(id, tool, { error = false, success = undefined } = {}) {
  const raw = tool === "Skill" ? { success: success ?? !error } : { type: tool.toLowerCase() };
  if (error && tool !== "Skill") raw.isError = true;
  return {
    type: "user",
    message: { role: "user", content: [{ type: "tool_result", tool_use_id: id, is_error: error }] },
    tool_use_result: raw,
  };
}

function invocation(id, tool, input, options = {}) {
  return [toolUse(id, tool, input), toolResult(id, tool, options)];
}

function validEvents(workspaceRoot, skillRoot) {
  return [
    { type: "system", subtype: "init", cwd: workspaceRoot, permissionMode: "dontAsk", tools: ["Read", "Skill", "Write"] },
    ...invocation("skill", "Skill", { skill: "wiki-to-diagnosis-skill" }),
    ...invocation("wiki", "Read", { file_path: path.join(workspaceRoot, "inputs", "wiki.md") }),
    ...invocation("identity", "Read", { file_path: path.join(workspaceRoot, "runtime", "source-wiki-identity.json") }),
    ...invocation("contract", "Read", { file_path: path.join(skillRoot, "references", "output-contract.md") }),
    ...Object.entries(PACKAGE_FILES).flatMap(([relative, content], index) => invocation(`write-${index}`, "Write", { file_path: relative, content })),
    { type: "result", subtype: "success", is_error: false },
  ];
}

function arrangeValid() {
  const fixture = fixtureRoot();
  return { ...fixture, events: validEvents(fixture.workspaceRoot, fixture.skillRoot) };
}

function useEvent(events, id) {
  return events.find((event) => event?.message?.content?.[0]?.id === id);
}

function resultEvent(events, id) {
  return events.find((event) => event?.message?.content?.[0]?.tool_use_id === id);
}

function replacePackageContent(fixture, relative, content) {
  write(fixture.workspaceRoot, relative, content);
  const event = fixture.events.find((candidate) => {
    const block = candidate?.message?.content?.[0];
    return block?.type === "tool_use" && block.name === "Write" && block.input?.file_path === relative;
  });
  assert.ok(event, `missing Write fixture for ${relative}`);
  event.message.content[0].input.content = content;
}

function errorCode(action, expected) {
  assert.throws(action, (error) => error?.code === expected);
}

function absolutePermissionRule(filePath) {
  const resolved = path.resolve(filePath);
  const drive = /^([A-Za-z]):[\\/](.*)$/.exec(resolved);
  const portable = drive
    ? `${drive[1]}/${drive[2].replaceAll("\\", "/")}`
    : resolved.split(path.sep).join("/").replace(/^\/+/, "");
  return `Read(//${portable.replaceAll("\\", "\\\\").replaceAll("(", "\\(").replaceAll(")", "\\)")})`;
}

test("audits one strict multi-file Methods package and emits a closed v6 receipt", () => {
  const fixture = arrangeValid();
  try {
    const receipt = auditSkillGenerationTrace(fixture);
    assert.equal(receipt.schema_version, SKILL_GENERATION_TRACE_SCHEMA_VERSION);
    assert.equal(receipt.status, "PASS");
    assert.equal(receipt.package.skill_name, SKILL);
    assert.equal(receipt.package.file_count, Object.keys(PACKAGE_FILES).length);
    assert.deepEqual(receipt.package.method_marker_sets, [{
      method_id: "api-overrun",
      evidence_markers: ["API_COMPLETE service=", "%s rpc timeout"],
      activation_markers: ["API_COMPLETE service="],
    }]);
    assert.deepEqual(receipt.required_reads, [
      "workspace/inputs/wiki.md",
      "workspace/runtime/source-wiki-identity.json",
      "skill/references/output-contract.md",
    ]);
    assert.deepEqual(receipt.linked_references, ["skill/references/output-contract.md"]);
    assert.equal(receipt.tool_sequence.filter((record) => record.tool === "Write").length, 5);
    assert.deepEqual(receipt.source_log_templates, {
      extraction_version: 1,
      count: 3,
      inventory_sha256: sha256(canonicalJson({ version: 1, templates: LOG_TEMPLATES })),
      reference_path: `workspace/output/${SKILL}/${SOURCE_LOG_TEMPLATES_REFERENCE}`,
      reference_sha256: sha256(SOURCE_LOG_TEMPLATES),
    });
    assert.equal(validSkillGenerationTraceAuditReceipt(receipt), true);

    const tampered = structuredClone(receipt);
    tampered.package.files[0].sha256 = "0".repeat(64);
    assert.equal(validSkillGenerationTraceAuditReceipt(tampered), false);

    const tamperedActivation = structuredClone(receipt);
    tamperedActivation.package.method_marker_sets[0].activation_markers = ["%s rpc timeout", "API_COMPLETE service="];
    assert.equal(validSkillGenerationTraceAuditReceipt(tamperedActivation), false);

    const legacy = structuredClone(receipt);
    legacy.schema_version = 4;
    assert.equal(validSkillGenerationTraceAuditReceipt(legacy), false);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("rejects missing, empty, duplicate, non-member, and reordered activation markers", async (context) => {
  const cases = [
    ["missing", (method) => { delete method.activation_markers; }],
    ["empty", (method) => { method.activation_markers = []; }],
    ["duplicate", (method) => { method.activation_markers = ["API_COMPLETE service=", "API_COMPLETE service="]; }],
    ["non-member", (method) => { method.activation_markers = ["INVENTED"]; }],
    ["reordered", (method) => { method.activation_markers = ["%s rpc timeout", "API_COMPLETE service="]; }],
  ];
  for (const [name, mutate] of cases) {
    await context.test(name, () => {
      const fixture = arrangeValid();
      try {
        const relative = `output/${SKILL}/methods.json`;
        const methods = JSON.parse(PACKAGE_FILES[relative]);
        mutate(methods.methods[0]);
        replacePackageContent(fixture, relative, `${JSON.stringify(methods)}\n`);
        errorCode(() => auditSkillGenerationTrace(fixture), CODES.METHODS_CONTRACT_INVALID);
      } finally {
        fs.rmSync(fixture.parent, { recursive: true, force: true });
      }
    });
  }
});

test("grants only the Wiki, source identity, linked output contract, Skill load, and audited output subtree", () => {
  const fixture = arrangeValid();
  try {
    const linkedReferences = discoverLinkedSkillReferences(fixture.skillRoot);
    assert.deepEqual(linkedReferences, ["references/output-contract.md"]);
    assert.deepEqual(skillGenerationPermissionRules({ ...fixture, linkedReferences }), [
      "Skill(wiki-to-diagnosis-skill)",
      "Read(/inputs/wiki.md)",
      "Read(/runtime/source-wiki-identity.json)",
      absolutePermissionRule(path.join(fixture.skillRoot, "references", "output-contract.md")),
      "Edit(/output/**)",
    ]);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("hard-cuts clarifications and every GenerationSpec-era meta Skill reference", () => {
  const fixture = arrangeValid();
  try {
    write(fixture.workspaceRoot, "inputs/clarifications.md", "legacy\n");
    const legacyRead = structuredClone(fixture.events);
    useEvent(legacyRead, "wiki").message.content[0].input.file_path = "inputs/clarifications.md";
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: legacyRead }), CODES.READ_UNLINKED);

    write(fixture.skillRoot, "references/generation-spec-v6-reference.md", "legacy\n");
    write(fixture.skillRoot, "SKILL.md", [
      "[output](references/output-contract.md)",
      "[legacy](references/generation-spec-v6-reference.md)",
      "",
    ].join("\n"));
    errorCode(() => discoverLinkedSkillReferences(fixture.skillRoot), CODES.REQUIRED_REFERENCE_INVALID);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("requires the first and only exact successful meta Skill invocation", () => {
  const fixture = arrangeValid();
  try {
    const wrong = structuredClone(fixture.events);
    useEvent(wrong, "skill").message.content[0].input.extra = true;
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: wrong }), CODES.SKILL_INVOCATION_INVALID);

    const failed = structuredClone(fixture.events);
    resultEvent(failed, "skill").tool_use_result.success = false;
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: failed }), CODES.TOOL_RESULT_ERROR);

    const second = structuredClone(fixture.events);
    second.splice(-1, 0, ...invocation("skill-again", "Skill", { skill: "wiki-to-diagnosis-skill" }));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: second }), CODES.SKILL_INVOCATION_INVALID);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("rejects any extra tool, failed call, missing result, or non-sequential call", () => {
  const fixture = arrangeValid();
  try {
    const bash = structuredClone(fixture.events);
    bash.splice(-1, 0, ...invocation("bash", "Bash", { command: "true" }));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: bash }), CODES.TOOL_NOT_ALLOWED);

    const failedWrite = structuredClone(fixture.events);
    resultEvent(failedWrite, "write-0").message.content[0].is_error = true;
    resultEvent(failedWrite, "write-0").tool_use_result.isError = true;
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: failedWrite }), CODES.TOOL_RESULT_ERROR);

    const missing = structuredClone(fixture.events);
    missing.splice(missing.indexOf(resultEvent(missing, "contract")), 1);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: missing }), CODES.TOOL_RESULT_MISSING);

    const concurrent = structuredClone(fixture.events);
    const contractResult = resultEvent(concurrent, "contract");
    concurrent.splice(concurrent.indexOf(contractResult), 1);
    concurrent.splice(concurrent.indexOf(useEvent(concurrent, "write-0")) + 1, 0, contractResult);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: concurrent }), CODES.REQUIRED_READ_ORDER_INVALID);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("allows exactly three full Reads and requires all before every Write", () => {
  const fixture = arrangeValid();
  try {
    const partial = structuredClone(fixture.events);
    useEvent(partial, "wiki").message.content[0].input.limit = 1;
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: partial }), CODES.READ_INPUT_INVALID);

    const extra = structuredClone(fixture.events);
    extra.splice(extra.indexOf(useEvent(extra, "write-0")), 0, ...invocation("wiki-again", "Read", { file_path: "inputs/wiki.md" }));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: extra }), CODES.REQUIRED_READ_MISSING);

    const unlinked = structuredClone(fixture.events);
    write(fixture.workspaceRoot, "other.md", "no\n");
    useEvent(unlinked, "wiki").message.content[0].input.file_path = "other.md";
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: unlinked }), CODES.READ_UNLINKED);

    const afterWrite = structuredClone(fixture.events);
    const contractPair = [useEvent(afterWrite, "contract"), resultEvent(afterWrite, "contract")];
    for (const event of contractPair) afterWrite.splice(afterWrite.indexOf(event), 1);
    afterWrite.splice(-1, 0, ...contractPair);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: afterWrite }), CODES.REQUIRED_READ_ORDER_INVALID);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("requires a closed canonical v2 source identity with the exact ordered Wiki template inventory", async (context) => {
  const cases = [
    ["legacy schema", (value) => { value.schema_version = 1; }],
    ["extra key", (value) => { value.extra = true; }],
    ["Wiki digest substitution", (value) => { value.sha256 = "0".repeat(64); }],
    ["extraction version substitution", (value) => { value.log_template_extraction_version = 2; }],
    ["template omission", (value) => { value.log_templates.pop(); }],
    ["template reordering", (value) => { [value.log_templates[0], value.log_templates[1]] = [value.log_templates[1], value.log_templates[0]]; }],
    ["template substitution", (value) => { value.log_templates[0] = "INVENTED value={value}"; }],
    ["inventory digest substitution", (value) => { value.log_template_inventory_sha256 = "0".repeat(64); }],
  ];
  for (const [name, mutate] of cases) {
    await context.test(name, () => {
      const fixture = arrangeValid();
      try {
        const identityPath = path.join(fixture.workspaceRoot, "runtime", "source-wiki-identity.json");
        const value = JSON.parse(fs.readFileSync(identityPath, "utf8"));
        mutate(value);
        fs.writeFileSync(identityPath, `${canonicalJson(value)}\n`);
        errorCode(() => auditSkillGenerationTrace(fixture), CODES.SOURCE_IDENTITY_INVALID);
      } finally {
        fs.rmSync(fixture.parent, { recursive: true, force: true });
      }
    });
  }

  await context.test("non-canonical bytes", () => {
    const fixture = arrangeValid();
    try {
      const identityPath = path.join(fixture.workspaceRoot, "runtime", "source-wiki-identity.json");
      const value = JSON.parse(fs.readFileSync(identityPath, "utf8"));
      fs.writeFileSync(identityPath, `${JSON.stringify(value, null, 2)}\n`);
      errorCode(
        () => skillGenerationPermissionRules({ ...fixture, linkedReferences: ["references/output-contract.md"] }),
        CODES.SOURCE_IDENTITY_INVALID,
      );
    } finally {
      fs.rmSync(fixture.parent, { recursive: true, force: true });
    }
  });
});

test("requires one traced deterministic source-log-templates shared reference", async (context) => {
  const cases = [
    ["fixed reference missing from package", (fixture) => {
      fs.rmSync(path.join(fixture.workspaceRoot, `output/${SKILL}/${SOURCE_LOG_TEMPLATES_REFERENCE}`));
      for (const event of [useEvent(fixture.events, "write-4"), resultEvent(fixture.events, "write-4")]) {
        fixture.events.splice(fixture.events.indexOf(event), 1);
      }
    }, CODES.SOURCE_LOG_TEMPLATES_INVALID],
    ["fixed reference materialized without a traced Write", (fixture) => {
      for (const event of [useEvent(fixture.events, "write-4"), resultEvent(fixture.events, "write-4")]) {
        fixture.events.splice(fixture.events.indexOf(event), 1);
      }
    }, CODES.OUTPUT_TREE_INVALID],
    ["template omitted from fixed reference", (fixture) => {
      const relative = `output/${SKILL}/${SOURCE_LOG_TEMPLATES_REFERENCE}`;
      replacePackageContent(fixture, relative, `# Source log templates\n\n\`\`\`text\n${LOG_TEMPLATES.slice(0, -1).join("\n")}\n\`\`\`\n`);
    }, CODES.SOURCE_LOG_TEMPLATES_INVALID],
    ["template order changed in fixed reference", (fixture) => {
      const relative = `output/${SKILL}/${SOURCE_LOG_TEMPLATES_REFERENCE}`;
      replacePackageContent(fixture, relative, `# Source log templates\n\n\`\`\`text\n${[LOG_TEMPLATES[1], LOG_TEMPLATES[0], LOG_TEMPLATES[2]].join("\n")}\n\`\`\`\n`);
    }, CODES.SOURCE_LOG_TEMPLATES_INVALID],
    ["fixed reference is not first shared reference", (fixture) => {
      const relative = `output/${SKILL}/methods.json`;
      const methods = JSON.parse(PACKAGE_FILES[relative]);
      methods.shared_references.reverse();
      replacePackageContent(fixture, relative, `${JSON.stringify(methods)}\n`);
    }, CODES.SOURCE_LOG_TEMPLATES_INVALID],
    ["fixed reference is used as a method reference", (fixture) => {
      const relative = `output/${SKILL}/methods.json`;
      const methods = JSON.parse(PACKAGE_FILES[relative]);
      methods.methods[0].reference = SOURCE_LOG_TEMPLATES_REFERENCE;
      replacePackageContent(fixture, relative, `${JSON.stringify(methods)}\n`);
    }, CODES.SOURCE_LOG_TEMPLATES_INVALID],
  ];
  for (const [name, mutate, expectedCode] of cases) {
    await context.test(name, () => {
      const fixture = arrangeValid();
      try {
        mutate(fixture);
        errorCode(() => auditSkillGenerationTrace(fixture), expectedCode);
      } finally {
        fs.rmSync(fixture.parent, { recursive: true, force: true });
      }
    });
  }
});

test("rejects legacy, escaping, mixed-root, duplicate, missing, and untraced output paths", async (context) => {
  const cases = [
    ["legacy GenerationSpec", (fixture, events) => { useEvent(events, "write-0").message.content[0].input.file_path = "output/generation-spec.json"; }, CODES.WRITE_PATH_INVALID],
    ["escaping path", (fixture, events) => { useEvent(events, "write-0").message.content[0].input.file_path = "output/../escape.md"; }, CODES.PATH_TRAVERSAL],
    ["second Skill root", (fixture, events) => { useEvent(events, "write-0").message.content[0].input.file_path = "output/other-skill/SKILL.md"; }, CODES.WRITE_PATH_INVALID],
    ["duplicate Write", (fixture, events) => { events.splice(-1, 0, ...invocation("duplicate", "Write", { file_path: `output/${SKILL}/SKILL.md`, content: PACKAGE_FILES[`output/${SKILL}/SKILL.md`] })); }, CODES.OUTPUT_TREE_INVALID],
    ["missing traced Write", (fixture, events) => { const use = useEvent(events, "write-3"); const result = resultEvent(events, "write-3"); events.splice(events.indexOf(use), 1); events.splice(events.indexOf(result), 1); }, CODES.OUTPUT_TREE_INVALID],
    ["extra untraced file", (fixture) => { write(fixture.workspaceRoot, `output/${SKILL}/references/untraced.md`, "extra\n"); }, CODES.OUTPUT_TREE_INVALID],
  ];
  for (const [name, mutate, code] of cases) {
    await context.test(name, () => {
      const fixture = arrangeValid();
      try {
        const events = structuredClone(fixture.events);
        mutate(fixture, events);
        errorCode(() => auditSkillGenerationTrace({ ...fixture, events }), code);
      } finally {
        fs.rmSync(fixture.parent, { recursive: true, force: true });
      }
    });
  }
});

test("requires exact Write inputs and byte-for-byte materialization", () => {
  const fixture = arrangeValid();
  try {
    const missingContent = structuredClone(fixture.events);
    delete useEvent(missingContent, "write-0").message.content[0].input.content;
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: missingContent }), CODES.WRITE_INPUT_INVALID);

    const empty = structuredClone(fixture.events);
    useEvent(empty, "write-0").message.content[0].input.content = "";
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: empty }), CODES.WRITE_INPUT_INVALID);

    const mismatch = structuredClone(fixture.events);
    useEvent(mismatch, "write-0").message.content[0].input.content += "drift\n";
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: mismatch }), CODES.WRITE_CONTENT_MISMATCH);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("rejects unsafe roots, references, symlinks, and hardlinks", { skip: process.platform === "win32" }, async (context) => {
  await context.test("nested source workspace", () => {
    const fixture = arrangeValid();
    try {
      errorCode(() => auditSkillGenerationTrace({ ...fixture, sourceRoot: fixture.parent }), CODES.ROOT_INVALID);
    } finally {
      fs.rmSync(fixture.parent, { recursive: true, force: true });
    }
  });

  await context.test("remote Skill link", () => {
    const fixture = arrangeValid();
    try {
      write(fixture.skillRoot, "SKILL.md", "[bad](https://example.invalid/output-contract.md)\n");
      errorCode(() => discoverLinkedSkillReferences(fixture.skillRoot), CODES.SKILL_LINK_INVALID);
    } finally {
      fs.rmSync(fixture.parent, { recursive: true, force: true });
    }
  });

  for (const [label, rootKey, relative] of [
    ["wiki symlink", "workspaceRoot", "inputs/wiki.md"],
    ["source identity symlink", "workspaceRoot", "runtime/source-wiki-identity.json"],
    ["contract symlink", "skillRoot", "references/output-contract.md"],
    ["output symlink", "workspaceRoot", `output/${SKILL}/references/api-overrun.md`],
  ]) {
    await context.test(label, () => {
      const fixture = arrangeValid();
      try {
        const link = path.join(fixture[rootKey], ...relative.split("/"));
        const target = path.join(fixture.parent, `real-${path.basename(relative)}`);
        fs.writeFileSync(target, fs.readFileSync(link));
        fs.rmSync(link);
        fs.symlinkSync(target, link);
        errorCode(() => auditSkillGenerationTrace(fixture), CODES.PATH_SYMLINK);
      } finally {
        fs.rmSync(fixture.parent, { recursive: true, force: true });
      }
    });
  }

  await context.test("output hardlink", () => {
    const fixture = arrangeValid();
    try {
      const output = path.join(fixture.workspaceRoot, `output/${SKILL}/methods.json`);
      fs.linkSync(output, path.join(fixture.parent, "methods-hardlink.json"));
      errorCode(() => auditSkillGenerationTrace(fixture), CODES.PATH_HARDLINK);
    } finally {
      fs.rmSync(fixture.parent, { recursive: true, force: true });
    }
  });
});
