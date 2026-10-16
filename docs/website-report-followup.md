# 网站接入：报告后的连续追问

报告交付后，用户可以继续要求解释、质疑结论或补充文字。每次回答都追加到原会话中，绑定当前选中的报告；正式报告、下载产物和赞踩保持原样。独立定位的新问题使用“新建对话”，不要把追问发到旧 `messages` 接口，也不要在追问失败后自动回退到它。

首版支持 Generic V2 和默认 `skill-direct` 生成的 Markdown 报告。`strict` / `advisory` 结构化报告不在首版范围内。旧的适用报告也可追问：若原日志副本缺失，服务端只依据报告和已有问答回答，并明确提示：未重新核对原日志。以 GET 返回的 `can_ask` 和 `reason` 为准，不在前端按报告标题猜测资格。

功能默认关闭，服务端配置 `REPORT_FOLLOWUP_ENABLED=true` 后启用。`REPORT_FOLLOWUP_SNAPSHOT_BYTES` 默认 1073741824，控制单份原日志副本的大小；可调低，最大 1 GiB。`REPORT_FOLLOWUP_STORAGE_BYTES` 默认 5368709120，控制副本总量，不能小于单份上限。副本异步准备失败或超限不会撤销已交付报告，页面仍可提供仅依据报告的问答。模型命令沿用 `DIAGNOSE_CLAUDE_COMMAND`，未设置时回退到 `CLAUDE_COMMAND`。提交追问会调用真实模型；读取、分页、订阅和恢复状态不会调用模型。

每条回答支持独立赞踩，也可复用追问入口提交明确的文字纠正。原报告评价和所有追问响应字段保持不变；接入方需要更新的文件、请求示例和经验采纳边界见[回答反馈增量适配说明](website-answer-feedback-upgrade.md)。按钮和输入框由网站实现。

## 继续诊断与报告版本

报告页区分两种操作：“追问报告”解释所选报告；“继续诊断”采用补充日志、更正后的信息和新目标，生成新版完整报告。继续诊断支持 Generic V2、skill-direct Markdown，从最新已发布报告开始。每轮创建独立 run 和 Case，重新路由；旧报告作为只读参考，不继承旧 Evidence、Outcome 或事实 ID。普通追问中的信息只有进入确认草稿后，才成为新一轮输入。

用户先预览完整问题、变更说明、诊断目标和最终日志清单，再确认一次。直接编辑和只补日志不调用草稿整理模型；未直接填写完整问题时，补充说明、变更说明或诊断目标中的自然语言复用一次 INTAKE，同时判断是否只是询问当前报告。更正信息或变更日志生成待确认草稿；纯报告追问返回 `FOLLOWUP`，不创建 Case 或 run。原有“追问报告”和共用输入框仍直接调用 followups，不增加分类调用。读取和刷新不会调用模型，也没有独立的报告比较模型。

`FOLLOWUP` 页面显示“这是对当前报告的追问，无需新建诊断”和 `followup_text`，用户点击“发送报告追问”后，调用原报告的 followups 接口。回答仍保存在该报告的问答记录中。此类草稿不能 PATCH 或 confirm；确实要更正问题或补充日志时，点击“重新准备继续诊断”。分类失败保留 `FAILED`，不会自动创建新诊断；用户明确编辑完整问题并保存后，才可核对并确认。

以下为原生 API 路径；示例 BFF 将 `/api/v1/agent` 映射为 `/api/agent`。所有接口沿用会话 owner 鉴权，不接受 `share=true` 或其他查询参数。

| 方法 | `/api/v1/agent/conversations/{conversation_id}` 后的路径 | 用途 |
| --- | --- | --- |
| POST | `/runs/{run_id}/continuation-drafts` | 从最新报告创建草稿 |
| GET | `/continuation-drafts/{draft_id}` | 读取草稿；PREPARING 时可轮询 |
| PATCH | `/continuation-drafts/{draft_id}` | 编辑完整问题和最终日志集合 |
| POST | `/continuation-drafts/{draft_id}/confirm` | 确认草稿，返回新 `run_id` |
| GET | `/runs/{run_id}/report-versions` | 读取同一问题的已发布版本与输入变化 |

创建输入包含 `request_id`、可选 `text`（自然语言更正）、`effective_problem_text`（直接编辑的完整问题）、`change_note`、`diagnosis_goal`、`log_mode` 和 `attachment_ids`。`log_mode=APPEND` 默认保留父报告实际采用的日志，再加入本次 IDs；`REPLACE` 只使用提交的 IDs。若提供 `effective_problem_text`，直接使用该内容，不再整理 `text`。问题文字最多 65536 UTF-8 字节。

PATCH 额外要求 `expected_revision`；只更新传入字段，`attachment_ids` **始终代表最终完整集合**，不再做 APPEND 合并。用户取消勾选的旧包不会进入诊断。草稿返回 `attachments` 完整元数据、`input_changes`、递增的 `revision` 和状态 `PREPARING/DRAFT/FAILED/CONFIRMED/FOLLOWUP`。新增 `followup_text` 默认为空字符串，浏览器兼容旧草稿缺少此字段；`FOLLOWUP` 必须提供非空追问文字。PATCH 不接受新的自然语言整理请求，应直接编辑或新建草稿。

确认只接收 `{request_id,expected_revision}`，返回 `{conversation_id,run_id,parent_run_id,request_id,status:"ACCEPTED"}`。收到后订阅新轮进度，停止、读取、下载和追问继续使用既有接口。网络重试沿用原请求 ID 和原内容；重复确认同一草稿只创建一轮。已有新版或正在执行正式诊断时，先刷新版本状态。

发送 `FOLLOWUP` 使用 `client.followups.send(conversationId, draft.parent_run_id, {request_id, text: draft.followup_text})`。controller 保存这次发送的请求 ID、原文和成功回执。刷新或网络重试沿用原请求；成功后再次点击只打开已有问答，不重复发送，也不调用 messages 或 confirm。请求结果未确认前不能重新准备草稿。

日志按 SHA-256 去重，每个包单独调用现有 Logparse。Generic 使用 `parse-only`，skill-direct 使用 `parse-targets`，合并清单保留包 ID、原名和文件来源。最终清单最多 20 包、总计 5 GiB；上传仍受既有会话累计资源配额约束。旧包已清理时，选择 REPLACE 并重新上传所需材料。报告正文仍可阅读和对比。

版本接口返回 `root_run_id`、`latest_run_id`、`can_continue` 和 `versions`。每项包含 `run_id`、`parent_run_id`、`version`、`created_at`、`input_changes`、`diagnosis_goal`、`runtime_changes`。只有成功发布的报告进入版本列表；失败轮次仍保留在会话历史中，父报告可以再次继续诊断。Skill、模型执行配置和解析器变化使用保存的元数据比较；模型配置指纹不代表远端实际解析出的模型身份，缺少历史记录时明确提示无法比较。

新报告以“本轮变化”说明哪些旧判断得到支持、修正或撤回，引用旧版段落及本轮实际核对的日志；之后保留完整当前诊断。未提及的旧判断不视为撤回。页面按两个指定 `run_id` 读取原报告并排展示；语义变化直接来自新报告，没有 compare 服务或额外模型任务。

### 页面接入

与现有浏览器模块一起部署 `continuation-contract.js`、`continuation-controller.js`、`continuation-view.js`、`file-sha256.js` 和 `file-sha256-worker.js`；BFF 同步部署 `continuation-bff.mjs`。两个哈希文件放在同一目录，网站 CSP 允许 `worker-src 'self'`。默认在 Worker 中分块校验并显示进度，取消会终止 Worker 或中断网络上传；Worker 不可用时，每处理 64 KiB 就让出主线程。预览还使用 `continuation-preview.js`，线上无需部署它。示例：

```javascript
import { createContinuationController } from "/xiaodao/continuation-controller.js";
import { mountContinuationView } from "/xiaodao/continuation-view.js";

const controller = createContinuationController({
  client, conversationId: detail.conversation_id, runId: detail.selected_run_id,
  onConfirmed: (receipt) => selectRun(receipt.run_id),
  onFollowup: (receipt) => selectRun(receipt.run_id), // 刷新同一报告并重建其追问区
});
const dispose = mountContinuationView(container, controller, {
  client, conversationId: detail.conversation_id, runId: detail.selected_run_id,
  onSelectRun: selectRun,
});
await controller.start();
// 离开页面或切换报告时调用 dispose()；它会销毁 controller。
```

报告正文、下载和追问都绑定当前选中的 `selected_run_id`。`onFollowup` 在发送成功后调用，网站应刷新该报告的问答区并移到该区域；示例预览已接好这一操作。继续诊断按钮以版本接口的 `can_continue` 为准，不沿用旧轮已经只读的资源状态推断新轮资格。

## 接口与身份

以下为网站 BFF 路径。xiaodao 原生路径在 `/api` 后增加 `/v1`，其余部分相同。完整字段表见 [Agent API 参考](website-agent-api.md)。

| 方法 | 网站路径 | 输入 |
| --- | --- | --- |
| GET | `/api/agent/conversations/{conversation_id}/runs/{run_id}/followups` | 可选 `cursor`、`limit`，默认最新 50 条，最大 100 条 |
| POST | 同上 | `{request_id,text}`，只接收文字；纠正可加 `intent=CORRECTION`、`target_followup_id` |
| GET | 上一路径加 `/events` | `Last-Event-ID` 请求头 |
| POST | 上一路径加 `/{followup_id}/stop`（不含 `/events`） | `{request_id}` |
| GET / PUT | 追问路径加 `/{followup_id}/feedback` | GET 无请求体；PUT 为 `{request_id,rating}`，独立评价该回答 |

`run_id` 必须取当前展示报告的 `selected_run_id`，不能总取 `current_run.run_id`。切换历史报告时，销毁旧 controller，再为新报告创建实例。每个会话同时最多有一个活动追问；`active_followup` 可能属于另一份报告，停止时使用它自己的 `run_id` 和 `followup_id`，不要把它插入当前报告的问答列表。

默认接入方式是 BFF 原样透传 Cookie，由 xiaodao 的 Redis 会话鉴权读取 `user.userid` 并计算 owner。读取、提交、停止和 SSE 都须携带同一登录 Cookie。浏览器传入的 `X-Agent-Owner-Key` 不会被采用，知道 UUID 也不代表有权访问。

只有显式向 `createAgentBackend` 传入 `access`，或配置 `WEBSITE_AUTH_MODULE` 时，BFF 才会调用 `access.authenticate(request)` 并根据返回的可信用户身份计算 owner。该旧接入方式须配合服务端 `WEBSITE_AUTH_MODE=trusted_header`，并由网站的登录模块校验登录态及 CSRF / Origin。默认 Cookie 透传 BFF 不自动执行 CSRF / Origin 校验，网站必须保留自己的校验入口；下例中的 `X-CSRF-Token` 也只有被该入口接收并验证才生效。两种方式的部署配置见 [Redis 会话接入说明](website-redis-deployment.md)。

`request_id` 为 1 到 128 个 Unicode 字符，不能全为空白；`text` 非空白，最多 65536 字符且最多 65536 UTF-8 字节。不要用 JavaScript 字符串长度代替 UTF-8 字节长度。同一逻辑提交重试保持 ID 和原文；新问题使用新 ID。停止操作有自己独立且稳定的 ID。

## 直接复用浏览器模块

复制 [示例目录](../examples/website-agent/README.md) 中的 `browser-client.js`、`followup-contract.js`、`continuation-contract.js`、`followup-controller.js`、`followup-view.js`、`report-view.js` 和 `report-view.css` 到网站静态资源目录；复用共用输入框时，一并部署 `conversation-input.js`。这些模块应来自同一次发布，保留相对导入路径，不要只替换入口文件。controller 发真实的同源请求；离线预览仅替换它的 `fetchImpl`，没有另一套页面逻辑。

BFF 同步部署 `server.ts`、`server.mjs`、`followup-bff.mjs`、`followup-contract.js`、`continuation-bff.mjs` 和 `continuation-contract.js`。即使 `REPORT_FOLLOWUP_ENABLED=false`，也必须保留这些静态导入的模块，否则 BFF 无法启动。后端、BFF 和浏览器模块的累计升级顺序见[生产升级清单](production-upgrade-2026-09-23.md)。

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
6. 事件游标超出当前范围时返回 `409 / AGENT_FOLLOWUP_INVALID_CURSOR`，先重新 GET 快照，再从新游标订阅。已有追问及回答永久保留；日志正常保留 15 天，磁盘紧张时可提前回收已完成轮次。资源清理或追问期限结束后，`can_ask=false`、`reason=EXPIRED`，历史仍可读取。用户主动删除会话后才返回 404。只在本地保存游标而不保存历史，无法靠该游标重建页面；本示例刷新后总从快照恢复。

SSE 在所选报告的追问全部终止且待发事件已发完后关闭。另一报告忙时，可用 GET 刷新会话级 `active_followup`；不会把另一报告的事件塞进当前流。网络中断或页面刷新不会停止回答，若要停止，必须显式调用 stop。

controller 在提交前保存原文和稳定请求 ID，收到回执后保存回执身份。响应丢失时先刷新；若列表里已有相同 `request_id`，直接恢复该记录。结果仍不确定时，由用户重试原请求，不生成新 ID。不会自动重试模型提交。

## 可用性与边界

| 状态或错误 | 页面行为 |
| --- | --- |
| `snapshot_status=READY` | 提示可以结合报告和原日志回答；单条回答实际依据以 `context_mode` 为准 |
| `UNAVAILABLE`、`PENDING`、`BUILDING`、`FAILED` | 提示当前只依据报告和已有问答，未重新核对日志 |
| `AGENT_FOLLOWUP_DISABLED` | 提示功能尚未启用，不回退到 messages |
| `AGENT_FOLLOWUP_UNSUPPORTED` | 提示报告暂不支持追问 |
| `AGENT_FOLLOWUP_EXPIRED` | 提示本轮已结束追问，报告和历史问答仍可查看 |
| `AGENT_FOLLOWUP_BUSY` | 等待活动回答结束，或显式停止后再问 |
| `AGENT_FOLLOWUP_SOURCE_CHANGED` | 刷新报告状态后重新提问 |
| `AGENT_FOLLOWUP_LIMIT_EXCEEDED` | 每份报告最多 100 条问答；提示新建对话 |
| `AGENT_FOLLOWUP_CONTEXT_LIMIT` | 单次提示最多 262144 字节；支持 Read/Grep 时，较长历史保存在完整历史文件中，提示放入索引和可容纳的最近问答。仍无法满足限额时拒绝提交 |
| `AGENT_FOLLOWUP_INVALID_CURSOR` | 分页游标无效返回 400，重新读取最新页；事件游标超出范围返回 409，重新 GET 快照并续订独立事件流 |
| `AGENT_FOLLOWUP_NOT_FOUND` | 提示追问不存在或已删除 |
| `AGENT_IDEMPOTENCY_CONFLICT` | 核对原 ID 和文字，不将冲突请求换 ID 自动补发 |

服务重启时，仍处于 `QUEUED`、尚未开始且资源仍可用的已受理追问，会在功能启用时继续执行，即使此时刚超过 15 天期限；资源清理须等任务结束。如果功能关闭，排队任务会取消。原先处于 `RUNNING` 的任务无法确认模型执行结果，会标为 `INTERRUPTED`，不会自动重跑；期限内可新发一条问题。取消、失败和中断也保留在问答记录中。报告追问不接收新日志、不重写正式报告；需要这些操作时使用“继续诊断”。专用 `strict` / `advisory` 报告追问不属于首版能力。

每条回答的 `context_mode` 为 `REPORT_ONLY` 或 `REPORT_AND_LOGS`。前者只依据报告和已有问答；后者可读取原日志副本。日志后来准备好，也不能把较早的 `REPORT_ONLY` 回答显示成已核对日志。

长对话不会删除较早问答。支持只读 Read/Grep 的运行命令可从 `inputs/history.md` 查阅完整历史，单次提示同时提供历史索引和能容纳的最近问答。命令不支持此能力，或原问题、报告与当前追问仍超过单次提示限额时，返回 `AGENT_FOLLOWUP_CONTEXT_LIMIT`。网页继续按原分页接口展示完整问答，不需要读取服务端工作区文件。

新网站在诊断中发送补充时必须携带保存的 `target_run_id`，把发送固定到仍未交付报告的当前轮。协议仅为兼容旧客户端允许省略此字段。报告刚好生成或轮次变化时会返回 `409 / AGENT_RUN_CHANGED`；先重新读取同一 `run_id`，确认报告已生成后将文字送到 followups，不能重发一条不带目标轮次的消息。附件不进入追问；需要新日志时使用“继续诊断”预览并确认，独立问题使用新建对话。追问失败也不能退回旧消息接口。

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

继续诊断预览中，输入固定样例“请解释报告中的结论。”，不另填目标、变更说明或新日志，可体验 `FOLLOWUP` 页面及发送后的问答区。这个固定样例仅用于离线演示；线上语义判断由服务端完成。`continuation.test.mjs` 覆盖草稿兼容、纯追问不能确认、跨刷新重试、成功后防重复发送和原报告版本不变。

`examples/website-agent/followup.test.mjs` 由原有 `server.test.mjs` Gate 导入，覆盖 DTO/轮次校验、BFF 登录和 owner、SSE 分片回放、游标越界恢复、响应丢失重试、迟到回执、旧页覆盖竞态、跨报告停止、安全文本展示及离线预览。正式验收仍使用仓库 Test Flow 的 `verdict.json`；这些确定性测试不调用模型，也不替代真实 Agent 的只读日志验证。
