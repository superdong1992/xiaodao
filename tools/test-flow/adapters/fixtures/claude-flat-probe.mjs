// Repository-owned mock Host/MCP pair used only by the host capability adapter.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";

const evidenceRoot = path.resolve(process.argv[2]);
const apiLog = path.join(evidenceRoot, "api-requests.jsonl");
const mcpLog = path.join(evidenceRoot, "mcp-requests.jsonl");
const readyFile = path.join(evidenceRoot, "servers-ready.json");
const requestId = "10000000-0000-0000-0000-000000000105";

function append(file, value) {
  fs.appendFileSync(file, `${JSON.stringify(value)}\n`, "utf8");
}

function readJson(request) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      try {
        const text = Buffer.concat(chunks).toString("utf8");
        resolve(text.length === 0 ? null : JSON.parse(text));
      } catch (error) {
        reject(error);
      }
    });
    request.on("error", reject);
  });
}

function json(response, status, value) {
  const body = JSON.stringify(value);
  response.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(body),
  });
  response.end(body);
}

function sse(response, events) {
  response.writeHead(200, {
    "content-type": "text/event-stream",
    "cache-control": "no-cache",
    connection: "close",
  });
  for (const [event, data] of events) {
    response.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  }
  response.end();
}

const problemFields = {
  statement: "连接失败",
  expected_behavior: "请求成功",
  actual_behavior: "请求超时",
  scope: "局域网",
  goals: ["定位根因"],
  non_goals: [],
  constraints: ["只使用现有证据"],
  completion_criteria: ["确认参数类型"],
};
const tool = {
  name: "problem_locator_create_case",
  description: "Create a probe case with flat inputs.",
  inputSchema: {
    type: "object",
    properties: {
      request_id: { type: "string" },
      statement: { type: "string" },
      expected_behavior: { type: "string" },
      actual_behavior: { type: "string" },
      scope: { type: "string" },
      goals: { type: "array", items: { type: "string" } },
      non_goals: { type: "array", items: { type: "string" } },
      constraints: { type: "array", items: { type: "string" } },
      completion_criteria: { type: "array", items: { type: "string" } },
      initial_user_fact_names: {
        type: "array",
        items: { type: "string" },
        maxItems: 64,
        uniqueItems: true,
      },
      initial_user_fact_values: {
        type: "array",
        items: { type: "string" },
        maxItems: 64,
      },
      wait_seconds: { type: "integer" },
    },
    required: [
      "request_id",
      "statement",
      "expected_behavior",
      "actual_behavior",
      "scope",
      "goals",
      "non_goals",
      "constraints",
      "completion_criteria",
    ],
    additionalProperties: false,
  },
};

function messageStart(id) {
  return [
    "message_start",
    {
      type: "message_start",
      message: {
        id,
        type: "message",
        role: "assistant",
        model: "claude-flat-probe",
        content: [],
        stop_reason: null,
        stop_sequence: null,
        usage: { input_tokens: 10, output_tokens: 0 },
      },
    },
  ];
}

function toolEvents(toolName) {
  const input = {
    request_id: requestId,
    ...problemFields,
    initial_user_fact_names: ["host"],
    initial_user_fact_values: ["节点一"],
    wait_seconds: 0,
  };
  return [
    messageStart("msg_flat_probe"),
    [
      "content_block_start",
      {
        type: "content_block_start",
        index: 0,
        content_block: {
          type: "tool_use",
          id: "toolu_flat_probe",
          name: toolName,
          input: {},
        },
      },
    ],
    [
      "content_block_delta",
      {
        type: "content_block_delta",
        index: 0,
        delta: { type: "input_json_delta", partial_json: JSON.stringify(input) },
      },
    ],
    ["content_block_stop", { type: "content_block_stop", index: 0 }],
    [
      "message_delta",
      {
        type: "message_delta",
        delta: { stop_reason: "tool_use", stop_sequence: null },
        usage: { output_tokens: 5 },
      },
    ],
    ["message_stop", { type: "message_stop" }],
  ];
}

function textEvents() {
  return [
    messageStart("msg_done"),
    [
      "content_block_start",
      {
        type: "content_block_start",
        index: 0,
        content_block: { type: "text", text: "" },
      },
    ],
    [
      "content_block_delta",
      {
        type: "content_block_delta",
        index: 0,
        delta: { type: "text_delta", text: "DONE" },
      },
    ],
    ["content_block_stop", { type: "content_block_stop", index: 0 }],
    [
      "message_delta",
      {
        type: "message_delta",
        delta: { stop_reason: "end_turn", stop_sequence: null },
        usage: { output_tokens: 1 },
      },
    ],
    ["message_stop", { type: "message_stop" }],
  ];
}

const apiServer = http.createServer(async (request, response) => {
  try {
    const body = await readJson(request);
    append(apiLog, { method: request.method, url: request.url, body });
    if (request.url.includes("count_tokens")) {
      json(response, 200, { input_tokens: 10 });
      return;
    }
    const hasToolResult = Array.isArray(body?.messages)
      && body.messages.some((message) => Array.isArray(message.content)
        && message.content.some((content) => content?.type === "tool_result"));
    if (hasToolResult) {
      sse(response, textEvents());
      return;
    }
    const advertised = Array.isArray(body?.tools)
      ? body.tools.find((candidate) => candidate?.name?.endsWith(tool.name))
      : null;
    if (!advertised) {
      json(response, 400, {
        error: { type: "invalid_request_error", message: "tool was not advertised" },
      });
      return;
    }
    sse(response, toolEvents(advertised.name));
  } catch (error) {
    append(apiLog, { server_error: String(error), stack: error.stack });
    json(response, 500, { error: { type: "api_error", message: String(error) } });
  }
});

function toolResult(argumentsValue) {
  const valid = argumentsValue
    && !Object.hasOwn(argumentsValue, "problem_spec")
    && !Object.hasOwn(argumentsValue, "initial_user_facts")
    && typeof argumentsValue.statement === "string"
    && Array.isArray(argumentsValue.goals)
    && Array.isArray(argumentsValue.initial_user_fact_names)
    && Array.isArray(argumentsValue.initial_user_fact_values)
    && argumentsValue.initial_user_fact_names.length
      === argumentsValue.initial_user_fact_values.length;
  const structuredContent = valid
    ? { ok: true, data: { request_id: argumentsValue.request_id }, error: null }
    : {
      ok: false,
      data: null,
      error: {
        code: "VALIDATION_ERROR",
        details: [{ field: "problem_spec", expected: "forbidden" }],
      },
    };
  return {
    content: [{ type: "text", text: JSON.stringify(structuredContent) }],
    structuredContent,
    isError: false,
  };
}

function handleMcp(message) {
  const { id, method, params } = message;
  if (method === "initialize") {
    return {
      jsonrpc: "2.0",
      id,
      result: {
        protocolVersion: params?.protocolVersion ?? "2025-03-26",
        capabilities: { tools: {} },
        serverInfo: { name: "problem-locator", version: "3.0.0" },
      },
    };
  }
  if (method === "tools/list") {
    return { jsonrpc: "2.0", id, result: { tools: [tool] } };
  }
  if (method === "tools/call") {
    return { jsonrpc: "2.0", id, result: toolResult(params?.arguments) };
  }
  if (id === undefined) return null;
  return {
    jsonrpc: "2.0",
    id,
    error: { code: -32601, message: `Method not found: ${method}` },
  };
}

const mcpServer = http.createServer(async (request, response) => {
  try {
    if (request.method === "GET") {
      response.writeHead(405, { allow: "POST" });
      response.end();
      return;
    }
    if (request.method === "DELETE") {
      response.writeHead(204);
      response.end();
      return;
    }
    const body = await readJson(request);
    append(mcpLog, {
      method: request.method,
      url: request.url,
      headers: request.headers,
      body,
    });
    const messages = Array.isArray(body) ? body : [body];
    const results = messages.map(handleMcp).filter(Boolean);
    if (results.length === 0) {
      response.writeHead(202);
      response.end();
      return;
    }
    json(response, 200, Array.isArray(body) ? results : results[0]);
  } catch (error) {
    append(mcpLog, { server_error: String(error), stack: error.stack });
    json(response, 500, {
      jsonrpc: "2.0",
      id: null,
      error: { code: -32603, message: String(error) },
    });
  }
});

await Promise.all([
  new Promise((resolve) => apiServer.listen(0, "127.0.0.1", resolve)),
  new Promise((resolve) => mcpServer.listen(0, "127.0.0.1", resolve)),
]);
fs.writeFileSync(readyFile, JSON.stringify({
  api: apiServer.address().port,
  mcp: mcpServer.address().port,
  request_id: requestId,
}), "utf8");

function close() {
  apiServer.close();
  mcpServer.close();
}
process.on("SIGINT", close);
process.on("SIGTERM", close);
