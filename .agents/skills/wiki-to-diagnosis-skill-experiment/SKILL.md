---
name: wiki-to-diagnosis-skill-experiment
description: Convert an authored troubleshooting Wiki into one evidence-driven diagnosis skill with independently loadable method cards. Use for feasibility experiments that must preserve the Wiki's meaning; do not use to diagnose an incident directly.
---

# Wiki 转定位 Skill 实验

把用户提供的定位 Wiki 转成一个可直接使用的 Codex Skill。生成物必须忠实表达 Wiki，而不是补写经验、改写阈值，或把示例扩成普遍规则。

## 工作方式

1. 完整阅读 Wiki，计算原始文件的 SHA-256。不要修改 Wiki。
2. 提取 Wiki 明确要求用户提供的参数、附件，以及只能从日志中读取的字段。不要把日志字段改成用户参数，也不要自行增加 Wiki 未要求的输入。
3. 从 Wiki 中识别能够凭独立证据确认的定位方法。方法按“判断一种原因所需的证据和计算”拆分，不按标题数量或日志数量机械拆分。
4. 把所有方法都要遵守的输入含义、观测限制、安全语义和证据边界提取为共享引用，避免在每张卡里重复。
5. 先清点 Wiki `text` 代码块中全部带字段占位符的稳定日志模板，再为每种方法选择 Wiki 原文中真实存在的简短日志标记。标记用于先扫描证据、再按需加载方法卡；不得发明 Wiki 中没有的标记，也不得丢失只确认问题发生或用于关联的公共模板。
6. 不能区分具体原因、但用于确认问题发生或关联目标请求的稳定日志模板，放入共享引用。不得因为它不是分支标记就从生成物中丢失。
7. 按用户指定的目录和名称生成一个 Skill。生成前先阅读 [输出合同](references/output-contract.md)，严格使用其中的文件结构和字段。
8. 生成后运行本 Skill 的校验脚本。校验失败时只修正被报告的结构问题；不要借机改变 Wiki 语义。

## 拆分原则

- 一个方法对应一种可单独成立的原因判断；同一条日志可以为多个方法提供证据。
- 只要 Wiki 允许多个原因同时成立，生成的 Skill 就必须检查全部正向证据，不能命中第一条后停止。
- 不预设目标日志中只有一次相关调用。检查全部符合用户输入范围的正向证据；只有日志字段足以证明属于同一次调用时才合并，否则分别保留发现。
- 有抑制、限流、采样或条件打印时，日志缺失只能形成未知边界，不能自动排除原因。
- 单位换算、时间段、集合分组、目标记录选择和贡献者计算必须保留 Wiki 给出的精确规则。
- Wiki 明确说明某条日志只在确认条件已经满足时打印，则观测到该日志本身就是相应方法的正向确认证据，不能降级成不影响结论的补充信息。
- Wiki 没有说明、而且会实质改变结论的信息，必须报告为作者待确认项；不能自行补默认值。
- 方法卡可以引用共享边界，但不能依赖未列入 `methods.json` 的隐藏文件。
- Wiki 给出的每种稳定日志模板都必须在方法卡或共享引用中保留可搜索的字面标记；方法索引只承担原因路由，不必把共同症状标记复制到每个方法。

## 运行时边界

完整使用入口接收 Wiki 声明的用户参数和日志附件。运行器先用其中与日志选择有关的输入完成一次 Logparse 预处理；生成的定位 Skill 在诊断阶段消费 `request.json`、已经冻结的 Logparse `target_logs` 和 receipt：

- `request.json` 保留用户参数和已消费日志附件的身份；不得要求用户补充 `log_derived_fields` 中的字段。
- 只读取 `target_logs[*].log_path` 明确列出的日志。
- 不调用 Logparse，不遍历解析目录，不重新选择生命周期、进程或日志路径。
- 先读取 `methods.json`，扫描所有目标日志中的证据标记，再只加载相关方法卡和共享引用。
- 检查输入范围内全部相关调用；证据摘要使用日志中已有的关联字段或时间段区分不同调用。
- 没有足够正向证据时返回证据不足，并明确缺失证据可能受到 Wiki 所述观测策略影响。

## 校验

在生成工作区根目录执行：

```bash
python3 .agents/skills/wiki-to-diagnosis-skill-experiment/scripts/validate_generated_skill.py \
  --skill-dir <生成的-skill-目录> \
  --wiki <原始-wiki-路径> \
  --json
```

校验器只验证文件结构、索引、引用、原文标记和哈希；它不替代实际场景诊断。
