"""M5 Task 7: 用量聚合查询（asset_records.usage JSON 聚合）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.db.connection import Database
from app.db.migrate import migrate
from app.store import usage as store_usage


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "t.db")
    migrate(database)
    return database


def _record(
    conn,
    *,
    asset_id="a1",
    job_id=None,
    usage=None,
    model=None,
    created_at=None,
    dry_run=0,
) -> None:
    conn.execute(
        "INSERT INTO asset_records (asset_id, job_id, model, usage, status, "
        "dry_run, source, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (asset_id, job_id, model,
         json.dumps(usage) if usage is not None else None,
         "ok", dry_run, "manual",
         created_at or "2020-10-12 10:00:00"),
    )


def _job(conn, *, type_="scan", status="done") -> int:
    cur = conn.execute(
        "INSERT INTO jobs (type, status, total) VALUES (?,?,0)",
        (type_, status),
    )
    return cur.lastrowid


# ---------- by_day ----------

def test_by_day_counts_and_tokens(db: Database) -> None:
    with db.connect() as conn:
        _record(conn, created_at="2020-10-11 08:00:00",
                usage={"prompt_tokens": 10, "completion_tokens": 5})
        _record(conn, created_at="2020-10-11 09:00:00",
                usage={"prompt_tokens": 100, "completion_tokens": 50})
        _record(conn, created_at="2020-10-12 08:00:00",
                usage={"prompt_tokens": 7, "completion_tokens": 3})
    rows = store_usage.by_day(db)
    assert len(rows) == 2
    assert rows[0]["day"] == "2020-10-12"  # DESC
    assert rows[0]["requests"] == 1
    assert rows[0]["prompt_tokens"] == 7
    assert rows[0]["completion_tokens"] == 3
    assert rows[0]["tokens"] == 10
    assert rows[1]["requests"] == 2
    assert rows[1]["prompt_tokens"] == 110
    assert rows[1]["completion_tokens"] == 55
    assert rows[1]["tokens"] == 165


def test_by_day_usage_null_treated_as_zero(db: Database) -> None:
    with db.connect() as conn:
        _record(conn, usage=None)
        _record(conn, usage={"prompt_tokens": 4})
    rows = store_usage.by_day(db)
    assert rows[0]["requests"] == 2
    assert rows[0]["prompt_tokens"] == 4
    assert rows[0]["completion_tokens"] == 0
    assert rows[0]["tokens"] == 4


def test_by_day_partial_fields_and_total_token_fallback(db: Database) -> None:
    """total_tokens 缺失时 tokens = prompt + completion。"""
    with db.connect() as conn:
        _record(conn, usage={"prompt_tokens": 4, "completion_tokens": 6})
    rows = store_usage.by_day(db)
    assert rows[0]["tokens"] == 10


def test_by_day_limit(db: Database) -> None:
    with db.connect() as conn:
        for d in range(5):
            _record(conn, created_at=f"2026-10-0{d + 1} 08:00:00",
                    usage={"prompt_tokens": 1, "completion_tokens": 1})
    rows = store_usage.by_day(db, limit=3)
    assert len(rows) == 3
    assert rows[0]["day"] == "2026-10-05"


# ---------- by_model ----------

def test_by_model_groups(db: Database) -> None:
    with db.connect() as conn:
        _record(conn, model="gpt-4o-mini",
                usage={"prompt_tokens": 10, "completion_tokens": 5})
        _record(conn, asset_id="a2", model="gpt-4o-mini",
                usage={"prompt_tokens": 1, "completion_tokens": 2})
        _record(conn, asset_id="a3", model="claude",
                usage={"prompt_tokens": 100, "completion_tokens": 0})
        _record(conn, asset_id="a4", model=None)  # 无 model 不参与
    rows = store_usage.by_model(db)
    assert len(rows) == 2
    assert rows[0]["model"] == "claude"  # tokens 降序
    assert rows[0]["requests"] == 1
    assert rows[0]["tokens"] == 100
    assert rows[1]["model"] == "gpt-4o-mini"
    assert rows[1]["requests"] == 2
    assert rows[1]["tokens"] == 18


# ---------- by_job ----------

def test_by_job_joins_jobs(db: Database) -> None:
    with db.connect() as conn:
        j1 = _job(conn, type_="scan", status="done")
        j2 = _job(conn, type_="dryrun", status="done")
        _record(conn, job_id=j1, usage={"prompt_tokens": 1, "completion_tokens": 1})
        _record(conn, asset_id="a2", job_id=j1,
                usage={"prompt_tokens": 2, "completion_tokens": 2})
        _record(conn, asset_id="a3", job_id=j2,
                usage={"prompt_tokens": 5, "completion_tokens": 0})
        _record(conn, asset_id="a4", job_id=None)  # 无 job 也计入
    rows = store_usage.by_job(db)
    assert len(rows) == 3
    top = rows[0]
    assert top["job_id"] == j1
    assert top["type"] == "scan"
    assert top["status"] == "done"
    assert top["requests"] == 2
    assert top["tokens"] == 6
    # job_id 为 NULL 的记录：type/status 为 None
    null_row = [r for r in rows if r["job_id"] is None][0]
    assert null_row["type"] is None
    assert null_row["requests"] == 1


def test_by_job_limit(db: Database) -> None:
    with db.connect() as conn:
        for _ in range(3):
            jid = _job(conn)
            _record(conn, job_id=jid, usage={"prompt_tokens": 1, "completion_tokens": 0})
    assert len(store_usage.by_job(db, limit=2)) == 2


# ---------- totals ----------

def test_totals_today_and_all(db: Database) -> None:
    with db.connect() as conn:
        _record(conn, created_at="2020-10-11 08:00:00",
                usage={"prompt_tokens": 10, "completion_tokens": 10})
        _record(conn, dry_run=1,  # dry_run 计入
                usage={"prompt_tokens": 20, "completion_tokens": 5})
    rows = store_usage.totals(db)
    assert rows["all"]["requests"] == 2
    assert rows["all"]["prompt_tokens"] == 30
    assert rows["all"]["completion_tokens"] == 15
    assert rows["all"]["tokens"] == 45
    # fixture 日期为 2020 年，非今日 → today 必为 0
    assert rows["today"]["requests"] == 0
    assert rows["today"]["tokens"] == 0


def test_dry_run_counted_in_aggregates(db: Database) -> None:
    with db.connect() as conn:
        _record(conn, dry_run=1, usage={"prompt_tokens": 3, "completion_tokens": 3})
    assert store_usage.totals(db)["all"]["tokens"] == 6
    assert store_usage.by_day(db)[0]["requests"] == 1


def test_empty_db_returns_empty_structures(db: Database) -> None:
    assert store_usage.by_day(db) == []
    assert store_usage.by_model(db) == []
    assert store_usage.by_job(db) == []
    totals = store_usage.totals(db)
    assert totals["all"]["requests"] == 0
    assert totals["today"]["requests"] == 0
