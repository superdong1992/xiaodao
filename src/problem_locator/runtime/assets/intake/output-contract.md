# INTAKE 输出合同 1.0.0

根对象只能包含以下五个字段，全部必填：

```json
{
  "schema_version": 1,
  "action": "NEED_CLARIFICATION",
  "message": "请说明正常情况下应出现什么结果，以及问题影响哪些设备或业务。",
  "problem_fields": [],
  "user_facts": []
}
```

action 只能是 NEED_CLARIFICATION、CREATE_CASE、SUBMIT_SUPPLEMENT 或 NEW_CASE_REQUIRED。

problem_fields 和 user_facts 均为数组。每项只能包含 name、value、source_message_id、source_quote 四个非空字符串。value 必须等于 source_quote，引用只能来自 USER 消息。name 使用小写字母开头的下划线命名，最多 64 个字符。

problem_fields 的 name 只能是 statement、expected_behavior、actual_behavior、scope、goals、non_goals、constraints、completion_criteria。前四个字段各最多一项，后四个字段可以提供不同值的多项。同名字段的本轮值替换服务端草稿中的旧值，未提供的字段保留草稿。不能凭空补全字段。

CREATE_CASE 必须让合并后的草稿具备四个有用户来源的必需字段。SUBMIT_SUPPLEMENT 仅限已有任务的 OPEN INPUT requirements；其 user_facts 名称必须匹配要求，并满足字节长度、允许值和正则约束。仅提交附件时允许空 user_facts，服务端会校验并处理附件要求。

NEED_CLARIFICATION 的 message 清楚列出当前仍缺少的信息，可以保留已得到的草稿字段。NEW_CASE_REQUIRED 不携带新字段或事实，也不更改当前任务。
