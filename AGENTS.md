# 项目协作约束

## 修复前必须验证问题

- 在修改代码、合同、Skill、文档或测试来“修复”用户描述的问题之前，必须先在当前工作区和当前版本中确认问题确实存在。
- 验证时必须核对实际使用的版本、入口、输入和输出；不得依据旧版本、旧产物、历史结论或仅凭描述直接开始修改。
- 优先通过最小复现、现有测试、代码路径检查或生成产物检查取得证据，并先向用户说明确认结果。
- 如果无法复现，或证据表明问题来自版本/调用方式不一致，则停止修复性修改，只报告调查结果和需要对齐的信息。
- 未确认问题存在前，只允许进行只读调查，不得以“顺手优化”名义扩大修改范围。

## Claude Code MCP 工具命名兼容

- Problem Locator 客户端存在两种已确认的 Host 工具名：官方 Claude Code 使用 `mcp__problem-locator__problem_locator_*`，旧版/改版客户端使用 `problem_locator_problem_locator_*`。后者第一个 `problem_locator` 来自规范化 server key，第二个来自服务端工具名，不表示服务端重复注册。
- 修改客户端 Hook、matcher、DFX 或相关测试时必须同时覆盖两种格式，保留原始 `tool_name`，归一化后仍严格限制为七个 Problem Locator 工具；不得用宽泛前缀记录其他工具。
- `/hooks` 显示 Hook 已加载不代表 matcher 已命中。没有客户端日志时，先核对当前项目的默认 `.problem-locator/client-dfx.jsonl`、完整 Host 工具名和 Hook 命令，再判断参数序列化边界。
- 对对象被发送为 JSON 字符串的问题，必须关联 Hook 与服务端相同 `request_id` 的类型证据；不得通过服务端自动 `json.loads` 掩盖客户端类型错误。
