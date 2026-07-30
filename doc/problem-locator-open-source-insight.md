# 问题定位框架：开源 AI-SRE 与 Agent 上下文机制洞察

> 技术诊断会评审工作稿  
> 调研日期：2026-07-28  
> 状态：业界洞察已形成；当前框架尚未定稿，因此“与当前设计的对比”只给出预比较和待补维度，不把讨论中的方案写成既定结论。

## 0. 先读结论

1. **Tracer-Cloud OpenSRE 最值得借鉴的是“证据驱动、受预算约束、可评测”的调查闭环。** 它把告警解析、工具规划、ReAct（推理与行动）调查、结构化 RCA（根因分析）、交付和反馈串成一条链，并为循环次数、工具数量、重复调用和停滞终止设置明确边界；但当前默认主链是单一调查 Agent，不是完整的多 Agent Case 编排框架。
2. **在本次纳入调研的阿里生态候选中，`derisk-ai/OpenDerisk` 与 OpenSRE 定位最接近，并具有蚂蚁集团作者与生产实践背景。** 它强调多 Agent 协同、知识引擎、上下文压缩、证据链可视化，以及人工介入与审核。本文按“阿里生态”研究口径纳入；这不等于阿里云、阿里巴巴或蚂蚁集团官方仓库或官方发布。SREWorks、CodeFuse-muAgent 和阿里云可观测 MCP Server 分别更接近运维平台、通用多 Agent 框架和工具接入层，不是同类完整 AI-SRE 诊断系统。
3. **仓内 8 个 Agent 参考对象解决的是不同切面，没有一个能直接替代问题定位框架。** LangGraph 擅长结构化状态与状态检查点；OpenHands 擅长只追加事件日志与可审计的上下文压缩；SWE-agent 擅长在不改原始执行轨迹的前提下筛选本轮模型输入；Cline 擅长单任务边界和显式交接；AutoGen、CrewAI、Aider 和 mini-SWE-agent 分别提供模型输入策略、Flow（流程）状态、由 Git 和文件保存的工程状态，以及极简基线。
4. **候选方向不是“选一个框架照搬”，而是组合机制；以下仍待评审。** 仓内调研建议评审“结构化 Case/DiagnosisState（诊断状态）+ JobContextManifest（执行输入清单）+ 受 Token 预算约束的单次模型输入包 + 可由固定输入重建的 Agent 会话”。当前正式设计仍采用 OPT-015 的同 Agent 跨 Job 会话，是否替换须单独决策。Context Compiler（上下文编译器）、ContextPack（单次模型输入包）和 Outcome Gate（结果写入规则）是本报告候选命名，不是现有能力。
5. **成熟度必须保守表述。** OpenSRE 仍是 Public Alpha（公开早期测试），README 明确没有公开基准评测结果；OpenDerisk 的大规模生产数字和评测提升主要来自论文作者自述，专有 AntRCA 数据集与人工评分限制了独立复现。GitHub Star 数、集成数量和厂商宣传都不能等价为诊断准确率。

## 1. 范围、口径与证据分级

### 1.1 本次研究对象

| 类别 | 对象 | 本次口径 |
|---|---|---|
| AI-SRE 诊断框架 | Tracer-Cloud/opensre | 本文所称 OpenSRE；对应 opensre.com |
| 阿里生态同类方案 | OpenDerisk（`derisk-ai` 社区仓库；蚂蚁集团作者与生产实践背景） | 在本次纳入调研的阿里生态候选中与 OpenSRE 定位最接近；不等同于集团官方仓库或官方发布 |
| 仓内 Agent 参考对象 | LangGraph、OpenHands、Cline、AutoGen、CrewAI、Aider、SWE-agent、mini-SWE-agent | 以仓内调研列出的 8 个项目为全集 |
| 当前设计 | `design/` 下问题定位框架文档，以及 `doc/high-star-agent-context-strategy-survey.md` | 正式设计与调研建议严格分开 |

### 1.2 证据分级

| 标记 | 含义 | 在评审中的使用方式 |
|---|---|---|
| **F：可核事实** | 代码、配置、许可证、接口契约，或官方文档对可见结构/当前默认行为的明确说明；不含能力效果与生产规模宣传 | 可作为实现与行为事实 |
| **S：项目方陈述** | 论文、README 或官网对能力、效果、案例和生产规模的陈述，尚未独立复现 | 必须注明“作者报告/官方宣称” |
| **I：本次推断** | 由多项事实归纳出的设计判断 | 必须与事实分开 |
| **T：待定事项** | 当前问题定位框架尚未决策或仍在讨论 | 不得写成已落地能力 |

### 1.3 不用 Stars 代替成熟度

社区热度只反映关注度。成熟度至少还要看：版本稳定性、公开评测、故障恢复、权限边界、状态能否重放、证据能否审计、生产运维成本和独立复现情况。本报告只在身份识别时给出截至调研日的近似社区数据。

## 2. OpenSRE：诊断 Agent 与评测闭环

### 2.1 身份判定与同名歧义

本文研究的是 [Tracer-Cloud/opensre](https://github.com/Tracer-Cloud/opensre)，官网为 [opensre.com](https://www.opensre.com/)。

- **F**：Apache-2.0，当前标记 Public Alpha / v0.1。
- **F**：截至 2026-07-28，GitHub 页面约 9.3k stars、1.3k forks、2,951 commits；最新版本约为 `0.1.2026.7.27`。数据变化快，只用于说明项目活跃度。
- **F**：官方定位为开源 AI-SRE Agent 框架及训练、评测环境，并称可连接 60 余种工具与服务；该计数跨大模型、可观测、基础设施、数据、事件管理等类别，不同集成的能力深度并不一致。
- **风险**：另有 [swapnildahiphale/OpenSRE](https://github.com/swapnildahiphale/OpenSRE)，特征是 LangGraph、Neo4j 拓扑和并行子 Agent。它与 Tracer-Cloud 版是两个项目，不能混写。
- **纠偏**：Tracer 当前主线已经移除旧 graph/chain 层，依赖中没有 LangGraph；旧缓存页面中的“LangGraph deployment”不代表当前架构。

### 2.2 准确定位

OpenSRE 的核心任务可以概括为：

> 将告警或自然语言故障描述，与日志、指标、Trace、部署和配置等多源信号关联，经过受预算、轮次和停止条件约束的工具调查，输出带证据的结构化根因分析，并把人工反馈沉淀为可复现的回归用例。

它不是单纯的“运维问答机器人”，也不是问题定位业务系统的全部控制面。它更接近：

1. 可连接多类观测与基础设施系统的调查 Agent 运行时；
2. 约束工具循环的诊断流水线；
3. 结构化 RCA 报告器；
4. 将生产误判转成可复现合成场景的评测环境。

### 2.3 模块架构

- **F**：官方将七个一方 package 划分为四层，并以 CI 强制 import boundary。
- **I**：从仓库与部署形态看，本报告将其归纳为有依赖边界的 Python 模块化单体：

| 层 | 主要职责 | 对问题定位框架的含义 |
|---|---|---|
| `surfaces` / `gateway` | CLI、REPL、Slack、Telegram、HTTP 等入口 | 渠道与诊断核心解耦 |
| `tools` / `integrations` | Agent 工具与外部系统客户端 | 工具协议与具体厂商适配分离 |
| `core` / `platform` | Agent 运行时、状态、大模型、上下文预算、脱敏、执行约束、沙箱、观测 | 调查机制集中在共享运行时 |
| `config` | 提示词、常量与主题 | 策略可配置但需版本化 |

**F**：依赖方向由 CI 规则约束；明确的例外是 `core ↔ platform` 的双向关系。  
**I**：最可迁移的不是目录名，而是入口、工具、运行时和配置边界均可独立演进且能被自动校验。

### 2.4 六阶段调查流水线

官方调查主链可归纳为：

```text
原始告警（raw_alert）
  → 解析可用集成（resolve_integrations）
  → 提取问题（extract_alert）/ 无效输入提前结束
  → 规划候选工具（plan_actions）
  → 调查 Agent 循环（ConnectedInvestigationAgent）
  → 生成结构化结论（diagnose）
  → 交付结果（deliver）
```

| 阶段 | 关键动作 | 产物或约束 |
|---|---|---|
| 解析可用集成（`resolve_integrations`） | 根据配置确定可用数据源 | 缩小候选工具范围 |
| 提取问题（`extract_alert`） | 识别问题、时间窗和实体；无效输入可提前结束 | 形成规范化调查输入，避免无效调查 |
| 规划候选工具（`plan_actions`） | 对候选工具与关键词打分 | 默认保留前 10 个候选 |
| ReAct loop | 假设—取证—更新判断 | EvidenceEntry 与工具轨迹 |
| 生成结构化结论（`diagnose`） | 将调查自由文本解析为 RCA DTO | 结构化根因、诊断判断、因果链和建议 |
| 交付结果（`deliver`） | 发布诊断结果 | Slack、GitLab、`report.md` 等；可选修复是产品级能力，不应视为该阶段的默认产物 |

`raw_alert` 是上述六阶段的输入，不是一个独立阶段。

**I**：先约束问题边界和候选工具范围，再允许开放式推理，比“一开始把全部工具交给模型”更可控。

### 2.5 有预算和停止条件的 ReAct 调查

OpenSRE 当前默认 RCA 路径是单个 `ConnectedInvestigationAgent` 的工具循环，不是 Router/Specialist Agent Team（路由器/专家智能体团队）。

关键约束：

- **F**：默认工具规划保留 Top 10。
- **F**：单轮最多向模型提供 32 个工具定义（Schema）。
- **F**：最多 20 次调查循环。
- **F**：部分高确定性工具可在首次调用大模型前执行，预先取得证据。
- **F**：同一工具与参数不会重复执行，复用缓存结果。
- **F**：当连续 2 个迭代中的所有工具调用都只是重复调用（没有新的工具调用）时，下一轮移除工具访问并要求模型给出纯文本结论。
- **F**：LLM 调用失败时保留已收集证据，并可降级输出部分结果。
- **F**：最终自由文本再经一次结构化 LLM 调用转成 RCA DTO。

这些机制分别约束成本失控、重复调用、连续多轮只产生重复调用，以及单次大模型调用失败导致已取证据丢失。

### 2.6 状态、证据、会话与上下文

OpenSRE 值得关注的分离关系是两条相互独立的链：

```text
调查链（一次 RCA）：
调查状态（AgentState）+ 证据条目（EvidenceEntry）
        ↓ context_budget：选择 / 去重 / 淘汰 / 截断
本轮模型输入 → 模型调用 / 工具循环

交互链（REPL，独立机制）：
会话文件（对话、工具轨迹、基础设施上下文）
        ↓ /compact 或自动 compaction
/resume 恢复交互上下文
```

- **F**：`AgentState` 在阶段间传递结构化调查状态。
- **F**：`EvidenceEntry` 保存证据来源与采集信息。
- **F**：REPL 会话保存对话、工具轨迹和基础设施上下文，支持恢复和 `/compact`。
- **F**：上下文预算会为响应预留 token，保留预置消息（seed message），优先删除重复工具结果，再淘汰低价值工具交换，最后截断最大消息。
- **I（证据边界）**：截至调研日，公开资料未说明中断中的六阶段调查状态能否保存持久检查点，并在进程重启后继续；REPL 会话恢复不能替代 Case/Job 恢复。
- **I**：上下文裁剪只改变本轮模型输入，不能被当成正式状态迁移；会话摘要也不应成为权威证据仓。

### 2.7 集成、部署与安全边界

**生态与部署**

- **F**：Python 3.12+、Pydantic、FastAPI/Uvicorn。
- **F**：支持 OpenAI、Anthropic、LiteLLM 等模型通道，也列出 Codex、Ollama、Gemini、OpenRouter、Bedrock 等接入方式。
- **F**：集成覆盖 Observability（可观测性）、云、数据库、数据平台、代码仓、事件管理和通信。
- **F**：部署方式包括本地、Docker/ECR、AMI + systemd Gateway，以及多种 ASGI 托管环境；持久化可配 PostgreSQL/Redis。

**安全评审必须关注**

- **F**：支持自托管、可逆标识符掩码和规则型不可逆脱敏。
- **F**：脱敏功能默认关闭。
- **F**：PostHog、Sentry 遥测默认开启，可手动关闭。
- **F**：REPL 历史默认可读文件持久化；规则脱敏不保证覆盖自然语言中的秘密。
- **F**：部分部署示例的 API ingress 默认较宽，生产必须收紧。
- **I**：执行修复操作必须经过独立审批，并具备最小权限和完整审计边界。

### 2.8 评测闭环比宣传数字更有价值

OpenSRE 的 synthetic RCA（合成根因分析）套件可检查：

- 根因类别、关键词和必需证据源；
- 禁止出现的错误结论；
- 轨迹与工具效率；
- 无故障对照场景、误导性线索、关键指标缺失和复合故障。

另一个 closed-loop learning（闭环学习）工作流负责：

- 调查后的人工评分与诊断失败分类，如检索、推理、工具、路由或提示词问题；
- 将误判和漏判导出为基准评测场景，再提交为可复现回归用例。

它也提供 CloudOpsBench 运行入口，但必须谨慎解释：

- **F**：README 明确没有公开完整基准评测结果。
- **F**：CloudOpsBench 页面上的部分数字是目标值，不是已实现成绩。
- **F**：静态合成套件公开承认对增量时序取证、复合故障、单根因 schema 和部分 required-query 约束的覆盖仍有限。
- **I**：OpenSRE 的长期价值更可能来自“诊断—人工评分—回归—再评测”闭环，而不是某次演示中的自动 RCA。

### 2.9 对问题定位框架的启示与边界

**可借鉴**

1. 记录证据来源与采集信息，并显式区分已有证据支持和尚待验证的诊断判断；
2. 工具规划、数量上限、循环预算、调用去重、停滞终止与降级输出；
3. 权威调查状态、原始证据与本轮模型输入分离；
4. 误导性线索、缺失证据、复合故障和调查效率评测；
5. 生产误判回流为回归案例。

**不直接照搬**

1. 用单 Agent + 共享状态替代 Case/Job/Coordinator（案例/任务/协调器）业务控制面；
2. 用会话文件或摘要承担权威状态；
3. 把“并行假设”宣传等价成有合并语义的多 Agent；
4. 未经独立审批执行修复；
5. 把活跃度、集成数或目标指标当作诊断准确率。

## 3. OpenDerisk：多 Agent 风险诊断与证据链

### 3.1 为什么选择 OpenDerisk

候选对比：

| 候选 | 主定位 | 与 OpenSRE 的相似度 | 结论 |
|---|---|---:|---|
| **OpenDerisk** | AI-native 风险智能、DeepResearch RCA、多 Agent、知识与证据链 | 高 | 本次主对象 |
| SREWorks | 面向企业运维的数据化 AIOps/DevOps 平台 | 中低 | 平台层参考，不是同类 Agent 框架 |
| RCAgent | 工具增强 LLM 自主 RCA，论文接入阿里云 Flink | 高 | 截至 2026-07-28，本次在论文页及 `alibaba`、`aliyun` 官方 GitHub 组织中未检索到论文指向的官方开源实现；只作论文参考 |
| SysOM AI | Linux 系统诊断 MCP 工具与 Skills | 中 | 工具层，不含完整编排、记忆和报告框架 |
| UModel | 对象图、拓扑、MCP 与 RCA Skills | 中 | 诊断语义/上下文层，不是完整 Agent Runtime |
| CodeFuse-muAgent | 从 OpsGPT 演进的通用多 Agent 框架 | 中 | Agent 基座参考，不是完整 AI-SRE 产品 |
| AgentScope | 通用 Agent 团队、权限、沙箱与记忆框架 | 中低 | 可作为基座，但没有开箱即用的 SRE 调查模型 |
| Spring AI Alibaba | 通用 Java Agent/Workflow/Multi-Agent 框架 | 中低 | 阿里官方 Agent 基座，包含上下文工程、人工介入与审核能力，但没有开箱即用的 AI-SRE RCA 产品模型 |
| 阿里云 Observability MCP Server | 向 Agent 暴露日志、指标等能力的 MCP 工具层 | 中低 | 数据/工具接入层，不含完整诊断控制面 |

归属口径：

- **F**：OpenDerisk 代码位于独立 `derisk-ai/OpenDerisk` 社区仓库。
- **F**：论文作者、邮箱和生产部署描述均指向 Ant Group（蚂蚁集团）。
- **F**：公开仓库采用 MIT 许可证。
- **结论**：应写“OpenDerisk（蚂蚁集团作者与生产实践背景，按阿里生态纳入）”，不能写成“阿里云 OpenDerisk”或“蚂蚁集团官方开源”。

### 3.2 产品定位与开放范围

OpenDerisk 将自身定位为 AI-native Risk Intelligence（AI 原生风险智能）系统，面向日志、Trace、代码等数据开展 DeepResearch RCA（深度研究式根因分析），并可视化展示证据链和多 Agent 协作过程。

官方列出的角色包括：

- SRE Agent：面向故障调查；
- Code Agent：按需编写分析代码；
- Data Agent：数据查询与统计；
- Vis Agent：可视化；
- Report Agent：报告生成。

必须区分两个范围：

- **S**：README 宣称并展示 Microsoft OpenRCA 数据集运行路径、Code Agent 动态分析和 Web UI；本次未完成端到端运行复现。
- **S**：论文描述的是蚂蚁集团内部更广的生产平台、知识体系、专家生态和规模化使用。
- **I**：开源仓库能证明“公开实现的起点”，不能自动证明论文中全部生产能力完整开源。

### 3.3 三层运行架构

**S（论文架构/设计）**：论文可归纳为：

```text
Perception（感知与接入）
        ↓
DeRisk Core（规划、调度、推理、知识、上下文、MCP）
        ↓
Analysis & Reporting（分析、证据链、报告、人工介入与审核）
```

四个核心支柱：

1. Adaptive Multi-Agent System（自适应多 Agent 系统）；
2. Pluggable Reasoning Engine（可插拔推理引擎）；
3. Knowledge Engine（知识引擎）；
4. MCP（模型上下文协议）接入。

中央 Orchestrator（编排器）负责 Agent 生命周期、消息和任务，异步消息总线负责协作。  
**I**：它比 OpenSRE 更接近“专家团队 + 平台中枢”，但也更依赖共享平台、知识和治理能力。

### 3.4 Agent 协作模式

**S（论文架构与案例）**：论文列出三种协作范式，但未给出完整形式化语义：

| 模式 | 论文可确认的含义 | 场景口径 |
|---|---|---|
| Single-Agent | 单 Agent 执行 | 图 3 列出的基础范式，边界未详述 |
| TeamMode | 团队模式；案例让各 Agent 在相互隔离的上下文中独立分析 | 隔离评估；专家分工属本文研判 |
| GroupMode | 案例中评审 Agent 读取各专家输出后汇总 | 多结论对比汇聚，属本文研判 |

论文还给出隔离式交叉判断：分析 Agent 在相互隔离的上下文中独立工作，评审 Agent 再读取多个结论进行比较和汇总。

**可借鉴**

- 专家角色按能力而非渠道划分；
- 独立分析与汇总审核分离，降低从众；
- 生命周期、消息、任务由中央编排器治理。

**风险**

- 角色越多，时延、Token、故障面和状态合并复杂度越高；
- “消息互通”不等于“事实合并”；
- 没有明确的“诊断判断—证据”状态机时，多 Agent 可能只是放大意见数量。

### 3.5 三类推理路径

| 推理模式 | 特点 | 工程权衡 |
|---|---|---|
| Dynamic ReAct（动态推理与行动） | Agent 自主选择下一步 | 灵活，但时延和可重复性较弱 |
| Deterministic SOP（确定性标准作业程序） | 预定义阶段和控制条件 | 可审计、可复现，但适应性较弱 |
| RL Dynamic（论文中的概念/路线图） | 论文架构列出该模式，但同时说明 Agentic RL 当前不适合生产部署；完整系统级 RL 属于 V4 规划 | 按未公开验证的目标能力处理，不作为当前开源版已验证能力 |

**I**：问题定位框架更适合采用“确定性控制面 + Job 内受控 Agent 自主性”的混合方式，而不是在业务状态机层面完全放开。

### 3.6 知识与上下文工程

**S（论文架构/设计）**：论文中的 Knowledge Engine 采用五阶段管线：

1. 解析与清洗；
2. 分块；
3. 语义增强；
4. KV、向量、全文和知识图谱的混合索引；
5. 主动更新。

Context Engine（上下文引擎）强调：

- 基于摘要的记忆压缩；
- 可配置上下文策略；
- 子 Agent 生成结构化摘要；
- 在结构化摘要中保留置信度和证据引用；
- 多层记忆支持不同生命周期的信息。

**I**：比“摘要整段对话”更重要的是让摘要带结构、置信度和证据引用；不过权威事实仍应落在独立状态与证据仓，而不是压缩文本中。

### 3.7 证据可视化与人工审核

**S（论文/README 描述）**：OpenDerisk 的 UI 重点呈现：

- 推理过程的流式进展；
- 多 Agent 动态；
- 根因到证据的关联；
- 结果审阅和人工介入。

**I**：技术诊断会需要的不是暴露所有 Chain-of-Thought（思维链），而是可审计的简化轨迹：使用了哪些数据、形成了哪些诊断判断、每个判断被哪些证据支持或反驳、谁在何时审核并改变状态。

推荐只展示：

```text
诊断判断（Claim）
  ↕ 支持 / 反驳
证据
  + 来源 / 时间范围 / 查询条件 / 原始材料
  + Agent 建议 / 审核结论 / 状态变更
```

### 3.8 开源工程化与生产差距

公开仓库能直接核验：

- **F**：Python 3.10+ 与 `uv` 多包工作区；本地 CLI 和 Web 服务；
- **F**：Web 端采用 Next.js、React、TypeScript 和 Tailwind CSS；
- **F**：提供 OpenRCA、火焰图分析和 DataExpert 等公开场景；
- **F（声明/依赖清单）**：仓库配置或可选依赖覆盖模型适配、MCP、Skills、若干向量/关系存储、OSS、钉钉和飞书扩展点；端到端可用性未逐项运行验证；
- **F**：README 明确说明开源代码当前主要实现架构图中的高亮部分，而不是内部平台全部能力；
- **F**：README 最新动态仍标注 v0.2，而若干包清单已写 v0.7.0，存在文档与包版本漂移；
- **F**：安装脚本会拉取或更新 `main`，脚本中的版本变量没有用于固定 checkout，生产验证必须自行 pin commit/tag；
- **F**：默认配置绑定 `0.0.0.0`，使用 SQLite、占位密钥和本地 sandbox。

**I**：公开版已提供本地 quickstart、CLI/Web UI 与若干场景，但这只证明存在可试用路径，不等于生产就绪。提交记录显示已加入 OAuth2 登录、简单用户管理、统一工具授权和加密秘密存储；生产采用仍需逐项验证认证与授权完整性，并加固 RBAC、租户隔离、密钥托管、网络边界、审计、沙箱、可复现发布和数据治理。论文中的内部平台规模不能替代这些验证。

### 3.9 评测和生产数据：价值与证据边界

论文报告：

- **S**：作者称三个月生产期内应用到 13 个新场景；内部开发者创建 50+ 个专用 Agent；
- **S**：3,000+ 日活用户，60,000+ 次运行/日；
- **S**：在一个对比中，Bailing DeepSeek-V3 由基础 ReAct 的 39 分提升到阶段控制 58 分、多专家 76 分；
- **S**：QWQ 示例运行时从 V1 的 6 分钟增加到 V3 的 22 分钟；
- **S**：Trace Agent 经 1,743 名开发者、6,000+ 案例测试，聚合成功率超过 80%。

评审限制：

- 评测混合 335 条 OpenRCA 与专有 AntRCA；
- 采用人工 100 分制，评分细节和专有数据限制独立复现；
- 同一基础模型下存在 V1/V2/V3 横向比较，39/58/76 并非由更换模型造成；但升级同时改变阶段控制、提示与领域知识、Workflow/ToolCall/Handoff 和专家协作，不能把全部提升单独归因于“Agent 数量”；
- 性能提升伴随明显时延和成本增加。

### 3.10 局限与对问题定位框架的启示

论文自己承认：

- 仍是 copilot（副驾驶），需要人工监督；
- 效果依赖知识库质量；
- 准确率、时延和计算成本存在权衡；
- 蚂蚁成熟可观测生态之外的泛化尚未验证。

**可借鉴**

1. 单体/团队/群组协作模式与隔离审查；
2. 确定性 SOP 和动态 ReAct 混合；
3. 子 Agent 生成结构化摘要，保留置信度与证据引用；
4. 知识索引与上下文策略解耦；
5. 证据链和人工审核；
6. 评估准确率时同时呈现时延和成本。

**不直接照搬**

1. 把生产论文中的所有能力视为开源仓库已有能力；
2. 以 Agent 数量代表诊断质量；
3. 用内部使用规模替代公开可重复评测；
4. 在没有状态合并与责任边界时引入 Group Mode。

## 4. 仓内 8 个 Agent 参考对象

> 本节对象来自仓内 `doc/high-star-agent-context-strategy-survey.md`。该文档是调研材料，不是当前设计决策。
>
> 下述核心机制是对项目文档的事实性归纳（F）；“对问题定位框架的价值”和“局限”是本次设计推断（I），不代表项目官方结论。

### 4.1 LangGraph：状态优先的可恢复编排

**定位**

LangGraph 是 Agent/工作流的状态图编排框架。它关注节点如何围绕共享状态执行，以及一个线程如何通过状态检查点恢复、重放和分叉。

**关键机制**

- 节点读取共享 State，输出增量更新；
- 每个 super-step（超级步骤）生成 `StateSnapshot`；
- Checkpointer 按线程 ID 和状态检查点 ID 持久化；
- 长期 Store 与 thread 内状态分离；
- 消息字段可以裁剪或摘要，但状态模型由应用定义。

**对问题定位框架的价值**

- `DiagnosisState ↔ State`；
- `Job ↔ 可检查的步骤边界`；
- `JobContextManifest ↔ 固定输入引用`；
- `DiagnosisStateDelta ↔ 节点更新`；
- 支持故障后从明确的状态检查点恢复，而不是依赖会话记忆。

**局限**

通用状态允许直接写入完整消息；如果把聊天记录当权威状态，就会失去领域事实、证据约束和状态迁移语义。应借鉴状态检查点机制，但不能把“消息历史”当作权威业务状态。

### 4.2 OpenHands：追加事件与可审计的上下文压缩

**定位**

OpenHands 是交互式编码 Agent。对本项目最有价值的是会话持久化、完整事件日志和长历史治理。

**关键机制**

- `base_state.json` 保存 Conversation（会话）基础状态；
- `events/event-*.json` 追加保存消息和工具事件；
- Workspace 保存实际文件与执行结果；
- 历史压缩器（Condenser）保留首尾、压缩中间，并把压缩结果写成可追踪事件；
- 模型只读取压缩后的本轮输入，原始事件不删除。

**对问题定位框架的价值**

- 原始事件记录与本轮模型输入分离；
- 压缩动作本身可审计；
- 原始证据不因模型窗口压缩而丢失；
- 可以从基础状态 + 事件重建执行过程。

**局限**

会话和事件记录仍是主要工作上下文；自由文本压缩摘要不能替代结构化 DiagnosisState，也不能保证恢复每个事实、排除项和证据关系。

### 4.3 Cline：单任务边界与显式交接

**定位**

Cline 以单个长期任务（Task）为工作边界，结合任务目录、完整记录、命令记录、文件修改和 Git 状态检查点。

**关键机制**

- 每个 Task 有 ID 和独立持久化目录；
- Git 状态检查点保护代码状态；
- Auto Compact 在上下文接近上限时用摘要替换旧消息；
- `/newtask` 把计划、文件、未完成事项提炼到新任务；
- 从完整 Task 历史恢复，而不是只依赖当前窗口。

**对问题定位框架的价值**

- 一个 Case 应有清晰目标和边界；
- 范围发生实质变化时新建 Case/Revision（修订）；
- 结构化交接材料优于隐式跨 Agent 记忆；
- 诊断流程检查点与被诊断系统或文件的检查点应分开管理。

**局限**

摘要服务于继续编码，并不保证每个诊断事实都有对应证据，也不能保证已经排除的假设不会在摘要中被误恢复。

### 4.4 AutoGen：可插拔的模型输入策略

**定位**

AutoGen 提供有状态 Agent/Team，并把模型输入窗口抽象为可替换的 ModelContext 策略。

**关键机制**

- Agent 和 Team 支持 `save_state/load_state`；
- 支持全量、固定条数、Token 上限和保留首尾等可替换策略；
- 模型调用前由 ModelContext 决定提供哪些消息；
- 上下文窗口策略与 Agent 能力解耦。

**对问题定位框架的价值**

- 把模型输入策略做成明确接口；
- Agent Backend 与上下文构造解耦；
- 同一 Job 可用不同策略做评测；
- 状态保存和上下文窗口是两个不同问题。

**局限**

它主要解决“给模型哪些消息”，不定义“哪些诊断事实属于权威状态”。如果 Agent/Team State 和外部 Case 同时可写，会形成双写冲突。**F（截至 2026-07-28）**：AutoGen 官方仓库已标记为维护模式，新项目被建议采用 Microsoft Agent Framework；因此它更适合作为上下文机制参考，而不是新项目的长期依赖默认选型。

### 4.5 CrewAI：确定性 Flow 与自主 Crew 分层

**定位**

CrewAI 同时提供自主多 Agent Crew（团队）和事件驱动 Flow（流程）。Flow 使用 Python/Pydantic 结构化状态与 `start/listen/router` 控制。

**关键机制**

- Flow 负责确定性状态修改和路由；
- Crew/Agent 在有限 Task 内自主执行；
- Agent 具备 Memory（记忆）和 Task Context；
- Flow State 可持久化；
- 长上下文可自动摘要。

**对问题定位框架的价值**

- Application Service/Coordinator 保持 Flow 式确定性；
- Specialist（专家 Agent）只在 Job 内自主；
- Pydantic State 提供可验证的状态边界；
- 控制面和推理面分层。

**局限**

Agent 记忆与 Flow 状态容易形成双写冲突。必须明确只有业务状态仓可以提交正式事实，Agent 记忆只能作为建议或缓存。

### 4.6 Aider：Git 和文件保存工程状态，会话仅用于协作

**定位**

Aider 是聊天驱动的代码修改 Agent，围绕聊天历史、当前文件、Repo Map（仓库地图）和 Git 工作。

**关键机制**

- 真实代码状态由文件和 Git 承担；
- Repo Map 只选择与当前请求相关的代码上下文；
- Token 接近软上限时自动摘要；
- `/clear`、`/drop`、`/map` 支持显式重建输入；
- 新会话可通过 Git diff 恢复工程事实。

**对问题定位框架的价值**

- 工程产物和证据独立于对话保存；
- 模型只读取当前相关材料；
- 模型输入可以丢弃，并从 Git 和文件状态重建；
- 显式选择比无上限积累更可靠。

**局限**

它依赖 Git 和人工交互，不提供事实、假设、证据与审核的领域状态机。

### 4.7 SWE-agent：完整保存执行轨迹，只筛选模型输入

**定位**

SWE-agent 面向代码任务，保存完整执行轨迹，并通过历史处理器控制每次模型调用看到的内容。

**关键机制**

- 原始执行轨迹持续保存；
- Sandbox/Repository 保存真实执行环境；
- 模型调用前可过滤早期观察结果、大段工具输出和差异内容；
- 过滤器是可插拔策略；
- 可调整消息布局以提高 prompt cache（提示缓存）命中。

**对问题定位框架的价值**

- Context Processor 是独立扩展点；
- 大段日志只保存在原始材料和证据库中；
- 本轮模型输入可以删减，但权威代码仓库不变；
- 可对不同处理器做可复现对照试验。

**局限**

典型任务是一次性编码任务，执行轨迹生命周期较短；它没有提供面向长期 Case 的事实状态机、跨 Job 合并，也没有定义审核结果如何写入权威状态。

### 4.8 mini-SWE-agent：验证复杂机制是否真的有收益

**定位**

mini-SWE-agent 是面向短期、封闭编码任务的极简基线。

**关键机制**

- 线性消息历史；
- Repository 保存代码事实；
- 每个 Shell 操作在独立进程中执行；
- 不做复杂压缩，所有步骤持续追加。

**对问题定位框架的价值**

- 作为“没有复杂上下文治理”的基准组；
- 衡量上下文编译器、摘要、事件化和多 Agent 的真实增益；
- 暴露复杂机制增加的时延、成本和潜在故障点。

**局限**

历史线性增长；没有结构化状态、完整恢复语义或长期上下文治理，不适合直接承载长周期诊断 Case。

## 5. 综合比较

### 5.1 功能与组织方式

| 对象 | 主要目标 | 默认组织方式 | 最强项 | 不能替代什么 |
|---|---|---|---|---|
| OpenSRE | AI-SRE 调查与评测 | 单调查 Agent + 多阶段流水线 | 调查约束、证据驱动 RCA、评测闭环 | 完整 Case/Job 业务控制面 |
| OpenDerisk | 开源核心框架；论文另描述蚂蚁内部生产平台 | 论文：中央编排 + 专家协作；开源端到端等价性未复现 | 多角色、结构化摘要、证据可视化（论文/README） | 可独立复现的通用准确率与生产等价性证明 |
| LangGraph | 状态图编排 | 图 + 节点 + 状态检查点 | 结构化状态、恢复、重放、分叉 | 诊断领域模型 |
| OpenHands | 编码 Agent | Conversation + 事件日志 | 事件溯源、压缩留痕 | 权威诊断事实模型 |
| Cline | 单任务编码 Agent | 长任务 + Git 状态检查点 | 单目标边界、交接 | 诊断判断与证据审核 |
| AutoGen | Agent/Team 框架 | 消息上下文 + Team | 可插拔上下文策略 | 权威状态及审核写入规则 |
| CrewAI | 多 Agent + Flow | Flow 控制 + Crew 执行 | 确定性流程与自主执行分层 | 唯一权威状态保证 |
| Aider | 代码修改 Agent | Chat + Git/文件 | 对话与工程事实分离 | 长周期诊断状态机 |
| SWE-agent | 代码任务 Agent | 执行轨迹 + 历史处理器 | 原始轨迹与本轮模型输入分离 | 跨 Job Case 治理 |
| mini-SWE-agent | 极简编码 Agent | 线性历史 | 低复杂度基线 | 长上下文与恢复 |

### 5.2 状态、上下文和恢复

| 对象 | 主要状态载体 | 模型输入策略 | 恢复方式 | 重建边界 |
|---|---|---|---|---|
| OpenSRE | 调查状态（AgentState）+ 证据条目（EvidenceEntry）；REPL：独立会话文件 | 调查循环：预算、预取证消息、重复结果优先淘汰、低价值工具交换淘汰、消息截断；REPL 另有 `/compact` | 明确支持 REPL `/resume`；未见六阶段调查状态的持久检查点和重启恢复说明 | 调查状态和 REPL 会话均不应承担权威业务状态 |
| OpenDerisk | 论文：中央编排、多层记忆和知识；开源实现边界待核验 | 论文：摘要压缩、按策略选择、子 Agent 结构化摘要 | 论文描述平台级生命周期管理；开源恢复语义未独立验证 | 需实测，暂不评级 |
| LangGraph | 图状态（Graph State） | 应用自定义消息裁剪或摘要 | 状态检查点重放或分叉 | 可从外部状态检查点重建 |
| OpenHands | 基础状态 + 只追加事件日志 | 历史压缩器保留首尾、压缩中间 | 基础状态 + 事件重建 | 依赖持久化基础状态与事件，不依赖活跃物理会话 |
| Cline | 任务存储 + Git | 自动压缩、显式新建任务 | 完整任务记录 + Git | 依赖持久化任务记录；工程事实可由 Git 恢复 |
| AutoGen | Agent/Team State | 多种 ModelContext | save/load state | 依赖已保存的 Agent/Team State |
| CrewAI | Flow State + Agent Memory | 自动摘要/Task Context | Flow 持久化 | 必须协调 Flow State 与 Memory 的恢复边界 |
| Aider | Git/文件 + Chat | Repo Map、摘要、显式选择 | Git diff + Chat | 工程事实可由 Git/文件重建，协作语义依赖 Chat |
| SWE-agent | 执行轨迹 + 代码仓库 | 历史处理器 | 轨迹和环境重建 | 依赖持久化执行轨迹与可重建环境 |
| mini-SWE-agent | Repository + 线性消息 | 不压缩 | 任务内轨迹 | 仅覆盖任务内线性历史 |

### 5.3 证据、审核、成熟度与适配（适配判断为 I）

| 对象 | 证据可追溯 | 显式审核 | 公开评测证据 | 对问题定位框架的适配 |
|---|---:|---:|---|---|
| OpenSRE | 中强：工具证据包含 EvidenceEntry 及来源信息；公开状态契约未显示强制绑定诊断判断和证据 ID | 弱—中：区分已有证据支持和尚待验证的诊断判断，并有事后人工评分，但默认 RCA 链无独立审核角色 | 有框架；README 明确暂无结果 | 受控调查与评测机制强，独立审核与 Case/Job 控制面需另建 |
| OpenDerisk | 论文/README 强调证据链；未独立复现 | 论文强调人工介入与审核，案例包含评审 Agent | 作者报告；含专有 AntRCA 和人工评分 | 多 Agent 协同与结构化摘要可借鉴；生产等价性待验证 |
| LangGraph | 取决于 State 设计 | 仓内材料未见内置诊断领域审核 | 非诊断评测 | 状态与恢复基础强 |
| OpenHands | 事件强、事实语义弱 | 仓内材料未见内置诊断领域审核 | 编码任务评测 | 审计与压缩机制强 |
| Cline | Git/任务轨迹强 | 人工交互 | 编码任务评测 | Task 边界与交接中强 |
| AutoGen | 消息级 | 取决于 Team | 框架级 | 上下文策略中强 |
| CrewAI | Flow 级 | 取决于应用 | 框架级 | 控制/执行分层中强 |
| Aider | Git 级 | 人工确认 | 编码任务评测 | 事实外置中强 |
| SWE-agent | 执行轨迹可追溯性强 | 仓内材料未见内置诊断领域审核 | SWE-bench 生态 | 模型输入筛选强 |
| mini-SWE-agent | 基础执行轨迹 | 仓内材料未见内置诊断领域审核 | 基线用途 | 对照实验强 |

## 6. 对当前问题定位框架的预比较

### 6.1 当前正式设计已有的稳定方向

根据 `design/` 当前文档，以下方向已经具备较清晰的设计意图：

- Application Service（应用服务）是唯一业务状态写入者；
- Diagnosis Coordinator（诊断协调器）保持确定性、无副作用；
- Case Repository 与 BlobStore 分离结构化状态和大文件；
- V1 Dispatcher 只路由 Routing/Diagnosis Worker；目标架构新增 Review Worker；
- Shared Diagnosis Runtime 与版本化 Diagnosis Skill Catalog 解耦 Agent Backend；
- Agent 输出先作为类型化结果，而不是直接写业务状态；
- 证据审核器（Evidence Reviewer）已进入目标架构，但不是 V1 已实现能力；审核器只产生 `ReviewAssessment`，应用服务根据协调器决策更新案例状态。

这些方向与业界机制总体相容：确定性控制面、类型化边界、大文件与证据材料外置、可替换 Agent Backend 都值得保留。

### 6.2 当前最需要在详细设计中澄清的矛盾

当前正式决策仍写有“同一 Agent 跨任务保持会话；不同 Agent 之间采用结构化交接”。仓内新调研则建议重开该决策。现阶段应将下列问题放入评审，而不是预先下结论：

1. 如果案例状态是权威状态，为什么诊断连续性仍依赖会话？
2. 如果任务输入必须可复现，隐含的会话状态如何进入 `JobContextManifest`？
3. 如果会话丢失后无法准确恢复，它是否事实上已经成为第二套权威状态？
4. 为什么跨 Agent 必须结构化交接，而同 Agent 可以依赖不可见记忆？
5. Reviewer 读取哪些固定材料、输出何种 `ReviewAssessment`；若未来采用诊断判断模型，Application Service/Coordinator 如何据此决定是否允许状态变更？
6. 压缩、摘要、检索和上下文选择是否被版本化、记录输入引用并可重放？

### 6.3 从业界洞察推导出的候选组合

> 以下是 **I：本次推断**，不是当前设计已确认结论。

```text
案例 / 诊断状态（Case / DiagnosisState，权威当前状态）
        + 只追加的领域事件记录
        + 原始证据和材料（不可因压缩丢失）
                         ↓
任务输入清单（JobContextManifest，固定任务输入）
                         ↓
上下文编译器与版本化输入策略（Context Compiler / Context Policy）
                         ↓
单次模型输入包（ContextPack，受 Token 预算约束）
                         ↓
每次任务尝试默认新建 Agent 会话
                         ↓
结构化建议与结果（Proposal / Outcome）
                         ↓
状态写入规则 + 审核器（Reviewer）
                         ↓
Application Service 单点提交状态
```

机制来源：

- OpenSRE：受控调查循环、证据来源与采集信息、生产误判回流；
- OpenDerisk：专家角色、隔离审查、结构化摘要、证据链与人工审核；
- LangGraph：状态检查点、重放和分叉；
- OpenHands：只追加事件日志与压缩留痕；
- Cline：单 Task 边界和显式交接；
- AutoGen：可插拔 Context Policy；
- CrewAI：确定性 Flow + Job 内自主执行；
- Aider：工程产物独立于对话；
- SWE-agent：原始执行轨迹不变，本轮模型输入可筛选；
- mini-SWE-agent：为所有复杂机制提供基准组。

## 7. 框架定稿后应追加的正式对比

当前设计完成后，建议在本报告和 PPT 追加 4—6 页，不重写业界洞察，只补以下矩阵：

1. **能力覆盖**：告警接入、问题建模、工具规划、调查、证据、审核、报告、修复和反馈。
2. **控制与状态**：谁能写 Case；Coordinator 是否纯函数；Job/Attempt 的幂等、重试和恢复语义。
3. **上下文**：会话生命周期、ContextPack 结构定义、选择/压缩策略、版本、复现方式和预算。
4. **证据与审核**：诊断判断状态、支持/反驳关系、证据来源与采集信息、Reviewer 隔离和状态变更规则。
5. **安全治理**：凭证、脱敏、遥测、只读/写操作、审批、审计和多租户隔离。
6. **评测与运维**：准确率、证据完整度、错误结论率、工具效率、时延、Token 成本、失败恢复和人工接管率。

届时每项必须标注：

- 当前设计是否覆盖；
- 覆盖在 V1、目标架构还是暂不考虑；
- 相比 OpenSRE/OpenDerisk 的优势、差距和主动取舍；
- 可验证的测试或指标；
- 仍需决策的负责人和截止点。

## 8. 技术诊断会建议决策

本轮评审建议只做四个原则性决策：

1. 是否确认“结构化案例/诊断状态（Case/DiagnosisState）是唯一权威业务状态，Agent 会话不承担权威状态”；
2. 是否确认“每次 JobAttempt 的输入必须由可持久化、可准确复现的 Manifest 固定”；
3. 是否确认“证据、诊断判断、审核结论和状态变更必须可审计，摘要不得覆盖原始材料”；
4. 是否确认“诊断能力必须同时用准确率、错误结论率、证据完整度、工具效率、时延和成本评测”。

如果这四项不先确定，后续无论采用单 Agent、多 Agent、长会话还是新会话，都很容易在正确性、恢复和成本之间反复摇摆。

## 9. PPT 页面结构

随本报告生成的华为企业汇报风网页 PPT 共 55 页。该篇幅采用上限配置：OpenSRE/OpenDerisk 各用章节幕封 + 9 页完整机制链，8 个仓内对象各取最低要求 3 页，综合比较压缩为 3 页；大量页面是图示与单主题页，不等于 55 页高密度文字。

| 页码 | 内容 |
|---:|---|
| 1—4 | 封面、执行摘要、目录、证据口径 |
| 5—14 | OpenSRE：章节幕封 + 9 页详细介绍 |
| 15—24 | OpenDerisk：章节幕封 + 9 页详细介绍 |
| 25—49 | 仓内 8 个 Agent 参考对象：章节幕封 + 每个对象 3 页 |
| 50—52 | 综合比较：3 页，第一页兼作章节引导 |
| 53—54 | 与当前设计的预比较、框架定稿后的正式对比维度 |
| 55 | 结论与下一步 |

## 10. 一手来源

### OpenSRE

- Repository: <https://github.com/Tracer-Cloud/opensre>
- Architecture: <https://github.com/Tracer-Cloud/opensre/blob/main/docs/ARCHITECTURE.md>
- Investigation Pipeline: <https://github.com/Tracer-Cloud/opensre/blob/main/docs/investigation-pipeline-architecture.md>
- State Contract: <https://github.com/Tracer-Cloud/opensre/blob/main/core/state/README.md>
- Evidence Contract: <https://github.com/Tracer-Cloud/opensre/blob/main/core/state/evidence.py>
- Context Budget: <https://github.com/Tracer-Cloud/opensre/blob/main/core/context_budget.py>
- Deployment: <https://github.com/Tracer-Cloud/opensre/blob/main/DEPLOYMENT.md>
- Releases: <https://github.com/Tracer-Cloud/opensre/releases>
- Sessions: <https://www.opensre.com/docs/sessions>
- Closed-Loop Learning: <https://www.opensre.com/docs/closed-loop-learning>
- Masking: <https://www.opensre.com/docs/masking>
- Interactive Shell Privacy: <https://www.opensre.com/docs/interactive-shell-privacy>
- CloudOpsBench: <https://www.opensre.com/docs/cloudopsbench>
- Synthetic RCA Scenarios: <https://github.com/Tracer-Cloud/opensre/tree/main/tests/synthetic/rds_postgres>

### OpenDerisk 与候选

- OpenDerisk Repository: <https://github.com/derisk-ai/OpenDerisk>
- OpenDerisk Paper: <https://arxiv.org/abs/2510.13561>
- OpenDerisk Paper HTML: <https://arxiv.org/html/2510.13561v2>
- SREWorks Repository: <https://github.com/alibaba/SREWorks>
- CodeFuse-muAgent Repository: <https://github.com/codefuse-ai/CodeFuse-muAgent>
- Alibaba Cloud Observability MCP Server: <https://github.com/aliyun/alibabacloud-observability-mcp-server>
- SysOM AI: <https://github.com/aliyun/sysom-ai>
- UModel: <https://github.com/alibaba/UnifiedModel>
- RCAgent Paper: <https://arxiv.org/abs/2310.16340>
- AgentScope: <https://github.com/agentscope-ai/agentscope>
- Spring AI Alibaba: <https://github.com/alibaba/spring-ai-alibaba>

### 仓内 Agent 参考对象

- LangGraph: <https://github.com/langchain-ai/langgraph>
- LangGraph Persistence: <https://docs.langchain.com/oss/python/langgraph/persistence>
- OpenHands: <https://github.com/OpenHands/OpenHands>
- OpenHands Conversation Persistence: <https://docs.openhands.dev/sdk/guides/convo-persistence>
- OpenHands Condenser: <https://docs.openhands.dev/sdk/arch/condenser>
- Cline: <https://github.com/cline/cline>
- Cline Task Management: <https://docs.cline.bot/core-workflows/task-management>
- Cline Auto Compact: <https://docs.cline.bot/features/auto-compact>
- AutoGen: <https://github.com/microsoft/autogen>
- AutoGen Model Context: <https://microsoft.github.io/autogen/stable/reference/python/autogen_core.model_context.html>
- CrewAI: <https://github.com/crewAIInc/crewAI>
- CrewAI Documentation: <https://docs.crewai.com/>
- Aider: <https://github.com/Aider-AI/aider>
- Aider Commands: <https://aider.chat/docs/usage/commands.html>
- SWE-agent: <https://github.com/SWE-agent/SWE-agent>
- SWE-agent History Processor: <https://swe-agent.com/1.0/reference/history_processor_config/>
- mini-SWE-agent: <https://github.com/SWE-agent/mini-swe-agent>

### 当前仓库

- `design/README.md`
- `design/target-diagnosis-architecture.md`
- `design/v1-overall-framework.md`
- `design/v1-option-decisions.md`
- `design/v1-agent-access-and-file-transfer.md`
- `doc/high-star-agent-context-strategy-survey.md`
