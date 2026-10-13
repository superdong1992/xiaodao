"""使用现有 repository 的同步统计写入端，只在最外层业务事务提交后调用。

repository 将 SQL 的 ? 占位符转换为 psycopg 3 的 %s。
嵌套调用 database_transaction() 可能只创建 savepoint，不是独立事务。
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def record_closed_run(repository, *, conversation_id: str, run_id: str) -> bool:
    """业务事务提交后保存终态统计；普通统计失败返回 False，不向业务调用方抛出。"""
    try:
        if not conversation_id or not run_id:
            raise ValueError("统计写入缺少 conversation_id 或 run_id。")
        sql = Path(__file__).with_name("backfill.sql").read_text(encoding="utf-8")
        sql = sql.replace(
            "-- ONLINE_FILTER", "AND r.conversation_id = ? AND r.run_id = ?"
        ).replace("LIMIT 1000 -- ONLINE_LIMIT", "LIMIT 1")
        with repository.database_transaction() as stats_db:
            stats_db.execute("SET LOCAL statement_timeout = '1000ms'")
            stats_db.execute("SET LOCAL lock_timeout = '250ms'")
            inserted = stats_db.execute(sql, (conversation_id, run_id)).fetchone()[0]
            if not inserted:
                exists = stats_db.execute(
                    "SELECT 1 FROM public.agent_run_usage_stats "
                    "WHERE conversation_id = ? AND run_id = ?",
                    (conversation_id, run_id),
                ).fetchone()
                if exists is None:
                    raise ValueError("未找到已提交的终态 run，无法保存统计。")
        return True
    except Exception as exc:
        # 生产应另接失败计数与补录告警；不记录正文、附件、连接串或完整异常文本。
        logger.warning(
            "会话统计写入失败，等待补录。conversation_id=%s run_id=%s error_type=%s",
            conversation_id, run_id, type(exc).__name__,
        )
        return False


# 接入示意（不是现有 repository API）：
# with <现有业务事务上下文> as db:
#     <现有 _close、_save 等业务操作>
#     closed_ids = (body["conversation_id"], body["run_id"])
#
# # 确认真正的最外层事务已经成功提交，才能执行：
# record_closed_run(self.repository,
#                   conversation_id=closed_ids[0], run_id=closed_ids[1])
#
# 一个事务关闭多个 run 时，应收集并写入每个实际结束的 run。
# 不能在 finally 里调用，也不能用 current_run_id 代替刚结束的旧 run_id。
# 不要在 _close 内增加统计 SQL 后仅 try/except：SQL 错误可能使主事务失效。
