---
name: wiki-to-diagnosis-skill
description: 将非敏感故障定位 Wiki 转换为通用 Problem Locator Diagnosis Skill v3；声明业务 requirements、阶段和可选 Logparse 映射，生成并校验 schema v2 diagnosis-skill.json 与 SKILL.md。用于新建或升级 diagnose-* Skill。
---

# Wiki to Diagnosis Skill v3

本 Skill 负责业务规则生成，不修改全局 DIAGNOSE output contract，也不把某个 Fixture
的字段提升为通用协议。生成器固定为 `3.0.4`，输入规范为 `GenerationSpec v2`，输出
Skill 从 `3.0.0` 起，manifest 为 schema v2。

## 开始前确认

先阅读 Wiki，只确认下列会改变生成语义的信息：

1. Skill id、capability、标题、摘要、范围和目标版本。
2. `requirements[]`：每项的 name、`INPUT|ATTACHMENT`、
   `INITIAL|AFTER_LOGPARSE`、用户提示和 S00 原生 constraints。
3. 是否使用 Logparse。若使用，确认归档 requirement（可为 null）、problem time 的
   value binding、按顺序排列的 anchors 及各字段的 `USER_FACT|SKILL_FIXED` binding。
4. 仅当需要非默认产品时确认 `logparse_product`；默认产品直接省略。
5. 分析步骤、时间特征、判定规则、输出要求和假设。

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

## 构造 GenerationSpec v2

优先依据 [wiki-template.md](references/wiki-template.md) 在 Wiki 的
`## GenerationSpec v2` JSON fence 中形成完整对象。也可把同一对象保存为独立 JSON。
requirements 与 logparse_plan 是唯一机器事实源；`SKILL.md` 和
`diagnosis-skill.json` 均从它渲染，不维护第二套业务字段。

value binding 只有两种形状：

```json
{"source":"USER_FACT","name":"incident_time"}
{"source":"SKILL_FIXED","value":"database"}
```

INPUT constraints 逐字使用 S00：`value_type=STRING`、`min_utf8_bytes`、
`max_utf8_bytes`、`pattern`、`allowed_values[]`。ATTACHMENT constraints 使用
`allowed_content_types[]`、`min_count`、`max_count`；若该附件供 Logparse 使用，
allowed_content_types 必须按固定顺序等于
`["application/gzip","application/zip","application/x-tar"]`。

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

validator 必须确认 Canonical manifest、schema/version、requirements/logparse_plan、
SKILL 内嵌机器块逐字一致、结果 JSON/ZIP 约束，以及非 RPC Skill 没有 RPC Fixture 字段泄漏。

## 验收

至少用三个异构规范做前向测试：

- RPC：四个 INITIAL INPUT、一个归档、一个 AFTER_LOGPARSE INPUT、两个 anchors、
  显式非默认 product。
- 数据库死锁：不同字段名、一个 anchor、省略 product，验证有效默认值。
- 无日志人工排查：无 module、roles、attachment、logparse 和后补阶段。

检查每个生成 manifest 的 requirement 集合精确隔离；错误场景字段不得出现在其他 Skill。
生成 Skill 形成 Candidate 时必须同时生成 `diagnosis-result.json` 和受控
`USER_RESULT_ARCHIVE/result.zip`，后者由安装的 `problem-locator-pack-result` 创建并由
Runtime 对 Candidate 实际绑定日志逐字校验。
