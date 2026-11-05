# 网站接入：报告后的连续追问

报告交付后，继续使用同一个追问输入框提问、更正信息或补充日志。每次追问都会直接创建关联 run 和独立 Case，使用通用 Skill 分析本次问题并发布新的 Generic V2 完整报告。入口仍是 followups，前端不增加模式选择、草稿确认或意图判断。旧报告、下载、历史问答和评价保持原样。

追问可以转向新的分析方向，最新问题的原文就是本轮 raw problem text，不先拼接成旧故障的延伸问题。需要完全独立的会话记录时仍可选择“新建对话”。不要把报告追问发到旧 messages 接口，也不要在追问失败后自动回退到它。

支持从 Generic V2 和默认 skill-direct 的 Markdown 报告发起追问；strict / advisory 结构化报告仍不在支持范围。只能从同一版本链的最新报告发起；查看旧版不影响阅读和下载，但应先切换最新版再追问。以 GET 的 can_ask 和 reason 为准，不能按标题或 resources_expired 猜测资格。

功能默认关闭，服务端配置 REPORT_FOLLOWUP_ENABLED=true 后启用。提交追问会调用真实模型，但没有前置的只读问答、充分性分类或 ROUTE 调用；模型工作发生在正式通用诊断中。读取、分页、订阅和恢复状态不会调用模型。少一次前置调用不等于整体更快或更便宜：每问都执行完整诊断，成本和等待时间取决于本次材料、工具与模型。

新流程使用正式 Generic 的模型命令、工具、工作区及现有 1800 秒执行预算，不使用旧 REPORT_FOLLOWUP_TIMEOUT_SECONDS 作为诊断时限。该配置仍保留解析与取值校验，不能用它调整新通用诊断预算。REPORT_FOLLOWUP_SNAPSHOT_BYTES 和 REPORT_FOLLOWUP_STORAGE_BYTES 保留历史日志快照管理用途；新追问不为只读回答复制快照，也不因旧快照未就绪而退回报告问答。

历史普通回答仍可按原接口独立赞踩或提交文字负反馈。新追问项的 diagnosis_run_id 指向正式诊断，answer_markdown 只是服务器保存的交接说明，不是诊断结论，不接受赞踩、文字负反馈或经验提炼。应评价新报告本身；原报告评价不变。详情见[回答反馈增量适配说明](website-answer-feedback-upgrade.md)。

## 原生连续会话

新版不再启动或 resume 旧 REPORT_FOLLOWUP 原生只读模型会话。原生追问时期已经保存的网站问答和报告仍保留，在新诊断中作为参考资料使用；不会恢复旧模型角色，也不把旧答案作为本次推理前提。启用新流程不再要求启动器具备旧追问的 --resume 能力，但仍须满足正式 Generic 诊断的执行要求。

旧 CLI 会话目录 DATA_ROOT/resources/followup-sessions/<conversation_id>/config 与数据库记录继续按原保留策略管理，不在升级时自动删除。它们曾保存的日志片段不代表本轮重新核对过日志。部署保留和显式删除规则见[存储说明](storage-retention.md)；新诊断所需背景来自服务端保存的业务记录，不依赖旧 CLI 自动压缩或续接效果。

## 继续诊断与报告版本

### 每次追问如何执行

1. 浏览器提交原 request_id、最新 text 和可选 attachment_ids。服务端先检查归属、报告版本、活动任务和材料状态；原请求重放先返回已有收据，不重复创建。
2. 追问 worker 直接交接正式诊断。关联 run 和追问项的 diagnosis_run_id 在同一事务提交，不调用问答模型或分类模型。
3. 新 Case 原样采用本次问题，内部强制进入 Generic，不经过 ROUTE，也不沿用旧专用 Skill 的选择。通用 Skill 按正式工具权限核对资料、调用所需工具并生成完整 V2 报告。
4. 资料不足也先完成本轮通用分析，以 UNRESOLVED 说明已知信息、证据缺口和建议补充内容；不在诊断前另发 REQUEST_INPUT 阻断。用户可依据报告继续补充，再发起下一次追问。
5. 只有成功发布的报告进入版本列表。执行失败、中断或停止时保留旧版；读取、刷新和重连不会自动重跑。

| 用户输入 | 本轮处理 | 历史资料的作用 |
| --- | --- | --- |
| “原报告里的 3 秒是什么意思？” | 通用 Skill 分析并形成完整结果 | 相关报告段落作为解释对象 |
| “先不看连接池，查另一个服务的 DNS 失败” | 以这句新问题确定方向，不先按旧报告作答 | 只有与 DNS 问题相关的背景才采用 |
| “实际发生于 10:05，刚才时间写错了” | 分析更正后的问题 | 当前更正优先，不把旧时间继续当事实 |
| “这是不是网络问题？目前没有服务端日志” | 仍执行通用分析，证据不足时给出 UNRESOLVED | 明确假设与缺口，不把怀疑写成已确认根因 |
| 关联诊断正在执行 | 暂不接受另一条新追问 | 页面继续显示该轮进度和停止操作 |

### 内部决策与网页展示分开

新版不再要求追问模型返回 ANSWER、REQUEST_INPUT 或 CONTINUE_DIAGNOSIS 结果包。旧 xiaodao.followup.v1 与 262144 字节结果包限制只属于历史只读模型路径，不是新 Generic 报告合同。公开 followups 请求、回执和 SSE 仍保持原形状，text 仍最多 65536 UTF-8 字节。

浏览器只显示公开字段，不从答案中的 JSON、action 字段或代码围栏执行动作。FollowupItem.run_id 绑定提交时的父报告，diagnosis_run_id 绑定新诊断；追问项 COMPLETED 表示交接已完成，不表示新报告已经生成。页面读取新轮状态，只有 report_state=READY 才显示“查看新版报告”并调用 onDiagnosisReady。新报告仍按正式报告接口读取，不能用交接说明替代。

旧记录没有 diagnosis_run_id 时，仍显示原答案和原 context_mode；新诊断不能用父报告的 snapshot_status 或 context_mode 宣称已核对哪些日志。实际检查范围以新报告和本轮证据为准。

### 当前问题与历史资料分开

本次 text 是当前任务。完整业务背景另存为只读 inputs/followup-context.json，包括原问题、此前问答与报告、最新未发布尝试的输入和用户补充、附件来源，以及纠正目标。模型按需查阅，不把整段历史塞进当前原话，也不由额外模型摘要或改写。参考文件有大小上限；超限明确失败，不静默截断历史。

历史用户陈述、旧模型结论和日志证据保留来源区别。最新用户更正优先；上次整理后的输入不是新的用户确认。QUEUED 消息仍是待采用陈述，UNUSED 消息不能标成已采用事实，其附件不自动进入候选。只承接最新关联且未发布报告的尝试，不把此前失败模型输出当作事实。原 Evidence、Outcome 和事实 ID 不复制为新 Case 的证据。

若主题仍相同，新报告说明旧判断为何保留、修正或撤回；若已经换主题，先直接回答当前问题并说明本轮范围，不强行比较两个故障的根因。版本链记录的是这段交流中连续发布的报告，不意味着所有版本必须属于同一个故障结论。

### 日志准备与证据选择

补日志仍使用原附件预约与上传接口，上传完成后将同会话 IDs 和文字一起提交。默认承接父报告实际采用的日志；若最近一次诊断失败且已保存有效材料集合，则承接该集合（也可能为空），不重新加入此前明确排除的文件。再追加该轮之后有效补充和本次上传，从中选择仍可用的材料，按 SHA-256 去重后交给正式 Generic 的 direct-v1 准备流程；可读文本与归档成员范围见本轮 generic_logs.json。最终仍受每次最多 20 份、总计 5 GiB 及原上传配额约束。原日志已清理时，不凭历史快照声称重新读取；有效的新日志仍可用于分析。

最新文字中的“替换”“排除”和更正要求由通用 Skill 结合来源遵守。框架目前不会在准备前用自然语言分类器决定这些要求的含义，也不能保证被文字排除的旧文件没有进入物理准备流程。模型只能引用本轮实际核对且符合当前要求的证据；日志已准备不等于已经核实。旧草稿 API 的显式最终 attachment_ids、APPEND/REPLACE 规则仍见下节，不与新版追问的自然语言要求混淆。

### 失败、停止与成本边界

页面优先显示终态和安全失败说明，旧进度不能遮住失败。交接前可停止追问处理；交接后使用 diagnosis_run_id 停止正式诊断。没有新报告时原版继续可读，用户可以补充后重新发问；失败提示、刷新或重连不会自动提交新任务。

旧诊断或兼容流程若仍返回 WAITING_INPUT，页面保留同一输入区，按后端 current_questions 补充，并固定 target_run_id=diagnosis_run_id；遇到 409 / AGENT_RUN_CHANGED 只刷新原目标，不自动改发追问。新流程不以材料充分性判断作为启动前置条件。

这里保证的是每次追问进入通用诊断、当前问题不被改写、事务与版本一致性。完整诊断也不能保证模型准确理解每次更正、选全证据或定位真实根因；没有真实模型验收就不能宣称生产准确率或延迟改善。测试范围见[报告追问验证说明](report-followup-testing.md)。

### 已有草稿接口兼容

旧版显式草稿 API 继续保留，供已接入的网站兼容使用；新版报告页不再提供第二个“继续诊断”按钮。以下预览与确认规则仅适用于这些旧接口，不是单追问入口的前置步骤。

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

日志按 SHA-256 去重。新 Generic Job 使用 `direct-v1` 直接读取 UTF-8 文本或安全展开归档中的文本，旧 Generic Job 仍使用创建时固定的 `parse-only`；skill-direct 的 `parse-targets` 不变。合并清单保留附件 ID、原名、成员路径、跳过原因和处理范围。没有可读日志的新通用报告按文字和参考资料分析，不能宣称已核对日志。最终清单最多 20 份文件、总计 5 GiB；上传仍受既有会话累计资源配额约束。旧文件已清理时，选择 REPLACE 并重新上传所需材料。报告正文仍可阅读和对比。

版本接口返回 `root_run_id`、`latest_run_id`、`can_continue` 和 `versions`。每项包含 `run_id`、`parent_run_id`、`version`、`created_at`、`input_changes`、`diagnosis_goal`、`runtime_changes`。只有成功发布的报告进入版本列表；失败轮次仍保留在会话历史中，父报告可以再次继续诊断。Skill、模型执行配置和解析器变化使用保存的元数据比较；模型配置指纹不代表远端实际解析出的模型身份，缺少历史记录时明确提示无法比较。

旧草稿继续诊断的报告以“本轮变化”说明旧判断的变化；新版统一追问先回答当前问题，同主题时再说明判断变化，换主题时说明本轮范围。页面按两个指定 run_id 读取完整报告并排展示，输入变化来自已保存的输入记录；没有 compare 服务或额外模型任务。

### 页面接入

报告页保留一个追问输入框、补日志上传和版本对比。收到带 `diagnosis_run_id` 的问答项时，展示交接说明并读取该轮进度；重复事件、刷新或重连只能恢复这条关联，不能再提交新诊断。旧报告仍按原 `run_id` 展示和下载，待新报告发布后再提供版本切换。

与现有浏览器模块一起部署 `continuation-contract.js`、`continuation-controller.js`、`continuation-view.js`、`log-upload-controller.js`、`file-sha256.js` 和 `file-sha256-worker.js`；BFF 同步部署 `continuation-bff.mjs`。统一追问的日志上传直接依赖上传控制器及两个哈希文件，版本对比和旧客户端也仍依赖继续诊断模块，不能因删除第二个按钮就漏发文件。两个哈希文件放在同一目录，网站 CSP 允许 `worker-src 'self'`。默认在 Worker 中分块校验并显示进度，取消会终止 Worker 或中断网络上传；Worker 不可用时，每处理 64 KiB 就让出主线程。预览还使用 `continuation-preview.js`，线上无需部署它。

## 接口与身份

以下为网站 BFF 路径。xiaodao 原生路径在 `/api` 后增加 `/v1`，其余部分相同。完整字段表见 [Agent API 参考](website-agent-api.md)。

| 方法 | 网站路径 | 输入 |
| --- | --- | --- |
| GET | `/api/agent/conversations/{conversation_id}/runs/{run_id}/followups` | 可选 `cursor`、`limit`，默认最新 50 条，最大 100 条 |
| POST | 同上 | `{request_id,text}`，可加已上传的 `attachment_ids`；纠正可加 `intent=CORRECTION`、`target_followup_id` |
| GET | 上一路径加 `/events` | `Last-Event-ID` 请求头 |
| POST | 上一路径加 `/{followup_id}/stop`（不含 `/events`） | `{request_id}` |
| GET / PUT | 追问路径加 `/{followup_id}/feedback` | GET 无请求体；PUT 为 `{request_id,rating}`，独立评价该回答 |

`run_id` 必须取当前展示报告的 `selected_run_id`，不能总取 `current_run.run_id`。切换历史报告时，销毁旧 controller，再为新报告创建实例。每个会话同时最多有一个活动追问；`active_followup` 可能属于另一份报告，停止时使用它自己的 `run_id` 和 `followup_id`，不要把它插入当前报告的问答列表。

默认接入方式是 BFF 原样透传 Cookie，由 xiaodao 的 Redis 会话鉴权读取 `user.userid` 并计算 owner。读取、提交、停止和 SSE 都须携带同一登录 Cookie。浏览器传入的 `X-Agent-Owner-Key` 不会被采用，知道 UUID 也不代表有权访问。

只有显式向 `createAgentBackend` 传入 `access`，或配置 `WEBSITE_AUTH_MODULE` 时，BFF 才会调用 `access.authenticate(request)` 并根据返回的可信用户身份计算 owner。该旧接入方式须配合服务端 `WEBSITE_AUTH_MODE=trusted_header`，并由网站的登录模块校验登录态及 CSRF / Origin。默认 Cookie 透传 BFF 不自动执行 CSRF / Origin 校验，网站必须保留自己的校验入口；下例中的 `X-CSRF-Token` 也只有被该入口接收并验证才生效。两种方式的部署配置见 [Redis 会话接入说明](website-redis-deployment.md)。

`request_id` 为 1 到 128 个 Unicode 字符，不能全为空白；`text` 非空白，最多 65536 字符且最多 65536 UTF-8 字节。不要用 JavaScript 字符串长度代替 UTF-8 字节长度。同一逻辑提交重试保持 ID 和原文；新问题使用新 ID。停止操作有自己独立且稳定的 ID。

## 直接复用浏览器模块

复制 [示例目录](../examples/website-agent/README.md) 中的 `browser-client.js`、`followup-contract.js`、`continuation-contract.js`、`feedback-comment-contract.js`、`followup-controller.js`、`followup-view.js`、`log-upload-controller.js`、`file-sha256.js`、`file-sha256-worker.js`、`report-view.js` 和 `report-view.css` 到网站静态资源目录；复用共用输入框时，一并部署 `conversation-input.js`。这些模块应来自同一次发布，保留相对导入路径，不要只替换入口文件。controller 发真实的同源请求；离线预览仅替换它的 `fetchImpl`，没有另一套页面逻辑。

BFF 同步部署 `server.ts`、`server.mjs`、`followup-bff.mjs`、`followup-contract.js`、`continuation-bff.mjs`、`continuation-contract.js`、`feedback-comments-bff.mjs` 和 `feedback-comment-contract.js`。即使 `REPORT_FOLLOWUP_ENABLED=false`，也必须保留这些静态导入的模块，否则 BFF 无法启动。后端、BFF 和浏览器模块的累计升级顺序见[生产升级清单](production-upgrade-2026-09-23.md)。

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
6. 事件游标超出当前范围时返回 `409 / AGENT_FOLLOWUP_INVALID_CURSOR`，先重新 GET 快照，再从新游标订阅。已有追问及回答永久保留；日志正常保留 15 天，磁盘紧张时可提前回收已完成轮次。日志清理后，报告归档完整仍可继续追问，网站以 `can_ask` / `reason` 为准，不能仅凭 `resources_expired` 禁用输入。用户主动删除会话后才返回 404。只在本地保存游标而不保存历史，无法靠该游标重建页面；本示例刷新后总从快照恢复。

SSE 在所选报告的追问全部终止且待发事件已发完后关闭。另一报告忙时，可用 GET 刷新会话级 `active_followup`；不会把另一报告的事件塞进当前流。网络中断或页面刷新不会停止回答，若要停止，必须显式调用 stop。

controller 在提交前保存原文和稳定请求 ID，收到回执后保存回执身份。响应丢失时先刷新；若列表里已有相同 `request_id`，直接恢复该记录。结果仍不确定时，由用户重试原请求，不生成新 ID。不会自动重试模型提交。

## 可用性与边界

| 状态或错误 | 页面行为 |
| --- | --- |
| `snapshot_status` | 保留旧日志副本状态；不据此判断新通用诊断的证据或能否提交 |
| `AGENT_FOLLOWUP_DISABLED` | 提示功能尚未启用，不回退到 messages |
| `AGENT_FOLLOWUP_UNSUPPORTED` | 提示报告暂不支持追问 |
| `BUSY`、`AGENT_FOLLOWUP_BUSY`、`AGENT_DIAGNOSIS_BUSY` | 等待当前追问处理或关联诊断结束，或显式停止后再问；BUSY 时 active_followup 可能为 null |
| `LATEST_REPORT_REQUIRED`、`AGENT_REPORT_CHANGED` | 已有新版报告，请切换到最新版继续追问；保留旧版阅读和下载 |
| `AGENT_FOLLOWUP_SOURCE_CHANGED` | 刷新报告状态后重新提问 |
| `AGENT_RESOURCE_EXPIRED`、`AGENT_ATTACHMENT_EXPIRED` | BFF 保留 HTTP 410。提示日志已清理或附件已失效，清除已被明确拒绝的待重试请求，允许移除过期日志或重新上传；不得包装成 502 并锁定原请求 |
| `AGENT_FOLLOWUP_LIMIT_EXCEEDED` | 服务端资源或停止请求额度已满，按返回原因提示稍后重试 |
| `AGENT_FOLLOWUP_CONTEXT_LIMIT` | 参考资料超过可保存范围，请新建对话并提供本次相关材料；不得静默丢弃历史后重试 |
| `AGENT_FOLLOWUP_FAILED` | 展示交接失败，不伪装成报告答案，不自动新建或回退旧接口 |
| `AGENT_FOLLOWUP_INVALID_CURSOR` | 分页游标无效返回 400，重新读取最新页；事件游标超出范围返回 409，重新 GET 快照并续订独立事件流 |
| `AGENT_FOLLOWUP_NOT_FOUND` | 提示追问不存在或已删除 |
| `AGENT_IDEMPOTENCY_CONFLICT` | 核对原 ID 和文字，不将冲突请求换 ID 自动补发 |

服务重启时，仍处于 QUEUED、尚未开始且来源记录完整的已受理追问，会在功能启用时继续交接。如果功能关闭，排队任务取消。RUNNING 状态无法确认提交结果时标为 INTERRUPTED，不自动重跑；已提交的诊断继续读取原 diagnosis_run_id，不能再次创建。取消、失败和中断都保留在追问记录中。正式诊断的恢复沿用原任务规则，新报告发布前不覆盖旧报告。

历史普通回答的 context_mode 为 REPORT_ONLY 或 REPORT_AND_LOGS，分别表示当时只依据报告和问答，或可读取原日志副本。旧 REPORT_ONLY 答案仍显示“未重新核对原日志”，原日志后来就绪也不能改成已经核对。新交接项继续保留字段兼容，页面不把它解释为新报告的证据范围。

长对话不会删除较早问答。新 Generic 工作区从只读 inputs/followup-context.json 按需查阅背景，网页继续按原分页接口展示历史，无需读取服务端文件。旧 CLI 会话目录的保留不代表新任务会恢复旧模型状态。

新网站在诊断中发送补充时必须携带保存的 `target_run_id`，把发送固定到仍未交付报告的当前轮。协议仅为兼容旧客户端允许省略此字段。报告刚好生成或轮次变化时会返回 `409 / AGENT_RUN_CHANGED`；先重新读取同一 `run_id`，确认报告已生成后将文字及已上传附件 IDs 送到 followups，不能重发一条不带目标轮次的消息。独立问题使用新建对话。追问失败也不能退回旧消息接口。

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

运行 `node examples/website-agent/preview.mjs` 可检查追问表单、历史答案、停止、刷新及版本切换。预览还保留旧只读问答和 WAITING_INPUT 的兼容场景，不能作为“每问都进入通用 Skill”的行为验收。预览是浏览器合成数据，刷新整个页面会重置模拟服务，不证明后端持久化。

统一追问页面专项验证新请求的通用诊断提示、交接后只展示新轮状态、非最新版和忙碌提示，以及旧答案原样可读。continuation.test.mjs 保留旧草稿接口、跨刷新重试和版本比较；旧 FOLLOWUP 草稿发送到同一追问入口后也遵循新流程。

`examples/website-agent/followup.test.mjs` 由原有 `server.test.mjs` Gate 导入，覆盖 DTO/轮次校验、BFF 登录和 owner、SSE 分片回放、游标越界恢复、响应丢失重试、迟到回执、旧页覆盖竞态、跨报告停止、安全文本展示及离线预览。正式验收仍使用仓库 Test Flow 的 `verdict.json`；这些确定性测试不调用模型，也不替代真实 Generic Skill 对转向问题、日志证据和资料不足场景的验证。
