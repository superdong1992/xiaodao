---
name: diagnose-service-takeover
description: "定位合成服务接管场景中的 RPC 超时"
---

# 服务接管 RPC 超时定位

由 `wiki-to-diagnosis-skill` generator `3.0.5` 生成。公共 DIAGNOSE output
contract 只定义通用 Schema、安全、Evidence/Candidate 与原子输出；本文件独占业务
requirements、阶段、工具映射和判定规则。

<!-- DIAGNOSIS_SKILL_MANIFEST_V2_BEGIN -->
```json
{"capability":"service-takeover","entry_document":"SKILL.md","id":"diagnose-service-takeover","logparse_plan":{"anchors":[{"label":"client","module":{"source":"SKILL_FIXED","value":"compact"},"pid":null,"process_name":{"source":"SKILL_FIXED","value":"checkout-client"},"slot":{"source":"SKILL_FIXED","value":"slot_1"}},{"label":"server","module":{"source":"SKILL_FIXED","value":"compact"},"pid":null,"process_name":{"source":"SKILL_FIXED","value":"inventory-server"},"slot":{"source":"SKILL_FIXED","value":"slot_2"}}],"attachment_requirement":"log_archive","problem_time_binding":{"name":"problem_time","source":"USER_FACT"}},"logparse_product":"compact","requirements":[{"constraints":{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"},"fulfillment_source":"USER_FACT","kind":"INPUT","name":"caller_service","prompt":"请提供调用方服务名。","stage":"INITIAL"},{"constraints":{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"},"fulfillment_source":"USER_FACT","kind":"INPUT","name":"server_service","prompt":"请提供服务方服务名。","stage":"INITIAL"},{"constraints":{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"},"fulfillment_source":"USER_FACT","kind":"INPUT","name":"rpc_method","prompt":"请提供超时的 RPC 方法名。","stage":"INITIAL"},{"constraints":{"allowed_values":[],"max_utf8_bytes":24,"min_utf8_bytes":24,"pattern":"^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z$","value_type":"STRING"},"fulfillment_source":"USER_FACT","kind":"INPUT","name":"problem_time","prompt":"请提供毫秒精度 UTC 问题时间。","stage":"INITIAL"},{"constraints":{"allowed_content_types":["application/gzip","application/zip","application/x-tar"],"max_count":1,"min_count":1},"fulfillment_source":"READY_ATTACHMENT","kind":"ATTACHMENT","name":"log_archive","prompt":"请上传 Logparse 支持的日志归档。","stage":"INITIAL"},{"constraints":{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"},"fulfillment_source":"USER_FACT","kind":"INPUT","name":"order_id","prompt":"请提供用于两端日志关联的订单号。","stage":"AFTER_LOGPARSE"}],"requires_logparse":true,"schema_version":2,"summary":"定位合成服务接管场景中的 RPC 超时","tool_bundle_id":"tool-bundle/diagnose","version":"3.0.5"}
```
<!-- DIAGNOSIS_SKILL_MANIFEST_V2_END -->

## 范围与角色

定位调用方到服务方的 RPC 超时，并用两端目标日志验证服务接管链路。

- `client`：调用方进程
- `server`：服务方进程

## Requirements

所有声明均为必需项；空数组表示不添加任何默认参数。
INPUT 只能由 `USER_FACT` 满足，ATTACHMENT 只能由 `READY_ATTACHMENT` 满足。

| 名称 | 类型 | 阶段 | 满足来源 | 用户提示 | S00 constraints |
| --- | --- | --- | --- | --- | --- |
| `caller_service` | INPUT | INITIAL | USER_FACT | 请提供调用方服务名。 | `{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"}` |
| `server_service` | INPUT | INITIAL | USER_FACT | 请提供服务方服务名。 | `{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"}` |
| `rpc_method` | INPUT | INITIAL | USER_FACT | 请提供超时的 RPC 方法名。 | `{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"}` |
| `problem_time` | INPUT | INITIAL | USER_FACT | 请提供毫秒精度 UTC 问题时间。 | `{"allowed_values":[],"max_utf8_bytes":24,"min_utf8_bytes":24,"pattern":"^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z$","value_type":"STRING"}` |
| `log_archive` | ATTACHMENT | INITIAL | READY_ATTACHMENT | 请上传 Logparse 支持的日志归档。 | `{"allowed_content_types":["application/gzip","application/zip","application/x-tar"],"max_count":1,"min_count":1}` |
| `order_id` | INPUT | AFTER_LOGPARSE | USER_FACT | 请提供用于两端日志关联的订单号。 | `{"allowed_values":[],"max_utf8_bytes":256,"min_utf8_bytes":1,"pattern":null,"value_type":"STRING"}` |

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

## Candidate 与用户结果

只有每个 completion criterion 均由 Evidence 支持时才提出 Candidate。形成 Candidate
时，同一 Outcome 必须恰好提出以下两个 FILE Artifact：

`supporting_evidence_bindings` 必须去重并保持当前快照 `evidence_refs` 的相对顺序；同一
Outcome 新接收的 Evidence 只按 `state_delta.add_evidence_bindings` 顺序追加。禁止按业务
角色、日志时间或叙述习惯重排。completion mapping 与 USER_RESULT 重复这些 binding 时
也保持同一顺序；这是 Coordinator 的固定子序列合同。

1. `USER_RESULT`：proposal key `user-result`，name `diagnosis-result.json`，
   content type `application/json`，path `output/proposals/user-result/payload`，metadata
   为 `{"schema_version":1,"format_id":"problem-locator-diagnosis-v1","description":"Diagnosis result"}`。
2. `USER_RESULT_ARCHIVE`：proposal key `user-result-archive`，name `result.zip`，
   content type `application/zip`，path
   `output/proposals/user-result-archive/result.zip`，metadata 使用
   `format_id=problem-locator-result-archive-v1`、
   `user_result_proposal_key=user-result` 和实际 `target_log_count`。

USER_RESULT 必须是 S00 Canonical `UserResultPayload`，并与同一 Candidate seam 逐字一致。
先写 Canonical 请求到 `output/proposals/user-result-archive/request.json`，字段恰好为
`schema_version=1`、`result_text=Candidate statement + 一个 LF` 和
`target_log_paths[]`。日志路径仅列 Candidate
实际绑定的 LOGPARSE Evidence 对应完整目标日志，按 binding 首次出现顺序去重；人工
无日志场景传空数组。构造该数组时，先以 `target-logs` 每项的 `log_path` 建立路径映射，
再严格遍历 Candidate `supporting_evidence_bindings`，解析每条 Evidence 的
`locator.relative_path` 并从映射取对应完整路径；禁止直接复制或沿用 `target-logs`
结果数组的 anchor 顺序，因为它可能与快照 Evidence 顺序不同。然后仅调用一次：

```text
problem-locator-pack-result --request output/proposals/user-result-archive/request.json --result output/proposals/user-result-archive/result.zip
```

禁止自行调用 zip/tar、包含原始上传包、无关日志、parse 目录或完整 LOGPARSE_RUN。
Runtime 会逐字校验 ZIP 中 `result.txt` 与 `target-log-001.log` 等扁平条目。两个结果
Artifact 和 Candidate 必须共同接受，并等待独立 REVIEW PASS 后才可下载。

## 原子交付

最终 `output/job_outcome.json` 使用公共合同给出的 V1 Canonical JSON 和同目录原子替换。
退出前重新读取实际字节，校验 S00 Schema、当前 Job/Case、上述 manifest 声明、proposal
size/hash、结果 Artifact 配对和所有业务阶段规则。stdout/stderr 和部分文件不是业务结果。
