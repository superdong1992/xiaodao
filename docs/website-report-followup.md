# 网站接入：报告后的连续追问

报告交付后，用户可以继续要求解释、质疑结论或补充文字。每次回答都追加到原会话中，绑定当前选中的报告；正式报告、下载产物和赞踩保持原样。独立定位的新问题使用“新建对话”，不要把追问发到旧 `messages` 接口，也不要在追问失败后自动回退到它。

首版支持 Generic V2 和默认 `skill-direct` 生成的 Markdown 报告。`strict` / `advisory` 结构化报告不在首版范围内。旧的适用报告也可追问：若原日志副本缺失，服务端只依据报告和已有问答回答，并明确提示：未重新核对原日志。以 GET 返回的 `can_ask` 和 `reason` 为准，不在前端按报告标题猜测资格。

功能默认关闭，服务端配置 `REPORT_FOLLOWUP_ENABLED=true` 后启用。`REPORT_FOLLOWUP_SNAPSHOT_BYTES` 默认 1073741824，控制单份原日志副本的大小；可调低，最大 1 GiB。`REPORT_FOLLOWUP_STORAGE_BYTES` 默认 5368709120，控制副本总量，不能小于单份上限。副本异步准备失败或超限不会撤销已交付报告，页面仍可提供仅依据报告的问答。模型命令沿用 `DIAGNOSE_CLAUDE_COMMAND`，未设置时回退到 `CLAUDE_COMMAND`。提交追问会调用真实模型；读取、分页、订阅和恢复状态不会调用模型。

## 接口与身份

以下为网站 BFF 路径。xiaodao 原生路径在 `/api` 后增加 `/v1`，其余部分相同。完整字段表见 [Agent API 参考](website-agent-api.md)。

| 方法 | 网站路径 | 输入 |
| --- | --- | --- |
| GET | `/api/agent/conversations/{conversation_id}/runs/{run_id}/followups` | 可选 `cursor`、`limit`，默认最新 50 条，最大 100 条 |
| POST | 同上 | `{request_id,text}`，只接收文字 |
| GET | 上一路径加 `/events` | `Last-Event-ID` 请求头 |
| POST | 上一路径加 `/{followup_id}/stop`（不含 `/events`） | `{request_id}` |

`run_id` 必须取当前展示报告的 `selected_run_id`，不能总取 `current_run.run_id`。切换历史报告时，销毁旧 controller，再为新报告创建实例。每个会话同时最多有一个活动追问；`active_followup` 可能属于另一份报告，停止时使用它自己的 `run_id` 和 `followup_id`，不要把它插入当前报告的问答列表。

默认接入方式是 BFF 原样透传 Cookie，由 xiaodao 的 Redis 会话鉴权读取 `user.userid` 并计算 owner。读取、提交、停止和 SSE 都须携带同一登录 Cookie。浏览器传入的 `X-Agent-Owner-Key` 不会被采用，知道 UUID 也不代表有权访问。

只有显式向 `createAgentBackend` 传入 `access`，或配置 `WEBSITE_AUTH_MODULE` 时，BFF 才会调用 `access.authenticate(request)` 并根据返回的可信用户身份计算 owner。该旧接入方式须配合服务端 `WEBSITE_AUTH_MODE=trusted_header`，并由网站的登录模块校验登录态及 CSRF / Origin。默认 Cookie 透传 BFF 不自动执行 CSRF / Origin 校验，网站必须保留自己的校验入口；下例中的 `X-CSRF-Token` 也只有被该入口接收并验证才生效。两种方式的部署配置见 [Redis 会话接入说明](website-redis-deployment.md)。

`request_id` 为 1 到 128 个 Unicode 字符，不能全为空白；`text` 非空白，最多 65536 字符且最多 65536 UTF-8 字节。不要用 JavaScript 字符串长度代替 UTF-8 字节长度。同一逻辑提交重试保持 ID 和原文；新问题使用新 ID。停止操作有自己独立且稳定的 ID。

## 直接复用浏览器模块

复制 [示例目录](../examples/website-agent/README.md) 中的 `browser-client.js`、`followup-contract.js`、`followup-controller.js`、`followup-view.js`、`report-view.js` 和 `report-view.css` 到网站静态资源目录；复用共用输入框时，一并部署 `conversation-input.js`。这些模块应来自同一次发布，保留相对导入路径，不要只替换入口文件。controller 发真实的同源请求；离线预览仅替换它的 `fetchImpl`，没有另一套页面逻辑。

BFF 同步部署 `server.ts`、`server.mjs`、`followup-bff.mjs` 和 `followup-contract.js`。即使 `REPORT_FOLLOWUP_ENABLED=false`，也必须保留这些静态导入的模块，否则 BFF 无法启动。后端、BFF 和浏览器模块的累计升级顺序见[生产升级清单](production-upgrade-2026-09-23.md)。

```javascript
import { createAgentClient } from "/xiaodao/browser-client.js";
import { createFollowupController } from "/xiaodao/followup-controller.js";
import { mountFollowupView } from "/xiaodao/followup-view.js";

const client = createAgentClient({ headers: () => ({ "X-CSRF-Token": readCsrfToken() }) });
let dispose;
async function selectReport(conversation) {
  dispose?.();
  const controller = createFollowupController({
    client,
    conversationId: conversation.conversation_id,
    runId: conversation.selected_run_id,
    storage: sessionStorage,
    storageNamespace: `xiaodao-followup:${signedInUserStorageKey}`,
  });
  dispose = mountFollowupView(document.querySelector("#report-followups"), controller);
  try { await controller.start(); }
  catch { /* 问答区已显示读取失败，可点击“刷新记录”。 */ }
}
```

`signedInUserStorageKey` 是网站本地草稿的用户命名空间，不是 xiaodao owner，不参与服务端鉴权。退出登录时清除对应草稿；切换账号不能沿用上一用户的本地状态。controller 在首次授权查询成功后才展示恢复的草稿。浏览器不能保存草稿时，页面会显示提示；业务记录仍以服务端为准。

调用 `setDraft(text)` 保存输入，`submit()` 发送或重试同一未确认请求，`loadOlder()` 加载更早问答，`refresh()` 恢复快照并重新订阅，`stop()` 停止会话当前活动追问。`subscribe(listener)` 用于接入自己的组件，返回取消监听函数；页面退出调用 `destroy()`。销毁只断开本地请求和事件流，不会停止服务端模型任务。

示例 Markdown 沿用报告的 `textContent` 原文展示，不解析 HTML、不自动打开链接。换成网站自己的 Markdown 组件时，继续过滤 HTML 和危险链接。不要把原日志、回答或用户文字当作 HTML 插入页面。

## 刷新、重试与事件恢复

1. 首次打开和刷新先 GET 最新追问快照，显示 `items`，保存其 `last_event_id`。
2. 用 fetch 订阅本报告的 `/followups/events`，把该序号放入 `Last-Event-ID`。快照和订阅之间产生的事件会回放，不会漏掉。
3. 事件帧只有 `data:` 和连接/心跳注释；不发送 `id:`、`event:` 或 `retry:`。业务类型读 JSON 的 `type`，游标读 `sequence`。仅处理匹配当前会话与轮次的事件，按序号去重，处理成功后才推进游标。
4. 较早页只补齐未加载的项，不能用旧页中的 RUNNING 覆盖实时收到的 COMPLETED，也不能用分页响应推进实时游标。
5. POST 回执仅保存请求 ID、追问 ID 和接收序号，再 GET 最新状态。回执可能比完成事件更晚到达，不能用 `ACCEPTED` 把已完成回答改回排队。
6. 事件游标超出当前范围时返回 `409 / AGENT_FOLLOWUP_INVALID_CURSOR`，先重新 GET 快照，再从新游标订阅。追问随整轮记录到期清理，清理后返回 404，不提供单独的“追问事件过期”错误码。只在本地保存一个游标而不保存历史，无法靠该游标重建页面；本示例刷新后总从快照恢复。

SSE 在所选报告的追问全部终止且待发事件已发完后关闭。另一报告忙时，可用 GET 刷新会话级 `active_followup`；不会把另一报告的事件塞进当前流。网络中断或页面刷新不会停止回答，若要停止，必须显式调用 stop。

controller 在提交前保存原文和稳定请求 ID，收到回执后保存回执身份。响应丢失时先刷新；若列表里已有相同 `request_id`，直接恢复该记录。结果仍不确定时，由用户重试原请求，不生成新 ID。不会自动重试模型提交。

## 可用性与边界

| 状态或错误 | 页面行为 |
| --- | --- |
| `snapshot_status=READY` | 提示可以结合报告和原日志回答；单条回答实际依据以 `context_mode` 为准 |
| `UNAVAILABLE`、`PENDING`、`BUILDING`、`FAILED` | 提示当前只依据报告和已有问答，未重新核对日志 |
| `AGENT_FOLLOWUP_DISABLED` | 提示功能尚未启用，不回退到 messages |
| `AGENT_FOLLOWUP_UNSUPPORTED` | 提示报告暂不支持追问 |
| `AGENT_FOLLOWUP_EXPIRED` | 提示报告已过保留期 |
| `AGENT_FOLLOWUP_BUSY` | 等待活动回答结束，或显式停止后再问 |
| `AGENT_FOLLOWUP_SOURCE_CHANGED` | 刷新报告状态后重新提问 |
| `AGENT_FOLLOWUP_LIMIT_EXCEEDED` | 每份报告最多 100 条问答；提示新建对话 |
| `AGENT_FOLLOWUP_CONTEXT_LIMIT` | 单次提示最多 262144 字节；支持 Read/Grep 时，较长历史保存在完整历史文件中，提示放入索引和可容纳的最近问答。仍无法满足限额时拒绝提交 |
| `AGENT_FOLLOWUP_INVALID_CURSOR` | 分页游标无效返回 400，重新读取最新页；事件游标超出范围返回 409，重新 GET 快照并续订独立事件流 |
| `AGENT_FOLLOWUP_NOT_FOUND` | 提示追问不存在或已过保留期 |
| `AGENT_IDEMPOTENCY_CONFLICT` | 核对原 ID 和文字，不将冲突请求换 ID 自动补发 |

服务重启时，仍处于 `QUEUED`、尚未开始且报告未到期的追问，会在功能启用时继续执行；如果功能关闭，排队任务会取消。原先处于 `RUNNING` 的任务无法确认模型执行结果，会标为 `INTERRUPTED`，不会自动重跑；用户可新发一条问题。取消、失败和中断也保留在问答记录中。新日志上传、重写正式报告、修改赞踩以及专用 `strict` / `advisory` 报告追问均不属于首版能力。

每条回答的 `context_mode` 为 `REPORT_ONLY` 或 `REPORT_AND_LOGS`。前者只依据报告和已有问答；后者可读取原日志副本。日志后来准备好，也不能把较早的 `REPORT_ONLY` 回答显示成已核对日志。

长对话不会删除较早问答。支持只读 Read/Grep 的运行命令可从 `inputs/history.md` 查阅完整历史，单次提示同时提供历史索引和能容纳的最近问答。命令不支持此能力，或原问题、报告与当前追问仍超过单次提示限额时，返回 `AGENT_FOLLOWUP_CONTEXT_LIMIT`。网页继续按原分页接口展示完整问答，不需要读取服务端工作区文件。

新网站在诊断中发送补充时必须携带保存的 `target_run_id`，把发送固定到仍未交付报告的当前轮。协议仅为兼容旧客户端允许省略此字段。报告刚好生成或轮次变化时会返回 `409 / AGENT_RUN_CHANGED`；先重新读取同一 `run_id`，确认报告已生成后将文字送到 followups，不能重发一条不带目标轮次的消息。附件不进入追问；需要新日志时新建对话。追问失败也不能退回旧消息接口。

共用输入框可直接复用 `conversation-input.js`。它按页面已确认的报告状态选择入口，为诊断补充强制带上 `target_run_id`，只在上述 409 竞态后重新查询并转为报告追问。调用前由网站保存以下全部参数，重试保持原值，尤其不要因报告后来生成就修改 `reportReady`；响应丢失时必须先让原入口命中旧收据，避免同一补充又触发一次追问。

```javascript
import { sendConversationInput } from "/xiaodao/conversation-input.js";

const result = await sendConversationInput({
  client, conversationId, runId: selectedRunId,
  requestId: savedRequestId, text: savedText, attachmentIds: savedAttachmentIds,
  reportReady: savedReportReady,
});
// result.kind 为 message 或 followup。收到回执后读取对应快照，不用回执覆盖已完成答案。
```

## 离线预览与验证

运行 `node examples/website-agent/preview.mjs` 后，选择“通用 Markdown”或“旧报告追问”，可体验连续提问、仅依据报告回答、停止、刷新记录及报告切换。“新建对话并开始定位”会调用 create 再 send，追问表单只调用 followups。预览是浏览器内的合成数据，刷新整个页面会重置模拟服务，不能用于证明服务端持久化。

`examples/website-agent/followup.test.mjs` 由原有 `server.test.mjs` Gate 导入，覆盖 DTO/轮次校验、BFF 登录和 owner、SSE 分片回放、游标越界恢复、响应丢失重试、迟到回执、旧页覆盖竞态、跨报告停止、安全文本展示及离线预览。正式验收仍使用仓库 Test Flow 的 `verdict.json`；这些确定性测试不调用模型，也不替代真实 Agent 的只读日志验证。
