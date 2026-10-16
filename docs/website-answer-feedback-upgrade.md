# 网站增量接入：回答评价与文字纠正

本次为现有 8.2.0 代码线的增量更新。部署时核对实际提交和在线 OpenAPI；不要仅凭软件包版本判断新增接口是否存在。原报告评价接口保持不变，已有客户端可继续使用。

## 哪些接入方需要适配

| 接入方 | 必须或可选的改动 |
| --- | --- |
| 只使用原报告赞踩的网站 | 无需修改请求和响应；仍调用原报告的 GET/PUT feedback |
| 接入每条追问赞踩的网站 | 新增以下 GET/PUT 路径，响应校验增加必填 followup_id；按回答分别保存状态 |
| 接入“说明哪里不对”的网站 | 在现有 POST followups 中发送 intent=CORRECTION，可选 target_followup_id；只有用户确认发送后才提交 |
| 使用示例 SDK/BFF 的网站 | 同步更新 browser-client.js、followup-contract.js、followup-bff.mjs 和 server.mjs；server.ts 仍为兼容入口 |
| 运维 | 停服后备份数据库与 DATA_ROOT，再按现有部署流程升级。本次反馈和追问私有存储的增量升级在启动时完成；既有 V11 数据版本升级仍按原升级文档处理 |
| MCP 客户端 | 七个工具的输入不变，无需适配 |

本次会更新私有存储标记，旧程序不能直接接管升级后的数据库。需要回退时，恢复与旧程序对应的数据库和 DATA_ROOT 备份。

按钮、反馈输入框和页面布局由网站实现。本次没有新增 SSE 事件、经验管理界面或提炼状态展示。原 FollowupItem、FollowupView、提交回执和追问事件的字段与 schema_version 均不变。

## 每条回答独立评价

原报告继续使用：

~~~text
GET /api/v1/agent/conversations/{conversation_id}/runs/{run_id}/feedback
PUT /api/v1/agent/conversations/{conversation_id}/runs/{run_id}/feedback
~~~

每条追问回答使用新路径：

~~~text
GET /api/v1/agent/conversations/{conversation_id}/runs/{run_id}/followups/{followup_id}/feedback
PUT /api/v1/agent/conversations/{conversation_id}/runs/{run_id}/followups/{followup_id}/feedback
~~~

网站 BFF 路径去掉 /v1。两类 PUT 均只接收 request_id 和 rating（LIKE 或 DISLIKE），不接收评论。读取不接收请求体，两个方法都不接收查询参数。请求沿用网站可信登录身份，不接受浏览器指定 owner。

新接口沿用 {ok,data,error} 包装，data 固定为以下七个字段。原报告响应仍为原六个字段，没有 followup_id。

~~~json
{
  "schema_version": 1,
  "conversation_id": "10000000-0000-0000-0000-000000000001",
  "run_id": "20000000-0000-0000-0000-000000000001",
  "followup_id": "30000000-0000-0000-0000-000000000001",
  "can_rate": true,
  "rating": "LIKE",
  "updated_at": "2026-10-16T14:00:00.000+08:00"
}
~~~

尚未评价时 rating、updated_at 均为 null。已完成且仍可访问的 Generic 或 skill-direct 回答可评价；未完成回答的 can_rate 为 false。页面以 GET 返回值为准，不根据经验库开关隐藏评价。不同用户不能读取或修改他人的反馈。

同一 request_id 重试时保持目标和 rating 不变；同票不累计，换票用新 ID，旧请求重放返回当前投票。报告和各条回答的请求 ID 分别生成，不跨目标复用。刷新后重新 GET；缓存键至少包含 conversation_id、run_id 和 followup_id。只采用与当前目标一致的响应。对同一目标按用户操作顺序发送换票，避免并发请求到达顺序不确定。

~~~javascript
const feedback = await client.conversations.getFollowupFeedback(conversationId, selectedRunId, followupId);
const saved = await client.conversations.setFollowupFeedback(conversationId, selectedRunId, followupId, {
  request_id: savedVoteRequestId,
  rating: "DISLIKE",
});
~~~

原报告点踩、某条解释点赞可以同时存在。给解释点赞不会改写报告的投票；点赞也不等于根因已经验证。反馈接口不会重新生成用户可见回答；启用通用经验库后，符合条件的点赞可能安排一次后台经验提炼。专用定位只保存投票。

## 点踩后说明问题

点踩仅保存投票。网站可以展示“说明哪里不对”，等用户主动发送文字后，再调用已有 POST followups。输入上限、忙状态、停止、幂等和 SSE 恢复机制与普通追问相同。

~~~javascript
// 纠正原报告：省略 target_followup_id。
await client.followups.send(conversationId, selectedRunId, {
  request_id: savedCorrectionRequestId,
  text: savedCorrectionText,
  intent: "CORRECTION",
});

// 纠正特定追问回答：必须使用同一轮内已完成回答的 followup_id。
await client.followups.send(conversationId, selectedRunId, {
  request_id: anotherSavedRequestId,
  text: anotherSavedText,
  intent: "CORRECTION",
  target_followup_id: followupId,
});
~~~

省略 intent 即为普通追问；普通追问不能携带非空 target_followup_id。空字段建议直接省略。要修正上一条纠正回答时，target_followup_id 指向那条回答；服务端不会自行猜测应替代哪条经验。重试须保留原 ID、原文、intent 和目标；不能把尚未确认的普通请求改成纠正请求继续复用同一 ID。

服务端把文字纠正和目标放入后续上下文，并追加新的回答，原报告与原回答保持原样。收到回执后，继续使用原追问快照和事件流展示答案。页面不应把提交成功、回答完成或用户点赞显示成“经验已采纳”。本次未提供经验采纳状态接口。

启用 GENERIC_MEMORY_ENABLED 后，通用定位的纠正结果可进入后台核对。只有原始证据支持的修正才成为共享经验；没有可读取的原日志时，文字仍用于当前对话，但不会自动发布未核实的纠正经验。采纳后的经验显式替代被纠正目标的旧经验，旧任务晚到或旧目标再次获赞不能恢复被替代内容。新回答点踩会停用它自己的经验，不改其他对象的投票。

REPORT_FOLLOWUP_ENABLED 仍控制提交追问，GENERIC_MEMORY_ENABLED 仍控制通用经验提炼与召回，默认关闭，无新增必填环境变量。每次定位最多参考一张 4 KiB 经验卡；共享的是脱敏诊断步骤和适用条件，不共享原报告与日志。

## 网站验收重点

- 原报告点踩、追问点赞互不覆盖；历史轮次和其他回答不串票。
- 刷新可恢复最新投票；同票重试和旧请求重放不产生新投票或新回答。
- 点踩不会自动提交文字、调用追问模型或替用户生成纠正内容。
- 普通追问仍发送原来的两个字段；纠正重试保留全部参数。
- 分享页不显示评价和纠正入口；权限失败不回退到其他接口。
- 模型没有核对原日志时，页面保留 REPORT_ONLY 的说明，不宣称已验证结论。

更完整的追问恢复与故障处理见 [报告追问接入](website-report-followup.md)，原报告反馈约定见 [通用经验库](generic-feedback-memory.md)。
