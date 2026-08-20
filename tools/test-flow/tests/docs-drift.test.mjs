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
  "docs/browser-rest-api.md",
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

test("README keeps the browser REST API entry isolated from other protocols", () => {
  const root = fs.readFileSync(path.join(REPO_ROOT, "README.md"), "utf8");
  const heading = /^### 浏览器 REST API\s*$/m;
  const match = heading.exec(root);
  assert.ok(match, "README is missing the browser REST API heading");
  const following = root.slice(match.index + match[0].length);
  const nextHeading = /^###\s+.+$/m.exec(following);
  assert.ok(nextHeading, "browser REST API entry must end at the next peer heading");
  const section = following.slice(0, nextHeading.index);
  assert.match(section, /\]\(docs\/browser-rest-api\.md\)/, "browser REST API entry must link the standalone guide");
  assert.doesNotMatch(section, /\/api\/v1\//, "endpoint details belong in the standalone guide");
  assert.doesNotMatch(section, /\b(?:MCP|Claude|Skill)\b|problem_locator_[a-z_]+/i, "browser REST API entry contains cross-protocol details");
});
