# 内部网站接入 Agent 对话与实时进度

适用于 xiaodao `8.0.0`、V11 / `v11-contract-r1`。网站把用户原话和日志交给 xiaodao，展示追问、执行进度和经过服务端验证的定位报告。一次会话只对应一次定位任务。

首次接入先读 [部署后快速接入与联调清单](website-agent-quickstart.md)。本文是完整接口参考。在线入口是 xiaodao 服务的 `/docs` 和 `/openapi.json`；仓库保存 [完整 OpenAPI 快照](../schemas/v2/web-api.openapi.snapshot.json)。联调前核对线上 `info.version` 和路由，不能假设已部署服务与当前源码一致。

调用关系：网站前端 → 网站后端 → Linux 上的 xiaodao REST API。网站原有问答功能保持独立。网站后端负责登录校验、会话和附件归属、事件订阅权限，以及报告下载权限；UUID 不是授权凭据。不要把 xiaodao 内部地址或下载地址直接交给浏览器。

## 1. 网站开发者需要实现的流程

1. 创建会话，保存网站用户与 `conversation_id` 的归属关系。
2. 用户发送原话，或先预约并上传日志，再发送附件 ID。网站不需要构造 `problem_spec` 或命名事实。
3. 订阅 SSE。显示 `assistant.question` 的追问；用户继续调用消息接口回答。显示 `agent.progress` 的阶段消息。
4. 收到 `result.available` 后查询关联 Case 和产物列表，校验并下载报告 JSON，按中文结构展示。不要等待 ZIP 才展示报告。
5. `archive.updated` 的状态变为 `READY` 后提供 ZIP 下载按钮。用户点击并确认包含原始目标日志后才下载。
6. 收到 `conversation.completed` 后关闭订阅。报告完成后的新问题另建会话。

关闭页面或断开事件流不会停止后台任务。刷新页面后可查询会话快照并回放历史。服务重启后，未完成任务会明确标为 `INTERRUPTED`；用户需重新创建会话。已完成报告保留，待生成 ZIP 继续按既有归档机制恢复。

全部用户原话保存在会话历史中。关联 Case 的 `raw_problem_text` 使用经过来源校验的问题描述片段，不拼接整段聊天记录；网站展示原话时读取会话消息，不把该字段当作聊天历史。

## 2. 接口和公共合同

以下路径是 **xiaodao 服务接口**。部署示例的 `$BASE` 是网站后端可访问的 xiaodao 地址，例如 `http://xiaodao.internal:8000`。本文 UUID 仅用于说明，实际调用需使用创建响应中的值。

| 方法与路径 | 输入或用途 |
| --- | --- |
| `POST /api/v1/agent/conversations` | 创建会话，JSON：`request_id` |
| `POST /api/v1/agent/conversations/{conversation_id}/messages` | 发送消息，JSON：`request_id`、可空 `text`、`attachment_ids` |
| `GET /api/v1/agent/conversations/{conversation_id}` | 会话快照、追问、消息采用状态、附件、Case ID、事件游标 |
| `GET /api/v1/agent/conversations/{conversation_id}/events` | SSE 历史回放和实时订阅；可带 `Last-Event-ID` 请求头 |
| `POST /api/v1/agent/conversations/{conversation_id}/attachments` | 预约日志上传 |
| `PUT /api/v1/agent/attachments/{attachment_id}/content` | 上传文件原始字节 |
| `GET /api/v1/cases/{case_id}` | 读取权威 Case、结果来源 Job 和产物摘要 |
| `GET /api/v1/cases/{case_id}/artifacts` | 现有公共产物列表和下载描述符 |
| `GET /api/v1/artifacts/{artifact_id}/content?case_id={case_id}` | 按列表返回的地址下载产物 |

六个 Agent 接口均不接受查询参数。路径标识和 `attachment_ids` 必须是小写规范 UUID。JSON 未定义字段、字符串化数组、重复附件 ID 会被拒绝。消息至少包含非空文本或一个已上传附件；`text` 可省略或为 `null`。文本和 `request_id` 分别最多 65,536 UTF-8 字节，一条消息最多 20 个附件。

创建、发消息和预约上传都使用稳定 `request_id`。同一逻辑请求重试时保持内容和 ID 不变；内容改变会返回幂等冲突。新消息使用新 ID。网站应按登录用户为创建请求划分命名空间，避免不同用户使用相同 ID 命中同一个创建回执；下方 TypeScript 示例已实现。

成功响应统一为：

```json
{"ok":true,"data":{},"error":null}
```

错误响应统一为：

```json
{
  "ok": false,
  "data": null,
  "error": {
    "code": "AGENT_IDEMPOTENCY_CONFLICT",
    "message": "同一消息请求的内容不能更改。",
    "details": [],
    "retryable": false
  }
}
```

常见 HTTP 状态：`400` 参数错误；`404` 不存在；`409` 状态或幂等冲突；`413` 超出限制；`422` 文件校验失败；`503` 服务未配置或暂时不可用。网站自身还应返回 `401` 未登录和 `403` 无权访问。Agent 错误码属于 Agent 接口；现有 Case 接口仍使用既有错误合同。

### 创建与发送消息

```http
POST /api/v1/agent/conversations
Content-Type: application/json

{"request_id":"website-conversation-20260907-001"}
```

```json
{
  "ok": true,
  "data": {
    "conversation_id": "10000000-0000-0000-0000-000000000001",
    "request_id": "website-conversation-20260907-001",
    "schema_version": 1
  },
  "error": null
}
```

```http
POST /api/v1/agent/conversations/10000000-0000-0000-0000-000000000001/messages
Content-Type: application/json

{"request_id":"message-001","text":"付款服务调用库存服务时频繁超时，请帮我定位。","attachment_ids":[]}
```

```json
{
  "ok": true,
  "data": {
    "conversation_id": "10000000-0000-0000-0000-000000000001",
    "message_id": "20000000-0000-0000-0000-000000000001",
    "request_id": "message-001",
    "event_id": 1,
    "status": "ACCEPTED"
  },
  "error": null
}
```

`ACCEPTED` 只表示消息已持久接收。后台 INTAKE 负责整理问题和追问；事实缺失时不会替用户编造。定位运行期间的新消息显示“已收到，尚未用于本次诊断”，只在合法补充点采用。更正已冻结事实或任务目标时应另建任务。

### 预约和上传日志

文件名只接受现有压缩日志格式：`.zip`、`.tar`、`.tar.gz`、`.tgz`、`.gz`。文件名不得包含目录或控制字符，压缩后缀使用小写。类型分别为 `application/zip`、`application/x-tar`、`application/gzip`。单文件最多 2,684,354,560 字节，每会话最多 20 个附件且总量受服务端限制；不支持截图或 PDF 解析。

先计算文件真实字节数和完整 SHA-256，再预约。以下 digest 仅演示字段格式，不能直接用于上传真实文件。

```http
POST /api/v1/agent/conversations/10000000-0000-0000-0000-000000000001/attachments
Content-Type: application/json

{
  "request_id": "attachment-001",
  "name": "logs.zip",
  "content_type": "application/zip",
  "declared_size": 1024,
  "declared_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

```json
{
  "ok": true,
  "data": {
    "attachment": {
      "attachment_id": "30000000-0000-0000-0000-000000000001",
      "conversation_id": "10000000-0000-0000-0000-000000000001",
      "request_id": "attachment-001",
      "name": "logs.zip",
      "content_type": "application/zip",
      "size": 1024,
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "status": "RESERVED",
      "created_at": "2026-09-07T08:00:00.000Z",
      "case_attachment_id": null
    },
    "upload": {
      "attachment_id": "30000000-0000-0000-0000-000000000001",
      "method": "PUT",
      "url": "http://xiaodao.internal:8000/api/v1/agent/attachments/30000000-0000-0000-0000-000000000001/content",
      "required_headers": {
        "Idempotency-Key": "30000000-0000-0000-0000-000000000001",
        "Content-Type": "application/zip",
        "X-Content-SHA256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      },
      "expected_content_length": 1024,
      "max_bytes": 2684354560,
      "expires_at": null
    }
  },
  "error": null
}
```

按 `upload` 描述符发送原始字节，不能使用 multipart、Base64 或 JSON 包装。HTTP 必须包含四个请求头：`Idempotency-Key`、`Content-Type`、`Content-Length`、`X-Content-SHA256`。`Idempotency-Key` 必须等于附件 UUID，不能用预约请求的 `request_id`。浏览器的 `Content-Length` 由 HTTP 实现生成；网站后端转发时保持真实长度。

```bash
curl --fail-with-body --request PUT "$UPLOAD_URL" \
  --header "Idempotency-Key: $ATTACHMENT_ID" \
  --header "Content-Type: application/zip" \
  --header "Content-Length: $FILE_SIZE" \
  --header "X-Content-SHA256: $FILE_SHA256" \
  --data-binary @logs.zip
```

上传成功响应的 `data` 是状态为 `READY` 的完整 `AgentAttachment`。预约目前没有过期时间，`expires_at` 固定为 `null`。`READY` 表示上传校验完成，**不表示诊断已经采用**。随后发送消息引用该附件：

```json
{
  "request_id": "message-002",
  "text": "这是付款服务和库存服务的日志，问题发生在 2026-09-07 15:20。",
  "attachment_ids": ["30000000-0000-0000-0000-000000000001"]
}
```

Case 建立后，服务端按原附件协议导入，`case_attachment_id` 标明稳定映射。重试不会重复导入；导入完成的会话附件状态是 `IMPORTED`。

### 字段对照

以下模型名与 OpenAPI 完全一致，便于生成网站后端类型。共享的 Case、产物、下载描述符和错误枚举见[浏览器 REST API 指南](browser-rest-api.md)。可空字段保持 `null`，网站不能补写没有来源的事实或报告。

| 模型 | 字段 | 含义与使用方式 |
| --- | --- | --- |
| `CreateConversationBody` | `request_id` | 创建请求的稳定幂等标识；按网站登录用户划分命名空间。 |
| `SendMessageBody` | `request_id` | 此条消息的稳定请求标识。 |
| `SendMessageBody` | `text` | 用户原话；可省略或为 `null`，不能与附件列表同时为空。 |
| `SendMessageBody` | `attachment_ids` | 当前会话内已经上传完成的附件 UUID 数组。 |
| `PrepareAgentAttachmentBody` | `request_id` | 本次预约的稳定请求标识。 |
| `PrepareAgentAttachmentBody` | `name` | 不含目录的压缩日志文件名。 |
| `PrepareAgentAttachmentBody` | `content_type` | 与文件压缩格式一致的媒体类型。 |
| `PrepareAgentAttachmentBody` | `declared_size` | 完整文件的字节数，不是分块大小。 |
| `PrepareAgentAttachmentBody` | `declared_sha256` | 完整原始文件的 SHA-256，小写 64 位十六进制。 |
| `ConversationReceipt` | `schema_version` | 会话回执合同版本，固定为 `1`。 |
| `ConversationReceipt` | `conversation_id` | 已持久创建的会话 UUID。 |
| `ConversationReceipt` | `request_id` | 创建请求的原始幂等标识。 |
| `MessageReceipt` | `conversation_id` | 接收消息的会话 UUID。 |
| `MessageReceipt` | `message_id` | 已持久接收的消息 UUID。 |
| `MessageReceipt` | `request_id` | 本次消息请求的原始幂等标识。 |
| `MessageReceipt` | `event_id` | 消息接收事件的序号；不是 Case revision。 |
| `MessageReceipt` | `status` | 固定为 `ACCEPTED`，不表示消息已经用于诊断。 |
| `AgentMessage` | `message_id` | 消息 UUID；刷新后用它关联原消息。 |
| `AgentMessage` | `request_id` | 用户提交消息时的幂等标识。 |
| `AgentMessage` | `text` | 持久保存的用户原话。 |
| `AgentMessage` | `attachment_ids` | 这条消息引用的会话附件 UUID 数组。 |
| `AgentMessage` | `status` | `QUEUED`、`PROCESSING`、`APPLIED` 或 `UNUSED`，表示消息采用状态。 |
| `AgentMessage` | `created_at` | 消息持久接收的时间。 |
| `AgentMessage` | `notice` | 可空的中文采用说明；例如尚未用于本次诊断。 |
| `AgentAttachment` | `attachment_id` | 会话附件 UUID，同时用于上传幂等请求头。 |
| `AgentAttachment` | `conversation_id` | 附件所属会话；网站仍需验证用户归属。 |
| `AgentAttachment` | `request_id` | 预约上传时的幂等标识。 |
| `AgentAttachment` | `name` | 预约并校验的文件名。 |
| `AgentAttachment` | `content_type` | 预约并校验的媒体类型。 |
| `AgentAttachment` | `size` | 预约声明的字节数；`READY` 后表示上传字节数也已核对。 |
| `AgentAttachment` | `sha256` | 预约声明的完整文件哈希；`READY` 后表示上传内容也已核对。 |
| `AgentAttachment` | `status` | `RESERVED`、`UPLOADING`、`READY`、`IMPORTED` 或 `FAILED`。 |
| `AgentAttachment` | `created_at` | 预约持久创建的时间。 |
| `AgentAttachment` | `case_attachment_id` | 导入既有 Case 后的附件 UUID；导入前为 `null`。 |
| `PreparedAgentAttachment` | `attachment` | 本次预约的完整 `AgentAttachment`。 |
| `PreparedAgentAttachment` | `upload` | `WebUploadDescriptor`，含上传 URL、方法、请求头和大小上限。 |
| `ConversationView` | `schema_version` | 会话快照合同版本，固定为 `1`。 |
| `ConversationView` | `conversation_id` | 会话 UUID。 |
| `ConversationView` | `status` | `INTAKE`、`WAITING_INPUT`、`RUNNING`、`COMPLETED`、`FAILED` 或 `INTERRUPTED`。 |
| `ConversationView` | `case_id` | 关联 Case UUID；整理问题阶段尚未创建时为 `null`。 |
| `ConversationView` | `job_id` | 当前关联 Job UUID；没有活动 Job 时可为 `null`。 |
| `ConversationView` | `case_status` | 关联 Case 的公开状态；尚未绑定时为 `null`。 |
| `ConversationView` | `archive_status` | `NOT_REQUIRED`、`PENDING`、`READY` 或 `FAILED`；JSON 报告不受 ZIP 失败影响。 |
| `ConversationView` | `current_questions` | 当前需要展示的中文追问数组。 |
| `ConversationView` | `messages` | 按接收顺序保存的 `AgentMessage` 列表。 |
| `ConversationView` | `attachments` | 当前会话附件列表，每项为 `AgentAttachment`。 |
| `ConversationView` | `last_event_id` | 当前最新事件序号；恢复页面时与本地已处理游标配合使用。 |
| `ConversationView` | `created_at` | 会话创建时间。 |
| `ConversationView` | `updated_at` | 最近一次会话持久更新的时间。 |
| `AgentEvent` | `schema_version` | 公共事件合同版本，固定为 `1`。 |
| `AgentEvent` | `sequence` | 会话内递增序号；用于去重和手动续传，不另发送 SSE `id` 行。 |
| `AgentEvent` | `conversation_id` | 事件所属会话 UUID。 |
| `AgentEvent` | `case_id` | 事件关联 Case UUID，创建前为 `null`。 |
| `AgentEvent` | `job_id` | 事件关联 Job UUID，没有关联时为 `null`。 |
| `AgentEvent` | `type` | 公共事件类型；按下一节事件表分发，不作为诊断结论解析。 |
| `AgentEvent` | `created_at` | 服务端持久记录事件的时间。 |
| `AgentEvent` | `data` | 与事件类型对应的公开载荷，字段规则见下一节。 |
| `AgentErrorEnvelope` | `ok` | 失败时固定为 `false`。 |
| `AgentErrorEnvelope` | `data` | 失败时固定为 `null`。 |
| `AgentErrorEnvelope` | `error` | `AgentHttpError`，含安全中文错误和重试提示。 |
| `AgentHttpError` | `code` | 稳定错误码；网站据此决定交互，不匹配中文文本。 |
| `AgentHttpError` | `message` | 可直接向用户展示的简体中文说明。 |
| `AgentHttpError` | `details` | 安全的字段错误详情，不含内部路径或原始日志。 |
| `AgentHttpError` | `retryable` | 服务端是否建议重试；重试仍需保持原请求 ID 和内容。 |

## 3. 实时事件、历史和状态显示

响应是 UTF-8 `text/event-stream`，每个业务事件只发送一行 `data: <AgentEvent JSON>`，后接一个空行（`\n\n`）。JSON 内的换行会转义，不能把网络分块当作事件边界。浏览器统一用 `onmessage` 接收，再按 JSON 的 `type` 分发；不需要注册命名事件。

```bash
curl --no-buffer --fail-with-body \
  --header 'Accept: text/event-stream' \
  --header 'Last-Event-ID: 12' \
  "$BASE/api/v1/agent/conversations/$CONVERSATION_ID/events"
```

省略 `Last-Event-ID` 或传 `0` 时回放全部历史；传 `12` 只回放序号大于 12 的事件，然后持续订阅。游标必须是非负规范十进制整数，不接受重复头、负数、小数、前导零或超出已有历史的值。

```text
: connected

data: {"schema_version":1,"sequence":13,"conversation_id":"10000000-0000-0000-0000-000000000001","case_id":null,"job_id":null,"type":"agent.progress","created_at":"2026-09-07T08:00:01.000Z","data":{"stage":"INTAKE","message":"正在整理问题"}}

data: {"schema_version":1,"sequence":14,"conversation_id":"10000000-0000-0000-0000-000000000001","case_id":null,"job_id":null,"type":"assistant.question","created_at":"2026-09-07T08:00:02.000Z","data":{"questions":["问题发生在哪个时间段？正常情况下预期是什么？"]}}

: heartbeat

```

所有业务事件都有同一外层字段，`sequence` 在会话内单调递增。不发送 `id:`、`event:`、`retry:` 行，也不发送 `[DONE]`；结束标志是 JSON 中的 `type=conversation.completed`。这是基础 SSE 传输，不是 OpenAI `choices` / `delta` 响应合同。

连接建立后立即发送 `: connected` 注释，服务端每 15 秒发送一次空闲 `: heartbeat` 注释。注释不触发 `onmessage`，不是业务事件，也不更新游标。网站代理需关闭响应缓冲、保持流式转发，并将读取超时设为大于心跳间隔，例如 60 秒。

| 事件类型 | `data` 内容与网站行为 |
| --- | --- |
| `message.accepted` | 完整消息记录；显示已接收，用 `message_id` 去重 |
| `message.updated` | `message_id`、`status`、`notice`；更新采用状态 |
| `assistant.question` | `questions` 数组；按原文显示追问 |
| `agent.progress` | `stage`、`message`；显示已经发生的阶段动作 |
| `case.updated` | `status`、`case_revision`；更新定位状态 |
| `result.available` | `status`、报告 `artifacts` 摘要、`result_field`；获取并校验正式报告 |
| `archive.updated` | `status`、归档 `artifacts` 摘要；更新 ZIP 或审计包下载入口 |
| `attachment.updated` | 完整会话附件状态；同步上传/导入状态 |
| `agent.failed` | `code`、`message`；展示执行失败，不构造定位结论 |
| `conversation.interrupted` | `code`、`message`；提示任务中断和重新发起 |
| `conversation.completed` | `status`；本会话事件流即将结束，可关闭订阅 |

公共进度不包含百分比、内部思考过程、工具参数、原始日志或路径。审核通过前，不会发出 Candidate 报告或根因结论。阶段事件不会增加 `case_revision`。

| 会话状态 | 建议文案 |
| --- | --- |
| `INTAKE` | 正在整理问题 |
| `WAITING_INPUT` | 请补充信息或上传日志 |
| `RUNNING` | 定位进行中，显示最新阶段消息 |
| `COMPLETED` | 本次定位已结束，请查看报告 |
| `FAILED` | 本次定位未能完成，请重新发起任务 |
| `INTERRUPTED` | 本次任务已中断，请重新发起 |

消息采用状态：`QUEUED` 表示已排队、`PROCESSING` 表示正在整理、`APPLIED` 表示已经采用、`UNUSED` 表示未用于本次诊断。请优先显示服务端的 `notice`，不要把排队消息显示为“已用于诊断”。

`GET conversation` 的 `data` 含 `schema_version`、`conversation_id`、`status`、`case_id`、`job_id`、`case_status`、`archive_status`、`current_questions`、`messages`、`attachments`、`last_event_id`、`created_at`、`updated_at`。快照和 SSE 可能重叠；按消息 ID 更新消息，按事件序号去重。

网页可用 `EventSource` 订阅网站自己的同源 SSE 路由。由于响应没有 `id:` 行，它不会记录业务游标；短暂断线自动重连或刷新后的新连接都会从历史开始回放，显示层必须按 JSON 的 `sequence` 去重。需要精准续传时，使用流式 `fetch`，手动把最后处理成功的序号放入 `Last-Event-ID` 请求头。

```javascript
const events = new EventSource(`/api/agent/conversations/${conversationId}/events`);
let lastSequence = 0;
let queue = Promise.resolve();
let pending = 0;
let stopped = false;
const closeAgentEvents = () => { stopped = true; events.close(); };
const fail = (error) => {
  if (stopped) return;
  closeAgentEvents();
  showEventRetry(error); // 网站组件：保留页面；重试时重新创建订阅，从历史回放。
};
const types = ["message.accepted", "message.updated", "assistant.question", "agent.progress",
  "case.updated", "result.available", "archive.updated", "attachment.updated",
  "agent.failed", "conversation.interrupted", "conversation.completed"];
events.onmessage = (message) => {
  if (stopped) return;
  if (pending >= 128) { fail(new Error("页面处理较慢，请重新连接并回放历史。")); return; }
  pending += 1;
  queue = queue.then(async () => {
    if (stopped) return;
    const event = JSON.parse(message.data);
    if (event.schema_version !== 1 || event.conversation_id !== conversationId ||
        !types.includes(event.type) || !Number.isSafeInteger(event.sequence) || event.sequence < 1)
      throw new Error("事件格式不符合约定。");
    if (event.sequence <= lastSequence) return;
    await renderEventAsText(event); // 网站组件：按 ID 更新，文本不得作为 HTML 执行。
    if (event.type === "result.available") {
      const response = await fetch(`/api/agent/conversations/${conversationId}/report`);
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error("报告暂未加载成功，请重试。");
      if (stopped) return;
      await renderVerifiedReport(result.data);
    }
    if (stopped) return;
    lastSequence = event.sequence; // 所有业务处理成功后才能推进游标。
    if (event.type === "conversation.completed") closeAgentEvents();
  }).catch(fail).finally(() => { pending -= 1; });
};
```

上面的 `renderEventAsText`、`renderVerifiedReport`、`showEventRetry` 由网站组件实现；渲染须幂等，失败要抛错。组件销毁时调用 `closeAgentEvents()`，只断开订阅，不取消诊断。服务器和示例均限制待处理事件数量，慢连接不会无限积压。

临时网络断线可由 `EventSource` 自动重连，但这里不会自动携带业务 `Last-Event-ID`，重连会回放历史，本例按 `lastSequence` 跳过已经处理成功的事件。报告加载、解析或显示失败时，本例关闭连接并显示重试入口，不处理排队中的完成事件；点击重试需重新执行订阅初始化，从历史回放，页面按 ID 更新已有内容。需要持久精准续传时，用流式 `fetch` 携带最后处理成功的 `Last-Event-ID`；按空行拆帧并忽略以冒号开头的注释，不按读取到的网络块直接 `JSON.parse`。刷新恢复还应根据会话快照重新获取已经发布的报告，不能只恢复进度文字。

## 4. 正式报告与下载校验

`result.available` 只是产物已正式发布的通知，不是报告全文。按会话 `case_id` 获取现有 Case 和产物列表，并核对产物 ID、种类、来源 Job、大小和 SHA-256。`ArtifactView` 提供下载 URL；来源 Job 位于 Case 的 `artifacts[].created_by_job_id`，需与 `final_result.proposed_by_job_id`、`unresolved_result.source_job_id` 或 Generic 结果来源一致。

专有报告是唯一的 `USER_RESULT` / `diagnosis-result.json`。下载必须是 HTTP 200，不允许重定向；验证 `Content-Length`、`X-Content-SHA256`、真实字节数、SHA-256 和 Content-Type 后再展示。报告要求 `schema_version=3`、`format_id=problem-locator-diagnosis-v3`，保留完整字段，不从 `methods_result`、SSE 阶段消息或 stdout 重建结论。

| Case 状态 | JSON 报告状态和可用产物 |
| --- | --- |
| `RESOLVED` | `COMPLETED` JSON；ZIP 可处于 `PENDING` |
| `PARTIALLY_RESOLVED` | `PARTIAL` JSON；明确展示限制和证据缺口 |
| `UNRESOLVED` | `INCONCLUSIVE` JSON 和审计包；`root_cause=null`，没有结果 ZIP |
| `FAILED` / `CANCELLED` / `INTERRUPTED` | 展示 failure 或状态，不伪造报告 |

展示顺序：定位结论、问题描述、关键发现、确认/候选/排除因素、完成条件、服务端验证及证据、时间相关性、证据缺口、限制、处置建议与安全说明。缺失必需字段属于协议错误；`null` 或空数组按其真实含义显示，不自动补写根因。通用诊断沿用 Generic 合同；Markdown 也应先校验字节数和 SHA-256，再用关闭原始 HTML 的渲染器展示。

`archive_status=PENDING`：JSON 立即可展示，继续等 `archive.updated`。`READY`：按用户请求下载 `USER_RESULT_ARCHIVE` / `result.zip`。`FAILED`：归档失败，但已经交付的报告仍有效。`NOT_REQUIRED`：没有待生成的结果 ZIP，审计包是否可下载以产物列表为准。

下载 ZIP 前显示：“该文件包含原始目标日志，可能含有业务信息。请确认后下载。”审计包也仅按用户请求下载。网站代理应把文件下载到唯一临时文件，完整校验后再发给浏览器，成功和失败都清理临时文件。不要把全部日志 ZIP 缓存在内存中。

## 5. 可运行的 TypeScript 网站后端

仓库的 `examples/website-agent/server.ts` 使用 Node.js 24 内置 TypeScript 支持和标准库，无需安装 npm 包。它实现登录/归属回调、同源会话接口、SSE 转发、上传转发、JSON 中文展示结构，以及经校验的报告下载。网站可直接集成其中逻辑，或把它作为后端服务接入现有反向代理。

```powershell
$env:XIAODAO_BASE_URL = 'http://xiaodao.internal:8000'
$env:WEBSITE_AUTH_MODULE = 'D:\website\xiaodao-access.mjs'
node examples/website-agent/server.ts
```

Linux 启动命令、五个授权回调及反向代理要求见 [示例 README](../examples/website-agent/README.md)。示例固定监听 `127.0.0.1`，不是可以直接暴露给所有浏览器的完整网站。

未配置 `WEBSITE_AUTH_MODULE` 时，服务仍可启动，但业务请求全部返回 `401`。不要为了联调删掉授权检查。认证模块导出 `access`，按 `server.ts` 中的 `Access` 类型实现五个异步回调：验证网站登录态、查询会话归属、持久保存会话归属、查询附件归属、持久保存附件归属。Cookie 认证还需接入网站既有 CSRF 和 Origin 校验；不得相信客户端自行传入的用户名或 user ID。

示例网站路径使用 `/api/agent`，不是 xiaodao 上游的 `/api/v1/agent`。额外提供：

| 网站示例路径 | 用途 |
| --- | --- |
| `GET /api/agent/conversations/{id}/report` | 自动下载唯一 JSON 或 Generic Markdown，校验后返回报告和中文展示结构 |
| `GET /api/agent/conversations/{id}/artifacts` | 授权后返回产物，下载 URL 改为网站同源路径 |
| `GET /api/agent/conversations/{id}/artifacts/{artifact_id}/content` | 授权并校验后下载报告 |
| 上一下载路径加 `?download=archive&acknowledge_raw_logs=true` | 用户确认原始日志提示后下载结果 ZIP |
| 上一下载路径加 `?download=audit` | 用户主动下载审计包 |

示例不信任上游返回的任意下载地址：先核对配置的服务地址、Case ID 和 Artifact ID，再使用已验证的描述符下载。网站可以使用不同网络地址访问 xiaodao，但需将 xiaodao 的 `PUBLIC_BASE_URL` 配成与网站后端的 `XIAODAO_BASE_URL` 一致的地址和路径前缀。

运行接入示例确定性测试：

```bash
node --test examples/website-agent/server.test.mjs
```

测试覆盖未登录、无权会话、无权上传、无权下载、跨用户幂等命名空间、SSE 游标与心跳、报告字段、来源 Job/URL/hash 不匹配、ZIP 主动下载确认。实际部署还应检查网站反向代理没有缓冲 SSE，且只允许网站后端连接 xiaodao。

## 6. 服务配置与升级

本次调整的是现有 `8.0.0` 预览版的 SSE 传输格式，持久会话与事件仍为 `schema_version=1`，V11 数据合同不变。已按旧版 `event:` 注册命名监听器的网站须改用 `onmessage`，从 JSON 读取 `type` 和 `sequence`，不再依赖 `lastEventId` 或服务端 `retry:`。更新部署时核对实际响应字节和对应源码版本，不能只看 `info.version=8.0.0`。此传输调整本身不要求重建已经使用的 V11 数据根。

`INTAKE_CLAUDE_COMMAND` 配置独立问题整理角色；缺省沿用路由角色命令。该角色只整理用户消息和公开 requirements，不获得诊断工具、日志读取或发布结果权限。`SPECIALIZED_REVIEWER_ENABLED` 延续现有配置：关闭时通过服务端验证的 Candidate 可直接交付；开启时只在 Review PASS 后公开正式报告。

V11 使用全新空 `DATA_ROOT`。不要把旧版本目录直接切给新版本，也不要删除旧数据。旧报告可由原版本只读查看或事先导出；不存在自动迁移或从旧 `methods_result` 反推报告的步骤。MCP 仍为原来的七个工具，输入继续根层扁平；网站接入只增加 REST Agent 接口。
