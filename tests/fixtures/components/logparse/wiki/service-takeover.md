# 服务接管 RPC 超时定位 Wiki

本 Wiki 是 RPC 专属 E2E Fixture；其中业务字段不得进入通用 output contract 或生成模板。

## GenerationSpec v3

```json
{
  "schema_version": 3,
  "generator_version": "3.1.1",
  "id": "diagnose-service-takeover",
  "version": "3.1.1",
  "capability": "service-takeover",
  "summary": "定位合成服务接管场景中的 RPC 超时",
  "chinese_title": "服务接管 RPC 超时定位",
  "module_name": "compact",
  "problem_scope": "定位调用方到服务方的 RPC 超时，并用两端目标日志验证服务接管链路。",
  "roles": [
    {"label": "client", "description": "调用方进程"},
    {"label": "server", "description": "服务方进程"}
  ],
  "requirements": [
    {"name": "caller_service", "kind": "INPUT", "stage": "INITIAL", "fulfillment_source": "USER_FACT", "prompt": "请提供调用方服务名。", "constraints": {"value_type": "STRING", "min_utf8_bytes": 1, "max_utf8_bytes": 256, "pattern": null, "allowed_values": []}, "supplement_policy": "MISSING_ONLY"},
    {"name": "server_service", "kind": "INPUT", "stage": "INITIAL", "fulfillment_source": "USER_FACT", "prompt": "请提供服务方服务名。", "constraints": {"value_type": "STRING", "min_utf8_bytes": 1, "max_utf8_bytes": 256, "pattern": null, "allowed_values": []}, "supplement_policy": "MISSING_ONLY"},
    {"name": "rpc_method", "kind": "INPUT", "stage": "INITIAL", "fulfillment_source": "USER_FACT", "prompt": "请提供超时的 RPC 方法名。", "constraints": {"value_type": "STRING", "min_utf8_bytes": 1, "max_utf8_bytes": 256, "pattern": null, "allowed_values": []}, "supplement_policy": "MISSING_ONLY"},
    {"name": "problem_time", "kind": "INPUT", "stage": "INITIAL", "fulfillment_source": "USER_FACT", "prompt": "请提供毫秒精度 UTC 问题时间。", "constraints": {"value_type": "STRING", "min_utf8_bytes": 24, "max_utf8_bytes": 24, "pattern": "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z$", "allowed_values": []}, "supplement_policy": "MISSING_ONLY"},
    {"name": "log_archive", "kind": "ATTACHMENT", "stage": "INITIAL", "fulfillment_source": "READY_ATTACHMENT", "prompt": "请上传 Logparse 支持的日志归档。", "constraints": {"min_count": 1, "max_count": 1}, "supplement_policy": "MISSING_ONLY"},
    {"name": "order_id", "kind": "INPUT", "stage": "AFTER_LOGPARSE", "fulfillment_source": "USER_FACT", "prompt": "请提供用于两端日志关联的订单号。", "constraints": {"value_type": "STRING", "min_utf8_bytes": 1, "max_utf8_bytes": 256, "pattern": null, "allowed_values": []}, "supplement_policy": "MISSING_ONLY"}
  ],
  "logparse_plan": {
    "attachment_requirement": "log_archive",
    "problem_time_binding": {"source": "USER_FACT", "name": "problem_time"},
    "anchors": [
      {"label": "client", "module": {"source": "SKILL_FIXED", "value": "compact"}, "slot": {"source": "SKILL_FIXED", "value": "slot_1"}, "process_name": {"source": "SKILL_FIXED", "value": "checkout-client"}, "pid": null},
      {"label": "server", "module": {"source": "SKILL_FIXED", "value": "compact"}, "slot": {"source": "SKILL_FIXED", "value": "slot_2"}, "process_name": {"source": "SKILL_FIXED", "value": "inventory-server"}, "pid": null}
    ]
  },
  "verification_contract": {
    "schema_version": 1,
    "event_extractors": [
      {
        "id": "client_timeout",
        "anchor": "client",
        "line_pattern": "^(?P<event_time>\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z) COMPACT (?P<caller_service>\\S+) proc=checkout-client-\\d+ slot 1 cpu \\d+ \\|No\\[\\d+\\] rpc deadline exceeded after \\d+ms server=(?P<server_service>\\S+) method=(?P<rpc_method>\\S+) order_id=(?P<order_id>\\S+)$",
        "timestamp_group": "event_time",
        "timestamp_format": "RFC3339_MILLIS_UTC",
        "field_groups": ["caller_service", "server_service", "rpc_method", "order_id"],
        "match_cardinality": "EXACTLY_ONE"
      },
      {
        "id": "server_takeover_accepted",
        "anchor": "server",
        "line_pattern": "^(?P<event_time>\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z) COMPACT (?P<server_service>\\S+) proc=inventory-server-\\d+ slot 2 cpu \\d+ \\|No\\[\\d+\\] service takeover active; rpc request accepted method=(?P<rpc_method>\\S+) order_id=(?P<order_id>\\S+)$",
        "timestamp_group": "event_time",
        "timestamp_format": "RFC3339_MILLIS_UTC",
        "field_groups": ["server_service", "rpc_method", "order_id"],
        "match_cardinality": "EXACTLY_ONE"
      },
      {
        "id": "server_pool_wait_complete",
        "anchor": "server",
        "line_pattern": "^(?P<event_time>\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z) COMPACT (?P<server_service>\\S+) proc=inventory-server-\\d+ slot 2 cpu \\d+ \\|No\\[\\d+\\] connection pool wait \\d+ms complete order_id=(?P<order_id>\\S+)$",
        "timestamp_group": "event_time",
        "timestamp_format": "RFC3339_MILLIS_UTC",
        "field_groups": ["server_service", "order_id"],
        "match_cardinality": "EXACTLY_ONE"
      }
    ],
    "rules": [
      {"id": "client_timeout_present", "kind": "EVENT_PRESENT", "description": "调用端必须出现唯一的 RPC deadline 事件。", "depends_on": [], "remediation_requirements": [], "parameters": {"event": "client_timeout"}},
      {"id": "server_takeover_present", "kind": "EVENT_PRESENT", "description": "服务端必须出现唯一的接管接受事件。", "depends_on": [], "remediation_requirements": [], "parameters": {"event": "server_takeover_accepted"}},
      {"id": "server_pool_wait_present", "kind": "EVENT_PRESENT", "description": "服务端必须出现唯一的连接池等待完成事件。", "depends_on": [], "remediation_requirements": [], "parameters": {"event": "server_pool_wait_complete"}},
      {"id": "client_timeout_in_window", "kind": "EVENT_TIME_WINDOW", "description": "调用端超时事件必须落在合成事故窗口内。", "depends_on": ["client_timeout_present"], "remediation_requirements": [], "parameters": {"event": "client_timeout", "reference": {"source": "USER_FACT", "name": "problem_time"}, "before_ms": 3500, "after_ms": 500, "lower_bound": "INCLUSIVE", "upper_bound": "INCLUSIVE"}},
      {"id": "server_takeover_in_window", "kind": "EVENT_TIME_WINDOW", "description": "服务端接管事件必须落在合成事故窗口内。", "depends_on": ["server_takeover_present"], "remediation_requirements": [], "parameters": {"event": "server_takeover_accepted", "reference": {"source": "USER_FACT", "name": "problem_time"}, "before_ms": 3500, "after_ms": 500, "lower_bound": "INCLUSIVE", "upper_bound": "INCLUSIVE"}},
      {"id": "server_pool_wait_in_window", "kind": "EVENT_TIME_WINDOW", "description": "服务端连接池等待事件必须落在合成事故窗口内。", "depends_on": ["server_pool_wait_present"], "remediation_requirements": [], "parameters": {"event": "server_pool_wait_complete", "reference": {"source": "USER_FACT", "name": "problem_time"}, "before_ms": 3500, "after_ms": 500, "lower_bound": "INCLUSIVE", "upper_bound": "INCLUSIVE"}},
      {"id": "caller_service_matches", "kind": "FACT_FIELD_EQUALS", "description": "调用端日志服务名必须等于用户事实。", "depends_on": ["client_timeout_present"], "remediation_requirements": [], "parameters": {"event": "client_timeout", "field": "caller_service", "fact_name": "caller_service"}},
      {"id": "client_server_service_matches", "kind": "FACT_FIELD_EQUALS", "description": "调用端日志目标服务名必须等于用户事实。", "depends_on": ["client_timeout_present"], "remediation_requirements": [], "parameters": {"event": "client_timeout", "field": "server_service", "fact_name": "server_service"}},
      {"id": "server_service_matches", "kind": "FACT_FIELD_EQUALS", "description": "服务端日志服务名必须等于用户事实。", "depends_on": ["server_takeover_present"], "remediation_requirements": [], "parameters": {"event": "server_takeover_accepted", "field": "server_service", "fact_name": "server_service"}},
      {"id": "client_method_matches", "kind": "FACT_FIELD_EQUALS", "description": "调用端 RPC 方法必须等于用户事实。", "depends_on": ["client_timeout_present"], "remediation_requirements": [], "parameters": {"event": "client_timeout", "field": "rpc_method", "fact_name": "rpc_method"}},
      {"id": "server_method_matches", "kind": "FACT_FIELD_EQUALS", "description": "服务端 RPC 方法必须等于用户事实。", "depends_on": ["server_takeover_present"], "remediation_requirements": [], "parameters": {"event": "server_takeover_accepted", "field": "rpc_method", "fact_name": "rpc_method"}},
      {"id": "client_order_matches", "kind": "FACT_FIELD_EQUALS", "description": "调用端订单号必须等于用户事实。", "depends_on": ["client_timeout_present"], "remediation_requirements": [], "parameters": {"event": "client_timeout", "field": "order_id", "fact_name": "order_id"}},
      {"id": "server_order_matches", "kind": "FACT_FIELD_EQUALS", "description": "服务端订单号必须等于用户事实。", "depends_on": ["server_takeover_present"], "remediation_requirements": [], "parameters": {"event": "server_takeover_accepted", "field": "order_id", "fact_name": "order_id"}},
      {"id": "required_roles_covered", "kind": "ROLE_COVERAGE", "description": "调用端与服务端都必须有原始事件证据。", "depends_on": ["client_timeout_present", "server_takeover_present"], "remediation_requirements": [], "parameters": {"coverage": [{"role": "client", "event": "client_timeout"}, {"role": "server", "event": "server_takeover_accepted"}]}},
      {"id": "order_correlates_across_roles", "kind": "CROSS_ROLE_CORRELATION", "description": "调用端与服务端事件必须属于同一订单。", "depends_on": ["client_order_matches", "server_order_matches", "server_pool_wait_present"], "remediation_requirements": [], "parameters": {"members": [{"event": "client_timeout", "field": "order_id"}, {"event": "server_takeover_accepted", "field": "order_id"}, {"event": "server_pool_wait_complete", "field": "order_id"}]}},
      {"id": "method_correlates_across_roles", "kind": "CROSS_ROLE_CORRELATION", "description": "调用端与服务端事件必须属于同一 RPC 方法。", "depends_on": ["client_method_matches", "server_method_matches"], "remediation_requirements": [], "parameters": {"members": [{"event": "client_timeout", "field": "rpc_method"}, {"event": "server_takeover_accepted", "field": "rpc_method"}]}},
      {"id": "server_correlates_across_roles", "kind": "CROSS_ROLE_CORRELATION", "description": "调用端目标服务与服务端身份必须一致。", "depends_on": ["client_server_service_matches", "server_service_matches"], "remediation_requirements": [], "parameters": {"members": [{"event": "client_timeout", "field": "server_service"}, {"event": "server_takeover_accepted", "field": "server_service"}]}},
      {"id": "takeover_precedes_pool_wait", "kind": "EVENT_ORDER", "description": "服务接管接受必须早于连接池等待完成。", "depends_on": ["server_takeover_in_window", "server_pool_wait_in_window"], "remediation_requirements": [], "parameters": {"before_event": "server_takeover_accepted", "after_event": "server_pool_wait_complete", "allow_equal": false}},
      {"id": "pool_wait_precedes_timeout", "kind": "EVENT_ORDER", "description": "连接池等待完成不得晚于调用端 deadline。", "depends_on": ["server_pool_wait_in_window", "client_timeout_in_window"], "remediation_requirements": [], "parameters": {"before_event": "server_pool_wait_complete", "after_event": "client_timeout", "allow_equal": true}},
      {"id": "takeover_pool_wait_caused_timeout", "kind": "SEMANTIC_CAUSALITY", "description": "两名 Agent 必须独立判断接管期间的连接池等待是否导致本次 RPC 超时。", "depends_on": ["required_roles_covered", "order_correlates_across_roles", "method_correlates_across_roles", "server_correlates_across_roles", "takeover_precedes_pool_wait", "pool_wait_precedes_timeout"], "remediation_requirements": [], "parameters": {"assertion": "同一服务、RPC 方法和订单的服务接管连接池等待导致调用端在本次事故窗口内超时。", "evidence_events": ["client_timeout", "server_takeover_accepted", "server_pool_wait_complete"]}}
    ]
  },
  "time_characteristics": ["以 problem_time 为唯一时间锚点，不推测时区。"],
  "analysis_steps": ["先验证调用端超时证据。", "取得 order_id 后关联服务端接管证据。"],
  "judgement_rules": ["两端 Evidence 同时支持接管链路时才形成候选结论。"],
  "output_requirements": ["说明调用端与服务端证据如何共同支持结论。"],
  "assumptions": ["测试归档是非敏感合成数据。"],
  "requires_logparse": true,
  "logparse_product": "compact"
}
```
