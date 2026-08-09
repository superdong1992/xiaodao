# Problem Locator V2

## Diagnosis Skill v4

当前冻结版本如下；这些版本共同定义本次 V2 行为，不应只按其中某一个版本判断兼容性：

| 合同或资产 | 当前版本 |
| --- | --- |
| Problem Locator package | `2.0.0` |
| State / Job / Outcome schema | `3` |
| S00 contract revision | `v3-contract-r1` |
| GenerationSpec | `v4` |
| Diagnosis Skill generator / 生成 Skill | `4.0.0` |
| Diagnosis Skill manifest | `4`（`verification_contract.schema_version=1`） |
| ROUTE / DIAGNOSE / REVIEW output contract | `2.0.0` / `4.0.0` / `2.0.0` |
| Specialist / Reviewer profile | `1.0.1` / `1.0.1` |
| Router / Diagnose / Review tool bundle | `2.0.0` / `3.0.0` / `2.0.0` |

State、Job 和权威 Outcome 已硬切到 V3。Problem Locator 2.0.0 只接受路径尚不存在或目录完全为空的全新 `DATA_ROOT`，首次启动会写入 canonical `data-format.json`；已有非空但无 marker、使用旧 marker 或 marker 被篡改的目录都会启动失败，服务不会迁移、改写或删除其中任何内容。升级前必须先备份旧目录，再使用新的 `DATA_ROOT`；需要保留的 V1/V2 State、Job 或 Outcome 只能作为只读历史材料另行处理。

本仓库将故障定位能力分为三层：

- 全局 DIAGNOSE output contract 只定义通用 Schema、Canonical JSON、Evidence、Candidate、服务端确定性输出和安全约束，不包含 RPC、数据库等业务字段。
- `logparse-diagnose` 只负责 Logparse broker、一次解析、`LOGPARSE_RUN` 持久化与复用，以及受控路径规则。
- 每个生成的 Diagnosis Skill 自己声明业务 requirements、阶段、补参提示、约束和 Logparse 字段映射。

Diagnosis Skill 由 `wiki-to-diagnosis-skill` 根据 GenerationSpec v4 生成。每个 requirement 都必须声明 `name`、`kind`、`stage`、`fulfillment_source`、`supplement_policy`、`prompt` 和 S00 原生 `constraints`。manifest v4 还必须声明 `deployment_scope=PRODUCTION|TEST_ONLY` 与 `verification_contract`：整行事件提取器、显式时间窗及边界、事实字段、角色覆盖、跨角色关联、事件顺序和语义因果规则。`requires_logparse` 只表示绑定 Logparse 工具，不会自动生成 RPC 参数；`custom_parameters` 为空表示不增加任何自定义参数。

Logparse 产品可以省略。省略时 Runtime 记录有效产品 `default`，Broker 不向上游强制传入 `--product`；只有非默认产品才显式传参。生成定位 Skill 时，作者只声明 Logparse 归档 requirement 的数量约束，不填写 Content-Type；上传时用户也只选择归档文件。平台按文件后缀确定内部 Content-Type：`.gz/.tar.gz/.tgz` 为 `application/gzip`，`.zip` 为 `application/zip`，`.tar` 为 `application/x-tar`。

Agent 不再直接产生权威 Outcome 或公开用户产物。DIAGNOSE Agent 禁止提出或写入 `USER_RESULT`、`USER_RESULT_ARCHIVE`、`diagnosis-result.json`、`result.zip` 或归档请求；DIAGNOSE 或 REVIEW Agent 只写入 `output/job_outcome.draft.json` 并调用 `problem-locator-seal-outcome-draft` 封存草稿。Agent 退出后，服务端按固定 Skill 重新打开原始证据、独立执行 `verification_contract`，再生成带 `outcome_id`、时间和 `decision_audit` 的唯一权威 `output/job_outcome.json`。

DIAGNOSE 草稿通过服务端验证后，服务端立即生成并持久化以下候选结果；在 Case 处于 `REVIEWING` 时它们不可公开下载，仅在独立 Review PASS 后成为公开产物：

- `diagnosis-result.json`：规范化 `USER_RESULT`。
- `result.zip`：仅为 `COMPLETED` 结果生成的 `USER_RESULT_ARCHIVE` v2，固定按 `result.txt`、`archive-manifest.json`、Logparse plan 全部可交付 anchor 的目标日志排列；日志采用包含 label/module/slot/process/PID（并在 broker 提供时包含 CPU）的语义文件名，不按引用证据临时编号。无日志场景仍固定包含前两个条目。

这两个候选结果采用 V1 durable outbox 的顺序发布与幂等采用语义，不承诺底层正式资源在任意故障时刻都“物理零部分”：第二项发布失败时，第一项可以已存在于内部正式资源区，但 State repository、CaseView、产物列表和下载入口都不得公开任一结果。同一权威 Outcome 重试时按既定目标和 SHA-256 采用已落盘的第一项，再完成第二项；成功提交后两项在 `REVIEWING` 阶段仍保持内部，只有 Review PASS 的状态提交才使 JSON 与 ZIP 同时对外可见。

Agent 无权预先构造、摘要或替代这两项结果。Reviewer 使用盲审上下文：只接收固定 Candidate、固定用户事实、Skill 规则和 Candidate 实际绑定的原始 Evidence，不接收 Specialist 的结论、解释或先前判词作为证明。

时间、必选参数、角色、跨角色关联或事件顺序任一不符合 Skill，或因果链不能由原始证据支持时，DIAGNOSE Candidate 或 REVIEW PASS 会被服务端降级为 `INCONCLUSIVE`；Reviewer 基于证据给出的合法 `REJECT` 则保留为负向判决。两者都会使 Case 终止为 `UNRESOLVED`，被拒绝的 Candidate 仅以 `REJECTED` 保留在内部；服务端会公开一份 `status=INCONCLUSIVE` 的 `USER_RESULT` JSON，明确列出验证结果、证据缺口、限制和建议，但禁止生成 `USER_RESULT_ARCHIVE`/`result.zip`。服务同时生成可下载的 `AUDIT_BUNDLE`，供局域网内复盘和重放。`LOGPARSE_RUN` 仍是内部持久化输入，不会作为公开产物返回。当前生成器包含 RPC 超时、数据库死锁和无日志人工排查三个异构测试 Fixture，用于验证参数隔离与有/无 Logparse 的流程差异；当前行为以 v4 generator、manifest、生成资产与 runtime contracts 为准。

### 发布验收

仓库测试统一从 [`tools/test-flow/README.md`](tools/test-flow/README.md) 进入；终态结构见 [`design/test-flow-architecture.md`](design/test-flow-architecture.md)。Dev 默认只跑受影响确定性测试和完整确定性套件，不调用真实模型；SameJob 已纳入确定性 Journey。Release 要求 clean commit、当前平台的 built-in Client→Linux adapter、完整确定性/平台证明，以及从 GENESIS 和全新空 `DATA_ROOT` 开始的一条 no-mock CrossJob 旅程。

每次运行的本地证据保存在 `.tmp/test-flow-evidence/<run-id>`。`verdict.json` 是唯一权威结论；缺失就是 `UNFINALIZED`。证据在复用前会按当前配置、密钥扫描器和事件合同重新审计，且不会自动删除。

Problem Locator 是一个单实例故障诊断服务。它接收结构化问题，收集事实与附件，执行固定版本的路由、诊断和盲审任务，最终发布经过机器验证和独立复核的完成态 `USER_RESULT`，或发布说明无法可靠定论的 `INCONCLUSIVE` `USER_RESULT` JSON 与 `UNRESOLVED` 审计包。

V2 使用本地 JSON 状态文件和文件系统资源实现持久化；所有业务写操作都通过应用服务及其仓储端口完成。

## 环境要求与安装

- CPython 3.12（项目要求 `>=3.12,<3.13`）
- `uv`，并使用仓库中已提交的 `uv.lock`
- 受控的 Logparse 源码目录、配置文件及 Python 启动器；源码目录可以来自 Git checkout，也可以来自源码压缩包解压
- 用于执行真实 Agent 任务、兼容 Claude 的命令行程序

安装锁定版本的运行时依赖和开发依赖：

```sh
uv sync --frozen --all-groups
uv lock --check
```

在部署或日常安装时，请勿顺带升级已锁定的 MCP、HTTP 或存储相关依赖。

## 配置

复制 [`.env.example`](.env.example) 到一个不提交至版本库的私有配置文件，并将所有占位值替换为绝对路径。通过 `--env-file` 显式指定的文件会按 UTF-8 dotenv 格式解析；如果进程环境中已经存在同名变量，则进程环境变量优先。

| 环境变量 | 必填 | 默认值 | 说明 |
|---|:---:|---|---|
| `DATA_ROOT` | 是 | 无 | 独占的持久化状态、资源和任务根目录 |
| `PUBLIC_BASE_URL` | 是 | 无 | 对外提供服务的 HTTP(S) 根地址，不得包含查询参数或片段 |
| `SKILL_DIR` | 是 | 无 | 外部受控的生产 Diagnosis Skill 目录；必须至少包含一个 `PRODUCTION` Skill，且生产 catalog 拒绝任何 `TEST_ONLY` Skill。不得指向仓库 `.claude/skills` |
| `LOGPARSE_REPO` | 是 | 无 | 受控的 Logparse 源码目录；Git checkout 和源码压缩包解压目录均受支持，启动时按实际内容生成指纹 |
| `LOGPARSE_CONFIG_PATH` | 是 | 无 | Logparse 工作区内的配置文件 |
| `BIND_HOST` | 否 | `127.0.0.1` | Uvicorn 监听地址 |
| `PORT` | 否 | `8000` | Uvicorn 监听端口 |
| `CLAUDE_COMMAND` | 否 | `claude` | Agent 命令，会被解析为 argv 参数模板 |
| `LOGPARSE_PYTHON` | 否 | 当前 Python | Logparse 使用的 Python 启动命令 |
| `DFX_LOG_LEVEL` | 否 | `INFO` | 结构化诊断日志级别：`DEBUG`、`INFO`、`WARNING`、`ERROR` 或 `CRITICAL` |
| `DFX_LOG_DIR` | 否 | 无 | 服务端可观测日志目录的绝对路径；配置后生成 `debug.jsonl`、`journey.jsonl` 和按 Case 渲染的人类可读日志 |

运行时限制是冻结的契约常量，不属于可配置项。V2 会拒绝 `JOB_CONCURRENCY` 以及未知的 limit、max、retention 覆盖项，避免运维人员误以为某项实际上无效的限制已经生效。

不要配置或持久化 `PROBLEM_LOCATOR_LOGPARSE_ENDPOINT` 和 `PROBLEM_LOCATOR_LOGPARSE_TOKEN`。这两个值会按任务临时创建，并在代理会话结束时删除。

## 启动服务

校验配置并启动唯一的工作线程：

```sh
uv run python -m problem_locator serve --env-file /absolute/path/to/service.env
```

对于同一个 `DATA_ROOT`，V2 只允许一个服务进程和一个 Uvicorn worker。第二个进程会因实例锁就绪检查失败而退出。请勿让多 worker 进程管理器共用同一个数据根目录。

服务接口：

- MCP 传输端点：`/mcp`
- 存活检查：`GET /live`
- 就绪检查：`GET /ready`
- 准备附件：`POST /api/v1/cases/{case_id}/attachments`
- 上传已准备的附件内容：`PUT /api/v1/attachments/{attachment_id}/content`
- 下载公开产物：`GET /api/v1/artifacts/{artifact_id}/content`

服务提供以下 7 个远程 MCP 工具：

- `problem_locator_create_case`
- `problem_locator_prepare_attachment`
- `problem_locator_submit_supplement`
- `problem_locator_get_case`
- `problem_locator_resume_case`
- `problem_locator_cancel_case`
- `problem_locator_list_artifacts`

七个工具的顶层参数形状如下；`req` 表示必填，`opt` 表示可省略：

| 工具 | 参数形状 |
| --- | --- |
| `problem_locator_create_case` | `request_id/statement/expected_behavior/actual_behavior/scope: string` req；`goals/non_goals/constraints/completion_criteria: array<string>` req；`initial_user_fact_names/initial_user_fact_values: array<string>` opt；`wait_seconds: integer` opt |
| `problem_locator_prepare_attachment` | `request_id/case_id/name/content_type` req；`expected_case_revision: integer` req；`declared_size: integer\|null` opt；`declared_sha256: string\|null` opt |
| `problem_locator_submit_supplement` | `request_id/case_id` req；`expected_case_revision: integer` req；`input_names/input_values: array<string>` req；`attachment_ids: array<string>` req；`wait_seconds` opt |
| `problem_locator_get_case` | `case_id` req；`wait_for_job_id: string\|null` opt；`wait_seconds` opt |
| `problem_locator_resume_case` | `request_id/case_id/expected_case_revision` req；`wait_seconds` opt |
| `problem_locator_cancel_case` | `request_id/case_id/expected_case_revision` req |
| `problem_locator_list_artifacts` | `case_id` req |

七个公开 MCP input schema 全部扁平化，根属性只能是标量、nullable 标量或标量数组。`create_case` 的八个问题字段直接位于根层；两组 name/value 数组必须等长并按索引配对。完整规范示例见客户端 Skill。

仓库内置的 [`.claude/skills/problem-locator-client`](.claude/skills/problem-locator-client) Skill 说明了安全的请求 ID、修订版本处理方式、上传请求头以及产物哈希校验方法。文件内容只通过 HTTP 传输，绝不会嵌入 MCP 消息。

### 客户端远端 MCP 配置

Windows 和 macOS 默认使用本机 Claude Code；Linux Client 只在显式选择时使用。客户端不安装 `problem-locator` Python 包，也不启动本地 MCP Server 或转发代理。Claude Code 自身作为 MCP Host/Client，直接连接唯一受支持的 Linux 服务端 Streamable HTTP `/mcp`。客户端项目根目录的 `.mcp.json` 使用固定 server key `problem-locator`：

```json
{
  "mcpServers": {
    "problem-locator": {
      "type": "http",
      "url": "${PROBLEM_LOCATOR_MCP_URL}"
    }
  }
}
```

完整模板见 [client-mcp-config.json](.claude/skills/problem-locator-client/references/client-mcp-config.json)；`${VAR}` URL 展开规则见 [Claude Code MCP 配置说明](https://code.claude.com/docs/en/mcp#environment-variable-expansion-in-mcpjson)。启动 Claude Code 前设置真实地址，且地址必须以 `/mcp` 结尾。若机器存在 `HTTP_PROXY` 或 `HTTPS_PROXY`，把 Linux 服务端的主机名或 IP 追加到已有 `NO_PROXY`，不要用 `NO_PROXY=*`，也不要覆盖企业代理所需的其他排除项；代理变量行为见 [Claude Code 企业代理说明](https://code.claude.com/docs/en/corporate-proxy)：

```powershell
$env:PROBLEM_LOCATOR_MCP_URL = "http://192.168.1.20:8000/mcp"
$env:NO_PROXY = "localhost,127.0.0.1,192.168.1.20"
```

客户端不安装 `problem-locator`，不安装 Problem Locator Hook，不运行本地 MCP 或转发代理，也不生成 Problem Locator 专用 DFX。启动后用 `/mcp` 确认 `problem-locator` 的传输类型和连接状态。服务端严格拒绝字段 `problem_spec`、`initial_user_facts`、`inputs`，也不会自动解析 JSON 字符串。

Windows/macOS 默认跟随当前 Host；Linux Client 必须显式启用。当前平台的真实发布证明由 Test Flow 的 built-in adapter 执行：直接 HTTP negative probe 验证错误复合字段和错误数组类型被拒绝，真实 Host 调用则用 Claude stream-json 与 Linux 服务端 DFX 验证扁平参数、七工具 correspondence，并反向确认客户端没有专用 DFX。版本不在文档中写死，而是以 executable hash、`--version`、runtime profile 和 settings allowlist 纳入身份与 verdict。

线上 schema、实际参数和验证错误以 Linux 服务端的 `mcp.tools.listed`、`mcp.tool.started`、`mcp.tool.completed` 和验证事件为准。Test Flow 是这些发布证明的唯一入口；skip、零执行用例或缺少服务端 DFX 都不能作为通过。局域网改版客户端仍需在实际部署后用 `/mcp` 以及真实 `create_case`/`submit_supplement` 关闭环境故障，不能从其他 Client 平台的 verdict 外推。

所有新增或修改的 MCP 输入必须继续保持扁平：根 object 属性只能是标量、nullable 标量或标量数组，不得新增 `$ref/$defs`、嵌套 object、动态 Map 或对象数组；合同测试不设白名单。

`/live` 表示 HTTP 进程正在提供服务。`/ready` 还会检查配置、实例锁、状态有效性、数据目录和启动恢复过程。在恢复期间，或出现致命状态/worker 故障后，服务可能仍然存活，但尚未就绪。

### DFX 诊断日志

配置 `DFX_LOG_DIR` 后，服务把原有单行 JSON 诊断事件追加写入 `<dir>/debug.jsonl`，同时把可重放的端到端语义事件追加写入 `<dir>/journey.jsonl`。Journey 事件通过 `correlation_id`、`request_id`、`case_id`、`job_id` 和 `outcome_id` 关联一次问题定位的各个阶段。每个 HTTP 请求仍会返回 `X-Problem-Locator-Correlation-ID`，同一值会出现在对应日志中。MCP/HTTP 参数校验失败会记录完整参数、字段路径、实际输入和异常堆栈；MCP 错误响应也会在 `ApplicationError.details[]` 中返回可操作的字段错误。

服务端日志不需要安装额外组件；它随 `problem-locator` 包安装。服务端在发布源码目录执行 `uv sync --frozen` 后，把下面两项写入启动时通过 `--env-file` 指定的配置文件：

写入指定目录的配置示例：

```dotenv
DFX_LOG_LEVEL=DEBUG
DFX_LOG_DIR=/var/log/problem-locator
```

然后按“启动服务”一节运行服务，并确认日志文件已经产生：

```sh
uv run python -m problem_locator serve --env-file /absolute/path/to/service.env
tail -f /var/log/problem-locator/debug.jsonl
```

需要查看某个 Case 的完整链路时，运行确定性渲染命令。它会严格校验完整 `journey.jsonl`，并覆盖生成 `<dir>/cases/<case_id>/detailed.log` 和 `brief.log`：

```sh
uv run python -m problem_locator render-journey \
  --case-id 00000000-0000-0000-0000-000000000000 \
  --log-dir /var/log/problem-locator
```

`detailed.log` 保留全部语义事件及 `journey.jsonl:<line>` 来源，适合逐步定位；`brief.log` 只保留 Case 状态、关键里程碑、当前结论、阻塞项和失败点。运行中的 Case 会明确标记为“当前快照”，不会伪装成最终结论。仓库内置的 [`.claude/skills/render-problem-locator-trace`](.claude/skills/render-problem-locator-trace) Skill 只调用该命令，不自行解析 Journey，也不回退到 debug 日志。

如果不配置 `DFX_LOG_DIR`，Journey 日志关闭，原有 debug 日志仍写入 stderr，可由 Docker、systemd 或启动脚本收集和轮转。直接启动时也可以这样重定向：

```sh
uv run python -m problem_locator serve --env-file /absolute/path/to/service.env \
  2>> /absolute/path/to/problem-locator.log
```

## V2 机器校验、盲审与审计包

DIAGNOSE 和 REVIEW 都执行同一条服务端可信边界：

1. Agent 按固定 Workspace 输入、Skill 和 output contract 写 `AgentJobOutcomeDraftV2`。
2. `problem-locator-seal-outcome-draft` 只封存并哈希草稿，不给出业务结论。
3. Agent 进程退出后，服务端按 manifest v4 的 `verification_contract` 重新扫描 Evidence locator 指向的原始日志范围，独立校验事件基数、时间窗口及开闭边界、用户事实字段、角色、关联和顺序，并为每条规则记录 `VERIFIED_PASS`、`VERIFIED_FAIL`、`UNVERIFIABLE` 或 `SEMANTIC_ONLY`；Agent 自报的 PASS/FAIL/UNKNOWN 只是并列保留的 claim。
4. 服务端生成 `decision_audit.json`、所用原始证据行记录和唯一权威 Outcome。Agent 自述、Evidence summary、文件名或“看起来合理”的因果故事都不能代替规则通过。

Reviewer 是盲审任务。它不会获得 Specialist 的隐藏会话或判词，只读取 `REVIEW_SUBJECT` 固定的 Candidate、用户事实、规则及 Candidate supporting/completion Evidence 的去重并集，并独立执行同一组规则。只有全部必需规则通过且 Review 问题数组为空时才允许 PASS。

Case 中已有的 `problem_time` 和其他 USER_FACT 是冻结输入，不能在同一 Case 中请求替换。只有 Skill 明确声明为 OPEN 且 `supplement_policy=MISSING_ONLY` 的缺失 requirement 才能补充。若时间、参数、角色、关联、顺序或因果证据不匹配，服务端会拒绝 DIAGNOSE Candidate/REVIEW PASS；不一致的正向结果归一化为 `INCONCLUSIVE`，合法的 Reviewer `REJECT` 保留为负向判决，随后 Case 终止为 `UNRESOLVED`。需要用修正后的事实重新创建 Case。

每个 `UNRESOLVED` Case 都发布一个可下载 `AUDIT_BUNDLE`。包内只收集允许公开的可观察记录，包括 Case/Job、实际 Agent context、Agent draft、服务端 decision audit、服务端从完整扫描范围中匹配并用于判定的原始证据行、finalization manifest、盲审 subject，以及存在时的 broker audit。Agent stdout/stderr 的原始内容只保留在本地 execution record 和隔离 replay 目录；下载包只记录它们是否存在、字节数和 SHA-256，避免被拒绝的 `USER_RESULT` 经进程输出旁路进入审计包。原始上传包和完整 Logparse 树同样不会进入审计包。

这些记录用于检查“Agent 看到了什么、声明了什么、服务端按哪条规则接受或拒绝，以及实际进程输出了什么”。它们不是、也不会包含模型的隐藏思维链；增加日志同样不能使隐藏推理过程可见。

## 隔离重放指定 Job

`replay-job` 是普通本地 CLI，不引入管理员角色、管理 API、认证或权限模型。它只接受当前 V3 State/Job/Outcome 闭包，并在新的隔离安装中按当前固定资产执行指定阶段：

- `diagnose-only`：源 Job 必须是 DIAGNOSE；执行服务端终结，但不向隔离 State 提交诊断 Outcome。
- `review-only`：源 Job 必须是 REVIEW；执行服务端终结，但不向隔离 State 提交 Review Outcome。
- `through-review`：源 Job 必须是 DIAGNOSE；提交诊断 Outcome，并在确实产生唯一 Review Job 时继续执行和提交 Review。诊断直接进入 `UNRESOLVED`、等待补参或改路由而没有 Review Job，也是一个有记录的正常停止结果。

示例：

```sh
uv run python -m problem_locator replay-job \
  --source-data-root /absolute/path/to/stopped-source-data \
  --job-id 00000000-0000-0000-0000-000000000000 \
  --mode through-review \
  --output-dir /absolute/path/to/new-replay-output \
  --env-file /absolute/path/to/service.env \
  --skill-dir /absolute/path/to/current-skills
```

重放前必须停止使用源 `DATA_ROOT` 的服务；CLI 会获取同一把独占实例锁，锁被占用时拒绝运行。`--output-dir` 必须是绝对路径、尚不存在，并且不能与源数据、Skill、Logparse 或 DFX 路径重叠。CLI 在其中创建隔离的 `data/`、`replay-manifest.json`、`replay-result.json`、DFX/Journey 和执行记录，不修改源安装。manifest 同时记录源/重放固定资产引用、差异和输入输出哈希，便于比较修复前后的同一阶段。

## 附件与结果处理

准备附件时，服务会创建元数据和上传描述信息。上传文件内容时，服务会校验其准确大小与 SHA-256，校验通过后将附件转为 `READY` 状态。仅上传附件不会推进 Case；调用方必须显式将 `READY` 附件作为补充材料提交。

`WorkspaceAttachmentInput.filename_suffix` 为必填字段，但允许值为 `null`。归档文件后缀及 content-type 的校验使用冻结的公共契约辅助函数；路径形式、包含大写字母的别名以及不匹配的后缀都会被拒绝。

默认只列出可下载的公开产物。`COMPLETED` 的 `USER_RESULT` 及 `result.zip` 只在 Review PASS 后公开；服务端验证后终止的 `INCONCLUSIVE` `USER_RESULT` JSON 也会公开，但不存在 `result.zip`。下载内容必须与声明的字节数和 SHA-256 一致。内部 `LOGPARSE_RUN` 目录会作为后续任务的持久化输入，但永远不可下载。

## 启动恢复与重试语义

启动恢复只适用于同一 `schema_version=3`、`contract_revision=v3-contract-r1` 的数据。读取 `state.json` 时会先严格校验 V3 envelope 和全部引用；任何 V1/V2 State、Job、Outcome 或混合版本闭包都会以 `STATE_SCHEMA_UNSUPPORTED`/状态损坏拒绝，调度器不会尝试兼容、迁移或运行其中的旧 Job。

对于已经由当前 State V3 服务创建的数据，每次启动时调度器都会创建新的运行时 epoch，并在接受新任务之前完成以下恢复流程：

1. 逐字节重放所有已持久化、已最终确定但尚未确认的 Job Outcome。
2. 完成重放后，才会把没有最终 Outcome 的同合同 `RUNNING` 任务标记为 `INTERRUPTED`。
3. 重新调度已经持久化的 `PENDING` 任务。

重试提交 Outcome 时会复用同一份最终回执，不会再次运行 Agent。资源或配置错误，以及带类型的状态读取错误，会使 worker 停止接单并导致就绪检查失败。恢复后的任务会保留所有冻结的运行时绑定，当前 Catalog 不能用新版本替换这些绑定。被中断的 `REVIEW` 任务会继续执行 `REVIEW`，不会退回 `DIAGNOSE`。

如果一条命令的业务变更已经提交，但提交后的 Case 再读取失败，服务会返回持久化回执，并令 `case_view=null`。应将该响应视为持久化成功，随后重新查询 Case；不要创建第二个逻辑请求。

## 校验、导出、备份与恢复

以下管理命令会获取与服务相同的独占实例锁，因此只能在对应 `DATA_ROOT` 的服务停止后执行：

```sh
uv run python -m problem_locator validate-state \
  --data-root /absolute/path/to/problem-locator-data

uv run python -m problem_locator export-state \
  --data-root /absolute/path/to/problem-locator-data \
  --output /absolute/path/outside-data-root/state-export.json
```

`validate-state` 输出规范化的 `ValidationReport`。`export-state` 输出规范化的 `StateExport`，其中包含单个状态世代、完整对象数量，以及按顺序排列的资源大小/哈希清单。导出文件必须位于 `DATA_ROOT` 之外；它只用于审计和同合同备份核对，不能替代资源备份，也不能把 V1/V2 数据转换为 State V3。

创建可恢复备份：

1. 停止服务，并等待关闭流程完成。
2. 执行 `validate-state` 和 `export-state`。
3. 完整复制 `DATA_ROOT` 目录树，并尽量以原子方式保证 `state.json`、`jobs/**` 和 `resources/**` 来自同一个停机时间点。
4. 将导出文件与备份放在一起，以便核对对象数量和哈希。

恢复时，应将损坏的数据根目录保持为只读，把完整且已知可用的 State V3 备份复制到一个新的绝对路径，执行 `validate-state`，并核对导出文件中的对象数量和哈希，最后使用新的数据根目录启动服务。

不要手工编辑 `state.json`，不要丢弃已经最终确定的 outbox 文件，也不要静默回退到 `state.json.prev`。

State V3 与所有 V1/V2 State、Job 和 Outcome 有意不兼容。服务不提供原地迁移、旧 Job 恢复、隐藏旧字段或按需转换路径；部署当前版本时使用新的数据根目录。

### 冻结发布边界声明

以下英文短句是发布测试使用的稳定语义标识；中文解释是规范正文：

- State V3 is a hard cut：服务不迁移或恢复 V1/V2 State、Job、Outcome。
- Replay every durable, finalized but unconfirmed Job Outcome：启动时先重放所有已最终确定但未确认的 Outcome。
- 当 `state.json` approaches 16 MiB 时，应启动离线迁移设计。
- 当 retained history approaches 500 Cases 时，应启动离线迁移设计。
- 需要 second service instance or high availability 时，必须迁移出单实例 JSON 架构。
- 恢复或迁移期间必须 keep the original JSON root read-only。

## PostgreSQL 迁移边界

V2 不包含 PostgreSQL、ORM、双写机制或分布式锁。当满足以下任一条件时，应开始设计离线 PostgreSQL 迁移方案：

- 需要第二个服务实例或高可用能力；
- `state.json` 接近 16 MiB；
- 保留的历史记录接近 500 个 Case；
- 状态写入延迟已经明显影响运行。

迁移时必须停止写入，导出一个规范化状态世代，通过等价的仓储/资源记录完成导入，核对所有对象数量和资源哈希，并在验收完成前保持原 JSON 数据根目录只读。

领域层、应用层和运行时层依赖冻结的端口，而不是 JSON 适配器，因此迁移仍然是一次离线适配器替换，而不是业务模型分叉。

## 安全说明与已知限制

- V2 面向可信用户、固定版本 Skill 和可信 Agent 命令所在的受控网络，不提供租户级授权；本次重放能力也没有引入管理员、管理端或额外权限模型。
- 服务进程和 Agent 都不是操作系统沙箱。请使用专用操作系统账户运行，并只授予必要的仓库和数据访问权限。
- 普通 MCP/HTTP 元数据和错误响应中不得出现密钥、原始环境变量值、服务器路径、日志归档内容、代理令牌或内部执行日志。只有用户显式下载 `UNRESOLVED` 的 `AUDIT_BUNDLE` 时，才会返回前述 allowlist 中经过固定边界处理的 context、decision evidence 和 stdout/stderr 元数据；原始 stdout/stderr 内容仍只存在于本地 execution record 或隔离 replay 目录，原始上传归档、完整 Logparse 树和隐藏思维链仍不公开。
- Logparse 会在启动时进行指纹校验。首个符合条件的诊断任务可以解析一次日志；后续任务必须使用已持久化的 `LOGPARSE_RUN`，不得再次解包或解析原始归档。
- V2 的并发数固定为 `1`，上下文、工作区和输出限制均为固定值；持久化依赖本地文件系统，不提供多实例故障转移。
- Linux Server 启动验证、Windows/macOS 默认 Client 能力、显式 Linux Client、平台进程树/取消验证、确定性 Journey 和真实 Logparse 冒烟测试属于不同证明。测试或交接记录必须明确实际运行的平台和 Stage。

## 测试与发布

测试计划、Dev 运行、真实模型重试合同、Release 缓存准备、三平台 built-in adapter、证据管理和退出码统一见 [`tools/test-flow/README.md`](tools/test-flow/README.md)。不要直接运行底层 selector 后自行组合发布结论。

Release closure 会分别验证 Linux Server 原生启动与安装分发、本机 Client/Host、进程树与取消、完整 deterministic/SameJob、真实 Logparse、真实 Agent 以及 fresh CrossJob。skip 不等于通过；每个 Gate 的 JUnit 执行/跳过计数、平台、候选 Git SHA、runtime profile、外部源码和 executable identity 都写入 receipt。只有 exact clean commit 上最后生成且可重新验证的 `verdict.json` 能证明该次发布。
