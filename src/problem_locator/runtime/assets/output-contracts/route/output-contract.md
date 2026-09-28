# ROUTE 输出合同

最终响应只返回 JSON 对象，固定四个字段：`skill_id`、`reason`、`confidence`、`assessments`。

```json
{"skill_id":"diagnosis-skill/example","reason":"适用条件明确，其他候选均可排除。","confidence":0.98,"assessments":[{"skill_id":"diagnosis-skill/example","applicability":[{"condition_id":"rpc-timeout","verdict":"SUPPORTED","reason":"用户明确报告 RPC 超时。","evidence":[{"pointer":"/problem_spec/statement","quote":"RPC 请求超时"}]}],"exclusions":[]}]}
```

`skill_id` 必须逐字取自目录的 `ref.id`，或为 `null`。`reason` 为非空简短说明（最多 1024 字符）。`confidence` 为有限的 0..1 JSON 数字，表示对本次选择的把握，不是统计概率；低于 0.95 不进入专用定位。

`assessments` 必须恰好覆盖每个 `routing` 非 null 的候选，不得遗漏、重复或加入旧版候选。每项固定 `skill_id`、`applicability`、`exclusions`；后两个数组逐项覆盖同名注册条件。每个条件固定 `condition_id`、`verdict`、`reason`、`evidence`。`verdict` 只能是 `SUPPORTED`（条件成立）、`REFUTED`（条件不成立）或 `UNKNOWN`（无法判断）。即使返回 null，也必须完成这些审核。

`evidence` 是 `{ "pointer": "...", "quote": "..." }` 数组。pointer 是相对 `CONTEXT_SNAPSHOT` 的 JSON Pointer，只能引用 `problem_spec` 中的字符串或字符串列表项，或 `user_facts` / `confirmed_facts` 的 `statement`。quote 必须逐字复制对应字段的非空连续原文。明确判断必须有原文依据；无法找到依据时返回 UNKNOWN，不能把未提及等同于不存在。不得引用假设、历史结论、目录或提示词作问题事实。

任一适用条件 REFUTED 或任一排除条件 SUPPORTED，表示该候选不适用；全部适用条件 SUPPORTED 且全部排除条件 REFUTED 才表示明确适用，否则为不确定。只有唯一候选明确适用、其他可审核候选均明确不适用、引用有效且 confidence >= 0.95，服务端才放行。多个匹配、其他候选仍不确定、证据不足均转通用；不得选“最接近”的一个。未声明 routing 的候选不能自动选用。

缺少后续诊断材料不等于适用范围不符。服务端固定版本、hash 和任务身份，并校验审核完整性与引用。非法 JSON、未知 ID、遗漏审核项或字段错误会报错，不会转通用或重新调用模型。

所有路由输入均已提供。不要调用文件工具，不要创建草稿，不要输出 Markdown 代码块或 JSON 之外的内容。
