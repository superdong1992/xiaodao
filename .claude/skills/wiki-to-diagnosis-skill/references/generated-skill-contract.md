# 生成出的 Diagnosis Skill 2.0.0 契约

本文件约束生成器产品，不创建第二套运行时 DTO。字段、枚举、错误和 JSON
验证始终以当前 S00 Python 合同及 `schemas/v1/*.schema.json` 为准。

## 产品目录

生成路径为 `.claude/skills/diagnose-<capability>/`，产品文件恰好为：

```text
SKILL.md
diagnosis-skill.json
```

不得生成 `agents/openai.yaml`、README、changelog、缓存、时间戳或来源标记。
同一规范化 Wiki 和相同参数必须生成相同字节。目录产品 hash 使用 S04 的
Canonical tree manifest 算法。已有相同 `{id,version}` 时，只有产品 hash 完全
相同才是幂等成功；不同产品必须拒绝覆盖。语义变化先提升 version。

## diagnosis-skill.json

manifest 是带末尾 LF 的 S00 Canonical JSON，字段必须恰好为：

```json
{
  "schema_version": 1,
  "id": "diagnose-<capability>",
  "version": "2.0.0",
  "capability": "<稳定 lower-kebab 标识>",
  "summary": "<非敏感 Router 摘要>",
  "entry_document": "SKILL.md",
  "tool_bundle_id": "tool-bundle/diagnose",
  "requires_logparse": true,
  "logparse_product": "<固定 product>"
}
```

无日志产品必须令 `requires_logparse=false` 且 `logparse_product=null`。有日志
产品必须逐字固定非空 product。Router 只能看到 `summary`，所以摘要不得包含 Wiki
敏感细节、客户信息、路径、凭据或日志内容。

## SKILL.md frontmatter

frontmatter 只包含 `name` 和 `description`：

```yaml
---
name: diagnose-link-timeout
description: 用于定位 RPC 链路超时；在 Problem Locator DIAGNOSE Job 中遵守 S00 输出合同。
---
```

目录名、frontmatter name 和 manifest id 必须完全相同。版本只由 manifest
表达，避免在两个产品字段中漂移。

## 固定输入

生成 Skill 只消费本 Job 固定的：

- `JOB_INSTRUCTION`
- `CONTEXT_SNAPSHOT`
- `OPEN_REQUIREMENTS`
- `PREVIOUS_OUTCOME`
- `RESOURCE_MANIFEST`
- 只读 `inputs/manifest.json`

不得读取执行时最新 Case、Repository、旧 Session 隐式状态或扫描 `inputs/`。
每个 target anchor 固定字段为 `label,module,slot,process_name,pid`，其中 module
来自 Wiki 产品，pid 可以为 null，其他运行值来自当前 Job 已验证事实。

## S07 固定 requirements

RPC 超时主场景使用以下稳定 requirement name：

- 参数组 A：`caller_service`、`server_service`、`rpc_method`、`problem_time`
- 唯一日志：`log_archive`
- 参数 B：`order_id`

`problem_time` 是毫秒精度 UTC RFC 3339 单值。缺失参数时只为缺失名称提出
当前 S00 `INPUT` requirement；参数组 A 满足而没有日志时提出一个当前 S00
`ATTACHMENT` requirement。已经存在的 OPEN requirement 必须复用其 ID，不得重复
创建。Constraint 字段完全采用当前 S00 合同；生成器和 Skill 不增加私有字段。

日志 allow-list 的每一项必须先通过 S00 Canonical ContentType grammar：

```text
^[a-z0-9][a-z0-9!#$&^_.+-]{0,62}/[a-z0-9][a-z0-9!#$&^_.+-]{0,62}$
```

值必须是 3～127 个 ASCII 字符、逐字唯一且保持工具声明顺序。禁止大写、
参数、引号、通配符、空白、控制字符、CR/LF、非 ASCII 或自动规范化。

## 四种 DIAGNOSE result type

生成 Skill 只返回当前 S00 允许的四种业务结果：

- `NEED_INPUT`
- `NEED_ATTACHMENT`
- `REROUTE`
- `COMPLETED`

业务性缺参不是 `FAILED`。每次都生成完整 S00 `AgentJobOutcome`，不省略 null、
空数组或空对象，不增加字段。Outcome 退出前原子发布到
`output/job_outcome.json`；stdout/stderr 只含安全摘要，不是业务结果回退源。

## DiagnosisStateDelta

Agent 只能提出当前 S00 `DiagnosisStateDelta`：

- `add_user_facts=[]` 和 `fulfill_requirements=[]`；二者属于应用服务。
- Agent 推断写入 `proposed_facts`，不能直接确认为事实。
- 新假设、问题、requirements 与 Evidence binding 使用 S00 对应分支。
- 新 item 的 provenance 绑定当前 Outcome；已存在条目按固定 ID 更新。
- 未使用字段写空数组或 null。

中间 Job 结束前，所有后续 Job 必需状态都必须进入 StateDelta、Evidence、
Attachment、Artifact 或 previous Outcome。不得把旧 Session 或临时 Workspace 当作
跨 Job状态。

## broker-only logparse

调用链只能是：

```text
diagnose-*
  -> logparse-diagnose
  -> problem-locator-logparse broker client
  -> job-scoped broker
  -> fixed logparse CLI
```

生成 Skill 不读取 raw `LOGPARSE_REPO`、`LOGPARSE_CONFIG_PATH`、
`LOGPARSE_PYTHON`，不直接启动 `cli.py`，不打开/枚举/解包/扫描原始归档，不用
grep/rg 替代 logparse，不接受任意 argv，也不把 endpoint/token 写入文件或日志。

`parse-targets` request 只使用 S07 固定字段；它不含 product。product 只来自只读
WorkspaceInputManifest。首次日志 Job 最多调用一次 `parse-targets`。若 manifest 已
包含 `LOGPARSE_RUN`，必须直接拒绝 parse 并使用 `target-logs`。

## 首次解析与 LOGPARSE_RUN

首次日志 Job 必须在同一 Outcome 中提出：

1. `LOGPARSE_RUN` Artifact Draft，proposal key 固定或稳定且 Job 内唯一；
2. 以该 Artifact proposal key 为 source binding 的 `LOGPARSE` Evidence Draft；
3. 需要参数 B 时的中间 StateDelta 和 OPEN `order_id` requirement；
4. `NEED_INPUT` 业务结果。

LOGPARSE_RUN 使用 S00 固定 kind、directory ContentType、resource kind 和
`LogparseRunMetadata`。metadata 的 parse parameters 只有固定 product。Evidence
locator 只保存受控 output root 内安全相对 POSIX 路径，不能保存绝对路径。

补参后的新 Job 从 manifest 验证并只读使用
`inputs/artifacts/<artifact_id>/tree`，调用 `target-logs`，绝不再次 parse。完整
连续性来自固定 StateDelta、Evidence、源 Attachment、LOGPARSE_RUN 和
PREVIOUS_OUTCOME。

## Candidate 与唯一 USER_RESULT

只有每项完成条件都满足且有 Evidence binding 时才能提出
`CandidateConclusionDraft`。mapping 按当前 ProblemSpec completion criteria 顺序
完整覆盖，criterion 逐字回显，全部 `satisfied=true` 且 binding 非空。

含 Candidate 的同一 AgentJobOutcome 必须恰好有一个 USER_RESULT Draft：

```text
proposal_key = user-result
artifact_kind = USER_RESULT
name = diagnosis-result.json
content_type = application/json
resource_kind = FILE
workspace_relative_path = output/proposals/user-result/payload
metadata = {schema_version:1, format_id:problem-locator-diagnosis-v1,
            description:Diagnosis result}
```

没有 Candidate 时禁止 USER_RESULT。payload 必须逐字通过 S00
`UserResultPayload` / `user-result.schema.json`，并使用 Canonical JSON：

```text
schema_version=1
format_id=problem-locator-diagnosis-v1
problem_statement=<固定 ProblemSpec.statement>
candidate_statement=<同一 Candidate.statement>
supporting_evidence_bindings=<同一 Candidate bindings>
completion_criteria_mapping=<同一 Candidate 完整 mapping>
```

payload 不写时间、Workspace 路径、正式 ID 猜测、endpoint、token 或 raw 配置。
Candidate 仍需独立 REVIEW PASS，Skill 不自行设置 final result 或 Case RESOLVED。

## 原子输出与脱敏

先在 `output/` 内写同目录临时文件，flush、同步后再原子替换
`output/job_outcome.json`。所有文件、Outcome、proposal 和日志都禁止出现：

- broker endpoint/token
- raw logparse 路径或环境变量值
-绝对路径
- 原始日志正文（Evidence 指向受控相对位置）
- 敏感 Wiki 内容、真实客户标识、生产凭据或真实订单号

任何不能由当前 S00 合同表达的需求都停止并提交合同变更请求；不得创建本地公开
DTO、错误码或兼容分支。
