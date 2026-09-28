# 专用 Skill 的路由准入

ROUTE 只调用一次模型，逐项审核各 Skill 声明的适用条件和排除条件。服务端核对审核是否完整、引文是否存在于本次冻结输入，再决定是否进入专用定位。只有一个候选明确适用、其他候选均明确不适用，且模型置信度不低于 0.95 时，才进入专用流程。置信度是附加门槛，不代表经过统计校准的正确率；引用真实原文也不等于模型的语义判断必然正确。

低置信、条件未知、引用缺失或失实、多个候选同时适用，均按 `NO_CAPABILITY` 转入现有通用定位。只有一个候选也必须审核。已确认适用但缺少日志、时间或进程参数时，仍进入专用流程，再补齐材料。不得把“诊断材料未齐”写成排除条件。

非法 JSON、未知 Skill ID、遗漏候选或条件、字段类型错误仍报 `OUTCOME_INVALID`。不把协议故障当作正常回退，也不自动重试模型。取消、超时和服务故障沿用原处理方式。

## 注册与升级

新注册使用 `registration-template.json` 的 `schema_version: 2`，新增必填 `routing`：

```json
"routing": {
  "applicability": [
    {"id": "rpc-timeout", "description": "问题是客户端 RPC 调用超时，需要定位超时原因。"}
  ],
  "exclusions": []
}
```

适用条件为 1～16 条，排除条件为 0～16 条。每条仅含 `id` 和 `description`；ID 为不超过 64 字符的小写 kebab-case，在同一 routing 中唯一；说明为不超过 1024 字符的非空文本。条件应来自原始 Wiki，明确领域、场景和症状。没有已声明的额外排除条件时写空数组，不补写经验规则。

旧 `schema_version: 1` 注册仍可加载和读取历史记录，索引中的 `routing` 为 null，不能自动选入专用流程。所有生产候选仍在索引中，不按初始事实名预过滤。全部候选都缺少范围声明时，服务端直接转通用，零模型调用。新声明纳入注册内容哈希；更新后按既有部署流程重启服务，已冻结的 Job 不使用运行中被替换的声明。

完整注册生成器和发布模板已更新；只生成 Methods 包的元 Skill 仍只生成包。MCP 的七个公开输入、Case REST 输入和持久化 `RouteDecision` 格式不变。

## 审核与排查

内部模型响应固定为 `skill_id`、`reason`、`confidence`、`assessments`。每个可审核候选的所有条件都需返回 `SUPPORTED`、`REFUTED` 或 `UNKNOWN`，附理由和原文引用。JSON Pointer 只能指向冻结问题描述中的字符串或字符串列表项，以及活跃用户事实、已确认事实的 `statement`；假设和历史路由结论不能作为依据。具体格式见[ROUTE 输出合同](../src/problem_locator/runtime/assets/output-contracts/route/output-contract.md)。

每次有模型响应的 ROUTE 保存 `route-response.raw.txt`；合法审核另存 `route-admission.json`，包含原选择、实际选择、回退原因、逐项审核及上下文、目录和草稿哈希。事件 `runtime.route.admission` 仅记录关联标识、选择和原因代码。审计写入失败时任务中断，不能无记录放行。新审核协议不再修复根 `reason` 的未转义引号，审核理由和证据引文也不修补；旧恢复记录保留用于历史排查。

准入规则始终生效，不受 `METHODS_EVIDENCE_VALIDATION` 或 Reviewer 开关影响。确定性测试验证门槛与状态转换；真实模型语义表现须在 `dev.real` 的 `real.route` 阶段单独验证，运行前先审阅 `--plan-only` 中的身份、五个路由场景及模型预算。
