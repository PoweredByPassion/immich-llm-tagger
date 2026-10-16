"""相册 effector。"""
from __future__ import annotations

from app.classify.effectors.base import Effect, EffectContext


def plan(ctx: EffectContext, last_written: dict) -> list[Effect]:
    if "album" not in ctx.enabled or ctx.gates.metadata_blocked:
        return []
    album = getattr(ctx.tag_def, "album", None) if ctx.tag_def is not None else None
    if not album:
        return []
    return [Effect("album_add", ctx.asset_id, {"album_name": album})]
