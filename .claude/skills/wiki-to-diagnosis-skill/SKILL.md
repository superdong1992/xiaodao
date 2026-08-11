---
name: wiki-to-diagnosis-skill
description: 将普通 UTF-8 故障定位 Wiki 转换为通用 Problem Locator 专用 Diagnosis Skill；识别作者旁注，声明 requirements、Logparse 映射、事件集合、观测限制、机器规则和 COMPLETE/PARTIAL/NONE 终态路径，生成并校验 manifest schema v5。用于新建或升级 diagnose-* Skill。
---

# Wiki to Diagnosis Skill v5

本 Skill 把人编写的业务定位 Wiki 理解为一份明确、可审计的 `GenerationSpec v5`，再调用确定性
generator 生成恰好两个文件：`SKILL.md` 与 `diagnosis-skill.json`。自然语言理解属于当前转换
Agent；脚本只做严格规范化、渲染与校验，不用启发式 NLP 猜规则。

业务名、日志消息、版本、协议、阈值、原因排序和模块策略只能进入生成 Skill 或自包含业务用例，
不得写入 Problem Locator 通用源码、公共 output contract 或 Test Flow 配置。

## 输入与旁注

输入可以是普通 Markdown，不要求作者手写 JSON、完整平台日志前缀或正则。`(# ... #)` 与
`（# ... #）` 是作者给转换 Agent 的元旁注：可以帮助理解匿名化、简写和特殊边界，但不得复制
到 GenerationSpec 的业务文本、生成 Skill、manifest 或最终用户结果。括号不配对时停止并请
作者修正。

作者给出的稳定日志消息体可以生成 `SEARCH` 定位器；只有作者明确给出完整行合同才使用
`FULL_LINE`。Wiki 只描述超长日志“包含哪些字段”而没有稳定文本时，把它留作语义证据要求，
不要伪造正则。平台统一前缀不是作者必填信息。

## 只确认会改变语义的信息

先完整阅读 Wiki，再只询问缺失且会改变产物的问题：

1. Skill id/capability/版本、`PRODUCTION|TEST_ONLY` 和定位范围；
2. requirements 的 name、`INPUT|ATTACHMENT`、阶段、提示、约束和补充策略；
3. Logparse archive、problem time、anchors 及 USER_FACT/SKILL_FIXED binding；
4. 依赖“日志缺失”分支的 observation policy 与跨时钟容差；
5. COMPLETE、PARTIAL、NONE 各自需要哪些规则结果，以及因素/排除项如何绑定证据。

若结论只使用正向日志，不因未知抑制策略阻塞。若结论依赖 absence，而 Wiki 未声明模块策略，
必须询问；作者不知道时，让相关规则得到 UNKNOWN，不能发明“无抑制”。模块默认“多数日志受
抑制、少数明确无抑制”时，应把默认策略展开成每个受影响 event 的 policy 引用，例外 event
使用空引用。多个策略可以叠加。

不要询问日志归档 Content-Type。平台按后缀固定映射 `.gz/.tar.gz/.tgz`、`.zip`、`.tar`。

## Requirements 与工具边界

所有 requirement 都是必需项，不存在 optional 参数。旧 `custom_parameters` 必须显式转成
INPUT requirement；空集合不添加任何默认业务字段。

- `requires_logparse` 只控制工具绑定，不代表 RPC 或固定参数组。
- `LOGPARSE_RESULT` 只能形成 Evidence/Finding/proposed fact，不能满足 USER_FACT。
- 每阶段最多一个 ATTACHMENT；AFTER_LOGPARSE 只允许 INPUT。
- 一个等待轮次可以同时请求缺失 INPUT 和 ATTACHMENT，保持两个 ID 数组与 requirement 顺序。
- parse 后等待补参时，必须用 `state_delta.add_evidence_bindings` 接受要跨 Job 保留的 Evidence，
  并让它绑定同一 `LOGPARSE_RUN`；续跑只复用正式运行，不重新 parse。

## GenerationSpec v5

按 [wiki-template.md](references/wiki-template.md) 形成独立 JSON，或把完全相同的对象放入转换
Agent 的工作 Wiki 中唯一 `## GenerationSpec v5` fence。该 fence 是 Agent 的中间机器产物，
不是要求 Wiki 作者填写的格式。requirements、logparse_plan 和 verification_contract 是唯一
机器事实源。

verification contract v2 使用：

- `observation_policies[]`：首版仅 `SUPPRESSION|RATE_LIMIT`，显式 scope、key、窗口和边界；
- `event_extractors[]`：一个或多个有序成员、`FULL_LINE|SEARCH`、命名字段、
  `STRING|INTEGER|TIMESTAMP`、单位/clock domain、selector、group_by、行间隔和 min/max；
- `rules[]`：EVENT_COUNT/PRESENT、时间窗、事实比较、同/跨 anchor 关联、角色、顺序、白名单
  数值表达式与 SEMANTIC_CAUSALITY；
- `terminal_paths[]`：按顺序匹配 DNF 条件，结果为 `COMPLETE|PARTIAL|NONE`，最后必须有无条件
  NONE fallback。COMPLETE/PARTIAL 路径必须包含至少一个 SEMANTIC_CAUSALITY PASS。

数值表达式只使用 FIELD/FACT/CONST、ADD/SUBTRACT、MULTIPLY_CONST 和 CONVERT；禁止任意代码。
跨 clock domain 的比较必须显式写 `clock_tolerance_ms`，框架无默认。进程局部 ID 不得当作全局
唯一键，应通过 selector/group/join 与进程实例、anchor 或生命周期组成复合身份。

存在 lossy policy 时，正向 presence 仍有效；absence 和有上界的 count 只能是 UNKNOWN/下界。
固定快照之外不补历史日志，不等待未来日志，也不启动监控。

## 生成与校验

```text
python scripts/generate_diagnosis_skill.py --spec <generation-spec.json> --output-root <parent>
python scripts/generate_diagnosis_skill.py --wiki <agent-authored-wiki-with-v5-fence.md> --output-root <parent>
python scripts/validate_generated_skill.py <generated-skill-dir>
```

同一 id/version 的不同语义禁止覆盖；明确升级版本时才使用
`--replace-different-version`。validator 必须确认 Canonical manifest、v5 marker、旁注不泄漏、
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
两个中性异构规范；旁注剥离；业务 canary 只存在自包含 case root；混合 INPUT+ATTACHMENT 和
initial fact 精确名称过滤不回归。所有仓库测试只经 `tools/test-flow/run.ps1|run.sh`。
