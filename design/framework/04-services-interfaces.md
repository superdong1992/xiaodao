# 会话服务、报告追问、经验记忆与接口详细设计

本章按当前工作区源码说明 `agent`、`followup`、`memory` 和 `interfaces` 四个模块，共 35 个 Python 文件。模块中保留的 V1、V2、V3 名称分别描述对应数据合同，不能据此判断整个系统的版本。本文描述实现，不代替接口合同、部署配置或 Test Flow 验证结论。

## 1. 模块职责与依赖方向

| 模块 | 负责的状态与行为 | 主要依赖 | 不承担的职责 |
| --- | --- | --- | --- |
| `agent` | 网站会话、每轮诊断、消息、附件、事件、停止和删除 | Application 命令/查询、Case 仓库、Intake Engine | 不直接决定诊断结论；正式诊断仍由 Case 命令驱动 |
| `followup` | 围绕已发布报告的问答、日志副本、独立事件和停止状态 | Agent 归属和使用租约、数据库、AgentBackend | 不修改原 Case、原报告和原 Agent 事件合同 |
| `memory` | 点赞/点踩、一次性经验提炼、有限的历史经验检索 | 已发布 Generic V2 报告、数据库、AgentBackend | 不把点赞当成根因已证实，也不把历史经验当成本次证据 |
| `interfaces` | HTTP、SSE、MCP、客户端传输、认证与公开结果投影 | 冻结的命令/查询端口和可选 Agent 服务 | 不另建诊断状态机，不在读取接口中启动模型 |

这些模块共用 `CaseStateRepository` 提供的数据库事务。生产配置使用 PostgreSQL；不带 `database_url` 的直接构造保留 SQLite 参考实现，用于离线工具与确定性测试。各业务 Store 使用显式 SQL 方言辅助函数，不通过整段 SQL 的隐式翻译模拟 PostgreSQL。

## 2. 网站会话的数据流

1. `create_conversation` 按归属和 `request_id` 创建会话及第一轮 `run_id`，本身不调用模型。
2. `send_message` 校验文字和已上传附件，保存消息与 `message.accepted` 事件。终态会话可以产生新一轮；显式指定 `target_run_id` 时，旧轮次不能被悄悄替换。
3. Intake 工作线程领取待处理会话。第一条有效问题使用确定性模板创建 Case，完整保留原始文字；不会先让模型改写问题再创建 Case。
4. Case 路由后，如果存在待补充输入，服务构造包含用户消息、开放要求、已冻结事实和附件元数据的 `IntakeInput`。模型只提议补充值；服务校验来源引用及约束后，才发出 `SubmitSupplement`。
5. Case 提交会同步投影会话状态和公开事件。详情查询从相互一致的会话投影及 Case 快照生成 `ConversationDetail`，按 `include` 选择历史、报告与产物。
6. SSE 只重放数据库内的事件。客户端断开不会停止诊断，重新连接也不会重复 Intake 或诊断模型调用。

### 2.1 幂等、轮次与命令交付

会话包含多个独立 `run_id`，每轮关联自己的 Case、消息、派发记录和附件导入记录。`run_scope` 将内部读写固定到一轮，避免后台旧结果写入用户刚开启的新轮次。创建请求按归属计算稳定键；消息重放校验指纹，不允许同一个请求标识更换内容。

服务在调用 Application 前保存完整命令，包括初始 Case revision。已接收命令可以重放其幂等交付，但不能据此重新运行一个交付结果未知的模型。Intake 的输入、工作区标识和完成决策也分别保存：已完成决策可复用，未完成的旧模型调用会按中断处理。

Generic 诊断途中补交日志有单独流程。服务先校验所选附件，再保存 `RestartGenericDiagnosis` 或 `MarkInitialLogArchiveExpected` 意图。路由若已从 ROUTE 进入 Generic 或专用 Skill，会基于当前 Case 再决定如何接入。多份日志不能静默只取第一份；后续明确选择会替换尚未采用的旧选择。

### 2.2 停止、删除与失败

停止先持久化 `CANCELLING` 和停止请求，再发送取消信号。独立管理线程处理取消命令、等待 Intake/Dispatcher 退出并确认关联 Case 未变化后，才完成停止。因此模型阻塞时，停止入口仍可用；取消提交失败时也不会提前显示 `CANCELLED`。

删除先写入删除标记并撤销可见性，同时撤销追问与经验，再由清理服务处理精确资源路径。清理必须取得会话及 Case 的空闲租约，并确认诊断、归档和停止活动已经结束。清理清单先持久化，随后解除数据库引用、移入隔离目录并删除；中途失败保留任务以便重试。

公开失败由固定错误码、阶段和诊断标识组成，任意异常正文不能进入用户结果。数据库持久化出现不确定性时，服务可暂停接收工作；不会为了生成一个看似完整的终态而覆盖尚未确认的交付结果。报告与归档独立：正式报告已发布但归档状态提交失败时，报告仍可读取，归档状态标为暂时无法确认。

### 2.3 `agent` 逐文件设计

| 文件 | 职责、关键对象与输入输出 |
| --- | --- |
| [agent/__init__.py](../../src/problem_locator/agent/__init__.py) | 包说明，界定网站会话围绕核心 Case 工作流构建；没有启动副作用或公共重导出。 |
| [agent/models.py](../../src/problem_locator/agent/models.py) | 定义严格 Pydantic 合同。`SendMessageRequest` 限制消息为 65,536 UTF-8 字节、最多 20 个不重复附件且不能全空；`ConversationDetail` 为 v3，`AgentEvent` 为 v2，包含 `run_id`。`ConversationReportView` 区分诊断 v3、Markdown、历史 Generic v1；未请求的详情字段为 `null`。事件根据 `type` 校验精确载荷字段，产物只允许公开下载元数据。`AgentStoreError` 统一传递错误码、HTTP 状态、详情与可重试标记。 |
| [agent/store.py](../../src/problem_locator/agent/store.py) | `AgentStore` 是会话事务中心。维护会话、轮次、消息、事件、派发、采用关系、附件、导入映射、停止请求、删除标记和清理任务。`upgrade_agent_storage_v2` 补齐轮次结构与索引；`create_conversation`、`submit_message`、`request_stop`、`request_delete` 是写入口。`read_conversation` 对齐会话与 Case 快照；`_project_case` 把 Case 提交变为用户可见状态；`recover` 识别旧运行期未完成任务并记录中断。游标包含作用域，目录分页按 owner 过滤，历史清理后记录已裁剪事件边界。 |
| [agent/service.py](../../src/problem_locator/agent/service.py) | `AgentConversationService` 编排公开操作与后台线程。输入为会话/轮次标识、owner、文字或附件；输出为合同对象。`_advance` 执行 Case 优先的 Intake 流程；`_execute_frozen_command` 保存命令后调用 Application；`_advance_generic_supplement` 在文件导入前后重新检查最新附件选择。`operation_lease` 把归属校验与使用计数绑定；`_conversation_detail`、`_report_view` 组合报告投影；`control_once` 仅重试已保存的管理意图。 |
| [agent/intake.py](../../src/problem_locator/agent/intake.py) | 定义 `IntakeInput`、`IntakeValue`、`IntakeDecision`、`IntakeEngine` 和 `ClaudeIntakeEngine`。`build_initial_problem_spec` 生成中性创建模板；`validate_intake_decision` 检查值对应的用户消息和原文片段、开放输入要求、已冻结事实及输入约束。决策为 `NEED_CLARIFICATION`、`SUBMIT_SUPPLEMENT` 或 `NEW_CASE_REQUIRED`。模型不读取附件内容，运行使用 `file_access="none"`；Intake 配置/输出版本为 `1.3.1`，调用上限为 1，单次墙钟限制 120 秒。提取修正保留无正文的处理回执，JSON 提取审计写入该次工作区。 |
| [agent/uploads.py](../../src/problem_locator/agent/uploads.py) | `ConversationUploads` 将上传内容保存到私有附件目录，再按原字节导入 Case。`prepare` 预约元数据；`upload` 检查四项上传头、长度、SHA-256、单链接普通文件及读取期间文件身份，完成后只读发布并标记 READY。重复上传仍消费并验证完整请求体。`import_into_case` 发出 `PrepareAttachment`、`UploadAttachmentContent`，按 `(run_id, attachment_id)` 保存采用关系；`_BorrowedStream` 防止下游提前关闭验证上下文持有的文件描述符。 |
| [agent/failures.py](../../src/problem_locator/agent/failures.py) | `public_failure` 将内部异常转成固定、安全、有限的公开诊断，最多保留 8 个合法字段位置，不输出原值。`interrupted_execution_failure` 按当前未被替代的 Job、时间和处理记录识别唯一中断原因，避免借用历史失败。`exception_code`、`exception_details` 适配应用异常。 |
| [agent/usage.py](../../src/problem_locator/agent/usage.py) | `ConversationUsageGuard` 维护会话使用计数和清理集合。`acquire` 返回读写/模型使用租约；`acquire_cleanup_if_idle` 仅在无使用者时取得清理租约。已进入清理的会话拒绝新使用者，退出上下文后释放计数。 |
| [agent/cleanup.py](../../src/problem_locator/agent/cleanup.py) | `ConversationCleanupService.run_once` 领取显式删除任务，取消所属 Case 的诊断/归档，等待所有租约和停止状态结束。`_manifest` 从持久化归属推导 Case、Job、附件及工作区路径，校验 UUID 和固定路径结构。数据库及资源清理可重启；不会调用模型或恢复诊断。 |

## 3. 报告追问的独立边界

报告追问是一个独立工作流。支持来源为成功发布的 Generic V2 Markdown 报告，或 `specialized direct` 模式的 Markdown 报告；需核对 Job、Skill、产物标识、正文和 SHA-256。普通 Methods 结构化报告不能因此自动视为支持追问。

服务观察新报告后，异步复制该报告实际使用的日志。副本必须与执行记录中的日志清单一致，保存到 `jobs/<source_job_id>/followup-inputs`。启用功能前的历史报告不会重新解析旧附件或假设临时日志还在，可以退化为仅报告交流。

提交时固定 `REPORT_ONLY` 或 `REPORT_AND_LOGS`，并先验证上下文预算。每个会话最多一个活跃追问，每份报告最多 100 轮，问题和回答各不超过 65,536 UTF-8 字节，上下文上限 262,144 字节。报告来源达到 7 天后不再接收新追问。日志副本默认单报告预算 1 GiB、总预算 5 GiB，实际可由服务构造参数调整。

追问 Worker 为每条任务创建独立工作区，只调用一次模型。命令支持只读检索时允许 `Read`、`Grep` 并可将完整历史保存在文件；否则不授予文件访问。模型不能运行 Skill、Logparse、命令、网络或子任务，也不能写文件。执行前后都校验输入树。`REPORT_ONLY` 回答强制带有未重新核对原始日志的说明。

追问结果写入 `agent_followup_*` 表及独立事件序列，不改变原报告与原诊断终态。重启时已运行的任务成为 `INTERRUPTED`，不会重新排队调用模型；合法且未过期的 QUEUED 任务可继续。快照复制失败只影响追问日志能力，不能撤回已经提交的正式诊断。

### 3.1 `followup` 逐文件设计

| 文件 | 职责、关键对象与输入输出 |
| --- | --- |
| [followup/__init__.py](../../src/problem_locator/followup/__init__.py) | 重导出 `ReportFollowupService`，作为装配入口。 |
| [followup/models.py](../../src/problem_locator/followup/models.py) | 定义请求、停止请求、回答条目、列表、接收回执和 v1 事件；定义 `FollowupSource` 与字节/轮次限制。校验完成状态必须有回答、失败/中断必须有错误，事件的 run 和 followup 身份必须与正文一致。 |
| [followup/context.py](../../src/problem_locator/followup/context.py) | `context_value` 组织原问题、原报告、历史问答和当前问题；`build_prompt` 与提交阶段共用同一预算算法。支持检索时超大历史转为完整文件与索引，并尽可能保留最近问答；仍超预算则拒绝。`validate_answer` 校验非空 Markdown、字节上限，并补齐报告模式说明。 |
| [followup/service.py](../../src/problem_locator/followup/service.py) | `ReportFollowupService` 校验来源、授权和可用性，装配 `FollowupStore`、`FollowupWorker`、`SnapshotWorker`。`schedule_observe` 只保存有界观察提示；`observe_report` 创建快照任务；`get` 是纯查询，返回禁用、不支持、过期、忙或达到次数上限等原因；`submit`、`stop` 是显式改变追问状态的入口。 |
| [followup/store.py](../../src/problem_locator/followup/store.py) | 管理 metadata、snapshots、tasks、events、stops 五张表，严格验证 storage version 1。按会话加锁，PostgreSQL 领取任务用 `FOR UPDATE ... SKIP LOCKED`；配额使用独立 advisory lock。请求指纹绑定 `run_id + text`，事件序号按报告轮次递增。`revoke` 可参与会话删除事务；`busy`、`purge`、`workspace_ids` 和 `workspace_in_use` 为保留清理提供精确保护信息。 |
| [followup/snapshots.py](../../src/problem_locator/followup/snapshots.py) | `build_snapshot` 校验报告哈希和 Generic/专用诊断日志清单，先预约容量，再受控复制、生成 manifest、设为只读并发布。`ordinary_path`、`read_small` 拒绝越界、链接、非普通文件和读取漂移；`verify_inputs` 同时校验文件集合、大小及哈希；`copy_snapshot` 为追问创建新的输入副本。`SnapshotWorker` 消费观察提示与快照队列，复制失败保存失败状态。 |
| [followup/worker.py](../../src/problem_locator/followup/worker.py) | `FollowupWorker.run_once` 领取一条任务，取得会话使用租约，生成固定上下文和输入文件，执行一次模型后验证输入再发布回答。`_Cancellation` 同时检查关闭信号和数据库任务许可；`cancel` 只命中指定会话/追问。默认执行上限 300 秒，工作区预算为快照预算加 16 MiB。结果提交不确定时保留 RUNNING 供启动恢复识别，不重新执行。 |

## 4. 反馈与经验记忆

反馈目前只接受已发布的 Generic V2 Markdown 报告。读取或写入都要求明确 owner；评分绑定会话、轮次、来源 Job 和报告哈希。请求幂等键按 owner 隔离，旧请求重放返回当前评分，不把后来修改的评分恢复成旧值。

首次点赞为同一 `(run_id, report_sha256)` 创建一次提炼任务。模型从原问题和报告中提炼四组通用信息：问题特征、适用条件、排查步骤与限制。模型不使用工具，输出必须是严格 JSON。经验卡最多 4,096 字节；校验拒绝可识别的实例标识、敏感字段、长原文和指令式内容。该校验是保守过滤，代码没有声称正则表达式可以识别所有姓名或自然语言秘密。

成功或失败后移除任务内的原问题和原报告；未完成来源最多保留 7 天，已完成卡片从完成时间起最多保留 90 天。点踩关闭卡片的 active 标记，重新点赞可在有效期内重新启用已有卡片，不再次调用模型。显式删除会话撤销卡片；自然到期删除来源时，已完成卡片保留原有独立有效期。

检索按 Skill 取有效卡片，用英文标识词、中文双字片段和罕见错误码匹配，最多选一张。候选卡先校验规范 JSON、UUID 与哈希。提示中明确经验来自其他用户点赞、尚未验证，仅供选择排查方向；检索错误返回无卡片，不阻断当前诊断。

### 4.1 `memory` 逐文件设计

| 文件 | 职责、关键对象与输入输出 |
| --- | --- |
| [memory/__init__.py](../../src/problem_locator/memory/__init__.py) | 包说明；没有初始化数据库或启动线程的副作用。 |
| [memory/models.py](../../src/problem_locator/memory/models.py) | `FeedbackRequest` 定义 `LIKE`/`DISLIKE` 与请求标识；`FeedbackView` 保证评分和时间同时存在或同时为空；`FeedbackSource` 保存已发布报告的内部来源身份，不直接作为公开接口。 |
| [memory/service.py](../../src/problem_locator/memory/service.py) | `FeedbackService._source` 从一致的会话/Case 快照校验成功 Generic Job、Skill、报告与产物；`get_feedback`、`put_feedback` 持有会话操作租约并传递 owner。功能关闭或来源不支持时读接口显示 `can_rate=false`，写接口拒绝。 |
| [memory/store.py](../../src/problem_locator/memory/store.py) | `MemoryStore` 管理 `memory_feedback`、`memory_feedback_requests`、`memory_tasks`。事务覆盖评分、去重、配额和任务创建；来源及任务各最多 10,000 条，每份报告最多 128 个反馈请求。`claim_task` 将 PENDING 改为 RUNNING；`finish_task` 再校验卡片并删除原文；`recover` 将遗留 RUNNING 标为 FAILED。`revoke_conversation`、`expire_sources`、`prune` 分别处理显式撤销、自然来源到期及周期清理。 |
| [memory/extraction.py](../../src/problem_locator/memory/extraction.py) | `parse_card_json` 禁止重复字段、未知字段、空数组、超长条目及可识别隐私/指令内容，不从围栏中修补 JSON。`build_extraction_prompt` 验证来源哈希和预算。`MemoryExtractionWorker` 单线程领取一次性任务，使用 `file_access="none"`，默认墙钟限制 120 秒；日志只记录错误类型，不记录模型输出。仅删除自己创建的空工作区，异常文件交给保留清理。 |
| [memory/retrieval.py](../../src/problem_locator/memory/retrieval.py) | `ExperienceRetriever.select` 完成确定性单卡检索，按共同特征数、覆盖比例、更新时间和稳定 ID 排序；一般至少两个共同特征，足够长且唯一的错误码允许单独命中。`MemorySelection.receipt` 返回卡片与引用文字的哈希，供执行记录绑定本次实际提示。 |

## 5. HTTP、SSE、MCP 与归属认证

### 5.1 入口及安全边界

`create_http_app` 创建共享 FastAPI/ASGI 应用，提供健康检查、Case 控制、附件传输、产物下载、Agent 会话和追问接口，并挂载官方 MCP Streamable HTTP 管理器。HTTP 端口在工作线程中执行同步 Application 操作，避免阻塞 ASGI 事件循环。

生产网站默认使用 Redis session。中间件从唯一 Cookie 读取 session，在 `airobot2-session:<session_id>` 中查找 `user.userid`，将 `[owner_namespace, userid]` 的 JSON 字节哈希为 owner_key。Redis 模式先剥离请求中的 `X-Agent-Owner-Key`，身份存放在请求自己的 ASGI scope 中。缺少有效登录返回 401，来源不可信返回 403，Redis 不可用返回可重试的 503，不能回退为信任用户自报归属。

`trusted_header` 是显式配置模式；底层无认证配置的端口注入入口也保留该兼容接缝。此时服务依赖可信网站后端提供合法且唯一的归属头。这个模式本身不会证明任意公网请求头可信，部署时必须保证入口边界。

Agent 自身接口要求 owner。Core 的 Case、附件和产物路由若关联 Agent 会话，也必须校验归属，并在整个下载流期间持有会话使用租约。没有关联到 Agent 的 Core 对象继续遵循原 Core 端口模型；这里不能理解为所有 MCP/Core 操作都使用网站 Cookie 认证。

### 5.2 流式传输与失败处理

上传在读取正文前验证唯一的 `Idempotency-Key`、`Content-Type`、`Content-Length`、`X-Content-SHA256`；幂等键必须等于附件 ID。`AsyncRequestBinaryStream` 把异步请求体桥接为同步只向前读取的 BinaryStream，每次最多合并 1 MiB，避免一次性读入整个附件。HTTP 被取消时先中止输入，等待不可直接取消的端口线程结束，并释放它可能返回的流资源。

Agent SSE 和追问 SSE 均发送初始 `: connected`、周期心跳及规范 JSON 的 `data:` 帧。帧内保存 `sequence`，当前实现没有单独发送 SSE 的 `id:` 或 `event:` 行；客户端应保存正文序号，并在重连时使用 `Last-Event-ID`。服务严格检查事件连续性和归属，达到终态且积压事件已发完后关闭。响应头已发送后发生错误，只关闭连接，不拼造业务事件或输出内部异常。

MCP 公开七个工具：创建、预约附件、补充、查询、恢复、取消、列出产物。输入根属性保持标量、nullable 标量或标量数组；问题八字段位于根层，事实与补充值使用等长 names/values 数组。内部 Application 命令可使用对象结构，不能反过来把嵌套对象暴露为 MCP 输入。MCP 默认返回精简 `CaseProgress`，完整 Case 详情需显式查询。

### 5.3 `interfaces` 逐文件设计

| 文件 | 职责、关键对象与输入输出 |
| --- | --- |
| [interfaces/__init__.py](../../src/problem_locator/interfaces/__init__.py) | 重导出 `InterfaceDependencies` 与 `create_asgi_app`，提供进程边界适配器的统一导入入口。 |
| [interfaces/composition_hooks.py](../../src/problem_locator/interfaces/composition_hooks.py) | `InterfaceDependencies` 注入命令、查询、管理端口、公共 URL、可选 Agent 和网站认证配置；`create_asgi_app` 延迟导入并调用 `create_http_app`，减少装配循环依赖。 |
| [interfaces/http_app.py](../../src/problem_locator/interfaces/http_app.py) | 共享 FastAPI 应用工厂。注册健康、Case、补充、附件预约/上传、产物列表/下载和 MCP 路由；装配 CORS、HTTP 诊断、网站 session 与 Agent Case 访问中间件。`_port_call` 屏蔽取消直到同步操作结束，`_ClosingStreamingResponse` 保证异常时关闭流；`parse_upload_headers` 在读正文前验证头。REST OpenAPI overlay 补充字段语义、条件响应和序列化必填约束，不修改核心合同。 |
| [interfaces/agent_http.py](../../src/problem_locator/interfaces/agent_http.py) | `register_agent_routes` 注册会话创建/列表/详情/重命名/停止/删除、消息、反馈、SSE、附件和会话文件下载。请求模型禁止未知字段；`_query` 拒绝重复及未知参数；`_owner_key` 优先使用认证 scope。`respond` 二次校验服务输出身份，按选中 run 生成下载 URL。`_checked_batch` 校验连续事件和单帧大小，`_events` 只读重放。 |
| [interfaces/followup_http.py](../../src/problem_locator/interfaces/followup_http.py) | 在 `/api/v1/agent/conversations/{conversation_id}/runs/{run_id}/followups` 注册追问提交、读取、停止和事件流。复用归属/错误包装，返回独立 Followup 合同。每次最多读取 20 条事件，单帧不超过 1 MiB，校验 conversation、run、followup 身份；纯读取不触发快照或模型。 |
| [interfaces/session_auth.py](../../src/problem_locator/interfaces/session_auth.py) | `WebsiteAuthConfig`、`WebsiteIdentity`、`RedisSessionAuthenticator` 与 ASGI 中间件。Cookie 重复或格式异常直接拒绝；跨站 `Sec-Fetch-Site` 被拒绝，非安全方法提供 Origin 时必须命中允许列表。Redis 调用限制 2 秒且不自动重试，连接最多 20 个；退出生命周期时关闭客户端。错误响应不带 session、Redis 地址或凭据。 |
| [interfaces/mcp_server.py](../../src/problem_locator/interfaces/mcp_server.py) | 七个严格输入模型生成工具 schema；`McpAdapter.call` 统一日志、验证、命令转换、错误包装和结果投影。`create_mcp_transport` 使用官方 `Server` 与 `StreamableHTTPSessionManager`，工具列表记录 schema 摘要；`McpHttpApplication` 只转交 ASGI 请求。不会用 `json.loads` 或 Hook 修补被客户端字符串化的嵌套输入。 |
| [interfaces/rest_models.py](../../src/problem_locator/interfaces/rest_models.py) | 定义浏览器 REST 请求和响应包装。`CreateCaseBody`、`ProblemSpecBody`、`NamedValueBody`、`SubmitSupplementBody` 校验重复事实、空补充及限制；REST 可使用明确的嵌套 JSON，独立于 MCP 扁平合同。`WebUploadDescriptor` 把浏览器可设置的头与浏览器负责的内容长度分开表达；还定义上传完成、产物列表、健康与成功/失败包装。 |
| [interfaces/progress.py](../../src/problem_locator/interfaces/progress.py) | `CaseProgress.from_view` 从完整 Case 提取状态、当前 Job、开放 requirements、附件、产物与失败；去掉已关闭补充要求。`McpApplicationResponse` 和 `McpCaseQueryResponse` 为精简/完整查询提供明确类型。 |
| [interfaces/projections.py](../../src/problem_locator/interfaces/projections.py) | `append_public_path` 保留公共基地址路径前缀；`upload_descriptor`、`web_upload_descriptor` 从业务回执生成上传描述；`artifact_view` 只给可下载产物加公共 URL。输入是内部已验证摘要，输出不泄露存储路径。 |
| [interfaces/error_mapping.py](../../src/problem_locator/interfaces/error_mapping.py) | `model_json` 序列化合同；`success_envelope`、`error_envelope` 统一 `ok/data/error`；`http_status_for`、`cli_exit_for` 使用合同错误映射。`validation_diagnostics` 与 `validation_error_from` 把 Pydantic/类型错误转成受限字段位置和公开值类型，避免原始无界异常直接进入协议。 |
| [interfaces/http_streaming.py](../../src/problem_locator/interfaces/http_streaming.py) | `AsyncRequestBinaryStream` 在工作线程与事件循环间桥接上传，串行 read、保留一个未消费 ASGI 块的 memoryview，并支持 abort/close。`iterate_binary_stream` 分块读取同步下载流并保证关闭。内存随分块预算变化，不随整个附件大小增长。 |
| [interfaces/client_access.py](../../src/problem_locator/interfaces/client_access.py) | `ClientAccessWorkflow` 封装 MCP 控制加 HTTP 文件传输：创建/查询/恢复/取消 Case、补充、预约后上传再提交、产物列表与下载。`SystemCurl` 使用参数数组执行 curl，限制进程输出和传输大小；检查 HTTP URL、响应合同、附件描述和哈希。下载先写临时文件、核对大小/SHA-256 后发布到目标，避免把部分内容当作完成文件。这是客户端访问库，不是本机 MCP Server、代理或 Hook。 |

## 6. 维护时必须保持的关系

- 新增模型活动必须有显式写入口、固定来源、预算、领取状态和结果提交状态。GET、详情投影、SSE 重连都不能成为隐式启动入口。
- 会话、轮次、Case、Job 和报告来源标识不能互相代用。公开结果、下载授权、反馈和追问均需检查各自绑定关系。
- 接收业务请求与模型实际执行是不同阶段。已保存的命令可幂等交付，结果未知的模型调用不能直接重跑。
- 自然保留清理、用户显式删除和后台工作取消必须使用各自的事务及租约；不要用单独删除数据库行或递归删除目录替代完整流程。
- SSE 消息、HTTP 错误、MCP schema 与 OpenAPI 各有自己的合同。增加一处字段时，应分别检查序列化、归属、公开信息范围及对应调用方。
