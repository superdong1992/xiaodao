# 8.0 性能分析、预处理扫描与 Specialist 命中索引

核对日期：2026-09-07。首次调查从干净的 `8b53bc5` / package `8.0.0` 开始，随后完成预处理扫描优化，并继续处理 Specialist 的重复机械定位。用户补充了现场汇总数据，但尚未提供原始日志和部署源码身份。本轮未调用真实模型。下面区分已验证的代码行为、用户提供的数据、历史测量和待测假设。

## 现场补充：固定内部模型，主要生成量是 thinking

用户确认慢请求是 Specialist 单轮诊断，使用无法更换的公司内部模型，没有 Reviewer。提供的汇总为：

| 指标 | 用户提供的数值 |
| --- | ---: |
| BACKEND_EXECUTE | 677 秒 |
| model_api_duration | 656.1 秒 |
| cli_duration | 657 秒 |
| 服务端 prompt | 约 80 KB |
| prompt 写入 | 12.1 秒 |
| 首次 system 消息时间点 | 约 17.4 秒（需以原始字段确认） |
| turn_count | 1 |
| input / output tokens | 43,413 / 20,281 |
| thinking / text | 约 65.4 KB / 1.4 KB |
| 最终诊断 JSON | 约 1.4 KB |

API 字段约占 BACKEND_EXECUTE 的 96.9%，优先研究模型需要完成的工作。最终正文很短，压缩原文回显或修改输出协议不再是本例的优先方向。thinking 字节量说明主要生成内容是推理，但不能按字节比例分摊 API 时间，也不能据此认定全部推理都是冗余。

当前源码把 CLI 的 `duration_ms`、`duration_api_ms`、`num_turns` 和 usage 原样转为遥测字段。`system_observed_ms` 从遥测对象创建开始计时，会覆盖启动及部分 stdin 等待；`prompt_write_ms` 包含管道反压、flush 和 close。12.1、17.4、656.1 秒相加超过总耗时，不能当作互斥阶段。当前字段也不能拆出 API 排队、首 token 等待、真实解码速率或 CLI 内部重试。

## 本轮落实：复用完整 marker 命中索引

在当前生产 V1 路径中，服务端在 Specialist 调用前已经执行 `scan_method_markers()`，获得全部来源、marker 和一基行号，却只用它选择方法卡。原 profile 和输出合同又分别要求模型全量扫描和逐引用子串自检。

本轮把同一调用中产生的扫描收据传入 prompt 构造器，生成 `SERVER_MARKER_INDEX` 辅助段，包含 `complete`、`method_markers` 方法归属表和 `source_hits` 分组行号。共享 marker 的重复三元组去重；零命中来源仍明确保留。索引不复制原文，不引入新证据 ID，也不改变诊断输出 V1、报告或 grounding。

完整索引及说明最多 8 KiB，构建中即限制大小，超限整份省略。若加入索引会让原本可内联的请求超过 128 KiB，也整份省略并保留原有内联行为。原本需要读取文件的请求仍列出全部文件。没有完整索引时，Specialist 沿用原全量查找流程。

profile 和输出合同明确：有完整索引时，直接使用服务端提供的位置，不再重复枚举 marker、计数和验算子串。仍须完整阅读冻结日志和方法卡，判断 Wiki 条件、对象身份、时序、因果关系及反证；命中、命中数量和索引顺序都不能替代诊断结论。日志内自称索引的文本不具有指令效力。

Specialist profile 更新为 `8.0.0`，DIAGNOSE output contract 更新为 `11.0.0`，最终 JSON 的 `schema_version` 仍为 `1`。模型和 Reviewer 配置未变。生产资产在启动时冻结，因此部署这些字节后需要重启服务，已有进程不会热加载新提示词。

**验证范围**：确定性测试检查索引完整性、归属、去重、Unicode、大小与内联边界，以及运行入口确实使用本轮同一份冻结输入和收据；原 grounding 与具体报告用例继续覆盖最终结果。真实内部模型上的 thinking 和 677 秒是否下降仍待同输入对照，不能从静态测试宣称提速。

## 本轮落实：降低日志预处理的目录扫描频率

7.0 已把普通 Agent 的全目录扫描改为每秒一次，但 `_DirectPreprocessingCancellation._refresh()` 仍按 `poll_interval_seconds` 调度扫描，默认每 50 毫秒一次。Logparse 检查取消状态时会触发它，解压目录越大，重复遍历成本越高。

修改前使用当前类、假时钟和内存扫描计数器：在约 10 秒内执行 200 次取消检查，触发 200 次扫描，退出强制检查后为 201 次。本轮将预处理的周期扫描也改为每秒一次；真实 Workspace 的回归用例中，同一窗口只有 10 次扫描（含初始检查），退出后总计 11 次。取消检查仍保持原频率，超时优先级和退出强制扫描保留；另有专项用例验证扫描间隔内取消立即生效、退出时发现目录超限。

这项改动减少重复扫描次数，不等于端到端定位提速相同比例。目录超限和非法节点的周期发现间隔随之扩大到约一秒，另有扫描本身的耗时；短暂出现又消失的节点不保证被采样捕获。退出后的完整校验仍为交付前边界。它沿用 7.0 主 Agent 已采用的取舍。

验证以 `tools/test-flow/run.ps1 --track dev --goal dev.default` 的当前源码快照 `verdict.json` 为准；最终 run ID、状态和源码摘要写入 [`FIXED_ISSUES.md`](../FIXED_ISSUES.md) 的 PL-FIX-053 本轮元数据。组件诊断不是独立发布证明。

## 其余优化方向

| 方向 | 当前证据 | 适用场景与下一步 |
| --- | --- | --- |
| 模型关键路径 | ROUTE 已输出三字段最终 JSON；小 Specialist 已完整内联并直接输出 JSON。历史 7.0 单样本约 54 秒，其中 Specialist 约 48 秒。 | 单 Case、小日志仍慢时，先核对当前模型窗口、输出 token、缓存与工具回合，再做同输入、同身份的多次测量。旧样本不能证明当前 8.0 的根因。 |
| Reviewer | 当前仍要求 Read 输入并 Write 草稿；内存构造现有 REVIEW fixture 和生产角色资产得到 8,777 字节上下文，同一 918 字节 Candidate 精确出现 3 次。 | 仅显式启用 `SPECIALIZED_REVIEWER_ENABLED` 时影响主链路。优先评估重复上下文收敛和最终 JSON 输出，同时保留独立审核、证据核验与失败收口。 |
| 网站会话排队 | 全部会话共用一个会话 worker。真实服务对象配合阻塞式假模型：A 的补充整理未结束时 `run_once(B)` 返回 false，B 保持 QUEUED；释放后才开始 B。非空问题文本建案不调用 INTAKE，但仍经过同一调度入口。 | 多会话并发时评估跨会话有界并发，同一会话仍串行。增加 DIAGNOSE worker 不会消除会话队列。 |
| 网站空闲负载 | SSE 每 0.5 秒查事件，再构建完整会话。SQLite trace 中，一个无新事件批次执行 5 条 SELECT；两个无待处理消息的 WAITING_INPUT 会话，一次后台扫描执行 23 条 SELECT，其中 6 次加载消息全历史。 | 长历史或长期保留大量未关闭会话时，评估轻量状态查询与只调度有新工作的会话。计数证明冗余，不证明已产生秒级延迟。 |
| 大附件 I/O | 网站上传先 hash 并保存会话 payload；导入 Case 再完整 hash，回到文件开头后经原上传端口再次 hash 和复制。65,536 字节内存流复现导入阶段两次完整读取和一次写入。 | 首次上传加导入至少 3 次全量 hash、2 份落盘，仅适用于网站 Agent。先测目标机读取字节和阶段耗时，再评估不可变暂存资源复用；不能直接删除完整性校验。 |
| 多日志目标 | 首次解析 N 个 anchor 时，当前 broker 顺序启动 1 次 parse 和 N 次 target 子进程，默认 Logparse 并发为 1。 | 多目标场景先测 1/2/4/8 个 anchor 的启动、TARGET 和排队时间，再评估批处理。当前没有生产量级对照。 |

关键源码：

- [`diagnosis_runtime.py`](../src/problem_locator/runtime/diagnosis_runtime.py)：直接预处理扫描、Specialist/Reviewer 执行入口。
- [`final_response.py`](../src/problem_locator/runtime/final_response.py)：Specialist 的 128 KiB 完整内联门槛。输入超过门槛后转为完整受控文件读取，可能增加模型工具回合。
- [`context_builder.py`](../src/problem_locator/runtime/context_builder.py)：Reviewer 的 snapshot、review target 和资源 manifest。
- [`agent/service.py`](../src/problem_locator/agent/service.py)、[`agent/store.py`](../src/problem_locator/agent/store.py)：会话调度、事件查询和历史加载。
- [`agent/uploads.py`](../src/problem_locator/agent/uploads.py)：会话附件保存与导入。
- [`logparse/broker.py`](../src/problem_locator/integrations/logparse/broker.py)：逐 anchor 的 target 执行。

## 目标环境复测应回答什么

先对齐实际部署 commit、package、客户端入口、角色模型命令、Reviewer 开关、附件大小及目标数。客户端与 Server 的版本须分别核对；当前工作区的版本不代表已部署版本。

已有服务端 Journey 包含排队、预处理、模型调用和上传阶段。慢 Case 先查看 `job.inputs.prepared` 的 `inputs_inlined`、`complete_input_bytes`，以及 Backend 遥测中的 turns、input/output/cache token 和模型 API 耗时。CLI/API 自报时间与服务端观察窗口不一定可直接相减，必须保留各自口径。

网站入口还要从收到用户消息开始计时，覆盖会话排队、Case 创建、后续补充信息的 INTAKE 整理和附件导入。非空问题文本建案前不调用 INTAKE；但现有 `render-journey` 按 Case ID 筛选，仍未覆盖创建前的消息接收和排队，所以 Case `brief.log` 不能代表完整网站体验。需把会话事件与服务端阶段对齐，不能把未覆盖的时间当作零。

现有 MCP 的 30 秒长轮询在状态变化后可提前返回，不是每轮固定休眠 30 秒。调整轮询频率前，先区分服务端工作时间、客户端模型思考及两次调用间的空档。

对同一批输入同时记录耗时分布和诊断质量；并发能力另测 1/2/3 个 Case、峰值内存和排队分布。Dev 的 `performance=NOT_CALIBRATED` 表示性能基线尚未校准，功能 PASS 不能替代产品性能验收。

历史样本、条件及限制见 [`performance-v10.md`](performance-v10.md)。
