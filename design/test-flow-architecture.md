# Test Flow 终态架构

状态：当前权威设计  
更新时间：2026-09-02

本文定义 Test Flow 的最终结构与不变量。操作命令只在
[`tools/test-flow/README.md`](../tools/test-flow/README.md) 维护。

## 1. 目标与边界

Test Flow 必须回答“这个 Goal 的全部 claim 是否由当前输入身份下可验证的原子证据满足”，而不是回答“某条命令是否返回 0”。唯一合法聚合链路是：

```text
Goal → Proof → Stage → Gate → Gate receipt
                         ↓
                 Stage / Proof outcome
                         ↓
           evidence finalization → verdict.json
```

- Goal 表达用户意图；Proof 表达必须证明的 claim。
- Stage 是 DAG 调度、复用、checkpoint、平台适用性和性能观测单元。
- Gate 是 allowlist 中的原子验证器；Gate 自身不能形成发布结论。
- receipt 记录执行事实、数量、跳过情况、身份、资源、DFX 与 usage。
- finalization 是运行生命周期后置条件，不是可跳过或复用的 Stage。
- `verdict.json` 是唯一最终结论，且必须最后原子创建。

## 2. 六份配置的单一职责

runner 只接受 schema v2 的六份配置：

| 配置 | 唯一职责 |
| --- | --- |
| `proofs.v2.json` | 公开 Goal、Proof 与 Proof→Stage 闭包 |
| `stages.v2.json` | Stage DAG、Gate 引用、复用、checkpoint、平台和时限 |
| `gates.v2.json` | allowlisted executor、selector、最小通过数、skip 与 evidence contract |
| `identities.v2.json` | 正交 identity component/set |
| `policy.v2.json` | track admission、重试、状态/退出码、性能与证据限制 |
| `runtime-profiles.v2.json` | Claude、模型、uv、Python、镜像、外部源码、环境 allowlist 与硬预算 |

cross-validator 对未知字段、悬空引用、DAG 环、孤儿、不可达 closure、任意命令、越界 selector、平台缺口和身份覆盖缺口 fail closed。每个配置字段必须改变 plan、identity 或 execution，否则不能进入 schema。

## 3. Goal 与发布证明闭包

公开 Goal 由配置冻结。Evidence V2 当前入口如下：

- `dev.default`：framework/config、仓库静态检查、affected、Evidence V2 Core 子收据与 full deterministic；不使用真实模型。
- `dev.real`：完整 Dev 确定性闭包，加一个显式选择的真实 Proof/Stage。
- `dev.macos-codex-luna-methods`：Darwin arm64 上恰好一次 Codex CLI + `gpt-5.6-luna`/medium 调用，独立生成并校验 Methods package，再按完整 producer identity 写入不可变缓存；不执行诊断旅程。
- `dev.macos-codex-luna-e2e` 与 `dev.macos-claude-deepseek-e2e`：默认运行 P2/P1 Specialist-only model cert，正常一次调用，最多一次 repair。
- 名称含 `blind-review-e2e` 的两个 Dev Goal：显式运行可选 Reviewer，保留双角色 2–4 次调用合同。
- `release.full`：从空数据根运行默认关闭 Reviewer 的五阶段 CrossJob，并验证终态重启。
- `release.evidence-v2-certification`：聚合同一快照下默认 Specialist-only 的 Core、P1 和 P2。
- `release.evidence-v2-blind-review-certification`：仅在显式选择时聚合双角色 P1/P2，不是默认 Release 依赖。

`det.evidence-v2-core` 是 `deterministic.full` 内的零模型 Gate。它执行固定生产链用例并生成
`core-verdict.json`，绑定 source snapshot digest、V8 contract manifest digest、用例清单 digest、
pytest summary 和 JUnit。该文件只是 Gate 子收据，最终结论仍由外层 `verdict.json` 给出。

Release 的真实 Agent、ROUTE、默认 Specialist-only DIAGNOSE 与 Logparse claim 由同一 fresh CrossJob 给出，不重复运行隔离真实 Gate。通用 `real.review` 仍是独立合同，不代表 Evidence V2 默认会创建 REVIEW Job。编译、锁文件和 Git whitespace 是正式 cheap Gate，而不是文档外的人工附加步骤。

Provider model-cert Goal 使用同一套 built-in adapter，并由 Gate 的 `evaluation_mode` 冻结单评或盲评。Methods cache key 显式绑定 Wiki、元 Skill tree、输出合同、validator、registration template、模型运行时、prompt/runner 合同；`--scenario` 只能选择仓库固定场景，不能接受路径或命令。

Release case 的 Logparse 产品适配属于测试输入闭包，而不是外部仓库的预置状态。容器初始化从已审阅 Skill/driver 的产品、anchor 与事实绑定生成独立配置，并将原始附件逐行保真投影为冻结 Logparse 当前插件的 loose-diagnostic 格式；在任何模型阶段之前，冻结 Logparse 必须完成 smoke parse 且解析出每个预期 module/slot/process。配置与归档投影都由收据摘要绑定，运行时不修改外部 Logparse checkout。

pytest Gate 必须解析 JUnit：执行数为零、全部 skipped、低于 `min_passed` 或违反 skip policy 都不能 PASS。退出码只能作为一个输入，不能替代结构化计数。

## 4. CrossJob 分段与 checkpoint

CrossJob 有五个逻辑 Stage：

1. Environment
2. Route
3. Upload（真实浏览器运行体跨源重放 REST 创建/查询/准备，并用 `Blob` 验证上传）
4. Diagnose（默认只运行 Specialist，真实浏览器运行体重放补参并验证查询、列表和下载）
5. Publish/Restart

共有四个 checkpoint boundary：Route→Upload、Upload→Diagnose、Diagnose→Publish/Restart、Publish/Restart→end。Environment 不产生 checkpoint；Diagnose 必须证明只出现 DIAGNOSE 模型调用、没有 REVIEW Job，才封存下一边界。

Dev 可按 identity 使用普通 receipt 或 checkpoint-chain。恢复前必须重新验证 source verdict、payload seal、当前扫描器、事件合同和 checkpoint 分类 receipt，并解包到新的空根。复用只能直接引用原始 `EXECUTED` receipt；禁止从 `REUSED` stub 再复用。

Release 对真实旅程一律 `reuse=never`，忽略业务 checkpoint，从 GENESIS 和新的空 `DATA_ROOT` 开始。Dev checkpoint 只能加速诊断，不能替代发布证明。

## 5. 平台模型

Linux 是唯一 Server 平台。Windows 与 macOS 默认使用本机 Client，Linux Client 仅在显式选择时启用；三者都通过 HTTP 直连 Linux Server，且都有仓库拥有的 built-in adapter。三个薄 wrapper 共享相同 Gate receipt、checkpoint、DFX、预算和 evidence 合同，不接受调用方提供任意 adapter 命令。host-client 的 Web API 运行体固定为 Google Chrome；Darwin 调度的显式 Linux Client 固定为 Client image 内的官方 Chrome Headless Shell。planning 先用冻结 image 做无网络 DOM smoke，environment 再从非 root Linux Client 容器以 loopback runner 复核 HOME、执行体与进程树边界，Upload/Diagnose 才允许访问 Linux Uvicorn。

平台“受支持”与“已在某次发布真实通过”是两件事。一个 verdict 只证明其中记录的 Client 平台、Server、不可变源码快照、运行时 profile 和外部输入；不得把 macOS 的真实 PASS 外推到 Windows 或 Linux Client。每个平台的真实认证必须由该平台自己的 `release.full` verdict 给出。

## 6. 身份模型

Release planning 会枚举 Git 可见工作树：使用 tracked 文件的当前工作树字节，并纳入所有未被 ignore 的 untracked 文件；忽略文件和 `.git` 元数据不属于发布源码。排序后的 path、类型、可执行位、大小和内容 SHA-256 形成 source snapshot manifest 与唯一 digest。base commit、branch 和当时是否 dirty 只作为溯源元数据，不参与“是否允许执行”的判断。Windows Docker bind mount 会把文件 mode 合成为 `0777`；CrossJob 初始化只在私有副本中先按清单核对路径、类型、字节和链接目标，再施加清单声明的可执行位并执行完整 digest 复核，不能把 bind mount 的合成 mode 当作源码事实。

执行前把该快照物化到新的 attempt scratch；正式 Host/Linux/CrossJob adapter 只读取物化副本，直接读取工作树的 Gate 在执行前后复验同一清单。Linux 容器复制源码后再次按密封 manifest 验证完整 path set、模式和内容。planning 到 verdict 期间任何 Git 可见源码漂移都使本次运行 ERROR；因此不可变性由内容合同保证，不依赖提前提交。

身份分三层：

- producer identity：产品源码、schema/生成资产、Client、Server、Skill、Logparse/MCP 外部源码、Claude/settings、模型和 runtime profile；
- proof identity：producer identity 加 canonical Stage/Gate 定义、依赖 proof identity、runner、扫描器、事件/status policy；
- performance identity：Stage、producer identity、metric contract 与 performance policy version。

产品输入和测试实现分开建模：测试/文档变化会使 framework proof 失效，但不应无意义地改变业务 producer identity。真实 Agent 的 Claude entry、settings allowlist、Skill、Logparse 配置/Python和外部提交都必须入身份。任何定义或依赖变化都必须使预期 closure 精确失效。

Wiki→Methods 生成不要求受限模型执行不可审计的密码学心算，也不依赖模型自行记住散落的完整日志
模板。Gate 在 invocation 前从冻结 Wiki 原始字节生成 closed-schema v2 canonical source-identity
sidecar；它同时绑定 Wiki SHA-256 与 extraction-v1 机械模板清单。v5 tool-trace policy 仍只允许模型
恰好读取 Wiki、identity 和元 Skill output contract 三个来源，并要求清单逐项逐序写入固定
`references/source-log-templates.md`，作为 `methods.json.shared_references[0]`。sidecar schema、
canonical bytes、digest-to-Wiki、清单与固定引用字节在启动前和收尾时由独立路径校验；receipt 只保存
模板数量和摘要，不保存正文。模型只能逐字复制 digest/清单，canonical validator 随后仍直接重算
Wiki SHA-256 与模板文件。不得开放 Bash、从 semantic oracle 或 registration 泄漏业务答案、放宽
校验，或在模型退出后回填/补写生成包。Codex/Luna 的第一次 generation call 物化同一 v2 identity；
后续九次 diagnosis 不接触该生成期 sidecar。

## 7. 真实模型预算

runtime profile 为每个真实 Gate 声明 model、turn、token、USD 与 time 上限；Stage 可显式选择一个版本化 cap，未选择时回落到该类 Gate 的默认 cap。plan 同时显示非阻断 estimate 和 hard caps，estimate 不得超过同 Stage 声明 invocation 的聚合 hard token cap，也不能替代后者。Methods Skill generation 以已执行样本估算 600,000 cache-inclusive tokens；16 turns、1,000,000 total tokens、$10 与 1800 秒才是阻断上限。turn、USD 与时间由执行器/provider 强制；token 上限由终端 usage receipt 再次校验。版本化 usage 合同分别保存普通输入、输出、cache creation 与 cache read token，并以四项之和作为 `total_tokens`；Gate、Stage、verdict 聚合和 `max_total_tokens` 判定都使用这一个 cache-inclusive 公式。可选的 `max_output_tokens` 表示单次模型请求的输出上限，不是累计 Agent usage。它由身份绑定的 wrapper 参数、只注入 Claude 子进程的环境值、固定 CLI 上限校验及密封 runtime 实现共同证明；Claude Code 终态 `modelUsage.maxOutputTokens` 是静态模型档位默认值而非请求 `max_tokens` 回显，不能作为 cap 证明。命令没有生效上限、receipt 缺 model/cap/任一 terminal usage 分项、总数不一致，或实际 usage 超限，都不能 PASS。若模型返回结构完整的失败 terminal，执行器先密封实际 usage 与失败码，再保持调用和 Gate 失败；没有合法 terminal 的调用只能标为 usage 不完整，不能伪造零消耗证明。

同一失败身份不允许盲重试。下一次运行必须记录新的 `reason`、`hypothesis` 和 `expected_evidence`；这三个字段进入 plan 和证据。

## 8. Performance 单源裁决

`policy.v2.json` 是 performance 唯一规则源。metric 从第一天使用同一结构，生命周期由 `observe|warn|gate` 决定；硬时限始终强制。当前 robust baseline 使用最近 10 个同身份、已验证且真实执行的样本，最少 5 个样本，并以版本化 median/MAD 规则判定。

- 样本不足：`NOT_CALIBRATED`，产生 warning 而不伪造基线。
- Dev 显著变慢：warning。
- Release 同一 performance identity 第一次显著变慢：`PASS_WITH_WARNINGS`；连续第二次：FAIL。
- producer identity、metric contract 或 policy version 变化：自动开始新样本序列。
- 复用 Stage：`NOT_MEASURED`，不写 0 秒、不增加样本、不清除旧 slow streak。

status adjudicator 只运行一次。Stage executor 只报告观察，不能自行写 overall；finalizer 完成 evidence/resource/security 审计后调用统一裁决。

## 9. DFX 与瀑布

Problem Locator Client 不记录专用 DFX。Host 等待从 Claude stream/tool-use/result 边界观察；线上参数、schema、验证失败与服务处理以 Linux Server DFX 为权威。

Server Gate evidence contract 要求：

- `mcp.tools.listed` 精确列出七个扁平 input schema；
- 每次调用的 started/completed 具有精确 request/tool 对应；
- negative probe 产生带字段路径和类型事实的 validation event；
- `correlation_id`、`request_id`、`case_id`、`job_id` 按阶段规则贯通；
- 关键阶段报告 duration、重试、超时和传输字节，形成 Host→网络→排队→Route→Upload/Logparse→Diagnose→服务端复验→发布/下载瀑布。

每个服务实例/relay 保留自己的 producer、原始 NDJSON、sequence 和 monotonic clock domain。聚合器不得把多个实例伪装成一个 producer，也不能直接相减不同 monotonic clock；跨 Host/Linux 的区间只使用明确关联边界与记录的 UTC/clock-offset。waterfall 是由原始流和 receipt 摘出的索引，原始流仍是权威证据。

每个 CrossJob Gate 在 `gates.v2.json` 的结构化 evidence contract 中直接声明 event instance、PASS 时必需的 stream、允许为空的 stream 和失败证据空流策略。finalizer 只解释该合同，不按 Gate 或 Stage 名猜测文件；同一 stream 若同时被另一个 PASS Gate 要求非空，非空要求优先。raw→receipt hash、sequence、单 producer monotonic 非递减、大小上限、密钥扫描和必需事件缺一不可。

## 10. Evidence、复用与防篡改

运行先关闭 writer，再写 candidate、Gate/Stage/Proof audit、事件/资源/安全审计和 finalization seal，最后原子创建 verdict。无 verdict 即 `UNFINALIZED`。

验证器执行严格 schema 校验，并从 sealed candidate、Gate receipt、Stage/Proof aggregation、policy/config digest、事件/资源/安全审计重新计算 `decision_input_digest` 和最终状态。单独修改 overall、可复用标记、Stage identity/status 或 source metadata 都必须使 verdict INVALID。

复用 source 必须是经过当前合同重新验证的原始 executed receipt，并记录 source run/stage/receipt digest。source 缺失或篡改会使派生 proof 不满足；引用 source 的证据默认拒绝 prune。若要使归档独立，应使用明确 CAS/复制合同，不能留下隐式 stub 链。

清理失败不是功能 PASS 的可复用例外：它使 operation/overall 为 ERROR，并使证据不可复用。资源、事件或 finalization 失败同样如此。

## 11. 完成与发布定义

框架代码“存在”、deterministic PASS 和某个历史发布都不能证明新的源码快照。终态变更完成需要：

1. 六配置与全部 schema/cross-validation、自测和确定性产品测试通过；
2. 文档只有本架构和操作说明两处当前 Test Flow 权威，且职责不重复；
3. 旧迁移 runner、静态 bundle、重复摘要和一次性实施文档从当前树移除；
4. planning 冻结当前 Git 可见工作树的 exact source snapshot；
5. Evidence V2 的 P1/P2 model-cert adapter 完成并移除显式 blocker 后，对该快照、实际平台和冻结 runtime/external inputs 执行 fresh `release.full`；
6. 最后生成且可重新验证的 verdict 满足全部 Proof；
7. 如需 Git 持久化，在测试完成后提交完全相同的 path/字节；提交前后 snapshot digest 必须一致，提交动作本身不构成新的测试证明。

原分阶段优化在这里收敛为一套架构：cheap deterministic 先行、五个逻辑 Stage/四个 checkpoint boundary、producer/proof/performance identity 分离、语义心跳与双时限、真实模型最小抽样，以及版本化瀑布/性能策略。样本积累或 policy mode 变化是同一框架的运行状态，不再保留第二套 runner 或过渡语义。
