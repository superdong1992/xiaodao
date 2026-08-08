---
name: diagnose-link-timeout
description: 用于链路超时问题定位；必须先调用 logparse-diagnose 获取 target_logs，再按规则分析并生成扁平 result.zip。
module_name: module1
roles:
  - label: client
    description: 发起请求的客户端进程
    required: true
  - label: server
    description: 处理请求的服务端进程
    required: false
---

# 链路超时定位

## 问题范围

定位客户端发起请求后未及时收到服务端响应的问题。

## 运行时输入

使用 `input_path`、`config_path`、`output_dir`、`python_command`、
`problem_time`、`request_path` 和 `pack_result_script`。固定 module_name 写入
`targets[].module`，运行时不再询问 module。

| 标签 | 说明 | 是否必需 | 运行时字段 |
| --- | --- | --- | --- |
| client | 发起请求的客户端进程 | 是 | `slot`、`process_name`、可选 `pid` |
| server | 处理请求的服务端进程 | 否 | `slot`、`process_name`、可选 `pid` |

| 字段 | 是否必需 |
| --- | --- |
| `label` | 是 |
| `slot` | 是 |
| `process_name` | 是 |
| `pid` | 否 |

## 自定义定位参数

| 参数名 | 说明 | 是否必需 |
| --- | --- | --- |
| request_id | 本次请求的唯一标识 | 是 |
| error_code | 已知错误码 | 否 |

## 先调用 logparse-diagnose skill

先调用 `logparse-diagnose`，由它执行 `cli.py mech-target-logs` 并返回结构化
`target_logs`。

## 证据收敛约束

只读取 `target_logs[*].log_path`；不要遍历 output，也不要重新选择 lifecycle/cycle。

## 时间相关性要求

从问题时间附近开始分析，并在 `result.txt` 中写明时间相关性说明。

## Wiki 定位步骤

1. 检查请求发送与响应记录。
2. 使用 request_id 关联客户端与服务端证据。

## 判断规则

证据不完整时必须写“当前证据不足以确认根因”。

## Result.zip 交付物

生成包含 `result.txt` 和实际使用日志的扁平 `result.zip`，并使用运行时提供的
`pack_result_script` 打包。

## 最终回复

返回定位结论、关键证据、证据缺口和结果路径。
