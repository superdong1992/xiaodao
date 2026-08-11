# Wiki 定位能力泛化设计

状态：已实施；确定性验收通过，真实 Skill 生成等待缓存封存

基线：`main@f477742c0678da210f5c59a3f05ea3725c790246`
更新时间：2026-08-12

## 目的与完成后的效果

本次改造要让 Problem Locator 能把人编写的真实业务定位 Wiki 转换成一个可部署、可审计的
专用 Diagnosis Skill，并让该 Skill 在固定日志快照上给出三种诚实结果：

1. **完整定位**：证据足以支持 Skill 声明的完整结论；
2. **部分定位**：已经确认或排除了一部分因素，但受观测能力、日志抑制、时钟误差或证据缺口
   限制，不能可靠收敛到完整结论；
3. **无法定位**：现有证据没有形成可复核的有效进展。

它解决的不是某一种超时问题，而是以下通用问题：重复事件、多行记录、正向证据与缺失证据的
不同可信度、模块级日志抑制/限流、跨进程关联、带单位的数值推导、时钟容差、多因素共同贡献、
条件分支和正式的部分结果。业务名、日志文本、阈值和局部原因排序只存在于对应 Skill 或一个
自包含用例内，不进入框架源码和测试编排器。

用户在真实环境中的体验是：提供原始问题、Skill 明确声明的少量定位参数和一个固定日志归档；
平台只分析这份快照，不要求事后补历史日志，也不启动持续监控。若缺日志可能是抑制、限流、
版本、协议或采集时点造成，结果必须明确写成“不可观测/仍未确定”，不能把“没看到”当成反证。

## 新基线带来的架构调整

### 两种 DIAGNOSE 模式必须分工

`f477742` 已引入 `DiagnosisMode.SPECIALIZED` 与 `DiagnosisMode.GENERIC`：

- ROUTE 找到专用 Skill 时，继续执行 **SPECIALIZED DIAGNOSE → 独立 REVIEW**。本设计的
  Wiki 转 Skill、Logparse 证据、机器规则、部分定位和结果归档全部属于这条路径。
- ROUTE 返回 `NO_CAPABILITY` 时，Coordinator 不再把 Case 直接标为失败，而是创建隔离的
  **GENERIC DIAGNOSE**。GenericLocatorExecutor 只把 `raw_problem_text` 原样交给预装通用
  Skill，读取严格的 `generic_diagnosis_result.txt`，直接形成 `RESOLVED|UNRESOLVED`；它没有
  Problem Locator Evidence、附件、专用状态或 REVIEW。

因此，旧设计中“把 NO_CAPABILITY 改成 UNRESOLVED”“让通用兜底承接业务 Wiki 证据链”两项
已经失效。GENERIC 用来处理没有专用能力的开放问题；Wiki 生成的业务能力仍是 SPECIALIZED。
本次不得扩大 GENERIC 的输入面，也不得让它读取专用附件或伪装成已审计的专用定位结果。

### `raw_problem_text` 与公开 MCP 扁平合同

`problem_locator_create_case` 已要求根层必填标量 `raw_problem_text`。它是 GENERIC 的完整原始
输入，同时也是 Case 的不可变原始问题记录；它不是 SPECIALIZED 的动态参数 Map，也不能替代
八个根层问题字段和显式 USER_FACT requirements。

七个公开 MCP 工具继续满足：根对象的属性只能是标量、nullable 标量或标量数组。新增能力不
增加嵌套对象、对象数组或动态 Map，不使用 `json.loads` 兼容层，不增加客户端 Hook、代理或
Problem Locator 专用 DFX。Skill 的条件参数仍通过现有 `initial_user_fact_names/values` 与
`input_names/values` 两组等长标量数组传递。

### State V4 基线、State V5 硬切与既有修复

`f477742` 基线的 State/Job/Outcome 已是 V4，`GENERIC_SKILL_NAME`、`generic_result`、严格结果文件和
GENERIC 失败不重试语义已经存在。旧设计中“升级到 State V4”已经完成，不能再次按旧模型设计。
本次为正式部分终态及其审计结构执行第二次明确硬切：当前实现为 Problem Locator 3.0.0、State / Job /
Outcome schema V5 与 `v5-contract-r1`。不迁移旧 DATA_ROOT，部署时使用全新空根；文档、schema、fixture
和 marker 使用同一版本。

以下两个已修复缺陷是本次不可回归的不变量：

- 一个等待轮次可以同时请求普通 INPUT 和一个 ATTACHMENT；INPUT ID 保持在
  `requested_input`，ATTACHMENT ID 保持在 `requested_attachments`，且生成顺序与
  `add_pending_requirements` 一致。
- ROUTE 只向声明了全部初始 USER_FACT 名称的专用 Skill 提供候选；若没有兼容 Skill，应在
  调用路由模型前确定性进入 NO_CAPABILITY，再转 GENERIC，不能在 ROUTE/DIAGNOSE 间轮询。

## 基线中已验证的缺口与本次改造目标

实现前的 `f477742` 基线存在以下已逐项从代码确认的合同限制；它们是本次改造目标，不再描述改造后的
当前合同：

- `build_spec_from_wiki()` 只读取恰好一个 `## GenerationSpec v4` JSON fence；普通 Markdown
  Wiki 不能直接成为真实转换输入。
- manifest v4 的 verification contract 只有 v1，事件只能使用完整 UTF-8 单行正则、
  RFC3339 毫秒时间组与 `EXACTLY_ONE`。
- 服务端在执行任何非语义规则前先要求相关事件恰好出现一次；重复调用、多行块和一类日志的
  多次观测不能安全表达。
- 全部规则按 AND 门禁；没有条件分支、`ANY_OF`、不适用状态或按终态路径选择规则。
- 没有带类型/单位的字段、数值推导、跨时钟容差、同 anchor 通用关联或多行记录组装。
- catalog 明确拒绝 suppression/rate-limit/sampling 字段；因此日志缺失既不能声明其观测限制，
  也不能安全参与结论。
- SPECIALIZED Candidate 要求每个 completion criterion 都为 true，Review PASS 只会进入
  `RESOLVED`；`PARTIALLY_RESOLVED`、结构化贡献因素和可下载的部分结果均不存在。

## 输入与 Wiki 转换边界

### 原始 Wiki 与作者旁注

转换入口接受普通 UTF-8 Markdown，不要求作者手写 JSON 或正则。`(# ... #)` 与
`（# ... #）` 都是作者给转换器的元旁注：转换时可用于理解匿名化、简写和约束，但必须从生成
Skill、manifest、用户结果及通用测试数据中剥离。原 Wiki 原样保存在用例输入中；转换过程中
确实需要的澄清写入同一用例的补充说明并纳入哈希，不能悄悄改写 Wiki。

Wiki 中给出的稳定日志消息体足以作为定位定义，作者不必抄写平台统一前缀或超长完整行，也不必
手写精确正则。转换器可生成两类定位器：

- **机械消息定位器**：对稳定消息体做确定性的单行或有界多行匹配，并声明捕获字段类型；
- **语义定位器**：Wiki 只描述长日志包含哪些信息时，要求 Specialist 与 Reviewer 独立引用
  原始行/行段并判断，不伪造机器精确匹配，也不得由其缺失推导反证。

生成器仍负责确定性规范化、校验和产品渲染；对自然语言 Wiki 的理解由真实
`wiki-to-diagnosis-skill` Agent 完成。确定性脚本不尝试用脆弱启发式 NLP 猜业务规则。

### 只问会改变结论的问题

转换时只对缺失且会改变 Skill 语义的信息提问，例如部署范围、Logparse anchor 的定位字段、
明确依赖日志缺失的模块默认观测策略或跨时钟容差。若某个结论只用正向日志即可形成，不因未知
抑制策略阻塞转换。

当 Wiki 未说明日志抑制，而某条分支需要依据“没有这条日志”判断时，必须询问作者采用哪个模块
策略；作者明确“不知道”时将该缺失证据标为不可验证。既不能发明某业务模块的默认规则，也
不能把完全沉默解释成“无抑制”。

## 通用 Verification Contract

新合同采用事件集合与显式终态路径，不把任一业务名或阈值放入通用枚举。

### 事件与记录

- 事件定位器先匹配并按 Skill 声明的事实/时间选择器筛选，再检查 `min_matches/max_matches`；
  不再全局假设恰好一次。
- 字段声明 `STRING|INTEGER|TIMESTAMP`，数值字段必须带单位，时间字段必须带 clock domain。
- 单个事件可以由一行或有界有序的多行成员组成；合同声明成员顺序、允许行间隔和同一记录的
  关联字段。服务端审计记录每个 occurrence 的来源、行范围和捕获值。
- 关联使用显式复合身份。进程内请求 ID 之类的局部标识不能被框架当作全局唯一值；Skill 应把
  anchor/进程实例或生命周期与局部 ID 一起用于关联。

### 规则与数值计算

机械规则支持事件计数、字段/事实比较、同/跨 anchor 字段关联、有界序列、事件顺序和白名单
数值表达式。表达式仅允许字段、事实、常量、单位换算、加减和常数乘法，再做比较；禁止任意
代码执行。服务端在审计中记录原始值、换算值、派生值和单位。

跨 clock domain 的比较必须由 Skill 显式给出容差，框架没有全局默认。服务端按区间判断：整个
不确定区间都满足才 PASS，整个区间都不满足才 FAIL，阈值落在区间内则 UNKNOWN。同 clock
domain 可以声明零容差。

语义因果规则继续由 Specialist 和盲审 Reviewer 独立判断，但它只能建立在已绑定的正向证据、
机械派生值和显式 UNKNOWN 上；不能把 Agent 文字包装成服务端已验证事实。

### 日志抑制和限流

首版提供两个通用 observation policy：

- `SUPPRESSION`：同一 scope/key 在窗口内后续事件可能不打印；
- `RATE_LIMIT`：同一 scope 在窗口内最多观测到有限次输出。

策略由 Skill 声明适用事件、scope、key、窗口和边界，可叠加。业务模块的“默认所有日志受抑制、
个别日志例外”由转换器展开成每个事件的 policy 引用；通用代码不知道模块名或具体秒数。
`ONCE_PER_SUBJECT` 与采样暂不进入首版，相关 Wiki 语义只能作为正向事件限制和结果 caveat，
不能用于可靠 absence 推导。

存在 lossy policy 时，观测到事件仍是有效正向证据；未观测到事件只能得到 UNKNOWN，观测次数
只是实际次数的下界。固定快照之外不补日志，也不因用户问题时间启动等待或监控。

### 条件分支与终态路径

Skill 显式声明有优先顺序的 terminal paths。每条路径用 `ALL_OF/ANY_OF` 组合规则的
`PASS|FAIL|UNKNOWN`，并声明结果完整度：

- `COMPLETE`：Skill 声明的完整因果链已满足；
- `PARTIAL`：至少一个因素或排除项已被证实，同时未完成项和阻塞原因被显式列出；
- `NONE`：没有足够的可复核进展。

服务端根据规则对齐结果重算并选择路径；Agent 必须绑定同一路径，不能通过省略不利规则缩小
审计范围。未选分支在 audit 中保留 applicability，Reviewer 可看到完整 Skill 规则集、选中路径
和未完成缺口。

## 结构化结论与部分终态

SPECIALIZED Candidate 增加：

- `resolution_status=COMPLETE|PARTIAL`；
- `terminal_path_id`；
- `causal_factors[]`，每项带稳定 ID、`CAUSE|CONTRIBUTOR|CONDITION` 角色、statement、
  Evidence 和 rule 绑定；允许多个共同贡献因素，不强制唯一根因；
- `excluded_factors[]` 与 `candidate_factors[]`；
- completion criterion 使用 `SATISFIED|PARTIALLY_SATISFIED|UNSATISFIED|UNKNOWN`，
  不再用一个布尔值掩盖已完成进展。

Reviewer 不接收 Specialist 的隐藏判词，仍对固定 Candidate、完整 Skill、原始 Evidence、选中
terminal path 和服务端机械结果进行独立复核。Review PASS 表示“Candidate 对其声明的完整度
真实且完整”：COMPLETE 要求完整路径成立；PARTIAL 要求已声明因素全部成立、未完成部分已明确
列为 gap，且不能静默省略冲突规则。

PASS 后 COMPLETE 进入 `RESOLVED`，PARTIAL 进入正式 `PARTIALLY_RESOLVED`。两者均公开服务端
生成的结构化结果 JSON 与 `result.zip`；部分结果的 `root_cause` 可以为空，但 confirmed、
candidate、excluded factors、证据缺口、限制、建议和“超时不等于取消”等安全说明必须保留。
NONE 进入 `UNRESOLVED`，继续只发布 inconclusive JSON 与 audit bundle。

GENERIC 结果合同首版保持 `RESOLVED|UNRESOLVED`，不借用 SPECIALIZED 的部分终态或 Review
语义；未来如需扩展必须单独设计 GENERIC 结果版本。

## 自包含业务用例与泛化护栏

所有业务专有材料只放在一个 case root：原始 Wiki、补充澄清、生成规范、批准的 Skill、合成
日志归档、两个场景的驱动输入和 oracle。通用 loader 只认识文件角色、哈希、action 和通用结果
字段，不认识业务服务名、日志文案、协议、版本或阈值。

首版 case 含两个固定快照场景：

1. 一条 COMPLETE 场景，验证多行记录、复合关联、数值推导和多个共同贡献因素；
2. 一条 PARTIAL 场景，验证正向证据可以排除一项，而跨时钟容差与日志抑制使其余候选保持
   UNKNOWN；快照中更早的日志只能作为抑制上下文，不能当成目标事件证明。

真实 Wiki 和真实日志不进入仓库；case 使用脱敏 Wiki 与小型合成日志。真实部署后不运行仓库
测试，问题由用户带现场证据另行反馈。仓库测试证明的是通用转换/合同/执行能力，不外推真实
环境中的具体业务结论。

case loader 必须校验 exact keys、相对路径、无 symlink/traversal、全文件 size/hash 和
allowlisted journey action。oracle 只供 Gate 校验，绝不暴露给转换 Agent、Specialist 或
Reviewer。另用至少两个中性临时 case 验证字段、附件和分支均由数据驱动，并用 canary 扫描保证
case 专有词没有泄漏到通用源码、适配器或配置。

## Test Flow 验收

所有活动只从 `tools/test-flow/run.ps1|run.sh` 进入：

1. Dev 先执行 `dev.default` 的 affected 与 full deterministic；
2. 新增真实 Wiki→Skill Gate：真实模型只读取原始 Wiki/澄清，产物经确定性 validator、语义
   oracle 和旁注剥离检查；
3. 确定性验证用同一批准产物分别执行 COMPLETE 与 PARTIAL 两个场景，覆盖服务端重算、独立 Review、
   结果公开和归档；Release 只保留一个选定场景的 fresh CrossJob，验证 Logparse 只解析一次、重启后
   下载字节不变，不把两个业务场景硬编码进编排器；
4. Release 仍从 GENESIS 与空 DATA_ROOT 开始，并把 case input digest、批准 Skill digest、模型、
   Client/Server/Logparse/MCP 和源码快照全部纳入 identity；
5. 真实模型活动必须先 `--plan-only`。缺少 cache seal 等 admission 条件时只报告 BLOCKED，不运行
   模型且不盲重试。

实现前权威基线证据为 `run-20260811T164446Z-49c22067`：`PASS_WITH_WARNINGS`，功能与证据复核
均为 PASS；总计 2373 passed、1 skipped、0 failed/errors，唯一 warning 为
`NOT_CALIBRATED`。真实 generic Skill Gate 仅完成 plan-only，并因
`CLAUDE_CACHE_SEAL_MISSING` 未运行模型；它不能作为真实模型通过证明。

### 实施检查点与最终交接证据

开发过程中的确定性实施检查点曾达到 contracts 556、unit 1692、integration 44、same-job journey 4，
共 2296 passed、1 skipped、0 failed/errors，且 functional、operation、verification 与 secret scan
均为 PASS；唯一 verdict warning 是性能样本不足导致的 `NOT_CALIBRATED`。该结果只证明检查点自身
绑定的 Git-visible snapshot，不声称覆盖检查点之后发生变化的文档、代码、路径或文件模式。

最终交接的权威结论只来自仓库外 Test Flow evidence 中不可变且已密封的 `verdict.json`。交接报告必须
给出该 verdict 的 run ID、`source.snapshot.digest` 与 `file_count`，并确认它们对应交接时 Git-visible
工作树的精确字节和模式；交接前只要任一 Git-visible 字节、路径或模式变化，原 verdict 就不再证明
当前工作树，必须对新 snapshot 重新执行 plan-only 和完整验证。本文档不固化“最新”run ID 或 digest，
避免为了更新证据编号而改变被证明的 snapshot，形成无限自引用。

`proof.real-skill-generation` / `real.skill-generation` 也必须对同一个最终 snapshot 单独执行 plan-only。
官方 Claude Code 2.1.89 CLI、包信息与 env-only settings 应先完成身份检查；若正式缓存没有
`cache-seal.json`，admission 应以 `CLAUDE_CACHE_SEAL_MISSING` 阻塞且不得调用模型。计划上限仍为
12 turns、200,000 token、3 USD 与 1200 秒。真实 Wiki→Skill 生成与 fresh Release CrossJob 只有在
各自 Gate 实际执行并形成密封 verdict 后才能宣称通过。

## 完成判据

- 普通 Markdown Wiki 无需 GenerationSpec fence 即可由真实转换 Skill 产出新合同 Skill；
- 特殊旁注不进入产物，缺失但影响 absence 语义的策略会触发明确澄清；
- 事件重复、多行组、两种 observation policy、单位换算、显式时钟容差和多因素结论均有服务端
  audit；
- COMPLETE、PARTIAL、NONE 三条路径均由服务端重算；COMPLETE/PARTIAL Candidate 经独立 Review，
  NONE 不生成 Candidate、也不伪造 Review；
- PARTIAL 以正式 Case 状态公开 JSON 与 ZIP，不伪装成完整定位，也不丢弃已证实进展；
- GENERIC fallback、`raw_problem_text`、七工具扁平 schema、混合 INPUT+ATTACHMENT 等待和初始
  fact 名称兼容过滤均保持原行为；
- case 专有字符串仅存在于自包含 case root，通用代码与 Test Flow 配置保持业务无关；
- 最终结论只引用当前源码快照对应的 `verdict.json`，并明确未运行的真实项和 warnings。
