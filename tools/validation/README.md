# Evidence V2 验证收据

本目录只保存 Evidence V2 的子收据合同。正式测试仍从 `tools/test-flow/run.sh` 或
`tools/test-flow/run.ps1` 进入，最终结论仍以外层 `verdict.json` 为准。

`det.evidence-v2-core` 运行固定的零模型生产链用例。全部用例通过且没有 skip 后，Test Flow
使用 `evidence-v2-core.mjs` 生成 `core-verdict.json`。该收据绑定：

- Test Flow 冻结的 source snapshot digest；
- `schemas/v2/contract-manifest.json` 的 SHA-256；
- 固定 Core selector 清单及其 SHA-256；
- `pytest-summary.json` 与 `pytest.xml` 的 SHA-256 和计数；
- `model_invocations=0`。

固定清单同时包含真实用户入口的 SameJob 全链路，以及七个 source-overlay mutation 用例。mutation
用例只复制并修改临时 source overlay，再用当前 Python 解释器调用对应的生产回归测试；当前工作树
源码不会被改写。七个 mutant 覆盖 method-qualified marker/index、下游重新匹配 marker、Specialist 错入
Methods V1 分支、第三次角色调用、Workspace hardlink，以及两套生成器的 marker ownership。

`core-verdict.schema.json` 描述收据结构；运行时 validator 还会重新读取上述文件，核对摘要和计数。
它不能单独声明 Release PASS。

## P1/P2 model cert

P1、P2 provider adapter 只写同 Gate 目录下的 `model-cert-input.json`。Test Flow 使用
`evidence-v2-certification.mjs` 的共享 builder 复核输入后，才写出 `model-cert.json`。两份收据使用
完全相同的字段：

- 同一个 source snapshot、V8 contract manifest 和 `core-verdict.json` 摘要；
- `provider.id`、transport、model ID、revision 及 revision 来源；
- Runtime、prompt policy、profile 和 tool policy 的版本与 SHA-256；
- 按实际调用顺序记录的 Specialist/Reviewer primary/repair、每次精确 prompt 摘要和 usage；
- 总调用数、每角色调用数、每角色 repair 数和 `model_retries=0`；
- cache-inclusive 汇总 usage；
- 最终公开 `methods_result` 的 canonical 摘要、大小和稳定身份。

每个角色必须恰好有一次 primary，最多紧跟一次 repair。共享 validator 只接受 2–4 次调用，并重新
计算调用计数与 usage。P1/P2 Gate 都依赖 `deterministic.full`，因此缺少同一次 Test Flow 的 Core
PASS 时不能生成 model cert。

provider adapter 使用以下固定接口：

```js
materializeEvidenceV2ModelCert({
  certificationTarget: "P1" | "P2",
  sourceSnapshotDigest,
  sourceSnapshotRoot,
  attemptRoot,
  gateRoot,
})
```

`model-cert-input.schema.json`、`model-cert.schema.json` 和对应的运行时 validator 共同定义完整合同。
P1 固定为 `deepseek` / `claude-code-compatible-api` / `deepseek-v4-flash[1m]`，revision 使用冻结
settings fingerprint，`revision_source=settings-fingerprint`。P2 固定为 `openai` /
`codex-app-server` / `gpt-5.6-luna`，revision 使用冻结 Codex CLI 与 app-server Runtime fingerprint，
`revision_source=frozen-codex-cli-and-app-server-runtime-identity`；不得把猜测的后端日期版本写入收据。

## release-verdict

`buildEvidenceV2ReleaseVerdict` / `materializeEvidenceV2ReleaseVerdict` 只在同一 source snapshot、同一
V8 manifest、同一 Core PASS、一个 P1 PASS 和一个 P2 PASS 全部一致时生成
`release-verdict.json`。最终收据绑定 Core、两份 model cert、各自 provider/model/revision 以及各自
`methods_result` 身份。当前没有新增 combined Release Goal；零模型框架测试使用确定性 fake receipt
覆盖完整聚合和单字段 mutation。

P1/P2 adapter 仍在迁移。所有仍消费 Methods V1 定位产物的真实 Stage 继续在 planning 阶段返回
`EVIDENCE_V2_REAL_DIAGNOSIS_ADAPTER_UNMIGRATED`，不会调用模型。
