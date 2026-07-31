# Problem Locator V1 决策记录

状态：与 [V1 基线设计](v1-baseline-design.md) 及 [S00～S08 实施说明书](v1-specs/README.md)同步生效

更新时间：2026-07-31

## 1. 文档定位

本文记录 V1 当前有效的关键设计选择、理由、接受的代价、被替代的旧选择和复议条件。

本文不重复完整架构和接口说明。规范性要求依次以 [《Problem Locator V1 基线设计》](v1-baseline-design.md)、[S00 冻结合同与 S01～S07 模块说明书](v1-specs/README.md)以及 [S08 组合与总装说明书](v1-composition-spec.md)为准；本记录解释这些规范背后的选择，不另建一套实现合同。

状态定义：

| 状态 | 含义 |
|---|---|
| 已确认 | 已进入当前 V1 基线 |
| 暂缓 | 当前不实现，但保留演进边界 |
| 已替代 | 曾经采用，现已被新决策替换 |

## 2. 当前有效决策索引

| 编号 | 主题 | 当前选择 | 状态 |
|---|---|---|---|
| DR-001 | V1 范围与实现边界 | 当前仓库继续正式实现；单机、单进程、单 Uvicorn worker | 已确认 |
| DR-002 | 客户端接入与文件传输 | Agent Skill（智能体技能）+ Remote MCP（远程 MCP）传控制，HTTP 传文件 | 已确认 |
| DR-003 | 业务归一与写入边界 | Application Service（应用服务）单写入，Coordinator（协调器）纯决策 | 已确认 |
| DR-004 | 执行与等待模型 | 底层统一异步；全局 Agent Job 并发固定为 1；支持有限同步等待 | 已确认 |
| DR-005 | 权威诊断状态 | Case（诊断案例）+ DiagnosisState（诊断状态）保存全部跨 Job 必需信息 | 已确认 |
| DR-006 | Job 语义与固定输入 | Job（任务）是自包含业务单元，创建时固定小型结构化快照、引用和执行版本 | 已确认 |
| DR-007 | 上下文构造 | 固定 Job 输入；Router 128 KiB，Specialist / Reviewer 200 KiB | 已确认 |
| DR-008 | Session 与 Backend 生命周期 | 每 Job 新 Session；`CLAUDE_COMMAND` 除 logparse 保留环境外复用 `issue-locator` 语义 | 已确认 |
| DR-009 | Skill、工具与工作区 | `SKILL_DIR` 启动扫描；Job 级临时 Workspace；logparse 只经 job-scoped broker | 已确认 |
| DR-010 | 类型化编排与最终复核 | ROUTE / DIAGNOSE / REVIEW 任务推进；最终结论必须独立复核 | 已确认 |
| DR-011 | 并发与恢复边界 | 单 Case 单活跃 Job；finalized Outcome replay-before-interrupt | 已确认 |
| DR-012 | 数据、存储与安全基线 | `state.json` + 文件资源/执行记录；无数据库、无 Docker；受控内网 | 已确认 |
| DR-013 | 压缩日志处理权 | 只有 `logparse` 可处理压缩日志；一个 Case 首次解析一次，后续复用 | 已确认 |
| DR-014 | 未来开发任务执行约束 | S00～S08 未来开发任务统一使用 `gpt-5.6-sol` + `ultra` | 已确认 |

## 3. 当前有效决策

### DR-001：V1 范围与实现边界

**状态：已确认**

决策：

- V1 在当前 `D:\code\xiaodao` 仓库继续正式实现；不再新建正式代码仓库。
- 正式实现以当前已冻结设计为起点，不兼容旧 Demo 的内部结构和临时接口。
- V1 部署在受控内网，采用单机、单服务进程、单 Uvicorn worker 和单 Agent Job worker；`.instance.lock` 排斥同一 `DATA_ROOT` 的第二个服务进程。
- 工程栈固定为 Python 3.12、FastAPI/Uvicorn、官方 MCP Python SDK 和 Pydantic 2。
- V1 不使用 Docker、数据库、外部任务队列或多实例 Worker。
- V1 实现 Router Agent（路由智能体）、Specialist Agent（专项智能体）和 Reviewer Agent（复核智能体）。
- General Code Agent（通用代码智能体）暂缓。

理由：

- 先确认领域状态、Job 和上下文边界，避免被 Demo 结构反向限制。
- 单节点足以验证核心产品语义。
- Reviewer 复用现有 Typed Worker（类型化工作器）模式，不需要新增一套编排系统。

接受的代价：

- 首版没有多实例高可用。
- 需要在当前设计仓库内建立新的产品代码、合同、测试和运行目录，并明确隔离调研材料。

复议条件：

- 出现明确的多实例容量、故障接管或独立扩缩容需求；此时同时复议进程锁、单 worker、JSON 状态存储和 PostgreSQL 迁移方案。

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

- Application Service 是 Case、DiagnosisState、Job、JobOutcome、Evidence、Attachment 和 Artifact 元数据的唯一业务写入入口；正式资源 ID 也只能由它分配。
- Runtime 负责提供方结果解析与 Schema 校验。
- Application Service 负责幂等、归属、活跃 Job、版本和引用等技术校验。
- Diagnosis Coordinator 只根据 `CaseSnapshot + Validated Trigger` 计算 `TransitionPlan（状态转换计划）`，其中包含业务上接受的状态增量和可选 JobSpec。
- Application Service 只执行并持久化 TransitionPlan；下一 Job 的快照从计划应用后的目标 DiagnosisState 物化。
- Coordinator 不读写 StateRepository 或 ResourceStore、不提交 Dispatcher、不调用 Agent。
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
- 接口层的 `wait_seconds` 合法范围为 `0..30` 秒；等待只观察同一个 Job。
- 有限等待超时不取消、不重建、不重复提交 Job。
- V1 使用进程内 Job Dispatcher（任务分发器）和单 Agent Job worker；服务级全局 Agent Job 并发固定为 `1`，不同 Case 的 Job 也必须排队执行。
- 单次 Agent Job 默认 wall time 为 `30 minutes = 1800 seconds`，stdout 与 stderr 合计上限为 `64 MiB = 67108864 bytes`，Workspace 临时输出上限为 `1 GiB = 1073741824 bytes`。
- 超时、取消、stdout/stderr 超限或 Workspace 超限都必须终止完整子进程树，并产生类型化 ExecutionFailure；不得只结束父进程后留下子进程。
- 等待用户输入或附件期间，当前 Job 已经结束，不占用 Worker。

理由：

- 一套执行语义兼顾短任务体验和长任务稳定性。
- 避免分别维护同步和异步两套诊断引擎。
- 单 worker 与单文件状态存储、单写入者和首版可验证性保持一致。

接受的代价：

- 需要 Case 查询和明确的等待状态。
- 需要处理状态提交后分发失败。
- 不同 Case 不能并行运行 Agent Job，长任务会形成队列等待。

复议条件：

- 出现经过测量的队列延迟、外部队列、多实例 Worker 或通知推送需求；提高并发前必须先完成 PostgreSQL 和多实例一致性设计。

### DR-005：权威诊断状态

**状态：已确认**

决策：

- StateRepository 是结构化业务状态和诊断状态的唯一真相源；Case 及其 DiagnosisState 是当前诊断状态的唯一权威投影。FileResourceStore 保存由状态记录引用的文件字节，不构成第二套业务状态机。
- DiagnosisState 最少保存 ProblemSpec（问题规格）、带 provenance 的用户事实、确认事实、活跃假设、已排除假设、未决问题、待补要求、Evidence 引用和候选结论。
- Agent Session（智能体会话）、Context View（上下文视图）、模型摘要和 Workspace 都是可丢弃派生状态。
- 关闭所有 Session 后，系统仍必须能够创建下一 Job。

理由：

- 长期 Session 会积累旧假设、过期指令和无关工具输出。
- 如果 Session 丢失会导致 Case 无法继续，StateRepository 就不是真正权威状态。
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
  - Artifact 引用；
  - 必要的历史 Outcome 引用；
  - Agent Profile 版本；
  - ROUTE Job 可见的 Diagnosis Skill 版本集合；
  - Diagnosis Skill 版本；
  - Tool Bundle 版本；
  - Context Policy（上下文策略）版本；
  - Output Contract（输出契约）版本；
  - REVIEW Job 的候选结论 ID、版本和内容哈希；
  - `context_bytes`、`wall_time_seconds`、`stdout_stderr_bytes` 和 `workspace_bytes` 资源上限。
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
- Context Builder 发送给 Agent Backend 的最终上下文正文按 UTF-8 序列化字节计数：Router 上限为 `128 KiB = 131072 bytes`，Specialist 和 Reviewer 上限均为 `200 KiB = 204800 bytes`。
- 上述字节上限不包含 Workspace 中未内联的文件，也不包含执行后产生的 stdout/stderr，更不是单个 Attachment 的大小上限。
- V1 使用确定性优先级和字节预算，不实现模型自动摘要。
- 固定 Profile、完整 Skill 或 Skill 摘要、开放 requirements、输出 Schema，以及 Reviewer 的候选结论与固定复核目标属于不可截断的最低必需集合；必需集合超过角色预算时返回 `CONTEXT_LIMIT`，且不得调用 Backend。
- Context Builder 只读，不修改 StateRepository。

理由：

- 持久化历史和模型实际需要读取的内容不是同一个集合。
- 角色化、有限的输入可以降低噪声和旧状态复活。
- 规则式构建比首版引入自动摘要更容易验证。

接受的代价：

- 必须维护精确、可复现的选择顺序、UTF-8 字节清单和预算计算。
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
- Agent Backend 只通过 `CLAUDE_COMMAND` 启动外部 Agent 客户端；除 logparse 保留环境外，其解析和启动行为必须用 characterization tests 锁定为与 `issue-locator` 完全一致：按平台化 `shlex` 规则拆分，接受命令前置环境变量赋值，不使用 Shell，继承父进程环境并由前置赋值覆盖同名变量，在 Windows 使用同样的 command shim 解析行为。启动前大小写不敏感地移除原始 `LOGPARSE_REPO`、`LOGPARSE_CONFIG_PATH`、`LOGPARSE_PYTHON` 和既有 `PROBLEM_LOCATOR_LOGPARSE_*`；前置赋值若设置这些保留键则配置无效，固定 logparse Job 只加入本 Job broker session 返回的 endpoint/token。
- Prompt 只通过 stdin 提交。子进程成功退出后，Runtime 只从 Agent 原子发布的 `output/job_outcome.json` 读取业务结果；stdout/stderr 仅是受限执行日志，不是业务结果的回退来源。
- 外部模型认证由 `CLAUDE_COMMAND` 所调用的客户端及其继承环境负责。Problem Locator 不新增模型凭据存储，不把凭据写入 `state.json`、Job、日志或协议响应。
- 将来 Session 复用只能作为 Runtime 内部透明性能优化。

理由：

- 从根源消除跨 Job 长会话增长。
- Session 故障、回收和服务重启不再改变业务正确性。
- 不需要设计空闲 Session 回收、容量上限和亲和性。

接受的代价：

- 每个 Job 有 Session 创建成本。
- 可能减少特定提供方的 Prompt Cache（提示缓存）收益。
- Context Builder 必须提供充分的结构化输入。
- 运行环境必须预先配置可用的外部 Agent 客户端和认证；V1 不代管其账号生命周期。

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
- 服务只在组合启动时扫描配置的 `SKILL_DIR`；每个 Diagnosis Skill 以完整目录内容的规范化 SHA-256 固定版本，运行期间只读且不热更新。
- 启动配置支持 env-file 与进程环境，进程环境变量优先；`SKILL_DIR`、`CLAUDE_COMMAND` 和 logparse 路径均通过同一配置边界注入，敏感环境值不得回显。
- `SKILL_DIR` 发生变化后，只有重启服务形成新 Catalog 才能供新 Job 使用；已经创建的 Job 不得回退到当前最新版。
- Runtime 为每个 Job 创建临时 Workspace。
- READY Attachment 根据 Job 固定引用只读物化到该 Workspace。
- 需要跨 Job 保留的结果先通过 JobOutcome 提交为候选 Evidence 或 Artifact；文件写入 FileResourceStore 持久化暂存区，再由 Application Service 按 Coordinator 接受的 proposal key 发布正式记录。
- 固定 logparse Job 由 Runtime 打开唯一 job-scoped broker session；Agent 侧 stub 只能使用该 endpoint/token 和只读 WorkspaceInputManifest，不能接触 raw repo/config/python、任意 argv 或直接 CLI。broker 关闭时先使 token 失效，再终止并回收本 session 启动的全部子进程。
- Workspace 只提供路径归属、只读输入与并发正确性，不是恶意代码安全沙箱；V1 只允许运行受信任的 Skill、Agent 客户端和 logparse 资产。

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
- Agent 尚未产生业务载荷时的上下文超限、Backend 失败或工具超时令 `payload=null`，并写入 `error=ExecutionFailure（执行失败）`。
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
- 不同 Case 可以同时存在 PENDING Job，但 V1 全局只有一个 Agent Job worker，实际运行并发固定为 `1`。
- 同一 `DATA_ROOT` 只允许一个服务进程持有 `.instance.lock`；锁获取失败时第二个进程必须启动失败，不得以共享 `state.json` 的方式运行。
- 所有 Case 和活跃 Job 修改使用 `case_revision` 条件更新；JobOutcome 合并额外校验 `base_state_revision == DiagnosisState.revision`。
- 已知 `case_id` 的未完成 Case 可以从持久化 DiagnosisState 手动继续。
- `ResumeCase（恢复案例）`只处理已持久化的 `PENDING` Job，或已经持久化为 `INTERRUPTED` 且尚未被替代的 Job，不注入新输入。启动恢复先重放全部尚无 processing record 的 finalized Outcome；只有确实没有 finalized Outcome 的旧 `runtime_epoch（运行代次）` `RUNNING` Job 才独立转为 `INTERRUPTED`。等待资料的 Case 继续使用 `SubmitSupplement`。
- PENDING Job 在固定资产仍可加载时重新分发同一 Job 和快照；INTERRUPTED Job 创建新的替代 Job、快照和 `job_id`，并保持原业务阶段，REVIEW 不得降级为 DIAGNOSE。
- Case 已经持久化为 `INTERRUPTED` 且没有活跃 Job 时，`ResumeCase` 从最近一个尚未被替代的 INTERRUPTED Job 创建替代 Job；`replacement_for_job_id` 唯一约束保证幂等。
- Application Service 只做恢复技术校验；对已 INTERRUPTED Case，Coordinator 返回同阶段替代 JobSpec，Application Service 一次条件事务创建 replacement。旧 Job 中断属于单独的 `MARK_OLD_EPOCH_INTERRUPTED` 启动计划。
- 固定资产不可用时不替换最新版：PENDING Job 与 Case 进入 `FAILED`，已 INTERRUPTED Job 保持终态而 Case 进入 `FAILED`。
- 旧 Job 的首次迟到 Outcome 因活跃 Job与状态不匹配而保存为 `STALE` Outcome disposition；它只使 `case_revision + 1` 以记录审计，不改变 Case 状态、active Job、JobStatus 或 DiagnosisState revision。相同 Outcome 再到达为 DUPLICATE，任何 revision 都不变；`STALE` 不是 JobStatus。可恢复 Job 的工具默认只读或幂等。
- 服务启动时按 `{case_id,job_id}` 扫描恢复视图：先从 ExecutionRecordStore 读取所有未确认 finalized Outcome 并走正常提交路径；同进程或重启后的投递失败只重投同一 receipt，绝不再次运行 Agent。执行记录损坏或投递仍有瞬时错误时 recovery/readiness 保持失败，不得提前中断或 Resume。完成 replay 后，没有 finalized Outcome 的旧 `runtime_epoch` RUNNING Job 才经 Application Service 转为 `INTERRUPTED` 并清除活跃执行关系，仍为 PENDING 且固定资产可加载的 Job 最后自动重投同一 `job_id`。
- 启动恢复不为 INTERRUPTED Job 自动创建替代项；用户必须显式调用 `ResumeCase`，再由 Coordinator/Application Service 创建同阶段 replacement Job。
- V1 不实现多实例故障接管。

理由：

- 串行 Case 执行避免首版引入分支合并和冲突归并。
- Session 已经可丢弃，服务重启不再需要作废整个 Case。
- 手动继续与自动任务恢复是两个独立能力。

接受的代价：

- 同一 Case 不能并行探索多个假设。
- 不同 Case 的 Agent 执行也会串行排队。
- 服务重启后可能需要用户显式继续。

替代的旧决策：

- 原 OPT-009 中“服务重启后未完成 Case 必须重新创建”；
- 原 OPT-020 中依赖同一 Session 串行锁的部分。

复议条件：

- 明确需要并行探索、自动重调度或多实例高可用。

### DR-012：数据、存储与安全基线

**状态：已确认**

决策：

存储实现：

- V1 不使用数据库。`StateRepository` 的正式适配器是 JsonFileStateRepository，单个 `DATA_ROOT/state.json` 是结构化业务状态的权威文件。
- 状态写入必须使用同目录临时文件、落盘与原子替换合同；`state.json.prev` 只作为人工恢复材料。当前 `state.json` 损坏时必须停止并明确报错，不能静默自动回退到 `.prev`。
- `ResourceStore` 的正式适配器是 FileResourceStore，用于保存 READY Attachment、Evidence 文件、Artifact、持久化提案暂存文件和不可变 `LOGPARSE_RUN` 只读目录树；状态文件只保存稳定资源 ID、相对 storage key、大小和 SHA-256，不保存绝对路径或大型文件字节。
- 领域层和应用层只依赖 `StateRepository`、`ResourceStore`、`ExecutionRecordStore`、短临界区 `PublicationCommitGuard` 与进程内 `AttachmentUploadGuard` Port，不得直接读取 `state.json`、依赖文件布局或绕过 Port。
- V1 不使用 Docker，不提供 PostgreSQL、JSON/PostgreSQL 双写或在线迁移。

持久化：

- Case 和 DiagnosisState；
- Job 和 Typed JobOutcome；
- RouteDecision、DiagnosisOutcome 和 ReviewAssessment；
- Evidence 元数据、结构化内容和 Evidence 文件；
- Attachment 和 Artifact 元数据与文件；
- 实际使用的 Profile、Skill、Tool Bundle、Context Policy 和 Output Contract 版本。
- finalized `job.json`、`job_outcome.json` 执行记录与 OutcomeProcessingRecord；finalized Outcome 是 durable outbox，processing record 是投递确认。

不持久化：

- Agent Session 完整对话；
- 模型隐藏推理；
- Backend Handle 和进程 PID；
- 原始流式事件；
- 未提升的临时工具轨迹和 Workspace 文件。

资源发布与清理：

- FileResourceStore 与 `state.json` 没有跨存储事务。Application Service 按幂等 proposal key 先发布不可变资源，再条件提交状态；Outcome proposal 的正式 ID 由 installation/case/outcome/proposal key 确定性派生，同一 Outcome 跨重启可校验并采用同一正式目标。资源发布后状态提交失败不能返回业务成功，只允许从 finalized Outcome 重放；预发布 next Job 必须采用旧 `job.json` 的逐字 RuntimeBindings 与创建时间，不得因 Catalog 漂移改写。
- 单个 Attachment 上限为 `2.5 GiB = 2684354560 bytes`；单个 Case 的正式文件总量上限为 `5 GiB = 5368709120 bytes`。Case 用量按正式 resources 根下唯一、非 quarantine 的 storage key 计数，包含 state 引用、未确认 outbox 目标与普通 orphan；全批目标必须在同一 publication lease 内、移动任一 stage 前原子校验，超过限制时零对象发布。
- 同一 attachment_id 的流式上传由唯一进程内 guard 串行化并持有到发布/状态提交结束；网络流期间不持有 publication lease。流结束后在短 lease 内重新读取权威 snapshot，再做幂等/状态/容量检查、发布和 commit；generation 冲突只重算该 post-stage 阶段，绝不重读已经消费的 body。发布成功但 commit 失败后，相同 size/hash 的重试采用既有目标，不同 size/hash 固定为 `IDEMPOTENCY_CONFLICT`，不得覆盖。
- 上传临时文件、未接受的提案暂存文件和已无活跃执行引用的临时 Workspace 按 `24 hours = 86400 seconds` 保留期限清理。
- 已发布但未登记进权威状态的 orphan 资源按 `7 days = 604800 seconds` 保留期限清理；发布/采用到 state commit 与清理共享同一可重入协调锁。清理必须在锁内再次确认没有任何权威引用并原子移动到不可采用的 quarantine，锁外才删除，不能留下检查/删除 TOCTOU。
- finalized 且尚无 OutcomeProcessingRecord 的 Outcome 保护其 staged refs、确定性正式目标和预发布 next Job，不受 24 小时/7 日规则；损坏的未确认执行记录使 readiness 失败并暂停破坏性清理，禁止回退到当前 Catalog 或重跑 Agent。
- 已登记且仍由 Case、Job、Outcome、Evidence 或 Artifact 引用的正式资源不受上述临时清理期限影响。
- 清理器在启动后运行，并每 `24 hours` 串行运行一次；每次删除前必须依据当前 StateRepository 状态再次确认无引用。
- V1 不自动删除 Case 的 Attachment、Evidence、Artifact、Job 文件、执行日志或已接受的 `LOGPARSE_RUN`，也不提供在线删除命令；未来若增加管理员清理，只能显式删除整个已终止 Case 的正式数据。

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
- JSON 状态和文件资源之间没有分布式事务，Evidence / Artifact 发布需要幂等、补偿、对账和 orphan 清理。
- 单文件状态存储不支持多进程写入、横向扩容或数据库级查询。
- 当前基线不适合直接部署到不受控网络。

替代的旧决策：

- 原 OPT-023 中将诊断连续性委托给非持久化 Session 的部分。

复议条件：

- 出现审计、合规、身份、权限隔离或公网部署需求时复议安全基线。
- 出现第二个服务实例、高可用要求、`state.json` 接近 `16 MiB`、历史 Case 接近 `500` 个或写入延迟明显上升中的任一条件时，优先启动 PostgreSQL 升级设计。
- PostgreSQL 迁移采用停机离线导出/导入，不做双写；首先接管结构化业务状态，大文件可以继续由 FileResourceStore 保存。迁移不得改变 ID、枚举、UTC 时间、revision、Outcome Schema、资源 ID 和 SHA-256 语义。

### DR-013：压缩日志处理权与解析复用

**状态：已确认**

决策：

- `logparse` 是 V1 唯一有权处理压缩日志包的能力。Runtime、Context Builder、Application Service 和其他 Skill 不得枚举压缩包成员、解包、扫描压缩日志或以项目内自制 grep 替代 `logparse`。
- S07 提供并版本化 `logparse` Diagnosis Skill；Skill 生成器版本固定为 `2.0.0`。
- 每个固定 logparse Job 只允许 Runtime 创建一个 job-scoped broker session。Agent stub 只发送受限 request 与 Workspace 相对路径；broker 从只读 manifest 取得 Job 固定 `logparse_product`，持有实际 repo/config/python，拒绝 Agent 自报 product、跨 Job token、第二次 parse、任意 argv 和 direct-CLI 降级，关闭/取消时回收全部子进程。
- Specialist 首次分析日志时只执行一次真实 `logparse parse`，读取其 `parse_manifest.json`，再在解析结果上执行 `mech-target-logs` 等受支持的定向操作。
- 首次解析产生的目录必须通过 JobOutcome 作为内部 `LOGPARSE_RUN` Artifact 提案，并在被 Coordinator 接受后由 Application Service 以不可变只读目录树发布到 FileResourceStore。该 Artifact 固定源 Attachment、解析参数、工具版本、manifest 和内容哈希。
- 如果首次分析需要用户补充参数，当前 Job 结束并保存 Evidence 与 `LOGPARSE_RUN`；补参创建的新 DIAGNOSE Job 固定引用该 Artifact，Runtime 只读物化已保存解析目录，不得重新解包或再次执行 `logparse parse`。
- 一旦 `LOGPARSE_RUN` 已被同一次业务提交接受并形成正式 Artifact，后续服务重启、Session 丢失和同阶段 Job 替代都必须复用它，不得再次 parse；S08 的无故障 RPC 超时主场景必须证明从首次分析到最终查询的总 parse 次数严格为 `1`。
- 如果 parse 已开始但 Runtime 尚未 finalize 合法规范 Outcome 就因进程退出、取消、Backend 超时或输出非法而失败，恢复遵循 ResumeCase 的至少一次执行语义；替代 Job 可以重新 parse，累计调用次数可能增加，但不得伪造不存在的 `LOGPARSE_RUN`。一旦规范 Outcome finalized，即使 Artifact 尚未写入 state 或 state commit 失败，也只能 submission replay 同一 receipt 并采用确定性正式目标，禁止再运行 Agent 或再次 parse。V1 不承诺 finalized Outcome 之前所有故障窗口中的工具 exactly-once。

理由：

- 压缩日志处理涉及归档安全、确定性解析、工具版本和跨 Job 复用，必须只有一个可测试的权威入口。
- 保存结构化解析产物可以在不保留 Agent Session 的前提下继续诊断，并避免重复消耗时间和磁盘。

接受的代价：

- `logparse` 不可用、manifest 非法或固定解析产物丢失时必须明确失败，不能静默降级到另一套解包或文本扫描路径。
- FileResourceStore 需要支持目录型 Artifact 的不可变目录树发布、规范化清单哈希、只读物化和清理引用检查。

复议条件：

- 只有在形成新的替代决策、保持相同归档安全与可重现合同并补齐 S07/S08 验收后，才能增加第二种压缩日志处理器或改变“一次解析、后续复用”规则。

### DR-014：未来开发任务执行约束

**状态：已确认**

决策：

- S00 合同冻结任务以及其后的 S01～S08 未来开发任务统一使用 `gpt-5.6-sol`，`reasoning_effort=ultra`。
- 一个说明书对应一个独立 Codex 任务、`codex/` 前缀分支和独立 worktree；任务只写 [S00～S08 责任白名单](v1-specs/README.md)内的文件，并按 [S08](v1-composition-spec.md)规定的顺序交接、合并和验收。
- 上述模型与推理强度只约束未来的代码开发任务，不是 Problem Locator 运行时模型选择，不得写入 Job、Agent Profile 或 `CLAUDE_COMMAND` Backend 合同。
- 运行时 Agent 仍完全由 `CLAUDE_COMMAND`、外部客户端配置与其认证环境决定；未来开发任务的 Codex 模型不得成为产品运行依赖。

理由：

- 固定高能力开发执行配置可降低跨切片实现质量漂移，并使交接与返工结果具有共同预期。
- 将“开发 Codex 模型”和“产品运行时 Agent Backend”明确分离，避免把开发基础设施误写成产品合同。

接受的代价：

- 未来开发任务的成本与执行时间较高，且需要目标模型与 `ultra` 推理强度可用。
- 各切片必须经过 S00 合同冻结和 S08 串行总装，不能为了速度在同一 worktree 越界并行修改。

复议条件：

- 目标模型或推理强度不可用，或有同等质量的后继配置时，可以更新未来任务执行配置；该变化不允许修改运行时 `CLAUDE_COMMAND`、Session、状态或认证语义。

## 4. 已替代的关键旧决策

### 4.1 长期 Session 复用

旧选择：

> 同一个 Agent 跨多个 Job 保持 Session，并保留完整对话。

状态：**已替代**

新选择：

> StateRepository 保存结构化系统真相，FileResourceStore 保存被权威状态引用的文件字节；Case 和 DiagnosisState 保存当前诊断状态，Job 固定本轮历史输入；每个 Job 默认使用新的、可丢弃的 Session。

替代原因：

- 长期 Session 会持续积累噪声、旧假设和过期指令；
- Job 固定引用不能约束旧 Session 中的隐式输入；
- StateRepository 被声明为权威状态，但 Session 丢失又会使 Case 无法继续，形成双重状态源；
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

### 4.6 当前仓库只做设计、实现另建仓库

旧选择：

- 当前仓库只保存设计和调研；
- 正式实现另建代码仓库。

状态：**已替代**

新选择：

- 正式 V1 继续在当前 `D:\code\xiaodao` 仓库实现；
- S00～S08 使用责任白名单、独立分支和独立 worktree 隔离并行开发，不再通过迁移到新仓库隔离。

### 4.7 未定的状态与文件资源存储产品选型

旧选择：

- 状态仓库和文件字节存储的具体产品与布局留待后续决定。

状态：**已替代**

新选择：

- V1 使用单个 `DATA_ROOT/state.json` 的 JsonFileStateRepository 和 FileResourceStore；
- 不使用数据库、Docker 或抽象但未落地的文件对象存储产品；
- 领域层与应用层仍通过 `StateRepository` / `ResourceStore` Port 保留未来 PostgreSQL 迁移边界。

### 4.8 不同 Case 有界并发

旧选择：

- 同一 Case 串行，不同 Case 可由有界 Worker Pool 并发。

状态：**已替代**

新选择：

- V1 只有一个 Agent Job worker，全局执行并发固定为 `1`；
- 不同 Case 可以排队，但不能同时运行 Agent Job。

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
| 动态 Skill Registry（技能注册中心） | 暂缓 | 当前只在服务启动时扫描 `SKILL_DIR`，运行期不热更新 |
| 多实例 Worker 和高可用 | 暂缓 | 保留 Job、StateRepository、ResourceStore 和 Backend Port；先完成 PostgreSQL 与多实例一致性设计 |
| PostgreSQL StateRepository | 暂缓 | 达到 DR-012 迁移触发条件后，停机离线迁移；不做 JSON/数据库双写 |
| Docker / 容器化部署 | 暂缓 | V1 直接运行单个 Python 服务进程；出现明确交付或编排需求时再增加 |

## 6. 冻结实施规格落点

原“待后续细化”的字段、状态、协议、存储和验收事项已经分配到 S00～S08，不再作为无所有者的悬空清单保留。规范路径和权威范围如下：

| 编号 | 冻结说明书 | 本记录对应范围 |
|---|---|---|
| S00 | [合同冻结与公共测试](v1-specs/S00-contract-freeze.md) | 公共枚举、DTO、Port、错误码、Schema、revision 矩阵、限制常量和 fixture |
| S01 | [领域模型与 Coordinator](v1-specs/S01-domain-coordinator.md) | Case、DiagnosisState、状态机、不变量、TransitionPlan 和复核门禁 |
| S02 | [JSON 与文件资源存储](v1-specs/S02-json-resource-storage.md) | `state.json`、`.instance.lock`、原子写、FileResourceStore、校验、备份、清理和 PostgreSQL 导出边界 |
| S03 | [Application Service](v1-specs/S03-application-service.md) | 单写入、幂等、引用校验、资源提案、条件提交和跨存储补偿 |
| S04 | [Runtime、Context Builder 与 Backend](v1-specs/S04-runtime-context-backend.md) | 角色字节预算、`SKILL_DIR`、Workspace、`CLAUDE_COMMAND`、结果文件、超时和进程树终止 |
| S05 | [调度与恢复](v1-specs/S05-scheduler-recovery.md) | 单 worker、认领、取消、中断、恢复、替代 Job、`runtime_epoch` 和 STALE disposition |
| S06 | [MCP、HTTP 与 CLI](v1-specs/S06-mcp-http-cli.md) | Remote MCP、文件 HTTP、有限等待、配置、认证边界、Client Access Skill、协议错误和 CLI |
| S07 | [Skill 与 logparse](v1-specs/S07-skill-logparse.md) | Diagnosis Skill 生成器、压缩日志唯一处理权、`LOGPARSE_RUN` 与单次解析复用 |
| S08 | [组合与总装](v1-composition-spec.md) | 依赖批次、责任白名单、合并顺序、R01～R14、接缝测试、返工路由和发布验收 |

规范优先级为 V1 基线、S00 冻结合同、S01～S07 模块说明书、S08 总装说明书。下一级不得覆盖上一级；发现合同无法表达已确认决策时必须停止相关实现并走 S00 合同变更流程，不能在模块内部发明私有字段、第二套 DTO 或兼容分支。
