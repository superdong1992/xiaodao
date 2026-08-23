---
name: diagnose-rpc-timeout
description: Use frozen RPC timeout target logs and receipt to diagnose API execution, server receive-thread queueing, and client receive-thread blocking according to the authored CCCC/BBBB Wiki.
---

# RPC 超时定位

输入是已经冻结的 `target_logs` 和 receipt。先读取 `methods.json`，只读取
`target_logs[*].log_path` 明确列出的日志；不得调用 Logparse、遍历解析目录或重新选择
生命周期、进程和日志路径。

先扫描全部目标日志中的全部正向 evidence markers，再加载所有命中的方法卡和共享引用；不能
在第一个命中处分支短路。按方法卡的精确字段、单位换算、时间分组、目标选择和贡献者规则
计算，并允许多个原因同时成立。

没有足够正向证据时输出“证据不足”，同时说明 Wiki 所述的日志抑制、限流或条件打印可能造成
观测缺失，不能把缺失日志当作原因不存在。结果必须保留 RPC 超时不等于取消、服务端可能仍
执行并产生副作用的安全提醒，以及输入目标日志作用域边界。
