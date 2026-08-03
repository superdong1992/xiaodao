# 服务接管 RPC 超时定位

本 Fixture 只描述合成服务和合成请求，不包含生产客户、凭据、真实订单号或内部路径。

## 基本信息

```yaml
title: 服务接管 RPC 超时定位
skill_name: diagnose-service-takeover
module_name: COMPACT
```

## 问题范围

定位合成的调用方到服务端 RPC deadline exceeded：区分调用方 3 秒 deadline、服务端
连接池等待和处理延迟。没有同一合成请求的两端机器证据时，不确认服务端根因。

## 目标进程角色

| 标签 | 说明 | 是否必需 |
| --- | --- | --- |
| client | 发起合成 RPC 的客户端进程；broker anchor 固定为 slot=`1`、process_name=`checkout-client`、pid=`101` | 是 |
| server | 接收并处理合成 RPC 的服务端进程；broker anchor 固定为 slot=`2`、process_name=`inventory-server`、pid=`202` | 是 |

## 自定义定位参数候选

| 参数名 | 说明 | 是否必需 |
| --- | --- | --- |
| order_id | 合成请求的唯一关联标识 | 是 |

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
