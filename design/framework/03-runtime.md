# Runtime 运行时详细设计

[返回框架设计目录](README.md)

本章说明 `src/problem_locator/runtime/` 的模块边界、执行流程和逐文件设计。原有流程以 2026-09-28 源码为基础，2026-11-05 补充文档型 Skill 的注册与执行，2026-11-10 补充可选关键词直达路由。本章记录实现，不代表已完成完整 Test Flow 验证。

## 1. 模块职责与依赖

Runtime 接收应用层已认领、状态为 `RUNNING` 的不可变 `Job`，解析其固定版本资产，准备工作区，调用外部 Agent 或服务端 Logparse 预处理，校验输出并发布 `RuntimeExecutionReceipt`。它不直接修改 Case、Diagnosis 或 Candidate 的业务状态；业务接收与状态推进由应用层负责。

| 依赖方向 | 使用内容 | 所有权与约束 |
| --- | --- | --- |
| `contracts` | Job、Outcome、资源引用、版本引用、执行阶段、失败对象和端口 | 对外数据与错误词汇以合同层为准，Runtime 不另造一套公共协议。 |
| `application.formalization`、`domain.methods_state_v2` | Methods V2 结果与状态转换 | 供保留的 V2 实现调用，不据此认定 V2 是当前默认执行链。 |
| `StateRepository` | 读取 Job 所属 Case 聚合 | 核对 Job、Case、资源归属；执行依据仍是 Job 冻结的引用与上下文。 |
| `ResourceStore` | 复制输入、暂存与验证输出资源 | Agent 只能接触工作区副本；资源晋升和业务接收不在 Runtime 中完成。 |
| `ExecutionRecordStore` | 原始响应、审计、日志、Outcome | Outcome 发布成功后返回其持久化引用；发布状态不明确时必须回读。 |
| `AssetCatalogPort` | 固定 profile、Skill、工具、上下文策略、输出合同 | 引用同时包含版本和内容摘要，不能悄悄换成目录中的新版本。 |
| `integrations.logparse` | 请求、broker、受控结果树与路径校验 | 预处理由服务端掌握，模型不能自行扩大已冻结的目标范围。 |
| 外部 Claude 兼容 CLI | 执行提示词，返回 stream-json 或工作区结果文件 | 每次新建进程；取消、超时、输出量、工作区量和工具策略由服务端检查。 |

当前部署默认 `METHODS_EVIDENCE_VALIDATION=off`，来源是 [settings.py](../../src/problem_locator/entrypoints/settings.py)，[bootstrap.py](../../src/problem_locator/bootstrap.py) 将配置显式传给目录和 Runtime。这与类构造器的独立默认值不同：`VersionedAssetCatalog` 为 `strict`，`DiagnosisRuntime` 为 `advisory`。理解产品默认行为应看实际装配参数，不能只看某个构造器。

| 专用定位模式 | 模型交付 | 服务端后处理 | Reviewer |
| --- | --- | --- | --- |
| `off` | 状态首行与完整 Markdown 报告 | 校验包络、非空正文、65,536 UTF-8 字节上限和私有能力泄露，保留正文；不按引用匹配结果重写结论 | 强制 `ReviewPolicy.NONE` |
| `advisory` | Methods V1 诊断 JSON | 保留可识别的模型判断；核对固定输入身份，生成带限制说明的结构化结果，不把模型引用冒充逐项验证过的证据 | 由配置决定；审核整份结果 |
| `strict` | Methods V1 诊断 JSON | 核对实际引用和输入摘要，逐条保留可用发现；从保留证据推导确认方法 | 由配置决定；按已知发现对齐审核，缺项或冲突按证据不足处理 |

`off` 关闭的是框架对 Skill 结论的证据一致性后处理。资产身份、输入准备、Logparse 受控执行、进程限制和交付格式仍然有效。

## 2. 主要执行流程

### v3 文档型专用定位

`specialized_skill.py` 按注册版本分派加载器：v1/v2 使用原 `methods_skill.py`，v3 使用 `document_skill.py`。v3 校验来源摘要、三类输入、单个材料集合、准备方式和 Markdown 引用完整性；只接受平台支持的材料格式和普通文档包。目录注册后保存内容快照，后续执行与可选参数策略都使用任务固定的 `skill_ref`，不读取被部署方替换的新版本。

`catalog.py` 为 v3 固定 `skill-direct` 运行配置和 `ReviewPolicy.NONE`。`diagnosis_runtime.py` 在 Methods 预检前转入 `document_runtime.py`，后者按 `none/direct_files/logparse` 准备材料。直接读取复用 `direct_generic_logs.py` 的文件校验、安全解压和容量限制；Logparse 复用 broker、回执和结果校验，再转换为统一清单。没有调用解析器的分支不生成解析回执。

三种模式都保存只读 `inputs/request.json`、`inputs/evidence-manifest.json` 和 `inputs/skill/` 文档。请求保留原始问题和已接受参数，清单记录文件来源、大小、SHA-256、跳过项和限制。入口文档进入基础上下文；参考资料不受 marker 命中限制，按固定文档清单的顺序，将全部原文及路径边界追加到最终提示词。完整提示词（含续接上下文、说明和文档边界）加上必读请求、材料清单的 UTF-8 字节数须同时符合 `128 KiB` 内联阈值和 Job 上下文限额，否则全部参考资料保留为按需读取的完整文件，不截断或选择部分文档。`document-prompt.txt` 记录实际交给后端的完整提示词，`context.txt` 保留基础上下文；参考资料始终是 Skill 文档，不进入日志证据清单。准备耗时计入 Job 总时限；执行后重新核对请求、清单、材料和 Skill 文档的内容与文件身份，再发布状态标记和 Markdown 报告。

必填参数生成现有 `PendingRequirement`，可选参数不生成待办。`SpecializedInputPolicy` 只在应用层、Intake 和领域补充流程中临时传递，不写入 Job 或 State V11。v3 不使用通用定位专属的 `generic_log_preparation`，也不进入 Methods 的证据核验和 Reviewer。

### 2.1 ROUTE：关键词直达、最多 5 个候选与简短选择

1. `RuntimeAssetResolver` 解析 Job 固定的资产与全部候选 Skill，生成包含 `routing` 的索引。
2. 无候选，或所有候选都未声明适用范围时，直接发布 `NO_CAPABILITY`；后一种情况保存 `model_called: false` 的准入审计，不调用模型。
3. 先以真实 `Case.raw_problem_text` 检查作者显式声明的 `routing.direct_match_keywords`。仅首轮、唯一 Skill 字面命中且无阻止条件时跳过模型，保存 `direct_keyword_route_v1`、`model_called=false` 的独立准入审计。
4. 无法直达时，`recall_route_candidates()` 从本次固定问题与活跃事实召回最多 5 个正分候选；零命中直接发布通用结果。`ContextBuilder` 仅收到精简候选索引，候选的适用和排除条件完整保留，完整目录仍在服务端。
5. Router 不允许文件工具，最终只返回 `skill_id`、`reason`、`confidence`。模型负责适用性判断，服务端不再逐条件核对引文；选择属于本次候选、固定版本有效且置信度不低于 `0.95` 时才能 `MATCHED`。
6. 额外字段包括历史 `assessments` 均忽略。根字段缺省、候选外 Skill、空响应或无法解析的 JSON 正常转通用；理由仅用于展示，置信度接受数字字符串，无法使用时按 0 处理。这些情况不记录异常或警告、不重试模型。发布前 `server_outcome_finalizer` 核对准入记录与本次 Job、候选范围、完整目录和草稿摘要；关键词路径继续根据原始问题和冻结索引重新计算命中结果。

两条路径都保留 ROUTE Job，并由正常 Outcome 接收流程推进到 DIAGNOSE。关键词直达只省去路由模型调用，不绕过专用流程的参数和材料收集。`previous_outcome_refs` 非空时不使用直达，避免把首轮原文误当成后续路由的新依据。

逐字引用只证明引用来自本次冻结输入，不证明模型的语义判断正确。

### 2.2 专用定位：先固定输入，再运行 Skill

1. 根据注册配置、用户事实和附件检查必需输入。缺少材料时生成补充要求，先不启动诊断模型。
2. `compile_resolved_logparse_plan()` 编译具体问题时间、角色、slot、进程和可选 PID。复用已有 `LOGPARSE_RUN` 时固定其 Artifact；否则固定本次日志附件。
3. 服务端建立独立 `logparse-preprocess` 工作区并执行预处理。主诊断工作区最初只写元数据，不再复制原始归档。
4. 从 broker 审计重建目标日志集合，重新读取并校验受控源树、目标日志和摘要，再将目标字节、请求与回执写成冻结输入。Specialist 不获得 broker 能力。
5. 完整扫描日志中的方法 marker，按命中加载方法卡。`off` 允许没有可交付目标日志时使用空扫描回执。过大的完整 Skill 材料写到 `inputs/methods-package.txt`，不会只截取部分卡片。
6. 所有必要输入不超过 `128 KiB` 时完整内联；超过时列出完整输入文件。两种情况都使用 `diagnose` 策略，允许 `Read`、`Grep`、`Glob`、`Bash`。文档型 v3 专用定位也使用同一策略，按其请求和材料清单读取输入。可选 marker 行号索引超过 `8 KiB` 时整体省略，不能截成不完整清单。
7. `off` 解析 Skill 状态包络，直接发布 Markdown 型 `JobOutcome`。其他模式先解析 Methods V1 JSON，再分别进入 advisory、逐项选择或 strict 核验。
8. 结构化路径将 Methods 结果映射为领域草稿，服务端生成最终 Outcome、Result v3 和归档计划，暂存资源，最后发布执行回执。

### 2.3 独立 REVIEW

REVIEW 使用新的工作区和进程，读取固定 Candidate、Evidence、此前 Methods 诊断及服务端审计，不能继续 Specialist 会话。当前 Methods V1 REVIEW 输出写入 `output/method-review.draft.json`，与 Router 和 Specialist 使用最终响应的方式不同。审核不获得 Logparse 能力；strict 要求准确覆盖每个 `(method_id, identity_tokens)`，advisory 保留整份结果审核语义。

### 2.4 GENERIC：预装 Skill 黑盒执行

`GenericLocatorExecutor` 将原问题原文、分段补充内容和输出合同交给配置的预装 Skill。有日志时，服务端先解析归档，只提供 `inputs/generic_logs.json` 及按摘要复制的只读日志，模型按需读文件。此路径不要求先提供问题时间、slot、进程或 PID。

可选历史经验最多占 `4096` UTF-8 字节，并且必须完整放入剩余上下文预算；检索失败或放不下时整段省略，不挤占问题与合同。日志解析成功但没有可分析文件时，服务端直接生成 `UNRESOLVED` 报告，不调用模型。

当前优先交付 `output/generic_diagnosis_result.md` 的 V2 Markdown 报告，同时保留读取旧 V1 文本格式的能力。两种文件同时出现属于歧义，必须拒绝。接收报告前再次检查日志与清单是否发生变化。

### 2.5 保留的 Methods V2 实现

目录仍保留 Evidence Graph、Evaluation Plan、双角色求共识、PRIMARY/REPAIR、失败重放和终态映射。源码中 `_execute_methods_v2()` 当前仅有定义，没有现行执行分支调用；V1 Skill 校验还明确拒绝旧 V2 输入名称。以下逐文件说明保留其真实接口，但不能把它们画成部署默认主链或宣称开启 Reviewer 就切换到 V2。

## 3. 核心运行与资产文件

### `diagnosis_runtime.py`

源码：[diagnosis_runtime.py](../../src/problem_locator/runtime/diagnosis_runtime.py)

`DiagnosisRuntime` 是整个模块的编排入口，注入状态仓库、资源仓库、资产目录、broker factory、执行记录、时钟、ID 生成器、工作区管理器和后端。Router 与诊断可使用不同后端，未指定时共用默认后端；进度回调只暴露诸如 `ROUTING`、`LOGPARSE`、`DIAGNOSING`、`VERIFYING`、`REPORTING` 的阶段。

`execute(job, cancellation)` 负责统一错误边界，`_execute()` 选择 ROUTE、GENERIC、专用定位和 REVIEW 路径。资源解析早于可变应用状态读取。读取聚合后仍核对 Job 与持久化记录是否相同，只有冻结引用可成为输入。

主要辅助职责：

- `_methods_preflight_state()`、`_publish_methods_preflight()` 将缺失用户参数或日志转为补充要求；`_publish_generic_preflight()` 处理通用日志任务尚未拿到附件的情况。
- `_run_methods_preprocessing()`、`_prepare_generic_logs()` 在产品拥有的预处理工作区调用 broker，记录请求与审计，检查取消、时限和空间。
- `_materialize_context()` 保存本次实际上下文；`_execute_backend()` 管理后端与 broker 的生命周期，最终关闭 broker 并归档其审计。
- `_prior_methods_diagnosis()`、`_methods_review_subject()` 重建 REVIEW 的准确对象；不能从当前模型文字猜测待审核范围。
- Methods V1 分支保留原始响应、JSON 提取回执、核验或选择审计，再依次调用 `map_verified_methods_draft()`、`finalize_server_outcome()`、`stage_validated_output()` 和 `OutcomePublisher`。
- V2 的 `_commit_method_*()`、`_run_method_role_attempt_v2()`、终态及资源漂移处理保留了 Graph/Plan/State 发布和有界修复逻辑，但不属于当前入口调用链。

普通执行错误归一为 `ExecutionFailure` 并尝试发布失败 Outcome。`STATE_CORRUPT`、`STATE_SCHEMA_UNSUPPORTED` 的端口错误不吞掉；`RuntimeInfrastructureError` 表示执行记录无法可靠发布或确认，向外传播。GENERIC 的普通可重试失败会被转为不可重试，避免自动重复黑盒 Skill。发布前明确失败可以清理未引用暂存资源；发布状态不确定时不能先清理可能已被 Outcome 引用的数据。

### `catalog.py`

源码：[catalog.py](../../src/problem_locator/runtime/catalog.py)

`VersionedAssetCatalog`（别名 `AssetCatalog`）加载内置 profile、工具包、上下文策略和输出合同，扫描外部 Skill 注册目录，维护 `(id, version, content_hash)` 到 `ResolvedAsset` 的映射。Methods Skill 需要 Logparse 时，必须配套有效的 Logparse 资产。

`freeze_assets()` 在启动时复制资产，并在复制后重新核对内容摘要，再注册进程内快照。Logparse 外部工具不在这次复制循环内。固定快照根由调用方在实例锁下管理；`close()` 只能在所有运行使用者停止后释放。

`route_bindings()` 返回全部符合目录规则的候选，不按初始用户事实名筛选。`diagnose_bindings()` 根据证据模式选择 specialist 或 skill-direct profile/合同，`off` 同时关闭 Reviewer。`generic_diagnose_bindings(with_logs=...)` 单独决定是否绑定通用日志解析产品。`review_bindings()` 固定审核角色且不提供 Logparse。`check()` 和 `resolve()` 拒绝不再可用的固定版本。

### `catalog_hash.py`

源码：[catalog_hash.py](../../src/problem_locator/runtime/catalog_hash.py)

`product_directory_entries()` 按相对 POSIX 路径排序，记录普通文件的路径、大小和 SHA-256；`hash_product_directory()` 对版本化清单做规范 JSON 摘要。扫描拒绝符号链接、非普通节点、危险路径及重复文件身份，Windows 使用句柄补充文件身份检查。`.pyc`、`__pycache__`、`.pytest_cache`、`.DS_Store` 和管理标记不参与产品内容摘要；这份排除规则由源码集中定义。

### `asset_snapshot.py`

源码：[asset_snapshot.py](../../src/problem_locator/runtime/asset_snapshot.py)

`register_snapshot()` 保存启动快照内的文件字节、资产引用、注册解析结果和 `asset.json` 内容。`snapshot_asset()`、`snapshot_bytes()`、`snapshot_manifest()` 给运行阶段提供固定视图，`release_snapshots()` 按快照父目录释放缓存。注册与释放用进程内 `RLock` 保护；`snapshot_bytes()` 对未缓存文件仍会读磁盘，因此调用方必须先确定文件属于已验证资产。

### `context_policy.py`

源码：[context_policy.py](../../src/problem_locator/runtime/context_policy.py)

`RuntimeAssetResolver.resolve_job()` 解析 Job 指定的各类资产，校验类型、版本和内容身份，读取入口文本。`ResolvedJobAssets` 保存尚未附加工作区的资产结果；`bind_workspace()` 再生成 `ResolvedContextAssets` 和 `ContextMaterials`。

Skill 索引包含固定引用、能力说明和路由条件。专用 Skill 只加载已选方法卡与共享参考；Methods V1 Specialist 不向模型重复暴露历史 Outcome，REVIEW 也不把全部旧 Outcome 混入上下文。V2 的 Graph 与 Plan 必须成对传入，选卡顺序必须与计划一致。资产入口要求安全相对路径与普通文件，不能用 manifest 引用逃出资产目录。

### `context_builder.py`

源码：[context_builder.py](../../src/problem_locator/runtime/context_builder.py)

`ContextMaterials` 是 profile、Skill/索引、工具声明、输出合同、工作区清单、证据和历史结果的输入集合。`ContextBuilder.build()` 返回 `BoundedContext`，同时记录最终 UTF-8 字节数、正文 SHA-256 和各段来源。

构造顺序是角色指令、Skill 或目录、工具说明、Job 指令、冻结上下文、未满足要求，随后插入对应角色的证据或审核目标，最后保留输出合同和资源清单。所有必需段先占预算；可选 Evidence 按冻结顺序逐段尝试加入，只能整段保留或省略。必需段已经超限时抛出 `ContextLimitExceeded(observed, limit)`，不裁剪合同或 Skill。

Methods 输入中的命名事实以 `request.json` 为唯一模型可见来源，因此不会再在 snapshot 段重复值。REVIEW 清除上下文中不属于固定审核对象的假设、开放问题等信息。`build_methods_*_method_cards_v2()` 和 `build_methods_reviewer_manifest_v2()` 为保留的 V2 路径提供角色最小输入。

### `workspace.py`

源码：[workspace.py](../../src/problem_locator/runtime/workspace.py)

`WorkspaceManager` 在 `DATA_ROOT/tmp/workspaces/<job_id>` 新建独占目录，预处理阶段使用 `<job_id>.logparse-preprocess`。固定结构为 `inputs/`、`runtime/tool-state/`、`output/proposals/`，已存在目录不能作为新的 Job 工作区复用。

`prepare()` 从 `ResourceStore` 复制冻结附件、Evidence 和 Artifact，检查大小、摘要及目录清单，再写 `inputs/manifest.json`。`prepare_fresh_methods_specialist_main_metadata_only()` 与 `prepare_generic_main_metadata_only()` 只建立主工作区元数据；实际归档仍在服务端预处理工作区。

`PreparedWorkspace` 保存根目录及重要子目录的设备号、inode、清单字节与输入对象。后续读写不能只信任路径字符串，还要确认目录身份没有被替换。平台支持时使用目录描述符和相对打开；其他平台使用路径祖先、文件身份、普通文件类型和前后元数据校验。输入设为只读，同时继续依赖摘要与身份复核，不能将只读权限当成唯一保护。

`freeze_methods_inputs()` 将服务端捕获的目标日志、请求和 Logparse 回执写入主工作区，返回持有原始字节的 `FrozenMethodsWorkspaceInputs`。`freeze_methods_review_inputs()` 固定此前诊断与审计；`freeze_methods_package()` 保存过大的完整材料。V2 的 `publish_methods_specialist_inputs_v2()`、`publish_methods_reviewer_inputs_v2()` 只发布角色所需的最小请求与清单，并确认没有多余输入节点。

`write_context()` 安全写入 `runtime/context.txt`；`temporary_output_bytes()` 计算临时输出；`read_claim()` 读取有界 Logparse claim。输入资源不匹配、链接或目录替换、跨 Case 引用、摘要漂移和空间异常都会阻止继续执行。

### `input_profile.py`

源码：[input_profile.py](../../src/problem_locator/runtime/input_profile.py)

加载并校验唯一内建输入模板。`load_builtin_input_profile()` 返回缓存的深拷贝，调用方不能修改全局模板；`canonical_profile_bytes()` 与 `builtin_input_profile_sha256()` 为生成器和 Runtime 提供同一份内容身份。

`expand_profile_requirements()` 将全局问题时间、每个角色的 slot/process_name/可选 pid，以及需要时的日志归档展开为平铺要求，补上 `origin`、`role` 等来源信息。字段集合、字符串字节范围、正则、MIME 类型、附件数量和补充策略均在读取阶段检查。

### `methods_skill.py`

源码：[methods_skill.py](../../src/problem_locator/runtime/methods_skill.py)

严格区分作者生成的诊断包和产品拥有的注册配置。`load_methods_package()` 要求包根只有 `SKILL.md`、`methods.json`、`references/`；引用文件必须是普通 Markdown。校验目录名、frontmatter、索引 Skill 名一致，输入/产物/日志派生字段互不重叠，方法 ID、优先级、marker、共享引用和方法卡引用完整闭合。

方法卡需有“适用条件、所需证据、计算与判断、确认条件、未知边界、输出含义”六节。源日志模板的固定片段与方法 marker 需要一致，不能把带占位符的完整模板当成日志中必然出现的连续文字。V1 Skill 必须声明指定诊断协议，不能要求 `method-evidence-graph.json`、`evaluation_input` 等旧输入。

`load_specialized_skill_registration()` 要求注册根只有 `registration-template.json` 和 `package/`，后者恰有一个真实 Skill 目录。注册 V1 没有路由范围，V2 增加 `routing.applicability` 与 `routing.exclusions`；每组最多 16 条，适用条件至少一条，条件 ID 在整个 Skill 内唯一。运行角色绑定限定为框架已有 ID，预处理绑定核对角色与 Logparse 计划。

V2 与文档型 V3 共用 `RoutingScopeV1`，可额外声明 `direct_match_keywords`，由 `routing_keywords.py` 统一校验。缺省或空数组保持原路由行为，索引也不输出此字段；有值时关键词随注册摘要和目录快照一起固定。修改注册需重启服务，再由新 Case 使用，不能覆盖已有 Job 的固定引用。

输出 `ResolvedSpecializedSkillV1` 同时保存注册 SHA-256、包树 SHA-256 和两者组合摘要。`load_registered_skill_from_package()` 是提供给 Test Flow 的简化读取面。`MethodCardV1`、`MethodsManifestV1`、`RegistrationTemplateV1` 等不可变值对象供目录、选卡、预处理与核验共享。

## 4. Agent 进程、响应和追问边界文件

### `agent_backend.py`

源码：[agent_backend.py](../../src/problem_locator/runtime/agent_backend.py)

`AgentBackend.execute()` 接收提示词、工作区、取消信号、日志 sink、资源限额和可选 broker 环境，返回 `BackendExecution`，包含退出码、日志字节数、工作区字节数、耗时和可用最终响应。`BackendExecutionLimits` 从合同限额构造，也接受测试专用覆盖。

执行前准备无 shell 命令、应用本阶段工具策略、记录工作区身份。stdin、stdout、stderr 使用非阻塞管道和独立线程，输出按 stdout/stderr 合计限额计数，私有 broker 字节先脱敏再写日志。主循环检查取消、写入失败、输出超限、墙钟时限和工作区大小；运行中扫描允许受控瞬态变化，结束后再次严格检查。

主进程退出码为 `0` 且尚未失败时，主循环继续等待进程树清空和全部 I/O 线程结束。两者共享一个从首次观察到正常退出起算的 10 秒窗口，由内部 `normal_exit_grace_seconds` 控制；提前结束就立即返回，不分别累加等待。窗口内继续响应取消、总时限、输出错误和工作区限制，工作区扫描也可被收尾期限中断。收尾时间计入原有执行总时限；超过收尾窗口仍未结束时，清理进程树并返回 `BACKEND_EXIT_FAILED`。非零退出和既有失败保留原有处理，终止宽限 `termination_grace_seconds` 不变。

任何退出路径都负责终止整个进程树、结束管道线程、关闭 sink 并恢复工作区权限。进程退出成功不等于任务输出有效：最终还要检查遥测输出限额、工具调用集合和最终工作区边界。日志无法完成写入时归为执行记录失败。遥测事件发送失败本身不能改变 Job 结论，但用于工具政策判定的遥测缺失或格式异常不能被当成已获许可。

### `claude_command.py`

源码：[claude_command.py](../../src/problem_locator/runtime/claude_command.py)

`parse_command_tokens()` 将配置中的单条 `CLAUDE_COMMAND` 拆成 argv 和前置环境赋值，不启动 shell。`sanitize_environment()` 去掉环境中已有的 Logparse 仓库、配置和 broker 能力变量，并禁止命令自行覆盖保留键；只接受本 Job 注入的 endpoint/token。Windows 另外处理 PATH 大小写与可执行 shim 查找。

`apply_final_response_policy()` 支持 `none`、`read-only`、`read-search`、`diagnose`。对已识别的 Claude、codeagent 或相应 Node CLI，重写工具、权限和 stream-json 参数，同时保留运营方模型、端点等配置。`none` 不允许文件工具，`read-only` 仅允许 `Read`，`read-search` 允许 `Read`、`Grep`；专用定位的 `diagnose` 允许 `Read`、`Grep`、`Glob`、`Bash`，不随输入是否内联而改变。自定义 wrapper 从 `PROBLEM_LOCATOR_AGENT_FILE_ACCESS` 接收策略，必须执行对应工具限制，并用 stream-json 提供最终响应和完整工具事件供服务端审计。

`diagnose` 的 `Bash` 不属于只读沙箱，工具策略不保证命令只能访问 `inputs/`。运行时继续固定只读材料、检查执行后的输入完整性，并审计实际工具集合。Skill 包本身的脚本、工具和依赖限制保持不变。

报告追问只接受已知可执行策略的 CLI 或仓库拥有的 `isolated-agent-wrapper.mjs --workflow report-followup`。`supports_read_search()` 提供提前检查。追问阶段生成独立 settings，隔离继承的插件、MCP、slash commands、浏览器接入和会话保存；未知 launcher 不能获得原始日志输入。

### `process_tree.py`

源码：[process_tree.py](../../src/problem_locator/runtime/process_tree.py)

`spawn_managed_process()` 创建 `ManagedProcess` 并把进程放进本次执行拥有的终止边界。POSIX 使用独立进程组；Windows 使用 Job Object，先建立并确认归属再恢复运行。`is_tree_empty()` 只查询父进程及其进程组或 Job 是否已全部退出，不发送终止信号、不释放归属；已关闭对象直接返回缓存的清理结果，不再查询旧进程组或句柄。正常收尾由 `AgentBackend` 统一轮询，完成后调用 `close_after_exit()` 确认并释放归属。`terminate_tree()` 先给终止宽限时间，再强制停止整个组；`close_after_exit()` 发现残留进程仍会清理并返回失败。内部 Windows 结构与句柄函数只承担平台机制，不代表 Windows 是受支持的 Server 部署平台。

### `agent_telemetry.py`

源码：[agent_telemetry.py](../../src/problem_locator/runtime/agent_telemetry.py)

`AgentStreamTelemetry` 增量读取 stream-json，记录提示词写入、assistant 文本/思考块数量与字节数、工具调用次数、工具耗时区间、模型 token 用量和 CLI 时间。`TelemetryTeeSink` 在保留原日志 sink 语义的同时旁路观察输出。

`final_result` 只采用唯一成功的 terminal result；诊断事件不包含最终报告正文。`permits_file_access()` 检查实际工具名称集合是否符合当前策略，无法解析、超限或内部失败时拒绝。新实现同时观察 partial tool stream：在完整消息到来前出现的工具 ID、名称和参数事件也必须一致，不能靠只检查完整消息漏掉工具调用。

### `secret_redactor.py`

源码：[secret_redactor.py](../../src/problem_locator/runtime/secret_redactor.py)

`StreamingSecretRedactor` 对 exact bytes 匹配，将命中的每个字节替换为一个 `*`，保持输出计量长度。缓存最多 `最长秘密长度 - 1` 个待提交字节，以捕获跨 chunk 的秘密；`flush()` 不提前释放可能构成匹配的尾部，`close()` 写完尾部并擦除缓存与模式。此处是已知 broker token 的精确脱敏，不是泛化的个人信息识别器。

### `followup_access.py`

源码：[followup_access.py](../../src/problem_locator/runtime/followup_access.py)

报告追问专用的服务器端文件工具边界，也是本次 Agent 的 `PreToolUse` 命令。`permits_tool()` 只接受 `Read.file_path` 和 `Grep.path`，路径必须落在当前工作区 `inputs/`，逐级检查不能含符号链接，叶子普通文件不得有多重硬链接。Read 只接受文件，Grep 可以针对文件或目录。

`prepare_settings()` 从配置保留供应商相关环境与 model，重新创建本次追问 settings，并以独占方式写入 `runtime/followup-settings.json`，权限设为 `0600`。`main()` 从 stdin 读取工具请求，返回 allow/deny；生成的 hook 命令以 `|| exit 2` 保证命令损坏时阻止工具调用。

该文件运行在 Linux Server 的报告追问 Agent 环境中，与客户端“不安装 Problem Locator Hook”的部署边界是不同位置。追问 HTTP、会话授权、资源选择与持久化由 `followup/` 等模块负责，本文件只检查已选工作区内的读取范围。

### `final_response.py`

源码：[final_response.py](../../src/problem_locator/runtime/final_response.py)

`parse_route_response()` 将最终 JSON 交给路由准入模块，并建立由服务端固定身份的 `AgentJobOutcomeDraftV2`。`parse_specialist_response()` 读取 Methods V1 JSON；需要 advisory 或逐项选择时保留原 evidence 数组，避免整体强类型解析提前丢掉仍可处理的项目。

`specialist_prompt()` 返回 `(prompt, inputs_inlined, complete_input_bytes)`。它计算完整上下文、请求、目标清单、回执、方法包和全部日志是否能内联，不取样、不截断。`_specialist_marker_index()` 只渲染本次服务端扫描回执，核对 Skill 和日志身份；索引只是找行辅助，命中不等于确认结论。

### `model_json.py`

源码：[model_json.py](../../src/problem_locator/runtime/model_json.py)

只对模型输出提供格式兼容。`normalize_model_json_bytes()` 识别开头 BOM、外围空白和完整 JSON 围栏，语言标签不区分大小写。`extract_model_json_bytes()` 在不超过 `1 MiB` 的混合文本中读取唯一完整对象，接受行内 JSON、前后说明及围栏展示差异；多个对象或损坏的外层仍不能当作一个结果。`parse_model_json_response()` 解析实际 JSON 字节，返回规范字节和可选 `ModelJsonExtraction`，不增加格式异常日志。

提取回执记录原始/有效字节摘要、长度、范围和规则名，不把原始文本塞入遥测。此机制不修复用户 MCP 参数，也不修改 JSON 字段含义。

### `route_json.py`

源码：[route_json.py](../../src/problem_locator/runtime/route_json.py)

保留旧三字段 ROUTE 响应的有界引号恢复工具。`parse_route_json_bytes()` 先严格解析，失败后只尝试对可唯一确定的 `reason` 字符串中的裸引号补反斜杠；恢复候选最多 `128` 个、输入最多 `64 KiB`，不修复控制字段、重复键、截断或分隔符。`RouteQuoteRecovery` 保存精确插入位置和摘要。

当前 ROUTE 主入口直接走 `parse_model_json_response()` 和 `route_admission.py`，没有调用该恢复函数；`output_reader.py` 仅保留其回执类型。因此不能把这段旧兼容代码描述成当前 Router 会自动纠正无效 JSON。

### `route_admission.py`

源码：[route_admission.py](../../src/problem_locator/runtime/route_admission.py)

`evaluate_route_admission()` 接收模型三字段对象、固定 ROUTE Job、完整 Skill 索引及本次召回结果，返回 `RouteAdmissionResult(decision, audit)`。服务端完整索引仍为版本 3，准确覆盖 Job 的版本引用；模型候选投影为版本 1，仅包括本次召回的有范围声明的 Skill。

`read_route_catalog()` 核对完整服务端索引与 Job 固定引用。模型响应只读取 `skill_id`、`reason`、`confidence`；`evaluate_route_admission()` 复用提示词采用的 `RouteCandidates`，不再读取 `assessments` 或逐条件引用。零候选、无选择、候选外选择和低置信度均为正常通用结果，否则为 ADMITTED。数字字符串置信度可用，缺失或无效时按 0 处理；不增加异常、警告或模型重试。

模型路由策略为 `shortlisted_route_v1`。审计保存模型与实际选择、正常回退原因、`retrieval` 及固定上下文、完整目录和草稿摘要。完整目录仍核对身份，但未入选项不再参与适用性判断，也不标记为语义上的 REFUTED。

### `route_recall.py`

源码：[route_recall.py](../../src/problem_locator/runtime/route_recall.py)

`recall_route_candidates()` 以当前问题字段、活跃用户事实和已确认事实为检索输入；不读取假设或历史结论。英文完整标识符及分段、中文连续二字片段用于确定性匹配，过滤故障泛词，保留 IO/DB 等短组件名。名称和能力权重 3、适用条件权重 2、简介权重 1，同词取最高权重、不重复累计；稀有词按 `1 + ln((N + 1) / (df + 1))` 加权。明确关键词命中数优先，其次词语得分，最后按 Skill ID 排序。

返回 `RouteCandidates(skill_ids, model_index, audit)`：最多 5 个有命中的候选、不凑数，模型索引只含 ID、名称、能力、简介及完整条件；审计保留 `lexical_top5_v1`、全量排名、实际候选和输入摘要。排除条件、材料需求、版本哈希不参与打分。词法召回不能保证同义表达不漏选；未入选表示未召回，不代表已排除。

### `routing_keywords.py`

源码：[routing_keywords.py](../../src/problem_locator/runtime/routing_keywords.py)

`parse_direct_match_keywords()` 接受 0～16 项字符串数组，返回不可变 tuple。每项必须为 3～128 个字符，不含首尾空白或控制字符，按原始文本去重；与超时、失败、错误、故障、异常、timeout、error、failure、slow、rpc 这些泛词完全相同的声明按忽略大小写规则拒绝。

这些关键词是作者明确确认足以选择整个 Skill 的充分条件，不是摘要分词或日志 marker。生成器与服务端都不自动补出现场未确认的关键词；有排除条件的 Skill 可以保存声明，但不能靠它跳过模型审核。

### `keyword_route.py`

源码：[keyword_route.py](../../src/problem_locator/runtime/keyword_route.py)

`try_direct_keyword_route()` 核对 ROUTE Job、全部冻结候选和真实 `Case.raw_problem_text`，仅在唯一 Skill 命中时返回 `RouteAdmissionResult`，否则返回 `None`，继续本地召回与模型选择。匹配区分大小写，不做正则、模糊或语义扩展；关键词首尾为 ASCII 字母、数字或下划线时，检查相邻字符，避免命中更长标识符内部。

首轮以 `previous_outcome_refs` 为空为条件。后续轮次、多个 Skill 命中、命中 Skill 有 `exclusions`，以及常见否定、疑似、历史、引用、示例和假设等词法信号，均交回模型；引用范围和代码块也保守处理。这些有限规则不能保证理解任意自然语言，关键词本身的充分性仍须由作者确认。

独立审计策略为 `direct_keyword_route_v1`，记录 `model_called=false`、原始问题/冻结上下文/索引的摘要、所选 Skill ID 和匹配位置。位置以 Unicode 码点计数；`confidence=1.0` 表示确定性的字面命中，不是模型置信度。发布时重新计算结果，不把这份记录伪装成模型逐项审核。

### `skill_direct.py`

源码：[skill_direct.py](../../src/problem_locator/runtime/skill_direct.py)

`parse_skill_direct_response()` 使用 `report_text.read_report_text()` 读取状态行和 Markdown，兼容 BOM、外围空白、大小写和明确包住状态行的代码围栏；缺少或未知状态时保留正文，按 `UNRESOLVED` 交付。普通 Markdown 代码块不剥离。正文仍要求非空、UTF-8 字节数在预算内且不包含私有能力 token；服务端计算 report SHA-256，返回 `GenericDiagnosisOutcomeV2` 并固定 Skill 名。它不会增加证据不足结语，也不会把专用 Job 改成 GENERIC Job。

## 5. Logparse 与通用定位文件

### `resolved_logparse.py`

源码：[resolved_logparse.py](../../src/problem_locator/runtime/resolved_logparse.py)

`compile_resolved_logparse_plan()` 读取已固定 Skill 的预处理配置与 Job 的用户事实，将 `USER_FACT` 和 `SKILL_FIXED` 绑定编译成 `ResolvedLogparsePlan`。必需角色始终启用；可选角色在提供其相关事实名后启用。一个输入名只能对应一条来源为 `USER_INPUT` 的事实。

计划必须唯一绑定一个已有 `LOGPARSE_RUN` 或一个 Job 附件；不能选择多个运行产物或工作区外附件。缺少有效但尚未提供的参数抛出 `ResolvedLogparsePlanNotReady`，与注册结构损坏的 `ValueError` 区分。无 Logparse 绑定时返回 `None`。

### `authoritative_targets.py`

源码：[authoritative_targets.py](../../src/problem_locator/runtime/authoritative_targets.py)

`resolve_authoritative_targets()` 以冻结计划决定目标身份及顺序，以 broker 审计中的唯一成功操作决定真实来源与匹配结果。来源只可能是输入 Artifact 或受控输出 proposal，不接收模型自报路径。`validated_successful_broker_record()` 核对 Job、请求摘要、操作类型和成功记录数。

`AuthoritativeTargetLog` 保存请求和实际选择的模块、slot、进程、PID、cycle、匹配状态与路径；`AuthoritativeTargetSet` 保存完整集合和源树摘要。`exact`、`nearest` 可交付，`missing`、`ambiguous` 保留为缺口。归档名由语义字段生成，限制危险字符、保留设备名、长度和大小写不敏感冲突。`require_deliverable()` 用于要求所有目标完整可用的路径。

### `generic_logs.py`

源码：[generic_logs.py](../../src/problem_locator/runtime/generic_logs.py)

`freeze_generic_logs()` 将解析器清单中的文件流式复制成 `inputs/generic-logs/log-000001.log` 等固定名字，生成 `inputs/generic_logs.json` 并恢复只读权限。每次复制检查大小、SHA-256、源文件身份、链接数和前后元数据，支持取消，不把整份日志一次性读入内存。清单预算为 `2,000,000` 字节。

`verify_generic_logs()` 在 Skill 完成后检查清单字节完全一致，并重新校验所有已暴露日志；输入发生变化时拒绝报告。文件路径必须是受控根目录内的安全相对路径。

### `generic_locator.py`

源码：[generic_locator.py](../../src/problem_locator/runtime/generic_locator.py)

`GenericLocatorExecutor` 负责通用 Skill 的工作区、提示词、可选经验、模型执行和结果解析。它不经过 Methods evidence 映射，也不生成专用 Candidate/Evidence；成功时以 `COMPLETED` Outcome 包住业务上的 `RESOLVED` 或 `UNRESOLVED`。

`build_prompt()` 保持 `generic_problem_text` 原文，用字节长度与边界标记区分补充文本、可选经验和框架指令。日志只能来自服务端提供的清单。`execute()` 对提示词预算先做硬检查，历史经验失败只记安全事件；结果文件解析前校验通用日志未变化。

`parse_generic_result()` 优先读取 V2；同时存在 V1 时不再拒绝或重复检查另一版本。V2 与专用报告共用正文读取，接受纯 Markdown，缺少或未知状态按 `UNRESOLVED` 交付，报告体最多 `65,536` 字节。V1 保留区段读取，但普通代码块和标记示例不再导致拒绝。读取时仍检查普通文件、工作区身份、大小及前后稳定性，未发布的 `.part` 不能作为报告。

## 6. Methods 证据处理文件

### `methods_grounding.py`

源码：[methods_grounding.py](../../src/problem_locator/runtime/methods_grounding.py)

Methods V1 的模型结果读取与证据核验。`FrozenTargetLogV1` 保存 source ID、相对路径、摘要和不可变 UTF-8 字节；构造时即验证摘要。`MethodDiagnosisDraftV1.from_mapping()` 读取确认/候选方法、证据、限制和安全说明，忽略额外字段，可选数组缺省为空，字符串条目去重；可识别发现不会因为缺少说明字段或状态标签与内容不一致而丢失。不能读取的子项直接跳过，不抛异常，也不新增异常记录。

`scan_method_markers()` 扫描全部目标日志，按 `casefold()` 后的连续子串匹配生成 `SkillLoadReceiptV1`。`verify_method_evidence()` 独立核对每条发现：方法已注册、来源存在、行号有效、完整原文相同、marker 属于当前方法并出现在该行、每个身份词出现在本条证据引用行中。不能从另一条发现借身份词。

`verify_method_diagnosis()` 复用 `select_method_diagnosis()`，先核对固定输入，再逐条保留引用成立的发现。未知方法、缺引用或引用无法核对的条目成为普通证据缺口，不使整份响应失败。确认方法由保留证据推导，身份词可省略；此时按实际引用位置区分发现，不生成替代身份词。产出 `VerifiedMethodDiagnosisV1`、`MethodGroundingAuditV1` 和选择回执。这验证引用一致性，并不把 marker 命中本身升级为因果证明。

`MethodReviewV1` 接受同样的可选字段和额外字段差异，无法识别的 verdict 按 `NEED_MORE_EVIDENCE` 处理。`verify_method_review()` 在 strict 模式按保留发现对齐审核，忽略额外条目，缺项或判断冲突按 `NEED_MORE_EVIDENCE` 处理，并据逐项结果调整整份结果的 verdict，不抛出格式或机械一致性异常。历史审计没有 `validation_mode` 时按 strict 解释。advisory 的 prior audit 则转到 advisory review 解析。

### `methods_selection.py`

源码：[methods_selection.py](../../src/problem_locator/runtime/methods_selection.py)

`select_method_diagnosis()` 用于 strict 诊断，包括安排 Reviewer 的路径。先验证共享 Skill/日志/回执，再逐条解析和核验 Evidence。相同身份且内容完全相同的发现合并；同身份但有冲突或畸形项目时整组不保留。没有身份词时按引用来源和行号分组，不把不同位置的发现混在一起。

最终只留下独立成立的完整发现；未成立的确认方法不会被偷偷转成候选。未知候选、缺失目标和拒绝项目记录原因，结果可能成为 `PARTIAL` 或 `INSUFFICIENT`。`selection` 审计包含源/有效草稿摘要、输入摘要、保留/合并/拒绝索引和缺口，不把被拒绝的摘要继续带进有效报告。

不再重复拒绝缺省数组、标签与内容不一致或没有可识别发现的模型响应；没有可保留发现时正常返回 `INSUFFICIENT`，不把格式差异记录为执行异常。

### `methods_advisory.py`

源码：[methods_advisory.py](../../src/problem_locator/runtime/methods_advisory.py)

`accept_method_diagnosis_advisory()` 核对固定 Skill、日志集合和扫描回执身份，但不重复逐行 marker 核验。直接复用 Methods 的结果读取，删除独立的字段集合、数组数量、理由和状态交叉校验。解析器保留可识别的模型判断，合并完全相同的发现；缺引用、缺身份词、未知方法或同身份不同判断成为明确限制，不直接删除全部结果。

有保留发现时有效状态设为 `PARTIAL`，否则为 `INSUFFICIENT`；审计标注 `validation_mode="advisory"` 和 `checked_source_count=0`。`parse_advisory_diagnosis()` 读取已经固定的有效草稿，`parse_advisory_review()` 保留 reviewer 的整份结果 verdict，不要求严格身份拼写或覆盖。报告展示层只在实际日志可定位时展示行引用。

### `methods_outcome.py`

源码：[methods_outcome.py](../../src/problem_locator/runtime/methods_outcome.py)

`map_verified_methods_draft()` 把已接受的 Methods V1 诊断/审核映射为现有业务 DTO，返回 `MappedMethodsDraft`。输入包括本次 Job、manifest、真实模型协议字节、核验结果及预处理或 prior audit；DIAGNOSE 与 REVIEW 的参数组合互斥。

`_diagnosis_projection()` 建立目标日志 Evidence proposal、Logparse Artifact、发现、候选结论、因果因素、完成条件映射以及 `DecisionAuditV2`。规则 ID 绑定方法与身份，未提供身份词时使用实际引用摘要；advisory 使用发现内容摘要，避免相同身份的不同模型判断冲突。逐项选择适用于有无 Reviewer 的两条路径，缺口保留在报告限制中；安排 Reviewer 时只审核真实发现和候选，不为选择缺口创建额外审核规则。能定位的物理原文写入独立 `decision_evidence.jsonl`，超出 Job 上下文预算会失败。

`_review_projection()` 将独立审核绑定到此前已固定诊断和审计。整份审核为非 PASS、但没有逐项问题说明时，采用普通整份审核说明，保留 Reviewer 判断，不因理由缺省触发 DTO 校验失败。`draft_bytes` 始终保留真实 Methods 协议规范字节，而非内部桥接 DTO 的重新序列化结果，使最终审计能够指回模型实际输出。advisory 报告中的 `0.5` 只是合同所需占位值，源码同时加入不可将其视为统计概率的说明。

### `methods_evidence_v2.py`

源码：[methods_evidence_v2.py](../../src/problem_locator/runtime/methods_evidence_v2.py)

保留 V2 的一次性服务端扫描和确定性计划构造。`scan_method_evidence_v2()` 输出来源、命中、事件和已加载方法的 `MethodEvidenceGraphV2`，保存原始 marker 与完整行。`build_method_evaluation_plan_v2()` 不再次搜日志，而是从 Graph 按方法优先级和 ID 构造每方法一个 evaluation，检查 activation marker、marker 索引与 Skill 归属。

`validate_method_evaluation_plan_v2()` 要求计划准确覆盖、分割 Graph 的全部事件和命中，固定顺序、Skill 摘要与 Graph 引用完全一致。

### `methods_evaluation_input_v2.py`

源码：[methods_evaluation_input_v2.py](../../src/problem_locator/runtime/methods_evaluation_input_v2.py)

将 Graph/Plan 投影为有界、去重但不丢信息的模型输入。不可变 Pydantic 模型分离 source、observation、marker、match、event 与 evaluation，重复物理日志行只保存一次，其余位置用连续整数 ID 引用。

`build_method_evaluation_input_v2()` 建立投影，`validate_method_evaluation_input_v2()` 验证它与原 Graph/Plan 一致。模型验证器要求 ID 连续、排序确定、同一物理行唯一、所有引用存在且所有观察/marker 被实际使用，不能加入计划外材料。

### `methods_evaluation_v2.py`

源码：[methods_evaluation_v2.py](../../src/problem_locator/runtime/methods_evaluation_v2.py)

`parse_method_evaluation_response_v2()` 接受 BOM 和整份 JSON 代码围栏，从根数组按 `evaluation_ref` 读取判断，再恢复服务端计划顺序。额外字段忽略，重复判断合并，引用按本项计划去重和排序；计划外引用不进入结果。缺项、判断冲突、无法识别的 verdict 或确认项没有可用支持时按 `UNKNOWN` 处理，可选理由缺省为普通说明，不记录格式异常或触发修复调用。非法 JSON 仍无法读取；持久化角色结果的计划归属和公共 DTO 约束保持不变。

`evaluate_method_role_v2()` 为 PRIMARY 或 REPAIR 生成固定角色结果。`resolve_method_consensus_v2()` 比较两个盲评角色的 evaluation、verdict 和支持事件集合，不比较理由文字；两者一致、没有 UNKNOWN 且至少一个 CONFIRMED 才能 RESOLVED。

### `methods_records_v2.py`

源码：[methods_records_v2.py](../../src/problem_locator/runtime/methods_records_v2.py)

对 `ExecutionRecordStore` 提供固定文件名的 Graph、Plan、limitations、State、提示词及 rejected-attempt 读写函数。发布前执行合同类型与规范 JSON 往返校验，读取仍使用对应强类型；提示词和拒绝记录按 SPECIALIST/REVIEWER 与 PRIMARY/REPAIR 固定名称，不能由模型指定任意文件名。`build_method_limitations_record_v2()` 绑定当前计划与证据图。

### `methods_replay_v2.py`

源码：[methods_replay_v2.py](../../src/problem_locator/runtime/methods_replay_v2.py)

`replay_method_rejected_attempt_v2()` 读取持久化 Job、Graph、Plan、可选 State 和指定拒绝尝试，重建对应工作流后重复输出校验。它不扫描原日志、不再次调用模型。替换 Job 需要读取前驱 State，Reviewer 需要准确的源诊断目标；缺记录、记录损坏、工作流不匹配和校验差异使用 `MethodValidationReplayErrorCodeV2` 区分。返回 `MethodValidationReplayReceiptV2`，供诊断原失败使用，不能替代正式 Test Flow verdict。

### `methods_outcome_v2.py`

源码：[methods_outcome_v2.py](../../src/problem_locator/runtime/methods_outcome_v2.py)

`build_method_terminal_result_v2()` 只接受 `RESOLVED`、`UNRESOLVED`、`FAILED` State，以 evaluation/event/hit 引用生成 `MethodTerminalResultV2`。先核对 State、Plan、Graph 身份与覆盖，RESOLVED 再从已确认角色结果取支持事件；有 reviewer 时必须与 specialist 选择一致。它不重新解释 marker，也不从理由文字编造证据，最终交合同层终态校验与投影。

## 7. 输出校验、服务端结果和发布文件

### `output_reader.py`

源码：[output_reader.py](../../src/problem_locator/runtime/output_reader.py)

这是不可信 Agent 文件进入服务端的主要读取边界。`ValidatedAgentDraft`、`ValidatedMethodDiagnosisDraft`、`ValidatedMethodReviewDraft` 分离不同协议；`ValidatedAgentOutput` 表示可暂存的最终结果；`ValidatedProposalResource` 绑定 proposal、大小、摘要、目录清单和来源身份。

`read_agent_output()` 根据不可变 Job 选择 Methods 草稿或保留的封存 V2 草稿协议。当前主流程中 Router 和 Specialist 的最终响应由 `final_response.py` 读取，此处仍负责 REVIEW 文件与旧文件协议，不能仅按本文件 docstring 推断所有角色都写文件。`.part` 文件从不作为最终结果。

读取检查包括：工作区根与 `inputs/output` 身份、祖先目录、普通节点和同设备限制、符号链接/重解析点/硬链接、路径规范、目录清单、大小/摘要、已知私密字节和封存 marker。支持时用目录描述符锚定打开；复制与暂存前后还会复核，防止校验后被替换。

`read_methods_preprocessing()` 必须从 broker 的单次成功预处理重建请求与来源，重新核对 Logparse 源树及每个可交付日志，返回 `ValidatedMethodsPreprocessing`。`read_method_role_attempt_v2()` 读取保留的 V2 单次响应。`ValidatedProposalResource.open_verified_file()` 输出重新验算过的文件流；服务端内存生成文件则走 `inline_bytes`，避免再次信任 Agent 工作区路径。

缺失与无效输出分开归类为 `OUTCOME_MISSING`、`OUTCOME_INVALID`。`RejectedAgentOutputError` 另外保留安全的失败类别和可归档原始字节，公开异常不串联模型可控解析器或 OS 错误内容。

### `outcome_finalizer.py`

源码：[outcome_finalizer.py](../../src/problem_locator/runtime/outcome_finalizer.py)

`seal_agent_outcome_draft()` 为保留文件协议提供封存：读取 `output/job_outcome.draft.json`，按 `AgentJobOutcomeDraftV2` 规范化，然后写 `runtime/tool-state/agent-job-outcome-draft.finalized`，记录固定路径、大小和 SHA-256。提供兼容 CLI 入口。

这一步不生成 Outcome ID、不设置服务端时间、不核验证据，也不写最终 `output/job_outcome.json`。Agent 提前占用服务端最终路径会被拒绝。规范草稿或 marker 无法落盘时抛出 `AgentOutcomeDraftSealWriteError`。

### `server_outcome_finalizer.py`

源码：[server_outcome_finalizer.py](../../src/problem_locator/runtime/server_outcome_finalizer.py)

`finalize_server_outcome()` 是结构化路径唯一的服务端最终化过程。它接受已验证草稿、服务端 Outcome ID/时间、`VerificationResult`、权威目标日志与可选路由准入记录，返回 `ServerFinalizationResult`。

ROUTE 非失败草稿必须带本次准入证明；DIAGNOSE/REVIEW 非失败草稿必须带 verification。COMPLETED 未通过正向 gate，或目标缺失且当前策略不允许部分结果时，改为 INCONCLUSIVE，并清除不应进入结论的内容。随后替换服务端 provenance、构造 Result v3，校验 Outcome/Result 与 Job 一致。

关键词直达使用独立的 `direct_keyword_route_v1` 分支。最终化接收本次真实问题原文和冻结 Skill 索引，重新执行匹配，核对决策、审计和草稿摘要；只改 `model_called`、Skill ID、匹配位置或摘要不能构造有效的直达结果。模型路径使用 `shortlisted_route_v1`，绑定当前 Job、上下文、完整索引、草稿和候选范围，核对固定 Skill 引用与置信度，不重新召回或审核语义。

最终 `output/job_outcome.json` 必须原先不存在；服务端独占创建 `runtime/server-state/`，原子写入结果和 `server-job-outcome.finalized`，marker 绑定源草稿、Outcome、DecisionAudit 与决策原文字节摘要。`off` 的 Markdown 直接交付路径在 Runtime 中构造并发布 Outcome，不经过这里的证据 gate。

### `user_results.py`

源码：[user_results.py](../../src/problem_locator/runtime/user_results.py)

`build_server_result_bundle()` 根据结构化 diagnosis/review、验证结果和已捕获日志生成 `UserResultPayloadV3` 与 `ServerGeneratedResultFile`。报告包含问题、根因、发现、因果/候选/排除因素、支持证据、完成条件、验证规则、时间相关性、缺口、限制、建议和安全说明。

`_CapturedLogIndex` 批量缓存实际引用的物理行范围；引用内容从捕获的原始字节生成，而不是直接复制模型自报片段。只有完全完成的 Candidate 才填写 `root_cause`；部分或未确认结果明确区分状态。advisory 的说明保留其“模型判断”属性。

当前函数直接生成 `diagnosis-result.json`，候选结果另在 metadata 中保存 `prepare_result_archive()` 产生的归档计划。源码虽然仍有提及 ZIP 的历史 docstring，但此函数当前没有直接把 ZIP 文件加入 `files`；实际归档生成属于后续资源流程。

### `result_types.py`

源码：[result_types.py](../../src/problem_locator/runtime/result_types.py)

只定义两个不可变共享值：`CapturedTargetLog` 将权威目标、冻结字节和 Evidence bindings 放在一起；`ServerGeneratedResultFile` 将服务端 Artifact draft 与内存字节绑定。它们连接目标捕获、结果生成和暂存，不引入新持久化格式。

### `verification_result.py`

源码：[verification_result.py](../../src/problem_locator/runtime/verification_result.py)

`VerificationResult` 保存 `DecisionAuditV2`、正向 gate、决策原文字节、逐发现规则 ID、部分证据选择标记和证据模式。`permits_missing_targets()` 只有在专用 DIAGNOSE、正向 gate 已通过、Candidate 为 PARTIAL，并符合无 Reviewer 逐项选择或 advisory 规则时允许部分目标缺失；其他路径不能借此绕过完整目标要求。

### `proposal_stager.py`

源码：[proposal_stager.py](../../src/problem_locator/runtime/proposal_stager.py)

`stage_validated_output()` 将已验证资源写入 `ResourceStore`，由 proposal key 替换为实际 `StagedResourceRef`，再构造持久化 `JobOutcome`，返回 `StagedOutcome`。文件使用 `open_verified_file()`，目录在复制前后复查；每个 store 回执必须与 Job、proposal key、类型、大小、摘要和树清单一致，随后再调用 `validate_staged()`。

Logparse 运行的 claim、parse 参数和资源 metadata 需要相互一致，不能只凭 Agent 声称解析成功。任何明确失败会尽力丢弃本次已暂存项并归一为资源阶段失败；最终结果和 Result 仍要按合同校验。`discard_staged()` 只处理指定暂存引用，不承担历史测试证据清理。

### `outcome_publisher.py`

源码：[outcome_publisher.py](../../src/problem_locator/runtime/outcome_publisher.py)

`OutcomePublisher.publish_success()` 先验证 Outcome，再发布规范 JSON；`publish_failure()` 用服务端 ID/时间构造规范失败 Outcome。持久化调用出错不等于没有写成功，`_recover_after_publish_failure()` 必须回读同一 Job 的已发布结果。

若已有有效 Outcome，它占有该不可变 outbox 路径，重新发布相同已存在字节以补完 chmod/fsync；不能用新失败结果覆盖。无法确认或修复发布时抛出 `RuntimeInfrastructureError`，这是正常失败 Outcome 之外的基础设施出口，调用方不能将其当作业务成功。

### `failures.py`

源码：[failures.py](../../src/problem_locator/runtime/failures.py)

`RuntimeExecutionError` 是内部携带 `ExecutionFailure` 的异常，`runtime_failure()` 将阶段、错误码、安全消息、retryable 和细节统一组装。公开词汇仍来自合同层，不新增 wire error 模型。

### `limits.py`

源码：[limits.py](../../src/problem_locator/runtime/limits.py)

直接重导出合同层的 ROUTE/DIAGNOSE/REVIEW 上下文、日志、墙钟和工作区限制，避免两份数值发生漂移。此文件额外定义进程终止宽限时间 `PROCESS_TERMINATION_GRACE_SECONDS = 5.0`。

## 8. 内置资产逐文件设计

普通运行资产的 `asset.json` 声明 `schema_version`、`asset_kind`、`id`、`version` 与 `entry`。实际 `content_hash` 由目录内容计算；修改 Markdown、JSON 或 manifest 都会影响固定资产身份。`intake/asset.json` 是 intake 自身的角色配置，不使用普通资产的同一字段结构。

### 8.1 角色 profile：10 个文件

| 文件 | 职责与调用关系 |
| --- | --- |
| [profiles/router/asset.json](../../src/problem_locator/runtime/assets/profiles/router/asset.json) | 注册 `agent-profile/router` `2.0.0`，入口 `profile.md`；供 `route_bindings()` 固定。 |
| [profiles/router/profile.md](../../src/problem_locator/runtime/assets/profiles/router/profile.md) | 要求逐项审核适用/排除条件；目录中无 routing 的 Skill 不可自动选择；一次响应完成，不追问、不调用工具。 |
| [profiles/specialist/asset.json](../../src/problem_locator/runtime/assets/profiles/specialist/asset.json) | 注册 `agent-profile/specialist` `8.0.0`；用于非 off 的专用诊断。 |
| [profiles/specialist/profile.md](../../src/problem_locator/runtime/assets/profiles/specialist/profile.md) | 要求阅读完整冻结日志与方法卡，区分 marker 命中与完整确认条件，返回 Methods V1 JSON；允许完整内联或受限读取，不允许 Write。 |
| [profiles/skill-direct/asset.json](../../src/problem_locator/runtime/assets/profiles/skill-direct/asset.json) | 注册 `agent-profile/skill-direct` `1.0.0`；`off` 模式显式选择。 |
| [profiles/skill-direct/profile.md](../../src/problem_locator/runtime/assets/profiles/skill-direct/profile.md) | 按固定 Skill 分析并给出 Markdown 报告；不要求为了框架补齐引用元数据，不安排 Reviewer，也不按 marker 数量改变结论。 |
| [profiles/reviewer/asset.json](../../src/problem_locator/runtime/assets/profiles/reviewer/asset.json) | 注册 `agent-profile/reviewer` `7.0.0`；供独立 REVIEW 绑定。 |
| [profiles/reviewer/profile.md](../../src/problem_locator/runtime/assets/profiles/reviewer/profile.md) | 审核固定诊断、审计、Candidate 与 Evidence，要求逐身份覆盖，只写 `output/method-review.draft.json`；advisory 的放宽说明由当前 Runtime 补充。 |
| [profiles/generic-locator/asset.json](../../src/problem_locator/runtime/assets/profiles/generic-locator/asset.json) | 注册 `agent-profile/generic-locator` `4.0.0`；用于预装通用 Skill 黑盒调用。 |
| [profiles/generic-locator/profile.md](../../src/problem_locator/runtime/assets/profiles/generic-locator/profile.md) | 调用配置的 Skill 一次，保留原问题；日志来自只读清单，不能自行拆归档或递归调用 Problem Locator；历史经验只是参考。 |

### 8.2 工具包：8 个文件

| 文件 | 职责与调用关系 |
| --- | --- |
| [tool-bundles/router/asset.json](../../src/problem_locator/runtime/assets/tool-bundles/router/asset.json) | 注册 `tool-bundle/router` `3.0.0`，绑定 router 工具声明。 |
| [tool-bundles/router/tool-bundle.json](../../src/problem_locator/runtime/assets/tool-bundles/router/tool-bundle.json) | `tools: []`，Router 所需资料已内联。实际 CLI 禁用工具由命令策略落实。 |
| [tool-bundles/diagnose/asset.json](../../src/problem_locator/runtime/assets/tool-bundles/diagnose/asset.json) | 注册 `tool-bundle/diagnose` `5.0.0`，文档型 v3 与旧 Methods 专用定位共用。 |
| [tool-bundles/diagnose/tool-bundle.json](../../src/problem_locator/runtime/assets/tool-bundles/diagnose/tool-bundle.json) | 声明 `workspace-file-read`、`workspace-file-search`、`workspace-shell`，对应 `Read`、`Grep`、`Glob`、`Bash`；输入内联时保留这些工具。 |
| [tool-bundles/review/asset.json](../../src/problem_locator/runtime/assets/tool-bundles/review/asset.json) | 注册 `tool-bundle/review` `3.0.0`。 |
| [tool-bundles/review/tool-bundle.json](../../src/problem_locator/runtime/assets/tool-bundles/review/tool-bundle.json) | 声明 `workspace-file-read` 与 `workspace-proposal-write`，支持读取冻结审核材料并写 Methods REVIEW 草稿。 |
| [tool-bundles/generic-locator/asset.json](../../src/problem_locator/runtime/assets/tool-bundles/generic-locator/asset.json) | 注册 `tool-bundle/generic-locator` `1.0.0`。 |
| [tool-bundles/generic-locator/tool-bundle.json](../../src/problem_locator/runtime/assets/tool-bundles/generic-locator/tool-bundle.json) | `mode: INHERIT_AGENT_ENVIRONMENT`；通用 Skill 使用 Agent 环境已有工具，不能套用专用角色的只读声明解释其权限。 |

### 8.3 上下文策略：8 个文件

| 文件 | 职责与调用关系 |
| --- | --- |
| [context-policies/route/asset.json](../../src/problem_locator/runtime/assets/context-policies/route/asset.json) | 注册 `context-policy/route` `1.0.0`，入口 `policy.md`。 |
| [context-policies/route/policy.md](../../src/problem_locator/runtime/assets/context-policies/route/policy.md) | `131072` 字节预算，完整保留必需段及最多 5 个候选的条件；完整目录留在服务端，可选证据按冻结顺序整段加入。 |
| [context-policies/diagnose/asset.json](../../src/problem_locator/runtime/assets/context-policies/diagnose/asset.json) | 注册 `context-policy/diagnose` `1.0.0`。 |
| [context-policies/diagnose/policy.md](../../src/problem_locator/runtime/assets/context-policies/diagnose/policy.md) | `262144` 字节预算，保留完整选中 Skill、必需证据与 manifest，再决定可选证据。 |
| [context-policies/review/asset.json](../../src/problem_locator/runtime/assets/context-policies/review/asset.json) | 注册 `context-policy/review` `3.0.0`。 |
| [context-policies/review/policy.md](../../src/problem_locator/runtime/assets/context-policies/review/policy.md) | `204800` 字节预算，固定 Candidate、完整 Skill、必需 Evidence、原 Methods 诊断和审计；不换成当前状态或 Specialist 会话。 |
| [context-policies/generic-locator/asset.json](../../src/problem_locator/runtime/assets/context-policies/generic-locator/asset.json) | 注册 `context-policy/generic-locator` `3.0.0`。 |
| [context-policies/generic-locator/policy.md](../../src/problem_locator/runtime/assets/context-policies/generic-locator/policy.md) | 保留问题原文和独立补充边界；日志按文件读取，不加入专用状态；可选脱敏经验卡最多 `4096` 字节且只能整段省略。 |

这些 Markdown 记录策略意图，实际字节判断由 Job 的 `resource_limits`、`ContextBuilder` 或 `GenericLocatorExecutor` 执行。

### 8.4 输出合同：10 个文件

| 文件 | 职责与调用关系 |
| --- | --- |
| [output-contracts/route/asset.json](../../src/problem_locator/runtime/assets/output-contracts/route/asset.json) | 注册 `output-contract/route` `6.0.0`。 |
| [output-contracts/route/output-contract.md](../../src/problem_locator/runtime/assets/output-contracts/route/output-contract.md) | 三字段简短结果、本次最多 5 个候选及 `0.95` 置信度阈值，不要求回写逐条件审核或引文。 |
| [output-contracts/diagnose/asset.json](../../src/problem_locator/runtime/assets/output-contracts/diagnose/asset.json) | 注册 `output-contract/diagnose` `11.0.0`。 |
| [output-contracts/diagnose/output-contract.md](../../src/problem_locator/runtime/assets/output-contracts/diagnose/output-contract.md) | 固定 Methods V1 七字段诊断 JSON、来源/行号/完整原文/marker/身份词约束；在最终响应交付，由服务端核验或按所选 advisory 策略处理。 |
| [output-contracts/skill-direct/asset.json](../../src/problem_locator/runtime/assets/output-contracts/skill-direct/asset.json) | 注册 `output-contract/skill-direct` `1.0.0`。 |
| [output-contracts/skill-direct/output-contract.md](../../src/problem_locator/runtime/assets/output-contracts/skill-direct/output-contract.md) | 固定 `SKILL_DIAGNOSIS_RESULT_V1` 状态首行与最长 `65536` UTF-8 字节正文；不要求证据核验 JSON，保留 Skill 报告。 |
| [output-contracts/review/asset.json](../../src/problem_locator/runtime/assets/output-contracts/review/asset.json) | 注册 `output-contract/review` `10.0.0`。 |
| [output-contracts/review/output-contract.md](../../src/problem_locator/runtime/assets/output-contracts/review/output-contract.md) | 输出 REVIEW 草稿；顶层与逐发现 verdict 为 PASS/NEED_MORE_EVIDENCE/REJECT。strict 路径按已知发现对齐，缺项或冲突按证据不足处理。 |
| [output-contracts/generic-locator/asset.json](../../src/problem_locator/runtime/assets/output-contracts/generic-locator/asset.json) | 注册 `output-contract/generic-locator` `2.1.0`。 |
| [output-contracts/generic-locator/output-contract.md](../../src/problem_locator/runtime/assets/output-contracts/generic-locator/output-contract.md) | 只写 V2 `.md` 结果文件，状态首行后为完整 Markdown；正文保留原字节，不得同时生成旧 `.txt`，未确认报告说明假设与缺口。 |

### 8.5 输入模板与 intake：4 个文件

| 文件 | 职责与调用关系 |
| --- | --- |
| [input-profile/profile.json](../../src/problem_locator/runtime/assets/input-profile/profile.json) | `builtin-global-v1`：毫秒 UTC `problem_time`；各角色 slot、process_name、可选 pid；一个 gzip/zip/tar 日志附件。`input_profile.py` 校验并展开，供专用流程使用，不能外推为 GENERIC 前置要求。 |
| [intake/asset.json](../../src/problem_locator/runtime/assets/intake/asset.json) | intake 配置版本 `1.1.0`，绑定 profile/合同入口；每条消息最多一次后端调用、`file_access=none`、`131072` 上下文、180 秒、1 MiB 输出与 2 MiB 工作区限制。 |
| [intake/profile.md](../../src/problem_locator/runtime/assets/intake/profile.md) | 当前正文标题 `1.3.1`。角色只整理已有任务的补充事实，查看全部 USER 来源；不能诊断、读文件、建案或更改冻结事实。部分有效参数可先提交，换题或更正已冻结事实需新任务。 |
| [intake/output-contract.md](../../src/problem_locator/runtime/assets/intake/output-contract.md) | 当前正文标题 `1.3.1`。规定五字段 JSON、NEED_CLARIFICATION/SUBMIT_SUPPLEMENT/NEW_CASE_REQUIRED、来源 message/quote 校验和逐项采纳规则。内建时间转换由服务端限定处理；不能用模型改写值代替用户原文。 |

intake manifest 版本和正文标题当前并不相同，本章分别记录实际值，不把文档标题当成资产绑定版本。intake 的执行与补充接收在 `agent/` 模块，Runtime 目录在这里提供受控角色资产及共用 AgentBackend。

## 9. 跨文件不变量与维护方式

- **身份一路传递。** Job 固定资产引用和输入资源；workspace 固定路径身份及摘要；目标来自 broker 审计；Outcome 再绑定源草稿、执行审计和资源回执。任何一层不能只凭名称相同就替换字节。
- **模型输出与服务端结论分开。** 模型可以提供路由审核、Methods 判断或 Markdown 报告；最终 ID、时间、资源引用、准入审计和结构化结果均由服务端生成。Skill direct 的原样报告必须保留其独立交付语义。
- **失败与证据不足分开。** 无效 JSON、文件漂移、协议错误是执行失败；正常路由未准入是回退；模型无法确认可以是有效 `UNRESOLVED` 报告。不能混用这些状态掩盖错误。
- **权限声明需要执行机制。** 工具包与 profile 是受信输入，实际限制还依靠 CLI 参数、追问 hook、工作区验证和工具遥测。保留的跨平台实现不扩大 Server 支持范围。
- **版本名不能代替调用关系。** Methods V2、旧 ROUTE 引号恢复和旧输出文件协议仍在源码中，但当前默认链以 `DiagnosisRuntime._execute()`、实际 bindings 和调用点为准。

增加运行资产时，应同时说明 manifest、入口、版本引用生成、上下文注入位置与实际工具权限。修改解析、暂存或最终化边界时，应针对输入歧义、摘要漂移、资源替换和发布不确定性选择直接覆盖的专项用例；正式功能结论仍只能来自仓库规定的 Test Flow 入口与 `verdict.json`。
