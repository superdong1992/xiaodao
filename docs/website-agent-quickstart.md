# 网站 Agent 接入：部署后的第一轮联调

面向网站前后端开发和 Linux 测试环境运维，适用于 xiaodao `8.0.0` / V11。目标是跑通一次“用户原话 → 创建任务 → 按要求补充 → 日志 → 实时进度 → 具体定位报告”。本文不代表你的测试服务已通过验收。

网站保留原有问答，新建一个 Agent 入口。调用关系是：**浏览器 → 网站后端 → xiaodao**。网站不用安装 MCP 客户端，也不用把用户原话加工成 `problem_spec`。

## 1. 先把这几项交给网站开发人员

| 交接项 | 需要提供什么 |
| --- | --- |
| 测试服务地址 | 网站后端可访问的完整地址、端口和路径前缀；不要把内部地址直接暴露给浏览器 |
| 部署版本 | 应为 `8.0.0`，同时记录部署的 commit 或源码快照；不能仅凭进程启动判断版本 |
| 接口合同 | 在线 `GET /openapi.json`；可视化入口 `GET /docs`；仓库 [OpenAPI 快照](../schemas/v2/web-api.openapi.snapshot.json) |
| 完整说明 | [Agent API 参考](website-agent-api.md)：请求响应、附件、SSE、字段、错误与报告校验 |
| 网站后端示例 | [TypeScript 示例和启动说明](../examples/website-agent/README.md)：登录/归属回调、上传、SSE、报告下载 |
| 联调样本 | 经批准可用于测试的真实问题和配套压缩日志；时间、环境等信息按任务要求补充，预期行为不作为建案前置条件，不要用虚构事实补齐追问 |
| 网络和运行约束 | 允许访问的后端来源、反向代理配置、上传限制、模型调用预算、测试负责人 |

在线合同以**实际部署的服务**为准。如果在线版本、路由与本仓库不同，先对齐部署，不要让网站适配旧版本。示例的 `XIAODAO_BASE_URL` 与服务端 `PUBLIC_BASE_URL` 应保持相同的地址和路径前缀，否则下载地址校验会拒绝请求。

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
2. `/ready`：HTTP 200，`ok=true`、`data.ready=true`。失败时先由运维处理返回的错误码。它检查服务就绪条件，**不证明模型或诊断全链路已经可用**。
3. `/openapi.json`：直接返回 OpenAPI 文档，不套 `ok/data` 信封；`info.version` 为 `8.0.0`，`paths` 中有 `/api/v1/agent/conversations` 及消息、事件、附件路由。

Swagger 页面位于服务的 `/docs`。如果内网资源加载受限或代理加了路径前缀，页面可能打不开；当前页面从根路径 `/openapi.json` 加载合同，可先直接读取 OpenAPI。不要因此误判业务 API 不可用，也不要为了打开文档放开公网访问。

路由存在只证明接口已注册。接下来创建一个空会话并查询它，确认 Agent 服务实际接入；这会持久保存会话，但不会调用模型。在网站后端先完成登录和归属接入，再用自己的同源接口测试。直接检查上游时，创建请求为：

```http
POST /api/v1/agent/conversations
Content-Type: application/json

{"request_id":"website-smoke-20260907-001"}
```

保存响应的 `data.conversation_id`，调用 `GET /api/v1/agent/conversations/{conversation_id}`；应能读回同一会话。重试沿用同一 `request_id`，下一次独立测试换新 ID。`503 / AGENT_UNAVAILABLE` 表示 Agent 服务未就绪，不能靠前端重试解决。不要删除已有 `DATA_ROOT` 来排查；8.0 需要新空 V11 数据根，历史数据必须保留。

## 3. 网站前后端分别做什么

| 负责方 | 首版必须完成 |
| --- | --- |
| 网站后端 | 验证登录；持久保存用户与会话、附件的归属；每次查询、上传、订阅、下载都检查权限；转发 SSE 和文件；校验报告来源、大小和 SHA-256 |
| 网站前端 | 提供输入框、压缩日志上传、追问、消息采用状态、阶段进度、报告区和下载按钮；刷新后恢复历史；按事件序号去重 |
| xiaodao 运维 | 配置问题整理和诊断角色、日志解析与可选审核；确认模型身份和预算；限制服务可达来源；保证 SSE 不被代理缓冲 |

网站前端访问网站自己的 `/api/agent/...`（示例路径）；网站后端访问 xiaodao 的 `/api/v1/agent/...`。两者不要混用。UUID 不是授权凭据；网站不得相信前端自报的 `user_id`。示例没有内置登录系统，未接入授权回调时返回 `401` 是预期行为。

## 4. 按这个顺序跑通第一条旅程

发送消息会触发真实模型。先由测试负责人按[官方 Test Flow 说明](../tools/test-flow/README.md)审查对应 `--plan-only` 的身份、调用数、模型预算、预计 token/cost、admission blocker，以及 Proof、Stage、Gate 和复用决定，再开展真实模型验证。手工联调记录不能替代官方 `verdict.json`。

| 步骤 | 网站调用与展示 | 成功标志 |
| --- | --- | --- |
| 创建会话 | `POST /api/v1/agent/conversations`，保存用户归属 | 拿到 `conversation_id` |
| 订阅进度 | `GET /api/v1/agent/conversations/{conversation_id}/events` | `text/event-stream`；每条业务消息为一行 `data: <JSON>` 加空行，`onmessage` 可直接接收；空闲每 15 秒有注释心跳 |
| 发送原话 | `POST /api/v1/agent/conversations/{conversation_id}/messages` | 立即收到 `ACCEPTED` 回执；非空问题文本按 MCP 固定中性模板创建 Case，初始事实为空，创建前不调用 INTAKE、不追问预期行为 |
| 回答追问 | Case 创建后按原文展示 OPEN requirements 的 `assistant.question`，仍调用同一消息接口回答 | 仅补充任务当前要求；没有 OPEN requirements 就不追问，不要求网站生成诊断字段 |
| 补充日志 | 预约附件 → 按描述符 PUT 原始字节 → 发消息引用 `attachment_ids` | 上传为 `READY`；消息是否被采用另看 `APPLIED` / `notice` |
| 显示进度 | 展示 `agent.progress`，同步 `case.updated` | 能看到实际执行阶段；审核前没有根因或 Candidate 报告 |
| 展示报告 | 收到 `result.available` 后查 Case 和产物列表，下载并校验 JSON；采用示例时调用网站 `/report` | 页面展示具体结论、依据、完成条件、限制和建议，不只是“定位完成” |
| 提供 ZIP | JSON 先展示；`archive.updated=READY` 后允许用户确认下载 | 提示“包含原始目标日志”；没有用户请求就不下载 ZIP 或审计包 |

输入只需 `request_id`、`text`、`attachment_ids`；每个新逻辑请求生成新 ID，网络重试保持 ID 和内容不变。完整可复制的请求/响应、文件哈希和请求头见 [API 参考](website-agent-api.md)。附件仅支持现有压缩日志格式，不是截图、PDF 或任意文件上传接口。

“实时输出”是服务端发布的阶段消息和追问，**不是模型逐 token 输出或内部推理**。`result.available` 也不是报告全文。报告必须从正式产物获取，不从进度文案或旧 `methods_result` 拼接。

基础 SSE 只发送单行 `data:` 业务帧，不发送 `event:`、`id:` 或 `retry:` 行；`type` 和 `sequence` 保留在 JSON 内。连接注释 `: connected` 和心跳注释不会触发 `onmessage`。网站统一接收 `message`，再按 JSON 的 `type` 显示追问、进度或报告通知；不要按命名事件注册监听器，也不要等待 `[DONE]` 或按 OpenAI `choices` / `delta` 解析。完整前端示例见 [API 参考](website-agent-api.md)。

刷新时先查询会话快照，恢复消息、追问、附件和报告状态，再回放事件并去重。响应不含 `id:`，原生 `EventSource` 自动重连时不会携带业务游标，会重新回放历史。需要精准续传时，用流式 `fetch` 手动设置 `Last-Event-ID`，使用最后**处理成功**的序号；不要直接把快照的最大序号当作所有事件都已展示。处理事件要串行；报告加载失败时提供重试，不能被随后到达的完成事件掩盖。

`result.available` 不等于归档完成：JSON 已就绪但 ZIP 为 `PENDING` 时继续订阅。只在 `conversation.completed` 后结束正常订阅。断开 SSE 不会取消后台任务；首版不提供停止按钮。已经完成报告的新问题另建会话。

## 5. 网站界面至少覆盖这些情况

| 状态或问题 | 网站行为 |
| --- | --- |
| `WAITING_INPUT` | 展示 `current_questions`，允许继续输入和补日志 |
| 消息 `QUEUED` / `UNUSED` | 显示服务端 `notice`，不冒称消息已经用于诊断 |
| Case `UNRESOLVED` | 展示正式 `INCONCLUSIVE` JSON、证据缺口和限制；没有结果 ZIP |
| 归档 `PENDING` / `FAILED` | 显示“日志包生成中”/“日志包生成失败”；已交付 JSON 仍有效 |
| 会话 `FAILED` / `INTERRUPTED` | 展示安全错误或中断说明，由用户明确另建会话；不自动重跑模型 |
| SSE 断线 | 显示连接状态；`EventSource` 重连回放历史并去重，或用 `fetch` 携带最后成功游标续传；不能因为断线就新建诊断任务 |
| HTTP `409` | 区分幂等冲突或会话已结束；不要换 ID 盲重发原诊断请求 |
| 报告校验失败 | 不展示未校验内容，提示重新获取报告；保留已接收消息和会话 |

## 6. 联调验收清单

- [ ] 从网站后端机器确认服务版本、就绪状态、Agent 创建和查询都正常。
- [ ] 仅有非空问题原话也能先创建 Case，未提供预期行为或范围不会阻塞；随后按 OPEN requirements 原文完成补充、日志上传和定位，最终显示具体报告。
- [ ] 日志上传同时核对类型、字节数和 SHA-256；同一预约、消息重试不重复创建。
- [ ] SSE 每条业务消息只有一行 `data:` 加空行，`onmessage` 能实时收到；断线重放、手动游标续传、刷新和重复事件不丢报告、不重复消息。
- [ ] JSON 不等待 ZIP；ZIP 仅在确认后下载，下载前完成大小和 SHA-256 校验。
- [ ] 未登录用户、其他用户不能查询会话、订阅、上传或下载；修改 UUID 不能越权。
- [ ] 经测试负责人安排，在无其他受影响用户时做重启检查：历史保留，活动任务明确中断，不自动重跑；完成报告仍可读取，待归档任务按原机制恢复。
- [ ] 留存部署身份、会话 ID、Case ID、事件序号、产物大小/哈希和正式 Test Flow verdict；手工联调与正式发布结论分开记录。

通过这些检查后再开放小范围试用；“Linux 服务已启动”“OpenAPI 能打开”或“确定性测试通过”都不能单独代替部署环境的端到端验收。

如果网站已经对接较早的 `8.0.0` 预览版，本次只调整 SSE 传输格式：改为统一 `onmessage` 并自行管理处理游标。事件 JSON 版本和 V11 持久合同不变，不需为这次调整重建 V11 数据根；核对实际帧格式和源码版本，不能仅凭相同版本号判断网站已适配。
