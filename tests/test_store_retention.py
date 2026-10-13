"""M6 Task 5: retention 清理测试。

purge_expired 按 record_retention_days 清理 jobs（仅终态）/ asset_records / events；
change_log 绝不动。0 = 禁用。
"""
from pathlib import Path

import pytest

from app.db.connection import Database
from app.db.migrate import migrate
from app.store import events, jobs
from app.store.retention import purge_expired
from app.store.settings import SettingsStore


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "t.db")
    migrate(database)
    return database


def _make_job(db: Database, status: str, *, finished_at: str | None = None) -> int:
    jid = jobs.create_job(db, "scan", {})
    if status != "queued":
        with db.connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = :status, started_at = :f, finished_at = :f"
                " WHERE id = :id",
                {"status": status, "f": finished_at, "id": jid},
            )
    return jid


def _count(db: Database, table: str) -> int:
    with db.connect() as conn:
        return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


# ---------- 禁用 ----------

def test_zero_days_disabled(db: Database) -> None:
    SettingsStore(db).set("record_retention_days", 0)
    _make_job(db, "done", finished_at="2020-01-01 00:00:00")
    result = purge_expired(db)
    assert result == {"purged": False}
    assert _count(db, "jobs") == 1


# ---------- 90 天前的终态数据被清 ----------

def test_old_terminal_job_purged(db: Database) -> None:
    old = _make_job(db, "done", finished_at="2020-01-01 00:00:00")
    result = purge_expired(db)
    assert result["jobs"] == 1
    assert jobs.get_job(db, old) is None


def test_old_asset_records_and_events_purged(db: Database) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, job_id, source, status,"
            " created_at) VALUES ('a1', NULL, 'manual', 'ok', '2020-01-01 00:00:00')"
        )
        conn.execute(
            "INSERT INTO events (source, event_type, payload, ts)"
            " VALUES ('poll', 'old', '{}', '2020-01-01 00:00:00')"
        )
    result = purge_expired(db)
    assert result["asset_records"] == 1
    assert result["events"] == 1
    assert _count(db, "asset_records") == 0
    assert _count(db, "events") == 0


# ---------- 新数据保留 ----------

def test_recent_data_kept(db: Database) -> None:
    _make_job(db, "done")  # finished_at = now
    events.log_event(db, "poll", "recent", {})
    result = purge_expired(db)
    assert result == {"jobs": 0, "asset_records": 0, "events": 0}
    assert _count(db, "jobs") == 1
    assert _count(db, "events") == 1


# ---------- 未终态 job 不清 ----------

@pytest.mark.parametrize("status", ["queued", "running"])
def test_non_terminal_jobs_never_purged(db: Database, status: str) -> None:
    # 终态时间列即使填了很旧的值也只认 status
    jid = jobs.create_job(db, "scan", {})
    with db.connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = :s, started_at = '2020-01-01 00:00:00',"
            " finished_at = '2020-01-01 00:00:00' WHERE id = :id",
            {"s": status, "id": jid},
        )
    purge_expired(db)
    assert jobs.get_job(db, jid) is not None


# ---------- change_log 完好 ----------

def test_change_log_untouched(db: Database) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO change_log (asset_id, action, direction, payload, applied_at)"
            " VALUES ('a1', 'tag_add', 'apply', '{}', '2020-01-01 00:00:00')"
        )
    SettingsStore(db).set("record_retention_days", 0)
    purge_expired(db)
    assert _count(db, "change_log") == 1


# ---------- scheduler 挂点：24h 自管理间隔 ----------

def test_scheduler_retention_runs_once_per_24h(db: Database, tmp_path: Path) -> None:
    """可注入的 now/purge 参数驱动：>24h 才重复跑；last_retention_run 存 settings。"""
    from app.engine.scheduler import PollScheduler

    scheduler = PollScheduler(
        tmp_path / "t.db",
        lambda: None,
        interval_getter=lambda: 60,
        enabled_getter=lambda: True,
    )
    settings = SettingsStore(db)
    settings.set("record_retention_days", 0)
    calls: list[dict] = []
    purge = lambda _: calls.append({"purged": False}) or calls[-1]

    assert scheduler.maybe_purge(now=1000.0, purge=purge) == {"purged": False}
    assert calls == [{"purged": False}]
    assert settings.get("last_retention_run") == 1000.0
    # 24h 内不重复跑
    assert scheduler.maybe_purge(now=1000.0 + 3600, purge=purge) is None
    assert len(calls) == 1
    # 超过 24h 再跑
    assert scheduler.maybe_purge(now=1000.0 + 86401, purge=purge) == {"purged": False}
    assert len(calls) == 2
