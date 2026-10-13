"""缩略图取图与编码：把 Immich 缩略图转成 base64 JPEG 供视觉 LLM 使用。

取图顺序：先 preview，**仅 404** 才退到 thumbnail；非 404 的错误原样抛出，
绝不能当成"换个小图就行"——那会把 401/500 等真实配置错误掩盖掉。
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from PIL import Image

from app.clients.immich import ImmichClient, ImmichError

PRESETS: dict[str, int] = {"large": 1280, "medium": 720, "small": 480}


@dataclass(frozen=True)
class ThumbConfig:
    preset: str = "large"
    quality: int = 85


def _target(preset: str) -> int:
    try:
        return PRESETS[preset]
    except KeyError as exc:
        raise ValueError(f"未知 preset: {preset!r}，可选: {sorted(PRESETS)}") from exc


def _scale(im: Image.Image, target: int) -> Image.Image:
    # 等比缩放且不放大：原图长边不足目标时保持原尺寸。
    w, h = im.size
    if max(w, h) <= target:
        return im
    ratio = target / max(w, h)
    return im.resize((round(w * ratio), round(h * ratio)), Image.LANCZOS)


def encode_for_llm(raw: bytes, cfg: ThumbConfig) -> str:
    """把图片字节转成 base64（不含 data: 前缀）的 JPEG。"""
    target = _target(cfg.preset)
    with Image.open(io.BytesIO(raw)) as im:
        im = im.convert("RGB")  # HEIC/PNG alpha/灰度直接存 JPEG 会失败，先统一转 RGB
        im = _scale(im, target)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=cfg.quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def fetch_and_encode(client: ImmichClient, asset_id: str, cfg: ThumbConfig) -> str:
    try:
        raw = client.fetch_thumbnail_bytes(asset_id, "preview")
    except ImmichError as exc:
        if exc.status_code != 404:
            raise  # 非 404（401/500/网络）是真错误，不 fallback
        raw = client.fetch_thumbnail_bytes(asset_id, "thumbnail")
    return encode_for_llm(raw, cfg)
