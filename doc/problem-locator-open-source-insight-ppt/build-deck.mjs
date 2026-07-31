import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const templatePath =
  process.env.PPT_TEMPLATE ||
  "C:/Users/admin/.codex/skills/guizang-ppt-skill/assets/template-huawei.html";
const lucidePath =
  process.env.PPT_LUCIDE ||
  "C:/Users/admin/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/lucide/dist/umd/lucide.min.js";
const outputPath = path.join(here, "index.html");
const opensreCommit = "4afe9572a45b41a92c65d9944a592c412760446a";
const opensreBase = `https://github.com/Tracer-Cloud/opensre/blob/${opensreCommit}`;

const brand = `
  <div class="brand-lockup" data-anim>
    <span class="brand-mark" aria-hidden="true"></span>
    <span>HUAWEI</span>
  </div>`;

const source = (items = []) => {
  if (!items.length) return "";
  const links = items
    .map(
      ([label, href]) =>
        `<a href="${href}" target="_blank" rel="noreferrer">${label}</a>`,
    )
    .join(" · ");
  return `<div class="deck-source">来源：${links} · 访问于 2026-07-30</div>`;
};

const chrome = (chapter, label) => `
  <div class="chrome" data-anim>
    <div class="l"><span class="square"></span><span>${chapter}</span></div>
    <span>${label}</span>
  </div>`;

const card = (title, body, icon = "circle-dot") => `
  <article class="card" data-anim>
    <div class="icon-disc"><i data-lucide="${icon}"></i></div>
    <div class="card-title">${title}</div>
    <div class="card-body">${body}</div>
  </article>`;

const bullets = (items) => `
  <div class="bullet-list">
    ${items
      .map(
        (item) => `
      <div class="bullet-item" data-anim>
        <span class="bullet-dot"></span>
        <div class="card-body">${item}</div>
      </div>`,
      )
      .join("")}
  </div>`;

const evidenceTag = (type, text) =>
  `<span class="evidence-tag ${type.toLowerCase()}">${type} · ${text}</span>`;

const slides = [];
const add = (html) => slides.push(html.trim());

add(`
<section class="slide hw-cover active" data-layout="H01">
  ${brand}
  <div class="cover-shell">
    <div class="cover-title-wrap">
      <div class="kicker" data-anim>技术诊断会 · 业界方案洞察（工作稿）</div>
      <h1 class="h-hero" data-anim>问题定位框架<br><span class="red-text">开源方案洞察</span></h1>
      <p class="lead cover-subtitle" data-anim>
        OpenSRE、OpenDerisk 与仓内 8 个 Agent 参考项目<br>
        从诊断闭环、状态、上下文、证据与恢复机制看设计取舍
      </p>
      <div class="tag-row" data-anim>
        <span class="plain-tag">49 页</span>
        <span class="plain-tag">10 个开源项目</span>
        <span class="plain-tag">框架定稿后补正式对比</span>
      </div>
    </div>
    <div class="cover-visual" data-anim>
      <div class="cover-badge"><span class="brand-mark" aria-hidden="true"></span></div>
      <div class="signal-orbit orbit-a"></div>
      <div class="signal-orbit orbit-b"></div>
      <div class="signal-node n1">案例</div>
      <div class="signal-node n2">证据</div>
      <div class="signal-node n3">Agent</div>
      <div class="signal-node n4">审核</div>
    </div>
  </div>
  <div class="cover-wave"></div>
  <div class="cover-meta">
    <strong>技术诊断会评审工作稿</strong>
    <span>2026-07-30 · 业界洞察已完成 / 当前设计未定稿</span>
  </div>
</section>`);

add(`
<section class="slide" data-layout="H15">
  ${brand}${chrome("00 · 执行摘要", "先看结论")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>四点核心判断</div>
      <h2 class="h-xl" data-anim>结论不是“选一个照搬”，而是组合机制</h2>
    </div>
    <div class="grid-4 same-size">
      ${card("OpenSRE", "<strong>证据驱动 + 受约束调查 + 评测闭环</strong><br>当前默认主链是单一调查 Agent；尚处公开早期测试阶段，暂无完整基准评测结果。", "search-check")}
      ${card("OpenDerisk", "<strong>专家协同 + 知识与上下文工程 + 人工审核</strong><br><code>derisk-ai</code> 社区仓具有蚂蚁集团作者与生产实践背景，但不代表集团官方发布。", "network")}
      ${card("8 个参考项目", "<strong>状态、事件、任务边界和模型输入各有强项</strong><br>没有一个项目覆盖问题定位全流程的业务编排与状态管理。", "boxes")}
      ${card("候选机制组合", "<strong>结构化状态 + 只追加事件 + 单次模型输入包 + 独立审核</strong><br>建议重点评审会话能否由固定输入重建；当前设计尚未定稿。", "git-merge")}
    </div>
    <div class="quote-band compact-quote" data-anim>
      先定义正确性、恢复和证据边界，再决定单 Agent、多 Agent 与会话策略。
    </div>
  </div>
  ${source([
    ["OpenSRE", "https://github.com/Tracer-Cloud/opensre"],
    ["OpenDerisk", "https://github.com/derisk-ai/OpenDerisk"],
    ["仓内调研", "../high-star-agent-context-strategy-survey.md"],
  ])}
</section>`);

add(`
<section class="slide" data-layout="H02">
  ${brand}${chrome("00 · 议程", "阅读路径")}
  <div class="agenda-layout">
    <div data-anim>
      <div class="kicker">评审路径</div>
      <h2 class="h-xl">五段式<br>评审路径</h2>
      <p class="lead">先明确研究对象与证据口径，再看关键机制、综合比较和当前设计的待决问题。</p>
    </div>
    <div class="agenda-list">
      <div class="agenda-item" data-anim><div class="idx">01</div><div class="txt"><span>OpenSRE · 项目概览、六步流程、Agent 实现逻辑与 RPC 示例</span><small>8 分钟</small></div></div>
      <div class="agenda-item" data-anim><div class="idx">02</div><div class="txt"><span>OpenDerisk · 阿里生态同类候选</span><small>8 分钟</small></div></div>
      <div class="agenda-item" data-anim><div class="idx">03</div><div class="txt"><span>仓内 8 个 Agent · 状态与上下文模式</span><small>备查</small></div></div>
      <div class="agenda-item" data-anim><div class="idx">04</div><div class="txt"><span>综合比较 · 10 个项目的能力边界</span><small>6 分钟</small></div></div>
      <div class="agenda-item" data-anim><div class="idx">05</div><div class="txt"><span>当前设计预比较 · 定稿后补充项</span><small>8 分钟</small></div></div>
      <div class="agenda-fastpath" data-anim><strong>35 分钟主讲路径</strong><span>第 1—18 页 → 第 44—49 页；第 19—43 页按需展开；问答 5 分钟</span></div>
    </div>
  </div>
</section>`);

add(`
<section class="slide" data-layout="H11">
  ${brand}${chrome("00 · 调研方法", "证据口径")}
  <div class="content-stack slide-body">
    <div class="grid-2">
      <div data-anim>
        <div class="kicker">证据优先</div>
        <h2 class="h-xl">可核事实、项目陈述与推断分层标注</h2>
        <p class="lead">架构评审的风险不只来自事实错误，也来自把“项目方自报结果”写成“公开验证结果”。</p>
      </div>
      <div class="legend-stack">
        <div class="legend-row" data-anim>${evidenceTag("F", "可核事实")}<span>代码、配置、许可证、接口契约或文档明确默认行为</span></div>
        <div class="legend-row" data-anim>${evidenceTag("S", "项目方陈述")}<span>论文、README、官网对能力、效果、案例与规模的陈述</span></div>
        <div class="legend-row" data-anim>${evidenceTag("I", "本次推断")}<span>由多项事实归纳的设计判断</span></div>
        <div class="legend-row" data-anim>${evidenceTag("T", "待定事项")}<span>当前框架尚未决策，不写成已落地能力</span></div>
      </div>
    </div>
    <div class="grid-3 same-size">
      ${card("先确认项目身份", "OpenSRE 存在同名项目；本稿始终指向 <strong>Tracer-Cloud/opensre</strong>。", "link")}
      ${card("规模不等于效果", "GitHub Star 数、集成数、日活用户和日运行次数，不能替代诊断准确率、证据完整度与成本。", "scale")}
      ${card("当前设计仅做预比较", "正式优劣矩阵待框架定稿后追加。", "hourglass")}
    </div>
  </div>
</section>`);

add(`
<section class="slide" data-layout="H05">
  ${brand}${chrome("01 · OpenSRE", "01 / 04 · 项目概览")}
  <div class="opensre-overview">
    <header data-anim>
      <div class="kicker">OPEN-SOURCE AI-SRE INVESTIGATION FRAMEWORK</div>
      <h2 class="h-xl">OpenSRE：开源的线上故障定位 Agent 框架</h2>
    </header>

    <div class="opensre-definition" data-anim>
      <strong>核心定位</strong>
      <p>OpenSRE 在通用大模型之上实现了一个线上故障定位 Agent。模型负责提出假设、选择下一步；OpenSRE 负责限定工具、执行查询、记录证据、控制循环并交付报告。</p>
    </div>

    <div class="opensre-overview-bars" data-anim>
      <div class="opensre-purpose-wrap">
        <div class="purpose-label">端到端用途</div>
        <div class="opensre-purpose" aria-label="OpenSRE 用途链">
          <span>接收告警</span><i data-lucide="arrow-right"></i>
          <span>查询运维数据</span><i data-lucide="arrow-right"></i>
          <span>验证根因假设</span><i data-lucide="arrow-right"></i>
          <span>输出故障分析报告</span>
        </div>
      </div>
      <div class="opensre-composition">
        <b>核心组成</b>
        <span>固定的外层流程</span><i data-lucide="plus"></i>
        <span>单个故障定位 Agent</span><i data-lucide="plus"></i>
        <span>运维工具接入</span><i data-lucide="plus"></i>
        <span>评测机制</span>
      </div>
    </div>

    <div class="grid-3 same-size opensre-feature-grid">
      <article class="card" data-anim>
        <div class="feature-index">01</div>
        <div class="card-title">接入现有系统</div>
        <p class="card-body">通过工具接入企业已有的日志、指标、调用链、<span class="nowrap">发布与配置</span>、<span class="nowrap">数据库</span>和云平台，无需迁移全部数据。</p>
        <div class="feature-tags"><span>日志</span><span>指标</span><span>调用链</span><span>发布与配置</span><span>数据库 / 云平台</span></div>
      </article>
      <article class="card" data-anim>
        <div class="feature-index">02</div>
        <div class="card-title">Agent 动态排查</div>
        <p class="card-body">不依赖固定流程的故障定位脚本；Agent 会在<span class="nowrap">可用工具范围</span>内提出假设、选择查询并逐轮验证。</p>
        <div class="feature-tags"><span>单 Agent</span><span>动态选用工具</span><span>逐轮验证</span></div>
      </article>
      <article class="card" data-anim>
        <div class="feature-index">03</div>
        <div class="card-title">结论附带证据</div>
        <p class="card-body">报告给出可能的根因、因果链、查询证据、<span class="nowrap">待确认项与处置建议</span>，供人工复核。</p>
        <div class="feature-tags"><span>查询证据</span><span>因果链</span><span>待确认项</span></div>
      </article>
    </div>

    <div class="opensre-boundary" data-anim>
      <strong>实现边界</strong>
      <span>使用通用大模型，不另行训练基础模型</span><span>当前主流程为单 Agent</span><span>能力取决于已接入工具</span><span>项目仍处于公开测试早期</span>
    </div>
  </div>
  ${source([["OpenSRE README", `${opensreBase}/README.md`]])}
</section>`);

add(`
<section class="slide" data-layout="H08">
  ${brand}${chrome("01 · OpenSRE", "02 / 04 · 外层六步流程")}
  <div class="opensre-process-slide">
    <header data-anim>
      <div class="kicker">FIXED OUTER PIPELINE</div>
      <h2 class="h-xl">OpenSRE 如何按六步处理一条告警</h2>
    </header>

    <div class="opensre-six-flow" data-anim>
      <article class="opensre-flow-step">
        <span class="step-no">01</span><strong>确认系统接入</strong>
        <p>确认当前已连接且可用的<span class="nowrap">外部系统</span></p><small>resolve_integrations</small>
      </article>
      <article class="opensre-flow-step">
        <span class="step-no">02</span><strong>解析告警</strong>
        <p>提取服务名、错误信息与<span class="nowrap">时间范围</span></p><small>extract_alert</small>
      </article>
      <article class="opensre-flow-step selected">
        <span class="step-no">03</span><strong>确定工具范围</strong>
        <p>按固定规则筛选本次可用工具</p><small>plan_actions</small>
      </article>
      <article class="opensre-flow-step active">
        <span class="step-no">04</span><strong>Agent 排查</strong>
        <p>在可用工具范围内逐轮查询并<span class="nowrap">验证根因假设</span></p><small title="ConnectedInvestigationAgent.run">agent.run</small>
      </article>
      <article class="opensre-flow-step">
        <span class="step-no">05</span><strong>整理 RCA 结论</strong>
        <p>按预定义结构整理最终输出</p><small>diagnose</small>
      </article>
      <article class="opensre-flow-step">
        <span class="step-no">06</span><strong>交付报告</strong>
        <p>生成、保存或发送故障分析报告</p><small>deliver</small>
      </article>
    </div>

    <div class="stage-relation" data-anim>
      <strong>第 3 步</strong>由程序按固定规则确定 Agent 可以使用哪些工具
      <i data-lucide="arrow-right"></i>
      <strong>第 4 步</strong>由 Agent 在这些工具中动态选择并调用，具体循环见下一页
    </div>

    <div class="opensre-stage-grid">
      <article class="opensre-stage-card selected" data-anim>
        <div class="stage-card-head"><span>03</span><div><strong>程序按规则确定工具范围</strong><small>固定规则筛选，不由模型规划</small></div></div>
        <ul>
          <li>候选来自已注册且当前可用的排查工具，再由程序按固定规则排序。</li>
          <li>代码字段 <code>planned_actions</code> 保存<strong>入选工具名短名单</strong>；规划阶段还记录选择理由、审计信息和建议的检索参数。</li>
          <li>本阶段不提出根因假设，也不生成实际工具调用参数。默认最多选择 <strong>10</strong> 个工具，可配置为 <strong>1—50</strong> 个；实际数量可能少于上限。</li>
          <li>Agent 只能调用短名单中仍可用的工具；如果短名单为空或其中工具均不可用，系统才会从最多 <strong>32</strong> 个候选工具中重新筛选。</li>
        </ul>
      </article>
      <article class="opensre-stage-card active" data-anim>
        <div class="stage-card-head"><span>04</span><div><strong>Agent 在可用工具范围内动态排查</strong><small>模型负责决策，OpenSRE 运行时负责执行</small></div></div>
        <ul>
          <li>第 4 步接收告警内容、可用工具范围和已有证据，再进入 Agent 排查循环。</li>
          <li>模型读取告警和已有证据，决定下一步调用哪个工具，并确定查询参数、时间范围和是否继续。</li>
          <li><strong>结论整理（diagnose）</strong>通常由推理模型按预定义结构整理 Agent 的最终输出；失败时改用<span class="nowrap">旧版标签解析规则</span>。该步骤不会继续排查，也不会独立复核结论。</li>
          <li><strong>报告交付（deliver）</strong>负责生成、保存或发送报告。</li>
        </ul>
      </article>
    </div>
    <div class="outer-pipeline-note" data-anim>
      <span><b>外层实现：</b>非噪声告警按六步固定顺序执行；噪声告警在第 2 步后结束。第 3—5 步依次确定<span class="nowrap">工具范围</span>、运行 Agent、整理 RCA 结论。</span>
      <span><b>当前缺口：</b>计划阶段建议的时间范围和返回条数（<code>retrieval_controls</code>）目前不会自动写入实际查询参数。</span>
    </div>
  </div>
  ${source([
    ["外层流程", `${opensreBase}/tools/investigation/lifecycle.py`],
    ["规划阶段", `${opensreBase}/tools/investigation/stages/plan_evidence/node.py`],
    ["工具筛选", `${opensreBase}/tools/investigation/stages/gather_evidence/tools.py`],
    ["结论整理", `${opensreBase}/tools/investigation/stages/diagnose/node.py`],
  ])}
</section>`);

add(`
<section class="slide" data-layout="H37">
  ${brand}${chrome("01 · OpenSRE", "03 / 04 · Agent 实现逻辑")}
  <div class="opensre-agent-slide">
    <header data-anim>
      <div class="kicker">CONNECTEDINVESTIGATIONAGENT.RUN</div>
      <h2 class="h-xl">OpenSRE 的故障定位 Agent 如何运行</h2>
    </header>

    <div class="agent-equation" data-anim>
      <strong>故障定位 Agent</strong><small>（ConnectedInvestigationAgent）</small><span>=</span>
      <b>排查提示词</b><i data-lucide="plus"></i><b>可用工具</b><i data-lucide="plus"></i><b>模型决策循环</b><i data-lucide="plus"></i><b>工具执行</b><i data-lucide="plus"></i><b>证据记录</b><i data-lucide="plus"></i><b>运行控制</b>
    </div>

    <div class="agent-method-strip" data-anim>
      <strong>提示词引导</strong>
      <span>初步判断</span><i data-lucide="arrow-right"></i><span>提出假设</span><i data-lucide="arrow-right"></i><span>查询并验证</span><i data-lucide="arrow-right"></i><span>给出处置建议</span>
      <small>由提示词引导，并非代码状态机；首轮模型调用产生新工具结果后，运行时追加阶段复盘提示，要求模型明确当前判断、根因假设和待确认问题。</small>
    </div>

    <div class="agent-workbench">
      <div class="agent-main-flow" data-anim>
        <div class="agent-start-grid">
          <article class="agent-step">
            <em>01</em><div><strong>准备本次运行</strong><p>读取本次任务的告警、已解析字段、连接信息和可用工具列表；初始化<span class="nowrap">消息历史</span>、证据集和工具调用缓存。</p></div>
          </article>
          <article class="agent-step">
            <em>02</em><div><strong>可选：执行预置首查</strong><p>如果告警来源配置了预置首查，运行时会在<span class="nowrap">首次调用模型前</span>执行；结果保存为初始证据并加入<span class="nowrap">模型上下文</span>。</p></div>
          </article>
        </div>
        <div class="agent-loop-label"><span>单 Agent 排查循环</span><small>每轮查询结果都会返回模型，供其继续推理</small></div>
        <div class="agent-loop-row">
          <article class="agent-loop-step model"><em>03</em><strong>模型决定下一步</strong><p>提出假设，选择工具，生成<span class="nowrap">查询参数</span>和时间范围。</p></article>
          <i data-lucide="chevron-right"></i>
          <article class="agent-loop-step"><em>04</em><strong>运行时校验并执行</strong><p>模型生成调用参数；运行时<span class="nowrap">校验参数</span>、<span class="nowrap">注入连接凭据</span><span class="nowrap">并执行工具</span>。同轮多个工具<span class="nowrap">默认并行</span>，包含串行工具时<span class="nowrap">整批顺序执行</span>；工具失败时，<span class="nowrap">错误信息</span>返回 Agent，<span class="nowrap">排查继续</span>。</p></article>
          <i data-lucide="chevron-right"></i>
          <article class="agent-loop-step"><em>05</em><strong>记录新证据</strong><p>新结果加入<span class="nowrap">模型上下文</span>，<span class="nowrap">并保存为</span>脱敏的<span class="nowrap">证据溯源记录</span>（<code>EvidenceEntry</code>）；<span class="nowrap">复用缓存结果时</span><span class="nowrap">不新增证据记录</span>。</p></article>
          <i data-lucide="chevron-right"></i>
          <article class="agent-loop-step end"><em>06</em><strong>继续或结束</strong><p>有新调用则进入下一轮；模型不再调用工具时，运行时检查结论格式，首次不完整时最多再提醒一次。模型调用上限为 20 次。</p></article>
        </div>
        <div class="agent-feedback">
          <span><b>继续：</b>新证据返回步骤 03，模型调整假设和查询</span>
          <i data-lucide="refresh-cw"></i>
          <span><b>结束：</b>模型最终回复 + 证据集 + 循环次数 → 外层流程</span>
        </div>
      </div>

      <aside class="agent-controls">
        <article class="agent-control-card" data-anim>
          <div class="control-head"><span>A</span><strong>模型上下文长度控制</strong></div>
          <p>每轮调用模型前，运行时估算<strong>系统提示词、工具定义和消息历史</strong>的总长度。本轮输入上限 = <span class="nowrap">模型上下文窗口</span> − 为输出预留 <strong>16,000 个 token</strong>。</p>
          <ol>
            <li>超限时先删除重复的工具调用及其结果；有多组<span class="nowrap">可删内容</span>时，优先删除占用上下文最多的一组。</li>
            <li>再删除占用上下文最多的普通工具调用及其结果；预置首查及其结果不会整组删除。</li>
            <li>如果仍超限，再截断可裁剪消息中最长的一条。</li>
          </ol>
          <small>这里只做删除或截断，不自动生成摘要。被裁掉的工具结果不再发送给模型，但已有脱敏证据记录仍<span class="nowrap">单独保留</span>；<span class="nowrap">普通消息</span>不保证另有备份。</small>
        </article>
        <article class="agent-control-card cache-card" data-anim>
          <div class="control-head"><span>B</span><strong>同一次排查中复用相同调用的结果</strong></div>
          <p>运行时用<strong>工具名和调用参数</strong>生成缓存键；连接凭据等受保护参数不参与生成。完全相同的调用复用已有结果，参数变化则重新执行。缓存满时按照 LRU 策略<span class="nowrap">淘汰旧记录</span>。</p>
          <div class="cache-metrics"><span><b>最多 128 条</b>缓存记录</span><span><b>约 200 万字符</b>缓存总容量</span><span><b>最多 8,000 字符</b>重复调用时<br>返回模型</span></div>
          <small>如果连续两轮都只有重复调用，下一轮暂不提供工具，<span class="nowrap">只要求模型给出结论</span>。</small>
        </article>
      </aside>
    </div>

    <div class="agent-boundary-strip" data-anim>
      <strong>实现边界</strong>
      <span>上下文裁剪与缓存由 OpenSRE 运行时负责，不是模型自身能力</span>
      <span>同轮工具并行 ≠ 多 Agent 协作</span>
      <span><span class="agent-boundary-copy">结论整理（<code>diagnose</code>）只整理最终结论，<span class="nowrap">不继续排查</span>或独立复核</span></span>
      <span>计划阶段建议的时间范围和返回条数不会自动写入实际查询参数</span>
    </div>
  </div>
  ${source([
    ["Agent 循环", `${opensreBase}/tools/investigation/stages/gather_evidence/agent.py`],
    ["提示词", `${opensreBase}/tools/investigation/stages/gather_evidence/prompt.py`],
    ["上下文控制", `${opensreBase}/core/context_budget.py`],
    ["证据记录", `${opensreBase}/core/state/evidence.py`],
  ])}
</section>`);

add(`
<section class="slide" data-layout="H24">
  ${brand}${chrome("01 · OpenSRE", "04 / 04 · RPC 超时示例")}
  <div class="opensre-case-slide">
    <header data-anim>
      <div class="kicker">END-TO-END WALKTHROUGH</div>
      <h2 class="h-xl">以 RPC 超时为例：Agent 如何逐步定位根因</h2>
    </header>

    <div class="teaching-notice" data-anim>
      <i data-lucide="info"></i>
      <span>演示场景：以下内容为便于讲解而构造，只展示一种可能的排查路径；不是 OpenSRE 官方案例，也不是内置的 RPC 超时固定定位流程。</span>
    </div>

    <div class="case-stage-rail" data-anim aria-label="与第 2 页对应的六个步骤">
      <span><b>01</b>确认系统接入</span><i data-lucide="chevron-right"></i>
      <span><b>02</b>解析告警</span><i data-lucide="chevron-right"></i>
      <span class="selected"><b>03</b>确定工具范围</span><i data-lucide="chevron-right"></i>
      <span class="active"><b>04</b>Agent 排查</span><i data-lucide="chevron-right"></i>
      <span><b>05</b>整理 RCA 结论</span><i data-lucide="chevron-right"></i>
      <span><b>06</b>交付报告</span>
    </div>

    <div class="case-main-grid">
      <div class="case-left-column">
        <article class="alert-card" data-anim>
          <div class="alert-card-head"><span>P1 告警</span><strong>2026-07-29 10:02</strong></div>
          <dl>
            <div><dt>环境</dt><dd>生产环境 prod / <code>commerce-prod</code></dd></div>
            <div><dt>调用</dt><dd>订单服务 <code>order-service</code> → 库存服务 <code>inventory-service</code></dd></div>
            <div><dt>接口</dt><dd>库存预留 <code>/Inventory/Reserve</code></dd></div>
            <div><dt>现象</dt><dd>P99 延迟 <b>3.2 秒</b>，告警阈值 800 毫秒</dd></div>
            <div><dt>错误率</dt><dd>12% · gRPC 超时 <code>DEADLINE_EXCEEDED</code></dd></div>
            <div><dt>来源</dt><dd>Datadog 监控</dd></div>
          </dl>
          <small>场景前提：已接入 Datadog、<span class="nowrap">Grafana Tempo 调用链</span>、<span class="nowrap">配置变更查询</span>和数据库监控工具；<span class="nowrap">未接入相应数据源时</span>，无法<span class="nowrap">完成对应验证</span>。</small>
        </article>

        <article class="tool-plan-card" data-anim>
          <div><span>可用工具范围</span><strong>本次可用的排查工具（仅示意工具类型）</strong></div>
          <p><span class="nowrap">Datadog 上下文查询</span> · <span class="nowrap">调用链查询</span> · <span class="nowrap">库存日志查询</span> · <span class="nowrap">发布与配置查询</span> · <span class="nowrap">连接池与数据库指标查询</span></p>
          <small>第 3 步确定工具范围；第 4 步由模型决定查询参数和排查顺序。</small>
        </article>
      </div>

      <div class="evidence-path" data-anim>
        <article>
          <span class="evidence-no">4A</span>
          <div><strong>模型调用前：运行时执行 Datadog 预置首查</strong><p>OpenSRE 根据告警来源自动执行该查询，在首次调用模型前获取相关监控、日志和变更事件，作为初始证据。</p></div>
        </article>
        <article>
          <span class="evidence-no">4B</span>
          <div><strong>模型首轮判断：选择调用链查询</strong><p>模型选择 Grafana Tempo 并生成参数；运行时执行后返回：总耗时 <b>3.21 秒</b>，其中库存服务获取数据库连接阶段（<code>db.acquire</code>）耗时 <b>3.02 秒</b>。</p></div>
        </article>
        <article>
          <span class="evidence-no">4C</span>
          <div><strong>模型提出假设，并查询日志验证</strong><p>模型提出“库存服务在等待数据库连接”的假设；运行时查询后发现，日志从 10:00 起出现“获取数据库连接等待 3,000 毫秒后超时”（<code>connection acquisition timeout after 3000ms</code>）。</p></div>
        </article>
        <article>
          <span class="evidence-no">4D</span>
          <div><strong>模型继续查询发布记录和配置变更</strong><p>运行时发现：09:58 发布的库存服务 v4.8.3（<code>inventory-service</code>）将连接池上限从 <b>80</b> 下调至 <b>8</b>。</p></div>
        </article>
        <article>
          <span class="evidence-no">4E</span>
          <div><strong>模型查询指标，继续交叉验证</strong><p>运行时返回：活动连接数 active=8、等待请求数 waiters=120；数据库 SQL 耗时、CPU、网络和其他下游<span class="nowrap">均无异常</span>。结果进一步支持<span class="nowrap">连接池耗尽</span>判断，并降低其他原因的可能性。</p></div>
        </article>
      </div>
    </div>

    <div class="case-conclusion" data-anim>
      <div>
        <span class="nowrap">Agent 结论</span>
        <strong>09:58 发布的版本将连接池上限从 80 下调至 8，导致连接池耗尽、请求排队，库存服务响应时间超过 3 秒，最终触发上游 RPC 超时。</strong>
      </div>
      <div class="cause-chain">
        <span>连接池上限 80 → 8</span><i data-lucide="arrow-right"></i><span>连接池耗尽</span><i data-lucide="arrow-right"></i><span>等待获取数据库连接（<code>db.acquire</code>）</span><i data-lucide="arrow-right"></i><span>库存服务响应超过 3 秒</span><i data-lucide="arrow-right"></i><span>上游 RPC 超时</span>
      </div>
      <p><b>处置建议（人工执行）：</b>经审批将连接池上限恢复到经验证的合理值，并增加配置校验和连接池等待请求数告警。</p>
    </div>

    <div class="case-outer-finish" data-anim>
      <strong>外层报告流程</strong><span>Agent 最终结论</span><i data-lucide="arrow-right"></i><span>结论整理（<code>diagnose</code>）</span><i data-lucide="arrow-right"></i><span>报告交付（<code>deliver</code>）</span>
    </div>

    <div class="case-boundary" data-anim>
      <strong>能力边界</strong>
      <div class="case-boundary-tags">
        <span>演示场景，并非官方案例</span><span>未内置针对 RPC 超时的固定定位流程</span><span>默认不会自动回滚</span><span>Agent 完成排查不等于结论已通过独立复核</span>
      </div>
    </div>
  </div>
  ${source([
    ["Agent 循环", `${opensreBase}/tools/investigation/stages/gather_evidence/agent.py`],
    ["排查提示词", `${opensreBase}/tools/investigation/stages/gather_evidence/prompt.py`],
    ["证据状态", `${opensreBase}/core/state/evidence.py`],
  ])}
</section>`);

if (false) {
add(`
<section class="slide dark" data-layout="H03">
  ${brand}${chrome("01 · OpenSRE", "Tracer-Cloud / opensre")}
  <div class="section-layout">
    <div data-anim>
      <div class="section-no">01</div>
      <div class="kicker">面向 SRE 的智能调查</div>
    </div>
    <div data-anim>
      <h2 class="h-hero">OpenSRE</h2>
      <p class="lead">最值得借鉴：结论可追溯到证据、调查受预算约束、结果可评测。</p>
      <div class="hairline section-line"></div>
      <div class="meta">Public Alpha（公开早期测试）· Apache-2.0 · 9 页</div>
    </div>
  </div>
</section>`);

add(`
<section class="slide" data-layout="H04">
  ${brand}${chrome("01 · OpenSRE", "01 / 09 · 身份判定")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>项目身份确认</div>
      <h2 class="h-xl" data-anim>本稿所称 OpenSRE，特指 Tracer-Cloud/opensre</h2>
      <p class="lead" data-anim>同名项目机制差异很大；评审材料必须明确仓库地址，避免混淆。</p>
    </div>
    <div class="grid-2 same-size">
      <article class="card focus-card" data-anim>
        <div class="project-head"><span class="project-monogram">OS</span><div><strong>Tracer-Cloud/opensre</strong><div class="meta">本次主对象</div></div></div>
        <ul class="clean-list">
          <li>AI-SRE 框架及训练、评测环境</li>
          <li>支持 60 余种工具与服务；Apache-2.0</li>
          <li>Public Alpha / v0.1；接口变化快</li>
          <li>默认调查主链并非 LangGraph 多 Agent 编排</li>
        </ul>
      </article>
      <article class="card muted-card" data-anim>
        <div class="project-head"><span class="project-monogram grey">OS</span><div><strong>swapnildahiphale/OpenSRE</strong><div class="meta">独立同名项目</div></div></div>
        <ul class="clean-list">
          <li>LangGraph 并行子 Agent</li>
          <li>Neo4j 拓扑图与情景记忆（episodic memory）</li>
          <li>社区与代码规模明显更小</li>
          <li>两者不能混为一谈</li>
        </ul>
      </article>
    </div>
    <div class="tag-row" data-anim>
      ${evidenceTag("F", "约 9.3k Stars / 1.3k Forks")}
      ${evidenceTag("F", "最新版本：0.1.2026.7.27")}
      ${evidenceTag("I", "热度只说明关注，不说明准确率")}
    </div>
  </div>
  ${source([
    ["Tracer-Cloud/opensre", "https://github.com/Tracer-Cloud/opensre"],
    ["独立同名项目", "https://github.com/swapnildahiphale/OpenSRE"],
  ])}
</section>`);

add(`
<section class="slide" data-layout="H08">
  ${brand}${chrome("01 · OpenSRE", "02 / 09 · 产品定位")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>从信号输入到可评分的根因分析</div>
      <h2 class="h-xl" data-anim>它覆盖“调查—报告—反馈”，不只是问答</h2>
    </div>
    <div class="process process-5">
      <div class="process-step" data-anim><div class="num-pill">01</div><div class="card-title">多源输入</div><div class="card-body">告警、日志、指标、Trace、部署、配置与知识</div></div>
      <div class="process-step" data-anim><div class="num-pill">02</div><div class="card-title">问题收敛</div><div class="card-body">实体、时间窗、噪声过滤、数据源解析</div></div>
      <div class="process-step" data-anim><div class="num-pill">03</div><div class="card-title">调用工具开展调查</div><div class="card-body">提出假设—获取证据—更新结论；循环有预算</div></div>
      <div class="process-step" data-anim><div class="num-pill">04</div><div class="card-title">结构化 RCA</div><div class="card-body">根因、因果链、已有证据支持或尚待验证的诊断判断与建议</div></div>
      <div class="process-step" data-anim><div class="num-pill">05</div><div class="card-title">沉淀回归用例</div><div class="card-body">人工标记诊断失败，转成可重复评测场景</div></div>
    </div>
    <div class="quote-band compact-quote" data-anim>
      可迁移价值：不再只看回答是否像样，而是评估证据是否充分、调查过程是否合理、结果是否正确。
    </div>
  </div>
  ${source([
    ["OpenSRE README", "https://github.com/Tracer-Cloud/opensre"],
    ["Closed-Loop Learning", "https://www.opensre.com/docs/closed-loop-learning"],
  ])}
</section>`);

add(`
<section class="slide" data-layout="H23">
  ${brand}${chrome("01 · OpenSRE", "03 / 09 · 模块架构")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>官方四层代码组织 · 本报告归纳</div>
      <h2 class="h-xl" data-anim>官方四层包架构；本报告归纳为模块化单体</h2>
    </div>
    <div class="layer-stack">
      <div class="layer-row" data-anim><span class="layer-no">01</span><strong>交互入口与网关</strong><span>CLI · REPL · Slack · Telegram · HTTP</span><i data-lucide="panels-top-left"></i></div>
      <div class="layer-row" data-anim><span class="layer-no">02</span><strong>工具与集成</strong><span>Agent 工具 · 外部系统客户端 · MCP</span><i data-lucide="plug-zap"></i></div>
      <div class="layer-row emphasized" data-anim><span class="layer-no">03</span><strong>核心运行与平台能力</strong><span>状态 · 大模型 · 预算 · 脱敏 · 执行约束 · 沙箱</span><i data-lucide="cpu"></i></div>
      <div class="layer-row" data-anim><span class="layer-no">04</span><strong>配置</strong><span>提示词 · 常量 · 主题 · 策略配置</span><i data-lucide="settings-2"></i></div>
    </div>
    <div class="grid-2">
      <div class="mini-callout" data-anim><strong>值得借鉴</strong><span>模块依赖边界可自动校验，渠道、工具和运行时可独立演进。</span></div>
      <div class="mini-callout warning" data-anim><strong>不照搬</strong><span>目录结构不能替代 Case/Job 的业务状态与恢复语义。</span></div>
    </div>
  </div>
  ${source([["Architecture", "https://github.com/Tracer-Cloud/opensre/blob/main/docs/ARCHITECTURE.md"]])}
</section>`);

add(`
<section class="slide" data-layout="H24">
  ${brand}${chrome("01 · OpenSRE", "04 / 09 · 调查流水线")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>先定流程，再开放自主推理</div>
      <h2 class="h-xl" data-anim>先收窄问题和可用工具范围，再开放调查</h2>
    </div>
    <div class="pipeline-input" data-anim><span>输入</span><strong>原始告警</strong><small>raw_alert · 问题描述</small></div>
    <div class="flow-6 compact-flow">
      <div class="flow-node" data-anim><span>01</span><strong>解析集成</strong><small>resolve_integrations</small></div>
      <div class="flow-node" data-anim><span>02</span><strong>提取问题</strong><small>extract_alert · 实体 / 时间窗</small></div>
      <div class="flow-node" data-anim><span>03</span><strong>规划工具</strong><small>plan_actions · 候选前 10</small></div>
      <div class="flow-node hot" data-anim><span>04</span><strong>开展调查</strong><small>investigate · ReAct</small></div>
      <div class="flow-node" data-anim><span>05</span><strong>生成结论</strong><small>diagnose · RCA DTO</small></div>
      <div class="flow-node" data-anim><span>06</span><strong>交付结果</strong><small>deliver · Slack / GitLab</small></div>
    </div>
    <div class="grid-3 same-size">
      ${card("无效告警提前结束", "不让无效输入进入高成本调查。", "filter-x")}
      ${card("共享调查状态", "阶段间通过 AgentState 传递结构化结果。", "database")}
      ${card("结果结构化", "将自由文本结论解析为结构化 RCA 数据对象（DTO）。", "braces")}
    </div>
  </div>
  ${source([["Investigation Pipeline", "https://github.com/Tracer-Cloud/opensre/blob/main/docs/investigation-pipeline-architecture.md"]])}
</section>`);

add(`
<section class="slide" data-layout="H15">
  ${brand}${chrome("01 · OpenSRE", "05 / 09 · 调查约束")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>受约束的 ReAct 调查</div>
      <h2 class="h-xl" data-anim>开放式推理受预算、上限与停滞阈值约束</h2>
    </div>
    <div class="grid-4 metric-grid">
      <div class="metric-card" data-anim><div class="stat-big">10</div><strong>候选工具</strong><span>计划阶段默认 Top 10</span></div>
      <div class="metric-card" data-anim><div class="stat-big">32</div><strong>工具定义</strong><span>单轮最多提供给模型</span></div>
      <div class="metric-card" data-anim><div class="stat-big">20</div><strong>调查循环</strong><span>全程最大轮数</span></div>
      <div class="metric-card" data-anim><div class="stat-big">2</div><strong>停滞阈值</strong><span>连续 2 轮仅产生重复调用</span></div>
    </div>
    <div class="grid-3 same-size">
      ${card("重复调用去重", "相同工具和参数直接复用缓存，避免重复成本。", "copy-check")}
      ${card("首轮预取证", "预置工具可在首次调用大模型前获取高置信证据。", "sprout")}
      ${card("模型失败时降级输出", "保留已经取得的证据，并输出部分结果。", "shield-alert")}
    </div>
  </div>
  ${source([["Investigation Pipeline", "https://github.com/Tracer-Cloud/opensre/blob/main/docs/investigation-pipeline-architecture.md"]])}
</section>`);

add(`
<section class="slide" data-layout="H42">
  ${brand}${chrome("01 · OpenSRE", "06 / 09 · 状态与上下文")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>区分调查状态与模型输入</div>
      <h2 class="h-xl" data-anim>RCA 调查状态与 REPL 会话恢复是两套机制</h2>
    </div>
    <div class="dual-chain">
      <div class="chain-row" data-anim>
        <div class="chain-label">RCA 调查链</div>
        <div class="chain-box"><small>调查状态</small><strong>AgentState + EvidenceEntry</strong></div>
        <i data-lucide="chevron-right"></i>
        <div class="chain-box compiler"><small>模型输入规则</small><strong>context_budget</strong></div>
        <i data-lucide="chevron-right"></i>
        <div class="chain-box"><small>本轮请求</small><strong>模型调用与工具循环</strong></div>
      </div>
      <div class="chain-row secondary" data-anim>
        <div class="chain-label">REPL 交互链</div>
        <div class="chain-box"><small>会话持久化</small><strong>会话文件</strong></div>
        <i data-lucide="chevron-right"></i>
        <div class="chain-box compiler"><small>历史压缩</small><strong>/compact</strong></div>
        <i data-lucide="chevron-right"></i>
        <div class="chain-box"><small>会话恢复</small><strong>/resume 交互上下文</strong></div>
      </div>
    </div>
    <div class="grid-2">
      <div class="mini-callout" data-anim><strong>可确认</strong><span>REPL 可恢复对话、工具轨迹与基础设施上下文。</span></div>
      <div class="mini-callout warning" data-anim><strong>公开资料未说明</strong><span>六阶段调查状态能否保存持久检查点，并在进程重启后继续。</span></div>
    </div>
    <div class="verdict-line" data-anim><strong>边界：</strong>REPL 会话恢复不能替代 Case/Job 恢复；OpenSRE 的调查状态和 REPL 会话都不能直接作为本框架的权威业务状态。</div>
  </div>
  ${source([
    ["State Contract", "https://github.com/Tracer-Cloud/opensre/blob/main/core/state/README.md"],
    ["Context Budget", "https://github.com/Tracer-Cloud/opensre/blob/main/core/context_budget.py"],
    ["Sessions", "https://www.opensre.com/docs/sessions"],
  ])}
</section>`);

add(`
<section class="slide" data-layout="H05">
  ${brand}${chrome("01 · OpenSRE", "07 / 09 · 生态与安全")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>接入数量不等于生产安全</div>
      <h2 class="h-xl" data-anim>接入广度突出，默认值仍需生产加固</h2>
    </div>
    <div class="grid-3 same-size">
      ${card("60 余种工具和服务", "<strong>覆盖大模型、可观测、云、数据和事件管理等类别</strong><br>不同集成的能力深度并不一致；接入越广，凭证与数据暴露面越大。", "cable")}
      ${card("多形态部署", "<strong>本地、Docker/ECR、AMI + systemd、ASGI 托管</strong><br>持久化可配 PostgreSQL / Redis。", "cloud-cog")}
      ${card("安全默认值", "<strong>脱敏默认关闭；PostHog/Sentry 遥测默认开启、可手动关闭；历史记录以可读文件落盘</strong><br>示例 Ingress 配置不能原样上线。", "shield-off")}
    </div>
    <div class="risk-strip" data-anim>
      <strong>自动修复前置条件</strong>
      <span>独立审批</span><span>最小权限</span><span>可回滚</span><span>全流程审计</span>
    </div>
  </div>
  ${source([
    ["Deployment", "https://github.com/Tracer-Cloud/opensre/blob/main/DEPLOYMENT.md"],
    ["Masking", "https://www.opensre.com/docs/masking"],
    ["Shell Privacy", "https://www.opensre.com/docs/interactive-shell-privacy"],
  ])}
</section>`);

add(`
<section class="slide" data-layout="H07">
  ${brand}${chrome("01 · OpenSRE", "08 / 09 · 评测闭环")}
  <div class="content-stack slide-body">
    <div class="grid-2">
      <div data-anim>
        <div class="kicker">将生产反馈沉淀为回归用例</div>
        <h2 class="h-xl">长期价值在闭环，<br>不在宣传数字</h2>
        <p class="lead">将生产误判沉淀为回归评测场景，并记录预期根因、错误类别和诊断失败备注；必需证据和调查过程约束仍需场景作者补充。</p>
      </div>
      <div class="loop-ring" data-anim>
        <div class="loop-center"><strong>RCA</strong><span>持续改进</span></div>
        <div class="loop-item l1">生产调查</div>
        <div class="loop-item l2">人工评分</div>
        <div class="loop-item l3">失误分类</div>
        <div class="loop-item l4">回归用例</div>
        <div class="loop-item l5">基准测试</div>
      </div>
    </div>
    <div class="grid-3 same-size">
      ${card("可评测内容", "根因类别、关键词、必需证据、禁止结论、调查过程和效率。", "list-checks")}
      ${card("覆盖哪些干扰场景", "无故障对照、误导性线索、关键指标缺失和复合故障。", "bug-off")}
      ${card("证据边界", "<strong>README 明确暂无完整基准评测结果</strong>；目标值不等于实际成绩。", "triangle-alert")}
    </div>
  </div>
  ${source([
    ["OpenSRE README", "https://github.com/Tracer-Cloud/opensre"],
    ["Closed-Loop Learning", "https://www.opensre.com/docs/closed-loop-learning"],
    ["CloudOpsBench", "https://www.opensre.com/docs/cloudopsbench"],
    ["Synthetic RCA", "https://github.com/Tracer-Cloud/opensre/tree/main/tests/synthetic/rds_postgres"],
  ])}
</section>`);

add(`
<section class="slide" data-layout="H26">
  ${brand}${chrome("01 · OpenSRE", "09 / 09 · 结论")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>借鉴诊断机制，不照搬整体编排</div>
      <h2 class="h-xl" data-anim>借诊断机制，不用它替代业务控制面</h2>
    </div>
    <div class="grid-2 compare-panels">
      <article class="compare-panel yes" data-anim>
        <div class="compare-title"><i data-lucide="check-circle-2"></i> 建议吸收</div>
        ${bullets([
          "记录证据来源与采集信息，区分已有证据支持和尚待验证的诊断判断",
          "工具规划、预算、去重、停滞终止和降级",
          "调查状态、原始证据和本轮模型输入分离",
          "评测误导性线索、缺失证据和调查效率",
          "将生产误判和漏判回流为回归用例",
        ])}
      </article>
      <article class="compare-panel no" data-anim>
        <div class="compare-title"><i data-lucide="x-circle"></i> 不直接照搬</div>
        ${bullets([
          "用单 Agent 共享状态替代 Case/Job/Coordinator",
          "将会话文件或摘要作为权威业务状态",
          "把并行生成假设误认为已具备多 Agent 结果合并和冲突消解",
          "未经审批执行修复操作",
          "把社区热度与集成数当成准确率",
        ])}
      </article>
    </div>
    <div class="verdict-line" data-anim><strong>一句话：</strong>OpenSRE 展示了如何把诊断 Agent 工程化为证据驱动、受约束且可评测的系统。</div>
  </div>
</section>`);

}

add(`
<section class="slide dark" data-layout="H03">
  ${brand}${chrome("02 · OpenDerisk", "derisk-ai · 蚂蚁集团作者及生产实践背景")}
  <div class="section-layout">
    <div data-anim>
      <div class="section-no">02</div>
      <div class="kicker">面向 SRE 诊断的多 Agent 协作</div>
    </div>
    <div data-anim>
      <h2 class="h-hero">OpenDerisk</h2>
      <p class="lead">最值得借鉴：专家协同、知识与上下文工程、证据链，以及人工介入与审核。</p>
      <div class="hairline section-line"></div>
      <div class="meta">MIT · 蚂蚁集团实践背景 · 9 页</div>
    </div>
  </div>
</section>`);

add(`
<section class="slide" data-layout="H27">
  ${brand}${chrome("02 · OpenDerisk", "01 / 09 · 候选判定")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>阿里生态候选项目</div>
      <h2 class="h-xl" data-anim>本次候选中，OpenDerisk 与 OpenSRE 定位最接近；但它不是集团官方仓</h2>
    </div>
    <div class="candidate-grid">
      <div class="candidate-card selected" data-anim><strong>OpenDerisk</strong><span>开源仓提供 AI-SRE 多 Agent 核心能力；论文另有内部平台说明</span><b>选定</b></div>
      <div class="candidate-card" data-anim><strong>SREWorks</strong><span>AIOps / DevOps 平台</span><b>平台层</b></div>
      <div class="candidate-card" data-anim><strong>SysOM AI</strong><span>Linux 诊断 MCP / Skills</span><b>工具层</b></div>
      <div class="candidate-card" data-anim><strong>UModel</strong><span>对象图 / 拓扑 / 语义</span><b>上下文层</b></div>
      <div class="candidate-card" data-anim><strong>muAgent</strong><span>通用多 Agent 基座</span><b>框架层</b></div>
      <div class="candidate-card" data-anim><strong>Spring AI Alibaba</strong><span>Java Agent、工作流与人工介入</span><b>通用基座</b></div>
      <div class="candidate-card" data-anim><strong>AgentScope</strong><span>通用 Agent 团队 / 权限 / 沙箱</span><b>通用基座</b></div>
      <div class="candidate-card" data-anim><strong>Observability MCP</strong><span>阿里云可观测数据 / 工具接入</span><b>工具层</b></div>
      <div class="candidate-card" data-anim><strong>RCAgent</strong><span>本次未在论文页及 alibaba、aliyun 组织找到对应实现</span><b>论文提及</b></div>
    </div>
    <div class="attribution-note" data-anim>
      <i data-lucide="badge-info"></i>
      <span>论文作者及其中的工业实践来自蚂蚁集团；公开代码仓归属 derisk-ai 社区。本次按阿里生态相关项目纳入，不代表集团官方发布。</span>
    </div>
  </div>
  ${source([
    ["OpenDerisk", "https://github.com/derisk-ai/OpenDerisk"],
    ["SREWorks", "https://github.com/alibaba/SREWorks"],
    ["SysOM AI", "https://github.com/aliyun/sysom-ai"],
    ["UModel", "https://github.com/alibaba/UnifiedModel"],
    ["完整候选与来源索引见报告", "../problem-locator-open-source-insight.md"],
  ])}
</section>`);

add(`
<section class="slide" data-layout="H08">
  ${brand}${chrome("02 · OpenDerisk", "02 / 09 · 产品定位")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>README 与论文 · 面向 SRE 调查的智能助手</div>
      <h2 class="h-xl" data-anim>从多源异常走向根因、证据和处置建议</h2>
    </div>
    <div class="process">
      <div class="process-step" data-anim><div class="num-pill">01</div><div class="card-title">信号</div><div class="card-body">日志告警 · 应用异常 · 环境变更 · 代码变化</div></div>
      <div class="process-step" data-anim><div class="num-pill">02</div><div class="card-title">调查</div><div class="card-body">假设生成 · 工具取证 · 因果推理 · 专家协作</div></div>
      <div class="process-step" data-anim><div class="num-pill">03</div><div class="card-title">输出</div><div class="card-body">根因位置 · 证据链 · 报告 · 处置建议</div></div>
      <div class="process-step" data-anim><div class="num-pill">04</div><div class="card-title">人工审核</div><div class="card-body">纠正 · 补充 · 终止 · 最终修复确认</div></div>
    </div>
    <div class="grid-3 same-size">
      ${card("公开示例场景", "OpenRCA、火焰图分析、DataExpert。", "workflow")}
      ${card("开源边界", "README 说明当前代码只覆盖架构图的高亮部分。", "scan-eye")}
      ${card("能力边界", "当前定位为辅助诊断工具（Copilot）；尚无公开证据表明其可无人值守自动修复。", "user-check")}
    </div>
  </div>
  ${source([
    ["OpenDerisk README", "https://github.com/derisk-ai/OpenDerisk"],
    ["OpenDerisk Paper", "https://arxiv.org/html/2510.13561v2"],
  ])}
</section>`);

add(`
<section class="slide" data-layout="H23">
  ${brand}${chrome("02 · OpenDerisk", "03 / 09 · 总体架构")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>论文架构：感知层 → DeRisk 核心 → 分析与报告</div>
      <h2 class="h-xl" data-anim>核心是多 Agent 平台中枢，而不是单个 Agent</h2>
    </div>
    <div class="architecture-3">
      <div class="arch-band" data-anim>
        <div><span>01</span><strong>感知层（Perception）</strong></div>
        <p>告警 · 异常行为 · 环境变化 · GitHub 代码变化</p>
      </div>
      <div class="arch-band core" data-anim>
        <div><span>02</span><strong>DeRisk 核心：决策与执行</strong></div>
        <div class="arch-pills"><b>多 Agent</b><b>推理引擎</b><b>知识引擎</b><b>工具 / MCP</b></div>
      </div>
      <div class="arch-band" data-anim>
        <div><span>03</span><strong>分析与报告</strong></div>
        <p>根因定位 · 证据链 · 诊断报告 · 处置意见 · 人工审核</p>
      </div>
    </div>
    <div class="grid-2">
      <div class="mini-callout" data-anim><strong>中央编排器（Orchestrator）</strong><span>管理 Agent 生命周期、消息和任务。</span></div>
      <div class="mini-callout" data-anim><strong>异步消息总线</strong><span>支撑专家之间的任务协作。</span></div>
    </div>
  </div>
  ${source([["OpenDerisk Paper", "https://arxiv.org/html/2510.13561v2"]])}
</section>`);

add(`
<section class="slide" data-layout="H17">
  ${brand}${chrome("02 · OpenDerisk", "04 / 09 · Agent 协作")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>按问题复杂度升级协作方式</div>
      <h2 class="h-xl" data-anim>从单 Agent 到专家团队，再到评审 Agent 汇总</h2>
    </div>
    <div class="hub-diagram">
      <div class="hub-center" data-anim><i data-lucide="route"></i><strong>主管 Agent</strong><span>拆解 · 路由 · 汇总</span></div>
      <div class="hub-node h1" data-anim><strong>SRE</strong><span>调查</span></div>
      <div class="hub-node h2" data-anim><strong>代码</strong><span>分析代码</span></div>
      <div class="hub-node h3" data-anim><strong>数据</strong><span>查询统计</span></div>
      <div class="hub-node h4" data-anim><strong>图表</strong><span>可视化</span></div>
      <div class="hub-node h5" data-anim><strong>报告</strong><span>形成报告</span></div>
    </div>
    <div class="mode-strip">
      <div data-anim><b>单 Agent（Single-Agent）</b><span>基础范式；边界未详述</span></div>
      <div data-anim><b>团队模式（TeamMode）</b><span>各专家上下文相互隔离，分别独立分析</span></div>
      <div data-anim><b>群组模式（GroupMode）</b><span>评审 Agent 汇总各专家的完整材料</span></div>
    </div>
  </div>
  ${source([["OpenDerisk Paper", "https://arxiv.org/html/2510.13561v2"]])}
</section>`);

add(`
<section class="slide" data-layout="H05">
  ${brand}${chrome("02 · OpenDerisk", "05 / 09 · 推理路径")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>三种推理模式</div>
      <h2 class="h-xl" data-anim>确定性流程与开放推理分开治理</h2>
    </div>
    <div class="grid-3 same-size">
      ${card("自主推理（Dynamic ReAct）", "<strong>自主选择下一步</strong><br>适应性强；时延、成本与可重复性较弱。", "sparkles")}
      ${card("固定流程（SOP）", "<strong>预定义阶段和条件</strong><br>可审计、可复现；对未知问题适应性较弱。", "list-tree")}
      ${card("强化学习策略（路线图）", "<strong>论文列出概念，但认为当前不适合生产部署</strong><br>完整系统级强化学习属于 V4 规划，不算当前已验证能力。", "brain-circuit")}
    </div>
    <div class="decision-ladder" data-anim>
      <span>低复杂度</span>
      <div><b>固定流程</b><i></i><b>单 Agent</b><i></i><b>多专家协作</b></div>
      <span>高复杂度</span>
    </div>
    <div class="quote-band compact-quote" data-anim>
      对当前框架的启示：控制面保持确定性，仅允许 Agent 在单个 Job 边界内自主决策。
    </div>
  </div>
  ${source([["OpenDerisk Paper", "https://arxiv.org/html/2510.13561v2"]])}
</section>`);

add(`
<section class="slide" data-layout="H23">
  ${brand}${chrome("02 · OpenDerisk", "06 / 09 · 上下文工程")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>论文机制与本报告设计推断</div>
      <h2 class="h-xl" data-anim>子 Agent 只上报结构化结论，不传完整对话历史</h2>
    </div>
    <div class="context-stack">
      <div class="context-layer l-top" data-anim><b><em class="layer-evidence i">I</em> 任务契约</b><span>目标 · 约束 · 输出结构 · 可用工具</span></div>
      <div class="context-layer" data-anim><b><em class="layer-evidence s">S</em> 当前上下文窗口</b><span>原始问题与本轮推理保留全文</span></div>
      <div class="context-layer" data-anim><b><em class="layer-evidence s">S</em> 结构化摘要</b><span>关键发现 · 置信度 · 证据引用</span></div>
      <div class="context-layer l-bottom" data-anim><b><em class="layer-evidence i">I</em> 独立证据库</b><span>原始日志、Trace、代码和查询结果独立保存，不随压缩丢失</span></div>
    </div>
    <div class="grid-3 same-size">
      ${card("摘要式压缩", "旧轮次压缩为长期摘要。", "file-down")}
      ${card("按策略裁剪", "大段工具输出按规则截断或摘要。", "sliders-horizontal")}
      ${card("独立分析后汇总", "团队模式减少相互影响，群组模式负责汇总。", "split")}
    </div>
  </div>
  ${source([["OpenDerisk Paper", "https://arxiv.org/html/2510.13561v2"]])}
</section>`);

add(`
<section class="slide" data-layout="H08">
  ${brand}${chrome("02 · OpenDerisk", "07 / 09 · 知识与证据")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>知识引擎与诊断判断—证据模型</div>
      <h2 class="h-xl" data-anim>知识帮助形成假设，证据支撑诊断结论</h2>
    </div>
    <div class="process process-5 slim-process">
      <div class="process-step" data-anim><div class="num-pill">01</div><div class="card-title">解析清洗</div></div>
      <div class="process-step" data-anim><div class="num-pill">02</div><div class="card-title">分块</div></div>
      <div class="process-step" data-anim><div class="num-pill">03</div><div class="card-title">语义增强</div></div>
      <div class="process-step" data-anim><div class="num-pill">04</div><div class="card-title">混合索引</div></div>
      <div class="process-step" data-anim><div class="num-pill">05</div><div class="card-title">主动更新</div></div>
    </div>
    <div class="model-label" data-anim>本报告建议的诊断判断—证据状态模型</div>
    <div class="evidence-chain" data-anim>
      <div><small>诊断判断</small><strong>候选根因</strong></div>
      <span>支持 / 反驳</span>
      <div><small>证据</small><strong>内容及来源</strong></div>
      <span>指向</span>
      <div><small>原始材料</small><strong>日志 / Trace / 代码</strong></div>
      <span>提交审核</span>
      <div><small>人工审核</small><strong>审核结论</strong></div>
    </div>
    <div class="risk-strip" data-anim><strong>关键风险</strong><span>知识更新及时性</span><span>实体关系正确性</span><span>数据完整性</span></div>
  </div>
  ${source([["OpenDerisk Paper", "https://arxiv.org/html/2510.13561v2"]])}
</section>`);

add(`
<section class="slide" data-layout="H07">
  ${brand}${chrome("02 · OpenDerisk", "08 / 09 · 效果与工程证据")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>质量提升伴随更高时延与成本</div>
      <h2 class="h-xl" data-anim>多专家方案得分更高，但耗时显著增加</h2>
    </div>
    <div class="grid-2">
      <article class="card" data-anim>
        <div class="card-title">作者报告的诊断评分</div>
        <div class="bar-chart">
          <div class="bar-row"><span>V1 ReAct</span><div class="bar-track"><div class="bar-fill" style="--v:39%"></div></div><strong>39</strong></div>
          <div class="bar-row"><span>V2 分阶段</span><div class="bar-track"><div class="bar-fill" style="--v:58%"></div></div><strong>58</strong></div>
          <div class="bar-row"><span>V3 多 Agent</span><div class="bar-track"><div class="bar-fill" style="--v:76%"></div></div><strong>76</strong></div>
        </div>
        <div class="meta card-note">Bailing DeepSeek-V3；人评 100 分制</div>
      </article>
      <article class="card" data-anim>
        <div class="card-title">作者报告的运行时间</div>
        <div class="bar-chart">
          <div class="bar-row"><span>V1</span><div class="bar-track"><div class="bar-fill" style="--v:27%"></div></div><strong>6 分钟</strong></div>
          <div class="bar-row"><span>V3</span><div class="bar-track"><div class="bar-fill" style="--v:100%"></div></div><strong>22 分钟</strong></div>
        </div>
        <div class="meta card-note">Qwen-QWQ-32B 示例</div>
      </article>
    </div>
    <div class="grid-4 mini-metrics">
      <div data-anim><b>13</b><span>3 个月内新增场景</span></div><div data-anim><b>50+</b><span>专用 Agent</span></div><div data-anim><b>3,000+</b><span>日活用户</span></div><div data-anim><b>60,000+</b><span>日运行次数</span></div>
    </div>
    <div class="evidence-warning" data-anim>${evidenceTag("S", "论文作者报告")}<span>同一底座模型下可以横向比较，但版本间同时改动了阶段控制、知识库、工作流、工具调用、任务交接和协作方式，不能把全部提升归因于 Agent 数量。</span></div>
  </div>
  ${source([["OpenDerisk Paper", "https://arxiv.org/html/2510.13561v2"]])}
</section>`);

add(`
<section class="slide" data-layout="H26">
  ${brand}${chrome("02 · OpenDerisk", "09 / 09 · 结论")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>设计思路较完整，开源实现仍在快速演进</div>
      <h2 class="h-xl" data-anim>设计思路较完整，但开源实现仍需生产验证</h2>
    </div>
    <div class="grid-2 compare-panels">
      <article class="compare-panel yes" data-anim>
        <div class="compare-title"><i data-lucide="check-circle-2"></i> 建议吸收</div>
        ${bullets([
          "按问题复杂度选择单 Agent、专家团队或评审汇总",
          "固定流程与自主推理相结合",
          "隔离上下文独立分析，再由评审 Agent 汇总",
          "证据链可视化和人工审核",
          "准确率与时延、成本共同评测",
        ])}
      </article>
      <article class="compare-panel no" data-anim>
        <div class="compare-title"><i data-lucide="x-circle"></i> 仍需验证</div>
        ${bullets([
          "开源版是否覆盖内部平台全部能力",
          "私有数据与人评结果能否外部复现",
          "文档与软件包版本是否一致，以及直接从 main 分支安装的稳定性",
          "先验证 OAuth2、用户和工具授权、密钥存储，再补齐 RBAC、多租户、审计和沙箱隔离",
          "脱离蚂蚁集团成熟可观测体系后的适用性",
        ])}
      </article>
    </div>
    <div class="verdict-line" data-anim><strong>一句话：</strong>多 Agent 的价值在“隔离、分工、合并与审核”，不在数量。</div>
  </div>
  ${source([
    ["OpenDerisk Repo", "https://github.com/derisk-ai/OpenDerisk"],
    ["OpenDerisk Paper", "https://arxiv.org/html/2510.13561v2"],
  ])}
</section>`);

add(`
<section class="slide dark" data-layout="H03">
  ${brand}${chrome("03 · 参考对象", "仓内 8 个项目")}
  <div class="section-layout">
    <div data-anim>
      <div class="section-no">03</div>
      <div class="kicker">状态 · 事件记录 · 任务边界 · 模型输入</div>
    </div>
    <div data-anim>
      <h2 class="h-hero">8 个 Agent 参考项目</h2>
      <p class="lead">每个项目 3 页：定位与状态载体、关键机制、可借鉴点与适用边界。</p>
      <div class="hairline section-line"></div>
      <div class="meta">LangGraph · OpenHands · Cline · AutoGen · CrewAI · Aider · SWE-agent · mini-SWE-agent</div>
    </div>
  </div>
</section>`);

const references = [
  {
    no: "01",
    name: "LangGraph",
    subtitle: "状态优先的可恢复编排",
    category: "状态优先",
    positioning: "用于构建 Agent 与工作流状态图；各节点围绕共享状态执行。",
    state: "图状态（Graph State）· 线程 · 状态快照",
    context: "应用可裁剪或摘要消息字段；具体规则由应用定义。",
    recovery: "按线程 ID 和状态检查点 ID 恢复、重放或分叉。",
    steps: [
      ["定义共享状态", "领域字段决定哪些结果可以校验"],
      ["节点增量更新", "每一步只提交状态变化"],
      ["保存运行快照", "由 Checkpointer 持久化每个 Super-step 后的状态"],
      ["恢复或分叉", "从明确版本继续，或创建试验分支"],
    ],
    borrow: [
      "用 DiagnosisState 表达结构化诊断状态",
      "把 Job 设为可检查的步骤边界",
      "用 JobContextManifest 固定单次执行输入",
      "用 DiagnosisStateDelta 表达状态增量",
    ],
    limits: [
      "通用状态不自带诊断领域语义",
      "完整消息写入状态，会让状态退化为会话记录库",
      "检查点只能恢复运行状态，不能替代证据审核",
    ],
    verdict: "可借鉴状态检查点与重放机制，但消息历史不能成为权威业务状态。",
    src: [
      ["Persistence", "https://docs.langchain.com/oss/python/langgraph/persistence"],
      ["Memory", "https://docs.langchain.com/oss/python/langgraph/add-memory"],
    ],
  },
  {
    no: "02",
    name: "OpenHands",
    subtitle: "只追加事件与可审计的上下文压缩",
    category: "事件记录优先",
    positioning: "交互式编码 Agent；重点是会话持久化和长历史治理。",
    state: "基础状态文件 · 只追加事件日志 · 工作区",
    context: "历史压缩器（Condenser）保留首尾并压缩中间，压缩结果也记录为事件。",
    recovery: "使用基础状态和原始事件重建会话。",
    steps: [
      ["保存基础状态", "记录会话元数据与配置"],
      ["只追加事件", "消息与工具调用逐条留存"],
      ["生成压缩事件", "压缩结果本身可以追踪"],
      ["构造模型输入", "模型只读取压缩后的内容"],
    ],
    borrow: [
      "原始事件记录与本轮模型输入分离",
      "压缩动作本身可审计",
      "原始证据不因上下文压缩而丢失",
      "事件记录可用于还原执行过程",
    ],
    limits: [
      "会话仍是主要工作上下文",
      "自由文本压缩摘要不能作为权威状态",
      "不能恢复完整的诊断判断—证据审核状态机",
    ],
    verdict: "原始事件完整保留；压缩只影响本轮模型输入。",
    src: [
      ["Persistence", "https://docs.openhands.dev/sdk/guides/convo-persistence"],
      ["Condenser", "https://docs.openhands.dev/sdk/arch/condenser"],
    ],
  },
  {
    no: "03",
    name: "Cline",
    subtitle: "单任务边界与显式交接",
    category: "任务边界优先",
    positioning: "编辑器编码 Agent；以单个长期任务（Task）组织对话和执行。",
    state: "任务存储 · 命令与修改记录 · Git 状态检查点",
    context: "自动压缩（Auto Compact）；/newtask 生成明确的交接材料。",
    recovery: "使用完整任务记录和 Git、文件状态恢复。",
    steps: [
      ["创建任务", "记录目标、ID 与独立目录"],
      ["持续执行", "对话、命令、修改留痕"],
      ["自动压缩", "接近窗口上限时生成摘要"],
      ["创建新任务", "范围变化时生成交接摘要"],
    ],
    borrow: [
      "每个 Case 只承载一个明确诊断目标",
      "范围变化时新 Case / Revision",
      "跨 Agent 使用结构化交接材料",
      "诊断流程检查点与被诊断系统检查点分开管理",
    ],
    limits: [
      "摘要服务于继续编码",
      "不保证每个事实都有证据",
      "摘要可能遗漏“已排除”标记，使旧假设再次出现",
    ],
    verdict: "长任务可恢复，但诊断交接必须比摘要更结构化。",
    src: [
      ["Task Management", "https://docs.cline.bot/core-workflows/task-management"],
      ["Auto Compact", "https://docs.cline.bot/features/auto-compact"],
    ],
  },
  {
    no: "04",
    name: "AutoGen",
    subtitle: "可插拔的模型上下文策略",
    category: "模型输入策略优先",
    positioning: "有状态 Agent 与团队框架；将模型输入窗口抽象为可替换策略。",
    state: "Agent 状态 · 团队状态 · 消息上下文",
    context: "全量 · 固定条数 · Token 上限 · 保留首尾",
    recovery: "通过 save_state 和 load_state 保存、恢复 Agent 或团队状态。",
    steps: [
      ["接收消息", "Agent 或团队持续接收事件"],
      ["执行输入策略", "按条数、Token 预算或首尾规则筛选"],
      ["调用模型", "模型只接收筛选后的输入"],
      ["保存与恢复", "序列化 Agent 或团队状态"],
    ],
    borrow: [
      "把模型输入策略定义为明确接口",
      "Agent Backend 与输入构造逻辑解耦",
      "同一 Job 可对不同输入策略做 A/B 评测",
      "状态保存与窗口管理分开讨论",
    ],
    limits: [
      "解决的是“哪些消息提供给模型”",
      "不定义“哪些诊断事实属于权威状态”",
      "外部 Case 与团队状态可能形成双写冲突",
      "官方已进入维护模式；新项目建议评估 Microsoft Agent Framework",
    ],
    verdict: "可借鉴可插拔输入策略，但模型上下文不能决定权威业务状态。",
    src: [
      ["Model Context", "https://microsoft.github.io/autogen/stable/reference/python/autogen_core.model_context.html"],
      ["Repository", "https://github.com/microsoft/autogen"],
    ],
  },
  {
    no: "05",
    name: "CrewAI",
    subtitle: "Flow 控流程，Crew 自主执行",
    category: "控制与执行分层",
    positioning: "Crew 负责多 Agent 自主协作，事件驱动的 Flow 负责编排。",
    state: "类型化流程状态 · Agent 记忆 · 任务上下文",
    context: "任务上下文配合自动摘要；Agent 只在局部任务内自主。",
    recovery: "Flow 状态可以持久化；Agent 记忆能否随恢复加载，取决于具体存储与应用配置。",
    steps: [
      ["启动 Flow", "用确定性逻辑建立状态"],
      ["监听与路由", "按事件修改状态并选择路径"],
      ["Crew 执行", "专家在有限任务内自主协作"],
      ["提交结果", "结果回传 Flow，由控制面决定后续状态"],
    ],
    borrow: [
      "由 Application Service / Coordinator 承担确定性编排",
      "专家 Agent 只在 Job 内自主",
      "类型化状态提供清晰边界",
      "控制面和推理面分层",
    ],
    limits: [
      "Agent 记忆与流程状态容易形成双写冲突",
      "自动摘要不等于领域状态迁移",
      "多 Agent 不会自动带来可审计的状态迁移和审核机制",
    ],
    verdict: "确定性 Flow 管理业务状态，Crew 只提交建议和结果。",
    src: [
      ["Documentation", "https://docs.crewai.com/"],
      ["Repository", "https://github.com/crewAIInc/crewAI"],
    ],
  },
  {
    no: "06",
    name: "Aider",
    subtitle: "工程状态落在 Git 和文件中，会话仅用于协作",
    category: "工程事实优先",
    positioning: "对话驱动的代码修改 Agent；工程事实始终保存在文件和 Git 中。",
    state: "对话记录 · Git · 当前文件 · 仓库结构图（Repo Map）",
    context: "Repo Map 选择相关代码；接近上限时摘要；支持 /clear 和 /drop。",
    recovery: "从 Git 差异和文件状态恢复工程事实，从对话记录恢复协作过程。",
    steps: [
      ["生成 Repo Map", "选择与当前请求相关的代码结构"],
      ["加入文件", "显式控制模型输入"],
      ["编辑与提交", "真实变化写入文件和 Git"],
      ["清空并重建", "会话可丢弃，工程状态可恢复"],
    ],
    borrow: [
      "将产物和证据独立于对话保存",
      "模型只读当前相关材料",
      "模型输入可从持久材料重建",
      "显式选择材料，避免历史无限累积",
    ],
    limits: [
      "依赖 Git 和人工交互",
      "不提供诊断事实与假设状态机",
      "不定义审核结果如何写入权威状态",
    ],
    verdict: "更换会话后，工程状态仍可从版本库和文件恢复；协作意图仍依赖对话记录。",
    src: [
      ["Repository", "https://github.com/Aider-AI/aider"],
      ["Commands", "https://aider.chat/docs/usage/commands.html"],
    ],
  },
  {
    no: "07",
    name: "SWE-agent",
    subtitle: "保留完整轨迹，只裁剪模型输入",
    category: "模型输入优先",
    positioning: "代码任务 Agent；历史处理器决定模型每一轮能看到哪些材料。",
    state: "原始执行轨迹 · 沙箱 · 代码仓库",
    context: "过滤早期观察结果、大段工具输出和差异内容；策略可替换。",
    recovery: "保存完整执行轨迹，并依赖环境和仓库重建执行上下文。",
    steps: [
      ["记录执行轨迹", "原样保存操作和观察结果"],
      ["处理历史材料", "调用模型前按规则筛选输入"],
      ["模型决策", "只接收经历史处理器筛选的上下文"],
      ["环境执行", "工程变化写入沙箱和代码仓库"],
    ],
    borrow: [
      "把输入处理器定义为独立扩展点",
      "大段日志只保存在原始材料和证据库",
      "输入过滤不得修改权威代码仓库",
      "不同策略可做可重复对照",
    ],
    limits: [
      "典型任务是一次性编码任务，轨迹生命周期较短",
      "没有长期 Case 合并语义",
      "没有独立诊断审核状态机",
    ],
    verdict: "完整执行轨迹保留；本轮模型输入由可替换策略生成。",
    src: [
      ["History Processor", "https://swe-agent.com/1.0/reference/history_processor_config/"],
      ["Repository", "https://github.com/SWE-agent/SWE-agent"],
    ],
  },
  {
    no: "08",
    name: "mini-SWE-agent",
    subtitle: "检验复杂机制的真实收益",
    category: "极简基线",
    positioning: "面向目标明确、周期短的编码任务；刻意保持极简。",
    state: "代码仓库 · 线性消息记录",
    context: "不做复杂压缩；消息随执行步骤线性追加。",
    recovery: "主要依赖单次任务内的线性执行记录。",
    steps: [
      ["接收任务", "固定单一目标"],
      ["追加消息", "线性积累历史"],
      ["独立 Shell 进程", "每个操作在独立进程中执行"],
      ["完成退出", "不维护长期业务状态"],
    ],
    borrow: [
      "作为不含复杂上下文治理的基准组",
      "测量摘要、事件日志和多 Agent 的真实增益",
      "量化复杂机制增加的时延与潜在故障点",
      "验证收益后再引入复杂机制",
    ],
    limits: [
      "历史线性增长",
      "没有结构化状态与恢复语义",
      "不适合长周期诊断 Case",
    ],
    verdict: "复杂设计必须在极简基线上证明收益。",
    src: [["Repository", "https://github.com/SWE-agent/mini-swe-agent"]],
  },
];

for (const ref of references) {
  add(`
  <section class="slide" data-layout="H04">
    ${brand}${chrome("03 · 参考对象", `${ref.no} · ${ref.name} · 1 / 3`)}
    <div class="content-stack slide-body">
      <div>
        <div class="kicker" data-anim>${ref.category}</div>
        <h2 class="h-xl" data-anim>${ref.name}｜${ref.subtitle}</h2>
        <p class="lead" data-anim>${ref.positioning}</p>
      </div>
      <div class="grid-3 same-size">
        ${card("主要状态载体", ref.state, "database")}
        ${card("模型输入策略", ref.context, "scan-text")}
        ${card("恢复方式", ref.recovery, "rotate-ccw")}
      </div>
      <div class="reference-signature" data-anim><span>${ref.no}</span><strong>${ref.verdict}</strong></div>
    </div>
    ${source(ref.src)}
  </section>`);

  add(`
  <section class="slide" data-layout="H08">
    ${brand}${chrome("03 · 参考对象", `${ref.no} · ${ref.name} · 2 / 3`)}
    <div class="content-stack slide-body">
      <div>
        <div class="kicker" data-anim>关键机制</div>
        <h2 class="h-xl" data-anim>${ref.name} 如何工作</h2>
      </div>
      <div class="process">
        ${ref.steps
          .map(
            ([title, body], idx) => `
          <div class="process-step" data-anim>
            <div class="num-pill">${String(idx + 1).padStart(2, "0")}</div>
            <div class="card-title">${title}</div>
            <div class="card-body">${body}</div>
          </div>`,
          )
          .join("")}
      </div>
      <div class="quote-band compact-quote" data-anim>${ref.verdict}</div>
    </div>
    ${source(ref.src)}
  </section>`);

  add(`
  <section class="slide" data-layout="H26">
    ${brand}${chrome("03 · 参考对象", `${ref.no} · ${ref.name} · 3 / 3`)}
    <div class="content-stack slide-body">
      <div>
        <div class="kicker" data-anim>对当前框架的启示</div>
        <h2 class="h-xl" data-anim>${ref.name}｜可借鉴机制与适用边界</h2>
      </div>
      <div class="grid-2 compare-panels">
        <article class="compare-panel yes" data-anim>
          <div class="compare-title"><i data-lucide="corner-down-right"></i> 可借鉴机制（I）</div>
          ${bullets(ref.borrow)}
        </article>
        <article class="compare-panel no" data-anim>
          <div class="compare-title"><i data-lucide="shield-alert"></i> 边界与风险</div>
          ${bullets(ref.limits)}
        </article>
      </div>
      <div class="verdict-line" data-anim><strong>结论：</strong>${ref.verdict}</div>
    </div>
    ${source(ref.src)}
  </section>`);
}

add(`
<section class="slide" data-layout="H26">
  ${brand}${chrome("04 · 综合比较", "01 / 03 · 按能力层次比较")}
  <div class="content-stack slide-body comparison-body">
    <div>
      <div class="kicker" data-anim>先按层分类，再比较强弱</div>
      <h2 class="h-xl" data-anim>诊断框架、编排框架与编码 Agent 不能混为一类</h2>
    </div>
    <div class="compare-group-list">
      <article class="compare-group-row" data-anim>
        <div class="compare-family"><small>诊断执行</small><strong>诊断执行层</strong></div>
        <div class="compare-projects"><b class="project-chip hot">OpenSRE</b><b class="project-chip hot">OpenDerisk</b></div>
        <div class="compare-signals">
          <span><small>强项</small>受约束调查、评测闭环与专家协作</span>
          <span><small>边界</small>缺少独立的 Case / Job 控制面和可重建执行契约</span>
        </div>
      </article>
      <article class="compare-group-row" data-anim>
        <div class="compare-family"><small>状态与记录</small><strong>状态与记录层</strong></div>
        <div class="compare-projects"><b class="project-chip">LangGraph</b><b class="project-chip">OpenHands</b></div>
        <div class="compare-signals">
          <span><small>强项</small>状态检查点、重放与只追加事件留痕</span>
          <span><small>边界</small>不提供诊断领域事实、证据质量与审核规则</span>
        </div>
      </article>
      <article class="compare-group-row" data-anim>
        <div class="compare-family"><small>任务与控制</small><strong>任务与控制层</strong></div>
        <div class="compare-projects"><b class="project-chip">Cline</b><b class="project-chip">AutoGen</b><b class="project-chip">CrewAI</b></div>
        <div class="compare-signals">
          <span><small>强项</small>任务边界、模型输入策略和控制/执行分层</span>
          <span><small>边界</small>会话或 Agent 记忆可能与权威状态发生双写</span>
        </div>
      </article>
      <article class="compare-group-row" data-anim>
        <div class="compare-family"><small>材料与轨迹</small><strong>材料与轨迹层</strong></div>
        <div class="compare-projects"><b class="project-chip">Aider</b><b class="project-chip">SWE-agent</b><b class="project-chip">mini-SWE</b></div>
        <div class="compare-signals">
          <span><small>强项</small>Git 保存工程事实；完整轨迹与模型输入分离</span>
          <span><small>边界</small>不覆盖长生命周期 Case 和独立审核角色</span>
        </div>
      </article>
    </div>
  </div>
</section>`);

add(`
<section class="slide" data-layout="H26">
  ${brand}${chrome("04 · 综合比较", "02 / 03 · 四类信息分工")}
  <div class="content-stack slide-body comparison-body">
    <div>
      <div class="kicker" data-anim>权威状态 · 原始记录 · 模型输入 · 可重建材料</div>
      <h2 class="h-xl" data-anim>状态、记录、模型输入和可重建材料必须分开</h2>
    </div>
    <div class="pattern-grid">
      <article class="pattern-card truth" data-anim>
        <div class="pattern-head"><small>01 · 权威状态</small><strong>记录当前正式状态</strong></div>
        <div class="pattern-projects"><b>LangGraph</b><b>CrewAI Flow</b><b>OpenSRE 调查状态</b><b>OpenDerisk 论文</b></div>
        <p>只有领域字段、唯一写入者、持久化和写入规则齐备，才能作为权威业务状态。</p>
      </article>
      <article class="pattern-card history" data-anim>
        <div class="pattern-head"><small>02 · 原始记录</small><strong>只追加事件和执行轨迹</strong></div>
        <div class="pattern-projects"><b>OpenHands</b><b>SWE-agent</b><b>Cline 任务记录</b></div>
        <p>用于审计和还原调查过程，但不能替代当前正式状态。</p>
      </article>
      <article class="pattern-card view" data-anim>
        <div class="pattern-head"><small>03 · 本轮模型输入</small><strong>按预算选取相关材料</strong></div>
        <div class="pattern-projects"><b>OpenSRE</b><b>OpenDerisk 论文</b><b>AutoGen</b><b>SWE-agent</b><b>Cline / Aider</b></div>
        <p>裁剪、摘要、去重和 Repo Map 都是输入选择规则，必须版本化并可解释。</p>
      </article>
      <article class="pattern-card artifact" data-anim>
        <div class="pattern-head"><small>04 · 可重建材料</small><strong>用于复核和恢复的原始依据</strong></div>
        <div class="pattern-projects"><b>Aider / Git</b><b>SWE-agent / Repo</b><b>Cline / Git</b><b>mini-SWE / Repo</b></div>
        <p>代码场景依靠 Git 和文件；诊断场景依靠 Case、Blob、日志、Trace 与证据。</p>
      </article>
    </div>
  </div>
</section>`);

add(`
<section class="slide" data-layout="H42">
  ${brand}${chrome("04 · 综合比较", "03 / 03 · 组合结论")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>候选机制组合（尚待评审）</div>
      <h2 class="h-xl" data-anim>候选组合是一条“可重建、可审核”的链</h2>
    </div>
    <div class="synthesis-chain" data-anim>
      <div><small>权威状态</small><strong>权威业务状态</strong><span>Case / DiagnosisState</span></div>
      <i data-lucide="chevron-right"></i>
      <div><small>原始记录</small><strong>只追加事件</strong><span>完整保留调查过程</span></div>
      <i data-lucide="chevron-right"></i>
      <div><small>模型输入</small><strong>单次模型输入包</strong><span>ContextPack · 按预算选择材料</span></div>
      <i data-lucide="chevron-right"></i>
      <div><small>受控执行</small><strong>Agent</strong><span>预算、权限和停止条件</span></div>
      <i data-lucide="chevron-right"></i>
      <div><small>独立审核</small><strong>审核 Agent</strong><span>Reviewer · 通过后写回正式状态</span></div>
    </div>
    <div class="grid-3 same-size">
      ${card("控制面", "由应用服务与协调器用确定性逻辑维护业务状态（Application Service / Coordinator）。", "workflow")}
      ${card("执行面（候选）", "Agent 仅在单个 Job 内自主；会话应能从执行输入清单重建。", "bot")}
      ${card("评测面", "以 mini-SWE-agent 为极简基线，验证每项复杂机制是否有量化收益。", "test-tube-diagonal")}
    </div>
    <div class="quote-band compact-quote" data-anim>
      本次调研的 10 个项目中，没有一个同时解决权威业务状态、调查执行、证据审核和长期恢复。
    </div>
  </div>
</section>`);

add(`
<section class="slide" data-layout="H27">
  ${brand}${chrome("05 · 当前设计", "预比较 · 非最终结论")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>保留稳定方向，重新评审冲突项</div>
      <h2 class="h-xl" data-anim>当前设计符合调研共性，会话策略单独评审</h2>
    </div>
    <div class="grid-2 compare-panels">
      <article class="compare-panel yes" data-anim>
        <div class="compare-title"><i data-lucide="lock-keyhole"></i> 已明确的设计原则</div>
        ${bullets([
          "应用服务是业务状态的唯一写入者（Application Service）",
          "诊断协调器采用确定性逻辑且无副作用（Diagnosis Coordinator）",
          "案例数据与大文件分开存储",
          "执行器输入输出类型明确；诊断技能可版本化，Agent 后端可替换",
        ])}
      </article>
      <article class="compare-panel no" data-anim>
        <div class="compare-title"><i data-lucide="circle-help"></i> 详细设计必须回答</div>
        ${bullets([
          "会话中隐藏了哪些继续诊断所必需的状态？",
          "会话丢失后，能否从任务输入清单（JobContextManifest）完整重建？",
          "审核结论如何通过应用服务迁移业务状态？",
        ])}
      </article>
    </div>
    <div class="future-strip" data-anim>
      <strong>框架定稿后追加 4—6 页</strong>
      <span>能力覆盖</span><span>控制与状态</span><span>上下文</span><span>证据审核</span><span>安全治理</span><span>评测运维</span>
    </div>
  </div>
  ${source([
    ["当前总体设计", "../../design/v1-overall-framework.md"],
    ["目标架构", "../../design/target-diagnosis-architecture.md"],
    ["上下文调研", "../high-star-agent-context-strategy-survey.md"],
  ])}
</section>`);

add(`
<section class="slide" data-layout="H20">
  ${brand}${chrome("05 · 当前设计", "框架定稿后追加")}
  <div class="content-stack slide-body">
    <div>
      <div class="kicker" data-anim>框架定稿后补充</div>
      <h2 class="h-xl" data-anim>下一版补充六维正式差距分析</h2>
      <p class="lead" data-anim>每项同时写清：V1、目标架构、暂不考虑项、优势与差距、主动取舍、验证指标和负责人。</p>
    </div>
    <div class="grid-3 same-size reserve-grid">
      ${card("01 · 能力覆盖", "接入、建模、工具规划、调查、证据、审核、报告、修复、反馈。", "layout-list")}
      ${card("02 · 控制与状态", "唯一写入者、Coordinator 的确定性与无副作用约束、Job/Attempt 幂等、重试与恢复。", "waypoints")}
      ${card("03 · 上下文", "会话生命周期、ContextPack 结构定义、策略版本、预算与复现。", "brackets")}
      ${card("04 · 证据审核", "诊断判断状态、支持与反驳、证据来源、Reviewer 隔离及状态变更规则。", "badge-check")}
      ${card("05 · 安全治理", "凭证、脱敏、遥测、只读/写权限、审批、审计、多租户。", "shield-check")}
      ${card("06 · 评测运维", "准确率、错误结论率、证据完整度、工具效率、时延、成本、人工接管率。", "gauge")}
    </div>
    <div class="verdict-line" data-anim><strong>交付原则：</strong>任何“领先或落后”结论都必须有对应测试、量化指标，或明确说明是主动取舍。</div>
  </div>
</section>`);

add(`
<section class="slide dark" data-layout="H10">
  ${brand}${chrome("05 · 结论", "技术诊断会建议")}
  <div class="close-layout">
    <div data-anim>
      <div class="kicker">最终建议</div>
      <h2 class="h-hero">先把正确性约束<br>固化到结构与流程</h2>
      <p class="lead">再让 Agent 在预算、证据和审核约束下自主执行。</p>
    </div>
    <div class="decision-list">
      <div data-anim><span>01</span><strong>Case / DiagnosisState 是唯一权威业务状态</strong></div>
      <div data-anim><span>02</span><strong>每次 JobAttempt 的输入都被持久保存，并可准确复现</strong></div>
      <div data-anim><span>03</span><strong>证据、诊断判断、审核结论和状态变更全程可审计</strong></div>
      <div data-anim><span>04</span><strong>同时评测准确率、错误结论率、证据完整度、时延和成本</strong></div>
    </div>
  </div>
  <div class="closing-note" data-anim>本页四项为本轮建议评审事项，不是当前正式设计已确认结论。</div>
</section>`);

if (slides.length !== 49) {
  throw new Error(`Expected 49 slides, got ${slides.length}`);
}

const customCss = `
  /* Project-specific additions. Huawei Cloud Tech preset only. */
  .chrome{right:16vw}
  .red-text{color:var(--brand-red)}
  .nowrap{white-space:nowrap}
  .slide-body{padding-top:7.4vh;min-height:84vh;justify-content:center}
  .agenda-item .txt{display:flex;align-items:center;justify-content:space-between;gap:1vw}
  .agenda-item .txt small{font:700 .58vw var(--mono);color:var(--brand-red);letter-spacing:.06em}
  .agenda-fastpath{display:flex;align-items:center;justify-content:space-between;gap:1vw;padding:1.25vh 1.2vw;background:var(--brand-black);color:#fff}
  .agenda-fastpath strong{font:800 .7vw var(--mono);letter-spacing:.05em}.agenda-fastpath span{font-size:.76vw;color:rgba(255,255,255,.76)}
  .section-line{margin:3vh 0}
  .deck-source{
    position:absolute;left:7vw;right:7vw;bottom:4.7vh;
    font-size:.62vw;color:var(--muted);line-height:1.35;z-index:4;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  }
  .deck-source a{color:inherit;text-decoration:none;border-bottom:1px dotted currentColor}
  .dark .deck-source{color:rgba(255,255,255,.68)}
  .tag-row{display:flex;gap:.65vw;align-items:center;flex-wrap:wrap}
  .plain-tag,.evidence-tag{
    display:inline-flex;align-items:center;min-height:2.7vh;padding:.45vh .7vw;
    border:1px solid var(--line-strong);background:#fff;color:var(--ink);
    font-family:var(--mono);font-size:.68vw;font-weight:700;letter-spacing:.02em;
  }
  .evidence-tag.f{border-color:var(--brand-red);color:var(--brand-red)}
  .evidence-tag.s{background:var(--brand-red-soft);border-color:var(--brand-red-soft);color:var(--brand-red-dark)}
  .evidence-tag.i{background:var(--brand-grey)}
  .evidence-tag.t{background:var(--brand-black);border-color:var(--brand-black);color:#fff}
  .signal-orbit{position:absolute;border:1px solid rgba(var(--brand-red-rgb),.32);border-radius:50%}
  .orbit-a{width:29vw;height:29vw;right:0;top:2vh}
  .orbit-b{width:20vw;height:20vw;right:4.5vw;top:9vh;border-style:dashed}
  .signal-node{
    position:absolute;display:grid;place-items:center;width:6.1vw;height:6.1vw;
    border-radius:50%;background:#fff;border:1px solid var(--line);box-shadow:var(--shadow-soft);
    font:700 .7vw var(--mono);color:var(--ink);
  }
  .signal-node.n1{right:20vw;top:6vh}.signal-node.n2{right:1vw;top:12vh}
  .signal-node.n3{right:17vw;top:36vh;background:var(--brand-red);color:#fff;border:0}
  .signal-node.n4{right:0;top:42vh}
  .compact-quote{font-size:1.55vw;padding:2.1vh 2vw;line-height:1.3}
  .legend-stack{display:flex;flex-direction:column;gap:1.4vh}
  .legend-row{display:grid;grid-template-columns:10vw 1fr;gap:1.2vw;align-items:center;border-bottom:1px solid var(--line);padding-bottom:1.2vh;color:var(--muted);font-size:.92vw}
  .project-head{display:flex;align-items:center;gap:1vw;margin-bottom:2vh}
  .project-monogram{width:4vw;height:4vw;border-radius:50%;display:grid;place-items:center;background:var(--brand-red);color:#fff;font:800 1vw var(--mono)}
  .project-monogram.grey{background:var(--brand-grey);color:var(--muted)}
  .focus-card{border-top:4px solid var(--brand-red)}
  .muted-card{background:var(--brand-grey)}
  .clean-list{list-style:none;display:grid;gap:1.1vh;color:var(--muted);font-size:.95vw;line-height:1.45}
  .clean-list li{padding-left:1.2vw;position:relative}
  .clean-list li::before{content:"";position:absolute;left:0;top:.58em;width:.5vw;height:.5vw;border-radius:50%;background:var(--brand-red)}
  .process-5{grid-template-columns:repeat(5,1fr)}
  .process-5 .process-step{min-height:26vh;padding:2.5vh 1.25vw}
  .layer-stack{display:flex;flex-direction:column;gap:1.2vh}
  .layer-row{display:grid;grid-template-columns:3.6vw 15vw 1fr 2.4vw;align-items:center;gap:1.2vw;padding:2.1vh 1.4vw;background:#fff;border:1px solid var(--line);box-shadow:var(--shadow-soft)}
  .layer-row.emphasized{background:var(--brand-red);color:#fff;border-color:var(--brand-red)}
  .layer-row.emphasized span,.layer-row.emphasized strong{color:#fff}
  .layer-row .layer-no{font:800 .82vw var(--mono);color:var(--brand-red)}
  .layer-row strong{font-size:1.1vw}.layer-row > span:nth-child(3){color:var(--muted);font-size:.88vw}
  .layer-row .lucide{width:1.6vw}
  .mini-callout{padding:1.6vh 1.4vw;background:#fff;border-left:4px solid var(--brand-red);display:flex;gap:1vw;align-items:center;font-size:.86vw;color:var(--muted)}
  .mini-callout.warning{border-left-color:var(--brand-black)}
  .flow-6{display:grid;grid-template-columns:repeat(6,1fr);gap:1.1vw;position:relative}
  .flow-6::before{content:"";position:absolute;left:3%;right:3%;top:2.4vw;height:3px;background:var(--line-strong);z-index:0}
  .pipeline-input{width:max-content;display:flex;align-items:center;gap:.8vw;padding:.7vh 1vw;background:var(--brand-black);color:#fff;font-size:.72vw}
  .pipeline-input span{font:700 .6vw var(--mono);color:rgba(255,255,255,.7)}.pipeline-input strong{font:.78vw var(--mono)}.pipeline-input small{color:rgba(255,255,255,.72)}
  .compact-flow{margin-top:-.2vh}
  .flow-node{position:relative;z-index:1;text-align:center;padding:0 .35vw}
  .flow-node > span{width:4.8vw;height:4.8vw;border-radius:50%;display:grid;place-items:center;margin:0 auto 1.4vh;background:#fff;border:3px solid var(--brand-red);color:var(--brand-red);font:800 .86vw var(--mono)}
  .flow-node.hot > span{background:var(--brand-red);color:#fff}
  .flow-node strong{display:block;font:800 .86vw var(--mono);margin-bottom:.7vh}
  .flow-node small{display:block;color:var(--muted);font-size:.84vw;line-height:1.4}
  .metric-card{min-height:20vh;background:#fff;border:1px solid var(--line);padding:2.5vh 1.5vw;display:flex;flex-direction:column;justify-content:center}
  .metric-card strong{font-size:1vw;margin-top:1vh}.metric-card span{font-size:.78vw;color:var(--muted);margin-top:.45vh}
  .truth-view-diagram{display:grid;grid-template-columns:1fr 17vw 1fr;gap:2vw;align-items:center}
  .truth-box,.view-box{min-height:27vh;padding:3.2vh 2.2vw;background:#fff;border:1px solid var(--line);display:flex;flex-direction:column;justify-content:center}
  .truth-box{border-top:5px solid var(--brand-red)}.view-box{border-top:5px solid var(--brand-black)}
  .box-label{font:700 .7vw var(--mono);letter-spacing:.08em;color:var(--brand-red);margin-bottom:1.5vh}
  .truth-box strong,.view-box strong{font-size:1.35vw;margin-bottom:1vh}.truth-box span,.view-box span{font-size:.9vw;color:var(--muted)}
  .compiler-box{text-align:center;padding:2.2vh 1vw;border:1px dashed var(--line-strong);background:var(--brand-grey)}
  .compiler-box .lucide{width:2.3vw;height:2.3vw;color:var(--brand-red);margin-bottom:1vh}
  .compiler-box strong{display:block;font-size:1vw}.compiler-box small{display:block;color:var(--muted);font-size:.72vw;line-height:1.45;margin-top:.6vh}
  .dual-chain{display:flex;flex-direction:column;gap:1.5vh}
  .chain-row{display:grid;grid-template-columns:8vw 1fr 1.3vw 1fr 1.3vw 1fr;gap:.8vw;align-items:center;padding:1.3vh 1.2vw;background:#fff;border:1px solid var(--line);border-left:5px solid var(--brand-red)}
  .chain-row.secondary{border-left-color:var(--brand-black);background:var(--brand-grey)}
  .chain-label{font:800 .72vw var(--mono);color:var(--brand-red)}.chain-row.secondary .chain-label{color:var(--brand-black)}
  .chain-box{min-height:8.5vh;padding:1.1vh .8vw;background:#fff;border:1px solid var(--line);display:flex;flex-direction:column;justify-content:center}
  .chain-box.compiler{background:var(--brand-red-soft)}.chain-box small{font:.56vw var(--mono);color:var(--muted);margin-bottom:.35vh}.chain-box strong{font-size:.78vw}
  .chain-row > .lucide{width:1.2vw;color:var(--brand-red)}
  .risk-strip,.future-strip{display:flex;align-items:center;gap:1vw;padding:1.55vh 1.5vw;background:var(--brand-black);color:#fff}
  .risk-strip strong,.future-strip strong{margin-right:auto;font-size:.9vw}.risk-strip span,.future-strip span{padding:.45vh .62vw;border:1px solid rgba(255,255,255,.35);font-size:.72vw}
  .loop-ring{position:relative;height:34vh}
  .loop-center{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:8vw;height:8vw;border-radius:50%;background:var(--brand-red);color:#fff;display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:2}
  .loop-center strong{font-size:1.3vw}.loop-center span{font-size:.7vw}
  .loop-item{position:absolute;width:8vw;height:4.5vw;border-radius:99px;background:#fff;border:1px solid var(--line);display:grid;place-items:center;text-align:center;font-size:.78vw;font-weight:700;box-shadow:var(--shadow-soft)}
  .loop-item.l1{left:4%;top:2%}.loop-item.l2{right:4%;top:2%}.loop-item.l3{right:0;bottom:4%}.loop-item.l4{left:50%;bottom:0;transform:translateX(-50%)}.loop-item.l5{left:0;bottom:4%}
  .compare-panels{align-items:stretch}
  .compare-panel{padding:2.6vh 2vw;background:#fff;border:1px solid var(--line);min-height:38vh}
  .compare-panel.yes{border-top:5px solid var(--brand-red)}.compare-panel.no{border-top:5px solid var(--brand-black)}
  .compare-title{display:flex;align-items:center;gap:.7vw;font-size:1.25vw;font-weight:800;margin-bottom:2vh}
  .compare-title .lucide{width:1.6vw;color:var(--brand-red)}
  .compare-panel .bullet-list{gap:1.4vh}.compare-panel .card-body{font-size:.9vw}
  .verdict-line{padding:1.5vh 1.5vw;background:var(--brand-grey);border-left:5px solid var(--brand-red);font-size:.96vw}
  .candidate-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1.2vw}
  .candidate-card{min-height:11vh;padding:1.5vh 1.2vw;background:#fff;border:1px solid var(--line);display:grid;grid-template-columns:1fr auto;gap:.45vh 1vw;align-items:center}
  .candidate-card strong{font-size:1vw}.candidate-card span{grid-column:1/-1;color:var(--muted);font-size:.84vw}.candidate-card b{font:700 .66vw var(--mono);color:var(--muted)}
  .candidate-card.selected{background:var(--brand-red);color:#fff;border-color:var(--brand-red)}.candidate-card.selected span,.candidate-card.selected b{color:#fff}
  .attribution-note{display:flex;align-items:center;gap:1vw;padding:1.4vh 1.2vw;background:var(--brand-red-soft);color:var(--brand-red-dark);font-size:.82vw}
  .attribution-note .lucide{width:1.4vw}
  .architecture-3{display:grid;grid-template-columns:1fr 1.35fr 1fr;gap:1.4vw;align-items:stretch}
  .arch-band{min-height:28vh;padding:2.5vh 1.7vw;background:#fff;border:1px solid var(--line);display:flex;flex-direction:column;justify-content:center;gap:1.7vh}
  .arch-band.core{background:var(--brand-red);color:#fff;border-color:var(--brand-red)}
  .arch-band > div:first-child{display:flex;align-items:center;gap:.8vw}.arch-band > div:first-child span{font:800 .78vw var(--mono);opacity:.75}
  .arch-band strong{font-size:1.15vw}.arch-band p{font-size:.82vw;line-height:1.55;color:var(--muted)}.arch-band.core p{color:#fff}
  .arch-pills{display:grid;gap:.8vh}.arch-pills b{padding:.8vh .7vw;background:rgba(255,255,255,.16);font-size:.75vw}
  .hub-diagram{position:relative;height:40vh}
  .hub-center{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:13vw;height:13vw;border-radius:50%;background:var(--brand-red);color:#fff;display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:2;box-shadow:0 18px 35px rgba(var(--brand-red-rgb),.3)}
  .hub-center .lucide{width:2.2vw;height:2.2vw;margin-bottom:.6vh}.hub-center strong{font-size:1.2vw}.hub-center span{font-size:.7vw}
  .hub-diagram::before{content:"";position:absolute;left:22%;right:22%;top:50%;height:1px;background:var(--line-strong);box-shadow:0 -14vh 0 var(--line-strong),0 14vh 0 var(--line-strong)}
  .hub-node{position:absolute;width:8vw;height:6vw;background:#fff;border:1px solid var(--line);box-shadow:var(--shadow-soft);display:flex;flex-direction:column;align-items:center;justify-content:center}
  .hub-node strong{font-size:.9vw}.hub-node span{font-size:.7vw;color:var(--muted)}
  .hub-node.h1{left:9%;top:2%}.hub-node.h2{left:9%;bottom:2%}.hub-node.h3{right:9%;top:2%}.hub-node.h4{right:9%;bottom:2%}.hub-node.h5{left:50%;top:0;transform:translateX(-50%)}
  .mode-strip{display:grid;grid-template-columns:repeat(3,1fr);gap:1vw}.mode-strip > div{padding:1.2vh 1vw;background:#fff;border-left:4px solid var(--brand-red);display:flex;justify-content:space-between}.mode-strip b{font:.85vw var(--mono)}.mode-strip span{font-size:.72vw;color:var(--muted)}
  .decision-ladder{display:grid;grid-template-columns:7vw 1fr 7vw;align-items:center;gap:1vw;color:var(--muted);font-size:.75vw}
  .decision-ladder > div{display:flex;align-items:center;gap:1vw;background:var(--brand-grey);padding:1.2vh 1.2vw}.decision-ladder i{height:2px;background:var(--brand-red);flex:1}.decision-ladder b{color:var(--ink);white-space:nowrap}
  .context-stack{display:flex;flex-direction:column;align-items:center;gap:.7vh}
  .context-layer{width:76%;padding:1.35vh 1.4vw;background:#fff;border:1px solid var(--line);display:flex;justify-content:space-between;box-shadow:var(--shadow-soft)}
  .context-layer:nth-child(2){width:84%}.context-layer:nth-child(3){width:92%}.context-layer:nth-child(4){width:100%;background:var(--brand-black);color:#fff}
  .context-layer b{font-size:.9vw}.context-layer span{font-size:.78vw;color:var(--muted)}.context-layer:last-child span{color:rgba(255,255,255,.75)}
  .layer-evidence{display:inline-grid;place-items:center;min-width:1.35vw;height:1.35vw;margin-right:.35vw;border-radius:50%;font:800 .55vw var(--mono);font-style:normal}
  .layer-evidence.s{background:var(--brand-red);color:#fff}.layer-evidence.i{background:var(--brand-black);color:#fff}
  .context-layer:last-child .layer-evidence.i{background:#fff;color:var(--brand-black)}
  .slim-process .process-step{min-height:14vh;padding:1.7vh 1vw}.slim-process .card-title{font-size:1vw;white-space:normal}
  .model-label{width:max-content;padding:.45vh .7vw;background:var(--brand-black);color:#fff;font:700 .62vw var(--mono)}
  .evidence-chain{display:grid;grid-template-columns:1fr auto 1fr auto 1fr auto 1fr;gap:.7vw;align-items:center}
  .evidence-chain > div{padding:1.25vh 1vw;background:#fff;border:1px solid var(--line);text-align:center}.evidence-chain small{display:block;font:.62vw var(--mono);color:var(--brand-red);margin-bottom:.4vh}.evidence-chain strong{font-size:.8vw}.evidence-chain > span{font:.58vw var(--mono);color:var(--muted)}
  .card-note{margin-top:1.4vh}.mini-metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:1vw}.mini-metrics > div{padding:1.25vh 1vw;background:var(--brand-grey);display:flex;align-items:baseline;gap:.7vw}.mini-metrics b{font:800 1.6vw var(--mono);color:var(--brand-red)}.mini-metrics span{font-size:.72vw;color:var(--muted)}
  .evidence-warning{display:flex;align-items:center;gap:1vw;font-size:.75vw;color:var(--muted)}
  .reference-signature{display:grid;grid-template-columns:4vw 1fr;align-items:center;background:var(--brand-black);color:#fff}.reference-signature span{height:5.5vh;display:grid;place-items:center;background:var(--brand-red);font:800 .9vw var(--mono)}.reference-signature strong{padding:0 1.2vw;font-size:.9vw}
  .comparison-body{gap:1.4vh}
  .compare-group-list{display:grid;grid-template-rows:repeat(4,1fr);gap:1vh}
  .compare-group-row{display:grid;grid-template-columns:11.5vw 22vw 1fr;align-items:center;gap:1.2vw;min-height:11.5vh;padding:1.2vh 1.25vw;background:#fff;border:1px solid var(--line);border-left:5px solid var(--brand-red);box-shadow:var(--shadow-soft)}
  .compare-family{display:flex;flex-direction:column;gap:.4vh}.compare-family small{font:700 .68vw var(--mono);color:var(--brand-red);letter-spacing:.05em}.compare-family strong{font-size:1vw}
  .compare-projects{display:flex;gap:.45vw;flex-wrap:wrap}.project-chip{padding:.55vh .6vw;background:var(--brand-grey);border:1px solid var(--line);font:.68vw var(--mono)}.project-chip.hot{background:var(--brand-red);border-color:var(--brand-red);color:#fff}
  .compare-signals{display:grid;grid-template-columns:1fr 1fr;gap:1vw;font-size:.95vw;line-height:1.4;color:var(--muted)}.compare-signals span{display:flex;flex-direction:column}.compare-signals small{font:700 .68vw var(--mono);color:var(--ink);margin-bottom:.25vh}
  .pattern-grid{display:grid;grid-template-columns:1fr 1fr;gap:1.1vh 1vw}
  .pattern-card{min-height:18vh;padding:1.35vh 1.25vw;background:#fff;border:1px solid var(--line);border-top:4px solid var(--brand-red);box-shadow:var(--shadow-soft);display:flex;flex-direction:column;justify-content:center;gap:.8vh}
  .pattern-card.history,.pattern-card.artifact{border-top-color:var(--brand-black)}
  .pattern-head{display:flex;align-items:baseline;justify-content:space-between;gap:1vw}.pattern-head small{font:700 .68vw var(--mono);color:var(--brand-red)}.pattern-card.history .pattern-head small,.pattern-card.artifact .pattern-head small{color:var(--ink)}.pattern-head strong{font-size:1vw}
  .pattern-projects{display:flex;gap:.4vw;flex-wrap:wrap}.pattern-projects b{padding:.35vh .5vw;background:var(--brand-grey);font:.68vw var(--mono)}
  .pattern-card p{font-size:.9vw;line-height:1.4;color:var(--muted)}
  .recovery-strip{display:grid;grid-template-columns:repeat(4,1fr);background:var(--brand-black);color:#fff}.recovery-strip div{min-height:6.8vh;padding:1vh 1vw;border-right:1px solid rgba(255,255,255,.18);display:flex;flex-direction:column;justify-content:center}.recovery-strip div:last-child{border-right:0}.recovery-strip b{font:.67vw var(--mono);color:#fff}.recovery-strip span{font-size:.65vw;color:rgba(255,255,255,.72);margin-top:.25vh}
  .level{display:inline-block;min-width:3vw;padding:.25vh .45vw;text-align:center;font:700 .58vw var(--mono);background:var(--brand-grey)}.level.high{background:var(--brand-red-soft);color:var(--brand-red-dark)}.level.low{background:#fff;border:1px solid var(--line)}
  .synthesis-chain{display:grid;grid-template-columns:repeat(9,auto);gap:.6vw;align-items:center}.synthesis-chain > div{min-height:15vh;padding:1.4vh 1vw;background:#fff;border-top:4px solid var(--brand-red);display:flex;flex-direction:column;justify-content:center}.synthesis-chain small{font:.68vw var(--mono);color:var(--brand-red);margin-bottom:.55vh}.synthesis-chain strong{font-size:.9vw}.synthesis-chain span{font-size:.78vw;color:var(--muted);margin-top:.5vh;line-height:1.35}.synthesis-chain > .lucide{width:1vw;color:var(--brand-red)}
  .close-layout{height:100%;display:grid;grid-template-columns:.92fr 1.08fr;gap:5vw;align-items:center}.decision-list{display:flex;flex-direction:column;gap:1.3vh}.decision-list > div{display:grid;grid-template-columns:4vw 1fr;align-items:center;background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.24);min-height:8vh}.decision-list span{height:100%;display:grid;place-items:center;background:#fff;color:var(--brand-red);font:800 .85vw var(--mono)}.decision-list strong{padding:0 1.2vw;font-size:.95vw}.closing-note{position:absolute;left:7vw;bottom:6vh;color:rgba(255,255,255,.78);font-size:.8vw}
  .reserve-grid{grid-auto-rows:1fr}.reserve-grid .card{padding:2.2vh 1.6vw}.reserve-grid .icon-disc{width:3.6vw;height:3.6vw}.reserve-grid .icon-disc .lucide{width:1.6vw;height:1.6vw}

  /* OpenSRE four-slide narrative */
  .opensre-overview{
    height:100%;display:grid;grid-template-rows:auto auto auto 36vh auto;
    gap:0;align-content:space-between;padding-top:3.4vh;padding-bottom:2.8vh;
  }
  .opensre-overview header,.opensre-process-slide header,.opensre-case-slide header{display:grid;gap:.55vh}
  .opensre-overview .h-xl{font-size:min(3.15vw,5.65vh)}
  .opensre-process-slide .h-xl,.opensre-case-slide .h-xl{font-size:min(2.75vw,4.95vh)}
  .opensre-definition{
    display:grid;grid-template-columns:7.2vw 1fr;gap:1.2vw;align-items:center;
    padding:1.35vh 1.35vw;background:#fff;border-left:5px solid var(--brand-red);
    box-shadow:var(--shadow-soft);
  }
  .opensre-definition strong{font-size:1.03vw;color:var(--brand-red)}
  .opensre-definition p{font-size:.98vw;line-height:1.48;color:#4d5764}
  .opensre-purpose-wrap{display:grid;grid-template-rows:auto auto;gap:.55vh}
  .purpose-label{font:800 .74vw var(--mono);letter-spacing:.04em;color:var(--brand-red)}
  .opensre-purpose{
    display:grid;grid-template-columns:auto 1.1vw auto 1.1vw auto 1.1vw minmax(16vw,1fr);
    gap:.65vw;align-items:center;
  }
  .opensre-purpose span{
    min-height:5.3vh;padding:0 1.1vw;display:grid;place-items:center;text-align:center;
    background:var(--brand-grey);border:1px solid var(--line);font-size:.92vw;font-weight:800;
  }
  .opensre-purpose span:last-child{background:var(--brand-black);border-color:var(--brand-black);color:#fff}
  .opensre-purpose .lucide{width:1.05vw;color:var(--brand-red)}
  .opensre-feature-grid{gap:1.2vw;min-height:0}
  .opensre-feature-grid .card{
    min-height:0;padding:2.2vh 1.4vw;border-top:4px solid var(--brand-red);
    display:flex;flex-direction:column;justify-content:center;
  }
  .opensre-feature-grid .card:nth-child(2){border-top-color:var(--brand-black)}
  .opensre-feature-grid .card:nth-child(3){border-top-color:var(--line-strong)}
  .opensre-feature-grid .feature-index{font:800 3vw var(--mono);line-height:1;color:rgba(var(--brand-red-rgb),.18);margin-bottom:1.25vh}
  .opensre-feature-grid .card-title{font-size:1.36vw;margin-bottom:.9vh}
  .opensre-feature-grid .card-body{font-size:1.04vw;line-height:1.48;color:#4d5764}
  .feature-tags{align-self:flex-start;display:flex;gap:.45vw;flex-wrap:wrap;margin-top:1.8vh}
  .feature-tags span{padding:.48vh .58vw;background:var(--brand-grey);font-size:.76vw;font-weight:700;color:#53606f}
  .opensre-boundary{
    display:flex;align-items:center;gap:.8vw;min-height:4.6vh;padding:.75vh 1vw;
    background:var(--brand-black);color:#fff;font-size:.72vw;
  }
  .opensre-boundary strong{margin-right:auto;font-size:.79vw}
  .opensre-boundary span{padding:.42vh .58vw;border:1px solid rgba(255,255,255,.32);white-space:nowrap}

  .opensre-process-slide{
    height:100%;display:grid;grid-template-rows:auto auto auto minmax(0,1fr);
    gap:.85vh;padding-top:2.7vh;padding-bottom:2.8vh;
  }
  .opensre-six-flow{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:.65vw;align-items:stretch}
  .opensre-flow-step{
    position:relative;min-height:19.2vh;padding:1.05vh .68vw .8vh;background:#fff;
    border:1px solid var(--line);border-top:4px solid var(--line-strong);
    display:grid;grid-template-rows:auto auto 1fr auto;gap:.42vh;box-shadow:var(--shadow-soft);
  }
  .opensre-flow-step:not(:last-child)::after{
    content:"";position:absolute;right:-.5vw;top:48%;width:.62vw;height:.62vw;
    border-top:2px solid var(--brand-red);border-right:2px solid var(--brand-red);
    transform:translateY(-50%) rotate(45deg);z-index:3;background:transparent;
  }
  .opensre-flow-step .step-no{
    width:1.8vw;height:1.8vw;display:grid;place-items:center;background:var(--brand-grey);
    color:var(--brand-red);font:800 .8vw var(--mono);
  }
  .opensre-flow-step strong{font-size:1.08vw;line-height:1.2}
  .opensre-flow-step p{font-size:1vw;line-height:1.27;color:#404a58}
  .opensre-flow-step small{font:.98vw var(--mono);line-height:1.1;color:#56616f;overflow-wrap:anywhere}
  .opensre-flow-step.selected{border-top-color:var(--brand-red);background:#fff7f8}
  .opensre-flow-step.active{border-color:var(--brand-red);background:var(--brand-red);color:#fff}
  .opensre-flow-step.active .step-no{background:#fff;color:var(--brand-red)}
  .opensre-flow-step.active p,.opensre-flow-step.active small{color:rgba(255,255,255,.84)}
  .stage-relation{
    display:grid;grid-template-columns:auto auto 1.3vw auto 1fr;gap:.58vw;align-items:center;
    min-height:4.8vh;padding:.7vh 1.1vw;background:var(--brand-black);color:#fff;font-size:.92vw;
  }
  .stage-relation strong{color:#fff}.stage-relation .lucide{width:1.05vw;color:#fff}
  .opensre-detail-grid{display:grid;grid-template-columns:1.05fr .95fr;gap:1vw;min-height:0}
  .ops-detail-card{
    min-height:0;padding:1.05vh 1.05vw;background:#fff;border:1px solid var(--line);
    box-shadow:var(--shadow-soft);display:grid;grid-template-rows:auto minmax(0,1fr);gap:.7vh;
  }
  .ops-detail-title{display:grid;grid-template-columns:2vw 1fr auto;gap:.65vw;align-items:center}
  .ops-detail-title > span{
    width:1.8vw;height:1.8vw;display:grid;place-items:center;background:var(--brand-red);
    color:#fff;font:800 .58vw var(--mono);
  }
  .ops-detail-title strong{font-size:1.02vw}
  .ops-detail-title small{font:.68vw var(--mono);color:#596574}
  .score-list,.runtime-list{min-height:0}
  .score-list{display:grid;grid-template-rows:repeat(5,minmax(0,1fr));gap:.38vh}
  .score-list > div{
    display:flex;align-items:center;justify-content:space-between;gap:1vw;
    padding:.48vh .72vw;background:var(--brand-grey);font-size:1vw;color:#37414e;
  }
  .score-list b{font:1vw var(--mono);color:var(--brand-red);white-space:nowrap}
  .score-list b.negative{color:var(--brand-black)}
  .runtime-list{
    list-style:none;display:grid;grid-template-rows:repeat(6,minmax(0,1fr));gap:.32vh;
  }
  .runtime-list li{
    position:relative;padding:.46vh .65vw .46vh 1.45vw;background:var(--brand-grey);
    color:#3f4a58;font-size:.94vw;line-height:1.22;
  }
  .runtime-list li::before{
    content:"";position:absolute;left:.62vw;top:50%;transform:translateY(-50%);
    width:.42vw;height:.42vw;background:var(--brand-red);border-radius:50%;
  }
  .runtime-list b{color:var(--ink)}
  .fallback-note{
    display:grid;grid-template-rows:auto auto;gap:.28vh;font-size:.84vw;line-height:1.24;
    color:#414c59;background:#fff7f8;border-left:3px solid var(--brand-red);padding:.48vh .65vw;
  }
  .fallback-note span{display:block}.fallback-note b{color:var(--brand-red)}
  .fallback-note code{font-family:var(--mono);font-size:.92em}

  .opensre-case-slide{
    height:100%;display:grid;grid-template-rows:auto auto auto minmax(0,1fr) auto auto;
    gap:.65vh;padding-top:2.8vh;padding-bottom:2.8vh;
  }
  .teaching-notice{
    width:max-content;max-width:100%;display:flex;align-items:center;gap:.55vw;
    padding:.55vh .8vw;background:var(--brand-red-soft);color:var(--brand-red-dark);
    font-size:.82vw;border-left:3px solid var(--brand-red);
  }
  .teaching-notice .lucide{width:1vw;height:1vw}
  .case-stage-rail{
    display:grid;grid-template-columns:1fr .75vw 1fr .75vw 1fr .75vw 1fr .75vw 1fr .75vw 1fr;
    gap:.28vw;align-items:center;
  }
  .case-stage-rail > span{
    min-height:3.7vh;padding:.35vh .52vw;display:flex;align-items:center;gap:.42vw;
    background:#fff;border:1px solid var(--line);font-size:.75vw;font-weight:700;white-space:nowrap;
  }
  .case-stage-rail > span b{font:.6vw var(--mono);color:var(--brand-red)}
  .case-stage-rail > span.selected{border-color:var(--brand-red);background:#fff7f8}
  .case-stage-rail > span.active{background:var(--brand-red);border-color:var(--brand-red);color:#fff}
  .case-stage-rail > span.active b{color:#fff}
  .case-stage-rail > .lucide{width:.7vw;color:var(--brand-red)}
  .case-main-grid{display:grid;grid-template-columns:.86fr 2.14fr;gap:.85vw;min-height:0}
  .case-left-column{display:grid;grid-template-rows:minmax(0,1fr) auto;gap:.62vh;min-height:0}
  .alert-card{
    min-height:0;padding:.95vh .9vw;background:#fff;border:1px solid var(--line);
    border-top:4px solid var(--brand-red);box-shadow:var(--shadow-soft);
    display:grid;grid-template-rows:auto minmax(0,1fr) auto;gap:.5vh;
  }
  .alert-card-head{display:flex;justify-content:space-between;align-items:center;gap:.5vw}
  .alert-card-head span{padding:.32vh .48vw;background:var(--brand-red);color:#fff;font:800 .58vw var(--mono)}
  .alert-card-head strong{font:.84vw var(--mono);color:#4d5764}
  .alert-card dl{display:grid;grid-template-rows:repeat(6,minmax(0,1fr));gap:.22vh}
  .alert-card dl > div{display:grid;grid-template-columns:3.1vw 1fr;gap:.48vw;align-items:center;padding:.28vh .42vw;background:var(--brand-grey)}
  .alert-card dt{font-size:.84vw;color:#53606f}
  .alert-card dd{font-size:.86vw;line-height:1.2;min-width:0}
  .alert-card > small{font-size:.68vw;line-height:1.25;color:#5a6573}
  .opensre-case-slide code{
    font:inherit;font-family:var(--mono);font-size:.92em;background:#eef1f5;padding:.08em .2em;
    overflow-wrap:anywhere;
  }
  .tool-plan-card{padding:.75vh .85vw;background:var(--brand-black);color:#fff}
  .tool-plan-card > div{display:flex;align-items:center;gap:.55vw;margin-bottom:.45vh}
  .tool-plan-card > div span{font:800 .53vw var(--mono);color:#fff}
  .tool-plan-card > div strong{font-size:.9vw}
  .tool-plan-card p{font-size:.8vw;line-height:1.35;color:rgba(255,255,255,.9)}
  .tool-plan-card small{display:block;margin-top:.42vh;font-size:.66vw;line-height:1.25;color:rgba(255,255,255,.7)}
  .evidence-path{
    display:grid;grid-template-columns:1fr;grid-template-rows:repeat(4,minmax(0,1fr));
    gap:.52vh;min-height:0;
  }
  .evidence-path article{
    min-height:0;padding:.82vh .78vw;background:#fff;border:1px solid var(--line);
    display:grid;grid-template-columns:1.7vw 1fr;gap:.62vw;align-items:start;
  }
  .evidence-path article:nth-child(3){border-left:4px solid var(--brand-red)}
  .evidence-no{
    width:2vw;height:2vw;border-radius:50%;display:grid;place-items:center;
    background:var(--brand-red);color:#fff;font:800 .72vw var(--mono);
  }
  .evidence-path strong{font-size:1vw;line-height:1.24}
  .evidence-path p{margin-top:.35vh;font-size:.9vw;line-height:1.35;color:#46515f}
  .evidence-path p b{color:var(--ink)}
  .case-conclusion{
    display:grid;grid-template-columns:1fr;grid-template-rows:auto auto auto;gap:.5vh;
    padding:.8vh .95vw;background:#fff;border-left:5px solid var(--brand-red);box-shadow:var(--shadow-soft);
  }
  .case-conclusion > div:first-child{display:grid;grid-template-columns:4.5vw 1fr;gap:.65vw;align-items:start}
  .case-conclusion > div:first-child span{font:800 .86vw var(--mono);color:var(--brand-red);padding-top:.15vh}
  .case-conclusion > div:first-child strong{font-size:1vw;line-height:1.34}
  .cause-chain{
    display:grid;grid-template-columns:1fr auto 1fr auto 1fr auto 1fr auto 1fr;gap:.32vw;align-items:center;
  }
  .cause-chain span{
    padding:.5vh .42vw;background:var(--brand-grey);font-size:1vw;font-weight:800;text-align:center;white-space:nowrap;
  }
  .cause-chain .lucide{width:.9vw;color:var(--brand-red)}
  .case-conclusion > p{font-size:.9vw;color:#3f4a58}
  .case-boundary{
    display:grid;grid-template-columns:6vw 1fr;gap:.8vw;align-items:stretch;
    padding:.58vh .72vw;background:var(--brand-grey);color:#34404d;
  }
  .case-boundary > strong{
    display:grid;place-items:center;background:var(--brand-black);color:#fff;
    font-size:.86vw;letter-spacing:.04em;
  }
  .boundary-lines{display:grid;grid-template-rows:auto auto;gap:.3vh}
  .boundary-lines > div{
    display:grid;grid-template-columns:4.4vw 1fr auto;gap:.55vw;align-items:center;
    font-size:.9vw;line-height:1.22;
  }
  .boundary-lines b{color:var(--brand-red);font-size:.8vw}
  .boundary-lines em{
    padding:.35vh .45vw;background:var(--brand-red);color:#fff;font:800 .78vw var(--mono);
    font-style:normal;white-space:nowrap;
  }

  /* OpenSRE 4-page update: Cloud Tech red / black / grey only */
  .opensre-overview{
    grid-template-rows:auto auto auto minmax(0,1fr) auto;
    gap:.65vh;padding-top:2.8vh;padding-bottom:2.8vh;
  }
  .opensre-overview .h-xl{font-size:min(2.75vw,4.95vh);white-space:nowrap}
  .opensre-overview-bars{display:grid;grid-template-rows:auto auto;gap:.65vh}
  .opensre-composition{
    min-height:4.7vh;padding:.55vh .85vw;display:grid;
    grid-template-columns:auto auto .85vw auto .85vw auto .85vw auto;
    align-items:center;gap:.58vw;background:var(--brand-black);color:#fff;
  }
  .opensre-composition b{font-size:.74vw;color:#fff;margin-right:.25vw}
  .opensre-composition span{
    min-height:3.2vh;padding:.35vh .72vw;display:grid;place-items:center;
    background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.18);
    font-size:.81vw;font-weight:750;white-space:nowrap;
  }
  .opensre-composition .lucide{width:.8vw;height:.8vw;color:#fff}
  .opensre-feature-grid .card{padding:1.65vh 1.35vw}
  .opensre-feature-grid .card:nth-child(3){border-top-color:var(--line-strong)}
  .opensre-feature-grid .feature-index{font-size:2.55vw;margin-bottom:.85vh}
  .opensre-feature-grid .card-title{font-size:1.23vw;margin-bottom:.65vh}
  .opensre-feature-grid .card-body{font-size:.96vw;line-height:1.42}
  .feature-tags{margin-top:1.15vh}
  .opensre-boundary{min-height:5vh;font-size:.83vw}
  .opensre-boundary strong{font-size:.84vw}

  .opensre-process-slide{
    grid-template-rows:auto auto auto minmax(0,1fr) auto;
    gap:.7vh;padding-top:2.45vh;padding-bottom:2.8vh;
  }
  .opensre-process-slide .h-xl{font-size:min(2.55vw,4.6vh)}
  .opensre-flow-step{min-height:16.5vh;padding:.9vh .68vw .7vh}
  .opensre-flow-step strong{font-size:1vw}
  .opensre-flow-step p{font-size:.87vw;line-height:1.28}
  .opensre-flow-step small{font-size:.78vw}
  .stage-relation{min-height:4.35vh;font-size:.84vw}
  .opensre-stage-grid{display:grid;grid-template-columns:1fr 1fr;gap:.9vw;min-height:0}
  .opensre-stage-card{
    min-height:0;padding:1.05vh 1vw;background:#fff;border:1px solid var(--line);
    border-top:4px solid var(--line-strong);box-shadow:var(--shadow-soft);
    display:grid;grid-template-rows:auto minmax(0,1fr);gap:.68vh;
  }
  .opensre-stage-card.selected{border-top-color:var(--brand-black)}
  .opensre-stage-card.active{border-top-color:var(--brand-red)}
  .stage-card-head{display:grid;grid-template-columns:2vw 1fr;gap:.7vw;align-items:center}
  .stage-card-head > span{
    width:1.9vw;height:1.9vw;display:grid;place-items:center;
    background:var(--brand-grey);color:var(--brand-red);font:800 .68vw var(--mono);
  }
  .opensre-stage-card.active .stage-card-head > span{background:var(--brand-red);color:#fff}
  .stage-card-head div{display:flex;justify-content:space-between;align-items:baseline;gap:.8vw}
  .stage-card-head strong{font-size:1.02vw}
  .stage-card-head small{font:.66vw var(--mono);color:var(--muted);white-space:nowrap}
  .opensre-stage-card ul{
    list-style:none;display:grid;grid-template-rows:repeat(4,minmax(0,1fr));gap:.4vh;min-height:0;
  }
  .opensre-stage-card li{
    position:relative;padding:.52vh .62vw .52vh 1.35vw;background:var(--brand-grey);
    font-size:.84vw;line-height:1.28;color:#3d4855;
  }
  .opensre-stage-card li::before{
    content:"";position:absolute;left:.58vw;top:.96vh;width:.38vw;height:.38vw;
    background:var(--brand-red);border-radius:50%;
  }
  .opensre-stage-card code,.outer-pipeline-note code{font:inherit;font-family:var(--mono);font-size:.92em}
  .outer-pipeline-note{
    min-height:5vh;display:grid;grid-template-columns:1fr 1.35fr;gap:1vw;align-items:center;
    padding:.55vh .9vw;background:var(--brand-grey);border-left:4px solid var(--brand-red);
    font-size:.83vw;line-height:1.3;color:#46515f;
  }
  .outer-pipeline-note b{color:var(--ink)}

  .opensre-agent-slide{
    height:100%;display:grid;
    grid-template-rows:auto 4.7vh 4.65vh minmax(0,1fr) 5.5vh;
    gap:.72vh;padding-top:2.35vh;padding-bottom:2.8vh;
  }
  .opensre-agent-slide header{display:grid;gap:.45vh}
  .opensre-agent-slide .h-xl{font-size:min(2.48vw,4.45vh);white-space:nowrap}
  .agent-equation{
    min-height:4.7vh;padding:.48vh .85vw;display:grid;
    grid-template-columns:auto auto repeat(6,auto auto);align-items:center;justify-content:start;gap:.52vw;
    background:#fff;border-left:5px solid var(--brand-red);box-shadow:var(--shadow-soft);
  }
  .agent-equation strong{font:800 .9vw var(--mono);color:var(--brand-red)}
  .agent-equation > small{font:700 .68vw var(--mono);color:#667180;white-space:nowrap}
  .agent-equation > span{font:800 1vw var(--mono);color:var(--ink)}
  .agent-equation b{padding:.36vh .5vw;background:var(--brand-grey);font-size:.84vw;white-space:nowrap}
  .agent-equation .lucide{width:.72vw;height:.72vw;color:var(--brand-red)}
  .agent-method-strip{
    min-height:4.65vh;padding:.45vh .75vw;display:flex;align-items:center;gap:.52vw;
    background:var(--brand-black);color:#fff;
  }
  .agent-method-strip strong{font-size:.78vw;margin-right:.25vw}
  .agent-method-strip span{
    min-width:5vw;padding:.45vh .6vw;text-align:center;background:rgba(255,255,255,.12);
    border:1px solid rgba(255,255,255,.2);font-size:.9vw;font-weight:750;
  }
  .agent-method-strip .lucide{width:.75vw;height:.75vw}
  .agent-method-strip small{
    margin-left:auto;max-width:52vw;font-size:.76vw;line-height:1.18;
    color:rgba(255,255,255,.8);text-align:right;white-space:nowrap;
  }
  .agent-workbench{display:grid;grid-template-columns:2.45fr .95fr;gap:.9vw;min-height:0}
  .agent-main-flow{
    min-height:0;display:grid;grid-template-rows:10.5vh 2.7vh minmax(0,1fr) 4.3vh;gap:.55vh;
  }
  .agent-start-grid{display:grid;grid-template-columns:1fr 1fr;gap:.75vw;min-height:0}
  .agent-step{
    min-height:0;padding:.75vh .75vw;background:#fff;border:1px solid var(--line);
    border-left:4px solid var(--brand-black);display:grid;grid-template-columns:1.65vw 1fr;gap:.55vw;align-items:start;
  }
  .agent-step:nth-child(2){border-left-color:var(--brand-red)}
  .agent-step em,.agent-loop-step em{
    width:1.55vw;height:1.55vw;display:grid;place-items:center;background:var(--brand-grey);
    color:var(--brand-red);font:800 .58vw var(--mono);font-style:normal;
  }
  .agent-step strong{font-size:1vw}.agent-step p{margin-top:.28vh;font-size:.94vw;line-height:1.25;color:#46515f}
  .agent-loop-label{display:flex;align-items:center;gap:.65vw}
  .agent-loop-label::before,.agent-loop-label::after{content:"";height:1px;background:var(--line-strong);flex:1}
  .agent-loop-label span{font:800 .8vw var(--mono);color:var(--brand-red);white-space:nowrap}
  .agent-loop-label small{font-size:.78vw;color:var(--muted);white-space:nowrap}
  .agent-loop-row{
    min-height:0;display:grid;grid-template-columns:1fr .6vw 1fr .6vw 1fr .6vw 1fr;
    gap:.35vw;align-items:stretch;
  }
  .agent-loop-row > .lucide{width:.72vw;height:.72vw;align-self:center;color:var(--brand-red)}
  .agent-loop-step{
    min-height:0;padding:.72vh .62vw;background:#fff;border:1px solid var(--line);
    border-top:4px solid var(--line-strong);display:flex;flex-direction:column;justify-content:center;gap:.4vh;
  }
  .agent-loop-step.model{border-top-color:var(--brand-red);background:#fff7f8}
  .agent-loop-step.end{border-top-color:var(--brand-black)}
  .agent-loop-step strong{font-size:.98vw;line-height:1.2}
  .agent-loop-step p{font-size:.94vw;line-height:1.26;color:#475260}
  .agent-loop-step code{font:inherit;font-family:var(--mono);font-size:.92em}
  .agent-feedback{
    min-height:4.1vh;padding:.45vh .68vw;display:grid;grid-template-columns:1fr auto 1fr;
    align-items:center;gap:.7vw;background:var(--brand-grey);font-size:.84vw;color:#46515f;
  }
  .agent-feedback span:last-child{text-align:right}.agent-feedback b{color:var(--ink)}
  .agent-feedback .lucide{width:.85vw;height:.85vw;color:var(--brand-red)}
  .agent-controls{display:grid;grid-template-rows:1fr 1fr;gap:.65vh;min-height:0}
  .agent-control-card{
    min-height:0;padding:.78vh .75vw;background:#fff;border:1px solid var(--line);
    border-top:4px solid var(--brand-black);display:flex;flex-direction:column;gap:.47vh;
  }
  .agent-control-card.cache-card{border-top-color:var(--brand-red)}
  .control-head{display:grid;grid-template-columns:1.55vw 1fr;gap:.5vw;align-items:center}
  .control-head span{
    width:1.45vw;height:1.45vw;display:grid;place-items:center;background:var(--brand-black);
    color:#fff;font:800 .55vw var(--mono);
  }
  .cache-card .control-head span{background:var(--brand-red)}
  .control-head strong{font-size:.98vw}
  .agent-control-card > p{font-size:.94vw;line-height:1.23;color:#44505e}
  .agent-control-card ol{padding-left:1.05vw;display:grid;gap:.22vh}
  .agent-control-card li{font-size:.84vw;line-height:1.2;color:#4a5563}
  .agent-control-card > small{margin-top:auto;font-size:.83vw;line-height:1.2;color:var(--muted)}
  .cache-metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:.3vw}
  .cache-metrics span{
    padding:.38vh .25vw;background:var(--brand-grey);text-align:center;
    font-size:.76vw;line-height:1.12;color:#505b69;
  }
  .cache-metrics b{display:block;font:.84vw var(--mono);color:var(--brand-red)}
  .agent-boundary-strip{
    min-height:5.5vh;display:grid;grid-template-columns:5.2vw repeat(4,1fr);align-items:stretch;
    background:var(--brand-black);color:#fff;
  }
  .agent-boundary-strip strong{display:grid;place-items:center;background:var(--brand-red);font-size:.72vw}
  .agent-boundary-strip > span{
    padding:.5vh .65vw;display:grid;place-items:center;border-right:1px solid rgba(255,255,255,.18);
    font-size:.83vw;line-height:1.2;text-align:center;color:rgba(255,255,255,.92);
  }
  .agent-boundary-copy{display:inline}
  .agent-boundary-strip code{font:inherit;font-family:var(--mono);font-size:.92em;color:#fff}

  .opensre-case-slide{
    grid-template-rows:auto auto auto minmax(0,1fr) auto auto auto;
    gap:.48vh;padding-top:2.35vh;padding-bottom:2.8vh;
  }
  .opensre-case-slide .h-xl{font-size:min(2.45vw,4.4vh)}
  .teaching-notice{padding:.42vh .72vw;font-size:.84vw}
  .case-stage-rail > span{min-height:3.25vh;font-size:.83vw}
  .case-main-grid{grid-template-columns:.87fr 2.13fr;gap:.72vw}
  .alert-card{padding:.72vh .78vw;gap:.35vh}
  .alert-card dl{gap:.12vh}
  .alert-card dl > div{padding:.2vh .36vw}
  .alert-card-head span{font-size:.7vw}.alert-card-head strong{font-size:.94vw}
  .alert-card dt{font-size:.9vw}.alert-card dd{font-size:.94vw}
  .alert-card > small{font-size:.83vw;line-height:1.18}
  .tool-plan-card{padding:.58vh .72vw}
  .tool-plan-card > div{margin-bottom:.3vh}.tool-plan-card > div span{font-size:.7vw}.tool-plan-card > div strong{font-size:.94vw}
  .tool-plan-card p{font-size:.88vw}.tool-plan-card small{font-size:.83vw}
  .evidence-path{grid-template-rows:repeat(5,minmax(0,1fr));gap:.35vh}
  .evidence-path article{padding:.55vh .68vw;grid-template-columns:1.55vw 1fr;gap:.55vw}
  .evidence-no{width:1.65vw;height:1.65vw;font-size:.58vw}
  .evidence-path strong{font-size:.98vw}.evidence-path p{margin-top:.18vh;font-size:.94vw;line-height:1.2}
  .evidence-path article:nth-child(3){border-left:1px solid var(--line)}
  .evidence-path article:nth-child(4){border-left:4px solid var(--brand-red)}
  .case-conclusion{padding:.52vh .78vw;gap:.28vh}
  .case-conclusion > div:first-child{grid-template-columns:5vw 1fr;gap:.55vw}
  .case-conclusion > div:first-child span{font-size:.84vw}
  .case-conclusion > div:first-child strong{font-size:.94vw;line-height:1.22}
  .cause-chain span{padding:.34vh .32vw;font-size:.84vw}
  .cause-chain .lucide{width:.72vw}
  .case-conclusion > p{font-size:.84vw}
  .case-outer-finish{
    min-height:3.8vh;padding:.4vh .72vw;display:grid;
    grid-template-columns:auto auto .8vw auto .8vw auto;gap:.5vw;align-items:center;
    background:var(--brand-black);color:#fff;font-size:.84vw;
  }
  .case-outer-finish strong{color:#fff;margin-right:.3vw}
  .case-outer-finish span{padding:.3vh .5vw;background:rgba(255,255,255,.1);text-align:center}
  .case-outer-finish .lucide{width:.72vw;height:.72vw;color:#fff}
  .opensre-case-slide .case-outer-finish code{font:inherit;font-family:var(--mono);font-size:1em;font-weight:700;color:#fff;background:transparent;padding:0}
  .case-boundary{grid-template-columns:5.4vw 1fr;padding:.42vh .55vw;gap:.55vw}
  .case-boundary > strong{font-size:.84vw}
  .case-boundary-tags{display:grid;grid-template-columns:repeat(4,1fr);gap:.35vw}
  .case-boundary-tags span{
    min-height:3.2vh;padding:.32vh .42vw;display:grid;place-items:center;
    background:#fff;border:1px solid var(--line);font-size:.84vw;line-height:1.16;text-align:center;
  }
  #hint{bottom:3.6vh}
  .page-no{bottom:3.6vh}
  #nav{bottom:3.6vh}
`;

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
  );
html = html.replace(
  "<title>[必填] 替换为 PPT 标题 · Huawei Corporate Deck</title>",
  "<title>问题定位框架开源方案洞察 · Huawei Corporate Deck</title>",
);

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
for (const [from, to] of themeReplacements) html = html.replace(from, to);

html = html.replace("</style>", `${customCss}\n</style>`);
html = html.replace("<!-- SLIDES_HERE -->", slides.join("\n\n"));
fs.writeFileSync(outputPath, html, "utf8");
console.log(`Wrote ${slides.length} slides to ${outputPath}`);
