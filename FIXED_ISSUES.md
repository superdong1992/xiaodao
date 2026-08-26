# 已修复问题台账

更新时间：2026-08-25

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

- **状态**：代码修复完成；最终权威验证仍在进行，是否验证通过只以本条“最新 Test Flow verdict”
  为准，当前不得宣称已验证通过。
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
