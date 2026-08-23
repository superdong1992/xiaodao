# RPC client 收包线程被阻塞

## 适用条件

判断第三种可能原因：RPC client 收包线程被本进程作为 server 承载的 API 调用、异步回调等
其他耗时操作阻塞。使用关联目标请求的迟到响应计算客户端收包线程排队时间。

## 所需证据

日志模板为：

```text
LATE_RESPONSE service={service} api={api} request_id={request_id} client_send_us={client_send_us} server_recv_us={server_recv_us} server_send_us={server_send_us} client_now_us={client_now_us}
```

## 计算与判断

四个时间字段都是微秒时间戳，拆成三段：

- 服务端收包线程排队：`server_recv_us-client_send_us`。
- 服务端 API 执行：`server_send_us-server_recv_us`。
- 客户端收包线程排队：`client_now_us-server_send_us`。

三段之和为端到端时间 `client_now_us-client_send_us`。与超时时间比较前，先把毫秒换算成微秒。
当端到端总时长超过超时时间，先看服务端 API 执行时间；若它没有超过超时，则执行本身没有
超时，是收包线程排队使总时长超过超时。此时比较两个排队时间，较长的一方对应被占用更久的
收包线程；客户端排队时间更长时，支持本原因。

## 确认条件

关联目标请求的 `LATE_RESPONSE` 满足：端到端总时长超过超时、服务端 API 执行时间不超过超时，
且客户端收包线程排队时间长于服务端收包线程排队时间，即可确认客户端收包线程被阻塞。该
原因可以与其他方法同时成立。

## 未知边界

迟到响应或客户端排队相关日志受 BBBB 默认抑制；缺失不能排除客户端收包线程阻塞。若只有超时
模板而没有迟到响应的完整时间字段，无法确认本原因，也不能用缺失反推其他原因。

## 输出含义

报告四个时间戳、三段时长、毫秒到微秒换算、比较结果及关联请求 ID。明确该结论表示客户端收包
线程等待响应期间被占用更久；RPC 超时不等于取消，服务端仍可能继续执行并产生副作用。
