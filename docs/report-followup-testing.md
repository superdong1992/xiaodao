# 报告追问验证范围

正式验证仍使用中央 tools/test-flow/run.ps1 或 run.sh 的 dev.default，先审阅计划，再由编排器执行 affected 与完整确定性轨。新追问测试不能替代创建、补参、上传、通用/专用定位、审核、报告、下载、反馈、停止、删除、历史及诊断 SSE 用例；不调整完整测试的 300 秒门槛。

## 每次追问直接进入通用诊断（2026-11-05）

本轮从 ab031f7 复核生产问题：用户转向新的问题时，旧追问模型仍可能依据前一份报告返回 ANSWER，没有启动通用 Skill。新行为取消这个前置判断，每次合法追问都交接独立 Generic Case 和 run。专项应验证真实代码链路，不能只断言提示词包含“通用诊断”。

| 范围 | 直接覆盖 |
| --- | --- |
| 无前置模型交接 | tests/deterministic/unit/agent/test_followup_generic_dispatch.py：新方向和解释型问题均交接，旧问答 backend 不执行，当前文字原样进入 Case，停止前不建案，重复请求只返回原收据 |
| 强制 Generic | tests/deterministic/unit/application/test_forced_generic_creation.py：内部 force_generic 绕过 ROUTE，使用正式 Generic Job 和现有 Skill；普通新建问题仍保留原路由规则，公开七个 MCP 输入保持扁平 |
| 正式报告旅程 | tests/deterministic/integration/test_followup_generic_round.py：HTTP 追问、原子关联、真实 Case/Job 链、通用 Skill 输入、完整 V2 报告与版本；旧报告不改写，当前主题不被旧报告替换 |
| 输入与材料 | 当前问题与只读 inputs/followup-context.json 分开；历史更正、报告和最新未发布尝试保留来源；候选可用日志进入正式准备，数量/总大小/哈希与归属校验继续有效；不能把自然语言排除要求误测成框架已经物理过滤文件 |
| 忙碌与最新版本 | 交接后即使 active_followup 为 null，也须返回 BUSY；旧版返回 LATEST_REPORT_REQUIRED，POST 保留对应冲突；原请求重放优先于新任务资格检查，不能重复建案 |
| 页面与兼容 | examples/website-agent/unified-followup.test.mjs、followup.test.mjs、continuation.test.mjs：每次通用诊断提示、交接不等于报告完成、新项不以旧 context_mode 宣称证据、旧答案可读、终态优先、停止、READY 后切换、分页和重连不重建、旧草稿兼容 |
| 反馈与恢复 | 交接说明仍不可评价或提炼，新报告及历史普通答案各自评价；删除、停止、提交回滚、服务重启和迟到结果不能重复创建或修改旧报告 |

资料不足也执行通用分析，以 UNRESOLVED 说明证据缺口与建议补充；不能把它恢复成诊断前的 REQUEST_INPUT 分类。提示词所表达的当前问题优先、旧结论仅供参考和日志排除要求仍需代表性真实案例验证。确定性替身不能证明模型在生产中一定选择正确证据，也不能证明诊断更快或更便宜。

历史 WAITING_INPUT 页面和接口继续覆盖：同一输入区展示后端 current_questions，固定 target_run_id=diagnosis_run_id 提交补充；遇到 AGENT_RUN_CHANGED 只刷新原目标，不自动改发追问。新通用流程不以缺少用户资料作为前置分类阻断。

## 真实模型验收边界

本轮日常专项不调用真实模型。新流程使用正式 Generic 的执行预算，不使用 REPORT_FOLLOWUP_TIMEOUT_SECONDS 控制正式诊断。原 real.report-followup profile 的两次只读模型续接、read-search 工具清单和 300 秒单次预算描述的是旧流程，不能用该旧轨的 PASS 宣称新“每问通用诊断”已验收，也不能为迁就新实现放宽旧断言。

需要真实验收时，先把计划与当前 Generic 调用链对齐，再审阅仓库入口的 --plan-only，核对身份、工具权限、调用数、回合、token/cost、预算与阻塞项。代表案例至少包括：明确换主题、同主题更正、补日志、资料不足、连续两次追问，以及新版报告发布后的下一轮。正式 Test Flow 的结论以对应源码快照的 verdict.json 为准，不能把离线预览、专项通过或旧原生会话测试外推为 Release 或生产验收。

## 历史原生会话验证记录（2026-10-29）

以下是上一版只读原生追问的既有记录，不表示本轮重新执行，也不描述新版处理路径。旧源码基线为 1153b31，Linux 四文件基线 111 passed；Windows 基线受文件元数据与 PostgreSQL 环境影响而中止。原始产物和失败记录保留。

| 专项范围 | 结果 |
| --- | --- |
| 追问单元测试 | 384 passed |
| 追问集成测试 | 5 passed |
| runtime 与 tools Python 专项 | 分别 112 passed、67 passed |
| Node 追问及受控启动器 | 67 passed |
| Node 普通启动器回归 | 7 passed |
| settings、bootstrap 与发布边界 | 40 passed |
| 到期后的材料交接（三个 Python 测试文件） | 60 passed，`.tmp/native-only-materials-20261029-01.xml` |
| 资源保留与到期后继续追问 | 两个 Python 文件 22 passed、对应 PostgreSQL 用例 1 passed；保留原正文、幂等和资源释放断言，明确新问题为 `REPORT_ONLY` |
| 报告页过期提示、追问与统一入口（三个 Node 测试文件） | 66 passed，`.tmp/native-only-report-notice-green-20261029.tap`；过期提示回归修改前先复现 1 failed |


这些范围可能重叠，不累计。历史原生会话的 --resume、200k 窗口及内部 codeagent / Glm-5.2-Auto 的真实效果当时未验收；新版不再依赖旧只读模型续接，不能以本轮变更补记这些历史能力为已验证。具体修复与各轮权威 verdict 见 [PL-FIX-083 修复台账](../FIXED_ISSUES.md#pl-fix-083继续诊断入口把纯报告追问整理成可确认的新诊断草稿)。
