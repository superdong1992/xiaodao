# 网站接入：报告后的连续追问

报告交付后，用户可以继续要求解释、质疑结论或补充文字。每次回答都追加到原会话中，绑定当前选中的报告；正式报告、下载产物和赞踩保持原样。独立定位的新问题使用“新建对话”，不要把追问发到旧 `messages` 接口，也不要在追问失败后自动回退到它。

首版支持 Generic V2 和默认 `skill-direct` 生成的 Markdown 报告。`strict` / `advisory` 结构化报告不在首版范围内。旧的适用报告也可追问：若原日志副本缺失，服务端只依据报告和已有问答回答，并明确提示：未重新核对原日志。以 GET 返回的 `can_ask` 和 `reason` 为准，不在前端按报告标题猜测资格。

功能默认关闭，服务端配置 `REPORT_FOLLOWUP_ENABLED=true` 后启用。`REPORT_FOLLOWUP_SNAPSHOT_BYTES` 默认 1073741824，控制单份原日志副本的大小；可调低，最大 1 GiB。`REPORT_FOLLOWUP_STORAGE_BYTES` 默认 5368709120，控制副本总量，不能小于单份上限。副本异步准备失败或超限不会撤销已交付报告，页面仍可提供仅依据报告的问答。模型命令沿用 `DIAGNOSE_CLAUDE_COMMAND`，未设置时回退到 `CLAUDE_COMMAND`。提交追问会调用真实模型；读取、分页、订阅和恢复状态不会调用模型。

普通回答支持独立赞踩；点踩后的文字负反馈使用独立 feedback/comments 接口，不经过追问入口。既有 intent=CORRECTION 仍用于用户主动继续对话。带 `diagnosis_run_id` 的交接说明不是诊断结论，不接受赞踩或文字负反馈，也不提炼为经验。原报告评价不变；接入方需要更新的文件、请求示例和经验采纳边界见[回答反馈增量适配说明](website-answer-feedback-upgrade.md)。按钮和输入框由网站实现。

## 继续诊断与报告版本

报告页只提供一个追问入口，用户可提问、更正信息或补充日志。xiaodao 在同一次追问模型调用中核对原问题、报告、历史问答和可用日志，再决定下一步；不要求用户先判断应当“追问”还是“继续诊断”。

- 已有材料足以直接回答时，返回 `ANSWER`，页面展示其中的 Markdown 答复，不创建 Case 或 run，只有一次追问调用。
- 已有材料足以继续排查，但需要诊断 Skill、重新解析日志或执行验证时，模型返回内部 `CONTINUE_DIAGNOSIS` 决策。后端校验当前状态后，在同一事务中创建关联 run 并保存 `diagnosis_run_id`，之后由原诊断流程推进。不会再调用一个分类模型，也不经过第二个页面入口。
- 仍缺少必须由用户提供的材料时，返回 `REQUEST_INPUT`，在当前问答中说明需要补充什么；用户继续在同一入口回复或上传日志，不先启动空跑的诊断。

新诊断支持 Generic V2、skill-direct Markdown，从最新已发布报告建立关联轮次。旧报告仅作为旧判断的只读参考，不继承旧 Evidence、Outcome 或事实 ID。`FollowupItem.run_id` 始终指向原报告，`diagnosis_run_id` 指向新轮次；普通回答和旧记录该字段为 null，旧记录还可能没有此字段。交接项的 `answer_markdown` 只说明已经开始诊断，不能展示成新版报告。页面读取新轮进度，直到新报告发布才更新版本列表；失败、中断或尚未发布时保留旧报告、下载和历史问答。

如果关联新轮返回 `WAITING_INPUT`，在同一追问记录中展示后端 `current_questions`，继续使用原输入区收集回答和日志。此次补充发送到现有诊断消息接口，并固定 `target_run_id=diagnosis_run_id`；不能再发往旧报告的 followups，也不能为尚未生成报告的新轮挂载报告追问。该场景遇到 `409 / AGENT_RUN_CHANGED` 时只刷新原目标状态，不自动改发追问。前端只使用后端状态和轮次标识，不自行判断意图。新报告就绪后，沿用 `onDiagnosisReady` 回调查看报告并更新版本选择器。

补日志先使用原附件预约与上传接口，上传完成后将同会话的 `attachment_ids` 与文字一起提交到 followups，最多 20 个。原请求 ID、文字、纠正目标和附件 IDs 一并保存，网络重试不得更换。原日志与新增材料的最终采用范围由服务端核对；不能静默丢弃已提交日志。读取、刷新和事件回放不调用模型，也没有独立的报告比较模型。

### 如何决定回答、补材料或继续诊断

这里的判断由 xiaodao 追问模型结合当前资料完成，后端负责校验和执行。前端只展示后端状态，不能根据“分析”“重查”“不对”等词决定是否开案，也不能根据答案里出现了 JSON 就执行动作。

判断先看用户实际要求，再看完成这个要求需要什么材料和能力。用户只是在讨论假设时，可以解释该假设成立的条件；不能把假设当成已发生的事实。用户明确要求重新检查全部材料、重新执行验证或更新正式报告时，不能只复述旧报告来替代这项要求。

| 当前问题与材料情况 | 处理方式 | 典型例子 |
| --- | --- | --- |
| 解释术语、引用依据、比较报告中的判断，现有信息已经能支持答复 | `ANSWER`，直接说明并区分结论和限制 | “报告中的 3 秒指什么？”、“为什么优先怀疑连接池？” |
| 讨论假设或质疑推理，还没有要求核验实际故障，也无需新工具即可解释 | `ANSWER`，说明假设和待验证条件 | “假如是 DNS 问题，会出现哪些现象？”不等于确认本次就是 DNS 问题 |
| 要判断实际故障，但缺少只能由用户提供的关键材料；重复处理现有材料也无法补足 | `REQUEST_INPUT`，明确列出缺什么、用途是什么 | “请核对服务端原因”，但当前没有对应时段的服务端日志 |
| 已有材料可支持下一步，回答需要重新解析、调用诊断 Skill 或验证尚未核对的原因 | `CONTINUE_DIAGNOSIS`，创建关联诊断并执行检查 | 原报告只检查客户端，现有服务端日志足以继续核对排队原因 |
| 用户明确要求完整重查或更新正式报告，必要材料已经具备 | `CONTINUE_DIAGNOSIS`，按完整目标生成新报告 | “使用这些日志重新完整定位，给出更新后的正式报告” |
| 用户要求完整重查，但关键日志缺失、已清理或无法确认采用范围 | 先 `REQUEST_INPUT`，材料补齐后再决定是否交接 | “重查全部日志”，但关键包已到期且没有替代材料 |
| 关联诊断仍在执行或等待补充 | 继续显示该轮状态；等待补充时向该轮提交，不重复创建诊断 | 新轮已要求故障时间，用户在原输入框回复“10:05” |
| 最新关联诊断失败或中断后，用户补充材料并再次提问 | 带上该次尝试的有效用户信息重新判断；需要工具时新建关联尝试 | “刚才补了时间，再用这份新日志继续查”不能退回只看最初问题 |

这些规则的优先关系是：先确认用户要的是解释还是实际检查；实际检查缺少用户专有材料时先索取；材料足够且明确要求重查或确实需要诊断工具时交接；其余能由现有资料回答的问题直接回答。表达不清时先澄清，不擅自扩大为完整重查。日志快照尚未就绪本身不构成重诊断理由；新增日志也只是可用材料，不能仅凭文件名就宣称已核实内容。

### 内部决策与网页展示分开

三种结果都由原有的一次追问调用返回，使用同一个严格的内部结果包：`protocol` 固定为 `xiaodao.followup.v1`，`request_key` 必须等于当前任务的 `followup_id`，`action` 指定处理方式。`ANSWER` 与 `REQUEST_INPUT` 携带要显示的 `answer_markdown`；`CONTINUE_DIAGNOSIS` 必须给出完整的 `effective_problem_text`、`diagnosis_goal`、`reason` 和最终 `attachment_ids`，没有 `log_mode`。后端核对协议、追问身份、字段与材料范围后才执行。错误身份、缺项、未知动作或不合法的结果包不能启动诊断，也不能作为普通答案原样展示。

回答中的 JSON 示例、字段名 `action` 或文字 `CONTINUE_DIAGNOSIS` 都只是正文。浏览器只渲染公开 `FollowupItem.answer_markdown`，不解析模型结果包，不根据代码围栏或正文决定跳转。真实交接以持久化的 `diagnosis_run_id` 为准；交接说明由后端在成功创建新轮后保存，不能提前显示“已开始诊断”。这些内部协议不增加公开 MCP 输入，也不改变网站原有的追问请求形状。

该流程不增加分类模型，不增加报告比较模型，也不在协议错误后自动修补并再次调用。普通答案和索取材料各只有一次追问调用；继续诊断则在这次判断后执行必要的正式诊断工作。新 run 和新 Case 用于保存本轮输入、执行过程和新版报告，避免把旧报告或未完成的尝试改写成新结果。

### 当前事实、旧报告和失败尝试如何承接

传给模型的材料需要保留来源区别：当前有效用户信息用于理解故障；旧报告只代表上一轮判断；历史回答、假设和失败模型输出都不能变成已经证实的事实。用户明确更正的信息应按时间和对象核对，不能因为旧报告重复了一个判断，就把它置于当前用户更正之上。

除选中报告的有效输入和追问历史外，只承接该报告最新一个关联、尚未发布报告的诊断尝试。包括其状态、安全失败说明、待补问题，以及用户在该轮提交的文字和日志。该次尝试已采用的完整整理输入单独放在 `latest_attempt.effective_problem_text`，使连续失败后仍能保留此前已采用的更正；它不是新的用户确认或日志证据。真实用户补充单独放在 `user_messages`，不把创建诊断时生成的整理消息再注入其中。

已采纳的用户事实与未采纳的补充必须区分。`QUEUED` 消息仍作为用户陈述，其附件可进入候选清单，但不能标成诊断已确认的信息；`UNUSED` 消息保留状态供解释，其附件不进入候选清单。不拼接所有失败轮次，也不把其他问题或更早尝试已放弃的材料自动带回来。长 `user_messages` 超出内联预算时，有 `read_search` 能力的执行器可通过 Read/Grep 按需读取只读 `inputs/diagnosis-user-messages.json`，不增加摘要模型。准入时计入实际上下文；没有检索能力且超限时提前返回 HTTP 413 / `AGENT_FOLLOWUP_CONTEXT_LIMIT`，不能接收后才因相同预算失败。

例如，父报告的问题是“10:00 客户端超时”，新轮等待输入时用户补充“实际是 10:05，服务端也异常”，之后新轮执行失败。用户再问“结合刚才补充继续查”时，模型应同时知道新的时间、服务端现象和上次失败状态；旧报告仍独立保留。若需要再次诊断，本轮完整问题采用仍有效的用户信息，旧报告作为对比参考传入，不继承旧 Evidence、Outcome 或事实 ID。必要资料已被清理时明确索取，不虚构已恢复的上下文。

### 日志按最终清单选择

网页请求的 `attachment_ids` 表示这次新提交的日志；内部 `CONTINUE_DIAGNOSIS.attachment_ids` 表示本轮诊断最终采用的完整集合。两者用途不同。模型只能从后端提供的候选材料中选择，候选来自原报告、该报告的追问上传，以及上述最新关联尝试的用户材料。后端校验归属、可用性、数量、大小与哈希，未知或跨会话 ID 不能执行。

最终清单不再只用“追加”或“替换”两个模式推断。例如，最初使用 A，用户随后要求 B 替换 A，又补充 C，最后只补时间，本轮应选择 B、C，不能恢复 A，也不能只留下 C。用户要求排除某个旧包时，最终清单应明确不含它。历史包已过期但本轮不需要它时，不应阻止采用其他可用材料；如果它仍是必要证据，则索取替代日志。未采用的历史材料仍留在记录中，不因排除而删除。

只有最终选中的日志进入准备流程，按 SHA-256 去重。新通用诊断直接准备文本和归档中的可读文本；专用诊断逐包调用现有 Logparse。最终仍最多 20 份文件、总计 5 GiB，并受原有上传资源配额约束。追问模型只读取原报告对应的只读快照，不直接打开新上传文件；需要核对新日志时交给正式诊断。旧草稿 API 的 APPEND/REPLACE 规则继续保留，见下节；它们不代表新版统一追问只能表达这两种材料选择。

### 失败展示、恢复和验证范围

页面先显示明确的终态：失败或中断展示后端安全失败说明，停止显示已停止；只有未结束的诊断才显示“正在解析”等进度。旧的进度事件不能遮住失败原因。原报告继续可读，用户可在同一输入区补充后再次提问；读取、刷新、重连和失败提示本身都不会自动启动新诊断。

这里保证的是结果包校验、材料归属、状态流转和恢复路径。模型仍负责语义判断，提示词与确定性替身测试不能证明它在所有真实故障上都能正确判断材料是否充分、选全日志或给出正确根因。真实模型准确率需要另外用代表性案例评估；本次确定性验收不作此承诺。具体执行边界和测试范围见[报告追问验证说明](report-followup-testing.md)。

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

日志按 SHA-256 去重。新 Generic Job 使用 `direct-v1` 直接读取 UTF-8 文本或安全展开归档中的文本，旧 Generic Job 仍使用创建时固定的 `parse-only`；skill-direct 的 `parse-targets` 不变。合并清单及追问快照保留附件 ID、原名、成员路径、跳过原因和处理范围。没有可读日志的新通用报告按文字分析，追问也明确标为仅依据报告和文字。最终清单最多 20 份文件、总计 5 GiB；上传仍受既有会话累计资源配额约束。旧文件已清理时，选择 REPLACE 并重新上传所需材料。报告正文仍可阅读和对比。

版本接口返回 `root_run_id`、`latest_run_id`、`can_continue` 和 `versions`。每项包含 `run_id`、`parent_run_id`、`version`、`created_at`、`input_changes`、`diagnosis_goal`、`runtime_changes`。只有成功发布的报告进入版本列表；失败轮次仍保留在会话历史中，父报告可以再次继续诊断。Skill、模型执行配置和解析器变化使用保存的元数据比较；模型配置指纹不代表远端实际解析出的模型身份，缺少历史记录时明确提示无法比较。

新报告以“本轮变化”说明哪些旧判断得到支持、修正或撤回，引用旧版段落及本轮实际核对的日志；之后保留完整当前诊断。未提及的旧判断不视为撤回。页面按两个指定 `run_id` 读取原报告并排展示；语义变化直接来自新报告，没有 compare 服务或额外模型任务。

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
| `AGENT_RESOURCE_EXPIRED`、`AGENT_ATTACHMENT_EXPIRED` | BFF 保留 HTTP 410。提示日志已清理或附件已失效，清除已被明确拒绝的待重试请求，允许移除过期日志或重新上传；不得包装成 502 并锁定原请求 |
| `AGENT_FOLLOWUP_LIMIT_EXCEEDED` | 每份报告最多 100 条问答；提示新建对话 |
| `AGENT_FOLLOWUP_CONTEXT_LIMIT` | 单次提示最多 262144 字节；支持 Read/Grep 时，较长历史保存在完整历史文件中，提示放入索引和可容纳的最近问答。仍无法满足限额时拒绝提交 |
| `AGENT_FOLLOWUP_INVALID_CURSOR` | 分页游标无效返回 400，重新读取最新页；事件游标超出范围返回 409，重新 GET 快照并续订独立事件流 |
| `AGENT_FOLLOWUP_NOT_FOUND` | 提示追问不存在或已删除 |
| `AGENT_IDEMPOTENCY_CONFLICT` | 核对原 ID 和文字，不将冲突请求换 ID 自动补发 |

服务重启时，仍处于 `QUEUED`、尚未开始且资源仍可用的已受理追问，会在功能启用时继续执行，即使此时刚超过 15 天期限；资源清理须等任务结束。如果功能关闭，排队任务会取消。原先处于 `RUNNING` 的任务无法确认模型执行结果，会标为 `INTERRUPTED`，不会自动重跑；期限内可新发一条问题。已完成的交接读取原 `diagnosis_run_id`，不能再次创建新轮次。取消、失败和中断也保留在问答记录中。追问可提交补充日志，但不会覆盖旧正式报告；需要诊断时另行发布关联新版。专用 `strict` / `advisory` 报告追问不属于首版能力。

每条回答的 `context_mode` 为 `REPORT_ONLY` 或 `REPORT_AND_LOGS`。前者只依据报告和已有问答；后者可读取原日志副本。日志后来准备好，也不能把较早的 `REPORT_ONLY` 回答显示成已核对日志。

长对话不会删除较早问答。支持只读 Read/Grep 的运行命令可从 `inputs/history.md` 查阅完整历史，单次提示同时提供历史索引和能容纳的最近问答。命令不支持此能力，或原问题、报告与当前追问仍超过单次提示限额时，返回 `AGENT_FOLLOWUP_CONTEXT_LIMIT`。网页继续按原分页接口展示完整问答，不需要读取服务端工作区文件。

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

运行 `node examples/website-agent/preview.mjs` 后，选择“通用 Markdown”或“旧报告追问”，可体验连续提问、仅依据报告回答、停止、刷新记录及报告切换。“新建对话并开始定位”会调用 create 再 send，追问表单只调用 followups。预览是浏览器内的合成数据，刷新整个页面会重置模拟服务，不能用于证明服务端持久化。

统一追问预览用于检查普通答复、补日志、诊断交接和版本对比的页面状态。离线合成结果只验证接入和展示，不能证明线上模型判断正确。`continuation.test.mjs` 保留旧草稿兼容、纯追问不能确认、跨刷新重试与原报告版本不变的覆盖；统一入口还应检查重复交接事件和刷新不重复开案。

`examples/website-agent/followup.test.mjs` 由原有 `server.test.mjs` Gate 导入，覆盖 DTO/轮次校验、BFF 登录和 owner、SSE 分片回放、游标越界恢复、响应丢失重试、迟到回执、旧页覆盖竞态、跨报告停止、安全文本展示及离线预览。正式验收仍使用仓库 Test Flow 的 `verdict.json`；这些确定性测试不调用模型，也不替代真实 Agent 的只读日志验证。
