# 工程配套与验证设计

[返回框架设计](README.md) · [逐文件索引](file-index.md)

生产实现集中在 `src/problem_locator/`。本章解释外围配置、合同文件、测试编排、Skill 与网站示例如何支撑这些实现。工程脚本中的历史名称如 `evidence-v2` 需要结合当前配置和调用点理解，不能单凭名称判断生产使用的诊断协议。

## 1. 仓库配置与文档

| 文件 / 目录 | 设计职责与使用边界 |
| --- | --- |
| [pyproject.toml](../../pyproject.toml) | 声明 `problem-locator` 包、Python 3.12 约束、固定依赖、开发依赖和四个命令入口。wheel 只打包 `src/problem_locator`；pytest 默认目录是 `tests/deterministic`，正式验证仍由 Test Flow 调度。 |
| [uv.lock](../../uv.lock) | 固定依赖解析结果，与 `pyproject.toml` 一起决定安装环境；不能只依据本机已安装库判断可复现性。 |
| [.env.example](../../.env.example) | 部署变量示例，说明数据库、模型命令、路径、并发和开关；真实凭据放在私有环境或私有配置中，配置语义由 `Settings.load()` 校验。 |
| [.gitignore](../../.gitignore) | 划分源码与本地运行产物；Release 源码快照使用 Git 可见文件集合，因此新增规则会影响快照范围。 |
| [.gitattributes](../../.gitattributes) | 规定受版本控制文件的文本处理规则，减少平台换行差异对脚本和字节摘要的影响。 |
| [AGENTS.md](../../AGENTS.md) | 协作、修复前复现、MCP 扁平参数、客户端边界和 Test Flow 约束。 |
| [README.md](../../README.md) | 产品行为、版本、部署和公开能力的总入口。 |
| [TODO.md](../../TODO.md) | 活跃未完成事项；文档描述某项能力不等于待办已完成。 |
| [FIXED_ISSUES.md](../../FIXED_ISSUES.md) | 修复历史、不可回归行为、专项测试和最终 verdict 引用。新增架构说明不作为产品修复登记。 |
| [docs/](../../docs) | API、升级、保留、路由、诊断策略和网站接入的专题说明，供调用者和部署人员使用。 |
| [design/](..) | 框架与测试架构说明；本目录 `framework/` 给出当前工作区的模块和文件设计。其他设计材料有自己的编写基线，不能据旧版本叙述覆盖当前实现。 |
| [experiments/](../../experiments)、[handoff/](../../handoff) | 可行性实验与交接材料，不属于默认生产装配或发布证明。 |

## 2. 合同快照

`contracts/serialization.py` 从 Pydantic 合同生成规范 JSON 和 schema 字节。[tools/update-contract-snapshots.py](../../tools/update-contract-snapshots.py) 刷新已有快照，并可在明确选择时更新已评审 fixture 的大小与摘要；不自动把新文件登记成已评审 fixture，也不生成 OpenAPI。`schemas/v2/` 是保留的目录名，当前 State/Job/Outcome 版本由文件内容决定。

| 文件（均位于 `schemas/v2/`） | 约束对象 / 消费方 |
| --- | --- |
| [contract-manifest.json](../../schemas/v2/contract-manifest.json) | schema 及合同源码的 SHA-256 清单、生成器版本与合同修订，供漂移检查和测试身份绑定。 |
| [state.schema.json](../../schemas/v2/state.schema.json) | State 的 Case 集合、幂等记录和运行期记录格式；不定义 PostgreSQL 表结构。 |
| [job.schema.json](../../schemas/v2/job.schema.json) | Job 身份、阶段、上下文、revision 和运行绑定。 |
| [job-outcome.schema.json](../../schemas/v2/job-outcome.schema.json) | 服务端确认的任务结果。 |
| [agent-job-outcome.schema.json](../../schemas/v2/agent-job-outcome.schema.json) | Agent 侧 Outcome 协议对象，与服务端公开结果区分。 |
| [agent-job-outcome-draft.schema.json](../../schemas/v2/agent-job-outcome-draft.schema.json) | Agent 未封装完成的输出草稿，供规范化及封装工具校验。 |
| [workspace-input-manifest.schema.json](../../schemas/v2/workspace-input-manifest.schema.json) | 工作区允许读取的文件、资产、资源身份和绑定。 |
| [logparse-parse-claim.schema.json](../../schemas/v2/logparse-parse-claim.schema.json) | 一次受控 Logparse 解析的来源声明。 |
| [handoff.schema.json](../../schemas/v2/handoff.schema.json) | 阶段之间传递的交接对象。 |
| [user-result.schema.json](../../schemas/v2/user-result.schema.json) | 结构化用户报告，不用于替换 skill-direct Markdown。 |
| [methods-terminal-projection.schema.json](../../schemas/v2/methods-terminal-projection.schema.json) | Methods 终态的服务端投影。 |
| [methods-reviewer-result.schema.json](../../schemas/v2/methods-reviewer-result.schema.json) | Methods 审核结果格式。 |
| [method-evidence-graph.schema.json](../../schemas/v2/method-evidence-graph.schema.json) | 保留的 Methods V2 证据图 DTO。 |
| [method-evaluation-plan.schema.json](../../schemas/v2/method-evaluation-plan.schema.json) | 保留的 Methods V2 方法评估计划。 |
| [method-evaluation-response.schema.json](../../schemas/v2/method-evaluation-response.schema.json) | 保留的 Methods V2 角色响应。 |
| [method-role-evaluation.schema.json](../../schemas/v2/method-role-evaluation.schema.json) | 保留的角色评估记录。 |
| [method-limitations-record.schema.json](../../schemas/v2/method-limitations-record.schema.json) | 保留的评估限制记录。 |
| [method-state.schema.json](../../schemas/v2/method-state.schema.json) | 保留的 Methods V2 状态对象。 |
| [method-consensus.schema.json](../../schemas/v2/method-consensus.schema.json) | 保留的角色共识结果。 |
| [method-terminal-result.schema.json](../../schemas/v2/method-terminal-result.schema.json) | 保留的 Methods V2 终态结果格式。 |
| [fixture-manifest.schema.json](../../schemas/v2/fixture-manifest.schema.json) | 测试材料的用途、schema 引用、字节数和摘要清单。 |
| [web-api.openapi.snapshot.json](../../schemas/v2/web-api.openapi.snapshot.json) | HTTP OpenAPI 快照，用于检查路由和请求/响应格式变化；它与上面的合同生成脚本分别维护。 |

保留 schema 可供旧格式分析或专项测试使用，不表示其对应分支已接入默认运行链。修改字段时，应从生产模型、生成入口、快照、消费端和专项测试一起核查，不能只编辑 JSON 快照。

## 3. Test Flow 的设计

测试关系为 Goal → Proof → Stage → Gate → receipt → `verdict.json`。Goal 决定要证明什么，Proof 定义证据要求，Stage 排列依赖，Gate 执行具体检查。编排器固定输入身份、检查准入条件、选择可复用证据、记录过程并生成最终结论。

默认 `dev.default` 运行框架自测、仓库静态检查、受影响确定性测试和完整确定性测试，不调用真实模型。完整确定性测试包括独立 PostgreSQL Gate，需要专用测试管理库。SameJob 是确定性旅程。Release 则要求固定源码快照、当前客户端/服务端/外部工具身份、全新数据根和真实 CrossJob；Dev checkpoint 不能替代 fresh Release。

### 3.1 入口与配置文件

| 文件 | 输入、职责与输出 |
| --- | --- |
| [tools/test-flow/run.ps1](../../tools/test-flow/run.ps1) | PowerShell 正式入口，定位 `node.exe` 后转发参数给 `run.mjs`，原样返回退出码。 |
| [tools/test-flow/run.sh](../../tools/test-flow/run.sh) | shell 正式入口，定位仓库并转发参数给同一 Node 编排器。 |
| [tools/test-flow/run.mjs](../../tools/test-flow/run.mjs) | 解析 track/goal/stage/client/plan-only 与重试理由，拒绝未知参数，输出计划或 verdict 摘要。 |
| [tools/test-flow/evidence.mjs](../../tools/test-flow/evidence.mjs) | 证据报告及清理入口；先 report 或 dry-run，再由人按精确 run ID 执行清理，不能自动删除测试证据。 |
| [config/proofs.v2.json](../../tools/test-flow/config/proofs.v2.json) | 定义 Goals、Proofs 与依赖闭包。 |
| [config/stages.v2.json](../../tools/test-flow/config/stages.v2.json) | 定义阶段顺序、Gate 集合、类型和复用策略。 |
| [config/gates.v2.json](../../tools/test-flow/config/gates.v2.json) | 定义具体测试命令、选择器、结果要求与边界。 |
| [config/identities.v2.json](../../tools/test-flow/config/identities.v2.json) | 指定各类证明身份绑定的源码与资产范围。 |
| [config/policy.v2.json](../../tools/test-flow/config/policy.v2.json) | 定义准入、资源预算、性能和错误策略。 |
| [config/runtime-profiles.v2.json](../../tools/test-flow/config/runtime-profiles.v2.json) | 固定运行时版本、外部依赖、模型、镜像和环境允许项。 |
| [Dockerfile](../../tools/test-flow/Dockerfile)、[Dockerfile.dockerignore](../../tools/test-flow/Dockerfile.dockerignore) | 定义 Linux 服务测试镜像及构建上下文范围。 |
| [Dockerfile.client](../../tools/test-flow/Dockerfile.client)、[Dockerfile.client.dockerignore](../../tools/test-flow/Dockerfile.client.dockerignore) | 定义显式 Linux Client 镜像和客户端构建范围。 |
| [logparse-requirements.txt](../../tools/test-flow/logparse-requirements.txt) | 固定测试所需 Logparse Python 依赖。 |
| [prepare-release-cache.mjs](../../tools/test-flow/prepare-release-cache.mjs) | 准备发布所需的受版本约束依赖缓存，不产生发布 PASS。 |
| [prepare-release-settings.mjs](../../tools/test-flow/prepare-release-settings.mjs) | 从授权配置生成发布用隔离环境设置，避免把任意 Host 配置带入测试。 |

### 3.2 `tools/test-flow/lib/` 逐文件职责

| 文件 | 详细设计 |
| --- | --- |
| [config.mjs](../../tools/test-flow/lib/config.mjs) | 加载并严格校验六类 v2 配置，解析 Goal 闭包、检查依赖和允许的 Gate/Stage 类型，形成规范定义供身份计算。 |
| [planner.mjs](../../tools/test-flow/lib/planner.mjs) | 根据 Goal、平台、变更、历史和配置创建计划；选择内置 adapter，计算预算、复用决定和 admission blocker。计划阶段不调用真实模型。 |
| [identity.mjs](../../tools/test-flow/lib/identity.mjs) | 读取 Git 基线与变更文件，计算源码、阶段与性能身份，供 affected 选择和复用检查。 |
| [source-snapshot.mjs](../../tools/test-flow/lib/source-snapshot.mjs) | 枚举 tracked 当前字节和未忽略 untracked，生成 SHA-256 清单，保存/展开快照并检测漂移和越界链接。 |
| [engine.mjs](../../tools/test-flow/lib/engine.mjs) | 驱动计划、阶段、Gate、复用、资源生命周期及最终审计，归并失败、性能和 usage，最后调用证据模块生成 verdict。 |
| [actions.mjs](../../tools/test-flow/lib/actions.mjs) | 把 Gate 定义映射到 Node/pytest/仓库检查/adapter/观察动作，收集退出码、JUnit、摘要与子收据；不接受外部任意 adapter。 |
| [process.mjs](../../tools/test-flow/lib/process.mjs) | 运行有界子进程，管理超时、输出和终止，供 Gate 执行使用。 |
| [events.mjs](../../tools/test-flow/lib/events.mjs) | 输出结构化执行事件，给审计和状态汇总提供统一事件源。 |
| [status.mjs](../../tools/test-flow/lib/status.mjs) | 将功能、性能与运行状态组合成总状态和退出码，按策略判断性能偏差。 |
| [evidence.mjs](../../tools/test-flow/lib/evidence.mjs) | 创建 attempt 目录、写清单与收据、检查证据完整性、密钥和当前事件合同，生成及复核 `verdict.json`。 |
| [history.mjs](../../tools/test-flow/lib/history.mjs) | 加载历史 verdict，筛选符合当前身份和复核条件的可复用阶段，读取性能样本和失败指纹。 |
| [checkpoint.mjs](../../tools/test-flow/lib/checkpoint.mjs) | 创建和恢复开发检查点，检查其身份和文件清单；检查点是诊断加速输入，不能替代 fresh Release。 |
| [resources.mjs](../../tools/test-flow/lib/resources.mjs) | 为单次 run 登记容器、网络、卷和标签，并在检查 Docker 身份后清理本次资源；不负责删除历史测试证据。 |
| [release-inputs.mjs](../../tools/test-flow/lib/release-inputs.mjs) | 从固定 profile 读取外部工具、模型、镜像和包身份，验证缓存、CLI、Docker、浏览器与配置来源。 |
| [release-case.mjs](../../tools/test-flow/lib/release-case.mjs) | 校验发布用例 manifest，读取用户输入、Methods 注册、原始日志和 oracle 分区，计算场景摘要。 |
| [cross-job-polling.mjs](../../tools/test-flow/lib/cross-job-polling.mjs) | 实现 CrossJob 状态轮询与结束条件，集中管理等待节奏和超时。 |
| [methods-v1-oracle.mjs](../../tools/test-flow/lib/methods-v1-oracle.mjs) | 检查当前 Methods V1 场景产物、来源与预期，供发布场景断言。 |
| [methods-oracle.mjs](../../tools/test-flow/lib/methods-oracle.mjs) | 保留的 Methods V2 记录、图、计划和终态验证工具；调用方需匹配实际协议。 |
| [failure-diagnostic.mjs](../../tools/test-flow/lib/failure-diagnostic.mjs) | 从失败 Gate 和证据投影诊断信息，输出受 schema 约束的失败说明，不改变原始 verdict。 |
| [usage.mjs](../../tools/test-flow/lib/usage.mjs) | 规范化 token usage，包含输入、输出及缓存字段，识别缺失数据并求和，支持预算审计。 |
| [browser.mjs](../../tools/test-flow/lib/browser.mjs) | 定位 Chrome 可执行文件，读取版本并计算文件哈希，提供浏览器身份；不负责执行浏览器场景。 |
| [website-identity.mjs](../../tools/test-flow/lib/website-identity.mjs) | 定义合成网站用户的 namespace 和用户标识，生成稳定的 owner_key，供 Test Flow 场景共用。 |
| [website-agent.mjs](../../tools/test-flow/lib/website-agent.mjs) | 提供网站 Agent 旅程的请求、响应与状态验证辅助。 |
| [website-browser.mjs](../../tools/test-flow/lib/website-browser.mjs) | 生成上传、报告读取和下载验证的浏览器测试 HTML，实现浏览器侧 SHA-256 校验；实际 Chrome 执行由 `cross-job-core.mjs` 编排。 |
| [util.mjs](../../tools/test-flow/lib/util.mjs) | 集中实现规范 JSON、摘要、文件写入、命令定位、Python 运行时选择和错误脱敏。路径清理函数要求明确的允许根目录。 |

### 3.3 平台适配与运行辅助

| 文件 / 目录 | 设计职责 |
| --- | --- |
| [adapters/cross-job-core.mjs](../../tools/test-flow/adapters/cross-job-core.mjs) | 共用 CrossJob 驱动，连接当前 Client、Linux Server、诊断、产物和重启检查。 |
| [adapters/windows-linux-release.mjs](../../tools/test-flow/adapters/windows-linux-release.mjs) | Windows 本机 Host 到 Linux Server 的内置 adapter。 |
| [adapters/windows-process.ps1](../../tools/test-flow/adapters/windows-process.ps1) | Windows 进程启动和生命周期辅助，接受 adapter 的固定调用。 |
| [adapters/macos-linux-release.mjs](../../tools/test-flow/adapters/macos-linux-release.mjs) | macOS 本机 Host 到 Linux Server 的内置 adapter。 |
| [adapters/macos-linux-linux-release.mjs](../../tools/test-flow/adapters/macos-linux-linux-release.mjs) | macOS 上显式选择 Linux 容器 Client 的 adapter。 |
| [adapters/linux-linux-release.mjs](../../tools/test-flow/adapters/linux-linux-release.mjs) | 显式 Linux Client 到 Linux Server 的 adapter。 |
| [adapters/host-capability.mjs](../../tools/test-flow/adapters/host-capability.mjs) | 检查客户端 Host 的运行能力与身份。 |
| [adapters/server-linux-capability.mjs](../../tools/test-flow/adapters/server-linux-capability.mjs) | 检查 Linux Server 的安装、启动和运行边界。 |
| [adapters/fixtures/claude-flat-probe.mjs](../../tools/test-flow/adapters/fixtures/claude-flat-probe.mjs) | 测试用扁平参数探针；不是客户端生产兼容层。 |
| [runtime-support/](../../tools/test-flow/runtime-support) | 服务监督、隔离 Agent 环境、模型工具审计、测试 PostgreSQL、浏览器驱动、设置准备及 source snapshot 复核。由 Gate/adapter 选择调用，不作为公开测试入口。 |
| [runtime-support/postgres-sidecar.mjs](../../tools/test-flow/runtime-support/postgres-sidecar.mjs) | 管理发布测试的独立 PostgreSQL sidecar 与运行身份。 |
| [runtime-support/report-followup-tool-audit.mjs](../../tools/test-flow/runtime-support/report-followup-tool-audit.mjs) | 审计报告追问的工具使用，确保工具权限符合该场景边界。 |
| [runtime-support/isolated-agent-env.mjs](../../tools/test-flow/runtime-support/isolated-agent-env.mjs) | 定义隔离模型调用的环境策略、输出上限和允许项。 |
| [runtime-support/isolated-agent-wrapper.mjs](../../tools/test-flow/runtime-support/isolated-agent-wrapper.mjs) | 包装受约束模型调用，记录可供 Gate 审计的运行数据。 |
| [runtime-support/service-supervisor.sh](../../tools/test-flow/runtime-support/service-supervisor.sh) | 管理测试 Linux 服务的启动、存活和停止，与正式 CLI 保持一致的服务边界。 |
| [runtime-support/verify-source-snapshot.mjs](../../tools/test-flow/runtime-support/verify-source-snapshot.mjs) | 在执行环境中复核固定的源码清单，发现复制差异或执行期漂移。 |
| [schemas/failure-diagnostic.schema.json](../../tools/test-flow/schemas/failure-diagnostic.schema.json) | 约束失败诊断产物的字段，供编排器与消费者一致读取。 |
| [measurements/v10_components.py](../../tools/test-flow/measurements/v10_components.py) | 组件性能测量程序，提供观察数据；测量结果不能独立证明功能正确。 |

### 3.4 Fast E2E 与子收据

`quick-validation/standalone-suite.mjs` 负责独立短流程套件，`fast-e2e-scenarios.mjs` 定义场景。`codex-luna/` 与 `claude-deepseek/` 各有 `run.sh` / `run.mjs`、provider runtime、固定合同与自测；`wsl/` 用 Dockerfile、镜像准备、容器套件和 launcher 将短流程限制在密封 Ubuntu 22.04 环境。操作入口及适用平台见[快测说明](../../tools/test-flow/quick-validation/README.md)。

短流程不重新挂回中央 Goal/Proof/Stage/Gate，也不能把 standalone verdict 外推为 Release。真实模型调用前必须查看对应入口的 `--plan-only`，核对身份、调用数、预算、预计 token/cost 和 blocker；同一失败身份重试必须提供新的 reason、hypothesis 和 expected evidence。

| `tools/validation/` 文件 | 子收据职责 |
| --- | --- |
| [evidence-v2-core.mjs](../../tools/validation/evidence-v2-core.mjs) | 生成和复核绑定源码、合同、固定用例及 JUnit 的 Core 子收据。 |
| [evidence-v2-certification.mjs](../../tools/validation/evidence-v2-certification.mjs) | 复核 provider model-cert 和聚合认证收据，绑定同一 attempt 与输入身份。 |
| [evidence-v2-evaluation-mode.mjs](../../tools/validation/evidence-v2-evaluation-mode.mjs) | 定义单 Specialist 与显式盲评模式的允许值。 |
| [evidence-v2-scenario-oracle.mjs](../../tools/validation/evidence-v2-scenario-oracle.mjs) | 计算并核查场景预期及原件绑定，防止只相信收据中的 PASS。 |
| [core-verdict.schema.json](../../tools/validation/core-verdict.schema.json) | Core 子收据结构。 |
| [model-cert-input.schema.json](../../tools/validation/model-cert-input.schema.json) | provider 认证输入结构。 |
| [model-cert.schema.json](../../tools/validation/model-cert.schema.json) | provider 模型认证收据结构。 |
| [release-verdict.schema.json](../../tools/validation/release-verdict.schema.json) | 认证聚合收据结构；不替代外层 Test Flow verdict。 |
| [README.md](../../tools/validation/README.md) | 子收据概念与使用导航；字段和生成职责以当前实现为准。 |

[tools/methods-consensus-attribution-report.py](../../tools/methods-consensus-attribution-report.py) 读取 Methods 共识与归因材料生成分析报告，属于诊断辅助工具，不写产品诊断结果。

## 4. 测试目录与设计的对应关系

| 目录 / 辅助文件 | 验证重点 |
| --- | --- |
| [tests/deterministic/contracts/](../../tests/deterministic/contracts) | 合同 schema、canonical bytes、快照与 fixture manifest。 |
| [tests/deterministic/unit/](../../tests/deterministic/unit) | 按 application、domain、dispatch、runtime、storage、interfaces、agent、integrations 等职责验证局部行为和错误条件。 |
| [tests/deterministic/integration/](../../tests/deterministic/integration) | 生产组件组合后的状态、MCP/REST 旅程、网站会话、报告交付、路由准入和追问。 |
| [tests/deterministic/postgres/](../../tests/deterministic/postgres) | 真实 PostgreSQL 事务、并发、幂等、事件、历史、追问与导入，不用 SQLite PASS 代替。 |
| [tests/platform/](../../tests/platform) | Linux Server、各客户端平台与安装分发的能力和限制。 |
| [tests/real/](../../tests/real) | 明确选择的真实模型与真实外部工具验证，默认 Dev 不执行。 |
| [tests/fixtures/](../../tests/fixtures) | 正反例、虚拟 Agent、可复现日志和组件资产；fixture manifest 声明材料用途和摘要。 |
| [tests/cases/release/](../../tests/cases/release) | 发布场景的注册包、输入、日志、oracle 和 manifest；场景预期与模型可见材料保持区分。 |
| [tests/postgres_helpers.py](../../tests/postgres_helpers.py) | 为 PostgreSQL 专项测试建立隔离数据库与辅助对象。 |
| [tests/route_helpers.py](../../tests/route_helpers.py) | 构造路由候选、审核输入和预期，保持准入测试使用统一合同。 |
| [tests/v2_helpers.py](../../tests/v2_helpers.py) | Methods V2 测试对象和场景辅助；不代表生产默认启用 V2。 |
| [tools/test-flow/tests/](../../tools/test-flow/tests) | 编排器自身的计划、身份、证据、安全、资源、平台 adapter 与文档漂移测试。 |

运行验证时，先使用正式入口的 `--plan-only` 检查计划，再运行相同 Goal。没有最终 `verdict.json`、只存在半成品目录或独立摘要，都不能报告完整验证通过。新增框架说明可以单独检查文件覆盖与链接，但该检查不证明业务代码已通过 Test Flow。

## 5. Skill 作者、部署与客户端

| 文件 | 设计职责 |
| --- | --- |
| [.agents/skills/wiki-to-diagnosis-skill/SKILL.md](../../.agents/skills/wiki-to-diagnosis-skill/SKILL.md) | 将已评审 Wiki 转为 Methods V1 包，约束生成步骤与文件集合；不是运行时诊断入口。 |
| [.agents/.../references/output-contract.md](../../.agents/skills/wiki-to-diagnosis-skill/references/output-contract.md) | 规定 `SKILL.md`、`methods.json` 和独立方法卡的内容与相互引用。 |
| [.agents/.../scripts/validate_generated_skill.py](../../.agents/skills/wiki-to-diagnosis-skill/scripts/validate_generated_skill.py) | 校验生成包、输入声明、引用和方法索引，阻止不完整包进入部署。 |
| [.claude/skills/wiki-to-logparse-diagnosis-skill/SKILL.md](../../.claude/skills/wiki-to-logparse-diagnosis-skill/SKILL.md) | 生成可部署注册目录，外层含注册配置，`package/` 内含完整 Methods 包。 |
| [.claude/.../references/output-contract.md](../../.claude/skills/wiki-to-logparse-diagnosis-skill/references/output-contract.md) | 约束注册范围、用户输入、附件、Logparse 锚点与内置运行配置。 |
| [.claude/.../scripts/validate_generated_skill.py](../../.claude/skills/wiki-to-logparse-diagnosis-skill/scripts/validate_generated_skill.py) | 检查注册与包的一致性及生产输入要求。 |
| [.claude/skills/problem-locator-client/SKILL.md](../../.claude/skills/problem-locator-client/SKILL.md) | 指导 Host 用七个 MCP 工具建单、补参、上传、查询和下载；业务 Skill 在服务端执行。 |
| [.claude/.../references/client-mcp-config.json](../../.claude/skills/problem-locator-client/references/client-mcp-config.json) | HTTP MCP 客户端配置模板，不部署本地 Server 或代理。 |
| [.claude/.../agents/openai.yaml](../../.claude/skills/problem-locator-client/agents/openai.yaml) | 客户端 Skill 的展示与调用元数据。 |
| [.claude/skills/render-problem-locator-trace/SKILL.md](../../.claude/skills/render-problem-locator-trace/SKILL.md) | 指导调用旅程渲染命令，读取服务端已有证据。 |
| [.claude/.../agents/openai.yaml](../../.claude/skills/render-problem-locator-trace/agents/openai.yaml) | 旅程渲染 Skill 的展示元数据。 |
| [.claude/skills/adapt-lan-generic-locator-v2/SKILL.md](../../.claude/skills/adapt-lan-generic-locator-v2/SKILL.md) | 指导将局域网通用定位 Skill 适配为框架模式。 |
| [.claude/.../references/framework-mode.md](../../.claude/skills/adapt-lan-generic-locator-v2/references/framework-mode.md) | 说明通用定位的框架输入、输出与工具边界。 |
| [.claude/.../scripts/verify_generic_locator_v2.py](../../.claude/skills/adapt-lan-generic-locator-v2/scripts/verify_generic_locator_v2.py) | 检查适配产物是否满足 Generic V2 要求。 |
| [.claude/skills/logparse-diagnose/SKILL.md](../../.claude/skills/logparse-diagnose/SKILL.md) | Logparse 定位相关的独立 Skill 说明；是否使用由调用方选择，不是 bootstrap 隐式执行的脚本。 |

这里描述 Skill 文件在仓库中的角色，不表示编写框架文档时执行了这些 Skill 的生成或部署流程。

## 6. 网站接入示例逐文件设计

示例使用同源浏览器 SDK → 网站 BFF → xiaodao HTTP API。默认 BFF 透传 Cookie、Origin 和 Sec-Fetch-Site，由 xiaodao 校验；可信 owner 接入需要显式配置相应模式。示例不向浏览器暴露可任意指定归属用户的接口。

| 文件（均位于 `examples/website-agent/`） | 详细职责 |
| --- | --- |
| [README.md](../../examples/website-agent/README.md) | 接入顺序、运行方式、示例模块用法和报告字段说明。 |
| [server.mjs](../../examples/website-agent/server.mjs) | BFF 统一实现，转发认证上下文、会话和附件请求，验证上游响应、SSE 和下载；下载使用大小/摘要校验与临时文件机制。导出 `createAgentBackend` 供现有后端集成。 |
| [server.ts](../../examples/website-agent/server.ts) | Node.js 24+ 的类型声明与兼容启动入口，复用 `server.mjs`，不维护第二份业务逻辑。 |
| [browser-client.js](../../examples/website-agent/browser-client.js) | 封装同源 API 和 `AgentApiError`，统一参数、响应和错误；不自动重试写请求，调用方须保留 request ID。 |
| [conversation-input.js](../../examples/website-agent/conversation-input.js) | 共用输入框分流：诊断中携带 `target_run_id`，报告已完成则走追问；遇轮次变化先刷新指定轮次，避免响应丢失后误发第二次模型请求。 |
| [report-view.js](../../examples/website-agent/report-view.js) | 按固定 report_state/format 渲染结构化或 Markdown 报告；默认 Markdown 作为文本展示，避免任意 HTML 执行。 |
| [report-view.css](../../examples/website-agent/report-view.css) | 独立报告组件样式，可由接入网站替换。 |
| [followup-contract.js](../../examples/website-agent/followup-contract.js) | SDK 与 BFF 共用的追问 ID、文字、视图和回执验证，避免各端接受不同 DTO。 |
| [followup-bff.mjs](../../examples/website-agent/followup-bff.mjs) | 独立追问列表、提交、停止和事件流路由；复用当前请求凭据，核对会话/轮次/请求绑定并限制响应体积。 |
| [followup-controller.js](../../examples/website-agent/followup-controller.js) | 管理每份报告的草稿、请求 ID、快照和 SSE 游标，恢复连接与停止状态；切换报告时释放实例。 |
| [followup-view.js](../../examples/website-agent/followup-view.js) | 挂载连续问答界面并订阅 controller，保持追问与正式报告分开展示。 |
| [followup-preview.js](../../examples/website-agent/followup-preview.js) | 离线追问模拟，演示等待、完整回答与停止；定时回调一次性发布完整回答，不调用模型。 |
| [preview.mjs](../../examples/website-agent/preview.mjs) | 本地静态预览服务，提供页面与示例资产。 |
| [preview.html](../../examples/website-agent/preview.html) | 预览页结构、控件和模块挂载点。 |
| [preview.css](../../examples/website-agent/preview.css) | 预览页面布局与交互样式，与报告组件样式分离。 |
| [preview-model.js](../../examples/website-agent/preview-model.js) | 离线场景数据与模拟状态，供预览切换诊断阶段、历史和报告。 |
| [preview.js](../../examples/website-agent/preview.js) | 连接预览控件、模拟模型、报告视图与追问视图，处理本地页面交互。 |
| [server.test.mjs](../../examples/website-agent/server.test.mjs) | BFF 认证转发、上游响应、SSE 与下载边界测试。 |
| [browser-client.test.mjs](../../examples/website-agent/browser-client.test.mjs) | 浏览器 SDK 的参数、请求与错误行为测试。 |
| [report-view.test.mjs](../../examples/website-agent/report-view.test.mjs) | 报告格式、字段渲染和安全文本展示测试。 |
| [followup.test.mjs](../../examples/website-agent/followup.test.mjs) | 追问合同、BFF、controller、视图与分流竞态测试。 |
| [onboarding.test.mjs](../../examples/website-agent/onboarding.test.mjs) | 接入示例和使用约定的一致性检查。 |
| [preview.test.mjs](../../examples/website-agent/preview.test.mjs) | 离线预览页面与模拟交互测试。 |

示例模块为接入参考，不属于 Python wheel。运行离线预览只说明静态展示可用，不代表服务端认证、真实诊断或模型调用已通过验收。
