# S06：Remote MCP、HTTP、CLI 与配置实施说明书

- 状态：V1 独立开发合同
- 未来 Codex 开发任务模型：`gpt-5.6-sol`，`reasoning_effort=ultra`
- 公共合同：[S00-contract-freeze.md](S00-contract-freeze.md)
- 组合验收：[../v1-composition-spec.md](../v1-composition-spec.md)

## 1. 目标

本切片实现 Problem Locator 的全部进程外入口和启动配置：

1. 使用官方 MCP Python SDK 在 `/mcp` 暴露七个 Remote MCP 工具。
2. 使用 FastAPI 在 `/api/v1` 暴露健康检查、Attachment 准备/上传和 Artifact 下载。
3. 实现 `python -m problem_locator` 的 serve、validate-state、export-state 三个入口。
4. 实现 env-file 与进程环境配置，进程环境变量优先。
5. 定义并实现 `problem-locator-client` Client Access Skill，以七个 MCP 工具做控制调用、以系统 `curl` 做文件传输。
6. 所有接口只调用 S00 `ApplicationCommandPort`、`ApplicationQueryPort` 或 `StateAdminPort`，不复制业务规则。
7. 保证远程响应与 Client Access Skill 不暴露内部路径、环境变量、凭据、日志包字节或不可下载的内部 Artifact。

## 2. 非目标

- 不实现 Case/Job 状态机、Coordinator、幂等存储或业务事务。
- 不直接读写 `state.json` 或资源目录；validate/export 也只调用 S00 `StateAdminPort`。
- 不实现 Scheduler、Runtime、Agent Backend、logparse 或 Diagnosis Skill。
- 不实现 Diagnosis Skill、Wiki 生成器或 logparse Skill；它们由 S07 独占。
- 不提供 Web 管理页面、本地 MCP Server、桌面常驻客户端或 Case 自动发现。
- 不实现认证、TLS、细粒度授权、一次性上传凭证、反向回调或公网部署能力。
- 不把本地文件字节放入 MCP 消息；文件字节只经 HTTP。
- 不返回绑定 Bash/PowerShell 的服务端上传命令；MCP 只返回结构化上传描述。
- 不在 Adapter 内维护第二套状态机或重新解释 S00 错误码。

## 3. 上游合同

唯一规范上游是 S00。必须直接使用：

- 核心命令、查询、回执、错误 Envelope、Case/Artifact/Upload 数据模型；
- StateRepository、ResourceStore 等公共 Port，以及 S03 暴露的单写应用入口；
- `idempotency_key`、`expected_case_revision`、有限等待、幂等冲突和 revision 冲突语义；
- Attachment/Artifact 元数据与字节流合同；
- S00 的错误码和 HTTP/MCP 错误映射表。

S01～S05 的具体类不是单元测试前置条件。接口通过依赖注入消费 S00 Port；S08 负责真实装配。

## 4. 唯一文件责任区

本切片是以下路径的唯一所有者：

```text
src/problem_locator/interfaces/**
src/problem_locator/entrypoints/**
.claude/skills/problem-locator-client/**
tests/unit/interfaces/**
handoff/S06.json
```

建议内部布局：

```text
src/problem_locator/interfaces/
├── mcp_server.py
├── http_app.py
├── http_streaming.py
├── error_mapping.py
└── composition_hooks.py

src/problem_locator/entrypoints/
├── settings.py
├── env_file.py
└── cli.py
```

测试 Fixture 只能放入本切片测试子树；不得新增仓库级 `tests/conftest.py`。

## 5. 禁止修改项

- 不得修改 `src/problem_locator/contracts/**`、S01～S05、S07～S08 的责任区、`pyproject.toml` 或锁文件。
- 不得修改 `.claude/skills/wiki-to-diagnosis-skill/**`、`.claude/skills/logparse-diagnose/**`、`.claude/skills/diagnose-service-takeover/**` 或其他 Diagnosis Skill；这些由 S07 独占。
- 除 `.claude/skills/problem-locator-client/**` 外不得修改任何 `.claude/skills/**`；根 `.env.example`、根 README、`src/problem_locator/bootstrap.py` 和公共命令注册文件由 S08 独占。
- 不得直接实例化并写具体 JSON Repository/ResourceStore 的内部结构；真实对象由组合钩子注入。
- 不得新增接口私有错误码；所有业务与系统错误只映射 S00。
- 不得在 MCP 和 HTTP 分别实现状态判断、幂等、revision 或 Attachment 业务规则。
- 不得通过 `wait_seconds` 取消、重建或重复提交 Job。
- 不得将 Case ID、Attachment ID 或 Artifact ID 当成授权凭证。
- 不得在日志记录完整上传 URL 查询凭据、环境变量值、文件字节或服务内部绝对路径。
- 不得把服务部署假设从“受控内网、无认证、无 TLS”静默扩大为公网可用。

## 6. 输入输出契约

### 6.1 Remote MCP 工具

Remote MCP 使用官方 Python SDK 的 Streamable HTTP transport，固定挂载于同一 ASGI 应用的 `/mcp`，采用 stateless + JSON response 模式；业务正确性不得依赖 MCP session、SSE 重连或进程内对话。协议协商、初始化和标准 MCP 错误外壳由 SDK 实现，业务工具错误仍使用本册 `{ok,data,error}` 结果。V1 不启用 CORS，也不提供旧 SSE transport 或 stdio server。

工具名称固定为：

```text
problem_locator_create_case
problem_locator_prepare_attachment
problem_locator_submit_supplement
problem_locator_get_case
problem_locator_resume_case
problem_locator_cancel_case
problem_locator_list_artifacts
```

字段固定为：

| 工具 | 必需输入 | 可选输入 |
|---|---|---|
| `problem_locator_create_case` | `request_id`、`problem_spec` | `initial_user_facts[]`、`wait_seconds` |
| `problem_locator_prepare_attachment` | `request_id`、`case_id`、`expected_case_revision`、`name`、`content_type` | `declared_size`、`declared_sha256` |
| `problem_locator_submit_supplement` | `request_id`、`case_id`、`expected_case_revision`、`inputs`、`attachment_ids` | `wait_seconds` |
| `problem_locator_get_case` | `case_id` | `wait_for_job_id`、`wait_seconds` |
| `problem_locator_resume_case` | `request_id`、`case_id`、`expected_case_revision` | `wait_seconds` |
| `problem_locator_cancel_case` | `request_id`、`case_id`、`expected_case_revision` | 无 |
| `problem_locator_list_artifacts` | `case_id` | 无 |

`problem_spec` 的 JSON 形状严格为 S00 `ProblemSpecInput`：`{statement,expected_behavior,actual_behavior,scope,goals,non_goals,constraints,completion_criteria}`，不得由客户端传 revision。`initial_user_facts[]` 每项严格为 `{name,value}`。`inputs` 是 requirement name 到字符串值的 JSON object，`attachment_ids` 是去重 UUID 数组；两者不能同时为空。name、文本大小、constraints、额外字段拒绝和规范化 hash 规则全部复用 S00，Adapter 不 trim 或改写用户文本。

所有响应 envelope 固定为 `{ok,data,error}`：成功时 `data` 非空且 `error=null`，失败时 `data=null` 且 `error` 是完整 S00 `ApplicationError={code,message,details[],retryable}`；Adapter 不重算 retryable。七个工具的成功 `data` 逐项固定为：

| 工具 | `data` |
|---|---|
| `problem_locator_create_case` | S00 `ApplicationResponse`；`case_view` 非空 |
| `problem_locator_prepare_attachment` | `{application_response,upload}`；前者是 S00 `ApplicationResponse`，后者是完整 `UploadDescriptor` |
| `problem_locator_submit_supplement` | S00 `ApplicationResponse`；`case_view` 非空 |
| `problem_locator_get_case` | `{case_view,wait_timed_out}` |
| `problem_locator_resume_case` | S00 `ApplicationResponse`；`case_view` 非空 |
| `problem_locator_cancel_case` | S00 `ApplicationResponse`；`case_view` 非空 |
| `problem_locator_list_artifacts` | `{artifacts}`，值为公开 `ArtifactView[]` |

`UploadDescriptor` 必须逐字段为 `{attachment_id,method:"PUT",url,required_headers,max_bytes:2684354560,expires_at:null}`；返回 URL 只能位于 `PUBLIC_BASE_URL` 下。`required_headers` 的类型、四个精确键和值逐字采用 S00：Idempotency-Key=attachment_id、Content-Type=已通过 S00 Canonical ContentType grammar 的 prepare 值，已声明的 size/hash 写入对应字符串，未声明项写 null 并由 Client Access Skill 对完整本地文件计算后替换。S06 不解析或规范化 Content-Type，不接受参数、大小写变体或前后空白；只把 Application Service 的结构化业务结果投影为这些形状，不重算状态或修改 business receipt。

ApplicationResponse/CaseQueryResponse 内的 `CaseView.artifacts[]` 保持协议无关 ArtifactSummary，不含 URL；只有 `problem_locator_list_artifacts` 把每个 downloadable summary 加上 `PUBLIC_BASE_URL` 下的 download_url 并投影为 ArtifactView。Client Access Skill 需要下载时先调用 list，不从 CaseView 猜 URL。

规则：

- 所有写工具都必须接收外部字段 `request_id`，并一对一映射为 S00 的 `idempotency_key`；不得同时维护第二个幂等键。
- create/prepare/submit/resume/cancel 只调用 S00 `ApplicationCommandPort`；get/list 只调用 `ApplicationQueryPort`。Adapter 不直接读 Repository 或拼装 CaseView。
- 创建 Case 之外、直接以 Case 聚合为目标的写命令必须接收 `expected_case_revision`。Attachment content PUT 是 S00 唯一例外：它以已准备的 `attachment_id`、Attachment 当前状态和预期内容 hash/size 做条件写，不携带 Case revision。
- 同一 `request_id` 与规范化相同载荷返回首次回执；相同 ID 不同载荷返回 S00 的幂等冲突。
- `problem_locator_submit_supplement` 同时接受结构化事实答案和 READY Attachment 引用，允许部分、分批、幂等提交。
- 查询返回 Case 状态、开放 requirements（含 requirement ID、kind、name、prompt、constraints 和 status）、Job 摘要、已接收资料、最终结果和 Artifact 元数据，不返回内部 storage key。
- `wait_seconds` 的合法范围是 `0..30` 秒；等待结束只查询同一个异步 Job，超时不取消、不重建、不重复分发。
- MCP 结果使用结构化对象，不把 HTTP 文件内容嵌入工具返回值。

### 6.2 HTTP 路由

固定路由：

```text
GET  /live
GET  /ready
POST /api/v1/cases/{case_id}/attachments
PUT  /api/v1/attachments/{attachment_id}/content
GET  /api/v1/artifacts/{artifact_id}/content
```

行为：

- `/live` 仅表示进程和 ASGI 事件循环仍可响应；即使权威状态损坏，它仍返回存活结果。
- `/ready` 只有在配置有效、实例锁已持有、权威状态通过 Schema/引用/不变量校验、必需本地目录可用，且 S05 已依次完成未确认 finalized Outcome replay、无 Outcome 的旧 epoch RUNNING 中断与 PENDING 重投时返回就绪；执行记录损坏或 replay 未完成时必须失败。
- POST Attachment 入口与 MCP prepare 工具构造同一个 S00 Application Command，返回结构化上传描述。
- PUT 把 ASGI request body 适配成 S00 同步 `BinaryStream`，以受控小块读取且在成功、客户端断开或异常时都关闭；不把整个文件读入内存。大小和 SHA-256 由服务端计算并通过 ApplicationCommandPort 交给 Application Service/ResourceStore 完成发布。
- READY Attachment 不自动推进 Case；客户端随后显式调用 SubmitSupplement。
- GET Artifact 只调用 `ApplicationQueryPort.open_artifact` 并按 S00 `BinaryStream.read(max_bytes)` 流式转发结果，EOF/异常后在 `finally` 关闭；内部 `LOGPARSE_RUN` 和尚未通过最终复核的 USER_RESULT 不对用户暴露下载入口。

精确 HTTP 载体：

- `POST /api/v1/cases/{case_id}/attachments` 的 JSON body 为 `{request_id,expected_case_revision,name,content_type,declared_size?,declared_sha256?}`，成功 `data` 与 MCP prepare 的 `{application_response,upload}` 逐字段相同；
- `PUT /api/v1/attachments/{attachment_id}/content` 必须带 S00 descriptor 中全部四个 header：`Idempotency-Key` 必须逐字等于 path 的 attachment_id 并直接作为 UploadAttachmentContent.idempotency_key，不能复用 prepare request_id；`Content-Length` 是实际完整文件十进制字节数，`X-Content-SHA256` 是实际完整文件 64 位小写 hash，`Content-Type` 必须逐字等于 prepare 值；带参数、大小写变化、空白/控制字符或任一非 Canonical 值都以 `VALIDATION_ERROR` 在读取 body 前拒绝；若 prepare 已声明 size/hash，两处必须一致；
- PUT 不接收 `expected_case_revision`。成功 JSON 的 `data` 固定为 `{attachment_id,case_id,status:"READY",case_revision}`；其中 revision 取本次响应重新读取的 `CaseView.case_revision`，Client Access Skill 必须把它用于随后 SubmitSupplement，不能复用 prepare 前的旧值；若其后仍发生并发写而收到 `REVISION_CONFLICT`，先重新 GetCase 再重试；
- `GET /api/v1/artifacts/{artifact_id}/content` 必须带 `case_id` 查询参数；它不承担授权，只用于强制资源归属校验；
- JSON 成功响应使用与 MCP 相同的 `{ok,data,error}` envelope；文件下载成功使用字节流，并返回 `Content-Length`、`Content-Type` 和 `X-Content-SHA256`；
- 请求 `LOGPARSE_RUN` 等内部 Artifact 时返回 `ARTIFACT_NOT_FOUND`，避免暴露内部对象存在性。

固定容量：

- 单 Attachment 最大 `2.5 GiB = 2684354560 bytes`；
- 单 Case 正式文件总量最大 `5 GiB = 5368709120 bytes`。

超过限制时必须在正式资源发布前停止，临时上传按 S03 清理合同处理。

### 6.3 CLI

固定入口：

```text
python -m problem_locator serve --env-file <path>
python -m problem_locator validate-state --data-root <path>
python -m problem_locator export-state --data-root <path> --output <path>
```

- `serve` 启动单服务进程、单 Uvicorn worker；不得允许多个 worker 共享 DATA_ROOT。
- `validate-state` 调用 `StateAdminPort.validate_state`，向 stdout 输出 S00 `ValidationReport` 的 Canonical JSON；损坏时非零退出且不覆盖文件。
- `export-state` 调用 `StateAdminPort.export_state`，把其 `CanonicalJsonBytes<StateExport>` 原样原子写入显式 output；StateExport 含固定 Schema 版本、完整计数、同 generation StateFile 和按 storage key 排序的资源 hash 清单，不修改权威状态。
- CLI 参数错误、配置错误和业务错误使用 S00 的机器错误表示，并向 stderr 输出安全摘要。

### 6.4 配置

至少支持：

```text
DATA_ROOT
PUBLIC_BASE_URL
BIND_HOST
PORT
CLAUDE_COMMAND
SKILL_DIR
LOGPARSE_REPO
LOGPARSE_CONFIG_PATH
LOGPARSE_PYTHON
DFX_LOG_LEVEL
DFX_LOG_DIR
```

V1 的 Context、Job、附件、Case、并发和保留期限制全部是 S00 编译期合同常量，不是 Settings 或环境变量；S02～S05 直接消费同一常量模块，S08 不注入第二份 effective limits。未知的 `*_LIMIT_*`、`*_MAX_*`、`*_RETENTION_*` 或 `JOB_CONCURRENCY` 配置键按额外配置拒绝并返回 `CONFIG_INVALID`，防止运维人员误以为下调已经生效。固定值为 Router `131072`，Specialist/Reviewer `204800`，Job `1800` 秒，日志 `67108864` 字节，Workspace `1073741824` 字节，并发 `1`，附件 `2684354560` 字节，Case 正式文件 `5368709120` 字节，临时资源 `86400` 秒，orphan `604800` 秒。

配置装载固定为：显式 `--env-file` 使用 UTF-8 dotenv 语法加载且不覆盖启动进程中已有变量；随后读取进程环境并构造不可变 Settings。`DATA_ROOT`、`PUBLIC_BASE_URL`、`SKILL_DIR`、`LOGPARSE_REPO`、`LOGPARSE_CONFIG_PATH` 是必需项；四个路径必须是绝对路径，`PUBLIC_BASE_URL` 必须是不含 userinfo 的绝对 HTTP(S) URL。`CLAUDE_COMMAND` 默认 `claude`，`BIND_HOST` 默认 `127.0.0.1`，`PORT` 默认 `8000`，`LOGPARSE_PYTHON` 默认当前 Python 可执行文件，`DFX_LOG_LEVEL` 默认 `INFO` 并只接受 Python 标准日志级别 `DEBUG/INFO/WARNING/ERROR/CRITICAL`。`DFX_LOG_DIR` 可选且必须是绝对路径；旧键 `DFX_LOG_FILE` 即使为空也必须拒绝。未配置目录时 debug 日志目标为 stderr，Journey 日志关闭。

配置 `DFX_LOG_DIR` 后，服务必须把原有单行 JSON DFX 事件写入 `<dir>/debug.jsonl`，并把固定 `schema_version=1` 的语义事件写入 `<dir>/journey.jsonl`。Journey 固定字段为 `schema_version/sequence/timestamp/level/event/correlation_id/request_id/case_id/job_id/job_type/outcome_id/duration_ms/data`，进程内 `sequence` 严格递增；本迭代不保证跨重启连续性，不提供轮转、脱敏、分布式追踪或额外可靠性。运行时写入失败必须 fail-open，不得中断业务流程。每个 HTTP 请求生成并返回 `X-Problem-Locator-Correlation-ID`，同一值贯穿该请求触发的 MCP 和线程任务。

CLI 必须提供 `python -m problem_locator render-journey --case-id <uuid> [--log-dir <absolute-dir>]`。目录未显式传入时读取 `DFX_LOG_DIR`。渲染前必须严格校验完整 Journey 文件，包括 UTF-8、逐行完整性、schema 和连续 sequence；任何错误都不得覆盖既有产物。成功时确定性覆盖 `<dir>/cases/<case_id>/detailed.log` 与 `brief.log`：详细版展示全部语义事件和原始行引用，简略版展示 Case 状态、里程碑、结论、阻塞项和失败点；非终态必须标记为当前快照。退出码固定为成功 `0`、输入错误 `2`、配置或源文件错误 `3`、输出错误 `4`。

组合根只能把不可变 Settings 中的 `LOGPARSE_REPO/LOGPARSE_CONFIG_PATH/LOGPARSE_PYTHON` 三个 raw 值交给 S07 的服务侧启动构造器；S06 不生成工具 ref/fingerprint，S04 也不直接读取 Settings。这三个值不得进入 Agent 环境、Workspace、Context、Job、状态、外部响应或日志。S07 返回的 S00 `ResolvedAsset(LOGPARSE_TOOL)` 与 `LogparseBrokerFactory` 必须作为同一构造结果一起交给 S04 Catalog/Runtime，不能分别从不同配置实例创建。

### 6.5 Client Access Skill

`.claude/skills/problem-locator-client/**` 与本册接口合同由 S06 共同拥有。Skill 必须：

- 通过本地 `problem-locator-client-proxy` stdio MCP 暴露七个工具，再由代理连接局域网 Streamable HTTP `/mcp`；不得让 Claude Code 直接连接上游，以免客户端 schema 拒绝发生在可观测边界之外；
- 代理对 Claude Code 广告只保留字段名和说明的宽松 object schema，上游原始 schema 仍由服务端权威执行；每次调用转发前后必须把完整参数、完整响应/异常、`operation_id`、`attempt_id`、同操作递增的 `attempt_number` 和耗时追加写入客户端 JSONL；日志实现不得读取或依赖 Claude Code debug 日志；
- 客户端日志路径由 `PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE` 或代理 `--log-file` 配置，默认是客户端项目目录下 `.problem-locator/client-dfx.jsonl`；日志级别由 `PROBLEM_LOCATOR_CLIENT_DFX_LOG_LEVEL` 或 `--log-level` 配置；

- 使用调用方已有的 Remote MCP 客户端调用本册七个固定工具；
- 使用系统 `curl` 按 UploadDescriptor 上传用户明确选择的本地文件，并按 ArtifactView.download_url 下载用户可见 Artifact；
- 每次 MCP 写工具和 POST JSON 写请求都生成并复用各自稳定 `request_id`；Attachment PUT 不另生成 request_id，而是逐字使用 UploadDescriptor 的 `Idempotency-Key=attachment_id`。从 PUT 成功响应读取最新 `case_revision` 后，再为 SubmitSupplement 生成独立稳定 request_id；
- 明确展示 `case_id`、当前 revision、开放 requirements、active Job 和下一步动作；
- 上传完成后显式调用 SubmitSupplement 引用 READY Attachment，不能把 READY 当作已采用；
- 下载前若目标路径已存在必须停下询问或选择新文件名，禁止自动覆盖；
- 不把日志字节放入 MCP、不在本地维护服务端权威状态；
- 不请求或展示内部 `LOGPARSE_RUN`、storage key、服务端绝对路径和环境变量。

Skill 只描述跨平台语义，不拼装服务端返回之外的 URL。上传命令必须使用参数数组或安全引用，不能把文件路径、URL 或 header 值作为未经引用的 Shell 片段。测试覆盖含空格、Unicode 和 Shell 元字符的本地路径。

## 7. 行为与错误码

所有错误码、HTTP 状态、MCP 错误 Envelope、是否可重试和业务状态影响只引用 S00。S06 只做无损协议映射，不重新分类。

Adapter 产生的 `VALIDATION_ERROR` 必须在现有 `ApplicationError.details[]` 中返回每个失败字段：`field` 使用点号/数组下标路径，`expected` 使用 `<error type>: <message>`，`actual` 保留标量或规范 JSON 字符串；根级错误使用 `$`。同一诊断的完整参数、原始 Pydantic 错误和 traceback 同时写入 DFX 日志。

必须映射的 S00 行为包括：

- 非法 request、幂等冲突、revision 冲突和 NEW_CASE_REQUIRED；
- Case/Attachment/Artifact 不存在或归属不匹配；
- Attachment 非 UPLOADING、非 READY、哈希/大小不符和容量超限；
- Case 状态不允许 SubmitSupplement、Resume 或 Cancel；
- 服务未就绪、状态损坏、实例锁冲突和固定资产不可用；
- wait 超时的成功异步响应；
- Artifact 不可下载或内部 Artifact 被请求。

`/live` 与 `/ready` 的健康响应不是新业务错误码。Adapter 不得把所有错误统一成 500，也不得向客户端泄露 traceback、绝对路径或环境值。

## 8. 关键边界与不变量

- Remote MCP 承载控制面和小型结构化数据；HTTP 承载文件字节。
- MCP、HTTP、CLI 均调用同一个 Application Service 语义。
- 上传 READY 与采用 Attachment 是两个显式步骤。
- 所有写操作幂等；查询只读且不增加 revision。
- wait 超时不改变 Job。
- 单 Uvicorn worker、单服务进程、单 DATA_ROOT 实例锁是 V1 前提。
- `PUBLIC_BASE_URL` 只用于生成客户端可访问 URL，不参与本地存储路径解析。
- 返回对象永远不含绝对路径、storage key、凭据、环境变量或日志包字节。
- Client Access Skill 只能操作用户明确提供或服务返回的 ID/本地路径。

## 9. Fake 与 Fixture

本切片必须提供：

1. `FakeApplicationService`：记录七种 MCP/HTTP 应用调用，支持成功、S00 各类错误、有限等待和幂等回放。
2. `FakeStateValidator` / `FakeStateExporter`：验证 CLI 退出码、stdout/stderr 分离和只读语义。
3. `StreamingUploadFixture`：生成超过内存友好阈值的分块流，证明 PUT 未一次性读取全部内容；覆盖 prepare 已声明/未声明 size/hash，两种 descriptor 都恰含四个 header，Client 填充 null 后 PUT 逐字使用 attachment_id 幂等键。
4. `FakeArtifactReader`：产生分块流、内部 Artifact、缺失 Artifact 和中途读取失败。
5. `ConfigFixture`：覆盖 env-file、进程环境覆盖、缺失必需项、无效端口和固定默认值。
6. MCP in-process Client Fixture：通过官方 SDK 调用七个工具，不直接调用 Python handler。
7. ASGI Fixture：使用 HTTP 客户端测试全部路由、四个精确上传 header、prepare request_id 不得用作 PUT key、流和错误映射。
8. `FakeCurl` 与 Fake MCP Client：记录 Client Access Skill 的工具顺序、method、URL、本地输入/输出路径、descriptor header 填充、revision 更新和敏感信息脱敏行为。

Fixture 只放本切片责任区，且必须实现 S00 Port。

## 10. 独立验证命令

从仓库根目录执行：

```powershell
python -m pytest -q tests/unit/interfaces/test_mcp_server.py
python -m pytest -q tests/unit/interfaces/test_http_app.py
python -m pytest -q tests/unit/interfaces/test_http_streaming.py
python -m pytest -q tests/unit/interfaces/test_error_mapping.py
python -m pytest -q tests/unit/interfaces/test_settings.py
python -m pytest -q tests/unit/interfaces/test_cli.py
python -m pytest -q tests/unit/interfaces/test_client_access_skill.py
python -m pytest -q tests/unit/interfaces
python -m problem_locator.entrypoints.cli --help
python -m problem_locator.entrypoints.cli serve --help
python -m problem_locator.entrypoints.cli validate-state --help
python -m problem_locator.entrypoints.cli export-state --help
```

测试必须使用 in-process MCP/ASGI Fixture，不启动真实 Claude，不依赖真实 logparse，不访问公网。

## 11. 完成标准

- 七个 MCP 工具名称、字段传递、幂等和 wait 语义通过官方 SDK in-process 测试。
- 五个 HTTP 路由完成流式、错误、安全和健康语义测试。
- 2.5 GiB/5 GiB 使用精确字节值，不通过实际创建超大文件测试；使用可控计数流验证边界。
- 三个 CLI 入口的帮助、参数校验、退出码和只读行为通过测试。
- 配置优先级与全部固定默认值通过测试；敏感 env 不出现在日志或响应。
- Client Access Skill 通过 Fake MCP/curl 完成 create、prepare、upload、submit、get、resume/cancel 和 Artifact 下载合同测试。
- MCP 与 HTTP handler 中没有 Case 状态机复制代码。
- `python -m pytest -q tests/unit/interfaces` 全绿。
- `git diff --name-only` 中本切片实现变更只位于第 4节责任区。

## 12. 向 S08 的交接格式

```json
{
  "spec_id": "S06",
  "title": "Remote MCP, HTTP, CLI and Client Access Skill",
  "executor": {"model": "gpt-5.6-sol", "reasoning_effort": "ultra"},
  "contract_revision": "v1-contract-r1",
  "contract_base_commit": "<contract-base-commit>",
  "branch": "codex/v1-s06-mcp-http-cli",
  "head_commit": "<head-commit>",
  "scope_completed": [],
  "changed_files": [],
  "fixtures_consumed": [],
  "fixtures_produced": [],
  "tests": [
    {"command": "python -m pytest -q tests/unit/interfaces", "status": "passed"}
  ],
  "dependency_requests": [],
  "contract_change_requests": [],
  "known_limitations": [],
  "risks": [],
  "integration_notes": [],
  "forbidden_scope_touched": false
}
```

以上顶层字段全部必填，不得省略；没有内容的列表写空数组。交接文件固定写入 `handoff/S06.json`。交接的 `integration_notes` 必须列出真实 MCP SDK 版本和 FastAPI/Uvicorn 版本，版本应来自 S00 锁文件，不得在本切片单独升级。

## 13. S08 组合要求

S08 通过 `composition_hooks` 注入真实 Application Service、Repository、ResourceStore、Scheduler 和 Runtime。至少验证：

- `/mcp` 与 `/api/v1` 共用一个服务地址和一个应用服务实例；
- MCP prepare 与 HTTP POST prepare 产生相同业务语义；
- PUT 发布 READY 后不会自动创建 Job，SubmitSupplement 才推进；
- `wait_seconds=30` 以内等待同一个 Job，超时后 Job 继续；
- 服务重启后 get/resume 能看到持久化状态；
- `/ready` 在损坏 state 时失败而 `/live` 仍成功；
- S06 Client Access Skill 可完成一次上传与 Artifact 下载且无内部路径泄漏。

组合缺陷退回路径所有者；S08 不直接修改 S06 责任区。

## 14. 合同变更请求格式

```json
{
  "request_id": "CCR-S06-001",
  "requesting_spec": "S06",
  "current_contract_revision": "v1-contract-r1",
  "problem": "现有合同无法实现或验证的精确问题",
  "proposed_change": "请求后的完整协议语义",
  "affected_types_or_codes": [],
  "affected_specs": ["S00", "S06"],
  "compatibility": "对 MCP、HTTP、CLI、持久化或客户端的影响",
  "fixture_and_test_changes": []
}
```

S00 未接受前，不得加入私有字段、别名工具、备用路由或第二套错误格式。
