# RPC 超时定位 Skill 可行性结果

> 本文件记录人工 Wiki 修改前的四例可行性基线。后续九例加固见 [HARDENING_RESULTS.md](HARDENING_RESULTS.md)，通用证据溯源修复见 [EVIDENCE_GROUNDING_RESULTS.md](EVIDENCE_GROUNDING_RESULTS.md)；两者均未形成九例 PASS。

结论：功能可行性验证通过。同一个由 Wiki 生成的定位 Skill 在不知道预期答案的情况下，正确处理了 API 执行过长、服务端排队、客户端收包阻塞和证据不足四种场景。

这不是 Test Flow 或 Release verdict，也没有登记为产品修复。

## 实验身份

- 分支：`codex/rpc-skill-feasibility`
- 人工 Wiki SHA-256：`9138ee2358137fdc1dcc828f08a0f89ebc5e55816f71a1142ccd5d9b8ddc8161`
- 生成 Skill SHA-256：`c1618c664f4518c83d967204aa1a37ad5ecf9468c9588d7d6fd20ebd625cf431`
- 模型：`gpt-5.6-luna`，reasoning effort `medium`
- Codex CLI：`codex-cli 0.149.0-alpha.4.1`
- Logparse：`a233b500d9c99e6815d1ffd82cb4ca55bbfe657a`

GPT-5.6 Luna 和 `codex exec --json/--output-schema` 的选择依据为 [GPT-5.6 Luna 模型说明](https://developers.openai.com/api/docs/models/gpt-5.6-luna)、[Codex 非交互模式](https://developers.openai.com/codex/non-interactive-mode)和 [Structured Outputs 支持范围](https://developers.openai.com/api/docs/guides/structured-outputs)。

## 迭代结果

第 1 轮成功生成三张方法卡并通过结构校验，但漏掉两种共同的客户端超时日志模板。元 Skill 随后区分了“原因路由标记”和“共同症状/请求关联模板”：前者进入方法索引，后者进入共享引用。

第 2 轮保留了全部六类 Wiki 日志标记，仍只生成三个独立原因方法，并通过四个场景。期间修正的 `.agents` 写入位置、本机 CLI 参数和 Structured Outputs schema 都属于实验运行器适配，没有修改 Wiki、场景答案或方法语义。

成功第 2 轮记录的模型使用量为：生成阶段 input 469,688、cached input 434,688、output 11,149；四次诊断合计 input 343,976、cached input 260,864、output 7,392。失败或中止调用只保留在 `.tmp` JSONL 中，因此这里不把这些数字宣称为整个探索的完整账单。

## 场景结果

| 场景 | 结果 | 确认方法 |
|---|---|---|
| API 执行过长 | `CONFIRMED` | `api-execution-slow` |
| 客户端收包阻塞 | `CONFIRMED` | `client-receive-blocked` |
| 证据不足 | `INSUFFICIENT` | 无；三个原因保持候选 |
| 服务端排队 | `CONFIRMED` | `server-queueing` |

服务端排队场景同时识别出 `svc_catalog:Refresh` 和 `svc_auth:Sync` 两个贡献者，计算重叠分别为 2,000,000 和 1,500,000 微秒。证据不足场景没有确认根因，并明确保留了日志抑制、限流和条件打印造成的未知边界。四个结果都保留“RPC 超时不等于取消”的安全说明。

## Logparse 复用

每个用例只执行过一次 `parse`，随后在同一预处理阶段各执行 client/server 两次 target query。第 2 轮所有用例均为缓存命中，四次诊断的命令轨迹中 Logparse 调用数为零。

最终生成的完整定位 Skill 见 [diagnose-rpc-timeout](diagnose-rpc-timeout/)，运行摘要见 [results.json](results.json)，逐场景完整结果见 [diagnoses](diagnoses/)。原始 Codex JSONL、失败尝试和 Logparse 解析树仍只保存在被 Git 忽略的 `.tmp/rpc-skill-feasibility/`。

## 边界

- 当前只验证四个合成场景；`DEADLOOP_DETECTED` 规则已进入生成 Skill，但没有单独运行真实诊断场景。
- 结果证明轻量链路的功能可行性，不证明真实现场覆盖率、发布稳定性或旧版兼容性。
- 没有比较或挑选多个候选 Skill；四个场景始终使用同一个第 2 轮生成物。
