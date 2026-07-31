# S07：Diagnosis Skill、Wiki 生成器与 logparse 集成实施说明书

- 状态：V1 独立开发合同
- 未来 Codex 开发任务模型：`gpt-5.6-sol`，`reasoning_effort=ultra`
- 公共合同：[S00-contract-freeze.md](S00-contract-freeze.md)
- 组合验收：[../v1-composition-spec.md](../v1-composition-spec.md)

## 1. 目标

本切片交付可由 S04 Catalog 扫描、固定和执行的诊断 Skill 资产：

1. 从 sibling `problem-locator-mcp` 仓库完整复制 `wiki-to-diagnosis-skill`，删除缓存与受管标记，并升级为 `2.0.0`。
2. 使生成的 `diagnose-*` Skill 支持 `NEED_INPUT | NEED_ATTACHMENT | COMPLETED | REROUTE`，并原子提交中间 DiagnosisStateDelta、Evidence/Artifact Draft 和 `job_outcome.json`。
3. 提供 `logparse-diagnose` Skill，把压缩日志处理完全委托给固定版本 logparse。
4. 提供由非敏感 Wiki Fixture 生成并校验的 `diagnose-service-takeover` 演示 Skill。
5. 实现“参数组 A → 一次日志 → 首次 parse → 参数 B → 新 Job 复用 LOGPARSE_RUN → Candidate”完整协议。
6. 用 Fake logparse 和真实 logparse 两层测试证明 parse 总次数为 1。

## 2. 非目标

- 不在 Problem Locator Python 服务代码中解析、枚举、解包、扫描、grep 或截取压缩日志。
- 不重新实现 logparse 支持的格式、递归、安全阈值、lifecycle 选择或内部错误判断。
- 不修改 S04 Catalog、Workspace 或 Backend；Skill 只遵守 S00 固定输入/输出合同。
- 不实现 Router、Coordinator、Application Service、Scheduler、MCP 或 HTTP 服务端。
- 不保存完整 Agent 对话、隐藏推理或未提升的临时工具轨迹。
- 不允许补参后的新 Job 再次解包或再次执行 parse。
- 不引入第二个日志附件来完成目标验收场景。
- 不支持 Skill 运行期热更新。

## 3. 上游合同

唯一规范上游是 S00。Skill 与生成器必须遵守：

- Job Workspace 输入/输出布局、固定资源引用和相对路径规则；
- DiagnosisOutcome、DiagnosisStateDelta、AgentEvidenceProposalDraft、AgentArtifactProposalDraft 和 AgentJobOutcome Schema；
- WorkspaceInputManifest、LogparseParseClaim、Evidence locator 和 Artifact metadata discriminated union；
- `proposal_key` Job 内唯一性、Workspace 相对路径和声明大小/SHA-256 规则；`StagedResourceRef` 只由 Runtime 生成；
- result type、ArtifactKind=`LOGPARSE_RUN`、固定版本资产引用和错误分类；
- Attachment/Evidence/Artifact 不可变与 Job 绑定规则。

S04/S05 的具体实现不是本切片单元测试前置条件。测试通过 Workspace Fixture 和 Fake logparse 驱动 Skill；S08 负责真实 Runtime 装配。

## 4. 唯一文件责任区

本切片是以下路径的唯一所有者：

```text
src/problem_locator/integrations/logparse/**
.claude/skills/wiki-to-diagnosis-skill/**
.claude/skills/logparse-diagnose/**
.claude/skills/diagnose-service-takeover/**
tests/unit/integrations/**
tests/fixtures/components/logparse/**
handoff/S07.json
```

`tests/unit/integrations/conftest.py` 可以定义 `--run-real-logparse`，但不得新增仓库级 `tests/conftest.py`。
S07 的可复用 logparse 组件 Fixture 及其 manifest 只能写入 `tests/fixtures/components/logparse/**`。`tests/fixtures/rpc_timeout/**` 和 `tests/fixtures/failures/**` 由 S08 独占；S08 在集成阶段引用或复制已经交接的组件 Fixture，S07 不直接写跨模块场景目录。

源复制位置固定为：

```text
../problem-locator-mcp/.claude/skills/wiki-to-diagnosis-skill/
```

复制时排除以下非产品内容：

```text
**/__pycache__/**
**/.pytest_cache/**
**/*.pyc
**/.DS_Store
**/.managed
**/.managed.*
**/.codex-managed
```

除上述项和版本/输出合同升级所需修改外，源生成器文件必须完整保留；测试生成复制清单与 SHA-256 差异报告。

## 5. 禁止修改项

- 不得修改 `src/problem_locator/**` 中除 `src/problem_locator/integrations/logparse/**` 外的文件，也不得修改 S00～S06、S08、`pyproject.toml` 或锁文件。
- 不得修改 S06 所有的 `.claude/skills/problem-locator-client/**`。
- 不得在 Skill 中使用 Python 标准库或系统命令自行打开 zip/tar/gzip/7z 等归档。
- 不得用 `grep`、`rg`、`findstr` 或自制脚本扫描原始日志内容。
- 不得绕过 logparse 直接选择目标日志或 lifecycle。
- 不得把 logparse 临时目录路径当成跨 Job 依赖；必须提出 `LOGPARSE_RUN` AgentArtifactProposalDraft，由 Runtime 转成持久化暂存 Proposal。
- 不得把中途补参当作执行失败；合法 NEED_INPUT/NEED_ATTACHMENT 必须让当前 Job 正常结束并写有效 Outcome。
- 不得在新 Job 看不到 LOGPARSE_RUN 时假装复用；缺失/哈希不一致按 S00 错误返回。
- 不得把生成器版本保持在 1.x，也不得原地覆盖同版本语义。
- 不得写入敏感 Wiki 内容、生产凭据、真实客户日志或真实订单号 Fixture。

## 6. 输入输出契约

### 6.1 Wiki 生成器 2.0.0

生成器自身版本固定为 `2.0.0`。输入是非敏感 Wiki 内容、生成参数和目标 Skill 的语义版本；首次生成的 Skill 版本必须从 `2.0.0` 开始，产品内容变化时必须显式提升版本，不得以同一 `{id,version}` 覆盖旧语义。输出是一个完整 `diagnose-<capability>` 目录。

每个生成目录至少包含 `SKILL.md` 和 S04 Catalog 要求的 `diagnosis-skill.json`。manifest 字段逐字为：

```json
{
  "schema_version": 1,
  "id": "diagnose-<capability>",
  "version": "2.0.0",
  "capability": "<稳定能力标识>",
  "summary": "<Router 使用的非敏感摘要>",
  "entry_document": "SKILL.md",
  "tool_bundle_id": "tool-bundle/diagnose",
  "requires_logparse": true,
  "logparse_product": "<固定 product>"
}
```

不需要日志的 Skill 必须令 `requires_logparse=false`、`logparse_product=null`；需要日志时二者必须如上。`summary` 是 Router 唯一可见的 Skill 正文摘要，不能包含 Wiki 敏感细节。生成器必须拒绝同目录已有相同 id/version 但产品 hash 不同的覆盖请求。

生成 Skill 必须：

- 在缺少结构化参数时返回 `NEED_INPUT` 和符合 S00 的稳定 requirement ID/name/INPUT constraints；RPC 超时 Fixture 的参数组 A 名称固定为 `caller_service`、`server_service`、`rpc_method`、`problem_time`，其中 problem_time 必须是 S00 毫秒精度 UTC RFC 3339 时刻，分析中途参数 B 固定为 `order_id`；
- 在需要首次日志时返回 `NEED_ATTACHMENT`，创建唯一 OPEN ATTACHMENT requirement `name=log_archive`、`min_count=max_count=1`，其 `allowed_content_types[]` 由固定 logparse 版本支持格式生成；每项必须先通过 S00 Canonical ContentType grammar、逐字唯一且保持工具声明顺序，任何大写、参数、空白/控制字符、CRLF、非 ASCII 或超长值都使产品生成失败，不得自行规范化；requirement 明确只接受一次目标场景日志；
- 在能力不匹配时返回 `REROUTE`，不自行调用 Router；
- 在形成候选结论时返回 `COMPLETED`，逐项给出满足 S00 覆盖/证据规则的 completion-criteria draft mapping，但不声称 Case 已 RESOLVED；
- 可提交中间事实/假设/问题/requirements 的 StateDelta 提案；
- 可提交 Evidence/Artifact proposal，文件只使用 Workspace 内相对路径；
- 将最终结果原子写入 `output/job_outcome.json`；stdout/stderr 只记录安全执行摘要。

任何 `COMPLETED` 且含 CandidateConclusionDraft 的输出都必须同时提出恰好一个 USER_RESULT AgentArtifactProposalDraft；没有 candidate 时禁止 USER_RESULT。该草稿固定为：

```text
proposal_key = "user-result"
artifact_kind = USER_RESULT
name = "diagnosis-result.json"
content_type = "application/json"
resource_kind = FILE
workspace_relative_path = "output/proposals/user-result/payload"
metadata = {
  schema_version: 1,
  format_id: "problem-locator-diagnosis-v1",
  description: "Diagnosis result"
}
```

payload 必须逐字使用 S00 `UserResultPayload` / `user-result.schema.json` 和 Canonical JSON，不在 S07 定义私有结果格式；其字段为 `{schema_version:1,format_id:"problem-locator-diagnosis-v1",problem_statement,candidate_statement,supporting_evidence_bindings[],completion_criteria_mapping[]}`。后四项逐字来自本 Job 固定 ProblemSpec 与同一 Outcome 的 CandidateConclusionDraft，不写时间、Workspace 路径或正式 ID 猜测。相同 candidate 必须生成相同字节。Runtime 在 stage 前校验实际 payload，S03 重算 size/hash 并验证 binding 正式化映射；S01/S03 负责与 candidate 同批接受和发布，Skill 不自行判定 downloadable。

生成器输出必须通过 S00 AgentJobOutcome Schema 校验和目录哈希稳定性测试；Runtime 规范化 Fixture 还必须通过 JobOutcome Schema。同一规范化 Wiki 和相同生成参数必须产生字节稳定的产品文件；时间戳不得进入产品内容哈希。

### 6.2 logparse 调用链

调用链固定为：

```text
Specialist
  → diagnose-*
  → logparse-diagnose
  → 固定版本 logparse parse
  → 固定版本 logparse mech-target-logs
```

logparse 的解释器、仓库与配置分别来自服务侧只读配置：

```text
LOGPARSE_PYTHON
LOGPARSE_REPO
LOGPARSE_CONFIG_PATH
```

这些 raw 值只注入 S07 的服务侧 `LogparseBrokerFactory`，不得进入 Agent 子进程、Workspace manifest、Context 或 stub 命令行。Skill 只调用随服务安装且由 S00 工程骨架注册的 `problem-locator-logparse` broker 客户端；客户端只读取 S00 session 给出的 job-scoped endpoint/token，不得读取、定位或拼接上述三个配置。固定入口为：

S07 对组合根提供唯一服务侧构造函数 `build_logparse_runtime(logparse_repo, logparse_config_path, logparse_python) -> (ResolvedAsset, LogparseBrokerFactory)`：三个参数逐字来自 S06 同一个不可变 Settings；它先按本节固定算法生成 `ResolvedAsset(asset_kind=LOGPARSE_TOOL)`，再构造持有同一 ref/fingerprint 与 raw 配置的 BrokerFactory。任一步失败即启动失败/readiness=false，不得只返回其中一半或回退 direct CLI。`open` 时 Factory 必须证明 `job.logparse_tool_ref == resolved_asset.ref == workspace_manifest.logparse_tool_ref`，任一不等返回 `ASSET_VERSION_UNAVAILABLE` 且不启动 endpoint/子进程。

```text
problem-locator-logparse parse-targets
  --request output/proposals/<proposal_key>/request.json
  --result output/proposals/<proposal_key>/target_logs.json

problem-locator-logparse target-logs
  --request output/proposals/<proposal_key>/request.json
  --result output/proposals/<proposal_key>/target_logs.json
```

两个路径都必须是当前 Workspace 内的相对 POSIX 路径。request 使用 Canonical JSON，公共字段为 `schema_version=1`、`problem_time` 和 `anchors[]`；problem_time 逐字复制已校验的参数 A 单值，不接受 range，也不做取中点或时区猜测；每个 anchor 只含 `label,module,slot,process_name,pid?`。`parse-targets` 另外只含 `attachment_id,artifact_proposal_key`，`target-logs` 另外只含 `artifact_id`；两个 request 都禁止 `logparse_product`。Agent stub 只把两个相对路径和 request bytes 发给当前 broker。Broker 按 S00 `workspace-input-manifest.schema.json` 读取只读 `inputs/manifest.json`：通过对应 discriminated entry 把 ID 映射到 relative_path，并逐字取得 Job 固定 `logparse_tool_ref` 与 `logparse_product`；它不得扫描 inputs、补猜文件名、信任 Agent 自报 product 或读取 Repository。endpoint/token 必须绑定本 Job、一次 session 和 Workspace，关闭后失效。Broker 拒绝绝对路径、仓库路径、配置路径、解释器路径或任意 CLI 选项。result 是单个 `target_logs` JSON object；stdout/stderr 只有安全摘要，Agent 必须读取 result 文件。

`parse-targets` 的 parse-once 门禁由服务侧 broker 强制，不依赖 Skill 或 Agent stub 自律：

- `inputs/manifest.json` 只要含任一 `input_kind=ARTIFACT, artifact_kind=LOGPARSE_RUN` entry，立即以 `LOGPARSE_FAILED`、`retryable=false` 拒绝 parse-targets；补参 Job 即使仍固定原 Attachment 也只能调用 target-logs。
- 首次调用在启动 logparse 前，由 broker 以 create-new 语义原子创建 Runtime 保留的 `runtime/tool-state/logparse-parse.claim`。内容必须逐字使用 S00 `LogparseParseClaim`：`job_id` 来自 manifest，`attachment_id/attachment_sha256` 来自目标 Attachment entry，`artifact_proposal_key` 来自 request，`logparse_tool_ref` 来自 manifest，`request_sha256` 来自本次 request Canonical bytes；同一 Job 的第二次 parse-targets 无论换 request、proposal key 或 Attachment 都拒绝。Agent stub 没有写入 tool-state 的 API。
- claim 在 Job 生命周期内不删除；首次 parse 失败也由 JobOutcome/ExecutionFailure 结束当前执行，不在同一 Job 内偷偷重试。S04 在 Backend 退出后按 S00 公共 Schema/矩阵校验，S07 不定义第二份 marker DTO。

因此正常 R09→R12 链路只有“无 LOGPARSE_RUN 且无 claim”的首次 Job 可以 parse。故障导致首次 parse 未形成可接受 LOGPARSE_RUN 时，恢复属于失败重执行语义，不冒充已复用结果；R01～R14 的无故障发布门禁仍要求实际 parse 进程启动次数严格为 1。

Broker 再使用固定仓库版本提供的正式 logparse CLI，不复制其算法。所有子进程以参数数组启动，不通过 Shell 拼接，并绑定 S04 传入的 CancellationSignal 与可终止进程树；格式支持、安全限制、递归行为和内部错误以该固定 logparse 版本为唯一权威。

`src/problem_locator/integrations/logparse/**` 只实现 Agent stub、服务侧 broker、受控 CLI adapter 与启动时资产指纹：broker 输入为已由 S04 物化的 Attachment 路径或 `LOGPARSE_RUN` 根、当前 Job 的 proposal 输出根、固定配置引用、anchors 和取消信号；输出为 logparse 进程回执、`parse_manifest.json` 引用与 `target_logs` JSON。`logparse-diagnose` 只能通过 broker 发起下面冻结的 argv，不能自行拼接另一套命令。只有 broker 可以启动和终止 logparse 进程；stub 不获得 raw 配置或任意 argv 能力。二者都不得打开输入归档、解释日志内容、选择 lifecycle、重写 manifest 或读取 target log 正文，只使用 S00 既有错误。

Broker session 的 endpoint/token、关闭与子进程归属逐字采用 S00：token 只绑定一个 Job/Workspace，close 先失效 token，再同步终止并回收该 session 全部 logparse 子进程和 endpoint。S07 必须让子进程响应 S04 传入的 CancellationSignal；不得另建脱离 Runtime 生命周期的 daemon、全局复用 token 或跨 Job 进程池。

logparse 运行资产在服务启动时固定。先执行 `git -C LOGPARSE_REPO ls-files --cached --others --exclude-standard`；失败、空结果、非 UTF-8 路径或越界路径均使配置无效。把路径规范成相对 POSIX 形式并按 Unicode 码点排序，为每个文件生成 `{path,size,sha256}`，再对 S00 Canonical JSON `{version:1,entries:[...]}` 求 `repo_tree_sha256`。随后对以下对象求 Canonical JSON SHA-256，结果作为 VersionedRef.content_hash：

```json
{
  "repo_tree_sha256": "<sha256>",
  "config_sha256": "<LOGPARSE_CONFIG_PATH 文件字节 sha256>",
  "python_resolved_path": "<LOGPARSE_PYTHON resolve 后绝对路径>",
  "python_version": "<LOGPARSE_PYTHON --version 合并并 trim 后的单行输出>"
}
```

路径值只参与 hash，不进入 Job、状态、Agent 输出或外部响应。VersionedRef 固定为 `{id:"logparse-tool/logparse",version:"sha256-<content_hash前16位>",content_hash:"<完整hash>"}`。该值就是同次 `build_logparse_runtime` 返回的 ResolvedAsset.ref 和 BrokerFactory 内部固定 ref；每个使用日志的 Job 固定该 ref。执行前重新计算不一致、Catalog/Factory 不是同一构造结果或 Job/manifest ref 不一致时返回 `ASSET_VERSION_UNAVAILABLE`，不得运行变化后的最新版。缓存和 Git ignore 文件不参与 hash。

首次 parse argv 固定为：

```text
<LOGPARSE_PYTHON> <LOGPARSE_REPO>/cli.py parse <input_path>
  -c <LOGPARSE_CONFIG_PATH>
  -o <workspace>/output/proposals/<artifact_proposal_key>/tree
  --product <diagnosis-skill 固定的 logparse_product>
```

不得添加 `--debug-expand-gz`、`--profile` 或 `--keep-workspace`。每次调用使用新的空 output root；该 root 同时就是 `LOGPARSE_RUN` AgentArtifactProposalDraft 的 `workspace_relative_path`。成功后只允许出现一个直接任务目录 `<task_id>/`，且其中必须有合法 `parse_manifest.json`；`parse_manifest_relative_path` 固定为 `<task_id>/parse_manifest.json`。Artifact 保存完整 output root，而不是只保存 task 子目录，因此后续 Job 可把 materialized `tree/` 原样作为 `--output`。枚举这个受控输出根的唯一直接任务目录不等于枚举原始压缩包。

每个目标 anchor 的 argv 固定为：

```text
<LOGPARSE_PYTHON> <LOGPARSE_REPO>/cli.py mech-target-logs <task_id>
  --output <workspace>/output/proposals/<artifact_proposal_key>/tree
  --problem-time <ISO_TIME>
  --module <module>
  --slot <slot_id>
  --process-name <name>
  [--pid <pid>]
  [--label <label>]
```

`logparse_product` 只来自 S04 根据 Job 固定 Skill 生成的只读 WorkspaceInputManifest，module 和 anchor 字段来自参数组 A；不得由 Agent request 或包装器猜测。`mech-target-logs` stdout 必须是单个合法 JSON object，作为 target_logs 机器结果；stderr 只作执行日志。任何 `target_logs[*].log_path` 都必须解析到当前受控 output root 内；Agent 当前轮可以读取这些路径，但持久化的 Evidence locator 只能保存相对 output root 的 POSIX 路径，禁止保存绝对路径。

### 6.3 首次日志 Job

在没有固定 `LOGPARSE_RUN` 引用时：

1. Specialist 先取得参数组 A；参数组 A 至少能确定调用方、服务端、RPC 方法和唯一问题发生时刻 `problem_time`。
2. 只接受 Job 固定引用中的一个 READY 日志 Attachment。
3. 调用 logparse `parse` 恰好一次，输出到 `output/proposals/<artifact_proposal_key>/tree`。
4. 读取 logparse 生成的 `parse_manifest.json` 作为机器结果；Skill 不枚举原归档。
5. 调用 logparse `mech-target-logs` 获得目标日志材料。
6. 若分析需要参数 B，生成稳定 requirement，例如 RPC 超时场景中的 `order_id`。
7. 同一 Outcome 提出可定位 Evidence 和内部 `LOGPARSE_RUN` Artifact；Artifact 是 `resource_kind=DIRECTORY`、`content_type=application/vnd.problem-locator.logparse-run+directory` 的完整只读 output root，metadata 必须逐字使用 S00 `LogparseRunMetadata`，其中 parse_parameters 只有 `{product:<固定 logparse_product>}`。每条 LOGPARSE Evidence 使用 S00 `LogparseEvidenceLocator`，relative_path 是 target_logs 返回路径相对 output root 的安全 POSIX 路径，行/时间边界按实际机器结果填写或显式 null；source binding 用同一 Outcome 的 Artifact proposal key。二者必须被同一计划共同接受。
8. 原子写 `NEED_INPUT` AgentJobOutcome 并正常退出；Runtime 随后 stage 目录并构造规范 JobOutcome。

当前 Job结束后，是否接受提案和进入 WAITING_INPUT 由 Application Service/Coordinator 决定，Skill 不直接修改 Case。

### 6.4 补参后的新 Job

新 Job 固定引用已接受的 `LOGPARSE_RUN` 和参数 B 时：

1. 校验 ArtifactKind、`parse_manifest_relative_path`、完整目录哈希和固定 logparse 版本；
2. 使用 S04 已物化为 `inputs/artifacts/<artifact_id>/tree/` 的只读 output root，并从 manifest 相对路径恢复唯一 task ID；
3. 严禁再次执行 logparse `parse`；
4. 以 `--output inputs/artifacts/<artifact_id>/tree` 调用 logparse 提供的后处理/目标日志能力，围绕参数 B 继续分析；
5. 形成新的 Evidence/Artifact Draft 或 CandidateConclusionDraft；若形成 Candidate，必须同时按 6.1 生成唯一 `user-result` 草稿和确定性 payload；
6. 原子写本 Job 的 `job_outcome.json`，candidate、supporting Evidence binding 与 USER_RESULT 必须位于同一 Outcome。

parse 计数是无故障 RPC 主场景的可观测验收量：从首次日志到 Candidate/Review 前总次数必须严格为 1。正式 LOGPARSE_RUN 被接受前的失败替代遵循 6.2 的至少一次口径，不能用这个主场景断言伪装成全故障 exactly-once。

### 6.5 RPC 超时演示 Skill

`diagnose-service-takeover` 使用非敏感 Wiki Fixture，至少覆盖：

- 参数组 A：调用方、服务端、RPC 方法、唯一 UTC 故障发生时刻；
- 一次日志 Attachment；
- 客户端 3 秒 deadline exceeded Evidence；
- 解析后请求参数 B：`order_id`；
- 新 Job 复用 `LOGPARSE_RUN`，以 order_id 定位服务端连接池等待或处理延迟；
- 形成带完整 supporting evidence refs 的 CandidateConclusion；
- 与 Candidate 同 Outcome 生成固定 `diagnosis-result.json` USER_RESULT，独立 REVIEW PASS 后由 R14 下载同一字节；
- 最终 REVIEW 由 S08 驱动，不由 Skill 自行 PASS。

演示数据不得包含真实服务名、客户标识、凭据或生产日志。

## 7. 行为与错误码

本切片不定义错误码。所有 logparse 启动失败、parse 失败、manifest 缺失、固定版本不匹配、LOGPARSE_RUN 缺失/损坏、输出 Schema 错误、路径逃逸和上下文错误均使用 S00 定义的错误及 ExecutionFailure 分类。

业务性缺参使用 S00 的合法 result type，不伪装成错误：

- 缺参数：`NEED_INPUT`；
- 需要首次日志：`NEED_ATTACHMENT`；
- 能力不匹配：`REROUTE`；
- 已形成本阶段候选：`COMPLETED`。

logparse 自身判定不支持的格式、安全阈值超限或归档错误时，Skill 必须保留其安全摘要和机器可判定结果，并映射到 S00 已冻结的失败合同；不得改写成自定义 Skill 错误码。

## 8. 关键边界与不变量

- Problem Locator 不处理压缩日志；logparse 是唯一权威。
- 一次日志、一次 parse；补参 Job 只复用正式 LOGPARSE_RUN。
- Job Session 可丢弃；跨 Job 依赖只能通过结构化状态、Evidence、Attachment 和 Artifact 引用。
- NEED_INPUT 返回前必须先把后续需要的解析目录作为 AgentArtifactProposalDraft 提交；Runtime 必须在返回执行回执前把它转换为带 StagedResourceRef 的 ArtifactProposal，不得只留 Workspace 临时文件。
- proposal 被接受前不是正式业务记录；Skill 不分配正式 Evidence/Artifact ID。
- 用户陈述与日志证据必须区分 provenance；服务端延迟在没有对应 Evidence 时只能是假设。
- Candidate 不是 final result，必须经过独立 REVIEW PASS。
- Skill 固定版本不可原地覆盖；生成器 2.0.0 输出由完整目录 SHA-256 固定。
- Skill、Agent stub 和 Agent 子进程不得获得或泄露 LOGPARSE_REPO、配置/解释器绝对路径或环境变量值；受支持的真实 logparse 子进程只能由 job-scoped broker 启动。
- endpoint/token 只能用于 stub→broker 传输；不得写入 AgentJobOutcome、Candidate、USER_RESULT、Evidence/Artifact proposal 的路径或内容。S04 会在任何 stage/publish 前做跨分块精确拒绝扫描。
- V1 仍采用基线中的可信用户/可信 Skill、无操作系统级 Agent 沙箱假设；parse-count=1 的发布门禁覆盖受支持 broker 工作流和全部合同内故障，不宣称能阻止恶意本机进程主动枚举宿主文件系统。这一风险不能被实现成 direct-CLI 降级路径。

## 9. Fake 与 Fixture

本切片必须提供：

1. `FakeLogparse` CLI：支持 `parse` 与 `mech-target-logs`，记录每个只由 broker 发起的子命令调用次数和参数数组。
2. `FakeLogparseBrokerFactory/Session` 与 Agent stub：覆盖 S06 同一 Settings 构造的 ResolvedAsset/Factory ref 一致、错配启动失败，endpoint/token 的 Job/Workspace 绑定、关闭失效、跨 Job 请求、任意 argv、raw 环境变量缺失和 direct-CLI 降级拒绝。
3. Fake parse 结果：产生规范 `parse_manifest.json`、目标日志目录和可定位 RPC Evidence。
4. 失败 Fixture：不支持格式、manifest 缺失/非普通文件、非零退出、超时、输出目录逃逸、解析目录哈希不符、同 Job 第二次 parse、含 LOGPARSE_RUN 的补参 Job 再 parse、公共 parse claim 篡改/伪造，以及 parse 后 hang 保留 `BACKEND_TIMEOUT`。
5. 两 Job Workspace Fixture：消费 S00 共享 WorkspaceInputManifest golden；Job 1 含一次日志与参数 A，Job 2 含参数 B、previous outcome、源 Attachment和已物化 LOGPARSE_RUN；product 从只读 manifest 取得，字段/排序与 S04 生产物逐字兼容。
6. RPC 超时非敏感日志 Fixture：客户端 3 秒 deadline 和服务端连接池等待记录，ID 全部为合成值。
7. Wiki Fixture：用于确定性生成 `diagnose-service-takeover`。
8. `parse_counter.json`：由 Fake 和真实 broker 记录，最终断言严格等于 1。
9. 静态边界测试：扫描 `src/problem_locator/**`，拒绝出现针对输入归档的解包实现或绕过 broker 的 raw logparse argv；允许依赖管理或无关文本必须用精确 allowlist 解释。
10. 真实 logparse pytest Fixture：服务侧从 `LOGPARSE_REPO`、`LOGPARSE_CONFIG_PATH`、`LOGPARSE_PYTHON` 加载固定环境，仅在显式 `--run-real-logparse` 时运行，并断言 Agent 环境没有这些键。
11. Candidate/USER_RESULT Fixture：有候选时恰好一个固定草稿、payload 逐字通过 S00 Schema 且 Canonical bytes 稳定；无候选、缺失/重复结果、语义错配、metadata 或 content type 错误均被合同拒绝。
12. ContentType Fixture：logparse 支持格式全部满足 S00 Canonical grammar 且逐字唯一；大写、参数、空白/控制字符、CRLF、非 ASCII、超长或重复项使产品生成失败。

真实测试不得上传或提交真实客户日志；使用本切片合成归档 Fixture。

## 10. 独立验证命令

基础验收必须执行：

```powershell
python -m pytest -q tests/unit/integrations/test_generator_copy.py
python -m pytest -q tests/unit/integrations/test_generator_v2.py
python -m pytest -q tests/unit/integrations/test_skill_contract.py
python -m pytest -q tests/unit/integrations/test_logparse_fake_e2e.py
python -m pytest -q tests/unit/integrations/test_no_archive_processing.py
python -m pytest -q tests/unit/integrations
```

真实 logparse 发布门禁必须在配置好固定版本后执行：

```powershell
python -m pytest -q tests/unit/integrations/test_logparse_real_e2e.py --run-real-logparse
```

真实门禁必须断言参数 A、一次日志、参数 B、新 Job 复用和 parse count=`1`。未执行真实门禁时可以完成本切片独立开发，但必须在交接 `known_limitations` 中记录，且 S08 不得开始最终组合；V1 最终组合验收不能豁免该门禁。

## 11. 完成标准

- 生成器从指定 sibling 源完整复制，排除清单有机器验证，版本为 2.0.0。
- 生成 Skill 的四种 result type 和中间 Delta/Evidence/Artifact Draft 全部通过 AgentJobOutcome Schema，Runtime 规范化结果通过 JobOutcome Schema。
- `diagnose-service-takeover` 可由非敏感 Wiki Fixture 确定性再生成且目录哈希一致。
- Fake E2E 中首次 Job parse 一次并提交 LOGPARSE_RUN，补参新 Job parse 零次，总计严格为 1。
- parse-once 是 Adapter 强制门禁：换 proposal key、保留原 Attachment 或直接重复调用都不能触发第二个 parse 进程。
- 真实 logparse E2E 同样总计严格为 1。
- `parse_manifest.json` 是机器结果入口，Problem Locator 与 Skill wrapper 均未自行枚举原归档。
- NEED_INPUT 前解析结果已从 Draft 转成可持久化 ArtifactProposal，不依赖旧 Workspace/Session。
- S04 生产与 S07 消费的 WorkspaceInputManifest/LogparseParseClaim 通过同一 S00 golden/negative Fixture，不存在私有字段。
- Candidate 与唯一 USER_RESULT 位于同一 Outcome，内容由同一 mapping 确定性生成；独立 REVIEW 门禁保留，R14 可下载被接受候选所属结果。
- `python -m pytest -q tests/unit/integrations` 与真实门禁均全绿。
- `git diff --name-only` 中本切片实现变更只位于第 4 节责任区。

## 12. 向 S08 的交接格式

```json
{
  "spec_id": "S07",
  "title": "Diagnosis Skills and logparse Integration",
  "executor": {"model": "gpt-5.6-sol", "reasoning_effort": "ultra"},
  "contract_revision": "v1-contract-r1",
  "contract_base_commit": "<contract-base-commit>",
  "branch": "codex/v1-s07-skill-logparse",
  "head_commit": "<head-commit>",
  "scope_completed": [],
  "changed_files": [],
  "fixtures_consumed": [],
  "fixtures_produced": [],
  "tests": [
    {"command": "python -m pytest -q tests/unit/integrations", "status": "passed"},
    {
      "command": "python -m pytest -q tests/unit/integrations/test_logparse_real_e2e.py --run-real-logparse",
      "status": "passed"
    }
  ],
  "dependency_requests": [],
  "contract_change_requests": [],
  "known_limitations": [],
  "risks": [],
  "integration_notes": ["generator_version=2.0.0", "observed_parse_count=1"],
  "forbidden_scope_touched": false
}
```

以上顶层字段全部必填，不得省略；没有内容的列表写空数组。交接文件固定写入 `handoff/S07.json`。`integration_notes` 必须填每个交付 Skill 的真实完整目录 SHA-256。真实 logparse 门禁未运行或有 skip 时，交接必须记录限制，S08 不得接受。

## 13. S08 组合要求

S08 至少执行 RPC 超时链路：

1. 创建 Case；
2. Specialist 请求参数组 A；
3. 用户分批补齐 A；
4. Specialist 请求一次日志 Attachment；
5. 上传并 SubmitSupplement；
6. 首次日志 Job 调用真实 logparse parse 一次，保存 Evidence 和内部 LOGPARSE_RUN；
7. Specialist 在分析中途请求参数 B=`order_id`；
8. 新 Job 使用新 Session，并物化已保存 LOGPARSE_RUN；
9. 新 Job 不再 parse，围绕 order_id 形成 Candidate；
10. 独立 Reviewer PASS，Case RESOLVED；
11. 下载用户可见最终 Artifact；
12. 全链路 parse 总次数断言为 1。

S08 还必须静态证明 `src/problem_locator/**` 不含输入归档解包/日志扫描逻辑。组合缺陷退回本切片，S08 不直接修改 S07 责任区。

## 14. 合同变更请求格式

```json
{
  "request_id": "CCR-S07-001",
  "requesting_spec": "S07",
  "current_contract_revision": "v1-contract-r1",
  "problem": "现有合同无法实现或验证的精确问题",
  "proposed_change": "请求后的完整 Skill/资源语义",
  "affected_types_or_codes": [],
  "affected_specs": ["S00", "S07"],
  "compatibility": "对生成 Skill、Runtime、持久化或 E2E 的影响",
  "fixture_and_test_changes": []
}
```

只有 S00 所有者接受并更新合同后才能实现变化。不得用私有 result type、自定义错误码或重复 parse 绕过合同。
