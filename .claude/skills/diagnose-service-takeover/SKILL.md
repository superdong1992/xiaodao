---
name: "diagnose-service-takeover"
description: "用于定位合成服务接管场景中的 RPC 超时；在 Problem Locator DIAGNOSE Job 中遵守 S00 AgentJobOutcome，缺参或缺日志时正常结束本 Job，日志仅经 logparse-diagnose broker 分析，候选结论等待独立复核。"
---

# 服务接管 RPC 超时定位

本产品由 `wiki-to-diagnosis-skill` 生成器 `2.0.0` 生成。只消费当前
Problem Locator Job 的固定输入；S00 冻结 DTO、Schema、枚举和错误码是唯一机器合同。
禁止增加私有结果字段、私有错误码或直接修改 Case。Candidate 不是最终结果，必须等待
独立 REVIEW Job 的 `PASS`。

## 产品固定信息

- capability：`service-takeover`
- module：`COMPACT`
- logparse product：`compact`
- generator version：`2.0.0`

允许日志 Content-Type（逐字匹配 S00 Canonical ContentType，不做大小写或参数归一化）：

- `application/gzip`
- `application/zip`
- `application/x-tar`

## 问题范围

定位合成的调用方到服务端 RPC deadline exceeded：区分调用方 3 秒 deadline、服务端
连接池等待和处理延迟。没有同一合成请求的两端机器证据时，不确认服务端根因。

## 运行时输入

只读取 Runtime 提供的 `JOB_INSTRUCTION`、`CONTEXT_SNAPSHOT`、`OPEN_REQUIREMENTS`、
`PREVIOUS_OUTCOME`、`RESOURCE_MANIFEST` 与只读 `inputs/manifest.json`。不得扫描
`inputs/`、读取 Repository、沿用旧 Session 隐式状态或采用 Job 创建后的输入。

目标角色顺序固定为：

| 标签 | 说明 | 是否必需 |
| --- | --- | --- |
| client | 发起合成 RPC 的客户端进程 | 是 |
| server | 接收并处理合成 RPC 的服务端进程 | 是 |

每个 broker anchor 只含 `label`、`module`、`slot`、`process_name`、`pid`；其中
`module` 固定为 `COMPACT`，`pid` 可以为 null，其余值必须来自本 Job 已验证事实。

## 自定义定位参数

| 参数名 | 说明 | 是否必需 |
| --- | --- | --- |
| order_id | 合成请求的唯一关联标识 | 是 |

参数组 A 的 requirement name 固定为 `caller_service`、`server_service`、`rpc_method`、
`problem_time`。`problem_time` 必须是毫秒精度 UTC RFC 3339 单值，必须匹配
`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$`；不得接受范围、猜测时区或取中点。参数 B 固定为 `order_id`。

## 四种业务结果

始终按 S00 `agent-job-outcome.schema.json` 生成完整 `AgentJobOutcome`，并在退出前原子
发布为 `output/job_outcome.json`。只使用以下 DIAGNOSE result type：

- `NEED_INPUT`：缺少参数组 A，或首次解析后的机器证据仍缺 `order_id`。
- `NEED_ATTACHMENT`：参数组 A 已满足，但尚无本 Job 固定的唯一日志 Attachment。
- `REROUTE`：问题不属于本 capability；不调用 Router，也不选择另一个 Skill。
- `COMPLETED`：当前完成条件均有 Evidence binding，可提出 Candidate；不声称 Case 已
  `RESOLVED`。

业务性缺参不是执行失败。`DiagnosisStateDelta`、requirement、Evidence/Artifact Draft、
Candidate 和 error 字段全部逐字使用当前 S00 合同；未使用的集合写空数组、无值写 null。
`add_user_facts` 与 `fulfill_requirements` 由应用服务拥有，Agent 必须写空数组。新事实只写
`proposed_facts`，并通过 `add_evidence_bindings` 提案引用 Evidence。

## 参数组 A 与一次日志

先复用 `CONTEXT_SNAPSHOT` 中已有且仍有效的事实和 OPEN requirement。缺少参数时返回
`NEED_INPUT`，只为缺失名称提出当前 S00 定义的 INPUT requirement；已经存在的
requirement 必须复用原 `requirement_id`，不得重复创建。

参数组 A 齐全但没有可用日志时返回 `NEED_ATTACHMENT`。日志 requirement 的 name 固定
为 `log_archive`，只接受一个 Attachment，允许 Content-Type 只能来自上面的固定列表。
上传本身不推进 Case；后续 Job 只能消费 `inputs/manifest.json` 中固定的 READY Attachment。

## 先调用 logparse-diagnose Skill

加载 `logparse-diagnose`，且只调用随服务安装的 `problem-locator-logparse` broker 客户端。
禁止读取 `LOGPARSE_REPO`、`LOGPARSE_CONFIG_PATH`、`LOGPARSE_PYTHON`，禁止直接启动
`cli.py`，禁止打开、枚举、解包或扫描原始归档，也禁止用 grep/rg 代替 logparse。

首次日志 Job 在 manifest 不含 `LOGPARSE_RUN` 时：

1. 用 Canonical JSON 写 `output/proposals/logparse-run/request.json`；request 只含 S07
   `parse-targets` 字段，禁止携带 `logparse_product` 或任意 argv。
2. 仅调用一次 `problem-locator-logparse parse-targets --request ... --result ...`。
3. 读取 broker 生成的 `target_logs.json` 与受控 `parse_manifest.json` 机器结果。
4. 提出 proposal key=`logparse-run` 的 `LOGPARSE_RUN` 目录 Artifact Draft，以及用同一
   artifact proposal key 作为 source binding 的 `LOGPARSE` Evidence Draft。
5. 若仍缺 `order_id`，在同一 `NEED_INPUT` Outcome 中提交中间 StateDelta、Evidence、
   LOGPARSE_RUN 与新 OPEN INPUT requirement；正常结束 Job。

## LOGPARSE_RUN 复用

只要 `inputs/manifest.json` 已含任一 `artifact_kind=LOGPARSE_RUN`，严禁调用
`parse-targets`。验证 manifest 固定的 Artifact kind、目录 hash、parse manifest 相对路径、
源 Attachment、`logparse_tool_ref` 与 product 后，使用其只读
`inputs/artifacts/<artifact_id>/tree` 根调用 `problem-locator-logparse target-logs`。
request 只含 S07 `target-logs` 字段且 `artifact_id` 必须来自 manifest。不得修改物化目录，
不得再次 parse；新 Job 的连续性只来自固定 StateDelta、Evidence、Attachment、
`LOGPARSE_RUN` 与 `PREVIOUS_OUTCOME`。

## Evidence 与 Candidate

只把 `target_logs` 返回并解析到受控 output root 内的安全相对 POSIX `log_path` 写入 S00
`LogparseEvidenceLocator.relative_path`。没有匹配、路径歧义、时间无法关联或证据不足时
必须明确保留缺口，不得把假设升级为事实。

形成 Candidate 时，supporting Evidence bindings 和每个 completion criterion mapping
必须完整、按 ProblemSpec 顺序、全部 satisfied 且非空。Candidate 所在 Outcome 必须恰好
同时提出一个 USER_RESULT Draft：

- proposal key：`user-result`
- kind/name/content type/resource kind：`USER_RESULT` / `diagnosis-result.json` /
  `application/json` / `FILE`
- path：`output/proposals/user-result/payload`
- metadata：`{"schema_version":1,"format_id":"problem-locator-diagnosis-v1","description":"Diagnosis result"}`

payload 只用 S00 `UserResultPayload`：`problem_statement` 逐字等于 Job 固定 ProblemSpec，
`candidate_statement`、`supporting_evidence_bindings`、`completion_criteria_mapping` 逐字等于
同一 Candidate Draft。使用 S00 Canonical JSON（UTF-8、排序、紧凑、末尾一个 LF）；禁止
写入时间、正式 ID 猜测、Workspace 路径、endpoint、token 或 raw logparse 配置。

## 时间特征

- 调用方 deadline exceeded 必须位于 problem_time 附近。
- 服务端较晚记录只有在同一 order_id 或连续因果链下才能关联。
- 3 秒 deadline 只证明调用方等待边界，不单独证明服务端处理耗时或根因。

## Wiki 定位步骤

1. 在 client target log 中定位 problem_time 附近的 deadline exceeded，并记录 RPC 方法。
2. 首次日志 Job 通过 broker 完成唯一一次 parse，保存 LOGPARSE_RUN 和客户端 Evidence。
3. 若缺少唯一关联值，提出 order_id requirement 并以 NEED_INPUT 正常结束当前 Job。
4. 新 Job 只读复用 LOGPARSE_RUN，通过 target-logs 围绕 order_id 定位 server Evidence。
5. 比较客户端 deadline、服务端连接池等待和处理时间，形成带证据的候选结论。

## 判断规则

- 同一 order_id、RPC 方法和时间因果链同时成立，才能关联两端记录。
- 仅有客户端 timeout 文案时，只确认调用方 deadline exceeded，不确认服务端根因。
- 服务端连接池等待或处理延迟必须有对应 target log Evidence。
- target_logs 缺失、歧义、路径越界或时间无法关联时，明确报告证据不足。
- 不补充 Wiki 外的经验性根因或排查方向。

## 输出要求

- Candidate statement 区分已确认现象与仍待验证假设。
- 每个 completion criterion mapping 都逐字回显条件并引用 Evidence binding。
- Candidate 与唯一 `diagnosis-result.json` USER_RESULT 位于同一 Outcome。
- 最终结果等待独立 REVIEW PASS；本 Skill 不自行宣称 Case RESOLVED。

## 假设

- 只使用合成服务名、合成订单号和非敏感日志。

## 原子交付

先写同目录临时文件、flush 并同步，再原子替换 `output/job_outcome.json`；成功退出后 stdout
和 stderr 只给安全摘要，不能作为业务结果回退。任何 endpoint/token、绝对路径、环境值、
原始日志正文或敏感 Wiki 内容都不得进入 Outcome、proposal、USER_RESULT 或日志。
