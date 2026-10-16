"""可见性 effector：仅本系统的处理才主动设 timeline（§5.5 ②）。"""
from __future__ import annotations

from app.classify.effectors.base import Effect, EffectContext


def plan(ctx: EffectContext, last_written: dict, has_system_archive: bool = False) -> list[Effect]:
    if "visibility" not in ctx.enabled or ctx.gates.metadata_blocked:
        return []
    current = ctx.snapshot.visibility
    if ctx.archive:
        if current == "archive":
            return []
        return [Effect("archive", ctx.asset_id, {"from_visibility": current or "timeline"})]
    if has_system_archive:
        if current == "timeline":
            return []
        return [Effect("timeline", ctx.asset_id, {"from_visibility": current or "archive"})]
    return []
