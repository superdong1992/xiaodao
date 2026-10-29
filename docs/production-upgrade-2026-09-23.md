# 2026-09-23 以来的生产环境累计升级清单

本清单覆盖 2026-09-23 00:00（UTC+8）至 2026-10-16 的累计改动，以此前主干提交 `c37c4f5` 为基线。除 PostgreSQL、Redis 鉴权、严格路由和报告追问外，也包含持续诊断与报告版本、回答反馈、Web 正文永久保存、日志资源回收，以及 `b465fb2` 的三项性能优化。以包含本清单的最终主干提交为部署单位，不能只挑选单个文件。

这些版本的软件包号仍为 `8.2.0`，当前合同为 `v11-contract-r3`。部署记录必须保留 Git 提交、依赖锁文件和源码校验结果；不能用 `/openapi.json` 中的版本号判断是否已经完成本次升级。9 月 21～22 日的功能背景见[前期适配清单](website-agent-changes-2026-09-22.md)，其中七天保留的旧规则已被下文替代。

## 按接入范围核对变化

| 变化 | 生产需要完成的操作 | 未适配的结果 |
| --- | --- | --- |
| PostgreSQL 替换 SQLite | 准备 PostgreSQL 15 或更新版本；配置 `DATABASE_URL`；有历史数据时停服后显式导入新数据库和新资源目录 | 缺少连接配置无法启动，旧 SQLite 目录也不能直接打开 |
| 网站默认 Redis 鉴权 | 配置 Redis，BFF 透传 Cookie，核对 `user.userid` 与原用户归属 | Cookie 无效返回 401；Redis 未配置或不可用返回 503；身份变化会导致旧会话不可见 |
| 专用路由准入收紧 | 从原 Wiki 重新生成带适用／排除条件的注册 V2，并同步自定义 ROUTE 包装器 | 注册 V1 仍能加载，但不会自动进入专用定位；旧模型响应可能变成协议错误 |
| 报告追问与消息轮次绑定 | 同步前后端模块；诊断补充携带 `target_run_id`；完成验收后再打开追问开关 | 缺模块会导致加载失败；未区分入口可能重复提交或发往错误轮次 |
| Web 正文与日志分开保留（必改） | 核对 15 天资源期限和磁盘阈值；前端处理轮次只读、过期附件、ZIP 隐藏和 SSE 游标恢复 | 旧页面可能展示失效下载，或反复重试已清理资源 |
| 持续诊断与版本对比（界面可选，模块依赖必改） | 同步 continuation 合同、BFF 与同源哈希 Worker；按草稿确认创建新版报告 | 混用旧模块可能加载失败；直接把补日志发到追问接口会被拒绝 |
| 回答评价与文字纠正（可选） | 按具体回答保存赞踩，纠正使用原追问接口；经验库另行启用 | 只更新服务端不会自动增加网站按钮，评价原报告与评价回答不能混用 |
| 登录后的只读分享（可选） | 使用 `share=true`，接收者仍须登录；分享页隐藏写操作 | 分享链接不是匿名链接，也不授予修改、追问或删除权限 |
| 长期统计（外部接入项） | 已采用参考包的部署同步刷新和补录逻辑，计入报告追问 | 仅升级 Server 不会自动建统计表、挂接写入或刷新 Dashboard |

Server 只支持 Linux，使用 Python 3.12。同步源码、`pyproject.toml` 和 `uv.lock` 后执行 `uv sync --frozen`；本次新增依赖包括 psycopg、连接池与 Redis 客户端。示例 BFF 使用 Node.js 24 或更新版本。

## 1. PostgreSQL、资源目录与历史数据

生产最低版本为 PostgreSQL 15，建议使用所选主版本最新的维护小版本。Release 固定使用 PostgreSQL 17.11 镜像，以复现验证环境；该镜像版本不代表最低部署版本。

### 配置与运行边界

```dotenv
DATA_ROOT=/srv/xiaodao/data-postgresql
# 示例地址；实际账号、密码和 TLS 参数由生产配置管理注入。
DATABASE_URL=postgresql://problem_locator:change-me@db.internal:5432/problem_locator
DATABASE_POOL_SIZE=8
REPORT_FOLLOWUP_ENABLED=false
```

`DATABASE_URL` 必填，生产入口不会自动回退 SQLite。数据库必须专用；初始化账号需要在 `public` 创建表、索引和 identity 序列的权限。连接池默认上限为 8，可配置 2～32；此外还占用一条实例锁连接，数据库连接预算需加上这条连接及运维余量。

仍只支持单实例、单 Server 进程和单 Uvicorn worker。数据库中的会话锁保护进程所有权，活动 Case 仍在内存中；换库不代表可以启动多副本。若使用数据库连接代理，必须保持会话语义，不能使用会丢失 session advisory lock 语义的事务池模式。

PostgreSQL 保存业务记录，`DATA_ROOT` 继续保存报告、日志、附件和快照。二者绑定安装 ID 及解析后的绝对目录路径。迁移后的目录不要改名，容器内挂载路径也不能随意更换；不要手改格式标记绕过检查。

### 选择升级路径

| 当前数据 | 升级路径 |
| --- | --- |
| V11 r2 或 r3、Agent storage v2 的 SQLite | 直接执行 PostgreSQL 导入 |
| V11 r1 / r2、Agent storage v1 的 SQLite | 先按[离线格式升级说明](data-upgrade-v11-r2.md)生成新的 V11 r3、Agent v2 SQLite 中间目录，再对中间目录执行 PostgreSQL 导入；不能在中间步骤启动当前 Server |
| V1～V10、旧 `state.json` 或旧 StateExport | 当前工具不支持直接导入，不得强改标记 |
| V11 r2 或 r3 的 PostgreSQL | 排空、停服并备份后，保留匹配的数据库、安装 ID 和原 `DATA_ROOT` 路径，直接启动新版；不执行 SQLite 导入 |
| 全新安装，无历史数据 | 使用专用空数据库和新的空 `DATA_ROOT`，由 Server 首次启动初始化 |

若旧格式升级需要 `--ownership-map`，映射结果必须与当前登录身份及命名空间一致。未分配归属的历史会话不会自动出现在用户目录中。

现有 r2 PostgreSQL 的外层 State 合同头在读取时映射为 r3，历史快照、目录标记和报告字节不因此重写；新快照使用 r3。State schema 11、报告 schema 3、Agent storage 2、会话详情 3、公开事件 2 保持不变。反馈、追问、报告归档等私有表的增量升级由启动流程完成，账号仍须有相应建表和索引权限。不要手改版本标记或重新创建生产库。写入新数据、更新私有存储标记后，旧程序不能直接接管；回退须恢复升级前匹配的数据库与资源备份。兼容范围见[PostgreSQL 迁移说明](postgresql-migration.md)。

### 停服、备份、导入、启动

先停止接收新任务，等待诊断、消息处理、追问、快照、经验任务、上传和清理结束，再停止旧 Server。强制杀进程不能替代排空，导入工具会拒绝未结束的任务。

保存旧程序、配置及完整源目录备份，包括 SQLite、仍存在的 WAL/SHM、报告和附件。提前记录几份历史报告与下载文件的哈希，供切换后核对。目标目录必须尚不存在、父目录已经存在，且不能与源目录互相包含；目标数据库必须为空。

以下命令仅适用于 SQLite 迁移，假设源目录已经是 V11 r2 或 r3、Agent v2，且目标 `DATABASE_URL` 已安全注入进程环境。先查看计划，再执行；已有 PostgreSQL 部署跳过这些导入命令：

```bash
uv run problem-locator-postgres-import \
  --source-data-root /srv/xiaodao/data-sqlite-v2 \
  --target-data-root /srv/xiaodao/data-postgresql \
  --database-url-env DATABASE_URL \
  --plan-only

uv run problem-locator-postgres-import \
  --source-data-root /srv/xiaodao/data-sqlite-v2 \
  --target-data-root /srv/xiaodao/data-postgresql \
  --database-url-env DATABASE_URL \
  --execute
```

`--plan-only` 只检查源文件清单，不连接数据库，也不验证业务记录和引用资源。执行成功须同时满足输出 `status: IMPORTED` 和退出码 `0`。工具复制源目录后才打开 SQLite 副本，核对每张表的数量与内容摘要；源 SQLite、WAL 和资源不作修改。保存目标目录中的 `postgresql-import.receipt.json`。

导入成功后，同时切换 `DATA_ROOT` 和 `DATABASE_URL`。服务尚未启动时先校验、导出，再启动：

```bash
uv run python -m problem_locator validate-state \
  --data-root /srv/xiaodao/data-postgresql

uv run python -m problem_locator export-state \
  --data-root /srv/xiaodao/data-postgresql \
  --output /srv/xiaodao-backup/import-check.json

uv run python -m problem_locator serve \
  --env-file /opt/xiaodao/service.env
```

先创建示例中的备份输出目录。`serve` 使用配置文件时须显式传入 `--env-file`，也可由进程管理器直接注入环境变量；不会自动读取 `.env`，同名进程环境变量优先。离线校验、导出和导入命令读取进程中的数据库配置，不能只修改 `service.env` 就执行它们。

导入失败时，保留源目录、目标数据库、暂存目录及收据，不删除未完成标记。修正原因后，换另一个空数据库和另一个新目录重试。详见[PostgreSQL 迁移说明](postgresql-migration.md)。

### 备份和回退边界

切换前可回退旧程序与原 SQLite 目录。切换后产生的新数据不会自动同步回 SQLite，不能忽略这部分数据直接回退。

上线后，PostgreSQL 备份和完整 `DATA_ROOT` 应来自同一次停服窗口。数据库可用 `pg_dump --format=custom` 导出；恢复时必须恢复匹配的资源目录并保留绑定路径。实际 TLS、权限、备份恢复及容量须在生产环境验证。

## 2. Redis 登录、Cookie 与历史会话归属

`WEBSITE_AUTH_MODE` 默认改为 `redis`。在 xiaodao 的私有配置中填写：

```dotenv
WEBSITE_AUTH_MODE=redis
WEBSITE_REDIS_HOST=redis.internal
WEBSITE_REDIS_PORT=6379
WEBSITE_REDIS_DB=0
WEBSITE_REDIS_SSL=false
WEBSITE_SESSION_COOKIE_NAME=sessionid
WEBSITE_OWNER_NAMESPACE=xiaodao-website
# 如需认证，安全注入 WEBSITE_REDIS_USERNAME / WEBSITE_REDIS_PASSWORD。
```

主机名、端口、数据库、认证和 TLS 按实际环境填写。服务端读取 Cookie 的 `sessionid`，查询固定前缀 `airobot2-session:{session_id}`，要求 JSON 中的 `user.userid` 是非空字符串，保留工号前导零。Cookie 名可配置，Redis 键前缀不能自行替换。

`owner_key` 根据 `WEBSITE_OWNER_NAMESPACE` 与工号计算。切换前核对原 BFF 的 `user.id` 与当前 `user.userid` 是否一致，并保留原命名空间；不一致会让旧会话不可见，不能用重新创建用户或放开归属校验掩盖。必要的归属映射须在上线前确认。

默认 BFF 透传 Cookie，并忽略浏览器自带的 `X-Agent-Owner-Key`。采用 Redis 模式时移除 `WEBSITE_AUTH_MODULE` 和自定义 `createAgentBackend({ access })` 的 `access` 参数。暂时保留旧登录模块时，后端必须显式配置 `WEBSITE_AUTH_MODE=trusted_header`，与该 BFF 成对部署；两种模式不要混搭。

网站同源 `/api/agent/` 代理必须保留 Cookie，Cookie 的 Domain、Path、Secure 和 SameSite 设置须适合实际站点。上传、下载、诊断 SSE 和追问 SSE 都使用同一登录身份。浏览器继续访问同源 BFF，不改为直接跨域 Cookie 请求 xiaodao。

网站或网关原有的 CSRF / Origin 校验必须保留。默认 Redis BFF 不验证 `X-CSRF-Token`，Redis 会话校验也不能替代这些保护；浏览器设置请求头不代表服务端已经校验。

`/ready` 不检查 Redis。必须另外用有效登录 Cookie 读取用户目录、创建一个空会话并读回；这不调用模型。缺失或过期 Cookie 返回 401，Redis 地址未配置或不可用返回 503。详见[Redis 部署说明](website-redis-deployment.md)。

## 3. 专用路由注册和自定义 Agent

所有生产专用注册应使用 `registration-template.json@2`，声明 `routing.applicability`（1～16 条）和 `routing.exclusions`（0～16 条）。从原 Wiki 重新生成完整注册目录，在新目录校验后切换 `SKILL_DIR` 并重启。生成说明见[注册元 Skill](../.claude/skills/wiki-to-logparse-diagnosis-skill/SKILL.md)。

条件应描述产品和问题的适用范围。日志缺失、时间未填或进程名待补齐属于后续诊断材料，不应直接写成范围排除条件。旧注册 V1 仍可加载，但不参与自动专用路由；全部为旧注册时直接走通用定位，不调用 ROUTE 模型。

ROUTE 响应现在必须包含 `skill_id`、`reason`、`confidence` 和 `assessments`，逐一评估候选及条件。仅当唯一候选满足全部适用条件、排除条件不成立、其他候选被明确排除且置信度至少为 `0.95` 时准入。未知、低分或歧义会转通用；非法 JSON、未知 ID、缺项等协议错误仍为 `OUTCOME_INVALID`，不会自动重试或降级。

同步修改自行维护的 ROUTE 包装器、响应 mock 和告警规则，避免继续发送旧三字段响应。此准入门槛不受 `METHODS_EVIDENCE_VALIDATION` 或 Reviewer 开关影响。七个公开 MCP 工具和 Core REST 的输入格式没有因此新增字段；客户端仍经 HTTP 直连 Linux Server，不安装本地代理或 Hook。

## 4. 报告追问与网站发布

先保持 `REPORT_FOLLOWUP_ENABLED=false`。支持 Generic V2 和默认 `skill-direct` 的 Markdown 报告；`strict` / `advisory` 结构化报告不支持。旧报告没有日志快照时可按 `REPORT_ONLY` 回答，页面须说明没有重新核对原日志，不重新解析旧日志。

同步部署整个[网站示例模块](../examples/website-agent/README.md)：BFF 的 `server.mjs` 依赖 `followup-bff.mjs`、`followup-contract.js`、`continuation-bff.mjs`、`continuation-contract.js`、`feedback-comments-bff.mjs` 和 `feedback-comment-contract.js`；新版 `browser-client.js` 也依赖这三份合同模块。即使功能暂未开启，也不能只复制入口文件。启用界面时一并接入 `followup-controller.js`、`followup-view.js`、报告视图和 `conversation-input.js`。统一追问的日志上传还直接依赖 `log-upload-controller.js`、`file-sha256.js` 和 `file-sha256-worker.js`；报告版本对比和旧草稿兼容仍需 `continuation-controller.js`、`continuation-view.js`。哈希文件保持同一目录，网站 CSP 允许同源 Worker。

前端必须完成以下适配，细节见[追问接入说明](website-report-followup.md)：

- 诊断补充发送保存的 `target_run_id`。收到 `409 / AGENT_RUN_CHANGED` 后刷新对应轮次，再决定是否转为追问，不重发不带目标轮次的消息。
- 追问绑定当前显示的 `selected_run_id`，不能总绑定 `current_run.run_id`。失败不能自动回退 `messages`；重试保留原请求 ID、文字和已选入口，防止重复执行。
- 先 GET 追问快照，再用其 `last_event_id` 订阅独立的追问 SSE。追问事件 schema 为 1，诊断事件为 2，游标不能混用。代理关闭 SSE 缓冲，保留 `Last-Event-ID`。
- 每个会话最多一个活动追问。刷新页面不会停止模型；停止必须调用 stop。重启后排队任务可继续，运行中的任务标记 `INTERRUPTED`，不自动重跑模型。

追问沿用 `DIAGNOSE_CLAUDE_COMMAND`，否则使用 `CLAUDE_COMMAND`。自定义命令须验证追问协议与工具策略；不能假设任何兼容 CLI 都支持只读 Read/Grep 和原日志访问。

`REPORT_FOLLOWUP_SNAPSHOT_BYTES` 默认 1 GiB，`REPORT_FOLLOWUP_STORAGE_BYTES` 默认 5 GiB，应按磁盘容量配置。失败或超限的快照不撤销原报告，可退为仅按报告回答。关闭功能后停止接收新追问，排队任务取消，历史记录仍可读取。

## 5. 正文永久保存、资源期限与磁盘保护

Web 用户消息、助手问题和回复、完整报告、历史问答及附件元数据不再自动到期删除；用户主动删除会话时一起删除。原始附件、ZIP 和追问日志快照默认从轮次结束起保留 15 天，磁盘紧张时可提前回收已结束轮次。等待补充的轮次连续 15 天没有业务活动才结束，磁盘压力不会提前结束它。完整规则见[服务端文件保留与清理](storage-retention.md)。

```dotenv
WEB_RESOURCE_RETENTION_DAYS=15
HISTORY_RETENTION_DAYS=30
RESOURCE_DISK_LOW_FREE_BYTES=10737418240
RESOURCE_DISK_LOW_FREE_PERCENT=10
RESOURCE_DISK_TARGET_FREE_BYTES=21474836480
RESOURCE_DISK_TARGET_FREE_PERCENT=15
```

`HISTORY_RETENTION_DAYS` 继续控制非 Web 历史和删除凭据，不再控制 Web 正文。磁盘空闲低于 `max(10 GiB, 10%)` 时触发，回收到 `max(20 GiB, 15%)` 后停止；每 60 秒检查，主要大文件写入前也会检查。小磁盘必须按实际容量调低字节阈值，两项目标值均须分别大于触发值。PostgreSQL 所在盘另行监控，日志盘的检查不能代替数据库容量规划。单附件 2.5 GiB、Case 总额 5 GiB 和原快照上限不变。

首次升级会补齐历史业务活动时间，并在回收对应 Case 前逐轮归档报告原始字节；归档校验失败则保留资源。备份应覆盖升级前的数据库和文件；升级后核对旧报告下载哈希。已经删除的数据不会恢复，已经进入删除清单的任务不会撤销。不得再用旧的七天脚本按目录年龄删除 Web 会话或数据库正文。

网站必须同步处理以下行为：

- 按所选轮次的 `resources_expired` 显示“相关日志已清理，本次回答未重新核对原日志”；报告原地址及历史问答仍可读。隐藏已清理 ZIP 的下载入口，旧地址经权限检查后返回 `410 / AGENT_RESOURCE_EXPIRED`。附件 `EXPIRED` 不能通过旧请求重试恢复。
- 清理时间在 `resources_expired_at`，原因 `resource_cleanup_reason` 区分 `RETENTION_EXPIRED` 与 `DISK_PRESSURE`；共享附件的状态不能代替轮次状态。页面提示的天数须与实际 `WEB_RESOURCE_RETENTION_DAYS` 一致。
- 追问统一使用原生 session；资源清理只影响原日志，报告归档完整时仍可继续交流。输入可用性以追问接口的 `can_ask` / `reason` 为准，不能仅凭 `resources_expired` 禁用。需要正式诊断时校验本次选中的附件，新上传的有效材料仍可使用。已受理任务结束前，清理不会移除其使用中的资源。
- 上传收到 `503 / AGENT_STORAGE_PRESSURE` 时提示稍后重试，不反复发送或扩大额度。删除、停止和失败残留仍由原机制处理。
- 诊断 SSE 返回 `409 / AGENT_EVENT_CURSOR_EXPIRED` 时，先重读会话快照，再以其 `last_event_id` 设置 `Last-Event-ID` 重连；使用可设置请求头的流式 fetch。追问流仍按自己的游标错误恢复，两个流不要混用。

本次性能优化无新增必填配置：附件哈希与导入使用两个有界后台任务名额，历史分页批量查询，压力回收按批读取候选。它们减少跨会话等待和重复查库，不能作为生产磁盘吞吐保证；上线验收仍需同时发送纯文本和大附件，观察页面响应、数据库连接数和磁盘余量。详情参数与异常处理以专项文档为准。

## 6. 持续诊断、回答反馈、分享与统计

### 持续诊断与新版报告

新版网站将报告追问、更正和补日志放在同一入口，启用 `REPORT_FOLLOWUP_ENABLED` 后由同次追问模型核对材料：能回答则直接回答，需要诊断工具则交接关联 run 和 Case，缺少用户材料则在原入口索要。页面按 `diagnosis_run_id` 读取进度；新轮等待补充时展示后端问题，并向该轮提交诊断补充。旧报告保持原样，新报告发布后再更新版本。多包日志按 SHA-256 去重、逐包解析，不能继续假设每轮只支持一份日志。

旧草稿 API 保留兼容，独立于 `REPORT_FOLLOWUP_ENABLED`。旧客户端可按 `can_continue` 从最新报告创建草稿，核对完整问题和日志清单后确认；旧日志已清理时选择 `REPLACE` 并重新上传。自然语言草稿可能返回 `FOLLOWUP`，此时发送原报告追问，不能调用草稿 confirm；直接编辑完整问题不增加草稿整理模型调用。完整路径、字段和幂等规则见[持续诊断与追问接入](website-report-followup.md)，前后端文件及 Worker 要求见第 4 节。自定义模型包装器须同步当前追问决策、INTAKE 输出和多日志输入，不能继续按旧单包格式自行拼装。

### 独立回答评价与文字纠正

原报告赞踩接口保持不变。若需要逐条评价回答，新增 `followups/{followup_id}/feedback` 的 GET/PUT，按会话、轮次、回答分别缓存；纠正走原 followups 提交，发送 `intent=CORRECTION`，指向具体回答时带 `target_followup_id`。点踩本身不自动发送纠正或调用模型。接入示例、重试约束和验收项见[回答反馈增量适配](website-answer-feedback-upgrade.md)。

`GENERIC_MEMORY_ENABLED=false` 仍是默认值，与 `REPORT_FOLLOWUP_ENABLED` 分开控制。开启经验提炼前须验收脱敏和证据核对；只看报告的 `REPORT_ONLY` 回答不应显示为“已核实原日志”，用户点赞或提交纠正成功也不等于经验已采纳。历史评价会保留，新评价入口以 GET 返回的 `can_rate` 为准；不能仅凭历史报告仍可读，就推断仍能提交评价或追问。

### 登录后的只读分享

按[网站示例的分享接入](../examples/website-agent/README.md#分享报告)传 `share=true`；接收者使用自己的登录 Cookie，允许读取被分享的会话或报告。分享页隐藏发送、上传、追问、评价、重命名、删除和继续诊断入口；分享不改变 owner 归属，未带分享参数的普通请求继续校验 owner。不要把它当匿名链接或写权限授权。

### 已接入长期统计的部署

[长期统计接入指南](../usage-stats-reference/INTEGRATION.md)及其 SQL/Python 仍是参考实现，尚未接入 Server 的业务入口、定时任务和清理任务。生产若已采用旧参考包，应同步预检、补录及 writer：消息数要包含 `agent_messages` 和 `agent_followup_tasks`；关闭 run 与追问入库后均须在最外层提交完成后刷新，幂等命中也不能漏刷。无需统计的部署不必执行这些 SQL；需要统计的部署不能把本次 Server 升级当作统计接入完成。

## 上线顺序与验收记录

1. 记录实际旧版本、数据格式、资源路径、用户身份算法、生产注册和 Agent 命令；准备匹配的备份与回退方案。
2. 排空停服并备份。SQLite 按表完成升级和导入，保存收据；已有 PostgreSQL 保留原绑定库与路径，跳过导入。按迁移说明校验后启动新版。
3. 同步后端、BFF、浏览器模块、Worker、Cookie 代理和 Redis 配置；核对资源期限及磁盘阈值，更新注册 V2。首次接入追问或经验库时先保持对应开关关闭。
4. 不调用模型的检查：`/ready`、真实用户 Cookie、旧会话与轮次、报告及附件哈希、历史分页、上传下载、两个独立事件流的鉴权与恢复。检查资源过期提示、ZIP 隐藏、存储不足重试和分享只读权限；重启后再次读取已完成记录。不要在生产删除真实文件来模拟过期，使用隔离验收数据；确认生产没有新建 SQLite 数据库。
5. 按所接入功能验收追问、继续诊断、反馈和统计；同时验证大附件期间其他会话可响应。完成真实 Agent 与容量验收后再开放新增功能。任何真实模型活动先查看[中央 Test Flow](../tools/test-flow/README.md)对应入口的 `--plan-only`，确认身份、调用数、token/cost、Proof、Stage、Gate 和阻塞项；失败重试必须有新的假设与预期证据。
6. 保存部署提交、环境配置版本、迁移收据、备份位置和验收结果。Test Flow 只以 `verdict.json` 为准，普通连通检查或导入收据不能代替正式结论。

当前本地确定性验证不能代替生产数据库的 TLS／权限／备份恢复、真实 Redis 与 Cookie、历史数据导入、Docker 部署、真实模型旅程或负载容量验收。本仓库不包含生产地址和凭据，执行上述步骤时须使用部署环境的实际配置。
