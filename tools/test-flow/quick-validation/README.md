# Standalone Fast E2E

本目录提供开发期 Fast E2E。每个 provider 拥有独立 planner、runner、runtime、tests、缓存、证据和轻量 `verdict.json`，不读取中央 Goal/Proof/Stage/Gate，也不进入 Release/CrossJob finalization。

- `codex-luna/`：Codex CLI + `gpt-5.6-luna`。
- `claude-deepseek/`：Claude Code + `deepseek-v4-flash[1m]`。
- `wsl/`：在密封 Ubuntu 22.04 Linux/x64 容器中运行上述 standalone 入口。

两套 E2E 都支持仓库冻结的九个 RPC 场景。单场景用于定位问题；provider 原生 `--all-scenarios` 顺序执行九例，生成九份子 verdict 和一份聚合 verdict。WSL wrapper 的 `--all-scenarios` 则把九个单场景分发到九个相互隔离的 Docker 容器并发执行，再机械聚合相同的九场景结论。Codex/Luna 继续消费 Methods package cache；Claude/DeepSeek 改为消费一次真实 generation 生成的完整 registration cache。两者都必须匹配完整 producer identity，suite 不会自动生成。Linux planner 同时校验密封 marker、Ubuntu 22.04 系统身份、冻结 image seal 和 wrapper 专属 tmpfs scratch；普通 Linux、缺少 scratch 或伪造任一身份都会在规划阶段阻断。

standalone verdict 是对应 Fast E2E 的开发结论，只证明计划中声明的 provider、平台、场景和调用边界。正式 Test Flow、Release、源码快照和修复登记仍使用 `tools/test-flow/run.sh` 或 `run.ps1`。中央 macOS Quick Goal 仅保留为可选认证能力，不是 Fast E2E 默认入口。

真实模型执行前必须先运行同参数的 `--plan-only`。suite 内不自动重试。原生串行 suite 遇到工程失败后停止后续场景；WSL 九容器越过共同预检后已全部启动，因此不会强杀同伴，而是等待已启动容器封存，再把聚合状态置为 `ERROR`。修复失败后重跑时，入口会生成新 run ID；调用方还必须提供新的 `--reason`、`--hypothesis` 和 `--expected-evidence`，并先复核新 plan。
