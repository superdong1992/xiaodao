# 当前设计导航

状态：Problem Locator 5.0.0 / State V7 与 Test Flow 当前设计

本目录只维护当前架构入口，不保存已完成的一次性实施任务书。历史设计、取舍和迁移过程由 Git 历史保留，不再与现行合同并列。

## 权威边界

- 产品使用、部署边界与公开行为：[`README.md`](../README.md)
- 测试框架架构、不变量与完成判据：[`test-flow-architecture.md`](test-flow-architecture.md)
- 真实 Wiki 转 Methods package、产品注册、两 Pass 运行与 grounding/Review 边界：[`wiki-diagnosis-generalization.md`](wiki-diagnosis-generalization.md)
- 测试操作与证据管理：[`tools/test-flow/README.md`](../tools/test-flow/README.md)
- 当前活跃产品待办：[`TODO.md`](../TODO.md)
- 已修复问题、回归历史与专项测试：[`FIXED_ISSUES.md`](../FIXED_ISSUES.md)
- 公开 MCP 合同：生成的 `schemas/`、运行时 `src/` 合同和 `.claude/skills/problem-locator-client/`
- Methods Skill 合同：`.agents/skills/wiki-to-diagnosis-skill/`、产品拥有的 `registration-template.json`、内置 output contracts 和运行时加载器
- 局域网直用 Logparse Skill 合同：`.claude/skills/wiki-to-logparse-diagnosis-skill/`；它与当前 Server Methods registration 分离

发生冲突时，机器可校验的 schema、生成资产和运行时代码高于叙述性摘要；叙述文档必须随同一变更同步修正。测试发布结论只来自与对应不可变源码快照绑定的 Test Flow verdict，不能用文档、历史记录、Git 提交本身或代码存在性替代。

## 当前架构基线

Problem Locator 是 Linux 单实例 Server。Windows、macOS 和显式 Linux Client 使用本机 Host，通过 HTTP 直连 Server；客户端没有本地 MCP、代理、Hook 或 Problem Locator 专用 DFX。七个公开 MCP 工具只接受扁平根参数。

State、Job 与权威 Outcome 当前使用 schema V7 / `v7-contract-r1`，并区分 SPECIALIZED/GENERIC DIAGNOSE。SPECIALIZED Agent 只生成 Methods diagnosis/review 草稿；服务端绑定产品 registration 与闭合 package，独立完成 Logparse 预处理、冻结输入、原始行 grounding、域模型映射与权威 Outcome 生成。Methods package 和运行时资产的当前版本表见根 README。

Test Flow 使用 Goal → Proof → Stage → Gate → receipt → verdict 的单一链路。Dev 除快速确定性反馈和受控复用外，还提供两个独立的 Darwin arm64 Codex/Luna Goal：一次调用的 Methods cache bootstrap，以及依赖精确缓存、恰好五次调用的本机 Streamable HTTP MCP 单场景 E2E；二者都不产生 Release 或跨平台结论。Release 绑定当前全部身份，从 GENESIS 和空数据根执行一条 fresh CrossJob。详细语义只在测试架构文档中维护。
