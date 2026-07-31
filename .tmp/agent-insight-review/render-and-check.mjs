import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "file:///C:/Users/admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs";

const root = "D:/code/xiaodao";
const htmlPath = path.join(
  root,
  "doc/problem-locator-open-source-insight-ppt/problem-locator-agent-insight.html",
);
const outputDir = path.join(root, ".tmp/agent-insight-review");
fs.mkdirSync(outputDir, { recursive: true });

const browser = await chromium.launch({
  headless: true,
  executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
});

const allResults = [];
for (const viewport of [
  { width: 1920, height: 1080, label: "1920x1080" },
  { width: 1366, height: 768, label: "1366x768" },
]) {
  const page = await browser.newPage({
    viewport: { width: viewport.width, height: viewport.height },
    deviceScaleFactor: 1,
  });
  await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "load" });
  await page.evaluate(() => {
    window.__lowPowerMode = true;
    document.body.classList.add("low-power");
  });
  const slideCount = await page.locator("#deck > .slide").count();
  for (let index = 0; index < slideCount; index += 1) {
    await page.evaluate((target) => {
      document.querySelector(`#nav button:nth-child(${target + 1})`)?.click();
    }, index);
    await page.waitForTimeout(25);
    const slide = page.locator(`#deck > .slide:nth-child(${index + 1})`);
    const issues = await slide.evaluate((rootElement) => {
      const issueList = [];
      const rootRect = rootElement.getBoundingClientRect();
      for (const element of rootElement.querySelectorAll("*")) {
        // Variable-font ascenders make H2 scrollHeight a few pixels taller than
        // its painted box even when no glyph is visually clipped.
        if (element.classList.contains("insight-title")) continue;
        const style = getComputedStyle(element);
        if (
          style.display === "none" ||
          style.visibility === "hidden" ||
          Number(style.opacity) === 0
        ) {
          continue;
        }
        const rect = element.getBoundingClientRect();
        if (rect.width < 1 || rect.height < 1) continue;
        const text = (element.textContent || "").trim().replace(/\s+/g, " ");
        if (!text) continue;
        const clippedX = element.scrollWidth > element.clientWidth + 4;
        const clippedY = element.scrollHeight > element.clientHeight + 4;
        const outside =
          rect.left < rootRect.left - 2 ||
          rect.right > rootRect.right + 2 ||
          rect.top < rootRect.top - 2 ||
          rect.bottom > rootRect.bottom + 2;
        if (clippedX || clippedY || outside) {
          issueList.push({
            tag: element.tagName,
            className:
              typeof element.className === "string" ? element.className : "",
            text: text.slice(0, 100),
            clippedX,
            clippedY,
            outside,
            rect: {
              x: Math.round(rect.x),
              y: Math.round(rect.y),
              width: Math.round(rect.width),
              height: Math.round(rect.height),
            },
          });
        }
      }
      return issueList.slice(0, 60);
    });
    const file = path.join(
      outputDir,
      `${viewport.label}-slide-${String(index + 1).padStart(2, "0")}.png`,
    );
    await page.screenshot({ path: file });
    allResults.push({
      viewport: viewport.label,
      slide: index + 1,
      project: await slide.getAttribute("data-project"),
      layout: await slide.getAttribute("data-layout"),
      issues,
      file,
    });
  }
  await page.close();
}

const interactionPage = await browser.newPage({
  viewport: { width: 1920, height: 1080 },
});
await interactionPage.goto(pathToFileURL(htmlPath).href, { waitUntil: "load" });
const navCount = await interactionPage.locator("#nav button").count();
const activeNo = async () =>
  interactionPage.locator("#deck > .slide.active .page-no").textContent();
const interaction = { navCount, start: await activeNo() };
await interactionPage.keyboard.press("ArrowRight");
await interactionPage.waitForTimeout(780);
interaction.afterArrow = await activeNo();
await interactionPage.mouse.wheel(0, 800);
await interactionPage.waitForTimeout(780);
interaction.afterWheel = await activeNo();
const lowPowerBefore = await interactionPage
  .locator("body")
  .evaluate((element) => element.classList.contains("low-power"));
await interactionPage.keyboard.press("b");
const lowPowerAfter = await interactionPage
  .locator("body")
  .evaluate((element) => element.classList.contains("low-power"));
interaction.lowPowerToggled = lowPowerBefore !== lowPowerAfter;
await interactionPage.keyboard.press("Escape");
interaction.overviewAfterEscape = await interactionPage
  .locator("body")
  .evaluate((element) => element.classList.contains("overview"));
await interactionPage.close();

fs.writeFileSync(
  path.join(outputDir, "render-results.json"),
  JSON.stringify({ interaction, slides: allResults }, null, 2),
  "utf8",
);
console.log(
  JSON.stringify({
    interaction,
    screenshots: allResults.length,
    slidesWithIssues: allResults.filter((item) => item.issues.length).length,
  }),
);
await browser.close();
