"""数据保留与自动清理（retention）。

按 record_retention_days 清理 jobs（仅终态 done/failed/canceled，按 finished_at）、
asset_records（按 created_at）、events（按 ts）。change_log 保持留存供回滚。
时间列均为 datetime('now') 生成的 TEXT，可直接字符串比较。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.connection import Database
from app.store.settings import SettingsStore

_TERMINAL_STATUSES = ("done", "failed", "canceled")


def purge_expired(db: Database) -> dict:
    """清理早于保留期的记录。0 = 禁用，返回 {"purged": False}。"""
    days = SettingsStore(db).get("record_retention_days")
    if days == 0:
        return {"purged": False}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    result: dict = {}
    with db.connect() as conn:
        placeholders = ",".join("?" for _ in _TERMINAL_STATUSES)
        cur = conn.execute(
            f"DELETE FROM jobs WHERE status IN ({placeholders})"
            " AND finished_at IS NOT NULL AND finished_at < ?",
            (*_TERMINAL_STATUSES, cutoff),
        )
        result["jobs"] = cur.rowcount
        cur = conn.execute(
            "DELETE FROM asset_records WHERE created_at < ?", (cutoff,)
        )
        result["asset_records"] = cur.rowcount
        cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        result["events"] = cur.rowcount
    return result
