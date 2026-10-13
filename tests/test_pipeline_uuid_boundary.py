"""真机抓到的边界：Immich 生成的 DTO id 是 UUID 对象，不是 str。

单测里的 mock 全部用字符串 id，从未暴露这一点；真机 dry-run（M2 Task 8）
在 lease.claim 绑定参数时以 sqlite3.ProgrammingError 崩掉。修复是入口统一 str()。
"""
import uuid

from app.classify.pipeline import PipelineConfig, PipelineResult, classify_asset


def _cfg(**over):
    from app.classify.thumb import ThumbConfig
    from app.store.tags import TagDef

    class _FakeLLMConn:
        timeout = 180

    base = dict(
        prompt_template="{{TAG_LIST}}",
        tags=[TagDef(id=1, name="photo", description="照片", kind="primary",
                     enabled=True, archive=False, album=None, min_confidence=None)],
        marker_rules=[],
        fallback_tag="other",
        default_min_confidence=0.8,
        thumb=ThumbConfig(),
        queue_tag_name="未分类",
        llm=_FakeLLMConn(),
        concurrency_owner=1,
    )
    base.update(over)
    return PipelineConfig(**base)


class _StubDB:
    """记录 claim 收到的 asset_id 类型。"""
    def __init__(self):
        self.claimed = None

    def connect(self):
        return self


class _StubLease:
    def __init__(self, db):
        self._db = db


def test_uuid_asset_id_is_converted_to_str(monkeypatch):
    import app.classify.pipeline as pl

    seen = {}

    def fake_claim(db, asset_id, owner, lease_seconds):
        seen["asset_id"] = asset_id
        return False  # 拿不到租约 → 直接返回，不触发后续流程

    monkeypatch.setattr(pl, "claim", fake_claim)
    db = _StubDB()
    res = classify_asset(db, None, None, uuid.UUID("f5848a69-49fa-424d-a9d9-4e4a9a99e52f"),
                         _cfg(), source="manual")
    assert seen["asset_id"] == "f5848a69-49fa-424d-a9d9-4e4a9a99e52f"
    assert isinstance(seen["asset_id"], str)
    assert res.status == "error"


def test_str_asset_id_unchanged(monkeypatch):
    import app.classify.pipeline as pl

    seen = {}

    def fake_claim(db, asset_id, owner, lease_seconds):
        seen["asset_id"] = asset_id
        return False

    monkeypatch.setattr(pl, "claim", fake_claim)
    classify_asset(_StubDB(), None, None, "abc", _cfg(), source="manual")
    assert seen["asset_id"] == "abc"
