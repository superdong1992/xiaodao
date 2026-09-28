# 模型输出的转义处理与格式修复

网站后端调用 Agent HTTP API，诊断运行时从 CLI 的 `stream-json` 事件中读取最终响应。这里有两层 JSON：外层是 CLI 事件，内层是模型返回的业务对象。解析外层后，应保留内层原文，不要额外反转义。内层 JSON 按以下规则解析。

例如，业务 JSON 文本是：

```json
{"reason":"使用 \"foo\""}
```

放入外层事件后，`result` 字段应是：

```json
{"result":"{\"reason\":\"使用 \\\"foo\\\"\"}"}
```

解析外层后，内层的 `\"` 必须保留。外层事件合法，不代表其中的业务 JSON 也合法。现有模拟测试未发现遥测（telemetry）处理丢失合法转义，不能据此认定公司现场问题的根因。

## 各入口的处理边界

| 入口 | 支持的格式 | 非法字符串转义处理 |
| --- | --- | --- |
| CLI `stream-json` | LF、CRLF；在本阶段输出总量上限内保留完整的最终响应 | 不补引号，不重复解码 |
| ROUTE 最终响应 | 一个开头 BOM、包住完整 JSON 的一个代码围栏，以及下述说明文字加唯一最终 JSON | 严格拒绝，包括根 `reason`；新审核协议不再修复语法 |
| Intake 最终响应 | 同上 | 严格拒绝；不改用户事实和来源原文 |
| Specialist 最终响应、Reviewer 草稿文件 | 同上 | 严格拒绝；不改证据、引用、身份或审核判断 |
| Generic 报告文件 | 协议头接受 BOM、LF、CRLF | 正文是 Markdown，保留原始字节；不解析或修复正文里的 JSON 示例 |
| 公开 HTTP/MCP 输入、Agent 工具 JSON | 遵循各自规定的输入格式 | 不应用模型输出的修复规则；七个公开 MCP 工具的输入继续保持扁平 |

## 说明文字与最终 JSON

提示词仍要求模型只输出本阶段的 JSON 对象。`stream-json` 只规定 CLI 事件的外层格式，不能保证 `result` 字符串也符合业务 JSON 的格式要求。最终响应之前的 assistant 文本块仅用于遥测；框架不会拼接这些片段，只使用唯一一条成功结束事件中的 `result`。

先严格解析原响应；仅解析失败时，尝试以下两种明确格式：

- Markdown 说明后，给出唯一完整的 `json` 或无语言标签代码围栏。
- Markdown 说明后，在独立一行开始给出唯一完整 JSON 对象，一直延续到响应末尾。

JSON 或代码围栏结束后只能有空白。如果前文含有其他 JSON 候选、多个围栏，或损坏的外层对象、数组，框架不会随意选择最后一个结果，也不会从嵌套结构中摘取对象。提取时会正确处理字符串、转义和嵌套层级，不修补字段、被截断的内容或引用原文。提取结果仍须通过对应阶段的 JSON、schema 和业务检查。

解析失败后，提取扫描最多处理 1 MiB 原文，同时仍受本阶段输出总量上限的约束。纯 JSON 不受这个提取上限影响，不扫描候选，也不产生提取审计记录；ROUTE 仍保存原文和准入审核记录。文字与 JSON 混合的输出只扫描一次，不会遇到每个左花括号就重新解析，也不会增加模型调用或重新启动任务。

## ROUTE 的严格审核协议

四字段审核协议要求完整的 `skill_id`、`reason`、`confidence`、`assessments`。合法展示包装可按前述规则提取，非法 JSON 直接报 `OUTCOME_INVALID`，不修复根 reason、不增加模型调用，也不转通用定位。

解析后还须核对冻结 Skill 身份、字段类型、数值范围、reason 长度和[语义审核准入](route-admission.md)。审核内容完整但低置信、依据不足或存在歧义时，才正常转入通用定位。

旧版三字段协议曾允许仅修复根 reason 的未转义引号，限制为 64 KiB 原文、128 个候选引号和 1024 字符理由。历史帮助器及审计格式保留，但不用于新 ROUTE。Intake、Specialist、Reviewer 和公开输入同样不修补 JSON 字符串；诊断交付策略见[说明](diagnosis-advisory.md)。

## 审计与排查

ROUTE 无论是否需要格式恢复，都保存模型最终响应原文；审核成功解析后保存 `route-admission.json`，区分模型提名与实际路由。通用回退的原始模型理由保留在 `model_reason`，最终 Outcome 的理由说明服务端为何回退。协议错误保留原文和失败 Outcome，不产生成功路由。

说明文字提取成功时，Intake 在本次私有工作区保存 `runtime/intake-response-original.txt`、`intake-response-effective.json` 和 `intake-response-extraction.json`。运行时其他入口复用对应的 `route-response.raw.txt`、`method-diagnosis.raw.txt` 或 `method-review.raw.txt`，另保存 `model-response.extracted.txt` 和 `model-json-extraction.json`。原文和选定片段分别保留，记录包含规则、UTF-8 字节区间、长度、哈希及 `diagnostic_id`；最终采用的对象继续保存于原有草稿/Outcome 记录。Intake 的既有参数处理记录仍区分原始输出和筛选后的决定。

提取日志为 `agent.intake.model_json_extracted` 或 `runtime.model_json.extracted`，只包含关联标识和处理元数据。

历史 ROUTE 修复记录包含 `route-response.raw.txt`、`route-response.effective.txt` 和 `route-json-recovery.json`，分别记录原文、采用文本和插入转义的位置及哈希。新审核协议不再产生这些恢复记录；已有证据不得自动删除。

旧服务端事件 `runtime.route.reason_quotes_recovered` 可用于关联历史恢复记录。现场排查应保留脱敏后的 stdout 原始事件及版本或提交号；调试器展开后的字符串不能用来判断外层事件实际写了几层转义。

审计文件无法保存时，本次任务会中断，不会在缺少修复记录的情况下继续返回结果。网站从会话快照读取具体错误码和失败阶段。只有能明确对应本次中断 Job、且已由服务端确认的错误记录，才会作为失败原因返回；旧任务或迟到结果中的错误不会用于解释本次失败。

## 回归覆盖

`test_model_json.py` 和 `test_route_json.py` 覆盖说明文字加围栏/裸对象、UTF-8 字节范围、多候选与损坏外层、重复键和非有限数，以及解析/扫描次数上界；`test_intake_tolerance.py` 验证参数采用、单次调用、私有审计与写入失败；`test_final_response.py`、`test_output_reader.py` 验证生产入口仍执行各自合同。`test_mixed_model_json_delivery.py` 贯穿网站、Intake、ROUTE、专有诊断、可选 Reviewer、JSON、ZIP 和 SSE 回放，验证 strict/advisory 下均不增加模型调用。

`tests/deterministic/unit/runtime/test_model_output_escaping.py` 固定覆盖脱敏器、日志分流、telemetry 到 ROUTE/Intake/Specialist 的实际字符串路径，区分双引号、单引号、反斜杠、字面 `\n`、真实换行和 Unicode，并覆盖 UTF-8 逐字节分片与 CRLF。Reviewer 从实际草稿文件入口验证，Generic 检查原始正文、实际长度和 SHA-256。

反例把未转义引号放入 ROUTE reason、Intake 用户事实、Specialist 引用原文和 Reviewer 判断，确认各入口均严格拒绝。共享模型解析器与 Agent 工具解析器也保持严格拒绝。多个终态由 `test_final_response.py::test_stream_result_is_unique_successful_and_not_in_telemetry` 覆盖；异常退出及输出预算由 `test_agent_backend.py::test_nonzero_exit_is_typed_and_does_not_read_business_output` 和 `test_large_terminal_obeys_process_exit_and_stage_output_budget` 覆盖；非法 ROUTE 字段由 `test_final_response.py::test_invalid_route_response_is_rejected_without_repair` 覆盖。

正式验证须运行 Test Flow Dev 的受影响测试和完整确定性测试（affected + full deterministic），结果以当前源码对应的 `verdict.json` 为准，记录见 `FIXED_ISSUES.md`。模拟测试不能证明公司内网实测完成率，也不能说明调用真实模型的 Release 已通过。
