# 模型输出的转义处理与格式修复

网站后端调用 Agent HTTP API，诊断运行时从 CLI 的 `stream-json` 事件中读取最终响应。传统 `result` 文本包含两层 JSON：外层是 CLI 事件，内层是模型返回的业务对象。解析外层后，应保留内层原文，不要额外反转义。使用结构化输出时，直接采用成功终态的 `structured_output` 对象；两种入口最终都要通过相同的业务合同检查。

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
| CLI `stream-json` | LF、CRLF；唯一成功终态中的 `structured_output` 对象，或没有该字段时的 `result` 文本；受本阶段输出总量限制 | 不补引号，不重复解码 |
| ROUTE 最终响应 | 一个开头 BOM、包住完整 JSON 的一个代码围栏，以及下述说明文字加唯一最终 JSON | 严格拒绝，包括根 `reason`；新审核协议不再修复语法 |
| Intake 最终响应 | 同上 | 严格拒绝；不改用户事实和来源原文 |
| Specialist 最终响应、Reviewer 草稿文件 | 同上 | 严格拒绝；不改证据、引用、身份或审核判断 |
| 报告追问最终响应 | 完整 `xiaodao.followup.v1` 对象，`request_key` 与当前追问任务一致；字符串内允许未转义的 U+0000–U+001F 控制字符 | 保留字面控制字符；仅按下述规则恢复最后一个 `answer_markdown` 字段中的双引号，不提取对象或自动重试 |
| Generic 报告文件 | 协议头接受 BOM、LF、CRLF | 正文是 Markdown，保留原始字节；不解析或修复正文里的 JSON 示例 |
| 公开 HTTP/MCP 输入、Agent 工具 JSON | 遵循各自规定的输入格式 | 不应用模型输出的修复规则；七个公开 MCP 工具的输入继续保持扁平 |

报告追问兼容 codeagent 等 Claude 兼容 CLI 在内层字符串中直接输出换行、回车或制表符的情况，以 `strict=False` 保留原文。这些字符与其合法 JSON 转义得到相同的字符串。解析后仍检查协议、当前任务、动作、字段类型和大小限制；重复字段和非有限数直接拒绝。此规则仅适用于追问最终响应，外层 `stream-json` 事件仍须是合法的逐行 JSON。

## 追问正文中的未转义双引号

提示词要求所有字符串正确转义，并将 `ANSWER`、`REQUEST_INPUT` 的 `answer_markdown` 放在最后。正常 JSON 可以继续使用任意字段顺序。仅发生 JSON 语法错误时，才尝试以下受限恢复：

1. 严格读取且只读取 `protocol`、`request_key`、`action` 三个唯一控制字段，顺序不限。协议和当前任务必须匹配，动作只能是 `ANSWER` 或 `REQUEST_INPUT`。
2. 接着必须是最后一个字段 `answer_markdown`，其开始引号、末尾闭引号及根对象右花括号均须完整。不能为缺少的内容补字段、分隔符或结束符，也不恢复 `CONTINUE_DIAGNOSIS` 的引号错误。
3. 只在该正文内未转义的 ASCII 双引号前插入反斜杠，保留已有合法转义及字面控制字符。若双引号后跳过空白即遇到 `:`、`,`、`{`、`}`、`[` 或 `]`，则保守拒绝，避免把疑似字段、对象或数组吞进正文。非法转义同样拒绝。
4. 对采用文本重新解析并校验结果合同、Unicode 和正文大小，再保存恢复记录。整个响应仍限 262144 UTF-8 字节，正文仍限 65536 UTF-8 字节；恢复按输入长度线性扫描，不逐个引号尝试解析。

这个规则可恢复普通文字中的 `这里的"超时"并非根因。`，也支持它与字面换行同时出现。未转义的 JSON 示例、引号后紧接英文逗号等情况可能因结构歧义而拒绝；正确转义的相同正文不受影响。不能把任意损坏 JSON 都安全地猜成答案。

中文弯引号和正确转义的 ASCII 双引号本来都合法，不做字符替换。`C:\new` 中的 `\n` 已是合法 JSON 转义，程序无法确定模型想表达换行还是路径，因此不会二次解码或擅自补反斜杠。服务端保留运维配置的 `--json-schema` 参数，不自动给所有阶段添加同一份 schema；结构化输出按下一节处理，仍需与当前阶段的业务合同匹配。

## 结构化输出与工具权限

Claude 兼容 CLI 可以使用 `StructuredOutput` 提交结构化结果；codeagent 的小写名称 `structuredoutput` 同样支持。这两个精确名称只负责格式化输出，不代表读写文件能力。服务端追问的执行前检查、执行后工具审计，以及正式测试的追问轨迹检查均区分这类工具与 `Read`、`Grep`。不放行近似名称、带 MCP 前缀的同名工具、Shell、文件写入或任意其他工具。日志读取仍限于 `inputs`，格式化调用不计为日志检索。

唯一一条成功 `result` 事件含 `structured_output` 时，以该对象为最终业务结果，再用标准 JSON 序列化交给原业务校验器。同行的 `result` 可能只是说明文字，即使包含看似可执行的 JSON 也不采用。没有 `structured_output` 字段时才使用原 `result` 文本；该字段存在但不是对象、包含非法数值或无法编码时直接失败，不回退到另一个答案。协议、当前任务 ID、动作、字段、正文大小和诊断材料仍按原规则校验。

不会把工具参数、工具返回值或 assistant 中间文本当成最终答案。错误终态、多个终态、损坏的事件、重复字段、异常退出或超限输出仍失败，不新增模型调用或自动重试。官方终态字段说明见 [Claude Agent SDK 结构化输出文档](https://code.claude.com/docs/en/agent-sdk/structured-outputs)。本仓库以确定性事件及子进程回放验证兼容路径，不代表局域网 codeagent 的真实模型验收。

## 说明文字与最终 JSON

本节适用于上表的 ROUTE、Intake、Specialist 和 Reviewer，不适用于报告追问的严格结果包。追问的三类结果和页面展示规则见[追问决策说明](website-report-followup.md#内部决策与网页展示分开)。

提示词仍要求模型只输出本阶段的 JSON 对象。`stream-json` 只规定 CLI 事件的外层格式，不能保证 `result` 字符串也符合业务 JSON 的格式要求。最终响应之前的 assistant 文本块仅用于遥测；框架不会拼接这些片段，只使用唯一成功终态，按前述规则选择结构化对象或结果文本。

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

追问引号恢复在本次私有工作区保存 `runtime/followup-response-original.txt`、`runtime/followup-response-effective.txt` 和 `runtime/followup-response-recovery.json`。原文与采用文本按字节保存；后者仍可能含字面控制字符，不能当作严格 JSON 文件。收据记录规则、两份内容的长度与 SHA-256，以及原始 UTF-8 字节中的反斜杠插入位置。`agent.followup.answer_quotes_recovered` 日志只记录追问 ID、规则、哈希和插入数量，不含正文；任何记录文件写入失败都会使本次追问失败，不交付答案、不重试模型。文件沿用追问工作区的保留与清理规则。

历史 ROUTE 修复记录包含 `route-response.raw.txt`、`route-response.effective.txt` 和 `route-json-recovery.json`，分别记录原文、采用文本和插入转义的位置及哈希。新审核协议不再产生这些恢复记录；已有证据不得自动删除。

旧服务端事件 `runtime.route.reason_quotes_recovered` 可用于关联历史恢复记录。现场排查应保留脱敏后的 stdout 原始事件及版本或提交号；调试器展开后的字符串不能用来判断外层事件实际写了几层转义。

审计文件无法保存时，本次任务会中断，不会在缺少修复记录的情况下继续返回结果。网站从会话快照读取具体错误码和失败阶段。只有能明确对应本次中断 Job、且已由服务端确认的错误记录，才会作为失败原因返回；旧任务或迟到结果中的错误不会用于解释本次失败。

## 回归覆盖

`test_model_json.py` 和 `test_route_json.py` 覆盖说明文字加围栏/裸对象、UTF-8 字节范围、多候选与损坏外层、重复键和非有限数，以及解析/扫描次数上界；`test_intake_tolerance.py` 验证参数采用、单次调用、私有审计与写入失败；`test_final_response.py`、`test_output_reader.py` 验证生产入口仍执行各自合同。`test_mixed_model_json_delivery.py` 贯穿网站、Intake、ROUTE、专有诊断、可选 Reviewer、JSON、ZIP 和 SSE 回放，验证 strict/advisory 下均不增加模型调用。

`tests/deterministic/unit/runtime/test_model_output_escaping.py` 固定覆盖脱敏器、日志分流、telemetry 到 ROUTE/Intake/Specialist 的实际字符串路径，区分双引号、单引号、反斜杠、字面 `\n`、真实换行和 Unicode，并覆盖 UTF-8 逐字节分片与 CRLF。Reviewer 从实际草稿文件入口验证，Generic 检查原始正文、实际长度和 SHA-256。

`tests/deterministic/unit/agent/test_followup_result_protocol.py` 覆盖追问字符串内全部 32 个控制字符与合法转义的等值性、继续诊断的文字字段，以及分片 `stream-json` 到 worker 的单次执行和正文保存。`test_followup_quote_recovery.py` 覆盖普通引号、引号与控制字符混合、反斜杠奇偶、UTF-8 插入位置、重复字段、结构歧义与截断拒绝、解析次数，以及实际 worker 保存记录和写入失败不交付。

`test_structured_output.py` 覆盖完整及分片工具事件、唯一成功结构化终态、错误容器不回退、输出上限、异常退出与真实 backend 子进程回放。`test_report_followup_policy.py` 覆盖服务端执行前检查和两种 CLI schema 参数写法；`test_followup_structured_output.py` 与 `test_followup_handoff.py` 覆盖回答、索取材料、正式交接及反馈审核，继续拒绝错误身份和超限正文。正式测试审计器的同类边界由 `report-followup-gate.test.mjs` 覆盖。

反例把未转义引号放入 ROUTE reason、Intake 用户事实、Specialist 引用原文和 Reviewer 判断，确认各入口均严格拒绝。共享模型解析器与 Agent 工具解析器也保持严格拒绝。多个终态由 `test_final_response.py::test_stream_result_is_unique_successful_and_not_in_telemetry` 覆盖；异常退出及输出预算由 `test_agent_backend.py::test_nonzero_exit_is_typed_and_does_not_read_business_output` 和 `test_large_terminal_obeys_process_exit_and_stage_output_budget` 覆盖；非法 ROUTE 字段由 `test_final_response.py::test_invalid_route_response_is_rejected_without_repair` 覆盖。

正式验证须运行 Test Flow Dev 的受影响测试和完整确定性测试（affected + full deterministic），结果以当前源码对应的 `verdict.json` 为准，记录见 `FIXED_ISSUES.md`。模拟测试不能证明公司内网实测完成率，也不能说明调用真实模型的 Release 已通过。
