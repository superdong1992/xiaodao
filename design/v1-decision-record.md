# Problem Locator V1 决策记录

状态：与 [V1 基线设计](v1-baseline-design.md) 同步生效

更新时间：2026-07-31

## 1. 文档定位

本文记录 V1 当前有效的关键设计选择、理由、接受的代价、被替代的旧选择和复议条件。

本文不重复完整架构和接口说明。规范性要求以[《Problem Locator V1 基线设计》](v1-baseline-design.md)为准。

状态定义：

| 状态 | 含义 |
|---|---|
| 已确认 | 已进入当前 V1 基线 |
| 暂缓 | 当前不实现，但保留演进边界 |
| 已替代 | 曾经采用，现已被新决策替换 |
| 待详细设计 | 方向已确认，具体字段或机制未确定 |

## 2. 当前有效决策索引

| 编号 | 主题 | 当前选择 | 状态 |
|---|---|---|---|
| DR-001 | V1 范围与实现边界 | 单节点低并发；当前仓库只设计；正式代码进入新仓库 | 已确认 |
| DR-002 | 客户端接入与文件传输 | Agent Skill（智能体技能）+ Remote MCP（远程 MCP）传控制，HTTP 传文件 | 已确认 |
| DR-003 | 业务归一与写入边界 | Application Service（应用服务）单写入，Coordinator（协调器）纯决策 | 已确认 |
| DR-004 | 执行与等待模型 | 底层统一异步，支持立即返回和有限同步等待 | 已确认 |
| DR-005 | 权威诊断状态 | Case（诊断案例）+ DiagnosisState（诊断状态）保存全部跨 Job 必需信息 | 已确认 |
| DR-006 | Job 语义与固定输入 | Job（任务）是自包含业务单元，创建时固定小型结构化快照、引用和执行版本 | 已确认 |
| DR-007 | 上下文构造 | Context Builder（上下文构建器）按 Job 固定版本生成有界输入 | 已确认 |
| DR-008 | Session 与 Backend 生命周期 | 每个 Agent Job 默认创建新 Session（会话）；Session 可丢弃 | 已确认 |
| DR-009 | Skill、工具与工作区 | 按角色注入版本化能力；使用 Job 级临时 Workspace（工作区） | 已确认 |
| DR-010 | 类型化编排与最终复核 | ROUTE / DIAGNOSE / REVIEW 任务推进；最终结论必须独立复核 | 已确认 |
| DR-011 | 并发与恢复边界 | 同一 Case 一个活跃 Job；已知 case_id 可从持久化状态继续 | 已确认 |
| DR-012 | 数据保留与安全基线 | 持久化业务状态，不持久化 Agent 对话；接受受控内网安全假设 | 已确认 |

## 3. 当前有效决策

### DR-001：V1 范围与实现边界

**状态：已确认**

决策：

- V1 部署在受控内网，采用单节点、单服务进程、低并发形态。
- 当前仓库只保存设计文档和调研材料。
- 正式实现代码在新的代码仓库中开发。
- 正式 V1 不兼容当前 Demo 的内部结构和临时接口。
- V1 实现 Router Agent（路由智能体）、Specialist Agent（专项智能体）和 Reviewer Agent（复核智能体）。
- General Code Agent（通用代码智能体）暂缓。

理由：

- 先确认领域状态、Job 和上下文边界，避免被 Demo 结构反向限制。
- 单节点足以验证核心产品语义。
- Reviewer 复用现有 Typed Worker（类型化工作器）模式，不需要新增一套编排系统。

接受的代价：

- 首版没有多实例高可用。
- 正式实现需要初始化新仓库和新的工程结构。

复议条件：

- 出现明确的多实例容量、故障接管或独立扩缩容需求。

### DR-002：客户端接入与文件传输

**状态：已确认**

决策：

- 客户端以 Agent Skill（智能体技能）作为使用入口。
- 结构化控制使用 Remote MCP（远程模型上下文协议）。
- Attachment（附件）上传和 Artifact（产物）下载使用 HTTP。
- Agent CLI 通过 Remote MCP 的 `PrepareAttachment（准备附件）`获取结构化上传描述；Web 或普通客户端可通过等价 HTTP `POST` 调用同一 Application Service 命令。
- 等待中的 Case 只通过 `SubmitSupplement（提交补充资料）`采用补充信息；一次命令可同时提交结构化文本和已经 `READY（就绪）` 的 Attachment 引用。
- 用户不安装 Local MCP Server（本地 MCP 服务）或项目专用常驻进程。
- Remote MCP Adapter（远程 MCP 适配器）和 HTTP File Adapter（HTTP 文件适配器）使用同一稳定服务地址并复用 Application Service。
- Web（网页）客户端暂缓，但后续必须复用同一接口和业务模型。

理由：

- MCP 适合结构化命令和结果，不适合传输大型文件字节。
- HTTP 文件流和 `curl` 具有良好的客户端兼容性。
- 协议适配层共享 Application Service，避免产生两套 Case 和 Attachment 状态机。

接受的代价：

- 客户端需要同时具备 MCP 和 HTTP/`curl` 能力。
- 文件上传是多步骤交互。
- Attachment 上传完成不会自动推进 Case，客户端还需提交一次幂等的 `SubmitSupplement`。

复议条件：

- 客户端运行环境没有 Shell 或 HTTP 上传能力；
- 需要浏览器直接上传或统一身份认证。

### DR-003：业务归一与写入边界

**状态：已确认**

决策：

- Application Service 是 Case、DiagnosisState、Job、JobOutcome、Evidence 和 Attachment 元数据的唯一业务写入入口。
- Runtime 负责提供方结果解析与 Schema 校验。
- Application Service 负责幂等、归属、活跃 Job、版本和引用等技术校验。
- Diagnosis Coordinator 只根据 `CaseSnapshot + Validated Trigger` 计算 `TransitionPlan（状态转换计划）`，其中包含业务上接受的状态增量和可选 JobSpec。
- Application Service 只执行并持久化 TransitionPlan；下一 Job 的快照从计划应用后的目标 DiagnosisState 物化。
- Coordinator 不读写 Repository、不提交 Dispatcher、不调用 Agent。
- Dispatcher、Worker、Context Builder、Runtime 和 Agent Backend 不修改 Case，也不创建后续 Job。

理由：

- 单一写入者避免多个组件并发覆盖业务状态。
- 纯 Coordinator 便于测试状态转换。
- 外部命令和内部 JobOutcome 可以复用同一个状态闭环。

接受的代价：

- Application Service 需要负责事务、幂等和版本校验。
- 组件之间需要清晰的 DTO（数据传输对象）。

复议条件：

- 不改变单写入和纯决策原则；多实例时只复议事务、队列和消息投递实现。

### DR-004：执行与等待模型

**状态：已确认**

决策：

- 服务端底层统一使用异步 Job。
- 客户端可以立即返回并查询，也可以有限同步等待同一个异步 Job。
- 有限等待超时不取消、不重建、不重复提交 Job。
- V1 使用进程内 Job Dispatcher（任务分发器）和有界 Worker Pool（工作器池）。
- 等待用户输入或附件期间，当前 Job 已经结束，不占用 Worker。

理由：

- 一套执行语义兼顾短任务体验和长任务稳定性。
- 避免分别维护同步和异步两套诊断引擎。

接受的代价：

- 需要 Case 查询和明确的等待状态。
- 需要处理状态提交后分发失败。

复议条件：

- 出现外部队列、多实例 Worker 或通知推送需求。

### DR-005：权威诊断状态

**状态：已确认**

决策：

- Repository 是系统唯一真相源；Case 及其 DiagnosisState 是当前诊断状态的唯一权威投影。
- DiagnosisState 最少保存 ProblemSpec（问题规格）、确认事实、活跃假设、已排除假设、未决问题、待补要求、Evidence 引用和候选结论。
- Agent Session（智能体会话）、Context View（上下文视图）、模型摘要和 Workspace 都是可丢弃派生状态。
- 关闭所有 Session 后，系统仍必须能够创建下一 Job。

理由：

- 长期 Session 会积累旧假设、过期指令和无关工具输出。
- 如果 Session 丢失会导致 Case 无法继续，Repository 就不是真正权威状态。
- 结构化状态便于查询、校验、恢复和跨 Agent 交接。

接受的代价：

- 必须设计 DiagnosisState 和 DiagnosisStateDelta Schema。
- Agent 的有用中间结论必须显式提升为结构化状态或 Evidence。

替代的旧决策：

- 原 OPT-007 中“Agent 完整对话由服务端 Session 持有”的部分；
- 原 OPT-023 中“Session 丢失使未完成 Case 无法继续”的部分。

复议条件：

- 不复议权威状态原则；只复议 DiagnosisState 的具体结构和存储实现。

### DR-006：Job 语义与固定输入

**状态：已确认**

决策：

- Job 是具有明确目标、完成边界和 Typed JobOutcome（类型化任务结果）契约的业务单元。
- Job 不再被定义为“向已有 Session 追加一次 Turn（交互轮次）”。
- Job 创建时固定：
  - `base_state_revision`；
  - `context_snapshot（上下文快照）`；
  - Evidence 引用；
  - Attachment 引用；
  - 必要的历史 Outcome 引用；
  - Agent Profile 版本；
  - ROUTE Job 可见的 Diagnosis Skill 版本集合；
  - Diagnosis Skill 版本；
  - Tool Bundle 版本；
  - Context Policy（上下文策略）版本；
  - Output Contract（输出契约）版本；
  - REVIEW Job 的候选结论 ID、版本和内容哈希；
  - Turn、Token、时间和工具输出资源上限。
- V1 将这些字段直接放在 Job 中，不建立独立 JobContextManifest（任务上下文清单）实体。
- 用户补充新信息后创建引用新状态版本的新 Job。
- `context_snapshot` 是在创建 Job 的业务提交中，从 TransitionPlan 应用后的目标 DiagnosisState 复制的小型结构化执行视图，不包含完整聊天、大型文件或 Workspace。
- `base_state_revision` 用于检测过期和并发冲突，`context_snapshot` 用于重现 Job 的实际结构化输入。
- 下一 Job 的 `context_snapshot` 从同一 TransitionPlan 应用后的目标 DiagnosisState 生成。
- Job 引用的 Attachment、Evidence、Artifact、JobOutcome 和版本化运行资产必须不可变；内容改变时创建新 ID 或新版本。
- Outcome 只能消费 Job 固定可见范围内的既有资源；候选结论的支持证据只能来自 Job 固定 Evidence 或本 Outcome 的候选 Evidence。

理由：

- Job 的完整业务输入必须独立于旧 Session。
- V1 不保存完整状态修订历史，也不使用 Event Sourcing（事件溯源）；在 Job 中直接保存小型快照是最简单的可重现方式。
- 固定版本使并发校验和故障分析具有明确基础。
- 将清单字段放入 Job 可以避免首版过度抽象。

接受的代价：

- Job Schema 比原设计更丰富。
- 每个 Job 会重复保存少量结构化状态，但不会复制大型附件。
- 状态版本发生变化时，旧 Outcome 可能被判定为过期。

替代的旧决策：

- 原 OPT-015 中“一个 Job 是对长期 Session 的一次调用”。

复议条件：

- Job 上下文字段明显膨胀或需要独立生命周期时，再提取 JobContextManifest 实体。

### DR-007：Context Builder（上下文构建器）

**状态：已确认**

决策：

- Runtime 使用独立逻辑边界 Context Builder，根据 Job 固定引用构造本轮有限输入。
- Context Builder 从 Job 的 `context_snapshot` 开始，不用执行时最新 DiagnosisState 替换它。
- Context Builder 为 Router、Specialist 和 Reviewer 构造不同视图。
- Router 使用 ROUTE Job 固定的 Skill 摘要集合；Reviewer 使用 REVIEW Job 固定的快照、候选结论和 Evidence，不读取执行时最新状态。
- 大型 Evidence 片段由 Job 固定 locator 或固定 Context Policy 的确定性规则选择。
- 默认不发送完整聊天、过期状态、被替代决定、大型日志全文和无关工具输出。
- V1 使用确定性优先级和 Token（令牌）预算，不实现模型自动摘要。
- 必需内容超过预算时明确失败或拆分 Job，不静默丢弃。
- Context Builder 只读，不修改 Repository。

理由：

- 持久化历史和模型实际需要读取的内容不是同一个集合。
- 角色化、有限的输入可以降低噪声和旧状态复活。
- 规则式构建比首版引入自动摘要更容易验证。

接受的代价：

- 需要定义选择顺序和预算。
- 某些大型 Evidence 需要预先提取相关片段。

复议条件：

- Evidence 数量和规模使规则式选择不足时，再评估索引、检索或带来源的摘要。

### DR-008：Session（会话）与 Agent Backend（智能体执行后端）生命周期

**状态：已确认**

决策：

- 每个 ROUTE、DIAGNOSE 和 REVIEW Job 默认创建新的 Agent Session。
- 一个 Job 内可以有多次模型和工具调用。
- Job 结束后 Session 关闭。
- 删除 Case Session Registry（案例会话注册表）和跨 Job Session Key（会话键）设计。
- 保留共享 Diagnosis Runtime、Agent Profile 和统一 Agent Backend 接口。
- Backend Session Handle 不进入业务模型。
- 将来 Session 复用只能作为 Runtime 内部透明性能优化。

理由：

- 从根源消除跨 Job 长会话增长。
- Session 故障、回收和服务重启不再改变业务正确性。
- 不需要设计空闲 Session 回收、容量上限和亲和性。

接受的代价：

- 每个 Job 有 Session 创建成本。
- 可能减少特定提供方的 Prompt Cache（提示缓存）收益。
- Context Builder 必须提供充分的结构化输入。

替代的旧决策：

- 原 OPT-015：同一个 Agent 跨 Job 保持 Session；
- 原 OPT-018：Case Session Registry；
- 原 OPT-022：多个 Job 映射为同一个 Session 上的连续 Turn。

复议条件：

- 有实际延迟和成本数据证明复用收益显著，并且复用与不复用保持相同业务语义。

### DR-009：Skill（技能）、工具与工作区

**状态：已确认**

决策：

- Router 只获得 ROUTE Job 固定的路由规则和 Diagnosis Skill 摘要目录。
- Specialist 只获得选定的完整 Diagnosis Skill 和对应 Tool Bundle。
- Reviewer 获得复核指令、候选结论和相关 Evidence，不继承 Specialist 对话。
- Diagnosis Skill 随服务版本发布，运行期间只读，版本不可原地覆盖。
- Runtime 为每个 Job 创建临时 Workspace。
- READY Attachment 根据 Job 固定引用只读物化到该 Workspace。
- 需要跨 Job 保留的结果先通过 JobOutcome 提交为候选 Evidence 或 Artifact；文件写入 BlobStore 持久化暂存区，再由 Application Service 按 Coordinator 接受的 proposal key 发布正式记录。

理由：

- 角色化注入减少无关能力和上下文。
- Job 级 Workspace 与可丢弃 Session 语义一致。
- 版本固定保证 Job 不受运行期资产变化影响。

接受的代价：

- 需要管理 Profile、Skill、Tool Bundle 和 Workspace 装配。
- 首版不支持 Skill 热更新。

替代的旧决策：

- 原 OPT-019 中的 Session 子目录和向已有 Session 追加附件。

复议条件：

- 需要 Skill 独立发布、动态启停或强隔离工作区。

### DR-010：类型化编排与最终复核

**状态：已确认**

决策：

- Coordinator 通过 `ROUTE / DIAGNOSE / REVIEW` 三类 Job 推进 V1。
- Router、Specialist 和 Reviewer 不互相直接调用。
- Agent 输出是类型化提案。
- Runtime 完成提供方输出解析和 Schema 校验。
- Agent 尚未产生业务载荷时的上下文超限、Backend 失败或工具超时使用 `ExecutionFailure（执行失败）`载荷。
- Application Service 校验 `base_state_revision` 与当前 `DiagnosisState.revision`、活跃 Job、资源归属和 Evidence 引用。
- Coordinator 决定 DiagnosisStateDelta 的业务合法性，并在 TransitionPlan 中返回 `accepted_state_delta（已接受状态增量）`。
- Agent 提出的新事实只有进入 `accepted_state_delta` 后才能成为 `confirmed_facts（确认事实）`。
- Specialist 给出最终候选结论后，Coordinator 返回 REVIEW JobSpec，Application Service 原子保存候选结论并创建 REVIEW Job。
- REVIEW Job 和 ReviewAssessment 都绑定候选结论 ID、版本、内容哈希以及固定 Evidence。
- `ReviewAssessment.PASS` 的 Evidence 集合必须来自 REVIEW Job 固定引用，并覆盖候选结论的全部支持证据。
- ReviewAssessment 为 `PASS` 前，候选结论不能成为 `final_result`，Case 不能进入 `RESOLVED`。

理由：

- 类型化 Job 使阶段状态、输入和结果可观察。
- 独立 Reviewer 减少 Specialist 历史假设对复核的影响。
- Coordinator 集中业务转换，Application Service 保持单写入和技术完整性边界。

接受的代价：

- 最终诊断增加一次 Agent 调用。
- 必须设计 ReviewAssessment 和复核失败后的状态转换。

替代的旧决策：

- 原目标架构中“Reviewer 不属于 V1”的说明。

复议条件：

- 可以调整哪些中间结果需要复核；但任何写入 `final_result` 并使 Case 进入 `RESOLVED` 的结论都必须取得 `ReviewAssessment.PASS`。改变此规则必须形成新的替代决策。

### DR-011：并发与恢复边界

**状态：已确认**

决策：

- 同一个 Case 同时只运行一个活跃 Job。
- 不同 Case 可以有界并发。
- 所有 Case 和活跃 Job 修改使用 `case_revision` 条件更新；JobOutcome 合并额外校验 `base_state_revision == DiagnosisState.revision`。
- 已知 `case_id` 的未完成 Case 可以从持久化 DiagnosisState 手动继续。
- `ResumeCase（恢复案例）`只处理已持久化的 `PENDING` Job、属于旧 `runtime_epoch（运行代次）` 的 `RUNNING` Job，或已经持久化为 `INTERRUPTED` 且尚未被替代的 Job，不注入新输入。等待资料的 Case 继续使用 `SubmitSupplement`。
- PENDING Job 在固定资产仍可加载时重新分发同一 Job 和快照；INTERRUPTED Job 创建新的替代 Job、快照和 `job_id`，并保持原业务阶段，REVIEW 不得降级为 DIAGNOSE。
- Case 已经持久化为 `INTERRUPTED` 且没有活跃 Job 时，`ResumeCase` 从最近一个尚未被替代的 INTERRUPTED Job 创建替代 Job；`replacement_for_job_id` 唯一约束保证幂等。
- Application Service 只做恢复技术校验；Coordinator 返回包含旧 Job 中断、清理活跃 Job 和同阶段替代 JobSpec 的 Resume TransitionPlan，Application Service 一次条件事务执行。
- 固定资产不可用时不替换最新版：PENDING Job 与 Case 进入 `FAILED`，已 INTERRUPTED Job 保持终态而 Case 进入 `FAILED`。
- 旧 Job 的迟到 Outcome 因活跃 Job 与状态不匹配而拒绝；可恢复 Job 的工具默认只读或幂等。
- V1 不自动发现历史 Case。
- V1 不自动恢复服务重启时处于 RUNNING 的 Job；继续 Case 前必须先将旧 Job 转为 `INTERRUPTED`。
- V1 不实现多实例故障接管。

理由：

- 串行 Case 执行避免首版引入分支合并和冲突归并。
- Session 已经可丢弃，服务重启不再需要作废整个 Case。
- 手动继续与自动任务恢复是两个独立能力。

接受的代价：

- 同一 Case 不能并行探索多个假设。
- 服务重启后可能需要用户显式继续。

替代的旧决策：

- 原 OPT-009 中“服务重启后未完成 Case 必须重新创建”；
- 原 OPT-020 中依赖同一 Session 串行锁的部分。

复议条件：

- 明确需要并行探索、自动重调度或多实例高可用。

### DR-012：数据保留与安全基线

**状态：已确认**

决策：

持久化：

- Case 和 DiagnosisState；
- Job 和 Typed JobOutcome；
- RouteDecision、DiagnosisOutcome 和 ReviewAssessment；
- Evidence；
- Attachment 和 Artifact 元数据与文件；
- 实际使用的 Profile、Skill、Tool Bundle、Context Policy 和 Output Contract 版本。

不持久化：

- Agent Session 完整对话；
- 模型隐藏推理；
- Backend Handle 和进程 PID；
- 原始流式事件；
- 未提升的临时工具轨迹和 Workspace 文件。

安全基线：

- 假设受控内网、可信用户和可信文件；
- V1 不默认加入 TLS、认证、一次性上传凭证、细粒度授权、上传前二次确认、本地路径白名单和 Agent 沙箱；
- Case ID、Attachment ID 和 Artifact ID 只是资源标识，不是授权凭证。
- Client Access Skill 取得 Shell 权限后可读取当前用户有权访问的本地文件，且本地路径、上传 URL 和命令可能进入 Agent 或 Shell 日志。

理由：

- 结构化业务状态足以支持诊断连续性，不需要保存完整对话。
- 安全能力不改变核心业务模型，但当前风险必须显式记录。

接受的代价：

- 无法依靠原始完整聊天重放模型过程。
- BlobStore 与 Repository 没有跨存储事务，Evidence / Artifact 发布需要幂等、补偿和孤立 Blob 清理。
- 当前基线不适合直接部署到不受控网络。

替代的旧决策：

- 原 OPT-023 中将诊断连续性委托给非持久化 Session 的部分。

复议条件：

- 出现审计、合规、身份、权限隔离或公网部署需求。

## 4. 已替代的关键旧决策

### 4.1 长期 Session 复用

旧选择：

> 同一个 Agent 跨多个 Job 保持 Session，并保留完整对话。

状态：**已替代**

新选择：

> Repository 保存系统真相，Case 和 DiagnosisState 保存当前诊断状态，Job 固定本轮历史输入；每个 Job 默认使用新的、可丢弃的 Session。

替代原因：

- 长期 Session 会持续积累噪声、旧假设和过期指令；
- Job 固定引用不能约束旧 Session 中的隐式输入；
- Repository 被声明为权威状态，但 Session 丢失又会使 Case 无法继续，形成双重状态源；
- Session 亲和性阻碍故障恢复和后续多实例演进；
- 同 Agent 与跨 Agent 使用了不同的交接标准。

新方案不是“每个 Job 保存完整对话快照”。它只在 Job 中复制执行所需的小型结构化 `context_snapshot`，大型内容继续使用不可变引用；当前 DiagnosisState 仍是唯一权威状态。

### 4.2 Case Session Registry（案例会话注册表）

旧选择：

- 按 Case、Agent Profile 和 Skill 定位长期 Session；
- 管理 Session 复用、空闲保留和回收。

状态：**已替代**

新选择：

- Runtime 每个 Job 创建并关闭 Session；
- V1 不需要 Session Registry、逻辑 Session Key、空闲回收和容量上限。

### 4.3 Session 子目录

旧选择：

- 每个长期 Session 在 Case Workspace 下使用独立子目录。

状态：**已替代**

新选择：

- 每个 Job 使用临时 Workspace；
- 需要保留的内容显式提升为 Evidence 或 Artifact。

### 4.4 重启后未完成 Case 作废

旧选择：

- 服务重启后 Session 不可恢复，因此未完成 Case 不可继续。

状态：**已替代**

新选择：

- Session 仍不恢复；
- Case 和 DiagnosisState 可以恢复；
- RUNNING Job 可以转为 `INTERRUPTED`；
- 用户持有 `case_id` 时可以显式继续。

### 4.5 Reviewer 仅作为远期目标

旧选择：

- Evidence Reviewer（证据复核智能体）不属于 V1。

状态：**已替代**

新选择：

- REVIEW Job 纳入 V1 基线；
- 仅在最终候选结论前触发；
- Reviewer 使用独立新 Session。

## 5. 暂缓能力

| 能力 | 当前状态 | 保留的演进边界 |
|---|---|---|
| General Code Agent（通用代码智能体） | 暂缓 | 接入 Typed Job、Runtime 和 Backend |
| Web（网页）客户端 | 暂缓 | 复用 Application Service 和 HTTP 文件接口 |
| Session Cache（会话缓存） | 暂缓 | 只能作为 Runtime 透明性能优化 |
| 独立 JobContextManifest（任务上下文清单） | 暂缓 | Job 字段明显膨胀后再提取 |
| JobAttempt（任务执行尝试） | 暂缓 | 需要自动重试和执行审计时增加 |
| Event Sourcing（事件溯源） | 暂缓 | 当前保存结构化状态和 Outcome 历史 |
| Transactional Outbox（事务发件箱） | 暂缓 | 引入可靠队列或多实例时增加 |
| 自动上下文摘要 | 暂缓 | 先使用固定规则和预算 |
| Vector Retrieval（向量检索） | 暂缓 | Evidence 规模显著增长时评估 |
| 并行 Diagnosis Branch（诊断分支） | 暂缓 | 需要分支状态和合并规则 |
| 动态 Skill Registry（技能注册中心） | 暂缓 | 当前 Skill 随服务版本发布 |
| 多实例 Worker 和高可用 | 暂缓 | 保留 Job、Repository 和 Backend 边界 |

## 6. 待详细设计

### 6.1 领域状态

- DiagnosisState 精确字段和条目版本规则；
- confirmed fact（确认事实）的证据门槛；
- Hypothesis（假设）的新增、支持、排除和替代规则；
- DiagnosisStateDelta 的幂等和冲突规则；
- `case_revision` 与 `DiagnosisState.revision` 的条件更新规则；
- 旧 `base_state_revision` Outcome 的处理。

### 6.2 Case 与 Job 状态机

- Case 全部状态转换；
- Job 全部状态转换；
- WAITING_INPUT、WAITING_ATTACHMENT、REVIEWING 和 INTERRUPTED 语义；
- 取消和失败时的清理行为。

### 6.3 Context Builder（上下文构建器）

- 角色化上下文 Schema；
- Token 预算；
- Evidence 片段选择；
- 必需内容超限时的错误和拆分规则；
- 大型附件的内容索引方式。

### 6.4 Runtime（运行时）与 Backend（执行后端）

- 一个 Job 内允许的模型和工具循环；
- Turn、Token、执行时间和文件规模上限；
- Backend 创建、执行、取消、关闭和错误协议；
- Job Workspace 布局和清理；
- 候选 Evidence / Artifact 的暂存、正式发布和 proposal key 映射。

### 6.5 可靠性

- 幂等键；
- Application Service 条件更新；
- 状态提交成功但分发失败的处理；
- `ResumeCase` 对 PENDING Job 的幂等重新分发；
- 自动 PENDING Job 扫描的后续版本边界；
- `runtime_epoch`、RUNNING Job 转 INTERRUPTED 的条件事务和迟到 Outcome；
- 外部副作用工具的恢复与补偿边界；
- 手动继续 Case 的具体命令。

### 6.6 接口与存储

- MCP 工具名和 DTO；
- HTTP 请求、响应和错误码；
- Repository 和 BlobStore 选型；
- 上传限制、清理和保留期限；
- BlobStore 发布成功但 Repository `READY` 提交失败时的补偿、对账和孤立 Blob 清理；
- 安全能力的后续规划。
