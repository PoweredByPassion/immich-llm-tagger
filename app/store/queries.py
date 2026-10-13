"""概览页聚合查询，集中在此避免路由拼多条 SQL。

连接状态（Immich/LLM）不在 DB，概览路由自行探测，不进 overview()。

今日边界：按调用方传入的 IANA 时区名计算"本地 00:00 对应的 UTC 时刻"；
数据库 created_at 一律 UTC 存储，仅在此换算查询边界。tz 缺省/非法时回落 UTC。
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.db.connection import Database

# scan 拆出的内部子任务类型：不进面向用户的队列深度/运行列表。
_INTERNAL_TYPE = "classify_item"


def _today_start_utc(tz_name: str | None) -> str:
    """给定 IANA 时区名，返回该时区"今天 00:00"对应的 UTC 时刻字符串。

    无效或缺失时区 → 回落 UTC 当天 00:00。返回值直接用于 SQL 比较。
    """
    if tz_name:
        try:
            now_local = datetime.now(ZoneInfo(tz_name))
            start_local = now_local.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            return start_local.astimezone(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass  # 回落 UTC
    return datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).strftime("%Y-%m-%d %H:%M:%S")


def overview(db: Database, tz_name: str | None = None) -> dict:
    """概览计数：队列深度、今日处理/失败（按 tz_name 本地自然日）、
    归档计数（change_log archive+apply）、运行中 job 详情。"""
    today_start = _today_start_utc(tz_name)
    with db.connect() as conn:
        # classify_item 是 scan 拆出的内部子任务，不进面向用户的队列深度/运行列表。
        queued = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs "
            "WHERE status = 'queued' AND type != ?",
            (_INTERNAL_TYPE,),
        ).fetchone()["n"]
        running = conn.execute(
            "SELECT id, type, done, total FROM jobs WHERE status = 'running' "
            "AND type != ? ORDER BY id",
            (_INTERNAL_TYPE,),
        ).fetchall()
        processed = conn.execute(
            "SELECT COUNT(*) AS n FROM asset_records "
            "WHERE created_at >= ? AND status = 'ok'",
            (today_start,),
        ).fetchone()["n"]
        failed = conn.execute(
            "SELECT COUNT(*) AS n FROM asset_records "
            "WHERE created_at >= ? AND status = 'error'",
            (today_start,),
        ).fetchone()["n"]
        archived = conn.execute(
            "SELECT COUNT(*) AS n FROM change_log "
            "WHERE action = 'archive' AND direction = 'apply'"
        ).fetchone()["n"]
    return {
        "queued_jobs": queued,
        "running_jobs": len(running),
        "processed_today": processed,
        "failed_today": failed,
        "archived_count": archived,
        "running": [dict(r) for r in running],
    }
