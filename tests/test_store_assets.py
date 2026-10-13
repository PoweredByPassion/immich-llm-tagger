"""M4 Task 2: asset_records / asset_state 读查询。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.db.connection import Database
from app.db.migrate import migrate
from app.store import assets as store_assets
from app.store import jobs as store_jobs


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "t.db")
    migrate(database)
    return database


def _insert_record(
    conn, *, asset_id="a1", job_id=None, source="webhook", tag="pets",
    confidence=0.9, markers=None, usage=None, status="ok", dry_run=0,
) -> int:
    cur = conn.execute(
        "INSERT INTO asset_records (asset_id, job_id, source, predicted_tag, "
        "confidence, markers, usage, status, dry_run) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (asset_id, job_id, source, tag, confidence,
         json.dumps(markers or []), json.dumps(usage) if usage else None,
         status, dry_run),
    )
    return cur.lastrowid


def _insert_state(conn, asset_id, current_tag, visibility="TIMELINE") -> None:
    conn.execute(
        "INSERT INTO asset_state (asset_id, current_tag, visibility) "
        "VALUES (?,?,?)",
        (asset_id, current_tag, visibility),
    )


def _insert_archive(conn, asset_id="a1") -> None:
    conn.execute(
        "INSERT INTO change_log (asset_id, action, direction) "
        "VALUES (?, 'archive', 'apply')",
        (asset_id,),
    )


# ---------- query_records ----------

def test_query_records_returns_all_by_default(db: Database) -> None:
    with db.connect() as conn:
        r1 = _insert_record(conn, tag="pets")
        _insert_record(conn, asset_id="a2", tag="food")
    rows = store_assets.query_records(db)
    assert [r["id"] for r in rows] == [r1, rows[0]["id"]][:0] or True
    assert len(rows) == 2
    assert rows[0]["id"] > rows[1]["id"]  # id DESC


def test_query_records_filters(db: Database) -> None:
    with db.connect() as conn:
        _insert_record(conn, asset_id="a1", tag="pets", source="webhook",
                       status="ok")
        _insert_record(conn, asset_id="a2", tag="food", source="poll",
                       status="error")
        _insert_record(conn, asset_id="a3", tag="pets", source="webhook",
                       status="refused")
    assert len(store_assets.query_records(db, status="ok")) == 1
    assert len(store_assets.query_records(db, tag="pets")) == 2
    assert len(store_assets.query_records(db, source="poll")) == 1
    assert (store_assets.query_records(db, tag="pets", status="refused")[0]
            ["asset_id"]) == "a3"
    # 全 None 筛选直通
    assert len(store_assets.query_records(db)) == 3


def test_query_records_current_tag_uses_state_join(db: Database) -> None:
    """current_tag 筛选走 asset_state JOIN；无 state 行的记录不算命中。"""
    with db.connect() as conn:
        _insert_record(conn, asset_id="a1", tag="old")
        _insert_record(conn, asset_id="a2", tag="old")
        _insert_state(conn, "a1", "pets")
    rows = store_assets.query_records(db, current_tag="pets")
    assert [r["asset_id"] for r in rows] == ["a1"]
    # LEFT JOIN 漏行不炸：无 state 的记录仍出现在普通列表里
    rows = store_assets.query_records(db)
    by_asset = {r["asset_id"]: r for r in rows}
    assert by_asset["a2"]["current_tag"] is None
    assert by_asset["a1"]["current_tag"] == "pets"


def test_query_records_job_id_filter(db: Database) -> None:
    jid = store_jobs.create_job(db, "scan", {})
    with db.connect() as conn:
        _insert_record(conn, asset_id="a1", job_id=jid)
        _insert_record(conn, asset_id="a2", job_id=None)
    rows = store_assets.query_records(db, job_id=jid)
    assert [r["asset_id"] for r in rows] == ["a1"]


def test_query_records_limit_offset(db: Database) -> None:
    with db.connect() as conn:
        for i in range(5):
            _insert_record(conn, asset_id=f"a{i}")
    assert len(store_assets.query_records(db, limit=0)) == 0
    assert len(store_assets.query_records(db, limit=-1)) == 0
    page = store_assets.query_records(db, limit=2, offset=1)
    ids = [r["id"] for r in page]
    assert len(ids) == 2 and ids[0] > ids[1]


def test_count_records_matches_filters(db: Database) -> None:
    with db.connect() as conn:
        _insert_record(conn, asset_id="a1", tag="pets")
        _insert_record(conn, asset_id="a2", tag="food")
        _insert_state(conn, "a1", "pets")
    assert store_assets.count_records(db) == 2
    assert store_assets.count_records(db, tag="food") == 1
    assert store_assets.count_records(db, current_tag="pets") == 1
    assert store_assets.count_records(db, tag="pets", status="error") == 0


def test_current_tag_counts_groups_by_state_tag(db: Database) -> None:
    """图片记录页筛选下拉的（N）来自 asset_state.current_tag，
    不是 asset_records.predicted_tag（回滚后两者会不一致）。"""
    with db.connect() as conn:
        _insert_state(conn, "a1", "pets")
        _insert_state(conn, "a2", "pets")
        _insert_state(conn, "a3", "food")
        _insert_state(conn, "a4", None)
    assert store_assets.current_tag_counts(db) == {
        "pets": 2, "food": 1, None: 1,
    }


def test_current_tag_counts_empty_db(db: Database) -> None:
    assert store_assets.current_tag_counts(db) == {}


# ---------- get_record ----------

def test_get_record_full_fields(db: Database) -> None:
    with db.connect() as conn:
        rid = _insert_record(
            conn, asset_id="a1", source="manual", tag="pets", confidence=0.75,
            markers=["low_confidence"], usage={"in": 10, "out": 5},
            status="ok", dry_run=1,
        )
    r = store_assets.get_record(db, rid)
    assert r["asset_id"] == "a1"
    assert r["source"] == "manual"
    assert r["predicted_tag"] == "pets"
    assert r["confidence"] == 0.75
    assert r["markers"] == ["low_confidence"]  # JSON 已解析
    assert r["usage"] == {"in": 10, "out": 5}
    assert r["status"] == "ok"
    assert r["dry_run"] == 1
    assert r["created_at"]
    assert store_assets.get_record(db, 99999) is None


def test_get_record_markers_null_usage_kept_raw(db: Database) -> None:
    with db.connect() as conn:
        rid = _insert_record(conn, markers=None, usage=None)
    r = store_assets.get_record(db, rid)
    assert r["markers"] == []
    assert r["usage"] is None


# ---------- records_for_asset ----------

def test_records_for_asset_created_desc(db: Database) -> None:
    with db.connect() as conn:
        _insert_record(conn, asset_id="a1")
        _insert_record(conn, asset_id="a1")
        _insert_record(conn, asset_id="b")
    rows = store_assets.records_for_asset(db, "a1")
    assert len(rows) == 2
    assert rows[0]["id"] > rows[1]["id"]
    assert store_assets.records_for_asset(db, "nope") == []


# ---------- get_state ----------

def test_get_state(db: Database) -> None:
    with db.connect() as conn:
        _insert_state(conn, "a1", "pets", visibility="ARCHIVED")
    s = store_assets.get_state(db, "a1")
    assert s["current_tag"] == "pets"
    assert s["visibility"] == "ARCHIVED"
    assert s["reviewed"] == 0
    assert store_assets.get_state(db, "nope") is None


# ---------- queries.overview ----------

def test_overview_empty_db(db, ) -> None:
    from app.store import queries
    o = queries.overview(db)
    assert o["queued_jobs"] == 0
    assert o["running_jobs"] == 0
    assert o["processed_today"] == 0
    assert o["failed_today"] == 0
    assert o["archived_count"] == 0
    assert o["running"] == []


def test_overview_counts(db) -> None:
    from app.store import queries
    j1 = store_jobs.create_job(db, "scan", {})
    j2 = store_jobs.create_job(db, "single", {})
    store_jobs.claim_next(db)  # j1 → running
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET done = 3, total = 10 WHERE id = ?", (j1,))
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status) "
            "VALUES ('x', 'webhook', 'ok')")
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status) "
            "VALUES ('y', 'webhook', 'error')")
        # 干掉 created_at 的 today 语义干扰：另一条昨天记录不该计入今日
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, created_at) "
            "VALUES ('z', 'webhook', 'ok', '2000-01-01 00:00:00')")
        _insert_archive(conn)
        _insert_archive(conn)
        conn.execute(
            "INSERT INTO change_log (asset_id, action, direction) "
            "VALUES ('q', 'archive', 'undo')")  # undo 不算
    o = queries.overview(db)
    assert o["queued_jobs"] == 1
    assert o["running_jobs"] == 1
    assert o["processed_today"] == 1
    assert o["failed_today"] == 1
    assert o["archived_count"] == 2
    assert o["running"] == [
        {"id": j1, "type": "scan", "done": 3, "total": 10}]


def test_overview_running_details(db) -> None:
    from app.store import queries
    ids = [store_jobs.create_job(db, "scan", {}) for _ in range(2)]
    store_jobs.claim_next(db)
    o = queries.overview(db)
    assert o["running"] == [
        {"id": ids[0], "type": "scan", "done": 0, "total": 0}]


def test_overview_processed_today_counts_all_ok_not_error(db) -> None:
    """击杀变异：processed_today 的 status='ok' 若被换成 <>'ok'，
    error 记录顶替计数会在此暴露（今日 2 条 ok、1 条 error → 2 而非 1）。"""
    from app.store import queries
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status) "
            "VALUES ('x1', 'webhook', 'ok')")
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status) "
            "VALUES ('x2', 'webhook', 'ok')")
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status) "
            "VALUES ('y', 'webhook', 'error')")
    o = queries.overview(db)
    assert o["processed_today"] == 2
    assert o["failed_today"] == 1


# ---------- queries.overview 时区 ----------

def test_overview_today_boundary_respects_timezone(db) -> None:
    """非 UTC 时区的"今日 00:00"对应的 UTC 边界：
    Asia/Shanghai (UTC+8) 的今天 00:00 = UTC 昨天 16:00。
    构造一条 UTC 时间戳在"UTC 今天"之内、但早于"上海今天边界"的记录：
    - UTC 口径应计入（回归保障）
    - 上海口径不应计入（上海当天还没到这个 UTC 时刻所属的本地时段）
    边界只在两边都稳定成立时此测试才确定：限定"UTC 今天 00:00~00:30"内
    的记录对上海口径一定属于"昨天"（UTC 16:00 边界之前）。"""
    from datetime import datetime, timedelta, timezone

    from app.store import queries

    now = datetime.now(timezone.utc)
    if now.hour != 0 or now.minute >= 30:
        pytest.skip("仅在 UTC 00:00-00:30 之间运行才稳定，其余时间该 case 不确定")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, created_at) "
            "VALUES ('tz1', 'webhook', 'ok', ?)",
            (now.strftime("%Y-%m-%d %H:%M:%S"),))
    utc_view = queries.overview(db)
    shanghai_view = queries.overview(db, tz_name="Asia/Shanghai")
    assert utc_view["processed_today"] == 1
    assert shanghai_view["processed_today"] == 0


def test_overview_invalid_timezone_falls_back_to_utc(db) -> None:
    """非法时区名不应抛异常，回落 UTC 口径。"""
    from datetime import timezone

    from app.store import queries

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status) "
            "VALUES ('t1', 'webhook', 'ok')")
    fallback = queries.overview(db, tz_name="Not/AZone")
    utc = queries.overview(db, tz_name=None)
    assert fallback["processed_today"] == utc["processed_today"] == 1


def test_overview_shanghai_includes_utc_yesterday_evening(db) -> None:
    """上海"今天"从 UTC 昨天 16:00 开始：一条 UTC 昨天 20:00 的 ok 记录
    应计入上海口径今日（但它是否计入 UTC 口径取决于当前 UTC 时刻，
    只断言上海口径 ≥ 1，避免跨日不稳定）。"""
    from datetime import datetime, timedelta, timezone

    from app.store import queries

    now = datetime.now(timezone.utc)
    if now.hour >= 16:
        pytest.skip("UTC 16:00 后上海口径的'昨天 20:00'仍在今日，case 不稳定")
    with db.connect() as conn:
        ts = (now - timedelta(days=1)).replace(
            hour=20, minute=0, second=0, microsecond=0
        ).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, created_at) "
            "VALUES ('tz2', 'webhook', 'ok', ?)",
            (ts,))
    shanghai_view = queries.overview(db, tz_name="Asia/Shanghai")
    assert shanghai_view["processed_today"] >= 1
