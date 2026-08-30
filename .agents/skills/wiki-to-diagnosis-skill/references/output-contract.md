# 生成物合同

生成目录中只能包含以下内容：

```text
<skill-name>/
|-- SKILL.md
|-- methods.json
`-- references/
    |-- source-log-templates.md
    |-- <method-id>.md
    `-- <shared-topic>.md
```

不要生成旧版 manifest、GenerationSpec、registration template、README、测试框架或复制的 Wiki。Registration 由产品运行时在生成包之外管理。

## SKILL.md

- frontmatter 只要求 `name` 和 `description`；`name` 必须与目录名及 `methods.json.skill_name` 相同。
- 入口保持简短，说明读取冻结 `request.json`、Server 生成的 `method-evidence-graph.json` 和
  `method-evaluation-plan.json`，并按需读取方法卡和共享引用。
- 明确方法规则需要用户输入时读取 `request.json` 中的冻结值。
- 明确日志证据只能来自 Evidence Graph 和 Evaluation Plan；不读取目标日志、不重新扫描 marker，
  也不重新选择日志。
- 明确按 Evaluation Plan 顺序评估全部 `evaluation_ref`，不能在第一个确认项后停止。
- 明确每项只输出 `evaluation_ref`、`verdict`、`supporting_event_refs` 和 `reason`。
  `CONFIRMED` 必须按计划顺序选择当前 evaluation 的非空 event ref 子集；`REJECTED` 或
  `UNKNOWN` 必须使用空数组。
- `reason` 只概括方法规则判断，不回抄 marker、日志原文、行号、哈希或事件身份。
- 证据不足或受 Wiki 观测限制影响时使用 `UNKNOWN`，并在 `reason` 中说明边界。

## methods.json

使用以下精简结构，不增加其他字段：

```json
{
  "schema_version": 1,
  "skill_name": "<skill-name>",
  "source_wiki_sha256": "<64 lowercase hex>",
  "required_user_inputs": ["<snake_case-id>"],
  "required_artifacts": ["<snake_case-id>"],
  "log_derived_fields": ["<snake_case-id>"],
  "shared_references": [
    "references/source-log-templates.md",
    "references/<shared-topic>.md"
  ],
  "methods": [
    {
      "id": "<stable-kebab-case-id>",
      "title": "<short title>",
      "reference": "references/<method-id>.md",
      "priority": 1,
      "evidence_markers": ["<literal substring present in the Wiki>"],
      "activation_markers": ["<ordered activation marker from evidence_markers>"]
    }
  ]
}
```

- `required_user_inputs` 只列 Wiki 明确要求用户提供的标量参数。
- `source_wiki_sha256` 是调用方绑定原始 Wiki 字节的 source identity。调用方提供 identity 文件时，
  必须逐字复制其中的 `sha256`；没有 identity 时只能用确定性哈希工具从原始 Wiki 字节计算。禁止
  猜测、根据文本重建、规范化换行或由校验器事后改写生成包。
- source identity v2 的 `log_templates` 是从相同 Wiki 字节机械派生的完整性清单，不是新的业务事实源。
  生成时必须逐项、逐序复制到固定的 `references/source-log-templates.md`；不得重排、去重、改写字段
  占位符或只保留 marker。旧版 source identity 不属于本合同。
- `required_artifacts` 只列 Wiki 明确要求用户提供的日志或其他附件；需要由 Logparse 消费的日志包使用稳定 ID `log_archive`。
- `log_derived_fields` 列出 Wiki 明确说明只能从日志获得、且不能要求用户提供的字段。
- 三组 ID 使用小写 `snake_case`，组内唯一且三组之间不得重复。Wiki 没有声明某一类时使用空数组。

当 Wiki 使用以下常见含义时，使用稳定 ID，避免为同一输入生成不同别名：

| Wiki 含义 | ID |
| --- | --- |
| 问题、故障或超时发生时间 | `problem_time` |
| 客户端或调用端进程信息 | `client_process` |
| 服务端或被调用端进程信息 | `server_process` |
| 服务名 | `service` |
| API 名 | `api` |
| 供 Logparse 处理的日志或日志包 | `log_archive` |
其他含义使用简短、无多余 `_name` 或 `_info` 后缀的 `snake_case` ID。

Wiki `text` 日志模板使用 `{field_name}` 命名字段时，先按模板及模板内的首次出现顺序收集所有唯一
`field_name`，再删除已经位于 `required_user_inputs` 的字段；`log_derived_fields` 必须与剩余列表
逐项、逐序一致，不得翻译、增加前后缀或重新排序。没有命名占位符时，才根据 Wiki 的自然语言说明
选择简短 ID；这类 ID 放在命名字段之后。

- 方法 ID、引用和 priority 都必须唯一；priority 从 1 连续递增。
- `shared_references` 必须非空，第一项固定为 `references/source-log-templates.md`。该文件不得用作任何
  method 的 `reference`；其他共享引用如有需要排在其后。
- Wiki 有明确原因列表时，每个列表原因对应一个方法。属于同一原因的不同日志、检测条件和观测阶段合并为该方法的多个证据标记，不按日志种类另建方法。
- `evidence_markers` 必须覆盖该方法在确认、排除、计算和关联目标请求时需要读取的全部日志模板，不只
  包含直接表示原因的日志。使用 Wiki 日志模板机械提取的 canonical stable marker，保持大小写。模板在第一个
  `{field}` 或 `%x` 占位符前存在非空字面前缀时，marker 必须是该完整前缀去除首尾空白后的精确
  字节；模板以占位符开头时，使用占位符之间或最后一个占位符之后最长的非空字面片段（长度相同取
  最早者），同样只去除首尾空白。不得截短前缀、保留占位符或改选另一个片段。每个 marker 必须在当前方法卡的“所需
  证据”段对应至少一条逐字完整的源模板；该段逐字出现的每条源模板所派生的 canonical marker
  也必须被索引。其他段落中的 marker 子串不参与闭包。`evidence_markers` 按
  `references/source-log-templates.md` 的源模板顺序排列。
  例如，`API_COMPLETE service={service} api={api} ...` 的 marker 是
  `API_COMPLETE service=`，不是 `API_COMPLETE`；`QUEUE_HISTORY print_time_ms={print_time_ms} ...`
  的 marker 是 `QUEUE_HISTORY print_time_ms=`，不是 `QUEUE_HISTORY`。
- `activation_markers` 必填、非空且组内唯一，并且必须是当前方法 `evidence_markers` 的保序子序列。
  它只列“出现后值得为该方法创建 evaluation”的 marker。activation 命中不表示单条日志足以确认
  原因；Agent 仍须读取完整 Evidence Graph，并按方法卡判断。公共症状只能作为 context，不能用于
  激活所有原因；公共 RPC timeout 日志只能放在 `evidence_markers` 中作为判断上下文。同一 literal
  如果确实会触发多个方法，可以分别出现在这些方法的 `activation_markers` 中。
- 方法按 Wiki 给出的可能性或诊断顺序排列；不要把顺序解释成互斥。
- 共同症状、失败入口或请求关联日志的解释可以只写一次共享引用；但只要某个方法会根据该日志是否
  出现、其中的字段或它与其他日志的关联作出判定，就必须把 canonical marker 写入该方法的
  `evidence_markers`，并在该方法卡“所需证据”中列出完整模板。只存在于共享引用不算进入该方法的
  Evidence Graph。

调用方提供的 source identity v2 使用以下闭合结构，不增加其他字段：

```json
{
  "algorithm": "sha256",
  "log_template_extraction_version": 1,
  "log_template_inventory_sha256": "<64 lowercase hex>",
  "log_templates": ["<complete template line>"],
  "schema_version": 2,
  "sha256": "<64 lowercase hex>",
  "source_path": "<Wiki path>"
}
```

`log_template_inventory_sha256` 由调用方绑定清单，生成 Agent 不计算或改写它；生成 Agent 只按清单完成
固定引用，并由调用方在启动前和生成后独立核对 identity、Wiki 与生成字节。

## 方法卡

每张方法卡必须包含以下标题：

```markdown
# <方法标题>

## 适用条件
## 所需证据
## 计算与判断
## 确认条件
## 未知边界
## 输出含义
```

在相应段落中保留 Wiki 的字段关联、单位换算、阈值、分组、目标选择和多贡献者规则。确认条件只能建立在正向证据上；日志缺失策略放在“未知边界”。

“所需证据”必须列出该方法判断、计算、排除或关联目标请求时会读取的每种完整日志模板，包括共同
症状和请求关联日志。对应 canonical marker 必须与 `evidence_markers` 双向一致，并按固定源模板
清单顺序排列。validator 只在“所需证据”段查找逐字完整模板；仅写 marker 子串、在其他段落提到
marker，或只把模板放入共享引用，都不能建立闭包。共享引用可以承载共同字段含义和观测边界，但
不能替代这里的方法归属和索引。

`activation_markers` 不改变“所需证据”闭包，也不要求另建方法卡段落。它只从已经闭合的
`evidence_markers` 中选择触发项；未入选 activation 的 marker 仍会作为已激活 evaluation 的上下文。

如果 Wiki 说明某条日志只有在对应阈值或条件已经满足时才会打印，把观测到该日志写入“确认条件”，不能只称为补充证据。

“输出含义”必须说明：Server 会把同一方法的全部独立事件绑定到该方法的 `evaluation_ref`；Agent
返回该引用、判定、从当前计划项选择的 `supporting_event_refs` 和简短原因。Agent 不复制 marker、
日志原文、行号、哈希、identity token 或 hit ref；Server 从双方一致选择的 event ref 机械派生 hit。

## Methods V2 评估输出

运行结果是一个根 JSON 数组，顺序与 `method-evaluation-plan.json` 完全一致。每项只能包含：

- `evaluation_ref`：逐字复制对应计划项的引用。
- `verdict`：`CONFIRMED`、`REJECTED` 或 `UNKNOWN`。
- `supporting_event_refs`：`CONFIRMED` 时按计划顺序选择当前 evaluation 的非空 event ref 子集；
  `REJECTED` 或 `UNKNOWN` 时使用空数组。
- `reason`：非空的规则判断摘要。

不得增加、遗漏、重复或重排计划项。不得输出 `method_id`、marker、日志原文、行号、哈希、
`identity_tokens`、hit ref 或其他证据字段。Server 负责保存 Evidence Graph，并把双方一致选择的
event ref 映射到最终结果。

## 固定源日志模板引用

`references/source-log-templates.md` 始终存在，即使 Wiki 没有机械日志模板。其 UTF-8 字节必须严格按
下列格式生成，使用 LF 和终止换行；`<template lines>` 是 source identity v2 `log_templates` 中的每一项，
保持原顺序和重复项，一项一行，不增加项目符号、解释或空白：

````text
# Source log templates

```text
<template lines>
```
````

机械提取版本 1 的规则是：只进入首尾去空白后恰为三个反引号加 `text` 的 fence 起始行；对 fence 中每个非空行去除
首尾空白；只收集包含 `{named_field}` 或 `%x` 形式占位符的整行；按首次遇到的顺序保留每次出现，
不去重。固定文件只承载完整性清单；模板用途、字段关联、阈值和观测边界仍来自 Wiki，并写入相应方法卡
或其他共享引用。

## 其他共享引用

除固定模板清单外，其他共享引用没有固定标题，只放多个方法共同遵守的内容，例如：

- 输入和证据的作用域；
- 用户输入、附件和日志派生字段的自然语言含义；
- 多个方法共用的日志字段含义和关联规则；方法实际需要读取的模板仍须在相应方法卡与索引中列出；
- 抑制、限流、采样带来的不可观测性；
- 结果措辞和副作用提醒。

不要在共享引用中增加 Wiki 未提供的阈值或经验结论。

当 Evidence Graph 中同一方法包含多次相关调用时，按方法卡规则评估该计划项覆盖的全部事件；
证据不足以证明事件关系时返回 `UNKNOWN`，不得自行重组 Evidence Graph。
