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

<<<BEGIN S00 USER RESULT SCHEMA>>>
{{S00_USER_RESULT_SCHEMA_JSON}}
<<<END S00 USER RESULT SCHEMA>>>

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

有 `resolved_logparse_plan` 时，所有 Logparse 请求的附件、`problem_time` 和有序 anchors
必须与它逐字段完全相同。不得改写时间、删改 anchor 或另选附件来寻找更像结论的日志。
`missing`、`ambiguous` 或远离 Skill 时间窗口的目标不能作为事故证据。

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

## Evidence、Candidate 与用户结果

Evidence 只能来自当前 Job 固定输入或同一 draft 的合法 proposal。新 LOGPARSE Evidence
必须绑定 broker 返回的同一 `LOGPARSE_RUN`。Candidate supporting bindings 与每个
completion mapping 必须覆盖全部所需 Evidence，保持当前快照和新增 binding 的稳定顺序，
并同时列入 `consumed_evidence_refs`。正式 `evidence_refs` 必须保持当前 Job Evidence 的
固定子序列；禁止按业务角色、日志时间或叙述顺序重新排序。

形成 Candidate 时必须恰好提出一个 `USER_RESULT` FILE；若 Skill 要求归档，还要恰好提出
一个由 `problem-locator-pack-result` 生成的 `USER_RESULT_ARCHIVE`。USER_RESULT 必须符合
上方 Schema，并与 Candidate statement、ProblemSpec、supporting bindings 和 completion
mappings 逐字一致。被 Review 拒绝或服务端验证失败的结果不会成为可下载用户结果。

`state_delta.add_user_facts` 与 `state_delta.fulfill_requirements` 由应用层拥有，Agent 保持
为空。缺参、附件、COMPLETED/REROUTE 的 payload 组合及所有 proposal 形状以嵌入 Schema
为准。

## 草稿封存与服务端终结

完成 proposal 和 `output/job_outcome.draft.json` 后，最后一个修改 Workspace 的命令
必须恰好为：

```text
problem-locator-seal-outcome-draft
```

sealer 只校验和 Canonical-JSON 规范化 Agent draft/USER_RESULT，并记录 draft 的
size/SHA-256；它不生成 Outcome ID、时间或验证结论。成功后不得继续修改 `output/`。
Agent 进程退出后，服务端重新读取原始证据、重算机械规则并生成唯一权威的
`output/job_outcome.json`、`decision_audit.json` 和可观察的证据行记录。stdout、stderr、
隐藏思维过程或半成品都不是业务输出。
