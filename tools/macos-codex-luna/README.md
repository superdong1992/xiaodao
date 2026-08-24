# macOS Codex Luna Fast E2E

这是独立于 `tools/test-flow` orchestrator 的轻量测试工程。它只复用 Codex App Server、
MCP 和 Methods 的协议实现，不读取旧 Goal/Proof/Stage/Gate 配置，不创建 Git 源码快照，
不查询历史 verdict，也不进入旧 finalization。

该 Dev 冒烟不把权限或网络隔离强度作为验收目标。为保护个人认证信息，原始 credential
不得进入 prompt、计划、证据或 verdict；内存登录、零认证文件持久化和 secret scan 仍是
必需的隐私边界。

入口：

```bash
./tools/macos-codex-luna/run.sh --goal methods --plan-only
./tools/macos-codex-luna/run.sh --goal methods --allow-real-model

./tools/macos-codex-luna/run.sh --goal e2e \
  --scenario api-execution-overrun \
  --logparse-root /absolute/path/to/logparse \
  --plan-only
./tools/macos-codex-luna/run.sh --goal e2e \
  --scenario api-execution-overrun \
  --logparse-root /absolute/path/to/logparse \
  --allow-real-model
```

每次 run 的计划、工作目录、私有运行态和证据只写入
`.tmp/macos-codex-luna/runs/<run-id>/`：

```text
plan.json
work/
private/
usage/
evidence/
  gate-receipt.json
verdict.json
```

`verdict.json` 是该独立工程的唯一结论。真实模型不自动重试。Methods cache 已存在时，
Methods Goal 只进行身份、validator、tree digest 与 secret 的确定性复核，不重复调用模型。
首次 Methods bootstrap 另写入 `.tmp/macos-codex-luna/cache/`，供后续 E2E 精确复用。
