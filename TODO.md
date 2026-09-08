# TODO

更新时间：2026-09-07

本文件是仓库活跃待办的唯一清单。已完成事项由代码、当前设计与 Git 历史证明，不在这里保留关闭项。

2026-09-05 仓库分析的核对基线为 `main@443ca21` / Problem Locator `6.0.0` / State V9。
本次补充记录已由代码路径确认的实现边界及待评估风险，尚未执行性能压测或新的 Test Flow。
后续是否修复、采用何种方案，仍需结合届时的当前版本、复现证据和实际使用需求决定。

## P0：8.0 网站 Agent 真实 Release 与内部网站联调

- Agent REST、自然语言追问、持久 SSE、报告下载和网站后端示例已实现。正式 Dev 验证以当前源码快照的 `verdict.json` 为准。
- `release.full` 已改为一条网站原话 → 建案与权威追问 → 附件上传 → Reviewer → 报告下载 → 重启检查旅程；中间不再重启丢失活动 Case。首条非空原话直接建案，补充字段和提交附件各调用一次 INTAKE，正常/硬上限均为 7 次真实模型调用，不允许 repair。
- 2026-09-07 的官方 `--plan-only` 因当时 Docker Linux daemon 未启动、外部依赖未对齐而未进入真实模型。fresh Release 仍须从全新 V11 数据根执行，不能用 Dev 检查代替。
- 2026-09-08 本地 Docker 已恢复，Windows Client、Linux 镜像、Claude 2.1.89 和 DeepSeek Flash 测试配置已对齐。`dev.real` 的完整 Web CrossJob 前几轮被过期的安装版本/Skill 哈希断言、旧 `state.json` 夹具初始化，以及并行修改中的网站测试拦住；这些轮次均未进入真实网站与模型生成。已修正平台夹具并等待网页先建案改动完成，后续按合并后的源码重新规划并执行完整旅程，结果以对应 verdict 为准。服务端 INTAKE 对 Markdown 围栏的拒绝已在本地复现，仍需结合真实 Web 输出与内网实际模型核对。
- 合并后的完整旅程计划已通过环境准入，但实际执行被自动审批拒绝：需用户明确批准向 `api.deepseek.com` 发送本地测试提示词、Skill 和诊断材料，最多 7 次调用、费用硬上限 22 美元。批准前只运行零真实模型的正式 Dev 检查；Linux 平台修正和完整真实 Web 旅程仍待复验，不以计划或确定性通过代替。
- **本轮验证元数据**：Dev `run-20260908T081857Z-5a9f18d6` 为 `FAIL`；默认基线下 affected 597 passed / 24 skipped，但 66.806 秒超过 60 秒门槛，full 未运行。源码快照 `git-visible-worktree-v1:5921b281a8e2cadb6005c99787a88fdaa301c425195767f9031ce9534f502e41`，源码核验 `PASS`、真实模型调用为 0；继续前需解决该耗时阻塞并取得上述外部模型授权。此行是验证后的状态回填，不属于所引用快照。
- 网站开发者需把示例中的登录与归属回调接入内部网站后端，并部署来源访问限制。未经这些接入和真实 Release，不宣称已在内部网站生产可用。
- **推送前 Dev 复验元数据（2026-09-08）**：`run-20260908T082640Z-197109e5` 为 `PASS_WITH_WARNINGS`，基线为网站引入提交 `8b53bc5`，受影响范围按既定规则移交全量；完整确定性阶段、功能、运行及源码核验均通过，性能基线尚未校准。源码快照 `git-visible-worktree-v1:5167e26963def74008b0e1c760d7e7e6c8c73377393a83d04780ef098aacaf28`，零真实模型调用。此前默认快速阶段超时记录保留，外部模型授权、完整真实 Web 旅程和 Linux 平台复验仍待完成。本行是验证后的元数据回填，不属于所引用快照。

## P0：Methods V1 Reviewer 最长链路 Release

- 当前零模型 Core 和 SameJob 已恢复 Methods V1 报告验证；`release.full` 也已切到
  `SPECIALIZED_REVIEWER_ENABLED=true`，要求实际观察 `REVIEWING`，并由真实浏览器核对
  `diagnosis-result.json`、`result.zip`、Content-Length、SHA-256、原始日志字节和重启重放。
- 旧 Evidence V2 provider model-cert 不代表 Problem Locator 6.0.0 的专有运行时，不能作为 V9 发布
  结论或复用来源。后续若保留这些工具，必须明确标为历史实验；若要成为正式认证入口，需按当前
  Methods V1 草稿、Candidate、可选 Review 和用户报告合同重新实现。
- 当前待办是审阅 `release.full --plan-only` 的模型身份、调用数、预算、外部源码和环境 blocker，
  然后在依赖齐备的受支持主机上执行 fresh Release，取得绑定当前源码快照的权威 `verdict.json`。

## P0：Generic V2 最终集成与生产验收

- C 变更集提供 V1 兼容、完整 Markdown V2、服务端 `GENERIC_REPORT` 产物和局域网适配 Skill；最终发布前仍须与其他并行变更合一，并由主控对合并后的同一源码快照执行 fresh `release.full`。不得复用 C 的 Dev verdict 冒充 Release。
- 局域网管理员须在私有通用定位 Skill 内应用最小 framework-mode 适配，并在同一 Linux 服务账号、Agent、settings、模型和工具身份下运行本地 A/B 验收。收据只保留 Skill tree 摘要与显式版本、输入/结果的 size/hash/状态、两次相同的运行身份 manifest 摘要和本地人工语义 verdict，不保存或上传私有 Skill、报告正文、prompt、路径或执行输出；不得把两个随机模型调用的报告 hash 相等作为默认门槛。
- 只有合并后的 Release verdict 与局域网生产验收都完成后，才在 `FIXED_ISSUES.md` 登记本问题的最终修复记录与权威 verdict；本并行任务不写“已修复”或占位 verdict。

## P0：Diagnosis Skill 条件性可选参数

- 现状核对：`runtime/diagnosis_runtime.py::_methods_user_input_projection` 按角色是否必需、是否已提供
  角色事实激活输入，包内其余声明输入仍按必需项处理；尚未形成按诊断分支激活参数的通用合同。
- Diagnosis Skill 必须支持条件性可选参数。参数未命中其声明的诊断分支时，不得成为 OPEN requirement，也不得阻塞路由、诊断、Review 或结果交付；只有进入指定分支且该分支确实依赖该参数时，Runtime 才向用户索要。
- 分支激活条件必须由 Skill 显式声明、可机读，并写入审计与 replay 输入；不得由 Agent 临时发明分支、用空字符串或隐藏默认值冒充未提供参数，也不得依赖客户端 Hook 修正语义。
- 条件参数若已作为初始 USER_FACT 提供，应直接固定并复用，不得重复询问；若未提供，分支激活后才创建一次可补充的 OPEN requirement。
- 生成器、manifest/合同、Catalog、Coordinator、服务端验证器和正反向测试必须共同覆盖“命中分支才询问、未命中分支不询问且不阻塞”。

## P1：下一轮性能优化与目标机复测

- Specialist marker 索引已实现，但正式 Dev 验证未完成：`run-20260907T143410Z-1b5e8749` 的 affected 535 passed / 24 skipped，60.637 秒超过既有 60 秒门槛，full 未运行；此前同快照为 61.710 秒。继续定位测试运行开销时保留两次 FAIL，不扩大测试选择或放宽门槛来获取 PASS。详细源码身份与 verdict 见 `FIXED_ISSUES.md` 的 PL-FIX-053 本轮元数据。本行是验证后的状态回填，不属于所引用快照。
- 2026-09-07 用户确认当前慢请求为固定内部模型的单轮 Specialist，无 Reviewer：BACKEND_EXECUTE 677 秒、API 656.1 秒、input/output 43,413/20,281 tokens，thinking 65.4 KB、最终 JSON/text 1.4 KB。优先在该环境对比有无完整 marker 索引的 thinking、API 时长和诊断质量；输出协议压缩不是此例首选。原始日志、部署身份及真实 A/B 尚未取得，不能从本地确定性测试外推分钟级降幅。
- 2026-09-07 已按 `8b53bc5` / 8.0.0 核对模型、Reviewer、网站会话、附件和 Logparse 路径；当前复现证据与目标机测量口径见 [`docs/performance-v11.md`](docs/performance-v11.md)。待评估项包括 Reviewer 重复上下文与文件工具回合、网站单 INTAKE worker、SSE/空闲扫描重复加载全历史，以及会话附件导入的重复 hash/复制。网站测量须从收到消息开始，Case brief 不覆盖首次 INTAKE。
- 第一轮保留自动 ROUTE 和 Agent CLI。直接模型 API、常驻 CLI 进程池、Logparse 多 target 批处理分别评估，避免一次改变模型协议、进程生命周期和日志解析合同。
- 7.0 原生 WSL 单样本已将 ROUTE/Specialist 降为各 1 turn、零文件工具，但 Specialist 约 48 秒，较前次约 18 秒更慢。下一轮对同输入重复采样，区分提示词、推理输出长度、缓存和服务负载；评估模型推理设置时同时检查证据与诊断质量，不仅追求总耗时下降。详见 `docs/performance-v10.md`。
- 在目标 4 核、8 GB Linux Server 上采集并发 1/2/3 Case 的吞吐、队列 P50/P95/P99、进程树峰值 RSS、磁盘读写量和上传吞吐。默认 ROUTE 1、DIAGNOSE 2、Logparse 1、ZIP 1 只是起点，需要真实负载校准。
- 继续拆分客户端“等待材料”的 108 秒：分别核对 ROUTE 返回、准备附件、客户端 hash、上传接收、发布和 submit。当前没有目标机详细事件，不能把两段约 54 秒归因于某个服务端步骤，也不能宣称已消除。
- 超过 128 KiB 的 Specialist 输入保留受控完整读取。后续评估证据包或更长上下文时，保留完整方法卡、目标日志和逐行证据核验，不静默截断。
- Logparse 新产物树仍有多次跨边界完整核验；可评估首次核验后使用受控只读 stage 和清单复用，但不能改变原始日志、marker、来源和行号的判定依据。
- 若后续需要跳过 ROUTE，先设计明确的扁平 Skill selector 或单一专用入口。只有一个候选不代表它一定匹配问题。
- 更长轮询或进度流需要核对 Claude Host、局域网和代理的支持情况；当前维持 30 秒有限等待。

## P1：证据核验与诊断语义的保证范围（待评估）

- 已确认的实现边界：[`verify_method_diagnosis`](src/problem_locator/runtime/methods_grounding.py)
  核对方法归属、marker、来源、行号、原文和 identity token，并绑定冻结日志与回执；这些机械
  校验确认引用来源，summary 是否正确解释日志、因果关系和方法规则是否成立仍依赖模型判断。
- 专有 Job 默认冻结 `review_policy=NONE`，核验后的 Candidate 可直接交付；显式开启
  `INDEPENDENT` 后才由独立 Job/Workspace 的 Reviewer 复核。后续应明确产品对两种模式的质量
  承诺，并用相同输入比较误判、未解决率、耗时和模型成本，再决定是否调整审核策略。
- 建议补充“日志引用真实，但诊断规则或因果解释不成立”的反例，分别评估机械校验和语义审核。
  若业务需要更强的确定性保证，再设计可机读的方法规则及专项回归；不得把引用校验通过当成
  对全部诊断语义的机械证明。
- [`GenericLocatorExecutor`](src/problem_locator/runtime/generic_locator.py) 将原始问题交给预装
  Skill，校验结果格式、大小和哈希，不复用专有链路的逐行证据核验。其质量验收继续归入上方
  “Generic V2 最终集成与生产验收”，需要单独评估实际报告语义。

## P1：Methods V1 UNRESOLVED 真实分布

- 当前确定性测试已覆盖 Reviewer `REJECT`、`NEED_MORE_EVIDENCE`、证据不足和发布失败的收口行为，
  但这些覆盖样本不能代表生产分布。
- 后续在显式启用 Reviewer 的同身份生产运行中，只统计已脱敏的终态类别、证据缺口类别和方法数；
  不收集报告正文、原始日志、路径或身份 token。任何语义调整都必须先有足够样本和专项回归，不能
  用放宽核验或跳过 Reviewer 来降低 `UNRESOLVED` 比例。

## P1：日志抑制、限流与采样规则

- 当前版本只支持普通事件时间窗，不声明或推断日志抑制、限流或采样语义。
- 后续若业务 Skill 需要 75 秒或其他抑制机制，应新增显式、可机读的规则类型，并由 Skill 自己声明允许窗口方向、开闭边界、抑制键、最大间隔以及无日志时的可验证行为。
- 框架不得硬编码 75 秒，也不得在 Skill 未声明时自行放宽时间窗口。

## P2：核心大模块与历史实现的维护成本（待评估）

- 2026-09-05 基线中，[`contracts/models.py`](src/problem_locator/contracts/models.py) 约 6900 行，
  [`runtime/diagnosis_runtime.py`](src/problem_locator/runtime/diagnosis_runtime.py) 约 4600 行，合同
  校验和运行时流程集中在少数模块。生产主链路已采用 Methods V1，仓库仍保留大量 Evidence V2
  实现、测试和命名，后续修改容易混淆实际入口与历史路径。
- 后续先梳理生产入口、调用关系、历史或实验用途，以及它们对应的测试和文档，再决定是否拆分
  模块、统一命名或收敛旧路径。不能仅凭文件名中的 V2 判断代码无用，也不能把历史认证结果当成
  当前 V9 的发布证明；认证入口的收口继续归入上方“Methods V1 Reviewer 最长链路 Release”。
- 若实施重构，需保持公开扁平 MCP schema、当前合同版本、状态转换、冻结资产身份、权威产物和
  重启恢复行为，并用当前 Test Flow 验证同一源码快照。
