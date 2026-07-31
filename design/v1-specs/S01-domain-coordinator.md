# S01 领域模型与 Coordinator 说明书

- 状态：V1 详细设计冻结稿
- 说明书编号：S01
- 上游合同：[`v1-contract-r1`](S00-contract-freeze.md)
- 组合入口：[《S08 V1 组合说明书》](../v1-composition-spec.md)

## 1. 目标与非目标

S01 是 Case/Job 状态机、DiagnosisState 语义、Coordinator 决策、TransitionPlan 和 revision 业务规则的唯一权威来源。

本册实现的是无 I/O 的纯领域层。它不负责 JSON 持久化、资源发布、幂等记录、进程执行、调度、MCP/HTTP 或 logparse。

## 2. 独立文件责任区

未来 S01 实现任务唯一允许修改：

```text
src/problem_locator/domain/**
tests/unit/domain/**
tests/fixtures/components/domain/**
handoff/S01.json
```

公共模型必须从 `problem_locator.contracts` 导入。领域层可以定义内部校验器和纯函数，但不得复制公共枚举或 DTO。

## 3. 禁止修改项

- 不修改 S00 合同、Schema、错误码或限制；
- 不修改 storage、application、runtime、scheduler、interfaces、integrations；
- 不读写文件、环境变量、时钟、UUID、网络或进程；
- 不分配 Evidence/Artifact 正式 ID；
- 不提交 Dispatcher；
- 不直接运行 Router、Specialist 或 Reviewer；
- 不把 Agent 建议当作已接受事实；
- 不引入 `STALE` Job 状态或跨 Job Session。

## 4. 输入输出契约

唯一入口：

```text
Coordinator.plan(snapshot: CaseSnapshot, trigger: ValidatedTrigger)
  -> TransitionPlan
ContextSnapshotProjector.project(target_diagnosis_state: DiagnosisState)
  -> ContextSnapshot
```

`CaseSnapshot` 必须逐字段使用 S00 的固定合同：当前 Case（含唯一 DiagnosisState）、可选 active Job、可选恢复源 Job 和 replacement 索引。不得追加资源内容、协议对象或任意上下文字段。`ValidatedTrigger` 必须来自 S00 固定集合；Application Service 保证其引用、归属、幂等和 Schema 已通过技术校验。

Coordinator 输出完整计划，不做部分修改。`TransitionPlan` 必须：

- 明确目标 Case 状态；
- 明确是否清除活跃 Job；
- 对当前 Job 的每个状态变化给出 `job_updates`；
- 只接受 Outcome 中存在的 proposal key；
- 接受候选结论时只回显 `accepted_candidate_proposal_key`，不分配正式 conclusion ID；
- 通过 `selected_skill_update`、`case_failure_update` 和 `candidate_mutation` 显式表达所有 Case/Candidate 字段变化，不把这些变化留给 S03 推断；
- 明确接受的 DiagnosisStateDelta；
- 最多创建一个 `next_job_spec`；
- 若创建下一 Job，以 `target_state_revision`、资源 binding 和运行资产描述其固定输入模板；正式 ContextSnapshot 由 S03 在 proposal 解析后调用本册纯 projector 生成；
- 仅在 REVIEW PASS 时产生指向当前固定 CandidateTarget 的 `final_result_target`；S03 应用计划时将其对应完整候选置为 ACCEPTED 并写入 Case.final_result；
- 对同一输入产生字节等价的规范化结果。

`next_job_spec.evidence_bindings[]`、`artifact_bindings[]` 和 `review_target_binding` 必须使用 S00 的 placeholder union。引用本轮提案时只能填写已经被同一计划接受的 proposal key；引用既有对象时只能填写 CaseSnapshot 或 Trigger 中已验证的正式 ID。S01 不生成 Evidence、Artifact、Candidate 或 Job ID。

Coordinator 不选择运行资产版本。创建下一 Job 时只能逐字段复制 `ValidatedTrigger.runtime_bindings_by_job_type[target_job_type]` 中的 Profile、Skill/Skill Index、Tool Bundle、Context Policy、Output Contract、可选 logparse tool 和限制；缺少所需 binding 时返回 `VALIDATION_ERROR`，不得使用“当前最新版”。

非法业务转换直接返回 S00 `ApplicationError`，`code` 只能使用 `INVALID_CASE_STATE`、`ACTIVE_JOB_EXISTS`、`NEW_CASE_REQUIRED` 或 `VALIDATION_ERROR`，`retryable=false`，details[] 只使用 S00 `ApplicationErrorDetail`。S01 不定义或导出 `DomainDecisionError` 等第二套跨模块异常 DTO。

## 5. 聚合不变量

每次 `plan` 返回前必须同时满足：

1. `RUNNING` 必须有且仅有一个 `PENDING`/`RUNNING` 的 ROUTE 或 DIAGNOSE Job。
2. `REVIEWING` 必须有且仅有一个 `PENDING`/`RUNNING` 的 REVIEW Job。
3. `NEW` 只允许存在于创建计划内部，不得作为提交后的稳定状态。
4. `WAITING_INPUT`、`WAITING_ATTACHMENT`、`RESOLVED`、`FAILED`、`CANCELLED`、`INTERRUPTED` 没有活跃 Job。
5. 同一 Case 最多一个活跃 Job。
6. 下一 JobSpec 的 `target_state_revision` 等于计划应用后的 `DiagnosisState.revision`。
7. S03 解析 proposal 后交给 `ContextSnapshotProjector` 的 DiagnosisState 必须是同一目标状态；投影结果的 revision 和最终 Job.base_state_revision 都等于 `target_state_revision`。
8. `RESOLVED` 必须有 `final_result`，且候选状态为 `ACCEPTED`。
9. 非 `RESOLVED` Case 不得写 `final_result`。
10. `FAILED` 必须有 CaseFailure，其他 Case 的 failure 必须为 null。
11. REVIEWING 必须有状态为 REVIEWING 且与 active REVIEW Job target 相同的候选；RESOLVED 的候选和 final_result 必须是同一个 ACCEPTED 版本。
12. `selected_skill_ref` 必须来自 ROUTE Job 固定候选集；active DIAGNOSE/REVIEW Job 必须有 selected skill，active ROUTE Job 必须没有 selected skill。
13. 用户事实与确认事实保存在不同集合；无 Evidence 的 Agent proposed fact 不得进入确认事实。
14. 被 supersede 的条目保留但不进入新的有效上下文。
15. Job 和 Outcome 内容不可原地修改；计划只能改变 Job 生命周期字段。
16. Router 只作 `MATCHED` 或 `NO_CAPABILITY`，不能创建待补要求。
17. WAITING_INPUT/WAITING_ATTACHMENT 的全部 OPEN requirement 必须有同一个 `requested_by_job_id`，该 Job 必须有且仅有一个 APPLIED 的 NEED_INPUT/NEED_ATTACHMENT Outcome 使 Case 进入当前等待态。
18. REVIEW Job 固定 target 必须等于目标 ContextSnapshot 的 REVIEWING candidate，且其全部 supporting Evidence 必须按候选顺序包含在 Job evidence bindings 解析后的正式集合中。

## 6. 状态机

### 6.1 创建与路由

| 当前状态 | Trigger | 条件 | 目标状态 | Job 处理 | 语义状态 |
|---|---|---|---|---|---|
| 不存在 | `CREATE_CASE` | 问题目标完整且幂等已验证 | `RUNNING` | 创建 ROUTE | 初始化 revision 1 |
| `RUNNING` | `ROUTE_OUTCOME/COMPLETED` | `RouteKind=MATCHED` | `RUNNING` | ROUTE→SUCCEEDED；创建 DIAGNOSE | `selected_skill_update=SET(RouteDecision.skill_ref)` |
| `RUNNING` | `ROUTE_OUTCOME/NO_CAPABILITY` | `RouteKind=NO_CAPABILITY` | `FAILED` | ROUTE→SUCCEEDED | CLEAR selected skill；SET `CaseFailure(code=NO_CAPABILITY)`，不创建 General Code Agent |
| `RUNNING` | `ROUTE_OUTCOME/FAILED` | 按 6.6 分类 | `INTERRUPTED` 或 `FAILED` | ROUTE→INTERRUPTED/FAILED | 不变 |

Router 返回输入/附件请求、集合外 Skill 或其他结果类型属于技术合同错误，在进入 Coordinator 前由 S03 拒绝。

### 6.2 诊断结果

| 当前状态 | Trigger | 必要载荷 | 目标状态 | 下一 Job |
|---|---|---|---|---|
| `RUNNING` | `DIAGNOSIS_OUTCOME/NEED_INPUT` | `requested_input[]` 至少指向一个应用增量后 OPEN 的 `INPUT` requirement | `WAITING_INPUT` | 无 |
| `RUNNING` | `DIAGNOSIS_OUTCOME/NEED_ATTACHMENT` | `requested_attachments[]` 至少指向一个应用增量后 OPEN 的 `ATTACHMENT` requirement | `WAITING_ATTACHMENT` | 无 |
| `RUNNING` | `DIAGNOSIS_OUTCOME/COMPLETED` | 候选结论 | `REVIEWING` | REVIEW |
| `RUNNING` | `DIAGNOSIS_OUTCOME/COMPLETED` | 无候选，但有被接受的语义进展和明确下一目标 | `RUNNING` | DIAGNOSE |
| `RUNNING` | `DIAGNOSIS_OUTCOME/REROUTE` | 路由理由且没有候选结论 | `RUNNING` | ROUTE |
| `RUNNING` | `DIAGNOSIS_OUTCOME/FAILED` | `ExecutionFailure` | `INTERRUPTED` 或 `FAILED` | 无 |

以上每条都把当前 DIAGNOSE Job 结束为 `SUCCEEDED`，唯独执行失败按 6.6 写 `INTERRUPTED` 或 `FAILED`。带候选的 COMPLETED 必须 `candidate_mutation=INSTALL(accepted_candidate_proposal_key → REVIEWING)`，并同时接受 S00 要求的唯一 USER_RESULT Artifact proposal；缺失、重复或未接受都返回 `VALIDATION_ERROR`。创建 REVIEW Job 时，候选的全部 supporting Evidence binding 必须按候选顺序进入 `next_job_spec.evidence_bindings[]`，review target binding 指向同一候选。REROUTE 必须 `selected_skill_update=CLEAR`。等待结果可以同时接受中间 Evidence、假设、问题和事实增量；当前 Session 随 Job 结束。

`COMPLETED` 无候选、又没有任何可接受语义变化时是 `VALIDATION_ERROR`，防止零进展 Job 自动循环。

### 6.3 提交补充资料

`SUBMIT_SUPPLEMENT` 只接受 `WAITING_INPUT` 或 `WAITING_ATTACHMENT`：

1. 将结构化输入写入 `user_facts`，将 READY Attachment 引用绑定到对应 requirement；
2. 只满足名称、类型和约束匹配的当前 pending requirement；
3. 分批提交时立即保留已接受内容；
4. 仍有必需 requirement 未满足时，保持相应等待状态且不创建 Job；
5. 所有当前必需 requirement 都满足时，将其标为 fulfilled，创建且只创建一个新 DIAGNOSE Job，Case→`RUNNING`；
6. 输入仅是补充、澄清或环境信息时仍属于同一 Case；
7. 输入改变问题主体、目标系统、期望结果或完成条件时返回 `NEW_CASE_REQUIRED`，整次计划不产生任何修改。

若同时仍缺输入和附件，目标状态按确定顺序选择：先 `WAITING_INPUT`，输入满足后直接转为 `WAITING_ATTACHMENT`，不创建过渡 Job；两类均满足才创建一个 DIAGNOSE Job。提交不属于当前要求的额外资料不注入活跃或未来 Job；V1 以 `VALIDATION_ERROR` 拒绝整次提交。

### 6.4 复核

| 当前状态 | Trigger | verdict | 目标状态 | 处理 |
|---|---|---|---|---|
| `REVIEWING` | `REVIEW_OUTCOME/COMPLETED` | `PASS` | `RESOLVED` | REVIEW→SUCCEEDED；`candidate_mutation=SET_STATUS(REVIEWING→ACCEPTED)`；写相同 `final_result_target` |
| `REVIEWING` | `REVIEW_OUTCOME/COMPLETED` | `NEED_MORE_EVIDENCE` | `RUNNING` | REVIEW→SUCCEEDED；`candidate_mutation=SET_STATUS(REVIEWING→REJECTED)`；创建 DIAGNOSE |
| `REVIEWING` | `REVIEW_OUTCOME/COMPLETED` | `REJECT` | `RUNNING` | REVIEW→SUCCEEDED；`candidate_mutation=SET_STATUS(REVIEWING→REJECTED)`；创建 DIAGNOSE |
| `REVIEWING` | `REVIEW_OUTCOME/FAILED` | 不适用 | `INTERRUPTED` 或 `FAILED` | REVIEW→INTERRUPTED/FAILED；无下一 Job |

`NEED_MORE_EVIDENCE` 和 `REJECT` 都回到 DIAGNOSE，不由 Reviewer 直接把 Case 放进等待态；新的 Specialist 根据复核意见决定继续分析还是索要资料。两者创建的 DIAGNOSE Job 必须把当前 Review Outcome ID 作为唯一 `previous_outcome_refs[]`，因此 recommendation 和四类问题数组会进入新 Job 的固定输入。

`PASS` 仅在 ID、revision、content hash、状态 revision和全部 supporting Evidence 与 REVIEW Job 固定目标相同，且四个问题数组全空时合法。`NEED_MORE_EVIDENCE`/`REJECT` 必须满足 S00 问题数组非空矩阵；不满足者在 S03 技术校验阶段作为 `OUTCOME_INVALID` 拒绝，不进入 Coordinator。绑定过期属于 S03 的 `STALE` 处理，不进入 Coordinator。

Review 三个 verdict 的 candidate binding 都必须等于当前 REVIEW Job 固定 CandidateTarget；NEED_MORE_EVIDENCE/REJECT 的 mutation reason 使用 ReviewAssessment.recommendation，PASS 的 `final_result_target` 与 mutation binding 必须完全相同。S03 只解析并应用这些字段，不得自行根据 verdict 改 candidate。

### 6.5 取消、恢复与终态

| 当前状态 | Trigger | 目标状态 | 处理 |
|---|---|---|---|
| 任一非终态 | `CANCEL_CASE` | `CANCELLED` | 活跃 Job→CANCELLED，清除 active |
| `CANCELLED` | 同一取消幂等重放 | `CANCELLED` | 无变化 |
| `INTERRUPTED` | `RESUME_INTERRUPTED` | `RUNNING` 或 `REVIEWING` | 创建原 job_type 的唯一替代 Job |
| `RUNNING`/`REVIEWING` | `MARK_OLD_EPOCH_INTERRUPTED` | `INTERRUPTED` | 旧代次 RUNNING Job→INTERRUPTED；清除 active；不创建替代 Job |
| `RUNNING`/`REVIEWING` | `STALE_ACTIVE_OUTCOME` | `INTERRUPTED` | 当前 RUNNING Job→INTERRUPTED；清除 active；不应用 Outcome |
| `RUNNING`/`REVIEWING` | `ASSET_VERSION_UNAVAILABLE` | `FAILED` | PENDING Job→FAILED；清除 active |
| `INTERRUPTED` | `ASSET_VERSION_UNAVAILABLE` | `FAILED` | 原 INTERRUPTED Job 保持终态；不创建替代 Job |

终态为 `RESOLVED`、`FAILED`、`CANCELLED`。除取消幂等重放和只读查询外，终态拒绝全部 Trigger。`ResumeCase` 不提交新事实；等待资料的 Case 必须使用 `SubmitSupplement`。

上表所有进入 FAILED 的计划以及 6.6 中的 fatal 失败都必须 SET `CaseFailure`；进入 INTERRUPTED/CANCELLED 的计划不得写 failure。CaseFailure 的 source Job/Outcome 和 occurred_at 逐字来自已验证 Trigger，S01 不读取时钟。

替代 Job 必须设置 `replacement_for_job_id`。同一中断 Job 最多一个替代项；被中断的 REVIEW 只能替换为 REVIEW。

PENDING Job 的启动重投或 `ResumeCase` 唤醒不改变领域状态，不进入 Coordinator；它只把同一持久化 `job_id` 再交给 Dispatcher。

### 6.6 执行失败分类

以下错误无条件进入 `FAILED`：

```text
CONTEXT_LIMIT
ASSET_VERSION_UNAVAILABLE
BACKEND_OUTPUT_LIMIT
OUTCOME_MISSING
OUTCOME_INVALID
WORKSPACE_LIMIT
LOGPARSE_OUTPUT_INVALID
CONFIG_INVALID
RESOURCE_NOT_FOUND
RESOURCE_HASH_MISMATCH
RESOURCE_SIZE_MISMATCH
RESOURCE_LIMIT_EXCEEDED
PATH_VIOLATION
```

以下错误仅在 `ExecutionFailure.retryable=true` 时进入 `INTERRUPTED`，否则进入 `FAILED`：

```text
BACKEND_START_FAILED
BACKEND_CANCELLED
BACKEND_TIMEOUT
BACKEND_EXIT_FAILED
WORKSPACE_PREPARE_FAILED
RESOURCE_STAGE_FAILED
EXECUTION_RECORD_FAILED
LOGPARSE_FAILED
```

其他 S00 错误不会作为 Agent ExecutionFailure 进入 Coordinator，而由调用边界直接处理。

`DISPATCH_REJECTED` 发生在 Job 已持久化之后，Job 保持 PENDING；`RESOURCE_PUBLISH_FAILED` 和 `STATE_WRITE_FAILED` 发生在业务 commit 之前并返回调用方重试。这三项都不能通过一个尚未持久化的失败计划改变 Case。

### 6.7 下一 Job 固定输入

S01 只使用 S00 `ValidatedTrigger.continuation_resources` 的已验证正式 ID，不读取 Evidence/Artifact 元数据。下文的 continuation view 均逐字指该字段，固定规则如下：

- CREATE_CASE 的 ROUTE Job 四类资源引用均为空；REROUTE 的 ROUTE Job 也不携带 Evidence/Attachment/Artifact，但把本次 DIAGNOSIS Outcome ID 作为唯一 previous outcome，使 Router 看见结构化重路由理由。
- ROUTE MATCHED、无候选的 DIAGNOSE continuation、SUBMIT_SUPPLEMENT 全部满足、以及 REVIEW 非 PASS 创建的 DIAGNOSE Job，逐字复制 continuation view 的现有 Evidence/Attachment/Artifact ID；若本轮同时接受新 Evidence/Artifact，则按目标 DiagnosisState Evidence 顺序追加对应 proposal binding，并只追加这些 Evidence 依赖的 Artifact binding。previous outcome 逐字复制 continuation view。
- 带候选的 DIAGNOSE Outcome 创建 REVIEW Job时，`evidence_bindings[]` 表达目标 DiagnosisState 的完整 `evidence_refs[]` 顺序，并确保候选 supporting bindings 是其子序列；Attachment/Artifact 使用 continuation view 加被这些 Evidence 依赖的本轮 Artifact proposal，previous outcome 固定为本次 DIAGNOSIS Outcome ID。唯一 USER_RESULT 虽被同计划接受，但不作为 Reviewer 必需输入。
- RESUME_INTERRUPTED 的替代 Job 四个集合逐字复制源 Job，不追加失败 Outcome 或执行时新资源。

existing ID 使用 `PlannedResourceBinding.existing_resource_id`，同轮新资源使用已接受 proposal key。每个数组去重时保留首次出现位置。S01 不根据 Artifact metadata 推导依赖；S03 已在 continuation view 中提供既有闭包，而同轮 LOGPARSE Evidence 的 proposal binding 在 Outcome 内显式指出其 Artifact proposal。若闭包、binding 或 supporting coverage 不完整，返回 `VALIDATION_ERROR`，不得创建一个由 S03 猜测修补的 Job。

## 7. DiagnosisStateDelta 规则

处理顺序固定为：

1. supersede ProblemSpec 内被明确替代的同目标字段；
2. 写入用户事实；
3. 接受有合法 provenance 和 Evidence 的 proposed facts；
4. 更新、拒绝或新增假设；
5. 解决旧问题，再新增未决问题；
6. 满足旧 requirement，再新增 requirement；
7. 将接受的 Evidence 正式引用加入状态；
8. 接受候选 proposal key，或更新/拒绝既有候选结论；正式候选 ID、证据 binding 和 hash 由 S03 解析后写入；
9. 去重并按稳定 ID 排序；
10. 仅在有效语义投影变化时增加 DiagnosisState.revision。

同一 item ID 只能由一次增量操作；`supersedes[]` 不得成环；已 rejected 假设不能原地恢复，重新提出时使用新 ID 并引用旧项。候选结论内容或证据集合变化必须增加其 revision 并重算 hash。

Agent `proposed_facts` 默认不进入 `confirmed_facts`。只有 statement、指向本 Outcome 的 provenance 和至少一个已固定或本轮接受的 Evidence binding 都完整时，Coordinator 才能把它写入 `accepted_state_delta`。Agent Outcome 的 `add_user_facts[]` 与 `fulfill_requirements[]` 必须为空；这两项只接受 S03 在 SUBMIT_SUPPLEMENT Trigger 中提供的已校验正式条目。

形成 Candidate 时，Coordinator 必须验证 completion-criteria draft mapping 按索引恰好覆盖当前 ProblemSpec、criterion 原文逐字一致、每项 satisfied 且至少有一个 Evidence binding；任一项不满足时拒绝 candidate，不能创建 REVIEW Job。S03 负责把 binding 解析为正式 Evidence ID，Coordinator 不分配资源 ID。

Coordinator 接受带 `source_type=LOGPARSE` 且 `source_binding.artifact_proposal_key` 非空的 Evidence proposal 时，必须在同一 `TransitionPlan` 同时接受被引用的 `LOGPARSE_RUN` Artifact proposal；若 Artifact 未被接受，该 Evidence、依赖它的 state delta 与 candidate 都不得被接受。其他 `source_type` 不得使用 `artifact_proposal_key`。Coordinator 只处理同一 Outcome 内的 proposal key 绑定，不分配或猜测正式 Artifact ID。

## 8. revision 与快照

S01 只声明某个计划是否包含业务/lifecycle 变化；具体计数和条件写由 S03/S02执行。

- 每个非空 TransitionPlan 使 `case_revision + 1`；
- 仅诊断语义投影变化使 `DiagnosisState.revision + 1`；
- Job 认领、取消、失败、中断、Attachment 状态、重复/迟到 Outcome 审计不增加 DiagnosisState.revision；
- 纯幂等重放返回原计划结果，不再次执行计划；
- 下一 Job 在增量应用后物化，使用新的语义 revision；
- 接受至少一个实质变化的 ProblemSpecPatch 时 ProblemSpec.revision 恰好 +1；同值/空 patch 不变且不能单独构成语义进展，目标变化则整次返回 NEW_CASE_REQUIRED；
- REVIEW verdict 不增加语义 revision 的唯一例外是 `PASS`：候选从 REVIEWING 到 ACCEPTED 及 final_result 写入视为语义变化，增加 revision；
- `NEED_MORE_EVIDENCE`/`REJECT` 会改变候选状态并写复核意见，因此增加语义 revision。

## 9. TransitionPlan 确定性

数组顺序固定为：现有条目保持原稳定顺序，新条目按 `item_id` 排序追加。计划不包含 `now()`、随机 ID 或文件路径；需要的 ID 与时间由 Trigger 提供。相同规范化 `CaseSnapshot + ValidatedTrigger` 必须产生相同 Canonical JSON。

公共 `ContextSnapshotProjector` 是本册实现的纯函数：

```text
project(target_diagnosis_state)
```

投影只保留当前有效条目，不加载文件内容，不选择日志片段，不做模型摘要。TransitionPlan 只携带 `next_job_spec.target_state_revision`，不能携带尚未实体化的 snapshot。

S03 先按计划解析 proposal binding、分配正式 ID并构造最终目标 DiagnosisState，再把该完整状态传入 projector。生成结果必须逐字段符合 S00 `ContextSnapshot`：`diagnosis_state_revision` 等于目标 DiagnosisState revision，并包含 `problem_spec`、`user_facts[]`、`confirmed_facts[]`、`active_hypotheses[]`、`rejected_hypotheses[]`、`open_questions[]`、`pending_requirements[]`、`evidence_refs[]` 和可空 `candidate_conclusion`。Application Service 创建 Job 时必须令 `base_state_revision`、`target_state_revision` 和 snapshot revision 相等；不得从转换前状态或执行时最新状态重新投影。

## 10. Fake、Fixture 与注入点

S01 使用 S00 的固定时钟、ID 和合同 Fixture，不创建 Repository Fake。组件 Fixture 至少包括：

- 新 Case→ROUTE→DIAGNOSE；
- 参数组 A 分批提交；
- 唯一日志 requirement；
- 接受 `LOGPARSE_RUN` 和中间 Evidence 后索要参数 B；
- 参数 B 满足后继续 DIAGNOSE，R10 状态到 R11 计划逐字携带既有 Evidence、源 Attachment、LOGPARSE_RUN 和等待来源 Outcome；
- 同 Outcome 新 Evidence+Candidate→REVIEW，由 S03 解析后投影正式 snapshot，候选 supporting Evidence 全部固定；
- 候选与唯一 USER_RESULT 同时接受→REVIEW→PASS；
- REVIEW `NEED_MORE_EVIDENCE` 与 `REJECT` 回到 DIAGNOSE；
- REVIEW 回流 Job 的 PREVIOUS_OUTCOME 固定包含 recommendation/问题数组，PASS 与非 PASS 问题数组非法组合均拒绝；
- 稳定目标变化返回 `NEW_CASE_REQUIRED` 且计划为空；
- recoverable/fatal 错误的完整分类；
- PENDING 重发、旧代次中断、REVIEW 替代；
- 所有非法状态/Trigger 笛卡尔组合。

唯一注入点是纯 `ContextSnapshotProjector`；测试中使用确定性实现验证投影前后 revision。

## 11. 独立测试命令

```text
python -m pytest tests/unit/domain -q
```

测试必须对所有 CaseStatus × Trigger 组合做表驱动覆盖，并验证相同输入得到 Canonical JSON 等价计划。

## 12. 完成标准

- 第 6 节每个合法转换均有正向测试；
- 所有未列出的状态/Trigger 组合都返回固定错误；
- 同一 Case 永远不产生两个活跃 Job；
- SubmitSupplement 的分批保存与“全部满足后恰好一个 Job”已验证；
- Router 从不索要资料；
- `STALE` 未出现在 JobStatus；
- fatal/recoverable 失败分类完整；
- Reviewer 是进入 RESOLVED 的唯一门禁；
- 所有纯函数无 I/O、全局时钟、随机数和可变单例；
- S00 合同和本册测试命令通过。

## 13. S08 交接格式

```json
{
  "spec_id": "S01",
  "title": "Domain Model and Coordinator",
  "executor": {"model": "gpt-5.6-sol", "reasoning_effort": "ultra"},
  "contract_revision": "v1-contract-r1",
  "contract_base_commit": "<contract-base-commit>",
  "branch": "codex/v1-s01-domain-coordinator",
  "head_commit": "<head-commit>",
  "scope_completed": [],
  "changed_files": [],
  "fixtures_consumed": [],
  "fixtures_produced": [],
  "tests": [{"command": "python -m pytest tests/unit/domain -q", "status": "passed"}],
  "dependency_requests": [],
  "contract_change_requests": [],
  "known_limitations": [],
  "risks": [],
  "integration_notes": [],
  "forbidden_scope_touched": false
}
```

以上顶层字段全部必填，不得省略；没有内容的列表写空数组。交接文件固定写入 `handoff/S01.json`。

## 14. 合同变更请求

若 Trigger、TransitionPlan 或公共枚举不足，不得在领域包创建替代 DTO。按 S00 第 16 节格式提交变更请求，并附上失败的状态矩阵行和预期计划；S00 接受并发布新修订后才能实现。
