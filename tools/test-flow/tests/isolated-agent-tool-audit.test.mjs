import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  auditSkillGenerationTrace,
  discoverLinkedSkillReferences,
  skillGenerationPermissionRules,
  SKILL_GENERATION_TOOL_ATTEMPT_POLICY,
  SKILL_GENERATION_TRACE_CODES as CODES,
  SKILL_GENERATION_TRACE_SCHEMA_VERSION,
  validSkillGenerationTraceAuditReceipt,
} from "../runtime-support/isolated-agent-tool-audit.mjs";

function write(root, relative, content = `${relative}\n`) {
  const target = path.join(root, ...relative.split("/"));
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, content, "utf8");
}

function workspaceFixture() {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "isolated-agent-audit-"));
  const workspaceRoot = path.join(parent, "workspace");
  const skillRoot = path.join(parent, "installed-skill");
  fs.mkdirSync(workspaceRoot);
  write(workspaceRoot, "inputs/wiki.md");
  write(workspaceRoot, "inputs/clarifications.md");
  write(skillRoot, "SKILL.md", [
    "# Converter",
    "[generation](references/generation-spec-v6-reference.md)",
    "[verification](references/verification-contract-v2-reference.md)",
    "[optional](references/ordinary-example.md)",
    "",
  ].join("\n"));
  write(skillRoot, "references/generation-spec-v6-reference.md");
  write(skillRoot, "references/verification-contract-v2-reference.md");
  write(skillRoot, "references/ordinary-example.md");
  write(workspaceRoot, "unlinked.md");
  return { parent, workspaceRoot, skillRoot };
}

function toolUse(id, name, input) {
  return {
    type: "assistant",
    message: { role: "assistant", content: [{ type: "tool_use", id, name, input }] },
  };
}

function toolResult(id, tool = "ordinary", { error = false, success = undefined } = {}) {
  const raw = tool === "Skill" ? { success: success ?? !error } : { type: tool.toLowerCase() };
  if (error && tool !== "Skill") raw.isError = true;
  return {
    type: "user",
    message: { role: "user", content: [{ type: "tool_result", tool_use_id: id, is_error: error }] },
    tool_use_result: raw,
  };
}

function invocation(id, name, input, options) {
  return [toolUse(id, name, input), toolResult(id, name, options)];
}

function validEvents(workspaceRoot, skillRoot, content = "{}") {
  return [
    { type: "system", subtype: "init", cwd: workspaceRoot, permissionMode: "dontAsk", tools: ["Read", "Skill", "Write"] },
    ...invocation("skill", "Skill", { skill: "wiki-to-diagnosis-skill" }),
    ...invocation("wiki", "Read", { file_path: path.join(workspaceRoot, "inputs", "wiki.md") }),
    ...invocation("clarifications", "Read", { file_path: path.join(workspaceRoot, "inputs", "clarifications.md") }),
    ...invocation("generation", "Read", { file_path: path.join(skillRoot, "references", "generation-spec-v6-reference.md") }),
    ...invocation("verification", "Read", { file_path: path.join(skillRoot, "references", "verification-contract-v2-reference.md") }),
    ...invocation("optional", "Read", { file_path: path.join(skillRoot, "references", "ordinary-example.md"), limit: 200 }),
    ...invocation("write", "Write", { file_path: path.join(workspaceRoot, "output", "generation-spec.json"), content }),
    { type: "result", subtype: "success", is_error: false },
  ];
}

function arrangeValid() {
  const fixture = workspaceFixture();
  const content = "{\"schema_version\":6}\n";
  write(fixture.workspaceRoot, "output/generation-spec.json", content);
  return { ...fixture, content, events: validEvents(fixture.workspaceRoot, fixture.skillRoot, content) };
}

function replaceToolPath(events, tool, occurrence, filePath) {
  const matches = events.filter((event) => event?.message?.content?.[0]?.type === "tool_use"
    && event.message.content[0].name === tool);
  matches[occurrence].message.content[0].input.file_path = filePath;
}

function errorCode(callback, code) {
  assert.throws(callback, (error) => error?.code === code, code);
}

test("audits a complete, confined Skill-generation trace and returns only relative paths", () => {
  const fixture = arrangeValid();
  try {
    const receipt = auditSkillGenerationTrace(fixture);
    assert.equal(receipt.schema_version, SKILL_GENERATION_TRACE_SCHEMA_VERSION);
    assert.equal(receipt.status, "PASS");
    assert.equal(receipt.workflow, "skill-generation");
    assert.deepEqual(receipt.tool_inventory, ["Skill", "Read", "Write"]);
    assert.equal(receipt.permission_mode, "dontAsk");
    assert.match(receipt.permission_policy_sha256, /^[a-f0-9]{64}$/);
    assert.deepEqual(receipt.attempt_policy, SKILL_GENERATION_TOOL_ATTEMPT_POLICY);
    assert.match(receipt.attempt_policy_sha256, /^[a-f0-9]{64}$/);
    assert.deepEqual(receipt.accepted_validation_rejections, []);
    assert.deepEqual(receipt.tool_sequence.map((item) => item.tool), ["Skill", "Read", "Read", "Read", "Read", "Read", "Write"]);
    assert.ok(receipt.tool_sequence.every((item) => item.outcome === "SUCCESS"));
    assert.deepEqual(receipt.required_reads, [
      "workspace/inputs/wiki.md",
      "workspace/inputs/clarifications.md",
      "skill/references/generation-spec-v6-reference.md",
      "skill/references/verification-contract-v2-reference.md",
    ]);
    assert.equal(receipt.output.path, "workspace/output/generation-spec.json");
    assert.equal(receipt.output.size_bytes, Buffer.byteLength(fixture.content));
    assert.match(receipt.output.sha256, /^[a-f0-9]{64}$/);
    assert.equal(validSkillGenerationTraceAuditReceipt(receipt), true);
    for (const item of receipt.tool_sequence) {
      if (item.path) assert.equal(path.posix.isAbsolute(item.path) || path.win32.isAbsolute(item.path), false);
    }
    assert.doesNotMatch(JSON.stringify(receipt), new RegExp(fixture.workspaceRoot.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    assert.doesNotMatch(JSON.stringify(receipt), new RegExp(fixture.skillRoot.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("records one explicit empty Write validation rejection immediately before the only successful Write", () => {
  const fixture = arrangeValid();
  try {
    const events = structuredClone(fixture.events);
    events.splice(-3, 0, ...invocation("empty-write", "Write", {}, { error: true }));
    const receipt = auditSkillGenerationTrace({ ...fixture, events });
    assert.equal(receipt.status, "PASS");
    assert.equal(validSkillGenerationTraceAuditReceipt(receipt), true);
    assert.deepEqual(receipt.tool_sequence.slice(-2), [
      {
        ordinal: receipt.tool_sequence.length - 2,
        tool: "Write",
        outcome: "REJECTED",
        classification: "EMPTY_INPUT_REQUIRED_FIELDS_ABSENT",
      },
      {
        ordinal: receipt.tool_sequence.length - 1,
        tool: "Write",
        outcome: "SUCCESS",
        path: "workspace/output/generation-spec.json",
      },
    ]);
    assert.deepEqual(receipt.accepted_validation_rejections, [{
      ordinal: receipt.tool_sequence.length - 2,
      tool: "Write",
      classification: "EMPTY_INPUT_REQUIRED_FIELDS_ABSENT",
      input_key_names: [],
      result_completed_before_success: true,
    }]);
    assert.equal(receipt.output.ordinal, receipt.tool_sequence.length - 1);
    assert.doesNotMatch(JSON.stringify(receipt), /isError|tool_use_error|InputValidationError|content|file_path/);

    const tampered = structuredClone(receipt);
    tampered.accepted_validation_rejections[0].input_key_names.push("file_path");
    assert.equal(validSkillGenerationTraceAuditReceipt(tampered), false);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("builds exact least-privilege CLI rules with Edit authorizing only the audited Write path", () => {
  const fixture = arrangeValid();
  try {
    const linkedReferences = discoverLinkedSkillReferences(fixture.skillRoot);
    const absoluteRule = (relative) => {
      const resolved = path.resolve(fixture.skillRoot, ...relative.split("/"));
      const drive = /^([A-Za-z]):[\\/](.*)$/.exec(resolved);
      const portable = drive
        ? `${drive[1]}/${drive[2].replaceAll("\\", "/")}`
        : resolved.split(path.sep).join("/").replace(/^\/+/, "");
      const escaped = portable.replaceAll("\\", "\\\\").replaceAll("(", "\\(").replaceAll(")", "\\)");
      return `Read(//${escaped})`;
    };
    assert.deepEqual(skillGenerationPermissionRules({ ...fixture, linkedReferences }), [
      "Skill(wiki-to-diagnosis-skill)",
      "Read(/inputs/wiki.md)",
      "Read(/inputs/clarifications.md)",
      ...linkedReferences.map(absoluteRule),
      "Edit(/output/generation-spec.json)",
    ]);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("permission rules escape special characters in absolute Skill paths", () => {
  const fixture = arrangeValid();
  const parentWithParens = `${fixture.parent} (safe)`;
  try {
    fs.renameSync(fixture.parent, parentWithParens);
    const workspaceRoot = path.join(parentWithParens, "workspace");
    const skillRoot = path.join(parentWithParens, "installed-skill");
    const linkedReferences = discoverLinkedSkillReferences(skillRoot);
    const rules = skillGenerationPermissionRules({ workspaceRoot, skillRoot, linkedReferences });
    assert.ok(rules.some((rule) => rule.includes("\\(safe\\)")));
    if (process.platform === "win32") {
      assert.ok(rules.filter((rule) => rule.startsWith("Read(//")).every((rule) => /^Read\(\/[\/][A-Za-z]\//.test(rule)));
      assert.ok(rules.every((rule) => !/\/\/[A-Za-z]:\//.test(rule)));
    }
  } finally {
    fs.rmSync(parentWithParens, { recursive: true, force: true });
  }
});

test("permission rules reject an unsafe root or incomplete and injected reference lists", () => {
  const fixture = arrangeValid();
  try {
    const linkedReferences = discoverLinkedSkillReferences(fixture.skillRoot);
    errorCode(() => skillGenerationPermissionRules({
      workspaceRoot: "relative-workspace",
      skillRoot: fixture.skillRoot,
      linkedReferences,
    }), CODES.ROOT_INVALID);
    errorCode(() => skillGenerationPermissionRules({
      ...fixture,
      linkedReferences: linkedReferences.slice(1),
    }), CODES.REQUIRED_REFERENCE_INVALID);
    errorCode(() => skillGenerationPermissionRules({
      ...fixture,
      linkedReferences: [...linkedReferences, "references/../unlinked.md"],
    }), CODES.PATH_TRAVERSAL);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("trace audit rejects a workspace nested in the source repository", () => {
  const fixture = arrangeValid();
  try {
    const linkedReferences = discoverLinkedSkillReferences(fixture.skillRoot);
    errorCode(() => skillGenerationPermissionRules({
      ...fixture,
      linkedReferences,
      sourceRoot: fixture.parent,
    }), CODES.ROOT_INVALID);
    errorCode(() => auditSkillGenerationTrace({
      ...fixture,
      sourceRoot: fixture.parent,
    }), CODES.ROOT_INVALID);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("discovers sorted, unique, ordinary direct Skill references", () => {
  const fixture = arrangeValid();
  try {
    assert.deepEqual(discoverLinkedSkillReferences(fixture.skillRoot), [
      "references/generation-spec-v6-reference.md",
      "references/ordinary-example.md",
      "references/verification-contract-v2-reference.md",
    ]);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("reference discovery rejects remote, absolute, traversing, missing, and nonordinary links", async (context) => {
  const cases = [
    ["remote", "[bad](https://example.invalid/reference)", CODES.SKILL_LINK_INVALID],
    ["absolute", `[bad](${path.resolve("outside.md").replaceAll("\\", "/")})`, CODES.SKILL_LINK_INVALID],
    ["traversal", "[bad](references/../../outside.md)", CODES.SKILL_LINK_INVALID],
    ["missing", "[bad](references/missing.md)", CODES.PATH_MISSING],
    ["image-only", "![bad](references/ordinary-example.md)", CODES.SKILL_LINK_INVALID],
    ["reference-style", "[bad][reference]\n\n[reference]: references/ordinary-example.md", CODES.SKILL_LINK_INVALID],
    ["html", "<a href=\"references/ordinary-example.md\">bad</a>", CODES.SKILL_LINK_INVALID],
  ];
  for (const [name, markdown, code] of cases) {
    await context.test(name, () => {
      const fixture = arrangeValid();
      try {
        write(fixture.skillRoot, "SKILL.md", `${markdown}\n`);
        errorCode(() => discoverLinkedSkillReferences(fixture.skillRoot), code);
      } finally {
        fs.rmSync(fixture.parent, { recursive: true, force: true });
      }
    });
  }
});

test("reference discovery and audit reject hardlinked files", { skip: process.platform === "win32" }, async (context) => {
  for (const target of [
    { root: "skillRoot", relative: "references/ordinary-example.md" },
    { root: "workspaceRoot", relative: "inputs/wiki.md" },
    { root: "workspaceRoot", relative: "output/generation-spec.json" },
  ]) {
    await context.test(target.relative, () => {
      const fixture = arrangeValid();
      try {
        const original = path.join(fixture[target.root], ...target.relative.split("/"));
        fs.linkSync(original, path.join(fixture.parent, `hardlink-${path.basename(target.relative)}`));
        const action = target.root === "skillRoot"
          ? () => discoverLinkedSkillReferences(fixture.skillRoot)
          : () => auditSkillGenerationTrace(fixture);
        errorCode(action, CODES.PATH_HARDLINK);
      } finally {
        fs.rmSync(fixture.parent, { recursive: true, force: true });
      }
    });
  }
});

test("requires one init, one final successful result, and the exact tool inventory", () => {
  const fixture = arrangeValid();
  try {
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: fixture.events.slice(1) }), CODES.INIT_INVALID);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: [fixture.events[0], ...fixture.events] }), CODES.INIT_INVALID);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: fixture.events.slice(0, -1) }), CODES.RESULT_INVALID);
    const errorTerminal = structuredClone(fixture.events);
    errorTerminal.at(-1).subtype = "error_during_execution";
    errorTerminal.at(-1).is_error = true;
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: errorTerminal }), CODES.RESULT_NOT_SUCCESS);
    const inventory = structuredClone(fixture.events);
    inventory[0].tools.push("Bash");
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: inventory }), CODES.TOOL_INVENTORY_INVALID);
    const permissionMode = structuredClone(fixture.events);
    permissionMode[0].permissionMode = "bypassPermissions";
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: permissionMode }), CODES.PERMISSION_MODE_INVALID);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("requires the first and only tool call to be the exact successful Skill invocation", () => {
  const fixture = arrangeValid();
  try {
    const wrongInput = structuredClone(fixture.events);
    wrongInput[1].message.content[0].input.extra = true;
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: wrongInput }), CODES.SKILL_INVOCATION_INVALID);

    const readFirst = structuredClone(fixture.events);
    const skillPair = readFirst.splice(1, 2);
    readFirst.splice(3, 0, ...skillPair);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: readFirst }), CODES.SKILL_INVOCATION_INVALID);

    const failedSkill = structuredClone(fixture.events);
    failedSkill[2].tool_use_result.success = false;
    failedSkill[2].message.content[0].is_error = false;
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: failedSkill }), CODES.TOOL_RESULT_ERROR);

    const implicitSkill = structuredClone(fixture.events);
    implicitSkill[2].tool_use_result = {};
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: implicitSkill }), CODES.SKILL_RESULT_INVALID);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("rejects disallowed tools and malformed, missing, duplicate, or failed tool results", () => {
  const fixture = arrangeValid();
  try {
    const otherTool = structuredClone(fixture.events);
    otherTool.splice(-1, 0, ...invocation("other", "Bash", { command: "true" }));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: otherTool }), CODES.TOOL_NOT_ALLOWED);

    const unpaired = structuredClone(fixture.events);
    unpaired.splice(4, 1);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: unpaired }), CODES.TOOL_RESULT_MISSING);

    const unmatched = structuredClone(fixture.events);
    unmatched.splice(-1, 0, toolResult("absent", "Read"));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: unmatched }), CODES.TOOL_RESULT_UNMATCHED);

    const duplicate = structuredClone(fixture.events);
    duplicate.splice(-1, 0, toolResult("wiki", "Read"));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: duplicate }), CODES.TOOL_RESULT_DUPLICATE);

    const failedRead = structuredClone(fixture.events);
    failedRead[4].message.content[0].is_error = true;
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: failedRead }), CODES.TOOL_RESULT_ERROR);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("permits only the two inputs and ordinary references linked by SKILL.md", () => {
  const fixture = arrangeValid();
  try {
    const missingRequired = structuredClone(fixture.events);
    missingRequired.splice(7, 2);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: missingRequired }), CODES.REQUIRED_READ_MISSING);

    const unlinked = structuredClone(fixture.events);
    unlinked[11].message.content[0].input.file_path = "unlinked.md";
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: unlinked }), CODES.READ_UNLINKED);

    write(fixture.workspaceRoot, "references/generation-spec-v6-reference.md");
    const workspaceShadow = structuredClone(fixture.events);
    replaceToolPath(
      workspaceShadow,
      "Read",
      2,
      path.join(fixture.workspaceRoot, "references", "generation-spec-v6-reference.md"),
    );
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: workspaceShadow }), CODES.READ_UNLINKED);

    const absolute = structuredClone(fixture.events);
    absolute[3].message.content[0].input.file_path = path.resolve(fixture.parent, "outside.md");
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: absolute }), CODES.READ_UNLINKED);

    const traversal = structuredClone(fixture.events);
    traversal[3].message.content[0].input.file_path = "inputs/../inputs/wiki.md";
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: traversal }), CODES.PATH_TRAVERSAL);

    const relativePaths = structuredClone(fixture.events);
    replaceToolPath(relativePaths, "Read", 0, "inputs/wiki.md");
    replaceToolPath(relativePaths, "Read", 1, "inputs/clarifications.md");
    replaceToolPath(relativePaths, "Write", 0, "output/generation-spec.json");
    assert.equal(auditSkillGenerationTrace({ ...fixture, events: relativePaths }).status, "PASS");

    const partialRequiredRead = structuredClone(fixture.events);
    partialRequiredRead[3].message.content[0].input.limit = 1;
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: partialRequiredRead }), CODES.REQUIRED_READ_PARTIAL);

    const readBeforeSkillResult = structuredClone(fixture.events);
    const skillResult = readBeforeSkillResult.splice(2, 1);
    readBeforeSkillResult.splice(4, 0, ...skillResult);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: readBeforeSkillResult }), CODES.REQUIRED_READ_ORDER_INVALID);

    const requiredReadAfterWrite = structuredClone(fixture.events);
    const generationRead = requiredReadAfterWrite.splice(7, 2);
    requiredReadAfterWrite.splice(-1, 0, ...generationRead);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: requiredReadAfterWrite }), CODES.REQUIRED_READ_ORDER_INVALID);

    errorCode(() => auditSkillGenerationTrace({
      ...fixture,
      requiredReferencePaths: ["references/not-linked.md"],
    }), CODES.REQUIRED_REFERENCE_INVALID);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("requires exactly one successful Write to the fixed regular output path", () => {
  const fixture = arrangeValid();
  try {
    const missingWrite = structuredClone(fixture.events);
    missingWrite.splice(-3, 2);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: missingWrite }), CODES.WRITE_COUNT_INVALID);

    const duplicateWrite = structuredClone(fixture.events);
    duplicateWrite.splice(-1, 0, ...invocation("write-again", "Write", {
      file_path: path.join(fixture.workspaceRoot, "output", "generation-spec.json"),
      content: fixture.content,
    }));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: duplicateWrite }), CODES.WRITE_COUNT_INVALID);

    const twoEmptyRejections = structuredClone(fixture.events);
    twoEmptyRejections.splice(-3, 0,
      ...invocation("empty-write-1", "Write", {}, { error: true }),
      ...invocation("empty-write-2", "Write", {}, { error: true }));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: twoEmptyRejections }), CODES.WRITE_COUNT_INVALID);

    const emptyBeforeRequiredReads = structuredClone(fixture.events);
    emptyBeforeRequiredReads.splice(3, 0, ...invocation("empty-write", "Write", {}, { error: true }));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: emptyBeforeRequiredReads }), CODES.REQUIRED_READ_ORDER_INVALID);

    const readBetweenRetryAndSuccess = structuredClone(fixture.events);
    readBetweenRetryAndSuccess.splice(-5, 0, ...invocation("empty-write", "Write", {}, { error: true }));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: readBetweenRetryAndSuccess }), CODES.REQUIRED_READ_ORDER_INVALID);

    const successBeforeRetryResult = structuredClone(fixture.events);
    successBeforeRetryResult.splice(-3, 0, ...invocation("empty-write", "Write", {}, { error: true }));
    const retryResultIndex = successBeforeRetryResult.findIndex((event) => event?.message?.content?.[0]?.tool_use_id === "empty-write");
    const [retryResult] = successBeforeRetryResult.splice(retryResultIndex, 1);
    const successfulWriteUseIndex = successBeforeRetryResult.findIndex((event) => event?.message?.content?.[0]?.id === "write");
    successBeforeRetryResult.splice(successfulWriteUseIndex + 1, 0, retryResult);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: successBeforeRetryResult }), CODES.REQUIRED_READ_ORDER_INVALID);

    const emptyAfterSuccess = structuredClone(fixture.events);
    emptyAfterSuccess.splice(-1, 0, ...invocation("empty-write", "Write", {}, { error: true }));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: emptyAfterSuccess }), CODES.REQUIRED_READ_ORDER_INVALID);

    for (const input of [
      { file_path: "output/generation-spec.json" },
      { content: fixture.content },
      { unexpected: true },
      null,
      [],
    ]) {
      const failedWriteWithInput = structuredClone(fixture.events);
      failedWriteWithInput.splice(-3, 0, ...invocation("failed-write", "Write", input, { error: true }));
      errorCode(() => auditSkillGenerationTrace({ ...fixture, events: failedWriteWithInput }), CODES.TOOL_RESULT_ERROR);
    }

    const permissionDeniedWrite = structuredClone(fixture.events);
    permissionDeniedWrite.at(-2).message.content[0].is_error = true;
    permissionDeniedWrite.at(-2).tool_use_result = "Error: permission denied";
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: permissionDeniedWrite }), CODES.TOOL_RESULT_ERROR);

    const contradictoryEmptyWrite = structuredClone(fixture.events);
    const contradictory = invocation("empty-write", "Write", {}, { error: true });
    contradictory[1].tool_use_result.success = true;
    contradictoryEmptyWrite.splice(-3, 0, ...contradictory);
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: contradictoryEmptyWrite }), CODES.TOOL_RESULT_ERROR);

    const missingContent = structuredClone(fixture.events);
    delete missingContent.at(-3).message.content[0].input.content;
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: missingContent }), CODES.WRITE_INPUT_INVALID);

    const emptyContent = structuredClone(fixture.events);
    emptyContent.at(-3).message.content[0].input.content = " \n";
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: emptyContent }), CODES.WRITE_INPUT_INVALID);

    const wrongWrite = structuredClone(fixture.events);
    wrongWrite.at(-3).message.content[0].input.file_path = "output/other.json";
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: wrongWrite }), CODES.WRITE_PATH_INVALID);

    const failedWrongWrite = structuredClone(fixture.events);
    failedWrongWrite.splice(-3, 0, ...invocation("wrong-write", "Write", {
      file_path: "output/other.json",
      content: fixture.content,
    }, { error: true }));
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: failedWrongWrite }), CODES.TOOL_RESULT_ERROR);

    const mismatchedContent = structuredClone(fixture.events);
    mismatchedContent.at(-3).message.content[0].input.content = "{}";
    errorCode(() => auditSkillGenerationTrace({ ...fixture, events: mismatchedContent }), CODES.WRITE_CONTENT_MISMATCH);
  } finally {
    fs.rmSync(fixture.parent, { recursive: true, force: true });
  }
});

test("rejects symlinks in every observed input, reference, or output path", { skip: process.platform === "win32" }, async (context) => {
  for (const target of [
    { root: "workspaceRoot", relative: "inputs/wiki.md" },
    { root: "skillRoot", relative: "references/generation-spec-v6-reference.md" },
    { root: "workspaceRoot", relative: "output/generation-spec.json" },
  ]) {
    await context.test(target.relative, () => {
      const fixture = arrangeValid();
      try {
        const link = path.join(fixture[target.root], ...target.relative.split("/"));
        const realTarget = path.join(fixture[target.root], `real-${path.basename(target.relative)}`);
        fs.writeFileSync(realTarget, fs.readFileSync(link));
        fs.rmSync(link);
        fs.symlinkSync(path.relative(path.dirname(link), realTarget), link);
        errorCode(() => auditSkillGenerationTrace(fixture), CODES.PATH_SYMLINK);
      } finally {
        fs.rmSync(fixture.parent, { recursive: true, force: true });
      }
    });
  }
});
