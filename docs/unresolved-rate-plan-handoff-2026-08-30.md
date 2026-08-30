# 实施计划书:降低 Evidence V2 的 UNRESOLVED 率(归因基线 + 消除假分歧)

> **交付对象**:实施 Agent(零上下文)。本文自包含,不要求阅读此前的对话。
> **仓库**:`D:\code\xiaodao`,Problem Locator 5.0.0,Python 3.12,分支 `main`。
> **执行方式**:按 GOAL 0 → 1 → 2 → 3 顺序执行。每个 GOAL 有明确完成判据,未达成不得进入下一个。
> **最重要的一条**:遇到"停止条件"章节列出的任一情形,**立即停止并报告**,不要自行绕过。

---

## 0. 前置依赖:必须先完成 Fast E2E 测试收口

**本计划书不得在测试收口完成前启动。**

仓库当前处于 Evidence V2 Fast E2E 迁移中途(HEAD~1 提交名即 `wip: checkpoint Evidence V2 fast e2e migration`)。已知尚有四类未收口问题:

1. `formalization` 必须携带 Graph,堵住 event 与 hit 错配
2. Claude Fast 的历史字段映射成 production registration 的 slot/process 字段
3. Fast 去掉 Release Wiki/缓存依赖,并校验 registration 生成凭据
4. 去掉正测中的 oracle 反向喂答案,补 noise 误选负例,统一 marker 大小写/顺序规则

### 为什么必须先收口

- **GOAL 0 的基线依赖它**。本计划书要求先取得 `deterministic.full` PASS 作为对照基线。迁移中途要么拿不到,要么拿到的是"绿但空心"的基线。
- **第 1 项与本计划书的爆炸半径直接重叠**。`MethodsTerminalProjectionV2.confirmed_hit_refs` 由所选 event 机械派生。在 event/hit 错配未修的同时改变"哪些 event 被选中",两个变更会互相干扰,变成移动靶。
- **第 4 项的"oracle 反向喂答案"直接削弱本计划书的回归保护**。若正测从 oracle 反向取答案,绿色套件是自证的,GOAL 2 的回归安全网就是假的。
- **第 4 项的"统一 marker 大小写/顺序规则"与 GOAL 2 实施要点 A 冲突**。A 要求先确认 `supporting_event_refs` 的 plan-order 是否被强制;若顺序规则正在被改,这个确认就是对着将变的代码做的。

### 给测试收口方的协调约束(重要)

补 noise 误选负例时:

- **可以**继续按现有模式钉"两侧 event 集合**不相交**"的场景(如 `test_methods_evaluation_v2.py:406`),这类语义本计划书不改
- **不得**新钉"两侧 CONFIRMED、event 集合**部分重叠** → UNRESOLVED"的行为

部分重叠正是 GOAL 2 要修订的语义。此刻把它钉成测试,会与随后的修订直接冲突。该场景的行为留给 GOAL 2 定义。

### 已核实的两处状态偏差(收口方需自行复核)

以下两项在当前 `main` 上看起来至少部分已完成,与自述不符,应先复核再决定是否还需工作:

- `src/problem_locator/application/formalization.py:138` 已有 `graph: MethodEvidenceGraphV2` 参数(199、287 行另有 `evidence: MethodEvidenceGraphV2`)
- noise 误选负例已存在:`tests/deterministic/unit/runtime/test_methods_evaluation_v2.py:375` 与 `:406`,`tests/deterministic/unit/runtime/test_methods_outcome_v2.py:252` 与 `:288`

若实际缺口只在 Fast E2E 场景层而非核心层,应在报告中明确区分。

---

## 1. 背景与问题证据

Problem Locator 的 SPECIALIZED 诊断链路(Evidence V2)由服务端扫描冻结日志、生成 Evidence Graph 与 Evaluation Plan,再由 Specialist 和 Reviewer 两个隔离角色盲评,最后由服务端做共识裁决。模型只提交 `{evaluation_ref, verdict, supporting_event_refs, reason}` 数组,不产生权威结论。

这套设计几乎消灭了幻觉类失效,代价是共识裁决极度保守。预期真实失效模式是 **UNRESOLVED 率过高**,而非给错答案。

### 保守性的确切位置

> **注意:两侧 `supporting_event_refs` 必须逐字相等这一约束,在仓库中被强制了七处**(计算方 + 五个校验方 + 一个归因点),下方 位置 A / A2 / C / D / E / F / G。**七处必须同时改**,否则会在共识之后的终态构造阶段抛出看似无关的异常。这也是 GOAL 2 要求抽取单一纯函数的原因。
>
> 注意 E 与 F 是**同一条断言被复制在两个不同文件**(错误文本都是 `resolved role evaluations select different evidence events`)。改了其中一个另一个仍会抛,且异常信息完全一样——**看到这条异常第二次出现时,是还有一个文件没改,不是改错了**。
>
> 只改前三处的典型症状:共识算出来了,随后在终态构造时抛 `resolved role evaluations select different evidence events` 或 `resolved Methods Reviewer verdicts or events differ from confirmed refs`。**遇到这两条异常不要改方向,也不要削弱校验——是漏改了位置 D / E。**

**位置 A(计算方,真正产生 consensus)** —— `src/problem_locator/runtime/methods_evaluation_v2.py:195` 的 `resolve_method_consensus_v2`,判定在 214–227 行:

```python
first_blind = tuple(
    (item.evaluation_ref, item.verdict, item.supporting_event_refs)
    for item in first.evaluations
)
second_blind = tuple(
    (item.evaluation_ref, item.verdict, item.supporting_event_refs)
    for item in second.evaluations
)
verdicts = tuple(item.verdict for item in first.evaluations)
resolved = (
    first_blind == second_blind
    and "UNKNOWN" not in verdicts
    and "CONFIRMED" in verdicts
)
if not resolved:
    return MethodConsensusV2(
        plan_ref=plan.plan_ref,
        status="UNRESOLVED",
        confirmed_evaluation_refs=(),
        confirmed_method_ids=(),
        confirmed_event_refs=(),
    )
```

**位置 A2(校验方)** —— `src/problem_locator/domain/methods_state_v2.py`,函数 `_validate_consensus`(约 377 行起),独立重算同一判定:

```python
specialist_blind = tuple(
    (item.evaluation_ref, item.verdict, item.supporting_event_refs)
    for item in specialist.evaluations
)
reviewer_blind = tuple(
    (item.evaluation_ref, item.verdict, item.supporting_event_refs)
    for item in reviewer.evaluations
)
verdicts = tuple(item.verdict for item in specialist.evaluations)
resolved = (
    specialist_blind == reviewer_blind
    and "UNKNOWN" not in verdicts
    and "CONFIRMED" in verdicts
)
```

**位置 B** —— `src/problem_locator/contracts/models.py`(约 775 行),`MethodsTerminalProjectionV2.validate_terminal_projection`:

```python
else:
    if any(confirmed):
        raise ValueError(
            "non-resolved Methods terminal projection must clear confirmed refs"
        )
```

**位置 C** —— `src/problem_locator/contracts/methods_state_v2.py`(约 263 行),`MethodStateV2` 的模型校验器**独立重算**同一套期望值并比对:

```python
self.consensus.status != expected_status
or self.consensus.confirmed_evaluation_refs != expected_refs
or self.consensus.confirmed_event_refs != expected_event_refs
```

**位置 D** —— `src/problem_locator/contracts/models.py:~836`,`validate_methods_reviewer_terminal_v2`。RESOLVED 时断言终态的 `confirmed_event_refs` 等于 **Reviewer 自己**的 event refs 展平结果:

```python
reviewer_events = tuple(
    event_ref
    for _, verdict, event_refs in reviewer_verdicts
    if verdict == "CONFIRMED"
    for event_ref in event_refs
)
if (
    reviewer_confirmed != terminal.confirmed_evaluation_refs
    or reviewer_events != terminal.confirmed_event_refs
    or any(verdict == "UNKNOWN" for _, verdict, _ in reviewer_verdicts)
):
    raise ValueError(
        "resolved Methods Reviewer verdicts or events differ from confirmed refs"
    )
```

**位置 E** —— `src/problem_locator/contracts/methods_state_v2.py:~658`。显式要求两角色所选 event 完全相同:

```python
selected_event_refs = tuple(
    specialist_by_ref[item.evaluation_ref].supporting_event_refs
    for item in confirmed_plan
)
if any(
    reviewer_by_ref[item.evaluation_ref].supporting_event_refs != selected
    for item, selected in zip(confirmed_plan, selected_event_refs, strict=True)
):
    raise ValueError("resolved role evaluations select different evidence events")
expected_event_refs = tuple(dict.fromkeys(
    event_ref for selected in selected_event_refs for event_ref in selected
))
if expected_event_refs != consensus.confirmed_event_refs:
    raise ValueError("resolved consensus differs from selected evidence events")
```

> 交集是两侧的**真子集**,位置 D 与 E 都会因此抛异常。它们不是"顺带受影响",而是本次修订的**必改项**。

### 已确认的两项前置事实(不需要再验证)

`_validate_role_coverage`(`runtime/methods_evaluation_v2.py:158`)已经强制:

1. **`supporting_event_refs` 必须按 plan 中该 evaluation 的 event 顺序排列**
   ```python
   expected_supporting_order = tuple(
       ref for ref in planned.evidence_event_refs if ref in supporting_ref_set
   )
   if item.supporting_event_refs != expected_supporting_order:
       raise ValueError(...)
   ```
   → **交集保留任一侧顺序即等于 plan 顺序,不需要显式重排。**

2. **`supporting_event_refs` 只能引用本 evaluation 自己的 event**
   ```python
   if any(ref not in planned.evidence_event_refs for ref in item.supporting_event_refs):
       raise ValueError(...)
   ```
   → **跨 evaluation / 跨方法 event 混用结构上不可能**;两个子集的交集仍是同一 evaluation 事件集的子集。交集规则不会引入跨方法泄漏。

3. `MethodEvaluationOutputItemV2`(`contracts/methods_v2.py:509–517`)**禁止** `CONFIRMED` 携带空的 `supporting_event_refs`:
   ```python
   if self.verdict == "CONFIRMED" and not self.supporting_event_refs:
       raise ValueError("a confirmed evaluation requires supporting_event_refs")
   ```
   → **"交集为空必须判为真分歧"不是设计偏好,是合同强制**。空交集根本无法作为 CONFIRMED 发布,不存在"确认但无证据"这个选项。

### 三层保守性的性质不同

| 层 | 性质 | 本轮处置 |
|---|---|---|
| `supporting_event_refs` 参与相等判定 | **缺陷**。两角色都判 CONFIRMED、都认同原因成立,仅引用了不同证据子集就算"分歧",全案作废 | **本轮修(GOAL 2)** |
| `"UNKNOWN" not in verdicts` 全局门 | 设计矛盾:方法卡指示模型证据不足时返回 UNKNOWN,框架却因此作废全案 | **不动**,等归因数据 |
| 非 RESOLVED 时清空全部已确认 ref | 数据已算出,在投影边界被主动丢弃 | **不动**,等归因数据 |

### 为什么归因必须与修复并行

`_consensus_reason`(同文件约 340 行)当前把两种完全不同的情况都归为 `SPECIALIST_REVIEWER_DISAGREEMENT`:

- verdict 本身不同(真分歧)
- verdict 全同、仅 `supporting_event_refs` 集合不同(假分歧)

无法区分。而这正是归因最需要的一刀。两条线动的是同一个函数,因此并行。

### 必须先承认的不确定性

UNRESOLVED 归因目前是**零数据状态**——仓库中没有任何 `model-cert.json` 或真实模型运行记录。同样的症状也可能来自:

- `NO_MATCHING_METHOD_EVIDENCE`(marker 过度召回或激活过松)
- `*_SEMANTIC_INVALID` / `*_REPAIR_EXHAUSTED`(模型产不出合规数组)

这两种情况下放松共识规则**收益为零**。因此 GOAL 1(归因)的价值不低于 GOAL 2,且 GOAL 3 之后的方向必须由数据决定,不得由本文预设。

---

## 2. 仓库硬规则(违反将导致工作被拒)

摘自仓库根 `AGENTS.md`,必须遵守:

1. **修复前必须验证问题**。在修改代码"修复"问题前,必须先在当前工作区和当前版本确认问题确实存在,并优先通过最小复现取得证据。未确认前只允许只读调查。
2. **每个修复必须新增能直接复现原问题的测试**。泛化的全量 PASS 不能替代专项回归用例。
3. **修复登记**:完成后写入根目录 `FIXED_ISSUES.md`,含症状、受影响版本、根因、不可回归行为、修复历史、专项回归测试、最新 Test Flow verdict。
4. **verdict 引用只能在验证完成后写入**。除该元数据行外,不得在最后一次通过后继续修改交付字节。
5. **Test Flow 是唯一入口**:`tools/test-flow/run.ps1`(Windows)或 `run.sh`。不得绕过编排器拼装发布结论。
6. **不得为测试方便增加兼容层**、隐藏字段或客户端 Hook。
7. 活跃未完成事项进 `TODO.md`,不进 `FIXED_ISSUES.md`。

补充自 `design/wiki-diagnosis-generalization.md`:

8. 机器可校验的 schema、生成资产和运行时代码高于叙述性文档;**叙述文档必须随同一变更同步修正**。
9. 负向用例从生产生成的合法基线开始,每次只修改一个字段;删除关键校验时对应 mutation 必须失败。

---

## GOAL 0 — 解除 INFRA 阻塞,取得对照基线

### 背景

最新一次 `dev.default` 运行在 `repository.static` 阶段 BLOCKED,`failure_domain: INFRA`:

- `repo.compileall` → `PYTHON_312_TEST_RUNTIME_MISSING`
- `repo.uv-lock` → `UV_REQUIRED`

导致 `deterministic.affected` 与 `deterministic.full` 双双 `NOT_RUN`。**这不是代码缺陷**,是本机缺 CPython 3.12 与 `uv`。

### 步骤

1. 安装 CPython 3.12(`pyproject.toml` 要求 `>=3.12,<3.13`;3.13 会失败)
2. 安装 `uv`
3. 在仓库根执行:
   ```
   uv sync --frozen --all-groups
   uv lock --check
   ```
   不得顺带升级已锁定的 MCP、HTTP 或存储依赖。
4. 先看计划:
   ```
   tools\test-flow\run.ps1 --track dev --goal dev.default --plan-only
   ```
   确认无 admission blocker。
5. 实跑同一 Goal:
   ```
   tools\test-flow\run.ps1 --track dev --goal dev.default
   ```

> **Windows 路径提示**:若深层工作树触发 `MAX_PATH`,在 plan-only 与实跑两次命令中把 `TEST_FLOW_WINDOWS_SCRATCH_ROOT` 设为同一个绝对可写短目录。

### 完成判据

- `verdict.json` 中 `deterministic.full` 为 PASS
- 其中 `det.evidence-v2-core` 的 `core-verdict.json` 为 `status: PASS`
- 记录并在最终报告中引用:run ID、`source_snapshot_digest`、pytest 计数

**已知参考基线**(来自 2026-08-28 的一次历史运行,仅供对照,不得直接复用):
`core_cases.count = 55`,`pytest: 106 executed / 106 passed / 0 failures / 0 skipped`,`model_invocations = 0`。

### 停止条件

拿不到 `deterministic.full` PASS 时**停止**。不得在无基线的情况下修改判定逻辑——否则后续无法区分"我改坏了"和"本来就坏"。

---

## GOAL 1 — 归因仪表(内部记录,零公开合同变更)

### 目标

让每个 UNRESOLVED case 的成因可被聚合统计,特别是把 `SPECIALIST_REVIEWER_DISAGREEMENT` 拆成真假分歧。

### 硬约束

**公开合同一字不动**:

- `src/problem_locator/contracts/methods_reason_v2.py` 的 `MethodStateReasonCodeV2` Literal 联合类型 **不得增删成员**
- `METHOD_PUBLIC_REASON_TEXT_V2` **不得增删条目**
- `MethodsTerminalProjectionV2` 的字段与 `status` 枚举 **不得变更**
- `schemas/v2/contract-manifest.json` **不得出现任何漂移**(GOAL 1 是纯增量埋点,不应触碰任何被钉死的 `.py` 文件)

> 与 GOAL 2 的区别:GOAL 2 允许并且必须重算 manifest;**GOAL 1 不允许**。若 GOAL 1 的实现导致 manifest 需要重算,说明落点选错了,回到步骤 1 的可行路径。

区分结果**只写内部 append-only execution record**,不进任何公开投影(MCP / REST / Case)。这样归因不产生合同代价、不使冻结 registration 身份失效。

### 步骤

1. **落点已核实,按下述唯一可行路径实施**(不要在既有合同模型上加字段):

   **为什么不能加字段**:`schemas/v2/contract-manifest.json` 按 sha256 钉死了 `contracts/methods_state_v2.py`、`methods_v2.py`、`models.py`、`methods_reason_v2.py` 等 12 个 `.py` 文件;且 `ContractModel` 全局 `extra="forbid"`(`contracts/models.py:333`),`methods_records_v2.py:101` 每次写入还会用同一模型回读校验。**给 `MethodStateV2` 加一个内部字段 = 改动被钉死的字节 = manifest 漂移**,与"不动公开合同"的目标直接冲突。

   **唯一不触碰冻结字节的路径**:
   - `storage/execution_records.py` **不在** manifest 清单内(该清单不含 `storage/` 下任何文件)
   - `publish_audit_bytes`(`execution_records.py:507`)接受任意原始字节,但文件名必须在写死的 `_AUDIT_FILENAMES` frozenset(`execution_records.py:74-105`)内,否则 `ValueError("unsupported execution audit filename")`
   - 因此:在 `_AUDIT_FILENAMES` 中**新增一个全新文件名**(如 `methods-consensus-attribution-v2.json`),用一个**不注册进 `schemas/v2/`、不进 manifest** 的内部结构写入
   - 该 frozenset 不属于 `contracts/ports.py` 的 `ExecutionRecordStore` Protocol,只是这个适配器的实现细节,改动风险可控

   **注意语义**:`publish_audit_bytes` 的"append-only"指**幂等写一次后不可变**(同名文件再写会按字节比对,不同则 `IDEMPOTENCY_CONFLICT`),**不是**逐行追加的日志通道。每个 job 写一次完整归因记录即可。

   **不要改用** `diagnostics.log_event` 或 `journey.record_journey_event`:两者虽然字段自由,但都是尽力而为、允许静默丢失的观测通道(`diagnostics.py:102` 吞异常、`journey.py:307` 写失败会禁用整个 writer),且按进程级单文件滚动而非按 job 分区,不适合做需要精确聚合的归因底座。

2. 在 `domain/methods_state_v2.py` 的 `_consensus_reason` 中,除现有返回值外,额外产出内部子因枚举:

   | 子因 | 判定 |
   |---|---|
   | `UNKNOWN_PRESENT` | 任一项 verdict 为 `UNKNOWN` |
   | `VERDICT_MISMATCH` | 存在某项两侧 verdict 不同 |
   | `EVIDENCE_SET_MISMATCH` | 所有项 verdict 相同,但存在某项 `supporting_event_refs` 集合不同 |
   | `NO_CONFIRMED` | 两侧完全一致但无 `CONFIRMED` |

   注意现有实现的一处不对称:`_consensus_reason` 检查 `second.verdict == "UNKNOWN"`(只看 reviewer),而 `_validate_consensus` 检查 `verdicts`(取自 specialist)。**记录子因时两侧都要检查**,并在报告中说明这处不对称是否需要单独处理。

3. 每个 case 额外采集以下指标写入同一内部记录:

   | 指标 | 来源 |
   |---|---|
   | 公开 `reason_code` | 已有 |
   | 共识子因 | 本 GOAL 新增 |
   | N = evaluation 数 | `MethodEvaluationPlanV2` |
   | 每 evaluation 的 event 数 | Evidence Graph |
   | 激活方法数 | `graph.loaded_method_ids`,取 `len()` —— 在调用点直接可得 |
   | package 总方法数 | **调用点取不到**,见下方说明 |

   > **已核实的信息可达性缺口**:前三项(N、每 evaluation 的 event 数、激活方法数)在 `diagnosis_runtime.py:2732` 处由形参 `plan` / `graph` 直接可得。但 **package 总方法数取不到** —— `_evaluate_methods_reviewer_v2`(签名在 `diagnosis_runtime.py:2556-2567`)的形参里没有 `skill` / `assets`,该值在 `ResolvedSpecializedSkillV1.methods.methods` 上。需要给该方法**新增一个形参穿透**。这是纯代码改动,不涉及合同,但别以为能就地取到。

   > N 与激活率是关键:一致性难度随 N 上升,而 N 由 activation 精度决定。"共识太严"和"激活太松"会产生**完全相同的症状**,必须靠这两个指标区分。

4. 新增只读聚合脚本,从 execution records 统计上述分布。放在 `tools/` 下。**不得接入中央 Goal / Proof / Stage / Gate**——它是诊断工具,不产出 verdict(见硬规则 5)。

### 完成判据

- 三种共识子因在确定性 journey 产生的 execution records 上都能被区分出来
- 聚合脚本可跑通并输出分布
- `git diff` 显示 `methods_reason_v2.py`、`MethodsTerminalProjectionV2`、`schemas/` 均未变更
- `deterministic.full` 仍 PASS

---

## GOAL 2 — 消除假分歧(域逻辑,不动 wire schema)

### 先复现,再修(硬规则 1 与 2)

**第一步必须是写一个失败测试**,不是改代码:

- 构造两角色 verdict 逐项全一致、至少一项 `CONFIRMED`,**仅 `supporting_event_refs` 取了不同但有交集的子集**
- 断言当前实现得到 `UNRESOLVED`
- 该测试改动前必须 FAIL(即证明问题存在),改动后必须 PASS

落点参考:`tests/deterministic/unit/domain/test_methods_state_v2.py` 同目录,或 `tests/deterministic/unit/runtime/test_methods_evidence_v2.py`。与现有用例同风格。

### 目标语义

逐项比较从"三元组逐字相等"改为:

1. **verdict 必须相等**(不放松)
2. verdict 为 `CONFIRMED` 时:两侧 `supporting_event_refs` **交集非空**即算一致,发布**交集**
3. 交集为空 → 仍判真分歧(两侧指向不同事件,是实质分歧)
4. verdict 为 `REJECTED` / `UNKNOWN` 时:两侧仍必须为空数组(不放松)
5. `"UNKNOWN" not in verdicts` 与 `"CONFIRMED" in verdicts` 两个全局门 **保持不变**

该规则严格弱于原相等判定,且**永远不会确认只有单个角色看到的事件**——是移除假阴性,不是放松证据标准。

### 实施要点

**A. 排序 —— 已确认,不要再实现重排**

plan 顺序**已由 `_validate_role_coverage` 强制**(见上文"已确认的两项前置事实")。两侧的 `supporting_event_refs` 都已经是 plan 顺序,因此:

- 交集直接保留任一侧的相对顺序即可,天然等于 plan 顺序
- **不要**再写一次按 plan 重排的逻辑 —— 那是多余代码,且会掩盖将来 `_validate_role_coverage` 若被削弱时本该暴露的问题

`confirmed_event_refs` 跨 evaluation 展平后的去重沿用位置 E 现有的 `dict.fromkeys` 写法。

**B. 七处强制点必须同时改 —— 这是本 GOAL 最容易漏的部分**

| 位置 | 文件 | 角色 | 漏改的症状 |
|---|---|---|---|
| A | `runtime/methods_evaluation_v2.py:214–227` **及 237–260** | **计算方**:判定 + 构造 `confirmed_event_refs` | 交集规则根本没生效 |
| A2 | `domain/methods_state_v2.py:~388`(`_validate_consensus`) | 校验方,独立重算后比对 | `consensus differs from the two role evaluations` |
| C | `contracts/methods_state_v2.py:~263`(错误文本在 :269) | 校验方,再独立重算 | `consensus event refs differ from the two role evaluations` |
| D | `contracts/models.py:~836`(错误文本在 :858) | 断言终态 refs == Reviewer 自己的 refs | `resolved Methods Reviewer verdicts or events differ from confirmed refs` |
| E | `contracts/methods_state_v2.py:~658`(错误文本在 :671) | 断言两角色所选 event 完全相同 | `resolved role evaluations select different evidence events` |
| F | `runtime/methods_outcome_v2.py:~105`(错误文本在 :111) | **与 E 完全相同的断言,不同文件** | 同上,文本一模一样 |
| G | `domain/methods_state_v2.py:357–369`(`_consensus_reason`) | **归因**:用 `next()` 找第一个逐字不等项作 `diagnostic_evaluation_ref` | **不抛异常**,静默归因到错误的 evaluation |

> **位置 G 是最隐蔽的一处:它不会让任何测试因异常而红,只会让诊断 ID 指错。** 场景:计划有两项,第 1 项双方 CONFIRMED 且交集非空(新规则下算一致),第 2 项 verdict 真不一致。整体因第 2 项判 UNRESOLVED,但 `next()` 在第 1 项就命中"refs 不逐字相等",于是对外暴露的 `diagnostic_evaluation_ref` 指向第 1 项——一个实际上达成了一致的项。
>
> **G 与 GOAL 1 是同一个函数**。实施时两个 GOAL 在此交汇,请一并处理,不要分两次改同一段逻辑。

### 已独立排查、确认**不受影响**的区域(不要在这里浪费时间)

`runtime/methods_replay_v2.py`(只重放单侧)、`runtime/methods_records_v2.py`(纯 I/O)、`runtime/server_outcome_finalizer.py`、`runtime/outcome_publisher.py`、`runtime/user_results.py`、`application/outcome_submission.py`、`application/projection.py`、`application/audit_bundle_assembler.py`、`interfaces/*`(仅 OpenAPI 描述文字)均无双侧比较逻辑。

`runtime/methods_grounding.py` 与 `runtime/methods_outcome.py` 属于**旧 Methods V1 协议**(字段模型完全不同,无 Specialist/Reviewer 对称评估概念),与本次变更无关。

### 另有两处"单项构造"必须同改(不是校验,是结果正确性)

即使 A–F 的判定与聚合全部改成交集,以下两处仍会把 **Specialist 的全集**写进每条 confirmed evaluation 的 `evidence_event_refs` 字段:

| 文件:行 | 当前写法 |
|---|---|
| `contracts/methods_state_v2.py:~716` | `MethodConfirmedEvaluationV2(..., evidence_event_refs=selected, ...)`,`selected` 来自 :659 的 specialist 全量 |
| `runtime/methods_outcome_v2.py:~138` | `evidence_event_refs=selected_by_ref[item.evaluation_ref]`,同样是 specialist 全量 |

两者是同一问题的"校验侧"与"构造侧"。**漏改不会报错,但会发布 Reviewer 未认可的事件引用**——直接违背"只发布双方共同确认的证据"这一本次修订的核心意图。

> **位置 A 有两段,别只改一段**。214–227 是"是否 RESOLVED"的判定;237–260 才是构造 `confirmed_event_refs` 的地方,当前直接取 `first.evaluations`(即 Specialist)的 `supporting_event_refs` 展平。**这一段必须改成取交集**,否则判定放宽了但发布的仍是 Specialist 的全集。

**上表最后一列的四条异常都不表示交集方案有问题,只表示还有位置没改。遇到它们时继续补齐,不要改变方案方向,更不要削弱这些校验的其他断言。**

要求:把"两侧某 evaluation 是否达成一致 + 达成一致时的 confirmed event 集合"抽成 `contracts/` 下的**单一纯函数**,七处都调用它,不得各自重算。

> 分层约束:`runtime → contracts` 与 `domain → contracts` 均已存在且是单向的,反向不可。因此共享函数必须放在 `contracts/`,不能放 `domain/` 或 `runtime/`。

**C. 重算 contract-manifest —— 必做,且仓库内没有生成器**

改动 `contracts/models.py` 与 `contracts/methods_state_v2.py` 后,`schemas/v2/contract-manifest.json` 中这两条的 `sha256` 会失配。

- 校验点:`tests/deterministic/contracts/test_schema_snapshots.py:128` 逐条断言 `entry["sha256"] == hashlib.sha256(file_path.read_bytes()).hexdigest()`
- **已确认仓库内没有 manifest 生成器**,必须手工重算这两条 `sha256` 写回
- 文件必须保持 **canonical JSON 字节**(同文件 :97 断言 `is_canonical_json_bytes(raw)`)
- 不要增删 manifest 条目;`:131` 会检查它不含自身、`pyproject.toml`、`uv.lock`、`tests/`、`handoff/`

> **`test_schema_snapshots.py` 变红不表示改错了**,它只是提醒 manifest 还没重算。这是本轮唯一一个"预期会红、且正确修法是更新它而不是回退代码"的测试。

**D. 同步叙述文档**(硬规则 8)

- `design/wiki-diagnosis-generalization.md` 的共识表
- `README.md` 的 "Evidence V2、盲评与审计" 段

### 向后兼容:已持久化状态会失效 —— 必须先报告再决定

**已验证的证据链**(不是推测):

- `read_method_state_v2`(`runtime/methods_records_v2.py:257`)调用 `_read_contract(..., model_type=MethodStateV2)`,即对持久化 JSON 执行 `model_validate`
- `diagnosis_runtime.py` 的 1322 / 1340 / 1361 / 1685 行在重启与恢复路径上调用它
- `_replace`(`domain/methods_state_v2.py:84`)在每次状态迁移时重建 `MethodStateV2`,同样触发校验

因此 `MethodStateV2` 的模型校验器在**反序列化和每次迁移时**都会重跑。一个在旧规则下因"部分重叠"被判为 `UNRESOLVED` 的已持久化状态,在新规则下重算期望值会得出 `RESOLVED`,于是 `consensus.status != expected_status` → 抛 `ValueError` → **该 Case 无法再被读出**。

影响面比单个模型更广。位置 D 的 `validate_methods_reviewer_terminal_v2` 还被 `contracts/models.py:3739` 的 **`JobOutcome` 自身校验器**(REVIEW 分支)以及 `contracts/outcomes.py:369` 调用,因此:

- 失效的不只是 `MethodStateV2`,**任何反序列化 REVIEW 类型 `JobOutcome` 的路径**(存储读取、replay、审计工具)都会抛
- 全新空 `DATA_ROOT`(确定性测试、Release):**无影响**
- 已有 `DATA_ROOT` 且存在旧终态 Case / 旧 REVIEW Outcome:**读取即失败**

本计划书**不预设**处理方式。仓库文化是硬切不迁移(5.0.0 只接受全新空 `DATA_ROOT`),因此"记录为已知破坏 + 要求新数据根"很可能是正确答案,但这是产品决策。

**要求:实施到此处时停止,把影响面和候选方案报告给用户,由用户决定。不得自行选择方案,尤其不得为了兼容而在校验器里加旧规则分支——那会让两套语义长期并存。**

### 变更半径(已核对,供参考)

生产侧唯一调用点:`src/problem_locator/runtime/diagnosis_runtime.py:2732`(`finalize_reviewer_consensus_v2`)。`src/` 内无其他调用者。

**零改动**:ROUTE / no-plan preflight、Logparse 预处理与冻结、单次扫描、Evidence Graph、Evaluation Plan、Specialist 受理路径(`accept_specialist_evaluation_v2` 只校验自身结构与 repair,不做跨角色比较)、GENERIC 回退路径(`runtime/generic_locator.py` 对 `methods_state_v2` 零引用)。

**改的是裁决,不是评审**:两个模型的 prompt、上下文、方法卡、输出合同、调用预算(正常 2 次 / 硬上限 4 次)全部不变,Specialist 与 Reviewer 产出的数组一字不差。变的只是服务端如何合并两份未改变的判断。

**不需要改**:`MethodsTerminalProjectionV2` 字段与 status 枚举、`schemas/v2/contract-manifest.json` 的 JSON 形状、元 Skill 输出合同、模型侧 prompt。wire schema 不变,冻结 registration 身份不失效。

### 测试影响 —— **套件对本次改动是失明的,这是最大的风险**

> **已穷尽核实(独立复查):没有任何现有语义测试会因本次改动而失败。**
>
> 原因:所有构造"两侧都 CONFIRMED"的用例,两侧 `supporting_event_refs` 只有两种关系——**完全相同**(绝大多数)或**完全不相交**(仅三处)。新旧规则在这两种边界情形下结论一致。**从来没有任何测试构造过"部分重叠"的输入。**
>
> **推论,务必理解:如果你只改了位置 A 而漏掉 B–G,整个套件仍然全绿,缺陷会静默上线。** 现有测试**不是**这次改动的安全网。

**因此本 GOAL 新增的测试不是"顺带补个回归",而是唯一的检测手段**,且必须满足:

1. 构造两侧都 `CONFIRMED`、`supporting_event_refs` **部分重叠**(交集非空,且至少一侧不是另一侧的子集)
2. **走完整终态链路**,而不是只单测 `resolve_method_consensus_v2` —— 必须穿过 state 提交、`MethodTerminalResultV2` 构造、`JobOutcome` 校验、公开投影,这样才能同时覆盖位置 A–F
3. 断言发布的 `confirmed_event_refs` **和每条 confirmed evaluation 的 `evidence_event_refs`** 都等于交集,而不是任一侧的全集
4. 另需一条用例覆盖位置 G:计划两项,第 1 项双方 CONFIRMED 且交集非空,第 2 项 verdict 真分歧,断言 `diagnostic_evaluation_ref` 指向**第 2 项**

**已确认属于"不相交"家族、原样通过、不需改断言的三条**:

| 文件:行 | 测试 | 构造 |
|---|---|---|
| `test_methods_evaluation_v2.py:406` | `test_consensus_requires_exact_supporting_event_agreement` | specialist=[target] / reviewer=[noise] |
| `test_methods_state_v2.py:382` | `test_supporting_event_disagreement_is_not_resolved` | 两侧各选不同单一 ref |
| `test_methods_state_v2.py:407` | `test_state_machine_rejects_forged_resolved_consensus_when_event_refs_differ` | 同上 + 伪造 RESOLVED |

这三条的**函数名在新规则下已经名不副实**(它们实际只验证了"不相交"这一特例,而非"必须精确相等")。建议重命名以反映真实覆盖范围,**但重命名可选,不得借机改动断言**。

### 原"测试影响"清单(供定位,非必改)

引用 `finalize_reviewer_consensus_v2` 的测试文件:

- `tests/deterministic/unit/domain/test_methods_state_v2.py`(多处 `pytest.raises`,约 423 / 595 / 692 行 —— **疑似钉死精确相等的负向用例**)
- `tests/deterministic/unit/domain/test_methods_v2_blind_review_seam.py`
- `tests/deterministic/unit/domain/test_methods_v2_terminal_bridge.py`
- `tests/deterministic/contracts/test_methods_v2_public_schemas.py`

还有直接钉共识语义的(调用 `resolve_method_consensus_v2`):`tests/deterministic/unit/runtime/test_methods_evaluation_v2.py`。

### 已预先分析的两条关键用例

**`test_consensus_requires_exact_supporting_event_agreement`(:406)—— 预计原样通过,不需修改。**

名字虽是 "requires exact",但实际场景是 specialist 取 `[target_ref]`、reviewer 取 `[noise_ref]`,**两个集合不相交**(:411–412),断言 UNRESOLVED 且清空全部 confirmed ref(:432–435)。交集为空在新规则下仍判真分歧,行为不变。

> 仅建议在改动后重命名以反映其真实覆盖范围(如 `..._rejects_disjoint_supporting_events`),**但重命名是可选的,不得借机改变断言**。

**`test_consensus_keeps_only_target_event_from_same_method_noise`(:375)—— 预计原样通过。**

两侧都取 `[target_ref]`,完全一致,新旧规则同样判 RESOLVED。

**尚不存在的覆盖:部分重叠**。当前没有任何用例覆盖"两侧 CONFIRMED、event 集合部分重叠"——这正是 GOAL 2 修订的场景,也正是本 GOAL 必须新增的复现测试。

**每一处失败都要单独判断属于哪一类,不得一律改绿**:

| 类别 | 处置 |
|---|---|
| 该用例钉的正是被本次修订的"精确相等"语义 | 更新断言,并在提交说明中写明理由与对应合同条款 |
| 该用例本意是防别的东西(例如跨方法 event 混用),而交集规则**恰好放过了它** | **规则设计有洞。停止并报告,不要改测试。** |

第二类若出现,说明交集规则需要收紧(例如追加"交集内 event 必须同属该 evaluation 所属方法"的约束),应重新设计后再推进。把这类信号当作"跟随合同修订"糊过去,等于把安全属性一起松掉了。

### 完成判据

- 新增复现测试:改动前 FAIL,改动后 PASS
- 共识期望值计算在 `contracts/` 下只有一份实现,**七处强制点(A / A2 / C / D / E / F / G)全部调用它**,无任何一处保留独立重算
- 全仓搜索 `select different evidence events`、`differ from confirmed refs`、`differ from the two role evaluations` 三条错误文本,确认没有遗漏的重算点
- 位置 G 的 `diagnostic_evaluation_ref` 在"第 1 项一致、第 2 项真分歧"的构造下指向第 2 项(需新增用例证明)
- `contracts/methods_state_v2.py:~716` 与 `runtime/methods_outcome_v2.py:~138` 的单项 `evidence_event_refs` 发布的是交集,不是 Specialist 全集
- `deterministic.full` PASS,`det.evidence-v2-core` 的 `core-verdict.json` 仍 PASS
- 所有被修改的负向用例都有分类说明,无第二类未解决项
- `design/` 与 `README.md` 已同步

---

## GOAL 3 — 登记与收口

1. 取得最终 `verdict.json`(`tools\test-flow\run.ps1 --track dev --goal dev.default`),确认绑定当前源码快照
2. 按硬规则 3,在 `FIXED_ISSUES.md` 新增假分歧修复条目(编号顺延现有 `PL-FIX-*`),含:症状、受影响版本、根因、不可回归行为、修复历史、专项回归测试、最新 Test Flow verdict
3. 按硬规则 4,**verdict 引用行只能在全部验证完成后写入**;写入该元数据行之外,不得再修改交付字节
4. 归因结论写入 `TODO.md`(活跃事项),**不写入 `FIXED_ISSUES.md`**——归因本身不是修复

### 归因数据出来后的方向(本轮不实施,供报告参考)

| 主导 reason / 子因 | 真实瓶颈 | 下一步 | 代价 |
|---|---|---|---|
| `EVIDENCE_SET_MISMATCH` 占大头 | 假分歧 | GOAL 2 已解决,停 | 无 |
| `INCOMPLETE_EVALUATION` 占大头 | UNKNOWN 全局门 | per-evaluation 部分结果 | 需放松位置 B 的硬校验,schema 升版,全部冻结身份失效 |
| `NO_MATCHING_METHOD_EVIDENCE` 占大头 | marker 精度 / 激活过松 | 改 canonical marker 提取规则 | 元 Skill 与 validator 必须锁步,所有 registration 身份失效 |
| `*_SEMANTIC_INVALID` 占大头 | 模型产不出合规数组 | 改输出合同或 repair 预算 | 动模型侧契约与调用预算上限 |

若最终要做部分结果,倾向:**保持三态不变,RESOLVED 判据一字不改,仅在 UNRESOLVED 时不再清空双方一致确认的项**。不复活 `PARTIALLY_RESOLVED`,模型侧零改动。

---

## 停止条件(任一触发即停止并报告)

0. **第 0 节的 Fast E2E 测试收口尚未完成**(四类问题未全部关闭,或未取得收口后的绿色回归)
1. GOAL 0 拿不到 `deterministic.full` PASS
2. GOAL 2 的复现测试在改动前**没有** FAIL(说明问题不存在或复现构造错误 —— 见硬规则 1,此时不得继续修改)
3. 任一 `pytest.raises` 用例的失败原因属于"交集规则放过了本该拦住的东西"(上文第二类)。**特别注意**:`test_methods_evaluation_v2.py:406` 已预先分析为应原样通过;若它反而失败,说明交集实现有误(很可能错误地把不相交也当成一致),**立即停止**
4. 触及"已持久化状态会失效"这一节 —— **停止并报告,由用户决定处理方式**
5. 发现 `_validate_role_coverage` 的 plan-order 或跨 evaluation 约束**与本文所述不符**(本文已确认两者均被强制;若实际不符,说明代码在收口期间被改动,排序与安全性论证都需要重做)
6. 任何改动会**增删** `MethodsTerminalProjectionV2` 的字段、`MethodStateReasonCodeV2` 的 Literal 成员,或**改变** `schemas/v2/*.schema.json` 的 JSON 形状
7. 需要修改 `uv.lock` 或升级任何已锁定依赖

> ### 第 6 条的边界说明 —— 务必读完,否则会误停整个 GOAL 2
>
> `schemas/v2/contract-manifest.json` 按 sha256 **钉死了 12 个 `.py` 源文件**,其中包括 `contracts/models.py` 与 `contracts/methods_state_v2.py` —— 正是位置 B / C / D / E 所在的文件。
>
> **因此 GOAL 2 必然改动被 manifest 钉死的文件。这是预期内的,不构成第 6 条的停止情形。**
>
> | 允许(本轮必做) | 禁止 |
> |---|---|
> | 修改位置 B–G 的比较与推导逻辑 | 增删 `MethodsTerminalProjectionV2` 的字段、改其 `status` 枚举 |
> | 因此重算 `contract-manifest.json` 的 sha256 条目 | 改变 `schemas/v2/*.schema.json` 的 JSON 形状 |
> | 在 `storage/` 下新增内部记录(该目录未被钉死) | 增删 `MethodStateReasonCodeV2` 的 Literal 成员 |
> | | 放松位置 B 的"非 RESOLVED 必须清空 confirmed ref" |
>
> **"不动 models.py"和"不动 schemas/"都是错误的理解。** 该动的是校验逻辑、以及随之而来的 manifest 摘要;不该动的是合同的字段与形状。

---

## 明确不做(不得越界)

- 不动盲评结构(Reviewer 在模型调用结束前看不到 Specialist 输出)
- 不让模型接触原始日志、重新扫描 marker 或自报证据身份
- 不放松 `"UNKNOWN" not in verdicts` 全局门(等归因数据)
- 不放松非 RESOLVED 时清空 confirmed ref 的规则(等归因数据)
- 不放松 `REJECTED` / `UNKNOWN` 必须空数组的约束
- 不引入第三方仲裁角色(会突破 4 次调用硬上限;2-of-3 多数可能确认一个多数幻觉,对故障定位是比 UNRESOLVED 更差的失效)
- 不为让测试通过而修改钉死合同的负向用例
- 不做顺手重构。`diagnosis_runtime.py` 有 4100 行、`create_http_app` 有 784 行,**都不在本次范围内**

---

## 附:关键文件索引

| 路径 | 作用 |
|---|---|
| `src/problem_locator/runtime/methods_evaluation_v2.py:195` | **共识计算方**(位置 A),`resolve_method_consensus_v2`,判定在 214–227 |
| `src/problem_locator/domain/methods_state_v2.py` | 共识校验方(位置 A2);`_consensus_reason` ~340,`_validate_consensus` ~377,`finalize_reviewer_consensus_v2` ~433 |
| `src/problem_locator/contracts/methods_state_v2.py` | `MethodStateV2` 独立重算校验(位置 C,~263) |
| `tests/deterministic/unit/runtime/test_methods_evaluation_v2.py` | 直接钉共识语义的用例(:375 / :406 已预先分析) |
| `src/problem_locator/contracts/models.py` | `MethodsTerminalProjectionV2`(~683),清空校验(位置 B,~775),Reviewer 终态校验(**位置 D**,~836) |
| `src/problem_locator/contracts/methods_state_v2.py:~658` | 终态结果构造,断言两角色所选 event 相同(**位置 E**) |
| `src/problem_locator/runtime/methods_outcome_v2.py:~105` | **位置 F** —— 与 E 相同的断言,不同文件 |
| `src/problem_locator/contracts/methods_v2.py:509` | `CONFIRMED` 禁止空 `supporting_event_refs`(本轮只读) |
| `src/problem_locator/runtime/methods_evaluation_v2.py:158` | `_validate_role_coverage` —— 已强制 plan 顺序与 evaluation 归属 |
| `src/problem_locator/contracts/methods_reason_v2.py` | 公开 reason 词汇表 —— **本轮只读** |
| `src/problem_locator/runtime/diagnosis_runtime.py:2732` | 生产侧唯一调用点 |
| `src/problem_locator/runtime/methods_evidence_v2.py` | 单次扫描与 Plan 构建(排序策略确认点之一) |
| `src/problem_locator/storage/execution_records.py` | append-only 内部记录(GOAL 1 复用) |
| `AGENTS.md` | 仓库硬规则 |
| `design/wiki-diagnosis-generalization.md` | Evidence V2 权威设计与共识表 |
| `tools/test-flow/README.md` | Test Flow 操作说明 |
