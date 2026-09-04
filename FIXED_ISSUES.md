# 已修复问题台账

更新时间：2026-09-03

本文件记录已经在当前工作区验证、修复并由专项回归测试保护的问题。活跃待办仍只写入
[`TODO.md`](TODO.md)；同一问题再次回归时更新原条目，不另建一个缺少历史关联的条目。

## 登记格式

每条记录必须包含：问题 ID、状态、症状、受影响版本、根因、不可回归行为、修复历史、
专项回归测试和最新 Test Flow verdict。只有能直接复现该问题的测试才算专项回归测试；
全量测试通过本身不能替代专项用例。

## PL-FIX-001：不兼容初始事实导致重复路由

- **状态**：已按 Methods V7 破坏性合同再次修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：旧版曾因 Case 携带专用 Skill 未声明的初始 USER_FACT 而在 ROUTE/DIAGNOSE 间
  无进展轮询；早期修复又把“当前事实名完全匹配”固化为目录预过滤，导致 Methods V7 的语义
  Router 看不到合法的已注册 Skill，额外事实错误地变成能力淘汰条件。
- **受影响版本**：`fddd170` 之前存在原始轮询；Methods V7 接入前的 4.x 候选仍保留旧目录
  预过滤语义。
- **根因**：原实现把“Skill 声明的待补输入”与“Router 可见的生产能力集合”混为一层；同时
  原始路径缺少对无语义进展 `REROUTE` 的确定性拒绝。
- **不可回归行为**：ROUTE 始终暴露全部已注册且身份有效的 production Methods Skills，由
  Router 做语义选择；额外 Case fact 保留在冻结 snapshot，但不得进入只含声明字段的
  `methods_request.json`。空目录仍在调用 Router 前发布 `NO_CAPABILITY`；无语义进展的
  `REROUTE` 必须被拒绝。
- **修复历史**：`fddd170 fix: terminate incompatible fact routing`；`6be7ce6` 完成集成合同与
  测试收敛；2026-08-24 的 Methods V7 hard cut 移除事实名目录预过滤，保留 no-progress
  REROUTE 防循环并将输入投影绑定到 registration。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_diagnosis_runtime.py::test_extra_user_fact_keeps_registered_route_candidate_for_semantic_router`
  - `tests/deterministic/unit/runtime/test_methods_output_pipeline.py::test_methods_request_projects_declared_inputs_and_keeps_extra_fact_in_snapshot`
  - `tests/deterministic/unit/runtime/test_diagnosis_runtime.py::test_empty_production_catalog_publishes_no_capability_without_router`
  - `tests/deterministic/unit/domain/test_coordinator_diagnosis.py::test_zero_actionable_requirement_reroute_without_progress_is_rejected`
- **最新 Test Flow verdict**：Dev `run-20260824T044518Z-4467c837`，`PASS_WITH_WARNINGS`；
  functional、operation、verification 均为 `PASS`，performance 为 `NOT_CALIBRATED`；验证
  源码快照 `git-visible-worktree-v1:5d9d8f34f1addf5a04b8c2bc5b3933a6a6daae39d8365b5d742a9f6135ae245c`
  （641 files），framework Node 351 passed；deterministic 2348 passed、1 skipped。

## PL-FIX-003：纯通用部署因空 SKILL_DIR 被拒绝启动

- **状态**：实现已合入，待当前 B+C 合并快照的 fresh Release 验证。
- **症状**：局域网 Linux Server 只部署通用定位 Skill、将必填 `SKILL_DIR` 指向实际空目录时，
  服务启动返回 `CONFIG_INVALID`，要求至少存在一个 `PRODUCTION` Diagnosis Skill。
- **受影响版本**：Problem Locator 3.0.0，当前基线 `d29b6f2`。
- **根因**：生产 catalog 在完成安全扫描后无条件要求至少一个 `PRODUCTION` 专用 Skill，未允许
  已由 Runtime 支持的“零专用候选后确定性转入 GENERIC DIAGNOSE”部署形态。
- **不可回归行为**：`SKILL_DIR` 仍为必填的实际绝对目录且禁止符号链接；目录可以为空，空目录
  产生零路由候选并转入通用定位；任何 `TEST_ONLY` Diagnosis Skill 仍使生产启动失败。
- **修复历史**：2026-08-17 当前变更移除最少一个 `PRODUCTION` Skill 的启动限制，保留目录安全
  与 `TEST_ONLY` 拒绝规则，并补充生产 composition 回归测试。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_catalog.py::test_production_catalog_allows_empty_skill_directory_for_generic_only`
  - `tests/deterministic/integration/test_bootstrap_composition.py::test_production_app_starts_with_empty_diagnosis_skill_catalog`
  - `tests/deterministic/unit/runtime/test_diagnosis_runtime.py::test_empty_route_candidate_set_publishes_no_capability_without_backend`
- **最新 Test Flow verdict**：`run-20260817T104736Z-aaa3c1e0`，`PASS_WITH_WARNINGS`；
  functional、operation、verification 均为 `PASS`，performance 为 `NOT_CALIBRATED`；
  验证源码快照 `git-visible-worktree-v1:1c25acc7e7bbdec657b1f1c16b0992750d1320b947a06f7638bb0245a14357a7`
  （582 files）。

## PL-FIX-002：参数与附件混合等待被拒绝为 OUTCOME_INVALID

- **状态**：已修复；Methods V7 接入中再次发现并修复顺序回归，是否验证通过以本条
  “最新 Test Flow verdict”为准。
- **症状**：同一 DIAGNOSE 轮次同时缺少 INPUT 和 ATTACHMENT 时，Agent 按输出合同生成
  `NEED_INPUT`、`requested_input` 和 `requested_attachments`，Server Verifier 却只激活
  INPUT，最终把合法 Outcome 归一化为 `OUTCOME_INVALID`。
- **受影响版本**：最初缺陷存在于 `4e9d381` 之前；`be61e9a` 的 requirement activation
  重构首次回归；Methods V7 接入候选又会按 opaque requirement UUID 排序，破坏 Skill 声明顺序。
- **根因**：`resolve_requirements()` 在存在缺失 INITIAL INPUT 时丢弃了同时缺失的 INITIAL
  ATTACHMENT，而输出合同、Pydantic 合同、Finalizer 和 Coordinator 仍允许混合等待；原
  Server Verifier 回归测试也在同一重构中被改成了仅 INPUT 场景。
- **不可回归行为**：混合等待使用 `NEED_INPUT`；`requested_input` 依 Skill 顺序包含全部缺失
  INITIAL INPUT，`requested_attachments` 随后包含缺失 INITIAL ATTACHMENT。Case 先进入
  `WAITING_INPUT`，参数补齐后直接进入 `WAITING_ATTACHMENT`，期间不创建中间 DIAGNOSE Job。
  只有没有待补 INPUT 时才使用 `NEED_ATTACHMENT`；Coordinator 与 Formalization 必须原样
  保持已经验证的 registration 顺序，不得按 UUID 或显示文本重排。
- **修复历史**：`4e9d381 fix: accept mixed input and attachment waits` 首次修复；`be61e9a`
  回归；2026-08-17 恢复完整激活集合与端到端旅程；2026-08-24 Methods V7 hard cut 删除旧
  activation 层后，在 Coordinator/Formalization 同时移除 UUID 排序并迁移等待态输出合同。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_diagnosis_runtime.py::test_methods_preflight_publishes_waiting_without_backend_or_broker`
  - `tests/deterministic/unit/domain/test_coordinator_diagnosis.py::test_need_input_accepts_multiple_inputs_and_attachment_in_one_wait`
  - `tests/deterministic/unit/application/test_formalization.py::test_builds_pending_job_from_final_state_and_projector`
  - `tests/deterministic/journey/test_rpc_timeout.py::test_r01_r14_rpc_timeout_is_one_durable_cross_module_path`
- **最新 Test Flow verdict**：Dev `run-20260824T044518Z-4467c837`，`PASS_WITH_WARNINGS`；
  functional、operation、verification 均为 `PASS`，performance 为 `NOT_CALIBRATED`；验证
  源码快照 `git-visible-worktree-v1:5d9d8f34f1addf5a04b8c2bc5b3933a6a6daae39d8365b5d742a9f6135ae245c`
  （641 files），framework Node 351 passed；deterministic 2348 passed、1 skipped。

## PL-FIX-004：通用/专用定位全链路耗时缺少可解释归因

- **状态**：实现已合入，待当前 B+C 合并快照的 fresh Release 验证。
- **症状**：通用定位、专用定位及 REVIEW 的 Agent 阶段可持续数十至上百秒，
  `brief.log` 仅有总耗时，`detailed.log` 需要人工对齐原始事件，无法直接区分用户等待、
  排队、Agent backend、模型 API、CLI 非 API、Logparse 或本地阶段。
- **受影响版本**：Problem Locator 3.0.0，当前基线 `8a9b127`。
- **根因**：Journey 只记录了 Job 和局部 Stage 的基础时间；Agent stdout 没有安全的
  `stream-json` 元数据观察器，Logparse 没有操作/子进程时间事件，renderer 也没有区间
  并集和父子 exclusive time 模型。
- **不可回归行为**：
  - 七个公共 MCP 工具与扁平输入 schema 不变，不增加客户端 Hook、代理或客户端 DFX。
  - Agent 遥测只观察现有脱敏链后的 stdout，不记录 prompt/模型正文、工具输入输出、
    调用 ID 或密钥；任何解析异常不得改变 Agent 的成败、取消或退出码。
  - 服务不修改 `CLAUDE_COMMAND`；非受支持 `stream-json` 时保留基础耗时，并明确输出
    “Agent 细分不可用”和稳定降级原因。
  - brief 与 detailed 按 Case 墙钟时间输出 Top 3 主要耗时来源；嵌套/并发区间
    不重复计时，thinking/text/工具只作为不可加和的服务端观察窗口；缺失或不一致时
    显式标记，不生成负残差，不强行凑满 100%。
  - Logparse 仅记录受控操作名、PARSE/TARGET 子阶段、序号、耗时、状态和错误码，
    不记录路径、请求或解析结果。
- **修复历史**：2026-08-17 当前变更增加失败隔离的 Agent 流式遥测、Logparse 操作/
  子阶段事件、Journey 非重复计时与两级 Agent 可加口径，并扩展原有 brief/detailed
  renderer、DFX Skill、README 及 Test Flow 安全归一化；同时将真实 Agent backend Gate
  从错误地假定直接 `claude` 可执行文件，改为校验实际的 Node 审计 wrapper 及其
  `--claude-entry` 官方 CLI。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_agent_telemetry.py`
  - `tests/deterministic/unit/runtime/test_agent_backend.py`
  - `tests/deterministic/unit/test_journey_renderer.py`
  - `tests/deterministic/unit/integrations/test_logparse_fake_e2e.py::test_fake_logparse_bridge_e2e`
  - `tests/deterministic/unit/interfaces/test_trace_skill.py`
  - `tools/test-flow/tests/events-status.test.mjs`
  - `tests/real/agent/test_real_agent_backend_gate.py::test_real_claude_code_writes_exact_agent_outcome_through_backend`
- **最新 Test Flow verdict**：fresh Release `run-20260818T030707Z-26372dce`，
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；server-dfx、real-agent、real-logparse 与 fresh CrossJob proofs 均为 `PASS`，
  全部 CrossJob stage 实际 `EXECUTED`。5 个服务端 Agent 遥测均为 `COMPLETE` 且
  `content_included=false`；Logparse `parse-targets`/`target-logs` 及 PARSE/TARGET 子阶段均有
  受控耗时事件。验证源码快照
  `git-visible-worktree-v1:d852cee96757200d121e5bd7db53addaa8c0242c1eba6b6181f9f26452fd4140`
  （585 files）。

## PL-FIX-005：Linux 安装包能力 Gate 因过期 Skill product hash 失败

- **状态**：已修复。
- **症状**：fresh Release 在 `platform.server-linux-capability` 阶段失败；密封安装包能成功
  导入和启动，但 `diagnose-service-takeover` 的实际 product hash 为 `7f0447…`，测试仍
  期待 `abd24d…`。
- **受影响版本**：`be61e9a` 将该 fixture 从 generator v5 升级为 v6 后，包括当前
  3.0.0 基线 `8a9b127`。
- **根因**：fixture 的 `SKILL.md` 和 `diagnosis-skill.json` 都已更新，安装包能力
  测试中对应的冻结 `TAKEOVER_PRODUCT_HASH` 未同步。
- **不可回归行为**：Linux 密封安装包中的接管 Skill 必须与当前仓库 fixture 整体
  product hash 一致；任何 Skill 字节变化都必须显式更新该能力合同，不得跳过检查。
- **修复历史**：2026-08-17 根据 Windows 工作树和 fresh Release Linux 快照的一致计算结果，
  将冻结 hash 更新为 `7f0447460e4a56f882a1f46493ceb645930c0a527bccb303c7929a1d7b3cbe9e`。
- **专项回归测试**：
  - `tests/platform/distribution/test_installed_distribution_gate.py::test_clean_installed_distribution_import_cli_and_server_gate`
- **最新 Test Flow verdict**：fresh Release `run-20260818T030707Z-26372dce`，
  `PASS_WITH_WARNINGS`；`platform.server-linux-capability`、functional、operation、verification
  均为 `PASS`，performance 为 `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:d852cee96757200d121e5bd7db53addaa8c0242c1eba6b6181f9f26452fd4140`
  （585 files）。

## PL-FIX-006：Release CrossJob 将可选初始参数误判为输入漂移

- **状态**：已修复。
- **症状**：真实 Skill 生成通过后，fresh Release 在 `journey.cross-job.environment` 以
  `RELEASE_CASE_INITIAL_INPUT_DRIFT` 立即失败，未进入 route/upload/diagnose。
- **受影响版本**：`be61e9a` 引入 INPUT `requiredness` 和可选 role pid 后，包括当前
  3.0.0 基线 `8a9b127`。
- **根因**：CrossJob adapter 仍要求 Skill 的全部 INITIAL INPUT 名称与 driver 的初始事实
  数组同序全等，因而错误地要求可选 `client_pid`/`server_pid`，也与公共扁平平行
  数组允许的任意名称顺序不一致。
- **不可回归行为**：driver 只能提供 Skill 声明的 INITIAL INPUT，必须覆盖全部
  `REQUIRED` 输入，可选输入可省略，名称顺序不影响合同；AFTER_LOGPARSE 补充名称仍必须
  与当前旅程声明的集合一致。
- **修复历史**：2026-08-17 将同序全等改为声明集合/必填覆盖检查，保留未知输入、
  缺少必填输入、重复名称及补充集合漂移的拒绝。
- **专项回归测试**：
  - `tools/test-flow/tests/release-case.test.mjs` 中
    `Release driver may omit optional initial facts and reorder the declared inputs`
- **最新 Test Flow verdict**：fresh Release `run-20260818T030707Z-26372dce`，
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；全部 CrossJob stage 实际 `EXECUTED` 并为 `PASS`；验证源码快照
  `git-visible-worktree-v1:d852cee96757200d121e5bd7db53addaa8c0242c1eba6b6181f9f26452fd4140`
  （585 files）。

## PL-FIX-007：Skill 生成合同未明确无枚举值时禁止 null

- **状态**：已修复。
- **症状**：fresh Release 的真实 Skill 生成 Agent 成功完成受控读取和唯一 Write，随后
  GenerationSpec v6 加载以 `INPUT allowed_values are invalid` 失败；生成结果在无枚举限制的
  INPUT 上写入了 `"allowed_values": null`。
- **受影响版本**：Problem Locator 3.0.0，当前基线 `8a9b127`。
- **根因**：生成器只接受字符串数组，示例也使用 `[]`，但参考合同正文仅称其为“唯一非空
  字符串数组”，没有明确无枚举限制时必须使用空数组且 JSON `null` 非法，给真实模型留下了
  可空字段的解释空间。
- **不可回归行为**：每个 INPUT 的 `allowed_values` 始终为数组；没有枚举限制时写 `[]`，
  绝不能写 JSON `null`；有枚举限制时写 1..100 个唯一非空字符串。
- **修复历史**：2026-08-18 根据 `run-20260817T155219Z-9a6733e7` 的真实模型产物失败证据，
  在 GenerationSpec v6 参考合同中补全无枚举值的 canonical 表达，同步 Skill 交付收据及其
  上层 fixture manifest，并增加文档合同专项测试。
- **专项回归测试**：
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_wiki_conversion_contract_is_self_contained_and_business_neutral`
  - `tests/deterministic/unit/integrations/test_generator_copy.py::test_receipt_matches_source_and_every_delivered_byte`
  - `tests/deterministic/unit/integrations/test_logparse_fixture_manifest.py::test_logparse_fixture_manifest_matches_schema_dto_and_disk`
- **最新 Test Flow verdict**：fresh Release `run-20260818T030707Z-26372dce`，
  `PASS_WITH_WARNINGS`；`real.skill-generation`、functional、operation、verification 均为
  `PASS`，performance 为 `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:d852cee96757200d121e5bd7db53addaa8c0242c1eba6b6181f9f26452fd4140`
  （585 files）。

## PL-FIX-008：真实 Skill 生成未稳定执行 event-field 引用闭包

- **状态**：已修复。
- **症状**：真实转换 Agent 完成受控读取和唯一 Write 后，GenerationSpec v6 加载以
  `verification rule names an unknown event field` 失败，fresh Release 无法进入 CrossJob。
- **受影响版本**：Problem Locator 3.0.0，当前基线 `8a9b127`；相同症状见
  `run-20260817T152514Z-6b010c53` 和 `run-20260817T161812Z-7ffefe8b`。
- **根因**：Skill 和 verification reference 虽已声明抽象的 `(event, field)` 集合闭包规则，
  但没有把 validator 覆盖的各字段引用位置展开成低自由度清单，也没有给出“字段存在于另一个
  event 仍然非法”的具体正反例；真实模型未稳定执行抽象检查。
- **不可回归行为**：唯一 Write 前必须内部展开 FACT_FIELD_EQUALS、FIELDS_EQUAL、
  CROSS_ROLE_CORRELATION、EVENT_ORDER join、NUMERIC_COMPARE 递归 FIELD/join 的所有
  `(event, field)` 引用；每个 field 只能属于该行 event，禁止跨 event 借用或近似命名。
- **修复历史**：2026-08-18 增加逐引用内部清单、完整字段引用位置和正反例，并在真实 Gate
  prompt 中显式要求执行相同检查；`run-20260818T011500Z-8004e6d9` 仍复现同一失败后，
  增加只含 Rule 序号、受控 kind 与引用序号的隐私安全诊断，不记录 event/field 名称、模型正文、
  工具参数或返回值；`run-20260818T012918Z-12d7d408` 将失败定位到
  `rules[9] FIELDS_EQUAL reference[1]`，据此增加“Equality 两侧允许不同字段名、每侧必须使用
  自己 event 声明字段”的精确正反 JSON 示例。不增加 Hook、工具或第二次 Write；
  `run-20260818T014451Z-8f6f0659` 的真实模型产物随后通过加载、编译、语义、工具审计、
  隐私扫描和完整 usage 检查。
- **专项回归测试**：
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_wiki_conversion_contract_is_self_contained_and_business_neutral`
  - `tests/deterministic/unit/integrations/test_generator_v3.py::test_unknown_event_field_error_reports_only_controlled_location`
  - `tests/real/agent/test_real_wiki_skill_generation_gate.py::test_real_conversion_agent_builds_an_executable_reviewed_skill_from_plain_wiki`
- **最新 Test Flow verdict**：fresh Release `run-20260818T030707Z-26372dce`，
  `PASS_WITH_WARNINGS`；`real.skill-generation`、functional、operation、verification 均为
  `PASS`，performance 为 `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:d852cee96757200d121e5bd7db53addaa8c0242c1eba6b6181f9f26452fd4140`
  （585 files）。

## PL-FIX-009：真实 Skill 生成只做静态闭包而未演算正向场景

- **状态**：已修复。
- **症状**：fresh Release `run-20260818T020202Z-9f3a7f1e` 的真实转换产物可以加载、编译和
  校验，但 complete 场景中 `complete_timeout_consistent` 得到 `NOT_APPLICABLE`，继而令
  `queue_contributed_timeout` 因机械前提缺失得到 `SEMANTIC_ONLY` issue；CrossJob 仍未运行。
- **受影响版本**：Problem Locator 3.0.0，当前基线 `8a9b127`。
- **根因**：转换 Skill 只要求 event-field 引用闭包和 TerminalPath DAG 静态可达，没有要求把
  标记外 Wiki 中的稳定日志消息体代入最终 regex、selector、多行组装和依赖顺序。真实模型因此
  可能产出结构完全合法、但正向 event 为零或机械依赖为 `UNKNOWN|NOT_APPLICABLE` 的规范。
- **不可回归行为**：唯一 Write 前，每个非 fallback `COMPLETE|PARTIAL` path 都必须仅用 Wiki
  标记外正文和权威澄清构造正向 witness；逐条执行最终 `line_pattern`/`match_mode`、多行顺序与
  group、selector、event count、Rule 依赖和 Equality occurrence tuple。路径所需 event 必须
  非零，机械依赖必须正向 ready；不得读取测试/oracle、伪造日志或把 witness 写入产物。
- **修复历史**：2026-08-18 先为场景失败审计增加只含受控 rule/event ID、枚举状态、稳定错误码
  和计数的 `evaluation_diagnostic`，明确禁止保存 issue prose、字段值、日志或模型正文；随后在
  Skill、verification reference 和真实 Gate prompt 中加入正向 witness 演算及通用正反例，并
  同步 Skill source-copy 与上层 fixture manifest。不增加 Hook、额外工具或第二次 Write。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_real_wiki_skill_generation_audit.py::test_rule_mismatch_diagnostic_reports_only_controlled_dependencies_and_counts`
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_wiki_conversion_contract_is_self_contained_and_business_neutral`
  - `tests/deterministic/unit/integrations/test_generator_copy.py::test_receipt_matches_source_and_every_delivered_byte`
  - `tests/deterministic/unit/integrations/test_logparse_fixture_manifest.py::test_logparse_fixture_manifest_matches_schema_dto_and_disk`
  - `tests/real/agent/test_real_wiki_skill_generation_gate.py::test_real_conversion_agent_builds_an_executable_reviewed_skill_from_plain_wiki`
- **最新 Test Flow verdict**：fresh Release `run-20260818T030707Z-26372dce`，
  `PASS_WITH_WARNINGS`；`real.skill-generation`、functional、operation、verification 均为
  `PASS`，performance 为 `NOT_CALIBRATED`；全部 CrossJob stage 实际 `EXECUTED` 并为
  `PASS`；验证源码快照
  `git-visible-worktree-v1:d852cee96757200d121e5bd7db53addaa8c0242c1eba6b6181f9f26452fd4140`
  （585 files）。

## PL-FIX-010：浏览器 REST 接入说明无法独立支撑前端实现

- **状态**：代码修复已完成；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：根 README 的“浏览器 REST API”标题在 Markdown 结构上继续包含远程工具、扁平
  参数和客户端配置；REST 正文只有两个请求示例，没有逐字段类型、含义、完整响应、状态动作、
  错误恢复或可执行浏览器流程。仓库中的 OpenAPI 文件也只保存 hash、操作名和 schema 名摘要，
  不能离线生成客户端或审阅字段。
- **受影响版本**：Problem Locator 3.0.0，当前基线 `f99a3d5`。
- **根因**：新增 REST 摘要时没有为既有后续内容恢复同级标题；接口类型只存在于代码生成的
  schema 中，路由和字段缺少语义元数据；版本化快照测试只冻结了摘要，没有建立“运行时合同、
  完整静态合同、人工指南”三者的等价关系。
- **不可回归行为**：浏览器前端只依赖独立 REST 指南和完整 OpenAPI 即可实现全部七个业务
  操作；指南不得包含跨协议工具或客户端配置，必须覆盖所有公开字段、Case 状态、错误码、幂等、
  revision、长轮询、附件和产物校验。运行时 `/openapi.json` 与版本化完整合同逐字节一致；
  OpenAPI 固定 operation ID、参数约束、含义、示例和响应头；现有业务 URL、请求、响应和状态码
  不变。
- **修复历史**：2026-08-18 新增 `docs/browser-rest-api.md`，把 README REST 区域缩为单一入口
  并恢复后续同级标题；补全 REST-only OpenAPI 元数据和完整 canonical 快照；增加框架无关
  TypeScript/`fetch` 示例、状态与错误动作表，以及指南/OpenAPI/Test Flow 身份防漂移检查。
- **专项回归测试**：
  - `tests/deterministic/unit/interfaces/test_web_api.py::test_openapi_and_swagger_publish_the_browser_contract`
  - `tests/deterministic/unit/interfaces/test_web_api.py::test_openapi_describes_every_parameter_and_reachable_model_field`
  - `tests/deterministic/unit/interfaces/test_web_api.py::test_openapi_uuid_metadata_preserves_application_validation_errors`
  - `tests/deterministic/unit/interfaces/test_web_api.py::test_openapi_examples_validate_against_the_real_rest_dtos`
  - `tests/deterministic/unit/interfaces/test_web_api.py::test_guide_json_examples_validate_against_the_real_rest_dtos`
  - `tests/deterministic/unit/interfaces/test_web_api.py::test_create_case_openapi_and_guide_examples_execute_through_asgi`
  - `tests/deterministic/unit/interfaces/test_web_api.py::test_openapi_contract_matches_versioned_snapshot`
  - `tools/test-flow/tests/rest-api-guide.test.mjs`
  - `tools/test-flow/tests/docs-drift.test.mjs::README keeps the browser REST API entry isolated from other protocols`
- **最新 Test Flow verdict**：fresh Release `run-20260820T045247Z-bbd8abff`，
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:9eacd6a22cec2cb37b88503a2663f0825eccca76397b31b5199af687ccfa2051`
  （603 files）。

## PL-FIX-011：冻结清单摘要与 Windows 工作树字节不一致

- **状态**：代码修复已完成；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：在 Windows 工作树中运行 deterministic contracts 时，
  `test_contract_manifest_covers_the_exact_frozen_inputs` 报告 `models.py` 和 `ports.py` 的实际
  SHA-256 与 `schemas/v2/contract-manifest.json` 不一致；完整 unit 随后还发现
  runtime-backend 的 `fake_claude.py` 以及 Release case 清单在 CRLF 工作树中发生大小和摘要
  漂移，而 Git blob 与权威清单使用 LF。
- **受影响版本**：Problem Locator 3.0.0，当前基线 `f99a3d5`；清单中的两个摘要无法匹配相应
  文件的任何 Git 历史版本。
- **根因**：合同源码和 Agent fixture 演进后，冻结清单只同步了部分条目，遗留了无法对应当前
  Git blob 的摘要和换行后大小；仓库也没有统一固定 tracked 文本的 checkout EOL，
  `core.autocrlf=true` 会让正确的 LF 清单在 Windows 对比 CRLF 工作树字节。
- **不可回归行为**：冻结清单的文件集合、顺序、大小和每个 SHA-256 必须精确对应当前
  canonical schema、合同源码与 Agent fixture 字节；所有 Git 识别为文本的 tracked 文件在
  各平台 checkout 均保持 LF。不通过跳过测试或更改产品字节掩盖收据漂移。
- **修复历史**：2026-08-18 把 `models.py`、`ports.py` 和 runtime-backend `fake_claude.py`
  的清单元数据更新为 HEAD 的实际 canonical LF 字节，并用 `.gitattributes` 的
  `* text=auto eol=lf` 固定跨平台 checkout；没有修改这些被冻结文件、schema 或 wire 行为。
- **专项回归测试**：
  - `tests/deterministic/contracts/test_schema_snapshots.py::test_contract_manifest_covers_the_exact_frozen_inputs`
  - `tests/deterministic/unit/runtime/test_agent_backend.py::test_backend_fixture_manifest_is_exact`
  - `tests/deterministic/unit/runtime/test_diagnosis_runtime.py::test_diagnosis_runtime_fixture_manifests_remain_contract_valid`
  - `tools/test-flow/tests/config-contract.test.mjs` 中 `Git checkouts and the current worktree preserve byte-pinned text as LF`
- **最新 Test Flow verdict**：fresh Release `run-20260820T045247Z-bbd8abff`，
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:9eacd6a22cec2cb37b88503a2663f0825eccca76397b31b5199af687ccfa2051`
  （603 files）。

## PL-FIX-012：Windows Test Flow 临时资源路径超过 MAX_PATH

- **状态**：代码修复已完成；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：Windows `dev.default` 的 affected pytest 在多个真实存储集成用例中统一返回
  `RESOURCE_STAGE_FAILED`；相同用例使用极短临时根时通过。
- **受影响版本**：Problem Locator 3.0.0，当前基线 `f99a3d5`。
- **根因**：Test Flow 虽已缩短 Windows pytest scratch 目录名，但 `--basetemp` 仍使用普通
  Win32 路径；pytest 的测试名、proposal hash、Job UUID 和原子临时文件名组合后超过传统
  `MAX_PATH`，底层文件创建失败。
- **不可回归行为**：Windows pytest 使用同一受控 scratch 目录的扩展长度绝对路径，固定
  SameJob/CrossJob 确定性旅程的受控数据根也使用扩展长度路径；不移动 scratch、不放宽清理
  边界，也不改变 Linux/macOS 路径。真实文件和目录 staging 集成测试必须能在标准 Codex
  worktree 深度下通过。
- **修复历史**：2026-08-18 为 Test Flow 增加跨平台 `pytestBaseTempPath`；Windows drive 和 UNC
  路径分别转换为 `\\?\` 与 `\\?\UNC\`，其他平台保持普通绝对路径；同时让两个复用 `.s08`
  数据根的确定性旅程在 Windows 使用相同扩展路径语义。
- **专项回归测试**：
  - `tools/test-flow/tests/actions.test.mjs` 中 `Windows pytest base temp uses an extended-length path without moving scratch`
  - `tests/deterministic/integration/test_bootstrap_resource_export.py::test_nonempty_state_export_is_complete_canonical_and_generation_consistent`
  - `tests/deterministic/journey/test_rpc_timeout.py::test_r01_r14_rpc_timeout_is_one_durable_cross_module_path`
  - `tests/deterministic/journey/test_rpc_timeout.py::test_same_job_uses_initial_order_fact_and_survives_restart`
- **最新 Test Flow verdict**：fresh Release `run-20260820T045247Z-bbd8abff`，
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:9eacd6a22cec2cb37b88503a2663f0825eccca76397b31b5199af687ccfa2051`
  （603 files）。

## PL-FIX-013：等待用户材料的 Diagnose Agent 因无意义逐规则 claims 超出 token cap

- **状态**：代码修复已完成；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：fresh Release 的 `journey.cross-job.route` 已完成 9 次客户端和 10 次服务端工具调用，
  但服务端 DIAGNOSE Agent 在只需返回 `NEED_ATTACHMENT` 时消耗 35 turns、2,329,988 个
  cache-inclusive tokens，超过固定 2,000,000 cap；外层 Docker adapter 随后把 usage audit
  失败折叠为 `DOCKER_COMMAND_FAILED:exec`，导致后续 Upload/Diagnose/Review 未运行。
- **受影响版本**：Problem Locator 4.0.0，B+C 合并候选
  `git-visible-worktree-v1:830bcd5b1071a9cf99e101962a430879c48f4a686b2d6cc9e2cc8b6ffe005df9`。
- **根因**：`AgentJobOutcomeDraftV2` 对全部非失败 DIAGNOSE 强制非空 claims，使纯等待态也必须
  机械展开 Skill 的全部规则。真实 Agent 首次写出了结构正确的 `NEED_ATTACHMENT` 和固定
  requirement，但 `rule_claims=[]` 被隐藏 model validator 拒绝；由于等待态本来没有附件、
  Logparse Evidence 或 Candidate，Agent 随后花费 24 次 Bash、8 次 Read 和第二次 Write
  反查安装包合同并构造 33 条不参与正向决策的 claims，cache-read 被多轮大上下文放大。
- **不可回归行为**：只有 DIAGNOSE 的 `NEED_INPUT|NEED_ATTACHMENT` 可使用空 claims 快路径；
  服务端仍须加载固定 Skill、重算全部规则并验证 requirement 声明与激活。其他非失败
  DIAGNOSE 和全部 REVIEW 必须提交与固定 Skill 数量、顺序、rule ID 完全一致的 claims；
  不提高 turn/token/time/cost cap，不把等待快路径扩展到 Candidate 或最终结论。
- **修复历史**：2026-08-20 根据 Release `run-20260820T030618Z-13f87ea1` 的 verdict、
  adapter receipt、保留 volume 中的 terminal usage 和工具轨迹，增加等待材料空 claims 快路径；
  同时在服务端 verifier 收紧非等待态/Review 的完整有序 claims 校验，并将 Diagnose output
  contract 升级为 5.1.0，明确禁止为发现输出形状反查安装包源码。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_outcome_finalizer.py::test_sealer_accepts_need_input_with_multiple_inputs_and_attachment`
  - `tests/deterministic/unit/runtime/test_outcome_finalizer.py::test_sealer_accepts_need_attachment_without_rule_claims`
  - `tests/deterministic/unit/runtime/test_outcome_finalizer.py::test_sealer_rejects_completed_diagnosis_without_rule_claims`
  - `tests/deterministic/unit/runtime/test_server_verifier_v2.py::test_valid_missing_only_wait_is_preserved_when_fact_is_absent`
  - `tests/deterministic/unit/runtime/test_server_verifier_v2.py::test_completed_diagnosis_claims_exactly_follow_the_pinned_skill`
  - `tests/deterministic/unit/runtime/test_server_verifier_v2.py::test_review_claims_exactly_follow_the_pinned_skill`
  - `tests/deterministic/unit/runtime/test_p0_semantic_assets.py::test_specialist_assets_require_skill_and_raw_evidence_checks`
  - `tests/deterministic/journey/test_rpc_timeout.py::test_r01_r14_rpc_timeout_is_one_durable_cross_module_path`
- **最新 Test Flow verdict**：fresh Release `run-20260820T045247Z-bbd8abff`，
  `PASS_WITH_WARNINGS`；`journey.cross-job.route` 与后续全部 CrossJob stage 均为 `PASS`，
  未再触发服务端 2,000,000-token cap；functional、operation、verification 均为 `PASS`，
  performance 为 `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:9eacd6a22cec2cb37b88503a2663f0825eccca76397b31b5199af687ccfa2051`
  （603 files）。

## PL-FIX-014：CrossJob 客户端在长轮询中重复空 get_case 直到 turn 上限

- **状态**：代码修复已完成；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：fresh Release 的 Diagnose phase3 先以完整三字段参数成功调用
  `problem_locator_get_case` 7 次，并完成附件提交、观察 `WAITING_INPUT` 与补参；随后一次空
  `{}` 调用得到 `VALIDATION_ERROR`，模型仅纠正一次，又连续重复 39 次空调用，最终 Claude
  terminal 为 `error_max_turns`、`num_turns=51`，导致 `CLAUDE_PHASE3_EXIT_1`。
- **受影响版本**：Problem Locator 4.0.0，B+C 合并候选
  `git-visible-worktree-v1:de11cbcacdd7e0d8fa592d5215fae2531b37978332115fcfeea256c11874c6d2`。
- **根因**：公开 schema 正确要求非空 `case_id`，服务端也正确拒绝空输入；客户端 Skill 和
  Release prompt 虽要求 `wait_seconds=30`，但没有把完整 get-case 参数作为跨轮次不变模板，
  也没有规定本地参数校验失败后的唯一恢复动作。多次大 CaseView 长轮询后，真实模型丢失工具
  参数并把相同本地校验失败误当作可重复轮询，40 次失败调用均未到达 Linux 服务端。
- **不可回归行为**：每次 get-case 长轮询都显式携带 authoritative `case_id`、当前
  `wait_for_job_id=null` 和 `wait_seconds=30`；RUNNING、WAITING_INPUT、REVIEWING 到终态始终复制
  同一个三字段字面对象，让 null 由查询语义自动跟随当前 active Job；不得向任何 Problem
  Locator 工具发送空 `{}`。
  空/缺参 `VALIDATION_ERROR` 后下一调用必须从最后权威值重建完整模板，不得重复相同无效输入；
  不通过客户端 Hook、服务端隐藏默认值、嵌套参数兼容或提高 turns/token cap 掩盖问题。
- **修复历史**：2026-08-20 根据 Release `run-20260820T041337Z-189bb669` 的 phase3
  terminal、47 次 get-case 工具轨迹和逐调用输入/结果分类，在生产客户端 Skill 与 Test Flow
  phase1/phase3 prompt 中加入同一纯函数生成的完整 null-target 轮询模板和单次 fail-recovery
  约束；不修改公开 MCP schema、服务端验证或运行预算。
- **专项回归测试**：
  - `tests/deterministic/unit/interfaces/test_client_access_skill.py::test_skill_document_names_tools_and_safety_invariants`
  - `tools/test-flow/tests/release-inputs.test.mjs` 中
    `CrossJob runtime uses pull-never, empty labeled storage and authoritative server DFX`
  - `tools/test-flow/tests/cross-job-polling.test.mjs`
- **最新 Test Flow verdict**：fresh Release `run-20260820T045247Z-bbd8abff`，
  `PASS_WITH_WARNINGS`；`journey.cross-job.diagnose`、Review、Publish/Restart 均为 `PASS`，
  未再出现空参数循环或 `error_max_turns`；functional、operation、verification 均为 `PASS`，
  performance 为 `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:9eacd6a22cec2cb37b88503a2663f0825eccca76397b31b5199af687ccfa2051`
  （603 files）。

## PL-FIX-015：Methods method cards 在相关 marker 确认前被提前暴露

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：专用 DIAGNOSE 的初始上下文可能携带完整 Methods package 的全部 method cards，
  Agent 在读取冻结 target logs 前已经获得不相关方法；`loaded_method_ids` 也可能与真实注入文件
  分离，令“按需加载”只存在于事后声明。
- **受影响版本**：Methods V7 接入的早期 5.0.0 候选。
- **根因**：context 构建复用了整包资源投影，marker scan 和 grounding audit 均发生在 Agent
  上下文已经冻结之后；旧 V6 envelope 读取路径也未完全 hard cut。
- **不可回归行为**：Runtime 必须先扫描冻结 source marker，再只注入命中的 method cards；初始
  specialized context 只能含 `SKILL.md`、`methods.json` 与 shared references。最终
  `loaded_method_ids`、marker hits、registration/package/combined digest 和 Logparse receipt 必须
  与实际注入字节闭合；专用 DIAGNOSE/REVIEW 只接受 Methods draft 路径，不接受 V6 envelope。
- **修复历史**：2026-08-24 新增 marker-first grounding 和 Methods-only output pipeline，拆开
  元 Skill、目录索引与按需卡片上下文，并将真实 load receipt 写入产品审计记录。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_methods_skill.py::test_marker_scan_loads_only_relevant_method_cards_before_context`
  - `tests/deterministic/unit/runtime/test_methods_skill.py::test_grounding_rejects_a_receipt_that_does_not_match_injected_marker_cards`
  - `tests/deterministic/unit/runtime/test_methods_output_pipeline.py::test_workspace_freezes_minimal_methods_boundary_and_server_receipt`
  - `tests/deterministic/unit/runtime/test_methods_output_pipeline.py::test_specialized_diagnosis_hard_cut_ignores_legacy_v6_envelope`
- **最新 Test Flow verdict**：Dev `run-20260824T044518Z-4467c837`，`PASS_WITH_WARNINGS`；
  functional、operation、verification 均为 `PASS`，performance 为 `NOT_CALIBRATED`；验证
  源码快照 `git-visible-worktree-v1:5d9d8f34f1addf5a04b8c2bc5b3933a6a6daae39d8365b5d742a9f6135ae245c`
  （641 files），framework Node 351 passed；deterministic 2348 passed、1 skipped。

## PL-FIX-016：Methods limitations 与 safety notes 在最终结果链路丢失

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：Methods draft 已给出 `limitations`/`safety_notes`，映射到 Domain outcome、Finalizer
  和 `UserResult` 后却可能丢失或被固定通用文本替代；Test Flow 又曾在整份 report 中搜索安全
  短语，使短语出现在其他字段也能误判通过。
- **受影响版本**：Methods V7 接入的早期 5.0.0 候选。
- **根因**：V6 outcome DTO 没有这两个 Methods 字段，server mapping 与最终报告模型未逐层
  传递；Release oracle 没有把安全断言限定到 `report.safety_notes`。
- **不可回归行为**：两个数组从 canonical Methods draft 原样贯穿 DiagnosisOutcome、
  server-owned final outcome 和 UserResult；显式空 `safety_notes=[]` 必须保留，不得生成固定替代
  文本。安全 oracle 只能检查 `report.safety_notes`，不能从其他字段借用命中。
- **修复历史**：2026-08-24 扩展 V5 合同、Methods mapper、Finalizer 与 UserResult，并把
  CrossJob/Codex 两个 consumer 的安全检查绑定到结构化字段。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_methods_skill.py::test_grounded_methods_are_mapped_by_the_server_into_candidate_domain`
  - `tests/deterministic/unit/runtime/test_diagnosis_runtime.py::test_methods_two_pass_closes_broker_then_stages_grounded_result`
  - `tests/deterministic/contracts/test_user_result_v2.py::test_user_result_safety_notes_preserve_an_explicit_empty_methods_array`
  - `tools/test-flow/tests/cross-job-runtime-boundary.test.mjs` 中
    `CrossJob report oracle requires safety_notes placement and one verification rule per same-method event`
- **最新 Test Flow verdict**：Dev `run-20260824T044518Z-4467c837`，`PASS_WITH_WARNINGS`；
  functional、operation、verification 均为 `PASS`，performance 为 `NOT_CALIBRATED`；验证
  源码快照 `git-visible-worktree-v1:5d9d8f34f1addf5a04b8c2bc5b3933a6a6daae39d8365b5d742a9f6135ae245c`
  （641 files），framework Node 351 passed；deterministic 2348 passed、1 skipped。

## PL-FIX-017：Release Methods oracle 未消费 expected_status 且可合并重复事件

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：fixture 的 `expected_status` 即使与最终 DIAGNOSE 不一致也可能不影响 verdict；
  adapter 声明的 method ID 可被直接信任；同一 method 的两个独立日志事件也可能合并成一个
  evidence/rule 后仍通过。
- **受影响版本**：Methods V7 Test Flow 接入的初始实现。
- **根因**：oracle 只检查最终报告的表层结构，没有从唯一最终 DIAGNOSE Job 的产品记录和实际
  生成 package 独立重算语义；事件校验按 method ID 去重而非按 source-line identity 计数。
- **不可回归行为**：consumer 必须从唯一最终 DIAGNOSE 的 canonical `job.json`、
  `method-grounding-audit.json`、`methods_logparse_receipt.json` 读取实际状态和身份；独立重读生成
  package、重算三重 digest 并映射 method ID。每个同方法独立 event 都必须有自己的 evidence
  和 verification rule，未知 method、字段顺序漂移、歧义 Job 与事件合并均失败关闭。
- **修复历史**：2026-08-24 新增共享 Methods oracle，令 CrossJob 与 Codex action consumer
  独立重演 package/grounding/status/事件合同，不再相信 adapter summary。
- **专项回归测试**：
  - `tools/test-flow/tests/cross-job-runtime-boundary.test.mjs` 中
    `CrossJob Methods status oracle reads the exact grounded execution record and fails on status or identity drift`
  - 同文件 `CrossJob Methods consumer re-derives method IDs from the generated package and rejects coherent unknown-method tampering`
  - 同文件 `CrossJob Methods consumer rejects a coherently rebound ordered-field mutation`
  - 同文件 `CrossJob report oracle requires safety_notes placement and one verification rule per same-method event`
- **最新 Test Flow verdict**：Dev `run-20260824T044518Z-4467c837`，`PASS_WITH_WARNINGS`；
  functional、operation、verification 均为 `PASS`，performance 为 `NOT_CALIBRATED`；验证
  源码快照 `git-visible-worktree-v1:5d9d8f34f1addf5a04b8c2bc5b3933a6a6daae39d8365b5d742a9f6135ae245c`
  （641 files），framework Node 351 passed；deterministic 2348 passed、1 skipped。

## PL-FIX-018：Methods Logparse 预处理 workspace 被资源路径校验拒绝

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：Methods 两阶段 DIAGNOSE 将资源物化到 `<job-uuid>.logparse-preprocess` 时，
  `ResourceFileReader` 把该产品自有目录误判为非法 UUID，真实预处理在调用 Logparse 前失败。
- **受影响版本**：Methods V7 两阶段运行时的初始 5.0.0 候选。
- **根因**：storage 边界只允许裸 UUID workspace 名称，没有为同一 Job 的受控预处理阶段建立
  精确后缀合同。
- **不可回归行为**：资源只能物化到裸 `<uuid>` 或精确 `<uuid>.logparse-preprocess`；任何其他
  后缀、多重后缀、路径穿越、链接或身份漂移必须失败关闭。
- **修复历史**：2026-08-24 在 storage 层增加唯一产品后缀 allowlist，不放宽通用路径解析。
- **专项回归测试**：
  - `tests/deterministic/unit/storage/test_resource_files.py::test_reader_materializes_one_resource_into_main_and_logparse_workspaces`
  - `tests/deterministic/unit/storage/test_resource_files.py::test_reader_rejects_non_product_workspace_suffixes`
  - `tests/deterministic/unit/storage/test_resource_files.py::test_reader_rejects_traversal_from_logparse_workspace`
  - `tests/deterministic/journey/test_rpc_timeout.py::test_r01_r14_rpc_timeout_is_one_durable_cross_module_path`
- **最新 Test Flow verdict**：Dev `run-20260824T044518Z-4467c837`，`PASS_WITH_WARNINGS`；
  functional、operation、verification 均为 `PASS`，performance 为 `NOT_CALIBRATED`；验证
  源码快照 `git-visible-worktree-v1:5d9d8f34f1addf5a04b8c2bc5b3933a6a6daae39d8365b5d742a9f6135ae245c`
  （641 files），framework Node 351 passed；deterministic 2348 passed、1 skipped。

## PL-FIX-019：Codex Luna 探索流缺少可发布的最小权限与独立证据边界

- **状态**：已工程化；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：原探索只能作为本机脚本结论，无法由 Test Flow 冻结 Codex CLI/model/effort、十次
  调用、权限、usage/cost 和 Methods 语义；早期工程化路径还把 output schema 路径当作 schema
  object，给 `codex sandbox` 传入其不支持的全局 `--strict-config`，并未把 forbidden read
  receipt 与真实目标及独立 consumer 绑定。
- **受影响版本**：`release.codex-luna-methods` 引入前及其早期候选。
- **根因**：探索 runner 同时承担执行与判定，没有 app-server 原始协议 allowlist、外部内存
  auth、命名 permission profile 和 action consumer；不同 Codex 子命令的参数边界被误认为一致。
- **不可回归行为**：独立 Release goal 固定 ChatGPT 内置 Codex CLI 0.149.0-alpha.4.1、
  `gpt-5.6-luna`、medium 和十次 fresh 调用；每次使用独立 HOME/CODEX_HOME/thread/turn，auth
  只经 stdin 驻留内存且不得进入证据。app-server 使用严格配置和原始消息 allowlist；sandbox
  probe 不携带不支持的参数，命令网络关闭，repo/AGENTS/auth/raw 读取失败关闭。输出 schema 必须
  解析为 object，最终 action consumer 独立重算 Methods/usage/权限/secret 结论。
- **修复历史**：2026-08-24 新增 app-server runtime、隔离配置、结构化输出 schema、十调用
  runner、post-hoc 预算准入和独立 actions consumer；旧 raw exploration runner 被 hard cut。
- **专项回归测试**：
  - `tools/test-flow/tests/codex-luna-app-server.test.mjs`
  - `tools/test-flow/tests/codex-luna-app-server-runtime.test.mjs`
  - `tools/test-flow/tests/codex-luna-contract.test.mjs`
  - `tools/test-flow/tests/actions.test.mjs` 中 Codex Luna action consumer 篡改回归
  - `tools/test-flow/tests/config-planner.test.mjs` 中 `release.codex-luna-methods` 准入回归
- **最新 Test Flow verdict**：Dev `run-20260824T044518Z-4467c837`，`PASS_WITH_WARNINGS`；
  functional、operation、verification 均为 `PASS`，performance 为 `NOT_CALIBRATED`；验证
  源码快照 `git-visible-worktree-v1:5d9d8f34f1addf5a04b8c2bc5b3933a6a6daae39d8365b5d742a9f6135ae245c`
  （627 files），framework Node 322 passed；真实十调用结果仍以本条后续 fresh Release 元数据为准。

## PL-FIX-020：Darwin 主机无法执行真实 Linux Client → Linux Server Release

- **状态**：回归已再次修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：在 Darwin 上显式选择 `--client linux` 时，旧 planner 仍解析到只适用于 Linux host
  的 `linux-linux-release.mjs`，没有创建隔离 Linux Client 容器，也无法把客户端 OS、浏览器、
  Claude CLI 和服务端容器分别冻结为双 Linux 拓扑证据。
- **受影响版本**：新增 Darwin-orchestrated Linux Client 支持前的 Test Flow v2；以及首次真实
  dual-Linux Release 源码快照
  `git-visible-worktree-v1:e93ed0c156cb7d59110e184bab960f1998fa9dc457569750d98e182f354fe613`
  （627 files）；以及 environment 只验证 Chrome `--version`、CrossJob Client 未绑定 HOME 的
  源码快照 `git-visible-worktree-v1:192ac15a6704eabe29fbe6893462931b8ac2c6e59f7b13b9897abbc9ebd66064`
  （629 files），其 fresh Release `run-20260824T004249Z-35bb1233` 在 Skill generation 与 route
  已 PASS 后，于首个浏览器 upload、任何 client/server tool call 或模型调用前以
  `CHROME_UPLOAD_EXIT_133` 失败；以及加入前置 Chrome 启动探针后的源码快照
  `git-visible-worktree-v1:038ca588db3f5c6de36fac2248c3abcb2329523003092dc106af734d258f1274`
  （630 files），其 fresh Release `run-20260824T013356Z-a0eef225` 在 Skill generation PASS 后，
  于 CrossJob environment 以 `CHROME_CAPABILITY_TIMEOUT` 阻断。该 environment 内模型调用、
  Client/Server tool call 均为 0，但前置 Skill generation 已消耗 1 次调用、13 turns、323202 tokens。
- **根因**：built-in adapter 只按 client 标签选择，没有把 host orchestrator 与 client runtime
  拆开建模；sealed cache 也只有 server image，没有 client image identity。后续回归的直接原因是
  CrossJob 虽创建了可写 `/client-home` tmpfs，却没有向非 root Client 容器设置
  `HOME=/client-home`；environment 又只核对 Chrome 二进制版本与哈希，没有实际启动 headless
  browser，所以错误直到已消耗真实模型的 upload Stage 才暴露。旧 launcher 还只观测 Docker
  exec 外层 133，并在 child `exit` 而非 stdio `close` 时封口，不能把 133 安全归因为 Chrome
  `SIGTRAP`，也可能漏掉输出尾部。第二次回归证明仅把启动探针移到 environment 仍不够早：
  Apple Silicon 宿主上的 Colima 使用 `x86_64 + qemu + rosetta=false`，完整 Chrome for Testing
  152 即使以官方最小参数访问静态 `data:` URL，120 秒内仍不产出 DOM；同版本官方 Chrome
  Headless Shell 在完全相同的 image/user/HOME/tmpfs 边界约 11 秒完成。planning 原先只校验
  二进制版本/哈希和 opaque Colima fingerprint，没有执行零模型 DOM smoke；Python runner 的
  `subprocess.run(timeout=45)` 也只封口直接父进程，不能独立证明 Chrome 后代进程全部消失。
- **不可回归行为**：Darwin + `--client linux` 只能选择仓库自有 dual-Linux adapter，构建并
  identity-bind 独立 Linux Client/Server images 与 containers；Client 不安装本地 MCP、代理、
  Hook 或专用 DFX，只用 Claude Code 2.1.89 + `deepseek-v4-flash[1m]` 经 HTTP 直连 Linux Server。
  `--client macos` 仍保留本机 Client → Linux Server，其他 host/client 组合失败关闭。Client
  capability 与 CrossJob Client container 必须显式继承 Darwin 调度用户的非 root 数字 uid:gid，
  不记录用户名或主机路径；producer/runtime receipt 与 actions consumer 都必须拒绝 uid=0，且
  source snapshot 只读、无本地 MCP/Hook/代理和 run-owned container cleanup 边界保持不变。
  CrossJob Client 还必须恰好绑定一个 `HOME=/client-home` 并通过真实写入探针；dual-Linux
  environment 必须在服务初始化和任何模型调用前，用 upload/API 共用的同一 pinned launcher
  完成一次零模型 loopback DOM roundtrip，而 host-client 必须显式记录 `browser_capability:null`。
  browser failure receipt 只能保存分层 exit/signal、字节数与 SHA-256，不保存原始 DOM、stderr、
  环境或命令；actions consumer 必须独立绑定 Stage label、Client container/image/non-root user、
  HOME、Chrome、runner、wrapper、process attribution 与 code，外层 133 只能标记为未确认的
  POSIX signal candidate，不能宣称为浏览器 `SIGTRAP`。
  显式 Linux Client 不得再携带或兼容旧完整 Chrome 路径/label：必须冻结官方 Chrome Headless
  Shell 的 product、版本、归档与可执行文件摘要；planning 必须在任何真实模型 Stage 之前用
  exact Client image 执行一次 `--network none`、非 root DOM smoke，失败即 admission blocker。
  正式 runner 必须为浏览器创建私有 POSIX session，超时执行 TERM→有界等待→必要时 KILL，
  receipt 只有在 direct parent 已回收且 process group 确认不存在时才允许 PASS。
- **修复历史**：2026-08-24 增加 client Dockerfile、Darwin dual-Linux adapter、拓扑准入、
  双 image/runtime identity 和 client-container 调用预算，仍复用同一 CrossJob core 与七工具合同。
  2026-08-24 真实 Release `run-20260823T211650Z-5f6841ed` 暴露一次回归：冻结 Client image 的
  `Config.User=""`，两个 `docker run` 入口也未指定 `--user`，Docker 因而以 uid 0 启动 Claude，
  2.1.89 正确拒绝 `--dangerously-skip-permissions`。修复为两个入口都显式传递非 root 数字
  uid:gid，提供隔离可写 HOME，并把 uid/gid/root 状态加入 capability/runtime evidence 及独立
  consumer；不修改 Claude 参数、模型、MCP、网络或容器清理策略。
  2026-08-24 根据 `run-20260824T004249Z-35bb1233` 的零调用 browser failure，再次修复该回归：
  CrossJob 显式设置并写探测 `/client-home`，新增 source-owned Python browser runner 直接记录
  Chrome 子进程 exit/signal，通用 capture 等待 `close` 后封口；environment evidence contract
  破坏性升级为 `cross-job-environment-v3`，在模型前执行共享 launcher capability，并由 actions
  对 PASS 与 failure receipt 做 exact-schema、identity 和语义一致性复核。
  2026-08-24 根据 `run-20260824T013356Z-a0eef225` 的 sealed timeout receipt 和同镜像无模型
  A/B 复现，删除完整 Chrome cache/path/labels 与通用兼容别名，改为摘要冻结的官方同版本
  Chrome Headless Shell；release cache seal 升级为 v3。planner 新增无网络 image DOM smoke；
  runtime identity、PASS/failure consumer 和文档改用显式 Headless Shell product。runner 改用
  `Popen(start_new_session=True)` 与 Linux subreaper，封口整个私有进程组并新增 closed
  `cleanup.process_tree` 证据。
- **专项回归测试**：
  - `tools/test-flow/tests/release-inputs.test.mjs` 中
    `the dual Linux adapter fails closed on traversal, mutable Skills, proxy leakage and runtime replacement`
  - 同文件 `the first-party adapter matrix is thin, platform-bound and shares one core contract`
  - `tools/test-flow/tests/config-planner.test.mjs` 中 Darwin + Linux Client topology 回归
  - `tools/test-flow/tests/cross-job-runtime-boundary.test.mjs`
  - `tools/test-flow/tests/actions.test.mjs` 中
    `Darwin explicit Linux runs Client capability inside the frozen Linux image`
    （显式 uid:gid、root producer/consumer 篡改回归）
  - `tools/test-flow/tests/cross-job-runtime-boundary.test.mjs` 中
    `dual Linux Client identity requires one explicit non-root uid:gid`
  - 同文件 `dual Linux Client container binds one writable HOME before its runtime probe`
  - 同文件 `captured commands wait for inherited stdout to close before sealing evidence`
  - 同文件 `Linux browser execution receipt distinguishes confirmed child signals from outer exit conventions`
  - 同文件 `environment browser capability routing requires exact null for host Client and validated PASS for dual Linux`
  - `tools/test-flow/tests/actions.test.mjs` 中
    `Linux Client browser capability is a closed zero-model runnable receipt`
  - `tools/test-flow/tests/release-inputs.test.mjs` 中
    `active runtime support is explicit and the historical harness closure is gone`
  - 同文件 `dual Linux planning executes one closed zero-network Headless Shell smoke before admission`
  - `tests/deterministic/unit/runtime/test_linux_client_browser_runner.py` 中 timeout grandchild、
    normal zombie reap 与真实 `127.0.0.1` GET 三条 runner 专项回归
- **最新 Test Flow verdict**：最终 Dev `run-20260824T012851Z-7765e2b4`，
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:f0a2ee0ec9f084d08d98f3e0b9d16c038ed85efd0acb5c072f59738823667eca`
  （630 files），framework config 18/18、Node 333/333、docs 4/4，deterministic 2345 passed、
  1 skipped，source materialization/worktree verification 均为 `PASS`。该 verdict 引用元数据段
  本身不宣称被其所引用的源码快照覆盖；修复后的 fresh Release 元数据待后续权威 verdict 追加。

## PL-FIX-021：Methods V7 迁移残留旧身份快照与分叉 JSON 解析路径

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：扩大 deterministic 回归同时发现三类漂移：runtime seam 仍固定旧 rpc registration
  digest，OpenAPI snapshot 仍声明 4.0.0，Methods output reader 又直接解析 product-owned broker
  audit，绕开已经集中校验 canonical/shape/hash/唯一成功记录的权威 helper。
- **受影响版本**：Methods V7 hard cut 完成前的 5.0.0 候选。
- **根因**：package anchor 从 `rpc` 迁为真实 fake Logparse `compact` 后没有重算三重 identity；
  版本快照与 output-reader 审计路径也未纳入同一次破坏性迁移。
- **不可回归行为**：runtime seam 必须通过真实 loader 绑定 registration/package/combined 三重
  digest；OpenAPI version 与 `pyproject.toml` 同步；Agent-authored JSON 统一走 shared Agent JSON
  parser，product-owned broker audit 统一走 `validated_successful_broker_record`，不得在 consumer
  中复制解析与唯一性判断。旧 V6 draft 和不完整 Methods audit closure 不得进入 bundle。
- **修复历史**：2026-08-24 根据完整 deterministic 的 3 个精确失败，分别重算 compact
  identity、机械更新仅版本变化的 OpenAPI snapshot，并集中 broker audit validator；同步迁移
  application waiting/unresolved 测试，不放宽安全扫描。
- **专项回归测试**：
  - `tests/deterministic/integration/test_s07_settings_catalog_runtime_seam.py::test_settings_pin_one_s07_pair_into_s04_catalog_and_runtime`
  - `tests/deterministic/unit/integrations/test_agent_json.py::test_agent_json_consumers_do_not_bypass_the_shared_parser`
  - `tests/deterministic/contracts/test_schema_snapshots.py`
  - `tests/deterministic/unit/application/test_outcome_submission.py::test_unresolved_submission_atomically_binds_downloadable_audit_and_replays`
  - `tests/deterministic/unit/application/test_projection.py`
- **最新 Test Flow verdict**：Dev `run-20260824T044518Z-4467c837`，`PASS_WITH_WARNINGS`；
  functional、operation、verification 均为 `PASS`，performance 为 `NOT_CALIBRATED`；验证
  源码快照 `git-visible-worktree-v1:5d9d8f34f1addf5a04b8c2bc5b3933a6a6daae39d8365b5d742a9f6135ae245c`
  （641 files），framework Node 351 passed；deterministic 2348 passed、1 skipped。

## PL-FIX-022：安装分发 Gate 仍固定迁移前的 Methods 三重身份

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：fresh Linux Client → Linux Server Release 在 Server capability 阶段安装 wheel 后，
  `load_specialized_skill_registration()` 正确解析当前 rpc Methods package，却因 platform gate 仍
  期望较早生成阶段的 registration/package/combined digest 而失败；真实 Skill 与 CrossJob
  stages 因此前置失败均未运行。
- **受影响版本**：Methods V7 破坏性迁移的 5.0.0 候选，源码快照
  `git-visible-worktree-v1:920c6dd9bb138381d925144039f91b2d73092cc41e4d6aa814466136fa8675ea`
  （627 files）。
- **根因**：rpc fixture 最终迁移到 `registration-template.json + package/` 后，deterministic seam
  已通过真实 loader 重算当前三重身份，但安装分发 gate 的同组三个冻结常量遗漏同步。
- **不可回归行为**：源码态 deterministic seam 与隔离 wheel 安装态 platform gate 必须针对同一
  rpc registration，分别经真实 loader 得到并固定完全相同的 registration、package tree 与
  combined digest；不得跳过安装态断言或改成只检查字段存在。
- **修复历史**：2026-08-24，fresh Release `run-20260823T213051Z-97b1fccf` 首次暴露该漂移；
  独立重算确认当前三重身份后，仅同步安装分发 gate 的三个冻结期望值，不修改 loader、fixture、
  wheel 内容或 capability 边界。
- **专项回归测试**：
  - `tests/platform/distribution/test_installed_distribution_gate.py::test_clean_installed_distribution_import_cli_and_server_gate`
  - `tests/deterministic/integration/test_s07_settings_catalog_runtime_seam.py::test_settings_pin_one_s07_pair_into_s04_catalog_and_runtime`
- **最新 Test Flow verdict**：修复后 Dev `run-20260823T213530Z-5a170329` 为 `PASS`；functional、
  operation、verification 均为 `PASS`，验证源码快照
  `git-visible-worktree-v1:6d7fd3a64710679b712eb1c2ca5f4860cb438f9b34f18ea132af1af2c6074808`
  （627 files）。该 Dev run 按 affected policy 复用 deterministic receipts；安装态专项回归仍由
  后续 fresh Release 的 `platform.server-linux-adapter` 直接执行。修复前 Release
  `run-20260823T213051Z-97b1fccf` 及其失败源码快照保留在修复历史中。

## PL-FIX-023：Linux Server capability 误判 uv/uvx 的固定版本输出

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：安装分发专项用例修复后，Server capability 内三个 platform pytest 已全部通过，
  adapter 却在后置 runtime identity 校验把官方固定二进制的真实 `uv/uvx --version` 输出判为
  不匹配，导致 fresh Release 在任何真实 Skill/CrossJob 模型调用前 `BLOCKED`。
- **受影响版本**：Test Flow v2 的 0.11.32 Linux/amd64 runtime identity 检查，源码快照
  `git-visible-worktree-v1:3bacf491b29c40d38e321a7e66639fe41a73742bd6f9cb320f99bca925a8a224`
  （627 files）。
- **根因**：生产 adapter 与独立 actions consumer 都假定版本输出只有 `uv 0.11.32` /
  `uvx 0.11.32`，遗漏官方 Linux/amd64 binary 固定输出中的
  `(x86_64-unknown-linux-gnu)` target triple；此前安装分发断言先失败，遮蔽了该后置分支。
- **不可回归行为**：runtime profile 必须同时冻结 uv version、完整 uv/uvx version output、archive
  与两个 executable SHA-256；producer 和 consumer 必须精确要求 Linux/amd64 完整输出。不得用
  宽松前缀/正则、删除 target triple 或只凭 version text 取代二进制 hash。
- **修复历史**：2026-08-24，fresh Release `run-20260823T213652Z-a50a4898` 确认 platform
  pytest 3/3 PASS 后暴露该问题；将两个完整输出提升为 formal Release runtime profile 字段，并
  让 config validator、producer 与独立 consumer 共同精确消费。
- **专项回归测试**：
  - `tools/test-flow/tests/actions.test.mjs` 中
    `Linux Server runtime identity requires exact CLI, uv and uvx bytes inside the frozen image`
  - `tools/test-flow/tests/release-inputs.test.mjs` 中
    `runtime profile is the sole frozen version and artifact-pin source`
  - 同文件 `Linux capability installs the immutable source snapshot from the sealed offline cache before testing`
- **最新 Test Flow verdict**：修复后 Dev `run-20260823T214500Z-0266c76f` 为
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:427fb6ec406f4f7d4ed50c8004d11fc16382087f5eb695abdc91a8b543d652ef`
  （627 files），framework config 18/18、Node 302/302、docs 4/4，deterministic 2322 passed、
  1 skipped。修复后的 frozen-image runtime identity 仍由后续 fresh Release 直接验证。

## PL-FIX-024：Server capability 的受控首错被改写成模型不可用

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：Server adapter 因 runtime identity 受控退出时，权威 gate receipt 却记录
  `SERVER_MODEL_CAPABILITY_UNAVAILABLE`；该 stage 没有执行任何模型调用，实际首错只能在 stderr
  中人工恢复。
- **受影响版本**：Test Flow v2 的 Server Linux capability action，至少影响 fresh Release
  `run-20260823T213652Z-a50a4898`。
- **根因**：orchestrator 仅按 exit code 分类，把 adapter 的所有 exit 2/3 分别折叠成一个 model
  unavailable/failed code；adapter 没有提供可独立消费的结构化终止收据。
- **不可回归行为**：adapter 的受控终止必须原子写入只含 schema/status/code 的收据；actions
  consumer 只接受精确字段、匹配 exit status 和内置 code allowlist，保留真实首错。未知 code、
  多余字段、status/exit 不匹配或缺失收据必须转为 harness error；不得解析任意 stderr。
- **修复历史**：2026-08-24 增加 `server-capability-termination.json` producer 与独立 allowlist
  consumer，移除误导性的通用 model code 映射。
- **专项回归测试**：
  - `tools/test-flow/tests/actions.test.mjs` 中
    `Linux Server capability preserves only an allowlisted structured adapter termination`
  - `tools/test-flow/tests/release-inputs.test.mjs` 中
    `Linux capability installs the immutable source snapshot from the sealed offline cache before testing`
- **最新 Test Flow verdict**：修复后 Dev `run-20260823T214500Z-0266c76f` 为
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:427fb6ec406f4f7d4ed50c8004d11fc16382087f5eb695abdc91a8b543d652ef`
  （627 files），相关 producer/consumer mutation tests 已包含在实际执行的 framework Node
  302/302 PASS 中。结构化失败路径仍由后续失败身份触发时直接验证。

## PL-FIX-025：Methods 真实生成 Gate 缺少固定 runtime workspace

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：Linux Server capability 全部通过后，`real.skill-generation` 在 0.31 秒内以
  `BACKEND_START_FAILED: Agent Workspace is unavailable` 失败；模型 invocation 和 usage 均为零。
- **受影响版本**：Methods V7 real Skill generation gate 的初始实现，源码快照
  `git-visible-worktree-v1:2968a5c3c09938902c7de8f47dbf2053e7da29d114d93d963cde8db45609d70a`
  （627 files）。
- **根因**：破坏性迁移后的测试只创建 `workspace/inputs` 与 `workspace/output`，遗漏
  AgentBackend 自身固定且失败关闭的第三个顶层目录 `workspace/runtime`；因为该 real gate 在
  deterministic 轨默认 skip，只有 fresh Release 首次走到模型启动边界时暴露。
- **不可回归行为**：真实 Methods generation 与其他 Agent gate 一样，启动前必须物化且只物化
  `inputs`、`runtime`、`output` 三个普通目录；不得通过放宽 `_WORKSPACE_TOP_LEVEL`、绕过 identity
  capture、使用链接或在根目录增加额外路径来让 gate 启动。
- **修复历史**：2026-08-24，fresh Release `run-20260823T214710Z-66588ff8` 证明 Server
  capability producer/consumer 与 platform 3/3 已 PASS 后，确认 real gate 的 workspace 夹具
  漏建；仅补建空 `runtime` 目录，不修改 AgentBackend 或模型调用合同。
- **专项回归测试**：
  - `tests/real/agent/test_real_wiki_skill_generation_gate.py::test_claude_2_1_89_pinned_model_generates_registered_methods_package`
  - `tests/deterministic/unit/runtime/test_agent_backend.py` 中固定 workspace identity 与顶层目录拒绝回归
- **最新 Test Flow verdict**：修复后 Dev `run-20260823T215139Z-f296001b` 为 `PASS`；
  functional、operation、verification 均为 `PASS`，验证源码快照
  `git-visible-worktree-v1:5a2e1add22ee29a43e6fbfe22c6bb391aa7f915eb41bf3e473e805a1aa086f59`
  （627 files）。该 Dev run 按 affected policy 复用已重审的 framework/full receipts；真实 Methods
  generation 专项回归仍由后续 fresh Release 直接执行。

## PL-FIX-026：Methods 多文件生成所需 turns 超过旧 V6 上限且计划低估 token

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：真实 DeepSeek 调用成功结束并通过工具审计，写出完整的 8 文件 Methods package，
  wrapper 却因终态 `num_turns=13` 超过 `max_turns=12` 判为
  `WRAPPER_MODEL_TERMINAL_INVALID`；计划同时只估算 12,000 tokens，而实际完整 usage 为 589,513。
- **受影响版本**：从单文件 V6 生成迁移为 V7 Methods 多文件 package 后仍沿用旧 cap/估算的
  Test Flow，源码快照
  `git-visible-worktree-v1:9d3920b916a44e3d112f791e4ccd80b8985294474f33be0a11aa0718497dfa9e`
  （627 files）。
- **根因**：固定场景现在必须完成 1 次 Skill、2 次 Read、8 次连续 Write 和终态响应，但 runtime
  profile 仍按旧生成流固定 12 turns；planner 又通用地用 `max_turns * 1000` 估算该高缓存上下文
  调用，未绑定 Methods 场景的实测量级。
- **不可回归行为**：Skill generation 仍恰好 1 次 invocation，并保留 100 万 total tokens、64,000
  output tokens、$10、1800 秒硬上限；只把 turn cap 调整为可容纳固定协议且仍有界的 16。wrapper
  必须继续拒绝超过 cap、非 success terminal 或不完整 usage。plan 必须从 stage config 显示 600,000
  tokens 的场景估算，不得把估算误当成或替代硬上限。
- **修复历史**：2026-08-24，fresh Release `run-20260823T215257Z-3a6ede9f` 记录恰好 1 次
  `deepseek-v4-flash[1m]` 调用、13 turns、589,513 tokens、$1.845061；工具 trace PASS，但在 canonical
  validator 前因旧 turn cap 失败。随后仅调整该独立 cap 和显式 stage estimate，不放宽 wrapper。
- **专项回归测试**：
  - `tools/test-flow/tests/config-contract.test.mjs` 中 real cap 与 stage estimate 的值、正数和 scope 回归
  - `tools/test-flow/tests/config-planner.test.mjs` 中 `plans expose only observable progress and exact serial model deadlines`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 terminal/turn/usage 硬上限拒绝回归
  - `tests/real/agent/test_real_wiki_skill_generation_gate.py::test_claude_2_1_89_pinned_model_generates_registered_methods_package`
- **最新 Test Flow verdict**：修复后 Dev `run-20260823T220800Z-103802f3` 为
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:d5a80d6304ff8503263cc318d0a257eac4b7a4f39a0e96f97a2edf635542e9eb`
  （627 files），framework config 18/18、Node 304/304、docs 4/4，deterministic 2322 passed、
  1 skipped。真实 16-turn generation 仍由后续 fresh Release 直接验证。

## PL-FIX-027：受限 Methods 生成 Agent 被要求在无哈希工具时计算 Wiki SHA-256

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：fresh Linux Client → Linux Server Release 的真实 meta-Skill 调用正常终止、工具轨迹
  PASS 且生成闭合 Methods package，但 canonical validator 以
  `source_wiki_sha256 does not match the supplied Wiki` 拒绝包；语义 oracle 与全部 CrossJob stage
  因此前置失败均未运行。
- **受影响版本**：Methods V7 隔离生成合同，源码快照
  `git-visible-worktree-v1:8505fcb3c37c0787d91255c99d0afef9e2ef050702891d099940be114adc1399`
  （627 files）。
- **根因**：meta Skill 要求生成 Agent 计算原始 Wiki 的 SHA-256，但 Release wrapper 故意只提供
  `Skill/Read/Write`，没有 Bash 或任何哈希 primitive；旧 v3 trace 又只允许读取 Wiki 与 output
  contract。要求模型从 7,418 字节文本心算密码学摘要既不可执行，也不可审计。
- **不可回归行为**：Gate 必须在 invocation 前直接从未修改的 Wiki 字节生成 closed-schema、
  canonical `runtime/source-wiki-identity.json`；v4 trace 在启动前和收尾时都独立验证 sidecar schema、
  canonical bytes 与 Wiki digest，并要求模型恰好读取 Wiki、source identity 与 output contract 后才
  连续写包。工具集仍只能是 `Skill/Read/Write`，不得开放 Bash、读取 registration/semantic oracle、
  猜测 digest、放宽 validator 或在模型退出后回填 `methods.json`。canonical validator 必须继续从
  原始 Wiki 独立重算，单个 nibble 漂移也必须失败。
- **修复历史**：2026-08-24，fresh Release `run-20260823T221129Z-bed6b806` 记录恰好 1 次
  `deepseek-v4-flash[1m]` 调用、11/16 turns、129,031 tokens、$1.308059，terminal/wrapper/tool trace
  均 PASS，但 canonical validator 在进入模型不可见 oracle 前精确失败。随后新增调用方拥有的
  source-identity sidecar、v4 permission/trace consumer 与 prompt 绑定；生成包保持 Agent 原始字节，
  没有后处理。
- **专项回归测试**：
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中
    `requires a canonical source identity that binds the exact Wiki bytes`
  - 同文件 `grants only the Wiki, source identity, linked output contract, Skill load, and audited output subtree`
    及 `allows exactly three full Reads and requires all before every Write`
  - `tests/deterministic/unit/runtime/test_meta_skill_source_identity.py::test_canonical_validator_independently_recomputes_source_wiki_identity`
  - `tests/real/agent/test_real_wiki_skill_generation_gate.py::test_claude_2_1_89_pinned_model_generates_registered_methods_package`
- **最新 Test Flow verdict**：修复后 Dev `run-20260823T222815Z-60b9394d` 为
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:2fdef71cb8774f78d7dfbc8557f188308c201c1255e592e0c8b8766d8781e3d8`
  （628 files）。所有 stage 均实际执行；framework Node 306/306、config 18/18、docs 4/4，
  deterministic 2323 passed、1 skipped，source-identity 专项在 unit JUnit 中实际执行并 PASS。
  修复后的真实模型与 CrossJob 仍由后续 fresh Release 直接验证；本 verdict 引用元数据行不宣称
  被其所引用的源码快照覆盖。

## PL-FIX-028：Methods 语义 oracle 要求未声明的 marker 拼写并漏列 Wiki 命名字段

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：v4 source identity 修复后，真实 meta-Skill 调用、三次必读、连续写包与 canonical
  validator 全部 PASS，gate-only semantic oracle 却同时报告 `log_derived_fields` 和三个 method
  marker set 不匹配，阻止 CrossJob 启动。
- **受影响版本**：Methods V7 输出合同与 release-case oracle，源码快照
  `git-visible-worktree-v1:3099a98a0fcb24a3af22239945c9c2fef5d25848608cfb1a0531b0d6ea4f5197`
  （628 files）。
- **根因**：输出合同只要求 marker 是 Wiki 中的“短字面量”，没有定义唯一提取算法；oracle 却用
  exact list equality 固定了占位符前缀。与此同时，合同要求保留命名日志字段，oracle 却漏掉
  `print_time_ms`、`ordinal`、`current_us`、`request_us` 四个 Wiki `{field}`，使遵守合同的生成物
  反而无法通过隐藏 oracle。
- **不可回归行为**：`log_derived_fields` 必须按 Wiki `text` 日志模板及模板内首次出现顺序收集
  唯一命名字段，再排除 `required_user_inputs`；不得遗漏或重排。每个 `evidence_marker` 必须使用
  占位符前的完整稳定前缀；模板以占位符开头时，机械选择最长稳定字面片段并以最早者破同长；不得
  截短、保留占位符或自由改写。meta Skill、canonical validator、fixture oracle 和独立回归必须使用
  同一算法；semantic oracle 只检查原因分组/覆盖，不得另设模型不可见的表面拼写规则。
- **修复历史**：2026-08-24，fresh Release `run-20260823T223123Z-6a2c116f` 记录恰好 1 次
  `deepseek-v4-flash[1m]` 调用、14/16 turns、124,357 tokens、$1.215717；v4 tool trace 与
  canonical validator（3 methods、6 markers、6 templates、Wiki hash）均 PASS，随后 oracle 精确
  暴露上述四类 mismatch。修复将字段顺序与 marker 提取提升为公开机械合同，validator 失败关闭，
  并把 oracle 补齐到 Wiki 的 14 个日志派生字段。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_meta_skill_source_identity.py::test_validator_requires_canonical_markers_and_named_field_order`
  - `tests/deterministic/unit/runtime/test_meta_skill_source_identity.py::test_release_semantic_oracle_matches_mechanical_wiki_extraction`
  - `tools/test-flow/tests/release-case.test.mjs` 中 registration/oracle/partition 与 manifest drift 回归
  - `tests/real/agent/test_real_wiki_skill_generation_gate.py::test_claude_2_1_89_pinned_model_generates_registered_methods_package`
- **最新 Test Flow verdict**：修复后 Dev `run-20260823T224753Z-64a8fce8` 为
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:4abcca44bb1113ce7eee6e3ca3ee72fab79508cb11ccb0d77d1b6d1a02219577`
  （628 files）。framework Node 306/306、config 18/18、docs 4/4；deterministic full 2326 passed、
  1 skipped，四个 source-identity/canonicalization 专项与 CrossJob Methods consumer 变异回归均
  实际执行并 PASS；`deterministic.affected` 由策略判定为 `NOT_REQUIRED`，source verification
  对物化快照和工作树均为 `PASS`、无 drift。修复后的真实模型与 CrossJob 仍由后续 fresh
  Release 直接验证；本 verdict 引用元数据行不宣称被其所引用的源码快照覆盖。

## PL-FIX-029：CrossJob 安装树探针按目录遍历顺序而非完整路径规范序列计算身份

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：fresh Linux Client → Linux Server Release 的真实 meta-Skill 生成、v4 工具轨迹、
  canonical validator 与 gate-only semantic oracle 全部 PASS；`journey.cross-job.environment`
  随后在复制同一生成包后以 `GENERATED_SKILL_INSTALLED_TREE_DRIFT` 失败，尚未启动任何
  CrossJob Client/Server 工具调用。
- **受影响版本**：共享 CrossJob generated-Skill 安装边界，源码快照
  `git-visible-worktree-v1:79f4f1849a1a2bc179d2db6b57a0fef387c7a8a27bf257720f634982906e8c5a`
  （628 files）；所有内置 macOS、Windows、Linux host-client 以及 Darwin 编排的双 Linux
  container adapter 都复用该边界，fresh 与 restart 初始化均受影响。
- **根因**：生成侧先按完整相对路径全局排序文件记录再计算 `content_tree_sha256`；容器内独立
  Python 探针虽分别排序每层目录和文件，却按 `os.walk` 的“当前层文件先于子目录”顺序输出
  records，Node 消费者直接对这个有序数组做摘要。同一 9 个文件因此分别得到规范
  `e5bd219b...` 与遍历序 `fe76c460...`；文件路径、长度和 SHA-256 均未变化。
- **不可回归行为**：安装探针返回的普通文件记录必须在摘要前由消费者按完整 POSIX 相对路径
  全局规范排序，使树身份与遍历顺序无关；`cp -a` 后的 byte-for-byte `diff`、symlink、hardlink、
  owner、mode 检查及 exact content-tree equality 必须继续失败关闭。改变任一文件内容摘要仍必须
  触发 `GENERATED_SKILL_INSTALLED_TREE_DRIFT`，不得把真实 byte/path/node 漂移归一化掉。
- **修复历史**：2026-08-24，fresh Release `run-20260823T225100Z-95af9a24` 在真实生成
  14/16 turns、99,331 tokens、$0.858175 后完成 3 次必读、8 次连续写、3 methods、6 markers、
  6 templates 及 14 字段语义验证；环境 Gate 随后以上述排序漂移失败。修复只为安装探针 records
  增加完整相对路径规范化，并增加可注入的共享安装边界测试 seam；未改生成包或 identity 内容。
- **专项回归测试**：
  - `tools/test-flow/tests/cross-job-runtime-boundary.test.mjs` 中
    `installed generated Skill identity is independent of probe traversal order`：修复前唯一以
    `GENERATED_SKILL_INSTALLED_TREE_DRIFT` 失败，修复后 PASS；同时变异一个文件 SHA-256 并断言
    仍以同一错误失败关闭。
- **最新 Test Flow verdict**：修复后 Dev `run-20260823T230455Z-9da01210` 为
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:dfe0717e2a372655df788fb06180a6266b4100a14fca5395366c285901b9afe5`
  （628 files）。framework Node 307/307、config 18/18、docs 4/4；新增安装树遍历序专项实际执行
  并 PASS，deterministic full 2326 passed、1 skipped；`deterministic.affected` 的 selector 实际
  执行后由策略判定为 `NOT_REQUIRED/AFFECTED_SCOPE_DEFERRED_TO_FULL`。物化快照与工作树验证
  均为 `PASS`、无 drift。修复后的真实安装与 CrossJob 仍由后续 fresh Release 直接验证；本
  verdict 引用元数据段不宣称被其所引用的源码快照覆盖。

## PL-FIX-030：Server capability 的子进程树测试把夹具启动抖动计入 0.25 秒清理断言

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：排序修复后的 fresh Linux Release 在 `platform.server-linux-capability` 提前失败；
  三个离线 capability 用例中启动与安装两项 PASS，进程树用例已收到产品层
  `BACKEND_TIMEOUT`，却在读取 `output/proposals/child/child.pid` 时遇到空文件并以
  `ValueError: invalid literal for int() with base 10: ''` 退出。真实生成和 CrossJob 均未启动。
- **受影响版本**：AgentBackend 真实进程树测试夹具与 Linux Server capability，源码快照
  `git-visible-worktree-v1:53a66f5b5c52ae1a4698a860ce282f473b63cab8e8ebb0e10fd7cee338e98e6a`
  （628 files）；POSIX、Windows unit 的同类 `child-hang` 用例也存在相同潜在竞态。
- **根因**：fake Claude 先 spawn descendant，再用 `Path.write_text` 直接打开、截断并写最终 PID
  marker；AgentBackend 的 0.25 秒测试 wall clock 从 backend 启动即计时。在容器调度较慢时，
  timeout 可恰好落在目标文件创建与 PID 写入之间，测试观察到 0-byte marker。权威证据没有
  process-tree cleanup failure；失败发生在 `RuntimeExecutionError(BACKEND_TIMEOUT)` 返回后的
  测试观测代码，因此是夹具发布/同步竞态，不是已证实的生产整树清理缺陷。
- **不可回归行为**：fake descendant PID 必须通过同目录临时文件完整写入、`fsync` 后
  `os.replace` 原子发布，最终 marker 只能缺席或包含完整正整数。真实子树 cleanup 专项必须等
  marker 可解析后才推进 0.25 秒测试时钟，并有独立 5 秒真实启动上限防止永冻；之后仍必须由
  生产 `BACKEND_TIMEOUT` 路径终止真实 POSIX process group / Windows Job Object，并验证 child
  不存在。普通 `ignore-stdin` 用例继续以未门控的真实时钟覆盖执行起点 timeout；完整平台用例
  继续证明 Runtime 只调用一次、失败被发布且 replay 不重跑。
- **修复历史**：2026-08-24，fresh Release `run-20260823T230729Z-9f8b1223` 在零次真实模型
  调用时以 `SERVER_CAPABILITY_CONTRACT/EXTERNAL` 失败；JUnit 精确记录空 PID marker。随后新增
  共用的 test-only readiness monotonic、原子 PID 发布，并故意把 marker 发布延迟设为 0.35 秒
  （大于 0.25 秒 timeout），从而把启动调度与已就绪子树的清理断言机械分离；生产 AgentBackend
  与 process-tree 实现未修改。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_agent_backend.py::test_timeout_cannot_be_blocked_by_agent_ignoring_stdin`
  - `tests/deterministic/unit/runtime/test_agent_backend.py::test_timeout_terminates_complete_posix_child_tree`
  - `tests/deterministic/unit/runtime/test_agent_backend.py::test_timeout_terminates_complete_windows_child_tree`
  - `tests/platform/compat/test_macos_process_tree_gate.py::test_host_timeout_kills_the_real_child_tree_without_rerunning_agent`
  - `tests/deterministic/unit/runtime/test_agent_backend.py::test_backend_fixture_manifest_is_exact`
- **最新 Test Flow verdict**：修复后 Dev `run-20260823T231924Z-103252bb` 为
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:da3d6259020c510d290151d8d9946b0953efc525799453fd268816448f6a9f25`
  （629 files）。deterministic full 2326 passed、1 skipped：真实时钟 timeout、POSIX readiness/
  整树 timeout 与 fixture manifest 专项各实际执行一次并 PASS，Windows 同一整树用例被收集后按
  当前平台正确 skip；`deterministic.affected` 的 selector 实际执行后 deferred-to-full。framework
  与 repository 基于未变证明身份复用前序 PASS；物化快照与工作树验证均为 `PASS`、无 drift。
  后续 fresh Release `run-20260823T232209Z-0c67ba5d` 的 Linux Server capability 3/3 PASS，
  包含完整 JobWorker/process-tree 专项；该 run 的 overall 在更晚的真实 Skill 模板完整性 Gate 因
  PL-FIX-031 失败，所以不替代上述 Dev overall。verdict 引用元数据段不宣称被其所引用的源码快照覆盖。

## PL-FIX-031：Methods 完整日志模板只受散落文字约束且没有固定生成位置

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：fresh Linux Client → Linux Server Release 的真实 meta-Skill 调用通过 v4 source
  identity、三次必读、连续 Write 与 6 个 canonical marker 检查，但 canonical validator 随后发现
  生成包丢失三条完整 Wiki 模板：`API_COMPLETE ...`、`QUEUE_HISTORY ...` 和
  `DEADLOOP_DETECTED ...`；gate-only semantic oracle、生成包安装与全部 CrossJob stage 均未运行。
- **受影响版本**：Methods V7 的模板完整性合同与 Claude/Codex 两条真实生成流，源码快照
  `git-visible-worktree-v1:e0a5601beed8c7c5fbe2a49a67e834bed264dccaac9571f534eaafc85d420cb5`
  （629 files），fresh Release `run-20260823T232209Z-0c67ba5d`。
- **根因**：公开 meta Skill 只用散落 prose 要求“模板位于任意方法卡或共享引用”，既没有给模型一个
  可逐项核对的机械清单，也没有规定唯一文件和精确字节；`methods.json` 只索引 canonical marker，
  因而保留 6 个短 marker 不能证明 6 条完整模板存在。后置 validator 将整个 package 拼接后做
  substring 检查，能够发现遗漏，却没有在生成前给出同等精确的可执行合同。
- **不可回归行为**：调用方必须从原始 Wiki 以 extraction-v1 机械规则生成 closed-schema v2
  source identity，按源顺序保留重复模板并绑定 inventory SHA-256；v5 trace 必须从 Wiki 独立重算，
  仍只允许恰好三次 Read。生成 Agent 必须以一次成功 Write 创建精确
  `references/source-log-templates.md`，其字节固定为标题、一个 `text` fence、identity 中逐项逐序的
  完整模板和终止换行；该路径必须是 `shared_references[0]` 且不得作为 method reference。缺失、增加、
  改写、重排、去重、无 Write 的事后补文件、旧 v1 identity 或旧 v4 receipt 都必须失败关闭。
  canonical validator 继续直接从 Wiki 重算，不得信任 sidecar、放宽模板检查或在模型退出后修补包。
  Codex/Luna generation workspace 必须物化同一 v2/固定引用合同，同时保持 1 次生成 + 9 次诊断、
  无重试的十次调用边界。
- **修复历史**：2026-08-24，失败 Release 记录恰好 1 次 `deepseek-v4-flash[1m]` 调用、12/16
  turns、300,640 tokens、$0.929944；wrapper、terminal、v4 trace、Wiki hash、3 methods、6 markers
  与 14 个日志派生字段均通过，随后 canonical validator 精确列出上述三条缺失模板。修复把模板清单
  提升到 source identity v2，把模型写出的固定引用与摘要提升到 v5 trace receipt，并让 meta Skill、
  Python validator、Claude real gate、Codex/Luna producer/consumer 和文档共同消费；没有把本次三条
  缺失值或模型不可见 semantic oracle 硬编码进 prompt，也没有增加第四次 Read 或改变 methods-v1。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_meta_skill_source_identity.py::test_validator_reproduces_release_failure_when_three_full_templates_are_lost`
  - 同文件 `test_source_identity_v2_mechanically_preserves_template_order_and_duplicates`、
    `test_validator_requires_fixed_inventory_bytes_first_and_shared_only` 与
    `test_fixed_inventory_renderer_has_canonical_empty_bytes`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中
    `requires a closed canonical v2 source identity with the exact ordered Wiki template inventory`、
    `requires one traced deterministic source-log-templates shared reference` 与 closed v5 receipt 回归
  - `tools/test-flow/tests/codex-luna-contract.test.mjs` 中 source identity v2、generation workspace/
    prompt、fixed reference verifier 及 prompt mirror 回归
  - `tests/real/agent/test_real_wiki_skill_generation_gate.py::test_claude_2_1_89_pinned_model_generates_registered_methods_package`
- **最新 Test Flow verdict**：修复后 Dev `run-20260823T234416Z-02c34e77` 为
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:e6f93305f05a1c458d3672dfe3c8a9429545b55bc3f4449a186834903b34b008`
  （629 files）。framework config 18/18、framework Node 325/325、docs 4/4；deterministic full
  2330 passed、1 skipped、0 failed/error，source identity v2、fixed template reference、v5 receipt
  与 Codex prompt/verifier 专项均实际执行并 PASS；`deterministic.affected` selector 137/137 后按
  策略 deferred-to-full。四项 proof、event audit、payload seal、secret/meta scan、物化快照与
  工作树校验均为 `PASS`、无 source drift。真实 Linux→Linux 与 Codex/Luna 结论仍由后续 fresh
  Release 直接验证；本 verdict 引用元数据段不宣称被其所引用的源码快照覆盖。

## PL-FIX-032：Service usage auditor 把无日志的 Methods preflight 误判为 Docker 基础设施失败

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：fresh Linux Client → Linux Server Release 的真实 meta-Skill 生成、六模板独立校验、
  gate-only semantic oracle、生成包安装与双 Linux 环境均 PASS；CrossJob route 也已成功完成
  4 次 Client MCP 调用和 5 次 Server 工具调用并进入 `WAITING_ATTACHMENT`，但停止服务后的
  usage audit 读取 no-model Diagnose preflight 的 `stdout.log` 时遇到 `FileNotFoundError`，将
  Gate 封存为 `BLOCKED / INFRA / DOCKER_COMMAND_FAILED:exec`，upload 及后续 stage 未运行。
- **受影响版本**：Methods CrossJob route 的 service-agent usage 审计，源码快照
  `git-visible-worktree-v1:d7a5f02b04bafea1fbea645e3dbd7a19353067ae991bb7e330bfb6140c3e0ec1`
  （629 files），权威 Release `run-20260823T234813Z-857cdcb1`；consumer 收紧后的首轮回归影响
  `git-visible-worktree-v1:cf780785b6ad80a68565f0ea4ef8405ad9568f46deee0776647ac870e00dadab`
  （629 files），权威 Release `run-20260824T002858Z-36f58fe0`。
- **根因**：产品合同明确 Methods preflight 在缺少输入或日志归档时不启动 Agent backend、也不
  创建 execution log sinks；但 `audit_service_agent_usage.py` 在判断 `methods_preflight.json`
  前无条件读取 `stdout.log`。其既有单测人为创建空 stdout，覆盖了一个不存在于生产路径的状态，
  因而没有复现真实的“完整 preflight receipts + 不存在 stdout/stderr”字节形态。
- **不可回归行为**：合法 no-model Methods preflight 的 stdout/stderr 必须同时不存在；存在但
  为空的 log pair 也不得冒充 preflight。此路径只有在 canonical `Job`/`JobOutcome` 产品 schema、
  `validate_outcome_for_job`、preflight exact keys、registration/result/missing arrays、新增 requirements
  与 request IDs 全部闭合，且 Job 目录恰好只有三份 receipt 时，才能封存 schema-v2
  `methods-server-preflight`；receipt 必须声明 `model_invoked=false`、`log_pair=ABSENT` 并绑定三份
  原始字节 SHA-256；所有被读文件必须是 single-link ordinary file。顶层 service usage receipt
  必须闭合 schema，`new_job_ids` 必须与 model invocation 和 no-model evidence 的 job ID 去重并排序后
  精确相等，不能借伪造 ID 排除后续审计。route 必须把唯一 no-model receipt 同本次生成包的
  registration、`WAITING_ATTACHMENT` requirement 的来源 Job 和 `NEED_ATTACHMENT` 分支精确绑定。
  缺少或伪造 preflight、缺失模型 stdout、单边/空 execution log pair、非法 artifact、额外 Agent
  痕迹、硬链接、非空模型流同时存在 preflight 均必须 fail closed。不得创建伪 stdout、跳过模型
  usage，或放宽 terminal/model/cap 校验。
  通用 stage invocation 允许 Linux Client receipt 不携带 server `job_id`；UUID 约束只适用于
  service-agent producer evidence，不得把两个 receipt class 混成同一合同。
- **修复历史**：
  - 2026-08-24：auditor 先验证 stdout/stderr 是否为普通文件且成对存在；真实的双 absent 形态
    进入 schema-v2 `_methods_preflight` 闭合校验，其他缺失 stdout 以稳定错误拒绝。专项 fixture
    改为完整 canonical 产品 Job/Outcome 与生产真实的无 log-sink 形态；consumer 同步硬切 v2 exact
    keys 与三份 digest，增加缺/坏 preflight、单边/空日志、Agent 痕迹、非法 artifact/registration、
    outcome requirement linkage 与模型流冲突的负向回归。
  - 2026-08-24：独立审计发现 consumer 仍可接受顶层额外字段及与 evidence 不一致的
    `new_job_ids`，并且 route 只核对 `job_type=DIAGNOSE`。consumer 改为独立重算 exact sorted
    evidence union，拒绝重复、遗漏、追加、乱序、非字符串和 model/no-model 重叠 ID；route 再绑定
    generated registration、requirement `requested_by_job_id` 与 `NEED_ATTACHMENT`。auditor 同时拒绝
    `st_nlink != 1`，并将混合 mutation 拆成可证明各条约束的专项测试，补齐 NEED_INPUT-only 与
    NEED_INPUT+attachment 两个合法产品分支。
  - 2026-08-24：fresh Release `run-20260824T002858Z-36f58fe0` 已证明生成、安装、双 Linux
    环境和 route v2 no-model receipt 都有效，但 stage 封存以
    `MODEL_INVOCATION_RECEIPT_INVALID` 失败。根因是 UUID `job_id` 条件误加在 client/server 共用的
    successful invocation validator；修复把它下沉到 service-agent 专用 validator，并保留通用
    client receipt 无 server Job 身份的原合同。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_service_agent_usage_audit.py::test_absent_diagnose_stream_is_sealed_as_methods_no_model_preflight`
  - `tests/deterministic/unit/runtime/test_service_agent_usage_audit.py::test_missing_model_stream_without_methods_preflight_fails_closed`
  - `tests/deterministic/unit/runtime/test_service_agent_usage_audit.py::test_absent_stream_with_invalid_methods_preflight_fails_closed`
  - `tests/deterministic/unit/runtime/test_service_agent_usage_audit.py::test_one_sided_execution_log_pair_fails_closed`
  - `tests/deterministic/unit/runtime/test_service_agent_usage_audit.py::test_empty_execution_log_pair_cannot_impersonate_preflight`
  - `tests/deterministic/unit/runtime/test_service_agent_usage_audit.py::test_model_stream_with_methods_preflight_fails_closed`
  - 同文件 `test_preflight_file_set_fails_closed_on_agent_context`、
    `test_preflight_closed_schema_rejects_extra_key`、
    `test_preflight_registration_linkage_fails_closed`、
    `test_preflight_artifact_name_fails_closed`、
    `test_preflight_outcome_requirement_linkage_fails_closed`
  - 同文件 `test_need_input_only_preflight_is_sealed_without_model_logs`、
    `test_need_input_and_attachment_preflight_is_sealed_in_product_order`、
    `test_need_input_preflight_rejects_wrong_result_type`、
    `test_hardlinked_job_record_fails_closed` 与 `test_hardlinked_model_log_fails_closed`
  - `tests/deterministic/unit/runtime/test_diagnosis_runtime.py::test_methods_preflight_publishes_waiting_without_backend_or_broker`
  - `tools/test-flow/tests/cross-job-runtime-boundary.test.mjs` 中
    `service Agent usage receipt is closed and new_job_ids exactly equals its evidence union` 与
    `route no-model evidence binds the selected generated registration and waiting Job`、
    `client invocation omits server job_id while service Agent evidence still requires it`
- **最新 Test Flow verdict**：修复后 Dev `run-20260824T004019Z-8f6e9864` 为
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:872a3e5147ed5cb236aa6a4dbd567055a2f5f190742ac01c380a999364f60fcf`
  （629 files），materialized/live worktree expected 与 observed 完全一致、无 drift。framework
  config 18/18、Node 328/328、docs 4/4，新增 client/service receipt class 专项在 TAP 中实际
  执行并 PASS；deterministic full 2346 collected、2345 executed、1 explicit skip、0 failed/error。
  affected selector 实际执行后按策略 deferred-to-full；没有真实模型调用。本 verdict 引用元数据段
  不宣称被其所引用的源码快照覆盖。

## PL-FIX-033：Claude Quick 输出上限加入配置后旧 watchdog 合同仍只允许 isolated cap

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：新增 Claude/DeepSeek Quick Validation 后，规定的 `dev.default` 在
  `framework.config` 失败；`model watchdogs cover every serial Backend invocation and Stage evidence`
  仍断言除 `isolated.skill-generation` 外任何 real cap 都不能声明 `max_output_tokens`，因而拒绝
  已冻结为 64,000 的 `claude.macos-methods` 与 `claude.macos-e2e`。
- **受影响版本**：引入 Claude/DeepSeek Quick Validation 的 `60d0292` 至当前基线
  `5668fbb`；该失败与宿主平台无关，macOS/Linux 运行同一中央 deterministic Gate 都会触发。
- **根因**：运行时配置和 `config.mjs` 已把 `claude.*` 定义为合法的 Claude Agent cap 范围，
  但旧 watchdog 测试仍保留新增 Quick caps 之前的唯一 ID 白名单，提交时没有同步扩展闭合断言。
- **不可回归行为**：仅 `isolated.skill-generation`、`claude.macos-methods`、
  `claude.macos-e2e` 可以声明 `max_output_tokens`，且三者必须精确等于冻结 Claude runtime 上限；
  其他 real cap 必须继续不含该字段，配置校验器仍拒绝非 Claude scope、零值和超上限值。
- **修复历史**：2026-08-25，将旧单 ID 断言改为上述三个冻结 cap ID 的闭合集合；未修改
  Goal、Proof、Stage、Gate、runtime profile 或模型预算。
- **专项回归测试**：
  - `tools/test-flow/tests/config-contract.test.mjs` 中
    `model watchdogs cover every serial Backend invocation and Stage evidence`
  - 同文件 `isolated output token caps are positive and cannot exceed the pinned Claude runtime`
- **最新 Test Flow verdict**：修复后 Dev `run-20260825T090837Z-ef066b90` 为
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:09d3d9f7b328a71bfabf487290621fa28b570e9c5615b783cc4e399b0df5b70e`
  （669 files）。framework config 18/18、framework Node 349 passed/4 skipped、docs 4/4；
  deterministic full 为 contracts 569/569、unit 1730 passed/1 skipped、integration 45/45、
  SameJob 4/4，零失败/错误；source materialization、worktree verification、secret/meta scan 均
  `PASS`。本 verdict 引用元数据段不宣称被其所引用的源码快照覆盖。

## PL-FIX-037：Ubuntu Codex Quick E2E 沿用 macOS service workspace 与本地 venv 假设

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：现有 `dev.macos-codex-luna-e2e` 经 Ubuntu 22.04 wrapper 运行时，先后出现
  bubblewrap uid-map、`WORKSPACE_LIMIT`、materialized source path-set drift、合法附件 hard link
  被拒、`MACOS_CODEX_LUNA_SERVICE_LOGPARSE_MISSING` 和外层 `NO_PROGRESS`。其中真实 ROUTE
  transcript 一直正确选择 `diagnosis-skill/rpc-timeout-methods-v1`；失败发生在 Linux adapter/
  runtime 边界，而不是同一模型在 Linux 生成了不同业务内容。
- **受影响版本**：Ubuntu Codex Quick E2E 首次 root 适配至源码快照
  `git-visible-worktree-v1:31988d171be5449b84e53948911023028dc7dfe3c6020ca5f92c8fc15a83764d`
  （669 files）；权威失败证据包括 `run-20260825T104146Z-aa1552b0`、
  `run-20260825T111811Z-7e326442`、`run-20260825T113636Z-d7407c41`、
  `run-20260825T114319Z-e0238a40` 与 `run-20260825T114816Z-971e4232`。
- **根因**：adapter 曾给允许使用 root 的 Codex child 额外注入 nested `setpriv`/cap-drop，破坏
  bubblewrap uid map；恢复 root 后又把严格只允许 `inputs/output/runtime` 的产品 Workspace 直接
  当作 Linux Codex project cwd，Codex 的 `.agents/.codex/.git` 项目元数据触发 fail-closed。
  后续隔离镜像初版仍把 S02 合法只读 hard link 误当 Agent 输出 hard link，且 service runner
  假定源码 checkout 自带 `.venv/bin/problem-locator-logparse`。此外 Python `-I` 会忽略
  `PYTHONDONTWRITEBYTECODE`，服务导入 materialized source 时生成 `__pycache__`；Codex E2E main
  也没有像 Claude E2E 一样向外层 watchdog 转发已经观察到的语义进度。
- **不可回归行为**：Ubuntu wrapper 必须继续以 UID/GID 0、Docker 默认 root capability、只读根
  文件系统和 Codex-scoped `seccomp=unconfined` 运行，不得重新引入 nested setpriv/cap-drop。
  Linux service Codex project 必须位于产品 Workspace `runtime/` 下的可清理隔离目录，产品顶层
  仍只能是 `inputs/output/runtime`；S02 可信普通 hard link 可以作为复制源，但隔离副本和模型 draft
  必须为单链接普通文件，且只允许按阶段回写固定 draft。finalizer 与 Logparse CLI 必须由 runner
  显式绑定到所选 Python 环境的冻结同级命令；service Python 必须使用 `-I -B`。CLIENT、ROUTE、
  LOGPARSE、DIAGNOSE、REVIEW 的内部进度必须输出 allowlisted `stage.progress`，不得通过放大
  no-progress 超时掩盖心跳遗漏。
- **修复历史**：2026-08-25，撤销错误的 root child setpriv/cap-drop，保留 root bubblewrap 合同；
  增加 content-free Workspace identity 失败收据并据此确认顶层污染；Linux service 改用
  `runtime/test-flow-codex-project` 隔离项目，只发布固定 draft 后调用产品 finalizer/validator；
  复制器接受受信只读 hard-link 源但继续拒绝链接或非普通节点；E2E service 增加 `-B`，并显式
  传入 `problem-locator-seal-outcome-draft` 与 `problem-locator-logparse`；最后补齐
  `TEST_FLOW_PROGRESS stage.progress codex-luna ...` 语义心跳。每个真实失败后均使用新
  reason/hypothesis/expected evidence 重新 plan-only，未自动重试。
- **专项回归测试**：
  - `tools/test-flow/quick-validation/codex-luna/tests/macos-codex-luna-service-wrapper.test.mjs` 中
    `Linux service project contains Codex metadata without changing the product Workspace root shape`
  - 同文件 `server wrapper runs the one product-owned Logparse command without persisting broker credentials`
    与 `server wrapper seals a service outcome draft before the Agent process exits`
  - `tools/test-flow/quick-validation/codex-luna/tests/macos-codex-luna-e2e-runner.test.mjs` 中
    `E2E service launch disables Python bytecode inside the materialized source snapshot` 与
    `Codex E2E forwards semantic progress heartbeats to the outer Test Flow watchdog`
  - `tests/deterministic/unit/runtime/test_agent_backend.py::test_workspace_identity_failure_reports_content_free_root_shape`
  - `tools/test-flow/tests/wsl-quick-validation.test.mjs` 中 root/no-setpriv、system Codex 三件套及
    Ubuntu wrapper delegate 合同测试
- **最新 Test Flow verdict**：最终 Dev deterministic `run-20260825T120201Z-f36f4c31` 为
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:f992bed8b34ad8a95a80a2c5df95242aca9a5ba476ccc72fe42a53108deb104b`
  （669 files），framework、repository static、affected 与 deterministic full 全部实际执行并
  `PASS`。此前同一实现/专项测试字节的真实 Codex E2E
  `run-20260825T115405Z-cc714117` 同为 `PASS_WITH_WARNINGS`，验证源码快照
  `git-visible-worktree-v1:bec40897b9560216aefddb098f446e978acb774f05aaecacf1348c6bb2a5a294`
  （669 files）；真实 Gate 实际执行 CLIENT、ROUTE、LOGPARSE、DIAGNOSE、REVIEW 各 1 次且
  全部 terminal PASS，合计 834,437 tokens，Stage 223.337 秒。adapter、MCP、attachment、
  artifact、HTTP boundary、oracle、security、source materialization 与 worktree verification
  全部 `PASS`，secret scan 为零命中。本 verdict 引用元数据段不宣称被其所引用的源码快照覆盖。

## PL-FIX-038：Ubuntu Claude Quick E2E 依赖 macOS checkout 本地 venv 且会写 pycache

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：Claude Methods cache 和 Ubuntu E2E plan 均已 PRESENT/ADMITTED，但当前 service wrapper
  仍从源码 checkout 的 `.venv/bin` 查找 `problem-locator-seal-outcome-draft` 与
  `problem-locator-logparse`，同时以 `python -I` 启动 materialized source 中的测试服务却只靠
  `PYTHONDONTWRITEBYTECODE=1` 禁止 pycache。ext4 checkout 实际没有 `.venv`，因此真实流程若不修正
  会在 ROUTE/LOGPARSE 的机械命令处失败，并令 materialized source path set 漂移。
- **受影响版本**：修复前 Claude E2E plan-only 源码快照
  `git-visible-worktree-v1:e36355d6d82d56ffb2ac56eb8037ec171833cb80e60dd7c29054de631d1ca155`
  （669 files）；Claude Methods 不经过 E2E service launcher，不受此问题影响。
- **根因**：E2E runner 沿用了原生 macOS checkout 通常自带项目 `.venv` 的假设，没有把已经由
  action 选定的 Python runtime 同级产品 CLI 显式传给 service wrapper；Python `-I` 又隐含 `-E`，
  会忽略所有 `PYTHON*` 环境变量，因此环境变量不能阻止 source import 生成 `__pycache__`。
- **不可回归行为**：Claude service finalizer 与 Logparse 必须显式绑定到所选 `python-entry` 的
  同级冻结命令，不得依赖 checkout `.venv`、ambient PATH 或 Codex runtime。E2E 服务必须使用
  `-I -B`；Claude 继续使用自己的私有 settings、tool permission、禁止 service Bash 和既有
  `stage.progress` 心跳，不得复制 Codex 的 seccomp/bubblewrap 或 service-project mirror。
- **修复历史**：2026-08-25，在真实模型执行前复盘 Codex Linux 的已封存失败证据，并对当前
  Claude 入口、ext4 checkout 和 exact image 做零模型核对；仅增加显式 `finalizer-entry`、
  `logparse-entry` 和 `-B`，补齐闭合参数/源码专项测试。修复后的 plan 无 blocker/warning，首次
  真实 Claude E2E 即完整通过，没有失败后重试。
- **专项回归测试**：
  - `tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-service-wrapper.test.mjs` 中
    `service wrapper accepts only frozen Claude/provider roots and no external adapter` 与
    `service Claude process has no dangerous permission mode or model Bash`
  - `tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-e2e-runner.test.mjs` 中
    `E2E service uses isolated Python without writing bytecode into the materialized source` 与
    `client uses strict MCP, production Skill, exact Bash programs, and one fresh data root`
- **最新 Test Flow verdict**：最终 Dev deterministic `run-20260825T123115Z-05e81c44` 为
  `PASS`，functional、performance、operation、verification 均为 `PASS`；验证源码快照
  `git-visible-worktree-v1:1c923e0651978a29eb1ca16f7723b20d0f3685ecce0f93b233b59f46c9b7512a`
  （669 files）。此前同一实现/专项测试字节的真实 Claude E2E
  `run-20260825T122501Z-d6ce1f1e` 为 `PASS_WITH_WARNINGS`，验证源码快照
  `git-visible-worktree-v1:a16a736ac40fce05ab1545ecfd633ecb2cd5c1557aa57515470fa7515ab342d9`
  （669 files）；functional、operation、verification 均为 `PASS`，仅 performance 为
  `NOT_CALIBRATED`。真实 Gate 实际执行 CLIENT、ROUTE、LOGPARSE、DIAGNOSE、REVIEW 各 1 次且
  全部 terminal PASS，总计 715,605 tokens、USD 1.537385，Stage 193.558 秒；finalizer、Logparse、
  Methods canonicalizer、MCP、attachment、Bash policy、artifact、HTTP boundary、oracle、security、
  source materialization 与 worktree verification 全部 `PASS`，secret scan 为零命中。本 verdict
  引用元数据段不宣称被其所引用的源码快照覆盖。

## PL-FIX-035：Codex Quick runner 已 PASS 但中央 action 读取了不存在的旧 Gate 文件名

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：中央 Codex Methods runner 已完成 1 次模型调用、canonical validator、secret scan 和
  cache 原子发布，inner `adapter-receipt.json` 为 `PASS`，中央 Stage 却封存为
  `ERROR / HARNESS / MACOS_CODEX_LUNA_GATE_RECEIPT_INVALID`。权威失败 run 为
  `run-20260825T083916Z-82665555`。
- **受影响版本**：`c5a5858` 将 provider runner 统一为 `adapter-receipt.json` 后，仍保留
  `befc308` 中央 action/Gate 的旧 `gate-receipt.json` 消费合同；Codex Methods 与 E2E 两个中央
  Goal 均受影响，和 macOS/Linux 平台无关。
- **根因**：provider adapter receipt 与中央 engine 自有 Gate receipt 使用了两个不同层级的
  概念；Codex action/config 没有随 runner 文件名迁移，而 Claude action/config 已使用正确的
  `adapter-receipt.json`。中央 engine 的 `gate-receipt.json` 只有 action 返回后才写入，因此 action
  不可能在执行中读取它。
- **不可回归行为**：Codex provider runner、中央 action 与两个 Codex Quick Gate 必须统一消费
  `adapter-receipt.json`；中央 `gate-receipt.json` 仍只由 engine 封存。Methods cache 已存在时，
  planner 必须把 invocation caps 降为 0，并调用 runner 既有的 `--verify-cache-only` 路径；该路径
  必须重跑 identity、tree、canonical validator 与 security 校验，不得再次调用模型或重发费用。
- **修复历史**：2026-08-25，将两个 Codex Quick Gate 的 provider evidence 改为
  `adapter-receipt.json`，中央 action 同步读取该文件；接通已存在的 cache-only runner，并从实际
  invocation caps 推导期望调用数。失败 run 中成功发布的 package tree 为
  `84ca3319c586ba103f4b0b305fcac3112b024426a5c65cf95eece617abf63ea6`，validator 为 `PASS`；
  该结果不会通过再次生成来“修复”编排错误。
- **专项回归测试**：
  - `tools/test-flow/tests/wsl-quick-validation.test.mjs` 中
    `central Codex Quick Gates consume the provider adapter receipt and can verify a published cache without a model`
- **最新 Test Flow verdict**：最终 Dev deterministic `run-20260825T090837Z-ef066b90` 为
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:09d3d9f7b328a71bfabf487290621fa28b570e9c5615b783cc4e399b0df5b70e`
  （669 files），framework config 18/18、Node 349 passed/4 skipped、docs 4/4，deterministic
  contracts 569/569、unit 1730 passed/1 skipped、integration 45/45、SameJob 4/4。中央 Codex
  Methods cache-verification `run-20260825T091208Z-fc467dae` 同为 `PASS_WITH_WARNINGS`，验证源码
  快照为 `git-visible-worktree-v1:fadfa27763d04dcf9ad52c756e2e4c998b02f647591883607d3464ebc83d57ae`；
  invocation/usage 均为 0，package tree 保持
  `84ca3319c586ba103f4b0b305fcac3112b024426a5c65cf95eece617abf63ea6`，adapter、validator、
  cache identity、security、operation 与 verification 全部 `PASS`。verdict 引用元数据段不宣称
  被其所引用的源码快照覆盖。

## PL-FIX-036：Ubuntu Quick 镜像将 Logparse venv 放在中央 E2E 合同之外

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：Codex Methods cache 已 PRESENT 后，现有中央 `dev.macos-codex-luna-e2e` 的
  plan-only 仍被 `CODEX_LOGPARSE_RUNTIME_INVALID / CODEX_LOGPARSE_VENV_INVALID` 阻塞，真实 E2E
  模型调用数为 0。
- **受影响版本**：Ubuntu Quick 镜像
  `sha256:44dc69c6dc11935dcc7168c6641db4ee47eab6d0778bfbbc622c6d0b6772acbb`；
  对应源码快照
  `git-visible-worktree-v1:586c3ef44ee358fe50c9054c9400d010a0e6aa330c439e2baf4fea2c61f20e4f`
  （669 files）。
- **根因**：薄镜像把冻结 Logparse Python 3.12 venv 建在 `/opt/venvs/logparse`，而现有中央
  Codex E2E 身份和 action 固定消费干净 Logparse checkout 下的 `.venv/bin/python`；Methods 不需要
  Logparse runtime，因此直到 E2E planning 才暴露。合同明确拒绝以 venv 根符号链接替代真实目录。
- **不可回归行为**：镜像内 `/opt/logparse` 必须保持冻结 commit 且 Git clean，同时拥有真实目录
  `/opt/logparse/.venv`；其 Python 必须为 3.12.13，`sys.prefix` 必须精确等于该 venv 根，完整 tree、
  Python base、import paths 与 `cli.py` 必须继续由 `codexLogparseRuntimeIdentity` 冻结。不得放宽
  E2E planner、改用 ambient Python 或用被合同拒绝的 venv 根软链。
- **修复历史**：2026-08-25，镜像仍从冻结本地 cache 离线创建 venv，在复制并验证 Logparse
  checkout 后将完整 venv 移入 `/opt/logparse/.venv`，并把 Python 版本与 `sys.prefix` 加入
  network-none/read-only/root smoke；未修改 E2E runner、模型或业务合同。
- **专项回归测试**：
  - `tools/test-flow/tests/wsl-quick-validation.test.mjs` 中
    `the image supplies Ubuntu 22.04 runtimes and only a BSD-stat compatibility boundary`
  - `tools/test-flow/quick-validation/wsl/prepare-image.sh` 的 Logparse Python 版本与 `sys.prefix` smoke
- **最新 Test Flow verdict**：新 exact image
  `sha256:7ad87dbc0bacf0ac711ba6ca1215556454db3b080bb4f00c2b2d9af4494d87b0` 与 seal SHA-256
  `3ddf45df139abe6822399f235120c48a7d8e108c96832f4023c3efb8ca47d09d` 已通过
  network-none/read-only/root smoke；Codex E2E plan 中 `codex_logparse_runtime=PRESENT` 且
  admission `ADMITTED`，Claude E2E plan 同样 cache PRESENT/ADMITTED。最终 Dev deterministic
  `run-20260825T090837Z-ef066b90` 为 `PASS_WITH_WARNINGS`，functional、operation、verification
  均为 `PASS`，验证源码快照
  `git-visible-worktree-v1:09d3d9f7b328a71bfabf487290621fa28b570e9c5615b783cc4e399b0df5b70e`
  （669 files）。随后 Codex E2E 已越过本条 venv blocker，在独立的 ROUTE Workspace blocker
  `run-20260825T085932Z-3a2ea09e` 停止；该未完成事项记录于 `TODO.md`，不回归为本条 venv 失败。

## PL-FIX-034：Ubuntu 中央 Codex Quick 私有复制 CLI 后丢失相邻 Code Mode host

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：Ubuntu 22.04 容器中的现有中央 `dev.macos-codex-luna-methods` 已 ADMITTED，
  但真实 Gate 在生成包出现前失败；Codex 连续报告私有 scratch 下相邻
  `codex-code-mode-host` 不存在，runner 随后因 `generated/diagnose-rpc-timeout` 不存在而退出。
  权威 run `run-20260825T083119Z-d751652c` 的 Gate code 为
  `MACOS_CODEX_LUNA_RUNNER_FAILED`，模型 invocation receipt 与 Test Flow usage 均为 0。
- **受影响版本**：Ubuntu 22.04 中央 Quick wrapper 的首个真实源码快照
  `git-visible-worktree-v1:70b1054a6eec709cd90dca83e896ce6f774c1a8256bc8889c098679445c39583`
  （669 files）；原生 macOS 私有复制路径不受影响。
- **根因**：中央 action 延续 macOS 的 attempt-private CLI 复制策略，只复制 `codex`，而 Linux
  CLI 按自身相邻路径解析 Code Mode host；镜像中已经封存并验证的
  `/usr/bin/codex`、`/usr/bin/codex-code-mode-host`、`/usr/bin/codex-linux-sandbox` 三件套因此没有
  被作为同一运行时使用。
- **不可回归行为**：显式 sealed Ubuntu 22.04 Linux/x64 标记下必须直接执行只读镜像中的 exact
  `/usr/bin/codex`，让三件套保持相邻；认证仍必须复制到 attempt-private 0400 文件。非标记宿主及
  原生 macOS 必须继续使用 attempt-private CLI 副本。调用方不能覆盖 system entry，三件套版本与
  SHA-256 仍必须在 planning、执行和 receipt 中验证；不得改变 prompt、模型、reasoning、内容缓存键
  或独立 validator。
- **修复历史**：2026-08-25，将中央 Quick Codex entry staging 明确分为
  `sealed-system-entry` 与 `attempt-private-copy`，并对 Linux system entry 固定为
  `/usr/bin/codex`；失败后未以原身份盲重试。
- **专项回归测试**：
  - `tools/test-flow/tests/wsl-quick-validation.test.mjs` 中
    `the container marker selects only the frozen Linux Codex and app-server identities`
  - 同文件 `central Quick Goals accept native macOS or the explicitly marked Linux container only`
- **最新 Test Flow verdict**：修复后 Dev `run-20260825T090837Z-ef066b90` 为
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:09d3d9f7b328a71bfabf487290621fa28b570e9c5615b783cc4e399b0df5b70e`
  （669 files）。framework config 18/18、framework Node 349 passed/4 skipped、docs 4/4；
  deterministic full 为 contracts 569/569、unit 1730 passed/1 skipped、integration 45/45、
  SameJob 4/4，零失败/错误；source materialization、worktree verification、secret/meta scan 均
  `PASS`。本 verdict 引用元数据段不宣称被其所引用的源码快照覆盖。

## PL-FIX-039：WSL Fast E2E 错误委托中央 Goal，九场景缺少独立容器闭包

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：Linux 开发期 Fast E2E 被接入中央 macOS Quick Goal，九场景曾在单一 provider 流程中
  串行执行；后段暴露工程兼容问题后只能从第一例重新开始。改回 standalone 并发容器后，又依次
  暴露 utility tmpfs 缺少 `/private/tmp`、root 子证据无法由宿主用户归集、Codex curl 参数漂移、
  重复证据身份误计数，以及 Claude Methods/E2E 确定性 TAP 污染空 evidence root、跨挂载 rename
  `EXDEV`、Bash policy 拒绝规定的 `--max-time 60`、被拒 `Glob` 误判成已执行工具和中文 oracle
  词形过拟合等问题。
- **受影响版本**：Linux 九场景首次被扩入中央 `config/proofs/stages/identities/planner` 的错误方向，
  至 standalone 九容器 wrapper 和两 provider runner 完成闭包前的工作树。代表性失败证据包括
  `wsl-codex-luna-suite-20260826T040250Z-74259`、
  `wsl-codex-luna-suite-20260826T040518Z-74728`、
  `wsl-codex-luna-suite-20260826T043058Z-76512`、
  `claude-deepseek-20260826T045937Z-ab93e44d`、
  `claude-deepseek-20260826T050301Z-c738707d` 和
  `wsl-claude-deepseek-suite-20260826T050818Z-79772`。
- **根因**：测试入口边界定义错误，把开发期 Fast E2E 与中央 Release/Test Flow 证明混为一层；
  WSL wrapper 又只处理平台委托，没有拥有九个独立容器的启动屏障、子根、root 权限归集和机械聚合。
  两 provider 的原生 runner 仍隐含 macOS 单文件系统、空 evidence root、shell 参数和模型中文措辞
  假设，专项测试未覆盖真实 WSL 挂载、并发及被拒工具事件。
- **不可回归行为**：Linux/macOS 开发期 Fast E2E 只能走 provider standalone 或密封 Ubuntu 22.04
  WSL wrapper，不得重新接入中央 Goal/Proof/Stage/Gate。WSL `--all-scenarios` 必须先完成零模型规划和
  共享预检，再同时启动九个容器；每个容器只跑一个场景，拥有独立 scratch、DATA_ROOT、服务端口、
  usage 和 evidence。Methods cache 缺失或身份漂移必须零调用阻断；suite 不自动重试。九份子 verdict
  必须由 root utility 原子归集，根 verdict 按固定顺序重算 44 次活动、usage、完成数和停止原因。
  Codex 与 Claude 都必须接受自然中文等价词形，但仍严格核验 oracle 状态、正向证据、来源身份、
  附件、MCP、HTTP、artifact、模型调用数、预算、secret 和零重试边界。正式 Release、源码快照证明
  和修复登记仍只认中央 Test Flow。Claude 只允许一次未实际轮询的 `get_case` 语法纠正：空 `{}`
  必须立即补全，或把被错误编码成字符串 `"null"` 的 `wait_for_job_id` 以相同 `case_id`、
  `wait_seconds` 立即改成原生 `null`/省略可选字段；其他 validation/business error 继续拒绝。
  `prepare_attachment` 的 size/SHA 不得传 `null`。被拒且未执行的工具尝试必须留痕但不能冒充执行；
  九路并发下单进程 no-progress 为 300 秒，仍受 600 秒硬墙钟约束。
  Claude 每次 `get_case` 必须在同一 `tool_use.input` 一次性携带完整三字段，不能先发送空 `{}` 再补
  参数；并发五阶段已确认的 USD 3.280786 合法样本必须准入，单例仍以 4 USD、suite 以 36 USD
  硬阻断，2,000,000/18,000,000 token 上限保持不变。三字段约束必须使用 Claude Code 原生
  `--append-system-prompt`，收据只保存 prompt SHA-256 与 UTF-8 字节数；不得用 Hook、代理或参数修补。
  Codex app-server 的 client/service 约束必须使用原生 `thread/start.developerInstructions`：所有命令 cwd
  固定在 invocation workspace，service draft 逐字绑定 `inputs/manifest.json` 与冻结输入，client 的
  attachment size/SHA 逐字绑定确定性 ZIP；收据同样只保存指令 SHA-256 与 UTF-8 字节数，命令 cwd
  越界、未落地来源或附件字节漂移仍必须失败。
  client/service developer instructions 必须携带各 invocation 的绝对 workspace；已由 app-server 加载的
  client Skill 禁止再用 shell 读取，client 第一条允许的命令只能是 workspace 内的附件 openssl/stat。
  client 每个 turn 最多 16 次 `get_case`；每次响应后必须重新读取 `case_view.active_job.job_id`，Job
  切换时立即改用新 ID，`active_job=null` 时不得复用旧 ID。client 总 MCP 启动数另以 24 次硬上限
  早停并封存脱敏 transcript，防止紧轮询耗尽 app-server stdout；这不是自动重试。
  app-server 进程还必须使用 CLI 原生全局 `-C <workspace>`，并与 `thread/start`、`turn/start` 的 cwd
  绑定同一路径；启动参数收据以 `<WORKSPACE_ROOT>` 占位，不把一次性 run 路径混入协议身份。
  Codex client 的 `prepare_attachment` 只有在 `declared_size`/`declared_sha256` 被零副作用
  `VALIDATION_ERROR` 拒绝时，才允许保留同一 request_id、Case、revision、name 与 content_type，
  立即按冻结附件身份精确纠正一次；插入其他调用、修改其他参数、第二次纠正或进程级重试都必须失败。
  Service evidence 的 `line_number` 必须按对应 `source_id` 单文件从 1 计数并逐字复制整行；
  `LATE_RESPONSE` 必须优先绑定相同 `request_id` 的 timeout，不能被旁边通用 timeout 覆盖；所有已加载
  method 与 marker occurrence 必须枚举完再结束诊断。
- **修复历史**：2026-08-25 至 2026-08-26，精确撤出中央九场景扩展并保留四个单场景可选认证 Goal；
  为两个 provider 增加九场景 standalone suite，WSL wrapper 改为九容器 fan-out。随后依据每次封存
  证据补齐 utility tmpfs、root materialization、跨设备 TAP 复制校验后同设备 rename、固定 descriptor
  upload、证据源身份去重、Claude policy/audit-only denied tool 和 REVIEW 限制语边界。每次真实重跑
  均使用新 run ID 与新的 reason/hypothesis/expected evidence，未自动重试。最终并发证据又补齐空
  `get_case` 的一次性语法纠正、非空附件声明、通用 denied-tool 识别和 300 秒 no-progress 边界；
  terminal Case 为 `FAILED` 时直接投影 service 工程失败，不再降级成 artifact 为空。Claude 九容器
  `wsl-claude-deepseek-suite-20260826T062433Z-85046` 又取得 8/9 PASS，并保留了唯一失败场景
  `server-queue-delay`：模型把原生 `null` 生成成字符串 `"null"`，服务端零副作用拒绝后立即以相同
  Case/poll 参数省略可选字段并完成 RESOLVED；据此把 recovery 审计收窄为只承认这一次精确更正。
  修复后的受影响单例 `claude-deepseek-20260826T063950Z-a021b57a` 为 PASS，模型直接使用原生
  JSON `null`，5/5 个进程、零重试，730,105 tokens、USD 1.701213。随后最终字节九容器
  `wsl-claude-deepseek-suite-20260826T064721Z-86600` 封存为 ERROR：`client-receive-blocked`
  的模型思考包含完整参数，但实际 `tool_use.input` 七次为空 `{}`，审计按合同拒绝；
  `multiple-rpc-timeouts` 五阶段全部 terminal PASS、1,325,546 tokens，但 USD 3.280786 超过旧 3 USD
  单例上限。据此强化调用点提示且不放宽空输入，并把有权威逐阶段 usage 支持的单例上限校准为 4 USD。
  受影响单例 `claude-deepseek-20260826T070431Z-db970c54` 随后以 5/5、零失败 envelope 通过；但
  `claude-deepseek-20260826T070435Z-7b2e4c6b` 仍两次生成空 `tool_use.input`，模型还在 Skill 通用更正
  建议与本场景强约束之间反复权衡。Claude Code 2.1.89 的密封 CLI help 已确认原生支持
  `--append-system-prompt`，因此将不可恢复的三字段约束提升到 system 层，并显式覆盖该通用建议。
  system-prompt 修复后的 `claude-deepseek-20260826T071732Z-ff912dc3` 为 PASS：客户端流没有任何
  failure envelope，5/5 个进程、零重试、1,127,984 tokens、USD 2.821576；CLIENT receipt 绑定
  system prompt SHA-256 `e2cd70575aa870e58396b3a1eb1267e87d28781119c48ceb73eb41217e704963`
  与 451 UTF-8 字节。
  同一最终字节的 Codex 九容器 `wsl-codex-luna-suite-20260826T073828Z-89435` 为 ERROR、4/9 PASS、
  19/44 次调用、零重试：三个表面不同的 service/runner 失败均由
  `CODEX_LUNA_APP_SERVER_COMMAND_WORKSPACE_INVALID` 触发；`api-execution-overrun` 的 DIAGNOSE draft
  未绑定冻结来源而被 finalizer 以 `OUTCOME_INVALID` 拒绝；`server-queue-five` 把冻结 SHA 中的
  `…7134f…` 抄成 `…713f…`。审计均正确，未予放宽；改用 app-server 原生 developer instructions
  提升 cwd、manifest 与动态 archive identity 约束，并让 cwd 越界失败保留脱敏 transcript。
  五个受影响单例随后并发验证：`client-receive-blocked`（`luna-20260826T075845Z-0119ad5a`）、
  `server-queue-delay`（`luna-20260826T075842Z-c6207a3b`）和 `server-queue-five`
  （`luna-20260826T075835Z-1a2698b4`）均 PASS，关闭 cwd 与 SHA 问题；
  `multiple-rpc-timeouts`（`luna-20260826T075835Z-3bbea056`）5/5、零重试但漏掉
  `LATE_RESPONSE request_id=501`，原因是误用通用 5000 而非同 reqid 的 3000 毫秒预算；
  `api-execution-overrun`（`luna-20260826T075833Z-e5405769`）仍 `OUTCOME_INVALID`，新证据确认 Agent
  把仅 1 行的 `server.log` 标成“第 3 行”。据此继续收窄 developer instruction，不修改 oracle/grounder。
  收窄后 `multiple-rpc-timeouts`（`luna-20260826T080846Z-39dc656b`）以 5/5、零重试 PASS；
  `api-execution-overrun`（`luna-20260826T080843Z-9db3667d`）越过 service finalizer，却在 client
  `prepare_attachment` 把 64 位冻结 SHA 抄成 63 位，服务端按合同零副作用拒绝。为此新增上述一次性
  精确纠正能力及完整 MCP ledger 审计，不改服务端 schema、不增加 Hook，也不做场景自动重试。
  修复后 `api-execution-overrun`（`luna-20260826T082301Z-f00683da`）以 5/5、零重试 PASS；client
  首次即提交完整 64 位 SHA，13 个 MCP 调用全部成功，未实际使用纠正分支。该例耗时 233.194 秒，
  992,571 tokens、等价 USD 0.065418。
  随后的完整九容器 `wsl-codex-luna-suite-20260826T083031Z-92866` 为 8/9 PASS、39/44、零重试；唯一
  `server-queue-five` 在 ROUTE 首次读取 manifest 时把命令 cwd 落到外层 run 根，完全相同的下一条命令
  才回到 invocation workspace，最终被现有越界审计以
  `CODEX_LUNA_APP_SERVER_COMMAND_WORKSPACE_INVALID` 正确拒绝。密封 transcript 证明
  `spawn(..., {cwd})` 和 thread/turn cwd 尚不足以稳定约束 Codex CLI 0.149.1 的首个 shell；据其官方
  CLI help 增加全局 `-C`，让进程、thread、turn 三层工作根一致，不放宽越界合同。
  `-C` 后的受影响单例 `luna-20260826T084913Z-c23145da` 已让 ROUTE、LOGPARSE、DIAGNOSE、REVIEW
  四个 service invocation 全部完成，并越过附件上传；但 client 第一条命令仍尝试用外层 run 根重复读取
  已附加的 `.agents/skills/problem-locator-client/SKILL.md`，失败后才在 client workspace 成功，最终
  继续被越界审计拒绝。由此进一步把绝对 workspace 写进 client/service developer instructions，禁止
  client shell 重读已加载 Skill，并固定其第一条允许命令为附件身份核对。
  修复后 `server-queue-five` 单例 `luna-20260826T085850Z-eaed939b` 以 5/5、零重试 PASS，耗时
  249.593 秒，836,031 tokens、等价 USD 0.058809。client 仅有两条命令，第一条即为附件
  openssl/stat，未再读取 Skill，且 cwd 完全一致；ROUTE、LOGPARSE、DIAGNOSE、REVIEW 均 PASS，
  23 条 service command 全部位于各自 `runtime/test-flow-codex-project`，无越界。
  下一次完整九容器 `wsl-codex-luna-suite-20260826T090455Z-94784` 再次为 8/9 PASS、39/44、零重试；
  `server-queue-five` 已在完整并发中 PASS，唯一失败转为 `multiple-rpc-timeouts` 的
  `CODEX_LUNA_APP_SERVER_STDOUT_LIMIT`。服务 DFX 证明四个 service invocation 均已完成，但 client 在
  DIAGNOSE Job 结束后仍复用旧 `wait_for_job_id`，3 分多钟内累计 5,163 次 `get_case`（正常 PASS 样本
  `luna-20260826T080846Z-39dc656b` 只有 12 次总 MCP）；5,163 次响应把 app-server stdout 推过 64 MiB。
  据此增加上述 Job handoff/轮询上限，并在 runtime 流式计数，超过 24 次立即失败，不再等到 stdout 膨胀。
  修复后 `multiple-rpc-timeouts` 单例 `luna-20260826T092338Z-92061bf0` 以 5/5、零重试 PASS，耗时
  275.851 秒，1,024,818 tokens、等价 USD 0.065500；总 MCP 为 14 次。第 10 次 `get_case` 返回新
  REVIEW Job ID 后，第 11 次立即切换到新 ID，最终正常到达 `UNRESOLVED`，未触发 24 次早停。
- **专项回归测试**：
  - `tools/test-flow/quick-validation/wsl/container-suite.test.mjs`：九容器计划、root 归集、固定顺序聚合、
    合同失败继续、工程失败封存和零调用阻断。
  - `tools/test-flow/tests/wsl-quick-validation.test.mjs`：WSL 只能委托 standalone、中央 Goal 不作为 Linux
    Fast E2E、密封 Linux 准入、普通 Linux 拒绝及容器边界。
  - `tools/test-flow/quick-validation/standalone-suite.test.mjs` 与两 provider `framework.test.mjs`：九场景、
    44 次活动、隔离子根、usage 重算、停止语义和 Methods cache admission。
  - Codex `macos-codex-luna-e2e-contract.test.mjs`、`macos-codex-luna-e2e-runner.test.mjs`、
    `macos-codex-luna-service-wrapper.test.mjs`：证据身份、descriptor upload、manifest 绑定、四/五阶段，
    prepare 声明错误只能立即以同 request_id 和冻结身份精确纠正一次的正反例，以及 client 禁止 shell
    重读已加载 Skill、首命令类型和 client/service 绝对 workspace 注入。
  - `tools/test-flow/tests/codex-luna-app-server-runtime.test.mjs`：原生 developer instructions 转发、
    指纹收据、全局 `-C` 与 OS cwd 同源、脱敏失败 transcript 及原有 app-server 权限/身份边界；
    还直接复现第三次 MCP 启动越过测试上限时的流式早停；
    `tools/test-flow/tests/codex-luna-app-server.test.mjs` 核验动态启动参数和稳定占位收据。
  - Claude `claude-deepseek-bash-policy.test.mjs`、`claude-deepseek-e2e-runner.test.mjs`、
    `claude-deepseek-service-wrapper.test.mjs`：空根 staging、EXDEV 归集、`--max-time 60`、denied Glob、
    REVIEW limitations、字符串 `"null"` 的一次性同 poll 更正、空 `tool_use.input` 强阻断、
    system prompt argv/收据、五阶段 USD 预算校准与中文词形。
- **最新 Test Flow verdict**：当前交付字节的密封 Ubuntu standalone 专项回归为 127/127 PASS。
  Codex Linux Standalone Fast E2E 根 verdict `wsl-codex-luna-suite-20260826T093151Z-96206` 为 PASS：
  9/9 场景、44/44 次模型调用、`retry_count=0`，九个容器均 exit 0，工程失败与 stop reason 均为空；
  总耗时 282.248 秒，7,886,191 tokens、等价 USD 0.502484。固定顺序的
  `api-execution-overrun`、`client-receive-blocked`、`deadloop-detected`、`insufficient-evidence`、
  `multiple-rpc-timeouts`、`server-queue-delay`、`server-queue-five`、`server-queue-single`、
  `unrelated-log-noise` 全部 PASS。该 standalone verdict 只证明 Codex Linux 开发期 Fast E2E；Claude
  未在本轮最终字节上重跑，中央 `dev.default` 与 Release 也按用户明确指示未执行，不能据此外推为中央
  Test Flow、Release 或源码快照证明。本 verdict 引用元数据段不宣称被其所引用的测试字节覆盖。

## PL-FIX-040：局域网 Logparse 元 Skill 生成物无法注册，Fast E2E 绕过真实 MCP 与 Server

- **状态**：回归代码已修复；验证范围以本条“最新 Test Flow verdict”和“最新 Linux Fast E2E verdict”分别为准。
- **症状**：`d13675d` 新增的元 Skill 只生成可在 Claude Code 本地直跑的 `SKILL.md`、
  `logparse.json` 和打包脚本，没有 Server 必需的 `registration-template.json`；因此产物不能放入 Linux
  Server `SKILL_DIR`，也无法参与 ROUTE。配套 Fast E2E 另建 provider，用仓库内 broker 合同桩直接跑
  生成物，并由生成物自行调用 Helper、broker 和 ZIP 打包器；它没有复现用户实际的
  `problem-locator-client -> HTTP MCP -> ROUTE -> LOGPARSE -> DIAGNOSE -> REVIEW -> result.zip`
  链路，所以错误地把不可部署的产物判为可用。修复提交 `0b2f2e6` 部署到真实局域网后又暴露 ROUTE
  回归：Agent 把 `SKILL_INDEX.skills[*].registration_id=diagnose-rpc-timeout` 写成
  `payload.skill_ref.id`，而唯一可用身份实际是
  `diagnosis-skill/diagnose-rpc-timeout`，Server 因 exact-ref 不匹配以 `OUTCOME_INVALID` 拒绝。
- **受影响版本**：提交 `d13675d49c6b8e86a9ddb6f5eb209ca1c45090b8`（`feat: add LAN logparse
  diagnosis meta skill`）；首次重构提交 `0b2f2e667608637519fecc87cdd28b71eb943de4` 仍受 ROUTE 短 ID
  歧义与 Fast E2E 错误安装业务 Skill 影响。
- **根因**：把“局域网 Claude Code 直用”误解成客户端本地执行定位 Skill，混淆了客户端 Skill、
  Server registration、Methods package 和 Server 预处理 Helper 四层职责；测试又复制出一套
  `claude-deepseek-lan-skill` provider，以本地 broker 桩和自定义 ZIP 代替真实服务端链路，没有复用
  已有 `claude-deepseek` 用户旅程。首次重构后，独立审查又确认三个会污染真实结论的缺口：Claude
  E2E 服务端没有把当前 `sourceRoot` 注入 Python，中央 planner 对 `insufficient-evidence` 仍固定声明
  5 次调用，客户端审计也没有证明 `problem-locator-client` 确实加载；定向测试当时仍会假通过。
  后续局域网证据确认，`SKILL_INDEX` 又同时暴露完整 `ref` 和短 `registration_id`，DeepSeek 将后者误作
  VersionedRef ID。对应 Fast E2E 还把生成的业务 package 复制进 Server Claude 的 Agent Skill 目录，
  并允许 ROUTE、DIAGNOSE、REVIEW 调用 `Skill(diagnose-rpc-timeout)`；测试环境因此比真实部署多出一条
  非产品路径，虽然最终 exact-ref 审计严格，仍可能掩盖 Router 对双身份字段的依赖。移除该假路径后的
  首次真实 E2E 又确认，Server wrapper 的非 LOGPARSE 写权限没有绑定 Job 工作区的绝对 `output/`
  路径；Claude Code 在 `dontAsk` 模式下因此拒绝合法 draft。进一步修正路径值后确认，Claude Code
  对实际 `Write` 工具使用 `Edit(...)` 权限类别，且权限语法以单 `/` 表示项目根、双 `//` 表示文件系统
  绝对路径；`Write(...)` 或 `Edit(/run/...)` 都不会放行。权限失败后的模型还写入了 Claude 默认放行的
  memory 目录，说明仅依赖默认权限不足以证明 Job 边界。runner 又先审计客户端 recovery/Bash、后检查
  terminal Case，把真实 `BACKEND_EXIT_FAILED` 掩盖成无关的次生审计错误。
- **不可回归行为**：元 Skill 必须生成完整 PRODUCTION registration 根目录；
  `registration-template.json` 与 `package/diagnose-*` 必须能被当前 Server loader 原样装载。
  `client_slot`、`client_process_name`、`server_slot`、`server_process_name` 必须是动态且必填的 USER_FACT；
  client/server 共用生成时确认的固定 module，PID 只允许作为可选 USER_FACT，内部
  `logparse_product` 固定为 `default`。业务 Skill 不得调用 `logparse-diagnose`、broker、Logparse CLI
  或自行打包 ZIP。Server LOGPARSE Pass A 必须先且只加载一次现装 `logparse-diagnose`，随后只执行一次
  job-scoped broker 命令；Helper 加载失败、broker 失败或重试都必须停止。Fast E2E 只能复用现有
  `claude-deepseek` provider：同一客户端只装 `problem-locator-client`，经 HTTP MCP 触发精确 ROUTE，
  最后按服务端 descriptor 下载并独立校验 `result.zip`；不得再增加本地直跑 provider、broker 桩或
  客户端自制 ZIP。ROUTE `SKILL_INDEX` 只能暴露完整 namespaced `ref`，不得同时暴露可被误作 ID 的短
  `registration_id`；Router 必须逐字段复制同一个 `ref` 对象。Fast E2E 的 Server Agent 只能安装
  `logparse-diagnose`，业务 package 只能由 Server Catalog 从 `SKILL_DIR` 加载并注入上下文，所有非
  LOGPARSE 阶段都不得获得或调用 Agent Skill 工具。非 LOGPARSE Agent 的工具集合只能含 Read/Write；
  权限规则必须按 Claude 双斜杠绝对路径语法，用 `Read(//<Job绝对路径>/**)` 与文件写入类别
  `Edit(//<Job绝对路径>/output/**)` 精确绑定；wrapper 还必须独立拒绝文件工具报错、Job 外 Read 和
  `output/` 外 Write，包括 Claude 自动放行的 memory 目录。terminal Server Job 失败必须先于
  recovery、上传、下载和 Bash 审计报告，不能被次生错误覆盖。
- **修复历史**：2026-08-27，删除 `claude-deepseek-lan-skill` provider、仓库内 broker 桩和元 Skill
  固定 packer；把元 Skill 改为生成完整 registration 与 Methods package，并让 validator 同时核对
  Server loader 限额、路径、角色、固定 module、`default` product、方法卡和禁止越权调用。Server
  增加闭合的 Helper-first Pass A，且把 broker 审计收紧为总操作数恰好一次并成功。现有
  `claude-deepseek` generation cache 改为缓存模型生成的完整 registration 根；E2E 由客户端经 HTTP
  MCP 跑完整五阶段，在 LOGPARSE 模型轨迹中核验 Helper -> 唯一 broker 顺序，并由客户端下载、runner
  复核 Server v3 ZIP。独立审查后又把 `TEST_FLOW_SOURCE_ROOT` 精确绑定到本次只读源码根，中央计划与
  invocation ledger 改为按场景核对阶段、顺序和数量，并新增客户端 Skill 双账本审计：首个且唯一
  成功加载的 Skill 必须是 `problem-locator-client`；缺失、重复、其他 Skill 或加载失败一律拒绝。首次
  WSL 真实 E2E `claude-deepseek-20260826T180040Z-ba507a95` 在零模型合同 Gate 封存为 FAIL：三个新增
  Node 用例把临时目录建在只读源码根，触发 `EROFS`，`actual` 和 usage 均为空。确认首错后把这三个
  用例统一改用 `os.tmpdir()`；该失败没有触发模型调用，也没有以原字节盲重试。修复后真实 E2E
  `claude-deepseek-20260826T180325Z-e1468e95` 的五个模型进程和客户端下载均完成，但 runner 以
  `CLAUDE_DEEPSEEK_SELECTED_SKILL_MISMATCH` 拒绝：cache 派生 ref 为 `19f60e…`，Server 实际 ref 为
  `b92d13…`。根因是 JS 用 `localeCompare` 排 package 路径，把 `SKILL.md` 排在小写文件之后，而
  Server 的冻结 Python 合同按 Unicode code point 排序。修复后改用显式 code-point 顺序，并把
  generation contract 提升到 v3，使错误 cache 保持不可变并以新 producer identity 自然失效；没有
  放宽 exact ref 审计。随后新身份 generation `claude-deepseek-20260826T181538Z-06fbd352` 的模型调用
  正常结束，但 canonical validator 拒绝了 6 个缩短 marker。根据封存轨迹重建产物后确认，模型把
  `API_COMPLETE`、`LATE_RESPONSE` 等事件名当成 marker，漏掉了 Wiki 模板中的稳定字面前缀。修复后
  runner 从 source identity 的模板机械计算并在 prompt 中公开完整 canonical marker allowlist，不公开
  隐藏 oracle 的方法分配；generation prompt 提升到 v3。元 Skill 同时新增明确反例，防止局域网直接
  生成时再次把事件名缩写当作 marker。prompt v3 generation 通过后，真实 E2E
  `claude-deepseek-20260826T183743Z-3fec2607` 已确认新 ref `e433b2…` 与 Server 完全一致，但 Client
  Host 意外生成了一次空 `get_case`；测试专用 system prompt 又要求不可恢复地停止，与现装
  `problem-locator-client` 允许一次紧邻零副作用语法纠正的合同冲突。修复后 client prompt v2 仍禁止
  主动空调用，只允许同一模型进程在 Server 返回 `VALIDATION_ERROR` 后立刻用当前 Case、原生 null 或
  真实 Job UUID、`wait_seconds=30` 完整纠正一次；中间插入工具、第二次空调用或纠正失败仍拒绝。
  该分支不使用 Hook、不改写 MCP 参数、不修改服务端 schema，并把 prompt 版本、SHA-256 和字节数
  加入 standalone plan identity。client prompt v2 的真实 E2E
  `claude-deepseek-20260826T184641Z-3f332462` 随后完成五个阶段，但旧生命周期审计把真实附件旅程中的
  两个 DIAGNOSE Job 误判为异常：第一个是缺附件时的零模型 preflight，第二个才是补件后的实际诊断；
  审计还固定从第一个 Job 读取 broker audit。修复后精确要求一个 preflight 和一个实际 DIAGNOSE，
  两者都必须保持 `SPECIALIZED` 与 exact ref，并用最终 `server.outcome.job_id` 选择实际诊断 Job；多余
  或缺失 Job 仍拒绝。standalone plan 也新增完整 provider runtime tree SHA-256，确保审计代码变化会
  形成新的可核验运行身份，而不是只靠 retry 说明区分。随后
  `claude-deepseek-20260826T185756Z-5cd7d2ce` 的 Client Host 连续生成两次空 `get_case`，有界纠正审计
  正确拒绝。轨迹同时暴露共享 client prompt 要求已知 Job ID 时传 UUID，而现装 Skill 要求普通轮询
  始终传 null。client prompt v3 移除这项选择：本场景 `wait_for_job_id` 固定为原生 null，等待时用 30，
  仅两次 revision refresh 用 0；空调用纠正也只接受 null 和 0/30。provider-specific prompt 会机械确认
  已替换共享冲突句，未替换时直接阻断。最终 standalone E2E
  `claude-deepseek-20260826T190333Z-5fd045f7` 为 PASS：5/5 个模型进程、`retry_count=0`，总计
  1,002,709 tokens、USD 1.769453。CLIENT 首工具唯一加载 `problem-locator-client`；ROUTE 为
  `MATCHED`；一个附件 preflight 与一个实际 DIAGNOSE 都保持 `SPECIALIZED` 和 ref `e433b2…`；
  LOGPARSE 恰好先加载一次 Helper、再调用一次 broker，无 fallback/retry；双端 anchor 为 module
  `rpc`、slot `1`、进程 `rpc_client/rpc_server`。客户端下载的 Server v3 `result.zip` 为 3747 字节、
  SHA-256 `7a524e…c3cd`，包含 `result.txt`、`archive-manifest.json` 和两份实际使用日志；安全审计 PASS，
  没有凭据落盘。该 standalone verdict 只证明本次 Fast E2E，不替代中央 Test Flow 或 Release。首次
  中央 Dev `run-20260826T192433Z-676b81f2` 随后在同一源码快照上封存为 FAIL：framework、repository、
  affected、contracts、unit 和 integration 均 PASS，只有 SameJob 的两条真实 Methods 旅程在 Pass A
  报 `BACKEND_EXIT_FAILED`。失败证据确认生产 Runtime 已输出新的 `SERVER_PREPROCESS` 首行，但
  `tests/fixtures/rpc_timeout/fake_agent.py` 仍只识别旧首行，误把预处理 prompt 当成普通 DIAGNOSE，读取
  不存在的 `JOB_INSTRUCTION` 后异常退出。修复只更新确定性测试桩的 Pass A 分流，并在桩内锁定
  `Skill(logparse-diagnose)` 恰好一次、broker 命令恰好一次且 Helper 必须在前；没有放宽生产合同。
  容器挂载修正后的中央 Dev `run-20260826T194315Z-e94108c9` 已让 framework 与 repository 全部 PASS，
  随后在 `det.affected` 以 1014 项中的唯一失败封存：受控 fixture manifest 仍记录修改前
  `fake_agent.py` 的 size/SHA-256。同步更新该 canonical 完整性条目后再以新源码身份验证，不绕过
  fixture 自校验。2026-08-27，依据局域网真实失败的 ROUTE draft，移除 `SKILL_INDEX` 中重复的短
  `registration_id`，将内部 index 升级为 v2，并把 ROUTE output contract 升级为 3.0.0，明确禁止删改
  `diagnosis-skill/` namespace。Fast E2E 同步移除业务 package 的 Agent Skill 安装和非 LOGPARSE
  `Skill` 工具，只保留 Server Catalog 注入的业务上下文与现装 `logparse-diagnose` Helper；exact-ref
  拒绝规则保持不变，没有增加短 ID 兼容或 Server 自动补前缀。新 generation
  `claude-deepseek-20260827T035724Z-a3bb328c` 为 PASS；随后完整 E2E
  `claude-deepseek-20260827T040046Z-c8931d2b` 在 Router 已正确选择完整 ref 后封存为 FAIL，原始 trace
  证明合法绝对路径 Write 被 `Write(/output/**)` 拒绝。该运行没有自动重试。修复改为按每个 Job 的
  实际绝对路径生成规则。新源码 E2E `claude-deepseek-20260827T051755Z-e2dfe5d9` 再次封存为 FAIL，
  证明 `Write(<绝对路径>/**)` 仍不是 Claude Code 的文件写入权限类别；同一 trace 中 Router 已生成正确
  namespaced ref，但两次 Write 均被拒绝。修复改用 `Edit(<绝对output>/**)` 放行实际 Write，并把
  terminal Case 检查前移到 recovery 与 Bash 审计之前。v3 E2E
  `claude-deepseek-20260827T052451Z-bb76d4d1` 随后以正确的
  `CLAUDE_DEEPSEEK_SERVICE_JOB_FAILED` 首错封存，证明错误优先级已生效，同时确认单斜杠仍被解释成
  项目相对规则；模型另成功写入默认 memory 目录。修复最终复用 generation runner 已真实验证的双斜杠
  绝对路径编码，并新增独立文件轨迹边界审计。三次失败均未自动重试。
- **专项回归测试**：
  - `tests/deterministic/unit/integrations/test_lan_logparse_meta_skill.py` 中
    `test_valid_production_registration_passes`、`test_valid_production_registration_loads_in_server`、
    缺少 slot、固定或重映射 anchor、module 漂移、非 `default` product、越权 Server 工作和路径越界的
    正反例。
  - `tests/deterministic/unit/runtime/test_diagnosis_runtime.py` 中
    `test_methods_preprocess_prompt_declares_helper_before_one_broker_request`、
    `test_methods_helper_load_failure_never_reaches_broker_or_pass_b`、
    `test_methods_preprocessing_rejects_failed_operation_before_success` 和
    `test_default_product_survives_compiler_and_workspace_manifest`。
  - `tests/deterministic/unit/integrations/test_logparse_diagnose_skill.py` 与
    `tests/deterministic/unit/runtime/test_output_reader_result_v2.py::test_methods_preprocessing_requires_one_total_successful_server_operation`。
  - Claude/DeepSeek `claude-deepseek-contract.test.mjs`、`claude-deepseek-methods-runner.test.mjs`、
    `claude-deepseek-service-wrapper.test.mjs`、`claude-deepseek-e2e-runner.test.mjs` 和
    `claude-deepseek-bash-policy.test.mjs` 中完整 registration cache、动态 slot、精确 ROUTE、
    Helper-first 唯一 broker、当前源码根、客户端 Skill 双账本、HTTP MCP、descriptor 下载与 Server v3
    ZIP 校验用例。
  - `tools/test-flow/tests/config-planner.test.mjs` 中 `insufficient-evidence` 的四阶段中央计划用例，以及
    `tools/test-flow/tests/actions.test.mjs` 中按计划逐序核验 Claude E2E invocation ledger 的正反例。
  - WSL 密封容器的 `quick-claude-e2e-contracts.tap` 直接在只读仓库挂载下执行上述测试，防止测试临时
    文件再次写入源码树。
  - `tests/deterministic/journey/test_rpc_timeout.py` 的两条 SameJob 旅程，以及其
    `tests/fixtures/rpc_timeout/fake_agent.py` Pass A 合同断言，直接覆盖新 Helper-first 首行、唯一调用和
    Helper-before-broker 顺序。
  - `claude-deepseek-contract.test.mjs` 中
    `registration runtime ref uses the Server code-point order for package paths`，直接用包含大写
    `SKILL.md` 与小写文件的 package 复现旧排序差异。
  - `claude-deepseek-methods-runner.test.mjs` 中 canonical marker 机械提取、prompt allowlist 与缩短事件名
    反例；`test_lan_logparse_meta_skill.py::test_validator_rejects_shortened_event_name_marker` 直接复现
    `API_COMPLETE` 被旧生成模型误写为 marker 的问题。
  - `claude-deepseek-e2e-runner.test.mjs` 中空 `get_case` 只允许一次紧邻、完整、30 秒纠正的正反例，
    client prompt identity，以及“附件 preflight + 实际 DIAGNOSE”双 Job 的正反例；
    `framework.test.mjs` 核验该 identity 进入 standalone plan。
  - `tests/deterministic/unit/runtime/test_diagnosis_runtime.py::test_route_skill_index_v2_exposes_only_the_complete_namespaced_ref`
    直接复现短 `registration_id` 与完整 namespaced ref 同时暴露的歧义。
  - Claude/DeepSeek `claude-deepseek-e2e-runner.test.mjs` 的 Server Agent Skill 集合正反例，以及
    `claude-deepseek-service-wrapper.test.mjs` 的非 LOGPARSE 零 Skill 工具边界、Job 绝对工作区
    双斜杠 `Read(...)`/`Edit(...)` 规则、禁止伪 `Write(...)` 权限规则、Write denial 和默认 memory
    越界反例；`claude-deepseek-e2e-runner.test.mjs`
    另以 terminal `FAILED` Case 直接锁定 `CLAUDE_DEEPSEEK_SERVICE_JOB_FAILED` 必须早于 recovery 和
    Bash policy 审计。
- **最新 Test Flow verdict**：最终 Dev `run-20260826T200500Z-f663eeb6` 为
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:4ae9db980116027e6613e7190d61f4b75c3eff00e05a8d3694acf2a97e0db412`
  （678 files）。当前快照下 affected 1013 passed/1 skipped、contracts 569/569、unit 1786
  passed/1 skipped、integration 45/45、SameJob 4/4；framework 与 repository Stage 复用前一份
  PASS receipt 并完成当前重审。standalone generation
  `claude-deepseek-20260826T183253Z-3e225612` 和完整 MCP E2E
  `claude-deepseek-20260826T190333Z-5fd045f7` 均为 PASS、`retry_count=0`，只分别证明本次真实模型
  生成和 Fast E2E；本轮未运行 Release，不能外推为 Release 结论。本 verdict 引用元数据段不宣称
  被其所引用的源码快照覆盖。该中央 verdict 早于本次 ROUTE 回归修复，不覆盖当前改动；用户明确要求
  本轮不运行中央 Test Flow 或 Release。
- **最新 Linux Fast E2E verdict**：`claude-deepseek-20260827T053547Z-2db93307` 为 `PASS`，plan SHA-256
  为 `64b9c13a04cc7cf02e402c9d15dae32cd6de43bf3cd417714f06e4afe96708df`。运行前 Windows 与 WSL
  ext4 副本独立核对的 Git 可见源码摘要为
  `git-visible-worktree-v1:70cd2cdeff2ad625ddd7a9d7df0127ab12f45fb7d2189c37af8dbf9a5659e4e0`
  （678 files）；standalone verdict 自身仍明确 `source_snapshot=false`。5/5 个 Claude Code 2.1.89 +
  `deepseek-v4-flash[1m]` 进程完成，`retry_count=0`，总计 788,345 tokens、USD 1.634013。ROUTE 仅有
  1 次成功 output Write，`denied=0`、`workspace_escape=false`，finalizer PASS；最终精确选择
  `diagnosis-skill/rpc-timeout-methods-v1@1.0.0#e55ce3…c7ada`。Server Agent 只安装
  `logparse-diagnose`，业务 Skill 未安装；LOGPARSE 恰好先加载一次 Helper、再调用一次 broker，无
  fallback/retry。客户端下载并复核的 Server v3 `result.zip` 为 3400 字节、SHA-256
  `4abbff61033adb75a2652d1d83c34bb4c097d7daac6da23b95c6b88ce50baebe`，含 `result.txt`、manifest
  和两份实际使用日志。该 standalone verdict 只证明本次 Linux Fast E2E，不替代中央 Test Flow、
  Release 或源码快照证明；本 verdict 引用元数据段不宣称被其所引用的测试字节覆盖。

## PL-FIX-041：去掉 Wiki 日志围栏的 text 标签后模板被静默丢失

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：同一 RPC 超时 Wiki 把日志块从带 `text` 标签的代码围栏改成无语言标签的代码围栏后，
  生成器的来源身份只
  收录带 `text` 标签的围栏，裸围栏中的日志模板被静默遗漏。共享症状模板因此可能不进入
  `source-log-templates.md`，生成模型再把方法卡中的非围栏判定线索误当成“通用模式”或无法区分原因。
- **受影响版本**：提交 `d13675d49c6b8e86a9ddb6f5eb209ca1c45090b8` 中的
  `wiki-to-logparse-diagnosis-skill` 来源身份与 validator。
- **根因**：日志模板提取器把 Markdown 语法着色标签当成业务语义，只识别带 `text` 标签的围栏，
  没有把裸围栏
  视为等价的纯文本日志块；元 Skill、validator 和 Fast E2E producer identity 又共同沿用了这条错误
  规则，使缺失模板在缓存身份内看似自洽。
- **不可回归行为**：source identity v2 必须同时提取 `text` 围栏和无语言标签围栏中带日志占位符的
  完整非空行，并忽略明确标为其他语言的围栏；提取规则必须由元 Skill、validator、generation cache
  identity 和语义 oracle 共用。所有来源模板必须完整写入固定的 `references/source-log-templates.md`；
  每张方法卡只拥有自己的判定 marker，公共症状不得被复制成每种原因都“独有”的 marker，也不得因
  修改围栏标签而静默消失。
- **修复历史**：2026-08-27，将来源身份升级为 v2，并统一元 Skill、validator 与 Fast E2E 的围栏
  处理和摘要绑定；同时把方法 marker oracle 收紧为精确一一对应，避免三张方法卡都复制同一组共享
  症状仍被误判为通过。最终 prompt v3 generation
  `claude-deepseek-20260826T183253Z-3e225612` 为 PASS：1/1 个模型进程、`retry_count=0`，validator
  `errors=[]`，共保留 6 条模板、3 个方法和 6 个 canonical marker；其不可变 cache producer identity
  为 `37fe5e…dc45`，随后被上述真实 E2E 精确消费。
- **专项回归测试**：
  - `tests/deterministic/unit/integrations/test_lan_logparse_meta_skill.py::test_source_identity_v2_extracts_text_and_bare_fences`
  - 同文件 `test_validator_rejects_lost_bare_fence_template`、
    `test_validator_rejects_stale_source_identity_extraction_version` 和
    `test_marker_starting_with_placeholder_ignores_trailing_suffix`
  - `tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-methods-runner.test.mjs` 中
    `source identity v2 extracts text and bare fences while ignoring other language fences` 与严格方法 marker
    集合 oracle 用例。
- **最新 Test Flow verdict**：最终 Dev `run-20260826T200500Z-f663eeb6` 为
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`；验证源码快照
  `git-visible-worktree-v1:4ae9db980116027e6613e7190d61f4b75c3eff00e05a8d3694acf2a97e0db412`
  （678 files）。affected 1013 passed/1 skipped、contracts 569/569、unit 1786 passed/1 skipped、
  integration 45/45、SameJob 4/4。真实 generation
  `claude-deepseek-20260826T183253Z-3e225612` 同为 PASS、`retry_count=0`，保留 6 条来源模板、3 张
  方法卡和 6 个 canonical marker；该 standalone verdict 不替代中央验证，也不外推为 Release。
  本 verdict 引用元数据段不宣称被其所引用的源码快照覆盖。

## PL-FIX-042：Methods 草稿把等价 JSON 格式误判为无效结果

- **状态**：代码已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：局域网真实定位已完成 Logparse 预处理和 Methods 分析，Agent 使用常见的
  `json.dumps(data, indent=2)` 写出 schema 合法的 `method-diagnosis.draft.json`，服务端却在结果
  校验阶段以 `OUTCOME_INVALID / method_draft_non_canonical` 拒绝。REVIEW 草稿使用相同读取路径，
  也存在同一问题。
- **受影响版本**：`5.0.0`，至少包括提交
  `0b2f2e667608637519fecc87cdd28b71eb943de4` 的 Methods-only 输出协议。
- **根因**：Methods output reader 虽然已使用 shared Agent JSON parser 拒绝 BOM、重复键、非有限
  数字和非法 UTF-8，却又要求 Agent 原始字节与 Canonical JSON 完全相等，把服务端存储与 hash
  规范错误地变成模型输出格式要求。配套 happy path 预先用 `canonical_json_bytes()` 构造理想输入，
  唯一使用 `indent=2` 的旅程测试反而把拒绝行为固化成正确结果。Linux Fast E2E 的 Server wrapper
  还在产品 Runtime 读取前调用 `canonicalizeMethodsDraft()` 改写 DIAGNOSE/REVIEW 草稿，使真实模型
  即使写出 pretty JSON 也被测试代码提前修正，继续掩盖线上失败。
- **不可回归行为**：DIAGNOSE 与 REVIEW 的 Agent 草稿只要是 schema 合法、无歧义的 UTF-8 JSON，
  服务端就必须在 Agent 退出后完成稳定读取和 schema 校验，再原子改写为 Canonical JSON；审计记录、
  source draft hash 和后续 finalization 只能使用规范化字节。UTF-8 BOM、重复键、NaN/Infinity、非法
  UTF-8、schema 错误及读取期间内容漂移仍必须失败，失败原始字节不得被规范化或丢失。Fast E2E
  wrapper 只能审计 Agent 原始草稿，必须保留 `harness_normalized=false`，不得解析后回写；真实链路要在
  `authored_canonical=false` 时仍由产品 Runtime 完成 DIAGNOSE、REVIEW 和结果下载。
- **修复历史**：2026-08-27，将 Methods DIAGNOSE/REVIEW 纳入 shared Agent JSON surface owner；
  output reader 先按原始文件完成冻结读取、解析、schema 与稳定性校验，只对等价格式差异执行服务端
  原子规范化，并再次核对规范化后的冻结字节。输出契约同步明确 Canonical JSON 编码由服务端负责。
  首次 Linux Fast E2E 虽为 PASS，复核 `before_sha256/after_sha256` 后确认改写来自测试 wrapper，故不将
  该结果作为产品修复证明；随后移除 Codex/Claude 共用 wrapper 的草稿改写，只保留原始字节审计。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_methods_output_pipeline.py::test_specialized_diagnosis_normalizes_pretty_methods_draft`
  - 同文件 `test_review_normalizes_pretty_methods_draft_without_sealer` 和
    `test_methods_draft_still_rejects_ambiguous_or_invalid_json`
  - `tests/deterministic/unit/integrations/test_agent_json.py::test_all_agent_json_surfaces_have_one_server_side_owner`
  - `tools/test-flow/quick-validation/codex-luna/tests/macos-codex-luna-service-wrapper.test.mjs` 中
    `server wrapper audits Methods drafts without normalizing product input`
- **最新 Test Flow verdict**：中央 `dev.default` 尚无覆盖最终交付字节的 PASS verdict。Windows 尝试
  `run-20260827T042259Z-85ea6f2d` 在 `framework.node-tests` 失败，pytest 未启动；WSL ext4 尝试
  `run-20260827T044033Z-22fe4e66` 在同一 framework gate 命中 120 秒硬上限而 `BLOCKED`，也未进入
  deterministic，且早于最后一次移除 Fast E2E wrapper 改写，均不得登记为修复已获正式 Test Flow
  验证。开发期直接专项为 21 passed，相关 runtime 扩展回归为 174 passed、6 skipped；Linux 共享
  wrapper 专项为 12/12 PASS。最终 Ubuntu 22.04 Claude Code `2.1.89` +
  `deepseek-v4-flash[1m]` Fast E2E `claude-deepseek-20260827T044857Z-52269227` 为 PASS：5/5 个模型
  进程、`retry_count=0`、833,289 total tokens、$1.626329；DIAGNOSE 与 REVIEW 均记录
  `authored_canonical=false`、`harness_normalized=false`、`normalization_owner=product-runtime`，全部 Job
  SUCCEEDED、公开 Case 为 RESOLVED，Server v3 `result.zip` 下载及双端日志校验 PASS。该 standalone
  verdict 直接证明本次 Linux Fast E2E，不替代中央 Test Flow 或 Release；本元数据段本身不宣称被
  上述运行覆盖。

  2026-08-28 的前置中央 Dev `run-20260828T110620Z-25ef1b05` 为
  `PASS_WITH_WARNINGS`，已经覆盖移除 wrapper 规范化后的产品实现；最终台账快照的 verdict 元数据
  将在本轮复验后追加。
  **最终复验元数据**：Dev `run-20260828T112351Z-3d7ee53b` 为 `PASS`；functional、operation、
  verification 均为 `PASS`，源码快照
  `git-visible-worktree-v1:b3f3ff6e28d9e1cccee712d8f617d470501aa97a53d662b49222b5a6d7d85968`
  （718 files）。本 verdict 元数据行本身不宣称被其引用的快照覆盖。

## PL-FIX-043：Methods marker 大小写语义在多阶段校验中不一致

- **状态**：已按 Evidence V2 单次扫描合同再次修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：`methods.json` 声明 `API_COMPLETE` 时，冻结日志中的等价文本 `api_complete` 无法进入
  marker scan；旧修复只改了部分比较后，预处理可以命中，但后续 receipt 或 grounding 仍可能按原始
  大小写重新匹配并拒绝同一证据。
- **受影响版本**：`5.0.0`，至少包括提交
  `0b2f2e667608637519fecc87cdd28b71eb943de4` 的 Methods grounding，以及后续仍传递完整
  `SkillLoadReceipt` 的 Evidence V1 路径。
- **根因**：scan、receipt 比较和 grounding 都把 marker 文本当作可再次验证的身份，分别实现匹配；
  大小写规则只要有一处不同，同一证据就会在后续阶段翻转结果。
- **不可回归行为**：服务端只能在生成 Evidence Graph 时扫描一次，并使用 Unicode `casefold()` 判断
  marker 是否出现；Graph 保留方法声明的原始 marker 和冻结日志原文。后续 Plan、Specialist、Reviewer
  与 Outcome 只消费稳定 ref，不得重新匹配 marker、日志行或恢复完整 receipt 比较。
- **修复历史**：2026-08-27，集中新增 `_marker_occurs()`，让全量 marker scan 与单条来源 grounding
  复用不区分大小写的匹配。2026-08-28 升级到 Evidence V2 后，删除下游 marker 重匹配：casefold
  只发生在唯一 scanner 内，完整链路从 uppercase 日志一直到 Case、MCP 和 REST Outcome。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_methods_evidence_v2.py::test_scan_casefolds_but_preserves_declared_marker_and_frozen_line`
  - 同文件 `test_plan_consumes_production_graph_refs_without_rescanning_logs`
  - `tests/deterministic/integration/test_methods_v2_runtime_journey.py::test_runtime_submission_reviewer_and_public_projection_are_one_v2_journey`
  - `tests/deterministic/integration/test_evidence_v2_source_mutations.py::test_source_overlay_mutant_is_killed_by_exact_regression_test`
- **最新 Test Flow verdict**：前置 Dev `run-20260828T110620Z-25ef1b05` 为
  `PASS_WITH_WARNINGS`，Evidence V2 Core 106/106 PASS，`model_invocations=0`；验证实现快照
  `git-visible-worktree-v1:f6db8bd7eaaacb4680b927db9b37a0d01adebc1aefb189cad8e919695d1e298e`
  （718 files）。该运行早于本次台账正文，最终台账快照的 verdict 元数据将在复验后追加。
  **最终复验元数据**：Dev `run-20260828T112351Z-3d7ee53b` 为 `PASS`，绑定源码快照
  `git-visible-worktree-v1:b3f3ff6e28d9e1cccee712d8f617d470501aa97a53d662b49222b5a6d7d85968`
  （718 files）；复用 Gate 均通过当前 re-audit。本元数据行本身不宣称被该快照覆盖。

## PL-FIX-044：Evidence 校验可跨 method 借 marker、重复比对 receipt 且失败原因不可见

- **状态**：已按 Evidence V2 / State V8 破坏性合同修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：一张方法卡可以借用另一张方法卡中存在的相同 literal marker；预处理生成的
  `SkillLoadReceipt` 在后续被全对象比较，使同一证据因大小写或排序差异被拒绝；校验失败后公共 Case
  只显示笼统错误，无法知道是 Specialist、Reviewer、资源漂移、服务端约束还是审计归档失败。
- **受影响版本**：`5.0.0` 的 Methods V7 / Evidence V1 定位链路，实施基线 `06fd82e`。
- **根因**：marker、日志行、receipt 和模型输出在多个阶段重复承载同一身份，校验器反复从文本重建
  关系；marker 只做全包级存在性检查，没有绑定当前 method。旧测试又常用手写的“可信” Evidence、
  Audit 和 Outcome，只覆盖单 method、同大小写和理想 JSON，无法复现真实用户入口。
- **不可回归行为**：每个 hit 必须绑定 `(method_id, marker_index, marker)`，marker 必须来自当前 method；
  服务端只扫描一次并生成完整 method-qualified Graph 和 Plan。模型只返回
  `evaluation_ref + verdict + reason`；Specialist 与盲评 Reviewer 各最多一次 repair。公共 Case/MCP/REST
  必须返回稳定 reason code 和 diagnostic ID；Graph/Plan 前失败使用 `failure`，已进入评估的终态使用
  `methods_result`。正向测试只能由生产代码生成 Graph、Plan、Outcome 和公开投影，负向测试必须从合法
  基线只改一个字段。
- **修复历史**：2026-08-28 硬切 Methods V2、State V8 和 Review V2，建立单次 scanner、Evidence
  Graph、Evaluation Plan、角色隔离、盲评 consensus、13 个公开 reason 与确定性 diagnostic ID；两套
  generator validator 同步检查 method 自有 marker。Core 固定 55 个生产 selector，并用 7 个
  source-overlay mutant 证明删掉关键校验、恢复下游匹配、允许第三次调用或恢复 hardlink 时测试必然失败。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_methods_evidence_v2.py::test_plan_rejects_rehashed_hit_bound_to_another_methods_marker_index`
  - `tests/deterministic/unit/runtime/test_meta_skill_source_identity.py::test_validator_rejects_marker_from_another_method_reference`
  - `tests/deterministic/unit/integrations/test_lan_logparse_meta_skill.py::test_validator_rejects_marker_from_another_method_reference`
  - `tests/deterministic/integration/test_methods_v2_terminal_submission.py::test_each_role_failure_reason_reaches_case_mcp_and_rest`
  - 同文件 `test_consensus_terminal_projection_survives_submission_mcp_and_rest` 和
    `test_each_failed_terminal_reason_reaches_case_mcp_and_rest`
  - `tests/deterministic/integration/test_methods_v2_pre_evaluation_failures.py::test_pre_evaluation_failure_reaches_case_mcp_and_rest_without_fake_graph`
  - `tests/deterministic/unit/runtime/test_methods_outcome_v2.py::test_outcome_mapping_does_not_rescan_evidence`
  - `tests/deterministic/integration/test_evidence_v2_source_mutations.py::test_source_overlay_mutant_is_killed_by_exact_regression_test`
- **最新 Test Flow verdict**：前置 Dev `run-20260828T110620Z-25ef1b05` 为
  `PASS_WITH_WARNINGS`；Core 55 selectors / 106 tests 全部 PASS，合同 601/601、unit 1893 passed/68
  skipped、integration 66/66、SameJob 3/3，模型调用与 token/cost 均为 0。该运行覆盖实现字节但早于
  本次台账正文，最终元数据将在复验后追加。
  **最终复验元数据**：Dev `run-20260828T112351Z-3d7ee53b` 为 `PASS`；functional、operation、
  verification 均为 `PASS`，绑定源码快照
  `git-visible-worktree-v1:b3f3ff6e28d9e1cccee712d8f617d470501aa97a53d662b49222b5a6d7d85968`
  （718 files）。本元数据行本身不宣称被该快照覆盖。

## PL-FIX-045：Client 在创建 Case 前自行推测并追问问题细节

- **状态**：客户端合同与确定性生产入口已修复；真实 Client model-cert 待 V2 adapter 迁移后执行。
- **症状**：用户已经给出问题描述，Client 仍在调用 `problem_locator_create_case` 前根据文本和 Skill
  自行推测“还缺哪些信息”，先连续追问一批字段；实际服务端 requirements 尚未生成，提问内容可能与
  Case 建立后的权威需求不同。
- **受影响版本**：Evidence V2 之前的 `problem-locator-client` 交互说明与旧真实旅程。
- **根因**：客户端说明把“帮助整理问题描述”和“决定服务端缺失输入”混成一步，测试只检查工具文本或
  理想调用，没有从真实 create → requirements → supplement 入口约束首个业务动作。
- **不可回归行为**：只要用户提供了可创建 Case 的问题描述，Client 的首个业务动作必须是
  `problem_locator_create_case`；建案前不得按 Wiki、Skill 或模型猜测补充项。建案后只能询问 Case 返回的
  OPEN requirements，并用服务端名称原样提交。Methods V2 终态直接展示 `methods_result`，不得等待
  `result.zip` 或自行重写证据结论。
- **修复历史**：2026-08-28 重写 Client Skill 的 intake 和终态展示规则；SameJob 旅程改为真实
  create、requirements、supplement、单次 Logparse、Specialist、Reviewer、Outcome 和 restart 链路。
- **专项回归测试**：
  - `tests/deterministic/unit/interfaces/test_client_access_skill.py::test_skill_creates_case_before_requesting_missing_details`
  - 同文件 `test_skill_presents_methods_v2_without_waiting_for_an_artifact`
  - `tests/deterministic/journey/test_rpc_timeout.py::test_rpc_timeout_methods_v2_is_one_durable_same_job_path`
- **最新 Test Flow verdict**：前置 Dev `run-20260828T110620Z-25ef1b05` 的确定性闭包为
  `PASS_WITH_WARNINGS`，SameJob 3/3 PASS，模型调用为 0。该 verdict 证明客户端合同和服务入口，不证明
  尚未迁移的真实 Client 模型行为；P1/P2 model-cert 仍被
  `EVIDENCE_V2_REAL_DIAGNOSIS_ADAPTER_UNMIGRATED` 阻断。
  **最终复验元数据**：Dev `run-20260828T112351Z-3d7ee53b` 为 `PASS`，源码快照
  `git-visible-worktree-v1:b3f3ff6e28d9e1cccee712d8f617d470501aa97a53d662b49222b5a6d7d85968`
  （718 files）；真实 Client model-cert 仍未执行。本元数据行本身不宣称被该快照覆盖。

## PL-FIX-046：Workspace hardlink 清理会改变正式附件权限并破坏后续读取

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：FILE materialization 使用 hardlink 时，Workspace 清理阶段对副本执行 chmod，实际会修改
  formal resource 的同一 inode；之后 State 或附件读取可能以权限错误失败，表面看起来像随机恢复故障。
- **受影响版本**：实施基线 `06fd82e` 的 `FormalResourceReader` FILE materialization。
- **根因**：实现把“内容不可变”等同于“共享 inode 不会产生副作用”，忽略清理和只读收口仍会修改
  inode metadata；旧测试还明确把接受 hardlink 当成正确行为。
- **不可回归行为**：所有 Workspace FILE 必须是独立副本，复制完成后校验 size/hash、设为只读并原子
  发布；Runtime 与 Logparse Workspace 必须拒绝 `st_nlink != 1` 的文件。清理 Workspace 不得改变
  formal resource 的字节、inode 或权限。
- **修复历史**：2026-08-28 删除 FILE hardlink 快路径，统一使用临时副本与原子 replace；同步把旧的
  “允许 hardlink”测试迁移为明确拒绝，并加入禁止调用 `os.link` 的非平台跳过回归和 mutation。
- **专项回归测试**：
  - `tests/deterministic/unit/storage/test_resource_files.py::test_reader_materializes_file_at_fixed_workspace_path_as_isolated_copy`
  - 同文件 `test_reader_file_materialization_never_attempts_a_hardlink` 和
    `test_reader_isolates_attachment_even_when_hardlinks_are_available`
  - `tests/deterministic/unit/runtime/test_diagnosis_runtime.py::test_materialized_file_rejects_read_only_hard_link`
  - `tests/deterministic/unit/integrations/test_logparse_workspace.py::test_bind_attachment_rejects_shared_inode_materialization`
- **最新 Test Flow verdict**：前置 Dev `run-20260828T110620Z-25ef1b05` 为
  `PASS_WITH_WARNINGS`，相关 Core 和 unit Gate 均 PASS。该运行早于本次台账正文，最终元数据将在复验后
  追加。
  **最终复验元数据**：Dev `run-20260828T112351Z-3d7ee53b` 为 `PASS`，源码快照
  `git-visible-worktree-v1:b3f3ff6e28d9e1cccee712d8f617d470501aa97a53d662b49222b5a6d7d85968`
  （718 files）；复用 Gate 当前 re-audit 为 PASS。本元数据行本身不宣称被该快照覆盖。

## PL-FIX-047：replacement 恢复会重扫日志、重置 repair，拒绝记录先落盘时又无法 replay

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：旧 epoch 的 Methods Job 被标为 `INTERRUPTED` 后，`ResumeCase` 创建新 job_id；Runtime 只按
  新 ID 读取记录，导致重新 Logparse/scan、生成新 evaluation identity 并重置 repair 次数。若 rejected
  attempt 已归档而 State checkpoint 尚未写入，validation-only replay 又直接报 `STATE_NOT_FOUND`。
- **受影响版本**：Evidence V2 初版恢复与 replay 实现。
- **根因**：replacement Job 没有沿 `replacement_for_job_id` 读取直接前驱闭包；replay 把可派生 State
  当成必备记录，没有按 PRIMARY → REPAIR 的 append-only 拒绝序列机械推进状态。
- **不可回归行为**：Specialist replacement 必须复用前驱 Graph、Plan、limitations、evaluation ID、
  rejected attempts 和 repair 额度，只把 `source_job_id` 绑定到新 Job，且不得再次 Logparse/scan；Reviewer
  replacement 必须保留 Specialist source lineage。replay 在 State 缺失时必须从 immutable Job、Graph、
  Plan、source handoff State 和拒绝序列重建，只读执行当前 parser，不写 State、不扫描、不调用模型。
- **修复历史**：2026-08-28 增加 direct-predecessor 与多跳 replacement 恢复、拒绝记录继承和一次性
  repair 续跑；replay 支持 State crash window，并接入正式只读 CLI：
  `python -m problem_locator replay-method-rejection`。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_specialist_replacement_resumes_old_repair_without_rescan`
  - 同文件 `test_reviewer_replacement_inherits_old_rejection_and_interrupted_state`、
    `test_specialist_restart_reuses_graph_and_runs_only_repair` 和
    `test_reviewer_restart_runs_only_repair_and_reads_source_state_after_model`
  - 同文件 `test_specialist_replacement_lineage_resumes_from_immediate_predecessor`
  - `tests/deterministic/unit/runtime/test_methods_replay_v2.py::test_real_store_replays_specialist_primary_rejection_without_rescanning`
  - 同文件 `test_real_store_replays_reviewer_repair_from_legal_rejection_sequence`
  - `tests/deterministic/unit/interfaces/test_replay_cli.py::test_production_cli_replays_real_rejection_without_scanner_model_or_writes`
- **最新 Test Flow verdict**：前置 Dev `run-20260828T110620Z-25ef1b05` 为
  `PASS_WITH_WARNINGS`，上述恢复、replay kernel 和正式 CLI 均进入 Core 106/106 PASS；最终台账快照的
  verdict 元数据将在复验后追加。
  **最终复验元数据**：Dev `run-20260828T112351Z-3d7ee53b` 为 `PASS`，源码快照
  `git-visible-worktree-v1:b3f3ff6e28d9e1cccee712d8f617d470501aa97a53d662b49222b5a6d7d85968`
  （718 files）；复用 Gate 当前 re-audit 为 PASS。本元数据行本身不宣称被该快照覆盖。

## PL-FIX-048：冻结源码快照中的 framework Node Gate 冷启动超过旧硬上限

- **状态**：已修复；是否验证通过以本条“最新 Test Flow verdict”为准。
- **症状**：工作树直接运行同一 Node 测试集约 15 秒，但正式 Test Flow 在无 `.git` 的冻结源码快照中
  首次命中 120 秒硬上限，产生 `BLOCKED / NODE_TEST_FAILED`；第二次把上限暂调到 180 秒后，
  `framework.node-tests` Gate 以 178.5 秒通过，但整个 run 随后因 `UV_REQUIRED` 仍为 `BLOCKED`，且
  Node Gate 只剩 1.5 秒余量。
- **受影响版本**：`framework.self-test.timeout_seconds=120` 的 Test Flow V2 配置。
- **根因**：planner 和 source-snapshot 自检在物化快照中重复计算仓库身份，Windows 冷文件缓存下明显
  慢于普通工作树；Stage 上限仍按旧测试规模设置。
- **不可回归行为**：framework Node Gate 仍必须有有限硬上限，但要覆盖当前冻结快照的实测冷启动；
  超时不得被改写为测试 PASS，TAP 不完整时仍必须 `BLOCKED`。本轮固定为 300 秒，不改变产品 Runtime、
  模型预算或其他 Stage 上限。
- **修复历史**：2026-08-28 先用权威 run
  `run-20260828T104408Z-460e3190` 复现 120 秒硬超时，再在相同 digest 的手动物化快照中复跑；根据
  `run-20260828T105520Z-f2beebdb` 的 178.5 秒实测将上限固定为 300 秒。
- **专项回归测试**：
  - `tools/test-flow/tests/config-planner.test.mjs` 中
    `Dev default selects the complete cheap deterministic closure and no model budget`
  - 正式 Test Flow 的 `framework.node-tests` Gate 必须生成完整 TAP 并为 PASS。
- **最新 Test Flow verdict**：前置 Dev `run-20260828T110620Z-25ef1b05` 在 300 秒合同下为
  `PASS_WITH_WARNINGS`，`framework.node-tests` 178.29 秒 PASS；该运行早于本次台账正文，最终元数据
  将在复验后追加。
  **最终复验元数据**：Dev `run-20260828T112351Z-3d7ee53b` 为 `PASS`，源码快照
  `git-visible-worktree-v1:b3f3ff6e28d9e1cccee712d8f617d470501aa97a53d662b49222b5a6d7d85968`
  （718 files）；framework reuse 的当前 re-audit 为 PASS。本元数据行本身不宣称被该快照覆盖。

## PL-FIX-049：Evidence V2 Fast E2E 迁移残留自证 oracle、旧 registration 输入与失效基线

- **状态**：已修复；验证结论以本条最终复验元数据为准。
- **症状**：Fast E2E 虽已切到生产 Graph/Plan 与 Specialist/Reviewer，但 Codex 的零模型正测仍从
  `case.json` 的 `expected_branch_markers`、`expected_terms` 和 evidence identity 反向配置 Fake role
  输出；两个 provider 的 `unrelated-log-noise` Gate 都有禁止噪声检查，却没有直接负例。Claude Fast
  又把历史 `client_process/server_process` 原样提交给要求
  `client_slot/client_process_name/server_slot/server_process_name` 的 production registration。Fast planner
  还会读取 Release Wiki 并允许从 Methods cache 猜 registration。迁移 checkpoint 同时留下 Graph 加参
  后的 helper 解包、source mutation anchor、合同 manifest 和 SameJob fixture manifest 漂移，使
  `deterministic.full` 无法形成可信基线。
- **受影响版本**：`5.0.0`，Evidence V2 Fast E2E checkpoint
  `2009355dfd3582cb4ce09792272cbaa63778c9ea`。
- **根因**：Fast 场景、provider planner、production Runtime driver 与零模型证明在一次大迁移中各自
  演进，没有统一冻结“历史输入只作为输入、oracle 只做事后裁决、registration 必须显式提供”的边界；
  核心 Graph 校验已经完成，但测试仍保留旧函数形状、旧 fixture identity 和依赖 marker 文本的哨兵。
- **不可回归行为**：Fast E2E 必须直接读取九个历史 `case.json` 和原始日志，但不得把任何
  `expected_*` 或 `forbidden_*` 字段送进 role 输出生成逻辑。Claude 历史 slot/process 必须映射到
  production 用户事实名。Fast 必须显式接收一份已验证 production registration，不读取 Release Wiki，
  也不从 Methods cache 推导 registration；WSL 场景容器只读挂载该目录并在模型调用前检查
  `registration-template.json`。`unrelated-log-noise` 一旦把噪声 event/hit 纳入 confirmed evidence，
  provider Gate 必须失败。核心共识继续只固定“两侧 event 集合不相交 → UNRESOLVED”，不新增“部分
  重叠 → UNRESOLVED”合同。
- **修复历史**：2026-08-30，先复核确认 formalization 三个入口和 event→hit 精确校验已经携带同一
  Graph，四个现有 noise 单测也已覆盖核心语义，因此没有重复修改核心层。随后移除 Fast Runtime 的
  Release Wiki 参数，改由实际加载 registration 的 `source_wiki_sha256` 提供场景身份；修正 Claude
  七个历史用户事实的 production 映射；两套 planner 与 WSL wrapper 改为显式 registration；Codex
  正测不再读取历史 oracle 配置 Fake role。同步修复迁移造成的 helper、mutation anchor、fixture 和
  contract manifest 漂移。
- **专项回归测试**：
  - `tools/test-flow/quick-validation/codex-luna/tests/macos-codex-luna-fast-e2e-runner.test.mjs` 中
    `unrelated-log-noise fails when confirmed evidence includes a noise event` 与
    `Fast runner has no Release fixture or Methods cache dependency`
  - `tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-fast-e2e-runner.test.mjs` 中
    `Runtime driver parameterization reads the historical case and raw logs`、
    `unrelated-log-noise fails when confirmed evidence includes a noise event` 与
    `Fast runner does not import the Core, Release oracle, or model-cert builder`
  - `tools/test-flow/quick-validation/codex-luna/tests/test_macos_codex_luna_model_cert_driver.py::test_fast_e2e_runtime_uses_production_v2_records_without_oracle_feedback`
  - 同文件 `test_fast_e2e_oracle_accepts_a_result_not_derived_from_its_expectation` 与
    `test_fast_e2e_oracle_rejects_confirmed_unrelated_noise`
  - `tests/deterministic/integration/test_methods_v2_runtime_journey.py::test_runtime_submission_reviewer_and_public_projection_are_one_v2_journey`
  - `tests/deterministic/integration/test_methods_v2_terminal_submission.py::test_each_failed_terminal_reason_reaches_case_mcp_and_rest`
  - `tests/deterministic/integration/test_evidence_v2_source_mutations.py::test_source_overlay_mutant_is_killed_by_exact_regression_test`
  - `tests/deterministic/unit/runtime/test_methods_outcome_v2.py::test_outcome_mapping_does_not_rescan_evidence`
  - `tests/deterministic/contracts/test_schema_snapshots.py::test_contract_manifest_covers_the_exact_frozen_inputs`
  - `tests/deterministic/journey/test_rpc_timeout.py::test_rpc_timeout_fixture_manifest_is_schema_valid_and_exhaustive`
- **最新 Test Flow verdict**：首次权威复现 `run-20260830T092923Z-5c87d24a` 为 `FAIL`：
  `deterministic.full` 的 Core、contracts、unit、integration、SameJob 分别直接暴露上述 11、1、1、10、1
  个失败，源码快照为
  `git-visible-worktree-v1:09e272af362aaf60bb4c9b537088492650fc38081fda43c941d7b7846fc94995`
  （745 files）。修正后的对应专项为 28/28 PASS；最终权威复验元数据将在通过后追加。
  **最终复验元数据**：Dev `run-20260830T093950Z-93fff624` 为 `PASS_WITH_WARNINGS`，仅缺性能基线；
  `deterministic.full` 为干净 `PASS`：Core 108/108、contracts 602/602、unit 1942 passed/68 skipped、
  integration 68/68、SameJob 3/3，全部 Gate 的 failure/error 为 0，模型调用为 0。Core 绑定
  `contract-manifest.json` SHA-256
  `b41d74d70b8d6e1441ea1aee384f4f50ec7753adea8b7eb1a4cb5be38048fcdb`，源码快照为
  `git-visible-worktree-v1:5c9cf39747e1d0f02e4ebced9c26eb8c08298c4d2fd463d378858ad433a47459`
  （745 files）。本元数据行本身不宣称被其引用的快照覆盖。

## PL-FIX-050：ROUTE 重复加载 Methods Skill，去重提交又留下失败测试

- **状态**：已修复；验证结论以本条最终复验元数据为准。
- **症状**：每个 ROUTE Job 会对每个可用 Methods Skill 重复读取、解析并哈希最多 5 次。
  `428d35e` 首轮去重后仍有 3 次，而且删除 `catalog.check()` 后保留了断言该调用存在的旧测试，
  导致 `test_public_asset_fake_typed_resolve_failure_preserves_details_as_outcome` 在当前主分支直接失败。
- **受影响版本**：`5.0.0`，包括 `428d35e` 至本次修复前的主分支。
- **根因**：`_validate_resolved_asset()` 已返回完整 `ResolvedSpecializedSkillV1`，但 `_resolve()`
  丢弃该对象，`_skill_index_entry()` 为构造同一 Job 的索引再次扫描目录。测试交接又错误假设
  `FakeAssetCatalog.resolve_calls` 会记录注入失败的调用；实际 Fake 在追加前先抛异常。
- **不可回归行为**：Catalog 与 Runtime 必须继续各做一次完整内容校验；同一个 ROUTE Job 构造
  `SKILL_INDEX` 时必须复用 Runtime 已验证的 Skill 快照，不得第三次加载。Skill 内容即使保持相同
  文件大小并恢复 mtime，只要字节变化仍必须以 `ASSET_VERSION_UNAVAILABLE` fail closed。typed
  `ApplicationPortError.details`、首个 ref fail-fast、State 未读取和 Backend 未启动语义保持不变。
- **修复历史**：2026-09-01，先用当前源码专项复现旧断言失败，再用运行探针确认每个 Skill 的
  Catalog/Resolver 加载数为 1+2。随后增加 Job 内 `_ResolvedSkillSnapshot`，把 Resolver 加载降为
  1 次，总数降为 2；测试改为在 Fake 委托前记录实际尝试的 ref，不改变共享 Fake 的失败顺序。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_diagnosis_runtime.py::test_public_asset_fake_typed_resolve_failure_preserves_details_as_outcome`
  - 同文件 `test_route_reuses_one_validated_skill_snapshot_for_the_index`
  - 同文件 `test_asset_content_drift_never_substitutes_the_frozen_job_version`
  - 同文件 `test_asset_content_drift_with_unchanged_size_and_mtime_is_rejected`
  - `tests/deterministic/unit/runtime/test_methods_skill.py::test_catalog_routes_registered_methods_skill_for_empty_partial_and_extra_facts`
  - `tests/deterministic/integration/test_s07_settings_catalog_runtime_seam.py`
  - `tests/deterministic/integration/test_bootstrap_composition.py`
  - `tests/deterministic/contracts/test_execution_replay_scenarios.py`
- **最新 Test Flow verdict**：Dev `run-20260901T040141Z-d2f71825` 为 `PASS_WITH_WARNINGS`，仅因
  性能基线尚未校准；`deterministic.full` 为 PASS，Core 108/108、contracts 602/602、unit
  1953 passed/68 skipped、integration 68/68、SameJob 3/3，模型调用为 0。源码快照为
  `git-visible-worktree-v1:86407911bd63718703d2ba23946e9415039d35777da0b4b0a4e00b11fcca235a`
  （749 files），verdict verification 为 PASS。本元数据行本身不宣称被其引用的快照覆盖。

## PL-FIX-051：dev.quick 要求 full 后又被同身份重试策略阻断，BLOCKED verdict 自身校验失败

- **状态**：已修复；验证结论以本条最终复验元数据为准。
- **症状**：宽范围改动下，`dev.quick` 正确生成
  `AFFECTED_SCOPE_REQUIRES_FULL / BLOCKED`，但紧接着运行 `dev.default` 会被
  `UNCHANGED_RETRY_INTENT_REQUIRED` 拦截，要求用户为同一失败重新填写 reason、hypothesis 和
  expected evidence。该 admission-blocked 计划若包含可复用 Stage，候选收据又把这些 Stage 记为
  `NOT_EXECUTED`，而审计器强制要求 `REUSED`，最终把本应可验证的 BLOCKED verdict 升级成 ERROR。
- **受影响版本**：`dev.quick` 初始实现及 Test Flow V2 admission-blocked 收据审计路径。
- **根因**：retry 策略不知道 `AFFECTED_SCOPE_REQUIRES_FULL` 是从 quick 升级到 full 的预期控制流；
  receipt audit 也没有区分“计划决定可复用”和“整个计划未获准执行”，错误地要求 admission-blocked
  candidate 实际采用复用结果。
- **不可回归行为**：quick 的宽范围结果必须继续 fail closed；只有包含 `deterministic.full` 的计划
  才能把该 code 视为已被更强证明覆盖。其他同身份失败仍要求结构化新假设。任何 admission-blocked
  计划的所有 Stage 都必须保持 `NOT_EXECUTED`，且最终 BLOCKED verdict 必须通过自身收据校验；不得
  在未获准执行时把历史复用 Stage 写成已采用的 PASS。
- **修复历史**：2026-09-01，先实跑 `dev.quick` 取得
  `run-20260901T034234Z-be9873a0` 的预期 BLOCKED，再用 `dev.default` 复现 retry blocker 和
  `CANDIDATE_PLAN_STAGE_IDENTITY_MISMATCH`。随后只在 full 闭包中把
  `AFFECTED_SCOPE_REQUIRES_FULL` 标记为已被升级覆盖，并让收据审计按 admission 状态校验 Stage
  result source。
- **专项回归测试**：
  - `tools/test-flow/tests/actions.test.mjs` 中
    `a broad affected selection fails closed when the quick plan has no full suite`
  - 同文件 `a broad affected selection is not required only when full is in the plan`
  - `tools/test-flow/tests/config-planner.test.mjs` 中
    `Dev quick selects only the affected deterministic closure`
  - 同文件 `a full deterministic plan supersedes the quick scope escalation`
  - `tools/test-flow/tests/evidence.test.mjs` 中
    `admission-blocked plans require every Stage to remain not executed`
- **最新 Test Flow verdict**：预期阻断验证 `run-20260901T035500Z-87116ef7` 为 `BLOCKED` 且
  verification PASS；最终 Dev `run-20260901T040141Z-d2f71825` 为 `PASS_WITH_WARNINGS`，
  `dev.default` 在 quick 升级后正常获准，完整确定性闭包全部 PASS。源码快照为
  `git-visible-worktree-v1:86407911bd63718703d2ba23946e9415039d35777da0b4b0a4e00b11fcca235a`
  （749 files）。本元数据行本身不宣称被其引用的快照覆盖。

## PL-FIX-052：Evidence V2 证据重复放大导致 Context 超限并误报 OUTCOME_INVALID

- **状态**：已修复；验证结论以本条最终复验元数据为准。
- **症状**：几十行长日志在 1:1 marker-hit、跨方法共享 marker 和每个 hit 携带完整原始行后，
  可形成约 254 KiB 的 Evidence Graph；旧上下文又同时内嵌完整 Graph、Plan 和重复方法卡，实际
  Specialist 输入达到约 295 KiB，超过 `specialist_context_bytes=262144`。服务端随后把
  `CONTEXT_LIMIT` 改写为 `SERVER_INVARIANT_VIOLATION / OUTCOME_INVALID`，用户看到的原因与真实容量
  问题无关。
- **受影响版本**：Problem Locator `5.0.0` Evidence V2 初版，至
  `codex/evidence-v2-reviewer-toggle-minimal@2f8f5d80`。
- **根因**：Graph 为了审计与机械校验，按 method-qualified hit 重复保存 marker 和命中行；
  ContextBuilder 却把这份服务端记录直接当模型输入，并同时保留模型可读 Graph/Plan 文件和
  `runtime/context.txt`。Scanner 还会按方法重复检查相同 casefold marker。Specialist 的
  Workspace/context 异常捕获又无差别生成 Methods 系统失败，吞掉了原始容量错误；预算只检查
  context body，没有计入最终角色指令和必读 request。
- **不可回归行为**：
  - Graph/Plan 的公开结构、ref、method-qualified hit 和服务端审计语义不变；模型只接收一次机械
    派生的紧凑 `evaluation_input`。`sources` 保留全部冻结目标，包括零命中 source；物理日志行和
    marker 字面量各存一次，evaluation、event、identity 和全部 method-qualified match 关系不得
    截断、采样、合并或丢失。
  - 同一 casefold marker 每行只做一次 substring 检查，再按原顺序展开各方法 hit；不得改变
    activation、Graph/Plan 字节或终态校验。
  - Specialist/Reviewer 角色 Workspace 的 `inputs/` 只保留 `manifest.json`、`request.json`，
    `runtime/` 只保留 `tool-state`；完整 Graph/Plan 和 `runtime/context.txt` 只能留在服务端
    execution records。用户事实只从 request 读取，不再复制进 prompt。
  - `context_bytes` 必须在模型调用前覆盖 context body、必读 request 和最长 repair 角色后缀。
    紧凑后仍超限时原样发布 `CONTEXT_LIMIT` 及 observed/limit，模型不得启动，`methods_result`、
    Methods reason code 和 diagnostic ID 必须缺省；不能改写为 `OUTCOME_INVALID`。真正的审计归档
    失败仍保持 `AUDIT_ARCHIVE_FAILED`。
  - 默认产品、P1/P2 和 `release.full` 仍只运行 Specialist；Reviewer 继续只在显式开启或独立盲评
    认证时运行。本修复不增加分批、截断、采样、配置跨重启冻结或一致性防护。
  - 旧 Methods package 不得继续要求模型读取 Graph/Plan 文件；部署前必须用当前元 Skill 从原 Wiki
    重新生成并校验 package，不能由 wrapper 删除或修补旧输入后伪造通过。
- **修复历史**：2026-09-03，从干净基线创建独立工作树；增加无损紧凑投影与 source catalog，
  删除模型 Workspace 中的 Graph/Plan、prompt 和用户事实重复通道，合并共享 marker 匹配，按最终
  模型输入统一计费并仅让 `CONTEXT_LIMIT` 穿透原有 Specialist 终态映射；同步两套 Wiki 元 Skill、
  内置 profile/output/context policy、P1/P2 wrapper、文档和固定认证选择器。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_methods_workspace_context_v2.py::test_specialist_context_compacts_shared_marker_capacity_without_truncation`
  - 同文件 `test_role_workspaces_hide_graph_plan_and_publish_compact_context_once`
  - `tests/deterministic/unit/runtime/test_methods_evaluation_input_v2.py::test_shared_marker_lines_are_catalogued_once_without_losing_relations`
  - 同文件 `test_large_shared_marker_graph_projects_below_the_byte_boundary` 和
    `test_source_catalog_preserves_scanned_source_without_matching_lines`
  - `tests/deterministic/unit/runtime/test_methods_evidence_v2.py::test_shared_casefold_literal_is_matched_once_per_line_then_expanded`
  - `tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_specialist_context_limit_preserves_classified_failure_without_terminal_projection`
  - 同文件 `test_specialist_final_role_prompt_is_included_in_context_byte_limit`、
    `test_reviewer_context_limit_preserves_classified_failure_without_terminal_projection`、
    `test_reviewer_final_role_prompt_is_included_in_context_byte_limit` 和
    `test_specialist_context_audit_failure_remains_audit_terminal`
  - `.agents/skills/wiki-to-diagnosis-skill/scripts/validate_generated_skill.py` 与
    `.claude/skills/wiki-to-logparse-diagnosis-skill/scripts/validate_generated_skill.py` 的旧输入负例
  - `tools/test-flow/quick-validation/claude-deepseek/tests/claude-deepseek-service-wrapper.test.mjs`
  - `tools/test-flow/quick-validation/codex-luna/tests/macos-codex-luna-model-cert-wrapper.test.mjs`
- **最新 Test Flow verdict**：Dev `run-20260903T031108Z-ce0c6063` 为 `PASS_WITH_WARNINGS`，仅因
  性能基线尚未校准；functional、operation、verification 均为 `PASS`，模型调用、token 和费用均为
  0。`deterministic.full` 为 PASS：Evidence V2 Core 116/116、contracts 602/602、unit
  1982 passed/68 skipped、integration 69/69、SameJob 3/3。验证源码快照
  `git-visible-worktree-v1:f2e9fcc3fa07caaf0654fb5b5103040b9081ba72bc4beed7dd8254dfd1c96ed7`
  （752 files），worktree/materialized source verification 均为 PASS。本元数据行本身不宣称被其引用的
  源码快照覆盖。

## PL-FIX-053：ROUTE 大合同、统一模型命令与附件串行等待放大端到端耗时

- **状态**：代码优化已完成；验证结论以本条最终复验元数据为准。
- **症状**：实际环境一次完整定位耗时 12 分 10 秒，其中 ROUTE 的 `BACKEND_EXECUTE` 为
  2 分 34 秒，等待用户材料和状态变化为 4 分 45 秒，第二轮正式 DIAGNOSE 的
  `BACKEND_EXECUTE` 为 4 分 23 秒；三段合计占总耗时 96.2%，其余服务端处理只有秒或毫秒级。
  代表性 ROUTE 输入为 38,413 字节，其中共享 `AgentJobOutcomeDraftV2` schema 为 33,213
  字节，模型还必须在写完草稿后调用一次封装工具。客户端即使已经拿到本地日志，也会等到
  `WAITING_ATTACHMENT` 后才开始上传，并把事实和附件拆成串行补充。
  继续拆解当前源码后还确认：七个 MCP `outputSchema` 合计 173,697 字节，每次初始化重复发送
  四份相同的 28,179 字节 `ApplicationResponse` 深层 schema；fresh Specialist 又把同一日志附件
  先复制到主 Workspace、再复制到 `.logparse-preprocess`，随后还把已验证 target logs 写入主
  Workspace，构图后立即删除。以上都是可在线性放大附件 I/O 或客户端启动负担的重复工作。
- **受影响版本**：Problem Locator `5.0.0`，本轮基线
  `5723e96cf537f3aaf33bb36c0418a3b5d0d98746`。
- **根因**：ROUTE 只需要输出 `MATCHED` 或 `NO_CAPABILITY`，却内嵌了 DIAGNOSE、REVIEW 等
  全角色共享 schema，并把 Canonical JSON 封装交给模型工具回合。所有角色又只能使用同一个
  `CLAUDE_COMMAND`，部署者无法让低复杂度 ROUTE 与高复杂度 DIAGNOSE 采用不同延迟配置。
  附件 prepare/PUT 本来允许在非终态 Case 上执行，PUT 回执也已经权威确认 READY，但客户端流程
  没有把这段文件 I/O 与 ROUTE 重叠，也没有优先合并同批事实和附件。
  MCP 适配器把服务端已经由 Pydantic DTO 约束的完整数据结构再次展开成七份深层输出 schema，
  官方 SDK 又为同一返回值生成缩进 JSON 文本和 `structuredContent`。Evidence V2 则沿用通用
  Workspace 的“先物化全部资源”入口，没有区分只供服务端预处理的 payload 与模型最终可见输入；
  `freeze_methods_inputs()` 也保留了旧版中间落盘流程，尽管 Graph 只读取其返回的内存字节。
- **不可回归行为**：
  - ROUTE 模型上下文只保留角色专用的十二字段、两个合法分支和完整 Skill ref 规则；服务端仍按
    完整 `AgentJobOutcomeDraftV2`、Job/Case 绑定、秘密扫描、稳定快照和 Workspace 边界复验。
    Router 必须继续看到全部有效 production Skill 并做语义选择；唯一候选也可能返回
    `NO_CAPABILITY`，不得改成按候选数自动命中或重新引入 user-fact-name 过滤。
  - Router 工具集不再暴露 `problem-locator-seal-outcome-draft`。Agent 进程树退出后，Runtime
    在 `OUTCOME_VALIDATE` 内直接调用同一产品封装函数，再由原 output reader 校验 canonical draft
    与 marker。旧 adapter 已产生 marker 时保持兼容并原样复验。服务端 draft/marker 写失败必须保留
    retryable Workspace 故障分类；封装新增字节必须再次计入固定 Workspace 上限。
  - `ROUTE_CLAUDE_COMMAND` 与 `DIAGNOSE_CLAUDE_COMMAND` 可独立覆盖默认命令；任一未配置时必须
    精确回退 `CLAUDE_COMMAND`。SPECIALIZED、GENERIC、Logparse 预处理和可选 Reviewer 使用
    DIAGNOSE 命令，Reviewer 与 Specialist 仍保持同一模型身份。
  - 用户在创建 Case 时已经选择本地附件，客户端可以在 ROUTE 期间 prepare/PUT；PUT 成功回执
    直接作为 READY 依据，不增加确认轮询。只有最新 Case 已出现匹配的 OPEN requirement 才能提交，
    同批 INPUT 与全部 READY 附件应合并为一次 supplement；revision conflict 仍按同一逻辑请求 ID
    刷新后重试。
  - 不放宽 Agent Workspace 的 50ms 安全扫描、Methods `request.json` 单一用户事实来源、Evidence
    校验、状态恢复和唯一 repair。它们不是本次分钟级耗时的已证实来源；不得用未经 A/B 的删减换取
    不可审计的表面提速。
  - 七个公开 MCP 输入 schema 继续保持扁平，实际 success/error 数据、内部 DTO 和 REST OpenAPI
    不变；精简后的输出 schema 仍严格校验顶层 `ok/data/error` 及互斥分支，官方 SDK 必须拒绝非法
    envelope。文本结果必须是同一 `structuredContent` 的完整 canonical UTF-8 JSON，不能只返回摘要。
  - 只有没有可恢复 Graph/Plan 的 fresh SPECIALIZED DIAGNOSE 可使用 metadata-only 主 Workspace；
    `.logparse-preprocess` 仍须完整物化并校验固定附件/Artifact，资源同大小篡改必须在 Backend 启动前
    fail closed。恢复路径仍完整物化，模型启动前 `inputs/` 必须精确收敛为 `manifest.json` 和
    `request.json`，symlink、reparse、跨设备节点和 hardlink 均不得越过该边界。
  - target logs、预处理 request 和 receipt 可以只在服务进程内冻结，但完整 canonical 字节、哈希及
    execution-record 审计必须保持不变。Agent Workspace 的 50ms 扫描频率和 Logparse 每个子进程前
    的资产指纹校验均保持原样；只新增扫描次数/累计耗时和预处理 Workspace 物化耗时 DFX。
- **修复历史**：2026-09-03，先按实际 Journey 拆分 730 秒墙钟并测量 ROUTE 输入，确认三段主导
  96.2% 耗时。将 ROUTE output contract 从共享 schema 改为 2,811 字节的角色专用合同，代表性
  完整上下文从 38,413 字节降至 4,318 字节，减少 88.76%；Router tool bundle 改为空，封装迁到
  Agent 退出后的服务进程内执行。新增两个向后兼容的角色命令配置。客户端 Skill 改为已有附件
  立即预上传、等待 requirement 后一次合并提交。审查期间撤回了把 Workspace 递归扫描从 50ms
  降到 1 秒的方案，也没有把 Specialist request 复制进 prompt；前者会扩大瞬时越界节点窗口，
  后者会破坏 PL-FIX-052 的事实来源和大请求完整读取合同。
  同日继续按当前源码做零模型拆解：MCP 输出 schema 总量降至 2,590 字节（减少 98.51%），并把 SDK
  自动生成的缩进/ASCII 转义文本改成与 `structuredContent` 等值的 canonical UTF-8 JSON；代表性
  `ApplicationResponse` 文本由 1,579 字节降至 1,165 字节（减少 26.22%）。fresh Specialist 主
  Workspace 改为只冻结资源 DTO/manifest 元数据，正式附件仅在预处理 Workspace 物化一次；已验证
  target logs、request 和 receipt 直接以内存字节构图并归档，不再落入随后删除的模型 Workspace。
  20 次小型本地零模型基准中，预处理均值由 30.480 ms 降至 24.953 ms（减少 18.13%），完整服务端
  流程由 66.988 ms 降至 61.389 ms（减少 8.36%）；该结果只证明本地框架/I/O 改进，不外推真实
  模型墙钟。审查期间再次撤回了 1 秒 Workspace 扫描和 operation 结束后才校验 Logparse 指纹的
  方案：前者扩大瞬时非法节点窗口，后者可能先执行漂移代码再失败。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_context_builder.py::test_production_output_contract_materializes_the_role_specific_protocol[ROUTE]`
  - `tests/deterministic/unit/runtime/test_p0_semantic_assets.py::test_router_writes_one_server_finalized_draft_without_a_tool_round_trip`
  - `tests/deterministic/unit/runtime/test_diagnosis_runtime.py::test_runtime_executes_one_frozen_route_and_publishes_canonical_receipt`
  - 同文件 `test_route_in_process_seal_write_failure_is_retryable_workspace_failure`、
    `test_post_seal_workspace_limit_stays_in_outcome_validation_stage` 和
    `test_router_semantic_no_match_publishes_no_capability_after_backend`
  - `tests/deterministic/integration/test_bootstrap_composition.py::test_production_composition_routes_each_job_role_to_its_agent_backend`
  - `tests/deterministic/unit/interfaces/test_settings.py::test_role_agent_commands_override_the_legacy_fallback_independently`
  - 同文件 `test_legacy_agent_command_remains_the_role_fallback_when_overrides_are_omitted`
  - `tests/deterministic/journey/test_rpc_timeout.py::test_attachment_preupload_during_route_batches_first_supplement`
  - `tests/deterministic/unit/interfaces/test_client_access_skill.py::test_skill_batches_inputs_and_ready_attachments_without_ready_poll`
  - `tests/real/agent/test_real_route_agent_contract_gate.py::test_real_route_agent_synthesizes_valid_outcome_from_production_contract`
  - `tests/deterministic/unit/interfaces/test_mcp_server.py::test_official_sdk_calls_all_seven_stateless_tools`
  - 同文件 `test_official_sdk_marks_invalid_top_level_output_envelope_as_error`
  - `tests/deterministic/unit/runtime/test_diagnosis_runtime_methods_v2.py::test_fresh_specialist_materializes_attachment_only_in_preprocessing_workspace`
  - 同文件 `test_fresh_specialist_resource_drift_fails_in_preprocessing_workspace`
  - `tests/deterministic/unit/runtime/test_methods_output_pipeline.py::test_workspace_freezes_methods_audit_bytes_in_memory_without_workspace_io`
  - `tests/deterministic/unit/runtime/test_methods_workspace_context_v2.py::test_fresh_specialist_main_workspace_freezes_metadata_without_payloads`
  - 同文件 `test_specialist_publish_rejects_unsafe_lexical_input_without_touching_external`
  - `tests/deterministic/unit/runtime/test_agent_backend.py::test_success_logs_bounded_agent_completion_metrics`
- **最新 Test Flow verdict**：待本轮官方 `dev.default` 复验；零模型 Dev 只能证明结构与行为回归，
  不证明真实环境墙钟降幅。12 分 10 秒场景必须在相同模型、网络和日志附件下重新 A/B 测量。
  **最终复验元数据**：Dev `run-20260903T090251Z-66b9e1e4` 为 `PASS_WITH_WARNINGS`，仅因性能基线
  尚未校准；functional、operation、verification 均为 `PASS`，模型调用、token 和费用均为 0。
  `deterministic.full` 为 PASS：Evidence V2 Core 116/116、contracts 602/602、unit 1991 passed/68
  skipped、integration 70/70、SameJob 4/4，全部 failure/error 为 0。验证源码快照
  `git-visible-worktree-v1:3444c00ab0564c849dfbb2386be5ef7b24ee3416fbc92984619692cb425f4e60`
  （752 files），worktree 与 materialized source verification 均为 PASS。本元数据行本身不宣称被其
  引用的源码快照覆盖。

## PL-FIX-054：专有定位终态只返回审计引用，缺少面向用户的具体报告

- **状态**：修复完成；验证结论以本条“最新 Test Flow verdict”为准。
- **症状**：当前专有定位被生产短路导向 Methods V2，Case 到达终态后
  `final_result=null`、`artifacts=[]`，客户端只能读取 `methods_result` 中的内部评估引用，无法向
  用户给出具体根因、分析依据、完成条件、证据、限制和处置建议，也无法下载原始目标日志包。
- **受影响版本**：Problem Locator `5.0.0` 的 Evidence V2 生产链路，至少包括本轮修改前的当前
  工作区；最后实际执行旧报告链路的参考提交为 `b7cbac5`。
- **根因**：`DiagnosisRuntime._execute()` 对专有 DIAGNOSE 直接进入 `_execute_methods_v2`，绕过仍
  存在的 `MethodDiagnosisDraftV1`、服务端 grounding、Candidate、`build_server_result_bundle()` 和
  `build_result_archive()`。随后客户端 Skill 和 Test Flow 又把“专有终态零 Artifact”写成正向断言，
  使缺少用户报告变成了被测试锁定的行为。
- **不可回归行为**：
  - V9 专有 DIAGNOSE 必须执行 `Candidate → 可选 Review → USER_RESULT`。服务端重新核对方法、
    marker、日志来源、行号、原文和哈希；Agent 不得创建 Candidate、权威 Outcome、JSON 或 ZIP。
  - `review_policy` 在 Job 创建时冻结。`NONE` 直接接受通过核验的 COMPLETE/PARTIAL Candidate；
    `INDEPENDENT` 必须在 `REVIEWING` 阶段隐藏产物，只在 PASS 后同时公开。
  - 已解决 Case 必须且只能公开一个 `diagnosis-result.json` 和一个 `result.zip`；JSON 使用
    `problem-locator-diagnosis-v3`，ZIP 包含九节中文 `result.txt`、manifest 和按权威 plan 排序的
    全部可交付目标日志。非 PASS 只能公开新的 INCONCLUSIVE JSON 与审计包，不得泄露原 Candidate
    的 JSON/ZIP。
  - 发布、Artifact 正式化与 Case 终态保持原子可见；同一 finalized Outcome 重放必须复用相同
    Artifact ID、大小和 SHA-256。生成或发布失败不能提交 `RESOLVED`。
  - 客户端必须自动下载、校验并按固定中文结构展示 JSON；`result.zip` 和审计包只在用户要求时
    下载，ZIP 下载前必须提示包含原始目标日志。`methods_result` 在 V9 专有 Case 中始终为空，不能
    作为结果来源。
  - Problem Locator 版本为 `6.0.0`，State/Job/Outcome 使用 V9 / `v9-contract-r1`。只接受全新空
    `DATA_ROOT`；V1–V8 数据只读保留且不得迁移。七个 MCP 工具名、REST 路径、附件协议和 MCP
    根层扁平输入保持不变。
- **修复历史**：2026-09-04，以 `b7cbac5` 的实际报告行为为参考，移除专有 DIAGNOSE 的 Methods
  V2 生产短路，恢复 Methods V1 草稿、冻结目标日志、服务端 grounding、Candidate、可选独立审核和
  V3 报告/归档生成；新增冻结审核策略与新配置开关，硬切 V9；同步客户端 Skill、两套 Wiki 生成
  Skill、内置 profile/output contract、OpenAPI、浏览器指南、Release CrossJob 和真实 Chrome
  list/download/restart 校验。旧 Evidence V2 package 由加载器明确拒绝，需从原 Wiki 重新生成。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_diagnosis_runtime.py::test_methods_v1_specialist_publishes_candidate_json_and_log_archive`
  - `tests/deterministic/journey/test_rpc_timeout.py::test_r01_r14_rpc_timeout_is_one_durable_cross_module_path`
  - 同文件 `test_same_job_uses_initial_order_fact_and_survives_restart`
  - `tests/deterministic/unit/domain/test_coordinator_diagnosis.py::test_specialized_candidate_is_accepted_and_published_when_review_is_disabled`
  - `tests/deterministic/unit/application/test_outcome_submission.py::test_candidate_outcome_formalizes_user_result_and_creates_review_job`
  - 同文件 `test_candidate_outcome_without_review_atomically_publishes_json_and_zip`、
    `test_candidate_result_retry_adopts_internal_first_file_before_state_commit` 和
    `test_finalized_candidate_replay_adopts_consumed_file_and_directory`
  - `tests/deterministic/unit/integrations/test_result_archive.py::test_result_archive_v3_is_deterministic_and_uses_plan_order`
  - 同文件 `test_result_text_uses_the_locked_nine_chinese_sections` 和
    `test_inconclusive_result_never_builds_result_zip`
  - `tests/deterministic/contracts/test_user_result_v2.py::test_completed_candidate_server_final_requires_json_and_archive`
  - 同文件 `test_inconclusive_server_final_requires_json_and_forbids_archive` 和
    `test_non_pass_review_carries_json_but_pass_carries_no_new_result`
  - `tests/deterministic/unit/interfaces/test_client_access_skill.py::test_skill_downloads_and_presents_the_specialized_user_report`
  - `tests/deterministic/unit/runtime/test_methods_skill.py::test_production_loader_rejects_an_old_evidence_v2_package`
  - `tests/deterministic/unit/storage/test_state_repository.py::test_v1_through_v8_state_is_read_only_and_unsupported`
  - `tests/deterministic/contracts/test_mcp_input_schema_flatness.py::test_all_public_mcp_inputs_are_flat_without_exceptions`
- **最新 Test Flow verdict**：Dev `run-20260904T054739Z-a7fd98c7` 为 `PASS_WITH_WARNINGS`，仅因
  performance 为 `NOT_CALIBRATED`；functional、operation、verification 均为 `PASS`，模型调用、
  token 和费用均为 0。`deterministic.full` 为 PASS：Methods V1 Core 30/30、contracts 576/576、
  unit 1889 passed/68 skipped、integration 41/41、SameJob 4/4，全部 failure/error 为 0。验证源码
  快照 `git-visible-worktree-v1:df2e4fb9a270a8b28c4c44be561ac3f393c641ac2990980019264faa848cd659`
  （753 files），worktree 与 materialized source verification 均为 PASS。本元数据行本身不宣称被其
  引用的源码快照覆盖；`release.full --plan-only` 的环境审查结果见本次交付说明。
  **本轮极限优化复验元数据**：Dev `run-20260903T135533Z-5c688ba5` 为
  `PASS_WITH_WARNINGS`；functional、operation、verification 均为 `PASS`，performance 为
  `NOT_CALIBRATED`（性能基线样本不足，无失败）。affected 906/906、Evidence V2 Core 116/116、
  contracts 602/602、unit 2000 passed/69 skipped、integration 70/70、SameJob 4/4，全部
  failure/error 为 0。验证源码快照
  `git-visible-worktree-v1:5f72aab29bc22eaac1d56f67ae66825f6b9b302507265919c3d83095da1b2e44`
  （752 files），worktree 与 materialized source verification 均为 PASS。本元数据行本身不宣称被其
  引用的源码快照覆盖。
