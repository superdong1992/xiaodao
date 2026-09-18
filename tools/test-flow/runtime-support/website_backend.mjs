// 在 Linux Server 容器运行正式 BFF。浏览器只有测试会话，不接触原生 owner header。
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { resolve, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createAgentBackend } from "../../../examples/website-agent/server.mjs";
import { WEBSITE_TEST_NAMESPACE, WEBSITE_TEST_USER } from "../lib/website-identity.mjs";

const COOKIE = "xiaodao_test_flow_session";
const STAGE = /^[a-z][a-z0-9.-]{0,127}$/;
const LABELS = new Set(["upload", "resolved-api"]);

export function createWebsiteHarness({ upstream, sessionToken, pagesRoot, fixturePath, fetchImpl = fetch }) {
  if (!/^[a-f0-9]{64}$/.test(sessionToken)) throw new Error("WEBSITE_TEST_SESSION_INVALID");
  const authenticated = (request) => request.headers.authorization === `Bearer ${sessionToken}` ||
    (request.headers.cookie ?? "").split(";").some((item) => item.trim() === `${COOKIE}=${sessionToken}`);
  const backend = createAgentBackend({ upstream, ownerNamespace: WEBSITE_TEST_NAMESPACE, fetchImpl, access: {
    authenticate: async (request) => {
      if (!authenticated(request)) return null;
      if (request.headers.origin && request.headers.origin !== `http://${request.headers.host}`) return null;
      return { id: WEBSITE_TEST_USER };
    },
  } });
  const dispatch = backend.listeners("request")[0];
  return createServer(async (request, response) => {
    const url = new URL(request.url ?? "/", "http://website.test");
    if (!url.pathname.startsWith("/__testflow/")) { dispatch(request, response); return; }
    const send = (status, body, headers = {}) => {
      response.writeHead(status, { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer", ...headers });
      response.end(body);
    };
    if (request.method !== "GET") { send(404, "Not found"); return; }
    if (url.pathname === "/__testflow/ready" && !url.search) { send(200, "ready"); return; }
    try {
      if (url.pathname === "/__testflow/fixture" && !url.search && authenticated(request)) {
        send(200, await readFile(fixturePath), { "Content-Type": "application/octet-stream" }); return;
      }
      const page = /^\/__testflow\/page\/([^/]+)\/([^/]+)$/.exec(url.pathname);
      if (page && STAGE.test(page[1]) && LABELS.has(page[2]) &&
          [...url.searchParams.keys()].length === 1 && url.searchParams.get("session") === sessionToken) {
        const bytes = await readFile(join(resolve(pagesRoot), page[1], `website-${page[2]}.html`));
        send(200, bytes, { "Content-Type": "text/html; charset=utf-8",
          "Set-Cookie": `${COOKIE}=${sessionToken}; HttpOnly; SameSite=Strict; Path=/` }); return;
      }
      send(404, "Not found");
    } catch { send(404, "Not found"); }
  });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const options = JSON.parse(process.argv[2]);
  const server = createWebsiteHarness(options);
  server.listen(options.port ?? 8001, "0.0.0.0");
}
