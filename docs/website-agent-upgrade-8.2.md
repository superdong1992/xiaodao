# 网站升级到 xiaodao 8.2：前端必改清单

本文供已接入 8.1 或会话接口精简过渡版的网站升级使用。**本次包含破坏性变更，前端、网站后端和 xiaodao 服务端必须一起升级。** 浏览器仍只使用“会话、附件”两个抽象；不需要 MCP 客户端。

完整字段以 [Agent API 参考](website-agent-api.md)和实际部署的 `/openapi.json` 为准。可复用的代码在 [browser-client.js](../examples/website-agent/browser-client.js)、[报告渲染模块](../examples/website-agent/report-view.js)和[网站示例](../examples/website-agent/README.md)。

## 1. 先对齐版本与路由

| 项目 | 8.2 的要求 |
| --- | --- |
| 服务版本 | `8.2.0` |
| 会话详情 | `schema_version=3`；旧正式版为 1，精简过渡版为 2 |
| 公开 SSE 事件 | `schema_version=2`，每条事件含 `run_id`；帧格式不变 |
| 专有 JSON 报告正文 | schema 3，不需要重新设计报告字段；通用 Markdown 和历史 Generic V1 仍按 `format` 区分 |
| 核心数据 | State V11 / `v11-contract-r2` 不变；Agent 存储升级到 2 |
| 浏览器请求前缀 | 网站示例为 `/api/agent`，使用网站同源地址 |
| 网站后端请求前缀 | xiaodao 原生接口为 `/api/v1/agent`，保留部署配置的路径前缀 |

下表省略前缀，`{id}` 表示会话 ID。旧路径已删除，没有兼容别名。

| 旧调用 | 新调用 | 前端读取位置 |
| --- | --- | --- |
| `GET /conversations/{id}/status` | `GET /conversations/{id}?include=none` | 轻量状态、`current_run`、`capabilities` |
| `GET /conversations/{id}/report` | `GET /conversations/{id}?include=report` | `data.result`，不再把整个 `data` 当报告 |
| 网站 `GET /conversations/{id}/artifacts` | `GET /conversations/{id}?include=artifacts` | `data.artifacts` |
| 网站 `GET /conversations/{id}/artifacts/{artifact_id}/content` | `GET /conversations/{id}/files/{artifact_id}/content?run_id={run_id}` | 原始文件字节；使用会话返回的同源下载入口 |
| `POST /conversations/{id}/attachments` | `POST /attachments` | 请求体增加 `conversation_id`，其他文件元数据按现有合同 |

`PUT /attachments/{attachment_id}/content` 仍上传原始文件字节。创建、消息和 SSE 的路径保持不变，响应需按新合同解析。网站页面不再串行请求 Case、报告和产物列表。

新增会话管理操作：

| 操作 | 请求 | 返回后如何更新页面 |
| --- | --- | --- |
| 历史目录 | `GET /conversations?limit=20&cursor=...` | 使用 `items`；将 `next_cursor` 原样传回，首次不传 cursor |
| 重命名 | `PATCH /conversations/{id}`，JSON：`{"title":"新的标题"}` | 用返回的摘要更新侧栏和页面标题；标题为 1 到 80 个字符 |
| 停止 | `POST /conversations/{id}/stop`，JSON：`request_id`、`run_id` | 区分 `CANCELLING`、`CANCELLED`、`ALREADY_FINISHED` |
| 删除 | `DELETE /conversations/{id}`，无请求体 | 收到 `DELETING` 或 `DELETED` 后立即移除本地会话视图 |

目录默认 20 条，最多 100 条。按服务端顺序展示，用会话 ID 去重，不自己计算 offset；列表项已有标题、当前轮和操作权限，不要为每一项再请求详情或报告。

## 2. 用新的会话结构恢复页面

| 字段 | 必须调整的行为 |
| --- | --- |
| `title` | 侧栏和页面标题以服务端值为准；默认标题从首条问题确定性截取，不调用模型 |
| `current_run` | 当前诊断轮次；按钮和当前执行状态使用它，不根据 Case ID 猜测轮次 |
| `capabilities` | 使用 `can_send`、`can_stop`、`can_rediagnose`、`can_rename`、`can_delete` 控制操作 |
| `selected_run_id` | 本次选中查看的轮次；不传查询 `run_id` 时为当前轮 |
| 顶层 `status`、`case_id`、`result`、`artifacts` | 属于选中轮次；查看旧报告时不应据此覆盖当前轮的执行状态 |
| `history` | 替代旧 JSON 字段 `messages`；统一恢复用户消息、历史追问和诊断结果卡片 |
| `history_next_cursor` | 将其作为下一次请求的 `history_before`，向前加载更早历史 |
| `included` | 只合并本次实际加载的部分；未加载的 `null` 不得清空已展示内容 |

历史条目按 `type` 分派：`user.message` 读取 `message`，`assistant.question` 读取 `questions`，`diagnosis.result` 读取 `result`。每项含稳定的 `id` 和 `run_id`；按 `id` 去重，按 `run_id` 放到对应轮次。结果卡片既包括报告，也包括失败、停止和中断记录，不能假定所有卡片都有报告。

历史默认最近 50 项，最多 100 项；每页按时间正序返回，更早页应插到现有列表前。`run_id` 只选择状态、报告和产物，**不会把 `history` 过滤成单轮历史**。`current_run` 和 `capabilities` 始终描述当前轮，即使页面正在查看旧报告。

首次打开或刷新查询完整会话；持续状态更新用 SSE 或 `include=none`。`include=report`、`include=artifacts` 可组合，HTTP 参数是逗号分隔字符串。SDK 的 `include: []` 会自动编码为 `include=none`；SDK 方法已经返回 `data`，不要再取一次 `.data`。

## 3. 停止、删除和再次诊断

停止按钮在点击时保存 `{request_id, run_id: current_run.run_id}`，网络重试沿用原值，不能改成届时的新轮。即使正在看旧报告，也不能把 `selected_run_id` 当作停止目标。

- `CANCELLING`：显示“正在停止”，继续读取状态；不要提前显示“已停止”，也不要提交下一轮。
- `CANCELLED`：显示“已停止”，不作为诊断失败。用户可按 `capabilities` 再次诊断。
- `ALREADY_FINISHED`：目标轮已结束，重新读取该轮快照，按真实状态显示完成、失败或中断。已有 READY 报告时保留，归档可能仍在继续；这个收据不保证目标轮成功或有报告。

删除成功受理后，从侧栏移除会话，离开其详情页，关闭 SSE，取消本地尚未完成的读取，清除该会话的本地缓存。`DELETING` 表示访问已撤销、后台还在清理，不需要等待文件清理完成才隐藏。删除可按同一会话 ID 重试；收到 404 后不使用旧创建请求自动补建会话。已经离开页面的迟到响应也不得重新插入该会话。

同一会话可以保留多轮诊断，每轮使用独立 Case。当前轮结束后，用户明确发送**完整的新问题**，服务端才创建下一轮；`can_send=true` 在终态也可能成立，并不代表会恢复旧任务。再次诊断使用新的消息 `request_id`，不会自动沿用旧模型结论或事实。

消息请求仍只有 `request_id`、`text`、`attachment_ids`，**不要添加 `run_id`**。受理回执中的 `run_id` 是该消息的固定目标。旧消息重放仍返回旧轮回执，不能把它当作新一轮已受理。

再次诊断可让用户明确勾选已有 `READY` 或 `IMPORTED` 附件，把其 `attachment_id` 放入新消息，无需重复上传。不要默认选中所有旧附件，也不要根据旧 `case_attachment_id` 判定新轮已经采用日志。上传到 READY 后仍须发消息引用；仅上传不触发诊断。`APPLIED` 表示消息已被业务采用，不等于其中所有 Skill 参数都已提取。

## 4. SSE 必须按轮次处理

事件 schema 升为 2，新增 `run.started` 和 `run.stopping`。事件 URL 不接受 `run_id` 查询参数；会话内所有轮次共用事件流，`sequence` 在整个会话内递增，切换轮次不能将去重序号归零。

1. 使用 `onmessage` 接收单行 `data: <JSON>` 帧，再按 JSON 的 `type` 分派；不等待命名事件、`[DONE]` 或逐 token 输出。连接与心跳注释不作为业务事件。
2. 每条事件按 `run_id` 更新对应卡片。迟到的旧轮进度、报告、归档和完成事件不能覆盖当前轮，也不能关闭新轮订阅。
3. 当前轮 `result.available` 到达后，以该事件的 `run_id` 查询 `include=report`；旧轮事件只更新卡片，不自动下载全部历史报告。报告请求和缓存按 `conversation_id + run_id` 区分，并合并重复请求。
4. `agent.failed`、`conversation.interrupted` 需要读取相应轮次快照中的受控 `failure`。不要将旧轮失败当成当前轮失败；已有历史快照的信息无需逐事件重复加载。
5. 串行完成事件处理，成功后才推进游标。需要精准续传时用流式 `fetch` 携带 `Last-Event-ID`；帧没有 `id:` 行，原生 `EventSource` 不会自动携带业务游标。
6. 当前轮 `conversation.completed` 后可关闭本轮订阅；再次诊断受理后重新恢复快照并建立订阅。断流、关页或取消浏览器 fetch 都不等于停止诊断。

完整会话查询只恢复最近一页历史。不要把 `last_event_id` 直接当作“所有历史已经展示”，也不要因回放再次下载已经展示成功的不可变报告。

## 5. 报告、下载和错误处理

报告仍使用固定字段：先判断 `result.report_state`，`PENDING` 为等待、`UNAVAILABLE` 为本轮没有报告、`READY` 才展示正文；三者都可能是正常 HTTP 200。JSON 报告的 `COMPLETED`、`PARTIAL`、`INCONCLUSIVE` 都是可展示结果。按 `format` 读取 `report` 或 `markdown`，无需另建 `/report` API。

顶层 `report_state=READY` 与 `result=null` 可以同时出现，表示这次只请求了轻量状态，没有加载报告正文。ZIP 处于 PENDING、FAILED 或交付状态未知时，不撤回已展示报告；失败字段也不能把已就绪报告覆盖为空白。

打开历史结果卡片时：

```javascript
const detail = await client.conversations.get(conversationId, {
  include: ["report", "artifacts"],
  run_id: historyEntry.run_id,
});
renderReport(reportContainer, detail.result);
```

文件下载保留所选 `run_id`。采用示例 BFF 时，JSON 中的下载入口已经是网站同源路径；结果 ZIP 点击后在同一路径添加 `download=archive&acknowledge_raw_logs=true`，审计包添加 `download=audit`。先显示原始日志提示并取得用户确认，不在收到事件时自动下载 ZIP。网站后端继续校验归属、来源、类型、实际大小和 SHA-256；浏览器不按响应中的内网 URL 直接请求服务器。

SDK 抛出的 `AgentApiError` 保留 `status`、`code`、`message`、`details`、`retryable`。页面保留安全错误结构，不将所有失败折叠成“诊断失败”。网络结果不确定时先查询已有会话，提交重试保留原 ID 和内容；终态错误不自动重试模型。只把动态文本作为文本渲染，Markdown 过滤不安全 HTML 和链接。

## 6. 网站后端与部署方必须同步完成

原生 Agent 请求必须携带 `X-Agent-Owner-Key`。网站后端从固定 `WEBSITE_OWNER_NAMESPACE` 和已认证用户的稳定 ID 计算 `SHA-256(JSON.stringify([namespace, user.id]))`。浏览器不得自报此头或用户归属，SDK 会移除该头；原生服务只应向可信网站后端开放。

认证模块由原来的多个归属回调精简为一个 `authenticate(request)`，返回稳定的 `{id}` 或未登录时的 `null`。会话目录和归属由原生服务唯一保存，不再同步写第二份目录；Cookie 登录继续使用网站既有 CSRF / Origin 校验。命名空间和用户 ID 不随部署变化。

已有 BFF 创建请求的幂等转换仍为 `SHA-256(JSON.stringify([user.id, browserRequestId]))`，不要改用 `owner_key` 重新计算。浏览器保存最初生成的请求 ID，不拿 BFF 转换后的响应 `request_id` 再创建会话。

已有数据必须先停服、备份，按[离线副本升级说明](data-upgrade-v11-r2.md)向独立目标目录升级，并通过 `--ownership-map` 显式导入原网站归属。未知归属不会分配给任何用户。不能直接改目录标记、清空旧数据或让新服务自动升级旧目录。切换前后核对实际部署版本，再同时发布前后端。

`server.mjs` 是网站后端唯一业务实现；`server.ts` 保留 Access 类型和 Node.js 24+ 启动入口。只复制 `server.ts` 不够，需同时部署其依赖文件，具体方式见[网站示例说明](../examples/website-agent/README.md)。

## 7. 前端验收清单

- 新建会话、先上传后发送、先发送后补日志均可用；不再调用已删除路径。
- 刷新可恢复用户消息、追问及结果卡片，向前分页不重复；目录列表不逐项读取报告。
- 可改名、停止和删除；CANCELLING 不伪装成 CANCELLED，停止不显示为模型失败。
- 当前轮结束后明确发新问题可开启下一轮，用户可选择复用日志，旧消息或停止请求重放不会误操作新轮。
- 查看旧报告时当前轮按钮仍正确；旧轮归档迟到不会清空新轮输入、覆盖进度或断开新轮 SSE。
- 断线、刷新和重复事件不重复下载报告；轻量查询的 null 不清空已显示内容；报告无需等待 ZIP。
- 删除后列表与页面立即移除，迟到响应不恢复会话；其他用户不能查询、上传、订阅或下载。
- GET、目录、重命名、停止和删除不发起额外模型请求；不新增自动诊断重试、任务续办或报告追问模型。

使用合成接口夹具先完成前端自动化验证；公司内网联调和真实模型验收另行安排，不把本地确定性通过描述为内网已验收。

## 8. 可直接交给前端开发的 Prompt

```text
请将当前网站接入升级到 xiaodao 8.2.0。先阅读后端仓库的 docs/website-agent-upgrade-8.2.md、docs/website-agent-api.md、examples/website-agent/README.md，并核对实际部署的 OpenAPI。直接修改现有前端和必要的网站后端适配，保持现有技术栈与登录体系。

1. 只使用会话、附件两个 API 抽象。移除旧 /status、/report、网站 /artifacts 及会话下预约附件调用，改用统一详情的 include 和独立附件接口。
2. 会话详情按 schema_version=3 接入。history 替代 messages，恢复用户消息、追问和结果卡片并支持向前分页；实现目录分页、重命名、停止、删除。按钮统一使用 capabilities。
3. 区分 current_run 和 selected_run_id。看旧报告不影响当前轮状态；停止用稳定 request_id + current_run.run_id，区分 CANCELLING、CANCELLED、ALREADY_FINISHED。删除受理后立即移除并关闭订阅，迟到响应不得恢复会话。
4. 再次诊断必须由用户明确提交完整新问题，使用新的消息 request_id，并允许显式选择已有 READY/IMPORTED 附件。消息请求只传 request_id/text/attachment_ids，不添加 run_id；旧请求重试保持原 ID 和内容。
5. SSE 按 schema_version=2、全会话 sequence 去重、事件 run_id 分流。SSE URL 不加 run_id；旧轮事件不得覆盖新轮或关闭其订阅。新轮重新订阅，断流不等于停止诊断。
6. 默认只展示当前轮报告，历史报告点击后按 run_id 查询。报告缓存按 conversation_id + run_id，轻量查询的 null 不清空已有内容；JSON 先展示，ZIP 不阻塞。保留结构化错误，不自动重跑模型。
7. 浏览器只调用网站同源 API，不设置 owner_key。网站后端从登录身份派生归属头，保留旧创建请求哈希算法，与运维确认历史归属的离线升级已完成。
8. 控制请求量：目录不做逐项详情查询，日常用 SSE/include=none，历史和报告按需读取。SDK 已返回 data，不再二次解包；文件下载使用对应轮次的同源入口。

使用确定性夹具验证完整交互、停止与完成竞争、旧轮迟到事件、删除后迟到响应、分页、刷新恢复、错误状态和用户隔离。完成后列出修改文件、测试结果及部署联调事项；不调用真实模型、不增加自由聊天、报告追问或自动续办。
```
