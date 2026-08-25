# macOS Claude/DeepSeek Quick Validation

该流程冻结 Claude Code `2.1.89`、官方 `cli.js` SHA-256、env-only settings fingerprint 和
`deepseek-v4-flash[1m]`。它只支持 Darwin arm64 与 `api-execution-overrun`，不接收 Codex
认证、Docker context、MCP source 或外部 adapter，也不执行 CrossJob、浏览器、重启或业务 REST。

先查看并执行 Methods Bootstrap：

```bash
./tools/test-flow/quick-validation/claude-deepseek/run.sh \
  --goal dev.macos-claude-deepseek-methods \
  --client macos \
  --claude-entry /absolute/cache/claude/2.1.89/package/cli.js \
  --claude-settings /absolute/settings.json \
  --cache-root /absolute/cache \
  --plan-only

./tools/test-flow/quick-validation/claude-deepseek/run.sh \
  --goal dev.macos-claude-deepseek-methods \
  --client macos \
  --claude-entry /absolute/cache/claude/2.1.89/package/cli.js \
  --claude-settings /absolute/settings.json \
  --cache-root /absolute/cache \
  --allow-real-model
```

E2E 只消费完全匹配的 Methods cache，不会自动 Bootstrap：

```bash
./tools/test-flow/quick-validation/claude-deepseek/run.sh \
  --goal dev.macos-claude-deepseek-e2e \
  --client macos \
  --claude-entry /absolute/cache/claude/2.1.89/package/cli.js \
  --claude-settings /absolute/settings.json \
  --cache-root /absolute/cache \
  --logparse-source /absolute/logparse \
  --scenario api-execution-overrun \
  --plan-only

./tools/test-flow/quick-validation/claude-deepseek/run.sh \
  --goal dev.macos-claude-deepseek-e2e \
  --client macos \
  --claude-entry /absolute/cache/claude/2.1.89/package/cli.js \
  --claude-settings /absolute/settings.json \
  --cache-root /absolute/cache \
  --logparse-source /absolute/logparse \
  --scenario api-execution-overrun \
  --allow-real-model
```

Methods 的 producer cache 位于 `<cache-root>/claude-deepseek-methods/<producer-identity>/`，
通过 staging directory 与原子 rename 发布；同 identity 已存在时只能接受完全相同的 manifest
和 package。E2E 从空 `DATA_ROOT` 起服务，恰好聚合 CLIENT、ROUTE、LOGPARSE、DIAGNOSE、
REVIEW 五个 Claude 进程收据。Standalone 结论只看对应 run 根目录的 `verdict.json`。
