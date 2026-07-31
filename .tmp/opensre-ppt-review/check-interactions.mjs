import { pathToFileURL } from "node:url";
import { chromium } from "file:///C:/Users/admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs";

const browser = await chromium.launch({
  headless: true,
  executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe",
});
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
await page.goto(
  pathToFileURL(
    "D:/code/xiaodao/doc/problem-locator-open-source-insight-ppt/index.html",
  ).href,
  { waitUntil: "load" },
);

const navCount = await page.locator("#nav button").count();
await page.locator("#nav button:nth-child(5)").click();
await page.waitForTimeout(800);
const beforeArrow = await page
  .locator("#deck > .slide.active .page-no")
  .textContent();
await page.keyboard.press("ArrowRight");
await page.waitForTimeout(800);
const afterArrow = await page
  .locator("#deck > .slide.active .page-no")
  .textContent();
await page.mouse.wheel(0, 900);
await page.waitForTimeout(800);
const afterWheel = await page
  .locator("#deck > .slide.active .page-no")
  .textContent();
const lowPowerBefore = await page.locator("body").evaluate((el) =>
  el.classList.contains("low-power"),
);
await page.keyboard.press("b");
const lowPowerAfter = await page.locator("body").evaluate((el) =>
  el.classList.contains("low-power"),
);
await page.keyboard.press("Escape");
const overviewAfterEscape = await page.locator("body").evaluate((el) =>
  el.classList.contains("overview"),
);

console.log(
  JSON.stringify(
    {
      navCount,
      beforeArrow,
      afterArrow,
      afterWheel,
      lowPowerToggled: lowPowerBefore !== lowPowerAfter,
      overviewAfterEscape,
    },
    null,
    2,
  ),
);
await browser.close();
