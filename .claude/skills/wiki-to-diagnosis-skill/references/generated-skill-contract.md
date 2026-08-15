# Generated Diagnosis Skill v6 contract

生成产品恰好包含 `SKILL.md` 和 Canonical `diagnosis-skill.json`。manifest 必须使用
`schema_version=6`、Skill `version>=6.0.0`，并声明 capability、deployment_scope、summary、
entry document、diagnose tool bundle、内置 input profile 快照及哈希、roles、requirements、
logparse_plan 和 verification_contract。
`logparse_product` 是唯一可选顶层字段，省略表示上游默认。

Requirement 仍是严格的扁平输入声明：INPUT/USER_FACT 或 ATTACHMENT/READY_ATTACHMENT，
INITIAL/AFTER_LOGPARSE，带 prompt、constraints、origin、role、requiredness、activation condition
和 supplement policy。profile 自动注入 `problem_time`、每个 role 的 slot/process_name/pid 与
Logparse archive。REQUIRED 始终激活，OPTIONAL 不主动询问，CONDITIONAL 仅在机器条件成立时
激活；AFTER_LOGPARSE 只允许 INPUT。

`logparse_plan` 使用 USER_FACT/SKILL_FIXED value binding，manifest anchors 按顺序声明
label、module、slot、process_name、pid；后三者由 role label 派生。USER_FACT 必须引用 INPUT
requirement。`requires_logparse=false` 时 plan
为 null、无 extractors、无 AFTER_LOGPARSE requirement。

每个 `logparse_run_artifact_draft` 必须声明
`application/vnd.problem-locator.logparse-run+directory`、`declared_size` 和
`declared_sha256`。它的 `metadata` 严格且仅含 `schema_version`、`format_id`、
`description`、`tree_manifest_sha256`、`logparse_version_ref`、
`parse_manifest_relative_path`、`source_attachment_id`、
`source_attachment_sha256`、`parse_parameters` 这些合同字段；后六项恰好包含
Logparse 运行身份、来源与参数，Agent 不得自行增删或改名。

Agent 禁止提出或写入 `USER_RESULT`、`USER_RESULT_ARCHIVE`、
`diagnosis-result.json` 或 `result.zip`。这些公开产物只能由服务端根据已验证的审计
生成，并在独立 Review PASS 后开放公开下载。

## verification_contract v2

顶层字段恰好是：

```text
schema_version=2
observation_policies[]
event_extractors[]
rules[]
terminal_paths[]
```

Observation policy 首版仅支持 SUPPRESSION 与 RATE_LIMIT，均显式声明 id/kind/scope/key_fields/
window_ms/max_observed/boundary；SUPPRESSION 的 max_observed 为 null。policy 只描述观测损失，
正向事件仍可证明发生，absence/上界可能为 UNKNOWN。

Extractor 以事件集合为单位，声明 id/anchor、有序 members、typed fields、timestamp_field、
group_by、selectors、max_gap_lines、min_matches/max_matches 和 policy IDs。member 的 match_mode
只能为 FULL_LINE 或 SEARCH；所有命名捕获必须恰好等于 fields。INTEGER 必须带单位，TIMESTAMP
必须带 clock domain；timestamp_field 必须是有 clock 的 INTEGER/TIMESTAMP。多行成员只能在同一
受控 source 中按顺序、有界间隔组装。

规则按 DAG 顺序声明，支持 EVENT_COUNT、EVENT_PRESENT、EVENT_TIME_WINDOW、
FACT_FIELD_EQUALS、FACT_IN、FIELDS_EQUAL、ROLE_COVERAGE、CROSS_ROLE_CORRELATION、
EVENT_ORDER、NUMERIC_COMPARE、SEMANTIC_CAUSALITY。数值 AST 为白名单，字段类型/单位/clock
domain 必须兼容。remediation 只能引用 MISSING_ONLY requirement。

terminal path 按顺序使用 `condition.any_of[].all_of[]` 组合 rule 的 PASS/FAIL/UNKNOWN。
COMPLETE/PARTIAL 必须含语义 PASS；最后一条必须是无条件 NONE。Agent 仍提交全部规则 claim，
不能只提交选中分支。

LOGPARSE Evidence 不复制目标日志文件；locator 相对绑定的 LOGPARSE_RUN tree root。Agent 不得
生成 USER_RESULT/ZIP。Runtime 在 Agent 退出后重算 v2 合同并记录每个事件的观测计数/下界、
原始行、派生值、选中路径和完整规则 audit；独立 Review PASS 后才公开结果。

生产 catalog 拒绝 TEST_ONLY，测试 harness 只能通过内部显式开关加载。原 Wiki、澄清、合成
日志和业务 oracle 只在自包含 case root，oracle 不得暴露给转换 Agent、Specialist 或 Reviewer。
