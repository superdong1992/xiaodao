# Diagnosis Skill V3 通用化与 V2 可验证定位设计

## 执行状态

- 状态：已落地（2026-08-08）。
- State、Job、Agent draft 和权威 Outcome 使用 V2 合同；V2 是硬切版本，不读取、迁移或恢复 V1 State、V1 Job、V1 Outcome。
- Diagnosis Skill 生成器和正式生成 Skill 为 `3.1.1`，GenerationSpec 为 `v3`，manifest schema 为 `3`。
- 本次把“Agent 按 Skill 分析”升级为“Agent 声明 + 服务端按 Skill 机器复验”，并把 Reviewer 改为盲审。
- 本次不实现日志抑制、限流、采样或 75 秒规则；相关后续工作统一记录在仓库根目录的 [`TODO.md`](../TODO.md)。

## 冻结版本矩阵

| 合同或资产 | 版本 |
| --- | --- |
| Problem Locator package | `1.0.7` |
| State / Job / Outcome schema | `2` |
| S00 contract revision | `v2-contract-r1` |
| GenerationSpec | `v3` |
| Diagnosis Skill generator / 生成 Skill | `3.1.1` |
| Diagnosis Skill manifest | `3` |
| `verification_contract` | schema `1` |
| ROUTE output contract | `2.0.0` |
| DIAGNOSE output contract | `3.0.0` |
| REVIEW output contract | `2.0.0` |
| Specialist / Reviewer profile | `1.0.1` / `1.0.1` |
| Router / Diagnose / Review tool bundle | `2.0.0` / `2.0.0` / `2.0.0` |

七个公开 MCP 工具及其扁平输入 schema 不在本次变更范围内。`create_case` 和 `submit_supplement` 继续使用等长 name/value 标量数组，服务端不解析嵌套对象或 JSON 字符串。

## 边界与信任模型

系统继续保持三层业务边界：

- 全局 DIAGNOSE/REVIEW output contract：定义 V2 draft、Canonical JSON、Evidence/Candidate、Workspace 写入和封存约束，不包含 RPC、数据库等业务字段。
- `logparse-diagnose`：负责 broker、一次 parse、`LOGPARSE_RUN` 持久化与复用、受控路径和请求审计。
- 生成的 Diagnosis Skill：声明业务 requirements、Logparse 字段映射、事件提取器和判断规则。

Agent 不是最终裁决者。Agent 的文字结论、Evidence summary、文件名、Logparse target 状态、先前解释和 `rule_claims` 都是待验证声明。只有服务端按固定 Skill 和原始 Evidence 重算后的 DecisionAudit 才能使 Candidate 进入 Review 或使 Review PASS 生效。

## Manifest v3 与机器验证合同

manifest v3 是生成 Skill 的规范化事实源，`SKILL.md` 从同一对象确定性渲染。每个 requirement 声明：

- `name`、`kind`、`stage`、`fulfillment_source`、`prompt` 和 S00 原生 `constraints`；
- `supplement_policy=MISSING_ONLY`；
- `INPUT -> USER_FACT`，`ATTACHMENT -> READY_ATTACHMENT`；
- 阶段固定为 `INITIAL | AFTER_LOGPARSE`。

`requires_logparse` 只控制工具绑定，不自动添加 RPC 参数、附件或后补参数。默认 Logparse 产品可以省略；省略时 Runtime 记录有效产品 `default`，Broker 不向上游显式传 `--product`。

每个需要机器判断的 Skill 还声明 `verification_contract`：

- `event_extractors[]`：固定 anchor、整行正则、时间字段/格式、业务字段和事件基数；
- `rules[]`：稳定 ID、规则类型、依赖、参数和可选补救 requirement；
- 普通事件时间窗必须显式给出相对 `problem_time` 的前后毫秒数及上下界开闭方向；
- 当前规则类型覆盖事件存在/基数、时间窗、事实字段相等、角色覆盖、跨角色关联、事件顺序和语义因果。

RPC 字段只存在于 service-takeover 的 Wiki、正式演示 Skill 和对应 Fixture；不得提升为通用合同默认值。

## Draft 到权威 Outcome

DIAGNOSE 和 REVIEW 使用相同的服务端可信边界：

1. Runtime 固定 Job、Skill、用户事实、资源和（适用时）`resolved_logparse_plan`。
2. Agent 读取原始 Evidence，写 `output/job_outcome.draft.json`。draft 是 `AgentJobOutcomeDraftV2`，不含 `outcome_id`、`produced_at` 或 `decision_audit`。
3. Agent 最后调用 `problem-locator-seal-outcome-draft`。sealer 只做 Schema、Canonical JSON、USER_RESULT 和 size/SHA-256 校验，不生成权威结论。
4. Agent 退出后，服务端重新读取固定输入和原始 Evidence，独立扫描 Evidence locator 的完整允许范围。事件基数不能只从 Agent 引用的行推断，否则 Agent 可能通过漏引重复行隐藏 `EXACTLY_ONE` 失败。
5. 服务端逐条重算 Skill 规则；Agent citation 只能证明它引用过某个服务端匹配行，不能代替完整扫描或规则结果。缺失/空的 Logparse 行号 locator 不可验证并导致规则失败。
6. 服务端生成唯一权威 `output/job_outcome.json`、`decision_audit.json`、`decision_evidence.jsonl` 和 finalization manifest。

服务端对原始整行字节（包括行结束符）计算哈希，展示文本与哈希来源分离，避免不同换行处理掩盖证据变化。

## 固定输入与错误时间

Case 已有的 `problem_time`、service、method、order 等 USER_FACT 是冻结输入。`resolved_logparse_plan` 的附件/Artifact、问题时间和有序 anchors 必须与固定 Skill 和用户事实逐字段相同，Agent 不得改写时间、替换 anchor 或另选归档来寻找更像结论的日志。

只有 Skill 声明为 OPEN、当前确实缺失且 `supplement_policy=MISSING_ONLY` 的 requirement 可以补充。已有值错误不是“缺参”，不能在同一个 Case 中替换；若证据表明时间或参数错误，应终止当前 Case，并用正确事实创建新 Case。

## Reviewer 盲审

REVIEW 不继续 Specialist 会话，也不把 Specialist 判词、Candidate prose、Finding、Evidence summary 或先前 Outcome 当成证明。Runtime 构造固定 `ReviewSubjectV2`，其中包含：

- Candidate 的固定 identity/revision/hash；
- Candidate supporting Evidence 与全部 completion-mapping Evidence 的稳定去重并集；
- 固定 Skill、required rule IDs、用户事实和服务端机械事实；
- 需要独立判断的语义因果 assertions。

Reviewer 必须重新打开所有 required Evidence 并按 Skill 顺序逐条执行规则。只有所有规则独立通过，且 `unsupported_findings`、`evidence_conflicts`、`missing_evidence`、`stale_references` 全为空时才允许 PASS。服务端随后再次执行同一机器校验，不信任 Reviewer 自报 PASS。

`NEED_MORE_EVIDENCE` 只允许指向一个真实 OPEN 的 `MISSING_ONLY` requirement；否则 Review REJECT 或无法验证都会终止为 `UNRESOLVED`，不自动生成无限重试链。

## INCONCLUSIVE、UNRESOLVED 与结果可见性

DIAGNOSE 若提出 Candidate 但没有通过服务端正向门禁，或 REVIEW 声称 PASS 但没有通过同一门禁，权威 Outcome 会被归一化为 `INCONCLUSIVE`。合法的 REVIEW `REJECT` 会保留为负向判决并终止为 `UNRESOLVED`；`NEED_MORE_EVIDENCE` 仅在恰好指向一个真实缺失的 `MISSING_ONLY` requirement 时进入等待，否则同样归一化为 `INCONCLUSIVE`。典型阻断原因包括：

- `problem_time` 超出 Skill 窗口或边界不符；
- 必选参数与原始事件字段不一致；
- 必需角色缺失；
- 跨角色关联值不一致；
- 事件基数或顺序错误；
- 原始行未读、locator 不完整或证据冲突；
- 语义因果边缺少证据，只有时间接近或合理叙事。

Coordinator 将 `INCONCLUSIVE` 和合法的 REVIEW `REJECT` 终止为 `UNRESOLVED`，不再自动派生同阶段 Job。已提出的 Candidate 以 `REJECTED` 保留在内部审计状态；对应 `USER_RESULT` 和 `USER_RESULT_ARCHIVE` 不可下载。只有经过 Reviewer PASS 和服务端复验的 Candidate 才变为 `ACCEPTED` 并公开结果。

## 可观察审计，而非隐藏思维链

每个 `UNRESOLVED` Case 生成一个确定性的可下载 `AUDIT_BUNDLE`。审计包按 allowlist 收集：

- Case/Job 和问题/用户事实快照；
- 实际发送给 Agent 的 `context.txt`；
- Agent 的 `agent_job_outcome.draft.json`；
- 权威 `job_outcome.json`、`decision_audit.json`、`decision_evidence.jsonl` 和 finalization manifest；
- REVIEW 的 `review_subject.json`；
- 存在时的 broker audit，以及 stdout/stderr 的可用性、字节数和 SHA-256 元数据。

审计包不包含被拒绝的 USER_RESULT、原始上传归档、完整 Logparse 目录或原始 stdout/stderr。后两者仍保存在本地 execution record 和隔离 replay 目录，供局域网内的 Agent 读取；可下载包只公开 stdout/stderr 的存在性、大小和哈希。

这些材料能回答“输入是什么、Agent 声明了什么、引用了哪些原始行、服务端哪条规则为何 `VERIFIED_PASS`/`VERIFIED_FAIL`/`UNVERIFIABLE`/`SEMANTIC_ONLY`、进程输出了什么”。Agent 自报的 PASS/FAIL/UNKNOWN 会作为 claim 并列记录，但不能覆盖服务端结果。审计材料不包含模型隐藏思维链；服务端加日志也不能取得或重建隐藏推理过程。

## 隔离重放

`replay-job` 是普通 CLI，没有管理员身份、管理 API、认证或权限模型。它只接受当前 V2 源闭包，使用当前固定资产在新安装中运行：

- `diagnose-only`：执行一个源 DIAGNOSE Job，生成服务端终结结果但不提交到隔离 State；
- `review-only`：执行一个源 REVIEW Job，生成服务端终结结果但不提交到隔离 State；
- `through-review`：执行并提交 DIAGNOSE；若产生唯一 REVIEW Job，再执行并提交 REVIEW。诊断直接进入等待、改路由或 `UNRESOLVED` 而没有 REVIEW，也是成功的 `NO_REVIEW_JOB` 停止结果。

源服务必须停止，因为 CLI 使用同一独占实例锁。输出目录必须是绝对路径、尚不存在且不与源数据、Skill、Logparse、配置或 DFX 路径重叠。CLI 只写隔离 `output-dir`，不修改源安装；replay manifest 记录源/重放固定资产引用、逐绑定差异和阶段输入输出哈希，用于在局域网修改 Skill/Verifier 后比较同一阶段。

## Fixture 与参数隔离

- RPC service-takeover：INITIAL 包含 `caller_service/server_service/rpc_method/problem_time`，一个日志归档，AFTER_LOGPARSE 包含 `order_id`；两个固定 anchor 和非默认 product。
- 数据库死锁：使用独立的实例、进程、事故时间、归档和事务参数，省略 product 以覆盖默认值。
- 无日志人工排查：没有 module、roles、附件、Logparse 或后补阶段。

每个 Outcome 的 requirement 集合必须精确等于其 manifest。向错误 Case 提交其他场景参数或附件必须失败且状态不变；无日志场景不得调用 broker。两个日志场景各自只 parse 一次，续跑复用固定 `LOGPARSE_RUN`。

## 迁移与兼容边界

- V2 State/Job/Outcome 是硬切合同；不存在 V1 启动恢复、原地迁移、隐藏旧字段或运行时兼容分支。
- 部署 V2 使用新的 `DATA_ROOT`。旧根保持只读，仅可作为历史材料，不得交给 V2 服务或 `replay-job`。
- 旧 Diagnosis Skill 必须显式按 GenerationSpec v3 重新生成；Runtime 不按需猜测或迁移旧 manifest。
- 本次不改变 Windows Claude Code 通过 HTTP 直连 Linux MCP Server 的部署边界，也不增加客户端 Hook、代理或专用 DFX。

## 后续工作

本设计中的活跃待办已统一迁移至仓库根目录的 [`TODO.md`](../TODO.md)，此处不再维护重复清单。
