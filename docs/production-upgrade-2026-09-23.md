# 2026-09-23 以来的生产环境累计升级清单

本清单覆盖 2026-09-23 00:00（UTC+8）至 2026-09-28 的累计改动，以此前主干提交 `c37c4f5` 为基线。包含严格路由准入 `343e1ba`、Redis 工号鉴权 `095dade`、报告追问分支 `632f57d` 的合入内容，以及本次 PostgreSQL 切换。以包含本清单的最终主干提交为部署单位，不能只挑选单个文件。

这些版本的软件包号仍为 `8.2.0`。部署记录必须保留 Git 提交、依赖锁文件和源码校验结果；不能用 `/openapi.json` 中的版本号判断是否已经完成本次升级。9 月 21～22 日的通用日志、报告赞踩、经验库和七天保留另见[前期适配清单](website-agent-changes-2026-09-22.md)。

## 必须同步适配的四项变化

| 变化 | 生产需要完成的操作 | 未适配的结果 |
| --- | --- | --- |
| PostgreSQL 替换 SQLite | 准备 PostgreSQL 15 或更新版本；配置 `DATABASE_URL`；有历史数据时停服后显式导入新数据库和新资源目录 | 缺少连接配置无法启动，旧 SQLite 目录也不能直接打开 |
| 网站默认 Redis 鉴权 | 配置 Redis，BFF 透传 Cookie，核对 `user.userid` 与原用户归属 | Cookie 无效返回 401；Redis 未配置或不可用返回 503；身份变化会导致旧会话不可见 |
| 专用路由准入收紧 | 从原 Wiki 重新生成带适用／排除条件的注册 V2，并同步自定义 ROUTE 包装器 | 注册 V1 仍能加载，但不会自动进入专用定位；旧模型响应可能变成协议错误 |
| 报告追问与消息轮次绑定 | 同步前后端模块；诊断补充携带 `target_run_id`；完成验收后再打开追问开关 | 缺模块会导致加载失败；未区分入口可能重复提交或发往错误轮次 |

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
| V11 r2、Agent storage v2 的 SQLite | 直接执行 PostgreSQL 导入 |
| 8.0 / 8.1 的 Agent storage v1 | 先按[离线格式升级说明](data-upgrade-v11-r2.md)生成新的 V11 r2、Agent v2 SQLite 中间目录，再对中间目录执行 PostgreSQL 导入；不能在中间步骤启动当前 Server |
| V1～V10、旧 `state.json` 或旧 StateExport | 当前工具不支持直接导入，不得强改标记 |
| 已是当前 PostgreSQL 格式 | 使用与资源目录匹配的数据库，不执行 SQLite 导入 |
| 全新安装，无历史数据 | 使用专用空数据库和新的空 `DATA_ROOT`，由 Server 首次启动初始化 |

若旧格式升级需要 `--ownership-map`，映射结果必须与当前登录身份及命名空间一致。未分配归属的历史会话不会自动出现在用户目录中。

### 停服、备份、导入、启动

先停止接收新任务，等待诊断、消息处理、追问、快照、经验任务、上传和清理结束，再停止旧 Server。强制杀进程不能替代排空，导入工具会拒绝未结束的任务。

保存旧程序、配置及完整源目录备份，包括 SQLite、仍存在的 WAL/SHM、报告和附件。提前记录几份历史报告与下载文件的哈希，供切换后核对。目标目录必须尚不存在、父目录已经存在，且不能与源目录互相包含；目标数据库必须为空。

以下示例假设源目录已经是 V11 r2、Agent v2，且目标 `DATABASE_URL` 已安全注入进程环境。先查看计划，再执行：

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

同步部署整个[网站示例模块](../examples/website-agent/README.md)：BFF 的 `server.mjs` 依赖 `followup-bff.mjs` 与 `followup-contract.js`；新版 `browser-client.js` 也依赖 `followup-contract.js`。即使功能暂未开启，也不能只复制入口文件。启用界面时一并接入 `followup-controller.js`、`followup-view.js`、报告视图和 `conversation-input.js`。

前端必须完成以下适配，细节见[追问接入说明](website-report-followup.md)：

- 诊断补充发送保存的 `target_run_id`。收到 `409 / AGENT_RUN_CHANGED` 后刷新对应轮次，再决定是否转为追问，不重发不带目标轮次的消息。
- 追问绑定当前显示的 `selected_run_id`，不能总绑定 `current_run.run_id`。失败不能自动回退 `messages`；重试保留原请求 ID、文字和已选入口，防止重复执行。
- 先 GET 追问快照，再用其 `last_event_id` 订阅独立的追问 SSE。追问事件 schema 为 1，诊断事件为 2，游标不能混用。代理关闭 SSE 缓冲，保留 `Last-Event-ID`。
- 每个会话最多一个活动追问。刷新页面不会停止模型；停止必须调用 stop。重启后排队任务可继续，运行中的任务标记 `INTERRUPTED`，不自动重跑模型。

追问沿用 `DIAGNOSE_CLAUDE_COMMAND`，否则使用 `CLAUDE_COMMAND`。自定义命令须验证追问协议与工具策略；不能假设任何兼容 CLI 都支持只读 Read/Grep 和原日志访问。

`REPORT_FOLLOWUP_SNAPSHOT_BYTES` 默认 1 GiB，`REPORT_FOLLOWUP_STORAGE_BYTES` 默认 5 GiB，应按磁盘容量配置。失败或超限的快照不撤销原报告，可退为仅按报告回答。关闭功能后停止接收新追问，排队任务取消，历史记录仍可读取。

## 上线顺序与验收记录

1. 记录实际旧版本、数据格式、资源路径、用户身份算法、生产注册和 Agent 命令；准备匹配的备份与回退方案。
2. 准备 PostgreSQL，排空停服，按数据格式完成升级和导入；保存导入收据，离线校验后切换数据库及资源目录。
3. 同步后端、BFF、Cookie 代理和 Redis 配置；更新注册 V2。追问开关先保持关闭。
4. 不调用模型的检查：`/ready`、真实用户 Cookie、旧会话与轮次、报告及附件哈希、上传下载、两个独立事件流的鉴权与恢复。重启后再次读取已完成记录，并确认生产没有新建 SQLite 数据库。
5. 部署追问 UI，完成真实 Agent 与容量验收后再启用。任何真实模型活动先查看[中央 Test Flow](../tools/test-flow/README.md)对应入口的 `--plan-only`，确认身份、调用数、token/cost、Proof、Stage、Gate 和阻塞项；失败重试必须有新的假设与预期证据。
6. 保存部署提交、环境配置版本、迁移收据、备份位置和验收结果。Test Flow 只以 `verdict.json` 为准，普通连通检查或导入收据不能代替正式结论。

当前本地确定性验证不能代替生产数据库的 TLS／权限／备份恢复、真实 Redis 与 Cookie、历史数据导入、Docker 部署、真实模型旅程或负载容量验收。本仓库不包含生产地址和凭据，执行上述步骤时须使用部署环境的实际配置。
