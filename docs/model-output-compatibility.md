# 模型输出的转义处理与格式修复

网站后端调用 Agent HTTP API，诊断运行时从 CLI 的 `stream-json` 事件中读取最终响应。传统 `result` 文本包含两层 JSON：外层是 CLI 事件，内层是模型返回的业务对象。解析外层后，应保留内层原文，不要额外反转义。使用结构化输出时，优先采用成功终态中可用的 `structured_output` 对象，否则读取同一终态的 `result` 文本。各入口只读取实际使用的内容，格式差异不应导致整次诊断失败。

例如，业务 JSON 文本是：

```json
{"reason":"使用 \"foo\""}
```

放入外层事件后，`result` 字段应是：

```json
{"result":"{\"reason\":\"使用 \\\"foo\\\"\"}"}
```

解析外层后，内层的 `\"` 必须保留。外层事件合法，不代表其中的业务 JSON 也合法。现有模拟测试未发现遥测（telemetry）处理丢失合法转义，不能据此认定公司现场问题的根因。

新版报告追问直接进入正式 Generic 诊断，使用 Generic V2 报告合同，不再执行下述旧只读追问的结果包解析或原生模型调用。相关历史格式、审计文件与测试记录保留供兼容和排查，不能作为新流程的执行说明。

## 各入口的处理边界

| 入口 | 支持的格式 | 非法字符串转义处理 |
| --- | --- | --- |
| CLI `stream-json` | LF、CRLF；唯一成功终态中优先读取可用的 `structured_output` 对象，否则读取 `result` 文本；受本阶段输出总量限制 | 不补引号，不重复解码 |
| ROUTE 最终响应 | BOM、代码围栏、行内 JSON、前后说明中的唯一完整对象 | 不猜测损坏字符串；没有可用选择时正常转通用 |
| Intake 最终响应 | 同上 | 严格拒绝；不改用户事实和来源原文 |
| Specialist 最终响应、Reviewer 草稿文件 | 同上 | 严格拒绝；不改证据、引用、身份或审核判断 |
| 历史只读报告追问最终响应 | 完整 `xiaodao.followup.v1` 对象，`request_key` 与当前追问任务一致；字符串内允许未转义的 U+0000–U+001F 控制字符 | 保留字面控制字符；仅按下述规则恢复最后一个 `answer_markdown` 字段中的双引号，不提取对象或自动重试 |
| Generic 与专用 Markdown 报告 | 接受纯正文，状态行兼容 BOM、空白、大小写、换行及明确的外层围栏 | 不解析或修复正文里的 JSON 示例；缺少或未知状态按 `UNRESOLVED` 交付 |
| 公开 HTTP/MCP 输入、Agent 工具 JSON | 遵循各自规定的输入格式 | 不应用模型输出的修复规则；七个公开 MCP 工具的输入继续保持扁平 |

报告追问兼容 codeagent 等 Claude 兼容 CLI 在内层字符串中直接输出换行、回车或制表符的情况，以 `strict=False` 保留原文。这些字符与其合法 JSON 转义得到相同的字符串。解析后仍检查协议、当前任务、动作、字段类型和大小限制；重复字段和非有限数直接拒绝。此规则仅适用于追问最终响应，外层 `stream-json` 事件仍须是合法的逐行 JSON。

## 追问正文中的未转义双引号

本节只描述旧只读追问的历史输出合同；新追问生成正式 Generic 报告，不使用这条恢复路径。

提示词要求所有字符串正确转义，并将 `ANSWER`、`REQUEST_INPUT` 的 `answer_markdown` 放在最后。正常 JSON 可以继续使用任意字段顺序。仅发生 JSON 语法错误时，才尝试以下受限恢复：

1. 严格读取且只读取 `protocol`、`request_key`、`action` 三个唯一控制字段，顺序不限。协议和当前任务必须匹配，动作只能是 `ANSWER` 或 `REQUEST_INPUT`。
2. 接着必须是最后一个字段 `answer_markdown`，其开始引号、末尾闭引号及根对象右花括号均须完整。不能为缺少的内容补字段、分隔符或结束符，也不恢复 `CONTINUE_DIAGNOSIS` 的引号错误。
3. 只在该正文内未转义的 ASCII 双引号前插入反斜杠，保留已有合法转义及字面控制字符。若双引号后跳过空白即遇到 `:`、`,`、`{`、`}`、`[` 或 `]`，则保守拒绝，避免把疑似字段、对象或数组吞进正文。非法转义同样拒绝。
4. 对采用文本重新解析并校验结果合同、Unicode 和正文大小，再保存恢复记录。整个响应仍限 262144 UTF-8 字节，正文仍限 65536 UTF-8 字节；恢复按输入长度线性扫描，不逐个引号尝试解析。

这个规则可恢复普通文字中的 `这里的"超时"并非根因。`，也支持它与字面换行同时出现。未转义的 JSON 示例、引号后紧接英文逗号等情况可能因结构歧义而拒绝；正确转义的相同正文不受影响。不能把任意损坏 JSON 都安全地猜成答案。

中文弯引号和正确转义的 ASCII 双引号本来都合法，不做字符替换。`C:\new` 中的 `\n` 已是合法 JSON 转义，程序无法确定模型想表达换行还是路径，因此不会二次解码或擅自补反斜杠。服务端保留运维配置的 `--json-schema` 参数，不自动给所有阶段添加同一份 schema；结构化输出按下一节处理，仍需与当前阶段的业务合同匹配。

## 结构化输出与工具权限

Claude 兼容 CLI 可以使用 `StructuredOutput` 提交结构化结果；codeagent 的小写名称 `structuredoutput` 同样支持。这两个精确名称只负责格式化输出，不代表读写文件能力。历史只读追问的执行前检查、执行后工具审计，以及正式测试的追问轨迹检查均区分这类工具与 `Read`、`Grep`。该追问策略不放行近似名称、带 MCP 前缀的同名工具、Shell、文件写入或任意其他工具。日志读取仍限于 `inputs`，格式化调用不计为日志检索。

命令生成和执行后检查共用工具权限规则：`none` 不允许文件工具，`read-only` 只允许 `Read`，`read-search` 允许 `Read` 和 `Grep`，`diagnose` 允许 `Read`、`Grep`、`Glob` 和 `Bash`；未知权限值一律拒绝。文档型 v3 和旧 Methods 专用定位都使用 `diagnose`，输入内联时也保留这些工具。自定义 wrapper 必须执行相同策略，并用 stream-json 提供最终响应和完整工具事件。

`diagnose` 中的 `Bash` 不属于只读沙箱，不能据此断言它只能访问 `inputs`。专用定位仍保留只读材料、执行后输入完整性校验和工具调用审计；开放这些工具不改变 Skill 包的脚本、工具或依赖限制。原有 `none`、`read-only`、`read-search` 策略保持不变。历史只读追问与经验纠正的服务端 Hook 显式绑定本次权限，并拒绝重复 JSON 字段、非法数值和异常工具参数；启动时移除配置中的 `--bare`，避免跳过 Hook。客户端不增加 Hook。

工具声明既可能出现在完整 assistant 消息中，也可能出现在顶层或嵌套的流式分片事件中。两种分片格式按相同规则检查，完整调用必须包含对象类型的 `input`，合法分片与完整消息不会重复计数。正式测试审计还要求完整参数和工具结果配对，不能靠只有分片的轨迹宣称日志已被读取。最后一行没有换行时也执行相同检查，处理异常不会抛出到调用方或恢复先前结果。

唯一一条成功 `result` 事件中，可用的 `structured_output` 对象优先作为最终业务结果，序列化后交给当前阶段处理。可选的 `structured_output` 缺失、为 null、不是对象或无法编码时，继续读取同一终态已有的 `result` 文本，不因这个可选字段丢掉正文，也不记录异常或警告。结构化对象可用时，仍不采用同行的说明文字；两个字段都不可用时没有可交付结果。

不会把工具参数、工具返回值或 assistant 中间文本当成最终答案。错误终态、多个终态、损坏的事件、重复字段、异常退出或超限输出仍失败，不新增模型调用或自动重试。官方终态字段说明见 [Claude Agent SDK 结构化输出文档](https://code.claude.com/docs/en/agent-sdk/structured-outputs)。本仓库以确定性事件及子进程回放验证兼容路径，不代表局域网 codeagent 的真实模型验收。

## 说明文字与最终 JSON

本节适用于上表的 ROUTE、Intake、Specialist 和 Reviewer，不适用于报告追问的严格结果包。新版追问与历史输出包的区别见[追问执行说明](website-report-followup.md#内部决策与网页展示分开)。

提示词仍要求模型只输出本阶段的 JSON 对象。`stream-json` 只规定 CLI 事件的外层格式，不能保证 `result` 字符串也符合业务 JSON 的格式要求。最终响应之前的 assistant 文本块仅用于遥测；框架不会拼接这些片段，只使用唯一成功终态，按前述规则选择结构化对象或结果文本。

先解析原响应；仅解析失败时，从展示文本中读取唯一完整的 JSON 对象。对象可以放在说明文字同行或代码围栏中，前后都可以有普通说明。围栏的大小写、语言标签、重复展示围栏或缺少末尾围栏不影响完整 JSON 的读取。JSON 本身仍须可解析，不补写截断内容或改动引用原文。

多个完整对象没有唯一可用结果；损坏外层对象或另一 JSON 容器中的嵌套对象也不能冒充响应。各阶段对无法使用的内容采用自己的正常结果：路由转通用，Methods 保留可用发现或返回信息不足。公开 HTTP/MCP 输入与存储 JSON 不采用这些展示格式规则。

解析失败后，提取扫描最多处理 1 MiB 原文，同时仍受本阶段输出总量上限的约束。纯 JSON 不受这个提取上限影响，不扫描候选，也不产生提取审计记录；ROUTE 仍保存原文和准入审核记录。文字与 JSON 混合的输出只扫描一次，不会遇到每个左花括号就重新解析，也不会增加模型调用或重新启动任务。

## ROUTE 的正常回退

2026-11-13 起，本地召回最多 5 个候选，提示词只要求 `skill_id`、`reason`、`confidence`。服务端读取实际可用字段，`reason` 仅用于展示，置信度接受数字字符串；缺省或无法使用的置信度按 0 处理。空响应、无法解析的 JSON、候选外 Skill 或低于 0.95 时正常转通用，不记录格式异常、不重试路由模型。完全没有召回候选时不调用路由模型。

取消逐条件审核及引文回写；历史 `assessments` 与其他额外字段都忽略。模型仍阅读候选的完整适用与排除条件，再给出选择。召回与选择规则见[路由说明](route-admission.md)，冻结目录、任务身份和发布记录仍由服务端核对。

旧版三字段协议曾允许仅修复根 reason 的未转义引号，限制为 64 KiB 原文、128 个候选引号和 1024 字符理由。历史帮助器及审计格式保留，但不用于新 ROUTE。Intake、Specialist、Reviewer 和公开输入同样不修补 JSON 字符串；诊断交付策略见[说明](diagnosis-advisory.md)。

## 审计与排查

ROUTE 保存可编码为 UTF-8 的模型响应原文和正常 `route-admission.json`，区分模型提名与实际路由；没有可用专用选择时保存正常通用回退结果。不可编码的文本按无可用响应处理，不用替换字符伪造原文字节。可用模型理由保留在 `model_reason`，最终 Outcome 说明实际采用的路由。上述格式差异不产生失败 Outcome、异常或警告日志。

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

`test_structured_output.py` 覆盖完整及分片工具事件、唯一成功结构化终态、可选结构化字段不可用时采用正文、输出上限、异常退出与真实 backend 子进程回放。`test_report_followup_policy.py` 覆盖服务端执行前检查和两种 CLI schema 参数写法；`test_followup_structured_output.py` 与 `test_followup_handoff.py` 覆盖回答、索取材料、正式交接及反馈审核，继续拒绝错误身份和超限正文。正式测试审计器的同类边界由 `report-followup-gate.test.mjs` 覆盖。

未转义字符串不会被猜测成合法 JSON，ROUTE 对这类响应正常转通用，其余需要结构化内容的入口仍不能凭空构造事实或证据。多个终态由 `test_final_response.py::test_stream_result_is_unique_successful_and_not_in_telemetry` 覆盖；异常退出及输出预算由 `test_agent_backend.py::test_nonzero_exit_is_typed_and_does_not_read_business_output` 和 `test_large_terminal_obeys_process_exit_and_stage_output_budget` 覆盖。`test_final_response.py::test_unusable_route_response_uses_generic_without_repair` 验证不可用路由响应正常回退且无异常日志。

## 专项排查范围（2026-11-12）

本轮基于 `8.2.0 / b3e7132` 的当前工作树逐项检查调用点，先以固定输入复现，再调整实际入口。下表覆盖模型最终文本、结构化对象、模型草稿文件及其后续消费链；没有全局放宽公共 DTO 或存储合同。

| 路径 | 本轮处理 |
| --- | --- |
| `agent_telemetry` → Backend | 可选 `structured_output` 不可用时读取同一成功终态的正文，可用结构化对象仍优先；不丢弃已有结果 |
| `model_json` → 各 JSON 消费者 | 删除行首、末尾说明、语言标签和围栏完整性的机械限制；只读取完整对象，不增加异常日志 |
| ROUTE → 专用或通用 | 缺字段、无效选择和无法解析的输出正常转通用；保留冻结目录及专用适用性规则 |
| `skill_direct` / `document_runtime` | 共用报告正文读取；缺状态不拒绝正文，普通代码块保留 |
| `generic_locator` | V2 与专用报告共用读取；双版本结果优先 V2，V1 不再禁止普通代码块和标记示例 |
| Methods V1 / advisory / selection | 删除额外字段、可选缺项、重复字符串和顶层状态机械一致性的拒绝；逐项采用可用发现 |
| Methods Reviewer / V2 evaluation | 按已知证据或计划 ID 对齐，恢复服务端顺序；重复引用去重，无法使用的条目按证据不足或 `UNKNOWN` 处理 |
| Intake / 旧 continuation 草稿 | 只读取当前意图使用的字段，可选项使用默认值；删除类型化决策的重复解析 |
| Memory 提炼、纠正、归因、审核 | 忽略无关字段、去除重复索引和重复卡片解析；`null`、空卡、没有可用步骤或拒绝采纳均正常结束，不记录失败事件、不入库 |
| 当前报告追问 | 直接派发 Generic，复用报告读取；历史只读响应协议保留兼容，不当作当前主链 |
| Logparse 请求与输出、封印及发布 | 当前主要是服务端生成的工具请求、外部程序清单和资源绑定；文件路径、归属、哈希、发布原子性不是冗余模型内容校验，保留 |

可用事实和证据仍须属于本次输入；本轮不会补造用户事实、放行目录外 Skill、跳过工具授权或放松文件访问边界。结果内容差异直接按正常流程处理，不为这些差异新增异常或警告记录，不增加模型重试。日常专项与完整 Test Flow 的结论分别记录在 `TODO.md`、`FIXED_ISSUES.md`。

## 非必要字段缺省复核（2026-11-13）

在 `46f9f96` 继续按真实读取入口逐字段复核，补齐两处遗漏：

- Generic 旧 V1 报告仍要求 `STATUS` 行。现在缺少状态或状态无法识别时按 `UNRESOLVED` 交付，保留 `CONCLUSION` 与 `ROOT_CAUSE_ANALYSIS` 两段正文，不产生 `OUTCOME_INVALID` 或失败事件。标准输出仍使用 V2 Markdown，Generic 输出合同资源版本为 `2.1.0`。
- 经验纠错中，模型没有给出可采用的经验或必要证据时正常不入库；不再因省略空字段记录 `memory.extraction.failed`。`UNRELATED` 可省略空的 `evidence_indexes`；不涉及局部适用范围时，`scope_terms` 可省略或为 null。卡片 ID 和哈希可省略，由服务端绑定本次实际使用的经验；明确返回其他卡片身份时仍不得采用。

经验引用的文件哈希可从已固定清单取得。无法定位原文的引用缺项，以及无法确定关系或局部范围的审核缺项，按整次不采纳处理，不重排引用索引、不修改旧经验。具体规则见[经验处理说明](generic-feedback-memory.md)。

Methods 的 strict/advisory Specialist、Reviewer 文件读取和内部 DTO 转换，以及保留的 V2 读取路径，逐字段省略均按可用证据返回结果或信息不足。当前报告追问使用 Generic，不重新启用历史只读输出协议。正常决策审计继续保留；缺省字段不产生异常或警告，不触发模型重试。

正式验证须运行 Test Flow Dev 的受影响测试和完整确定性测试（affected + full deterministic），结果以当前源码对应的 `verdict.json` 为准，记录见 `FIXED_ISSUES.md`。模拟测试不能证明公司内网实测完成率，也不能说明调用真实模型的 Release 已通过。
