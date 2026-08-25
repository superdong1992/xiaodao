# Test Flow 操作说明

本目录是仓库测试活动的唯一操作入口。测试架构及不变量见
[`design/test-flow-architecture.md`](../../design/test-flow-architecture.md)；本文件只说明如何规划、运行和读取结果，不复制底层 pytest 命令。

## 公开 Goal

| Goal | Track | 含义 |
| --- | --- | --- |
| `dev.default` | `dev` | 框架自测、仓库静态检查、受影响确定性测试和完整确定性测试；不调用真实模型 |
| `dev.real` | `dev` | 在 `dev.default` 闭包之外，显式选择一个真实 Proof/Stage |
| `dev.macos-codex-luna-methods` | `dev` | macOS arm64 上用一次 Codex CLI + gpt-5.6-luna 调用生成、校验并按完整 producer identity 冻结 Methods package |
| `dev.macos-codex-luna-e2e` | `dev` | macOS arm64 上运行一个 fresh `DATA_ROOT` 的 Codex/Luna Streamable HTTP MCP 单场景冒烟；不含 Docker、浏览器或业务 REST |
| `dev.macos-claude-deepseek-methods` | `dev` | 先运行迁移后的 Codex 与 Claude 快测合同，再用 Claude Code 2.1.89 + DeepSeek 生成、校验并原子冻结 Methods cache |
| `dev.macos-claude-deepseek-e2e` | `dev` | 从空 `DATA_ROOT` 运行 CLIENT/ROUTE/LOGPARSE/DIAGNOSE/REVIEW 五进程 Claude MCP 冒烟；不进入 CrossJob |
| `release.full` | `release` | 不可变源码快照上的完整发布证明；包含平台能力和从空数据根开始的 fresh CrossJob |
| `release.codex-luna-methods` | `release` | 同一不可变源码快照上的独立 Codex CLI + gpt-5.6-luna Methods 探索验证；不属于产品 CrossJob 闭包 |

Windows、macOS 和显式 Linux Client 都有仓库内置 adapter。`--client auto` 在当前主机上选择对应 adapter；所有 Client 都通过 HTTP 直连 Linux Server。adapter 不是任意命令扩展点，调用方不能注入外部执行器。host-client 的 Web API 正式浏览器证明要求当前稳定版 Google Chrome，可通过 `TEST_FLOW_CHROME` 指定绝对可执行文件路径；Darwin 上显式 Linux Client 则只使用冻结在 Client image 中的官方 Chrome Headless Shell，不读取宿主浏览器。planning 会先在无网络临时容器中完成零模型 DOM smoke，CrossJob environment 再用正式 source-owned runner 做 loopback DOM roundtrip。

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

## macOS Codex/Luna 快速 E2E

这两条 Dev Goal 有意相互独立。Methods Goal 恰好执行一次真实调用并把校验后的 package 写入
`<cache-root>/codex-luna-methods/<producer-identity>/`；E2E Goal 不会自动 bootstrap，缺少精确缓存时会在 planning 阶段阻断。首个且当前唯一可选场景是 `api-execution-overrun`。

先审阅 Methods 计划；确认后移除 `--plan-only` 才会发生一次真实调用：

```sh
./tools/test-flow/run.sh \
  --track dev \
  --goal dev.macos-codex-luna-methods \
  --client macos \
  --codex-entry /Applications/ChatGPT.app/Contents/Resources/codex \
  --codex-auth /absolute/path/to/.codex/auth.json \
  --cache-root /absolute/path/to/test-flow-cache \
  --allow-codex-posthoc-budget \
  --allow-real-model \
  --reason "生成当前 producer identity 的 Methods cache" \
  --plan-only
```

Methods verdict 为 PASS 且缓存身份匹配后，再审阅 E2E 计划；确认后移除 `--plan-only` 才会执行恰好 5 次真实调用（Client、ROUTE、Logparse、DIAGNOSE、REVIEW），不自动 retry：

```sh
./tools/test-flow/run.sh \
  --track dev \
  --goal dev.macos-codex-luna-e2e \
  --client macos \
  --scenario api-execution-overrun \
  --logparse-source /absolute/path/to/logparse \
  --codex-entry /Applications/ChatGPT.app/Contents/Resources/codex \
  --codex-auth /absolute/path/to/.codex/auth.json \
  --cache-root /absolute/path/to/test-flow-cache \
  --allow-codex-posthoc-budget \
  --allow-real-model \
  --reason "运行已审阅 Methods cache 的本机 MCP 冒烟" \
  --plan-only
```

Methods/E2E 的 hard caps 分别为 1/5 次调用、1,000,000/2,000,000 token、2/3 USD 等价估算和 30 分钟 Stage；单次调用最多 10 分钟，180 秒无语义进展即失败。E2E 只允许 `/mcp` transport 和 `UploadDescriptor` 指定的一次 PUT；Case 操作、终态读取和产物列表都必须经公开 MCP 工具完成。

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

Release 从 GENESIS 和新的空 `DATA_ROOT` 开始，不复用业务 checkpoint。它执行一条 CrossJob：Environment、Route、Upload、Diagnose、自动 Review、Publish/Restart，并同时证明真实 Agent、真实 Logparse、七工具扁平 schema、服务端 DFX、安装分发、重启恢复和证据完整性。Upload Stage 使用真实浏览器运行体跨源重放 REST 创建/查询/附件准备，并以 `Blob` 覆盖长度与哈希失败后完成上传；Diagnose Stage 在终态幂等重放补参，再验证 REST 查询、公开产物列表和逐字节下载。host-client 绑定 Google Chrome；显式 Linux Client 绑定官方 Chrome Headless Shell 的 product、版本、归档和可执行文件 SHA-256。浏览器脚本不会设置 `Content-Length`，超时路径必须封口整个私有进程组并证明无残留。

正式用例的日志归档不是假设外部 Logparse 已预装业务产品配置。容器初始化会从已审阅 Diagnosis Skill 的 `logparse_product`、anchors 和 journey driver 机械生成独立的只读运行时配置，并把每份原始附件无损投影为当前 Logparse loose-diagnostic 输入；初始化阶段先用冻结 Logparse 提交完成一次无模型 smoke parse，逐一证明 module/slot/process anchor 可解析。配置摘要、归档投影版本和归档摘要写入 Release case 与容器收据，服务只使用该独立配置，外部 Logparse Git 快照仍保持未修改。

## Codex Luna Methods 独立 Release

这条工程化验证只支持 Darwin arm64 orchestrator，使用冻结的 Codex CLI、外置 ChatGPT token、`gpt-5.6-luna` 和 `medium` reasoning。每次调用都启动新的 `codex app-server --stdio`、ephemeral thread 和唯一 turn；父进程从冻结的 auth source 读取 access token/account id，只通过 `account/login/start` 写入子进程 stdin，活动 `CODEX_HOME` 不含 `auth.json`，刷新请求一律 fail closed。模型只能使用显式启用的仓库 Skill 和 `shell_command`，单层 named permission profile 将根目录设为 deny、最小系统路径设为 read、生成 workspace 设为 write/诊断 workspace 设为 read，并关闭 command network；provider 网络只属于 app-server 父进程。

它直接生成一个 Methods 包，再让九个隔离诊断调用复用完全相同的包字节；不会启动 Linux Server、MCP、Docker 或 Claude Code，也不会成为 `release.full` 或 CrossJob 的依赖。每个调用的脱敏 JSONL、profile bytes、schema tree、raw/terminal usage 对账、独立 consumer 重审、无凭据扫描和 durable generated package 都进入 Gate 证据。先规划并检查十次调用、5,000,000 token、API 等价 10 USD 的聚合上限和 admission：

```sh
./tools/test-flow/run.sh \
  --track release \
  --goal release.codex-luna-methods \
  --client macos \
  --resume fresh \
  --logparse-source /absolute/path/to/logparse \
  --codex-entry /Applications/ChatGPT.app/Contents/Resources/codex \
  --codex-auth /absolute/path/to/.codex/auth.json \
  --allow-codex-posthoc-budget \
  --plan-only

./tools/test-flow/run.sh \
  --track release \
  --goal release.codex-luna-methods \
  --client macos \
  --resume fresh \
  --logparse-source /absolute/path/to/logparse \
  --codex-entry /Applications/ChatGPT.app/Contents/Resources/codex \
  --codex-auth /absolute/path/to/.codex/auth.json \
  --allow-codex-posthoc-budget
```

`--allow-codex-posthoc-budget` 是显式例外确认：Codex CLI 没有本框架可用的调用前 token/USD 硬停止接口，所以这些聚合上限是每个 terminal receipt 和最终 verdict 的阻断性后验校验，不是预防性花费控制。产品 Linux→Linux `release.full` 与本 goal 产生两个独立权威 verdict；要同时宣称两条测试流通过，二者必须绑定相同 source snapshot digest。

## 预算、超时与性能

真实 Gate 的计划列出模型、turn/token/USD/time 上限和预计成本。cap 可由 Stage 显式选择；未选择时使用该类 Gate 的默认 cap，因此某个长耗时工作流可以获得独立上限而不放大其他真实 Gate。turn、USD 与进程时限由执行器或 provider 强制；token 上限还会由终端 receipt 复核，usage 缺失或超限不能 PASS。模型 usage 使用版本化的 cache-inclusive 合同，逐项记录 `input_tokens`、`output_tokens`、`cache_creation_input_tokens` 与 `cache_read_input_tokens`，并强制 `total_tokens = input_tokens + output_tokens + cache_creation_input_tokens + cache_read_input_tokens`。四个分项任一缺失、总数不一致或总数超过 `max_total_tokens` 时，`usage_complete` 不得为真且 Gate 不能 PASS；Gate、Stage 与最终 verdict 的累计值沿用同一公式。plan 中的 token estimate 是非阻断 planning 数，不能替代硬上限；Methods Skill generation 当前按已执行样本估算 600,000 cache-inclusive tokens，而 16 turns、1,000,000 total tokens、$10 与 1800 秒仍是实际阻断 cap。Skill-generation 另外声明 `max_output_tokens=64000`：它是每次模型请求的输出上限，不是整次 Agent 调用的累计输出量。该上限由身份绑定的 wrapper 参数、只注入 Claude 子进程的 `CLAUDE_CODE_MAX_OUTPUT_TOKENS`、固定 Claude CLI 上限校验及密封 runtime 实现共同证明；终态 `modelUsage.maxOutputTokens` 在固定 Claude Code 版本中只是静态模型档位默认值，不是实际请求 `max_tokens` 的回显，不进入 cap 证明。结构完整的失败终态也会先持久化实际 terminal usage，再保持原 Gate 失败；缺少合法终态时不得把零调用误报为完整 usage。

只有直接向编排器输出 allowlist 语义事件的 adapter 才启用无进展计时。pytest 包装的真实 Agent Gate
会捕获内部模型流，编排器无法把该流当成可信的实时进度，因此明确禁用无进展计时；模型 wrapper 的
硬时限必须短于 Backend wall time，Stage 总时限还必须覆盖该 Gate 声明的全部串行调用与证据收尾，
从而保证终止宽限、JUnit、usage 与最终证据能够落盘。硬时限始终生效。性能使用同一版本化策略累计
样本：样本不足是 `NOT_CALIBRATED`；Dev 回归告警；Release 同一性能身份第一次显著变慢为 warning，
连续第二次才失败。复用 Stage 不产生性能样本。

真实 Wiki→Skill Gate 使用独立于源码仓库的临时 workspace。Gate 在模型调用前从未修改的 Wiki
字节生成 closed-schema v2、canonical `runtime/source-wiki-identity.json`；除 Wiki SHA-256 外，v2
还携带 extraction-v1 从 `text` fence 机械提取、保序且保留重复项的完整日志模板清单及其摘要。
轨迹审计会从 Wiki 独立重算并核对 identity schema、canonical bytes、Wiki SHA-256 和模板清单。
Claude 的有效工具仍仅为 `Skill/Read/Write`，权限模式必须是
`dontAsk`；Read 白名单只包含 Wiki、该 source identity 和元 Skill 直接链接的
`references/output-contract.md`，因此仍恰好三次 Read。模型必须读取 identity、逐字复制 digest，并将
模板逐项逐序写入固定 `references/source-log-templates.md`；该文件必须是
`methods.json.shared_references[0]` 且不能作为方法卡。不能心算、猜测、重排或去重；后置 canonical
validator 仍从原始 Wiki 独立重算，Test Flow 不会事后修改生成包。Write 权限覆盖隔离 workspace 的
`output/**`，但 v5 轨迹审计把它收紧为
唯一 `output/<skill>/` Methods 包：根目录只能有 `SKILL.md`、`methods.json` 与非空的
`references/*.md`，每个最终文件必须由一次参数完整的 Write 创建，且全部 Write 构成必读材料之后的连续
工具序列。任何失败调用、额外读取、部分读取、重复路径、越界路径、软硬链接、旧 GenerationSpec 文件、
未被 Write 轨迹覆盖的文件或 Write 内容与落盘字节不一致都会使 Gate 失败。receipt 只记录相对路径、
调用结果、文件大小和摘要，不记录 Write 内容或绝对路径。Gate 随后用元 Skill 的 canonical validator 和
模型不可见的语义 oracle 验证包，再在包外复制产品 registration template，形成 CrossJob 消费的注册目录。
validator 会机械重算固定模板文件的精确字节、命名日志字段的首次出现顺序与每种日志模板的 canonical
stable marker；语义 oracle 只能在该机械合同上检查原因分组与覆盖，不能另设未公开的 marker 拼写或
遗漏 Wiki 命名字段。独立 Codex/Luna generation workspace 会物化同一 v2 identity 与固定引用合同，
但仍保持一次生成加九次诊断的十次调用边界。

所有 pytest 包装的真实 isolated Agent Gate 还使用版本化的
`isolated-agent-env-allowlist-v3` 环境策略。pytest 只继承跨平台启动所需的
`PATH/HOME/SystemRoot/TEMP` 等基础键，并显式加入当前 Test Flow Gate 所需的键；宿主 provider、代理、
云平台和 CI 环境变量不会进入 pytest、AgentBackend 或 Claude wrapper。模型 provider 认证只由已审计的
env-only settings 文件提供；Logparse 会话凭据只能由 AgentBackend 的显式 broker 机制成对加入。
wrapper 会再次拒绝未知入站键；macOS 启动 Node 时注入的 `__CF_USER_TEXT_ENCODING` 只允许出现在入站
审计中，不会转发给 pytest 或 Claude。Skill-generation 的单响应上限只由计划派生，宿主同名环境变量不能覆盖。invocation receipt 会写入策略版本、有效键名列表及其 SHA-256，
不写入任何环境值或 secret。该策略实现位于 `runtime.support` 身份中，源码变化会使既有证明失效。

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
