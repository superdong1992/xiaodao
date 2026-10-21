# 报告追问验证范围

使用中央 `tools/test-flow/run.ps1` 或 `run.sh` 执行 `dev.default`，由编排器选择 affected 与完整确定性轨。新追问测试不能替代原有创建、补参、上传、通用/专用定位、审核、报告、下载、反馈、停止、删除、历史及诊断 SSE 用例。

| 范围 | 直接覆盖 |
| --- | --- |
| 生产链路 | `tests/deterministic/integration/test_report_followups.py`：已发布报告资格、HTTP 提交、连续追问、原报告不变；`test_unified_followup.py`：HTTP 普通答复后交接新 run / Case、Generic 解析两个日志包、发布 V2 新报告和版本，保留旧 Case 与报告 |
| 存储与执行 | `tests/deterministic/unit/agent/test_report_followup.py`：重复请求、会话互斥、日志读取、文本补充、旧报告降级、输入漂移、失败与重启恢复；`test_report_followup_faults.py`：模型超时、磁盘不足、总量配额、开关往返和提交事务回滚 |
| 同次决策与交接 | `tests/deterministic/unit/agent/test_followup_result_protocol.py` 与 `test_followup_decision.py`：三种严格结果包、当前任务身份、正文中的 JSON 不是控制动作、无效输出不泄露或自动重试；`test_followup_handoff.py` 与 `tests/deterministic/postgres/test_postgres_followup_handoff.py`：run 与完成记录同事务、并发幂等、回滚、停止、删除，以及旧日志快照不遮蔽当前有效用户补充 |
| 最终材料与失败后承接 | `tests/deterministic/unit/agent/test_followup_material_selection.py` 与 `test_followup_context_recovery.py`：最终附件集合、排除旧包、未选过期包不阻断、最新未发布尝试的用户消息与采纳状态、连续失败后的整理输入、长补充只读检索与准入预算、选中材料的事务前重验；无材料且没有关联尝试时不增加数据库查询 |
| 消息兼容 | `tests/deterministic/unit/agent/test_target_run_guard.py`：旧行为、定向补充、完成竞争、幂等及通用日志重启 |
| HTTP 与恢复 | `tests/deterministic/unit/interfaces/test_followup_http.py`：归属、严格输入、独立游标回放、查询不执行模型；补日志 `attachment_ids` 和交接 `diagnosis_run_id` 必须随快照与事件一致恢复，旧记录缺省仍可读 |
| 反馈与经验兼容 | `tests/deterministic/unit/agent/test_followup_handoff_feedback.py`：交接说明不可评价、不可提交文字意见、不进入经验；普通回答仍接受独立负反馈与网站分析资料。既有 `test_feedback_comments.py`、`test_feedback_comment_review.py`、`test_followup_corrections.py` 保留管理员资料来源、引用核验及纠正回执约束 |
| 到期与删除 | `tests/deterministic/unit/storage/test_followup_retention.py`：队列与来源保护、关闭后清理、原报告期限、删除后迟到回答 |
| 网站联调 | `examples/website-agent/followup.test.mjs`：快照与事件恢复、迟到响应、分页、原请求重试、跨轮停止、BFF 鉴权、正文展示；`unified-followup.test.mjs`：补日志上传、关联诊断进度、同一输入区按后端待补问题回复新轮，409 刷新原目标，真实 BFF 透传附件过期 410 后允许修改输入 |
| 真实 Agent | `tests/real/agent/test_real_report_followup.py`：两次生产 worker 调用，日志独有证据、前轮语境和文字补充；工具路径及输入哈希审计 |

真实验证使用独立 `dev.real --stage real.report-followup`。执行前必须审阅同一组参数的 `--plan-only`，核对模型与启动器身份、Proof、Stage、Gate、复用决定、两次调用和预算。计划单次最多 8 个模型回合、120000 token、1 美元、300 秒；两次合计上限为 240000 token 和 2 美元。此次 Gate 不代表 Release，也不能替代实际网站与 Linux 服务部署验收。

上述 300 秒属于独立真实测试 profile，不随生产配置调整。生产追问使用 `REPORT_FOLLOWUP_TIMEOUT_SECONDS`，默认 600 秒、最大 1800 秒；测试启动器或模型服务自己的更短限制仍会生效。确定性回归使用 `test_followup_execution_budget.py`、settings 与 bootstrap 专项覆盖默认值、边界校验、实际参数传递、超时无重试、停止和关闭，以及检索规则的提示输入；模拟耗时不代表真实模型的延迟或判断效果。

结构化输出使用 `test_structured_output.py`、`test_report_followup_policy.py`、`test_followup_structured_output.py` 和交接专项，覆盖执行前检查、执行后权限、终态对象、业务合同和真实 backend 子进程回放；均不调用模型。正式追问审计允许精确的 `StructuredOutput` / `structuredoutput` 格式化工具，不把它们计为日志读取，仍要求对象参数及成功配对的工具结果。`report-followup-gate.test.mjs` 覆盖初始化工具清单、输入不变、错误工具与终态、启动器原样透传；不改变真实测试的调用数、回合或时间上限。

`test_followup_hook_boundaries.py` 验证前置 Hook 拒绝歧义 JSON、绑定本次文件权限，以及移除会跳过 Hook 的 `--bare`；`test_agent_telemetry_boundaries.py` 验证完整工具参数、未知权限、顶层与嵌套分片的一致处理，以及异常尾帧不会外抛或恢复旧结果。Node 审计同时检查分片身份与完整工具参数，不能只凭分片产出通过结论。合法流式重复事件保持只计一次。

单入口交接的专项还须断言：一次追问不额外调用 INTAKE 分类；只有有效 `CONTINUE_DIAGNOSIS` 决策可以交接；同一事务保存完成记录与关联 run；并发重放、提交回滚、停止、删除及重启不会重复开案；新轮未发布或失败时不推进报告版本。页面刷新与 SSE 重放只读取 `diagnosis_run_id`，不能重新提交。确定性模型替身验证这些执行边界，不代表真实模型已准确判断所有材料是否充分；不得为适配新行为删减真实测试断言或放宽预算。

判断复核还要求直接覆盖以下旅程：A 被 B 替换后再补 C，最终清单为 B、C，且可以排除单个旧包，不被未选中的过期包阻断；交接后在新轮补充文字和日志，再失败，下一次追问能看到最新尝试的有效用户信息与状态，连续两次失败也保留上次采用的整理输入；失败、中断和停止时优先展示终态或安全失败说明，不能被旧进度覆盖。长补充按只读文件检索，不调用摘要模型；没有检索能力时须在准入阶段拒绝超限。已采纳事实、未采纳用户陈述与旧模型判断分别处理，不把所有失败轮次拼进上下文。

新轮进入 `WAITING_INPUT` 时，必须继续展示后端 `current_questions`，同一输入区以 `target_run_id=diagnosis_run_id` 提交诊断补充。直接复现过的错误是补充仍发到旧报告，查看进度又为未生成报告的新轮挂载追问而得到 `UNSUPPORTED`；此旅程必须覆盖刷新、补充成功和新报告就绪后的版本切换，不能只验证交接通知出现。

改动前基线和最终权威 verdict 记录在 [修复台账 PL-FIX-062](../FIXED_ISSUES.md#pl-fix-062报告交付后继续提问会误开新诊断缺少独立问答与恢复能力)。基线已验证全部旧功能 Gate；Windows 运行耗时超过原有性能门槛，保留 FAIL 原结论。最终结论只引用对应源码快照的 `verdict.json`，不把静态检查、测试文件存在或离线预览当成真实验收。
