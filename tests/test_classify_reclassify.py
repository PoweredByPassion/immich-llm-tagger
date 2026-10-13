"""M6 Task 2: reclassify_asset 的测试。

语义（设计 §5.8）：摘"本系统管理的标签"∩ 实际拥有 → change_log(tag_remove,
job_id=本 job) → 清空 current_tag/current_markers、更新 last_job_id →
classify_asset(source="manual") 重跑；重分类显式发起不受 manual_override
限制（§5.5），apply_plan 的 manual_override=False。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.classify.reclassify as rc_module
from app.classify.pipeline import PipelineConfig, PipelineResult
from app.db.connection import Database
from app.db.migrate import migrate


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    migrate(d)
    return d


def insert_tag(db, name, kind="primary"):
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO tags (name, description, kind, enabled, archive, album) "
            "VALUES (?, '', ?, 1, 0, NULL)", (name, kind))
        return cur.lastrowid


class FakeClient:
    def __init__(self, owned_tags=("风景", "我的手打标签")):
        self.owned = list(owned_tags)
        self.untagged: list[tuple[str, list[str]]] = []

    def get_asset(self, asset_id):
        class _A:
            tags = [SimpleNamespace(name=n) for n in self.owned]
            original_file_name = f"{asset_id}.png"
            exif_info = None
            local_date_time = None
            visibility = "timeline"
        return _A()

    def ensure_tags(self, names):
        return {n: f"id-{n}" for n in names}

    def untag_assets(self, tag_id, asset_ids):
        self.untagged.append((tag_id, list(asset_ids)))

    def tag_assets(self, tag_ids, asset_ids):
        self.tagged = getattr(self, "tagged", []) + [(list(tag_ids), list(asset_ids))]

    def set_visibility(self, asset_ids, visibility):
        self.visibility_set = getattr(self, "visibility_set", []) + [(list(asset_ids), visibility)]

    def ensure_album(self, name):
        return f"album-id-{name}"

    def add_to_album(self, album_id, asset_ids):
        pass


def make_cfg():
    from app.classify.thumb import ThumbConfig
    return PipelineConfig(
        prompt_template="t {{TAG_LIST}}", tags=[],
        marker_rules=[], fallback_tag="other", default_min_confidence=0.6,
        thumb=ThumbConfig(), queue_tag_name="未分类",
        llm=None, concurrency_owner=1,
    )


def seed_state(db, asset_id, **kw):
    row = {"asset_id": asset_id, "current_tag": "风景",
           "current_markers": json.dumps(["天空"]), "visibility": "timeline",
           "manual_override": 0, "last_job_id": None}
    row.update(kw)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_state (asset_id, current_tag, current_markers, "
            "visibility, manual_override, last_job_id) VALUES (:asset_id, "
            ":current_tag, :current_markers, :visibility, :manual_override, "
            ":last_job_id)", row)


# --------------------------------------------------------------- 摘旧

def test_removes_only_intersection_of_managed_and_owned(db, monkeypatch):
    insert_tag(db, "风景")
    insert_tag(db, "天空", "marker")
    seed_state(db, "a1")
    client = FakeClient(owned_tags=("风景", "天空", "我的手打标签"))
    captured = {}

    def fake_classify(db, client, llm, asset_id, cfg, **kw):
        captured.update(kw)
        return PipelineResult(asset_id=asset_id, tag="人像", confidence=0.9,
                              markers=(), status="ok", dry_run=False)

    monkeypatch.setattr(rc_module, "classify_asset", fake_classify)
    rc_module.reclassify_asset(db, client, None, make_cfg(), "a1", job_id=7)

    # 只摘"风景/天空"，"我的手打标签"不动；tag id 用 ensure_tags 映射
    assert client.untagged == [("id-天空", ["a1"]), ("id-风景", ["a1"])]


def test_change_log_tag_remove_with_job_id(db, monkeypatch):
    insert_tag(db, "风景")
    seed_state(db, "a1")
    client = FakeClient(owned_tags=("风景",))

    monkeypatch.setattr(rc_module, "classify_asset",
                        lambda *a, **k: PipelineResult(
                            asset_id="a1", tag="x", confidence=None,
                            markers=(), status="ok", dry_run=False))
    rc_module.reclassify_asset(db, client, None, make_cfg(), "a1", job_id=7)

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT action, direction, job_id, payload FROM change_log "
            "WHERE asset_id='a1'").fetchall()
    removes = [r for r in rows if r["action"] == "tag_remove"]
    assert len(removes) == 1
    assert removes[0]["direction"] == "apply"
    assert removes[0]["job_id"] == 7
    assert json.loads(removes[0]["payload"])["tag_name"] == "风景"


def test_state_cleared_and_last_job_id_updated(db, monkeypatch):
    insert_tag(db, "风景")
    seed_state(db, "a1", manual_override=1, last_job_id=3)
    client = FakeClient(owned_tags=("风景",))
    monkeypatch.setattr(rc_module, "classify_asset",
                        lambda *a, **k: PipelineResult(
                            asset_id="a1", tag="x", confidence=None,
                            markers=(), status="ok", dry_run=False))
    rc_module.reclassify_asset(db, client, None, make_cfg(), "a1", job_id=7)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT current_tag, current_markers, last_job_id, manual_override "
            "FROM asset_state WHERE asset_id='a1'").fetchone()
    assert row["current_tag"] is None
    assert json.loads(row["current_markers"] or "[]") == []
    assert row["last_job_id"] == 7
    assert row["manual_override"] == 1  # 重分类不动该位（§5.5）


def test_classify_called_with_manual_source_and_no_override(db, monkeypatch):
    """重跑走 classify_asset：source="manual"（绕 _already_done），
    apply 的 manual_override=False（§5.5 显式发起不受限）。"""
    insert_tag(db, "风景")
    seed_state(db, "a1", manual_override=1)
    client = FakeClient(owned_tags=("风景",))
    captured = {}

    def fake_classify(db, client, llm, asset_id, cfg, **kw):
        captured.update(kw)
        return PipelineResult(asset_id=asset_id, tag="x", confidence=None,
                              markers=(), status="ok", dry_run=False)

    monkeypatch.setattr(rc_module, "classify_asset", fake_classify)
    rc_module.reclassify_asset(db, client, None, make_cfg(), "a1", job_id=7)

    assert captured["source"] == "manual"
    assert captured["job_id"] == 7
    assert captured["override_manual"] is False


def test_owned_empty_means_no_untag_no_changelog(db, monkeypatch):
    """资产上已无管理标签：不调 untag、不写 tag_remove，但仍重跑。"""
    insert_tag(db, "风景")
    seed_state(db, "a1")
    client = FakeClient(owned_tags=("我的手打标签",))
    called = []
    monkeypatch.setattr(rc_module, "classify_asset",
                        lambda *a, **k: (called.append(1),
                                         PipelineResult(
                                             asset_id="a1", tag="x",
                                             confidence=None, markers=(),
                                             status="ok", dry_run=False))[1])
    rc_module.reclassify_asset(db, client, None, make_cfg(), "a1", job_id=7)
    assert client.untagged == []
    with db.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM change_log WHERE action='tag_remove'"
        ).fetchone()["c"]
    assert n == 0
    assert called == [1]


def test_reclassify_runs_real_classify_asset_without_typeerror(
    db, monkeypatch
):
    """真实 classify_asset（含 apply 阶段）跑通；kwarg 形参不匹配必须
    TypeError（BUG-1 回归）。"""
    from app.store.connections import LLMConn
    from app.classify.thumb import ThumbConfig

    insert_tag(db, "风景")
    seed_state(db, "a1", manual_override=1)
    client = FakeClient(owned_tags=("风景",))

    cfg = PipelineConfig(
        prompt_template="t {{TAG_LIST}}", tags=[], marker_rules=[],
        fallback_tag="other", default_min_confidence=0.6,
        thumb=ThumbConfig(), queue_tag_name="未分类",
        llm=LLMConn(base_url="http://llm", api_key="k", model="m",
                    timeout=30, multimodal=False),
        concurrency_owner=1,
    )
    llm = SimpleNamespace(
        chat=lambda messages: SimpleNamespace(
            content='{"tag": "风景", "confidence": 0.9, '
                    '"sensitive": false, "reason": "ok"}',
            model="m", usage=None),
    )
    result = rc_module.reclassify_asset(db, client, llm, cfg, "a1", job_id=7)
    assert result.status == "ok"  # 不再 TypeError

    # apply 阶段真的执行了（未被 except 吞掉）：change_log 有本 job 的 tag_add
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT action FROM change_log WHERE job_id = 7 "
            "AND action IN ('tag_add', 'queue_remove')").fetchall()
    assert any(r["action"] == "tag_add" for r in rows)
