"""Task 6: 应用器（Applier）的测试。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.classify.apply import ApplyPlan, apply_plan
from app.db.connection import Database
from app.db.migrate import migrate


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    migrate(d)
    return d


class FakeClient:
    """记录调用的 Immich 客户端替身；fail_on 指定的方法在真正执行前抛异常。"""

    def __init__(self, visibility: str = "timeline", fail_on: str | None = None):
        self.visibility = visibility
        self.fail_on = fail_on
        self.ensure_tags_calls: list[list[str]] = []
        self.tag_assets_calls: list[tuple[list[str], list[str]]] = []
        self.untag_calls: list[tuple[str, list[str]]] = []
        self.visibility_calls: list[str] = []
        self.album_calls: list[str] = []

    def _fail(self, name: str) -> None:
        if self.fail_on == name:
            raise RuntimeError(f"injected failure: {name}")

    def ensure_tags(self, names: list[str]) -> dict[str, str]:
        self.ensure_tags_calls.append(list(names))
        return {n: f"tag-id-{n}" for n in names}

    def tag_assets(self, tag_ids: list[str], asset_ids: list[str]) -> None:
        self._fail("tag_assets")
        self.tag_assets_calls.append((list(tag_ids), list(asset_ids)))

    def untag_assets(self, tag_id: str, asset_ids: list[str]) -> None:
        self._fail("untag_assets")
        self.untag_calls.append((tag_id, list(asset_ids)))

    def set_visibility(self, asset_ids: list[str], visibility: str) -> None:
        self._fail("set_visibility")
        self.visibility_calls.append(visibility)

    def ensure_album(self, name: str) -> str:
        self._fail("ensure_album")
        self.album_calls.append(name)
        return f"album-id-{name}"

    def add_to_album(self, album_id: str, asset_ids: list[str]) -> None:
        self._fail("add_to_album")
        self.album_calls.append(album_id)

    def get_asset(self, asset_id: str):
        return SimpleNamespace(visibility=self.visibility)


def make_plan(**overrides) -> ApplyPlan:
    defaults = dict(
        asset_id="asset-1",
        primary_tag="cat",
        marker_tags=("screenshot",),
        archive=False,
        album=None,
        queue_tag="queue",
    )
    defaults.update(overrides)
    return ApplyPlan(**defaults)


def plan_and_apply(db, client, **overrides):
    plan = make_plan(**{k: v for k, v in overrides.items() if k != "manual_override"})
    apply_plan(db, client, plan, job_id=7, queue_tag_id="tag-id-queue",
               manual_override=overrides.get("manual_override", False))
    return plan


def change_rows(db, asset_id="asset-1"):
    with db.connect() as conn:
        return conn.execute(
            "SELECT action, direction, payload FROM change_log "
            "WHERE asset_id = ? ORDER BY id",
            (asset_id,),
        ).fetchall()


def state_row(db, asset_id="asset-1"):
    with db.connect() as conn:
        return conn.execute(
            "SELECT current_tag, current_markers, visibility, last_job_id "
            "FROM asset_state WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()


# 1. 执行顺序：tag_add... → archive/timeline → album_add → queue_remove（最后）
def test_action_order_queue_remove_last(db: Database) -> None:
    client = FakeClient()
    plan_and_apply(db, client, marker_tags=(), archive=True, album="Pets")
    rows = change_rows(db)
    actions = [r["action"] for r in rows]
    assert actions == ["tag_add", "archive", "album_add", "queue_remove"]
    assert actions.index("queue_remove") == len(actions) - 1


# 2. 用户自行归档的图不被拉回 timeline：
#    plan.archive=False + 无本系统 archive/apply 记录 + Immich 侧是 archive
def test_user_archived_not_restored_to_timeline(db: Database) -> None:
    client = FakeClient(visibility="archive")
    plan_and_apply(db, client, archive=False)
    assert client.visibility_calls == []


# 3. 本系统归档过的图，改判为非归档 tag 时被设回 timeline
def test_system_archived_asset_restored_to_timeline(db: Database) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
            "VALUES (1, 'asset-1', 'archive', 'apply', '{}')",
        )
    client = FakeClient(visibility="archive")
    plan_and_apply(db, client, archive=False)
    assert client.visibility_calls == ["timeline"]


# 4. manual_override=true：跳过 ②③，⑤ 的 visibility 取 Immich 当前值
def test_manual_override_skips_visibility_and_album(db: Database) -> None:
    client = FakeClient(visibility="archive")
    plan_and_apply(
        db, client, archive=True, album="Pets", manual_override=True
    )
    assert client.visibility_calls == []
    assert client.album_calls == []
    row = state_row(db)
    assert row["visibility"] == "archive"  # Immich 当前值，不是推导值


# 5. 队列标签最后才摘：摘标签失败时新标签已写入、队列标签仍在
def test_queue_tag_removed_last_injected_failure(db: Database) -> None:
    client = FakeClient(fail_on="untag_assets")
    with pytest.raises(RuntimeError, match="untag_assets"):
        plan_and_apply(db, client)
    # 新标签已经打上（不会出现"既无新标签又不在队列"）
    assert client.tag_assets_calls
    # 队列标签尚未被摘掉（调用本身失败了，没有成功记录）
    assert client.untag_calls == []


# 6. 单事务：⑤ 之前的失败（album 步）→ change_log 未落库、asset_state 保持原样
def test_single_transaction_no_partial_writes(db: Database) -> None:
    client = FakeClient(fail_on="ensure_album")
    with pytest.raises(RuntimeError, match="ensure_album"):
        plan_and_apply(db, client, archive=True, album="Pets")
    assert change_rows(db) == []
    with db.connect() as conn:
        row = conn.execute(
            "SELECT current_tag, visibility, last_job_id FROM asset_state "
            "WHERE asset_id = 'asset-1'",
        ).fetchone()
    # asset_state 没被写过（不存在行）
    assert row is None


# 7. change_log.payload 逐 action 含规定字段（完整 payload 断言）
def test_payload_fields_exact(db: Database) -> None:
    client = FakeClient(visibility="archive")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
            "VALUES (1, 'asset-1', 'archive', 'apply', '{}')",
        )
    plan_and_apply(db, client, marker_tags=(), archive=False, album="Pets")
    rows = change_rows(db)
    by_action = {}
    for r in rows:
        by_action.setdefault(r["action"], []).append(
            (r["direction"], json.loads(r["payload"]))
        )
    assert by_action["tag_add"] == [("apply", {"tag_name": "cat"})]
    assert by_action["timeline"] == [("apply", {"from_visibility": "archive"})]
    assert by_action["album_add"] == [("apply", {"album_name": "Pets"})]
    assert by_action["queue_remove"] == [
        ("apply", {"queue_tag_name": "queue"})
    ]


# 8. marker 标签有多个时全部写入
def test_all_marker_tags_written(db: Database) -> None:
    client = FakeClient()
    plan_and_apply(db, client, marker_tags=("screenshot", "meme", "low quality"))
    assert client.ensure_tags_calls == [["cat", "screenshot", "meme", "low quality"]]
    tagged = client.tag_assets_calls[0][0]
    assert sorted(tagged) == [
        "tag-id-cat", "tag-id-low quality", "tag-id-meme", "tag-id-screenshot",
    ]
    markers = json.loads(state_row(db)["current_markers"])
    assert markers == ["screenshot", "meme", "low quality"]


# 4b. manual_override 下 ⑤ 的 visibility 必须是 Immich 当前值：
#     Immich 侧是 timeline，但推导值（plan.archive=True）是 archive —— 两者不同才测得出来
def test_manual_override_visibility_uses_immich_current(db: Database) -> None:
    client = FakeClient(visibility="timeline")
    plan_and_apply(db, client, archive=True, manual_override=True)
    row = state_row(db)
    assert row["visibility"] == "timeline"


# 变异：「是否归档过」多查了 timeline 动作 —— 只有 timeline/apply 记录时
# 不算"本系统归档过"，不该拉回 timeline
def test_timeline_apply_record_does_not_trigger_restore(db: Database) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
            "VALUES (1, 'asset-1', 'timeline', 'apply', '{}')",
        )
    client = FakeClient(visibility="archive")
    plan_and_apply(db, client, archive=False)
    assert client.visibility_calls == []


# 补充：⑤ asset_state 正常更新
def test_asset_state_updated(db: Database) -> None:
    client = FakeClient()
    plan_and_apply(db, client, archive=True)
    row = state_row(db)
    assert row["current_tag"] == "cat"
    assert json.loads(row["current_markers"]) == ["screenshot"]
    assert row["visibility"] == "archive"
    assert row["last_job_id"] == 7


# 变异：⑤ 的 visibility 用推导值（manual_override 下应取 Immich 当前值）
# 已由 test_manual_override_skips_visibility_and_album 覆盖。
# 变异：② 的无条件 timeline —— 已由 test_user_archived_not_restored_to_timeline 覆盖。
# 变异：「是否归档过」查询漏 direction='apply' → 用 undo 记录应不算数
def test_undo_archive_record_does_not_count(db: Database) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
            "VALUES (1, 'asset-1', 'archive', 'undo', '{}')",
        )
    client = FakeClient(visibility="archive")
    plan_and_apply(db, client, archive=False)
    assert client.visibility_calls == []


# ------------------------------------------------------------- skip_immich

def test_skip_immich_writes_nothing_but_queue_remove(db: Database) -> None:
    """skip_immich：不打标、不进相册、不归档，只摘队列标签 + 落本地状态。"""
    client = FakeClient()
    plan_and_apply(db, client, skip_immich=True, album="Documents",
                   archive=True)

    assert client.tag_assets_calls == []       # 不打标
    assert client.album_calls == []            # 不进相册
    assert client.visibility_calls == []       # 不归档
    assert client.untag_calls == [("tag-id-queue", ["asset-1"])]  # 摘队列标签
    # change_log 只有 queue_remove
    actions = [r["action"] for r in change_rows(db)]
    assert actions == ["queue_remove"]
    # 本地状态照常记录
    st = state_row(db)
    assert st["current_tag"] == "cat"
    assert st["current_markers"] == '["screenshot"]'


def test_skip_immich_queries_current_visibility(db: Database) -> None:
    """skip_immich 仍查一次当前可见性（本地状态需要），只读不写。"""
    client = FakeClient(visibility="archive")
    plan_and_apply(db, client, skip_immich=True, archive=True)
    assert client.visibility_calls == []
    assert state_row(db)["visibility"] == "archive"  # 沿用 Immich 现状


def test_skip_immich_off_writes_normally(db: Database) -> None:
    """默认（skip_immich=False）行为不变。"""
    client = FakeClient()
    plan_and_apply(db, client, album="Documents", archive=True)
    assert client.tag_assets_calls != []
    assert client.ensure_tags_calls[0] == ["cat", "screenshot"]
    assert client.album_calls == ["Documents", "album-id-Documents"]
    assert client.visibility_calls == ["archive"]
