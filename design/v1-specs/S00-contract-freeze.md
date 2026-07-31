# S00 公共合同冻结说明书

- 状态：V1 详细设计冻结稿
- 说明书编号：S00
- 合同修订：`v1-contract-r1`
- 上位规范：[《Problem Locator V1 基线设计》](../v1-baseline-design.md)
- 组合入口：[《S08 V1 组合说明书》](../v1-composition-spec.md)

## 1. 目标与非目标

S00 是 V1 公共词汇、数据结构、Port、错误码、状态枚举、资源限制和共享 Fixture 的唯一权威来源。S01～S07 可以依赖这些合同，但不能复制后改写它们。

S00 的目标是让各组件仅依赖冻结合同即可独立实现和验证。S00 不实现领域状态转换、磁盘算法、应用编排、Agent 运行、调度、网络适配或 logparse 调用。

## 2. 上游输入与冻结产物

输入是 `v1-contract-r1` 对应的基线设计、决策记录和本说明书。未来实现任务必须生成并维护：

```text
pyproject.toml
uv.lock
src/problem_locator/contracts/
├─ __init__.py
├─ commands.py
├─ enums.py
├─ errors.py
├─ limits.py
├─ models.py
├─ outcomes.py
├─ ports.py
└─ serialization.py
schemas/v1/
├─ contract-manifest.json
├─ state.schema.json
├─ job.schema.json
├─ agent-job-outcome.schema.json
├─ job-outcome.schema.json
├─ workspace-input-manifest.schema.json
├─ logparse-parse-claim.schema.json
├─ user-result.schema.json
├─ handoff.schema.json
└─ fixture-manifest.schema.json
tests/contracts/
tests/fixtures/contracts/
handoff/S00.json
```

`contract-manifest.json` 固定为 `{schema_version:1,contract_revision,generator_version,files[]}`，每个 file 条目只有 `{path,sha256}`。files 精确包含 `src/problem_locator/contracts/**/*.py` 和 `schemas/v1/*.schema.json` 的普通文件，使用仓库相对 POSIX path 并按 path 升序；明确排除 `contract-manifest.json` 自身、`pyproject.toml`、`uv.lock`、tests、fixtures、handoff、缓存和生成临时文件，避免自哈希及后置交接漂移。Schema 由 Python 合同模型生成，不手工维护第二套字段定义；manifest 不含生成时间，同一合同输入必须字节稳定。

S00 必须在冻结的 `pyproject.toml` 工程骨架中预注册 console script：

```toml
[project.scripts]
problem-locator-logparse = "problem_locator.integrations.logparse.cli:main"
```

该声明只冻结安装入口；`problem_locator.integrations.logparse.cli:main` 的实现与测试归 S07，S00 不创建 `integrations` 业务代码。Diagnosis Skill 只能调用这个随服务安装的受控入口，不得自行拼接 `LOGPARSE_REPO` 绝对路径。

## 3. 独立文件责任区

未来 S00 实现任务唯一允许修改：

- `pyproject.toml` 与 `uv.lock`，仅用于冻结 V1 Python 与测试依赖；
- `src/problem_locator/contracts/**`；
- `schemas/v1/**`；
- `tests/contracts/**`；
- `tests/fixtures/contracts/**`；
- `handoff/S00.json`；
- `design/v1-specs/README.md`，仅用于合同冻结或已接受合同修订所需的公共路径和责任矩阵同步。

S00 是公共合同、Schema、合同测试与合同 Fixture 的唯一维护者。`pyproject.toml`、`uv.lock` 和责任矩阵只在合同冻结阶段由 S00 修改；`v1-contract-r1` 冻结后，S00 不再拥有根依赖文件，获批的根依赖变更只能由 S08 在集成分支串行应用。根包导出、启动装配和业务实现由 S08 或对应分册负责。

## 4. 禁止修改项

S00 实现任务不得修改：

- `src/problem_locator/domain/**`、`src/problem_locator/storage/**`、`src/problem_locator/application/**`、`src/problem_locator/runtime/**`、`src/problem_locator/dispatch/**`、`src/problem_locator/interfaces/**`、`src/problem_locator/integrations/**`；
- `.claude/skills/**`；
- 组件测试和端到端测试；
- Case 转换策略、JSON 原子写算法、进程控制或协议适配行为；
- 已冻结合同的含义而不递增 `contract_revision`。

## 5. 公共词汇与枚举

### 5.1 状态枚举

| 枚举 | 固定值 |
|---|---|
| `CaseStatus` | `NEW`、`RUNNING`、`WAITING_INPUT`、`WAITING_ATTACHMENT`、`REVIEWING`、`RESOLVED`、`FAILED`、`CANCELLED`、`INTERRUPTED` |
| `JobStatus` | `PENDING`、`RUNNING`、`SUCCEEDED`、`FAILED`、`CANCELLED`、`INTERRUPTED` |
| `JobType` | `ROUTE`、`DIAGNOSE`、`REVIEW` |
| `OutcomeDisposition` | `APPLIED`、`DUPLICATE`、`STALE`、`REJECTED` |
| `FailureReportDisposition` | `APPLIED`、`DUPLICATE`、`STALE` |
| `OutcomeResultType` | `COMPLETED`、`NEED_INPUT`、`NEED_ATTACHMENT`、`REROUTE`、`NO_CAPABILITY`、`FAILED` |
| `AttachmentStatus` | `UPLOADING`、`READY`、`FAILED` |
| `CandidateStatus` | `PROPOSED`、`REVIEWING`、`REJECTED`、`ACCEPTED` |
| `ReviewVerdict` | `PASS`、`NEED_MORE_EVIDENCE`、`REJECT` |
| `RouteKind` | `MATCHED`、`NO_CAPABILITY` |
| `RequirementKind` | `INPUT`、`ATTACHMENT` |
| `RequirementStatus` | `OPEN`、`FULFILLED` |
| `ArtifactKind` | `USER_RESULT`、`DIAGNOSTIC_EXPORT`、`LOGPARSE_RUN` |
| `ResourceKind` | `FILE`、`DIRECTORY` |
| `CancellationReason` | `USER_CANCEL`、`SERVICE_SHUTDOWN` |
| `DiagnosisItemStatus` | `ACTIVE`、`RESOLVED`、`REJECTED`、`SUPERSEDED` |
| `DiagnosisProvenanceType` | `USER_INPUT`、`AGENT_OUTCOME` |
| `FieldUpdateAction` | `SET`、`CLEAR` |
| `CandidateMutationAction` | `INSTALL`、`SET_STATUS` |
| `EvidenceSourceType` | `USER_FACT`、`ATTACHMENT`、`LOGPARSE`、`TOOL_OUTPUT`、`PREVIOUS_OUTCOME` |
| `AssetKind` | `AGENT_PROFILE`、`DIAGNOSIS_SKILL`、`TOOL_BUNDLE`、`CONTEXT_POLICY`、`OUTPUT_CONTRACT`、`LOGPARSE_TOOL` |
| `ExecutionStage` | `ASSET_RESOLUTION`、`CONTEXT_BUILD`、`WORKSPACE_PREPARE`、`BACKEND_START`、`BACKEND_EXECUTE`、`TOOL_EXECUTE`、`OUTCOME_VALIDATE`、`RESOURCE_STAGE`、`EXECUTION_RECORD` |
| `ContextSectionKind` | `PROFILE`、`SKILL`、`SKILL_INDEX`、`TOOL_BUNDLE`、`JOB_INSTRUCTION`、`CONTEXT_SNAPSHOT`、`OPEN_REQUIREMENTS`、`REVIEW_TARGET`、`OUTPUT_CONTRACT`、`PREVIOUS_OUTCOME`、`EVIDENCE`、`RESOURCE_MANIFEST` |
| `WorkspaceInputKind` | `ATTACHMENT`、`EVIDENCE`、`ARTIFACT`、`PREVIOUS_OUTCOME` |

`STALE` 只表示一次 Outcome 提交的处理结果，不是 `JobStatus`。数据库迁移或接口适配不得重新把它解释为 Job 终态。

### 5.2 通用标量

- `OpaqueId = str`；所有明确标注为 OpaqueId 的业务实体 ID（Case、Job、Outcome、Attachment、Evidence、Artifact、Candidate、DiagnosisItem、Requirement、Trigger、failure、runtime epoch 等）是不透明、小写连字符 UUID，调用方不得从 ID 推导路径或权限。`VersionedRef.id` 是稳定逻辑资产名，`proposal_key`、requirement/name、幂等键和 storage key 另有各自合同，不属于 OpaqueId。
- `UtcTimestamp = str`；值是带 `Z` 的 UTC RFC 3339 字符串，精度固定到毫秒。
- `CanonicalJsonBytes[T] = bytes`；`T` 只表达这些不可变字节解码后的 Schema 类型，字节本身必须符合第 11 节且不得在 Port 内改用文本字符串。
- SHA-256 是 64 位小写十六进制字符串。
- `ContentType = str`；V1 故意采用比通用 MIME 更窄的 Canonical 子集，必须是 3～127 个 ASCII 字符并匹配 `^[a-z0-9][a-z0-9!#$&^_.+-]{0,62}/[a-z0-9][a-z0-9!#$&^_.+-]{0,62}$`。type/subtype 均只能小写且各 1～63 字符；禁止参数、引号、通配符、空白、控制字符、CR/LF、非 ASCII 和大小写自动规范化。请求、持久化 Attachment/Artifact、requirement allow-list、UploadDescriptor 与 HTTP header 都逐字使用同一值。
- `storage_key` 是 `/` 分隔的相对 POSIX 路径，不允许空段、`.`、`..`、反斜杠、驱动器号或绝对路径。
- requirement 和结构化事实的 `name` 必须匹配 `^[a-z][a-z0-9_]{0,63}$`；同一请求内名称不得重复，同一 DiagnosisState 内所有 `OPEN` requirement 的名称不得重复。
- `revision`、`generation` 和字节数是大于等于零的十进制整数。
- 除 hash、枚举和 MIME type 外，用户文本均是 UTF-8 字符串：按 Unicode 空白判断不得为空，但持久化时逐字保留，不自动 trim、改写大小写或拼接标签；单个用户文本值最多 `65536` UTF-8 bytes。
- 持久化对象、AgentJobOutcome、JobOutcome 和响应中的可选字段无值时显式写 JSON `null`；外部请求表中标 `?` 的字段可以省略，Adapter 必须先补成 null/默认值再验证和计算规范化 hash。集合没有成员时用空数组或空对象，不省略。
- 未在 Schema 中声明的字段一律拒绝。

跨 Port 的同步流和取消协议固定为：

```text
BinaryStream
  read(max_bytes: integer) -> bytes
  close() -> None
  __enter__() -> BinaryStream
  __exit__(...) -> None

AppendOnlyByteSink
  write(chunk: bytes) -> None
  flush() -> None
  close() -> None

CancellationSignal
  reason: CancellationReason?
  is_cancelled() -> bool
  wait(timeout_seconds: number?) -> bool

LogparseBrokerSession
  agent_environment() -> map<string,string>
  close() -> None

AttachmentUploadGuard
  acquire(attachment_id) -> AttachmentUploadLease

AttachmentUploadLease
  attachment_id: OpaqueId
  is_released() -> bool
  release() -> None
```

`BinaryStream.read` 要求 `max_bytes > 0`，每次返回不超过该值的不可变 bytes，EOF 固定返回 `b""`；不支持 seek/tell，`close` 幂等，关闭后 read 必须失败。调用者必须通过 context manager 或 `finally` 关闭。`AppendOnlyByteSink.write` 接受非空 bytes 并全部写入或抛出类型化错误，不允许部分写返回值；`flush/close` 幂等，关闭后 write 必须失败。`ExecutionLogSinks.stdout/stderr` 都实现该协议并共享合同给出的合计计数器。

`CancellationSignal.reason` 在未取消时为 null，取消后永久固定为一个 `CancellationReason`；`wait(null)` 等到取消，非 null timeout 必须大于等于 0，返回值只表示返回时是否已取消。Signal 只读且线程安全，Runtime 不得清除或改写它。`LogparseBrokerSession.agent_environment()` 只返回当前 Job 访问受控 broker 所需的两个键 `PROBLEM_LOCATOR_LOGPARSE_ENDPOINT` 与 `PROBLEM_LOCATOR_LOGPARSE_TOKEN`，值不得为空；`close` 幂等，必须先使 token 失效，再同步终止并回收该 session 启动的全部 logparse 子进程和本地 endpoint，返回时不得有残留。endpoint/token 只存在于进程与 Agent 子进程环境，禁止持久化、记录或进入 Outcome/响应。`AttachmentUploadGuard` 是单进程内按 attachment_id 串行化的长流保护：不同 ID 可并行，同 ID 按获取顺序等待；lease 的 attachment_id 创建后不可变，`is_released()` 只读且线程安全，lease 不可跨线程传递、release 幂等，进程退出自然释放。S02 的 Guard 和 FileResourceStore 必须共享同一 per-attachment registry，使 `stage_attachment` 能验证 lease capability、ID 和释放状态。它与短 `PublicationCommitLease` 不同，后者绝不能覆盖网络流读取。框架可在 HTTP 等最外层做异步适配，但 S00 Port 的同步语义、关闭责任和背压边界不得改变。

## 6. 输入输出契约：核心数据合同

以下字段是 V1 最小且完整的共享合同。实现可以增加私有计算类型，但不得增加持久化或协议字段。

### 6.1 Case 与 DiagnosisState

```text
Case
  case_id, status, case_revision, diagnosis_state,
  active_job_id?, selected_skill_ref?, final_result?, failure?,
  created_at, updated_at

DiagnosisState
  revision, problem_spec, user_facts[], confirmed_facts[],
  active_hypotheses[], rejected_hypotheses[], open_questions[],
  pending_requirements[], evidence_refs[], candidate_conclusion?

ProblemSpec
  revision, statement, expected_behavior, actual_behavior,
  scope, goals[], non_goals[], constraints[], completion_criteria[]

ProblemSpecInput
  statement, expected_behavior, actual_behavior,
  scope, goals[], non_goals[], constraints[], completion_criteria[]

UserFactInput
  name, value

DiagnosisItem
  item_id, statement, status, provenance, evidence_refs[],
  created_revision, supersedes[]

DiagnosisProvenance
  source_type, source_ref, input_name?

PendingRequirement
  requirement_id, kind, name, prompt, required, constraints,
  status, requested_by_job_id, fulfilled_by_refs[]

InputRequirementConstraints
  value_type="STRING", min_utf8_bytes, max_utf8_bytes,
  pattern?, allowed_values[]

AttachmentRequirementConstraints
  allowed_content_types[], min_count, max_count

CompletionCriterionMapping
  criterion_index, criterion, satisfied, evidence_refs[], explanation

CandidateTarget
  candidate_conclusion_id, candidate_revision, candidate_content_hash
```

外部 CreateCase 只接收 `ProblemSpecInput`；S03 验证后补 `revision=1`，形成持久化 `ProblemSpec`。`statement`、`expected_behavior`、`actual_behavior`、`scope`、`goals[]` 和 `completion_criteria[]` 均不得为空；`non_goals[]`、`constraints[]` 可以为空；各数组保持调用方顺序且不得含完全相同的重复字符串。`initial_user_facts[]` 是 `UserFactInput` 数组，最多 64 项。

`user_facts` 保存用户逐字提交且已绑定 provenance 的事实；`confirmed_facts` 只保存 Coordinator 接受、且能引用证据的诊断事实。两者不可合并为一个无来源列表。S03 将每个 `UserFactInput.value` 原样写入一个 `DiagnosisItem.statement`；其 `provenance.source_type=USER_INPUT`、`source_ref=trigger_id`、`input_name=UserFactInput.name`。Agent 提议的事实使用 `source_type=AGENT_OUTCOME`、`source_ref=outcome_id` 且 `input_name=null`。`source_ref` 只用于审计来源，不作为磁盘路径。

`PendingRequirement.constraints` 由 `kind` 判别：INPUT 只能使用 `InputRequirementConstraints`，ATTACHMENT 只能使用 `AttachmentRequirementConstraints`。V1 的 `required` 必须为 true；INPUT 默认且合法边界为 `value_type=STRING`、`1 <= min_utf8_bytes <= max_utf8_bytes <= 65536`，`pattern` 是 Python 3.12 `re.fullmatch` 语法字符串或 null，`allowed_values=[]` 表示不设枚举限制；ATTACHMENT 满足 `1 <= min_count <= max_count`，`allowed_content_types[]` 每项必须是上述 Canonical ContentType、数组内逐字唯一并保持声明顺序，空数组表示不额外缩小全部合法 ContentType，非空时用逐字相等判断 membership，不做大小写或参数归一化。同一 DiagnosisState 最多存在一个 OPEN ATTACHMENT requirement，因此扁平 `attachment_ids[]` 的归属唯一。INPUT requirement 的 `fulfilled_by_refs[]` 只含为它创建的 user-fact item ID，ATTACHMENT requirement 只含同 Case READY Attachment ID；数量和约束满足后才能置为 `FULFILLED`。

`CandidateConclusion` 固定包含 `conclusion_id`、`revision`、`content_hash`、`statement`、`supporting_evidence_refs[]`、`completion_criteria_mapping[]`、`proposed_by_job_id` 和 `status`。其 mapping 必须按 `criterion_index` 升序、恰好覆盖当前 ProblemSpec 的每个 `completion_criteria[]` 项，`criterion` 与对应原文逐字相等；进入 REVIEW 前每项必须 `satisfied=true`、`evidence_refs[]` 非空且引用当前 Case Evidence。

`CandidateConclusion.content_hash` 只对以下对象按第 11 节 Canonical JSON 求 SHA-256：

```json
{
  "statement": "<逐字声明>",
  "supporting_evidence_refs": ["<按候选固定顺序>"],
  "completion_criteria_mapping": ["<每个完整 CompletionCriterionMapping，按 criterion_index>"]
}
```

preimage 明确排除 `conclusion_id`、`revision`、`status`、`proposed_by_job_id` 和 `content_hash` 本身。状态从 REVIEWING 变为 ACCEPTED/REJECTED 不改变 hash；声明、supporting Evidence 的值或顺序、任一完整 mapping 字段的变化都必须改变 hash。

`Job.review_target`、`ReviewTargetBinding.existing_candidate_target` 和 REVIEW 对比使用同一个 `CandidateTarget`。`Case.final_result` 是状态为 `ACCEPTED` 的完整 `CandidateConclusion`，其 ID/revision/hash 必须与通过 REVIEW 的 CandidateTarget 相同；非 RESOLVED Case 的 final_result 为 null。

`CaseFailure` 固定包含 `code`、`message`、`source_job_id?`、`source_outcome_id?` 和 `occurred_at`。`NO_CAPABILITY` 使用同名失败码；其他值必须来自第 9 节。非 `FAILED` Case 的 `failure` 必须为 null。

`PendingRequirement.status` 只能使用 `RequirementStatus`；`DiagnosisItem.status` 只能使用 `DiagnosisItemStatus`；`Evidence.source_type` 与 `EvidenceProposal.source_type` 只能使用 `EvidenceSourceType`。集合名称与条目状态必须一致，例如 `active_hypotheses[]` 不能包含 `REJECTED` 条目。

### 6.2 Job 与固定输入

```text
Job
  job_id, case_id, job_type, status, goal, base_state_revision,
  context_snapshot, evidence_refs[], attachment_refs[],
  previous_outcome_refs[], artifact_refs[], agent_profile_ref,
  available_skill_refs[], skill_ref?, tool_bundle_ref,
  context_policy_ref, output_contract_ref, logparse_tool_ref?,
  logparse_product?,
  review_target?,
  replacement_for_job_id?, resource_limits, created_at,
  started_at?, finished_at?, runtime_epoch?

ContextSnapshot
  diagnosis_state_revision, problem_spec, user_facts[],
  confirmed_facts[], active_hypotheses[], rejected_hypotheses[],
  open_questions[], pending_requirements[], evidence_refs[],
  candidate_conclusion?
```

`ContextSnapshot` 是 Job 创建所在 TransitionPlan 应用后的目标 DiagnosisState 的确定性投影；`diagnosis_state_revision` 必须等于该目标状态的 revision，也必须等于 Job 的 `base_state_revision`。上述字段是公共 Schema 的完整字段集，不得省略或增加私有持久化字段；`problem_spec` 自身包含目标、非目标、约束和完成条件。它不是会话记录、自由文本摘要或执行时最新状态。

每个 Job 的 `evidence_refs[]` 必须是其 ContextSnapshot `evidence_refs[]` 的去重子序列；`attachment_refs[]`、`artifact_refs[]` 和 `previous_outcome_refs[]` 必须属于同一 Case 且内容不可变。REVIEW Job 的 `review_target`、ContextSnapshot.candidate_conclusion 的 ID/revision/hash 必须完全相同，候选状态必须为 REVIEWING，并且候选 `supporting_evidence_refs[]` 必须按其固定顺序全部出现在 Job.evidence_refs 中；缺一项都不得创建或认领 REVIEW Job。非 REVIEW Job 的 `review_target` 必须为 null。

`agent_profile_ref`、`available_skill_refs[]`、`skill_ref`、`tool_bundle_ref`、`context_policy_ref`、`output_contract_ref` 和 `logparse_tool_ref` 均使用完整 `VersionedRef`，不得用裸版本字符串替代。`context_policy_ref` 与 `output_contract_ref` 的命名在 Job、JobSpec 和 RuntimeBindings 中保持一致。`logparse_tool_ref` 与 `logparse_product` 必须同时为 null 或同时非 null；非 null 时 Job 必须为 DIAGNOSE、固定 Diagnosis Skill 的 manifest 必须 `requires_logparse=true`，且 `logparse_product` 必须逐字等于该 manifest 的非空值。ROUTE、REVIEW 和不使用 logparse 的 DIAGNOSE Job 两者都为 null。

Coordinator 的公共输入快照固定为：

```text
CaseSnapshot
  case, active_job?, resume_source_job?,
  replacement_job_ids_by_source{}

ContinuationResourceView
  evidence_refs[], attachment_refs[], artifact_refs[],
  previous_outcome_refs[]
```

`case` 含唯一的 `diagnosis_state`；`active_job` 必须等于 `Case.active_job_id` 所指 Job；`resume_source_job` 只在 INTERRUPTED 恢复时出现，且必须是最近一个尚未被替代的同 Case INTERRUPTED Job；`replacement_job_ids_by_source` 用于证明一个中断 Job 最多一个替代项。已经由 Application Service 验证的资源、Outcome 和用户提交内容进入 Trigger payload，不再以任意“附加上下文”字段塞入快照。

`ContinuationResourceView` 是 S03 从同一 StateFile generation 构造并完成归属、状态、hash 和依赖闭包校验的只读正式 ID 集合，随 `ValidatedTrigger` 提供给 S01；它不是任意附加上下文，也不含路径或资源内容。每个数组内 ID 唯一。`evidence_refs[]` 按当前 DiagnosisState 顺序；其他数组先按来源 Job 的冻结顺序保留，再按 target DiagnosisState requirement 顺序和 Evidence 顺序追加尚未出现的依赖。依赖闭包必须加入 ATTACHMENT Evidence 的 Attachment、LOGPARSE Evidence 的 `LOGPARSE_RUN` Artifact 及其 metadata 指定的源 Attachment、TOOL_OUTPUT Evidence 的 `DIAGNOSTIC_EXPORT` Artifact、PREVIOUS_OUTCOME Evidence 的同 Case 已保存且不可变的 `source_ref` Outcome，以及已满足 ATTACHMENT requirement 中的 READY Attachment；新增的 PREVIOUS_OUTCOME 来源按 Evidence 顺序稳定去重追加到 `previous_outcome_refs[]`。

对 Outcome Trigger，来源 Job 是当前 active Job，`previous_outcome_refs[]` 先放本次已验证 incoming Outcome ID，再按上段规则稳定追加该 Job 固定集合及 Evidence 依赖中尚未出现的 prior Outcome；本次 incoming Outcome 是唯一允许尚未存在于 StateFile snapshot 的 continuation 对象，它必须已经通过 `RuntimeExecutionReceipt` 的文件 hash、Canonical bytes、Schema 和 Job 绑定校验，并在引用它的 next Job 所在同一个 `StateMutation` 中先作为规范 Outcome 插入。任何其他 previous outcome 都必须是同一 snapshot 中已保存且不可变的正式对象。对 `SUBMIT_SUPPLEMENT`，来源 Job 是所有当前 OPEN requirement 共同的 `requested_by_job_id`，previous outcome 固定为该 Job 唯一 APPLIED 且使 Case 进入当前等待态的 Outcome，再按依赖闭包追加；对 `RESUME_INTERRUPTED`，四个数组逐字复制 `resume_source_job` 的固定引用；`CREATE_CASE` 和不创建后续 Job 的控制 Trigger 使用四个空数组。S03 不判断下一 Job 类型或业务取舍，只构造该机械闭包；S01 决定目标 Job 使用哪些集合并可追加本轮被接受 proposal 的 binding。

`VersionedRef` 固定为 `{id, version, content_hash}`。`ResourceLimits` 固定为：

```text
context_bytes
wall_time_seconds
stdout_stderr_bytes
workspace_bytes
```

ROUTE Job 的 `context_bytes=131072`；DIAGNOSE 和 REVIEW Job 的 `context_bytes=204800`。三类 Job 的其他默认值分别为 1800 秒、67108864 字节和 1073741824 字节。

### 6.3 AgentJobOutcome、JobOutcome 与载荷

```text
AgentJobOutcome
  outcome_id, job_id, case_id, job_type, base_state_revision,
  result_type, payload, consumed_evidence_refs[],
  proposed_evidence_drafts[], proposed_artifact_drafts[], error, produced_at

JobOutcome
  outcome_id, job_id, case_id, job_type, base_state_revision,
  result_type, payload, consumed_evidence_refs[],
  proposed_evidence[], proposed_artifacts[], error, produced_at
```

Agent 只能原子写 `AgentJobOutcome` 到 Workspace 的 `output/job_outcome.json`。其中有文件内容的草稿只能引用 `output/proposals/<proposal_key>/...` 下的 Workspace 相对路径。Runtime 校验路径和内容、通过 ResourceStore 完成持久化暂存，再构造不含 Workspace 路径的规范 `JobOutcome`；只有后者交给 S03、发布到 `jobs/<job_id>/job_outcome.json` 并进入 `state.json`。

允许组合：

| Job 类型 | 允许的 `result_type` | 非失败载荷 |
|---|---|---|
| `ROUTE` | `COMPLETED`、`NO_CAPABILITY`、`FAILED` | `RouteDecision` |
| `DIAGNOSE` | `COMPLETED`、`NEED_INPUT`、`NEED_ATTACHMENT`、`REROUTE`、`FAILED` | `DiagnosisOutcome` |
| `REVIEW` | `COMPLETED`、`FAILED` | `ReviewAssessment` |

`FAILED` 必须令 `payload=null` 且 `error=ExecutionFailure`；其他结果必须令 payload 为表中对应类型且 `error=null`。两个字段在 JSON 中始终存在，不能同时非空或同时为空。

`RouteDecision` 包含 `kind`、`skill_ref?`、`reason` 和 `confidence`。`MATCHED` 必须携带固定候选集中的 `skill_ref`；`NO_CAPABILITY` 不得携带 `skill_ref`，Router 不得索要输入或附件。

`DiagnosisOutcome` 包含 `findings[]`、`state_delta`、`requested_input[]`、`requested_attachments[]`、`candidate_conclusion_draft?` 和 `recommended_next_step`。`Finding` 固定为 `{statement,evidence_bindings[],confidence}`，其中 `confidence` 是闭区间 `0..1` 的十进制数；`recommended_next_step` 是非空说明文本，只供后续 Agent 阅读，不能覆盖 `result_type` 驱动的状态转换。

`requested_input[]` 和 `requested_attachments[]` 只保存 requirement ID，不复制 prompt 或 constraints；ID 必须分别指向应用本次 `state_delta` 后仍为 OPEN 的 INPUT 和 ATTACHMENT requirement，数组内不得重复。`NEED_INPUT` 的 `requested_input[]` 非空且 `requested_attachments=[]`；`NEED_ATTACHMENT` 则相反。若同轮同时缺两类资料，先返回 `NEED_INPUT`，附件 requirement 可以写入状态增量但不列入本轮 `requested_attachments[]`；输入满足后 Coordinator 直接转入 `WAITING_ATTACHMENT`，不创建过渡 Job。

`DiagnosisStateDelta` 与其可选 ProblemSpec 补丁固定为：

```text
ProblemSpecPatch
  statement?, expected_behavior?, actual_behavior?, scope?,
  goals?, non_goals?, constraints?, completion_criteria?

DiagnosisStateDelta
  problem_spec_patch?, add_user_facts[], proposed_facts[],
  add_active_hypotheses[], update_hypotheses[], reject_hypotheses[],
  add_open_questions[], resolve_questions[],
  add_pending_requirements[], fulfill_requirements[],
  add_evidence_bindings[]

DiagnosisItemDraft
  item_id, statement, provenance, evidence_bindings[], supersedes[]

DiagnosisItemChange
  item_id, statement?, reason, evidence_bindings[]

RequirementFulfillment
  requirement_id, fulfilled_by_refs[]
```

`add_user_facts[]` 和 `fulfill_requirements[]` 只接收 S03 为 SUBMIT_SUPPLEMENT 构造的正式 `DiagnosisItem` 与 `RequirementFulfillment`；Agent 产生的 DIAGNOSE Outcome 必须令两者为空。`proposed_facts[]`、`add_active_hypotheses[]` 和 `add_open_questions[]` 使用 `DiagnosisItemDraft`；`update_hypotheses[]`、`reject_hypotheses[]` 和 `resolve_questions[]` 使用 `DiagnosisItemChange`。新条目的 item ID 由 Agent 提供并在 Outcome 内唯一；其 provenance 必须为本 Outcome，Evidence 使用下述 binding。update 可以带新 statement；reject/resolve 的 statement 必须为 null；三者都必须带非空 reason。`add_pending_requirements[]` 使用完整 `PendingRequirement`，且 `requested_by_job_id` 必须是当前 Job。`add_evidence_bindings[]` 的元素是 `EvidenceBinding`。

Coordinator 的 `accepted_state_delta` 仍可保留本轮 proposal-key binding；S03 在业务 commit 前将所有接受的 EvidenceBinding 解析为正式 Evidence ID，再构造持久化 DiagnosisItem、requirement fulfillment 和 `DiagnosisState.evidence_refs[]`。任何 proposal key、draft 或 binding 都不得进入 `state.json`。稳定诊断目标发生实质变化时，Coordinator 返回 `NEW_CASE_REQUIRED`，不得应用 `problem_spec_patch`。

`ProblemSpecPatch` 中未出现的字段保持原值，出现的标量或数组逐字段整体替换，不做列表追加或文本 trim。应用后 Canonical JSON 与当前字段相同的项视为无变化；全部项均相同的 patch 是空语义 patch，不能单独满足“有进展”条件。至少一个允许字段实质变化时 `ProblemSpec.revision + 1`，一次 TransitionPlan 最多增加 1；它不强制等于 DiagnosisState.revision。接受的非空 patch 同时属于诊断语义变化，因此 DiagnosisState.revision 也按本计划增加 1。

`CandidateConclusionDraft` 固定包含 `proposal_key`、`existing_conclusion_id?`、`statement`、`supporting_evidence_bindings[]` 和 `completion_criteria_mapping[]`。草稿中的每个 `CompletionCriterionDraftMapping` 固定为 `{criterion_index,criterion,satisfied,evidence_bindings[],explanation}`；覆盖、顺序和逐字匹配规则与正式 mapping 相同，且形成候选时每项必须 satisfied、至少有一个 binding。每个 `EvidenceBinding` 必须且只能包含 `existing_evidence_id` 或同一 Outcome 的 `evidence_proposal_key`。`existing_conclusion_id` 若存在，只能引用当前 Case 的候选；接受后只沿用 conclusion ID、revision 增加 1，`proposed_by_job_id` 必须更新为本 Outcome.job_id。若不存在，Agent 不得自造 ID，S03 分配新 conclusion ID并从 revision 1 开始，同样令 `proposed_by_job_id=Outcome.job_id`。Coordinator 只接受 candidate proposal key，S03 在提交计划时解析 supporting 和逐项 criterion binding，去重后形成正式 Evidence ID，最后计算 Candidate content hash。

V1 的候选必须带一个可下载结果草稿：`candidate_conclusion_draft` 非 null 时，同一 AgentJobOutcome 的 `proposed_artifact_drafts[]` 必须恰好有一个 `artifact_kind=USER_RESULT`，并使用 `resource_kind=FILE`、`content_type=application/json` 和 `UserResultMetadata`；没有候选时禁止 USER_RESULT 草稿。Runtime 规范化后保持同一 proposal key 和上述约束。任何接受候选的 TransitionPlan 必须同时接受这一个 USER_RESULT Artifact proposal；否则计划无效且不得 commit。

`ReviewAssessment` 固定包含 `candidate_conclusion_id`、`candidate_revision`、`candidate_content_hash`、`reviewed_state_revision`、`reviewed_evidence_refs[]`、`verdict`、`unsupported_findings[]`、`evidence_conflicts[]`、`missing_evidence[]`、`stale_references[]` 和 `recommendation`。前三项必须逐字回显 Job.review_target；后五个说明数组是字符串数组，`recommendation` 是非空字符串。`PASS` 要求四个问题数组全部为空；`NEED_MORE_EVIDENCE` 要求 `missing_evidence[]` 或 `unsupported_findings[]` 至少一个非空；`REJECT` 要求 `unsupported_findings[]`、`evidence_conflicts[]` 或 `stale_references[]` 至少一个非空。任何不满足该矩阵的 ReviewAssessment 都是 `OUTCOME_INVALID`，不能进入 Coordinator。

`ExecutionFailure` 包含 `stage`、`code`、`message`、`retryable` 和 `details: ApplicationErrorDetail[]`。`details` 在 JSON 中始终存在，没有条目时写 `[]`；其字段类型、取值限制和敏感信息禁令与第 6.8 节 `ApplicationError.details[]` 完全相同。`stage` 必须来自 `ExecutionStage`，`code` 必须来自第 9 节。

`EvidenceSourceBinding` 固定包含 `existing_source_ref?` 和 `artifact_proposal_key?`，且必须二选一：前者引用当前 Case 中已经正式存在、与 `source_type` 匹配的来源；后者引用同一 Outcome 的 Artifact proposal key。只有 `source_type=LOGPARSE` 可以使用 `artifact_proposal_key`，且目标 Artifact proposal 必须为 `artifact_kind=LOGPARSE_RUN`。

`AgentEvidenceProposalDraft` 固定包含 `proposal_key`、`source_type`、`source_binding`、`locator`、`summary`、`workspace_relative_path?`、`declared_size?` 和 `declared_sha256?`。`AgentArtifactProposalDraft` 固定包含 `proposal_key`、`artifact_kind`、`name`、`content_type`、`resource_kind`、`workspace_relative_path`、`declared_size?`、`declared_sha256?` 和 `metadata`。

Runtime 规范化后的 `EvidenceProposal` 固定包含 `proposal_key`、`source_type`、`source_binding`、`locator`、`summary`、`content_hash?` 和 `staged_resource_ref?`。`ArtifactProposal` 固定包含 `proposal_key`、`artifact_kind`、`name`、`content_type`、`resource_kind`、`size`、`sha256`、`staged_resource_ref` 和 `metadata`。Runtime 必须逐字保留合法 `source_binding`，不得提前虚构正式 Artifact ID。规范 Proposal 不得保留 Workspace 路径；`staged_resource_ref` 使用第 6.8 节可持久化的 `StagedResourceRef`。

若 `EvidenceProposal.source_binding.artifact_proposal_key` 非空，同一个 `TransitionPlan` 必须同时把该 Evidence 的 `proposal_key` 列入 `accepted_evidence_proposal_keys[]`，并把被引用 key 列入 `accepted_artifact_proposal_keys[]`；不得单独接受其中一方。S03 在业务 commit 前将 binding 解析为正式 Artifact ID。持久化后的 `Evidence` 仍只含已经解析的 `source_ref`，不得保存 `source_binding` 或 proposal key。

`LOGPARSE_RUN` 必须使用 `resource_kind=DIRECTORY`、`content_type=application/vnd.problem-locator.logparse-run+directory`，其 metadata 必须包含 `tree_manifest_sha256`、`logparse_version_ref`、`parse_manifest_relative_path`、`source_attachment_id`、`source_attachment_sha256` 和 `parse_parameters`。`proposal_key` 在单个 Outcome 内唯一。

`parse_parameters` 在 V1 只保存 `product`，不保存其他 CLI 选项，也不保存 Workspace、仓库、配置或解释器绝对路径；未来增加任何解析语义选项必须先修订 S00 `LogparseParseParameters` DTO/Schema 和合同修订号。

### 6.4 TransitionPlan 与 Trigger

```text
ValidatedTrigger
  trigger_id, trigger_type, case_id, expected_case_revision,
  idempotency_key, payload, continuation_resources,
  runtime_bindings_by_job_type{}, occurred_at

TransitionPlan
  accepted_state_delta, target_case_status, job_updates[],
  outcome_disposition?, accepted_evidence_proposal_keys[],
  accepted_artifact_proposal_keys[], accepted_candidate_proposal_key?,
  selected_skill_update?, case_failure_update?, candidate_mutation?,
  next_job_spec?, final_result_target?,
  clear_active_job, reason

SelectedSkillUpdate
  action, value?

CaseFailureUpdate
  action, value?

CandidateMutation
  action, candidate_binding: ReviewTargetBinding,
  expected_status?, target_status, reason?

JobSpec
  job_type, goal, target_state_revision,
  evidence_bindings[], attachment_refs[], previous_outcome_refs[],
  artifact_bindings[], agent_profile_ref, available_skill_refs[],
  skill_ref?, tool_bundle_ref, context_policy_ref,
  output_contract_ref, logparse_tool_ref?, logparse_product?,
  review_target_binding?,
  replacement_for_job_id?, resource_limits

PlannedResourceBinding
  existing_resource_id? | accepted_proposal_key?

ReviewTargetBinding
  existing_candidate_target? | accepted_candidate_proposal_key?

RuntimeBindings
  agent_profile_ref, available_skill_refs[], skill_ref?, tool_bundle_ref,
  context_policy_ref, output_contract_ref, logparse_tool_ref?,
  logparse_product?,
  resource_limits
```

`JobSpec` 是尚未分配 Job ID、尚未把本轮 proposal 解析成正式对象的计划模板，因此禁止携带 `ContextSnapshot`。`target_state_revision` 必须等于应用计划后的语义 revision。S03 解析全部 binding、构造最终正式 DiagnosisState 后，必须调用公共纯 Port `ContextSnapshotProjector.project(target_diagnosis_state)` 生成完整 ContextSnapshot，并令最终 `Job.base_state_revision`、snapshot revision 和 `target_state_revision` 三者相等；这一步是机械投影，不得增加或丢弃业务变化。

`SelectedSkillUpdate` 和 `CaseFailureUpdate` 的 action 只能来自 `FieldUpdateAction`：SET 时 `value` 分别为完整 `VersionedRef` 和 `CaseFailure`，CLEAR 时 value 必须为 null；字段整体为 null 表示保持原值。`CandidateMutation` 的 INSTALL 只能绑定本计划 `accepted_candidate_proposal_key`，`expected_status=null`、`target_status=REVIEWING`；SET_STATUS 只能绑定当前 Job 固定的 existing CandidateTarget，`expected_status=REVIEWING`，target 只能为 ACCEPTED 或 REJECTED。ACCEPTED 必须同时携带相同 `final_result_target`，REJECTED 的 reason 必须非空且 final_result_target=null。

任何把 Case 置为 FAILED 的计划都必须 `case_failure_update=SET`，且 value.code 来自第 9 节；任何非 FAILED 目标状态都不得 SET failure。FAILED Case 必须有 failure，其他 Case 的 failure 必须为 null。ROUTE MATCHED 必须 SET selected skill；REROUTE 必须 CLEAR；其他计划只有在 S01 转换表明确要求时才能改变该字段。Candidate 状态只能通过 `candidate_mutation` 改变，S03 不得从 verdict、result_type 或目标 Case 状态反推一个未写入计划的隐式 mutation。

`PlannedResourceBinding` 两个分支必须且只能出现一个，并通过所在数组确定是 Evidence 还是 Artifact。`accepted_proposal_key` 必须同时位于计划对应的 accepted proposal key 集合。`ReviewTargetBinding` 同样只能选一个分支。`final_result_target` 只能在 REVIEW PASS 计划出现，类型为当前 Job 已固定的 `CandidateTarget`。Coordinator 不分配正式 Evidence、Artifact、Candidate 或 Job ID；S03 在资源发布后把 proposal-key binding 解析成正式 ID，再从完整 `JobSpec` 和投影后的 ContextSnapshot 创建持久化 Job。任何未解析 binding 都禁止进入 Job Schema。

合法 `trigger_type` 为：

```text
CREATE_CASE
ROUTE_OUTCOME
DIAGNOSIS_OUTCOME
REVIEW_OUTCOME
SUBMIT_SUPPLEMENT
CANCEL_CASE
RESUME_INTERRUPTED
EXECUTION_FAILED
ASSET_VERSION_UNAVAILABLE
MARK_OLD_EPOCH_INTERRUPTED
STALE_ACTIVE_OUTCOME
```

`runtime_bindings_by_job_type` 的键只能是 `ROUTE`、`DIAGNOSE`、`REVIEW`，值只能是完整 `RuntimeBindings`；Coordinator 只可消费本次转换实际创建的 Job 类型。每个 Trigger 的 payload 类型固定为：

| Trigger | payload |
|---|---|
| `CREATE_CASE` | `CreateCaseTriggerPayload(problem_spec, initial_user_facts[])` |
| `ROUTE_OUTCOME` | `RouteOutcomeTriggerPayload(job_outcome)`，Outcome 必须属于 ROUTE |
| `DIAGNOSIS_OUTCOME` | `DiagnosisOutcomeTriggerPayload(job_outcome)`，Outcome 必须属于 DIAGNOSE |
| `REVIEW_OUTCOME` | `ReviewOutcomeTriggerPayload(job_outcome)`，Outcome 必须属于 REVIEW |
| `SUBMIT_SUPPLEMENT` | `SubmitSupplementTriggerPayload(user_facts[], ready_attachment_ids[])` |
| `CANCEL_CASE` | `CancelCaseTriggerPayload(reason=USER_CANCEL, active_job_id?)` |
| `RESUME_INTERRUPTED` | `ResumeInterruptedTriggerPayload(source_job_id)` |
| `EXECUTION_FAILED` | `ExecutionFailedTriggerPayload(source_job_id, source_outcome_id?, execution_failure)` |
| `ASSET_VERSION_UNAVAILABLE` | `AssetUnavailableTriggerPayload(source_job_id, missing_refs[])` |
| `MARK_OLD_EPOCH_INTERRUPTED` | `OldEpochTriggerPayload(source_job_id, previous_runtime_epoch, current_runtime_epoch)` |
| `STALE_ACTIVE_OUTCOME` | `StaleActiveOutcomeTriggerPayload(source_job_id, outcome_id, expected_base_state_revision, actual_state_revision)` |

`CreateCaseTriggerPayload.problem_spec` 是已经由 S03 规范化、补入 `revision=1` 的 `ProblemSpec`，`initial_user_facts[]` 是已经分配 item ID、revision 和 `USER_INPUT` provenance 的 `DiagnosisItem`。`SubmitSupplementTriggerPayload.user_facts[]` 同样是 S03 从本次 `inputs{}` 规范化得到的 `DiagnosisItem`，`ready_attachment_ids[]` 是去重且已验证归属/READY/requirement 约束的 Attachment ID；Coordinator 不解析外部 DTO。

Outcome Trigger 的 `occurred_at` 必须逐字等于其规范 `JobOutcome.produced_at`，不能在每次提交尝试重新取时钟。该稳定事件时间同时用于本次接受产生的正式 Evidence.collected_at、Artifact.created_at 和 next Job.created_at；因此确定性 Job ID 对应的完整 `job.json` 在 state commit 故障、进程重启和同 Outcome 重交后字节相同。OutcomeProcessingRecord.processed_at 和 Case.updated_at 可以使用本次处理时钟，不进入 job.json 或 proposal ID/hash。其他 Trigger 的 occurred_at 由 S03 在一次命令尝试开始时从 Clock 取得，并在该命令的全部内部 conflict retry 中保持不变。

Payload 中未列出的字段一律拒绝。Outcome Trigger 只接收 Runtime 规范化且已通过 S03 技术校验的 `JobOutcome`，不接收 AgentJobOutcome。

Coordinator 只接收已经通过技术校验的 Trigger；禁止在 Trigger 中放原始 HTTP/MCP 对象、磁盘路径、进程句柄或未经验证的 Agent JSON。

### 6.5 文件资源

```text
Attachment
  attachment_id, case_id, status, name, content_type,
  declared_size?, declared_sha256?, size?, sha256?, storage_key?,
  created_at, updated_at

Evidence
  evidence_id, case_id, source_type, source_ref, locator,
  summary, collected_at, content_hash?, resource_ref?

Artifact
  artifact_id, case_id, kind, name, content_type, resource_kind, size,
  sha256, storage_key, metadata, created_by_job_id, created_at

ResourceRef
  resource_kind, storage_key, size, sha256

UserFactEvidenceLocator
  kind="USER_FACT", input_name

AttachmentEvidenceLocator
  kind="ATTACHMENT", byte_start?, byte_end_exclusive?

LogparseEvidenceLocator
  kind="LOGPARSE", relative_path,
  start_line?, end_line?, start_time?, end_time?

ToolOutputEvidenceLocator
  kind="TOOL_OUTPUT", relative_path, json_pointer?

PreviousOutcomeEvidenceLocator
  kind="PREVIOUS_OUTCOME", json_pointer

UserResultMetadata
  schema_version=1, format_id="problem-locator-diagnosis-v1", description

UserResultPayload
  schema_version=1, format_id="problem-locator-diagnosis-v1",
  problem_statement, candidate_statement,
  supporting_evidence_bindings[],
  completion_criteria_mapping: CompletionCriterionDraftMapping[]

DiagnosticExportMetadata
  schema_version=1, format_id, description

LogparseParseParameters
  product

LogparseRunMetadata
  tree_manifest_sha256, logparse_version_ref,
  parse_manifest_relative_path, source_attachment_id,
  source_attachment_sha256, parse_parameters: LogparseParseParameters
```

`Evidence.locator` 是由 `source_type` 判别且禁止额外字段的上述 locator union，`source_ref` 类型也固定：USER_FACT 指向当前 Case `user_facts[]` 的 DiagnosisItem ID，ATTACHMENT 指向 READY Attachment ID，LOGPARSE 指向 `kind=LOGPARSE_RUN` 的 Artifact ID，TOOL_OUTPUT 指向 `kind=DIAGNOSTIC_EXPORT` 的 Artifact ID，PREVIOUS_OUTCOME 指向当前 Case 已保存 JobOutcome ID。locator 的 `kind` 必须等于 source_type。`AgentEvidenceProposalDraft`、规范 `EvidenceProposal` 和正式 Evidence 使用同一个 locator union；`EvidenceSourceBinding.existing_source_ref` 也按此映射校验。proposal-key source binding 仅允许 LOGPARSE，并必须指向同 Outcome 的 LOGPARSE_RUN Artifact proposal。

所有 locator 路径都是其正式 Resource 根内的相对 POSIX 路径，不是 Workspace、storage key 或绝对路径。Attachment byte 边界必须同时为空或满足 `0 <= byte_start < byte_end_exclusive`。LOGPARSE 行边界必须同时为空或满足 `1 <= start_line <= end_line`，时间边界必须同时为空或是毫秒精度 UTC RFC 3339 且 start 不晚于 end；`relative_path` 必填。TOOL_OUTPUT 的 `relative_path` 必填；`json_pointer` 为 null 或 RFC 6901 JSON Pointer。PREVIOUS_OUTCOME 的 pointer 是 RFC 6901 字符串，空字符串表示整个规范 JobOutcome。USER_FACT 的 `input_name` 必须等于来源 DiagnosisItem provenance 的非空 input_name。

`Artifact.metadata`、`AgentArtifactProposalDraft.metadata` 和 `ArtifactProposal.metadata` 使用由 ArtifactKind 判别且禁止额外字段的 metadata union：USER_RESULT 只用 `UserResultMetadata`；DIAGNOSTIC_EXPORT 只用 `DiagnosticExportMetadata`，其 `format_id` 匹配 `^[a-z][a-z0-9.-]{0,63}$`；LOGPARSE_RUN 只用 `LogparseRunMetadata`。所有 description 非空且最多 4096 UTF-8 bytes。`LogparseParseParameters` 在 V1 只有非空 `product`，不得保存任意 CLI 选项、路径或环境值。

USER_RESULT 文件内容必须逐字通过 `user-result.schema.json`，使用第 11 节 Canonical JSON bytes 且禁止额外字段。`problem_statement` 等于生产 Job 固定 ProblemSpec.statement；`candidate_statement`、`supporting_evidence_bindings[]` 和完整 `completion_criteria_mapping[]` 分别逐字等于同一 Outcome 的 CandidateConclusionDraft。S04 在 stage 前验证文件字节，S03 还必须从规范 Outcome 重算期望 Canonical bytes 的 size/hash 并与 ArtifactProposal 相等。S03 将 bindings 解析成正式 Evidence 后，UserResultPayload 的每个 binding 必须一一映射到 final Candidate 的对应 Evidence 引用；因此下载内容是候选的确定性表示，而不是未复核的任意旁路文本。

`LOGPARSE_RUN` 是 `resource_kind=DIRECTORY` 的内部 Artifact；资源内容是 logparse 已解析任务目录及其不可变清单。它可以固定进后续 DIAGNOSE Job，但不作为默认用户下载结果展示。

`LogparseRunMetadata.tree_manifest_sha256` 必须等于 Artifact.sha256；`logparse_version_ref` 必须逐字等于生产 Job 固定的 `logparse_tool_ref`；`source_attachment_id/sha256` 必须逐字匹配生产 Job 固定的 READY Attachment；`parse_manifest_relative_path` 是目录根内安全相对 POSIX 路径且目标普通文件存在。后续 Job 的技术依赖闭包必须同时携带该 LOGPARSE_RUN 和源 Attachment。

目录资源清单固定为：

```text
TreeManifest
  version: 1
  entries[]:
    path, size, sha256
```

entries 按相对 POSIX path 升序排列，目录本身不列项；禁止绝对路径、空段、`.`、`..`、反斜杠、符号链接、硬链接、设备文件和命名管道。目录 `size` 是全部文件 size 之和，目录 `sha256` 是 TreeManifest Canonical JSON 的 SHA-256。

S04 与 S07 的 Workspace 接缝只使用以下两个公共 Schema：

```text
WorkspaceInputManifest
  schema_version=1, job_id, case_id, job_type,
  logparse_tool_ref?, logparse_product?, entries[]

WorkspaceAttachmentInput
  input_kind="ATTACHMENT", resource_id, relative_path,
  resource_kind="FILE", size, sha256, content_type

WorkspaceEvidenceInput
  input_kind="EVIDENCE", resource_id, relative_path?,
  resource_kind?, size?, sha256?, source_type, source_ref,
  locator, summary, content_hash?

WorkspaceArtifactInput
  input_kind="ARTIFACT", resource_id, relative_path,
  resource_kind, size, sha256, artifact_kind, name,
  content_type, metadata

WorkspacePreviousOutcomeInput
  input_kind="PREVIOUS_OUTCOME", resource_id, relative_path,
  resource_kind="FILE", size, sha256, source_job_id, result_type

LogparseParseClaim
  schema_version=1, job_id, attachment_id, attachment_sha256,
  artifact_proposal_key, logparse_tool_ref, request_sha256
```

`WorkspaceInputManifest.entries[]` 是由 `input_kind` 判别、禁止额外字段的 union。它必须对 Job 的 `attachment_refs[]`、`evidence_refs[]`、`artifact_refs[]`、`previous_outcome_refs[]` 各生成且只生成一项，分组顺序固定为 ATTACHMENT、EVIDENCE、ARTIFACT、PREVIOUS_OUTCOME，组内逐字保持各 Job 数组顺序；`resource_id` 是对应正式 ID。`job_id/case_id/job_type/logparse_tool_ref/logparse_product` 必须逐字等于 Job，数组内及跨同 kind 不得重复 ID。适配器只能从该只读 manifest 取得 `logparse_product`，不能接受 Agent request 自报或从当前 Skill 目录重新选值。

物化路径固定为 `inputs/attachments/<id>/payload`、`inputs/evidence/<id>/{payload|tree}`、`inputs/artifacts/<id>/{payload|tree}` 和 `inputs/outcomes/<outcome_id>/job_outcome.json`。路径使用 Workspace 相对 POSIX 形式。无 `resource_ref` 的 Evidence 仍必须有 manifest 项，但 `relative_path/resource_kind/size/sha256` 必须同时为 null；有资源时四者必须同时非 null并与 ResourceRef 和实际只读物化字节一致。Artifact metadata 与 Evidence locator 使用本节同一公共 union。Previous outcome 文件是 state 中规范 JobOutcome 的 Canonical JSON bytes，`size/sha256` 对这些实际字节计算。

`inputs/manifest.json` 必须逐字通过 `workspace-input-manifest.schema.json`，并在 Backend 启动前写完、同步且随整个 `inputs/` 设为只读。S04 是唯一生产者；S07 只能消费，不能补字段、扫描替代路径或读取 Repository。`RESOURCE_MANIFEST` Context section 的内容必须逐字等于该文件的 Canonical JSON。

`runtime/tool-state/logparse-parse.claim` 必须逐字通过 `logparse-parse-claim.schema.json`。S07 adapter 在真正启动首次 parse 前以 create-new 语义写入并原子发布；`attachment_id/sha256` 和 `logparse_tool_ref` 来自只读 manifest，`request_sha256` 是 parse-targets request Canonical JSON bytes 的 hash。claim 一旦创建不得修改或删除。固定 manifest 已含任一 LOGPARSE_RUN 时 claim 禁止出现；claim 存在时 Job 启动 manifest 不得含 LOGPARSE_RUN。非失败 Outcome 含 LOGPARSE_RUN proposal 时 claim 必须存在且其 proposal key、源 Attachment、工具 ref 与 request hash 全部匹配。claim 存在但执行以失败结束时允许与实际失败阶段一致的任意 S00 ExecutionFailure，但禁止附带 LOGPARSE_RUN proposal；只有失败点直接来自 logparse adapter 时，code 才限定为 `LOGPARSE_FAILED` 或 `LOGPARSE_OUTPUT_INVALID`。无 claim 时禁止产生 LOGPARSE_RUN proposal。S04 在 Backend 退出后执行这些校验，并拒绝 `runtime/tool-state/` 中任何其他节点。

### 6.6 状态文件 envelope

以下 envelope 属于公共持久化 Schema；S02 独占其磁盘算法：

```text
StateFile
  schema_version, contract_revision, generation, installation_id,
  created_at, updated_at, runtime_epochs[],
  cases{}, idempotency_records{}

CaseAggregate
  case, jobs{}, outcomes{}, outcome_processing_records{},
  execution_failure_records{},
  attachments{}, evidence{}, artifacts{}

IdempotencyRecord
  operation, idempotency_key, request_hash, business_receipt,
  case_id?, created_at

OutcomeProcessingRecord
  outcome_id, job_id, outcome_hash, outcome_file_ref,
  disposition, processed_at, error_code?,
  accepted_evidence_ids[], accepted_artifact_ids[],
  created_job_id?, reason

ExecutionFailureRecord
  failure_id, job_id, runtime_epoch, failure, recorded_at

RuntimeEpochRecord
  runtime_epoch, started_at, recovery_id, recovery_completed_at?
```

`Case.diagnosis_state` 是聚合内唯一 DiagnosisState；`CaseAggregate` 不再保存第二份副本。map 键必须等于对象 ID。
每个 Job、Case.selected_skill_ref 和 Outcome 已保存其实际使用的完整 `VersionedRef`，因此 V1 不在 `state.json` 维护第二份全局资产目录或“当前版本”指针。当前进程可加载性只通过第 7 节 `AssetCatalogPort` 精确检查。

### 6.7 应用命令与回执

外部写命令字段固定为：

```text
CreateCase(idempotency_key, problem_spec: ProblemSpecInput,
           initial_user_facts: UserFactInput[], wait_seconds)
PrepareAttachment(idempotency_key, case_id, expected_case_revision,
                  name, content_type, declared_size?, declared_sha256?)
UploadAttachmentContent(idempotency_key, attachment_id,
                        expected_size, expected_sha256,
                        byte_stream: BinaryStream)
SubmitSupplement(idempotency_key, case_id, expected_case_revision,
                 inputs: map<string,string>, attachment_ids[], wait_seconds)
ResumeCase(idempotency_key, case_id, expected_case_revision, wait_seconds)
CancelCase(idempotency_key, case_id, expected_case_revision)
```

`CreateCase.initial_user_facts[]` 的 name 在数组内唯一。`SubmitSupplement.inputs` 的每个 key 必须是一个当前 OPEN INPUT requirement 的 name，value 是要逐字保存的字符串；`attachment_ids[]` 内唯一且每项必须匹配当前 OPEN ATTACHMENT requirement。两类集合不能同时为空。相同 command 的规范化 hash 保留对象键排序、不改变数组顺序，并排除 `wait_seconds`；服务端生成 ID、revision、provenance 和时间不进入请求 hash。

查询为 `GetCase(case_id, wait_for_job_id?, wait_seconds)`、`ListArtifacts(case_id, include_internal)` 和 `OpenArtifact(case_id, artifact_id)`。

所有应用回执由不可变 `business_receipt` 和本次调用动态计算的 `case_view?`、`wait_timed_out`、`dispatch_pending` 组成。幂等记录只保存业务回执；相同请求重放仍按本次 `wait_seconds` 重新等待并读取最新 CaseView，也会对业务回执所指且仍为 PENDING 的 Job 幂等重投一次。`dispatch_pending=true` 只表示本次 `Dispatcher.submit` 未接受已持久化 Job，不进入幂等 hash 或持久化回执。

`CaseView` 固定包含 `case_id`、`status`、`case_revision`、`diagnosis_state_revision`、`problem_spec`、`user_facts[]`、`confirmed_facts[]`、`open_questions[]`、`pending_requirements[]`、`active_job?`、`selected_skill_ref?`、`final_result?`、`failure?`、`artifacts[]`、`created_at` 和 `updated_at`。`active_job` 是 `JobSummary={job_id,job_type,status,goal,base_state_revision,created_at,started_at?,finished_at?}`；`artifacts[]` 只含 downloadable `ArtifactSummary`，不含 URL；`final_result` 是完整 ACCEPTED CandidateConclusion。它不含 storage key、内部 Artifact、绝对路径或执行日志。客户端需要下载 URL 时显式调用 ListArtifacts，由 S06 生成 ArtifactView。

`UploadDescriptor` 固定包含 `attachment_id`、`method=PUT`、`url`、`required_headers: map<string,string?>`、`max_bytes=2684354560` 和 `expires_at=null`；V1 上传 URL 不过期，但该字段固定保留为 null。`required_headers` 必须恰好有四个大小写固定键：`Idempotency-Key` 的值逐字等于 `attachment_id`，`Content-Type` 等于 PrepareAttachment 已按 ContentType 语法校验并逐字保存的 content_type，`Content-Length` 是已声明 size 的十进制字符串或 null，`X-Content-SHA256` 是已声明的 64 位小写 hash 或 null。null 表示客户端必须在发起 PUT 前从完整本地文件计算并替换，不表示可省略 HTTP header。PUT Adapter 要求 `Idempotency-Key == attachment_id`，并把它作为 `UploadAttachmentContent.idempotency_key`；不得复用 prepare 的 request_id，也不得接受第二个上传幂等键。`Content-Type` header 必须逐字等于 descriptor 值，带参数、大小写变体或任何空白/控制字符都拒绝。内部查询使用 `ArtifactSummary={artifact_id,kind,name,content_type,resource_kind,size,sha256,created_by_job_id,created_at,downloadable}`，不含 storage key。`DIAGNOSTIC_EXPORT` 固定 downloadable=true；`LOGPARSE_RUN` 固定 false；`USER_RESULT` 只有在 Case 已 RESOLVED 且 `created_by_job_id` 等于 `Case.final_result.proposed_by_job_id` 时才为 true，候选被拒绝或仍在复核时不能下载。S06 对可下载 summary 增加 URL 后形成 `ArtifactView={artifact_id,name,content_type,size,sha256,created_at,download_url}`。

内部命令固定为：

```text
ClaimJob(job_id, runtime_epoch)
SubmitJobOutcome(job_outcome, outcome_file_ref)
ReportExecutionInfrastructureFailure(
  job_id, runtime_epoch, failure_id, execution_failure)
InterruptPreviousEpoch(current_runtime_epoch, recovery_id)
```

外部写命令使用调用方幂等键。内部命令以自然键幂等：claim=`{job_id,runtime_epoch}`，Outcome=`{job_id,outcome_id}`，未发布执行失败=`{job_id,failure_id}`，启动恢复=`recovery_id`。

### 6.8 提交与执行回执

```text
StateMutation
  upsert_case?, upsert_runtime_epoch_records[], insert_jobs[],
  job_lifecycle_updates[],
  insert_outcomes[], insert_outcome_processing_records[],
  insert_execution_failure_records[],
  upsert_attachments[], insert_evidence[], insert_artifacts[],
  insert_idempotency_records[]

JobLifecycleUpdate
  job_id, expected_status, target_status,
  started_at?, finished_at?, runtime_epoch?

BusinessReceipt
  operation, primary_resource_id, case_id?, case_revision?,
  job_id?, status

ApplicationResponse
  business_receipt, case_view?, wait_timed_out, dispatch_pending

ApplicationError
  code, message, details[], retryable

ApplicationErrorDetail
  field?, resource_type?, resource_id?, resource_ref?,
  expected?, actual?, limit?, observed?

CaseQueryResponse
  case_view, wait_timed_out

ArtifactListResponse
  artifacts: ArtifactSummary[]

OpenArtifactResult
  artifact: ArtifactSummary, stream: BinaryStream

RuntimeExecutionReceipt
  job_outcome, outcome_file_ref

PublishedJobReceipt
  job, job_file_ref

RuntimeInfrastructureError
  failure_id, execution_failure

JobInstructionPayload
  job_id, job_type, goal, base_state_revision

BoundedContext
  job_id, job_type, body, sections[], utf8_bytes, limit_bytes, body_sha256

ContextSection
  ordinal, kind, source_refs[], required, utf8_bytes, content_sha256

ExecutionFileRef
  relative_key, size, sha256

ExecutionLogSinks
  stdout: AppendOnlyByteSink, stderr: AppendOnlyByteSink,
  combined_limit_bytes

ClaimReceipt
  claimed, job?, failure_applied, failure_code?

OutcomeReceipt
  disposition, case_view

FailureReceipt
  failure_id, disposition, case_view

RecoveryReceipt
  recovery_id, interrupted_job_ids[], pending_job_ids[]

DispatchReceipt
  job_id, accepted, duplicate

CancelReceipt
  job_id, signalled

CommitReceipt
  generation, case_revision?

StagedResourceRef
  staging_id, owner_job_id, proposal_key, resource_kind,
  size, sha256, tree_manifest?

AttachmentStagedRef
  attachment_id, resource_kind=FILE, size, sha256

PlannedResourceTarget
  final_storage_key, resource_kind, size, sha256

CaseResourceUsage
  current_bytes, new_bytes, total_bytes, limit_bytes

MaterializedPath
  path, read_only

AssetAvailabilityReport
  available, missing_refs[]

ResolvedAsset
  ref, asset_kind, root_path

ValidationReport
  valid, schema_version?, contract_revision?, generation?,
  object_counts: StateExportObjectCounts, errors: ValidationIssue[]

ValidationIssue
  code, object_type, object_id?, field_path?, message

ReadinessReport
  ready, checks: ReadinessCheck[], error: ApplicationError?

ReadinessCheck
  name, passed, message?

StateExport
  export_schema_version=1, schema_version, contract_revision,
  source_generation, installation_id, object_counts, state, resources[]

StateExportObjectCounts
  cases, jobs, outcomes, outcome_processing_records,
  execution_failure_records, attachments, evidence, artifacts,
  idempotency_records, runtime_epochs

StateExportResource
  resource_kind, storage_key, size, sha256
```

`StateMutation` 只包含完整合同对象和受条件保护的 lifecycle 更新，不包含回调、脚本、绝对路径或外部 I/O。V1 不提供单对象 delete mutation。

`StagedResourceRef` 是不含路径的持久化 Proposal 暂存引用，只允许出现在 Runtime 规范化的 Agent Proposal、对应 JobOutcome 和 Outcome 审计中；它不等于正式 `ResourceRef`，不进入 CaseView 或其他外部协议，并可在 Outcome 处理完成后按清理规则失效。Agent Proposal 只能使用 `StagedResourceRef`，不得使用 `AttachmentStagedRef`。

`AttachmentStagedRef` 是 Attachment 上传专用暂存引用，只能由 `stage_attachment` 产生，并只绑定 `attachment_id`；它没有 `owner_job_id` 或 `proposal_key`，不得出现在 Agent Proposal、JobOutcome 或 Outcome 审计中。两类 staged ref 都不包含文件系统路径。`MaterializedPath.path` 仅在进程内 Port 之间传递，禁止持久化或进入外部协议。

`ApplicationError.code` 必须来自第 9 节；details 是零个或多个安全结构化条目，`expected/actual` 只能是 JSON string/integer/boolean/null，`limit/observed` 只能是非负整数。`resource_ref` 是完整 VersionedRef。details 不允许自由嵌套对象、路径、日志、命令、凭据或环境变量值。

`JobInstructionPayload` 禁止额外字段，四个字段必须逐字等于当前 Job；它按第 11 节编码后的 Canonical JSON bytes 是唯一合法的 `JOB_INSTRUCTION` section 内容。`goal` 因而始终进入 Backend 的最低必需上下文，不能只存在于 `job.json`。

`ApplicationQueryPort.list_artifacts(include_internal=false)` 只返回 downloadable summary；true 返回当前 Case 全部 ArtifactSummary，仍不得暴露 storage key。`open_artifact` 只允许 downloadable=true 且属于目标 Case 的 Artifact，否则统一返回 `ARTIFACT_NOT_FOUND`；结果 stream 的实际 size/hash 必须已验证等于 summary。S06 只能把 downloadable summary 投影成 ArtifactView。

`ValidationReport.errors[]` 只使用 `ValidationIssue`，按 `{object_type,object_id,field_path,code,message}` 排序；valid=true 时为空。`ReadinessReport.checks[]` 固定按 `CONFIG`、`INSTANCE_LOCK`、`STATE`、`DATA_DIRECTORIES`、`RECOVERY` 排序并使用 `ReadinessCheck`；ready=false 时 error 是 `ApplicationError`，ready=true 时为 null。

`StateExport.object_counts` 使用完整 `StateExportObjectCounts`，`state` 是同一 generation 的完整 StateFile，`resources[]` 是该 state 正式引用资源的去重清单，按 storage_key 升序；目录资源的 size/hash 使用 TreeManifest 语义。StateExport 不含导出时间，因而同一 generation 和同一正式资源集合必须产生字节相同的 Canonical JSON。

### 6.9 开发交接记录

`schemas/v1/handoff.schema.json` 是 S00～S08 开发任务交接文件的唯一机器合同。它不进入运行时 `state.json`，但必须由 S00 合同测试发布并由 S08 门禁使用。顶层字段固定为：

```text
HandoffRecord
  spec_id, title, executor, contract_revision, contract_base_commit,
  branch, head_commit, scope_completed[], changed_files[],
  fixtures_consumed[], fixtures_produced[], tests[],
  dependency_requests[], contract_change_requests[],
  known_limitations[], risks[], integration_notes[],
  forbidden_scope_touched

ExecutorSpec
  model, reasoning_effort

HandoffTestResult
  command, status, summary?

DependencyRequest
  package, version, purpose, license_impact

ContractChangeRequest
  request_id, requesting_spec, current_contract_revision, problem,
  proposed_change, affected_types_or_codes[], affected_specs[],
  compatibility, fixture_and_test_changes[]

FixtureManifest
  schema_version=1, owner_spec, root, files[]

FixtureManifestEntry
  path, purpose, schema_ref?, size, sha256
```

`spec_id` 只能为 S00～S08；executor 必须逐字等于 `{model:"gpt-5.6-sol",reasoning_effort:"ultra"}`。`contract_base_commit` 和 `head_commit` 是 40～64 位小写十六进制 Git object ID；branch 必须以 `codex/` 开头。路径数组使用仓库相对 POSIX path；描述数组使用非空字符串；`HandoffTestResult.status` 只能为 `passed`、`failed` 或 `skipped`，可选 summary 必须为非空字符串。所有顶层字段和嵌套字段都禁止额外字段；空集合写空数组。

Schema 只验证结构；S08 另行验证 commit/parent/branch、白名单、Fixture manifest 和测试结果是否与 Git 事实一致。发布候选要求全部必需测试为 passed、`contract_change_requests=[]` 且 `forbidden_scope_touched=false`；有待处理依赖、skip、限制或风险时由 S08 按组合说明书拒绝或显式路由，不能篡改交接记录。

`schemas/v1/fixture-manifest.schema.json` 是所有 Fixture 子树 manifest 的唯一合同。owner_spec 只能为 S00～S08；root 是 manifest 所在责任子树的仓库相对 POSIX path。entry.path 相对 root，按 Unicode 码点升序且不得重复；只能指向 root 内普通文件，禁止绝对路径、空段、`.`、`..`、反斜杠和链接。purpose 为非空字符串，schema_ref 为适用 JSON Schema 的仓库相对 POSIX path或 null，size/hash 按文件实际字节计算。`fixture-manifest.json` 自身不列入 files，避免自哈希。S08 必须验证 manifest 列表与 root 内除 manifest 自身外的全部普通文件完全相等，不能漏登未跟踪 Fixture。

## 7. 公共 Port

Port 以同步语义定义；具体框架可以在边界外适配异步调用，但不得改变原子性。

```text
Coordinator.plan(snapshot, trigger) -> TransitionPlan
ContextSnapshotProjector.project(target_diagnosis_state) -> ContextSnapshot

StateRepository.read_case(case_id) -> CaseAggregate
StateRepository.read_job(job_id) -> Job
StateRepository.read_artifact(artifact_id) -> Artifact
StateRepository.read_snapshot() -> StateFile
StateRepository.commit(expected_generation, expected_case_revision?, mutation)
  -> CommitReceipt(generation, case_revision?)
StateRepository.validate_all() -> ValidationReport
StateRepository.export_snapshot() -> CanonicalJsonBytes

PublicationCommitGuard.acquire() -> PublicationCommitLease
PublicationCommitLease.release() -> None

ResourceStore.stage_file(
  owner_job_id, proposal_key, stream, expected_size?, expected_sha256?)
  -> StagedResourceRef
ResourceStore.stage_tree(
  owner_job_id, proposal_key, root, expected_manifest_hash?)
  -> StagedResourceRef
ResourceStore.stage_attachment(
  attachment_id, upload_lease: AttachmentUploadLease,
  stream, expected_size?, expected_sha256?)
  -> AttachmentStagedRef
ResourceStore.publish(
  staged_ref: StagedResourceRef | AttachmentStagedRef, final_storage_key)
  -> ResourceRef
ResourceStore.validate_case_capacity(case_id, planned_final_targets[])
  -> CaseResourceUsage
ResourceStore.open_read(resource_ref) -> BinaryStream
ResourceStore.materialize_read_only(resource_ref, destination) -> MaterializedPath
ResourceStore.discard(staged_ref: StagedResourceRef | AttachmentStagedRef) -> None

AssetCatalogPort.check(refs[]) -> AssetAvailabilityReport
AssetCatalogPort.resolve(ref) -> ResolvedAsset
AssetCatalogPort.route_bindings() -> RuntimeBindings
AssetCatalogPort.diagnose_bindings(skill_ref) -> RuntimeBindings
AssetCatalogPort.review_bindings(skill_ref) -> RuntimeBindings

LogparseBrokerFactory.open(
  job, workspace_root, workspace_manifest, cancellation: CancellationSignal)
  -> LogparseBrokerSession

Dispatcher.submit(job_id) -> DispatchReceipt
Dispatcher.cancel(job_id) -> CancelReceipt
StateChangeNotifier.notify(case_id, generation) -> None
StateChangeNotifier.wait_for_change(case_id, after_generation, timeout_seconds)
  -> changed: bool
ExecutionRecordStore.publish_job(job) -> ExecutionFileRef
ExecutionRecordStore.publish_outcome_bytes(job_id, canonical_bytes) -> ExecutionFileRef
ExecutionRecordStore.read_published_job(job_id) -> PublishedJobReceipt?
ExecutionRecordStore.read_published_outcome(job_id) -> RuntimeExecutionReceipt?
ExecutionRecordStore.open_log_sinks(job_id, combined_limit_bytes) -> ExecutionLogSinks
Runtime.execute(job, cancellation: CancellationSignal)
  -> RuntimeExecutionReceipt(job_outcome, outcome_file_ref)
ApplicationCommandPort.execute(command) -> ApplicationResponse
ApplicationQueryPort.get_case(case_id, wait_for_job_id?, wait_seconds)
  -> CaseQueryResponse
ApplicationQueryPort.list_artifacts(case_id, include_internal=false)
  -> ArtifactListResponse
ApplicationQueryPort.open_artifact(case_id, artifact_id)
  -> OpenArtifactResult
JobControlPort.claim_job(job_id, runtime_epoch) -> ClaimReceipt
JobControlPort.submit_outcome(job_outcome, outcome_file_ref) -> OutcomeReceipt
JobControlPort.report_execution_infrastructure_failure(
  job_id, runtime_epoch, failure_id, execution_failure) -> FailureReceipt
JobControlPort.interrupt_previous_epoch(current_runtime_epoch, recovery_id) -> RecoveryReceipt
StateAdminPort.readiness() -> ReadinessReport
StateAdminPort.validate_state() -> ValidationReport
StateAdminPort.export_state() -> CanonicalJsonBytes<StateExport>
Clock.now() -> UtcTimestamp
IdGenerator.new(kind) -> OpaqueId
IdGenerator.derive(kind, stable_parts[]) -> OpaqueId
```

Repository 的 `mutation` 是由 Application Service 根据 TransitionPlan 构造的一次结构化条件提交；它不是可序列化脚本，也不能执行外部 I/O。

除下述流式 Upload 特例外，所有写命令和启动恢复必须从一次 `read_snapshot()` 返回的同一不可变 `StateFile` 读取 generation、幂等记录、Case、Job 和资源元数据；不得拼接多个不同 generation 的独立读取结果。`read_case/read_job/read_artifact` 只服务只读查询或已知 ID 的执行读取。V1 的 JSON 实现可以复制内存快照，未来数据库实现必须提供等价的一致读视图。

`UploadAttachmentContent` 在 per-attachment guard 内先读一次 snapshot，只做不消费 body 的 early validation/idempotency short-circuit；确需上传时，`BinaryStream` 必须单次顺序消费并得到不可变 `AttachmentStagedRef(actual size/hash)`。stage 后才取得短 `PublicationCommitLease`，并在其共享锁内重新 `read_snapshot()`；该 fresh snapshot 是本次 post-stage mutation 的唯一 generation，必须从头重验幂等记录、Case/Attachment 归属与状态、staged 内容、正式 target 和容量，再 publish/adopt 并 commit。若 commit 返回 `REVISION_CONFLICT`，只能复用同一 request hash、同一 completed staged ref 或已发布正式 target 重算整个 post-stage 阶段，绝不得重读、seek 或缓存 body；最终发现相同请求已完成时 discard stage 并复用 receipt，Case/Attachment 已终止或内容冲突时 discard 后返回固定错误。不同 attachment_id 的流可以并行，任一 state generation 在流期间变化都不得使 body 被消费第二次。

`PublicationCommitGuard` 是应用层“正式路径发布/采用到 state commit”与存储清理之间的短临界区 Port。S03 必须在发布或采用任何正式 Resource、预发布 `job.json` 之前取得 lease，持有到对应 `StateRepository.commit` 成功或失败后，并在 `finally` 中释放；暂存大文件、调用 Coordinator、通知、分发和等待不得占用 lease。S02 的 Guard、Repository、ResourceStore、ExecutionRecordStore 和清理器必须共享同一可重入协调锁：正式路径发布/采用和 Job 预发布在该锁内执行，清理只能在同一锁内重验引用后把候选原子移入不可采用的 quarantine。`release` 幂等但 lease 不得跨线程传递。任何实现都不得在“检查无引用”和“隔离候选”之间释放该锁。

`read_published_job` 和 `read_published_outcome` 是执行提交恢复的只读权威入口：最终路径不存在时返回 null；存在时必须读取完整普通文件、验证对应 S00 Schema、Canonical JSON bytes、路径中的 `job_id` 与 DTO 绑定，并从实际字节计算 `ExecutionFileRef`。合法 `job.json` 必须是尚未进入生命周期更新的完整 `status=PENDING` Job。任何截断、额外字段、非规范编码、ID 不符、链接或内容漂移都抛出 `ApplicationError(code=EXECUTION_RECORD_FAILED,retryable=false)`，不得返回部分 DTO。调用者不得直接打开 `jobs/` 路径或从 stdout/stderr 重建结果。

`StateChangeNotifier` 是 S03 实现的进程内等待优化，不是权威状态源。每次成功改变 Case 的 commit 后以返回的 generation 调用 `notify`；调用失败不回滚已提交业务状态。`wait_for_change` 必须在该 Case 已观察到的 generation 大于 `after_generation` 时立即返回 true，否则等待至通知或超时。调用方无论返回值为何都重新读取 Repository，因此通知丢失、进程重启或虚假唤醒不会改变正确性。

`BoundedContext.body` 是实际提交给 Backend 的 UTF-8 文本，`utf8_bytes` 必须等于其编码长度，`body_sha256` 是同一字节串的 SHA-256；`sections[]` 按正文顺序列出且字节数之和必须等于总数。`ExecutionLogSinks.stdout/stderr` 是第 5.2 节 `AppendOnlyByteSink`，共享并强制 `combined_limit_bytes`，不进入状态文件。`ResolvedAsset.root_path` 和 Runtime 的 CancellationSignal 仅是进程内值，禁止持久化或进入外部协议。

`LogparseBrokerFactory` 只在 Job 固定 `logparse_tool_ref/logparse_product` 非 null 时调用。S07 实现的 broker 在服务进程侧持有实际 `LOGPARSE_REPO/CONFIG_PATH/PYTHON` 配置、固定 ResolvedAsset 和子进程启动能力；Agent 侧的 `problem-locator-logparse` 只通过本 Job endpoint/token 发请求。broker 必须复核 token、Job、只读 WorkspaceInputManifest、请求 Schema、parse claim、固定 product 和取消信号，拒绝跨 Job、第二次 parse 或任意 argv。S04 启动 Agent 前必须从继承环境中大小写不敏感地删除原始 `LOGPARSE_REPO`、`LOGPARSE_CONFIG_PATH`、`LOGPARSE_PYTHON` 和既有 `PROBLEM_LOCATOR_LOGPARSE_*`，再只加入 session 返回的两个键；`CLAUDE_COMMAND` 前置赋值若试图设置这些保留键，启动以 `CONFIG_INVALID` 失败。

`validate_case_capacity` 必须在调用方已持有 `PublicationCommitLease`、任何本批 publish 之前执行；`planned_final_targets[]` 按 final_storage_key 升序且 key 唯一，必须全部位于该 Case 的正式 resources 根。V1 的 `current_bytes` 是该根下当前可采用、尚未进入 quarantine 的唯一正式 storage_key 的已验证 ResourceRef.size 之和：包括 state 已引用对象、未确认 durable outbox 的确定性目标和普通 orphan；排除 tmp、Workspace、quarantine、Job、Outcome 和执行日志。同一 key 被多处引用或同 target 重交只计一次，既有相同 kind/size/hash target 的 `new_bytes=0`；不同 key 即使 hash 相同也分别计数。方法对全部 planned targets 原子计算 `total_bytes=current_bytes+Σ新 key size`，超过 5368709120 返回 `RESOURCE_LIMIT_EXCEEDED`，既有 key 内容冲突返回 `RESOURCE_HASH_MISMATCH`；通过后才能在同一 lease/共享协调锁内逐项 publish 并 commit。PrepareAttachment 的检查只属预检、不预留空间；实际 Upload 与 Outcome batch 都必须重新调用本方法。

`IdGenerator.derive` 用于必须跨进程重试稳定的服务端 ID：把 `{"kind":kind,"parts":stable_parts}` 按第 11 节编码，去掉规范末尾 LF 后解码为 UTF-8 name，再计算 RFC 4122 UUIDv5，namespace 固定为标准 URL namespace `6ba7b811-9dad-11d1-80b4-00c04fd430c8`；返回小写连字符 UUID。kind 和每个 part 都是非空字符串。S03 对 Outcome 接受的 Evidence、Artifact、首次 Candidate 和可选下一 Job 分别使用 kind `evidence`、`artifact`、`candidate_conclusion`、`job`，stable_parts 固定为 `[installation_id,case_id,outcome_id,proposal_key]`；next Job 没有 proposal key时最后一项固定为 `next_job`。同一输入在重启后必须得到相同 ID。其他一次性 ID 使用 `new`。

`Runtime.execute` 正常返回时必须带已经发布的 Outcome 文件引用。若唯一失败点正是 `ExecutionRecordStore`，导致 Runtime 连系统生成的失败 Outcome 也无法发布，它抛出公共 `RuntimeInfrastructureError(failure_id, execution_failure)`；其中 `execution_failure.stage` 必须为 `EXECUTION_RECORD`，`execution_failure.code` 必须为 `EXECUTION_RECORD_FAILED`。S05 必须立即调用 `report_execution_infrastructure_failure`。Application Service 只有在 Job 仍为当前 active RUNNING、且 `runtime_epoch` 匹配时才通过 `EXECUTION_FAILED` 计划保存 `ExecutionFailureRecord` 并结束 Job；迟到或重复报告不得覆盖新状态。

S05 对已经发布的 `RuntimeExecutionReceipt` 做提交投递重放，不是再次执行 Agent：同进程保存 Runtime 返回的原对象，进程重启时只通过 `read_published_outcome` 恢复同一规范字节。S03 在 Outcome 处理时用 S00 确定性 next Job ID 调用 `read_published_job`；若存在，只能把其中完整 RuntimeBindings、`created_at` 和最终 DTO 作为本次重放约束，不得从重启后的 Catalog 替换版本。Coordinator 仍必须重算业务计划，最终构造的 Job Canonical bytes 必须逐字等于该已发布 Job；既有记录损坏或合法记录与重算 bytes 不同都作为内部 `EXECUTION_RECORD_FAILED + REJECTED` 结束仍活跃来源 Job，不得覆盖。底层 `publish_job` 的直接同 ID/不同 bytes 调用仍可返回 `IDEMPOTENCY_CONFLICT`，S03 在本 Outcome 恢复路径必须把它归一到上述执行记录失败。不存在已发布 next Job 时才允许从本次固定 Catalog bindings 构造并首次发布。

## 8. revision 与幂等矩阵

| 事件 | `case_revision` | `DiagnosisState.revision` |
|---|---:|---:|
| 创建 Case 和首个 ROUTE Job | 设为 1 | 设为 1 |
| Job `PENDING → RUNNING`、进度或 `runtime_epoch` | +1 | 不变 |
| ROUTE Outcome、selected skill 或 NO_CAPABILITY | +1 | 不变 |
| DIAGNOSE/REVIEW Outcome 或补充资料改变语义投影 | +1 | +1 |
| Outcome 只结束 Job、但 accepted state delta 为空 | +1 | 不变 |
| 相同 Outcome 的 DUPLICATE 重放 | 不变 | 不变 |
| 首次保存 STALE Outcome 审计记录 | +1 | 不变 |
| Attachment 生命周期变化 | +1 | 不变 |
| 取消、失败、中断、恢复创建替代 Job | +1 | 不变，除非同一计划接受语义增量 |
| PENDING Job 的 Resume 唤醒及其幂等记录 | 不变 | 不变 |
| 顶层 RuntimeEpochRecord 建立或完成 | 不适用 | 不适用 |
| 只读查询、等待超时 | 不变 | 不变 |
| 相同幂等键和相同规范化请求重放 | 不变 | 不变 |

active RUNNING Job 的 base-drift Outcome 是上表“首次保存 STALE 审计”和“中断”的合并特例：processing record 与 `STALE_ACTIVE_OUTCOME` 计划必须在一次条件 commit 中完成，`case_revision` 总计只增加 1；不得把两行机械累加为 2。非 active/终态 Job 的首次 STALE 只执行审计行。

每个外部写命令携带 `idempotency_key`；内部写命令使用第 6.7 节自然键。记录值为 `{operation, request_hash, business_receipt, created_at}`。同键同 `request_hash` 复用业务回执，但按本次 `wait_seconds` 重新执行只读等待；同键不同 hash 返回 `IDEMPOTENCY_CONFLICT`。规范化请求不包含 `wait_seconds`，所以改变等待时长不会制造新业务提交。

ProblemSpec.revision 从 CreateCase 的 1 开始，只在第 6.3 节定义的实质非空 ProblemSpecPatch 被接受时增加；同值 patch、被拒绝 patch、STALE/DUPLICATE Outcome 和不含 patch 的其他语义变化都不改变它。

## 9. 错误码全集

V1 只能产生下列公共错误码。新增错误码必须修改 S00、Schema、映射和合同 Fixture，并递增合同修订。

S03 把规范 Outcome 的确定性技术校验失败投影成公共 `ExecutionFailure` 时只使用下列固定映射：

| 条件 | `stage` | `code` | `message` |
|---|---|---|---|
| Outcome 缺失 | `OUTCOME_VALIDATE` | `OUTCOME_MISSING` | `Job outcome validation failed.` |
| 业务 Schema/binding/proposal/metadata/USER_RESULT 或正式化不变量错误 | `OUTCOME_VALIDATE` | `OUTCOME_INVALID` | `Job outcome validation failed.` |
| finalized Outcome 或预发布 next-job 执行记录损坏/冲突 | `EXECUTION_RECORD` | `EXECUTION_RECORD_FAILED` | `Execution record validation failed.` |
| 全批 Case 配额失败 | `RESOURCE_STAGE` | `RESOURCE_LIMIT_EXCEEDED` | `Case resource capacity exceeded.` |
| 确定性正式 target 的 kind/size/hash 与本 Outcome 冲突 | `RESOURCE_STAGE` | `RESOURCE_HASH_MISMATCH` | `Resource publication validation failed.` |

这些 Failure 的 `retryable=false`，`details[]` 只能使用安全 `ApplicationErrorDetail` 并按 `{field,resource_type,resource_id}` 排序；配额失败至少带 `limit=5368709120` 与实际 `observed=CaseResourceUsage.total_bytes`，没有其他安全细节时为 `[]`。确定性拒绝若在 publication lease 内发现，必须先释放 lease再在锁外调用 Coordinator；瞬时 publication/state/revision 投递错误不构造 ExecutionFailure。

| 错误码 | HTTP | 含义 |
|---|---:|---|
| `VALIDATION_ERROR` | 400 | 请求或合同字段无效 |
| `CASE_NOT_FOUND` | 404 | Case 不存在 |
| `JOB_NOT_FOUND` | 404 | Job 不存在 |
| `JOB_CASE_MISMATCH` | 409 | Job 不属于目标 Case |
| `ATTACHMENT_NOT_FOUND` | 404 | Attachment 不存在 |
| `ARTIFACT_NOT_FOUND` | 404 | Artifact 不存在 |
| `RESOURCE_NOT_FOUND` | 500 | 已持久化内部资源缺失 |
| `INVALID_CASE_STATE` | 409 | 当前 Case 状态不接受该命令 |
| `ACTIVE_JOB_EXISTS` | 409 | 同一 Case 已有活跃 Job |
| `NEW_CASE_REQUIRED` | 409 | 补充内容实质改变稳定诊断目标 |
| `REVISION_CONFLICT` | 409 | 条件 revision 或 generation 不匹配 |
| `IDEMPOTENCY_CONFLICT` | 409 | 幂等键已绑定不同请求 |
| `RESOURCE_CASE_MISMATCH` | 409 | 资源不属于目标 Case |
| `ATTACHMENT_NOT_READY` | 409 | Attachment 尚不可消费 |
| `UPLOAD_INCOMPLETE` | 409 | 上传未完整结束 |
| `RESOURCE_HASH_MISMATCH` | 422 | 实际哈希不等于声明值 |
| `RESOURCE_SIZE_MISMATCH` | 422 | 实际大小不等于声明值 |
| `RESOURCE_LIMIT_EXCEEDED` | 413 | 附件、Case、输出或临时空间超限 |
| `PATH_VIOLATION` | 400 | storage key 或物化路径越界 |
| `CONTEXT_LIMIT` | 422 | 必需上下文无法装入角色预算 |
| `ASSET_VERSION_UNAVAILABLE` | 422 | Job 固定运行资产不可加载 |
| `OUTCOME_MISSING` | 422 | 成功进程未产生唯一结果文件 |
| `OUTCOME_INVALID` | 422 | `job_outcome.json` 不符合合同 |
| `BACKEND_START_FAILED` | 500 | Agent 子进程无法启动 |
| `BACKEND_CANCELLED` | 409 | 已接受的取消信号终止 Agent 执行 |
| `BACKEND_TIMEOUT` | 504 | Job 超过 1800 秒 |
| `BACKEND_OUTPUT_LIMIT` | 422 | stdout 与 stderr 合计超过 64 MiB |
| `BACKEND_EXIT_FAILED` | 502 | 子进程非零退出或异常终止 |
| `WORKSPACE_LIMIT` | 422 | Job Workspace 超过 1 GiB |
| `WORKSPACE_PREPARE_FAILED` | 500 | Job Workspace 无法安全创建或物化 |
| `RESOURCE_STAGE_FAILED` | 500 | Runtime 无法把提案内容写入持久化暂存区 |
| `EXECUTION_RECORD_FAILED` | 500 | Job 清单、结果文件或执行日志无法可靠保存 |
| `LOGPARSE_FAILED` | 422 | logparse 合法执行失败 |
| `LOGPARSE_OUTPUT_INVALID` | 422 | logparse 结果清单缺失或无效 |
| `DISPATCH_REJECTED` | 503 | 进程内队列无法接受已持久化 Job |
| `CLAIM_REJECTED` | 409 | Job 已被认领或不再是 PENDING |
| `INSTANCE_LOCKED` | 503 | 数据目录已被另一实例占用 |
| `STATE_CORRUPT` | 503 | `state.json` 无法解析或违反不变量 |
| `STATE_SCHEMA_UNSUPPORTED` | 503 | `schema_version` 或 `contract_revision` 不受支持 |
| `STATE_WRITE_FAILED` | 500 | 原子状态写入失败 |
| `RESOURCE_PUBLISH_FAILED` | 500 | 暂存资源无法正式发布 |
| `CONFIG_INVALID` | 500 | 启动配置缺失或无效 |
| `NO_CAPABILITY` | 422 | Router 在固定候选集中找不到诊断能力 |

只有 `BACKEND_START_FAILED`、`BACKEND_CANCELLED`、`BACKEND_TIMEOUT`、`BACKEND_EXIT_FAILED`、`WORKSPACE_PREPARE_FAILED`、`RESOURCE_STAGE_FAILED`、`EXECUTION_RECORD_FAILED` 和 `LOGPARSE_FAILED` 可以根据具体原因设置 `ExecutionFailure.retryable=true`；其余错误码用作 ExecutionFailure 时必须为 false。`SERVICE_SHUTDOWN` 导致的 `BACKEND_CANCELLED` 为 true，`USER_CANCEL` 为 false；配置/合同错误、格式不支持、确定性资源超限或固定内容损坏均为 false，瞬时 OS/磁盘/进程启动故障才可为 true。

协议或应用边界直接返回的 `ApplicationError.retryable` 也由 S00 冻结：`REVISION_CONFLICT`、`ATTACHMENT_NOT_READY`、`UPLOAD_INCOMPLETE`、`DISPATCH_REJECTED`、`INSTANCE_LOCKED`、`STATE_WRITE_FAILED` 和 `RESOURCE_PUBLISH_FAILED` 固定为 true；若 ApplicationError 是某个 ExecutionFailure 的直接投影，则逐字复制该 failure.retryable；其他错误码固定为 false。true 表示调用方可在刷新状态、满足等待条件或退避后重试同一逻辑操作，不允许绕过幂等键。应用命令本身不因此获得隐藏业务重试；唯二的服务内投递例外是 S05 对同一份已发布 `RuntimeExecutionReceipt` 重调 `submit_outcome`，以及对同一 `failure_id + ExecutionFailure` 重调 `report_execution_infrastructure_failure`。两者都不重新运行 Runtime、不产生 JobAttempt，也不改变既有 bytes/ID。

MCP 错误 `code` 原样使用上表，`message` 面向用户，`details` 不包含绝对路径、环境变量值或原始日志。CLI 退出码固定为：成功 `0`，请求/状态冲突 `2`，配置或状态损坏 `3`，运行失败 `4`。

有限同步等待到期不是错误：返回正常资源状态和 `wait_timed_out=true`，Job 保持原 ID 与状态。

## 10. 固定资源限制

| 项目 | V1 默认值 |
|---|---:|
| Router Context | 128 KiB = 131072 bytes |
| Specialist Context | 200 KiB = 204800 bytes |
| Reviewer Context | 200 KiB = 204800 bytes |
| 单 Attachment | 2.5 GiB = 2684354560 bytes |
| 单 Case 正式文件总量 | 5 GiB = 5368709120 bytes |
| Job wall time | 30 min = 1800 seconds |
| 单 Job stdout + stderr | 64 MiB = 67108864 bytes |
| 单 Job Workspace | 1 GiB = 1073741824 bytes |
| 活跃 Worker 数 | 1 |
| upload 临时文件保留 | 24 h |
| proposal 暂存文件保留 | 24 h |
| Workspace 保留 | 24 h |
| 无元数据引用的正式孤立资源 | 7 d |

V1 不自动删除 Case 的 Attachment、Evidence、Artifact 或已接受的 `LOGPARSE_RUN`；业务文件只允许管理员显式删除整个已终止 Case 的数据。清理任务只能处理表中临时项和确认的孤立项。

## 11. Canonical JSON

用于哈希和幂等的 JSON 固定为 UTF-8、无 BOM、对象键按 Unicode 码点升序、分隔符 `,` 与 `:` 后无空格、字符串按 JSON 标准转义、禁止 NaN/Infinity、文件末尾一个 LF。业务哈希计算时排除 `wait_seconds`、传输 URL 和服务端生成时间；具体排除字段由对应命令 Schema 的 `hash_excluded_fields` 声明。

`schema_version` 固定为 `1`，`contract_revision` 固定为 `v1-contract-r1`。V1 不执行 on-read migration。

## 12. Fake、Fixture 与注入点

共享 Fixture 必须覆盖：

- 最小合法 Case、三类 Job 和四种载荷；
- 每个枚举值和每个错误码；
- 参数组 A、唯一日志附件、分析中途参数 B 的 RPC 超时场景；
- 固定 ID、固定 UTC 时钟、固定 `runtime_epoch`；
- `LOGPARSE_RUN` 清单、SHA-256 和 parse 调用计数；
- WorkspaceInputManifest 与 LogparseParseClaim 的合法/非法、固定 `logparse_product`、排序、额外字段、parse 后 hang→`BACKEND_TIMEOUT` 场景；
- `JOB_INSTRUCTION` 与 `RESOURCE_MANIFEST` 都被计入最低必需上下文，goal/manifest 超预算边界可复现；
- USER_RESULT 的合法 Canonical payload，以及 problem/candidate/binding/mapping 任一不匹配时拒绝；
- Candidate hash 的状态变化稳定性及内容/证据/mapping 变化敏感性；
- Review verdict 与四类问题数组的合法/非法矩阵；
- publish 成功、state commit 故障、进程重启、通过执行记录重放同一 Outcome；确定性 ID、稳定业务时间、完整 job.json、旧 RuntimeBindings 与正式资源字节全部复用；
- 上述五类确定性拒绝的 stage/code/message/retryable/details 排序与 Canonical Failure bytes；
- next job 预发布后 Catalog 升级，重放仍采用旧 job.json，后续 Claim 对确已不可用的旧资产明确返回 `ASSET_VERSION_UNAVAILABLE`；
- 重复幂等请求、冲突请求、旧 revision、迟到 Outcome；
- Attachment UploadDescriptor 四个精确 header、per-attachment guard、body 单次消费/流后 snapshot 重读，以及发布成功/READY commit 失败后的同 hash 采用与异 hash `IDEMPOTENCY_CONFLICT`；
- ContentType 合法边界与大写、参数、空白、控制字符、CRLF、非 ASCII、超长、allow-list 非 Canonical/重复和 PUT header 非逐字相等的拒绝；
- ProblemSpec 同值 patch、不允许的目标变更和合法非空 patch 的独立 revision；
- 合法/非法 S00～S08 handoff、commit 字段格式、嵌套额外字段和测试状态；
- 合法/非法 FixtureManifest、越界/漏登/重复路径和实际 size/hash 漂移；
- 200 KiB 恰好可装入与超 1 byte 的上下文边界；
- 2.5 GiB/5 GiB 用稀疏或计数流模拟；5 GiB 覆盖同 key 重交/多引用、不同 key 同 hash、未确认 outbox、orphan、quarantine 和全批零 partial publish，不提交巨型二进制。

公共 Fake 只实现 Port，不依赖组件内部类：

```text
FakeClock
DeterministicIdGenerator
PureContextSnapshotProjector
InMemoryStateRepository
InMemoryResourceStore
InMemoryPublicationCommitGuard
InMemoryAttachmentUploadGuard
InMemoryBinaryStream
InMemoryCancellationSignal
FakeAssetCatalog
FakeLogparseBrokerFactory
RecordingDispatcher
InMemoryStateChangeNotifier
InMemoryExecutionRecordStore
ScriptedRuntime
ScriptedCoordinator
RecordingApplicationCommand
StubApplicationQuery
StubJobControl
StubStateAdmin
CountingLogparseAdapter
```

所有故障通过注入点触发；产品代码不得识别 Fixture ID 或路径。

## 13. 独立测试命令

```text
python -m pytest tests/contracts -q
```

合同测试必须校验 Schema 快照、contract/fixture manifest 哈希、合法与非法 Fixture、HandoffRecord、Canonical JSON、Port Fake 一致性、错误码全集和资源限制常量。

## 14. 完成标准

- 所有公共类型都能生成 JSON Schema，且禁止额外字段；
- `state.json`、`job.json`、`job_outcome.json` Fixture 均通过 Schema；
- S00～S08 文档模板以固定合法 commit hash 实例化后的 Fixture 均通过 `handoff.schema.json`；非法 hash/字段形状由 Schema 拒绝，错误 parent/branch/Git 事实由 S08 门禁拒绝；
- 每棵共享 Fixture 子树的 manifest 通过 `fixture-manifest.schema.json`，并与磁盘普通文件全集、size 和 hash 一致；
- 所有枚举、错误码、资源限制和 revision 事件只有本说明书一个权威定义；
- S01～S07 所需 Port 全部可由共享 Fake 实现；
- 不包含业务状态转换、磁盘或网络实现；
- `pyproject.toml` 已注册 `problem-locator-logparse = problem_locator.integrations.logparse.cli:main`，但其实现仍由 S07 独占；
- 合同测试命令通过且 manifest 无漂移；
- 文档、Schema 和 Python 常量都标记 `v1-contract-r1`。

## 15. S08 交接格式

未来实现任务以 JSON 交接：

```json
{
  "spec_id": "S00",
  "title": "Contract Freeze and Public Test Specification",
  "executor": {"model": "gpt-5.6-sol", "reasoning_effort": "ultra"},
  "contract_revision": "v1-contract-r1",
  "contract_base_commit": "<contract-base-commit>",
  "branch": "codex/v1-s00-contract-freeze",
  "head_commit": "<head-commit>",
  "scope_completed": [],
  "changed_files": [],
  "fixtures_consumed": [],
  "fixtures_produced": [],
  "tests": [{"command": "python -m pytest tests/contracts -q", "status": "passed"}],
  "dependency_requests": [],
  "contract_change_requests": [],
  "known_limitations": [],
  "risks": [],
  "integration_notes": [],
  "forbidden_scope_touched": false
}
```

以上顶层字段全部必填，不得省略；没有内容的列表写空数组。交接文件固定写入 `handoff/S00.json`。S00 的 `contract_base_commit` 填本任务起始提交；写入该交接文件的 handoff-only 分支头才是后续 S01～S08 使用并加冻结标签的最终合同冻结提交。

## 16. 合同变更请求

S01～S07 不得直接改 S00 路径。发现合同缺口时提交：

```text
request_id
requesting_spec
current_contract_revision
problem
proposed_change
affected_types_or_codes[]
affected_specs[]
compatibility
fixture_and_test_changes[]
```

S00 串行裁决。接受后递增合同修订、更新 manifest 和 Fixture，受影响任务同步新合同后再继续；拒绝时记录理由，不在组件内加私有兼容字段。
