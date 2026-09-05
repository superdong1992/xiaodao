# 7.0 第一轮性能优化与测量记录

本轮从 `e55a08c` 实施 V10 存储、启动资产快照、CLI 最终 JSON、独立队列和后台 ZIP。存储规模与上传桥接的重复工作已经减少，模型文件工具往返也已消除；**本次真实 RPC 的端到端耗时没有下降**。模型推理仍是单 Case 延迟的主要变量，不能把组件提速倍数直接当作产品提速倍数。

## 实现与取舍

| 改动 | 当前行为 | 影响与限制 |
|---|---|---|
| 活动状态 | Case、Job、幂等记录和索引在内存，按 Case 锁与 revision 更新 | 服务退出后活动任务丢失，需要重新创建 |
| 终态持久化 | 引用资源与报告先同步落盘，再提交 SQLite WAL/FULL 事务，最后通知；历史按需加载 | 不逐次核验历史文件；已知存储故障使 readiness 失败 |
| 启动资产 | Skill registration、方法卡、提示词和合同冻结；Workspace 使用同一份字节 | 更新后重启；Logparse 使用固定发布目录，运行期不要覆盖 |
| ROUTE | 最终响应只含 `skill_id`、`reason`、`confidence`，服务端补全权威元数据 | 非法 JSON、未知 Skill、异常退出直接失败，不自动修复 |
| Specialist | 完整输入不超过 128 KiB 时内联；较大输入列出完整受控 Read 文件；最终 JSON 无 Write | 不静默裁剪；marker、来源、行号、日志原文和身份校验保留 |
| 并发 | ROUTE 1、DIAGNOSE/REVIEW 2、Logparse 1、ZIP 1；同 Case 串行 | 增加并发资源消耗；仍是单进程 Server |
| 上传 | 事件循环合并小帧，每批最多 1 MiB，固定容量并及时释放已消费批次，单次流式 hash | 自有 payload 缓冲最多 2 MiB；HTTP 服务器拥有的输入帧不计入自有缓冲 |
| 结果 | 先交付 JSON，后台流式 DEFLATE level 1 ZIP；`archive_status` 四态 | ZIP 更晚可用，可能更大；失败不撤回 JSON，待归档任务可跨重启继续 |
| MCP/扫描 | 默认紧凑进度，显式 `include_details=true` 返回详情；写响应可直接带下载信息；Workspace 每秒扫描及退出检查 | 七个公开输入仍扁平；取消检查仍为 50 ms；未改变客户端自动续诊协议 |

升级必须使用新的 `DATA_ROOT`。旧目录原样保留，没有迁移、双写或旧格式读取。

## 原生 WSL + Claude + DeepSeek

测量于 2026-09-05 完成，run ID 为 `run-20260905T142155Z`，Case 为 `RESOLVED`。运行前已检查计划：最多 2 次 Agent，每次最多 24 turns、600 秒、CLI 记账预算 1 美元；实际恰好 2 次，无重试。没有使用 Docker 或密封 Ubuntu 认证入口。

环境为 WSL2 原生 Linux、i7-14700、28 个逻辑 CPU、约 15.45 GiB 内存。源码和服务数据在 Linux 文件系统；Python 3.12.13、Node 24.16.0、Claude Code 2.1.89，模型为 DeepSeek 官方接口的 `deepseek-v4-flash[1m]`。Logparse 为干净提交 `a233b500d9c99e6815d1ffd82cb4ca55bbfe657a`。

输入沿用前次 RPC fixture：1 张方法卡、2 个日志文件、3 行原文共 561 字节，上传 ZIP 571 字节，Reviewer 关闭。客户端用官方 MCP SDK 经真实 HTTP 调用，自动上传并提交附件，没有人工操作等待。这不是交互式 Claude 客户端模型全程测量，也不覆盖局域网网络与客户端思考耗时。

| 指标 | 前次 6.0 样本 | 本次 7.0 样本 |
|---|---:|---:|
| ROUTE Agent 执行窗口 | 8.393 s | 5.177 s |
| Specialist Agent 执行窗口 | 18.334 s | 47.984 s |
| Logparse 预处理 | 0.563 s | 0.586 s |
| 创建到 JSON 可见 | 28.099 s | 54.021 s |
| 创建到 JSON、ZIP 均下载验证完成 | 28.116 s | 54.296 s |
| ROUTE turns / 文件工具 | 2 / 1 Write | 1 / 0 |
| Specialist turns / 文件工具 | 7 / 5 Read、1 Write | 1 / 0 |
| Workspace 扫描次数 / 累计耗时 | 530 / 216.21 ms | 56 / 24.67 ms |
| CLI 累计 token，含缓存读取 | 69,695 | 25,485 |
| 其中缓存读取 token | 44,800 | 0 |
| CLI 记账费用 | $0.236415 | $0.289445 |

前次源码是 `443ca219456c3b84d78cd0864fbd5bcc2f94c9a8`，**不是本轮精确基线 `e55a08c` 的受控 A/B**。两次各只有一个真实 Case，缓存情况也不同，表格不能证明平均延迟、p95、准确率或费用改善。CLI 费用没有与 DeepSeek 账单核对。

本次 Specialist 完整输入为 14,653 字节，全部内联。CLI 记录 7,504 个输出 token，观察到的 thinking 文本为 29,073 字节，最终正文为 2,049 字节；前次 Specialist 输出为 3,183 token。本次额外时间主要落在 Agent 窗口内，模型较多推理输出是可见差异，但单样本不能隔离提示词、模型负载、缓存等因素的贡献。没有为了得到更快结果重复调用，也没有未经评估修改模型推理设置。

其他观测：

- 服务启动约 984 ms。3 个 Job 的队列等待分别为 0.638、0.711、0.623 ms，其中一个 DIAGNOSE 只做附件预检，没有启动模型。
- 571 字节上传 HTTP 往返约 17.05 ms；接收与 hash 暂存 5.80 ms、回执校验 0.008 ms、发布 5.26 ms、提交 0.71 ms。这个小文件不能代表大附件网络吞吐。
- 从 JSON 可见到两个下载校验完成约 276 ms，包含轮询观察间隔和下载，不能全部算作压缩时间。
- 每 100 ms 采样 Server 及其子进程树，最大 RSS 求和约 **481.1 MiB**。这不是物理内存峰值的精确上界，共享页可能重复计数；也未测两个真实 Specialist 同时运行的峰值。

真实测量使用实现快照 `3af1de357d9cce684b7873ff61002ccf186e57b2efb2bf038c20204e391a9b5b`，762 个文件。之后又收紧了上传缓冲分配、完善文档和测试；这些最终改动由下面的组件测量与正式确定性验证覆盖，不能声称真实模型运行对应最终交付的全部字节。

原始证据：

- [测量计划](../.tmp/perf-implementation/native-results/run-20260905T142155Z/plan.json)
- [端到端计时与下载 hash](../.tmp/perf-implementation/native-results/run-20260905T142155Z/measurement.json)
- [Journey 与 Agent telemetry](../.tmp/perf-implementation/native-results/run-20260905T142155Z/dfx/journey.jsonl)
- [RSS 采样](../.tmp/perf-implementation/native-results/run-20260905T142155Z/resources.json)
- [前次测量与限制](../.tmp/rpc-perf-20260905/PERFORMANCE_BASELINE.md)

## 上传、存储与调度组件

零模型测量入口为 [`v10_components.py`](../tools/test-flow/measurements/v10_components.py)。它保留原始输出，不产生 Test Flow verdict。原生 WSL 运行示例：

```bash
python tools/test-flow/measurements/v10_components.py \
  --baseline e55a08c \
  --output-root /absolute/new/component-measurement
```

本次输出目录位于 WSL Linux 文件系统。源码模块 hash 和各轮原始值见 [组件测量 JSON](../.tmp/perf-implementation/components.json)。

**上传桥接**：32 MiB、每个 ASGI 帧 8 KiB，旧桥接实现精确取自 `e55a08c`。两者共用当前复制/hash 下游，隔离桥接改动；各测 3 次，表中为中位数。计时含桥接、文件写和 SHA-256，但不含真实网络和 fsync；它不是完整上传 API 的吞吐承诺。

| 指标 | 基线桥接 | 当前桥接 |
|---|---:|---:|
| 32 MiB 耗时 | 320.15 ms | 99.09 ms |
| 吞吐 | 99.95 MiB/s | 322.93 MiB/s |
| 跨线程读取次数，含 EOF | 4,097 | 33 |
| 独立内存测量的 Python 分配峰值 | 61,615 B | 2,114,022 B |

本地桥接吞吐约为原来的 3.23 倍，代价是每路使用较大的固定批次。内存测量包含约 16.5 KiB 的任务、Future 和文件对象开销；payload 缓冲不超过 2 MiB。开启 tracemalloc 的额外测量不参与吞吐中位数。

**存储**：SQLite 中分别准备 1、1,000、10,000 个小型历史快照，每组对一个活动 Case 连续读取并提交 200 次。该活动 Case 始终是唯一驻留 Case，测量段 SQL 执行次数均为 0。历史规模验收另有专项测试阻止加载其他 Case；这些数据没有把 10,000 份真实大报告和资源 fsync 成本包含进来。

| 历史 Case | 活动读取+提交中位数 / p95 | 终态读取+提交中位数 / p95 |
|---:|---:|---:|
| 1 | 0.168 / 0.232 ms | 1.497 / 1.821 ms |
| 1,000 | 0.164 / 0.185 ms | 1.202 / 1.663 ms |
| 10,000 | 0.166 / 0.197 ms | 1.177 / 1.682 ms |

终态测量每组 50 次，是真实 SQLite WAL/FULL 提交，但使用无报告资源的小 Case；不能代表大型报告的最终发布延迟。主要结论是活动路径未出现随历史规模线性增长的工作量。

**调度**：用 6 个固定 100 ms 的假 DIAGNOSE 和 1 个 10 ms 的假 ROUTE，隔离调度成本。两组都使用当前独立 ROUTE 队列，仅把 DIAGNOSE worker 从 1 改为 2；各测 3 次。

| DIAGNOSE worker | 总完成时间中位数 | 诊断吞吐中位数 | ROUTE 排队范围 |
|---:|---:|---:|---:|
| 1 | 603.79 ms | 9.94 Job/s | 0.05–0.28 ms |
| 2 | 302.54 ms | 19.83 Job/s | 0.23–0.55 ms |

该结果验证队列能重叠工作，不代表真实模型吞吐翻倍。Logparse、磁盘、模型服务并发和目标机内存仍可能成为限制。

## 验收证据与后续

正式结论继续以 `tools/test-flow/run.ps1 --track dev --goal dev.default` 的当前快照 `verdict.json` 为准；最终 run ID、源码摘要和状态登记在 [`FIXED_ISSUES.md`](../FIXED_ISSUES.md) 的 7.0 元数据行。上述一次性性能观测不构成 Release、Fast E2E 或完整 Test Flow 结论。

专项保护包含：

- `test_case_store_v10.py`、`test_state_repository.py`：历史按需加载、按 Case revision、事务及索引回滚、旧目录保持原样、清理不读取历史附件。
- `test_terminal_crash.py`：真实子进程分别在终态提交前后退出；未交付活动任务可丢失，已提交 JSON 可重启查询并下载。
- `test_startup_v10.py`、资产与上下文测试：不恢复旧活动任务、不重放持久记录，Workspace 使用启动快照。
- `test_final_response.py` 与 RPC Journey：最小 ROUTE、最终 JSON、内联与大输入完整读取、证据核验。
- `test_parallel_queues.py` 与上传并发测试：同 Case 串行、跨 Case 重叠、取消信号隔离。
- `test_async_archive.py`：ZIP 延迟/失败不阻塞 JSON，待归档及 ZIP 发布中断后重启完成，归档原文 hash 一致。
- `test_http_streaming.py`：小帧合并、真实分配峰值、断连与取消。
- 客户端合同及七工具 schema lint：紧凑进度、显式完整查询、扁平输入和下载描述符。

目标机器为 4 核、8 GB、150 GB，本轮没有拿到目标机详细运行证据。下一步应在这台机器采集多 Case、真实大日志、模型并发的 RSS 与延迟分布；上传接收/校验/发布/提交已分段计时，继续定位此前约 108 秒的“等待材料”。直接模型 API、常驻 CLI 池、Logparse 多 target 批处理及模型推理设置评估仍在 [`TODO.md`](../TODO.md)。
