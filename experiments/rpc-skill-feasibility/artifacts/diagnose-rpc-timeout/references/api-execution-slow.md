# 用户 API 执行时间过长

## 适用条件

判断 RPC 超时的第一种可能原因：用户调用的 API 执行时间过长。目标请求和日志必须来自冻结
`target_logs` 指定的范围，并按共享引用解释超时和日志缺失。

## 所需证据

搜索并按目标服务名和 API 名关联以下日志：

```text
API_COMPLETE service={service} api={api} start_us={start_us} end_us={end_us} cost_us={cost_us}
DEADLOOP_DETECTED service={service} api={api} start_us={start_us} current_us={current_us} request_us={request_us} timeout_ms={timeout_ms}
```

## 计算与判断

`API_COMPLETE` 的 `start_us`、`end_us`、`cost_us` 都是微秒值，且 `cost_us=end_us-start_us`。
看到与目标 API 关联的这条日志，就表示 API 自身执行时间已经超过超时时间。

对于 `DEADLOOP_DETECTED`，先以微秒比较：API 执行时长为 `current_us-start_us`，它必须同时
超过 `timeout_ms` 换算成微秒后的 2 倍，并且超过 60 秒，才会打印该日志。同一次调用只打印
一次，并叠加 BBBB 默认抑制。

迟到响应也可作为补充计算证据。其模板为：

```text
LATE_RESPONSE service={service} api={api} request_id={request_id} client_send_us={client_send_us} server_recv_us={server_recv_us} server_send_us={server_send_us} client_now_us={client_now_us}
```

四个时间字段为微秒时间戳。服务端 API 执行时间是 `server_send_us-server_recv_us`；与超时
时间比较前把毫秒换成微秒。端到端总时长 `client_now_us-client_send_us` 超过超时时，若 API
执行时间单独超过超时时间，则 API 自身执行过慢。

## 确认条件

存在与目标 API 关联的 `API_COMPLETE`，或存在满足条件的 `DEADLOOP_DETECTED`，即可确认 API
自身执行时间过长。迟到响应中 API 执行时间单独超过超时时，也可确认该原因。它可以与其他
方法同时确认。

## 未知边界

`DEADLOOP_DETECTED` 受“超过超时 2 倍且超过 60 秒”的条件打印、同一次调用只打印一次及
BBBB 抑制影响；缺失不能排除死循环。若只有超时模板而无上述正向证据，不能确认 API 自身过慢。

## 输出含义

报告目标 API、使用的正向日志和计算结果；若确认，说明执行本身超过了超时。仍须说明 RPC
超时不等于取消，服务端后续执行可能产生副作用。
