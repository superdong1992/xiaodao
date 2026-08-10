import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const AUTHORITATIVE = [
  "AGENTS.md",
  "README.md",
  "TODO.md",
  "design/README.md",
  "design/test-flow-architecture.md",
  "tools/test-flow/README.md",
  ".claude/skills/problem-locator-client/SKILL.md",
];

function markdownLinks(text) {
  return [...text.matchAll(/\[[^\]]*\]\(([^)]+)\)/g)].map((match) => match[1]);
}

test("authoritative documentation has no retired Test Flow vocabulary", () => {
  const forbidden = [
    /tools\/e2e\//,
    /ReleaseGates/,
    /verification-report\.json/,
    /release\.rollout-parity/,
    /run-windows-linux-e2e\.ps1/,
    /handoff\/S08/,
  ];
  for (const relative of AUTHORITATIVE) {
    const filePath = path.join(REPO_ROOT, relative);
    assert.ok(fs.existsSync(filePath), `missing authoritative document: ${relative}`);
    const text = fs.readFileSync(filePath, "utf8");
    for (const expression of forbidden) assert.doesNotMatch(text, expression, `${relative} contains retired vocabulary`);
  }
});

test("relative Markdown links in authoritative documentation resolve", () => {
  for (const relative of AUTHORITATIVE) {
    const filePath = path.join(REPO_ROOT, relative);
    const text = fs.readFileSync(filePath, "utf8");
    for (const raw of markdownLinks(text)) {
      const target = raw.split("#", 1)[0];
      if (!target || /^(?:https?:|mailto:|#)/.test(raw)) continue;
      const decoded = decodeURIComponent(target.replace(/^<|>$/g, ""));
      assert.ok(fs.existsSync(path.resolve(path.dirname(filePath), decoded)), `${relative} has broken link: ${raw}`);
    }
  }
});

test("the operator and architecture authorities are singular and explicit", () => {
  const root = fs.readFileSync(path.join(REPO_ROOT, "README.md"), "utf8");
  const design = fs.readFileSync(path.join(REPO_ROOT, "design", "README.md"), "utf8");
  assert.match(root, /tools\/test-flow\/README\.md/);
  assert.match(design, /test-flow-architecture\.md/);
});
