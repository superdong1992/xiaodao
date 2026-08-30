# TODO

更新时间：2026-08-30

本文件是仓库活跃待办的唯一清单。已完成事项由代码、当前设计与 Git 历史证明，不在这里保留关闭项。

## P0：Evidence V2 双 Provider model cert 与 Release

- 零模型 Core、CrossJob 和两套 provider adapter 已接入 Evidence V2；旧 Codex/Luna 直接 Methods V1
  定位 Goal、Stage、Gate 和 action 路由已删除。正式结论仍须等待当前源码快照的真实 P1/P2 认证。
- P1 Claude Code + DeepSeek 与 P2 Codex + Luna 必须各自运行生产 DiagnosisRuntime：服务端生成
  Graph/Plan，模型只提交 Specialist/Reviewer evaluation 数组，每角色最多一次 repair；正常调用数为
  2，硬上限为 4。
- 两份 `model-cert.json` 必须绑定同一 attempt 中的 production registration、source snapshot、V8 contract manifest 和
  `core-verdict.json`，并记录 provider/model/revision、prompt/profile/tool policy、调用/repair 次数、
  usage 与最终 `methods_result` 身份。
- `release.evidence-v2-certification` 必须从同一 attempt 的 Core、P1、P2 生成
  `release-verdict.json`。当前待办是完成 plan-only 审核、真实模型调用和最终权威 verdict；在此之前
  不得登记为已修复。

## P0：Generic V2 最终集成与生产验收

- C 变更集提供 V1 兼容、完整 Markdown V2、服务端 `GENERIC_REPORT` 产物和局域网适配 Skill；最终发布前仍须与其他并行变更合一，并由主控对合并后的同一源码快照执行 fresh `release.full`。不得复用 C 的 Dev verdict 冒充 Release。
- 局域网管理员须在私有通用定位 Skill 内应用最小 framework-mode 适配，并在同一 Linux 服务账号、Agent、settings、模型和工具身份下运行本地 A/B 验收。收据只保留 Skill tree 摘要与显式版本、输入/结果的 size/hash/状态、两次相同的运行身份 manifest 摘要和本地人工语义 verdict，不保存或上传私有 Skill、报告正文、prompt、路径或执行输出；不得把两个随机模型调用的报告 hash 相等作为默认门槛。
- 只有合并后的 Release verdict 与局域网生产验收都完成后，才在 `FIXED_ISSUES.md` 登记本问题的最终修复记录与权威 verdict；本并行任务不写“已修复”或占位 verdict。

## P0：Diagnosis Skill 条件性可选参数

- Diagnosis Skill 必须支持条件性可选参数。参数未命中其声明的诊断分支时，不得成为 OPEN requirement，也不得阻塞路由、诊断、Review 或结果交付；只有进入指定分支且该分支确实依赖该参数时，Runtime 才向用户索要。
- 分支激活条件必须由 Skill 显式声明、可机读，并写入审计与 replay 输入；不得由 Agent 临时发明分支、用空字符串或隐藏默认值冒充未提供参数，也不得依赖客户端 Hook 修正语义。
- 条件参数若已作为初始 USER_FACT 提供，应直接固定并复用，不得重复询问；若未提供，分支激活后才创建一次可补充的 OPEN requirement。
- 生成器、manifest/合同、Catalog、Coordinator、服务端验证器和正反向测试必须共同覆盖“命中分支才询问、未命中分支不询问且不阻塞”。

## P1：Evidence V2 UNRESOLVED 真实归因基线

- Runtime 已具备内部共识归因记录：保留公开 reason code，并把共识型 `UNRESOLVED` 细分为
  `UNKNOWN_PRESENT`、`VERDICT_MISMATCH`、`EVIDENCE_SET_MISMATCH` 和 `NO_CONFIRMED`；同时记录
  evaluation 数、每项 event 数、激活方法数和 package 总方法数。归因数据只存在于 execution
  records，不进入 MCP、REST、Case 或 `MethodsTerminalProjectionV2`。
- `tools/methods-consensus-attribution-report.py` 会从 `methods-state-v2.json` 统计全部公开终态原因，
  并从 `methods-consensus-attribution-v2.json` 统计共识子因和规模分布。当前确定性验收样本刻意让
  四个子因各出现 1 次，因此样本内各占 25%；这是覆盖证明，不是生产流量结论，不能据此放松共识
  或实施部分重叠 event 的新语义。
- 下一步要在 P1/P2 真实模型认证或同身份生产运行中收集新的 execution records，再按 reason、子因、
  N、每项 event 数和激活率联合判断主因。若 `EVIDENCE_SET_MISMATCH` 主导，再单独评估证据交集规则；
  若 `INCOMPLETE_EVALUATION`、`NO_MATCHING_METHOD_EVIDENCE` 或 `*_SEMANTIC_INVALID` 主导，则分别处理
  UNKNOWN 全局门、marker 激活精度或模型输出合同，不能用放松共识替代。

## P1：日志抑制、限流与采样规则

- 当前版本只支持普通事件时间窗，不声明或推断日志抑制、限流或采样语义。
- 后续若业务 Skill 需要 75 秒或其他抑制机制，应新增显式、可机读的规则类型，并由 Skill 自己声明允许窗口方向、开闭边界、抑制键、最大间隔以及无日志时的可验证行为。
- 框架不得硬编码 75 秒，也不得在 Skill 未声明时自行放宽时间窗口。
