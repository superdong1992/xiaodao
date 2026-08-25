# CCCC 简介

CCCC 是 RPC（Remote Procedure Call，远程过程调用）模块。它把消息收发的编程方式转换为 API
接口调用方式，提升了开发效率。BBBB 是 CCCC 的上一级进程间消息通信机制。

下文日志模板中，`{字段名}` 表示运行时字段值，花括号不属于日志原文；`%s`、`%u` 等格式化占位符
同样表示运行时字段值。

# RPC 调用超时失败

RPC 同步调用和异步调用都存在超时。在指定时间内，CCCC client 的收包线程没有收到 CCCC server
返回的、包含 API 执行结果的 RPC 响应消息，就会触发 RPC 超时失败：

1. 同步请求超时后返回，并填充结构体 `CCCC_STATUS_S`：`bRpcSuccess=false`，
   `uiErrno=0xaaaaaaaa CCCC_ERRNO_REQUEST_TIMEOUT_FAIL`。
2. 异步请求超时后调用用户注册的回调函数，第一个入参也是 `CCCC_STATUS_S`，内容与同步超时一致。

RPC 超时说明客户端没有在截止时间前收到响应，服务端可能执行该 API。超时本身不等于取消，
后续执行仍可能产生副作用。

# 参数说明
定位该问题时，用户必须提供的参数为RPC超时发生时间点、客户端和服务端进程信息、服务名和API名。除了这些参数，还需要提供日志。

# RPC 调用超时失败的可能原因

RPC 超时的常见原因如下，按可能性从大到小排序：

1. 用户调用的 API 执行时间过长。
2. RPC server 收包线程在此之前执行了其他耗时操作（包括但不限于其他 API 调用），目标 API 排队
   等待，响应无法在截止时间前返回，触发超时。
3. RPC client 收包线程被本进程作为 server 承载的 API 调用、异步回调等其他耗时操作阻塞。

# BBBB 日志定位

BBBB 默认存在进程级日志抑制：同一进程内，如果 75 秒内已经打印过同一源码行且错误码相同的日志，
后续同一源码行、同一错误码的日志不再打印。抑制只看进程、源码行和错误码，日志里的字段值、请求
ID、线程是什么都不影响；字段值不同也不会重新打印。除非某条日志明确说明"无抑制"，否则以下
BBBB 日志均受此规则影响。

例如，同一 client 进程内，svc_alpha 的 Fetch 调用超时，打印了"rpc call svc_alpha:Fetch timeout
limit 5000 recv no response"日志；75 秒内 svc_beta 的 List 调用也超时，虽然服务名和 API 名都
不同，但仍是同一源码行、同一错误码，这次不再打印。日志里只有一条记录，两次超时却都发生了。

## 客户端超时日志

1. 同步调用超时：

   ```text
   rpc call %s:%s timeout limit %u recv no response
   ```

   字段依次为服务名、API 名和超时时间（毫秒）。

2. 同步和异步调用超时：

   ```text
   %s rpc %s call unsuccess, reqid(%u), timeout %u
   ```

   字段依次为服务名、API 名、进程内唯一请求 ID 和超时时间（毫秒）。（进程内唯一请求ID对用户来说只在日志中能看到）

## 晚响应与 API 完成日志

3. 客户端最终收到迟到响应（late response）时打印：

   ```text
   LATE_RESPONSE service={service} api={api} request_id={request_id} client_send_us={client_send_us} server_recv_us={server_recv_us} server_send_us={server_send_us} client_now_us={client_now_us}
   ```

   四个时间字段都是微秒时间戳，能拆成三段：

   - 服务端收包线程排队时间：`server_recv_us - client_send_us`
   - 服务端 API 执行时间：`server_send_us - server_recv_us`
   - 客户端收包线程排队时间：`client_now_us - server_send_us`

   三段之和等于客户端端到端时间 `client_now_us - client_send_us`。与超时时间比较前，先把超时
   时间从毫秒换算成微秒。端到端总时长超过超时时间时，先看服务端 API 执行时间：它单独超过超时
   时间，说明是 API 自身执行过慢；没有超过，则说明执行本身没有超时，是收包线程排队让总时长
   超过了超时时间，两个排队时间中更长的一方，对应被占用更久的收包线程。

4. 服务端 API 执行结束时发现执行时间超过超时时间时打印：

   ```text
   API_COMPLETE service={service} api={api} start_us={start_us} end_us={end_us} cost_us={cost_us}
   ```

   `start_us`、`end_us`、`cost_us` 都是微秒值，`cost_us=end_us-start_us`。能看到这条日志，
   就说明 API 自身执行时间已经超过了超时时间。

## 排队历史日志

5. 服务端目标 API 执行结束后，会打印该收包线程最近的 1–5 条 API 历史，从旧到新依次对应
   `first|second|third|fourth|fifth` 五种序号：

   ```text
   QUEUE_HISTORY print_time_ms={print_time_ms} ordinal={ordinal} service={service} api={api} end_us={end_us} cost_us={cost_us} queue_us={queue_us} timeout_ms={timeout_ms}
   ```

   每条记录的 `print_time_ms` 是这条日志的打印时间。所有匹配记录中，最早和最晚打印时间相差
   不超过 1000 毫秒的，视为同一次历史输出；中间可以夹杂任意其他日志。历史条数不固定，1–5 条
   都有可能；序号从旧到新排列，`first` 是最旧的一条，最后一条记录始终是当前目标 API（条数为 N
   时，最后一条对应第 N 个序号），且必须与当前服务名和 API 名一致。

   对目标记录按以下两个条件判断是否因排队超时：

   ```text
   target_cost_us + target_queue_us > target_timeout_ms * 1000
   target_cost_us < target_timeout_ms * 1000
   ```

   两个条件同时成立，说明目标 API 自身执行没有超过超时时间，但排队加上执行的总耗时超过了超时
   时间，可见是排队导致超时。

   目标 API 的排队区间按以下公式还原：

   ```text
   target_execution_start_us = target_end_us - target_cost_us
   target_queue_start_us = target_execution_start_us - target_queue_us
   ```

   对目标之前的每条 API，只使用其 `end_us` 和 `cost_us` 计算实际执行区间；它自己的 `queue_us`
   只表示它在排队，不代表占用收包线程，不参与贡献判断：

   ```text
   prior_execution_start_us = prior_end_us - prior_cost_us
   overlap_us = min(prior_end_us, target_execution_start_us)
                - max(prior_execution_start_us, target_queue_start_us)
   ```

   `overlap_us > 0` 表示该前序 API 在目标 API 排队期间实际占用了收包线程。所有满足该条件的前序
   API 都是排队贡献者，不只选最近、耗时最长或经验上最可疑的一条。只有目标一条记录时，可以判断
   目标是否因排队超时，但无法确认具体是哪个前序 API 贡献了排队。

   排队历史日志受同进程 180 秒一次的限流，并叠加 BBBB 默认抑制。日志缺失不能反推没有排队。

## 死循环检测日志

6. API 仍在执行时可能打印：

   ```text
   DEADLOOP_DETECTED service={service} api={api} start_us={start_us} current_us={current_us} request_us={request_us} timeout_ms={timeout_ms}
   ```

   当 API 执行时长（`current_us - start_us`）超过超时时间的 2 倍，并且超过 60 秒时，才会打印
   这条日志（超时时间以毫秒计，先换算成微秒再比较）。同一次调用只打印一次，并叠加 BBBB 默认
   抑制。没看到这条日志不代表没有死循环。
