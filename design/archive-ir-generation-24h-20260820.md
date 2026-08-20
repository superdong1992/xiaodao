# GenerationSpec / Rule-IR 路线归档（2026-08-20）

> 状态：**历史实现归档，不可交付，不代表 Release PASS。**
>
> 本文随分支 `codex/archive-ir-generation-24h-20260820` 保存，用于保留当前代码、实现思路、24 小时以上的反复执行记录和失败证据索引。它不能替代任何 `verdict.json`，也不能作为继续生产接入的默认架构决策。

## 1. 归档边界

- 归档执行窗口：约 `2026-08-18T13:42Z` 至 `2026-08-19T16:28Z`，持续约 26 小时 46 分钟。
- 本地 evidence 共形成 94 个权威 verdict、23 次 real-skill gate。
- 17 次 usage 完整的真实模型调用累计：**14,465,415 tokens / $94.538827**。
- 全窗口没有 Release PASS。
- 基线 commit：`f99a3d5f2cef54fd86ef43030311ab5c42e377d4`
- 集成 worktree：原 A+B+C 独立集成 worktree；未修改 A/B/C/D 来源 worktree。
- main：未合并、未改写。
- 最后一个经过零模型 Dev 验证的 Git-visible source：
  `747bbae230a08ee7f1ddd26a8dd27ddc535532e3224c7cdd63577b2088c4c627 / 618 files`
- 最后零模型 Dev：`run-20260819T160744Z-e60da0a7`，`PASS_WITH_WARNINGS`，
  full deterministic 全 PASS，真实模型用量 0。
- 最后真实模型校准：`run-20260819T161046Z-f83a1349`，`FAIL / CONTRACT / PYTEST_FAILED`。
- 最终 Release：**未执行**。CrossJob 六个 Stage 没有在最终候选上运行。

该分支同时保留 A+B+C 合并实现及随后加入的 full GenerationSpec、StructuredOutput、Rule-IR/compiler、wrapper、receipt、Test Flow 与回归测试修改。它是失败路线的完整工程快照，不是只包含 IR 文件的最小补丁。

## 2. 实际实现思路

### 2.1 第一阶段：模型直接生成完整 GenerationSpec

最初生产路径要求真实模型读取：

1. `wiki-to-diagnosis-skill`；
2. Wiki 与 clarifications；
3. 六份串行 reference/checkpoint；
4. 在固定 ordinal 上通过唯一 `StructuredOutput` 提交完整 GenerationSpec v6；
5. wrapper 将对象规范化、封存、落盘，再进入深层 validator、产品语义审计与九场景后验。

完整对象包含 2 个 observation policies、10 个 extractors、165 条 rules 和 9 条 terminal paths。批准样本约 139 KiB，其中 144 条重复 ordered-interval / `q_*` family rules 占主要体积。

Test Flow 对该路径冻结了 exact tool sequence、turn/token/time/cost caps、Claude CLI/settings identity、源码快照、schema、trace、terminal、canonical output 与 evidence receipts。失败必须 fail closed，不允许接受第二次 StructuredOutput 或放宽最终 Gate。

### 2.2 第二阶段：紧凑 GenerationBlueprint / RuleFamily IR

在完整对象多次失败后，实现转为：

- 模型提交紧凑 `GenerationBlueprint`；
- 10 个 extractors、21 条非重复 rules 和其他业务语义仍为 typed literal；
- 一个版本化 `ordered_interval_coverage_v1` family 描述五个 ordered positions、字段绑定、依赖、语义文本与 terminal path metadata；
- 可信、纯确定性 compiler 展开 144 条重复 rules 和 3 条 family terminal paths；
- 最终仍产生完整的 2/10/165/9 GenerationSpec，并继续经过原 loader、深层 validator、业务 invariant、产品语义审计和九场景 Gate。

compiler 不读取 approved spec、oracle、case 模板、时间或随机源，不允许任意表达式、`eval` 或残留模板。wrapper 绑定：

- IR canonical input seal；
- compiler id/version；
- expanded GenerationSpec output seal；
- terminal StructuredOutput 回显；
- source / producer / proof / settings identity。

IR 上限被冻结为 48 KiB。任何 unknown family、exact-key 偏差、重复 ID、字段或依赖不闭合、模板残留、错误数量、compiler/validator 失败都必须拒绝且不创建最终文件。

实际冻结身份为：

- IR 根键：`schema_version / compiler / spec / verification`；
- compiler：`generation-blueprint-ordered-interval@1.0.0`；
- blueprint schema：`1`；
- family：`ORDERED_INTERVAL@1`；
- rule 拼接顺序：`7 literal prefix + 105 mechanical + 9 literal middle + 39 semantic + 5 literal suffix = 165`；
- path 拼接顺序：`1 generated complete + 2 literal + 2 generated partial + 4 literal = 9`。

### 2.3 Test Flow 与失败诊断扩展

为支持上述路径，工程中还实现了：

- Claude CLI StructuredOutput trace audit；
- 首次/第二次提交、tool ordinal、turn、terminal 和 usage 的 fail-closed 校验；
- content-free partial trace 与 constraint-level compiler diagnostics；
- source、settings、MCP、Skill、schema、fixture、producer、proof 与 evidence receipt 身份绑定；
- Windows scratch、source-copy、cache checkout 与证据保留处理；
- Dev affected + full deterministic、dev.real plan-only、校准与 Release admission 编排。

这些机制提高了失败可审计性，但也使测试/生成框架逐渐主导了产品流程。

## 3. 关键实现文件

### Skill 与合同

- `.claude/skills/wiki-to-diagnosis-skill/SKILL.md`
- `.claude/skills/wiki-to-diagnosis-skill/references/checkpoints/01-begin-repeated-families-and-paths.md`
- `.claude/skills/wiki-to-diagnosis-skill/references/checkpoints/02-begin-9-1-inventory.md`
- `.claude/skills/wiki-to-diagnosis-skill/references/checkpoints/03-begin-9-2-witnesses.md`
- `.claude/skills/wiki-to-diagnosis-skill/references/checkpoints/04-write-now.md`
- `.claude/skills/wiki-to-diagnosis-skill/references/generation-spec-v6-reference.md`
- `.claude/skills/wiki-to-diagnosis-skill/references/verification-contract-v2-reference.md`

### IR schema 与确定性 compiler

- `tools/test-flow/runtime-support/skill-generation-rule-ir.mjs`
- `tools/test-flow/runtime-support/skill_generation_rule_ir.py`
- `tools/test-flow/runtime-support/compile_skill_generation_rule_ir.py`
- `prototypes/generation_blueprint/fixtures/rpc_timeout_blueprint.json`

### Wrapper、trace 与 evidence

- `tools/test-flow/runtime-support/isolated-agent-wrapper.mjs`
- `tools/test-flow/runtime-support/isolated-agent-tool-audit.mjs`
- `tools/test-flow/runtime-support/isolated-agent-env.mjs`
- `tools/test-flow/lib/actions.mjs`
- `tools/test-flow/lib/engine.mjs`
- `tools/test-flow/lib/evidence.mjs`
- `tools/test-flow/lib/identity.mjs`
- `tools/test-flow/lib/release-case.mjs`

### 主要专项与 Gate

- `tests/real/agent/test_real_wiki_skill_generation_gate.py`
- `tests/deterministic/unit/prototype/test_generation_blueprint_compiler.py`
- `tools/test-flow/tests/isolated-agent-tool-audit.test.mjs`
- `tools/test-flow/tests/isolated-agent-wrapper.test.mjs`
- `tools/test-flow/tests/release-inputs.test.mjs`
- `tests/deterministic/unit/integrations/test_skill_contract.py`
- `tests/deterministic/unit/integrations/test_generator_copy.py`
- `FIXED_ISSUES.md`
- `TODO.md`

## 4. 24 小时以上执行时间线

下表只列出能够由本地权威 `verdict.json` 重验的代表性 run。时间为 run ID 中的 UTC 时间；完整 94 个 verdict 仍保存在本地 evidence 目录。

| Run | 路线 / source | 权威结果 | 模型用量 | 主要结论 |
|---|---|---|---:|---|
| `run-20260818T135346Z-80d3e1e9` | 初始大对象 source `f1b13535…` / 609 | Dev PASS_WITH_WARNINGS | 0 | 零模型基线通过 |
| `run-20260818T144146Z-e49cee4b` | 完整 GenerationSpec | Release FAIL | usage 见本地 verdict | 读取多余示例并反复重构，1800 秒仍无最终 Write |
| `run-20260818T155925Z-f6f83c0d` | 完整 GenerationSpec / `d77056bf…` / 609 | Release FAIL | 471,332 / $6.683608 | 单响应 64k 上限截断 |
| `run-20260818T170644Z-aa8e683c` | 有界状态机 + 自由字符串 Write / 613 | Release FAIL | 903,218 / $5.354810 | 完成 Skill + 8 Reads + Write，但 146,007-byte JSON 无法解析 |
| `run-20260818T175029Z-a7f3b222` | 自由字符串 Write / 613 | Release FAIL | 780,531 / $3.587775 | wrapper 可提前分类 `SKILL_TRACE_WRITE_JSON_INVALID`，随后转原生 StructuredOutput |
| `run-20260818T190152Z-30a86d3d` | 原生 StructuredOutput / 613 | Release FAIL | 924,075 / $5.088739 | `error_max_turns`，终止协议未闭合 |
| `run-20260818T195757Z-eea3189d` | StructuredOutput / 613 | Release FAIL | 1,319,141 / 约 $5.044 | 连续五次 StructuredOutput、16 turns，证明 retry/terminal 设计失配 |
| `run-20260819T003721Z-b8cad111` | StructuredOutput constraint 诊断 / 613 | Release FAIL | 944,349 / $6.856281 | 首次 `{}`，第二次 144,173 B，仅 `output_requirements` 数量错误 |
| `run-20260819T042443Z-f79c4c06` | 完整根对象 / 613 | Release FAIL | 1,029,343 / $6.646835 | 两次均提交 `{}` |
| `run-20260819T051117Z-3ac62051` | 完整 GenerationSpec / `e3ae632d…` / 613 | Release FAIL | 882,479 tokens / $6.396563 | 14 turns；StructuredOutput retry terminal；CrossJob 未运行 |
| `run-20260819T054518Z-57092337` | 完整 GenerationSpec / `737e7028…` / 613 | Release FAIL | 830,032 / $4.011196 | 第二次提交仍未形成可交付 terminal；CrossJob 未运行 |
| `run-20260819T061847Z-116675e2` | 完整 GenerationSpec / `4d76671b…` / 613 | Release FAIL | 894,224 / $5.389720 | 生成过大对象，但首次提交/terminal 协议失败；语义审计未运行 |
| `run-20260819T065156Z-68a47363` | typed-frame 完整对象 / `7ec3a988…` / 613 | Release FAIL | 1,021,410 / $6.856722 | 仍以 StructuredOutput retry terminal 失败；CrossJob 未运行 |
| `run-20260819T085733Z-5113863a` | compact Rule IR 原型 / `a4296e0a…` / 616 | Dev PASS_WITH_WARNINGS | 0 | 只证明 deterministic compiler component，不能证明真实模型端到端可行 |
| `run-20260819T111551Z-54bd0272` | 首个生产 IR source `4edff649…` / 618 | Dev PASS_WITH_WARNINGS | 0 | full deterministic PASS，证明零模型 compiler 路径可运行 |
| `run-20260819T112446Z-7e68015f` | IR 校准 / `4edff649…` | dev.real FAIL | usage 不完整 | 13 stream events，无可用 IR/semantic evidence |
| `run-20260819T113041Z-685e4239` | IR 校准 / `4edff649…` | dev.real FAIL | usage 不完整 | 同上；随后转入 CLI/settings/MCP 诊断 |
| `run-20260819T152125Z-2df0a623` | IR / `4edff649…` | dev.real FAIL（模型未运行） | 0 | deterministic source-copy/cache origin 身份失败 |
| `run-20260819T152518Z-c7b17b9a` | IR / `4edff649…` | dev.real FAIL | 796,424 / $4.309208 | 12 turns、terminal success；`SKILL_TRACE_RULE_IR_INVALID`，当时 receipt 尚无 constraint pointer |
| `run-20260819T160744Z-e60da0a7` | v8 constraint source `747bbae2…` / 618 | Dev PASS_WITH_WARNINGS | 0 | full deterministic PASS；最终零模型验证快照 |
| `run-20260819T161046Z-f83a1349` | IR / `747bbae2…` | dev.real FAIL | 811,944 / $4.602584 | terminal success；IR 51,457 bytes，超过 48 KiB 上限 2,305 bytes |

上述四个 05:11Z–07:15Z 代表性完整对象 Release run 合计 3,628,145 tokens、$22.654201；两个有完整 usage 的生产 IR 校准合计 1,608,368 tokens、$8.911792。它们只是表内子集。全窗口权威聚合仍应使用本节开头的 17 次完整 usage：14,465,415 tokens、$94.538827。

期间还发生了以下非产品问题：

- 最小诊断最初写入无效 MCP 配置 `{}`，Claude CLI 要求顶层 `mcpServers`；这导致 97-byte、0-event 本地退出。
- 修正为 `{"mcpServers":{}}` 后，DNS/TCP/TLS/鉴权与 DeepSeek stream 得到真实成功证据。
- 两次旧校准使用的 settings fingerprint 与成功探针不同；统一到 `53c3a4a3…` 后 StructuredOutput 最小探针成功。
- cache checkout 的 origin 被改写为 `/mcp`；切换到含冻结 commit 且 origin 正确的只读 checkout 后，校准才真正进入 IR compiler。

这些修复消除了基础设施噪声，但没有改变最终端到端失败。

## 5. 已证明与未证明

### 已证明

- A+B+C 合并代码可在两个 IR 候选 source 上通过权威零模型 Dev full deterministic。
- 通用、确定性的 RuleFamily compiler 能把已给定 blueprint 展开为完整结构，并能 fail closed。
- wrapper 可以封存 IR/compiler/output 三方身份，并保留无内容泄漏的 constraint evidence。
- DeepSeek endpoint、鉴权、Claude CLI StructuredOutput 和 usage 采集在最小探针上可用。

### 未证明

- 真实模型能在 48 KiB 内稳定生成完整、语义正确的 IR。
- 模型输出可以通过完整 GenerationSpec 深层 validator。
- 产品语义 audit 可以通过。
- 九个业务场景可以通过。
- 六个 Release CrossJob Stage 可以通过。
- 当前架构能够实现用户最初期望的“由定位指导 Skill 驱动模型逐步定位问题”。

最终一次模型输出只比 IR cap 多 2,305 bytes，但压缩这部分只能修复当前样本，不能证明架构可重复。继续增加 token、重写 prompt 或压缩同一大对象会延续同一种高成本失败模式。

`FIXED_ISSUES.md` 的最新 verdict 元数据仍停留在较早的 `run-20260819T061847Z-116675e2`；正文虽记录了后续 IR 历史，但没有把 `run-20260819T161046Z-f83a1349` 登记为已验证修复。该状态是正确的 pending/失败归档，不得补写成 PASS。

## 6. 架构偏移复盘

用户最初期望的是：Skill 提供定位方法，模型按轮次提问、收集事实、形成假设、调用稳定 MCP 工具验证，并输出小型结构化 `next_action` 或结论。

本归档实现实际演变为：模型生成一份大型诊断程序或其 IR，compiler 展开后再运行该程序。模型从“按 Skill 办案的诊断助手”变成了“诊断规则编译器的代码生成器”。

关键流程错误：

1. 把模型/Skill/compiler 的责任边界当作实现细节，没有在编码前单独进行架构确认。
2. 把 compiler-only 的零模型可行性错误解释为真实模型端到端可行性。
3. 让测试可复验要求逐步反向塑造产品流程。
4. 在 semantic audit 和九场景从未开始的情况下，继续把问题归因于 prompt、terminal、settings 或尺寸。
5. 多次小修需要完整模型重跑，缺少“小提交 → 精确诊断 → 小补丁”的增量恢复通道。

因此该路线应视为历史失败分支，而不是下一轮实现的默认基线。

## 7. Evidence 保留与安全说明

权威原始 evidence 继续保留在该 worktree 的忽略目录 `.tmp/test-flow-evidence/`，没有删除。它不随 Git 分支提交，原因是其中包含大量运行时产物、机器本地路径及受控 provider evidence；本归档只记录安全摘要、run ID 和 verdict SHA-256。

| Run | `verdict.json` SHA-256 |
|---|---|
| `run-20260819T051117Z-3ac62051` | `c100383bab3a7905dd8b00fc1055ec24690ec722281f21e96528bcaabd4475cf` |
| `run-20260819T054518Z-57092337` | `92cf2db3991b81ad3092eb4032a8fc86dfbf0b5bf361a1db6badb43437a58610` |
| `run-20260819T061847Z-116675e2` | `6b74922b927fa4900f1b02a104e79a0c856ead763c923bc9c4eec7295c2804fb` |
| `run-20260819T065156Z-68a47363` | `fccb4da14cac1003d3e44b31ee06bbb43d1e2684469cf4b001c5bf7bc6ab9c87` |
| `run-20260819T111551Z-54bd0272` | `55b2da83a4fae6dcb2b2410567a3d938af94bbdeb3ca6d4cc02bd904c23c2b63` |
| `run-20260819T112446Z-7e68015f` | `7659165fbd9a7f12d87d2a4a45cd18efe5ce3502ba6679639ec41d2a6aa85ca3` |
| `run-20260819T113041Z-685e4239` | `da860b3a7d155278c0997717cc0ed6775407b1cdbd97b1d5f4d05850fea9c7db` |
| `run-20260819T152125Z-2df0a623` | `d5639935347cbe4db6290dda706aedfdb50220260bc5ed363b4402ca6f4456c2` |
| `run-20260819T152518Z-c7b17b9a` | `aa4d3215407e6484a9ab52fb4c8ae24aa45a22e7876b61ac6cd2d00895b25346` |
| `run-20260819T160744Z-e60da0a7` | `016111ba36c10d087661fd499bbbdf22bac4ed48e3d95e17efb4bdb54467d54e` |
| `run-20260819T161046Z-f83a1349` | `71aebaf78b02383b12d2ede2611223c8ebdd438bbd27964bdf0ed9e119c6d126` |

未提交、也不应提交：provider token/header、原始 provider error、原始模型正文、完整 stream、用户凭据或可能重构敏感输入的 payload。所有已列 verdict 的 secret scan 均由 Test Flow 管理；本归档不改变其权威性。

## 8. 使用限制

- 不得把该分支描述为已修复、已验证或可发布。
- 不得从任何旧 PASS 推导最终 Release PASS。
- 若复用 compiler 或 Test Flow 组件，必须重新确认它们是否服务于新的 Skill 驱动产品流程，而不是继续生成大型诊断程序。
- 新方案应先完成产品流程和模型/Skill/MCP/确定性代码责任边界确认，再做低成本真实模型垂直切口。
- 本分支只用于审计、复盘、比较和按需提取独立组件。
