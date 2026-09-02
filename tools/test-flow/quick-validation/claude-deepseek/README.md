# Claude Code + DeepSeek 快速验证

该入口冻结 Claude Code `2.1.89`、官方 `cli.js` SHA-256、env-only settings fingerprint 和
`deepseek-v4-flash[1m]`。该入口有三个互不替代的 Goal：Methods registration 生成、历史场景
Fast E2E、Evidence V2 P1 model cert。

## 生成 Methods registration cache

先看计划；确认后移除 `--plan-only` 并显式允许真实模型：

```bash
./tools/test-flow/quick-validation/claude-deepseek/run.sh \
  --goal dev.macos-claude-deepseek-methods \
  --client macos \
  --plan-only
```

该 Goal 只生成并校验完整 production registration cache，不执行诊断。计划中的
`inputs.registration_cache.path` 是 producer cache 目录，`inputs.registration_cache.registration_root`
是当前校验通过的完整 registration 根。P2 必须使用这里列出的同一个 `registration_root`，不能另行生成
或改写 package。

## 历史九场景 Fast E2E

Fast E2E 直接把 `experiments/rpc-skill-feasibility/cases/**` 中冻结的 `case.json` 和双端原始日志
交给生产 `DiagnosisRuntime`。它验证四个基础场景和五个能力探针，不读取 Release 的同名场景：

```text
api-execution-overrun      client-receive-blocked
deadloop-detected          insufficient-evidence
multiple-rpc-timeouts      server-queue-delay
server-queue-five          server-queue-single
unrelated-log-noise
```

先查看全部场景计划：

```bash
./tools/test-flow/quick-validation/claude-deepseek/run.sh \
  --goal fast-e2e \
  --all-scenarios \
  --registration-root <generated production registration> \
  --plan-only
```

也可以把 `--all-scenarios` 换成 `--scenario <id>` 调试单例。`insufficient-evidence` 必须在服务端
发现没有 cause evaluation 后直接 `UNRESOLVED`，模型调用数和硬上限都是 0；其余八例正常各调用
Specialist、Reviewer 一次，每个角色最多 repair 一次。完整矩阵正常 16 次、硬上限 32 次调用。

Fast E2E 不要求 source snapshot 或 Core verdict，不生成 `model-cert-input.json`、`model-cert.json` 或
Release verdict。每个场景有独立轻量 verdict；历史 oracle 不进入模型上下文。oracle 或模型能力失败
会封存该场景并继续矩阵，Runtime、provider 或证据落盘等工程失败才停止后续场景。该结论只说明九个
冻结场景下的定位能力边界。

## Evidence V2 P1 model cert

P1 只使用固定 `multiple-rpc-timeouts` 场景。调用方必须提供同一源码快照的 digest，以及该快照
已经通过的 `core-verdict.json`：

```bash
./tools/test-flow/quick-validation/claude-deepseek/run.sh \
  --goal dev.macos-claude-deepseek-e2e \
  --client macos \
  --scenario multiple-rpc-timeouts \
  --source-snapshot-digest <sha256> \
  --core-verdict <core-gate-root>/core-verdict.json \
  --registration-root <real.skill-generation registration root> \
  --evaluation-mode SPECIALIST_ONLY \
  --plan-only
```

`--registration-root` 优先使用同一次 `real.skill-generation` 产出的 production registration；未显式
提供时才使用 standalone cache。规划结果会在 `inputs.production_registration` 列出实际根路径、tree、
template 和 `methods.json` 摘要，并列出 provider/model/settings、Core 绑定、evaluation mode、调用上限、
token/cost 和 admission blocker。`--evaluation-mode` 省略时使用 `SPECIALIST_ONLY`：正常调用 Specialist
一次，协议 repair 后最多两次。显式传 `BLIND_CONSENSUS` 才会再调用 Reviewer，正常两次、最多四次。
审阅后才能移除 `--plan-only`，并加入 `--allow-real-model`。

认证驱动直接运行生产 `DiagnosisRuntime`：

1. 固定 Logparse fixture 完成一次确定性预处理；
2. 服务端生成 `methods-evidence-graph-v2.json` 和 `methods-evaluation-plan-v2.json`；
3. Specialist 只读取 request、Graph、Plan 和方法卡，写
   `output/method-diagnosis.draft.json`；
4. `SPECIALIST_ONLY` 直接发布 Case 的 `methods_result`；
5. `BLIND_CONSENSUS` 才由 `OutcomeSubmissionService` 创建独立 REVIEW Job，Reviewer 写
   `output/method-review.draft.json`，随后由 Runtime 机械共识。

角色只提交 `evaluation_ref + verdict + supporting_event_refs + reason` 根数组。确认项只选当前计划项中的
有序 event ref 子集。每个启用的角色第一次发生 JSON 结构或 Plan 覆盖错误时，只允许一次 repair；
没有模型重试。wrapper 不改写草稿，`harness_normalized=false`。

该路径不创建或消费 Candidate，不产生 `PARTIALLY_RESOLVED`，不下载 `result.zip`，也不读取
Methods V1 grounding。它不运行 Client、浏览器、上传、CrossJob 或 Release。

Gate 先写 `model-cert-input.json`，再用共享 builder 在同一 evidence 根生成并复验
`model-cert.json`。中央 Test Flow 之后若再次物化，必须得到完全相同的 canonical bytes。两者都绑定：

- source snapshot 与 V8 contract manifest；
- 同快照的 `core-verdict.json`；
- DeepSeek provider/model/settings revision；
- Runtime、prompt/profile/tool policy identity；
- 每次角色调用、repair、四项 token usage 和费用；
- 固定场景的原始 Wiki、validated registration、生产 Skill content hash，以及 driver 原始初始输入；
- 生产 Graph 的有序 sources 和 Graph/Plan canonical bytes identity；
- 最终公开 `methods_result` 的 canonical identity。

在写 model cert 前，production driver 会留下原始 execution records。`SPECIALIST_ONLY` 保存 source Job、
Graph、Plan、limitations、终态 source state 和 source Outcome 共六份；`BLIND_CONSENSUS` 再保存 Reviewer
Job、终态 Reviewer state 和 Reviewer Outcome，共九份。两种模式都会保存 `methods-result-v2.json` 和实际
加载的 `methods.json`。`scenario-oracle-receipt.json` 只是这些原件的闭合索引；共享 validator 会重新读取
原件并调用完整 Methods V2 oracle，不会信任 Runtime 自报的 PASS 布尔值。

场景身份直接取生产 Runtime 生成的 Graph/Plan，并强制它们的 Skill hash 与当前
`source_job.skill_ref.content_hash` 相同。`methods_result` 必须引用同一 Graph/Plan；测试侧不会重新匹配
日志 marker，也不会用 registration tree digest 冒充 Skill content hash。

Standalone verdict 只证明这条 P1 快速认证路径，不能替代 `release.full`。
