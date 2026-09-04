# Wiki 转 Methods Skill 与 Methods V1 定位运行时

状态：已实施；发布结论只认 Test Flow 为当前源码快照生成的密封 `verdict.json`

当前版本：Problem Locator 6.0.0，State / Job / Outcome schema V9，`v9-contract-r1`

## 目标与边界

这条链路把已评审的故障定位 Wiki 转成闭合 Methods package。生成器保留 Wiki
中的方法、正向日志模板、完成条件、限制和安全说明；产品 registration 再绑定路由、
Logparse 计划与内置运行时资产。

系统只依据本次 Case 的冻结日志确认定位方法。日志缺失、无法关联或证据不足时，结果必须
明确写成限制、证据缺口或 `INCONCLUSIVE`，不能让 Agent 补写日志身份、行号、原文或哈希。

V9 是破坏性升级：

- 只接受全新空 `DATA_ROOT`，V1–V8 数据只读保留，不迁移、不改写；
- 旧 Evidence V2 package 不做静默兼容，必须从原 Wiki 重新生成；
- 七个公开 MCP 工具和 REST 路径不变，MCP 输入仍保持根层扁平；
- GENERIC V2 路径保持不变，不读取专用 Methods package。

## 闭合 Methods package

生成目录固定为：

```text
<skill-name>/
|-- SKILL.md
|-- methods.json
`-- references/
    |-- <method-id>.md
    `-- <shared-topic>.md
```

`methods.json@1` 固定原 Wiki SHA-256、用户输入、附件、日志派生字段、共享参考和有序方法索引。
每张方法卡必须包含适用条件、所需证据、计算与判断、确认条件、未知边界和输出含义。
`evidence_markers` 来自 Wiki 的正向日志模板；`activation_markers` 只是决定是否加载该方法，
不代表单条命中已经证明根因。

Agent 不得创建 Candidate、`USER_RESULT`、ZIP 或权威 Outcome。生成 Skill 的
`SKILL.md` 必须明确要求 Specialist 只写 `MethodDiagnosisDraftV1` 的七个字段：
`schema_version`、`status`、`confirmed_methods`、`candidate_methods`、`evidence`、
`limitations` 和 `safety_notes`。

## Specialist 与服务端核验

材料齐备后，服务端先在独立 Workspace 调用固定 Logparse 计划，选择权威目标日志。Broker
能力随预处理结束撤销。随后服务端把以下内容复制到 Specialist 的只读输入区：

- `request.json`；
- `target_logs.json`；
- `logparse-receipt.json`；
- `target-logs/<source_id>.log`。

Specialist 可读取这些冻结文件和已加载的方法卡。每条 evidence 必须给出方法 ID、摘要、
身份标记，以及精确的 `source_id`、行号、marker 和日志原文。

服务端收到草稿后重新完成全部权威核验：

1. 方法必须来自固定 package，且确实被 marker 激活；
2. `source_id` 必须出现在冻结目标清单；
3. 行号、marker 和原文必须与冻结日志逐字一致；
4. 日志字节、目标清单和 receipt 的大小及 SHA-256 必须一致；
5. evidence 身份、完成条件映射和限制必须能机械映射为 Evidence、
   `CandidateConclusionDraft`、`DecisionAuditV2` 和 `DiagnosisOutcome`。

任一项不成立时，不得发布已解决结果。

## 审核策略

专有 DIAGNOSE Job 创建时冻结：

- `review_policy=NONE`：服务端核验通过后，在同一状态提交中接受 Candidate，并同时公开
  `USER_RESULT` 与 `USER_RESULT_ARCHIVE`；
- `review_policy=INDEPENDENT`：Candidate 进入 `REVIEWING`，报告产物保持内部状态。
  Reviewer 使用独立 Job 和 Workspace，只能核对已冻结的 Candidate、Evidence 和 grounding
  audit。只有 PASS 才同时公开两项产物。

部署开关是 `SPECIALIZED_REVIEWER_ENABLED=false|true`，默认关闭。旧变量
`EVIDENCE_V2_REVIEWER_ENABLED` 会触发配置错误。Job 中的策略是恢复和重放依据，服务重启后
不受当前环境变量变化影响。

Reviewer 返回 `REJECT` 或 `NEED_MORE_EVIDENCE` 时，原 Candidate 及其 JSON/ZIP 永远
不可下载。服务端另行生成 `INCONCLUSIVE` 的 `diagnosis-result.json` 和
`AUDIT_BUNDLE`，Case 收口为 `UNRESOLVED`。

## 用户结果

`diagnosis-result.json` 固定使用 `problem-locator-diagnosis-v3`，包含：

- 具体 `root_cause` 和 findings；
- 因素与完成条件；
- 服务端验证结果和时间相关性；
- 证据缺口、限制、处置建议和安全说明。

已解决结果的 `result.zip` 固定包含：

1. `result.txt`；
2. `archive-manifest.json`；
3. 按权威 Logparse plan 顺序排列的全部可交付目标日志。

`result.txt` 固定为九节中文：定位结论、问题描述、分析依据、完成条件、服务端验证、
时间相关性、证据缺口、处置建议、目标日志清单。ZIP 中日志字节必须等于冻结来源字节。

`CaseView.methods_result` 仅保留为旧字段占位，V9 专有 Case 必须为空。网站和客户端只使用
`final_result`、`unresolved_result` 及现有 artifact list/download 接口。

## 原子性与重放

报告暂存、Artifact 正式化和 Case 终态使用同一提交边界。物理文件可以先写入，但提交完成前
不得出现在 CaseView、artifact 列表或下载入口。发布失败不能提交 `RESOLVED`。

同一 finalized Outcome 重放时，系统必须复用相同 Artifact ID、大小和 SHA-256。失败、
取消或中断只返回对应状态和 failure，不伪造用户报告。

## 验证要求

确定性测试必须覆盖 Reviewer 关闭直出、Reviewer 开启时的隐藏与 PASS 发布、非 PASS 的
INCONCLUSIVE、PARTIAL、失败、取消、中断、发布中断和重启重放；还要逐字节核对 JSON、
九节 `result.txt`、manifest、ZIP 顺序、日志内容、Content-Length 和 SHA-256。

正式结论只能来自 `tools/test-flow/run.sh` 或 `run.ps1`。Release 必须先查看
`--plan-only`，再从全新 V9 数据根执行 Reviewer 开启的最长链路。
