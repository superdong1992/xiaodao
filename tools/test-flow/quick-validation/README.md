# Quick Validation

本目录收纳不进入 Release/CrossJob 闭包的 macOS arm64 快速端到端验证。每个 provider
拥有独立的 runner、runtime、tests、证据与轻量 verdict；两套流程不共享认证、模型调用
或缓存身份。通用 Codex app-server、探索 runner、schema 和正式 Claude runtime profile 继续
由 `tools/test-flow/runtime-support/` 与 `tools/test-flow/config/` 持有。

- `codex-luna/`：冻结 Codex CLI + `gpt-5.6-luna` 的 Methods Bootstrap 与单场景 MCP E2E。
- `claude-deepseek/`：冻结 Claude Code + `deepseek-v4-flash[1m]` 的对应流程。

公共中央 Goal：

```text
dev.macos-codex-luna-methods
dev.macos-codex-luna-e2e
dev.macos-claude-deepseek-methods
dev.macos-claude-deepseek-e2e
```

中央入口统一使用：

```bash
./tools/test-flow/run.sh --track dev --goal <上述-goal> <provider-inputs> --allow-real-model --plan-only
./tools/test-flow/run.sh --track dev --goal <上述-goal> <provider-inputs> --allow-real-model
```

`<provider-inputs>` 的冻结参数和独立入口命令分别见 `codex-luna/README.md` 与
`claude-deepseek/README.md`。真实执行前必须先查看同一组输入的 `--plan-only`。

各 provider 也保留自己的 `run.sh`，便于只规划或只验证该 provider。Standalone 证据默认写到
`.tmp/quick-validation/<provider>/runs/<run-id>/`，缓存默认写到
`.tmp/quick-validation/<provider>/cache/`；中央 Goal 则使用 `--evidence-root` 与 `--cache-root`。

Quick Validation 只证明自身计划中声明的本机 smoke，不证明 Docker、浏览器、重启、
Windows/Linux Client、完整 Test Flow Release 或其他 provider 的状态。
