# Codex Luna Standalone Fast E2E

该轻量工程独立于中央 `tools/test-flow` 编排器，只复用 Codex App Server、MCP 和 Methods 协议实现。它不读取 Goal/Proof/Stage/Gate，不创建完整 Git 源码快照，不查询历史 verdict，也不进入中央 finalization。

原始 credential 不得进入 prompt、计划、证据或 verdict；内存登录、零认证文件持久化和 secret scan 仍是必需边界。支持原生 macOS arm64，以及 `wsl/run.sh` 创建的密封 Ubuntu 22.04 Linux/x64 容器。

Methods 先规划再执行：

```bash
./tools/test-flow/quick-validation/codex-luna/run.sh --goal methods --plan-only
./tools/test-flow/quick-validation/codex-luna/run.sh --goal methods --allow-real-model
```

单场景调试：

```bash
./tools/test-flow/quick-validation/codex-luna/run.sh \
  --goal e2e --scenario api-execution-overrun \
  --logparse-root /absolute/logparse --plan-only
```

九场景 suite：

```bash
./tools/test-flow/quick-validation/codex-luna/run.sh \
  --goal e2e --all-scenarios \
  --logparse-root /absolute/logparse --plan-only
```

确认计划后用相同参数移除 `--plan-only` 并加入 `--allow-real-model`。suite 固定执行 `api-execution-overrun`、`client-receive-blocked`、`deadloop-detected`、`insufficient-evidence`、`multiple-rpc-timeouts`、`server-queue-delay`、`server-queue-five`、`server-queue-single`、`unrelated-log-noise`。其中 `insufficient-evidence` 为四阶段，其余为五阶段，总计 44 次模型调用。

每个场景使用独立空 `DATA_ROOT`。suite 根 `verdict.json` 聚合九份 `scenarios/<id>/verdict.json`；业务/oracle 失败继续，工程失败停止，真实模型不自动重试。Methods cache 缺失或身份漂移时在首个模型调用前阻断。

以上停止语义适用于 provider 原生串行 suite。经 `wsl/run.sh --all-scenarios` 执行时，wrapper 会在共同 plan 和确定性预检通过后同时启动九个容器，每个容器只调用本入口的一个 `--scenario`；九个容器结束后再生成 WSL 聚合 verdict。
