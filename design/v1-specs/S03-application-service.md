# S03 Application Service 说明书

- 状态：V1 详细设计冻结稿
- 说明书编号：S03
- 上游合同：[`v1-contract-r1`](S00-contract-freeze.md)
- 领域决策：[《S01 领域模型与 Coordinator》](S01-domain-coordinator.md)
- 存储边界：[《S02 JSON 状态与文件资源存储》](S02-json-resource-storage.md)
- 组合入口：[《S08 V1 组合说明书》](../v1-composition-spec.md)

## 1. 目标与非目标

S03 是所有应用命令、技术校验、幂等、资源提案发布、TransitionPlan 提交、下一 Job 固定和提交后分发的唯一权威来源。Application Service 是唯一业务写入入口，并通过 S00 `PublicationCommitGuard` 保证正式发布/采用至状态提交不会与孤立对象清理交错。

S03 不决定领域转换，不实现 JSON/文件算法，不运行 Agent，不拥有调度队列，也不解析 MCP/HTTP 协议。

## 2. 独立文件责任区

未来 S03 实现任务唯一允许修改：

```text
src/problem_locator/application/**
tests/unit/application/**
tests/fixtures/components/application/**
handoff/S03.json
```

应用层仅依赖 S00 Port 和 DTO；S01 Coordinator/ContextSnapshotProjector、S02 Repository/ResourceStore/PublicationCommitGuard/AttachmentUploadGuard、S05 Dispatcher 均通过构造函数注入。

## 3. 禁止修改项

- 不修改公共合同和 Coordinator 规则；
- 不读取或拼接 `DATA_ROOT` 内部路径；
- 不解析日志压缩包或调用 logparse；
- 不创建进程、Session 或 Workspace；
- 不让 Adapter、Worker、Runtime 或 Repository 绕过本服务写 Case；
- 不在资源发布前提交其正式引用；
- 不在状态提交前分发 Job；
- 不把 READY Attachment 自动当作 `SubmitSupplement`；
- 不把补充资料注入已经创建的 Job；
- 不将 `STALE` 写成 JobStatus。

## 4. 输入输出契约：应用命令

### 4.1 外部命令

```text
CreateCase
  idempotency_key, problem_spec: ProblemSpecInput,
  initial_user_facts: UserFactInput[], wait_seconds

PrepareAttachment
  idempotency_key, case_id, expected_case_revision, name, content_type,
  declared_size?, declared_sha256?

UploadAttachmentContent
  idempotency_key, attachment_id, expected_size, expected_sha256,
  byte_stream: BinaryStream

SubmitSupplement
  idempotency_key, case_id, expected_case_revision,
  inputs: map<string,string>, attachment_ids[], wait_seconds

GetCase
  case_id, wait_for_job_id?, wait_seconds

ResumeCase
  idempotency_key, case_id, expected_case_revision, wait_seconds

CancelCase
  idempotency_key, case_id, expected_case_revision

ListArtifacts
  case_id, include_internal=false

OpenArtifact
  case_id, artifact_id
```

`ProblemSpecInput`、`UserFactInput`、requirement name/constraints 和 `inputs` 的键值合同完全引用 S00，不在 S03 定义第二套 DTO。`wait_seconds` 范围是 0～30，默认 0；它只控制读取等待，不进入幂等 request hash，不取消或重建 Job。

六个外部写命令实现 S00 `ApplicationCommandPort.execute -> ApplicationResponse`；`GetCase`、`ListArtifacts`、`OpenArtifact` 分别实现 S00 `ApplicationQueryPort` 的三个方法并返回 `CaseQueryResponse`、`ArtifactListResponse`、`OpenArtifactResult`。查询 Port 只读且不接受幂等键；OpenArtifactResult.stream 是已完成归属、公开种类和 size/hash 校验的只读 BinaryStream。

### 4.2 内部命令

```text
ClaimJob(job_id, runtime_epoch)
SubmitJobOutcome(job_outcome, outcome_file_ref)
ReportExecutionInfrastructureFailure(
  job_id, runtime_epoch, failure_id, execution_failure)
InterruptPreviousEpoch(current_runtime_epoch, recovery_id)
```

Worker 只能通过这些内部命令改变 Job 生命周期。内部命令和外部命令使用同一 Repository 条件提交规则。

## 5. 通用命令管线

所有写命令按固定顺序执行：

1. 用 S00 Schema 验证 DTO；
2. 计算排除等待字段和 byte stream 后的 Canonical request hash；上传命令的 hash 必须包含预期 size 与 SHA-256；
3. 调用 `StateRepository.read_snapshot()` 一次取得同一 generation 下的幂等记录、Case 聚合及请求资源；本次尝试不得混用其他 generation 的读取结果；
4. 在该快照中查询 `{operation,idempotency_key}`；
5. 同键同 hash 复用已保存 business receipt，不重复执行业务步骤；若其 `job_id` 仍为 PENDING，则对同一 ID 幂等调用 `Dispatcher.submit` 一次，随后仍按本次 `wait_seconds` 做只读等待和 CaseView 投影；
6. 同键不同 hash 返回 `IDEMPOTENCY_CONFLICT`；
7. 校验 Case/resource 存在、归属、状态、revision 和固定引用；
8. 按 S00 从同一快照构造并技术校验 `ValidatedTrigger.continuation_resources`，再调用 Coordinator；
9. 验证 TransitionPlan 自洽，且所有 Evidence/Artifact/Candidate placeholder 只引用输入中存在并被同一计划接受的 proposal key；selected skill、CaseFailure 和 candidate 状态的每个变化都必须由对应显式 mutation 表达；
10. 取得 `PublicationCommitGuard` lease，必要时在该 lease 内发布或采用资源并预发布不可变 `job.json`；
11. 保持同一 lease，在一次 Repository commit 中执行整个计划及幂等记录；无论发布或 commit 成功或失败都在 `finally` 释放 lease；
12. 释放 lease 后，若 commit 成功则先用返回的 generation 通知 `StateChangeNotifier`，再向 Dispatcher 提交新建或待重发 Job；通知失败不回滚业务提交；
13. 可选等待同一个 Job 到终态或 Case 等待态；
14. 返回保存的结构化响应。

Dispatcher 暂时拒绝时，已提交 Job 保持 `PENDING`，本次 `ApplicationResponse` 仍返回成功并带 `dispatch_pending=true`；该字段不写入幂等 business receipt。S05 会重投 PENDING，原请求重放或 `ResumeCase` 也可以幂等地唤醒同一 Job。不得因为分发失败回滚已经持久化的业务命令或创建第二个 Job。

`UploadAttachmentContent` 是通用管线的单次流式特化：先按 attachment_id 取得 S00 `AttachmentUploadGuard` lease，再在该 guard 内用 header 中的预期 size/hash 读取初始 snapshot、完成无需消费 body 的校验与幂等短路；仅当确需上传时，流式 stage 一次并计算实际值。流结束后保留同一 `AttachmentStagedRef`，才取得短 `PublicationCommitLease`，在共享锁内重新读取权威 snapshot/generation，重验幂等、Case/Attachment 状态和本次内容，再执行 capacity→publish/adopt→commit。若条件提交仍发生 revision conflict，只释放并重新取得短 lease、从新 snapshot 重算 post-stage 阶段，绝不 seek、缓存或再次读取已经消费的 `BinaryStream`；同请求已完成则 discard stage 并复用 receipt，Case/Attachment 已终止或内容冲突则 discard stage 并返回对应错误。guard 一直持有到 commit/短路完成，或 stage 被安全 discard 后，最终在 `finally` 关闭 stream 并释放。不把 2.5 GiB body 放进内存；实际值不符时不发布。网络流读取期间绝不持有 `PublicationCommitLease`。

lease 只覆盖步骤 10～11 的短临界区；流式 stage、Coordinator 计算、通知、分发和同步等待都必须在 lease 外。S03 不得自行实现锁，也不得在 publish 返回后、commit 开始前释放 lease。没有正式对象发布或预发布的写命令仍按同一规则短暂取得 lease 后 commit，使清理观察到的 state generation 与全部应用写入顺序确定。

PrepareAttachment、UploadAttachmentContent 和 PENDING Resume 只改变资源生命周期或队列信号，不产生 S01 Trigger；正常 ClaimJob 只做 `PENDING → RUNNING` 条件转换。CreateCase、SubmitSupplement、CancelCase、已中断 Resume、有效 Outcome、旧 epoch 中断、active stale，以及 Claim/Resume 发现固定资产不可用时必须调用 Coordinator。

构造 Trigger 时，S03 通过 S00 `AssetCatalogPort` 取得且只取得可能的下一 Job 绑定：CreateCase 取 ROUTE；MATCHED Route 取所选 Skill 的 DIAGNOSE；Diagnosis Outcome 按其 result type 提供 ROUTE、DIAGNOSE 或 REVIEW；Review 非 PASS 取同 Skill 的 DIAGNOSE；SubmitSupplement 取 DIAGNOSE。绑定包含 S00 的完整 VersionedRef、可选 `logparse_product` 和限制，Coordinator 只能逐字复制，不得选最新版。INTERRUPTED Resume 逐字段复用源 Job 的运行绑定并先 `check`，不调用 `*_bindings()` 换版本。

`continuation_resources` 只由 S03 构造。它从当前 DiagnosisState.evidence_refs、S00 指定的来源 Job 固定数组、fulfilled Attachment requirement 和 Evidence source 依赖做稳定去重闭包；每个 Evidence、Attachment、Artifact 和 prior Outcome 的 Case 归属、正式状态、kind、size/hash 和反向 metadata 都必须在同一 StateFile snapshot 中验证。ATTACHMENT、LOGPARSE、TOOL_OUTPUT、PREVIOUS_OUTCOME Evidence 的 source 依赖必须分别进入 attachment/artifact/previous-outcome 闭包；LOGPARSE Evidence 还必须解析到 LOGPARSE_RUN Artifact metadata 指定的源 Attachment。缺任一项即 `OUTCOME_INVALID`/`STATE_CORRUPT`，不得给 S01 一个删减视图。SUBMIT_SUPPLEMENT 还必须证明全部 OPEN requirement 来自同一等待来源 Job，并把该 Job 唯一 APPLIED 的等待 Outcome 作为 previous outcome；Outcome Trigger 先加入本次已通过 execution receipt/hash/Schema 校验的 incoming Outcome，再追加 prior Outcome 依赖。incoming Outcome 是唯一尚未存在于 snapshot 的例外，S03 必须把它与引用它的 next Job 在同一 StateMutation 中原子插入；Resume 逐字复制源 Job。该步骤只计算技术引用闭包，不决定 Job 类型、不接受 proposal、不改变业务状态。

所有 Outcome Trigger 的 `occurred_at` 逐字取 JobOutcome.produced_at。S03 用同一值填本轮正式 Evidence.collected_at、Artifact.created_at 和 next Job.created_at，不得在重交时再次 `Clock.now()`；processing record 和 Case.updated_at 才使用本次处理时间。

Outcome 技术校验通过后、为其选择下一 Job bindings 之前，S03 必须先按 S00 `[installation_id,case_id,outcome_id,"next_job"]` 派生 prospective next Job ID，并调用 `ExecutionRecordStore.read_published_job`。若返回 null，才从当前 Catalog 取得本次可能使用的 bindings。若已有合法记录，先验证其 ID、Case、`status=PENDING`、`created_at=Outcome.produced_at` 且未带 started/finished/runtime epoch，再从该 Job 机械提取完整 RuntimeBindings 作为 Trigger 唯一候选，不调用当前 Catalog 的 binding/check 方法；Coordinator 产出并完成正式化后，完整 Job Canonical bytes 必须与记录逐字相同。这样 state commit 前已预发布的旧资产版本在重启后不会被新 Catalog 替换；状态提交成功后，S05 正常 Claim 才检查旧 VersionedRef 当前是否仍可加载。

## 6. 命令语义

### 6.1 CreateCase

- 服务端分配 Case ID、Trigger ID、ROUTE Job ID 和时间；
- 按 S00 验证 ProblemSpecInput 有稳定问题主体、目标、范围和完成条件，补入 `revision=1` 形成 ProblemSpec；
- 为每个 `initial_user_facts[]` 项分配 DiagnosisItem ID，逐字保存 value，并写入 `{source_type=USER_INPUT,source_ref=trigger_id,input_name=name}` provenance；这些条目进入 `user_facts`，不是 `confirmed_facts`；
- 调用 S01 `CREATE_CASE`，一次 commit 写入 Case、DiagnosisState、ROUTE Job 和幂等响应；
- Case 创建成功时为 `RUNNING`，`case_revision=1`、`DiagnosisState.revision=1`；
- 同一幂等请求永远返回同一 `case_id` 和首个 `job_id`。

### 6.2 PrepareAttachment

- Case 必须存在且不是终态；
- `expected_case_revision` 必须匹配；
- `content_type` 必须逐字满足 S00 Canonical ContentType；大写、参数、空白、控制字符、CRLF、非 ASCII 或超长都以 `VALIDATION_ERROR` 零修改拒绝，不做规范化；
- 在不改变 DiagnosisState 的情况下创建 `UPLOADING` Attachment；
- 若声明大小超过 2.5 GiB，或用 S00 Case usage 语义做 advisory 预检时 `current + declared` 已超 5 GiB，返回 `RESOURCE_LIMIT_EXCEEDED`；该预检不预留空间，未声明大小时只检查当前 usage；
- 返回 `attachment_id` 和供 S06 生成上传描述所需的结构化信息，不返回 Shell 命令；
- 增加 `case_revision`，不创建 Job。

### 6.3 UploadAttachmentContent

- 进入 per-attachment guard 后先读取 snapshot；Attachment 必须属于当前 Case 且为 `UPLOADING`，或已经 READY 且 header 中的 size/hash 与首次回执相同；READY 幂等短路不消费 body，Adapter 仍负责安全关闭它；
- 将流和当前 `AttachmentUploadLease` 交给 `ResourceStore.stage_attachment(attachment_id, upload_lease, stream, expected_size, expected_sha256)`，取得 Attachment 专用 `AttachmentStagedRef`，并校验实际 size/hash；不得调用只接受 `owner_job_id/proposal_key` 的 Proposal `stage_file`；
- stage 完成后取得 publication lease，并在其中重新读取最新 snapshot/generation、重验自然幂等键、Case/Attachment 状态与 staged size/hash；在任何 publish 前以该 Attachment 的唯一 `PlannedResourceTarget` 调用 `validate_case_capacity`。超过 5 GiB 返回 `RESOURCE_LIMIT_EXCEEDED` 且不发布，成功后在同一 lease 内正式发布不可变资源并条件 commit Attachment→`READY`；
- 其他 Attachment、Case 命令或 Worker 在长流期间推进全局 generation 是正常情况；它们不能迫使本次 body 重读。条件冲突只以同一 completed `AttachmentStagedRef` 重做“新 snapshot→重验→capacity→publish/adopt→commit”短阶段，最多采用通用三次重算边界；若正式目标已在前一轮发布则按相同 hash 采用；
- 相同 idempotency key、相同内容重放返回原结果；
- 已 READY 且 hash 相同可返回原结果；内容不同返回 `IDEMPOTENCY_CONFLICT`；
- 正式 target 已发布但 state commit 失败时仍保持 UPLOADING 且没有幂等记录；同 attachment_id、同 size/hash 重试把既有 target 作为 capacity delta=0 幂等 finalize 后完成 commit，不要求旧 stage；不同 size/hash 在进入 publish 前固定映射 `IDEMPOTENCY_CONFLICT`，绝不以 `RESOURCE_HASH_MISMATCH` 暴露底层路径冲突或覆盖首个 target；
- 发布成功但 commit 失败的 Attachment target 由同 key 重试优先采用；超过孤立期限且仍无 finalized outbox/state 引用时才由 S02 清理；
- 增加 `case_revision`，不增加 DiagnosisState.revision，不创建 Job。

Attachment READY 只表示后续可以引用，绝不触发诊断推进。

同一 attachment 的并发 PUT 必须在 guard 上等待：相同 size/hash 的后到请求在首个 commit 后读取并复用同一业务回执，不再打开/覆盖 stage；不同 size/hash 使用同一个自然幂等键而返回 `IDEMPOTENCY_CONFLICT`。首个请求若在 commit 前失败，必须安全关闭流；只有确定不再重算 post-stage 时才 discard 其 completed/未完成 stage 并释放 guard，后到请求随后可以从新的 snapshot 开始。进程退出释放内存 guard，重启后仍由 Attachment 状态、幂等记录和 stage 完成标记决定，不把锁本身持久化。

Upload 不携带 expected Case revision，因为长时间流式传输期间其他附件生命周期可以合法前进；它用 Attachment 当前状态、预期 size/hash 和最终 Repository 条件提交防止覆盖。该例外不允许修改任何已创建 Job 的固定输入。

### 6.4 SubmitSupplement

技术校验必须在 Coordinator 前完成：

- Case 是 `WAITING_INPUT` 或 `WAITING_ATTACHMENT`；
- `expected_case_revision` 必须匹配；
- `inputs` 与 `attachment_ids[]` 不能同时为空；每个 inputs key 恰好命中一个 OPEN INPUT requirement，value 逐字保存且符合其 STRING/bytes/pattern/allowed-values 约束；
- 每个 Attachment 存在、属于该 Case、为 READY、未重复，并以 Canonical ContentType 逐字 membership 符合唯一 OPEN ATTACHMENT requirement 的 allow-list 和计数约束；
- 请求不包含当前要求以外的隐式目标替换。

对可能改变稳定诊断目标的输入，Application Service 只标记为 `target_change_candidate` 交给 Coordinator；Coordinator 返回 `NEW_CASE_REQUIRED` 时整次命令零修改。

S03 为每个接受的 inputs 条目分配 DiagnosisItem ID，以本次 Trigger ID 和 requirement name 写入 USER_INPUT provenance，并把 item ID 放入对应 requirement 的 `fulfilled_by_refs[]`；Attachment ID 放入唯一附件 requirement 的同名数组。两类引用在构造 Trigger 前均已去重和验证。

分批补充也执行一次完整 TransitionPlan 和 commit：资料立即保存；仍有 INPUT requirement 时保持 `WAITING_INPUT`，否则仍有 ATTACHMENT requirement 时进入 `WAITING_ATTACHMENT`，两类均满足时才创建恰好一个 DIAGNOSE Job。重复请求通过幂等记录返回同一结果，不重复 revision 或 Job。

### 6.5 GetCase 与有限等待

查询响应包含 Case 当前投影、未决要求、active Job 摘要、公开 Artifact 元数据和 `wait_timed_out`。

- 未给 `wait_for_job_id` 时，可以等待查询开始时观察到的 active Job；
- 指定 Job 必须属于该 Case；
- 等待先记录当前 snapshot generation，再调用 S00 `StateChangeNotifier.wait_for_change(case_id, generation, remaining_seconds)`；无论收到通知、虚假唤醒还是超时，都重新读取 Repository 后才判断结果；
- 30 秒内完成、进入等待态或终态即返回；
- 超时正常返回 `wait_timed_out=true`；
- 断开连接不影响 Job；
- 查询不写 idempotency、revision 或 generation。

`wait_for_job_id` 存在但属于其他 Case 时返回 `JOB_CASE_MISMATCH`。

### 6.6 ResumeCase

- Case 有 PENDING active Job：向 Dispatcher 幂等提交同一 `job_id`，不创建新 Job、不改变固定输入；真正执行前仍由 ClaimJob 通过 S00 `AssetCatalogPort` 检查全部固定资产；
- Case 为 `INTERRUPTED`：找到最近且尚无替代项的 INTERRUPTED Job，校验所有固定资产仍可用，再调用 `RESUME_INTERRUPTED`；
- Coordinator 创建同 `job_type` 的唯一替代 Job，设置 `replacement_for_job_id`；
- REVIEW 必须替换为 REVIEW；
- 固定资产缺失时改用 `ASSET_VERSION_UNAVAILABLE` Trigger，Case→FAILED；
- 等待资料返回 `INVALID_CASE_STATE` 并提示使用 SubmitSupplement；
- 终态返回 `INVALID_CASE_STATE`，已存在替代或 active Job 时返回当前结果而不重复创建。

S05 在启动时自动重投 PENDING，并把旧代次 RUNNING 变为 INTERRUPTED；ResumeCase 是人工恢复 INTERRUPTED 或主动唤醒 PENDING 的显式入口。

PENDING 唤醒仍要在 `state.json` 保存 ResumeCase 幂等 business receipt，因此 global generation 增加；它不改变 Case/Job 生命周期，`case_revision` 和 DiagnosisState.revision 均不增加。

### 6.7 CancelCase

- 非终态 Case 调用 `CANCEL_CASE`；
- PENDING/RUNNING Job 在同一 commit 中变为 CANCELLED 并清除 active；
- commit 后向 S05 发送取消信号，信号失败不回滚业务状态；
- Runtime 的迟到 Outcome 按 STALE 保存，不合并；
- 已 CANCELLED 的同请求重放返回原响应；
- RESOLVED/FAILED 返回 `INVALID_CASE_STATE`。

### 6.8 ListArtifacts 与 OpenArtifact

- Case 和 Artifact 必须存在且归属匹配；
- ListArtifacts 按 S00 计算 downloadable：DIAGNOSTIC_EXPORT 恒为 true，LOGPARSE_RUN 恒为 false，USER_RESULT 仅在 Case.RESOLVED 且其 created_by_job_id 等于 final_result.proposed_by_job_id 时为 true；默认只返回 true，不列出内部或被拒候选的结果；
- `include_internal=true` 返回不含 storage key 的内部 ArtifactSummary，只供受控运维/测试调用，S06 不向普通 Client Access Skill 暴露；
- OpenArtifact 只允许当前计算 `downloadable=true` 的 Artifact，其他种类或尚未通过复核的 USER_RESULT 统一返回 `ARTIFACT_NOT_FOUND`；成功返回 ArtifactSummary 和只读 BinaryStream，不返回 `storage_key` 或绝对路径；
- 下载前 ResourceStore 再校验 size/hash；缺失是 `RESOURCE_NOT_FOUND`。

## 7. Job 认领与 Outcome 处理

### 7.1 ClaimJob

条件为 Job 当前 `PENDING`、是 Case.active_job_id、Case 状态与 job_type 匹配。成功的一次 commit：

- Job→`RUNNING`；
- 写 `started_at` 和当前 `runtime_epoch`；
- `case_revision + 1`；
- DiagnosisState.revision 不变。

在提交 `PENDING → RUNNING` 前，Application Service 必须把 Job 中的 Agent Profile、Skill/Skill 摘要、Tool Bundle、Context Policy、Output Contract 和可选 logparse `VersionedRef` 一次性交给 S00 `AssetCatalogPort.check`。全部可用才允许正常认领。

若任一固定资产不可用，Application Service 以该 Job 和缺失 refs 构造 `ASSET_VERSION_UNAVAILABLE` Trigger；Coordinator 计划把 PENDING Job 与 Case 置为 FAILED。条件 commit 成功后返回 `ClaimReceipt(claimed=false,job=null,failure_applied=true,failure_code=ASSET_VERSION_UNAVAILABLE)`，Worker 不执行 Runtime。这个失败路径使 `case_revision + 1`、DiagnosisState.revision 不变，且不创建 JobOutcome。

重复、状态不匹配或竞争认领返回 `CLAIM_REJECTED`，不改变状态。Worker 收到拒绝后不得执行 Runtime。

### 7.2 技术校验

`SubmitJobOutcome` 在 Coordinator 前逐项校验：

1. 用 `ExecutionRecordStore.read_published_outcome(job_id)` 读取最终执行记录；null 映射 `OUTCOME_MISSING`，损坏由该 Port 映射 `EXECUTION_RECORD_FAILED`；
2. 读取结果中的规范 JobOutcome Canonical bytes 与计算出的 `outcome_file_ref` 必须逐字等于 Worker 传入的 `RuntimeExecutionReceipt`；后续校验只使用读取结果，不信任 Workspace 或内存副本；
3. Outcome Schema、job_type/result_type/payload 组合以及 outcome/job/case ID 绑定合法；
4. Job 当前为 RUNNING；
5. Job 是 Case.active_job_id；
6. `base_state_revision` 同时等于 Job 和当前 DiagnosisState revision；
7. consumed Evidence 是 Job 固定 Evidence 子集；
8. Attachment、Artifact、previous Outcome 引用都属于当前 Case 且已固定到 Job；
9. Skill/工具/输出合同引用没有被 Agent 改写；
10. state delta、finding 和 candidate 中每个 Evidence binding 都来自 Job 固定 Evidence 或本 Outcome 提案，Agent Outcome 的 `add_user_facts[]` 与 `fulfill_requirements[]` 为空；
11. candidate 的 completion-criteria draft mapping 按索引恰好覆盖当前 ProblemSpec，每项原文逐字匹配、satisfied 且至少有一个合法 Evidence binding；
12. proposal key 唯一，`StagedResourceRef` 属于当前 Job/proposal，size/hash 与 Outcome 一致；首次发布要求暂存 Resource 存在，确定性正式目标已存在时允许由 `ResourceStore.publish` 在协调锁内校验并采用，不得仅因 stage 已被首轮发布移动而提前拒绝重交；
13. 每个 Evidence proposal 的 `source_binding`、`source_type→source_ref` 和 discriminated locator 严格符合 S00；只有 `source_type=LOGPARSE` 可用 `artifact_proposal_key`，且该 key 指向同一 Outcome 的 `LOGPARSE_RUN` Artifact proposal；
14. 每个 Artifact proposal 的 kind/resource/content-type/metadata union 合法；LOGPARSE metadata 的工具、`parse_parameters.product` 与源 Attachment 必须逐字匹配 Job 固定 `logparse_tool_ref/logparse_product` 和输入资源；
15. 有 candidate 时恰好一个合法 USER_RESULT proposal，无 candidate 时为零；S03 从 Job ProblemSpec 和规范 Outcome 的 CandidateConclusionDraft 重建 S00 `UserResultPayload` Canonical bytes，要求其实际 size/hash 逐字等于 USER_RESULT ArtifactProposal，再证明每个 draft binding 在本轮正式化后与最终 Candidate 的 Evidence 引用一一对应；completion mapping 与 candidate hash preimage 可确定；
16. REVIEW 的候选绑定、reviewed Evidence 和 verdict 问题数组矩阵完全匹配固定目标。

结构错误返回 S00 对应错误码并产生 `EXECUTION_FAILED` Trigger；归属、active Job 或 base revision 已过期时走 7.3，而不是让 Coordinator处理脏输入。ReviewAssessment 与其 Job 自身固定 target 不一致属于伪造/损坏，映射 `OUTCOME_INVALID + REJECTED`；只有 Job 固定 target 已被当前 Case 的合法新版本替代才是 `STALE`。

### 7.3 DUPLICATE、STALE 与 REJECTED

- 同一 `outcome_id`、相同 Canonical hash 已处理：`DUPLICATE`，返回原处理响应，revision 不变；
- 同一 `outcome_id`、不同 hash：`IDEMPOTENCY_CONFLICT`；
- Job 不再 active、已取消/中断/终止、base revision 或 review target 不匹配：`STALE`；
- Schema、引用、归属或提案完整性无效：`REJECTED`，并按失败分类结束仍活跃的 Job。

`STALE` 会保存只读 Outcome processing record 和原文件 hash，DiagnosisState.revision 不变。若该 Job 仍是 active RUNNING 但仅 base revision 不匹配，Application Service 将 `STALE_ACTIVE_OUTCOME` Trigger 交给 Coordinator，并在同一次 Repository 条件 commit 中同时保存 processing record、应用 Job/Case→INTERRUPTED 计划且令 `case_revision` 总计只 +1；不得先提交审计 +1 再提交中断 +1。若 Job 已非 active 或终态，只保存审计记录并令 `case_revision +1`，不改变 Case 状态、active_job_id 或 JobStatus。

`REJECTED` 不应用任何 Agent delta。仍活跃时通过 S01 `EXECUTION_FAILED` 生成 FAILED/INTERRUPTED 计划；已终态时只保存处理记录。

技术校验映射固定为：

| 条件 | 错误码 | disposition/动作 |
|---|---|---|
| 最终 Outcome 文件不存在 | `OUTCOME_MISSING` | `REJECTED`；活跃 Job 走 `EXECUTION_FAILED` |
| 最终 Outcome 文件不可读、非普通文件、非 Canonical JSON、Schema/路径 job_id 损坏 | `EXECUTION_RECORD_FAILED` | `REJECTED`；活跃 Job 走 `EXECUTION_FAILED` |
| 已可靠读取的 Outcome 与 Worker receipt hash/bytes 不同，或 job/case/type/result 业务绑定错误 | `OUTCOME_INVALID` | `REJECTED`；活跃 Job 走 `EXECUTION_FAILED` |
| Job 不存在 | `JOB_NOT_FOUND` | 拒绝调用，不创建 processing record |
| Job 已非 RUNNING、非 active 或 Case 已前进 | 无业务错误 | `STALE` 审计 |
| base revision 与当前状态不符 | 无业务错误 | `STALE`；仍 active 时走 `STALE_ACTIVE_OUTCOME` |
| Outcome 引用未固定到 Job 的资源或其他 Case 资源 | `OUTCOME_INVALID` | `REJECTED` |
| proposal key、暂存引用、声明 size 或 hash 无效 | `OUTCOME_INVALID` | `REJECTED` |
| Evidence locator、source_ref 类型或 Artifact metadata 判别分支无效 | `OUTCOME_INVALID` | `REJECTED` |
| Candidate 与 USER_RESULT 缺失、重复，或 UserResultPayload 的 Canonical bytes/语义/hash 不匹配 | `OUTCOME_INVALID` | `REJECTED` |
| Evidence `source_binding` 非二选一、类型不允许或 Artifact proposal key 无法在同一 Outcome 解析 | `OUTCOME_INVALID` | `REJECTED` |
| ReviewAssessment 没有逐字回显 Job 固定 target | `OUTCOME_INVALID` | `REJECTED` |
| Review verdict 与问题数组矩阵不符 | `OUTCOME_INVALID` | `REJECTED` |
| Job 固定 review target 已被合法新候选替代 | 无业务错误 | `STALE` 审计 |
| PENDING Job 在 Claim 前发现固定运行资产不可加载 | `ASSET_VERSION_UNAVAILABLE` | Coordinator 直接使 Job/Case FAILED，不创建 Outcome |
| RUNNING Job 在 Runtime 内发现固定运行资产不可加载 | `ASSET_VERSION_UNAVAILABLE` | Runtime 形成 FAILED Outcome，再走 `EXECUTION_FAILED` |
| 接受 proposal 的全批正式目标使 Case usage 超过 5 GiB | `RESOURCE_LIMIT_EXCEEDED` | `REJECTED`；移动零个 stage，不映射成发布故障 |
| 正式资源发布失败 | `RESOURCE_PUBLISH_FAILED` | 不 commit、不创建 disposition，允许相同 Outcome 重交 |
| 正式目标已存在但实际 size/hash 与本 Outcome 冲突 | `RESOURCE_HASH_MISMATCH` | `REJECTED`；不得改写、采用或笼统映射成发布故障 |
| 确定性 next job 的既有记录损坏，或合法记录与本次重算 DTO bytes 不同 | `EXECUTION_RECORD_FAILED` | `REJECTED`；底层 publish 的冲突在本恢复路径归一到该码，不得换 Catalog 版本或覆盖 |
| state 原子写失败 | `STATE_WRITE_FAILED` | 不返回成功；重读最后提交状态后允许重交 |

所有由本节技术校验生成并交给 `EXECUTION_FAILED` Trigger 的 `ExecutionFailure` 必须逐字采用 S00 固定矩阵：Outcome 缺失使用 `OUTCOME_VALIDATE/OUTCOME_MISSING/"Job outcome validation failed."`；业务 Schema/binding/proposal/metadata/USER_RESULT 及第 7.4 节正式化不变量使用 `OUTCOME_VALIDATE/OUTCOME_INVALID/"Job outcome validation failed."`；已 finalized 的 Outcome 或 next-job 执行记录损坏/冲突使用 `EXECUTION_RECORD/EXECUTION_RECORD_FAILED/"Execution record validation failed."`；全批 Case 配额失败使用 `RESOURCE_STAGE/RESOURCE_LIMIT_EXCEEDED/"Case resource capacity exceeded."`；确定性正式 target 的 kind/size/hash 冲突使用 `RESOURCE_STAGE/RESOURCE_HASH_MISMATCH/"Resource publication validation failed."`。全部 `retryable=false`，`details[]` 只填 S00 `ApplicationErrorDetail` 允许的 field/resource/expected/actual/limit/observed，并按 `{field,resource_type,resource_id}` 排序；配额失败至少填 `{limit:5368709120,observed:<CaseResourceUsage.total_bytes>}`，没有其他安全细节时为 `[]`。不得放入路径、原始 JSON、日志、命令或异常文本。`RESOURCE_PUBLISH_FAILED`、`STATE_WRITE_FAILED` 和 `REVISION_CONFLICT` 是 pre-commit 投递错误，不构造该 Failure、不进入 Coordinator。

### 7.4 接受提案并提交

对技术校验通过的 Outcome：

1. 构造对应 Validated Trigger；
2. 调用 Coordinator 得到计划；
3. 验证每个被接受 Evidence 的 `EvidenceSourceBinding`：使用 `artifact_proposal_key` 时，被引用的 `LOGPARSE_RUN` key 必须也在本计划的 `accepted_artifact_proposal_keys`；未接受 Artifact 时不得接受或保存该 Evidence；
4. 只为 `accepted_evidence_proposal_keys`、`accepted_artifact_proposal_keys` 和首次出现的 `accepted_candidate_proposal_key` 建立正式 ID；全部按 S00 `IdGenerator.derive` 和 `[installation_id,case_id,outcome_id,proposal_key]` 生成，existing candidate 沿用原 ID；可选下一 Job 用 proposal_key 部分 `next_job` 确定性生成；正式对象和 next Job 的业务创建时间统一使用稳定 Trigger.occurred_at；
5. 取得 publication lease，先把本轮全部需发布/采用的 Evidence/Artifact（包括 USER_RESULT 与 LOGPARSE_RUN）转换为按 final_storage_key 排序且去重的 `PlannedResourceTarget[]`，调用 `validate_case_capacity` 一次完成全批 5 GiB 检查。若超限，移动零个 stage并立即释放 lease；随后按上一节构造固定 `RESOURCE_LIMIT_EXCEEDED` ExecutionFailure，在锁外重新读取/条件校验并调用 Coordinator：Job 仍 active 时短暂重新取得 lease，只提交原 Outcome、processing=`REJECTED` 和 `EXECUTION_FAILED` 计划；若状态已前进则按 STALE 路径提交。任何 conflict retry 都重新读状态，绝不在 lease 内调用 Coordinator。容量通过后才在原 lease 内逐项发布或采用并记录 proposal key→正式 ID。正式 Artifact 必须逐字段保留经校验的 discriminated metadata，尤其是 `LOGPARSE_RUN` 的 manifest/version/source/parse 参数；接受 Candidate 时其唯一 USER_RESULT 必须在同批发布，且第 7.2 节重算的 payload bindings 必须与下一步解析出的正式 Evidence 一一对应；
6. 在 commit 前解析每个 Evidence 的 `source_binding`：`existing_source_ref` 复核归属后沿用，`artifact_proposal_key` 替换为第 4 步分配的正式 Artifact ID；正式 `Evidence` 只写解析后的 `source_ref`，不持久化 binding 或 proposal key；
7. 解析 accepted state delta、finding、Candidate supporting 和逐项 completion-criteria 中的 EvidenceBinding，按 S00 的覆盖/顺序规则构造正式 DiagnosisItem/状态 Evidence 引用及 CandidateConclusion，并只按 S00 固定 preimage 计算 content hash；existing candidate 只沿用 ID、revision+1，所有新 revision 都把 `proposed_by_job_id` 更新为本 Outcome.job_id；
8. 用正式 ID 解析计划和 `next_job_spec` 中全部 Evidence/Artifact/ReviewTarget placeholder；逐字应用 `selected_skill_update`、`case_failure_update` 和 `candidate_mutation`，REVIEW PASS 时校验 `final_result_target` 等于该 Job 固定 CandidateTarget，并把对应完整候选写入 Case.final_result；S03 不得从 result_type/verdict/目标状态补出计划未声明的业务 mutation，任何未解析 binding 都使整次处理失败且不 commit；
9. 若存在 next_job_spec，把计划应用后的最终正式 DiagnosisState 交给注入的 `ContextSnapshotProjector.project`，验证投影 revision 等于 `target_state_revision`，再与已解析固定引用、确定性 Job ID 和本次选定的 RuntimeBindings 构造完整 Job；REVIEW Job 还必须验证 review target、snapshot candidate 和全部 supporting Evidence 覆盖；若前置读取发现既有 PublishedJobReceipt，完整 Canonical bytes 必须逐字相同，否则 `EXECUTION_RECORD_FAILED + REJECTED`；
10. 仍持有同一 publication lease，首次预发布下一 Job 的 `job.json`，或采用前置读取且已经逐字验证的既有记录；若 Coordinator 本次不产生 next_job_spec 但同一派生 ID 已有合法 job.json，同样按 `EXECUTION_RECORD_FAILED + REJECTED`，不得静默留下矛盾记录；
11. 仍持有 lease，一次 commit 保存 Outcome、processing=`APPLIED`、正式元数据、状态增量、当前 Job 终态、Case 和可选下一 Job；成功或失败后在 `finally` 释放；
12. 释放 lease 后清理未接受提案并分发下一 Job。

第 5～11 步任何位置若发现确定性终止型拒绝（包括 `RESOURCE_LIMIT_EXCEEDED`、`RESOURCE_HASH_MISMATCH`、`EXECUTION_RECORD_FAILED`、proposal/正式化不变量失败），都必须放弃原 business TransitionPlan，在 `finally` 释放当前 lease 后，按第 7.3 节的固定 ExecutionFailure 在锁外重新读取、重新判断 active/stale 并调用 Coordinator；随后只用新的短 lease 条件提交原 Outcome、REJECTED/STALE processing record 和对应失败计划。不得持 lease 调 Coordinator，不得沿用原 accepted keys/state delta，也不得把确定性拒绝伪装成 retryable publication 错误。只有 `RESOURCE_PUBLISH_FAILED`、`STATE_WRITE_FAILED` 和耗尽内部冲突重算的 `REVISION_CONFLICT` 保持“无 disposition、同 receipt 重投”。

资源发布或 job.json 发布失败时不 commit，Outcome 可以以相同 ID 重交。状态 commit 失败时已发布对象成为可复用幂等对象：进程重启后，同 Outcome 必须重新得到相同正式 ID/target key；`ResourceStore.publish` 验证并重新 finalize 既有正式对象后采用，即使首轮 `os.replace` 已消耗 staged payload，也不能随机分配新 ID。未处理的 finalized Outcome 仍是 durable outbox，S02 清理不得隔离其 staged refs、确定性正式目标或预发布 next job；只有写入 APPLIED/STALE/REJECTED processing record 后，未引用对象才恢复普通孤立清理。

### 7.5 启动 epoch 恢复

本命令不是启动恢复的第一步。S05 必须先从同一 snapshot 枚举所有尚无对应 `OutcomeProcessingRecord` 的持久化 Job，并对 `ExecutionRecordStore.read_published_outcome` 返回的每个 finalized Outcome 调用正常 `SubmitJobOutcome`；这包括取消先胜出后才 finalize 的终态 Job，其结果应补记 STALE。只有这些投递都得到 APPLIED、DUPLICATE、STALE 或 REJECTED 后，才以同一 recovery_id 调用本命令。`RESOURCE_PUBLISH_FAILED`、`STATE_WRITE_FAILED` 或 `REVISION_CONFLICT` 使 replay 阶段保持未完成：仍为 RUNNING 的 Job不能提前中断，readiness=false。

`InterruptPreviousEpoch(current_runtime_epoch,recovery_id)` 只处理状态仍为 RUNNING、且 `runtime_epoch != current_runtime_epoch` 的 Job：

1. 以 `recovery_id` 做全局幂等检查；首次调用先追加 `RuntimeEpochRecord(runtime_epoch=current_runtime_epoch,started_at,recovery_id,recovery_completed_at=null)`；
2. 对每个旧 Job 构造 `MARK_OLD_EPOCH_INTERRUPTED` Trigger；
3. Coordinator 只把旧 Job 和 Case 变为 INTERRUPTED，不创建 replacement；
4. 每个 Case 单独条件 commit，成功一次使 `case_revision + 1`，DiagnosisState.revision 不变；
5. 全部旧 Job 处理完成后更新同一 RuntimeEpochRecord 的 `recovery_completed_at` 并保存恢复回执；S05 在内存恢复报告中另记已判定的 replayed outcome IDs，S03 重读状态后只处理中断前仍为旧 epoch RUNNING 的 Job；
6. 任一 Case commit 失败时 readiness 保持 false，重新执行同一 recovery_id 可从未完成项继续；不得为同一 runtime epoch 追加第二条记录。

恢复完成后 S05 才重投 PENDING 并启动 Worker。INTERRUPTED Case 等用户显式 Resume。

### 7.6 未发布执行失败

`ReportExecutionInfrastructureFailure` 只用于 S04 无法通过 `ExecutionRecordStore` 发布系统失败 Outcome 的窄故障路径：

1. 校验 `failure_id` 自然幂等键、ExecutionFailure Schema、Job/Case 归属和 `runtime_epoch`；
2. Job 仍为当前 active RUNNING 且 epoch 相同时，构造 `EXECUTION_FAILED` Trigger；
3. 一次条件 commit 保存 `ExecutionFailureRecord`、应用 Coordinator 计划并使 `case_revision +1`；
4. 相同 failure ID 与相同 hash 重放返回 `DUPLICATE`，revision 不变；同 ID 不同 hash 返回 `IDEMPOTENCY_CONFLICT`；
5. Job 已非 active、非 RUNNING 或 epoch 已变化时返回 `STALE`，不得改变 Case、Job 或任何 revision，也不保存新的失败记录。

正常 Runtime 失败仍必须通过 `job_outcome.json` 和 `SubmitJobOutcome` 进入，不得把本命令当成绕过 Outcome Schema 的通用入口。

## 8. 幂等与一致性

幂等记录与业务变化在同一 `state.json` commit 中持久化。记录逐字段使用 S00 `IdempotencyRecord`：operation、key、request hash、不可变 business receipt、Case ID 和创建时间；动态 CaseView、等待结果和 `dispatch_pending` 不持久化。

- 外部写命令记录保留到 Case 被显式删除；CreateCase 记录在 V1 中不自动删除；
- Outcome 以 `{job_id,outcome_id}` 加内容 hash 去重；
- Attachment 发布以 `{attachment_id,sha256}` 去重；
- 替代 Job 以 `replacement_for_job_id` 唯一；
- Dispatcher.submit 以 `job_id` 去重；
- 所有 commit 使用读取时 generation 和适用的 `case_revision`；
- `REVISION_CONFLICT` 时从头重读和重算，最多做 3 次无副作用内部重试；仍冲突返回错误；
- 一次外部命令尝试中预分配的 Case/Attachment 等 ID 和 Clock 时间在三次内部重试间保持不变；Outcome 接受产生的 Evidence/Artifact/首次 Candidate/next Job ID 则必须按 S00 `derive`，业务创建时间固定为 Outcome.produced_at，所以即使 state commit 前进程退出、重启后重交，也保持同一 proposal→ID 映射、完整 job.json bytes 和已发布对象。

V1 虽然一个 Worker，但仍允许多个客户端并发上传/查询，所以不能省略条件写。

## 9. 错误与响应

所有错误码、`ApplicationError` 字段和 retryable 映射都来自 S00；S03 只填充该公共类型，不定义第二套错误 DTO：

```text
ApplicationError
  code, message, details[], retryable
```

`details[]` 只使用 S00 `ApplicationErrorDetail`，含资源类型、不透明 ID/VersionedRef、期望/实际状态或 revision 和数值边界；不含绝对路径、日志内容、命令、凭据或环境变量值。

写命令 commit 成功后，即使分发或有限等待失败也返回已保存业务响应。调用方通过 Case/Job 状态判断后续动作。

## 10. Fake、Fixture 与注入点

依赖全部可注入：

```text
FakeCoordinator
PureContextSnapshotProjector
InMemoryStateRepository
InMemoryResourceStore
InMemoryPublicationCommitGuard
InMemoryAttachmentUploadGuard
FakeAssetCatalog
RecordingDispatcher
FixedClock
DeterministicIdGenerator
StateChangeNotifier
```

Fixture 至少覆盖：

- CreateCase 原子创建和重放；
- 参数组 A 分批满足且只创建一个 Job；
- Attachment READY 后不推进、SubmitSupplement 后推进；
- 日志 Job 在同一计划接受中间 Evidence/`LOGPARSE_RUN`，将 Evidence 的 Artifact proposal binding 解析为正式 `source_ref` 后进入 WAITING_INPUT；覆盖 Artifact 未接受、binding 非法和无法解析时整批不 commit；
- 参数 B 新 Job 固定复用前一 Artifact；
- continuation resource closure 从 R10 状态生成 R11 的 Evidence、源 Attachment、LOGPARSE_RUN、PREVIOUS_OUTCOME Evidence 来源与等待 Outcome；incoming Outcome 作为唯一 pre-commit 对象与 next Job 同一 mutation 插入，缺依赖整次拒绝；
- 同 Outcome 新 Evidence/Candidate 解析为正式对象后才投影 REVIEW snapshot，supporting Evidence 无遗漏；
- candidate 与唯一 USER_RESULT 同批接受；重算 UserResultPayload Canonical bytes/size/hash，binding 正式化后一一对应；候选沿用 ID 时 proposed_by_job_id 更新，旧 Job 结果保持隐藏，只有最终 ACCEPTED revision 所属 Job 的结果可下载；
- proposal 发布成功/失败、state commit 失败和孤立项；
- Upload 单目标与 Outcome 多资源 batch 在 publication lease 内一次性验证 5 GiB usage；超限时移动零个 stage，同 target 重放 delta=0；
- 并发同 attachment PUT barrier：相同 hash 的后到请求等待后复用首次回执且不覆盖 stage，不同 hash 返回冲突，首个流失败释放 per-attachment guard 后后者可继续；全程断言全局 publication lease 不跨网络流；
- 两个不同 attachment 的长流与另一 state 写入交错并推进全局 generation：各 `BinaryStream.read` 到 EOF 只发生一轮，流结束后各自重读 snapshot；revision conflict 只以同一 completed staged ref 重算 post-stage，成功或确定失败后才 discard；
- Attachment publish 成功→state commit 失败后，同 hash 重试采用既有 target 并完成 READY，异 hash 重试返回 `IDEMPOTENCY_CONFLICT` 且既有 target 不变；
- Prepare/requirement/PUT 共用 S00 ContentType grammar：合法 vendor `+`/`.` 类型通过，大写、参数、空白、控制字符、CRLF、非 ASCII、超长、allow-list 重复/非 Canonical 和 header 非逐字相等全部拒绝；
- lease 内发现 capacity/hash/next-job 记录等确定性拒绝时先释放 lease，再在锁外生成失败计划并用新 snapshot/短 lease 提交 REJECTED；Coordinator 调用期间断言未持锁；
- Outcome missing/invalid、正式化不变量、执行记录、Case capacity 和正式 target hash 五类拒绝逐字断言 S00 stage/code/message/retryable/details 排序及 Canonical Failure bytes；
- publish 成功→state commit 故障→进程重启→通过 `read_published_outcome` 重交，派生相同 Evidence/Artifact/Candidate/next Job ID并采用相同字节，完整 job.json（含 created_at、RuntimeBindings、logparse_product）逐字相等；
- Catalog A 预发布 next job 后 commit 失败、重启仅有 Catalog B：Outcome 重放仍采用 A 的完整 job.json；随后 Claim 若 A 不可加载则明确 `ASSET_VERSION_UNAVAILABLE`，不能重写成 B；
- 既有 next job 记录损坏或合法但重算 bytes 冲突都归一为 `EXECUTION_RECORD_FAILED + REJECTED`，且不得覆盖；
- publication lease 覆盖发布/采用到 commit，异常路径在 `finally` 释放，通知和等待不持锁；
- 同键同/不同 payload；
- revision 冲突三次边界；
- stale/duplicate/rejected Outcome；active base-drift 的 STALE 审计与 INTERRUPTED 计划同一 commit 且 case_revision 总计 +1；
- ExecutionRecordStore 故障通过未发布失败入口结束 active Job，重复/迟到报告不覆盖状态；
- REVIEW target 绑定、supporting Evidence 覆盖、verdict 问题数组矩阵与 PASS；
- Claim 前固定资产缺失使 PENDING Job/Case FAILED，Runtime 调用次数为 0；
- PENDING Resume 同 Job、INTERRUPTED Resume 新 Job；
- cancel/outcome 竞态的两个确定顺序；
- wait_seconds 超时不取消。

测试不得启动真实线程、HTTP、Claude 或 logparse；等待使用可控 notifier。

## 11. 独立测试命令

```text
python -m pytest tests/unit/application -q
```

## 12. 完成标准

- 所有业务写路径只能通过 Application Service；
- 每个命令顺序与第 5 节一致；
- 幂等记录和业务结果同 commit；
- READY Attachment 不自动推进；
- 分批补参已保存，全部满足时恰好一个新 Job；
- Outcome 技术校验与 Coordinator 业务决策无重叠；
- accepted proposal 才正式入状态；
- 下一 Job 在 proposal 正式化后由公共 projector 固定目标状态而非旧状态；
- Outcome 派生 ID 和已发布对象可跨进程故障重交复用；
- `STALE` 只作为 disposition；
- commit 后才分发，等待超时不改变 Job；
- Fake 测试不依赖 JSON、网络或子进程；
- S00、S01 合同测试和本册命令通过。

## 13. S08 交接格式

```json
{
  "spec_id": "S03",
  "title": "Application Service",
  "executor": {"model": "gpt-5.6-sol", "reasoning_effort": "ultra"},
  "contract_revision": "v1-contract-r1",
  "contract_base_commit": "<contract-base-commit>",
  "branch": "codex/v1-s03-application-service",
  "head_commit": "<head-commit>",
  "scope_completed": [],
  "changed_files": [],
  "fixtures_consumed": [],
  "fixtures_produced": [],
  "tests": [{"command": "python -m pytest tests/unit/application -q", "status": "passed"}],
  "dependency_requests": [],
  "contract_change_requests": [],
  "known_limitations": [],
  "risks": [],
  "integration_notes": [],
  "forbidden_scope_touched": false
}
```

以上顶层字段全部必填，不得省略；没有内容的列表写空数组。交接文件固定写入 `handoff/S03.json`。

## 14. 合同变更请求

若命令 DTO、Port 或错误码不足，按 S00 第 16 节提交合同变更请求，并附命令管线步骤、原子性影响、幂等 hash 变化及至少一个失败 Fixture。不得在 application 包创建协议专用或存储专用公共类型。
