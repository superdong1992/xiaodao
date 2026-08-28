# Ubuntu 22.04 Evidence V2 正式认证入口

这个 wrapper 只负责把中央 Test Flow 放进密封的 WSL2 Ubuntu 22.04/Linux x64 容器运行。认证逻辑只有一套，入口始终是：

```text
tools/test-flow/run.sh
  -> release.evidence-v2-certification
  -> Core
  -> 1 次 production Skill generation
  -> P1 DeepSeek model-cert
  -> P2 GPT/Luna model-cert
  -> release-verdict.json
```

P1、P2 使用同一 attempt 内生成的 production registration，也绑定同一 source snapshot 和 Core verdict。WSL wrapper 不再展开九场景，不生成独立 verdict，也不调用 provider standalone 编排器。

镜像仍从冻结的本地缓存构建：

```bash
bash tools/test-flow/quick-validation/wsl/prepare-image.sh \
  --cache-root /home/xiaodao/quick-validation/cache \
  --codex-root /home/xiaodao/quick-validation/cache/codex/0.149.1/bin \
  --logparse-source /home/xiaodao/quick-validation/src/logparse
```

先看正式计划：

```bash
bash tools/test-flow/quick-validation/wsl/run.sh \
  --cache-root /home/xiaodao/quick-validation/cache \
  --evidence-root /home/xiaodao/quick-validation/evidence/evidence-v2-release \
  --codex-auth /home/xiaodao/quick-validation/secrets/codex-auth.json \
  --claude-settings /home/xiaodao/quick-validation/secrets/claude-settings.json \
  --plan-only
```

计划由中央编排器原样输出。检查以下字段后，使用完全相同的参数移除 `--plan-only`：

- Goal 是 `release.evidence-v2-certification`，scenario 是 `multiple-rpc-timeouts`；
- `real.skill-generation` 正常调用数为 1；
- P1、P2 各自正常调用数为 2、repair 上限为 2、总硬上限为 4；
- 整个认证正常调用数为 5，含 repair 的总硬上限为 9；
- provider/model/revision、token 和费用预算符合预期；
- admission 没有 blocker。

真实执行结果写入 `--evidence-root` 下的正式 Test Flow attempt。权威结论仍是 attempt 根目录的 `verdict.json`，Evidence V2 的最终认证收据是该 attempt 内 `evidence-v2.release-verdict` Gate 生成的 `release-verdict.json`。

同一失败身份再次运行时，把中央 Test Flow 要求的三项说明传给 wrapper：

```bash
--reason "为什么值得再次运行" \
--hypothesis "这次变化应解决什么" \
--expected-evidence "哪些新证据可以证实或证伪该假设"
```

wrapper 会把当前 WSL 用户的 uid/gid 原样传给容器，不再用 root 运行。正式认证前，它会先用同一身份做一次零模型预检，确认 Codex CLI、Claude CLI、Python、两份模型凭据和 `/evidence` 都可用。预检不会发起模型请求。

容器只读挂载仓库和 dependency cache；正式 Stage 的所有输出都写入可写的 `/evidence`。Skill generation 的 registration 位于当前 attempt，不会写回 cache，因此 cache 只读不会阻止认证。容器退出后，wrapper 还会检查当前 attempt 的 `verdict.json` 是否仍归调用用户所有且可读，并保留中央 Test Flow 的原始退出码。
