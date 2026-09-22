# 网站接入示例：先看报告，再接 API

诊断记录和附件保留 **7 天**。接入页面须提示用户及时下载，并处理历史过期与 SSE 游标重置；详见[服务端文件保留与清理](../../docs/storage-retention.md)。

已接入本示例的网站请核对 [2026-09-21～22 增量适配清单](../../docs/website-agent-changes-2026-09-22.md)，同步更新浏览器 SDK、BFF 和页面交互。

从旧版升级时，请先读 [8.2 前端必改清单与开发 Prompt](../../docs/website-agent-upgrade-8.2.md)。其中列出了旧接口的替换方式、轮次处理、停止和删除操作，以及部署时需要同步完成的事项。

本示例提供离线报告预览、可直接使用的浏览器模块和网站后端代码。建议先确认报告的展示效果，再接入网站登录、实际定位任务和日志上传。使用 Node.js 24+，无需安装 npm 依赖。

部署和联调步骤见[快速接入清单](../../docs/website-agent-quickstart.md)，请求、响应和 SSE 的详细说明见 [Agent API 参考](../../docs/website-agent-api.md)。

通用报告赞踩接口及经验复用规则见[经验库接入说明](../../docs/generic-feedback-memory.md)。按钮由网站实现，本示例提供调用封装和后端转发。

## 1. 先打开离线报告预览

从仓库根目录启动：

```bash
node examples/website-agent/preview.mjs
```

打开 [http://127.0.0.1:8788/](http://127.0.0.1:8788/)。预览只使用本地示例数据，不连接 xiaodao、不创建任务，也不调用模型。可以查看等待、完整报告、部分结果、尚无定论和失败等状态，还可试用重命名、停止、删除、开始新一轮诊断和切换历史报告等操作。这些操作只修改浏览器内的模拟数据，刷新后即可恢复。

| 文件 | 作用 | 接入网站时怎么用 |
| --- | --- | --- |
| `preview.mjs` | 启动离线预览 | 本地查看，无需部署到网站 |
| `report-view.js` | 按固定字段生成报告界面 | 复制到网站静态资源目录，调用 `renderReport` |
| `report-view.css` | 报告区样式 | 与渲染模块一起复制，也可替换为网站自己的样式 |
| `browser-client.js` | 封装浏览器对网站同源 API 的调用 | 复制后使用，或按相同接口接入现有请求库 |
| [server.mjs](server.mjs) | 统一实现网站后端的权限校验、上游请求、SSE 和产物下载 | Node.js 18+ 可导入 `createAgentBackend`，集成到现有后端 |
| [server.ts](server.ts) | 类型声明和兼容启动入口 | 使用 Node.js 24+ 执行下文启动命令，与 `server.mjs` 共用实现 |

## 2. 用最少代码显示已有报告

把 `report-view.js`、`report-view.css` 和 `browser-client.js` 放进网站的同一个静态资源目录，例如 `/xiaodao/`。后端接入同源 `/api/agent/` 接口后，在现有页面加入以下代码：

```html
<link rel="stylesheet" href="/xiaodao/report-view.css">
<p id="report-error" role="alert"></p>
<div id="diagnosis-report" aria-live="polite"></div>

<script type="module">
  import { createAgentClient } from "/xiaodao/browser-client.js";
  import { renderReport } from "/xiaodao/report-view.js";

  const client = createAgentClient();
  const container = document.querySelector("#diagnosis-report");
  const errorContainer = document.querySelector("#report-error");
  const conversationId = "替换为已保存的 conversation_id";

  try {
    const conversation = await client.conversations.get(conversationId, { include: ["report"] });
    renderReport(container, conversation.result);
    errorContainer.textContent = "";
  } catch (error) {
    errorContainer.textContent = error.message || "报告暂时无法读取，请稍后重试。";
  }
</script>
```

客户端方法成功时直接返回响应中的 `data`，失败时抛出 `AgentApiError`，不会自动重试。错误提示使用独立容器，读取或渲染失败时保留已有报告。若直接使用 `fetch`，先检查 HTTP 状态和响应中的 `ok`，再调用 `renderReport(container, response.data.result)`。读取已有报告是只读操作，不会重新诊断、重新生成报告或调用模型。

会话响应固定为 `schema_version=3`，报告位于 `result`。先判断 `result.report_state`：`PENDING` 时展示等待提示，`UNAVAILABLE` 时展示结束原因，`READY` 时才展示正文。这三种状态都返回 HTTP 200。状态为 `READY` 时，再根据 `format` 选择内容：

| `format` | 正文 | 展示方式 |
| --- | --- | --- |
| `problem-locator-diagnosis-v3` | `data.result.report` | 按结构化字段展示；`COMPLETED`、`PARTIAL`、`INCONCLUSIVE` 都有正式报告 |
| `markdown` | `data.result.markdown` | 默认以纯文本展示原文，保留换行；需要排版时可替换为网站自己的 Markdown 渲染器，并过滤不安全的 HTML 和链接 |
| `generic-v1` | `data.result.report` | 展示历史结果的 `conclusion` 和 `root_cause_analysis` |

JSON 报告使用 `conversation.result.report` 的固定字段填充组件。下表字段均位于 `result` 下；接口不再返回重复的中文 `sections`。

| 页面组件 | 固定字段 |
| --- | --- |
| 结果状态、问题描述 | `report.status`、`report.problem_statement` |
| 根因或结论 | `report.root_cause`；为 null 时保留“尚未确认”的含义 |
| 关键发现 | `report.findings` |
| 原因、候选因素、已排除因素 | `report.causal_factors`、`report.candidate_factors`、`report.excluded_factors` |
| 完成条件 | `report.completion_criteria_mapping` |
| 规则和证据 | `report.verification_rules`、`report.supporting_evidence_bindings` |
| 时间关联 | `report.time_relevance` |
| 证据缺口、限制 | `report.evidence_gaps`、`report.limitations` |
| 后续建议、注意事项 | `report.recommendations`、`report.safety_notes` |

归档状态 `archive_status` 与报告是否可读需分别处理。ZIP 仍在生成或生成失败时，报告照常展示；即使 `failure` 包含归档异常，也不能覆盖已发布的报告。

## 3. 再接创建、消息和上传

`browser-client.js` 不依赖框架或第三方库，默认调用网站同源 `/api/agent`。这些方法分别对应一个业务操作：

| 方法 | 用途 |
| --- | --- |
| `conversations.list({cursor, limit})` | 分页读取当前用户的会话列表，默认 20 条、最多 100 条 |
| `conversations.rename(id, title)` | 修改标题，1 到 80 个字符 |
| `conversations.stop(id, {request_id, run_id})` | 停止指定轮，重试沿用两个原值 |
| `conversations.delete(id)` | 删除会话，无请求体；按会话 ID 幂等 |
| `conversations.create(requestId)` | 创建会话，保存 `conversation_id` |
| `conversations.send(id, {request_id, text, attachment_ids})` | 发送问题、补充回答或已上传附件 ID |
| `conversations.get(id)` | 一次恢复状态、追问、进度、消息、附件、报告和下载信息 |
| `conversations.get(id, {include: []})` | 仅读取基本状态，不加载历史、报告或产物列表 |
| `conversations.get(id, {include: ["report"]})` | 从同一会话读取报告；也可选择 `history`、`artifacts` 或组合 |
| `conversations.getFeedback(id, runId, {signal})` | 读取指定报告的评价资格和当前投票，`signal` 可选 |
| `conversations.setFeedback(id, runId, {request_id, rating})` | 提交 `LIKE` 或 `DISLIKE`；重试保留原 ID 和内容，换票使用新 ID |
| `conversations.eventsUrl(id)` | 返回会话 SSE 路径，交给订阅和游标处理代码 |
| `attachments.prepare(id, metadata)` | 预约上传日志附件，提供会话、文件名、类型、大小和 SHA-256 |
| `attachments.upload(prepared, file)` | 上传原始 Blob/File，返回附件记录 |

按需要配置网站路径、请求库或 CSRF 头：

```javascript
const client = createAgentClient({
  basePath: "/api/agent",
  headers: () => ({ "X-CSRF-Token": readWebsiteCsrfToken() }),
});
```

`readWebsiteCsrfToken` 由网站提供；无需此请求头时省略 `headers`。默认使用浏览器 `fetch`，也可传入 `fetchImpl`。每个新逻辑请求使用新的 `request_id`，网络重试保持 ID 和业务内容不变。

示例中的 `crypto.randomUUID()` 需要 HTTPS 或 localhost 等安全上下文。公司内网若使用普通 HTTP，请改用网站已有的请求 ID 生成器，并保持上述重试规则。

创建会话并发送问题的示例如下。每次提交前，先保存问题原文和 ID，并将这份数据放在按钮回调及网络重试函数之外。重试时只需再次调用 `submitProblem()`：

```javascript
const problem = {
  createRequestId: crypto.randomUUID(),
  conversationId: null,
  message: { request_id: crypto.randomUUID(), text: problemText, attachment_ids: [] },
};

async function submitProblem() {
  if (!problem.conversationId) {
    const receipt = await client.conversations.create(problem.createRequestId);
    problem.conversationId = receipt.conversation_id;
  }
  return client.conversations.send(problem.conversationId, problem.message);
}
```

`problemText` 是用户本次从输入框提交的原文。如果需要在刷新后继续重试，网站应持久保存这份提交记录；只有提交新请求时才更换 ID。创建回执中的 `request_id` 已由网站后端按用户改写，不能用它替换原始 `createRequestId` 再次创建会话。

收到非空的问题文本后，服务端按 MCP 客户端的固定中性模板创建 Case，初始事实为空。网站不应要求用户先提供预期行为、范围或日志，才允许创建 Case；创建前也不调用 INTAKE。Case 创建后，按原文展示 OPEN requirements 中的追问。没有 OPEN requirements 时不额外追问，前端也无需生成 `problem_spec`。

附件按“预约上传 → PUT 原始字节 → 发消息引用 `attachment_ids`”的顺序提交。上传状态变为 `READY` 只表示文件可用；是否已用于诊断，需查看消息的 `APPLIED` / `notice`。以下代码中的 `file` 是用户选中的原始 File，`conversationId` 是已有会话 ID：

```javascript
const lowercaseName = file.name.toLowerCase();
const suffix = [".tar.gz", ".tgz", ".gz", ".zip", ".tar"]
  .find((value) => lowercaseName.endsWith(value));
if (!suffix || !file.name.endsWith(suffix)) {
  throw new Error("请选择后缀为小写 .zip、.tar、.tar.gz、.tgz 或 .gz 的日志归档。");
}
if (file.size < 1 || file.size > 2_684_354_560) {
  throw new Error("日志归档大小须为 1～2,684,354,560 字节。");
}
const contentType = {
  ".zip": "application/zip", ".tar": "application/x-tar",
  ".tar.gz": "application/gzip", ".tgz": "application/gzip", ".gz": "application/gzip",
}[suffix];
const metadata = {
  request_id: crypto.randomUUID(), name: file.name, content_type: contentType,
  declared_size: file.size, declared_sha256: await websiteUpload.sha256(file),
};
const attachmentMessageId = crypto.randomUUID();
let prepared, uploaded;

async function submitLogs() {
  prepared ??= await client.attachments.prepare(conversationId, metadata);
  uploaded ??= await client.attachments.upload(prepared, file);
  return client.conversations.send(conversationId, {
    request_id: attachmentMessageId, attachment_ids: [uploaded.attachment_id],
  });
}
```

`websiteUpload.sha256(file)` 需接入网站现有的增量哈希组件或后端上传模块，返回该文件实际 SHA-256 的 64 位小写十六进制值。示例不提供哈希实现，也不使用占位哈希。计算哈希时，应避免一次读取数 GiB 文件。

示例先检查后缀和大小，再计算哈希，全程保留文件原名。`file.type` 可能为空，因此根据支持的小写压缩后缀确定 MIME；服务端仍会核验实际上传内容。仅带附件的消息应省略 `text` 或传 null，不传空字符串。

上传预约的元数据、原文件、消息 ID 和已收到的回执，也应保存在按钮回调及重试函数之外。同一次上传失败后，沿用这些数据调用 `submitLogs()`，不能更换文件或重新生成 ID。

### 目录、历史与重新诊断

使用 `conversations.get(id, {include: ["history"], history_before, history_limit: 50})` 加载历史记录，`history_before` 取上一页返回的 `history_next_cursor`。按 `history[].id` 去重，将更早的记录插入顶部。点击结果卡片后，调用 `conversations.get(id, {include: ["report", "artifacts"], run_id: entry.run_id})`；`current_run` 仍表示当前轮，报告正文则属于 `selected_run_id`。

根据 `capabilities` 控制操作按钮。停止时保存原 `request_id` 和 `current_run.run_id`，在 `CANCELLING` 状态下等待当前轮停止。当前轮结束后，用户使用普通 `conversations.send` 提交新问题和需要复用的附件 ID，即可开始新一轮诊断，且不会继承旧结论。新一轮开始后重新订阅 SSE，历史结果按 `run_id` 缓存；旧轮次的完成事件不能关闭新轮次的订阅。

`diagnosis.result` 也包含因失败、取消或中断而没有报告的诊断轮次。此时 `result.report_state=UNAVAILABLE`，应展示 `status` 和可选的 `failure`。点击卡片仍可读取该轮详情，不能显示为“结果仍在生成”。

## 4. 接入真实网站后端

先确认网站后端可以访问 xiaodao 的 `/live`、`/ready` 和 `/openapi.json`，实际部署版本为 `8.2.0` / `v11-contract-r2`。

`WEBSITE_AUTH_MODULE` 指向网站自己的 `.mjs` 模块。该模块须以具名导出的方式提供 `access` 对象，对应 [server.ts](server.ts) 中的 `Access` 类型：

| 回调 | 网站需实现的逻辑 |
| --- | --- |
| `authenticate(request)` | 校验真实登录态，返回 `{id: string}` 或 `null`；Cookie 认证还需校验 CSRF / Origin |

唯一的回调 `authenticate(request)` 是异步函数。会话和附件的用户归属由 xiaodao 持久保存，重复请求不会重复登记，网站不再维护另一份列表。后端将固定的 `WEBSITE_OWNER_NAMESPACE` 与已认证用户 ID 编码后计算 SHA-256，作为 `X-Agent-Owner-Key`。命名空间默认为 `xiaodao-website`，上线后应保持不变，不同网站使用不同的值。不得信任浏览器自行提交的用户归属键或 `user_id`。

从仓库根目录启动，替换实际地址和模块路径。

Linux / Bash：

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

后端示例固定监听 `127.0.0.1`，默认端口为 `8787`；`PORT` 只能修改端口。将网站同源 `/api/agent/` 反向代理到此服务，或把 `createAgentBackend` 接入现有的 Node HTTP 后端。SSE 代理需关闭响应缓冲，将读取超时设为大于 15 秒心跳间隔的值，例如 60 秒。上传代理需保留原始字节、长度和校验请求头。

未配置认证模块时，进程可启动，但业务请求全部返回 `401`。模块路径建议使用绝对路径；相对路径以启动目录为准。不要为了联调删除授权检查，也不要把监听地址改为公网来绕过授权。

浏览器调用网站的 `/api/agent`，网站后端访问 xiaodao 的 `/api/v1/agent`。后端只向配置的 `XIAODAO_BASE_URL` 发送请求，每次读取会话只请求一次上游接口。`included` 标明本次实际加载了哪些部分。未请求的 `history`、`attachments`、`result` 或 `artifacts` 为 null，页面应保留此前的内容；空数组则表示确实没有记录。

原始产物的下载地址使用已核验的会话、轮次和产物 ID 构造，不使用上游返回的 `download_url`。因此，内部地址可以与服务端 `PUBLIC_BASE_URL` 不同，配置的路径前缀会保留。上传地址也会改写为网站同源路径。

## 5. 最后接实时进度和下载

从网站 `/api/agent/conversations/{id}/events` 订阅 SSE。每条业务消息是一行 `data: <完整 AgentEvent JSON>` 加空行，不包含 `id:`、`event:`、`retry:` 行。使用 `onmessage` 接收，根据 JSON 的 `type` 处理事件，按 `sequence` 去重。连接和心跳注释不会触发业务消息；响应中没有 `[DONE]` 或 OpenAI `choices` / `delta` 包装。

首次打开或刷新页面时，调用 `conversations.get(id)`，直接展示其中的 `result`。收到 `result.available` 且尚未显示报告时，调用 `conversations.get(id, {include: ["report"]})`。同一会话已有报告读取请求尚未完成时，应合并重复请求。报告成功展示后，重复收到就绪事件时无需再次下载。`PENDING` 或读取失败的结果不能缓存为已就绪；收到后续通知或用户重试时，仍需重新查询。

原生 `EventSource` 无法从仅含 `data:` 的响应中记录业务游标，自动重连时会回放历史。需要准确续传时，使用流式 `fetch`，将 `Last-Event-ID` 手动设为最后处理成功的事件序号。事件应串行处理，报告读取并展示成功后才更新游标。包含队列容量限制和失败重试的完整代码见 [API 参考](../../docs/website-agent-api.md)；报告读取代码不应另行启动诊断流程。

收到 `agent.failed` 或 `conversation.interrupted` 时，重新读取会话快照并展示 `failure`。`details` 中保留了失败阶段和 `diagnostic_id`，便于服务端排查。刷新后也从快照恢复失败原因；查询已结束的任务不会重新调用模型。

若 `failure.details` 包含 `ARCHIVE_STATUS_COMMIT / persistence=UNKNOWN`，显示“报告已生成，但归档状态暂时无法确认”，保留 `RUNNING / PENDING` 和已有报告。该提示仅在进程运行期间临时保存，不写入历史，也不生成完成事件。继续订阅，等收到 `conversation.completed` 后再结束；SSE 断线不会取消后台任务。进度只展示公开的阶段信息和追问，不转发模型内部推理或未经审核的报告。

下载信息在会话的 `artifacts` 中返回。归档就绪后，可使用 `conversations.get(id, {include: ["artifacts"]})` 更新。下载原始文件时，使用其中的网站同源 `download_url`，路径为 `/api/agent/conversations/{id}/files/{artifact_id}/content?run_id={selected_run_id}`。

下载结果 ZIP 前，需由用户确认文件包含原始目标日志。下载时保留原 `run_id`，并使用 URL.searchParams 添加 `download=archive&acknowledge_raw_logs=true`。审计包仅在用户提出请求时下载，同样保留 `run_id`，并添加 `download=audit`。

后端先核对实际字节数和 SHA-256，再转发内容。`Content-Length`、`X-Content-SHA256` 可以缺省，但提供时必须与实际内容一致。下载不接受重定向。不超过 16 MiB 的 JSON 和 Markdown 在设有上限的内存缓冲区中处理；ZIP 及更大的产物使用独立的临时文件，转发结束后清理。

报告原始内容最多 16 MiB。JSON 转义可能扩大响应，因此会话包含报告时预留 `6 × 16 MiB + 64 KiB`；同时加载历史时再预留 16 MiB。未请求报告的响应上限仍为 16 MiB。报告加载失败时保留会话并提供重新读取入口，不要自动新建诊断任务。

## 6. 本地验证、环境联调与升级

离线预览只验证展示效果。示例自检使用模拟的上游服务，不访问已部署服务，也不调用真实模型：

```bash
node --test examples/website-agent/server.test.mjs
```

自检通过不代表 Linux 环境已通过验收。向实际服务发送消息前，按 [Test Flow 说明](../../tools/test-flow/README.md)查看对应 `--plan-only` 输出中的身份和预算，再按[联调清单](../../docs/website-agent-quickstart.md)验证追问、SSE、报告、权限和重启恢复。

早期 `8.0.0` 预览版曾调整 SSE 传输格式，接入方需统一使用 `onmessage`，并管理已处理事件的游标；那次调整没有改变 V11 数据规范。本次 `8.2.0` 使用 `v11-contract-r2`，SSE 为 `schema_version=2`，每条事件包含 `run_id`。已有 r1 或 r2 数据须提供 `--ownership-map`，按[副本升级说明](../../docs/data-upgrade-v11-r2.md)手动升级。原数据和历史报告需保留，不能直接修改旧目录的数据规范标记。

当前版本为 `8.2.0` / `v11-contract-r2`，State schema 11 与报告 schema 3 保持不变，Agent 存储版本为 2，会话详情版本为 3。已有数据必须先按副本升级流程处理，旧事件和不可变产物的原始字节会保留。原 `/status`、`/report`、产物列表和会话下预约附件路径已删除，网站需同步更新。
