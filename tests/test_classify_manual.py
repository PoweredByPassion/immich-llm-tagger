"""M5 Task 8: 人工改判 + 标记已复核（app/classify/manual.py）的测试。

mock Immich client（不联网）：
- 摘旧标签只摘 asset 实际拥有的、且属于"本系统管理的标签"（§5.8）
- "本系统管理的标签"只看 asset_state 当前值 + tags 表，不看 asset_records 历史
- change_log 写 tag_remove/tag_add、direction=apply、job_id=NULL
- 不动 visibility / 相册；manual_override 置位
- clear_override 只清位，不回滚任何标签
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.classify.manual import apply_override, clear_override, mark_reviewed
from app.db.connection import Database
from app.db.migrate import migrate

ASSET_ID = "asset-manual-1"


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    migrate(d)
    return d


def _insert_tag(db, name: str, kind: str = "primary", enabled: int = 1) -> int:
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO tags (name, description, kind, enabled, archive, album) "
            "VALUES (?, '', ?, ?, 0, NULL)",
            (name, kind, enabled),
        )
        return cur.lastrowid


def _insert_state(db, asset_id: str, **kw) -> None:
    row = {
        "asset_id": asset_id,
        "current_tag": None,
        "current_markers": "[]",
        "visibility": "timeline",
        "manual_override": 0,
        "reviewed": 0,
    }
    row.update(kw)
    cols = list(row.keys())
    with db.connect() as conn:
        conn.execute(
            f"INSERT INTO asset_state ({', '.join(cols)}) "
            f"VALUES ({', '.join(':' + c for c in cols)})",
            row,
        )


def _insert_record(db, asset_id: str, markers: str = "[]") -> None:
    """往 asset_records 写历史（用于钉住"不看历史"）。"""
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, job_id, source, predicted_tag, "
            "confidence, markers, model, usage, duration_ms, status) "
            "VALUES (?, 1, 'webhook', '旧标签', 0.9, ?, 'm', '{}', 1, 'ok')",
            (asset_id, markers),
        )


class _Tag:
    def __init__(self, name: str, tid: str):
        self.name = name
        self.id = tid


class _Asset:
    def __init__(self, tags: list[_Tag]):
        self.tags = tags


class FakeClient:
    """记录调用的 Immich 替身；asset_tags 是该 asset 当前实际拥有的标签。
    ensure_tags 的 id 与 asset_tags 一致（模拟真实 Immich：同一标签 id 相同）。"""

    def __init__(self, asset_tags: list[_Tag] | None = None):
        self.asset = _Asset(asset_tags or [])
        self._ids = {t.name: t.id for t in (asset_tags or [])}
        self.ensure_tags_calls: list[list[str]] = []
        self.tag_assets_calls: list[tuple[list[str], list[str]]] = []
        self.untag_calls: list[tuple[str, list[str]]] = []

    def get_asset(self, asset_id: str) -> _Asset:
        return self.asset

    def ensure_tags(self, names: list[str]) -> dict[str, str]:
        self.ensure_tags_calls.append(list(names))
        return {n: self._ids.get(n, f"tag-id-{n}") for n in names}

    def tag_assets(self, tag_ids: list[str], asset_ids: list[str]) -> None:
        self.tag_assets_calls.append((list(tag_ids), list(asset_ids)))

    def untag_assets(self, tag_id: str, asset_ids: list[str]) -> None:
        self.untag_calls.append((tag_id, list(asset_ids)))


def _state(db, asset_id: str = ASSET_ID) -> dict:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM asset_state WHERE asset_id = ?", (asset_id,)
        ).fetchone()
    return dict(row) if row else None


def _change_logs(db, asset_id: str = ASSET_ID) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM change_log WHERE asset_id = ? ORDER BY id", (asset_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def _events(db, event_type: str) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE event_type = ? AND source = 'system'",
            (event_type,),
        ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------- mark_reviewed


def test_mark_reviewed_inserts_minimal_row(db):
    mark_reviewed(db, ASSET_ID)
    st = _state(db)
    assert st["reviewed"] == 1
    assert st["manual_override"] == 0
    assert st["current_tag"] is None
    assert st["visibility"] is None


def test_mark_reviewed_preserves_existing_fields(db):
    _insert_state(db, ASSET_ID, current_tag="风景", visibility="archive")
    mark_reviewed(db, ASSET_ID)
    st = _state(db)
    assert st["reviewed"] == 1
    assert st["current_tag"] == "风景"
    assert st["visibility"] == "archive"


def test_mark_reviewed_clear(db):
    _insert_state(db, ASSET_ID, reviewed=1)
    mark_reviewed(db, ASSET_ID, reviewed=False)
    assert _state(db)["reviewed"] == 0


def test_mark_reviewed_clear_no_row_is_noop(db):
    mark_reviewed(db, "ghost", reviewed=False)
    assert _state(db, "ghost") is None


def test_mark_reviewed_logs_event(db):
    mark_reviewed(db, ASSET_ID)
    assert len(_events(db, "asset_reviewed")) == 1


# ------------------------------------------------------------- apply_override


def test_apply_override_removes_only_owned_managed_tags(db):
    """摘旧只摘实际拥有的；tags 表里的其他标签、用户手打标签都不动。"""
    _insert_tag(db, "风景", "primary")
    _insert_tag(db, "天空", "marker")
    _insert_tag(db, "人像", "primary")  # 在 tags 表但 asset 没有
    _insert_state(
        db, ASSET_ID, current_tag="风景", current_markers=json.dumps(["天空"])
    )
    client = FakeClient(
        asset_tags=[_Tag("风景", "t1"), _Tag("用户的", "t2"), _Tag("人像", "t3")]
    )
    apply_override(db, client, ASSET_ID, "人像")
    # 风景(t1)是旧主标签，摘；人像(t3)虽实际拥有且在 tags 表，但它就是新标签
    # 本身——不摘再打（避免无意义的 remove/add，undo 也不会误摘）。"用户的"(t2)
    # 两个条件都不满足，不动。
    assert {tid for tid, _ in client.untag_calls} == {"t1"}


def test_apply_override_ignores_history_records(db):
    """历史 record 里的 marker 不算"管理标签"——只看 asset_state 当前值。
    （过期的marker 刻意留在 tags 表外：若误用历史会摘掉 t9。）"""
    _insert_tag(db, "风景", "primary")
    _insert_state(db, ASSET_ID, current_tag="风景", current_markers="[]")
    _insert_record(db, ASSET_ID, markers=json.dumps(["过期的marker"]))
    client = FakeClient(asset_tags=[_Tag("风景", "t1"), _Tag("过期的marker", "t9")])
    apply_override(db, client, ASSET_ID, "人像")
    untagged = {tid for tid, _ in client.untag_calls}
    assert untagged == {"t1"}


def test_apply_override_removes_state_markers_even_if_tag_deleted(db):
    """asset_state 当前值里的标签即使已从 tags 表删除也摘（§5.8 条件2）。"""
    _insert_tag(db, "风景", "primary")
    _insert_state(db, ASSET_ID, current_tag="风景", current_markers='["天空"]')
    client = FakeClient(asset_tags=[_Tag("风景", "t1"), _Tag("天空", "t2")])
    apply_override(db, client, ASSET_ID, "人像")
    assert {tid for tid, _ in client.untag_calls} == {"t1", "t2"}


def test_apply_override_tags_new_and_writes_changelog_null_job(db):
    _insert_tag(db, "人像", "primary")
    _insert_tag(db, "天空", "marker")
    _insert_state(db, ASSET_ID, current_tag="风景")
    client = FakeClient(asset_tags=[_Tag("风景", "t1")])
    apply_override(db, client, ASSET_ID, "人像", marker_tags=["天空"])
    assert client.ensure_tags_calls[-1] == ["人像", "天空"]
    assert client.tag_assets_calls == [(["tag-id-人像", "tag-id-天空"], [ASSET_ID])]
    logs = _change_logs(db)
    removes = [l for l in logs if l["action"] == "tag_remove"]
    adds = [l for l in logs if l["action"] == "tag_add"]
    assert [json.loads(l["payload"])["tag_name"] for l in removes] == ["风景"]
    assert {json.loads(l["payload"])["tag_name"] for l in adds} == {"人像", "天空"}
    for l in logs:
        assert l["job_id"] is None
        assert l["direction"] == "apply"


def test_apply_override_sets_state_but_not_visibility(db):
    _insert_tag(db, "人像", "primary")
    _insert_state(db, ASSET_ID, visibility="archive", last_job_id=7)
    client = FakeClient(asset_tags=[])
    apply_override(db, client, ASSET_ID, "人像")
    st = _state(db)
    assert st["manual_override"] == 1
    assert st["current_tag"] == "人像"
    assert st["current_markers"] == "[]"
    assert st["visibility"] == "archive"  # 不动可见性
    assert st["last_job_id"] == 7  # 改判不是 job，last_job_id 语义保持（见 manual.py 注释）
    assert client.tag_assets_calls  # Immich 打了新标签


def test_apply_override_inserts_state_when_missing(db):
    _insert_tag(db, "人像", "primary")
    client = FakeClient(asset_tags=[])
    apply_override(db, client, ASSET_ID, "人像")
    st = _state(db)
    assert st["manual_override"] == 1
    assert st["current_tag"] == "人像"


def test_apply_override_no_album_calls(db):
    _insert_tag(db, "人像", "primary")
    client = FakeClient(asset_tags=[])
    apply_override(db, client, ASSET_ID, "人像")
    # FakeClient 没有 ensure_album/add_to_album —— 有调用就会 AttributeError


def test_apply_override_logs_event(db):
    _insert_tag(db, "人像", "primary")
    client = FakeClient(asset_tags=[])
    apply_override(db, client, ASSET_ID, "人像")
    assert len(_events(db, "asset_manual_override")) == 1


# ------------------------------------------------------------- clear_override


def test_clear_override_only_clears_bit(db):
    _insert_tag(db, "人像", "primary")
    _insert_state(db, ASSET_ID, current_tag="风景", manual_override=1)
    client = FakeClient(asset_tags=[_Tag("风景", "t1")])
    clear_override(db, ASSET_ID)
    st = _state(db)
    assert st["manual_override"] == 0
    assert st["current_tag"] == "风景"  # 不回滚标签
    assert client.untag_calls == []  # FakeClient 从未传入 manual —— 无调用


def test_clear_override_no_row_noop(db):
    clear_override(db, "ghost")
    assert _state(db, "ghost") is None
