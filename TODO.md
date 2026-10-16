# TODO

更新时间：2026-10-16

本文件是仓库活跃待办的唯一清单。已完成事项由代码、当前设计与 Git 历史证明，不在这里保留关闭项。

## 本次合并交付推送验收（2026-10-16）

- 推送前远端已新增 `09f1481`：逐条回答反馈和纠正经验。先前本地 `603e5b9` 的完整验证为 `PASS_WITH_WARNINGS`，完整轨 268.340 秒；远端功能也有独立 verdict，但两者都不能证明合并结果。保留全部证据，解决接口、反馈保留与迁移交集后重新验证最终源码。
- 合并专项通过，待推送前完整验证：`test_answer_memory_retention.py` 与回答学习、反馈、纠正相关文件 100 passed；真实 PostgreSQL 导入与记忆 16 passed；追问保留、装配和报告 39 passed；三个 HTTP 文件 181 passed；前端反馈、继续诊断和哈希 89 passed；接入与接口文档 50 passed。纠正回执和逐回答迁移的合并修复登记在 PL-FIX-089；8 条最新 verdict 引用统一等待最终合并源码的完整验证。
- 合并后完整 `run-20261016T062028Z-07331a81` 因 4 项旧迁移单测的二元解包失败，101 份证据保留；真实 PostgreSQL 100 项及其余 Gate 均通过。仅适配内部三元返回值并增加默认列精确断言，未改变产品。`python -m pytest tests/deterministic/unit/interfaces/test_postgres_import_memory.py tests/deterministic/integration/test_answer_memory_retention.py -q --tb=short -p no:cacheprovider --basetemp=/tmp/continuation-merged-import-final-01`：13 passed，3.23 秒。专项通过，仍待最终源码重新完整验证。
- **专项通过，待推送前完整验证**。按用户最新明确授权，以 `a84bc45` 为基线，一并交付报告正文与历史追问永久保存、日志资源回收与容量管理、持续诊断、多包日志、报告版本、意图分流及性能修复；保留已提交的长期统计修复。本轮不再按“仅存储”隔离交付。
- 最终状态统一指向 `FIXED_ISSUES.md` 的 PL-FIX-070 本轮元数据及 PL-FIX-083 至 PL-FIX-089。先完成所有交付正文，再查看仓库入口的 `dev.default --plan-only`，执行 affected 与完整确定性验证，核对权威 `verdict.json` 和最终源码快照。验证完成后只回填台账预留的 verdict 元数据行；本处不预写 PASS，也不以历史失败后的专项代替完整结论。
- 完整验证通过后才能推送；若仍有失败，保留证据并按新证据修正后重新验证。此前独立统计提交跳过完整验证、保留策略的功能及性能失败记录均继续保留，不能改写成已通过。此次不调用真实模型，生产部署、真实日志质量和 Release 验收仍按下方待办处理。
- 2026-10-16 本轮完整 `dev.default` `run-20261016T051049Z-835e4391` 为 FAIL：三项附件集成暴露 strict/advisory 要求误放宽到多包，以及 INTAKE 期间的新选择被旧选择抢先采用。原断言保留，按实际能力恢复单包要求，并核对导入前及补充提交前的最新选择；失败源码快照、verdict 摘要及 101 份证据见 PL-FIX-083。修正后仍须重新查看计划并执行完整验证，七条最新 verdict 元数据均不得提前填写 PASS。

## 持续诊断推送前验收（2026-10-16）

- **2026-10-16 意图分流补充，专项通过，待推送前完整验证**：普通报告追问原本就不创建 Case，仍直接调用原追问接口。本次只在继续诊断的既有 INTAKE 整理中增加 FOLLOWUP / CONTINUE 判断：解释结论、询问证据和假设讨论保留为父报告追问；事实更正、日志变化和明确重做请求才准备新诊断。FOLLOWUP 不能 confirm，也不能用 PATCH 转为 DRAFT；页面转交原追问接口，重试保存原请求 ID，不自动退回新诊断。
- 先用 `test_report_continuation.py -k "report_question_in_continuation or new_logs_cannot"` 复现原入口把纯提问整理为 DRAFT 的行为（4 failed，其中一项为尚未新增的字段）；补充意图输出解析专项也先复现旧合同拒绝 FOLLOWUP。实现后 `python -m pytest tests/deterministic/unit/agent/test_report_continuation.py tests/deterministic/unit/agent/test_continuation_intent.py tests/deterministic/unit/interfaces/test_continuation_import.py tests/deterministic/unit/interfaces/test_web_api.py tests/deterministic/integration/test_website_report_continuation.py -q --tb=short --maxfail=3 -p no:cacheprovider --basetemp=/tmp/continuation-intent-final-01`：70 passed，2 项集成测试仍按旧流程期待同步 DRAFT 而失败。已改为核对 PREPARING → 意图判断，并加入真实接口拒绝纯追问 confirm 的断言；单独重跑 `python -m pytest tests/deterministic/integration/test_website_report_continuation.py -q --tb=short --maxfail=2 -p no:cacheprovider --basetemp=/tmp/continuation-intent-integration-02`：2 passed。两个现有日志旅程均保留，普通追问、新 Case 创建与新报告发布的区别已覆盖。模型使用确定性替身，未验证真实模型的意图识别准确率。
- 意图分流页面专项先复现 2 failed，完成后 `node --test examples/website-agent/continuation.test.mjs examples/website-agent/preview.test.mjs examples/website-agent/followup.test.mjs`：42 passed，覆盖 FOLLOWUP 页面、BFF、父轮次追问、重试及刷新后的重复点击。文档检查 `node --test tools/test-flow/tests/rest-api-guide.test.mjs tools/test-flow/tests/website-agent-guide.test.mjs`：46 passed；`node --check examples/website-agent/preview.js` 和 `git diff --check` 通过。
- 草稿调度修复：Event 最小复现确认整理等待期间普通会话无法推进；改为独立单 worker，普通追问仍无额外分类。`.venv/Scripts/python.exe -m pytest tests/deterministic/unit/agent/test_continuation_scheduling.py tests/deterministic/unit/agent/test_report_continuation.py -q -p no:cacheprovider --basetemp=.tmp/continuation-scheduling-c9 --tb=short`：28 passed，证据目录同 `--basetemp`；覆盖双向不阻塞、单任务上限、手动推进、删除及关闭取消。修复登记见 PL-FIX-084。
- 版本读取修复：32 个版本、每版父报告 64 KiB 的新测试先复现两次全量查询（`.tmp/continuation-version-before-c10`），随后复用已有列表。`.venv/Scripts/python.exe -m pytest tests/deterministic/unit/agent/test_continuation_performance.py tests/deterministic/unit/agent/test_continuation_scheduling.py tests/deterministic/unit/agent/test_report_continuation.py -q -p no:cacheprovider --basetemp=.tmp/continuation-version-after-c11 --tb=short`：33 passed，确认只全量读取一次、历史正文只解析一次且继续诊断资格不变。修复登记见 PL-FIX-085。
- 分类输出修复：65536 字节原问题的完整回显加上 JSON 后超出输出预算，新测试先复现短结果被拒绝（`.tmp/continuation-compact-before-c12`）。FOLLOWUP 改为 null，由代码恢复原文；CONTINUE 保持完整非空文本。`.venv/Scripts/python.exe -m pytest tests/deterministic/unit/agent/test_continuation_intent.py tests/deterministic/unit/agent/test_report_continuation.py -q -p no:cacheprovider --basetemp=.tmp/continuation-compact-after-c13 --tb=short`：48 passed，最大原问题使用不足 128 字节的模型结果且仅调用一次。修复登记见 PL-FIX-086。
- 事实承接最终复核：`timeout_seconds=100` 误吞 ACTIVE `timeout_seconds=10` 的新专项先复现 3 failed、2 passed（WSL `/tmp/continuation-fact-line-red-02`）。按完整行和完整补充项去重后，在 WSL Ubuntu 22.04、`/mnt/d/code/xiaodao` 执行 `PYTHONPATH=src ~/quick-validation/web-retention-push-20261016-retry2/runtime/bin/python -m pytest tests/deterministic/unit/agent/test_report_continuation.py -q -p no:cacheprovider --basetemp=/tmp/continuation-fact-line-green-01`：26 passed。断言覆盖确认草稿与新 Case 实际输入，修复历史归入 PL-FIX-083；原失败和临时目录均保留。
- 完整验证后的附件专项：WSL Ubuntu 22.04、`/mnt/d/code/xiaodao` 执行 `PYTHONPATH=src ~/quick-validation/web-retention-push-20261016-retry2/runtime/bin/python -m pytest tests/deterministic/unit/agent/test_attachment_selection_performance.py tests/deterministic/unit/runtime/test_multi_log_runtime.py tests/deterministic/integration/test_intake_attachment_selection.py tests/deterministic/integration/test_intake_adoption.py::test_completed_intake_replay_after_adoption_only_finishes_coverage tests/deterministic/integration/test_website_report_continuation.py -q -p no:cacheprovider --basetemp=/tmp/continuation-selection-green-02 --tb=short`：22 passed、1 条既有 Starlette 警告，73.69 秒。覆盖三种运行策略的附件数量、原单包恢复、整理及导入中的选择替换、事实已采用后只换附件、实际 Job 输入、最终报告、消息均 APPLIED、无额外 INTAKE 和长历史重查。专项通过，待推送前完整验证；修复和失败历史统一见 PL-FIX-083。
- 附件重查最终优化：改为数据库按有效选择筛选，既有索引倒序 `LIMIT 1`，只返回 IDs，避免 PostgreSQL 驱动缓冲全历史。相同 WSL/Python 环境执行 `python -m pytest tests/deterministic/unit/agent/test_attachment_selection_performance.py tests/deterministic/integration/test_intake_attachment_selection.py -k "rechecks_only or import-1-True" -q -p no:cacheprovider --basetemp=/tmp/continuation-selection-bounded-01 --tb=short`：2 passed、8 deselected。安全加载并导出既有测试连接后执行 `python -m pytest tests/deterministic/postgres/test_postgres_attachment_selection.py -q -p no:cacheprovider --basetemp=/tmp/continuation-selection-postgres-02 --tb=short`：真实 PostgreSQL 1 passed；每例新建隔离测试库并保留回执，不触碰业务库。两库均核对长历史只解析选中 IDs；SQLite 另核对既有索引和无临时排序，PostgreSQL 不限制优化器的具体计划，也不改 planner 配置。最后使用同一环境联合运行上述 unit 与 postgres 长历史文件，`--basetemp=/tmp/continuation-selection-databases-final-01`：2 passed，2.08 秒。最初未导出连接的 setup 失败保留于 `/tmp/continuation-selection-postgres-01`。最终交付结论仍等待新源码的完整 Test Flow。
- 前端哈希与部署修复：最小清单先复现 continuation 依赖 404，六项新增清单测试先失败；同一 128 MiB 输入的旧主线程哈希为 3956 ms，修后 Worker 为 532.65 ms，主线程文件读取为 0，降级每 64 KiB 让出执行。`node --test examples/website-agent/file-sha256.test.mjs examples/website-agent/continuation.test.mjs examples/website-agent/onboarding.test.mjs examples/website-agent/preview.test.mjs examples/website-agent/browser-client.test.mjs`：90 passed，覆盖进度、取消、重试、标准摘要、部署依赖和 Worker 静态资源。专项与基准保留于本会话执行输出，没有独立 verdict；Node 基准不等于浏览器帧率验收。修复登记见 PL-FIX-087、PL-FIX-088。
- **专项通过，待推送前完整验证**。已实现继续诊断草稿、完整输入确认、独立 run/Case、日志追加与替换、逐包解析、旧报告只读上下文、报告版本列表和并排查看。自然语言更正复用一次有预算限制的 INTAKE，直接编辑不增加草稿模型任务；变化说明由本轮诊断生成，不增加比较模型。接入方式见[报告追问与持续诊断](docs/website-report-followup.md)和[网站 API](docs/website-agent-api.md)。
- 实际 HTTP → Agent → ROUTE → DIAGNOSE → 报告发布专项：`python -m pytest tests/deterministic/unit/interfaces/test_web_api.py tests/deterministic/unit/agent/test_report_continuation.py tests/deterministic/integration/test_website_report_continuation.py -q --tb=short -p no:cacheprovider --basetemp=/tmp/continuation-root-linux-06`，WSL Ubuntu 22.04：**43 passed**。覆盖追加、替换日志的真实解析路径、更正事实、旧报告独立上下文、新 Case、版本发布、幂等确认、失败重试和启动恢复；诊断模型使用测试替身。
- Agent 与存储专项：`python -m pytest tests/deterministic/unit/agent/test_report_continuation.py tests/deterministic/unit/agent/test_intake.py tests/deterministic/unit/agent/test_generic_log_messages.py tests/deterministic/unit/agent/test_report_followup.py tests/deterministic/unit/storage/test_multi_log_workspaces.py -q --tb=short -p no:cacheprovider --basetemp=/tmp/continuation-root-linux-01`：**162 passed**。后续受影响用例已由上述 43 项及运行时专项复验；各组有重叠，不累加为独立用例数。
- 合同与兼容性专项：`python -m pytest tests/deterministic/unit/application/test_continuation_contract_chain.py tests/deterministic/unit/application/test_preparation.py tests/deterministic/unit/application/test_generic_log_handoff.py tests/deterministic/unit/application/test_contract_readiness.py tests/deterministic/unit/domain tests/deterministic/unit/interfaces/test_data_upgrade.py tests/deterministic/unit/storage/test_postgres_compatibility.py tests/deterministic/unit/storage/test_fixtures.py tests/deterministic/unit/storage/test_state_repository.py tests/deterministic/contracts/test_schema_snapshots.py tests/deterministic/contracts/test_mcp_input_schema_flatness.py tests/deterministic/unit/interfaces/test_mcp_server.py tests/deterministic/unit/interfaces/test_replay_cli.py tests/deterministic/integration/test_failure_fixture_manifest.py tests/deterministic/integration/test_s02_contract_storage_seam.py -q --tb=short -p no:cacheprovider --basetemp=.tmp/continuation-contracts-7`：**403 passed，1 项平台跳过**。该次运行时合同链文件名为 `test_report_continuation.py`；其后仅为避免 pytest 同名收集冲突而改为当前名称，三个持续诊断文件联合收集通过。覆盖七个公开 MCP 工具扁平输入、r2/r3 数据读取与复制升级；最后受影响兼容性专项 25 passed，日志交接专项 22 passed。
- 迁移标记补充回归：`python -m pytest tests/deterministic/postgres/test_postgres_import.py::test_postgres_import_plan_never_opens_sqlite_or_postgres_or_writes_target tests/deterministic/postgres/test_postgres_import.py::test_postgres_import_rejects_existing_target_before_any_database_access tests/deterministic/unit/interfaces/test_continuation_import.py -q --tb=short -p no:cacheprovider --basetemp=/tmp/continuation-root-linux-07`：**7 passed**。验证 r2/r3 Agent V2 标记、草稿归属和未完成任务拒绝；此组不连接真实 PostgreSQL。
- 页面专项：`node --test examples/website-agent/server.test.mjs` 所覆盖页面与 BFF 用例 **244 passed，1 项平台跳过**；对应联合运行最初仅文档字段表失败，补齐后 `node --test tools/test-flow/tests/rest-api-guide.test.mjs tools/test-flow/tests/website-agent-guide.test.mjs` **46 passed**。本机浏览器人工核对了更正信息预览、确认生成 V2、版本切换及并排布局。
- 本轮合同更新为 `v11-contract-r3`。上线前按[数据升级说明](docs/data-upgrade-v11-r2.md)核对旧数据；保留现有会话累计附件数量和容量限制。专项使用确定性替身，尚未验证真实模型的意图准确率和报告质量。以上专项有重叠，不累加为独立用例总数，也不替代正式验收；本次合并交付的最终状态统一见台账 PL-FIX-070、PL-FIX-083 至 PL-FIX-089 的最新 verdict 元数据。

## Web 正文永久保存与日志资源回收验收（2026-10-16）

- 已实现正文、报告原始字节和历史追问永久保存；Web 文件默认 15 天，磁盘压力按完成时间回收。配置和接入变化见 [存储策略](docs/storage-retention.md) 与 [网站接口](docs/website-agent-api.md)。专项通过，推送前完整验证状态统一见 `FIXED_ISSUES.md` 的 PL-FIX-070“2026-10-16：Web 正文永久保存，日志资源独立回收”最新 verdict 元数据。
- 专项覆盖超过 30 天及数据库重开后的正文、报告原 URL/字节/哈希、15 天边界、等待状态转换与活动时间回填、权限、跨轮共享附件、物理清理失败重试、主动删除与隔离目录竞态、过期请求不复活、额度释放和大文件容量预约。
- Linux 专项：`python -m pytest tests/deterministic/unit/agent/test_report_followup.py tests/deterministic/unit/storage/test_followup_retention.py tests/deterministic/unit/storage/test_web_resource_retention.py tests/deterministic/unit/storage/test_completed_workspaces.py tests/deterministic/unit/storage/test_retention_cleaner.py tests/deterministic/unit/storage/test_capacity.py -q --tb=short --maxfail=3 -p no:cacheprovider`：95 passed。使用 WSL Ubuntu 22.04、仓库锁定依赖和独立 ext4 目录；包含真实 GiB 稀疏文件，确认拒绝写入时不读取、哈希或复制大文件。
- Linux 交叉修正专项：`python -m pytest tests/deterministic/unit/interfaces/test_postgres_import_memory.py tests/deterministic/unit/agent/test_cleanup_history_receipts.py tests/deterministic/unit/storage/test_capacity_reclamation.py tests/deterministic/integration/test_agent_reports.py -q --tb=short --maxfail=3 -p no:cacheprovider`：34 passed。覆盖旧库派生列的完整导入循环、源/目标摘要、主动删除优先级及报告资源回收集成。
- 最后补充的等待状态修正：`python -m pytest tests/deterministic/unit/storage/test_web_resource_retention.py tests/deterministic/unit/agent/test_store.py -q --tb=short --maxfail=3 -p no:cacheprovider`：77 passed。三个入口均先复现失败，再验证只有真正进入等待时续期，重复投影、读取和改名不续期。配置专项 `test_settings.py`：131 passed；容量回收与服务装配专项：34 passed；报告归档/使用租约/删除/HTTP 专项：149 passed；反馈与经验期限专项：148 passed。各组有重叠，不累加为独立用例总数。
- 页面专项：`node --test examples/website-agent/report-view.test.mjs examples/website-agent/followup-view.test.mjs examples/website-agent/browser-client.test.mjs`：64 passed。`git diff --check` 通过。
- 完整 REST 合同检查先复现 OpenAPI 缺少资源字段描述，再补齐中文说明及 Web 快照。`python -m pytest tests/deterministic/unit/interfaces/test_web_api.py -q --tb=short --maxfail=3 -p no:cacheprovider`：23 passed；`node --test tools/test-flow/tests/rest-api-guide.test.mjs tools/test-flow/tests/website-agent-guide.test.mjs`：46 passed。现有用例直接覆盖文档接口、快照字节和字段表一致性。
- 推送门槛：在真实 PostgreSQL 环境核对归档表、迁移和清理事务，先查看 `dev.default --plan-only`，再由仓库入口验证最终源码。适配器导入、上述专项或历史 PASS 均不能替代本轮权威 `verdict.json`；正式结果仅回填 PL-FIX-070 的本轮元数据行，不在此预写数据库计数或完成结论。
- 2026-10-16 真实 PostgreSQL 15.19 专项：`python -m pytest tests/deterministic/postgres/test_postgres_web_retention.py tests/deterministic/postgres/test_postgres_web_import.py -q --tb=short --maxfail=3 -p no:cacheprovider`：11 passed。覆盖报告 BYTEA 原字节、清理后重启、365 天历史、共享附件、主动删除与历史清单竞态，以及旧反馈派生列和归档的完整导入；每例新建独立测试库并保留证据。该专项不替代完整 `dev.default`。
- 首轮完整 `dev.default`（`run-20261016T021933Z-e298f83c`）发现 9 项旧测试未适配新接口或保留策略，结果为 FAIL，证据保留。已更新清理与 ENOSPC 测试替身、HTTP 文件读取接线、ZIP 受控入口、PostgreSQL 附件元数据预期及公开配置精确清单，原有权限和资源保护断言保留。修正后专项：Linux `test_generic_log_messages.py` + `test_report_followup_faults.py` 39 passed，HTTP `test_agent_file_access.py` + `test_agent_sharing.py` 46 passed，真实 PostgreSQL `test_followup_database_concurrency.py` 10 passed，`test_no_archive_processing.py` 4 passed，`test_release_boundaries.py` 3 passed；均使用 `python -m pytest <上述文件> -q --tb=short -p no:cacheprovider`。这些结果仍只证明专项，待重新运行最终源码的完整验证。
- 2026-10-16 推送前复核：前轮完整验证因 300 秒性能门槛失败；已完成同输入旧版—新版—旧版对照及文件同步调用采样，未发现明显源码回退。此前基于远端 `a84bc45` 隔离保留策略核查；按用户最新授权，本轮将其与持续诊断、意图分流及性能修复统一交付，保留长期统计修复及所有历史失败证据。最终工作区须重新查看 `dev.default --plan-only` 并运行完整验证，最终状态以 PL-FIX-070 及 PL-FIX-083 至 PL-FIX-089 的最新 verdict 元数据为准，不沿用历史 PASS。
- 确定性夹具性能修正：`_Stack.http_app` 的按需单次装配专项先复现失败，再修为惰性缓存；`.venv/Scripts/python.exe -m pytest tests/deterministic/journey/test_rpc_timeout.py::test_stack_http_app_is_built_once_only_when_requested tests/deterministic/integration/test_website_agent.py::test_first_problem_creates_case_before_one_requirement_aware_intake tests/deterministic/journey/test_rpc_timeout.py::test_same_job_uses_initial_order_fact_and_survives_restart -q --basetemp=.tmp/lazy-http-after-20261016 -p no:cacheprovider`：4 passed。只消除测试夹具的重复装配，测试范围、业务断言和正式性能门槛不变；本轮完整结论仍待最终统一验证。
- 测试替身导入最终专项：`test_fake_agent_import_does_not_load_unused_phase_helpers` 先复现失败；`fake_agent.py` 将 Logparse 导入推迟到预处理，移除未使用的 Outcome helper。`.venv/Scripts/python.exe -m pytest tests/deterministic/journey/test_rpc_timeout.py::test_stack_http_app_is_built_once_only_when_requested tests/deterministic/journey/test_rpc_timeout.py::test_fake_agent_import_does_not_load_unused_phase_helpers tests/deterministic/journey/test_rpc_timeout.py::test_r01_r14_rpc_timeout_is_one_durable_cross_module_path tests/deterministic/journey/test_rpc_timeout.py::test_same_job_uses_initial_order_fact_and_survives_restart tests/deterministic/integration/test_website_agent.py::test_first_problem_creates_case_before_one_requirement_aware_intake -q --basetemp=.tmp/fixture-perf-final-20261016 -p no:cacheprovider`：6 passed，零真实模型调用。Linux 交错 A/B 测量保留于 `/home/xiaodao/quick-validation/continuation-fixture-profile-20261016/results.json`；仅减少测试启动开销，不外推完整验证或产品响应速度，最终结论见 PL-FIX-070。
- 本轮文档复核已确认并修正统计记录中的两个本机 XML 链接：证据路径仍保留，改用路径文本，避免干净检出依赖未入库文件。直接回归 `node --test tools/test-flow/tests/docs-drift.test.mjs`：4 passed；本轮正式结果仍统一见 PL-FIX-070 最新 verdict 元数据。
- 生产接入尚待验收：备份后确认两项保留期和磁盘阈值，核对旧数据升级、正文与原报告下载、资源清理后的只读状态和页面提示。开发专项未调用真实模型，不代表线上数据库、生产日志效果或 Release 已验收。

## 回答反馈与纠正经验接入验收（2026-10-16）

- 开发分支 `codex/answer-feedback-learning` 保留原报告反馈，新增逐条追问反馈，并在现有追问入口接受纠正目标。后台按目标提炼或核对证据后更新经验，默认关闭，七个 MCP 输入不变。
- 专项通过，待推送前完整验证：运行时提炼/日志联动 93 项、纠正/追问/执行策略 53 项、反馈/数据库 74 项、旧新导入 3 项、服务组合闭环及提炼分流 15 项、HTTP 35 项、网站和文档相关专项通过；完整检查发现的合同清单补齐后相关 HTTP 38 项通过，清理及取消检查修复后的纠正专项 25 项通过；均无真实模型调用。推送前须对合并后的最终源码运行中央 `dev.default --plan-only` 与 `dev.default`，以最终 `verdict.json` 为准，不复用历史 PASS。
- 外部网站按[适配说明](docs/website-answer-feedback-upgrade.md)更新 BFF 和 SDK，按钮按各回答的 `can_rate` 显示；接入纠正时显式传 `intent=CORRECTION`。现有报告客户端无需改动。
- 真实通用 Skill 验收仍待执行：先查看对应真实模型入口 `--plan-only`，再用同组问题比较首次定位与经验召回后的步骤、耗时、准确性和错误纠正效果。确定性测试不代表真实诊断质量已提升。

## 网站会话分享生产接入验收（2026-10-14）

- 前端按[分享接口说明](docs/website-agent-api.md#分享只读访问)生成带 `share=true` 的链接，分享页保留登录态，按只读能力隐藏操作入口；下载和历史轮次读取继续传递分享参数。
- 上线后用两个不同账号核对详情、报告和文件下载，确认登录过期、删除会话、写操作拒绝等行为。开发专项及本次正式验证记录见 `FIXED_ISSUES.md` 的 PL-FIX-081；生产接入验收尚未完成。

## 会话长期统计生产接入验收（2026-10-14）

- 2026-10-16 已修复参考包的报告追问漏计（`PL-FIX-082`）。此前专项通过，独立统计提交时按用户明确要求跳过该次推送前完整验证；本次合并交付保留该提交，最终统一验证见 PL-FIX-070 本轮元数据。未接入本项目业务，也未部署内网。替换步骤和追问提交后的挂接示例见[接入指南](usage-stats-reference/INTEGRATION.md)。
- PostgreSQL 15.19 最终专项：`.venv/Scripts/python.exe -B -m pytest tests/deterministic/postgres/test_usage_stats_reference.py -q --tb=short --maxfail=3 -p no:cacheprovider --basetemp=.tmp/usage-stats-final-20261016 --junitxml=.tmp/usage-stats-final-20261016.xml`：**21 passed**，证据 `.tmp/usage-stats-final-20261016.xml`。使用现有本机专项管理库，每例独立创建数据库；没有调用真实模型。覆盖两表计数、重复及并发请求、全部追问状态、拒绝与回滚、超时补录、终态前追问、旧 run、陈旧快照、1001 条批次、源清理、元数据保留和旧返回列名兼容。
- 修复前首例已复现 `1 != 2`，证据 `.tmp/usage-stats-before-20261016.xml`。最终专项已包含元数据和关闭前返回值的补充断言。参考包指南及本次 TODO/台账条目的相对链接检查、`git diff --check` 通过；不涉及历史台账链接的完整校验。
- 已提供[生产接入指南](usage-stats-reference/INTEGRATION.md)和参考脚本；尚未挂接业务入口、持续补录或清理保护。
- 在内网核对 run 字段、会话最初创建时间、终态、最外层提交位置、统计时区和 Dashboard 授权范围，再适配脚本。
- 使用源数据仍完整的样本完成指南中的 PostgreSQL 专项验收，并记录历史覆盖范围。此前仓库 Dev 验证不代表该参考包已完成生产验收。

## 网站 Redis 生产接入验收（2026-09-28）

- 按[生产适配步骤](docs/website-redis-deployment.md)更新后端和 BFF，补充 `WEBSITE_REDIS_HOST` 内网 IP，以及实际端口、DB 和登录凭据。
- 用真实 Cookie 验证 `sessionid → airobot2-session:{session_id} → user.userid`，核对工号和历史会话归属。当前没有生产 Redis 地址，真实联调待部署时完成。
- `/ready` 不检查 Redis，验收须带真实 Cookie 请求网站 API。Redis 身份接入本身不改变业务数据格式；当前累计版本的 PostgreSQL 配置与历史数据迁移仍须按[生产升级清单](docs/production-upgrade-2026-09-23.md)完成，不能据此跳过迁移。


## P0：8.0 网站 Agent 真实 Release 与内部网站联调

- **2026-09-17 / 8.1.0 测试路径核对**：已补充两个独立的零模型 MCP/Case REST 诊断 Gate，覆盖实际协议与生产业务闭环；这不证明真实 Agent 遵循 Skill。当前 `cross-job-core.mjs` 的 route/upload/diagnose 走网站，`runClaude` 只用于重启后查询。仍需补齐用户 Agent 从加载正式 `problem-locator-client`、新问题建单、按权威 requirements 追问、补参和上传，到诊断、报告下载的完整路径，并与网站路径分别保存验收结果。不得把旧 Phase prompt/validator 的单测当成实际执行证明，也不得用旧 provider model-cert 替代。
- 本轮官方 `release.full --client windows --plan-only` 未指定 Claude CLI、env-only settings、Logparse 和外部 MCP 源码，准入为 BLOCKED；计划为正常/最多 7 次模型调用、费用上限 22 美元，未执行模型。继续前须明确实际测试环境和这些输入，审阅新计划；若扩展真实 MCP 旅程，必须同步调整 Proof/Stage/Gate、身份、调用预算和 Linux 服务端事件取证，仍遵守客户端直连边界。

- Agent REST、自然语言追问、持久 SSE、报告下载和网站后端示例已实现。正式 Dev 验证以当前源码快照的 `verdict.json` 为准。
- `release.full` 已改为一条网站原话 → 建案与权威追问 → 附件上传 → Reviewer → 报告下载 → 重启检查旅程；中间不再重启丢失活动 Case。首条非空原话直接建案，补充字段和提交附件各调用一次 INTAKE，正常/硬上限均为 7 次真实模型调用，不允许 repair。
- 2026-09-07 的官方 `--plan-only` 因当时 Docker Linux daemon 未启动、外部依赖未对齐而未进入真实模型。fresh Release 仍须从全新 V11 数据根执行，不能用 Dev 检查代替。
- 2026-09-08 本地 Docker 已恢复，Windows Client、Linux 镜像、Claude 2.1.89 和 DeepSeek Flash 测试配置已对齐。`dev.real` 的完整 Web CrossJob 前几轮被过期的安装版本/Skill 哈希断言、旧 `state.json` 夹具初始化，以及并行修改中的网站测试拦住；这些轮次均未进入真实网站与模型生成。已修正平台夹具并等待网页先建案改动完成，后续按合并后的源码重新规划并执行完整旅程，结果以对应 verdict 为准。服务端 INTAKE 对 Markdown 围栏的拒绝已在本地复现，仍需结合真实 Web 输出与内网实际模型核对。
- 合并后的完整旅程计划已通过环境准入，但实际执行被自动审批拒绝：需用户明确批准向 `api.deepseek.com` 发送本地测试提示词、Skill 和诊断材料，最多 7 次调用、费用硬上限 22 美元。批准前只运行零真实模型的正式 Dev 检查；Linux 平台修正和完整真实 Web 旅程仍待复验，不以计划或确定性通过代替。
- **本轮验证元数据**：Dev `run-20260908T081857Z-5a9f18d6` 为 `FAIL`；默认基线下 affected 597 passed / 24 skipped，但 66.806 秒超过 60 秒门槛，full 未运行。源码快照 `git-visible-worktree-v1:5921b281a8e2cadb6005c99787a88fdaa301c425195767f9031ce9534f502e41`，源码核验 `PASS`、真实模型调用为 0；继续前需解决该耗时阻塞并取得上述外部模型授权。此行是验证后的状态回填，不属于所引用快照。
- 网站开发者需配置 Cookie 透传和后端 Redis 会话读取；显式旧模式才需要网站登录回调。未经这些接入和真实 Release，不宣称已在内部网站生产可用。
- **推送前 Dev 复验元数据（2026-09-08）**：`run-20260908T082640Z-197109e5` 为 `PASS_WITH_WARNINGS`，基线为网站引入提交 `8b53bc5`，受影响范围按既定规则移交全量；完整确定性阶段、功能、运行及源码核验均通过，性能基线尚未校准。源码快照 `git-visible-worktree-v1:5167e26963def74008b0e1c760d7e7e6c8c73377393a83d04780ef098aacaf28`，零真实模型调用。此前默认快速阶段超时记录保留，外部模型授权、完整真实 Web 旅程和 Linux 平台复验仍待完成。本行是验证后的元数据回填，不属于所引用快照。

## P0：Methods V1 Reviewer 最长链路 Release

- 当前零模型 Core 和 SameJob 已恢复 Methods V1 报告验证；`release.full` 也已切到
  `SPECIALIZED_REVIEWER_ENABLED=true`，要求实际观察 `REVIEWING`，并由真实浏览器核对
  `diagnosis-result.json`、`result.zip`、Content-Length、SHA-256、原始日志字节和重启重放。
- 旧 Evidence V2 provider model-cert 不代表 Problem Locator 6.0.0 的专有运行时，不能作为 V9 发布
  结论或复用来源。后续若保留这些工具，必须明确标为历史实验；若要成为正式认证入口，需按当前
  Methods V1 草稿、Candidate、可选 Review 和用户报告合同重新实现。
- 当前待办是审阅 `release.full --plan-only` 的模型身份、调用数、预算、外部源码和环境 blocker，
  然后在依赖齐备的受支持主机上执行 fresh Release，取得绑定当前源码快照的权威 `verdict.json`。

## P0：Generic V2 最终集成与生产验收

- **2026-09-22 通用日志接入上线验收**：部署前核对 Linux 实际 Logparse 的解析目录、日志格式及 `GENERIC_LOGPARSE_PRODUCT`。在局域网通用 Skill 中应用只读清单与补充描述适配，用“问题中没有、只在日志里出现”的线索验证实际文件读取、关键摘录与报告结论，并覆盖大日志按需读取。先审阅对应真实模型入口 `--plan-only` 的身份、调用数、token/cost 预算及 blocker，再执行获准的验收；仓库确定性测试或输出协议握手不替代真实日志分析。实现的 Dev 验证单独记录于 PL-FIX-072，不将其登记为生产验收完成。
- C 变更集提供 V1 兼容、完整 Markdown V2、服务端 `GENERIC_REPORT` 产物和局域网适配 Skill；最终发布前仍须与其他并行变更合一，并由主控对合并后的同一源码快照执行 fresh `release.full`。不得复用 C 的 Dev verdict 冒充 Release。
- 局域网管理员须在私有通用定位 Skill 内应用最小 framework-mode 适配，并在同一 Linux 服务账号、Agent、settings、模型和工具身份下运行本地 A/B 验收。收据只保留 Skill tree 摘要与显式版本、输入/结果的 size/hash/状态、两次相同的运行身份 manifest 摘要和本地人工语义 verdict，不保存或上传私有 Skill、报告正文、prompt、路径或执行输出；不得把两个随机模型调用的报告 hash 相等作为默认门槛。
- 只有合并后的 Release verdict 与局域网生产验收都完成后，才在 `FIXED_ISSUES.md` 登记本问题的最终修复记录与权威 verdict；本并行任务不写“已修复”或占位 verdict。

## P0：Diagnosis Skill 条件性可选参数

- 现状核对：`runtime/diagnosis_runtime.py::_methods_user_input_projection` 按角色是否必需、是否已提供
  角色事实激活输入，包内其余声明输入仍按必需项处理；尚未形成按诊断分支激活参数的通用合同。
- Diagnosis Skill 必须支持条件性可选参数。参数未命中其声明的诊断分支时，不得成为 OPEN requirement，也不得阻塞路由、诊断、Review 或结果交付；只有进入指定分支且该分支确实依赖该参数时，Runtime 才向用户索要。
- 分支激活条件必须由 Skill 显式声明、可机读，并写入审计与 replay 输入；不得由 Agent 临时发明分支、用空字符串或隐藏默认值冒充未提供参数，也不得依赖客户端 Hook 修正语义。
- 条件参数若已作为初始 USER_FACT 提供，应直接固定并复用，不得重复询问；若未提供，分支激活后才创建一次可补充的 OPEN requirement。
- 生成器、manifest/合同、Catalog、Coordinator、服务端验证器和正反向测试必须共同覆盖“命中分支才询问、未命中分支不询问且不阻塞”。

## P1：下一轮性能优化与目标机复测

- Specialist marker 索引已实现，但正式 Dev 验证未完成：`run-20260907T143410Z-1b5e8749` 的 affected 535 passed / 24 skipped，60.637 秒超过既有 60 秒门槛，full 未运行；此前同快照为 61.710 秒。继续定位测试运行开销时保留两次 FAIL，不扩大测试选择或放宽门槛来获取 PASS。详细源码身份与 verdict 见 `FIXED_ISSUES.md` 的 PL-FIX-053 本轮元数据。本行是验证后的状态回填，不属于所引用快照。
- 2026-09-07 用户确认当前慢请求为固定内部模型的单轮 Specialist，无 Reviewer：BACKEND_EXECUTE 677 秒、API 656.1 秒、input/output 43,413/20,281 tokens，thinking 65.4 KB、最终 JSON/text 1.4 KB。优先在该环境对比有无完整 marker 索引的 thinking、API 时长和诊断质量；输出协议压缩不是此例首选。原始日志、部署身份及真实 A/B 尚未取得，不能从本地确定性测试外推分钟级降幅。
- 2026-09-07 已按 `8b53bc5` / 8.0.0 核对模型、Reviewer、网站会话、附件和 Logparse 路径；当前复现证据与目标机测量口径见 [`docs/performance-v11.md`](docs/performance-v11.md)。待评估项包括 Reviewer 重复上下文与文件工具回合、网站单 INTAKE worker、SSE/空闲扫描重复加载全历史，以及会话附件导入的重复 hash/复制。网站测量须从收到消息开始，Case brief 不覆盖首次 INTAKE。
- 第一轮保留自动 ROUTE 和 Agent CLI。直接模型 API、常驻 CLI 进程池、Logparse 多 target 批处理分别评估，避免一次改变模型协议、进程生命周期和日志解析合同。
- 7.0 原生 WSL 单样本已将 ROUTE/Specialist 降为各 1 turn、零文件工具，但 Specialist 约 48 秒，较前次约 18 秒更慢。下一轮对同输入重复采样，区分提示词、推理输出长度、缓存和服务负载；评估模型推理设置时同时检查证据与诊断质量，不仅追求总耗时下降。详见 `docs/performance-v10.md`。
- 在目标 4 核、8 GB Linux Server 上采集并发 1/2/3 Case 的吞吐、队列 P50/P95/P99、进程树峰值 RSS、磁盘读写量和上传吞吐。默认 ROUTE 1、DIAGNOSE 2、Logparse 1、ZIP 1 只是起点，需要真实负载校准。
- 继续拆分客户端“等待材料”的 108 秒：分别核对 ROUTE 返回、准备附件、客户端 hash、上传接收、发布和 submit。当前没有目标机详细事件，不能把两段约 54 秒归因于某个服务端步骤，也不能宣称已消除。
- 超过 128 KiB 的 Specialist 输入保留受控完整读取。后续评估证据包或更长上下文时，保留完整方法卡、目标日志和逐行证据核验，不静默截断。
- Logparse 新产物树仍有多次跨边界完整核验；可评估首次核验后使用受控只读 stage 和清单复用，但不能改变原始日志、marker、来源和行号的判定依据。
- 若后续需要跳过 ROUTE，先设计明确的扁平 Skill selector 或单一专用入口。只有一个候选不代表它一定匹配问题。
- 更长轮询或进度流需要核对 Claude Host、局域网和代理的支持情况；当前维持 30 秒有限等待。

## P1：证据核验与诊断语义的保证范围（待评估）

- 已确认的实现边界：[`verify_method_diagnosis`](src/problem_locator/runtime/methods_grounding.py)
  核对方法归属、marker、来源、行号、原文和 identity token，并绑定冻结日志与回执；这些机械
  校验确认引用来源，summary 是否正确解释日志、因果关系和方法规则是否成立仍依赖模型判断。
- 专有 Job 默认冻结 `review_policy=NONE`，核验后的 Candidate 可直接交付；显式开启
  `INDEPENDENT` 后才由独立 Job/Workspace 的 Reviewer 复核。后续应明确产品对两种模式的质量
  承诺，并用相同输入比较误判、未解决率、耗时和模型成本，再决定是否调整审核策略。
- 建议补充“日志引用真实，但诊断规则或因果解释不成立”的反例，分别评估机械校验和语义审核。
  若业务需要更强的确定性保证，再设计可机读的方法规则及专项回归；不得把引用校验通过当成
  对全部诊断语义的机械证明。
- [`GenericLocatorExecutor`](src/problem_locator/runtime/generic_locator.py) 将原始问题交给预装
  Skill，校验结果格式、大小和哈希，不复用专有链路的逐行证据核验。其质量验收继续归入上方
  “Generic V2 最终集成与生产验收”，需要单独评估实际报告语义。

## P1：Methods V1 UNRESOLVED 真实分布

- 当前确定性测试已覆盖 Reviewer `REJECT`、`NEED_MORE_EVIDENCE`、证据不足和发布失败的收口行为，
  但这些覆盖样本不能代表生产分布。
- 后续在显式启用 Reviewer 的同身份生产运行中，只统计已脱敏的终态类别、证据缺口类别和方法数；
  不收集报告正文、原始日志、路径或身份 token。任何语义调整都必须先有足够样本和专项回归，不能
  用放宽核验或跳过 Reviewer 来降低 `UNRESOLVED` 比例。

## P1：日志抑制、限流与采样规则

- 当前版本只支持普通事件时间窗，不声明或推断日志抑制、限流或采样语义。
- 后续若业务 Skill 需要 75 秒或其他抑制机制，应新增显式、可机读的规则类型，并由 Skill 自己声明允许窗口方向、开闭边界、抑制键、最大间隔以及无日志时的可验证行为。
- 框架不得硬编码 75 秒，也不得在 Skill 未声明时自行放宽时间窗口。

## P2：核心大模块与历史实现的维护成本（待评估）

- 2026-09-05 基线中，[`contracts/models.py`](src/problem_locator/contracts/models.py) 约 6900 行，
  [`runtime/diagnosis_runtime.py`](src/problem_locator/runtime/diagnosis_runtime.py) 约 4600 行，合同
  校验和运行时流程集中在少数模块。生产主链路已采用 Methods V1，仓库仍保留大量 Evidence V2
  实现、测试和命名，后续修改容易混淆实际入口与历史路径。
- 后续先梳理生产入口、调用关系、历史或实验用途，以及它们对应的测试和文档，再决定是否拆分
  模块、统一命名或收敛旧路径。不能仅凭文件名中的 V2 判断代码无用，也不能把历史认证结果当成
  当前 V9 的发布证明；认证入口的收口继续归入上方“Methods V1 Reviewer 最长链路 Release”。
- 若实施重构，需保持公开扁平 MCP schema、当前合同版本、状态转换、冻结资产身份、权威产物和
  重启恢复行为，并用当前 Test Flow 验证同一源码快照。
