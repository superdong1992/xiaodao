import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { Worker } from "node:worker_threads";
import test from "node:test";
import { sha256File } from "./file-sha256.js";

const digest = (bytes) => createHash("sha256").update(bytes).digest("hex");
const settle = () => new Promise((resolve) => setImmediate(resolve));
class FakeWorker {
  terminated = 0;
  postMessage(value) { this.input = value; }
  terminate() { this.terminated++; }
  message(data) { this.onmessage?.({ data }); }
}

test("文件哈希 Worker 只接收 File，回传进度和摘要后释放线程", async () => {
  const file = new File(["actual bytes"], "logs.zip"), worker = new FakeWorker(), progress = [];
  file.slice = () => assert.fail("主线程不能读取文件块");
  const promise = sha256File(file, { workerFactory: () => worker, onProgress: (...value) => progress.push(value) });
  assert.deepEqual(Object.keys(worker.input), ["file"]); assert.equal(worker.input.file, file);
  worker.message({ type: "progress", loaded: 4, total: file.size });
  worker.message({ type: "result", sha256: digest("actual bytes") });
  assert.equal(await promise, digest("actual bytes")); assert.deepEqual(progress, [[4, file.size]]);
  assert.equal(worker.terminated, 1); assert.equal(worker.onmessage, null);
});

test("取消文件哈希立即终止 Worker，不降级或接收迟到结果", async () => {
  const file = new Blob(["bytes"]), worker = new FakeWorker(), abort = new AbortController();
  file.slice = () => assert.fail("取消不能转为主线程重算");
  const promise = sha256File(file, { workerFactory: () => worker, signal: abort.signal,
    onProgress: () => assert.fail("取消后不能更新进度") });
  abort.abort(); await assert.rejects(promise, { name: "AbortError" });
  assert.equal(worker.terminated, 1); worker.message({ type: "result", sha256: digest("bytes") });
  await assert.rejects(sha256File(file, { signal: abort.signal, workerFactory: () => assert.fail("已取消不能创建线程") }), { name: "AbortError" });
});

for (const mode of ["unsupported", "constructor", "load", "clone"]) {
  test(`Worker 不可用时分块降级并让出执行：${mode}`, async () => {
    const bytes = Buffer.alloc(1024 * 1024 + 65, 23), file = new Blob([bytes]), worker = new FakeWorker();
    let yields = 0, maxRead = 0;
    const original = file.slice.bind(file);
    file.arrayBuffer = () => assert.fail("降级也不能一次读整个文件");
    file.slice = (start, end) => { maxRead = Math.max(maxRead, end - start); return original(start, end); };
    const promise = sha256File(file, { workerFactory: () => {
      if (mode === "unsupported") return null;
      if (mode === "constructor") throw new Error("CSP");
      if (mode === "clone") worker.postMessage = () => { throw new Error("DataCloneError"); };
      return worker;
    }, yieldTask: async () => { yields++; } });
    if (mode === "load") worker.onerror({ preventDefault() {} });
    assert.equal(await promise, digest(bytes)); assert.ok(maxRead <= 1024 * 1024); assert.ok(yields >= 17);
    if (["load", "clone"].includes(mode)) assert.equal(worker.terminated, 1);
  });
}

test("主线程降级在小块边界响应取消，Worker 校验失败不会暗中重算", async () => {
  const abort = new AbortController(); let yields = 0;
  await assert.rejects(sha256File(new Blob([Buffer.alloc(1024 * 1024)]), {
    workerFactory: () => null, signal: abort.signal, yieldTask: async () => { yields++; abort.abort(); },
  }), { name: "AbortError" });
  assert.equal(yields, 1);
  const file = new Blob(["bytes"]), worker = new FakeWorker();
  file.slice = () => assert.fail("读取失败不应重复读文件");
  const promise = sha256File(file, { workerFactory: () => worker });
  worker.message({ type: "error" }); await assert.rejects(promise, /文件校验失败/); assert.equal(worker.terminated, 1);
});

test("真实 Worker 执行同一浏览器脚本，多分块摘要和进度正确，主线程保持可调度", async () => {
  const bytes = Buffer.alloc(4 * 1024 * 1024 + 65, 41), file = new Blob([bytes]);
  const workerUrl = new URL("./file-sha256-worker.js", import.meta.url).href;
  let adapter, native, terminated = false, ticks = 0; const progress = [];
  const timer = setInterval(() => ticks++, 1);
  try {
    const actual = await sha256File(file, { workerFactory: () => {
      native = new Worker(`const { parentPort } = require('node:worker_threads');
        globalThis.self = { postMessage: value => parentPort.postMessage(value) };
        import(${JSON.stringify(workerUrl)}).then(() => parentPort.on('message', data => self.onmessage({data})));`, { eval: true, execArgv: [] });
      adapter = { postMessage: (value) => native.postMessage(value), terminate() { terminated = true; void native.terminate(); } };
      native.on("message", (data) => adapter.onmessage?.({ data })); native.on("error", (error) => adapter.onerror?.(error));
      return adapter;
    }, onProgress: (loaded, total) => { assert.equal(total, file.size); progress.push(loaded); } });
    assert.equal(actual, digest(bytes)); assert.equal(progress.at(-1), file.size);
    assert.equal(progress.length, 5); assert.ok(ticks > 0); assert.equal(terminated, true);
  } finally { clearInterval(timer); if (native) await native.terminate(); }
  await settle();
});
