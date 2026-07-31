import fs from "node:fs";
import path from "node:path";
import { chromium } from "file:///C:/Users/admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs";

const root = "D:/code/xiaodao";
const deckPath = path.join(
  root,
  "doc/problem-locator-open-source-insight-ppt/problem-locator-agent-insight.html",
);
const expectedProjects = [
  ...Array(4).fill("opensre"),
  ...Array(5).fill("derisk"),
  ...Array(3).fill("langgraph"),
  ...Array(3).fill("openhands"),
  ...Array(3).fill("cline"),
  ...Array(3).fill("autogen"),
  ...Array(3).fill("crewai"),
  ...Array(3).fill("aider"),
  ...Array(3).fill("sweagent"),
  ...Array(3).fill("miniswe"),
];
const rpcPages = new Set([4, 8, 11, 14, 17, 20, 23, 26, 29, 32]);

const html = fs.readFileSync(deckPath, "utf8");
const browser = await chromium.launch({
  headless: true,
  executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
});
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
await page.goto(`file:///${deckPath.replaceAll("\\", "/")}`, {
  waitUntil: "networkidle",
});

const audit = await page.evaluate(({ expectedProjects, rpcPages }) => {
  const errors = [];
  const slides = [...document.querySelectorAll("#deck > .slide")];
  if (slides.length !== 33) errors.push(`expected 33 slides, got ${slides.length}`);

  const projects = slides.map((slide) => slide.dataset.project);
  if (projects.join("|") !== expectedProjects.join("|")) {
    errors.push(`project sequence mismatch: ${projects.join(",")}`);
  }

  slides.forEach((slide, index) => {
    const pageNumber = index + 1;
    const title = slide.querySelector(".insight-title");
    if (title) {
      const range = document.createRange();
      range.selectNodeContents(title);
      const lineRects = [...range.getClientRects()].filter(
        (rect) => rect.width > 2 && rect.height > 2,
      );
      if (
        lineRects.length > 1 &&
        lineRects.at(-1).width < lineRects[0].width * 0.28
      ) {
        errors.push(`slide ${pageNumber}: orphaned title line`);
      }
    }
    const links = [...slide.querySelectorAll(".source-links a")].map(
      (anchor) => anchor.href,
    );
    const fixedBlobLinks = links.filter((href) =>
      /github\.com\/[^/]+\/[^/]+\/blob\/[0-9a-f]{40}\//i.test(href),
    );
    if (!fixedBlobLinks.length) {
      errors.push(`slide ${pageNumber}: no fixed-SHA source link`);
    }
    for (const href of links) {
      if (/github\.com\/.+\/blob\//i.test(href) && !/\/blob\/[0-9a-f]{40}\//i.test(href)) {
        errors.push(`slide ${pageNumber}: floating GitHub blob link ${href}`);
      }
    }
    if (rpcPages.includes(pageNumber)) {
      const boundary = slide.querySelector(".boundary-bar")?.textContent || "";
      if (!boundary || !/(非官方|机制映射|需补充|工具适配|演示场景)/.test(boundary)) {
        errors.push(`slide ${pageNumber}: RPC boundary is missing or unclear`);
      }
    }
  });

  const deckText = slides.map((slide) => slide.textContent || "").join("\n");
  if (/剧本/.test(deckText)) errors.push("forbidden wording: 剧本");
  if (/(TODO|TBD|PLACEHOLDER|待补充)/i.test(deckText)) {
    errors.push("placeholder wording remains");
  }
  return {
    errors,
    slides: slides.length,
    projects,
    sourceLinks: document.querySelectorAll(".source-links a").length,
  };
}, { expectedProjects, rpcPages: [...rpcPages] });

await browser.close();

const rawBlobLinks = [...html.matchAll(/href="([^"]*github\.com\/[^"]*\/blob\/[^"]+)"/gi)]
  .map((match) => match[1]);
const floating = rawBlobLinks.filter(
  (href) => !/\/blob\/[0-9a-f]{40}\//i.test(href),
);
if (floating.length) {
  audit.errors.push(`raw HTML contains ${floating.length} floating blob link(s)`);
}

console.log(JSON.stringify(audit, null, 2));
if (audit.errors.length) process.exitCode = 1;
