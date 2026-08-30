# Ubuntu 22.04 验证入口

这个目录提供两套互不混用的 WSL2 Ubuntu 22.04/Linux x64 容器入口：

- 不传 `--mode` 时，行为保持不变：运行中央 Test Flow 的正式 Evidence V2 Release 认证。
- 显式传入 `--mode fast-e2e --provider codex-luna|claude-deepseek` 时，运行 provider standalone Fast E2E 九场景套件。

Fast E2E 不接中央 Goal、Proof、Stage 或 Gate，不生成 Release verdict，也不能外推成正式发布结论。

## 准备镜像

两种模式使用同一份冻结镜像：

```bash
bash tools/test-flow/quick-validation/wsl/prepare-image.sh \
  --cache-root /home/xiaodao/quick-validation/cache \
  --codex-root /home/xiaodao/quick-validation/cache/codex/0.149.1/bin \
  --logparse-source /home/xiaodao/quick-validation/src/logparse
```

## 默认正式 Release

不传 `--mode` 时，wrapper 仍调用：

```text
tools/test-flow/run.sh
  -> release.evidence-v2-certification
  -> Core
  -> production Skill generation
  -> P1 DeepSeek model-cert
  -> P2 GPT/Luna model-cert
  -> release-verdict.json
```

先看计划：

```bash
bash tools/test-flow/quick-validation/wsl/run.sh \
  --cache-root /home/xiaodao/quick-validation/cache \
  --evidence-root /home/xiaodao/quick-validation/evidence/evidence-v2-release \
  --codex-auth /home/xiaodao/quick-validation/secrets/codex-auth.json \
  --claude-settings /home/xiaodao/quick-validation/secrets/claude-settings.json \
  --plan-only
```

正式结论仍以中央 attempt 的 `verdict.json` 和 `release-verdict.json` 为准。默认分支仍要求两份 provider 凭据，也继续从空数据根运行正式 Release。

## Codex/Luna Fast E2E

先看九场景计划：

```bash
bash tools/test-flow/quick-validation/wsl/run.sh \
  --mode fast-e2e \
  --provider codex-luna \
  --cache-root /home/xiaodao/quick-validation/cache \
  --registration-root /path/to/generated/production-registration \
  --evidence-root /home/xiaodao/quick-validation/evidence/fast-e2e/codex-luna \
  --codex-auth /home/xiaodao/quick-validation/secrets/codex-auth.json \
  --plan-only
```

确认计划后，使用相同参数移除 `--plan-only`，再加入 `--allow-real-model`。

## Claude/DeepSeek Fast E2E

```bash
bash tools/test-flow/quick-validation/wsl/run.sh \
  --mode fast-e2e \
  --provider claude-deepseek \
  --cache-root /home/xiaodao/quick-validation/cache \
  --registration-root /path/to/generated/production-registration \
  --evidence-root /home/xiaodao/quick-validation/evidence/fast-e2e/claude-deepseek \
  --claude-settings /home/xiaodao/quick-validation/secrets/claude-settings.json \
  --plan-only
```

确认计划后，同样移除 `--plan-only` 并加入 `--allow-real-model`。

Fast 模式只要求所选 provider 的凭据，并要求显式传入一份已经生成、校验通过的 production
registration。它不会根据 Release Wiki 推导 producer identity，也不会从生成缓存中猜测 registration。
wrapper 会把该目录只读挂载到场景容器，并调用对应的 standalone 入口：

```text
quick-validation/<provider>/run.sh --goal fast-e2e --all-scenarios --plan-only
```

这一步只读取并核验九场景计划，不调用模型。真实执行时，wrapper 把计划展开成九个相互隔离的容器；每个容器再以 `--goal fast-e2e --scenario <id>` 运行一个场景。

## Fast E2E 调用边界

九场景顺序固定为：

1. `api-execution-overrun`
2. `client-receive-blocked`
3. `deadloop-detected`
4. `insufficient-evidence`
5. `multiple-rpc-timeouts`
6. `server-queue-delay`
7. `server-queue-five`
8. `server-queue-single`
9. `unrelated-log-noise`

调用预算不是九例统一乘法：

- `insufficient-evidence` 在机械预处理后没有属于任何 cause method 的证据，因此模型调用数必须为 0，硬上限也是 0。
- 其余八例正常调用 2 次，每例最多 4 次。
- 九例合计正常调用 16 次，硬上限 32 次。

这个 0 调用边界会直接检查 evidence marker ownership。公共 timeout marker 不能把 `insufficient-evidence` 错误加载到任意 cause method，也不能触发 Specialist 或 Reviewer。

## 容器与结果

九个场景容器并行启动。它们分别拥有独立的进程、网络、`/tmp`、`/private/tmp`、`/root`、scratch、服务端数据根和 evidence 子根。仓库、生成结果 cache、image seal 和所选 provider 凭据以只读方式挂载。

依赖 cache 只用于冻结镜像和 CLI/runtime 依赖，不再提供 Fast E2E 的 registration。结果写入：

```text
<evidence-root>/wsl-<provider>-fast-e2e-<run-id>/
  plan.json
  verdict.json
  scenarios/<scenario>/verdict.json
  evidence/container-runtime/<scenario>/stdout.txt
  evidence/container-runtime/<scenario>/stderr.txt
```

根 `verdict.json` 只做机械聚合：按冻结顺序引用九份 standalone verdict，重算实际调用数和 usage，并核对每个容器的退出码。它明确记录 `goal=fast-e2e`、`source_snapshot=false` 和 `release_verdict=false`。

一个场景出现合同失败时，已经启动的其他八个场景仍会跑完。容器丢失、verdict 身份不匹配、调用数越界或退出码不一致会把根状态置为 `ERROR`。suite 不自动重试。

同一失败身份再次运行时，可以附带：

```bash
--reason "为什么值得再次运行" \
--hypothesis "这次变化应解决什么" \
--expected-evidence "哪些新证据可以证实或证伪该假设"
```
