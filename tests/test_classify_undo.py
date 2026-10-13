"""M5 Task 3: 回滚（undo）核心逻辑的测试。mock Immich client，纯单测。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.classify.undo import UndoSession
from app.clients.immich import ImmichError
from app.db.connection import Database
from app.db.migrate import migrate


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    migrate(d)
    return d


class FakeClient:
    """记录调用的 Immich 客户端替身。untag_fail_tag 指定的 tag id 摘标时抛 404。"""

    def __init__(self, existing_tags: tuple[str, ...] = ("cat", "screenshot"),
                 untag_fail_tag: str | None = None):
        self._next = 0
        self.tags: dict[str, str] = {n: self._next_id() for n in existing_tags}
        self.untag_fail_tag = untag_fail_tag
        self.untag_calls: list[tuple[str, str]] = []       # (tag_id, asset_id)
        self.tag_calls: list[tuple[str, str]] = []          # (tag_id, asset_id)
        self.visibility_calls: list[tuple[str, str]] = []   # (asset_id, visibility)
        self.remove_album_calls: list[tuple[str, str]] = [] # (album_id, asset_id)
        self.add_album_calls: list[tuple[str, str]] = []

    def _next_id(self) -> str:
        self._next += 1
        return f"tag-id-{self._next}"

    def list_tags(self):
        from types import SimpleNamespace
        return [SimpleNamespace(name=n, id=i) for n, i in self.tags.items()]

    def ensure_tags(self, names: list[str]) -> dict[str, str]:
        for n in names:
            if n not in self.tags:
                self.tags[n] = self._next_id()
        return {n: self.tags[n] for n in names}

    def tag_assets(self, tag_ids: list[str], asset_ids: list[str]) -> None:
        for t in tag_ids:
            for a in asset_ids:
                self.tag_calls.append((t, a))

    def untag_assets(self, tag_id: str, asset_ids: list[str]) -> None:
        for a in asset_ids:
            if tag_id == self.untag_fail_tag:
                raise ImmichError("not found", status_code=404)
            self.untag_calls.append((tag_id, a))

    def set_visibility(self, asset_ids: list[str], visibility: str) -> None:
        for a in asset_ids:
            self.visibility_calls.append((a, visibility))

    def ensure_album(self, name: str) -> str:
        return f"album-id-{name}"

    def add_to_album(self, album_id: str, asset_ids: list[str]) -> None:
        for a in asset_ids:
            self.add_album_calls.append((album_id, a))

    def remove_from_album(self, album_id: str, asset_ids: list[str]) -> None:
        for a in asset_ids:
            self.remove_album_calls.append((album_id, a))


def seed_apply_log(db: Database, job_id: int, rows: list[tuple[str, str, dict]]) -> None:
    """按给定顺序写 apply change_log（id 递增 = 应用顺序）。"""
    with db.connect() as conn:
        for action, asset, payload in rows:
            conn.execute(
                "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
                "VALUES (?, ?, ?, 'apply', ?)",
                (job_id, asset, action, json.dumps(payload)),
            )


def seed_state(db: Database, asset_id: str, *, tag: str | None, markers: list[str],
               visibility: str | None, manual_override: int = 0) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_state (asset_id, current_tag, current_markers, "
            "visibility, manual_override) VALUES (?, ?, ?, ?, ?)",
            (asset_id, tag, json.dumps(markers), visibility, manual_override),
        )


def undo_log(db: Database, undo_job_id: int):
    with db.connect() as conn:
        return conn.execute(
            "SELECT action, asset_id, payload FROM change_log "
            "WHERE job_id = ? AND direction = 'undo' ORDER BY id",
            (undo_job_id,),
        ).fetchall()


def state_row(db: Database, asset_id: str):
    with db.connect() as conn:
        return conn.execute(
            "SELECT current_tag, current_markers, visibility, manual_override "
            "FROM asset_state WHERE asset_id = ?", (asset_id,),
        ).fetchone()


# 1. 逆序撤销：apply 是 tag_add(primary) → tag_add(marker) → archive → album_add
#    → queue_remove；撤销的 Immich 调用顺序必须反过来（queue 先恢复，标签最后摘）
def test_undo_in_reverse_order(db: Database) -> None:
    seed_apply_log(db, 1, [
        ("tag_add", "a-1", {"tag_name": "cat"}),
        ("tag_add", "a-1", {"tag_name": "screenshot"}),
        ("archive", "a-1", {"from_visibility": "timeline"}),
        ("album_add", "a-1", {"album_name": "Pets"}),
        ("queue_remove", "a-1", {"queue_tag_name": "未分类"}),
    ])
    client = FakeClient()
    s = UndoSession(db, client, undo_job_id=2)
    rows = s.load_rows(1)
    assert [r["action"] for r in rows] == [
        "queue_remove", "album_add", "archive", "tag_add", "tag_add",
    ]
    for r in rows:
        assert s.undo_row(r) is True
    s.flush_state()
    # 恢复队列标签在最前（tag_calls[0]），摘标签在最后（untag_calls[-1]）
    queue_id = client.tags["未分类"]
    assert client.tag_calls[0] == (queue_id, "a-1")
    assert client.remove_album_calls == [("album-id-Pets", "a-1")]
    assert client.visibility_calls == [("a-1", "timeline")]
    untagged = {t for t, _ in client.untag_calls}
    assert untagged == {client.tags["cat"], client.tags["screenshot"]}


# 2. 撤销记录：direction=undo、job_id=undo job、action 用逆操作名、payload 契约
def test_undo_records_written_with_inverse_actions(db: Database) -> None:
    seed_apply_log(db, 1, [
        ("tag_add", "a-1", {"tag_name": "cat"}),
        ("archive", "a-1", {"from_visibility": "timeline"}),
        ("album_add", "a-1", {"album_name": "Pets"}),
        ("queue_remove", "a-1", {"queue_tag_name": "未分类"}),
    ])
    client = FakeClient()
    s = UndoSession(db, client, undo_job_id=2)
    for r in s.load_rows(1):
        s.undo_row(r)
    rows = undo_log(db, 2)
    by_action = {r["action"]: json.loads(r["payload"]) for r in rows}
    assert by_action["queue_restore"] == {"queue_tag_name": "未分类"}
    assert by_action["album_remove"] == {"album_name": "Pets"}
    # 撤销 archive → timeline；from_visibility 是恢复前的值（archive 设置后的值）
    assert by_action["timeline"] == {"from_visibility": "archive"}
    assert by_action["tag_remove"] == {"tag_name": "cat"}
    assert all(r["asset_id"] == "a-1" for r in rows)


# 3. direction 过滤：目标 job 的 undo 记录不重撤（也不计入 total）
def test_direction_filter_only_apply_rows(db: Database) -> None:
    seed_apply_log(db, 1, [("tag_add", "a-1", {"tag_name": "cat"})])
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
            "VALUES (1, 'a-1', 'queue_remove', 'undo', '{\"queue_tag_name\": \"q\"}')",
        )
    client = FakeClient()
    s = UndoSession(db, client, undo_job_id=2)
    rows = s.load_rows(1)
    assert len(rows) == 1
    assert rows[0]["action"] == "tag_add"
    for r in rows:
        s.undo_row(r)
    # 只有 tag_add 被撤销；queue 没被动过（tag_calls 为空）
    assert client.tag_calls == []


# 4. 404 跳过：不中断后续步、不写该步的 undo 记录、undo_row 返回 False
def test_404_skip_no_record_but_continues(db: Database) -> None:
    seed_apply_log(db, 1, [
        ("tag_add", "a-1", {"tag_name": "gone"}),
        ("tag_add", "a-1", {"tag_name": "cat"}),
    ])
    client = FakeClient(untag_fail_tag="tag-id-gone")
    # gone 不在 Immich 里也行（按名查不到同样是 404 路径）；这里用 fail 注入
    client.tags.pop("gone", None)
    s = UndoSession(db, client, undo_job_id=2)
    rows = s.load_rows(1)
    # 逆序：后写入的 cat（id 更大）先撤；gone 404 跳过
    results = [s.undo_row(r) for r in rows]
    assert results == [True, False]
    undone = undo_log(db, 2)
    assert [(r["action"], json.loads(r["payload"])["tag_name"]) for r in undone] == [
        ("tag_remove", "cat"),
    ]


# 5. 按名查不到 tag 也走 404 跳过（tag 已被用户在 Immich 删除）
def test_missing_tag_name_skips(db: Database) -> None:
    seed_apply_log(db, 1, [("tag_add", "a-1", {"tag_name": "gone"})])
    client = FakeClient()  # "gone" 不在 existing_tags
    s = UndoSession(db, client, undo_job_id=2)
    assert s.undo_row(s.load_rows(1)[0]) is False
    assert undo_log(db, 2) == []


# 6. asset_state 回写：current_tag 清空、marker 摘除、visibility 恢复、
#    manual_override 不被覆盖
def test_asset_state_written_back(db: Database) -> None:
    seed_apply_log(db, 1, [
        ("tag_add", "a-1", {"tag_name": "cat"}),
        ("tag_add", "a-1", {"tag_name": "screenshot"}),
        ("archive", "a-1", {"from_visibility": "timeline"}),
    ])
    seed_state(db, "a-1", tag="cat", markers=["screenshot"],
               visibility="archive", manual_override=1)
    client = FakeClient()
    s = UndoSession(db, client, undo_job_id=2)
    for r in s.load_rows(1):
        s.undo_row(r)
    s.flush_state()
    row = state_row(db, "a-1")
    assert row["current_tag"] is None
    assert json.loads(row["current_markers"]) == []
    assert row["visibility"] == "timeline"
    assert row["manual_override"] == 1
    assert all(a == "a-1" for t, a in client.untag_calls)


# 7. 只撤销 tag 部分的 job：visibility 保持原值不动
def test_state_visibility_untouched_without_visibility_rows(db: Database) -> None:
    seed_apply_log(db, 1, [("tag_add", "a-1", {"tag_name": "cat"})])
    seed_state(db, "a-1", tag="cat", markers=[], visibility="archive")
    client = FakeClient()
    s = UndoSession(db, client, undo_job_id=2)
    for r in s.load_rows(1):
        s.undo_row(r)
    s.flush_state()
    row = state_row(db, "a-1")
    assert row["current_tag"] is None
    assert row["visibility"] == "archive"


# 8. 回滚可再回滚：undo job 自身产出的 tag_remove / album_remove /
#    queue_restore 被逆向重做（tag_add / album_add / queue_remove）
def test_undo_of_undo_actions_is_re_undoable(db: Database) -> None:
    seed_apply_log(db, 1, [
        ("tag_remove", "a-1", {"tag_name": "cat"}),
        ("album_remove", "a-1", {"album_name": "Pets"}),
        ("queue_restore", "a-1", {"queue_tag_name": "未分类"}),
        ("timeline", "a-1", {"from_visibility": "archive"}),
    ])
    client = FakeClient()
    s = UndoSession(db, client, undo_job_id=2)
    for r in s.load_rows(1):
        assert s.undo_row(r) is True
    assert (client.tags["cat"], "a-1") in client.tag_calls
    assert ("album-id-Pets", "a-1") in client.add_album_calls
    assert ("a-1", "archive") in client.visibility_calls
    assert (client.tags["未分类"], "a-1") in client.untag_calls
    actions = {r["action"] for r in undo_log(db, 2)}
    assert actions == {"tag_add", "album_add", "queue_remove", "archive"}


# 9. 撤销 timeline 记录：恢复为 from_visibility（archive）
def test_undo_timeline_restores_archive(db: Database) -> None:
    seed_apply_log(db, 1, [("timeline", "a-1", {"from_visibility": "archive"})])
    client = FakeClient()
    s = UndoSession(db, client, undo_job_id=2)
    s.undo_row(s.load_rows(1)[0])
    s.flush_state()
    assert client.visibility_calls == [("a-1", "archive")]
    assert state_row(db, "a-1")["visibility"] == "archive"
