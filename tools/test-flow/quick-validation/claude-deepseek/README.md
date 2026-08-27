# Claude/DeepSeek Standalone Fast E2E

该流程冻结 Claude Code `2.1.89`、官方 `cli.js` SHA-256、env-only settings fingerprint 和 `deepseek-v4-flash[1m]`。它不执行中央 CrossJob、浏览器、重启或业务 REST，支持原生 macOS arm64 和密封 Ubuntu 22.04 Linux/x64 容器。

先独立规划，并用一次真实模型调用生成完整 registration cache（包含 `registration-template.json` 和 `package/`，固定 `module=rpc`）：

```bash
./tools/test-flow/quick-validation/claude-deepseek/run.sh \
  --goal dev.macos-claude-deepseek-methods \
  --client macos --plan-only
```

E2E 不自动生成 registration。默认单场景是 `api-execution-overrun`；也可以显式选择单场景或运行九场景 suite：

```bash
./tools/test-flow/quick-validation/claude-deepseek/run.sh \
  --goal dev.macos-claude-deepseek-e2e --client macos \
  --logparse-source /absolute/logparse --plan-only

./tools/test-flow/quick-validation/claude-deepseek/run.sh \
  --goal dev.macos-claude-deepseek-e2e --client macos \
  --logparse-source /absolute/logparse \
  --all-scenarios --plan-only
```

确认计划后用相同参数移除 `--plan-only` 并加入 `--allow-real-model`。suite 与 Codex 使用相同九场景；`insufficient-evidence` 没有 REVIEW，共四个 Claude 进程，其余每例五个进程，总计 44 个进程。

每例从空 `DATA_ROOT` 启动。客户端只安装 `problem-locator-client`，经 HTTP MCP 操作 Linux Server；同一个客户端模型在 `list_artifacts` 后按返回的 `download_url` 下载 `result.zip`，并核对 size、SHA-256 和 Server v3 ZIP 内容。Server LOGPARSE trace 必须证明先加载一次 `logparse-diagnose`，再执行一次 job-scoped broker；业务 Methods Skill 只消费冻结日志。

DIAGNOSE 和 REVIEW 的测试 wrapper 只记录 Agent 原始草稿的大小、SHA-256 与是否已经 canonical，绝不改写草稿；JSON 解析、schema 校验和 Canonical JSON 规范化必须由产品 Runtime 完成。Fast E2E 必须保留 `harness_normalized=false`，不得用测试代码替产品修正输出。

suite 根 `verdict.json` 绑定九份子 verdict、模型进程数和聚合 usage；业务/oracle 失败继续，工程失败停止，无自动重试。Standalone 结论只证明对应 Fast E2E，不等同于中央 Test Flow 或 Release。

以上停止语义适用于 provider 原生串行 suite。经 `wsl/run.sh --all-scenarios` 执行时，wrapper 会在共同 plan 和确定性预检通过后同时启动九个容器，每个容器只调用本入口的一个 `--scenario`；九个容器结束后再生成 WSL 聚合 verdict。
