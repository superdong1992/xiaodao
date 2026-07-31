# Problem Locator V1 独立说明书索引与权威归属矩阵

状态：正式索引

本目录用于组织可独立开发、独立验证的 S00～S07 分册；S08 位于上级 `design/` 目录。当前变更只落库设计文档，没有创建或派发开发任务，没有创建分支或 worktree，也没有修改产品代码或 Skill 资产。

## 1. 说明书索引

| 编号 | 名称 | 规范路径 | 权威范围 |
|---|---|---|---|
| S00 | 合同冻结与公共测试规范 | [`S00-contract-freeze.md`](S00-contract-freeze.md) | 公共枚举、DTO、Port、错误码、Schema、Fixture、revision 矩阵 |
| S01 | 领域模型与状态机说明书 | [`S01-domain-coordinator.md`](S01-domain-coordinator.md) | Case、DiagnosisState、不变量、Coordinator、TransitionPlan |
| S02 | JSON 与文件资源存储说明书 | [`S02-json-resource-storage.md`](S02-json-resource-storage.md) | state.json、实例锁、原子写、ResourceStore、清理和恢复校验 |
| S03 | Application Service 说明书 | [`S03-application-service.md`](S03-application-service.md) | 单写入口、幂等、引用校验、计划提交、资源提案 |
| S04 | Runtime 与上下文说明书 | [`S04-runtime-context-backend.md`](S04-runtime-context-backend.md) | Context Builder、Workspace、Agent Backend、结果文件和进程限制 |
| S05 | 调度与恢复说明书 | [`S05-scheduler-recovery.md`](S05-scheduler-recovery.md) | Dispatcher、Worker、Job 认领、取消、中断、恢复和 STALE disposition |
| S06 | MCP、HTTP 与 CLI 说明书 | [`S06-mcp-http-cli.md`](S06-mcp-http-cli.md) | Remote MCP、文件 HTTP、配置、CLI、Client Access Skill、协议错误映射 |
| S07 | Skill 与 logparse 说明书 | [`S07-skill-logparse.md`](S07-skill-logparse.md) | Diagnosis Skill 扫描/生成、真实 logparse 和解析复用 |
| S08 | 组合与总装说明书 | [`../v1-composition-spec.md`](../v1-composition-spec.md) | 依赖批次、合并顺序、接缝、E2E、返工和发布验收 |

S00～S08 均已形成。文件名、编号和链接以本表为准。

## 2. 规范权威顺序

1. `design/v1-baseline-design.md`：产品范围、架构和核心不变量。
2. S00 冻结合同及其 manifest：机器可验证的跨模块事实。
3. S01～S07：各模块内部行为和验收。
4. S08：组合、合并、返工和发布流程。

后一级不得覆盖前一级。发生冲突时停止实现并走 S00 合同变更流程。

## 3. 生产代码责任白名单

未来代码目录以本矩阵为唯一写入归属。若实际脚手架需要调整目录，必须先由 S00 修改合同与本矩阵，不能由模块任务各自改名。

| 说明书 | 独占写入白名单 | 禁止直接修改 |
|---|---|---|
| S00 | `src/problem_locator/contracts/**`、`schemas/v1/**`、`tests/contracts/**`、`tests/fixtures/contracts/**`、`handoff/S00.json`、合同冻结或已接受合同修订所需的 `design/v1-specs/README.md`；仅冻结阶段的 `pyproject.toml`、`uv.lock` | 所有模块业务实现；合同冻结后不得再直接修改根依赖 |
| S01 | `src/problem_locator/domain/**`、`tests/unit/domain/**`、`tests/fixtures/components/domain/**`、`handoff/S01.json` | contracts、存储、接口、Runtime、根依赖 |
| S02 | `src/problem_locator/storage/**`、`tests/unit/storage/**`、`tests/fixtures/components/storage/**`、`handoff/S02.json` | domain、application、interfaces、Runtime、根依赖 |
| S03 | `src/problem_locator/application/**`、`tests/unit/application/**`、`tests/fixtures/components/application/**`、`handoff/S03.json` | contracts、具体存储内部、接口、Runtime、根依赖 |
| S04 | `src/problem_locator/runtime/**`、`tests/unit/runtime/**`、`tests/fixtures/components/runtime-*/**`、`handoff/S04.json` | domain、application、interfaces、Skill、根依赖 |
| S05 | `src/problem_locator/dispatch/**`、`tests/unit/dispatch/**`、`tests/fixtures/components/dispatch-*/**`、`handoff/S05.json` | domain、application、interfaces、Runtime 内部、根依赖 |
| S06 | `src/problem_locator/interfaces/**`、`src/problem_locator/entrypoints/**`、`.claude/skills/problem-locator-client/**`、`tests/unit/interfaces/**`、`handoff/S06.json` | domain、application、storage、Runtime、Diagnosis Skill、根依赖 |
| S07 | `src/problem_locator/integrations/logparse/**`、`.claude/skills/wiki-to-diagnosis-skill/**`、`.claude/skills/logparse-diagnose/**`、`.claude/skills/diagnose-service-takeover/**`、`tests/unit/integrations/**`、`tests/fixtures/components/logparse/**`、`handoff/S07.json` | domain、application、storage、interfaces、Client Access Skill、根依赖、跨模块 Fixture |
| S08 | `src/problem_locator/__init__.py`、`src/problem_locator/__main__.py`、`src/problem_locator/bootstrap.py`、`tests/conftest.py`、`tests/integration/**`、`tests/e2e/**`、`tests/fixtures/rpc_timeout/**`、`tests/fixtures/failures/**`、`README.md`、`.env.example`、`handoff/S08.json`；合同冻结后经批准的 `pyproject.toml`、`uv.lock` | 各模块责任目录中的业务逻辑 |

`handoff/S00.json`～`handoff/S08.json` 由对应说明书独占。一个任务只能写自己的交接文件。S08 不使用另一个未冻结格式的“最终报告”替代 `handoff/S08.json`。

## 4. 测试责任归属

| 测试类型 | 权威所有者 |
|---|---|
| 公共 Schema、fixture 和 Port conformance | S00 |
| 单模块单元测试 | 对应 S01～S07 |
| 两模块及以上接缝/集成测试 | S08 |
| RPC 超时 R01～R14 与 `tests/fixtures/rpc_timeout/**` | S08；S07 只提供 `tests/fixtures/components/logparse/**` 可复用组件 Fixture |
| 通用故障场景与 `tests/fixtures/failures/**` | S08 |
| Windows/Linux 启动验证 | S08 |
| PostgreSQL 离线导出兼容测试 | S08 验证边界，数据库实现不属于 V1 |

每个 Fixture 责任子树必须维护通过 S00 `schemas/v1/fixture-manifest.schema.json` 的 `fixture-manifest.json`；它只能登记同一子树内的普通文件，并且条目集合必须与除 manifest 自身外的磁盘文件全集完全相等：

- S00：`tests/fixtures/contracts/fixture-manifest.json`；
- S01：`tests/fixtures/components/domain/fixture-manifest.json`；
- S02：`tests/fixtures/components/storage/fixture-manifest.json`；
- S03：`tests/fixtures/components/application/fixture-manifest.json`；
- S04：每个匹配 `tests/fixtures/components/runtime-*/**` 的具体子树各自维护 manifest；
- S05：每个匹配 `tests/fixtures/components/dispatch-*/**` 的具体子树各自维护 manifest；
- S07：`tests/fixtures/components/logparse/fixture-manifest.json`；
- S08：`tests/fixtures/rpc_timeout/fixture-manifest.json` 和 `tests/fixtures/failures/fixture-manifest.json`。

禁止创建由多个并行任务共同写入的 `tests/fixtures/fixture-manifest.json`。

本矩阵中的 glob 全部按仓库相对 POSIX path 解释：`**` 可递归匹配子目录和文件，`*` 只匹配一个路径段内的字符；Windows 实现也先把路径规范为 `/` 再做白名单匹配。

## 5. 依赖请求和共享文件规则

- 模块任务需要新增依赖时，在交接 JSON 的 `dependency_requests` 中给出包名、固定版本、用途和许可证影响。
- S00 仅在合同冻结阶段独占 `pyproject.toml` 与 `uv.lock`，负责建立初始依赖基线；冻结提交形成后，S00 和 S01～S07 都不得再编辑这两个文件。
- 合同冻结后，只有 S08 可以在集成分支串行审查并应用交接中已经批准的 `dependency_requests`；S08 不得借此改变未获批准的合同语义。
- 公共 `__init__`、命令注册、应用工厂和启动装配发生冲突时，由 S08 统一处理。
- 若共享修改会改变公开 DTO、Port 或错误语义，必须返回 S00，而不是由 S08 处理。

## 6. 未来任务启动条件

每个未来 Codex 任务的任务书必须明确写入：

```text
spec_id = Sxx
title = <任务标题>
model = gpt-5.6-sol
reasoning_effort = ultra
contract_revision = <冻结修订号>
contract_base_commit = <S00 为任务起始提交；S01～S08 为 S00 冻结提交>
branch = codex/<任务分支；S08 固定为 codex/v1-s08-integration>
write_allowlist = <本矩阵对应路径>
required_tests = <对应说明书验收命令>
handoff_file = handoff/Sxx.json
handoff_required_fields = spec_id,title,executor,contract_revision,contract_base_commit,branch,head_commit,scope_completed,changed_files,fixtures_consumed,fixtures_produced,tests,dependency_requests,contract_change_requests,known_limitations,risks,integration_notes,forbidden_scope_touched
```

所有交接必填字段都必须出现；没有内容的列表写空数组，`contract_base_commit`、`branch` 和 `head_commit` 必须填写真实值。S00 的 `contract_base_commit` 是其任务起始提交；S01～S08 的该字段必须是包含 `handoff/S00.json` 的最终 S00 冻结提交。`head_commit` 指加入交接文件之前已经通过测试的实现/集成提交；随后只允许用一个 handoff-only commit 写入本任务自己的 `handoff/Sxx.json`，该提交的第一父提交必须等于所声明的 `head_commit`。S00 的 handoff-only 分支头就是供后续任务使用并加冻结标签的合同冻结提交。S00 冻结前不得启动 S01～S07 的正式实现。S08 只在 S01～S07 交接有效、独立测试通过后启动。当前索引的建立不代表这些任务已经启动。
