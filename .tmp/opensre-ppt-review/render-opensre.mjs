import path from "node:path";
import { pathToFileURL } from "node:url";
import { chromium } from "file:///C:/Users/admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs";

const root = "D:/code/xiaodao";
const htmlPath = path.join(
  root,
  "doc/problem-locator-open-source-insight-ppt/index.html",
);
const outputDir = path.join(root, ".tmp/opensre-ppt-review");

const browser = await chromium.launch({
  headless: true,
  executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
});
const page = await browser.newPage({
  viewport: { width: 1920, height: 1080 },
  deviceScaleFactor: 2,
});

await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "load" });
await page.evaluate(() => {
  window.__lowPowerMode = true;
  document.body.classList.add("low-power");
});

const results = [];
for (const slideNumber of [5, 6, 7, 8]) {
  await page.evaluate((targetSlide) => {
    document.querySelector(`#nav button:nth-child(${targetSlide})`)?.click();
  }, slideNumber);
  await page.waitForTimeout(80);
  await page.evaluate(
    ({ standaloneIndex }) => {
      const currentPageNo = document.querySelector(".slide.active .page-no");
      if (currentPageNo) {
        currentPageNo.textContent = `${String(standaloneIndex).padStart(2, "0")} / 04`;
      }

      const navButtons = [...document.querySelectorAll("#nav button")];
      navButtons.forEach((button, index) => {
        button.style.display = index < 4 ? "block" : "none";
        button.classList.toggle("active", index === standaloneIndex - 1);
        if (index < 4) {
          button.setAttribute("aria-label", `Go to OpenSRE slide ${index + 1}`);
        }
      });
    },
    { standaloneIndex: slideNumber - 4 },
  );
  const slide = page.locator(`.slide:nth-child(${slideNumber})`);
  const layout = await slide.getAttribute("data-layout");
  const overflows = await slide.evaluate((root) => {
    const viewport = { width: window.innerWidth, height: window.innerHeight };
    const issues = [];
    for (const el of root.querySelectorAll("*")) {
      const style = getComputedStyle(el);
      if (
        style.display === "none" ||
        style.visibility === "hidden" ||
        Number(style.opacity) === 0
      ) {
        continue;
      }
      const rect = el.getBoundingClientRect();
      if (rect.width < 1 || rect.height < 1) continue;
      const text = (el.textContent || "").trim().replace(/\s+/g, " ");
      if (!text) continue;
      const clippedX = el.scrollWidth > el.clientWidth + 2;
      const clippedY = el.scrollHeight > el.clientHeight + 2;
      const outside =
        rect.left < -2 ||
        rect.right > viewport.width + 2 ||
        rect.top < -2 ||
        rect.bottom > viewport.height + 2;
      if (clippedX || clippedY || outside) {
        issues.push({
          tag: el.tagName,
          className: el.className,
          text: text.slice(0, 90),
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
    return issues.slice(0, 40);
  });
  const file = path.join(
    outputDir,
    `slide-${String(slideNumber).padStart(2, "0")}-3840x2160.png`,
  );
  await page.screenshot({ path: file });
  results.push({ slideNumber, layout, file, overflows });
}

console.log(JSON.stringify(results, null, 2));
await browser.close();
