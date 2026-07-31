import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const workspace = path.resolve(here, "..", "..");
const templatePath =
  process.env.PPT_TEMPLATE ||
  "C:/Users/admin/.codex/skills/guizang-ppt-skill/assets/template-huawei.html";
const lucidePath =
  process.env.PPT_LUCIDE ||
  "C:/Users/admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/lucide/dist/umd/lucide.min.js";
const outputPath = path.join(here, "problem-locator-agent-insight.html");
const ledgerPath = path.join(workspace, ".tmp", "open-source-agent-evidence.md");
const investigatedAt = "2026-07-30";

const projects = {
  opensre: {
    name: "OpenSRE",
    repo: "Tracer-Cloud/opensre",
    sha: "400972f2b97ca5b8ff8dc1e093a7263626def2b7",
    branch: "main",
    license: "Apache-2.0",
    pages: 4,
    primary: "/root/derisk_content_draft",
    reviewer: "/root/opensre_cn_review_r9",
  },
  derisk: {
    name: "OpenDerisk",
    repo: "derisk-ai/OpenDerisk",
    sha: "3194987c1f42c9227d21d4578ee3c98408908dfe",
    branch: "main",
    license: "MIT",
    pages: 5,
    primary: "/root/derisk_content_draft",
    reviewer: "/root/opensre_unified_cn",
  },
  langgraph: {
    name: "LangGraph",
    repo: "langchain-ai/langgraph",
    sha: "41341457342327166d72fc11952ab28fb61ec0bf",
    branch: "main",
    license: "MIT",
    pages: 3,
    primary: "/root/derisk_content_draft",
    reviewer: "/root/agent_refs_content",
  },
  openhands: {
    name: "OpenHands",
    repo: "OpenHands/OpenHands",
    sha: "fe693b29b0f4a67c7fb77cde6ed2416c7df84715",
    branch: "main",
    license: "MIT",
    pages: 3,
    primary: "/root/derisk_content_draft",
    reviewer: "/root/agent_refs_content",
  },
  cline: {
    name: "Cline",
    repo: "cline/cline",
    sha: "d2b674bb9df7b38024878128dae666adbf6e900a",
    branch: "main",
    license: "Apache-2.0",
    pages: 3,
    primary: "/root/agent_refs_content",
    reviewer: "/root/opensre_cn_review_r9",
  },
  autogen: {
    name: "AutoGen",
    repo: "microsoft/autogen",
    sha: "027ecf0a379bcc1d09956d46d12d44a3ad9cee14",
    branch: "main",
    license: "代码 MIT / 文档 CC BY 4.0",
    pages: 3,
    primary: "/root/agent_refs_content",
    reviewer: "/root/opensre_unified_cn",
  },
  crewai: {
    name: "CrewAI",
    repo: "crewAIInc/crewAI",
    sha: "112762a7faf4803cf8e5606b60be96373e17660a",
    branch: "main",
    license: "MIT",
    pages: 3,
    primary: "/root/agent_refs_content",
    reviewer: "/root/opensre_unified_cn",
  },
  aider: {
    name: "Aider",
    repo: "Aider-AI/aider",
    sha: "5dc9490bb35f9729ef2c95d00a19ccd30c26339c",
    branch: "main",
    license: "Apache-2.0",
    pages: 3,
    primary: "/root/opensre_fact_review_r9",
    reviewer: "/root/opensre_cn_review_r9",
  },
  sweagent: {
    name: "SWE-agent",
    repo: "SWE-agent/SWE-agent",
    sha: "3ea751c087f32b16e039a2233dd6eefecef325d5",
    branch: "main",
    license: "MIT",
    pages: 3,
    primary: "/root/opensre_fact_review_r9",
    reviewer: "/root/opensre_cn_review_r9",
  },
  miniswe: {
    name: "mini-SWE-agent",
    repo: "SWE-agent/mini-swe-agent",
    sha: "a83fcae82d2a08f0ee0c688f9d137b3566c097f8",
    branch: "main",
    license: "MIT",
    pages: 3,
    primary: "/root/opensre_fact_review_r9",
    reviewer: "/root/opensre_unified_cn",
  },
};

const escapeHtml = (value) =>
  String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");

const ref = (projectKey, file, lines, label, evidence = "代码可核") => {
  const project = projects[projectKey];
  const anchor = lines ? `#L${lines.replace("-", "-L")}` : "";
  return {
    projectKey,
    file,
    lines,
    label,
    evidence,
    href: `https://github.com/${project.repo}/blob/${project.sha}/${file}${anchor}`,
  };
};

const externalRef = (projectKey, href, label, evidence) => ({
  projectKey,
  file: href,
  lines: "",
  label,
  evidence,
  href,
});

const claim = (text, sourceRef, type = sourceRef.evidence, symbol = "") => ({
  text,
  sourceRef,
  type,
  symbol,
});

const brand = `
  <div class="brand-lockup" data-anim>
    <span class="brand-mark" aria-hidden="true"></span>
    <span>HUAWEI</span>
  </div>`;

const chrome = (project, index, total, section) => `
  <div class="chrome" data-anim>
    <div class="l"><span class="square"></span><span>${escapeHtml(project)} · ${String(index).padStart(2, "0")} / ${String(total).padStart(2, "0")}</span></div>
    <span>${escapeHtml(section)}</span>
  </div>`;

const evidenceLabel = (type) => {
  if (type === "代码可核") return "CODE";
  if (type === "文档陈述") return "DOC";
  if (type === "论文陈述") return "PAPER";
  if (type === "机制映射") return "MAPPING";
  return "UNCONFIRMED";
};

const sourceFooter = (projectKey, refs) => {
  const project = projects[projectKey];
  const unique = [...new Map(refs.map((item) => [item.href, item])).values()];
  return `
    <div class="evidence-footer">
      <div class="baseline-chip">
        <b>${escapeHtml(project.name)}</b>
        <code>${project.sha.slice(0, 8)}</code>
        <span>${escapeHtml(project.license)}</span>
      </div>
      <div class="source-links">
        ${unique
          .map(
            (item) =>
              `<a href="${item.href}" target="_blank" rel="noreferrer"><em>${evidenceLabel(item.evidence)}</em>${escapeHtml(item.label)}</a>`,
          )
          .join("")}
      </div>
    </div>`;
};

const chip = (text, tone = "") =>
  `<span class="chip ${tone}">${escapeHtml(text)}</span>`;

const factCard = (number, title, body, tone = "") => `
  <article class="fact-card ${tone}" data-anim>
    <div class="fact-no">${number}</div>
    <div><h3>${title}</h3><p>${body}</p></div>
  </article>`;

const flow = (steps, compact = false) => `
  <div class="flow-row ${compact ? "compact" : ""}">
    ${steps
      .map(
        (step, index) => `
          <article class="flow-step ${step.tone || ""}" data-anim>
            <span>${String(index + 1).padStart(2, "0")}</span>
            <strong>${step.title}</strong>
            <p>${step.body}</p>
          </article>
          ${index < steps.length - 1 ? '<i data-lucide="arrow-right"></i>' : ""}
        `,
      )
      .join("")}
  </div>`;

const rpcInput = `
  <div class="rpc-input" data-anim>
    <div class="rpc-input-title"><span>统一输入</span><strong>同一组材料，只比较组织方式</strong></div>
    <div class="rpc-facts">
      <span><b>调用</b><code>order-service → inventory-service</code></span>
      <span><b>接口</b><code>/Inventory/Reserve</code></span>
      <span><b>现象</b>P99 3.2s / 阈值 800ms / 错误率 12%</span>
      <span><b>Trace</b><code>db.acquire = 3.02s</code></span>
      <span><b>变更</b>连接池上限 <code>80 → 8</code></span>
      <span><b>指标</b><code>active=8 · waiters=120</code></span>
    </div>
  </div>`;

const rpcBoundary = (nativeName = "该项目") => `
  <div class="boundary-bar" data-anim>
    <strong>边界</strong>
    <span>机制映射演示，并非官方案例</span>
    <span>${nativeName} 不原生提供生产环境 RPC 定位能力</span>
    <span>需接入 Trace、日志、指标、发布配置与数据库工具</span>
  </div>`;

const slides = [];
const ledgerRows = [];
const projectOrder = [
  "opensre",
  "derisk",
  "langgraph",
  "openhands",
  "cline",
  "autogen",
  "crewai",
  "aider",
  "sweagent",
  "miniswe",
];
const addSlide = ({
  projectKey,
  localPage,
  section,
  title,
  eyebrow,
  lede,
  body,
  refs,
  claims,
  layout = "H05",
}) => {
  const project = projects[projectKey];
  const globalPage = slides.length + 1;
  slides.push({ projectKey, localPage, html: `
    <section class="slide insight-slide ${projectKey}-slide ${globalPage === 1 ? "active" : ""}" data-layout="${layout}" data-project="${projectKey}">
      ${brand}
      ${chrome(project.name, localPage, project.pages, section)}
      <div class="insight-head">
        <div class="kicker" data-anim>${escapeHtml(eyebrow)}</div>
        <h2 class="insight-title" data-anim>${title}</h2>
        ${lede ? `<p class="insight-lede" data-anim>${lede}</p>` : ""}
      </div>
      <div class="insight-body">${body}</div>
      ${sourceFooter(projectKey, refs)}
    </section>` });

  claims.forEach((item, index) => {
    ledgerRows.push({
      id: `${projectKey.toUpperCase()}-${String(localPage).padStart(2, "0")}-${String(index + 1).padStart(2, "0")}`,
      project: project.name,
      commit: project.sha,
      projectKey,
      localPage,
      page: 0,
      claim: item.text,
      type: item.type,
      file: item.sourceRef.file,
      symbol: item.symbol,
      lines: item.sourceRef.lines,
      href: item.sourceRef.href,
      primary: project.primary,
      reviewer: project.reviewer,
      status:
        item.type === "未确认"
          ? "双重审查：已明确降级"
          : "双重独立代码审查通过",
    });
  });
};

const customCss = `
  :root{
    --paper:#f4f7fb;--paper-rgb:244,247,251;--ink:#20242c;--ink-rgb:32,36,44;
    --muted:#667080;--line:#dce3ea;--line-strong:#c1cad4;
    --brand-red:#d20a2e;--brand-red-rgb:210,10,46;--brand-red-dark:#9c001f;
    --brand-red-soft:#f8e5e9;--brand-black:#14171c;--brand-grey:#edf2f7;
  }
  .insight-slide{
    padding:8.3vh 6.4vw 6.9vh;
    display:grid;
    grid-template-rows:auto minmax(0,1fr) auto;
    gap:1.35vh;
  }
  .insight-slide::before{opacity:.16}
  .insight-slide::after{opacity:.08}
  .insight-slide .chrome{right:14vw}
  .insight-head{display:grid;gap:.65vh;align-content:start;min-height:0}
  .insight-title{
    font-family:var(--sans-zh);
    font-size:min(2.7vw,5vh);
    line-height:1.11;
    font-weight:850;
    letter-spacing:-.015em;
    max-width:89%;
  }
  .insight-lede{
    max-width:92%;
    color:#4c5665;
    font-size:1.03vw;
    line-height:1.48;
  }
  .insight-body{min-height:0;display:grid;gap:1.05vh;align-content:stretch}
  .insight-body p,.insight-body li{font-size:1vw;line-height:1.39;color:#46515f}
  .insight-body strong{color:var(--ink)}
  .insight-body code{font:inherit;font-family:var(--mono);font-size:.92em;color:#20242c}
  .nowrap{white-space:nowrap}
  .chip-row{display:flex;gap:.55vw;flex-wrap:wrap;align-items:center}
  .chip{
    display:inline-flex;align-items:center;min-height:2.55vh;padding:.28vh .58vw;
    background:#fff;border:1px solid var(--line-strong);font-size:.75vw;font-weight:750;color:#4b5664;
  }
  .chip.red{background:var(--brand-red);border-color:var(--brand-red);color:#fff}
  .chip.dark{background:var(--brand-black);border-color:var(--brand-black);color:#fff}
  .chip.soft{background:var(--brand-red-soft);border-color:#efbcc7;color:#9c001f}
  .definition-band{
    display:grid;grid-template-columns:8.4vw 1fr;align-items:stretch;
    min-height:8vh;background:var(--brand-black);color:#fff;
  }
  .definition-band > strong{
    display:grid;place-items:center;background:var(--brand-red);font-size:.92vw;
  }
  .definition-band > p{
    padding:1.15vh 1.15vw;display:flex;align-items:center;color:#fff;
    font-size:1.03vw;line-height:1.42;
  }
  .definition-band code{color:#fff}
  .fact-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:.85vw;min-height:0}
  .fact-grid.two{grid-template-columns:repeat(2,1fr)}
  .fact-grid.four{grid-template-columns:repeat(4,1fr)}
  .fact-card{
    min-height:0;padding:1.15vh .9vw;background:#fff;border:1px solid var(--line);
    border-top:4px solid var(--brand-black);display:grid;grid-template-columns:2.15vw 1fr;
    gap:.65vw;align-items:start;box-shadow:0 8px 20px rgba(15,23,42,.05);
  }
  .fact-card.red{border-top-color:var(--brand-red)}
  .fact-card.soft{background:#fff8f9;border-top-color:var(--brand-red)}
  .fact-no{
    width:2vw;height:2vw;display:grid;place-items:center;background:var(--brand-grey);
    color:var(--brand-red);font:800 .7vw var(--mono);
  }
  .fact-card h3{font-size:1.08vw;line-height:1.2;margin:.12vh 0 .45vh}
  .fact-card p{font-size:.95vw;line-height:1.36}
  .flow-row{
    display:grid;
    grid-template-columns:1.18fr .22fr 1fr .22fr 1fr .22fr 1fr .22fr 1fr .22fr 1fr;
    gap:.42vw;align-items:stretch;
  }
  .flow-row.compact{
    grid-template-columns:1fr .22fr 1fr .22fr 1fr .22fr 1fr .22fr 1fr;
  }
  .flow-row > .lucide{width:.8vw;height:.8vw;align-self:center;color:var(--brand-red)}
  .flow-step{
    min-height:12.4vh;padding:1vh .68vw;background:#fff;border:1px solid var(--line);
    border-top:4px solid var(--brand-black);display:grid;grid-template-rows:auto auto 1fr;gap:.42vh;
  }
  .flow-step.red{border-top-color:var(--brand-red);background:#fff8f9}
  .flow-step.black{background:var(--brand-black);color:#fff;border-color:var(--brand-black)}
  .flow-step > span{font:800 .64vw var(--mono);color:var(--brand-red)}
  .flow-step.black > span,.flow-step.black strong,.flow-step.black p{color:#fff}
  .flow-step strong{font-size:.98vw;line-height:1.18}
  .flow-step p{font-size:.91vw;line-height:1.3}
  .flow-step code{overflow-wrap:anywhere;word-break:break-word}
  .flow-step.black code{color:#fff}
  .split-grid{display:grid;grid-template-columns:1fr 1fr;gap:.9vw;min-height:0}
  .split-grid.wide-left{grid-template-columns:1.2fr .8fr}
  .split-grid.wide-right{grid-template-columns:.82fr 1.18fr}
  .panel{
    min-height:0;background:#fff;border:1px solid var(--line);padding:1.15vh .95vw;
    display:grid;align-content:start;gap:.75vh;
  }
  .panel.red{border-top:4px solid var(--brand-red)}
  .panel.dark{border-top:4px solid var(--brand-black)}
  .panel.soft{background:#fff8f9;border-top:4px solid var(--brand-red)}
  .panel-head{display:flex;align-items:center;justify-content:space-between;gap:.6vw}
  .panel-head h3{font-size:1.08vw;line-height:1.2}
  .panel-head span{font:800 .62vw var(--mono);color:var(--brand-red)}
  .clean-list{display:grid;gap:.52vh;list-style:none}
  .clean-list li{
    position:relative;padding-left:1.05vw;font-size:.95vw;line-height:1.35;
  }
  .clean-list li::before{
    content:"";position:absolute;left:0;top:.53em;width:.38vw;height:.38vw;background:var(--brand-red);
  }
  .clean-list li small{display:block;margin-top:.14vh;font-size:.78vw;color:var(--muted)}
  .formula{
    min-height:6.5vh;padding:.65vh .8vw;display:flex;align-items:center;justify-content:center;
    gap:.45vw;background:var(--brand-grey);border:1px solid var(--line);font-size:.92vw;font-weight:750;
  }
  .formula .lucide{width:.8vw;height:.8vw;color:var(--brand-red)}
  .runtime-loop{
    display:grid;grid-template-columns:1fr .72vw 1fr .72vw 1fr .72vw 1fr;gap:.42vw;align-items:stretch;
  }
  .runtime-loop > .lucide{width:.78vw;height:.78vw;align-self:center;color:var(--brand-red)}
  .loop-node{
    min-height:10vh;padding:.85vh .75vw;background:#fff;border:1px solid var(--line);
    border-left:4px solid var(--brand-black);display:grid;align-content:center;gap:.36vh;
  }
  .loop-node.red{border-left-color:var(--brand-red);background:#fff8f9}
  .loop-node strong{font-size:1.02vw}.loop-node p{font-size:.9vw;line-height:1.3}
  .return-arrow{
    min-height:4.2vh;padding:.45vh .8vw;background:var(--brand-grey);
    display:flex;align-items:center;justify-content:center;gap:.6vw;font-size:.82vw;color:#4f5966;
  }
  .return-arrow .lucide{width:.85vw;height:.85vw;color:var(--brand-red)}
  .control-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:.75vw}
  .control-card{padding:.8vh .72vw;background:#fff;border:1px solid var(--line);border-top:3px solid var(--brand-red)}
  .control-card strong{display:block;font-size:.96vw;margin-bottom:.28vh}
  .control-card p{font-size:.86vw;line-height:1.3}
  .rpc-input{
    display:grid;grid-template-columns:9.7vw 1fr;min-height:7.2vh;background:var(--brand-black);color:#fff;
  }
  .rpc-input-title{padding:.7vh .8vw;background:var(--brand-red);display:grid;align-content:center;gap:.18vh}
  .rpc-input-title span{font:800 .58vw var(--mono);opacity:.8}
  .rpc-input-title strong{font-size:.83vw;color:#fff}
  .rpc-facts{padding:.62vh .8vw;display:grid;grid-template-columns:repeat(3,1fr);gap:.4vh .8vw;align-content:center}
  .rpc-facts span{font-size:.83vw;color:rgba(255,255,255,.92)}
  .rpc-facts b{margin-right:.38vw;color:#fff}
  .rpc-facts code{color:#fff}
  .rpc-lanes{display:grid;grid-template-columns:.76fr 1.24fr;gap:.82vw;min-height:0}
  .rpc-lane{padding:.9vh .82vw;background:#fff;border:1px solid var(--line);display:grid;align-content:start;gap:.6vh}
  .rpc-lane.code{border-top:4px solid var(--brand-black)}
  .rpc-lane.add{border-top:4px solid var(--brand-red);background:#fff8f9}
  .rpc-lane h3{font-size:1.02vw}
  .rpc-lane ol{padding-left:1.15vw;display:grid;gap:.38vh}
  .rpc-lane li{font-size:.9vw;line-height:1.32}
  .rpc-chain{display:grid;grid-template-columns:repeat(9,minmax(0,1fr));gap:.36vw;align-items:stretch}
  .rpc-chain > .lucide{width:.72vw;height:.72vw;align-self:center;color:var(--brand-red)}
  .rpc-chain article{
    min-height:6.4vh;padding:.55vh .48vw;background:#fff;border:1px solid var(--line);
    display:grid;align-content:center;gap:.18vh;text-align:center;
  }
  .rpc-chain article.red{background:var(--brand-red);border-color:var(--brand-red);color:#fff}
  .rpc-chain strong{font-size:.86vw}.rpc-chain span{font-size:.76vw;color:#606b78}
  .rpc-chain article.red strong,.rpc-chain article.red span{color:#fff}
  .boundary-bar{
    min-height:4.4vh;display:grid;grid-template-columns:5vw repeat(3,1fr);
    align-items:stretch;background:var(--brand-black);color:#fff;
  }
  .boundary-bar strong{display:grid;place-items:center;background:var(--brand-red);font-size:.76vw}
  .boundary-bar span{padding:.42vh .62vw;display:grid;place-items:center;text-align:center;border-right:1px solid rgba(255,255,255,.16);font-size:.76vw;line-height:1.2}
  .truth-table{display:grid;grid-template-columns:1fr 1fr;gap:.82vw;min-height:0}
  .truth-col{padding:1vh .9vw;background:#fff;border:1px solid var(--line);display:grid;align-content:start;gap:.65vh}
  .truth-col.confirmed{border-top:4px solid var(--brand-black)}
  .truth-col.limited{border-top:4px solid var(--brand-red);background:#fff8f9}
  .truth-col h3{font-size:1.05vw}
  .truth-row{display:grid;grid-template-columns:5.8vw 1fr;gap:.55vw;align-items:start;padding:.48vh 0;border-bottom:1px solid var(--line)}
  .truth-row:last-child{border-bottom:0}
  .truth-row b{font-size:.74vw;color:var(--brand-red)}
  .truth-row p{font-size:.92vw;line-height:1.32}
  .takeaway{
    min-height:6.2vh;padding:.72vh .9vw;background:var(--brand-black);color:#fff;
    display:grid;grid-template-columns:6.3vw 1fr;align-items:center;gap:.8vw;
  }
  .takeaway strong{height:100%;display:grid;place-items:center;background:var(--brand-red);font-size:.8vw;color:#fff}
  .takeaway p{color:#fff;font-size:.94vw;line-height:1.35}
  .evidence-footer{
    min-height:4.55vh;margin-bottom:-.15vh;display:grid;grid-template-columns:auto 1fr;
    gap:.65vw;align-items:center;border-top:1px solid var(--line);padding-top:.52vh;
  }
  .baseline-chip{display:flex;align-items:center;gap:.4vw;white-space:nowrap;font-size:max(.7vw,13px);color:var(--muted)}
  .baseline-chip b{color:var(--ink)}
  .baseline-chip code{padding:.18vh .34vw;background:var(--brand-black);color:#fff;font-size:max(.68vw,12px)}
  .source-links{display:flex;justify-content:flex-end;align-content:center;flex-wrap:wrap;gap:.22vh .35vw;min-width:0;overflow:visible}
  .source-links a{
    min-width:0;max-width:18vw;padding:.26vh .4vw;background:#fff;border:1px solid var(--line);
    color:#45515f;text-decoration:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:max(.68vw,13px);line-height:1.15;
  }
  .source-links em{
    margin-right:.3vw;color:var(--brand-red);font:800 max(.52vw,10px) var(--mono);font-style:normal;
  }
  .code-vs-model{display:grid;grid-template-columns:1fr .52vw 1fr;gap:.65vw;align-items:stretch}
  .code-vs-model > .lucide{width:.72vw;height:.72vw;align-self:center;color:var(--brand-red)}
  .role-card{padding:1vh .88vw;background:#fff;border:1px solid var(--line);border-top:4px solid var(--brand-black)}
  .role-card.model{border-top-color:var(--brand-red);background:#fff8f9}
  .role-card h3{font-size:1.02vw;margin-bottom:.55vh}
  .mini-matrix{display:grid;grid-template-columns:6.7vw 1fr 1fr 1fr;gap:1px;background:var(--line);border:1px solid var(--line)}
  .mini-matrix > *{padding:.55vh .55vw;background:#fff;font-size:.84vw;line-height:1.25}
  .mini-matrix .head{background:var(--brand-black);color:#fff;font-weight:800}
  .mini-matrix .rowhead{background:var(--brand-grey);font-weight:800}
  .note-strip{align-self:start;padding:.62vh .82vw;background:var(--brand-grey);border-left:4px solid var(--brand-red);font-size:.88vw;line-height:1.32;color:#4d5866}
  .h-stack{display:grid;gap:.7vh;align-content:start}
  .metric-row{display:grid;grid-template-columns:repeat(4,1fr);gap:.6vw}
  .metric{padding:.65vh .6vw;background:#fff;border:1px solid var(--line);text-align:center}
  .metric b{display:block;font:800 1.05vw var(--mono);color:var(--brand-red)}
  .metric span{font-size:.7vw;color:var(--muted)}
  #hint,.page-no,#nav{bottom:2.9vh}
  @media(max-width:900px){
    .insight-slide{padding:8.3vh 5vw 7vh}
    .insight-title{font-size:5vw}.insight-lede{font-size:1.9vw}
  }
`;

// ---------------------------------------------------------------------------
// 01–04 · OpenSRE
// ---------------------------------------------------------------------------

{
  const lifecycle = ref(
    "opensre",
    "tools/investigation/lifecycle.py",
    "27-67",
    "六阶段外层流程",
  );
  const agent = ref(
    "opensre",
    "tools/investigation/stages/gather_evidence/agent.py",
    "120-410",
    "ConnectedInvestigationAgent.run",
  );
  const evidence = ref(
    "opensre",
    "core/state/evidence.py",
    "10-21",
    "EvidenceEntry",
  );
  addSlide({
    projectKey: "opensre",
    localPage: 1,
    section: "项目定位",
    eyebrow: "CODE-EVIDENCED POSITIONING",
    title: "OpenSRE：把通用大模型工程化为线上故障定位 Agent",
    lede:
      "它不是新的基础模型，也不是一组固定故障脚本；核心是固定外层流水线与单个调查 Agent 的组合。",
    body: `
      <div class="definition-band" data-anim>
        <strong>核心定位</strong>
        <p>模型负责提出假设、选择下一查询并解释结果；OpenSRE 运行时负责限定工具、执行查询、记录证据、控制循环，并把结论交给外层报告流程。</p>
      </div>
      <div class="fact-grid">
        ${factCard("01", "接入既有运维系统", "通过工具连接日志、指标、Trace、发布配置、数据库与云平台；实际能力受已接入工具约束。", "red")}
        ${factCard("02", "Agent 动态排查", "不依赖固定流程的故障定位脚本；Agent 在获准使用的工具范围内提出假设并逐轮验证。")}
        ${factCard("03", "查询证据可追溯", "查询结果保存为证据溯源记录（<code>EvidenceEntry</code>），外层再整理 RCA 并交付报告。")}
      </div>
      <div class="chip-row" data-anim>
        ${chip("固定 Python 外层流程", "dark")}
        ${chip("当前主链：单 Agent", "red")}
        ${chip("通用大模型")}
        ${chip("已接入工具限定调查能力")}
        ${chip("Apache-2.0")}
      </div>`,
    refs: [lifecycle, agent, evidence],
    claims: [
      claim(
        "OpenSRE 用普通 Python 代码按固定顺序执行接入确认、告警解析、工具规划、Agent 排查、结论整理与报告交付。",
        lifecycle,
        "代码可核",
        "run_connected_investigation",
      ),
      claim(
        "ConnectedInvestigationAgent 是单 Agent 模型—工具循环，模型依据告警与当前证据决定下一工具和参数。",
        agent,
        "代码可核",
        "ConnectedInvestigationAgent.run",
      ),
      claim(
        "EvidenceEntry 记录工具、参数、来源、结果与调查轮次，但不等同于独立的证据充分性审核。",
        evidence,
        "代码可核",
        "EvidenceEntry",
      ),
    ],
  });
}

// ---------------------------------------------------------------------------
// 25–27 · Aider
// ---------------------------------------------------------------------------

{
  const loop = ref(
    "aider",
    "aider/coders/base_coder.py",
    "876-950",
    "Coder.run / run_one",
  );
  const send = ref(
    "aider",
    "aider/coders/base_coder.py",
    "1419-1617",
    "Coder.send_message",
  );
  const chunks = ref(
    "aider",
    "aider/coders/base_coder.py",
    "1260-1335",
    "模型输入分块",
  );
  const repoMap = ref(
    "aider",
    "aider/repomap.py",
    "365-704",
    "RepoMap",
  );
  const git = ref(
    "aider",
    "aider/repo.py",
    "131-230",
    "GitRepo.commit",
  );
  const summary = ref(
    "aider",
    "aider/history.py",
    "7-126",
    "ChatSummary",
  );
  const shell = ref(
    "aider",
    "aider/coders/base_coder.py",
    "2434-2482",
    "Shell 命令执行",
  );
  addReferenceProject({
    projectKey: "aider",
    mechanismTitle: "Aider：Git 承载工程事实，Repo Map 构造模型上下文",
    mechanismLede:
      "Aider 的强项是代码修改 Agent：聊天负责协作，Git 与文件保存结果，Repo Map 按 token 预算构造代码上下文。",
    mechanismSteps: [
      { title: "用户消息", body: "cur_messages / done_messages" },
      { title: "组装模型输入", body: "历史、Repo Map、文件与当前消息", tone: "red" },
      { title: "生成并应用编辑", body: "可执行 shell、lint、test" },
      { title: "错误反思", body: "受 max_reflections 限制", tone: "red" },
      { title: "Git 结果", body: "可选自动 commit 与哈希" },
    ],
    mechanismCards: [
      {
        title: "Repo Map 是代码相关性图",
        body: "从源码提取定义/引用，构造文件—符号图，并用 PageRank 结合聊天文件与标识符排序。",
      },
      {
        title: "Git 是工程状态载体",
        body: "模型修改可自动提交并记录哈希；Git 恢复代码结果，但不恢复进行中的模型调用。",
      },
      {
        title: "ChatSummary 是有损压缩",
        body: "用模型摘要旧历史并保留近期尾部；摘要不是逐条可审计的 Evidence。",
      },
    ],
    mechanismNote:
      "模型建议的 Shell 命令需要用户确认；用户还可以决定是否把输出加入聊天上下文。Repo Map 不是服务拓扑、Trace 或 CMDB。",
    mechanismRefs: [loop, send, chunks, repoMap, git, summary, shell],
    mechanismClaims: [
      claim(
        "Aider 主链是用户输入、模型生成编辑、应用修改、可选 lint/test 与错误反思重试。",
        loop,
        "代码可核",
        "Coder.run / run_one",
      ),
      claim(
        "模型输入按 system、历史、Repo Map、文件内容与当前消息分块组装。",
        chunks,
        "代码可核",
        "Coder.format_chat_chunks",
      ),
      claim(
        "RepoMap 提取定义和引用、构造文件—符号图并用 PageRank 排序代码上下文。",
        repoMap,
        "代码可核",
        "RepoMap.get_ranked_tags",
      ),
      claim(
        "ChatSummary 用模型摘要旧历史并保留近期消息，属于有损压缩。",
        summary,
        "代码可核",
        "ChatSummary",
      ),
    ],
    rpcTitle: "RPC 超时如何映射到 Aider 的 Chat、Repo Map 与 Git",
    rpcLede:
      "Aider 不是 SRE Agent；它更适合把运行时异常与相关配置代码、Git 变更连接起来。",
    codeProvides: [
      "模型—编辑—lint/test—反思的编码 Agent 循环。",
      "Repo Map 的相关代码上下文、Git diff/commit 与对话摘要。",
      "经用户确认后执行通用 Shell，并由用户决定是否把输出加入聊天。",
    ],
    rpcSteps: [
      "把 RPC 告警放入聊天；经用户确认后执行外部 Trace、日志和指标 CLI，并将观察结果加入上下文。",
      "当配置变更指向连接池上限 <code>80 → 8</code>，Repo Map 帮助定位相关配置定义与引用。",
      "Git diff/commit 定位连接池上限 80 → 8 对应的代码或配置变更；模型再核对该变更与 <code>db.acquire</code> 等待是否相关。",
      "结构化 Evidence、查询来源、时间窗与审核状态仍需外部 DiagnosisState。",
    ],
    rpcVerdict:
      "Aider 最有价值的是“把运行时线索追到代码/配置变更”；不能把 Repo Map 和 Git 误写成完整故障证据系统。",
    rpcRefs: [loop, repoMap, git, shell],
    rpcClaims: [
      claim(
        "该 RPC 流程通过经确认的 Shell/CLI 查询外部观测系统，并用 Repo Map 与 Git 关联代码配置。",
        shell,
        "机制映射",
        "Coder.run_shell_commands",
      ),
      claim(
        "Repo Map 可帮助定位相关配置代码，但不是运行时服务拓扑或调用链。",
        repoMap,
        "机制映射",
        "RepoMap",
      ),
      claim(
        "Git 记录工程变更，结构化 Evidence 和 RCA 审核仍需问题定位框架自行实现。",
        git,
        "机制映射",
        "GitRepo.commit",
      ),
    ],
    takeawaysTitle: "Aider 的启示：权威工程状态外置，模型输入可以重建",
    takeawaysLede:
      "Git 与文件承载可重建的工程事实；Repo Map 是从代码派生的模型输入视图。运行时诊断仍需独立状态和证据模型。",
    borrow: [
      "把代码、配置与变更事实留在 Git/文件中，不塞进“Agent 记忆”。",
      "按 token 预算生成相关代码地图，避免向模型注入整个仓库。",
      "Shell 执行保留人工确认，并明确是否将输出加入模型上下文。",
      "会话摘要只作为工作视图，原始工程结果由 Git 提供。",
    ],
    limits: [
      "Repo Map 是代码依赖近似图，不是服务拓扑、Trace 或 CMDB。",
      "Git 只恢复代码状态，不恢复 Agent 控制状态和外部查询。",
      "ChatSummary 是有损模型摘要，不保证逐条保留证据。",
      "Aider 没有 Incident、Hypothesis、Evidence 或 Diagnosis 领域对象。",
    ],
    verdict:
      "借鉴“Git/文件是权威工程事实、模型输入可重建”的原则；不要把聊天摘要或 Repo Map 升格为诊断事实层。",
    takeawayRefs: [repoMap, git, summary, chunks],
    takeawayClaims: [
      claim(
        "Aider 将 Git 和文件作为工程状态载体，模型输入可从历史、Repo Map 和文件重建。",
        chunks,
        "机制映射",
        "Coder.format_chat_chunks",
      ),
      claim(
        "Git 不能恢复进行中的模型调用、外部调查副作用或结构化诊断状态。",
        git,
        "机制映射",
        "GitRepo",
      ),
      claim(
        "ChatSummary 是有损工作上下文，不应作为唯一 Evidence。",
        summary,
        "机制映射",
        "ChatSummary",
      ),
    ],
  });
}

// ---------------------------------------------------------------------------
// 28–30 · SWE-agent
// ---------------------------------------------------------------------------

{
  const agent = ref(
    "sweagent",
    "sweagent/agent/agents.py",
    "930-1060",
    "DefaultAgent loop",
  );
  const messages = ref(
    "sweagent",
    "sweagent/agent/agents.py",
    "443-554",
    "history / messages",
  );
  const trajectory = ref(
    "sweagent",
    "sweagent/agent/agents.py",
    "762-789",
    "trajectory 保存",
  );
  const processors = ref(
    "sweagent",
    "sweagent/agent/history_processors.py",
    "74-337",
    "History Processors",
  );
  const env = ref(
    "sweagent",
    "sweagent/environment/swe_env.py",
    "51-276",
    "SWEEnv",
  );
  const replay = ref(
    "sweagent",
    "sweagent/run/run_replay.py",
    "1-171",
    "run-replay",
  );
  addReferenceProject({
    projectKey: "sweagent",
    mechanismTitle: "SWE-agent：保留原始 trajectory，只处理本轮模型输入",
    mechanismLede:
      "核心分层是原始 <code>history</code>、处理后 <code>messages</code>、详细 <code>trajectory</code> 与环境状态相互独立。",
    mechanismSteps: [
      { title: "原始 history", body: "完整 Action / Observation 历史" },
      { title: "History Processor", body: "构造本轮 messages", tone: "red" },
      { title: "模型与动作", body: "解析 action 并交给 SWEEnv" },
      { title: "观察回填", body: "写入 history 与 trajectory", tone: "red" },
      { title: "保存 .traj", body: "step 后落盘，可重放动作" },
    ],
    mechanismCards: [
      {
        title: "模型视图不改原始轨迹",
        body: "History Processor 只改变传给模型的 messages，原始 history 保留；处理器可链式组合。",
      },
      {
        title: "处理器不是统一摘要",
        body: "LastNObservations 用占位替换旧观察；ClosedWindow 压缩过时文件窗口；RemoveRegex 删除匹配内容。",
      },
      {
        title: "trajectory 可审计但非事务 Checkpoint",
        body: "记录 action、observation、response、thought、query、耗时和工具状态；当前 step 完成后才保存。",
      },
    ],
    mechanismNote:
      "<code>run-replay</code> 重新执行历史动作，主要用于演示、调试和工具测试；它不是从中断点恢复原模型推理。",
    mechanismRefs: [agent, messages, trajectory, processors, env, replay],
    mechanismClaims: [
      claim(
        "SWE-agent 默认循环处理历史、查询模型、解析动作、Shell 执行、观察回填并继续或提交。",
        agent,
        "代码可核",
        "DefaultAgent.forward / run",
      ),
      claim(
        "原始 history 与传给模型的处理后 messages 分离，History Processor 只改变模型视图。",
        messages,
        "代码可核",
        "DefaultAgent.messages",
      ),
      claim(
        "LastNObservations、ClosedWindowHistoryProcessor 与 RemoveRegex 进行占位、窗口压缩或删除，不是模型摘要。",
        processors,
        "代码可核",
        "HistoryProcessor",
      ),
      claim(
        "每步保存 .traj，但正在执行的命令或模型调用不是事务性 Checkpoint。",
        trajectory,
        "代码可核",
        "DefaultAgent.save_trajectory",
      ),
    ],
    rpcTitle: "RPC 超时如何映射到 trajectory 与可处理的模型历史",
    rpcLede:
      "SWE-agent 能完整记录模型—命令—观察轨迹；生产工具、DiagnosisState 与 Evidence 语义仍需外接。",
    codeProvides: [
      "模型—Shell—观察的循环，以及持续 SWEEnv 会话。",
      "原始 history、处理后 messages 与 .traj 分层。",
      "History Processor、动作重放和 Git patch 结果。",
    ],
    rpcSteps: [
      "将告警作为 problem statement，用 SWEEnv 执行 Trace/日志/指标/配置 CLI。",
      "每次观察进入 history 与 trajectory：记录 <code>db.acquire=3.02s</code>、连接获取超时和连接池变更。",
      "History Processor 可裁掉低价值旧观察，但原始 trajectory 继续保留。",
      "最终自然语言 RCA 需转换为外部结构化 Evidence；<code>run-replay</code> 不能视为崩溃恢复机制。",
    ],
    rpcVerdict:
      "最值得借鉴的是“原始轨迹不可丢、模型输入可裁剪”；Evidence 仍要从 trajectory 中抽取并进入领域状态。",
    rpcRefs: [agent, trajectory, processors, env],
    rpcClaims: [
      claim(
        "该 RPC 流程使用 SWEEnv 通用 Shell 连接外部观测 CLI，观察自动进入 history/trajectory。",
        env,
        "机制映射",
        "SWEEnv.communicate",
      ),
      claim(
        "History Processor 可缩小模型视图，但不修改原始 history，适合保留完整调查轨迹。",
        processors,
        "机制映射",
        "HistoryProcessor",
      ),
      claim(
        "结构化 DiagnosisState、Evidence 与 RCA 审核不是 SWE-agent 原生能力。",
        trajectory,
        "机制映射",
        "DefaultAgent.get_trajectory_data",
      ),
    ],
    takeawaysTitle: "SWE-agent 的启示：裁剪模型输入，保留原始执行轨迹",
    takeawaysLede:
      "“模型没看到”与“系统没保存”是两件事；上下文治理不能以牺牲审计记录为代价。",
    borrow: [
      "持久保存原始 action、observation、response 与工具状态。",
      "让 History Processor 只生成本轮模型视图，不回写原始 trajectory。",
      "按规则替换大块旧观察，而不是让摘要覆盖原事实。",
      "环境状态与模型消息分层，并记录命令超时和执行结果。",
    ],
    limits: [
      ".traj 在 step 完成后落盘，不是进行中调用的事务 Checkpoint。",
      "run-replay 重放动作，不恢复原模型推理或原运行时。",
      "Git diff/patch 是代码结果，不是诊断证据图。",
      "通用 Shell 可调用观测 CLI，但仓库没有原生 SRE 集成。",
    ],
    verdict:
      "采用“trajectory 持久保存 + model view 可替换”的双轨结构，再把经过验证的事实提升为独立 Evidence。",
    takeawayRefs: [messages, trajectory, processors, replay],
    takeawayClaims: [
      claim(
        "SWE-agent 的 History Processor 只处理模型消息视图，原始 history/trajectory 保留。",
        messages,
        "代码可核",
        "DefaultAgent.messages",
      ),
      claim(
        "trajectory 保存与动作 replay 不等于中断点恢复模型推理。",
        replay,
        "代码可核",
        "RunReplay",
      ),
      claim(
        "问题定位框架可借鉴 trajectory 与 model view 分离，但需额外定义 Evidence 提升规则。",
        trajectory,
        "机制映射",
        "DefaultAgent.get_trajectory_data",
      ),
    ],
  });
}

// ---------------------------------------------------------------------------
// 31–33 · mini-SWE-agent
// ---------------------------------------------------------------------------

{
  const agent = ref(
    "miniswe",
    "src/minisweagent/agents/default.py",
    "38-190",
    "DefaultAgent",
  );
  const environment = ref(
    "miniswe",
    "src/minisweagent/environments/local.py",
    "13-92",
    "LocalEnvironment",
  );
  const model = ref(
    "miniswe",
    "src/minisweagent/models/litellm_model.py",
    "60-80",
    "LitellmModel",
  );
  const cacheControl = ref(
    "miniswe",
    "src/minisweagent/models/utils/cache_control.py",
    "49-67",
    "cache-control",
  );
  const cli = ref(
    "miniswe",
    "src/minisweagent/run/mini.py",
    "22-105",
    "mini CLI",
  );
  addReferenceProject({
    projectKey: "miniswe",
    mechanismTitle: "mini-SWE-agent：用极简循环检验复杂机制是否真的有收益",
    mechanismLede:
      "单次运行的核心只有消息列表、模型、Bash、观察与若干成本/步数限制；<code>run()</code> 启动时清空 messages，没有隐藏的恢复机制。",
    mechanismSteps: [
      { title: "完整 messages", body: "所有历史送给模型" },
      { title: "模型生成动作", body: "仅暴露 Bash tool", tone: "red" },
      { title: "新 Shell 子进程", body: "执行并捕获 stdout/stderr" },
      { title: "追加 observation", body: "回到消息列表", tone: "red" },
      { title: "finally 保存 JSON", body: "每轮记录轨迹与成本" },
    ],
    mechanismCards: [
      {
        title: "状态极小",
        body: "<code>run()</code> 从空 messages 开始，再累计成本、调用次数和格式错误；没有独立 history/trajectory 双轨。",
      },
      {
        title: "保存不等于恢复",
        body: "每轮 finally 保存完整 JSON，但指定 SHA 没有从该 JSON 恢复并继续执行的 loader。",
      },
      {
        title: "没有上下文治理",
        body: "默认完整消息交给 API；cache-control 只添加缓存标记，不做摘要、删除或窗口管理。",
      },
    ],
    mechanismNote:
      "LocalEnvironment 每条命令默认 30 秒并在超时后杀死进程组；Agent 的总时长限制只在模型 query 前检查，不能中断已经阻塞的模型/API 调用。",
    mechanismRefs: [agent, environment, model, cacheControl, cli],
    mechanismClaims: [
      claim(
        "mini-SWE-agent 默认循环将完整消息送给模型、执行动作、追加观察，直到退出或触发限制。",
        agent,
        "代码可核",
        "DefaultAgent.run / step",
      ),
      claim(
        "每次循环 finally 保存 JSON，内容包括消息、配置、成本、调用次数和结果。",
        agent,
        "代码可核",
        "DefaultAgent.serialize / save",
      ),
      claim(
        "run 启动时清空 messages，默认 CLI 直接调用 agent.run 且没有加载轨迹继续执行的入口，因此保存 JSON 不是 crash-resume。",
        cli,
        "代码可核",
        "mini CLI",
      ),
      claim(
        "默认模型适配器不压缩或裁剪历史，cache-control 仅添加缓存标记。",
        cacheControl,
        "代码可核",
        "set_cache_control",
      ),
      claim(
        "LocalEnvironment 提供命令级 timeout，并在超时后终止进程组。",
        environment,
        "代码可核",
        "LocalEnvironment",
      ),
      claim(
        "Agent 总时长限制只在模型 query 前检查，不能中断已经阻塞的模型/API 调用。",
        agent,
        "代码可核",
        "DefaultAgent.query",
      ),
    ],
    rpcTitle: "RPC 超时在极简模型—Bash 循环中会怎样组织",
    rpcLede:
      "接入所需 SRE 命令后，它可承载同一组调查步骤，并提供步数、成本、时间和格式错误限制；结构化恢复、独立审核和证据完整性仍需问题定位框架补充，这正是其作为基线的价值。",
    codeProvides: [
      "完整 messages、模型—Bash—观察循环。",
      "步数、成本、时间和格式错误限制。",
      "每轮 JSON 审计记录，以及文件系统上的外部产物。",
    ],
    rpcSteps: [
      "把告警作为任务，依次执行 Trace、日志、配置和指标命令；每个命令可设置 timeout，输出原样追加到 messages。",
      "模型从 <code>db.acquire=3.02s</code> 推导连接池等待，再看到 <code>80 → 8</code> 与 <code>active=8 / waiters=120</code>。",
      "最终输出自然语言因果链；没有结构化 Evidence、阶段状态、独立审核或可恢复 Checkpoint。",
      "所需的 SRE 连接器、凭据注入、脱敏、查询限制和 DiagnosisState 均需外部实现；模型 API deadline 也需适配层保证。",
    ],
    rpcVerdict:
      "接入所需 SRE 能力后，这个极简循环可承载一次调查，并提供基础的步数、成本与时间边界。由于结构化恢复、独立审核和证据完整性仍需外部机制补齐，它适合作为复杂机制的对照组。",
    rpcRefs: [agent, environment, model],
    rpcClaims: [
      claim(
        "该 RPC 流程只使用 mini-SWE-agent 的模型—Bash—观察循环，全部生产工具由外部提供。",
        agent,
        "机制映射",
        "DefaultAgent.run",
      ),
      claim(
        "每条命令是新的本地 Shell，持久状态主要来自文件系统和显式参数。",
        environment,
        "代码可核",
        "LocalEnvironment.execute",
      ),
      claim(
        "结构化 Evidence、阶段状态、独立审核与恢复 Checkpoint 都需要问题定位框架补充。",
        agent,
        "机制映射",
        "DefaultAgent",
      ),
    ],
    takeawaysTitle: "mini-SWE-agent 的最大价值：提供复杂上下文机制的最小基线",
    takeawaysLede:
      "如果复杂机制不能在准确率、恢复率、token 或人工复核成本上明显胜过它，就不应默认引入。",
    borrow: [
      "保留极简、可读的模型—工具—观察主循环。",
      "把步数、成本、时间和格式错误限制设为明确运行边界。",
      "每轮保存可审计 JSON，便于离线比较不同机制。",
      "用同一 RPC 数据集做增量实验：逐项加入状态、Evidence、摘要和 Checkpoint。",
    ],
    limits: [
      "没有历史摘要、裁剪或独立 model view。",
      "保存 JSON 没有对应继续执行 loader，不能宣称恢复。",
      "没有 Repo Map、Git 状态抽象或多 Agent 调度。",
      "Bash 是唯一通用执行入口，不提供生产观测、凭据和权限治理。",
    ],
    verdict:
      "把 mini-SWE-agent 当实验基线，而不是目标架构：所有新增复杂度都必须证明对诊断正确性、恢复或成本有可测收益。",
    takeawayRefs: [agent, environment, model, cacheControl],
    takeawayClaims: [
      claim(
        "mini-SWE-agent 适合作为最小基线，衡量复杂状态、上下文与恢复机制的真实收益。",
        agent,
        "机制映射",
        "DefaultAgent",
      ),
      claim(
        "默认路径没有摘要、裁剪、Repo Map、Git 状态抽象或多 Agent 调度。",
        model,
        "代码可核",
        "LitellmModel",
      ),
      claim(
        "cache-control 标记不是上下文压缩或历史治理。",
        cacheControl,
        "代码可核",
        "set_cache_control",
      ),
    ],
  });
}

// ---------------------------------------------------------------------------
// Build HTML and evidence ledger
// ---------------------------------------------------------------------------

function buildOutputs() {
if (slides.length !== 33) {
  throw new Error(`Expected 33 slides, got ${slides.length}`);
}
const orderedSlides = [...slides].sort(
  (left, right) =>
    projectOrder.indexOf(left.projectKey) - projectOrder.indexOf(right.projectKey) ||
    left.localPage - right.localPage,
);
const pageLookup = new Map(
  orderedSlides.map((slide, index) => [
    `${slide.projectKey}:${slide.localPage}`,
    index + 1,
  ]),
);
ledgerRows.forEach((row) => {
  row.page = pageLookup.get(`${row.projectKey}:${row.localPage}`) || 0;
});

const themeReplacements = new Map([
  ["--paper:#f7f8fa;", "--paper:#f4f7fb;"],
  ["--paper-rgb:247,248,250;", "--paper-rgb:244,247,251;"],
  ["--ink:#22252b;", "--ink:#20242c;"],
  ["--ink-rgb:34,37,43;", "--ink-rgb:32,36,44;"],
  ["--muted:#6f7680;", "--muted:#667080;"],
  ["--line:#dfe3e8;", "--line:#dce3ea;"],
  ["--line-strong:#c7ccd4;", "--line-strong:#c1cad4;"],
  ["--brand-red:#c7000b;", "--brand-red:#d20a2e;"],
  ["--brand-red-rgb:199,0,11;", "--brand-red-rgb:210,10,46;"],
  ["--brand-red-dark:#990008;", "--brand-red-dark:#9c001f;"],
  ["--brand-red-soft:#f5e5e7;", "--brand-red-soft:#f8e5e9;"],
  ["--brand-black:#111111;", "--brand-black:#14171c;"],
  ["--brand-grey:#f0f2f5;", "--brand-grey:#edf2f7;"],
]);

let html = fs.readFileSync(templatePath, "utf8");
const lucideScript = fs
  .readFileSync(lucidePath, "utf8")
  .replace(/<\/script/gi, "<\\/script");
html = html
  .replace('<link rel="preconnect" href="https://fonts.googleapis.com">\n', "")
  .replace(
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n',
    "",
  )
  .replace(
    '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&family=Noto+Sans+SC:wght@300;400;500;700;900&display=swap" rel="stylesheet">\n',
    "",
  )
  .replace(
    '<script src="https://unpkg.com/lucide@latest/dist/umd/lucide.min.js"></script>',
    () => `<script>\n(() => {\n${lucideScript}\n})();\n</script>`,
  )
  .replace(
    "<title>[必填] 替换为 PPT 标题 · Huawei Corporate Deck</title>",
    "<title>问题定位开源 Agent 代码实证洞察 · Huawei Corporate Deck</title>",
  );
for (const [from, to] of themeReplacements) html = html.replace(from, to);
html = html
  .replace("</style>", `${customCss}\n</style>`)
  .replace(
    "<!-- SLIDES_HERE -->",
    orderedSlides.map((slide) => slide.html).join("\n\n"),
  );
const uniqueFixedSourceLinkCount = new Set(
  [...html.matchAll(
    /href="(https:\/\/github\.com\/[^"]+\/blob\/[0-9a-f]{40}\/[^"]+)"/g,
  )].map((match) => match[1]),
).size;
fs.writeFileSync(outputPath, html, "utf8");

const baselineRows = Object.values(projects)
  .map(
    (project) =>
      `| ${project.name} | \`${project.repo}\` | \`${project.branch}\` | \`${project.sha}\` | ${project.license} | ${investigatedAt} |`,
  )
  .join("\n");
const orderedLedgerRows = [...ledgerRows].sort(
  (left, right) => left.page - right.page || left.id.localeCompare(right.id),
);
const claimRows = orderedLedgerRows
  .map(
    (item) =>
      `| ${item.id} | ${item.project} | ${item.page} | ${item.type} | ${item.claim.replaceAll("|", "\\|")} | \`${item.file}\` | ${item.symbol || "—"} ${item.lines ? `(L${item.lines})` : ""} | [固定链接](${item.href}) | ${item.primary} | ${item.reviewer} | ${item.status} |`,
  )
  .join("\n");
const ledger = `# 开源 Agent 洞察证据账本

> 调查日期：${investigatedAt}  
> 生成源：\`doc/problem-locator-open-source-insight-ppt/build-agent-insight-deck.mjs\`  
> 证据类型：代码可核 / 文档陈述 / 论文陈述 / 机制映射 / 未确认  
> 说明：HTML 中的所有源码链接均固定到完整 commit；“机制映射”不是项目原生 SRE 能力。

## 代码基线

| 项目 | 仓库 | 默认分支 | 固定 HEAD | 许可证 | 调查日期 |
|---|---|---|---|---|---|
${baselineRows}

## Claim 账本

| Claim ID | 项目 | PPT 页码 | 证据类型 | 结论 | 源码路径 | 实现符号 / 行号 | 固定链接 | 主审 Agent | 复核 Agent | 状态 |
|---|---:|---:|---|---|---|---|---|---|---|---|
${claimRows}

## 运行验证边界

- 已完成固定 commit 的代码审查与局部静态核验。
- 已用十个本地固定 commit 的 Git tree 校验 HTML 中 ${uniqueFixedSourceLinkCount} 个唯一源码链接，未发现路径缺失或浮动分支链接。
- 已尝试运行 OpenSRE 的 context budget 与 plan_actions 定向测试；本机捆绑 Python 缺少 pytest，且未安装 uv，因此未修改快照或临时安装仓库依赖，测试未实际执行。
- 未调用外部大模型、云监控、数据库或生产环境，因此未完成十个项目的端到端生产复现。
- OpenHands 当前仓库为 Agent Canvas；核心 Agent Server / software-agent-sdk 不在本次十仓基线中，因此相关运行时能力明确降级。
- OpenDerisk 的 GroupMode、完整 RL Dynamic、内部生产平台和论文效果不作为当前开源执行能力。
`;
fs.mkdirSync(path.dirname(ledgerPath), { recursive: true });
fs.writeFileSync(ledgerPath, ledger, "utf8");

console.log(`Wrote ${slides.length} slides to ${outputPath}`);
console.log(`Wrote ${ledgerRows.length} claims to ${ledgerPath}`);
}

// ---------------------------------------------------------------------------
// 19–21 · AutoGen
// ---------------------------------------------------------------------------

{
  const assistant = ref(
    "autogen",
    "python/packages/autogen-agentchat/src/autogen_agentchat/agents/_assistant_agent.py",
    "901-1310",
    "Agent loop",
  );
  const iterations = ref(
    "autogen",
    "python/packages/autogen-agentchat/src/autogen_agentchat/agents/_assistant_agent.py",
    "137-153",
    "tool iterations",
  );
  const modelContext = ref(
    "autogen",
    "python/packages/autogen-core/src/autogen_core/model_context/_chat_completion_context.py",
    "10-74",
    "ModelContext",
  );
  const buffered = ref(
    "autogen",
    "python/packages/autogen-core/src/autogen_core/model_context/_buffered_chat_completion_context.py",
    "15-41",
    "Buffered",
  );
  const tokenLimited = ref(
    "autogen",
    "python/packages/autogen-core/src/autogen_core/model_context/_token_limited_chat_completion_context.py",
    "19-77",
    "TokenLimited",
  );
  const manager = ref(
    "autogen",
    "python/packages/autogen-agentchat/src/autogen_agentchat/teams/_group_chat/_base_group_chat_manager.py",
    "77-230",
    "GroupChat manager",
  );
  const roundRobin = ref(
    "autogen",
    "python/packages/autogen-agentchat/src/autogen_agentchat/teams/_group_chat/_round_robin_group_chat.py",
    "25-82",
    "RoundRobin",
  );
  const assistantState = ref(
    "autogen",
    "python/packages/autogen-agentchat/src/autogen_agentchat/agents/_assistant_agent.py",
    "1630-1639",
    "Agent state",
  );
  const teamState = ref(
    "autogen",
    "python/packages/autogen-agentchat/src/autogen_agentchat/teams/_group_chat/_base_group_chat.py",
    "748-834",
    "Team state",
  );
  const cancellation = ref(
    "autogen",
    "python/packages/autogen-core/src/autogen_core/_cancellation_token.py",
    "6-46",
    "CancellationToken",
  );
  const readme = ref("autogen", "README.md", "13-27", "Maintenance Mode", "文档陈述");
  addReferenceProject({
    projectKey: "autogen",
    mechanismTitle: "AutoGen：可替换 ModelContext 与显式 Team 发言调度",
    mechanismLede:
      "AutoGen 把“消息如何存储”和“本轮给模型看什么”分开；但 <code>ModelContext</code> 仍是消息状态，不是权威诊断状态。",
    mechanismSteps: [
      { title: "新消息 / Memory", body: "加入 ModelContext" },
      { title: "模型判断", body: "输出文本或工具调用", tone: "red" },
      { title: "并发工具执行", body: "同轮调用 asyncio.gather" },
      { title: "结果回填", body: "继续工具循环或结束", tone: "red" },
      { title: "save / load state", body: "显式保存 llm_context 或 Team state" },
    ],
    mechanismCards: [
      {
        title: "工具循环默认不长",
        body: "<code>max_tool_iterations</code> 默认值为 1；只有显式调高才形成同一次 run 内的多轮工具排查。",
      },
      {
        title: "ModelContext 策略可替换",
        body: "Buffered 只给模型最近 N 条；TokenLimited 超限时删除中部消息并尽量保留首尾。",
      },
      {
        title: "Team 有发言语义",
        body: "GroupChatManager 管理共享线程、轮数、终止条件与下一发言者；RoundRobin 每轮只选一个 Agent。",
      },
    ],
    mechanismNote:
      "固定 SHA 的 README 明确标注 Maintenance Mode，并建议新项目转向 Microsoft Agent Framework；这是项目文档陈述。",
    mechanismRefs: [
      assistant,
      iterations,
      modelContext,
      buffered,
      tokenLimited,
      manager,
      roundRobin,
      assistantState,
      teamState,
      readme,
    ],
    mechanismClaims: [
      claim(
        "AssistantAgent 将新消息与 Memory 结果加入 ModelContext，再调用模型并处理文本或工具调用。",
        assistant,
        "代码可核",
        "AssistantAgent.on_messages_stream",
      ),
      claim(
        "max_tool_iterations 默认值为 1，只有显式提高才形成一次 run 内的多轮工具循环。",
        iterations,
        "代码可核",
        "AssistantAgent",
      ),
      claim(
        "ModelContext 分离完整消息存储与模型消息视图，并支持 save_state/load_state。",
        modelContext,
        "代码可核",
        "ChatCompletionContext",
      ),
      claim(
        "RoundRobinGroupChat 每轮只选一个 Agent，并保存线程、轮数和下一发言者。",
        roundRobin,
        "代码可核",
        "RoundRobinGroupChatManager",
      ),
      claim(
        "AssistantAgent.save_state 只保存 llm_context；不会自动包含外部 Memory、workbench 内部状态或工具副作用。",
        assistantState,
        "代码可核",
        "AssistantAgent.save_state",
      ),
      claim(
        "GroupChat state 需要调用方显式保存，且运行中不能 load_state。",
        teamState,
        "代码可核",
        "BaseGroupChat.save_state / load_state",
      ),
      claim(
        "AutoGen 当前处于 Maintenance Mode 是 README 项目陈述。",
        readme,
        "文档陈述",
        "README",
      ),
    ],
    rpcTitle: "RPC 超时如何映射到 AssistantAgent、ModelContext 与 Team",
    rpcLede:
      "AutoGen 能组织工具循环和多 Agent 发言，但诊断事实必须独立于会被裁剪的消息上下文；单次 RPC deadline 仍由工具适配器实现。",
    codeProvides: [
      "AssistantAgent 的模型—工具循环与同轮工具并发。",
      "Buffered / TokenLimited 等可替换 ModelContext 策略。",
      "Agent 与 Team 的显式 save/load state，以及 GroupChat 发言调度。",
    ],
    rpcSteps: [
      "把告警送入 AssistantAgent；显式调高 <code>max_tool_iterations</code>，并由每个观测工具自行设置 deadline、重试和错误分类。",
      "Trace 结果和日志观察进入 ModelContext；模型形成连接池等待假设。",
      "也可由 Team 分配核验任务，但共享消息线程之外仍需外部 <code>DiagnosisState</code>。",
      "将 <code>db.acquire=3.02s</code>、连接池 <code>80 → 8</code> 和反证写入权威 Evidence，避免裁剪后失去依据。",
    ],
    rpcVerdict:
      "ModelContext 是模型输入策略，不是事实数据库；RPC 调查应把 Evidence 外置，再按需要投影到各 Agent 的上下文。",
    rpcRefs: [assistant, iterations, modelContext, manager, cancellation],
    rpcClaims: [
      claim(
        "该 RPC 流程需显式调高 max_tool_iterations，并外接生产观测工具。",
        iterations,
        "机制映射",
        "AssistantAgent",
      ),
      claim(
        "工具结果可以写回 ModelContext 继续推理，但关键 Evidence 应同步写入外部 DiagnosisState。",
        modelContext,
        "机制映射",
        "ChatCompletionContext",
      ),
      claim(
        "GroupChat 可分配 Agent 发言，但不会自动解决事实冲突、证据优先级与审核。",
        manager,
        "机制映射",
        "BaseGroupChatManager",
      ),
      claim(
        "CancellationToken 提供协作式取消但没有 deadline/timeout 字段，因此单次 RPC 超时控制必须由工具实现。",
        cancellation,
        "机制映射",
        "CancellationToken",
      ),
    ],
    takeawaysTitle: "AutoGen 的关键启示：上下文策略可换，业务事实状态必须独立",
    takeawaysLede:
      "Buffered、TokenLimited 与 Team 调度会改变模型看到的消息；它们不应改变“哪些事实已经被验证”。",
    borrow: [
      "把 ModelContext 抽象成可替换策略，并提供 save/load state。",
      "同轮多个只读工具调用可并发，结果再统一回填。",
      "Team Manager 明确发言者、轮数和终止条件。",
      "Agent Memory 只作为模型输入来源，不直接升格为权威事实。",
    ],
    limits: [
      "Buffered 会隐藏较早消息；关键证据不能只存在消息中。",
      "TokenLimited 删除中部消息，且该组件标注 Experimental。",
      "RoundRobin 是顺序发言，不是并行专家调查。",
      "AssistantAgent state 只含 llm_context；Team state 也需调用方保存，恢复不等于外部副作用确定性重放。",
    ],
    verdict:
      "把 AutoGen 的 ModelContext 当“消费视图”，把 DiagnosisState 与 Evidence 当“事实层”；框架维护模式也降低其新项目依赖优先级。",
    takeawayRefs: [buffered, tokenLimited, assistantState, teamState, manager, readme],
    takeawayClaims: [
      claim(
        "BufferedChatCompletionContext 只向模型提供最近 N 条消息。",
        buffered,
        "代码可核",
        "BufferedChatCompletionContext",
      ),
      claim(
        "TokenLimitedChatCompletionContext 超限时删除消息中部并尽量保留首尾，且源码标注 Experimental。",
        tokenLimited,
        "代码可核",
        "TokenLimitedChatCompletionContext",
      ),
      claim(
        "业务事实状态应独立于 ModelContext，否则裁剪策略会改变诊断依据。",
        modelContext,
        "机制映射",
        "ChatCompletionContext",
      ),
    ],
  });
}

// ---------------------------------------------------------------------------
// 22–24 · CrewAI
// ---------------------------------------------------------------------------

{
  const crew = ref(
    "crewai",
    "lib/crewai/src/crewai/crew.py",
    "988-1090",
    "Crew.kickoff",
  );
  const tasks = ref(
    "crewai",
    "lib/crewai/src/crewai/crew.py",
    "1490-1625",
    "Task 调度",
  );
  const executor = ref(
    "crewai",
    "lib/crewai/src/crewai/agents/crew_agent_executor.py",
    "208-430",
    "Agent ReAct loop",
  );
  const context = ref(
    "crewai",
    "lib/crewai/src/crewai/agents/crew_agent_executor.py",
    "405-460",
    "上下文超限处理",
  );
  const checkpoint = ref(
    "crewai",
    "lib/crewai/src/crewai/crew.py",
    "424-534",
    "Crew Checkpoint",
  );
  const checkpointConfig = ref(
    "crewai",
    "lib/crewai/src/crewai/state/checkpoint_config.py",
    "146-234",
    "CheckpointConfig",
  );
  const runtimeState = ref(
    "crewai",
    "lib/crewai/src/crewai/state/runtime.py",
    "177-416",
    "RuntimeState",
  );
  const checkpointListener = ref(
    "crewai",
    "lib/crewai/src/crewai/state/checkpoint_listener.py",
    "113-270",
    "CheckpointListener",
  );
  const flow = ref(
    "crewai",
    "lib/crewai/src/crewai/flow/runtime/__init__.py",
    "361-440",
    "FlowState / Flow",
  );
  const persistence = ref(
    "crewai",
    "lib/crewai/src/crewai/flow/runtime/__init__.py",
    "1982-2075",
    "Flow persistence",
  );
  const cache = ref(
    "crewai",
    "lib/crewai/src/crewai/crew.py",
    "220-250",
    "Crew cache",
  );
  const agentTimeout = ref(
    "crewai",
    "lib/crewai/src/crewai/agent/core.py",
    "830-1056",
    "Agent execution timeout",
  );
  addReferenceProject({
    projectKey: "crewai",
    mechanismTitle: "CrewAI：Flow 控制确定性路径，Crew 与 Agent 承担自主执行",
    mechanismLede:
      "Flow、Crew、Task、Agent Memory 各有状态；真正的设计难点是明确哪一层才是权威业务状态。",
    mechanismSteps: [
      { title: "Flow start", body: "初始化带 ID 的 Pydantic state" },
      { title: "Crew kickoff", body: "sequential / hierarchical", tone: "red" },
      { title: "Task 调度", body: "顺序执行或异步批次" },
      { title: "Agent loop", body: "模型—Action—工具—结果", tone: "red" },
      { title: "持久化 / 恢复", body: "仅在配置后生效" },
    ],
    mechanismCards: [
      {
        title: "Flow 是控制面",
        body: "<code>start / listen / router</code> 与结构化 state 形成确定性路径；诊断字段仍需应用定义。",
      },
      {
        title: "Crew/Agent 是执行面",
        body: "Crew 支持 sequential 与 hierarchical；单 Agent 在 ReAct 循环内执行工具，异步 Task 可形成并发批次。",
      },
      {
        title: "上下文与 Memory 不是同一层",
        body: "上下文压缩与 Memory 是不同机制；两者都不能替代完整、权威的业务事实状态。",
      },
    ],
    mechanismNote:
      "配置 Checkpoint 后可恢复 Flow/Crew 运行结构与进度；自动写入是 best-effort，不能回滚已发生的外部工具副作用，也不能据此保证 <span class=\"nowrap\">exactly-once</span>。",
    mechanismRefs: [crew, tasks, executor, context, flow, persistence, checkpointConfig, runtimeState, checkpointListener],
    mechanismClaims: [
      claim(
        "Crew kickoff 支持 sequential 与 hierarchical 两种执行模式，异步 Task 可形成并发批次。",
        crew,
        "代码可核",
        "Crew.kickoff",
      ),
      claim(
        "单个 Agent 在 CrewAgentExecutor 中循环执行模型推理、Action、工具和结果，直到结束或达到上限。",
        executor,
        "代码可核",
        "CrewAgentExecutor._invoke_loop_react",
      ),
      claim(
        "Flow 使用带 ID 的 Pydantic state 和 start/listener/router 构建控制流程。",
        flow,
        "代码可核",
        "FlowState / Flow",
      ),
      claim(
        "Flow 持久化仅在配置 persistence 或 checkpoint 后生效；未配置时相关路径 no-op。",
        persistence,
        "代码可核",
        "Flow.kickoff",
      ),
      claim(
        "CheckpointConfig 与 RuntimeState 可保存并恢复 Flow/Crew 运行结构和进度。",
        runtimeState,
        "代码可核",
        "RuntimeState",
      ),
      claim(
        "自动 Checkpoint listener 是 best-effort，不能回滚外部工具副作用或保证 exactly-once。",
        checkpointListener,
        "代码可核",
        "CheckpointListener",
      ),
    ],
    rpcTitle: "RPC 超时如何用 Flow 固定控制面、用 Crew 执行核验",
    rpcLede:
      "推荐把诊断状态写入 Flow 或外部 Case Store，让 Crew/Agent 只消费任务并提交证据，而不是多处重复写状态。",
    codeProvides: [
      "Flow 的结构化 state、router、listener 与条件持久化。",
      "Crew 的 sequential/hierarchical Task 编排与异步 Task。",
      "单 Agent 工具循环、Memory 召回和显式配置的 Crew/Flow Checkpoint。",
    ],
    rpcSteps: [
      "Flow start 创建 Case state，记录告警、服务、时间窗和当前阶段；每个观测工具自行设置单次调用 timeout。",
      "Crew 分配 Trace、日志、发布配置、连接池/数据库指标 Task；异步 Task 可并发。",
      "各 Task 输出 <code>db.acquire=3.02s</code>、连接池 <code>80 → 8</code> 与反证，Flow 统一写入 Evidence。",
      "router 根据证据充分性进入追加核验、人工审核或 RCA 报告节点。",
    ],
    rpcVerdict:
      "Flow 适合做确定性控制面，Crew 适合做自主执行面；两者之间必须用唯一 DiagnosisState 交接。",
    rpcRefs: [flow, crew, tasks, executor, agentTimeout],
    rpcClaims: [
      claim(
        "该 RPC 流程把 Flow state 作为控制面，并把 Crew Task 映射为 Trace、日志、配置与指标核验。",
        flow,
        "机制映射",
        "Flow",
      ),
      claim(
        "异步 Task 可承载并行核验，但任务结构和结果合并规则需应用预先定义。",
        tasks,
        "机制映射",
        "Crew._execute_tasks",
      ),
      claim(
        "CrewAI 不原生提供 RPC 语义、观测连接器、Evidence schema 或独立审核。",
        executor,
        "机制映射",
        "CrewAgentExecutor",
      ),
      claim(
        "max_execution_time 限制整个 Agent task 的墙钟时间，不是下游单次 RPC deadline。",
        agentTimeout,
        "机制映射",
        "Agent execution timeout",
      ),
    ],
    takeawaysTitle: "CrewAI：控制面与推理面分离，状态只保留一个权威来源",
    takeawaysLede:
      "如果 Flow state、Agent Memory 和 Task context 都能修改诊断事实，就会出现恢复不一致和审核歧义。",
    borrow: [
      "用 Flow 固定告警接收、证据收集、人工审核与报告交付路径。",
      "用 Crew/Agent 执行受限范围内的自主查询与专家分工。",
      "用 router 显式表达停止、追加核验和降级路径。",
      "只在明确外部副作用、Memory 与 <span class=\"nowrap\">exactly-once</span> 边界后启用 Checkpoint。",
    ],
    limits: [
      "hierarchical 不等于所有 Agent 自动并行。",
      "Checkpoint 可恢复运行结构与进度，但不能回滚外部副作用，也不等于 <span class=\"nowrap\">exactly-once</span>。",
      "Flow 持久化不是默认能力，需要显式配置。",
      "工具缓存默认关闭；实时日志/指标/Trace 不应盲目复用旧结果。",
    ],
    verdict:
      "可采用“Flow 控制 + Crew 执行”，但 DiagnosisState 必须独立且唯一；Memory 和 Task context 只作为读视图或临时协作载体。",
    takeawayRefs: [checkpoint, checkpointListener, persistence, cache, flow],
    takeawayClaims: [
      claim(
        "Crew Checkpoint 可恢复运行结构与 Task 进度，但不能回滚外部工具副作用。",
        checkpointListener,
        "代码可核",
        "CheckpointListener",
      ),
      claim(
        "Crew 相同工具结果缓存默认关闭，源码提醒实时或有副作用工具谨慎启用。",
        cache,
        "代码可核",
        "Crew.cache",
      ),
      claim(
        "问题定位框架应把 Flow state 设为唯一权威 DiagnosisState，避免与 Memory 和 Task context 双写。",
        flow,
        "机制映射",
        "FlowState",
      ),
    ],
  });
}

function addReferenceProject({
  projectKey,
  mechanismTitle,
  mechanismLede,
  mechanismSteps,
  mechanismCards,
  mechanismNote,
  mechanismRefs,
  mechanismClaims,
  rpcTitle,
  rpcLede,
  codeProvides,
  rpcSteps,
  rpcVerdict,
  rpcRefs,
  rpcClaims,
  takeawaysTitle,
  takeawaysLede,
  borrow,
  limits,
  verdict,
  takeawayRefs,
  takeawayClaims,
}) {
  const project = projects[projectKey];
  addSlide({
    projectKey,
    localPage: 1,
    section: "代码机制",
    eyebrow: "SOURCE-CODE MECHANISM",
    title: mechanismTitle,
    lede: mechanismLede,
    body: `
      ${flow(mechanismSteps, true)}
      <div class="fact-grid">
        ${mechanismCards
          .map((item, index) =>
            factCard(
              String(index + 1).padStart(2, "0"),
              item.title,
              item.body,
              index === 0 ? "red" : "",
            ),
          )
          .join("")}
      </div>
      <div class="note-strip" data-anim>${mechanismNote}</div>`,
    refs: mechanismRefs,
    claims: mechanismClaims,
    layout: "H37",
  });

  addSlide({
    projectKey,
    localPage: 2,
    section: "RPC 机制映射",
    eyebrow: "SHARED RPC CASE · MECHANISM MAPPING",
    title: rpcTitle,
    lede: rpcLede,
    body: `
      ${rpcInput}
      <div class="rpc-lanes">
        <div class="rpc-lane code" data-anim>
          <h3>项目代码提供</h3>
          <ol>${codeProvides.map((item) => `<li>${item}</li>`).join("")}</ol>
        </div>
        <div class="rpc-lane add" data-anim>
          <h3>如何承载同一组排查材料</h3>
          <ol>${rpcSteps.map((item) => `<li>${item}</li>`).join("")}</ol>
        </div>
      </div>
      <div class="takeaway" data-anim><strong>映射结论</strong><p>${rpcVerdict}</p></div>
      ${rpcBoundary(project.name)}
    `,
    refs: rpcRefs,
    claims: rpcClaims,
    layout: "H26",
  });

  addSlide({
    projectKey,
    localPage: 3,
    section: "借鉴与边界",
    eyebrow: "DESIGN TAKEAWAYS",
    title: takeawaysTitle,
    lede: takeawaysLede,
    body: `
      <div class="truth-table">
        <div class="truth-col confirmed" data-anim>
          <h3>对问题定位框架值得借鉴</h3>
          ${borrow
            .map(
              (item) =>
                `<div class="truth-row"><b>BORROW</b><p>${item}</p></div>`,
            )
            .join("")}
        </div>
        <div class="truth-col limited" data-anim>
          <h3>适用边界与误用风险</h3>
          ${limits
            .map(
              (item) =>
                `<div class="truth-row"><b>BOUNDARY</b><p>${item}</p></div>`,
            )
            .join("")}
        </div>
      </div>
      <div class="takeaway" data-anim><strong>一句话结论</strong><p>${verdict}</p></div>`,
    refs: takeawayRefs,
    claims: takeawayClaims,
    layout: "H24",
  });
}

// ---------------------------------------------------------------------------
// 10–12 · LangGraph
// ---------------------------------------------------------------------------

{
  const state = ref(
    "langgraph",
    "libs/langgraph/langgraph/graph/state.py",
    "100-180",
    "StateGraph",
  );
  const compile = ref(
    "langgraph",
    "libs/langgraph/langgraph/graph/state.py",
    "1110-1245",
    "StateGraph.compile",
  );
  const snapshot = ref(
    "langgraph",
    "libs/langgraph/langgraph/types.py",
    "650-714",
    "StateSnapshot",
  );
  const loop = ref(
    "langgraph",
    "libs/langgraph/langgraph/pregel/_loop.py",
    "520-760",
    "PregelLoop",
  );
  const react = ref(
    "langgraph",
    "libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py",
    "284-986",
    "预构建 ReAct Agent",
  );
  const send = ref(
    "langgraph",
    "libs/langgraph/langgraph/types.py",
    "715-810",
    "Send",
  );
  const durability = ref(
    "langgraph",
    "libs/langgraph/langgraph/types.py",
    "82-103",
    "Durability",
  );
  const defaultDurability = ref(
    "langgraph",
    "libs/langgraph/langgraph/pregel/main.py",
    "2600-2603",
    "durability 默认值",
  );
  const checkpointBase = ref(
    "langgraph",
    "libs/checkpoint/langgraph/checkpoint/base/__init__.py",
    "180-330",
    "BaseCheckpointSaver",
  );
  addReferenceProject({
    projectKey: "langgraph",
    mechanismTitle: "LangGraph：用显式 State 与 Checkpoint 驱动可恢复执行",
    mechanismLede:
      "图调度循环与大模型思考循环是两层机制：<code>StateGraph</code> 负责状态更新与路由，节点内部是否调用模型由应用决定。",
    mechanismSteps: [
      { title: "定义 State", body: "字段与 reducer 由应用声明" },
      { title: "节点返回更新", body: "读取共享状态，返回局部增量", tone: "red" },
      { title: "Pregel superstep", body: "调度任务、通道与写入" },
      { title: "Checkpoint", body: "保存 StateSnapshot 与下一节点", tone: "red" },
      { title: "恢复 / 分叉", body: "按 thread 与 checkpoint 继续" },
    ],
    mechanismCards: [
      {
        title: "StateGraph + reducer",
        body: "同一字段可通过 reducer 合并并行更新；诊断字段和冲突规则必须由应用定义。",
      },
      {
        title: "Checkpointer + Store",
        body: "Checkpointer 保存线程内版本化状态；Store 可承载跨线程数据，但都不自带业务语义。",
      },
      {
        title: "pre_model_hook / Send",
        body: "可单独构造模型输入视图，也可把不同状态发送给同一节点形成任务扇出。",
      },
    ],
    mechanismNote:
      "指定 SHA 中，预构建 <code>create_react_agent</code> 已标记为 deprecated。若调用方未显式传入 <code>durability</code>，且配置中也未指定，Pregel 运行路径默认采用 <code>async</code>。Checkpoint 仅保存图执行状态；数据库、配置中心等外部副作用仍需由应用另行设计事务、幂等或补偿。",
    mechanismRefs: [state, compile, snapshot, loop, durability, defaultDurability, checkpointBase, react, send],
    mechanismClaims: [
      claim(
        "StateGraph 节点读取共享状态并返回局部更新，同一字段可通过 reducer 合并并行更新。",
        state,
        "代码可核",
        "StateGraph",
      ),
      claim(
        "编译图可接入 checkpointer、cache、Store 和中断点。",
        compile,
        "代码可核",
        "StateGraph.compile",
      ),
      claim(
        "StateSnapshot 保存状态值、下一节点、任务、错误与中断，可作为查看和恢复执行的基础。",
        snapshot,
        "代码可核",
        "StateSnapshot",
      ),
      claim(
        "PregelLoop 的 superstep 是图调度机制，不等于大模型思考循环。",
        loop,
        "代码可核",
        "PregelLoop",
      ),
      claim(
        "Durability 类型支持 sync、async、exit 三种持久化时机。",
        durability,
        "代码可核",
        "Durability",
      ),
      claim(
        "调用方未显式传入 durability 且配置中也未指定时，Pregel 运行路径使用 async。",
        defaultDurability,
        "代码可核",
        "Pregel._defaults",
      ),
      claim(
        "不能把图状态 Checkpoint 外推为数据库或配置中心副作用的事务回滚；问题定位框架需自行设计幂等与补偿。",
        checkpointBase,
        "机制映射",
        "BaseCheckpointSaver",
      ),
    ],
    rpcTitle: "RPC 超时如何映射为一张可恢复的诊断状态图",
    rpcLede:
      "LangGraph 原生提供状态与执行基础设施；RPC 领域状态、工具、路由和审核都需问题定位框架自行实现。",
    codeProvides: [
      "<code>StateGraph</code>、reducer 与条件边。",
      "<code>StateSnapshot</code>、checkpointer、Store 与 interrupt。",
      "<code>Send</code> 的任务扇出，以及 <code>pre_model_hook</code> 的模型输入视图。",
    ],
    rpcSteps: [
      "定义 <code>DiagnosisState</code>：告警、假设、Evidence、排除项、待确认项和审核状态。",
      "Trace 节点写入 <code>db.acquire=3.02s</code>；路由进入日志与配置/指标节点。",
      "用 reducer 合并连接获取超时、连接池 <code>80 → 8</code>、<code>active=8 / waiters=120</code> 等证据。",
      "在结论节点前 interrupt，人工复核后从 Checkpoint 恢复并交付 RCA。",
    ],
    rpcVerdict:
      "最强项是让“诊断事实和执行位置”成为显式、可恢复的状态；模型只应消费状态视图，不应把消息历史当唯一事实源。",
    rpcRefs: [state, snapshot, send, compile],
    rpcClaims: [
      claim(
        "该 RPC 流程是基于 StateGraph、reducer、Send 与 Checkpoint 的机制映射，不是 LangGraph 原生 SRE 能力。",
        state,
        "机制映射",
        "StateGraph",
      ),
      claim(
        "DiagnosisState、Trace/日志/指标/配置工具和证据充分性路由需要应用自行实现。",
        compile,
        "机制映射",
        "StateGraph.compile",
      ),
      claim(
        "可在人工复核节点中断，并依靠 StateSnapshot 与 checkpointer 恢复图执行。",
        snapshot,
        "机制映射",
        "StateSnapshot",
      ),
    ],
    takeawaysTitle: "LangGraph：权威状态与可恢复执行，不提供诊断知识",
    takeawaysLede:
      "对问题定位框架而言，它更像控制面与状态基础设施，而不是可直接使用的 RCA Agent。",
    borrow: [
      "用显式 <code>DiagnosisState</code> 管理事实、假设、证据、排除项与审核状态。",
      "通过 reducer 明确定义并行结果如何合并，避免多路写入各说各话。",
      "用 Checkpoint、interrupt 和 thread 实现人工复核与可恢复执行。",
      "用 <code>pre_model_hook</code> 构造模型输入视图，不覆盖权威状态。",
    ],
    limits: [
      "StateSnapshot 是执行快照，不是不可变的 Evidence；默认 async durability 也不提供外部副作用的全局 <span class=\"nowrap\">exactly-once</span>。",
      "节点或 <code>Send</code> 并行不自动等于多个自主 Agent。",
      "框架不提供 RPC/SRE 语义、数据源适配、停止规则或根因审核。",
      "预构建 ReAct Agent 在该版本已 deprecated，接口选型需谨慎。",
    ],
    verdict:
      "把 LangGraph 放在“诊断状态机与恢复层”，让 Agent 作为可替换节点，而不是让图状态退化成聊天消息集合。",
    takeawayRefs: [state, snapshot, durability, defaultDurability, checkpointBase, react, send],
    takeawayClaims: [
      claim(
        "LangGraph 适合承担诊断状态机、Checkpoint 和恢复层，诊断领域模型必须由应用定义。",
        state,
        "机制映射",
        "StateGraph",
      ),
      claim(
        "Checkpoint 与 StateSnapshot 不能替代证据溯源和独立根因审核。",
        snapshot,
        "机制映射",
        "StateSnapshot",
      ),
      claim(
        "Send 只提供任务扇出，节点并行不自动具有多 Agent 角色与通信语义。",
        send,
        "代码可核",
        "Send",
      ),
    ],
  });
}

// ---------------------------------------------------------------------------
// 13–15 · OpenHands
// ---------------------------------------------------------------------------

{
  const readme = ref("openhands", "README.md", "1-75", "Agent Canvas README", "文档陈述");
  const architecture = ref(
    "openhands",
    "docs/architecture.md",
    "1-45",
    "Canvas 架构",
    "文档陈述",
  );
  const event = ref(
    "openhands",
    "src/types/agent-server/core/openhands-event.ts",
    "1-46",
    "OpenHandsEvent",
  );
  const condensation = ref(
    "openhands",
    "src/types/agent-server/core/events/condensation-event.ts",
    "5-46",
    "CondensationEvent",
  );
  const state = ref(
    "openhands",
    "src/types/agent-server/core/events/conversation-state-event.ts",
    "7-128",
    "ConversationStateEvent",
  );
  const eventsApi = ref(
    "openhands",
    "src/api/event-service/event-service.api.ts",
    "17-180",
    "事件查询 API",
  );
  const eventRenderer = ref(
    "openhands",
    "src/components/conversation-events/chat/event-content-helpers/should-render-event.ts",
    "39-118",
    "chat 事件渲染规则",
  );
  const metricsAggregator = ref(
    "openhands",
    "src/utils/conversation-metrics.ts",
    "8-70",
    "会话指标汇总",
  );
  addReferenceProject({
    projectKey: "openhands",
    mechanismTitle: "OpenHands 当前仓库：Agent Canvas 控制面",
    mechanismLede:
      "固定 SHA 的 <code>OpenHands/OpenHands</code> 已是前端控制中心；Agent 循环、沙箱和 Condenser 算法位于外部 <code>OpenHands/software-agent-sdk</code>。",
    mechanismSteps: [
      { title: "Canvas 发起会话", body: "连接外部 Agent Server" },
      { title: "Agent Server 运行", body: "执行 Agent、沙箱与凭据", tone: "red" },
      { title: "事件协议返回", body: "Action / Observation / State" },
      { title: "Condensation 事件", body: "声明 LLM View 遗忘事件与摘要", tone: "red" },
      { title: "Canvas 展示", body: "分页查询历史与运行状态" },
    ],
    mechanismCards: [
      {
        title: "事件契约可核",
        body: "前端类型区分 Action、Observation、Message、状态、暂停、错误与 Condensation。",
      },
      {
        title: "模型视图概念可核",
        body: "<code>CondensationEvent</code> 表达哪些事件从 LLM View 中被遗忘，以及可选摘要。",
      },
      {
        title: "服务端持久化不可由前端推出",
        body: "能分页查询历史事件，不等于证明服务端 <span class=\"nowrap\">append-only</span>、不可变存储或具体恢复算法。",
      },
    ],
    mechanismNote:
      "必须纠偏旧结论：当前仓库不能验证旧版 <code>base_state.json</code>、Workspace 执行、Agent 循环、实际 Condenser 算法或完整恢复语义。",
    mechanismRefs: [readme, architecture, event, condensation, state, eventsApi],
    mechanismClaims: [
      claim(
        "当前 OpenHands/OpenHands 仓库是 Agent Canvas，用于运行和管理外部编码 Agent 后端。",
        readme,
        "文档陈述",
        "README",
      ),
      claim(
        "Agent 执行、沙箱和凭据由外部 Agent Server 负责，核心运行时不在当前仓库。",
        architecture,
        "文档陈述",
        "architecture.md",
      ),
      claim(
        "Canvas 事件契约区分 Action、Observation、Message、状态、错误与 Condensation 等事件。",
        event,
        "代码可核",
        "OpenHandsEvent",
      ),
      claim(
        "CondensationEvent 描述从 LLM View 遗忘的事件 ID 和可选摘要，但不能证明服务端原始事件永久保留。",
        condensation,
        "代码可核",
        "CondensationEvent",
      ),
    ],
    rpcTitle: "RPC 案例当前只映射 OpenHands Canvas 的控制面机制",
    rpcLede:
      "在纳入并审查固定 SHA 的 <code>software-agent-sdk</code> 前，本页只展示 Canvas 如何消费调查事件。",
    codeProvides: [
      "创建、连接和监控外部 Agent Server 会话。",
      "消费 Action、Observation、Message、State 与 Condensation 事件。",
      "分页查询历史事件；Canvas 以会话视图展示后端上报的 token、成本和状态，不等于分别呈现 Agent 与 Condenser 的完整指标。",
    ],
    rpcSteps: [
      "外部调查 Agent 查询 Trace、日志、配置与连接池指标；Canvas 只接收对应 Action / Observation。",
      "UI 可展示 <code>db.acquire=3.02s</code>、连接池 <code>80 → 8</code> 等结果和会话状态。",
      "若后端发送 Condensation 事件，类型契约可标记 <code>forgotten_event_ids</code>；当前 chat renderer 不直接展示该事件，若需呈现须补充 UI。",
      "Agent 循环、工具鉴权、原始事件存储和恢复均需在 Agent Server / SDK 侧另行审查。",
    ],
    rpcVerdict:
      "当前仓库可借鉴的是“事件协议与控制面可视化”，不是已经确认的 RPC 调查 Agent 实现。",
    rpcRefs: [
      architecture,
      event,
      condensation,
      eventsApi,
      eventRenderer,
      metricsAggregator,
    ],
    rpcClaims: [
      claim(
        "该 RPC 页面只映射 Canvas 对外部 Agent Server 事件的展示与查询能力。",
        architecture,
        "机制映射",
        "Agent Canvas architecture",
      ),
      claim(
        "Canvas 可以消费调查动作、观察和状态事件，但当前仓库不能证明这些调查如何执行。",
        event,
        "机制映射",
        "OpenHandsEvent",
      ),
      claim(
        "CondensationEvent 提供 forgotten_event_ids 类型契约，但当前 chat 事件渲染规则不会直接渲染该事件。",
        eventRenderer,
        "代码可核",
        "shouldRenderEvent",
      ),
      claim(
        "会话指标工具会汇总 usage_to_metrics 中的各项指标，不能据此写成 Agent 与 Condenser 的两套独立 UI。",
        metricsAggregator,
        "代码可核",
        "getCombinedMetrics",
      ),
      claim(
        "RPC 数据源工具、后端 Agent 循环、沙箱、鉴权和恢复必须由外部 Agent Server/SDK 提供。",
        eventsApi,
        "机制映射",
        "event-service.api",
      ),
    ],
    takeawaysTitle: "OpenHands：可借鉴事件语言与模型视图，运行事实需追溯",
    takeawaysLede:
      "把前端协议写成后端运行事实，是本次代码实证中风险最大的一类错误。",
    borrow: [
      "把 Action、Observation、State、Error 与 Condensation 定义为清晰事件契约。",
      "问题定位控制面应分别记录调查 Agent 与上下文压缩组件的 token、成本和运行状态；当前 Canvas 只能作为会话级指标与字段契约参考。",
      "用“LLM View”概念明确模型当前看到的事件范围，而不是把摘要当原始事实。",
      "支持历史事件分页查询，便于控制面审计与回看。",
    ],
    limits: [
      "前端能查询历史，不证明服务端采用 <span class=\"nowrap\">append-only</span> 或不可变事件存储。",
      "Condensation 协议不等于具体 Condenser 算法已在本仓库实现。",
      "客户端 localStorage 元数据不是 Agent 权威状态或恢复 Checkpoint。",
      "未固定并审查 software-agent-sdk 前，不能宣称 Workspace、循环和恢复机制。",
    ],
    verdict:
      "借鉴 OpenHands Canvas 的事件协议和可观测控制面；核心运行时结论必须继续追到 Agent Server / SDK 的固定代码版本。",
    takeawayRefs: [
      event,
      condensation,
      state,
      eventsApi,
      eventRenderer,
      metricsAggregator,
    ],
    takeawayClaims: [
      claim(
        "OpenHands Canvas 的事件协议与 LLM View 概念可作为问题定位控制面的参考。",
        condensation,
        "机制映射",
        "CondensationEvent",
      ),
      claim(
        "当前仓库不能支持 append-only 事件存储、Workspace、Agent 循环或完整恢复的实现结论。",
        architecture,
        "未确认",
        "architecture.md",
      ),
      claim(
        "历史事件分页查询是前端可核能力，但其后端写入顺序与不可变性未从本仓库确认。",
        eventsApi,
        "代码可核",
        "event-service.api",
      ),
    ],
  });
}

// ---------------------------------------------------------------------------
// 16–18 · Cline
// ---------------------------------------------------------------------------

{
  const runtime = ref(
    "cline",
    "sdk/packages/agents/src/agent-runtime.ts",
    "396-735",
    "AgentRuntime",
  );
  const tools = ref(
    "cline",
    "sdk/packages/agents/src/agent-runtime.ts",
    "1291-1425",
    "工具执行",
  );
  const resume = ref(
    "cline",
    "apps/vscode/src/sdk/sdk-task-resume.ts",
    "1-70",
    "Task 恢复",
  );
  const compact = ref(
    "cline",
    "sdk/packages/core/src/extensions/context/compaction.ts",
    "235-340",
    "Auto Compact",
  );
  const checkpoint = ref(
    "cline",
    "sdk/packages/core/src/hooks/checkpoint-hooks.ts",
    "155-291",
    "Git Checkpoint",
  );
  const team = ref(
    "cline",
    "sdk/packages/core/src/extensions/tools/team/multi-agent.ts",
    "176-335",
    "AgentTeam",
  );
  const hydrate = ref(
    "cline",
    "sdk/packages/core/src/extensions/tools/team/multi-agent.ts",
    "702-790",
    "Team 状态恢复",
  );
  addReferenceProject({
    projectKey: "cline",
    mechanismTitle: "Cline：Session、Auto Compact 与 AgentTeam",
    mechanismLede:
      "当前固定版本已经具备真实多 Agent 运行时；旧的“Cline 只有单 Agent”结论不再成立。",
    mechanismSteps: [
      { title: "Session 输入", body: "Task ID、transcript 与 workspace" },
      { title: "AgentRuntime", body: "模型判断与工具调用循环", tone: "red" },
      { title: "运行时治理", body: "授权、hook、顺序/并行工具" },
      { title: "上下文与版本", body: "Auto Compact + Git Checkpoint", tone: "red" },
      { title: "可选 AgentTeam", body: "路由、串行或并行 Session" },
    ],
    mechanismCards: [
      {
        title: "Session 可恢复",
        body: "Task 恢复读取持久化 transcript，以原 Task ID 作为 sessionId，并用历史消息重新启动。",
      },
      {
        title: "上下文压缩是运行时能力",
        body: "每次模型请求前估算 token，达到阈值后执行 basic、agentic 或自定义压缩策略。",
      },
      {
        title: "Git Checkpoint 有精确时机",
        body: "根 Session 每个 run 的第一次模型调用前创建一次私有 Git ref 快照，不是每次工具调用后。",
      },
    ],
    mechanismNote:
      "AgentTeam 状态可导出和 hydrate；但恢复后的 teammate 只保留元数据且状态为 stopped，需要重新创建实际 Agent。",
    mechanismRefs: [runtime, tools, resume, compact, checkpoint, team, hydrate],
    mechanismClaims: [
      claim(
        "Cline SDK 以 Session 为运行边界，AgentRuntime 反复执行模型判断、工具调用和结果回填。",
        runtime,
        "代码可核",
        "AgentRuntime.execute",
      ),
      claim(
        "Auto Compact 在模型请求前估算 token 并按 basic、agentic 或自定义策略压缩上下文。",
        compact,
        "代码可核",
        "createContextCompactionPrepareTurn",
      ),
      claim(
        "Git Checkpoint 在根 Session 每个 run 的第一次模型调用前创建一次私有 ref 快照。",
        checkpoint,
        "代码可核",
        "createCheckpointHooks",
      ),
      claim(
        "当前 Cline 具备 AgentTeam，可路由、串行或并行运行多个独立 SessionRuntime。",
        team,
        "代码可核",
        "AgentTeam",
      ),
    ],
    rpcTitle: "RPC 超时如何落入 Cline 的 Session 与 AgentTeam",
    rpcLede:
      "Cline 可以承载模型—工具—观察循环和多 Session 分工，但生产观测工具与 DiagnosisState 必须外置。",
    codeProvides: [
      "可恢复 Session、transcript 与模型工作上下文压缩。",
      "工具 hook、策略判断、人工授权与顺序/并行执行。",
      "AgentTeam 多 Session 协作，以及 Git workspace Checkpoint。",
    ],
    rpcSteps: [
      "根 Session 接收告警，调用外接 Trace 工具发现 <code>db.acquire=3.02s</code>。",
      "可由 AgentTeam 分配日志、发布配置和连接池指标核验；各 Session 返回结果。",
      "根 Session 汇总连接池 <code>80 → 8</code> 与 <code>active=8 / waiters=120</code>，但应同步写入外部 <code>DiagnosisState</code>。",
      "Task transcript 恢复协作历史；Git Checkpoint 只恢复工作区，不恢复外部查询副作用或已验证事实。",
    ],
    rpcVerdict:
      "Cline 的优势是 Session 边界、上下文治理和真实 AgentTeam；诊断事实不能只留在 transcript 或 Git 中。",
    rpcRefs: [runtime, tools, team, checkpoint, resume],
    rpcClaims: [
      claim(
        "该 RPC 流程把告警放入 Cline Session，并使用外接观测工具与可选 AgentTeam 进行机制映射。",
        runtime,
        "机制映射",
        "AgentRuntime.execute",
      ),
      claim(
        "多个核验任务可由 AgentTeam 承载，但跨 Session 事实汇总和冲突规则需要外部 DiagnosisState。",
        team,
        "机制映射",
        "AgentTeam",
      ),
      claim(
        "Git Checkpoint 恢复工作区版本，不等于恢复外部调查副作用或证据审核状态。",
        checkpoint,
        "机制映射",
        "createCheckpointHooks",
      ),
    ],
    takeawaysTitle: "Cline：Case 边界、模型上下文与工程版本要分层",
    takeawaysLede:
      "Session、transcript、Auto Compact 生成的模型工作上下文、Git Checkpoint 与 Team state 各自解决不同问题，不应混成一种“记忆”。",
    borrow: [
      "用稳定 Case/Session ID 固定一次调查边界，并支持显式恢复。",
      "把 Auto Compact 作为运行时策略，原始记录与模型工作上下文分离。",
      "危险工具经过 hook、策略与人工授权，再决定顺序或并行执行。",
      "用 AgentTeam 明确父子 Session、路由与协作关系。",
    ],
    limits: [
      "AgentRuntime snapshot 不是业务级 DiagnosisState。",
      "Git Checkpoint 不是 Evidence，也不是每次工具调用后的事务快照。",
      "Team hydrate 后 teammate 处于 stopped，不能宣称原执行点自动继续。",
      "多 Agent transcript 不能替代事实冲突处理、证据优先级规则和审核规则。",
    ],
    verdict:
      "借鉴 Cline 的 Session 生命周期和 AgentTeam，但把故障事实、假设与 Evidence 放在独立、可审计的 DiagnosisState 中。",
    takeawayRefs: [runtime, resume, compact, checkpoint, hydrate],
    takeawayClaims: [
      claim(
        "Cline 的可恢复 Session 和上下文压缩适合承载 Case 生命周期与模型输入治理。",
        resume,
        "机制映射",
        "prepareTaskResumeStartInput",
      ),
      claim(
        "Git Checkpoint 只处理工作区版本，不能当作诊断证据或完整 Agent 恢复。",
        checkpoint,
        "机制映射",
        "createCheckpointHooks",
      ),
      claim(
        "AgentTeam hydrate 仅恢复元数据，teammate 状态为 stopped，需要重建实际 Agent。",
        hydrate,
        "代码可核",
        "hydrateState",
      ),
    ],
  });
}

{
  const lifecycle = ref(
    "opensre",
    "tools/investigation/lifecycle.py",
    "27-67",
    "run_connected_investigation",
  );
  const plan = ref(
    "opensre",
    "tools/investigation/stages/plan_evidence/node.py",
    "25-179",
    "plan_actions",
  );
  const select = ref(
    "opensre",
    "tools/investigation/stages/gather_evidence/tools.py",
    "61-126",
    "工具筛选",
  );
  const diagnose = ref(
    "opensre",
    "tools/investigation/stages/diagnose/node.py",
    "24-85",
    "结论整理",
  );
  addSlide({
    projectKey: "opensre",
    localPage: 2,
    section: "外层流程",
    eyebrow: "DETERMINISTIC OUTER PIPELINE",
    title: "OpenSRE 如何按六步处理一条告警",
    lede:
      "第 3 步只限定可用工具；第 4 步才由 Agent 在工具范围内动态排查。外层流程不是多 Agent 编排。",
    body: `
      ${flow([
        { title: "确认系统接入", body: "<code>resolve_integrations</code><br>识别当前可用连接" },
        { title: "解析告警", body: "<code>extract_alert</code><br>提取服务、错误与时间" },
        { title: "确定工具范围", body: "<code>plan_actions</code><br>按固定规则选工具名", tone: "red" },
        { title: "Agent 排查", body: "<code>agent.run</code><br>模型决定查询与参数", tone: "red" },
        { title: "整理 RCA", body: "<code>diagnose</code><br>整理最终文本，不再排查" },
        { title: "交付报告", body: "<code>deliver</code><br>生成、保存或发送报告", tone: "black" },
      ])}
      <div class="split-grid wide-left">
        <div class="panel red" data-anim>
          <div class="panel-head"><h3>第 3 步：程序先限定工具范围</h3><span>RULE-BASED</span></div>
          <ul class="clean-list">
            <li>输出主要是工具名称短名单，不生成根因假设、调查步骤或具体查询参数。</li>
            <li>默认最多选择 <b>10</b> 个工具，可配置为 1–50；实际入选数量可能更少。</li>
            <li>计划阶段建议的时间范围和返回条数目前不会自动写入实际工具参数。</li>
          </ul>
        </div>
        <div class="panel dark" data-anim>
          <div class="panel-head"><h3>第 4 步：Agent 在范围内动态排查</h3><span>MODEL + RUNTIME</span></div>
          <ul class="clean-list">
            <li>接收告警、工具范围和已有证据。</li>
            <li>模型选择下一工具与参数；运行时代为执行。</li>
            <li><code>diagnose</code> 只整理最终结论，不是第二个调查或审核 Agent。</li>
          </ul>
        </div>
      </div>`,
    refs: [lifecycle, plan, select, diagnose],
    claims: [
      claim(
        "OpenSRE 外层由六个 Python 阶段按固定顺序运行，不是 LangGraph 或多 Agent 编排图。",
        lifecycle,
        "代码可核",
        "run_connected_investigation",
      ),
      claim(
        "计划阶段按固定规则选择工具名，默认上限 10，可配置并限制在 1–50。",
        plan,
        "代码可核",
        "plan_actions",
      ),
      claim(
        "计划阶段的 retrieval_controls 没有在 Agent/工具执行链中被强制写入真实调用参数。",
        plan,
        "代码可核",
        "_build_retrieval_controls",
      ),
      claim(
        "diagnose 负责整理 Agent 最终结论，不继续发起调查，也不能视为独立证据复核。",
        diagnose,
        "代码可核",
        "diagnose",
      ),
    ],
  });
}

{
  const agent = ref(
    "opensre",
    "tools/investigation/stages/gather_evidence/agent.py",
    "120-410",
    "Agent 循环",
  );
  const seed = ref(
    "opensre",
    "tools/investigation/stages/gather_evidence/tools.py",
    "191-249",
    "预置首查",
  );
  const execution = ref(
    "opensre",
    "core/execution.py",
    "91-160",
    "工具执行",
  );
  const cache = ref(
    "opensre",
    "tools/investigation/stages/gather_evidence/loop.py",
    "20-149",
    "查询结果缓存",
  );
  const context = ref(
    "opensre",
    "core/context_budget.py",
    "392-437",
    "上下文预算",
  );
  const phasePrompt = ref(
    "opensre",
    "tools/investigation/stages/gather_evidence/incident_command.py",
    "1-22",
    "阶段复盘提示",
  );
  addSlide({
    projectKey: "opensre",
    localPage: 3,
    section: "Agent 实现逻辑",
    eyebrow: "CONNECTEDINVESTIGATIONAGENT.RUNTIME",
    title: "OpenSRE 的故障定位 Agent 如何运行",
    lede:
      "上下文裁剪、重复查询缓存和工具执行都是 OpenSRE 运行时代码提供的能力，不是大模型自身“自带”。",
    body: `
      <div class="formula" data-anim>
        <code>ConnectedInvestigationAgent</code>
        <i data-lucide="equals"></i>
        <span>排查提示词</span><i data-lucide="plus"></i><span>许可工具</span><i data-lucide="plus"></i>
        <span>模型调用循环</span><i data-lucide="plus"></i><span>工具执行</span><i data-lucide="plus"></i><span>证据与运行控制</span>
      </div>
      <div class="runtime-loop">
        <div class="loop-node"><strong>准备输入与预置首查</strong><p>读取告警、工具范围与已有状态；部分告警来源在首次模型调用前执行预置初始查询。</p></div>
        <i data-lucide="arrow-right"></i>
        <div class="loop-node red"><strong>模型决定下一步</strong><p>根据当前证据提出假设，选择下一工具，并生成参数和时间范围。</p></div>
        <i data-lucide="arrow-right"></i>
        <div class="loop-node"><strong>运行时校验并执行</strong><p>校验许可与参数、注入受保护凭据；同一轮安全调用可并行，错误作为结果返回。</p></div>
        <i data-lucide="arrow-right"></i>
        <div class="loop-node red"><strong>记录证据并继续</strong><p>新结果加入模型消息和 <code>EvidenceEntry</code>；模型调整假设，或停止调用工具并给出结论。</p></div>
      </div>
      <div class="return-arrow" data-anim><i data-lucide="corner-down-left"></i><span>查询结果回到同一个 Agent 的下一轮；最多 <b>20</b> 次模型调用</span></div>
      <div class="control-grid">
        <div class="control-card" data-anim><strong>上下文长度控制</strong><p>每轮计算 system prompt、工具定义与消息长度，并预留 16,000 token 输出空间。超限时先删除低价值工具往返，最后才截断最长消息；这不是自动摘要。</p></div>
        <div class="control-card" data-anim><strong>相同查询结果复用</strong><p>缓存键为工具名＋模型或预置首查提供的规范化输入参数，不包含运行时注入的受保护连接字段。完全相同调用复用结果；参数或时间范围变化则重新执行。缓存仅作用于本次调查，容量 128 项、约 200 万字符，按 LRU 淘汰。</p></div>
        <div class="control-card" data-anim><strong>停滞与结束控制</strong><p>连续两轮只有重复调用时，下一轮暂不提供工具并要求结论。提示词引导“初步判断—假设—验证—建议”，但不是代码硬编码状态机；代码中的 post-triage checkpoint 是阶段复盘提示，不是 durable Checkpoint。</p></div>
      </div>`,
    refs: [agent, seed, execution, cache, context, phasePrompt],
    claims: [
      claim(
        "部分告警来源在首次模型调用前由代码执行预置首查，首查结果进入消息与证据。",
        seed,
        "代码可核",
        "build_seed_calls",
      ),
      claim(
        "运行时校验模型参数、注入受保护连接信息，并执行同轮工具调用；工具并行不等于多 Agent。",
        execution,
        "代码可核",
        "execute_tool_calls",
      ),
      claim(
        "本次调查内完全相同的工具名与规范化输入参数调用会复用缓存结果；运行时注入的受保护连接字段不参与缓存键，缓存按条数与字符量采用 LRU 控制。",
        cache,
        "代码可核",
        "InvestigationToolCallCache",
      ),
      claim(
        "上下文超限时由代码删除低价值工具往返或截断消息，不是模型自动总结。",
        context,
        "代码可核",
        "enforce_context_budget",
      ),
      claim(
        "Agent 调查最多进行 20 次模型调用；提示词阶段不是代码状态机。",
        agent,
        "代码可核",
        "ConnectedInvestigationAgent.run",
      ),
      claim(
        "post-triage checkpoint 只是在首轮新工具结果后追加阶段复盘提示，不是可持久化并恢复执行位置的 durable Checkpoint。",
        phasePrompt,
        "代码可核",
        "POST_TRIAGE_CHECKPOINT",
      ),
    ],
    layout: "H37",
  });
}

{
  const lifecycle = ref(
    "opensre",
    "tools/investigation/lifecycle.py",
    "27-67",
    "外层流程",
  );
  const agent = ref(
    "opensre",
    "tools/investigation/stages/gather_evidence/agent.py",
    "120-410",
    "Agent 循环",
  );
  const execution = ref(
    "opensre",
    "core/execution.py",
    "91-160",
    "工具执行",
  );
  addSlide({
    projectKey: "opensre",
    localPage: 4,
    section: "RPC 超时案例",
    eyebrow: "MECHANISM MAPPING · TEACHING CASE",
    title: "以 RPC 超时为例：Agent 如何逐步定位根因",
    lede:
      "演示场景为便于讲解而构造；OpenSRE 没有内置针对 RPC 超时的固定定位流程。",
    body: `
      ${rpcInput}
      <div class="rpc-chain">
        <article><strong>预置首查 / 首轮研判</strong><span>告警与初始证据</span></article><i data-lucide="arrow-right"></i>
        <article><strong>Trace 查询</strong><span><code>db.acquire=3.02s</code></span></article><i data-lucide="arrow-right"></i>
        <article><strong>日志验证</strong><span>数据库连接获取超时</span></article><i data-lucide="arrow-right"></i>
        <article><strong>配置关联</strong><span>连接池上限 80 → 8</span></article><i data-lucide="arrow-right"></i>
        <article class="red"><strong>指标交叉核对</strong><span><code>active=8 · waiters=120</code></span></article>
      </div>
      <div class="split-grid wide-left">
        <div class="panel red" data-anim>
          <div class="panel-head"><h3>同一个 Agent 在每轮决定“下一查什么”</h3><span>MODEL DECISION</span></div>
          <ul class="clean-list">
            <li>Trace 显示主要等待集中在库存服务获取数据库连接。</li>
            <li>模型提出“连接池等待”假设，随后查询日志与发布配置。</li>
            <li>配置变更与连接池指标支持该假设；SQL、CPU、网络和其他下游没有对应异常，降低其他原因可能性。</li>
          </ul>
        </div>
        <div class="panel dark" data-anim>
          <div class="panel-head"><h3>外层收尾</h3><span>PIPELINE</span></div>
          <div class="h-stack">
            <div class="formula"><span>Agent 最终结论</span><i data-lucide="arrow-right"></i><span>结论整理（<code>diagnose</code>）</span><i data-lucide="arrow-right"></i><span>报告交付（<code>deliver</code>）</span></div>
            <p><strong>因果链：</strong>连接池上限下调 → 连接池耗尽 → <code>db.acquire</code> 等待 → 库存服务响应超过 3 秒 → 上游 RPC 超时。</p>
          </div>
        </div>
      </div>
      ${rpcBoundary("OpenSRE")}
    `,
    refs: [lifecycle, agent, execution],
    claims: [
      claim(
        "该 RPC 页面是把 OpenSRE 的固定外层流程、模型—工具循环和证据记录机制映射到统一教学材料，不是官方案例。",
        agent,
        "机制映射",
        "ConnectedInvestigationAgent.run",
      ),
      claim(
        "OpenSRE 可通过接入的 Trace、日志、配置和指标工具承载该排查过程，但这些具体适配器是案例前提。",
        execution,
        "机制映射",
        "execute_tool_calls",
      ),
      claim(
        "Agent 最终文本返回后，外层再执行 diagnose 与 deliver；默认流程不等于自动回滚或独立审核。",
        lifecycle,
        "机制映射",
        "run_connected_investigation",
      ),
    ],
  });
}

// ---------------------------------------------------------------------------
// 05–09 · OpenDerisk
// ---------------------------------------------------------------------------

{
  const agentCore = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/core/agent.py",
    "17-279",
    "Agent / AgentContext",
  );
  const loop = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/core/base_agent.py",
    "881-1295",
    "ConversableAgent.generate_reply",
  );
  const delegate = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/expand/actions/agent_action.py",
    "17-191",
    "agent_start",
  );
  const resourceInject = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/core/base_agent.py",
    "821-836",
    "AppResource 工具注入",
  );
  const readme = externalRef(
    "derisk",
    `https://github.com/${projects.derisk.repo}/blob/${projects.derisk.sha}/README.md`,
    "项目说明",
    "文档陈述",
  );
  addSlide({
    projectKey: "derisk",
    localPage: 1,
    section: "项目定位",
    eyebrow: "OPEN-SOURCE AGENT FRAMEWORK · RISK INTELLIGENCE CONTEXT",
    title: "OpenDerisk：具备通用 Agent 循环与委派能力的开源框架",
    lede:
      "代码能确认的是通用 Agent、工具、Memory、会话上下文和子 Agent 委派；“完整 RCA 产品能力”需要与 README、论文陈述分开。",
    body: `
      <div class="definition-band" data-anim>
        <strong>代码定位</strong>
        <p><code>AgentContext</code> 管理会话、轮数、token、模型参数与运行环境；<code>ConversableAgent</code> 执行 thinking → act → verify 循环。</p>
      </div>
      <div class="fact-grid">
        ${factCard("01", "通用 Agent 运行时", "开源代码提供 Agent 接口、模型调用、工具权限与执行、Memory 写入和失败重试。", "red")}
        ${factCard("02", "有条件的子 Agent 委派", "绑定相应 <code>AppResource</code> 后，主 Agent 可用 <code>agent_start</code> 把完整任务和背景交给上下文隔离的已注册子 Agent。")}
        ${factCard("03", "RCA 定位来自项目陈述", "README/论文把项目定位于风险智能和根因分析；不能反向证明所有论文机制都已在当前代码启用。")}
      </div>
      <div class="chip-row" data-anim>
        ${chip("derisk-ai 独立社区仓库", "dark")}
        ${chip("MIT")}
        ${chip("AgentContext")}
        ${chip("thinking → act → verify", "red")}
        ${chip("论文与代码分区")}
      </div>
      <div class="note-strip" data-anim>身份口径：可写“项目成员具有蚂蚁集团生产实践背景（文档/论文陈述）”，不写成“阿里云产品”或“蚂蚁集团官方开源项目”。</div>`,
    refs: [agentCore, loop, delegate, resourceInject, readme],
    claims: [
      claim(
        "OpenDerisk 的 AgentContext 管理会话、轮数、token、模型参数和运行环境。",
        agentCore,
        "代码可核",
        "AgentContext",
      ),
      claim(
        "ConversableAgent 实现 thinking、act、verify 的模型—工具循环，并在失败时写入 Memory 后重试。",
        loop,
        "代码可核",
        "ConversableAgent.generate_reply",
      ),
      claim(
        "agent_start 只在绑定相应 AppResource 时注入，并把任务交给已注册的上下文隔离子 Agent。",
        resourceInject,
        "代码可核",
        "_inject_resource_based_tools",
      ),
      claim(
        "OpenDerisk 的风险智能与 RCA 产品定位来自项目文档，不能用于证明所有论文机制已在开源执行链中启用。",
        readme,
        "文档陈述",
        "README",
      ),
    ],
  });
}

{
  const loop = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/core/base_agent.py",
    "537-1295",
    "工具注入与 Agent 循环",
  );
  const delegate = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/expand/actions/agent_action.py",
    "17-244",
    "AgentStart",
  );
  const resourceInject = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/core/base_agent.py",
    "821-836",
    "AppResource 工具注入",
  );
  const act = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/core/base_agent.py",
    "1727-1815",
    "ConversableAgent.act",
  );
  const team = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/core/base_team.py",
    "53-151",
    "Team / ManagerAgent",
  );
  const mode = ref(
    "derisk",
    "packages/derisk-serve/src/derisk_serve/agent/team/base.py",
    "8-74",
    "TeamMode",
  );
  addSlide({
    projectKey: "derisk",
    localPage: 2,
    section: "代码架构",
    eyebrow: "ACTIVE EXECUTION PATHS",
    title: "代码中真正运行的主链：单 Agent 循环，可按需委派子 Agent",
    lede:
      "基础 Team 抽象确实存在；当前可验证的协作动作是有条件注入、并等待结果返回的 <code>agent_start</code>，不能仅凭类名画出论文式中央编排。",
    body: `
      <div class="runtime-loop">
        <div class="loop-node"><strong>AgentContext</strong><p>会话、轮数、token、模型、资源与运行环境。</p></div>
        <i data-lucide="arrow-right"></i>
        <div class="loop-node red"><strong>ConversableAgent</strong><p>thinking → act → verify；失败写入 Memory 并按配置重试。</p></div>
        <i data-lucide="arrow-right"></i>
        <div class="loop-node"><strong>工具与知识</strong><p>file、shell、network、knowledge 等工具经过权限检查；同轮 action 按声明顺序执行。</p></div>
        <i data-lucide="arrow-right"></i>
        <div class="loop-node red"><strong>有条件的 agent_start</strong><p>绑定 AppResource 后，把完整任务交给已注册的隔离子 Agent，并等待结果返回。</p></div>
      </div>
      <div class="return-arrow" data-anim><i data-lucide="corner-down-left"></i><span>任务、消息与 ActionOutput 写入 Memory，模型继续下一轮或结束</span></div>
      <div class="split-grid">
        <div class="panel dark" data-anim>
          <div class="panel-head"><h3>代码可以确认</h3><span>ACTIVE</span></div>
          <ul class="clean-list">
            <li>工具权限检查；同一 action 列表按顺序执行。</li>
            <li>单 Agent 循环与失败重试。</li>
            <li>绑定 <code>AppResource</code> 后注入 <code>agent_start</code>。</li>
            <li><code>Team</code> / <code>ManagerAgent</code> 基础抽象。</li>
          </ul>
        </div>
        <div class="panel soft" data-anim>
          <div class="panel-head"><h3>不能从类名直接推出</h3><span>BOUNDARY</span></div>
          <ul class="clean-list">
            <li><code>TeamMode</code> 的值是 <code>AUTO_PLAN / AWEL_LAYOUT / SINGLE_AGENT / NATIVE_APP</code>。</li>
            <li>指定 SHA 中未找到可运行的 <code>GroupMode</code>。</li>
            <li><code>sync/background</code> 虽在参数模型中声明，执行路径没有据此实现 fire-and-forget。</li>
            <li>不能把基础 Team 抽象写成专家投票、冲突仲裁或自动证据汇总已经实现。</li>
          </ul>
        </div>
      </div>`,
    refs: [loop, act, delegate, resourceInject, team, mode],
    claims: [
      claim(
        "OpenDerisk 运行时支持工具权限检查；同一 action 列表按声明顺序执行，并把前一 action 输出传给下一 action。",
        act,
        "代码可核",
        "ConversableAgent.act",
      ),
      claim(
        "agent_start 将完整任务和背景交给已注册的上下文隔离子 Agent，实际路径等待子 Agent 返回。",
        delegate,
        "代码可核",
        "AgentAction.run",
      ),
      claim(
        "agent_start 仅在绑定相应 AppResource 时注入。",
        resourceInject,
        "代码可核",
        "_inject_resource_based_tools",
      ),
      claim(
        "sync/background 字段虽然在 AgentStart 参数中声明，但当前执行路径没有据此形成独立的 fire-and-forget 分支。",
        delegate,
        "代码可核",
        "AgentStart / AgentAction.run",
      ),
      claim(
        "Team 与 ManagerAgent 是基础抽象，不能据此证明完整自动协作模式已投入运行。",
        team,
        "代码可核",
        "Team / ManagerAgent",
      ),
      claim(
        "当前 TeamMode 枚举值为 AUTO_PLAN、AWEL_LAYOUT、SINGLE_AGENT、NATIVE_APP，不能解释成与 GroupMode 并列的论文协作模式。",
        mode,
        "代码可核",
        "TeamMode",
      ),
    ],
    layout: "H37",
  });
}

{
  const loop = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/core/base_agent.py",
    "1376-1409",
    "特定 retry 恢复",
  );
  const gptsMemory = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/core/memory/gpts/gpts_memory.py",
    "425-950",
    "GptsMemory",
  );
  const history = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/core/memory/session_history.py",
    "1-380",
    "SessionHistoryManager",
  );
  const window = ref(
    "derisk",
    "packages/derisk-core/src/derisk/context/window.py",
    "1-71",
    "ContextWindow",
  );
  const retryService = ref(
    "derisk",
    "packages/derisk-serve/src/derisk_serve/agent/agents/chat/agent_chat.py",
    "2409-2415",
    "retry_chat",
  );
  addSlide({
    projectKey: "derisk",
    localPage: 3,
    section: "上下文、知识与记录",
    eyebrow: "MEMORY AND CONTEXT ARE NOT EVIDENCE GOVERNANCE",
    title: "OpenDerisk 的推理、Memory 与证据边界",
    lede:
      "开源代码能确认消息、任务与工具结果的记录、持久消息加载和特定 retry 恢复；不能把它写成任意中断点的 Checkpoint/resume。",
    body: `
      <div class="code-vs-model">
        <div class="role-card model" data-anim>
          <h3>模型 / Agent 负责</h3>
          <ul class="clean-list">
            <li>读取当前任务、消息与工具描述。</li>
            <li>在 thinking → act → verify 中选择动作。</li>
            <li>根据 ActionOutput 调整判断或重试。</li>
          </ul>
        </div>
        <i data-lucide="arrow-left-right"></i>
        <div class="role-card" data-anim>
          <h3>运行时 / Memory 负责</h3>
          <ul class="clean-list">
            <li>把任务、消息与执行结果写入缓存和可选持久层。</li>
            <li>Session History 做冷热分层、token 限制与规则摘要。</li>
            <li>缓存为空可加载持久消息；特定 retry 首轮可复用本 Agent 最近回复。</li>
          </ul>
        </div>
      </div>
      <div class="mini-matrix" data-anim>
        <div class="head">机制</div><div class="head">代码中可核</div><div class="head">不能等价为</div><div class="head">对定位框架的要求</div>
        <div class="rowhead">Memory</div><div>任务、消息、ActionOutput 持久化</div><div>不可变的诊断证据账本</div><div>单独定义 Evidence 与来源哈希</div>
        <div class="rowhead">Context</div><div>冷热分层、token 限制、规则摘要</div><div>所有历史事实永久进入模型</div><div>权威状态与模型输入视图分离</div>
        <div class="rowhead">Recovery</div><div>持久消息加载；特定 retry 最近回复</div><div>任意中断点 resume 或副作用重放</div><div>定义幂等、Checkpoint 与恢复边界</div>
        <div class="rowhead">Evidence</div><div>消息和执行记录可追溯</div><div>Claim—Evidence 充分性审核</div><div>增加验证状态、反证和审核规则</div>
      </div>
      <div class="note-strip" data-anim>恢复边界：服务层 <code>retry_chat()</code> 当前仍为空实现；指定 SHA 未识别出 OpenSRE 式重复工具调用缓存，<code>RL Dynamic</code> 也不进入开源实现主图。</div>`,
    refs: [gptsMemory, history, loop, retryService, window],
    claims: [
      claim(
        "GptsMemory 提供会话缓存、持久消息加载和异步持久写入。",
        gptsMemory,
        "代码可核",
        "GptsMemory",
      ),
      claim(
        "SessionHistoryManager 实现冷热分层、token 限制和规则摘要，但冷数据归档仍留有 TODO。",
        history,
        "代码可核",
        "SessionHistoryManager",
      ),
      claim(
        "最近模型回复恢复只用于特定 retry 首轮，不能称为任意中断点 resume。",
        loop,
        "代码可核",
        "_update_recovering / _recovery_message",
      ),
      claim(
        "服务层 retry_chat 当前仍为空实现。",
        retryService,
        "代码可核",
        "retry_chat",
      ),
      claim(
        "当前代码未确认专门的 Claim—Evidence 领域实体或 OpenSRE 式重复工具调用缓存。",
        window,
        "未确认",
        "ContextWindow",
      ),
    ],
  });
}

{
  const loop = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/core/base_agent.py",
    "881-1295",
    "Agent 循环",
  );
  const delegate = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/expand/actions/agent_action.py",
    "17-191",
    "agent_start",
  );
  const resourceInject = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/core/base_agent.py",
    "821-836",
    "AppResource 工具注入",
  );
  const history = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/core/memory/session_history.py",
    "1-380",
    "会话历史",
  );
  addSlide({
    projectKey: "derisk",
    localPage: 4,
    section: "RPC 超时案例",
    eyebrow: "MECHANISM MAPPING · NOT NATIVE SRE",
    title: "同一 RPC 超时材料，OpenDerisk 的代码机制怎样承载",
    lede:
      "案例只使用当前代码可确认的 Agent 循环、工具调用、Memory 与有条件注入的 <code>agent_start</code>，不画入未确认的 GroupMode 或 RL Dynamic。",
    body: `
      ${rpcInput}
      <div class="rpc-lanes">
        <div class="rpc-lane code" data-anim>
          <h3>项目代码提供</h3>
          <ol>
            <li><code>AgentContext</code> 承载会话、模型与运行资源。</li>
            <li>Agent 在 thinking → act → verify 中调用外接工具。</li>
            <li>消息、任务与 ActionOutput 写入 Memory。</li>
            <li>绑定 AppResource 且目标已注册时，可用 <code>agent_start</code> 委派隔离子 Agent，并等待返回。</li>
          </ol>
        </div>
        <div class="rpc-lane add" data-anim>
          <h3>机制映射：从现象到根因</h3>
          <ol>
            <li>主 Agent 调用 Trace 工具，看到 <code>db.acquire=3.02s</code>，提出连接池等待假设。</li>
            <li>可按顺序委派“日志核验”和“配置/指标核验”子 Agent；当前执行路径会等待每次委派返回。</li>
            <li>主 Agent 关联连接池上限 <code>80 → 8</code> 与 <code>active=8 / waiters=120</code>。</li>
            <li>需要自行实现结构化 Evidence、冲突处理、证据充分性与最终审核，不能只依赖 Memory 消息。</li>
          </ol>
        </div>
      </div>
      <div class="takeaway" data-anim><strong>映射结论</strong><p>OpenDerisk 提供“Agent 推理—工具执行—结果回填—可选子 Agent 委派”的通用骨架；RPC 语义、数据源适配和审核闭环都需问题定位框架补齐。</p></div>
      ${rpcBoundary("OpenDerisk")}
    `,
    refs: [loop, delegate, resourceInject, history],
    claims: [
      claim(
        "该 RPC 页面仅把 OpenDerisk 当前可核的 Agent 循环、Memory 和 agent_start 委派映射到统一排查材料。",
        loop,
        "机制映射",
        "ConversableAgent.generate_reply",
      ),
      claim(
        "日志与配置核验可以建模为 agent_start 子 Agent 委派；目标需已注册，当前执行路径等待结果返回。",
        delegate,
        "机制映射",
        "AgentAction.run",
      ),
      claim(
        "agent_start 需要绑定相应 AppResource 才会注入；多 Agent 汇总、冲突与终止规则仍需应用自行实现。",
        resourceInject,
        "机制映射",
        "_inject_resource_based_tools",
      ),
      claim(
        "Memory 可记录执行过程，但结构化 Evidence 和独立审核不是该映射可直接复用的现成能力。",
        history,
        "机制映射",
        "SessionHistoryManager",
      ),
    ],
  });
}

{
  const mode = ref(
    "derisk",
    "packages/derisk-serve/src/derisk_serve/agent/team/base.py",
    "8-74",
    "TeamMode",
  );
  const autoPlan = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/core/plan/auto/team_auto_plan.py",
    "1-340",
    "自动规划代码",
  );
  const reactPlan = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/core/plan/react/team_react_plan.py",
    "1-300",
    "ReAct Team Manager",
  );
  const resourceInject = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/core/base_agent.py",
    "821-836",
    "AppResource 工具注入",
  );
  const gptsMemory = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/core/memory/gpts/gpts_memory.py",
    "425-950",
    "GptsMemory",
  );
  const recovery = ref(
    "derisk",
    "packages/derisk-core/src/derisk/agent/core/base_agent.py",
    "1376-1409",
    "特定 retry 恢复",
  );
  const paper = externalRef(
    "derisk",
    "https://arxiv.org/abs/2510.13561",
    "OpenDerisk 论文",
    "论文陈述",
  );
  addSlide({
    projectKey: "derisk",
    localPage: 5,
    section: "开源与论文边界",
    eyebrow: "WHAT IS IMPLEMENTED · WHAT IS CLAIMED",
    title: "OpenDerisk：开源代码、论文陈述与未确认能力必须分区",
    lede:
      "评审时最重要的不是把能力清单写满，而是防止把类名、注释或论文效果误写成当前开源运行语义。",
    body: `
      <div class="truth-table">
        <div class="truth-col confirmed" data-anim>
          <h3>开源代码可直接确认</h3>
          <div class="truth-row"><b>CODE</b><p>通用 AgentContext 与 thinking → act → verify 循环。</p></div>
          <div class="truth-row"><b>CODE</b><p>工具权限、GptsMemory 持久消息与特定 retry 最近回复恢复。</p></div>
          <div class="truth-row"><b>CODE</b><p>绑定 AppResource 后注入 <code>agent_start</code>，并等待隔离子 Agent 返回。</p></div>
          <div class="truth-row"><b>CODE</b><p>Team / ManagerAgent 基础抽象与当前 <code>TeamMode</code> 枚举。</p></div>
        </div>
        <div class="truth-col limited" data-anim>
          <h3>必须降级为论文陈述或未确认</h3>
          <div class="truth-row"><b>PAPER</b><p><code>RL Dynamic</code>、内部生产平台、准确率和规模数字。</p></div>
          <div class="truth-row"><b>NOT FOUND</b><p>指定 SHA 中未找到可运行的 <code>GroupMode</code>。</p></div>
          <div class="truth-row"><b>COMMENTED</b><p>自动规划与 ReAct Team Manager 的主体实现处于注释状态。</p></div>
          <div class="truth-row"><b>NOT RESUME</b><p>消息加载和特定 retry 不是任意中断点 Checkpoint/resume。</p></div>
          <div class="truth-row"><b>NOT PROVEN</b><p>专家投票、冲突仲裁、完整 Claim—Evidence 审核闭环。</p></div>
        </div>
      </div>
      <div class="split-grid">
        <div class="takeaway" data-anim><strong>值得借鉴</strong><p>通用 Agent 运行时、子 Agent 上下文隔离、会话历史分层和工具权限边界。</p></div>
        <div class="takeaway" data-anim><strong>不应照搬</strong><p>用 Agent 数量替代验证规则，或把论文效果直接当作开源版本可复现结果。</p></div>
      </div>`,
    refs: [mode, resourceInject, gptsMemory, recovery, autoPlan, reactPlan, paper],
    claims: [
      claim(
        "OpenDerisk 当前 TeamMode 的枚举语义不能等价为论文中的 GroupMode 协作模式。",
        mode,
        "代码可核",
        "TeamMode",
      ),
      claim(
        "agent_start 需要绑定相应 AppResource 才会注入，实际执行路径等待子 Agent 返回。",
        resourceInject,
        "代码可核",
        "_inject_resource_based_tools",
      ),
      claim(
        "GptsMemory 可加载持久消息，并可异步写入持久层。",
        gptsMemory,
        "代码可核",
        "GptsMemory",
      ),
      claim(
        "最近回复恢复只出现在特定 retry 路径，不是任意中断点 Checkpoint/resume。",
        recovery,
        "代码可核",
        "_update_recovering / _recovery_message",
      ),
      claim(
        "指定 SHA 中自动规划与 ReAct Team Manager 主体实现处于注释状态，不能进入开源运行主图。",
        autoPlan,
        "代码可核",
        "team_auto_plan.py",
      ),
      claim(
        "RL Dynamic、内部生产能力和效果数字只作为论文陈述，不用于证明当前开源执行路径。",
        paper,
        "论文陈述",
        "OpenDerisk paper",
      ),
      claim(
        "指定 SHA 中无法确认可运行的 GroupMode、专家投票或完整 Claim—Evidence 审核闭环。",
        reactPlan,
        "未确认",
        "team_react_plan.py",
      ),
    ],
    layout: "H24",
  });
}

buildOutputs();
