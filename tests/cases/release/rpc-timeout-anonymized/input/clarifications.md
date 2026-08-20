# 用例澄清

本文件只补齐会改变可执行测试语义的信息，不代表真实产品日志格式。
为避免同一语义因无意义缩进超过真实转换的单响应上限，唯一最终 `Write` 必须使用 UTF-8 紧凑
JSON（不输出缩进和多余空白）；这只约束序列化，不得删减任何字段、规则、路径或业务语义。

本离线用例的转换身份固定为：Skill id `diagnose-anonymized-rpc-timeout`、版本 `6.2.0`、
capability `anonymized-rpc-timeout`、deployment scope `TEST_ONLY`。作者确认 `client` 与 `server`
依次为 REQUIRED role，role label 必须分别与同名 anchor label 一致；前者表示发起调用并接收响应的
客户端贡献者，后者表示在共享串行 lane 中接收并执行 API 的服务端贡献者。两者 module 固定为
`bbbb`。内置 profile 自动生成 `problem_time`、每个 role 的 `slot/process_name/pid` 以及
`log_archive`；其中 `problem_time`、`slot`、`process_name` 与归档必选，`pid` 可选。

作者确认 Wiki 专属参数中 `service_name`、`api_name` 为 REQUIRED，`target_version`、
`transport_protocol` 为 OPTIONAL；AFTER_LOGPARSE 的 `request_id` 为 CONDITIONAL，仅当已提供的
`transport_protocol` 等于 `standard` 时激活。上述澄清是 GenerationSpec 中 `confirmed=true`
的权威来源；不得把未确认的模型提议直接写入最终 GenerationSpec。
终止路径 id 按选择顺序固定为 `complete_queue_and_upstream`、`complete_api_overrun`、
`complete_deadloop_overrun`、`partial_queue_non_rpc_lane`、`partial_queue_mixed_coverage`、
`partial_server_receive_aggregate`、`partial_cross_clock_ambiguity`、
`partial_client_receive_aggregate`、`none`；最后一条才是无条件 NONE fallback，前八条任一正向
semantic PASS 都不得落入 NONE。

本用例的非排队 event id 固定为 `client_timeout_call`、`client_timeout_detail`、`late_response`、
`api_complete`、`deadloop_detected`。同一五行块按目标位置形成
`queue_history_target_first` 至 `queue_history_target_fifth` 五个 selector 视图；每个视图仍抽取完整
五行，只在自己的位置用 service/API selector 选择目标。排队 rule id 使用稳定的
`q_{target}_...` 族，分别表达 presence、timeout 一致性、串行顺序、正重叠、连续覆盖和未覆盖空隙；
非排队 rule id 保持原有 late-response、api-overrun 与 deadloop 命名。factor id 固定为
`server_queue_contribution`、`upstream_lane_blocker`、`non_rpc_lane_occupancy`、
`server_side_sojourn_overrun`、`server_receive_queue`、`direct_api_overrun`、
`client_receive_queue`、`server_receive_aggregate_overrun`、`client_receive_aggregate_overrun`；
这些 id 只属于该脱敏用例，不得进入框架级源码或其他测试夹具。

## 五位置 `q_{target}` 权威机械矩阵

下面矩阵是作者确认的既有业务语义，不是可自由改写的建议，也不提供预生成 JSON、场景答案或验收
预期。转换时只按矩阵机械展开，不重新设计 family、公式、dependency 或 terminal branch。令
`start(x)=x_end_us-x_cost_us`、`request(k)=start(k)-k_queue_us`；目标排队区间为
`[request(k),start(k))`，前序 RPC `i` 的执行区间为 `[start(i),i_end_us)`。

| `target=k` | extractor id | selector 字段 | 有序前序位置 `P(k)` | 最近前序 |
| --- | --- | --- | --- | --- |
| `first` | `queue_history_target_first` | `first_service/first_api` | `[]` | 无 |
| `second` | `queue_history_target_second` | `second_service/second_api` | `[first]` | `first` |
| `third` | `queue_history_target_third` | `third_service/third_api` | `[first,second]` | `second` |
| `fourth` | `queue_history_target_fourth` | `fourth_service/fourth_api` | `[first,second,third]` | `third` |
| `fifth` | `queue_history_target_fifth` | `fifth_service/fifth_api` | `[first,second,third,fourth]` | `fourth` |

每一行先展开共同基座，再按 `P(k)` 展开前序 family。所有机械比较均为同一 server clock、
`quantifier=EXISTS`、`joins=[]`、`clock_tolerance_ms=0`；共同语义 dependency 还必须保留
`enhanced_version`、`known_protocol`、对应 presence、timeout、总预算、执行时长、正排队和串行规则。

| family 模板 | 实例范围 | 固定机械语义或 dependency |
| --- | --- | --- |
| `q_{k}_present` | 每个 `k` | 对 `queue_history_target_{k}` 的 `PRESENT`。 |
| `q_{k}_timeout_consistent` | 每个 `k` | `FIELDS_EQUAL(client_timeout_call.timeout_ms, client_timeout_detail.timeout_ms, k_timeout_ms)`。 |
| `q_{k}_total_exceeds_timeout` | 每个 `k` | `k_queue_us+k_cost_us > CONVERT(k_timeout_ms,MICROSECOND)`。 |
| `q_{k}_execution_within_timeout` | 每个 `k` | `k_cost_us <= CONVERT(k_timeout_ms,MICROSECOND)`。 |
| `q_{k}_queue_positive` | 每个 `k` | `k_queue_us > 0 MICROSECOND`。 |
| `q_{k}_serial_{left}_{right}` | 从 `first` 到 `k` 的每个相邻对 | `left_end_us <= start(right)`；不得跳过中间位置。 |
| `q_{k}_overlap_{i}_starts_before_end` 与 `...ends_after_start` | 每个 `i ∈ P(k)` | `start(i) < start(k)` 且 `i_end_us > request(k)`，两者共同证明正时长交集。 |
| `q_{k}_cover_from_{i}_starts_before_queue` | 每个 `i ∈ P(k)` | `start(i) <= request(k)`。 |
| `q_{k}_cover_{left}_{right}_no_gap` | `i` 到最近前序之间的每个相邻对 | `start(right) <= left_end_us`；与 serial 同时成立才是无空隙且不重叠。 |
| `q_{k}_cover_ends_after_queue` | `P(k)` 非空 | 最近前序的 `end_us >= start(k)`。 |
| `q_{k}_latest_prior_before_queue` | `P(k)` 非空 | 最近前序的 `end_us <= request(k)`。 |
| `q_{k}_gap_prefix_open` / `...gap_suffix_open` | `P(k)` 非空 | 最早前序 `start > request(k)` / 最近前序 `end < start(k)`。 |
| `q_{k}_gap_{left}_{right}_open` | 每个前序相邻对 | `left_end_us < start(right)`；对应 `...before_end` 与 `...after_start` 还要证明该空隙与目标排队有正交集。 |
| `q_{k}_overlap_{i}_confirmed` | 每个 `i ∈ P(k)` | 共同基座、全部 serial 与该 `i` 的两条 overlap 机械规则。 |
| `q_{k}_full_from_{i}` | 每个 `i ∈ P(k)` | 共同基座、全部 serial、从 `i` 覆盖起点、覆盖终点以及 `i` 到最近前序的全部 no-gap。 |
| `q_{k}_non_rpc_lane_confirmed` | 每个 `k` | `first` 无前序；其他位置依赖 `latest_prior_before_queue`，确认全部排队来自身份未知的非 RPC lane 占用。 |
| `q_{k}_gap_*_confirmed` | 每个 prefix/internal/suffix gap | 共同基座、全部 serial 和对应 gap 机械闭包，确认存在身份未知的非 RPC lane 空隙。 |

terminal branch 也只做机械笛卡尔展开：`complete_queue_and_upstream` 包含每个
`q_{k}_full_from_{i}=PASS`；`partial_queue_non_rpc_lane` 包含五个
`q_{k}_non_rpc_lane_confirmed=PASS`；`partial_queue_mixed_coverage` 包含每个
`q_{k}_overlap_{i}_confirmed=PASS` 与同一 `k` 的每个 prefix/internal/suffix
`q_{k}_gap_*_confirmed=PASS` 组合。其余六条非排队/fallback path 保持上文固定顺序。完整展开必须
保留五个目标视图、165 条 rule 和九条 terminal path；这些计数只是闭包完整性校验，不能替代任何
字段、公式、dependency、witness 或业务语义。

- `wiki.md` 是从用户手写 Wiki 整理出的脱敏输入；`(# ... #)` 与 `（# ... #）` 均为转换旁注，不能进入生成 Skill。
- 原Wiki省略的长日志3和4不要求用户补写平台统一前缀或正则。为离线自包含用例，下面只定义合成快照的稳定消息体；真实部署由实际 Wiki 转换时生成对应定位器。
- 合成日志3：`late response service:{service}, api:{api}, reqid:{reqid}, timeout:{ms}, client_send:{us}, server_recv:{us}, server_send:{us}, client_now:{us}`。
- 合成日志4：`api complete service:{service}, api:{api}, start:{us}, end:{us}, cost:{us}, timeout:{ms}`。
- 合成日志7：`cost too long, service:{service}, api:{api}, start time:{us}, cur time:{us}, request time:{us}, timeout:{ms}`；只匹配稳定消息体，不要求用户补平台统一前缀。
- event extractor 必须在提取阶段使用 `EQUALS` selector 限定当前目标调用，不能先提取其他服务、API或请求再依赖下游 rule 排除：`client_timeout_call` 将 `service`、`api` 分别绑定 `USER_FACT(service_name)`、`USER_FACT(api_name)`；`client_timeout_detail` 将 `service`、`request_id` 分别绑定 `USER_FACT(service_name)`、`USER_FACT(request_id)`；五个 `queue_history_target_*` 视图分别把自己位置的 service/API 绑定同一目标事实；`late_response` 将 `service`、`api`、`request_id` 分别绑定 `USER_FACT(service_name)`、`USER_FACT(api_name)`、`USER_FACT(request_id)`；`api_complete` 与 `deadloop_detected` 均将 `service`、`api` 分别绑定 `USER_FACT(service_name)`、`USER_FACT(api_name)`。非目标行不得形成对应 event。
- 所有微秒字段使用整数 `MICROSECOND`，timeout使用整数 `MILLISECOND`。
- `Q = server_recv_us - client_send_us`、`S = server_send_us - server_recv_us`、`C = client_now_us - server_send_us`，客户端端到端为`client_now_us - client_send_us = Q + S + C`。Q和C跨client/server clock domain，必须使用显式100毫秒容差；S与客户端端到端是同钟计算，使用0容差。timeout先从毫秒显式转换为微秒。
- 匿名化新版本值为`enhanced_v2`，旧版本值为`legacy_v1`；只有五行排队增强路径与deadloop路径必须通过`enhanced_version`，late_response和api_complete正向路径不依赖该版本guard。
- 匿名化协议值为`standard`和`silent_timeout_detail`；后者可能没有客户端日志2。
- BBBB默认抑制策略在本用例中的稳定ID为`bbbb_default_suppression`，应用于全部事件；其`scope=process_instance_source_line_errno`表达进程实例、源码行与错误码是策略内在键，`window_ms=75000`、`boundary=CLOSED_OPEN`。这些键不是每条合成消息都能动态提取的共同字段，所以`key_fields`保持空数组。五个排队目标视图都额外叠加ID为`queue_rate_limit`的180秒进程级限流，稳定`scope=process_instance`，`max_observed=1`、`boundary=CLOSED_OPEN`；用例没有任何“明确无抑制”事件。
- 请求ID只在单个进程实例内唯一；关联身份是anchor所确定的进程实例与request ID，不允许把request ID当成全局唯一键。
- 每个场景的 `client.log` 与 `server.log` 各自只绑定一次且是该 anchor 的完整、有界固定快照；两份附件不互为副本或别名。一次匹配按来源文件与原始行区间唯一，不能把同一行重复计为多个事件。扫描完整性不改变抑制语义：受 SUPPRESSION 或 RATE_LIMIT 影响的目标日志即使在完整快照中缺失，也只能判为 UNKNOWN。
- 合成五行块只为本离线用例明确：first 到 fifth 按结束先后从旧到新，目标 service/API 可以位于任一位置；一个完整块列出所有可能与目标排队区间重叠的 RPC API 执行记录。目标位置为 `k` 时，`target_start=k_end-k_cost`、`target_request=target_start-k_queue`，目标排队区间为半开区间 `[target_request,target_start)`；位置早于 `k` 的记录才是前序 RPC 候选，晚于或等于 `k` 的其他记录不得倒因果使用。
- 共享 lane 串行执行。每个相邻位置都必须机械验证 `left_end <= right_start`；前序 RPC 执行区间 `[end-cost,end)` 与目标排队区间必须有正时长交集才算贡献，端点相接不算贡献。前序 RPC 区间并集只有在不晚于目标排队起点开始、不早于目标排队终点结束，且覆盖链每个相邻端点都同时满足 `left_end <= right_start` 与 `right_start <= left_end`（即无空隙且不重叠）时才可进入 COMPLETE。
- 至少一个前序 RPC 有正交集、但前缀、内部或后缀存在正时长未覆盖空隙时进入 `partial_queue_mixed_coverage`：发布全部已确认 RPC 贡献者，同时确认未覆盖空隙属于非 RPC lane 占用，但具体非 RPC 工作身份未知。完整五行块中最近前序 RPC 已不晚于目标排队起点结束，或目标位于 first 而没有更早记录时，进入 `partial_queue_non_rpc_lane`：确认全部排队来自非 RPC lane 占用，具体身份仍未知。
- 每个目标位置的 `q_{target}_total_exceeds_timeout` 必须严格比较该位置的 `queue_us + cost_us` 与显式转换为微秒的同位置 `timeout_ms`；`client_timeout_call.timeout_ms`、`client_timeout_detail.timeout_ms`和目标位置 timeout 必须相等，不能用不同预算拼出排队结论。
- 排队块缺失或受抑制时，五个目标视图及其排队原因都保持 UNKNOWN；不能用零匹配确认无排队。PARTIAL 至少一个 completion criterion 必须未完成；NONE 不发布任何 factor，criteria 全为 UNKNOWN。
- 只有 `transport_protocol=standard` 的场景才提供条件激活的 `request_id`。依赖 `client_timeout_detail` 或 `late_response` request selector 的场景必须使用该协议；不依赖 request selector 的 `silent_timeout_detail` 场景不得提交未激活的 `request_id`。
- api_complete与deadloop只凭选择器命中的正向日志和机械时长阈值进入COMPLETE；deadloop还必须同时满足执行时长严格大于2倍timeout和严格大于60秒，并受`enhanced_version`保护。once-per-call只说明同一次调用不重复打印，不允许据其缺失排除死循环。
- late_response PARTIAL只依赖自身service、API、request选择器、`response_after_deadline`及对应Q/S/C聚合机械规则，不依赖`enhanced_version`或同步专用`client_timeout_call`。
- 两条positive机械规则采用对称且唯一的机器语义：`server_queue_positive`的`depends_on`严格且仅为`["late_response_present"]`，left为`late_response.server_recv_us - late_response.client_send_us`（Q），operator为`GT`，right为`CONST(value=0, unit=MICROSECOND)`；`client_queue_positive`的`depends_on`严格且仅为`["late_response_present"]`，left为`late_response.client_now_us - late_response.server_send_us`（C），operator为`GT`，right同为`CONST(value=0, unit=MICROSECOND)`。两者都使用`quantifier=EXISTS`、`joins=[]`、`clock_tolerance_ms=100`，不得依赖版本、同步专用事件或其他原因分支。
- 当同一服务端时钟的`S=server_send_us-server_recv_us`超过timeout，且late_response的service、API、request选择器与客户端端到端deadline规则均通过时，必须存在可达的PARTIAL分支确认`server_side_sojourn_overrun`；`server_receive_queue`、`direct_api_overrun`与`client_receive_queue`仍保持候选。Q或C落在100毫秒跨时钟容差内，或没有越过timeout时，该分支不得要求`server_queue_positive`或`client_queue_positive`为PASS。
- 三条late_response PARTIAL必须互相独立且可达：`partial_snapshot_supported`只依赖`late_response_present`、`response_after_deadline`和`server_sojourn_exceeds_timeout`，只服务于S超预算分支；`server_receive_aggregate_partial`只依赖`late_response_present`、`response_after_deadline`和`server_receive_aggregate_exceeds_timeout`，不得再依赖S、C或`partial_snapshot_supported`；`client_receive_aggregate_partial`只依赖`late_response_present`、`response_after_deadline`和`client_receive_aggregate_exceeds_timeout`，不得再依赖S、Q或`partial_snapshot_supported`。对应terminal path只要求本分支必要的机械结果和本分支semantic PASS，不得要求其他聚合段为PASS。
- 原因1至3是常见原因而非封闭全集；本用例只验证Skill声明范围内的因素，不排除范围外原因。
- 超时不等于取消；后续API可能执行并产生副作用，结果必须保留该安全说明。
- 快照之外不补日志、不等待未来日志，也不根据用户问题时间启动监控。
