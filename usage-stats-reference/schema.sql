-- 参考方案，先核对 README.txt。示例时区 Asia/Shanghai 必须在部署前确认。
BEGIN;
CREATE TABLE public.agent_run_usage_stats (
    conversation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    owner_key TEXT,
    created_at TIMESTAMPTZ NOT NULL, -- 会话最初创建时间
    run_created_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    terminal_status TEXT NOT NULL CHECK (
        terminal_status IN ('COMPLETED', 'FAILED', 'INTERRUPTED', 'CANCELLED')
    ),
    message_count BIGINT NOT NULL CHECK (message_count >= 0),
    created_day DATE GENERATED ALWAYS AS (
        CAST(created_at AT TIME ZONE 'Asia/Shanghai' AS DATE)
    ) STORED,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (conversation_id, run_id)
);
CREATE INDEX agent_run_usage_stats_day ON public.agent_run_usage_stats (created_day);
CREATE INDEX agent_run_usage_stats_time ON public.agent_run_usage_stats (created_at);

-- 不建立指向业务表的外键，清理任务必须排除统计表。
-- 同一会话的 owner_key 和 created_at 必须一致；metadata_conflict 用于核查。
CREATE VIEW public.agent_conversation_usage_stats AS
SELECT conversation_id,
       MIN(owner_key) AS owner_key,
       MIN(created_at) AS created_at,
       MIN(created_day) AS created_day,
       MAX(completed_at) AS last_completed_at,
       SUM(message_count) AS message_count,
       COUNT(*) AS closed_run_count,
       (COUNT(DISTINCT owner_key) > 1
        OR COUNT(owner_key) NOT IN (0, COUNT(*))
        OR MIN(created_at) <> MAX(created_at)) AS metadata_conflict
FROM public.agent_run_usage_stats
GROUP BY conversation_id;
COMMIT;
