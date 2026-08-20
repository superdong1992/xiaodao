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

function pythonConstant(text, name) {
  const match = new RegExp(`^${name}\\s*=\\s*(?:"([^"]+)"|([0-9]+))\\s*$`, "m").exec(text);
  assert.ok(match, `missing Python constant: ${name}`);
  return match[1] ?? match[2];
}

function projectVersion(text) {
  let inProject = false;
  for (const line of text.split(/\r?\n/u)) {
    const section = /^\[([^\]]+)\]\s*$/u.exec(line);
    if (section) {
      inProject = section[1] === "project";
      continue;
    }
    if (!inProject) continue;
    const version = /^version\s*=\s*"([^"]+)"\s*$/u.exec(line);
    if (version) return version[1];
  }
  assert.fail("missing [project].version in pyproject.toml");
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

test("README Diagnosis Skill versions follow the generator and manifest source constants", () => {
  const readme = fs.readFileSync(path.join(REPO_ROOT, "README.md"), "utf8");
  const generator = fs.readFileSync(path.join(
    REPO_ROOT,
    ".claude",
    "skills",
    "wiki-to-diagnosis-skill",
    "scripts",
    "generate_diagnosis_skill.py",
  ), "utf8");
  const verification = fs.readFileSync(path.join(
    REPO_ROOT,
    "src",
    "problem_locator",
    "runtime",
    "verification_contract.py",
  ), "utf8");
  const limits = fs.readFileSync(path.join(
    REPO_ROOT,
    "src",
    "problem_locator",
    "contracts",
    "limits.py",
  ), "utf8");
  const pyproject = fs.readFileSync(path.join(REPO_ROOT, "pyproject.toml"), "utf8");
  const generatorVersion = pythonConstant(generator, "GENERATOR_VERSION");
  const specVersion = pythonConstant(generator, "SPEC_SCHEMA_VERSION");
  const manifestVersion = pythonConstant(verification, "MANIFEST_SCHEMA_VERSION");
  const verificationVersion = pythonConstant(verification, "VERIFICATION_CONTRACT_SCHEMA_VERSION");
  const stateVersion = pythonConstant(limits, "SCHEMA_VERSION");
  const packageVersion = projectVersion(pyproject);
  const packageMajor = packageVersion.split(".", 1)[0];
  assert.match(packageVersion, /^[1-9][0-9]*\.[0-9]+\.[0-9]+$/u);
  const top = readme.split(/\r?\n/u).slice(0, 20);

  assert.ok(top.includes(`# Problem Locator V${packageMajor}`));
  assert.ok(top.includes(`| Problem Locator package | \`${packageVersion}\` |`));
  assert.ok(top.includes(`| State / Job / Outcome schema | \`${stateVersion}\` |`));
  assert.ok(top.includes(`## Diagnosis Skill v${manifestVersion}`));
  assert.ok(top.includes(`| GenerationSpec | \`v${specVersion}\` |`));
  assert.ok(top.includes(`| Diagnosis Skill generator / 生成 Skill | \`${generatorVersion}\` |`));
  assert.ok(top.includes(`| Diagnosis Skill manifest | \`${manifestVersion}\`（\`verification_contract.schema_version=${verificationVersion}\`） |`));

  for (const [expression, expected] of [
    [/当前行为以 v([0-9]+) generator、manifest/u, manifestVersion],
    [/^V([0-9]+) 使用本地 JSON 状态文件/mu, packageMajor],
    [/^运行时限制[^\n]+V([0-9]+) 会拒绝/mu, packageMajor],
    [/^## V([0-9]+) 机器校验、盲审与审计包$/mu, packageMajor],
    [/只接受当前 V([0-9]+) State\/Job\/Outcome 闭包/u, stateVersion],
    [/^V([0-9]+) 不包含 PostgreSQL/mu, packageMajor],
    [/^- V([0-9]+) 面向可信用户/mu, packageMajor],
    [/^- V([0-9]+) 的并发数固定为/mu, packageMajor],
  ]) {
    const match = expression.exec(readme);
    assert.ok(match, `README is missing current-version statement: ${expression}`);
    assert.equal(match[1], expected, `README current-version statement drifted: ${expression}`);
  }
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

test("Test Flow README pins the compact IR batch, compiler closure, DONE and retry boundary", () => {
  const readme = fs.readFileSync(path.join(REPO_ROOT, "tools", "test-flow", "README.md"), "utf8");
  assert.match(readme, /Skill\/Read\/StructuredOutput/);
  assert.match(readme, /Wiki 与澄清必须在同一 assistant response/);
  assert.match(readme, /ASCII `DONE`/);
  assert.match(readme, /`GenerationBlueprint` v1/);
  assert.match(readme, /不超过 48 KiB/);
  assert.match(readme, /144 条/);
  assert.match(readme, /2\/10\/165\/9 GenerationSpec/);
  assert.match(readme, /IR\/compiler\/output/);
  assert.match(readme, /isolated-agent-env-allowlist-v3/);
  assert.match(readme, /MAX_STRUCTURED_OUTPUT_RETRIES=2/);
  assert.doesNotMatch(readme, /isolated-agent-env-allowlist-v2/);
  assert.doesNotMatch(readme, /skill-generation-tool-attempts-v4/);
  assert.doesNotMatch(readme, /Read,Write,Skill|Edit\(\/output\/generation-spec\.json\)/);
});
