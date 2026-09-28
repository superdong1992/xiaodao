import { AgentApiError } from "./browser-client.js";
import { validFollowupId, validFollowupRequestId } from "./followup-contract.js";

/** 新网站共用输入框的分流入口。调用者先保存全部参数，重试不能改写 reportReady。 */
export async function sendConversationInput({ client, conversationId, runId, requestId, text = null,
  attachmentIds = [], reportReady }) {
  if (!validFollowupId(conversationId) || !validFollowupId(runId) || !validFollowupRequestId(requestId) ||
    !Array.isArray(attachmentIds) || typeof reportReady !== "boolean")
    throw new TypeError("请保存有效的会话、轮次、报告状态和请求 ID，再发送补充内容。");
  const changed = () => new AgentApiError("所选诊断已结束或发生变化，请刷新会话；独立定位请新建对话。", {
    status: 409, code: "AGENT_RUN_CHANGED",
  });
  async function snapshot() {
    const value = await client.conversations.get(conversationId, { include: [], run_id: runId });
    if (value.conversation_id !== conversationId || value.selected_run_id !== runId ||
      !["PENDING", "READY", "UNAVAILABLE"].includes(value.report_state))
      throw new AgentApiError("会话响应与所选诊断不一致，请刷新后重试。", { status: 502, code: "WEBSITE_INVALID_RESPONSE" });
    return value;
  }
  async function ask() {
    if (attachmentIds.length) throw new AgentApiError("报告后的追问只接收文字；如需提交新日志，请新建对话。", {
      status: 409, code: "WEBSITE_FOLLOWUP_TEXT_ONLY",
    });
    return { kind: "followup", receipt: await client.followups.send(conversationId, runId, {
      request_id: requestId, text,
    }) };
  }
  // 响应丢失后仍重放原入口，让服务端先命中旧收据。不能因报告后来完成就改发第二个模型请求。
  if (reportReady) return ask();
  try {
    return { kind: "message", receipt: await client.conversations.send(conversationId, {
      request_id: requestId, text, attachment_ids: attachmentIds, target_run_id: runId,
    }) };
  } catch (error) {
    if (error.status !== 409 || error.code !== "AGENT_RUN_CHANGED") throw error;
    const refreshed = await snapshot();
    if (refreshed.report_state === "READY") return ask();
    throw changed();
  }
}
