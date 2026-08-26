# Ubuntu 22.04 Standalone Fast E2E

该 wrapper 在 WSL2 Ubuntu 22.04 的密封 Linux/x64 容器中运行 provider standalone Fast E2E。它不调用中央 `tools/test-flow/run.sh`，也不创建 Goal/Proof/Stage/Gate。

镜像仍从冻结本地缓存构建：

```bash
bash tools/test-flow/quick-validation/wsl/prepare-image.sh \
  --cache-root /home/xiaodao/quick-validation/cache \
  --codex-root /home/xiaodao/quick-validation/cache/codex/0.149.1/bin \
  --logparse-source /home/xiaodao/quick-validation/src/logparse
```

Codex 九场景计划：

```bash
bash tools/test-flow/quick-validation/wsl/run.sh \
  --provider codex-luna --mode e2e --all-scenarios \
  --cache-root /home/xiaodao/quick-validation/cache \
  --evidence-root /home/xiaodao/quick-validation/evidence/standalone-fast-e2e/codex-luna \
  --codex-auth /home/xiaodao/quick-validation/secrets/codex-auth.json \
  --plan-only
```

Claude 九场景计划：

```bash
bash tools/test-flow/quick-validation/wsl/run.sh \
  --provider claude-deepseek --mode e2e --all-scenarios \
  --cache-root /home/xiaodao/quick-validation/cache \
  --evidence-root /home/xiaodao/quick-validation/evidence/standalone-fast-e2e/claude-deepseek \
  --claude-settings /home/xiaodao/quick-validation/secrets/claude-settings.json \
  --plan-only
```

Methods cache 缺失时改用 `--mode methods` 单独规划和执行。真实 E2E 必须先检查 plan；确认后移除 `--plan-only` 并加入 `--allow-real-model`。

局域网 Logparse 元 Skill 使用独立 provider。先规划 generation，再规划 diagnosis；diagnosis 只读
generation cache，不会自动生成：

```bash
bash tools/test-flow/quick-validation/wsl/run.sh \
  --provider claude-deepseek-lan-skill --mode generation \
  --cache-root /home/xiaodao/quick-validation/cache \
  --evidence-root /home/xiaodao/quick-validation/evidence/lan-skill-generation \
  --claude-settings /home/xiaodao/quick-validation/secrets/claude-settings.json \
  --plan-only

bash tools/test-flow/quick-validation/wsl/run.sh \
  --provider claude-deepseek-lan-skill --mode diagnosis --all-scenarios \
  --cache-root /home/xiaodao/quick-validation/cache \
  --evidence-root /home/xiaodao/quick-validation/evidence/lan-skill-diagnosis \
  --claude-settings /home/xiaodao/quick-validation/secrets/claude-settings.json \
  --plan-only
```

该 provider 的两个诊断场景很小，由 provider 自己在同一个隔离容器中串行执行，不进入下文的九容器
并行矩阵。它仍使用只读仓库、generation 时可写 cache、diagnosis 时只读 cache，以及独立 evidence
和 tmpfs scratch。

`--all-scenarios` 由 wrapper 在容器层展开：先用 provider 的九场景 planner 做一次零模型规划，再运行一次共享确定性预检；全部通过后，同时启动九个 Docker 容器。每个容器只执行一个 provider `--scenario`，拥有独立的 PID/网络命名空间、`/tmp`、`/private/tmp`、`/root`、`/run/test-flow-scratch`、服务端口、`DATA_ROOT` 和 evidence 子根。仓库、Methods cache、image seal 和 provider credential 只以受控 bind mount 共享。

持久化结果写到 `<evidence-root>/wsl-<provider>-suite-<run-id>/`：九份权威子结论位于 `scenarios/<scenario>/verdict.json`，根 `verdict.json` 按冻结顺序重算调用数和 usage。合同失败不影响其他已启动容器；工程失败也不强杀同伴，待九个已启动容器封存后把根状态置为 `ERROR`。suite 不自动重试。

wrapper 在启动容器前核验冻结 image seal 和镜像 ID，容器内 planner 再核验 marker、Ubuntu 22.04 系统身份、挂载的 image seal 和独立 tmpfs scratch。可执行的 work/private 放在临时 scratch，evidence、usage 和 verdict 才写入持久化目录，避免把 Linux sandbox workspace 放进 evidence bind mount。容器使用只读仓库、独立可写 cache/evidence、provider 专属只读凭据、只读根文件系统和 Docker init。Codex 单独启用现有 `seccomp=unconfined`，不增加 privileged、capability、Docker socket、host PID 或 host network。每个场景的服务端仍从空数据根启动，凭据不得进入证据。
