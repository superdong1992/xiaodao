---
name: wiki-to-logparse-diagnosis-skill
description: Convert an authored troubleshooting Wiki into a LAN-direct Claude diagnosis Skill that requires client/server slots, loads logparse-diagnose, and returns a report plus result.zip. Do not use for current Server Methods packages or incident diagnosis itself.
---

# Wiki 转局域网 Logparse 定位 Skill

把一份已经评审的定位 Wiki 转成可在局域网 Claude Code 中直接运行的定位 Skill。生成物负责收集
client/server anchor、加载现装 `logparse-diagnose`、分析它返回的目标日志，并交付结论摘要与
`result.zip`。不要修改现有 Methods package，也不要生成当前 Server 的 registration。

## 开始前

必须取得以下输入：

- 原始 Wiki；
- 以 `diagnose-` 开头的目标 Skill 名称和输出目录；
- client 与 server 共用的固定 Logparse module。

module 必须由用户明确提供，且为 1–128 字节的非空单行 ASCII。缺失或不合法时停止并询问，
不得从 Wiki、日志模板、示例或历史产物猜测。

完整阅读 Wiki。调用方提供 source identity v2 时，读取并逐字使用其中的 Wiki SHA-256 与
`log_templates`；未提供时只能用确定性哈希工具计算。不要修改或规范化 Wiki 字节。

生成前阅读 [输出合同](references/output-contract.md)。同时读取
[固定打包器](assets/pack_result_zip.py)，并把它逐字复制到生成物的
`scripts/pack_result_zip.py`，不得改写。

## 转换原则

1. 无条件注入 `problem_time`、`client_slot`、`client_process_name`、`server_slot`、
   `server_process_name` 五个必填输入；`client_pid`、`server_pid` 只在用户主动提供时使用。
2. `client_slot` 和 `server_slot` 始终来自本次诊断的用户输入，不得固定、推断或从日志反查。
3. `log_archive` 始终是必需附件。Wiki 明确要求的其他参数和附件在固定输入之后按源顺序保留；
   同义的时间、slot 或进程信息映射到固定名称，不生成别名。
4. 按 Wiki 明确列出的原因拆分方法。保留字段含义、计算、单位、阈值、观测限制和安全提醒，
   不增加经验规则。
5. 按 source identity v2 的顺序逐字保存全部机械日志模板，并严格按输出合同的机械算法提取
   canonical stable marker；marker 不能包含 `{field}`、`%x` 占位符，也不能直接使用完整模板。
   同一正向模板如果能凭自身字段与 Wiki 计算独立确认多个原因，必须把它的 marker 列入每个适用
   方法，不能只挂到其中一个方法；只有不能区分任何具体原因的共同症状才只放共享引用。
6. 生成的入口必须先确认所有必填输入齐全，再恰好加载一次 `Skill(logparse-diagnose)`；加载后
   遵守现装 Helper 返回的合同。不得复制旧 CLI 参数、直接运行 Logparse 或设置回退路径。
7. 只分析 Helper 返回的 `target_logs[*].log_path`。missing、ambiguous、无路径或 Helper 失败时
   停止并报告证据缺口，不遍历目录、不猜路径、不选择邻近日志。
8. 最终回复直接给出结论、关键证据、证据缺口、所用日志和 ZIP 路径；完整 `result.txt` 与实际
   使用的日志使用固定打包器生成扁平 `result.zip`。日志副本名遵循输出合同的固定安全格式，
   必须保留 label、module、slot 与 process name。

## 校验

生成后执行：

```text
python3 <本元-Skill目录>/scripts/validate_generated_skill.py \
  --skill-dir <生成的-Skill目录> \
  --wiki <原始-Wiki路径> \
  --module <用户确认的固定-module> \
  --json
```

只有 validator 返回成功才结束。校验失败时只修正报告的合同问题，不改变 Wiki 业务语义。
