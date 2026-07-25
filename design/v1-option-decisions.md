# V1 方案选择记录

状态：总体粗设计决策已确认；详细设计事项持续更新
更新时间：2026-07-25

## 1. 文档定位

本文记录 Problem Locator 正式 V1 设计过程中比较过的候选方案、最终选择、未采纳原因、接受的限制和未来复议条件。

- [《V1 总体框架粗设计》](v1-overall-framework.md)记录当前有效的总体设计结论。
- [《V1 Agent 接入与文件传输设计》](v1-agent-access-and-file-transfer.md)记录接入与文件传输方向。
- 本文解释“为什么这样选择”，不替代上述设计正文，也不把候选方案写成实现要求。
- 当前处于总体粗设计阶段；接口路径、字段、状态机及实现技术尚未进入最终详细设计。

除安全基线相关决策外，本文中方案的取舍主要基于功能、安装成本、实现复杂度、可靠性和演进能力，不以安全为由自动排除方案。

## 2. 状态定义

| 状态 | 含义 |
|---|---|
| 已确认 | 已纳入当前 V1 目标方案 |
| 暂缓 | 当前不实现，但保留后续接入或复议空间 |
| 未采纳 | 已比较但不进入当前 V1 |
| 待详细设计 | 只确认方向，具体机制尚未确定 |
| 已替代 | 曾经提出，随后被新的明确选择替代 |

## 3. 决策索引

| 编号 | 主题 | 当前选择 | 状态 |
|---|---|---|---|
| OPT-001 | 设计推进粒度 | 先完成总体粗设计，再统一详细设计 | 已确认 |
| OPT-002 | CLI 接入与用户侧交付 | Agent Skill + Remote MCP，不安装本地服务 | 已确认 |
| OPT-003 | 控制与文件传输 | MCP 传控制，Skill/curl + HTTP 传文件 | 已确认 |
| OPT-004 | 服务端业务归一方式 | 两个 Adapter 直接复用 Application Service | 已确认 |
| OPT-005 | Web 上传时机 | V1 暂不实现，未来复用相同接口 | 已确认 |
| OPT-006 | 执行与等待模型 | 统一异步，支持立即返回和有限同步等待 | 已确认 |
| OPT-007 | 诊断状态权威来源 | 服务端 Case 管理客户端可见业务状态 | 已确认 |
| OPT-008 | 对外服务入口 | 同一稳定地址和监听，不同路径 | 已确认 |
| OPT-009 | Case 定位与恢复强度 | 仅保证当前服务进程内已知 `case_id` 的继续操作 | 已确认 |
| OPT-010 | V1 部署目标 | 单节点低并发，保留多实例演进边界 | 已确认 |
| OPT-011 | 接口兼容策略 | 不兼容 Demo，正式 V1 为兼容起点 | 已确认 |
| OPT-012 | 上传信息返回形式 | 返回结构化信息，由 Skill 构造命令 | 已确认 |
| OPT-013 | 安全基线 | 受控内网下不增加额外安全措施 | 已确认 |
| OPT-014 | 实现仓库边界 | 当前仓库只设计，在新仓库实现 | 已确认 |
| OPT-015 | 跨 Job 的 Agent 上下文 | 同 Agent 保持 Session，不同 Agent 结构化交接 | 已确认 |
| OPT-016 | Application Service 后的执行形态 | 进程内 Dispatcher + 有界 Worker Handler | 已确认 |
| OPT-017 | 多 Agent 流程编排 | Coordinator 通过类型化 Job 推进 | 已确认 |
| OPT-018 | Diagnosis Runtime 与 Session 解析 | 共享 Runtime + Agent Profile + Case Session Registry | 已确认 |
| OPT-019 | Skill、工具和工作区注入 | 按 Agent 角色注入，使用 Case Workspace + Session 子目录 | 已确认 |
| OPT-020 | 同一 Case 的执行并发 | 同时只运行一个活跃诊断 Job | 已确认 |
| OPT-021 | Diagnosis Skill Catalog 来源与更新 | 随服务版本发布，启动时扫描，运行期不热更新 | 已确认 |
| OPT-022 | Agent Backend 边界 | Runtime 依赖统一 Agent Backend 接口 | 已确认 |
| OPT-023 | 数据保留边界 | 持久化业务数据，不持久化或恢复 Agent 运行上下文 | 已确认 |
| OPT-024 | General Code Agent 的 V1 范围 | 保留扩展边界，V1 不实现 | 已确认 |

## 4. 方案比较

### OPT-001：设计推进粒度

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| 立即完成 CLI 到服务端的完整详细设计 | 能快速得到接口和时序 | Application Service 后续框架未确定，容易提前固化错误边界 | 未采纳 |
| 先完成总体粗设计，再统一详细设计 | 先稳定模块职责和演进边界，减少返工 | 暂时不能直接进入实现 | **已确认** |

CLI 到 Application Service 可以先独立确定粗略接入边界，但详细接口仍需在总体框架完成后，与 Application Service 内部处理一并校验。

复议条件：总体框架确认完成后，本决策自然进入详细设计阶段。

### OPT-002：CLI 接入与用户侧交付

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| Local MCP Server | 本地文件访问和协议封装集中 | 用户需要安装、启动和升级本地服务 | 未采纳 |
| 项目专用本地 CLI 或常驻进程 | 可封装上传、重试和状态管理 | 仍增加发布、安装和版本维护成本 | 未采纳 |
| HTTP-only + Skill/curl | 协议数量较少 | 弱化 MCP 的工具发现、结构化参数和 Agent 集成体验 | 未采纳 |
| Agent Skill + Remote MCP，使用系统能力处理本地操作 | 不安装本地服务，保留 MCP 工具体验 | 依赖 Agent 环境支持 Remote MCP、Shell 和 curl | **已确认** |

当前选择优先满足“用户不能接受安装本地 MCP 服务”的约束。Remote MCP 仍是 Agent 的重要接入适配器，但不是业务核心。

复议条件：目标 Agent 不支持 Remote MCP，或者以后允许发布项目专用本地组件。

### OPT-003：控制与文件传输

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| 所有内容均通过 MCP | 表面上只有一种协议 | 文件字节进入 MCP JSON 后不利于大文件、流式传输和本地文件访问 | 未采纳 |
| 所有内容均通过 HTTP | 传输协议统一 | Agent 失去 MCP 的结构化工具接口和能力描述 | 未采纳 |
| Local MCP 或专用 CLI 代传文件 | 客户端交互可被完整封装 | 增加本地安装与升级成本 | 未采纳 |
| MCP 传控制和结构化数据，Skill 调用 curl 通过 HTTP 传文件 | 各协议承担适合的职责，不需要本地服务 | Skill 需要执行 Shell；控制与文件使用两种协议 | **已确认** |
| Web 页面上传 | 不要求 Agent 直接访问本地文件 | V1 需要额外建设页面和交互 | 暂缓 |

这里的“归一”是将 Case、Attachment 和业务规则归一到 Application Service，而不是强制归一为一种传输协议。

安全影响：当前方案允许 Skill 在 Agent 所在用户的权限范围内读取文件；路径、命令和上传地址可能进入 Agent 或 Shell 日志。当前受控内网基线接受这些影响，不增加路径白名单或上传二次确认。

复议条件：需要支持没有 Shell/curl 的客户端，或文件规模使当前 HTTP 上传方式不再适用。

### OPT-004：服务端业务归一方式

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| MCP Adapter 再回调本服务公开 HTTP API | 表面上复用同一个 HTTP 接口 | 产生进程内协议回绕、额外网络语义和 HTTP 耦合 | 未采纳 |
| MCP Adapter 与 HTTP Adapter 各自实现业务规则 | 两条链路可以独立开发 | Case、Attachment 和 Job 规则重复且容易不一致 | 未采纳 |
| 两个 Adapter 直接调用同一个 Application Service | 无内部协议回绕，业务规则只有一份 | 需要维护清晰的应用层接口 | **已确认** |

这一选择使 MCP 和 HTTP 都保持为协议适配层；Application Service 是服务端业务归一边界。

复议条件：暂无。即便未来拆分进程，也应继续保持单一应用语义，而不是复制业务规则。

### OPT-005：Web 上传时机

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| V1 同时建设 Web 管理和上传页面 | 用户无需 Shell 即可上传 | 扩大 V1 范围和前端交付成本 | 未采纳 |
| V1 使用 Skill/curl，未来 Web 复用相同 Attachment 能力 | 当前交付较小，未来不需要重建文件体系 | 无 Shell/curl 的客户端在 V1 无法直传 | **已确认** |
| 未来另建 Web 专用上传系统 | 可按页面需求独立设计 | 形成重复数据模型和业务规则 | 未采纳 |

复议条件：开始建设 Web 管理页面，或者必须支持没有 Shell/curl 的用户环境。

### OPT-006：执行与等待模型

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| 全程同步阻塞直至诊断完成 | 客户端调用直观 | 长连接易受 MCP 超时、网络和进程退出影响 | 未采纳 |
| 仅支持异步立即返回 | 服务端和客户端语义简单 | 短任务也必须额外查询，交互体验较差 | 未采纳 |
| 分别实现同步和异步两套诊断引擎 | 可独立优化两种模式 | 重复执行逻辑和状态规则 | 未采纳 |
| 底层统一异步，客户端可立即返回或有限同步等待 | 一套执行模型兼顾短任务体验和长任务稳定性 | 需要提供查询与有限等待语义 | **已确认** |

有限同步等待超时后只返回当前状态并自然转为异步，不取消、不重建任务。具体等待时长和状态查询方式待详细设计。

复议条件：不重评统一异步原则；只在详细设计中确定等待和查询机制。

### OPT-007：诊断状态权威来源

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| Agent 对话上下文为主要状态 | 实现最少 | 上下文丢失即失去正式状态，不利于 Web 和未来多实例 | 未采纳 |
| 客户端与服务端各自维护权威状态 | 客户端可离线保留更多内容 | 需要处理冲突、覆盖和双向合并 | 未采纳 |
| 服务端 Case 管理客户端可见业务状态 | CLI、Web 和 Worker 读取同一状态，客户端不维护第二份权威数据 | 服务端必须维护 Case 状态和并发一致性 | **已确认** |

客户端可以保存 `case_id` 和可丢弃缓存，但不能用本地缓存覆盖服务端 Case。Case 对外部业务状态负责；Agent 的非持久化完整对话按 OPT-015 由服务端 Agent Session 持有。

复议条件：暂无，这是正式 V1 后续兼容和多实例演进的稳定原则。

### OPT-008：对外服务入口

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| 同一域名、同一端口、不同路径 | 客户端只配置一个稳定地址 | 服务进程需要挂载两种 Adapter | **已确认** |
| 同一域名、不同端口 | 服务监听可以独立 | 客户端仍要维护两个端口 | 未采纳 |
| MCP 与 HTTP 使用完全独立地址 | 可以独立部署和扩缩容 | 当前没有对应容量收益，增加配置与运维 | 未采纳 |
| V1 增设独立 Gateway 或负载均衡组件 | 提前具备统一路由组件 | 单节点阶段增加不必要部署组件 | 未采纳 |

统一入口是部署和路由边界，不是新增业务层。V1 可由同一个服务进程监听并按路径分发；未来即使增加负载均衡，客户端稳定地址也应保持。

复议条件：出现独立部署、独立扩缩容或多实例入口需求。

### OPT-009：Case 定位与恢复强度

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| 仅在当前服务进程内使用已知 `case_id` 继续未完成诊断 | 实现和使用模型最简单 | `case_id` 丢失时无法找回；服务重启后未完成诊断只能重新创建 Case | **已确认** |
| Skill 维护本地 Case Locator | 上下文丢失后可自动定位 | 增加本地状态、路径兼容和多 Case 选择逻辑 | 未采纳 |
| 服务端按用户或客户端列出历史 Case | 可跨设备查找 | 需要稳定身份或客户端关联；当前无身份时无法可靠判断“我的 Case” | 未采纳 |
| MCP 会话保存 Case 定位 | 无额外本地文件 | MCP 会话本身也可能丢失，不能承担恢复职责 | 未采纳 |
| 检查点、启动扫描、自动重调度和跨重启续跑 | 故障恢复体验最好 | 显著增加任务、幂等、检查点和调度复杂度 | 未采纳 |

V1 只保证当前服务进程生命周期内的正常多轮诊断和基于已知 `case_id` 的继续操作。服务重启后的未完成 Case、Job 和 Agent Session 均不保证继续，用户可以重新创建 Case。

复议条件：明确提出历史 Case 查找、高可用、自动恢复或跨设备继续需求。

### OPT-010：V1 部署目标

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| V1 直接实现多实例高可用 | 初始即具备故障切换能力 | 引入共享存储、协调、重调度和运维复杂度 | 未采纳 |
| 单节点低并发，模块边界允许未来升级 | 符合当前负载和可靠性要求 | V1 不提供实例级高可用 | **已确认** |
| 保持 Demo 形态，不考虑演进边界 | 当前改动最少 | 正式化后容易再次重构外部接口和模块边界 | 未采纳 |

V1 不提前指定 PostgreSQL、Redis、对象存储、负载均衡或 Worker 集群。

复议条件：实际容量、维护窗口或可用性目标明确要求多实例。

### OPT-011：接口兼容策略

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| 正式版兼容当前 Demo | 可以平滑沿用现有调用 | 为尚未正式发布的接口背负兼容成本 | 未采纳 |
| 不兼容 Demo，以正式 V1 作为兼容起点 | 可以重新建立清晰边界 | 现有 Demo 调用方需要切换 | **已确认** |

正式 V1 发布后，现有字段语义不得静默改变，兼容新增使用可选字段，破坏性变化使用新的主版本。

复议条件：Demo 兼容不再复议；正式 V1 发布后开始承担接口兼容责任。

### OPT-012：上传信息返回形式

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| 服务端返回拼接完成的 curl/Bash/PowerShell 命令 | Skill 可直接执行 | 协议绑定特定 Shell、操作系统和转义规则 | 未采纳 |
| 服务端返回结构化上传信息，由 Skill 构造命令 | 协议与客户端实现解耦 | Skill 需要负责本地命令构造 | **已确认** |

复议条件：暂无。未来 Web 或其他客户端同样消费结构化上传信息。

### OPT-013：安全基线

当前选择是在受控内网、用户可信、文件可信的前提下，不默认增加额外安全措施。

| 候选措施 | 能解决的问题 | 引入成本或使用影响 | 当前结论 |
|---|---|---|---|
| 不增加额外安全措施 | 不增加交付、运维和用户操作成本 | 接受下述内网调用、传输和本地文件风险 | **已确认** |
| TLS | 降低传输被观察或修改的风险 | 证书、入口和续期运维 | 未采纳 |
| 用户或客户端认证 | 限制 Case 与文件的访问主体 | 身份、凭据和授权管理 | 未采纳 |
| 短期上传凭证 | 限制上传地址的可用范围和时间 | 凭证签发、校验和过期处理 | 未采纳 |
| 本地路径限制或上传确认 | 降低 Skill 误传其他本地文件的可能性 | 增加客户端逻辑和用户交互 | 未采纳 |

当前接受的安全影响：

- 能访问服务地址的内网主体可以调用 Case、上传和下载接口。
- 网络流量可能被内网中的其他主体观察或修改。
- Skill 可以读取 Agent 所在用户权限范围内的其他文件。
- 本地路径、上传地址和命令可能出现在 Agent 或 Shell 日志中。
- Case ID、Attachment ID 和 Artifact ID 只是资源标识，不构成访问授权。

任何可选安全措施只有在用户明确选择后才能纳入设计。信任边界发生变化时可以提出复议，但不得自动加入。

### OPT-014：实现仓库边界

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| 直接改造当前 Demo 仓库 | 可以就地验证 | 将设计讨论与正式实现混合，受 Demo 结构影响 | 未采纳 |
| 当前仓库只保存设计，正式代码在新仓库实现 | 正式版本可以按新边界建立工程结构 | 需要后续新建和初始化仓库 | **已确认** |

当前仓库不修改 `src/`、测试或部署脚本来实现正式版本，除非用户另行明确要求。

### OPT-015：跨 Job 的 Agent 上下文

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| 每个 Job 输出结构化上下文快照，下一个 Job 重建 Agent | Worker 无状态，便于服务重启和多实例接手 | 需要设计完整快照、持续压缩上下文和重建规则 | 未采纳 |
| 每个 Job 重放完整 Agent 对话 | 不保留常驻 Agent 进程 | 对话和 Token 持续增长，启动成本较高 | 未采纳 |
| 所有路由、专项和通用分析共用一个 Agent Session | 上下文天然连续 | 不同 Agent 角色和运行配置无法独立，职责耦合 | 未采纳 |
| 同一个 Agent 跨 Job 保持 Session，不同 Agent 使用结构化信息交接 | 同 Agent 延续完整对话，不需要故障恢复快照，同时支持不同 Agent 分工 | 占用空闲 Agent 进程资源，存在 Session 亲和性，服务重启后上下文丢失 | **已确认** |

一个 Job 是对 Agent Session 的一次有限调用。Agent 返回等待用户输入或附件时，Job 结束并释放活跃 Worker 执行名额，但 Agent Session 可以保持空闲存活。用户补充数据后，新 Job 继续调用同一个 Session。

V1 的 Router Agent 和 Specialist Agent 使用不同 Session；切换 Agent 时只传递结构化路由结论、事实、证据和下一步目标，不要求共享完整内部对话。未来增加 General Code Agent 时沿用同一原则。

这一选择替代了“每个 Job 必须持久化足以重建 Agent 的完整诊断上下文”这一更强方案。它依赖已经确认的单节点、低并发和不考虑服务重启恢复的边界。

复议条件：开始建设多实例 Worker、要求 Agent 进程故障接管、需要跨服务重启继续诊断，或大量等待中的 Session 产生不可接受的资源消耗。

### OPT-016：Application Service 后的执行形态

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| Application Service 直接执行耗时诊断 | 组件最少 | 接入、状态维护和 Agent 执行耦合，难以实现清晰的异步与并发限制 | 未采纳 |
| 进程内 Dispatcher + 有界 Worker Handler | 保持单进程部署，同时分离编排、调度和执行 | 运行任务仍依赖当前进程，不提供跨重启恢复 | **已确认** |
| 外部任务队列 + 独立 Worker 服务 | 可独立扩容并支持多实例执行 | 引入消息队列、多进程部署和运维复杂度 | 暂缓 |

Application Service 负责业务命令和 Case 状态，不直接运行耗时 Agent。Diagnosis Coordinator 创建 Job，Dispatcher 按类型选择 Worker Handler。V1 的 Dispatcher、Worker Handler 和共享 Diagnosis Runtime 都位于同一个服务进程。

复议条件：出现独立扩容、跨实例调度或明确的高可用要求。

### OPT-017：多 Agent 流程编排

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| Router Agent 内部直接调用下一个 Agent | Agent 间调用直观 | 路由和诊断形成不透明长任务，难以独立调度和记录状态 | 未采纳 |
| 一个通用 Agent 同时承担路由、专项和代码分析 | 上下文无需交接 | Agent 职责和配置耦合，难以让不同 Worker 承担不同工作 | 未采纳 |
| Coordinator 根据结构化结果创建类型化 Job | 不同诊断阶段可以由不同 Worker/Agent 执行，Case 中保留清晰执行链 | 增加 Job 边界和结构化结果约定 | **已确认** |

初始问题由路由类 Job 交给 Router Agent。V1 中 Router Agent 返回结构化 RouteDecision，Coordinator 再创建专项 Skill Job；没有匹配 Skill 时返回“无可用诊断能力”的结构化结果。后续 Agent 返回的等待输入、等待附件、完成、失败或重新路由结果也由 Coordinator 推进 Case。

V1 实现 Routing Worker 和 Skill Diagnosis Worker。General Code Worker 作为后续扩展，不在 V1 创建空 Handler 或占位实现，详见 OPT-024。

复议条件：暂无。具体 Job 类型、结果枚举和路由策略留到详细设计。

### OPT-018：Diagnosis Runtime 与 Session 解析

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| 每类 Worker Handler 自行创建 Agent、加载 Skill、装配工具和工作区 | 单个 Handler 代码直观 | 三类 Handler 重复生命周期和装配逻辑，后续容易漂移 | 未采纳 |
| 共享 Diagnosis Runtime，根据 Agent Profile 组装或复用 Session | 生命周期和注入方式统一，同时保留不同 Agent 配置 | 需要定义逻辑 Profile 和 Session 解析边界 | **已确认** |
| 建设动态插件 Runtime、数据库 Registry 和热加载平台 | 扩展能力最强 | 明显超过单节点低并发 V1 的需要 | 未采纳 |

类型化 Job 只指向逻辑 Agent Profile。Runtime 使用 Case 级 Session Registry，按照 Case、Agent Profile 和专项类型定位 Session；Specialist 还需匹配相同 Diagnosis Skill。物理进程和 Backend Session ID 不进入 Job 业务契约。

用户补充参数或附件时可以继续使用原语义 Job 类型，由 Runtime 复用目标 Session，不强制增加一个通用 `CONTINUE` Job 类型。

复议条件：未来引入完全不同的执行后端时可以新增 Backend Adapter，但保持共享 Runtime 和逻辑 Profile 边界。

### OPT-019：Skill、工具和工作区注入

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| 所有 Agent 获得全部 Skill、工具和一个全局工作目录 | 装配最简单 | Router、专项和通用 Agent 职责混杂，不同 Case 文件容易互相污染 | 未采纳 |
| 每个 Agent Session 拥有完全独立的 Skill、工具和完整工作区副本 | Session 边界清楚 | 跨 Agent 共享附件和证据需要复制，代码仓复制成本较高 | 未采纳 |
| 按 Agent 角色注入 Skill 与 Tool Bundle，每个 Case 一个 Workspace、Session 使用子目录 | 角色清晰，跨 Agent 共享方便，文件归属明确 | 需要维护 Profile、Tool Bundle 和目录职责 | **已确认** |

注入规则：

- Router Agent 获得路由规则和 Diagnosis Skill 摘要目录。
- Specialist Agent 只获得选定的完整 Diagnosis Skill。
- General Code Agent 后续可以获得通用代码定位流程，但 V1 不创建其 Profile、Tool Bundle 或 Workspace。
- 工具以逻辑 Tool Bundle 表达，由 Runtime 适配为本地库、CLI、MCP Tool 或其他后端绑定。
- Case Workspace 保存输入、共享材料和 Artifact；Session 使用自己的子目录。

Tool Bundle 与 Workspace 子目录当前不构成安全权限或沙箱边界。Agent 仍可以服务账号权限访问其可见文件和命令；这是无额外安全措施方案已接受的影响。

复议条件：需要动态 Skill 平台、强隔离工作区或独立扩缩容某类领域工具。

### OPT-020：同一 Case 的执行并发

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| 同一 Case 并行运行多个诊断 Agent Job | 可能缩短复杂问题的探索时间 | Case 状态、证据、Session 和结果合并明显复杂化 | 未采纳 |
| 同一 Case 同时只运行一个活跃诊断 Job | 状态和 Agent Session 推进顺序明确 | 同一 Case 无法并行探索多个方向 | **已确认** |

不同 Case 仍可通过有界 Worker Pool 并发执行。同一 Agent Session 同时也只能处理一个 Job；具体锁和队列留到详细设计。

复议条件：未来明确需要并行多 Agent 推理，并愿意引入结果合并和冲突处理模型。

### OPT-021：Diagnosis Skill Catalog 来源与更新

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| Diagnosis Skill 随服务版本发布，服务启动时扫描静态目录 | 实现和运维最简单；代码、工具与 Skill 可以一起验证、发布和回滚；多实例使用相同发布包时版本天然一致 | 修改 Skill 也需要重新发布并重启服务 | **已确认** |
| 独立 Skill 仓库，发布版本化静态快照到服务端 | Skill 可以独立迭代，仍不需要动态 Registry | 增加仓库、发布流程以及服务与 Skill 的兼容管理 | 暂缓，作为后续优化方向 |
| 动态 Skill Registry 和运行期热更新 | 支持动态启停、灰度和管理页面 | 需要处理缓存、版本一致性、回滚和多实例同步，超过 V1 需求 | 暂缓 |

V1 为 Skill 保留稳定的 `skill_id` 和不可原地覆盖的 `skill_version`。Job 和 Specialist Session 使用逻辑 `skill_id@version`，不暴露发布包中的物理路径。Runtime 只依赖 Diagnosis Skill Catalog 接口，因此未来替换 Catalog 来源不影响 Case、Job、Agent Profile 和 Session 的基本语义。

当前不增加 Skill 签名、发布审批或运行沙箱等安全措施。服务信任随发布包提供的 Skill；能够修改服务发布内容的主体也可以改变 Agent 指令和工具使用行为。该影响纳入已确认的受控内网安全基线。

复议条件：Skill 需要脱离服务代码独立发布，或者多实例环境需要集中启停、灰度和动态更新。

### OPT-022：Agent Backend 边界

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| Runtime 直接调用具体 Agent 进程或模型 API | 组件最少 | Runtime 与具体 Agent 实现耦合，更换执行方式时会影响 Session 和 Job 处理 | 未采纳 |
| Runtime 依赖统一 Agent Backend 接口 | 逻辑 Session、业务结果与物理执行解耦，方便测试及增加其他 Agent 实现 | 需要定义一套通用 Session 和 Turn 执行协议 | **已确认** |
| Backend 同时管理 Skill、Workspace、逻辑 Session 和 Agent 执行 | 单个 Backend 自包含 | 不同 Backend 会重复 Runtime 职责，Profile、Skill 和 Session 规则容易漂移 | 未采纳 |

Coordinator 创建 Job，Runtime 将一个面向 Agent 的 Job 转换为目标逻辑 Agent Session 上的一次 Turn。一个 Job 执行一次后结束；多个顺序 Job 可以复用同一个 Session。`WAITING_INPUT` 或 `WAITING_ATTACHMENT` 结束当前 Job，但不要求关闭对应 Session。

Runtime 负责 Profile、Skill、Tool Bundle、Workspace、逻辑 Session 解析以及从 Agent 结果到 `JobOutcome` 的转换。Backend 只负责物理 Session 的创建、单轮调用、提供方响应与错误标准化以及关闭。物理 Session Handle 是 Backend 的不透明值，不进入 Case、Job 或外部接口。

V1 可以只有一个 Backend 实现。具体使用 Claude Code 子进程、模型 SDK 还是远程 Agent API，以及 Agent Backend 的方法签名和错误分类，留到详细设计。

Agent Backend 是扩展边界，不构成安全隔离。V1 不增加沙箱、独立系统账号或 Backend 级权限控制；物理 Agent 继承服务进程权限，这是当前无额外安全措施基线的一部分。

复议条件：未来 Agent 执行方式无法映射为“创建物理 Session、执行 Turn、关闭 Session”的通用模型。

### OPT-023：数据保留边界

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| 所有数据只在当前服务进程内存在 | 实现最简单 | 重启后 Case、附件和结果全部丢失 | 未采纳 |
| 只持久化最终结果和 Artifact | 存储内容较少 | 无法完整回看输入、附件和结构化诊断过程 | 未采纳 |
| 持久化业务数据，不持久化 Agent 运行上下文 | 可以凭已知 `case_id` 查询历史 Case 和结果，同时不引入 Agent Session 恢复复杂度 | 需要 Repository、BlobStore 和数据清理机制 | **已确认** |
| 持久化完整 Agent 对话、模型事件和工具轨迹 | 审计信息最完整 | 数据量、数据模型和恢复语义明显复杂化 | 未采纳 |

V1 持久化 Case、用户输入、Job 及结构化 Outcome、路由与 Skill 版本、结构化交接、已经发布为 `READY` 的 Attachment 原始文件、最终结果、Evidence 和 Artifact。Agent 完整对话、Backend Handle、PID、模型原始流式事件、Session 临时目录和未提升的中间文件不持久化。

已完成 Case 可以凭已知 `case_id` 跨服务重启查询并下载保留期内的 Artifact；V1 不因此提供历史 Case 列表、搜索或自动找回。未完成 Case 在重启后不恢复、不自动重新创建 Job，并向客户端呈现为不可恢复的中断状态；具体状态名和最小状态归一化机制留到详细设计。状态归一化只用于避免继续展示过期的运行状态，不属于任务恢复扫描。

V1 通过逻辑 Repository 和 BlobStore 持久化数据，具体数据库、文件布局和保留天数留到详细设计。未来多实例可替换为共享数据库和对象存储，而不改变业务数据语义。

当前不增加静态数据加密、用户级隔离或细粒度访问控制。持久化会延长问题描述、日志附件和诊断结果的磁盘留存时间，能访问相关服务或服务器文件的主体可能读取这些数据。

复议条件：存储容量、审计要求或数据合规要求需要缩短、延长或扩大保存范围。

### OPT-024：General Code Agent 的 V1 范围

| 候选方案 | 优点 | 代价或局限 | 结论 |
|---|---|---|---|
| V1 完整实现 General Code Agent | 初版即可处理没有专项 Skill 的代码问题 | 需要同时设计代码仓接入、代码 Workspace、工具配置和新的 Agent 流程，扩大 V1 范围 | 未采纳 |
| 保留 General Code Agent 的架构扩展边界，V1 不实现 | 控制 V1 范围，同时保留后续接入 Runtime、Backend 和结构化交接的路径 | V1 对没有匹配 Diagnosis Skill 的问题只能返回无可用诊断能力 | **已确认** |
| 从设计中完全删除 General Code Agent 概念 | V1 文档最精简 | 后续引入时需要重新识别 Worker、Profile、Session 和 Workspace 扩展点 | 未采纳 |

V1 只实现 Router Agent 和 Specialist Agent，不实现 General Code Job、Worker、Agent Profile、Session、代码仓接入、代码 Workspace Binding 或工具配置，也不创建空 Handler 和占位 Session。总体架构图使用虚线保留该扩展位置。

V1 的 Router 只在已启用的 Diagnosis Skill 中选择目标；没有匹配能力时返回结构化的“无可用诊断能力”结果。具体状态或结果名称留到详细设计。

未来实现 General Code Agent 时，应复用共享 Diagnosis Runtime、统一 Agent Backend、独立 Agent Session 和结构化交接边界。服务端代码仓的配置、选择和安全影响届时重新讨论，不作为 V1 要求。

复议条件：V1 发布前出现必须由通用代码分析覆盖、且无法通过 Diagnosis Skill 实现的明确场景。

## 5. 尚未选择或待详细设计

以下事项尚未形成最终方案，不得从本文推断为既定实现要求：

- 有限同步等待的具体时长。
- 轮询、长轮询、通知或其他状态获取方式。
- MCP 工具名、HTTP 精确路径、请求字段和响应字段。
- 任务幂等键、错误码和重试规则。
- Case 并发控制采用版本号、条件更新、锁还是队列串行。
- `WAITING_INPUT`、`WAITING_ATTACHMENT` 的完整状态转换。
- 诊断完成前后再次请求补充参数或附件的完整循环。
- Case Session Registry、空闲回收、容量上限和 Session 串行调用方式。
- 跨 Agent 结构化交接信息的具体格式。
- Application Service、Coordinator 和 Dispatcher 的具体接口及事务边界。
- Job 类型、Job 结果、RouteDecision 的精确结构和 Worker 并发参数。
- Agent Profile、逻辑 Session Key 和 Job 目标的具体字段。
- Agent Backend 的 Session 配置、Turn 输入、标准化结果、错误分类和关闭协议。
- Diagnosis Skill manifest 字段、包目录结构和内容校验规则。
- Tool Bundle 到本地库、CLI、MCP Tool 或其他能力的具体映射。
- Case Workspace 和 Session 子目录的具体组织方式。
- BlobStore Attachment 绑定或物化到 Case Workspace 的具体方式。
- Repository、BlobStore 的具体产品、数据结构、文件布局、清理机制和保留天数。
- PostgreSQL、Redis、对象存储、Worker 集群及多实例协调方式。

此前讨论中出现过的 PostgreSQL、Redis Queue、Skill Registry 和 MCP Registry 等属于候选设想，尚未被确认为正式 V1 方案。
