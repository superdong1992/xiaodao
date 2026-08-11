# 用例澄清

本文件只补齐会改变可执行测试语义的信息，不代表真实产品日志格式。

本离线用例的转换身份固定为：Skill id `diagnose-anonymized-rpc-timeout`、版本 `5.0.0`、
capability `anonymized-rpc-timeout`、deployment scope `TEST_ONLY`。INITIAL INPUT 依次为
`service_name`、`api_name`、`target_version`、`transport_protocol`、`problem_time`、
`client_process`、`server_process`，INITIAL ATTACHMENT 为 `log_archive`，AFTER_LOGPARSE INPUT
为 `request_id`。Logparse anchors 依次为 `client` 与 `server`，module 固定为 `bbbb`，slot
分别为 `client_slot`、`server_slot`，process name 分别绑定 `client_process`、`server_process`。
终止路径 id 依次固定为 `complete_queue_and_upstream`、`partial_cross_clock_ambiguity`、`none`；
最后一条是无条件 NONE fallback。

- `wiki.md` 是从用户手写 Wiki 整理出的脱敏输入；`(# ... #)` 与 `（# ... #）` 均为转换旁注，不能进入生成 Skill。
- 原Wiki省略的长日志3和4不要求用户补写平台统一前缀或正则。为离线自包含用例，下面只定义合成快照的稳定消息体；真实部署由实际 Wiki 转换时生成对应定位器。
- 合成日志3：`late response service:{service}, api:{api}, reqid:{reqid}, timeout:{ms}, client_send:{us}, server_recv:{us}, server_send:{us}, client_now:{us}`。
- 合成日志4：`api complete service:{service}, api:{api}, start:{us}, end:{us}, cost:{us}, timeout:{ms}`。
- 所有微秒字段使用整数 `MICROSECOND`，timeout使用整数 `MILLISECOND`。
- client与server属于不同clock domain；跨clock比较必须使用显式100毫秒容差。同一clock domain比较使用0容差。
- 匿名化新版本值为`enhanced_v2`，旧版本值为`legacy_v1`；本用例的新诊断日志只对`enhanced_v2`适用。
- 匿名化协议值为`standard`和`silent_timeout_detail`；后者可能没有客户端日志2。
- BBBB默认抑制策略应用于本用例全部事件。排队五行块额外叠加180秒进程级限流；用例没有任何“明确无抑制”事件。
- 请求ID只在单个进程实例内唯一；关联身份是anchor所确定的进程实例与request ID，不允许把request ID当成全局唯一键。
- 合成五行块只为本离线用例明确：first是当前触发调用，second是紧邻前序调用；真实Wiki若不保证顺序，转换时必须重新澄清。
- 原因1至3是常见原因而非封闭全集；本用例只验证Skill声明范围内的因素，不排除范围外原因。
- 超时不等于取消；后续API可能执行并产生副作用，结果必须保留该安全说明。
- 快照之外不补日志、不等待未来日志，也不根据用户问题时间启动监控。
