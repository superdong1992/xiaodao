const pptxgen = require("pptxgenjs");
const path = require("path");

async function main() {
  const pptx = new pptxgen();
  pptx.layout = "LAYOUT_WIDE";
  pptx.author = "OpenAI Codex";
  pptx.company = "Huawei";
  pptx.subject = "OpenSRE 四页介绍";
  pptx.title = "OpenSRE：开源的线上故障定位 Agent 框架";
  pptx.lang = "zh-CN";
  pptx.theme = {
    headFontFace: "Microsoft YaHei UI",
    bodyFontFace: "Microsoft YaHei UI",
    lang: "zh-CN",
  };

  const reviewDir = __dirname;
  const images = [5, 6, 7, 8].map((n) =>
    path.join(reviewDir, `slide-${String(n).padStart(2, "0")}-3840x2160.png`)
  );

  for (const imagePath of images) {
    const slide = pptx.addSlide();
    slide.background = { color: "FFFFFF" };
    slide.addImage({ path: imagePath, x: 0, y: 0, w: 13.333333, h: 7.5 });
  }

  const outputPath = path.join(
    "D:\\code\\xiaodao\\doc\\problem-locator-open-source-insight-ppt",
    "OpenSRE-四页介绍.pptx"
  );

  await pptx.writeFile({ fileName: outputPath });
  console.log(outputPath);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
