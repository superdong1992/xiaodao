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
- 同一个 `multiple-rpc-timeouts` 场景身份：原始 Wiki、注册 ID、Skill 内容、用户输入、有序日志
  source 清单，以及生产代码生成的 Evidence Graph 和 Evaluation Plan；
- `provider.id`、transport、model ID、revision 及 revision 来源；
- Runtime、prompt policy、profile 和 tool policy 的版本与 SHA-256；
- 按实际调用顺序记录的 Specialist/Reviewer primary/repair、每次精确 prompt 摘要和 usage；
- 总调用数、每角色调用数、每角色 repair 数和 `model_retries=0`；
- cache-inclusive 汇总 usage；
- 最终公开 `methods_result` 的 canonical 摘要、大小和稳定身份。

两家 adapter 还必须把 production driver 产生的原始字节留在同一个 Gate 根：source/reviewer Job、
Evidence Graph、Evaluation Plan、limitations、source/terminal state、两份 Outcome，共九个 execution
record；另保留公开 `methods-result-v2.json`，以及 Runtime 实际加载 registration 中逐字复制的
`methods.json`。`scenario-oracle-receipt.json` 绑定这些文件和 frozen release case/oracle。

`validateEvidenceV2ModelCert(..., certRoot)` 不信任 receipt 的 `status`。每次验证都会从 `sourceRoot`
重新读取 release case/oracle，从 `certRoot` 重新读取全部原件，按 provider role receipt 映射 source 与
reviewer Job，再调用 `validateMethodsV2ExecutionRecords` 完整复核 method-qualified Graph、Plan 全覆盖、
盲评共识、limitations、Outcome 和公开投影。删除任一原件、漏掉 evidence identity、改变 Graph/Plan、
改变方法 marker，或两角色一致确认错误方法，都会让认证失败。

场景摘要的 preimage 固定如下，provider 不得各自解释：

- `source_wiki_sha256` 逐字取已加载 `methods.json` 绑定的原始 Wiki SHA-256；
- `skill_content_sha256` 取生产 `source_job.skill_ref.content_hash`，该值必须同时等于已加载 Skill 的
  `combined_sha256`、Evidence Graph 的 `skill_sha256` 和 Evaluation Plan 的 `skill_sha256`，不是
  registration 目录 tree digest；
- `user_inputs_sha256` 对场景 `driver.json` 的原始等长数组投影
  `{initial_user_fact_names,initial_user_fact_values}` 计算共享 `canonicalJson` UTF-8 SHA-256；对象 key
  递归排序、JSON 不加空白、末尾恰好一个 LF，不使用 Runtime 重命名或展开后的 facts；
- `sources` 保持生产 Evidence Graph 的 source 顺序，只投影 `source_id` 和 `content_sha256`；
- Graph/Plan 摘要和大小取生产 canonical JSON 的完整对象字节。

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
`release-verdict.json`。最终收据绑定 Core、两份 model cert、各自 provider/model/revision、各自
scenario oracle binding 以及各自 `methods_result` 身份。release verdict 只公开一份共同的 `scenario`；聚合器会按 canonical JSON 逐字
比对 P1、P2 的完整场景身份，并校验各自 `methods_result` 的 Graph/Plan ref。任一 Skill、日志 source、
Graph 或 Plan 不同都不能聚合为 PASS。当前没有新增 combined Release Goal；零模型框架测试使用
确定性 fake receipt 覆盖完整聚合和从合法 baseline 开始的单字段 mutation。

P1/P2 model-cert Stage 都依赖同一次 `real.skill-generation` 和 `deterministic.full`，两家必须消费同一个
production registration 根；任一依赖没有 PASS 时都不会生成 release verdict。
