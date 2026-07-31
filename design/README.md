# Problem Locator 正式版本设计

状态：V1 基线已收敛

更新时间：2026-07-31

本目录保存 Problem Locator（问题定位系统）正式版本的设计基线。当前代码仓只负责设计和调研；正式实现将在新的代码仓库中开发，不以现有 Demo（演示实现）的内部结构为兼容目标。

## 当前有效文档

1. [Problem Locator V1 基线设计](v1-baseline-design.md)

   唯一规范性设计，定义范围、静态架构、职责、领域模型、上下文策略、Agent Session（智能体会话）生命周期、流程、文件传输、可靠性与验收条件。

2. [Problem Locator V1 决策记录](v1-decision-record.md)

   解释当前选择、接受的代价、被替代的旧设计和复议条件。若它与基线正文冲突，以基线正文为准。

## 一句话基线

> Case（诊断案例）有状态，Job（任务）自包含，Agent Session（智能体会话）可丢弃。

跨 Job 必须延续的信息进入 Repository（仓库）中的结构化 DiagnosisState（诊断状态）；每个 Job 固定小型 `context_snapshot（上下文快照）`、不可变资源引用和执行版本；Context Builder（上下文构建器）据此生成有界输入；每个 Agent Job 默认使用新 Session。

## 参考材料

- [高 Star Agent 上下文策略调研](../doc/high-star-agent-context-strategy-survey.md)
- [问题定位开源项目洞察](../doc/problem-locator-open-source-insight.md)

参考材料用于解释行业做法和设计来源，不自动构成实现要求。

## 文档状态约定

- “已确认”：已经进入 V1 基线。
- “暂缓”：方向不否定，但 V1 不实现。
- “已替代”：历史选择，不能再指导实现。
- “待详细设计”：架构方向已确认，字段、状态转换或错误语义仍需细化。

旧的总体框架、选项清单、接入专题和静态架构草图已被上述两份文档完整替代，不再在工作区保留；需要追溯时使用 Git 历史。

## 下一会话入口

下一阶段从基线设计第 17～18 节开始，依次细化：

1. Case、Job 和 JobOutcome（任务结果）状态机；
2. DiagnosisState 与 DiagnosisStateDelta（诊断状态增量）Schema（结构定义）；
3. Diagnosis Coordinator（诊断协调器）的确定性转换表；
4. Context Builder 的输入、输出和预算规则；
5. Agent Backend（智能体执行后端）协议；
6. Repository 事务与 Job 恢复语义；
7. Remote MCP（远程 MCP）与 HTTP 接口字段。
