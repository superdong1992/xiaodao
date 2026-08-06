# 项目协作约束

## 修复前必须验证问题

- 在修改代码、合同、Skill、文档或测试来“修复”用户描述的问题之前，必须先在当前工作区和当前版本中确认问题确实存在。
- 验证时必须核对实际使用的版本、入口、输入和输出；不得依据旧版本、旧产物、历史结论或仅凭描述直接开始修改。
- 优先通过最小复现、现有测试、代码路径检查或生成产物检查取得证据，并先向用户说明确认结果。
- 如果无法复现，或证据表明问题来自版本/调用方式不一致，则停止修复性修改，只报告调查结果和需要对齐的信息。
- 未确认问题存在前，只允许进行只读调查，不得以“顺手优化”名义扩大修改范围。

## MCP 输入 schema 扁平化约束

- Claude Code/局域网改版 Host 可能把嵌套 object、对象数组或动态 Map 二次字符串化。所有公开 MCP 输入参数必须保持扁平，不设历史白名单或例外。
- 根 object 的属性只能是标量、nullable 标量或标量数组。禁止 `$ref/$defs`、嵌套 object、动态 Map 和对象数组；schema lint 必须覆盖全部七个公开工具。
- `problem_locator_create_case` 的八个问题字段直接位于根层；初始事实只能使用等长的 `initial_user_fact_names` 和 `initial_user_fact_values` 标量数组。
- `problem_locator_submit_supplement` 的补充输入只能使用等长的 `input_names` 和 `input_values` 标量数组。
- 不得通过服务端 `json.loads`、隐藏旧字段或客户端 Hook 掩盖错误输入类型。重新引入非扁平字段、兼容 Hook 或客户端 DFX，必须先获得用户明确批准。

## 客户端部署边界

- Windows Claude Code 是当前唯一 MCP Client，通过 HTTP 直连 Linux `problem-locator` MCP Server；客户端不安装本地 MCP、不运行代理，也不安装 Problem Locator Hook。
- 客户端暂不记录 Problem Locator 专用 DFX；线上参数、schema 与验证错误以 Linux 服务端的 `mcp.tools.listed`、`mcp.tool.started` 和验证事件为准。
- Host 工具名历史上可能显示为官方 `mcp__problem-locator__problem_locator_*` 或改版客户端的 `problem_locator_problem_locator_*`；重复的 `problem_locator` 来自 server key 和服务端工具名，不表示重复注册。
