# RPC 超时定位 Skill 历史可行性实验

这个目录仅保留 Methods 工程化前的历史探索记录。当时用一份人工 Wiki 生成定位
Skill，再对九份冻结日志做多轮诊断；这些记录帮助确认了原文证据、身份 token、
多事件拆分和候选方法的必要性。

历史结果包括：

- [`artifacts/RESULTS.md`](artifacts/RESULTS.md)：早期四例可行性基线；
- [`artifacts/HARDENING_RESULTS.md`](artifacts/HARDENING_RESULTS.md)：加固实验；
- [`artifacts/EVIDENCE_GROUNDING_RESULTS.md`](artifacts/EVIDENCE_GROUNDING_RESULTS.md)：证据 grounding 试验。

它们只描述当时的实验输入、运行器和模型行为，不是当前产品回归证据，不产生
Test Flow verdict，也不能支持“九例 PASS”或 Release PASS 结论。

## 当前唯一入口

旧 `run.py`、`check_evidence_contract.py` 和实验版元 Skill 已退役；不得从本目录直接
执行预处理、生成或诊断，也不得用历史 JSON/Markdown 产物组装新的发布结论。

当前 Methods 架构见
[`design/wiki-diagnosis-generalization.md`](../../design/wiki-diagnosis-generalization.md)，所有测试活动只从
[`tools/test-flow/run.sh`](../../tools/test-flow/run.sh) 或 Windows 等价入口 `run.ps1` 进入，操作、缓存、
预算、身份与证据合同见
[`tools/test-flow/README.md`](../../tools/test-flow/README.md)。

日常确定性验证使用 `dev.default`；它不调用真实模型：

```bash
./tools/test-flow/run.sh --track dev --goal dev.default
```

Codex CLI + `gpt-5.6-luna` 探索已收口为独立的 `release.codex-luna-methods` Goal；它不属于
产品 `release.full` 的 CrossJob 闭包。该 Goal 用一次 Methods 生成加九次只读诊断，绑定精确
Codex CLI、`gpt-5.6-luna`、`medium` reasoning、Logparse 输入和不可变 source snapshot。
运行前必须先对 `release.codex-luna-methods` 执行 `--plan-only`，提供配置要求的
`--logparse-source`、`--codex-entry` 和 `--codex-auth`，并显式确认
`--allow-codex-posthoc-budget`；它不需要 MCP、Claude Code 或 Docker 输入。不得直接调用
`tools/test-flow/runtime-support/codex-luna-exploration-runner.mjs` 或从本目录绕过 Test Flow。

要同时宣称产品 Linux→Linux CrossJob 和 Codex Luna 探索通过，必须分别执行两个 Goal，且两个
权威 `verdict.json` 必须绑定同一个 Git-visible source snapshot digest。

只有最后与当前 Git-visible source snapshot 精确绑定的 `verdict.json` 是权威结论；plan-only、
半成品证据目录、历史 artifact 或直接运行底层 runner 都不能代替它。
