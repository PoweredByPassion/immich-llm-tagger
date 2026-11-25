"""Prune diagnostic history while keeping undoable jobs and unfinished writes."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.db.connection import Database
from app.store.settings import SettingsStore

_TERMINAL_STATUSES = ("done", "failed", "canceled")


def purge_expired(db: Database) -> dict:
    """清理早于保留期的记录。0 = 禁用，返回 {"purged": False}。"""
    settings = SettingsStore(db).get_all()
    days = settings["record_retention_days"]
    if days == 0:
        return {"purged": False}
    now = datetime.now(UTC)
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    undo_days = min(int(settings["undo_retention_days"]), 99999)
    undo_cutoff = (now - timedelta(days=max(days, undo_days))).strftime("%Y-%m-%d %H:%M:%S")
    result: dict = {}
    with db.connect() as conn:
        placeholders = ",".join("?" for _ in _TERMINAL_STATUSES)
        cur = conn.execute(
            f"DELETE FROM jobs WHERE status IN ({placeholders})"
            " AND finished_at IS NOT NULL AND finished_at < ?"
            " AND ((? > 0 AND finished_at < ?) OR NOT EXISTS ("
            "      SELECT 1 FROM change_log WHERE job_id=jobs.id AND direction='apply'))"
            " AND NOT EXISTS (SELECT 1 FROM pending_effects WHERE job_id=jobs.id)"
            " AND NOT EXISTS (SELECT 1 FROM inverse_steps s"
            "                 WHERE s.job_id=jobs.id AND s.status='pending')"
            " AND NOT EXISTS (SELECT 1 FROM jobs AS undo"
            "                 WHERE undo.type='undo' AND undo.status IN ('queued','running')"
            "                 AND json_extract(undo.params, '$.target_job_id')=jobs.id)",
            (*_TERMINAL_STATUSES, cutoff, undo_days, undo_cutoff),
        )
        result["jobs"] = cur.rowcount
        conn.execute(
            "DELETE FROM inverse_steps WHERE status!='pending' "
            "AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.id=inverse_steps.job_id)"
        )
        cur = conn.execute(
            "DELETE FROM asset_records WHERE created_at < ? AND NOT EXISTS ("
            "SELECT 1 FROM pending_effects p WHERE p.analysis_record_id=asset_records.id)", (cutoff,)
        )
        result["asset_records"] = cur.rowcount
        cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        result["events"] = cur.rowcount
    return result
