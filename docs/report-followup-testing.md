# 报告追问验证范围

使用中央 `tools/test-flow/run.ps1` 或 `run.sh` 执行 `dev.default`，由编排器选择 affected 与完整确定性轨。新追问测试不能替代原有创建、补参、上传、通用/专用定位、审核、报告、下载、反馈、停止、删除、历史及诊断 SSE 用例。

| 范围 | 直接覆盖 |
| --- | --- |
| 生产链路 | `tests/deterministic/integration/test_report_followups.py`：已发布报告资格、HTTP 提交、连续追问、原数据不变 |
| 存储与执行 | `tests/deterministic/unit/agent/test_report_followup.py`：重复请求、会话互斥、日志读取、文本补充、旧报告降级、输入漂移、失败与重启恢复；`test_report_followup_faults.py`：模型超时、磁盘不足、总量配额、开关往返和提交事务回滚 |
| 消息兼容 | `tests/deterministic/unit/agent/test_target_run_guard.py`：旧行为、定向补充、完成竞争、幂等及通用日志重启 |
| HTTP 与恢复 | `tests/deterministic/unit/interfaces/test_followup_http.py`：归属、严格输入、独立游标回放、查询不执行模型、原 schema 不变 |
| 到期与删除 | `tests/deterministic/unit/storage/test_followup_retention.py`：队列与来源保护、关闭后清理、原报告期限、删除后迟到回答 |
| 网站联调 | `examples/website-agent/followup.test.mjs`：快照与事件恢复、迟到响应、分页、原请求重试、跨轮停止、BFF 鉴权、正文展示 |
| 真实 Agent | `tests/real/agent/test_real_report_followup.py`：两次生产 worker 调用，日志独有证据、前轮语境和文字补充；工具路径及输入哈希审计 |

真实验证使用独立 `dev.real --stage real.report-followup`。执行前必须审阅同一组参数的 `--plan-only`，核对模型与启动器身份、Proof、Stage、Gate、复用决定、两次调用和预算。计划单次最多 8 个模型回合、120000 token、1 美元、300 秒；两次合计上限为 240000 token 和 2 美元。此次 Gate 不代表 Release，也不能替代实际网站与 Linux 服务部署验收。

改动前基线和最终权威 verdict 记录在 [修复台账 PL-FIX-062](../FIXED_ISSUES.md#pl-fix-062报告交付后继续提问会误开新诊断缺少独立问答与恢复能力)。基线已验证全部旧功能 Gate；Windows 运行耗时超过原有性能门槛，保留 FAIL 原结论。最终结论只引用对应源码快照的 `verdict.json`，不把静态检查、测试文件存在或离线预览当成真实验收。
