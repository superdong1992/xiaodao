# 浏览器 REST API 接入指南

本文是浏览器前端接入 Problem Locator REST API 的人工可读指南。前端开发者只需本文、部署方提供的服务基址，以及服务发布的 OpenAPI 文件即可完成接入；不需要了解服务内部实现。

当前正式浏览器目标是**当前稳定版 Google Chrome**。服务不承诺 Firefox、Safari、反向代理或旧版 Chrome 的兼容性。本文中的 TypeScript 使用标准 `fetch`、`File`、`Blob`、Web Worker 和 Web Streams，不依赖 React、Vue 等界面框架。

## 1. 接入前先确认

- 向部署方取得服务基址，例如 `https://locator.example`。下文记为 `baseUrl`，末尾是否带 `/` 均可。
- 先请求 `GET /live` 和 `GET /ready`。只有 `/ready` 成功时才开始业务操作。
- 运行时机器合同是 `GET /openapi.json`；仓库内可离线使用的同一合同是 [`schemas/v2/web-api.openapi.snapshot.json`](../schemas/v2/web-api.openapi.snapshot.json)。交互查看入口是 `GET /docs`。
- 服务没有登录、Cookie、令牌、Case 所有权或租户隔离。它只适合部署在受控网络；知道 ID 的调用方可能读取对应资源。
- 首版没有 Case 列表、恢复、取消接口。前端必须持久保存创建响应中的 `case_id`，并把 `INTERRUPTED` 视为只能查询的状态。

全部入口如下：

| 方法与路径 | 用途 | 响应类型 |
| --- | --- | --- |
| `GET /live` | 进程存活检查 | JSON envelope |
| `GET /ready` | 依赖与恢复状态检查 | JSON envelope |
| `GET /openapi.json` | 下载完整 OpenAPI 合同 | OpenAPI JSON |
| `GET /docs` | 打开交互调试页 | HTML |
| `POST /api/v1/cases` | 创建 Case | `ApplicationResponse` envelope |
| `GET /api/v1/cases/{case_id}` | 查询或长轮询 Case | `CaseQueryResponse` envelope |
| `POST /api/v1/cases/{case_id}/attachments` | 准备附件 | `PrepareAttachmentData` envelope |
| `PUT /api/v1/attachments/{attachment_id}/content` | 上传原始附件字节 | `UploadReadyData` envelope |
| `POST /api/v1/cases/{case_id}/supplements` | 采用补充事实和已上传附件 | `ApplicationResponse` envelope |
| `GET /api/v1/cases/{case_id}/artifacts` | 列出公开产物 | `ArtifactListData` envelope |
| `GET /api/v1/artifacts/{artifact_id}/content` | 下载公开产物字节 | 二进制 |

## 2. 通用 wire 约定

### 2.1 JSON、CORS 与响应头

- JSON 请求使用 `Content-Type: application/json`。模型严格校验类型、拒绝未知字段、拒绝未注明可空的 `null`，也不会把字符串自动转换为数字或布尔值。
- 数组顺序保留；标为唯一的数组不得重复。查询参数不得重复，也不得出现端点未声明的名称。
- CORS 允许任意来源、`GET`、`POST`、`PUT`、`OPTIONS`，但不允许凭据。不要设置 `credentials: "include"`。
- 每个 HTTP 响应都有 `X-Problem-Locator-Correlation-ID`。发生错误时把它连同请求时间、方法和路径交给服务运维；它不是业务 ID，也不能用于重试。
- 所有 JSON 和二进制业务响应成功时均为 HTTP `200`。有限等待超时仍为 `200`，由响应字段表示。

### 2.2 基础类型

| 名称 | wire 类型 | 约束与含义 |
| --- | --- | --- |
| `uuid` | `string` | 小写规范 UUID：`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`。所有 `case_id`、`job_id`、`attachment_id`、`artifact_id` 等资源 ID 均使用此格式。 |
| `sha256` | `string` | 原始字节的 SHA-256，小写十六进制，恰好 64 位：`^[0-9a-f]{64}$`。 |
| `timestamp` | `string` | UTC RFC 3339，必须恰好毫秒精度，例如 `2026-08-18T02:30:00.000Z`。 |
| `name` | `string` | `^[a-z][a-z0-9_]{0,63}$`，用于事实、输入和 requirement 名称。 |
| `text` | `string` | 非空、不能全为空白、最多 65,536 UTF-8 字节；前端不要自动 trim 或改写用户文本。 |
| `content_type` | `string` | 小写规范 media type，不带参数；归档上传使用 `application/zip`、`application/x-tar` 或 `application/gzip`。 |
| `case_revision` | `integer` | 大于 `0` 的 Case 乐观并发版本。写操作只使用它。 |
| `diagnosis_state_revision` | `integer` | 大于 `0` 的诊断内容版本，仅展示；绝不能代替 `case_revision` 提交写操作。 |
| `wait_seconds` | `integer` | `0..30`，默认 `0`；`0` 表示不等待后台 Job。 |
| `request_id` | `string` | 每个逻辑写操作的稳定幂等键；非空且最多 65,536 UTF-8 字节。建议前端生成 UUID，但 wire 不强制 UUID。 |

OpenAPI 对受 UTF-8 字节数约束的字符串同时给出 `maxLength` 和
`x-max-utf8-bytes`；前者是字符长度提示，后者才是多字节文本也必须遵守的实际字节上限。

### 2.3 JSON envelope

除 `/openapi.json`、`/docs` 和成功的二进制下载外，成功响应固定为：

```json
{
  "ok": true,
  "data": {},
  "error": null
}
```

失败响应固定为：

```json
{
  "ok": false,
  "data": null,
  "error": {
    "code": "REVISION_CONFLICT",
    "message": "The expected Case revision is stale.",
    "details": [
      {
        "field": "expected_case_revision",
        "resource_type": "case",
        "resource_id": "10000000-0000-4000-8000-000000000001",
        "resource_ref": null,
        "expected": 4,
        "actual": 5,
        "limit": null,
        "observed": null
      }
    ],
    "retryable": true
  }
}
```

不要只依据 HTTP 状态或英文 `message` 编写分支；以 `error.code` 和 `error.retryable` 为准。`details` 可以为空，也可包含多个字段错误。

### 2.4 幂等、并发与安全重试

1. 为每次逻辑 `create`、`prepare`、`supplement` 生成一个 `request_id`，先保存再发送。
2. 网络断开、请求超时或未收到响应时，重发**相同** `request_id` 和相同业务内容。`wait_seconds` 只控制本次等待，可在幂等重放时改变。
3. 同一 `request_id` 改变业务内容会得到 `IDEMPOTENCY_CONFLICT`；这不是可恢复的 revision 冲突。停止自动重试，检查前端是否错误复用了 ID。
4. `prepare` 与 `supplement` 只提交最后一次查询或写响应给出的 `case_revision`。Job 生命周期和每次附件生命周期都可能推进 revision。
5. 收到 `REVISION_CONFLICT` 后，立即 `GET` 最新 Case，重新展示并确认用户准备提交的内容仍适用，然后使用同一逻辑操作的 `request_id` 和新的 `case_revision` 重试。
6. 写响应中的 `business_receipt` 是持久化成功的权威回执。即使 `case_view` 为 `null`，也不得把写操作当作失败或创建替代 Case；保存回执中的 `case_id`、`case_revision`、`job_id`，再查询刷新。
7. `dispatch_pending=true` 表示 Job 已持久化，但本次调度尚未被接受。不要创建新 Case 或换新 `request_id`；先轮询该 Case，必要时以完全相同的业务请求做幂等重放。

### 2.5 长轮询和 Job 切换

- `wait_timed_out=true` 只表示本次有限等待到期，后台 Job 仍可能运行；它不是业务错误。
- 查询时省略 `wait_for_job_id`，服务会在请求开始时选择当前活动 Job；显式提供它则只等待该 Job。
- 推荐循环：读取 `case_view.active_job.job_id`，用它和 `wait_seconds=30` 查询；响应后先处理状态。若仍为 `RUNNING`/`REVIEWING` 且 `active_job.job_id` 已变化，下一次等待新 ID。
- `WAITING_INPUT`、`WAITING_ATTACHMENT`、所有终态和 `INTERRUPTED` 会立即结束等待。
- 浏览器切页或组件卸载时用 `AbortController` 取消本地 HTTP 等待；这不会取消后台 Job。

## 3. 服务与合同入口

### GET `/live`

无 path、query、header 或 body 参数。它只表示 HTTP 进程正在响应，不表示依赖可用。

```json
{"ok":true,"data":{"status":"live"},"error":null}
```

代表性失败：网络错误或非 `200` 表示进程/链路不可用；此端点没有业务错误码。

### GET `/ready`

无 path、query、header 或 body 参数。成功时 `ready` 为 `true`；`checks` 是逐项布尔结果，成功响应中的 `message` 固定为 `null`。

| `checks[].name` | 含义 |
| --- | --- |
| `CONFIG` | 服务配置可用。 |
| `INSTANCE_LOCK` | 单实例锁有效。 |
| `STATE` | 持久化状态可读取。 |
| `DATA_DIRECTORIES` | 所需数据目录可用。 |
| `RECOVERY` | 启动恢复已经完成。 |

```json
{
  "ok": true,
  "data": {
    "ready": true,
    "checks": [
      {"name": "CONFIG", "passed": true, "message": null},
      {"name": "INSTANCE_LOCK", "passed": true, "message": null},
      {"name": "STATE", "passed": true, "message": null},
      {"name": "DATA_DIRECTORIES", "passed": true, "message": null},
      {"name": "RECOVERY", "passed": true, "message": null}
    ],
    "error": null
  },
  "error": null
}
```

代表性失败为 `503` error envelope。前端应保持只读不可操作状态并退避重试；不要用 `/live` 成功覆盖 `/ready` 失败。

### GET `/openapi.json`

无参数。返回 OpenAPI `3.1.0` JSON 文档，不套 envelope。代码生成、请求模型与离线合同校验以它为准。代表性失败是普通网络/服务错误。

### GET `/docs`

无参数。返回 HTML 调试页，不套 envelope，仅供开发调试，不应嵌入生产业务流程。

## 4. 七个业务操作

### POST `/api/v1/cases`

创建新 Case。请求 body：

| 字段 | 类型 | 必填 | 默认 | 约束与含义 |
| --- | --- | --- | --- | --- |
| `request_id` | `string` | 是 | — | 本次创建的稳定幂等键。 |
| `raw_problem_text` | `text` | 是 | — | 用户原始完整描述；原样保存，不要用结构化摘要替换。 |
| `problem_spec` | `ProblemSpecBody` | 是 | — | 对问题的结构化描述。 |
| `initial_user_facts` | `NamedValueBody[]` | 否 | `[]` | 最多 64 项，`name` 唯一；只发送确知且可由服务接收的初始事实。 |
| `wait_seconds` | `integer` | 否 | `0` | `0..30`；创建后等待首个 Job 推进。 |

`ProblemSpecBody`：

| 字段 | 类型 | 必填 | 约束与含义 |
| --- | --- | --- | --- |
| `statement` | `text` | 是 | 要定位的问题。 |
| `expected_behavior` | `text` | 是 | 正常情况下应发生什么。 |
| `actual_behavior` | `text` | 是 | 实际观察到什么。 |
| `scope` | `text` | 是 | 本次定位边界。 |
| `goals` | `text[]` | 是 | 至少 1 项、唯一。 |
| `non_goals` | `text[]` | 是 | 可为空、唯一。 |
| `constraints` | `text[]` | 是 | 可为空、唯一。 |
| `completion_criteria` | `text[]` | 是 | 至少 1 项、唯一。 |

`NamedValueBody`：

| 字段 | 类型 | 必填 | 约束与含义 |
| --- | --- | --- | --- |
| `name` | `name` | 是 | 服务声明的精确输入名称，不做别名推断。 |
| `value` | `text` | 是 | 精确值，不 trim、不猜测、不归一化。 |

完整请求：

```json
{
  "request_id": "web-create-20260818-0001",
  "raw_problem_text": "Payment to inventory RPC times out at 10:30 UTC.",
  "problem_spec": {
    "statement": "Payment to inventory RPC times out.",
    "expected_behavior": "The RPC completes before its deadline.",
    "actual_behavior": "The caller reports a timeout.",
    "scope": "Payment to inventory RPC.",
    "goals": ["Locate an evidenced cause."],
    "non_goals": [],
    "constraints": ["Use only supplied evidence."],
    "completion_criteria": ["Identify an evidenced cause."]
  },
  "initial_user_facts": [
    {"name": "problem_time", "value": "2026-08-18 10:30 UTC"}
  ],
  "wait_seconds": 0
}
```

完整成功响应（`case_view` 允许暂时不可用）：

```json
{
  "ok": true,
  "data": {
    "business_receipt": {
      "operation": "CreateCase",
      "primary_resource_id": "10000000-0000-4000-8000-000000000001",
      "case_id": "10000000-0000-4000-8000-000000000001",
      "case_revision": 1,
      "job_id": "20000000-0000-4000-8000-000000000001",
      "status": "RUNNING"
    },
    "case_view": null,
    "wait_timed_out": false,
    "dispatch_pending": false
  },
  "error": null
}
```

代表性错误：`400 VALIDATION_ERROR`（类型、未知字段、重复名称）、`409 IDEMPOTENCY_CONFLICT`、`422 NO_CAPABILITY`、`500/503` 状态或持久化故障。成功后立即保存回执中的 `case_id`。

### GET `/api/v1/cases/{case_id}`

| 位置 | 字段 | 类型 | 必填 | 默认 | 约束与含义 |
| --- | --- | --- | --- | --- | --- |
| path | `case_id` | `uuid` | 是 | — | 要查询的 Case。 |
| query | `wait_for_job_id` | `uuid` | 否 | `null` | 指定要等待的 Job；省略时选择请求开始时的活动 Job。不要发送字符串 `"null"`。 |
| query | `wait_seconds` | `integer` | 否 | `0` | `0..30`。没有目标 Job 时立即返回。 |

请求示例：

```http
GET /api/v1/cases/10000000-0000-4000-8000-000000000001?wait_for_job_id=20000000-0000-4000-8000-000000000001&wait_seconds=30
```

完整成功响应：

```json
{
  "ok": true,
  "data": {
    "case_view": {
      "case_id": "10000000-0000-4000-8000-000000000001",
      "status": "WAITING_INPUT",
      "case_revision": 3,
      "raw_problem_text": "Payment to inventory RPC times out at 10:30 UTC.",
      "diagnosis_state_revision": 2,
      "problem_spec": {
        "statement": "Payment to inventory RPC times out.",
        "expected_behavior": "The RPC completes before its deadline.",
        "actual_behavior": "The caller reports a timeout.",
        "scope": "Payment to inventory RPC.",
        "goals": ["Locate an evidenced cause."],
        "non_goals": [],
        "constraints": ["Use only supplied evidence."],
        "completion_criteria": ["Identify an evidenced cause."],
        "revision": 1
      },
      "user_facts": [
        {
          "item_id": "50000000-0000-4000-8000-000000000001",
          "statement": "2026-08-18 10:30 UTC",
          "status": "ACTIVE",
          "provenance": {
            "source_type": "USER_INPUT",
            "source_ref": "10000000-0000-4000-8000-000000000001",
            "input_name": "problem_time"
          },
          "evidence_refs": [],
          "created_revision": 1,
          "supersedes": []
        }
      ],
      "confirmed_facts": [],
      "open_questions": [],
      "pending_requirements": [
        {
          "requirement_id": "60000000-0000-4000-8000-000000000001",
          "kind": "INPUT",
          "name": "order_id",
          "prompt": "Provide the affected order ID.",
          "required": true,
          "constraints": {
            "value_type": "STRING",
            "min_utf8_bytes": 1,
            "max_utf8_bytes": 64,
            "pattern": "^order-[0-9]+$",
            "allowed_values": []
          },
          "status": "OPEN",
          "requested_by_job_id": "20000000-0000-4000-8000-000000000001",
          "fulfilled_by_refs": [],
          "supplement_policy": "MISSING_ONLY"
        }
      ],
      "active_job": null,
      "selected_skill_ref": null,
      "final_result": null,
      "unresolved_result": null,
      "generic_result": null,
      "generic_result_v2": null,
      "failure": null,
      "artifacts": [],
      "created_at": "2026-08-18T02:30:00.000Z",
      "updated_at": "2026-08-18T02:31:00.000Z"
    },
    "wait_timed_out": false
  },
  "error": null
}
```

代表性错误：`400 VALIDATION_ERROR`（非法/重复/未知 query）、`404 CASE_NOT_FOUND`、`404 JOB_NOT_FOUND`、`409 JOB_CASE_MISMATCH`、`503 STATE_CORRUPT` 或 `STATE_SCHEMA_UNSUPPORTED`。

### POST `/api/v1/cases/{case_id}/attachments`

先创建一个 `UPLOADING` 附件记录并取得上传描述。它不会采用附件，也不会继续 Case。

| 位置 | 字段 | 类型 | 必填 | 默认 | 约束与含义 |
| --- | --- | --- | --- | --- | --- |
| path | `case_id` | `uuid` | 是 | — | 所属 Case。 |
| body | `request_id` | `string` | 是 | — | 本次准备操作的稳定幂等键。 |
| body | `expected_case_revision` | `integer` | 是 | — | 最新 `case_revision`，大于 `0`。 |
| body | `name` | `text` | 是 | — | 安全文件名而非路径；不得含 `/`、反斜杠、控制字符、`.` 或 `..`。归档后缀必须为小写。 |
| body | `content_type` | `content_type` | 是 | — | 必须与 requirement 及文件后缀匹配。 |
| body | `declared_size` | `integer` | 是 | — | 原始文件字节数，`0..2684354560`。 |
| body | `declared_sha256` | `sha256` | 是 | — | 原始文件字节的散列。 |

后缀映射：`.zip` → `application/zip`；`.tar` → `application/x-tar`；`.tar.gz`、`.tgz`、`.gz` → `application/gzip`。归档后缀大小写必须精确。

```json
{
  "request_id": "web-prepare-20260818-0001",
  "expected_case_revision": 3,
  "name": "rpc-logs.zip",
  "content_type": "application/zip",
  "declared_size": 1024,
  "declared_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

完整成功响应：

```json
{
  "ok": true,
  "data": {
    "application_response": {
      "business_receipt": {
        "operation": "PrepareAttachment",
        "primary_resource_id": "30000000-0000-4000-8000-000000000001",
        "case_id": "10000000-0000-4000-8000-000000000001",
        "case_revision": 4,
        "job_id": null,
        "status": "UPLOADING"
      },
      "case_view": null,
      "wait_timed_out": false,
      "dispatch_pending": false
    },
    "upload": {
      "attachment_id": "30000000-0000-4000-8000-000000000001",
      "method": "PUT",
      "url": "https://locator.example/api/v1/attachments/30000000-0000-4000-8000-000000000001/content",
      "required_headers": {
        "Idempotency-Key": "30000000-0000-4000-8000-000000000001",
        "Content-Type": "application/zip",
        "X-Content-SHA256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      },
      "expected_content_length": 1024,
      "max_bytes": 2684354560,
      "expires_at": null
    }
  },
  "error": null
}
```

代表性错误：`400 VALIDATION_ERROR`、`404 CASE_NOT_FOUND`、`409 REVISION_CONFLICT`、`409 IDEMPOTENCY_CONFLICT`、`409 INVALID_CASE_STATE`、`413 RESOURCE_LIMIT_EXCEEDED`。下一步必须使用响应中的 `upload`，不要自行拼 URL 或请求头。

### PUT `/api/v1/attachments/{attachment_id}/content`

请求体是**原始文件字节**，不是 JSON、base64 或 multipart。

| 位置 | 字段 | 类型 | 必填 | 约束与含义 |
| --- | --- | --- | --- | --- |
| path | `attachment_id` | `uuid` | 是 | `prepare` 返回的 ID。 |
| header | `Idempotency-Key` | `uuid` | 是 | 必须恰好等于 path 中的 `attachment_id`，且只出现一次。 |
| header | `Content-Type` | `content_type` | 是 | 必须等于准备时的值，且只出现一次。 |
| header | `X-Content-SHA256` | `sha256` | 是 | 必须等于准备时的值，且只出现一次。 |
| header | `Content-Length` | 十进制 ASCII integer | 是 | 必须等于 `expected_content_length`。Chrome 根据 `File`/`Blob` 自动生成；浏览器脚本不得设置。 |
| body | binary | `File` 或 `Blob` | 是 | 大小不得超过 `max_bytes`；服务端流式核对实际大小与散列。 |

浏览器请求示例：

```ts
const response = await fetch(upload.url, {
  method: "PUT",
  headers: upload.required_headers,
  body: file,
});
```

完整成功响应：

```json
{
  "ok": true,
  "data": {
    "attachment_id": "30000000-0000-4000-8000-000000000001",
    "case_id": "10000000-0000-4000-8000-000000000001",
    "status": "READY",
    "case_revision": 5
  },
  "error": null
}
```

代表性错误：`400 VALIDATION_ERROR`（四个必需头非法）、`404 ATTACHMENT_NOT_FOUND`、`409 RESOURCE_CASE_MISMATCH`、`409 INVALID_CASE_STATE`、`409 IDEMPOTENCY_CONFLICT`、`413 RESOURCE_LIMIT_EXCEEDED`、`422 RESOURCE_SIZE_MISMATCH`、`422 RESOURCE_HASH_MISMATCH`、`409 UPLOAD_INCOMPLETE`。上传成功只表示 `READY`；仍需 `supplements` 显式采用。

### POST `/api/v1/cases/{case_id}/supplements`

提交当前开放 requirement 的事实和/或已为该 Case 上传成 `READY` 的附件。

| 位置 | 字段 | 类型 | 必填 | 默认 | 约束与含义 |
| --- | --- | --- | --- | --- | --- |
| path | `case_id` | `uuid` | 是 | — | 要继续的 Case。 |
| body | `request_id` | `string` | 是 | — | 本次补充操作的稳定幂等键。 |
| body | `expected_case_revision` | `integer` | 是 | — | 必须使用所有上传完成后的最新 `case_revision`。 |
| body | `inputs` | `NamedValueBody[]` | 是 | — | 可为空；`name` 唯一且必须精确匹配 `OPEN` 的 `INPUT` requirement，并满足其 `constraints`。 |
| body | `attachment_ids` | `uuid[]` | 是 | — | 可为空且唯一；每个附件必须属于该 Case、状态为 `READY`，并匹配开放的 `ATTACHMENT` requirement。 |
| body | `wait_seconds` | `integer` | 否 | `0` | `0..30`。 |

`inputs` 与 `attachment_ids` 不能同时为空。

```json
{
  "request_id": "web-supplement-20260818-0001",
  "expected_case_revision": 5,
  "inputs": [
    {"name": "order_id", "value": "order-123"}
  ],
  "attachment_ids": [
    "30000000-0000-4000-8000-000000000001"
  ],
  "wait_seconds": 0
}
```

完整成功响应：

```json
{
  "ok": true,
  "data": {
    "business_receipt": {
      "operation": "SubmitSupplement",
      "primary_resource_id": "10000000-0000-4000-8000-000000000001",
      "case_id": "10000000-0000-4000-8000-000000000001",
      "case_revision": 6,
      "job_id": "20000000-0000-4000-8000-000000000002",
      "status": "RUNNING"
    },
    "case_view": null,
    "wait_timed_out": false,
    "dispatch_pending": false
  },
  "error": null
}
```

代表性错误：`400 VALIDATION_ERROR`、`404 CASE_NOT_FOUND`/`ATTACHMENT_NOT_FOUND`、`409 REVISION_CONFLICT`、`409 IDEMPOTENCY_CONFLICT`、`409 INVALID_CASE_STATE`、`409 ATTACHMENT_NOT_READY`、`409 RESOURCE_CASE_MISMATCH`、`409 NEW_CASE_REQUIRED`。成功后根据返回的 Job 继续轮询。

### GET `/api/v1/cases/{case_id}/artifacts`

| 位置 | 字段 | 类型 | 必填 | 约束与含义 |
| --- | --- | --- | --- | --- |
| path | `case_id` | `uuid` | 是 | 要列出公开产物的 Case。 |

此端点不接受任何 query。不要根据 `CaseView.artifacts` 自行拼下载 URL；使用此响应的 `download_url`。

完整请求：

```http
GET /api/v1/cases/10000000-0000-4000-8000-000000000001/artifacts
```

完整成功响应：

```json
{
  "ok": true,
  "data": {
    "artifacts": [
      {
        "artifact_id": "40000000-0000-4000-8000-000000000001",
        "kind": "USER_RESULT",
        "name": "diagnosis-result.json",
        "content_type": "application/json",
        "size": 512,
        "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "created_at": "2026-08-18T02:35:00.000Z",
        "download_url": "https://locator.example/api/v1/artifacts/40000000-0000-4000-8000-000000000001/content?case_id=10000000-0000-4000-8000-000000000001"
      }
    ]
  },
  "error": null
}
```

代表性错误：`400 VALIDATION_ERROR`（存在 query）、`404 CASE_NOT_FOUND`、`503 STATE_CORRUPT` 或 `STATE_SCHEMA_UNSUPPORTED`。

### GET `/api/v1/artifacts/{artifact_id}/content`

| 位置 | 字段 | 类型 | 必填 | 约束与含义 |
| --- | --- | --- | --- | --- |
| path | `artifact_id` | `uuid` | 是 | 从产物列表取得。 |
| query | `case_id` | `uuid` | 是 | 必须是唯一 query，并与产物所属 Case 相同。 |

成功为原始二进制，响应头如下：

| 响应头 | 类型 | 含义 |
| --- | --- | --- |
| `Content-Length` | 非负十进制 integer | 权威字节数，必须等于产物列表的 `size`。 |
| `Content-Type` | `content_type` | 文件类型，必须等于产物列表的 `content_type`。 |
| `X-Content-SHA256` | `sha256` | 权威散列，必须等于产物列表的 `sha256`。 |
| `X-Problem-Locator-Correlation-ID` | `uuid` | 本次 HTTP 请求关联 ID。 |

完整请求：

```http
GET /api/v1/artifacts/40000000-0000-4000-8000-000000000001/content?case_id=10000000-0000-4000-8000-000000000001
```

完整成功响应由以上四个响应头和恰好 `Content-Length` 字节组成，不套 JSON envelope。下载完成前不要把文件标记为可用；必须同时校验响应头、实际字节数和 SHA-256。

代表性错误仍为 JSON envelope：`400 VALIDATION_ERROR`、`404 CASE_NOT_FOUND`、`404 ARTIFACT_NOT_FOUND`、`500 RESOURCE_NOT_FOUND`、`422 RESOURCE_SIZE_MISMATCH`、`422 RESOURCE_HASH_MISMATCH`、`503 STATE_CORRUPT` 或 `STATE_SCHEMA_UNSUPPORTED`。

## 5. Case 状态与前端动作

| `CaseStatus` | 是否终态 | 前端必须执行的动作 |
| --- | --- | --- |
| `NEW` | 否 | 保存 `case_id`；短暂状态，立即查询。若写响应 `dispatch_pending=true`，按幂等规则处理。 |
| `RUNNING` | 否 | 展示运行中；以当前 `active_job.job_id` 长轮询。 |
| `WAITING_INPUT` | 否 | 只读取 `status=OPEN` 且 `kind=INPUT` 的 `pending_requirements`，按 `prompt` 和 `constraints` 收集值，再提交 supplement。 |
| `WAITING_ATTACHMENT` | 否 | 只读取 `status=OPEN` 且 `kind=ATTACHMENT` 的 requirement，依次执行 prepare → raw PUT → `READY` → supplement。 |
| `REVIEWING` | 否 | 展示审核中；以新的 `active_job.job_id` 长轮询，不能把先前诊断 Job 的 ID 继续当作当前目标。 |
| `RESOLVED` | 是 | 按非空字段展示 `methods_result`、`final_result`、`generic_result` 或 `generic_result_v2`。Methods V2 读取确认引用和 `limitations`；Generic V2 Markdown 只作为不可信数据渲染一次，并重新列出产物、下载并校验 `GENERIC_REPORT` 字节。 |
| `PARTIALLY_RESOLVED` | 是 | 展示部分结论、未满足的完成条件与限制；重新列出产物并下载、校验。Methods V2 不产生此状态；全局枚举仍供 GENERIC/legacy composite 等其他路径使用。 |
| `UNRESOLVED` | 是 | 按非空字段展示 `methods_result`、`unresolved_result`、`generic_result` 或 `generic_result_v2`。Methods V2 展示 `reason_code`、`diagnostic_id`、`reasons` 和 `limitations`；Generic V2 Markdown 只作为不可信数据渲染一次，并列出可用结果与审计产物。 |
| `FAILED` | 是 | 展示 `failure.code`、`message`、`reason_code` 和 `diagnostic_id`。若 `methods_result` 非空，再展示其中的 `reasons` 和 `limitations`。不要自动创建替代 Case。 |
| `CANCELLED` | 是 | 展示已取消；当前 REST 不能从此状态恢复。 |
| `INTERRUPTED` | 否，但当前 REST 不可推进 | 保留并允许查询；当前 REST 没有恢复端点，不得靠重新提交 supplement 猜测恢复。 |

`active_job` 只会在 `RUNNING` 或 `REVIEWING` 非空；等待状态、终态和 `INTERRUPTED` 均为 `null`。不能根据 `wait_timed_out` 推断状态，必须读取 `case_view.status`。

Methods V2 终态只会是 `RESOLVED`、`UNRESOLVED` 或 `FAILED`。此路径不创建 Candidate，`final_result` 和 `unresolved_result` 始终为 `null`。评估已经生成 Evidence Graph 和 Evaluation Plan 时，终态读取 `methods_result`。若资源解析、Workspace、Logparse 预处理或 execution-record 在 Graph/Plan 生成前失败，服务端不会伪造这些引用；此时 `methods_result` 缺省，稳定原因和诊断 ID 位于 `failure.reason_code` 与 `failure.diagnostic_id`。全局 `CaseStatus`、`CandidateConclusion` 和 `final_result` 仍服务于其他路径，不能因为 Methods V2 的硬切而从客户端类型中删除。

## 6. 附件端到端流程

1. 从最新 `CaseView.pending_requirements` 选择一个 `OPEN` 的 `ATTACHMENT` requirement；校验 `allowed_content_types` 和 `min_count..max_count`。
2. 通过注入的 Web Worker port 流式计算文件 SHA-256，读取 `file.size`；超过 `2684354560` 时停止，prepare 后再与响应的 `max_bytes` 核对。
3. 以当前 `case_revision` 调用 prepare，保存 `attachment_id`、返回的新 revision 和完整 `upload`。
4. 把 `measureBlob` 返回的 `size`、`sha256` 与 `file.size`、`upload.expected_content_length`、`required_headers["X-Content-SHA256"]` 全部核对；把同一个不可变 `File`/`Blob` 直接 PUT 到 `upload.url`。不要设置 `Content-Length`。
5. 只有 PUT 返回 `status=READY` 才算上传完成；保存其新 `case_revision`。
6. 多附件必须串行推进 revision：每个 prepare 使用上一步最新 revision，每个 PUT 后再保存新的 revision。不要并发 prepare 或假设 revision 只在 supplement 时变化。
7. 全部附件 `READY` 后，使用最新 revision 和一个新的稳定 `request_id` 调用 supplement。直到该调用成功，附件都没有被当前诊断采用。

## 7. 框架无关 TypeScript/`fetch` 客户端

以下代码覆盖七个业务操作、错误解析、长轮询、上传和下载校验。为保持示例可审计，嵌套响应类型在下一节附录定义；生产项目可由完整 OpenAPI 合同生成等价类型。

```ts
type UUID = string;
type Sha256 = string;
const MAX_ATTACHMENT_BYTES_V1 = 2_684_354_560;

interface ApiErrorDetail {
  field: string | null;
  resource_type: string | null;
  resource_id: UUID | null;
  resource_ref: { id: string; version: string; content_hash: Sha256 } | null;
  expected: string | number | boolean | null;
  actual: string | number | boolean | null;
  limit: number | null;
  observed: number | null;
}

interface ApiError {
  code: string;
  message: string;
  details: ApiErrorDetail[];
  retryable: boolean;
}

type Success<T> = { ok: true; data: T; error: null };
type Failure = { ok: false; data: null; error: ApiError };

class RestError extends Error {
  constructor(
    readonly status: number,
    readonly correlationId: string | null,
    readonly error: ApiError,
  ) {
    super(`${error.code}: ${error.message}`);
  }
}

interface ProblemSpecBody {
  statement: string;
  expected_behavior: string;
  actual_behavior: string;
  scope: string;
  goals: string[];
  non_goals: string[];
  constraints: string[];
  completion_criteria: string[];
}

interface NamedValueBody { name: string; value: string }

type CaseStatus =
  | "NEW" | "RUNNING" | "WAITING_INPUT" | "WAITING_ATTACHMENT" | "REVIEWING"
  | "RESOLVED" | "PARTIALLY_RESOLVED" | "UNRESOLVED" | "FAILED"
  | "CANCELLED" | "INTERRUPTED";
type JobStatus = "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED" | "INTERRUPTED";
type ArtifactKind = "USER_RESULT" | "USER_RESULT_ARCHIVE" | "DIAGNOSTIC_EXPORT" | "LOGPARSE_RUN" | "AUDIT_BUNDLE" | "GENERIC_REPORT";

interface VersionedRef { id: string; version: string; content_hash: Sha256 }
interface ProblemSpec extends ProblemSpecBody { revision: number }
interface DiagnosisProvenance {
  source_type: "USER_INPUT" | "AGENT_OUTCOME";
  source_ref: UUID;
  input_name: string | null;
}
interface DiagnosisItem {
  item_id: UUID;
  statement: string;
  status: "ACTIVE" | "RESOLVED" | "REJECTED" | "SUPERSEDED";
  provenance: DiagnosisProvenance;
  evidence_refs: UUID[];
  created_revision: number;
  supersedes: UUID[];
}
interface InputRequirementConstraints {
  value_type: "STRING";
  min_utf8_bytes: number;
  max_utf8_bytes: number;
  pattern: string | null;
  allowed_values: string[];
}
interface AttachmentRequirementConstraints {
  allowed_content_types: string[];
  min_count: number;
  max_count: number;
}
interface PendingRequirementBase {
  requirement_id: UUID;
  name: string;
  prompt: string;
  required: true;
  status: "OPEN" | "FULFILLED";
  requested_by_job_id: UUID;
  fulfilled_by_refs: UUID[];
  supplement_policy: "NONE" | "MISSING_ONLY";
}
type PendingRequirement = PendingRequirementBase & (
  | { kind: "INPUT"; constraints: InputRequirementConstraints }
  | { kind: "ATTACHMENT"; constraints: AttachmentRequirementConstraints }
);
interface JobSummary {
  job_id: UUID;
  job_type: "ROUTE" | "DIAGNOSE" | "REVIEW";
  diagnosis_mode: "SPECIALIZED" | "GENERIC" | null;
  status: JobStatus;
  goal: string;
  base_state_revision: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}
interface CausalFactor {
  factor_id: string;
  role: "CAUSE" | "CONTRIBUTOR" | "CONDITION";
  statement: string;
  evidence_refs: UUID[];
  required_rule_ids: string[];
}
interface CompletionCriterionMapping {
  criterion_index: number;
  criterion: string;
  status: "SATISFIED" | "PARTIALLY_SATISFIED" | "UNSATISFIED" | "UNKNOWN";
  evidence_refs: UUID[];
  explanation: string;
}
interface CandidateConclusion {
  conclusion_id: UUID;
  revision: number;
  content_hash: Sha256;
  resolution_status: "COMPLETE" | "PARTIAL";
  terminal_path_id: string;
  statement: string;
  causal_factors: CausalFactor[];
  candidate_factors: CausalFactor[];
  excluded_factors: CausalFactor[];
  supporting_evidence_refs: UUID[];
  completion_criteria_mapping: CompletionCriterionMapping[];
  proposed_by_job_id: UUID;
  status: "PROPOSED" | "REVIEWING" | "REJECTED" | "ACCEPTED";
}
interface UnresolvedResult {
  source_job_id: UUID;
  source_outcome_id: UUID;
  reason_code: "MECHANICAL_VERIFICATION_FAILED" | "INSUFFICIENT_EVIDENCE" | "SEMANTIC_REVIEW_REJECTED" | "INVALID_NEED_MORE_REQUEST";
  summary: string;
  blocking_rule_ids: string[];
  evidence_refs: UUID[];
  user_result_artifact_id: UUID;
  recommended_next_step: string;
  occurred_at: string;
  audit_artifact_id: UUID;
}
interface GenericResult {
  status: "RESOLVED" | "UNRESOLVED";
  conclusion: string;
  root_cause_analysis: string;
  skill_name: string;
  source_job_id: UUID;
  source_outcome_id: UUID;
  occurred_at: string;
}
interface GenericResultV2 {
  format_version: 2;
  status: "RESOLVED" | "UNRESOLVED";
  report_markdown: string;
  report_utf8_size: number;
  report_sha256: Sha256;
  report_artifact_id: UUID;
  skill_name: string;
  source_job_id: UUID;
  source_outcome_id: UUID;
  occurred_at: string;
}
type MethodsTerminalReasonCodeV2 =
  | "SPECIALIST_PROTOCOL_REPAIR_EXHAUSTED"
  | "REVIEWER_PROTOCOL_REPAIR_EXHAUSTED"
  | "SPECIALIST_SEMANTIC_INVALID"
  | "REVIEWER_SEMANTIC_INVALID"
  | "SPECIALIST_MODEL_EXECUTION_FAILED"
  | "REVIEWER_MODEL_EXECUTION_FAILED"
  | "SPECIALIST_REVIEWER_DISAGREEMENT"
  | "INCOMPLETE_EVALUATION"
  | "NO_CONFIRMED_METHOD"
  | "NO_MATCHING_METHOD_EVIDENCE"
  | "RESOURCE_SNAPSHOT_DRIFT"
  | "SERVER_INVARIANT_VIOLATION"
  | "AUDIT_ARCHIVE_FAILED";
type MethodsValidationReasonCode =
  | "METHOD_EVIDENCE_MARKER_NOT_INDEXED"
  | "METHOD_CONFIRMED_EVIDENCE_MISSING"
  | "METHOD_CONFIRMED_MARKER_SCAN_MISS"
  | "METHOD_EVIDENCE_SOURCE_CHANGED"
  | "METHOD_VALIDATION_FAILED";
interface MethodsTerminalProjectionV2 {
  schema_version: 2;
  case_id: UUID;
  source_job_id: UUID;
  result_ref: string;
  evaluation_id: UUID;
  status: "RESOLVED" | "UNRESOLVED" | "FAILED";
  plan_ref: string;
  evidence_graph_ref: string;
  reason_code: MethodsTerminalReasonCodeV2 | null;
  diagnostic_id: string;
  diagnostic_evaluation_ref: string | null;
  confirmed_evaluation_refs: string[];
  confirmed_method_ids: string[];
  confirmed_event_refs: string[];
  confirmed_hit_refs: string[];
  limitations: string[];
  reasons: string[];
}
interface CaseFailure {
  code: string;
  message: string;
  source_job_id: UUID | null;
  source_outcome_id: UUID | null;
  occurred_at: string;
  reason_code?: MethodsValidationReasonCode | MethodsTerminalReasonCodeV2 | null;
  diagnostic_id?: string | null;
}
interface ArtifactSummary {
  artifact_id: UUID;
  kind: ArtifactKind;
  name: string;
  content_type: string;
  resource_kind: "FILE" | "DIRECTORY";
  size: number;
  sha256: Sha256;
  created_by_job_id: UUID;
  created_at: string;
  downloadable: boolean;
}
interface ApplicationResponse {
  business_receipt: {
    operation: string;
    primary_resource_id: UUID;
    case_id: UUID | null;
    case_revision: number | null;
    job_id: UUID | null;
    status: string;
  };
  case_view: CaseView | null;
  wait_timed_out: boolean;
  dispatch_pending: boolean;
}

interface CaseView {
  case_id: UUID;
  status: CaseStatus;
  case_revision: number;
  raw_problem_text: string;
  diagnosis_state_revision: number;
  problem_spec: ProblemSpec;
  user_facts: DiagnosisItem[];
  confirmed_facts: DiagnosisItem[];
  open_questions: DiagnosisItem[];
  pending_requirements: PendingRequirement[];
  active_job: JobSummary | null;
  selected_skill_ref: VersionedRef | null;
  final_result: CandidateConclusion | null;
  unresolved_result: UnresolvedResult | null;
  generic_result: GenericResult | null;
  generic_result_v2: GenericResultV2 | null;
  methods_result?: MethodsTerminalProjectionV2;
  failure: CaseFailure | null;
  artifacts: ArtifactSummary[];
  created_at: string;
  updated_at: string;
}

interface UploadDescriptor {
  attachment_id: UUID;
  method: "PUT";
  url: string;
  required_headers: Record<"Idempotency-Key" | "Content-Type" | "X-Content-SHA256", string>;
  expected_content_length: number;
  max_bytes: 2684354560;
  expires_at: null;
}

interface ArtifactView {
  artifact_id: UUID;
  kind: ArtifactKind;
  name: string;
  content_type: string;
  size: number;
  sha256: Sha256;
  created_at: string;
  download_url: string;
}

interface BlobMeasurement { size: number; sha256: Sha256 }

// 实现方把这个 port 代理到一个专用 Web Worker；update 每次只传一个 chunk。
interface WorkerSha256Port {
  update(chunk: Uint8Array): Promise<void>;
  digestHex(): Promise<Sha256>;
  terminate(): void;
}
type WorkerSha256Factory = () => Promise<WorkerSha256Port>;

// sink 必须先暂存数据；只有 commit 后才能向用户暴露文件，abort 必须丢弃暂存内容。
interface DownloadSink {
  write(chunk: Uint8Array): Promise<void>;
  commit(): Promise<void>;
  abort(reason: Error): Promise<void>;
}

function joinBase(baseUrl: string, path: string): string {
  return `${baseUrl.replace(/\/$/, "")}${path}`;
}

async function jsonRequest<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, { ...init, credentials: "omit" });
  const correlationId = response.headers.get("X-Problem-Locator-Correlation-ID");
  const payload = (await response.json()) as Success<T> | Failure;
  if (!response.ok || !payload.ok) {
    if (payload.ok) throw new Error(`HTTP ${response.status}; correlation=${correlationId}`);
    throw new RestError(response.status, correlationId, payload.error);
  }
  return payload.data;
}

function asError(cause: unknown): Error {
  return cause instanceof Error ? cause : new Error(String(cause));
}

async function measureBlobIncrementally(
  blob: Blob,
  createWorker: WorkerSha256Factory,
  signal?: AbortSignal,
): Promise<BlobMeasurement> {
  if (blob.size > MAX_ATTACHMENT_BYTES_V1) {
    throw new Error("File exceeds the V1 attachment limit");
  }
  const worker = await createWorker();
  const reader = blob.stream().getReader();
  let size = 0;
  try {
    while (true) {
      signal?.throwIfAborted();
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      // slice 允许 Worker 实现 transfer 这个副本，而不破坏调用方仍持有的 chunk。
      await worker.update(value.slice());
    }
    const sha256 = await worker.digestHex();
    if (!/^[0-9a-f]{64}$/.test(sha256)) throw new Error("Worker returned an invalid SHA-256");
    return { size, sha256 };
  } catch (cause) {
    const error = asError(cause);
    await reader.cancel(error).catch(() => undefined);
    throw error;
  } finally {
    worker.terminate();
  }
}

function abortableDelay(milliseconds: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    try {
      signal?.throwIfAborted();
    } catch (cause) {
      reject(asError(cause));
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(asError(signal?.reason));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export class LocatorRestClient {
  constructor(
    readonly baseUrl: string,
    private readonly createSha256Worker: WorkerSha256Factory,
  ) {}

  measureBlob(blob: Blob, signal?: AbortSignal): Promise<BlobMeasurement> {
    return measureBlobIncrementally(blob, this.createSha256Worker, signal);
  }

  createCase(body: {
    request_id: string;
    raw_problem_text: string;
    problem_spec: ProblemSpecBody;
    initial_user_facts?: NamedValueBody[];
    wait_seconds?: number;
  }): Promise<ApplicationResponse> {
    return jsonRequest(joinBase(this.baseUrl, "/api/v1/cases"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ initial_user_facts: [], wait_seconds: 0, ...body }),
    });
  }

  getCase(caseId: UUID, options: {
    waitForJobId?: UUID;
    waitSeconds?: number;
    signal?: AbortSignal;
  } = {}): Promise<{ case_view: CaseView; wait_timed_out: boolean }> {
    const query = new URLSearchParams();
    if (options.waitForJobId !== undefined) query.set("wait_for_job_id", options.waitForJobId);
    query.set("wait_seconds", String(options.waitSeconds ?? 0));
    return jsonRequest(
      joinBase(this.baseUrl, `/api/v1/cases/${encodeURIComponent(caseId)}?${query}`),
      { signal: options.signal },
    );
  }

  prepareAttachment(caseId: UUID, body: {
    request_id: string;
    expected_case_revision: number;
    name: string;
    content_type: string;
    declared_size: number;
    declared_sha256: Sha256;
  }): Promise<{ application_response: ApplicationResponse; upload: UploadDescriptor }> {
    return jsonRequest(joinBase(
      this.baseUrl,
      `/api/v1/cases/${encodeURIComponent(caseId)}/attachments`,
    ), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  uploadAttachment(upload: UploadDescriptor, file: File, measured: BlobMeasurement): Promise<{
    attachment_id: UUID;
    case_id: UUID;
    status: "READY";
    case_revision: number;
  }> {
    if (
      measured.size !== file.size ||
      file.size !== upload.expected_content_length ||
      file.size > upload.max_bytes
    ) {
      throw new Error("File size no longer matches the prepared upload");
    }
    if (measured.sha256 !== upload.required_headers["X-Content-SHA256"]) {
      throw new Error("File SHA-256 no longer matches the prepared upload");
    }
    return jsonRequest(upload.url, {
      method: upload.method,
      headers: upload.required_headers,
      body: file,
    });
  }

  submitSupplement(caseId: UUID, body: {
    request_id: string;
    expected_case_revision: number;
    inputs: NamedValueBody[];
    attachment_ids: UUID[];
    wait_seconds?: number;
  }): Promise<ApplicationResponse> {
    return jsonRequest(joinBase(
      this.baseUrl,
      `/api/v1/cases/${encodeURIComponent(caseId)}/supplements`,
    ), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ wait_seconds: 0, ...body }),
    });
  }

  listArtifacts(caseId: UUID): Promise<{ artifacts: ArtifactView[] }> {
    return jsonRequest(joinBase(
      this.baseUrl,
      `/api/v1/cases/${encodeURIComponent(caseId)}/artifacts`,
    ));
  }

  async downloadArtifact(
    artifact: ArtifactView,
    sink: DownloadSink,
    signal?: AbortSignal,
  ): Promise<BlobMeasurement> {
    let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
    let worker: WorkerSha256Port | null = null;
    try {
      const response = await fetch(artifact.download_url, { credentials: "omit", signal });
      const correlationId = response.headers.get("X-Problem-Locator-Correlation-ID");
      if (!response.ok) {
        const failure = (await response.json()) as Failure;
        throw new RestError(response.status, correlationId, failure.error);
      }
      const rawLength = response.headers.get("Content-Length");
      const declaredHash = response.headers.get("X-Content-SHA256");
      const contentType = response.headers.get("Content-Type");
      if (rawLength === null || !/^(?:0|[1-9][0-9]*)$/.test(rawLength)) {
        throw new Error("Artifact Content-Length header is missing or invalid");
      }
      if (Number(rawLength) !== artifact.size) throw new Error("Artifact size header mismatch");
      if (declaredHash !== artifact.sha256) throw new Error("Artifact hash header mismatch");
      if (contentType !== artifact.content_type) throw new Error("Artifact content type mismatch");
      if (response.body === null) throw new Error("Artifact response has no byte stream");

      worker = await this.createSha256Worker();
      reader = response.body.getReader();
      let size = 0;
      while (true) {
        signal?.throwIfAborted();
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > artifact.size) throw new Error("Artifact byte stream exceeds declared size");
        await worker.update(value.slice());
        await sink.write(value);
      }
      if (size !== artifact.size) throw new Error("Artifact byte stream size mismatch");
      const sha256 = await worker.digestHex();
      if (sha256 !== artifact.sha256) throw new Error("Artifact bytes hash mismatch");
      await sink.commit();
      return { size, sha256 };
    } catch (cause) {
      const error = asError(cause);
      if (reader !== null) await reader.cancel(error).catch(() => undefined);
      await sink.abort(error).catch(() => undefined);
      throw error;
    } finally {
      worker?.terminate();
    }
  }
}

export async function pollUntilActionable(
  client: LocatorRestClient,
  initial: CaseView,
  signal?: AbortSignal,
): Promise<CaseView> {
  let view = initial;
  while (view.status === "NEW" || view.status === "RUNNING" || view.status === "REVIEWING") {
    // NEW 没有 active_job，服务会立即返回；退避避免形成忙循环。
    if (view.status === "NEW") await abortableDelay(500, signal);
    const result = await client.getCase(view.case_id, {
      waitForJobId: view.active_job?.job_id,
      waitSeconds: view.active_job === null ? 0 : 30,
      signal,
    });
    view = result.case_view;
  }
  return view;
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function sha256SmallBlob(blob: Blob): Promise<Sha256> {
  const SMALL_FILE_LIMIT = 64 * 1024 * 1024;
  if (blob.size > SMALL_FILE_LIMIT) {
    throw new Error("Use the incremental Web Worker hasher for this file");
  }
  return bytesToHex(new Uint8Array(await crypto.subtle.digest("SHA-256", await blob.arrayBuffer())));
}
```

附件调用顺序必须把同一次测量贯穿 prepare 和 PUT：

```ts
const measured = await client.measureBlob(file, signal);
const prepared = await client.prepareAttachment(caseId, {
  request_id: crypto.randomUUID(),
  expected_case_revision: latestCaseRevision,
  name: file.name,
  content_type: selectedContentType,
  declared_size: measured.size,
  declared_sha256: measured.sha256,
});
const ready = await client.uploadAttachment(prepared.upload, file, measured);
latestCaseRevision = ready.case_revision;
```

下载时传入的 `DownloadSink` 必须提供暂存语义，例如基于当前稳定版 Chrome File System Access API 的临时目标：`write` 只追加 chunk，`commit` 在大小与 SHA-256 全部通过后发布，`abort` 删除未验证内容。`downloadArtifact` 不创建整包 `Blob`，而是对 `response.body` 单次流式读取，同时计数、向 Worker 发送 chunk 副本并写入 sink。

`crypto.subtle.digest` 需要 HTTPS 等安全上下文，并且不是增量 API。上面的 `sha256SmallBlob` 只适合明确受限的小文件预览，不是上传或下载的通用路径。附件上限约 2.5 GiB，接近上限时绝不能调用 `file.arrayBuffer()`，也不能把所有 chunk 保存后再合并。生产实现应：

1. 在 Web Worker 内创建一个项目锁定版本、经测试的增量 SHA-256 实现，并用消息代理实现 `WorkerSha256Port`；每个实例只处理一个流。
2. `measureBlob` 或 `downloadArtifact` 顺序读取 `Uint8Array`，每次把 chunk 副本交给 Worker `update` 后立即释放，最终由 `digestHex` 返回 64 位小写 hex。
3. 提供取消消息并在取消时 `reader.cancel()`；显示已处理字节数。
4. 用空文件、`abc`、跨 chunk 边界和大于内存预算的 fixture 做已知向量测试；上传前再次核对 `file.size`，下载后同样用流式实现核对散列。
5. 不要尝试把各 chunk 的 SHA-256 再做一次 SHA-256；那不等于整个文件的 SHA-256。

## 8. 响应模型附录

除特别注明“可省略”外，服务实际 JSON 序列化会输出下列字段；`null` 也是显式值。所有 object 都拒绝未知字段。请求模型已在各端点中给出，本节集中定义所有 REST 可达响应模型。

### 8.1 Envelope、健康状态与写回执

| 模型 | 字段 | 类型 | 含义 |
| --- | --- | --- | --- |
| `SuccessEnvelope<T>` | `ok` | literal `true` | 成功判据。 |
| `SuccessEnvelope<T>` | `data` | `T` | 端点数据。 |
| `SuccessEnvelope<T>` | `error` | literal `null` | 成功时固定为空。 |
| `ErrorEnvelope` | `ok` | literal `false` | 失败判据。 |
| `ErrorEnvelope` | `data` | literal `null` | 失败时固定为空。 |
| `ErrorEnvelope` | `error` | `ApplicationError` | 结构化错误。 |
| `LiveData` | `status` | literal `"live"` | HTTP 进程存活。 |
| `ReadinessData` | `ready` | literal `true` | 所有就绪检查通过。 |
| `ReadinessData` | `checks` | `ReadinessCheckData[]` | 逐项检查。 |
| `ReadinessData` | `error` | literal `null` | 成功时固定为空；就绪失败走外层 error envelope。 |
| `ReadinessCheckData` | `name` | `string` | 稳定检查名。 |
| `ReadinessCheckData` | `passed` | `boolean` | 是否通过。 |
| `ReadinessCheckData` | `message` | literal `null` | 不向前端暴露基础设施文本。 |
| `ApplicationResponse` | `business_receipt` | `BusinessReceipt` | 已持久化写操作的权威回执。 |
| `ApplicationResponse` | `case_view` | `CaseView \| null` | 写后投影；读取状态失败时可为 `null`，不否定回执。 |
| `ApplicationResponse` | `wait_timed_out` | `boolean` | 本次有限等待是否到期。 |
| `ApplicationResponse` | `dispatch_pending` | `boolean` | 已创建 Job 是否仍待调度接受。 |
| `BusinessReceipt` | `operation` | `string` | 已执行的操作名。 |
| `BusinessReceipt` | `primary_resource_id` | `uuid` | 本次主要资源；创建时为 Case，准备时为附件。 |
| `BusinessReceipt` | `case_id` | `uuid \| null` | 关联 Case。业务写操作应非空。 |
| `BusinessReceipt` | `case_revision` | `integer \| null` | 写入后的 revision。业务写操作应非空。 |
| `BusinessReceipt` | `job_id` | `uuid \| null` | 本次创建/唤醒的 Job；没有则为 `null`。 |
| `BusinessReceipt` | `status` | `string` | 写入后的资源/Case 状态回执。 |
| `CaseQueryResponse` | `case_view` | `CaseView` | 当前完整投影。 |
| `CaseQueryResponse` | `wait_timed_out` | `boolean` | 本次查询等待是否到期。 |
| `PrepareAttachmentData` | `application_response` | `ApplicationResponse` | 准备操作回执与可选 Case 投影。 |
| `PrepareAttachmentData` | `upload` | `WebUploadDescriptor` | 浏览器上传所需的完整描述。 |
| `UploadReadyData` | `attachment_id` | `uuid` | 已上传附件。 |
| `UploadReadyData` | `case_id` | `uuid` | 所属 Case。 |
| `UploadReadyData` | `status` | literal `"READY"` | 上传内容已发布，但尚未表示被诊断采用。 |
| `UploadReadyData` | `case_revision` | `integer` | 上传完成后的新 revision。 |
| `ArtifactListData` | `artifacts` | `ArtifactView[]` | 当前公开且可下载的产物。 |

### 8.2 `CaseView` 与问题数据

| 模型 | 字段 | 类型 | 含义 |
| --- | --- | --- | --- |
| `CaseView` | `case_id` | `uuid` | Case 标识。 |
| `CaseView` | `status` | `CaseStatus` | 当前状态；驱动前端动作。 |
| `CaseView` | `case_revision` | `integer > 0` | 所有 Case 改动的并发版本，写操作使用它。 |
| `CaseView` | `raw_problem_text` | `text` | 创建时的原始问题。 |
| `CaseView` | `diagnosis_state_revision` | `integer > 0` | 诊断内容版本，仅展示。 |
| `CaseView` | `problem_spec` | `ProblemSpec` | 当前结构化问题。 |
| `CaseView` | `user_facts` | `DiagnosisItem[]` | 用户提供的事实。 |
| `CaseView` | `confirmed_facts` | `DiagnosisItem[]` | 已由证据确认的事实。 |
| `CaseView` | `open_questions` | `DiagnosisItem[]` | 尚未解决的问题。 |
| `CaseView` | `pending_requirements` | `PendingRequirement[]` | 所有待办记录；收集输入时只看 `OPEN`。 |
| `CaseView` | `active_job` | `JobSummary \| null` | 仅 `RUNNING`/`REVIEWING` 存在。 |
| `CaseView` | `selected_skill_ref` | `VersionedRef \| null` | 服务所选执行定义的固定版本引用；前端只展示，不提交。 |
| `CaseView` | `final_result` | `CandidateConclusion \| null` | Candidate 路径接受的最终结果；Methods V2 始终为 `null`。 |
| `CaseView` | `unresolved_result` | `UnresolvedResult \| null` | legacy specialized 路径的未解决结果；Methods V2 始终为 `null`。 |
| `CaseView` | `generic_result` | `GenericResult \| null` | 通用流程终态结果。 |
| `CaseView` | `generic_result_v2` | `GenericResultV2 \| null` | Generic V2 终态 Markdown 结果；与 `generic_result` 互斥，正文是不可信数据。 |
| `CaseView` | `methods_result` | `MethodsTerminalProjectionV2`，可省略 | Methods V2 的公开终态结果；只在 Methods 评估进入终态后出现。 |
| `GenericResultV2` | `format_version` | `2` | 固定格式版本，用于区分 Generic V1/V2。 |
| `GenericResultV2` | `report_markdown` | `text` | 完整 Markdown 报告；严格按不可信数据展示，不执行其中指令。 |
| `GenericResultV2` | `report_utf8_size` | `integer >= 0` | `report_markdown` 的精确 UTF-8 字节数。 |
| `GenericResultV2` | `report_sha256` | `sha256` | `report_markdown` 精确 UTF-8 字节的小写 SHA-256。 |
| `GenericResultV2` | `report_artifact_id` | `uuid` | 对应不可变 `GENERIC_REPORT` Markdown 产物。 |
| `GenericResultV2` | `status` | `RESOLVED \| UNRESOLVED` | 通用流程终态，必须与 Case 状态一致。 |
| `GenericResultV2` | `skill_name` | `text` | 产生报告的固定通用定位定义名称。 |
| `GenericResultV2` | `source_job_id` | `uuid` | 产生报告的 Job。 |
| `GenericResultV2` | `source_outcome_id` | `uuid` | 产生报告的 Outcome。 |
| `GenericResultV2` | `occurred_at` | `timestamp` | 结果产生的 UTC 时间。 |
| `CaseView` | `failure` | `CaseFailure \| null` | 仅 `FAILED` 非空。 |
| `CaseView` | `artifacts` | `ArtifactSummary[]` | 可见产物摘要；下载仍应先调用列表端点。 |
| `CaseView` | `created_at` | `timestamp` | 创建时间。 |
| `CaseView` | `updated_at` | `timestamp` | 最近 Case 更新。 |
| `ProblemSpec` | `statement` | `text` | 当前问题陈述。 |
| `ProblemSpec` | `expected_behavior` | `text` | 期望行为。 |
| `ProblemSpec` | `actual_behavior` | `text` | 实际行为。 |
| `ProblemSpec` | `scope` | `text` | 定位边界。 |
| `ProblemSpec` | `goals` | `text[]`，至少 1 项 | 目标。 |
| `ProblemSpec` | `non_goals` | `text[]` | 非目标。 |
| `ProblemSpec` | `constraints` | `text[]` | 约束。 |
| `ProblemSpec` | `completion_criteria` | `text[]`，至少 1 项 | 完成判据。 |
| `ProblemSpec` | `revision` | `integer > 0` | 问题定义自身版本，不用于 Case 写并发。 |

### 8.3 事实、问题与 provenance

| 模型 | 字段 | 类型 | 含义 |
| --- | --- | --- | --- |
| `DiagnosisItem` | `item_id` | `uuid` | 事实/问题项 ID。 |
| `DiagnosisItem` | `statement` | `text` | 内容。 |
| `DiagnosisItem` | `status` | `DiagnosisItemStatus` | 生命周期状态。 |
| `DiagnosisItem` | `provenance` | `DiagnosisProvenance` | 来源。 |
| `DiagnosisItem` | `evidence_refs` | `uuid[]` | 支撑证据 ID。 |
| `DiagnosisItem` | `created_revision` | `integer > 0` | 首次出现的诊断内容版本。 |
| `DiagnosisItem` | `supersedes` | `uuid[]` | 被本项替代的旧项。 |
| `DiagnosisProvenance` | `source_type` | `DiagnosisProvenanceType` | 用户输入或后台结果。 |
| `DiagnosisProvenance` | `source_ref` | `uuid` | 对应来源资源。 |
| `DiagnosisProvenance` | `input_name` | `name \| null` | 用户输入时为精确 requirement 名；后台结果时为 `null`。 |

### 8.4 Requirements

| 模型 | 字段 | 类型 | 含义 |
| --- | --- | --- | --- |
| `PendingRequirement` | `requirement_id` | `uuid` | requirement ID。 |
| `PendingRequirement` | `kind` | `RequirementKind` | 所需内容是字符串还是附件。 |
| `PendingRequirement` | `name` | `name` | supplement 必须原样使用的键。 |
| `PendingRequirement` | `prompt` | `text` | 展示给用户的提问。 |
| `PendingRequirement` | `required` | literal `true` | 当前合同中的 requirement 均必需。 |
| `PendingRequirement` | `constraints` | `InputRequirementConstraints \| AttachmentRequirementConstraints` | 根据 `kind` 选择对应形态。 |
| `PendingRequirement` | `status` | `RequirementStatus` | 是否仍开放。 |
| `PendingRequirement` | `requested_by_job_id` | `uuid` | 提出它的 Job。 |
| `PendingRequirement` | `fulfilled_by_refs` | `uuid[]` | 已满足时的事实或附件引用；`OPEN` 时为空。 |
| `PendingRequirement` | `supplement_policy` | `SupplementPolicy` | 是否允许通过 supplement 补充，缺省 wire 值为 `NONE`。 |
| `InputRequirementConstraints` | `value_type` | literal `"STRING"` | 当前只接受字符串。 |
| `InputRequirementConstraints` | `min_utf8_bytes` | `integer > 0` | 最小 UTF-8 字节数。 |
| `InputRequirementConstraints` | `max_utf8_bytes` | `integer > 0` | 最大 UTF-8 字节数，且不超过 65,536。 |
| `InputRequirementConstraints` | `pattern` | `string \| null` | 非空时按 Python `fullmatch` 语义校验；不要直接假设它等价于 JavaScript `RegExp`，浏览器预校验只能作为提示，服务端结果才是权威。 |
| `InputRequirementConstraints` | `allowed_values` | `text[]` | 非空时只能从中选择。 |
| `AttachmentRequirementConstraints` | `allowed_content_types` | `content_type[]` | 可接受类型，唯一。 |
| `AttachmentRequirementConstraints` | `min_count` | `integer > 0` | 最少附件数。 |
| `AttachmentRequirementConstraints` | `max_count` | `integer > 0` | 最多附件数，且不小于 `min_count`。 |

### 8.5 Job 与版本引用

| 模型 | 字段 | 类型 | 含义 |
| --- | --- | --- | --- |
| `JobSummary` | `job_id` | `uuid` | Job ID，也是长轮询目标。 |
| `JobSummary` | `job_type` | `JobType` | Job 阶段。 |
| `JobSummary` | `diagnosis_mode` | `DiagnosisMode \| null` | 仅诊断阶段可能存在。 |
| `JobSummary` | `status` | `JobStatus` | Job 生命周期状态。 |
| `JobSummary` | `goal` | `text` | 本 Job 目标。 |
| `JobSummary` | `base_state_revision` | `integer > 0` | Job 开始所依据的诊断内容版本。 |
| `JobSummary` | `created_at` | `timestamp` | 创建时间。 |
| `JobSummary` | `started_at` | `timestamp \| null` | 开始时间。 |
| `JobSummary` | `finished_at` | `timestamp \| null` | 结束时间。 |
| `VersionedRef` | `id` | `text` | 固定资产标识。 |
| `VersionedRef` | `version` | `text` | 版本。 |
| `VersionedRef` | `content_hash` | `sha256` | 版本内容散列。 |

### 8.6 解决结果

| 模型 | 字段 | 类型 | 含义 |
| --- | --- | --- | --- |
| `MethodsTerminalProjectionV2` | `schema_version` | literal `2` | 嵌套 Methods 结果的 wire 版本。 |
| `MethodsTerminalProjectionV2` | `case_id` | `uuid` | 所属 Case，必须与外层 `CaseView.case_id` 相同。 |
| `MethodsTerminalProjectionV2` | `source_job_id` | `uuid` | 产生终态投影的 Job。 |
| `MethodsTerminalProjectionV2` | `result_ref` | `string` | 服务端终态结果引用，格式为 `^result-[0-9a-f]{64}$`。 |
| `MethodsTerminalProjectionV2` | `evaluation_id` | `uuid` | Specialist 和 Reviewer 两个隔离 Job 共用的评估 ID。 |
| `MethodsTerminalProjectionV2` | `status` | `RESOLVED \| UNRESOLVED \| FAILED` | Methods V2 终态，必须与外层 `CaseView.status` 相同；不包含 `PARTIALLY_RESOLVED`。 |
| `MethodsTerminalProjectionV2` | `plan_ref` | `string` | 完整 Evaluation Plan 的固定引用，格式为 `^plan-[0-9a-f]{64}$`。 |
| `MethodsTerminalProjectionV2` | `evidence_graph_ref` | `string` | 服务端生成的 Evidence Graph 固定引用，格式为 `^graph-[0-9a-f]{64}$`。 |
| `MethodsTerminalProjectionV2` | `reason_code` | `MethodsTerminalReasonCodeV2 \| null` | `RESOLVED` 时为 `null`；`UNRESOLVED` 或 `FAILED` 时给出与状态匹配的稳定原因码。 |
| `MethodsTerminalProjectionV2` | `diagnostic_id` | `string` | 稳定诊断 ID，格式为 `^diag-[0-9a-f]{64}$`；用于关联公开终态和执行记录。 |
| `MethodsTerminalProjectionV2` | `diagnostic_evaluation_ref` | `string \| null` | 与本次终态诊断直接关联的 evaluation 引用；没有单一关联项时为 `null`，非空格式为 `^eval-[0-9a-f]{64}$`。 |
| `MethodsTerminalProjectionV2` | `confirmed_evaluation_refs` | `string[]` | 双方一致确认的 evaluation 引用，格式为 `^eval-[0-9a-f]{64}$`；顺序与 `confirmed_method_ids` 一一对应。 |
| `MethodsTerminalProjectionV2` | `confirmed_method_ids` | `string[]` | 双方一致确认的 method ID，格式为 `^[a-z0-9]+(?:-[a-z0-9]+)*$`；顺序与 `confirmed_evaluation_refs` 一一对应。 |
| `MethodsTerminalProjectionV2` | `confirmed_event_refs` | `string[]` | 已确认 evaluation 对应的 Evidence event 引用，格式为 `^event-[0-9a-f]{64}$`。 |
| `MethodsTerminalProjectionV2` | `confirmed_hit_refs` | `string[]` | 已确认 evaluation 对应的 Evidence hit 引用，格式为 `^hit-[0-9a-f]{64}$`。 |
| `MethodsTerminalProjectionV2` | `limitations` | `text[]` | 冻结证据采集阶段已知的限制；任何 Methods V2 终态都应保留并展示。 |
| `MethodsTerminalProjectionV2` | `reasons` | `text[]` | 服务端固定的终态说明。`RESOLVED` 时为空；其他状态包含与 `reason_code` 对应的单条说明。 |
| `CandidateConclusion` | `conclusion_id` | `uuid` | 结论 ID。 |
| `CandidateConclusion` | `revision` | `integer > 0` | 结论版本。 |
| `CandidateConclusion` | `content_hash` | `sha256` | 结论内容散列。 |
| `CandidateConclusion` | `resolution_status` | `DiagnosisResolutionStatus` | 完整或部分解决。 |
| `CandidateConclusion` | `terminal_path_id` | `name` | 命中的终态路径。 |
| `CandidateConclusion` | `statement` | `text` | 总结性结论。 |
| `CandidateConclusion` | `causal_factors` | `CausalFactor[]` | 已确认原因。 |
| `CandidateConclusion` | `candidate_factors` | `CausalFactor[]` | 部分解决时仍待确认的因素。 |
| `CandidateConclusion` | `excluded_factors` | `CausalFactor[]` | 已排除因素。 |
| `CandidateConclusion` | `supporting_evidence_refs` | `uuid[]` | 结论总体证据。 |
| `CandidateConclusion` | `completion_criteria_mapping` | `CompletionCriterionMapping[]`，至少 1 项 | 按创建顺序覆盖所有完成判据。 |
| `CandidateConclusion` | `proposed_by_job_id` | `uuid` | 提出结论的 Job。 |
| `CandidateConclusion` | `status` | `CandidateStatus` | 终态公开结论应为 `ACCEPTED`。 |
| `CausalFactor` | `factor_id` | `name` | 因素稳定 ID。 |
| `CausalFactor` | `role` | `CausalFactorRole` | 原因、贡献因素或条件。 |
| `CausalFactor` | `statement` | `text` | 因素描述。 |
| `CausalFactor` | `evidence_refs` | `uuid[]`，至少 1 项 | 直接证据。 |
| `CausalFactor` | `required_rule_ids` | `text[]`，至少 1 项 | 满足的规则 ID。 |
| `CompletionCriterionMapping` | `criterion_index` | `integer >= 0` | 创建时判据的零基索引。 |
| `CompletionCriterionMapping` | `criterion` | `text` | 判据原文。 |
| `CompletionCriterionMapping` | `status` | `CompletionCriterionStatus` | 满足程度。 |
| `CompletionCriterionMapping` | `evidence_refs` | `uuid[]` | 支撑该状态的证据。 |
| `CompletionCriterionMapping` | `explanation` | `text` | 判定说明。 |
| `UnresolvedResult` | `source_job_id` | `uuid` | 来源 Job。 |
| `UnresolvedResult` | `source_outcome_id` | `uuid` | 来源结果事件。 |
| `UnresolvedResult` | `reason_code` | `UnresolvedReasonCode` | 未解决原因。 |
| `UnresolvedResult` | `summary` | `text` | 安全摘要。 |
| `UnresolvedResult` | `blocking_rule_ids` | `text[]` | 阻塞规则。 |
| `UnresolvedResult` | `evidence_refs` | `uuid[]` | 已有证据。 |
| `UnresolvedResult` | `user_result_artifact_id` | `uuid` | 对外结果 JSON 产物。 |
| `UnresolvedResult` | `recommended_next_step` | `text` | 建议下一步。 |
| `UnresolvedResult` | `occurred_at` | `timestamp` | 产生时间。 |
| `UnresolvedResult` | `audit_artifact_id` | `uuid` | 对外审计包产物。 |
| `GenericResult` | `status` | `GenericResultStatus` | 通用流程终态。 |
| `GenericResult` | `conclusion` | `text` | 结论。 |
| `GenericResult` | `root_cause_analysis` | `text` | 根因分析。 |
| `GenericResult` | `skill_name` | `string` | 执行定义名称，`^[a-z0-9]+(?:-[a-z0-9]+)*$`，最多 64 字符。 |
| `GenericResult` | `source_job_id` | `uuid` | 来源 Job。 |
| `GenericResult` | `source_outcome_id` | `uuid` | 来源结果事件。 |
| `GenericResult` | `occurred_at` | `timestamp` | 产生时间。 |
| `CaseFailure` | `code` | `ErrorCode` | 终态故障码。 |
| `CaseFailure` | `message` | `text` | 安全故障说明。 |
| `CaseFailure` | `source_job_id` | `uuid \| null` | 来源 Job。 |
| `CaseFailure` | `source_outcome_id` | `uuid \| null` | 来源结果事件。 |
| `CaseFailure` | `occurred_at` | `timestamp` | 故障时间。 |
| `CaseFailure` | `reason_code` | `MethodsValidationReasonCode \| MethodsTerminalReasonCodeV2 \| null`，可省略 | 已分类的 Methods 故障原因；Methods V2 只使用 `FAILED` 组的三个原因码。 |
| `CaseFailure` | `diagnostic_id` | `uuid \| ^diag-[0-9a-f]{64}$ \| null`，可省略 | 与 `reason_code` 同时出现或同时缺省。已有 `methods_result` 时两处 ID 相同；评估开始前失败时只在 `CaseFailure` 中出现。 |

已经产生 `methods_result` 的 Methods V2 终态遵循以下固定形态：

- `RESOLVED`：`reason_code=null`、`reasons=[]`，四组 `confirmed_*` 引用均非空；`confirmed_evaluation_refs` 与 `confirmed_method_ids` 等长并按位置对应。
- `UNRESOLVED`：`reason_code` 属于下表的 `UNRESOLVED` 组，`reasons` 为对应的单条服务端说明，四组 `confirmed_*` 引用全部为空。
- `FAILED`：`reason_code` 属于下表的 `FAILED` 组，`reasons` 为对应的单条服务端说明，四组 `confirmed_*` 引用全部为空；外层 `CaseView.failure` 同时存在。

`diagnostic_id` 在三个终态中都存在。前端应原样保存和展示，不要自行重算。`limitations` 与成功或失败无关：只要证据采集记录了限制，`RESOLVED` 也会保留这些内容。角色输出中的自由文本不会进入此投影，前端只读取 `reasons`。

Graph/Plan 生成前的 `FAILED` 不属于 `MethodsTerminalProjectionV2`，因为它没有合法的 `evaluation_id`、`plan_ref` 或 `evidence_graph_ref`。客户端以 `methods_result` 是否存在区分“评估已开始”和“评估尚未开始”，两种失败都读取外层 `CaseFailure` 的固定公共原因与 `diag-*` ID。
冻结资源快照失败使用 `RESOURCE_SNAPSHOT_DRIFT`，Workspace、Logparse 预处理和其他服务端约束失败使用 `SERVER_INVARIANT_VIOLATION`，execution-record 失败使用 `AUDIT_ARCHIVE_FAILED`。

| `MethodsTerminalReasonCodeV2` | 对应状态 | 含义 |
| --- | --- | --- |
| `SPECIALIST_PROTOCOL_REPAIR_EXHAUSTED` | `UNRESOLVED` | Specialist 首次输出不符合结构或覆盖要求，唯一一次修复后仍不合格。 |
| `REVIEWER_PROTOCOL_REPAIR_EXHAUSTED` | `UNRESOLVED` | Reviewer 首次输出不符合结构或覆盖要求，唯一一次修复后仍不合格。 |
| `SPECIALIST_SEMANTIC_INVALID` | `UNRESOLVED` | Specialist 的评估不满足 Evaluation 合同。 |
| `REVIEWER_SEMANTIC_INVALID` | `UNRESOLVED` | Reviewer 的评估不满足 Evaluation 合同。 |
| `SPECIALIST_MODEL_EXECUTION_FAILED` | `UNRESOLVED` | Specialist 评估未完成。 |
| `REVIEWER_MODEL_EXECUTION_FAILED` | `UNRESOLVED` | Reviewer 评估未完成。 |
| `SPECIALIST_REVIEWER_DISAGREEMENT` | `UNRESOLVED` | Specialist 与 Reviewer 对至少一项 evaluation 的结论不一致。 |
| `INCOMPLETE_EVALUATION` | `UNRESOLVED` | 至少一项 evaluation 的结论仍为 `UNKNOWN`。 |
| `NO_CONFIRMED_METHOD` | `UNRESOLVED` | 没有 method 获得双方一致的 `CONFIRMED` 结论。 |
| `NO_MATCHING_METHOD_EVIDENCE` | `UNRESOLVED` | 已加载 method 中没有匹配到证据的项。 |
| `RESOURCE_SNAPSHOT_DRIFT` | `FAILED` | 评估完成前，冻结的资源快照发生变化。 |
| `SERVER_INVARIANT_VIOLATION` | `FAILED` | 服务端未能保持 Evidence V2 的机械不变量。 |
| `AUDIT_ARCHIVE_FAILED` | `FAILED` | 评估审计归档未完成。 |

### 8.7 附件描述与产物

| 模型 | 字段 | 类型 | 含义 |
| --- | --- | --- | --- |
| `WebUploadDescriptor` | `attachment_id` | `uuid` | 已准备附件。 |
| `WebUploadDescriptor` | `method` | literal `"PUT"` | 必须使用的方法。 |
| `WebUploadDescriptor` | `url` | `string` | 完整上传 URL，原样使用。 |
| `WebUploadDescriptor` | `required_headers` | `WebUploadRequiredHeaders` | 浏览器脚本必须原样设置的三个头。 |
| `WebUploadDescriptor` | `expected_content_length` | `integer >= 0` | Chrome 应自动产生的字节数。 |
| `WebUploadDescriptor` | `max_bytes` | literal `2684354560` | 单附件上限。 |
| `WebUploadDescriptor` | `expires_at` | literal `null` | 当前没有服务端过期时间；不能据此假设临时内容永久保留。 |
| `WebUploadRequiredHeaders` | `Idempotency-Key` | `uuid` | 等于 `attachment_id`。 |
| `WebUploadRequiredHeaders` | `Content-Type` | `content_type` | 已准备类型。 |
| `WebUploadRequiredHeaders` | `X-Content-SHA256` | `sha256` | 已声明散列。 |
| `ArtifactSummary` | `artifact_id` | `uuid` | 产物 ID。 |
| `ArtifactSummary` | `kind` | `ArtifactKind` | 产物种类。 |
| `ArtifactSummary` | `name` | `text` | 文件名。 |
| `ArtifactSummary` | `content_type` | `content_type` | 媒体类型。 |
| `ArtifactSummary` | `resource_kind` | `ResourceKind` | 文件或目录。Case 视图只公开可下载项。 |
| `ArtifactSummary` | `size` | `integer >= 0` | 字节数。 |
| `ArtifactSummary` | `sha256` | `sha256` | 内容散列。 |
| `ArtifactSummary` | `created_by_job_id` | `uuid` | 生成它的 Job。 |
| `ArtifactSummary` | `created_at` | `timestamp` | 生成时间。 |
| `ArtifactSummary` | `downloadable` | `boolean` | Case 视图中应为 `true`。 |
| `ArtifactView` | `artifact_id` | `uuid` | 产物 ID。 |
| `ArtifactView` | `kind` | `ArtifactKind` | 产物种类。 |
| `ArtifactView` | `name` | `text` | 下载文件名。 |
| `ArtifactView` | `content_type` | `content_type` | 媒体类型。 |
| `ArtifactView` | `size` | `integer >= 0` | 权威字节数。 |
| `ArtifactView` | `sha256` | `sha256` | 权威散列。 |
| `ArtifactView` | `created_at` | `timestamp` | 生成时间。 |
| `ArtifactView` | `download_url` | `string` | 含 `case_id` query 的完整下载 URL，原样使用。 |

### 8.8 错误模型

| 模型 | 字段 | 类型 | 含义 |
| --- | --- | --- | --- |
| `ApplicationError` | `code` | `ErrorCode` | 稳定机器分支码。 |
| `ApplicationError` | `message` | `text` | 面向人的安全消息，不应用来分支。 |
| `ApplicationError` | `details` | `ApplicationErrorDetail[]` | 字段/资源级诊断，可为空。 |
| `ApplicationError` | `retryable` | `boolean` | 此错误码是否允许按本文规则重试。 |
| `ApplicationErrorDetail` | `field` | `string \| null` | 错误字段路径，例如 `body.problem_spec.statement`。 |
| `ApplicationErrorDetail` | `resource_type` | `string \| null` | 资源类别。 |
| `ApplicationErrorDetail` | `resource_id` | `uuid \| null` | 资源 ID。 |
| `ApplicationErrorDetail` | `resource_ref` | `VersionedRef \| null` | 版本化资源引用。 |
| `ApplicationErrorDetail` | `expected` | `string \| integer \| boolean \| null` | 期望值/规则。 |
| `ApplicationErrorDetail` | `actual` | `string \| integer \| boolean \| null` | 实际值。 |
| `ApplicationErrorDetail` | `limit` | `integer >= 0 \| null` | 数量或字节限制。 |
| `ApplicationErrorDetail` | `observed` | `integer >= 0 \| null` | 实际数量或字节数。 |

## 9. 枚举与错误处理

### 9.1 全部响应枚举

| 枚举 | wire 值 | 前端解释 |
| --- | --- | --- |
| `CaseStatus` | `NEW`, `RUNNING`, `WAITING_INPUT`, `WAITING_ATTACHMENT`, `REVIEWING`, `RESOLVED`, `PARTIALLY_RESOLVED`, `UNRESOLVED`, `FAILED`, `CANCELLED`, `INTERRUPTED` | 见状态—动作表。 |
| `JobStatus` | `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`, `INTERRUPTED` | Job 生命周期；Case 动作仍以 `CaseStatus` 为准。 |
| `JobType` | `ROUTE`, `DIAGNOSE`, `REVIEW` | 路由、诊断、审核阶段。 |
| `DiagnosisMode` | `SPECIALIZED`, `GENERIC` | 专用或通用诊断模式。 |
| `DiagnosisItemStatus` | `ACTIVE`, `RESOLVED`, `REJECTED`, `SUPERSEDED` | 诊断项状态。 |
| `DiagnosisProvenanceType` | `USER_INPUT`, `AGENT_OUTCOME` | 用户输入或后台结果。 |
| `RequirementKind` | `INPUT`, `ATTACHMENT` | 字符串输入或文件。 |
| `RequirementStatus` | `OPEN`, `FULFILLED` | 待满足或已满足。 |
| `SupplementPolicy` | `NONE`, `MISSING_ONLY` | 不接受补充，或只接受缺失值。 |
| `CandidateStatus` | `PROPOSED`, `REVIEWING`, `REJECTED`, `ACCEPTED` | 候选结论生命周期。 |
| `DiagnosisResolutionStatus` | `COMPLETE`, `PARTIAL` | 完整或部分解决。 |
| `CompletionCriterionStatus` | `SATISFIED`, `PARTIALLY_SATISFIED`, `UNSATISFIED`, `UNKNOWN` | 每条完成判据的结果。 |
| `CausalFactorRole` | `CAUSE`, `CONTRIBUTOR`, `CONDITION` | 直接原因、贡献因素、必要条件。 |
| `GenericResultStatus` | `RESOLVED`, `UNRESOLVED` | 通用结果终态。 |
| `UnresolvedReasonCode` | `MECHANICAL_VERIFICATION_FAILED`, `INSUFFICIENT_EVIDENCE`, `SEMANTIC_REVIEW_REJECTED`, `INVALID_NEED_MORE_REQUEST` | 机械校验失败、证据不足、语义审核拒绝、追加请求非法。 |
| `ArtifactKind` | `USER_RESULT`, `USER_RESULT_ARCHIVE`, `DIAGNOSTIC_EXPORT`, `LOGPARSE_RUN`, `AUDIT_BUNDLE`, `GENERIC_REPORT` | 结果 JSON、结果归档、诊断导出、内部目录、审计包与 Generic V2 Markdown；列表端点只返回公开可下载项。 |
| `ResourceKind` | `FILE`, `DIRECTORY` | 文件或目录；下载端点只开放文件。 |

### 9.2 全部 `ErrorCode`

| HTTP | `ErrorCode` | `retryable` | 前端动作 |
| --- | --- | --- | --- |
| `400` | `VALIDATION_ERROR` | `false` | 读取 `details`，修正请求；不要原样重试。 |
| `400` | `PATH_VIOLATION` | `false` | 停止并修正非法路径/名称。 |
| `404` | `CASE_NOT_FOUND` | `false` | 检查已保存的 `case_id` 和环境。 |
| `404` | `JOB_NOT_FOUND` | `false` | 重新查询 Case，不再等待未知 Job。 |
| `404` | `ATTACHMENT_NOT_FOUND` | `false` | 重新检查 prepare 回执，不自行构造 ID。 |
| `404` | `ARTIFACT_NOT_FOUND` | `false` | 重新列出产物，停止使用旧 URL。 |
| `409` | `JOB_CASE_MISMATCH` | `false` | Job 不属于该 Case；修正客户端关联。 |
| `409` | `INVALID_CASE_STATE` | `false` | 重新查询并按最新状态动作。 |
| `409` | `ACTIVE_JOB_EXISTS` | `false` | 等待当前 Job，不能并发推进。 |
| `409` | `NEW_CASE_REQUIRED` | `false` | 当前事实不可在原 Case 中替换；让用户显式创建新 Case。 |
| `409` | `REVISION_CONFLICT` | `true` | 重新查询、重新确认内容、复用原 `request_id` 和新 revision。 |
| `409` | `IDEMPOTENCY_CONFLICT` | `false` | 停止；同一 ID 被用于不同业务内容。 |
| `409` | `RESOURCE_CASE_MISMATCH` | `false` | 资源不属于该 Case；修正关联。 |
| `409` | `ATTACHMENT_NOT_READY` | `true` | 使用已保存的 `upload` descriptor 和同一个 `File`/`Blob` 幂等重放 PUT；取得 `READY` 后，以原 supplement `request_id` 重试。没有附件状态查询操作。 |
| `409` | `UPLOAD_INCOMPLETE` | `true` | 重新发送同一附件的完整原始字节。 |
| `409` | `BACKEND_CANCELLED` | `false` | 展示后台取消；按最新 Case 状态决定。 |
| `409` | `CLAIM_REJECTED` | `false` | 后台认领冲突；查询 Case，不在前端重复写。 |
| `413` | `RESOURCE_LIMIT_EXCEEDED` | `false` | 阻止上传/提交并展示限制。 |
| `422` | `RESOURCE_HASH_MISMATCH` | `false` | 丢弃结果，重新计算原始字节散列。 |
| `422` | `RESOURCE_SIZE_MISMATCH` | `false` | 丢弃结果，重新读取实际字节数。 |
| `422` | `CONTEXT_LIMIT` | `false` | 展示输入/上下文超限。 |
| `422` | `ASSET_VERSION_UNAVAILABLE` | `false` | 服务端版本资源不可用，交给运维。 |
| `422` | `OUTCOME_MISSING` | `false` | 后台输出缺失，展示失败信息。 |
| `422` | `OUTCOME_INVALID` | `false` | 后台输出非法，展示失败信息。 |
| `422` | `BACKEND_OUTPUT_LIMIT` | `false` | 后台输出超限。 |
| `422` | `WORKSPACE_LIMIT` | `false` | 后台工作区超限。 |
| `422` | `LOGPARSE_FAILED` | `false` | 证据解析失败。 |
| `422` | `LOGPARSE_OUTPUT_INVALID` | `false` | 证据解析输出非法。 |
| `422` | `NO_CAPABILITY` | `false` | 当前输入没有可用处理能力；展示并停止。 |
| `500` | `RESOURCE_NOT_FOUND` | `false` | 已登记资源丢失；交给运维。 |
| `500` | `BACKEND_START_FAILED` | `false` | 后台启动失败；以 Case 终态/错误为准。 |
| `500` | `WORKSPACE_PREPARE_FAILED` | `false` | 后台工作区准备失败。 |
| `500` | `RESOURCE_STAGE_FAILED` | `false` | 资源暂存失败。 |
| `500` | `EXECUTION_RECORD_FAILED` | `false` | 执行记录失败。 |
| `500` | `STATE_WRITE_FAILED` | `true` | 保留相同业务请求和 ID，退避重试并检查 `/ready`。 |
| `500` | `RESOURCE_PUBLISH_FAILED` | `true` | 保留相同业务请求和 ID，退避重试并检查 `/ready`。 |
| `500` | `CONFIG_INVALID` | `false` | 服务配置错误，交给运维。 |
| `502` | `BACKEND_EXIT_FAILED` | `false` | 后台异常退出。 |
| `503` | `DISPATCH_REJECTED` | `true` | 退避并查询；不要创建重复 Case。 |
| `503` | `INSTANCE_LOCKED` | `true` | 服务实例锁冲突，检查 `/ready` 后退避。 |
| `503` | `STATE_CORRUPT` | `false` | 停止写入并交给运维。 |
| `503` | `STATE_SCHEMA_UNSUPPORTED` | `false` | 客户端/服务部署不匹配，交给运维。 |
| `504` | `BACKEND_TIMEOUT` | `false` | 后台超时；查询最终 Case 状态，不重放新业务写。 |

## 10. 能力边界与交付验收

前端上线前至少自动验证：

- 从 `/openapi.json` 生成或校验客户端类型，构建时拒绝合同漂移。
- 创建后刷新页面仍能从本地持久化恢复 `case_id`；不能依赖服务端 Case 列表。
- 覆盖 `case_view=null`、`wait_timed_out=true`、`dispatch_pending=true`、Job ID 切换、`REVISION_CONFLICT` 和 `IDEMPOTENCY_CONFLICT`。
- 覆盖全部 Case 状态，特别是 `PARTIALLY_RESOLVED`、`UNRESOLVED`、`FAILED` 与 `INTERRUPTED`。
- 在当前稳定版 Chrome 做真实跨源预检、`File`/`Blob` PUT、大小/散列失败、成功上传、采用、多附件 revision 推进和逐字节下载校验。
- 确认浏览器脚本没有设置 `Content-Length`，也没有发送 Cookie 或其他凭据。

本文未提供的能力——Case 历史列表、恢复、取消、认证、用户/租户权限——不能由前端通过猜测 URL、直接读存储或重用其他端点模拟。需要这些能力时必须先新增并发布明确的 REST 合同。
