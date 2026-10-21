"""描述 effector：与 rating 同构（标签开关 write_description），独立判定。"""
from __future__ import annotations

from app.classify.effectors.base import Effect, EffectContext, PlanOutcome, tag_writes


def plan(ctx: EffectContext, last_written: dict, outcome: PlanOutcome) -> list[Effect]:
    # 同 rating：标签不写描述时直接返回，不记 outcome.skipped。
    if ctx.gates.rating_description_blocked or not tag_writes(ctx, "write_description"):
        return []
    new = ctx.analysis.description
    if new is None:
        return []

    current = ctx.snapshot.description
    if current is None:
        return [Effect("description_set", ctx.asset_id, {"old": None, "new": new})]

    if last_written.get("description_set") == current:
        if ctx.gates.allow_metadata_overwrite and current != new:
            return [Effect("description_set", ctx.asset_id, {"old": current, "new": new})]
        return []
    outcome.skipped.append("description")
    return []
