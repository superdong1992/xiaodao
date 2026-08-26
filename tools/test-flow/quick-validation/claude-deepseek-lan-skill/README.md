# 局域网 Logparse Skill 独立 Fast E2E

该入口验证 `.claude/skills/wiki-to-logparse-diagnosis-skill`，不接入中央 Test Flow，也不产生
Release 结论。它复用已冻结的 Claude Code `2.1.89`、env-only settings identity 和
`deepseek-v4-flash[1m]`，支持原生 macOS arm64 与密封 Ubuntu 22.04 Linux/x64 容器。

先规划并生成不可变定位 Skill cache：

```bash
./tools/test-flow/quick-validation/claude-deepseek-lan-skill/run.sh \
  --goal generation --plan-only
```

确认身份、一次模型调用、token/USD 上限和 admission 后，用相同参数移除 `--plan-only` 并加入
`--allow-real-model`。再次运行相同身份时只校验 cache，不重复调用模型。

诊断目标不自动生成 cache。可分别规划缺 slot 与完整输入场景，或运行完整 suite：

```bash
./tools/test-flow/quick-validation/claude-deepseek-lan-skill/run.sh \
  --goal diagnosis --scenario missing-slots --plan-only

./tools/test-flow/quick-validation/claude-deepseek-lan-skill/run.sh \
  --goal diagnosis --scenario complete --plan-only

./tools/test-flow/quick-validation/claude-deepseek-lan-skill/run.sh \
  --goal diagnosis --all-scenarios --plan-only
```

`missing-slots` 必须直接请求 `client_slot`、`server_slot`，不得加载 Helper、调用 broker 或生成
ZIP。`complete` 加载仓库当前 `logparse-diagnose`，但只把 `problem-locator-logparse` 后端替换为
仓库自有合同桩；合同桩核对两个 slot 和完整 anchors，返回冻结目标日志。该目标不执行真实上游
Logparse，也不修改产品 Server。

每个场景使用 fresh Claude 进程，`retry_count=0`。工程或 provider 失败立即封存并停止，不自动
重试；合同失败可在修正源码后携带新的 `--reason`、`--hypothesis`、`--expected-evidence` 重新运行。
所有结果写入独立 `verdict.json`，只证明本入口声明的 Fast E2E。
