# S05：Scheduler、Worker 与恢复实施说明书

- 状态：V1 独立开发合同
- 未来 Codex 开发任务模型：`gpt-5.6-sol`，`reasoning_effort=ultra`
- 公共合同：[S00-contract-freeze.md](S00-contract-freeze.md)
- 组合验收：[../v1-composition-spec.md](../v1-composition-spec.md)

## 1. 目标

本切片实现单进程内的 Job 分发、条件认领、类型化 Worker、取消传播和重启恢复协调：

1. 只分发已经由 Application Service 持久化的 `job_id`。
2. 以条件操作保证同一 `job_id` 只有一次 `PENDING → RUNNING` 认领成功。
3. 以服务级并发 `1` 执行 ROUTE、DIAGNOSE、REVIEW 三类 Job。
4. 为每次服务启动生成新的 `runtime_epoch`，把当前代次写入成功认领的 Job。
5. 将已认领 Job 交给 S04 的 Runtime Port，并将结果交回 Application Service 的 Outcome 入口。
6. 传播取消、超时和关闭信号，等待 S04 终止完整子进程树。
7. 启动时先重放旧代次 RUNNING Job 已 finalized 且未处理的 Outcome，再把确实没有可重放 Outcome 的旧执行转为 `INTERRUPTED`，最后重投仍为 `PENDING` 的同一 Job。
8. 维持 Case 单活跃 Job、Review 门禁、迟到 Outcome 隔离和显式 Resume 语义。

## 2. 非目标

- 不直接修改 Case、DiagnosisState、Job 或 JobOutcome；所有业务写入通过 S00 的 Application Service Port。
- 不实现状态机或 TransitionPlan；只执行 S00/S01 已决定的状态操作。
- 不创建 PENDING 业务 Job，也不为失败 Job私自重试或生成 replacement Job。
- 不构建 Prompt、不物化附件、不启动 Agent 子进程；这些属于 S04。
- 不实现外部消息队列、Outbox、多进程 Worker、多实例选主、高可用或 JobAttempt。
- 不自动恢复已经 `INTERRUPTED` 的 Job；替代 Job 只能由显式 `ResumeCase` 经 Application Service 和 Coordinator 创建。
- 不将 REVIEW 中断后的任务降级成 DIAGNOSE。
- 不把 `STALE` 作为 JobStatus。

## 3. 上游合同

唯一规范上游是 S00。必须直接使用 S00 定义的：

- JobType、JobStatus、CaseStatus、Outcome disposition 与运行代次类型；
- Dispatcher、Runtime、StateRepository 等公共 Port；
- 认领、完成、失败、中断、取消、重启协调和 Resume 的命令/回执 DTO；
- ExecutionFailure 分类和状态转换所需 Trigger；
- `replacement_for_job_id`、`active_job_id`、`base_state_revision` 与条件修订规则。

Job 认领和 Outcome 提交必须通过 S03 的单写入口完成；S05 只传递 S00 命令与回执，不新增跨切片业务 DTO。S04 的具体实现不是本切片单元测试前置条件；测试使用 S00 `ScriptedRuntime` 或本地 Runtime Fixture。真实 Runtime 和 S03 单写入口由 S08 注入。

## 4. 唯一文件责任区

本切片是以下路径的唯一所有者：

```text
src/problem_locator/dispatch/**
tests/unit/dispatch/**
tests/fixtures/components/dispatch-*/**
handoff/S05.json
```

建议内部布局：

```text
src/problem_locator/dispatch/
├── dispatcher.py
├── worker.py
├── runtime_epoch.py
├── recovery.py
├── cancellation.py
└── service.py
```

组件 Fixture 只能放在 `tests/fixtures/components/dispatch-*/**`；glob 使用 `v1-specs/README.md` 的仓库相对 POSIX 语义。不得新增仓库级 `tests/conftest.py`。

## 5. 禁止修改项

- 不得修改 `src/problem_locator/contracts/**`、`src/problem_locator/runtime/**`、其他 S01～S08 责任区、`pyproject.toml` 或锁文件。
- 不得绕过 Application Service 或 StateRepository 的条件 API 写 `state.json`。
- 不得在内存队列中创造第二份业务 Job 真相；队列项只能保存已持久化的 `job_id`。
- 不得把重复 dispatch 转化为第二个 Job、第二次认领或 JobAttempt。
- 不得自动合并迟到 Outcome，不得把 `STALE` 加入 JobStatus。
- 不得把 `PENDING` 重投实现为新 `job_id`；重投必须保持原 Job 与原快照。
- 不得把 `INTERRUPTED` 恢复实现为原 `job_id` 重跑；Resume 必须创建同阶段 replacement Job。
- 不得在 REVIEW 恢复时改变 job type。
- 不得增加 Agent 执行、业务失败或 replacement 的自动重试；唯一例外是对同一 finalized `RuntimeExecutionReceipt` 的 submission-only 投递重放，使用第 6.3/6.5 节冻结的可取消退避且绝不再次调用 Runtime。
- 不得越界修改 S04 来适配本切片；使用第 14 节合同变更流程。

## 6. 输入输出契约

### 6.1 Dispatcher

输入是 S00 定义的已持久化 `job_id`。Dispatcher 不接受未持久化 Job DTO，也不接受任意 Prompt。

行为：

- `submit(job_id)` 幂等；同一个 ID 重复提交最多保留一个待执行信号。
- 分发成功不代表业务执行成功。
- 分发失败不得回滚已持久化 Job；Job 保持 `PENDING`，可在启动扫描或显式 Resume 规则下重投。
- 停止接收后，关闭流程不再认领新 Job，并对已运行 Job执行有界取消。

### 6.2 条件认领

Worker 在执行前必须经 S00 `JobControlPort.claim_job` 发起条件认领。认领至少校验：

- Job 存在且状态为 `PENDING`；
- Case 的 `active_job_id` 仍为该 Job；
- CaseStatus 与 JobType 的阶段一致：ROUTE/DIAGNOSE 对应 RUNNING，REVIEW 对应 REVIEWING；
- 同一 Case 没有其他活跃 Job；
- 固定资产经 S00 `AssetCatalogPort` 检查后仍可加载；
- 认领写入当前 `runtime_epoch`。

状态竞态导致的 `CLAIM_REJECTED` 不改变状态，Worker 不调用 Runtime，也不自行修正。若 Claim 回执为 `failure_applied=true`，说明 S03/S01 已因 `ASSET_VERSION_UNAVAILABLE` 原子结束 PENDING Job 和 Case；Worker 同样不得调用 Runtime。

### 6.3 Worker

成功认领后：

1. 按 JobType 选择类型化 Worker 视图；
2. 调用 S04 实现的 S00 `Runtime.execute` Port；
3. 将 `RuntimeExecutionReceipt(job_outcome,outcome_file_ref)` 原样交给 S00 `JobControlPort.submit_outcome`；
4. 若 S04 抛出 S00 `RuntimeInfrastructureError`，立即把其 failure ID 与 ExecutionFailure 原样交给 `JobControlPort.report_execution_infrastructure_failure`；
5. 等待 Application Service 返回 disposition；
6. 记录运行日志并释放全局并发许可。

Runtime 一旦返回 receipt，Agent 执行永久结束。若 `submit_outcome` 抛出 `RESOURCE_PUBLISH_FAILED`、`STATE_WRITE_FAILED` 或 S03 三次内部重算后仍有 `REVISION_CONFLICT`，Worker 必须保留同一个 receipt，不再次 claim、不再次调用 `Runtime.execute`、不调用未发布失败入口，并按 `0.1s、0.2s、0.5s、1s、2s、5s` 后固定封顶 5s 的 submission-only 退避重投，直到得到 APPLIED、DUPLICATE、STALE 或 REJECTED，或服务开始关闭。退避等待由可注入 `SubmissionBackoff` 实现并可被关闭唤醒；测试不得真实 sleep。关闭时停止投递并保留 Job 为 RUNNING，由下次启动从执行记录重放。其他非 retryable ApplicationError 是组件缺陷/明确失败，必须记录安全诊断并保持 readiness=false，不能重跑 Agent。

若 Runtime 抛出 `RuntimeInfrastructureError`，同样固定其原始 `failure_id + ExecutionFailure`，调用 `report_execution_infrastructure_failure` 遇到 `STATE_WRITE_FAILED` 或耗尽内部重算后的 `REVISION_CONFLICT` 时使用同一退避做 report-only 重投，直到 APPLIED、DUPLICATE、STALE 或关闭；不得生成第二个 failure ID，也不得重跑 Runtime。由于该窄故障没有 finalized Outcome，进程在报告成功前退出时，下次启动只会按“最终 Outcome 文件不存在”的旧 RUNNING 规则转 INTERRUPTED，之后由用户显式 Resume。

Worker 不解释 Agent 的 `recommended_next_step`，不应用 DiagnosisStateDelta，不分配 Evidence/Artifact 正式 ID。

每次成功认领都创建一个只读 S00 `CancellationSignal` 传给 `Runtime.execute`，其可变 controller 仅由 S05 持有。Application Service 在 CancelCase commit 后调用 `Dispatcher.cancel(job_id)` 时，S05 对匹配运行项一次性设置 `reason=USER_CANCEL`；服务关闭停止认领后，对当前运行项设置 `reason=SERVICE_SHUTDOWN`。首次 reason 永久生效，后到信号只返回 duplicate，不覆盖原因。PENDING Job 的取消只从队列去重移除，不调用 Runtime；找不到或已经结束返回 `CancelReceipt(signalled=false)`。Signal 的 `is_cancelled/wait/reason` 语义逐字使用 S00，不定义私有 Event 接口。

### 6.4 全局并发

- V1 服务进程同时最多执行 `1` 个 Agent Job。
- 并发限制跨 ROUTE、DIAGNOSE、REVIEW 和不同 Case 统一生效。
- 队列可以容纳多个已持久化 PENDING Job，但只有一个成功认领的执行进入 S04。
- 公平性使用确定性的入队顺序；重复 `job_id` 不改变为第二份工作。

### 6.5 runtime_epoch 与启动恢复

- 每个服务进程启动生成一个新的不可复用 `runtime_epoch`。
- 启动协调调用 `InterruptPreviousEpoch` 时由 S03 建立对应 `RuntimeEpochRecord`，只有其 `recovery_completed_at` 已写入才算恢复完成。
- 当前 epoch 仅在 `PENDING → RUNNING` 成功认领时持久化到 Job。
- 启动协调先生成 current epoch 并取得一个一致 StateFile snapshot，按 `{case_id,job_id}` 排序枚举所有尚无对应 `OutcomeProcessingRecord` 的持久化 Job；不得假设旧 PID 或 Session 仍存在。这样取消先胜出、Runtime 随后 finalize 后崩溃的终态 Job 也能补记 STALE，不会留下永久未确认 outbox。
- 第一阶段对上述每个 Job 调用 `ExecutionRecordStore.read_published_outcome(job_id)`。合法 receipt 逐字送入正常 `JobControlPort.submit_outcome`，使用第 6.3 节同一 submission-only 退避；APPLIED、DUPLICATE、STALE、REJECTED 都算已判定。最终文件不存在只表示无可重放 Outcome。最终文件存在但损坏时记录 `EXECUTION_RECORD_FAILED` 安全诊断并使 recovery 持久失败、readiness=false；该 Job 不得进入第二阶段、不得转 INTERRUPTED、不得 Resume 重跑，需管理员先按备份/校验流程修复执行记录。第二阶段只考虑最终文件确实不存在、且重读后仍为旧 epoch RUNNING 的 Job。
- 第一阶段任一投递仍返回 `RESOURCE_PUBLISH_FAILED`、`STATE_WRITE_FAILED` 或 `REVISION_CONFLICT` 时，本 recovery_id 保持未完成、readiness=false；不得把对应 Job 转 INTERRUPTED。进程可继续退避重投，关闭后下次以同一 finalized bytes 重来。
- 第一阶段全部可重放 Outcome 已判定后重新读取 StateFile，再以 `recovery_id=current_runtime_epoch` 调用 `InterruptPreviousEpoch`；它只会看到仍为旧 epoch RUNNING 且没有成功提交结果的 Job。
- 第二阶段剩余旧 Job 通过 Coordinator 的 `MARK_OLD_EPOCH_INTERRUPTED` 计划转换为 `INTERRUPTED`，清除活跃执行关系且不创建 replacement；Scheduler 不直接写状态。
- 恢复命令成功后，全部已持久化 PENDING Job 重投同一 `job_id` 和同一固定快照；其中可以包含第一阶段 Outcome 新创建的 next Job。
- Worker 认领时由 S03 通过 `AssetCatalogPort` 检查固定资产；不可加载时应用 `ASSET_VERSION_UNAVAILABLE` 计划使 Job/Case FAILED，不得替换最新版，也不得让 Job 永久停在 PENDING。
- 已经 WAITING_INPUT、WAITING_ATTACHMENT、RESOLVED、FAILED、CANCELLED 的 Case 不进入执行队列。
- 已是 INTERRUPTED 且无活跃 Job 的 Case等待用户调用 `ResumeCase`；启动器不自动创建 replacement。
- Outcome replay、旧 epoch 中断和 PENDING 重投全部完成前，Scheduler readiness 为 false，且不得认领 Job。

### 6.6 Resume 与 replacement

S06 的 `ResumeCase` 到达 Application Service 后，S05只接收最终已持久化的分发结果：

- PENDING Job：可重投原 Job；
- INTERRUPTED Job：Application Service/Coordinator 创建新 Job、新 `job_id`，并填 `replacement_for_job_id`；
- replacement 保持原阶段，REVIEW 仍为 REVIEW；
- 唯一约束保证重复 Resume 不产生多个 replacement；
- Resume 不注入新输入，等待资料的 Case 必须使用 `SubmitSupplement`。

## 7. 行为与错误码

本切片不定义错误码。认领冲突、资产不可用、取消、执行失败、关闭中断、恢复失败、重复 Outcome 和迟到 Outcome 均使用 S00 错误及 disposition 表。

必须实现以下 S00 行为：

- Runtime 返回可恢复执行错误时，通过 `JobControlPort.submit_outcome` 形成 S00 的失败 Trigger；Scheduler 不自行重跑。
- Outcome 投递遇到 `RESOURCE_PUBLISH_FAILED`、`STATE_WRITE_FAILED` 或耗尽 S03 内部重算后的 `REVISION_CONFLICT` 时，只重投同一 finalized receipt；这不是执行失败，也不能调用 Runtime 第二次。
- Runtime 唯因 ExecutionRecordStore 不可用而无法发布失败 Outcome 时，必须走 `report_execution_infrastructure_failure`；该报告的瞬时 state/revision 失败只重投同一 failure receipt，不得丢下当前进程仍可报告的 RUNNING Job，也不得自行修改状态。
- 固定资产缺失、结构性上下文超限、非法 Job 配置和非法输出合同错误按 S00 进入失败分支。
- 旧 Job 的首次迟到结果必须持久化为审计结果并标记 S00 的 `STALE` disposition；只允许 `case_revision + 1` 记录该审计，不得改变 CaseStatus、active Job、JobStatus 或 DiagnosisState revision。JobStatus 集合中没有 `STALE`。
- 同一 Outcome 重复到达使用 S00 的重复 disposition，不重复递增 revision 或应用 Delta。
- 取消与 Outcome 竞态由 Application Service 的条件事务决定唯一赢家；败方不能覆盖状态。
- 状态提交成功而内存分发失败时 Job 保持 PENDING，不返回虚假的业务失败回滚。

## 8. 关键边界与不变量

- 同一 Case 最多一个活跃 Job；活跃仅指 PENDING 或 RUNNING。
- RUNNING Case 只承载 ROUTE/DIAGNOSE，REVIEWING Case 只承载 REVIEW。
- Scheduler 只持有执行线索，不是业务真相源；进程退出后可从 Repository 重建待处理集合。
- 每个 Job 只允许一次成功认领；重复 dispatch、重复扫描和并发 Worker 都必须满足。
- PENDING 重投不改变 Job 固定输入；INTERRUPTED 重跑必须使用 replacement Job。
- REVIEW 门禁不能因取消、重启或 Resume 被绕过。
- 运行中用户上传新附件不影响已创建 Job。
- S05 不直接读取 Agent stdout/stderr 或 `job_outcome.json` 路径；运行中只消费 S04 receipt，启动时只通过 S00 `ExecutionRecordStore.read_published_outcome` 获取同一类型 receipt。
- 服务关闭不得无限等待；有界关闭到期后通过 S04 取消执行树，并按 S00 上报中断结果。

## 9. Fake 与 Fixture

本切片必须在自己的测试目录提供：

1. `FakeApplicationService` / `FakeJobClaimer`：支持条件认领、Outcome disposition、恢复命令和可注入竞态。
2. `FakeRuntime`：实现 S00 Runtime Port，可阻塞、成功、返回可恢复/不可恢复失败、等待取消、模拟迟到结果。
3. `FakeRecoveryView`：提供 PENDING、当前 epoch RUNNING、旧 epoch RUNNING、INTERRUPTED、终态和资产缺失组合。
4. `DeterministicEpochFactory`：产生固定 epoch，验证重启必然变化。
5. `ManualGate`：精确控制认领、取消、Outcome 和关闭的竞态顺序，不使用不稳定 sleep。
6. `RecordingDispatcher`：记录去重后的队列顺序和实际 Runtime 调用次数。
7. ROUTE/DIAGNOSE/REVIEW Job Fixture：覆盖 CaseStatus 匹配和不匹配。
8. replacement 链 Fixture：覆盖重复 Resume 只能创建一个同阶段 replacement。
9. S00 CancellationSignal Fixture：USER_CANCEL 与 SERVICE_SHUTDOWN 首次原因固定、重复信号不覆盖、wait 唤醒且关闭不无限等待。
10. `ManualSubmissionBackoff`：记录 `0.1/0.2/0.5/1/2/5/5...` 序列并可由关闭唤醒；验证连续 `RESOURCE_PUBLISH_FAILED/STATE_WRITE_FAILED/REVISION_CONFLICT` 只重投同一 Outcome receipt；RuntimeInfrastructureError 的报告失败也只重投同一 failure ID/对象，Runtime 调用次数严格为 1。
11. 启动 outbox Fixture：覆盖 finalize 后崩溃先 replay 再 interrupt、replay 持续失败时 readiness=false、取消先胜出后 finalize 的终态 Job 补记 STALE，以及最终文件损坏使 recovery/readiness 持久失败且绝不 interrupt/Resume/重跑 Agent。

Fake 必须实现 S00 Port，不得暴露生产代码不存在的捷径。

## 10. 独立验证命令

从仓库根目录执行：

```powershell
python -m pytest -q tests/unit/dispatch/test_dispatcher.py
python -m pytest -q tests/unit/dispatch/test_worker_claim.py
python -m pytest -q tests/unit/dispatch/test_concurrency.py
python -m pytest -q tests/unit/dispatch/test_cancellation_races.py
python -m pytest -q tests/unit/dispatch/test_recovery.py
python -m pytest -q tests/unit/dispatch/test_resume.py
python -m pytest -q tests/unit/dispatch
```

测试不得依赖真实 Claude 或真实 logparse，也不得使用超过 1 秒的固定 sleep。并发测试使用事件栅栏证明全局最大执行数严格为 1。

## 11. 完成标准

- 同一 job_id 并发提交和认领时，Runtime 实际调用次数严格为 1。
- 不同 Case 同时排队时，测得最大 Agent Job 执行并发严格为 1。
- 新进程生成新 runtime_epoch，旧 epoch RUNNING Job 经 Application Service 转 INTERRUPTED。
- finalized 且未处理的 Outcome 在同进程和重启后都只做 submission replay；成功结果不会先被转 INTERRUPTED，取消先胜出的结果会补记 STALE，Runtime 调用次数保持 1。
- PENDING Job 重投保持同一 Job ID 和固定输入；INTERRUPTED Resume 使用新 Job ID 和 `replacement_for_job_id`。
- REVIEW 重启、取消和 Resume 后仍走 REVIEW。
- 迟到 Outcome 和重复 Outcome 使用 S00 disposition，且不会修改当前 Case。
- 状态持久化后分发失败保留 PENDING，可再次重投。
- 关闭/取消信号确实传递给 Fake Runtime，并等待其完成清理。
- `python -m pytest -q tests/unit/dispatch` 全绿。
- `git diff --name-only` 中本切片实现变更只位于第 4 节责任区。

## 12. 向 S08 的交接格式

```json
{
  "spec_id": "S05",
  "title": "Scheduler, Worker and Recovery",
  "executor": {"model": "gpt-5.6-sol", "reasoning_effort": "ultra"},
  "contract_revision": "v1-contract-r1",
  "contract_base_commit": "<contract-base-commit>",
  "branch": "codex/v1-s05-scheduler-recovery",
  "head_commit": "<head-commit>",
  "scope_completed": [],
  "changed_files": [],
  "fixtures_consumed": [],
  "fixtures_produced": [],
  "tests": [
    {"command": "python -m pytest -q tests/unit/dispatch", "status": "passed"}
  ],
  "dependency_requests": [],
  "contract_change_requests": [],
  "known_limitations": [],
  "risks": [],
  "integration_notes": ["measured_max_agent_concurrency=1"],
  "forbidden_scope_touched": false
}
```

以上顶层字段全部必填，不得省略；各数组必须填写真实值，没有内容时写空数组。交接文件固定写入 `handoff/S05.json`。任何被跳过的当前平台可执行竞态测试都必须进入 `known_limitations`，且该交接不能进入 S08。

## 13. S08 组合要求

S08 通过 S00 Port 注入 S02 Repository、S04 Runtime 和 S03 Application Service，不引用 S05 私有实现。至少组合验证：

- 持久化 Job 后才能 dispatch；
- `PENDING → RUNNING` 条件认领与 runtime_epoch 持久化；
- 进程重启时 PENDING 重投、旧 RUNNING 转 INTERRUPTED；
- 进程重启先扫描全部未确认 finalized Outcome 并投递，只有剩余旧 RUNNING 才转 INTERRUPTED；投递故障时 readiness 保持 false；
- 显式 Resume 创建同阶段 replacement；
- HTTP/MCP 取消传播到正在运行的 S04 子进程树；
- Outcome 与取消竞态只产生一个有效状态转换；
- 迟到结果保留但 disposition 为 STALE。

组合缺陷退回路径所有者；S08 不直接修改 `src/problem_locator/dispatch/**`。

## 14. 合同变更请求格式

```json
{
  "request_id": "CCR-S05-001",
  "requesting_spec": "S05",
  "current_contract_revision": "v1-contract-r1",
  "problem": "现有合同无法实现或验证的精确问题",
  "proposed_change": "请求后的完整语义",
  "affected_types_or_codes": [],
  "affected_specs": ["S00", "S05"],
  "compatibility": "对状态机、持久化、Runtime 或接口的影响",
  "fixture_and_test_changes": []
}
```

只有 S00 所有者接受并更新合同后才能实现变化。不得用私有 Job 状态、隐式重试或队列字段绕过合同。
