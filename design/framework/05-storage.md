# 存储、并发控制与保留清理详细设计

本章覆盖当前 `src/problem_locator/storage` 中全部 20 个 Python 文件。该目录当前没有 `__init__.py`；不把生成的 `__pycache__` 作为设计文件。重点区分正在使用的生产存储、仍存在的参考实现和旧版原子状态写入原语。

## 1. 存储职责与生产边界

存储层为 Application、Dispatcher、Runtime 和网站服务提供状态事务、不可变资源、执行记录、流式读取与生命周期清理。公开端口传递 `ResourceRef`、`StagedResourceRef`、`PlannedResourceTarget` 和合同模型，物理路径、数据库连接和文件锁由本层管理。

| 数据 | 当前保存位置 | 读写特点 |
| --- | --- | --- |
| 活跃 Case 及进程元数据 | `CaseStateRepository` 的内存结构 | 按 Case 隔离锁和 revision；重启后不能假设活跃 Case 聚合完整保留 |
| 已完成 Case | PostgreSQL `completed_cases` 的规范快照字节与索引 | 达到规定终态后持久化；历史按需加载，不在每次写入时扫描全部历史 |
| 网站会话、轮次、消息、事件及管理意图 | 同一个 PostgreSQL 中的 `agent_*` 表 | 会话投影可以持久化，不能等同于完整活跃 Case 聚合已经持久化 |
| 反馈、经验、报告追问 | `memory_*`、`agent_followup_*` 表 | 各模块管理自己的事务、配额和任务恢复 |
| 正式附件、证据、产物 | `DATA_ROOT/resources/cases/...` | 发布后只读；大小、哈希及目录 manifest 定义内容身份 |
| Job、Outcome、执行审计和 stdout/stderr | `DATA_ROOT/jobs/<job_id>/...` | 固定文件名、规范字节或原始审计字节；不可变记录与有界追加日志分开 |
| 上传、提议、工作区、隔离内容 | `DATA_ROOT/tmp/...` | 有明确 owner、活动租约和保留期限 |

生产装配传入 PostgreSQL `database_url`，要求 PostgreSQL 15 或更高版本。数据库与 `DATA_ROOT` 同时绑定 installation ID、存储后端和资源根身份，不能把一个数据库随意连到另一个资源目录。数据库还持有单 Server 所有权锁：连接池允许同一服务内部并发，不代表支持多个 Server 共享一个数据库同时运行。

直接构造 `CaseStateRepository` 且不传 `database_url` 时，仍使用 `completed.sqlite3`，开启 WAL 和 `synchronous=FULL`。这是离线/确定性参考路径。`state_atomic.py` 中的单文件 `state.json` 写入器仍有独立用途和测试，但当前 Case 仓库不会用它保存生产主状态。平台辅助代码保留 Windows/macOS 文件原语，也不能据此推导这些平台支持运行生产 Server。

## 2. 目录与资源身份

```text
DATA_ROOT/
  .instance.lock
  data-format.json
  resources/
    cases/<case_id>/
      attachments/<resource_id>/payload
      evidence/<resource_id>/{payload|tree}
      artifacts/<resource_id>/{payload|tree}
    conversations/<attachment_id>/payload
  jobs/<job_id>/
    ... Job、Outcome、执行日志和固定审计文件
    followup-inputs/             # 报告追问的已验证副本
  tmp/
    uploads/<attachment_id>/
    proposals/<job_id>/p-<sha256>/
    workspaces/<job_id>/
    workspaces/<job_id>.logparse-preprocess/
    quarantine/<cleanup_id>/...
    state/                      # 保留的原子文件写入临时目录
```

目录名与文件名由服务生成的标识组成。用户的附件显示名不作为实际保存路径；任意 proposal key 先哈希成固定路径段。正式资源 key 必须符合六段相对 POSIX 路径语法，禁止绝对路径、反斜杠、空段、`.` 和 `..`。工作区还允许 Intake、追问等活动使用自己的 UUID。

所有受信任路径都需验证根目录、祖先和末端节点。文件读取/复制会检查普通文件、硬链接计数、文件描述符与路径身份、大小和时间等元数据。符号链接、Windows reparse point、读取期间替换或修改都会被拒绝，不能只靠一次 `resolve()` 判断资源安全。

## 3. 状态提交和数据库并发

### 3.1 Case 提交

`commit(expected_generation, expected_case_revision, mutation)` 先定位受影响 Case，取得该 Case 锁并校验版本，再在副本上应用 mutation。新的幂等键在提交前预约，失败时释放预约。不同 Case 使用独立版本域；不会为了提交某个 Case 而反序列化全部历史。

仓库的持久化终态集合为 `RESOLVED`、`PARTIALLY_RESOLVED`、`UNRESOLVED`、`FAILED`、`CANCELLED`。`INTERRUPTED` 不在该集合中，这一点与网站轮次可见的终态概念不同。达到持久化终态后，`_persist` 在一个事务中写入快照、对象/请求/资源索引、保留时间、归档任务和网站投影，随后移除相应活跃缓存。非持久化终态变化更新内存，同时可在数据库事务中提交网站投影。

资源文件的同步与只读发布必须先完成，Case 最终事务才允许引用它们。最终状态提交失败会锁存 `STATE_WRITE_FAILED`，后续写入停止并暴露健康错误，避免继续交付与磁盘结果不一致的状态。

`read_case_snapshot_with` 按 Case → 数据库的顺序取得关联投影与快照。传入的投影回调只能操作已给出的数据库连接，不能再获取 Case 锁或重新读取仓库；实际资源读取在释放相关短锁之后完成。下载等长活动另用 `case_usage` 租约防止历史清理。

### 3.2 PostgreSQL 连接与所有权

`PostgresDatabase` 使用一条专用所有权连接和独立连接池。连接租约保存在线程本地，嵌套调用复用当前连接；嵌套写事务使用 savepoint。只读顶层事务使用 `REPEATABLE READ READ ONLY`，不能在只读作用域内开始写入。

所有权连接持有 session advisory lock。每个业务事务另取共享 drain 锁并确认所有权仍有效；新的 Server 在接管时先等待旧事务结束。丢失所有权不会自动重连并继续写入，必须作为隔离失效报错。关闭服务时，所有权连接保留到最后一个活动租约释放，避免退休中的事务与新实例重叠。

按会话的短事务使用 transaction advisory lock；任务队列使用行锁与 `SKIP LOCKED`。`SqlDialect` 显式生成 JSON 访问、插入顺序及元数据查询所需差异。唯一参数改写是把 SQL 中真正的 `?` 占位符转换为 psycopg `%s`，不是把 SQLite 方言猜测性翻译成 PostgreSQL。

## 4. 资源暂存、发布与读取

1. `FileResourceStore.stage_file`、`stage_tree`、`stage_attachment` 或 `stage_generated_file` 接收内容并取得精确暂存路径租约。
2. `StagedObjectWriter` 在所属目录写临时内容，同步文件和目录后安装 `payload`/`tree`，最后发布不可变 `staged.json` 完成标记。没有完成标记的目录不能被当作可发布资源。
3. 资源 Store 校验 marker、内容大小、SHA-256、TreeManifest、producer 身份和预期 metadata，产生 `StagedResourceRef`。
4. Application 在短发布租约内调用 `plan_target` 和 `publish`，目标 Case、资源 ID、类别和内容身份必须一致。已有目标只允许采用相同内容；不同内容视为冲突。
5. 正式资源设为只读并同步后，Application 才提交引用该资源的状态。若文件已发布而状态未提交，资源仍是孤儿，后续由有引用检查的保留清理处理。
6. `open_read` 打开严格校验的文件流；`materialize_read_only` 把资源受控复制到合法工作区输入位置，并验证内容后只读发布。不会把任意外部目录当作 materialization 目标。

附件上传与发布使用不同租约：前者覆盖整个可能很长的输入流，按附件 ID FIFO 串行；后者覆盖资源发布到状态提交的短区间。同一 Case 发布串行，不同 Case 可重叠；无 Case 的维护屏障与所有发布互斥。清理器和发布者必须拿到同一实例的协调锁，不能各建一个名称相同的锁。

## 5. 执行记录与故障恢复

`FileExecutionRecordStore` 把 Job、Outcome 与审计文件写入对应 Job 目录。发布采用临时文件、同步、原子替换和最终只读校验。重试只允许采用完全一致的字节；已有不同字节不是可覆盖更新。`ExecutionFileRef` 返回相对文件身份、大小与哈希，消费者据此验证。

stdout/stderr 使用成对的追加 sink 和共享总字节预算。日志超预算或文件身份改变会失败，而不是无限扩张或继续写到被替换的文件。通用服务 JSONL 日志另由 `BoundedJsonlFile` 轮转，默认单文件 16 MiB、4 个备份，单事件最多 64 KiB；超大事件裁剪仍保持合法 JSON 和事件身份。

执行文件不是当前完整活跃 Case 的持久化替代品。Case 仓库启动只装配安装元数据、数据库索引和惰性读取能力；Agent、归档、追问、经验等模块分别根据自己的持久状态决定恢复、取消或标记中断。不能从一个残留工作区或日志文件直接宣称诊断已完成。

## 6. 保留策略和两阶段删除

| 对象 | 时间/条件 | 负责模块 |
| --- | --- | --- |
| 上传暂存、proposal 暂存、工作区、状态临时文件 | 严格超过 86,400 秒且无活动/引用保护 | `RetentionScanner` + `StorageRetentionCleaner` |
| 孤儿正式资源与 Job 目录 | 严格超过 604,800 秒且不再被业务索引引用 | 同上 |
| 完成轮次、Case 历史、过期会话附件、删除请求历史 | 7 天，并需空闲、无未完成归档/追问等活动 | `HistoryRetentionService` |
| 用户显式删除会话 | 先撤销访问，等待活动结束，再按持久清单删除 | `ConversationCleanupService`，见 [会话服务设计](04-services-interfaces.md) |
| 经验原文与经验卡 | 来源 7 天，卡片 90 天；显式删除与自然到期语义不同 | `MemoryStore`，见 [会话服务设计](04-services-interfaces.md) |

物理扫描仅判断节点形状、元数据和时间。真正删除前，清理器在维护屏障内再次检查元数据未变、资源索引、活跃 Job、上传租约、暂存路径及追问工作区保护。检查失败或证据不足时保留文件。可以删除的节点先原子移动到 `tmp/quarantine/<cleanup_id>/...`，释放协调锁后才递归删除隔离节点；失败路径保留供后续重试。

历史清理先保存精确清单，并在同一事务内移除业务引用；之后处理文件。旧轮次可以单独过期，当前新轮次继续保留。若整个会话所有轮次都到期，则复用会话删除清理流程，但使用自然过期语义保留已完成经验卡。历史事件裁剪更新 `events_pruned_through`，让过旧 SSE 游标被识别，而不是伪造连续历史。

数据库整理在业务事务外执行。PostgreSQL 使用 `VACUUM (ANALYZE)` 复用死元组空间，不使用 `VACUUM FULL`；SQLite 参考路径按需 checkpoint 和 VACUUM。这里的产品数据保留清理不等于仓库 Test Flow 证据清理，不能替代 `evidence.mjs` 的审查与精确删除流程。

## 7. 逐文件详细设计

### 7.1 状态、数据库与布局

| 文件 | 详细职责、输入输出与调用关系 |
| --- | --- |
| [storage/state_repository.py](../../src/problem_locator/storage/state_repository.py) | `CaseStateRepository` 实现 Case 快照读取、版本提交、幂等/对象/资源索引、归档任务、保留清理和状态导出。核心结构为 `_live`、`_objects`、`_requests`、每 Case 弱引用锁及使用计数；`_apply_mutation` 在合同模型副本上应用变化。`read_snapshot` 支持 case/job/artifact/attachment/request 定位；`commit` 输出 `CommitReceipt`。`on_case_projection` 参与同一事务，`on_case_committed` 在提交后通知；`health`、`validate_all` 和 `export_snapshot` 分别提供健康、显式全量验证和规范快照。持久化失败锁存错误，初始化不匹配归类为 schema/corrupt/instance locked。 |
| [storage/database.py](../../src/problem_locator/storage/database.py) | `SqlDialect`、表/列/索引元数据函数、`bind_qmarks`、会话/配额 advisory lock、`PostgresConnection`、代理、可重入 `ConnectionScope` 与 `PostgresDatabase`。输入为显式 PostgreSQL URL 和连接池大小（2–64，默认 8）；输出为线程范围 DB-API 风格连接。内部校验标识符和 JSON path；只读/写事务与所有权检查见第 3 节。SQLite 保持原生连接对象，不伪装成 PostgreSQL。 |
| [storage/postgres_layout.py](../../src/problem_locator/storage/postgres_layout.py) | `preflight_postgres_root` 在修改前拒绝旧 SQLite/state 文件、残缺 marker、非空无标记目录或错误格式。`marker_bytes` 生成含 installation ID、合同版本和 backend 的规范 marker；`root_identity` 哈希资源根绝对路径，不保存 DSN。`initialize_postgres_root` 将数据库安装身份绑定到目录并同步；`validate_postgres_root` 要求已有有效绑定。 |
| [storage/layout.py](../../src/problem_locator/storage/layout.py) | `StorageLayout` 提供固定目录属性与 `ensure_directories`，逐级验证真实目录并同步父目录。`DATA_FORMAT_MARKER_BYTES` 是保留的 SQLite v11 数据格式标记，与 PostgreSQL 标记不同。空目录识别只接受固定布局和明确许可文件；`initialize_v2_data_root`、`validate_v2_data_format` 拒绝把已有未知内容静默初始化成新安装。`state`/`previous_state` 路径属性为兼容原语保留。 |
| [storage/state_atomic.py](../../src/problem_locator/storage/state_atomic.py) | `AtomicStateFileWriter.write` 接收规范状态字节，在同卷 `tmp/state` 创建新文件并同步，保存前一版，再替换当前 `state.json`，最后读取验证并返回实际字节。注入 `FileSync`、`Replacer` 和读取函数便于故障控制。它不决定业务状态、schema 或事务；当前 `CaseStateRepository` 的生产路径不调用该类。 |

### 7.2 并发、原子文件与路径

| 文件 | 详细职责、输入输出与调用关系 |
| --- | --- |
| [storage/coordination.py](../../src/problem_locator/storage/coordination.py) | `StorageCoordinationLock` 管理按 Case 的可重入发布锁和全局维护屏障，并记录线程持有深度及 publication 深度。`InProcessPublicationCommitGuard` 签发线程绑定的 `PublicationCommitLease`；禁止跨 Case 嵌套。`AttachmentUploadRegistry` 按附件维护 FIFO 队列及活动集合，校验租约所属 registry、ID、线程和释放状态；`InProcessAttachmentUploadGuard` 将其暴露为合同端口。 |
| [storage/platform.py](../../src/problem_locator/storage/platform.py) | `FileInstanceLock` 在安装生命周期持有锁文件，POSIX 用非阻塞 `flock`，Windows 用字节锁。`PlatformFileSync` 同步文件/目录并设置只读权限，`chmod_no_follow` 避免对链接或被替换节点改权限；Windows 目录同步使用系统句柄。`PlatformReplaceOperation` 封装 `os.replace`。这些原语不改变 Linux-only Server 支持范围。 |
| [storage/atomic.py](../../src/problem_locator/storage/atomic.py) | 定义可注入 `FileSync`、`Replacer` 协议。`write_synced_file` 排他创建并同步普通文件；`atomic_write_bytes` 执行同卷临时写入与替换。`read_stable_file_bytes` 比较路径与句柄元数据，拒绝读取漂移；`require_ordinary_file`、`require_real_directory` 和 `is_reparse_point` 为上层提供节点校验。`finalize_read_only_file/tree` 依次完成权限和持久化。 |
| [storage/paths.py](../../src/problem_locator/storage/paths.py) | `StorageAddress`、`parse_storage_key`、`formal_storage_key` 定义正式资源路径语法。`resource_path` 同时检查 resource kind、根内边界和无链接祖先；`validate_data_root` 要求绝对路径。`proposal_directory_name` 哈希任意 proposal key；`proposal_stage_path`、`attachment_stage_path`、`job_directory` 只用已验证 ID 构造路径。`workspace_owner_id` 与 `job_workspace_names` 识别主工作区和 `.logparse-preprocess` 工作区的归属。 |
| [storage/streams.py](../../src/problem_locator/storage/streams.py) | `FileBinaryStream` 为已验证文件提供只向前读取，在 EOF 再核对打开后文件与路径是否稳定；close 幂等。`copy_binary_stream` 在预算内消费一次输入，写入同步暂存文件，计算 `StreamCopyReceipt(size, sha256)` 并关闭来源。`hash_file` 复用严格普通文件读取，不跟随末端链接。 |
| [storage/tree.py](../../src/problem_locator/storage/tree.py) | `inspect_tree` 受控遍历目录，禁止链接和不普通节点，按稳定相对路径生成 `TreeManifest`、总大小和哈希；可同时复制到指定目标。扫描前后检查文件身份与树结构变化。`verify_tree` 重建并比较完整不可变描述；`TreeInspection` 聚合 manifest 和内容摘要，供暂存、发布、读取使用。 |

### 7.3 资源与执行记录

| 文件 | 详细职责、输入输出与调用关系 |
| --- | --- |
| [storage/staging.py](../../src/problem_locator/storage/staging.py) | `StagedObjectWriter` 只负责内容物理暂存。`stage_file_content` 消费 BinaryStream，`stage_tree_content` 受控复制目录，最终分别安装 payload/tree；拒绝已完成目录和相反内容类型。`publish_marker` 在内容持久化之后写最终完成标记，`read_marker` 只读取最终标记，忽略未完成临时 marker。不定义公开 ResourceStore 的业务合同。 |
| [storage/resource_store.py](../../src/problem_locator/storage/resource_store.py) | `FileResourceStore` 实现公开 ResourceStore。`stage_file/tree/attachment/generated_file/archive` 输出带完整内容身份的暂存引用；`validate_staged` 重验完成对象；`plan_target` 分配合法正式 key，`publish` 在有效发布租约内校验并采用。`validate_case_capacity` 按唯一资源 key 计算 Case 容量并利用已知资源信息，`seed_case_resources`/`forget_case` 管理缓存；`open_read`、`materialize_read_only` 和 `discard` 封装安全读取、复制与丢弃。`StagePathRegistry` 使 writer 与 cleaner 对同一路径互斥；底层异常转换为合同规定的 `ApplicationPortError`。 |
| [storage/resource_files.py](../../src/problem_locator/storage/resource_files.py) | 物理正式资源实现。`validate_formal_resource` 对 `ResourceRef` 验证大小、哈希、目录结构和只读状态；`iter_case_resource_nodes` 只枚举元数据，`scan_case_resources`/`scan_all_resources` 才明确读取验证内容。`calculate_case_usage` 按唯一 key 计数。`FormalResourcePublisher.publish` 在共享锁内校验 stage、原子迁移或采用现有目标，并确认移动的是已验证 inode；`FormalResourceReader.open_file/materialize` 负责严格读取和工作区只读副本。 |
| [storage/execution_records.py](../../src/problem_locator/storage/execution_records.py) | `FileExecutionRecordStore` 发布/读取 Job、Outcome、被拒绝模型输出与固定名审计文件。`publish_job`、`publish_outcome_bytes`、`publish_audit_bytes` 返回执行文件引用；规范记录与原始审计字节分别处理，重放只采用相同字节。`open_log_sinks` 创建 stdout/stderr 配对 sink，`_LogSession` 共享总量限制，`_AppendOnlyFileSink` 同步并检测写入错误。文件冲突、丢失、损坏和 I/O 失败映射到各自端口错误。 |
| [storage/log_rotation.py](../../src/problem_locator/storage/log_rotation.py) | `bounded_json_line` 限制单条 JSONL 记录，同时保留事件身份与截断标记。`BoundedJsonlFile.write_line` 在路径锁内轮转固定备份、追加完整 UTF-8 行，`last_record` 查询最后有效记录。`jsonl_segment_paths` 按旧到新列段；`read_jsonl_segments` 对轮转前后快照检查，不静默漏段。`JsonlFileHandler` 将 Python logging 接入相同有界实现。 |

### 7.4 保留与隔离删除

| 文件 | 详细职责、输入输出与调用关系 |
| --- | --- |
| [storage/retention.py](../../src/problem_locator/storage/retention.py) | `RetentionScanner.discover` 根据注入 Clock 枚举 `_RetentionCandidate(kind, path, age_seconds, retention_seconds)`。使用纳秒计算严格过期边界；上传/proposal 优先以最终 staged marker 时间为锚，正式资源以资源父目录时间为锚。它只发现候选，不能判断业务引用，更不直接删除。 |
| [storage/retention_cleaner.py](../../src/problem_locator/storage/retention_cleaner.py) | `StorageRetentionCleaner.run_once` 记录候选元数据，在发布屏障内重查身份、仓库引用、活跃上传/Job/工作区和 stage 租约，再移入隔离目录。锁外删除；`CleanupRunResult` 返回观察、隔离、删除与失败信息，失败路径可再次处理。空父目录清理由专门受保护流程完成，不递归清除尚有内容的 parent。 |
| [storage/quarantine.py](../../src/problem_locator/storage/quarantine.py) | `QuarantineMover.move_if` 只接受固定资源布局内的精确候选，在持有协调锁时执行最后一次 predicate 并原子移动。`discover` 找到以前遗留的隔离节点；`delete` 要求锁外执行，校验隔离根与普通节点后删除，逐级同步并清除空回执目录。用户传入的任意路径不能直接成为候选。 |
| [storage/history_retention.py](../../src/problem_locator/storage/history_retention.py) | `HistoryRetentionService` 是 7 天业务历史清理入口，管理旧 run、整会话、闲置上传、Core Case 以及创建键/删除标记。`_expire_run` 获得会话与 Case 清理租约后，构造包含 Intake/追问工作区的 manifest；`commit_history_cleanup` 原子保存清单并删除引用；`_finish_paths`、`_retry_paths` 重试文件清理。活动诊断、归档、上传、追问及快照构建都能阻止到期删除；结束后可在事务外整理数据库。 |

## 8. 修改存储时的核对点

- 先区分活跃内存状态、网站持久投影、最终 Case 快照和执行文件；不要让一种记录暗中承担另一种记录的恢复语义。
- 新的资源发布必须定义内容身份、最终完成标记、同步顺序、冲突处理和与状态提交的关系。
- 新的后台活动必须接入引用/租约保护，并向显式删除及自然保留清理提供其工作区和所属对象。
- 新表需要明确事务粒度、领取与恢复规则、归属、幂等键、容量和保留时间；SQLite 可运行不表示 PostgreSQL 锁语义已经成立。
- 修改清理逻辑时先证明候选路径、归属和无活动引用，再解除数据库引用或移动文件；失败时保留可检查、可重试的信息。
