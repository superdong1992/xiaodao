---
name: wiki-to-diagnosis-skill
description: 将非敏感故障定位 Wiki 转换为通用 Problem Locator Diagnosis Skill；声明部署范围、业务 requirements、Logparse 映射、事件提取器和机器验证规则，生成并校验 schema v4 diagnosis-skill.json 与 SKILL.md。用于新建或升级 diagnose-* Skill。
---

# Wiki to Diagnosis Skill v4

本 Skill 负责业务规则生成，不修改全局 DIAGNOSE output contract，也不把某个 Fixture
的字段提升为通用协议。生成器固定为 `4.0.0`，输入规范为 `GenerationSpec v4`，输出
Skill 从 `4.0.0` 起，manifest 为 schema v4。

## 开始前确认

先阅读 Wiki，只确认下列会改变生成语义的信息：

1. Skill id、capability、标题、摘要、范围、目标版本，以及明确的
   `deployment_scope=PRODUCTION|TEST_ONLY`。
2. `requirements[]`：每项的 name、`INPUT|ATTACHMENT`、
   `INITIAL|AFTER_LOGPARSE`、用户提示、S00 原生 constraints 和
   `supplement_policy=NONE|MISSING_ONLY`。
3. 是否使用 Logparse。若使用，确认归档 requirement（可为 null）、problem time 的
   value binding、按顺序排列的 anchors 及各字段的 `USER_FACT|SKILL_FIXED` binding。
4. 仅当需要非默认产品时确认 `logparse_product`；默认产品直接省略。
5. `verification_contract`：UTF-8 整行事件提取器、无默认值的普通时间窗、事实/角色/
   关联/顺序机器规则，以及由 Specialist 和 Reviewer 独立判断的语义因果规则。
6. 分析步骤、时间特征、判定规则、输出要求和假设。

不要询问 Content-Type。Logparse 归档格式由平台固定：
`.gz/.tar.gz/.tgz -> application/gzip`、`.zip -> application/zip`、
`.tar -> application/x-tar`。

所有 requirement 都是必需项，不提供 optional 参数。旧 `custom_parameters` 必须显式
转换成 INPUT requirement 并指定 stage、prompt 和 constraints；空集合表示不添加任何
自定义或默认参数。禁止根据示例自动补业务字段。

## 边界

- `requires_logparse` 只控制工具绑定，不等价于 RPC、固定参数组、日志附件或 parse 后补参。
- `LOGPARSE_RESULT` 不能满足 requirement，只能成为 Evidence、Finding 或 proposed fact。
- `requires_logparse=false` 时，`logparse_plan=null`，roles 可为空，module 可为 null，
  且禁止 AFTER_LOGPARSE requirement。
- 每阶段最多一个 ATTACHMENT；AFTER_LOGPARSE 只允许 INPUT。
- Logparse product 省略表示上游 `default`。Runtime 记录有效值，但 Broker 不传
  `--product`；非默认值才显式传入。
- 存在 AFTER_LOGPARSE 缺参时，生成 Skill 必须用
  `state_delta.add_evidence_bindings` 接收必要 Evidence；新 LOGPARSE Evidence 通过
  `artifact_proposal_key` 绑定 broker 返回的 LOGPARSE_RUN，使续跑复用该运行。
  仅生成 proposal、Finding 或文字说明不构成接收，也不得在续跑时重新 parse。

## 构造 GenerationSpec v4

优先依据 [wiki-template.md](references/wiki-template.md) 在 Wiki 的
`## GenerationSpec v4` JSON fence 中形成完整对象。也可把同一对象保存为独立 JSON。
requirements、logparse_plan 与 verification_contract 是唯一机器事实源；`SKILL.md` 和
`diagnosis-skill.json` 均从它渲染，不维护第二套业务字段。

value binding 只有两种形状：

```json
{"source":"USER_FACT","name":"incident_time"}
{"source":"SKILL_FIXED","value":"database"}
```

INPUT constraints 逐字使用 S00：`value_type=STRING`、`min_utf8_bytes`、
`max_utf8_bytes`、`pattern`、`allowed_values[]`。普通 ATTACHMENT constraints 使用
`allowed_content_types[]`、`min_count`、`max_count`。若附件被
`logparse_plan.attachment_requirement` 引用，作者侧只声明 `min_count` 与 `max_count`；
不要询问或填写 `allowed_content_types`，生成器会在规范化结果和最终 manifest 中自动注入
`["application/gzip","application/zip","application/x-tar"]`。为兼容旧规范，显式提供同一
固定数组仍可接受，其他值必须拒绝。

每个事件提取器必须声明唯一 lower-snake id、Logparse anchor、`^...$` UTF-8 单行正则、
RFC3339 毫秒 UTC 时间命名组、业务字段命名组和 `EXACTLY_ONE` 基数。规则按声明顺序组成
DAG，kind 只能是 `EVENT_PRESENT`、`EVENT_TIME_WINDOW`、`FACT_FIELD_EQUALS`、
`ROLE_COVERAGE`、`CROSS_ROLE_CORRELATION`、`EVENT_ORDER` 或
`SEMANTIC_CAUSALITY`。时间窗必须逐条声明 before/after 毫秒及上下边界开闭语义；不提供
默认窗口。本版本禁止 suppression、rate-limit 或采样字段，将其保留为后续扩展。

## 生成与校验

从独立 spec 生成：

```text
python scripts/generate_diagnosis_skill.py --spec <generation-spec.json> --output-root <skill-dir-parent>
```

从含规范 fence 的 Wiki 生成：

```text
python scripts/generate_diagnosis_skill.py --wiki <wiki.md> --output-root <skill-dir-parent>
```

只有明确提升目标版本时才使用 `--replace-different-version`。同一 id/version 的内容
发生变化必须拒绝，不能原地覆盖语义。输出目录只能包含 `SKILL.md` 和
`diagnosis-skill.json`。

随后运行：

```text
python scripts/validate_generated_skill.py <generated-skill-dir>
```

validator 必须确认 Canonical manifest、schema/version/deployment scope、
requirements/logparse_plan、verification_contract、SKILL 内嵌机器块逐字一致、Agent 不生成
公开用户产物，以及非 RPC Skill 没有 RPC Fixture 字段泄漏。

## 验收

至少用三个异构规范做前向测试：

- RPC：四个 INITIAL INPUT、一个归档、一个 AFTER_LOGPARSE INPUT、两个 anchors、
  显式非默认 product。
- 数据库死锁：不同字段名、一个 anchor、省略 product，验证有效默认值。
- 无日志人工排查：无 module、roles、attachment、logparse 和后补阶段。

检查每个生成 manifest 的 requirement 集合精确隔离；错误场景字段不得出现在其他 Skill。
生产发布目录必须至少包含一个 `PRODUCTION` Skill，并不得包含 `TEST_ONLY` Skill；测试
harness 只能通过显式内部开关加载 `TEST_ONLY` fixture。

生成 Skill 形成 Candidate 时，Agent 禁止提出或写入 `USER_RESULT`、
`USER_RESULT_ARCHIVE`、`diagnosis-result.json`、`result.zip` 或归档请求。Runtime 在 Agent
退出后重新验证权威证据；DIAGNOSE 草稿通过验证后由服务端立即生成并持久化用户产物，仅在独立 Review PASS 后开放公开下载。

生成的 Skill 必须要求 Agent 写 `output/job_outcome.draft.json` 并以
`problem-locator-seal-outcome-draft` 封存；正式 Outcome 只能由 Agent 退出后的服务端验证器
生成。禁止让 Skill 把 Agent 自报结论描述成服务端已验证事实。
