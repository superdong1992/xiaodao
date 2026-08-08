# Diagnose output contract

本合同定义所有 DIAGNOSE Job 的公共输出边界。业务参数、日志映射、事件提取和判定规则
只以当前固定的 Diagnosis Skill 为准。

将一个完整 `AgentJobOutcomeDraftV2` 写入
`output/job_outcome.draft.json`。该文件必须被
`schemas/v2/agent-job-outcome-draft.schema.json` 接受。

以下安装时固定的 Schema 对所有嵌套字段具有最终解释权：

<<<BEGIN S00 AGENT JOB OUTCOME DRAFT SCHEMA>>>
{{S00_AGENT_JOB_OUTCOME_DRAFT_SCHEMA_JSON}}
<<<END S00 AGENT JOB OUTCOME DRAFT SCHEMA>>>

The top-level object has exactly these twelve fields and no others:
`["base_state_revision","case_id","consumed_evidence_refs","error","job_id","job_type","payload","proposed_artifact_drafts","proposed_evidence_drafts","result_type","rule_claims","schema_version"]`。

设置 `schema_version=2`，从 `JOB_INSTRUCTION` 和 `RESOURCE_MANIFEST` 逐字复制
Job/Case 绑定。Agent draft 不得包含 `outcome_id`、`produced_at`、
`decision_audit` 或任何服务端校验字段。`FAILED` 需要 `payload=null` 和非空 error；
非失败 draft 需要 `error=null`。

Never create a temporary file at workspace root; its direct children remain exactly
`inputs`, `runtime`, and `output`.

## Requirement 与固定输入

只按当前 Skill 的 requirements 声明请求缺失输入。仅 `supplement_policy=MISSING_ONLY`
且当前确实缺失的 OPEN requirement 可以由用户补充；已有的 `problem_time`、service、
method、order 或其他 USER_FACT 是当前 Case 的冻结输入，禁止请求替换。若证据表明冻结输入
错误或与日志不匹配，本 Case 不得形成 Candidate，服务端会以 `INCONCLUSIVE` 终止，用户
应使用正确事实创建新 Case。

构造 `state_delta.add_pending_requirements` 时，必须把当前 Skill 对应 requirement 的
`kind/name/prompt/constraints/supplement_policy` 逐字段原样复制到 PendingRequirement；
每一项都必须显式写出 `supplement_policy`，禁止省略后依赖 Schema 的 `NONE` 默认值，也
禁止把 Skill 声明的 `MISSING_ONLY` 改成 `NONE`。服务端会逐字段复验，不一致即拒绝整个
Outcome。

有 `resolved_logparse_plan` 时，所有 Logparse 请求的附件、`problem_time` 和有序 anchors
必须与它逐字段完全相同。不得改写时间、删改 anchor 或另选附件来寻找更像结论的日志。
`missing`、`ambiguous` 或远离 Skill 时间窗口的目标不能作为事故证据。

每个带 `resolved_logparse_plan` 的当前 DIAGNOSE Job 都必须通过 broker 重新建立权威目标
边界：首次附件解析只调用一次 `parse-targets`；续跑并复用已有 `LOGPARSE_RUN` 时只调用
一次 `target-logs`。即使当前快照已经含有旧 Evidence、locator、excerpt 或日志路径，也
禁止跳过本 Job 的 `target-logs` 后直接读取、引用或形成 Candidate。封存 draft 前必须确认
本 Job 的 broker audit 恰好含有对应的一次成功操作；空 audit、重复成功操作或错误操作
类型都会使整个 Outcome 无效。

调用 `parse-targets` 时先选定唯一 proposal key `K`，然后必须在所有位置逐字复用同一个
`K`：request path=`output/proposals/K/request.json`、result path=
`output/proposals/K/target_logs.json`、request 的 `artifact_proposal_key=K`，以及成功返回的
`logparse_run_artifact_draft.proposal_key=K`。不得把目录命名为 `parse-1` 却把
`artifact_proposal_key` 命名为 `parse-1-run`；broker 会在运行 Logparse 前拒绝这种请求。

首次解析产生的 `LOGPARSE_RUN` Artifact metadata 必须保持严格字段集：
`tree_manifest_sha256`、`logparse_version_ref`、
`parse_manifest_relative_path`、`source_attachment_id`、
`source_attachment_sha256` 和只含 `product` 的 `parse_parameters`。Agent 必须逐字采用
broker 返回的 `logparse_run_artifact_draft`，不得自行重建 metadata；metadata 禁止增加
`schema_version`、`format_id` 或 `description`。Artifact 的 content type 固定为
`application/vnd.problem-locator.logparse-run+directory`，`declared_size` 和
`declared_sha256` 都必须为 `null`。`tree_manifest_sha256` 是整个受控目录树的哈希，
not the hash of `parse_manifest.json`。LOGPARSE Evidence 不复制日志文件：
`workspace_relative_path` 必须为 `null`，真实相对路径只写在 `locator.relative_path`，并通过
`source_binding.artifact_proposal_key` 或 `existing_source_ref` 绑定对应运行。

若首次解析后需要跨 Job 补充 Skill 声明的缺失参数，必须同时把需要保留的 Evidence
binding 写入 `state_delta.add_evidence_bindings`，其中新 Evidence 使用
`evidence_proposal_key`；proposal 本身不会自动驱动接收。
只有 Skill 明确声明为 `AFTER_LOGPARSE` 且仍然缺失的 `MISSING_ONLY` requirement
才能在解析后请求；不得把已有事实伪装成这一阶段的缺参。

## 逐规则诊断

非失败 DIAGNOSE draft 必须按 Skill `verification_contract.rules` 的声明顺序输出恰好一条
`rule_claims`。每条 claim 必须列出实际使用的 user-fact item ID；凡规则声明日志事件，
必须引用已读取的原始 Evidence binding 与 inclusive 行号。无日志 Skill 中
`evidence_events` 为空的 `SEMANTIC_CAUSALITY` 不得伪造行号 citation，但 Candidate 仍必须
把实际依赖的非日志 Evidence 纳入 supporting/completion bindings 和
`consumed_evidence_refs`。Evidence summary、Finding、文件名、Logparse target 状态和
先前 Outcome 都只是待验证陈述。

逐条核对 Skill 定义的事件基数、普通时间窗口、事实字段、必需角色、跨角色关联和事件
顺序。语义因果规则必须由完整 Evidence 链支持；有日志事件时引用原始行，无日志规则则
核对 Candidate 实际绑定的结构化或用户 Evidence。“时间接近”或“故事听起来合理”不能
代替因果证据。任何必选事实缺失、字段/时间不符、角色/关联缺失、顺序错误、所需 Evidence
未读、证据冲突或因果仍不确定时，禁止提出 Candidate。

本版本不实现日志抑制、限流或采样窗口扩展；不得自行推导此类容差。后续由 Skill 合同
显式声明后再扩展服务端验证。

`fact_refs` 必须严格服从 Skill 的规则种类，因为服务端会把它与独立推导的输入逐项比较：
`FACT_FIELD_EQUALS` 只列出唯一匹配的 user-fact item ID；`EVENT_TIME_WINDOW` 只列出
它的 `USER_FACT` reference item ID（`SKILL_FIXED` 时为空）；every other rule kind，
包括 `EVENT_PRESENT`、`ROLE_COVERAGE`、`CROSS_ROLE_CORRELATION`、`EVENT_ORDER` 和
`SEMANTIC_CAUSALITY`, has `fact_refs=[]`。语义说明可以引用已由前置规则验证的值，
但不得把这些 fact ID 附加到跨角色、顺序或语义 claim。

每条 `EVENT_TIME_WINDOW` 的下界必须计算为 `problem_time - before_ms`，上界必须计算为
`problem_time + after_ms`；不得交换参数，也不得把 `before_ms` 加到问题时间上。例如
`problem_time=2026-07-31T00:00:03.000Z`、`before_ms=3500`、`after_ms=500` 时，窗口是
`[2026-07-30T23:59:59.500Z, 2026-07-31T00:00:03.500Z]`，因此
`2026-07-31T00:00:00.100Z` 位于窗口内。

## Evidence、Candidate 与服务端用户结果

Evidence 只能来自当前 Job 固定输入或同一 draft 的合法 proposal。新 LOGPARSE Evidence
必须绑定 broker 返回的同一 `LOGPARSE_RUN`。Candidate supporting bindings 与每个
completion mapping 必须覆盖全部所需 Evidence，保持当前快照和新增 binding 的稳定顺序，
并同时列入 `consumed_evidence_refs`。正式 `evidence_refs` 必须保持当前 Job Evidence 的
固定子序列；禁止按业务角色、日志时间或叙述顺序重新排序。

Agent 禁止提出或写入 `USER_RESULT`、`USER_RESULT_ARCHIVE`、`diagnosis-result.json`、
`result.zip` 或任何归档请求，也禁止自行调用 zip/tar。Agent draft 只提交 Candidate、
Evidence、rule claims 与合同允许的内部 Artifact proposal。Agent 进程退出后，服务端重读
权威证据并执行机器验证；DIAGNOSE 草稿通过服务端验证后，服务端立即从已验证的权威结果
生成并持久化用户产物，仅在独立 Review PASS 后开放公开下载。Agent 不得预先构造、摘要
或替代这些服务端产物。

`state_delta.add_user_facts` 与 `state_delta.fulfill_requirements` 由应用层拥有，Agent 保持
为空。缺参、附件、COMPLETED/REROUTE 的 payload 组合及所有 proposal 形状以嵌入 Schema
为准。

## 草稿封存与服务端终结

完成 proposal 和 `output/job_outcome.draft.json` 后，最后一个修改 Workspace 的命令
必须恰好为：

```text
problem-locator-seal-outcome-draft
```

sealer 只校验和 Canonical-JSON 规范化 Agent draft，并记录 draft 的 size/SHA-256；它不生成
Outcome ID、时间、验证结论或公开用户产物。成功后不得继续修改 `output/`。
Agent 进程退出后，服务端重新读取原始证据、重算机械规则并生成唯一权威的
`output/job_outcome.json`、`decision_audit.json` 和可观察的证据行记录。stdout、stderr、
隐藏思维过程或半成品都不是业务输出。
