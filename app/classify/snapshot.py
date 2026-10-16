"""资产的「当前值」只读快照。

两个来源的接口必须在这里抹平：
- webhook hint：exifInfo 是**原生 dict**；
- client.get_asset()：返回 **ExifResponseDto**，字段可能为 `Unset`。

⚠ Unset 陷阱：生成的 DTO 在**键缺失**时给的是 `Unset` 哨兵而不是 `None`。
若用 `x is not None` 判断"字段是否为空"，`Unset is not None` 为真 →
每张图都会被误判为"已有评分" → 评分永不写入。所有取值必须过 `_val`。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:  # pragma: no cover - 生成客户端在 make generate 后才存在
    from app.clients._immich_gen.types import Unset
except Exception:  # pragma: no cover
    class Unset:  # type: ignore[no-redef]
        pass


def _val(x: Any) -> Any:
    """把 Unset 哨兵折叠成 None；其余原样返回。"""
    return None if isinstance(x, Unset) else x


def _blank_to_none(x: Any) -> str | None:
    if isinstance(x, str) and x.strip():
        return x.strip()
    return None


@dataclass(frozen=True)
class AssetSnapshot:
    rating: int | None = None
    description: str | None = None
    visibility: str | None = None
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dto(cls, dto: Any) -> "AssetSnapshot":
        exif = getattr(dto, "exif_info", None)
        rating = _val(getattr(exif, "rating", None)) if exif is not None else None
        desc = _val(getattr(exif, "description", None)) if exif is not None else None
        tags = getattr(dto, "tags", None) or []
        return cls(
            rating=rating if isinstance(rating, int) else None,
            description=_blank_to_none(desc),
            visibility=_vis_str(_val(getattr(dto, "visibility", None))),
            tags=tuple(str(getattr(t, "name", t)) for t in tags),
        )

    @classmethod
    def from_hint(cls, hint: dict) -> "AssetSnapshot":
        exif = hint.get("exifInfo") if isinstance(hint, dict) else None
        exif = exif if isinstance(exif, dict) else {}
        rating = exif.get("rating")
        tags = hint.get("tags") or [] if isinstance(hint, dict) else []
        return cls(
            rating=rating if isinstance(rating, int) else None,
            description=_blank_to_none(exif.get("description")),
            visibility=None,   # `_HINT_FIELDS` 不含 visibility（hooks.py:40）
            tags=tuple(str(t.get("name", t) if isinstance(t, dict) else t) for t in tags),
        )


def _vis_str(v: Any) -> str | None:
    """AssetVisibility 枚举 / 字符串 → 纯字符串。"""
    if v is None:
        return None
    return getattr(v, "value", None) or str(v)
