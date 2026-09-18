# 诊断交付策略

网站实际调用路径是网站后端 → Agent HTTP API → 共享应用服务 → 调度器 → Runtime → 报告与归档。MCP 是同一应用服务的另一个入口。以下策略同时适用于 Web 和 MCP 发起的 Methods 诊断。

## 当前默认行为

`METHODS_EVIDENCE_VALIDATION=off` 是生产默认值，在服务启动时固定。服务保留模型执行前的 Logparse、目标日志冻结、marker 扫描和按命中加载方法卡；这些步骤用于准备诊断输入。方法卡是 Skill 内独立保存的诊断方法文档，包含适用条件、日志线索和判断步骤。

Skill 完成诊断后，框架直接交付其 Markdown 正文。模型首行写 `<<<SKILL_DIAGNOSIS_RESULT_V1:RESOLVED>>>` 或 `<<<SKILL_DIAGNOSIS_RESULT_V1:UNRESOLVED>>>`，换行后是原样正文。终态由 Skill 自己选择；框架不再做 grounding、证据一致性复核或 Candidate 语义判定，不自动降级为 `PARTIAL`、清空根因或补写“证据不足”。Skill 自己表达的不确定性和未解决结论仍按原文展示。

`off` 强制关闭独立 Reviewer，即使旧配置仍有 `SPECIALIZED_REVIEWER_ENABLED=true` 也不会启动审核。新 Job 冻结 `agent-profile/skill-direct` 与 `output-contract/skill-direct`，诊断模式仍为 `SPECIALIZED`，保留 `selected_skill_ref`。直接交付复用 Generic V2 的 Markdown 载体：Case 的 `generic_result_v2.skill_name` 记录实际 Skill，网站返回 `format=markdown`。不生成证据核验 JSON 或 `result.zip`，`archive_status=NOT_REQUIRED`。Generic 原有直出行为不变。

策略切换需要重启服务；升级或切换前先结束活跃任务。已有 Job 保留创建时冻结的身份和合同，不能按新配置改写；历史报告字节保持原样。

## 显式恢复原交付策略

设置 `METHODS_EVIDENCE_VALIDATION=advisory` 可恢复原建议模式：引用文字、行号、marker、身份词、方法标识不一致或引用缺失不清空整份诊断，完全相同的发现合并。有可识别发现时交付标明未经复核的 `PARTIAL`，没有发现时交付 `INCONCLUSIVE`；`root_cause=null`，无依据的完成条件为 `UNKNOWN`，校验状态为 `SEMANTIC_ONLY`。引用位置只从本次冻结日志解析。

设置 `METHODS_EVIDENCE_VALIDATION=strict` 可恢复原核验：Reviewer 关闭时逐项保留可核验发现，开启时执行整份核验。`advisory` 和 `strict` 下，`SPECIALIZED_REVIEWER_ENABLED` 继续控制独立审核；Reviewer 继承对应诊断审计中的证据策略，历史审计缺少该字段时视为 `strict`。这些模式保留原有 JSON 报告与归档合同。

## 输入和资源边界

Intake 先保留有效参数，再按剩余要求追问。用户引用可以包含原值两侧的上下文；完全重复项合并，多余字段和单项格式错误不会丢掉其他字段。明确带时区的完整 ISO 时间可规范为毫秒 UTC，标识符不改写、缺失时区不猜测。更正已冻结事实仍需新建任务。每条新消息最多一次 Intake，不增加修复模型调用。

公开 API 和七个 MCP 工具的输入类型及扁平 schema 不变。非法 API JSON、使用 JSON 合同的模型阶段返回非法 JSON、无法识别的终态标记、多个终态、进程异常退出、输出超限、权限错误、文件归属错误、路径越界和实际字节/哈希变化仍拒绝。`off` 关闭的是结论证据复核，文件安全、输入完整性和执行协议检查继续生效。

Intake、ROUTE，以及 `advisory` / `strict` 的 Methods 诊断和审核共用模型 JSON 的[受限提取规则](model-output-compatibility.md#说明文字与最终-json)：最终结果中出现 Markdown 说明时，可提取唯一完整的末尾 JSON，再执行字段和执行协议检查。多个候选不猜测。`off` 的 Skill 直出、Generic Markdown 和公开输入不套用 JSON 提取兼容。正常 JSON 不扫描候选，不增加模型调用、修复轮次或自动续办。

## 报告读取与验证范围

`off` 保留前置 marker 扫描和方法卡选择，不改变模型输入的筛选方式，也不据此宣称性能提升。`advisory` 保留原有优化：不做模型执行后的第二次 marker 全量扫描，报告与归档逐份日志提取所需引用，跨调用重新校验文件。

网站从同一 Case 快照读取产物元数据，省去独立产物列表请求及重复列表匹配。小于报告上限的报告在有界内存中校验后展示，ZIP 继续使用文件流。下载始终以配置地址、授权 Case 和核验过的产物 ID 寻址，不信任产物响应里的下载 URL。

验收应从正式 Test Flow 入口运行 Dev affected + full deterministic，不调用真实模型。是否通过以对应源码快照的 `verdict.json` 为准；确定性结果不能证明公司内网端到端延迟或真实模型完成率。
