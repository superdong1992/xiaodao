# RPC 超时定位 Skill 可行性实验

这个目录只验证一条最短链路：人工 Wiki 经实验版元 Skill 生成一个定位 Skill，再由同一个定位 Skill 诊断四份冻结日志。它不使用项目 Test Flow，也不产生 Release verdict。

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

运行产物保存在被 Git 忽略的 `.tmp/rpc-skill-feasibility/`。同一用例的输入或 Logparse 身份变化后，运行器会拒绝复用旧 receipt，也不会静默重新解析；需要人工移走对应缓存目录后再开始新的实验身份。
