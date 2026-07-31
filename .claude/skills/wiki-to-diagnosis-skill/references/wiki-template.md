# Problem Locator Diagnosis Skill Wiki 模板

只填写非敏感、可进入版本控制的规则。不要写真实客户名、生产凭据、内部 endpoint、
生产日志、真实订单号或绝对路径。

## 基本信息

```yaml
title: 服务接管 RPC 超时定位
skill_name: diagnose-service-takeover
module_name: PAYMENT
```

`skill_name` 使用 `diagnose-` 开头的英文 lower-kebab 名称。`module_name` 是写入
生成 Skill 正文并用于 anchor 的固定业务 module，不是运行时参数。

## 问题范围

定位合成的调用方到服务端 RPC deadline exceeded：区分调用方 3 秒 deadline、服务端
连接池等待和处理延迟。没有同一请求的两端机器证据时，不确认服务端根因。

## 目标进程角色

每行描述一个运行时 anchor 角色。具体 slot、process name 与 PID 不写入 Wiki。

| 标签 | 说明 | 是否必需 |
| --- | --- | --- |
| client | 发起 RPC 的客户端进程 | 是 |
| server | 接收并处理 RPC 的服务端进程 | 是 |

roles 保持声明顺序；label 使用稳定小写标识。module 由基本信息固定。

## 自定义定位参数候选

只列 Wiki 有依据的任务级单行关联值。它们仍需在生成前明确确认。

| 参数名 | 说明 | 是否必需 |
| --- | --- | --- |
| order_id | 合成请求的唯一关联标识 | 是 |

不需要额外参数时省略本节，并在生成草案中明确“不设置”。S07 的参数组 A、
`log_archive` 和 `order_id` 固定 requirement 不能在这里换名或重复声明其他含义。

## 时间特征

- 调用方 deadline exceeded 必须位于 problem_time 附近。
- 服务端较晚记录只有在同一 order_id 或连续因果链下才能关联。
- 3 秒 deadline 只证明调用方等待边界，不单独证明服务端处理耗时或根因。

## 定位步骤

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

## 生成参数（不写入 Wiki 产品正文的敏感值）

生成时另行提供并确认：

- `capability`
- 非敏感 Router `summary`
- Skill `version`（首次为 `2.0.0`）
- 固定 `logparse_product`
- 固定 logparse 版本声明的、已通过 S00 Canonical grammar 的 ContentType 列表
- 明确 assumptions
