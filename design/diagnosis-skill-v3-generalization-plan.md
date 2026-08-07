# Diagnosis Skill 通用化与生成语义 V3 修复计划

## 执行状态

- 状态：已完成（2026-08-04）。
- 实际版本：GenerationSpec v2、generator/生成 Skill `3.0.6`、manifest schema `2`、DIAGNOSE output contract `2.0.4`、S00 contract revision `v1-contract-r4`。
- 生产实现已完成三层边界拆分、声明式 requirements、可选 `logparse_product`、归档后缀派生 Content-Type、parse-once 续跑以及 `diagnosis-result.json` + `result.zip` 双公开产物。
- RPC、数据库死锁、无日志人工排查三个异构 Fixture 已完成参数隔离和确定性全生命周期覆盖；真实 Agent 合同门覆盖三类 requirements 生成与首次日志解析续跑。
- 最终冻结补丁 SHA-256 为 `ab6e0094c0060f3e0d01e9884dc2554f4e41e95dd50595ef8107d61e689508a2`。官方 Windows→Linux 分段发布验收已通过：Fast attempt52 为 156 项零失败、耗时 363.565 秒；ReleaseGates attempt53 为 2143 项零失败、13 项按平台/条件跳过、耗时 159.609 秒。两轮均通过最终密钥扫描，业务结果为 `ACCEPTED`，状态/HTTP/重启审计为 `PASS`。

## 总结

- 按三层重新划界：
  - 全局 DIAGNOSE output contract：只负责 Schema、通用结果约束、Canonical JSON、原子输出、安全及 Evidence/Candidate 约束。
  - `logparse-diagnose`：只负责 broker、请求结构、一次 parse、`LOGPARSE_RUN` 复用和路径安全。
  - 生成 Skill：负责业务 requirements、阶段、工具字段映射、补参流程和判断规则。
- 采用确定性规格与校验器作为机器事实源，保持生成的 `SKILL.md` 聚焦执行规则。
- RPC 字段不得出现在通用合同、生成模板或共享断言中，只保留在 service-takeover Wiki、演示 Skill 和对应场景测试数据中。

## 接口与合同变更

- `GenerationSpec.schema_version` 为 `2`，生成器和生成 Skill 当前版本为 `3.0.6`。
- `diagnosis-skill.json.schema_version` 升为 `2`，新增：
  - `requirements[]`：`name`、`kind`、`stage`、`fulfillment_source`、`prompt`、S00 原生 `constraints`。
  - `logparse_plan`：归档 requirement、问题时间绑定、按角色声明的 anchor bindings；无日志 Skill 为 `null`。
  - `logparse_product` 仅非默认产品时出现；省略表示使用 Logparse 的 `default`。
- 阶段固定为 `INITIAL | AFTER_LOGPARSE`；满足来源固定为：
  - `INPUT -> USER_FACT`
  - `ATTACHMENT -> READY_ATTACHMENT`
- 工具值绑定单独使用 `USER_FACT | SKILL_FIXED`。`LOGPARSE_RESULT` 只能形成 Evidence、Finding 或 `proposed_facts`，不能满足用户 requirement。
- 所有 `requirements[]` 都是必需项；`required=false` 在生成时拒绝。空 custom parameters 就是空集合，不添加任何默认参数。
- 不修改 S00 `PendingRequirement`、`NEED_INPUT`、`NEED_ATTACHMENT` DTO；本版本每阶段最多开放一个 Attachment requirement。
- Logparse 归档格式由平台固定，不再让 Skill 作者指定 Content-Type：
  - `.gz/.tar.gz/.tgz -> application/gzip`
  - `.zip -> application/zip`
  - `.tar -> application/x-tar`
- DIAGNOSE output contract 升为 `2.0.4`；Catalog 按内置资产分别声明版本，并由服务端 finalizer 统一发布 Canonical Agent 输出。

## 实现变更

- 生成器以 manifest v2 为唯一规范化事实源，并从同一对象渲染 `SKILL.md`；validator 校验两者逐项一致。
- custom parameter 必需行编译成 INPUT requirement，必须明确阶段和约束；旧三列表格或旧 GenerationSpec 不自动猜测迁移。
- `requires_logparse` 只控制工具绑定：
  - `true` 不再自动产生 RPC 参数、日志附件或 parse 后补参。
  - `false` 允许 `module=null`、空 roles，并禁止 `logparse_plan`、`AFTER_LOGPARSE` 和 broker 调用。
- 生成 Skill 按声明顺序执行通用阶段算法：
  - 先请求当前阶段全部缺失 INPUT。
  - INPUT 齐全后请求该阶段 Attachment。
  - parse 成功后，若缺少 `AFTER_LOGPARSE` INPUT，先持久化运行结果及必要 Evidence，再返回 `NEED_INPUT`。
  - 后补参数为空时直接继续分析，不制造中断。
- 将 `parse-targets`/`target-logs` 请求校验、parse-once 和 run 复用全部收敛到 `logparse-diagnose`；业务 Skill 只声明映射和解释规则。
- 默认产品时 Broker 不传 `--product`，但 `LogparseRunMetadata.parse_parameters.product` 记录有效值 `default`；非默认产品才显式传参。
- 保留 `diagnosis-result.json`，并新增 Review 后可下载的 `USER_RESULT_ARCHIVE/result.zip`：
  - 公共合同增加 `ArtifactKind.USER_RESULT_ARCHIVE` 和对应 metadata，因此 `CONTRACT_REVISION` 升至 `v1-contract-r4`。
  - 生成 Skill 形成 Candidate 时必须同时产生一个 JSON 和一个 ZIP；公共合同允许 archive 至多一个且禁止脱离 Candidate。
  - ZIP 为确定性扁平包：`result.txt` 加 Candidate 实际绑定的完整目标日志，按 binding 顺序命名为 `target-log-001.log` 等。
  - 人工排查 ZIP 只有 `result.txt`；禁止包含原始上传包、无关日志、全部 parse 输出或 `LOGPARSE_RUN`。
  - Runtime 对 ZIP 条目、来源字节、大小、路径和重复项进行有界校验；Review PASS 前两个结果均不可下载。

## Fixture 与测试

- RPC service-takeover：
  - INITIAL：`caller_service/server_service/rpc_method/problem_time`
  - Attachment：`log_archive`
  - AFTER_LOGPARSE：`order_id`
  - 两个固定 anchor、显式非默认 product。
- 数据库死锁：
  - INITIAL：`database_instance/database_process/incident_time`
  - Attachment：`database_logs`
  - AFTER_LOGPARSE：`victim_transaction_id`
  - 一个 anchor，`incident_time` 映射至工具 `problem_time`，省略 product 以验证默认值。
- 无日志人工排查：
  - INITIAL：`affected_component/observed_symptom/reproduction_steps`
  - 无 module、roles、Attachment、Logparse 或后补阶段。
- 将生成器 Fixture 移出 Logparse 专属目录；仅 service-takeover 作为正式演示 Skill，另外两个在测试目录临时生成。
- 三个场景均执行确定性全生命周期 E2E：Route、分阶段补参/附件、Candidate、Review、下载、重启及字节一致性。
- 增加三条真实 Agent contract gate；现有 Windows/Linux 完整发布旅程和真实 Logparse 发布门禁继续以 RPC 场景为代表。
- 参数隔离验收：
  - 每个 Outcome 的 requirement 集合精确等于对应 manifest 声明。
  - 向错误 Case 提交其他场景参数或附件必须失败且状态不变。
  - DB/人工的 Skill、Prompt、Outcome、CaseView 中不得出现 RPC 字段。
  - 人工场景 broker 调用为 0；两个日志场景 parse 总次数均为 1。
  - Review 前 ZIP/JSON 不可见；PASS 后可下载且 ZIP 内容精确匹配 Candidate 绑定日志。

## 版本、迁移与工作量

- 版本矩阵：`GenerationSpec v2`、generator `3.0.6`、manifest schema `2`、生成 Skill `3.0.6`、DIAGNOSE output contract `2.0.4`、S00 contract revision `v1-contract-r4`。
- 不做运行时按需迁移；旧 Skill 必须显式重新生成。预发布环境默认使用新数据根，需要保留数据时执行独立离线迁移。
- 历史 handoff 保持不变，通过新的合同修订和实施记录描述本次破坏性修正。
- 原评估总工作量约 `26-40 人日`；本计划范围已经完成。数据库和人工场景已具备确定性全生命周期与真实 Agent requirements 合同覆盖，但未扩展为各自独立的 Windows/Linux 发布旅程。
