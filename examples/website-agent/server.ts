/** Node.js 24+ 兼容入口；业务实现统一位于 server.mjs。
 * 默认原样透传 Cookie，由 Xiaodao 后端读取 Redis 会话中的 user.userid。
 * 仅已采用可信 owner 接入的部署需要显式配置 Access。
 */
import type { IncomingMessage } from "node:http";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { startAgentBackend } from "./server.mjs";
export { createAgentBackend, denyAccess, HttpError, startAgentBackend } from "./server.mjs";

type User = { id: string };
export type Access = {
  // 兼容接入：验证网站登录态和 CSRF / Origin，再返回服务端确认的身份。
  authenticate(request: IncomingMessage): Promise<User | null>;
};

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await startAgentBackend();
