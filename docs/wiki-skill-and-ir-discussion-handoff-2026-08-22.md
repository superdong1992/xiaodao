# Wiki 定位 Skill 与 IR 方案讨论归档

归档时间：2026-08-22（Asia/Shanghai）  
当前讨论任务：`澄清Wiki定位歧义`（thread `01a0282c-6ae3-7ff0-b078-2954039afcca`）  
关联任务：`创建工作树并分析IR方案`（thread `01a01e11-018a-7782-9395-efa3c342a6e2`）

> 本文件是跨设备继续讨论的交接档案，不是已批准设计、已完成修复或 Test Flow verdict。
> 仓库当前存在大量未提交改动；继续工作前必须先核对实际工作树，不能仅凭本档案假定代码已完成。

## 1. 下次继续时先看这里

当前最重要的纠正是：

1. `tests/cases/release/rpc-timeout-anonymized/input/wiki.md` 是尚待补全的 Wiki 中间稿，不能直接作为
   Wiki → Diagnosis Skill 的最终输入。
2. `(# ... #)` / `（# ... #）` 在本用例中是 Wiki 整理阶段的说明。应先落实其中需要补充、匿名化或
   澄清的内容，把必要知识写进完整 Wiki；然后删除旁注，最后才进入 Skill 转换。
3. 日志 3/4 只有字段语义，没有稳定消息体。此前直接生成 Skill 的流程错误；生成物缺少
   `late_response` 与 `api_complete` extractor。
4. 因输入前提错误，隔壁任务后半段发现的“`{服务名}` 被识别为字面花括号”只是下游历史现象，
   不能作为当前首要根因继续修。
5. 用户已要求：先修 Wiki 完整性门禁；制定强制的日志模板占位符规则并写入元 Skill 与 README；
   重新讨论可变长度 API 历史块；为进程存活新增 Logparse 生命周期查询；删除跨板时钟容差描述；
   常见原因必须保持开放集合。

尚未讨论完成的三个决定：

- 目标 API 排队区间与其他 API 执行区间的精确定义及边界是否闭合。
- 新 Logparse 进程生命周期查询以什么证据返回 `CONFIRMED`，以及 `UNKNOWN`/不可靠边界。
- “常见原因按经验排序”是否只影响调查与展示顺序，还是完全忽略排序。

另有一个已发现的实现约束：当前 Verification Contract v2 的多行 extractor 要求声明的所有
`members` 均出现，不能直接表达“最多五条、最后一条是目标”。继续设计前必须解决可变长度块的
确定性表示，不能再把五条写死。

## 2. 当前 Wiki 与旁注的正确理解

直接使用过的文件：

- `tests/cases/release/rpc-timeout-anonymized/input/wiki.md`
- 补充材料：`tests/cases/release/rpc-timeout-anonymized/input/clarifications.md`
- 完整机器基准：`tests/cases/release/rpc-timeout-anonymized/input/generation-spec.json`

`(# ... #)` 不是标准 Markdown。当前 Wiki 中至少承担以下用途：

- 说明匿名化替换，例如 CCCC、版本号、错误码；
- 解释 BBBB 与 Logparse module 的关系；
- 表明原始日志被省略、当前只保留字段语义；
- 说明测试合同或首版框架暂不支持的限制。

此前错误地把“剔除旁注”当成转换前的第一步。正确顺序是：

1. 识别旁注要求补充或确认的正文知识；
2. 从真实 Wiki、作者回答或本离线用例的受控补充材料中补齐；
3. 形成单一、完整、可供人复核的 Wiki；
4. 删除转换旁注与测试专用说明；
5. 运行 Wiki → Skill 转换。

旁注独有的测试答案不能直接泄漏进产品；但旁注指出的业务缺口也不能被静默忽略。

## 3. 已确认的六个工作项

### 3.1 完整 Wiki 入口门禁——直接修复

用户判断：无需继续讨论，可直接修复；遇到无法确认的信息再讨论。

应达到的行为：

- 未补齐会影响 extractor、字段匹配、机械计算或因果结论的信息时，不得生成 Diagnosis Skill。
- 转换器应产出普通语言的作者问题或停在 Wiki 整理阶段。
- 当前 Luna Gate 不得一边移除 `clarifications.md`，一边断言 `author-questions.json` 不得存在。
- 当前离线用例应先把日志 3/4 的稳定消息体等必要知识写回完整 Wiki，再以这份 Wiki 作为唯一业务输入。
- 基础 Schema/validator 通过不能替代“输入 Wiki 已完整”的证明。

当前错误证据：

- `wiki.md` 第 28/29 行只描述日志 3/4 字段语义。
- `clarifications.md` 第 42–44 行才给出合成稳定消息体。
- 旧真实 Gate 同时提供 Wiki 与 clarifications；新 Luna Gate 刻意不提供 clarifications，并强制不得提问。
- 生成产物只有 4 个 extractor；完整基准有 6 个。

### 3.2 日志模板与字段占位符约定——元 Skill + README 强制规则

推荐名称：**日志模板与字段占位符约定**。对应的问题类型可称为
**日志模板元语法未声明**。

已对齐的规则：

- 日志模板中的 `{字段名}` 表示运行时字段值，花括号不是原始日志字符。
- `{服务名}` 是“名为服务名的字段占位符”，不是固定字符串“服务名”。
- `%s`、`%u` 等原始格式化记号也表示运行时字段；生成 extractor 时匹配实际值。
- 如果真实日志确实打印 `{`/`}`，Wiki 必须明确标注其为字面字符。
- 转换器应使用中性字段值构造最小 witness，验证正则匹配实际日志值且 capture 不含模板标记。
- 规则必须进入 `.claude/skills/wiki-to-diagnosis-skill/SKILL.md`，也必须在根 `README.md` 中作为
  Wiki 必须遵守的写作规则公开。

当前工作树已有针对花括号的未提交尝试，但它产生于错误输入流程之后，不能视为已完成修复；应在
完整 Wiki 和正确 Gate 上重新验证。

### 3.3 “前面的长耗时 API”——Wiki 必须补全，仍需完成公式讨论

用户已澄清：

- 历史块是**最多五条**，不是固定五条。
- **最后一条**才是当前目标 API；此前测试把 first 当目标是错误的。
- 目标行记录 `end time`、`cost time`、`queue time`。
- 应从目标行向前还原它的执行与等待时间，再用其他 API 的 `end time`/`cost time` 判断执行区间是否
  与目标等待区间重叠；存在重叠说明该 API 对目标排队产生了影响。

待用户最终确认的推荐公式：

```text
target_execution_start = target_end - target_cost
target_queue_start     = target_end - target_cost - target_queue
target_wait_interval   = [target_queue_start, target_execution_start)

other_execution_start = other_end - other_cost
other_execution_interval = [other_execution_start, other_end]
```

若 `other_execution_interval` 与 `target_wait_interval` 相交，则该 API 执行占用了目标等待期间的
串行 lane。需要确认端点相等是否算影响，以及 Wiki 中“开始执行”是否其实指“进入 lane 开始等待”。

实现约束：Verification Contract v2 的多行 extractor 是固定 `members` 序列，当前无法直接表达
1–5 条可变块。不得再生成固定五行 extractor。需要在不制造 RPC 专用框架逻辑的前提下设计
可变长度有序块表示与回归测试。

### 3.4 “两侧进程仍存活”——新增 Logparse 进程生命周期查询

来源：`wiki.md` 第 44 行：

> 如果只有客户端超时日志，且两侧进程仍存活……

此前生成 Skill 的 `timeout_candidate_set` 只依赖客户端 timeout event，却在语义结论中使用“两侧
存活”这个未验证前提，这是不安全的。

用户选择：新增 Logparse 生命周期查询，而不是把存活降为纯文本说明或直接复用 `target-logs`。

现状核对：

- Logparse 已有 `mech-lifecycles` 和 `mech-target-logs`。
- `mech-target-logs.match_status=exact` 表示选中的板级/CPU 周期覆盖 `problem_time`。
- 当前 `target_logs` 不返回周期起止时间或 `lifecycle_reliable`。
- 当前 V3 生命周期主要是 board/CPU interval；进程摘要包含 process/PID 与日志，但没有对外的严格
  进程级 start/end 存活区间。
- 因此“目标进程出现在覆盖问题时间的板级周期”不能未经定义就等同于“目标进程在问题时刻存活”。

建议的待讨论合同：按 `task_id + module + slot + process_name + optional pid + problem_time` 查询，返回
所选进程/PID、生命周期起止、可靠性、匹配状态、caveats，以及 `CONFIRMED|UNKNOWN`。只有可靠的
同 process/PID 生命周期覆盖问题时间才可作为机械存活前提；边界、歧义、不可靠或 nearest 均不得
确认。该建议尚未获得用户对“CONFIRMED 的证据条件”的最终确认。

Problem Locator 的 job-scoped broker 也需要增加对应只读 operation，并保持当前一次解析、固定
`LOGPARSE_RUN`、路径约束和审计记录；不能让生成 Skill直接读取 `result.json` 自行判断。

### 3.5 跨板时钟容差——删除

用户决定：从 Wiki 和生成 Skill 中移除跨板时钟容差的任何描述，不再把它作为问题定位考虑项。

当前 Wiki 第 33–40 行定义 Q/S/C，并对 Q、C 引入跨客户端/服务端时钟容差；测试 clarifications
人为固定为 100ms。此前生成 Skill还创建了 `cross_clock_tolerance_ms` 用户输入。

继续实施前仍需在字节层面确认删除范围：

- 已确定删除跨板时钟容差说明与 `cross_clock_tolerance_ms` requirement。
- 尚需确认 Q/C 段是否连同机械推理全部删除，还是只保留不参与结论的描述；用户此前倾向“完全删掉，
  不需要考虑”，应优先按删除 Q/C 推理理解，但不要未经复核保留零容差跨板判断。
- 同一时钟内的服务端 S 与客户端端到端耗时不属于跨板容差，可否保留需在完整 Wiki 修订时明确。

### 3.6 常见原因是开放集合——规则对齐与回归

原 Wiki 写“常见原因按局部经验排序，但不是穷尽集合”是正常领域表达，不是 Wiki 缺陷。

推荐规则：

- 常见原因只构成开放候选集；不得把列表编译成穷尽、排他的决策树。
- 原因顺序最多影响调查和展示顺序，不能让高优先原因的 terminal path 遮蔽其他已存在的正向证据。
- COMPLETE/PARTIAL 必须由正向证据决定；未命中列表内原因不能排除范围外原因。
- `NONE` 只表示“本 Skill 声明范围内没有形成可复核进展”，不表示所有可能原因均不存在。
- 输出应保留“可能存在 Wiki 范围外原因”的边界。

待讨论：是否保留经验排序用于调查/展示，还是在生成 Skill 中完全忽略排序。禁止采用“排序影响终态
优先级、命中一个就停止检查其他证据”的闭世界行为。

## 4. 关联任务《创建工作树并分析IR方案》归档

### 4.1 原始请求与实际工作树

原始请求：

> 新建工作树，切换到 `archive-ir-generation-24h-20260820`，分析为什么用 IR 实现专用问题定位不合适。

最终没有再新建重复工作树，而是复用：

- 工作树：`C:\Users\admin\.codex\worktrees\472f\xiaodao`
- 分支：`codex/archive-ir-generation-24h-20260820`
- 固定提交：`f1563c4`

### 4.2 IR 架构复盘结论

仍然有效的核心结论：不合适的不是所有 IR，而是
**“让模型一次性生成大型、样例绑定的 Rule-IR，再编译成诊断程序”**这条主产品路径。

主要理由：

- **职责倒置**：预期是 Skill 指导模型逐步补事实、调用工具、验证假设；实际变成模型一次生成完整
  诊断程序。
- **样例绑定而非通用**：归档 IR 固定 position/role/requirement/anchor/extractor/rule/path 数量，
  实质是 RPC ordered-interval 用例宏，难以自然覆盖 manual-triage、deadlock、单 anchor 等异构 Skill。
- **只压缩重复语法**：compiler 能展开大量机械重复规则，但业务文本、事件、语义因果和终态仍需模型
  一次性正确生成，核心语义难度未消失。
- **真实生成失败**：最终历史 run `run-20260819T161046Z-f83a1349` 为 FAIL；IR 51,457 B 超过
  48 KiB，单次消耗 811,944 tokens、$4.602584。归档窗口累计 14,465,415 tokens、$94.538827，
  仍无 Release PASS。
- **验证错觉**：deterministic full PASS 只证明已知 blueprint 可以被 compiler 展开，不证明真实模型
  生成、业务语义、九场景和 fresh CrossJob 已闭合。
- **接口边界不兼容**：深层对象 IR 不能进入七个公开扁平 MCP 输入，只能作为内部离线产物，不能
  成为运行时定位协议。
- **维护成本高**：JS schema、Python compiler、wrapper、trace audit、seal 和大量测试形成第二套
  合同，小错误也会引发昂贵的整轮模型重跑。

推荐边界：

- Skill 保存经作者审阅的业务方法、输入、证据解释和结论边界。
- 模型每轮只输出有限动作：补充输入、请求附件、调用工具、提出 Candidate 或声明证据不足。
- MCP 保持稳定扁平接口，不传输诊断程序。
- 服务端负责状态迁移、机械规则重算、证据审计和权威 Outcome；Reviewer 独立复核。
- IR 最多作为从已批准 Skill 确定性派生的非权威内部产物，或用于真正通用的 verifier primitive；
  不再由模型生成整份专用诊断程序。

### 4.3 后续工作目标的多次漂移与最终对齐

该任务随后从“IR 复盘”扩展到 Codex Luna 专用 Skill 可行性实验，期间计划多次漂移：

- 一度计划完整 Client/MCP/Coordinator/Diagnosis/Logparse/Reviewer/Test Flow 链路；
- 一度改为 `$skill-creator` 直接生成最小 Skill，绕过 `$wiki-to-diagnosis-skill`；
- 又重新确认交付物应是仓库内元 Skill `$wiki-to-diagnosis-skill`；
- 用户最终明确当前阶段是探索 Skill 是否能用：先验证，失败后分析并最小修正，直到能生成定位 Skill，
  且在日志信息满足时能定位问题；阶段失败只阻止无意义下游调用，不表示任务结束；当前阶段不应先
  扩建整套 Problem Locator 链路。

稳定目标应保留为：

1. 输入是完成、经复核的人类 Wiki，作者只需普通语言澄清；GenerationSpec/机器合同是内部实现。
2. 使用 `$skill-creator` 改进仓库元 Skill；不手工修生成产品，不重新引入大型 IR/compiler。
3. 元 Skill生成 Runtime 可加载的 Diagnosis Skill。
4. 生成 Skill通过 `$logparse-diagnose` 和真实/受控 Logparse 证据完成定位；只使用 broker 选择的
   `target_logs`，不扫描归档、不猜路径。
5. 证据充分时正确定位；证据不足时诚实输出 PARTIAL/UNKNOWN/NONE。
6. 先走最短闭环；只有 Skill 可行后，才值得建设公开 MCP、完整 Coordinator 与独立 Reviewer 的
   最终系统验收。

### 4.4 实际产物与历史结果

隔壁任务在主工作树产生了大量未提交修改，包括元 Skill、Wiki 写作建议、Luna Gate、Test Flow
身份/预算/运行配置、测试和台账。继续工作前必须以 `git status --short` 为准逐项审计，不应整体
接受或整体删除。

历史 Luna 生成结果：

- Run：`run-20260821T125735Z-0f6b4d7e`
- 生成 `SKILL.md` SHA-256：
  `27ac6cb0b05847253ed30cf65c905b1fdfa50739953ec61ef659f98df3474669`
- 生成 `diagnosis-skill.json` SHA-256：
  `7a1c817ccf989759e09b1f8da5ee011dc5865fff4ee3d7ef17a20c91f4e9aa7f`
- generator 与 validator 通过；模型成本 `$0.062697`。
- 历史主 COMPLETE 语义门得到 PARTIAL：客户端两个 timeout event 命中，排队块/死循环 event 未命中。
- 当时判断为 `{服务名}` 被当作日志字面花括号。

历史 deterministic 验证：

- Run：`run-20260821T132318Z-ac2079ac`
- Overall：`PASS_WITH_WARNINGS`
- functional / operation / verification：PASS
- performance：NOT_CALIBRATED
- 源码快照：
  `git-visible-worktree-v1:ee116526b33e1860d60e84705ba87a432f5924c71bc863b149be2325b6fb1aec`

这些结果的当前解释：

- deterministic PASS 只证明当时 Test Flow/harness 的确定性合同通过。
- Luna 产品的业务结果不能再证明“完整 Wiki 转 Skill失败”，因为输入 Wiki 本身未补齐。
- 生成物遗漏日志 3/4 的 `late_response` 与 `api_complete` extractor；完整基准本应有 6 个 extractor，
  它只有 4 个。
- 因此不要继续以“修花括号后重跑主 COMPLETE”为下一步。应先修 Wiki 完整性门禁与完整输入。

## 5. 建议的恢复顺序

1. 在新电脑打开本文件，核对当前分支、HEAD、`git status --short` 与相关任务状态。
2. 不执行真实模型或 Test Flow；先审计并修正 Wiki 完整性入口。
3. 把日志 3/4 稳定消息体、可变 1–5 条历史块语义和必要业务关系写入完整 Wiki；删除已落实的旁注。
4. 把占位符约定写入元 Skill 与 README，并增加中性正反例回归。
5. 讨论并锁定目标等待区间公式、生命周期 `CONFIRMED` 证据和原因排序展示规则。
6. 设计 Logparse 进程生命周期只读查询与 broker operation；不能直接暴露/扫描 `result.json`。
7. 从 Wiki/Skill 中删除跨板时钟容差相关要求；确认 Q/C 与同钟 S/端到端字段的最终保留范围。
8. 解决可变长度历史块在 Verification Contract 中的确定性表示。
9. 重新生成同一个 Diagnosis Skill；先做零模型结构/语义前向验证，再决定是否调用 Luna 与真实 Logparse。
10. 所有已确认修复按仓库规定增加直接回归、更新 `FIXED_ISSUES.md`，最终只认 Test Flow
    `verdict.json`。

## 6. 当前工作树提醒

归档时主工作树已存在大量未提交修改，至少涉及：

- `.claude/skills/wiki-to-diagnosis-skill/`
- `TODO.md`、`FIXED_ISSUES.md`
- Luna Wiki generation Gate 与专项测试
- `tools/test-flow/config/`、`tools/test-flow/lib/`、runtime support 与相关测试
- Logparse fixture manifest/source-copy

这些改动归属隔壁任务与用户工作树，不能在恢复时使用 `git reset --hard`、`git checkout --` 或批量
删除。应逐项审阅并保留证据目录；被 verdict 引用的证据不得删除。
