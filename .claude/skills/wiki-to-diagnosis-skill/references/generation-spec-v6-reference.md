# GenerationSpec v6 精确参考

本文是转换 Agent 构造 `GenerationSpec v6` 的自包含唯一合同。版本常量为
`generator_version="6.0.0"`、`schema_version=6`。转换 Agent 不读取 generator 或 Runtime
源码反推格式。verification contract 使用独立的
[verification-contract-v2-reference.md](verification-contract-v2-reference.md)。

## 1. 通用约定

- exact keys：对象必须恰好包含声明的 key。
- required nullable：key 必须出现，值可以是 JSON `null`。
- Name：`^[a-z][a-z0-9_]{0,63}$`。
- canonical single line：非空、无首尾空白和换行；默认不超过 4,096 UTF-8 bytes。
- normalized text：非空 UTF-8 文本，CRLF/CR 归一为 LF 后去除首尾空白。
- 数组保持声明顺序；ID/name 在各自作用域唯一。
- 所有 `confirmed` 必须是 JSON boolean `true`；字符串 `"true"` 无效。

### 1.1 文本字段的 authoring 语义

以下约定只定义既有字段的 authoring 语义，不增加 key，也不改变后续章节声明的 schema、类型、
数量或规范化约束：

- `(# ... #)` 与 `（# ... #）` 旁注是转换元数据。应用字段映射前，先从输入剔除每个旁注的
  起止标记及其整个正文，只把标记外正文解释为业务事实。旁注正文只能用于排除审计；临时禁止集合仅包含旁注中未由
  标记外正文或权威澄清独立支持的实质内容。旁注与外部来源语义重叠时，只能依据标记外正文或
  权威澄清生成并记录具体源映射，不得因旁注重复而删除合法事实，也不得借旁注补足外部来源未声明的
  限定。绝不能用旁注正文理解、补全、修正或推断业务语义；旁注标记、旁注独有的逐字或独特片段，
  以及临时禁止集合中的内容均不得复制、改写、概括或转成约束及任何 GenerationSpec 字段值。
  发现未闭合、嵌套或交叉标记时停止并澄清，不得猜测边界。
- 在唯一最终 `Write` 前，递归遍历待写 GenerationSpec 的所有对象和数组并检查每一个字符串值。
  为其中每项语义及限定确认到标记外正文或权威澄清的具体源映射。任一值含旁注标记、旁注独有的
  逐字或独特片段，或复制、改写、概括外部来源未独立支持的旁注内容时，立即丢弃整份草稿；最多允许
  一次从标记外正文与权威澄清重新构造并重新递归检查，不能就地删改命中字段。该次复检仍失败时
  立即停止并请求澄清，不得再次重构或 `Write`。语义重叠且源映射独立支持完整语义及限定时，不得
  因旁注重复而删除合法事实。复检通过前不得 `Write`。
- `judgement_rules` 承载影响是否可安全判断或采取行动的判定义务，包括条件、禁止、限制、例外、
  不确定性及相关风险后果。
- `output_requirements` 承载最终用户必须看到的警示、限制、注意事项和风险后果。
- 同一输入陈述同时承担安全判断与用户告知义务时，必须分别写入两个字段；不得因一个字段已有内容而
  省略另一个字段的对应语义。
- 写入前逐条核对输入中的义务、否定、条件与适用范围、确定性/可能性强度和风险后果。允许忠实改写，
  不要求逐字复制；只有输入明确要求固定措辞、原文引用或逐字保留时才逐字写入。
- 不得把“可能”强化为“必然”，不得删除否定、例外或风险后果，也不得用通用安全套话替代输入中
  实际声明的义务。

## 2. 根对象

必填 key：

```text
schema_version, generator_version, id, version, capability, deployment_scope,
summary, chinese_title, module_name, problem_scope, roles, requirements,
logparse_plan, verification_contract, time_characteristics, analysis_steps,
judgement_rules, output_requirements, assumptions, requires_logparse
```

唯一可选 key 是 `logparse_product`。

| Key | 类型与约束 |
| --- | --- |
| `schema_version` | integer 6 |
| `generator_version` | string `6.0.0` |
| `id` | `^diagnose-[a-z0-9]+(?:-[a-z0-9]+)*$`，最多 64 bytes |
| `version` | 三段 SemVer，major >= 6 |
| `capability` | `^[a-z][a-z0-9-]{1,63}$` |
| `deployment_scope` | `PRODUCTION` 或 `TEST_ONLY` |
| `summary` | canonical single line |
| `chinese_title` | canonical single line，最多 256 bytes |
| `module_name` | required nullable canonical single line |
| `problem_scope` | normalized text |
| `roles` | 0..20 个 Role |
| `requirements` | 0..64 个 Wiki Requirement；不包含 profile 字段 |
| `logparse_plan` | required nullable LogparsePlan |
| `verification_contract` | schema v2 |
| `time_characteristics` | 0..100 个唯一 normalized text |
| `analysis_steps` | 1..100 个唯一 normalized text |
| `judgement_rules` | 1..100 个唯一 normalized text |
| `output_requirements` | 1..100 个唯一 normalized text |
| `assumptions` | 0..100 个唯一 normalized text |
| `requires_logparse` | JSON boolean |
| `logparse_product` | 可省略/null/非 `default` 字符串 |

## 3. 作者确认

模型先提出 role 与 Wiki 参数候选，并向作者展示 label/name、必选性、条件和具体 Wiki 来源。
完整权威澄清文件可以直接完成确认；缺失或冲突项必须询问作者。只有作者已经确认的项才可写入
最终 GenerationSpec，并且每项都必须带非空 `source_reference` 与 `confirmed=true`。

未确认完整时不得生成 GenerationSpec。不得依据模型置信度、字段被规则引用、既有用例习惯或
generator 报错自行补写确认。

## 4. Role

Role exact keys：

```text
{ label, description, presence, source_reference, confirmed }
```

- `label`：Name；所有 role 唯一。
- `description`：canonical single line，最多 512 bytes。
- `presence`：`REQUIRED` 或 `OPTIONAL`。
- `source_reference`：作者可核对的 Wiki/澄清来源，非空单行。
- `confirmed`：必须为 `true`。

Logparse anchors 与 roles 按相同顺序一一对应。Logparse Skill 至少一个 REQUIRED role；非
Logparse Skill 的 roles 必须为空。

Runtime 语义：REQUIRED role 始终激活。OPTIONAL role 完全没有任何
`<role>_slot|<role>_process_name|<role>_pid` 事实时不激活；任一字段出现后激活，并要求补齐
slot 与 process_name。pid 始终可选。

## 5. 内置 input profile

GenerationSpec 不声明以下保留参数，generator 从唯一内置 profile 自动注入 manifest：

| 名称 | 阶段 | 必选性 |
| --- | --- | --- |
| `problem_time` | INITIAL | REQUIRED |
| `<role>_slot` | INITIAL | REQUIRED（仅已激活 role） |
| `<role>_process_name` | INITIAL | REQUIRED（仅已激活 role） |
| `<role>_pid` | INITIAL | OPTIONAL |
| `log_archive` | INITIAL | REQUIRED（仅 Logparse Skill） |

profile 同时固定 prompt、constraints、supplement policy 和日志归档 Content-Type。Wiki
Requirement 与任何保留名称冲突时拒绝。最终 manifest 保存完整 profile 快照及 SHA-256；Catalog
只加载与当前内置 profile 完全相同的 Skill。

## 6. Wiki Requirement

GenerationSpec 的每个 Requirement 都来自 Wiki/权威澄清，exact keys：

```text
{
  name, kind, stage, fulfillment_source, prompt, constraints,
  supplement_policy, requiredness, activation_condition,
  source_reference, confirmed
}
```

基础字段：

- `name`：Name；在展开 profile 后仍全局唯一。
- `kind`：`INPUT` 或 `ATTACHMENT`。
- `stage`：`INITIAL` 或 `AFTER_LOGPARSE`。
- `fulfillment_source`：INPUT 固定 `USER_FACT`；ATTACHMENT 固定 `READY_ATTACHMENT`。
- `prompt`：canonical single line。
- `supplement_policy`：REQUIRED/CONDITIONAL 固定 `MISSING_ONLY`；OPTIONAL 固定 `NONE`。
- `requiredness`：`REQUIRED`、`OPTIONAL` 或 `CONDITIONAL`。
- `activation_condition`：只有 CONDITIONAL 非 null；其余必须 null。
- `source_reference`：作者可核对的具体来源，非空单行。
- `confirmed`：必须为 `true`。

`AFTER_LOGPARSE` 只允许 INPUT。Logparse Skill 的 archive 由 profile 注入，禁止另声明 Wiki
ATTACHMENT；非 Logparse Skill 每阶段最多一个 ATTACHMENT。

INPUT constraints exact keys：

```text
{ value_type, min_utf8_bytes, max_utf8_bytes, pattern, allowed_values }
```

`value_type` 仅 `STRING`；byte 范围满足 `1 <= min <= max <= 65536`；pattern 为 null 或可编译
Python regex；`allowed_values` 必须始终是数组，成员是唯一非空字符串。没有枚举限制时必须写
空数组 `[]`，绝不能写 JSON `null`；有枚举限制时写 1..100 个唯一非空字符串。

ATTACHMENT constraints exact keys：

```text
{ allowed_content_types, min_count, max_count }
```

count 为正整数且 min <= max；Content-Type 是唯一非空字符串数组。

## 7. Requiredness 与条件

- REQUIRED：进入对应阶段即激活；缺失时必须询问并阻塞。
- OPTIONAL：从不主动询问、不创建 OPEN requirement；创建 Case 时已提供则可以使用。
- CONDITIONAL：条件成立后等同 REQUIRED；条件未成立时不询问、不阻塞。

ActivationCondition exact shape：

```text
{
  any_of: [
    {
      all_of: [
        { source, name, operator: "EQUALS", value }
      ]
    }
  ]
}
```

`any_of` 与每个 `all_of` 都必须非空，最多 20 项，分支和 term 唯一。

- `source=USER_FACT`：name 必须指向另一个非 CONDITIONAL 的 INITIAL INPUT；允许在 INITIAL 或
  AFTER_LOGPARSE 条件中使用。事实未提供时 term 为 false。
- `source=RULE_RESULT`：value 仅 `PASS|FAIL|UNKNOWN`；只允许 AFTER_LOGPARSE。name 必须是
  verification contract 的机械 rule，不得为 SEMANTIC_CAUSALITY。
- RULE_RESULT 对应 rule 的参数、event selector 和递归 depends_on 不得引用待激活参数。
- 禁止自依赖、条件参数链和任何循环。

示例：

```json
{
  "any_of": [
    {
      "all_of": [
        {
          "source": "USER_FACT",
          "name": "transport_protocol",
          "operator": "EQUALS",
          "value": "standard"
        }
      ]
    }
  ]
}
```

## 8. LogparsePlan

GenerationSpec 的 LogparsePlan exact keys：

```text
{ anchors }
```

`anchors` 为 1..20 个 exact `{ label, module }`：

- label 必须是 Name、唯一，并与同位置 Role label 相同。
- module 必须是 exact `{source:"SKILL_FIXED", value:<canonical string>}`。

generator 在 manifest 中补全：

- `attachment_requirement = "log_archive"`；
- `problem_time_binding = USER_FACT(problem_time)`；
- 每个 anchor 的 slot/process_name/pid 分别绑定
  `<role>_slot|<role>_process_name|<role>_pid`。

`requires_logparse=true` 时 plan 非 null、至少一个 extractor，并允许 AFTER_LOGPARSE；false 时
plan 为 null、roles 为空、extractors 为空、禁止 AFTER_LOGPARSE 和 logparse_product。

## 9. Verification contract 接口

verification contract v2 校验看到的是 profile 展开后的完整 requirement 集，而不是仅 Wiki
requirements。因此 selector、FACT、时间 reference 和 remediation 可以引用 `problem_time`、角色
字段或 Wiki 参数。所有 USER_FACT 必须命名完整集合中的 INPUT；selector 不得引用 OPTIONAL
INPUT，只能引用始终可用的 REQUIRED 或由显式条件激活后补齐的 CONDITIONAL INPUT。

Role/Anchor/Event/Field/Rule 的引用闭包、机械 rule、SEMANTIC_CAUSALITY 和终态 DNF 规则见独立
v2 参考。CONDITIONAL RULE_RESULT 额外遵守第 7 节的独立性限制。

## 10. Wiki fence

`--wiki` 输入必须恰好有一个：

````text
## GenerationSpec v6

```json
{ ...完整 GenerationSpec v6 对象... }
```
````

自然语言理解和作者确认属于转换 Agent；确定性 generator 只读取显式 JSON，不做启发式 NLP。

## 11. Manifest v6

manifest 不是作者输入。generator 生成 `schema_version=6`，保存：业务身份、profile 快照/hash、
已确认 roles、profile+Wiki 展开的 requirements、完整 Logparse binding、verification contract。
每个 manifest requirement 额外包含 `origin`、nullable `role`、`requiredness`、nullable
`activation_condition` 和 nullable `source_reference`。`PendingRequirement.required` 仍为 true，
因为只有已经激活且缺失的项才允许进入 OPEN 状态。
