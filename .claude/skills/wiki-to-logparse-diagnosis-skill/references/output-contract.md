# Problem Locator registration 生成合同

## 目录

输出目录名是 `registration_id`，并使用小写 kebab-case。生成目录只能包含：

```text
<registration-id>/
|-- registration-template.json
`-- package/
    `-- <diagnose-skill>/
        |-- SKILL.md
        |-- methods.json
        `-- references/
            |-- source-log-templates.md
            |-- <method-id>.md
            `-- <shared-topic>.md
```

`package/` 只能有一个真实目录。目标 Skill 名、该目录名、`SKILL.md` frontmatter 的 `name`、
`methods.json.skill_name` 和 registration 的 `package.skill_name` 必须相同，并以 `diagnose-` 开头。

不要生成旧版 manifest、GenerationSpec、README、复制的 Wiki、脚本、打包器或测试文件。不要创建
链接。整个 `package/` 内的 `.md` 和 `.json` 文本都不得出现以下本地运行标识，即使上下文是否定句：

- 裸 `logparse-diagnose`，无论是否包在工具调用中；
- 任意 `Skill(` 工具调用；
- `load Helper`、`调用 Helper` 等 Helper 调用措辞；
- `invoke broker preprocessing`、`调用 broker 预处理` 等动作型 broker 预处理措辞；
- `problem-locator-logparse`；
- `result.zip`；
- `pack_result_zip`；
- `logparse.json`；
- `cli.py`。

## registration-template.json

使用以下闭合结构，不增加字段：

```json
{
  "schema_version": 1,
  "registration_id": "<输出目录名>",
  "version": "1.0.0",
  "capability": "<忠实概括 Wiki 定位能力的非空单行文本>",
  "deployment_scope": "PRODUCTION",
  "summary": "<忠实概括用途和证据边界的非空文本>",
  "package": {
    "relative_path": "package/<diagnose-skill>",
    "skill_name": "<diagnose-skill>",
    "source_wiki_sha256": "<64 位小写十六进制>"
  },
  "runtime": {
    "diagnose": {
      "agent_profile_id": "agent-profile/specialist",
      "tool_bundle_id": "tool-bundle/diagnose",
      "context_policy_id": "context-policy/diagnose",
      "output_contract_id": "output-contract/diagnose"
    },
    "review": {
      "agent_profile_id": "agent-profile/reviewer",
      "tool_bundle_id": "tool-bundle/review",
      "context_policy_id": "context-policy/review",
      "output_contract_id": "output-contract/review"
    },
    "preprocessing": {
      "requires_logparse": true,
      "logparse_product": "default",
      "roles": [
        {
          "label": "client",
          "description": "<client 日志的简短说明>",
          "presence": "REQUIRED",
          "source_reference": "<Wiki 中 client 输入或日志用途的简短出处说明>"
        },
        {
          "label": "server",
          "description": "<server 日志的简短说明>",
          "presence": "REQUIRED",
          "source_reference": "<Wiki 中 server 输入或日志用途的简短出处说明>"
        }
      ],
      "logparse_plan": {
        "attachment_requirement": "log_archive",
        "problem_time_binding": {"source": "USER_FACT", "name": "problem_time"},
        "anchors": [
          {
            "label": "client",
            "module": {"source": "SKILL_FIXED", "value": "<用户确认的 module>"},
            "slot": {"source": "USER_FACT", "name": "client_slot"},
            "process_name": {"source": "USER_FACT", "name": "client_process_name"},
            "pid": {"source": "USER_FACT", "name": "client_pid"}
          },
          {
            "label": "server",
            "module": {"source": "SKILL_FIXED", "value": "<同一个 module>"},
            "slot": {"source": "USER_FACT", "name": "server_slot"},
            "process_name": {"source": "USER_FACT", "name": "server_process_name"},
            "pid": {"source": "USER_FACT", "name": "server_pid"}
          }
        ]
      }
    }
  }
}
```

- `version` 固定为 `1.0.0`，`deployment_scope` 固定为 `PRODUCTION`。
- `logparse_product` 是 Server 内部字段，固定为 `default`，不能要求用户提供，也不能根据 Wiki 改写。
- diagnose/review 四元绑定逐项固定，不能改成生成 Agent 自选的 profile、tool、context 或 output。
- client 与 server 共用用户确认的同一个 module，两个绑定都使用 `SKILL_FIXED`。
- slot 和 process name 必须使用对应 `USER_FACT`，不能固定、互换、从日志派生或映射到别名。
- PID 始终保留对应 `USER_FACT` 绑定，但它是可选事实；用户没有提供时 Server 不主动索取。
- 两个 role 都是 `REQUIRED`，顺序与两个 anchor 一致。description 和 source_reference 必须非空、
  忠实来自 Wiki 或固定输入含义，不能加入新的业务判断。
- `package.source_wiki_sha256` 必须与 `methods.json.source_wiki_sha256` 及原始 Wiki 字节一致。

## 业务 SKILL.md

frontmatter 只包含 `name` 和 `description`。入口保持简短，并明确：

1. 读取 Server 写入的 `request.json`、`methods.json` 和 `target_logs.json`。
2. 只读取 `target_logs[*].log_path` 明确列出的冻结日志，不遍历目录、不猜路径、不重新选择日志。
3. 先扫描所有方法的正向 marker，再只加载相关方法卡和共享引用；不能在第一个命中处停止。
4. 检查输入范围内全部相关调用。只有证据足以证明属于同一次调用时才合并发现。
5. 每个原因、每次独立事件分别输出证据。每条证据用 `sources` 保存完整冻结日志原文，并用
   `identity_tokens` 保存同一来源中的事件身份字面量。
6. 保留证据不足、观测限制和 Wiki 的安全提醒。

业务 `SKILL.md` 必须逐字包含以下两句，不改写，也不要补入具体命令名或文件名：

```text
Logparse 预处理、目标日志冻结、Review 和最终 Artifact 发布由 Server 完成；诊断阶段不重新执行这些操作。
`client_pid` 和 `server_pid` 是可选事实；缺失时不请求补充，也不构成证据缺口。
```

方法卡和共享引用也不能加入本地预处理、内部辅助组件或打包步骤。业务入口固定使用上面的两句话，
不要补写内部 Skill、Helper 或 broker 的名称。整个业务 package 都只描述冻结证据的分析方法。

## methods.json

使用以下闭合结构，不增加字段：

```json
{
  "schema_version": 1,
  "skill_name": "<diagnose-skill>",
  "source_wiki_sha256": "<64 位小写十六进制>",
  "required_user_inputs": [
    "problem_time",
    "client_slot",
    "client_process_name",
    "server_slot",
    "server_process_name",
    "client_pid",
    "server_pid",
    "<Wiki 的其他用户参数>"
  ],
  "required_artifacts": ["log_archive"],
  "log_derived_fields": ["<只能从日志读取的字段>"],
  "shared_references": [
    "references/source-log-templates.md",
    "references/<shared-topic>.md"
  ],
  "methods": [
    {
      "id": "<stable-kebab-case-id>",
      "title": "<简短标题>",
      "reference": "references/<method-id>.md",
      "priority": 1,
      "evidence_markers": ["<Wiki 模板中的稳定字面子串>"]
    }
  ]
}
```

`required_user_inputs` 的前七项必须逐项、逐序等于上例。前五项是 Case 运行前必须齐全的事实；
`client_pid` 和 `server_pid` 只用于可选 PID 绑定，不属于缺失时需要追问的 mandatory facts。Wiki 的
其他用户参数从第八项开始按源顺序追加。

Wiki 的同义输入按以下规则合并：

| Wiki 含义 | 固定 ID |
| --- | --- |
| 问题、故障或超时发生时间 | `problem_time` |
| client slot 或客户端槽位 | `client_slot` |
| client 进程信息或进程名 | `client_process_name` |
| server slot 或服务端槽位 | `server_slot` |
| server 进程信息或进程名 | `server_process_name` |
| client PID | `client_pid` |
| server PID | `server_pid` |
| 服务名 | `service` |
| API 名 | `api` |

不得生成 `client_process`、`server_process`、`slot`、`service_name`、`api_name` 等别名。其他用户
参数使用简短的小写 snake_case。

`required_artifacts` 必须精确等于 `["log_archive"]`。当前 Server 不支持业务 package 声明第二种
附件；Wiki 明确要求其他附件时停止并报告 unsupported，不得生成 registration。
`log_derived_fields` 按机械日志模板中 `{named_field}` 的首次出现顺序收集，再删除已经属于
`required_user_inputs` 的字段。三组 ID 必须各自唯一且彼此不重复。

- `shared_references[0]` 固定为 `references/source-log-templates.md`。
- 方法 ID、引用和 priority 唯一；priority 按数组顺序从 1 连续递增。
- `required_user_inputs`、`required_artifacts`、`log_derived_fields` 各自最多 200 项；methods 最多
  100 项；每个方法最多 100 个 marker。marker 必须非空、不得换行，UTF-8 编码后不得超过
  1024 字节。
- Wiki 明确列出原因时，每个原因对应一个方法。同一原因的不同观测阶段合并在同一张方法卡中。
- 每种可独立确认的方法至少有一个 canonical stable marker。模板在第一个 `{field}` 或 `%x`
  占位符前存在非空字面前缀时，marker 是该完整前缀去除首尾空白后的精确字节；模板以占位符开头
  时，只在相邻占位符之间选择最长的非空字面片段，长度相同取最早者；最后一个占位符之后的
  suffix 不是候选。保持大小写，不得截短、改选其他片段、保留占位符或使用整条模板。模板只有
  一个开头占位符、后面再无占位符时，没有 canonical marker。
- 事件名或日志类型缩写不等于 canonical marker。例如模板
  `API_COMPLETE service={service} api={api}` 的 marker 必须精确为 `API_COMPLETE service=`；
  `API_COMPLETE`、`API_COMPLETE service` 和整条模板都无效。写 `methods.json` 前先机械算出合法
  marker 清单，`evidence_markers` 中的每一项都必须逐字取自该清单。
- 同一正向模板能够独立确认多个原因时，把 marker 列入每个适用方法。共同症状不必复制到每个
  方法，但完整模板仍必须保存在固定共享引用中。
- 不能用日志缺失排除受抑制、限流或采样影响的原因。

## source identity v2 与固定模板清单

source identity 使用以下闭合结构：

```json
{
  "algorithm": "sha256",
  "log_template_extraction_version": 2,
  "log_template_inventory_sha256": "<64 位小写十六进制>",
  "log_templates": ["<完整模板行>"],
  "schema_version": 2,
  "sha256": "<原始 Wiki 字节的 SHA-256>",
  "source_path": "<Wiki 路径>"
}
```

提取版本 2 的机械规则：

- fence 起始行去除首尾空白后，必须精确等于三个反引号加 `text`，或仅三个反引号；其他语言
  fence 不进入清单；
- 仅三个反引号的行在 fence 外表示开始，在已进入 fence 时表示结束；
- fence 中每个非空行只去除首尾空白；
- 只收集包含 `{named_field}` 或 `%x` 形式占位符的完整行；
- 按首次遇到的顺序保留每次出现，不去重。

`references/source-log-templates.md` 始终存在，UTF-8、LF、带终止换行。它必须严格按下列格式生成；
`<template lines>` 逐项、逐序、逐字来自经过核对的 identity 清单：

````text
# Source log templates

```text
<template lines>
```
````

若没有机械模板，代码块内容为空，但文件仍保留。模板用途、字段关联、阈值和观测边界仍只来自 Wiki。

## 方法卡与共享引用

每张方法卡必须包含以下标题：

```markdown
# <方法标题>

## 适用条件
## 所需证据
## 计算与判断
## 确认条件
## 未知边界
## 输出含义
```

保留 Wiki 的字段关联、单位换算、阈值、分组、目标选择和多贡献者规则。确认条件只能建立在正向
证据上；日志缺失策略放在“未知边界”。如果某条日志只在阈值或条件已经满足时打印，观测到该日志
本身就是确认条件。

“输出含义”必须说明：同一方法命中多个独立事件时分别输出；每条输出保留完整 `sources` 和来自
这些来源的 `identity_tokens`。不同来源没有可靠共同身份时不得强行合并。

其他共享引用只放多个方法共同遵守的 Wiki 内容，例如输入含义、证据作用域、共同症状、观测限制
和安全提醒。不要增加 Wiki 没有提供的阈值或经验结论。

## validator 执行责任

文件生成结束后，调用方必须运行本元 Skill 的 validator。调用环境明确授权执行命令时，生成 Agent
可以代为运行；restricted Test Flow 只给生成 Agent 文件读写能力时，Agent 不得尝试 Bash 或其他
命令，runner 必须在生成结束后执行 validator 并以结果决定是否接收产物。
