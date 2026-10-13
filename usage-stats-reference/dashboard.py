"""asyncpg 查询。直接传 datetime.date；日期范围为 [start_day, end_day)。"""

from datetime import date


# 先过滤日期再按会话汇总，count(*) 计算会话数，不是 run 数。
# owner_key 与原始创建时间在同一会话内必须一致。
PER_CONVERSATION = """
WITH per_conversation AS (
    SELECT conversation_id,
           MIN(owner_key) AS owner_key,
           MIN(created_day) AS created_day,
           SUM(message_count) AS message_count,
           (COUNT(DISTINCT owner_key) > 1
            OR COUNT(owner_key) NOT IN (0, COUNT(*))
            OR MIN(created_at) <> MAX(created_at)) AS metadata_conflict
    FROM public.agent_run_usage_stats
    WHERE created_day >= $1 AND created_day < $2
    GROUP BY conversation_id
)
"""

SUMMARY_SQL = PER_CONVERSATION + """
SELECT COUNT(*) AS conversation_count,
       COUNT(DISTINCT owner_key) AS user_count,
       COALESCE(SUM(message_count), 0) AS message_count,
       COUNT(*) FILTER (WHERE owner_key IS NULL) AS unknown_owner_conversation_count,
       COUNT(*) FILTER (WHERE metadata_conflict) AS metadata_conflict_count
FROM per_conversation
"""

DAILY_SQL = PER_CONVERSATION + """
SELECT created_day AS day,
       COUNT(*) AS conversation_count,
       COUNT(DISTINCT owner_key) AS user_count,
       COALESCE(SUM(message_count), 0) AS message_count,
       COUNT(*) FILTER (WHERE owner_key IS NULL) AS unknown_owner_conversation_count
FROM per_conversation
GROUP BY created_day
ORDER BY created_day
"""


async def fetch_dashboard(pool, start_day: date, end_day: date) -> dict:
    if type(start_day) is not date or type(end_day) is not date:
        raise TypeError("日期参数必须是 datetime.date 对象。")
    if start_day >= end_day:
        raise ValueError("结束日期必须晚于开始日期，结束当天不计入范围。")
    async with pool.acquire(timeout=2) as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            summary = dict(await conn.fetchrow(
                SUMMARY_SQL, start_day, end_day, timeout=10,
            ))
            if summary["metadata_conflict_count"]:
                raise ValueError("同一会话的用户或创建时间不一致，请先核对统计记录。")
            rows = await conn.fetch(DAILY_SQL, start_day, end_day, timeout=10)
    # SUM(bigint) 返回 numeric，转为 Python int，便于输出 JSON。
    summary["message_count"] = int(summary["message_count"])
    daily = []
    for record in rows:
        item = dict(record)
        item["day"] = item["day"].isoformat()
        item["message_count"] = int(item["message_count"])
        daily.append(item)
    return {"summary": summary, "daily": daily}


# 示例：await fetch_dashboard(pool, date(2026, 10, 1), date(2026, 11, 1))
# 整段用户数单独去重，不能把每日用户数相加。
