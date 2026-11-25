"""Reclassification keeps previous edits until a valid fresh result is ready."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.classify.reclassify as rc_module
from app.classify.pipeline import PipelineConfig, PipelineResult
from app.classify.reclassify import reclassify_asset
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
    def __init__(self, owned_tags=("风景", "我的手打标签"), rating=None):
        self.owned = list(owned_tags)
        self.tag_ids: dict[str, str] = {}
        self.rating = rating                       # get_asset 暴露的"当前评分"
        self.untagged: list[tuple[str, list[str]]] = []
        self.set_props_calls: list[dict] = []

    def get_asset(self, asset_id):
        class _A:
            tags = [SimpleNamespace(name=n, id=self.tag_ids.get(n, f"id-{n}")) for n in self.owned]
            original_file_name = f"{asset_id}.png"
            exif_info = SimpleNamespace(rating=self.rating, description=None)
            local_date_time = None
            visibility = "timeline"
        return _A()

    def list_asset_albums(self, asset_id):
        return []

    def ensure_tags(self, names):
        return {n: self.tag_ids.get(n, f"id-{n}") for n in names}

    def untag_assets(self, tag_id, asset_ids):
        self.untagged.append((tag_id, list(asset_ids)))
        self.owned = [n for n in self.owned if self.tag_ids.get(n, f"id-{n}") != tag_id]

    def tag_assets(self, tag_ids, asset_ids):
        self.tagged = getattr(self, "tagged", []) + [(list(tag_ids), list(asset_ids))]
        self.owned.extend(tag_id.removeprefix("id-") for tag_id in tag_ids)

    def set_visibility(self, asset_ids, visibility):
        self.visibility_set = getattr(self, "visibility_set", []) + [(list(asset_ids), visibility)]

    def ensure_album(self, name):
        return f"album-id-{name}"

    def add_to_album(self, album_id, asset_ids):
        pass

    def set_asset_properties(self, asset_ids, **props):
        for a in asset_ids:
            self.set_props_calls.append({"asset_id": a, **props})


def make_cfg(*names):
    from app.classify.thumb import ThumbConfig
    from app.store.connections import LLMConn
    from app.store.tags import TagDef
    tags = [TagDef(id=i, name=name, kind="primary", enabled=True, description="",
                   archive=False, album=None, min_confidence=None)
            for i, name in enumerate(names or ("风景", "人像"), 1)]
    return PipelineConfig(
        prompt_template="t {{TAG_LIST}}", tags=tags,
        marker_rules=[], fallback_tag="other", default_min_confidence=0.6,
        thumb=ThumbConfig(), queue_tag_name="未分类",
        llm=LLMConn(base_url="http://llm", api_key="k", model="m",
                    timeout=30, multimodal=False),
        concurrency_owner=1,
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

def test_reclassify_with_active_lease_keeps_existing_tags(db):
    from app.classify.lease import claim

    insert_tag(db, "风景")
    assert claim(db, "a1", owner=9, lease_seconds=60)
    client = FakeClient(owned_tags=("风景",))

    result = reclassify_asset(db, client, None, make_cfg(), "a1", job_id=2)

    assert result.status == "error"
    assert client.untagged == []


def _record_tag_add(db, asset_id, name, *, tag_id=None):
    payload = {"tag_name": name}
    if tag_id:
        payload["tag_id"] = tag_id
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO change_log (asset_id, action, direction, payload) "
            "VALUES (?, 'tag_add', 'apply', ?)", (asset_id, json.dumps(payload)),
        )


def _llm_reply(content):
    return SimpleNamespace(chat=lambda messages: SimpleNamespace(content=content, model="m", usage=None))


def test_replaces_only_confirmed_owned_tags_after_valid_classification(db):
    insert_tag(db, "风景")
    insert_tag(db, "天空", "marker")
    seed_state(db, "a1", manual_override=1, last_job_id=3)
    _record_tag_add(db, "a1", "风景", tag_id="id-风景")
    # The mirror says "天空", but no successful Tagger write is recorded.
    client = FakeClient(owned_tags=("风景", "天空", "我的手打标签"))

    result = reclassify_asset(
        db, client, _llm_reply('{"tag":"人像","confidence":0.9}'),
        make_cfg(), "a1", job_id=7,
    )

    assert result.status == "ok"
    assert set(client.owned) == {"人像", "天空", "我的手打标签"}
    assert client.untagged == [("id-风景", ["a1"])]
    with db.connect() as conn:
        removes = conn.execute(
            "SELECT action, direction, job_id, payload FROM change_log WHERE action='tag_remove'"
        ).fetchall()
        row = conn.execute(
            "SELECT current_tag, last_job_id, manual_override FROM asset_state WHERE asset_id='a1'"
        ).fetchone()
    assert len(removes) == 1
    assert removes[0]["direction"] == "apply"
    assert removes[0]["job_id"] == 7
    assert json.loads(removes[0]["payload"])["tag_name"] == "风景"
    assert tuple(row) == ("人像", 7, 1)


def test_reclassify_preserves_existing_tags_when_new_output_is_invalid(db):
    seed_state(db, "a1", last_job_id=3)
    _record_tag_add(db, "a1", "风景")
    client = FakeClient(owned_tags=("风景",))

    result = reclassify_asset(
        db, client, _llm_reply('{"tag":"missing","confidence":0.9}'),
        make_cfg(), "a1", job_id=7,
    )

    assert result.status == "error"
    assert client.owned == ["风景"]
    with db.connect() as conn:
        row = conn.execute("SELECT current_tag,last_job_id FROM asset_state").fetchone()
        latest = conn.execute("SELECT apply_status FROM asset_records WHERE job_id=7").fetchone()
    assert tuple(row) == ("风景", 3)
    assert latest["apply_status"] != "pending"


def test_reclassify_calls_model_when_previous_analysis_is_unapplied(db):
    seed_state(db, "a1", last_job_id=3)
    _record_tag_add(db, "a1", "风景")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, source, status, predicted_tag, confidence) "
            "VALUES ('a1','poll','ok','风景',0.9)"
        )
    client = FakeClient(owned_tags=("风景",))

    result = reclassify_asset(
        db, client, _llm_reply('{"tag":"人像","confidence":0.9}'),
        make_cfg(), "a1", job_id=7,
    )

    assert result.tag == "人像"
    assert "人像" in client.owned and "风景" not in client.owned


def test_reclassify_preserves_a_same_name_tag_recreated_while_model_runs(db):
    _record_tag_add(db, "a1", "风景", tag_id="id-风景")
    client = FakeClient(owned_tags=("风景",))

    def chat(messages):
        client.tag_ids["风景"] = "replacement"
        return SimpleNamespace(content='{"tag":"人像","confidence":0.9}', model="m", usage=None)

    result = reclassify_asset(db, client, SimpleNamespace(chat=chat), make_cfg(), "a1", job_id=7)

    assert result.status == "ok"
    assert "风景" in client.owned
    assert client.untagged == []


def test_reclassify_keeps_an_existing_manual_metadata_marker(db):
    marker = "保留人工信息"
    _record_tag_add(db, "a1", marker, tag_id=f"id-{marker}")
    client = FakeClient(owned_tags=("风景", marker), rating=5)
    result = reclassify_asset(
        db, client, _llm_reply('{"tag":"风景","confidence":0.9,"rating":3}'),
        make_cfg(), "a1", job_id=7,
    )

    assert result.status == "ok"
    assert marker in client.owned
    assert marker in result.markers
    assert client.untagged == []


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
    assert captured["force_reanalysis"] is True
    assert captured["lease_held"] is True


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


def test_reclassify_refreshes_our_own_rating(db):
    """显式重新分类必须能刷新我们上次写的评分（§5.8.1 情形②）。

    改之前：非空 → 判定"跳过" → 新结果整份丢弃，还反手打个"保留人工信息"。
    """
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO change_log (asset_id, action, direction, payload) "
            "VALUES ('a1','rating_set','apply','{\"old\": null, \"new\": 3}')")
    client = FakeClient(rating=3)                 # 当前值 == 我们上次写的 new
    llm = SimpleNamespace(
        chat=lambda messages: SimpleNamespace(
            # 只给 rating：属性桶会把 rating 与 description 合并成**一次**
            # set_asset_properties，带上 description 会让断言里的 dict 对不上。
            content='{"tag": "风景", "confidence": 0.9, "sensitive": false, '
                    '"reason": "ok", "rating": 5}',
            model="m", usage=None),
    )
    reclassify_asset(db, client, llm, make_cfg(), "a1", job_id=7)

    props = [c for c in client.set_props_calls if "rating" in c]
    assert props == [{"asset_id": "a1", "rating": 5}]
    # 跳过标记标签不得被贴：断言在 tag id 上（tag_assets 存的是
    # (tag_ids, asset_ids) 元组，直接遍历拿不到 tag 名字符串）。
    tagged_ids = [t for ids, _ in getattr(client, "tagged", []) for t in ids]
    assert "id-保留人工信息" not in tagged_ids


def test_reclassify_resets_apply_attempts(db):
    """§7.3：改判必须重置 apply_attempts，否则"已放弃"的资产被永久钉死。"""
    with db.connect() as conn:
        conn.execute("INSERT INTO asset_state (asset_id, apply_attempts) "
                     "VALUES ('a1', 3)")
    llm = SimpleNamespace(
        chat=lambda messages: SimpleNamespace(
            content='{"tag": "风景", "confidence": 0.9, "sensitive": false, '
                    '"reason": "ok"}',
            model="m", usage=None),
    )
    reclassify_asset(db, FakeClient(), llm, make_cfg(), "a1", job_id=7)
    with db.connect() as conn:
        assert conn.execute("SELECT apply_attempts FROM asset_state "
                            "WHERE asset_id='a1'").fetchone()["apply_attempts"] == 0
