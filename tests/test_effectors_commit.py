"""Committer：分桶、Unset 语义、逐桶立即记账、Finalizer 闸门。

本文件自带 `db`夹具与 `FakeClient`，与 tests/test_classify_pipeline.py 同风格
（仓库没有 conftest.py，每个测试文件管自己的夹具）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.classify.commit import commit_effects
from app.classify.effectors.base import Effect, Gates
from app.clients.immich import ImmichError
from app.db.connection import Database
from app.db.migrate import migrate


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    migrate(d)
    return d


class FakeClient:
    """记录物理调用，可用 fail_on 注入指定方法的失败。"""

    def __init__(self) -> None:
        self.write_calls: list[tuple] = []
        self.set_props_calls: list[dict] = []
        self.fail_on: str | None = None

    def _maybe_fail(self, name: str) -> None:
        if self.fail_on == name:
            raise ImmichError(f"injected {name} failure")

    def ensure_tags(self, names):
        self.write_calls.append(("ensure_tags", list(names)))
        return {n: f"tag-id-{n}" for n in names}

    def tag_assets(self, tag_ids, asset_ids):
        self.write_calls.append(("tag_assets", list(tag_ids), list(asset_ids)))
        self._maybe_fail("tag_assets")

    def untag_assets(self, tag_id, asset_ids):
        self.write_calls.append(("untag_assets", tag_id, list(asset_ids)))
        self._maybe_fail("untag_assets")

    def ensure_album(self, name):
        self.write_calls.append(("ensure_album", name))
        return f"album-id-{name}"

    def add_to_album(self, album_id, asset_ids):
        self.write_calls.append(("add_to_album", album_id, list(asset_ids)))
        self._maybe_fail("add_to_album")

    def set_asset_properties(self, asset_ids, **props):
        self.write_calls.append(("set_asset_properties", list(asset_ids), props))
        self.set_props_calls.append(props)
        self._maybe_fail("set_asset_properties")


@pytest.fixture()
def fake_client() -> FakeClient:
    return FakeClient()


def _effects():
    """注意：**没有** queue 的 Effect —— 队列是 Finalizer 的职责（§5.6）。"""
    return [
        Effect("tag_add", "a1", {"tag_name": "cat"}),
        Effect("rating_set", "a1", {"old": None, "new": 4}),
        Effect("description_set", "a1", {"old": None, "new": "一只猫"}),
    ]


def _commit(db, client, *, effects=None, **over):
    """effects=None 才是"用默认那组"；显式传 effects=[] 表示"一个 Effect 都没有"
    （`effects or _effects()` 会把空列表当成 None —— 别那么写）。"""
    kw = dict(asset_id="a1", job_id=1, queue_tag_id="q1",
              queue_tag_name="未分类", gates=Gates())
    kw.update(over)
    return commit_effects(db, client,
                          _effects() if effects is None else effects, **kw)


def _actions(db, asset_id="a1") -> list[str]:
    with db.connect() as conn:
        return [r["action"] for r in conn.execute(
            "SELECT action FROM change_log WHERE asset_id = ? ORDER BY id",
            (asset_id,))]


def test_asset_props_merge_into_one_request(db, fake_client):
    res = _commit(db, fake_client)
    assert fake_client.set_props_calls == [{"rating": 4, "description": "一只猫"}]
    assert res.ok


def test_ledger_is_written_per_bucket_not_at_end(db, fake_client):
    """属性桶成功、标签桶失败 → 属性写入必须已有账。

    账丢了就判不出"这个值是不是我们写的"（§5.8.1），该写入也无法 Undo/Reset。
    因为属性桶排在标签桶**之前**（_BUCKET_ORDER），这个断言才钉得住时序。
    """
    fake_client.fail_on = "tag_assets"
    res = _commit(db, fake_client)
    assert not res.ok and res.failed_bucket == "tags"
    assert {"rating_set", "description_set"} <= set(_actions(db))
    assert "queue_remove" not in _actions(db)      # Finalizer 未执行


def test_finalizer_runs_only_on_full_success(db, fake_client):
    res = _commit(db, fake_client)
    assert res.ok
    actions = _actions(db)
    assert actions[-1] == "queue_remove"           # 永远最后
    assert ("untag_assets", "q1", ["a1"]) in fake_client.write_calls


def test_finalizer_failure_is_reported(db, fake_client):
    fake_client.fail_on = "untag_assets"
    res = _commit(db, fake_client)
    assert not res.ok and res.failed_bucket == "queue"
    assert "queue_remove" not in _actions(db)


def test_finalizer_runs_even_with_no_effects(db, fake_client):
    """skip_immich：Effects 为空，但队列仍必须摘掉（否则图卡在队列里）。"""
    res = _commit(db, fake_client, effects=[])
    assert res.ok
    assert _actions(db) == ["queue_remove"]


def test_commit_result_reports_only_committed(db, fake_client):
    """`committed` 只含**真正落地**的 Effect —— 管线靠它写本地镜像。"""
    fake_client.fail_on = "tag_assets"
    res = _commit(db, fake_client)
    assert {e.action for e in res.committed} == {"rating_set", "description_set"}

    fake_client.fail_on = None
    ok = _commit(db, fake_client, job_id=2)
    assert {e.action for e in ok.committed} == {
        "tag_add", "rating_set", "description_set", "queue_remove"}


def test_queue_effect_is_not_an_effect_action():
    """queue_remove 没有 plannable 桶 —— 传进来应直接失败，而不是静默丢掉。"""
    with pytest.raises(ValueError):
        Effect("queue_remove", "a1", {}).bucket


def test_dry_run_writes_nothing(db, fake_client):
    res = _commit(db, fake_client, gates=Gates(dry_run=True))
    assert fake_client.write_calls == [] and res.ok is False
    assert _actions(db) == []
