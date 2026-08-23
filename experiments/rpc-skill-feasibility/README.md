# RPC 超时定位 Skill 可行性实验

这个目录验证一条最短链路：人工 Wiki 经实验版元 Skill 生成一个定位 Skill，再由同一个定位 Skill 诊断九份冻结日志。它不使用项目 Test Flow，也不产生 Release verdict。

当前加固阶段和后续证据溯源修复均在各自三轮上限内停止，不能宣称九例 PASS。原始加固失败见
`artifacts/HARDENING_RESULTS.md`，证据溯源修复结果见 `artifacts/EVIDENCE_GROUNDING_RESULTS.md`；
`artifacts/RESULTS.md` 保留此前四例可行性基线。

运行前提：

- 从仓库根目录执行；
- 本机 `codex` 已登录并可使用 `gpt-5.6-luna`；
- 提供 Logparse 仓库，且其中已有可运行的 `.venv/bin/python`。

执行全部预处理、生成和诊断：

```bash
python3 experiments/rpc-skill-feasibility/run.py \
  --round 1 \
  --logparse-root /path/to/logparse
```

只验证实验源文件并完成每个用例的一次 Logparse 预处理：

```bash
python3 experiments/rpc-skill-feasibility/run.py \
  --prepare-only \
  --logparse-root /path/to/logparse
```

单独验证领域无关的证据原文、身份 token 和多事件合同：

```bash
python3 -B experiments/rpc-skill-feasibility/check_evidence_contract.py
```

运行产物保存在被 Git 忽略的 `.tmp/rpc-skill-feasibility/`。同一个用例再次运行时，如果原始日志、问题时间、两端进程以及 Logparse 配置和版本没有变化，运行器直接复用冻结日志。如果这些内容变化或上次预处理未完整结束，运行器停止并说明原因；新输入必须使用新的用例记录，不能覆盖旧收据后静默重跑。
