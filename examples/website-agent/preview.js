import { renderReport } from "./report-view.js";

const report = document.querySelector("#report");
const scenarioButtons = document.querySelector("#scenarios");
const responseText = document.querySelector("#response");
const note = document.querySelector("#scenario-note");

try {
  // 本地预览只加载一次合成数据；切换场景不会发请求。
  const response = await fetch("/sample-data.json");
  if (!response.ok) throw new Error("无法读取预览数据，请重新启动 preview.mjs。");
  const samples = await response.json();
  function show(sample, selected) {
    renderReport(report, sample.response.data);
    responseText.textContent = JSON.stringify(sample.response, null, 2);
    note.textContent = sample.note;
    for (const button of scenarioButtons.children) button.setAttribute("aria-pressed", String(button === selected));
  }
  for (const sample of samples) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = sample.label;
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", () => show(sample, button));
    scenarioButtons.append(button);
  }
  show(samples[0], scenarioButtons.firstElementChild);
} catch (error) {
  note.textContent = error.message;
}
