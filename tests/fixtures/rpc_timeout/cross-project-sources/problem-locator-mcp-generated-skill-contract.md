# 生成出的定位 Skill 契约

## 目录

- Frontmatter
- 必备章节
- 运行时输入
- 自定义参数
- Logparse 调用
- 证据和时间约束
- Result.zip

## Frontmatter

生成路径必须是 `.claude/skills/diagnose-<english-topic-slug>/SKILL.md`，目录名
与 `name` 完全一致。新 Skill 不写 `version` 或 `effort`；更新已有 Skill 时保留
原有 `effort` 值。

```yaml
---
name: diagnose-link-timeout
description: 用于链路超时问题定位；必须先调用 logparse-diagnose skill 获取 target_logs，再只基于 target_logs[*].log_path 指定日志按 wiki 规则分析并生成扁平 result.zip。
module_name: EXAMPLE
roles:
  - label: client
    description: RPC 客户端进程
    required: true
  - label: server
    description: RPC 服务端进程
    required: true
---
```

`roles` 必须非空且至少有一个 `required: true`。每项只能包含 `label`、
`description`、`required`：

- `label` 使用 `^[a-z][a-z0-9_-]{0,63}$`，同一 Skill 内唯一。
- `description` 是非空单行中文说明。
- `required` 必须是 YAML 布尔值，不能写成字符串。
- roles 顺序就是运行时 target 的规范顺序。

## 必备章节

正文和运行时提问使用中文，按顺序包含：

1. `问题范围`
2. `运行时输入`
3. `自定义定位参数`
4. `先调用 logparse-diagnose skill`
5. `证据收敛约束`
6. `时间相关性要求`
7. `Wiki 定位步骤`
8. `判断规则`
9. `Result.zip 交付物`
10. `最终回复`

`运行时输入` 中必须包含与 frontmatter roles 同顺序、同 label、同说明和同必选
状态的角色表：

```markdown
| 标签 | 说明 | 是否必需 | 运行时字段 |
| --- | --- | --- | --- |
| client | RPC 客户端进程 | 是 | `slot`、`process_name`、可选 `pid` |
| server | RPC 服务端进程 | 是 | `slot`、`process_name`、可选 `pid` |
```

## 运行时输入

全局输入为 `input_path`、具体 YAML 文件 `config_path`、`output_dir`、
`python_command`、`problem_time`、`request_path` 和 `pack_result_script`。
`module_name` 来自 frontmatter，运行时不得再次询问。

每个 `targets[]` 元素必须遵守以下固定字段契约：

| 字段 | 是否必需 |
| --- | --- |
| `label` | 是 |
| `slot` | 是 |
| `process_name` | 是 |
| `pid` | 否 |

表格声明的是字段及必选性，不能写入某次运行的具体 slot、进程名或 PID 值。

每个 target 是同一条 `label + 固定 module_name + slot + process_name + 可选 pid`
记录。不得把字段拆成独立列表或交叉组合。每个必选 role 都必须收集完整 target；
可选 role 只有在用户提供时才加入。组装 `targets[]` 时将固定 module 写入
`targets[].module`。

## 自定义参数

自定义参数属于整次任务，只支持用户确认后的单行文本值。名称使用小写 snake_case，
最多 32 个，不得使用：`input_path`、`config_path`、`output_dir`、
`python_command`、`request_path`、`problem_time`、`skill_name`、`module`、
`module_name`、`targets`、`target_logs`、`log_path`、`match_status`、`label`、
`slot`、`process_name`、`pid`、`custom_parameters`、`pack_result_script`、
`logparse_repo` 或 `skill_dir`。

有参数时使用 `参数名 | 说明 | 是否必需` 三列表格；没有时必须写：
`本 Skill 不设置自定义定位参数。` 不得输出空表。自定义参数不得写入
`targets[]`、`mech-target-logs` 参数或 logparse 配置。

## Logparse 调用

必须先调用/加载 `/logparse-diagnose`，并一次性交付 input、配置、输出目录、问题
时间和全部 targets。原始输入的预处理等价于：

```text
<python_command> cli.py parse <input_path> -c <config_path> -o <output_dir>
```

业务 Skill 不得自行 parse。`logparse-diagnose` 对每个 anchor 调用
`cli.py mech-target-logs` 并返回 `target_logs`。只允许读取
`target_logs[*].log_path`；禁止遍历 output、重新选择 lifecycle/cycle、重拼路径或
用相关日志替代 missing/ambiguous 目标。

## 证据和时间约束

`problem_time` 是首要锚点。明显偏离问题时间的证据必须由唯一关联键、连续因果链、
wiki 明确的延迟/持续/重试特征或同一生命周期证据支撑。相同关键词、最近日志或没有
更近日志不能单独作为依据。

- `exact`：正常分析，仍从问题时间附近开始。
- `nearest/unknown`：说明 caveat、时间偏差和采用依据。
- `missing/ambiguous` 或没有 `log_path`：停止并报告证据缺失。

无法证明时间相关性时写“当前证据不足以确认该日志与问题时间属于同一次故障”；无法
确认根因时写“当前证据不足以确认根因”。不要根据经验补充 wiki 外的排查方向。

## Result.zip

结果必须是扁平 ZIP，只包含 `result.txt` 和实际读取的目标日志，不得包含目录或
`manifest.txt`。日志名使用安全扁平格式：

```text
<label>__<module_name>__slot_<slot>__<process_name>[-<pid>].log
<label>__<module_name>__slot_<slot>__cpu_<cpu_id>__<process_name>[-<pid>].log
```

`result.txt` 至少包含定位结论、关键依据、时间相关性说明和必要的证据缺口。使用准确的
`python_command` 调用运行时提供的 `pack_result_script`：

```text
<python_command> <pack_result_script> <临时目录> <result.zip路径>
```

最终回复说明结果路径、结论、证据缺口和使用过的目标日志。
