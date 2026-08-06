# S02 JSON 状态与文件资源存储说明书

- 状态：V1 详细设计冻结稿
- 说明书编号：S02
- 上游合同：[`v1-contract-r1`](S00-contract-freeze.md)
- 组合入口：[《S08 V1 组合说明书》](../v1-composition-spec.md)

## 1. 目标与非目标

S02 是 V1 `state.json`、单实例锁、原子写、严格启动校验、FileResourceStore、附件/产物字节、临时文件和清理规则的唯一权威来源。

V1 的“单机”是：一个服务进程独占一个本地 `DATA_ROOT`，同一时刻只有一个 Worker 执行 Job；多个客户端可以访问该服务，但不能由第二个服务实例共享或接管该目录。V1 不要求数据库、Docker、共享文件系统、外部队列或故障自动接管。

S02 不决定 Case 状态转换，不执行 TransitionPlan 之外的业务变化，也不调用 Agent 或 logparse。

## 2. 独立文件责任区

未来 S02 实现任务唯一允许修改：

```text
src/problem_locator/storage/**
tests/unit/storage/**
tests/fixtures/components/storage/**
handoff/S02.json
```

所有公共类型和 Port 从 S00 导入。存储实现通过 `StateRepository`、`ResourceStore`、`ExecutionRecordStore`、`PublicationCommitGuard` 与 `AttachmentUploadGuard` 暴露，不让上层依赖 JSON 路径或操作系统锁。

## 3. 禁止修改项

- 不修改 S00 合同或 S01 转换规则；
- 不修改 application、runtime、scheduler、interfaces、integrations 或 Skill；
- 不建立 SQLite、PostgreSQL、Redis 或任何数据库；
- 不新增 Dockerfile、Compose 或容器启动依赖；
- 不把一个聚合拆成多份权威 JSON；
- 不以 JSONL、事件日志、文件名顺序或目录扫描重建当前业务状态；
- 不自动使用 `state.json.prev` 替代损坏的 `state.json`；
- 不对原始日志压缩包做枚举、解压、格式识别或安全判定；
- 不自动删除正式业务文件。

## 4. DATA_ROOT 布局

固定布局：

```text
DATA_ROOT/
├─ .instance.lock
├─ state.json
├─ state.json.prev
├─ resources/
│  └─ cases/<case_id>/
│     ├─ attachments/<attachment_id>/payload
│     ├─ evidence/<evidence_id>/payload
│     └─ artifacts/<artifact_id>/
│        ├─ payload
│        └─ tree/...
├─ jobs/<job_id>/
│  ├─ job.json
│  ├─ job_outcome.json
│  ├─ agent_job_outcome.rejected.json（可选）
│  ├─ stdout.log
│  └─ stderr.log
└─ tmp/
   ├─ uploads/<attachment_id>/
   ├─ proposals/<job_id>/<proposal_key>/{staged.json,payload|tree/...}
   ├─ workspaces/<job_id>/
   ├─ quarantine/<cleanup_id>/...
   └─ state/
```

`state.json` 是当前结构化业务状态唯一权威入口。`job.json` 是从已提交 Job 生成的不可变执行清单；`jobs/<job_id>/job_outcome.json` 是 Runtime 将 AgentJobOutcome 校验、暂存并规范化后发布的 JobOutcome；被 Runtime 实际读取但拒绝的原始字节原样归档为可选的 `agent_job_outcome.rejected.json`，同时继续保留在临时 Workspace 直至普通 Workspace retention 清理；stdout/stderr 是执行日志。这些执行记录用于执行、诊断和审计，不能覆盖 `state.json` 中的 Job 生命周期或 Outcome 处理 disposition。

`resources/**` 保存大文件字节或目录树，`state.json` 只保存结构化对象、相对 `storage_key`、大小和 SHA-256。绝对路径不得进入状态、外部响应或 Agent 结果。

## 5. 输入输出契约：state.json 与 ResourceStore

顶层固定为：

```text
StateFile
  schema_version: 1
  contract_revision: "v1-contract-r1"
  generation: integer
  installation_id: uuid
  created_at: timestamp
  updated_at: timestamp
  runtime_epochs: [...]
  cases: {case_id: CaseAggregate}
  idempotency_records: {compound_key: IdempotencyRecord}
```

`CaseAggregate` 包含该 Case 对象以及 Job、JobOutcome、Outcome processing record、Attachment、Evidence 和 Artifact map；DiagnosisState 只存在于 `Case.diagnosis_state`，不得在聚合 envelope 再复制一份。跨 Case 资源引用禁止。所有 map 键必须等于对象内 ID。Job 与 Case 已保存实际使用的完整 VersionedRef，V1 不在 state 顶层复制运行时 Catalog。

`generation` 每次成功写整个 state 增加 1；首次空目录初始化为 1。它是 FileStateRepository 的全局条件写 token，不替代 `case_revision`。

`StateRepository.read_snapshot()` 必须在同一个进程内读锁下返回深度不可变的完整 StateFile 视图；调用方由该视图同时取得 generation、幂等记录、Case、Job 和恢复扫描数据。返回后发生的写入不会改变该对象，后续 `commit` 仍用其中 generation 做条件检查。不得通过多次独立读取拼出一个写命令的基础快照。

不支持 on-read migration。`schema_version != 1` 或 `contract_revision != v1-contract-r1` 返回 `STATE_SCHEMA_UNSUPPORTED`；解析、Schema、哈希、引用或领域不变量失败返回 `STATE_CORRUPT`。

## 6. 单实例与线程并发

### 6.1 进程锁

服务启动在任何状态读取前以非阻塞方式独占 `.instance.lock`：

- Windows 使用打开文件句柄加 `msvcrt.locking` 的独占锁；
- Linux/macOS 使用 `fcntl.flock(LOCK_EX | LOCK_NB)`；
- 锁文件句柄持有到进程正常关闭；
- 锁内容可以写 installation ID、PID 和启动时间供人工诊断，但内容不是锁的正确性来源；
- 获取失败返回 `INSTANCE_LOCKED`，readiness 为 false，不启动写服务或 Worker；
- 不以 PID 是否存在判断锁是否有效。

### 6.2 进程内协调锁

启动装配只创建一个进程内可重入 `StorageCoordinationLock`，并把同一实例注入 JsonFileStateRepository、FileResourceStore、FileExecutionRecordStore、`PublicationCommitGuard` 和清理器。Repository 的完整读取当前内存快照、条件校验、克隆、写盘和内存替换都在该锁内；正式路径的发布/已有对象采用、Job 文件预发布，以及清理候选的最终重验与隔离也使用同一锁。另创建唯一 per-attachment registry，同时注入 S02 的 `AttachmentUploadGuard` 与 FileResourceStore；lease 是该 registry 产生的 opaque capability，FileResourceStore 验证 owner ID 与 released 状态。只读调用可返回不可变深拷贝，不得暴露内部可变对象。

S00 `PublicationCommitGuard.acquire()` 取得的 lease 持有此锁，直到 S03 对同批发布执行 `StateRepository.commit` 成功或失败并在 `finally` 中释放。Repository 和各 Store 在 lease 内再次取锁必须依赖可重入语义，不得换用另一把同名锁。大文件流式暂存、hash 计算、Agent 执行、通知、Dispatcher 和有限等待不占用协调锁。

V1 不支持第二进程只读打开同一 `DATA_ROOT`；导出和校验命令也必须取得同一实例锁。

## 7. 启动、就绪与人工恢复

启动顺序固定：

1. 校验 `DATA_ROOT` 是显式绝对路径并创建允许的缺失目录；
2. 取得实例锁；
3. 若 `state.json` 不存在且目录没有既有业务内容，创建初始状态；
4. 若 `state.json` 不存在但 `state.json.prev` 或业务资源存在，返回 `STATE_CORRUPT`；
5. 读取全部字节，执行 UTF-8、Canonical JSON、Schema、合同修订和全局不变量校验；
6. 校验所有被状态引用的正式 Resource 与 Job 清单存在、大小和 hash 一致；
7. 构建只读内存快照；
8. 只有全部成功才令存储 readiness 为 true；服务总 readiness 还要等待 S05 完成启动恢复。

进程存活时 `/live` 可以返回成功；上述任一步失败时 `/ready` 必须失败，Application Service 拒绝写入且 Worker 不启动。

`state.json.prev` 仅供管理员手工比较和恢复。程序不得自动 fallback。人工恢复必须在服务停止后完成，先保留损坏文件，再显式用 `validate-state` 验证候选文件；恢复操作本身不在 V1 在线 API 内。

## 8. 原子状态提交

`StateRepository.commit` 的算法不可省略：

1. 在 `RLock` 内验证 `expected_generation`，以及命令指定时的 `expected_case_revision`；
2. 深克隆当前状态；
3. 将 Application Service 已构造的 mutation 应用于克隆；
4. 对完整克隆执行 S00 Schema、引用和全局不变量校验；
5. 将 `generation + 1` 并更新时间写入克隆；
6. 以 S00 Canonical JSON 编码；
7. 在 `tmp/state/` 创建同卷临时文件，写完后 `flush` 与文件 `fsync/FlushFileBuffers`；
8. 若当前 `state.json` 存在，将其字节写入另一个同卷临时文件、同步后 `os.replace` 为 `state.json.prev`；
9. `os.replace` 新临时文件为 `state.json`；
10. 对 `DATA_ROOT` 目录执行平台支持的目录持久化同步；Windows 使用等价目录句柄 `FlushFileBuffers`；
11. 重新读取或核对最终文件 hash 后才替换内存快照；
12. 返回 `CommitReceipt` 后上层才可响应成功。

任何步骤失败都返回 `STATE_WRITE_FAILED`，不得返回成功响应。失败后 Repository 必须重新读取并验证磁盘 `state.json`，使内存与实际最后提交保持一致。临时文件可留待 24 小时清理。

一次 commit 覆盖 TransitionPlan 的全部结构化变化：输入/Outcome、资源元数据、Case、DiagnosisState、当前 Job 结束和下一 Job 创建。不存在跨多个 JSON 的补偿事务。

## 9. ResourceStore

### 9.1 暂存

Attachment 上传和 Agent Proposal 分别流式写入 `tmp/uploads/<attachment_id>/` 与 `tmp/proposals/<job_id>/<proposal_key>/`：

- 写入同时累计字节数与 SHA-256；
- 单 Attachment 在第 2684354561 个字节到达前终止并返回 `RESOURCE_LIMIT_EXCEEDED`；
- stage 只执行单对象 2.5 GiB 边界，不预留 Case 配额；单 Case 5368709120 字节必须在 publication lease 内按 S00 全批正式目标语义重验；
- 声明大小或 hash 不符分别返回 `RESOURCE_SIZE_MISMATCH`、`RESOURCE_HASH_MISMATCH`；
- 暂存文件完成后同步文件和父目录；
- 未完整的上传返回 `UPLOAD_INCOMPLETE`，不得变为 READY。

Attachment 上传必须调用 S00 `ResourceStore.stage_attachment` 并传入同一 attachment_id 的有效 `AttachmentUploadLease`，完成后返回只含 `attachment_id`、`resource_kind=FILE`、`size`、`sha256` 的 `AttachmentStagedRef`。其暂存目录只由 `attachment_id` 决定，不得伪造 `owner_job_id` 或 `proposal_key`，也不得复用 Agent Proposal 的 `stage_file`。S02 必须拒绝 ID 不匹配或已释放 lease，同 ID 不支持无 guard 并发写。失败 stage 只能由持 lease 的调用方 discard，完成标记必须最后原子发布。

每个 Agent Proposal 暂存目录必须先写 `payload` 或 `tree/`，再原子发布 Canonical JSON `staged.json`；其字段逐字对应 S00 `StagedResourceRef`。`staging_id` 由 ResourceStore 分配，`owner_job_id` 与 `proposal_key` 同时决定目录，三者不匹配即返回 `OUTCOME_INVALID`。只有 `staged.json` 已发布且内容 hash 复核成功的暂存项可交给 S03；Workspace 路径永远不写入该文件。Agent Proposal 只能使用 `StagedResourceRef`，不得引用 `AttachmentStagedRef`。

S02 只按字节处理用户日志附件。不得打开、列出或解压压缩包；logparse 的格式、安全阈值和解析由 S07 独占。

### 9.2 发布

正式 `storage_key` 由资源所属 Case 和服务器分配的资源 ID 构造，不使用用户文件名决定目录。发布步骤：

1. 调用方已取得 PublicationCommitLease，并在同一共享 `StorageCoordinationLock` 内先用 S00 `validate_case_capacity` 一次提交本批全部 `PlannedResourceTarget`；S02 扫描/验证 Case 正式 resources 根，按唯一 storage_key 计算 current/new/total，任何超限或既有内容冲突都在移动首个 stage 前失败；
2. 从调用方给定的正式 resource ID 构造唯一目标；该 ID 已由 S03 按 S00 `IdGenerator.derive` 固定；
3. 若正式目标已经存在，按 staged ref 中冻结的 resource_kind/size/sha256 完整重验节点类型、普通文件或目录树边界和全部字节；不同返回 `RESOURCE_HASH_MISMATCH`，相同也不能直接返回，而要幂等重施第 7～8 步的只读与持久化 finalize，不要求首次发布已移动的 stage 仍存在；
4. 若正式目标不存在，才按 staged ref 类型验证其位于允许的 `tmp/uploads` 或 `tmp/proposals` 子树且 hash 未变；
5. 创建目标父目录；
6. 同卷 `os.replace` 文件，或对目录树逐项同步后原子替换顶层目录；
7. 将正式文件/目录树幂等设为服务账户只读，再同步每个正式文件；目录资源按叶到根同步目录句柄；
8. 同步目标父目录；既有目标采用也必须执行本步骤；
9. 返回相对 key、总字节和内容 hash；
10. Application Service 随后才允许在 state.json 中引用它。

若资源发布失败，状态 commit 不开始。若资源发布成功而状态 commit 失败，该资源成为待重放对象；只在对应 finalized Outcome 已有 processing record 且 state 无引用后才进入 7 日孤立规则，禁止立即回滚删除一个可能被幂等重试采用的正式对象。正式路径的发布或已有对象采用必须在共享 `StorageCoordinationLock` 内完成，并且调用方的 `PublicationCommitLease` 必须继续覆盖随后的 state commit。

发布幂等键是 `{case_id, resource_type, resource_id, sha256}`；Outcome proposal 的 resource ID 由 S00 固定输入跨重启确定性派生。正式目标已存在且 hash 相同视为成功，其路径和已验证内容就是无需第二个可变索引的 publication receipt；同 key 不同 hash 返回 `RESOURCE_HASH_MISMATCH`。`tmp/quarantine/` 中的对象既不是正式目标也不可采用；若清理先完成隔离，重试只能从仍然有效的 staged ref 重新发布，否则安全失败且不得提交引用。

Attachment 的外部语义由 S03 收口：发布成功但 READY commit 失败后，相同 attachment_id 与相同 size/hash 的重试采用上述正式目标；底层不同 hash 仍拒绝覆盖并返回 `RESOURCE_HASH_MISMATCH`，S03 必须将该特定 PUT 冲突稳定投影为 `IDEMPOTENCY_CONFLICT`，不得泄漏路径冲突。

Case 配额逐字采用 S00：state 引用、未确认 outbox 目标和普通 orphan 都计入，quarantine/tmp/Workspace/Job/Outcome/log 不计；同 key 多引用或重交只计一次，不同 key 同 hash 分别计数。PrepareAttachment 可在无 lease 时用 declared size 做 advisory 预检，但不预留；Upload 的单目标和 Outcome 接受的全部目标都必须在同一 publication lease 内、任何 publish 前全批重验，避免先发布一部分再因后续对象超限。

### 9.3 文件与目录资源

普通 Attachment/Evidence/用户 Artifact 使用 `payload` 文件。`LOGPARSE_RUN` 使用只读 `tree/` 目录，其整体 hash 由按相对 POSIX 路径排序的 `{path,size,sha256}` 清单的 Canonical JSON 计算。目录不得含链接、设备文件、命名管道、绝对路径或越界路径。

FileResourceStore 只接收已经由 logparse 产生的解析目录；它验证目录树边界和 hash，但不解释日志内容。

### 9.4 只读物化

`materialize_read_only` 必须：

- 解析并验证 storage key 后再拼接路径；
- 校验最终 resolved path 位于 `DATA_ROOT/resources`；
- 文件用硬链接或复制，目录用受控复制；任何失败可以回退为复制；
- 设置只读属性；
- 物化到指定 Job Workspace 的 `inputs/`，不暴露源绝对路径；
- 再次校验大小和 hash，不匹配返回 `RESOURCE_NOT_FOUND` 或 `RESOURCE_HASH_MISMATCH`。

## 10. Job 文件

在 state commit 引用新 Job 前，S03 提供完整 Job DTO，S02 先原子发布 `jobs/<job_id>/job.json`。Outcome 产生的 next Job ID 已按 S00 确定性派生；同路径既有 job.json 的 Canonical bytes 完全相同时视为幂等采用，不同则返回 `IDEMPOTENCY_CONFLICT`。首次发布和相同 bytes 采用都必须执行“完整普通文件/Schema/ID/Canonical bytes 校验→只读权限→文件同步→父目录同步”的 finalize；replace 成功但 chmod/fsync 失败不能返回 receipt，下次采用必须幂等补完。其内容创建后不可改；生命周期仍只在 `state.json` 中更新。

Agent 在 `tmp/workspaces/<job_id>/output/job_outcome.json` 临时产生 AgentJobOutcome；S04 验证并把 draft proposal 写入持久化暂存区后，通过 `ExecutionRecordStore.publish_outcome_bytes` 将规范 JobOutcome 原子发布为 `jobs/<job_id>/job_outcome.json`。若 Runtime 已安全读取该文件但拒绝其 JSON、Canonical、Schema、binding 或 proposal 语义，则通过 `publish_rejected_agent_output_bytes` 把参与校验的原始字节幂等归档为 `jobs/<job_id>/agent_job_outcome.rejected.json`；归档字节不重新编码、不要求自身是合法 JSON，相同字节采用，不同字节返回 `IDEMPOTENCY_CONFLICT`，归档失败不覆盖原始 Runtime failure。规范 Outcome publish 的成功返回仍是 Runtime 已永久结束的 durable-outbox 线性化点；之后只能重投同一 Outcome，禁止再执行 Agent。S03 保存 Outcome processing record 时同时保存规范 Outcome 文件 hash，rejected 归档不进入业务状态。

`ExecutionRecordStore.read_published_job/read_published_outcome` 只读取上述最终文件，临时 `.tmp/.part` 等同不存在。最终文件存在时重新验证普通文件、路径 ID、S00 Schema、Canonical JSON bytes 和实际 size/hash，再返回 `PublishedJobReceipt` 或 `RuntimeExecutionReceipt`；损坏、链接、不可读或 ID 不符返回 `EXECUTION_RECORD_FAILED`，不得读取 `.prev`、日志或当前 Catalog 补造。S05 启动恢复和 S03 Outcome 技术校验只能通过这两个 Port 读取，不能直接拼接 `jobs/` 路径。

S02 通过 `ExecutionRecordStore.open_log_sinks` 为 stdout/stderr 提供仅追加的二进制 sink；两个 sink 共享 64 MiB 计数器，达到限制时写入失败并通知 S04 终止进程树。每个 Job 首次打开时原子创建两个空文件，既有非空日志不得被截断或复用于第二次执行。它们不是 Canonical JSON，也不参与诊断状态恢复。

## 11. 清理与保留

启动后和每 24 小时执行一次单线程、可中断清理：

| 对象 | 删除条件 |
|---|---|
| upload 暂存 | 创建超过 24 小时，未被正式资源引用 |
| proposal 暂存 | 创建超过 24 小时，Outcome 未接受或已完成处理 |
| Workspace | Job 已终态且超过 24 小时，所有需保留提案已发布 |
| state 临时文件 | 超过 24 小时且不是当前/prev |
| 正式孤立资源 | 超过 7 日、完整 state 无引用、不是发布中的目标 |
| 孤立 Job 执行目录 | 超过 7 日、完整 state 中不存在该 `job_id`；仅可能来自 job.json 预发布后 state commit 失败 |

“Outcome 未接受”不能只按 StateFile 判断：只要 `jobs/<job_id>/job_outcome.json` 已 finalized 且 state 中尚无该 Outcome 的 `OutcomeProcessingRecord`，它就是未确认 durable outbox。清理器必须在共享协调锁内解析并验证该 Outcome，保护其中全部 `StagedResourceRef`、按 S00 proposal 派生出的正式资源目标，以及 `[installation_id,case_id,outcome_id,"next_job"]` 派生的预发布 Job 目录；这些对象即使超过 24 小时或 7 日也不得 quarantine。processing 成为 APPLIED、STALE 或 REJECTED 后，state 已引用对象照常保留，未引用对象才恢复普通临时/孤立清理。

若任一“最终文件存在但无 processing record”的 Outcome 无法通过 `read_published_outcome`，其内容不可信且无法枚举精确保护集合。本轮必须暂停全部破坏性清理，包括 upload/proposal/Workspace、正式 orphan、孤立 Job 和既有 quarantine 的递归删除，返回/记录 `EXECUTION_RECORD_FAILED`，直到管理员修复或显式处置该最终记录；不能以“无法解析所以无引用”为由越过 24 小时/7 日门槛。这与 S05 readiness 持久失败使用同一损坏事实。

Attachment、Evidence、Artifact、Job 文件和执行日志属于 Case 正式数据，不按时间自动删除。管理员未来的整 Case 显式删除命令不属于 V1。

对暂存项和 Workspace，清理器必须用创建完成标记和当前业务状态排除仍在写入或待发布的对象。对正式孤立资源和孤立 Job 执行目录，清理固定使用两阶段算法：

1. 在共享 `StorageCoordinationLock` 内读取当前 StateFile，再次确认候选无引用、已超过保留期且不是发布中的目标；
2. 仍在同一锁内，以 `os.replace` 把候选精确顶层路径原子移动到 `tmp/quarantine/<cleanup_id>/`，并同步正式父目录与 quarantine 父目录；检查与隔离之间不得释放锁；
3. 释放锁后才递归删除 quarantine 中的精确对象；删除失败只记录执行日志，后续周期重试，不改变业务状态。

正式发布/采用只检查正式资源路径，永不扫描或恢复 quarantine。因而无论清理先取得锁还是幂等重试先取得锁，都不能出现 `state.json` 已引用而正式对象随后被清理删除的结果。

## 12. PostgreSQL 迁移边界

上层只能依赖 `StateRepository`/`ResourceStore`：

- `case_revision`、不可变 Job/Outcome、幂等键和条件提交语义保持不变；
- `generation` 可映射为数据库全局版本或事务 token，不进入外部 API；
- JSON 的 `storage_key` 可继续指向文件/对象存储；
- PostgreSQL 版本用表和事务替代 state.json，不要求改 S01、S03、MCP DTO 或 Agent 合同；
- 迁移程序通过 `export-state` 的 Canonical JSON 读取，不让新版运行时直接 on-read 修改 V1 文件。

V1 不预建 ORM、迁移框架、数据库配置或 Docker 资产。

## 13. Fake、Fixture 与注入点

组件测试使用真实临时目录和可注入平台适配器：

```text
FakeFileSync
FakeInstanceLock
FaultInjectingReplace
BarrierStorageCoordinationLock
FixedClock
DeterministicIdGenerator
```

Fixture 必须覆盖：

- 空目录初始化；
- 有效 state、未知 schema、截断 JSON、非法引用、错误 hash；
- 写临时文件、更新 prev、replace、目录同步各阶段故障；
- 失败后磁盘/内存重新一致；
- 双实例锁竞争；
- per-attachment guard：同 ID 排队、不同 ID 可并行、lease ID/释放状态验证、进程内 registry 释放；同/异 hash PUT barrier 不覆盖 stage；
- 两个不同 ID 都完成流式 stage 后，A 的 commit 推进全局 generation，B 仍以同一 completed `AttachmentStagedRef` 在短 lease 内 fresh read 并成功；每个 BinaryStream 只读一轮，publish 后 conflict 采用既有正式 target 且 capacity delta=0；
- Windows 与 POSIX 锁适配器；
- 2.5 GiB/5 GiB 计数边界流；
- 5 GiB Case usage：满额、同 target 重交 delta=0、同 key 多引用只计一次、不同 key 同 hash 分别计、未确认 outbox/orphan 计入、quarantine 排除，以及两个并发上传在共享 lease/锁下只有合法批次通过；
- Outcome 多资源 batch 在移动任一 stage 前整体通过或拒绝配额，拒绝时不存在 partial formal target；
- 相同/冲突资源发布重放；
- 首次发布移动 stage 后 state commit 失败，重启重交用确定性正式目标作为 publication receipt 幂等采用；
- Attachment 发布成功但 READY commit 失败：同 attachment_id/同 hash PUT 不依赖旧 stage 即可采用并完成，同 ID/异 hash 底层拒绝覆盖且由 S03 投影为 `IDEMPOTENCY_CONFLICT`；
- replace 成功后只读设置、文件同步或父目录同步失败；重启采用既有资源、job.json 和 job_outcome.json 时必须补完 finalize 后才返回 receipt；
- `read_published_job/read_published_outcome` 的不存在、合法、截断、非 Canonical、路径 ID 不符和链接拒绝；
- 文件和 `LOGPARSE_RUN` 目录只读物化；
- 24 小时与 7 日边界清理；
- 被引用正式资源绝不被清理。
- barrier 竞态：清理完成无引用检查前后与幂等发布/采用及 state commit 交错，两种锁顺序下最终都不存在引用已被删除正式对象的状态。
- durable-outbox barrier：finalized Outcome 尚无 processing record 时，清理不得隔离其 staged refs、确定性正式目标或预发布 next job；写入终态 disposition 后未引用对象才恢复普通清理。
- 损坏且未确认的 finalized Outcome 跨过 24 小时/7 日阈值时，整轮破坏性清理暂停，任何依赖候选和既有 quarantine 都不删除；修复并确认后才恢复。

## 14. 独立测试命令

```text
python -m pytest tests/unit/storage -q
```

测试不需要数据库、Docker、网络、Claude 或真实 logparse。

## 15. 完成标准

- 只有一个 `state.json` 是当前结构化业务状态权威；
- 锁、严格启动、原子 replace、文件和目录同步均有故障注入测试；
- 损坏状态不会自动回退或继续写；
- Repository 不暴露 JSON 内部可变引用；
- ResourceStore 不解释或解压日志压缩包；
- 正式资源先发布、状态后引用，孤立清理可验证；
- 发布/采用到 state commit 与清理隔离共享同一协调锁，TOCTOU barrier 用例通过；
- 不自动删除正式业务数据；
- 路径遍历、链接和跨 Case 引用被拒绝；
- 上层测试可换 InMemory Port，不依赖 DATA_ROOT；
- S00 合同与本册测试命令通过。

## 16. S08 交接格式

```json
{
  "spec_id": "S02",
  "title": "JSON State and File Resource Storage",
  "executor": {"model": "gpt-5.6-sol", "reasoning_effort": "ultra"},
  "contract_revision": "v1-contract-r1",
  "contract_base_commit": "<contract-base-commit>",
  "branch": "codex/v1-s02-json-resource-storage",
  "head_commit": "<head-commit>",
  "scope_completed": [],
  "changed_files": [],
  "fixtures_consumed": [],
  "fixtures_produced": [],
  "tests": [{"command": "python -m pytest tests/unit/storage -q", "status": "passed"}],
  "dependency_requests": [],
  "contract_change_requests": [],
  "known_limitations": [],
  "risks": [],
  "integration_notes": [],
  "forbidden_scope_touched": false
}
```

以上顶层字段全部必填，不得省略；没有内容的列表写空数组。交接文件固定写入 `handoff/S02.json`。

## 17. 合同变更请求

若 StateRepository/ResourceStore Port 无法表达所需原子性，按 S00 第 16 节提交请求，并附故障注入用例、期望持久化边界和 PostgreSQL 兼容性分析。不得在 storage 包导出第二套 DTO。
