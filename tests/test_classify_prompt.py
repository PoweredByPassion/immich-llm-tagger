"""Tests for app/classify/prompt.py — Prompt 渲染器."""

import pytest

from app.classify.prompt import PromptContext, build_tag_list_block, render_prompt


class TestBuildTagListBlock:
    def test_formats_each_tag_as_dash_name_description_lines(self):
        tags = [("screenshot", "屏幕截图"), ("meme", "搞笑图片")]
        assert build_tag_list_block(tags) == (
            "- screenshot: 屏幕截图\n- meme: 搞笑图片"
        )


class TestRenderPromptTagList:
    def test_tag_list_replaced_in_template(self):
        ctx = PromptContext(tag_list=[("screenshot", "屏幕截图")])
        assert render_prompt("Tags:\n{{TAG_LIST}}\nEnd", ctx) == (
            "Tags:\n- screenshot: 屏幕截图\nEnd"
        )

    def test_empty_tag_list_renders_empty_string(self):
        # 定义：空 tag 列表 → {{TAG_LIST}} 替换为空串（不改成整行省略）
        ctx = PromptContext(tag_list=[])
        assert render_prompt("Tags:\n{{TAG_LIST}}\nEnd", ctx) == "Tags:\n\nEnd"


class TestOptionalPlaceholders:
    def test_filename_present_replaced(self):
        ctx = PromptContext(tag_list=[], filename="IMG_001.png")
        assert render_prompt("File: {{FILENAME}}", ctx) == "File: IMG_001.png"

    def test_filename_missing_line_removed_entirely(self):
        ctx = PromptContext(tag_list=[])
        result = render_prompt("Hello\nFile: {{FILENAME}}\nWorld", ctx)
        assert result == "Hello\nWorld"

    def test_scope_missing_line_removed(self):
        ctx = PromptContext(tag_list=[])
        assert render_prompt("A\nScope: {{SCOPE}}\nB", ctx) == "A\nB"

    def test_date_present_and_missing(self):
        ctx = PromptContext(tag_list=[], date="2026-01-02")
        assert render_prompt("D: {{DATE}}", ctx) == "D: 2026-01-02"
        assert render_prompt("A\nD: {{DATE}}\nB", PromptContext(tag_list=[])) == "A\nB"

    def test_no_extra_blank_line_when_last_line_removed(self):
        ctx = PromptContext(tag_list=[])
        assert render_prompt("A\nScope: {{SCOPE}}\n", ctx) == "A\n"

    def test_same_placeholder_multiple_occurrences_all_replaced(self):
        ctx = PromptContext(tag_list=[], filename="x.png")
        assert render_prompt("{{FILENAME}} and {{FILENAME}}", ctx) == "x.png and x.png"

    def test_unknown_placeholder_preserved(self):
        ctx = PromptContext(tag_list=[], filename="x.png")
        assert render_prompt("A {{UNKNOWN}} B {{FILENAME}}", ctx) == "A {{UNKNOWN}} B x.png"


class TestExif:
    def test_exif_present_rendered(self):
        ctx = PromptContext(
            tag_list=[],
            exif={"make": "Apple", "model": "iPhone 15", "iso": 100, "city": "深圳"},
        )
        result = render_prompt("{{EXIF}}", ctx)
        assert "厂商: Apple" in result
        assert "型号: iPhone 15" in result
        assert "ISO: 100" in result
        assert "城市: 深圳" in result

    def test_exif_partial_none_fields_skipped(self):
        ctx = PromptContext(
            tag_list=[],
            exif={"make": "Apple", "model": "iPhone 15", "focal_length": None,
                  "iso": None, "city": None},
        )
        result = render_prompt("{{EXIF}}", ctx)
        assert "None" not in result
        assert "焦距" not in result
        assert "厂商: Apple" in result

    def test_exif_all_none_line_removed(self):
        ctx = PromptContext(tag_list=[], exif={"make": None, "iso": None})
        assert render_prompt("A\n{{EXIF}}\nB", ctx) == "A\nB"

    def test_exif_missing_line_removed(self):
        assert render_prompt("A\n{{EXIF}}\nB", PromptContext(tag_list=[])) == "A\nB"

    def test_multiple_tags_all_rendered_through_render_prompt(self):
        ctx = PromptContext(tag_list=[("a", "A"), ("b", "B"), ("c", "C")])
        assert render_prompt("{{TAG_LIST}}", ctx) == "- a: A\n- b: B\n- c: C"
