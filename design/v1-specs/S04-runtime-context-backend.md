# S04：Runtime、Context Builder 与 Agent Backend 实施说明书

- 状态：V1 独立开发合同
- 未来 Codex 开发任务模型：`gpt-5.6-sol`，`reasoning_effort=ultra`
- 公共合同：[S00-contract-freeze.md](S00-contract-freeze.md)
- 组合验收：[../v1-composition-spec.md](../v1-composition-spec.md)

## 1. 目标

本切片实现一次已认领 Job 的纯执行闭环，不负责业务状态推进：

1. 根据 Job 创建时冻结的快照、资源引用和版本引用构建确定性的有界上下文。
2. 为每个 Job 创建全新、可丢弃的 Workspace 和 Agent Session。
3. 以与 `issue-locator` 完全相同的 `CLAUDE_COMMAND` 语义启动 Agent 子进程。
4. 只从 Agent 原子写入的 `output/job_outcome.json` 读取结果；stdout/stderr 仅作为执行日志。
5. 校验输出 Schema、Job 绑定、资源提案和文件限制，返回 S00 定义的有效执行结果或执行失败。
6. 从 `SKILL_DIR` 启动时扫描版本化 Skill，以完整目录 SHA-256 固定版本；运行期间不热更新。
7. 在 Windows 与 Linux 上可靠取消、超时并终止完整子进程树。

## 2. 非目标

- 不创建、修改或推进 Case、DiagnosisState、Job、JobOutcome 的业务状态。
- 不认领 Job、不维护队列、不产生 `runtime_epoch`；这些属于 S05。
- 不接受 MCP/HTTP/CLI 请求；这些属于 S06。
- 不实现或修改任何 Diagnosis Skill；这些属于 S07。
- 不解析压缩包、不枚举压缩包成员、不解包日志、不扫描日志、不选择 lifecycle，也不执行项目内自制 grep。
- 不保存完整 Agent 对话、隐藏推理、原始流式事件或未提升的工具轨迹。
- 不从执行时最新 Case 静默替换 Job 固定快照、引用或资产版本。
- 不引入 Session Cache、JobAttempt、自动模型摘要、向量检索、外部队列或业务级自动重试。

## 3. 上游合同

唯一规范上游是 S00。实现必须直接导入 S00 冻结的 DTO、枚举、错误类型和 Port，不得复制同名模型。

必须消费的 S00 合同包括：

- Job、JobType、JobStatus、Job 固定引用、资源限制和 `base_state_revision`。
- AgentJobOutcome、规范 JobOutcome、ExecutionFailure、Draft/Proposal 两阶段模型及其输出合同。
- WorkspaceInputManifest、LogparseParseClaim、UserResultPayload 及其公共 Schema。
- Runtime、StateRepository 的只读 `read_case`、ResourceStore、AssetCatalogPort、LogparseBrokerFactory、ExecutionRecordStore、Clock 与 IdGenerator Port。
- 版本化资产引用、资源 ID、相对 storage key 和 SHA-256 规则。
- S00 的错误码、错误阶段、可恢复性分类和 Outcome disposition。

Context Builder、Agent Backend 和 Workspace 是 S04 内部组件；Catalog 实现 S00 的 `AssetCatalogPort`，供 S03 在 Claim/Resume 时检查固定版本，也供 Runtime 解析同一版本。Runtime 以只读方式注入 StateRepository，仅用于把 Job 已固定的业务 ID 解析为不可变资源元数据，不写状态、不读取执行时最新 DiagnosisState；同时注入 S00 `Clock`、`IdGenerator` 和由 S07 实现的 `LogparseBrokerFactory`。Broker 持有 raw logparse 配置与启动能力，Agent Backend 只得到 job-scoped endpoint/token。其他组件最终组合成 S00 的 `Runtime.execute(job, cancellation) -> RuntimeExecutionReceipt`。S01～S03 的具体实现不是本切片单元测试的前置条件。本切片使用 S00 公共 Fake 或本地 Runtime Fixture，真实装配由 S08 完成。

## 4. 唯一文件责任区

本切片是以下路径的唯一所有者：

```text
src/problem_locator/runtime/**
tests/unit/runtime/**
tests/fixtures/components/runtime-*/**
handoff/S04.json
```

建议的内部文件布局如下；可以在责任区内细分，但不得把文件移到其他切片：

```text
src/problem_locator/runtime/
├── assets/
│   ├── profiles/{router,specialist,reviewer}/
│   ├── tool-bundles/{router,diagnose,review}/
│   ├── context-policies/{route,diagnose,review}/
│   └── output-contracts/{route,diagnose,review}/
├── context_builder.py
├── context_policy.py
├── catalog.py
├── workspace.py
├── diagnosis_runtime.py
├── claude_command.py
├── agent_backend.py
├── process_tree.py
├── output_reader.py
└── limits.py
```

组件 Fixture 只能放在 `tests/fixtures/components/runtime-*/**`；glob 使用 `v1-specs/README.md` 的仓库相对 POSIX 语义。不得新增仓库级 `tests/conftest.py`。

## 5. 禁止修改项

- 不得修改 `src/problem_locator/contracts/**`、S00、S01～S03、S05～S08 的责任区。
- 不得修改 `pyproject.toml`、锁文件、根 README、`.env.example` 或 `.gitignore`。
- 不得修改 `.claude/skills/**`。
- 不得新增第二套领域枚举、错误码、DTO、JSON Schema 或 Port。
- 不得把 `STALE` 加入 JobStatus；`STALE` 仅是 S00 定义的 Outcome disposition。
- 不得把 stdout/stderr、最后一行 stdout 或 Markdown 代码块作为 JobOutcome 回退来源。
- 不得使用 Shell 执行 `CLAUDE_COMMAND`。
- 不得访问绝对资源路径作为持久化合同；Workspace 外资源由 S00 的 ResourceStore Port 按固定引用物化。
- 不得为了修复跨切片不匹配而越界改文件；必须提交第 14 节的合同变更请求。

## 6. 输入输出契约

### 6.1 Context Builder

输入必须是 S00 的不可变 Job 执行视图，至少包括：

- `job_id`、`case_id`、`job_type`、`goal`、`base_state_revision`；
- 固定 `context_snapshot`；
- 固定 Evidence、Attachment、Artifact 和 previous outcome 引用；
- 固定 Profile、Skill、Tool Bundle、Context Policy 和 Output Contract 版本；
- REVIEW Job 的固定 review target；
- S00 定义的资源限制。

输出使用 S00 的 BoundedContext 合同。正文以 UTF-8 字节数计预算：

- Router：`128 KiB = 131072 bytes`；
- Specialist：`200 KiB = 204800 bytes`；
- Reviewer：`200 KiB = 204800 bytes`。

这里的上限对象是发送给 Agent Backend 的最终上下文正文 UTF-8 序列化字节数，不包含 Workspace 内未内联的文件字节，也不包含子进程运行后产生的 stdout/stderr。Context Builder 必须返回确定的内容清单、每段字节数和总字节数，便于测试复现。

正文 section 顺序固定为：`PROFILE`、`SKILL`（ROUTE 使用 `SKILL_INDEX`）、`TOOL_BUNDLE`、`JOB_INSTRUCTION`、`CONTEXT_SNAPSHOT`、`OPEN_REQUIREMENTS`、REVIEW 专用 `REVIEW_TARGET`、`OUTPUT_CONTRACT`、按 Job 数组逐项出现的 `PREVIOUS_OUTCOME`、`EVIDENCE`、`RESOURCE_MANIFEST`。不存在的角色专用 section 省略，其余不得换序。`JOB_INSTRUCTION` 内容是 S00 `JobInstructionPayload` 的 Canonical JSON，逐字带入 Job 的 ID、type、goal 和 base revision。每个 PREVIOUS_OUTCOME section 的内容是对应规范 JobOutcome 的 Canonical JSON，`source_refs=[outcome_id]`；不得只摘 recommendation 或重新摘要。结构化内容使用 S00 Canonical JSON；文本统一为 UTF-8、LF 行尾。每段 framing 固定为 `<<<SECTION {ordinal} {kind}>>>`、一个 LF、内容、确保一个末尾 LF、`<<<END SECTION>>>`、一个 LF；`ContextSection.utf8_bytes` 包含 framing。

最低必需集合不允许截断：固定 Profile、选定完整 Skill 或 Router Skill 摘要目录、Tool Bundle、`JOB_INSTRUCTION`、ContextSnapshot、开放 requirements、Output Contract、Job 固定的全部 previous outcomes，以及逐字等于 `inputs/manifest.json` 的 `RESOURCE_MANIFEST`；Reviewer 还必须包含候选结论、固定复核目标及候选声明的全部 supporting Evidence。必需集合超限时不调用 Backend。

Context Builder 先物化全部固定输入并生成 manifest，再计算所有必需 section 的完整 framed bytes；`RESOURCE_MANIFEST` 的字节必须先保留。若必需总数超过预算，产生 `CONTEXT_LIMIT`。否则按 `Job.evidence_refs[]` 扫描 Evidence：Reviewer supporting Evidence 必须全部选中；其余只在扣除 manifest 和所有尚未放入的必需 Evidence 后仍可整段装入时选中，不能截断。最终把选中 Evidence 按原 Job 顺序放在 `RESOURCE_MANIFEST` 之前。Attachment/Artifact 不内联文件内容，只在 manifest 中写固定 ID、类型、大小、SHA-256 和 Workspace 相对路径。禁止调用模型做自动摘要，也禁止从执行时最新 Case 补入资源。

### 6.2 Versioned Catalog

Catalog 的输入只有三部分：本切片随代码发布的内置资产目录、配置的 `SKILL_DIR`，以及 S07 以 S06 同一不可变 Settings 构造并成对返回的可选 `ResolvedAsset(asset_kind=LOGPARSE_TOOL)` 与 `LogparseBrokerFactory` 中的前者。Runtime 必须注入同一对中的 Factory；不得用另一配置/指纹实例重新构造。两者 ref/fingerprint 不一致、requires_logparse Skill 只有其一或 Factory 在 open 时发现 Job/manifest ref 不等时，Catalog 构建或 diagnose bindings 明确失败，不能回退 direct CLI。构造完成后 Catalog 不可变。

每个内置资产目录必须包含 `asset.json` 和一个入口文件。`asset.json` 固定字段为：

```text
schema_version=1
asset_kind
id
version
entry
```

内置逻辑 ID 固定为：

| Job 类型 | Agent Profile | Tool Bundle | Context Policy | Output Contract |
|---|---|---|---|---|
| ROUTE | `agent-profile/router` | `tool-bundle/router` | `context-policy/route` | `output-contract/route` |
| DIAGNOSE | `agent-profile/specialist` | `tool-bundle/diagnose` | `context-policy/diagnose` | `output-contract/diagnose` |
| REVIEW | `agent-profile/reviewer` | `tool-bundle/review` | `context-policy/review` | `output-contract/review` |

这些目录的初始 `version` 固定为 `1.0.0`。修改目录中任意产品文件时必须提升 version；Catalog 仍对完整目录生成 content hash，所以同 `{id,version}` 不同 hash 视为配置损坏，不允许并存。Output Contract 入口说明对应 Job 类型允许的 S00 AgentJobOutcome 载荷，并引用 S00 Schema，不复制 Schema。

V1 的 ROUTE 和 REVIEW Tool Bundle 不声明外部业务工具；DIAGNOSE Tool Bundle 只声明 Workspace 文件操作和 S00 已注册的 `problem-locator-logparse` broker 客户端。该列表是传给 Agent 的执行合同，不构成操作系统沙箱；S07 broker 仍必须拒绝路径、参数或 argv 越界。`requires_logparse=true` 的 Skill 若不能同时解析固定 LOGPARSE_TOOL ref、broker factory 与该入口，`diagnose_bindings` 必须失败，不能把直接运行 `LOGPARSE_REPO/cli.py` 作为降级路径。

`SKILL_DIR` 只在服务组合启动时扫描其直接子目录。一个可路由 Diagnosis Skill 必须同时包含 `diagnosis-skill.json` 和 manifest 指定的入口文档；其他目录（例如 Client Access Skill 或生成器）不会进入路由目录。`diagnosis-skill.json` 的固定字段为：

```text
schema_version=1
id
version
capability
summary
entry_document
tool_bundle_id
requires_logparse
logparse_product?
```

`id` 必须匹配 `[a-z][a-z0-9-]{1,63}`；`entry_document` 是目录内安全相对 POSIX 路径；`tool_bundle_id` 在 V1 只能是 `tool-bundle/diagnose`。`requires_logparse=true` 时必须给出非空 `logparse_product`，且启动组合必须提供 S07 生成的 LOGPARSE_TOOL 资产；false 时 `logparse_product` 必须为 null。Diagnosis Skill 的 VersionedRef ID 为 `diagnosis-skill/<id>`，version 取 manifest.version，content hash 取完整产品目录的规范化 SHA-256。

目录 hash 输入固定为 Canonical JSON `{version:1,entries:[{path,size,sha256}]}`：只纳入普通文件，相对 POSIX 路径按 Unicode 码点升序，文件字节原样求 hash；拒绝符号链接、硬链接、设备文件、路径逃逸和非 UTF-8 路径。排除项只有 `.DS_Store`、`__pycache__/**`、`*.pyc` 以及 S07 生成器 manifest 中明确列出的非产品来源标记；不得使用本机全局 ignore 规则。

三个 binding 方法的返回值固定为：

- `route_bindings()`：ROUTE 四个内置资产、全部已验证 Diagnosis Skill ref（按 `VersionedRef.id` 升序）、`logparse_tool_ref=null`、`logparse_product=null` 和 ROUTE 资源限制；
- `diagnose_bindings(skill_ref)`：Specialist/Profile、所选 Skill、该 manifest 的 Tool Bundle、DIAGNOSE Policy/Contract、空 `available_skill_refs[]`、DIAGNOSE 限制，以及 Skill 需要时的唯一 logparse ref 与逐字 `logparse_product`；
- `review_bindings(skill_ref)`：Reviewer/Profile、同一固定 Skill、REVIEW Tool Bundle/Policy/Contract、空 `available_skill_refs[]`、`logparse_tool_ref=null`、`logparse_product=null` 和 REVIEW 限制。

`check(refs[])` 对每个完整 `{id,version,content_hash}` 做精确匹配并按输入顺序返回缺失项；`resolve(ref)` 只在精确匹配时返回只读根路径和 asset kind。重复 ID/version、manifest 冲突、缺入口、hash 失败或 required logparse 缺失都使 Catalog 构建失败，服务 readiness 保持 false。Job 创建后只按固定引用解析，不得回退到“当前最新版”。V1 不支持运行期热更新；目录变化只有在新服务进程启动并形成新 Catalog 后才可用于新 Job。

### 6.3 Diagnosis Runtime

Runtime 接收一个已由 S05 成功认领、且仍保持固定输入的 Job，按以下顺序执行：

1. 解析所有固定资产版本；
2. 调用一次 `StateRepository.read_case(job.case_id)`，确认聚合内同 ID Job 的所有不可变字段与输入 Job 相同，并严格按 `evidence_refs[]`、`attachment_refs[]`、`artifact_refs[]`、`previous_outcome_refs[]` 的 ID 和数组顺序解析完整元数据；缺失映射 `RESOURCE_NOT_FOUND`，size/hash 不符映射对应资源错误，跨 Case、重复、未固定引用或 Job 不可变字段矛盾映射 `OUTCOME_INVALID`；
3. 创建 Job 专属 Workspace；
4. 只使用第 2 步解析出的 ResourceRef/Attachment/Artifact 元数据调用 ResourceStore，按固定引用只读物化 Attachment、Evidence、Artifact 和预处理目录；把每个 previous outcome 的完整规范 DTO 重新编码为 Canonical JSON 并物化到固定 outcomes 路径；
5. 构建有界上下文；
6. 若 Job 固定 logparse，调用 `LogparseBrokerFactory.open(job, workspace_root, workspace_manifest, cancellation)`；只把 session 的 endpoint/token 注入已净化 Agent 环境，无 logparse 时不启动 broker；
7. 通过 stdin 向 Backend 提交 Prompt；
8. 等待子进程成功退出；
9. 读取并按 S00 `AgentJobOutcome` Schema 校验 `output/job_outcome.json`；解析诊断严格区分 `outcome_json_invalid`（UTF-8、JSON 语法、重复键或非有限数字）、`outcome_non_canonical`（字节拼写不规范）和 `outcome_schema`（字段校验失败），前两类记录具体原因，Schema 类逐项记录字段路径、错误类型和消息；Schema 通过后还必须读取 `runtime/tool-state/agent-job-outcome.finalized`，按 Canonical 私有 marker 校验最终 Outcome 的 size/SHA-256，缺失、格式非法和不匹配分别记录 `outcome_finalizer_marker_missing`、`outcome_finalizer_marker_invalid`、`outcome_finalizer_marker_mismatch`，所有诊断均不记录整份原始内容；
10. 校验 draft proposal path、声明值、Job 绑定和相对路径安全性；若存在 broker session，还要对 AgentJobOutcome Canonical bytes、全部 proposal 相对路径 UTF-8 bytes，以及每个待保留普通文件/目录树文件内容做与日志相同的跨分块精确 secret 扫描；任一 endpoint/token 命中都产生 `OUTCOME_INVALID`，不 stage proposal、不发布原 AgentJobOutcome；Runtime 不即时删除 Agent output，而是保留 Workspace 原文件，并把已经安全读取、实际参与校验的 `job_outcome.json` 原始字节 best-effort 幂等归档到 Job 执行记录；随后按第 6.4 节正常系统失败路径构造并 finalize 规范 Failure JobOutcome，归档失败只记诊断且不覆盖原失败；
11. 若有 CandidateConclusionDraft，定位唯一 USER_RESULT draft，在暂存前完整读取其 payload，要求逐字是 S00 `UserResultPayload` 的 Canonical JSON bytes，且 problem、candidate statement、supporting bindings 和完整 completion mapping 分别匹配 Job 与同一 AgentJobOutcome；任一不符产生 `OUTCOME_INVALID`，没有 candidate 时不得读取或接受 USER_RESULT；若同时存在 USER_RESULT_ARCHIVE，Runtime 必须按 Candidate Evidence binding 首次出现顺序重建目标日志：既有 LOGPARSE_RUN 从冻结的 `inputs/` 边界读取，同一 Outcome 新建的 LOGPARSE_RUN 从冻结的 `output/` 边界读取，允许两类来源混合；`output/` 目标的实际 size/SHA-256 还必须匹配已验证 TreeManifest 对应条目；
12. 对每个有文件内容的 draft 调用 S00 ResourceStore Port，以 `{job_id,proposal_key}` 所有权写入持久化暂存区并取得 `StagedResourceRef`；
13. 用实际 size/hash、暂存引用和原语义字段构造不含 Workspace 路径的规范 `JobOutcome`；对 `LOGPARSE_RUN`，`tree_manifest_sha256`、工具 VersionedRef、`parse_parameters.product` 和源 Attachment ID/hash 必须由 Runtime 从实际暂存结果与 Job 固定引用重建；`parse_manifest_relative_path` 必须从受控输出中唯一直接 task 目录推导，并在 staged TreeManifest 中验证目标为存在的普通文件，不能信任 Agent 自报值；
14. 重新执行规范 JobOutcome Schema、binding 和 proposal 唯一性校验；
15. 通过 `ExecutionRecordStore.publish_outcome_bytes` 发布规范 Outcome；合法 Agent 原始文件留在临时 Workspace，被拒绝的原始 Outcome 还会归档为 `jobs/<job_id>/agent_job_outcome.rejected.json`；
16. 向 S05 返回 `RuntimeExecutionReceipt(job_outcome,outcome_file_ref)`，不直接提交业务状态。

从第 6 步成功开始，无论 Backend、proposal、Outcome 发布、取消或超时如何结束，都必须在 `finally` 调用 broker session.close；关闭失败不覆盖更早的主失败，但必须进入安全诊断。Broker 启动的真实 logparse 子进程也必须绑定同一 CancellationSignal 和 S04 可终止进程树。

第 2 步读取最新聚合只作为 ID→不可变元数据解析表；ContextSnapshot、ProblemSpec、用户事实、requirements、候选和运行资产一律来自 Job 自身。资源元数据在 V1 不可原地修改，因此并发 Attachment 上传或 Case revision 前进不能改变已固定 ID 的内容；若同 ID 元数据与 Job/已发布执行清单矛盾，返回 `RESOURCE_HASH_MISMATCH` 或 `OUTCOME_INVALID`，不得选择另一个资源。

Workspace 根固定为 `DATA_ROOT/tmp/workspaces/<job_id>/`，子树固定为：

```text
inputs/
├─ manifest.json
├─ attachments/<attachment_id>/payload
├─ evidence/<evidence_id>/{payload|tree/...}
├─ artifacts/<artifact_id>/{payload|tree/...}
└─ outcomes/<outcome_id>/job_outcome.json
runtime/
├─ context.txt
└─ tool-state/
output/
├─ job_outcome.json
└─ proposals/<proposal_key>/...
```

`inputs/` 在 Backend 启动前整体设为只读；`manifest.json` 必须由 S04 作为唯一生产者按 S00 `WorkspaceInputManifest` 和 `workspace-input-manifest.schema.json` 生成，逐项记录 Job 的全部固定引用以及 Job 固定的 `logparse_tool_ref/logparse_product`，字段、判别分支、顺序、相对路径、大小与 SHA-256 均不得由 S04 私自扩展。`RESOURCE_MANIFEST` section 逐字使用同一文件。`runtime/context.txt` 必须逐字等于 `BoundedContext.body`。`runtime/tool-state/` 只允许服务侧 logparse broker 创建 `logparse-parse.claim`，以及安装的 `problem-locator-finalize-outcome` 创建 `agent-job-outcome.finalized`；两者可共存，任何其他节点均拒绝。Agent 不得直接增删改该目录，Runtime 在进程退出后分别校验 claim 与 finalization marker。子进程当前目录固定为 Workspace 根，因此 Prompt 和 Skill 只使用上述相对路径。stdout/stderr 分块写入 S02 实现的 `ExecutionRecordStore` 日志 sink，不放入业务输出。

固定 logparse Job 的 stdout/stderr 在进入持久 sink 前必须经过二进制流式 secret redactor：对当前 session endpoint/token 的 UTF-8 byte sequence 做精确匹配，跨分块保留最长 secret byte length 减 1 的尾窗，命中后用与命中字节数完全相等的 ASCII `*` 覆盖，close 时再安全 flush；因此写入 sink 的字节数逐字等于原始字节数，S02 的 64 MiB 计数无需第二个旁路计数器。原始 chunk、secret 和尾窗不得写日志、异常或临时文件。无 logparse Job 不创建该 secret 集合。此规则不承诺通用敏感数据检测，仍以 V1 可信 Agent/Skill 边界为前提。

任何 proposal path 必须位于 `output/proposals/<proposal_key>/`，解析后仍在当前 Workspace 的允许输出根内；符号链接、路径穿越和绝对路径不得逃逸。Agent 只能创建 `output/job_outcome.json` 和 proposals，不得修改输入。

### 6.4 Agent Backend

`CLAUDE_COMMAND` 解析和启动语义必须与 `issue-locator` 完全一致，并用特征测试锁定：

- 配置边界只传入一个完整命令字符串；S06 已按“进程环境优先于 env-file”得出该字符串；
- 使用 `shlex.split(command, posix=os.name != "nt")` 拆分，并对每个 token 再去掉一对首尾相同的单引号或双引号；
- 从开头连续读取环境赋值，名称必须匹配 `[A-Za-z_][A-Za-z0-9_]*`，空值合法、重复名称以后者为准；只含赋值而没有可执行文件必须失败；
- 不使用 Shell；
- 先按 `issue-locator` 语义合并父进程环境与前置赋值；随后执行 S00 保留键门禁：`CLAUDE_COMMAND` 若前置设置任一 raw/broker 保留键则 `CONFIG_INVALID`，从合并环境中大小写不敏感删除 `LOGPARSE_REPO`、`LOGPARSE_CONFIG_PATH`、`LOGPARSE_PYTHON` 和全部既有 `PROBLEM_LOCATOR_LOGPARSE_*`，最后仅在固定 logparse Job 中加入当前 broker session 返回的 endpoint/token；除这一安全过滤外继承与覆盖结果保持不变；
- Windows 上，若 argv[0] 含 `/`、`\` 或末段已经含扩展名则保持原值；否则用合并后环境的 `PATH` 调用 `shutil.which` 解析 `.cmd`/`.exe` shim，找不到时保持原 token；
- 除上述 Windows shim 替换外，argv 必须与拆分结果完全相同，不隐式追加模型、权限、prompt 或输出参数；
- Prompt 以 UTF-8 文本只经 stdin 提交，写完立即关闭 stdin；
- 子进程 `cwd` 固定为当前 Job Workspace 根；Prompt 明确要求业务结果写到相对路径 `output/job_outcome.json`；
- 每个 Job 创建新 Session，不传入旧 Session handle。

固定默认限制：

- wall time：`30 minutes = 1800 seconds`；
- stdout 与 stderr 合计：`64 MiB = 67108864 bytes`；
- Workspace 临时输出：`1 GiB = 1073741824 bytes`；
- 服务级 Agent Job 并发：`1`，全局并发闸门由 S05 实现，Backend 仍须正确处理单次执行限制。

Backend 必须在超时、取消、日志超限和 Workspace 超限时终止完整子进程树。POSIX 启动使用新 session/process group，先向进程组发 `SIGTERM`，最多等待 5 秒，再发 `SIGKILL` 并回收；Windows 启动使用 `CREATE_NEW_PROCESS_GROUP` 并立即加入启用 kill-on-close 的 Job Object，先发送受控终止，最多等待 5 秒，再关闭 Job Object 并回收。无法建立可终止的执行组时以 `BACKEND_START_FAILED` 失败，不能退化为只杀父进程。

Broker session 不属于 Agent 可控制的业务输出，但其真实 logparse 子进程属于同一次 Runtime 执行：S05 发出的 USER_CANCEL/SERVICE_SHUTDOWN 只经共享 CancellationSignal 传播；Runtime 自身 wall-time/output/workspace limit 不得改写该只读 signal，而是直接终止 Backend 进程树并在 `finally` 调用 session.close。close 必须同步回收 broker 子进程和 endpoint。任一路径检测到残留进程都以主失败不变、附加安全诊断并令 Runtime 清理测试失败，不能把残留交给下一 Job。

取消 signal 携带 `USER_CANCEL` 或 `SERVICE_SHUTDOWN` 原因：两者都产生 `BACKEND_CANCELLED`，前者通常因 CancelCase 已提交而成为 STALE，后者以 `retryable=true` 让仍活跃 Job 进入 INTERRUPTED。进程非零退出时不读取业务结果；只有成功退出后才读取结果文件。结果文件缺失、JSON 非法、Schema 非法或 Job 绑定不一致均直接形成 S00 ExecutionFailure；V1 不调用模型修复非法输出，也不自动重跑 Agent。

Agent 的 Write 只能生成语法有效的 JSON draft。安装的 Logparse/结果归档工具负责在消费各自 request 前严格校验、递归 Canonical 化并原子回写；`problem-locator-finalize-outcome` 负责规范化 USER_RESULT、刷新其派生 size/hash、校验并原子发布 `output/job_outcome.json`，随后写入匹配 marker。该 finalizer 必须是最后一个修改 Workspace 的命令。Runtime 只读取最终文件名，不读取 `.part` 文件，也不替 Agent 自动修复缺少 marker 的 Outcome。

若执行在有效 Agent 结果产生前失败，Runtime 自己构造绑定当前 Job 的规范 `ExecutionFailure` JobOutcome，并用 `ExecutionRecordStore.publish_outcome_bytes` 原子发布；`outcome_id` 必须来自注入的 `IdGenerator.new("job_outcome")`，`produced_at` 必须来自注入的 `Clock.now()`。系统失败结果不伪装成 AgentJobOutcome，也不覆盖 Agent 的原始文件。它不得把 stdout/stderr 或残缺 Agent JSON伪装成业务载荷。

若唯一失败点正是 ExecutionRecordStore，连系统失败 Outcome 也无法发布，Runtime 必须以 `IdGenerator.new("execution_failure")` 生成 `failure_id` 并抛出 S00 `RuntimeInfrastructureError`，由 S05 调用未发布失败入口。Runtime 禁止直接调用系统时钟、`uuid4()` 或其他随机 ID API。除该窄路径外，Runtime 不得抛出未类型化异常给 Worker。

## 7. 行为与错误码

所有错误阶段、错误码、`retryable` 语义、Case/Job 后续状态和 Outcome disposition 只引用 S00，不在本说明书新定义编码。

本切片必须覆盖 S00 错误表中与以下行为对应的条目：

- 必需上下文超出角色预算；
- 固定 Profile、Skill、Tool Bundle、Context Policy 或 Output Contract 不可加载；
- 固定资源缺失、哈希不一致或无法安全物化；
- Backend 创建失败、非零退出、超时、取消和完整进程树终止失败；
- stdout/stderr 合计超限、Workspace 临时输出超限；
- `job_outcome.json` 缺失、非法、Schema 不匹配、Job 绑定不一致；
- proposal path 逃逸、proposal 文件缺失、大小或哈希不一致；
- 持久化暂存失败。

Context Builder 超限必须产生 S00 的 `CONTEXT_LIMIT`，且证明确实未调用 Backend。固定资产不可用必须使用 S00 对应错误，不得替换最新版。可恢复与不可恢复分类只按 S00 返回，Runtime 不自行决定 Case 进入 `INTERRUPTED` 或 `FAILED`。

执行阶段与错误映射固定为：

| 失败点 | `ExecutionStage` | 错误码 |
|---|---|---|
| 固定资产解析/精确版本缺失 | `ASSET_RESOLUTION` | `ASSET_VERSION_UNAVAILABLE` |
| 必需上下文超过角色预算 | `CONTEXT_BUILD` | `CONTEXT_LIMIT` |
| 固定资源缺失、损坏或物化失败 | `WORKSPACE_PREPARE` | `RESOURCE_NOT_FOUND`、`RESOURCE_HASH_MISMATCH` 或 `WORKSPACE_PREPARE_FAILED` |
| Workspace 路径逃逸或非法节点 | `WORKSPACE_PREPARE` | `PATH_VIOLATION` |
| Agent 进程无法创建执行组 | `BACKEND_START` | `BACKEND_START_FAILED` |
| 超时、取消、非零退出 | `BACKEND_EXECUTE` | `BACKEND_TIMEOUT`、`BACKEND_CANCELLED`、`BACKEND_EXIT_FAILED` |
| stdout/stderr 或 Workspace 超限 | `BACKEND_EXECUTE` | `BACKEND_OUTPUT_LIMIT` 或 `WORKSPACE_LIMIT` |
| logparse 执行/输出失败 | `TOOL_EXECUTE` | `LOGPARSE_FAILED` 或 `LOGPARSE_OUTPUT_INVALID` |
| Agent 结果缺失、JSON/Schema/绑定非法 | `OUTCOME_VALIDATE` | `OUTCOME_MISSING` 或 `OUTCOME_INVALID` |
| Proposal 暂存失败 | `RESOURCE_STAGE` | `RESOURCE_STAGE_FAILED` |

若系统失败 Outcome 或执行记录本身无法可靠发布，使用 `ExecutionStage=EXECUTION_RECORD`、`code=EXECUTION_RECORD_FAILED` 构造 S00 `RuntimeInfrastructureError`；这是唯一不返回 `RuntimeExecutionReceipt` 的已知路径。

## 8. 关键边界与不变量

- Context Builder 只读 Job 固定快照，不读取执行时最新 DiagnosisState。
- Job 创建后新增的 Attachment、Evidence 或 Skill 版本不得进入当前 Job。
- Router、Specialist、Reviewer 三种上下文视图彼此不同，Reviewer 不获得 Specialist 完整对话。
- stdout/stderr 永远不是业务真相源。
- Agent Session 和 Workspace 均可丢弃；跨 Job 信息只能来自正式状态或正式/暂存资源引用。
- Runtime、Context Builder、Backend 均不能直接写 Case。
- 同一次输出中的 proposal key 在 Job 内唯一；正式 Evidence/Artifact ID 只能由 Application Service 分配。
- Runtime 不解包日志。S07 通过 logparse Skill 处理日志，Runtime 只物化输入与已保存的 `LOGPARSE_RUN`。
- 资源清理只调用 S00/S03 提供的清理合同；本切片不另建清理数据库或定时器。

## 9. Fake 与 Fixture

本切片必须在自己的测试目录提供：

1. `FakeStateRepository`：返回含 Job 固定 ID 元数据的 CaseAggregate，可注入跨 Case、缺失、重复和同 ID 元数据漂移；测试断言 Runtime 不读取其中最新 DiagnosisState 替换 Job snapshot。
2. `FakeResourceStore`：按固定引用返回字节或目录，可注入缺失、哈希错误、发布暂存失败和路径逃逸。
3. `FakeCatalogResolver`：返回固定 Profile/Skill/Tool/Policy/Contract，也可注入版本不可用。
4. `FakeLogparseBrokerFactory`：与同一 Fake ResolvedAsset 成对构造，记录固定 Job/manifest/cancellation，只返回 job-scoped endpoint/token；覆盖 asset/Job/manifest ref 完全相等、错配启动失败、open/close、外部取消、Runtime timeout/limit 不改写 signal、token 失效、跨 Job拒绝、broker 故障，以及 close 后全部 fake 子进程/endpoint 已回收。
5. `FakeClaudeCommand` 可执行 Fixture：
   - 正常读取 stdin 并原子写结果；
   - 缺失结果文件；
   - 非法 JSON；
   - 错误 `job_id`；
   - 非零退出；
   - 无限等待；
   - 创建子进程后等待，用于验证进程树终止；
   - stdout/stderr 洪泛；
   - Workspace 输出洪泛；
   - 先写 `.part` 后原子替换。
6. `issue-locator` 命令解析 Golden Fixture：覆盖 Linux/Windows 引号、空格、前置 env、shim 和环境继承；另断言 raw `LOGPARSE_*` 被剥离、保留键前置赋值拒绝、无 logparse Job 不含 broker 键、固定 Job 只含当前 endpoint/token，stdout/stderr 在 secret 跨 chunk 边界时仍落同长度 `*` 且 64 MiB 临界不漂移。AgentJobOutcome、Candidate/USER_RESULT、普通 proposal、tree 文件内容或路径回显 endpoint/token 时均产生 `OUTCOME_INVALID`：原 Agent Outcome/proposal 零 stage、零正式资源发布，session 关闭后仍按正常系统失败路径 finalize 一个不含 secret 的规范 Failure JobOutcome 并返回 receipt；只有 ExecutionRecordStore 本身失败才走 RuntimeInfrastructureError。预期值必须固化在本切片测试中。
7. 三种角色的 Context Fixture：覆盖恰好等于上限、超出 1 byte、`JOB_INSTRUCTION`/goal 与 `RESOURCE_MANIFEST` 必需保留、manifest 预留后可选 Evidence 的确定性选择，以及 Unicode UTF-8 计数。
8. `LOGPARSE_RUN` 目录物化 Fixture：只验证已保存目录可按固定引用只读物化，不读取其中日志语义。
9. 消费 S00 `tests/fixtures/contracts/` 中的共享 WorkspaceInputManifest/LogparseParseClaim/UserResultPayload golden 与 negative Fixture；另验证固定 `logparse_product`、四组 entry 顺序、previous outcome Canonical bytes/size/hash、RESOURCE_MANIFEST 逐字一致、USER_RESULT 任一语义字段错配和额外字段拒绝。
10. parse-once marker Fixture：同 Job 第二次 parse、含 LOGPARSE_RUN 的补参 Job 再 parse、marker 被 Agent 或 stub 伪造/改写均确定性失败，target-logs 复用仍成功；parse 创建 claim 后 hang 必须保留 `BACKEND_TIMEOUT`，不能被改写成 logparse 错误；Agent 自报不存在或非普通文件的 `parse_manifest_relative_path` 被 Runtime 重建/拒绝。
11. `FixedClock` 与 `DeterministicIdGenerator` Fixture：系统失败 Outcome 固定 `outcome_id=00000000-0000-4000-8000-000000000401`、`produced_at=2026-01-02T03:04:05.000Z`；ExecutionRecordStore 故障固定 `failure_id=00000000-0000-4000-8000-000000000402`，并断言 kind 分别逐字为 `job_outcome`、`execution_failure`。

Fake 只能实现 S00 Port，不得通过弱化生产接口方便测试。

## 10. 独立验证命令

从仓库根目录执行，以下命令必须全部以退出码 0 完成：

```powershell
python -m pytest -q tests/unit/runtime/test_context_builder.py
python -m pytest -q tests/unit/runtime/test_catalog.py
python -m pytest -q tests/unit/runtime/test_claude_command.py
python -m pytest -q tests/unit/runtime/test_agent_backend.py
python -m pytest -q tests/unit/runtime/test_diagnosis_runtime.py
python -m pytest -q tests/unit/runtime
```

最后一条必须包含 Windows 当前平台上的完整进程树取消测试。平台专属的 Linux 用例可以使用 pytest marker，但不得把当前平台可执行的超时、洪泛和子进程测试标为跳过。

## 11. 完成标准

- 责任区内生产代码不包含未完成占位、伪实现或仅为测试返回固定成功的路径。
- 三种角色的 UTF-8 正文预算按 `131072/204800/204800` 字节边界通过测试。
- Characterization tests 证明 `CLAUDE_COMMAND` 与 `issue-locator` 在所列语义上完全一致。
- 30 分钟、64 MiB、1 GiB 限制均有单位测试或可控加速测试，数值本身不可缩小写入生产默认值。
- 超时、取消和资源超限均验证完整子进程树已终止。
- Runtime 只在进程成功退出后读取原子结果文件，且所有错误均使用 S00 合同。
- 系统失败 Outcome 和 `RuntimeInfrastructureError` 的时间与 ID 只来自注入的 Clock/IdGenerator，固定值测试通过。
- 固定资产、固定资源和 Job 绑定不会被执行时最新状态替换；StateRepository 只读解析严格限于 Job 固定 ID。
- `python -m pytest -q tests/unit/runtime` 全绿。
- `git diff --name-only` 中本切片实现变更只位于第 4 节责任区。

## 12. 向 S08 的交接格式

完成后提交以下 JSON；不得只写自然语言总结：

```json
{
  "spec_id": "S04",
  "title": "Runtime, Context Builder and Agent Backend",
  "executor": {"model": "gpt-5.6-sol", "reasoning_effort": "ultra"},
  "contract_revision": "v1-contract-r1",
  "contract_base_commit": "<contract-base-commit>",
  "branch": "codex/v1-s04-runtime-context-backend",
  "head_commit": "<head-commit>",
  "scope_completed": [],
  "changed_files": [],
  "fixtures_consumed": [],
  "fixtures_produced": [],
  "tests": [
    {"command": "python -m pytest -q tests/unit/runtime", "status": "passed"}
  ],
  "dependency_requests": [],
  "contract_change_requests": [],
  "known_limitations": [],
  "risks": [],
  "integration_notes": [],
  "forbidden_scope_touched": false
}
```

以上顶层字段全部必填，不得省略；没有内容的列表写空数组。交接文件固定写入 `handoff/S04.json`。`scope_completed`、`changed_files` 与 `integration_notes` 必须填写真实结果。需要新增依赖时只填 `dependency_requests`，不得修改根依赖文件。若存在跳过项，必须在 `known_limitations` 中逐项解释，并由 S08 判断是否可接受。

## 13. S08 组合要求

S08 只通过 S00 Port 装配本切片，不得读取内部私有模块。至少验证：

- S05 认领的 Job 可交给 Diagnosis Runtime 执行；
- S07 的 Skill 可被 Catalog 以目录 SHA-256 固定并装配；
- S03 的 READY Attachment 和 `LOGPARSE_RUN` 可按 Job 固定引用物化；
- S02 重启后的旧 Job 不会使 Runtime 读取最新状态；
- S06 的取消命令能经 S05 传播到 Backend 并终止进程树。

组合失败时，S08 将缺陷退回本切片；S08 不越权修改 `src/problem_locator/runtime/**`。

## 14. 合同变更请求格式

发现 S00 无法支持正确实现时，停止相关越界实现并提交：

```json
{
  "request_id": "CCR-S04-001",
  "requesting_spec": "S04",
  "current_contract_revision": "v1-contract-r1",
  "problem": "现有合同无法实现或验证的精确问题",
  "proposed_change": "请求后的完整语义",
  "affected_types_or_codes": [],
  "affected_specs": ["S00", "S04"],
  "compatibility": "兼容性、持久化与迁移影响",
  "fixture_and_test_changes": []
}
```

只有 S00 所有者接受并更新合同后才能实现该变化。S04 不得先行加入兼容分支、私有字段或第二套错误码。
