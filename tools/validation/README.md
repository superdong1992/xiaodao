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

P1/P2 model cert 尚未迁移。所有仍消费 Methods V1 定位产物的真实 Stage 都在 planning 阶段返回
`EVIDENCE_V2_REAL_DIAGNOSIS_ADAPTER_UNMIGRATED`，不会调用模型。迁移要求记录在根目录
`TODO.md`。
