# Redis 会话接入部署说明

请求链路为：浏览器 Cookie → 网站 BFF 透传 → xiaodao 读取 `sessionid` → Redis `GET airobot2-session:{session_id}` → JSON 中的 `user.userid`。

`{session_id}` 替换为实际 Cookie 值，不保留大括号。Redis value 是 JSON 字符串，例如：

```json
{"user":{"userid":"001234"},"cookie":{}}
```

后端只使用 `user.userid`，保留工号前导零。实际 Redis 内网 IP 尚待提供，真实连接仍需部署后联调。

## 1. Linux 后端配置

把以下配置加入现有私有配置文件，例如 `/opt/xiaodao/service.env`。Redis 必须从 xiaodao 所在机器可达。

```dotenv
WEBSITE_AUTH_MODE=redis
# 部署时填写实际内网 IP 或主机名。
WEBSITE_REDIS_HOST=
WEBSITE_REDIS_PORT=6379
WEBSITE_REDIS_DB=0
WEBSITE_REDIS_USERNAME=
WEBSITE_REDIS_PASSWORD=
WEBSITE_REDIS_SSL=false
WEBSITE_SESSION_COOKIE_NAME=sessionid
WEBSITE_OWNER_NAMESPACE=xiaodao-website
```

用户名、密码和 TLS 按实际 Redis 配置填写。已有网站保留原命名空间，并确认旧 `user.id` 与 `user.userid` 一致，以继续访问历史会话。

完整更新后端源码、`pyproject.toml` 和 `uv.lock`。当前累计版本还要求 PostgreSQL；先按[9 月 23 日以来的生产升级清单](production-upgrade-2026-09-23.md)完成数据库准备或历史迁移，再在 Linux 发布目录安装依赖并启动：

```bash
uv sync --frozen
uv run python -m problem_locator serve --env-file /opt/xiaodao/service.env
```

服务端使用 Python 3.12，新增的 Redis 依赖由 `uv sync --frozen` 安装。CLI **不会自动读取 `.env`**，需要显式传入 `--env-file`，或者由进程管理器注入配置。同名进程环境变量优先于配置文件。

Redis 身份接入本身不改变业务数据格式。但当前累计版本已改用 PostgreSQL，必须配置 `DATABASE_URL`；已有 SQLite 历史须按[PostgreSQL 迁移说明](postgresql-migration.md)复制到专用空数据库和新的 `DATA_ROOT`，再同时切换两项配置。已经使用当前 PostgreSQL 格式的部署，单独调整 Redis 配置时无需重建数据目录。

## 2. 同步更新 BFF

同步部署 `examples/website-agent/server.ts`、`server.mjs` 及其依赖 `followup-bff.mjs`、`followup-contract.js`，保留相对路径。业务实现位于 `server.mjs`，即使关闭报告追问，也不能遗漏它静态导入的模块。切换到默认 Redis 模式时，从部署环境删除 `WEBSITE_AUTH_MODULE`；自定义 `createAgentBackend({ access })` 还需移除 `access` 参数。

使用 Node.js 24+，从网站源码根目录启动，无需 npm 依赖：

```bash
unset WEBSITE_AUTH_MODULE
export XIAODAO_BASE_URL='http://xiaodao.internal:8000'
export PORT=8787
node examples/website-agent/server.ts
```

替换为实际 xiaodao 地址。BFF 读取进程环境，不自行加载 `.env`，默认监听 `127.0.0.1:8787`。将网站同源 `/api/agent/` 转发到 BFF，并保留 Cookie。Cookie 的 Domain、Path 等属性须覆盖该路径；浏览器无需读取 Cookie 或传入工号。

后端与 BFF 一起更新。旧部署暂不切换时，可继续使用 `WEBSITE_AUTH_MODE=trusted_header` 和原 BFF 登录模块。旧模式会改写创建请求的 `request_id`，切换前先确认未完成创建请求的结果。

## 3. 联调

Redis IP 配好后，用有效登录 Cookie 从网站读取会话目录，再创建一个空会话并读回，确认工号和历史数据对应正确。创建空会话不调用模型。

当前 `/ready` 不检查 Redis，健康检查成功不代表已读到工号。缺失或过期会话返回 `401`，Redis 地址未配置或不可用返回 `503`。上传、下载和 SSE 也应携带同一 Cookie。

真实定位仍按[官方 Test Flow](../tools/test-flow/README.md)的现有流程验证；手工连通检查不替代 `verdict.json`。
