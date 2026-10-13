import pytest

from app.store.errors import ValidationError
from app.store.validation import TagRow, validate_config


def _tag(id_: int, name: str, kind: str = "primary", enabled: bool = True) -> TagRow:
    return TagRow(id=id_, name=name, kind=kind, enabled=enabled)


def test_valid_config_passes() -> None:
    validate_config(
        tags=[_tag(1, "screenshot"), _tag(2, "sensitive", kind="marker")],
        marker_rules=[(1, 2)],          # (rule_id, marker_tag_id)
        fallback_tag_name="screenshot",
        active_prompt_version_id=None,
        prompt_version_ids=set(),
    )


def test_marker_rule_must_point_at_marker_tag() -> None:
    with pytest.raises(ValidationError) as e:
        validate_config(
            tags=[_tag(1, "screenshot"), _tag(2, "sensitive", kind="marker")],
            marker_rules=[(1, 1)],      # 指向 primary，非法
            fallback_tag_name="screenshot",
            active_prompt_version_id=None,
            prompt_version_ids=set(),
        )
    # 不能只断言 "marker"：每条 marker 消息都以「marker 规则」开头，
    # 那样断言在"tag 不存在"时同样会通过（实测过）。
    # 必须断言错误信息**区分**了「kind 不对」这件事。
    assert e.value.problems[0].key == "store.validation.marker_tag_kind"


def test_marker_rule_must_point_at_existing_tag() -> None:
    with pytest.raises(ValidationError) as e:
        validate_config(
            tags=[_tag(1, "screenshot")],
            marker_rules=[(1, 404)],
            fallback_tag_name="screenshot",
            active_prompt_version_id=None,
            prompt_version_ids=set(),
        )
    # 与上一条互补：这条钉住「不存在」的措辞，那条钉住「kind 不对」的措辞，
    # 两者合起来才能防止两个分支被合并成一个。
    assert e.value.problems[0].key == "store.validation.marker_tag_missing"


def test_fallback_tag_must_exist() -> None:
    with pytest.raises(ValidationError) as e:
        validate_config(
            tags=[_tag(1, "screenshot")],
            marker_rules=[],
            fallback_tag_name="other",
            active_prompt_version_id=None,
            prompt_version_ids=set(),
        )
    assert "other" in e.value.problems[0].args


def test_fallback_tag_must_be_enabled() -> None:
    with pytest.raises(ValidationError) as e:
        validate_config(
            tags=[_tag(1, "other", enabled=False)],
            marker_rules=[],
            fallback_tag_name="other",
            active_prompt_version_id=None,
            prompt_version_ids=set(),
        )
    assert e.value.problems[0].key == "store.validation.fallback_disabled"


def test_fallback_tag_must_be_primary() -> None:
    with pytest.raises(ValidationError):
        validate_config(
            tags=[_tag(1, "other", kind="marker")],
            marker_rules=[],
            fallback_tag_name="other",
            active_prompt_version_id=None,
            prompt_version_ids=set(),
        )


def test_active_prompt_version_must_exist() -> None:
    with pytest.raises(ValidationError) as e:
        validate_config(
            tags=[_tag(1, "other")],
            marker_rules=[],
            fallback_tag_name="other",
            active_prompt_version_id=7,
            prompt_version_ids={1, 2},
        )
    assert e.value.problems[0].key == "store.validation.active_prompt_missing"


def test_active_prompt_version_null_is_fine() -> None:
    validate_config(
        tags=[_tag(1, "other")],
        marker_rules=[],
        fallback_tag_name="other",
        active_prompt_version_id=None,
        prompt_version_ids=set(),
    )


def test_all_errors_are_collected_not_just_first() -> None:
    with pytest.raises(ValidationError) as e:
        validate_config(
            tags=[],
            marker_rules=[(1, 999)],
            fallback_tag_name="other",
            active_prompt_version_id=99,
            prompt_version_ids=set(),
        )
    assert len(e.value.problems) >= 3


def test_all_marker_rules_are_checked_not_just_the_first() -> None:
    """同上：marker_rules 的循环不能被截断。"""
    with pytest.raises(ValidationError) as e:
        validate_config(
            tags=[_tag(1, "screenshot"), _tag(2, "sensitive", kind="marker")],
            marker_rules=[(1, 2), (2, 999)],   # 第二条非法
            fallback_tag_name="screenshot",
            active_prompt_version_id=None,
            prompt_version_ids=set(),
        )
    assert any(999 in p.args for p in e.value.problems)


def test_error_message_joins_problems_with_fullwidth_semicolon() -> None:
    """多条问题时用全角分号连接 —— 这是 UI 直接展示给用户的格式。"""
    with pytest.raises(ValidationError) as e:
        validate_config(
            tags=[],
            marker_rules=[(1, 888)],
            fallback_tag_name="other",
            active_prompt_version_id=None,
            prompt_version_ids=set(),
        )
    text = str(e.value)
    assert "；" in text
    assert text.count("；") == len(e.value.problems) - 1
