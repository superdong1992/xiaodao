---
name: wiki-to-diagnosis-skill
description: Convert an authored troubleshooting Wiki into one evidence-driven diagnosis Skill with independently loadable method cards. Use when building or regenerating a specialized locator; do not use it to diagnose an incident directly.
---

# Wiki 转定位 Skill

把用户提供的定位 Wiki 转成一个可直接使用的 Codex Skill。生成物必须忠实表达 Wiki，而不是补写经验、改写阈值，或把示例扩成普遍规则。

## 工作方式

1. 完整阅读 Wiki 并取得原始字节的 SHA-256。调用方提供与该 Wiki 字节绑定的 source identity 时，
   必须读取并逐字复制其中的 digest；未提供时只能使用确定性的哈希工具计算。source identity v2 还会
   给出从同一 Wiki 机械提取的 `log_templates` 完整性清单；它不是新的业务事实源，必须与 Wiki 一起
   使用。没有 identity 或哈希工具时停止并要求调用方补充，禁止心算、猜测、归一化 Wiki 或事后回填
   digest。不要修改 Wiki。
2. 提取 Wiki 明确要求用户提供的参数、附件，以及只能从日志中读取的字段。不要把日志字段改成用户参数，也不要自行增加 Wiki 未要求的输入。
3. 从 Wiki 中识别能够凭独立证据确认的定位方法。方法按“判断一种原因所需的证据和计算”拆分，不按标题数量或日志数量机械拆分。
4. 把所有方法都要遵守的输入含义、观测限制、安全语义和证据边界提取为共享引用，避免在每张卡里重复。
5. 先清点 Wiki `text` 代码块中全部带字段占位符的稳定日志模板。source identity v2 已提供
   `log_templates` 时，把它作为逐项、逐序、逐字完成清单；仍以 Wiki 决定模板的业务含义。把完整清单
   写入固定共享引用 `references/source-log-templates.md`，并按输出合同的机械规则提取 canonical
   stable marker。对每种方法，把确认、排除、计算或关联目标请求时实际需要读取的全部日志模板纳入
   该方法的 marker 索引。标记用于先扫描证据、再按需加载方法卡；不得自行截短、改选片段、重排、
   去重或丢失模板。
6. 不能区分具体原因、但用于确认问题发生或关联目标请求的模板语义可以放入其他共享引用；无论如何，
   所有机械模板都必须逐字出现在固定模板清单文件中。只要某个方法会使用该日志是否出现、日志字段
   或与其他日志的关联来得出判定，就必须同时把 canonical marker 按源模板顺序写入该方法的
   `evidence_markers`，并把对应完整模板逐字写入该方法卡的“所需证据”段。其他段落中的 marker
   字样或共享解释都不能替代方法索引。
7. 为每张方法卡写清楚可机械执行的确认、排除和未知条件。Server 会把一次扫描得到的 Evidence Graph
   和完整 Evaluation Plan 交给 Agent；冻结 `request.json` 继续提供方法规则所需的用户输入。
   生成的 Skill 只负责让 Agent 能按方法规则判断每个 `evaluation_ref`，不要求 Agent 回抄
   marker、日志原文、行号、哈希或事件身份。
8. 按用户指定的目录和名称生成一个 Skill。生成前先阅读 [输出合同](references/output-contract.md)，严格使用其中的文件结构和字段。
9. 生成后运行本 Skill 的校验脚本。校验失败时只修正被报告的结构问题；不要借机改变 Wiki 语义。

## 拆分原则

- 一个方法对应一种可单独成立的原因判断；同一条日志可以为多个方法提供证据。
- Wiki 明确列出原因分类时，以该分类作为方法边界。后续日志、检测条件或观测方式默认是相应原因的证据分支；除非 Wiki 明确把它声明为另一原因，否则不能另拆方法。
- 只要 Wiki 允许多个原因同时成立，生成的 Skill 就必须检查全部正向证据，不能命中第一条后停止。
- 不预设目标日志中只有一次相关调用。检查全部符合用户输入范围的正向证据；只有日志字段足以证明属于同一次调用时才合并，否则分别保留发现。
- 原因相同但事件不同也要分别输出证据。摘要不能代替证据身份；每条证据都要原样引用所用冻结日志，并保留可区分该事件的日志字面量。
- 有抑制、限流、采样或条件打印时，日志缺失只能形成未知边界，不能自动排除原因。
- 单位换算、时间段、集合分组、目标记录选择和贡献者计算必须保留 Wiki 给出的精确规则。
- Wiki 明确说明某条日志只在确认条件已经满足时打印，则观测到该日志本身就是相应方法的正向确认证据，不能降级成不影响结论的补充信息。
- Wiki 没有说明、而且会实质改变结论的信息，必须报告为作者待确认项；不能自行补默认值。
- 方法卡可以引用共享边界，但不能依赖未列入 `methods.json` 的隐藏文件。
- Wiki 给出的每种稳定日志模板都必须在 `references/source-log-templates.md` 中按源顺序完整保留。
  `evidence_markers` 不只承担原因路由，还必须让 Evidence Graph 收齐该方法判断、计算、排除和请求关联
  所需的日志。共同日志可以共享解释，但凡方法会读取其出现情况或字段，就要在每个适用方法的
  “所需证据”中逐字列出完整模板，并在该方法中索引。

## 运行时边界

完整使用入口接收 Wiki 声明的用户参数和日志附件。运行器先完成 Logparse 预处理，再由 Server 扫描
一次冻结日志，生成 `method-evidence-graph.json` 和 `method-evaluation-plan.json`。生成的定位 Skill
在评估阶段同时读取冻结 `request.json`，并遵守以下边界：

- `request.json` 提供 Wiki 声明的用户输入；方法规则需要某项输入时使用其冻结值。
- `method-evidence-graph.json` 是本次评估的完整证据集合，`method-evaluation-plan.json` 是完整待判定清单。
- 日志证据只能来自 Evidence Graph 和 Evaluation Plan；不读取目标日志，不重新扫描 marker，
  不重新选择生命周期、进程或日志路径。
- 按 Evaluation Plan 顺序逐项应用对应方法卡；不能在第一个确认项后停止。
- 每个输出项只能包含 `evaluation_ref`、`verdict` 和 `reason`。`verdict` 只能是
  `CONFIRMED`、`REJECTED` 或 `UNKNOWN`。
- 没有足够证据时返回 `UNKNOWN`，并在 `reason` 中说明 Wiki 给出的观测限制；不补写 Wiki 未提供的事实。

## 校验

在生成工作区根目录执行：

```bash
python3 .agents/skills/wiki-to-diagnosis-skill/scripts/validate_generated_skill.py \
  --skill-dir <生成的-skill-目录> \
  --wiki <原始-wiki-路径> \
  --json
```

校验器只验证文件结构、索引、引用、原文标记和哈希；它不替代实际场景诊断。
