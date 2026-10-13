-- 每次补录最多 1000 个缺失的终态 run。请在独立事务内设置合适的执行超时。
-- 已确认 agent_messages 只有用户消息，包含 conversation_id、run_id；COUNT(*) 即用户消息数。
-- 待核对前提：run 结束后不再新增输入，其消息未被单独清理。
-- 不恢复已经清理的历史。字段参考本地代码，生产必须先核对 README.txt。
-- ONLINE 标记供 writer.py 替换为固定筛选条件，本文件也可独立执行。
WITH candidates AS MATERIALIZED (
    SELECT r.conversation_id, r.run_id, r.ordinal, r.status,
           NULLIF(c.owner_key, '') AS owner_key,
           CAST(r.body AS JSONB) AS run_body,
           CAST(c.body AS JSONB) AS conversation_body
    FROM public.agent_conversation_runs r
    JOIN public.agent_conversations c ON c.conversation_id = r.conversation_id
    WHERE r.status IN ('COMPLETED', 'FAILED', 'INTERRUPTED', 'CANCELLED')
      -- ONLINE_FILTER
      AND NOT EXISTS (
          SELECT 1 FROM public.agent_run_usage_stats s
          WHERE s.conversation_id = r.conversation_id AND s.run_id = r.run_id
      )
    ORDER BY r.conversation_id, r.run_id
    LIMIT 1000 -- ONLINE_LIMIT
), inserted AS (
    INSERT INTO public.agent_run_usage_stats (
        conversation_id, run_id, owner_key, created_at,
        run_created_at, completed_at, terminal_status, message_count
    )
    SELECT c.conversation_id, c.run_id, c.owner_key,
           CAST(COALESCE(
               NULLIF(c.run_body ->> 'conversation_created_at', ''),
               NULLIF(c.conversation_body ->> 'conversation_created_at', ''),
               CASE WHEN c.ordinal = 1 THEN NULLIF(c.run_body ->> 'created_at', '') END
           ) AS TIMESTAMPTZ),
           CAST(NULLIF(c.run_body ->> 'created_at', '') AS TIMESTAMPTZ),
           CAST(NULLIF(c.run_body ->> 'completed_at', '') AS TIMESTAMPTZ),
           c.status,
           (SELECT COUNT(*) FROM public.agent_messages m
            WHERE m.conversation_id = c.conversation_id AND m.run_id = c.run_id)
    FROM candidates c
    ON CONFLICT (conversation_id, run_id) DO NOTHING
    RETURNING conversation_id, run_id
)
SELECT COUNT(*) AS inserted_count FROM inserted;
