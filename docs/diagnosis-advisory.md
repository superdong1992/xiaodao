# 诊断交付策略

网站请求依次经过网站后端、Agent HTTP API、共享应用服务、调度器和运行时，最后生成报告及归档。MCP 也接入同一个应用服务。以下策略同时适用于 Web 和 MCP 发起的 Methods 诊断。

## 当前默认行为

`METHODS_EVIDENCE_VALIDATION=off` 是生产环境的默认配置，服务在启动时读取该值。在调用模型前，服务会先用 Logparse 处理日志、保存目标日志快照，再扫描日志标记（marker）并加载匹配的方法卡，准备诊断所需的输入。方法卡是 Skill 内独立保存的诊断方法文档，包含适用条件、日志线索和判断步骤。

Skill 完成诊断后，框架直接返回其 Markdown 正文。模型在首行写入 `<<<SKILL_DIAGNOSIS_RESULT_V1:RESOLVED>>>` 或 `<<<SKILL_DIAGNOSIS_RESULT_V1:UNRESOLVED>>>`，从下一行开始输出报告正文。诊断状态由 Skill 自己选择。框架不再核查结论是否有原始证据支撑（grounding），也不复核证据一致性或判断 Candidate 的含义；不会自动将结果降级为 `PARTIAL`、清空根因或补写“证据不足”。Skill 表达的不确定性和未解决结论仍按原文展示。

`off` 会关闭独立审核角色 Reviewer，即使旧配置仍有 `SPECIALIZED_REVIEWER_ENABLED=true` 也不会启动审核。新 Job 使用固定版本的 `agent-profile/skill-direct` 与 `output-contract/skill-direct`，诊断模式仍为 `SPECIALIZED`，并保留 `selected_skill_ref`。

报告沿用 Generic V2 的 Markdown 存储和返回方式：Case 的 `generic_result_v2.skill_name` 记录实际使用的 Skill，网站返回 `format=markdown`。此模式不生成证据核验 JSON 或 `result.zip`，`archive_status=NOT_REQUIRED`。Generic 原有的 Markdown 输出方式不变。

切换策略后需要重启服务。升级或切换前，应先结束正在执行的任务。已有 Job 继续使用创建时记录的版本和输出格式，不按新配置改写；历史报告也保持原样。

## 显式恢复原交付策略

设置 `METHODS_EVIDENCE_VALIDATION=advisory` 可恢复原建议模式。引用文字、行号、marker、身份词或方法标识不一致，以及引用缺失，都不会导致整份诊断被清空；完全相同的发现会合并。有可识别的发现时，返回标明未经复核的 `PARTIAL`；没有发现时，返回 `INCONCLUSIVE`。此时 `root_cause=null`，没有依据的完成条件标记为 `UNKNOWN`，校验状态为 `SEMANTIC_ONLY`。引用位置只从本次保存的日志快照中解析。

设置 `METHODS_EVIDENCE_VALIDATION=strict` 可恢复原核验模式。Reviewer 关闭时，逐项保留能通过核验的发现；开启时，核验整份诊断。在 `advisory` 和 `strict` 模式下，仍由 `SPECIALIZED_REVIEWER_ENABLED` 控制是否启动独立审核。Reviewer 使用对应诊断审计记录中的证据策略；历史审计缺少该字段时，按 `strict` 处理。这两种模式沿用原有的 JSON 报告格式和归档规则。

## 输入和资源边界

参数整理阶段 Intake 会先保留有效参数，再追问仍然缺少的信息。引用用户原话时，可以包含参数前后的上下文。完全重复的项会合并；多余字段或某一项的格式错误，不会导致其他有效字段丢失。完整且带有明确时区的 ISO 时间可统一为毫秒精度的 UTC 时间。标识符不改写，时区缺失时也不猜测。如需更正任务已采用并固定的事实，仍须新建任务。每条新消息最多调用一次 Intake，不会为修复输出而额外调用模型。

公开 API 和七个 MCP 工具的输入类型不变，输入 schema 仍保持扁平。以下情况仍会被拒绝：API 请求的 JSON 不合法；要求 JSON 输出的模型阶段返回了非法 JSON；结果标记无法识别或出现多个结束状态；进程异常退出；输出超限；权限、文件归属或路径检查失败；实际文件内容或哈希发生变化。`off` 关闭结论的证据复核后，文件安全、输入完整性和执行协议检查仍然生效。

Intake、ROUTE，以及 `advisory` / `strict` 模式下的 Methods 诊断和审核，共用模型 JSON 的[受限提取规则](model-output-compatibility.md#说明文字与最终-json)。如果最终结果带有 Markdown 说明，可提取末尾唯一完整的 JSON，再检查字段和执行协议；存在多个候选时，不猜测应使用哪一个。此规则不适用于 `off` 模式的 Skill 正文、Generic Markdown 或公开接口输入。正常 JSON 不需要扫描候选，也不会增加模型调用、修复次数或自动恢复任务。

## 报告读取与验证范围

`off` 保留调用模型前的 marker 扫描和方法卡选择，输入筛选方式没有变化，因此不能据此判断性能有所提升。`advisory` 保留原有优化：模型执行后不再全量扫描一次 marker；生成报告和归档时，逐份日志提取所需引用，不同调用之间重新校验文件。

网站从同一 Case 快照读取报告和文件的元数据，无需另行请求文件列表或重复匹配。小于大小上限的报告会在限定的内存空间中完成校验，再显示给用户；ZIP 仍使用文件流传输。下载地址始终根据配置的服务地址、已授权的 Case 和核验过的产物 ID 构造，不直接使用响应中的下载 URL。

验收时，从正式 Test Flow 入口运行 Dev 的受影响测试和完整确定性测试（affected + full deterministic），不调用真实模型。是否通过以对应源码快照的 `verdict.json` 为准。这些测试不能证明公司内网的端到端延迟或真实模型的完成率。
