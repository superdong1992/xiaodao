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

该 Goal 只生成并校验完整 production registration cache，不执行诊断。

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
  --plan-only
```

规划结果会列出 provider/model/settings、registration cache、Core 绑定、正常调用数 2、调用硬上限 4、
token/cost 和 admission blocker。审阅后才能移除 `--plan-only`，并加入 `--allow-real-model`。

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

Gate 写出 `model-cert-input.json`。中央 Test Flow 用共享 validator 生成 `model-cert.json`，两者都绑定：

- source snapshot 与 V8 contract manifest；
- 同快照的 `core-verdict.json`；
- DeepSeek provider/model/settings revision；
- Runtime、prompt/profile/tool policy identity；
- 每次角色调用、repair、四项 token usage 和费用；
- 最终公开 `methods_result` 的 canonical identity。

Standalone verdict 只证明这条 P1 快速认证路径，不能替代 `release.full`。
