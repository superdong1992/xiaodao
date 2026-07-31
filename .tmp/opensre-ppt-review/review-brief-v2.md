# OpenSRE 四页 PPT 审查简报

## 审查对象

- 事实基线：`Tracer-Cloud/opensre` 代码快照 `4afe9572a45b41a92c65d9944a592c412760446a`
- PPT 源文件：`D:\code\xiaodao\doc\problem-locator-open-source-insight-ppt\build-deck.mjs`
- 生成文件：`D:\code\xiaodao\doc\problem-locator-open-source-insight-ppt\index.html`
- 1920×1080 截图：
  - `D:\code\xiaodao\.tmp\opensre-ppt-review\slide-05-1920x1080.png`
  - `D:\code\xiaodao\.tmp\opensre-ppt-review\slide-06-1920x1080.png`
  - `D:\code\xiaodao\.tmp\opensre-ppt-review\slide-07-1920x1080.png`
  - `D:\code\xiaodao\.tmp\opensre-ppt-review\slide-08-1920x1080.png`

## 四页叙事

1. OpenSRE 是什么：开源的线上故障调查 Agent 框架。
2. 外层六步：固定 Python 流水线；第 3 步定工具边界，第 4 步进入 Agent。
3. Agent 实现逻辑：准备输入、可选种子查询、模型决策、运行时执行、证据记录、继续或结束；另解释上下文裁剪和当前调查内查询缓存。
4. RPC 超时教学示例：Datadog 种子查询 + Grafana Tempo 分诊 + 日志、配置和指标交叉验证；最后回到 diagnose / deliver。

## 关键事实边界

- 当前主调查链为单 Agent；同轮多个工具并行不等于多 Agent。
- `plan_actions` 用确定性规则生成 `planned_actions` 工具名短名单，同时记录 rationale、audit 和 `retrieval_controls`；后者尚未被 Agent / 工具执行链强制注入真实调用参数。
- 部分告警来源在第一次模型调用前执行代码预设的种子查询。
- 四个调查阶段主要由提示词引导，不是代码状态机；首轮新工具结果后有一次运行时 checkpoint。
- 上下文控制是删除 / 截断，不是模型摘要。
- 缓存仅限当前调查，采用 LRU；键为工具名加模型或种子查询提供的输入参数（按键排序规范化），运行时随后注入的受保护连接字段不进入缓存键。缓存最多 128 项、约 200 万字符；8,000 字符只限制重复结果向模型回放的长度。
- `diagnose` 通常调用推理模型，以结构化输出从最终助手文本提取 RCA 字段；失败时回退到旧解析器。它不重新调查，也不是证据充分性独立审核 Agent。
- RPC 数据与根因为教学构造；示例额外假设接入 Grafana Tempo、配置变更和数据库观测工具。

## 审查输出要求

每名审查者最终必须只给出以下两类结论之一：

- `无修改意见`
- 按严重程度排序的具体问题，注明页码、问题和可执行修改建议

任一路有修改意见，都将修订后重新启动三名全新审查 Agent；只有同一轮三路均明确“无修改意见”才结束。

## 最终收敛结果

- 第九轮源码事实审查：`无修改意见`
- 第九轮中文表达审查：`无修改意见`
- 第九轮视觉与投屏审查：`无修改意见`
- 华为版式静态校验：49 页通过
- 交互检查：49 个导航点；方向键、滚轮、B 静态模式、ESC 索引均通过
- 1920×1080 渲染检查：第 5—8 页无可见裁切、遮挡或导航安全区冲突
