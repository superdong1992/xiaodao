import { sha256FileInThread } from "./file-sha256.js";

// 一个 Worker 只处理一个文件；页面在完成或取消时终止它。
let started = false;
self.onmessage = async ({ data }) => {
  if (started) return;
  started = true;
  try {
    const sha256 = await sha256FileInThread(data.file, {
      cpuChunkBytes: 1024 * 1024,
      yieldTask: () => Promise.resolve(),
      onProgress: (loaded, total) => self.postMessage({ type: "progress", loaded, total }),
    });
    self.postMessage({ type: "result", sha256 });
  } catch {
    self.postMessage({ type: "error" });
  }
};
