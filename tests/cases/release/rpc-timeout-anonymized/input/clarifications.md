# 用例澄清

本文件只补齐会改变可执行测试语义的信息，不代表真实产品日志格式。

本离线用例的转换身份固定为：Skill id `diagnose-anonymized-rpc-timeout`、版本 `5.0.0`、
capability `anonymized-rpc-timeout`、deployment scope `TEST_ONLY`。INITIAL INPUT 依次为
`service_name`、`api_name`、`target_version`、`transport_protocol`、`problem_time`、
`client_process`、`server_process`，INITIAL ATTACHMENT 为 `log_archive`，AFTER_LOGPARSE INPUT
为 `request_id`。Logparse anchors 依次为 `client` 与 `server`，module 固定为 `bbbb`，slot
分别为 `client_slot`、`server_slot`，process name 分别绑定 `client_process`、`server_process`。
本用例的 roles 依次为 `client` 与 `server`，role label 必须分别与同名 anchor label 一致；
前者表示发起调用并接收响应的客户端贡献者，后者表示在共享串行 lane 中接收并执行 API 的服务端贡献者。
终止路径 id 按选择顺序固定为 `complete_queue_and_upstream`、`complete_api_overrun`、
`complete_deadloop_overrun`、`partial_server_receive_aggregate`、
`partial_cross_clock_ambiguity`、`partial_client_receive_aggregate`、`none`；最后一条才是无条件
NONE fallback，前六条任一正向 semantic PASS 都不得落入 NONE。

本用例的 event id 固定为 `client_timeout_call`、`client_timeout_detail`、`queue_history`、
`late_response`、`api_complete`、`deadloop_detected`。rule id 固定为 `enhanced_version`、
`known_protocol`、`client_call_present`、`client_detail_present`、`queue_history_present`、
`late_response_present`、`api_complete_present`、`deadloop_present`、
`complete_service_correlates`、`complete_api_correlates`、`complete_timeout_consistent`、
`queue_total_exceeds_timeout`、`target_execution_within_timeout`、`target_queue_positive`、
`prior_end_equals_target_start`、`prior_execution_overlaps_target_queue`、
`prior_api_longer_than_target`、`api_duration_exceeds_timeout`、
`server_sojourn_exceeds_timeout`、`server_queue_positive`、`client_queue_positive`、
`server_receive_aggregate_exceeds_timeout`、`client_receive_aggregate_exceeds_timeout`、
`response_after_deadline`、`deadloop_execution_exceeds_twice_timeout`、
`deadloop_execution_exceeds_sixty_seconds`、`queue_contributed_timeout`、
`upstream_api_caused_queue`、`api_overrun_confirmed`、`deadloop_overrun_confirmed`、
`partial_snapshot_supported`、`server_receive_aggregate_partial`、
`client_receive_aggregate_partial`。
factor id 固定为 `server_queue_contribution`、`upstream_lane_blocker`、
`server_side_sojourn_overrun`、`server_receive_queue`、`direct_api_overrun`、
`client_receive_queue`、`server_receive_aggregate_overrun`、
`client_receive_aggregate_overrun`；这些 id 只属于该脱敏用例，不得进入框架级源码或其他测试夹具。

- `wiki.md` 是从用户手写 Wiki 整理出的脱敏输入；`(# ... #)` 与 `（# ... #）` 均为转换旁注，不能进入生成 Skill。
- 原Wiki省略的长日志3和4不要求用户补写平台统一前缀或正则。为离线自包含用例，下面只定义合成快照的稳定消息体；真实部署由实际 Wiki 转换时生成对应定位器。
- 合成日志3：`late response service:{service}, api:{api}, reqid:{reqid}, timeout:{ms}, client_send:{us}, server_recv:{us}, server_send:{us}, client_now:{us}`。
- 合成日志4：`api complete service:{service}, api:{api}, start:{us}, end:{us}, cost:{us}, timeout:{ms}`。
- 合成日志7：`cost too long, service:{service}, api:{api}, start time:{us}, cur time:{us}, request time:{us}, timeout:{ms}`；只匹配稳定消息体，不要求用户补平台统一前缀。
- 六个 event extractor 必须在提取阶段使用 `EQUALS` selector 限定当前目标调用，不能先提取其他服务、API或请求再依赖下游 rule 排除：`client_timeout_call` 将 `service`、`api` 分别绑定 `USER_FACT(service_name)`、`USER_FACT(api_name)`；`client_timeout_detail` 将 `service`、`request_id` 分别绑定 `USER_FACT(service_name)`、`USER_FACT(request_id)`；`queue_history` 将 `first_service`、`first_api` 分别绑定 `USER_FACT(service_name)`、`USER_FACT(api_name)`；`late_response` 将 `service`、`api`、`request_id` 分别绑定 `USER_FACT(service_name)`、`USER_FACT(api_name)`、`USER_FACT(request_id)`；`api_complete` 与 `deadloop_detected` 均将 `service`、`api` 分别绑定 `USER_FACT(service_name)`、`USER_FACT(api_name)`。非目标行不得形成对应 event。
- 所有微秒字段使用整数 `MICROSECOND`，timeout使用整数 `MILLISECOND`。
- `Q = server_recv_us - client_send_us`、`S = server_send_us - server_recv_us`、`C = client_now_us - server_send_us`，客户端端到端为`client_now_us - client_send_us = Q + S + C`。Q和C跨client/server clock domain，必须使用显式100毫秒容差；S与客户端端到端是同钟计算，使用0容差。timeout先从毫秒显式转换为微秒。
- 匿名化新版本值为`enhanced_v2`，旧版本值为`legacy_v1`；只有五行排队增强路径与deadloop路径必须通过`enhanced_version`，late_response和api_complete正向路径不依赖该版本guard。
- 匿名化协议值为`standard`和`silent_timeout_detail`；后者可能没有客户端日志2。
- BBBB默认抑制策略在本用例中的稳定ID为`bbbb_default_suppression`，应用于全部事件；其`scope=process_instance_source_line_errno`表达进程实例、源码行与错误码是策略内在键，`window_ms=75000`、`boundary=CLOSED_OPEN`。这些键不是每条合成消息都能动态提取的共同字段，所以`key_fields`保持空数组。排队五行块额外叠加ID为`queue_rate_limit`的180秒进程级限流，稳定`scope=process_instance`，`max_observed=1`、`boundary=CLOSED_OPEN`；用例没有任何“明确无抑制”事件。
- 请求ID只在单个进程实例内唯一；关联身份是anchor所确定的进程实例与request ID，不允许把request ID当成全局唯一键。
- 每个场景的 `client.log` 与 `server.log` 各自只绑定一次且是该 anchor 的完整、有界固定快照；两份附件不互为副本或别名。一次匹配按来源文件与原始行区间唯一，不能把同一行重复计为多个事件。扫描完整性不改变抑制语义：受 SUPPRESSION 或 RATE_LIMIT 影响的目标日志即使在完整快照中缺失，也只能判为 UNKNOWN。
- 合成五行块只为本离线用例明确：first是当前触发调用，`target_start=first_end-first_cost`，`target_request=target_start-first_queue`；second是紧邻前序调用，`prior_start=second_end-second_cost`。COMPLETE queue路径必须机械验证`second_end==target_start`，且在目标排队为正时`prior_start<target_start`，从而确认second执行区间与target排队区间重叠；只比较cost大小不够。真实Wiki若不保证顺序必须重新澄清。
- `queue_total_exceeds_timeout`表达目标调用从request到end的服务端总预算消耗：必须严格比较`first_queue_us + first_cost_us`与显式转换为微秒的`first_timeout_ms`，并证明前者更大；不能用queue或cost单项替代该总和。
- `client_timeout_call.timeout_ms`、`client_timeout_detail.timeout_ms`和`queue_history.first_timeout_ms`必须相等，不能用不同预算拼出COMPLETE queue结论。
- api_complete与deadloop只凭选择器命中的正向日志和机械时长阈值进入COMPLETE；deadloop还必须同时满足执行时长严格大于2倍timeout和严格大于60秒，并受`enhanced_version`保护。once-per-call只说明同一次调用不重复打印，不允许据其缺失排除死循环。
- late_response PARTIAL只依赖自身service、API、request选择器、`response_after_deadline`及对应Q/S/C聚合机械规则，不依赖`enhanced_version`或同步专用`client_timeout_call`。
- 两条positive机械规则采用对称且唯一的机器语义：`server_queue_positive`的`depends_on`严格且仅为`["late_response_present"]`，left为`late_response.server_recv_us - late_response.client_send_us`（Q），operator为`GT`，right为`CONST(value=0, unit=MICROSECOND)`；`client_queue_positive`的`depends_on`严格且仅为`["late_response_present"]`，left为`late_response.client_now_us - late_response.server_send_us`（C），operator为`GT`，right同为`CONST(value=0, unit=MICROSECOND)`。两者都使用`quantifier=EXISTS`、`joins=[]`、`clock_tolerance_ms=100`，不得依赖版本、同步专用事件或其他原因分支。
- 当同一服务端时钟的`S=server_send_us-server_recv_us`超过timeout，且late_response的service、API、request选择器与客户端端到端deadline规则均通过时，必须存在可达的PARTIAL分支确认`server_side_sojourn_overrun`；`server_receive_queue`、`direct_api_overrun`与`client_receive_queue`仍保持候选。Q或C落在100毫秒跨时钟容差内，或没有越过timeout时，该分支不得要求`server_queue_positive`或`client_queue_positive`为PASS。
- 三条late_response PARTIAL必须互相独立且可达：`partial_snapshot_supported`只依赖`late_response_present`、`response_after_deadline`和`server_sojourn_exceeds_timeout`，只服务于S超预算分支；`server_receive_aggregate_partial`只依赖`late_response_present`、`response_after_deadline`和`server_receive_aggregate_exceeds_timeout`，不得再依赖S、C或`partial_snapshot_supported`；`client_receive_aggregate_partial`只依赖`late_response_present`、`response_after_deadline`和`client_receive_aggregate_exceeds_timeout`，不得再依赖S、Q或`partial_snapshot_supported`。对应terminal path只要求本分支必要的机械结果和本分支semantic PASS，不得要求其他聚合段为PASS。
- 原因1至3是常见原因而非封闭全集；本用例只验证Skill声明范围内的因素，不排除范围外原因。
- 超时不等于取消；后续API可能执行并产生副作用，结果必须保留该安全说明。
- 快照之外不补日志、不等待未来日志，也不根据用户问题时间启动监控。
