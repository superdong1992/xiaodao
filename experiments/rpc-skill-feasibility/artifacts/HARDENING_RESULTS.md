# RPC 超时定位 Skill 加固结果

> 针对本文件最后一个未闭合点的后续通用修复见 [EVIDENCE_GROUNDING_RESULTS.md](EVIDENCE_GROUNDING_RESULTS.md)。

结论：三轮上限内未完成九例加固，按实验停止条件结束。该结果不是 Test Flow 或 Release verdict。

## 实验身份

- 分支：`codex/rpc-skill-feasibility`
- Wiki SHA-256：`eb39edf220d0eed91ae03eb712efd8974a5e5c82c3deed035c236a0d1bf28aab`
- 最后生成 Skill SHA-256：`d35cbb613e179cffcfdc8d034fae69f9db7715ff9a782c987bba9c17ec94ba15`
- 模型：`gpt-5.6-luna`，reasoning effort `medium`
- Codex CLI：`codex-cli 0.149.0-alpha.4.1`

## 三轮结果

1. 第 1 轮保留了 Wiki 的输入含义，但为五个用户参数生成了不稳定别名；同时没有把可独立确认 API 执行过长的 `DEADLOOP_DETECTED` 放入方法索引。
2. 第 2 轮修正了输入 ID 和死循环索引，但遗漏两条公共客户端超时模板。增强后的确定性校验器在该轮产物上只报告这两条遗漏。
3. 第 3 轮生成包通过输入合同、三个方法、Wiki 哈希和六条日志模板校验。`deadloop-detected` 正确确认 API 执行过长；`multiple-rpc-timeouts` 同时确认 API 执行过长和客户端收包线程阻塞，并明确两次调用不能合并，但 `API_COMPLETE` 的证据摘要没有保留 `start_us/end_us` 执行时间段，未达到无 request ID 调用的区分要求。

第 3 轮第一次诊断校验曾把“超时不等于调用已取消”误判为缺少取消边界；这是确定性中文同义判断缺陷。修正后复用了原始结果，没有再次调用模型诊断同一输入。

## 已执行范围

- `deadloop-detected`：诊断语义通过。
- `multiple-rpc-timeouts`：两个原因均确认，但调用区分证据不完整，判定失败。
- `server-queue-five`、`server-queue-single`、`unrelated-log-noise`：因第 3 轮停止条件未执行诊断。
- 原四例：本阶段未进行 Luna 回归，不能沿用上一阶段结果声明新版 Wiki 与生成 Skill通过。

两个已执行结果见 `hardening-diagnoses/`。完整 Codex JSONL、生成包字节、失败记录和 Logparse 解析树只保存在 `.tmp/rpc-skill-feasibility/hardening/`。

## Logparse

九个用例的 receipt 均记录 `parse_invocations=1`、`target_query_invocations=2`。原四例复用上一阶段冻结日志，新五例只在首次预处理时解析一次，之后全部命中缓存。诊断阶段未调用 Logparse。

## 未闭合点

下一次继续前需要让定位 Skill在无 request ID 的正向证据摘要中保留足以区分调用的日志时间字段，例如 `API_COMPLETE` 的 `start_us/end_us`。本阶段不继续增加第 4 轮，不手改生成 Skill，也不修改 Wiki 或用例答案。
