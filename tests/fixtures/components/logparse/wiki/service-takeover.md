# 服务接管 RPC 超时定位 Wiki

本 Wiki 是 RPC 专属 E2E Fixture；其中业务字段不得进入通用 output contract 或生成模板。

## GenerationSpec v2

```json
{
  "schema_version": 2,
  "generator_version": "3.0.5",
  "id": "diagnose-service-takeover",
  "version": "3.0.5",
  "capability": "service-takeover",
  "summary": "定位合成服务接管场景中的 RPC 超时",
  "chinese_title": "服务接管 RPC 超时定位",
  "module_name": "compact",
  "problem_scope": "定位调用方到服务方的 RPC 超时，并用两端目标日志验证服务接管链路。",
  "roles": [{"label": "client", "description": "调用方进程"}, {"label": "server", "description": "服务方进程"}],
  "requirements": [
    {"name": "caller_service", "kind": "INPUT", "stage": "INITIAL", "fulfillment_source": "USER_FACT", "prompt": "请提供调用方服务名。", "constraints": {"value_type": "STRING", "min_utf8_bytes": 1, "max_utf8_bytes": 256, "pattern": null, "allowed_values": []}},
    {"name": "server_service", "kind": "INPUT", "stage": "INITIAL", "fulfillment_source": "USER_FACT", "prompt": "请提供服务方服务名。", "constraints": {"value_type": "STRING", "min_utf8_bytes": 1, "max_utf8_bytes": 256, "pattern": null, "allowed_values": []}},
    {"name": "rpc_method", "kind": "INPUT", "stage": "INITIAL", "fulfillment_source": "USER_FACT", "prompt": "请提供超时的 RPC 方法名。", "constraints": {"value_type": "STRING", "min_utf8_bytes": 1, "max_utf8_bytes": 256, "pattern": null, "allowed_values": []}},
    {"name": "problem_time", "kind": "INPUT", "stage": "INITIAL", "fulfillment_source": "USER_FACT", "prompt": "请提供毫秒精度 UTC 问题时间。", "constraints": {"value_type": "STRING", "min_utf8_bytes": 24, "max_utf8_bytes": 24, "pattern": "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z$", "allowed_values": []}},
    {"name": "log_archive", "kind": "ATTACHMENT", "stage": "INITIAL", "fulfillment_source": "READY_ATTACHMENT", "prompt": "请上传 Logparse 支持的日志归档。", "constraints": {"min_count": 1, "max_count": 1}},
    {"name": "order_id", "kind": "INPUT", "stage": "AFTER_LOGPARSE", "fulfillment_source": "USER_FACT", "prompt": "请提供用于两端日志关联的订单号。", "constraints": {"value_type": "STRING", "min_utf8_bytes": 1, "max_utf8_bytes": 256, "pattern": null, "allowed_values": []}}
  ],
  "logparse_plan": {"attachment_requirement": "log_archive", "problem_time_binding": {"source": "USER_FACT", "name": "problem_time"}, "anchors": [{"label": "client", "module": {"source": "SKILL_FIXED", "value": "compact"}, "slot": {"source": "SKILL_FIXED", "value": "slot_1"}, "process_name": {"source": "SKILL_FIXED", "value": "checkout-client"}, "pid": null}, {"label": "server", "module": {"source": "SKILL_FIXED", "value": "compact"}, "slot": {"source": "SKILL_FIXED", "value": "slot_2"}, "process_name": {"source": "SKILL_FIXED", "value": "inventory-server"}, "pid": null}]},
  "time_characteristics": ["以 problem_time 为唯一时间锚点，不推测时区。"],
  "analysis_steps": ["先验证调用端超时证据。", "取得 order_id 后关联服务端接管证据。"],
  "judgement_rules": ["两端 Evidence 同时支持接管链路时才形成候选结论。"],
  "output_requirements": ["说明调用端与服务端证据如何共同支持结论。"],
  "assumptions": ["测试归档是非敏感合成数据。"],
  "requires_logparse": true,
  "logparse_product": "compact"
}
```
