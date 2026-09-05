# TODO

更新时间：2026-09-05

本文件是仓库活跃待办的唯一清单。已完成事项由代码、当前设计与 Git 历史证明，不在这里保留关闭项。

2026-09-05 仓库分析的核对基线为 `main@443ca21` / Problem Locator `6.0.0` / State V9。
本次补充记录已由代码路径确认的实现边界及待评估风险，尚未执行性能压测或新的 Test Flow。
后续是否修复、采用何种方案，仍需结合届时的当前版本、复现证据和实际使用需求决定。

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

## P1：显式专用路由与多 Case 队头阻塞

- 当前 `create_case` 没有专用 Skill selector，registration 也只有自由文本 capability；唯一候选仍可能
  与问题不匹配。因此不能按候选数量自动跳过 ROUTE。若要消除实测约 2 分 34 秒的 ROUTE Agent，
  需要选择一种显式合同：由专用客户端提交完整扁平 selector，或把某个 Linux endpoint 明确配置成
  单一专用入口。服务端必须校验当前 production registration 并冻结完整 ref；旧客户端省略 selector
  时继续执行语义 ROUTE。
- 先用 `backend_phase=ROUTE` 的真实遥测核对 `turn_count`、`model_api_duration_ms` 和 Write 工具耗时。
  如果 ROUTE 因落盘草稿产生额外模型回合，可另行设计“模型只返回最小 RouteDecision、服务端补齐并
  冻结完整 Outcome”的单响应合同；不得把未密封的 stdout 文本直接当作权威结果。
- 当前调度器仍只有一个 active worker。多用户时，短 ROUTE 会排在其他 Case 的长 DIAGNOSE 后面，
  单 Case 本地基准无法暴露这类队头阻塞。后续若拆分 ROUTE lane 或开放并发，必须先冻结 CPU、内存、
  Logparse 子进程、状态提交和取消/恢复的资源隔离合同，再用多 Case Linux 压测给出 P50/P95/P99。
- 2026-09-05 已核对 [`InProcessDispatcher`](src/problem_locator/dispatch/dispatcher.py)：所有 Case
  共用一个 FIFO worker。当前预处理已直接调用 Logparse，但正常专有链路通常仍需 ROUTE 和
  Specialist 两次 Agent 启动。后续压测应分别统计排队、状态提交、Logparse 和模型执行耗时。
- 单体 StateFile 与资源复核带来的跨 Case 开销单列在下方“状态提交与全库资源校验开销”，与调度
  队头阻塞分别评估。

## P1：状态提交与全库资源校验开销（待评估）

- 已确认的代码路径：[`JsonFileStateRepository.commit`](src/problem_locator/storage/state_repository.py)
  在共享协调锁内生成完整 StateFile、校验全部外部引用、重写 `state.json`，再调用
  `_decode_and_validate` 复核落盘结果；后者再次校验全部外部引用。每轮都遍历全部 Case 的
  Job、Outcome 和资源，并由 [`validate_formal_resource`](src/problem_locator/storage/resource_files.py)
  读取资源的完整内容计算哈希。
- 待验证影响：提交成本可能随历史资源总量增长，一个 Case 的大附件可能拖慢其他 Case 的状态
  查询和更新。代码路径已确认，具体延迟、I/O 量和容量拐点尚未压测。
- 后续分别增加 Case 数、Job/Outcome 数和归档总字节数，测量资源校验次数、读取字节数、锁等待、
  提交延迟及多 Case P50/P95/P99；区分冷缓存与热缓存，避免只用小样本推断长期运行表现。
- 取得基线后，再评估按 Case 分片、增量核验或 append-only 日志。方案必须保留资源篡改检测、
  原子发布、幂等、崩溃恢复和源码快照证明；不能仅以 mtime/size 缓存替代最终内容校验。

## P1：专用定位单响应模型合同与 Logparse 批处理

- 当前 Specialist 仍由通用 Agent CLI 读取 `request.json`、目标日志和方法卡，再写
  `method-diagnosis.draft.json`。真实环境约 4 分 23 秒的 `BACKEND_EXECUTE` 是否来自多轮文件工具调用，
  必须先用新增 `backend_phase/backend_invocation_id` 遥测核对 `turn_count`、各工具耗时和模型 API 时间。
  若证据成立，应设计服务端生成的有界 evidence packet，并让低延迟模型一次返回可由服务端密封的
  结构化草稿；不能直接信任未密封 stdout，也不能丢掉原始日志的最终逐行校验。
- 一个 `parse-targets` 仍会按 anchor 串行启动多个 `mech-target-logs` Python 进程。优先给 pinned
  Logparse 增加一次性 multi-target 命令，在单进程中按声明顺序返回全部目标；并发子进程只能作为
  次选，启用前必须证明上游解析树只读、取消能回收整棵进程树，且总 CPU/内存仍受 Job 上限约束。
- Logparse 新产物树仍会在初检、目标捕获、跨 Specialist 边界和正式发布时多次完整 hash。后续可在
  第一次完整校验后立即封存为只读受控 stage，保存 inode/metadata seal 和 TreeManifest；中间边界
  用轻量 seal，跨 Agent 与正式发布仍做完整 hash。不得用可恢复的 mtime/size 代替最终内容校验。
- 30 秒长轮询会让分钟级 Job 产生多次客户端工具回合。只有确认 Claude Code/Codex Host、反向代理
  和企业网络都支持更长请求后，才考虑把上限提高到 90–300 秒；否则应使用连接稳定的进度流协议，
  不能单纯延长超时导致远端 MCP 请求被 Host 提前中断。

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
