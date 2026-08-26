# Claude/DeepSeek Standalone Fast E2E

该流程冻结 Claude Code `2.1.89`、官方 `cli.js` SHA-256、env-only settings fingerprint 和 `deepseek-v4-flash[1m]`。它不执行中央 CrossJob、浏览器、重启或业务 REST，支持原生 macOS arm64 和密封 Ubuntu 22.04 Linux/x64 容器。

先独立规划并生成 Methods cache：

```bash
./tools/test-flow/quick-validation/claude-deepseek/run.sh \
  --goal dev.macos-claude-deepseek-methods \
  --client macos --plan-only
```

E2E 不自动 Bootstrap。单场景和九场景 suite 分别使用：

```bash
./tools/test-flow/quick-validation/claude-deepseek/run.sh \
  --goal dev.macos-claude-deepseek-e2e --client macos \
  --logparse-source /absolute/logparse \
  --scenario api-execution-overrun --plan-only

./tools/test-flow/quick-validation/claude-deepseek/run.sh \
  --goal dev.macos-claude-deepseek-e2e --client macos \
  --logparse-source /absolute/logparse \
  --all-scenarios --plan-only
```

确认计划后用相同参数移除 `--plan-only` 并加入 `--allow-real-model`。suite 与 Codex 使用相同九场景；`insufficient-evidence` 没有 REVIEW，共四个 Claude 进程，其余每例五个进程，总计 44 个进程。

每例从空 `DATA_ROOT` 启动。suite 根 `verdict.json` 绑定九份子 verdict、模型进程数和聚合 usage；业务/oracle 失败继续，工程失败停止，无自动重试。Standalone 结论只证明对应 Fast E2E，不等同于中央 Test Flow 或 Release。

以上停止语义适用于 provider 原生串行 suite。经 `wsl/run.sh --all-scenarios` 执行时，wrapper 会在共同 plan 和确定性预检通过后同时启动九个容器，每个容器只调用本入口的一个 `--scenario`；九个容器结束后再生成 WSL 聚合 verdict。
