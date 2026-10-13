"""Tests for app/classify/parse.py (M2 Task 3)."""

import pytest

from app.classify.parse import extract_json, is_refusal


# ---------------------------------------------------------------------------
# extract_json: 剥围栏 → json.loads → 正则兜底 → None
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_clean_json(self):
        assert extract_json('{"tag": "screenshot", "confidence": 0.9}') == {
            "tag": "screenshot", "confidence": 0.9,
        }

    def test_markdown_fence(self):
        text = '```json\n{"tag": "screenshot", "confidence": 0.9}\n```'
        assert extract_json(text)["tag"] == "screenshot"

    def test_json_with_surrounding_prose(self):
        text = 'Here is my answer:\n{"tag": "chart"}\nHope that helps!'
        assert extract_json(text) == {"tag": "chart"}

    def test_fence_with_trailing_prose_containing_brace(self):
        # 贪婪正则兜底会从首个 { 抓到最后一个 }，必须靠剥围栏 + 直接 loads 救回
        text = '```json\n{"tag": "screenshot"}\n```\nNote: braces like } appear in text'
        assert extract_json(text) == {"tag": "screenshot"}

    def test_stray_brace_in_prose_before_json(self):
        # 散文里的未配对 { 会让"从首个 { 配对"的兜底走错起点，
        # 必须依赖剥围栏后直接 loads 命中
        text = '```json\n{"tag": "screenshot", "note": "a { inside string"}\n```\nsee {diagram'
        assert extract_json(text) == {"tag": "screenshot", "note": "a { inside string"}

    def test_unparseable_returns_none(self):
        assert extract_json("no json here at all") is None
        assert extract_json("") is None


# ---------------------------------------------------------------------------
# is_refusal
# ---------------------------------------------------------------------------

class TestIsRefusal:
    def test_refusal_markers(self):
        assert is_refusal("抱歉，我无法响应这张图片。") is True
        assert is_refusal("I'm unable to help with that.") is True

    def test_json_response_is_not_refusal(self):
        # 合法 JSON 响应绝不能被误判为拒答（即使包含"抱歉"等词也不该——以 { 开头即否决）
        assert is_refusal('{"tag": "screenshot"}') is False

    def test_json_containing_refusal_word_is_not_refusal(self):
        text = '{"tag": "screenshot", "reason": "抱歉只能说这么多"}'
        assert is_refusal(text) is False

    def test_plain_prose_not_refusal(self):
        assert is_refusal("The image shows a cat.") is False


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------

from app.classify.parse import Classification, normalize


class TestNormalize:
    def test_refusal_becomes_fallback_sensitive_no_confidence(self):
        c = normalize(
            None,
            refusal_text="抱歉，我无法响应这张图片。",
            enabled_primary_tags={"screenshot", "chart"},
            fallback_tag="other",
            marker_rules=[],
        )
        assert c == Classification(
            tag="other", confidence=None, sensitive=True, reason=None,
            markers=("sensitive",),  # 内置规则：拒答 → sensitive marker（§5.4）
        )

    def test_normal_parse(self):
        c = normalize(
            {"tag": "screenshot", "confidence": 0.9, "sensitive": False, "reason": "ui"},
            refusal_text=None,
            enabled_primary_tags={"screenshot", "chart"},
            fallback_tag="other",
            marker_rules=[],
        )
        assert c.tag == "screenshot"
        assert c.confidence == 0.9
        assert c.sensitive is False
        assert c.reason == "ui"
        assert c.markers == ()

    def test_sensitive_true_maps_unconditionally(self):
        # 即使 tag 是正常 primary tag 且 sensitive 标记不在规则里
        c = normalize(
            {"tag": "screenshot", "confidence": 0.9, "sensitive": True},
            refusal_text=None,
            enabled_primary_tags={"screenshot"},
            fallback_tag="other",
            marker_rules=[],
        )
        assert c.sensitive is True
        assert "sensitive" in c.markers

    def test_sensitive_missing_is_false(self):
        c = normalize(
            {"tag": "screenshot"},
            refusal_text=None,
            enabled_primary_tags={"screenshot"},
            fallback_tag="other",
            marker_rules=[],
        )
        assert c.sensitive is False

    def test_unknown_tag_falls_back(self):
        c = normalize(
            {"tag": "meme"},
            refusal_text=None,
            enabled_primary_tags={"screenshot"},
            fallback_tag="other",
            marker_rules=[],
        )
        assert c.tag == "other"

    def test_fallback_tag_itself_not_enabled_is_kept(self):
        # 定义：fallback_tag 不在 enabled_primary_tags 时仍原样使用，不递归归一
        c = normalize(
            {"tag": "meme"},
            refusal_text=None,
            enabled_primary_tags={"screenshot"},  # 不含 "other"
            fallback_tag="other",
            marker_rules=[],
        )
        assert c.tag == "other"

    def test_confidence_missing(self):
        c = normalize(
            {"tag": "screenshot"},
            refusal_text=None,
            enabled_primary_tags={"screenshot"},
            fallback_tag="other",
            marker_rules=[],
        )
        assert c.confidence is None

    @pytest.mark.parametrize("bad", ["high", -0.5, 1.5])
    def test_confidence_non_numeric_or_out_of_range(self, bad):
        c = normalize(
            {"tag": "screenshot", "confidence": bad},
            refusal_text=None,
            enabled_primary_tags={"screenshot"},
            fallback_tag="other",
            marker_rules=[],
        )
        assert c.confidence is None

    def test_marker_rule_confidence_lt_hit(self):
        c = normalize(
            {"tag": "screenshot", "confidence": 0.4},
            refusal_text=None,
            enabled_primary_tags={"screenshot"},
            fallback_tag="other",
            marker_rules=[("low_confidence", {"confidence_lt": 0.6})],
        )
        assert c.markers == ("low_confidence",)

    def test_marker_rule_not_hit(self):
        c = normalize(
            {"tag": "screenshot", "confidence": 0.9},
            refusal_text=None,
            enabled_primary_tags={"screenshot"},
            fallback_tag="other",
            marker_rules=[("low_confidence", {"confidence_lt": 0.6})],
        )
        assert c.markers == ()

    def test_multiple_rules_all_apply_in_order(self):
        c = normalize(
            {"tag": "screenshot", "confidence": 0.3},
            refusal_text=None,
            enabled_primary_tags={"screenshot"},
            fallback_tag="other",
            marker_rules=[
                ("b_rule", {"confidence_lt": 0.6}),
                ("a_rule", {"confidence_lt": 0.5}),
            ],
        )
        assert c.markers == ("b_rule", "a_rule")

    def test_marker_rule_condition_error_is_swallowed(self):
        # 条件求值失败不抛异常：跳过该规则
        c = normalize(
            {"tag": "screenshot", "confidence": 0.3},
            refusal_text=None,
            enabled_primary_tags={"screenshot"},
            fallback_tag="other",
            marker_rules=[
                ("bad_rule", {"confidence_lt": "not-a-number"}),
                ("good_rule", {"confidence_lt": 0.6}),
            ],
        )
        assert c.markers == ("good_rule",)

    def test_unknown_extra_fields_ignored(self):
        c = normalize(
            {"tag": "screenshot", "confidence": 0.9, "weather": "sunny", "n": 42},
            refusal_text=None,
            enabled_primary_tags={"screenshot"},
            fallback_tag="other",
            marker_rules=[],
        )
        assert c.tag == "screenshot"

    def test_unparseable_raw_yields_fallback(self):
        # raw 为 None 且没有拒答文本：视为无法解析 → fallback
        c = normalize(
            None,
            refusal_text=None,
            enabled_primary_tags={"screenshot"},
            fallback_tag="other",
            marker_rules=[],
        )
        assert c.tag == "other"
        assert c.sensitive is False


# ---------------------------------------------------------------------------
# decide_archive
# ---------------------------------------------------------------------------

from app.classify.parse import TagDef, decide_archive


class TestDecideArchive:
    def test_archive_true_confidence_meets_threshold(self):
        tag = TagDef(name="screenshot", archive=True, min_confidence=None)
        assert decide_archive(tag, 0.8, 0.7) is True

    def test_confidence_exactly_equal_threshold_is_true(self):
        tag = TagDef(name="screenshot", archive=True, min_confidence=None)
        assert decide_archive(tag, 0.7, 0.7) is True

    def test_below_threshold_false(self):
        tag = TagDef(name="screenshot", archive=True, min_confidence=None)
        assert decide_archive(tag, 0.5, 0.7) is False

    def test_tag_min_confidence_overrides_default(self):
        tag = TagDef(name="screenshot", archive=True, min_confidence=0.9)
        # 0.8 >= default 0.5 但 < tag 的 0.9
        assert decide_archive(tag, 0.8, 0.5) is False
        assert decide_archive(tag, 0.9, 0.5) is True

    def test_archive_flag_false(self):
        tag = TagDef(name="screenshot", archive=False, min_confidence=None)
        assert decide_archive(tag, 0.99, 0.5) is False

    def test_confidence_none_false(self):
        tag = TagDef(name="screenshot", archive=True, min_confidence=None)
        assert decide_archive(tag, None, 0.5) is False

    def test_tag_none_false(self):
        assert decide_archive(None, 0.9, 0.5) is False


class TestExtractJsonOrder:
    def test_fence_with_brace_on_lang_line(self):
        # 围栏语言行本身含 {}：无论剥围栏还是兜底，首个平衡块都是 {"a": 1}，
        # 两个候选 JSON 中「完整、位于围栏内」的语义上更可能是答案。
        # 现实现按解析顺序返回首个可解析平衡块 {"a": 1} —— 固化该行为。
        text = '```json {"a": 1}\n{"tag": "screenshot"}\n```'
        assert extract_json(text) == {"a": 1}


class TestExtractJsonPrecedence:
    def test_direct_loads_wins_over_later_blocks(self):
        # 解析顺序：直接 loads 成功即返回首个完整 JSON，不被后续块覆盖
        assert extract_json('{"tag": "a"}\n{"tag": "b"}') == {"tag": "a"}

    def test_fenced_json_wins_over_later_prose_json(self):
        # 剥围栏后直接 loads 命中围栏内 JSON，而不是正文后附的另一块
        text = '```json\n{"tag": "a"}\n```\nand also {"tag": "b"}'
        assert extract_json(text) == {"tag": "a"}


class TestMarkerBoundary:
    def test_confidence_exactly_equal_threshold_not_hit(self):
        # confidence_lt 严格小于：恰等于阈值不命中（区分 < 与 <=）
        c = normalize(
            {"tag": "screenshot", "confidence": 0.6},
            refusal_text=None,
            enabled_primary_tags={"screenshot"},
            fallback_tag="other",
            marker_rules=[("low_confidence", {"confidence_lt": 0.6})],
        )
        assert c.markers == ()
