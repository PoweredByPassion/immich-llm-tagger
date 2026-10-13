"""app/classify/thumb 的测试。"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from app.classify.thumb import PRESETS, ThumbConfig, encode_for_llm, fetch_and_encode
from app.clients.immich import ImmichError


def make_png(size: tuple[int, int], mode: str = "RGB", color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


def decoded_dimensions(b64: str) -> tuple[int, int]:
    raw = base64.b64decode(b64)
    with Image.open(io.BytesIO(raw)) as im:
        return im.size


class TestPresets:
    def test_preset_table(self):
        assert PRESETS == {"large": 1280, "medium": 720, "small": 480}


class TestEncodeScaling:
    def test_large_scales_long_edge_to_1280(self):
        raw = make_png((2000, 1000))
        out = encode_for_llm(raw, ThumbConfig(preset="large"))
        assert decoded_dimensions(out) == (1280, 640)

    def test_small_scales_long_edge_to_480(self):
        raw = make_png((1000, 500))
        out = encode_for_llm(raw, ThumbConfig(preset="small"))
        assert decoded_dimensions(out) == (480, 240)

    def test_no_upscale_when_source_smaller(self):
        raw = make_png((400, 200))
        out = encode_for_llm(raw, ThumbConfig(preset="large"))
        assert decoded_dimensions(out) == (400, 200)

    def test_upscale_guard_deterministic(self):
        # 回归测试：不放大守卫必须在 max>=target 时也成立（等号边界）
        raw = make_png((1280, 640))
        out = encode_for_llm(raw, ThumbConfig(preset="large"))
        assert decoded_dimensions(out) == (1280, 640)

    def test_resize_uses_smooth_interpolation(self):
        # mock resize 的 resample 实参，断言是 LANCZOS 而非 NEAREST。
        from unittest.mock import patch

        raw = make_png((2000, 1000))
        captured = {}
        orig_resize = Image.Image.resize

        def spy_resize(self, size, resample=None, **params):
            captured["resample"] = resample
            return orig_resize(self, size, resample=resample, **params)

        with patch.object(Image.Image, "resize", spy_resize):
            encode_for_llm(raw, ThumbConfig(preset="large"))
        assert captured["resample"] == Image.LANCZOS

    def test_unknown_preset_raises(self):
        with pytest.raises(ValueError):
            encode_for_llm(make_png((100, 100)), ThumbConfig(preset="huge"))


class FakeClient:
    """记录取图顺序，可按 size 注入行为。"""

    def __init__(self, behavior: dict[str, object] | None = None):
        self.calls: list[str] = []
        self.behavior = behavior or {}
        # behavior: size -> bytes | Exception

    def fetch_thumbnail_bytes(self, asset_id: str, size: str = "preview") -> bytes:
        self.calls.append(size)
        item = self.behavior.get(size, make_png((100, 80)))
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]


class TestFetchAndEncode:
    def test_preview_first(self):
        client = FakeClient()
        out = fetch_and_encode(client, "a1", ThumbConfig())
        assert client.calls == ["preview"]
        assert base64.b64decode(out)

    def test_404_falls_back_to_thumbnail(self):
        client = FakeClient({"preview": ImmichError("nf", status_code=404)})
        fetch_and_encode(client, "a1", ThumbConfig())
        assert client.calls == ["preview", "thumbnail"]

    def test_non_404_does_not_fallback(self):
        client = FakeClient(
            {
                "preview": ImmichError("boom", status_code=500),
                "thumbnail": make_png((100, 80)),
            }
        )
        with pytest.raises(ImmichError) as exc_info:
            fetch_and_encode(client, "a1", ThumbConfig())
        assert client.calls == ["preview"]  # 没有退到 thumbnail
        assert exc_info.value.status_code == 500

    def test_network_error_status_none_does_not_fallback(self):
        client = FakeClient({"preview": ImmichError("net", status_code=None)})
        with pytest.raises(ImmichError):
            fetch_and_encode(client, "a1", ThumbConfig())
        assert client.calls == ["preview"]

    def test_fallback_also_404_fails(self):
        client = FakeClient(
            {
                "preview": ImmichError("nf", status_code=404),
                "thumbnail": ImmichError("nf2", status_code=404),
            }
        )
        with pytest.raises(ImmichError):
            fetch_and_encode(client, "a1", ThumbConfig())
        assert client.calls == ["preview", "thumbnail"]


class TestQualityAndTranscode:
    def test_quality_passed_to_jpeg_encoder(self):
        # mock 编码器参数：监控 Image.save 的 quality 实参
        from unittest.mock import patch

        raw = make_png((100, 100))
        captured = {}
        orig_save = Image.Image.save

        def spy_save(self, fp, format=None, **params):
            captured["format"] = format
            captured.update(params)
            return orig_save(self, fp, format=format, **params)

        with patch.object(Image.Image, "save", spy_save):
            encode_for_llm(raw, ThumbConfig(quality=42))
        assert captured.get("quality") == 42
        assert captured.get("format") == "JPEG"

    def test_alpha_png_transcodes(self):
        raw = make_png((100, 100), mode="RGBA", color=(0, 255, 0, 128))
        out = encode_for_llm(raw, ThumbConfig(preset="small"))
        b = base64.b64decode(out)
        with Image.open(io.BytesIO(b)) as im:
            assert im.format == "JPEG"
            assert im.mode == "RGB"

    def test_grayscale_transcodes(self):
        raw = make_png((100, 100), mode="L", color=128)
        out = encode_for_llm(raw, ThumbConfig())
        with Image.open(io.BytesIO(base64.b64decode(out))) as im:
            assert im.format == "JPEG"
            assert im.mode == "RGB"

    def test_base64_roundtrip_and_size(self):
        raw = make_png((2000, 1000))
        out = encode_for_llm(raw, ThumbConfig(preset="medium"))
        b = base64.b64decode(out, validate=True)  # 严格校验，保证 base64 与字节一致
        with Image.open(io.BytesIO(b)) as im:
            assert im.format == "JPEG"
            assert im.size == (720, 360)
