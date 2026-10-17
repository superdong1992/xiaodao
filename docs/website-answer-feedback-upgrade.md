# 网站增量接入：回答评价与独立文字反馈

本次为现有 8.2.0 代码线的增量更新。部署时核对实际提交和在线 OpenAPI；不要仅凭软件包版本判断新增接口是否存在。原报告评价接口保持不变，已有客户端可继续使用。

## 哪些接入方需要适配

| 接入方 | 必须或可选的改动 |
| --- | --- |
| 只使用原报告赞踩的网站 | 无需修改请求和响应；仍调用原报告的 GET/PUT feedback |
| 接入每条追问赞踩的网站 | 新增以下 GET/PUT 路径，响应校验增加必填 followup_id；按回答分别保存状态 |
| 接入“说明哪里不对”的网站 | 点踩成功后调用新增 POST feedback/comments；不调用追问接口，不产生聊天消息 |
| 附带管理员分析资料的网站 | 网站后端按目标读取资料，填入 analysis_text 和可选 analysis_reference；浏览器不得指定这些字段 |
| 使用示例 SDK/BFF 的网站 | 同步更新 `browser-client.js`、`feedback-comment-contract.js`、`feedback-comments-bff.mjs` 和 `server.mjs`；保留既有 `followup-contract.js`、`followup-bff.mjs`、`continuation-contract.js`、`continuation-bff.mjs`；`server.ts` 仍为兼容入口 |
| 运维 | 停服后备份数据库与 DATA_ROOT，再按现有部署流程升级。本次新增文字反馈表，启动时完成增量初始化；既有 V11 数据版本升级仍按原升级文档处理 |
| MCP 客户端 | 七个工具的输入不变，无需适配 |

本次沿用当前版本演进，不另起 8.3 版本。新增接口不改变七个 MCP 工具，也不要求旧网站立刻上线文字反馈。部署后以在线 OpenAPI 中的两个 POST 路径为准。

按钮、反馈输入框和页面布局由网站实现。文字反馈本身不新增 SSE 事件、经验管理界面或提炼状态展示。统一追问另在 `FollowupItem` 增加 `attachment_ids` 和可空 `diagnosis_run_id`，旧记录缺省时分别按空数组和 null 处理；`FollowupView`、提交回执和追问事件沿用原 schema_version。带 `diagnosis_run_id` 的交接说明不是回答结论，`can_rate=false`，不接受赞踩、文字意见或经验提炼。新版正式报告发布后使用其自己的 run_id 评价，不能复用旧报告或交接项的投票目标。

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

## 点踩后提交独立文字反馈

前端已有点踩按钮和可选输入框即可。先等待原 PUT feedback 返回 DISLIKE；有用户意见或网站后端关联的管理员资料时，再调用对应的新接口。没有任何文字材料时，只保存点踩，不提交空反馈。两个请求分别使用稳定 request_id；文字提交失败不撤销已保存的点踩。

~~~text
POST /api/v1/agent/conversations/{conversation_id}/runs/{run_id}/feedback/comments
POST /api/v1/agent/conversations/{conversation_id}/runs/{run_id}/followups/{followup_id}/feedback/comments
~~~

第二条路径中的 followup_id 只标识被评价回答，不会调用 POST followups。接口只保存意见和返回回执，不新增对话消息、回答或 SSE 事件，也不在 HTTP 请求中调用模型。网站 BFF 路径去掉 /v1。

网站后端发送：

~~~json
{
  "request_id": "feedback-comment-123",
  "text": "报告没有找到实际原因。",
  "analysis_text": "管理员排查发现连接池持续耗尽，修复连接释放逻辑后恢复。",
  "analysis_reference": "web-analysis-123"
}
~~~

| 字段 | 约定 |
| --- | --- |
| request_id | 必填，1～128 个 Unicode 字符，不能全为空白；新提交使用新 ID，重试保留全部参数 |
| text | 用户填写的意见，可省略或为 null，UTF-8 最多 16 KiB |
| analysis_text | 网站后端附带的管理员分析资料，可省略或为 null，UTF-8 最多 64 KiB |
| analysis_reference | 资料来源标识，可省略或为 null，最多 256 个 Unicode 字符；必须与 analysis_text 一起提交 |

纯空白文字归一为 null；text 和 analysis_text 至少有一项非空。来源标识只用于追溯，小刀不会自动访问该标识对应的 URL 或 Web 内部系统。管理员资料保留来源属性，不会伪装成用户原话。

首次提交时，该用户对目标的当前投票必须为 DISLIKE；否则返回 409 / AGENT_FEEDBACK_REQUIRES_DISLIKE。相同 request_id、目标和材料重放返回原回执，即使之后已换成 LIKE，也不会重复保存或提炼。相同 ID 改目标、意见或管理员资料返回 409 / AGENT_IDEMPOTENCY_CONFLICT。资料后来有修改时，确认要再次提交后使用新 ID。

回执沿用 {ok,data,error}，HTTP 状态为 200。data 固定为：

~~~json
{
  "schema_version": 1,
  "conversation_id": "10000000-0000-0000-0000-000000000001",
  "run_id": "20000000-0000-0000-0000-000000000001",
  "followup_id": null,
  "comment_id": "40000000-0000-0000-0000-000000000001",
  "request_id": "feedback-comment-123",
  "created_at": "2026-10-17T12:00:00.000+08:00"
}
~~~

原报告的 followup_id 固定为 null；追问回答返回路径中的 ID。页面核对目标和 request_id 后显示“反馈已收到”，不要显示“经验已采纳”。本次不提供采纳状态或反馈管理接口。

浏览器示例：

~~~javascript
await client.conversations.setFeedback(conversationId, selectedRunId, {
  request_id: savedVoteRequestId,
  rating: "DISLIKE",
});
await client.conversations.submitFeedbackComment(conversationId, selectedRunId, {
  request_id: savedCommentRequestId,
  text: savedCommentText, // 可省略；只有网站后端附带资料时才能提交纯资料反馈。
});

// 对已完成的某条回答反馈，不改变报告投票。
await client.conversations.submitFollowupFeedbackComment(conversationId, selectedRunId, followupId, {
  request_id: anotherSavedRequestId,
  text: savedAnswerComment,
});
~~~

浏览器封装只接收 request_id 和可选 text，拒绝 analysis_text、analysis_reference、owner 或管理员身份字段。前端保存尚未确认成功的请求 ID 和文字，刷新或网络失败后重试原请求；不自动改写内容，也不回退到追问或普通消息接口。

## 网站后端如何附带管理员资料

createAgentBackend 新增可选回调 loadFeedbackAnalysis。未配置时只转发用户意见；配置后，示例先读取目标的 feedback 校验小刀归属，再调用回调取出网站侧资料。回调接收：

~~~javascript
{
  authContext,       // 本次请求的 Cookie / 可信 owner，禁止记录到日志或传给浏览器。
  conversationId,
  runId,
  followupId,        // 原报告为 null。
  requestId,
  text              // 原用户意见；省略或纯空白时为 null。
}
~~~

回调返回 null，或 {analysis_text, analysis_reference}。网站集成方需完成三件事：

1. 用当前认证身份和完整目标查找资料，确认该资料允许用于当前用户、当前报告或回答；不能只凭浏览器传入的 ID 读取任意管理员记录。这个回调不授予跨用户代评权限。
2. 按认证主体和 requestId 持久保存首次提交的目标、用户意见及管理员资料快照，保存动作须具备事务或唯一约束。同一次请求再次执行时返回原快照；若目标或用户意见改变，返回幂等冲突。不能每次重试重新读取可能被管理员修改的最新资料，单进程 Map 也不能代替重启后可用的持久记录。
3. 把已有分析内容映射为 analysis_text、内部资料标识映射为 analysis_reference。不要把原始凭据、密码或无关资料附带给小刀；不要自动截断超限材料，提交前由网站整理摘要并保存该次摘要快照。

上述持久化和管理员资料映射属于 Web 侧适配，小刀不接管网站内部资料库。相同快照可在网络异常后安全重试。后端还会核对完整请求指纹；资料变化却复用旧 ID 会返回冲突，不会重复学习。示例回调只在服务端配置，不能从浏览器请求中指定。

## 小刀如何吸收

现有赞踩仍独立保存。点踩停用目标原经验；文字反馈按目标绑定，不替换原报告、原回答和下载内容。启用 GENERIC_MEMORY_ENABLED 后，通用定位的反馈可安排后台核对：结合原问题、目标回答、用户意见、管理员资料和可用证据分析，有依据的修正才提炼为脱敏经验。管理员分析中的具体观察、排查过程和验证结果可作为外部文本证据；即使原日志不可读，也可据此核对，但仅有一句结论或“管理员已确认”不足以支持采纳。系统区分 WEB_ANALYSIS 与 ORIGINAL_LOG 来源，校验文本哈希和精确引用；这些校验确认资料来源一致，证据是否足以支持修正由审核模型判断，不承诺自动验证现实中的事实。

每次文字提交最多安排一次核对。失败不自动重跑；重启恢复未开始任务，执行状态不确定的任务标记失败。较新的反馈已经提交时，旧核对的晚到结果不能覆盖新反馈；成功采纳的修正替代被纠正目标的旧经验。后续换票不抹掉已提交文字，也不会恢复被替代的旧经验。

GENERIC_MEMORY_ENABLED 默认关闭；关闭时仍保存反馈，但不创建或积压学习任务，开启后不自动补学旧反馈。专用定位只收集反馈，不生成或召回通用经验。独立反馈不依赖 REPORT_FOLLOWUP_ENABLED，也不要求生成一条追问答案。既有 POST followups 的 intent=CORRECTION 仍兼容，适用于用户主动继续对话；新增文字负反馈不要调用该入口。

每个目标最多 32 条文字反馈（含保留的幂等回执），达到配额返回 429 / AGENT_FEEDBACK_LIMIT_EXCEEDED。数据库中的意见和管理员资料正文最长保留 7 天；后台核验工作区随诊断资源保留期清理，默认由 WEB_RESOURCE_RETENTION_DAYS 控制。已提炼的脱敏经验卡保留 90 天，原报告自然到期不立即删除经验；主动删除会话时清理反馈正文和相关工作区，停用经验并阻止晚到结果重新启用。仍可访问来源保留精简的请求指纹和回执，以确保正文清理后旧请求也不会重复提炼；已采纳经验的来源回执保留 analysis_reference、资料哈希与引用定位。

每次定位最多参考一张 4 KiB 经验卡，共享的是脱敏排查步骤与适用条件，不共享文字反馈、管理员原始资料、报告或日志。实际效果仍需用典型问题验证，确定性测试通过不等于定位质量已经提高。

## 网站验收重点

- 原报告、历史轮次和各条回答不串票、不串文字资料；分享页不开放评价入口。
- 旧 GET/PUT 的请求和响应字段完全兼容；刷新恢复最新投票。
- 点踩成功后才首次提交文字，409 时刷新票态；不把空输入框当成必须填写的评论。
- 用户意见、纯管理员资料、两者都有均可提交；两者都没有时不提交。
- 浏览器伪造 analysis_* 或身份字段被拒绝；管理员资料只由网站后端附带。
- 网络失败、刷新和网站重启后仍重放同一资料快照；改变资料必须用新请求 ID。
- 提交只显示“反馈已收到”，不新增聊天消息、不调用追问接口、不订阅新的 SSE。
- 经验开关关闭、核验失败、资料超限或配额不足时，原点踩和正常诊断不受影响。

更完整的追问恢复与故障处理见[报告追问接入](website-report-followup.md)，原报告反馈约定见[通用经验库](generic-feedback-memory.md)。
