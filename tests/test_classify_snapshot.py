"""Tests for app/classify/snapshot.py (M3 Task 6)."""


def test_unset_is_folded_to_none():
    """生成的 DTO 字段缺失时是 Unset 而非 None。
    若直接 hasattr / `is not None` 判断，每张图都会被误判"已有评分"。"""
    from app.classify.snapshot import _val
    from app.clients._immich_gen.types import UNSET

    assert _val(UNSET) is None
    assert _val(None) is None
    assert _val(4) == 4
    assert _val("x") == "x"


def test_from_dto_reads_exif_rating_and_description():
    from types import SimpleNamespace as NS
    from app.classify.snapshot import AssetSnapshot

    dto = NS(exif_info=NS(rating=4, description="一只猫"),
             visibility="timeline", tags=[])
    snap = AssetSnapshot.from_dto(dto)
    assert snap.rating == 4 and snap.description == "一只猫"
    assert snap.visibility == "timeline"


def test_from_dto_without_exif_info():
    from types import SimpleNamespace as NS
    from app.classify.snapshot import AssetSnapshot

    snap = AssetSnapshot.from_dto(NS(exif_info=None, visibility="timeline", tags=[]))
    assert snap.rating is None and snap.description is None


def test_from_hint_reads_plain_dict_exif():
    """webhook hint 的 exifInfo 是原生 dict，不是 DTO —— 两种来源必须抹平。"""
    from app.classify.snapshot import AssetSnapshot

    hint = {"exifInfo": {"rating": 3, "description": "海边"}, "tags": []}
    snap = AssetSnapshot.from_hint(hint)
    assert snap.rating == 3 and snap.description == "海边"
    assert snap.visibility is None      # hint 不带 visibility
