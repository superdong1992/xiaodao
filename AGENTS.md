# 项目协作约束

## 中文文档与回复

- 使用自然、流畅的简体中文，先说清结论或操作，再解释必要的技术细节。
- 按中文习惯组织句子，不照搬英文语序。长句按意思拆开，避免连续堆叠术语和限制条件。
- 优先用常见说法，例如“输出格式”“注册目录”“创建工作区并写入文件”。不要写成“输出形状”“registration root”“完整物化”。
- 说明版本固定、文件校验或结果保存时，直接写清对象和动作，避免笼统地说“冻结身份”“权威交付”“验证收口”。正式协议名称确有需要时保留，并在首次出现时解释。
- 保留代码标识符、命令、API 名称、文件名、路径、占位符和通用技术术语；普通说明中的英文词语优先改用自然中文，不作机械的全局替换。
- 写完后通读一遍，确认不熟悉内部实现的读者也能理解。文字润色不得改变接口、参数、示例或技术含义。

## 修复前必须验证问题

- 在修改代码、合同、Skill、文档或测试来“修复”用户描述的问题之前，必须先在当前工作区和当前版本中确认问题确实存在。
- 验证时必须核对实际使用的版本、入口、输入和输出；不得依据旧版本、旧产物、历史结论或仅凭描述直接开始修改。
- 优先通过最小复现、现有测试、代码路径检查或生成产物检查取得证据，并先向用户说明确认结果。
- 如果无法复现，或证据表明问题来自版本/调用方式不一致，则停止修复性修改，只报告调查结果和需要对齐的信息。
- 未确认问题存在前，只允许进行只读调查，不得以“顺手优化”名义扩大修改范围。

## 修复登记与回归测试

- 每个已确认并完成实现的修复都必须在推送前新增或更新根目录 `FIXED_ISSUES.md`。同一问题再次回归时，
  在原条目追加受影响版本、回归原因和再次修复记录，不得割裂成没有历史关联的新条目。
- 每条记录必须包含症状、受影响版本、根因、不可回归行为、修复历史、专项回归测试和最新
  Test Flow verdict。日常开发中的活跃事项只进入 `TODO.md`。
- 日常修改只要求完成直接覆盖问题的专项回归。专项通过但尚未准备推送时，在 `TODO.md` 记录
  “专项通过，待推送前完整验证”，并列出用例和结果；不得为提前关闭待办而每次运行完整测试。
- 准备推送时，先整理 `FIXED_ISSUES.md` 的修复正文和专项用例，并完成对应的 `TODO.md` 调整，
  明确本次修复尚待完整验证，不能沿用历史 PASS 宣称本次已验证。然后运行完整 Test Flow；
  通过后只回填 verdict 引用元数据行，本次是否已验证以该行结论为准，失败则仍为待验证。
- 每个修复必须新增能直接复现原问题的测试，或明确列出已经直接覆盖该问题的既有测试；
  泛化的全量 PASS、代码存在或文档声明不能替代专项回归用例。
- 修复只有在规定的 Test Flow 入口产生对应源码快照的权威 `verdict.json` 后才能登记为已验证；
  台账中的 verdict run ID、source snapshot digest 和状态必须与最终交付结论一致。verdict 引用
  只能在验证完成后写入，因此该引用行本身不宣称被其所引用的源码快照覆盖；除该元数据行外，
  不得在最后一次通过后继续修改交付字节。

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

- 正式 Test Flow、Release、源码快照证明和修复台账的最终验证以 `tools/test-flow/run.sh` 或 `tools/test-flow/run.ps1` 为唯一入口。Goal、Proof、Stage、Gate、identity、policy 与 runtime profile 由 `tools/test-flow/config/*v2.json` 定义，不得绕过编排器拼装发布结论。
- 日常每次修改只跑直接覆盖本次改动的最小测试：优先选择具体用例，其次选择相关测试文件；跨模块改动补充相关集成用例，纯文档改动只做文档检查。允许直接运行仓库已有的 pytest、Node 专项测试或静态检查，记录命令、范围和结果；这些结果只用于开发反馈，不代表完整 Test Flow、Release 或修复台账的最终验证。
- `dev.quick` 是可选的编排检查，不是每次修改的必跑项。若返回 `AFFECTED_SCOPE_REQUIRES_FULL`，保留其未完成结论，继续用专项测试验证本次改动，将完整测试留到推送前；不得自动升级为 `dev.default`，也不得把未执行或零用例记成通过。
- 每次推送前，对本次待推送的最终源码统一运行一次 `dev.default`（affected + full deterministic），先查看 `--plan-only`，再执行并核对权威 `verdict.json`；不调用真实模型。完整验证未通过不得推送。验证后若继续修改源码，先补跑受影响的最小测试，下一次推送前重新完整验证。日常编辑和本地提交不触发完整测试。
- Linux/macOS 开发期 Fast E2E 只能使用仓库拥有的 `tools/test-flow/quick-validation/<provider>/run.sh`，或密封 Ubuntu 22.04 的 `tools/test-flow/quick-validation/wsl/run.sh`。standalone `verdict.json` 只证明其计划声明的 Fast E2E，不得外推为 Release、完整 Test Flow 或产品修复登记结论；不得把 Fast E2E 重新接入中央 Goal/Proof/Stage/Gate。
- SameJob 属于确定性旅程；Release 是独立的发布验收，仍保留一条从空 `DATA_ROOT` 开始的真实 CrossJob 旅程，不因普通修改或推送自动触发。
- Windows、macOS 与显式 Linux Client 只能使用仓库拥有的 built-in adapter；不得从 CLI 注入任意 adapter 或为测试增加本地 MCP、代理、Hook、客户端专用 DFX。
- 任何真实模型活动必须先查看对应入口 `--plan-only` 中的身份、调用数、模型预算、预计 token/cost 与 admission blocker；中央 Test Flow 还必须查看 Proof、Stage、Gate 和复用决定。不得盲重试；同一失败身份再次运行前必须给出新的 reason、hypothesis 和 expected evidence。
- `verdict.json` 是正式 Test Flow 的唯一权威结论。无 verdict、半成品目录、可独立编辑的摘要、未按当前扫描器和事件合同重审的旧 PASS 都不得复用或宣称完整验证通过；日常专项测试必须明确标注其有限范围。
- Release 必须在 planning 时冻结 Git 可见工作树（tracked 当前字节与未忽略的 untracked 文件）的不可变源码快照，并绑定其 SHA-256 清单、当前 Client/Server/Logparse/MCP/Skill/模型上下文身份，从 GENESIS 和新空数据根开始。工作树无需预先提交，但从 planning 到 verdict 期间源码发生漂移必须失败；若验证后创建发布提交，只能持久化完全相同的快照。日常本地提交不要求提前执行 Release。Dev checkpoint 只能加速诊断，不能替代 fresh Release。
- 测试证据不得自动删除。只能先用 `tools/test-flow/evidence.mjs report` 或 `prune` 的 dry-run 查看，再以精确 `--run-id --execute` 人工删除；被 verdict 引用的 source receipt 不得删除。
