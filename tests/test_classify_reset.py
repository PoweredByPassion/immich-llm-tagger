"""M6 Task 3: reset_ops 的测试（§5.7）。

- preview：unarchive 数 = change_log archive/apply 的 distinct asset 数；
  标签集 = §5.8 管理标签 ∩ Immich list_tags 实际存在。
- run_reset_steps：unarchive（404 WARN 跳过）→ 删标签对象 → 清 asset_state
  （change_log / jobs / asset_records 保留）。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.classify.reset_ops as ro
from app.clients.immich import ImmichError
from app.db.connection import Database
from app.db.migrate import migrate


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    migrate(d)
    return d


def insert_tag(db, name, kind="primary"):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO tags (name, description, kind, enabled, archive, album) "
            "VALUES (?, '', ?, 1, 0, NULL)", (name, kind))


def seed_state(db, asset_id, **kw):
    row = {"asset_id": asset_id, "current_tag": "风景",
           "current_markers": json.dumps(["天空"]), "visibility": "timeline",
           "manual_override": 0, "rating": None, "description": None}
    row.update(kw)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_state (asset_id, current_tag, current_markers, "
            "visibility, manual_override, rating, description) "
            "VALUES (:asset_id, :current_tag, :current_markers, :visibility, "
            ":manual_override, :rating, :description)", row)


def seed_change(db, asset_id, action="archive", direction="apply"):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
            "VALUES (1, ?, ?, ?, '{}')", (asset_id, action, direction))


def seed_tag_change(db, asset_id, tag_name, direction="apply", job_id=None):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
            "VALUES (?, ?, 'tag_add', ?, ?)",
            (job_id, asset_id, direction, json.dumps({"tag_name": tag_name})),
        )


def _undo_rows(db):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT action, payload FROM change_log WHERE direction='undo'")]


class FakeClient:
    def __init__(self, tag_names=(), albums=None):
        self.tag_names = list(tag_names)
        self.archived: list[list[str]] = []
        self.deleted: list[str] = []
        self.untagged: list[tuple[str, list[str]]] = []
        self.albums: dict[str, str] = dict(albums or {})   # 相册名 → id
        self.current_rating: dict[str, int] = {}           # asset_id → 评分
        self.current_description: dict[str, str] = {}
        self.set_props_calls: list[dict] = []
        self.removed_from_album: list[tuple[str, str]] = []  # (album_id, asset_id)
        self.get_asset_calls = 0
        self.missing_assets: set[str] = set()               # 已被用户在 Immich 删除

    def list_tags(self):
        return [SimpleNamespace(name=n, id=f"id-{n}") for n in self.tag_names]

    def set_visibility(self, asset_ids, visibility):
        self.archived.append((list(asset_ids), visibility))

    def delete_tag(self, tag_id):
        self.deleted.append(tag_id)

    def untag_assets(self, tag_id, asset_ids):
        self.untagged.append((tag_id, list(asset_ids)))

    def list_albums(self):
        from types import SimpleNamespace
        # _remove_from_albums 用 a.album_name / str(a.id)
        return [SimpleNamespace(album_name=n, id=i) for n, i in self.albums.items()]

    def remove_from_album(self, album_id, asset_ids):
        for a in asset_ids:
            self.removed_from_album.append((album_id, a))

    def set_asset_properties(self, asset_ids, **props):
        for a in asset_ids:
            self.set_props_calls.append({"asset_id": a, **props})

    def get_asset(self, asset_id):
        from types import SimpleNamespace
        if asset_id in self.missing_assets:
            raise ImmichError("404", status_code=404)
        self.get_asset_calls += 1
        return SimpleNamespace(exif_info=SimpleNamespace(
            rating=self.current_rating.get(asset_id),
            description=self.current_description.get(asset_id)))


# --------------------------------------------------------------- preview

def test_preview_counts(db):
    insert_tag(db, "风景")
    insert_tag(db, "天空", "marker")
    seed_state(db, "a1")
    seed_state(db, "a2", current_tag=None, current_markers="[]")
    seed_change(db, "a1")
    seed_change(db, "a1")  # 同 asset 两条 archive → distinct 只算 1
    seed_change(db, "a3")
    client = FakeClient(tag_names=("风景", "天空", "用户自有", "不在系统里"))

    unarchive_count, tag_names = ro.reset_preview(db, client)
    assert unarchive_count == 2  # a1, a3
    # 管理标签（tags 表）∩ 实际存在 = {风景, 天空}；用户自有不在 tags 表
    assert sorted(tag_names) == ["天空", "风景"]


def test_preview_includes_state_only_names_that_exist(db):
    """current_tag 不在 tags 表但 Immich 上存在 → 也算管理标签（§5.8）。"""
    seed_state(db, "a1", current_tag="孤儿标签")
    client = FakeClient(tag_names=("孤儿标签",))
    _, names = ro.reset_preview(db, client)
    assert names == ["孤儿标签"]


def test_preview_empty(db):
    assert ro.reset_preview(db, FakeClient()) == (0, [])


# ------------------------------------------------------------ run_reset_steps

def test_run_unarchives_untags_and_clears_state(db):
    insert_tag(db, "风景")
    insert_tag(db, "天空", "marker")
    seed_state(db, "a1")
    seed_state(db, "a2")
    seed_change(db, "a1")
    seed_change(db, "a2", action="archive")
    seed_change(db, "a9", action="tag_add")  # 非 archive 不参与 unarchive
    client = FakeClient(tag_names=("风景", "天空"))

    with db.connect() as conn:
        before_records = 0
    steps = ro.run_reset_steps(db, client, job_id=1,
                               is_canceled=lambda: False)

    assert client.archived == [(["a1", "a2"], "timeline")]
    assert client.deleted == []
    # 无 tag_add change_log → 不摘任何资产（seed_change 只写了 archive）
    assert client.untagged == []
    with db.connect() as conn:
        n_state = conn.execute(
            "SELECT COUNT(*) c FROM asset_state").fetchone()["c"]
        n_change = conn.execute(
            "SELECT COUNT(*) c FROM change_log").fetchone()["c"]
    assert n_state == 0
    assert n_change == 3  # change_log 保留
    assert steps["unarchived"] == 2 and steps["untagged_tags"] == 0


def test_run_unarchive_404_skipped_with_warn(db):
    seed_change(db, "a1")
    client = FakeClient(tag_names=())

    def boom(asset_ids, visibility):
        client.archived.append((list(asset_ids), visibility))
        raise ImmichError("404", status_code=404)

    client.set_visibility = boom
    steps = ro.run_reset_steps(db, client, job_id=1, is_canceled=lambda: False)
    assert steps["unarchived"] == 0  # 404 跳过，不抛
    with db.connect() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM asset_state").fetchone()["c"]
    assert n == 0  # asset_state 仍清空


def test_run_cancel_between_steps(db):
    """取消在步骤之间：unarchive 后取消 → 不摘标签，不进 finish 步。"""
    insert_tag(db, "风景")
    seed_change(db, "a1")
    client = FakeClient(tag_names=("风景",))
    calls = {"n": 0}

    def cancel_after_first():
        calls["n"] += 1
        return calls["n"] > 1

    steps = ro.run_reset_steps(db, client, job_id=1,
                               is_canceled=cancel_after_first)
    assert client.archived == [(["a1"], "timeline")]
    assert client.untagged == []
    assert steps.get("canceled") is True


def test_run_no_events_only_progress(db):
    """asset_records 不被清（历史保留）。"""
    with db.connect() as conn:
        conn.execute("INSERT INTO asset_records (asset_id, source, status, "
                     "dry_run) VALUES ('a1', 'poll', 'ok', 0)")
    client = FakeClient(tag_names=())
    ro.run_reset_steps(db, client, job_id=1, is_canceled=lambda: False)
    with db.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM asset_records").fetchone()["c"]
    assert n == 1


def test_run_untags_only_system_tagged_assets(db):
    """SEC-1：reset 摘标只作用于本系统打过标的资产，不删标签实体。

    a1/a2 由本系统打过"风景"；change_log 里没有 a3 —— reset 后
    a1/a2 被摘、"风景"标签本身仍在 Immich（delete_tag 不被调用）。
    """
    insert_tag(db, "风景")
    seed_tag_change(db, "a1", "风景")
    seed_tag_change(db, "a2", "风景")
    client = FakeClient(tag_names=("风景",))

    steps = ro.run_reset_steps(db, client, job_id=9,
                               is_canceled=lambda: False)

    assert client.deleted == []  # 不再删除标签实体
    # 摘标按 (tag_id, [asset_ids]) 批量；只摘 change_log 记录的资产
    assert client.untagged == [("id-风景", ["a1", "a2"])]
    assert steps["untagged_tags"] == 1
    assert steps["deleted_tags"] == 0  # key 保留（恒 0），供 job_complete 事件兼容


def test_run_untag_writes_tag_remove_changelog(db):
    """摘除写 change_log(tag_remove, apply, job_id=reset job)。"""
    insert_tag(db, "风景")
    seed_tag_change(db, "a1", "风景")
    client = FakeClient(tag_names=("风景",))
    ro.run_reset_steps(db, client, job_id=9, is_canceled=lambda: False)

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT action, direction, job_id, payload FROM change_log "
            "WHERE action = 'tag_remove'").fetchall()
    assert len(rows) == 1
    assert rows[0]["direction"] == "apply"
    assert rows[0]["job_id"] == 9
    assert json.loads(rows[0]["payload"]) == {"tag_name": "风景"}


def test_run_no_tag_add_log_means_no_untag(db):
    """标签在管理集合里但 change_log 无 tag_add 记录 → 不摘任何资产。"""
    insert_tag(db, "风景")
    # 只 seed_state（current_tag=风景），无 tag_add change_log：
    seed_state(db, "a1")
    client = FakeClient(tag_names=("风景",))
    steps = ro.run_reset_steps(db, client, job_id=9,
                               is_canceled=lambda: False)
    assert client.untagged == []
    assert steps["untagged_tags"] == 0


def test_run_untag_keeps_assets_without_tag_add_record(db):
    """有 tag_add 记录但同一标签还曾在别的资产上被手工处理——
    只摘有 tag_add 记录的资产，不扩大范围。"""
    insert_tag(db, "风景")
    seed_tag_change(db, "a1", "风景")
    # a9 有 tag_add 但 payload 无 tag_name（异常记录）→ 不误摘
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
            "VALUES (NULL, 'a9', 'tag_add', 'apply', '{}')"
        )
    client = FakeClient(tag_names=("风景",))
    ro.run_reset_steps(db, client, job_id=9, is_canceled=lambda: False)
    # 简化口径：tag_add(apply) 且 payload.tag_name 命中才摘；a9 的 payload
    # 无 tag_name → 不进名单（不改写测试断言以外的行为）
    assert client.untagged == [("id-风景", ["a1"])]


# ------------------------------------------------------------ 多维度重置（D12/D13）

def test_partial_reset_only_touches_selected_dimension(db):
    """只勾「评分」：其他维度（归档 / 标签 / 描述 / 状态列）一律不动（D12）。"""
    insert_tag(db, "风景")
    seed_state(db, "a1", rating=4, description="旧描述")
    seed_change(db, "a1", action="archive")            # 有归档史
    seed_tag_change(db, "a1", "风景")                   # 有打标史
    client = FakeClient(tag_names=("风景",))

    ro.run_reset_steps(db, client, job_id=1, is_canceled=lambda: False,
                       dimensions={"rating"})

    assert client.archived == []                       # 未勾「分类」→ 不 unarchive
    assert client.untagged == []                       # 未勾「标签」→ 不摘标
    with db.connect() as conn:
        row = conn.execute("SELECT current_tag, visibility, rating, description "
                           "FROM asset_state WHERE asset_id='a1'").fetchone()
    assert row["rating"] is None                       # 被清
    assert row["description"] == "旧描述"               # 未勾「描述」→ 必须原样保留
    assert row["current_tag"] == "风景"                 # 保留
    assert row["visibility"] == "timeline"              # 保留


def test_full_dimensions_still_wipes_the_table(db):
    """四个维度全勾 → 才走整表 DELETE（等价改动前的行为）。"""
    seed_state(db, "a1")
    seed_state(db, "a2")
    ro.run_reset_steps(db, FakeClient(tag_names=()), job_id=1,
                       is_canceled=lambda: False,
                       dimensions={"tag", "classification", "rating", "description"})
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM asset_state").fetchone()["c"] == 0


def test_classification_dimension_removes_from_albums(db):
    """D13：「分类」= 归档 + 相册，两半都要做。"""
    seed_change(db, "a1", action="archive")
    seed_change(db, "a2", action="album_add")
    with db.connect() as conn:
        conn.execute("UPDATE change_log SET payload='{\"album_name\": \"Pets\"}' "
                     "WHERE asset_id='a2'")
    client = FakeClient(tag_names=(), albums={"Pets": "album-id-Pets"})

    steps = ro.run_reset_steps(db, client, job_id=1, is_canceled=lambda: False,
                               dimensions={"classification"})

    assert client.archived == [(["a1"], "timeline")]
    assert client.removed_from_album == [("album-id-Pets", "a2")]
    assert steps["removed_from_album"] == 1


def test_reset_rating_restores_old_but_skips_user_edited(db):
    """§5.10 安全阀：change_log 记我们写了 4，Immich 现在是 2（用户改过）→ 不覆盖。"""
    seed_change(db, "a1", action="rating_set")
    seed_change(db, "a2", action="rating_set")
    with db.connect() as conn:
        conn.execute("UPDATE change_log SET payload='{\"old\": null, \"new\": 4}' "
                     "WHERE asset_id='a1'")
        conn.execute("UPDATE change_log SET payload='{\"old\": null, \"new\": 4}' "
                     "WHERE asset_id='a2'")
    client = FakeClient(tag_names=())
    client.current_rating = {"a1": 4, "a2": 2}          # a2 被用户改成 2

    steps = ro.run_reset_steps(db, client, job_id=1, is_canceled=lambda: False,
                               dimensions={"rating"})

    assert client.set_props_calls == [{"asset_id": "a1", "rating": None}]
    assert steps["restored_metadata"] == 1
    (row,) = [r for r in _undo_rows(db) if r["action"] == "rating_set"]
    assert json.loads(row["payload"]) == {"old": 4, "new": None}


def test_reset_restore_skips_deleted_asset(db):
    """§5.10 404 容错：change_log 引用的资产已被用户在 Immich 删除 →
    只跳过该资产，不让整个重置中途失败（镜像 unarchive 的 404 处理）。"""
    seed_change(db, "a1", action="rating_set")
    seed_change(db, "a2", action="rating_set")
    with db.connect() as conn:
        conn.execute("UPDATE change_log SET payload='{\"old\": null, \"new\": 4}' "
                     "WHERE asset_id='a1'")
        conn.execute("UPDATE change_log SET payload='{\"old\": null, \"new\": 4}' "
                     "WHERE asset_id='a2'")
    client = FakeClient(tag_names=())
    client.current_rating = {"a2": 4}          # a2 仍在且值等于我们写入的 4
    client.missing_assets = {"a1"}             # a1 已被用户删除 → get_asset 404

    steps = ro.run_reset_steps(db, client, job_id=1, is_canceled=lambda: False,
                               dimensions={"rating"})

    assert client.set_props_calls == [{"asset_id": "a2", "rating": None}]
    assert steps["restored_metadata"] == 1


def test_reset_preview_counts_are_local_only(db):
    """预览必须是纯本地 SQL：函数签名里连 client 都没有，结构上就不发请求（§5.10）。"""
    import inspect

    seed_change(db, "a1", action="rating_set")
    seed_change(db, "a1", action="description_set")
    seed_change(db, "a2", action="rating_set")
    seed_change(db, "a3", action="archive")
    seed_change(db, "a4", action="tag_add")

    assert "client" not in inspect.signature(ro.reset_preview_counts).parameters
    assert ro.reset_preview_counts(db) == {
        "tag": 1, "classification": 1, "rating": 2, "description": 1}
