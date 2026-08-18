# Verification contract v2 精确参考

本文是转换 Agent 构造 Diagnosis Skill `verification_contract` 时使用的自包含唯一合同，
合同版本为 `schema_version = 2`。转换 Agent 不需要也不得通过读取生成器、validator 或
Runtime 源码补充、覆盖本文；维护者必须用合同测试保证 reference、生成器与 Runtime
同步。包含本对象的 GenerationSpec 结构见
[generation-spec-v6-reference.md](generation-spec-v6-reference.md)。

业务事件名、日志消息、正则、阈值、clock domain、观测窗口和因果断言不是平台默认值，
只能来自当前 Wiki，并且只能保存在当前生成 Skill 或自包含业务用例中。

## 1. 通用约定

- **exact keys**：对象必须恰好包含列出的 key。
- **required nullable**：key 必须出现，但值可为 JSON `null`。
- **Name**：`^[a-z][a-z0-9_]{0,63}$`。
- **canonical text**：非空单行 UTF-8 字符串；无首尾空白、CR 或 LF；默认最多
  4,096 bytes。
- 所有标为 integer 的值都要求严格 integer；boolean 不能替代 integer。
- 数组保持声明顺序。ID/name 数组除非另有说明都必须唯一。
- 所有时间数值边界按字段名所示以毫秒计；合同不提供隐藏容差。

## 2. 根对象

根对象 exact keys：

```text
{
  schema_version,
  observation_policies,
  event_extractors,
  rules,
  terminal_paths
}
```

| Key | 类型 | Cardinality / 约束 |
| --- | --- | --- |
| `schema_version` | integer | 必须等于 `2` |
| `observation_policies` | array | 0..100 个 ObservationPolicy |
| `event_extractors` | array | 0..100 个 EventExtractor |
| `rules` | array | 1..300 个 Rule |
| `terminal_paths` | array | 1..50 个 TerminalPath |

`requires_logparse` 必须严格等于 `bool(event_extractors)`：Logparse Skill 至少有一个
extractor；非 Logparse Skill 的 extractor 数组必须为空。

## 3. Reusable value shapes

### 3.1 Binding

Binding 是两个 exact-key 分支之一：

```text
USER_FACT   := { source: "USER_FACT", name: Name }
SKILL_FIXED := { source: "SKILL_FIXED", value: canonical-text }
```

`SKILL_FIXED.value` 最多 4,096 bytes。被规则作为 fact 引用的 `USER_FACT.name` 必须命名
GenerationSpec 中的 `INPUT` Requirement。

### 3.2 EventField

EventField exact keys：

```text
{ event, field }
```

两个值都是 Name，并且必须命名已声明的 EventExtractor 及其 Field。

### 3.3 Equality

Equality exact keys：

```text
{ members }
```

- `members` 是 2..20 个 EventField。
- 同一个 Equality 内的 `(event, field)` pair 必须唯一。
- Equality list 在使用处是 1..20 个 Equality。

## 4. ObservationPolicy

每个 policy exact keys：

```text
{ id, kind, scope, key_fields, window_ms, max_observed, boundary }
```

| Key | 类型 | Null | 约束 |
| --- | --- | --- | --- |
| `id` | Name | 否 | policy 数组内唯一 |
| `kind` | enum | 否 | `SUPPRESSION` 或 `RATE_LIMIT` |
| `scope` | Name | 否 | 通用作用域 ID；当前无其他 cross-check |
| `key_fields` | Name array | 否 | 0..100，唯一；被 event 引用时必须是该 event fields 的子集 |
| `window_ms` | integer | 否 | `1..604800000` |
| `max_observed` | integer | 是 | kind-dependent，见下表 |
| `boundary` | enum | 否 | `CLOSED_OPEN` 或 `CLOSED_CLOSED` |

Kind 条件：

| `kind` | `max_observed` |
| --- | --- |
| `SUPPRESSION` | 必须为 `null` |
| `RATE_LIMIT` | 必须为 `1..1000000` 的 integer |

Policy 只声明观测损失，不会抹除已观测到的正向事件；不存在未声明的默认 suppression、
rate limit 或窗口边界。

## 5. EventExtractor

每个 extractor exact keys：

```text
{
  id,
  anchor,
  members,
  fields,
  timestamp_field,
  group_by,
  selectors,
  max_gap_lines,
  min_matches,
  max_matches,
  observation_policy_ids
}
```

| Key | 类型 | Null | 约束 |
| --- | --- | --- | --- |
| `id` | Name | 否 | extractor 数组内唯一 |
| `anchor` | Name | 否 | 必须命名 GenerationSpec LogparsePlan 中的 anchor label |
| `members` | array | 否 | 1..16 个 Member，保持多行组装顺序 |
| `fields` | array | 否 | 1..100 个 Field；name 唯一 |
| `timestamp_field` | Name | 是 | required nullable；非 null 时必须是有 clock 的 INTEGER/TIMESTAMP Field |
| `group_by` | Name array | 否 | 0..100，唯一；必须是 fields 子集 |
| `selectors` | array | 否 | 0..20 个 Selector |
| `max_gap_lines` | integer | 否 | `0..10000` |
| `min_matches` | integer | 否 | `0..1000000` |
| `max_matches` | integer | 是 | required nullable；非 null 时 `0..1000000` 且不小于 min |
| `observation_policy_ids` | Name array | 否 | 0..100，唯一；必须命名已声明 policy |

### 5.1 Member

Member exact keys：

```text
{ line_pattern, match_mode }
```

- `line_pattern` 是 canonical text，最多 8,192 bytes，并且必须能被 Python `re.compile`
  编译。
- `match_mode` 仅 `FULL_LINE` 或 `SEARCH`。
- `FULL_LINE` pattern 的字符串必须以 `^` 开头并以 `$` 结尾。
- 每个 named capture group 都必须使用 Name。
- 全部 member 的 named capture name **并集**必须恰好等于 `fields[].name` 集合；不能少，也
  不能多。单个 member 可以只捕获字段子集。
- 未命名 capture group 不进入字段集合，当前实现也不禁止它。

### 5.2 Field

Field exact keys：

```text
{ name, type, unit, clock_domain }
```

四个 key 都必须出现；`unit` 和 `clock_domain` 是 conditionally nullable：

| `type` | `unit` | `clock_domain` |
| --- | --- | --- |
| `STRING` | 必须 `null` | 必须 `null` |
| `INTEGER` | 必须是 IntegerUnit | `null` 或非空 string |
| `TIMESTAMP` | 必须 `null` | 必须是非空 string |

IntegerUnit：

```text
NANOSECOND | MICROSECOND | MILLISECOND | SECOND | MINUTE | COUNT | BYTE
```

当前实现没有给 `clock_domain` string 施加 Name、单行或 byte 上限；Canonical authoring
仍应使用稳定、非空、单行标识。

### 5.3 Selector

Selector exact keys：

```text
{ field, operator, value }
```

- `field` 必须命名当前 extractor 的 Field。
- `operator` 仅 `EQUALS`。
- `value` 是 Binding。

当前验证器没有把 Selector 中的 `USER_FACT.name` 与 INPUT Requirement 做 cross-check。
Canonical authoring 必须主动保证它命名已声明 INPUT；不得依赖这个实现缺口。

### 5.4 Extractor 跨引用

- `observation_policy_ids` 只能引用已声明 policy。
- 对每个引用的 policy，policy `key_fields` 必须是当前 extractor fields 的子集。
- `timestamp_field` 非 null 时，目标 Field 必须为 `INTEGER` 或 `TIMESTAMP`，并且其
  `clock_domain` 非 null。
- `group_by` 只能引用当前 extractor fields。

## 6. Rule common envelope

每个 Rule exact keys：

```text
{ id, kind, description, depends_on, remediation_requirements, parameters }
```

| Key | 类型 | 约束 |
| --- | --- | --- |
| `id` | Name | rules 内唯一 |
| `kind` | enum | 11 个 kind 之一 |
| `description` | string | canonical text，最多 4,096 bytes |
| `depends_on` | Name array | 0..100，唯一；只能命名数组中已经出现的前序 Rule |
| `remediation_requirements` | Name array | 0..100，唯一；每项必须命名 `supplement_policy=MISSING_ONLY` 的 Requirement |
| `parameters` | object | exact shape 由 kind 决定 |

Rule 数组本身就是 DAG 的拓扑顺序。Remediation 当前不限制 Requirement kind；INPUT 和
ATTACHMENT 只要声明 `MISSING_ONLY` 都能被引用。

Canonical authoring 把 `depends_on` 当作合取式就绪门槛：一条 Rule 只有在全部依赖均能以该
Rule 所需的正向前提成立时，才可以支持后续结论。尤其是 `SEMANTIC_CAUSALITY`，应只列该因果
结论的最小充分机械前提，不得混入其他原因路径的证据或在本路径中预期为 FAIL/UNKNOWN 的规则。

Rule kind 枚举：

```text
EVENT_COUNT
EVENT_PRESENT
EVENT_TIME_WINDOW
FACT_FIELD_EQUALS
FACT_IN
FIELDS_EQUAL
ROLE_COVERAGE
CROSS_ROLE_CORRELATION
EVENT_ORDER
NUMERIC_COMPARE
SEMANTIC_CAUSALITY
```

合同中至少必须有一条 `SEMANTIC_CAUSALITY`。

## 7. Rule parameter catalog

### 7.1 EVENT_PRESENT

Exact keys：

```text
{ event }
```

`event` 必须命名已声明 EventExtractor。

### 7.2 EVENT_COUNT

Exact keys：

```text
{ event, min_count, max_count }
```

- `event`：known EventExtractor。
- `min_count`：integer `0..1000000`。
- `max_count`：required nullable；非 null 时 integer `0..1000000` 且不小于 min。

### 7.3 EVENT_TIME_WINDOW

Exact keys：

```text
{
  event,
  reference,
  before_ms,
  after_ms,
  lower_bound,
  upper_bound,
  quantifier,
  clock_tolerance_ms
}
```

- `event`：known EventExtractor。
- `reference`：Binding；USER_FACT 时必须命名 INPUT Requirement。
- `before_ms`, `after_ms`：integer `0..604800000`。
- `lower_bound`, `upper_bound`：各自为 `INCLUSIVE` 或 `EXCLUSIVE`。
- `quantifier`：`ANY` 或 `ALL`。
- `clock_tolerance_ms`：integer `0..86400000`。

当前 validator 不静态要求 event 声明 `timestamp_field`；Canonical authoring 必须只对具有
可解释时间字段的 event 使用时间窗口。

### 7.4 FACT_FIELD_EQUALS

Exact keys：

```text
{ event, field, fact_name, quantifier }
```

- event/field 必须存在。
- `fact_name` 必须命名 INPUT Requirement。
- `quantifier`：`ANY` 或 `ALL`。

### 7.5 FACT_IN

Exact keys：

```text
{ fact_name, allowed_values }
```

- `fact_name` 必须命名 INPUT Requirement。
- `allowed_values`：1..100 个 canonical text，必须唯一。

### 7.6 FIELDS_EQUAL

Exact keys：

```text
{ equalities, quantifier }
```

- `equalities`：1..20 个 [Equality](#33-equality)。
- `quantifier` 必须精确等于 `EXISTS`。

### 7.7 CROSS_ROLE_CORRELATION

Exact keys：

```text
{ members }
```

`members` 是 2..20 个 EventField。当前 validator 只校验 event/field 存在，不校验 member
唯一，也不校验 role 或 anchor；Canonical authoring 必须保持 member pair 唯一，并通过
EventExtractor anchor 明确角色来源。

### 7.8 ROLE_COVERAGE

Exact keys：

```text
{ coverage }
```

`coverage` 是 1..20 个 exact-key item：

```text
{ role, event }
```

- role 和 event 都是 Name。
- coverage 内 role 必须唯一。
- role 必须命名 GenerationSpec Role。
- event 必须存在，并且该 EventExtractor 的 `anchor` 必须精确等于 role。

### 7.9 EVENT_ORDER

Exact keys：

```text
{
  before_event,
  after_event,
  allow_equal,
  quantifier,
  clock_tolerance_ms,
  joins
}
```

- `before_event`, `after_event`：known EventExtractor。
- `allow_equal`：严格 boolean。
- `quantifier`：必须精确等于 `EXISTS`。
- `clock_tolerance_ms`：integer `0..86400000`。
- `joins`：canonical form 是 `[]` 或 1..20 个 Equality。

当前 validator 不静态要求两个 event 有 timestamp field，也不证明 clock domain 兼容；
Canonical authoring 必须显式满足这些条件。

### 7.10 NUMERIC_COMPARE

Exact keys：

```text
{
  left,
  operator,
  right,
  quantifier,
  joins,
  clock_tolerance_ms
}
```

- `left`, `right`：NumericExpression。
- `operator`：`LT`, `LTE`, `EQ`, `GTE`, `GT` 之一。
- `quantifier`：`EXISTS` 或 `ALL`。
- `joins`：canonical form 是 `[]` 或 1..20 个 Equality。
- `clock_tolerance_ms`：integer `0..86400000`。

### 7.11 SEMANTIC_CAUSALITY

Exact keys：

```text
{ assertion, evidence_events }
```

- `assertion`：canonical text，最多 4,096 bytes。
- `evidence_events`：0..100 个 unique known EventExtractor ID；允许空数组。

当语义判断实际依赖已抽取事件时，Canonical authoring 必须列出相应 event；只有刻意的、
不依赖 extractor 的人工语义判断才使用空数组。

## 8. NumericExpression AST

表达式对象必须精确匹配以下白名单分支之一。递归从 depth 0 开始；进入 depth 大于 8 的
节点时拒绝。

### 8.1 FIELD

```text
{ kind: "FIELD", event, field }
```

event/field 必须存在。Rule 级交叉校验会拒绝 `type=STRING` 的 Field；INTEGER/TIMESTAMP
可进入 numeric AST。

### 8.2 FACT

```text
{ kind: "FACT", name, value_type, unit, clock_domain }
```

`name` 必须命名 INPUT Requirement。类型条件：

| `value_type` | `unit` | `clock_domain` |
| --- | --- | --- |
| `INTEGER` | 必须是 IntegerUnit | `null` 或 string |
| `TIMESTAMP` | 必须 `null` | 非空 string |

注意：当前实现对 INTEGER FACT 的非 null `clock_domain` 只检查它是 string，因此空字符串也
会被接受；FACT clock-domain string 当前也没有格式或长度上限。Canonical authoring 禁止空
clock domain，并使用稳定单行标识。

### 8.3 CONST

```text
{ kind: "CONST", value, unit }
```

- `value` 是严格 integer；当前实现没有数值范围限制。
- `unit` 是 IntegerUnit。

### 8.4 ADD / SUBTRACT

```text
{ kind: "ADD", left, right }
{ kind: "SUBTRACT", left, right }
```

`left` 和 `right` 都是 NumericExpression。

### 8.5 MULTIPLY_CONST

```text
{ kind: "MULTIPLY_CONST", operand, multiplier }
```

- `operand` 是 NumericExpression。
- `multiplier` 是 integer `-1000000..1000000`。

### 8.6 CONVERT

```text
{ kind: "CONVERT", operand, unit }
```

- `operand` 是 NumericExpression。
- `unit` 是 IntegerUnit。

### 8.7 Numeric AST 当前校验边界

验证器会校验：AST kind/keys、递归深度、引用存在、FACT 命名 INPUT、FIELD 非 STRING、
unit 枚举和局部 type metadata。

验证器当前**不会**静态证明：

- ADD/SUBTRACT 两侧 unit 相容；
- 比较左右 unit 或 value type 相容；
- clock domain 相容；
- CONVERT 的源/目标转换有意义；
- FACT metadata 与 Requirement 字符串约束之间存在某种自动转换。

Canonical authoring 必须自行保持类型、unit 和 clock domain 一致，并在需要时显式使用
`CONVERT` 和 `clock_tolerance_ms`，不得把未被 validator 拒绝误认为语义有效。

## 9. Rule 交叉引用约束

规范化每条 Rule 后，验证器执行以下检查：

1. 所有 event ID 必须命名 EventExtractor。
2. 所有 field ID 必须命名相应 event 的 Field。
3. `FACT_FIELD_EQUALS`、`FACT_IN`、`EVENT_TIME_WINDOW.reference` 中的 USER_FACT，以及
   NumericExpression FACT 都必须命名 INPUT Requirement。
4. `ROLE_COVERAGE` 的 role 必须声明，且 event anchor 必须等于 role。
5. NumericExpression FIELD 不能为 STRING。
6. `depends_on` 只能引用前序 Rule。
7. `remediation_requirements` 只能引用 MISSING_ONLY Requirement。
8. 整个合同至少一条 SEMANTIC_CAUSALITY。

### 9.1 最终 Write 前的机械闭包算法

不要只目视检查名称。在最终 `Write` 前先构造 INPUT Requirement 名称集、Role label 集、Anchor
label 集、policy ID 集、`event_id -> field 名称集` 与已见 rule ID 集，然后按声明顺序执行：

1. 核对每个 extractor 的 anchor、policy、policy key、selector field、`timestamp_field`、`group_by`
   及 selector `USER_FACT`；所有 field 引用必须属于当前 extractor。
2. 递归遍历每条 rule 的 `parameters` 和 NumericExpression；每个 event 必须存在，每个
   `(event, field)` 必须满足 `field ∈ event_id -> field 名称集`，每个 `FACT`/`USER_FACT` 必须命名
   INPUT Requirement，role 必须存在且与 event anchor 一致。
3. 核对 `depends_on` 只引用已见 rule，`remediation_requirements` 只引用 `MISSING_ONLY`
   Requirement；通过后才把当前 rule ID 加入已见集合。
4. 核对每个 TerminalPath term 引用已声明 rule。

发现任一缺失、拼写漂移或跨 event 借用 field 时不得 `Write`；修正后必须从第 1 步重新完整核对。
不得依赖 validator 的失败消息来补做这一步。

把 Rule 字段引用展开成内部清单，清单只用于 Write 前核对，不写入 GenerationSpec。每一行至少包含
`rule index`、`parameter location`、`event`、`field` 和该 event 的 declared field set。必须展开：

- `FACT_FIELD_EQUALS.parameters.event/field`；
- `FIELDS_EQUAL.parameters.equalities[*].members[*]`；
- `CROSS_ROLE_CORRELATION.parameters.members[*]`；
- `EVENT_ORDER.parameters.joins[*].members[*]`；
- `NUMERIC_COMPARE.parameters.left/right` 内递归出现的每个 `FIELD`，以及
  `parameters.joins[*].members[*]`。

例如符号表是 `request_event -> {trace_id, start_ms}`、
`response_event -> {trace_id, end_ms}`：

- `request_event.trace_id`、`request_event.start_ms`、`response_event.trace_id` 和
  `response_event.end_ms` 是闭合引用；
- `request_event.end_ms` **不是**闭合引用。`end_ms` 即使存在于 `response_event`，也绝不能被
  `request_event` 借用；应把 event 改为 `response_event`，或把 Rule 改为引用
  `request_event` 实际声明的字段；
- Equality/join 两侧分别按各自 member 的 event 核对，不能因为另一侧 event 声明了同名字段就
  视为通过；NumericExpression 的嵌套 `FIELD` 也必须逐个核对，不能只检查顶层 Rule。

FIELDS_EQUAL 的两侧字段名不要求相同。若符号表是
`client_event -> {client_request_id}`、`server_event -> {server_request_id}`，正确写法是：

```json
{
  "equalities": [
    {
      "members": [
        {"event": "client_event", "field": "client_request_id"},
        {"event": "server_event", "field": "server_request_id"}
      ]
    }
  ],
  "quantifier": "EXISTS"
}
```

下面是禁止写法，因为第二个 member 的 `server_event` 没有声明 `client_request_id`：

```json
{
  "equalities": [
    {
      "members": [
        {"event": "client_event", "field": "client_request_id"},
        {"event": "server_event", "field": "client_request_id"}
      ]
    }
  ],
  "quantifier": "EXISTS"
}
```

不要为了表达两个业务标识相等而强制复用同一字段名；FIELDS_EQUAL 比较的是两个已闭合的
EventField 值，不是字段名文本本身。对每个 Equality，`reference[0]`、`reference[1]` 及后续
member 都必须分别通过各自 event 的 field set 检查。

只有内部清单的每一行都满足 `field ∈ fields(event)`，才允许执行唯一 `Write`。任何一行无法闭合时
停止，不得写出近似字段名、跨 event 借字段或等待 validator 纠错。

### 9.2 最终 Write 前的正向 witness 演算

引用闭包只证明名称合法，不证明 extractor 能命中源材料，也不证明 Rule 在预期路径中可执行。
最终 `Write` 前，必须只用标记外 Wiki 正文与权威澄清中的稳定日志消息体和已确认事实，执行下面的
内部演算；不得读取测试、scenario、oracle、Runtime 或 validator，也不得把 witness 写进
GenerationSpec：

1. 对每个非 fallback `COMPLETE|PARTIAL` TerminalPath 构造至少一个正向 witness。witness 中每条
   event 必须来自源材料里的实际稳定消息体，禁止为了匹配正则而改写、补全或伪造日志。
2. 对 path 递归依赖闭包中的每个 EventExtractor，把实际消息体逐条代入实际 `line_pattern` 与
   `match_mode`。`SEARCH` 必须在原消息体中真实命中，`FULL_LINE` 必须整行命中；
   多行 extractor 还必须核对 member 顺序、`max_gap_lines`、group capture 和 `group_by`。每个 Field
   的命名 capture 必须真实产生值。
3. 用已确认事实执行每个 selector；selector 过滤后，path 正向依赖的 event count 必须大于零。
   observation policy 不会把已经命中的正向 event 抹掉，但也不能把零次命中伪装成 presence。
4. 按 Rule 声明顺序演算 path 所需 semantic rule 的递归依赖闭包。每个机械依赖必须得到正向结果，
   不得为 `FAIL`、`UNKNOWN` 或 `NOT_APPLICABLE`；semantic rule 的 `evidence_events` 必须非零且所有
   机械前提 ready，否则该 path 没有正向 witness。
5. 对 `FIELDS_EQUAL`、`CROSS_ROLE_CORRELATION` 以及 join，必须在 witness 中找到同一个 occurrence
   tuple，使每个 Equality 的所有 member **实际值**相等。字段名可不同，字段引用闭合也仍不足以
   证明值相等；不得因为类型、单位或字段名相似就声明 Equality。

例如源材料的实际消息体是 `edge value:3000` 与 `core budget:3000`，对应 extractor 分别捕获
`edge_value="3000"` 与 `core_budget="3000"`，则两 event 的 presence 及
`edge_event.edge_value == core_event.core_budget` 可以形成正向 witness。以下任一情况都失败：

- `line_pattern` 写成 `edge value=(?P<edge_value>\\d+)`，因为实际消息体使用冒号，event count 为零；
- Equality 比较 `edge_value` 与另一个实际为不同值的字段，即使两侧 `(event, field)` 都合法；
- FIELDS_EQUAL 依赖一个会得到 `UNKNOWN` 的 presence rule，导致自身为 `NOT_APPLICABLE`。

不能只证明 JSON 可加载、extractor 名称存在或 DAG 静态可达。任一非 fallback path 无法从源材料
构造上述正向 witness 时不得 `Write`；应请求澄清，而不是发明日志、字段值或隐含场景。

## 10. TerminalPath 与 DNF

TerminalPath 按数组顺序声明。每个 path exact keys：

```text
{ id, resolution_status, condition }
```

| Key | 类型 | 约束 |
| --- | --- | --- |
| `id` | Name | path 数组内唯一 |
| `resolution_status` | enum | `COMPLETE`, `PARTIAL`, `NONE` |
| `condition` | object | exact `{ any_of }` |

`condition.any_of` 是 1..50 个 Branch。Branch exact keys：

```text
{ all_of }
```

`all_of` 是 0..100 个 Term。Term exact keys：

```text
{ rule_id, result }
```

- `rule_id` 必须命名已声明 Rule，并在同一 Branch 内唯一。
- `result`：`PASS`, `FAIL`, `UNKNOWN`。
- Branch 内 terms 是 AND；`any_of` 中 branches 是 OR，因此 condition 是 DNF。
- 每个 `COMPLETE` 或 `PARTIAL` Branch 都必须至少包含一个
  `SEMANTIC_CAUSALITY=PASS` term。
- 空 `all_of` 表示无条件匹配；它只允许出现在最后一个 path，并且该 path 必须是 `NONE`。
- 最后一个 path 必须为 `NONE`，并且至少有一个空 `all_of` Branch。
- 条件式 `NONE` path 可以出现在 fallback 之前。

生成 Skill 的执行语义是：先重算全部 Rule，再按声明顺序选择第一条匹配的 TerminalPath。
`terminal_path_matches()` 对单个 path 的判断是任一 Branch 的全部 Term 都与 Rule 结果相等。

提交规范前，对每个非 fallback path 写出并检查一份可达性 witness：目标 semantic rule、其递归
依赖闭包、branch 中允许或要求的 PASS/FAIL/UNKNOWN，以及会优先匹配的前序 path。要求 semantic
PASS 时，其依赖闭包必须能够同时正向成立；branch 不得同时要求依赖闭包中的规则为 FAIL/UNKNOWN，
也不得借用另一条互斥原因路径的 semantic PASS。witness 只用于构造时自检，不是 GenerationSpec
的新字段。

## 11. 当前实现的宽松边角

这些是当前代码的实际行为，不是推荐输入语法：

1. `EVENT_ORDER.joins` 和 `NUMERIC_COMPARE.joins` 使用
   `[] if not value["joins"]` 规范化，所以 JSON `null`、`false`、`0`、空字符串、空对象和
   空数组都会意外归一为 `[]`。Canonical authoring 只允许 `[]` 表示无 join。
2. Selector 的 USER_FACT Binding 没有校验为已声明 INPUT；Rule fact 有该校验。
3. GenerationSpec Role label 当前不检查唯一；`CROSS_ROLE_CORRELATION.members` 当前也不
   检查唯一。
4. 时间规则不静态要求 event 提供 timestamp；numeric AST 不完整证明 unit/type/clock
   compatibility。
5. INTEGER FACT 的空字符串 clock domain 会被接受，而 INTEGER event Field 的非 null clock
   domain 必须非空。
6. `SEMANTIC_CAUSALITY.evidence_events` 允许空数组。

这些边角若要收紧，需要作为通用合同变更单独修改实现和测试；不得在某个业务用例中用
特殊字段或隐式约定掩盖。

## 12. Canonical authoring 子集

转换 Agent 和 reference 示例必须遵循：

1. required-nullable key 总是出现；只使用 JSON `null` 表示 nullable 空值。
2. 空数组只写 `[]`，尤其是 `joins`。
3. Role、Anchor、Event、Field、Policy、Rule、Path ID 全部使用 Name，且在各自作用域唯一。
4. `CROSS_ROLE_CORRELATION.members` 和其他 member pair 主动去重。
5. 每个 Selector USER_FACT 都命名已声明 INPUT Requirement。
6. 只有具有可解释 timestamp/clock 的 event 才用于时间规则；clock domain 和容差显式声明。
7. Numeric AST 两侧保持类型、unit、clock domain 相容；转换显式写 `CONVERT`。
8. Semantic rule 实际依赖事件时显式列出 `evidence_events`。
9. 每条 Candidate path 都显式包含语义 PASS；最后保留无条件 NONE fallback。
10. Reference 只使用中性结构名，不包含任何具体业务字段、日志文本、阈值或因果内容。

## 13. 枚举总表

- Binding source：`USER_FACT`, `SKILL_FIXED`
- Observation kind：`SUPPRESSION`, `RATE_LIMIT`
- Window boundary：`CLOSED_OPEN`, `CLOSED_CLOSED`
- Member match mode：`FULL_LINE`, `SEARCH`
- Event Field type：`STRING`, `INTEGER`, `TIMESTAMP`
- IntegerUnit：`NANOSECOND`, `MICROSECOND`, `MILLISECOND`, `SECOND`, `MINUTE`, `COUNT`, `BYTE`
- Selector operator：`EQUALS`
- Rule kind：`EVENT_COUNT`, `EVENT_PRESENT`, `EVENT_TIME_WINDOW`, `FACT_FIELD_EQUALS`,
  `FACT_IN`, `FIELDS_EQUAL`, `ROLE_COVERAGE`, `CROSS_ROLE_CORRELATION`, `EVENT_ORDER`,
  `NUMERIC_COMPARE`, `SEMANTIC_CAUSALITY`
- Time-window bound：`INCLUSIVE`, `EXCLUSIVE`
- Presence/window quantifier：`ANY`, `ALL`
- Equality/order quantifier：`EXISTS`
- Numeric quantifier：`EXISTS`, `ALL`
- Numeric operator：`LT`, `LTE`, `EQ`, `GTE`, `GT`
- NumericExpression kind：`FIELD`, `FACT`, `CONST`, `ADD`, `SUBTRACT`, `MULTIPLY_CONST`,
  `CONVERT`
- Numeric FACT value type：`INTEGER`, `TIMESTAMP`
- Resolution：`COMPLETE`, `PARTIAL`, `NONE`
- Rule result：`PASS`, `FAIL`, `UNKNOWN`
