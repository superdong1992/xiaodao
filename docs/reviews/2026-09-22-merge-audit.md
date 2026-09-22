# 2026-09-22 合并代码与功能审计

本次审计核对 `0217d48..ffa6dc8` 的 **126 个变更文件**，重点检查新增功能与两次合并后的交接行为。审计开始时 `HEAD` 与 `origin/main` 均为 `ffa6dc8`，工作树干净。本文记录这一基线的检查结果，不把后续修复混入基线结论。

| 提交 | 内容及审查重点 |
| --- | --- |
| `45d2d68` | 七天历史保留、运行文件清理、日志轮转、资源使用租约和部署说明 |
| `aba3a5a` | 默认关闭的通用报告反馈、经验提炼与召回、网站调用封装 |
| `9afab70` | Web 日志接入通用定位、parse-only、补日志重启及附件交接竞态 |
| `c56074d` | 合并保留策略与经验库；父提交为 `45d2d68`、`aba3a5a` |
| `ffa6dc8` | 合并日志接入与经验库；父提交为 `9afab70`、`c56074d` |

审查对比了区间差异和两个 merge 的父提交，沿入口、状态提交、后台执行、结果发布及清理路径走读，并在核对源码一致的 Linux 副本上执行确定性专项。另用实际 HTTP 服务复现网站边界问题。未调用真实模型，未验证生产 Logparse 安装或局域网 Skill 的诊断质量。

## 按修改组核对

以下分组覆盖上述 126 个文件，包括相应测试、schema、fixture、文档和测试工具变更；代表用例用于说明直接覆盖的行为，不以文件数量或泛化的全量通过替代专项证据。

| 修改组与走读路径 | 核对结果与直接测试 |
| --- | --- |
| 历史保留：`storage/history_retention.py`、`state_repository.py`、`agent/store.py` | 每轮独立按完成时间保留七天；重命名或新一轮不延长旧轮。清理先持久保存精确文件清单，失败后可重试。`test_history_retention.py` 中 `test_old_run_is_removed_while_new_run_stays_and_sse_cursor_expires`、`test_completion_age_is_not_extended_by_rename`、`test_filesystem_failure_keeps_exact_manifest_and_retries_after_reopen` 直接覆盖。 |
| 资源使用与删除：`application/resource_usage.py`、`queries.py`、`uploads.py`、`external_commands.py`、`agent/cleanup.py`、`storage/resource_files.py`、`resource_store.py`、`paths.py`、`retention*.py` | 下载、上传、活跃 worker 和归档阻止清理；主工作区与 `.logparse-preprocess` 共用 Job 归属保护；仅清理受控路径，空父目录只尝试 `rmdir`。`test_resource_usage.py`、`test_cleanup.py`、`test_retention_cleaner.py` 覆盖租约、共享锁、删除失败重试；`test_resource_files.py`、`test_resource_store.py`、`test_paths_atomic.py` 覆盖独立复制、漂移、路径穿越和原发布边界。 |
| 日志与启动资源：`storage/log_rotation.py`、`diagnostics.py`、`journey.py`、`journey_renderer.py`、`runtime/catalog.py`、`bootstrap.py` | 日志按 16 MiB、四份备份轮转，单事件 64 KiB；巨行裁剪和已淘汰片段有明确标记。启动资源副本有边界，关闭失败保留实例锁并允许重试。`test_log_rotation.py`、`test_diagnostics.py`、`test_journey*.py`、`test_catalog_lifecycle.py`，以及 `test_bootstrap_composition.py` 的关闭失败和并发关闭专项覆盖。 |
| 数据升级：`entrypoints/data_upgrade.py`、`replay.py`、`agent/store.py`、`memory/store.py` | 现有 Agent v2 启动补齐新增表，不重写已有记录；离线升级仅接收规定的旧版本，未知表或当前 v2 不会被当成旧库迁移。`test_data_upgrade.py` 覆盖 WAL、报告资源、归属映射、失败不修改来源或发布目标；另做旧 v2 缺表和当前 v2 重启的数据库实测。 |
| 合同与领域：`contracts/{models,commands,enums,errors,outcomes,ports}.py`、`domain/coordinator.py`、`schemas/v2/*`、领域 fixtures | 新增字段默认不序列化，保留旧无日志字节及幂等身份；parse-only 与原计划组成联合类型；Generic 仅接收一份所选归档，不混入专用 Evidence、Artifact 或历史 outcome。`test_generic_log_handoff.py`、合同枚举和错误闭包、领域状态矩阵、schema/readiness 与 `test_release_boundaries.py` 覆盖合同及默认配置。公开 MCP 输入仍保持扁平。 |
| 应用交接：`application/{external_commands,formalization,preparation,projection,outcome_submission,runtime_bindings}.py` | 内部 `RestartGenericDiagnosis` 在同一 Case 取消旧 Job 并建立新预检 Job；`MarkInitialLogArchiveExpected` 只标记同一活跃 ROUTE。重复请求不新建任务，claim 增加 revision 不误拒绝，已结束或已替换来源会拒绝。`test_restart_cancels_previous_job_keeps_case_and_replays_once`、`test_restart_cancel_failure_defers_new_dispatch_and_replay_retries_cancel`、`test_restart_rebases_claim_revision_without_changing_source_or_request_hash`、`test_route_log_intent_racing_outcome_does_not_recover_text_only_successor` 直接覆盖。 |
| Web 消息交接：`agent/service.py`、`agent/store.py` | 接受消息与核心命令收据投影保持事务关联；等待附件时重读最新选包及文字；停止、删除、运行时期恢复不把旧请求当新诊断重放。`test_generic_log_messages.py`、`test_generic_web_logs.py` 覆盖首次绑定、ROUTE 完成、导入中换包、补充提交前后等屏障；`test_specialized_web_log_compatibility.py` 覆盖路由转专用、重复附件和再路由转 Generic。 |
| Logparse：`integrations/logparse/{requests,broker,cli,outputs,paths,tree}.py` | parse-only 使用现有产品配置，不要求时间、slot、进程或 PID；核对完整日志清单、路径、大小和哈希，扫描中检查取消、时限和资源预算。`test_logparse_parse_only.py` 的无参数、空清单、伪造或不完整清单、内容漂移、分块中止及 broker 只解析一次用例直接覆盖。 |
| Generic 执行：`runtime/{diagnosis_runtime,generic_locator,generic_logs,workspace,catalog}.py`、运行 profile/policy、`storage/execution_records.py` | 服务端解析后提供只读日志清单，模型按需读文件，日志正文不塞入 prompt。无日志沿用纯文本；有日志但解析失败不降级为成功；空清单返回 `UNRESOLVED` 且不调用模型。`test_generic_logs.py` 验证报告依赖真实日志内容、前后篡改、预算、取消及空清单；`test_generic_logs_supplements_and_memory_share_one_bounded_skill_invocation` 两个参数覆盖日志、补充描述和经验同次调用，以及经验超预算整条舍弃。 |
| 反馈授权与存储：`memory/{models,service,store}.py`、`agent/service.py`、`interfaces/agent_http.py`、`http_app.py` | 只允许已发布的 Generic V2 报告，核对所属用户、轮次、来源 Job、报告附件及正文。投票、幂等收据和唯一提炼任务同事务提交；旧请求重放不恢复旧票，满额整体回滚。`test_generic_feedback.py` 的真实来源 HTTP、跨用户、旧版/专用拒绝、并发幂等、换票及容量专项；报告身份、大小和哈希另由 `test_reports.py` 覆盖。 |
| 提炼与召回：`memory/{extraction,retrieval}.py`、`entrypoints/settings.py`、`bootstrap.py` | 开关默认关闭，同时控制评价、提炼和召回。每份报告最多一次提炼，不确定的运行任务重启后不重调；成功/失败清空任务原文，晚到结果重新检查投票和删除。经验按 Skill 名称隔离，最多一张、4 KiB；校验失败、检索异常或超预算不阻断诊断。`test_experience_memory.py`、`test_settings.py`、`test_memory_worker_and_recall_follow_the_same_feature_flag` 和 Generic 审计/预算用例覆盖；专用执行用例明确断言不调用召回。 |
| 网站接口与下载：`examples/website-agent/{server.mjs,browser-client.js}` 及其测试 | 反馈使用指定轮次，后端派生归属键；浏览器不能指定 owner，重试沿用请求 ID，响应再次校验身份。现有反馈、附件和文件代理测试通过，但错误码透传、重启等待的重试标志及小报告下载发现下文三项缺陷。 |
| 文档、接入 Skill 与验证工具：根目录说明/台账、`docs/*`、`.claude/skills/adapt-lan-generic-locator-v2/*`、`.env.example`、`tools/test-flow/runtime-support/relay_service_journey.py`、两个指南测试 | 核对默认开关、Linux 服务端边界、日志接入步骤、保留策略、迁移说明和新增反馈接口；指南/接口快照测试及 relay 轮转读取测试提供机械校验。上传 `expires_at=null` 的说明仍需按下文修正；生产验收和正式 verdict 不能由说明文件代替。 |

## 两次合并最终保留的语义

`c56074d` 同时保留七天原始诊断清理和经验库：自然过期只撤销未完成提炼，已完成经验沿用独立的 90 天期限；主动删除则立即撤销经验，即使会话已自然过期但删除凭据仍在。`expire_sources` 与历史删除处于同一事务，晚到提炼不能恢复已清理来源。对应 `test_natural_conversation_expiry_keeps_ready_memory_until_card_ttl`、`test_natural_conversation_expiry_erases_unfinished_memory_and_fences_late_result`、`test_explicit_delete_after_natural_expiry_revokes_ready_memory` 和旧轮次清理专项。服务关闭同时等待 memory worker，保留目录关闭、实例锁及失败重试逻辑。

`ffa6dc8` 同时保留日志读取和经验参考：完整原问题、补充文字、日志读取入口与输出合同优先，经验放在独立参考区，放不下时整条跳过。profile 升为 `4.0.0`、context policy 升为 `3.0.0`，catalog 同步；`GENERIC_LOGPARSE_PRODUCT` 与默认关闭的 `GENERIC_MEMORY_ENABLED` 并存。空日志仍为 `UNRESOLVED`，专用流程不读取经验。历史清理还覆盖 `agent_generic_restarts`，待交接请求选中的上传不会被提前回收。

另一次独立交叉走读确认：重启的状态提交先于取消信号和派发；取消失败不派发，Dispatcher 的 Case 互斥阻止新旧 worker 同时执行。停止夹在提交与队列通知之间时，claim 仍检查 PENDING、当前 active Job 与 RUNNING Case；已取消任务不会启动模型。旧 outcome 进入 STALE，不覆盖新 Job。ROUTE 的有日志后继 ID 与纯文本后继 ID 分开，避免冲突重试恢复错误的不可变 Job。重新路由带来的普通历史附件不会被误当日志；单个有效归档自动采用，多份有效归档要求明确选择。

## 基线中确认的问题

以下是 `ffa6dc8` 的复现结论，列入后续修复范围，不在本文预先宣称修复通过。

| 问题 | 入口、输入与实际输出 | 应保留的回归断言 |
| --- | --- | --- |
| 网站代理遗漏三个日志错误码 | 活动 Generic 多选两个包、重提当前包、重启请求尚待交接时，Agent HTTP 分别返回 `AGENT_LOG_SELECTION_INVALID`、`AGENT_LOG_ALREADY_SELECTED`、`AGENT_RESTART_PENDING`；BFF 的公开错误码白名单将其改为 `WEBSITE_AGENT_ERROR`。实际 Agent HTTP 与 Node BFF 串联复现。 | 这三个受控错误经过代理后保持错误码、HTTP 状态和安全文案；不得暴露内部异常原文。 |
| 重启等待错误被标为不可重试 | 已有冻结 PENDING 请求时提交另一请求，或 ROUTE 交接尚未稳定时，返回 `AGENT_RESTART_PENDING`（409/503），但 `retryable=false`。相同请求仍可在后台交接完成后重试，响应标志与流程不一致。 | 所有重启等待出口均明确可重试；沿用原请求 ID，不重复接纳消息或创建 Job。 |
| 小报告下载遗漏总时限与断连取消 | 对 `GENERIC_REPORT` 注入持续慢流，将 30 分钟时限压缩为 25 ms，150 ms 后请求仍未结束，未注册总时限定时器；浏览器断开后上游仍继续。相同条件下 ZIP 分支会超时并关闭上游。问题分支早于 `0217d48`，但本次新增下载总时限承诺没有覆盖它。 | 小报告与 ZIP 都执行总时限；客户端断开关闭上游；保留正文大小/哈希校验和小报告无临时文件的行为。 |

同时确认一处说明问题：上传预约 `expires_at=null` 表示接口没有返回独立到期时间，不能解释为永久保留或不自动回收。未被保留轮次引用的上传仍按七天策略清理；后续修正接口说明与字段表，不改变响应格式。

网站适配还须区分两个既有入口边界：首条或 ROUTE 阶段提交多包，可能先接受消息，再在 `current_questions` 要求选择；活跃 Generic 补包才同步返回选包错误。重启请求已固定后遇到报告抢先完成会拒绝；若消息进入服务端时本轮已经结束，有文本的新请求按原规则新开一轮，只有附件返回 400。消息体没有 `expected_run_id`，前端应核对收据中的 `run_id`，不能承诺所有结束竞态都返回 409。增量适配清单和 API 说明已按实际行为区分。

## 验证记录与结论边界

本轮开发期定向验证使用当前审计基线的 Linux 副本，结果按执行组记录。组间存在重复用例，**不得相加当作独立用例总数**，也不能替代正式 Test Flow verdict。

| 执行组 | 结果与范围 |
| --- | --- |
| 保留、资源与迁移 | 两轮合计 271 passed、1 skipped。首轮覆盖历史/临时清理、Agent 删除与重启记录、资源租约、日志轮转、离线迁移；第二轮覆盖资源文件、资源存储与路径发布。跳过项为非 Linux 入口拒绝测试在 Linux 上不适用。 |
| Logparse 与应用交接 | 389 passed，覆盖 `tests/deterministic/unit/integrations` 全目录、`test_generic_logs.py`、`test_generic_log_handoff.py`；两条既有性能用例的 `record_property`/xunit2 告警不代表功能失败。开发期 JUnit 文件为 Linux 副本中的 `.tmp/audit-root-logparse-20260922.xml`。 |
| 经验库独立审查 | 143 passed：反馈、提炼召回、历史保留、Generic 日志；补充 21 passed：开关、正式报告身份/哈希、经验预算/审计及专用不召回，共 164 项。 |
| Agent 与网站 | Python 定向 196 passed，覆盖日志消息、反馈、Agent HTTP、Generic Web 日志及专用兼容；Node 现有测试 173 passed。上述三个缺陷来自额外的真实 HTTP 边界复现，不能因现有用例通过而排除。 |

现有 Agent v2 缺表启动与重启的实测还确认：新增表可补齐，旧会话字节和已有反馈、任务、重启记录保持不变；对当前 v2 错用离线旧版升级入口会返回 `VERSION_UNSUPPORTED`，来源文件清单不变，也不发布新目标。

三个 Web 边界复现使用命令标准输入启动实际服务，观察结果保留在审计任务的工具输出中，没有另外保存复现脚本或正式证据目录；后续修复须补可重复的仓库专项入口。迁移实测目录为 Linux 副本中的 `.tmp/audit-schema-01`。这些开发产物均不是正式 `verdict.json`。

经验库按同一 `DATA_ROOT + GENERIC_SKILL_NAME` 共享；同名 Skill 内容更新不会自动使旧经验失效，这是当前设计边界。模式校验不能保证识别全部私有名称，真实模型脱敏和实际 Skill 使用经验仍需验收。外部 CLI 自身的会话、记忆和缓存按现有保留文档属于部署边界，本次没有证实新增泄漏。

后续修复的专项测试、最终源码快照及正式结果以根目录 [FIXED_ISSUES.md](../../FIXED_ISSUES.md) 的对应记录和所引用的 `verdict.json` 为准。本文不预写正式 PASS，也不引用尚未产生的提交或快照。
