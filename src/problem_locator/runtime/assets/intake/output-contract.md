# INTAKE 输出合同 1.3.1

根对象只能包含以下五个字段，全部必填：

```json
{
  "schema_version": 1,
  "action": "NEED_CLARIFICATION",
  "message": "<当前 OPEN requirement 的原始 description>",
  "problem_fields": [],
  "user_facts": []
}
```

action 只能是 NEED_CLARIFICATION、SUBMIT_SUPPLEMENT 或 NEW_CASE_REQUIRED。任务已由服务端创建，不接受 CREATE_CASE。

problem_fields 和 user_facts 均为数组。每项只能包含 name、value、source_message_id、source_quote 四个非空字符串。value 必须在 source_quote 中原样出现，引用只能来自 USER 消息。name 使用小写字母开头的下划线命名，最多 64 个字符。

problem_fields 的 name 只能是 statement、expected_behavior、actual_behavior、scope、goals、non_goals、constraints、completion_criteria。通常返回空数组；不能补全或重写已冻结的问题，也不能将建案默认值当成用户事实。服务端忽略问题字段的重新拆分、未知项和重复项，保留冻结的问题原文；这类重构不会触发 NEW_CASE_REQUIRED。

SUBMIT_SUPPLEMENT 仅限已有任务的 OPEN INPUT requirements；其 user_facts 名称必须匹配要求，并满足字节长度、允许值和正则约束。NEED_CLARIFICATION 中携带的 user_facts 也必须满足同样要求。只要有部分有效新参数，就返回这些参数并选择 SUBMIT_SUPPLEMENT；其余要求可以继续等待，不得要求用户重新发送已提供的值。仅提交附件时允许空 user_facts，服务端会校验并处理附件要求。

提取范围包括第一条问题描述和全部提供的 USER 历史消息。服务端会合并经过当前要求重新校验的 draft_user_facts；本轮同名值覆盖未冻结的旧草稿，已冻结同值去重。未采用的草稿不能冒充 frozen_user_facts。无论 action 是 NEED_CLARIFICATION 还是 SUBMIT_SUPPLEMENT，存在有效新参数时服务端都会提交；只有已冻结同值重述时不会发起空补充命令。

服务端按项处理参数：合并完全相同的重复值，移除未知、无有效用户来源或不符合约束的项，保留其他可用参数。同名参数存在冲突值时整组不采用，不任选一个；未采用的参数继续等待。可用参数为空时继续追问，不把单项格式问题当成整次诊断失败。

时间 value 保留用户原文。仅当 requirement 是固定内建 problem_time 且约束一致时，服务端才把包含完整日期、时间及 Z 或明确偏移的时间转成毫秒 UTC。日期与时间之间只能是 T 或一个 ASCII 空格，例如 2026-09-16 10:00:00+08:00。不得猜时区、日期或丢失亚毫秒精度；不接受未知时区 -00:00、多个空格、制表符或其他空白字符。其他参数及重命名的时间字段按原文使用，不做这类转换。

NEED_CLARIFICATION 的 message 只能引用当前 OPEN requirements 的原始 description；服务端会用权威 prompt 展示追问。没有 OPEN requirements 时不得自行追问。用户明确换题或修改已冻结命名事实时返回 NEW_CASE_REQUIRED，不携带新字段或事实，也不更改当前任务。
