"""描述 effector：与 rating 同构，独立判定。"""
from __future__ import annotations

from app.classify.effectors.base import Effect, EffectContext, PlanOutcome


def plan(ctx: EffectContext, last_written: dict, outcome: PlanOutcome) -> list[Effect]:
    if "description" not in ctx.enabled or ctx.gates.metadata_blocked:
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
