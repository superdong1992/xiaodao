# 实施计划书:Evidence V2 UNRESOLVED 归因基线

> **交付对象**:实施 Agent(零上下文)。本文自包含,不要求阅读此前的对话。
> **仓库**:`D:\code\xiaodao`,Problem Locator 5.0.0,Python 3.12。
> **本轮范围**:只做 GOAL 0 → 1 → 2。**附录 A 是未来候选工作,本轮不得实施。**
> **最重要的一条**:遇到"停止条件"列出的任一情形,**立即停止并报告**,不要自行绕过。

---

## 1. Context:为什么做这件事

Problem Locator 的 SPECIALIZED 诊断链路(Evidence V2)让 Specialist 和 Reviewer 两个隔离角色盲评,各自提交 `{evaluation_ref, verdict, supporting_event_refs, reason}` 数组,由服务端做共识裁决。模型不产生权威结论。

这套设计几乎消灭了幻觉类失效,但共识裁决极度保守:任一项分歧、任一项 `UNKNOWN`、或两侧引用的证据子集不完全相同,整个 Case 都判 `UNRESOLVED`。**因此预期的真实失效模式是"UNRESOLVED 率过高",而不是"给错答案"。**

**但这个判断目前零数据支撑**——仓库中没有任何 `model-cert.json` 或真实模型运行记录。UNRESOLVED 也完全可能主要来自:

- `NO_MATCHING_METHOD_EVIDENCE`(marker 过度召回、激活过松)
- `*_SEMANTIC_INVALID` / `*_REPAIR_EXHAUSTED`(模型产不出合规数组)

**这几种成因需要完全不同的修法,放松共识规则对后两种收益为零。** 而且"共识太严"和"激活太松"会产生完全相同的症状(方法数 N 越大,逐项一致越难达成),必须靠指标区分。

**本轮目标:建立可信的 UNRESOLVED 归因分布,为后续方向选择提供依据。不改变任何裁决语义。**

### 一个当前无法区分的关键分叉

`_consensus_reason`(`domain/methods_state_v2.py:340`)把两种性质完全不同的情况都归为 `SPECIALIST_REVIEWER_DISAGREEMENT`:

- 两侧 `verdict` 本身不同 → **真分歧**,模型判断不一致
- 两侧 `verdict` 全同,只是 `supporting_event_refs` 集合不同 → **假分歧**,双方都认同结论,只是引用了不同证据子集

这正是归因最需要的一刀,也是本轮的核心产出。

---

## 2. 前置依赖:必须先完成 Fast E2E 测试收口

**本计划书不得在测试收口完成前启动。**

仓库当前处于 Evidence V2 Fast E2E 迁移中途(HEAD~1 提交名即 `wip: checkpoint Evidence V2 fast e2e migration`)。已知尚有四类未收口问题:

1. `formalization` 必须携带 Graph,堵住 event 与 hit 错配
2. Claude Fast 的历史字段映射成 production registration 的 slot/process 字段
3. Fast 去掉 Release Wiki/缓存依赖,并校验 registration 生成凭据
4. 去掉正测中的 oracle 反向喂答案,补 noise 误选负例,统一 marker 大小写/顺序规则

**为什么必须先收口**:GOAL 0 要求先取得 `deterministic.full` PASS 作为对照基线。迁移中途要么拿不到,要么拿到的是"绿但空心"的基线(第 4 项的"oracle 反向喂答案"若属实,绿色套件是自证的)。

### 给测试收口方的协调约束

补 noise 误选负例时:

- **可以**继续按现有模式钉"两侧 event 集合**不相交** → UNRESOLVED"
- **不建议**新钉"两侧 CONFIRMED、event 集合**部分重叠** → UNRESOLVED"

后者的语义正在评估中(见附录 A),现在钉成永久合同,将来若调整会造成返工。

### 已核实的两处状态偏差(收口方需自行复核)

以下两项在当前 `main` 上看起来至少部分已完成,与自述不符,应先复核再决定是否还需工作:

- `src/problem_locator/application/formalization.py:138` 已有 `graph: MethodEvidenceGraphV2` 参数(199、287 行另有 `evidence: MethodEvidenceGraphV2`)
- noise 误选负例已存在:`tests/deterministic/unit/runtime/test_methods_evaluation_v2.py:375` 与 `:406`,`tests/deterministic/unit/runtime/test_methods_outcome_v2.py:252` 与 `:288`

若实际缺口只在 Fast E2E 场景层而非核心层,应在报告中明确区分。

---

## 3. 仓库硬规则(违反将导致工作被拒)

摘自仓库根 `AGENTS.md`:

1. **修复前必须验证问题**。未确认前只允许只读调查。
2. **每个修复必须新增能直接复现原问题的测试**。泛化的全量 PASS 不能替代专项回归用例。
3. **修复登记**写入 `FIXED_ISSUES.md`(含症状、根因、不可回归行为、专项回归测试、verdict)。
4. **verdict 引用只能在验证完成后写入**。
5. **Test Flow 是唯一入口**:`tools/test-flow/run.ps1`(Windows)或 `run.sh`。不得绕过编排器拼装发布结论。
6. **不得为测试方便增加兼容层**、隐藏字段或客户端 Hook。
7. 活跃未完成事项进 `TODO.md`,不进 `FIXED_ISSUES.md`。

补充自 `design/wiki-diagnosis-generalization.md`:

8. 叙述文档必须随同一变更同步修正。
9. 负向用例从生产生成的合法基线开始,每次只修改一个字段。

---

## GOAL 0 — 解除 INFRA 阻塞,取得对照基线

### 背景

最新一次 `dev.default` 在 `repository.static` BLOCKED,`failure_domain: INFRA`:`repo.compileall` → `PYTHON_312_TEST_RUNTIME_MISSING`,`repo.uv-lock` → `UV_REQUIRED`,导致 `deterministic.affected` 与 `deterministic.full` 双双 `NOT_RUN`。**不是代码缺陷**,是本机缺 CPython 3.12 与 `uv`。

### 步骤

1. 安装 CPython 3.12(`pyproject.toml` 要求 `>=3.12,<3.13`;3.13 会失败)与 `uv`
2. `uv sync --frozen --all-groups` 且 `uv lock --check`(不得顺带升级已锁定依赖)
3. `tools\test-flow\run.ps1 --track dev --goal dev.default --plan-only`,确认无 admission blocker
4. 实跑 `tools\test-flow\run.ps1 --track dev --goal dev.default`

> **Windows 路径提示**:若深层工作树触发 `MAX_PATH`,在 plan-only 与实跑两次命令中把 `TEST_FLOW_WINDOWS_SCRATCH_ROOT` 设为同一个绝对可写短目录。

### 完成判据

- `verdict.json` 中 `deterministic.full` 为 PASS,其中 `det.evidence-v2-core` 的 `core-verdict.json` 为 `status: PASS`
- **记录并保存** run ID、`source_snapshot_digest`、pytest 计数,以及 `core-verdict.json` 里的 `contract_manifest.sha256`(GOAL 1 结束时要比对它没变)

**历史参考基线**(2026-08-28 的一次运行,仅供对照,不得复用):`core_cases.count = 55`,`pytest: 106 executed / 106 passed / 0 failures`,`model_invocations = 0`。

---

## GOAL 1 — 归因仪表(本轮主交付)

### 核心约束:零冻结字节改动

`schemas/v2/contract-manifest.json` 按 sha256 钉死了 **12 个 `.py` 源文件,全部位于 `src/problem_locator/contracts/`**:`__init__.py`、`commands.py`、`enums.py`、`errors.py`、`limits.py`、`methods_reason_v2.py`、`methods_state_v2.py`、`methods_v2.py`、`models.py`、`outcomes.py`、`ports.py`、`serialization.py`。

**本 GOAL 要动的文件一个都不在这个清单里**:

| 要动的文件 | 是否被钉死 |
|---|---|
| `domain/methods_state_v2.py` | 否 |
| `runtime/diagnosis_runtime.py` | 否 |
| `storage/execution_records.py` | 否 |
| `tools/` 下新增聚合脚本 | 否 |

**因此 `contract-manifest.json` 不应出现任何漂移。若你的实现导致它需要重算,说明落点选错了,回到下面的可行路径。**

同时不得增删 `MethodStateReasonCodeV2` 的 Literal 成员、`METHOD_PUBLIC_REASON_TEXT_V2` 的条目、`MethodsTerminalProjectionV2` 的字段。**归因结果只进内部记录,不进任何公开投影(MCP / REST / Case)。**

### 步骤 1 — 内部归因记录的落点(唯一可行路径,已核实)

**不要给既有合同模型加字段。** `ContractModel` 全局 `extra="forbid"`(`contracts/models.py:333`),且 `runtime/methods_records_v2.py:101` 每次写入都会用同一模型回读校验;更关键的是那些模型所在文件被 manifest 钉死,加字段等于改动冻结字节。

**可行路径**:

- `storage/execution_records.py` **不在** manifest 清单内
- `publish_audit_bytes`(`execution_records.py:507`)接受任意原始字节,但文件名必须在写死的 `_AUDIT_FILENAMES` frozenset(`execution_records.py:74-105`)内,否则 `ValueError("unsupported execution audit filename")`
- 因此:在 `_AUDIT_FILENAMES` 中**新增一个全新文件名**(如 `methods-consensus-attribution-v2.json`),用一个**不注册进 `schemas/v2/`、不进 manifest** 的内部结构写入
- 该 frozenset 不属于 `contracts/ports.py` 的 `ExecutionRecordStore` Protocol,只是这个适配器的实现细节

**注意语义**:`publish_audit_bytes` 的 "append-only" 指**幂等写一次后不可变**(同名文件再写会按字节比对,不同则 `IDEMPOTENCY_CONFLICT`),**不是**逐行追加的日志通道。每个 job 写一次完整归因记录即可。

**不要改用** `diagnostics.log_event` 或 `journey.record_journey_event`:两者字段虽自由,但都是尽力而为、允许静默丢失的观测通道(`diagnostics.py:102` 吞异常;`journey.py:307` 写失败会禁用整个 writer),且按进程级单文件滚动而非按 job 分区,不适合做需要精确聚合的归因底座。

### 步骤 2 — 共识子因的区分

在 `domain/methods_state_v2.py` 的 `_consensus_reason`(约 340 行)现有返回值之外,额外产出内部子因:

| 子因 | 判定 |
|---|---|
| `UNKNOWN_PRESENT` | 任一侧任一项 verdict 为 `UNKNOWN` |
| `VERDICT_MISMATCH` | 存在某项两侧 verdict 不同 |
| `EVIDENCE_SET_MISMATCH` | 所有项 verdict 相同,但存在某项 `supporting_event_refs` 集合不同 |
| `NO_CONFIRMED` | 两侧完全一致但无 `CONFIRMED` |

**现有实现有一处不对称,记录子因时必须两侧都检查**:`_consensus_reason` 只看 `second.verdict == "UNKNOWN"`(即 Reviewer),而 `_validate_consensus` 检查的 `verdicts` 取自 Specialist。请在报告中说明这处不对称是否需要单独处理。

**公开 `reason_code` 保持不变**——`SPECIALIST_REVIEWER_DISAGREEMENT` 仍照常返回,子因只进内部记录。

### 步骤 3 — 规模指标

| 指标 | 来源 | 可达性 |
|---|---|---|
| 公开 `reason_code` | 已有 | 直接可得 |
| 共识子因 | 步骤 2 新增 | — |
| N = evaluation 数 | `plan.evaluations` 取 `len()` | 形参 `plan` 直接可得 |
| 每 evaluation 的 event 数 | `MethodEvaluationPlanItemV2.evidence_event_refs` 取 `len()` | 形参 `plan` 直接可得 |
| 激活方法数 | `graph.loaded_method_ids` 取 `len()` | 形参 `graph` 直接可得 |
| package 总方法数 | `ResolvedSpecializedSkillV1.methods.methods` | **取不到,见下** |

> **已核实的信息可达性缺口**:前几项在 `diagnosis_runtime.py:2732`(`finalize_reviewer_consensus_v2` 调用点)由形参 `plan` / `graph` 直接可得。但 **package 总方法数取不到**——`_evaluate_methods_reviewer_v2` 的签名(`diagnosis_runtime.py:2556-2567`)里没有 `skill` / `assets`。需要给该方法**新增一个形参穿透**。纯代码改动,不涉及合同。

> N 与激活率是关键:逐项一致的难度随 N 上升,而 N 由 activation 精度决定。**"共识太严"和"激活太松"症状完全相同**,必须靠这两个指标区分。

### 步骤 4 — 只读聚合脚本

从 execution records 统计上述分布,放在 `tools/` 下。

**不得接入中央 Goal / Proof / Stage / Gate**(硬规则 5)——它是诊断工具,不产出 verdict。

### 完成判据

- 四种共识子因在确定性 journey 产生的 execution records 上都能被区分出来
- 聚合脚本可跑通并输出分布
- `git diff` 确认 `contracts/` 下**零改动**、`schemas/` 下**零改动**
- `deterministic.full` 仍 PASS,且 `core-verdict.json` 的 `contract_manifest.sha256` 与 GOAL 0 记录的值**完全相同**
- 公开 MCP / REST 的 Case 投影**没有任何新字段**

---

## GOAL 2 — 收口与登记

1. 取得最终 `verdict.json`,确认绑定当前源码快照
2. **归因埋点不是"修复"**,因此**不写入 `FIXED_ISSUES.md`**(硬规则 7)。把归因能力与初步分布结论写入 `TODO.md` 的活跃事项
3. 在报告中给出 UNRESOLVED 成因分布,并按下表指出后续方向

| 主导 reason / 子因 | 真实瓶颈 | 后续方向 |
|---|---|---|
| `EVIDENCE_SET_MISMATCH` 占大头 | 假分歧 | 执行附录 A |
| `INCOMPLETE_EVALUATION` 占大头 | UNKNOWN 全局门 | per-evaluation 部分结果(需 schema 升版) |
| `NO_MATCHING_METHOD_EVIDENCE` 占大头 | marker 精度 / 激活过松 | 改 canonical marker 提取规则(元 Skill 与 validator 须锁步) |
| `*_SEMANTIC_INVALID` 占大头 | 模型产不出合规数组 | 改输出合同或 repair 预算 |

---

## 停止条件(任一触发即停止并报告)

1. **第 2 节的 Fast E2E 测试收口尚未完成**
2. GOAL 0 拿不到 `deterministic.full` PASS(不得在无基线的情况下改任何东西)
3. 实现导致 `contract-manifest.json` 需要重算,或 `contracts/` 下出现任何改动
4. 需要增删 `MethodStateReasonCodeV2` 成员、`METHOD_PUBLIC_REASON_TEXT_V2` 条目或 `MethodsTerminalProjectionV2` 字段
5. 归因数据必须出现在公开投影(MCP / REST / Case)里才能满足需求
6. 需要修改 `uv.lock` 或升级任何已锁定依赖

---

## 明确不做

- **不改变任何裁决语义**。本轮不动 `_validate_consensus` 与 `resolve_method_consensus_v2` 的判定逻辑,只在 `_consensus_reason` 旁增加内部子因产出
- **不实施附录 A**。那是待数据决定的候选工作
- 不动盲评结构、不让模型接触原始日志、不放松任何现有校验
- 不做顺手重构。`diagnosis_runtime.py` 有 4100 行、`create_http_app` 有 784 行,都不在本次范围内

---

## 附录 A — 待数据决定的候选工作:消除假分歧

> **本轮不实施。** 以下是已完成的完整调查结果。若归因数据显示 `EVIDENCE_SET_MISMATCH` 是主因,可直接依此执行,无需重新调查。

### 目标语义

逐项比较从"三元组逐字相等"改为:verdict 必须相等;`CONFIRMED` 时两侧 `supporting_event_refs` **交集非空**即算一致,发布**交集**;交集为空仍判真分歧;`REJECTED` / `UNKNOWN` 仍须两侧为空;两个全局门保持不变。

`MethodEvaluationOutputItemV2`(`contracts/methods_v2.py:509-517`)**禁止** `CONFIRMED` 携带空 refs,因此"空交集判为分歧"是**合同强制**,不是设计偏好。

### 七处强制点,必须同时改

| 位置 | 文件 | 角色 | 漏改的症状 |
|---|---|---|---|
| A | `runtime/methods_evaluation_v2.py:214-227` **及 237-260** | 计算方:判定 + 构造 `confirmed_event_refs` | 规则根本没生效 |
| A2 | `domain/methods_state_v2.py:~388` | 校验方 | `consensus differs from the two role evaluations` |
| C | `contracts/methods_state_v2.py:~263`(文本 :269) | 校验方 | `consensus event refs differ from the two role evaluations` |
| D | `contracts/models.py:~836`(文本 :858) | 断言终态 refs 等于 Reviewer 自己的 refs | `resolved Methods Reviewer verdicts or events differ from confirmed refs` |
| E | `contracts/methods_state_v2.py:~658`(文本 :671) | 断言两角色所选 event 相同 | `resolved role evaluations select different evidence events` |
| F | `runtime/methods_outcome_v2.py:~105`(文本 :111) | **与 E 相同的断言,不同文件** | 文本一模一样 |
| G | `domain/methods_state_v2.py:357-369` | **归因**,非判定 | **不抛异常**,静默把 `diagnostic_evaluation_ref` 指向错误的项 |

E 与 F 是同一条断言复制在两个文件,错误文本相同。**看到同样的异常第二次出现,是还有一个文件没改,不是改错了。**

**另有两处"单项构造"必须同改**(不是校验,是结果正确性):`contracts/methods_state_v2.py:~716` 与 `runtime/methods_outcome_v2.py:~138`,当前把 Specialist 全集写进每条 confirmed evaluation 的 `evidence_event_refs`。漏改不报错,但会发布 Reviewer 未认可的证据。

### 已确认的前置事实

`_validate_role_coverage`(`runtime/methods_evaluation_v2.py:158`)已强制两件事:**`supporting_event_refs` 必须按 plan 顺序排列**(所以交集保留任一侧顺序即可,不需重排),以及**只能引用本 evaluation 自己的 event**(所以跨方法混用结构上不可能,交集不会引入泄漏)。

### 测试套件对该改动是失明的

**已穷尽核实:没有任何现有语义测试会因该改动失败。** 所有双方 CONFIRMED 的用例,两侧 refs 只有"完全相同"或"完全不相交"两种关系,从未构造过部分重叠。

**推论:只改位置 A 而漏掉 B–G,套件仍全绿,缺陷静默上线。** 因此新增测试是**唯一检测手段**,且必须走完整终态链路(state 提交 → `MethodTerminalResultV2` 构造 → `JobOutcome` 校验 → 公开投影),不能只单测共识函数;另需一条用例覆盖位置 G 的归因正确性。

已确认属于"不相交"家族、原样通过的三条:`test_methods_evaluation_v2.py:406`、`test_methods_state_v2.py:382`、`test_methods_state_v2.py:407`。它们的函数名在新规则下名不副实,可重命名,但不得改动断言。

### 必须重算 contract-manifest

位置 C / D / E 在 `contracts/methods_state_v2.py` 与 `contracts/models.py`,**两者都被 manifest 按 sha256 钉死**。因此该改动必然需要重算 manifest。

- 校验点:`tests/deterministic/contracts/test_schema_snapshots.py:128`
- **仓库内没有生成器**,须手工重算并保持 canonical JSON 字节(同文件 :97)
- 不要增删条目(:131 会检查)
- **`test_schema_snapshots.py` 变红不表示改错了**,正确修法是更新 manifest 而不是回退代码

### 向后兼容:决策已作出 —— 硬切,不迁移

`read_method_state_v2`(`runtime/methods_records_v2.py:257`)对持久化 JSON 执行 `model_validate`,`diagnosis_runtime.py` 的 1322 / 1340 / 1361 / 1685 行在重启恢复路径调用它;`_replace`(`domain/methods_state_v2.py:84`)每次状态迁移也重建模型。位置 D 的 `validate_methods_reviewer_terminal_v2` 还被 `contracts/models.py:3739` 的 `JobOutcome` 校验器调用,因此**任何反序列化 REVIEW 类型 `JobOutcome` 的路径**也会受影响。

结论:旧的"部分重叠 → UNRESOLVED"持久化状态在新规则下读取即抛异常。

**决策已作出:硬切,不迁移。** 记录为已知破坏,要求使用全新空 `DATA_ROOT`(与 5.0.0 本身只接受全新空 `DATA_ROOT`、V1–V7 一律不迁移的既有文化一致)。**不得在校验器里加旧规则兼容分支**——那会让两套共识语义长期并存。

### 已排查确认不受影响的区域

`runtime/methods_replay_v2.py`(只重放单侧)、`runtime/methods_records_v2.py`(纯 I/O)、`runtime/server_outcome_finalizer.py`、`runtime/outcome_publisher.py`、`runtime/user_results.py`、`application/outcome_submission.py`、`application/projection.py`、`application/audit_bundle_assembler.py`、`interfaces/*`(仅 OpenAPI 描述文字)均无双侧比较逻辑。`runtime/methods_grounding.py` 与 `runtime/methods_outcome.py` 属于旧 Methods V1 协议,与此无关。

---

## 附:关键文件索引

| 路径 | 作用 |
|---|---|
| `src/problem_locator/domain/methods_state_v2.py:340` | `_consensus_reason` —— GOAL 1 步骤 2 落点(**未被 manifest 钉死**) |
| `src/problem_locator/storage/execution_records.py:74-105` | `_AUDIT_FILENAMES` 白名单 —— GOAL 1 步骤 1 落点(**未被钉死**) |
| `src/problem_locator/runtime/diagnosis_runtime.py:2556-2567` | `_evaluate_methods_reviewer_v2` 签名,需加形参穿透 |
| `src/problem_locator/runtime/diagnosis_runtime.py:2732` | `finalize_reviewer_consensus_v2` 调用点,`plan` / `graph` 在此可得 |
| `schemas/v2/contract-manifest.json` | 钉死 12 个 `contracts/*.py` —— GOAL 1 **不得使其漂移** |
| `tests/deterministic/contracts/test_schema_snapshots.py:128` | manifest 一致性校验 |
| `AGENTS.md` | 仓库硬规则 |
| `design/wiki-diagnosis-generalization.md` | Evidence V2 权威设计与共识表 |
| `tools/test-flow/README.md` | Test Flow 操作说明 |
