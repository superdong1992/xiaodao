# TODO

更新时间：2026-08-18

本文件是仓库活跃待办的唯一清单。已完成事项由代码、当前设计与 Git 历史证明，不在这里保留关闭项。

## P0：Generic V2 生产 LAN A/B 验收

- 仓库内的 Generic V2 合同、服务端 `GENERIC_REPORT` 产物、确定性双模式 fixture 和 Release 证据都不能替代生产 LAN 验收；它们不证明私有通用定位 Skill 已完成适配、诊断正确或可在生产身份下运行。
- 局域网管理员仍须在私有 Skill 中应用最小 framework-mode adapter，并在同一 Linux 服务账号、Agent、settings、模型和工具身份下，对同一输入分别执行原生 direct 与 framework V2 调用。收据只能保留 Skill tree 与显式版本、输入/结果的 size、SHA-256、受控状态、相同运行身份 manifest 的摘要和人工语义 verdict；不得保存或上传私有 Skill、报告正文、prompt、路径、stdout 或 stderr，也不得要求两个随机模型调用的报告 hash 相等。
- 只有本地 A/B 得到明确人工语义结论后，才能在未来候选快照中登记相应修复；`not-reviewed`、身份不一致、内容泄漏或仅有仓库 Test Flow PASS 都不能关闭本事项。

## P1：日志抑制、限流与采样参数的细粒度 evaluator

- 当前合同已能声明 `SUPPRESSION`/`RATE_LIMIT` 的 `scope`、`key_fields`、`window_ms`、`max_observed` 和 `boundary`，Runtime 也会把绑定这类 policy 的扫描作为下界：正向观测可证，不完整观测不能用来证明“不存在”；selector 缺失、anchor/范围不完整、lossy policy 与完整扫描真 no-match 已分别审计。
- 尚未完成的是通用 evaluator 对每个 policy 参数进行细粒度机械演算，包括 key/scope 分组、窗口边界与上限证明。在该能力具有直接合同和专项回归前，不得把有损扫描的零匹配升格为确定 FAIL。
- 框架不得硬编码 75 秒或 RPC 业务常量，也不得在 Skill 未声明 policy 时自行放宽可观测边界。
