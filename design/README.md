# 当前设计导航

状态：Problem Locator 6.0.0 / State V9 与 Test Flow 当前设计

本目录只维护当前架构入口，不保存已完成的一次性实施任务书。历史设计、取舍和迁移过程由 Git 历史保留，不再与现行合同并列。

## 权威边界

- 产品使用、部署边界与公开行为：[`README.md`](../README.md)
- 测试框架架构、不变量与完成判据：[`test-flow-architecture.md`](test-flow-architecture.md)
- 真实 Wiki 转 Methods package、Methods V1 证据核验与可选独立审核边界：[`wiki-diagnosis-generalization.md`](wiki-diagnosis-generalization.md)
- 测试操作与证据管理：[`tools/test-flow/README.md`](../tools/test-flow/README.md)
- 当前活跃产品待办：[`TODO.md`](../TODO.md)
- 已修复问题、回归历史与专项测试：[`FIXED_ISSUES.md`](../FIXED_ISSUES.md)
- 公开 MCP 合同：生成的 `schemas/`、运行时 `src/` 合同和 `.claude/skills/problem-locator-client/`
- Methods package 合同：`.agents/skills/wiki-to-diagnosis-skill/`、内置 output contracts 和运行时加载器
- 局域网部署 registration 生成合同：`.claude/skills/wiki-to-logparse-diagnosis-skill/`；它生成 `registration-template.json` 与闭合 Methods package，供客户端经 MCP 路由到当前 Server Methods V1 链路

发生冲突时，机器可校验的 schema、生成资产和运行时代码高于叙述性摘要；叙述文档必须随同一变更同步修正。测试发布结论只来自与对应不可变源码快照绑定的 Test Flow verdict，不能用文档、历史记录、Git 提交本身或代码存在性替代。

## 当前架构基线

Problem Locator 是 Linux 单实例 Server。Windows、macOS 和显式 Linux Client 使用本机 Host，通过 HTTP 直连 Server；客户端没有本地 MCP、代理、Hook 或 Problem Locator 专用 DFX。七个公开 MCP 工具只接受扁平根参数。

State、Job 与权威 Outcome 当前使用 schema V9 / `v9-contract-r1`，并区分 SPECIALIZED/GENERIC DIAGNOSE。SPECIALIZED Agent 提交 `MethodDiagnosisDraftV1`；服务端核对方法、marker、日志来源、行号、原文与哈希，再生成 Evidence、Candidate、审计记录和权威 Outcome。每个专有 Job 都冻结 `review_policy`：默认 `NONE`，直接接受通过核验的 Candidate；显式开启后使用 `INDEPENDENT`，只有 Reviewer PASS 才公开报告。终态报告是 `diagnosis-result.json`，已解决结果另带含原始目标日志的 `result.zip`。Methods package 和运行时资产的当前版本表见根 README。

Test Flow 使用 Goal → Proof → Stage → Gate → receipt → verdict 的单一链路。Dev 先跑 affected 与 full deterministic；Reviewer 关闭路径由 SameJob 覆盖。Release 从 GENESIS 和全新空 V9 数据根执行 Reviewer 开启的最长 CrossJob，核对 Candidate、审核隔离、JSON/ZIP 同时公开、真实浏览器 list/download 与重启重放。详细语义只在测试架构文档中维护。
