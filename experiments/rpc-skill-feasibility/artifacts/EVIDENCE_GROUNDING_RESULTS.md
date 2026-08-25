# 通用证据溯源修复结果

结论：原先“无请求标识的证据丢失执行区间”问题已在第 3 轮实际输出中消失，但该轮把
`logparse_receipt_sha256` 抄错，整体仍按三轮上限判定失败。该结果不是 Test Flow 或 Release verdict。

## 实验身份

- 分支：`codex/rpc-skill-feasibility`
- Wiki SHA-256：`eb39edf220d0eed91ae03eb712efd8974a5e5c82c3deed035c236a0d1bf28aab`
- 最后生成 Skill SHA-256：`bf37c11c575c44da14e5ae4f5a6a1770b2cac1273f4fe4eae45e1489f95a229a`
- 诊断结果 schema：v2
- 模型：`gpt-5.6-luna`，reasoning effort `medium`
- Codex CLI：`codex-cli 0.149.0-alpha.4.1`

## 实现结果

- 元 Skill 要求每个原因、每次独立事件分别输出证据，并通过 `sources` 原样引用冻结日志、通过
  `identity_tokens` 保留来源中的命名字段和值。
- 元 Skill 未出现本次 RPC 日志标记及身份字段；运行器的 generalization canary 对这些字面量保持禁止。
- schema v2 和运行器会拒绝不存在的原始日志行、不在同一行的日志标记、来源中不存在的身份 token、
  重复事件身份以及缺少正向证据的确认方法。
- 一个非 RPC 的双任务事件自检通过，证明新证据校验不依赖 RPC 字段。
- 每个用例的收据哈希已改为在诊断工作区 schema 中绑定为 `const`，不再依赖模型手工复制；该修正发生在
  三轮结束后，只完成了确定性自检，没有进行第 4 次 Luna 验证。

## 三轮结果

1. 第 1 轮生成包正确保留三类原因和完整来源日志，但诊断把身份 token 写成 `501`、`10000000` 等裸值，
   没有保留完整字段绑定，判定 `evidence_identity` 失败。
2. 第 2 轮保留了完整字段绑定，但把同一原因的一个检测分支另拆成第四个方法，判定
   `wiki_fidelity` 失败；该轮没有进入诊断。
3. 第 3 轮重新收敛为三个方法。诊断同时确认 API 执行过长和客户端收包线程阻塞，并分别保留：
   - `request_id=501`
   - `start_us=10000000`、`end_us=16500000`
   - 两条证据使用的完整冻结日志原文

   两次调用被明确分开，没有合并。该结果唯一的合同失败是收据哈希写成
   `a5ebbef5cc1fd0aa44655739bbde1a9eb9376059c44fdc6e14a6795b215c47e1`，而工作区实际 SHA-256 为
   `a5ebbef5cc1fd0aa44655739bbdeba1f9eb9376059c44fdc6e14a6795b215c47`。

第 1 轮诊断前还发生过一次 Structured Outputs 预检拒绝：schema 使用了不受支持的 `uniqueItems`。
该请求未进入模型推理，不计入三轮语义额度；去掉该关键字后，数组去重继续由确定性运行器检查。

## 验证边界

将第 3 轮原始结果仅在内存中替换为实际收据哈希后，运行器对两个确认方法、事件身份、原始日志、计算词、
无关证据和安全说明的其余断言全部通过。这个动作只用于隔离最后失败，不修改已保存的模型输出，也不作为
PASS 结论。

因达到三轮上限，没有执行其他八个用例，也没有进行九例全量回归。最后生成 Skill 不作为最终通过快照提交；
完整生成字节、Codex JSONL 和失败轮次继续保存在 `.tmp/rpc-skill-feasibility/evidence-grounding/`。

## Logparse

九个用例全部复用既有冻结日志。每份 receipt 仍记录 `parse_invocations=1`、
`target_query_invocations=2`，诊断阶段 Logparse 调用为零。
