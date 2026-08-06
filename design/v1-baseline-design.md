# Problem Locator V1 基线设计

状态：V1 产品与架构基线；详细合同已分册冻结

基线日期：2026-07-31

适用范围：Problem Locator（问题定位系统）正式版本

## 1. 文档定位

本文统一定义 Problem Locator V1（问题定位系统第一版）的产品边界、静态架构、模块职责、领域状态、Job（任务）模型、上下文策略、Agent Session（智能体会话）策略、文件接入、执行流程、持久化范围和可靠性边界。

本文是 V1 产品语义与架构不变量的最高规范来源。公共机器合同以 [S00](v1-specs/S00-contract-freeze.md) 为权威，组件内部行为以 [S01～S07](v1-specs/README.md) 为权威，未来组合规则以 [S08](v1-composition-spec.md) 为权威；后一级不得覆盖前一级。

设计理由和被替代方案记录在[《V1 决策记录》](v1-decision-record.md)中。业界调研位于 `../doc/`，仅提供参考，不自动构成设计要求。

### 1.1 核心基线

V1 的上下文与状态原则可以归纳为：

> Case（诊断案例）有状态，Job（任务）自包含，Agent Session（智能体会话）可丢弃。

具体含义：

- Case（诊断案例）及其 DiagnosisState（诊断状态）保存所有跨 Job 必须延续的诊断信息。
- Job（任务）在创建时固定本轮目标、状态版本、证据、附件、Agent Profile（智能体配置）和 Diagnosis Skill（诊断技能）版本。
- Context Builder（上下文构建器）只根据 Job 固定的引用构造本轮输入。
- 每个 Agent Job 默认创建新的 Agent Session；Session 结束或丢失不影响后续 Job 正确执行。
- Agent（智能体）只原子写入 AgentJobOutcome（智能体任务结果草稿）和 DiagnosisStateDelta（诊断状态增量）提案；Runtime 规范化为 Typed JobOutcome，二者都不能直接修改 Case。

### 1.2 本文不提前引入的复杂机制

V1 不把以下机制画成核心组件，也不要求首版实现：

- Event Sourcing（事件溯源）平台；
- Transactional Outbox（事务发件箱）；
- 独立 JobAttempt（任务执行尝试）实体；
- 长期 Session Cache（会话缓存）；
- 自动上下文摘要或多级压缩；
- Vector Database（向量数据库）；
- 并行 Diagnosis Branch（诊断分支）；
- 外部消息队列和多实例 Worker（工作器）集群。

这些能力均可在不改变本基线业务语义的前提下后续增加。

## 2. V1 目标与范围

### 2.1 目标

- 为 CLI（命令行）用户提供多轮问题定位能力。
- 允许用户补充结构化信息、日志和其他附件。
- 使用 Router Agent（路由智能体）选择一个已发布的 Diagnosis Skill。
- 使用 Specialist Agent（专项智能体）执行目标诊断。
- 在最终结论前使用 Reviewer Agent（复核智能体）核对结论与证据。
- 将 Case、诊断状态、Job、结果、证据和产物持久化。
- 保证关闭全部 Agent Session 后，仍可根据持久化状态继续未完成 Case。
- 保持模块边界可以后续演进到多实例，但 V1 固定为单机、单服务进程、单 Uvicorn worker、Agent Job 并发 1。

### 2.2 V1 非目标

- 不实现 General Code Agent（通用代码智能体）。
- 不实现并行驱动多个 Agent 探索同一个 Case。
- 不实现长期复用 Agent Session。
- 不实现自动语义摘要、向量检索或长期 Agent Memory（智能体记忆）。
- 不实现外部任务队列、多实例高可用或自动故障接管。
- 不实现动态 Skill Registry（技能注册中心）和运行期热更新。
- 不实现 Web（网页）管理或上传页面，但保留复用服务端接口的边界。
- 不实现本地 Case Locator（案例定位器）或自动查找用户历史 Case。
- 不另建正式代码仓库；后续实现、测试和 Skill 资产仍写入当前仓库。
- 不使用数据库或 Docker；结构化状态使用本地 `state.json`，文件使用 FileResourceStore。

## 3. 核心不变量

以下规则属于 V1 正确性基线，不是性能优化：

1. **Repository（仓库）是唯一诊断真相源。**
   Repository 中的 Case、DiagnosisState、Job、JobOutcome、Evidence（证据）和文件元数据共同构成系统可恢复状态；Case + DiagnosisState 是“当前诊断状态”的唯一权威投影，Job 的 `context_snapshot` 只是不可变历史输入。

2. **Application Service（应用服务）是唯一业务写入入口。**
   Coordinator（协调器）、Dispatcher（分发器）、Worker、Runtime（运行时）、Context Builder 和 Agent Backend（智能体执行后端）均不能直接修改 Case。

3. **Diagnosis Coordinator（诊断协调器）是确定性纯决策组件。**
   它只根据 `CaseSnapshot + Trigger` 计算状态变化和下一 Job 规格，不读写 Repository，也不执行 Agent。

4. **Job 输入在创建时固定。**
   Runtime 不得用执行时的最新 Case 内容静默替换 Job 所引用的状态版本、证据、附件或 Skill 版本。

5. **Agent Session 不承担跨 Job 状态。**
   同一种 Agent 连续执行多个 Job 时，也默认分别创建新的 Session。

6. **Agent 输出是提案，不是事实。**
   Runtime 负责 Schema（结构）解析；Application Service 负责幂等、归属、版本和引用等技术校验；Coordinator 负责决定业务上接受哪些 DiagnosisStateDelta；Application Service 只执行并持久化 TransitionPlan。

7. **同一 Case 同时只运行一个活跃 Job。**
   不同 Case 可以同时排队，但 V1 的 Agent Job 全局执行并发固定为 1。

8. **文件字节与结构化业务状态分离。**
   StateRepository 保存结构化业务状态和文件元数据；FileResourceStore 保存 Attachment、Evidence 与 Artifact 的文件或目录资源。

9. **最终结论必须经过独立复核。**
   Reviewer 使用新的 Session，只读取候选结论、当前问题、结构化诊断状态和相关证据，不继承 Specialist 的完整对话。

10. **上下文超限不得静默丢弃必需信息。**
    如果 Job 所需的最低上下文仍超过预算，Job 应返回明确错误或拆分建议，而不是悄悄省略关键约束或证据。

11. **下一 Job 固定转换后的状态。**
    同一业务提交中先得到目标 DiagnosisState，再从目标状态物化下一 Job 的 `context_snapshot`；不得从更新前状态生成快照。

12. **Job 固定引用不可原地修改。**
    Attachment、Evidence、Artifact、JobOutcome 和版本化运行资产发生内容变化时必须创建新 ID 或新版本。

13. **迟到和重复结果不能改变当前状态。**
    JobOutcome 必须同时通过活跃 Job、Job 状态、基础诊断版本和幂等校验；旧 Job 的迟到结果保存为过期记录但不得合并。

## 4. 目标静态架构

```mermaid
flowchart TB
    subgraph ClientSide["Client Side（客户端）"]
        User["User（用户）"]
        CLI["Agent CLI（智能体命令行）"]
        ClientSkill["Client Access Skill（客户端接入技能）"]

        User --> CLI
        CLI --> ClientSkill
    end

    subgraph Service["Problem Diagnosis Service（问题定位服务）"]
        MCP["Remote MCP Adapter（远程 MCP 适配器）"]
        HTTP["HTTP File Adapter（HTTP 文件适配器）"]

        App["Application Service（应用服务）<br/>唯一业务状态写入入口"]

        Coordinator["Diagnosis Coordinator（诊断协调器）<br/>根据 CaseSnapshot（案例快照）与 Trigger（触发事件）<br/>决定状态转换与下一 Job（任务）"]

        subgraph Persistence["Persistence（持久化层）"]
            Repository["Case Repository（案例仓库）<br/>Case（诊断案例）· DiagnosisState（诊断状态）<br/>Job（任务）· JobOutcome（任务结果）<br/>Evidence / Attachment / Artifact Metadata（文件元数据）"]

            FileResourceStore["FileResourceStore（文件资源存储）<br/>Attachment（附件）· Evidence File（证据文件）<br/>Artifact（产物）"]
        end

        Dispatcher["Job Dispatcher（任务分发器）"]

        subgraph Workers["Typed Workers（类型化工作器）"]
            RouterWorker["Routing Worker（路由工作器）<br/>Router Agent（路由智能体）"]

            DiagnosisWorker["Diagnosis Worker（诊断工作器）<br/>Specialist Agent（专项智能体）"]

            ReviewWorker["Review Worker（复核工作器）<br/>Reviewer Agent（复核智能体）<br/>仅在最终结论前执行"]
        end

        ContextBuilder["Context Builder（上下文构建器）<br/>根据 Job 固定引用<br/>构造有界模型输入"]

        Runtime["Diagnosis Runtime（诊断运行时）<br/>装配 Profile（配置）· Skill（技能）<br/>Tool Bundle（工具包）· Workspace（工作区）"]

        Catalog["Versioned Catalog（版本化目录）<br/>Profile（配置）· Skill（技能）· Tool Bundle（工具包）<br/>Context Policy（上下文策略）· Output Contract（输出契约）"]

        Backend["Agent Backend（智能体执行后端）"]

        Session["Agent Session（智能体会话）<br/>每个 Job 默认新建<br/>Job 结束后可以丢弃"]

        Outcome["Typed JobOutcome（类型化任务结果）<br/>RouteDecision（路由决定）· DiagnosisOutcome（诊断结果）<br/>ReviewAssessment（复核结果）· ExecutionFailure（执行失败）"]

        MCP --> App
        HTTP --> App

        App -->|"CaseSnapshot + Trigger<br/>案例快照 + 触发事件"| Coordinator
        Coordinator -->|"TransitionPlan + JobSpec<br/>状态转换计划 + 任务规格"| App

        App -->|"读写业务状态"| Repository
        App -->|"保存和读取文件"| FileResourceStore
        App -->|"状态提交后分发 Job"| Dispatcher

        Dispatcher --> RouterWorker
        Dispatcher --> DiagnosisWorker
        Dispatcher -.->|"最终结论需要复核"| ReviewWorker

        RouterWorker --> Runtime
        DiagnosisWorker --> Runtime
        ReviewWorker --> Runtime

        Runtime -->|"请求本 Job 有界输入"| ContextBuilder
        ContextBuilder -->|"读取 Job 的 context_snapshot<br/>上下文快照和固定引用"| Repository
        ContextBuilder -->|"读取相关附件和证据"| FileResourceStore
        ContextBuilder -->|"返回 Bounded Context（有界上下文）"| Runtime
        Catalog --> Runtime
        Runtime -->|"创建并驱动"| Backend
        Backend -->|"为本 Job 新建"| Session
        Session -->|"模型与工具结果"| Backend
        Backend -->|"原始执行结果"| Runtime
        Runtime -->|"解析并校验 Schema"| Outcome
        Outcome -->|"经对应 Worker 回送结果提案"| App
    end

    ClientSkill -->|"Structured Commands（结构化命令）"| MCP
    ClientSkill -->|"Upload / Download（上传 / 下载）"| HTTP
```

## 5. 模块职责

### 5.1 Client Access Skill（客户端接入技能）

- 解释服务端 MCP 工具的能力和结构化结果。
- 帮助 CLI Agent 构造创建 Case、提交补充信息、查询状态和取消 Case 的调用。
- 使用本地 `curl` 或等价能力通过 HTTP 上传 Attachment、下载 Artifact。
- 在用户交互中明确展示 `case_id`，便于客户端上下文丢失后手动继续。
- 不在本地保存或覆盖服务端权威业务状态。

### 5.2 Remote MCP Adapter（远程 MCP 适配器）

- 承载控制命令、小型结构化输入和结构化结果。
- 将协议请求转换为 Application Service 的应用命令。
- 不直接操作 StateRepository、FileResourceStore、Dispatcher 或 Agent。
- 不实现独立于 Application Service 的 Case 状态机。

### 5.3 HTTP File Adapter（HTTP 文件适配器）

- 承载 Attachment 准备、文件字节上传和 Artifact 下载。
- 将上传、发布和下载请求转换为 Application Service 的应用命令。
- 不根据上传来源区分 CLI、Web 或其他客户端。
- 不实现第二套 Attachment 或 Artifact 业务规则。

### 5.4 Application Service（应用服务）

- 接收外部应用命令和内部 JobOutcome。
- 读取当前 Case 和 DiagnosisState。
- 执行幂等、资源归属、活跃 Job、Job 状态、`base_state_revision` 和 Evidence 引用等技术校验。
- 将通过技术校验的 Trigger 与 CaseSnapshot 交给 Diagnosis Coordinator。
- 执行 Coordinator 返回的 TransitionPlan：保存输入或 Outcome、登记已接受的 Evidence / Artifact、更新 Case 和 DiagnosisState、结束当前 Job，并创建可选的下一 Job。
- 对包含文件的候选 Evidence / Artifact，先完成 FileResourceStore 内发布，再在一次 StateRepository 提交中执行 TransitionPlan；PublicationCommitGuard 持有同一存储协调锁覆盖“正式发布/采用→state commit”，避免孤立清理在两者之间删除对象。“原子”仍只描述结构化状态提交，不表示两个存储之间存在分布式事务。
- TransitionPlan 的 JobSpec 只携带目标语义 revision 和尚待解析的资源/候选 binding，不预造含 proposal key 的 ContextSnapshot。Application Service 先用确定性 ID 正式化 proposal、构造目标 DiagnosisState，再调用 S01 公共纯 projector 生成 `context_snapshot`，并与状态更新和 Job 创建在同一次业务提交中固定。
- 在状态提交成功后将已创建的 Job 交给 Dispatcher。
- 对外返回当前 Case 状态，不直接执行耗时诊断。

### 5.5 Diagnosis Coordinator（诊断协调器）

- 输入为 `CaseSnapshot + Trigger`。
- 输入中的 JobOutcome 已由 Application Service 完成结构、归属、版本和引用等技术校验。
- 输出为确定性的 `TransitionPlan（状态转换计划）`，其中包含业务上接受的 `accepted_state_delta（已接受状态增量）`、目标 Case 状态、selected skill/CaseFailure/Candidate 的显式 mutation，以及可选的下一 Job 规格。
- 决定何时路由、诊断、等待用户、等待附件、复核、完成、失败或取消。
- 不读取 Repository，不写入业务状态，不提交 Dispatcher，不调用 Agent。
- 不承担 Router Agent 的语义路由能力。

### 5.6 Case Repository（案例仓库）

持久化：

- Case；
- DiagnosisState；
- Job；
- JobOutcome；
- RouteDecision；
- DiagnosisOutcome；
- ReviewAssessment；
- Evidence 元数据与结构化内容；
- Attachment 和 Artifact 元数据；
- 实际使用的 Agent Profile、Diagnosis Skill、Tool Bundle、Context Policy 和 Output Contract 版本。

Repository 不保存文件字节，也不把 Agent Session、临时 Workspace 或模型隐藏推理当作业务状态。

### 5.7 Job Dispatcher（任务分发器）

- 根据 Job 类型选择对应 Worker。
- 只分发已经持久化的 Job。
- Worker 通过 Application Service 请求条件认领 `PENDING` Job；Application Service 将其更新为 `RUNNING`。同一 `job_id` 的重复分发最多只有一次认领成功。
- V1 使用进程内队列，执行并发上限固定为 1。
- 不修改 Case，不创建后续 Job。
- 分发失败时保留 Job 的 `PENDING` 状态；服务启动会自动重投同一 Job，显式 `ResumeCase` 也可以主动唤醒它。

### 5.8 Typed Worker（类型化工作器）

V1 包括：

- `ROUTE`：Routing Worker 调用 Router Agent；
- `DIAGNOSE`：Diagnosis Worker 调用 Specialist Agent；
- `REVIEW`：Review Worker 调用 Reviewer Agent。

Worker 负责把类型化 Job 交给共享 Runtime，并把 Runtime 返回的 Typed JobOutcome 回送 Application Service。Worker 不直接推进 Case 状态。

### 5.9 Context Builder（上下文构建器）

- 根据 Job 中固定的上下文引用读取指定版本数据。
- 根据 Agent 角色构造不同的有限上下文。
- 把 `{job_id,job_type,goal,base_state_revision}` 作为不可截断 `JOB_INSTRUCTION` Canonical JSON section，使 Agent 明确获得本 Job 目标。
- 将 Job 固定的规范 previous outcomes 按数组顺序作为不可截断的 `PREVIOUS_OUTCOME` sections 注入，使补参 continuation 和复核返工不依赖旧 Session。
- 把逐字等于只读 `inputs/manifest.json` 的 `RESOURCE_MANIFEST` 作为最低必需 section；先为它和全部其他必需 section 预留预算，再按 Job 顺序选择可选 Evidence。
- 对重复状态去重，排除被替代内容和无关历史。
- 将大型日志保留在 FileResourceStore 或 Evidence 中，只按 Job 固定 locator（定位信息）或固定 `context_policy_ref` 的确定性规则选择片段。
- 执行 UTF-8 字节预算检查。
- 不修改 Repository，不生成业务状态。

### 5.10 Diagnosis Runtime（诊断运行时）

- 加载 Agent Profile、Diagnosis Skill 和 Tool Bundle。
- 通过 Context Builder 获得本 Job 的模型输入。
- 为每个 Job 创建临时 Workspace。
- 按公共 WorkspaceInputManifest Schema 生成只读 `inputs/manifest.json`，其中 `logparse_product` 来自 Job 固定 Skill，并把同一 Canonical JSON 用作 RESOURCE_MANIFEST section；Skill 与 logparse broker 不扫描猜测路径。
- 固定 logparse Job 启动服务侧 job-scoped broker；Agent 只得到 endpoint/token，原始 `LOGPARSE_REPO/CONFIG_PATH/PYTHON` 从 Agent 环境剥离，broker close 同步回收其子进程。
- 调用 Agent Backend 创建新的物理 Agent Session。
- 驱动本 Job 内必要的模型与工具循环。
- 校验 Agent 写入的 AgentJobOutcome；USER_RESULT 在 stage 前必须逐字通过公共 UserResultPayload Schema 并匹配 Candidate，LOGPARSE_RUN 的 manifest 路径必须从实际 tree 重建；随后把 Workspace 相对路径引用的 Draft 内容写入持久化暂存区，再形成不含 Workspace 路径的 Typed JobOutcome。
- 系统失败 Outcome 的时间和 ID只来自注入的 Clock/IdGenerator；parse-once claim 使用公共 LogparseParseClaim Schema。
- Agent stdout/stderr 进入执行日志前对当前 broker endpoint/token 做跨分块精确脱敏，资源限额按原始字节计算。
- Job 结束后关闭 Session；规范 JobOutcome 中需要保留的内容以带 `StagedResourceRef` 的候选 Evidence 或 Artifact 交给 Application Service 决定是否正式发布。

### 5.11 Agent Backend（智能体执行后端）

- 创建、调用和关闭物理 Agent Session。
- 标准化不同提供方的响应、错误和中断。
- 对 Runtime 隐藏物理 Session Handle（会话句柄）、进程 PID 和具体 SDK。
- 以 `CLAUDE_COMMAND` 单字符串启动外部客户端：除 logparse 保留变量外复用 `issue-locator` 的平台化 `shlex`、可选前置环境赋值、继承环境、不使用 Shell、Windows shim 解析和 argv 不隐式追加参数语义。启动前大小写不敏感地移除原始 `LOGPARSE_REPO`、`LOGPARSE_CONFIG_PATH`、`LOGPARSE_PYTHON` 与既有 `PROBLEM_LOCATOR_LOGPARSE_*`；命令前置赋值不得重设这些键，固定 logparse Job 只加入本 Job broker session 返回的 endpoint/token。
- env-file 只补充未存在的变量，进程环境优先；运行模型和认证完全由外部客户端及继承环境管理，Problem Locator 不保存模型凭据。
- Prompt 只经 stdin；业务输出只读取成功退出后原子发布的 `output/job_outcome.json`，stdout/stderr 只作执行日志。
- 默认 wall time 30 分钟、stdout+stderr 64 MiB、Workspace 1 GiB。
- 不选择 Skill，不决定下一 Job，不修改 Case。
- V1 可以只有一个 Backend 实现。

### 5.12 Versioned Catalog（版本化目录）

- Agent Profile、Tool Bundle、Context Policy 和 Output Contract 作为 S04 内置资产随服务版本发布；Diagnosis Skill 从配置的 `SKILL_DIR` 扫描，logparse 运行资产由 S07 根据固定仓库、配置和解释器生成。
- Catalog 只在服务启动时构建，运行期间只读；`SKILL_DIR` 中只有带合法 `diagnosis-skill.json` 的直接子目录进入路由候选。
- 所有资产使用稳定 ID、不可原地覆盖的版本号和内容 SHA-256。
- Job 只记录逻辑版本引用，不记录发布包中的物理路径。

### 5.13 FileResourceStore（文件资源存储）

- 保存 READY Attachment（就绪附件）的原始字节。
- 保存 Evidence 文件和已发布 Artifact；`LOGPARSE_RUN` 使用不可变只读目录树。
- 返回不透明相对 `storage_key`、大小和 SHA-256 引用。
- Outcome proposal 的正式 ID 从 installation/case/outcome/proposal key 确定性派生；state commit 前故障后可跨重启校验并采用同一正式目标。
- 与 StateRepository、ExecutionRecordStore 和孤立清理共享可重入协调锁；清理在锁内重验后先原子隔离到 quarantine，锁外才删除。
- 单 Case 5 GiB 按正式 resources 根中唯一、可采用且非 quarantine 的 storage_key 计数，包含 state 引用、未确认 outbox 目标和 ordinary orphan；全批目标在同一 publication lease 内、移动任一 stage 前原子校验。
- 既有正式目标、job.json 和 job_outcome.json 的幂等采用仍重施只读权限与文件/父目录同步，不能在 replace 后故障窗口直接返回。
- finalized 且尚无 OutcomeProcessingRecord 的 Outcome 是 durable outbox；其 staged refs、确定性正式目标和 next job 在确认前不清理，损坏的未确认 Outcome 会暂停破坏性清理并使 readiness 失败。
- 不保存 Case 状态机。

## 6. 领域模型

### 6.1 Case（诊断案例）

Case 表示一次围绕稳定问题目标的完整多轮诊断。

V1 基线字段：

```text
Case
├── case_id
├── status
├── case_revision
├── diagnosis_state
├── active_job_id?
├── selected_skill_ref?
├── final_result?
├── failure?
├── created_at
└── updated_at
```

`case_revision` 保护 Case 状态、活跃 Job 和外部命令等全部业务修改；`diagnosis_state.revision` 只表示诊断语义状态版本，也是 Job 的 `base_state_revision`。`diagnosis_state` 是 Case 当前权威诊断状态；Job 中保存的历史 `context_snapshot` 只是某次执行的固定输入，不是第二份当前状态。

`diagnosis_state.problem_spec` 至少包含：

- 当前问题；
- 目标与非目标；
- 环境或影响范围；
- 用户明确约束；
- 完成条件。

如果只是补充参数、证据或环境信息，继续使用同一 Case 并增加 `DiagnosisState.revision`。如果诊断目标发生实质变化，应创建新 Case，并允许引用旧 Case 的结构化结果。

### 6.2 Case 状态

V1 Case 业务状态：

```text
NEW
RUNNING
WAITING_INPUT
WAITING_ATTACHMENT
REVIEWING
RESOLVED
FAILED
CANCELLED
INTERRUPTED
```

说明：

- `RUNNING` 表示当前存在 `PENDING` 或 `RUNNING` 的 ROUTE / DIAGNOSE 活跃 Job，具体阶段由活跃 Job 的 `job_type` 表达，不再复制为多个 Case 状态。
- `WAITING_INPUT` 和 `WAITING_ATTACHMENT` 表示当前没有运行中的诊断 Job。
- `REVIEWING` 表示当前存在 `PENDING` 或 `RUNNING` 的 REVIEW 活跃 Job，最终候选结论正在接受独立复核。
- `RESOLVED` 只能由通过复核的候选结论进入。
- `INTERRUPTED` 表示服务重启或执行异常导致原运行 Job 不再继续，但 Case 仍可从持久化状态恢复。
- 活跃 Job 只包括 `PENDING` 和 `RUNNING`；其余 Job 状态均为终态。Case 为 `INTERRUPTED` 时不得保留可执行的活跃 Job。

### 6.3 DiagnosisState（诊断状态）

DiagnosisState 保存跨 Job 必须延续的最小结构化诊断信息：

```text
DiagnosisState
├── revision
├── problem_spec
├── user_facts[]
├── confirmed_facts[]
├── active_hypotheses[]
├── rejected_hypotheses[]
├── open_questions[]
├── pending_requirements[]
├── evidence_refs[]
└── candidate_conclusion?
```

`problem_spec` 自身带版本，至少包含问题陈述、期望行为、实际行为、范围、约束和完成条件。公开 MCP 创建输入按 S06 扁平传递八个问题字段，以及相互配对的初始事实 name/value 标量数组；补充输入同样使用相互配对的 requirement name/value 标量数组。Remote MCP Adapter 严格验证并重建 S00 的 `ProblemSpecInput`、`UserFactInput[]` 与输入字典，服务端再生成 revision 1 和 provenance；内部领域模型、状态文件及输出结构保持嵌套。字段形状、name 规则、文本边界和 provenance 以 S00 与 S06 为权威。实质改变诊断目标时创建新 Case；同一目标下的澄清和补充递增 `DiagnosisState.revision`。

最小条目结构：

```text
DiagnosisItem
├── item_id
├── statement
├── status
├── provenance
├── evidence_refs[]
├── created_revision
└── supersedes[]
```

候选结论使用稳定标识并显式记录复核状态：

```text
CandidateConclusion
├── conclusion_id
├── revision
├── content_hash
├── statement
├── supporting_evidence_refs[]
├── completion_criteria_mapping[]
├── proposed_by_job_id
└── status
    ├── PROPOSED
    ├── REVIEWING
    ├── REJECTED
    └── ACCEPTED
```

`content_hash` 只覆盖候选陈述、按固定顺序的 `supporting_evidence_refs[]` 和完整完成条件映射的 Canonical JSON；明确排除 ID、revision、status、proposed_by_job_id 和 hash 本身。状态从 REVIEWING 变为 ACCEPTED/REJECTED 不改 hash，语义内容或证据变化必须产生新 revision 和 hash。

`completion_criteria_mapping[]` 按索引恰好覆盖当前 ProblemSpec 的全部完成条件，逐字回显条件原文并为每项记录 `satisfied`、Evidence 引用和解释；进入 REVIEW 前每项都必须满足且至少有一个 Evidence。Agent 草稿使用 Evidence binding，Application Service 解析为正式 Evidence ID。

更新既有候选时只沿用 conclusion ID、revision 增加 1，`proposed_by_job_id` 必须改成本轮 Outcome 的 Job ID；这保证最终可下载 USER_RESULT 对应被接受的候选 revision，而不是旧的被拒结果。

规则：

- 用户提供的信息进入 `user_facts`；Agent 推断只能先进入候选事实，二者必须通过 `provenance` 区分。
- Agent 输出的新事实只能先进入 `proposed_facts（候选事实）`；没有充分 Evidence 且未经 Coordinator 纳入 `accepted_state_delta` 的内容不得进入 `confirmed_facts`。
- 被排除假设必须记录排除原因和对应 Evidence。
- 被新信息替代的条目不物理删除，通过状态和 `supersedes` 关系失效。
- 自由文本总结可以作为展示内容，但不能覆盖这些结构化条目。

### 6.4 Job（任务）

Job 是一个有限、自包含的服务端工作单元。

```text
Job
├── job_id
├── case_id
├── job_type
├── status
├── goal
├── base_state_revision
├── context_snapshot
├── evidence_refs[]
├── attachment_refs[]
├── previous_outcome_refs[]
├── artifact_refs[]
├── agent_profile_ref
├── available_skill_refs[]
├── skill_ref?
├── tool_bundle_ref
├── context_policy_ref
├── output_contract_ref
├── logparse_tool_ref?
├── logparse_product?
├── review_target?
│   ├── candidate_conclusion_id
│   ├── candidate_revision
│   └── candidate_content_hash
├── replacement_for_job_id?
├── resource_limits
│   ├── context_bytes
│   ├── wall_time_seconds
│   ├── stdout_stderr_bytes
│   └── workspace_bytes
├── created_at
├── started_at?
├── finished_at?
└── runtime_epoch?
```

其中从 `goal` 到版本化运行配置的字段共同构成 Job 的 Context Manifest（上下文清单）。V1 不建立独立 `JobContextManifest` 实体。`logparse_tool_ref` 与 `logparse_product` 必须同为 null 或同为非 null；非 null 值在 Job 创建时从固定 Diagnosis Skill manifest 一次性复制，后续 Agent request、重启 Catalog 或包装器不得改写。

V1 固定默认限制：ROUTE 上下文 131072 bytes，DIAGNOSE/REVIEW 上下文 204800 bytes；三类 Job 的 wall time 均为 1800 秒、stdout+stderr 合计 67108864 bytes、Workspace 1073741824 bytes。

`context_snapshot（上下文快照）`是在 Job 创建时复制的一份小型结构化执行视图，至少包含该 Job 需要的 ProblemSpec、用户事实、确认事实、相关假设、未决问题、待补要求和完成条件。大型文件不复制，只保存不可变引用。

它不是：

- 完整历史聊天的快照；
- 整个 Case 聚合记录的副本；
- Workspace 文件系统副本；
- 由另一个 Agent 自动总结出来的自由文本。

`base_state_revision` 用于并发和过期校验，`context_snapshot` 用于重现该 Job 的实际业务输入。两者缺一不可。由于 V1 只直接保存当前 DiagnosisState、不通过事件回放重建旧版本，把小型快照直接放入 Job 是首版最简单的可重现方案。

`previous_outcome_refs[]` 不是死审计字段：补参 continuation 固定使 Case 进入等待态的 Outcome，复核非 PASS 后的新 Specialist 固定当前 ReviewAssessment。Runtime 将每个规范 Outcome 以 Canonical JSON 写入 Workspace 并作为必需 `PREVIOUS_OUTCOME` section 注入。REVIEW Job 的固定 target 必须与 snapshot 中 REVIEWING candidate 相同，其 supporting Evidence 必须全部位于 `evidence_refs[]`。

ROUTE Job 固定 `available_skill_refs[]`，DIAGNOSE Job 固定选中的 `skill_ref`，REVIEW Job 固定 `review_target`。所有非终态 Job 引用的版本化资产必须在允许恢复期间仍可加载，不得由“当前最新版本”替代。

Job 创建后：

- 业务目标、快照、资源引用、复核目标和运行版本不可变；
- 执行状态可以变化；
- 只有从未开始的 `PENDING` Job 可以重复分发同一 `job_id`；已经开始后失败或中断时必须创建新 Job；
- 用户补充新信息或附件后，应创建引用新 `DiagnosisState.revision` 并带有新 `context_snapshot` 的 Job。

### 6.5 Job 状态

```text
PENDING
RUNNING
SUCCEEDED
FAILED
CANCELLED
INTERRUPTED
```

Job 状态表示执行生命周期，不等同于 Case 业务状态。

`PENDING` 和 `RUNNING` 是非终态；`SUCCEEDED`、`FAILED`、`CANCELLED` 和 `INTERRUPTED` 是终态。`STALE` 是 Application Service 在发现活跃 Job、基础诊断版本或固定复核目标不匹配时保存的 Outcome disposition，不是 JobStatus，也不是 Reviewer 的业务意见。

V1 不自动重跑 Agent。Runtime 已 finalized 规范 Outcome 后，即使其首次业务投递失败，也只能重投同一持久化 receipt，永远不得再次执行该 Job 的 Agent；这属于 submission replay，不是 Agent retry。只有确实没有 finalized Outcome 的可恢复执行失败才先进入 `INTERRUPTED`，再由显式 Resume 创建替代 Job；不可恢复失败必须新建 Case。Context Builder 无法在预算内保留最低必需内容时，Job 以 `FAILED` 结束并记录 `error.code = CONTEXT_LIMIT`。

Agent 返回“需要用户输入”时：

- Job 以 `SUCCEEDED` 结束；
- JobOutcome 的业务结果为 `NEED_INPUT`；
- Case 进入 `WAITING_INPUT`。

### 6.6 AgentJobOutcome 与 Typed JobOutcome

Agent 原子写入 Workspace 的 `output/job_outcome.json`，其内容是 AgentJobOutcome：文件型 Evidence/Artifact 使用 Draft 和受限 Workspace 相对路径。Runtime 校验并持久化暂存 Draft 后，构造下列规范 JobOutcome；只有规范结果进入 Application Service、执行记录与状态文件。Runtime 对完整规范 Outcome 执行 Canonical JSON finalize 后，该 `job_outcome.json` 就是 durable outbox；`OutcomeProcessingRecord` 是其业务投递确认。

```text
JobOutcome
├── outcome_id
├── job_id
├── case_id
├── job_type
├── base_state_revision
├── result_type
├── payload
│   ├── RouteDecision
│   ├── DiagnosisOutcome
│   └── ReviewAssessment
├── consumed_evidence_refs[]
├── proposed_evidence[]
├── proposed_artifacts[]
├── error?
│   └── ExecutionFailure
└── produced_at
```

非失败 `payload` 必须与 `job_type` 一一对应且 `error=null`。`result_type = FAILED` 时 `payload=null`、`error=ExecutionFailure（执行失败）`，不要求 Agent 已经产生业务载荷。DIAGNOSE Job 的载荷为：

```text
DiagnosisOutcome
├── findings[]
├── state_delta
├── requested_input[]
├── requested_attachments[]
├── candidate_conclusion_draft?
└── recommended_next_step
```

```text
ExecutionFailure
├── stage
├── code
├── message
├── retryable
└── details[]: ApplicationErrorDetail
```

`details[]` 始终存在，没有条目时写空数组，并与公共 `ApplicationErrorDetail` 使用相同的安全字段和敏感信息禁令。`CONTEXT_LIMIT`、Backend 创建失败、工具超时和输出 Schema 不合法等都通过 ExecutionFailure 表达。V1 不调用模型修复非法输出；Application Service 完成技术校验后，仍把失败 Trigger 交给 Coordinator 决定 Case 进入 `INTERRUPTED` 还是 `FAILED`。

新 Evidence、Artifact 或 CandidateConclusion 不能在 Agent 输出前预先拥有正式业务 ID。Agent Draft 只带 Job 内唯一 `proposal_key`、语义字段和可选 Workspace 相对路径；Runtime 计算实际大小与哈希，把文件或目录写入 FileResourceStore 的持久化暂存区，并在规范 Proposal 中加入不含路径的 `StagedResourceRef`。规范 JobOutcome 不得保留 Workspace 路径。

Application Service 先校验候选内容和暂存资源，Coordinator 在 TransitionPlan 中分别返回被接受的 Evidence、Artifact 和 Candidate proposal key，并在下一 Job 中使用 proposal-key placeholder。LOGPARSE Evidence 可以用 `EvidenceSourceBinding.artifact_proposal_key` 引用同一 Outcome 尚未分配正式 ID 的 `LOGPARSE_RUN`，但 Coordinator 必须共同接受二者。Application Service 随后分配正式 ID，把被接受的暂存资源发布为不可变正式资源，解析 Evidence 来源、Candidate 的 Evidence binding 和下一 Job 的全部 placeholder，再在一次 StateRepository 提交中保存 Outcome、正式元数据、状态引用和下一 Job。Worker、Runtime、Coordinator 和 Agent 都不得绕过该流程创建正式业务记录。

Outcome proposal 的 Evidence、Artifact、首次 Candidate 和 next Job ID 必须从 installation/case/outcome/proposal key 确定性派生；它们的业务创建时间固定使用 JobOutcome.produced_at，使重交时完整 job.json 也保持相同字节。Application Service 在全部 binding 正式化并得到目标 DiagnosisState 后，才调用公共纯 projector 生成下一 Job snapshot。若重放时确定性 next Job 已存在，必须从旧 `job.json` 机械复用 RuntimeBindings 与创建时间、重构后逐字比对 Canonical bytes，不得因当前 Catalog 漂移改写；旧文件损坏或冲突必须拒绝覆盖。带 Candidate 的 Outcome 必须同时带且同批接受唯一 USER_RESULT JSON Artifact；其公开 payload 逐字符合公共 UserResultPayload Schema，problem/candidate 和 draft→正式 Evidence binding 必须与本轮正式化结果语义一致，该结果只有在所属 candidate 最终 ACCEPTED 后才可下载。

如果资源发布失败，StateRepository 业务提交不得发生；如果资源已发布但状态提交失败，正式资源成为待重放对象。发布键和 Outcome 处理必须幂等；同进程只重投同一 finalized receipt，重启先读取权威执行记录再走相同提交路径，同 Outcome 得到相同 ID，并可通过正式目标的 size/hash 采用已发布字节，不要求已移动的 stage 仍存在。未确认 durable outbox 的 staged refs、确定性正式目标和预发布 next Job 不受临时或孤立清理；执行记录损坏时 readiness 保持失败且不得回退到当前 Catalog 或重跑 Agent。Job Workspace 只有在 Outcome 处理完成，或暂存/补偿信息已经可靠登记后才能删除。

V1 通用 `result_type`：

```text
COMPLETED
NEED_INPUT
NEED_ATTACHMENT
REROUTE
NO_CAPABILITY
FAILED
```

`recommended_next_step` 只是 Agent 建议，最终下一步由 Diagnosis Coordinator 决定。

JobOutcome 及其 RouteDecision、DiagnosisOutcome、ReviewAssessment 载荷一经保存不可原地修改；更正或重新执行必须追加新记录。

### 6.7 DiagnosisStateDelta（诊断状态增量）

```text
DiagnosisStateDelta
├── problem_spec_patch?
├── add_user_facts[]
├── proposed_facts[]
├── add_active_hypotheses[]
├── update_hypotheses[]
├── reject_hypotheses[]
├── add_open_questions[]
├── resolve_questions[]
├── add_pending_requirements[]
├── fulfill_requirements[]
└── add_evidence_bindings[]
```

Application Service 的技术校验至少包括：

- Outcome 对应当前活跃 Job；
- Job 当前处于 `RUNNING`；
- Outcome、Job 与 Case 的 `case_id` 一致；
- `base_state_revision` 与 Job 固定版本及当前 `DiagnosisState.revision` 一致；
- `consumed_evidence_refs ⊆ Job.evidence_refs`；
- Outcome 引用的既有 Attachment、JobOutcome、Artifact 和其他资源必须属于当前 Case，且出现在 Job 的固定引用中；
- 候选结论的支持证据只能来自 `Job.evidence_refs`，或来自本 Outcome 的 `proposed_evidence` proposal key；
- proposed Evidence / Artifact 的临时内容存在且哈希一致；
- 同一个 Outcome 没有重复应用。

Coordinator 根据通过技术校验的 Outcome 与 CaseSnapshot 决定 Delta 是否符合业务转换，并在 `TransitionPlan` 中分别返回 `accepted_state_delta`、接受的 Evidence/Artifact proposal keys、可选的 Candidate proposal key，以及 selected skill、CaseFailure、Candidate 状态的显式 mutation。Application Service 不从 result type 或 verdict 推导遗漏的业务变化，只负责解析占位符并执行计划。

Delta 中的 `proposed_facts` 是候选内容，只有进入 `accepted_state_delta` 后才能成为 `confirmed_facts`。Agent 侧条目和 `add_evidence_bindings[]` 可以引用同一 Outcome 的 Evidence proposal key；Application Service 必须在提交前解析为正式 Evidence ID，draft、binding 和 proposal key 不得进入 `state.json`。候选结论由 `DiagnosisOutcome.candidate_conclusion_draft` 独立提出，并在 Coordinator 接受、Application Service 分配正式 ID 后进入候选状态；它还必须经过 REVIEW Job 才能成为 `final_result`。

如果当前 Case 已经超过基础版本，Application Service 把 Outcome 以 `STALE` disposition 保存为只读审计记录且不应用 Delta。首次保存使 `case_revision + 1`、DiagnosisState revision 不变；重复同一 Outcome 为 `DUPLICATE`，任何 revision 都不变。仍为 active RUNNING 的异常漂移通过 `STALE_ACTIVE_OUTCOME` Trigger 由 Coordinator 转入 `INTERRUPTED`；非 active 或已终态旧 Job 的状态不变。

### 6.8 RouteDecision（路由决定）

```text
RouteDecision
├── kind
├── skill_ref?
├── reason
└── confidence
```

Router Agent 只能返回 `MATCHED` 或 `NO_CAPABILITY`，并只能从 ROUTE Job 固定的 `available_skill_refs[]` 中选择；Application Service 必须拒绝集合外的 `skill_ref`。没有匹配能力时返回 `NO_CAPABILITY`，V1 不转入 General Code Agent，也不由 Router 索要资料。

### 6.9 ReviewAssessment（复核结果）

```text
ReviewAssessment
├── candidate_conclusion_id
├── candidate_revision
├── candidate_content_hash
├── reviewed_state_revision
├── reviewed_evidence_refs[]
├── verdict
├── unsupported_findings[]
├── evidence_conflicts[]
├── missing_evidence[]
├── stale_references[]
└── recommendation
```

V1 `verdict`：

```text
PASS
NEED_MORE_EVIDENCE
REJECT
```

`PASS` 只能接受 REVIEW Job 的 `review_target` 所固定的候选结论；ReviewAssessment 没有逐字回显该固定 ID、版本和哈希时是 `OUTCOME_INVALID/REJECTED`。只有 Job 固定目标后来被 Case 的合法新版本替代时才是系统级 `STALE`。`reviewed_evidence_refs[]` 必须全部来自 REVIEW Job 的固定 Evidence 集合，且 `PASS` 必须覆盖候选结论的全部 `supporting_evidence_refs[]`；引用未固定 Evidence 或遗漏支持证据时不得 `PASS`。PASS 还要求 unsupported/conflict/missing/stale 四个问题数组全空；NEED_MORE_EVIDENCE 至少有 missing 或 unsupported，REJECT 至少有 unsupported、conflict 或 stale。Reviewer 不能直接修改 Case，也不能直接调用 Specialist。Application Service 技术校验 ReviewAssessment 后交给 Coordinator，再原子保存 Outcome；`PASS` 完成 Case，`NEED_MORE_EVIDENCE` 或 `REJECT` 都创建新的 DIAGNOSE Job，并把本 ReviewAssessment 作为固定 PREVIOUS_OUTCOME 交给新 Specialist。

### 6.10 Attachment、Evidence 与 Artifact（附件、证据与产物）

三者职责不同：

- Attachment（附件）是用户提交的原始文件；
- Evidence（证据）是能够支撑或反驳诊断判断的可定位内容，不是“文件”的同义词；
- Artifact（产物）是系统生成并供用户下载或后续引用的结果文件。

V1 最小结构：

```text
Attachment
├── attachment_id
├── case_id
├── status
├── name
├── content_type
├── size
├── sha256
└── storage_key

Evidence
├── evidence_id
├── case_id
├── source_type
├── source_ref
├── locator
├── summary
├── collected_at
└── content_hash?

Artifact
├── artifact_id
├── case_id
├── kind
├── name
├── content_type
├── resource_kind
├── size
├── sha256
└── storage_key
```

Evidence 可以定位到附件行号、时间区间、请求 ID、命令结果或结构化用户输入。原始日志、截图、转储和大型工具输出保存在 FileResourceStore；模型上下文只接收必要片段和 Evidence 引用。

V1 不使用任意 JSON locator/metadata：Evidence locator 由 USER_FACT、ATTACHMENT、LOGPARSE、TOOL_OUTPUT、PREVIOUS_OUTCOME 判别，并分别约束 source_ref 指向 DiagnosisItem、READY Attachment、LOGPARSE_RUN Artifact、DIAGNOSTIC_EXPORT Artifact 或规范 JobOutcome。Artifact metadata 由 USER_RESULT、DIAGNOSTIC_EXPORT、LOGPARSE_RUN 判别；后者固定目录 hash、logparse VersionedRef、manifest 相对路径、源 Attachment ID/hash 和仅含 product 的 parse 参数。

一旦被 Job 引用，Attachment、Evidence、Artifact、JobOutcome 及其类型化载荷的内容与定位语义必须不可变；内容变化时创建新 ID。Evidence 如果来自会变化的外部资源，必须先物化内容或固定可重现的版本，不能让旧 Job 在执行时读到新内容。

## 7. 上下文策略

### 7.1 Context Builder 输入

Context Builder 只接收：

- 当前 Job；
- Job 固定的 `context_snapshot` 和 `base_state_revision`；
- Job 引用的 Evidence、Attachment 和历史 Outcome；
- 固定版本的 Agent Profile、`available_skill_refs[]` 或 Diagnosis Skill、Tool Bundle 和 Context Policy；
- Job 固定版本的 Output Contract（输出契约）。

它不得自行把 Job 创建后的最新输入、最新 Attachment 或最新 DiagnosisState 混入正在执行的 Job。

### 7.2 Specialist 上下文

按以下顺序组装：

1. Agent Profile 和目标 Diagnosis Skill；
2. 当前 Job 目标和完成条件；
3. `context_snapshot` 中的问题、非目标和有效约束；
4. 已确认事实及其 Evidence 引用；
5. 活跃假设；
6. 与当前目标相关的已排除假设及排除原因；
7. 未决问题；
8. 相关附件清单和必要内容片段；
9. Typed JobOutcome 输出 Schema。

### 7.3 Router 上下文

Router 只读取：

- `context_snapshot` 中的问题和必要环境信息；
- Job 固定的 Attachment 清单；
- ROUTE Job 的 `available_skill_refs[]` 所固定的 Diagnosis Skill 摘要目录；
- RouteDecision 输出 Schema。

Router 不读取 Specialist 的内部过程，也不获得所有完整 Diagnosis Skill。

### 7.4 Reviewer 上下文

Reviewer 只读取：

- REVIEW Job 创建时固定的 `context_snapshot`，其中包含 ProblemSpec 和所需 DiagnosisState 视图；
- `review_target` 固定的候选结论 ID、版本和内容哈希；
- REVIEW Job 固定引用的候选 DiagnosisOutcome；
- REVIEW Job 固定引用的 Evidence；
- ReviewAssessment 输出 Schema。

Reviewer 不读取执行时最新 DiagnosisState，也默认不读取 Specialist 的完整对话、草稿和与候选结论无关的工具轨迹。

### 7.5 默认排除内容

- 完整历史聊天；
- 被替代的 ProblemSpec；
- 已失效决定；
- 与当前 Job 无关的历史 Outcome；
- 大型原始日志全文；
- 与当前假设无关的工具输出；
- Agent 隐藏推理过程；
- 其他 Case 的任何内容；
- Job 创建后新增但没有进入固定引用的输入或附件。

### 7.6 Context 预算

V1 使用固定优先级和 UTF-8 字节预算，不实现模型自动摘要。Router 默认上限为 128 KiB（131072 bytes），Specialist 与 Reviewer 默认上限均为 200 KiB（204800 bytes）；准确计数对象是提交给 Backend 的最终上下文正文。

最低必需内容包括：

- 固定 Agent Profile；
- 选定 Diagnosis Skill，或 Router 的固定 Skill 摘要目录；
- 当前 Job 目标；
- 当前 ProblemSpec；
- 输出 Schema；
- 当前开放 requirements；
- 直接相关的确认事实、假设和 Evidence；
- 用户当前有效约束。
- Reviewer 的固定候选结论与 review target。

如果最低必需内容超过预算：

- 不允许静默删除；
- Runtime 返回明确的 `CONTEXT_LIMIT（上下文超限）`错误；
- Coordinator 可以在 TransitionPlan 中要求范围更小的新 Job，或让 Case 等待用户缩小问题范围；Job 仍由 Application Service 创建。

## 8. Agent Session（智能体会话）与 Workspace（工作区）策略

### 8.1 Session 生命周期

- 每个 ROUTE、DIAGNOSE 或 REVIEW Job 默认创建新的 Agent Session。
- 一个 Job 内部可以由 Runtime 和 Backend 驱动多次模型调用与工具调用。
- Job 到达完成、失败、等待输入或等待附件边界时结束，Session 随后关闭。
- 不同 Agent 使用不同 Session。
- 同一种 Agent 的连续 Job 也不继承完整对话。
- Session Handle、进程 PID 和提供方物理标识不进入 Case、Job 或外部接口。

未来可以增加 Session Cache 以降低延迟，但必须满足：

> 启用或禁用 Session 复用不改变 Job 的业务输入语义，也不影响 Case 是否可以继续。

### 8.2 Job Workspace（任务工作区）

- Runtime 为每个 Job 创建临时 Workspace。
- 根据 Job 固定引用只读物化 READY Attachment、Evidence、Artifact，并把 previous JobOutcome 的 Canonical JSON 写入固定 `inputs/outcomes/` 路径。
- `inputs/manifest.json` 按公共 discriminated schema 逐项记录四组输入、相对路径、size/hash、Artifact kind/metadata 和 Job 固定 logparse ref；S04 是唯一生产者，S07 只消费。
- `runtime/tool-state/logparse-parse.claim` 在真正启动首次 parse 前以 create-new 语义写入完整公共 claim；固定输入已含 LOGPARSE_RUN 时禁止产生 claim。
- Agent 草稿、临时命令输出和中间文件留在该 Workspace。
- 需要跨 Job 保留的结果必须作为 `proposed_evidence` 或 `proposed_artifact` 回送，并由 Application Service 发布为 Evidence 或 Artifact。
- Application Service 完成 Outcome 处理后，Job Workspace 可以删除；未被接受的暂存内容按清理策略删除。
- Workspace 用于文件归属和并发正确性，不构成安全沙箱。
- V1 因此只允许运行受信任的 Diagnosis Skill、外部 Agent 客户端和 logparse 资产；job-scoped broker 只缩小 logparse 调用面，不把 Workspace 提升为恶意代码隔离边界。

## 9. 服务端执行流程

### 9.1 创建 Case 与路由

```mermaid
sequenceDiagram
    actor U as User（用户）
    participant C as Client Skill（客户端技能）
    participant A as Application Service（应用服务）
    participant D as Diagnosis Coordinator（诊断协调器）
    participant R as Repository（仓库）
    participant J as Job Execution Pipeline（任务执行链）

    U->>C: 提交问题
    C->>A: 创建 Case 命令
    A->>D: CaseSnapshot + CREATE_CASE
    D-->>A: TransitionPlan + ROUTE JobSpec
    A->>R: 原子保存目标状态和带快照的 ROUTE Job
    A->>J: 提交已持久化 Job
    J-->>A: 使用新 Session 返回 RouteDecision 提案
    A->>A: 技术校验 RouteDecision
    A->>D: 当前 CaseSnapshot + ROUTE_OUTCOME
    D-->>A: TransitionPlan + DIAGNOSE JobSpec 或 NO_CAPABILITY
    A->>R: 原子保存 Outcome、目标状态和带快照的下一 Job
```

图中的 Job Execution Pipeline（任务执行链）统一按 9.2 节执行，不是绕过 Worker、Runtime 或 Application Service 校验的另一条路径。

### 9.2 通用 Job 执行闭环

```mermaid
sequenceDiagram
    participant A as Application Service（应用服务）
    participant R as Repository（仓库）
    participant C as Diagnosis Coordinator（诊断协调器）
    participant D as Dispatcher（分发器）
    participant W as Typed Worker（类型化工作器）
    participant X as Diagnosis Runtime（诊断运行时）
    participant CB as Context Builder（上下文构建器）
    participant B as Agent Backend（智能体执行后端）

    A->>R: 原子保存目标状态和带 context_snapshot 的 Job
    A->>D: 状态提交后分发 Job
    D->>W: 按 Job 类型选择 Worker
    W->>A: 条件认领 PENDING Job
    A->>R: PENDING → RUNNING
    A-->>W: 认领成功
    W->>X: 执行类型化 Job
    X->>CB: 请求构造固定上下文
    CB->>R: 读取 Job 快照和固定引用
    CB-->>X: Bounded Context（有界上下文）
    X->>B: 创建新 Session 并执行
    B-->>X: Agent 原始结果
    X-->>W: Schema 校验后的 Typed JobOutcome
    W-->>A: 回送结果提案
    A->>R: 读取当前 Case、Job 和引用
    A->>A: 技术校验幂等、归属、状态、revision 和引用
    A->>C: CaseSnapshot + Validated Outcome
    C-->>A: TransitionPlan + accepted_state_delta + optional JobSpec
    A->>R: 原子保存 Outcome、执行计划并创建带新快照的下一 Job
```

### 9.3 等待用户输入或附件

当 Specialist 返回 `NEED_INPUT` 或 `NEED_ATTACHMENT`：

1. Specialist 返回等待资料的 DiagnosisOutcome；
2. Runtime 结束 Agent 执行并关闭当前 Session；
3. Application Service 技术校验 Outcome，再交给 Coordinator；
4. Coordinator 返回保存未决要求并进入 `WAITING_INPUT` 或 `WAITING_ATTACHMENT` 的 TransitionPlan；
5. Application Service 原子保存 Outcome、执行计划并结束 Job；
6. 用户通过 `SubmitSupplement（提交补充资料）`一次提交文本输入和已经 `READY` 的 Attachment 引用；
7. Application Service 完成幂等、Case 状态、Attachment 归属和 `READY` 校验，再把 Trigger 交给 Coordinator；
8. Coordinator 返回满足待补要求、增加 `DiagnosisState.revision` 的 TransitionPlan 和 DIAGNOSE JobSpec；
9. Application Service 原子执行计划，并从更新后的 DiagnosisState 物化新 Job 的 `context_snapshot`；
10. Runtime 使用新 Session 执行，不恢复旧对话。

Attachment 上传完成本身不会自动推进 Case。只有 `SubmitSupplement` 成功后，补充资料才进入新的 DiagnosisState 和下一 Job；因此不存在“资料已就绪”和“继续诊断”两个可能重复触发的控制命令。

### 9.4 最终复核

1. Specialist 返回带 `candidate_conclusion` 的候选 DiagnosisOutcome；
2. Application Service 技术校验后将 Outcome 交给 Coordinator；
3. Coordinator 返回“接受候选结论、Case 进入 `REVIEWING`、创建 REVIEW Job”的 TransitionPlan；
4. Application Service 在一次业务提交中保存 Outcome 和候选结论，并从更新后的目标 DiagnosisState 物化 REVIEW Job 的 `context_snapshot` 与 `review_target`；此时不写入 `final_result`；
5. Review Worker 使用独立新 Session 和固定的受限上下文执行；
6. Reviewer 返回绑定 `review_target` 的 ReviewAssessment；
7. `PASS` 时 Coordinator 返回完成转换，Application Service 原子将该固定候选结论接受为 `final_result` 并把 Case 置为 `RESOLVED`；
8. `NEED_MORE_EVIDENCE` 或 `REJECT` 时，Coordinator 标记原候选结论并创建新的 DIAGNOSE Job；是否还需资料由该 Specialist 在下一 Job 中判断；
9. 如果 REVIEW Job 的固定目标已被合法状态更新替代，则结果记为系统级 `STALE`，不得完成 Case；若 ReviewAssessment 自身伪造或错误回显固定目标，则为 `OUTCOME_INVALID/REJECTED`。后续只能根据当前持久化状态创建新的 REVIEW 或 DIAGNOSE Job。

### 9.5 多次索要参数与日志分析中途补参

以下时序展示一个 Case 先后经历“索要参数组 A”“索要一次日志”“分析日志后发现还需要参数 B”，最后形成候选结论并通过独立复核的完整过程。RPC 超时 Fixture 中参数组 A 的 requirement name 固定为 `caller_service`、`server_service`、`rpc_method`、`problem_time`，其中 problem_time 是毫秒精度 UTC RFC 3339 时刻；日志 requirement 为 `log_archive`，参数 B 为 `order_id`。

```mermaid
sequenceDiagram
    autonumber
    actor U as User（用户）
    participant C as Client Access Skill（客户端接入技能）
    participant A as Application Service（应用服务）
    participant D as Diagnosis Coordinator（诊断协调器）
    participant R as Repository（仓库）
    participant B as HTTP File Adapter / FileResourceStore（文件接入与存储）
    participant X as Dispatcher / Worker / Runtime（任务执行链）
    participant G as Job-scoped logparse broker（受控代理）
    participant L as logparse CLI（压缩日志唯一处理方）

    Note over U,X: 创建 Case 并完成路由

    U->>C: 提交待诊断问题
    C->>A: CreateCase
    A->>D: CaseSnapshot + CREATE_CASE
    D-->>A: TransitionPlan + ROUTE JobSpec
    A->>R: 原子保存 Case 和 ROUTE Job
    A->>X: 分发 ROUTE Job
    Note over X: 创建新的 Router Session
    X-->>A: RouteDecision
    A->>D: ROUTE_OUTCOME
    D-->>A: 创建 DIAGNOSE Job #1
    A->>R: 保存路由结果和 Job #1 固定快照

    Note over U,X: 第一次索要参数

    A->>X: 执行 DIAGNOSE Job #1
    Note over X: 创建新的 Specialist Session
    X-->>A: NEED_INPUT（参数组 A）
    A->>D: DIAGNOSIS_OUTCOME
    D-->>A: Case → WAITING_INPUT
    A->>R: 保存 Outcome，Job #1 → SUCCEEDED
    Note over X: Session 关闭

    U->>C: 提交参数组 A
    C->>A: SubmitSupplement（参数组 A）
    A->>D: CaseSnapshot + 补充参数
    D-->>A: 更新状态并创建 DIAGNOSE Job #2
    A->>R: 增加状态版本，保存 Job #2 固定快照

    Note over U,X: 唯一一次索要日志

    A->>X: 执行 DIAGNOSE Job #2
    Note over X: 创建新的 Specialist Session
    X-->>A: NEED_ATTACHMENT（诊断日志）
    A->>D: DIAGNOSIS_OUTCOME
    D-->>A: Case → WAITING_ATTACHMENT
    A->>R: 保存 Outcome，Job #2 → SUCCEEDED
    Note over X: Session 关闭

    U->>C: 选择日志文件
    C->>A: PrepareAttachment
    A->>R: 创建 UPLOADING Attachment
    A-->>C: attachment_id + 上传描述
    C->>B: PUT 日志字节
    B->>A: 发布附件请求（大小、哈希）
    A->>B: 发布不可变文件资源
    A->>R: Attachment → READY
    Note over A,R: READY 本身不推进诊断

    C->>A: SubmitSupplement（日志 attachment_id）
    A->>D: CaseSnapshot + READY Attachment
    D-->>A: 更新状态并创建 DIAGNOSE Job #3
    A->>R: 保存 Job #3，固定日志引用和新快照

    Note over U,X: 日志分析到一半后发现还需要新参数

    A->>X: 执行 DIAGNOSE Job #3
    Note over X: 新 Specialist Session，读取固定日志
    X->>G: problem-locator-logparse parse-targets
    G->>L: 固定 argv 执行 parse（唯一一次）
    L-->>G: parse_manifest + 解析目录
    G->>L: 固定 argv 执行 mech-target-logs
    L-->>G: target_logs
    G-->>X: 受控结果相对路径
    X->>X: 模型与工具循环：分析到中途
    X->>X: 发现缺少参数 B
    X-->>A: NEED_INPUT（参数 B）+ Evidence / LOGPARSE_RUN 提案
    A->>D: DIAGNOSIS_OUTCOME
    D-->>A: 接受中间状态增量，Case → WAITING_INPUT
    A->>B: 发布只读 LOGPARSE_RUN 目录树
    A->>R: 原子保存中间发现、Evidence、Artifact 和 Outcome
    A->>R: Job #3 → SUCCEEDED
    Note over X: Session 关闭，中间进度由 Repository 承接

    Note over U,X: 第二次索要参数后继续诊断

    U->>C: 提交参数 B
    C->>A: SubmitSupplement（参数 B）
    A->>D: CaseSnapshot + 补充参数
    D-->>A: 更新状态并创建 DIAGNOSE Job #4
    A->>R: 保存新状态版本和 Job #4
    Note over A,R: 快照固定中间发现、参数 B 和 LOGPARSE_RUN 引用

    A->>X: 执行 DIAGNOSE Job #4
    Note over X: 新 Specialist Session，从结构化状态继续
    X->>B: 只读物化已保存 LOGPARSE_RUN
    X->>G: problem-locator-logparse target-logs
    G->>L: 对已保存目录执行 mech-target-logs
    L-->>G: target_logs
    G-->>X: 受控结果相对路径
    Note over X,L: 禁止再次解包或调用 parse；Case parse 计数仍为 1
    X-->>A: COMPLETED + CandidateConclusion
    A->>D: DIAGNOSIS_OUTCOME
    D-->>A: Case → REVIEWING，创建 REVIEW Job
    A->>R: 原子保存候选结论和 REVIEW Job

    Note over U,X: 独立复核并完成

    A->>X: 执行 REVIEW Job
    Note over X: 创建独立 Reviewer Session
    X-->>A: ReviewAssessment.PASS
    A->>D: REVIEW_OUTCOME
    D-->>A: Case → RESOLVED
    A->>R: 保存 final_result
    A-->>C: 返回最终诊断结果
    C-->>U: 展示结论和相关产物
```

本场景必须满足：

- 每次 `NEED_INPUT` 或 `NEED_ATTACHMENT` 都结束当前 Job，Job 状态为 `SUCCEEDED`，Case 进入对应等待状态，当前 Agent Session 随后关闭。
- Attachment 达到 `READY` 只表示文件可以被引用；只有后续 `SubmitSupplement` 才会推进 Case 并创建新 Job。
- Job #3 必须经 `diagnose-* → logparse-diagnose → problem-locator-logparse broker → logparse parse/mech-target-logs` 处理日志；只有 job-scoped broker 能启动固定 CLI，Problem Locator 其他组件不得打开、枚举或解压压缩包。
- Job #3 在分析日志后发现参数 B 时，必须通过 DiagnosisOutcome 提交中间发现、DiagnosisStateDelta、候选 Evidence 和目录型 `LOGPARSE_RUN`；只有 Coordinator 接受并由 Application Service 持久化的内容，才能进入后续 Job。
- Job #4 使用新的 Agent Session，其固定输入必须包含已接受的中间状态、参数 B、源 Attachment、`LOGPARSE_RUN` 和使 Case 进入等待态的 Job #3 Outcome，不能依赖 Job #3 的对话历史；它只读物化解析目录，不再次调用 `logparse parse`，整个无故障场景 parse 次数严格为 1。
- Job #4 的候选必须与唯一 `diagnosis-result.json` USER_RESULT 同 Outcome、同批发布；最终候选仍进入独立 REVIEW Job，只有 `ReviewAssessment.PASS` 后该 Artifact 才可下载并写入 `final_result`。

## 10. 客户端接入与文件传输

### 10.1 接入原则

- 用户不安装 Local MCP Server（本地 MCP 服务），也不运行项目专用常驻进程。
- Agent CLI 使用自身的 Remote MCP 客户端完成结构化控制交互。
- Client Access Skill 调用系统已有的 `curl` 完成本地文件上传和结果下载。
- Remote MCP 承载控制命令、小型输入和结构化结果。
- HTTP 承载附件和结果文件的字节流，也为 Web 或普通 HTTP 客户端提供等价的 Attachment 准备入口。
- Remote MCP Adapter 和 HTTP File Adapter 共同调用 Application Service。
- 两种 Adapter 不互相回调，也不分别实现业务规则。

### 10.2 统一服务入口

V1 使用同一个稳定服务地址：

```text
/mcp       Remote MCP（远程 MCP 接入）
/api/v1    HTTP API（HTTP 接口）
```

未来增加负载均衡或多实例时，客户端地址和业务语义保持稳定。

### 10.3 Remote MCP 能力

固定提供七个工具：

```text
problem_locator_create_case
problem_locator_prepare_attachment
problem_locator_submit_supplement
problem_locator_get_case
problem_locator_resume_case
problem_locator_cancel_case
problem_locator_list_artifacts
```

精确字段和 envelope 由 [S06](v1-specs/S06-mcp-http-cli.md) 独占定义。

`SubmitSupplement` 是唯一推进等待中 Case 的补充资料命令。它必须幂等，并在同一次业务提交中校验等待状态、Attachment 所属 Case 与 `READY` 状态、满足待补要求、递增状态版本并创建下一 Job。查询始终只读，服务端不主动回连 CLI。

Agent CLI 通过 Remote MCP 调用 `PrepareAttachment`；Web 或普通 HTTP 客户端可以使用 10.5 节的等价 `POST`。两条入口调用同一个 Application Service 命令。返回值是 `attachment_id`、HTTP 方法、上传 URL 和必要约束等结构化数据，不是绑定 Bash、PowerShell 或其他 Shell 的完整命令。

`ResumeCase` 不提交新诊断事实，也不修改已创建 Job 的上下文：

- Application Service 先读取 Case 与相关 Job，只做幂等、状态、`runtime_epoch` 和固定资产可用性技术校验；
- Case 存在 `PENDING` Job 且其固定版本仍可加载时，幂等地重新分发同一个 `job_id`、同一个快照；
- 每次服务进程启动产生新的 `runtime_epoch（运行代次）`；Job 从 `PENDING` 被认领时记录当前代次；
- 服务启动先按 `{case_id,job_id}` 扫描全部尚无 processing record 的 finalized Outcome，并通过正常提交路径重放；只有全部重放得到 APPLIED、DUPLICATE、STALE 或 REJECTED 后，才以 `MARK_OLD_EPOCH_INTERRUPTED` 将确实没有 finalized Outcome 的旧代次 `RUNNING` Job 转为 `INTERRUPTED` 并清除 active，不自动创建替代项。执行记录损坏或重放仍有瞬时错误时 readiness=false，禁止提前中断或重跑；
- Case 已经是 `INTERRUPTED` 且没有活跃 Job 时，Application Service 查找最近一个尚无替代项的 `INTERRUPTED` Job，再把 `RESUME_INTERRUPTED` 交给 Coordinator；
- Coordinator 返回保持同一业务阶段的替代 JobSpec；
- 被中断的 REVIEW 仍替换为 REVIEW，不能退回 DIAGNOSE 绕过复核；
- Application Service 在一次 Repository 条件事务中执行 Resume TransitionPlan，为替代执行创建新的 `job_id`、记录 `replacement_for_job_id`，并根据目标 DiagnosisState 生成新的 `base_state_revision` 和 `context_snapshot`；
- Case 正在等待补充资料时，拒绝恢复并提示使用 `SubmitSupplement`；
- Case 已终止或已有活跃 Job 时，不重复创建 Job。

`replacement_for_job_id` 的唯一约束保证重复调用不会创建多个替代 Job。旧 Job 的迟到 Outcome 因 `active_job_id` 和 Job 状态不匹配而记为 `STALE`，不得合并。

固定资产无法加载时不得静默替换为最新版。Application Service 产生 `ASSET_VERSION_UNAVAILABLE（资产版本不可用）` Trigger；Coordinator 对 PENDING Job 返回“Job → `FAILED`、清除 active Job、Case → `FAILED`”的 TransitionPlan，对已经 `INTERRUPTED` 的 Job 保持其终态并把 Case 置为 `FAILED`。用户若要使用新资产重新诊断，应创建新 Case。

ResumeCase 提供的是至少一次业务执行语义，不保证外部工具副作用恰好一次。可恢复 Job 默认只能使用只读或幂等工具；可能产生不可逆外部副作用的工具不得被无条件重放，其确认与补偿规则留到 Tool Bundle 详细设计。

### 10.4 Attachment 生命周期

```text
UPLOADING
READY
FAILED
```

规则：

- 准备上传时先创建 `UPLOADING` Attachment 元数据。
- 上传内容先通过 Attachment 专用 `AttachmentStagedRef` 写入临时资源；它不带 Job/proposal 字段，也不能出现在 Agent Proposal 中。
- 服务端计算大小和内容哈希。
- 校验成功后原子发布正式资源，并将 Attachment 标记为 `READY`。
- `READY` Attachment 不允许原地覆盖；重新上传产生新的 Attachment ID。
- 上传失败或中断时不发布正式资源。
- Worker 只消费 `READY` Attachment。
- Attachment 不记录文件来自 curl、Web 还是其他客户端。

这里的“原子发布”只指暂存资源到正式资源的 FileResourceStore 内发布，不假设 FileResourceStore 与 StateRepository 之间存在跨存储事务。必须保证 StateRepository 不会在正式资源可读之前提交 `READY`。如果资源已发布而 `READY` 元数据提交失败，Attachment 仍不可消费；相同 attachment_id 与相同 size/hash 的后续 PUT 必须采用该正式目标并完成 commit，不要求旧 stage；不同 size/hash 固定返回 `IDEMPOTENCY_CONFLICT` 且绝不覆盖。只有在既无 state 引用也无未确认 durable outbox 保护时，它才可按 7 日孤立规则清理。

同一 attachment_id 的完整上传从重新读取状态、流式 stage、容量校验、发布到状态提交都持有同一个进程内 `AttachmentUploadGuard`；不同 attachment_id 可并行。网络流期间不得持有全局 publication lease；body 只消费一次，流结束后才在短 lease 内重新读取最新 snapshot/generation，并覆盖全批 Case 5 GiB 校验、正式目标发布/采用和 state commit。期间发生的全局 generation 变化只重算使用同一 completed stage 的 post-stage 阶段，绝不重读流。

### 10.5 HTTP 接口语义

准备 Attachment：

```http
POST /api/v1/cases/{case_id}/attachments
Content-Type: application/json
```

上传内容：

```http
PUT /api/v1/attachments/{attachment_id}/content
Content-Type: application/octet-stream
```

下载 Artifact：

```http
GET /api/v1/artifacts/{artifact_id}/content
```

PrepareAttachment 返回的 UploadDescriptor 固定含 `method=PUT`、`max_bytes=2684354560`、`expires_at=null`，以及恰好四个必填 header 键：`Idempotency-Key=attachment_id`、`Content-Type`、`Content-Length`、`X-Content-SHA256`；后两者若返回 null，客户端必须在 PUT 前计算并替换，不能省略。正式字段、错误码、流式上传头和限制见 [S06](v1-specs/S06-mcp-http-cli.md)；单 Attachment 上限 2.5 GiB，单 Case 正式文件总量上限 5 GiB。

HTTP 完成 Attachment 发布后只改变文件对象状态，不自动创建诊断 Job。Client Access Skill 随后把需要采用的 Attachment ID 放入 `SubmitSupplement`。

### 10.6 有限同步等待

服务端底层统一使用异步 Job：

- 客户端可以立即返回并后续查询；
- 客户端也可以有限等待同一个异步 Job；
- Job 完成或进入等待用户状态时立即返回；
- 等待超时后自然转为异步响应；
- 超时不取消、不重建、不重复提交 Job。

## 11. 并发、幂等与一致性

### 11.1 Case 内并发

- 同一个 Case 同时只允许一个活跃 Job。
- 不同 Case 可以同时存在 PENDING Job，但 V1 Agent Job 全局执行并发固定为 1。
- 同一个 Case 的用户写命令和 JobOutcome 应串行化或使用条件更新。
- 所有 Case、活跃 Job 和命令状态更新使用 `case_revision` 条件写；JobOutcome 是否仍可合并额外校验 `base_state_revision == DiagnosisState.revision`。
- Job 和 Attachment 等独立资源的生命周期转换使用各自当前状态做条件更新。
- `SubmitSupplement` 只在等待输入或附件的状态下推进诊断；运行中可以继续上传 Attachment，但不能改变已创建 Job 的输入。
- 运行中若要改变问题目标或约束，必须先取消当前 Case 或等待当前 Job 结束；不得把新输入静默注入活跃 Job。

### 11.2 幂等

以下操作需要幂等保护：

- 创建 Case；
- 提交 `SubmitSupplement`；
- 提交 `ResumeCase`；
- 准备 Attachment；
- 发布 Attachment；
- 应用 JobOutcome；
- 取消 Case。

外部写操作使用 `request_id → idempotency_key`；同 operation/key 与相同 Canonical request hash 返回原 business receipt，不增加 revision，同键不同 hash 返回 `IDEMPOTENCY_CONFLICT`。`wait_seconds` 不参与业务 hash，重放时按本次等待值重新读取状态。记录在 V1 不自动删除。

### 11.3 状态提交与分发

- Application Service 必须先持久化 Job，再提交 Dispatcher。
- Dispatcher 只接收已经存在的 `job_id`。
- 重复提交同一 Job 不得创建第二份业务 Job。
- 状态提交成功但进程内分发失败时，Job 保持 `PENDING`；服务启动会自动重投同一 Job，`ResumeCase` 也可主动唤醒它。外部队列和 Outbox 后置。

## 12. 持久化与恢复边界

### 12.1 持久化数据

- Case 和 DiagnosisState；
- Job 和 Typed JobOutcome；
- finalized `job.json`、`job_outcome.json` 执行记录及其业务投递确认；
- RouteDecision、DiagnosisOutcome 和 ReviewAssessment；
- Evidence；
- Attachment 和 Artifact 元数据；
- READY Attachment、Evidence 文件和 Artifact 字节；
- 实际使用的 Profile、Skill、Tool Bundle、Context Policy 和 Output Contract 版本。

V1 使用一个由 `.instance.lock` 独占的 `DATA_ROOT/state.json` 保存全部权威结构化状态；`state.json.prev` 只供人工恢复，不自动 fallback。附件、Evidence、Artifact 和目录型 `LOGPARSE_RUN` 位于 FileResourceStore，状态只保存相对 storage key、大小和 SHA-256。V1 无数据库、无 Docker，精确布局和原子写见 [S02](v1-specs/S02-json-resource-storage.md)。

### 12.2 非持久化运行数据

- Agent Session 完整对话；
- Backend Session Handle 和进程 PID；
- 模型隐藏推理；
- 原始流式事件；
- 未提升为 Evidence 或 Artifact 的临时工具轨迹；
- Job Workspace 及其中可重新物化的文件副本。

### 12.3 服务重启

- 已完成 Case 可以通过 `case_id` 查询。
- 未完成 Case 可以通过已知 `case_id` 从持久化 DiagnosisState 继续。
- 原 Agent Session 不恢复，也不需要恢复。
- 服务重启后先扫描所有未确认 finalized Outcome 并以原字节重放；只有合法 Outcome 全部得到终态 disposition 后，才把确实没有 finalized Outcome 的旧 `runtime_epoch` `RUNNING` Job 通过 S03/S05 条件事务标记为 `INTERRUPTED`。重放或执行记录校验失败时 readiness=false，禁止先中断、Resume 或重跑 Agent。
- 服务启动自动重投仍为 `PENDING` 的同一 Job；不会自动为 `INTERRUPTED` Job 创建替代项。
- 用户可以通过幂等的 `ResumeCase` 显式继续被中断的 Case。
- 客户端丢失 `case_id` 时，V1 不负责自动找回 Case。

## 13. Reviewer（复核智能体）规则

Reviewer 必须检查：

- 每个最终结论是否有 Evidence 引用；
- Evidence 是否属于当前 Case；
- Evidence 是否来自 Job 固定的版本；
- 是否把未验证假设写成确认事实；
- 是否重新采用已经被排除的假设；
- 是否使用被新 ProblemSpec 替代的旧约束；
- 结论是否满足当前完成条件；
- 是否存在相互冲突但未解释的 Evidence。

Reviewer 只输出 ReviewAssessment，不直接写 Case、不创建下一 Job、不直接调用 Specialist。

## 14. 安全基线

V1 仍按受控内网、用户可信、文件可信的假设设计，不默认增加：

- TLS；
- 用户认证；
- 用户级 Case 隔离；
- 一次性上传凭证；
- 本地文件路径白名单；
- 上传前二次确认；
- Agent 操作系统级沙箱；
- Tool Bundle 的强权限隔离。

已知影响：

- 能访问服务地址的主体可能调用 Case 和文件接口；
- Case ID、Attachment ID 和 Artifact ID 只是资源标识，不是授权凭证；
- 网络传输可能被观察或修改；
- Client Access Skill 取得 Shell 权限后，可以读取当前用户权限范围内的其他本地文件；
- 用户提供的本地路径、上传 URL 和上传命令可能进入 Agent、工具或 Shell 日志；
- Agent 可以访问服务账号本身有权限访问的资源；
- 错误或恶意 Skill 可能产生超出预期的工具行为；
- 持久化问题描述、日志和诊断结果会延长敏感内容的保留时间。

这些限制是当前基线的显式风险接受，不代表推荐用于不受控网络。后续增加安全能力时，不应改变 Case、Job、Attachment 和 Artifact 的基本业务语义。

## 15. 兼容与演进规则

- 正式 HTTP 接口从 `/api/v1` 开始。
- 新增响应字段默认可选，不静默改变已有字段语义。
- 破坏性 HTTP 变更使用新的主版本。
- MCP 返回结构化上传信息，不返回绑定某种 Shell 的完整命令。
- Application Service 使用协议无关的内部类型。
- 后续 Web、普通 CLI 或其他客户端复用相同 Application Service。
- Session Cache、JobAttempt、独立 JobContextManifest、通用事务 Outbox、外部队列和多实例 Worker 可以后续增加；V1 已把 finalized Outcome 执行记录作为窄 durable outbox，但不得让 Agent Session 重新成为正确性来源。

## 16. V1 验收条件

V1 实现与总装必须通过以下架构验收：

1. 删除或关闭所有 Agent Session 后，未完成 Case 仍能根据 Repository 创建下一 Job。
2. 同一个 Job 在新 Session 中可以获得完整的业务输入。
3. Job 的 `context_snapshot`、固定资源引用以及固定 Profile / Skill / Tool / Context Policy / Output Contract 版本共同足以重现结构化业务输入，不依赖历史聊天或事件回放。
4. Runtime 不会为旧 Job 读取执行时最新的 DiagnosisState 或 Attachment。
5. 下一 Job 的快照来自同一提交中转换后的 DiagnosisState，能够看到本次接受的事实、待补要求和候选结论。
6. Job 创建后新增的 Attachment 不会进入该 Job。
7. `SubmitSupplement` 重复提交不会重复增加状态版本或创建 Job。
8. JobOutcome 的基础版本、活跃 Job 或固定复核目标不匹配时不会覆盖当前状态。
9. 同一个 JobOutcome 重复提交不会重复应用 DiagnosisStateDelta。
10. Worker 和 Runtime 只能提交候选 Evidence / Artifact，不能直接创建正式业务记录。
11. 未被显式发布的临时文件和工具结果不会成为跨 Job 依赖。
12. Reviewer 不读取 Specialist 完整对话或执行时最新 DiagnosisState。
13. `PASS` 只能接受 REVIEW Job 固定的候选结论；未通过 Reviewer 的候选结论不能进入 `RESOLVED`。
14. 中断的 REVIEW Job 恢复后仍执行 REVIEW，不能绕过复核门禁。
15. Attachment 未达到 `READY` 时不会被 Worker 消费。
16. 必需上下文超过预算时明确返回 `CONTEXT_LIMIT（上下文超限）`，不静默裁剪。
17. 同一 `job_id` 重复分发只有一次 `PENDING → RUNNING` 认领成功。
18. Remote MCP 和 HTTP 不各自实现一套 Case 状态机。
19. 一个进程独占 `DATA_ROOT`，损坏 `state.json` 时 readiness 失败且不自动使用 `.prev`。
20. V1 不需要数据库或 Docker 即可启动、诊断、重启恢复和导出状态。
21. Router、Specialist、Reviewer 的默认上下文上限分别为 131072、204800、204800 bytes。
22. `CLAUDE_COMMAND` 的解析、argv、stdin 和 Windows shim 行为通过 `issue-locator` characterization tests；唯一环境差异是移除所有 raw/既有 logparse 保留变量，并只注入本 Job broker endpoint/token。
23. 无故障 RPC 超时场景只索要一次日志；首次 Job 调用一次 `logparse parse`，补参后的新 Job 复用已接受 `LOGPARSE_RUN`，总 parse count 为 1；正式 Artifact 接受前的失败恢复遵循至少一次语义，不宣称全故障 exactly-once。
24. `STALE` 只作为 Outcome disposition，JobStatus 中不存在该值。
25. 单 Attachment 2.5 GiB、单 Case 5 GiB、Job 30 分钟、日志 64 MiB、Workspace 1 GiB 和全局并发 1 的边界均被验证。
26. Runtime finalized Outcome 后 Agent 调用次数永久保持 1；同进程投递重试与重启恢复都重放同一 receipt，且启动顺序严格为 replay-before-interrupt。
27. 同 attachment_id 并发 PUT 串行化；发布成功但 commit 失败后，相同字节采用既有目标，不同字节返回 `IDEMPOTENCY_CONFLICT`，Case 全批容量检查不存在 partial publish。
28. job-scoped logparse broker 拒绝跨 Job、第二次 parse、任意 argv 和 direct-CLI 降级；关闭或取消后 token 失效且全部子进程被回收，endpoint/token 不进入日志、Workspace 输出、Outcome 或响应。

## 17. 详细设计权威落点

原待细化事项均已有唯一归属：

| 范围 | 权威说明书 |
|---|---|
| 公共 Schema、Port、状态、错误码、revision 和限制 | [S00](v1-specs/S00-contract-freeze.md) |
| Case/Job 状态机、DiagnosisStateDelta、Coordinator、Review 转换 | [S01](v1-specs/S01-domain-coordinator.md) |
| `state.json`、实例锁、原子写、资源布局、保留与孤立清理 | [S02](v1-specs/S02-json-resource-storage.md) |
| 应用命令、幂等、Outcome 技术校验、提案发布与条件提交 | [S03](v1-specs/S03-application-service.md) |
| 字节预算、Workspace、Catalog、`CLAUDE_COMMAND` 与 Outcome 文件 | [S04](v1-specs/S04-runtime-context-backend.md) |
| Dispatcher、单 worker、runtime epoch、取消、启动恢复与 Resume | [S05](v1-specs/S05-scheduler-recovery.md) |
| Remote MCP、HTTP、CLI、配置、上传限制、错误映射和 Client Access Skill | [S06](v1-specs/S06-mcp-http-cli.md) |
| Diagnosis Skill 生成器 2.0.0、logparse 与解析复用 | [S07](v1-specs/S07-skill-logparse.md) |
| 依赖批次、接缝、RPC 超时 E2E、返工和 PostgreSQL 边界 | [S08](v1-composition-spec.md) |

## 18. 后续实施入口

本次只完成并提交设计分册，不创建产品代码、开发任务、额外分支、worktree 或 PR。只有用户另行明确要求“开始实现”后，才按 S08 执行：

1. S00 先冻结机器合同和工程骨架；
2. S01～S07 以冻结 Port 和 Fake 独立开发、验证；
3. S08 按固定顺序完成接缝与总装。

所有未来开发任务使用 `model: gpt-5.6-sol`、`reasoning_effort: ultra`。该配置只约束 Codex 开发任务，不进入 Problem Locator 运行时。
