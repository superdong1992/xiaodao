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

构造 JSON 前必须完整读取 [GenerationSpec v6 精确参考](references/generation-spec-v6-reference.md)
和 [verification contract v2 精确参考](references/verification-contract-v2-reference.md)。它们是转换
Agent 可使用的自包含格式合同；不要读取 generator、validator、Runtime 或测试源码来反推格式。
[wiki-template.md](references/wiki-template.md) 只演示无 Logparse 的最小对象，
[neutral-logparse-generation-spec-v6.json](references/neutral-logparse-generation-spec-v6.json) 只演示
复杂 Logparse 结构。示例中的 identity、名称、文本、阈值和策略均不是默认值，禁止复制到当前
业务产物；所有业务值必须来自当前 Wiki 与已确认澄清。

按上述合同形成独立 JSON，或把完全相同的对象放入转换 Agent 的工作 Wiki 中唯一
`## GenerationSpec v6` fence。该 fence 是 Agent 的中间机器产物，不是要求 Wiki 作者填写的
格式。requirements、logparse_plan 和 verification_contract 是唯一机器事实源。

### Write 前语义保真检查

在对 GenerationSpec 执行唯一最终 `Write` 前，对照 Wiki 与已确认澄清逐项检查：

1. 枚举所有带义务、禁止、允许、条件、限制、可能性或风险后果的陈述，不得只保留机械规则所需片段。
2. 影响是否可安全判断或采取行动的内容写入 `judgement_rules`；必须向最终用户展示的警示、限制或
   风险后果写入 `output_requirements`。同一陈述兼具两种作用时必须双落点，不能用其中一个替代另一个。
3. 保留原文的否定方向、条件与适用范围、确定性/可能性强度及风险后果；不得把“可能”提升为“必然”，
   也不得把禁止、例外或未知改写成肯定结论。
4. 默认只要求语义等价，不要求逐字复制；只有输入明确要求固定措辞、原文引用或逐字保留时才逐字写入。
5. 为每条上述陈述确认源文本到目标字段的映射；无法确定落点或语气强度时先澄清，不得静默省略。
6. 在唯一最终 `Write` 前，递归遍历待写 GenerationSpec 的所有对象和数组，检查每一个字符串值。
   为其中每项语义及限定确认到标记外正文或权威澄清的具体源映射。任一值含旁注标记、旁注独有的
   逐字或独特片段，或复制、改写、概括外部来源未独立支持的旁注内容，立即丢弃整份草稿；最多允许
   一次从标记外正文与权威澄清重新构造并重新递归检查，不能就地删改命中字段。该次复检仍失败时
   立即停止并请求澄清，不得再次重构或 `Write`。语义重叠且源映射独立支持完整语义及限定时，不得
   因旁注重复而删除合法事实。复检通过前不得 `Write`。

### Write 前机器引用闭包检查

语义保真检查通过后、唯一最终 `Write` 前，按声明顺序构造并核对以下只读符号表：INPUT Requirement
名称集、Role label 集、Anchor label 集、policy ID 集、`event_id -> field 名称集`、已见 rule ID 集。

1. 对每个 extractor，逐项确认 anchor、policy、policy key、selector field、`timestamp_field` 与
   `group_by` 都存在于相应符号表；selector 的 `USER_FACT` 必须命名 INPUT Requirement。
2. 对每条 rule 递归遍历 `parameters` 和 NumericExpression。每个 event 必须命名 extractor；每个
   `(event, field)` 必须满足 `field ∈ event_id -> field 名称集`；每个 `FACT`/`USER_FACT` 必须命名
   INPUT Requirement；role 必须已声明且与被引用 event 的 anchor 一致。
3. `depends_on` 只能使用该 rule 之前的已见 rule ID；`remediation_requirements` 只能使用
   `MISSING_ONLY` Requirement。检查完成后再把当前 rule ID 加入已见集合。
4. 每个 terminal condition term 必须命名已声明 rule，并保留既定的前序与可达性检查。

发现任何缺失、拼写漂移或跨 event 借用 field 时不得 `Write`。只修正引用后从第 1 步重新完整核对
一次；若仍不闭合，立即停止并请求澄清，不得靠 validator 报错后再猜测或继续写出。

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
