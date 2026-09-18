/** Node.js 24+ 兼容入口；业务实现统一位于 server.mjs。 */
import type { IncomingMessage } from "node:http";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { startAgentBackend } from "./server.mjs";
export { createAgentBackend, denyAccess, HttpError, startAgentBackend } from "./server.mjs";

type User = { id: string };
export type Access = {
  // 在此验证网站登录态、Cookie/CSRF 或 Bearer token，不接受前端自报 user_id。
  authenticate(request: IncomingMessage): Promise<User | null>;
};

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await startAgentBackend();
