// 文件始终按 1 MiB 读取；默认在 Worker 中计算，主线程降级时每 64 KiB 让出执行。
let yieldChannel;
const waiting = [];
function yieldToMain() {
  if (globalThis.scheduler?.yield) return globalThis.scheduler.yield();
  if (typeof globalThis.MessageChannel !== "function") return new Promise((resolve) => setTimeout(resolve, 0));
  if (!yieldChannel) {
    yieldChannel = new MessageChannel();
    yieldChannel.port1.onmessage = () => { waiting.shift()?.(); if (!waiting.length) yieldChannel.port1.unref?.(); };
    yieldChannel.port2.unref?.();
  }
  return new Promise((resolve) => { waiting.push(resolve); yieldChannel.port1.ref?.(); yieldChannel.port2.postMessage(null); });
}
const aborted = () => new DOMException("已取消文件校验。", "AbortError");
const defaultWorker = () => typeof globalThis.Worker === "function" ?
  new Worker(new URL("./file-sha256-worker.js", import.meta.url), { type: "module" }) : null;
const WORKER_UNAVAILABLE = Symbol("worker-unavailable");

export async function sha256File(file, { signal, onProgress = () => {}, yieldTask, workerFactory = defaultWorker } = {}) {
  if (signal?.aborted) throw aborted();
  let worker;
  try { worker = workerFactory(); } catch { /* CSP 或旧浏览器不支持 Worker，使用同一算法分块计算。 */ }
  if (worker) {
    try {
      return await new Promise((resolve, reject) => {
        let finished = false, loaded = 0;
        const finish = (error, hash) => {
          if (finished) return;
          finished = true; signal?.removeEventListener("abort", cancel);
          worker.onmessage = worker.onerror = worker.onmessageerror = null; worker.terminate();
          if (error) reject(error); else resolve(hash);
        };
        const cancel = () => finish(aborted());
        worker.onerror = worker.onmessageerror = (event) => { event.preventDefault?.(); finish(WORKER_UNAVAILABLE); };
        worker.onmessage = ({ data }) => {
          if (finished) return;
          try {
            if (data?.type === "progress" && Number.isSafeInteger(data.loaded) && data.loaded >= loaded &&
              data.loaded <= file.size && data.total === file.size) {
              loaded = data.loaded; onProgress(loaded, file.size);
            } else if (data?.type === "result" && /^[a-f0-9]{64}$/.test(data.sha256)) finish(null, data.sha256);
            else if (data?.type === "error") finish(new Error("文件校验失败，请重新选择文件后重试。"));
            else finish(new Error("文件校验返回了无效结果，请重试。"));
          } catch (error) { finish(error); }
        };
        signal?.addEventListener("abort", cancel, { once: true });
        if (signal?.aborted) { cancel(); return; }
        try { worker.postMessage({ file }); } catch { finish(WORKER_UNAVAILABLE); }
      });
    } catch (error) { if (error !== WORKER_UNAVAILABLE) throw error; }
  }
  return sha256FileInThread(file, { signal, onProgress, yieldTask });
}

const K = new Uint32Array([
  0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
  0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
  0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
  0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
  0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
  0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
  0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
  0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2,
]);
const rotate = (word, bits) => (word >>> bits) | (word << (32 - bits));
export async function sha256FileInThread(file, { signal, onProgress = () => {}, yieldTask = yieldToMain,
  cpuChunkBytes = 64 * 1024 } = {}) {
  const state = new Uint32Array([0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]);
  const words = new Uint32Array(64), chunkSize = 1024 * 1024;
  const check = () => { if (signal?.aborted) throw aborted(); };
  function block(bytes, offset) {
    for (let index = 0; index < 16; index++) {
      const pos = offset + index * 4;
      words[index] = (bytes[pos] << 24) | (bytes[pos + 1] << 16) | (bytes[pos + 2] << 8) | bytes[pos + 3];
    }
    for (let index = 16; index < 64; index++) {
      const x = words[index - 15], y = words[index - 2];
      words[index] = words[index - 16] + (rotate(x, 7) ^ rotate(x, 18) ^ (x >>> 3)) + words[index - 7] +
        (rotate(y, 17) ^ rotate(y, 19) ^ (y >>> 10));
    }
    let a = state[0], b = state[1], c = state[2], d = state[3], e = state[4], f = state[5], g = state[6], h = state[7];
    for (let index = 0; index < 64; index++) {
      const t1 = h + (rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25)) + ((e & f) ^ (~e & g)) + K[index] + words[index];
      const t2 = (rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
      h = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    state[0] += a; state[1] += b; state[2] += c; state[3] += d;
    state[4] += e; state[5] += f; state[6] += g; state[7] += h;
  }
  check();
  let tail = new Uint8Array();
  for (let offset = 0; offset < file.size; offset += chunkSize) {
    check();
    const bytes = new Uint8Array(await file.slice(offset, Math.min(file.size, offset + chunkSize)).arrayBuffer());
    check();
    const full = bytes.length - bytes.length % 64;
    for (let part = 0; part < full; part += cpuChunkBytes) {
      for (let start = part; start < Math.min(full, part + cpuChunkBytes); start += 64) block(bytes, start);
      if (part + cpuChunkBytes < full) { await yieldTask(); check(); }
    }
    tail = bytes.slice(full);
    onProgress(Math.min(file.size, offset + bytes.length), file.size);
    await yieldTask();
  }
  check();
  const ending = new Uint8Array(tail.length < 56 ? 64 : 128);
  ending.set(tail); ending[tail.length] = 0x80;
  const view = new DataView(ending.buffer), bits = file.size * 8;
  view.setUint32(ending.length - 8, Math.floor(bits / 0x100000000));
  view.setUint32(ending.length - 4, bits >>> 0);
  for (let start = 0; start < ending.length; start += 64) block(ending, start);
  return [...state].map((word) => word.toString(16).padStart(8, "0")).join("");
}
