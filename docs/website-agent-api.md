# 网站 API：会话与附件

诊断记录、报告和附件默认保留 **7 天**，每轮独立到期。页面应提示用户及时下载；到期轮次不再可查。创建请求幂等记录和已删除会话的最小凭据也有 7 天保留期。SSE 返回 `409 / AGENT_EVENT_CURSOR_EXPIRED` 时，先读取会话快照，再用其 `last_event_id` 作为 `Last-Event-ID` 重新订阅。完整策略见[服务端文件保留与清理](storage-retention.md)。

已有网站升级请先读 [8.2 前端必改清单](website-agent-upgrade-8.2.md)，再按本文核对完整接口说明。

适用于 xiaodao `8.2.0`、V11 / `v11-contract-r2`。网站把用户原话和日志交给 xiaodao，展示追问、执行进度和经过服务端验证的定位报告。一个会话包含多轮独立诊断，历史消息、追问和结果按轮次保留。网站只需接入会话和附件两类接口：页面所需的信息从会话接口读取，文件单独流式传输。会话查询响应的版本为 `schema_version=3`；SSE 为 schema_version=2，报告内部数据格式仍为 schema 3。

首次接入先读 [部署后快速接入与联调清单](website-agent-quickstart.md)。本文是完整接口参考。在线入口是 xiaodao 服务的 `/docs` 和 `/openapi.json`；仓库保存 [完整 OpenAPI 快照](../schemas/v2/web-api.openapi.snapshot.json)。联调前核对线上 `info.version` 和路由，不能假设已部署服务与当前源码一致。

调用关系：网站前端 → 网站后端 → Linux 上的 xiaodao REST API。网站原有问答功能保持独立。网站后端负责登录校验；xiaodao 保存会话、附件、订阅和文件的所属用户信息，并在访问时统一检查。UUID 不是授权凭据。不要把 xiaodao 内部地址或下载地址直接交给浏览器。

## 1. 网站开发者需要实现的流程

1. 网站后端根据登录身份生成用户标识，再创建会话；xiaodao 保存会话与用户的对应关系，网站不再单独维护一份会话目录。
2. 用户发送非空的问题原话，服务端立即按 MCP 客户端的固定中性模板创建 Case；创建前不追问预期行为、范围、日志或时间，也不调用 INTAKE。网站不需要构造 `problem_spec` 或命名事实。日志可先上传，再随问题消息发送附件 ID。
3. 订阅 SSE。选中 Skill 后，服务端先从已有消息（包括首条原文）提取所需参数，并提交通过校验的部分；此时显示 `INTAKE` 的整理进度。采用结果后，`assistant.question` 只列出仍未满足的要求，用户继续调用消息接口回答。没有剩余要求时不额外追问。
4. 初次打开或刷新调用 `GET /api/v1/agent/conversations/{conversation_id}`，一次获取状态、历史、报告和下载信息。收到 `result.available` 且页面尚无报告时，用同一路径加 `?include=report` 读取 `data.result`。采用网站示例时前缀是 `/api/agent`。不要等待 ZIP 才展示报告。
5. `archive.updated` 的状态变为 `READY` 后提供 ZIP 下载按钮。用户点击并确认包含原始目标日志后才下载。
6. 只有当前轮的 `conversation.completed` 才关闭本轮订阅；旧轮完成事件不能关闭新轮订阅。本轮结束后可在同一会话发送新问题，服务端创建新轮；重新建立订阅以接收新轮事件。

关闭页面或断开事件流不会停止后台任务。刷新页面后可查询会话快照并回放历史。服务重启后，未完成任务会明确标为 `INTERRUPTED`；用户可在原会话发送新问题开始下一轮。任务失败或中断后，当前追问会清空，页面按 `failure` 展示结束原因，历史问题仍可回看。已完成报告保留，待生成 ZIP 继续按既有归档机制恢复。

全部用户原话保存在会话历史中。关联 Case 的 `raw_problem_text` 保留创建任务的完整用户原话，`statement` 和 `actual_behavior` 使用同一文本。其余问题字段使用 MCP 客户端的固定中性默认值，初始事实为空；`expected_behavior` 不要求用户单独填写。后续消息按当前 OPEN requirements 补充，不拼接进 `raw_problem_text`；网站展示聊天历史时读取会话消息。只有附件、没有问题文本时，服务端先提示用户提供问题原话。

消息已经用于创建 Case（`APPLIED`）不表示其中的 Skill 参数已提取。首次参数提取在路由确定所需信息后执行；若此时已有新消息排队，会连同首条原文一起处理，避免重复提取。每条消息最多触发一次 Intake，GET、SSE、轮询和已接收命令的重放不增加模型调用。单条消息中有效的参数会立即采用；同一次诊断的后续补充会携带已确定的事实和仍相关的原文草稿。重复提供相同值不会再次提交；更正已确定的值需结束当前轮后重新诊断，新诊断不自动继承旧事实。

先发描述、再单独发送附件也支持上述流程：服务端会检查此前是否还有未提取的文字，不会因为最新消息没有 `text` 就跳过首条描述。文字已经整理后，仅选择或补交附件不再调用提取模型。网站可以先把一个日志包上传到 `READY`，再用同一条消息发送问题文字和 `attachment_ids`。

当前诊断要求一份日志归档。尚未采用附件时，以最近一条携带 `attachment_ids` 的消息作为附件选择；只发文字不会清除已有选择。一次选了多个包时，会话继续等待选择或合包，有效文字参数仍会采用。重新发送一条只引用目标附件 ID 的消息即可选择已有文件，无需重复上传。已被 Case 采用的日志不受后续选择影响。

模型只负责提取参数，是否采用参数由服务端决定。即使返回 `NEED_CLARIFICATION`，服务端也会提交有效的新事实和附件，再根据最新 Case 展示仍需补充的信息。引用可以包含值两侧的原文上下文。完全重复的项会合并；未知字段、无原文依据或格式不符的项会被过滤，其余有效项不受影响。明确包含日期和时区的 `problem_time` 可按固定规则转换为毫秒 UTC；服务端不会猜测时区或改写标识符。缺少的信息继续追问，更正已确定的值需结束当前轮后重新诊断。整理输入和提交参数尚未完成时，会话快照不显示过时追问；同时收到的新消息保持待处理状态。

时间须包含完整日期、时分秒及 `Z` 或明确时区偏移，日期与时间之间支持 `T` 或一个普通空格，例如 `2026-09-16 10:00:00+08:00`。不接受缺少时区的时间，也不会舍弃亚毫秒精度。Skill 已登记的角色说明会随对应字段要求传给提取模型，每段说明最多使用 256 个 UTF-8 字节；没有登记的字段别名和含义不会自动补造。

参数提取读取用户消息和附件元数据，不读取日志正文。问题时间、进程、槽位等关键参数应写在问题文字中；仅存在于日志文件内的信息目前不会自动补齐这些输入。

性能边界：空闲会话只读取轻量处理状态和带索引的未完成命令，不重载消息历史或查询 Case。首条消息会补上旧流程漏掉的一次必要提取；没有可提取参数的首条也可能耗用这一次调用，因此零模型验证不能保证真实模型的端到端时延不变。模型失败不自动重试。

## 2. 接口和通用约定

以下路径是 **xiaodao 服务接口**。部署示例的 `$BASE` 是网站后端可访问的 xiaodao 地址，例如 `http://xiaodao.internal:8000`。本文 UUID 仅用于说明，实际调用需使用创建响应中的值。

| 抽象 | 方法与路径 | 输入或用途 |
| --- | --- | --- |
| 会话 | `POST /api/v1/agent/conversations` | 创建会话，JSON：`request_id` |
| 会话 | `GET /api/v1/agent/conversations` | 当前用户的目录，`limit` 默认 20、最大 100；`cursor` 原样传回 |
| 会话 | `PATCH /api/v1/agent/conversations/{conversation_id}` | 修改标题，JSON：`title`，1 到 80 个字符 |
| 会话 | `POST /api/v1/agent/conversations/{conversation_id}/stop` | 停止指定轮次，JSON：稳定 `request_id` 和 `run_id` |
| 会话 | `DELETE /api/v1/agent/conversations/{conversation_id}` | 删除会话，无请求体；按会话 ID 幂等 |
| 会话 | `POST /api/v1/agent/conversations/{conversation_id}/messages` | 发送问题、补充回答或附件引用，JSON：`request_id`、可空 `text`、`attachment_ids` |
| 会话 | `GET /api/v1/agent/conversations/{conversation_id}` | 一次返回状态、进度、追问、失败、历史、完整报告和下载信息 |
| 会话 | `GET /api/v1/agent/conversations/{conversation_id}/events` | SSE 历史回放和实时订阅；可带 `Last-Event-ID` |
| 会话 | `GET /api/v1/agent/conversations/{conversation_id}/files/{artifact_id}/content` | 下载文件，可用 `run_id` 固定历史轮次 |
| 附件 | `POST /api/v1/agent/attachments` | 预约日志上传，JSON 含 `conversation_id` 和文件元数据 |
| 附件 | `PUT /api/v1/agent/attachments/{attachment_id}/content` | 上传文件原始字节 |

会话 GET 支持 `include`、`run_id`、`history_before`、`history_limit`。不传时加载 `history,report,artifacts`；`include=none` 只返回状态、追问、最新阶段和事件游标；也可传 `include=report`、`include=artifacts` 或不重复的逗号组合。目录查询支持 `cursor`、`limit`；文件支持 `run_id`；其余路由不接受查询参数。未知项、重复项和重复 `include` 参数返回明确校验错误。

返回字段固定，`included` 明确本次加载的部分。未加载的 `history`、`attachments`、`result`、`artifacts` 为 null；空数组表示已加载且没有记录。前端只更新本次加载的部分，不能用 null 清空此前的报告。`report_state` 表示业务可用性，`READY` 与 `result=null` 可以同时出现，含义是本次没有请求报告。

默认完整查询适合首次打开和刷新；持续更新用 SSE 或 `include=none`。收到报告或归档就绪事件时再加载相应部分。正常轻量查询不读消息正文、附件历史、Case 快照或报告文件；交付异常时仍需读取服务端保存的状态，确认是否已经完成。最新阶段从索引读取，首次启动时补建一次；索引可以重建，不改历史事件字节或序号。

旧 `/status`、`/report`、网站产物列表和会话下预约附件路径已删除，不提供兼容别名。底层 Case API 仍供已有 Case 集成使用，网站无需查询。文件下载入口随 `artifacts[].download_url` 返回，点击后才传输二进制文件。

路径标识和 `attachment_ids` 必须是小写规范 UUID。JSON 未定义字段、字符串化数组、重复附件 ID 会被拒绝。消息至少包含非空文本或一个已上传附件；`text` 可省略或为 `null`。文本和 `request_id` 分别最多 65,536 UTF-8 字节，一条消息最多 20 个附件。

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

常见 HTTP 状态：`400` 参数错误；`404` 不存在；`409` 状态或幂等冲突；`413` 超出限制；`422` 文件校验失败；`503` 服务未配置或暂时不可用。网站自身还应返回 `401` 未登录和 `403` 无权访问。Agent 错误码只用于 Agent 接口；现有 Case 接口的错误格式和处理规则保持不变。

### 创建与发送消息

每个发往 xiaodao 的 Agent 请求都必须带 `X-Agent-Owner-Key`。网站后端计算 `SHA-256(JSON.stringify([固定网站命名空间, 已认证用户 ID]))`，得到 64 位小写十六进制用户标识。命名空间与用户 ID 必须长期稳定。这个请求头不是登录凭据，xiaodao 只能开放给可信网站后端；其值不能由浏览器指定。Core Case 接口在访问 Agent 创建的数据时，也会核对数据是否属于该用户。

`current_run` 始终是当前轮；`selected_run_id` 指明这次读取的状态、报告和文件属于哪一轮。省略 `run_id` 选当前轮，传历史轮 ID 即可读取旧报告，不重新诊断。`capabilities` 给出发送、停止、重新诊断、重命名和删除按钮是否可用，前端不必自行猜测状态组合。

`history` 是唯一聊天展示序列，仅有 `user.message`、`assistant.question`、`diagnosis.result`；结果记录只含摘要，点击后按其 `run_id` 读取正文。默认取最近 50 条，最多 100 条；将 `history_next_cursor` 原样传入 `history_before` 加载更早记录。每页按时间正序返回，前端在顶部插入并按 `id` 去重。历史跨越多轮，分页不复制报告正文；不再返回 `messages`。

停止请求的 `request_id` 和 `run_id` 必须一起保存。`CANCELLING` 表示已接收但后台尚未退出，`CANCELLED` 表示已停止，`ALREADY_FINISHED` 表示目标轮已结束。旧停止请求不能误停新轮。关闭 SSE 不等于停止。新一轮仍调用消息接口，明确发送完整问题及需要复用的附件 ID，不自动继承旧文本、事实或结论。

DELETE 无请求体，返回 `DELETING` 或 `DELETED`。接收删除后目录立即隐藏会话，新查询、发送、上传、事件和下载返回 404；正在使用的资源释放后再清理字节。网络中断时重发同一 DELETE 可确认状态。

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

`ACCEPTED` 只表示消息已接收并保存。后台收到首条非空问题文本就创建 Case；创建本身不调用 INTAKE，随后路由和诊断仍可能调用模型。INTAKE 只在 Case 创建后允许补充信息时整理用户输入，按 OPEN INPUT requirements 提取有原文依据的事实；展示给用户的追问始终使用 requirements 的原始 prompt。定位运行期间的新消息显示“已收到，尚未用于本次诊断”。更正已确定的事实或任务目标时，应停止当前轮，再发送完整的新问题。

### 预约和上传日志

文件名只接受现有压缩日志格式：`.zip`、`.tar`、`.tar.gz`、`.tgz`、`.gz`。文件名不得包含目录或控制字符，压缩后缀使用小写。类型分别为 `application/zip`、`application/x-tar`、`application/gzip`。单文件最多 2,684,354,560 字节，每会话最多 20 个附件且总量受服务端限制；不支持截图或 PDF 解析。

先计算文件真实字节数和完整 SHA-256，再预约。以下哈希值仅演示字段格式，不能直接用于上传真实文件。

```http
POST /api/v1/agent/attachments
Content-Type: application/json

{
  "conversation_id": "10000000-0000-0000-0000-000000000001",
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
| `PrepareAgentAttachmentBody` | `conversation_id` | 已创建且归当前用户所有的会话 UUID。 |
| `PrepareAgentAttachmentBody` | `request_id` | 本次预约的稳定请求标识。 |
| `PrepareAgentAttachmentBody` | `name` | 不含目录的压缩日志文件名。 |
| `PrepareAgentAttachmentBody` | `content_type` | 与文件压缩格式一致的媒体类型。 |
| `PrepareAgentAttachmentBody` | `declared_size` | 完整文件的字节数，不是分块大小。 |
| `PrepareAgentAttachmentBody` | `declared_sha256` | 完整原始文件的 SHA-256，小写 64 位十六进制。 |
| `ConversationReceipt` | `schema_version` | 会话回执的数据格式版本，固定为 `1`。 |
| `ConversationReceipt` | `conversation_id` | 已创建并保存的会话 UUID。 |
| `ConversationReceipt` | `request_id` | 创建请求的原始幂等标识。 |
| `MessageReceipt` | `conversation_id` | 接收消息的会话 UUID。 |
| `MessageReceipt` | `message_id` | 已接收并保存的消息 UUID。 |
| `MessageReceipt` | `request_id` | 本次消息请求的原始幂等标识。 |
| `MessageReceipt` | `event_id` | 消息接收事件的序号；不是 Case revision。 |
| `MessageReceipt` | `status` | 固定为 `ACCEPTED`，不表示消息已经用于诊断。 |
| `AgentMessage` | `message_id` | 消息 UUID；刷新后用它关联原消息。 |
| `AgentMessage` | `request_id` | 用户提交消息时的幂等标识。 |
| `AgentMessage` | `text` | 持久保存的用户原话。 |
| `AgentMessage` | `attachment_ids` | 这条消息引用的会话附件 UUID 数组。 |
| `AgentMessage` | `status` | `QUEUED`、`PROCESSING`、`APPLIED` 或 `UNUSED`，表示消息采用状态。 |
| `AgentMessage` | `created_at` | 接收并保存消息的时间。 |
| `AgentMessage` | `notice` | 可空的中文采用说明；例如尚未用于本次诊断。 |
| `AgentAttachment` | `attachment_id` | 会话附件 UUID，同时用于上传幂等请求头。 |
| `AgentAttachment` | `conversation_id` | 附件所属会话；网站仍需检查该会话是否属于当前用户。 |
| `AgentAttachment` | `request_id` | 预约上传时的幂等标识。 |
| `AgentAttachment` | `name` | 预约并校验的文件名。 |
| `AgentAttachment` | `content_type` | 预约并校验的媒体类型。 |
| `AgentAttachment` | `size` | 预约声明的字节数；`READY` 后表示上传字节数也已核对。 |
| `AgentAttachment` | `sha256` | 预约声明的完整文件哈希；`READY` 后表示上传内容也已核对。 |
| `AgentAttachment` | `status` | `RESERVED`、`UPLOADING`、`READY`、`IMPORTED` 或 `FAILED`。 |
| `AgentAttachment` | `created_at` | 创建并保存上传预约的时间。 |
| `AgentAttachment` | `case_attachment_id` | 导入既有 Case 后的附件 UUID；导入前为 `null`。 |
| `PreparedAgentAttachment` | `attachment` | 本次预约的完整 `AgentAttachment`。 |
| `PreparedAgentAttachment` | `upload` | `WebUploadDescriptor`，含上传 URL、方法、请求头和大小上限。 |
| `ConversationDetailResponse` | `schema_version` | 会话查询响应的数据格式版本，固定为 `3`。 |
| `ConversationDetailResponse` | `conversation_id` | 会话 UUID。 |
| `ConversationDetailResponse` | `status` | `INTAKE`、`WAITING_INPUT`、`RUNNING`、`CANCELLING`、`CANCELLED`、`COMPLETED`、`FAILED` 或 `INTERRUPTED`。 |
| `ConversationDetailResponse` | `case_id` | 关联 Case UUID；整理问题阶段尚未创建时为 `null`。 |
| `ConversationDetailResponse` | `job_id` | 当前关联 Job UUID；没有活动 Job 时可为 `null`。 |
| `ConversationDetailResponse` | `case_status` | 关联 Case 的公开状态；尚未绑定时为 `null`。 |
| `ConversationDetailResponse` | `archive_status` | `NOT_REQUIRED`、`PENDING`、`READY` 或 `FAILED`；JSON 报告不受 ZIP 失败影响。 |
| `ConversationDetailResponse` | `current_questions` | 当前需要展示的中文追问数组。 |
| `ConversationDetailResponse` | `failure` | 可空的安全失败信息；终态错误持久保存，当前进程的归档交付未知只作临时投影。旧历史可为 `null`。 |
| `ConversationDetailResponse` | `history` | 按时间排序的跨轮展示序列；仅含用户消息、追问和结果摘要；未请求 history 时为 null。 |
| `ConversationDetailResponse` | `attachments` | 当前会话附件列表，每项为 `AgentAttachment`；未请求 history 时为 null。 |
| `ConversationDetailResponse` | `last_event_id` | 当前最新事件序号；恢复页面时与本地已处理游标配合使用。 |
| `ConversationDetailResponse` | `created_at` | 会话创建时间。 |
| `ConversationDetailResponse` | `updated_at` | 最近一次会话持久更新的时间。 |
| `ConversationDetailResponse` | `case_revision` | 加载报告或产物时读取的服务端 Case 版本；未读取时为 null。 |
| `ConversationDetailResponse` | `source_job_id` | 报告及产物的来源 Job；无结果或未加载时为 null。 |
| `ConversationDetailResponse` | `progress` | 最新公开阶段和固定中文说明；尚无阶段事件时为 null。 |
| `ConversationDetailResponse` | `report_state` | PENDING 等待诊断或补充，READY 已发布，UNAVAILABLE 已结束且无报告。 |
| `ConversationDetailResponse` | `included` | 实际加载的 history、report、artifacts；轻量查询为空数组。 |
| `ConversationDetailResponse` | `result` | 请求 report 时返回完整 ConversationReportView；未请求时为 null。 |
| `ConversationDetailResponse` | `artifacts` | 请求 artifacts 时返回下载元数据和入口；未请求时为 null。 |
| `AgentEvent` | `schema_version` | 公开事件的数据格式版本，固定为 `2`；每条事件含 `run_id`。旧事件字节不改，读取时转换为 v2 格式。 |
| `AgentEvent` | `sequence` | 会话内递增序号；用于去重和手动续传，不另发送 SSE `id` 行。 |
| `AgentEvent` | `conversation_id` | 事件所属会话 UUID。 |
| `AgentEvent` | `case_id` | 事件关联 Case UUID，创建前为 `null`。 |
| `AgentEvent` | `job_id` | 事件关联 Job UUID，没有关联时为 `null`。 |
| `AgentEvent` | `type` | 公共事件类型；按下一节事件表分发，不作为诊断结论解析。 |
| `AgentEvent` | `created_at` | 服务端持久记录事件的时间。 |
| `AgentEvent` | `data` | 与事件类型对应的公开数据，字段规则见下一节。 |
| `AgentErrorEnvelope` | `ok` | 失败时固定为 `false`。 |
| `AgentErrorEnvelope` | `data` | 失败时固定为 `null`。 |
| `AgentErrorEnvelope` | `error` | `AgentHttpError`，含安全中文错误和重试提示。 |
| `AgentHttpError` | `code` | 稳定错误码；网站据此决定交互，不匹配中文文本。 |
| `AgentHttpError` | `message` | 可直接向用户展示的简体中文说明。 |
| `AgentHttpError` | `details` | 安全的字段错误详情，不含内部路径或原始日志。 |
| `AgentHttpError` | `retryable` | 服务端是否建议重试；重试仍需保持原请求 ID 和内容。 |
| `AgentPublicFailure` | `code` | 安全错误码，用于识别失败种类。 |
| `AgentPublicFailure` | `message` | 可直接展示的中文说明，不含模型原文、堆栈或内部路径。 |
| `AgentPublicFailure` | `details` | `field`/`actual` 形式的安全详情，例如 `phase`、`diagnostic_id`、`reason_code`、`location`；运行态故障还可含 `persistence=UNKNOWN`。 |
| `AgentPublicFailure` | `retryable` | 固定为 `false`；查询或读取报告不会重新运行模型，也不能据此续办已结束的任务。 |

### 会话管理和历史字段

| 模型 | 字段 | 含义与使用方式 |
| --- | --- | --- |
| `RenameConversationBody` | `title` | 新的会话标题，1 到 80 个字符。 |
| `StopConversationBody` | `request_id` | 稳定停止幂等键。 |
| `StopConversationBody` | `run_id` | 本次停止的目标轮次，重试不得改写。 |
| `ConversationRun` | `run_id` | 独立轮次 UUID。 |
| `ConversationRun` | `ordinal` | 会话内从 1 开始的轮次序号。 |
| `ConversationRun` | `status` | 当前轮次状态，包含停止中和已停止。 |
| `ConversationRun` | `case_id` | 该轮关联的 Case UUID，可空。 |
| `ConversationRun` | `job_id` | 该轮 Job UUID，可空。 |
| `ConversationRun` | `case_status` | 该轮 Case 状态，可空。 |
| `ConversationRun` | `archive_status` | 该轮归档状态。 |
| `ConversationRun` | `report_state` | 该轮报告是否可用。 |
| `ConversationRun` | `created_at` | 本轮开始时间。 |
| `ConversationRun` | `updated_at` | 本轮最近更新时间。 |
| `ConversationCapabilities` | `can_send` | 是否可发送消息。 |
| `ConversationCapabilities` | `can_stop` | 是否可停止当前轮。 |
| `ConversationCapabilities` | `can_rediagnose` | 是否可开始新轮。 |
| `ConversationCapabilities` | `can_rename` | 是否可重命名。 |
| `ConversationCapabilities` | `can_delete` | 是否可删除会话。 |
| `ConversationSummary` | `conversation_id` | 会话 UUID。 |
| `ConversationSummary` | `title` | 会话标题。 |
| `ConversationSummary` | `current_run` | 当前轮的轻量摘要。 |
| `ConversationSummary` | `capabilities` | 当前可用操作。 |
| `ConversationSummary` | `created_at` | 会话创建时间。 |
| `ConversationSummary` | `updated_at` | 会话更新时间。 |
| `ConversationList` | `items` | 当前页摘要数组。 |
| `ConversationList` | `next_cursor` | 下一页游标；结束时为 null。 |
| `ConversationHistoryEntry` | `id` | 稳定记录 ID，页面按此去重。 |
| `ConversationHistoryEntry` | `run_id` | 所属轮次 UUID。 |
| `ConversationHistoryEntry` | `type` | user.message、assistant.question 或 diagnosis.result。 |
| `ConversationHistoryEntry` | `created_at` | 记录时间。 |
| `ConversationHistoryEntry` | `message` | 用户消息载荷，其他类型为 null。 |
| `ConversationHistoryEntry` | `questions` | 追问列表，其他类型为 null。 |
| `ConversationHistoryEntry` | `result` | 结果摘要，其他类型为 null。 |
| `ConversationResultSummary` | `status` | COMPLETED、FAILED、INTERRUPTED 或 CANCELLED；无报告的结束轮也保留卡片。 |
| `ConversationResultSummary` | `case_id` | 该轮关联的 Case；创建 Case 前已结束时为 null。 |
| `ConversationResultSummary` | `case_status` | 该轮 Case 状态，可空。 |
| `ConversationResultSummary` | `report_state` | READY 表示正式报告可读，UNAVAILABLE 表示本轮结束但没有报告。 |
| `ConversationResultSummary` | `source_job_id` | 报告来源 Job，可空。 |
| `ConversationResultSummary` | `failure` | 受控失败信息，可空；取消不伪造失败或报告。 |
| `StopReceipt` | `conversation_id` | 会话 UUID。 |
| `StopReceipt` | `run_id` | 停止目标轮次。 |
| `StopReceipt` | `request_id` | 停止幂等键。 |
| `StopReceipt` | `status` | CANCELLING、CANCELLED 或 ALREADY_FINISHED。 |
| `DeleteReceipt` | `conversation_id` | 会话 UUID。 |
| `DeleteReceipt` | `status` | DELETING 或 DELETED。 |
| `RunStartedData` | `ordinal` | 新轮次序号。 |
| `RunStoppingData` | `status` | 固定 CANCELLING。 |
| `ConversationDetailResponse` | `title` | 会话标题。 |
| `ConversationDetailResponse` | `current_run` | 当前轮摘要，历史读取也不改变。 |
| `ConversationDetailResponse` | `capabilities` | 当前允许的管理操作。 |
| `ConversationDetailResponse` | `selected_run_id` | 当前状态、报告和文件的所属轮次。 |
| `ConversationDetailResponse` | `run_id` | 所选轮次 UUID。 |
| `ConversationDetailResponse` | `history_next_cursor` | 更早记录游标，可空。 |
| `ConversationReceipt` | `run_id` | 创建的首轮 UUID。 |
| `MessageReceipt` | `run_id` | 消息所属轮次的 UUID。 |
| `AgentMessage` | `run_id` | 消息所属轮次。 |
| `AgentEvent` | `run_id` | 该事件所属轮次；sequence 仍在整个会话递增。 |

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

data: {"schema_version":2,"run_id":"50000000-0000-0000-0000-000000000001","sequence":13,"conversation_id":"10000000-0000-0000-0000-000000000001","case_id":"40000000-0000-0000-0000-000000000001","job_id":null,"type":"case.updated","created_at":"2026-09-07T08:00:01.000Z","data":{"status":"WAITING_INPUT","case_revision":2}}

data: {"schema_version":2,"run_id":"50000000-0000-0000-0000-000000000001","sequence":14,"conversation_id":"10000000-0000-0000-0000-000000000001","case_id":"40000000-0000-0000-0000-000000000001","job_id":null,"type":"assistant.question","created_at":"2026-09-07T08:00:02.000Z","data":{"questions":["问题发生在哪个时间段？"]}}

: heartbeat

```

所有业务事件的外层字段相同，`sequence` 在会话内单调递增。不发送 `id:`、`event:`、`retry:` 行，也不发送 `[DONE]`；结束标志是 JSON 中的 `type=conversation.completed`。这里使用基础 SSE 传输，不采用 OpenAI 的 `choices` / `delta` 响应格式。

连接建立后立即发送 `: connected` 注释，服务端每 15 秒发送一次空闲 `: heartbeat` 注释。注释不触发 `onmessage`，不是业务事件，也不更新游标。网站代理需关闭响应缓冲、保持流式转发，并将读取超时设为大于心跳间隔，例如 60 秒。

| 事件类型 | `data` 内容与网站行为 |
| --- | --- |
| `message.accepted` | 完整消息记录；显示已接收，用 `message_id` 去重 |
| `message.updated` | `message_id`、`status`、`notice`；更新采用状态 |
| `assistant.question` | `questions` 数组；按原文显示追问 |
| `agent.progress` | 执行阶段和固定中文进度说明；显示已经发生的阶段动作 |
| `case.updated` | `status`、`case_revision`；更新定位状态 |
| `result.available` | `status`、报告 `artifacts` 摘要、`result_field`；获取并校验正式报告 |
| `archive.updated` | `status`、归档 `artifacts` 摘要；更新 ZIP 或审计包下载入口 |
| `attachment.updated` | 完整会话附件状态；同步上传/导入状态 |
| `agent.failed` | `code`、`message`；读取会话快照并展示 `failure` 后，再推进处理游标 |
| `conversation.interrupted` | `code`、`message`；读取快照展示具体错误或中断说明，不能从固定 SSE 文案猜测失败阶段 |
| `conversation.completed` | `status`；对应 `run_id` 的诊断已结束，只有当前轮完成才关闭本轮订阅 |

公共进度不包含百分比、内部思考过程、工具参数、原始日志或路径。审核通过前，不会发出 Candidate 报告或根因结论。阶段事件不会增加 `case_revision`。

| 会话状态 | 建议文案 |
| --- | --- |
| `INTAKE` | 正在整理问题 |
| `WAITING_INPUT` | 请补充信息或上传日志 |
| `RUNNING` | 定位进行中，显示最新阶段消息 |
| `CANCELLING` | 正在停止，请等待当前操作退出 |
| `CANCELLED` | 已停止，可再次诊断 |
| `COMPLETED` | 本次定位已结束，请查看报告 |
| `FAILED` | 本次定位未能完成，请重新发起任务 |
| `INTERRUPTED` | 本次任务已中断，请重新发起 |

消息采用状态：`QUEUED` 表示已排队、`PROCESSING` 表示正在整理、`APPLIED` 表示已经采用、`UNUSED` 表示未用于本次诊断。请优先显示服务端的 `notice`，不要把排队消息显示为“已用于诊断”。

`GET conversation` 返回 schema 3 的完整会话视图；状态字段始终存在，历史、报告和下载列表按 `included` 更新，固定字段见上表。快照和 SSE 可能重叠；按消息 ID 更新消息，按事件序号去重。终态错误的 `diagnostic_id` 在刷新、历史回放和服务重启后保持不变，可交给运维关联服务端记录。

字段校验错误若有可公开的位置，`failure.details` 会含 `{"field":"location","actual":"input_values[2]"}` 这类条目。位置仅来自结构化错误的字段路径，不包含被拒绝的原始值、预期值或异常原文；没有安全位置时展示错误码、阶段和诊断 ID 即可。

`failure.details` 若含 `phase=ARCHIVE_STATUS_COMMIT` 和 `persistence=UNKNOWN`，表示报告已生成，但当前进程无法确认归档状态。页面保留 `RUNNING / PENDING`，展示这条提示并继续读取正式 JSON。它不写入会话历史，不代表诊断失败，也不会生成 `conversation.completed`。其他交付状态无法确认时，可能返回 HTTP `503`，并在 details 中提供可公开的错误详情。页面应保留已有内容，不得自行判定成功、失败或重新运行模型。

消息已接收但尚未创建 Case 时，若服务因其他任务故障暂停调度，查询快照和建立 SSE 会返回 `503 / DISPATCH_REJECTED`，详情为 `phase=DISPATCH_PAUSED`、`persistence=UNKNOWN`。页面提示任务暂时无法继续，保留原消息和收据；该提示不含其他任务 ID，也不改写会话终态。空会话和已关闭历史仍可查询。

网页可用 `EventSource` 订阅网站自己的同源 SSE 路由。由于响应没有 `id:` 行，它不会记录业务游标；短暂断线自动重连或刷新后的新连接都会从保留的历史开始回放，显示层必须按 JSON 的 `sequence` 去重。以下简化示例适用于历史尚未到期的会话；长期会话及精准续传应使用流式 `fetch`，手动把最后处理成功的序号放入 `Last-Event-ID` 请求头。历史已清理时，服务端明确返回游标过期，不能反复从 0 重连；按本文开头的快照恢复流程继续。

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
let currentRunId = null;
let snapshotSequence = 0;
const loadedReports = new Set();
const reportRequests = new Map();
const loadReport = (snapshot = null, runId = snapshot?.selected_run_id ?? currentRunId) => {
  if (loadedReports.has(runId)) return Promise.resolve(true);
  if (reportRequests.has(runId)) return reportRequests.get(runId);
  const reportRequest = (async () => {
    const response = snapshot ? null : await fetch(`/api/agent/conversations/${conversationId}?include=report&run_id=${encodeURIComponent(runId)}`);
    const result = snapshot ? { ok: true, data: snapshot } : await response.json();
    if ((response && !response.ok) || !result.ok || result.data?.schema_version !== 3 ||
        result.data?.conversation_id !== conversationId || result.data.selected_run_id !== runId || !result.data.included?.includes("report"))
      throw new Error("报告暂未加载成功，请重试。");
    if (stopped) return false;
    const report = result.data.result;
    if (!report || report.conversation_id !== conversationId) throw new Error("报告暂未加载成功，请重试。");
    if (report.report_state === "PENDING") return false;
    if (report.report_state === "UNAVAILABLE") {
      if (report.failure) await renderFailureAsText(report.failure);
      return false;
    }
    if (report.report_state !== "READY") throw new Error("报告状态不符合约定。");
    await renderVerifiedReport(report, runId); // 网站组件按 run_id 放到对应轮次卡片。
    if (stopped) return false;
    loadedReports.add(runId); // 每轮成功显示后才缓存；新轮不会复用旧结论。
    return true;
  })().finally(() => { reportRequests.delete(runId); });
  reportRequests.set(runId, reportRequest);
  return reportRequest;
};
const restoreConversation = async () => {
  const response = await fetch(`/api/agent/conversations/${conversationId}`);
  const result = await response.json();
  const snapshot = result.data;
  if (!response.ok || !result.ok || !snapshot || snapshot.schema_version !== 3 ||
      snapshot.conversation_id !== conversationId) throw new Error("会话信息暂未加载成功，请重试。");
  if (stopped) return;
  currentRunId = snapshot.current_run.run_id;
  snapshotSequence = snapshot.last_event_id;
  await renderConversationAsText(snapshot); // 网站组件：恢复消息、追问、附件和状态。
  if (stopped) return;
  if (snapshot.failure) await renderFailureAsText(snapshot.failure);
  if (stopped) return;
  if (snapshot.report_state === "READY") await loadReport(snapshot);
};
// 页面初次打开或刷新先恢复快照；事件排在恢复之后，不能用快照最大序号跳过未展示事件。
queue = restoreConversation().catch(fail);
const types = ["run.started", "run.stopping", "message.accepted", "message.updated", "assistant.question", "agent.progress",
  "case.updated", "result.available", "archive.updated", "attachment.updated",
  "agent.failed", "conversation.interrupted", "conversation.completed"];
events.onmessage = (message) => {
  if (stopped) return;
  if (pending >= 128) { fail(new Error("页面处理较慢，请重新连接并回放历史。")); return; }
  pending += 1;
  queue = queue.then(async () => {
    if (stopped) return;
    const event = JSON.parse(message.data);
    if (event.schema_version !== 2 || typeof event.run_id !== "string" || event.conversation_id !== conversationId ||
        !types.includes(event.type) || !Number.isSafeInteger(event.sequence) || event.sequence < 1)
      throw new Error("事件格式不符合约定。");
    if (event.sequence <= lastSequence) return;
    if (event.type === "run.started" && event.sequence > snapshotSequence) await restoreConversation();
    await renderEventAsText(event); // 按 run_id 更新对应卡片；旧轮事件不能覆盖当前轮状态。
    if (event.type === "result.available" && event.run_id === currentRunId && !await loadReport(null, event.run_id))
      throw new Error("报告尚未就绪，请稍后重新读取。");
    if ((event.type === "agent.failed" || event.type === "conversation.interrupted") &&
        event.run_id === currentRunId && event.sequence > snapshotSequence) await restoreConversation();
    if (stopped) return;
    lastSequence = event.sequence; // 所有业务处理成功后才能推进游标。
    if (event.type === "conversation.completed" && event.run_id === currentRunId) closeAgentEvents();
  }).catch(fail).finally(() => { pending -= 1; });
};
```

报告区已有可直接使用的 [report-view.js](../examples/website-agent/report-view.js)：导入 `renderReport` 后，将 `renderVerifiedReport(data)` 实现为 `renderReport(reportContainer, data)` 即可。组件按固定字段显示结构化报告、Skill 直出或通用诊断的 Markdown 原文，以及历史报告，处理空值、证据缺口及归档异常。先运行 [离线预览](../examples/website-agent/README.md)查看效果，再复制组件和 CSS 到网站。若使用 [browser-client.js](../examples/website-agent/browser-client.js)，其方法已经检查响应并返回 `data`，不要再次取 `.data`。

其余 `renderEventAsText`、`renderConversationAsText`、`renderFailureAsText`、`showEventRetry` 接入网站自己的消息、状态和重试组件。渲染须保持幂等，失败时抛出错误；普通文本不得作为 HTML 执行。`renderFailureAsText` 展示可公开的 code、phase 和诊断关联 ID；归档状态为 UNKNOWN 时保留报告区。历史结果事件只更新摘要卡片，用户点击后再用 SDK 的 `conversations.get(id, {include: ["report", "artifacts"], run_id: runId})` 获取并展示。这一步不依赖订阅是否仍在连接，刷新时也不会下载全部旧报告。当前轮结束后订阅会关闭；用户明确发送新问题后，重新初始化订阅和快照。组件销毁时调用 `closeAgentEvents()`，只断开订阅，不取消诊断。服务器和示例均限制待处理事件数量，慢连接不会无限积压。

临时网络断线可由 `EventSource` 自动重连，但这里不会自动携带业务 `Last-Event-ID`，重连会回放历史，本例按 `lastSequence` 跳过已经处理成功的事件。报告加载、解析或显示失败时，本例关闭连接并显示重试入口，不处理排队中的完成事件；点击重试需重新执行订阅初始化，从历史回放，页面按 ID 更新已有内容。需要持久精准续传时，用流式 `fetch` 携带最后处理成功的 `Last-Event-ID`；按空行拆帧并忽略以冒号开头的注释，不按读取到的网络块直接 `JSON.parse`。刷新恢复还应根据会话快照重新获取已经发布的报告，不能只恢复进度文字。

## 4. 正式报告与下载校验

### 会话中的报告

会话查询的 `data.result` 复用固定的 `ConversationReportView`，内部 JSON 报告 schema 仍为 3。完整会话一次返回报告；只更新报告时请求 `GET /api/v1/agent/conversations/{conversation_id}?include=report`。服务端从同一份 Case 快照选择报告和产物，不在网站后端串联 Case 查询，不调用模型或重跑证据审核。

以下 `report_state` 和正文字段均相对于 `data.result`：

| `report_state` | HTTP 状态 | 网站行为 |
| --- | --- | --- |
| `PENDING` | `200 / ok=true` | 尚未产出报告，包括等待补充，正文为空，继续显示状态或追问 |
| `READY` | `200 / ok=true` | 按 `format` 展示正式报告；Markdown 原样渲染，结构化报告内部可为 `COMPLETED`、`PARTIAL` 或 `INCONCLUSIVE` |
| `UNAVAILABLE` | `200 / ok=true` | 任务已结束且没有报告，展示 `failure`；旧历史的 failure 可为 null |

`format=problem-locator-diagnosis-v3` 时正文在 `report`；`format=markdown` 时正文在 `markdown`；历史 `format=generic-v1` 时正文在 `report`，且 `artifact=null`。未就绪和无报告不是 HTTP 错误；非法 ID、会话不存在、读取故障或文件损坏仍返回受控的 `4xx/5xx`。`artifact.sha256` 描述原始产物字节，不是整个 API 响应的哈希。

当前默认 `METHODS_EVIDENCE_VALIDATION=off`，专用定位也返回 `format=markdown`。正文直接采用 Skill 输出，`case_status` 采用 Skill 自己选择的 `RESOLVED` 或 `UNRESOLVED`；框架不再复核结论证据、清空根因或自动改为 `PARTIAL`。网站按已有 Markdown 分支展示，不能因为诊断模式是 `SPECIALIZED` 就要求 JSON 报告。该模式不生成 `diagnosis-result.json` 或 `result.zip`，`archive_status=NOT_REQUIRED`。

报告已发布时，即使 `archive_status=PENDING/FAILED` 或 failure 提示归档状态无法确认，`report_state` 仍为 `READY`。读取会话报告不会读取 ZIP。报告原始内容上限为 16 MiB；网站示例为含报告的响应保留 `6 × 16 MiB + 64 KiB`，同时包含历史时再加 16 MiB。不含报告的 JSON 响应上限仍为 16 MiB。

上面的前端示例会合并并发读取，并在报告显示成功后按 run_id 缓存结果。因此刷新时读取的报告不会因历史 `result.available` 回放再次下载；读取或渲染失败不会写入成功缓存。归档状态仍由会话事件或 `include=none` 更新，下载列表用 `include=artifacts` 更新。

### 会话内的下载信息

`result.available` 是报告已发布的通知。会话 `artifacts` 给出已核验产物的 ID、种类、类型、字节数、SHA-256、来源 Job 和下载入口；`created_by_job_id` 与同一会话响应的 `source_job_id` 一致。网站直接显示这些下载项，不再请求 Case 或单独产物列表。

默认专用定位与 Generic V2 都发布唯一的 `GENERIC_REPORT` Markdown 产物。只有手动恢复 `advisory` / `strict` 的专用定位才发布 `USER_RESULT` / `diagnosis-result.json`。下载响应必须为 HTTP 200，不允许重定向；真实字节数、SHA-256 和 Content-Type 必须与服务端的产物描述一致。响应可以省略 `Content-Length` 和 `X-Content-SHA256`，但存在时也必须核对；不能因为缺少响应头就跳过实际内容校验。结构化报告要求 `schema_version=3`、`format_id=problem-locator-diagnosis-v3`，并保留完整字段。不要根据 `methods_result`、SSE 阶段消息或 stdout 重新拼出结论。

| Case 状态 | 报告和可用产物 |
| --- | --- |
| `RESOLVED` | 默认展示 Skill Markdown 原文；`advisory` / `strict` 的结构化结果为 `COMPLETED` JSON，ZIP 可处于 `PENDING` |
| `PARTIALLY_RESOLVED` | `advisory` / `strict` 的 `PARTIAL` JSON；默认直出不会自动产生该状态 |
| `UNRESOLVED` | 默认仍展示 Skill Markdown 原文；`advisory` / `strict` 的结果为 `INCONCLUSIVE` JSON 和审计包，`root_cause=null`，没有结果 ZIP |
| `FAILED` / `CANCELLED` / `INTERRUPTED` | 展示 failure 或状态，不伪造报告 |

Markdown 报告按原文展示，渲染器应关闭原始 HTML。结构化报告的展示顺序为：定位结论、问题描述、关键发现、确认/候选/排除因素、完成条件、服务端验证及证据、时间相关性、证据缺口、限制、处置建议与安全说明。缺失必需字段属于协议错误；`null` 或空数组按其真实含义显示，不自动补写根因。会话中的 `result` 已由 xiaodao 服务按原始产物字节校验，前端可直接渲染，无需重复下载，也不要对重新序列化的 JSON 计算产物哈希。这些校验只验证文件内容是否完整，不代表已核实报告结论的证据。

`archive_status=PENDING`：JSON 立即可展示，继续等 `archive.updated`。`READY`：按用户请求下载 `USER_RESULT_ARCHIVE` / `result.zip`。`FAILED`：归档失败，但已经交付的报告仍有效。`NOT_REQUIRED`：没有待生成的结果 ZIP，审计包是否可下载以产物列表为准。

下载 ZIP 前显示：“该文件包含原始目标日志，可能含有业务信息。请确认后下载。”审计包也仅按用户请求下载。ZIP 代理使用唯一临时文件，完整校验后再发给浏览器，成功和失败都清理临时文件，不把全部日志 ZIP 缓存在内存中。独立下载报告文件时，网站代理在 16 MiB 上限内使用有界内存，按实际字节数和 SHA-256 校验完成后才交付原文件。

## 5. 可运行的 TypeScript 网站后端

仓库的 `examples/website-agent/server.ts` 使用 Node.js 24 内置 TypeScript 支持和标准库，无需安装 npm 包。`Access` 类型和启动入口在此文件，业务逻辑统一在 `server.mjs` 中实现。示例提供登录回调、服务端用户权限检查、同源会话接口、SSE 转发、上传转发、统一会话视图和文件下载校验。网站可直接集成其中的逻辑，也可将它作为后端服务接入现有反向代理。

```powershell
$env:XIAODAO_BASE_URL = 'http://xiaodao.internal:8000'
$env:WEBSITE_AUTH_MODULE = 'D:\website\xiaodao-access.mjs'
node examples/website-agent/server.ts
```

Linux 启动命令、认证回调及反向代理要求见 [示例 README](../examples/website-agent/README.md)。示例固定监听 `127.0.0.1`，不是可以直接暴露给所有浏览器的完整网站。

未配置 `WEBSITE_AUTH_MODULE` 时，服务仍可启动，但业务请求全部返回 `401`。不要为了联调删掉授权检查。认证模块导出 `access`，只需实现 `authenticate(request)`：验证网站登录态，返回稳定的 `{id}`，未登录时返回 `null`。BFF 根据已认证的 ID 和固定的 `WEBSITE_OWNER_NAMESPACE` 生成 `owner_key`。会话、附件与用户的对应关系统一由 xiaodao 保存，BFF 不再单独维护一份。Cookie 认证还需接入网站既有 CSRF 和 Origin 校验；不得相信客户端自行传入的用户名或用户 ID。

网站示例前缀是 `/api/agent`，xiaodao 上游前缀是 `/api/v1/agent`。两者的会话和附件操作一致；网站负责授权、受控错误和同源下载地址，不再额外生成中文 sections。

| 网站示例路径 | 用途 |
| --- | --- |
| `GET /api/agent/conversations/{id}` | 一次返回完整会话，支持与上游相同的 include 参数 |
| `POST /api/agent/attachments` | 预约上传；JSON 必须带 conversation_id，先检查该会话是否属于当前用户 |
| `GET /api/agent/conversations/{id}/files/{artifact_id}/content` | 从会话核验文件身份后下载 |
| 上一下载路径加 `?download=archive&acknowledge_raw_logs=true` | 用户确认原始日志提示后下载结果 ZIP |
| 上一下载路径加 `?download=audit` | 用户主动下载审计包 |

文件下载仍流式传输，与会话 JSON 分开；下载入口由会话提供，属于会话能力。旧独立读取路径和旧 SDK 方法不再提供。

示例只向配置的 `XIAODAO_BASE_URL` 发请求。检查会话所属用户、Case ID、Artifact ID 和来源 Job 后，根据固定 API 路径构造内部下载地址。响应中的 `download_url` 不用于构造请求地址，也不能指定主机、路径或重定向目标。网站访问的内部上游地址可以与服务端 `PUBLIC_BASE_URL` 不同，配置中的路径前缀会保留；浏览器始终使用网站同源接口。

运行接入示例确定性测试：

```bash
node --test examples/website-agent/server.test.mjs
```

测试覆盖权限、跨用户幂等命名空间、SSE 游标与心跳、失败快照、安全错误、恶意 URL 不参与下载、内部路径前缀、缺省或错误响应头、真实字节和哈希校验，以及 ZIP 主动下载确认。文档中的前端片段由 `tools/test-flow/tests/website-agent-guide.test.mjs` 执行，覆盖快照恢复、失败展示、游标和报告加载顺序。实际部署还应检查网站反向代理没有缓冲 SSE，且只允许网站后端连接 xiaodao。

## 6. 服务配置与升级

当前产品版本和数据格式仍是 `8.2.0` / `v11-contract-r2`。本次网站会话查询改为 `schema_version=3`，旧查询入口已删除，网站前后端需同步升级。报告 schema 3 不变；Agent 存储升级为 2、会话详情为 3、公开事件为 2。旧事件的原始字节不改，读取时转换为 v2 格式。已有 r1 或 r2 数据必须提供 `--ownership-map`，按[副本升级说明](data-upgrade-v11-r2.md)手动升级后，才能交给新版本。请保留原数据和历史报告，不要直接修改数据格式标记或把旧目录交给新版本。

历史兼容说明：早期 `8.0.0` 预览版曾把命名事件改成只含 data 的事件帧，那次 SSE 调整本身没有改变 V11 数据格式。仍使用旧版 `event:` 监听器的网站须改用 `onmessage`，从 JSON 读取 `type` 和 `sequence`，不再依赖 `lastEventId` 或服务端 `retry:`。这段历史说明不代表本次 8.1 升级无需处理数据格式变化。

`INTAKE_CLAUDE_COMMAND` 配置信息整理角色的独立命令；未设置时沿用路由角色命令。该角色只在已创建的 Case 需要补充信息时整理用户消息和公开 requirements，不负责创建任务，也没有使用诊断工具、读取日志或发布结果的权限。

`METHODS_EVIDENCE_VALIDATION` 当前默认为 `off`：直接交付 Skill Markdown，关闭输出后的依据核验、证据一致性复核、Candidate 语义判定和独立 Reviewer。即使旧配置仍有 `SPECIALIZED_REVIEWER_ENABLED=true`，也不会启动审核。模型执行前的 Logparse、固定日志快照、marker 扫描和命中方法卡加载保持不变。新 Job 使用创建时固定的 `agent-profile/skill-direct` 和 `output-contract/skill-direct`，保持 `SPECIALIZED` 模式与 `selected_skill_ref`；Case 使用 `generic_result_v2` 记录 Markdown 和实际 `skill_name`。已有 Job 保留创建时固定的配置标识。升级或切换策略前，应先结束活跃任务，再重启服务。

手动设置 `advisory` 可恢复原建议模式及 `PARTIAL` / `INCONCLUSIVE` 结构化交付，设置 `strict` 可恢复原核验方式；这两种策略下，`SPECIALIZED_REVIEWER_ENABLED` 继续控制独立审核。单项输入无效只影响该项；非法输入、模型执行协议错误、共享输入变化，以及权限、路径和文件完整性异常仍会使任务失败。不增加模型重试或任务续接。详见[诊断交付策略](diagnosis-advisory.md)。

要求输出 JSON 的模型阶段，如果在最终 `result` 中先写 Markdown 说明、再给出唯一完整 JSON，服务端会按[受限提取规则](model-output-compatibility.md#说明文字与最终-json)处理，前端无需自行截取或修复。出现多个候选、内容截断或无法识别的结果时，仍会返回具体失败信息。默认 Skill 直接输出首行 `SKILL_DIAGNOSIS_RESULT_V1` 终态标记和 Markdown 正文，不做 JSON 提取。`stream-json` 只规定 CLI 事件的外层格式，不改变各阶段的业务输出要求；兼容处理不增加模型调用。

新部署使用全新空 `DATA_ROOT`；升级 `8.0.0` 时，需手动生成并核验 r2 副本，原目录保持原样。其他旧数据按升级说明支持的范围处理，不自动迁移，也不从旧 `methods_result` 反推报告。MCP 仍为原来的七个工具，输入参数继续平铺在根层；网站直接使用 REST Agent 接口。

## 7. 状态与报告响应字段

统一会话顶层字段见第 2 节。以下 `ConversationReportView` 是 `data.result` 的固定结构，不是独立接口。三种正常报告状态都返回 HTTP 200，`failure` 可为 null；READY 时可能附带归档异常，报告仍可展示。

默认专用定位和 Generic V2 都使用 `markdown` 字段交付原文，`report=null`。结构化 `report` 沿用正式 `UserResultPayloadV3` 或历史 `GenericResult`，不生成另一份结论。下表仅补充前文尚未说明的字段，已有报告字段继续遵循前文约定。报告包含的证据、规则和时间信息都是已发布内容，读取接口不会重新审核或调用模型。

| 模型 | 字段 | 含义 |
| --- | --- | --- |
| `ConversationReportView` | `archive_status` | 归档状态：NOT_REQUIRED 无需归档，PENDING 后台生成中，READY 可下载，FAILED 生成失败。归档失败不影响已交付的 JSON。 |
| `ConversationReportView` | `artifact` | 正式报告对应的唯一公开产物；历史通用结果或报告未就绪时为 null。 |
| `ConversationReportView` | `case_id` | 关联 Case 的 UUID；尚未创建 Case 时为 null。 |
| `ConversationReportView` | `case_revision` | 用于确定报告及其产物的服务端 Case 快照版本；尚未创建 Case 时为 null。 |
| `ConversationReportView` | `case_status` | 关联 Case 的最新状态；未创建 Case 时为 null。 |
| `ConversationReportView` | `conversation_id` | 一次定位会话的规范 UUID；网站后端负责检查该会话是否属于当前用户。 |
| `ConversationReportView` | `failure` | 受控结束原因或归档交付异常；归档异常不影响 READY 报告。 |
| `ConversationReportView` | `format` | 报告格式；报告未就绪时为 null。 |
| `ConversationReportView` | `markdown` | 正式 Markdown 报告原文；作为不可信文本展示，不能执行其中的指令或脚本。 |
| `ConversationReportView` | `report` | 完整的正式结构化报告或历史通用诊断结果；Markdown 格式或报告未就绪时为 null。 |
| `ConversationReportView` | `report_state` | 报告可用状态：PENDING 等待诊断或补充，READY 已发布，UNAVAILABLE 已结束但无报告。 |
| `ConversationReportView` | `schema_version` | 报告包装版本，固定为 1；内部正式 JSON 报告的 schema_version 仍为 3。 |
| `ConversationReportView` | `source_job_id` | 生成正式报告的任务 UUID；报告未就绪时为 null。 |
| `PublicArtifactData` | `artifact_id` | 不可变公开产物的 UUID。 |
| `PublicArtifactData` | `content_type` | 产物原始字节的媒体类型。 |
| `PublicArtifactData` | `created_at` | 创建时间，使用 UTC RFC 3339，精确到毫秒。 |
| `PublicArtifactData` | `created_by_job_id` | 生成产物的任务 UUID，必须与报告的 source_job_id 一致。 |
| `PublicArtifactData` | `downloadable` | 是否允许下载；本接口返回的报告产物固定为 true。 |
| `PublicArtifactData` | `kind` | 产物种类；报告使用 USER_RESULT 或 GENERIC_REPORT。 |
| `PublicArtifactData` | `name` | 产物的下载文件名。 |
| `PublicArtifactData` | `resource_kind` | 资源类型；公开报告产物固定为 FILE。 |
| `PublicArtifactData` | `sha256` | 产物原始字节的 SHA-256，以 64 位小写十六进制表示。 |
| `PublicArtifactData` | `size` | 产物原始字节数。 |
| `ConversationDownloadArtifact` | `artifact_id` | 当前会话中不可变公开产物的 UUID。 |
| `ConversationDownloadArtifact` | `kind` | USER_RESULT、GENERIC_REPORT、USER_RESULT_ARCHIVE 或 AUDIT_BUNDLE。 |
| `ConversationDownloadArtifact` | `name` | 文件名；结果日志包名为 result.zip。 |
| `ConversationDownloadArtifact` | `content_type` | 原始文件的媒体类型，须与产物种类一致。 |
| `ConversationDownloadArtifact` | `resource_kind` | 固定为 FILE。 |
| `ConversationDownloadArtifact` | `size` | 文件实际字节数；下载时据此核对内容。 |
| `ConversationDownloadArtifact` | `sha256` | 原始文件的 64 位小写 SHA-256；不是会话响应哈希。 |
| `ConversationDownloadArtifact` | `created_by_job_id` | 来源任务，与当前会话响应的 source_job_id 一致。 |
| `ConversationDownloadArtifact` | `created_at` | 文件发布的 UTC 时间。 |
| `ConversationDownloadArtifact` | `downloadable` | 固定为 true；会话仅提供可下载的正式产物。 |
| `ConversationDownloadArtifact` | `download_url` | 已核验标识构成的文件入口；网站后端重写为授权后的同源 URL。 |
| `AgentProgressData` | `stage` | 当前最新的公开执行阶段，例如 ROUTE、LOGPARSE、DIAGNOSE、REVIEW、ARCHIVE 或 INTAKE。 |
| `AgentProgressData` | `message` | 与 stage 对应的固定中文进度说明，不包含模型内部推理。 |
| `UserResultPayloadV3` | `candidate_factors` | 尚未确认为原因的候选因素。 |
| `UserResultPayloadV3` | `causal_factors` | 报告保留的致因因素。 |
| `UserResultPayloadV3` | `evidence_gaps` | 未解决的证据缺口。 |
| `UserResultPayloadV3` | `excluded_factors` | 报告已排除的因素。 |
| `UserResultPayloadV3` | `limitations` | 本次诊断的范围和已知限制。 |
| `UserResultPayloadV3` | `problem_statement` | 本次诊断分析的问题描述。 |
| `UserResultPayloadV3` | `recommendations` | 报告建议的后续处理步骤。 |
| `UserResultPayloadV3` | `safety_notes` | 报告中的使用边界和注意事项。 |
| `UserResultPayloadV3` | `source_job_type` | 生成报告的任务阶段：DIAGNOSE 或 REVIEW。 |
| `UserResultPayloadV3` | `supporting_evidence_bindings` | 支持整份报告的证据引用。 |
| `UserResultFindingV2` | `citations` | 证据在日志中的具体位置及原文摘录。 |
| `UserResultFindingV2` | `confidence` | 发现的置信度。 |
| `UserResultFindingV2` | `evidence_bindings` | 支持当前发现、因素或规则的证据引用。 |
| `UserResultFindingV2` | `statement` | 发现或因素的具体说明。 |
| `UserResultFactorV3` | `citations` | 证据在日志中的具体位置及原文摘录。 |
| `UserResultFactorV3` | `evidence_bindings` | 支持当前发现、因素或规则的证据引用。 |
| `UserResultFactorV3` | `factor_id` | 报告中因素的稳定标识。 |
| `UserResultFactorV3` | `required_rule_ids` | 支持该因素的规则标识。 |
| `UserResultFactorV3` | `role` | 因素在因果关系中的角色。 |
| `UserResultFactorV3` | `statement` | 发现或因素的具体说明。 |
| `UserResultCitationV2` | `archive_name` | 引用文件在归档内的相对名称；无文件定位时为 null。 |
| `UserResultCitationV2` | `evidence_binding` | 一条证据引用；已有证据 ID 与提案键二者取其一。 |
| `UserResultCitationV2` | `excerpt` | 引用的原文摘录；无文件定位时为 null。 |
| `UserResultCitationV2` | `line_end` | 引用结束行号，包含该行；无文件定位时为 null。 |
| `UserResultCitationV2` | `line_start` | 引用起始行号，从 1 开始；无文件定位时为 null。 |
| `UserResultCitationV2` | `raw_bytes_sha256` | 引用日志原始字节的 SHA-256；无文件定位时为 null。 |
| `EvidenceBinding` | `evidence_proposal_key` | 本轮证据提案的键；引用已有证据时为 null。 |
| `EvidenceBinding` | `existing_evidence_id` | 已有证据的 UUID；引用本轮提案时为 null。 |
| `CompletionCriterionDraftMapping` | `criterion` | 问题中对应的完成条件原文。 |
| `CompletionCriterionDraftMapping` | `criterion_index` | 完成条件在原始列表中的位置，从 0 开始。 |
| `CompletionCriterionDraftMapping` | `evidence_bindings` | 支持当前发现、因素或规则的证据引用。 |
| `CompletionCriterionDraftMapping` | `explanation` | 该判断、规则结果或时间关联的说明。 |
| `CompletionCriterionDraftMapping` | `status` | 完成条件的判断：SATISFIED 满足、PARTIALLY_SATISFIED 部分满足、UNSATISFIED 未满足、UNKNOWN 尚无法判断。 |
| `UserResultVerificationRuleV2` | `citations` | 证据在日志中的具体位置及原文摘录。 |
| `UserResultVerificationRuleV2` | `derived_values` | 根据观测值计算的派生数据。 |
| `UserResultVerificationRuleV2` | `event_observations` | 规则记录的事件观测值。 |
| `UserResultVerificationRuleV2` | `evidence_bindings` | 支持当前发现、因素或规则的证据引用。 |
| `UserResultVerificationRuleV2` | `explanation` | 该判断、规则结果或时间关联的说明。 |
| `UserResultVerificationRuleV2` | `issues` | 规则未满足或无法核验的具体原因。 |
| `UserResultVerificationRuleV2` | `observed_times` | 规则提取出的 UTC 事件时间。 |
| `UserResultVerificationRuleV2` | `rule_id` | 报告中规则的稳定标识。 |
| `UserResultVerificationRuleV2` | `rule_kind` | 规则种类。 |
| `UserResultVerificationRuleV2` | `status` | 规则结果：VERIFIED_PASS、VERIFIED_FAIL、UNVERIFIABLE、SEMANTIC_ONLY 或 NOT_APPLICABLE；不等同于整份报告的状态。 |
| `UserResultTimeRelevanceV2` | `assessment` | 时间关联判断：RELEVANT、NOT_RELEVANT 或 UNKNOWN。 |
| `UserResultTimeRelevanceV2` | `citations` | 证据在日志中的具体位置及原文摘录。 |
| `UserResultTimeRelevanceV2` | `derived_anchor_time` | 从日志推导的基准时间；未确定时为 null。 |
| `UserResultTimeRelevanceV2` | `explanation` | 该判断、规则结果或时间关联的说明。 |
| `UserResultTimeRelevanceV2` | `observations` | 用于时间关联判断的观测记录。 |
| `UserResultTimeRelevanceV2` | `problem_time` | 用于比较的问题时间；没有确定时间时为 null。 |
| `UserResultTimeObservationV2` | `event_time` | 日志事件的 UTC 时间。 |
| `UserResultTimeObservationV2` | `offset_ms` | 事件相对问题时间的偏移，单位为毫秒。 |
| `UserResultTimeObservationV2` | `rule_id` | 报告中规则的稳定标识。 |
| `EventObservationAudit` | `count_is_lower_bound` | 观测数量是否仅表示已知下限。 |
| `EventObservationAudit` | `event_id` | 诊断规则中被观测事件的稳定名称；不是 SSE 事件序号。 |
| `EventObservationAudit` | `observed_count` | 已观测到的事件数量。 |
| `DerivedValueAudit` | `lower_bound` | 派生数值的下界；未确定时为 null。 |
| `DerivedValueAudit` | `name` | 派生数值的稳定名称。 |
| `DerivedValueAudit` | `unit` | 派生数值使用的单位。 |
| `DerivedValueAudit` | `upper_bound` | 派生数值的上界；未确定时为 null。 |
| `DerivedValueAudit` | `value` | 派生结果的文本或整数值；未确定时为 null。 |
