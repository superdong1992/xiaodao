# Codex/Luna 快速验证

本入口保留三个互不替代的目标：

- `methods`：用一次 Codex + `gpt-5.6-luna`/medium 调用生成并冻结 Methods package；
- `fast-e2e`：使用历史九场景快速验证定位能力和能力边界；
- `e2e`：运行生产 Evidence V2 `DiagnosisRuntime`，为 P2 生成 model cert。

`fast-e2e` 直接读取 `experiments/rpc-skill-feasibility/cases/**` 中已讨论并冻结的九个场景，
不读取同名 Release fixture。它复用生产 `DiagnosisRuntime`、Evidence Graph、Evaluation Plan、
Specialist 和盲评 Reviewer；八个有 cause evidence 的场景正常 2 次调用、repair 后最多 4 次，
`insufficient-evidence` 必须由服务端零模型结束。九场景合计正常 16 次、硬上限 32 次。它不要求 Core verdict 或
source snapshot，也不生成 `model-cert.json`。运行前先查看计划：

```bash
./tools/test-flow/quick-validation/codex-luna/run.sh \
  --goal fast-e2e \
  --all-scenarios \
  --registration-root <path/to/validated-registration> \
  --plan-only
```

单场景调试可把 `--all-scenarios` 换成 `--scenario <scenario-id>`。只有确认计划后才加入
`--allow-real-model`。九场景能力或 oracle 失败会封存该场景并继续；运行环境、provider 或输入损坏等
工程失败才停止后续场景。

`e2e` 固定使用 `multiple-rpc-timeouts`。生产 Runtime 从同一份已验证 registration、release
`driver.json`、`client.log` 和 `server.log` 生成权威 Evidence Graph 与 Evaluation Plan，再把机械派生的
紧凑 `evaluation_input` 内嵌到生产 prompt。模型只读取该紧凑输入、`request.json` 和 prompt 中的方法卡，
其中 `sources` 会保留全部冻结 target，包括没有命中 observation 的 source。
model-only Workspace 的 `runtime/` 目录只保留必要的 `tool-state`，且整个 Workspace 不包含单独的
Graph/Plan 文件或 `runtime/context.txt`；driver 仍以服务端记录中的权威 Graph/Plan 校验完整性、
顺序和 event 子集。Wrapper 还要求 prompt 恰好包含一个与角色匹配的数据 section，并将 Specialist
或 Reviewer 的闭合身份与 `request.json`、`manifest.json` 对齐；`inputs/` 只能包含这两个文件，
任何旧证据目录或未知输入都会原样保留并 fail closed。默认
`SPECIALIST_ONLY`，模型只提交 Specialist evaluation 数组，正常一次调用，协议 repair 后最多两次。
显式选择 `BLIND_CONSENSUS` 才会再调用盲评 Reviewer，正常两次、最多四次。该路径不读取 Candidate、
`PARTIALLY_RESOLVED`、`result.zip` 或 Methods V1 grounding。

先查看计划：

```bash
./tools/test-flow/quick-validation/codex-luna/run.sh \
  --goal e2e \
  --scenario multiple-rpc-timeouts \
  --source-snapshot-digest <sha256> \
  --core-verdict <path/to/core-verdict.json> \
  --registration-root <path/to/validated-registration> \
  --evaluation-mode SPECIALIST_ONLY \
  --plan-only
```

`--registration-root` 应优先指向 P1/正式 skill-generation 已验证的同一 production registration，
从而让 P1、P2 绑定完全相同的 Wiki、Skill、用户输入、日志、Graph 和 Plan。独立调试也可改传
`--cache-root`，消费当前 Codex Methods producer identity 的冻结 package；该 fallback 不会重新调用
生成模型。

确认计划中的 source snapshot、Core 收据、registration、模型身份、evaluation mode、调用上限、
token/cost 预算和 admission blocker 后，加入 `--allow-real-model` 并移除 `--plan-only` 才会调用模型。
真实运行会在同一 evidence root 保存 execution records：默认模式为六份 Specialist 原件，显式盲评模式
为九份。两种模式还会保存 `methods-result-v2.json`、registration 中逐字复制的 `methods.json`，以及
`scenario-oracle-receipt.json`。共享
validator 会从 frozen release case 和这些原件重放完整 Methods V2 oracle，再生成
`model-cert-input.json` 与 `model-cert.json`。provider 调用、usage、`runtime-receipt.json` 和
`adapter-receipt.json` 也保留在同一根，最后由 standalone `verdict.json` 封口。

Methods package 生成仍需先规划：

```bash
./tools/test-flow/quick-validation/codex-luna/run.sh --goal methods --plan-only
```

standalone verdict 只证明它声明的快速路径，不能替代中央 Test Flow、Release verdict 或物理局域网
验收。原始 credential 不得进入 prompt、计划、证据或 verdict。
