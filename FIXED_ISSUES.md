# 已修复问题台账

更新时间：2026-08-18

本文件记录已经在当前工作区验证、修复并由专项回归测试保护的问题。活跃待办仍只写入
[`TODO.md`](TODO.md)；同一问题再次回归时更新原条目，不另建一个缺少历史关联的条目。

## 登记格式

每条记录必须包含：问题 ID、状态、症状、受影响版本、根因、不可回归行为、修复历史、
专项回归测试和最新 Test Flow verdict。只有能直接复现该问题的测试才算专项回归测试；
全量测试通过本身不能替代专项用例。

## PL-FIX-001：不兼容初始事实导致重复路由

- **状态**：实现已合入，待当前 B+C 合并快照的 fresh Release 验证。
- **症状**：Case 携带专用 Skill 未声明的初始 USER_FACT 时，ROUTE 仍可能选择该 Skill，
  随后的 DIAGNOSE 无法消费该事实并再次路由，形成无语义进展的轮询。
- **受影响版本**：`fddd170` 之前的实现。
- **根因**：候选 Skill 没有按全部冻结 `input_name` 做严格身份过滤，同时空 `REROUTE`
  缺少确定性拒绝。
- **不可回归行为**：ROUTE 只暴露声明了全部初始 INPUT 名称的 Skill；若候选为空，Runtime
  在调用路由 Agent 前发布 `NO_CAPABILITY`；无语义进展的 `REROUTE` 必须被拒绝。
- **修复历史**：`fddd170 fix: terminate incompatible fact routing`；`6be7ce6` 完成集成合同与
  测试收敛。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_catalog.py::test_route_candidates_require_exact_declared_input_fact_names`
  - `tests/deterministic/unit/runtime/test_diagnosis_runtime.py::test_empty_route_candidate_set_publishes_no_capability_without_backend`
  - `tests/deterministic/unit/domain/test_coordinator_diagnosis.py::test_zero_actionable_requirement_reroute_without_progress_is_rejected`
- **最新 Test Flow verdict**：`run-20260817T045219Z-23643cf8`，`PASS_WITH_WARNINGS`；
  functional、operation、verification 均为 `PASS`，performance 为 `NOT_CALIBRATED`；
  验证源码快照 `git-visible-worktree-v1:4717a59b4d3ad007d6bdc5659ca10db2d0c3321d2de1dd45cccb226b0cf40015`
  （581 files）。

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

- **状态**：已修复；本次为回归后的再次修复。
- **症状**：同一 DIAGNOSE 轮次同时缺少 INPUT 和 ATTACHMENT 时，Agent 按输出合同生成
  `NEED_INPUT`、`requested_input` 和 `requested_attachments`，Server Verifier 却只激活
  INPUT，最终把合法 Outcome 归一化为 `OUTCOME_INVALID`。
- **受影响版本**：最初缺陷存在于 `4e9d381` 之前；`be61e9a` 的 requirement activation
  重构再次引入该问题，当前 `main` `be61e9a` 可通过代码路径确认。
- **根因**：`resolve_requirements()` 在存在缺失 INITIAL INPUT 时丢弃了同时缺失的 INITIAL
  ATTACHMENT，而输出合同、Pydantic 合同、Finalizer 和 Coordinator 仍允许混合等待；原
  Server Verifier 回归测试也在同一重构中被改成了仅 INPUT 场景。
- **不可回归行为**：混合等待使用 `NEED_INPUT`；`requested_input` 依 Skill 顺序包含全部缺失
  INITIAL INPUT，`requested_attachments` 随后包含缺失 INITIAL ATTACHMENT。Case 先进入
  `WAITING_INPUT`，参数补齐后直接进入 `WAITING_ATTACHMENT`，期间不创建中间 DIAGNOSE Job。
  只有没有待补 INPUT 时才使用 `NEED_ATTACHMENT`。
- **修复历史**：`4e9d381 fix: accept mixed input and attachment waits` 首次修复；`be61e9a`
  回归；2026-08-17 当前变更恢复完整激活集合与端到端旅程。
- **专项回归测试**：
  - `tests/deterministic/unit/runtime/test_requirement_activation.py::test_required_role_requests_missing_inputs_and_attachment_but_never_pid`
  - `tests/deterministic/unit/runtime/test_mixed_requirement_requests.py::test_server_verifier_accepts_missing_inputs_and_attachment_after_partial_input`
  - `tests/deterministic/unit/domain/test_coordinator_diagnosis.py::test_need_input_accepts_multiple_inputs_and_attachment_in_one_wait`
  - `tests/deterministic/unit/domain/test_coordinator_supplement.py::test_input_completion_moves_directly_to_waiting_attachment`
  - `tests/deterministic/journey/test_rpc_timeout.py::test_r01_r14_rpc_timeout_is_one_durable_cross_module_path`
- **最新 Test Flow verdict**：`run-20260817T045219Z-23643cf8`，`PASS_WITH_WARNINGS`；
  functional、operation、verification 均为 `PASS`，performance 为 `NOT_CALIBRATED`；
  验证源码快照 `git-visible-worktree-v1:4717a59b4d3ad007d6bdc5659ca10db2d0c3321d2de1dd45cccb226b0cf40015`
  （581 files）。

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
