"""events 写入 + 64KB 截断（M3 Task 2）。"""
import json
from pathlib import Path

import pytest

from app.db.connection import Database
from app.db.migrate import migrate
from app.store.events import log_event, tail_events


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "t.db")
    migrate(d)
    return d


def test_write_and_tail_fields_complete(db: Database) -> None:
    log_event(db, "webhook", "asset_ingested", {"asset_id": "a1"})
    log_event(db, "system", "job_started", None)
    rows = tail_events(db)
    assert len(rows) == 2
    latest = rows[0]
    assert latest["source"] == "system"
    assert latest["event_type"] == "job_started"
    assert latest["payload"] == "{}"
    assert latest["ts"]
    assert isinstance(latest["id"], int)
    assert rows[1]["payload"] == json.dumps({"asset_id": "a1"})


def test_tail_limit(db: Database) -> None:
    for i in range(5):
        log_event(db, "poll", "asset_skipped", {"i": i})
    rows = tail_events(db, limit=3)
    assert len(rows) == 3
    assert [r["payload"] for r in rows] == [
        json.dumps({"i": i}) for i in (4, 3, 2)
    ]


def test_payload_over_64kb_truncated_with_marker(db: Database) -> None:
    payload = {"blob": "x" * 100_000}
    log_event(db, "system", "job_error", payload)
    (row,) = tail_events(db)
    assert len(row["payload"]) <= 64 * 1024
    assert "truncated" in row["payload"]


def test_payload_exactly_64kb_not_truncated(db: Database) -> None:
    """序列化后恰好 64KB：边界不截断。"""
    # json.dumps({"blob": "x"*n}) = 12 + n
    n = 64 * 1024 - 12
    s = json.dumps({"blob": "x" * n})
    assert len(s) == 64 * 1024
    log_event(db, "system", "parse_fail", {"blob": "x" * n})
    (row,) = tail_events(db)
    assert row["payload"] == s
    assert "truncated" not in row["payload"]


def test_payload_none_stored_as_empty_object(db: Database) -> None:
    log_event(db, "poll", "asset_out_of_scope", None)
    (row,) = tail_events(db)
    assert row["payload"] == "{}"


def test_invalid_source_rejected(db: Database) -> None:
    import pytest

    with pytest.raises(Exception):
        log_event(db, "bogus", "asset_ingested", None)
