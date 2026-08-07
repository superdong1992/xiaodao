# Diagnose output contract

本合同只定义所有 DIAGNOSE Job 共用的输出边界。业务所需参数、阶段、附件、
Logparse 字段映射和判定规则只以当前选中 Diagnosis Skill 为准，不得从本合同
推导任何业务字段。

原子替换 `output/job_outcome.json`，内容必须是
`schemas/v1/agent-job-outcome.schema.json` 接受的完整 `AgentJobOutcome`。

Workspace 根目录只允许已有的 `inputs`、`runtime`、`output` 三个直接子目录。
禁止修改根目录权限、扫描未声明输入、读取仓库或环境秘密，也禁止把临时文件、
stdout/stderr 捕获或调试信息写到根目录。临时诊断只能写到系统临时目录，正式
业务产物只能写到 `output/`。

Never create a temporary file at workspace root; its direct children remain
exactly `inputs`, `runtime`, and `output`.

以下是安装时固定的权威 Schema。每对 BEGIN/END 标记之间是一份完整 JSON 文档。

<<<BEGIN S00 AGENT JOB OUTCOME SCHEMA>>>
{{S00_AGENT_JOB_OUTCOME_SCHEMA_JSON}}
<<<END S00 AGENT JOB OUTCOME SCHEMA>>>

<<<BEGIN S00 USER RESULT SCHEMA>>>
{{S00_USER_RESULT_SCHEMA_JSON}}
<<<END S00 USER RESULT SCHEMA>>>

## 顶层与结果类型

顶层必须恰好包含十二个字段：
`base_state_revision`、`case_id`、`consumed_evidence_refs`、`error`、`job_id`、
`job_type`、`outcome_id`、`payload`、`produced_at`、
`proposed_artifact_drafts`、`proposed_evidence_drafts`、`result_type`。
The top-level object has exactly these twelve fields and no others:
`["base_state_revision","case_id","consumed_evidence_refs","error","job_id","job_type","outcome_id","payload","produced_at","proposed_artifact_drafts","proposed_evidence_drafts","result_type"]`.

- 从 `JOB_INSTRUCTION` 逐字复制 `job_id`、`job_type`、`base_state_revision`。
- 从 `RESOURCE_MANIFEST` 逐字复制 `case_id`。
- `outcome_id` 使用新的小写 UUID；`produced_at` 使用当前 UTC 毫秒时间。
- Copy bindings from `JOB_INSTRUCTION` and from `RESOURCE_MANIFEST.case_id`; use a
  fresh lowercase UUID and current real UTC timestamp with exactly millisecond precision,
  and never reuse the Job or Case ID as the Outcome ID.
- 非失败结果必须有 `DiagnosisOutcome` 且 `error=null`；`FAILED` 必须
  `payload=null` 且携带合同允许的 error。
- `NEED_INPUT` 仅填写非空 `requested_input`；`NEED_ATTACHMENT` 仅填写非空
  `requested_attachments`；`COMPLETED` 和 `REROUTE` 两者都为空。
- Agent 必须令 `state_delta.add_user_facts=[]`、
  `state_delta.fulfill_requirements=[]`；用户事实和 requirement fulfillment 由应用层拥有。
  通用字段 `state_delta.add_user_facts` 与 `state_delta.fulfill_requirements` 均由应用层拥有。

## Requirement 通用规则

缺少输入时，只执行当前选中 Skill 的 `requirements` 声明。按 Skill 声明顺序检查
当前阶段：先请求该阶段全部缺失的 `INPUT`，全部齐备后才请求该阶段的
`ATTACHMENT`。不得添加 Skill 未声明的 requirement，不得把工具输出、日志文本、
推测值或 proposed fact 当作用户输入来满足 requirement。

复用同名同 kind 的现有 OPEN requirement ID；否则按 Skill 中的 name、kind、prompt
和 constraints 新建一个 OPEN requirement。新 requirement 必须
`required=true`、`requested_by_job_id` 等于当前 Job ID、`fulfilled_by_refs=[]`。
每个 requested ID 必须对应当前快照中已有或本 Outcome 新增的 OPEN requirement。

`AFTER_LOGPARSE` 只是生成 Skill 的业务阶段；S00 DTO 不新增 stage 字段。若当前
Skill 要求 parse 后补参，必须先提出必要 LOGPARSE Evidence，并把每个需要跨 Job 保留的
Evidence proposal 写入 `state_delta.add_evidence_bindings`：binding 的
`existing_evidence_id=null`，`evidence_proposal_key` 等于对应 proposal key。只把 Evidence
写进 `proposed_evidence_drafts`、`findings` 或 recommended text 不会触发接收。每个新
LOGPARSE Evidence 还必须通过 `artifact_proposal_key` 绑定同一 Outcome 中 broker 返回的
`LOGPARSE_RUN` proposal；平台由该依赖关系共同接收运行产物。完成这些绑定后才返回
`NEED_INPUT`。续跑 Job 必须复用快照中正式 Evidence 和 `LOGPARSE_RUN`，禁止再次
`parse-targets`。Logparse 结果只能形成 Evidence、Finding 或 proposed fact，不能满足
`USER_FACT` requirement。

## Evidence、Artifact 与 Candidate

Evidence 必须来自当前 Job 固定输入或同一 Outcome 的合法 proposal，并使用 Schema
规定的 locator/source binding。不得把假设升级为事实；证据不足时保留缺口。
引用同一 Outcome 新 `LOGPARSE_RUN` 的 LOGPARSE Evidence 必须通过
`artifact_proposal_key` 绑定；引用既有运行时使用其正式 Artifact ID。
LOGPARSE Evidence 的 `workspace_relative_path` 必须为 `null`：日志位置只写入
`locator.relative_path`，并且该路径相对于所绑定 LOGPARSE_RUN 的 tree root。不得把
LOGPARSE_RUN tree 内的日志路径冒充 Evidence 自己的 proposal 文件；任何非 null
`workspace_relative_path` 都必须位于 `output/proposals/<该 proposal_key>/` 下。

新 `LOGPARSE_RUN` 的 metadata 必须严格且仅包含六个字段：
`tree_manifest_sha256`、`logparse_version_ref`、`parse_manifest_relative_path`、
`source_attachment_id`、`source_attachment_sha256`、`parse_parameters`；其中
`parse_parameters` 仅含有效 `product`。不得添加 `schema_version`、`format_id`、
`description` 或其他通用 Artifact 字段。
`tree_manifest_sha256` 是完整规范化 tree manifest 的哈希，not the hash of `parse_manifest.json`。
Artifact draft 外壳也固定为 `artifact_kind=LOGPARSE_RUN`、
`content_type=application/vnd.problem-locator.logparse-run+directory`、
`resource_kind=DIRECTORY`，并将 `declared_size`、`declared_sha256` 均设为 null；
不得猜测 MIME type，亦不得独立计算 broker 受控树的 size/hash。
Agent 不自行构造这些字段：`parse-targets` 成功结果必须携带
`logparse_run_artifact_draft`，Agent 将该对象逐字段原样放入
`proposed_artifact_drafts`，不得扩展版本字符串或修改任何值。

Candidate 必须完成 ProblemSpec 中每个 completion criterion，且每项都有 Evidence
binding。出现 `candidate_conclusion_draft` 时，同一 Outcome 必须恰好提出一个：

Candidate 的 `supporting_evidence_bindings` 必须去重，并严格保持当前快照
`evidence_refs` 的相对顺序；同一 Outcome 新接收的 Evidence 只能按
`state_delta.add_evidence_bindings` 顺序接在既有 Evidence 之后。禁止按业务角色、日志时间
或叙述习惯重排 binding。completion mapping 和 USER_RESULT 中重复使用这些 binding 时也
保持同一顺序。该顺序是 Coordinator 的固定子序列合同，不是展示偏好。

- `USER_RESULT` / `diagnosis-result.json` / `application/json` / `FILE`；
- payload 使用上方 `UserResultPayload` Schema，且 problem、candidate、supporting
  bindings 和 completion mappings 与同一 Candidate seam 逐字一致。

如果当前选中 Skill 要求用户结果归档，还必须恰好提出一个：

- `USER_RESULT_ARCHIVE` / `result.zip` / `application/zip` / `FILE`；
- metadata 的 `user_result_proposal_key` 绑定上述唯一 USER_RESULT proposal；
- ZIP 的 `result.txt` 字节必须恰好为 Candidate statement 的 UTF-8 加一个 LF；
- ZIP 目标日志必须严格遵循 Candidate supporting Evidence binding 顺序，不得用 broker
  anchor 或 `target-logs` 返回数组顺序替代；
- 归档只能通过受控 `problem-locator-pack-result` 工具生成；条目、日志来源和字节
  由 Runtime 再校验。

公共合同允许 Candidate 不带归档以兼容显式未迁移的旧协议，但禁止无 Candidate 的
USER_RESULT 或 USER_RESULT_ARCHIVE，也禁止一个 Outcome 提出多个归档。Candidate、
USER_RESULT 以及存在时的 USER_RESULT_ARCHIVE 必须在同一 TransitionPlan 中共同接受。
它们在独立 REVIEW PASS 前都不可列出或下载。

## Logparse 工具边界

只有当前 Job 同时固定 `logparse_tool_ref` 与有效 `logparse_product`，且选中 Skill
声明 `requires_logparse=true` 时，才可加载 `logparse-diagnose`。业务 Skill 只声明
requirement 到 `problem_time`、anchor 字段的绑定；broker 请求规范、一次 parse、
`LOGPARSE_RUN` 复用和路径安全均以 `logparse-diagnose` 为准。

不得直接调用 Logparse 仓库、解释器或配置。不得把 product 放入 broker request；
有效 product 由 Job 固定，默认值也由 Runtime 记录。Broker 非零退出即结束本 Job，
不得修改请求重试或在 Workspace 中创建调试文件。

## Canonical JSON 与原子发布

`Write` 只能生成语法有效的 JSON draft，不能作为正式发布动作。Logparse 和结果归档
请求由各自安装的服务端工具在消费前校验、递归 Canonical 化并原子回写；
`USER_RESULT` 由 Outcome finalizer 校验和规范化。proposal 内容只能位于其声明的
`output/proposals/<proposal_key>/` 下。

完成全部 proposal 和 `output/job_outcome.json` draft 后，最后一个会修改 Workspace 的
命令必须恰好是：

```text
problem-locator-finalize-outcome
```

该工具刷新 `outcome_id`/`produced_at`，规范化 `USER_RESULT`，重算其声明 size/hash，
验证 `AgentJobOutcome`，递归排序所有嵌套对象键，原子发布 V1 Canonical JSON，并写入
size/SHA-256 finalization marker。非零退出必须修复后重试；命令成功后不得再增删改
`output/`。Runtime 仍会校验当前 Job/Case 绑定、selected Skill 阶段规则、proposal
路径与实际 size/hash，以及 Candidate/结果 Artifact 配对。不能把 prose、stdout、stderr
或半成品当作业务输出。
