-- 每批修复最多 1000 个缺失或计数偏低的终态 run；已对齐项不占批次。
-- 普通输入和报告追问分别来自两张表，每条已入库用户输入计 1，不按状态过滤。
-- 源输入必须完整；清理前应封口并确认最新统计，不能从残缺源数据恢复历史。
-- 请在独立事务中设置超时。字段参考本地代码，生产必须先核对 README.txt。
-- ONLINE 标记供 writer.py 替换为固定筛选条件，本文件也可独立执行。
WITH observed AS MATERIALIZED (
    SELECT r.conversation_id, r.run_id, r.ordinal, r.status,
           NULLIF(c.owner_key, '') AS owner_key,
           CAST(r.body AS JSONB) AS run_body,
           CAST(c.body AS JSONB) AS conversation_body,
           (SELECT COUNT(*) FROM public.agent_messages m
            WHERE m.conversation_id = r.conversation_id AND m.run_id = r.run_id)
           + (SELECT COUNT(*) FROM public.agent_followup_tasks f
              WHERE f.conversation_id = r.conversation_id AND f.run_id = r.run_id)
           AS message_count
    FROM public.agent_conversation_runs r
    JOIN public.agent_conversations c ON c.conversation_id = r.conversation_id
    WHERE r.status IN ('COMPLETED', 'FAILED', 'INTERRUPTED', 'CANCELLED')
      -- ONLINE_FILTER
), candidates AS MATERIALIZED (
    SELECT o.*
    FROM observed o
    LEFT JOIN public.agent_run_usage_stats s
      ON s.conversation_id = o.conversation_id AND s.run_id = o.run_id
    WHERE s.run_id IS NULL OR s.message_count < o.message_count
    ORDER BY o.conversation_id, o.run_id
    LIMIT 1000 -- ONLINE_LIMIT
), refreshed AS (
    INSERT INTO public.agent_run_usage_stats AS saved (
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
           c.status, c.message_count
    FROM candidates c
    -- 旧快照或源数据被清理后，不能把已保存的累计计数改小。
    -- 只更新计数，不改变原会话归属、创建日和 run 完成时间。
    ON CONFLICT (conversation_id, run_id) DO UPDATE
    SET message_count = GREATEST(saved.message_count, EXCLUDED.message_count)
    WHERE saved.message_count < EXCLUDED.message_count
    RETURNING conversation_id, run_id
)
-- 保留旧返回列名；数值包括新增行和计数更新行。
SELECT COUNT(*) AS inserted_count FROM refreshed;
