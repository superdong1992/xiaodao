import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";

const root = "D:/code/xiaodao";
const html = fs.readFileSync(
  path.join(
    root,
    "doc/problem-locator-open-source-insight-ppt/problem-locator-agent-insight.html",
  ),
  "utf8",
);
const localDirs = new Map([
  ["Tracer-Cloud/opensre", "opensre"],
  ["derisk-ai/OpenDerisk", "OpenDerisk"],
  ["langchain-ai/langgraph", "langgraph"],
  ["OpenHands/OpenHands", "OpenHands"],
  ["cline/cline", "cline"],
  ["microsoft/autogen", "autogen"],
  ["crewAIInc/crewAI", "crewAI"],
  ["Aider-AI/aider", "aider"],
  ["SWE-agent/SWE-agent", "SWE-agent"],
  ["SWE-agent/mini-swe-agent", "mini-swe-agent"],
]);

const links = [
  ...new Set(
    [...html.matchAll(/href="(https:\/\/github\.com\/([^/]+\/[^/]+)\/blob\/([0-9a-f]{40})\/([^"#?]+)(?:#L(\d+)(?:-L(\d+))?)?)"/gi)]
      .map((match) => ({
        href: match[1],
        repo: match[2],
        sha: match[3],
        file: decodeURIComponent(match[4]),
        startLine: match[5] ? Number(match[5]) : null,
        endLine: match[6] ? Number(match[6]) : match[5] ? Number(match[5]) : null,
      }))
      .map((item) => JSON.stringify(item)),
  ),
].map((item) => JSON.parse(item));

const treeCache = new Map();
const lineCountCache = new Map();
const missing = [];
for (const link of links) {
  const localName = localDirs.get(link.repo);
  if (!localName) {
    missing.push({ ...link, reason: "no local baseline mapping" });
    continue;
  }
  const cacheKey = `${localName}:${link.sha}`;
  if (!treeCache.has(cacheKey)) {
    const cwd = path.join(root, ".tmp/open-source-baselines", localName);
    const output = execFileSync(
      "git",
      [
        "-c",
        `safe.directory=${cwd.replaceAll("\\", "/")}`,
        "ls-tree",
        "-r",
        "--name-only",
        link.sha,
      ],
      { cwd, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 },
    );
    treeCache.set(
      cacheKey,
      new Set(output.split(/\r?\n/).filter(Boolean)),
    );
  }
  if (!treeCache.get(cacheKey).has(link.file)) {
    missing.push({ ...link, reason: "path absent from pinned tree" });
    continue;
  }
  if (link.endLine !== null) {
    const lineCacheKey = `${cacheKey}:${link.file}`;
    if (!lineCountCache.has(lineCacheKey)) {
      const cwd = path.join(root, ".tmp/open-source-baselines", localName);
      let content;
      try {
        content = fs.readFileSync(path.join(cwd, link.file), "utf8");
      } catch (error) {
        if (error?.code !== "ENOENT") throw error;
        content = execFileSync(
          "git",
          [
            "-c",
            `safe.directory=${cwd.replaceAll("\\", "/")}`,
            "show",
            `${link.sha}:${link.file}`,
          ],
          { cwd, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 },
        );
      }
      const lines = content.split(/\r?\n/);
      if (lines.at(-1) === "") lines.pop();
      lineCountCache.set(lineCacheKey, lines.length);
    }
    const lineCount = lineCountCache.get(lineCacheKey);
    if (link.startLine < 1 || link.endLine < link.startLine || link.endLine > lineCount) {
      missing.push({
        ...link,
        lineCount,
        reason: "line anchor exceeds pinned file bounds",
      });
    }
  }
}

const result = {
  uniqueFixedGithubLinks: links.length,
  pinnedTrees: treeCache.size,
  missing,
};
console.log(JSON.stringify(result, null, 2));
if (missing.length) process.exitCode = 1;
