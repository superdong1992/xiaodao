# 模型输出的转义与有限恢复

网站后端调用 Agent HTTP API，诊断运行时从 CLI 的 `stream-json` 事件中取得最终响应。这里有两层 JSON：外层是 CLI 事件，内层是模型返回的业务对象。每层只解析一次，不能额外反转义。

例如，业务 JSON 文本是：

```json
{"reason":"使用 \"foo\""}
```

放入外层事件后，`result` 字段应是：

```json
{"result":"{\"reason\":\"使用 \\\"foo\\\"\"}"}
```

解析外层后，内层的 `\"` 必须保留。外层事件合法，不代表其中的业务 JSON 也合法。当前合成场景未发现 telemetry 丢失合法转义；这次增强处理的是模型业务 JSON 中漏写转义的有限情形，不据此认定公司现场的根因。

## 各入口的处理边界

| 入口 | 接受的格式兼容 | 非法字符串转义 |
| --- | --- | --- |
| CLI `stream-json` | LF、CRLF；按阶段总输出预算保留完整终态 | 不补引号，不重复解码 |
| ROUTE 最终响应 | 一个开头 BOM、包住完整 JSON 的一个代码围栏 | 仅 `reason` 可按下述规则恢复 |
| Intake 最终响应 | 同上 | 严格拒绝；不改用户事实和来源原文 |
| Specialist 最终响应、Reviewer 草稿文件 | 同上 | 严格拒绝；不改证据、引用、身份或审核判断 |
| Generic 报告文件 | 协议头接受 BOM、LF、CRLF | 正文是 Markdown，保留原始字节；不解析或修复正文里的 JSON 示例 |
| 公开 HTTP/MCP 输入、Agent 工具 JSON | 保持各自的输入合同 | 不套用模型输出恢复；七个公开 MCP 输入继续保持扁平 |

## ROUTE 的恢复规则

先按原规则解析和校验；合法输出不经过修复。仅当业务 JSON 无法解析时，尝试一次本地恢复，不调用模型，不增加执行轮次。

恢复必须能唯一确定 `skill_id`、`reason`、`confidence` 的边界，只在 `reason` 内补必要的双引号转义，保留已有合法转义以及其他字段的原始内容。无法确认边界、出现字段形态的歧义、内容截断或超出恢复预算时，仍返回受控失败。恢复后重新执行完整 ROUTE 校验，包括冻结 Skill 身份、字段类型、数值范围和 `reason` 长度；不补字段，也不猜测路由。

恢复搜索最多处理 64 KiB 原文、检查 128 个未转义引号；`reason` 仍受原有 1024 字符限制。前两个上限只约束失败后的恢复搜索，不影响原本合法的 JSON。已有转义、非法转义和真实控制字符不会被再次转义或替换。

这不是共享 JSON 解析器的能力。Reviewer 的 `reason` 等同名字段也不会自动获得这一例外。Specialist 的顶层 JSON 仍须可解析。当前默认 `advisory` 策略保留未经证据一致性复核的模型判断；`strict` 下关闭 Reviewer 时才按完整发现筛选。两种策略都不修补引用文本。详见[诊断交付策略](diagnosis-advisory.md)。

## 审计与排查

恢复成功后分别保存 `route-response.raw.txt`、`route-response.effective.txt` 和 `route-json-recovery.json`。原始响应保留不变；采用文本保留原有 BOM、围栏和排版，只插入允许的反斜杠。回执记录 Case、Job、ROUTE 阶段、`diagnostic_id`、规则、原始与采用文本的长度和哈希，以及原始 UTF-8 字节中的插入位置。

服务端日志事件 `runtime.route.reason_quotes_recovered` 使用同一个 `diagnostic_id`，只记录关联标识、修改数量和哈希，不输出模型原文。现场排查应保留脱敏后的 stdout 原始事件及版本或提交号；调试器展开后的字符串不能用来判断外层事件实际写了几层转义。

审计文件无法持久化时，本次任务中断，不会带着缺失的恢复记录继续交付结果。网站从会话快照读取具体错误码和失败阶段；只有能与本次中断 Job 唯一关联的权威记录才会成为公开失败原因，不借用旧任务或迟到结果的错误。

## 回归覆盖

`tests/deterministic/unit/runtime/test_model_output_escaping.py` 固定覆盖脱敏器、日志分流、telemetry 到 ROUTE/Intake/Specialist 的实际字符串路径，区分双引号、单引号、反斜杠、字面 `\n`、真实换行和 Unicode，并覆盖 UTF-8 逐字节分片与 CRLF。Reviewer 从实际草稿文件入口验证，Generic 检查原始正文、实际长度和 SHA-256。

反例把未转义引号放入 Intake 用户事实、Specialist 引用原文和 Reviewer 判断，确认 ROUTE 例外不会扩散。共享模型解析器与 Agent 工具解析器也保持严格拒绝。多个终态由 `test_final_response.py::test_stream_result_is_unique_successful_and_not_in_telemetry` 覆盖；异常退出及输出预算由 `test_agent_backend.py::test_nonzero_exit_is_typed_and_does_not_read_business_output` 和 `test_large_terminal_obeys_process_exit_and_stage_output_budget` 覆盖；非法 ROUTE 字段由 `test_final_response.py::test_invalid_route_response_is_rejected_without_repair` 覆盖。

正式结论以当前源码对应的 Test Flow Dev affected + full deterministic `verdict.json` 为准，引用登记在 `FIXED_ISSUES.md`。合成测试不代表公司内网实测完成率，也不代表真实模型 Release 已通过。
