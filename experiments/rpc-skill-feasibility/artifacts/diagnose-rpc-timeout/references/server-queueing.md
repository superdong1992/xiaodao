# RPC server 收包线程排队

## 适用条件

判断第二种可能原因：RPC server 收包线程在此前执行其他耗时操作，目标 API 排队等待，响应
未能在截止时间前返回。使用目标 API 执行结束后打印的 `QUEUE_HISTORY` 记录。

## 所需证据

日志模板为：

```text
QUEUE_HISTORY print_time_ms={print_time_ms} ordinal={ordinal} service={service} api={api} end_us={end_us} cost_us={cost_us} queue_us={queue_us} timeout_ms={timeout_ms}
```

历史从旧到新用 `first|second|third|fourth|fifth`，每次 1–5 条。最后一条必须是当前目标 API，
并且目标服务名和 API 名一致。

## 计算与判断

把所有匹配记录中最早与最晚 `print_time_ms` 相差不超过 1000 毫秒的记录视为同一次历史输出，
中间可夹杂任意日志。条数为 N 时，最后一条对应第 N 个序号。

对目标记录同时检查：

```text
target_cost_us + target_queue_us > target_timeout_ms * 1000
target_cost_us < target_timeout_ms * 1000
```

成立则目标 API 自身执行没有超过超时时间，但排队加执行的总耗时超过超时，说明排队导致超时。
目标排队区间：

```text
target_execution_start_us = target_end_us - target_cost_us
target_queue_start_us = target_execution_start_us - target_queue_us
```

对目标之前每条 API 只用 `end_us` 和 `cost_us` 计算实际执行区间；其自身 `queue_us` 只表示它
在排队，不代表占用收包线程，不参与贡献判断：

```text
prior_execution_start_us = prior_end_us - prior_cost_us
overlap_us = min(prior_end_us, target_execution_start_us)
             - max(prior_execution_start_us, target_queue_start_us)
```

所有 `overlap_us > 0` 的前序 API 都是排队贡献者，不只选最近、最长或最可疑的一条。只有目标
一条记录时，可以判断目标是否因排队超时，但无法确认具体哪个前序 API 贡献排队。

## 确认条件

目标记录服务名和 API 名匹配，且上述两个目标条件同时成立，即确认服务端收包线程排队导致目标
超时。将所有重叠前序 API 列为贡献者；该原因可与其他方法同时确认。

## 未知边界

排队历史受同进程 180 秒一次的限流，并叠加 BBBB 默认抑制。历史不完整或缺失时，不能反推没有
排队，也不能把未出现的前序 API 当作不存在。单条目标记录只能确认排队超时，不能确认具体贡献者。

## 输出含义

输出历史分组依据、目标计算、全部贡献者及其重叠区间；若记录不足，明确哪些判断未知。RPC
超时本身不取消服务端执行，后续执行仍可能产生副作用。
