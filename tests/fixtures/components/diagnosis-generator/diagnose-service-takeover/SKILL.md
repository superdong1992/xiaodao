---
name: diagnose-service-takeover
description: "定位合成服务接管场景中的 RPC 超时"
---

# 服务接管 RPC 超时定位

由 `wiki-to-diagnosis-skill` generator `4.0.0` 生成。公共 DIAGNOSE output
contract 只定义通用 Schema、安全、Evidence/Candidate 与原子输出；本文件独占业务
requirements、阶段、工具映射和判定规则。

<!-- DIAGNOSIS_SKILL_MANIFEST_V4_BEGIN -->
```json
{"capability":"service-takeover","deployment_scope":"TEST_ONLY","entry_document":"SKILL.md","id":"diagnose-service-takeover","logparse_plan":{"anchors":[{"label":"client","module":{"source":"SKILL_FIXED","value":"compact"},"pid":null,"process_name":{"source":"SKILL_FIXED","value":"checkout-client"},"slot":{"source":"SKILL_FIXED","value":"slot_1"}},{"label":"server","module":{"source":"SKILL_FIXED","value":"compact"},"pid":null,"process_name":{"source":"SKILL_FIXED","value":"inventory-server"},"slot":{"source":"SKILL_FIXED","value":"slot_2"}}],"attachment_requirement":"log_archive","problem_time_binding":{"name":"problem_time","source":"USER_FACT"}},"logparse_product":"compact","requirements":[{"constraints":{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"},"fulfillment_source":"USER_FACT","kind":"INPUT","name":"caller_service","prompt":"请提供调用方服务名。","stage":"INITIAL","supplement_policy":"MISSING_ONLY"},{"constraints":{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"},"fulfillment_source":"USER_FACT","kind":"INPUT","name":"server_service","prompt":"请提供服务方服务名。","stage":"INITIAL","supplement_policy":"MISSING_ONLY"},{"constraints":{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"},"fulfillment_source":"USER_FACT","kind":"INPUT","name":"rpc_method","prompt":"请提供超时的 RPC 方法名。","stage":"INITIAL","supplement_policy":"MISSING_ONLY"},{"constraints":{"allowed_values":[],"max_utf8_bytes":24,"min_utf8_bytes":24,"pattern":"^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z$","value_type":"STRING"},"fulfillment_source":"USER_FACT","kind":"INPUT","name":"problem_time","prompt":"请提供毫秒精度 UTC 问题时间。","stage":"INITIAL","supplement_policy":"MISSING_ONLY"},{"constraints":{"allowed_content_types":["application/gzip","application/zip","application/x-tar"],"max_count":1,"min_count":1},"fulfillment_source":"READY_ATTACHMENT","kind":"ATTACHMENT","name":"log_archive","prompt":"请上传 Logparse 支持的日志归档。","stage":"INITIAL","supplement_policy":"MISSING_ONLY"},{"constraints":{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"},"fulfillment_source":"USER_FACT","kind":"INPUT","name":"order_id","prompt":"请提供用于两端日志关联的订单号。","stage":"AFTER_LOGPARSE","supplement_policy":"MISSING_ONLY"}],"requires_logparse":true,"schema_version":4,"summary":"定位合成服务接管场景中的 RPC 超时","tool_bundle_id":"tool-bundle/diagnose","verification_contract":{"event_extractors":[{"anchor":"client","field_groups":["caller_service","server_service","rpc_method","order_id"],"id":"client_timeout","line_pattern":"^\\[\\d{4}\\] \\[diagnostic\\|[A-Za-z0-9._/-]+\\] (?P<event_time>\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z) COMPACT (?P<caller_service>\\S+) proc=checkout-client-\\d+ slot 1 cpu \\d+ \\|No\\[\\d+\\] rpc deadline exceeded after \\d+ms server=(?P<server_service>\\S+) method=(?P<rpc_method>\\S+) order_id=(?P<order_id>\\S+)$","match_cardinality":"EXACTLY_ONE","timestamp_format":"RFC3339_MILLIS_UTC","timestamp_group":"event_time"},{"anchor":"server","field_groups":["server_service","rpc_method","order_id"],"id":"server_takeover_accepted","line_pattern":"^\\[\\d{4}\\] \\[diagnostic\\|[A-Za-z0-9._/-]+\\] (?P<event_time>\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z) COMPACT (?P<server_service>\\S+) proc=inventory-server-\\d+ slot 2 cpu \\d+ \\|No\\[\\d+\\] service takeover active; rpc request accepted method=(?P<rpc_method>\\S+) order_id=(?P<order_id>\\S+)$","match_cardinality":"EXACTLY_ONE","timestamp_format":"RFC3339_MILLIS_UTC","timestamp_group":"event_time"},{"anchor":"server","field_groups":["server_service","order_id"],"id":"server_pool_wait_complete","line_pattern":"^\\[\\d{4}\\] \\[diagnostic\\|[A-Za-z0-9._/-]+\\] (?P<event_time>\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z) COMPACT (?P<server_service>\\S+) proc=inventory-server-\\d+ slot 2 cpu \\d+ \\|No\\[\\d+\\] connection pool wait \\d+ms complete order_id=(?P<order_id>\\S+)$","match_cardinality":"EXACTLY_ONE","timestamp_format":"RFC3339_MILLIS_UTC","timestamp_group":"event_time"}],"rules":[{"depends_on":[],"description":"调用端必须出现唯一的 RPC deadline 事件。","id":"client_timeout_present","kind":"EVENT_PRESENT","parameters":{"event":"client_timeout"},"remediation_requirements":[]},{"depends_on":[],"description":"服务端必须出现唯一的接管接受事件。","id":"server_takeover_present","kind":"EVENT_PRESENT","parameters":{"event":"server_takeover_accepted"},"remediation_requirements":[]},{"depends_on":[],"description":"服务端必须出现唯一的连接池等待完成事件。","id":"server_pool_wait_present","kind":"EVENT_PRESENT","parameters":{"event":"server_pool_wait_complete"},"remediation_requirements":[]},{"depends_on":["client_timeout_present"],"description":"调用端超时事件必须落在合成事故窗口内。","id":"client_timeout_in_window","kind":"EVENT_TIME_WINDOW","parameters":{"after_ms":500,"before_ms":3500,"event":"client_timeout","lower_bound":"INCLUSIVE","reference":{"name":"problem_time","source":"USER_FACT"},"upper_bound":"INCLUSIVE"},"remediation_requirements":[]},{"depends_on":["server_takeover_present"],"description":"服务端接管事件必须落在合成事故窗口内。","id":"server_takeover_in_window","kind":"EVENT_TIME_WINDOW","parameters":{"after_ms":500,"before_ms":3500,"event":"server_takeover_accepted","lower_bound":"INCLUSIVE","reference":{"name":"problem_time","source":"USER_FACT"},"upper_bound":"INCLUSIVE"},"remediation_requirements":[]},{"depends_on":["server_pool_wait_present"],"description":"服务端连接池等待事件必须落在合成事故窗口内。","id":"server_pool_wait_in_window","kind":"EVENT_TIME_WINDOW","parameters":{"after_ms":500,"before_ms":3500,"event":"server_pool_wait_complete","lower_bound":"INCLUSIVE","reference":{"name":"problem_time","source":"USER_FACT"},"upper_bound":"INCLUSIVE"},"remediation_requirements":[]},{"depends_on":["client_timeout_present"],"description":"调用端日志服务名必须等于用户事实。","id":"caller_service_matches","kind":"FACT_FIELD_EQUALS","parameters":{"event":"client_timeout","fact_name":"caller_service","field":"caller_service"},"remediation_requirements":[]},{"depends_on":["client_timeout_present"],"description":"调用端日志目标服务名必须等于用户事实。","id":"client_server_service_matches","kind":"FACT_FIELD_EQUALS","parameters":{"event":"client_timeout","fact_name":"server_service","field":"server_service"},"remediation_requirements":[]},{"depends_on":["server_takeover_present"],"description":"服务端日志服务名必须等于用户事实。","id":"server_service_matches","kind":"FACT_FIELD_EQUALS","parameters":{"event":"server_takeover_accepted","fact_name":"server_service","field":"server_service"},"remediation_requirements":[]},{"depends_on":["client_timeout_present"],"description":"调用端 RPC 方法必须等于用户事实。","id":"client_method_matches","kind":"FACT_FIELD_EQUALS","parameters":{"event":"client_timeout","fact_name":"rpc_method","field":"rpc_method"},"remediation_requirements":[]},{"depends_on":["server_takeover_present"],"description":"服务端 RPC 方法必须等于用户事实。","id":"server_method_matches","kind":"FACT_FIELD_EQUALS","parameters":{"event":"server_takeover_accepted","fact_name":"rpc_method","field":"rpc_method"},"remediation_requirements":[]},{"depends_on":["client_timeout_present"],"description":"调用端订单号必须等于用户事实。","id":"client_order_matches","kind":"FACT_FIELD_EQUALS","parameters":{"event":"client_timeout","fact_name":"order_id","field":"order_id"},"remediation_requirements":[]},{"depends_on":["server_takeover_present"],"description":"服务端订单号必须等于用户事实。","id":"server_order_matches","kind":"FACT_FIELD_EQUALS","parameters":{"event":"server_takeover_accepted","fact_name":"order_id","field":"order_id"},"remediation_requirements":[]},{"depends_on":["client_timeout_present","server_takeover_present"],"description":"调用端与服务端都必须有原始事件证据。","id":"required_roles_covered","kind":"ROLE_COVERAGE","parameters":{"coverage":[{"event":"client_timeout","role":"client"},{"event":"server_takeover_accepted","role":"server"}]},"remediation_requirements":[]},{"depends_on":["client_order_matches","server_order_matches","server_pool_wait_present"],"description":"调用端与服务端事件必须属于同一订单。","id":"order_correlates_across_roles","kind":"CROSS_ROLE_CORRELATION","parameters":{"members":[{"event":"client_timeout","field":"order_id"},{"event":"server_takeover_accepted","field":"order_id"},{"event":"server_pool_wait_complete","field":"order_id"}]},"remediation_requirements":[]},{"depends_on":["client_method_matches","server_method_matches"],"description":"调用端与服务端事件必须属于同一 RPC 方法。","id":"method_correlates_across_roles","kind":"CROSS_ROLE_CORRELATION","parameters":{"members":[{"event":"client_timeout","field":"rpc_method"},{"event":"server_takeover_accepted","field":"rpc_method"}]},"remediation_requirements":[]},{"depends_on":["client_server_service_matches","server_service_matches"],"description":"调用端目标服务与服务端身份必须一致。","id":"server_correlates_across_roles","kind":"CROSS_ROLE_CORRELATION","parameters":{"members":[{"event":"client_timeout","field":"server_service"},{"event":"server_takeover_accepted","field":"server_service"}]},"remediation_requirements":[]},{"depends_on":["server_takeover_in_window","server_pool_wait_in_window"],"description":"服务接管接受必须早于连接池等待完成。","id":"takeover_precedes_pool_wait","kind":"EVENT_ORDER","parameters":{"after_event":"server_pool_wait_complete","allow_equal":false,"before_event":"server_takeover_accepted"},"remediation_requirements":[]},{"depends_on":["server_pool_wait_in_window","client_timeout_in_window"],"description":"连接池等待完成不得晚于调用端 deadline。","id":"pool_wait_precedes_timeout","kind":"EVENT_ORDER","parameters":{"after_event":"client_timeout","allow_equal":true,"before_event":"server_pool_wait_complete"},"remediation_requirements":[]},{"depends_on":["required_roles_covered","order_correlates_across_roles","method_correlates_across_roles","server_correlates_across_roles","takeover_precedes_pool_wait","pool_wait_precedes_timeout"],"description":"两名 Agent 必须独立判断接管期间的连接池等待是否导致本次 RPC 超时。","id":"takeover_pool_wait_caused_timeout","kind":"SEMANTIC_CAUSALITY","parameters":{"assertion":"同一服务、RPC 方法和订单的服务接管连接池等待导致调用端在本次事故窗口内超时。","evidence_events":["client_timeout","server_takeover_accepted","server_pool_wait_complete"]},"remediation_requirements":[]}],"schema_version":1},"version":"4.0.0"}
```
<!-- DIAGNOSIS_SKILL_MANIFEST_V4_END -->

## 范围与角色

定位调用方到服务方的 RPC 超时，并用两端目标日志验证服务接管链路。

- `client`：调用方进程
- `server`：服务方进程

## Requirements

所有声明均为必需项；空数组表示不添加任何默认参数。
INPUT 只能由 `USER_FACT` 满足，ATTACHMENT 只能由 `READY_ATTACHMENT` 满足。

| 名称 | 类型 | 阶段 | 满足来源 | 补充策略 | 用户提示 | S00 constraints |
| --- | --- | --- | --- | --- | --- | --- |
| `caller_service` | INPUT | INITIAL | USER_FACT | MISSING_ONLY | 请提供调用方服务名。 | `{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"}` |
| `server_service` | INPUT | INITIAL | USER_FACT | MISSING_ONLY | 请提供服务方服务名。 | `{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"}` |
| `rpc_method` | INPUT | INITIAL | USER_FACT | MISSING_ONLY | 请提供超时的 RPC 方法名。 | `{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"}` |
| `problem_time` | INPUT | INITIAL | USER_FACT | MISSING_ONLY | 请提供毫秒精度 UTC 问题时间。 | `{"allowed_values":[],"max_utf8_bytes":24,"min_utf8_bytes":24,"pattern":"^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z$","value_type":"STRING"}` |
| `log_archive` | ATTACHMENT | INITIAL | READY_ATTACHMENT | MISSING_ONLY | 请上传 Logparse 支持的日志归档。 | `{"allowed_content_types":["application/gzip","application/zip","application/x-tar"],"max_count":1,"min_count":1}` |
| `order_id` | INPUT | AFTER_LOGPARSE | USER_FACT | MISSING_ONLY | 请提供用于两端日志关联的订单号。 | `{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"}` |

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

本 Skill 需要 Logparse；有效 product 为 `compact`。产品省略时 Runtime 不向上游传
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
        "value": "compact"
      },
      "pid": null,
      "process_name": {
        "source": "SKILL_FIXED",
        "value": "checkout-client"
      },
      "slot": {
        "source": "SKILL_FIXED",
        "value": "slot_1"
      }
    },
    {
      "label": "server",
      "module": {
        "source": "SKILL_FIXED",
        "value": "compact"
      },
      "pid": null,
      "process_name": {
        "source": "SKILL_FIXED",
        "value": "inventory-server"
      },
      "slot": {
        "source": "SKILL_FIXED",
        "value": "slot_2"
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

以下 `verification_contract` 是候选结论的机器门禁，不得用叙述、摘要或 Agent 自报结论替代。逐条提交同一 rule ID 的证据声明；事件必须由服务端在对应 anchor 的 UTF-8 原始日志中按整行正则重算。时间窗的毫秒范围和开闭边界均以合同明示值为准，不存在默认窗口。本合同不包含日志抑制、限流或采样语义。

```json
{
  "event_extractors": [
    {
      "anchor": "client",
      "field_groups": [
        "caller_service",
        "server_service",
        "rpc_method",
        "order_id"
      ],
      "id": "client_timeout",
      "line_pattern": "^\\[\\d{4}\\] \\[diagnostic\\|[A-Za-z0-9._/-]+\\] (?P<event_time>\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z) COMPACT (?P<caller_service>\\S+) proc=checkout-client-\\d+ slot 1 cpu \\d+ \\|No\\[\\d+\\] rpc deadline exceeded after \\d+ms server=(?P<server_service>\\S+) method=(?P<rpc_method>\\S+) order_id=(?P<order_id>\\S+)$",
      "match_cardinality": "EXACTLY_ONE",
      "timestamp_format": "RFC3339_MILLIS_UTC",
      "timestamp_group": "event_time"
    },
    {
      "anchor": "server",
      "field_groups": [
        "server_service",
        "rpc_method",
        "order_id"
      ],
      "id": "server_takeover_accepted",
      "line_pattern": "^\\[\\d{4}\\] \\[diagnostic\\|[A-Za-z0-9._/-]+\\] (?P<event_time>\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z) COMPACT (?P<server_service>\\S+) proc=inventory-server-\\d+ slot 2 cpu \\d+ \\|No\\[\\d+\\] service takeover active; rpc request accepted method=(?P<rpc_method>\\S+) order_id=(?P<order_id>\\S+)$",
      "match_cardinality": "EXACTLY_ONE",
      "timestamp_format": "RFC3339_MILLIS_UTC",
      "timestamp_group": "event_time"
    },
    {
      "anchor": "server",
      "field_groups": [
        "server_service",
        "order_id"
      ],
      "id": "server_pool_wait_complete",
      "line_pattern": "^\\[\\d{4}\\] \\[diagnostic\\|[A-Za-z0-9._/-]+\\] (?P<event_time>\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z) COMPACT (?P<server_service>\\S+) proc=inventory-server-\\d+ slot 2 cpu \\d+ \\|No\\[\\d+\\] connection pool wait \\d+ms complete order_id=(?P<order_id>\\S+)$",
      "match_cardinality": "EXACTLY_ONE",
      "timestamp_format": "RFC3339_MILLIS_UTC",
      "timestamp_group": "event_time"
    }
  ],
  "rules": [
    {
      "depends_on": [],
      "description": "调用端必须出现唯一的 RPC deadline 事件。",
      "id": "client_timeout_present",
      "kind": "EVENT_PRESENT",
      "parameters": {
        "event": "client_timeout"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [],
      "description": "服务端必须出现唯一的接管接受事件。",
      "id": "server_takeover_present",
      "kind": "EVENT_PRESENT",
      "parameters": {
        "event": "server_takeover_accepted"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [],
      "description": "服务端必须出现唯一的连接池等待完成事件。",
      "id": "server_pool_wait_present",
      "kind": "EVENT_PRESENT",
      "parameters": {
        "event": "server_pool_wait_complete"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "client_timeout_present"
      ],
      "description": "调用端超时事件必须落在合成事故窗口内。",
      "id": "client_timeout_in_window",
      "kind": "EVENT_TIME_WINDOW",
      "parameters": {
        "after_ms": 500,
        "before_ms": 3500,
        "event": "client_timeout",
        "lower_bound": "INCLUSIVE",
        "reference": {
          "name": "problem_time",
          "source": "USER_FACT"
        },
        "upper_bound": "INCLUSIVE"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "server_takeover_present"
      ],
      "description": "服务端接管事件必须落在合成事故窗口内。",
      "id": "server_takeover_in_window",
      "kind": "EVENT_TIME_WINDOW",
      "parameters": {
        "after_ms": 500,
        "before_ms": 3500,
        "event": "server_takeover_accepted",
        "lower_bound": "INCLUSIVE",
        "reference": {
          "name": "problem_time",
          "source": "USER_FACT"
        },
        "upper_bound": "INCLUSIVE"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "server_pool_wait_present"
      ],
      "description": "服务端连接池等待事件必须落在合成事故窗口内。",
      "id": "server_pool_wait_in_window",
      "kind": "EVENT_TIME_WINDOW",
      "parameters": {
        "after_ms": 500,
        "before_ms": 3500,
        "event": "server_pool_wait_complete",
        "lower_bound": "INCLUSIVE",
        "reference": {
          "name": "problem_time",
          "source": "USER_FACT"
        },
        "upper_bound": "INCLUSIVE"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "client_timeout_present"
      ],
      "description": "调用端日志服务名必须等于用户事实。",
      "id": "caller_service_matches",
      "kind": "FACT_FIELD_EQUALS",
      "parameters": {
        "event": "client_timeout",
        "fact_name": "caller_service",
        "field": "caller_service"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "client_timeout_present"
      ],
      "description": "调用端日志目标服务名必须等于用户事实。",
      "id": "client_server_service_matches",
      "kind": "FACT_FIELD_EQUALS",
      "parameters": {
        "event": "client_timeout",
        "fact_name": "server_service",
        "field": "server_service"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "server_takeover_present"
      ],
      "description": "服务端日志服务名必须等于用户事实。",
      "id": "server_service_matches",
      "kind": "FACT_FIELD_EQUALS",
      "parameters": {
        "event": "server_takeover_accepted",
        "fact_name": "server_service",
        "field": "server_service"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "client_timeout_present"
      ],
      "description": "调用端 RPC 方法必须等于用户事实。",
      "id": "client_method_matches",
      "kind": "FACT_FIELD_EQUALS",
      "parameters": {
        "event": "client_timeout",
        "fact_name": "rpc_method",
        "field": "rpc_method"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "server_takeover_present"
      ],
      "description": "服务端 RPC 方法必须等于用户事实。",
      "id": "server_method_matches",
      "kind": "FACT_FIELD_EQUALS",
      "parameters": {
        "event": "server_takeover_accepted",
        "fact_name": "rpc_method",
        "field": "rpc_method"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "client_timeout_present"
      ],
      "description": "调用端订单号必须等于用户事实。",
      "id": "client_order_matches",
      "kind": "FACT_FIELD_EQUALS",
      "parameters": {
        "event": "client_timeout",
        "fact_name": "order_id",
        "field": "order_id"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "server_takeover_present"
      ],
      "description": "服务端订单号必须等于用户事实。",
      "id": "server_order_matches",
      "kind": "FACT_FIELD_EQUALS",
      "parameters": {
        "event": "server_takeover_accepted",
        "fact_name": "order_id",
        "field": "order_id"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "client_timeout_present",
        "server_takeover_present"
      ],
      "description": "调用端与服务端都必须有原始事件证据。",
      "id": "required_roles_covered",
      "kind": "ROLE_COVERAGE",
      "parameters": {
        "coverage": [
          {
            "event": "client_timeout",
            "role": "client"
          },
          {
            "event": "server_takeover_accepted",
            "role": "server"
          }
        ]
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "client_order_matches",
        "server_order_matches",
        "server_pool_wait_present"
      ],
      "description": "调用端与服务端事件必须属于同一订单。",
      "id": "order_correlates_across_roles",
      "kind": "CROSS_ROLE_CORRELATION",
      "parameters": {
        "members": [
          {
            "event": "client_timeout",
            "field": "order_id"
          },
          {
            "event": "server_takeover_accepted",
            "field": "order_id"
          },
          {
            "event": "server_pool_wait_complete",
            "field": "order_id"
          }
        ]
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "client_method_matches",
        "server_method_matches"
      ],
      "description": "调用端与服务端事件必须属于同一 RPC 方法。",
      "id": "method_correlates_across_roles",
      "kind": "CROSS_ROLE_CORRELATION",
      "parameters": {
        "members": [
          {
            "event": "client_timeout",
            "field": "rpc_method"
          },
          {
            "event": "server_takeover_accepted",
            "field": "rpc_method"
          }
        ]
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "client_server_service_matches",
        "server_service_matches"
      ],
      "description": "调用端目标服务与服务端身份必须一致。",
      "id": "server_correlates_across_roles",
      "kind": "CROSS_ROLE_CORRELATION",
      "parameters": {
        "members": [
          {
            "event": "client_timeout",
            "field": "server_service"
          },
          {
            "event": "server_takeover_accepted",
            "field": "server_service"
          }
        ]
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "server_takeover_in_window",
        "server_pool_wait_in_window"
      ],
      "description": "服务接管接受必须早于连接池等待完成。",
      "id": "takeover_precedes_pool_wait",
      "kind": "EVENT_ORDER",
      "parameters": {
        "after_event": "server_pool_wait_complete",
        "allow_equal": false,
        "before_event": "server_takeover_accepted"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "server_pool_wait_in_window",
        "client_timeout_in_window"
      ],
      "description": "连接池等待完成不得晚于调用端 deadline。",
      "id": "pool_wait_precedes_timeout",
      "kind": "EVENT_ORDER",
      "parameters": {
        "after_event": "client_timeout",
        "allow_equal": true,
        "before_event": "server_pool_wait_complete"
      },
      "remediation_requirements": []
    },
    {
      "depends_on": [
        "required_roles_covered",
        "order_correlates_across_roles",
        "method_correlates_across_roles",
        "server_correlates_across_roles",
        "takeover_precedes_pool_wait",
        "pool_wait_precedes_timeout"
      ],
      "description": "两名 Agent 必须独立判断接管期间的连接池等待是否导致本次 RPC 超时。",
      "id": "takeover_pool_wait_caused_timeout",
      "kind": "SEMANTIC_CAUSALITY",
      "parameters": {
        "assertion": "同一服务、RPC 方法和订单的服务接管连接池等待导致调用端在本次事故窗口内超时。",
        "evidence_events": [
          "client_timeout",
          "server_takeover_accepted",
          "server_pool_wait_complete"
        ]
      },
      "remediation_requirements": []
    }
  ],
  "schema_version": 1
}
```

## 分析步骤

- 先验证调用端超时证据。
- 取得 order_id 后关联服务端接管证据。

## 时间特征

- 以 problem_time 为唯一时间锚点，不推测时区。

## 判定规则

- 两端 Evidence 同时支持接管链路时才形成候选结论。

## 输出要求

- 说明调用端与服务端证据如何共同支持结论。

## 假设

- 测试归档是非敏感合成数据。

## Candidate 与服务端用户结果

只有每个 completion criterion 均由 Evidence 支持时才提出 Candidate。

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
