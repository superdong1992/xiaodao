import { sha256File } from "./file-sha256.js";

const MIME = [[".tar.gz", "application/gzip"], [".tgz", "application/gzip"], [".zip", "application/zip"], [".tar", "application/x-tar"], [".gz", "application/gzip"]];
const fileType = (file) => MIME.find(([suffix]) => file.name.toLowerCase().endsWith(suffix));

/** 原上传流程的独立队列；不判断用户意图，也不创建诊断。 */
export function createLogUploadController({ client, conversationId, makeRequestId = () => globalThis.crypto.randomUUID(),
  sha256 = sha256File, canChange = () => true, initialUploads = [], onChange = () => {} }) {
  const files = new Map();
  const state = { uploads: structuredClone(initialUploads), uploading: false, error: null };
  let disposed = false, abort = new AbortController();
  const emit = () => { if (!disposed) onChange(structuredClone(state)); };
  async function uploadFiles(selected) {
    if (disposed || !canChange() || state.uploading) return;
    const additions = [...new Set(selected)].filter((file) => !files.has(file));
    if (state.uploads.length + additions.length > 20) throw new Error("一次最多选择 20 个日志包。");
    if ([...state.uploads, ...additions].reduce((sum, file) => sum + file.size, 0) > 5_368_709_120)
      throw new Error("所选日志包的总大小不能超过 5 GiB。");
    for (const file of additions) {
      const type = fileType(file);
      if (!type || !file.name.endsWith(type[0]) || file.size < 1 || file.size > 2_684_354_560)
        throw new Error("请选择后缀为小写 zip、tar、tar.gz、tgz 或 gz 的日志包，单包大小为 1 字节到 2.5 GiB。");
    }
    for (const file of additions) {
      const item = { local_id: makeRequestId(), name: file.name, size: file.size, status: "WAITING", attachment_id: null };
      files.set(file, { file, item, request_id: makeRequestId(), sha: null, prepared: null }); state.uploads.push(item);
    }
    if (abort.signal.aborted) abort = new AbortController();
    const signal = abort.signal;
    const check = () => { if (signal.aborted) throw new DOMException("已取消日志校验和上传，可重试。", "AbortError"); };
    state.uploading = true; state.error = null; emit();
    try {
      for (const entry of files.values()) {
        if (entry.item.status === "READY") continue;
        if (disposed) return;
        check();
        if (!entry.sha) {
          entry.item.status = "CHECKING"; entry.item.checked_bytes = 0; emit();
          let lastProgress = -Infinity;
          entry.sha = await sha256(entry.file, { signal, onProgress: (loaded, total) => {
            if (disposed || signal.aborted) return;
            entry.item.checked_bytes = Math.max(entry.item.checked_bytes, loaded);
            if (Date.now() - lastProgress >= 100 || loaded === total) { lastProgress = Date.now(); emit(); }
          } });
        }
        if (disposed) return;
        check();
        const duplicate = state.uploads.find((item) => item !== entry.item && item.sha256 === entry.sha && item.size === entry.file.size && item.status === "READY");
        if (duplicate) {
          Object.assign(entry.item, { status: "READY", attachment_id: duplicate.attachment_id, sha256: entry.sha }); emit(); continue;
        }
        entry.item.status = "UPLOADING"; emit();
        entry.prepared ??= await client.attachments.prepare(conversationId, { request_id: entry.request_id,
          name: entry.file.name, content_type: fileType(entry.file)[1], declared_size: entry.file.size, declared_sha256: entry.sha });
        if (disposed) return;
        check();
        await client.attachments.upload(entry.prepared, entry.file, { signal });
        if (disposed) return;
        check();
        Object.assign(entry.item, { status: "READY", attachment_id: entry.prepared.attachment.attachment_id, sha256: entry.sha }); emit();
      }
    } catch (error) {
      if (!disposed) {
        for (const item of state.uploads) if (["CHECKING", "UPLOADING"].includes(item.status))
          item.status = error.name === "AbortError" ? "CANCELLED" : "FAILED";
        state.error = error.message; throw error;
      }
    } finally { state.uploading = false; emit(); }
  }
  return {
    uploadFiles, getState: () => structuredClone(state),
    attachmentIds: () => [...new Set(state.uploads.filter((item) => item.status === "READY").map((item) => item.attachment_id))],
    cancel() { if (state.uploading) abort.abort(); },
    clear() { if (disposed || state.uploading) return; files.clear(); state.uploads = []; state.error = null; emit(); },
    remove(localId) {
      if (disposed || !canChange() || state.uploading) return;
      state.uploads = state.uploads.filter((item) => item.local_id !== localId);
      for (const [file, entry] of files) if (entry.item.local_id === localId) files.delete(file);
      emit();
    },
    destroy() { disposed = true; abort.abort(); files.clear(); },
  };
}
