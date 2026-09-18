import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { once } from "node:events";
import { mkdtemp, mkdir, writeFile, rm, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import vm from "node:vm";
import test from "node:test";
import { createWebsiteHarness } from "../runtime-support/website_backend.mjs";
import { WEBSITE_TEST_OWNER } from "../lib/website-identity.mjs";
import { hashConfiguredPaths } from "../lib/identity.mjs";
import { browserSha256, websiteUploadPage, websiteResolvedPage } from "../lib/website-browser.mjs";
import { createAgentClient } from "../../../examples/website-agent/browser-client.js";

const conversation = "10000000-0000-0000-0000-000000000001";
const run = "20000000-0000-0000-0000-000000000001";
const attachment = "30000000-0000-0000-0000-000000000001";
const caseId = "40000000-0000-0000-0000-000000000001";
const job = "50000000-0000-0000-0000-000000000001";
const reportId = "60000000-0000-0000-0000-000000000001";
const archiveId = "60000000-0000-0000-0000-000000000002";
const token = "a".repeat(64), stage = "journey.cross-job.diagnose";
const digest = (bytes) => createHash("sha256").update(bytes).digest("hex");
const archiveBytes = Buffer.from("PK synthetic archive"), report = { schema_version: 3,
  format_id: "problem-locator-diagnosis-v3", status: "COMPLETED", problem_statement: "合成问题" };
const reportBytes = Buffer.from(JSON.stringify(report));
const artifact = (id, kind, bytes) => ({ artifact_id: id, kind, name: kind === "USER_RESULT" ? "diagnosis-result.json" : "result.zip",
  content_type: kind === "USER_RESULT" ? "application/json" : "application/zip", size: bytes.length, sha256: digest(bytes),
  resource_kind: "FILE", created_by_job_id: job, created_at: "2026-09-18T00:00:00Z", downloadable: true,
  download_url: "http://attacker.invalid/private" });
const artifacts = [artifact(reportId, "USER_RESULT", reportBytes), artifact(archiveId, "USER_RESULT_ARCHIVE", archiveBytes)];
const envelope = (data) => Response.json({ ok: true, data, error: null });
function fakeUpstream(calls) {
  return async (url, init) => {
    const parsed = new URL(url); calls.push({ url: parsed.href, method: init.method ?? "GET" });
    assert.equal(parsed.origin, "http://native.internal");
    assert.equal(new Headers(init.headers).get("X-Agent-Owner-Key"), WEBSITE_TEST_OWNER);
    if (parsed.pathname === "/api/v1/agent/conversations") return envelope({ conversation_id: conversation, run_id: run, request_id: "created" });
    if (parsed.pathname === "/api/v1/agent/attachments") return envelope({ attachment: { attachment_id: attachment,
      conversation_id: conversation, size: archiveBytes.length, sha256: digest(archiveBytes) },
      upload: { attachment_id: attachment, url: "http://attacker.invalid/upload", method: "PUT", required_headers: {
        "Content-Type": "application/zip", "Content-Length": String(archiveBytes.length),
        "X-Content-SHA256": digest(archiveBytes), "Idempotency-Key": "upload-once" } } });
    if (parsed.pathname === `/api/v1/agent/attachments/${attachment}/content`) {
      const chunks = []; for await (const chunk of init.body) chunks.push(chunk);
      assert.deepEqual(Buffer.concat(chunks), archiveBytes);
      return envelope({ attachment: { attachment_id: attachment, conversation_id: conversation, status: "READY",
        size: archiveBytes.length, sha256: digest(archiveBytes) } });
    }
    if (parsed.pathname.endsWith("/messages")) return envelope({ conversation_id: conversation, run_id: run, status: "ACCEPTED" });
    if (parsed.pathname === `/api/v1/agent/conversations/${conversation}`) {
      const included = parsed.searchParams.get("include").split(",");
      assert.equal(parsed.searchParams.get("run_id"), run);
      const shared = { conversation_id: conversation, case_id: caseId, case_revision: 4, case_status: "RESOLVED",
        archive_status: "READY", report_state: "READY", source_job_id: job, failure: null };
      return envelope({ ...shared, schema_version: 3, selected_run_id: run, current_run: { run_id: run }, capabilities: {},
        included, history: null, attachments: null, artifacts: artifacts,
        result: included.includes("report") ? { ...shared, schema_version: 1, format: "problem-locator-diagnosis-v3",
          report, markdown: null, artifact: artifacts[0] } : null });
    }
    const bytes = parsed.pathname.endsWith(`/${reportId}/content`) ? reportBytes : archiveBytes;
    assert.match(parsed.pathname, new RegExp(`^/api/v1/agent/conversations/${conversation}/files/`));
    assert.equal(parsed.searchParams.get("run_id"), run);
    return new Response(bytes, { headers: { "Content-Type": bytes === reportBytes ? "application/json" : "application/zip" } });
  };
}
async function harness(exercise) {
  const root = await mkdtemp(path.join(tmpdir(), "xiaodao-website-test-")), calls = [];
  await mkdir(path.join(root, stage)); await writeFile(path.join(root, "fixture.zip"), archiveBytes);
  await writeFile(path.join(root, stage, "website-upload.html"), "<html>fixture page</html>");
  const server = createWebsiteHarness({ upstream: "http://native.internal", sessionToken: token,
    pagesRoot: root, fixturePath: path.join(root, "fixture.zip"), fetchImpl: fakeUpstream(calls) });
  server.listen(0, "127.0.0.1"); await once(server, "listening");
  const origin = `http://127.0.0.1:${server.address().port}`;
  try { await exercise({ origin, calls }); }
  finally { const closed = once(server, "close"); server.close(); server.closeAllConnections(); await closed; await rm(root, { recursive: true, force: true }); }
}
async function browserSession(origin) {
  const response = await fetch(`${origin}/__testflow/page/${stage}/upload?session=${token}`);
  assert.equal(response.status, 200);
  const cookie = response.headers.get("set-cookie"); assert.match(cookie, /HttpOnly; SameSite=Strict/);
  return async (url, init = {}) => {
    const headers = new Headers(init.headers);
    assert.equal(headers.has("X-Agent-Owner-Key"), false, "浏览器不持有 owner_key");
    headers.set("Cookie", cookie.split(";")[0]);
    return fetch(new URL(url, origin), { ...init, headers });
  };
}
async function executePage(page, fetchImpl, origin) {
  let finish; const completed = new Promise((resolve) => { finish = resolve; });
  const document = { documentElement: { dataset: {} }, set title(_value) { finish(); } };
  const context = vm.createContext({ document, fetch: fetchImpl, location: { origin }, URL, TextEncoder, Uint8Array, Uint32Array,
    DataView, btoa: (value) => Buffer.from(value, "binary").toString("base64") });
  const script = page.match(/<script>([\s\S]*)<\/script>/)[1];
  vm.runInContext(script, context); await completed;
  return JSON.parse(Buffer.from(document.documentElement.dataset.result, "base64").toString("utf8"));
}

test("server harness requires a session and exposes only the fixed fixture and allowlisted pages", async () => {
  await harness(async ({ origin, calls }) => {
    assert.equal((await fetch(origin + "/__testflow/ready")).status, 200);
    assert.equal((await fetch(origin + "/api/agent/conversations")).status, 401);
    for (const suffix of ["/__testflow/fixture", "/__testflow/fixture?path=/etc/passwd",
      `/__testflow/page/${stage}/other?session=${token}`, `/__testflow/page/${stage}/upload?session=wrong`]) {
      assert.equal((await fetch(origin + suffix)).status, 404);
    }
    const browserFetch = await browserSession(origin);
    assert.deepEqual(Buffer.from(await (await browserFetch("/__testflow/fixture")).arrayBuffer()), archiveBytes);
    assert.equal((await browserFetch("/__testflow/fixture", { method: "POST" })).status, 404);
    assert.equal((await browserFetch("/api/agent/conversations", { headers: { Origin: "http://other.invalid" } })).status, 401);
    assert.equal(calls.length, 0);
  });
});

test("browser session traverses the formal BFF for create, reserve, Blob upload, send and selected-run report", async () => {
  await harness(async ({ origin, calls }) => {
    const browserFetch = await browserSession(origin), client = createAgentClient({ fetchImpl: browserFetch });
    await client.conversations.create("stable-create");
    const prepared = await client.attachments.prepare(conversation, { request_id: "prepare", name: "logs.zip",
      content_type: "application/zip", declared_size: archiveBytes.length, declared_sha256: digest(archiveBytes) });
    assert.equal(prepared.upload.url, `/api/agent/attachments/${attachment}/content`);
    const upload = await executePage(websiteUploadPage(attachment, prepared.upload.required_headers), browserFetch, origin);
    assert.equal(upload.ok, true); assert.equal(upload.data.data.attachment.status, "READY");
    await client.conversations.send(conversation, { request_id: "send", text: "定位合成问题", attachment_ids: [attachment] });
    const result = await executePage(websiteResolvedPage(conversation, run), browserFetch, origin);
    assert.equal(result.ok, true); assert.deepEqual(result.detail.data.result.report, report);
    assert.equal(result.downloads.length, 2);
    for (const item of result.downloads) {
      const expected = artifacts.find((candidate) => candidate.artifact_id === item.artifact_id);
      assert.equal(item.status, 200); assert.equal(item.sha256, expected.sha256); assert.equal(item.size, expected.size);
    }
    assert.equal(calls.filter((item) => item.url.includes("/files/")).length, 2);
    assert.ok(calls.every((item) => !item.url.includes("attacker")));
  });
});

test("server-side native oracle and BFF use the same stable test owner without leaking it to pages", async () => {
  const source = await readFile(new URL("../adapters/cross-job-core.mjs", import.meta.url), "utf8");
  assert.match(source, /exec", state\.active_container, "\/usr\/bin\/node",\s*"\/source\/xiaodao\/tools\/test-flow\/lib\/website-agent\.mjs"/);
  assert.doesNotMatch(source, /exec", state\.client_container, "\/usr\/bin\/node",\s*"\/workspace\/tools\/test-flow\/lib\/website-agent\.mjs"/);
  const pages = websiteUploadPage(attachment, { "Content-Length": "99", "X-Agent-Owner-Key": "forged" }) + websiteResolvedPage(conversation, run);
  assert.doesNotMatch(pages, /X-Agent-Owner-Key|forged|Content-Length|api\/v1/);
  assert.doesNotMatch(pages, new RegExp(WEBSITE_TEST_OWNER));
});

for (const size of [0, 1, 55, 56, 63, 64, 65, 100_000]) {
  test(`HTTP browser download SHA-256 matches Node for ${size} bytes`, () => {
    const bytes = Buffer.alloc(size); for (let index = 0; index < size; index++) bytes[index] = index % 251;
    assert.equal(browserSha256(bytes), digest(bytes));
  });
}

test("CrossJob adapter identity binds every server BFF and browser harness dependency", async () => {
  const config = JSON.parse(await readFile(new URL("../config/identities.v2.json", import.meta.url), "utf8"));
  const paths = config.components["adapter.cross-job"].paths;
  const required = ["examples/website-agent/server.mjs", "tools/test-flow/runtime-support/website_backend.mjs",
    "tools/test-flow/lib/website-agent.mjs", "tools/test-flow/lib/website-browser.mjs", "tools/test-flow/lib/website-identity.mjs"];
  const root = await mkdtemp(path.join(tmpdir(), "xiaodao-website-identity-"));
  try {
    for (const file of paths) { await mkdir(path.dirname(path.join(root, file)), { recursive: true }); await writeFile(path.join(root, file), "original\n"); }
    const original = hashConfiguredPaths(root, paths).digest;
    for (const file of required) {
      assert.ok(paths.includes(file), `${file} must affect the explicit adapter identity`);
      await writeFile(path.join(root, file), "changed\n");
      assert.notEqual(hashConfiguredPaths(root, paths).digest, original);
      await writeFile(path.join(root, file), "original\n");
    }
  } finally { await rm(root, { recursive: true, force: true }); }
});
