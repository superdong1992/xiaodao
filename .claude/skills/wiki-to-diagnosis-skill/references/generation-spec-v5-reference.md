# GenerationSpec v5 精确参考

本文是转换 Agent 构造并规范化 `GenerationSpec v5` 时使用的自包含唯一合同，版本常量为
`generator_version = "5.0.0"` 和 `schema_version = 5`。转换 Agent 不需要也不得通过读取
生成器或 Runtime 源码补充、覆盖本文；维护者必须用合同测试保证 reference、生成器与
Runtime 同步。`verification_contract` 的完整合同见
[verification-contract-v2-reference.md](verification-contract-v2-reference.md)。

本文只定义通用结构语言。具体名称、日志文本、正则、阈值、时间窗口、模块策略和
判定内容只能来自当前 Wiki，并且只能进入当前生成 Skill 或自包含业务用例。

## 1. 记号与规范化约定

- **exact keys**：对象必须恰好包含列出的 key；不得缺少，也不得增加未知 key。
- **required nullable**：key 必须出现，但值可以是 JSON `null`。
- **canonical single line**：非空字符串；不得有首尾空白、CR 或 LF；表中长度均为
  UTF-8 byte 上限。
- **normalized text**：非空文本；最多 65,536 UTF-8 bytes；CRLF/CR 规范化为 LF，
  再去除首尾空白；内部换行允许。
- **Name**：`^[a-z][a-z0-9_]{0,63}$`。
- JSON number 只有在实现明确要求 `int` 时才可用；boolean 不能代替 `0` 或 `1`。
- 所有数组保持声明顺序。除非本文件明确写明，生成器不会排序，也不会填入业务默认值。

## 2. 根对象

根对象有 20 个必填 key；`logparse_product` 是唯一可选 key。除此之外的 key 一律拒绝。

| Key | 类型 | Null | 约束 |
| --- | --- | --- | --- |
| `schema_version` | integer | 否 | 必须等于 `5` |
| `generator_version` | string | 否 | 必须精确等于 `5.0.0` |
| `id` | string | 否 | canonical single line，最多 64 bytes；匹配 `^diagnose-[a-z0-9]+(?:-[a-z0-9]+)*$` |
| `version` | string | 否 | canonical single line，最多 64 bytes；严格三段 SemVer，major 必须不小于 5 |
| `capability` | string | 否 | canonical single line，最多 64 bytes；匹配 `^[a-z][a-z0-9-]{1,63}$` |
| `deployment_scope` | enum | 否 | `PRODUCTION` 或 `TEST_ONLY` |
| `summary` | string | 否 | canonical single line，最多 4,096 bytes |
| `chinese_title` | string | 否 | canonical single line，最多 256 bytes |
| `module_name` | string | 是 | required nullable；非 null 时 canonical single line，最多 128 bytes |
| `problem_scope` | text | 否 | normalized text |
| `roles` | array | 否 | 0..20 个 [Role](#3-role) |
| `requirements` | array | 否 | 0..64 个 [Requirement](#4-requirement) |
| `logparse_plan` | object | 是 | required nullable；见 [LogparsePlan](#6-logparseplan) |
| `verification_contract` | object | 否 | `schema_version=2`；见独立 v2 参考 |
| `time_characteristics` | string array | 否 | 0..100 个 normalized text；规范化后唯一 |
| `analysis_steps` | string array | 否 | 1..100 个 normalized text；规范化后唯一 |
| `judgement_rules` | string array | 否 | 1..100 个 normalized text；规范化后唯一 |
| `output_requirements` | string array | 否 | 1..100 个 normalized text；规范化后唯一 |
| `assumptions` | string array | 否 | 0..100 个 normalized text；规范化后唯一 |
| `requires_logparse` | boolean | 否 | 严格 JSON boolean |
| `logparse_product` | string | 是 | **唯一可选 key**；省略或 null 表示使用上游默认；非 null 时 canonical single line，最多 4,096 bytes；字面值 `default` 禁止 |

补充约束：

- `id` 只校验自身格式；当前实现不自动证明它与 `capability` 的文本相等。
- `capability` 的正则要求至少两个字符。
- `version` 只接受 `major.minor.patch`，不接受预发布、build metadata 或数字前导零。

### 2.1 文本字段的 authoring 语义

以下约定只定义既有字段的 authoring 语义，不增加 key，也不改变本节表中的 schema、类型、数量或
规范化约束：

- `(# ... #)` 与 `（# ... #）` 旁注是转换元数据。应用字段映射前，先从输入剔除每个旁注的
  起止标记及其整个正文，
  只把标记外正文解释为业务事实。旁注正文只能用于排除审计；临时禁止集合仅包含旁注中未由
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

## 3. Role

Role 是 exact-key 对象：

```text
{ label, description }
```

| Key | 类型 | 约束 |
| --- | --- | --- |
| `label` | string | canonical single line，最多 64 bytes；`^[a-z][a-z0-9_-]{0,63}$` |
| `description` | string | canonical single line，最多 512 bytes |

当前验证器没有拒绝重复的 role label。Canonical authoring 必须主动保持 label 唯一；凡是
要被 verification contract 的 `ROLE_COVERAGE` 引用的 label，还必须使用 Name 语法，不能含
连字符。

当多个 Logparse anchor 表示参与诊断或因果贡献的不同参与者时，Canonical authoring 必须为
每个参与者声明一个唯一 Role，并让 Role label 与对应 anchor label 相同。只有输入完全不使用
角色、参与者或贡献者语义时，`roles` 才可为空。

## 4. Requirement

每个 Requirement 都是必需输入，不存在 optional requirement。对象 exact keys：

```text
{
  name,
  kind,
  stage,
  fulfillment_source,
  prompt,
  constraints,
  supplement_policy
}
```

| Key | 类型 | 约束 |
| --- | --- | --- |
| `name` | Name | 所有 requirement 中唯一 |
| `kind` | enum | `INPUT` 或 `ATTACHMENT` |
| `stage` | enum | `INITIAL` 或 `AFTER_LOGPARSE` |
| `fulfillment_source` | enum | `USER_FACT` 或 `READY_ATTACHMENT`；必须与 kind 配对 |
| `prompt` | string | canonical single line，最多 4,096 bytes |
| `constraints` | object | exact shape 由 kind 决定 |
| `supplement_policy` | enum | `NONE` 或 `MISSING_ONLY` |

只允许以下 kind/source 配对：

| `kind` | `fulfillment_source` |
| --- | --- |
| `INPUT` | `USER_FACT` |
| `ATTACHMENT` | `READY_ATTACHMENT` |

### 4.1 INPUT constraints

Exact keys：

```text
{ value_type, min_utf8_bytes, max_utf8_bytes, pattern, allowed_values }
```

| Key | 类型 | Null | 约束 |
| --- | --- | --- | --- |
| `value_type` | enum | 否 | 仅 `STRING` |
| `min_utf8_bytes` | integer | 否 | `1..65536` |
| `max_utf8_bytes` | integer | 否 | `min_utf8_bytes..65536` |
| `pattern` | string | 是 | required nullable；非 null 时必须能被 Python `re.compile` 编译 |
| `allowed_values` | string array | 否 | 每项必须是非空字符串并且唯一；允许空数组 |

当前实现没有为 `pattern` 单独设置长度上限，并且接受空正则字符串；也没有为
`allowed_values` 设置数组长度或单项 byte 上限。Canonical authoring 不应依赖这些宽松边角。

### 4.2 ATTACHMENT constraints

规范化后的 exact keys：

```text
{ allowed_content_types, min_count, max_count }
```

| Key | 类型 | 约束 |
| --- | --- | --- |
| `allowed_content_types` | string array | 可为空；每项非空且唯一；普通 attachment 必须显式出现 |
| `min_count` | integer | 严格正整数 |
| `max_count` | integer | 不小于 `min_count` |

当前实现没有给 count 设置数值上限，也没有给 content-type 数组设置 cardinality 上限。

若该 Requirement 正是 `logparse_plan.attachment_requirement` 指向的日志归档，
`allowed_content_types` 是平台拥有的字段：Agent-authored 输入可以省略它，生成器会在校验前
注入以下固定、有序值：

```json
[
  "application/gzip",
  "application/zip",
  "application/x-tar"
]
```

如果输入显式提供该 key，其最终数组必须与上面的值和顺序完全一致。空数组不会触发注入，
并会在后续跨字段校验中失败。

### 4.3 Requirement 跨字段约束

- `AFTER_LOGPARSE` 只允许 `INPUT`。
- 数组必须先列出全部 `INITIAL`，再列出全部 `AFTER_LOGPARSE`。
- Requirement name 全局唯一。
- 每个 stage 最多一个 `ATTACHMENT`；由于 `AFTER_LOGPARSE` 禁止 attachment，当前合同实际
  最多允许一个 `INITIAL` attachment。
- `MISSING_ONLY` 只是声明该 requirement 可以作为规则 remediation；它不会让 requirement
  变成 optional。

## 5. Binding

Binding 是以下两个 exact-key 分支之一：

```text
USER_FACT   := { source: "USER_FACT", name: Name }
SKILL_FIXED := { source: "SKILL_FIXED", value: canonical-single-line-string }
```

- `SKILL_FIXED.value` 最多 4,096 UTF-8 bytes。
- 在 `LogparsePlan` 中出现的每个 `USER_FACT.name` 必须命名一个 `INPUT` Requirement。
- Binding 不允许 null；只有包含 Binding 的上层字段明确标为 nullable 时，整个字段才能为
  null。

## 6. LogparsePlan

`logparse_plan` 非 null 时是 exact-key 对象：

```text
{ attachment_requirement, problem_time_binding, anchors }
```

| Key | 类型 | Null | 约束 |
| --- | --- | --- | --- |
| `attachment_requirement` | string | 是 | required nullable；非 null 时 canonical single line，最多 64 bytes，并且必须命名一个 ATTACHMENT Requirement |
| `problem_time_binding` | Binding | 否 | 必须出现 |
| `anchors` | array | 否 | 1..20 个 Anchor；label 唯一 |

Anchor exact keys：

```text
{ label, module, slot, process_name, pid }
```

| Key | 类型 | Null | 约束 |
| --- | --- | --- | --- |
| `label` | string | 否 | canonical single line，最多 64 bytes；plan 内唯一 |
| `module` | Binding | 否 | 必须出现 |
| `slot` | Binding | 否 | 必须出现 |
| `process_name` | Binding | 否 | 必须出现 |
| `pid` | Binding | 是 | required nullable |

被 `verification_contract.event_extractors[].anchor` 引用的 anchor label 必须是 Name。为避免
Role、Anchor 和 Event 之间出现语法分裂，Canonical authoring 对所有 anchor label 都使用
Name。

## 7. `requires_logparse` 状态矩阵

| 条件 | `requires_logparse=true` | `requires_logparse=false` |
| --- | --- | --- |
| `logparse_plan` | 必须非 null | 必须 null |
| `logparse_product` | 可省略/null/非默认字符串 | 必须省略或 null |
| `event_extractors` | 至少 1 个 | 必须为空数组 |
| `AFTER_LOGPARSE` Requirement | 允许 INPUT | 禁止 |
| `INITIAL` ATTACHMENT | 允许，若被 plan 引用则使用固定 archive MIME | 允许普通 attachment |
| LogparsePlan 的 USER_FACT bindings | 全部必须命名 INPUT | 不存在 |

当 `requires_logparse=true` 且 `attachment_requirement` 非 null 时，该名称必须找到一个
`ATTACHMENT` Requirement，其规范化 `allowed_content_types` 必须精确等于平台固定三项。

## 8. Verification contract 接口

GenerationSpec 会把以下上下文传给 verification contract v2 校验器：

- 完整、已规范化的 Requirement 列表；
- LogparsePlan 的 anchor label 集合；
- Role label 集合；
- `requires_logparse` boolean。

因此 verification contract 中的 fact、anchor、role 和 extractor 不是自由字符串；它们必须
满足独立参考中列出的跨引用约束。规范化后的 verification contract 会替换输入对象，成为
生成 manifest 的唯一机器事实。

## 9. Wiki fence 与确定性入口

使用 `--wiki` 时，工作 Wiki 必须恰好包含一个如下形式的 fenced object：

````text
## GenerationSpec v5

```json
{ ...完整 GenerationSpec v5 对象... }
```
````

标题、`json` fence 和对象都必须存在；零个或多个匹配块都会失败。自然语言 Wiki 的解释由
转换 Agent 完成，确定性脚本只解析这个显式对象，不做启发式业务推断。`--spec` 则直接读取
一个 UTF-8 JSON object。

## 10. Canonical authoring 子集

为了让 reference、生成结果和后续 validator 保持稳定，转换 Agent 必须使用下列严格子集，
即使当前实现对某些输入更宽松：

1. 所有 required-nullable key 都显式出现，空值只写 JSON `null`。
2. 空集合只写 `[]`；不使用其他 falsy 值表达空集合。
3. Requirement、Role、Anchor 等业务 ID 全部唯一；Role/Anchor/Event/Field/Rule ID 统一使用
   Name 语法。
4. 不利用空正则、无界超长 allowlist、空白 allowlist item 或无界 attachment count。
5. 日志归档的 MIME 由平台注入；若必须展示规范化结果，只展示固定三项及固定顺序。
6. `logparse_product` 使用上游默认时省略该 key；不要写字符串 `default`。
7. 所有 USER_FACT Binding 都命名已声明的 INPUT Requirement。
8. 不把某个具体业务用例的字段、日志文本、阈值或策略写入通用 reference。

## 11. 枚举总表

- `deployment_scope`: `PRODUCTION`, `TEST_ONLY`
- Requirement `kind`: `INPUT`, `ATTACHMENT`
- Requirement `stage`: `INITIAL`, `AFTER_LOGPARSE`
- `fulfillment_source`: `USER_FACT`, `READY_ATTACHMENT`
- `supplement_policy`: `NONE`, `MISSING_ONLY`
- Binding `source`: `USER_FACT`, `SKILL_FIXED`
- INPUT `value_type`: `STRING`
- 平台日志归档 Content-Type：`application/gzip`, `application/zip`, `application/x-tar`
