---
name: diagnose-anonymized-rpc-timeout
description: "从固定双端日志快照定位脱敏RPC超时的多个贡献因素"
---

# 脱敏RPC调用超时定位

由 `wiki-to-diagnosis-skill` generator `5.0.0` 生成。公共 DIAGNOSE output
contract 只定义通用 Schema、安全、Evidence/Candidate 与原子输出；本文件独占业务
requirements、阶段、工具映射和判定规则。

<!-- DIAGNOSIS_SKILL_MANIFEST_V5_BEGIN -->
```json
{"capability":"anonymized-rpc-timeout","deployment_scope":"TEST_ONLY","entry_document":"SKILL.md","id":"diagnose-anonymized-rpc-timeout","logparse_plan":{"anchors":[{"label":"client","module":{"source":"SKILL_FIXED","value":"bbbb"},"pid":null,"process_name":{"name":"client_process","source":"USER_FACT"},"slot":{"source":"SKILL_FIXED","value":"client_slot"}},{"label":"server","module":{"source":"SKILL_FIXED","value":"bbbb"},"pid":null,"process_name":{"name":"server_process","source":"USER_FACT"},"slot":{"source":"SKILL_FIXED","value":"server_slot"}}],"attachment_requirement":"log_archive","problem_time_binding":{"name":"problem_time","source":"USER_FACT"}},"logparse_product":"bbbb","requirements":[{"constraints":{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"},"fulfillment_source":"USER_FACT","kind":"INPUT","name":"service_name","prompt":"请提供超时调用的服务名。","stage":"INITIAL","supplement_policy":"MISSING_ONLY"},{"constraints":{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"},"fulfillment_source":"USER_FACT","kind":"INPUT","name":"api_name","prompt":"请提供超时调用的API名。","stage":"INITIAL","supplement_policy":"MISSING_ONLY"},{"constraints":{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"},"fulfillment_source":"USER_FACT","kind":"INPUT","name":"target_version","prompt":"请提供目标模块版本分类。","stage":"INITIAL","supplement_policy":"MISSING_ONLY"},{"constraints":{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"},"fulfillment_source":"USER_FACT","kind":"INPUT","name":"transport_protocol","prompt":"请提供底层通信协议分类。","stage":"INITIAL","supplement_policy":"MISSING_ONLY"},{"constraints":{"allowed_values":[],"max_utf8_bytes":24,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"},"fulfillment_source":"USER_FACT","kind":"INPUT","name":"problem_time","prompt":"请提供毫秒精度UTC问题时间。","stage":"INITIAL","supplement_policy":"MISSING_ONLY"},{"constraints":{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"},"fulfillment_source":"USER_FACT","kind":"INPUT","name":"client_process","prompt":"请提供客户端进程名。","stage":"INITIAL","supplement_policy":"MISSING_ONLY"},{"constraints":{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"},"fulfillment_source":"USER_FACT","kind":"INPUT","name":"server_process","prompt":"请提供服务端进程名。","stage":"INITIAL","supplement_policy":"MISSING_ONLY"},{"constraints":{"allowed_content_types":["application/gzip","application/zip","application/x-tar"],"max_count":1,"min_count":1},"fulfillment_source":"READY_ATTACHMENT","kind":"ATTACHMENT","name":"log_archive","prompt":"请上传Logparse支持的固定日志归档。","stage":"INITIAL","supplement_policy":"MISSING_ONLY"},{"constraints":{"allowed_values":[],"max_utf8_bytes":64,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"},"fulfillment_source":"USER_FACT","kind":"INPUT","name":"request_id","prompt":"请提供需要关联的进程内请求ID。","stage":"AFTER_LOGPARSE","supplement_policy":"MISSING_ONLY"}],"requires_logparse":true,"schema_version":5,"summary":"从固定双端日志快照定位脱敏RPC超时的多个贡献因素","tool_bundle_id":"tool-bundle/diagnose","verification_contract":{"event_extractors":[{"anchor":"client","fields":[{"clock_domain":null,"name":"service","type":"STRING","unit":null},{"clock_domain":null,"name":"api","type":"STRING","unit":null},{"clock_domain":null,"name":"timeout_ms","type":"INTEGER","unit":"MILLISECOND"}],"group_by":["service","api","timeout_ms"],"id":"client_timeout_call","max_gap_lines":0,"max_matches":null,"members":[{"line_pattern":"rpc call (?P<service>[^:\\s]+):(?P<api>\\S+) timeout limit (?P<timeout_ms>\\d+) recv no response","match_mode":"SEARCH"}],"min_matches":0,"observation_policy_ids":["bbbb_default_suppression"],"selectors":[{"field":"service","operator":"EQUALS","value":{"name":"service_name","source":"USER_FACT"}},{"field":"api","operator":"EQUALS","value":{"name":"api_name","source":"USER_FACT"}}],"timestamp_field":null},{"anchor":"client","fields":[{"clock_domain":null,"name":"service","type":"STRING","unit":null},{"clock_domain":null,"name":"call_type","type":"STRING","unit":null},{"clock_domain":null,"name":"request_id","type":"STRING","unit":null},{"clock_domain":null,"name":"timeout_ms","type":"INTEGER","unit":"MILLISECOND"}],"group_by":["request_id"],"id":"client_timeout_detail","max_gap_lines":0,"max_matches":null,"members":[{"line_pattern":"(?P<service>\\S+) rpc (?P<call_type>sync|async) call unsuccess, reqid\\((?P<request_id>\\d+)\\), timeout (?P<timeout_ms>\\d+)","match_mode":"SEARCH"}],"min_matches":0,"observation_policy_ids":["bbbb_default_suppression"],"selectors":[{"field":"service","operator":"EQUALS","value":{"name":"service_name","source":"USER_FACT"}},{"field":"request_id","operator":"EQUALS","value":{"name":"request_id","source":"USER_FACT"}}],"timestamp_field":null},{"anchor":"server","fields":[{"clock_domain":null,"name":"first_service","type":"STRING","unit":null},{"clock_domain":null,"name":"first_api","type":"STRING","unit":null},{"clock_domain":"server_clock","name":"first_end_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":null,"name":"first_cost_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":null,"name":"first_queue_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":null,"name":"first_timeout_ms","type":"INTEGER","unit":"MILLISECOND"},{"clock_domain":null,"name":"second_service","type":"STRING","unit":null},{"clock_domain":null,"name":"second_api","type":"STRING","unit":null},{"clock_domain":"server_clock","name":"second_end_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":null,"name":"second_cost_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":null,"name":"second_queue_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":null,"name":"second_timeout_ms","type":"INTEGER","unit":"MILLISECOND"},{"clock_domain":null,"name":"third_service","type":"STRING","unit":null},{"clock_domain":null,"name":"third_api","type":"STRING","unit":null},{"clock_domain":"server_clock","name":"third_end_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":null,"name":"third_cost_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":null,"name":"third_queue_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":null,"name":"third_timeout_ms","type":"INTEGER","unit":"MILLISECOND"},{"clock_domain":null,"name":"fourth_service","type":"STRING","unit":null},{"clock_domain":null,"name":"fourth_api","type":"STRING","unit":null},{"clock_domain":"server_clock","name":"fourth_end_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":null,"name":"fourth_cost_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":null,"name":"fourth_queue_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":null,"name":"fourth_timeout_ms","type":"INTEGER","unit":"MILLISECOND"},{"clock_domain":null,"name":"fifth_service","type":"STRING","unit":null},{"clock_domain":null,"name":"fifth_api","type":"STRING","unit":null},{"clock_domain":"server_clock","name":"fifth_end_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":null,"name":"fifth_cost_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":null,"name":"fifth_queue_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":null,"name":"fifth_timeout_ms","type":"INTEGER","unit":"MILLISECOND"}],"group_by":["first_service","first_api","first_end_us"],"id":"queue_history","max_gap_lines":1,"max_matches":null,"members":[{"line_pattern":"\\[BBBB\\]The first service:(?P<first_service>[^,]+), api:(?P<first_api>[^,]+), end time:(?P<first_end_us>\\d+), cost time:(?P<first_cost_us>\\d+), queue time:(?P<first_queue_us>\\d+), timeout:(?P<first_timeout_ms>\\d+)","match_mode":"SEARCH"},{"line_pattern":"\\[BBBB\\]The second service:(?P<second_service>[^,]+), api:(?P<second_api>[^,]+), end time:(?P<second_end_us>\\d+), cost time:(?P<second_cost_us>\\d+), queue time:(?P<second_queue_us>\\d+), timeout:(?P<second_timeout_ms>\\d+)","match_mode":"SEARCH"},{"line_pattern":"\\[BBBB\\]The third service:(?P<third_service>[^,]+), api:(?P<third_api>[^,]+), end time:(?P<third_end_us>\\d+), cost time:(?P<third_cost_us>\\d+), queue time:(?P<third_queue_us>\\d+), timeout:(?P<third_timeout_ms>\\d+)","match_mode":"SEARCH"},{"line_pattern":"\\[BBBB\\]The fourth service:(?P<fourth_service>[^,]+), api:(?P<fourth_api>[^,]+), end time:(?P<fourth_end_us>\\d+), cost time:(?P<fourth_cost_us>\\d+), queue time:(?P<fourth_queue_us>\\d+), timeout:(?P<fourth_timeout_ms>\\d+)","match_mode":"SEARCH"},{"line_pattern":"\\[BBBB\\]The fifth service:(?P<fifth_service>[^,]+), api:(?P<fifth_api>[^,]+), end time:(?P<fifth_end_us>\\d+), cost time:(?P<fifth_cost_us>\\d+), queue time:(?P<fifth_queue_us>\\d+), timeout:(?P<fifth_timeout_ms>\\d+)","match_mode":"SEARCH"}],"min_matches":0,"observation_policy_ids":["bbbb_default_suppression","queue_rate_limit"],"selectors":[{"field":"first_service","operator":"EQUALS","value":{"name":"service_name","source":"USER_FACT"}},{"field":"first_api","operator":"EQUALS","value":{"name":"api_name","source":"USER_FACT"}}],"timestamp_field":"first_end_us"},{"anchor":"client","fields":[{"clock_domain":null,"name":"service","type":"STRING","unit":null},{"clock_domain":null,"name":"api","type":"STRING","unit":null},{"clock_domain":null,"name":"request_id","type":"STRING","unit":null},{"clock_domain":null,"name":"timeout_ms","type":"INTEGER","unit":"MILLISECOND"},{"clock_domain":"client_clock","name":"client_send_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":"server_clock","name":"server_recv_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":"server_clock","name":"server_send_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":"client_clock","name":"client_now_us","type":"INTEGER","unit":"MICROSECOND"}],"group_by":["request_id"],"id":"late_response","max_gap_lines":0,"max_matches":null,"members":[{"line_pattern":"late response service:(?P<service>[^,]+), api:(?P<api>[^,]+), reqid:(?P<request_id>\\d+), timeout:(?P<timeout_ms>\\d+), client_send:(?P<client_send_us>\\d+), server_recv:(?P<server_recv_us>\\d+), server_send:(?P<server_send_us>\\d+), client_now:(?P<client_now_us>\\d+)","match_mode":"SEARCH"}],"min_matches":0,"observation_policy_ids":["bbbb_default_suppression"],"selectors":[{"field":"service","operator":"EQUALS","value":{"name":"service_name","source":"USER_FACT"}},{"field":"api","operator":"EQUALS","value":{"name":"api_name","source":"USER_FACT"}},{"field":"request_id","operator":"EQUALS","value":{"name":"request_id","source":"USER_FACT"}}],"timestamp_field":"client_now_us"},{"anchor":"server","fields":[{"clock_domain":null,"name":"service","type":"STRING","unit":null},{"clock_domain":null,"name":"api","type":"STRING","unit":null},{"clock_domain":"server_clock","name":"start_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":"server_clock","name":"end_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":null,"name":"cost_us","type":"INTEGER","unit":"MICROSECOND"},{"clock_domain":null,"name":"timeout_ms","type":"INTEGER","unit":"MILLISECOND"}],"group_by":["service","api","end_us"],"id":"api_complete","max_gap_lines":0,"max_matches":null,"members":[{"line_pattern":"api complete service:(?P<service>[^,]+), api:(?P<api>[^,]+), start:(?P<start_us>\\d+), end:(?P<end_us>\\d+), cost:(?P<cost_us>\\d+), timeout:(?P<timeout_ms>\\d+)","match_mode":"SEARCH"}],"min_matches":0,"observation_policy_ids":["bbbb_default_suppression"],"selectors":[{"field":"service","operator":"EQUALS","value":{"name":"service_name","source":"USER_FACT"}},{"field":"api","operator":"EQUALS","value":{"name":"api_name","source":"USER_FACT"}}],"timestamp_field":"end_us"}],"observation_policies":[{"boundary":"CLOSED_OPEN","id":"bbbb_default_suppression","key_fields":[],"kind":"SUPPRESSION","max_observed":null,"scope":"process_instance","window_ms":75000},{"boundary":"CLOSED_OPEN","id":"queue_rate_limit","key_fields":[],"kind":"RATE_LIMIT","max_observed":1,"scope":"process_instance","window_ms":180000}],"rules":[{"depends_on":[],"description":"目标版本必须包含增强诊断能力。","id":"enhanced_version","kind":"FACT_IN","parameters":{"allowed_values":["enhanced_v2"],"fact_name":"target_version"},"remediation_requirements":[]},{"depends_on":[],"description":"协议必须属于Wiki声明的已知集合。","id":"known_protocol","kind":"FACT_IN","parameters":{"allowed_values":["standard","silent_timeout_detail"],"fact_name":"transport_protocol"},"remediation_requirements":[]},{"depends_on":[],"description":"客户端同步超时消息体已出现。","id":"client_call_present","kind":"EVENT_PRESENT","parameters":{"event":"client_timeout_call"},"remediation_requirements":[]},{"depends_on":[],"description":"客户端请求ID超时消息体已出现。","id":"client_detail_present","kind":"EVENT_PRESENT","parameters":{"event":"client_timeout_detail"},"remediation_requirements":[]},{"depends_on":[],"description":"服务端五行排队历史块已出现。","id":"queue_history_present","kind":"EVENT_PRESENT","parameters":{"event":"queue_history"},"remediation_requirements":[]},{"depends_on":[],"description":"客户端最终收到晚响应。","id":"late_response_present","kind":"EVENT_PRESENT","parameters":{"event":"late_response"},"remediation_requirements":[]},{"depends_on":[],"description":"服务端目标API执行完成记录已出现。","id":"api_complete_present","kind":"EVENT_PRESENT","parameters":{"event":"api_complete"},"remediation_requirements":[]},{"depends_on":["client_call_present","client_detail_present","queue_history_present"],"description":"完整路径的客户端和服务端记录属于同一服务。","id":"complete_service_correlates","kind":"FIELDS_EQUAL","parameters":{"equalities":[{"members":[{"event":"client_timeout_call","field":"service"},{"event":"client_timeout_detail","field":"service"},{"event":"queue_history","field":"first_service"}]}],"quantifier":"EXISTS"},"remediation_requirements":[]},{"depends_on":["client_call_present","queue_history_present"],"description":"完整路径的目标API与排队块首条一致。","id":"complete_api_correlates","kind":"FIELDS_EQUAL","parameters":{"equalities":[{"members":[{"event":"client_timeout_call","field":"api"},{"event":"queue_history","field":"first_api"}]}],"quantifier":"EXISTS"},"remediation_requirements":[]},{"depends_on":["client_call_present","late_response_present","api_complete_present"],"description":"部分路径的超时、晚响应和执行记录属于同一目标。","id":"partial_target_correlates","kind":"FIELDS_EQUAL","parameters":{"equalities":[{"members":[{"event":"client_timeout_call","field":"service"},{"event":"late_response","field":"service"},{"event":"api_complete","field":"service"}]},{"members":[{"event":"client_timeout_call","field":"api"},{"event":"late_response","field":"api"},{"event":"api_complete","field":"api"}]}],"quantifier":"EXISTS"},"remediation_requirements":[]},{"depends_on":["queue_history_present"],"description":"目标API排队加执行时间超过超时阈值。","id":"queue_total_exceeds_timeout","kind":"NUMERIC_COMPARE","parameters":{"clock_tolerance_ms":0,"joins":[],"left":{"kind":"ADD","left":{"event":"queue_history","field":"first_queue_us","kind":"FIELD"},"right":{"event":"queue_history","field":"first_cost_us","kind":"FIELD"}},"operator":"GT","quantifier":"EXISTS","right":{"event":"queue_history","field":"first_timeout_ms","kind":"FIELD"}},"remediation_requirements":[]},{"depends_on":["queue_history_present"],"description":"目标API自身执行时间没有越过超时阈值。","id":"target_execution_within_timeout","kind":"NUMERIC_COMPARE","parameters":{"clock_tolerance_ms":0,"joins":[],"left":{"event":"queue_history","field":"first_cost_us","kind":"FIELD"},"operator":"LTE","quantifier":"EXISTS","right":{"event":"queue_history","field":"first_timeout_ms","kind":"FIELD"}},"remediation_requirements":[]},{"depends_on":["queue_history_present"],"description":"相邻前序API执行时间长于目标API。","id":"prior_api_longer_than_target","kind":"NUMERIC_COMPARE","parameters":{"clock_tolerance_ms":0,"joins":[],"left":{"event":"queue_history","field":"second_cost_us","kind":"FIELD"},"operator":"GT","quantifier":"EXISTS","right":{"event":"queue_history","field":"first_cost_us","kind":"FIELD"}},"remediation_requirements":[]},{"depends_on":["api_complete_present"],"description":"目标API自身执行时长超过超时阈值。","id":"api_duration_exceeds_timeout","kind":"NUMERIC_COMPARE","parameters":{"clock_tolerance_ms":0,"joins":[],"left":{"event":"api_complete","field":"cost_us","kind":"FIELD"},"operator":"GT","quantifier":"EXISTS","right":{"event":"api_complete","field":"timeout_ms","kind":"FIELD"}},"remediation_requirements":[]},{"depends_on":["late_response_present"],"description":"服务端接收时间晚于客户端发送时间。","id":"server_queue_positive","kind":"NUMERIC_COMPARE","parameters":{"clock_tolerance_ms":100,"joins":[],"left":{"kind":"SUBTRACT","left":{"event":"late_response","field":"server_recv_us","kind":"FIELD"},"right":{"event":"late_response","field":"client_send_us","kind":"FIELD"}},"operator":"GT","quantifier":"EXISTS","right":{"kind":"CONST","unit":"MICROSECOND","value":0}},"remediation_requirements":[]},{"depends_on":["late_response_present"],"description":"客户端当前时间晚于服务端发送时间。","id":"client_queue_positive","kind":"NUMERIC_COMPARE","parameters":{"clock_tolerance_ms":100,"joins":[],"left":{"kind":"SUBTRACT","left":{"event":"late_response","field":"client_now_us","kind":"FIELD"},"right":{"event":"late_response","field":"server_send_us","kind":"FIELD"}},"operator":"GT","quantifier":"EXISTS","right":{"kind":"CONST","unit":"MICROSECOND","value":0}},"remediation_requirements":[]},{"depends_on":["late_response_present"],"description":"同一客户端时钟下晚响应总耗时超过deadline。","id":"response_after_deadline","kind":"NUMERIC_COMPARE","parameters":{"clock_tolerance_ms":0,"joins":[],"left":{"kind":"SUBTRACT","left":{"event":"late_response","field":"client_now_us","kind":"FIELD"},"right":{"event":"late_response","field":"client_send_us","kind":"FIELD"}},"operator":"GT","quantifier":"EXISTS","right":{"event":"late_response","field":"timeout_ms","kind":"FIELD"}},"remediation_requirements":[]},{"depends_on":["enhanced_version","known_protocol","complete_service_correlates","complete_api_correlates","queue_total_exceeds_timeout","target_execution_within_timeout"],"description":"两名Agent独立判断服务端排队是否共同导致超时。","id":"queue_contributed_timeout","kind":"SEMANTIC_CAUSALITY","parameters":{"assertion":"同一固定快照中的客户端超时和服务端五行排队块共同支持服务端排队是超时贡献因素。","evidence_events":["client_timeout_call","client_timeout_detail","queue_history"]},"remediation_requirements":[]},{"depends_on":["prior_api_longer_than_target","queue_total_exceeds_timeout"],"description":"两名Agent独立判断前序长API是否造成目标API排队。","id":"upstream_api_caused_queue","kind":"SEMANTIC_CAUSALITY","parameters":{"assertion":"五行历史中紧邻目标调用的前序长API支持共享串行lane中的上游阻塞因素。","evidence_events":["queue_history"]},"remediation_requirements":[]},{"depends_on":["enhanced_version","known_protocol","partial_target_correlates","response_after_deadline"],"description":"两名Agent独立确认该快照只支持部分定位。","id":"partial_snapshot_supported","kind":"SEMANTIC_CAUSALITY","parameters":{"assertion":"超时、晚响应和执行完成正向证据足以排除目标API自身超时，但跨时钟容差和日志抑制使服务端与客户端排队贡献仍未决。","evidence_events":["client_timeout_call","late_response","api_complete"]},"remediation_requirements":[]}],"schema_version":2,"terminal_paths":[{"condition":{"any_of":[{"all_of":[{"result":"PASS","rule_id":"queue_contributed_timeout"},{"result":"PASS","rule_id":"upstream_api_caused_queue"}]}]},"id":"complete_queue_and_upstream","resolution_status":"COMPLETE"},{"condition":{"any_of":[{"all_of":[{"result":"FAIL","rule_id":"api_duration_exceeds_timeout"},{"result":"UNKNOWN","rule_id":"server_queue_positive"},{"result":"UNKNOWN","rule_id":"client_queue_positive"},{"result":"PASS","rule_id":"partial_snapshot_supported"}]}]},"id":"partial_cross_clock_ambiguity","resolution_status":"PARTIAL"},{"condition":{"any_of":[{"all_of":[]}]},"id":"none","resolution_status":"NONE"}]},"version":"5.0.0"}
```
<!-- DIAGNOSIS_SKILL_MANIFEST_V5_END -->

## 范围与角色

只分析用户提供的固定客户端与服务端日志快照；允许完整或部分结论，不从受抑制日志的缺失推导反证。

- `client`：发起调用并接收响应的进程实例
- `server`：在共享串行lane中接收并执行API的进程实例

## Requirements

所有声明均为必需项；空数组表示不添加任何默认参数。
INPUT 只能由 `USER_FACT` 满足，ATTACHMENT 只能由 `READY_ATTACHMENT` 满足。

| 名称 | 类型 | 阶段 | 满足来源 | 补充策略 | 用户提示 | S00 constraints |
| --- | --- | --- | --- | --- | --- | --- |
| `service_name` | INPUT | INITIAL | USER_FACT | MISSING_ONLY | 请提供超时调用的服务名。 | `{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"}` |
| `api_name` | INPUT | INITIAL | USER_FACT | MISSING_ONLY | 请提供超时调用的API名。 | `{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"}` |
| `target_version` | INPUT | INITIAL | USER_FACT | MISSING_ONLY | 请提供目标模块版本分类。 | `{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"}` |
| `transport_protocol` | INPUT | INITIAL | USER_FACT | MISSING_ONLY | 请提供底层通信协议分类。 | `{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"}` |
| `problem_time` | INPUT | INITIAL | USER_FACT | MISSING_ONLY | 请提供毫秒精度UTC问题时间。 | `{"allowed_values":[],"max_utf8_bytes":24,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"}` |
| `client_process` | INPUT | INITIAL | USER_FACT | MISSING_ONLY | 请提供客户端进程名。 | `{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"}` |
| `server_process` | INPUT | INITIAL | USER_FACT | MISSING_ONLY | 请提供服务端进程名。 | `{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"}` |
| `log_archive` | ATTACHMENT | INITIAL | READY_ATTACHMENT | MISSING_ONLY | 请上传Logparse支持的固定日志归档。 | `{"allowed_content_types":["application/gzip","application/zip","application/x-tar"],"max_count":1,"min_count":1}` |
| `request_id` | INPUT | AFTER_LOGPARSE | USER_FACT | MISSING_ONLY | 请提供需要关联的进程内请求ID。 | `{"allowed_values":[],"max_utf8_bytes":64,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"}` |

按声明顺序执行阶段算法：先复用当前快照中有效事实和同名 OPEN requirement；请求当前
阶段全部缺失 INPUT 并返回 NEED_INPUT；INPUT 齐全后才请求该阶段 ATTACHMENT 并返回
NEED_ATTACHMENT。
INITIAL 齐全后才可进入工具/分析；parse 成功后再检查 AFTER_LOGPARSE。缺少后补输入时，
必须提出必要 LOGPARSE Evidence，并把每个需要跨 Job 保留的 Evidence proposal 写入
`state_delta.add_evidence_bindings`：`existing_evidence_id=null`，
`evidence_proposal_key` 等于对应 proposal key。仅写 proposal、Finding 或说明文字不会
触发接收。每个新 Evidence 还必须用 `artifact_proposal_key` 绑定 broker 返回的同一
Outcome `LOGPARSE_RUN` proposal，使平台共同接收 Evidence 与运行产物；完成这些绑定后
才返回 NEED_INPUT。续跑必须复用正式 Evidence 与 LOGPARSE_RUN，并调用 `target-logs`，
禁止再次 `parse-targets`。工具输出只可形成 Evidence、Finding 或 proposed fact，绝不能
满足 USER_FACT requirement。

## Logparse 业务映射

本 Skill 需要 Logparse；有效 product 为 `bbbb`。产品省略时 Runtime 不向上游传
`--product`，但运行 metadata 仍记录 `default`。加载 `logparse-diagnose` 并严格执行其
broker、Canonical request、parse-once、LOGPARSE_RUN 复用及路径安全规则。

形成 LOGPARSE Evidence 时，`workspace_relative_path` 必须为 null；目标日志位置只写在
`locator.relative_path`，并通过同一 Outcome 的 `artifact_proposal_key` 或已有 Artifact
ID 绑定 LOGPARSE_RUN。不得把 LOGPARSE_RUN tree 内路径填成 Evidence 自己的 proposal
路径；任何非 null workspace path 都必须位于该 proposal key 的独立目录下。
构造 broker anchor 时，`label/module/slot/process_name` 必须保持 JSON string 并逐字复制
已解析 binding；即使值看起来像数字也禁止改变 JSON 类型。
新 `LOGPARSE_RUN.metadata` 必须严格且仅含 `tree_manifest_sha256`、
`logparse_version_ref`、`parse_manifest_relative_path`、`source_attachment_id`、
`source_attachment_sha256`、`parse_parameters` 六个字段；`parse_parameters` 仅含有效
`product`。禁止添加 `schema_version`、`format_id`、`description` 或其他通用字段。
Artifact draft 外壳固定为 `artifact_kind=LOGPARSE_RUN`、
`content_type=application/vnd.problem-locator.logparse-run+directory`、
`resource_kind=DIRECTORY`，且 `declared_size`、`declared_sha256` 均为 null；禁止自行猜测
MIME type 或计算 broker 受控树的 size/hash。
`parse-targets` 成功后必须把结果中的 `logparse_run_artifact_draft` 对象逐字段原样放入
`proposed_artifact_drafts`；禁止自行构造、扩展版本字符串或修改任何值。

业务映射的机器事实如下，不得改名、猜值或从日志反向满足 USER_FACT requirement：

```json
{
  "anchors": [
    {
      "label": "client",
      "module": {
        "source": "SKILL_FIXED",
        "value": "bbbb"
      },
      "pid": null,
      "process_name": {
        "name": "client_process",
        "source": "USER_FACT"
      },
      "slot": {
        "source": "SKILL_FIXED",
        "value": "client_slot"
      }
    },
    {
      "label": "server",
      "module": {
        "source": "SKILL_FIXED",
        "value": "bbbb"
      },
      "pid": null,
      "process_name": {
        "name": "server_process",
        "source": "USER_FACT"
      },
      "slot": {
        "source": "SKILL_FIXED",
        "value": "server_slot"
      }
    }
  ],
  "attachment_requirement": "log_archive",
  "problem_time_binding": {
    "name": "problem_time",
    "source": "USER_FACT"
  }
}
```

归档附件只接受平台固定后缀映射：`.gz/.tar.gz/.tgz -> application/gzip`、
`.zip -> application/zip`、`.tar -> application/x-tar`。Content-Type 不是生成参数。


## 机器验证合同

以下 `verification_contract` 是候选结论的机器门禁，不得用叙述、摘要或 Agent 自报结论替代。
逐条提交同一 rule ID 的证据声明；事件由服务端在对应 anchor 的 UTF-8 原始日志中重新组装、
筛选和计数。单行/多行成员、字段类型、单位、clock domain、选择器、基数、关联、时间窗、
数值表达式及终态路径均以合同明示值为准，不存在默认容差。`observation_policies` 声明的
抑制或限流可以让缺失/上界判断成为 UNKNOWN，但不能削弱已观测到的正向证据。

```json
{
  "event_extractors": [
    {
      "anchor": "client",
      "fields": [
        {
          "clock_domain": null,
          "name": "service",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": null,
          "name": "api",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": null,
          "name": "timeout_ms",
          "type": "INTEGER",
          "unit": "MILLISECOND"
        }
      ],
      "group_by": [
        "service",
        "api",
        "timeout_ms"
      ],
      "id": "client_timeout_call",
      "max_gap_lines": 0,
      "max_matches": null,
      "members": [
        {
          "line_pattern": "rpc call (?P<service>[^:\\s]+):(?P<api>\\S+) timeout limit (?P<timeout_ms>\\d+) recv no response",
          "match_mode": "SEARCH"
        }
      ],
      "min_matches": 0,
      "observation_policy_ids": [
        "bbbb_default_suppression"
      ],
      "selectors": [
        {
          "field": "service",
          "operator": "EQUALS",
          "value": {
            "name": "service_name",
            "source": "USER_FACT"
          }
        },
        {
          "field": "api",
          "operator": "EQUALS",
          "value": {
            "name": "api_name",
            "source": "USER_FACT"
          }
        }
      ],
      "timestamp_field": null
    },
    {
      "anchor": "client",
      "fields": [
        {
          "clock_domain": null,
          "name": "service",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": null,
          "name": "call_type",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": null,
          "name": "request_id",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": null,
          "name": "timeout_ms",
          "type": "INTEGER",
          "unit": "MILLISECOND"
        }
      ],
      "group_by": [
        "request_id"
      ],
      "id": "client_timeout_detail",
      "max_gap_lines": 0,
      "max_matches": null,
      "members": [
        {
          "line_pattern": "(?P<service>\\S+) rpc (?P<call_type>sync|async) call unsuccess, reqid\\((?P<request_id>\\d+)\\), timeout (?P<timeout_ms>\\d+)",
          "match_mode": "SEARCH"
        }
      ],
      "min_matches": 0,
      "observation_policy_ids": [
        "bbbb_default_suppression"
      ],
      "selectors": [
        {
          "field": "service",
          "operator": "EQUALS",
          "value": {
            "name": "service_name",
            "source": "USER_FACT"
          }
        },
        {
          "field": "request_id",
          "operator": "EQUALS",
          "value": {
            "name": "request_id",
            "source": "USER_FACT"
          }
        }
      ],
      "timestamp_field": null
    },
    {
      "anchor": "server",
      "fields": [
        {
          "clock_domain": null,
          "name": "first_service",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": null,
          "name": "first_api",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": "server_clock",
          "name": "first_end_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": null,
          "name": "first_cost_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": null,
          "name": "first_queue_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": null,
          "name": "first_timeout_ms",
          "type": "INTEGER",
          "unit": "MILLISECOND"
        },
        {
          "clock_domain": null,
          "name": "second_service",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": null,
          "name": "second_api",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": "server_clock",
          "name": "second_end_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": null,
          "name": "second_cost_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": null,
          "name": "second_queue_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": null,
          "name": "second_timeout_ms",
          "type": "INTEGER",
          "unit": "MILLISECOND"
        },
        {
          "clock_domain": null,
          "name": "third_service",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": null,
          "name": "third_api",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": "server_clock",
          "name": "third_end_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": null,
          "name": "third_cost_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": null,
          "name": "third_queue_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": null,
          "name": "third_timeout_ms",
          "type": "INTEGER",
          "unit": "MILLISECOND"
        },
        {
          "clock_domain": null,
          "name": "fourth_service",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": null,
          "name": "fourth_api",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": "server_clock",
          "name": "fourth_end_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": null,
          "name": "fourth_cost_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": null,
          "name": "fourth_queue_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": null,
          "name": "fourth_timeout_ms",
          "type": "INTEGER",
          "unit": "MILLISECOND"
        },
        {
          "clock_domain": null,
          "name": "fifth_service",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": null,
          "name": "fifth_api",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": "server_clock",
          "name": "fifth_end_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": null,
          "name": "fifth_cost_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": null,
          "name": "fifth_queue_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": null,
          "name": "fifth_timeout_ms",
          "type": "INTEGER",
          "unit": "MILLISECOND"
        }
      ],
      "group_by": [
        "first_service",
        "first_api",
        "first_end_us"
      ],
      "id": "queue_history",
      "max_gap_lines": 1,
      "max_matches": null,
      "members": [
        {
          "line_pattern": "\\[BBBB\\]The first service:(?P<first_service>[^,]+), api:(?P<first_api>[^,]+), end time:(?P<first_end_us>\\d+), cost time:(?P<first_cost_us>\\d+), queue time:(?P<first_queue_us>\\d+), timeout:(?P<first_timeout_ms>\\d+)",
          "match_mode": "SEARCH"
        },
        {
          "line_pattern": "\\[BBBB\\]The second service:(?P<second_service>[^,]+), api:(?P<second_api>[^,]+), end time:(?P<second_end_us>\\d+), cost time:(?P<second_cost_us>\\d+), queue time:(?P<second_queue_us>\\d+), timeout:(?P<second_timeout_ms>\\d+)",
          "match_mode": "SEARCH"
        },
        {
          "line_pattern": "\\[BBBB\\]The third service:(?P<third_service>[^,]+), api:(?P<third_api>[^,]+), end time:(?P<third_end_us>\\d+), cost time:(?P<third_cost_us>\\d+), queue time:(?P<third_queue_us>\\d+), timeout:(?P<third_timeout_ms>\\d+)",
          "match_mode": "SEARCH"
        },
        {
          "line_pattern": "\\[BBBB\\]The fourth service:(?P<fourth_service>[^,]+), api:(?P<fourth_api>[^,]+), end time:(?P<fourth_end_us>\\d+), cost time:(?P<fourth_cost_us>\\d+), queue time:(?P<fourth_queue_us>\\d+), timeout:(?P<fourth_timeout_ms>\\d+)",
          "match_mode": "SEARCH"
        },
        {
          "line_pattern": "\\[BBBB\\]The fifth service:(?P<fifth_service>[^,]+), api:(?P<fifth_api>[^,]+), end time:(?P<fifth_end_us>\\d+), cost time:(?P<fifth_cost_us>\\d+), queue time:(?P<fifth_queue_us>\\d+), timeout:(?P<fifth_timeout_ms>\\d+)",
          "match_mode": "SEARCH"
        }
      ],
      "min_matches": 0,
      "observation_policy_ids": [
        "bbbb_default_suppression",
        "queue_rate_limit"
      ],
      "selectors": [
        {
          "field": "first_service",
          "operator": "EQUALS",
          "value": {
            "name": "service_name",
            "source": "USER_FACT"
          }
        },
        {
          "field": "first_api",
          "operator": "EQUALS",
          "value": {
            "name": "api_name",
            "source": "USER_FACT"
          }
        }
      ],
      "timestamp_field": "first_end_us"
    },
    {
      "anchor": "client",
      "fields": [
        {
          "clock_domain": null,
          "name": "service",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": null,
          "name": "api",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": null,
          "name": "request_id",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": null,
          "name": "timeout_ms",
          "type": "INTEGER",
          "unit": "MILLISECOND"
        },
        {
          "clock_domain": "client_clock",
          "name": "client_send_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": "server_clock",
          "name": "server_recv_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": "server_clock",
          "name": "server_send_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": "client_clock",
          "name": "client_now_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        }
      ],
      "group_by": [
        "request_id"
      ],
      "id": "late_response",
      "max_gap_lines": 0,
      "max_matches": null,
      "members": [
        {
          "line_pattern": "late response service:(?P<service>[^,]+), api:(?P<api>[^,]+), reqid:(?P<request_id>\\d+), timeout:(?P<timeout_ms>\\d+), client_send:(?P<client_send_us>\\d+), server_recv:(?P<server_recv_us>\\d+), server_send:(?P<server_send_us>\\d+), client_now:(?P<client_now_us>\\d+)",
          "match_mode": "SEARCH"
        }
      ],
      "min_matches": 0,
      "observation_policy_ids": [
        "bbbb_default_suppression"
      ],
      "selectors": [
        {
          "field": "service",
          "operator": "EQUALS",
          "value": {
            "name": "service_name",
            "source": "USER_FACT"
          }
        },
        {
          "field": "api",
          "operator": "EQUALS",
          "value": {
            "name": "api_name",
            "source": "USER_FACT"
          }
        },
        {
          "field": "request_id",
          "operator": "EQUALS",
          "value": {
            "name": "request_id",
            "source": "USER_FACT"
          }
        }
      ],
      "timestamp_field": "client_now_us"
    },
    {
      "anchor": "server",
      "fields": [
        {
          "clock_domain": null,
          "name": "service",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": null,
          "name": "api",
          "type": "STRING",
          "unit": null
        },
        {
          "clock_domain": "server_clock",
          "name": "start_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": "server_clock",
          "name": "end_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": null,
          "name": "cost_us",
          "type": "INTEGER",
          "unit": "MICROSECOND"
        },
        {
          "clock_domain": null,
          "name": "timeout_ms",
          "type": "INTEGER",
          "unit": "MILLISECOND"
        }
      ],
      "group_by": [
        "service",
        "api",
        "end_us"
      ],
      "id": "api_complete",
      "max_gap_lines": 0,
      "max_matches": null,
      "members": [
        {
          "line_pattern": "api complete service:(?P<service>[^,]+), api:(?P<api>[^,]+), start:(?P<start_us>\\d+), end:(?P<end_us>\\d+), cost:(?P<cost_us>\\d+), timeout:(?P<timeout_ms>\\d+)",
          "match_mode": "SEARCH"
        }
      ],
      "min_matches": 0,
      "observation_policy_ids": [
        "bbbb_default_suppression"
      ],
      "selectors": [
        {
          "field": "service",
          "operator": "EQUALS",
          "value": {
            "name": "service_name",
            "source": "USER_FACT"
          }
        },
        {
          "field": "api",
          "operator": "EQUALS",
          "value": {
            "name": "api_name",
            "source": "USER_FACT"
          }
        }
      ],
      "timestamp_field": "end_us"
    }
  ],
  "observation_policies": [
    {
      "boundary": "CLOSED_OPEN",
      "id": "bbbb_default_suppression",
      "key_fields": [],
      "kind": "SUPPRESSION",
      "max_observed": null,
      "scope": "process_instance",
      "window_ms": 75000
    },
    {
      "boundary": "CLOSED_OPEN",
      "id": "queue_rate_limit",
      "key_fields": [],
      "kind": "RATE_LIMIT",
      "max_observed": 1,
      "scope": "process_instance",
      "window_ms": 180000
    }
  ],
  "rules": [
    {
      "depends_on": [],
      "description": "目标版本必须包含增强诊断能力。",
      "id": "enhanced_version",
      "kind": "FACT_IN",
      "parameters": {
        "allowed_values": [
          "enhanced_v2"
        ],
        "fact_name": "target_version"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [],
      "description": "协议必须属于Wiki声明的已知集合。",
      "id": "known_protocol",
      "kind": "FACT_IN",
      "parameters": {
        "allowed_values": [
          "standard",
          "silent_timeout_detail"
        ],
        "fact_name": "transport_protocol"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [],
      "description": "客户端同步超时消息体已出现。",
      "id": "client_call_present",
      "kind": "EVENT_PRESENT",
      "parameters": {
        "event": "client_timeout_call"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [],
      "description": "客户端请求ID超时消息体已出现。",
      "id": "client_detail_present",
      "kind": "EVENT_PRESENT",
      "parameters": {
        "event": "client_timeout_detail"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [],
      "description": "服务端五行排队历史块已出现。",
      "id": "queue_history_present",
      "kind": "EVENT_PRESENT",
      "parameters": {
        "event": "queue_history"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [],
      "description": "客户端最终收到晚响应。",
      "id": "late_response_present",
      "kind": "EVENT_PRESENT",
      "parameters": {
        "event": "late_response"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [],
      "description": "服务端目标API执行完成记录已出现。",
      "id": "api_complete_present",
      "kind": "EVENT_PRESENT",
      "parameters": {
        "event": "api_complete"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "client_call_present",
        "client_detail_present",
        "queue_history_present"
      ],
      "description": "完整路径的客户端和服务端记录属于同一服务。",
      "id": "complete_service_correlates",
      "kind": "FIELDS_EQUAL",
      "parameters": {
        "equalities": [
          {
            "members": [
              {
                "event": "client_timeout_call",
                "field": "service"
              },
              {
                "event": "client_timeout_detail",
                "field": "service"
              },
              {
                "event": "queue_history",
                "field": "first_service"
              }
            ]
          }
        ],
        "quantifier": "EXISTS"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "client_call_present",
        "queue_history_present"
      ],
      "description": "完整路径的目标API与排队块首条一致。",
      "id": "complete_api_correlates",
      "kind": "FIELDS_EQUAL",
      "parameters": {
        "equalities": [
          {
            "members": [
              {
                "event": "client_timeout_call",
                "field": "api"
              },
              {
                "event": "queue_history",
                "field": "first_api"
              }
            ]
          }
        ],
        "quantifier": "EXISTS"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "client_call_present",
        "late_response_present",
        "api_complete_present"
      ],
      "description": "部分路径的超时、晚响应和执行记录属于同一目标。",
      "id": "partial_target_correlates",
      "kind": "FIELDS_EQUAL",
      "parameters": {
        "equalities": [
          {
            "members": [
              {
                "event": "client_timeout_call",
                "field": "service"
              },
              {
                "event": "late_response",
                "field": "service"
              },
              {
                "event": "api_complete",
                "field": "service"
              }
            ]
          },
          {
            "members": [
              {
                "event": "client_timeout_call",
                "field": "api"
              },
              {
                "event": "late_response",
                "field": "api"
              },
              {
                "event": "api_complete",
                "field": "api"
              }
            ]
          }
        ],
        "quantifier": "EXISTS"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "queue_history_present"
      ],
      "description": "目标API排队加执行时间超过超时阈值。",
      "id": "queue_total_exceeds_timeout",
      "kind": "NUMERIC_COMPARE",
      "parameters": {
        "clock_tolerance_ms": 0,
        "joins": [],
        "left": {
          "kind": "ADD",
          "left": {
            "event": "queue_history",
            "field": "first_queue_us",
            "kind": "FIELD"
          },
          "right": {
            "event": "queue_history",
            "field": "first_cost_us",
            "kind": "FIELD"
          }
        },
        "operator": "GT",
        "quantifier": "EXISTS",
        "right": {
          "event": "queue_history",
          "field": "first_timeout_ms",
          "kind": "FIELD"
        }
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "queue_history_present"
      ],
      "description": "目标API自身执行时间没有越过超时阈值。",
      "id": "target_execution_within_timeout",
      "kind": "NUMERIC_COMPARE",
      "parameters": {
        "clock_tolerance_ms": 0,
        "joins": [],
        "left": {
          "event": "queue_history",
          "field": "first_cost_us",
          "kind": "FIELD"
        },
        "operator": "LTE",
        "quantifier": "EXISTS",
        "right": {
          "event": "queue_history",
          "field": "first_timeout_ms",
          "kind": "FIELD"
        }
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "queue_history_present"
      ],
      "description": "相邻前序API执行时间长于目标API。",
      "id": "prior_api_longer_than_target",
      "kind": "NUMERIC_COMPARE",
      "parameters": {
        "clock_tolerance_ms": 0,
        "joins": [],
        "left": {
          "event": "queue_history",
          "field": "second_cost_us",
          "kind": "FIELD"
        },
        "operator": "GT",
        "quantifier": "EXISTS",
        "right": {
          "event": "queue_history",
          "field": "first_cost_us",
          "kind": "FIELD"
        }
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "api_complete_present"
      ],
      "description": "目标API自身执行时长超过超时阈值。",
      "id": "api_duration_exceeds_timeout",
      "kind": "NUMERIC_COMPARE",
      "parameters": {
        "clock_tolerance_ms": 0,
        "joins": [],
        "left": {
          "event": "api_complete",
          "field": "cost_us",
          "kind": "FIELD"
        },
        "operator": "GT",
        "quantifier": "EXISTS",
        "right": {
          "event": "api_complete",
          "field": "timeout_ms",
          "kind": "FIELD"
        }
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "late_response_present"
      ],
      "description": "服务端接收时间晚于客户端发送时间。",
      "id": "server_queue_positive",
      "kind": "NUMERIC_COMPARE",
      "parameters": {
        "clock_tolerance_ms": 100,
        "joins": [],
        "left": {
          "kind": "SUBTRACT",
          "left": {
            "event": "late_response",
            "field": "server_recv_us",
            "kind": "FIELD"
          },
          "right": {
            "event": "late_response",
            "field": "client_send_us",
            "kind": "FIELD"
          }
        },
        "operator": "GT",
        "quantifier": "EXISTS",
        "right": {
          "kind": "CONST",
          "unit": "MICROSECOND",
          "value": 0
        }
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "late_response_present"
      ],
      "description": "客户端当前时间晚于服务端发送时间。",
      "id": "client_queue_positive",
      "kind": "NUMERIC_COMPARE",
      "parameters": {
        "clock_tolerance_ms": 100,
        "joins": [],
        "left": {
          "kind": "SUBTRACT",
          "left": {
            "event": "late_response",
            "field": "client_now_us",
            "kind": "FIELD"
          },
          "right": {
            "event": "late_response",
            "field": "server_send_us",
            "kind": "FIELD"
          }
        },
        "operator": "GT",
        "quantifier": "EXISTS",
        "right": {
          "kind": "CONST",
          "unit": "MICROSECOND",
          "value": 0
        }
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "late_response_present"
      ],
      "description": "同一客户端时钟下晚响应总耗时超过deadline。",
      "id": "response_after_deadline",
      "kind": "NUMERIC_COMPARE",
      "parameters": {
        "clock_tolerance_ms": 0,
        "joins": [],
        "left": {
          "kind": "SUBTRACT",
          "left": {
            "event": "late_response",
            "field": "client_now_us",
            "kind": "FIELD"
          },
          "right": {
            "event": "late_response",
            "field": "client_send_us",
            "kind": "FIELD"
          }
        },
        "operator": "GT",
        "quantifier": "EXISTS",
        "right": {
          "event": "late_response",
          "field": "timeout_ms",
          "kind": "FIELD"
        }
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "enhanced_version",
        "known_protocol",
        "complete_service_correlates",
        "complete_api_correlates",
        "queue_total_exceeds_timeout",
        "target_execution_within_timeout"
      ],
      "description": "两名Agent独立判断服务端排队是否共同导致超时。",
      "id": "queue_contributed_timeout",
      "kind": "SEMANTIC_CAUSALITY",
      "parameters": {
        "assertion": "同一固定快照中的客户端超时和服务端五行排队块共同支持服务端排队是超时贡献因素。",
        "evidence_events": [
          "client_timeout_call",
          "client_timeout_detail",
          "queue_history"
        ]
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "prior_api_longer_than_target",
        "queue_total_exceeds_timeout"
      ],
      "description": "两名Agent独立判断前序长API是否造成目标API排队。",
      "id": "upstream_api_caused_queue",
      "kind": "SEMANTIC_CAUSALITY",
      "parameters": {
        "assertion": "五行历史中紧邻目标调用的前序长API支持共享串行lane中的上游阻塞因素。",
        "evidence_events": [
          "queue_history"
        ]
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "enhanced_version",
        "known_protocol",
        "partial_target_correlates",
        "response_after_deadline"
      ],
      "description": "两名Agent独立确认该快照只支持部分定位。",
      "id": "partial_snapshot_supported",
      "kind": "SEMANTIC_CAUSALITY",
      "parameters": {
        "assertion": "超时、晚响应和执行完成正向证据足以排除目标API自身超时，但跨时钟容差和日志抑制使服务端与客户端排队贡献仍未决。",
        "evidence_events": [
          "client_timeout_call",
          "late_response",
          "api_complete"
        ]
      },
      "remediation_requirements": []
    }
  ],
  "schema_version": 2,
  "terminal_paths": [
    {
      "condition": {
        "any_of": [
          {
            "all_of": [
              {
                "result": "PASS",
                "rule_id": "queue_contributed_timeout"
              },
              {
                "result": "PASS",
                "rule_id": "upstream_api_caused_queue"
              }
            ]
          }
        ]
      },
      "id": "complete_queue_and_upstream",
      "resolution_status": "COMPLETE"
    },
    {
      "condition": {
        "any_of": [
          {
            "all_of": [
              {
                "result": "FAIL",
                "rule_id": "api_duration_exceeds_timeout"
              },
              {
                "result": "UNKNOWN",
                "rule_id": "server_queue_positive"
              },
              {
                "result": "UNKNOWN",
                "rule_id": "client_queue_positive"
              },
              {
                "result": "PASS",
                "rule_id": "partial_snapshot_supported"
              }
            ]
          }
        ]
      },
      "id": "partial_cross_clock_ambiguity",
      "resolution_status": "PARTIAL"
    },
    {
      "condition": {
        "any_of": [
          {
            "all_of": []
          }
        ]
      },
      "id": "none",
      "resolution_status": "NONE"
    }
  ]
}
```

## 分析步骤

- 确认版本、协议、两侧进程实例和固定日志快照。
- 以进程实例与请求ID组成复合身份，并提取客户端超时、晚响应、服务端执行和五行排队记录。
- 按显式单位与clock domain重算排队、执行和deadline关系。
- 选择首个成立的COMPLETE、PARTIAL或NONE路径，并结构化列出确认、候选和排除因素。

## 时间特征

- 所有微秒整数时间显式声明clock domain。
- 跨client/server时钟比较使用100毫秒容差；同一时钟使用0容差。
- 固定快照之外不补日志、不等待未来证据。

## 判定规则

- 受SUPPRESSION或RATE_LIMIT影响时，正向日志仍有效，缺失和上界计数只能是UNKNOWN。
- 允许多个因素共同贡献，不强制选择唯一根因。
- 超时不等于取消，结果必须提示后续执行和副作用风险。
- Wiki列出的原因不是穷尽集合，结论只覆盖Skill声明范围。

## 输出要求

- COMPLETE列出服务端排队与上游共享lane长API两个因素及各自证据。
- PARTIAL保留已排除的目标API自身超时，以及服务端和客户端排队两个未决候选。
- 公开原始日志引用、派生值、单位、时钟容差、观测下界、证据缺口和安全说明。

## 假设

- 离线用例的长日志3和4稳定消息体由clarifications.md专门定义，不外推为真实产品格式。
- 五行排队块中first是当前触发调用，second是紧邻的前序调用；真实Wiki若不保证顺序必须重新澄清。

## Candidate 与服务端用户结果

先按声明顺序重算全部规则，再选择第一条匹配的 `terminal_paths`。`COMPLETE` 或 `PARTIAL`
路径可以提出 Candidate，且 `resolution_status` 与 `terminal_path_id` 必须逐字绑定该路径；
`NONE` 路径禁止提出 Candidate。COMPLETE 的每个 completion criterion 都必须为
`SATISFIED`；PARTIAL 必须保留已证实进展，并把未完成 criterion 标成
`PARTIALLY_SATISFIED|UNSATISFIED|UNKNOWN`，不得伪装成完整结论。

Candidate 用 `causal_factors`、`candidate_factors` 和 `excluded_factors` 分别表达已证实因素、
仍待区分因素和已排除因素。每个 factor 必须绑定原始 Evidence 及实际支持它的 rule IDs；
允许多个共同贡献因素，不得为了给出单一根因而丢弃并发贡献或 UNKNOWN。

`supporting_evidence_bindings` 必须去重并保持当前快照 `evidence_refs` 的相对顺序；同一
Outcome 新接收的 Evidence 只按 `state_delta.add_evidence_bindings` 顺序追加。禁止按业务
角色、日志时间或叙述习惯重排。completion mapping 重复这些 binding 时也保持同一顺序；
这是 Coordinator 的固定子序列合同。

Agent 禁止提出或写入 `USER_RESULT`、`USER_RESULT_ARCHIVE`、`diagnosis-result.json`、
`result.zip` 或任何归档请求，也禁止自行调用 zip/tar。Agent draft 只提交 Candidate、
Evidence、rule claims 与合同允许的内部 Artifact proposal。Agent 进程退出后，Runtime
重读权威证据并完成机器验证；DIAGNOSE 草稿通过服务端验证后，服务端立即从已验证的
权威结果生成并持久化用户产物，但仅在独立 Review PASS 后开放公开下载。Agent 不得预先
构造、摘要或替代这些服务端产物。

## 原子交付

最终先写 `output/job_outcome.draft.json`，再把
`problem-locator-seal-outcome-draft` 作为最后一个修改 Workspace 的命令；成功后不得继续
写入 `output/`。sealer 只封存 Agent draft，不生成正式 Outcome、ID、时间或服务端验证结果。
Agent 进程退出后，Runtime 重新读取原始证据并按 manifest 重算机械规则，再生成唯一权威的
`output/job_outcome.json`。stdout/stderr、隐藏思维过程和部分文件不是业务结果。
