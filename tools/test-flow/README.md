# Test Flow 操作说明

本目录是仓库测试活动的唯一操作入口。测试架构及不变量见
[`design/test-flow-architecture.md`](../../design/test-flow-architecture.md)；本文件只说明如何规划、运行和读取结果，不复制底层 pytest 命令。

## 公开 Goal

| Goal | Track | 含义 |
| --- | --- | --- |
| `dev.default` | `dev` | 框架自测、仓库静态检查、受影响确定性测试和完整确定性测试；不调用真实模型 |
| `dev.real` | `dev` | 在 `dev.default` 闭包之外，显式选择一个真实 Proof/Stage |
| `release.full` | `release` | 不可变源码快照上的完整发布证明；包含平台能力和从空数据根开始的 fresh CrossJob |

Windows、macOS 和显式 Linux Client 都有仓库内置 adapter。`--client auto` 在当前主机上选择对应 adapter；所有 Client 都通过 HTTP 直连 Linux Server。adapter 不是任意命令扩展点，调用方不能注入外部执行器。

## Dev 确定性测试

先看计划，再运行同一 Goal：

```sh
./tools/test-flow/run.sh --track dev --goal dev.default --plan-only
./tools/test-flow/run.sh --track dev --goal dev.default
```

PowerShell 使用同参数的 `tools/test-flow/run.ps1`。计划会列出 Goal、Proof、Stage、Gate、依赖、身份、复用决定、性能身份、预计资源和 admission blocker。

## Dev 真实测试

`dev.real` 必须选择一个 Stage，并显式允许真实模型。第一次运行同样先看计划：

```sh
./tools/test-flow/run.sh \
  --track dev \
  --goal dev.real \
  --stage real.route \
  --allow-real-model \
  --logparse-source /absolute/path/to/logparse \
  --mcp-source /absolute/path/to/problem-locator-mcp \
  --claude-entry /absolute/path/to/claude-code/package/cli.js \
  --claude-settings /absolute/path/to/claude/settings.json \
  --plan-only
```

移除 `--plan-only` 才执行。相同失败身份再次运行时必须新增三个结构化字段：

```sh
--reason "为什么值得再次运行" \
--hypothesis "这次修复或环境变化应解决什么" \
--expected-evidence "哪些新证据可以证实或证伪该假设"
```

这三个字段是可审计的重试合同，不允许盲重试。

## Release

Release planning 会冻结当前 Git 可见工作树的 exact source snapshot；工作树可以尚未提交。tracked 文件使用当前字节，未忽略的 untracked 文件也会进入清单，ignored 文件不会进入。运行期间任何源码漂移都会使 verdict 成为 `ERROR`。先准备由
[`config/runtime-profiles.v2.json`](config/runtime-profiles.v2.json) 冻结的 Claude、uv 和 Linux base image 缓存；准备阶段可以联网，正式 Release 使用已封存缓存且不拉取镜像：

```sh
node tools/test-flow/prepare-release-cache.mjs \
  --repo-root /absolute/path/to/problem-locator \
  --cache-root /absolute/path/to/test-flow-cache \
  --docker-context colima
```

然后把 `PROFILE_VERSION` 替换为该配置中的 `claude.version`，用完全相同的输入先规划、再执行：

```sh
./tools/test-flow/run.sh \
  --track release \
  --goal release.full \
  --client macos \
  --resume fresh \
  --logparse-source /absolute/path/to/logparse \
  --mcp-source /absolute/path/to/problem-locator-mcp \
  --claude-entry /absolute/path/to/test-flow-cache/claude/PROFILE_VERSION/package/cli.js \
  --claude-settings /absolute/path/to/claude/settings.json \
  --docker-context colima \
  --cache-root /absolute/path/to/test-flow-cache \
  --plan-only

./tools/test-flow/run.sh \
  --track release \
  --goal release.full \
  --client macos \
  --resume fresh \
  --logparse-source /absolute/path/to/logparse \
  --mcp-source /absolute/path/to/problem-locator-mcp \
  --claude-entry /absolute/path/to/test-flow-cache/claude/PROFILE_VERSION/package/cli.js \
  --claude-settings /absolute/path/to/claude/settings.json \
  --docker-context colima \
  --cache-root /absolute/path/to/test-flow-cache
```

Windows 使用 `--client windows`，显式 Linux Client 使用 `--client linux`；两者不接受 macOS 的 Docker context。仓库拥有三种 adapter 的相同 Gate receipt 合同，但一次 Release 只证明 verdict 中记录的实际平台、source snapshot digest 和全部输入身份，不能外推成未执行平台的真实 PASS。

`verdict.json` 会同时记录 snapshot digest、base Git SHA、branch 和 planning 时的 dirty 状态。Git 提交不是 Release admission 条件；推荐在全部 Proof 通过后再提交完全相同的快照。提交若改变任何 Git 可见 path 或字节，原 verdict 不再证明新内容，必须重新运行 Release。

Release 从 GENESIS 和新的空 `DATA_ROOT` 开始，不复用业务 checkpoint。它执行一条 CrossJob：Environment、Route、Upload、Diagnose、自动 Review、Publish/Restart，并同时证明真实 Agent、真实 Logparse、七工具扁平 schema、服务端 DFX、安装分发、重启恢复和证据完整性。

## 预算、超时与性能

真实 Gate 的计划列出模型、turn/token/USD/time 上限和预计成本。turn、USD 与进程时限由执行器或 provider 强制；token 上限还会由终端 receipt 复核，usage 缺失或超限不能 PASS。

只有 allowlist 中的语义事件能刷新无进展计时。硬时限始终生效。性能使用同一版本化策略累计样本：样本不足是 `NOT_CALIBRATED`；Dev 回归告警；Release 同一性能身份第一次显著变慢为 warning，连续第二次才失败。复用 Stage 不产生性能样本。

## Verdict 与退出码

每次执行的证据位于 `.tmp/test-flow-evidence/<run-id>`。`verdict.json` 最后原子创建，是唯一权威结论；缺少它就是 `UNFINALIZED`。verdict 包含 Proof、Stage、Gate、配置/策略/身份摘要、DFX/资源/密钥扫描审计以及最终决策输入摘要。验证器会从密封 receipt 重新计算结论，不信任可单独编辑的摘要字段。

- `0`：`PASS` 或 `PASS_WITH_WARNINGS`
- `1`：功能失败或持续的 Release 性能回归
- `2`：`BLOCKED` 或 `INCONCLUSIVE`
- `3`：框架、证据、安全、finalization 或清理错误

清理失败会使本次运行成为 `ERROR` 且证据不可复用，即使业务 Gate 已经完成。

## 证据保留

证据从不自动删除。先报告或 dry-run，再精确删除某个 run：

```sh
node tools/test-flow/evidence.mjs report
node tools/test-flow/evidence.mjs report --run-id run-YYYYMMDDTHHMMSSZ-1234abcd
node tools/test-flow/evidence.mjs prune --dry-run --keep-last 10
node tools/test-flow/evidence.mjs prune --run-id run-YYYYMMDDTHHMMSSZ-1234abcd --execute
```

`report` 只会把按当前验证器重审为 PASS 的证据标成可复用；旧合同或篡改证据统一标为 `MANUAL_REVIEW`。被其他有效 verdict 引用的 executed source receipt 默认拒绝删除。复用只能直接引用已重新验证的原始 executed receipt，不能形成复用链。

## 配置权威

当前 runner 只读取六份 schema v2 配置：

- [`proofs.v2.json`](config/proofs.v2.json)：Goal 与 Proof 闭包；
- [`stages.v2.json`](config/stages.v2.json)：DAG、复用、checkpoint 和平台适用性；
- [`gates.v2.json`](config/gates.v2.json)：allowlisted 原子验证器及 evidence contract；
- [`identities.v2.json`](config/identities.v2.json)：正交身份组件和集合；
- [`policy.v2.json`](config/policy.v2.json)：admission、重试、状态、性能和证据限制；
- [`runtime-profiles.v2.json`](config/runtime-profiles.v2.json)：官方运行时、模型、镜像、外部源码和环境 allowlist 冻结值。

未知字段、悬空引用、不可达 Stage、任意命令、越出仓库的 selector 或身份覆盖缺口都会在执行前 fail closed。
