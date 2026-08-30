# Wiki 转 Methods Skill 与 Evidence V2 定位运行时

状态：已实施；当前发布结论只以 Test Flow 的密封 `verdict.json` 为准

当前版本：Problem Locator 5.0.0，State / Job / Outcome schema V8，
`v8-contract-r1`

## 目标与非目标

这条链路把一份已评审的故障定位 Wiki 转成一个闭合 Methods package。既有 `.agents`
入口只生成 package，再由 Problem Locator 产品注册绑定路由与运行时；局域网 `.claude`
入口直接生成包含同一 package 的完整生产 registration root。
在一份固定日志快照上，系统只允许用服务端生成的正向证据确认方法；日志缺失、
不可关联或证据不足必须保留为限制或不可定论，不能让模型补写证据身份。

它不是持续监控系统，不在固定快照之外补采日志，也不让生成 Agent 或诊断
Agent 决定部署范围、Logparse anchor、运行时资产或权威业务结果。

## 破坏兼容性边界

当前实现是一次明确的硬切：

- 状态只接受 State V8 / `v8-contract-r1` 的新空 `DATA_ROOT`，不迁移或恢复
  V1–V7 State、Job 或 Outcome。
- 两个现行元 Skill 都不再生成 `GenerationSpec`、编译 manifest 或
  `diagnosis-skill.json`。`.agents/skills/wiki-to-diagnosis-skill` 只生成 package；
  `.claude/skills/wiki-to-logparse-diagnosis-skill` 生成当前 Server 可直接加载的
  `registration-template.json` 与闭合 package。旧 `.claude/skills/wiki-to-diagnosis-skill`
  生成器与旧生成 fixture 不是当前入口。
- SPECIALIZED DIAGNOSE/REVIEW 不再接受 Agent 生成的 `AgentJobOutcomeDraftV2`，
  不再依赖 manifest verification contract、`verification_contract.py` 或
  `server_verifier.py`。这些旧文件已从当前源码删除。
- Methods V2 不生成 Candidate、`DecisionAuditV2` 或 `PARTIALLY_RESOLVED`，也没有
  Methods V1 的 diagnosis/review 草稿兼容入口。
- GENERIC DIAGNOSE 仍是独立的黑盒回退路径，不读取专用 Methods package、
  附件、Evidence 或 Review 状态。
- 七个公开 MCP 工具仍只接受扁平根参数；Methods 接入没有引入嵌套
  object、动态 Map、客户端 Hook、本地 MCP 或代理层。

历史设计、旧版合同和实施过程只在 Git 历史中保留，不与本文的现行架构并列。

## 所有权分层

| 层 | 拥有的信息 | 不得越界的信息 |
| --- | --- | --- |
| 作者 Wiki | 诊断方法、字段含义、正向日志模板、阈值/单位、观测与安全边界 | 产品部署和运行时资产版本 |
| `.agents` Wiki 元 Skill | 把 Wiki 忠实转为闭合 Methods package | 产品路由、Logparse 产品/anchor、Agent profile 和 output contract |
| `.claude` 局域网部署元 Skill | 生成完整生产 registration root；固定内置运行时绑定、内部默认 product、作者确认的 module 和双端 USER_FACT anchor | 不得改变 Wiki 诊断语义、固定 slot 或引入客户端本地运行链路 |
| 产品 registration | 能力描述、部署范围、package 绑定、DIAGNOSE/REVIEW 资产、Logparse plan | 不得改写生成 package 的 Wiki 语义 |
| Runtime | no-plan preflight、Logparse 冻结、单次证据扫描、Graph/Plan、隔离评估、共识和权威 Outcome | 不让模型扫描日志、自报证据或决定终态 |
| Test Flow Gate | 身份、工具轨迹、canonical validator、模型不可见的语义 oracle、主机/服务器证明 | 不向生成 Agent 泄露 oracle 或 registration |

## 闭合 Methods package

`.agents/skills/wiki-to-diagnosis-skill` 只生成以下目录：

```text
<skill-name>/
|-- SKILL.md
|-- methods.json
`-- references/
    |-- <method-id>.md
    `-- <shared-topic>.md
```

package 根目录只能有这三类条目。`methods.json@1` 使用 exact-key 合同，声明：

- `skill_name` 与原 Wiki SHA-256；
- Wiki 明确要求的 `required_user_inputs` 和 `required_artifacts`；
- 只能从日志中获得的 `log_derived_fields`；
- 共享参考卡和有序方法索引；
- 每种方法的独立 Markdown 卡、覆盖判断上下文的 `evidence_markers`，以及其中负责触发评估的
  `activation_markers` 保序子集。

每张方法卡固定包含适用条件、所需证据、计算与判断、确认条件、未知边界和
输出含义。相同原因的不同日志类型不拆成多个方法；多个原因可同时成立时，
运行时必须扫描全部目标日志，不能在第一个 marker 命中后短路。抑制、限流、
采样或条件打印只能形成未知边界，不能把日志缺失转换为排除证据。

marker 的所有权属于声明它的方法。即使两个方法声明了相同字面量，服务端也会
分别生成带各自 `method_id` 和 `marker_index` 的命中；后续 Plan 只能把命中分配给
所属方法，不能因为 marker 文本相同而跨方法复用。

package-only 生成合同把原本可能由模型自由改写的表面表示机械化：`log_derived_fields` 按 Wiki `text`
日志模板中命名占位符的首次出现顺序收集并排除用户输入；`evidence_markers` 使用第一个占位符前的
完整稳定字面前缀（模板以占位符开头时使用最长稳定片段）。canonical validator 与 gate-only oracle
必须遵循同一规则；`activation_markers` 则必须是非空、唯一的保序子集。它只表示该方法值得评估，
不表示单条命中足以确认原因。相同 literal 可以在多个方法中负责激活。

元 Skill 自带的 validator 校验目录、字段、frontmatter、Wiki hash、引用、方法卡标题和
原文 marker，但不代替场景诊断。局域网部署元 Skill 还校验完整 registration、固定运行时
绑定、双端动态 slot/process、可选 PID、作者确认 module 和生产部署范围；其日志模板提取版本
同时识别 `text` fence 与无语言 fence。

## 产品 registration 与身份

`SKILL_DIR` 的每个子目录是一个产品注册根：

```text
<SKILL_DIR>/
`-- <registration-id>/
    |-- registration-template.json
    `-- package/
        `-- <skill-name>/
            |-- SKILL.md
            |-- methods.json
            `-- references/*.md
```

`registration-template.json@1` 绑定 capability、deployment scope、package 相对路径、
`skill_name`、原 Wiki SHA-256、DIAGNOSE/REVIEW 的固定内置资产，以及可选的
Logparse 产品、角色和 anchor plan。当前 SPECIALIZED runtime 要求注册使用内置
Specialist/Reviewer profile、Methods-only tool bundle、context policy 与 output contract，不允许
registration 指向任意运行时资产。

Catalog 分别计算 registration SHA-256 和 package tree SHA-256，再计算 combined SHA-256。
Job 的 `diagnosis-skill/<registration-id>` ref 绑定 registration version 与 combined hash；
registration 或 package 任一字节变化都会改变身份，已冻结 Job 不能被当前 Catalog
静默替换。生产 Catalog 拒绝 `TEST_ONLY` 注册。

局域网部署元 Skill 的输出目录名就是 `registration_id`，registration 使用 `version=1.0.0`、
`deployment_scope=PRODUCTION` 和产品内置 DIAGNOSE/REVIEW 绑定。`logparse_product=default`
表示 Broker 不向上游传 `--product`，不是一个用户参数；client/server 共用作者在生成时明确
提供的 module，slot 与 process name 必须来自本次 Case 的 USER_FACT，PID 只在已提供时使用。

## 服务端 no-plan preflight

ROUTE 只返回声明了全部已提供 USER_FACT 名称的专用 registration 匹配项。
选中 SPECIALIZED 后，Runtime 用 registration 的 Logparse plan 与 Methods manifest 编译冻结
Logparse 执行计划。若缺少 Methods 声明的用户输入或 `log_archive`，它不启动 Agent，而是由
服务端直接生成 `NEED_INPUT` 或 `NEED_ATTACHMENT` Outcome 与
`supplement_policy=MISSING_ONLY` requirement。

当前 Methods runtime 只支持特殊附件 ID `log_archive`。Methods manifest 可声明
`problem_time`、进程名、service/API 等用户输入以及日志派生字段；Logparse
product、module、slot、角色与 anchor 的取值来源由产品 registration 固定。这个
preflight 是唯一能为 Methods 路径请求补充材料
的边界；Agent 输出合同没有 requirement 字段。

## 服务端单次证据构建

### Logparse 预处理与冻结

材料齐备后，产品 Logparse 预处理仍使用独立 `<job-id>.logparse-preprocess`
Workspace，只负责从上传包中选择 authoritative targets。服务端验证预处理结果后，
冻结 `request.json`、目标日志身份、Logparse receipt 和目标日志字节；随后撤销 broker
环境。模型角色不能重新解包、选择生命周期、调用 Logparse 或遍历其他日志。

冻结的目标日志只作为服务端证据构建输入。Specialist 和 Reviewer 的模型 Workspace
都不包含 `target_logs.json`、`target-logs/`、上传附件、既有 Evidence/Artifact 或先前
Outcome。

### Evidence Graph 与 Evaluation Plan

服务端对每份冻结目标日志只解码和逐行扫描一次。在这一处完成所有 marker 的
`casefold` 匹配，同时保留 marker 原文和命中行原文。扫描结束后，只保留至少命中一个自身
`activation_markers` 的方法；该方法在同一次扫描中得到的全部上下文命中都会保留。结果直接生成
method-qualified Evidence Graph：

- source 绑定 `source_id`、相对路径和内容 SHA-256；
- hit 绑定 `method_id`、方法优先级、`marker_index`、source、行号、marker 和原始行；
- event 只在同一方法内按日志派生身份字段分组；没有身份字段的命中各自成为独立事件；
- `loaded_method_ids` 精确等于有 activation 命中的方法，并按方法优先级和 ID 排序；
- Logparse 的观测 caveat 去重后写入 Graph 的 `limitations`。

因此，同一 marker 字面量属于不同方法时会生成不同的 hit；不同方法、不同请求或
无法可靠关联的事件不会因为文本相同被错误合并。

服务端随后只消费 Graph 中的 ref，生成完整 Evaluation Plan。每个已加载方法必须至少保留一个
activation hit，并且恰好有
一个 `evaluation_ref`，并精确覆盖该方法的全部 event/hit；整份 Plan 必须完整分区
Graph。这个阶段不再读取原始行、不重新匹配 marker，也不全量比较由模型回抄的
`SkillLoadReceiptV1`；Evidence V2 根本不把这份 V1 receipt 带入评估链路。没有任何
方法被激活时，服务端直接进入 `UNRESOLVED`，不启动 Specialist。

## Specialist 与 Reviewer 隔离评估

Specialist 和 Reviewer 使用同一选定模型身份，但分别运行在独立 Job、Workspace 和
上下文中。两者只读取：

- 当前角色自己的 `request.json`；
- 同一份 `method-evidence-graph.json` 和 `method-evaluation-plan.json`；
- `loaded_method_ids` 对应的方法卡及其显式共享引用。

Specialist 先独立评估完整 Plan。其结果通过服务端生成的
`methods_review_target` 只传递 Graph、Plan、Skill 和 evaluation 身份，Coordinator
据此创建不含 Candidate 的 REVIEW Job。Reviewer 看不到 Specialist 的 verdict、reason、
草稿、状态或上下文；Runtime 只在 Reviewer 模型调用返回后读取 Specialist 的已持久化
评估，再执行机械共识。

两个角色的输出合同完全相同：Specialist 写
`output/method-diagnosis.draft.json`，Reviewer 写
`output/method-review.draft.json`。文件内容是一个 JSON 根数组，并按 Plan 顺序精确
输出每个 evaluation。每项只能包含：

```json
  {
    "evaluation_ref": "eval-...",
    "verdict": "CONFIRMED",
    "supporting_event_refs": ["event-..."],
    "reason": "角色对该 evaluation 的判断理由"
  }
```

`verdict` 的合法值只有 `CONFIRMED`、`REJECTED` 和 `UNKNOWN`。

`CONFIRMED` 必须按 Plan 顺序选择当前 evaluation 的非空 `supporting_event_refs` 子集；
`REJECTED` 或 `UNKNOWN` 必须使用空数组。模型不回抄 `method_id`、marker、日志原文、行号、
hash、identity token、hit ref 或任何证据 receipt。服务端只接受与 Plan 数量、顺序和
`evaluation_ref` 完全一致的数组。

每个角色第一次出现 JSON 结构或 Plan 覆盖错误时，最多获得一次 repair。repair 仍使用
同一份 Graph、Plan 和方法卡，只提示重新提交完整数组；第二次仍不合格就进入
`UNRESOLVED`。已经归档 primary rejection 的重启只运行 repair；primary 和 repair 都已
归档时直接恢复终态，绝不发起第三次模型调用。

## 共识与状态真值

服务端裁决只比较两个角色逐项提交的 `(evaluation_ref, verdict, supporting_event_refs)`，不比较自由文本
`reason`：

| 条件 | Methods 状态 |
| --- | --- |
| 两次评估逐项一致、没有 `UNKNOWN`，且至少一个 `CONFIRMED` | `RESOLVED` |
| 任一项分歧、存在 `UNKNOWN`，或一致但没有确认原因 | `UNRESOLVED` |
| 没有匹配方法、角色模型失败、输出语义无效或 repair 耗尽 | `UNRESOLVED` |
| 冻结资源漂移、服务端不变量破坏或审计归档失败 | `FAILED` |
| 角色执行被取消 | `INTERRUPTED`，保留待执行角色，不发布终态投影 |

`RESOLVED` 只发布两次评估共同确认的 evaluation、method、所选 event，以及由所选 event 机械派生的 hit ref。
`UNRESOLVED` 与 `FAILED` 必须清空全部 confirmed ref，并发布固定 reason code、
`diagnostic_id` 和公共原因文本。Methods V2 只有 `RESOLVED`、`UNRESOLVED`、`FAILED`
三种终态，不产生 `PARTIALLY_RESOLVED`。

若资源解析、Workspace、Logparse 预处理或 execution-record 在 Graph/Plan 生成前失败，
服务端没有合法的 plan、graph 或 evaluation identity，因此不得构造
`MethodsTerminalProjectionV2`。这种 Case 直接以 `FAILED` 收口，`methods_result` 缺省，
`CaseFailure` 保存同一套 `FAILED` reason code、固定公共原因和稳定 `diag-*` ID。公共接口据此
明确区分“评估尚未开始”和“已有完整评估终态”，不靠伪造引用填满 DTO。

## limitations、公共投影与重放

`limitations` 是服务端拥有的数据。它从 Logparse authoritative target caveat 进入
Evidence Graph，同时写入独立 limitations record，并原样传到 Reviewer 和最终
`MethodsTerminalProjectionV2`。无论诊断最终是确认、不可定论还是系统失败，已记录的
观测限制都不能在跨 Job、重启或终态映射时丢失。

服务端先构造并校验完整 `MethodTerminalResultV2`、Outcome 和公共投影，再提交不可变
终态。公开 MCP/REST Case 只包含稳定的 result/evaluation/plan/graph/diagnostic 身份、
确认 ref、limitations、reason code 和固定公共原因；不包含 Specialist/Reviewer 的
verdict 数组、自由文本 reason、被拒草稿、marker、日志原文或行号。

内部 execution record 以 append-only 方式保存 Graph、Plan、State、limitations、每次
精确 prompt，以及被拒的 primary/repair 原始响应。Methods V2 不生成公共审计产物。
归档失败属于 `FAILED`，不能假装成证据不足。

validation-only replay 只读取已持久化的 Graph、Plan、State 和指定被拒响应，并调用
当前生产 parser 复现原拒绝原因。它不扫描日志、不创建 Workspace，也不调用模型或
backend。正常重启同样优先恢复已持久化记录：已有 Graph 时不再扫描日志；只有 Graph
而缺 Plan 时从 Graph 机械重建 Plan；已有 pending 或 terminal State 时按该状态继续或
直接发布结果。

正式入口是 `python -m problem_locator replay-method-rejection`。调用者只需指定当前
`DATA_ROOT`、`job_id`、角色和 PRIMARY/REPAIR attempt；成功与失败都返回稳定 JSON，且不会
写回 State 或 execution records。

## 自包含 Release case 与 Test Flow

当前 release case 使用 schema v2，业务专有材料全部收口在
`tests/cases/release/rpc-timeout-anonymized/`：

- 一份普通 Markdown Wiki；
- 一份产品 `registration-template.json`；
- 一份只对 Gate 可见的 Methods package 语义 oracle；
- 一条 `multiple-rpc-timeouts` CrossJob 场景及其 driver/oracle。

仓库不预存一份“批准的生成 package”。真实 Wiki→Methods Gate 只向生成 Agent 提供元
Skill、Wiki、由该 Wiki 原始字节生成并预先校验的 source-identity v2 sidecar，以及元 Skill直接链接的
output contract；v2 identity 除 Wiki digest 外，还提供从 Wiki 机械提取、按源顺序保留重复项的日志
模板清单。Agent 必须把该清单逐字写入固定 `references/source-log-templates.md`，并将它列为
`methods.json.shared_references[0]`。Agent 直接写完整 package，然后 Gate 审计 Skill/Read/Write v5
轨迹、运行独立重算 Wiki/模板的 canonical validator 和模型不可见的语义 oracle。验证通过后
Test Flow 才在 package 外复制产品 registration，以同一 package 字节身份供 fresh CrossJob
消费。

测试活动只从 `tools/test-flow/run.sh|run.ps1` 进入：

1. Dev 默认执行 affected + full deterministic，不调用真实模型。正向测试中的 Graph、Plan、
   State、Outcome、Case 和 verdict 必须由生产代码生成；fixture 只手写用户输入、Wiki、日志、
   附件和原始不可信模型响应。
2. Core 必须从真实 Case 入口覆盖 no-plan preflight、Logparse 冻结、单次扫描、casefold、
   跨 method 相同 literal、Plan 全覆盖、两个隔离角色、每角色一次 repair、共识真值、
   limitations、公共 MCP/REST 投影、重启恢复和 validation-only replay。
3. 负向用例从生产生成的合法基线开始，每次只修改一个字段。删除关键校验、恢复 receipt
   全量比较、重新匹配 marker 或允许第三次调用时，对应 mutation 必须失败。
4. Release 先用 `--plan-only` 审查 Proof、Stage、Gate、身份、模型预算、成本与
   admission blockers；Core PASS 之前不运行真实模型探针，同一失败身份不得盲目重试。
5. 需要真实模型认证时，各 provider cert 必须绑定相同 source snapshot、Evidence V2
   contract digest 和 Core verdict digest，并记录模型 revision、prompt/profile、调用次数、
   repair 次数和预算。只有 Core 与全部要求的 cert 都 PASS，最终 Release verdict 才成立。
6. Release planning 冻结 Git 可见工作树；只有与该 source snapshot 精确绑定、最后原子写入且
   可重新校验的 `verdict.json` 能声明通过。

Fast E2E 只能消费精确绑定的 registration/package 缓存，经公开 HTTP MCP 跑完 Case、
ROUTE、LOGPARSE、DIAGNOSE、REVIEW 和 `methods_result` 投影。它的 standalone verdict
只证明自身声明的短路径，不替代中央 Test Flow 或 Release。

模型名称、预算、超时、执行平台、可执行文件 hash 和缓存 admission 条件由 Test Flow
版本化配置管理；本文不把某次运行的 run ID 或 snapshot digest 固化为“最新结论”。

## 完成判据

- 普通 Markdown Wiki 能由 `.agents` 入口生成闭合 Methods package，也能由局域网 `.claude`
  入口生成包含该 package 的完整生产 registration root；固定模板引用精确绑定 Wiki 的机械清单。
- registration 与 package 的三层 hash 身份在 Catalog、Job、generation receipt 和 Test Flow 中一致。
- Client 收到问题描述后先创建 Case；只有建案后的服务端 no-plan preflight 可以返回
  `MISSING_ONLY` requirements，模型不能在建案前自行追问。
- 材料齐备后只运行一次服务端日志扫描。只有自身 activation marker 命中的 method 才进入 Graph，
  且它的全部上下文 hit 都会保留；Plan 精确覆盖全部 Graph event/hit。后续流程只按 ref 映射，
  不再扫描日志或匹配 marker。
- Specialist 与 Reviewer 使用隔离 Job、Workspace 和上下文，Reviewer 在提交盲评前看不到
  Specialist 结论；两个角色都只提交完整的
  `evaluation_ref + verdict + supporting_event_refs + reason` 数组。
- 每个角色最多一次 repair，重启不能增加调用次数；两次评估逐项一致、无 `UNKNOWN` 且至少
  确认一个方法时才 `RESOLVED`，其余业务不可定论进入 `UNRESOLVED`。
- Methods V2 不生成 Candidate、`DecisionAuditV2` 或 `PARTIALLY_RESOLVED`；只有资源漂移、
  服务端不变量破坏和审计归档失败进入 `FAILED`，取消则保留 `INTERRUPTED`。
- Graph/Plan 生成前失败时不得伪造评估引用；Case、MCP 和 REST 必须从 `CaseFailure` 返回稳定
  V2 reason code 与 diagnostic ID，且 `methods_result` 缺省。
- limitations 从预处理一直保留到公共投影；Methods V2 不生成公共审计产物，MCP/REST 不包含
  被拒草稿、角色自由文本 reason 或原始日志，内部拒绝记录可以由 validation-only replay 重放。
- 发布结论只引用当前源码快照对应的 Test Flow `verdict.json`，未执行、skip 或
  只有半成品目录的真实项不得声称通过。
