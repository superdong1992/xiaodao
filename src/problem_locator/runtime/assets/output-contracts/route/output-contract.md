# ROUTE 输出合同

在最终响应中直接返回一个 JSON 对象，且只包含 `skill_id`、`reason`、`confidence`：

```json
{"skill_id":"diagnosis-skill/example","reason":"问题符合该 Skill 的定位范围。","confidence":0.95}
```

`skill_id` 必须与 `SKILL_INDEX.skills[*].ref.id` 中的一个 ID 完全一致；无匹配时返回 `null`。`reason` 是简短、非空的理由，`confidence` 是 0 到 1 之间的 JSON 数字。

根据问题和 Skill 能力选择，不要仅凭缺少用户事实排除 Skill。服务端会从启动快照补齐 Skill 版本、hash、Case、Job、revision 和 Outcome 元数据。

所有路由输入已包含在上下文中。不要调用文件工具，不要创建草稿，不要输出 Markdown 代码块或 JSON 之外的内容。
