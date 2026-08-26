---
name: wiki-to-logparse-diagnosis-skill
description: Convert an authored troubleshooting Wiki into one production Problem Locator registration with a closed, evidence-driven Methods Skill package. Do not use it to diagnose an incident directly.
---

# Wiki 转 Problem Locator registration

把一份已经评审的定位 Wiki 转成可部署到 Linux Problem Locator Server `SKILL_DIR` 的完整
registration root。生成物只包含产品 registration 和闭合 Methods package；客户端仍使用
`problem-locator-client` 经 HTTP MCP 提交 Case，生成的业务 Skill 不负责 Logparse 预处理或结果打包。

## 开始前

必须取得以下输入：

- 原始 Wiki；
- 以 `diagnose-` 开头的目标 Skill 名称；
- registration 输出目录；该目录名就是 `registration_id`；
- client 与 server 共用的固定 Logparse `module`。

`module` 必须由用户明确提供，且为 1–128 字节的非空单行 ASCII。缺失或不合法时停止并询问，
不得从 Wiki、日志模板、示例或历史产物猜测。

完整阅读 Wiki。调用方提供 source identity v2 时，先核对其绑定的 Wiki SHA-256、提取版本和
`log_templates`，再逐字使用其中的 digest 与模板清单；未提供时只能用确定性哈希工具从原始字节
计算。不要修改或规范化 Wiki 字节。

生成前完整阅读 [输出合同](references/output-contract.md)。

## 转换原则

1. 生成目录根层只放 `registration-template.json` 和 `package/`；`package/` 内只有一个目标
   Methods Skill。
2. `required_user_inputs` 无条件先放
   `problem_time, client_slot, client_process_name, server_slot, server_process_name`，再放可选绑定
   `client_pid, server_pid`，随后按 Wiki 源顺序追加其他用户参数。PID 可缺省，不得因列入索引而
   把它变成本次 Case 的必填补充项。
3. `log_archive` 是唯一受支持的附件。Wiki 明确要求其他附件时停止并报告当前 Server 不支持，
   不得生成无法运行的 registration。Wiki 的其他标量参数按源顺序保留；同义的时间、slot、
   进程、服务或 API 信息映射到合同中的稳定名称，不生成别名。
4. registration 固定为 `version=1.0.0`、`deployment_scope=PRODUCTION`，内部
   `logparse_product=default`。client/server 的 `module` 使用相同 `SKILL_FIXED` 值；slot、
   process name 和可选 PID 使用各自的 `USER_FACT` 绑定。diagnose/review 使用产品固定绑定。
5. 按 Wiki 明确列出的原因拆分方法。保留字段含义、计算、单位、阈值、观测限制和安全提醒，
   不增加经验规则。
6. source identity 提取版本 2 同时识别 `text` fence 和无语言标记的裸 fence。按清单顺序逐字
   保存全部机械日志模板，并严格按输出合同提取 canonical stable marker；不得截短、改写、重排、
   去重或丢失模板。事件名本身不是 marker；例如 `API_COMPLETE service={service}` 对应
   `API_COMPLETE service=`，只写 `API_COMPLETE` 必须判为错误。
7. 业务 `SKILL.md` 只读取 Server 写入的 `request.json`、`target_logs.json`、`methods.json` 和
   `target_logs[*].log_path` 列出的冻结日志。它不加载预处理 Skill、不执行日志预处理、不自行选择
   日志，也不负责最终 Artifact 打包。业务入口必须逐字使用输出合同给出的 Server 边界和 PID 可选
   说明，避免写入任何被禁用的本地运行标识。
8. 先扫描所有正向 marker，再按需加载方法卡；不能命中第一种原因后停止。每个原因、每次独立事件
   分别输出证据，保留完整 `sources` 和同源 `identity_tokens`。日志缺失只能形成 Wiki 允许的未知
   边界，不能自动排除原因。

## 校验

生成完成后必须由调用方运行 validator：

```text
python3 <本元-Skill目录>/scripts/validate_generated_skill.py \
  --registration-dir <生成的-registration目录> \
  --wiki <原始-Wiki路径> \
  --module <用户确认的固定-module> \
  [--source-identity <source-identity-v2.json>] \
  --json
```

只有 validator 返回成功才结束。调用环境已经明确授权执行命令时，生成 Agent 可以代调用方运行；
restricted Test Flow 中，生成 Agent 不得尝试 Bash 或其他未授权命令，只完成文件写入，由 runner 在
生成结束后执行 validator。校验失败时只修正报告的合同问题，不改变 Wiki 业务语义。
