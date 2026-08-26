# 局域网 Logparse 定位 Skill 生成合同

## 目录

生成目录只能包含：

```text
<diagnose-skill>/
|-- SKILL.md
|-- methods.json
|-- logparse.json
|-- references/
|   |-- source-log-templates.md
|   |-- <method-id>.md
|   `-- <shared-topic>.md
`-- scripts/
    `-- pack_result_zip.py
```

不要生成 `registration-template.json`、GenerationSpec、`diagnosis-skill.json`、README、复制的
Wiki 或测试文件。该产物用于局域网 Claude Code 直用，不是当前 Problem Locator Server 的
Methods registration。

## SKILL.md

frontmatter 只包含 `name` 和 `description`。`name` 必须与目录名和
`methods.json.skill_name` 一致，并以 `diagnose-` 开头。

正文必须明确以下运行顺序和边界：

1. 读取 `methods.json` 与 `logparse.json`。
2. 在任何 Helper 调用前检查全部 `required_user_inputs` 和 `required_artifacts`。缺少
   `client_slot` 或 `server_slot` 时直接请求补充，不得加载 Helper、创建 Logparse 请求或生成 ZIP。
3. 用 `logparse.json` 的固定 module 和有序 roles 组装 client/server targets；slot 与
   process name 来自对应用户输入，PID 只在用户提供时加入。
4. 使用 Skill 工具恰好加载一次 `Skill(logparse-diagnose)`，随后遵守现装 Helper 返回的合同。
   读取 Helper 文件、复制旧命令、直接调用 `problem-locator-logparse`、`cli.py` 或自行解析归档，
   都不能替代这次 Skill 调用。
5. Helper 失败，或任一必需 target 为 missing、ambiguous、没有安全 `log_path` 时停止并报告证据
   缺口；不得遍历输出目录、重选生命周期、拼接路径或用邻近日志替代。
6. 只读取 `target_logs[*].log_path`，先扫描全部方法的正向 marker，再按需读取方法卡；不能在
   第一个命中处短路。
7. 每个原因、每次独立事件分别记录证据。每条证据保留完整日志原文、来源路径和能区分事件的
   `identity_tokens`；无法可靠关联的来源不得合并。
8. 写出完整 `result.txt`，只把它和实际读取的目标日志放入一个全新的扁平交付目录，再仅调用一次
   `scripts/pack_result_zip.py` 生成 `result.zip`。不得手工写 ZIP、加入目录或未读取日志。
9. 最终回复直接给出定位结论、关键证据、证据缺口、实际使用日志和 `result.zip` 路径。用户无需
   下载 ZIP 才能看到结论；回复不得与 `result.txt` 冲突。

生成的入口不得包含旧版 Logparse CLI 参数或直接执行命令；具体请求与 broker 行为只由运行时加载
的 `logparse-diagnose` 决定。

## methods.json

结构与当前 Methods package 保持一致：

```json
{
  "schema_version": 1,
  "skill_name": "diagnose-example",
  "source_wiki_sha256": "<64 lowercase hex>",
  "required_user_inputs": [
    "problem_time",
    "client_slot",
    "client_process_name",
    "server_slot",
    "server_process_name"
  ],
  "required_artifacts": ["log_archive"],
  "log_derived_fields": [],
  "shared_references": ["references/source-log-templates.md"],
  "methods": [
    {
      "id": "example-cause",
      "title": "示例原因",
      "reference": "references/example-cause.md",
      "priority": 1,
      "evidence_markers": ["EXAMPLE marker="]
    }
  ]
}
```

`required_user_inputs` 的前五项必须逐项、逐序等于上例。Wiki 的同义输入按以下规则合并：

| Wiki 含义 | 固定 ID |
| --- | --- |
| 问题、故障或超时发生时间 | `problem_time` |
| client slot 或客户端槽位 | `client_slot` |
| client 进程信息或进程名 | `client_process_name` |
| server slot 或服务端槽位 | `server_slot` |
| server 进程信息或进程名 | `server_process_name` |
| 服务名 | `service` |
| API 名 | `api` |

其他 Wiki 明确要求的标量参数按源顺序追加，使用小写 `snake_case`。不得再生成
`client_process`、`server_process`、`slot`、`service_name`、`api_name` 等别名。PID 不属于
必填数组。

`required_artifacts[0]` 固定为 `log_archive`；Wiki 明确要求的其他附件可按源顺序追加。
`log_derived_fields` 按 Wiki `text` 日志模板中命名字段首次出现顺序收集，再删除已经属于
`required_user_inputs` 的字段。三组 ID 必须各自唯一且彼此不重复。

`source_wiki_sha256`、`shared_references`、方法拆分、priority、canonical evidence marker、
方法卡标题与固定 `references/source-log-templates.md` 的规则，和当前 Methods package 合同一致：

- `shared_references[0]` 固定为 `references/source-log-templates.md`；
- 方法卡包含 `适用条件`、`所需证据`、`计算与判断`、`确认条件`、`未知边界`、`输出含义`；
- 每种可独立确认的方法至少有一个来自 Wiki 日志模板的 canonical stable marker。marker 必须按下列
  机械算法生成：模板在第一个 `{field}` 或 `%x` 占位符前有非空字面前缀时，使用该完整前缀并且
  只去除首尾空白；模板以占位符开头时，使用各占位符之间最长的非空字面片段，长度相同取最早
  出现者，同样只去除首尾空白。保持大小写，不得截短、改选其他片段、保留占位符或使用整条模板；
- 同一正向模板的字段与 Wiki 计算能够独立确认多个原因时，它的 marker 必须出现在每个适用方法
  中；不能为了让 marker 唯一而只保留在其中一张方法卡。只有不能区分具体原因的共同症状 marker
  才可以只进入共享引用；
- 不能命中第一种原因后停止，也不能用日志缺失排除受抑制、限流或采样影响的原因。

## logparse.json

使用以下闭合结构，不增加字段：

```json
{
  "schema_version": 1,
  "helper_skill": "logparse-diagnose",
  "module": "<用户在生成时确认的固定 module>",
  "problem_time_input": "problem_time",
  "artifact_input": "log_archive",
  "roles": [
    {
      "label": "client",
      "required": true,
      "slot_input": "client_slot",
      "process_name_input": "client_process_name",
      "pid_input": "client_pid"
    },
    {
      "label": "server",
      "required": true,
      "slot_input": "server_slot",
      "process_name_input": "server_process_name",
      "pid_input": "server_pid"
    }
  ]
}
```

module 为 1–128 字节非空单行 ASCII，client 和 server 共用同一个值。roles 的字段、顺序和映射
固定；不得把 slot 改成常量、日志派生字段或可选输入。

## 固定源日志模板

`references/source-log-templates.md` 始终存在，UTF-8、LF、带终止换行，格式固定：

````text
# Source log templates

```text
<source identity v2 log_templates，逐项逐序逐字，一项一行>
```
````

机械提取只收集 `text` fence 中包含 `{named_field}` 或 `%x` 占位符的非空整行，行首尾去空白，
保留顺序和重复项。模板用途和业务含义仍只来自 Wiki。

## 打包器与 result.zip

生成物的 `scripts/pack_result_zip.py` 必须与本元 Skill 的
`assets/pack_result_zip.py` 字节完全一致。打包器只接受一个新的扁平交付目录和一个尚不存在的
输出路径；要求非空 `result.txt` 和至少一个 `.log` 文件，拒绝其他扩展名、目录、链接、非普通
文件、重复项、`manifest.txt` 和嵌套路径。

ZIP 条目顺序固定为 `result.txt` 后跟按名称排序的实际使用日志。日志副本采用安全扁平名称，并在
生成的 `SKILL.md` 中逐字保留格式
`<label>__<module>__slot_<slot>__<process_name>[__pid_<pid>].log`；已知 PID 时追加 PID，组件需要
安全编码，不能引入路径分隔符。ZIP 不得包含原始日志归档、未读取日志、Helper 请求、缓存或内部
审计文件。
