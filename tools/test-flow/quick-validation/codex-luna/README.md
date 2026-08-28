# Codex/Luna 快速验证

本入口保留两个互不替代的目标：

- `methods`：用一次 Codex + `gpt-5.6-luna`/medium 调用生成并冻结 Methods package；
- `e2e`：运行生产 Evidence V2 `DiagnosisRuntime`，为 P2 生成 model cert。

`e2e` 固定使用 `multiple-rpc-timeouts`。生产 Runtime 从同一份已验证 registration、release
`driver.json`、`client.log` 和 `server.log` 生成 Evidence Graph 与 Evaluation Plan。模型只提交
Specialist 和盲评 Reviewer 的 evaluation 数组。正常调用数为 2；每个角色首次协议错误时最多 repair
一次，总上限为 4。该路径不读取 Candidate、`PARTIALLY_RESOLVED`、`result.zip` 或 Methods V1
grounding。

先查看计划：

```bash
./tools/test-flow/quick-validation/codex-luna/run.sh \
  --goal e2e \
  --scenario multiple-rpc-timeouts \
  --source-snapshot-digest <sha256> \
  --core-verdict <path/to/core-verdict.json> \
  --registration-root <path/to/validated-registration> \
  --plan-only
```

`--registration-root` 应优先指向 P1/正式 skill-generation 已验证的同一 production registration，
从而让 P1、P2 绑定完全相同的 Wiki、Skill、用户输入、日志、Graph 和 Plan。独立调试也可改传
`--cache-root`，消费当前 Codex Methods producer identity 的冻结 package；该 fallback 不会重新调用
生成模型。

确认计划中的 source snapshot、Core 收据、registration、模型身份、正常 2 次调用、4 次硬上限、
token/cost 预算和 admission blocker 后，加入 `--allow-real-model` 并移除 `--plan-only` 才会调用模型。
真实运行会在同一 evidence root 写出 9 个 production execution record、`methods-result-v2.json`、
实际加载 registration 中逐字复制的 `methods.json`，以及 `scenario-oracle-receipt.json`。共享
validator 会从 frozen release case 和这些原件重放完整 Methods V2 oracle，再生成
`model-cert-input.json` 与 `model-cert.json`。provider 调用、usage、`runtime-receipt.json` 和
`adapter-receipt.json` 也保留在同一根，最后由 standalone `verdict.json` 封口。

Methods package 生成仍需先规划：

```bash
./tools/test-flow/quick-validation/codex-luna/run.sh --goal methods --plan-only
```

standalone verdict 只证明它声明的快速路径，不能替代中央 Test Flow、Release verdict 或物理局域网
验收。原始 credential 不得进入 prompt、计划、证据或 verdict。
