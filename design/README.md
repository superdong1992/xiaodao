# 当前设计导航

状态：Problem Locator 3.0 与 Test Flow 当前设计

本目录只维护当前架构入口，不保存已完成的一次性实施任务书。历史设计、取舍和迁移过程由 Git 历史保留，不再与现行合同并列。

## 权威边界

- 产品使用、部署边界与公开行为：[`README.md`](../README.md)
- 测试框架架构、不变量与完成判据：[`test-flow-architecture.md`](test-flow-architecture.md)
- 真实 Wiki 转专用定位 Skill、证据规则与部分终态：[`wiki-diagnosis-generalization.md`](wiki-diagnosis-generalization.md)
- 测试操作与证据管理：[`tools/test-flow/README.md`](../tools/test-flow/README.md)
- 当前活跃产品待办：[`TODO.md`](../TODO.md)
- 已修复问题、回归历史与专项测试：[`FIXED_ISSUES.md`](../FIXED_ISSUES.md)
- 公开 MCP 合同：生成的 `schemas/`、运行时 `src/` 合同和 `.claude/skills/problem-locator-client/`
- Diagnosis Skill 合同：GenerationSpec、生成 manifest、output contracts 和生成资产

发生冲突时，机器可校验的 schema、生成资产和运行时代码高于叙述性摘要；叙述文档必须随同一变更同步修正。测试发布结论只来自与对应不可变源码快照绑定的 Test Flow verdict，不能用文档、历史记录、Git 提交本身或代码存在性替代。

## 当前架构基线

Problem Locator 是 Linux 单实例 Server。Windows、macOS 和显式 Linux Client 使用本机 Host，通过 HTTP 直连 Server；客户端没有本地 MCP、代理、Hook 或 Problem Locator 专用 DFX。七个公开 MCP 工具只接受扁平根参数。

State、Job 与权威 Outcome 当前使用 schema v5，并区分 SPECIALIZED/GENERIC DIAGNOSE。Agent 只生成 draft；服务端重新验证原始证据并生成权威 Outcome、公开 JSON/ZIP 或不可定论结果。Diagnosis Skill generator 与 manifest 的当前版本表见根 README。

Test Flow 使用 Goal → Proof → Stage → Gate → receipt → verdict 的单一链路。Dev 提供快速确定性反馈和受控复用；Release 绑定当前全部身份，从 GENESIS 和空数据根执行一条 fresh CrossJob。详细语义只在测试架构文档中维护。
