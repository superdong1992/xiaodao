# Result v2 测试简报（2026-08-08）

对应代码提交：`0bc76e0864cfbd9d1163716be4d4d760edac2a4b`

## 已通过

- Contracts：`553 passed`。
- Application + Integration + Contracts 汇总：`840 passed, 1 skipped`。
- 非 Storage Unit：`1311 passed, 38 skipped`。
- Runtime / Result archive 定向回归：`99 passed`。
- 真实 RPC initial + continuation E2E：`3 passed`；最后的 Logparse raw/target 分离与清单回归：`5 passed`。
- E2E harness 静态自检：`E2E_HARNESS_TESTS_PASSED`。
- Official Fast `attempt125-20260808-204038`：`PASS`。覆盖主流程、same-job continuation、Result v2 JSON/ZIP、目标日志字节与顺序、HTTP 前后、重启下载、State audit 和 secret scan。

## ReleaseGates 与后续复验

- Official ReleaseGates `attempt126-20260808-205758`：确定性组报告 `2461 tests`，失败集中为 3 个唯一问题：contract manifest 换行哈希、Storage fixture 的旧文件未从冻结补丁删除、RPC fixture 混用了 raw 日志与 Logparse target 日志。
- 上述 3 项均已修复；对应 Linux 定向复验为 `5 passed`，harness 静态门再次通过。
- 修复后的 Official Fast `attempt127`–`attempt131` 均通过 freeze、environment 和 service preflight；业务阶段因外部 `node/deepseek` Agent 的 300 秒超时未形成完整 PASS，因此没有再次启动 ReleaseGates。

## E2E 流程耗时与缺陷

成功的 Fast attempt125 总耗时 `979.5s`（约 16 分 20 秒）。其中 4 个真实 Agent 阶段分别耗时 `206.8s / 236.8s / 296.2s / 157.2s`，合计 `897s`，占总耗时约 `91.6%`；environment 与 service preflight 合计仅约 `21s`。主要问题不是构建，而是业务旅程被外部模型时延主导。

- 固定墙钟超时贴近真实分布：成功 continuation phase1 已耗时 `296.2s`，attempt130 为 `300.9s`，不到 5 秒差异就会把完整运行判成失败。
- 失败不能阶段续跑：主流程已通过后，continuation 超时仍需从 freeze、主 phase1、上传和主 phase3 全部重跑。
- 输出缓冲导致不可观测：Agent 运行期间结果文件长期为 0 字节，门禁无法区分“仍在推理”和“已经卡死”。
- 门禁顺序成本倒挂：昂贵的真实 Agent Fast 先跑，ReleaseGates 的确定性全量测试后跑；attempt126 的清单/RPC 问题本可在业务旅程前发现。
- Patch identity 耦合过宽：仅测试、fixture 或 harness 修正也会让既有 Fast 业务证据失效，强制重新执行全部真实 Agent 流程。
- initial 与 continuation 各执行一套 phase1/phase3，重复验证大量相同能力，导致外部模型调用次数和失败概率同时翻倍。

建议后续把流程调整为：先跑静态、合同、RPC 和确定性全量门；再运行真实 Agent；按 patch SHA 保存阶段 checkpoint，只重跑失败阶段；将产品/schema identity 与测试/harness identity 分离；使用心跳/无进展超时替代单一墙钟超时；真实 Agent 只承担最小业务体验抽样，其余合同由确定性 Agent 覆盖。

## 平台说明

- Windows 全量 Storage 测试结果为 `250 passed, 2 skipped, 42 failed`；42 项集中于 Windows 符号链接权限、`mkfifo`、`follow_symlinks` 和目录 fsync/只读语义，相关本轮 Storage 生产文件无新增差异。
