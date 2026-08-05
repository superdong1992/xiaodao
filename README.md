# Problem Locator V1

## Diagnosis Skill v3

当前落地版本为 GenerationSpec v2、生成器/生成 Skill `3.0.5`、manifest schema `2`、DIAGNOSE output contract `2.0.3` 和 S00 contract revision `v1-contract-r4`。

本仓库将故障定位能力分为三层：

- 全局 DIAGNOSE output contract 只定义通用 Schema、Canonical JSON、Evidence、Candidate、原子输出和安全约束，不包含 RPC、数据库等业务字段。
- `logparse-diagnose` 只负责 Logparse broker、一次解析、`LOGPARSE_RUN` 持久化与复用，以及受控路径规则。
- 每个生成的 Diagnosis Skill 自己声明业务 requirements、阶段、补参提示、约束和 Logparse 字段映射。

Diagnosis Skill 由 `wiki-to-diagnosis-skill` 根据 GenerationSpec v2 生成。每个 requirement 都必须声明 `name`、`kind`、`stage`、`fulfillment_source`、`prompt` 和 S00 原生 `constraints`。`requires_logparse` 只表示绑定 Logparse 工具，不会自动生成 RPC 参数；`custom_parameters` 为空表示不增加任何自定义参数。

Logparse 产品可以省略。省略时 Runtime 记录有效产品 `default`，Broker 不向上游强制传入 `--product`；只有非默认产品才显式传参。生成定位 Skill 时，作者只声明 Logparse 归档 requirement 的数量约束，不填写 Content-Type；上传时用户也只选择归档文件。平台按文件后缀确定内部 Content-Type：`.gz/.tar.gz/.tgz` 为 `application/gzip`，`.zip` 为 `application/zip`，`.tar` 为 `application/x-tar`。

候选结论必须同时产出：

- `diagnosis-result.json`：规范化 `USER_RESULT`。
- `result.zip`：可交付的 `USER_RESULT_ARCHIVE`，扁平包含 `result.txt` 和按证据顺序编号的目标日志；无日志场景只包含 `result.txt`。

两项结果都必须经过 Review PASS 才会公开下载。`LOGPARSE_RUN` 仍是内部持久化输入，不会作为公开产物返回。当前生成器包含 RPC 超时、数据库死锁和无日志人工排查三个异构 Fixture，用于验证参数隔离与有/无 Logparse 的流程差异。完整设计与版本矩阵见 [`design/diagnosis-skill-v3-generalization-plan.md`](design/diagnosis-skill-v3-generalization-plan.md)。

### 发布验收

使用仓库内置的分段链路验证同一份生产补丁：先运行 `Fast` 完成 Windows 客户端到 Linux 服务的完整业务旅程，再以该成功证据作为 `BusinessEvidenceRoot` 运行 `ReleaseGates`。后者会并行执行目标回归、全量套件、干净安装包、原生 Linux 启动以及真实 Agent/Route/Diagnosis 合同门，并要求低于 480 秒 SLA。

每次运行的本地证据保存在 `.tmp/pl-e2e-evidence/<attempt>`；运行时证据不属于发布包。发布结论以对应目录中的 `verification-report.json`、最终审计 JSON、密钥扫描结果和 JUnit 文件为准。

Problem Locator 是一个单实例故障诊断服务。它接收结构化问题，收集事实与附件，执行固定版本的路由、诊断和复核任务，最终发布经过复核的 `USER_RESULT` 结果文件。

V1 使用本地 JSON 状态文件和文件系统资源实现持久化；所有业务写操作都通过应用服务及其仓储端口完成。

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
| `SKILL_DIR` | 是 | 无 | 存放固定版本诊断 Skill 的目录 |
| `LOGPARSE_REPO` | 是 | 无 | 受控的 Logparse 源码目录；Git checkout 和源码压缩包解压目录均受支持，启动时按实际内容生成指纹 |
| `LOGPARSE_CONFIG_PATH` | 是 | 无 | Logparse 工作区内的配置文件 |
| `BIND_HOST` | 否 | `127.0.0.1` | Uvicorn 监听地址 |
| `PORT` | 否 | `8000` | Uvicorn 监听端口 |
| `CLAUDE_COMMAND` | 否 | `claude` | Agent 命令，会被解析为 argv 参数模板 |
| `LOGPARSE_PYTHON` | 否 | 当前 Python | Logparse 使用的 Python 启动命令 |
| `DFX_LOG_LEVEL` | 否 | `INFO` | 结构化诊断日志级别：`DEBUG`、`INFO`、`WARNING`、`ERROR` 或 `CRITICAL` |
| `DFX_LOG_DIR` | 否 | 无 | 服务端可观测日志目录的绝对路径；配置后生成 `debug.jsonl`、`journey.jsonl` 和按 Case 渲染的人类可读日志 |

运行时限制是冻结的契约常量，不属于可配置项。V1 会拒绝 `JOB_CONCURRENCY` 以及未知的 limit、max、retention 覆盖项，避免运维人员误以为某项实际上无效的限制已经生效。

不要配置或持久化 `PROBLEM_LOCATOR_LOGPARSE_ENDPOINT` 和 `PROBLEM_LOCATOR_LOGPARSE_TOKEN`。这两个值会按任务临时创建，并在代理会话结束时删除。

## 启动服务

校验配置并启动唯一的工作线程：

```sh
uv run python -m problem_locator serve --env-file /absolute/path/to/service.env
```

对于同一个 `DATA_ROOT`，V1 只允许一个服务进程和一个 Uvicorn worker。第二个进程会因实例锁就绪检查失败而退出。请勿让多 worker 进程管理器共用同一个数据根目录。

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
| `problem_locator_create_case` | `request_id: string` req；`problem_spec: object` req；`initial_user_facts: array<{name,value}>` opt；`wait_seconds: integer` opt |
| `problem_locator_prepare_attachment` | `request_id/case_id/name/content_type` req；`expected_case_revision: integer` req；`declared_size: integer\|null` opt；`declared_sha256: string\|null` opt |
| `problem_locator_submit_supplement` | `request_id/case_id` req；`expected_case_revision: integer` req；`inputs: object<string,string>` req；`attachment_ids: array<string>` req；`wait_seconds` opt |
| `problem_locator_get_case` | `case_id` req；`wait_for_job_id: string\|null` opt；`wait_seconds` opt |
| `problem_locator_resume_case` | `request_id/case_id/expected_case_revision` req；`wait_seconds` opt |
| `problem_locator_cancel_case` | `request_id/case_id/expected_case_revision` req |
| `problem_locator_list_artifacts` | `case_id` req |

`problem_spec` 必须直接传八成员 JSON 对象，不能把该对象再次序列化成带转义符的字符串；复合对象、数组和 Map 的完整规范示例见客户端 Skill。

仓库内置的 [`.claude/skills/problem-locator-client`](.claude/skills/problem-locator-client) Skill 说明了安全的请求 ID、修订版本处理方式、上传请求头以及产物哈希校验方法。文件内容只通过 HTTP 传输，绝不会嵌入 MCP 消息。

### 客户端本地 MCP 的安装与配置

客户端不得让 Claude Code 直接连接局域网 HTTP MCP。客户端本地 MCP 不是另一个 npm 包，而是 `problem-locator` Python 包内置的 `problem-locator-client-proxy` 命令。客户端必须安装与服务端完全相同版本的包，再让 Claude Code 把它作为本地 stdio MCP 启动；该进程负责连接局域网内服务端的 `/mcp`。

在客户端安装发布 wheel（推荐）或同一版本的源码目录：

```sh
# 安装发布 wheel；把路径替换为实际交付文件
uv tool install --python 3.12 /absolute/path/to/problem_locator-1.0.0-py3-none-any.whl

# 如果客户端拿到的是同一版本源码，也可以直接安装源码目录
uv tool install --python 3.12 /absolute/path/to/xiaodao
```

两个命令二选一，不需要都执行。安装后先验证命令确实可执行：

```sh
problem-locator-client-proxy --help
```

如果 uv 提示可执行文件目录不在 PATH，执行 `uv tool update-shell` 后重新打开 Claude Code；也可以运行 `uv tool dir --bin` 找到目录，并在下方 `.mcp.json` 的 `command` 中填写 `problem-locator-client-proxy` 的绝对路径。使用源码虚拟环境而不是 `uv tool install` 时，Windows 路径是 `D:/absolute/path/to/xiaodao/.venv/Scripts/problem-locator-client-proxy.exe`，Linux/macOS 路径是 `/absolute/path/to/xiaodao/.venv/bin/problem-locator-client-proxy`。

删除或替换原来直接连接服务端的 `"type": "http"` 配置，不要同时保留两个同名 MCP。客户端项目根目录的 `.mcp.json` 应配置为本地 `stdio`；下面示例中的地址和日志路径都要替换为客户端可用的真实值：

```json
{
  "mcpServers": {
    "problem-locator": {
      "type": "stdio",
      "command": "problem-locator-client-proxy",
      "args": [],
      "env": {
        "PROBLEM_LOCATOR_MCP_URL": "http://192.168.1.20:8000/mcp",
        "PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE": "D:/logs/problem-locator/client.jsonl",
        "PROBLEM_LOCATOR_CLIENT_DFX_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

Linux/macOS 客户端可以把日志路径改为 `/var/log/problem-locator/client.jsonl` 或其他可写路径。完整配置模板见 [client-mcp-config.json](.claude/skills/problem-locator-client/references/client-mcp-config.json)。修改后重启 Claude Code，通过 `/mcp` 确认 `problem-locator` 已连接，再调用一次工具并检查日志：

```powershell
Get-Content -Wait D:\logs\problem-locator\client.jsonl
```

```sh
tail -f /var/log/problem-locator/client.jsonl
```

代理使用宽松的本地工具输入 schema，确保 Claude Code 不会在日志产生前拦截参数；同时会从上游权威 schema 生成不参与校验的 JSON 形状、必填性、默认值和 nullable 说明，帮助 Agent 正确构造调用。上游服务仍执行权威 schema 校验。每次调用的完整参数、完整响应/错误、`operation_id`、`attempt_id`、递增的 `attempt_number` 和耗时都会写入客户端 JSONL，不依赖 Claude Code debug 日志。未配置 `PROBLEM_LOCATOR_CLIENT_DFX_LOG_FILE` 时，默认日志位置是客户端项目目录下的 `.problem-locator/client-dfx.jsonl`。

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

## 附件与结果处理

准备附件时，服务会创建元数据和上传描述信息。上传文件内容时，服务会校验其准确大小与 SHA-256，校验通过后将附件转为 `READY` 状态。仅上传附件不会推进 Case；调用方必须显式将 `READY` 附件作为补充材料提交。

`WorkspaceAttachmentInput.filename_suffix` 为必填字段，但允许值为 `null`。归档文件后缀及 content-type 的校验使用冻结的公共契约辅助函数；路径形式、包含大写字母的别名以及不匹配的后缀都会被拒绝。

默认只列出可下载的公开产物。经过复核的 `USER_RESULT` 可以下载，下载内容必须与声明的字节数和 SHA-256 一致。内部 `LOGPARSE_RUN` 目录会作为后续任务的持久化输入，但永远不可下载。

## 启动恢复与重试语义

每次启动时，调度器都会创建新的运行时 epoch，并在接受新任务之前完成以下恢复流程：

1. 逐字节重放所有已持久化、已最终确定但尚未确认的 Job Outcome。
2. 完成重放后，才会把没有最终 Outcome 的旧 `RUNNING` 任务标记为 `INTERRUPTED`。
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

`validate-state` 输出规范化的 `ValidationReport`。`export-state` 输出规范化的 `StateExport`，其中包含单个状态世代、完整对象数量，以及按顺序排列的资源大小/哈希清单。导出文件必须位于 `DATA_ROOT` 之外；它只用于审计和迁移，不能替代资源备份。

创建可恢复备份：

1. 停止服务，并等待关闭流程完成。
2. 执行 `validate-state` 和 `export-state`。
3. 完整复制 `DATA_ROOT` 目录树，并尽量以原子方式保证 `state.json`、`jobs/**` 和 `resources/**` 来自同一个停机时间点。
4. 将导出文件与备份放在一起，以便核对对象数量和哈希。

恢复时，应将损坏的数据根目录保持为只读，把完整且已知可用的备份复制到一个新的绝对路径，执行 `validate-state`，并核对导出文件中的对象数量和哈希，最后使用新的数据根目录启动服务。

不要手工编辑 `state.json`，不要丢弃已经最终确定的 outbox 文件，也不要静默回退到 `state.json.prev`。

r3 状态模式与预发布阶段的 r2 数据有意保持不兼容。旧数据只能离线重建，或迁移到全新的 r3 安装中；服务不提供 r2 原地兼容路径。

### 冻结发布边界声明

以下英文短句是发布测试使用的稳定语义标识；中文解释是规范正文：

- r3 state schema is intentionally incompatible：r3 不对 r2 提供原地兼容。
- Replay every durable, finalized but unconfirmed Job Outcome：启动时先重放所有已最终确定但未确认的 Outcome。
- 当 `state.json` approaches 16 MiB 时，应启动离线迁移设计。
- 当 retained history approaches 500 Cases 时，应启动离线迁移设计。
- 需要 second service instance or high availability 时，必须迁移出单实例 JSON 架构。
- 恢复或迁移期间必须 keep the original JSON root read-only。

## PostgreSQL 迁移边界

V1 不包含 PostgreSQL、ORM、双写机制或分布式锁。当满足以下任一条件时，应开始设计离线 PostgreSQL 迁移方案：

- 需要第二个服务实例或高可用能力；
- `state.json` 接近 16 MiB；
- 保留的历史记录接近 500 个 Case；
- 状态写入延迟已经明显影响运行。

迁移时必须停止写入，导出一个规范化状态世代，通过等价的仓储/资源记录完成导入，核对所有对象数量和资源哈希，并在验收完成前保持原 JSON 数据根目录只读。

领域层、应用层和运行时层依赖冻结的端口，而不是 JSON 适配器，因此迁移仍然是一次离线适配器替换，而不是业务模型分叉。

## 安全说明与已知限制

- V1 面向可信用户、固定版本 Skill 和可信 Agent 命令所在的受控网络，不提供租户级授权。
- 服务进程和 Agent 都不是操作系统沙箱。请使用专用操作系统账户运行，并只授予必要的仓库和数据访问权限。
- MCP/HTTP 响应中不得出现密钥、原始环境变量值、服务器路径、日志归档内容、代理令牌或内部执行日志。
- Logparse 会在启动时进行指纹校验。首个符合条件的诊断任务可以解析一次日志；后续任务必须使用已持久化的 `LOGPARSE_RUN`，不得再次解包或解析原始归档。
- V1 的并发数固定为 `1`，上下文、工作区和输出限制均为固定值；持久化依赖本地文件系统，不提供多实例故障转移。
- 原生 Windows/Linux 启动验证、macOS 进程树/取消验证、确定性模拟端到端测试以及真实 Logparse 冒烟测试都属于发布门禁。测试或交接记录必须明确实际运行的平台。

### 原生启动门禁

原生门禁会在其他操作系统上主动跳过；被跳过意味着门禁尚未执行，不能视为通过。每个运行环境都必须使用同一个候选发布 Git HEAD、CPython 3.12、锁定版本的依赖，以及运行门禁时所选的 Logparse 源码目录。该目录可以来自 Git checkout 或源码压缩包解压；仅在 Git 元数据存在时记录实际 Logparse commit，且不限制为某个固定 commit。

当前候选工作区已经完成 Windows 客户端到 Linux 服务的完整发布旅程和原生 Linux 启动门禁；这不等价于原生 Windows 服务启动或 macOS 门禁。后两项仍未在本次验收中执行，不得宣称为通过；需要发布到对应平台时，仍须在同一候选版本上执行下列平台门禁，并在交接记录中准确区分“通过”和“未执行”。

macOS shell（在候选发布版本 HEAD 上执行）：

```sh
uv sync --frozen --all-groups
export S08_NATIVE_STARTUP_GATE=darwin
export SKILL_DIR="$(pwd)/.claude/skills"
export LOGPARSE_REPO=/absolute/path/to/logparse
export LOGPARSE_CONFIG_PATH=/absolute/path/to/logparse/config.yaml
export LOGPARSE_PYTHON=/absolute/path/to/logparse/.venv/bin/python
export CLAUDE_COMMAND=claude
uv run pytest tests/e2e/test_native_startup_gate.py::test_native_macos_startup_gate -q -p no:cacheprovider
```

Windows PowerShell：

```powershell
uv sync --frozen --all-groups
$env:S08_NATIVE_STARTUP_GATE = "windows"
$env:SKILL_DIR = (Resolve-Path ".claude\skills").Path
$env:LOGPARSE_REPO = "C:\absolute\path\to\logparse"
$env:LOGPARSE_CONFIG_PATH = "C:\absolute\path\to\logparse\config.yaml"
$env:LOGPARSE_PYTHON = "C:\absolute\path\to\logparse\.venv\Scripts\python.exe"
$env:CLAUDE_COMMAND = "claude"
uv run pytest tests/e2e/test_native_startup_gate.py::test_native_windows_startup_gate -q -p no:cacheprovider
```

Linux shell：

```sh
uv sync --frozen --all-groups
export S08_NATIVE_STARTUP_GATE=linux
export SKILL_DIR="$(pwd)/.claude/skills"
export LOGPARSE_REPO=/absolute/path/to/logparse
export LOGPARSE_CONFIG_PATH=/absolute/path/to/logparse/config.yaml
export LOGPARSE_PYTHON=/absolute/path/to/logparse/.venv/bin/python
export CLAUDE_COMMAND=claude
uv run pytest tests/e2e/test_native_startup_gate.py::test_native_linux_startup_gate -q -p no:cacheprovider
```

每项测试都会校验原生操作系统、Logparse 源码目录及内容指纹、从环境文件启动、`/live`、`/ready` 的全部 5 项检查、限时关闭、规范化的 `validate-state` 与 `export-state`、实例锁释放，以及第二次恢复启动。

成功结果必须记录准确的候选发布 SHA、操作系统/构建版本、架构、Python 版本、执行命令和 pytest 用例数量。

真实 Agent Backend 发布冒烟测试与确定性模拟 Agent 端到端测试相互独立。只能在隔离的临时工作区中，使用已经完成身份验证的 Claude Code 安装执行。以下命令会禁用仓库自定义配置、会话持久化，以及除写入固定输出文件之外的所有工具：

```sh
export S08_REAL_AGENT_GATE=1
export S08_REAL_AGENT_COMMAND='/absolute/path/to/claude -p --safe-mode --no-chrome --no-session-persistence --dangerously-skip-permissions --tools Write --model haiku --effort low --max-budget-usd 0.10'
uv run pytest tests/e2e/test_real_agent_backend_gate.py -q -p no:cacheprovider
```

该门禁会验证真实 Claude Code 版本、通过生产 `AgentBackend` 传递标准输入、完全一致的规范化 `AgentJobOutcome` 字节、不可变的输入/运行时标记、输出拓扑、限时执行以及进程树清理。测试被跳过不等于通过。

### 干净安装包门禁

该门禁会构建候选发布版本的 wheel，从 `uv.lock` 导出仅包含运行时依赖且带哈希的数据，在全新的 CPython 3.12 环境中安装两者，并从源码目录之外执行每条已安装命令。仅当所选 uv 缓存已经完整时，才设置 `S08_UV_OFFLINE=1`；在冷启动 runner 上应保持为 `0`。

```sh
export S08_INSTALLED_DISTRIBUTION_GATE=1
export S08_UV="$(command -v uv)"
export S08_UV_OFFLINE=0
export SKILL_DIR="$(pwd)/.claude/skills"
export LOGPARSE_REPO=/absolute/path/to/logparse
export LOGPARSE_CONFIG_PATH=/absolute/path/to/logparse/config.yaml
export LOGPARSE_PYTHON=/absolute/path/to/logparse/.venv/bin/python
export CLAUDE_COMMAND=/absolute/path/to/claude
uv run pytest tests/e2e/test_installed_distribution_gate.py -q -p no:cacheprovider
```

预期结果为恰好 1 个测试通过。该测试会校验：wheel 只能从新环境的 `site-packages` 导入；运行时依赖版本已锁定；运行环境中不包含 pytest 和 Hatchling；Logparse 源码目录可以生成稳定内容指纹；Skill 产品哈希正确；可以通过环境文件启动已安装服务；`/live` 和 `/ready` 的全部 5 项检查通过；服务可以限时关闭；已安装的 `validate-state` 和 `export-state` 命令输出规范化结果。

如果在最终 S08 仅交接提交之前获得原生测试结果，应将真实命令和摘要加入 `handoff/S08.json.tests[]`。如果结果在该不可变提交之后才产生，不得修改或重写交接记录；应将相同字段附加到下游发布验证记录中，并保留 S08 的限制，直至经过批准的后续交接记录正式纳入该证据。

## 发布检查

请显式运行全部测试根目录；不要假设历史遗留的裸 pytest 配置一定包含每个测试套件：

```sh
uv run pytest tests/contracts tests/unit tests/integration tests/e2e
uv run python -m compileall -q src tests
uv lock --check
git diff --check
```

真实 Logparse、进程树/取消行为、干净环境安装、安装后导入/CLI/服务冒烟测试、fixture manifest 以及 Git 祖先/blob 完整性都属于相互独立的候选发布门禁。除非实际执行并通过，否则不得将其报告为通过。
