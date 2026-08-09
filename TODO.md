# TODO

更新时间：2026-08-09

本文件是仓库活跃待办的唯一清单。设计稿和复盘文档只保留背景、约束与历史结论，不再维护重复的 TODO。

## P0：Diagnosis Skill 条件性可选参数

- Diagnosis Skill 必须支持条件性可选参数。参数未命中其声明的诊断分支时，不得成为 OPEN requirement，也不得阻塞路由、诊断、Review 或结果交付；只有进入指定分支且该分支确实依赖该参数时，Runtime 才向用户索要。
- 分支激活条件必须由 Skill 显式声明、可机读，并写入审计与 replay 输入；不得由 Agent 临时发明分支、用空字符串或隐藏默认值冒充未提供参数，也不得依赖客户端 Hook 修正语义。
- 条件参数若已作为初始 USER_FACT 提供，应直接固定并复用，不得重复询问；若未提供，分支激活后才创建一次可补充的 OPEN requirement。
- 生成器、manifest/合同、Catalog、Coordinator、服务端验证器和正反向测试必须共同覆盖“命中分支才询问、未命中分支不询问且不阻塞”。

## P1：日志抑制、限流与采样规则

- 当前版本只支持普通事件时间窗，不声明或推断日志抑制、限流或采样语义。
- 后续若业务 Skill 需要 75 秒或其他抑制机制，应新增显式、可机读的规则类型，并由 Skill 自己声明允许窗口方向、开闭边界、抑制键、最大间隔以及无日志时的可验证行为。
- 框架不得硬编码 75 秒，也不得在 Skill 未声明时自行放宽时间窗口。

## P1：链路效率 DFX 与性能基线

- 在现有 Test Flow 阶段计时和性能身份机制上，补齐 Host 等待、网络传输、排队、ROUTE、Logparse、DIAGNOSE、服务端复验、REVIEW、发布与下载的统一耗时瀑布，并用 `correlation_id`、`request_id`、`case_id` 和 `job_id` 串联。
- 自动汇总各阶段耗时、重试、超时和传输字节；为尚未校准的阶段积累可比样本，据此设定阶段预算和回归门禁，再针对主要瓶颈优化。
- DFX 必须脱敏且有大小与轮转上限。保持 Windows/macOS 客户端通过 HTTP 直连 Linux Server，不引入本地 MCP、代理或 Hook；若确需客户端专用 DFX，须先明确采集、脱敏和部署方案。

## 已关闭（不计入活跃 TODO）

### 分段无 Mock E2E

已由 `tools/test-flow` 落地：真实 CrossJob 旅程按 Environment、Route、Upload、Diagnose、Review、Publish/Restart 分段；Dev 支持密封检查点复用，Release 禁止复用业务检查点并从空 `DATA_ROOT` 的 GENESIS 开始。
