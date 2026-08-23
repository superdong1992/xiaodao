# 生成物合同

生成目录中只能包含以下内容：

```text
<skill-name>/
|-- SKILL.md
|-- methods.json
`-- references/
    |-- <method-id>.md
    `-- <shared-topic>.md
```

不要生成旧版 manifest、GenerationSpec、README、测试框架或复制的 Wiki。

## SKILL.md

- frontmatter 只要求 `name` 和 `description`；`name` 必须与目录名及 `methods.json.skill_name` 相同。
- 入口保持简短，说明 `request.json`、共同诊断流程、按需读取引用的规则和结果边界。
- 明确只读取冻结的 `target_logs[*].log_path`，不调用 Logparse，不遍历其他日志。
- 明确读取 `methods.json` 后先扫描全部正向标记，不能在第一个命中处分支短路。
- 明确检查输入范围内全部相关调用；只有证据足以证明属于同一次调用时才合并发现。
- 结果必须保留证据不足、观测限制及 Wiki 中的安全提醒。

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
  "shared_references": ["references/<shared-topic>.md"],
  "methods": [
    {
      "id": "<stable-kebab-case-id>",
      "title": "<short title>",
      "reference": "references/<method-id>.md",
      "priority": 1,
      "evidence_markers": ["<literal substring present in the Wiki>"]
    }
  ]
}
```

- `required_user_inputs` 只列 Wiki 明确要求用户提供的标量参数。
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
| 日志中的请求 ID | `request_id` |
| 日志中的超时时间（毫秒） | `timeout_ms` |

其他含义使用简短、无多余 `_name` 或 `_info` 后缀的 `snake_case` ID。

- 方法 ID、引用和 priority 都必须唯一；priority 从 1 连续递增。
- `evidence_markers` 使用 Wiki 原文中的短字面量，保持大小写。把能够独立确认该方法的每种正向日志类型都列入索引，避免只出现其中一种日志时无法加载方法卡。
- 方法按 Wiki 给出的可能性或诊断顺序排列；不要把顺序解释成互斥。
- 共同症状、失败入口或请求关联日志不是原因路由标记时，不必复制到每个方法；但必须在共享引用中保留 Wiki 原文的可搜索字面标记和字段含义。

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

如果 Wiki 说明某条日志只有在对应阈值或条件已经满足时才会打印，把观测到该日志写入“确认条件”，不能只称为补充证据。

## 共享引用

共享引用没有固定标题，但只放多个方法共同遵守的内容，例如：

- 输入和证据的作用域；
- 用户输入、附件和日志派生字段的自然语言含义；
- 不能区分具体原因、但用于确认问题发生或关联目标请求的日志模板；
- 抑制、限流、采样带来的不可观测性；
- 结果措辞和副作用提醒。

不要在共享引用中增加 Wiki 未提供的阈值或经验结论。

当目标日志包含多次相关调用时，逐项保留所有满足正向确认条件的发现。使用日志已有的请求标识或时间字段区分调用；证据不足以证明属于同一次调用时不得强行合并。
