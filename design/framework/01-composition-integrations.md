# 入口、装配与外部集成

[返回框架设计](README.md) · [逐文件索引](file-index.md)

本章覆盖包根目录、`entrypoints/` 和 `integrations/`。这些文件负责从进程配置建立服务、统一生命周期，并把外部 Logparse 和文件格式接入框架。它们不替代领域层的状态转换，也不允许外部工具直接提交 Case 终态。

## 1. 生产装配与生命周期

生产调用顺序为 `python -m problem_locator` → `bootstrap.main()` → `entrypoints.cli.main()` → `Settings.load()` → `bootstrap.create_app()`。CLI 用 `CliHooks` 接收装配函数，因此参数解析本身不需要启动仓储和工作线程。

`bootstrap._assemble()` 先检查 Logparse 与运行时资产配置，再检查数据根、获取实例锁，建立仓储和文件服务。随后连接领域协调器、应用服务、模型后端、会话服务、追问、运行时、调度器、归档和清理服务。应用服务在构造时需要 Dispatcher，调度器又需要应用服务，因此使用只能绑定一次的 `LateBoundDispatcher` 解开构造依赖。

| 阶段 | 关键动作 | 失败边界 |
| --- | --- | --- |
| 配置与资产 | 解析不可变 Settings；计算 Logparse 身份；读取并固定运行资产 | 无效配置转换为 `CONFIG_INVALID`，不启动诊断 |
| 数据根与实例锁 | 检查格式标记与目录，取得 `FileInstanceLock` | 旧格式、目录异常或其他进程占用时拒绝接单 |
| 仓储与组件 | 建立 PostgreSQL 连接、业务仓储、资源服务与依赖图 | 仓储异常保留为启动失败状态，避免假装就绪 |
| 启动准备 | `SchedulerService.start()` 创建新 epoch 并启用任务领取，不读取或重放历史 Job | 随后网站 Agent 恢复会话投影，再启动归档、保留和经验 Worker |
| HTTP 生命周期 | ASGI lifespan 启动/关闭组合对象及 MCP session manager | readiness 区分配置、锁、状态、目录和恢复检查 |
| 关闭 | 共用截止时间，依次停止经验、会话、调度、保留和归档服务 | 线程未安全结束时不释放实例锁和仓储；不会允许第二实例抢先进入 |

`ServiceStateAdmin` 面向已运行实例，`StandaloneStateAdmin` 面向离线校验和导出。离线管理临时持锁，不运行 Scheduler，不能返回“在线服务已就绪”。导出时检查仓储 generation 和资源元数据，生成规范 JSON；不是直接复制任意内存对象。

### 包根目录逐文件设计

| 文件 | 主要接口 | 详细职责与依赖 |
| --- | --- | --- |
| [__init__.py](../../src/problem_locator/__init__.py) | `__version__` | 公开软件版本 `8.2.0`。导入包不建目录、不启动线程、不获取实例锁。 |
| [__main__.py](../../src/problem_locator/__main__.py) | `main(argv, stdout, stderr)` | 支持 `python -m problem_locator`。延迟导入 `bootstrap.main`，保留可注入字节输出流和退出码，模块导入与真正启动分开。 |
| [bootstrap.py](../../src/problem_locator/bootstrap.py) | `build_service`、`create_app`、`create_state_admin`、`ServiceComposition`、`cli_hooks` | 唯一生产装配点。提供 UTC 时钟、随机/派生 ID、按 generation 通知的 Condition、延迟 Dispatcher、在线/离线状态管理、保留 Worker 和生命周期管理。失败时以 `_StartupFailureOwner` 持有已取得的资源，向接口暴露不可用状态。 |
| [operational.py](../../src/problem_locator/operational.py) | `OperationalState`、`OperationalFault` | 用线程锁维护本进程是否接单、故障关联与已确认 Job。`record()` 记录失败即停止接收新任务；`require_accepting()` 返回不泄漏其他 Case 标识的错误。它不写业务终态，不跨 epoch 复用。 |
| [diagnostics.py](../../src/problem_locator/diagnostics.py) | `configure_diagnostics`、`bind_diagnostics`、`log_event`、`HttpDiagnosticsMiddleware` | 用 `contextvars` 关联请求、Case、Job；格式化单行 JSON，记录 HTTP 状态、耗时和异常。格式化器处理递归、不可序列化值和自身异常；文件输出交给 `storage/log_rotation.py`。 |
| [journey.py](../../src/problem_locator/journey.py) | `JourneyEvent`、`record_journey_event`、`record_stage_*` | 定义版本化语义事件和递增序号，文件模式从已有最后一条记录续接；将阶段开始、完成和失败写入受限 JSONL。对合同对象生成适合旅程分析的投影，与 DFX 共用关联上下文。 |
| [journey_renderer.py](../../src/problem_locator/journey_renderer.py) | `load_journey`、`render_detailed`、`render_brief`、`render_journey` | 严格读取 Journey，按 Case 汇总问题、状态、阶段、Methods 结论和故障，输出详细/简要文本及 `RenderJourneyReceipt`。输出使用原子替换，引用原始行号供核查；不调用模型。 |
| [journey_timing.py](../../src/problem_locator/journey_timing.py) | `analyze_timing`、`TimingReport`、`JobTiming` | 从事件时间、阶段跨度和后端遥测拆分排队、处理、交付、用户等待与未分类耗时。按优先级分配重叠区间，避免简单相加重复计算；保留证据行号和数据缺口说明。 |

## 2. 配置、命令与离线操作

配置文件只提供环境变量值，不直接创建服务。显式环境变量覆盖 `.env`；路径、URL、整数、布尔值及开关由 `Settings.load()` 及其配置辅助函数统一解析和验证，普通 dataclass 构造不执行这套检查。生产配置要求 `DATABASE_URL`，并校验连接池大小。网站认证配置支持 `redis` 与 `trusted_header`；默认 `redis`，具体归属规则见接口章节。

诊断并发分为 ROUTE Worker、DIAGNOSE Worker、Logparse 并发和归档 Worker。调高模型并发不会自动增加 Logparse 并发，也不会把单实例改成多进程服务。生产默认关闭报告追问和通用经验库；默认专用报告策略为 `off`。

| 文件 | 主要接口 / 输入输出 | 详细设计 |
| --- | --- | --- |
| [entrypoints/__init__.py](../../src/problem_locator/entrypoints/__init__.py) | 空 `__all__` | 标识进程入口支持包，不隐式导出或初始化具体实现。 |
| [entrypoints/env_file.py](../../src/problem_locator/entrypoints/env_file.py) | `load_env_file`、`merged_environment` | 读取指定 dotenv 文件，拒绝缺失值和无效文件，以显式环境映射覆盖文件值；错误用 `EnvFileError` 表示，避免在各 CLI 重复实现优先级。 |
| [entrypoints/settings.py](../../src/problem_locator/entrypoints/settings.py) | `Settings.load`、`load_database_configuration`、`load_website_auth_configuration` | 不可变配置对象统一保存目录、URL、命令、并发、日志、PostgreSQL、网站认证、Methods 策略和可选扩展，校验支持的变量取值；数据库 URL 不进入对象 repr。产品仅支持 Linux Server，但当前 `serve` 入口没有单独的平台拒绝检查；`run_uvicorn` 强制单 worker，离线升级及导入入口另有 Linux 校验。 |
| [entrypoints/cli.py](../../src/problem_locator/entrypoints/cli.py) | `main`、`CliHooks`、`run_uvicorn` | 解析 `serve`、`validate-state`、`export-state`、`render-journey`、`replay-job`、`replay-method-rejection`。把配置/参数/运行异常映射成统一错误和退出码；导出文件原子写入，uvicorn 固定单 worker。 |
| [entrypoints/replay.py](../../src/problem_locator/entrypoints/replay.py) | `ReplayRequest`、`ReplayResult`、`run_replay_job`、`run_method_validation_replay_v2` | 为指定 Job 构造新的隔离数据根，校验源/目标不重叠，投影所需状态和资源闭包，复制执行记录并比较资产绑定。执行后写重放 manifest 与阶段结果；被拒 Methods 响应可用当前校验器重放。该工具自身的数据根逻辑需单独核对，不等同于生产 PostgreSQL 恢复入口。 |
| [entrypoints/data_upgrade.py](../../src/problem_locator/entrypoints/data_upgrade.py) | `upgrade_data_root`、`main` | Linux 离线旧 SQLite 格式升级。持源锁、检查目录与普通文件、统计哈希，复制到暂存目录后修改快照/会话数据，并发布新目录和升级回执。支持先检查后 `execute`，源目录不作为目标覆写；不承担 PostgreSQL 导入。 |
| [entrypoints/postgres_import.py](../../src/problem_locator/entrypoints/postgres_import.py) | `import_data_root`、`main` | 从已停服、兼容格式的 SQLite 源导入空 PostgreSQL 和新数据根。验证待处理任务、附件、追问、表结构和行摘要，建立路径映射与数据库/目录标记，写 `postgresql-import.receipt.json`；连接信息从指定环境变量读取。 |

离线重放、旧格式升级和 PostgreSQL 导入解决不同问题。旧格式升级先使历史 SQLite 达到可导入格式；导入负责切换持久化介质；重放只复现指定任务。它们都不能绕过线上实例锁直接修改正在服务的数据。

## 3. Logparse 集成

### 3.1 请求与信任边界

`build_logparse_runtime()` 计算 Logparse 仓库、配置和 Python 解释器身份，返回版本化资产和 `PinnedLogparseBrokerFactory`。每个 Job 获得独立 Session；Session 绑定工作区清单、Job、取消令牌和服务端解析好的执行计划。

专用流程使用 `parse-targets` 或 `target-logs`，通用预处理使用 `parse-only`。前者从附件生成解析结果并筛出目标日志，第二种复用已有解析产物提取目标日志，第三种准备通用日志。产品为 `default` 时省略上游 `--product`。问题时间、锚点、附件/产物必须与服务端计划一致，模型不能自行换源。

当前诊断主流程由服务端直接驱动 broker。`problem-locator-logparse` CLI 仍保留受限调用协议，但它运行在服务端 Job 工作区的边界内，不是安装在用户 MCP Client 上的代理。

### 3.2 执行与关闭

Session 执行前验证输入文件身份、长度、SHA-256 和只读属性；执行时占用全局 Logparse 槽位，以参数数组启动固定 CLI，收集有界 stdout/stderr，并响应取消。执行后检查任务目录、manifest、目标日志和文件树，写操作回执及资源提案。

每个 Job 的预处理结束后，运行时关闭 Session、撤销令牌并读取已固定结果，再调用 Specialist。取消、启动失败、非零退出、输出过量、资产漂移和非法产物分别转换为明确的执行失败；不能把部分目录当成功产物继续使用。

### 3.3 逐文件设计

| 文件 | 主要接口 | 详细职责 / 输入输出 |
| --- | --- | --- |
| [integrations/logparse/__init__.py](../../src/problem_locator/integrations/logparse/__init__.py) | broker builder、factory/session、请求模型导出 | 仅公开集成入口与受限请求模型；上游日志解析和目标选择算法由固定的 Logparse CLI 拥有。 |
| [integrations/logparse/fingerprint.py](../../src/problem_locator/integrations/logparse/fingerprint.py) | `fingerprint_logparse_asset`、`resolve_logparse_configuration` | 验证仓库、配置和解释器路径，读取文件清单及哈希、Python 版本并生成 `ResolvedAsset`。检查链接和路径安全，支持仓库文件枚举；资产变动可被后续版本检查发现。 |
| [integrations/logparse/broker.py](../../src/problem_locator/integrations/logparse/broker.py) | `PinnedLogparseBrokerFactory`、`PinnedLogparseBrokerSession`、`build_logparse_runtime` | 工厂共享并发限制并创建 Job Session；Session 验证计划、令牌、请求和工作区，调用 Logparse，校验产物，记录操作。私有 HTTP 只绑定 loopback；`close()` 撤销调用能力并停止相关服务。 |
| [integrations/logparse/requests.py](../../src/problem_locator/integrations/logparse/requests.py) | `Anchor`、`ParseTargetsRequest`、`TargetLogsRequest`、`ParseOnlyRequest`、`ResolvedLogparsePlan`、`BrokerEnvelope` | 严格模型描述私有 broker 协议；校验安全字符串、proposal key、唯一输入源及请求与固定计划一致。这里的嵌套内部对象不是七个公开 MCP 工具的输入 schema。 |
| [integrations/logparse/cli.py](../../src/problem_locator/integrations/logparse/cli.py) | `run`、`main` | 接收 operation/request/result 路径，从环境获取 Job 的 loopback endpoint 与令牌。读取并规范化请求，发送有界 HTTP 请求，检查回执和允许的失败类型后原子写结果。 |
| [integrations/logparse/paths.py](../../src/problem_locator/integrations/logparse/paths.py) | `validate_relative_path`、`resolve_workspace_path`、`validate_proposal_io_paths`、`atomic_write_broker_result` | 将相对路径限制在工作区内，拒绝路径逃逸、链接及不合法的 proposal 布局，控制请求和结果文件的位置及原子发布。 |
| [integrations/logparse/workspace.py](../../src/problem_locator/integrations/logparse/workspace.py) | `load_workspace_manifest`、`bind_attachment`、`bind_logparse_run` | 将清单引用绑定到真实附件或历史解析树，检查普通文件、只读树、字节数与摘要，返回 `BoundAttachment` / `BoundLogparseRun`，防止请求换用未授权输入。 |
| [integrations/logparse/claim.py](../../src/problem_locator/integrations/logparse/claim.py) | `create_parse_claim` | 在服务端控制的目录创建解析声明，把本次输出与 Job 输入关联；拒绝非普通目录并支持故障注入，供 broker 检验解析结果来源。 |
| [integrations/logparse/process.py](../../src/problem_locator/integrations/logparse/process.py) | `SubprocessExecutor.run`、`ProcessResult`、`terminate_process_tree` | 清理子进程环境，用参数数组执行 Logparse，分别有界读取 stdout/stderr，处理启动失败、超量输出和取消；按进程树终止，避免遗留子进程继续写产物。 |
| [integrations/logparse/outputs.py](../../src/problem_locator/integrations/logparse/outputs.py) | `inspect_controlled_run`、`inspect_existing_run`、`generic_parse_result`、`normalize_target_result`、`aggregate_target_results` | 严格解析上游 JSON、拒绝重复 key 与非有限值，检查任务目录、parse manifest、UTF-8 日志和目标路径，输出服务端认可的通用/目标日志结果。 |
| [integrations/logparse/tree.py](../../src/problem_locator/integrations/logparse/tree.py) | `build_tree_manifest` | 遍历受控输出树，逐文件记录大小与 SHA-256，拒绝不支持的文件类型并支持取消检查；树摘要成为资源校验依据。 |

## 4. JSON 与归档边界

| 文件 | 主要接口 | 详细设计 |
| --- | --- | --- |
| [integrations/agent_json.py](../../src/problem_locator/integrations/agent_json.py) | `AgentJsonSurface`、`parse_agent_json_bytes`、`normalize_agent_json_file` | 为 Agent 写入的请求/草稿建立统一 JSON 边界。拒绝 BOM、重复 key、非有限数和非法 UTF-8；表层 schema 校验后转成 canonical JSON 并原子替换。读取时校验普通文件、单硬链接、大小和前后元数据，避免读写竞争。其严格文件协议不同于 `runtime/model_json.py` 对模型最终文本展示外壳的兼容。 |
| [integrations/result_archive.py](../../src/problem_locator/integrations/result_archive.py) | `prepare_result_archive`、`write_result_archive_file`、`build_result_archive`、`validate_result_archive_bytes` | 服务端生成确定性的 Result Archive v3：报告文本、manifest 与原始目标日志绑定，校验引用行、原文和哈希；固定 ZIP 元数据与条目顺序，并支持按 `ArchivePlan` 写磁盘。默认 skill-direct 不走该 ZIP 路径；结构化交付按自己的归档策略使用。 |

## 5. 验证落点

入口和装配由 `tests/deterministic/unit/interfaces/test_cli.py`、`test_settings.py` 及 `tests/deterministic/integration/test_bootstrap_composition.py` 等覆盖；日志由 `test_diagnostics.py` 和 Journey 相关测试覆盖；Logparse 与结果归档位于 `tests/deterministic/unit/integrations/`；平台启动和安装包检查位于 `tests/platform/`。正式测试从 `tools/test-flow/run.ps1` 或 `run.sh` 选择 Goal，不直接拼装发布结论。
