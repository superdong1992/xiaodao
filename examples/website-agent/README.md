# 网站接入示例：先看报告，再接 API

从旧版升级先读 [8.2 前端必改清单与开发 Prompt](../../docs/website-agent-upgrade-8.2.md)。旧接口替换、轮次处理、停止/删除及部署联动都列在其中。

这里提供离线报告预览、可复制的浏览器模块和网站后端示例。可以先确认报告怎么展示，再接入网站登录、真实任务和日志上传。使用 Node.js 24+，无需安装 npm 依赖。

部署联调查[快速接入清单](../../docs/website-agent-quickstart.md)，请求、响应和 SSE 细节查 [Agent API 参考](../../docs/website-agent-api.md)。

通用报告赞踩接口及经验复用规则见[经验库接入说明](../../docs/generic-feedback-memory.md)。按钮由网站实现，本示例提供调用封装和后端转发。

## 1. 先打开离线报告预览

从仓库根目录启动：

```bash
node examples/website-agent/preview.mjs
```

打开 [http://127.0.0.1:8788/](http://127.0.0.1:8788/)。预览只使用本地示例数据，不连接 xiaodao、不创建任务，也不调用模型。先在这里查看等待、完整报告、部分结果、尚无定论和失败等展示状态，也可试用重命名、停止、删除、新一轮和历史报告切换。交互只改浏览器内的合成数据，刷新即可恢复。

| 文件 | 作用 | 接入网站时怎么用 |
| --- | --- | --- |
| `preview.mjs` | 启动离线预览 | 本地查看，无需部署到网站 |
| `report-view.js` | 按固定字段生成报告界面 | 复制到网站静态资源目录，调用 `renderReport` |
| `report-view.css` | 报告区样式 | 与渲染模块一起复制，也可替换为网站自己的样式 |
| `browser-client.js` | 浏览器调用网站同源 API 的小封装 | 复制后使用，或按同样的接口接入现有请求库 |
| [server.mjs](server.mjs) | 网站后端授权、上游请求、SSE 和产物下载的唯一实现 | Node.js 18+ 可导入 `createAgentBackend` 集成到既有后端 |
| [server.ts](server.ts) | 类型声明和兼容启动入口 | 使用 Node.js 24+ 执行下文启动命令，与 `server.mjs` 共用实现 |

## 2. 用最少代码显示已有报告

把 `report-view.js`、`report-view.css` 和 `browser-client.js` 放进网站同一个静态资源目录，例如 `/xiaodao/`。后端接好同源 `/api/agent/` 后，在已有页面加入：

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

客户端方法成功时返回解包后的 `data`，失败抛出 `AgentApiError`，不会自动重试。错误提示使用独立容器，读取或渲染失败时保留已有报告。若直接使用 `fetch`，先检查 HTTP 状态和响应的 `ok`，再调用 `renderReport(container, response.data.result)`。读取已有报告是只读操作，不会重新诊断、重新生成报告或调用模型。

会话响应固定为 `schema_version=3`，报告放在 `result`。先看 `result.report_state`：`PENDING` 展示等待提示，`UNAVAILABLE` 展示结束原因，`READY` 才展示正文。这三种状态都返回 HTTP 200。`READY` 中再按 `format` 选择内容：

| `format` | 正文 | 展示方式 |
| --- | --- | --- |
| `problem-locator-diagnosis-v3` | `data.result.report` | 按结构化字段展示；`COMPLETED`、`PARTIAL`、`INCONCLUSIVE` 都有正式报告 |
| `markdown` | `data.result.markdown` | 默认安全地展示原文，保留换行；需要排版时可替换为网站自己的 Markdown 渲染器，并过滤不安全 HTML 和链接 |
| `generic-v1` | `data.result.report` | 展示历史结果的 `conclusion` 和 `root_cause_analysis` |

JSON 报告按 `conversation.result.report` 的固定字段绑定组件。以下字段相对于 `result`；不再返回重复的中文 `sections`。

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

归档的 `archive_status` 与报告可读性分开处理。ZIP 仍在生成或已经失败时，报告照常展示；`failure` 中的归档异常也不能覆盖已发布的报告。

## 3. 再接创建、消息和上传

`browser-client.js` 不依赖框架或第三方库，默认调用网站同源 `/api/agent`。这些方法分别对应一个业务操作：

| 方法 | 用途 |
| --- | --- |
| `conversations.list({cursor, limit})` | 分页读取当前用户的会话目录，默认 20 条、最多 100 条 |
| `conversations.rename(id, title)` | 修改标题，1 到 80 个字符 |
| `conversations.stop(id, {request_id, run_id})` | 停止指定轮，重试沿用两个原值 |
| `conversations.delete(id)` | 删除会话，无请求体；按会话 ID 幂等 |
| `conversations.create(requestId)` | 创建会话，保存 `conversation_id` |
| `conversations.send(id, {request_id, text, attachment_ids})` | 发送问题、补充回答或已上传附件 ID |
| `conversations.get(id)` | 一次恢复状态、追问、进度、消息、附件、报告和下载信息 |
| `conversations.get(id, {include: []})` | 只读轻量状态；不加载历史、报告或产物列表 |
| `conversations.get(id, {include: ["report"]})` | 从同一会话读取报告；也可选择 `history`、`artifacts` 或组合 |
| `conversations.getFeedback(id, runId, {signal})` | 读取指定报告的评价资格和当前投票，`signal` 可选 |
| `conversations.setFeedback(id, runId, {request_id, rating})` | 提交 `LIKE` 或 `DISLIKE`；重试保留原 ID 和内容，换票使用新 ID |
| `conversations.eventsUrl(id)` | 返回会话 SSE 路径，交给订阅和游标处理代码 |
| `attachments.prepare(id, metadata)` | 预约日志附件，声明会话、名称、类型、大小和 SHA-256 |
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

创建并发送问题可以这样接。准备一次逻辑提交时保存原话和 ID，放在按钮处理及网络重试函数之外；重试只再次调用 `submitProblem()`：

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

`problemText` 是输入框本次提交的实际原话。需要刷新后继续重试时，由网站持久保存这份提交记录；只有新逻辑提交才换 ID。创建回执的 `request_id` 已被网站后端按用户改写，不能拿它替换原始 `createRequestId` 再创建会话。

收到非空问题原话后，服务端按 MCP 客户端的固定中性模板创建 Case，初始事实为空。网站不应把预期行为、范围或日志设为建案前置条件；创建前不调用 INTAKE。建案后按原文展示 OPEN requirements 的追问，没有 OPEN requirements 就不额外追问，前端也无需生成 `problem_spec`。

附件按“预约 → PUT 原始字节 → 发消息引用 `attachment_ids`”提交。上传到 `READY` 只表示文件可用，是否被采用另看消息的 `APPLIED` / `notice`。下面的 `file` 是用户选中的原始 File，`conversationId` 是已有会话 ID：

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

`websiteUpload.sha256(file)` 是需要网站接入的现有增量哈希组件或后端上传模块，应返回该文件实际 SHA-256 的 64 位小写十六进制值；此示例不提供哈希实现，也不使用占位哈希。避免为计算哈希一次读取数 GiB 文件。示例先检查后缀和大小，再计算哈希，始终保留文件原名。`file.type` 可能为空，因此按支持的小写压缩后缀确定 MIME；服务端仍核验实际上传内容。仅带附件的消息省略 `text` 或传 null，不传空字符串。

预约元数据、原文件、消息 ID 和已取得的回执也保存在按钮及重试函数之外；同一逻辑上传失败后沿用它们调用 `submitLogs()`，不能换文件或重新生成 ID。

### 目录、历史与重新诊断

`conversations.get(id, {include: ["history"], history_before, history_limit: 50})` 加载历史，`history_before` 使用上页的 `history_next_cursor`。按 `history[].id` 去重，在顶部插入更早记录。结果卡片点击后调用 `conversations.get(id, {include: ["report", "artifacts"], run_id: entry.run_id})`；`current_run` 仍是当前轮，正文属于 `selected_run_id`。

操作按钮使用 `capabilities`。停止时保存原 `request_id` 和 `current_run.run_id`；`CANCELLING` 期间等待退出。当前轮结束后，用普通 `conversations.send` 明确发送新问题和要复用的附件 ID，自动开始新轮，不继承旧结论。开始新轮后重新订阅 SSE，历史结果按 `run_id` 缓存，旧轮完成事件不能关闭新轮订阅。

`diagnosis.result` 也包含没有报告的失败、取消或中断轮。此时 `result.report_state=UNAVAILABLE`，展示 `status` 和可选 `failure`；点击卡片仍可读取该轮详情，不能把它显示成“结果仍在生成”。

## 4. 接入真实网站后端

先确认网站后端可以访问 xiaodao 的 `/live`、`/ready` 和 `/openapi.json`，线上版本为 `8.2.0` / `v11-contract-r2`。

`WEBSITE_AUTH_MODULE` 指向网站自己的 `.mjs` 模块，模块须具名导出 `access` 对象，对应 [server.ts](server.ts) 中的 `Access` 类型：

| 回调 | 网站需实现的逻辑 |
| --- | --- |
| `authenticate(request)` | 校验真实登录态，返回 `{id: string}` 或 `null`；Cookie 认证还需校验 CSRF / Origin |

唯一回调 `authenticate(request)` 是异步函数。会话和附件归属由 xiaodao 持久、幂等地保存，网站不再维护另一份目录。后端将固定 `WEBSITE_OWNER_NAMESPACE` 与认证用户 ID 编码后计算 SHA-256，作为 `X-Agent-Owner-Key`。命名空间默认 `xiaodao-website`，上线后保持不变；不同网站使用不同值。不得信任浏览器自报的归属键或 `user_id`。

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

后端示例固定监听 `127.0.0.1`，默认端口 `8787`；`PORT` 只改端口。将网站同源 `/api/agent/` 反向代理到此服务，或把 `createAgentBackend` 接入既有 Node HTTP 后端。SSE 代理关闭响应缓冲，读取超时大于 15 秒心跳间隔，例如 60 秒；上传代理保留原始字节、长度和校验请求头。

未配置认证模块时，进程可启动，但业务请求全部返回 `401`。模块路径建议使用绝对路径；相对路径以启动目录为准。不要为了联调删除授权检查，也不要把监听地址改为公网来绕过授权。

浏览器使用网站的 `/api/agent`；网站后端访问 xiaodao 的 `/api/v1/agent`。后端只向配置的 `XIAODAO_BASE_URL` 发请求。会话读取只用一次上游请求。`included` 说明本次实际加载的部分；未请求的 `history`、`attachments`、`result` 或 `artifacts` 为 null，页面保留此前内容。空数组表示确实没有记录。

原始产物的下载地址由已核验的会话、轮次和产物 ID 构造，上游的 `download_url` 不参与寻址。因此内部地址可以与服务端 `PUBLIC_BASE_URL` 不同，配置的路径前缀会保留。上传地址也会改写为网站同源路径。

## 5. 最后接实时进度和下载

SSE 从网站 `/api/agent/conversations/{id}/events` 订阅。每条业务消息是一行 `data: <完整 AgentEvent JSON>` 加空行，不包含 `id:`、`event:`、`retry:` 行。使用 `onmessage` 接收，从 JSON 的 `type` 分派事件，按 `sequence` 去重。连接和心跳注释不会触发业务消息；没有 `[DONE]` 或 OpenAI `choices` / `delta` 包装。

首次打开或刷新调用 `conversations.get(id)`，直接展示其中的 `result`。收到 `result.available` 且尚未显示报告时，调用 `conversations.get(id, {include: ["report"]})`。合并同一会话尚未完成的报告读取；报告成功展示后，重复就绪事件不必再次下载。`PENDING` 和读取失败不能缓存成已就绪，后续通知或用户重试仍应重新查询。

原生 `EventSource` 不会从 data-only 响应记录业务游标，自动重连会回放历史。精准续传使用流式 `fetch`，手动设置最后处理成功的 `Last-Event-ID`。事件串行处理，报告读取和展示成功后才推进游标。完整有界队列和失败重试代码见 [API 参考](../../docs/website-agent-api.md)，不要在报告读取代码里另起一套诊断流程。

收到 `agent.failed` 或 `conversation.interrupted`，重新读取会话快照并展示 `failure`。`details` 中保留失败阶段和 `diagnostic_id`，便于服务端查证。刷新后也从快照恢复原因；终态不会因查询而重跑模型。

若 `failure.details` 含 `ARCHIVE_STATUS_COMMIT / persistence=UNKNOWN`，显示“报告已生成，但归档状态暂时无法确认”，保留 `RUNNING / PENDING` 和已有报告。这是进程内临时提示，不写入历史，也不生成完成事件。正常订阅等 `conversation.completed` 再收尾；SSE 断线不取消后台任务。进度只展示公开阶段和追问，不转发模型内部推理或未审核报告。

下载信息随会话的 `artifacts` 返回；归档就绪后可用 `conversations.get(id, {include: ["artifacts"]})` 更新。原始文件使用其中的网站同源 `download_url`，路径为 `/api/agent/conversations/{id}/files/{artifact_id}/content?run_id={selected_run_id}`：结果 ZIP 由用户确认包含原始目标日志后，保留原 `run_id` 并用 URL.searchParams 加上 `download=archive&acknowledge_raw_logs=true`；审计包仅按用户请求保留 `run_id` 并加 `download=audit` 下载。

后端先核对实际字节数和 SHA-256，再转发内容。`Content-Length`、`X-Content-SHA256` 可以缺省；提供时必须匹配。下载不接受重定向。不超过 16 MiB 的 JSON 和 Markdown 使用有界内存，ZIP 及更大产物使用唯一临时文件，转发结束后清理。

报告原始内容最多 16 MiB。JSON 转义可能扩大响应，因此会话包含报告时预留 `6 × 16 MiB + 64 KiB`；同时加载历史时再预留 16 MiB。未请求报告的响应上限仍为 16 MiB。报告加载失败时保留会话并提供重新读取入口，不要自动新建诊断任务。

## 6. 本地验证、环境联调与升级

离线预览只验证展示。示例自检使用假上游，不访问已部署服务，也不调用真实模型：

```bash
node --test examples/website-agent/server.test.mjs
```

自检通过不代表 Linux 环境验收通过。真实发消息前，按 [Test Flow 说明](../../tools/test-flow/README.md)查看对应 `--plan-only` 身份和预算，再按[联调清单](../../docs/website-agent-quickstart.md)验证追问、SSE、报告、权限和重启恢复。

早期 `8.0.0` 预览版曾调整 SSE 传输，接入方须统一使用 `onmessage` 并管理处理游标；历史上的那次调整没有改变 V11 数据合同。本次 `8.2.0` 使用 `v11-contract-r2`，SSE 为 `schema_version=2`，每条事件包含 `run_id`；已有 r1 或 r2 数据须提供 `--ownership-map`，按[副本升级说明](../../docs/data-upgrade-v11-r2.md)显式升级。保留原数据和历史报告，不能直接修改旧目录的合同标记。

本轮为 `8.2.0` / `v11-contract-r2`，State schema 11 与报告 schema 3 保持不变，Agent 存储版本为 2，会话详情为 3。必须先做副本升级；旧事件和不可变产物字节保留。原 `/status`、`/report`、产物列表和会话下预约附件路径已删除，网站需同步更新。
