# Problem Locator V1 设计入口

状态：V1 详细设计已分册，可进入后续实现
更新时间：2026-07-31

本目录保存 Problem Locator V1 的规范性设计。正式代码、测试和 Skill 资产后续仍在当前代码仓实现，不另建产品仓库；本次变更仅落库文档，尚未启动任何实现任务。

## 规范入口

1. [《Problem Locator V1 基线设计》](v1-baseline-design.md)

   定义产品范围、总体架构、核心不变量、领域语义和主流程。

2. [《Problem Locator V1 决策记录》](v1-decision-record.md)

   记录取舍、代价、已替代方案和复议条件。

3. [《V1 独立说明书索引与权威归属矩阵》](v1-specs/README.md)

   索引 S00～S07，冻结每项需求的唯一归属、未来代码白名单和独立验收入口。

4. [《S08 V1 组合与总装说明书》](v1-composition-spec.md)

   只定义未来任务批次、接缝、RPC 超时 E2E、返工路由、总装验收和 PostgreSQL 迁移边界，不执行组合。

## 分册

| 编号 | 说明书 | 权威职责 |
|---|---|---|
| S00 | [公共合同冻结](v1-specs/S00-contract-freeze.md) | Schema、Port、枚举、错误码、限制、共享 Fixture |
| S01 | [领域模型与 Coordinator](v1-specs/S01-domain-coordinator.md) | Case/Job 状态机、TransitionPlan、revision |
| S02 | [JSON 与文件资源存储](v1-specs/S02-json-resource-storage.md) | `state.json`、实例锁、原子写、FileResourceStore |
| S03 | [Application Service](v1-specs/S03-application-service.md) | 单写入口、幂等、资源提案、状态提交 |
| S04 | [Runtime、Context 与 Backend](v1-specs/S04-runtime-context-backend.md) | 200 KiB Context、Workspace、`CLAUDE_COMMAND`、Outcome 校验 |
| S05 | [Scheduler 与恢复](v1-specs/S05-scheduler-recovery.md) | Dispatcher、Worker、取消、INTERRUPTED、Resume、STALE |
| S06 | [Remote MCP、HTTP 与 CLI](v1-specs/S06-mcp-http-cli.md) | 七个 MCP 工具、文件 HTTP、CLI、配置、Client Access Skill |
| S07 | [Skill 与 logparse](v1-specs/S07-skill-logparse.md) | Diagnosis Skill、生成器 2.0.0、logparse |
| S08 | [组合与总装](v1-composition-spec.md) | 依赖批次、接缝测试、RPC 超时 E2E、发布门禁 |

## 一句话基线

> Case 有状态，Job 自包含，Agent Session 可丢弃。

跨 Job 的可靠连续性来自持久化 DiagnosisState、Evidence、Attachment、Artifact 和 Job 固定快照，不来自历史会话。一次日志分析的解析目录以内部 `LOGPARSE_RUN` Artifact 保存；中途补参后的新 Job 复用该目录，不再次执行 `logparse parse`。

## V1 固定部署与存储选择

- 当前仓库实现；
- 单机、单服务进程、单 Uvicorn worker、Agent Job 并发 1；
- 一个本地 `DATA_ROOT/state.json` 保存权威结构化状态；
- FileResourceStore 保存附件、证据和产物字节；
- 无数据库、无 Docker；
- Router 默认 128 KiB，Specialist/Reviewer 默认 200 KiB；
- 压缩日志只由 logparse 处理；
- 受控内网运行，不提供认证或 TLS。

“单机”不限制客户端数量；它表示同一个 `DATA_ROOT` 只能由一个服务进程独占，不能多实例共享或自动故障接管。

## 规范权威顺序

基线设计高于组件分册；S00 的机器合同高于 S01～S07 的内部实现描述；S08 只负责组合，不能覆盖组件语义。发现冲突时走 S00 合同变更流程，不能在组件内创建第二套字段或错误码。

## 参考材料

- [高 Star Agent 上下文策略调研](../doc/high-star-agent-context-strategy-survey.md)
- [问题定位开源项目洞察](../doc/problem-locator-open-source-insight.md)

参考材料只解释设计来源，不自动构成要求。

## 后续入口

只有在用户另行明确要求“开始实现”后，才按 S08 创建独立开发任务。所有未来开发任务固定使用：

```text
model: gpt-5.6-sol
reasoning_effort: ultra
```

执行顺序为 S00 合同冻结、S01～S07 分册实现、S08 总装；当前文档落库不代表任何开发任务已经启动。
