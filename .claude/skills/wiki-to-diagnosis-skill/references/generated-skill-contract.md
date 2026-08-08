# Generated Diagnosis Skill v3 contract

生成产品恰好包含 `SKILL.md` 和 Canonical `diagnosis-skill.json`。

manifest schema v3 必填字段：

```text
schema_version=3
id, version>=3.0.0, capability, summary
entry_document=SKILL.md
tool_bundle_id=tool-bundle/diagnose
requires_logparse
requirements[]
logparse_plan
verification_contract
```

`logparse_product` 是唯一可选字段：省略表示有效值 `default`；仅非默认产品出现。

RequirementSpec 字段恰好为：

```text
name
kind = INPUT | ATTACHMENT
stage = INITIAL | AFTER_LOGPARSE
fulfillment_source = USER_FACT | READY_ATTACHMENT
prompt
constraints = S00 InputRequirementConstraints | AttachmentRequirementConstraints
supplement_policy = NONE | MISSING_ONLY
```

INPUT 只能配 USER_FACT；ATTACHMENT 只能配 READY_ATTACHMENT。所有项天然 required，
manifest 禁止 `required` 字段。每阶段最多一个 ATTACHMENT，AFTER_LOGPARSE 只允许 INPUT。
作者侧 GenerationSpec 中，普通 ATTACHMENT 必须声明完整
`allowed_content_types/min_count/max_count`；被 `logparse_plan.attachment_requirement` 引用的
Logparse 附件只声明 `min_count/max_count`。生成器规范化时自动补入平台固定的
`application/gzip`、`application/zip`、`application/x-tar`，因此最终 manifest 仍是完整的
S00 AttachmentRequirementConstraints。Content-Type 不是生成时的用户输入。

`logparse_plan` 为 null 或对象：

```json
{
  "attachment_requirement": "archive_name_or_null",
  "problem_time_binding": {"source": "USER_FACT", "name": "incident_time"},
  "anchors": [
    {
      "label": "database",
      "module": {"source": "SKILL_FIXED", "value": "database"},
      "slot": {"source": "USER_FACT", "name": "database_instance"},
      "process_name": {"source": "USER_FACT", "name": "database_process"},
      "pid": null
    }
  ]
}
```

每个 ValueBinding 只能是 `USER_FACT{name}` 或 `SKILL_FIXED{value}`；USER_FACT name 必须
引用 INPUT requirement。`requires_logparse=false` 强制 plan=null、无 AFTER_LOGPARSE、
且省略 product。`requires_logparse=true` 要求显式 plan，但不会自动产生任何字段。

`verification_contract` 字段恰好为 `schema_version=1`、`event_extractors[]` 和
`rules[]`。Logparse Skill 至少声明一个 extractor；无 Logparse Skill 必须使用空数组。
Runtime 以它为服务端验证输入；Agent 的自然语言解释不是机器判定结果。
extractor 恰好声明：

```text
id, anchor, line_pattern
timestamp_group, timestamp_format=RFC3339_MILLIS_UTC
field_groups[], match_cardinality=EXACTLY_ONE
```

`line_pattern` 是 `^...$` UTF-8 单行 Python 正则，命名捕获组集合必须恰好等于时间组和
field_groups；缺失或多次匹配都不能通过。规则恰好声明
`id/kind/description/depends_on/remediation_requirements/parameters`，dependency 只能引用前置
rule。`remediation_requirements` 只能引用 `MISSING_ONLY` requirement，不能替换已有事实。

生成的 `SKILL.md` 使用 V2 两阶段交付：Agent 写
`output/job_outcome.draft.json` 并调用 `problem-locator-seal-outcome-draft`；只有 Agent
进程退出后的服务端验证器可以生成权威 `output/job_outcome.json` 与 decision audit。
kind 固定为 `EVENT_PRESENT | EVENT_TIME_WINDOW | FACT_FIELD_EQUALS | ROLE_COVERAGE |
CROSS_ROLE_CORRELATION | EVENT_ORDER | SEMANTIC_CAUSALITY`。普通时间窗必须显式声明
`before_ms/after_ms/lower_bound/upper_bound`，无默认窗口。本版本不支持 suppression、
rate-limit 或日志采样语义。

LOGPARSE Evidence 不复制或重声明目标日志文件：`workspace_relative_path` 固定为 null，
`locator.relative_path` 相对于绑定的 LOGPARSE_RUN tree root；新运行使用
`artifact_proposal_key`，已有运行使用正式 Artifact ID。任何非 null proposal path 都
只能位于 `output/proposals/<该 proposal_key>/` 下。
Broker anchor 的 `label/module/slot/process_name` 始终为 JSON string，必须逐字复制解析后
binding；禁止把数字样式字符串转换为 JSON number。
新 `LOGPARSE_RUN.metadata` 恰好包含 `tree_manifest_sha256`、`logparse_version_ref`、
`parse_manifest_relative_path`、`source_attachment_id`、
`source_attachment_sha256`、`parse_parameters` 六个字段，且 `parse_parameters` 仅含
有效 `product`；禁止添加 `schema_version`、`format_id`、`description` 等通用字段。
Artifact draft 外壳固定为 `artifact_kind=LOGPARSE_RUN`、
`content_type=application/vnd.problem-locator.logparse-run+directory`、
`resource_kind=DIRECTORY`，且 `declared_size`、`declared_sha256` 均为 null。
`parse-targets` 成功结果携带该 `logparse_run_artifact_draft`；Agent 必须逐字段原样复制，
不得自行构造、扩展版本字符串或修改任何值。`target-logs` 复用结果不携带新 draft。
若 parse 后因 AFTER_LOGPARSE requirement 缺失而返回 NEED_INPUT，每个需要跨 Job 保留的
LOGPARSE Evidence proposal 必须同时出现在 `state_delta.add_evidence_bindings` 中，binding
使用 `existing_evidence_id=null` 和对应 `evidence_proposal_key`；该 Evidence 通过
`artifact_proposal_key` 绑定新 LOGPARSE_RUN，使平台共同接收两者。proposal、Finding 或
prose 本身不驱动接收。续跑只能对正式 LOGPARSE_RUN 调用 `target-logs`，禁止重新 parse。

形成 Candidate 时，生成 Skill 必须同批提出唯一 `USER_RESULT` 和唯一
`USER_RESULT_ARCHIVE`（name 固定为 `result.zip`）。ZIP 为确定性扁平包：`result.txt` 加 Candidate 实际绑定的完整
目标日志，按首次 binding 顺序命名 `target-log-001.log` 等；无日志时只有 result.txt。
生成 `target_log_paths` 时必须先以 broker `target-logs[].log_path` 建映射，再按 Candidate
`supporting_evidence_bindings` 逐条解析 Evidence locator；禁止直接沿用 broker anchor 顺序。
Candidate supporting bindings 必须去重并保持当前快照 `evidence_refs` 的相对顺序；新接收
Evidence 只按 `state_delta.add_evidence_bindings` 顺序追加，禁止按角色、时间或叙述重排。
`result.txt` 字节恰好为 Candidate statement 的 UTF-8 加一个 LF。
禁止原始上传包、无关日志、parse 输出和完整 LOGPARSE_RUN。Review PASS 前两种结果均
不可见、不可下载。
