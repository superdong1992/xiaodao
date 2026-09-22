# 网站 Agent 接入：先预览报告，再联调服务

已接入旧版的网站，请先看 [8.2 前端升级清单](website-agent-upgrade-8.2.md)。其中列出了本次必须调整的接口、字段和状态处理逻辑，并附有可直接使用的开发 Prompt。

本文供网站前后端开发人员和 Linux 测试环境运维人员使用，适用于 xiaodao `8.2.0` / V11 / `v11-contract-r2`。建议先用离线预览确认报告的展示效果，再复制浏览器模块，最后接入权限校验、日志上传和实时进度，完成一次完整定位。测试服务仍需按文末清单验收。

网站保留原有问答功能，另建一个 Agent 入口。调用关系是：**浏览器 → 网站后端 → xiaodao**。网站无需安装 MCP 客户端，也无需将用户输入转换为 `problem_spec`。

需要接入通用报告的点赞、点踩时，见[经验库与赞踩接入说明](generic-feedback-memory.md)。按钮由网站实现；本仓库提供按报告轮次读取和提交评价的封装。

## 1. 先预览报告，再复制到网站

使用 Node.js 24+，从仓库根目录运行：

```bash
node examples/website-agent/preview.mjs
```

打开 [http://127.0.0.1:8788/](http://127.0.0.1:8788/)。本地预览使用固定示例数据，不连接服务端、不创建任务，也不调用模型。先查看完整结果、部分结果、尚无定论、等待和失败等状态，确定网站的展示方式。

将 `examples/website-agent/` 中的 `report-view.js`、`report-view.css` 和 `browser-client.js` 复制到网站的静态资源目录，例如 `/xiaodao/`。这些模块没有额外依赖，可以嵌入现有页面。后端接入同源 `/api/agent/` 接口后，用已有会话 ID 读取报告：

```javascript
import { createAgentClient } from "/xiaodao/browser-client.js";
import { renderReport } from "/xiaodao/report-view.js";

const client = createAgentClient();
const conversation = await client.conversations.get(conversationId);
renderReport(document.querySelector("#diagnosis-report"), conversation.result);
```

页面需加载 `report-view.css` 并提供 `#diagnosis-report` 容器。客户端方法直接返回响应中的 `data`，失败时抛出 `AgentApiError`，不会自动重试。网站应在独立的错误提示区提供重试入口，同时保留已显示的报告。直接使用 `fetch` 时，先检查 HTTP 状态和 `ok`，再调用 `renderReport(container, response.data.result)`。

渲染模块先判断 `report_state` 的三种状态，再根据 `format` 展示 JSON、Markdown 或历史 Generic V1 结果。JSON 使用 `result.report.root_cause`、`result.report.findings`、`result.report.evidence_gaps` 等固定字段填充组件，不根据 `sections.title` 的中文标题提取内容。Markdown 默认以纯文本展示，保留原文；需要排版时可接入网站自己的渲染器，并过滤不安全的 HTML 和链接。读取已有报告不会重新调用模型。

可直接使用的完整 HTML、文件用途和“字段 → 组件”对应关系见[网站示例说明](../examples/website-agent/README.md)。确认界面后，再准备接入服务所需的信息：

| 准备项 | 所需信息 |
| --- | --- |
| 测试服务地址 | 网站后端可访问的完整地址、端口和路径前缀；不要把内部地址直接暴露给浏览器 |
| 部署版本 | 应为 `8.2.0` / `v11-contract-r2`，同时记录部署的 commit 或源码快照；不能仅凭进程启动判断版本 |
| 接口规范 | 在线 `GET /openapi.json`；可视化入口 `GET /docs`；仓库 [OpenAPI 快照](../schemas/v2/web-api.openapi.snapshot.json) |
| 完整说明 | [Agent API 参考](website-agent-api.md)：请求响应、附件、SSE、字段、错误与报告校验 |
| 网站接入示例 | [预览、浏览器模块和后端启动说明](../examples/website-agent/README.md)：先展示报告，再接入登录回调、服务端用户归属校验、上传和 SSE |
| 联调样本 | 经批准可用于测试的真实问题和配套压缩日志；按任务要求补充时间、环境等信息。创建 Case 前无需提供预期行为，回答追问时不得编造事实 |
| 网络和运行约束 | 允许访问的后端来源、反向代理配置、上传限制、模型调用预算、测试负责人 |

在线接口规范以**实际部署的服务**为准。如果在线版本、路由与本仓库不同，先统一部署版本，不要让网站适配旧版本。示例仅使用配置的 `XIAODAO_BASE_URL` 和已核验的会话、轮次、产物 ID 构造下载路径，不使用响应中的 URL。`XIAODAO_BASE_URL` 可以与服务端 `PUBLIC_BASE_URL` 不同，内部地址的路径前缀会保留。

## 2. 先做不调用模型的连通检查

在**网站后端所在机器**执行，替换示例地址；下列命令不会创建定位任务。

```bash
export XIAODAO_BASE_URL='http://xiaodao.internal:8000'
curl --fail-with-body "$XIAODAO_BASE_URL/live"
curl --fail-with-body "$XIAODAO_BASE_URL/ready"
curl --fail-with-body "$XIAODAO_BASE_URL/openapi.json"
```

依次确认：

1. `/live`：HTTP 200，`{"ok":true,"data":{"status":"live"},"error":null}`。
2. `/ready`：HTTP 200，`ok=true`、`data.ready=true`。失败时先由运维根据返回的错误码排查。该接口检查服务是否就绪，**不代表模型或完整诊断流程已经可用**。
3. `/openapi.json`：直接返回 OpenAPI 文档，不使用 `ok/data` 包装；`info.version` 为 `8.2.0`，`paths` 中有 `/api/v1/agent/conversations` 及消息、事件、附件路由。

Swagger 页面位于服务的 `/docs`。如果内网资源加载受限，或代理添加了路径前缀，页面可能无法打开。当前页面从根路径 `/openapi.json` 加载接口规范，遇到问题时可先直接读取 OpenAPI。文档页面无法打开不代表业务 API 不可用，也不应因此开放公网访问。

路由存在只说明接口已注册。接下来创建一个空会话，再查询该会话，确认 Agent 服务可以正常调用。这一步会保存会话，但不会调用模型。网站后端应先接入登录和用户归属校验，再使用网站自己的同源接口测试。直接检查 xiaodao 服务时，创建请求如下：

```http
POST /api/v1/agent/conversations
Content-Type: application/json

{"request_id":"website-smoke-20260907-001"}
```

直接请求 xiaodao 接口时，必须携带 `X-Agent-Owner-Key`。该值由可信后端根据固定的网站命名空间和登录用户 ID 计算，格式为 64 位小写 SHA-256，不能由浏览器自行提交。保存响应中的 `data.conversation_id` 和 `run_id`，再调用 `GET /api/v1/agent/conversations/{conversation_id}`，应能读回同一会话。重试时沿用同一 `request_id`，下一次独立测试则使用新 ID。

`503 / AGENT_UNAVAILABLE` 表示 Agent 服务未就绪，前端重试无法解决。排查时不要删除已有 `DATA_ROOT`。新部署使用空目录；已有 r1 或 r2 数据需提供 `--ownership-map`，按[副本升级说明](data-upgrade-v11-r2.md)手动升级，并保留原目录和历史报告。

## 3. 网站前后端分别做什么

先接入网站登录和用户归属校验，再启用服务接口。浏览器模块 `createAgentClient()` 默认使用同源 `/api/agent`；可以配置 `basePath`、`fetchImpl` 和用于 CSRF 的 `headers` 回调，无需向浏览器提供服务端地址。

| 负责方 | 首版必须完成 |
| --- | --- |
| 网站后端 | 验证登录；根据登录身份生成稳定的用户归属键；所有请求交由 xiaodao 统一校验用户归属；转发服务端报告、SSE 和文件；下载原始产物时校验来源、大小和 SHA-256 |
| 网站前端 | 提供输入框、压缩日志上传、追问、消息采用状态、阶段进度、报告区和下载按钮；刷新后恢复历史；按事件序号去重 |
| xiaodao 运维 | 配置问题整理和诊断角色、日志解析与可选审核；确认模型身份和预算；限制服务可达来源；保证 SSE 不被代理缓冲 |

网站前端访问网站自己的 `/api/agent/...`（示例路径）；网站后端访问 xiaodao 的 `/api/v1/agent/...`。两者不要混用。UUID 不是授权凭据；网站不得相信前端自报的 `user_id`。示例没有内置登录系统，未接入授权回调时返回 `401` 是预期行为。

客户端只提供 `conversations` 和 `attachments` 两组方法。先使用 `conversations.create(requestId)`、`conversations.send(id, message)` 创建会话并发送消息。刷新页面时，用 `conversations.get(id)` 一次恢复状态、历史和报告；日常轮询则使用 `conversations.get(id, {include: []})`。同一逻辑请求的 ID 和原始内容应保存在按钮回调及网络重试函数之外。不要使用网站后端改写后返回的 `request_id` 重新创建会话。最简提交代码见[网站示例](../examples/website-agent/README.md)。

上传时先调用 `attachments.prepare(id, metadata)`，再把完整的上传预约结果和文件传给 `attachments.upload(prepared, file)`。文件 SHA-256 需使用网站现有的增量哈希组件或后端上传模块计算，避免一次读取数 GiB 日志；客户端示例不会自动计算哈希。`file.type` 可能为空，应根据支持的压缩后缀确定 MIME。仅带附件的消息应省略 `text` 或传 null。`conversations.eventsUrl(id)` 返回网站的 SSE 路径，可配合 [API 参考](website-agent-api.md)中的串行事件处理和游标续传代码使用。

## 4. 按顺序完成首次定位

发送消息会调用真实模型。测试负责人需先按[官方 Test Flow 说明](../tools/test-flow/README.md)审查对应 `--plan-only` 输出中的身份、调用数、模型预算、预计 token 用量和费用、执行前仍需解决的问题（admission blocker），以及测试项（Proof）、阶段（Stage）、检查门槛（Gate）和复用决定，再开始真实模型验证。手工联调记录不能替代官方 `verdict.json`。

| 步骤 | 网站调用与展示 | 成功标志 |
| --- | --- | --- |
| 创建会话 | `POST /api/v1/agent/conversations`，由可信后端提交登录用户的 `owner_key` | 拿到 `conversation_id` 和首轮 `run_id` |
| 订阅进度 | `GET /api/v1/agent/conversations/{conversation_id}/events` | `text/event-stream`；每条业务消息为一行 `data: <JSON>` 加空行，`onmessage` 可直接接收；空闲每 15 秒有注释心跳 |
| 发送问题原文 | `POST /api/v1/agent/conversations/{conversation_id}/messages` | 立即收到 `ACCEPTED` 回执；非空问题文本按 MCP 固定中性模板创建 Case，初始事实为空，创建前不调用 INTAKE、不追问预期行为 |
| 回答追问 | Case 创建后按原文展示 OPEN requirements 的 `assistant.question`，仍调用同一消息接口回答 | 仅补充任务当前要求；没有 OPEN requirements 就不追问，不要求网站生成诊断字段 |
| 补充日志 | 预约附件 → 按描述符 PUT 原始字节 → 发消息引用 `attachment_ids` | 上传为 `READY`；消息是否被采用另看 `APPLIED` / `notice` |
| 显示进度 | 展示 `agent.progress`，同步 `case.updated` | 能看到实际执行阶段；审核前没有根因或 Candidate 报告 |
| 展示报告 | 收到 `result.available` 后读取会话 `GET /api/v1/agent/conversations/{conversation_id}?include=report`，正文在 `data.result`；初次完整查询已返回报告时无需再读 | `report_state=READY` 后按 `format` 展示报告；`PARTIAL` 和 `INCONCLUSIVE` 也正常展示 |
| 提供 ZIP 下载 | 先展示 JSON 报告；`archive.updated=READY` 后允许用户确认下载 | 提示“包含原始目标日志”；没有用户请求就不下载 ZIP 或审计包 |

请求只需包含 `request_id`、`text`、`attachment_ids`。每个新逻辑请求生成新 ID，网络重试时保持 ID 和内容不变。可直接使用的完整请求和响应示例，以及文件哈希、请求头说明，见 [API 参考](website-agent-api.md)。附件仅支持现有的压缩日志格式，不支持截图、PDF 或其他任意文件。

首次提交建议先上传一个日志包，再将问题文本和该包的 `attachment_ids` 一起发送。也支持先发文字、后发附件，后台会先处理文字中尚未提取的参数。当前一次诊断使用一份日志归档；选择多个日志包时，请按提示合并，或发送新消息，只引用所需的一个附件。上传状态变为 `READY` 后，仍须发消息引用该附件，系统才会采用。

提取参数时不会读取日志正文，请将已知的问题时间、进程和槽位等信息写在问题文本中。完整时间应包含明确时区，如 `2026-09-16 10:00:00+08:00`。

“实时输出”指服务端发布的阶段消息和追问，**不包含模型逐 token 输出或内部推理**。`result.available` 也不包含报告全文。报告必须从正式产物读取，不能用进度文案或旧 `methods_result` 拼接。

基础 SSE 只发送单行 `data:` 业务帧，不发送 `event:`、`id:` 或 `retry:` 行；`type` 和 `sequence` 保留在 JSON 内。连接注释 `: connected` 和心跳注释不会触发 `onmessage`。网站统一接收 `message`，再按 JSON 的 `type` 显示追问、进度或报告通知；不要按命名事件注册监听器，也不要等待 `[DONE]` 或按 OpenAI `choices` / `delta` 解析。完整前端示例见 [API 参考](website-agent-api.md)。

刷新时先查询会话，从 `history` 恢复最近的消息、追问和结果卡片，从 `result` 显示选中轮次的报告，再回放事件并去重。更早的记录使用 `history_next_cursor` 向前分页加载。仅检查状态时，使用 xiaodao 接口 `/api/v1/agent/conversations/{conversation_id}?include=none`，避免重复加载历史或报告。`PENDING` 和 `UNAVAILABLE` 均为正常 `200` 响应；仅在 `READY` 时渲染正文。报告成功显示后，按 `conversation_id + run_id` 缓存，并合并重复的读取请求。

响应不含 `id:`，原生 `EventSource` 自动重连时不会携带业务游标，会重新回放历史。需要准确续传时，用流式 `fetch` 手动设置 `Last-Event-ID`，传入最后**处理成功**的序号。不能仅凭快照中的最大序号，就认定此前所有事件都已展示。事件必须串行处理；报告加载失败时应提供重试入口，随后到达的完成事件不能覆盖该错误提示。

收到 `agent.failed` 或 `conversation.interrupted` 时，先读取会话快照并展示 `failure`，再更新游标。`failure` 提供可安全展示的错误码、实际阶段和稳定的 `diagnostic_id`；刷新页面时也读取这些信息。若 details 中是 `ARCHIVE_STATUS_COMMIT / persistence=UNKNOWN`，应提示“报告已生成，但归档状态暂时无法确认”，并继续获取正式 JSON 报告，不能将其显示为任务失败或自行生成完成事件。

`result.available` 不代表归档完成：JSON 已就绪、但 ZIP 仍为 `PENDING` 时，应继续订阅。`conversation.completed` 只表示事件中 `run_id` 对应的轮次结束；旧轮次的归档或完成事件不能覆盖新轮次的状态。断开 SSE 不会取消后台任务。

停止按钮调用 `conversations.stop(id, {request_id, run_id})`，先显示 `CANCELLING`，等快照中的状态变为 `CANCELLED` 后再显示已停止。用户明确提交新问题时，在同一会话中开启新一轮；如需复用日志，请在请求中传入已有的 `attachment_ids`。

## 5. 网站界面至少覆盖这些情况

| 状态或问题 | 网站行为 |
| --- | --- |
| `WAITING_INPUT` | 展示 `current_questions`，允许继续输入和补日志 |
| 消息 `QUEUED` / `UNUSED` | 显示服务端 `notice`，不将消息显示为已用于诊断 |
| Case `UNRESOLVED` | 展示正式 `INCONCLUSIVE` JSON、证据缺口和限制；没有结果 ZIP |
| 归档 `PENDING` / `FAILED` | 显示“日志包生成中”/“日志包生成失败”；已交付 JSON 仍有效 |
| 当前轮 `FAILED` / `INTERRUPTED` | 展示快照 `failure` 中可安全公开的错误码、阶段和诊断关联 ID；用户明确提交新问题后开启新轮，不自动重新调用模型 |
| 当前轮 `CANCELLING` / `CANCELLED` | 分别显示“正在停止”/“已停止”；按 `capabilities` 控制输入与再次诊断按钮，不作为诊断失败 |
| 删除请求已受理 | 收到 `DELETING` 后从侧栏移除并关闭本地订阅；服务端立即拒绝后续访问，并在后台安全清理数据 |
| 归档交付 `persistence=UNKNOWN` | 保留 `RUNNING / PENDING` 和已发布 JSON，显示归档状态暂时无法确认；临时提示不写入历史 |
| SSE 断线 | 显示连接状态；`EventSource` 重连后回放历史并去重，或用 `fetch` 携带最后处理成功的游标续传；不能因为断线就新建诊断任务 |
| HTTP `409` | 区分幂等冲突、正在停止或目标轮次变化；不要在原因未明时换 ID 重发原诊断请求 |
| 报告校验失败 | 不展示未校验内容，提示重新获取报告；保留已接收消息和会话 |

## 6. 联调验收清单

- [ ] 从网站后端机器确认服务版本、就绪状态、Agent 创建和查询都正常。
- [ ] 仅提供非空问题文本也能先创建 Case，未提供预期行为或范围时不会阻塞；随后按 OPEN requirements 的原文提示补充信息、上传日志并完成定位，最终显示报告。
- [ ] 日志上传同时核对类型、字节数和 SHA-256；同一预约、消息重试不重复创建。
- [ ] SSE 每条业务消息只有一行 `data:` 加空行，`onmessage` 能实时收到；断线重放、手动游标续传、刷新和重复事件不丢报告、不重复消息。
- [ ] JSON 不等待 ZIP；ZIP 仅在确认后下载，下载前完成大小和 SHA-256 校验。
- [ ] 下载响应缺少 `Content-Length` / `X-Content-SHA256` 仍核对真实字节数和 SHA-256；提供这些头时必须匹配。
- [ ] 失败事件和刷新均读取快照 `failure`；报告已生成但归档 UNKNOWN 时，首次读取 JSON 仍可成功。
- [ ] 未登录用户、其他用户不能查询会话、订阅、上传或下载；修改 UUID 不能越权。
- [ ] 停止后可在原会话明确发起新轮；旧消息、停止请求重放和旧轮归档事件不会影响新轮；历史报告仍可按 `run_id` 读取。
- [ ] 重命名、目录分页和历史分页不触发模型；删除后立即不可访问，后台清理中断后仍会继续，旧请求不能重建会话。
- [ ] 经测试负责人安排，在无其他受影响用户时做重启检查：历史保留，活动任务明确中断，不自动重跑；完成报告仍可读取，待归档任务按原机制恢复。
- [ ] 保留部署身份、会话 ID、Case ID、事件序号、产物大小/哈希和正式 Test Flow verdict；手工联调与正式发布结论分开记录。

通过这些检查后再开放小范围试用；“Linux 服务已启动”“OpenAPI 能打开”或“确定性测试通过”都不能单独代替部署环境的端到端验收。

早期的 `8.0.0` 预览版曾调整 SSE 传输格式：接入方需统一使用 `onmessage`，并自行管理已处理事件的游标。那次调整没有改变 V11 数据规范。本次 `8.2.0` 保留相同的 SSE 帧格式，公开事件升级为 `schema_version=2`，并包含 `run_id`。State V11 / `v11-contract-r2` 和报告 schema 3 不变，Agent 存储版本升级为 2。已有 r1 或 r2 数据须按副本升级说明手动升级，并从原网站的用户归属库导出 `--ownership-map`；归属不明的数据不会自动分配给用户。

网站 API 分为会话和附件两组接口。会话完整响应为 `schema_version=3`，默认包含 `history,report,artifacts`；`include=none` 仅返回状态，`include=report` 仅附带报告，`include=artifacts` 仅附带下载信息。未加载的部分为 null，`included` 会标明本次加载了哪些部分，页面不能因此清空已有内容。预约附件统一使用 `POST /api/v1/agent/attachments`，在 JSON 中添加 `conversation_id`。旧的独立查询路径和会话下的预约附件路径已删除，前后端需一起更新。旧历史事件和不可变产物的原始字节会保留，读取时按新规范转换响应。

会话管理统一使用 `conversations.list`、`rename`、`stop`、`delete`。`current_run` 是当前轮，`capabilities` 控制按钮；当前轮结束后再发送新问题即可重新诊断。历史只读取 `history`，结果摘要按 `run_id` 打开报告。历史默认 50 条，目录默认 20 条，两者最多 100 条。删除返回 `DELETING` 时数据已对新请求隐藏，后台仍在清理文件。
