# 目标问题定位静态架构

状态：多 Agent 交叉审核通过
更新时间：2026-07-27

## 1. 文档定位

本图在当前 V1 设计主干上增加目标架构中的 Evidence Reviewer，用于统一表达客户端接入、确定性编排、类型化任务执行、上下文读取、持久化边界和异步任务结果闭环。

Evidence Reviewer 是目标架构新增能力，不属于当前 V1 实现范围。图中没有引入固定业务领域 Agent、Agent Team、红蓝对抗、动态 Skill Registry 或特定工作流框架依赖。

## 2. 静态架构

```mermaid
flowchart TB
    subgraph Client["客户端 / Client"]
        User["用户<br/>User"] --> CLI["智能体命令行（含客户端 Agent）<br/>Agent CLI / Client Agent"]
        CLI --> ClientSkill["客户端接入 Skill<br/>Client Access Skill"]
    end

    subgraph Service["问题定位服务 / Problem Diagnosis Service"]
        MCP["远程 MCP 接入适配器<br/>Remote MCP Adapter"]
        HTTP["HTTP 文件接入适配器<br/>HTTP File Adapter"]

        App["应用服务<br/>Application Service<br/>应用命令与 Case / Job 管理<br/>唯一业务状态写入入口"]
        Coordinator["诊断协调器<br/>Diagnosis Coordinator<br/>确定性、无副作用的下一步决策"]

        subgraph Persistence["持久化边界 / Persistence Boundary"]
            Repository["结构化 Case Repository<br/>Structured Case Repository<br/>Case · Job · JobOutcome · RouteDecision<br/>Evidence · Handoff · Attachment / Artifact Metadata"]
            BlobStore["文件字节存储<br/>BlobStore<br/>READY Attachment · Published Artifact"]
        end

        Dispatcher["进程内任务分发器<br/>In-process Job Dispatcher"]

        subgraph Workers["类型化工作器 / Typed Workers"]
            RoutingWorker["路由工作器<br/>Routing Worker<br/>执行 Router Agent"]
            DiagnosisWorker["专项 Skill 诊断工作器<br/>Skill Diagnosis Worker<br/>执行 Specialist Agent + 目标 Diagnosis Skill"]
            ReviewWorker["证据复核工作器<br/>Evidence Review Worker<br/>执行 Reviewer Agent（目标架构新增）"]
        end

        Runtime["共享诊断运行时<br/>Shared Diagnosis Runtime<br/>Profile · Skill · Tool · Workspace · Session 装配"]
        Catalog["随服务发布的只读版本化 Diagnosis Skill 目录<br/>Bundled Versioned Diagnosis Skill Catalog<br/>skill_id@version"]
        Resources["运行时资源<br/>Runtime Resources<br/>Agent Profile Catalog · Tool Bundle Provider<br/>Case Workspace Manager · Case Session Registry"]
        Backend["智能体执行后端<br/>Agent Backend<br/>管理物理 Agent Sessions"]
        Outcome["类型化任务结果<br/>Typed JobOutcome<br/>RouteDecision · DiagnosisOutcome · ReviewAssessment*"]

        MCP --> App
        HTTP --> App

        App -->|"CaseSnapshot + Trigger<br/>请求下一步决策"| Coordinator
        App -->|"读取并写入业务记录<br/>Read / Write Business Records"| Repository
        App -->|"保存或读取持久化文件<br/>Store / Read Persistent Files"| BlobStore
        App -->|"状态提交后提交类型化任务<br/>Submit Typed Job After State Commit"| Dispatcher

        Dispatcher --> RoutingWorker
        Dispatcher --> DiagnosisWorker
        Dispatcher --> ReviewWorker

        RoutingWorker --> Runtime
        DiagnosisWorker --> Runtime
        ReviewWorker --> Runtime

        Runtime --> Catalog
        Runtime --> Resources
        Runtime -->|"按 Job 固定引用只读加载结构化上下文<br/>Read Context by Fixed Job References"| Repository
        Runtime -->|"通过 Workspace Manager 只读物化 READY 附件<br/>Materialize READY Attachments Read-only"| BlobStore
        Runtime --> Backend

        Runtime -.->|"生成、校验并标准化<br/>Produce, Validate and Normalize"| Outcome
        Outcome -.->|"由 Worker / Dispatcher 回送<br/>Reported by Worker / Dispatcher"| App
    end

    ClientSkill -->|"结构化控制命令与补充输入<br/>Structured Commands / Supplemental Input"| MCP
    ClientSkill -->|"附件上传与产物下载<br/>Attachment Upload / Artifact Download"| HTTP
```

`* ReviewAssessment` 是目标架构新增的结构化复核结果，其字段和枚举留到详细设计。Reviewer 只提供复核判断，不直接修改 Case、不创建后续 Job，也不直接调用 Specialist；下一步仍由 Coordinator 决定。

## 3. 图示约定

- 实线：调用或依赖；
- 虚线：异步产生的业务结果；
- 返回值及用户多轮交互不单独画；
- Reviewer 是目标架构新增，其余保持当前设计主干。

由于普通返回值不单独画，Coordinator 返回的下一步决策没有反向箭头；Application Service 根据该决策更新业务状态、创建 Job，并在状态提交后交给 Dispatcher。

## 4. 已统一的职责边界

- Application Service 是 Case、Job、JobOutcome、Evidence 和 Handoff 的唯一业务写入入口。它读取当前 Case 后先调用 Coordinator，再在同一业务事务中保存 Outcome、应用状态变化并创建可选的下一 Job。
- Diagnosis Coordinator 是确定性、无副作用的决策组件，只根据 `CaseSnapshot + Trigger` 计算下一步；它不读写 Repository、不提交 Dispatcher，也不调用 Agent。
- Dispatcher、Worker、Runtime 和 Agent Backend 只负责执行已创建的 Job，不修改 Case，也不创建后续 Job。
- Runtime 按 Job 创建时固定的上下文引用只读加载结构化数据，并通过 Case Workspace Manager 将 `READY` 文件物化到派生工作区；Workspace 和 Agent Session 都不是业务状态权威来源。
- Runtime 生成、校验并标准化 `Typed JobOutcome`，由 Worker / Dispatcher 异步回送 Application Service，再进入相同的持久化和决策闭环。
- Repository 保存结构化业务记录；BlobStore 保存 Attachment 和 Artifact 文件字节。二者是不同的逻辑持久化边界。
- Diagnosis Skill Catalog 随服务版本发布、启动时加载、运行期只读；`skill_id@version` 不可被原地覆盖。
