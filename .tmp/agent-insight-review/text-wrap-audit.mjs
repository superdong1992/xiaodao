import path from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "file:///C:/Users/admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs";

const htmlPath =
  "D:/code/xiaodao/doc/problem-locator-open-source-insight-ppt/problem-locator-agent-insight.html";
const browser = await chromium.launch({
  headless: true,
  executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
});
const findings = [];

for (const viewport of [
  { width: 1920, height: 1080, label: "1920x1080" },
  { width: 1366, height: 768, label: "1366x768" },
]) {
  const page = await browser.newPage({ viewport });
  await page.goto(pathToFileURL(path.resolve(htmlPath)).href, {
    waitUntil: "load",
  });
  const viewportFindings = await page.evaluate(() => {
    const output = [];
    const tokenPattern =
      /[A-Za-z][A-Za-z0-9_.]*(?:-[A-Za-z0-9_.]+)+|[A-Za-z][A-Za-z0-9.]*_[A-Za-z0-9_.]+/g;
    document.querySelectorAll(".slide").forEach((slide, slideIndex) => {
      const walker = document.createTreeWalker(
        slide,
        NodeFilter.SHOW_TEXT,
        {
          acceptNode(node) {
            const parent = node.parentElement;
            if (!parent || parent.closest(".source-links")) {
              return NodeFilter.FILTER_REJECT;
            }
            return NodeFilter.FILTER_ACCEPT;
          },
        },
      );
      while (walker.nextNode()) {
        const node = walker.currentNode;
        const text = node.textContent || "";
        for (const match of text.matchAll(tokenPattern)) {
          const range = document.createRange();
          range.setStart(node, match.index);
          range.setEnd(node, match.index + match[0].length);
          const tops = [
            ...new Set(
              [...range.getClientRects()].map((rect) => Math.round(rect.top)),
            ),
          ];
          if (tops.length > 1) {
            output.push({
              slide: slideIndex + 1,
              token: match[0],
              tops,
            });
          }
        }
      }
    });
    return output;
  });
  findings.push(...viewportFindings.map((item) => ({ ...item, viewport: viewport.label })));
  await page.close();
}

await browser.close();
console.log(JSON.stringify({ findings }, null, 2));
if (findings.length) process.exitCode = 1;
