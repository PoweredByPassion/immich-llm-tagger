"""评分 effector：字段非空判定 + 三步跳过规则（§5.8.1）。"""
from __future__ import annotations

from app.classify.effectors.base import Effect, EffectContext, PlanOutcome


def plan(ctx: EffectContext, last_written: dict, outcome: PlanOutcome) -> list[Effect]:
    if "rating" not in ctx.enabled or ctx.gates.metadata_blocked:
        return []
    new = ctx.analysis.rating
    if new is None:
        return []

    current = ctx.snapshot.rating
    if current is None:
        return [Effect("rating_set", ctx.asset_id, {"old": None, "new": new})]

    if last_written.get("rating_set") == current:
        # 是我们自己上次写的
        if ctx.gates.allow_metadata_overwrite and current != new:
            return [Effect("rating_set", ctx.asset_id, {"old": current, "new": new})]
        return []                      # 值已正确；不打"保留人工信息"
    outcome.skipped.append("rating")   # 用户写的 → 跳过 + 打标记
    return []
