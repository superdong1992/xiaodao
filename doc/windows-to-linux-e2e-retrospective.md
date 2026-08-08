# Windows → Linux 真实端到端测试复盘

## 文档目的

本文记录基于提交 `c31cc03848155d03b9a35776555e413f26b264ad` 开展 Windows Claude Code → Linux Docker 真实端到端验收时发现的问题、对应修复，以及这些问题未被原 Linux → Linux 测试暴露的原因。

相关生产修复和测试工具已由提交 `82bfe22f11363c1b8ff65cc564dce3dd14fbf5e4` 引入。这里的“Windows → Linux”描述的是客户端和服务部署边界，不代表所有缺陷都是 Windows 专属缺陷。真正增加覆盖率的是以下条件首次同时出现：

- Windows 上的真实 Claude Code 客户端和严格 MCP 配置；
- Linux 容器中的非 root 服务进程；
- 通过 Claude Code 调用 DeepSeek 的真实 Agent；
- 真实 Logparse CLI、现场生成的诊断 Skill 和真实日志 ZIP；
- HTTP 上传、异步 Job、持久化、重启和结果下载组成的完整业务链路；
- Windows CRLF、PowerShell、`curl.exe` 与 Linux POSIX 权限之间的系统边界。

## 修复的生产实现问题

### 1. MCP `outputSchema` 与 Claude Code 不兼容

七个 MCP 工具原有的输出 Schema 以 `$defs` 和 `anyOf` 表达成功、失败 Envelope，但顶层没有显式的 `type: object`。官方 SDK 能够处理该 Schema，真实 Claude Code 的工具发现则要求顶层对象类型，导致客户端无法稳定发现或使用工具。

修复位于 `src/problem_locator/interfaces/mcp_server.py`：

- 保留原有 `$defs`、`anyOf` 和成功/失败 Envelope；
- 只补充顶层 `type: object`；
- 如果未来 Pydantic 生成非对象根类型，服务启动时 fail-closed；
- 不改变七个工具名称、请求 DTO、响应 DTO、HTTP 接口或持久化 Schema。

新增回归同时验证 Draft 2020-12 元 Schema、成功和失败响应，并拒绝标量、数组以及缺字段 Envelope。installed-distribution gate 还会启动真实 Uvicorn loopback 服务，通过官方 MCP SDK 执行 `initialize` 和 `tools/list`，确认恰好发现七个工具。

### 2. 用户补充事实被错误地要求保持列表顺序

应用层曾把 `add_user_facts` 当作有序列表比较。真实 Agent 返回的 JSON 字段和数组顺序可能与服务器预期顺序不同，即使事实集合完全等价，也会被拒绝。

修复位于 `src/problem_locator/application/external_commands.py`：在比较前按稳定的 `item_id` 排序，使顺序不再成为业务语义，同时继续严格校验事实内容。

### 3. Logparse 仓库在 root 安装、非 root 运行时不受 Git 信任

容器准备阶段由 root 放置 Logparse 仓库，生产服务由 `plagent` 用户执行。Git 的 dubious ownership 保护会拒绝读取该仓库，从而导致指纹和真实集成失败。

修复位于 `src/problem_locator/integrations/logparse/fingerprint.py`：

- 清除环境中可能污染命令的 `GIT_*` 变量；
- 禁用系统级和用户级 Git 配置；
- 仅对当前固定仓库按命令设置精确的 `safe.directory`；
- 禁止使用全局通配符信任。

这保留了 Git 的安全边界，同时支持跨 UID 的只读部署。

### 4. 真实 Logparse 输出包含可选 `module` 字段

真实 CLI 输出的 target 可能同时包含 `module`、`module_key` 和 `module_name`。原解析器只接受测试夹具中的较窄字段集合，因此真实输出被当作非法结果。

修复位于 `src/problem_locator/integrations/logparse/outputs.py`：接受经过类型和一致性校验的可选 `module` 字段；它不能覆盖或绕过既有 anchor 校验。

### 5. 现场生成的 Skill 在严格 umask 下不可被服务用户读取

Wiki 转诊断 Skill 的生成器原先依赖进程默认 umask。生成用户与服务用户不同时，目录或文件可能缺少读取、遍历权限。

修复位于 `.claude/skills/wiki-to-diagnosis-skill/scripts/generate_diagnosis_skill.py`：发布前将目录规范为 `0755`、普通文件规范为 `0644`，确保现场生成物可被非 root 服务安全读取。

### 6. Agent 工作区根目录缺少写入隔离

真实 Agent 不一定像 fake Agent 一样只写约定文件。测试中出现了在工作区根目录创建 `err.txt` 等临时文件的行为，这会破坏 broker 对受控工作树的假设。

修复位于 `src/problem_locator/runtime/agent_backend.py`：Agent 运行期间临时移除工作区根目录写位，结束后可靠恢复；保护失败时拒绝继续。允许写入的位置仍由运行时显式提供。

### 7. Agent 上下文中的输出契约不够精确

fake Agent 会直接构造正确对象，不需要理解提示词。真实模型必须从上下文中识别唯一、权威、可执行的契约。原上下文未能充分约束 Schema、原子写入、资源引用和 continuation 行为，容易产生结构近似但不可接受的输出。

修复涉及：

- `src/problem_locator/runtime/context_builder.py`：嵌入已安装的 S00 `AgentOutcome` 和 `UserResultPayload` 精确 Schema，要求唯一契约标记，并把输出契约放在 `RESOURCE_MANIFEST` 之前以维持权威性和近因性；
- `src/problem_locator/runtime/assets/output-contracts/route/output-contract.md`；
- `src/problem_locator/runtime/assets/output-contracts/diagnose/output-contract.md`；
- `src/problem_locator/runtime/assets/output-contracts/review/output-contract.md`。

契约现在明确要求：

- 仅在授权目录中工作，原子写入 `job_outcome.json.tmp` 后再替换正式结果；
- 参数组 A 必须是 `caller_service`、`server_service`、`rpc_method` 和 `problem_time`；
- 先取得日志附件，再运行 Logparse；
- `order_id` 只能来自当前有效的 `USER_INPUT` 事实，不能从日志推断；
- 首次诊断只产生一个 `LOGPARSE_RUN`，continuation 必须复用它并只执行 `mech-target-logs`；
- Evidence 必须绑定源附件、Logparse run 和候选结论；
- 使用目录 TreeManifest hash，而不是错误地使用 `parse_manifest` hash；
- Logparse 请求必须经过 canonical JSON 自检，并在唯一一次 CLI 调用前写入固定 sentinel。

### 8. 诊断 Skill 的真实业务锚点不够稳定

真实模型会尝试从服务名猜测 Logparse anchor，但 `checkout-synthetic`、`inventory-synthetic` 是业务服务名，不等同于日志中的 module、slot、process 和 pid。

生成器和生产 Skill 现在固定使用 Wiki 声明的 client/server anchor，并禁止把 caller/server service 名直接填入 anchor。这样真实 Logparse 能稳定选择客户端和服务端目标日志。

## 修复的测试流程问题

以下内容属于测试基础设施修复，不是产品 API 缺陷：

- 在容器内以 `core.autocrlf=false` 从固定提交重新克隆源码，避免 Windows 工作树 CRLF 改变固定字节、fixture hash 或 Skill hash；
- 修复 `fake_agent.py` 未消费 stdin 的竞态，要求它读取并核验 prompt 后再退出；
- 修复真实 diagnose gate 构造的 RUNNING Job 缺少 `started_at` 和 `runtime_epoch`；
- 将手工测试 manifest 和 previous outcome 权限设为与生产 broker 一致的只读 `0444`；
- 更新发生合法变化后的 fixture manifest hash；
- 为所有 Claude 子进程设置明确的有界超时，避免外部模型或传输卡住后无限等待；
- installed-distribution gate 改为真实 Uvicorn + 官方 MCP SDK 探测；
- 真实 Logparse gate 以非 root 身份执行固定 CLI，证明一次 `parse` 和后续 `mech-target-logs` 复用；
- 清理动作按精确容器和数据卷名称执行，失败现场保留，最终报告执行零泄漏密钥扫描。

## 为什么原 Linux → Linux 端到端测试没有发现

### 主要覆盖缺口

| 覆盖维度 | 原 Linux → Linux 测试 | Windows → Linux 真实验收 | 因而漏掉的问题 |
| --- | --- | --- | --- |
| MCP 客户端 | 内部调用、SDK 或宽松客户端 | Claude Code `--strict-mcp-config` | 顶层 `outputSchema.type` 缺失 |
| Agent | hand-written fake Agent | Claude Code + DeepSeek 真实模型 | 契约歧义、事实顺序、临时文件、错误推断 |
| Logparse | 隔离测试和较窄 golden fixture | 固定真实仓库与真实 CLI | `module` 字段、真实 anchor、parse/run 复用 |
| Linux 身份 | 安装和执行通常是同一 UID | root 准备、`plagent` 执行 | Git ownership、生成物权限 |
| 工作区安全 | fake Agent 遵守约定 | 不受信任 Agent 子进程 | 根目录越界写入 |
| 系统边界 | Linux 文件和 shell | Windows CRLF、PowerShell、`curl.exe` → Linux | 固定字节、命令和上传边界问题 |
| 业务组合 | 多个 seam 分别验证 | MCP、上传、Job、Agent、Logparse、重启串联 | 单点均通过但组合状态不成立 |
| JSON 生成 | 测试直接生成 canonical 对象 | 模型生成后由 broker 校验 | 顺序差异、近似 Schema、非 canonical 请求 |

### 根本原因

原测试验证了大量确定性逻辑，但业务旅程的关键部分由 fake Agent 编码了“正确答案”。它能证明 broker 在收到正确 `JobOutcome` 时如何工作，却不能证明真实模型能够：

1. 通过严格 MCP discovery 找到工具；
2. 正确理解上下文中的多个说明和 Schema；
3. 构造 canonical Logparse 请求；
4. 遵守工作区写入限制；
5. 在 continuation 中复用既有运行产物；
6. 跨 UID 读取真实 Git 仓库和现场生成 Skill。

因此，Windows 是暴露问题的触发环境，根因则主要是“真实客户端 + 真实模型 + 真实依赖 + 跨身份部署 + 完整组合链路”此前没有同时进入一个验收闭环。

## 必须长期保留的回归门禁

### 确定性门禁

- 完整运行 contracts、unit、integration 和 e2e 测试；
- 执行 `compileall`、`uv lock --check` 和 `git diff --check`；
- 平台不匹配产生的 skip 记录为“未执行”，不能计为通过；
- 七个 MCP 工具的 Schema 和成功/失败 Envelope 必须通过官方 SDK 校验。

### 真实组件门禁

- installed distribution 必须实际启动 Uvicorn 并完成 MCP `initialize`、`tools/list`；
- route、diagnose 和 review Agent gate 必须使用真实模型、有界超时、非 root 用户和受保护工作区；
- Logparse 必须从固定真实提交运行真实 `parse`，continuation 只能复用同一个 `LOGPARSE_RUN` 执行 `mech-target-logs`；
- 诊断 Skill 必须从 Wiki 现场生成并校验 product hash，不能替换成测试 fake Skill。

### 跨系统业务门禁

- Windows Claude Code 使用临时 inline strict MCP config；
- 上传由固定 Base64 fixture 重建的四成员真实 ZIP，并校验大小和 SHA-256；
- 必须通过真实 `curl.exe` 按 UploadDescriptor 四个 header 上传，并显式提交 READY attachment；
- Case 必须经过参数组 A、`log_archive`、`order_id`、`REVIEWING`，最终到达 `RESOLVED` 和 `ACCEPTED`；
- 公共 artifact list 只能看到一个 `USER_RESULT`，内部 `LOGPARSE_RUN` 不可公开列出或下载；
- 下载结果必须校验 header、长度、SHA-256、Canonical JSON 和 `UserResultPayload`；
- 停机后执行 `validate-state`、`export-state`，使用同一数据卷重启并证明 ID、revision、状态和结果 hash 不变；
- 所有命令、日志、报告和结果执行密钥扫描，命中数必须为零。

## 标准执行流程

后续测试应以 `tools/e2e/run-windows-linux-e2e.ps1` 为唯一顶层入口。`tools/e2e/harness` 下的脚本是由入口编排的实现组件；README 中带固定 attempt 名称的命令用于历史取证，不应复制为新一轮运行流程。

### 1. 前置检查

运行前确认：

- Windows 已安装 `docker.exe`、Windows PowerShell 5.1 和 Claude Code；
- 本机已有固定 Ubuntu 镜像，正式验收不允许临时拉取其他 tag 替代；
- Problem Locator、Logparse 和 problem-locator-mcp 源目录存在，提交身份符合本轮计划；
- `C:\Users\admin\.claude\settings.json` 只读提供；派生配置只能读取白名单字段，任何脚本都不得打印密钥或把密钥复制到持久介质；
- `127.0.0.1:18000` 未被无关进程占用；
- `.tmp\pl-e2e-evidence` 和 `.tmp\pl-e2e-cache` 只存放测试证据与缓存，不纳入产品 patch；
- 先运行 `tools/e2e/test-harness.ps1`，确认编排器自身的静态约束和失败路径仍有效。

### 2. 开发阶段真实业务验收

从仓库根目录运行：

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\tools\e2e\run-windows-linux-e2e.ps1 `
  -Profile Fast
```

`Fast` 必须从空数据卷依次完成：

1. 冻结源码 patch、版本、镜像和证据清单；
2. 创建 Linux 环境，安装固定依赖并现场生成诊断 Skill；
3. 以非 root 身份运行真实 Logparse gate；
4. 启动服务，分别从容器和 Windows 检查 `/live`、`/ready` 和七个 MCP 工具；
5. Windows Claude Phase1 创建 Case、提交参数组 A 并取得上传描述；
6. 使用真实 `curl.exe` 上传固定日志 ZIP；
7. Windows Claude Phase3 提交附件和 `order_id`，等待 `RESOLVED`；
8. 下载并审计公开 `USER_RESULT`；
9. 停止服务，执行 `validate-state` 和 `export-state`；
10. 使用同一数据卷创建新容器，重新查询并下载结果；
11. 对比重启前后的 Case、revision、Artifact 和结果 hash；
12. 生成 `verification-report.json` 和最终密钥扫描报告。

### 3. 完整发布验收

生产实现发生任何变化时运行：

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\tools\e2e\run-windows-linux-e2e.ps1 `
  -Profile Release
```

`Release` 在全新的 `Fast` 业务旅程之后，并行运行确定性门禁和真实 Agent 门禁。总运行阶段有明确的 480 秒发布 SLA；超时即失败。

只有已经存在一次被接受的 `Fast` 证据，并且当前生产 patch identity 与该证据完全一致时，才允许运行：

```powershell
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File .\tools\e2e\run-windows-linux-e2e.ps1 `
  -Profile ReleaseGates `
  -BusinessEvidenceRoot <accepted-fast-evidence-directory>
```

`ReleaseGates` 不是跳过业务验收的通用捷径。生产文件变化、证据缺失、密钥扫描失败或 patch hash 不一致时必须拒绝复用。

### 4. 失败后的处理顺序

统一入口会在 `verification-report.json` 中记录 `failure_stage`。按以下顺序定位，不要从头盲目重跑：

1. `host-preflight` / `evidence-freeze`：检查版本、路径、固定提交和 patch identity；
2. `base-image` / `fast-environment`：检查离线缓存、Linux 权限、Git trust、Skill hash 和真实 Logparse；
3. `fast-service-preflight`：检查进程身份、五项 readiness、loopback 端口和 MCP discovery；
4. `fast-windows-claude-phase1`：检查 Claude 超时、七工具发现、参数组 A 和 requirement 状态；
5. `fast-real-upload`：检查 ZIP 大小/hash、UploadDescriptor 四个 header 和 `curl.exe` 退出码；
6. `fast-windows-claude-phase3`：检查附件 READY、`order_id`、Job/Agent 输出和 Case 状态；
7. `fast-before-restart-audit` / `fast-restart-persistence`：检查状态导出、数据卷复用和结果一致性；
8. `release-parallel-gates`：分别查看 deterministic 与 agents 状态文件，不要把平台 skip 当成通过。

实现或契约问题应添加最小回归测试后修复，再从相关低成本门禁逐级升阶。DeepSeek 的 429 或 transport 错误只允许有限重试；鉴权、额度或持续服务不可用应明确记录为外部阻塞，禁止无限重试。

## 避坑清单

### 环境与源码

- 不要把 Windows 工作树直接作为 hash-sensitive Linux 执行源。必须在容器内使用 `core.autocrlf=false` 的固定提交副本，否则 CRLF 会改变 fixture、Schema 或 Skill 字节。
- 不要用浮动镜像 tag、浮动依赖或自动升级的 Claude Code做发布验收。报告必须记录镜像 digest、Python、uv、Claude 和三个仓库提交。
- 不要假设“容器里都是 Linux 用户，所以权限一致”。安装用户和运行用户必须明确分离测试，重点检查 Git ownership、目录 execute bit、文件 read bit 和 DATA_ROOT 所有权。
- 不要为了绕过 dubious ownership 设置全局 `safe.directory=*`；只允许命令级精确仓库路径。

### 密钥与网络

- DeepSeek token 不得出现在命令行、Docker environment、镜像层、数据卷、日志或报告中；Claude settings 只能按约定只读使用，临时派生配置放 tmpfs 且权限为 `0600`。
- 服务只发布 `127.0.0.1:18000:8000`。不得为方便调试改成局域网监听。
- 不要在 PowerShell 中使用可能映射到 `Invoke-WebRequest` 的 `curl` 别名；上传和下载必须显式调用 `curl.exe`，并设置连接、总时长和响应大小上限。
- 不要把原始 Claude prose 当作业务状态。只消费关联后的 `tool_use` / `tool_result` 和服务返回对象。

### MCP、Agent 与业务语义

- 不要只验证 MCP handler 能直接调用；必须通过官方 SDK 和真实 Claude Code验证 discovery，特别是顶层 `outputSchema.type` 和恰好七个工具。
- 不要用 fake Agent 或手工注入 `job_outcome.json` 证明真实链路通过。fake 只适用于确定性单元测试，不能替代模型对契约的理解。
- 不要依赖 JSON 对象键顺序或等价事实列表的输入顺序。业务比较应基于稳定身份和 canonical 表示。
- 不要让 Agent 在工作区根目录自由写临时文件；允许写入范围必须由 broker 建立并由权限强制执行。
- 不要仅靠自然语言描述输出格式。上下文必须包含精确已安装 Schema、唯一权威标记、原子写入规则和资源引用约束。
- 不要从日志推断 `order_id`，也不要提前请求它；它只能来自附件处理后新增的活动 `USER_INPUT` requirement。

### Logparse 与持久化

- 不要用 fake Logparse 仓库、伪造 parse outcome 或只测 fixture parser。至少一个门禁必须运行固定真实 `cli.py`。
- 第一次诊断只能执行一次 `parse` 并持久化一个 `LOGPARSE_RUN`；补参 continuation 必须引用相同 run 和源 attachment，只执行 `mech-target-logs`。
- 不要混淆目录 TreeManifest hash 与内部 `parse_manifest` hash；Artifact 和 Evidence 必须使用各自契约规定的标识。
- 不要把内部 `LOGPARSE_RUN` 暴露在公共 Artifact 列表或下载接口中。公开面只能出现最终 `USER_RESULT`。
- 不要只验证重启后“服务能启动”。必须比较重启前后的 Case ID、revision、状态、Artifact ID、Content-Length 和 SHA-256。

### 超时、重试和证据

- 不要直接启动无超时的 Claude 子进程。统一通过 `tools/e2e/bounded-process.ps1`，超时后终止完整进程树并记录 stdout、stderr 和退出原因。
- 不要在失败后复用同一个空数据卷冒充 clean run。调试可以保留现场或从持久状态恢复，最终接受证据必须从全新数据卷产生。
- 不要覆盖已有 evidence 文件。运行收据使用唯一 attempt 目录和 CreateNew 语义，失败现场原样保留。
- 不要在失败时自动删除容器和数据卷；先有界停止并保留证据。只有成功后才按本轮精确名称清理资源。
- 不要把 skip 计入 pass，也不要只看 pytest 退出码；最终报告必须分别记录 passed、failed、error、skipped/not-executed。
- 不要在生产 patch identity 不一致时使用 `ReleaseGates` 复用旧业务证据。

## 快速测试策略

入口为 `tools/e2e/run-windows-linux-e2e.ps1`，提供三种 profile：

- `Fast`：从空数据卷完成真实 Windows → Linux 业务旅程和持久化重启，优先给开发阶段快速反馈；
- `Release`：在 `Fast` 基础上运行完整发布门禁；
- `ReleaseGates`：只在生产 patch identity 与已接受的 `Fast` 业务证据完全一致时复用该证据，并行运行确定性门禁和真实 Agent 门禁。测试代码或报告代码变化可以重跑门禁，但任何生产实现变化都必须重新执行空数据卷的 `Fast` 旅程。

所有外部 Claude 进程都必须通过 `tools/e2e/bounded-process.ps1` 执行。超时是明确失败，不得用无限重试掩盖外部鉴权、额度、网络或模型行为问题。

### TODO：分段无 Mock E2E

- 将真实旅程拆成六段：环境与服务、ROUTE 与需求、上传/Logparse/目标日志、DIAGNOSE 与复验、REVIEW、发布/ZIP/重启恢复。
- 每段成功后冻结 `DATA_ROOT`、资源、State、阶段清单及相关版本/哈希；调试时克隆最近的有效检查点到新的临时 `DATA_ROOT`，不得复用失败或已进入 `UNRESOLVED` 终态的 Case。
- 检查点从最早受影响阶段起失效：Skill/Logparse/目标解析从第 3 段，Verifier/Result 从第 4 段，Review 合同从第 5 段，ZIP/发布/存储从第 6 段；State schema、核心合同或 bootstrap 变更时全部失效。
- 发布前仍必须从全新 `DATA_ROOT` 完整执行一次无 Mock `Release` journey。

### TODO：局域网链路提效与效率 DFX

- 先在真实 Windows → Linux 局域网链路建立耗时基线；在现有服务端 DFX 和 E2E 步骤计时上，补齐 Host 等待、网络传输、排队、ROUTE、Logparse、DIAGNOSE、服务端复验、REVIEW、发布与下载的统一耗时瀑布，并用 `correlation_id`、`request_id`、`case_id` 和 `job_id` 串联。
- 自动汇总各阶段耗时、重试/超时和传输字节，定位主要瓶颈；据此设定阶段预算和回归门禁，再针对最大耗时项优化。
- DFX 必须脱敏且有大小/轮转上限；保持 Windows 客户端 HTTP 直连，不引入本地 MCP、代理或 Hook。若确需客户端专用 DFX，须单独明确采集、脱敏和部署方案后再实施。

## 已通过的历史验收记录

2026 年 8 月的最终验收采用以下固定关键环境：

- Ubuntu 镜像 digest：`sha256:3131b4cc82a783df6c9df078f86e01819a13594b865c2cad47bd1bca2b7063bb`；
- CPython `3.12.13`、uv `0.11.32`、Claude Code `2.1.150`；
- DeepSeek 模型映射：`deepseek-v4-flash[1m]`；
- Logparse 提交 `a233b500…`，problem-locator-mcp 提交 `97d04465…`；
- 真实日志 ZIP：2367 bytes，SHA-256 `194f69fecd8dc8d40d1aedeb6fc25d2b7b4922b176be2b15be73ffe386cc5064`。

接受的无 Mock 业务旅程记录：

- attempt 68；
- Case：`d421187f-4433-4969-8ddc-ec66c9172f5a`；
- 公开 Artifact：`1e6ca3fc-0e9b-539a-ba3d-7240d77f96fb`；
- 内部 Logparse run：`98237fae-f1ec-590e-a7de-a418eefbed18`；
- 最终状态：`RESOLVED` / `ACCEPTED`；
- 业务旅程耗时：326.041 秒。

最终 ReleaseGates 记录为 attempt 72：2121 项测试通过门禁，0 failure、0 error，10 项平台 skip 单列为未执行；门禁耗时 164.312 秒，证据目录扫描 327 个文件，密钥命中为 0。

这些 attempt ID 只用于审计历史，不允许作为后续运行的状态输入。新的生产 patch 必须从空数据卷重新产生接受证据。

## 相关入口

- `tools/e2e/run-windows-linux-e2e.ps1`：快速和发布 profile 的统一入口；
- `tools/e2e/test-harness.ps1`：测试 E2E harness 自身；
- `tools/e2e/harness/README.md`：Windows Claude Code 业务旅程和底层证据说明；
- `tools/e2e/harness/restart/README-restart.md`：持久化重启验收；
- `tools/e2e/product-patch-files.txt`：决定业务证据是否允许复用的生产文件集合。
