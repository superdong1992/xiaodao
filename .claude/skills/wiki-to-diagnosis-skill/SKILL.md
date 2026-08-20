---
name: wiki-to-diagnosis-skill
description: 将普通 UTF-8 故障定位 Wiki 转换为通用 Problem Locator 专用 Diagnosis Skill；让作者确认角色及 Wiki 参数的 REQUIRED/OPTIONAL/CONDITIONAL 定义，声明 Logparse 映射、事件集合、观测限制、机器规则和 COMPLETE/PARTIAL/NONE 终态路径，生成并校验 manifest schema v6。用于新建或升级 diagnose-* Skill。
---

# Wiki to Diagnosis Skill v6

本 Skill 把人编写的业务定位 Wiki 理解为一份明确、可审计的 `GenerationSpec v6`，再调用确定性
generator 生成恰好两个文件：`SKILL.md` 与 `diagnosis-skill.json`。自然语言理解属于当前转换
Agent；脚本只做严格规范化、渲染与校验，不用启发式 NLP 猜规则。

业务名、日志消息、版本、协议、阈值、原因排序和模块策略只能进入生成 Skill 或自包含业务用例，
不得写入 Problem Locator 通用源码、公共 output contract 或 Test Flow 配置。

## 输入与旁注

输入可以是普通 Markdown，不要求作者手写 JSON、完整平台日志前缀或正则。`(# ... #)` 与
`（# ... #）` 是转换元数据。解释任何业务语义前，先剔除每个旁注的起止标记及其整个正文，
只保留标记外正文作为业务事实源。旁注正文只能用于排除审计；临时禁止集合仅包含旁注中未由
标记外正文或权威澄清独立支持的实质内容。旁注与外部来源语义重叠时，只能依据标记外正文或
权威澄清生成并记录具体源映射，不得因旁注重复而删除合法事实，也不得借旁注补足外部来源未声明的
限定。绝不能用旁注正文理解、补全、修正或推断业务语义；旁注标记、旁注独有的逐字或独特片段，
以及临时禁止集合中的内容均不得复制、改写、概括或转成约束、GenerationSpec 字段值、生成 Skill、
manifest 或最终用户结果。发现未闭合、嵌套或交叉的旁注标记时停止并请作者修正，不得猜测边界。

作者给出的稳定日志消息体可以生成 `SEARCH` 定位器；只有作者明确给出完整行合同才使用
`FULL_LINE`。Wiki 只描述超长日志“包含哪些字段”而没有稳定文本时，把它留作语义证据要求，
不要伪造正则。平台统一前缀不是作者必填信息。

## 只确认会改变语义的信息

先完整阅读 Wiki，再只询问缺失且会改变产物的问题：

1. Skill id/capability/版本、`PRODUCTION|TEST_ONLY` 和定位范围；
2. Wiki 专属 requirements 的 name、阶段、提示、约束、来源以及
   `REQUIRED|OPTIONAL|CONDITIONAL`；
3. Logparse roles、各 role 的 `REQUIRED|OPTIONAL`、Wiki 来源及固定 module；
4. 依赖“日志缺失”分支的 observation policy 与跨时钟容差；
5. COMPLETE、PARTIAL、NONE 各自需要哪些规则结果，以及因素/排除项如何绑定证据。

若结论只使用正向日志，不因未知抑制策略阻塞。若结论依赖 absence，而 Wiki 未声明模块策略，
必须询问；作者不知道时，让相关规则得到 UNKNOWN，不能发明“无抑制”。模块默认“多数日志受
抑制、少数明确无抑制”时，应把默认策略展开成每个受影响 event 的 policy 引用，例外 event
使用空引用。多个策略可以叠加。

内置 profile 自动声明全局 `problem_time`、每个 role 的 `slot/process_name/pid` 和 Logparse
归档，不要向作者询问这些字段的定义或日志归档 Content-Type。平台按后缀固定映射
`.gz/.tar.gz/.tgz`、`.zip`、`.tar`。

### 作者确认门禁

模型只能提出候选，不得替作者确认。生成最终 GenerationSpec 前，展示两张表：

1. role label、说明、`REQUIRED|OPTIONAL`、具体 Wiki 来源；
2. Wiki 参数 name、阶段、提示、`REQUIRED|OPTIONAL|CONDITIONAL`、条件及具体 Wiki 来源。

作者提供的完整权威澄清文件可以直接完成确认；只对缺失或冲突项询问作者。每个最终 role 和
Wiki requirement 都必须带非空 `source_reference` 与字面量 `confirmed=true`。未确认完整时停止，
不得写 GenerationSpec、不得调用 generator，也不得把模型置信度当作确认。

## Requirements 与工具边界

GenerationSpec 的 `requirements` 只包含 Wiki 专属参数。内置 profile 在 generator 中自动注入：

- 全局 `problem_time`：REQUIRED；
- 每个已确认 role 的 `<role>_slot`、`<role>_process_name`：REQUIRED；
- 每个已确认 role 的 `<role>_pid`：OPTIONAL；
- Logparse Skill 的 `log_archive`：REQUIRED。

Wiki 参数使用三态：REQUIRED 始终激活；OPTIONAL 从不主动请求，只使用创建 Case 时已经提供的
事实；CONDITIONAL 仅在显式机器 DNF 条件成立时激活。REQUIRED/CONDITIONAL 使用
`MISSING_ONLY`，OPTIONAL 使用 `NONE`。REQUIRED role 始终激活；OPTIONAL role 完全未提供时不
激活，一旦它的任一扁平字段已提供，就激活该 role 并要求补齐 slot 与 process_name。旧
`custom_parameters` 必须显式转成 Wiki INPUT requirement。

CONDITIONAL 的 `activation_condition` 只使用严格 DNF：term exact fields 为
`source/name/operator/value`，operator 只能是 `EQUALS`。INITIAL 与 AFTER_LOGPARSE 都可读取另一
个非条件 INITIAL `USER_FACT`；只有 AFTER_LOGPARSE 可读取 `RULE_RESULT=PASS|FAIL|UNKNOWN`。
RULE_RESULT 必须来自 verification contract 的机械 rule，禁止 SEMANTIC_CAUSALITY，且该 rule 的
事实、selector 与递归依赖不得使用待激活参数。禁止自依赖、条件链和循环。

- `requires_logparse` 只控制工具绑定，不代表 RPC 或固定参数组。
- `LOGPARSE_RESULT` 只能形成 Evidence/Finding/proposed fact，不能满足 USER_FACT。
- 每阶段最多一个 ATTACHMENT；AFTER_LOGPARSE 只允许 INPUT。
- 每个等待轮次先请求当前阶段全部缺失 INPUT；INPUT 齐全后，下一轮才请求缺失 ATTACHMENT。
- parse 后等待补参时，必须用 `state_delta.add_evidence_bindings` 接受要跨 Job 保留的 Evidence，
  并让它绑定同一 `LOGPARSE_RUN`；续跑只复用正式运行，不重新 parse。

## GenerationSpec v6

Skill 工具的加载结果会显示 `Base directory for this skill: <实际绝对目录>`。该实际目录是本
Skill 所有相对链接的唯一解析根。调用 `Read` 时，必须先用它作为下面 `references/...` 目标的
绝对前缀；不得把裸 `references/...` 交给 `Read`，也不得相对当前工作目录、输入 workspace
或仓库根解析。

Skill 加载成功后，下一条 assistant response 必须且只能按顺序并发发出两个 `Read` tool-use block：
先读 workspace 的 `inputs/wiki.md`，再读 `inputs/clarifications.md`。这是全流程唯一允许批量或并发
工具调用的 response。必须等待这两个 `Read` 的 tool result 都返回，才可在新的 assistant response
读取 GenerationSpec reference。GenerationSpec reference 及之后的每个 `Read` 都必须严格串行：
每条 assistant response 只发出一个 tool call，并等待其 tool result 返回后再进入下一次调用。工具
ordinal 保持为 0=`Skill`、1=Wiki `Read`、2=clarifications `Read`、3..8=六次串行 reference/checkpoint
`Read`、9=唯一 `StructuredOutput`。

开始构造前必须完整读取 [GenerationSpec v6 精确参考](references/generation-spec-v6-reference.md)；到达下述
固定边界后，再完整读取 [verification contract v2 精确参考](references/verification-contract-v2-reference.md)。
它们是转换 Agent 可使用的自包含格式合同；必须执行 verification reference 第 9.1 节的
逐引用内部清单并对照其中的正反例，再开始唯一 `StructuredOutput`。两份 reference 中遗留的
“Write 前”或“不得 Write”只表示最终提交边界；在本流程中一律解释为 `StructuredOutput`，绝不授权
`Write`、`Edit` 或 `Bash`。不要读取 generator、validator、Runtime 或测试源码来反推格式。
`references/wiki-template.md` 只演示无 Logparse 的最小对象，
`references/neutral-logparse-generation-spec-v6.json` 只演示复杂 Logparse 结构。它们不是正式
转换输入；转换 Agent 不得 `Read` 这两项可选示例。示例中的 identity、名称、文本、阈值和策略
均不是默认值，禁止复制到当前业务产物；所有业务值必须来自当前 Wiki 与已确认澄清。

下列四份文件只是在受控阶段之间强制产生工具边界，不是格式合同或业务来源：
[checkpoint 01](references/checkpoints/01-begin-repeated-families-and-paths.md)、
[checkpoint 02](references/checkpoints/02-begin-9-1-inventory.md)、
[checkpoint 03](references/checkpoints/03-begin-9-2-witnesses.md) 和
[checkpoint 04](references/checkpoints/04-write-now.md)。它们均为 `control_only: true`，其内容、路径、
checkpoint id 和控制指令绝不能进入 GenerationSpec、生成 Skill、manifest 或用户结果。只能在下述唯一
状态转换处读取一次，不得提前读取、重读或把它们当作补充业务信息。

按上述合同形成一个紧凑 `GenerationBlueprint` v1 根 plain object。它的 `spec` 保存最终 GenerationSpec
除 `verification_contract` 外的字段，`verification` 保存 literal policies/extractors/rule segments/path segments
与一个版本化 ordered-interval family；可信 compiler 才能展开 144 条重复 rules 和三条 family paths。
不得把 IR 手工序列化成 JSON 字符串、Markdown fence 或文件，也不得包在 `result`、`output`、`content`
或 `value` 等外层字段中。最终 GenerationSpec 的 requirements、logparse_plan 与展开后的
verification_contract 仍是唯一机器事实源。

### 有界构造与通过即提交

转换必须严格执行以下有界单遍状态机。每一阶段只处理列出的增量，禁止复述已完成字段、从对象开头
重启、重新推导固定输入、提前执行后续阶段，或用自由推演替代唯一的下一次工具调用：

1. 读取 Wiki、权威澄清和 GenerationSpec reference 后，建立紧凑源映射，只记录非 verification
   字段的 materialization blueprint：顶层 envelope、identity/version、roles、requirements、
   logparse_plan 和来源支持的业务文本数组。不要物化 root object、policies、extractors、rules 或
   terminal paths；本阶段必须以使用实际
   Skill base 绝对路径读取 verification reference 结束。
2. verification reference 的 `Read` 必须独占其 assistant response；等待该 tool result 返回后，不得先构造
   任何 verification 字段、输出说明或发出其他工具。下一条独立 assistant response 必须且只能读取
   `checkpoint 01`，并等待其 tool result 返回。
3. `checkpoint 01` 返回后，只记录恰好两个 observation policies、十个 event extractors 与全部固定
   非重复 rules 的紧凑 blueprint；不得物化 IR root，不得开始重复 ordered-interval family 或 terminal paths，
   也不得逐规则复述设计理由。本阶段完成后立即以读取 `checkpoint 02` 结束。
4. `checkpoint 02` 返回后，按已确认的有序位置矩阵建立一个版本化 ordered-interval family，记录位置、
   event/field 映射、固定依赖、受控文本槽与三条 family path 元数据；同时按最终声明顺序划分 literal
   rule/path segments。机械确认 compiler 会产生完整 165-rule closure 与九条 paths，但不得在 IR、thinking
   或正文中显式携带、展开、序列化或复述 144 个重复 rule objects。在本阶段末尾只对 blueprint 完成
   一次递归源映射扫描，不执行第 9.1 或 9.2 节，然后以读取 `checkpoint 03` 结束。
5. `checkpoint 03` 返回后，先执行 verification reference 第 9.1 节的逐引用闭包检查且仅执行一遍，
   再执行第 9.2 节的正向 witness 检查且仅执行一遍。两项检查都只保留紧凑的内部通过/失败状态，
   不得逐规则输出清单或 witness；任一失败时立即停止并请求澄清，通过时确认 blueprint 足以在一次
   StructuredOutput tool input 中物化完整紧凑 IR，且此前从未物化过该 IR root。
   读取 `checkpoint 04` 前，必须把发起提示和 provider 暴露的 `StructuredOutput`
   schema 中所有 required property、类型/const/enum 与精确 cardinality 逐项应用到待提交对象；任一项
   未满足时立即停止，不得读取 checkpoint 或提交。通过后再以读取 `checkpoint 04` 结束。不得构造 JSON
   字符串或手工序列化。
6. `checkpoint 04` 返回后，第十个且最后一个工具调用必须是唯一一次 `StructuredOutput`。此 tool call
   是完整 `GenerationBlueprint` v1 根 plain object 的第一次且唯一一次 materialization：直接提交 compact
   IR，不得把 144 条 family rules 或三条 family paths 显式展开进 tool input，不得先在 thinking/正文中
   生成第二份对象，不得包外层字段，也不得改成字符串。必须先在同一 assistant response 内把完整 tool arguments 组装好，
   然后才发出 tool call；`StructuredOutput` 不是用来打开待填充容器的交互步骤，零属性调用无法在
   tool result 后补齐，绝对不得先发占位调用再提交完整对象。`checkpoint 04` 提供与 provider schema
   等价的 typed argument frame；其中大写占位符只表示从 blueprint 机械填入的值，不得原样进入参数。
   必须在发出调用前把该 frame 全部实例化。Test Flow CLI
   的冻结 workflow schema 对协议已解析 IR 的四个根字段、`spec`、`verification`、literal segments、
   family kind/version 和声明的精确 cardinality 做机械校验。可信 wrapper 对 IR 与 terminal 回显做
   byte-equivalent audit，在内存中确定性展开后调用原 loader/validator 深验；只有通过时才按递归 key
   排序的 canonical JSON、唯一末尾 LF create-only 原子写入
   `workspace/output/generation-spec.json`，并封存 size 与 SHA-256。转换 Agent 不得调用 `Write`、
   `Edit`、`Bash`，不得自行序列化或创建文件。必须等待这次 `StructuredOutput` 的 tool result 明确成功，
   再生成唯一的 terminal assistant response；该 response 的完整内容必须是精确 ASCII sentinel `DONE`，
   不得带引号、Markdown、前后空白、标点或其他文本。`DONE` 不得进入 tool input；发出它后不得再生成
   任何 text、tool call 或 turn，尤其不得第二次调用 `StructuredOutput`。`StructuredOutput` 不是 schema
   discovery 或 validation probe；禁止用零属性 root、partial object、trial input 或 probe input 探索 schema、
   required 字段或 cardinality。第一次调用必须已经是完整且满足全部已知 schema 约束的唯一提交；若该
   调用返回错误，立即停止，不得修补后重试或第二次调用 `StructuredOutput`。

从 Skill 到最终 `StructuredOutput` 的每个状态只能前进一次；禁止重读任何合同或 checkpoint。澄清已固定的有序
位置矩阵、rule family、公式、dependency 和 terminal branch 是机械展开输入，不得重新设计、压缩或
替换其语义。所有提交前语义保真义务在前三个构造阶段随字段增量完成，不得在 checkpoint 之后从头
重复一轮全对象设计。

### StructuredOutput 前语义保真检查

在对紧凑 GenerationBlueprint 执行唯一最终 `StructuredOutput`、并由可信 compiler 投影为 GenerationSpec
前，对照 Wiki 与已确认澄清逐项检查：

1. 枚举所有带义务、禁止、允许、条件、限制、可能性或风险后果的陈述，不得只保留机械规则所需片段。
2. 影响是否可安全判断或采取行动的内容写入 `judgement_rules`；必须向最终用户展示的警示、限制或
   风险后果写入 `output_requirements`。同一陈述兼具两种作用时必须双落点，不能用其中一个替代另一个。
3. 保留原文的否定方向、条件与适用范围、确定性/可能性强度及风险后果；不得把“可能”提升为“必然”，
   也不得把禁止、例外或未知改写成肯定结论。
4. 默认只要求语义等价，不要求逐字复制；只有输入明确要求固定措辞、原文引用或逐字保留时才逐字写入。
5. 为每条上述陈述确认源文本到目标字段的映射；无法确定落点或语气强度时先澄清，不得静默省略。
6. 在唯一最终 `StructuredOutput` 前，递归遍历待提交 GenerationBlueprint 的 literal `spec`、literal
   verification segments、family text/name slots 及其确定性最终投影，检查每一个业务字符串值。为其中
   每项语义及限定确认到标记外正文或权威澄清的具体源映射。任一值含旁注标记、旁注独有的
   逐字或独特片段，或复制、改写、概括外部来源未独立支持的旁注内容，立即丢弃整份草稿；最多允许
   一次从标记外正文与权威澄清重新构造并重新递归检查，不能就地删改命中字段。该次复检仍失败时
   立即停止并请求澄清，不得再次重构或调用 `StructuredOutput`。语义重叠且源映射独立支持完整语义及
   限定时，不得因旁注重复而删除合法事实。复检通过前不得调用 `StructuredOutput`。
7. 一个有序多成员事件承载有限历史且目标可能位于任意成员时，必须先确认顺序方向、记录完整性和
   目标身份，再为每个允许位置提供等价的目标匹配；只有来源明确保证目标固定在某一位置时，才可把
   selector 绑定到单一位置字段。不得把测试样例中的位置当成业务不变量。
8. 用多个区间解释目标区间时，正时长交集只证明单个贡献者，不能证明完整解释。COMPLETE 必须机械
   证明区间并集覆盖目标起点、终点且中间没有未解释空隙；PARTIAL 必须保留已确认交集和未覆盖区间。
   当前白名单规则无法安全表达并集覆盖时，停止生成该 COMPLETE 路径并请求澄清或合同扩展，禁止用
   单个区间、持续时长之和、记录相邻或自然语言断言近似替代。

### StructuredOutput 前机器引用闭包检查

语义保真检查通过后、唯一最终 `StructuredOutput` 前，按声明顺序构造并核对以下只读符号表：INPUT Requirement
名称集、Role label 集、Anchor label 集、policy ID 集、`event_id -> field 名称集`、已见 rule ID 集。

1. 对每个 extractor，逐项确认 anchor、policy、policy key、selector field、`timestamp_field` 与
   `group_by` 都存在于相应符号表；selector 的 `USER_FACT` 必须命名 INPUT Requirement。
2. 对每条 rule 递归遍历 `parameters` 和 NumericExpression，把每个字段引用按 verification
   reference 第 9.1 节列为内部清单。每个 event 必须命名 extractor；每个 `(event, field)` 必须满足
   `field ∈ event_id -> field 名称集`，字段即使存在于另一个 event 也不能借用；每个
   `FACT`/`USER_FACT` 必须命名 INPUT Requirement；role 必须已声明且与被引用 event 的 anchor 一致。
   特别是 FIELDS_EQUAL/Equality 的各 member 字段名不要求相同；每个 member 必须使用自己 event
   实际声明的字段，禁止为了表达“相等”把第一侧字段名复制到第二侧。
3. `depends_on` 只能使用该 rule 之前的已见 rule ID；`remediation_requirements` 只能使用
   `MISSING_ONLY` Requirement。检查完成后再把当前 rule ID 加入已见集合。
4. 每个 terminal condition term 必须命名已声明 rule，并保留既定的前序与可达性检查。
5. 对每个非 fallback `COMPLETE|PARTIAL` path，用标记外 Wiki 正文或权威澄清中的稳定日志消息体
   构造至少一个内部正向 witness。把原始消息体逐条代入实际 `line_pattern` 与 `match_mode`，再检查
   多行顺序/间隔/group、selector 与已确认事实、event count，以及按声明顺序执行后的机械依赖状态。
   path 所需的 event 必须实际抽取为非零；所需机械 rule 必须能够正向成立；所需
   `SEMANTIC_CAUSALITY` 不得因缺失 event 或非正向依赖而带 issue。尤其是 `FIELDS_EQUAL`，witness
   必须存在一个 occurrence tuple，使每个 Equality 的 member **值**相等；字段引用闭合但样例值
   不相等不能算通过。

发现任何缺失、拼写漂移或跨 event 借用 field 时不得调用 `StructuredOutput`，并立即停止请求澄清。此有界转换
不得只修正引用后再从第 1 步重新完整核对，也不得靠 validator 报错后猜测或继续写出。

正向 witness 只使用当前 Wiki 标记外正文与权威澄清，不读取仓库测试、oracle 或实现，也不把内部
witness 写进 GenerationSpec。不能只证明 JSON 可加载或 Rule DAG 名称可达：若实际消息体不匹配
extractor、selector 过滤掉目标 event、event count 为零、依赖会得到 `FAIL|UNKNOWN|NOT_APPLICABLE`，
或 Equality 找不到同时满足的 occurrence tuple，就不得调用 `StructuredOutput`。源材料不足以构造某条非 fallback
path 的正向 witness 时先请求澄清，禁止伪造日志或假定字段值。

verification contract v2 使用：

- `observation_policies[]`：首版仅 `SUPPRESSION|RATE_LIMIT`，显式 scope、key、窗口和边界；
- `event_extractors[]`：一个或多个有序成员、`FULL_LINE|SEARCH`、命名字段、
  `STRING|INTEGER|TIMESTAMP`、单位/clock domain、selector、group_by、行间隔和 min/max；
- `rules[]`：EVENT_COUNT/PRESENT、时间窗、事实比较、同/跨 anchor 关联、角色、顺序、白名单
  数值表达式与 SEMANTIC_CAUSALITY；
- `terminal_paths[]`：按顺序匹配 DNF 条件，结果为 `COMPLETE|PARTIAL|NONE`，最后必须有无条件
  NONE fallback。COMPLETE/PARTIAL 路径必须包含至少一个 SEMANTIC_CAUSALITY PASS。

多个 Logparse anchor 表示参与诊断的不同贡献者时，为每个贡献者声明唯一 Role，并让 Role label
与对应 anchor label 一致。Logparse Skill 至少有一个 REQUIRED role；非 Logparse Skill 使用空
`roles`。role label 使用 lower snake case，以便稳定生成扁平字段。每条
SEMANTIC_CAUSALITY 只依赖该结论必要且能够同时成立的机械前提。对每个 COMPLETE/PARTIAL 的每个
DNF branch，递归展开全部 `depends_on`，确认规则结果在当前观测策略、时钟容差和前序 path 下可同时
满足；禁止把其他原因分支的 semantic rule 或与本分支矛盾的 PASS/FAIL/UNKNOWN 条件串入当前路径。

数值表达式只使用 FIELD/FACT/CONST、ADD/SUBTRACT、MULTIPLY_CONST 和 CONVERT；禁止任意代码。
跨 clock domain 的比较必须显式写 `clock_tolerance_ms`，框架无默认。进程局部 ID 不得当作全局
唯一键，应通过 selector/group/join 与进程实例、anchor 或生命周期组成复合身份。

存在 lossy policy 时，正向 presence 仍有效；absence 和有上界的 count 只能是 UNKNOWN/下界。
固定快照之外不补历史日志，不等待未来日志，也不启动监控。

## 生成与校验

```text
python scripts/generate_diagnosis_skill.py --spec <generation-spec.json> --output-root <parent>
python scripts/generate_diagnosis_skill.py --wiki <agent-authored-wiki-with-v6-fence.md> --output-root <parent>
python scripts/validate_generated_skill.py <generated-skill-dir>
```

同一 id/version 的不同语义禁止覆盖；明确升级版本时才使用
`--replace-different-version`。validator 必须确认 Canonical manifest、v6 marker、profile 快照及
哈希、作者确认元数据、旁注不泄漏、
机器块逐字一致、Agent 不生成公开用户产物，并验证至少一个与当前业务异构的 Skill 没有字段泄漏。

## Candidate 与验收

Agent 重算全部规则并选择第一条 terminal path。COMPLETE/PARTIAL 可提出 Candidate；NONE 不可。
Candidate 必须写 resolution/path，结构化 `causal_factors`、`candidate_factors`、
`excluded_factors`，以及四态 completion mapping。每个 factor 绑定 Evidence 与 supporting rule IDs，
允许多个共同贡献因素。独立 Reviewer 复核相同 Skill、固定 Candidate 和原始证据；PASS 表示候选
对其声明的完整度真实且完整。

Agent 只写并封存 `output/job_outcome.draft.json`，禁止创建 USER_RESULT、ZIP 或归档请求。服务端
退出后重读证据、生成权威 Outcome/audit/用户结果；Review PASS 后 COMPLETE 进入 RESOLVED，
PARTIAL 进入 PARTIALLY_RESOLVED，二者均公开服务端 JSON 与 ZIP。

至少验收：一个多行/重复/多因素 COMPLETE；一个受抑制与时钟容差限制的 PARTIAL；一个 NONE；
两个中性异构规范；旁注剥离；业务 canary 只存在自包含 case root；分阶段 INPUT/ATTACHMENT 和
initial fact 精确名称过滤不回归。所有仓库测试只经 `tools/test-flow/run.ps1|run.sh`。
