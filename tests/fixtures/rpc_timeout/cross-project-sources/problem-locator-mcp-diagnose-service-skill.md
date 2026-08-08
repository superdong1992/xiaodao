---
name: diagnose-service
description: 用于双 Lima 真实 E2E 的 SERVICE takeover 定位；必须先调用 logparse-diagnose skill 获取 target_logs，再只基于 target_logs[*].log_path 指定日志分析并生成扁平 result.zip。
effort: medium
module_name: EXAMPLE
roles:
  - label: service
    description: 发生 takeover 的 SERVICE 进程
    required: true
---

# SERVICE Takeover 定位

中文显示名：SERVICE Takeover 定位

## 问题范围

用于 logparse 自带的无敏感测试日志包，判断指定 SERVICE 进程在问题时间附近是否发生
takeover，并区分“发生了 takeover”与“能够确认 takeover 的底层触发原因”。

## 运行时输入

先收集全局输入：

- `input_path`：原始日志输入或 `output/{task_id}` 预处理结果目录。原始日志输入可以是
  日志压缩包、单个非压缩诊断日志，或原始日志目录。
- `config_path`：repo 内 V3 配置文件路径，必须包含具体 YAML 文件名，不要只传配置目录。
- `output_dir`：logparse 解析输出目录。
- `python_command`：本次运行固定的 Python 3.12 命令；所有 CLI 和打包命令必须原样使用。
- `problem_time`：问题发生的近似时间。
- `request_path`、`pack_result_script`：本次 Case 固定的请求和打包器路径。
- 固定 module_name：当前 skill 的 frontmatter `module_name: EXAMPLE`，运行时不再向用户询问模块。

目标进程只使用一组记录：

| 标签 | 说明 | 是否必需 | 运行时字段 |
| --- | --- | --- | --- |
| service | 发生 takeover 的 SERVICE 进程 | 是 | `slot`、`process_name`、可选 `pid`、固定 `label: service` |

每个 `targets[]` 元素遵守固定字段契约：

| 字段 | 是否必需 |
| --- | --- |
| `label` | 是 |
| `slot` | 是 |
| `process_name` | 是 |
| `pid` | 否 |

每组目标必须保持为同一条 `固定 module_name + slot + process_name + 可选 pid` 记录。
组装 targets[] 时使用 frontmatter 固定的 module name，并写入 `targets[].module`。当前唯一
目标必须在 `targets[]` 中保留 `label: service`，不得省略用户已提供的 `pid` 或 `label`。

## 先调用 logparse-diagnose skill

`/logparse-diagnose` 是 logparse 项目中的 Claude skill。必须先调用 `Skill` 工具，参数为
`skill: "logparse-diagnose"`；只读取它的 `SKILL.md` 或直接手工执行其中的命令不算调用。
把 `input_path + config_path + output_dir + python_command + problem_time + targets[]` 作为
同一份运行参数交给它。

如果 `input_path` 是原始日志输入，不要省略配置文件路径，不要只传配置目录。预处理必须
等价于使用准确的 `python_command` 执行
`cli.py parse <input_path> -c <config_path> -o <output_dir>`。随后由该 skill 对每个 anchor
调用 `cli.py mech-target-logs`，并返回结构化 `target_logs`。

当前 skill 不自行选择 lifecycle/cycle，也不自行构造目标日志路径。只有
`target_logs[*].log_path` 是允许分析和复制的日志来源。

## 证据收敛约束

- 不要遍历 `output/`，不要重新选择 lifecycle/cycle，不要重新拼接日志路径。
- 不要用相关日志替代缺失的目标日志，也不要读取未由 target_logs 授权的文件。
- 只分析 `target_logs[*].log_path` 中与问题时间、SERVICE 和 takeover 直接有关的行。
- 若日志只证明 takeover 已发生但没有给出底层触发信号，结论必须明确写
  “当前证据不足以确认根因”，不得把 takeover 事件本身夸大成底层故障原因。

## Wiki 定位步骤

1. 检查唯一目标的 `match_status`；若为 missing 或 ambiguous，返回确定性失败。
2. 读取该目标的 `target_logs[*].log_path`。
3. 查找 `Context=No[1] EXAMPLE takeover`，确认 slot、进程名和 PID 与 runtime target 一致。
4. 查找相邻 journal 行，确认 SERVICE 在 takeover 后仍出现在同一 slot。
5. 如果两类证据都存在，结论写明“slot 2 上发生 SERVICE takeover，但现有目标日志没有给出
   底层触发原因”；如果缺少 takeover 行，则写“当前证据不足以确认根因”。

## 判断规则

- diagnostic takeover 行是 takeover 事件的直接证据。
- 同 slot 的 journal SERVICE 行只用于证明事件后的服务活动，不能单独证明触发原因。
- 不允许根据常识补充网络、进程崩溃、资源耗尽或人工切换等未出现于目标日志的原因。

## Result.zip 交付物

在 Case 目录的一个直接子目录中生成 `result.txt`，并把实际读取的目标日志用 `cp --`
复制进去。目标日志使用安全扁平文件名，替换路径分隔符和 Windows 非法字符；若目标包含
CPU 信息，文件名包含 `cpu_<cpu_id>`。

`result.txt` 至少包含：

```text
定位结论
<takeover 结论及“当前证据不足以确认根因”的边界>

关键分析依据
1. <diagnostic takeover 行>
2. <journal SERVICE 行>

证据缺口
<未提供 takeover 底层触发信号>
```

使用运行参数中的准确 `python_command` 调用
`pack_result_zip.py` 对应的 `pack_result_script`，生成 Case 根目录下的扁平 `result.zip`。
不得先执行 `--help`、`--version` 或任何探测命令；打包器只允许执行一次，参数严格为
`python_command pack_result_script <交付目录> <Case 根目录/result.zip>`。
归档根目录只允许 `result.txt` 和本次实际使用的目标日志，不得创建子目录或
`manifest.txt`。打包器成功后立即停止 Shell 操作，不得再用命令打开、列出、测试、读取或
以其他方式检查 `result.zip`。

## 最终回复

确认 `result.zip` 已生成后，返回 `RESOLVED`，并提供非空的 `root_cause`、`evidence` 和
`recommendations`。公开字段不得包含内部路径。根因字段应说明已观察到的 takeover 事件，
并明确底层触发原因仍缺少证据。
