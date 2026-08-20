# Test Flow 操作说明

本目录是仓库测试活动的唯一操作入口。测试架构及不变量见
[`design/test-flow-architecture.md`](../../design/test-flow-architecture.md)；本文件只说明如何规划、运行和读取结果，不复制底层 pytest 命令。

## 公开 Goal

| Goal | Track | 含义 |
| --- | --- | --- |
| `dev.default` | `dev` | 框架自测、仓库静态检查、受影响确定性测试和完整确定性测试；不调用真实模型 |
| `dev.real` | `dev` | 在 `dev.default` 闭包之外，显式选择一个真实 Proof/Stage |
| `release.full` | `release` | 不可变源码快照上的完整发布证明；包含平台能力和从空数据根开始的 fresh CrossJob |

Windows、macOS 和显式 Linux Client 都有仓库内置 adapter。`--client auto` 在当前主机上选择对应 adapter；所有 Client 都通过 HTTP 直连 Linux Server。adapter 不是任意命令扩展点，调用方不能注入外部执行器。Web API 的正式浏览器证明还要求当前稳定版 Google Chrome；可通过 `TEST_FLOW_CHROME` 指定绝对可执行文件路径，未指定时只检查各平台标准安装位置。

## Dev 确定性测试

先看计划，再运行同一 Goal：

```sh
./tools/test-flow/run.sh --track dev --goal dev.default --plan-only
./tools/test-flow/run.sh --track dev --goal dev.default
```

PowerShell 使用同参数的 `tools/test-flow/run.ps1`。计划会列出 Goal、Proof、Stage、Gate、依赖、身份、复用决定、性能身份、预计资源和 admission blocker。

Windows pytest 默认在仓库 `.tmp/p` 与系统临时目录中选择物理路径较短的一侧。若深层 worktree
仍可能触发 `MAX_PATH`，可在规划和实际运行两次命令中把
`TEST_FLOW_WINDOWS_SCRATCH_ROOT` 设为同一个已存在或可创建的绝对可写短目录；编排器只在其下
创建独占 `p-*` 子目录，清理仍禁止删除边界本身或越界路径。

## Dev 真实测试

`dev.real` 必须选择一个 Stage，并显式允许真实模型。第一次运行同样先看计划：

```sh
./tools/test-flow/run.sh \
  --track dev \
  --goal dev.real \
  --stage real.route \
  --allow-real-model \
  --logparse-source /absolute/path/to/logparse \
  --mcp-source /absolute/path/to/problem-locator-mcp \
  --claude-entry /absolute/path/to/claude-code/package/cli.js \
  --claude-settings /absolute/path/to/claude/settings.json \
  --plan-only
```

移除 `--plan-only` 才执行。相同失败身份再次运行时必须新增三个结构化字段：

```sh
--reason "为什么值得再次运行" \
--hypothesis "这次修复或环境变化应解决什么" \
--expected-evidence "哪些新证据可以证实或证伪该假设"
```

这三个字段是可审计的重试合同，不允许盲重试。

## Release

Release planning 会冻结当前 Git 可见工作树的 exact source snapshot；工作树可以尚未提交。tracked 文件使用当前字节，未忽略的 untracked 文件也会进入清单，ignored 文件不会进入。运行期间任何源码漂移都会使 verdict 成为 `ERROR`。先准备由
[`config/runtime-profiles.v2.json`](config/runtime-profiles.v2.json) 冻结的 Claude、uv 和 Linux base image 缓存；准备阶段可以联网，正式 Release 使用已封存缓存且不拉取镜像：

```sh
node tools/test-flow/prepare-release-cache.mjs \
  --repo-root /absolute/path/to/problem-locator \
  --cache-root /absolute/path/to/test-flow-cache \
  --docker-context colima
```

然后把 `PROFILE_VERSION` 替换为该配置中的 `claude.version`，用完全相同的输入先规划、再执行：

```sh
./tools/test-flow/run.sh \
  --track release \
  --goal release.full \
  --client macos \
  --resume fresh \
  --logparse-source /absolute/path/to/logparse \
  --mcp-source /absolute/path/to/problem-locator-mcp \
  --claude-entry /absolute/path/to/test-flow-cache/claude/PROFILE_VERSION/package/cli.js \
  --claude-settings /absolute/path/to/claude/settings.json \
  --docker-context colima \
  --cache-root /absolute/path/to/test-flow-cache \
  --plan-only

./tools/test-flow/run.sh \
  --track release \
  --goal release.full \
  --client macos \
  --resume fresh \
  --logparse-source /absolute/path/to/logparse \
  --mcp-source /absolute/path/to/problem-locator-mcp \
  --claude-entry /absolute/path/to/test-flow-cache/claude/PROFILE_VERSION/package/cli.js \
  --claude-settings /absolute/path/to/claude/settings.json \
  --docker-context colima \
  --cache-root /absolute/path/to/test-flow-cache
```

Windows 使用 `--client windows`，显式 Linux Client 使用 `--client linux`；两者不接受 macOS 的 Docker context。仓库拥有三种 adapter 的相同 Gate receipt 合同，但一次 Release 只证明 verdict 中记录的实际平台、source snapshot digest 和全部输入身份，不能外推成未执行平台的真实 PASS。

`verdict.json` 会同时记录 snapshot digest、base Git SHA、branch 和 planning 时的 dirty 状态。Git 提交不是 Release admission 条件；推荐在全部 Proof 通过后再提交完全相同的快照。提交若改变任何 Git 可见 path 或字节，原 verdict 不再证明新内容，必须重新运行 Release。

Release 从 GENESIS 和新的空 `DATA_ROOT` 开始，不复用业务 checkpoint。它执行一条 CrossJob：Environment、Route、Upload、Diagnose、自动 Review、Publish/Restart，并同时证明真实 Agent、真实 Logparse、七工具扁平 schema、服务端 DFX、安装分发、重启恢复和证据完整性。Upload Stage 使用真实 Chrome 跨源重放 REST 创建/查询/附件准备，并以 `Blob` 覆盖长度与哈希失败后完成上传；Diagnose Stage 在终态幂等重放补参，再验证 REST 查询、公开产物列表和逐字节下载。Chrome 版本和可执行文件 SHA-256 属于对应 Stage 的 producer identity，浏览器脚本不会设置 `Content-Length`。

正式用例的日志归档不是假设外部 Logparse 已预装业务产品配置。容器初始化会从已审阅 Diagnosis Skill 的 `logparse_product`、anchors 和 journey driver 机械生成独立的只读运行时配置，并把每份原始附件无损投影为当前 Logparse loose-diagnostic 输入；初始化阶段先用冻结 Logparse 提交完成一次无模型 smoke parse，逐一证明 module/slot/process anchor 可解析。配置摘要、归档投影版本和归档摘要写入 Release case 与容器收据，服务只使用该独立配置，外部 Logparse Git 快照仍保持未修改。

## 预算、超时与性能

真实 Gate 的计划列出模型、turn/token/USD/time 上限和预计成本。cap 可由 Stage 显式选择；未选择时使用该类 Gate 的默认 cap，因此某个长耗时工作流可以获得独立上限而不放大其他真实 Gate。turn、USD 与进程时限由执行器或 provider 强制；token 上限还会由终端 receipt 复核，usage 缺失或超限不能 PASS。模型 usage 使用版本化的 cache-inclusive 合同，逐项记录 `input_tokens`、`output_tokens`、`cache_creation_input_tokens` 与 `cache_read_input_tokens`，并强制 `total_tokens = input_tokens + output_tokens + cache_creation_input_tokens + cache_read_input_tokens`。四个分项任一缺失、总数不一致或总数超过 `max_total_tokens` 时，`usage_complete` 不得为真且 Gate 不能 PASS；Gate、Stage 与最终 verdict 的累计值沿用同一公式。Skill-generation 另外声明 `max_output_tokens=64000`：它是每次模型请求的输出上限，不是整次 Agent 调用的累计输出量。该上限由身份绑定的 wrapper 参数、只注入 Claude 子进程的 `CLAUDE_CODE_MAX_OUTPUT_TOKENS`、固定 Claude CLI 上限校验及密封 runtime 实现共同证明；终态 `modelUsage.maxOutputTokens` 在固定 Claude Code 版本中只是静态模型档位默认值，不是实际请求 `max_tokens` 的回显，不进入 cap 证明。结构完整的失败终态也会先持久化实际 terminal usage，再保持原 Gate 失败；缺少合法终态时不得把零调用误报为完整 usage，真实 isolated invocation 的完整终态也不得以全零 token usage 形成 PASS。

只有直接向编排器输出 allowlist 语义事件的 adapter 才启用无进展计时。pytest 包装的真实 Agent Gate
会捕获内部模型流，编排器无法把该流当成可信的实时进度，因此明确禁用无进展计时；模型 wrapper 的
硬时限必须短于 Backend wall time，Stage 总时限还必须覆盖该 Gate 声明的全部串行调用与证据收尾，
从而保证终止宽限、JUnit、usage 与最终证据能够落盘。硬时限始终生效。性能使用同一版本化策略累计
样本：样本不足是 `NOT_CALIBRATED`；Dev 回归告警；Release 同一性能身份第一次显著变慢为 warning，
连续第二次才失败。复用 Stage 不产生性能样本。

真实 Wiki→Skill Gate 使用独立于源码仓库的临时 workspace。Claude 的有效工具仅为
`Skill/Read/StructuredOutput`，权限模式必须是 `dontAsk`；Read 白名单只包含 Wiki、澄清、两份格式合同和
四份只读控制 checkpoint。Skill 返回后，Wiki 与澄清必须在同一 assistant response 中按固定顺序读取，
两份结果都返回后才可继续；其余六次 Read 严格串行。完整轨迹固定为一次 Skill、八次 Read 和一次
StructuredOutput。任何额外、重复、乱序、越界、失败或部分读取都会使 Gate 失败。

StructuredOutput 的冻结 schema 机械约束不超过 48 KiB 的 `GenerationBlueprint` v1：四个根字段、19 个
必需 `spec` 字段加唯一可选 `logparse_product`、2/10 个 literal policy/extractor、7/9/5 个 literal rule
segment、2/4 个 literal path segment，以及一个版本化 ordered-interval family。IR 不得显式携带其 144 条
展开 rule 或三条展开 path；可信 compiler 按固定顺序形成最终 2/10/165/9 GenerationSpec，随后仍由仓库
loader、validator 与场景 oracle 深验。模型只能提交一次根 plain object；成功 tool result 后唯一终态响应必须精确为
ASCII `DONE`，不得再调用工具。wrapper 对 tool input 与 terminal IR 做闭合校验，封存 IR/compiler/output
三方 identity 与 SHA-256，再递归排序最终 GenerationSpec key、canonical JSON 编码并以 create-only 原子方式产生唯一文件。完整 PASS receipt
不保存提交正文或模型实际 terminal 正文；attempt policy 只把 exact `DONE` 记录为公开的冻结控制常量，
terminal receipt 本身仅保存 `subtype/is_error`。失败时只保存 ordinal、工具名、受控相对路径、outcome、
size/SHA-256 和安全 terminal 摘要，绝不保存 thinking、业务内容、绝对路径或 secret。公共 PASS 只接受当前版本的完整审计
合同，旧 Write 或旧 trace receipt 不得降级复用。

若 Skill-generation 在没有 terminal result 时被 watchdog 终止，wrapper 只在全部 stream 行都通过
fatal UTF-8 与 JSON 解析、存在唯一 init 且结果数为零时尝试生成独立的 FAIL-only timeout prefix receipt；
init 不在首事件会被固定分类为 `INIT_INVALID` rejection，而不会在候选筛选时静默丢失。
该 receipt 仅记录符合冻结计划的已完成或末尾 pending 工具 ordinal/name/outcome；Read 只记录受控相对
路径，StructuredOutput 只记录 canonical size/SHA-256，初始双 Read batch 和后续串行 barrier 仍须精确
成立。安全可识别的第二次 StructuredOutput 会被标记为 prefix sequence violation；若严格 prefix 审计因
越界路径、unsafe input、乱序或畸形结果拒绝，wrapper 只保留固定 outer code 与固定枚举 `audit_code`，
不复制异常 message/details、路径、input、raw 或工具正文。不可完整解析的流、非审计异常或非枚举错误码
仍退化为 `tool_trace_audit=null`；若流已全量解析且 event_count>0、init=1、result=0，这种 null 会被判为
HARNESS evidence invalid，不能再次静默通过证据收集。collector、engine 与最终 evidence 独立把两种
FAIL-only receipt 绑定到 terminal/usage 为空的 timeout/stream-invalid invocation；它们永远不能成为 PASS，
也不得触发产物物化。timeout 还必须绑定 `exit_code=null`、安全大写 signal 与 wrapper exit 124；
stream-invalid 必须绑定合法 child exit 或安全 signal 与 wrapper exit 1。
`last_event_type` 仅允许冻结的 Claude CLI 事件枚举；未知模型控制值会先归一为 null，绝不复制原词法，
且非空 stream 的 null 类型会被 actions/evidence 拒绝。terminal-missing prefix 禁止以 `result` 或 `error`
结尾；显式 `error` 事件只能形成固定 `STREAM_ERROR` rejection。

对 outcome=`ERROR` 的 StructuredOutput，审计器不复制 provider 错误，而是用同一冻结 IR schema
在本地重算 content-free diagnostic，只区分固定 `INVALID_IR` 与 `SCHEMA_VALID_TOOL_REJECTED` 状态。
实际字段值、动态额外属性名、raw error、content、snippet、message 和业务路径一律不得进入。ERROR 记录
必须携带该 exact-key diagnostic，SUCCESS/PENDING 记录必须不含；collector 与最终 evidence 会再次验证，
任何扩展字段或 stripped diagnostic 都使证据失效。

所有 pytest 包装的真实 isolated Agent Gate 还使用版本化的
`isolated-agent-env-allowlist-v3` 环境策略。pytest 只继承跨平台启动所需的
`PATH/HOME/SystemRoot/TEMP` 等基础键，并显式加入当前 Test Flow Gate 所需的键；宿主 provider、代理、
云平台和 CI 环境变量不会进入 pytest、AgentBackend 或 Claude wrapper。模型 provider 认证只由已审计的
env-only settings 文件提供；Logparse 会话凭据只能由 AgentBackend 的显式 broker 机制成对加入。
wrapper 会再次拒绝未知入站键；Skill-generation 的单响应上限只由计划派生，宿主同名环境变量不能覆盖。
冻结的 Claude Code 2.1.89 StructuredOutput 重试边界只对子进程注入
`MAX_STRUCTURED_OUTPUT_RETRIES=2`：首次合法提交后允许终态 `DONE`，第二次提交即有界失败，不能通过提高
turn、token、时间或费用上限绕过。invocation receipt 会写入策略版本、有效键名列表、StructuredOutput
重试边界及其 SHA-256，不写入任何环境值或 secret。可信 terminal usage 一旦超过冻结 turn/token/USD cap，
wrapper 必须优先分类为 cap exceeded；非成功 terminal 不能遮蔽已证实的 cap 越界。
该策略实现位于 `runtime.support` 身份中，源码变化会使既有证明失效。

## Verdict 与退出码

每次执行的证据位于 `.tmp/test-flow-evidence/<run-id>`。`verdict.json` 最后原子创建，是唯一权威结论；缺少它就是 `UNFINALIZED`。verdict 包含 Proof、Stage、Gate、配置/策略/身份摘要、DFX/资源/密钥扫描审计以及最终决策输入摘要。验证器会从密封 receipt 重新计算结论，不信任可单独编辑的摘要字段。

- `0`：`PASS` 或 `PASS_WITH_WARNINGS`
- `1`：功能失败或持续的 Release 性能回归
- `2`：`BLOCKED` 或 `INCONCLUSIVE`
- `3`：框架、证据、安全、finalization 或清理错误

清理失败会使本次运行成为 `ERROR` 且证据不可复用，即使业务 Gate 已经完成。

## 证据保留

证据从不自动删除。先报告或 dry-run，再精确删除某个 run：

```sh
node tools/test-flow/evidence.mjs report
node tools/test-flow/evidence.mjs report --run-id run-YYYYMMDDTHHMMSSZ-1234abcd
node tools/test-flow/evidence.mjs prune --dry-run --keep-last 10
node tools/test-flow/evidence.mjs prune --run-id run-YYYYMMDDTHHMMSSZ-1234abcd --execute
```

`report` 只会把按当前验证器重审为 PASS 的证据标成可复用；旧合同或篡改证据统一标为 `MANUAL_REVIEW`。被其他有效 verdict 引用的 executed source receipt 默认拒绝删除。复用只能直接引用已重新验证的原始 executed receipt，不能形成复用链。

## 配置权威

当前 runner 只读取六份 schema v2 配置：

- [`proofs.v2.json`](config/proofs.v2.json)：Goal 与 Proof 闭包；
- [`stages.v2.json`](config/stages.v2.json)：DAG、复用、checkpoint 和平台适用性；
- [`gates.v2.json`](config/gates.v2.json)：allowlisted 原子验证器及 evidence contract；
- [`identities.v2.json`](config/identities.v2.json)：正交身份组件和集合；
- [`policy.v2.json`](config/policy.v2.json)：admission、重试、状态、性能和证据限制；
- [`runtime-profiles.v2.json`](config/runtime-profiles.v2.json)：官方运行时、模型、镜像、外部源码和环境 allowlist 冻结值。

未知字段、悬空引用、不可达 Stage、任意命令、越出仓库的 selector 或身份覆盖缺口都会在执行前 fail closed。
