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

- Linux 是唯一受支持的 `problem-locator` Server 平台。Windows 和 macOS 默认使用本机 Claude Code 作为 MCP Client；Linux Client 只在显式选择时启用。三者都通过 HTTP 直连 Linux Server。
- 客户端不安装本地 MCP、不运行代理，也不安装 Problem Locator Hook。不得为了测试方便在客户端增加兼容转发层。
- 客户端暂不记录 Problem Locator 专用 DFX；线上参数、schema 与验证错误以 Linux 服务端的 `mcp.tools.listed`、`mcp.tool.started` 和验证事件为准。
- Host 工具名历史上可能显示为官方 `mcp__problem-locator__problem_locator_*` 或改版客户端的 `problem_locator_problem_locator_*`；重复的 `problem_locator` 来自 server key 和服务端工具名，不表示重复注册。

## 测试活动约束

- 新测试活动以 `tools/test-flow/run.sh` 或 `tools/test-flow/run.ps1` 为唯一入口；`tools/test-flow/harness` 中的旧脚本只是迁移期实现材料，不得绕过新编排器直接拼装一次新发布结论。
- 默认先执行 Dev 的 affected + full deterministic 轨，不调用真实模型。SameJob 属于确定性旅程；Release 只保留一条从空 `DATA_ROOT` 开始的真实 CrossJob 旅程。
- 任何真实模型活动必须先查看 `--plan-only` 的 Stage、身份、复用、预计 token/cost 与 admission blocker。不得自动重试；同一失败身份再次运行前必须给出新的 reason、hypothesis 和 expected evidence。
- `verdict.json` 是唯一权威结论。无 verdict、旧 `verification-report.json`、半成品目录、未按当前扫描器重审的旧 PASS 都不得复用或宣称通过。
- Release 必须绑定 clean commit、当前 Client/Server/Logparse/MCP/Skill/模型上下文身份，从 GENESIS 和新空数据根开始；Dev checkpoint 只能加速诊断，不能替代 fresh Release。
- 测试证据不得自动删除。只能先用 `tools/test-flow/evidence.mjs report` 或 `prune` 的 dry-run 查看，再以精确 `--run-id --execute` 人工删除。
