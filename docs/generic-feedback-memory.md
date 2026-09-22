# 通用定位经验库与网站赞踩接入

首版由网站实现“有帮助 / 没帮助”两个按钮，小刀提供反馈接口、浏览器调用封装和后台经验处理。不新增按钮样式、取消评价、文字评论、解决确认、经验管理或提炼进度，也不新增 SSE 事件。

## 1. 接入路径和归属

调用关系仍为：浏览器 → 网站同源后端 → 小刀。网站后端根据登录身份派生 `X-Agent-Owner-Key`；浏览器不能指定用户归属。网站现有登录、Origin / CSRF 校验同样适用于提交评价。

| 操作 | 小刀服务路径 | 网站示例路径 |
| --- | --- | --- |
| 读取评价 | `GET /api/v1/agent/conversations/{conversation_id}/runs/{run_id}/feedback` | `GET /api/agent/conversations/{conversation_id}/runs/{run_id}/feedback` |
| 提交评价 | `PUT /api/v1/agent/conversations/{conversation_id}/runs/{run_id}/feedback` | `PUT /api/agent/conversations/{conversation_id}/runs/{run_id}/feedback` |

两个接口都不接受查询参数，GET 不接受请求体。路径标识必须是小写规范 UUID。查看历史报告时，使用报告的 `selected_run_id`，不要自动替换成当前轮次。

PUT 使用 `Content-Type: application/json`，只接受两个字段：

```json
{
  "request_id": "本次评价的唯一标识",
  "rating": "LIKE"
}
```

`request_id` 是 1 到 128 个 Unicode 字符的非空字符串，不能全为空白；`rating` 只接受 `LIKE` 和 `DISLIKE`。未定义字段会被拒绝。

GET 和 PUT 都返回现有的 `{ok,data,error}` 格式：

```json
{
  "ok": true,
  "data": {
    "schema_version": 1,
    "conversation_id": "10000000-0000-0000-0000-000000000001",
    "run_id": "50000000-0000-0000-0000-000000000001",
    "can_rate": true,
    "rating": "LIKE",
    "updated_at": "2026-09-21T12:00:00Z"
  },
  "error": null
}
```

尚未评价时，`rating` 和 `updated_at` 都是 `null`。首版仅支持已交付的通用定位 V2 正式报告，服务端检查来源 Job 和报告身份，不靠 Markdown 格式猜测。功能关闭或报告不适用时，GET 返回 `can_rate=false`；PUT 返回 `409 / AGENT_FEEDBACK_UNSUPPORTED`。

前端只读取当前用户、指定报告的评价，不返回投票总数、经验内容或其他用户的报告。

## 2. 浏览器调用与换票

复制更新后的 `examples/website-agent/browser-client.js`，沿用网站已有的 CSRF 设置：

```javascript
import { createAgentClient } from "/xiaodao/browser-client.js";

const client = createAgentClient({
  headers: () => ({ "X-CSRF-Token": readWebsiteCsrfToken() }),
});
const report = { conversationId, runId: conversation.selected_run_id };
const state = await client.conversations.getFeedback(report.conversationId, report.runId, {
  signal: new AbortController().signal,
});
// 根据 state.can_rate 显示按钮，根据 state.rating 显示选中状态。

// 将这份请求保存到本次提交状态中，失败重试时复用原对象。
const pendingVote = { request_id: crypto.randomUUID(), rating: "LIKE" };
const updated = await client.conversations.setFeedback(report.conversationId, report.runId, pendingVote);
// 仅当页面仍显示 report 中的会话和轮次时，用 updated 更新选中状态。
```

封装成功时返回解包后的 `data`，失败抛出 `AgentApiError`。它校验响应身份和字段，不自动生成请求 ID、不自动重试，也不缓存投票。

网站需落实以下规则：

- 同一报告的提交串行处理；提交期间禁用两个按钮，或使用严格的单队列，避免并发换票改变操作顺序。
- 网络重试保留原 `request_id` 和 `rating`。同一 ID 改参数返回 `AGENT_IDEMPOTENCY_CONFLICT`。换票使用新 ID。
- 以服务端响应为准；旧点赞请求重放时可能返回最新的 `DISLIKE`，不能用旧请求内容覆盖它。
- 重复同票不会累计票数，也不会重复提炼经验。再次点击已选中的按钮不表示取消评价。
- 切换报告时取消旧的状态读取，并核对响应中的 `conversation_id`、`run_id`。不要把旧报告的迟到响应显示到新报告上。
- 读取和提交失败时保留已显示的报告。无法确认提交是否成功时，先查询反馈状态；如需重试，复用保存的请求。

| HTTP 状态 | 常见错误码或含义 | 网站处理 |
| --- | --- | --- |
| `400` | 参数无效 | 核对字段、标识和请求格式 |
| `401` / `403` | 网站登录或来源校验失败 | 沿用网站的登录和授权流程 |
| `404` | `AGENT_CONVERSATION_NOT_FOUND` / `AGENT_RUN_NOT_FOUND` | 会话、轮次不存在，已删除或不属于当前用户 |
| `409` | `AGENT_FEEDBACK_UNSUPPORTED` | 隐藏或禁用评价入口 |
| `409` | `AGENT_IDEMPOTENCY_CONFLICT` | 重试须使用原内容；新操作使用新 ID |
| `429` | `AGENT_FEEDBACK_LIMIT_EXCEEDED` | 反馈存储配额已满，联系管理员；不自动重试 |
| `502` / `503` | 响应异常或服务暂不可用 | 保留报告与请求，提示稍后查询 |

`examples/website-agent/server.mjs` 是网站转发的唯一实现；`server.ts` 继续作为类型与启动入口。后端仅转发两个允许的输入字段，并核对上游响应身份，不把内部错误原文暴露给浏览器。

## 3. 后台经验闭环

首次点赞在同一事务内保存反馈和唯一提炼任务。后台从问题和正式报告提炼一张经验卡，包括问题特征、适用条件、有效排查步骤和限制。用户点赞只表示认为回答有帮助，不代表根因已经验证。

每份报告最多安排一次提炼。点踩停用该报告的经验，再次点赞复用已有经验或原任务，不重新调用模型。任务完成时重新检查最新投票和会话状态，因此晚到结果不能覆盖点踩或会话删除。

提炼失败保留投票，不自动重试。重启后恢复尚未开始的任务；运行状态不确定的任务标记失败，避免重复调用模型。点踩不会自动判定诊断中引用的其他经验错误。

经验库按同一 `DATA_ROOT + GENERIC_SKILL_NAME` 共享，范围是同一部署和同一通用 Skill。共享内容为脱敏经验摘要，不向其他用户开放原报告或日志。专用定位不生成或读取这些经验。

首版按中英文关键词确定性匹配，每次最多采用一张经验卡，最多 4 KiB；超预算则整张跳过。经验放在独立的历史参考区，不能替代本次事实或覆盖 Skill 指令，用户原话保持不变。本次采用的内容、来源和哈希随执行记录保存。检索、存储或提炼异常不应阻断正常诊断。

主动删除来源会话时停用相关经验，并阻止后台任务重新启用。历史报告按保留策略自然清理时，已提炼的经验继续保留。不自动学习没有点赞的旧报告，不合并经验、不使用向量服务，也不自动修改 Skill。

首版采用固定容量和保留期限：每份报告最多保存 128 个不同的请求 ID，反馈来源和提炼任务各最多 10,000 条。达到上限时整次提交返回 429，不会只保存一半。问题原文和报告各限 64 KiB；待处理来源最多保留 7 天，成功或失败后立即清除任务中的原文。经验卡保留 90 天，期满停用并清除内容，不重新提炼。仍可访问的来源保留幂等记录，来源自然清理后再清理相应记录。

## 4. 部署和验收

`GENERIC_MEMORY_ENABLED=false` 为默认值。开启前先完成网站登录、反馈归属、指定轮次、请求幂等、赞踩切换、提炼失败、重启与删除验证，并确认实际安装的通用 Skill 能读取经验参考。

真实模型验证先检查对应入口的 `--plan-only`，核对身份、调用数、预算和阻塞项。使用同一组典型问题比较启用前后的正确性、排查步骤、时延和模型消耗；确定性测试通过本身不能证明诊断质量提高。

完成验收后再启用。关闭开关停止新评价、提炼和经验召回，已有数据继续按保留期限清理，无需回滚普通定位流程。脱敏依赖模型抽象，格式和敏感信息检查无法识别所有私有名称；启用前须用典型数据核查脱敏结果。正式 Test Flow 的 affected 与 full deterministic 结论以对应源码快照的 `verdict.json` 为准。
