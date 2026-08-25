# Wiki 转 Methods Skill 与专用定位运行时

状态：已实施；当前发布结论只以 Test Flow 的密封 `verdict.json` 为准

当前版本：Problem Locator 5.0.0，State / Job / Outcome schema V7，
`v7-contract-r1`

## 目标与非目标

这条链路把一份已评审的故障定位 Wiki 转成一个闭合 Methods package，再由
Problem Locator 产品注册把它绑定到路由、用户输入、Logparse 产品与运行时资产。
在一份固定日志快照上，系统只允许用可重新对照原始字节的正向证据确认方法；
日志缺失、不可关联或证据不足必须保留为候选、限制或不可定论。

它不是持续监控系统，不在固定快照之外补采日志，也不让生成 Agent 或诊断
Agent 决定部署范围、Logparse anchor、运行时资产或权威业务结果。

## 破坏兼容性边界

当前实现是一次明确的硬切：

- 状态只接受 State V7 / `v7-contract-r1` 的新空 `DATA_ROOT`，不迁移或恢复
  V1–V6 State、Job 或 Outcome。
- 元 Skill 的生成物不再包含 `GenerationSpec`、编译 manifest、
  `diagnosis-skill.json` 或产品 registration。旧 `.claude/skills/wiki-to-diagnosis-skill`
  生成器与旧生成 fixture 不是当前入口。
- SPECIALIZED DIAGNOSE/REVIEW 不再接受 Agent 生成的 `AgentJobOutcomeDraftV2`，
  不再依赖 manifest verification contract、`verification_contract.py` 或
  `server_verifier.py`。这些旧文件已从当前源码删除。
- GENERIC DIAGNOSE 仍是独立的黑盒回退路径，不读取专用 Methods package、
  附件、Evidence 或 Review 状态。
- 七个公开 MCP 工具仍只接受扁平根参数；Methods 接入没有引入嵌套
  object、动态 Map、客户端 Hook、本地 MCP 或代理层。

历史设计、旧版合同和实施过程只在 Git 历史中保留，不与本文的现行架构并列。

## 所有权分层

| 层 | 拥有的信息 | 不得越界的信息 |
| --- | --- | --- |
| 作者 Wiki | 诊断方法、字段含义、正向日志模板、阈值/单位、观测与安全边界 | 产品部署和运行时资产版本 |
| Wiki 元 Skill | 把 Wiki 忠实转为闭合 Methods package | 产品路由、Logparse 产品/anchor、Agent profile 和 output contract |
| 产品 registration | 能力描述、部署范围、package 绑定、DIAGNOSE/REVIEW 资产、Logparse plan | 不得改写生成 package 的 Wiki 语义 |
| Runtime | no-plan preflight、两 Pass 隔离、字节冻结、grounding、域模型映射、权威 Outcome | 不信任 Agent 摘要或自报证据 |
| Test Flow Gate | 身份、工具轨迹、canonical validator、模型不可见的语义 oracle、主机/服务器证明 | 不向生成 Agent 泄露 oracle 或 registration |

## 闭合 Methods package

`wiki-to-diagnosis-skill` 只生成以下目录：

```text
<skill-name>/
|-- SKILL.md
|-- methods.json
`-- references/
    |-- <method-id>.md
    `-- <shared-topic>.md
```

package 根目录只能有这三类条目。`methods.json@1` 使用 exact-key 合同，声明：

- `skill_name` 与原 Wiki SHA-256；
- Wiki 明确要求的 `required_user_inputs` 和 `required_artifacts`；
- 只能从日志中获得的 `log_derived_fields`；
- 共享参考卡和有序方法索引；
- 每种方法的独立 Markdown 卡和一组来自 Wiki 原文的正向
  `evidence_markers`。

每张方法卡固定包含适用条件、所需证据、计算与判断、确认条件、未知边界和
输出含义。相同原因的不同日志类型不拆成多个方法；多个原因可同时成立时，
运行时必须扫描全部目标日志，不能在第一个 marker 命中后短路。抑制、限流、
采样或条件打印只能形成未知边界，不能把日志缺失转换为排除证据。

生成合同把两处原本可能由模型自由改写的表面表示机械化：`log_derived_fields` 按 Wiki `text`
日志模板中命名占位符的首次出现顺序收集并排除用户输入；`evidence_markers` 使用第一个占位符前的
完整稳定字面前缀（模板以占位符开头时使用最长稳定片段）。canonical validator 与 gate-only oracle
必须遵循同一规则，因此不能用语义相近但字节不同的 marker，也不能让 oracle 漏掉日志命名字段。

元 Skill 自带的 validator 校验目录、字段、frontmatter、Wiki hash、引用、方法卡标题和
原文 marker，但不代替场景诊断或产品注册。

## 产品 registration 与身份

`SKILL_DIR` 的每个子目录是一个产品注册根：

```text
<SKILL_DIR>/
`-- <registration-id>/
    |-- registration-template.json
    `-- package/
        `-- <skill-name>/
            |-- SKILL.md
            |-- methods.json
            `-- references/*.md
```

`registration-template.json@1` 绑定 capability、deployment scope、package 相对路径、
`skill_name`、原 Wiki SHA-256、DIAGNOSE/REVIEW 的固定内置资产，以及可选的
Logparse 产品、角色和 anchor plan。当前 SPECIALIZED runtime 要求注册使用内置
Specialist/Reviewer profile、Methods-only tool bundle、context policy 与 output contract，不允许
registration 指向任意运行时资产。

Catalog 分别计算 registration SHA-256 和 package tree SHA-256，再计算 combined SHA-256。
Job 的 `diagnosis-skill/<registration-id>` ref 绑定 registration version 与 combined hash；
registration 或 package 任一字节变化都会改变身份，已冻结 Job 不能被当前 Catalog
静默替换。生产 Catalog 拒绝 `TEST_ONLY` 注册。

## 服务端 no-plan preflight

ROUTE 只向声明了全部已提供 USER_FACT 名称的专用 registration 提供候选。
选中 SPECIALIZED 后，Runtime 用 registration 的 Logparse plan 与 Methods manifest 编译冻结
plan。若缺少 Methods 声明的用户输入或 `log_archive`，它不启动 Agent，而是由
服务端直接生成 `NEED_INPUT` 或 `NEED_ATTACHMENT` Outcome 与
`supplement_policy=MISSING_ONLY` requirement。

当前 Methods runtime 只支持特殊附件 ID `log_archive`。Methods manifest 可声明
`problem_time`、进程名、service/API 等用户输入以及日志派生字段；Logparse
product、module、slot、角色与 anchor 的取值来源由产品 registration 固定。这个
preflight 是唯一能为 Methods 路径请求补充材料
的边界；Agent 输出合同没有 requirement 字段。

## 两 Pass 诊断执行

SPECIALIZED DIAGNOSE 在同一 Job 内执行两个隔离的 Agent pass，并共享同一个受限的
stdout/stderr 字节预算。

### Pass A：产品 Logparse 预处理

- 使用独立 `<job-id>.logparse-preprocess` Workspace。
- 不加载或执行 Methods package，不读取目标日志，不写诊断/Review 草稿。
- 服务端预先写入一份产品 request；Pass A 只能调用一次
  `problem-locator-logparse parse-targets` 或 `target-logs`。
- 服务端在 pass 结束后验证 broker audit、claim、接受请求和 authoritative targets，
  并从已校验资源重新读取每份可交付目标日志的字节。

### 服务端冻结边界

Pass A 退出且 broker 环境被撤销后，服务端把以下内容原子写入主 Workspace
的只读 `inputs/`：

- `request.json`：Job/Case/registration 身份、必需用户输入和已消费附件身份；
- `target_logs.json`：服务端 source ID、label、相对 `log_path`、size 和 SHA-256；
- `target-logs/*.log`：从 authoritative targets 重新读取后复制的冻结字节；
- `logparse-receipt.json`：操作、broker request/audit hash 与同一组 target 身份。

### Pass B：Methods diagnosis

Pass B 没有 Logparse broker 环境，不得重新解包、解析、选择生命周期或遍历其他日志。
它读取闭合 Methods package、`request.json`、`target_logs.json`、列出的日志与
receipt，先扫描每种方法的全部 marker，再按需加载方法卡。它只能写
`output/method-diagnosis.draft.json`。

## Methods diagnosis 合同与 grounding

`method-diagnosis.draft.json@1` 只有七个顶层字段：`schema_version`、`status`、
`confirmed_methods`、`candidate_methods`、`evidence`、`limitations` 和 `safety_notes`。
`status` 只能是 `CONFIRMED|PARTIAL|INSUFFICIENT`。

每条 evidence 代表一种方法在一个可区分事件上的发现，包含：

- package 中存在且已列入 `confirmed_methods` 的 `method_id`；
- 有界摘要与至少一个来自本条引用行的 `identity_tokens`；
- 至少一个 source：精确 `source_id`、一基行号、该方法声明的 marker 与完整单行原文。

服务端对所有冻结 target 重新执行全量 marker 扫描，检查 source ID、行号、完整
原文、marker 和 identity token，并要求每个 confirmed method 都有已 grounding evidence。
`(method_id, sorted(identity_tokens))` 必须唯一；不能把没有可靠共同身份的多个事件合并。

验证后，服务端记录 registration/package/combined hash、Logparse receipt hash、所扫描
source、全部 marker hits 与需加载的 method IDs，并生成 `method-grounding-audit.json`。
然后由服务端桥接层映射为现有 Evidence/Candidate/DecisionAudit/Result 域：

- `CONFIRMED` + 至少一个 confirmed method + 无 candidate method → COMPLETE Candidate；
- 至少一个 confirmed method，但草稿为 `PARTIAL` 或仍有 candidate method → PARTIAL Candidate；
- 没有 confirmed method → `INCONCLUSIVE`，不生成 Candidate。

已确认方法会生成服务端 `VERIFIED_PASS` 规则和原始行审计记录；未确认候选会保留
为 `UNVERIFIABLE` 规则。Agent 无权直接生成 Evidence/Artifact proposal、Candidate、
`DecisionAuditV2`、权威 Outcome 或公开 Result。

## 独立 Methods Review

DIAGNOSE 生成 Candidate 后，Coordinator 创建独立 REVIEW Job。Runtime 从 execution record
重新读取原始 `method-diagnosis.draft.json`、`method-grounding-audit.json` 和
`methods_logparse_receipt.json`，再核对 pinned registration/package/combined hash、receipt hash、
status、confirmed methods 和 evidence count。

Reviewer 只看到固定 Candidate、相关 Evidence、同一个 Methods package、先前的原始草稿与
grounding audit；它不获得 Specialist 的隐藏会话，也没有 Logparse 能力。它只能写
`output/method-review.draft.json@1`，顶层 verdict 和 finding verdict 只能是
`PASS|NEED_MORE_EVIDENCE|REJECT`。

服务端要求 Review findings 精确覆盖之前每一个
`(method_id, sorted(identity_tokens))` evidence 身份，不允许增删或重复。顶层 `PASS`
要求所有 finding PASS；`REJECT` 和 `NEED_MORE_EVIDENCE` 分别至少有一个同类
finding。DIAGNOSE 中的 candidate-method 规则不要求 Reviewer 伪造无证据 finding，而是由
服务端作为 `UNVERIFIABLE` mechanical fact 继续绑定到 Review subject 和 decision audit。

## Result v3、AUDIT_BUNDLE 与隐私边界

Methods 接入不改变既有 Result v3 的发布语义。独立 Review PASS 后，COMPLETE/PARTIAL
Candidate 分别进入 `RESOLVED`/`PARTIALLY_RESOLVED`，并以 durable outbox 语义发布服务端生成的
`diagnosis-result.json` 与 `result.zip`。两份产物在 Review 前都不对外可见；最终字节和
SHA-256 由服务端绑定。`INCONCLUSIVE`、Review `REJECT`，以及没有唯一可补充
`MISSING_ONLY` requirement 的 Review `NEED_MORE_EVIDENCE` 进入 `UNRESOLVED`，只公开
不可定论 USER_RESULT JSON 与 `AUDIT_BUNDLE`，不生成
`result.zip`。

`AUDIT_BUNDLE` 只收录 allowlist 内的可观察材料：Case/Job、实际 context、Methods 草稿、
Logparse receipt、grounding/decision audit、服务端引用的原始行、finalization manifest、
Review subject 与 broker audit。Agent stdout/stderr 的原始内容、原始上传包与完整 Logparse 树
不进入下载包；stdout/stderr 只暴露存在性、字节数与 SHA-256 元数据。任何审计材料
都不包含、也不能恢复模型的隐藏思维链。

## 自包含 Release case 与 Test Flow

当前 release case 使用 schema v2，业务专有材料全部收口在
`tests/cases/release/rpc-timeout-anonymized/`：

- 一份普通 Markdown Wiki；
- 一份产品 `registration-template.json`；
- 一份只对 Gate 可见的 Methods package 语义 oracle；
- 一条 `multiple-rpc-timeouts` CrossJob 场景及其 driver/oracle。

仓库不预存一份“批准的生成 package”。真实 Wiki→Methods Gate 只向生成 Agent 提供元
Skill、Wiki、由该 Wiki 原始字节生成并预先校验的 source-identity v2 sidecar，以及元 Skill直接链接的
output contract；v2 identity 除 Wiki digest 外，还提供从 Wiki 机械提取、按源顺序保留重复项的日志
模板清单。Agent 必须把该清单逐字写入固定 `references/source-log-templates.md`，并将它列为
`methods.json.shared_references[0]`。Agent 直接写完整 package，然后 Gate 审计 Skill/Read/Write v5
轨迹、运行独立重算 Wiki/模板的 canonical validator 和模型不可见的语义 oracle。验证通过后
Test Flow 才在 package 外复制产品 registration，以同一 package 字节身份供 fresh CrossJob
消费。

测试活动只从 `tools/test-flow/run.sh|run.ps1` 进入：

1. Dev 默认执行 affected + full deterministic，不调用真实模型；SameJob fixture 确定性覆盖
   no-plan preflight、Pass A、冻结边界、Pass B、grounding 与 Methods Review。
2. Release 先用 `--plan-only` 审查 Proof、Stage、Gate、身份、模型预算、成本与
   admission blockers；真实模型活动不得盲重试。
3. 真实生成 Gate 用身份绑定的 Claude Code + DeepSeek Flash 生成 Methods package；
   fresh CrossJob 用实际选定的 Client 直连 Linux Server，从 GENESIS 和新空 `DATA_ROOT`
   覆盖 Route、Upload、两 Pass Diagnose、Review、Publish/Restart。
4. Codex CLI + `gpt-5.6-luna` 是独立的工程化 Methods 探索 Gate：生成 workspace 同样物化
   source identity v2 与固定模板引用合同；一次生成与九次只读
   diagnosis 共十次调用。每次调用使用新的 stdio app-server、外置内存认证、ephemeral
   thread/turn 和单层 named permission profile；封存精确 CLI/model/reasoning、协议 schema、
   profile bytes、预处理、脱敏调用 trace、raw/terminal usage 对账、grounding、无凭据扫描、
   durable package 与 posthoc usage/cost 证据，并由 Test Flow consumer 独立重审；它不替代
   产品 CrossJob。
5. Release planning 冻结 Git 可见工作树；只有与该 source snapshot 精确绑定、最后原子写入且
   可重新校验的 `verdict.json` 能声明通过。

模型名称、预算、超时、执行平台、可执行文件 hash 和缓存 admission 条件由 Test Flow
版本化配置管理；本文不把某次运行的 run ID 或 snapshot digest 固化为“最新结论”。

## 完成判据

- 普通 Markdown Wiki 能直接生成只含 `SKILL.md` / `methods.json` / `references/*.md`
  的闭合 Methods package；其中 `references/source-log-templates.md` 精确绑定 Wiki 的机械模板清单。
- package 与产品 registration 分离，三层 hash 身份在 Catalog、Job、generation receipt
  和 Test Flow 中一致。
- 缺少输入时只执行服务端 no-plan preflight，不启动模型；材料齐备后 Pass A
  是唯一 Logparse 能力持有者，Pass B 只读冻结日志。
- Agent 只写 Methods diagnosis/review 草稿；原始行 grounding、Candidate/Review/Result 映射与
  权威 Outcome 完全属于服务端。
- COMPLETE、PARTIAL 和 INCONCLUSIVE 均由已 grounding 方法与未确认候选机械决定；
  Review 必须覆盖精确证据身份。
- Result v3、durable outbox、`AUDIT_BUNDLE` allowlist 与 Agent 隐私边界保持不变。
- 发布结论只引用当前源码快照对应的 Test Flow `verdict.json`，未执行、skip 或
  只有半成品目录的真实项不得声称通过。
