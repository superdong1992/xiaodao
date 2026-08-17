# 已修复问题台账

更新时间：2026-08-17

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
