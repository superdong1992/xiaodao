# 项目协作约束

## 设计到 Goal 流程

- 凡涉及仓库设计、规划、架构、接口/schema、公开行为或 Test Flow 流程变更，必须先使用仓级 Skill `$design-to-goal`（`.agents/skills/design-to-goal/SKILL.md`），不得依据临时会话直接进入实现。
- 收到实现请求时，如果不存在当前变更对应的、已按 SHA-256 明确批准的 `work-items/YYYYMMDD-<kebab-name>/design.md`，以及与之匹配的活动 Codex 执行 Goal，必须先回到 `$design-to-goal`。匹配要求 Goal objective 含规范 work-item 路径以及冻结的 conversation/design/goal 三个 SHA-256；已经在匹配 Goal 内执行时不得递归启动新的设计流程。
- 设计必须在普通/default 模式进行；处于系统 Plan 模式时必须停止并要求切换。在匹配 Goal 成功创建并确认 active 前，只允许写入唯一且固定的当前 work-item 目录；设计批准、Goal 启动请求或 Goal 创建失败均不解除该边界，源码、当前设计/运维文档、测试、AGENTS、Skill、Git 元数据与外部系统保持只读。
- 设计必须核对适用仓级要求、`design/README.md` 导航出的当前权威设计、相关代码/schema/config 与测试事实。所有冲突必须记录来源、影响、有效选项和用户裁决；存在未解决冲突时不得批准设计、生成 `goal.md` 或创建 Codex Goal。
- 对 `tools/test-flow/**`、Test Flow 架构/操作文档或本文件测试活动规则的任何变更，必须在设计中列明具体影响并获得独立、明确授权；普通设计批准不得隐含该授权。
- 设计批准必须绑定冻结的 `design.md` SHA-256；批准设计与启动 Codex Goal 必须是两条独立用户指令。启动前后都必须调用当前 Goal 检查能力并 fail closed：不得自动替换或清除未完成 Goal，新 Goal 必须绑定规范 work-item 路径以及冻结的 conversation/design/goal 摘要，确认匹配后才允许写入实现范围。只有创建明确失败且检查明确确认无未完成 Goal 时才能让候选摘要失效并继续 work-item 内归档；结果不明时文件保持冻结且只能只读复查。
- Codex 执行 Goal 统一覆盖批准范围内的实现、普通测试、当前权威文档同步和既有 Test Flow 验证，并只以与结果源码绑定且验证成功的 `verdict.json` 收口。Goal 创建前必须冻结三个 work-item 文件，执行与 Test Flow planning 后不得再修改它们。执行中发现新设计决策或未授权 Test Flow 影响时，必须停止实现并要求用户取消/清除当前 Goal；确认无未完成 Goal 后必须保留旧 work-item 不变并创建带前驱摘要的 `-r2` 后继，重新批准后再以新的独立指令启动新 Goal。全部实现、文档、计划身份和 verdict 条件逐项满足后才允许将 Goal 标记为 complete。
- 纯只读解释或调查不要求创建 work-item；但一旦转为修改请求，必须进入上述流程。

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

- 新测试活动以 `tools/test-flow/run.sh` 或 `tools/test-flow/run.ps1` 为唯一入口。Goal、Proof、Stage、Gate、identity、policy 与 runtime profile 由 `tools/test-flow/config/*v2.json` 定义，不得绕过编排器拼装发布结论。
- 默认先执行 Dev 的 affected + full deterministic 轨，不调用真实模型。SameJob 属于确定性旅程；Release 只保留一条从空 `DATA_ROOT` 开始的真实 CrossJob 旅程。
- Windows、macOS 与显式 Linux Client 只能使用仓库拥有的 built-in adapter；不得从 CLI 注入任意 adapter 或为测试增加本地 MCP、代理、Hook、客户端专用 DFX。
- 任何真实模型活动必须先查看 `--plan-only` 的 Proof、Stage、Gate、身份、复用、模型预算、预计 token/cost 与 admission blocker。不得盲重试；同一失败身份再次运行前必须给出新的 reason、hypothesis 和 expected evidence。
- `verdict.json` 是唯一权威结论。无 verdict、半成品目录、可独立编辑的摘要、未按当前扫描器和事件合同重审的旧 PASS 都不得复用或宣称通过。
- Release 必须在 planning 时冻结 Git 可见工作树（tracked 当前字节与未忽略的 untracked 文件）的不可变源码快照，并绑定其 SHA-256 清单、当前 Client/Server/Logparse/MCP/Skill/模型上下文身份，从 GENESIS 和新空数据根开始。工作树无需预先提交，但从 planning 到 verdict 期间源码发生漂移必须失败；Git 提交只在全部验证完成后用于持久化完全相同的快照。Dev checkpoint 只能加速诊断，不能替代 fresh Release。
- 测试证据不得自动删除。只能先用 `tools/test-flow/evidence.mjs report` 或 `prune` 的 dry-run 查看，再以精确 `--run-id --execute` 人工删除；被 verdict 引用的 source receipt 不得删除。
