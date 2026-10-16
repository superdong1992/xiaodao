-- 只读检查。先执行字段检查，确认字段存在后再执行后续查询。
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name IN ('agent_conversations', 'agent_conversation_runs', 'agent_messages', 'agent_followup_tasks')
ORDER BY table_name, ordinal_position;

-- false 只表示保留数据中未发现多 run，不能证明产品不支持。
SELECT EXISTS (
    SELECT 1 FROM public.agent_conversation_runs
    GROUP BY conversation_id HAVING COUNT(*) > 1
) AS observed_multiple_runs;

SELECT r.status, COUNT(*) AS missing_completed_at
FROM public.agent_conversation_runs r
WHERE r.status IN ('COMPLETED', 'FAILED', 'INTERRUPTED', 'CANCELLED')
  AND NULLIF(CAST(r.body AS JSONB) ->> 'completed_at', '') IS NULL
GROUP BY r.status;

SELECT COUNT(*) AS messages_without_run_id
FROM public.agent_messages WHERE run_id IS NULL OR run_id = '';

SELECT COUNT(*) AS followups_without_run_id
FROM public.agent_followup_tasks WHERE run_id IS NULL OR run_id = '';

-- 建表和写入后再执行；非零时先解决元数据冲突，不发布成准确的统计结果。
SELECT COUNT(*) AS inconsistent_conversation_count
FROM public.agent_conversation_usage_stats WHERE metadata_conflict;
