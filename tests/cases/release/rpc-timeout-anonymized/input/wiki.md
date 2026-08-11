# CCCC（#CCCC是RPC模块的名字，匿名化了#）简介

CCCC即RPC（Remote Procedure Call）过程远程调用，它的作用是把消息收发通信的编程方式转变为API接口调用方式，极大提升了开发效率。在xxx（#版本号匿名化了#）版本，引入了cccc_dbgex（#新增了一个目录，实现了cccc模块的维测能力增强#）维测增强能力，因此基于旧版本的定位wiki上修改得到本wiki。

# RPC调用超时失败

无论是rpc同步调用，还是异步调用，都有rpc超时的概念。在指定时间内cccc client的收包线程没有收到cccc server回复的包含api执行结果的rpc响应消息，那么就会触发rpc超时失败：

1. 同步请求会在rpc超时时返回，并填充返回结构体CCCC_STATUS_S，将bRpcSuccess设置为false，将uiErrno设置为`0xaaaaaaaa(#错误码被匿名化了#) CCCC_ERRNO_REQUEST_TIMEOUT_FAIL`。
2. 异步请求会在rpc超时时调用用户注册的回调函数，该函数的第一个入参也是结构体CCCC_STATUS_S，设置的内容与同步超时一致。

rpc超时只能说明响应没有在deadline前被客户端接收；API可能尚未开始、正在执行、已经执行或稍后执行，超时本身不等于取消。

# RPC调用超时失败的可能原因

RPC超时可能有以下常见原因，按本Wiki的局部经验从大到小排序，但不是穷尽集合：

- 原因1：用户指定的rpc对应的api执行时间过长。
- 原因2：rpc server收包线程串行承载多个服务实例或融合业务，同一执行lane中的前序耗时操作导致后续API排队。
- 原因3：rpc client收包线程被作为server的API、异步回调或其他融合业务阻塞。

# BBBB（#这是CCCC对应的上一级进程间消息通信机制的名字，也是logparse配置中的一个模块#）日志定位

BBBB默认存在进程级日志抑制：同一进程内，如果75秒内打印过同源码行、同错误码日志，后续相同键日志不会再次打印。除非某条日志明确说明“无抑制”，否则本Wiki中的BBBB日志均按受该默认规则影响处理；字段值、请求ID和线程不属于默认抑制键。

1. 同步调用超时：`rpc call %s:%s timeout limit %u recv no response`，依次为服务名、API名、超时时间（毫秒）。
2. 同步和异步超时：`%s rpc %s call unsuccess, reqid(%u), timeout %u`，依次为服务名、调用类型、进程内唯一请求ID、超时时间（毫秒）。一种通信协议不记录本条日志。
3. 客户端最终收到晚响应时会打印三条很长的日志，包含服务名、API名、请求ID、客户端发送、服务端接收、服务端发送和客户端当前时间等微秒时间戳。（#原日志很长，这里只描述稳定字段语义#）
4. 服务端API执行结束后，若API自身耗时超过超时阈值，会打印服务名、API名、开始、结束和耗时（微秒）。（#原日志很长，这里只描述稳定字段语义#）
5. 如果只有客户端超时日志，且两侧进程仍存活，只能保留服务端lane阻塞和客户端收包线程阻塞等候选，不能从缺日志二选一。
6. 新版本的排队超时日志在服务端执行结束后一次打印同一线程最近5次API：

```text
[BBBB]The first service:{服务名}, api:{API名}, end time:{结束时间}, cost time:{执行耗时}, queue time:{排队时间}, timeout:{超时时间}
[BBBB]The second service:{服务名}, api:{API名}, end time:{结束时间}, cost time:{执行耗时}, queue time:{排队时间}, timeout:{超时时间}
[BBBB]The third service:{服务名}, api:{API名}, end time:{结束时间}, cost time:{执行耗时}, queue time:{排队时间}, timeout:{超时时间}
[BBBB]The fourth service:{服务名}, api:{API名}, end time:{结束时间}, cost time:{执行耗时}, queue time:{排队时间}, timeout:{超时时间}
[BBBB]The fifth service:{服务名}, api:{API名}, end time:{结束时间}, cost time:{执行耗时}, queue time:{排队时间}, timeout:{超时时间}
```

执行耗时为`end-start`，queue time为`start-request`。该五行块还受同进程180秒一次的限流，并叠加BBBB默认抑制。目标API前面的长耗时API可能才是上游根因。

7. 新版本在API仍执行时可打印死循环检测日志：

```text
[BBBB]cost too long, service:{服务名}, api:{API名}, start time:{开始时间}, cur time:{当前时间}, request time:{请求时间}, timeout:{超时时间}
```

当执行时长同时超过2倍timeout且超过60秒时触发；同一次调用只打印一次，并叠加BBBB默认抑制。（#当前通用合同首版只正式支持SUPPRESSION和RATE_LIMIT；once-per-call仅作为限制说明，不用于absence推理#）

# 特殊说明

cccc_dbgex只在匿名化版本边界之后存在。固定日志快照之外不补历史日志、不针对用户问题时间启动等待或监控。
