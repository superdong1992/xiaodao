# 网站 Agent 后端示例

这是供网站后端集成的示例，**不是现成的聊天页面，也不包含登录系统**。先读 [快速接入与联调清单](../../docs/website-agent-quickstart.md)，接口细节查 [Agent API 参考](../../docs/website-agent-api.md)。

## 1. 启动前准备

使用 Node.js 24+，无需 npm 依赖。先确认网站后端可以访问 xiaodao 的 `/live`、`/ready` 和 `/openapi.json`，线上版本为 `8.0.0`。再接入网站的认证和归属存储。

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

这里的 `/api/agent` 是**网站同源路径**；xiaodao 上游使用 `/api/v1/agent`。服务端 `PUBLIC_BASE_URL` 与本例 `XIAODAO_BASE_URL` 的协议、地址、路径前缀须一致，示例会严格核对下载描述符地址；上传地址会改写为网站同源路径。

- 创建、发送消息、查询、订阅、预约和上传：使用 `/api/agent/...` 对应路径，详见 [API 参考](../../docs/website-agent-api.md)。
- `GET /api/agent/conversations/{id}/report`：收到 `result.available` 后调用，返回校验后的 JSON 或 Generic Markdown 和展示结构。
- `GET /api/agent/conversations/{id}/artifacts`：查询授权后的产物，下载地址已改为网站同源路径。
- 结果 ZIP：用户确认包含原始目标日志后，给相应下载路径添加 `?download=archive&acknowledge_raw_logs=true`。
- 审计包：只按用户请求下载，给相应下载路径添加 `?download=audit`。

`/report` 自动获取并校验 JSON 或 Generic Markdown；ZIP 只在用户主动确认后下载。所有产物均先落入唯一临时文件，验证 Content-Length、真实字节数和 SHA-256 后才转发，结束后清理。测试使用假上游，不调用真实模型。

## 4. 本地自检与环境联调分开

从仓库根运行示例自检，不访问已部署服务，不调用模型：

```bash
node --test examples/website-agent/server.test.mjs
```

示例自检通过不代表 Linux 环境验收通过。真实发消息前，按 [Test Flow 说明](../../tools/test-flow/README.md)查看对应 `--plan-only` 身份和预算，再按 [联调清单](../../docs/website-agent-quickstart.md)验证追问、SSE、报告、权限和重启恢复。
