# 网站 Agent 后端示例

这是供网站后端集成的示例，**不是现成的聊天页面，也不包含登录系统**。先读 [快速接入与联调清单](../../docs/website-agent-quickstart.md)，接口细节查 [Agent API 参考](../../docs/website-agent-api.md)。

## 1. 启动前准备

使用 Node.js 24+，无需 npm 依赖。先确认网站后端可以访问 xiaodao 的 `/live`、`/ready` 和 `/openapi.json`，线上版本为 `8.1.0` / `v11-contract-r2`。再接入网站的认证和归属存储。

`WEBSITE_AUTH_MODULE` 指向网站自己的 `.mjs` 模块，模块须具名导出 `access` 对象，对应 [server.ts](server.ts) 中的 `Access` 类型。以下五个回调都需要实现：

| 回调 | 网站需实现的逻辑 |
| --- | --- |
| `authenticate(request)` | 校验真实登录态，返回 `{id: string}` 或 `null`；Cookie 认证还需校验 CSRF / Origin |
| `ownsConversation(user, conversationId)` | 查询当前用户是否有权访问该会话 |
| `rememberConversation(user, conversationId)` | 持久、幂等地保存创建成功的会话归属 |
| `ownsAttachment(user, attachmentId)` | 查询当前用户是否有权访问该附件 |
| `rememberAttachment(user, conversationId, attachmentId)` | 持久、幂等地保存预约成功的附件与会话归属 |

回调都是异步函数。归属写入必须成功后才能向浏览器交付创建回执；不能用重启即丢失的内存 Map 代替网站数据库。创建会话的幂等键已按认证用户划分命名空间；不得信任前端自报 `user_id`。

## 2. 从仓库根目录启动

Linux / Bash（地址和模块路径替换为实际值）：

```bash
export XIAODAO_BASE_URL='http://xiaodao.internal:8000'
export WEBSITE_AUTH_MODULE='/opt/website/xiaodao-access.mjs'
node examples/website-agent/server.ts
```

Windows / PowerShell：

```powershell
$env:XIAODAO_BASE_URL = 'http://xiaodao.internal:8000'
$env:WEBSITE_AUTH_MODULE = 'D:\website\xiaodao-access.mjs'
node examples/website-agent/server.ts
```

示例固定监听 `127.0.0.1`，默认端口 `8787`；`PORT` 只改端口。将网站同源 `/api/agent/` 反向代理到此服务，或把导出的 `createAgentBackend` 接入既有 Node HTTP 后端。SSE 代理关闭响应缓冲，读取超时大于 15 秒心跳间隔，例如 60 秒；上传代理保留原始字节、长度和校验请求头。不要直接把监听地址改为公网来绕过网站授权。

未配置认证模块时，进程可启动，但业务请求全部返回 `401`，这是安全默认行为。模块路径建议使用绝对路径；相对路径以启动目录为准。不要为了联调删除授权检查。

## 3. 网站调用哪些路径

这里的 `/api/agent` 是**网站同源路径**；xiaodao 上游使用 `/api/v1/agent`。原生服务已提供会话 `/status` 和 `/report`，本例分别用一次上游请求获取状态或报告，再补上网站授权和中文展示结构。只向配置的 `XIAODAO_BASE_URL` 发请求；原始产物的下载路径由已经核验的 Case 和产物 ID 构造，上游返回的 `download_url` 不参与寻址。因此网站内部地址可以与服务端 `PUBLIC_BASE_URL` 不同。上传地址会改写为网站同源路径。

收到非空问题原话后，服务端按 MCP 客户端的固定中性模板创建 Case，初始事实为空。网站不应先要求用户填写预期行为、范围或日志；创建前不调用 INTAKE。建案后只按原文展示 OPEN requirements 的追问，并用消息接口提交回答。没有 OPEN requirements 时不额外追问。

- 创建、发送消息、查询、订阅、预约和上传：使用 `/api/agent/...` 对应路径，详见 [API 参考](../../docs/website-agent-api.md)。
- `GET /api/agent/conversations/{id}/status`：获取状态、追问、失败原因和 `report_state`，不返回完整消息及附件历史；适合状态轮询。初次打开和刷新页面仍用会话查询恢复历史。
- `GET /api/agent/conversations/{id}/report`：收到 `result.available` 或恢复状态发现报告就绪时调用，返回正式 JSON、Generic Markdown 或历史 Generic V1 结果。`READY` 时可展示；`PENDING` 表示仍在诊断或等待补充；`UNAVAILABLE` 表示已结束且没有报告。三者都返回 HTTP 200，只有 `READY` 含报告内容。JSON 报告保留现有 `format`、`report`、`sections` 字段。
- `GET /api/agent/conversations/{id}/artifacts`：查询授权后的产物，下载地址已改为网站同源路径。
- 结果 ZIP：用户确认包含原始目标日志后，给相应下载路径添加 `?download=archive&acknowledge_raw_logs=true`。
- 审计包：只按用户请求下载，给相应下载路径添加 `?download=audit`。

`/report` 复用原生服务已发布且校验完成的报告，不再查询 Case 或下载产物，不重新生成报告或调用模型。报告原始内容最多 16 MiB；JSON 转义会扩大传输内容，因此此接口的上游响应限额为 `6 × 16 MiB + 64 KiB`，其他 JSON 接口仍为 16 MiB。`PARTIAL` 和 `INCONCLUSIVE` 都属于可展示的 `READY` 报告。归档仍在处理或已经失败时，报告也可以立即展示。

原始产物继续走 `/artifacts/{id}/content`。不超过 16 MiB 的 JSON 和 Markdown 用有界内存校验；ZIP 及更大产物写入唯一临时文件，核对实际字节数和 SHA-256 后再转发，结束后清理。响应可省略 `Content-Length` 和 `X-Content-SHA256`；提供时必须匹配。下载不接受重定向。ZIP 只在用户主动确认后下载。测试使用假上游，不调用真实模型。

SSE 原样转发基础单行帧：每条业务消息是 `data: <完整 AgentEvent JSON>` 加空行，不包含 `id:`、`event:`、`retry:` 行。前端只需 `onmessage`，从 JSON 的 `type` 分发，从 `sequence` 去重。连接和心跳注释不会触发业务消息；结束使用 `conversation.completed`，没有 `[DONE]` 或 OpenAI `choices` / `delta` 包装。进度仍是已公开的阶段消息和追问，不转发模型内部推理或未审核报告。

原生 `EventSource` 不会从 data-only 响应记录业务游标，自动重连会重放历史。需要精准续传时，前端用流式 `fetch` 手动设置最后处理成功的 `Last-Event-ID`，网站后端将此请求头转发给上游。事件应串行处理，报告下载、校验和展示成功后才推进游标；完整有界队列和失败重试示例见 [API 参考](../../docs/website-agent-api.md)。

页面恢复和历史事件回放可能同时触发报告读取。前端应合并同一会话尚未完成的读取；报告展示成功后保留结果，重复的就绪事件不再下载。`PENDING` 和读取失败不能缓存成已就绪，下一次就绪通知或用户重试仍须重新查询。

收到 `agent.failed` 或 `conversation.interrupted` 后，重新读取会话快照并展示 `failure`。其中 `code` 是安全错误码；`details` 提供实际失败阶段和稳定的诊断关联 ID，供服务端查证。页面刷新也从快照恢复错误，不从 SSE 的固定错误文案猜测原因。终态任务不会因为重新查询或上传重试而重新调用模型。

`failure.details` 若含 `persistence=UNKNOWN`，表示当前进程尚不能确认交付状态。归档状态写入失败时，快照仍保留 `RUNNING / PENDING`，页面提示“报告已生成，但归档状态暂时无法确认”，已发布的 JSON 仍可读取和展示。这条临时提示不写入会话历史，不代表诊断失败，也不生成完成事件。

已接入早期 `8.0.0` 预览版的网站需要把命名事件监听改成 `onmessage`，不再读取 `lastEventId`。历史上的那次传输调整没有改变 V11 数据合同。本次 `8.1.0` 使用 `v11-contract-r2`，SSE 仍为 `schema_version=1`，但已有 r1 数据必须按[副本升级说明](../../docs/data-upgrade-v11-r2.md)显式升级；原数据和历史报告保留，不能直接修改旧目录的合同标记。

## 4. 本地自检与环境联调分开

从仓库根运行示例自检，不访问已部署服务，不调用模型：

```bash
node --test examples/website-agent/server.test.mjs
```

示例自检通过不代表 Linux 环境验收通过。真实发消息前，按 [Test Flow 说明](../../tools/test-flow/README.md)查看对应 `--plan-only` 身份和预算，再按 [联调清单](../../docs/website-agent-quickstart.md)验证追问、SSE、报告、权限和重启恢复。
