# Claude Code + DeepSeek 快速验证

该入口冻结 Claude Code `2.1.89`、官方 `cli.js` SHA-256、env-only settings fingerprint 和
`deepseek-v4-flash[1m]`。Methods package 生成与 Evidence V2 模型认证是两个独立 Goal。

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
  --plan-only
```

`--registration-root` 优先使用同一次 `real.skill-generation` 产出的 production registration；未显式
提供时才使用 standalone cache。规划结果会在 `inputs.production_registration` 列出实际根路径、tree、
template 和 `methods.json` 摘要，并列出 provider/model/settings、Core 绑定、正常调用数 2、调用硬上限
4、token/cost 和 admission blocker。审阅后才能移除 `--plan-only`，并加入 `--allow-real-model`。

认证驱动直接运行生产 `DiagnosisRuntime`：

1. 固定 Logparse fixture 完成一次确定性预处理；
2. 服务端生成 `methods-evidence-graph-v2.json` 和 `methods-evaluation-plan-v2.json`；
3. Specialist 只读取 request、Graph、Plan 和方法卡，写
   `output/method-diagnosis.draft.json`；
4. 生产 `OutcomeSubmissionService` 创建独立盲评 REVIEW Job；
5. Reviewer 只读取自己的冻结上下文，写 `output/method-review.draft.json`；
6. 生产 Runtime 机械共识并发布 Case 的 `methods_result`。

两个角色都只提交 `evaluation_ref + verdict + reason` 根数组。正常各调用一次；某角色第一次发生
JSON 结构或 Plan 覆盖错误时，只允许一次 repair。每角色最多两次，总上限四次，没有模型重试。
wrapper 不改写草稿，`harness_normalized=false`。

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

在写 model cert 前，production driver 会留下 source/reviewer Job、Graph、Plan、limitations、两份 state、
两份 Outcome 共九个原始 execution record，同时留下 `methods-result-v2.json` 和实际加载的
`methods.json`。`scenario-oracle-receipt.json` 只是这些原件的闭合索引；共享 validator 会重新读取原件
并调用完整 Methods V2 oracle，不会信任 Runtime 自报的 `hard_cut` 或 PASS 布尔值。

场景身份直接取生产 Runtime 生成的 Graph/Plan，并强制它们的 Skill hash 与当前
`source_job.skill_ref.content_hash` 相同。`methods_result` 必须引用同一 Graph/Plan；测试侧不会重新匹配
日志 marker，也不会用 registration tree digest 冒充 Skill content hash。

Standalone verdict 只证明这条 P1 快速认证路径，不能替代 `release.full`。
