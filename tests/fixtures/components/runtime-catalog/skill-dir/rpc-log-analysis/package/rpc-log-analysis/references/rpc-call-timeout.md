# RPC call timeout

## 适用条件
The client emits `rpc deadline exceeded` and the correlated server log emits
`connection pool wait` for the same request identity.

## 所需证据
Use both frozen client and server target logs.

## 计算与判断
Correlate the request identity and declared problem time.

## 确认条件
The positive timeout marker matches the selected request.

## 未知边界
Missing or suppressed logs do not exclude the cause.

## 输出含义
Return each call separately with its complete raw source and identity.
