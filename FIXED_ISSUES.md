# 已修复问题台账

更新时间：2026-08-18

本文件记录已经在当前工作区验证、修复并由专项回归测试保护的问题。活跃待办仍只写入
[`TODO.md`](TODO.md)；同一问题再次回归时更新原条目，不另建一个缺少历史关联的条目。

## 登记格式

每条记录必须包含：问题 ID、状态、症状、受影响版本、根因、不可回归行为、修复历史、
专项回归测试和最新 Test Flow verdict。只有能直接复现该问题的测试才算专项回归测试；
全量测试通过本身不能替代专项用例。

## PL-FIX-001：不兼容初始事实导致重复路由

- **状态**：已修复。
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

- **状态**：已修复。
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

- **状态**：已修复。
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

- **状态**：实现已完成；仅当本条“最新 Test Flow verdict”引用最终 A+B+C 合并源码快照的
  fresh Release 可交付结论后，才视为已验证。
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
  最终 V4/V6 合同必须包含 `GenericResultV2`、`GENERIC_REPORT` 及其精确字段/枚举约束。OpenAPI
  固定 operation ID、参数约束、含义、示例和响应头；现有业务 URL、请求、响应和状态码不变。
- **修复历史**：2026-08-18 新增 `docs/browser-rest-api.md`，把 README REST 区域缩为单一入口
  并恢复后续同级标题；补全 REST-only OpenAPI 元数据和完整 canonical 快照；增加框架无关
  TypeScript/`fetch` 示例、状态与错误动作表，以及指南/OpenAPI/Test Flow 身份防漂移检查。
  A+B+C 集成时保留完整静态合同形态，并从最终 V4/V6 REST 模型重新生成快照，使 Generic V2
  Case 结果和公开 Markdown 产物进入同一运行时/静态合同/指南闭包；未沿用 C 的摘要快照或 B 的
  V3 专属生成字节。
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
- **最新 Test Flow verdict**：待最终 A+B+C 合并源码快照的 fresh Release；尚未验证，任何独立 Dev verdict 均不可替代。

## PL-FIX-013：条件 selector 与不完整观测被误判为可确定 RPC 结论

- **状态**：实现已完成；仅当本条“最新 Test Flow verdict”引用最终 A+B+C 合并源码快照的
  fresh Release 可交付结论后，才视为已验证。
- **症状**：Diagnosis Skill 的 event selector 可以引用未声明或 `OPTIONAL` 的 USER_FACT；依赖
  rule 结果激活的条件 selector 只检查自身直接循环，无法拒绝跨条件或多跳依赖。服务端又把
  selector 缺失、条件未激活、anchor/locator 不完整、日志有损策略和完整扫描真零匹配压缩成
  同一布尔扫描状态，导致不适用参数可能被请求，或把下界观测错误升格为确定 FAIL。RPC Release
  case 还无法稳定证明 `request_id` 只在 `transport_protocol=standard` 时激活，以及目标在完整/
  部分区间和非 API lane 占用下应得到的精确 COMPLETE/PARTIAL 结论。
- **受影响版本**：Problem Locator 3.0.0，基线
  `f99a3d5f2cef54fd86ef43030311ab5c42e377d4`。
- **根因**：verification contract 没有把 selector USER_FACT 与 INPUT requiredness 建立闭包；
  requirement activation 只检测当前目标自身是否出现在 rule 事实闭包中；Server Verifier 采用
  单阶段 selector/rule 求值和无原因的 `event_scan_complete` 布尔值，量词聚合也没有区分完整
  集合与观测下界。Release supplement coverage 同时把三值激活条件近似为静态必填集合。
- **不可回归行为**：USER_FACT selector 只能引用已声明的 `REQUIRED`/`CONDITIONAL` INPUT，
  未声明或 `OPTIONAL` 引用在 Catalog 加载时失败；任何 selector 所依赖的条件事实都不得反向
  出现在其激活 rule 的展开闭包中。服务端先求机械 rule，再用 PASS/FAIL/UNKNOWN 重算条件
  selector：缺失为 `UNVERIFIABLE/SELECTOR_MISSING`，条件不成立为
  `NOT_APPLICABLE/SELECTOR_CONDITION_INACTIVE`，只有完整有界扫描的零匹配可成为确定 FAIL；
  partial/unbounded/unbound/no-anchor/lossy 扫描保持下界，正向事实仍可证明但不能用缺失证明否定。
  RPC `request_id` 只在 standard transport 分支请求一次，未激活时不得补交；Release case 必须
  覆盖目标位置、完整/部分区间和非 API lane。Generic runtime 不得继承这些 RPC business
  canary。此修复不宣称已实现 observation-policy 各参数的细粒度 evaluator，该事项继续留在
  `TODO.md`。
- **修复历史**：2026-08-18 A 变更在 `verification_contract.py`、
  `requirement_activation.py` 和 `server_verifier.py` 中加入 selector 输入约束、跨条件闭包拒绝、
  带原因的扫描状态、下界安全量词及两阶段激活重算；同步扩展 RPC Release fixture/oracle 和
  三值 supplement coverage。A+B+C 集成保留 C Generic V2 独立输入/输出边界和 B 的受控
  Windows Test Flow 路径，未把 RPC 常量或 selector 语义注入通用定位流程。
- **专项回归测试**：
  - `tests/deterministic/unit/integrations/test_generator_v3.py::test_selector_user_fact_requires_a_declared_non_optional_input`
  - `tests/deterministic/unit/integrations/test_generator_v3.py::test_selector_user_fact_accepts_a_conditional_input`
  - `tests/deterministic/unit/runtime/test_catalog.py::test_skill_catalog_rejects_unsafe_selector_user_facts`
  - `tests/deterministic/unit/runtime/test_requirement_activation.py::test_rule_activation_rejects_cross_conditional_selector_cycle`
  - `tests/deterministic/unit/runtime/test_requirement_activation.py::test_rule_activation_rejects_conditional_selector_in_dependency_closure`
  - `tests/deterministic/unit/runtime/test_requirement_activation.py::test_rule_activation_allows_required_selector_in_dependency_closure`
  - `tests/deterministic/unit/runtime/test_server_verifier_v2.py::test_incomplete_observability_keeps_positive_event_presence_verified`
  - `tests/deterministic/unit/runtime/test_server_verifier_v2.py::test_zero_match_audit_distinguishes_scan_limitations_from_true_absence`
  - `tests/deterministic/unit/runtime/test_server_verifier_v2.py::test_selector_audit_distinguishes_missing_from_inactive_condition`
  - `tests/deterministic/unit/runtime/test_server_verifier_v2.py::test_lower_bound_quantifiers_do_not_overstate_observed_results`
  - `tests/deterministic/unit/runtime/test_server_verifier_v2.py::test_result_quantifiers_respect_complete_and_lower_bound_event_sets`
  - `tests/deterministic/unit/runtime/test_server_verifier_v2.py::test_rule_activated_selector_is_re_evaluated_after_mechanical_pass`
  - `tests/deterministic/unit/runtime/test_release_case_verification.py::test_release_case_ordered_selector_families_cover_each_member_position`
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_release_case_business_canaries_do_not_leak_into_generic_runtime`
  - `tools/test-flow/tests/release-case.test.mjs` 中 `Release supplement coverage respects requiredness and three-state activation`
  - `tests/deterministic/journey/test_rpc_timeout.py::test_r01_r14_rpc_timeout_is_one_durable_cross_module_path`
- **最新 Test Flow verdict**：待最终 A+B+C 合并源码快照的 fresh Release；尚未验证，任何独立 Dev verdict 均不可替代。

## PL-FIX-011：冻结清单摘要与 Windows 工作树字节不一致

- **状态**：实现已完成；仅当本条“最新 Test Flow verdict”引用最终 A+B+C 合并源码快照的
  fresh Release 可交付结论后，才视为已验证。
- **症状**：在 Windows 工作树中运行 deterministic contracts 时，
  `test_contract_manifest_covers_the_exact_frozen_inputs` 报告 `models.py` 和 `ports.py` 的实际
  SHA-256 与 `schemas/v2/contract-manifest.json` 不一致；完整 unit 随后还发现
  runtime-backend 的 `fake_claude.py` 以及 Release case 清单在 CRLF 工作树中发生大小和摘要
  漂移，而 Git blob 与权威清单使用 LF。同一冻结元数据漂移在最终集成静态审查中又表现为根
  README 顶部仍声明 Diagnosis Skill、GenerationSpec、generator 与 manifest v5，而生产 generator、
  manifest validator 和同页当前行为正文均已冻结为 v6/6.0.0；同页多个现在时运行说明还把当前
  package 写成 V3、把 `replay-job` 接受的 State/Job/Outcome 闭包写成 V5，与 `pyproject.toml` 的
  4.0.0 和合同 `SCHEMA_VERSION=6` 冲突。
- **受影响版本**：Problem Locator 3.0.0，当前基线 `f99a3d5`；清单中的两个摘要无法匹配相应
  文件的任何 Git 历史版本。Problem Locator 4.0.0 A+B+C 修复候选
  `git-visible-worktree-v1:95a4b2f8b4de9cac240eb137f9491bfc661372b4949448de0991e4d418caabd2`
  （609 files）再次受影响。
- **根因**：合同源码和 Agent fixture 演进后，冻结清单只同步了部分条目，遗留了无法对应当前
  Git blob 的摘要和换行后大小；仓库也没有统一固定 tracked 文本的 checkout EOL，
  `core.autocrlf=true` 会让正确的 LF 清单在 Windows 对比 CRLF 工作树字节。README 顶部版本表
  也曾由人工硬编码维护，未由 generator 与 verification source constants 的直接回归约束。
- **不可回归行为**：冻结清单的文件集合、顺序、大小和每个 SHA-256 必须精确对应当前
  canonical schema、合同源码、完整 OpenAPI 与 Agent fixture 字节；所有 Git 识别为文本的
  tracked 文件在各平台 checkout 均保持 LF，OpenAPI snapshot 也显式固定 LF。任何 manifest、
  fixture digest、identity pin 或源码 receipt 都必须由最终字节的权威生成/校验入口产生；不得
  手改摘要、跳过测试或改动产品字节来掩盖漂移。根 README 顶部的 Diagnosis Skill、
  GenerationSpec、generator 与 manifest 版本必须直接匹配当前 generator/verification 源码常量，
  package 与所有现在时产品版本说明必须匹配 `pyproject.toml`，当前 State/Job/Outcome 说明必须匹配
  contracts `SCHEMA_VERSION`；不得以另一组硬编码期望掩盖版本漂移。
- **修复历史**：2026-08-18 把 `models.py`、`ports.py` 和 runtime-backend `fake_claude.py`
  的清单元数据更新为 HEAD 的实际 canonical LF 字节，并用 `.gitattributes` 的
  `* text=auto eol=lf` 固定跨平台 checkout；没有修改这些被冻结文件、schema 或 wire 行为。
  A+B+C 集成又同时改变了 V6 contracts/schema、Generic V2、完整 OpenAPI、Release fixtures 和
  Test Flow identity 输入，因此丢弃三个来源各自的旧摘要，按最终 LF 工作树字节统一重生成并
  校验全部派生产物；保留同一问题的历史连续性，不另建重复修复条目。2026-08-18 在 bounded
  construction 修复继续改变交付 Skill 与 case clarifications 后，Dev
  `run-20260818T154610Z-0a2f9967` 又以 delivered Skill 18,246-byte hash 与旧 source-copy receipt
  不一致直接复现；回归原因是修复后没有再次刷新下游 receipt。现从权威
  `D:\code\problem-locator-mcp` checkout 的冻结 commit 重新读取 source blob，并从当前交付树
  重新生成 `source-copy.json`、logparse fixture manifest 与 release-case fixture manifest；未手改
  任一摘要。随后 Dev `run-20260818T155049Z-989d6715` 进一步确认首次临时生成脚本遗漏
  canonical JSON 必需的唯一末尾 LF；该生成步骤已改为仓库 `canonical_json_bytes` 的同一
  `sort_keys`、compact separators、UTF-8 与单 LF 规则，并据此重新生成 2,908-byte manifest。
  2026-08-19 为 bounded construction 新增四个 control-only checkpoint 后，再次从同一冻结
  upstream commit 和最终交付字节重生成 source-copy receipt；四个新增文件逐项记录 size/SHA-256，
  `source-copy.json` 为 7,142 bytes、SHA-256
  `cbb309fb20ee4d1a08eabe1da673ae4389d61be85489a0140c05ae6915f2e806`，其下游 canonical
  logparse fixture manifest 仍为 2,908 bytes，并同步固定新 receipt 摘要，未复用前一候选 pin。
  后续 Dev `run-20260818T165440Z-87345df4` 要求把 Skill 的审计锚点改成连续文本后，又从同一
  upstream receipt 入口重生最终 pin：`source-copy.json` 仍为 7,142 bytes、SHA-256
  `79acc521bc469698017a98702cdf750616c8f6fff39179ac2cf8af68f315f1be`，logparse fixture
  manifest 为 2,908 bytes、SHA-256
  `ec000190354b0cd38f6ef6cf36b40875f2801882f5643013f228f6927a72a95c`。本轮 `PL-FIX-016`
  又改变最终 Skill/checkpoint 语法冻结合同，故按同一生成入口再次重生最终候选 pin：
  `source-copy.json` 为 7,143 bytes、SHA-256
  `2015c531a75ccfe04327b73c4fd70d8c0f810e8b00aa776e7c496df57d208d4e`，下游 canonical
  logparse fixture manifest 为 2,908 bytes、SHA-256
  `a3cafadca1ad749b7107cc5dd374069adf8b803027f655715344bddcfcff684e`。最终把自由字符串
  Write 改为原生 StructuredOutput、同步 checkpoint 术语并补生成态 10/165/9 不变量后，又由同一
  入口重生最终候选 pin：`source-copy.json` 为 7,156 bytes、SHA-256
  `1915aa97db50a699deedc08acd533264858ad1cdf9806d2b7323b5bd1552a3c7`；下游 canonical
  logparse fixture manifest 仍为 2,908 bytes、SHA-256
  `d46633ee36489a58ee179b651782cc5232a6159c64a079972fa60cb57ff06a24`。以上摘要均由最终
  Git-visible 字节生成，未手改 pin。Dev `run-20260818T183756Z-e715d2a0` 随后在源码
  `git-visible-worktree-v1:46282c870d77362ed35e9c0726f6c085ebc6e68859ab5ec2f54a864192f59e72`
  （613 files）确认 receipt 本身与下游 manifest 已通过 contracts/fixture 校验，但
  `test_source_copy_receipt_has_complete_sorted_fields` 仍把 checkpoint 04 的旧 purpose 文本
  `single compact output Write` 当作期望，导致 unit 直接失败。回归原因是生成元数据改为
  StructuredOutput 时漏同步该测试常量；现只把期望更新为 receipt 中真实的
  `single typed StructuredOutput submission`，不改 receipt 算法、摘要或交付字节。Release
  `run-20260818T190152Z-30a86d3d` 后删除 post-submit completion、明确 StructuredOutput 即终止动作，
  又改变 Skill 与 checkpoint 04 的交付字节；故再次由同一权威入口重生：`source-copy.json`
  为 7,156 bytes、SHA-256
  `b66f47dbcbfbe0cbaba7d4d82e01c7fea3492fd6ac5c95999af05583e291c438`；下游 canonical
  logparse fixture manifest 为 2,908 bytes、SHA-256
  `727c21de8bd102506a649086ba6208d3f9ac56e6198afe7cd7c454dee81f605a`，未手改 pin。Release
  `run-20260818T195757Z-eea3189d` 证明 Claude CLI 在 StructuredOutput tool result 后仍需要终态
  assistant response；本轮改为唯一 wiki/clarifications batch、唯一 StructuredOutput 后精确 `DONE`，
  并同步 Skill/checkpoint 字节后，再由同一权威入口重生：`source-copy.json` 为 7,156 bytes、
  SHA-256 `8139ecb52e379d072c86564b0734247a31dd798e334c5939430b2b8426530cca`；下游 canonical
  logparse fixture manifest 为 2,908 bytes、SHA-256
  `1227774ab0737bb115dab2ed68814604a8fde802adf9349bbc2b5b62a2b540eb`，未手改 pin。2026-08-19
  最终静态安全审查又确认 README 顶部四项仍停留在 v5，而
  `SPEC_SCHEMA_VERSION=6`、`GENERATOR_VERSION="6.0.0"`、`MANIFEST_SCHEMA_VERSION=6`；现只把
  顶部 heading/table 对齐实际常量。逐行复核还确认同页“当前行为”仍写 v5 generator/manifest，
  六处现在时产品说明仍写 V3，`replay-job` 当前闭包仍写 V5；现分别对齐 v6、V4 与 State V6，并新增
  直接读取 generator、verification、contracts Python 常量及 `pyproject.toml` project version 的
  docs-drift 回归，锁定这些现在时说明不能沿用旧标记。未修改 generator、manifest、schema 或产品
  运行语义。该修复仍为 pending，必须由最终 fresh Release verdict 验证。Dev
  `run-20260819T011622Z-572a42d7` 随后在源码
  `git-visible-worktree-v1:b3f2f642ac26f0a31f301acfcdb890eb7500f1e0fdeace5504cea04396fe4287`
  （613 files）以 `FAIL / PRODUCT / PYTEST_FAILED` 结束；source verification 与 operation PASS，
  framework.self-test、repository.static、contracts 565/565、integration 45/45 与 SameJob 4/4 均 PASS，
  unit 为 1,788 pass / 3 fail / 66 skip，模型调用与 usage 全为零。其中
  `test_receipt_matches_source_and_every_delivered_byte` 精确指出 source-copy 仍保存旧 delivered Skill
  22,582-byte/`f4c49f7e…` 事实，而当前 Skill 已变为 23,255 bytes、SHA-256
  `443175876f75bceea269cbdac90ec1a5cf4f1a1068ffd39c780652bd2598b334`；回归原因是 first-submit
  修复改变 Skill/checkpoint 字节后没有刷新派生 receipt，而不是 `_file_facts` 算法错误。本轮严格复用
  `test_generator_copy._file_facts` 的 mode/size/SHA-256 公式重算：`SKILL.md` 为 mode `100644`、
  23,255 bytes/`443175…b334`，checkpoint 04 为 2,162 bytes/`3d2c72d5c3918669921e9990b034ded67bab40e431e037d68e628090ce857581`；
  重生 `source-copy.json` 为 7,156 bytes、SHA-256
  `d0193c8eac3dbe67de62c56dc508a95df519533c76d9b3b2f493e227fee05928`，并同步重生 2,908-byte
  logparse `fixture-manifest.json`（SHA-256
  `034b3ea8741e794557f00b6bcff7548295dce3cb347e0ea72b4ec484cdf22112`）。所有值均来自当前权威字节，
  未手改摘要；另两项 unit 回归归入 `PL-FIX-016`。下一轮 Dev
  `run-20260819T012656Z-21f3720b` 在源码
  `git-visible-worktree-v1:c14eb5faba6e4d6c451270ce72b2a65b68a4c61781a399cf307457d77f515822`
  （613 files）取得 `PASS_WITH_WARNINGS`；materialized/worktree 双重 source verification、operation、复用的
  framework/repository 与本轮完整 deterministic 均 PASS，affected 为 NOT_REQUIRED。contracts 565/565、
  unit 1,791 pass / 66 skip / 1,857、integration 45/45、SameJob 4/4 全部通过，模型调用、token 与 cost
  均为零；唯一非 PASS 维度是 `performance_status=NOT_CALIBRATED`。这直接验证 source-copy 当前字节、
  delivered/added-file facts 与下游 logparse fixture manifest 已重新闭合，但不替代 fresh Release。
  后续为 `run-20260819T013351Z-b1da9580` 的 Stage 3 有界化修复再次改变 Skill/checkpoint 01 字节；按同一
  `_file_facts` 公式一次性重生：`SKILL.md` 为 23,423 bytes、SHA-256
  `cb317f08dc73137d15a8e849d4615407b6e6048da9cd87fc9b3ba1e054b745f7`，checkpoint 01 为
  1,060 bytes、SHA-256 `9382515b29b3bc495b6d37e216df12cfaa33686b3aa94640b633b67d1178b887`，
  `source-copy.json` 为 7,157 bytes、SHA-256
  `78cba7698948fff627433a62a11bdab1980dc3addc84290f26130cf83f32a4d6`，logparse
  `fixture-manifest.json` 为 2,908 bytes、SHA-256
  `fbbbbf4ff7259fc91ad58097e2ce31f99855f997b41ecb4b498feb7b5f6285f5`。该轮派生 receipt 仍待下一个
  权威 Dev 验证，不得沿用上一快照结论。该候选随后由 Dev
  `run-20260819T022003Z-9b64b945` 在源码
  `git-visible-worktree-v1:972c599c6267a26ff2726dfc222473a4681e83dfbaa3760ab82d40b609280519`
  （613 files）以 `PASS_WITH_WARNINGS` 验证；双重 source verification、operation、framework、repository
  与完整 deterministic 均 PASS，模型调用与 usage 为零。Release
  `run-20260819T022320Z-0deb10f2` 随后在同一精确源码上失败后，最小阶段边界修复再次改变
  Skill/checkpoint 01 字节；本轮继续只按 `_file_facts` 权威公式重生：`SKILL.md` 为 23,438 bytes、
  SHA-256 `5c2d78a15c0038a42b37a1b09e92b816a9379a67ad1ea255892aaf244cf460e4`，checkpoint 01 为
  1,170 bytes、SHA-256 `7872c3901adc7511684743ff3951a411b73e383d9e01d5fcf67fbb4da4b30bb9`，
  `source-copy.json` 为 7,157 bytes、SHA-256
  `963b1f37e1853f86dfc86227be3bf0a97a76eab13afbed4fb909e5dd5cf34be9`，logparse
  `fixture-manifest.json` 为 2,908 bytes、SHA-256
  `90e2f3ec0aa1946ca5f0341eaefccbd266d7de31db7743c26d003c86bff56b89`。这些派生字节待下一轮
  权威 Dev 验证，不得沿用 972c 快照结论。Release `run-20260819T031213Z-53c83c3f` 的最小阶段拆分
  随后改变 Skill 与前三个 checkpoint；仍按同一公式一次性重生：`SKILL.md` 为 23,398 bytes、
  SHA-256 `c4b288f5dcf7900baf006ae13467f40374d75416d15cbb94ee4e6c06ccdda056`；checkpoint 01/02/03
  分别为 1,111/926/913 bytes，SHA-256 分别为
  `57024ffed857219451ea6a8e9f6aff1287eb03d8ab34c4024a6767eeb5c42c52`、
  `82409c158542d06df42d5bb46416648b4b240f281b83f25940467e32ba560ffe`、
  `c2b812dee83726b3c7d77c4b71a3927fdfbbf194fd6d95e37e9c472f15adf391`；
  `source-copy.json` 为 7,157 bytes、SHA-256
  `b90f9ed5972263a593078ec597a76270b44f1ca3982a72ce52edfe7d359ca8db`，logparse
  `fixture-manifest.json` 为 2,908 bytes、SHA-256
  `e33837df183c7e18625b2cf52f10d8872bd4762d14212d45f375df6af2106ce2`。新派生字节待下一轮权威
  Dev 验证，不得沿用任何前一快照 verdict。Release `run-20260819T035423Z-8d271214` 的 first-submit
  key/count 修复进一步改变 Skill/checkpoint 04；同一公式重生后，`SKILL.md` 为 23,751 bytes、
  SHA-256 `2a36665adeede3ae6cffa3513e6ee356245192c56631f2fd95a470968fbe9b3d`，checkpoint 04 为
  2,685 bytes、SHA-256 `5d5148d714cc5e796e43c655ba9dffb5b29ee2f5f5f0a9e38d91066e015bcb53`，
  `source-copy.json` 为 7,157 bytes、SHA-256
  `6a66548bbe1d3ca99d982c30b6083e8b2ee441855ce4a33406b0efafeb3b9297`，logparse
  `fixture-manifest.json` 为 2,908 bytes、SHA-256
  `23b4837db5b685e509f508606f7d1852af1522a2779db87dfd8dc129df5afe95`。这些新派生字节仍待权威
  Dev 与 fresh Release，不沿用旧 verdict。Release `run-20260819T042443Z-f79c4c06` 的单次物化修复
  又改变 Skill 与四个 checkpoint；同一公式重生后，`SKILL.md` 为 24,261 bytes、SHA-256
  `69bb57060ebb617bccb94c3bc937ac45ce4d2e31aa7d2651e48649740e096401`；checkpoint 01/02/03/04
  分别为 1,147/1,074/946/2,932 bytes，SHA-256 分别为
  `0edaf8c85040d9cc0d7ce023413d396ed2ecbadb93d976a2ecc749a8b9b9e69e`、
  `07929dfb9f59f7b7f750ad152be3a420356478607ead361700f903d59ae7f737`、
  `d4f9cbb36145f709bef4dffdf24e8565a9352dc55a91121d7e5e7d331e22452b`、
  `8b6e8b245ccd05c9314087fdfad475a5fd23a28c950da9c2c7c30460e83f24ca`；
  `source-copy.json` 为 7,157 bytes、SHA-256
  `12655c0a41158b6f9ada4d37b03af2985bd22f04ab031b14b29bec025441c941`，logparse
  `fixture-manifest.json` 为 2,908 bytes、SHA-256
  `0ff13c5c24e280778528733ebefc41152b342aa32bc30171c1e0588cbb337154`。新字节继续待权威 Dev 与
  fresh Release，不沿用前一候选 verdict。Release `run-20260819T051117Z-3ac62051` 的空首调用修复
  只改变 Skill/checkpoint 04：重生后 `SKILL.md` 为 24,544 bytes、SHA-256
  `1b69b5878799901f3ab7153627149a4acf87fb47aeb3a7dd82221a422b6fb0e1`，checkpoint 04 为
  3,309 bytes、SHA-256 `03d33ea8d61d9227190ac9ac3f5f0980ee44f5f1993dbbf74225f3a31f86257b`，
  `source-copy.json` 为 7,157 bytes、SHA-256
  `4875031dc1a8908ec49a80c4f4909b39721ce73dacd23cc6255215650f357183`，logparse
  `fixture-manifest.json` 为 2,908 bytes、SHA-256
  `2ac6e4e4778c1282baa394467f4775c80c5dc4947f76f86688307ba7a60f9d74`。这些新派生字节待下一轮
  权威 Dev 与 fresh Release，不沿用旧 verdict。Release `run-20260819T054518Z-57092337` 的正向 root-key
  修复只改变 Skill/checkpoint 04：重生后 `SKILL.md` 为 24,556 bytes、SHA-256
  `1cee91aae76d2fe4a8846a2a4e92b20626ffadc9b4b6b2ee3fa3bd8436ab9f99`，checkpoint 04 为
  3,635 bytes、SHA-256 `de6f4ad0dc0c0b6ae3c7ce0d4178218bdc2b283ace6848a099fa068c64615b2c`，
  `source-copy.json` 为 7,157 bytes、SHA-256
  `df4a3ea01f130408b15cbd2388547d26c5a05fcf4b0a6559b99705d9ee5d4438`，logparse
  `fixture-manifest.json` 为 2,908 bytes、SHA-256
  `969dfcc6023718c602f321b35f9c85352d1249f05e34c9f500538f91707bdbd2`。新字节继续待权威 Dev 与
  fresh Release，不沿用旧 verdict。Release `run-20260819T061847Z-116675e2` 的 typed-frame 修复只改变
  Skill/checkpoint 04：重生后 `SKILL.md` 为 24,785 bytes、SHA-256
  `9ede995ce81511d208cf0b522b4775930893b2895fbcd9e39777380b005c7f15`，checkpoint 04 为
  5,053 bytes、SHA-256 `4060e57ddbb5107372b4c8cbe8500cec226536cb8e2f4de54757e604b5e9bc8e`，
  `source-copy.json` 为 7,157 bytes、SHA-256
  `9f244e0475577af2aad7caee72876a37cb1f1b288f00c7a61e38d666df3feab5`，logparse
  `fixture-manifest.json` 为 2,908 bytes、SHA-256
  `05b940b9d3b11b008108dcc1de4c95a02566d1768391eca0bdd32487ccc2bf2c`。新字节待下一轮权威 Dev 与
  fresh Release，不沿用旧 verdict。
- **专项回归测试**：
  - `tests/deterministic/contracts/test_schema_snapshots.py::test_generated_schema_snapshots_are_byte_stable`
  - `tests/deterministic/contracts/test_schema_snapshots.py::test_contract_manifest_covers_the_exact_frozen_inputs`
  - `tests/deterministic/unit/runtime/test_agent_backend.py::test_backend_fixture_manifest_is_exact`
  - `tests/deterministic/unit/runtime/test_diagnosis_runtime.py::test_diagnosis_runtime_fixture_manifests_remain_contract_valid`
  - `tests/deterministic/unit/interfaces/test_web_api.py::test_openapi_contract_matches_versioned_snapshot`
  - `tests/deterministic/unit/integrations/test_generator_copy.py::test_receipt_matches_source_and_every_delivered_byte`
  - `tests/deterministic/unit/integrations/test_generator_copy.py::test_source_copy_receipt_has_complete_sorted_fields`
  - `tests/deterministic/unit/integrations/test_logparse_fixture_manifest.py::test_logparse_fixture_manifest_matches_schema_dto_and_disk`
  - `tests/deterministic/journey/test_rpc_timeout.py::test_rpc_timeout_fixture_manifest_is_schema_valid_and_exhaustive`
  - `tools/test-flow/tests/config-contract.test.mjs` 中 `Git checkouts and the current worktree preserve byte-pinned text as LF`
  - `tools/test-flow/tests/docs-drift.test.mjs` 中 `README Diagnosis Skill versions follow the generator and manifest source constants`
- **最新 Test Flow verdict**：pending；Dev `run-20260819T022003Z-9b64b945` 在源码
  `972c599c6267a26ff2726dfc222473a4681e83dfbaa3760ab82d40b609280519`（613 files）为
  `PASS_WITH_WARNINGS`，但其后 Release 失败所需的最小 Skill/checkpoint 修复已经重生上述派生 receipt；
  当前新字节尚待权威 Dev 与 fresh Release，故本条仍未登记为最终已验证。

## PL-FIX-012：Windows Test Flow 临时资源路径超过 MAX_PATH

- **状态**：实现已完成；仅当本条“最新 Test Flow verdict”引用最终 A+B+C 合并源码快照的
  fresh Release 可交付结论后，才视为已验证。
- **症状**：Windows `dev.default` 的 affected pytest 在多个真实存储集成用例中统一返回
  `RESOURCE_STAGE_FAILED`；相同用例使用极短临时根时通过。
- **受影响版本**：Problem Locator 3.0.0，当前基线 `f99a3d5`。
- **根因**：Test Flow 虽已缩短 Windows pytest scratch 目录名，但 `--basetemp` 仍使用普通
  Win32 路径；pytest 的测试名、proposal hash、Job UUID 和原子临时文件名组合后超过传统
  `MAX_PATH`，底层文件创建失败。
- **不可回归行为**：Windows pytest 必须先解析并固定一个绝对、可写、受控的 scratch
  boundary，只在其下创建独占 `p-*` 子目录；默认候选和显式 override 都不得把清理权限扩大到
  boundary 本身或任何越界路径。传给 pytest 及 SameJob/CrossJob 深层存储旅程的 Windows
  路径使用 drive/UNC 对应的扩展长度语义；选择短路径不能丢失执行身份，扩展路径也不能改变
  scratch 所属 boundary。Linux/macOS 继续使用 attempt-root 语义。真实文件和目录 staging
  集成测试必须能在标准 Codex worktree 深度下通过。
- **修复历史**：2026-08-18 为 Test Flow 增加跨平台 `pytestBaseTempPath`；Windows drive 和 UNC
  路径分别转换为 `\\?\` 与 `\\?\UNC\`，其他平台保持普通绝对路径；同时让两个复用 `.s08`
  数据根的确定性旅程在 Windows 使用相同扩展路径语义。A+B+C 集成进一步合并短路径选择、
  namespaced 独占 scratch、绝对 override 和 boundary-safe cleanup：物理目录选择与传给 Win32
  深层文件操作的扩展表示分离，但二者始终绑定同一受控根。
- **专项回归测试**：
  - `tools/test-flow/tests/actions.test.mjs` 中 `Windows pytest base temp uses an extended-length path without moving scratch`
  - `tools/test-flow/tests/actions.test.mjs` 中 `Windows pytest selects the shortest safe default and honors an absolute override`
  - `tools/test-flow/tests/actions.test.mjs` 中 `non-Windows pytest scratch keeps the attempt root boundary`
  - `tests/deterministic/integration/test_bootstrap_resource_export.py::test_nonempty_state_export_is_complete_canonical_and_generation_consistent`
  - `tests/deterministic/journey/test_rpc_timeout.py::test_r01_r14_rpc_timeout_is_one_durable_cross_module_path`
  - `tests/deterministic/journey/test_rpc_timeout.py::test_same_job_uses_initial_order_fact_and_survives_restart`
- **最新 Test Flow verdict**：待最终 A+B+C 合并源码快照的 fresh Release；尚未验证，任何独立 Dev verdict 均不可替代。

## PL-FIX-014：大规模有序事件家族在唯一 Write 前耗尽硬时限

- **状态**：实现中，待最终 fresh Release；最新 Release 已确认完整 submission schema、唯一提交
  与 cap 分类仍未闭合。仅当本条“最新 Test Flow verdict”引用修复后最终 A+B+C 合并源码快照的
  fresh Release 可交付结论后，才视为已验证。
- **症状**：相同 `real.agent.skill-generation` gate、runtime、模型和 caps 的旧目标曾在
  804.92 秒完成；A+B+C 集成候选把 GenerationSpec 从 55,967 bytes、33 rules 扩展为
  138,917 bytes、165 rules。fresh Release
  `run-20260818T144146Z-e49cee4b` 中 Agent 持续活跃，但除四个权威输入外又读取两个 optional
  示例，并在固定五位置规则矩阵上反复 `reconsider`、重启设计和逐规则叙述，直到 1800 秒硬
  时限仍未调用唯一 `Write`（Write count 为 0），真实 Skill generation Gate 因而失败。首次
  bounded 修复后的 fresh Release `run-20260818T155925Z-f6f83c0d` 已严格缩到四个权威 Read，
  但其后仍在同一无工具边界阶段连续产生 233,542 / 217,810 / 227,349 / 217,739 字符的四段
  thinking 并多次从对象开头重启；1496 秒后 terminal 以 `response exceeded the 64000 output token
  maximum` 失败，仍未调用 `Write`。
- **受影响版本**：Problem Locator 4.0.0 A+B+C 集成候选；失败源码快照
  `git-visible-worktree-v1:f1b13535c4bc1af864e8e0588e3da0fc829d095e2eea9ec35e8ae6b9d5052aac`
  （609 files）。首次修复后的 Problem Locator 4.0.0 候选
  `git-visible-worktree-v1:d77056bf8fcd90bb0fafdab9346b8967264b1ff8c54619057bf2ea00bda7b26d`
  （609 files）再次受影响。StructuredOutput 修复后的最终 A+B+C 候选
  `git-visible-worktree-v1:affbae0379b0b2bd97b55e81fecf09209865c1520e515f66b4b40dc8c0bcda1b`
  （613 files）仍受影响；materialized 与 worktree source verification 均为 PASS。其后候选
  `git-visible-worktree-v1:a171b8b87595bda183ac4cefa8931bff2dd877c7bd63bf27312f918e694cce4e`
  （613 files）再次受影响，source verification 同样为 PASS。随后 Dev 候选
  `git-visible-worktree-v1:038a91432bc7ebfb6fee239b446dca2db6ed87fa5724021e8e5e96a264178fd5`
  （613 files）又因本条专项/隔离合同回归受影响，其 materialized/worktree verification 均为 PASS。
- **根因**：转换合同虽然要求唯一最终 Write 和写前引用/正向 witness 审计，却没有为大规模、
  已由作者确认的有序事件家族规定有界构造顺序、最大 Read 集合和单次审计边界。模型因此把固定
  的 `q_{target}` 行列矩阵当作开放式设计问题，读取非必需示例并多轮重新推导；目标规模增长后，
  自由叙述消耗了硬时限。首次修复只限制 Read 集和文字指令，没有在非 verification 前缀、
  verification 基座、重复 rule family、9.1、9.2 与最终 Write 之间提供可审计工具边界；模型自动
  续写时反复丢弃阶段进度，最终把单次 64k output cap 消耗在内部叙述而非唯一 Write。cap 对分段后的
  紧凑对象仍足够，根因不是需要提高预算或放宽 gate。StructuredOutput 初版又只向 CLI 冻结
  `{type: object}`，没有把 GenerationSpec 的完整根字段、required/additional-properties 边界及
  2 roles、5 requirements、2 anchors、2 policies、10 extractors、165 rules、9 paths、4 time
  characteristics、5 analysis steps、6 judgement rules、5 output requirements、3 assumptions 的计数
  一并放进 submission schema。CLI 因而能把不完整 object 作为一次 successful StructuredOutput 后
  自动重试；wrapper 只在最终 terminal 后审计，也没有在第二次提交时立即终止。原先把
  StructuredOutput 本身当作 terminal、完全取消工具后的完成词，同样没有匹配 CLI 的真实完成协议。
- **不可回归行为**：Release 的单次 isolated invocation 继续固定
  `hard_timeout_seconds=1800`、1m context、`max_output_tokens=64000`、`max_budget_usd=10`、
  `max_turns=12`；不得提高任一 cap，也不得删除、折叠或近似五个目标位置、完整 165-rule 闭包
  和九条有序 terminal paths。Agent 只能按 exact 状态机执行 10 个工具调用：Skill，Wiki、author
  clarifications、GenerationSpec reference、verification reference 四个权威 Read，四个业务中性、
  control-only checkpoint Read，以及一次最终提交；禁止读取两个 optional 示例，禁止额外、重复或
  乱序 Read。历史失败及首次 bounded 修复使用自由字符串 Write，但 `PL-FIX-016` 确认其无法机械
  保证大对象 JSON 语法后，production 最终提交改为 Claude CLI 原生 `--json-schema` 约束的
  StructuredOutput，不再允许自由字符串 Write、empty/rejected Write 或第二次提交。Generation
  reference 后先完成非-verification
  前缀，再用 verification Read 形成边界；随后依次完成 policies/extractors/non-queue rules、重复
  family 与 paths、一次 9.1、一次 9.2，并分别以前进到下一个 checkpoint 结束。最后 checkpoint
  必须紧邻唯一 StructuredOutput；任何 checkpoint 内容不得进入产物。wrapper 只可把该 schema 已
  验证的根 object canonical、原子地落到唯一 GenerationSpec 路径，不得让模型自由拼接正文或在
  落盘后修补。提交 schema 必须冻结上述完整根字段与精确计数，不能再退化为仅验证 object 类型。
  为保持 12-turn cap，只允许 Wiki 与 author clarifications 两个独立 Read 在唯一同一 assistant event
  中 batch；其余 Read/checkpoint 仍保持 result-before-next 的串行边界。唯一 StructuredOutput 成功后
  只允许精确终止文本 `DONE`，不得解释、修补或再次提交；观察到第二次 StructuredOutput 必须立即
  bounded fail closed。若 terminal receipt 同时表明 turns 或 total tokens 超 cap，wrapper/Gate 必须
  优先分类为 cap exceeded，同时保留原 terminal subtype 与 content-free trace；不得因本轮失败提高
  12 turns、1800 秒、64k、1m 或 $10 cap。工具总数仍为 10，165 rules、九 paths 与业务 oracle 不变。
  content-free receipt 的 attempt policy 可把 exact `DONE` 保存为公开冻结的控制常量，但真实 terminal
  receipt 只能保存 `subtype/is_error`，不得复制模型实际 terminal 正文。运行时仍必须对真实 terminal
  result 执行 exact ASCII `DONE` 校验。通用 schema/audit 专项只能使用 synthetic neutral exact-count
  fixture；当前 approved fixture 的兼容断言必须位于动态发现 case 的 case-aware 测试中，不得在通用
  framework 文件硬编码任何 Release case ID 或业务 canary。
- **修复历史**：2026-08-18 以失败 Release 的模型流、1802.528 秒 Gate receipt、零 Write 和
  旧 PASS 804.92 秒对照确认 bounded-construction 回归；在 wiki-to-diagnosis Skill 与真实
  generation Gate prompt 中固定四个 Read、五位置矩阵、165 rules、九 paths、单次 9.1/9.2
  审计及审计后立即 Write 的顺序。修复只约束构造过程，没有改变模型、预算、时限、轮次、业务
  规则、terminal 顺序或输出合同。2026-08-18 首次修复候选 Dev
  `run-20260818T154005Z-019701f6` 又确认新增专项把 case ID 和业务 path canary 复制进通用
  deterministic 测试，违反既有 case 隔离边界；回归原因是测试直接硬编码当前用例而非从
  `case.json` 读取声明。专项现改为动态发现 release case，并只按通用矩阵表、声明计数和 extractor
  引用闭包核对，不在非 case 源码复制业务标识。2026-08-19 根据第二次失败 Release 的完整 terminal、
  四个 64k 量级 thinking 块、精确 Skill + 4 Read 序列、零 Write、471,286 total tokens 与
  $6.683358 receipt，确认单一 post-read 阶段仍不可交付；再次修复把现有 verification Read 和四个
  新增业务中性 checkpoint 组成六段状态机，并由 tool-trace audit 强制 exact 顺序、零 rejected
  Write 和最后 checkpoint 紧邻唯一 Write。模型、端点、业务合同、oracle、caps 与 gate 均未改变。
  Dev `run-20260818T165440Z-87345df4` 随后直接确认两个生产指令锚点被换行或复合句隐藏，导致
  `test_skill_contract` 的 fail-closed 文本合同无法识别；现保留连续“逐引用内部清单”并把
  `Do not read repository source` 恢复为独立禁令，未删除或放宽专项断言。fresh Release
  `run-20260818T170644Z-aa8e683c` 随后在源码快照
  `git-visible-worktree-v1:618be67a68ff80fd30cf970881ca237bc63d60d1663fd89f12d6190d1925d59a`
  （613 files）中以 1065.085 秒完成 exact Skill + 8 ordered Read + 1 immediate Write，tool trace、
  wrapper terminal、usage receipt 均为 PASS，证明本条所要求的 bounded trace 已实际满足；但唯一
  Write 的 146,007-byte 内容不是合法 JSON，后续 Gate 在首次 `json.loads` 即失败。该 downstream
  内容有效性问题登记为 `PL-FIX-016`；本次失败不能把本条状态提升为已验证，也不能替代最终 fresh
  Release 可交付 verdict。后续 `run-20260818T175029Z-a7f3b222` 再次证明八个 Read 和最终提交均能
  在 688.435 秒内完成，但模型仍以自由字符串 Write 产生语法无效对象；因此最终提交机制转为原生
  StructuredOutput。该调整只替换第十个工具的提交语义，不改变前九个工具、阶段边界、业务矩阵、
  模型或 caps，也不把本条标为已验证。fresh Release
  `run-20260818T190152Z-30a86d3d` 随后在精确源码
  `git-visible-worktree-v1:affbae0379b0b2bd97b55e81fecf09209865c1520e515f66b4b40dc8c0bcda1b`
  （613 files）中以 `terminal.subtype=error_max_turns` 再次失败：receipt 记录 13 turns，超过冻结
  `max_turns=12`；Gate 用时 930.785 秒，真实 invocation 为 924,029 total tokens、$5.088489，
  `timed_out=false`，未超过 1800 秒、1m total token、64k 单响应 output 或 $10 的其他冻结 cap。
  terminal 非成功使 `tool_trace_audit=null`，故不能从权威证据宣称实际完成了哪一步或曾成功提交
  StructuredOutput；scenario evaluation 未开始，六个 CrossJob Stage 均以
  `PRIOR_STAGE_NOT_PASSING` 未运行。当前最小修复只移除 Skill/prompt/checkpoint 中
  post-StructuredOutput “minimal completion” 要求，把唯一 StructuredOutput 自身定义为终止动作；
  caps、Skill + 8 ordered Reads + 1 StructuredOutput 的 exact 10 tools、165 rules、九 paths 与
  9.1/9.2 全部保持不变，不得以提高预算或盲重试替代。fresh Release
  `run-20260818T195757Z-eea3189d` 随后在源码
  `git-visible-worktree-v1:a171b8b87595bda183ac4cefa8931bff2dd877c7bd63bf27312f918e694cce4e`
  （613 files）再次 `FAIL / CONTRACT / PYTEST_FAILED`。新 partial/content-free trace 已按设计生效，
  并精确证明前九项 Skill + 8 ordered Reads 全部正确；其后模型却连续发起五次 outcome=SUCCESS 的
  StructuredOutput（3、34,635、34,635、8,344、34,635 canonical bytes），terminal 为
  `error_max_structured_output_retries`。invocation 用时 892.835 秒、16 turns、1,319,095 total tokens、
  $5.043923，既超过 12 turns 也超过 1m total-token cap，但未超时且未超过 $10。没有 canonical
  output seal、scenario audit 或任何 CrossJob 执行。该证据确认失败可观测性修复有效，却证伪
  “仅去掉 minimal completion 即可交付”的假设。本轮修复因此冻结完整 submission schema；只允许
  wiki+clarifications 唯一 batch 以回收一个 turn；唯一 StructuredOutput 后要求精确 `DONE`；第二次
  submit 立即 bounded 失败；cap-exceeded 优先分类且不提升任何 cap。Dev
  `run-20260818T205838Z-9756b05e` 随后在源码
  `git-visible-worktree-v1:038a91432bc7ebfb6fee239b446dca2db6ed87fa5724021e8e5e96a264178fd5`
  （613 files）以 `FAIL / HARNESS / NODE_TEST_FAILED` 结束；source verification 与 operation 均 PASS，
  framework.node-tests 为 267 pass / 2 fail / 3 skip，repository.static、affected 和 full deterministic
  均因前置失败 NOT_RUN，模型调用与 usage 全为零。第一项失败是 content-free 专项扫描完整 PASS
  trace 时命中 attempt policy 的 `terminal_result:"DONE"`；这是策略常量与模型正文共用字面值造成的
  测试/披露边界冲突，不是业务内容泄漏。第二项失败是通用 submission-schema JS 专项直接写入
  `rpc-timeout-anonymized` 路径，触发既有 case-canary 隔离测试。最小修复保留公开冻结的
  `terminal_result:"DONE"` policy 与其身份摘要，同时显式证明真实 terminal receipt 没有 `result` 字段；
  schema 专项改用 synthetic neutral fixture 填满全部 exact counts，并把 approved fixture 兼容断言迁到
  动态发现 case 的 case-aware 测试，不删除或放宽 case-canary 扫描。Dev
  `run-20260818T210520Z-63bccb96` 随后在源码
  `git-visible-worktree-v1:e92a476a98ec17641b6edca552a4ea30b9bed7134aa161cd33b15de79bdf9817`
  （613 files）以 `FAIL / PRODUCT / PYTEST_FAILED` 结束；source verification 与 operation 均 PASS，
  framework.self-test、repository.static 均 PASS，deterministic.affected 为 NOT_REQUIRED，contracts 565、
  integration 45 与 SameJob 4 个用例全部 PASS，unit 为 1,789 pass / 1 fail / 66 skip，模型调用与 usage
  全为零。唯一失败位于 `test_skill_contract.py:498`：专项正则没有容忍 Skill 在 `Write`、之后的
  Markdown 换行，而生产 Skill 仍完整禁止 `Write`、`Edit`、`Bash` 三个工具。现仅把该断言改为
  ``r"不得\s*调用\s*`Write`、\s*`Edit`、\s*`Bash`"``，不改 Skill、source-copy、工具禁令或其他业务语义；
  该失败 Dev 与断言修复均不能替代最终 fresh Release 验证。Dev
  `run-20260818T211156Z-a80c2c96` 随后在源码
  `git-visible-worktree-v1:a8c23f519149e4e6c6d4658bb36440c14b31ba91c74f960f16e6a4c2a0f9d06e`
  （613 files）仍以 `FAIL / PRODUCT / PYTEST_FAILED` 结束；source verification 与 operation 均 PASS，
  framework.self-test、repository.static 为 REUSED PASS，deterministic.affected 为 NOT_REQUIRED，
  contracts 565、integration 45 与 SameJob 4 个用例全部 PASS，unit 为 1,789 pass / 1 fail / 66 skip，
  模型调用与 usage 全为零。唯一失败仍在同一 `test_wiki_conversion_contract_is_self_contained_and_business_neutral`
  测试：它先正向要求“冻结 workflow schema”，随后又对整个 bounded state machine 全局禁止“冻结”，
  因断言顺序而在前次 regex 修复后才暴露这一自相矛盾。现只禁止退役 manual-string 协议短语
  `parse-equivalent`、`grammar pass`、`冻结字符串`、`frozen string`、`byte-for-byte`，并继续正向要求
  `冻结 workflow schema`；不改 Skill、submission schema 或运行时语义，状态仍为 pending。fresh Release
  `run-20260818T212706Z-38373f8a` 随后在源码
  `git-visible-worktree-v1:be28f7f23a3277fe0ac6c857282bfe137385869c819307de0d2f556a6f11451c`
  （613 files）因 `real.skill-generation` watchdog timeout 失败；该 terminal-less 失败属于
  `PL-FIX-015` 的证据回归，既没有 scenario/CrossJob 结果，也不能证明本条的 bounded construction
  或唯一 StructuredOutput 已满足。当前不据此修改 prompt、submission schema 或 caps，本条仍待最终
  fresh Release 验证。fresh Release `run-20260818T223051Z-3cb5cc2a` 随后在源码
  `git-visible-worktree-v1:461b7665070b59c6996328bf93ad4e8ce6a2ca9105c3f84a33794b67ed84847e`
  （613 files）确认前九项 Skill + 8 ordered Reads 全部成功，但首个 StructuredOutput 为 ERROR，模型又
  发起第二个 StructuredOutput，最终 11 tools、13 turns 并失败。该证据再次确认本条 exact 10-tool
  bounded construction 尚未闭合；首错约束的诊断缺口归入 `PL-FIX-016`，不得据此提高 caps 或允许
  第二次提交。fresh Release `run-20260819T003721Z-b8cad111` 又在源码
  `git-visible-worktree-v1:be1b77dd73a1340bf0cf732df4c430816be058bedbed0f13899c4de5d5cfb5dd`
  （613 files）确认相同边界仍未闭合：Skill + 8 Reads 全部 SUCCESS 后，ordinal 9 以 3-byte `{}`
  schema probe 被拒，ordinal 10 又因 `output_requirements=6` 超过冻结 5 项被拒，最终仍为 11 tools、
  13 turns。完整 v6 partial trace 与 constraint catalog 已把具体错误封存，最小 first-submit/count-vector
  修复归入 `PL-FIX-016`；本条只交叉记录，不提高 caps、不把失败当作 bounded construction 已验证。
  Dev `run-20260819T012656Z-21f3720b` 在源码
  `git-visible-worktree-v1:c14eb5faba6e4d6c451270ce72b2a65b68a4c61781a399cf307457d77f515822`
  （613 files）以 `PASS_WITH_WARNINGS` 验证了 checkpoint、first-submit/count-vector 与 exact 10-tool
  静态合同；full deterministic 全部 PASS、模型调用为零，唯一非 PASS 维度为 performance
  `NOT_CALIBRATED`。该 Dev 不能证明真实模型按 exact 10 tools 完成，也不能替代 fresh Release。
  fresh Release `run-20260819T013351Z-b1da9580` 随后在精确源码
  `git-visible-worktree-v1:128b087150570f9e412e6a35e2985c0873565c0a9bb18131b1b638877665619d`
  （613 files）于 `real.skill-generation` 失败；source/operation/verification 及所有前置 Stage 均 PASS。
  唯一 invocation 在 1,792.112 秒形成完整 22-event stream，usage 为 35,580 input、261,339 output、
  201,984 cache read、498,903 total tokens、`$6.812367`，7 turns 与 token/cost/time 均在冻结 cap 内；
  terminal 为 `subtype=success/is_error=true`，wrapper 正确分类 `WRAPPER_MODEL_TERMINAL_INVALID`。v6 partial
  trace 精确封存 Skill 与前四个 Read 全部 SUCCESS，停在 verification reference 返回后、checkpoint 01
  之前，未调用 StructuredOutput、未生成 scenario audit，六个 CrossJob Stage 全部未执行。该证据把修复
  限定为现有阶段边界：Stage 3 只构造两个 policies 与十个 extractors 后立即读取 checkpoint 01，禁止在
  同一阶段继续构造 rules 或自由推演；Stage 4 再先机械构造固定非排队 rules、后展开 q-family 与 paths。
  不新增工具、checkpoint、schema、receipt 或 cap，也不改变 165 rules/9 paths 业务合同。
  Dev `run-20260819T021419Z-fc348170` 随后在源码
  `git-visible-worktree-v1:00ea4fc1a0ad9ec06e2d0f8cd0dfb480c1d6a5e4f43cab24634ab86a920d7ef2`
  （613 files）以 `FAIL / PRODUCT / PYTEST_FAILED` 结束；source verification、operation、framework、
  repository、contracts 565/565、integration 45/45 与 SameJob 4/4 均 PASS，unit 为 1,791 pass / 1 fail /
  66 skip。唯一失败是既有总合同测试仍要求已被本次边界修复删除的旧短语 `不属于固定`；现一次性把该
  静态断言更新为真实不可回归语义 `不得构造任何 rule`，不改生产 Skill 或 Stage 设计。该失败 Dev 不构成
  验证完成，下一轮必须对同一修复后的完整确定性闭包重新给出权威 verdict。
  Dev `run-20260819T022003Z-9b64b945` 随后在源码
  `git-visible-worktree-v1:972c599c6267a26ff2726dfc222473a4681e83dfbaa3760ab82d40b609280519`
  （613 files）取得 `PASS_WITH_WARNINGS`；operation、verification、framework、repository 与完整
  deterministic 均 PASS，模型调用与 usage 为零。fresh Release
  `run-20260819T022320Z-0deb10f2` 随后在同一精确源码上以
  `FAIL / CONTRACT / PYTEST_FAILED` 结束：`real.skill-generation` 在 1,802.097 秒命中 1,800 秒 watchdog，
  child 由 SIGTERM 终止、wrapper exit 124；stream 为 23/23 parsed events、init 1/result 0/last
  `assistant`，terminal、turns 与 usage 均未知。现有 v6 evidence 已把失败精确分类为
  `WRAPPER_MODEL_TIMEOUT`，并以 `SKILL_TRACE_INCOMPLETE_PREFIX_REJECTED /
  SKILL_TRACE_PHASE_SEQUENCE_INVALID` 固定、content-free 地指出 verification reference 返回后没有按
  冻结相位立即进入 checkpoint 01。没有 StructuredOutput、scenario audit 或 CrossJob 执行。最小修复
  仅把 verification Read 后的下一独立 response 固定为零业务工作的 checkpoint 01 Read；checkpoint
  返回后才构造 policies、extractors、rules 与 paths。它不改变 v6 validation framework、工具数、schema、
  receipt、模型、retry 或任何 cap。对应总合同专项同步锁定 checkpoint 返回后必须先构造恰好两个
  policies 与十个 extractors、再构造固定 non-queue rules，避免沿用旧的 rule-first 文案产生布局无关的
  假阴性；生产 Skill、prompt 与业务计数不因测试修正而改变。
  Dev `run-20260819T030733Z-174768b7` 在源码
  `git-visible-worktree-v1:a56e2a2688a88ade147d3da244a0e3cca6af330badbb0432b2354242b306f9d8`
  （613 files）取得 `PASS_WITH_WARNINGS`，随后 fresh Release
  `run-20260819T031213Z-53c83c3f` 在同一精确源码再次以
  `FAIL / CONTRACT / PYTEST_FAILED` 结束。v6 partial trace 精确封存 ordinal 0–5：Skill、四个权威
  Read 与 checkpoint 01 全部 SUCCESS，但没有 checkpoint 02 或 StructuredOutput；terminal 为
  `subtype=success/is_error=true`，8 turns，模型 usage 为 36,221 input、258,052 output、227,584 cache
  read、521,857 total tokens、`$6.746197`，用时 1,569.001 秒，均未触发冻结 cap。失败已能由现有
  v6 安全定位为 checkpoint 01 后的单阶段构造仍过宽，因此只重排现有四个 checkpoint：Stage 4 仅构造
  policies/extractors/non-queue core 后读 checkpoint 02；Stage 5 只机械展开重复 families 与 paths 后读
  checkpoint 03；Stage 6 再依次各执行一次 9.1/9.2 并读 checkpoint 04。工具数、路径、validation
  framework v6、schema、模型、retry、165 rules、9 paths 与所有 caps 均保持不变。对应专项对 checkpoint
  Markdown 先做布局空白归一化，并锁定新的“只构造 core”精确语义，避免换行或旧“先构造”文案造成
  与生产合同无关的假阴性。
  Dev `run-20260819T035115Z-81a92961` 在源码
  `git-visible-worktree-v1:d08917919b8e117b3fcdf5f5e9feeaf51d1291555fda54a74d7719a3ca1d40cd`
  （613 files）取得 `PASS_WITH_WARNINGS`；fresh Release
  `run-20260819T035423Z-8d271214` 随后在同一精确源码的 ordinal 0–8 完成 Skill、全部八个 Read 与四个
  checkpoint，证明本条既有阶段拆分已实际跨越此前停点。最终失败发生在两次被 schema 拒绝的
  StructuredOutput，归入 `PL-FIX-016`；本条不再改变 checkpoint 阶段或 validation framework，仍待
  最终可交付 Release verdict 才登记为已验证。
  Release `run-20260819T042443Z-f79c4c06` 同样完成 ordinal 0–8 的全部既定工具与 checkpoint；两次
  StructuredOutput 均被 schema 拒绝，进一步确认阶段序列不是当前失败点。后续单次物化修复归
  `PL-FIX-016`，本条不再扩展阶段或证据框架。
- **专项回归测试**：
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_wiki_conversion_contract_is_self_contained_and_business_neutral`
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_wiki_conversion_checkpoints_are_control_only_and_forward_only`
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_real_wiki_gate_allows_only_inputs_and_declared_skill_references`
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_structured_output_success_requires_exact_done_terminal_response`
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_release_first_structured_output_cannot_be_empty_probe_or_six_item_retry`
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_release_verification_core_reaches_checkpoint_before_any_rule_construction`
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_release_conversion_keeps_declared_authoritative_matrices_complete`
  - `tools/test-flow/tests/release-case.test.mjs` 中 `case business canaries do not leak into framework, runtime, or non-case tests`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `audits one schema-bound StructuredOutput submission and seals only a v6 PASS receipt`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `phase checkpoint permissions grant six exact Skill reads and no optional or mutation path`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `rejects missing reordered partial batched or retried production phase checkpoints`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `the production conversion Skill exposes only two contracts and four frozen checkpoints`
  - `tools/test-flow/tests/evidence.test.mjs` 中 `a passing skill-generation invocation must retain a valid trace audit receipt`
  - `tests/real/agent/test_real_wiki_skill_generation_gate.py::test_real_conversion_agent_builds_an_executable_reviewed_skill_from_plain_wiki`
- **最新 Test Flow verdict**：pending，待上述实现冻结后的最终 fresh Release。失败 Release
  `run-20260818T190152Z-30a86d3d`、`run-20260818T195757Z-eea3189d`、
  `run-20260818T212706Z-38373f8a`、`run-20260818T223051Z-3cb5cc2a`、
  `run-20260819T003721Z-b8cad111`、`run-20260819T013351Z-b1da9580` 与 Dev
  `run-20260818T205838Z-9756b05e`、`run-20260818T210520Z-63bccb96`、
  `run-20260818T211156Z-a80c2c96`、`run-20260819T012656Z-21f3720b`、
  `run-20260819T013207Z-efca72b7`、`run-20260819T021419Z-fc348170` 均保留为权威回归历史。Dev
  `run-20260819T061449Z-9070b60f` 在源码
  `4d76671b4083a8cd226d88eb8c79b1f023b4d27fe5429ff235b425547ddb4d7c`（613 files）为
  `PASS_WITH_WARNINGS`，其后 Release `run-20260819T061847Z-116675e2` 在相同源码为
  `FAIL / CONTRACT / PYTEST_FAILED`；两者均不构成最终已修复、已验证或可交付结论。

## PL-FIX-015：模型超时误记为 STREAM_INVALID 并丢失调用 receipt

- **状态**：实现中，待最终 fresh Release；基础 timeout invocation receipt 与 v6 fixed-code
  prefix/rejection evidence 已落盘，但最新 Release 具有完整 terminal、没有重新命中该分支。仅当
  本条“最新 Test Flow verdict”引用修复后最终 A+B+C 合并源码快照的 fresh Release 可交付结论后，
  才视为已验证。
- **症状**：同一失败 Release 中，wrapper watchdog 已因 1800 秒硬时限终止真实模型进程，但
  随后先执行“必须存在 terminal result”的完整流 invariant，抛出
  `WRAPPER_MODEL_STREAM_INVALID`。由于异常发生在调用 receipt 落盘之前，Gate 报告
  `model_invocations=[]`、`usage_complete=false` 且 real-stage usage 为零；这既丢失已发生的模型
  调用，也容易把未知 usage 误读为零，并使 timeout 原因被次生 stream/harness 错误遮蔽。
- **受影响版本**：Problem Locator 4.0.0 A+B+C 集成候选的 Test Flow wrapper；失败源码快照
  `git-visible-worktree-v1:f1b13535c4bc1af864e8e0588e3da0fc829d095e2eea9ec35e8ae6b9d5052aac`
  （609 files）。StructuredOutput 候选
  `git-visible-worktree-v1:affbae0379b0b2bd97b55e81fecf09209865c1520e515f66b4b40dc8c0bcda1b`
  （613 files）仍包含本轮静态审查确认的 receipt/evidence 缺口。最终候选
  `git-visible-worktree-v1:be28f7f23a3277fe0ac6c857282bfe137385869c819307de0d2f556a6f11451c`
  （613 files）进一步确认 terminal-less timeout prefix 仍未封存。
- **根因**：isolated wrapper 把 terminal stream 完整性、usage 规范化和 receipt 写入串成只能
  成功到底的单一路径，没有让 watchdog 的 `timedOut` 状态优先决定失败分类，也没有在 terminal
  缺失时持久化不完整但可审计的调用事实。Gate usage 汇总因此只能看到“没有 receipt”，无法在
  保留原 pytest 失败的同时表达 invocation 已发生、usage 未知。后续 wrapper 又在 stream 完整性
  尚未成立时直接把 `init.model` 与 terminal 字段回写 receipt，并用 `Number(...)` 强转 usage/cost；
  这会让不匹配模型、畸形 terminal、数字字符串、布尔值或单元素数组进入看似可信的 receipt。
  非 fatal UTF-8 解码还可能把无效字节替换后继续解析，且完整流没有统一强制 init 为首事件、result
  为末事件。最终 evidence 的 PASS 审计此前主要复核 usage/caps/tool trace，没有独立重放
  stream、terminal、wrapper 与 child process 的完整状态机。上述修复后，timeout invocation 已能
  正确分类并写 sanitized receipt，但 partial tool audit 仍强制恰好一个失败 terminal result；watchdog
  timeout 天然是 `result_count=0`，因此审计抛错后 wrapper fail closed 为 `tool_trace_audit=null`，丢失
  已完成到哪一工具 ordinal、是否存在 pending 工具以及安全阶段路径的诊断能力。新增合法 prefix
  auditor 后，wrapper 又把其抛出的受信固定 audit code 与任意非受信异常一并 catch 成 `null`；真实
  timeout 因而仍无法区分“安全 prefix 被哪条固定 invariant 拒绝”和“不可信异常不得保留”的两类结果。
- **不可回归行为**：watchdog timeout 必须稳定分类为 `WRAPPER_MODEL_TIMEOUT` 并以 124 退出；
  在退出前原子、create-only 地写入 sanitized invocation receipt，保留 invocation ID、有效模型
  （若唯一 init 可得）、冻结 caps、进程终止、stream 计数和 `timed_out=true`。terminal 或密封
  usage 不存在时必须写 `usage_complete=false`、`usage=null`、`terminal=null`，明确表示未知，
  不得伪造零 token/cost；失败 usage summary 仍须保留该 invocation 并标为 `INCOMPLETE`。原始
  pytest/Gate 失败是主失败，不得被缺失 receipt 或 HARNESS 汇总错误覆盖。非 timeout 且缺 terminal
  的流仍分类为 `WRAPPER_MODEL_STREAM_INVALID`；PASS 仍要求唯一 terminal、完整 usage、精确 caps
  与全部既有审计合同。receipt 的 `effective_model` 只能在唯一首事件 init 精确匹配冻结模型时回写
  冻结值，否则必须为 `null`；terminal 只有字段类型与状态机均有效时才可保存，否则为 `null`。
  usage/cost 必须在强转前就是有限、非负的原生 number，token 还必须是 safe integer；字符串、布尔值
  与数组一律拒绝。stream 必须用 fatal UTF-8 解码，严格满足 init 首、唯一 result 末及计数闭合。
  evidence 对 PASS invocation 必须独立复核 stream complete/count/order 摘要、terminal success、
  frozen model、usage/caps、`timed_out=false`、child exit 0/no signal、wrapper PASS/exit 0 和有效 tool
  trace；任一 tamper 都不得沿用局部 PASS。terminal-less watchdog timeout 还必须生成独立、严格且
  content-free 的 prefix receipt：已完成工具只记录 ordinal/name/outcome；唯一初始
  wiki/clarifications Read batch 可以同时保留两个 pending，除此以外至多一个 pending 工具且只记录
  ordinal/name；Read 只能保存验证后的受控相对路径，StructuredOutput 只能保存 canonical
  size/SHA-256，禁止正文、输入、prompt、snippet、绝对路径或 raw stream。该 prefix 必须是冻结生产
  工具序列的合法前缀；unsafe path、畸形/额外/乱序工具或不安全 pending 状态若以仓库固定枚举中的
  受信 `SKILL_TRACE_*` code 被 auditor 拒绝，且 canonical stream summary 本身仍完全落在冻结安全枚举内，
  wrapper 必须另写严格、content-free 的 incomplete-audit-rejection receipt，只保存该固定 audit code，
  不得保存 message、details、path、tool input、raw stream、content 或其他模型数据。未知/非受信 stream
  event type 或 shape、非枚举 code、普通 Error 及其他非受信内部异常必须保持 `null`。timeout prefix 与
  rejection receipt 永远为 FAIL，不得满足任何 PASS validator；
  actions/evidence 必须独立复核 code catalog、exact keys，以及它与 `timed_out=true`、terminal/usage null、
  watchdog `SIGTERM|SIGKILL`/wrapper exit 124 和 incomplete summary 的一致性。该证据修复不得改变
  prompt、submission schema 或 caps。
- **修复历史**：2026-08-18 根据 `run-20260818T144146Z-e49cee4b` 的 wrapper stderr、缺失
  model-usage/scenario receipt 和零 invocation 记录确认独立 Test Flow 缺陷；重排 wrapper 的
  timeout/stream/usage 判定，先形成失败码，再无论成功或失败都原子写调用 receipt。usage collector
  在 action 已失败时接受并汇总不完整 receipt，同时继续拒绝把它用于 PASS；未放宽模型 cap、
  terminal、工具审计或秘密扫描要求。2026-08-19 Dev
  `run-20260818T164909Z-91111be5` 又确认新增 PASS evidence 专项的夹具把 token hard-cap marker
  误写成 `input+output+cache_creation_input+cache_read_input`，权威验证器按生产公式正确拒绝为
  `MODEL_HARD_CAP_RECEIPT_MISMATCH`；该测试现直接复用共享 `TOKEN_USAGE_FORMULA`，避免测试夹具
  与 wrapper 的 `input_tokens+output_tokens+cache_creation_input_tokens+cache_read_input_tokens`
  冻结合同再次漂移，生产 validator 未作放宽。2026-08-19 本轮静态安全审查沿
  `run-20260818T190152Z-30a86d3d` 的失败 receipt 反向检查 wrapper/evidence 全链，确认上述四类
  fail-closed 缺口。修复改用 fatal `TextDecoder`；只接受首事件唯一 init 与末事件唯一 result；先对
  terminal metadata 和 raw numeric usage 做类型/范围检查，再形成 sanitized receipt；模型只回写
  冻结值，不匹配时为 `null`。evidence 新增完整 isolated invocation 状态机验证，并对 stream count、
  terminal、process、wrapper 与额外 raw field 的 tamper 逐项拒绝。该修复不放宽 timeout、模型、
  caps、usage 公式、tool trace 或任何 PASS 条件。fresh Release
  `run-20260818T212706Z-38373f8a` 在源码
  `git-visible-worktree-v1:be28f7f23a3277fe0ac6c857282bfe137385869c819307de0d2f556a6f11451c`
  （613 files）以 `FAIL / CONTRACT / PYTEST_FAILED` 结束；source verification 与 operation 均 PASS，
  framework、repository、deterministic.full 与两个 platform stages 均 PASS，affected 为 NOT_REQUIRED。
  唯一 `deepseek-v4-flash[1m]` invocation 在
  1802.173 秒 Gate 中留下 32 个已解析 events、init 1、result 0、last event `assistant`，随后 watchdog
  SIGTERM、wrapper exit 124；terminal、turns、usage 与真实 cost 均未知，scenario audit 未产生，六个
  CrossJob stages 全部 `NOT_RUN / PRIOR_STAGE_NOT_PASSING`。verdict 的 46 tokens、$0.00025 仅来自 host
  capability，不能当作真实 Skill invocation 的 usage。基础 timeout receipt 已正确记录
  `WRAPPER_MODEL_TIMEOUT`、`usage_complete=false`、`usage=null`，但因 terminal-less partial audit 要求
  result，`tool_trace_audit=null`，无法权威判断实际十工具序列完成到哪一步。本轮只补上述 content-free
  timeout prefix evidence，不改 prompt、submission schema、模型或 caps，也不把失败 Release 当验证。
  Dev `run-20260818T222144Z-adf23b84` 随后在源码
  `git-visible-worktree-v1:2f1acb6bc781f45c80f815a483301662a633183153f84bc0c2db1b8720c2c1b1`
  （613 files）以 `FAIL / HARNESS / NODE_TEST_FAILED` 结束；source verification 与 operation 均 PASS，
  framework.config、docs.current 为 PASS，framework.node-tests 为 313 pass / 1 fail / 3 skip，后续
  repository、affected 与 full deterministic 均因前置失败 NOT_RUN，模型调用与 usage 全为零。唯一失败
  是 `Skill generation uses one structured output and wrapper-owned canonical materialization` 在新增 engine
  evidence 静态断言时引用了当前 `test(...)` 作用域中未定义的 `engine`；同名读取只存在于前一个测试的
  局部作用域。这是 timeout-prefix 专项夹具回归，不是 runtime evidence 合同失败。修复只在该测试内
  局部读取 `tools/test-flow/lib/engine.mjs` 后执行原断言，不改 runtime、prompt、submission schema 或
  caps；该修复仍须新的权威 Test Flow verdict 验证。fresh Release
  `run-20260818T223051Z-3cb5cc2a` 随后产生完整 terminal 与 schema-v5 failed trace，证明非 timeout
  失败可封存前九项成功工具及两个 StructuredOutput 的 outcome/size/SHA-256；但本次并非
  terminal-less timeout，不能验证本条 incomplete-prefix 分支。首个 StructuredOutput 的约束级诊断
  缺失归入 `PL-FIX-016`，不在本条重复建项。fresh Release
  `run-20260818T231502Z-aa07942c` 随后在源码
  `git-visible-worktree-v1:a0702712091c1a8e9cd6e9bebf8a291e29e04088a0f769228007a642e76073fc`
  （613 files）再次 `FAIL / CONTRACT / PYTEST_FAILED`；source verification 与 operation 均 PASS，
  framework、repository、deterministic.full 与两个 platform stages 均 PASS，affected 为 NOT_REQUIRED。
  唯一 `deepseek-v4-flash[1m]` invocation 在 1802.103 秒留下 32 个已解析 events、init 1、result 0、
  last event `assistant`，随后 SIGTERM、wrapper exit 124；terminal、turns、usage 与真实 cost 均未知，
  `tool_trace_audit=null`，六个 CrossJob stages 全部 NOT_RUN。verdict 的 46 tokens、$0.00025 仍仅来自
  host capability。冻结源码已经包含合法 incomplete-prefix auditor，但 wrapper 在该真实 stream shape
  上捕获审计异常后只保留 `null`，无法知道是哪条固定 prefix invariant 拒绝；本轮最小修复只增加上述
  固定枚举 incomplete-audit-rejection receipt，非受信异常继续为 `null`，不放宽 prefix、PASS 或 caps。
  fresh Release `run-20260819T003721Z-b8cad111` 在源码
  `git-visible-worktree-v1:be1b77dd73a1340bf0cf732df4c430816be058bedbed0f13899c4de5d5cfb5dd`
  （613 files）没有再次命中 terminal-less timeout：唯一模型 invocation 为 36/36 parsed events、init 1、
  result 1、last `result`、`timed_out=false`，因此不存在 incomplete-prefix/rejection `audit_code`。本次
  schema-v6 `SKILL_TRACE_RESULT_NOT_SUCCESS` partial receipt 仍完整、content-free 地保留两次失败
  StructuredOutput 的 ordinal/outcome/size/hash 与固定 catalog diagnostic，证明 v6 失败证据链对完整
  terminal 正常工作；但它不能验证本条 terminal-less fixed-code rejection 分支，主失败仍归
  `PL-FIX-016`，本条状态保持 pending。
  fresh Release `run-20260819T022320Z-0deb10f2` 在源码
  `git-visible-worktree-v1:972c599c6267a26ff2726dfc222473a4681e83dfbaa3760ab82d40b609280519`
  （613 files）重新命中 terminal-less watchdog timeout；wrapper 正确分类 `WRAPPER_MODEL_TIMEOUT`、
  exit 124，usage 保持未知而非伪零。v6 fixed-code rejection receipt 进一步精确给出
  `SKILL_TRACE_PHASE_SEQUENCE_INVALID`，没有保存 prompt、thinking、路径或 raw stream。这直接证明本条
  timeout/prefix/rejection 证据链已按设计工作；对应业务相位偏差归 `PL-FIX-014` 修复，不再改动
  validation framework。本条仍须由最终可交付 fresh Release verdict 才能登记为已验证。
- **专项回归测试**：
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `a timed out model persists an incomplete sanitized receipt before exiting 124`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `a non-timeout stream without a terminal result persists STREAM_INVALID evidence`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `mismatched model and malformed terminal fields never enter the receipt`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `raw usage and cost reject string boolean and array coercions`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `invalid UTF-8 makes the whole stream invalid without retaining decoded replacements`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `the wrapper requires init first even for a non-Skill workflow`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `error_max_turns preserves a content-free partial trace without materializing StructuredOutput`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `an unsafe unsuccessful trace fails closed to null without exposing the raw path`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `skill-generation timeouts retain every safe production prefix without creating output`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `rejected timed-out Skill prefixes retain only a fixed audit code and canonical stream`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `a late init in a parse-complete terminal-less timeout seals only INIT_INVALID`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `terminal-less event types are frozen without leaking unknown or error payloads`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `malformed JSON and invalid UTF-8 remain STREAM_INVALID with null Skill audit`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `seals an unsuccessful terminal as a content-free partial trace that can never be a public PASS`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `partial audit rejects a pre-init tool, late init, or post-result event`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `audits every completed terminal-less production checkpoint and the initial pending batch`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `hashes pending and completed StructuredOutput prefixes and diagnoses a second attempt`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `terminal-less audit fails closed on unsafe inputs and its receipt validator rejects tampering`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `seals a rejected terminal-less audit as an exact content-free FAIL-only receipt`
  - `tools/test-flow/tests/actions.test.mjs` 中 `failed pytest requires a content-free audit for every parse-complete terminal-less Skill timeout`
  - `tools/test-flow/tests/actions.test.mjs` 中 `missing failed invocation usage remains incomplete instead of hiding the original Gate failure`
  - `tools/test-flow/tests/evidence.test.mjs` 中 `a failed planned invocation requires a sealed audit for a parse-complete terminal-less Skill timeout`
  - `tools/test-flow/tests/evidence.test.mjs` 中 `a passing skill-generation invocation must retain a valid trace audit receipt`（含 stream/process/terminal/raw-field tamper）
  - `tools/test-flow/tests/engine-usage.test.mjs` 中 `engine independently binds terminal-less prefix and rejection evidence to timeout state`
  - `tools/test-flow/tests/release-inputs.test.mjs` 中 `model invocations preserve failed terminals while PASS still requires exact caps and complete usage`
  - `tools/test-flow/tests/release-inputs.test.mjs` 中 `Skill generation uses one structured output and wrapper-owned canonical materialization`
- **最新 Test Flow verdict**：pending，待 timeout prefix/rejection evidence 修复后的最终 fresh Release。
  fresh Release `run-20260818T212706Z-38373f8a` 确认 terminal-less prefix 缺失；其后 Dev
  `run-20260818T222144Z-adf23b84` 在源码
  `2f1acb6bc781f45c80f815a483301662a633183153f84bc0c2db1b8720c2c1b1`（613 files）为
  `FAIL / HARNESS / NODE_TEST_FAILED`；Release `run-20260818T223051Z-3cb5cc2a` 虽产生完整 failed
  trace，但未进入 terminal-less timeout 分支；最新 Release `run-20260818T231502Z-aa07942c` 在源码
  `a0702712091c1a8e9cd6e9bebf8a291e29e04088a0f769228007a642e76073fc`（613 files）再次留下
  `tool_trace_audit=null`。其后的 Release `run-20260819T003721Z-b8cad111` 产生完整 terminal 与有效 v6
  partial trace，但没有进入 terminal-less 分支；Release `run-20260819T022320Z-0deb10f2` 则产生
  `WRAPPER_MODEL_TIMEOUT / SKILL_TRACE_INCOMPLETE_PREFIX_REJECTED /
  SKILL_TRACE_PHASE_SEQUENCE_INVALID` 的有效 v6 terminal-less evidence，但最终 verdict 仍为 FAIL；以上均
  不构成最终已修复、已验证或可交付结论。

## PL-FIX-016：自由字符串最终提交语法无效，且失败证据越过最小披露边界

- **状态**：实现中，待最终 fresh Release；content-free partial trace、完整 submission schema、固定
  catalog diagnostic 与 cap 优先分类已由失败 Release 证明生效，first-submit/count-vector 修复已落盘但
  尚未取得权威 Test Flow verdict。只有本条“最新 Test Flow verdict”引用最终 A+B+C 合并源码快照的
  fresh Release 可交付结论后，才视为已修复且已验证。
- **症状**：fresh Release `run-20260818T170644Z-aa8e683c` 的
  `real.agent.skill-generation` invocation 在冻结模型和 caps 内正常 terminal，严格执行 Skill、八个
  ordered Read 和最后 checkpoint 后唯一一次 successful Write；tool-trace audit、wrapper outcome、
  stream、usage receipt 均记为 PASS。该 Write 生成
  `workspace/output/generation-spec.json`（146,007 bytes，SHA-256
  `f6d5ad3dfc34d364d3c600775c2bc0fc58003707e5ddfcda0d3d8fb5b197b127`），但真实 Gate 首次读取时
  报 `JSONDecodeError: Expecting ',' delimiter: line 1 column 59228 (char 59227)`，JUnit 唯一用例
  1/1 FAIL。解析发生在 compile、validator、业务 invariant 和 scenario evaluation 之前，因此没有
  `scenario-evaluation-audit.json`，六个 CrossJob Stage 均以 `PRIOR_STAGE_NOT_PASSING` 未运行；
  scratch 清理后权威证据只剩产物 hash/size 与 JUnit 的截断表示，不能复查错误位置附近的精确字节。
  首次修复后的 fresh Release `run-20260818T175029Z-a7f3b222` 已让 tool audit 在 loader 之前正确拒绝
  第二份语法无效 Write：151,585 UTF-8 bytes、SHA-256
  `c4ff3a0a3111356fc174ed96d4d929b6355c5fdcfd236dbe91a6deddcf5f0009`，稳定分类为
  `SKILL_TRACE_WRITE_JSON_INVALID / WRAPPER_SKILL_TRACE_INVALID`，位置为 line 1、column 59564、
  offset 59563。精确失败字节显示 `q_third_cover_first_second_no_gap` 规则末尾在
  `clock_tolerance_ms` 后多出一个 `}`；模型仍声称已完成 RFC 8259 parse-equivalent pass，证明该
  自然语言自检不具机械权威。与此同时，真实测试的 RuntimeExecutionError 分支把完整 Agent stdout
  插入 pytest failure，形成 1,921,833-byte JUnit 和 1,102,149-byte pytest stdout log，其中包含完整
  Skill、Read 与 Write 内容；model receipt 虽为 content-free，外围证据仍越过最小披露边界。
- **受影响版本**：Problem Locator 4.0.0 A+B+C 集成候选；失败源码快照
  `git-visible-worktree-v1:618be67a68ff80fd30cf970881ca237bc63d60d1663fd89f12d6190d1925d59a`
  （613 files）。该快照的 materialized 与 worktree source verification 均为 PASS，故失败不是源码
  漂移；模型为 `deepseek-v4-flash[1m]`，调用 12 turns、903,172 total tokens、$5.35456，未超时且
  usage 完整。首次修复后的 Problem Locator 4.0.0 候选源码快照
  `git-visible-worktree-v1:f2ed0d5f3293eb54b64fb64bcd055a92a4663432eadef83a8b6848dac0b3c578`
  （613 files）再次受影响；其 materialized/worktree verification 同样均为 PASS，模型调用 12 turns、
  780,485 total tokens、$3.587525，未超时且 usage 完整。StructuredOutput 候选源码快照
  `git-visible-worktree-v1:affbae0379b0b2bd97b55e81fecf09209865c1520e515f66b4b40dc8c0bcda1b`
  （613 files）又受影响；其 materialized/worktree verification 均为 PASS。partial-trace 修复候选
  `git-visible-worktree-v1:a171b8b87595bda183ac4cefa8931bff2dd877c7bd63bf27312f918e694cce4e`
  （613 files）仍受影响，其 source verification 也为 PASS。后续 Dev 候选
  `git-visible-worktree-v1:038a91432bc7ebfb6fee239b446dca2db6ed87fa5724021e8e5e96a264178fd5`
  （613 files）又因 content-free 专项与 case 隔离回归受影响，source verification 仍为 PASS。
- **根因**：final checkpoint 把“紧邻唯一 Write”作为最后过程边界，但 `Write` 成功只证明工具已
  接受并落盘给定字节，不证明这些字节构成合法 UTF-8 JSON 或可加载 GenerationSpec。现有
  tool-trace audit 只核对工具 inventory、顺序、路径、outcome、size 和 digest，未把唯一 Write 的
  JSON 语法/根对象合同纳入 PASS 后置条件，因而对语法无效产物仍封出 PASS trace。真实 Gate 虽然
  fail closed，但其 scenario audit 只在 `load_generation_spec`、compile、validator 和 semantic
  检查之后写入；最早的 JSON decode failure 没有结构化诊断分支，也没有在 scratch 清理前把失败
  产物纳入密封证据，形成“最终 verdict 正确失败、局部 trace 错误 PASS、诊断证据不足”的组合。
  首次修复把 `JSON.parse` 加入 wrapper 后已经纠正局部 trace 误 PASS，但仍让模型手写并自行审查
  约 13.6 万字符的 JSON；自然语言 grammar pass 只是模型陈述，不是 parser，无法拒绝重复 rule
  family 中稳定出现的 delimiter 错误。wrapper 正确返回 bounded failure 后，真实 Gate 又把捕获的
  stdout/stderr 全量格式化进 `pytest.fail`，从旁路重新保存了本应只以 hash/size/position 表达的
  内容，故内容有效性和最小披露两部分根因均未闭合。StructuredOutput 迁移后又只冻结根
  `type=object`，没有把完整 GenerationSpec 根字段、required/additional-properties 与 10/165/9 等
  精确计数交给 CLI；不完整 object 因此可以触发多次 tool-level SUCCESS 与 CLI retry。wrapper 未在
  第二次提交时即时 bound，且 terminal-invalid 判断先于 turns/total-token cap，最终掩盖了同时发生的
  cap exceed。把 StructuredOutput 本身视为无需终止词的最终动作也与 CLI 实际协议不一致。完整 schema
  冻结后，failed trace 仍只为被拒 StructuredOutput 保存 outcome、size 与 SHA-256，没有保存由同一个
  冻结 schema 本地计算的约束级诊断；因此能够证明首次提交失败，却不能判断具体是哪个 required、
  additional-properties、type 或 cardinality 约束失败，也不能据此选择有证据的最小语义修复。
- **不可回归行为**：不得把模型声称完成 grammar pass、terminal success 或完整 usage 单独当作 Skill
  generation PASS。保持 `PL-FIX-014` 的 exact 10-tool 总数、前九项 Skill + 8 ordered Reads、冻结
  12 turns、1800 秒、64k 单响应 output、1m total token 和 $10 cap，不得以增加模型调用、提高 cap、
  盲重试、手工补逗号或放宽 GenerationSpec/业务 oracle 解决。production 第十项不再允许自由字符串
  Write，必须使用 Claude CLI 原生 `--json-schema` 绑定的 StructuredOutput；schema 机械要求根值为
  object，非空、完整字段、165-rule 闭包、九条 terminal paths、9.1/9.2 与业务语义仍由既有
  GenerationSpec loader、validator、业务 invariant 和 scenario oracle fail closed。最后 checkpoint
  必须紧邻首次且唯一 StructuredOutput，禁止额外正文、第二次提交或
  compatibility Hook。wrapper 必须只接受唯一 schema-valid structured result，按仓库 canonical JSON
  规则序列化，并在受控 output boundary 内 create-only、原子落盘；不得让模型或 harness 猜测修补
  标点。canonical 字节随后仍须通过既有 GenerationSpec loader、generator、validator、业务 invariant
  和全部 scenario oracle。缺失、多份、schema 无效或落盘失败的 StructuredOutput 必须 fail closed，
  模型调用 receipt、pytest failure、JUnit、stdout/stderr 与其他密封证据都只能保留 bounded code、
  invocation ID、size、SHA-256 和必要位置/阶段，不得保存 prompt、Read/Write/StructuredOutput 正文或
  snippet。完整 terminal/usage 仍须保留，局部 trace、Gate、Stage 和最终 verdict 必须一致，且源码
  从 planning 到 verdict 仍须零漂移。对 `error_max_turns`、output-cap、timeout 或其他非 PASS
  terminal，也必须写 content-free partial failure trace，至少密封已观察到的工具 ordinal/name/outcome、
  阶段 checkpoint 进度与策略身份；不得包含 tool input、Read/StructuredOutput 内容、prompt、snippet
  或原始 stream，也不得把 partial trace 用作 PASS。StructuredOutput 不得用作 schema-discovery、
  validation probe、trial 或 partial submission；第一次调用前必须按同一个冻结 schema 核完全部 20 个
  required 根字段与完整 count vector，第一次调用本身必须已经是唯一完整 schema-valid submission。
  工具返回 ERROR 时必须立即停止，不得修改后重试或第二次调用。唯一成功 StructuredOutput 后只允许
  精确 `DONE`，不得包含其他 completion 内容；任何第二次 StructuredOutput 仍必须立即 bounded fail
  closed。CLI submission
  schema 必须冻结完整根字段、required/additional-properties 以及 2 roles、5 requirements、2 anchors、
  2 policies、10 extractors、165 rules、9 paths、4 time characteristics、5 analysis steps、6 judgement
  rules、5 output requirements 与 3 assumptions 的精确计数。只允许 wiki 与 clarifications 两个 Read
  在唯一同一 assistant event batch，其他阶段仍严格串行。cap exceed 必须优先于一般 terminal-invalid
  分类，且不得提高冻结 caps。所有 receipt 还必须以 fatal UTF-8、首事件
  init/末事件 result、冻结模型匹配、原生 numeric usage 与安全 terminal shape 为前置条件；PASS
  evidence 必须独立复核 stream、terminal、process、wrapper 和 tool trace 的完整状态机。attempt
  policy receipt 可把 exact `DONE` 保存为公开冻结的控制常量，但真实 terminal receipt 不得保存
  `result` 或其他模型正文；terminal 仍必须在 audit 内精确匹配 ASCII `DONE`。schema/audit 的框架专项
  必须使用 synthetic neutral exact-count fixture；approved fixture 的 schema 兼容性只在动态发现 case
  的 case-aware 专项中断言，禁止在通用框架文件引用 Release case 名称、路径或其他业务 canary。首个
  StructuredOutput 为 ERROR 时，failed trace 必须用传给 CLI 的同一个冻结 schema 在本地生成 bounded、
  content-free diagnostic；只能保存固定枚举 constraint ID、schema pointer、失败 kind 及必要的
  expected/observed count 或 value kind，禁止保存任何字段值、tool result、raw message、正文、snippet、
  prompt 或模型输出。actions/evidence 必须独立重算并拒绝 pointer、kind、count 或 schema identity tamper；
  该 diagnostic 永远不能成为 PASS，也不得为取得证据而修改 prompt、submission schema、caps 或 retry。
- **修复历史**：2026-08-19 以 `run-20260818T170644Z-aa8e683c` 的权威 verdict、Gate receipt、
  JUnit、model-usage/tool-trace receipt 和 source verification 交叉确认：此前两次 Release 的
  timeout/output-cap 问题已经前进到唯一 Write，但新产物在 char 59227 处语法无效。该 invocation
  `isolated-agent:31104:772d8a92-c8cf-4ff6-a055-44de003ca868` 的 terminal、36-event stream、exact
  10-tool sequence、完整 usage 与 wrapper 均正常，排除 timeout、stream、权限、源码漂移及
  CrossJob 基础设施为本次主因；权威 failure identity 为
  `real.skill-generation / CONTRACT / PYTEST_FAILED`。本轮不得复用该失败或原样重跑。修复后，Skill 与
  real prompt 在读取 checkpoint 04 前把最终根 object 严格序列化一次，对同一字符串执行一次业务中性
  RFC 8259 parse-equivalent grammar pass 并冻结；checkpoint 04 后只允许逐字节相同的唯一 Write。
  tool audit 在确认 Write input 与落盘字节相同后执行 `JSON.parse` 并要求普通 object；失败时 wrapper
  fail closed，并把 content-free 位置/摘要诊断交给严格 collector。模型、caps、工具数量、165 rules、
  九 paths、9.1/9.2 与最终业务 Gate 均未放宽。Dev `run-20260818T174003Z-fa201f83` 随后确认
  `release-inputs.test.mjs` 仍锁定旧 Stage 7 标题，导致 framework.node-tests 在新实现进入确定性轨前
  失败；该静态合同现同步到“freeze valid JSON / write the frozen string”并直接锁住 grammar pass，
  未回退生产 prompt 或删除断言。Dev `run-20260818T174201Z-3103b53d` 又确认 Python 专项把
  Markdown 自动换行当成语义差异；状态机 token 检查现先把空白规范为单空格再匹配完整短语，仍保留
  所有序列化、grammar、冻结与唯一 Write 断言，未改变 Skill 字节或派生 pin。fresh Release
  `run-20260818T175029Z-a7f3b222` 随后在精确源码 `f2ed0d5f.../613` 上确认审计器修复有效：
  tool trace 不再误 PASS，wrapper 以完整 terminal/usage 和 content-free receipt 稳定拒绝 char 59563
  的额外 `}`。但相同模型仍在宣称 grammar pass 后写出无效 JSON，且 pytest 异常旁路保存完整 stdout，
  因而原修复假设被证伪，本条再次回归。当前机械修复已移除 production 自由字符串
  Write：由 Claude CLI 原生 `--json-schema`/StructuredOutput 承担结构校验，仍保持 10 tools 和同一
  caps；wrapper 对唯一 structured root 做 canonical JSON、create-only 原子落盘，真实 Gate 失败只
  输出 bounded JUnit。该选择与直接专项已经实现，但尚未经过新的 Dev 或 fresh Release，不能登记为已验证。
  Dev `run-20260818T183756Z-e715d2a0` 的 framework、contracts、integration 与 SameJob 已通过，unit
  仅有三个静态期望失败；其中本条两个 Skill 专项分别把 Markdown 换行后的 `最终 根 object`
  与复合句中的 `or turn it into a JSON string` 错当成缺少完整对象/禁字符串语义。回归原因是专项
  继续要求不稳定的排版邻接，而生产 Skill/checkpoint 的完整语义仍存在；现分别收敛到稳定子句
  `根 object 已完整` 与 `or turn it into a JSON string`，不删除 StructuredOutput、10-tool、canonical、
  create-only 或 bounded evidence 的任何断言。下一轮 Dev `run-20260818T184759Z-75d3d5fb` 在源码
  `git-visible-worktree-v1:78607814bca8f92c5353a604da673a6dde7b7e0c308bcb482cccd7661560cd67`
  （613 files）把 unit 收敛到唯一失败：同一专项又把 Skill 中跨行的 `不得\n调用 Write/Edit/Bash`
  规范化成 `不得 调用` 后，仍要求无空格邻接。现用 `不得\s*调用` 正则直接锁定否定词与三个禁用
  工具，同时允许 Markdown 排版空白；其余 1,787 个已执行 unit、565 contracts、45 integration、
  4 SameJob 与全部 framework 均已通过。第三轮 Dev `run-20260818T185131Z-985e8175` 在源码
  `git-visible-worktree-v1:d4809de7cc5058dd5fafb6d87366f9a958ed07ec8e0f6a61716b2cd38887b7c2`
  （613 files）再次只暴露同一测试块的一个排版邻接：Skill 将 `完整语义及限定` 跨行写成
  `完整语义及\n限定`，而测试仍对整组语义 token 使用 raw substring。该连续证据确认问题是整组
  Markdown 合同断言的系统性 whitespace 敏感，而不是逐个业务短语缺失；现对该组 document/token
  双方仅移除 layout whitespace 后比较，仍要求每个 Unicode/英文/代码 token 的非空白字符序列完整
  存在，未放宽任何业务语义或提交边界。fresh Release
  `run-20260818T190152Z-30a86d3d` 在源码
  `git-visible-worktree-v1:affbae0379b0b2bd97b55e81fecf09209865c1520e515f66b4b40dc8c0bcda1b`
  （613 files）确认 StructuredOutput 候选仍未闭合：真实 invocation 在 930.785 秒以
  `error_max_turns` 结束，13 turns 超过冻结 12；usage 为 924,029 total tokens、$5.088489，未超时且
  未超过其他冻结 cap。wrapper 正确只向 pytest 暴露 31-byte
  `WRAPPER_MODEL_TERMINAL_INVALID`，没有泄漏 raw stdout/stderr，但 invocation receipt 的
  `tool_trace_audit=null`，无法确认 content-free 的实际阶段进度；没有 StructuredOutput seal 或
  `scenario-evaluation-audit.json`，六个 CrossJob Stage 全部未运行。该证据把“失败内容最小披露”与
  “失败阶段可审计”收敛在本条同一历史中，不另建割裂 issue。最小修复为删除
  post-StructuredOutput minimal completion，把 StructuredOutput 本身作为终止动作，并为非 PASS
  terminal 增加严格 validator 约束的 content-free partial failure trace；caps、8 Reads、10 tools、
  165 rules、九 paths 与全部业务 oracle 均不变。本轮静态安全审查继续确认：若 wrapper 原样保留
  不匹配的 `init.model` 或畸形 terminal、用 `Number(...)` 接受字符串/布尔值/数组 usage、以 replacement
  character 容忍无效 UTF-8，或允许 late init，bounded receipt 仍可能被伪装成可信证据；evidence
  PASS 若不独立重审 stream/process，也可能沿用被篡改的局部 PASS。现将 model 限为匹配时回写冻结值、
  否则 `null`，terminal 只保留安全字段，raw usage/cost 必须原生 numeric，UTF-8 改为 fatal，统一强制
  init 首/result 末；partial audit 对 unsafe trace fail closed 为 `null`，成功 evidence 则重放完整状态机。
  这些修复只改变失败可观测性与证据验证，不保存 raw stream，也不放宽生产 PASS。
  fresh Release `run-20260818T195757Z-eea3189d` 随后在源码
  `git-visible-worktree-v1:a171b8b87595bda183ac4cefa8931bff2dd877c7bd63bf27312f918e694cce4e`
  （613 files）确认 partial/content-free 修复真实生效：receipt 无正文地封存了前九项正确工具以及
  五次 StructuredOutput SUCCESS 的 size/SHA-256，terminal 精确为
  `error_max_structured_output_retries`。但该 invocation 达到 16 turns、1,319,095 total tokens，分别
  超过冻结 12 与 1m；wrapper 仍以 `WRAPPER_MODEL_TERMINAL_INVALID` 而非 cap-exceeded 分类。没有
  canonical output、scenario audit，六个 CrossJob Stage 均未运行，最终 verdict 仍为
  `FAIL / CONTRACT / PYTEST_FAILED`。本轮确认根因是过宽 submission schema、未绑定 second submit
  与错误的完成协议，而不是需要提高 cap。修复冻结完整 schema/计数/根字段，仅允许
  wiki+clarifications 唯一 batch；唯一 StructuredOutput 后精确 `DONE`，第二次 submit 立即 bound；
  turns/total-token cap exceeded 优先分类，同时保持全部现有 caps 与业务 oracle。Dev
  `run-20260818T205838Z-9756b05e` 随后在源码
  `git-visible-worktree-v1:038a91432bc7ebfb6fee239b446dca2db6ed87fa5724021e8e5e96a264178fd5`
  （613 files）以 `FAIL / HARNESS / NODE_TEST_FAILED` 结束；verification/operation PASS，Node 汇总
  267 pass / 2 fail / 3 skip，后续 repository/affected/full 均 NOT_RUN，模型 usage 为零。失败一来自
  PASS trace 的 attempt policy 仍保存 `terminal_result:"DONE"`，而 content-free 断言把该安全策略
  常量与模型实际完成正文混为一谈；失败二来自 submission-schema 专项直接引用
  `rpc-timeout-anonymized`，被 case canary 隔离检查正确拒绝。本轮保留冻结 policy 常量并把专项收敛为
  “terminal receipt 无 `result`”；通用 schema 测试改用 synthetic neutral exact-count fixture，实际
  approved fixture 则由动态 case-aware 专项验证，不放宽 content-free 或 canary 规则。Dev
  `run-20260818T210520Z-63bccb96` 随后在源码
  `git-visible-worktree-v1:e92a476a98ec17641b6edca552a4ea30b9bed7134aa161cd33b15de79bdf9817`
  （613 files）以 `FAIL / PRODUCT / PYTEST_FAILED` 结束；source verification 与 operation 均 PASS，
  framework.self-test、repository.static 均 PASS，deterministic.affected 为 NOT_REQUIRED，contracts 565、
  integration 45 与 SameJob 4 个用例全部 PASS，unit 为 1,789 pass / 1 fail / 66 skip，模型调用与 usage
  全为零。唯一失败位于 `test_skill_contract.py:498`：专项正则没有容忍 Skill 在 `Write`、之后的
  Markdown 换行，而生产 Skill 仍完整禁止 `Write`、`Edit`、`Bash` 三个工具。现仅把该断言改为
  ``r"不得\s*调用\s*`Write`、\s*`Edit`、\s*`Bash`"``，不改 Skill、source-copy、工具禁令或其他业务语义；
  该失败 Dev 与断言修复均不能替代最终 fresh Release 验证。Dev
  `run-20260818T211156Z-a80c2c96` 随后在源码
  `git-visible-worktree-v1:a8c23f519149e4e6c6d4658bb36440c14b31ba91c74f960f16e6a4c2a0f9d06e`
  （613 files）仍以 `FAIL / PRODUCT / PYTEST_FAILED` 结束；source verification 与 operation 均 PASS，
  framework.self-test、repository.static 为 REUSED PASS，deterministic.affected 为 NOT_REQUIRED，
  contracts 565、integration 45 与 SameJob 4 个用例全部 PASS，unit 为 1,789 pass / 1 fail / 66 skip，
  模型调用与 usage 全为零。唯一失败仍在同一 `test_wiki_conversion_contract_is_self_contained_and_business_neutral`
  测试：它先正向要求“冻结 workflow schema”，随后又对整个 bounded state machine 全局禁止“冻结”，
  因断言顺序而在前次 regex 修复后才暴露这一自相矛盾。现只禁止退役 manual-string 协议短语
  `parse-equivalent`、`grammar pass`、`冻结字符串`、`frozen string`、`byte-for-byte`，并继续正向要求
  `冻结 workflow schema`；不改 Skill、submission schema 或运行时语义，状态仍为 pending。fresh Release
  `run-20260818T212706Z-38373f8a` 随后在源码
  `git-visible-worktree-v1:be28f7f23a3277fe0ac6c857282bfe137385869c819307de0d2f556a6f11451c`
  （613 files）因 `real.skill-generation` watchdog timeout 失败；该 terminal-less 失败属于
  `PL-FIX-015` 的证据回归，既没有 canonical output/scenario audit，也没有任何 CrossJob 执行，因而
  不能证明本条 StructuredOutput、bounded evidence 或业务 oracle 已闭合。当前不据此修改 prompt、
  submission schema 或 caps，本条仍待最终 fresh Release 验证。fresh Release
  `run-20260818T223051Z-3cb5cc2a` 随后在源码
  `git-visible-worktree-v1:461b7665070b59c6996328bf93ad4e8ce6a2ca9105c3f84a33794b67ed84847e`
  （613 files）以 `FAIL / CONTRACT / PYTEST_FAILED` 结束；source verification、operation、framework、
  repository、deterministic.full 及两个 platform stages 均 PASS，affected 为 NOT_REQUIRED。唯一
  `deepseek-v4-flash[1m]` invocation 用时 869.18 秒，完整 stream 为 36 events；ordinal 0–8 的 Skill 与
  八个 ordered Reads 均 SUCCESS，ordinal 9 StructuredOutput 为 ERROR（143,944 bytes，SHA-256
  `8ba0e2170b2f9414c4d3703e1d3a195e8697dba1baddc4a7d0b24cb9d8f6cc64`），ordinal 10
  StructuredOutput 为 SUCCESS（143,924 bytes，SHA-256
  `7816a172f8ae71395bff172c85b358a4066162a41b7b92611898962d9eb84123`）。terminal 为
  `error_max_structured_output_retries`，13 turns 超过冻结 12，wrapper 正确分类
  `WRAPPER_MODEL_CAP_EXCEEDED`；Skill invocation usage 为 92,901 input、181,705 output、604,928 cache
  read、879,534 total tokens、$5.309594，总 run 为 879,580 tokens、$5.309844。没有 canonical output
  seal 或 scenario audit，六个 CrossJob stages 均未运行。该 evidence 已证明首个 schema-bound submit
  被拒及第二次提交不可交付，但现有 content-free trace 未保留首错的具体 schema constraint；因此本轮
  只补同源本地 schema diagnostic，不改 prompt、submission schema、caps 或 retry，取得约束级证据前
  不猜测业务修复，也不把第二个 tool SUCCESS 当作可交付输出。fresh Release
  `run-20260818T231502Z-aa07942c` 随后在源码
  `git-visible-worktree-v1:a0702712091c1a8e9cd6e9bebf8a291e29e04088a0f769228007a642e76073fc`
  （613 files）以 terminal-less watchdog timeout 失败；`tool_trace_audit=null`，没有可审计的
  StructuredOutput ERROR record，所以固定 catalog diagnostic 没有触发，权威 evidence 中既没有
  diagnostic 对象也没有 violations 数组。该结果不能验证 catalog 的真实 Release 行为，也不能据此
  修改 prompt、submission schema、caps 或 retry；prefix audit 的 fixed-code 可观测性缺口归入
  `PL-FIX-015`，本条仍待后续实际 rejected StructuredOutput evidence。fresh Release
  `run-20260819T003721Z-b8cad111` 随后在源码
  `git-visible-worktree-v1:be1b77dd73a1340bf0cf732df4c430816be058bedbed0f13899c4de5d5cfb5dd`
  （613 files）取得了所需约束级证据，但仍以 `FAIL / CONTRACT / PYTEST_FAILED` 结束；plan-only 为
  `ADMITTED` 且 blockers/warnings 均为空，GENESIS、`EMPTY_REQUIRED` 与 checkpoint reuse `FORBIDDEN`
  lineage 未变。materialized 与
  worktree source verification、operation、framework、repository.static、deterministic.full、host 与
  Linux platform 均 PASS，affected 为 NOT_REQUIRED。repository.static 复用同快照 Dev
  `run-20260819T002822Z-0b6b10fc` 且 current re-audit PASS，deterministic.full 复用同快照 Dev
  `run-20260819T003302Z-5e51341f` 且 current re-audit PASS。唯一 real Skill Gate 用时 1440.483 秒，
  JUnit 唯一用例以 `BACKEND_EXIT_FAILED` 失败；stdout 为 0 bytes，stderr 仅保留 27-byte bounded code。
  唯一 `deepseek-v4-flash[1m]` invocation 为完整 36-event stream、init 1/result 1/last `result`，
  `timed_out=false`，terminal=`error_max_structured_output_retries`，wrapper 正确分类
  `WRAPPER_MODEL_CAP_EXCEEDED`。schema-v6 partial trace code 为 `SKILL_TRACE_RESULT_NOT_SUCCESS`，
  因为不是 terminal-less rejection，所以没有 `audit_code`；ordinal 0–8 的 Skill 与八个 ordered Reads
  全部 SUCCESS。ordinal 9 首次 StructuredOutput 为 ERROR（3 bytes，SHA-256
  `ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356`），固定 catalog 精确列出
  20 个根 required 字段全部 missing；ordinal 10 再次 ERROR（144,173 bytes，SHA-256
  `5d6af4a87267c3ea8b626bde16d5ebad1b356d751d9acc74d411345e16e855cc`），唯一 violation 为
  `ROOT_MAX_ITEMS_OUTPUT_REQUIREMENTS`，`#/properties/output_requirements/maxItems` 期望 5、实际 6。
  invocation usage 为 50,598 input、251,273 output、642,432 cache read、944,303 total tokens、
  `$6.856031`；13 turns 超过冻结 12，但 total token、cost 与 1800 秒 watchdog 未超，run 总 usage 为
  944,349 tokens、`$6.856281`。没有 canonical output 或 `scenario-evaluation-audit.json`；冻结 case
  digest `042e6c5676ad88d83a2a5bcc6f43f254241362afff71c10a3ff824fa9743e99a` 声明的九个 scenarios
  均未评估，六个 CrossJob stages 全部 `NOT_RUN / PRIOR_STAGE_NOT_PASSING / NOT_EXECUTED`。
  event/receipt/waterfall audit、payload seal 与两层 secret scan 均 PASS，`evidence_reusable=true` 只表示
  失败证据可验证，不表示交付通过。

  该证据把最小修复限定为 first-submit/count-vector，不改变模型、schema、retry limit 或任何 cap：通用
  Skill/checkpoint 明确 StructuredOutput 不是 discovery/validation probe，禁止首个 `{}`、partial、trial
  或 probe input；首个调用必须已经是唯一完整 schema-valid submission，ERROR 后立即停止且不得第二次
  submit。真实 Stage 6 在读取 checkpoint 04 前应用 initiating prompt 与 provider 暴露的同源 schema，
  核对 20 个 required 根字段，以及 roles=2、requirements=5、anchors=2、policies=2、extractors=10、
  rules=165、paths=9 和五组文本数组 4/5/6/5/3；`output_requirements` 必须恰好 5 项，若源支持语义多于
  五项，只能合并兼容语义且不得丢失 condition、scope、limitation、warning 或 risk consequence。Dev
  `run-20260819T011622Z-572a42d7` 随后在源码
  `git-visible-worktree-v1:b3f2f642ac26f0a31f301acfcdb890eb7500f1e0fdeace5504cea04396fe4287`
  （613 files）以 `FAIL / PRODUCT / PYTEST_FAILED` 结束；source verification 与 operation PASS，
  framework.self-test、repository.static、contracts 565/565、integration 45/45 与 SameJob 4/4 均 PASS，
  unit 为 1,788 pass / 3 fail / 66 skip，模型调用与 usage 全为零。第一项失败是 first-submit 修复改变
  Skill/checkpoint 字节后 source-copy receipt 未刷新，已归入 `PL-FIX-011`。本条两项直接回归分别为：
  `test_wiki_conversion_checkpoints_are_control_only_and_forward_only` 对 checkpoint 04 的精确短语
  `never call StructuredOutput a second time` 使用 raw substring，而 Markdown 换行把该短语拆开；
  `test_release_first_structured_output_cannot_be_empty_probe_or_six_item_retry` 又把仅在前一测试局部定义的
  `compact_skill` 误用于新测试，触发 `NameError`。现恢复 checkpoint 中同一行的精确短语，并把
  `compact_skill = re.sub(r"\s+", "", skill)` 只定义在新测试自己的作用域；first-submit/probe 禁令、
  20-root required、完整 count vector、Schema、Skill、prompt、模型与 caps 均未放宽。该 Dev 与两项测试
  修复仍不能替代最终 fresh Release，本条保持 pending。下一轮 Dev
  `run-20260819T012656Z-21f3720b` 在源码
  `git-visible-worktree-v1:c14eb5faba6e4d6c451270ce72b2a65b68a4c61781a399cf307457d77f515822`
  （613 files）取得 `PASS_WITH_WARNINGS`；双重 source verification、operation、framework、repository、
  contracts 565/565、unit 1,791 pass / 66 skip / 1,857、integration 45/45 与 SameJob 4/4 均 PASS，
  affected 为 NOT_REQUIRED，模型调用与 usage 全为零，唯一非 PASS 维度是
  `performance_status=NOT_CALIBRATED`。这直接验证 checkpoint 精确短语、`compact_skill` 局部作用域和
  `test_release_first_structured_output_cannot_be_empty_probe_or_six_item_retry` 所锁定的 first-submit/
  count-vector 静态合同；因未调用真实模型、未执行 scenario/CrossJob，仍不能替代 fresh Release。
  fresh Release `run-20260819T013351Z-b1da9580` 随后在源码
  `git-visible-worktree-v1:128b087150570f9e412e6a35e2985c0873565c0a9bb18131b1b638877665619d`
  （613 files）于 StructuredOutput 前失败：Skill 与前四个 Read SUCCESS 后没有进入 checkpoint 01，
  terminal 为 `success/is_error=true`，v6 trace、usage 与 wrapper 状态完整且无内容泄漏。该失败没有产生
  StructuredOutput constraint，也没有证伪 first-submit/count-vector 修复；其有界 Stage 3 修复与直接测试
  归入 `PL-FIX-014`，本条只交叉记录，继续保持 pending。
  Dev `run-20260819T022003Z-9b64b945` 在源码
  `git-visible-worktree-v1:972c599c6267a26ff2726dfc222473a4681e83dfbaa3760ab82d40b609280519`
  （613 files）再次验证 first-submit/count-vector 的静态与 deterministic 合同；后续 Release
  `run-20260819T022320Z-0deb10f2` 在 verification reference 与 checkpoint 01 之间超时，仍未触及
  StructuredOutput，因而同样没有证伪本条修复。该失败的精确 v6 phase-sequence 分类归
  `PL-FIX-014`，本条不扩 schema、receipt、prompt 范围或 caps。
  Release `run-20260819T031213Z-53c83c3f` 已成功读取 checkpoint 01，但在 checkpoint 02 前以完整
  non-PASS terminal 停止，仍没有 StructuredOutput 或 schema diagnostic；它同样没有证伪本条
  first-submit/count-vector 修复。现有 checkpoint 拆分归 `PL-FIX-014`，本条继续保持 pending。
  Dev `run-20260819T035115Z-81a92961` 在源码
  `git-visible-worktree-v1:d08917919b8e117b3fcdf5f5e9feeaf51d1291555fda54a74d7719a3ca1d40cd`
  （613 files）通过后，fresh Release `run-20260819T035423Z-8d271214` 首次完整跨过四个 checkpoint，
  但 ordinal 9 的 7,972-byte StructuredOutput 缺整个 `verification_contract`，ordinal 10 的
  34,815-byte StructuredOutput 仍缺 `rules` 与 `terminal_paths`；两次均由同源 schema-v6 catalog
  以 `ROOT_REQUIRED_VERIFICATION_CONTRACT`、`VERIFICATION_REQUIRED_RULES`、
  `VERIFICATION_REQUIRED_TERMINAL_PATHS` 精确拒绝。terminal 为
  `error_max_structured_output_retries`，14 turns 超过冻结 12，wrapper 正确分类
  `WRAPPER_MODEL_CAP_EXCEEDED`；invocation 为 37,650 input、171,315 output、748,160 cache read、
  957,125 total tokens、`$4.845205`，未超 token/cost/time caps。最小修复只把该已知 key/count vector
  放到最后 checkpoint 与 Stage 7 的实际 tool-input 边界：根层必须含 `verification_contract`，其
  `observation_policies`/`event_extractors`/`rules`/`terminal_paths` 必须同时存在且数量为
  2/10/165/9；prefix-only、core-only 或缺 rules/paths 的对象不得提交，ERROR 后仍不得重试。schema、
  retry、validation v6、模型与 caps 均不变。
  Dev `run-20260819T042115Z-ee94a644` 在源码
  `git-visible-worktree-v1:8cf81afd9419d52a8c15525026cc0c160accb5ef8185d7a8d516271def9f3f71`
  （613 files）通过后，fresh Release `run-20260819T042443Z-f79c4c06` 再次完整跨过四个 checkpoint，
  但 ordinal 9 与 10 的 StructuredOutput 均为精确 3-byte `{}`（SHA-256
  `ca3d163bab055381827226140568f3bef7eaac187cebd76878e0b63e9e442356`），v6 catalog 对两次都列出
  全部 20 个 root required missing。invocation 为 60,532 input、239,165 output、729,600 cache read、
  1,029,297 total tokens、`$6.646585`、14 turns；total token 与 turns 超过冻结 1m/12，wrapper 正确分类
  `WRAPPER_MODEL_CAP_EXCEEDED`。直接证据表明，在各阶段先完整展开 165 rules、最终又要求提交同一完整
  对象会重复物化并耗尽上下文，最后退化为空 probe；继续追加 key 列表已被证伪。最小修复因此只改变
  生成策略：Stage 2/4/5 记录紧凑 materialization blueprint，Stage 6 对 blueprint 各执行一次 9.1/9.2，
  此前不展开或序列化完整 rule objects；checkpoint 04 后才在唯一 StructuredOutput tool input 内第一次且
  唯一一次机械物化全部 20 根字段、165 rules 与九 paths。业务语义、schema、validation v6、工具序列、
  retry、模型和所有 caps 不变。相关直接专项同步从退役的“预先构造完整对象”短语收敛到当前 blueprint/
  single-materialization 合同，并对 checkpoint Markdown 使用布局无关比较；测试修正不改变生产语义。
  Dev `run-20260819T050613Z-2036e1d9` 在源码
  `git-visible-worktree-v1:e3ae632d3c836fce173f2605846d514acf39eca81b8b0395f5c9af71772c0832`
  （613 files）通过后，fresh Release `run-20260819T051117Z-3ac62051` 再次完整跨过四个 checkpoint。
  ordinal 9 仍错误提交 3-byte `{}` 并被 20 个 root-required constraints 拒绝；但 ordinal 10 已成功提交
  152,226-byte schema-valid StructuredOutput（SHA-256
  `8f18745508cc9dfa77825d0f233586b1596c59d861f22d65cde4f1d6ccb95b93`）。terminal 仅因第二次提交为
  `error_max_structured_output_retries`，14 turns 超过冻结 12，wrapper 正确分类
  `WRAPPER_MODEL_CAP_EXCEEDED`；invocation 为 55,360 input、232,897 output、594,176 cache read、
  882,433 total tokens、`$6.396313`，token/cost/time 均在 cap 内。该证据证明 compact blueprint 与完整
  物化已经可行，剩余唯一偏差是模型把 StructuredOutput 误当“先打开空容器、再填充”的交互步骤。
  最小修复明确要求同一 assistant response 在发出 tool call 前先完整组装 arguments；空 `{}` 调用已经
  是失败提交且无法由后续 call 补齐，从而把已成功的第二份对象前移为唯一 ordinal 9。schema、retry、
  validation v6、模型与 caps 均不变。
  Dev `run-20260819T054114Z-b68e4f74` 在源码
  `git-visible-worktree-v1:737e7028063c6517c90c9deef8320d2831136c8ef895945339fa89f05244126b`
  （613 files）通过后，fresh Release `run-20260819T054518Z-57092337` 的 ordinal 9 仍为 3-byte 空
  submission；ordinal 10 已前进为 3,827 bytes，但 v6 精确指出其缺 `verification_contract`、
  `time_characteristics`、`analysis_steps`、`judgement_rules`、`output_requirements`、`assumptions`、
  `requires_logparse` 七个根字段。invocation 为 74,260 input、133,134 output、622,592 cache read、
  829,986 total tokens、`$4.010946`、14 turns；仅 turns 超过冻结 12，wrapper 正确分类 cap exceeded。
  继续负向重复空对象示例未阻止 probe，且 checkpoint 的“其他 required”不足以让第二次提交闭合。
  最小修复改为纯正向、就近的有序 20-root-key 清单与 `root-key count=20` 前置条件，并从生产 Skill、
  checkpoint 与 Stage 7 移除空对象字面示例，避免提示复现该 probe；verification 四键与 2/10/165/9
  计数仍紧随其后。schema、retry、validation v6、模型与 caps 均不变。
  Dev `run-20260819T061449Z-9070b60f` 在源码
  `git-visible-worktree-v1:4d76671b4083a8cd226d88eb8c79b1f023b4d27fe5429ff235b425547ddb4d7c`
  （613 files）通过后，fresh Release `run-20260819T061847Z-116675e2` 的 ordinal 9 仍为 3-byte
  zero-property submission，但 ordinal 10 已再次生成 schema-valid 162,200-byte StructuredOutput
  （SHA-256 `9ec3994ab82a4be62e26fbe19497cf35c330a10a5ece5dca3ccab576164bb505`）。invocation 为
  63,465 input、190,073 output、640,640 cache read、894,178 total tokens、`$5.389470`、14 turns；
  仅 turns 超过冻结 12。正向 key list 仍未替代模型从真实 schema error 学习参数 shape 的行为，而第二份
  完整内容已连续两次证明可生成。最小修复因此在 checkpoint 04 增加业务中性的 provider-equivalent
  typed argument frame；大写占位符仅为元语法，必须在 tool-use block 发出前由 blueprint 全部实例化，
  不得原样进入参数。它预先提供此前由 schema error 才暴露的根/nested shape，不改变 schema、retry、
  validation v6、模型、工具序列或 caps。
  生产 IR 校准 `run-20260819T152518Z-c7b17b9a` 在精确源码
  `git-visible-worktree-v1:4edff64944974a91017c648ae1fe458391ed5f2bb8b239563af86b7f0f4515a0`
  （618 files）上首次完成 compact Blueprint 的唯一 StructuredOutput 与成功 terminal，但 wrapper 只
  封存 `SKILL_TRACE_RULE_IR_INVALID`：12 turns，41,228 input、152,060 output、603,136 cache read、
  796,424 total tokens、`$4.309208`，未超 token/cost/time caps；deterministic.full 全 PASS，
  semantic audit 与九个 scenarios 因 compiler fail closed 尚未开始。当前 adapter 把 IR size、compiler
  exact-key/relationship、deep validator 与 envelope 异常全部折叠成同一常量 stderr，wrapper 又丢弃
  compiler stderr，现有 receipt 因而没有 constraint、phase 或 IR seal，无法从权威证据选择最小修复。
  该“已分类但不可定位”缺口现以最小 schema-v8 诊断闭合：Python adapter 只输出固定 allowlist 的
  `phase/constraint_id`，wrapper 只追加 canonical IR 的 size/SHA-256；禁止保存 exception message、字段
  值、path、prompt、tool input、模型正文或 raw stream。未知或畸形 diagnostic 仍降为通用 audit failure，
  绝不能进入 PASS receipt；模型、Blueprint/GenerationSpec schema、compiler 行为、工具序列、retry、
  semantic/scenario Gate 与所有 caps 均未放宽。
  零模型 Dev `run-20260819T155600Z-c42da399` 随即确认 compiler subprocess 曾在受测
  `runtime-support` 目录生成 ignored `__pycache__`，使 framework 的 active-runtime-support exact-list
  专项在进入 deterministic 轨前 FAIL；这不是诊断 schema 或业务 compiler 失败。根因是 wrapper 只设置
  `PYTHONNOUSERSITE`，没有禁止 validator import 写 bytecode。现对该受信 subprocess 固定
  `PYTHONDONTWRITEBYTECODE=1`，并由同一 compiler-rejection wrapper 专项直接核对环境值；既有缓存仅作
  非证据、ignored Python 派生产物精确清理，不触碰 Test Flow evidence、tracked 字节或外部 checkout。
  下一轮零模型 Dev `run-20260819T160359Z-7a0a3130` 已使 framework、repository static、contracts、
  integration 与 SameJob 全 PASS；unit 唯一失败是 adapter 既有专项仍用旧自由文本 `canonical` 匹配
  新固定枚举异常。该测试现精确要求 `IR_CANONICAL_BOUNDED`，不改 adapter 分类、compiler 行为或
  diagnostic disclosure 边界。
- **专项回归测试**：
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_wiki_conversion_contract_is_self_contained_and_business_neutral`
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_wiki_conversion_checkpoints_are_control_only_and_forward_only`
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_real_wiki_gate_allows_only_inputs_and_declared_skill_references`
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_real_wiki_gate_failure_report_is_bounded_and_content_free`
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_structured_output_success_requires_exact_done_terminal_response`
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_release_first_structured_output_cannot_be_empty_probe_or_six_item_retry`
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_release_verification_core_reaches_checkpoint_before_any_rule_construction`
  - `tests/deterministic/unit/integrations/test_skill_contract.py::test_release_conversion_keeps_declared_authoritative_matrices_complete`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `audits one schema-bound StructuredOutput submission and seals only a v6 PASS receipt`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `seals an unsuccessful terminal as a content-free partial trace that can never be a public PASS`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `rejects Write, non-object StructuredOutput or terminal mismatch without creating output`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `phase checkpoint permissions grant six exact Skill reads and no optional or mutation path`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `rejects missing reordered partial batched or retried production phase checkpoints`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `GenerationSpec diagnostics identify every frozen constraint family with catalog metadata`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `GenerationSpec diagnostics are deterministic, bounded, and never echo dynamic names or values`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `the GenerationSpec diagnostic validator rejects extensions, forged catalog data, disorder, overflow, and stripping`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `failed rule IR diagnostics retain only a fixed constraint and input seal`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `StructuredOutput is canonicalized and atomically materialized with sealed CLI arguments`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `a compiler rejection seals only its fixed rule IR constraint and input digest`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `the child-only retry limit is two and a second StructuredOutput remains a failed partial trace`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `error_max_turns preserves a content-free partial trace without materializing StructuredOutput`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `an unsafe unsuccessful trace fails closed to null without exposing the raw path`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `mismatched model and malformed terminal fields never enter the receipt`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `raw usage and cost reject string boolean and array coercions`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `invalid UTF-8 makes the whole stream invalid without retaining decoded replacements`
  - `tools/test-flow/tests/isolated-agent-wrapper.test.mjs` 中 `the wrapper requires init first even for a non-Skill workflow`
  - `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs` 中 `partial audit rejects a pre-init tool, late init, or post-result event`
  - `tools/test-flow/tests/actions.test.mjs` 中 `failed isolated invocation usage is collected as evidence without converting the Gate to PASS`
  - `tools/test-flow/tests/evidence.test.mjs` 中 `a failed planned invocation requires a sealed audit for a parse-complete terminal-less Skill timeout`
  - `tools/test-flow/tests/evidence.test.mjs` 中 `a passing skill-generation invocation must retain a valid trace audit receipt`（含 stream/process/terminal/raw-field tamper）
  - `tools/test-flow/tests/release-inputs.test.mjs` 中 `Skill generation uses one structured output and wrapper-owned canonical materialization`
  - `tests/deterministic/unit/prototype/test_generation_blueprint_compiler.py::test_compiler_adapter_emits_only_a_fixed_content_free_constraint`
  - `tests/real/agent/test_real_wiki_skill_generation_gate.py::test_real_conversion_agent_builds_an_executable_reviewed_skill_from_plain_wiki`
- **最新 Test Flow verdict**：pending，待上述实现冻结后的最终 fresh Release。失败 Release
  `run-20260818T190152Z-30a86d3d`、`run-20260818T195757Z-eea3189d`、
  `run-20260818T212706Z-38373f8a`、`run-20260818T223051Z-3cb5cc2a`、
  `run-20260818T231502Z-aa07942c`、`run-20260819T003721Z-b8cad111`、
  `run-20260819T013351Z-b1da9580`、`run-20260819T152518Z-c7b17b9a` 与 Dev
  `run-20260818T205838Z-9756b05e`、`run-20260818T210520Z-63bccb96`、
  `run-20260818T211156Z-a80c2c96`、`run-20260819T011622Z-572a42d7`、
  `run-20260819T012656Z-21f3720b`、`run-20260819T013207Z-efca72b7` 均保留为权威历史。最新 Test Flow
  Dev `run-20260819T061449Z-9070b60f` 在源码
  `4d76671b4083a8cd226d88eb8c79b1f023b4d27fe5429ff235b425547ddb4d7c`（613 files）为
  `PASS_WITH_WARNINGS`，只验证静态/确定性合同；最新 Release
  `run-20260819T061847Z-116675e2` 在相同源码为 `FAIL / CONTRACT / PYTEST_FAILED`。两者均不构成
  最终已修复、已验证或可交付结论。
